"""F1 执行原语：真正的 `spawn`（进程创建、有界 stdio、超时杀进程树、退出码/信号）。

**先说清楚：这不是沙箱。** `LocalExecBackend.capabilities.isolation = "none"`：
argv 白名单、cwd 限制、env 白名单都是**策略约束**，不是隔离——子进程照样能读
整个磁盘、能联网、能 fork。真正的隔离（Seatbelt / Landlock / AppContainer /
容器）不在 v1 的交付范围（`docs/TECH-ARCH.md` §6），本模块不假装有。

四条实现约定：

1. **判定先于创建。** 所有调用先走 F2 的 `check_spawn()`；`pty=True` 还要
   单独过"策略许可 + 后端能力"两道门。策略不允许（`PolicyDenied`）或后端
   做不到（`PolicyUnsupported`）都在 `Popen` 之前抛出，绝不"降级后照跑"。
   **PTY 是每次调用的请求，不是策略里的模式开关**：`policy.allow_pty` 只
   回答"允不允许"，不回答"这次要不要用"——否则所有子进程都会以 tty 语义
   运行（`isatty()` 变真、行尾 CRLF、stdin 不再是 DEVNULL），差异极难事后归因。
2. **stdio 并发读、有界。** 两条流各一个线程；超过 `max_output_bytes` 后
   **继续排空但不保留**——停止读取会让子进程写满管道而永久阻塞，超时逻辑就
   永远等不到。截断的完整原文按内容寻址落盘（`stdout_ref` / `stderr_ref`），
   没有 `blob_dir` 时只报 `truncated`、不留 ref。
3. **超时杀进程组。** POSIX：`start_new_session=True` + `os.killpg(SIGKILL)`。
   Windows 零依赖（无 pywin32/ctypes Job Object）：能力声明
   `supports_process_group=False`，只 `kill()` 直接子进程，结果里
   `descendants_may_survive=True`——孙进程可能存活，不假装杀了树。
4. **退出码、信号、超时是三件事。** 被信号杀死：`exit_code=None, signal=9`；
   超时：`timed_out=True` 且 `exit_code=None`。不把超时伪装成退出码 1，也不把
   信号死亡报成"失败退出码"。

`spawn()` 是同步的：等进程结束（或被超时杀掉）后返回 `ExecResult`。异步句柄、
取消 API、PTY 交互写入是后续工单的事，本模块不做模型可见的工具。

`ExecResult.stderr_merged` 说明 stderr 是否被迫合并进 stdout：PTY 模式下
终端只有一条流，stderr 不可分离；管道模式恒为 False。
"""

from __future__ import annotations

import hashlib
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from .errors import TianshuError
from .execpolicy import (
    FEATURE_PTY,
    ISOLATION_BOUNDARIES,
    ISOLATION_BOUNDARY_CREDENTIALS,
    ISOLATION_BOUNDARY_FILES,
    ISOLATION_BOUNDARY_NETWORK,
    ISOLATION_BOUNDARY_PROCESS_TREE,
    ISOLATION_NONE,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    check_capabilities,
    check_spawn,
    filtered_env,
)

READ_CHUNK = 64 * 1024
"""每次 read 的字节数。足够大以减少 syscall，又不会让单次内存尖峰太夸张。"""

_JOIN_TIMEOUT_S = 5.0
"""进程退出后等读线程排空的总上限，超过就通知读线程自行关闭读端。"""

RULE_PTY_NOT_ALLOWED = "f2.pty_not_allowed"
"""本次调用请求了 PTY，但策略没允许（`ExecPolicy.allow_pty=False`）。

规则 id 沿用 `f2.` 前缀：它是一条**策略**拒绝（改策略能解决），不是后端
能力问题（换后端才能解决），UI 要能把两者分开。
"""


def local_capabilities() -> ExecBackendCapabilities:
    """本机本地后端的能力声明。**平台做不到的一律 False**，不虚标。

    - `isolation="none"`：策略约束不是隔离（见模块 docstring）。
    - `supports_pty` / `supports_signals` / `supports_process_group`：
      只有 POSIX 为 True；Windows 的标准库拿不到 PTY、没有 Unix 信号、
      零依赖也拿不到 Job Object。
    - `supports_job_objects` / `supports_network_isolation`：本实现都没有。
    """
    posix = os.name == "posix"
    return ExecBackendCapabilities(
        name="local-posix" if posix else "local-windows",
        isolation=ISOLATION_NONE,
        supports_pty=posix,
        supports_signals=posix,
        supports_process_group=posix,
        supports_job_objects=False,
        supports_network_isolation=False,
    )


#: 当前进程所在平台的本地后端能力（如实声明，见 `local_capabilities()`）。
LOCAL_EXEC_CAPABILITIES = local_capabilities()


# --------------------------------------------------------------- 隔离边界声明
#
# PRD §8.1.1 的四个边界。这里只放**只读声明**：声明是"后端自己说它做得到什么"，
# 不是实测证据。真正的证据由 `isolation.py` 的真实子进程探针产出；声明与探针
# 结果不一致时（例如后端自称隔离、探针却看到子进程能读写任意路径），门按
# 探针证据拒绝——声明不能自己给自己发通行证。

#: 边界常量定义在 `execpolicy.py`（策略要引用它们做封闭枚举），这里 re-export
#: 是为了既有 import 路径（`isolation.py` / 测试 / 文档）不被迫改。


#: 探针结论三态。`unknown` 与 `not_enforced` 一样阻断依赖能力——
#: "没证明隔离"不等于"隔离成立"。
BOUNDARY_ENFORCED = "enforced"
BOUNDARY_NOT_ENFORCED = "not_enforced"
BOUNDARY_UNKNOWN = "unknown"


def platform_label() -> str:
    """本机平台标签，例如 `macos-arm64` / `windows-x64`。

    只读事实（`sys.platform` + `platform.machine()`），用于隔离矩阵与探针报告
    定位证据；它不是任何"支持"宣称。
    """
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x64"}.get(machine, machine) or "unknown"
    if sys.platform == "darwin":
        os_name = "macos"
    elif sys.platform.startswith("win"):
        os_name = "windows"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    else:
        os_name = sys.platform or "unknown"
    return f"{os_name}-{machine}"


def local_isolation_claims() -> dict[str, str]:
    """本地后端对四边界的**声明**（不是实测证据，见本节说明）。

    依据 `local_capabilities()` 如实给出：`isolation="none"` 意味着文件与凭据
    边界都没有隔离；`supports_network_isolation=False` 意味着出网不受控；
    进程树终止只在 `supports_process_group=True` 时声明能做到，否则是 unknown
    （Windows 零依赖没有 Job Object，杀不干净子树——不能声明 enforced）。
    """
    caps = LOCAL_EXEC_CAPABILITIES
    file_claim = (
        BOUNDARY_NOT_ENFORCED if caps.isolation == "none" else BOUNDARY_UNKNOWN
    )
    network_claim = (
        BOUNDARY_NOT_ENFORCED
        if not caps.supports_network_isolation
        else BOUNDARY_ENFORCED
    )
    process_claim = (
        BOUNDARY_ENFORCED if caps.supports_process_group else BOUNDARY_UNKNOWN
    )
    return {
        ISOLATION_BOUNDARY_FILES: file_claim,
        ISOLATION_BOUNDARY_NETWORK: network_claim,
        ISOLATION_BOUNDARY_CREDENTIALS: file_claim,
        ISOLATION_BOUNDARY_PROCESS_TREE: process_claim,
    }


@dataclass(frozen=True)
class ExecResult:
    """一次 `spawn` 的结论。退出码、信号、超时三者分开。

    `exit_code` 与 `signal` 互斥：正常退出时 `signal is None`；被信号杀死时
    `exit_code is None, signal=<signum>`。超时是第三种状态：`timed_out=True`，
    且 `exit_code is None`（即使是我们自己发的 SIGKILL，也不把它算成"程序
    返回了 9"）。

    `stdout` / `stderr` 是**已截断**的文本（按字节上限截断后再解码，非法
    UTF-8 用替换字符）。`*_ref` 是完整原文的 sha256；只有截断且给了
    `blob_dir` 时才有值（blob 在 `<blob_dir>/<sha256>`）。

    `stderr_merged`：PTY 模式下 stderr 与 stdout 是同一条终端流，stderr
    不可分离——此时 `stderr` 恒空、内容都在 `stdout` 里，本字段为 True；
    管道模式恒为 False，`stderr` 可独立读取。

    `descendants_may_survive`：本次执行是否**无法确认**子树已经终止。只在
    超时/终止时有意义——Windows 只 kill 直接子进程时必为 True；POSIX 用
    进程组杀掉时为 False。正常退出时不对子树做任何声明，恒为 False。
    """

    exit_code: int | None
    signal: int | None
    timed_out: bool
    stdout: str
    stderr: str
    truncated: bool
    stdout_ref: str | None
    stderr_ref: str | None
    duration_ms: int
    stderr_merged: bool = False
    descendants_may_survive: bool = False


@dataclass(frozen=True)
class LocalExecBackend:
    """本机后端：能力声明 + spawn。**没有隔离**，只有策略约束。"""

    capabilities: ExecBackendCapabilities = field(default_factory=local_capabilities)

    def spawn(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | os.PathLike[str],
        env: dict[str, str],
        policy: ExecPolicy,
        blob_dir: Path | None = None,
        pty: bool = False,
    ) -> ExecResult:
        return spawn(
            argv,
            cwd=cwd,
            env=env,
            policy=policy,
            capabilities=self.capabilities,
            blob_dir=blob_dir,
            pty=pty,
        )


LOCAL_EXEC_BACKEND = LocalExecBackend()


def spawn(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str],
    policy: ExecPolicy,
    capabilities: ExecBackendCapabilities,
    blob_dir: Path | None = None,
    pty: bool = False,
) -> ExecResult:
    """按策略创建并等待一个进程。不通过就抛，不降级。

    `pty` 是**本次调用**的请求（默认 False，走管道）：

    - `pty=True` 且 `policy.allow_pty=False` → `PolicyDenied`（策略不允许）；
    - `pty=True`、策略允许、但后端 `supports_pty=False` → `PolicyUnsupported`；
    - `pty=False` → 走管道。`policy.allow_pty=True` 只是"允许"，不会自动把
      调用变成 PTY（否则 `isatty()`/CRLF/stdin 全都会悄悄变掉）。

    `env` 可以是原始环境：这里会先 `filtered_env()` 过滤，再用过滤后的环境
    过 `check_spawn()` 并交给子进程（未列出的变量不会出现在子进程里）。
    """
    if not isinstance(policy, ExecPolicy):
        raise TypeError(f"spawn 需要 ExecPolicy，收到：{type(policy).__name__}")
    if not isinstance(capabilities, ExecBackendCapabilities):
        raise TypeError(
            "spawn 需要 ExecBackendCapabilities，收到："
            f"{type(capabilities).__name__}"
        )

    child_env = filtered_env(policy, env)
    check_spawn(policy, argv, cwd, child_env)

    if pty:
        # 第一道门：策略许可（"允不允许用 PTY"，不是"这次要不要用"）。
        if not policy.allow_pty:
            raise PolicyDenied(
                "本次调用请求了 PTY，但策略没有允许"
                "（ExecPolicy.allow_pty=False）：改策略，或本次改用管道",
                rule_id=RULE_PTY_NOT_ALLOWED,
            )
        # 第二道门：后端能力。策略允许 ≠ 这台机器做得到。
        check_capabilities(policy, capabilities)
        if os.name != "posix":
            # 能力声明已经先拦过一次；这里再拦是因为调用方可能自己伪造 caps。
            raise PolicyUnsupported(
                "本实现没有 Windows 的 PTY（标准库没有 conpty），"
                "不会用管道假装",
                backend=capabilities.name,
                feature=FEATURE_PTY,
            )
        return _spawn_pty(
            argv, cwd=cwd, env=child_env, policy=policy, blob_dir=blob_dir
        )

    return _spawn_pipes(argv, cwd=cwd, env=child_env, policy=policy, blob_dir=blob_dir)


# --------------------------------------------------------------- 管道实现


def _spawn_pipes(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str],
    policy: ExecPolicy,
    blob_dir: Path | None,
) -> ExecResult:
    started = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        cwd=str(Path(cwd).resolve()),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=(os.name == "posix"),
    )
    assert proc.stdout is not None and proc.stderr is not None
    # Python 3.12 的 os.set_blocking 也支持 Windows 管道。读端必须可停止：
    # 孙进程可能仍握着写端；跨线程 close 正在 read 的 BufferedReader 会等待
    # 它的内部锁，反而让有界收尾永久阻塞。
    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)

    out = _StreamCollector(policy.max_output_bytes, blob_dir, "stdout")
    err = _StreamCollector(policy.max_output_bytes, blob_dir, "stderr")
    stop_reading = threading.Event()
    threads = [
        _start_pump(_pump_file, proc.stdout, out, stop_reading),
        _start_pump(_pump_file, proc.stderr, err, stop_reading),
    ]

    timed_out, tree_killed = _wait_with_timeout(proc, policy.timeout_ms)
    _join_pumps(threads, stop_reading)
    return _build_result(
        proc,
        out,
        err,
        timed_out=timed_out,
        tree_killed=tree_killed,
        duration_ms=_elapsed_ms(started),
        stderr_merged=False,
    )


# --------------------------------------------------------------- PTY 实现


def _spawn_pty(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str],
    policy: ExecPolicy,
    blob_dir: Path | None,
) -> ExecResult:
    import pty  # 仅 POSIX 存在；Windows 走上面的 PolicyUnsupported 分支

    started = time.monotonic()
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(Path(cwd).resolve()),
            env=dict(env),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            shell=False,
            start_new_session=True,
        )
    finally:
        # 父进程必须关掉从端，否则读主端永远等不到 EOF。
        os.close(slave_fd)

    os.set_blocking(master_fd, False)
    collector = _StreamCollector(policy.max_output_bytes, blob_dir, "stdout")
    stop_reading = threading.Event()
    reader = _start_pump(_pump_fd, master_fd, collector, stop_reading)
    timed_out, tree_killed = _wait_with_timeout(proc, policy.timeout_ms)
    _join_pumps([reader], stop_reading)
    _close_fd_quietly(master_fd)

    # PTY 把 stdout/stderr 合成一条流：都写进 `stdout`，`stderr` 为空。
    return _build_result(
        proc,
        collector,
        None,
        timed_out=timed_out,
        tree_killed=tree_killed,
        duration_ms=_elapsed_ms(started),
        stderr_merged=True,
    )


# --------------------------------------------------------------- 等待与杀树


def _wait_with_timeout(proc: subprocess.Popen, timeout_ms: int) -> tuple[bool, bool]:
    """等进程结束；超时就杀树。返回 (timed_out, tree_killed)。"""
    try:
        proc.wait(timeout=timeout_ms / 1000)
        return False, False
    except subprocess.TimeoutExpired:
        tree_killed = _kill_tree(proc)
        try:
            proc.wait(timeout=_JOIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # 连 SIGKILL 之后都等不到（macOS 上极罕见，通常是僵尸/权限问题）：
            # 如实返回，不要卡死调用方。
            return True, tree_killed
        return True, tree_killed


def _kill_tree(proc: subprocess.Popen) -> bool:
    """尽力终止整棵树。返回值 = "是否可以认为整棵树已被杀"。

    POSIX：进程组（start_new_session 让子进程成为新会话/进程组的组长）。
    Windows：零依赖拿不到 Job Object，只能杀直接子进程 → False。
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return True
        except ProcessLookupError:
            return True  # 已经退出了，没有可杀的树
        except OSError:
            pass  # 拿不到进程组就退化为杀直接子进程
    try:
        proc.kill()
    except OSError:
        pass
    return False


# --------------------------------------------------------------- 读流


def _start_pump(target, stream, collector, stop_reading) -> threading.Thread:
    thread = threading.Thread(
        target=target, args=(stream, collector, stop_reading), daemon=True
    )
    thread.start()
    return thread


def _pump_file(
    stream: IO[bytes], collector: _StreamCollector, stop_reading: threading.Event
) -> None:
    try:
        _pump_fd(stream.fileno(), collector, stop_reading)
    except (OSError, ValueError):
        # 对端关闭或读端不可用；已读到的部分仍然保留。
        pass
    finally:
        _close_quietly(stream)


def _pump_fd(
    fd: int, collector: _StreamCollector, stop_reading: threading.Event
) -> None:
    while not stop_reading.is_set():
        try:
            chunk = os.read(fd, READ_CHUNK)
        except BlockingIOError:
            # 没有数据不等于 EOF。短、有停止信号的等待既避免空转，也让孙进程
            # 不关闭继承的写端时，读线程仍能在收尾上限后退出。
            stop_reading.wait(0.01)
            continue
        except OSError:
            # Linux/macOS 上从端关闭后读主端会得到 EIO。
            break
        if not chunk:
            break
        collector.feed(chunk)


def _join_pumps(
    threads: list[threading.Thread], stop_reading: threading.Event
) -> None:
    deadline = time.monotonic() + _JOIN_TIMEOUT_S
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if not any(thread.is_alive() for thread in threads):
        return
    # 只发停止信号；每条读线程自己关闭所属流，主线程不争 BufferedReader 锁。
    stop_reading.set()
    for thread in threads:
        thread.join(timeout=1.0)


def _close_quietly(target) -> None:
    try:
        target.close()
    except (OSError, ValueError):
        pass


def _close_fd_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


# --------------------------------------------------------------- 组装结果


def _build_result(
    proc: subprocess.Popen,
    out: _StreamCollector,
    err: _StreamCollector | None,
    *,
    timed_out: bool,
    tree_killed: bool,
    duration_ms: int,
    stderr_merged: bool,
) -> ExecResult:
    stdout, out_truncated, stdout_ref = out.finish()
    if stderr_merged or err is None:
        stderr, err_truncated, stderr_ref = "", False, None
    else:
        stderr, err_truncated, stderr_ref = err.finish()

    exit_code: int | None
    killed_by: int | None
    returncode = proc.returncode
    if timed_out:
        # 超时是独立结论：即使我们发的 SIGKILL 让 returncode=-9，也不把它
        # 说成"程序自己以 9 号信号退出"。
        exit_code = None
        killed_by = -returncode if returncode is not None and returncode < 0 else None
    elif returncode is not None and returncode < 0:
        exit_code = None
        killed_by = -returncode
    else:
        exit_code = returncode
        killed_by = None

    return ExecResult(
        exit_code=exit_code,
        signal=killed_by,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        truncated=out_truncated or err_truncated,
        stdout_ref=stdout_ref,
        stderr_ref=stderr_ref,
        duration_ms=duration_ms,
        stderr_merged=stderr_merged,
        descendants_may_survive=timed_out and not tree_killed,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


# --------------------------------------------------------------- 有界收集


class _StreamCollector:
    """有界收集一条流。

    前 `limit` 字节留在内存里（返回给调用方），超出的部分**继续读但不保留
    在内存**：一旦溢出就开一个临时文件把完整原文流式写进去（用于内容寻址
    落盘），读线程永远不停止读取——停读会让子进程写满管道而永久阻塞。

    临时文件在 `blob_dir` 里（有就与目标同目录，保证 `os.replace` 原子），
    没有 `blob_dir` 时退化为匿名临时文件，收尾时丢弃。
    """

    def __init__(
        self, limit: int, blob_dir: Path | None, stream_name: str
    ) -> None:
        self._limit = limit
        self._blob_dir = blob_dir
        self._stream_name = stream_name
        self._head = bytearray()
        self._hasher = hashlib.sha256()
        self._total = 0
        self._spill: IO[bytes] | None = None
        self._spill_path: Path | None = None
        self._spill_error: OSError | None = None

    def feed(self, chunk: bytes) -> None:
        self._hasher.update(chunk)
        self._total += len(chunk)

        room = self._limit - len(self._head)
        if room > 0:
            take = min(room, len(chunk))
            self._head.extend(chunk[:take])
            chunk = chunk[take:]

        if chunk and self._spill_error is None:
            try:
                self._ensure_spill()
                assert self._spill is not None
                self._spill.write(chunk)
            except OSError as exc:
                self._spill_error = exc

    def _ensure_spill(self) -> None:
        if self._spill is not None:
            return
        if self._blob_dir is None:
            self._spill = tempfile.TemporaryFile()
        else:
            self._blob_dir.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                prefix=f".{self._stream_name}.",
                suffix=".tmp",
                dir=str(self._blob_dir),
            )
            self._spill_path = Path(name)
            self._spill = os.fdopen(fd, "wb")
        self._spill.write(bytes(self._head))

    def finish(self) -> tuple[str, bool, str | None]:
        """返回 (截断后的文本, 是否截断, 完整原文的 sha256)。"""
        truncated = self._total > self._limit
        text = bytes(self._head).decode("utf-8", errors="replace")
        if not truncated:
            self._discard()
            return text, False, None

        digest = self._hasher.hexdigest()
        if self._blob_dir is None:
            self._discard()
            return text, True, None
        if self._spill_error is not None:
            self._discard()
            raise TianshuError(
                f"{self._stream_name} 超过 max_output_bytes 且无法落盘："
                f"{self._spill_error}"
            )
        self._publish(digest)
        return text, True, digest

    def _publish(self, digest: str) -> None:
        assert self._spill is not None and self._spill_path is not None
        self._spill.flush()
        os.fsync(self._spill.fileno())
        self._spill.close()
        self._spill = None
        target = self._blob_dir / digest  # type: ignore[operator]
        os.replace(self._spill_path, target)
        self._spill_path = None
        _fsync_dir(target.parent)

    def _discard(self) -> None:
        if self._spill is not None:
            _close_quietly(self._spill)
            self._spill = None
        if self._spill_path is not None:
            try:
                self._spill_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._spill_path = None


def _fsync_dir(path: Path) -> None:
    """rename 的持久化需要目录 fsync（与 `fsutil.atomic_write_bytes` 同语义）。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


__all__ = [
    "BOUNDARY_ENFORCED",
    "BOUNDARY_NOT_ENFORCED",
    "BOUNDARY_UNKNOWN",
    "ISOLATION_BOUNDARIES",
    "ISOLATION_BOUNDARY_CREDENTIALS",
    "ISOLATION_BOUNDARY_FILES",
    "ISOLATION_BOUNDARY_NETWORK",
    "ISOLATION_BOUNDARY_PROCESS_TREE",
    "LOCAL_EXEC_BACKEND",
    "RULE_PTY_NOT_ALLOWED",
    "LOCAL_EXEC_CAPABILITIES",
    "ExecResult",
    "LocalExecBackend",
    "local_capabilities",
    "local_isolation_claims",
    "platform_label",
    "spawn",
]
