"""F1 执行原语：真正的 `spawn`（进程创建、有界 stdio、超时杀进程树、退出码/信号）。

**先说清楚：这不是沙箱。** `LocalExecBackend.capabilities.isolation = "none"`：
argv 白名单、cwd 限制、env 白名单都是**策略约束**，不是隔离——子进程照样能读
整个磁盘、能联网、能 fork。真正的隔离（Seatbelt / Landlock / AppContainer /
容器）不在 v1 的交付范围（`docs/TECH-ARCH.md` §6），本模块不假装有。

四条实现约定：

1. **判定先于创建。** 所有调用先走 F2 的 `check_exec()`；策略不允许
   （`PolicyDenied`）或后端做不到（`PolicyUnsupported`）都在 `Popen` 之前抛出，
   绝不"降级后照跑"。
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
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from .errors import TianshuError
from .execpolicy import (
    FEATURE_PTY,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyUnsupported,
    check_exec,
    filtered_env,
)

READ_CHUNK = 64 * 1024
"""每次 read 的字节数。足够大以减少 syscall，又不会让单次内存尖峰太夸张。"""

_JOIN_TIMEOUT_S = 5.0
"""杀进程后等读线程收尾的上限。超过就关读端，宁可少收尾也不永久挂住。"""


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
        isolation="none",
        supports_pty=posix,
        supports_signals=posix,
        supports_process_group=posix,
        supports_job_objects=False,
        supports_network_isolation=False,
    )


#: 当前进程所在平台的本地后端能力（如实声明，见 `local_capabilities()`）。
LOCAL_EXEC_CAPABILITIES = local_capabilities()


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
    ) -> ExecResult:
        return spawn(
            argv,
            cwd=cwd,
            env=env,
            policy=policy,
            capabilities=self.capabilities,
            blob_dir=blob_dir,
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
) -> ExecResult:
    """按策略创建并等待一个进程。不通过就抛，不降级。

    `env` 可以是原始环境：这里会先 `filtered_env()` 过滤，再用过滤后的环境
    过 `check_exec()` 并交给子进程（未列出的变量不会出现在子进程里）。
    """
    if not isinstance(policy, ExecPolicy):
        raise TypeError(f"spawn 需要 ExecPolicy，收到：{type(policy).__name__}")
    if not isinstance(capabilities, ExecBackendCapabilities):
        raise TypeError(
            "spawn 需要 ExecBackendCapabilities，收到："
            f"{type(capabilities).__name__}"
        )

    child_env = filtered_env(policy, env)
    check_exec(policy, capabilities, argv, cwd, child_env)

    if policy.allow_pty:
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

    out = _StreamCollector(policy.max_output_bytes, blob_dir, "stdout")
    err = _StreamCollector(policy.max_output_bytes, blob_dir, "stderr")
    threads = [
        _start_pump(_pump_file, proc.stdout, out),
        _start_pump(_pump_file, proc.stderr, err),
    ]

    timed_out, tree_killed = _wait_with_timeout(proc, policy.timeout_ms)
    _join_pumps(threads, (proc.stdout, proc.stderr))
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

    collector = _StreamCollector(policy.max_output_bytes, blob_dir, "stdout")
    reader = _start_pump(_pump_fd, master_fd, collector)
    timed_out, tree_killed = _wait_with_timeout(proc, policy.timeout_ms)
    _join_pumps([reader], ())
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


def _start_pump(target, stream, collector) -> threading.Thread:
    thread = threading.Thread(
        target=target, args=(stream, collector), daemon=True
    )
    thread.start()
    return thread


def _pump_file(stream: IO[bytes], collector: _StreamCollector) -> None:
    try:
        while True:
            chunk = stream.read(READ_CHUNK)
            if not chunk:
                break
            collector.feed(chunk)
    except (OSError, ValueError):
        # 主线程在收尾时可能主动关掉读端（孙进程还握着管道）；这不是错误，
        # 已经读到的部分照样保留。
        pass
    finally:
        _close_quietly(stream)


def _pump_fd(fd: int, collector: _StreamCollector) -> None:
    while True:
        try:
            chunk = os.read(fd, READ_CHUNK)
        except OSError:
            # Linux/macOS 上从端关闭后读主端会得到 EIO。
            break
        if not chunk:
            break
        collector.feed(chunk)


def _join_pumps(
    threads: list[threading.Thread], streams: tuple[IO[bytes], ...]
) -> None:
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT_S)
    if not any(thread.is_alive() for thread in threads):
        return
    # 有孙进程握着写端（Windows 只杀了直接子进程时最可能）。关读端解阻塞，
    # 读线程会把 ValueError/OSError 吞掉并退出；绝不为了"读干净"永久挂住。
    for stream in streams:
        _close_quietly(stream)
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
    "LOCAL_EXEC_BACKEND",
    "LOCAL_EXEC_CAPABILITIES",
    "ExecResult",
    "LocalExecBackend",
    "local_capabilities",
    "spawn",
]
