"""W14 Linux Landlock LSM 进程级沙箱后端（`LandlockExecBackend`，零第三方依赖）。

通过标准库 `ctypes` 直接调用 Linux `x86_64` / `aarch64` 内核系统调用：
- `prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)`
- `landlock_create_ruleset`（syscall 444）
- `landlock_add_rule`（syscall 445）
- `landlock_restrict_self`（syscall 446）

按真实探测到的内核 Landlock ABI 版本如实分级（绝不虚标，不可用时绝不回落 `local`）：
- **ABI >= 4（Linux >= 6.7）**：启用文件系统白名单 + TCP 绑定/连接全面阻断
  （`handled_access_net = 0x3` 且零端口放行规则），声明 `isolation = "app_container"`、
  `supports_network_isolation = True`。
- **ABI 1..3（Linux 5.13..6.6）**：内核尚无 TCP 规则支持，仅启用文件系统白名单、
  凭据环境清洗与进程组杀树，如实声明 `isolation = "process_tree"`、
  `supports_network_isolation = False`；请求 `network` 边界的任务由 `isolation.py`
  实测门诚实拒绝。
- **ABI < 1（Linux < 5.13 / Landlock 未启用）或非 `x86_64`/`aarch64` 架构**：
  不注册后端，显式请求时抛 `PolicyUnsupported`。
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .exec import (
    RULE_PTY_NOT_ALLOWED,
    ExecResult,
    _spawn_pipes,
    _spawn_pty,
    scrub_sandbox_env,
)
from .execpolicy import (
    ISOLATION_APP_CONTAINER,
    ISOLATION_PROCESS_TREE,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    check_capabilities,
    check_spawn,
    filtered_env,
)

# Linux UAPI 系统调用号（x86_64 与 aarch64 完全一致）
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2

PR_SET_NO_NEW_PRIVS = 38
O_PATH = getattr(os, "O_PATH", 0o10000000)

# Landlock 文件系统访问位（include/uapi/linux/landlock.h）
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13       # ABI 2+
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14    # ABI 3+

# 仅适用于非目录（普通文件 / 字符设备等）的访问位；若对文件传入目录专属位，
# 内核 `landlock_add_rule` 会返回 `EINVAL`。
LANDLOCK_FILE_ACCESS_MASK = (
    LANDLOCK_ACCESS_FS_EXECUTE
    | LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_TRUNCATE
)

LANDLOCK_DIR_ONLY_ACCESS_MASK = (
    LANDLOCK_ACCESS_FS_READ_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_CHAR
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | LANDLOCK_ACCESS_FS_MAKE_SYM
    | LANDLOCK_ACCESS_FS_REFER
)

LANDLOCK_HANDLED_FS_ABI1 = (1 << 13) - 1
LANDLOCK_HANDLED_FS_ABI2 = (1 << 14) - 1
LANDLOCK_HANDLED_FS_ABI3 = (1 << 15) - 1

# Landlock 网络访问位（ABI 4+，Linux >= 6.7）
LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0
LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1
LANDLOCK_HANDLED_NET_ABI4 = (
    LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP
)

SUPPORTED_LINUX_MACHINES: frozenset[str] = frozenset(
    {"x86_64", "amd64", "aarch64", "arm64"}
)

LANDLOCK_FAIL_CLOSED_EXIT_CODE = 126

_SYSTEM_RO_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc",
    "/proc/sys/kernel",
    "/dev/random",
    "/dev/urandom",
)

_OPTIONAL_RW_DEVICES: tuple[str, ...] = (
    "/dev/zero",
    "/dev/full",
)

_PTY_RW_PATHS: tuple[str, ...] = (
    "/dev/ptmx",
    "/dev/tty",
    "/dev/pts",
)


class LandlockRulesetAttr(ctypes.Structure):
    """对应内核 `struct landlock_ruleset_attr`（ABI 1..3 传 8 字节，ABI >= 4 传 16 字节）。"""

    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class LandlockPathBeneathAttr(ctypes.Structure):
    """对应内核 `struct landlock_path_beneath_attr __attribute__((packed))`（精确 12 字节）。"""

    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


if ctypes.sizeof(LandlockPathBeneathAttr) != 12:  # pragma: no cover - 结构守卫
    raise AssertionError(
        "LandlockPathBeneathAttr 必须为紧凑 12 字节，"
        f"实际为 {ctypes.sizeof(LandlockPathBeneathAttr)}"
    )
if ctypes.sizeof(LandlockRulesetAttr) != 16:  # pragma: no cover - 结构守卫
    raise AssertionError(
        "LandlockRulesetAttr 必须为 16 字节，"
        f"实际为 {ctypes.sizeof(LandlockRulesetAttr)}"
    )


@dataclass(frozen=True)
class LandlockRuleSpec:
    """父进程预解析的单条路径规则（避免 `fork()` 后在子进程里做重型路径计算）。"""

    path: str
    writable: bool
    required: bool


def is_supported_linux_arch(
    *,
    platform_name: str | None = None,
    machine_name: str | None = None,
) -> bool:
    plat = sys.platform if platform_name is None else platform_name
    mach = (
        platform.machine() if machine_name is None else machine_name
    ).strip().lower()
    return plat.startswith("linux") and mach in SUPPORTED_LINUX_MACHINES


def ruleset_config_for_abi(abi: int) -> tuple[int, int, int]:
    """根据内核 Landlock ABI 版本返回 `(handled_access_fs, handled_access_net, attr_size)`。"""
    if isinstance(abi, bool) or not isinstance(abi, int) or abi < 1:
        raise ValueError(f"Landlock ABI 版本必须为 >= 1 的整数，收到：{abi!r}")
    if abi == 1:
        handled_fs = LANDLOCK_HANDLED_FS_ABI1
    elif abi == 2:
        handled_fs = LANDLOCK_HANDLED_FS_ABI2
    else:
        handled_fs = LANDLOCK_HANDLED_FS_ABI3

    if abi >= 4:
        return handled_fs, LANDLOCK_HANDLED_NET_ABI4, 16
    return handled_fs, 0, 8


def access_mask_for_mode(
    st_mode: int, *, writable: bool, handled_fs: int
) -> int:
    """按目标 fd 的 `st_mode` 计算合法 Landlock 访问掩码（严格剥离非目录上的目录位）。"""
    if stat.S_ISDIR(st_mode):
        if writable:
            return handled_fs
        return (
            LANDLOCK_ACCESS_FS_EXECUTE
            | LANDLOCK_ACCESS_FS_READ_FILE
            | LANDLOCK_ACCESS_FS_READ_DIR
        ) & handled_fs

    file_allowed = LANDLOCK_FILE_ACCESS_MASK & handled_fs
    if writable:
        return (
            LANDLOCK_ACCESS_FS_EXECUTE
            | LANDLOCK_ACCESS_FS_READ_FILE
            | LANDLOCK_ACCESS_FS_WRITE_FILE
            | LANDLOCK_ACCESS_FS_TRUNCATE
        ) & file_allowed
    return (
        LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE
    ) & file_allowed


def _load_linux_libc() -> ctypes.CDLL | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if not hasattr(libc, "syscall") or not hasattr(libc, "prctl"):
        return None
    return libc


def detect_landlock_abi(
    *,
    syscall_fn: Callable[..., int] | None = None,
    platform_name: str | None = None,
    machine_name: str | None = None,
) -> int:
    """探测当前内核支持的最高 Landlock ABI 版本；不支持或未启用返回 `0`。"""
    if not is_supported_linux_arch(
        platform_name=platform_name, machine_name=machine_name
    ):
        return 0
    if syscall_fn is None:
        libc = _load_linux_libc()
        if libc is None:
            return 0
        libc.syscall.restype = ctypes.c_long
        ret = int(
            libc.syscall(
                ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET),
                ctypes.c_void_p(0),
                ctypes.c_size_t(0),
                ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
            )
        )
    else:
        ret = int(
            syscall_fn(
                SYS_LANDLOCK_CREATE_RULESET,
                0,
                0,
                LANDLOCK_CREATE_RULESET_VERSION,
            )
        )
    return ret if ret >= 1 else 0


def landlock_available() -> bool:
    return detect_landlock_abi() >= 1


def landlock_capabilities(abi: int) -> ExecBackendCapabilities:
    """按内核 Landlock ABI 版本生成诚实能力声明（ABI 1..3 绝不虚标网络隔离）。"""
    if isinstance(abi, bool) or not isinstance(abi, int) or abi < 1:
        raise PolicyUnsupported(
            "当前平台或内核未启用 Linux Landlock LSM（需 Linux x86_64/aarch64 且内核 >= 5.13）；"
            "禁止静默回落到 local",
            backend="landlock",
            feature="exec_backend",
        )
    if abi >= 4:
        return ExecBackendCapabilities(
            name="landlock",
            isolation=ISOLATION_APP_CONTAINER,
            supports_pty=True,
            supports_signals=True,
            supports_process_group=True,
            supports_job_objects=False,
            supports_network_isolation=True,
        )
    return ExecBackendCapabilities(
        name="landlock",
        isolation=ISOLATION_PROCESS_TREE,
        supports_pty=True,
        supports_signals=True,
        supports_process_group=True,
        supports_job_objects=False,
        supports_network_isolation=False,
    )


def _build_landlock_rules(
    policy: ExecPolicy,
    *,
    argv0: str,
    extra_rw_roots: Sequence[Path] = (),
    pty: bool = False,
) -> tuple[LandlockRuleSpec, ...]:
    """在父进程预构建规范化路径规则表；同路径读写冲突时以可写/必需优先合并。"""
    if not policy.cwd_roots:
        raise ValueError("Landlock 规则构建要求 policy.cwd_roots 非空")

    merged: dict[str, LandlockRuleSpec] = {}

    def add_rule(
        raw: str | os.PathLike[str], *, writable: bool, required: bool
    ) -> None:
        text = os.fspath(raw)
        if "\x00" in text:
            raise ValueError(f"Landlock 路径不能含 NUL：{text!r}")
        try:
            resolved = str(Path(text).resolve())
        except OSError:
            if required:
                raise
            return
        existing = merged.get(resolved)
        if existing is None:
            merged[resolved] = LandlockRuleSpec(
                path=resolved, writable=writable, required=required
            )
        else:
            merged[resolved] = LandlockRuleSpec(
                path=resolved,
                writable=existing.writable or writable,
                required=existing.required or required,
            )

    for sys_ro in _SYSTEM_RO_PATHS:
        add_rule(sys_ro, writable=False, required=False)
    add_rule(sys.base_prefix, writable=False, required=False)
    add_rule(sys.prefix, writable=False, required=False)
    add_rule(argv0, writable=False, required=False)

    add_rule("/dev/null", writable=True, required=True)
    for dev_rw in _OPTIONAL_RW_DEVICES:
        add_rule(dev_rw, writable=True, required=False)
    if pty:
        for pty_rw in _PTY_RW_PATHS:
            add_rule(pty_rw, writable=True, required=False)

    for root in policy.cwd_roots:
        add_rule(root, writable=True, required=True)
    for extra in extra_rw_roots:
        add_rule(extra, writable=True, required=True)

    return tuple(merged.values())


class _LinuxSyscallDriver:
    """在父进程预先绑定 `libc.syscall` 与 `libc.prctl`，供 `fork()` 后子进程无锁直调。"""

    def __init__(self, libc: ctypes.CDLL) -> None:
        self._libc = libc
        self._libc.syscall.restype = ctypes.c_long
        self._libc.prctl.restype = ctypes.c_int

    def prctl_no_new_privs(self) -> int:
        return int(
            self._libc.prctl(
                ctypes.c_int(PR_SET_NO_NEW_PRIVS),
                ctypes.c_ulong(1),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
            )
        )

    def create_ruleset(
        self, attr: LandlockRulesetAttr, attr_size: int, flags: int
    ) -> int:
        return int(
            self._libc.syscall(
                ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET),
                ctypes.byref(attr),
                ctypes.c_size_t(attr_size),
                ctypes.c_uint32(flags),
            )
        )

    def add_path_rule(
        self, ruleset_fd: int, path_attr: LandlockPathBeneathAttr, flags: int
    ) -> int:
        return int(
            self._libc.syscall(
                ctypes.c_long(SYS_LANDLOCK_ADD_RULE),
                ctypes.c_int(ruleset_fd),
                ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                ctypes.byref(path_attr),
                ctypes.c_uint32(flags),
            )
        )

    def restrict_self(self, ruleset_fd: int, flags: int) -> int:
        return int(
            self._libc.syscall(
                ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF),
                ctypes.c_int(ruleset_fd),
                ctypes.c_uint32(flags),
            )
        )


def _default_open_path(path: str) -> int:
    return os.open(path, O_PATH | getattr(os, "O_CLOEXEC", 0))


def _default_fstat_mode(fd: int) -> int:
    return os.fstat(fd).st_mode


def _default_close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _apply_landlock_in_child(
    abi: int,
    rules: Sequence[LandlockRuleSpec],
    *,
    prctl_fn: Callable[[], int],
    create_ruleset_fn: Callable[[LandlockRulesetAttr, int, int], int],
    add_rule_fn: Callable[[int, LandlockPathBeneathAttr, int], int],
    restrict_self_fn: Callable[[int, int], int],
    open_path_fn: Callable[[str], int] = _default_open_path,
    fstat_mode_fn: Callable[[int], int] = _default_fstat_mode,
    close_fd_fn: Callable[[int], None] = _default_close_fd,
    exit_fn: Callable[[int], NoReturn] = os._exit,
) -> None:
    """在子进程 `execve` 前强制激活 Landlock；任何一步失败立即 `os._exit(126)` 绝不裸跑。"""
    ruleset_fd = -1
    try:
        handled_fs, handled_net, attr_size = ruleset_config_for_abi(abi)
        if prctl_fn() != 0:
            exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
            return

        ruleset_attr = LandlockRulesetAttr(
            handled_access_fs=handled_fs,
            handled_access_net=handled_net,
        )
        ruleset_fd = create_ruleset_fn(ruleset_attr, attr_size, 0)
        if ruleset_fd < 0:
            exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
            return

        for spec in rules:
            path_fd = -1
            try:
                try:
                    path_fd = open_path_fn(spec.path)
                except OSError as exc:
                    if not spec.required and exc.errno in (
                        errno.ENOENT,
                        errno.ENOTDIR,
                        errno.ENXIO,
                    ):
                        continue
                    exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
                    return
                if path_fd < 0:
                    exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
                    return
                st_mode = fstat_mode_fn(path_fd)
                allowed_access = access_mask_for_mode(
                    st_mode, writable=spec.writable, handled_fs=handled_fs
                )
                if allowed_access <= 0:
                    exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
                    return
                path_attr = LandlockPathBeneathAttr(
                    allowed_access=allowed_access,
                    parent_fd=path_fd,
                )
                if add_rule_fn(ruleset_fd, path_attr, 0) != 0:
                    exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
                    return
            finally:
                if path_fd >= 0:
                    close_fd_fn(path_fd)

        if restrict_self_fn(ruleset_fd, 0) != 0:
            exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
            return
    except BaseException:
        exit_fn(LANDLOCK_FAIL_CLOSED_EXIT_CODE)
        return
    finally:
        if ruleset_fd >= 0:
            close_fd_fn(ruleset_fd)


class LandlockExecBackend:
    """Linux `x86_64` / `aarch64` Landlock LSM 进程级沙箱后端。"""

    def __init__(self, *, abi: int | None = None) -> None:
        resolved_abi = detect_landlock_abi() if abi is None else abi
        self._abi = resolved_abi
        self.capabilities = landlock_capabilities(resolved_abi)

    @property
    def abi(self) -> int:
        return self._abi

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
        if not isinstance(policy, ExecPolicy):
            raise TypeError(
                f"spawn 需要 ExecPolicy，收到：{type(policy).__name__}"
            )
        child_env = filtered_env(policy, env)
        check_spawn(policy, argv, cwd, child_env)

        if pty:
            if not policy.allow_pty:
                raise PolicyDenied(
                    "本次调用请求了 PTY，但策略没有允许"
                    "（ExecPolicy.allow_pty=False）：改策略，或本次改用管道",
                    rule_id=RULE_PTY_NOT_ALLOWED,
                )
            check_capabilities(policy, self.capabilities)

        libc = _load_linux_libc()
        if not is_supported_linux_arch() or libc is None or self._abi < 1:
            raise PolicyUnsupported(
                "当前平台或内核不可用 Linux Landlock 沙箱"
                "（仅支持 Linux x86_64/aarch64 且内核 >= 5.13）；"
                "禁止静默回落到 local",
                backend=self.capabilities.name,
                feature="exec_backend",
            )

        driver = _LinuxSyscallDriver(libc)
        sandbox_env = scrub_sandbox_env(child_env)
        priv_tmp = Path(tempfile.mkdtemp(prefix="tianshu-ll-tmp-")).resolve()
        try:
            sandbox_env["TMPDIR"] = str(priv_tmp)
            rules = _build_landlock_rules(
                policy,
                argv0=argv[0],
                extra_rw_roots=(priv_tmp,),
                pty=pty,
            )
            abi = self._abi

            def _preexec() -> None:
                _apply_landlock_in_child(
                    abi,
                    rules,
                    prctl_fn=driver.prctl_no_new_privs,
                    create_ruleset_fn=driver.create_ruleset,
                    add_rule_fn=driver.add_path_rule,
                    restrict_self_fn=driver.restrict_self,
                )

            if pty:
                return _spawn_pty(
                    argv,
                    cwd=cwd,
                    env=sandbox_env,
                    policy=policy,
                    blob_dir=blob_dir,
                    preexec_fn=_preexec,
                )
            return _spawn_pipes(
                argv,
                cwd=cwd,
                env=sandbox_env,
                policy=policy,
                blob_dir=blob_dir,
                preexec_fn=_preexec,
            )
        finally:
            shutil.rmtree(priv_tmp, ignore_errors=True)


def landlock_exec_backend() -> LandlockExecBackend:
    """构造当前 Linux 内核对应的 `LandlockExecBackend`（不支持时抛 `PolicyUnsupported`）。"""
    return LandlockExecBackend()


__all__ = [
    "LANDLOCK_ACCESS_FS_EXECUTE",
    "LANDLOCK_ACCESS_FS_MAKE_BLOCK",
    "LANDLOCK_ACCESS_FS_MAKE_CHAR",
    "LANDLOCK_ACCESS_FS_MAKE_DIR",
    "LANDLOCK_ACCESS_FS_MAKE_FIFO",
    "LANDLOCK_ACCESS_FS_MAKE_REG",
    "LANDLOCK_ACCESS_FS_MAKE_SOCK",
    "LANDLOCK_ACCESS_FS_MAKE_SYM",
    "LANDLOCK_ACCESS_FS_READ_DIR",
    "LANDLOCK_ACCESS_FS_READ_FILE",
    "LANDLOCK_ACCESS_FS_REFER",
    "LANDLOCK_ACCESS_FS_REMOVE_DIR",
    "LANDLOCK_ACCESS_FS_REMOVE_FILE",
    "LANDLOCK_ACCESS_FS_TRUNCATE",
    "LANDLOCK_ACCESS_FS_WRITE_FILE",
    "LANDLOCK_ACCESS_NET_BIND_TCP",
    "LANDLOCK_ACCESS_NET_CONNECT_TCP",
    "LANDLOCK_CREATE_RULESET_VERSION",
    "LANDLOCK_DIR_ONLY_ACCESS_MASK",
    "LANDLOCK_FAIL_CLOSED_EXIT_CODE",
    "LANDLOCK_FILE_ACCESS_MASK",
    "LANDLOCK_HANDLED_FS_ABI1",
    "LANDLOCK_HANDLED_FS_ABI2",
    "LANDLOCK_HANDLED_FS_ABI3",
    "LANDLOCK_HANDLED_NET_ABI4",
    "LANDLOCK_RULE_NET_PORT",
    "LANDLOCK_RULE_PATH_BENEATH",
    "LandlockExecBackend",
    "LandlockPathBeneathAttr",
    "LandlockRuleSpec",
    "LandlockRulesetAttr",
    "O_PATH",
    "PR_SET_NO_NEW_PRIVS",
    "SUPPORTED_LINUX_MACHINES",
    "SYS_LANDLOCK_ADD_RULE",
    "SYS_LANDLOCK_CREATE_RULESET",
    "SYS_LANDLOCK_RESTRICT_SELF",
    "_apply_landlock_in_child",
    "_build_landlock_rules",
    "access_mask_for_mode",
    "detect_landlock_abi",
    "is_supported_linux_arch",
    "landlock_available",
    "landlock_capabilities",
    "landlock_exec_backend",
    "ruleset_config_for_abi",
]
