"""W14 双系统进程级沙箱验收测试（macOS `SeatbeltExecBackend` + Linux `LandlockExecBackend`）。

覆盖范围：
1. `isolation.PROBE_VERSION == "v2"` 与 `_probe_process_tree` 工作区内临时目录自清理；
2. 注册表路由：`DEFAULT_EXEC_BACKEND == "local"`、`TIANSHU_EXEC_BACKEND=auto` /
   `preferred_exec_backend()` 及跨平台不可用时零静默回落（`PolicyUnsupported` /
   `c4.exec_unsupported`）；
3. 后端级凭据环境清洗（`scrub_sandbox_env`）与 SBPL 路径规范化/防控制字符注入；
4. macOS Darwin `SeatbeltExecBackend` 真实子进程实测（四边界全 `enforced`、工作区读写、
   `data_root` 越界阻断、loopback TCP 阻断、敏感 env 强制剥离、超时进程组杀树、PTY、
   审批前 `ApprovalView.isolation` 实测呈现与 `LyingBackend` 自相矛盾拦截）；
5. Linux `LandlockExecBackend` 全平台结构体打包/ABI 分级/文件与目录位掩码/preexec
   fail-closed（`exit 126`）单测 + Linux 内核原生四边界实测。
"""

from __future__ import annotations

import ctypes
import errno
import os
import socket
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import effects_admission

from tianshu import exec_backends, tools
from tianshu.control import ControlPlane
from tianshu.dispatcher import Dispatcher, build_request
from tianshu.events import EventLog
from tianshu.exec import (
    BOUNDARY_ENFORCED,
    ISOLATION_BOUNDARIES,
    LOCAL_EXEC_BACKEND,
    LOCAL_EXEC_CAPABILITIES,
    LocalExecBackend,
    _spawn_pipes,
    scrub_sandbox_env,
    spawn,
)
from tianshu.exec_backends import (
    AUTO_EXEC_BACKEND,
    DEFAULT_EXEC_BACKEND,
    EXEC_BACKEND_ENV,
    UNIMPLEMENTED_EXEC_BACKENDS,
    exec_backend,
    preferred_exec_backend,
    register_exec_backend,
    reset_exec_backends,
)
from tianshu.exec_landlock import (
    LANDLOCK_ACCESS_FS_EXECUTE,
    LANDLOCK_ACCESS_FS_MAKE_REG,
    LANDLOCK_ACCESS_FS_READ_DIR,
    LANDLOCK_ACCESS_FS_READ_FILE,
    LANDLOCK_ACCESS_FS_REFER,
    LANDLOCK_ACCESS_FS_TRUNCATE,
    LANDLOCK_ACCESS_FS_WRITE_FILE,
    LANDLOCK_DIR_ONLY_ACCESS_MASK,
    LANDLOCK_FAIL_CLOSED_EXIT_CODE,
    LANDLOCK_HANDLED_FS_ABI1,
    LANDLOCK_HANDLED_FS_ABI2,
    LANDLOCK_HANDLED_FS_ABI3,
    LANDLOCK_HANDLED_NET_ABI4,
    LandlockExecBackend,
    LandlockPathBeneathAttr,
    LandlockRulesetAttr,
    LandlockRuleSpec,
    _apply_landlock_in_child,
    access_mask_for_mode,
    detect_landlock_abi,
    landlock_available,
    landlock_capabilities,
    landlock_exec_backend,
    ruleset_config_for_abi,
)
from tianshu.exec_seatbelt import (
    RULE_SBPL_INVALID_PATH,
    SEATBELT_EXEC_BACKEND,
    SeatbeltExecBackend,
    _build_sbpl_profile,
    _escape_sbpl_path,
    seatbelt_available,
)
from tianshu.execpolicy import (
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
)
from tianshu.isolation import (
    PROBE_GRACE_MS,
    PROBE_VERSION,
    IsolationRefused,
    _wait_pid_gone,
    enabled_capabilities,
    isolation_view,
    measured_level,
    probe_backend,
    require_isolation_requirements,
    reset_probe_cache,
)
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.loop import Limits
from tianshu.model import ScriptedModel
from tianshu.permissions import ApprovalStore, GrantStore, HumanIdentity, PolicyEngine
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.workspace import Workspace

PY = sys.executable


@pytest.fixture(autouse=True)
def _clean_registry_and_cache(monkeypatch):
    reset_exec_backends()
    reset_probe_cache()
    monkeypatch.delenv(EXEC_BACKEND_ENV, raising=False)
    yield
    reset_exec_backends()
    reset_probe_cache()


def _policy(ws: Path, **overrides) -> ExecPolicy:
    base = {
        "argv_allowlist": (PY,),
        "cwd_roots": (ws,),
        "env_allowlist": ("PATH",),
        "max_output_bytes": 64 * 1024,
        "timeout_ms": 10_000,
    }
    base.update(overrides)
    return ExecPolicy(**base)


@dataclass
class _DispatchEnv:
    ws: Path
    data: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    @property
    def data_root(self) -> Path:
        return self.data

    def new_run(self, *, exec_policy: ExecPolicy):
        task = self.tasks.create_task(
            "执行沙箱命令", [{"root_path": str(self.ws)}], exec_policy=exec_policy
        )
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([self.ws], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim


def _make_dispatch_env(tmp_path: Path, *, backend) -> _DispatchEnv:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    log = EventLog(data / "events.db")
    try:
        leases = LeaseStore(log)
        claims = JobStore(log)
        snapshots = SnapshotStore(data / "artifacts")
        tasks = TaskStore(
            log,
            leases=leases,
            claims=claims,
            data_root=data,
            disk_admission=effects_admission(
                log,
                snapshots=snapshots,
                memory=None,
                contents=None,
                data_root=data,
            ),
        )
        approvals = ApprovalStore(log)
        grants = GrantStore(log)
        dispatcher = Dispatcher(
            log,
            tasks,
            leases,
            PolicyEngine(),
            approvals,
            grants=grants,
            claims=claims,
            snapshots=snapshots,
            data_root=data,
            exec_backend=backend,
        )
    except Exception:
        log.close()
        raise
    return _DispatchEnv(
        ws=ws,
        data=data,
        log=log,
        leases=leases,
        claims=claims,
        tasks=tasks,
        approvals=approvals,
        grants=grants,
        snapshots=snapshots,
        dispatcher=dispatcher,
    )


# ---------------------------------------------------------------- 1. PROBE_VERSION v2 与注册表路由


def test_probe_version_is_v2_and_process_tree_cleans_workspace_tempdir(tmp_path):
    assert PROBE_VERSION == "v2"
    ws = tmp_path / "ws"
    dr = tmp_path / "data"
    report = probe_backend(LOCAL_EXEC_BACKEND, workspace=ws, data_root=dr)
    assert report.probe_version == "v2"
    # 探针收尾后 workspace 内不得残留 `.tianshu-tree-probe-*` 临时目录
    assert list(ws.iterdir()) == []


def test_default_backend_stays_local_while_auto_resolves_or_fails_closed(monkeypatch):
    assert DEFAULT_EXEC_BACKEND == "local"
    assert exec_backend() is LOCAL_EXEC_BACKEND

    if seatbelt_available():
        assert preferred_exec_backend() == "seatbelt"
        monkeypatch.setenv(EXEC_BACKEND_ENV, AUTO_EXEC_BACKEND)
        assert exec_backend() is SEATBELT_EXEC_BACKEND
    elif landlock_available():
        assert preferred_exec_backend() == "landlock"
        monkeypatch.setenv(EXEC_BACKEND_ENV, AUTO_EXEC_BACKEND)
        assert exec_backend().capabilities.name == "landlock"
    else:
        assert preferred_exec_backend() == "local"
        monkeypatch.setenv(EXEC_BACKEND_ENV, AUTO_EXEC_BACKEND)
        with pytest.raises(PolicyUnsupported) as excinfo:
            exec_backend()
        assert excinfo.value.backend == "auto"


def test_auto_on_platform_without_native_sandbox_never_falls_back_to_local(monkeypatch):
    """负控：Windows 或无原生沙箱内核配置 `TIANSHU_EXEC_BACKEND=auto` 必须抛错，零回落 local。"""
    monkeypatch.setattr(exec_backends, "seatbelt_available", lambda: False)
    monkeypatch.setattr(exec_backends, "landlock_available", lambda: False)
    monkeypatch.setattr(exec_backends.sys, "platform", "win32")
    reset_exec_backends()

    assert preferred_exec_backend() == "local"
    assert "seatbelt" in UNIMPLEMENTED_EXEC_BACKENDS
    assert "landlock" in UNIMPLEMENTED_EXEC_BACKENDS

    for configured in ("auto", "seatbelt", "landlock"):
        monkeypatch.setenv(EXEC_BACKEND_ENV, configured)
        with pytest.raises(PolicyUnsupported) as excinfo:
            exec_backend()
        assert excinfo.value.backend == configured
        assert excinfo.value.feature == "exec_backend"


def test_unsupported_sandbox_backends_reject_with_zero_local_fallback(
    monkeypatch, tmp_path
):
    """跨平台负控：在非目标系统请求 `seatbelt`/`landlock` 必拒（`c4.exec_unsupported`），零回落。"""
    local_calls: list[int] = []
    real_local_spawn = LocalExecBackend.spawn

    def spy_local_spawn(self, *args, **kwargs):
        local_calls.append(1)
        return real_local_spawn(self, *args, **kwargs)

    monkeypatch.setattr(LocalExecBackend, "spawn", spy_local_spawn)

    # 1. 当前平台原本不支持的后端直接请求必抛 PolicyUnsupported
    if not seatbelt_available():
        with pytest.raises(PolicyUnsupported) as sb_exc:
            exec_backend("seatbelt")
        assert sb_exc.value.backend == "seatbelt"
    if not landlock_available():
        with pytest.raises(PolicyUnsupported) as ll_exc:
            exec_backend("landlock")
        assert ll_exc.value.backend == "landlock"

    # 2. 即使绕过注册表直接调用后端对象，非目标平台也在 spawn 前抛 PolicyUnsupported，
    #    且经 tools._run_command 映射为 rejected + c4.exec_unsupported，local_calls == 0
    ws = tmp_path / "ws"
    ws.mkdir()
    pol = _policy(ws)

    monkeypatch.setattr("tianshu.exec_seatbelt.seatbelt_available", lambda: False)
    sb_ctx = tools.ToolContext(exec_policy=pol, exec_backend=SeatbeltExecBackend())
    sb_res = tools._run_command(Workspace(ws), {"argv": [PY, "-c", "print(1)"]}, sb_ctx)
    assert sb_res.status == "rejected"
    assert sb_res.output["rule_id"] == "c4.exec_unsupported"

    monkeypatch.setattr(
        "tianshu.exec_landlock.is_supported_linux_arch", lambda **_kw: False
    )
    ll_ctx = tools.ToolContext(
        exec_policy=pol, exec_backend=LandlockExecBackend(abi=4)
    )
    ll_res = tools._run_command(Workspace(ws), {"argv": [PY, "-c", "print(1)"]}, ll_ctx)
    assert ll_res.status == "rejected"
    assert ll_res.output["rule_id"] == "c4.exec_unsupported"
    assert local_calls == []


# ---------------------------------------------------------------- 2. 凭据清洗与 SBPL 构造单测


def test_scrub_sandbox_env_strips_all_secret_shapes_and_keeps_benign_vars():
    raw = {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "CUSTOM_FLAG": "enabled",
        "TIANSHU_ISOLATION_SENTINEL": "probe-secret",
        "TIANSHU_API_KEY": "sk-tianshu",
        "TIANSHU_DATA_ROOT": "/tmp/secret-root",
        "OPENAI_API_KEY": "sk-openai",
        "OPENAI_ORG_ID": "org-123",
        "ANTHROPIC_API_KEY": "sk-ant",
        "AWS_ACCESS_KEY_ID": "AKIA123",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "GITHUB_TOKEN": "ghp_xxx",
        "MY_SERVICE_TOKEN": "tok-123",
        "APP_SECRET_VALUE": "sec-456",
        "DB_PASSWORD": "pw",
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        "GPG_AGENT_INFO": "/tmp/gpg",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "SECURITYSESSIONID": "186a5",
    }
    cleaned = scrub_sandbox_env(raw)
    assert cleaned == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "CUSTOM_FLAG": "enabled",
    }


def test_sbpl_profile_builder_escapes_quotes_rejects_control_chars_and_blocks_broad_roots(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    if os.name == "nt":
        # Windows NTFS 文件名禁含双引号 `"`，用空格括号合法目录并直接校验引号转义函数
        ws = tmp_path / "ws_space (paren)"
    else:
        ws = tmp_path / 'ws_"quoted"'
    ws.mkdir()
    pol = _policy(ws)

    assert _escape_sbpl_path('/tmp/ws_"quoted"\\sub').endswith(r'ws_\"quoted\"\\sub')

    profile = _build_sbpl_profile(pol, argv0=PY, pty=True)
    assert "(version 1)" in profile
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert '(global-name "com.apple.securityd")' in profile
    assert "(allow pseudo-tty)" in profile
    if os.name == "nt":
        assert "ws_space (paren)" in profile
    else:
        assert r'ws_\"quoted\"' in profile

    # 绝不允许宽泛放行 /Users、/Volumes、/tmp、/private/tmp 或 /private/var/folders
    for forbidden in (
        '(subpath "/Users")',
        '(subpath "/Volumes")',
        '(subpath "/tmp")',
        '(subpath "/private/tmp")',
        '(subpath "/private/var/folders")',
    ):
        assert forbidden not in profile

    # 控制字符（NUL / LF / CR）必须被拒绝，防 SBPL 规则注入
    for bad in ("/tmp/bad\n(allow default)", "/tmp/bad\rpath", "/tmp/bad\x00path"):
        with pytest.raises(ValueError, match="控制字符"):
            _escape_sbpl_path(bad)

    bad_argv = "/tmp/evil\n(allow default)"
    bad_policy = ExecPolicy(
        argv_allowlist=(bad_argv,),
        cwd_roots=(ws,),
        env_allowlist=("PATH",),
        max_output_bytes=4096,
        timeout_ms=1000,
    )
    if seatbelt_available():
        with pytest.raises(PolicyDenied) as denied:
            SEATBELT_EXEC_BACKEND.spawn(
                [bad_argv], cwd=ws, env={"PATH": "/usr/bin"}, policy=bad_policy
            )
        assert denied.value.rule_id == RULE_SBPL_INVALID_PATH


# ---------------------------------------------------------------- 3. Linux Landlock 全平台单测


def test_landlock_struct_packing_and_abi_rules():
    assert ctypes.sizeof(LandlockPathBeneathAttr) == 12
    assert ctypes.sizeof(LandlockRulesetAttr) == 16

    pb = LandlockPathBeneathAttr(allowed_access=0x0102030405060708, parent_fd=42)
    raw = bytes(pb)
    assert len(raw) == 12
    assert int.from_bytes(raw[:8], sys.byteorder) == 0x0102030405060708
    assert int.from_bytes(raw[8:12], sys.byteorder, signed=True) == 42

    # ABI 0 / 非法 ABI 必须抛错
    with pytest.raises(ValueError):
        ruleset_config_for_abi(0)
    with pytest.raises(PolicyUnsupported):
        landlock_capabilities(0)
    with pytest.raises(PolicyUnsupported):
        LandlockExecBackend(abi=0)

    # ABI 1..3：size=8，handled_net=0，如实声明 process_tree 且可注册进 landlock 槽位
    assert ruleset_config_for_abi(1) == (LANDLOCK_HANDLED_FS_ABI1, 0, 8)
    assert ruleset_config_for_abi(2) == (LANDLOCK_HANDLED_FS_ABI2, 0, 8)
    assert ruleset_config_for_abi(3) == (LANDLOCK_HANDLED_FS_ABI3, 0, 8)
    for abi in (1, 2, 3):
        caps = landlock_capabilities(abi)
        assert caps.name == "landlock"
        assert caps.isolation == "process_tree"
        assert caps.supports_network_isolation is False
    abi2_backend = LandlockExecBackend(abi=2)
    register_exec_backend(abi2_backend, name="landlock", replace=True)
    assert exec_backend("landlock") is abi2_backend

    # ABI 4+：size=16，handled_net=0x3，声明 app_container + supports_network_isolation
    for abi in (4, 5, 6):
        assert ruleset_config_for_abi(abi) == (
            LANDLOCK_HANDLED_FS_ABI3,
            LANDLOCK_HANDLED_NET_ABI4,
            16,
        )
        caps = landlock_capabilities(abi)
        assert caps.name == "landlock"
        assert caps.isolation == "app_container"
        assert caps.supports_network_isolation is True


def test_landlock_detect_abi_handles_arch_and_enosys():
    assert detect_landlock_abi(platform_name="darwin", machine_name="arm64") == 0
    assert detect_landlock_abi(platform_name="win32", machine_name="amd64") == 0
    assert detect_landlock_abi(platform_name="linux", machine_name="riscv64") == 0
    assert (
        detect_landlock_abi(
            platform_name="linux",
            machine_name="x86_64",
            syscall_fn=lambda *_a: -1,
        )
        == 0
    )
    assert (
        detect_landlock_abi(
            platform_name="linux",
            machine_name="aarch64",
            syscall_fn=lambda *_a: 4,
        )
        == 4
    )


def test_landlock_file_vs_directory_access_masking_prevents_einval():
    handled_fs = LANDLOCK_HANDLED_FS_ABI3

    # 普通文件与字符设备（如 /dev/null）绝不能带任何目录专属位（否则内核返 EINVAL）
    for mode in (stat.S_IFREG | 0o755, stat.S_IFCHR | 0o666):
        ro_file = access_mask_for_mode(mode, writable=False, handled_fs=handled_fs)
        rw_file = access_mask_for_mode(mode, writable=True, handled_fs=handled_fs)
        assert (ro_file & LANDLOCK_DIR_ONLY_ACCESS_MASK) == 0
        assert (rw_file & LANDLOCK_DIR_ONLY_ACCESS_MASK) == 0
        assert ro_file == (LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE)
        assert rw_file == (
            LANDLOCK_ACCESS_FS_EXECUTE
            | LANDLOCK_ACCESS_FS_READ_FILE
            | LANDLOCK_ACCESS_FS_WRITE_FILE
            | LANDLOCK_ACCESS_FS_TRUNCATE
        )

    # 目录可合法包含 READ_DIR / MAKE_REG / REFER 等目录位
    dir_mode = stat.S_IFDIR | 0o755
    ro_dir = access_mask_for_mode(dir_mode, writable=False, handled_fs=handled_fs)
    rw_dir = access_mask_for_mode(dir_mode, writable=True, handled_fs=handled_fs)
    assert ro_dir == (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_READ_DIR
    )
    assert (rw_dir & LANDLOCK_ACCESS_FS_MAKE_REG) != 0
    assert (rw_dir & LANDLOCK_ACCESS_FS_REFER) != 0
    assert rw_dir == handled_fs


def test_landlock_apply_in_child_happy_path_and_fail_closed_exit_126():
    class ExitTrap(BaseException):
        def __init__(self, code: int) -> None:
            self.code = code

    def raise_exit(code: int):
        raise ExitTrap(code)

    rules = (
        LandlockRuleSpec(path="/usr", writable=False, required=False),
        LandlockRuleSpec(path="/missing-optional", writable=False, required=False),
        LandlockRuleSpec(path="/dev/null", writable=True, required=True),
        LandlockRuleSpec(path="/workspace", writable=True, required=True),
    )

    modes = {
        10: stat.S_IFDIR | 0o755,
        11: stat.S_IFCHR | 0o666,
        12: stat.S_IFDIR | 0o700,
    }
    fds_for_path = {"/usr": 10, "/dev/null": 11, "/workspace": 12}

    # 1. ABI 4 正常路径：size=16, net=0x3，文件/目录掩码精确分离，所有 fd 正常关闭
    closed: list[int] = []
    added: list[tuple[int, int]] = []
    created_args: list[tuple[int, int, int]] = []
    restricted: list[int] = []

    def open_ok(path: str) -> int:
        if path == "/missing-optional":
            raise FileNotFoundError(errno.ENOENT, "missing", path)
        return fds_for_path[path]

    _apply_landlock_in_child(
        4,
        rules,
        prctl_fn=lambda: 0,
        create_ruleset_fn=lambda attr, size, flags: (
            created_args.append((attr.handled_access_fs, attr.handled_access_net, size))
            or 99
        ),
        add_rule_fn=lambda rfd, pattr, flags: (
            added.append((pattr.parent_fd, pattr.allowed_access)) or 0
        ),
        restrict_self_fn=lambda rfd, flags: restricted.append(rfd) or 0,
        open_path_fn=open_ok,
        fstat_mode_fn=lambda fd: modes[fd],
        close_fd_fn=closed.append,
        exit_fn=raise_exit,
    )
    assert created_args == [(LANDLOCK_HANDLED_FS_ABI3, LANDLOCK_HANDLED_NET_ABI4, 16)]
    assert len(added) == 3
    # fd 11 (/dev/null) 绝无目录专属位
    assert (added[1][1] & LANDLOCK_DIR_ONLY_ACCESS_MASK) == 0
    assert restricted == [99]
    assert sorted(closed) == [10, 11, 12, 99]

    # 2. 任一系统调用或必需路径失败 → 立即 fail-closed 退出 126，绝不放行裸跑
    failure_cases = [
        {"prctl_fn": lambda: -1},
        {"create_ruleset_fn": lambda _a, _s, _f: -1},
        {"add_rule_fn": lambda _r, _p, _f: -1},
        {"restrict_self_fn": lambda _r, _f: -1},
        {
            "open_path_fn": lambda p: (_ for _ in ()).throw(
                FileNotFoundError(errno.ENOENT, "missing required", p)
            )
            if p == "/workspace"
            else open_ok(p)
        },
        {
            "open_path_fn": lambda p: (_ for _ in ()).throw(
                PermissionError(errno.EACCES, "denied optional", p)
            )
            if p == "/missing-optional"
            else open_ok(p)
        },
        {"fstat_mode_fn": lambda _fd: (_ for _ in ()).throw(RuntimeError("boom"))},
    ]
    for override in failure_cases:
        kwargs = {
            "prctl_fn": lambda: 0,
            "create_ruleset_fn": lambda _a, _s, _f: 99,
            "add_rule_fn": lambda _r, _p, _f: 0,
            "restrict_self_fn": lambda _r, _f: 0,
            "open_path_fn": open_ok,
            "fstat_mode_fn": lambda fd: modes[fd],
            "close_fd_fn": lambda _fd: None,
            "exit_fn": raise_exit,
        }
        kwargs.update(override)
        with pytest.raises(ExitTrap) as trapped:
            _apply_landlock_in_child(4, rules, **kwargs)
        assert trapped.value.code == LANDLOCK_FAIL_CLOSED_EXIT_CODE


@pytest.mark.skipif(os.name != "posix", reason="preexec_fn 仅存在于 POSIX 子进程")
def test_landlock_preexec_failure_in_real_forked_child_exits_126_without_running_payload(
    tmp_path,
):
    """真子进程负控：fork 后 preexec 阶段 Landlock 失败必须硬退 126，目标脚本零执行。"""
    marker = tmp_path / "must_not_exist.txt"
    pol = _policy(tmp_path)
    rules = (LandlockRuleSpec(path=str(tmp_path), writable=True, required=True),)

    def failing_preexec() -> None:
        _apply_landlock_in_child(
            4,
            rules,
            prctl_fn=lambda: 0,
            create_ruleset_fn=lambda _a, _s, _f: -1,
            add_rule_fn=lambda _r, _p, _f: 0,
            restrict_self_fn=lambda _r, _f: 0,
        )

    res = _spawn_pipes(
        [
            PY,
            "-c",
            f"import pathlib; pathlib.Path({str(marker)!r}).write_bytes(b'ran'); print('RAN')",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        policy=pol,
        blob_dir=None,
        preexec_fn=failing_preexec,
    )
    assert res.exit_code == LANDLOCK_FAIL_CLOSED_EXIT_CODE
    assert res.stdout == ""
    assert not marker.exists()


# ---------------------------------------------------------------- 4. macOS Seatbelt 原生实测


@pytest.mark.skipif(
    not seatbelt_available(),
    reason="仅在具备可执行 /usr/bin/sandbox-exec 的 macOS Darwin 运行实测",
)
def test_macos_seatbelt_live_four_boundaries_all_enforced(tmp_path):
    ws = tmp_path / "ws"
    dr = tmp_path / "data"
    report = probe_backend(SEATBELT_EXEC_BACKEND, workspace=ws, data_root=dr)

    assert report.backend == "seatbelt"
    assert report.probe_version == "v2"
    for boundary in ISOLATION_BOUNDARIES:
        finding = report.finding(boundary)
        assert finding.status == BOUNDARY_ENFORCED, (
            f"Seatbelt 边界 {boundary} 未强制：{finding.evidence}"
        )

    assert measured_level(report) == "app_container"
    view = isolation_view(SEATBELT_EXEC_BACKEND, report)
    assert view["declared"] == "app_container"
    assert view["measured"] == "app_container"
    assert view["level"] == "app_container"
    assert view["unverified"] is False
    assert view["contradicted"] is False
    assert view["enforced_boundaries"] == sorted(ISOLATION_BOUNDARIES)
    assert len(enabled_capabilities(SEATBELT_EXEC_BACKEND, report=report)) == 3

    passed = require_isolation_requirements(
        _policy(ws, isolation_requires=ISOLATION_BOUNDARIES),
        SEATBELT_EXEC_BACKEND,
        workspace=ws,
        data_root=dr,
        report=report,
    )
    assert passed is report


@pytest.mark.skipif(
    not seatbelt_available(),
    reason="仅在具备可执行 /usr/bin/sandbox-exec 的 macOS Darwin 运行实测",
)
def test_macos_seatbelt_live_fs_net_env_tree_and_pty_enforcement(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "fake_home"
    fake_keychains = fake_home / "Library" / "Keychains"
    fake_keychains.mkdir(parents=True)
    (fake_keychains / "sentinel.db").write_bytes(b"do-not-read")
    monkeypatch.setenv("HOME", str(fake_home))

    ws = tmp_path / "ws"
    ws.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    events_db = data_root / "events.db"
    conn = sqlite3.connect(events_db)
    try:
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('secret-event')")
        conn.commit()
    finally:
        conn.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    script = """
import os, pathlib, socket, sqlite3, sys

ws = pathlib.Path(sys.argv[1])
db = pathlib.Path(sys.argv[2])
kc = pathlib.Path(sys.argv[3])
port = int(sys.argv[4])

(ws / "written.txt").write_bytes(b"ok-inside-ws")
assert (ws / "written.txt").read_bytes() == b"ok-inside-ws"

for label, target in [("DB_READ", db), ("KC_READ", kc)]:
    try:
        target.read_bytes()
        print(f"{label}=ALLOWED_BUG")
    except PermissionError:
        print(f"{label}=DENIED")

try:
    (db.parent / "evil.txt").write_bytes(b"x")
    print("DATA_WRITE=ALLOWED_BUG")
except PermissionError:
    print("DATA_WRITE=DENIED")

try:
    con = sqlite3.connect(str(db))
    con.execute("SELECT * FROM t").fetchall()
    print("SQLITE_OPEN=ALLOWED_BUG")
except Exception:
    print("SQLITE_OPEN=DENIED")

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1.0)
try:
    sock.connect(("127.0.0.1", port))
    print("TCP_CONNECT=ALLOWED_BUG")
except OSError:
    print("TCP_CONNECT=DENIED")
finally:
    sock.close()

print("SENTINEL=" + repr(os.environ.get("TIANSHU_ISOLATION_SENTINEL")))
print("OPENAI=" + repr(os.environ.get("OPENAI_API_KEY")))
print("SAFE=" + repr(os.environ.get("SAFE_FLAG")))
"""
    try:
        pol = _policy(
            ws,
            env_allowlist=(
                "PATH",
                "TIANSHU_ISOLATION_SENTINEL",
                "OPENAI_API_KEY",
                "SAFE_FLAG",
            ),
            allow_pty=True,
        )
        res = SEATBELT_EXEC_BACKEND.spawn(
            [
                PY,
                "-c",
                script,
                str(ws),
                str(events_db),
                str(fake_keychains / "sentinel.db"),
                str(port),
            ],
            cwd=ws,
            env={
                "PATH": "/usr/bin:/bin",
                "TIANSHU_ISOLATION_SENTINEL": "leak-1",
                "OPENAI_API_KEY": "sk-leak-2",
                "SAFE_FLAG": "kept",
            },
            policy=pol,
        )
    finally:
        listener.close()

    assert res.exit_code == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert (ws / "written.txt").read_bytes() == b"ok-inside-ws"
    assert "DB_READ=DENIED" in res.stdout
    assert "KC_READ=DENIED" in res.stdout
    assert "DATA_WRITE=DENIED" in res.stdout
    assert "SQLITE_OPEN=DENIED" in res.stdout
    assert "TCP_CONNECT=DENIED" in res.stdout
    assert "SENTINEL=None" in res.stdout
    assert "OPENAI=None" in res.stdout
    assert "SAFE='kept'" in res.stdout

    # PTY 模式实测
    pty_res = SEATBELT_EXEC_BACKEND.spawn(
        [PY, "-c", "import sys; print('TTY=' + str(sys.stdout.isatty()))"],
        cwd=ws,
        env={"PATH": "/usr/bin:/bin"},
        policy=pol,
        pty=True,
    )
    assert pty_res.exit_code == 0, pty_res
    assert pty_res.stderr_merged is True
    assert "TTY=True" in pty_res.stdout

    # 进程组超时杀树实测
    pid_file = ws / "grandchild.pid"
    tree_script = (
        "import pathlib, subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pid_file)!r}).write_bytes(str(p.pid).encode()); "
        "time.sleep(60)"
    )
    short_pol = _policy(ws, timeout_ms=800)
    tree_res = SEATBELT_EXEC_BACKEND.spawn(
        [PY, "-c", tree_script],
        cwd=ws,
        env={"PATH": "/usr/bin:/bin"},
        policy=short_pol,
    )
    assert tree_res.timed_out is True
    assert tree_res.descendants_may_survive is False
    grandchild_pid = int(pid_file.read_bytes().decode("utf-8").strip())
    assert _wait_pid_gone(grandchild_pid, PROBE_GRACE_MS) is False


@pytest.mark.skipif(
    not seatbelt_available(),
    reason="仅在具备可执行 /usr/bin/sandbox-exec 的 macOS Darwin 运行实测",
)
def test_macos_seatbelt_dispatcher_pre_approval_view_and_contradiction_guard(tmp_path):
    # 1. 真实 SeatbeltExecBackend：即使 isolation_requires 为空，审批前也实测展示 app_container
    env = _make_dispatch_env(tmp_path / "real", backend=SEATBELT_EXEC_BACKEND)
    try:
        task, run, claim = env.new_run(exec_policy=_policy(env.ws))
        args = {"argv": [PY, "-c", "print('sandboxed-dispatch')"]}
        req = build_request(
            task=task,
            run=run,
            claim=claim,
            call_id="c1",
            tool_id="run_command",
            target=env.ws,
            args=args,
        )
        decision = env.dispatcher.prepare(req, claim, args=args)
        assert decision.is_waiting, decision

        plane = ControlPlane(
            env,
            model=ScriptedModel([]),
            limits=Limits(max_turns=2),
        )
        approvals = plane.handle("approvals.list", {"run_id": run.run_id})["approvals"]
        assert len(approvals) == 1
        pre_view = approvals[0]["isolation"]
        assert pre_view["backend"] == "seatbelt"
        assert pre_view["declared"] == "app_container"
        assert pre_view["measured"] == "app_container"
        assert pre_view["level"] == "app_container"
        assert pre_view["unverified"] is False
        assert pre_view["contradicted"] is False
        assert pre_view["enforced_boundaries"] == sorted(ISOLATION_BOUNDARIES)

        env.approvals.decide(
            decision.approval_id,
            decision="approved",
            actor=HumanIdentity.local_cli(),
        )
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.status == "succeeded"
        assert result.output["stdout"].strip() == "sandboxed-dispatch"
        assert result.output["isolation"] == pre_view
    finally:
        env.log.close()

    # 2. 负控：自称 app_container 但委托 local 的假后端，审批前立即标出 contradicted=True
    class LyingSandboxBackend:
        capabilities = ExecBackendCapabilities(
            name="lying-sandbox",
            isolation="app_container",
            supports_pty=True,
            supports_signals=True,
            supports_process_group=True,
            supports_network_isolation=True,
        )

        def spawn(self, argv, *, cwd, env, policy, blob_dir=None, pty=False):
            return spawn(
                argv,
                cwd=cwd,
                env=env,
                policy=policy,
                capabilities=LOCAL_EXEC_CAPABILITIES,
                blob_dir=blob_dir,
                pty=pty,
            )

    lying_env = _make_dispatch_env(tmp_path / "lying", backend=LyingSandboxBackend())
    try:
        task2, run2, claim2 = lying_env.new_run(exec_policy=_policy(lying_env.ws))
        args2 = {"argv": [PY, "-c", "print(1)"]}
        req2 = build_request(
            task=task2,
            run=run2,
            claim=claim2,
            call_id="c2",
            tool_id="run_command",
            target=lying_env.ws,
            args=args2,
        )
        dec2 = lying_env.dispatcher.prepare(req2, claim2, args=args2)
        assert dec2.is_waiting, dec2
        plane2 = ControlPlane(
            lying_env,
            model=ScriptedModel([]),
            limits=Limits(max_turns=2),
        )
        lying_view = plane2.handle("approvals.list", {"run_id": run2.run_id})[
            "approvals"
        ][0]["isolation"]
        assert lying_view["declared"] == "app_container"
        assert lying_view["measured"] == "none"
        assert lying_view["level"] == "none"
        assert lying_view["unverified"] is False
        assert lying_view["contradicted"] is True
    finally:
        lying_env.log.close()


# ---------------------------------------------------------------- 5. Linux Landlock 原生实测


@pytest.mark.skipif(
    not landlock_available(),
    reason="仅在启用 Landlock ABI >= 1 的 Linux x86_64/aarch64 内核运行实测",
)
def test_linux_landlock_live_probe_and_dispatcher_end_to_end(tmp_path):
    backend = landlock_exec_backend()
    ws = tmp_path / "ws"
    dr = tmp_path / "data"
    report = probe_backend(backend, workspace=ws, data_root=dr)

    assert report.backend == "landlock"
    assert report.probe_version == "v2"
    for boundary in ("files_authority", "credentials", "process_tree"):
        assert report.finding(boundary).status == BOUNDARY_ENFORCED, (
            report.finding(boundary).evidence
        )

    if backend.abi >= 4:
        assert report.finding("network").status == BOUNDARY_ENFORCED, (
            report.finding("network").evidence
        )
        expected_level = "app_container"
    else:
        expected_level = "process_tree"
        with pytest.raises(IsolationRefused):
            require_isolation_requirements(
                _policy(ws, isolation_requires=("network",)),
                backend,
                workspace=ws,
                data_root=dr,
                report=report,
            )

    assert measured_level(report) == expected_level
    view = isolation_view(backend, report)
    assert view["declared"] == expected_level
    assert view["measured"] == expected_level
    assert view["unverified"] is False
    assert view["contradicted"] is False

    # 工作区读写放行 + data_root 读写拒绝 + 敏感环境变量剥离 +（ABI>=4）loopback 阻断
    outside_secret = dr / "events.db"
    outside_secret.write_bytes(b"sqlite-secret")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    check_code = """
import os, pathlib, socket, sys
ws = pathlib.Path(sys.argv[1])
outside = pathlib.Path(sys.argv[2])
port = int(sys.argv[3])
(ws / "ll.txt").write_bytes(b"landlock-ok")
try:
    outside.read_bytes()
    print("OUT_READ=ALLOWED_BUG")
except PermissionError:
    print("OUT_READ=DENIED")
try:
    (outside.parent / "hack.txt").write_bytes(b"x")
    print("OUT_WRITE=ALLOWED_BUG")
except PermissionError:
    print("OUT_WRITE=DENIED")
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1.0)
try:
    s.connect(("127.0.0.1", port))
    print("NET=OK")
except OSError:
    print("NET=DENIED")
finally:
    s.close()
print("OPENAI=" + repr(os.environ.get("OPENAI_API_KEY")))
"""
    try:
        res = backend.spawn(
            [PY, "-c", check_code, str(ws), str(outside_secret), str(port)],
            cwd=ws,
            env={"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "sk-secret"},
            policy=_policy(ws, env_allowlist=("PATH", "OPENAI_API_KEY")),
        )
    finally:
        listener.close()

    assert res.exit_code == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert (ws / "ll.txt").read_bytes() == b"landlock-ok"
    assert "OUT_READ=DENIED" in res.stdout
    assert "OUT_WRITE=DENIED" in res.stdout
    assert "OPENAI=None" in res.stdout
    if backend.abi >= 4:
        assert "NET=DENIED" in res.stdout

    # Dispatcher + ControlPlane 审批前实测视图检查
    env = _make_dispatch_env(tmp_path / "dispatch_ll", backend=backend)
    try:
        task, run, claim = env.new_run(exec_policy=_policy(env.ws))
        args = {"argv": [PY, "-c", "print('ll-dispatch-ok')"]}
        req = build_request(
            task=task,
            run=run,
            claim=claim,
            call_id="c1",
            tool_id="run_command",
            target=env.ws,
            args=args,
        )
        decision = env.dispatcher.prepare(req, claim, args=args)
        assert decision.is_waiting, decision
        plane = ControlPlane(env, model=ScriptedModel([]), limits=Limits(max_turns=2))
        pre_view = plane.handle("approvals.list", {"run_id": run.run_id})["approvals"][
            0
        ]["isolation"]
        assert pre_view["backend"] == "landlock"
        assert pre_view["measured"] == expected_level
        assert pre_view["unverified"] is False
        assert pre_view["contradicted"] is False
    finally:
        env.log.close()
