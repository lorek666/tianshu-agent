"""W14 macOS Seatbelt（SBPL）进程级沙箱后端（`SeatbeltExecBackend`）。

基于 Darwin `/usr/bin/sandbox-exec` 动态生成严格默认拒绝（`(deny default)`）的
SBPL 规则集，在不引入任何第三方运行时依赖的前提下同时收紧四条隔离边界：

1. `files_authority`：默认禁止读写，仅对系统只读运行库、当前 Python 运行时前缀做
   只读映射放行，仅对规范化后的 `policy.cwd_roots` 与每进程私有临时目录放行读写；
   工作区外目录、`data_root`（`events.db*`、`artifacts/`、`blobs/`、`run/ipc.key`）、
   `/tmp`、`/private/var/folders` 兄弟目录、`~/.tianshu` 等全部阻断。
2. `network`：默认全拒叠加显式 `(deny network*)`，阻断全部出站/入站 TCP/UDP/UNIX
   套接字（含 `127.0.0.1` loopback）。
3. `credentials`：`spawn()` 内部独立于 `ExecPolicy.env_allowlist` 强制执行
   `scrub_sandbox_env()` 剥离敏感环境变量（含隔离探针哨兵与各类 API Key/Token），
   并在 SBPL 末尾显式封锁 `~/Library/Keychains`、`~/.ssh`、`~/.aws` 与
   `com.apple.securityd` 等系统凭据通道。
4. `process_tree`：`sandbox-exec` 原地 `execvp` 目标程序，沿用 POSIX 新会话进程组
   （`start_new_session=True` + `os.killpg(SIGKILL)`）清理同组进程。这不能约束
   后代 `setsid()` 脱组；自动执行须通过 `execution_admission` 的独立负控，
   不把进程组消失当作整个进程树退出。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .exec import (
    RULE_PTY_NOT_ALLOWED,
    ExecResult,
    _spawn_pipes,
    _spawn_pty,
    scrub_sandbox_env,
)
from .execpolicy import (
    ISOLATION_APP_CONTAINER,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    check_capabilities,
    check_spawn,
    filtered_env,
)

SANDBOX_EXEC_PATH = "/usr/bin/sandbox-exec"

RULE_SBPL_INVALID_PATH = "f2.sbpl_invalid_path"

_SYSTEM_EXEC_SUBPATHS: tuple[str, ...] = (
    "/System",
    "/System/Volumes/Preboot/Cryptexes",
    "/usr/lib",
    "/usr/share",
    "/bin",
    "/usr/bin",
    "/sbin",
    "/usr/sbin",
    "/Library/Apple",
    "/Library/Frameworks",
)

_OPTIONAL_DEVTOOLS_SUBPATHS: tuple[str, ...] = (
    "/Library/Developer/CommandLineTools",
    "/Applications/Xcode.app/Contents/Developer",
)


def seatbelt_available() -> bool:
    """检查当前平台是否为具备可执行 `/usr/bin/sandbox-exec` 的 macOS Darwin。"""
    return (
        sys.platform == "darwin"
        and os.path.isfile(SANDBOX_EXEC_PATH)
        and os.access(SANDBOX_EXEC_PATH, os.X_OK)
    )


def seatbelt_capabilities() -> ExecBackendCapabilities:
    return ExecBackendCapabilities(
        name="seatbelt",
        isolation=ISOLATION_APP_CONTAINER,
        supports_pty=True,
        supports_signals=True,
        supports_process_group=True,
        supports_job_objects=False,
        supports_network_isolation=True,
    )


SEATBELT_CAPABILITIES = seatbelt_capabilities()


def _reject_control_chars(text: str) -> None:
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError(f"SBPL 路径不能包含控制字符（NUL/LF/CR）：{text!r}")


def _escape_sbpl_path(
    raw: str | os.PathLike[str], *, resolve: bool = True
) -> str:
    """规范化路径并转义 SBPL 字符串字面量，拒绝任何控制字符防规则注入。"""
    raw_text = os.fspath(raw)
    if not isinstance(raw_text, str) or not raw_text:
        raise ValueError(f"SBPL 路径必须是非空字符串：{raw!r}")
    _reject_control_chars(raw_text)
    normalized = (
        str(Path(raw_text).resolve())
        if resolve
        else os.path.abspath(raw_text)
    )
    _reject_control_chars(normalized)
    return normalized.replace("\\", "\\\\").replace('"', '\\"')


def _dedup_keep_order(items: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _sensitive_deny_subpaths() -> tuple[str, ...]:
    """需要在 SBPL 末尾显式封锁的敏感凭据与权威目录（覆盖可能过宽的工作区根）。"""
    candidates: list[Path] = [Path("/Library/Keychains")]
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    if home is not None:
        candidates.extend(
            (
                home / "Library" / "Keychains",
                home / ".ssh",
                home / ".aws",
                home / ".gnupg",
                home / ".local" / "share" / "keyrings",
                home / ".tianshu",
                home / "Library" / "Application Support" / "AgentOS",
            )
        )
    escaped: list[str] = []
    for candidate in candidates:
        escaped.append(_escape_sbpl_path(candidate, resolve=True))
    return _dedup_keep_order(escaped)


def _build_sbpl_profile(
    policy: ExecPolicy,
    *,
    argv0: str,
    extra_rw_roots: Sequence[Path] = (),
    pty: bool = False,
) -> str:
    """为单次受控执行生成动态 Darwin SBPL（version 1）策略文本。"""
    if not isinstance(policy, ExecPolicy):
        raise TypeError(
            f"_build_sbpl_profile 需要 ExecPolicy，收到：{type(policy).__name__}"
        )
    if not policy.cwd_roots:
        raise ValueError("SBPL 生成要求 policy.cwd_roots 非空")
    _reject_control_chars(argv0)

    ro_exec_subpaths: list[str] = [
        _escape_sbpl_path(path, resolve=False) for path in _SYSTEM_EXEC_SUBPATHS
    ]
    for opt_path in _OPTIONAL_DEVTOOLS_SUBPATHS:
        if Path(opt_path).exists():
            ro_exec_subpaths.append(_escape_sbpl_path(opt_path, resolve=True))

    ro_exec_subpaths.append(_escape_sbpl_path(sys.base_prefix, resolve=True))
    ro_exec_subpaths.append(_escape_sbpl_path(sys.prefix, resolve=True))
    ro_subpath_lines = "\n".join(
        f'    (subpath "{item}")'
        for item in _dedup_keep_order(ro_exec_subpaths)
    )

    ro_exec_literals: list[str] = []
    argv0_path = Path(argv0)
    if os.path.islink(argv0):
        ro_exec_literals.append(_escape_sbpl_path(argv0, resolve=False))
    try:
        resolved_argv0 = argv0_path.resolve()
        argv0_is_file = resolved_argv0.is_file()
    except OSError:
        resolved_argv0 = None
        argv0_is_file = False
    if resolved_argv0 is not None and argv0_is_file:
        ro_exec_literals.append(_escape_sbpl_path(resolved_argv0, resolve=True))
    ro_literal_lines = "\n".join(
        f'    (literal "{item}")'
        for item in _dedup_keep_order(ro_exec_literals)
    )

    rw_roots: list[str] = [
        _escape_sbpl_path(root, resolve=True) for root in policy.cwd_roots
    ]
    for extra in extra_rw_roots:
        rw_roots.append(_escape_sbpl_path(extra, resolve=True))
    rw_subpath_lines = "\n".join(
        f'    (subpath "{item}")' for item in _dedup_keep_order(rw_roots)
    )

    deny_subpath_lines = "\n".join(
        f'    (subpath "{item}")' for item in _sensitive_deny_subpaths()
    )

    sections: list[str] = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow process-info-pidinfo (target same-sandbox))",
        "(allow file-read-metadata)",
        "(allow file-read* file-map-executable",
        ro_subpath_lines,
    ]
    if ro_literal_lines:
        sections.append(ro_literal_lines)
    sections.append(")")
    sections.extend(
        [
            "(allow file-read*",
            '    (literal "/")',
            '    (subpath "/private/etc")',
            '    (subpath "/private/var/db/timezone")',
            '    (literal "/dev/null")',
            '    (literal "/dev/random")',
            '    (literal "/dev/urandom")',
            '    (literal "/dev/zero")',
            '    (literal "/dev/autofs_nowait")',
            '    (subpath "/dev/fd")',
            ")",
            "(allow file-read* file-write* file-map-executable",
            rw_subpath_lines,
            ")",
            "(allow file-write*",
            '    (literal "/dev/null")',
            '    (literal "/dev/zero")',
            '    (subpath "/dev/fd")',
            ")",
        ]
    )
    if pty:
        sections.extend(
            [
                "(allow pseudo-tty)",
                "(allow file-read* file-write* file-ioctl",
                '    (literal "/dev/ptmx")',
                '    (literal "/dev/tty")',
                '    (regex #"^/dev/ttys[0-9]+$")',
                ")",
            ]
        )
    sections.extend(
        [
            "(deny network*)",
            "(deny mach-lookup",
            '    (global-name "com.apple.securityd")',
            '    (global-name "com.apple.securityd.xpc")',
            '    (global-name "com.apple.SecurityServer")',
            ")",
            "(deny file-read* file-write*",
            deny_subpath_lines,
            ")",
        ]
    )
    return "\n".join(sections) + "\n"


@dataclass(frozen=True)
class SeatbeltExecBackend:
    """macOS Darwin 进程级沙箱后端（`/usr/bin/sandbox-exec` + SBPL）。"""

    capabilities: ExecBackendCapabilities = field(
        default_factory=seatbelt_capabilities
    )

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

        if not seatbelt_available():
            raise PolicyUnsupported(
                "当前平台不可用 macOS Seatbelt 沙箱"
                "（仅支持 Darwin 且要求存在可执行的 /usr/bin/sandbox-exec）；"
                "禁止静默回落到 local",
                backend=self.capabilities.name,
                feature="exec_backend",
            )

        sandbox_env = scrub_sandbox_env(child_env)
        priv_tmp = Path(tempfile.mkdtemp(prefix="tianshu-sb-tmp-")).resolve()
        try:
            sandbox_env["TMPDIR"] = str(priv_tmp)
            try:
                profile = _build_sbpl_profile(
                    policy,
                    argv0=argv[0],
                    extra_rw_roots=(priv_tmp,),
                    pty=pty,
                )
            except ValueError as exc:
                raise PolicyDenied(
                    f"无法构造安全的 Seatbelt 路径规则：{exc}",
                    rule_id=RULE_SBPL_INVALID_PATH,
                ) from exc

            wrapped_argv = [SANDBOX_EXEC_PATH, "-p", profile, *argv]
            if pty:
                return _spawn_pty(
                    wrapped_argv,
                    cwd=cwd,
                    env=sandbox_env,
                    policy=policy,
                    blob_dir=blob_dir,
                )
            return _spawn_pipes(
                wrapped_argv,
                cwd=cwd,
                env=sandbox_env,
                policy=policy,
                blob_dir=blob_dir,
            )
        finally:
            shutil.rmtree(priv_tmp, ignore_errors=True)


SEATBELT_EXEC_BACKEND = SeatbeltExecBackend()

__all__ = [
    "RULE_SBPL_INVALID_PATH",
    "SANDBOX_EXEC_PATH",
    "SEATBELT_CAPABILITIES",
    "SEATBELT_EXEC_BACKEND",
    "SeatbeltExecBackend",
    "_build_sbpl_profile",
    "_escape_sbpl_path",
    "seatbelt_available",
    "seatbelt_capabilities",
]
