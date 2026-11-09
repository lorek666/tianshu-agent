"""F1 验收：真正的 spawn、有界 stdio、超时杀树、退出码/信号、PTY 如实声明。

所有子进程都用 `sys.executable -c ...` 造，不依赖外部命令。要钉死的几件事：

1. 正常退出码 / 非零退出码 / stdout 与 stderr 分开。
2. 超时 → `timed_out=True` 且 `exit_code is None`（不伪装成退出码 1）。
3. 输出超上限 → `truncated=True`，完整原文留在 blob 里（sha256 对得上），
   而且**继续排空管道**——不排空的话子进程写满管道就会阻塞，超时逻辑永远
   等不到（用 2 MiB 输出 + 短超时来证伪）。
4. 被信号杀死 → `exit_code=None, signal=<n>`，与超时分开。
5. env 白名单：子进程看不到未列出的变量。
6. 策略拒绝在**创建进程之前**发生。

POSIX 专属用例标 `skipif(os.name != "posix")`；Windows 专属用例标
`skipif(os.name != "nt")`——本机（macOS）跑不了 Windows 分支，PR 里会明确
列出"这些用例在本机不可执行"。
"""

from __future__ import annotations

import ast
import hashlib
import os
import signal
import sys
from pathlib import Path

import pytest

from tianshu.exec import (
    LOCAL_EXEC_CAPABILITIES,
    LocalExecBackend,
    local_capabilities,
    spawn,
)
from tianshu.execpolicy import (
    RULE_ARGV_NOT_ARRAY,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
)

PY = sys.executable
POSIX = os.name == "posix"
WINDOWS = os.name == "nt"


def _cwd(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws.resolve()


def _policy(cwd: Path, **overrides) -> ExecPolicy:
    base = dict(
        argv_allowlist=(PY,),
        cwd_roots=(cwd,),
        env_allowlist=("TIANSHU_TEST", "PATH"),
        max_output_bytes=64 * 1024,
        timeout_ms=10_000,
    )
    base.update(overrides)
    return ExecPolicy(**base)


def _caps(**overrides) -> ExecBackendCapabilities:
    base = dict(
        name="test-local",
        isolation="none",
        supports_pty=True,
        supports_signals=True,
        supports_process_group=True,
    )
    base.update(overrides)
    return ExecBackendCapabilities(**base)


ENV = {"PATH": "/usr/bin:/bin", "TIANSHU_TEST": "yes"}


def _run(
    argv,
    cwd: Path,
    *,
    policy: ExecPolicy | None = None,
    capabilities: ExecBackendCapabilities | None = None,
    env: dict[str, str] | None = None,
    blob_dir: Path | None = None,
):
    return spawn(
        argv,
        cwd=cwd,
        env=ENV if env is None else env,
        policy=_policy(cwd) if policy is None else policy,
        capabilities=_caps() if capabilities is None else capabilities,
        blob_dir=blob_dir,
    )


# ---------------------------------------------------------------- 正常路径


def test_runs_and_reports_stdout_and_exit_code(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run([PY, "-c", "print('hello')"], cwd)

    assert result.exit_code == 0
    assert result.signal is None
    assert result.timed_out is False
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.truncated is False
    assert result.stdout_ref is None and result.stderr_ref is None
    assert result.duration_ms >= 0


def test_nonzero_exit_code_is_reported(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run([PY, "-c", "import sys; sys.exit(3)"], cwd)

    assert result.exit_code == 3
    assert result.signal is None
    assert result.timed_out is False


def test_stderr_is_captured_separately(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run([PY, "-c", "import sys; sys.stderr.write('bad')"], cwd)

    assert result.stdout == ""
    assert result.stderr == "bad"
    assert result.exit_code == 0


def test_local_backend_wrapper_matches_module_spawn(tmp_path):
    cwd = _cwd(tmp_path)
    backend = LocalExecBackend()
    result = backend.spawn([PY, "-c", "print(1)"], cwd=cwd, env=ENV, policy=_policy(cwd))

    assert result.exit_code == 0
    assert result.stdout == "1\n"
    assert backend.capabilities == LOCAL_EXEC_CAPABILITIES


# ---------------------------------------------------------------- 超时


def test_timeout_kills_the_child_and_is_not_an_exit_code(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run(
        [PY, "-c", "import time; time.sleep(30)"],
        cwd,
        policy=_policy(cwd, timeout_ms=300),
    )

    assert result.timed_out is True
    assert result.exit_code is None, "超时不能伪装成退出码"
    # POSIX 上我们发的是 SIGKILL；Windows 没有信号语义
    assert result.signal in (None, 9)
    assert result.duration_ms < 10_000


@pytest.mark.skipif(not POSIX, reason="Windows 没有进程组/信号，见 Windows 专属用例")
def test_posix_timeout_kills_the_whole_process_group(tmp_path):
    """孙进程也必须被杀：不然'杀树'只是句口号。

    子进程再起一个 1.5 秒后写标记文件的孙进程，父进程立刻睡 30 秒；
    300ms 超时后整组被杀，标记文件不应出现。
    """
    cwd = _cwd(tmp_path)
    marker = cwd / "grandchild.txt"
    child_code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('x')\"])\n"
        "time.sleep(30)\n"
    )
    result = _run(
        [PY, "-c", child_code], cwd, policy=_policy(cwd, timeout_ms=300)
    )

    assert result.timed_out is True
    assert result.exit_code is None
    # 等过孙进程原本要写标记的时间点
    import time as _time

    _time.sleep(2.0)
    assert not marker.exists(), "孙进程仍存活：进程组没有被杀干净"


# ---------------------------------------------------------------- 有界输出


def test_output_over_limit_is_truncated_and_blob_holds_full_text(tmp_path):
    cwd = _cwd(tmp_path)
    blob_dir = tmp_path / "blobs"
    result = _run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'x' * 5000)"],
        cwd,
        policy=_policy(cwd, max_output_bytes=1000),
        blob_dir=blob_dir,
    )

    assert result.truncated is True
    assert len(result.stdout) == 1000, "保留前 max_output_bytes 字节"
    assert result.stdout_ref is not None

    blob = (blob_dir / result.stdout_ref).read_bytes()
    assert len(blob) == 5000
    assert blob == b"x" * 5000
    assert hashlib.sha256(blob).hexdigest() == result.stdout_ref


def test_stderr_truncation_sets_combined_flag_and_its_own_ref(tmp_path):
    cwd = _cwd(tmp_path)
    blob_dir = tmp_path / "blobs"
    result = _run(
        [PY, "-c", "import sys; sys.stderr.buffer.write(b'e' * 5000)"],
        cwd,
        policy=_policy(cwd, max_output_bytes=1000),
        blob_dir=blob_dir,
    )

    assert result.truncated is True
    assert result.stderr_ref is not None
    assert (blob_dir / result.stderr_ref).read_bytes() == b"e" * 5000


def test_large_output_is_drained_so_the_child_does_not_deadlock(tmp_path):
    """2 MiB 输出 + 4 KiB 上限：如果到上限就停读，子进程会阻塞在管道里，
    结果会是 timed_out=True（甚至挂死）。正确实现必须一直排空。"""
    cwd = _cwd(tmp_path)
    result = _run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'y' * (2 * 1024 * 1024))"],
        cwd,
        policy=_policy(cwd, max_output_bytes=4096, timeout_ms=10_000),
        blob_dir=tmp_path / "blobs",
    )

    assert result.exit_code == 0
    assert result.timed_out is False, "停读导致子进程阻塞，超时了"
    assert result.truncated is True
    assert len(result.stdout) == 4096


def test_truncated_without_blob_dir_keeps_flag_but_no_ref(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'z' * 5000)"],
        cwd,
        policy=_policy(cwd, max_output_bytes=1000),
    )

    assert result.truncated is True
    assert result.stdout_ref is None, "没有 blob_dir 就不假装有原文证据"


# ---------------------------------------------------------------- env


def test_env_whitelist_hides_unlisted_variables(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run(
        [PY, "-c", "import os; print(','.join(sorted(os.environ)))"],
        cwd,
        policy=_policy(cwd, env_allowlist=("TIANSHU_TEST",)),
        env={
            "TIANSHU_TEST": "yes",
            "SECRET_SAUCE": "leak",
            "PATH": "/usr/bin:/bin",
        },
    )

    keys = set(result.stdout.strip().split(","))
    assert "TIANSHU_TEST" in keys
    assert "SECRET_SAUCE" not in keys, "未列出的变量被带进了子进程"
    assert "PATH" not in keys, "env_allowlist 里没有 PATH"


# ---------------------------------------------------------------- 策略拒绝


def test_empty_policy_denies_before_creating_a_process(tmp_path):
    cwd = _cwd(tmp_path)
    marker = cwd / "should-not-exist.txt"
    argv = [PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]

    with pytest.raises(PolicyDenied):
        spawn(argv, cwd=cwd, env={}, policy=ExecPolicy(), capabilities=_caps())

    assert not marker.exists(), "策略拒绝必须在创建进程之前发生"


def test_string_command_is_denied(tmp_path):
    cwd = _cwd(tmp_path)
    with pytest.raises(PolicyDenied) as excinfo:
        spawn("/usr/bin/echo hi", cwd=cwd, env={}, policy=_policy(cwd), capabilities=_caps())
    assert excinfo.value.rule_id == RULE_ARGV_NOT_ARRAY


def test_cwd_outside_roots_is_denied(tmp_path):
    cwd = _cwd(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(PolicyDenied):
        spawn(
            [PY, "-c", "pass"],
            cwd=outside,
            env={},
            policy=_policy(cwd),
            capabilities=_caps(),
        )


def test_pty_request_without_capability_is_unsupported_not_degraded(tmp_path):
    cwd = _cwd(tmp_path)
    policy = _policy(cwd, allow_pty=True)
    caps = _caps(supports_pty=False)

    with pytest.raises(PolicyUnsupported) as excinfo:
        spawn([PY, "-c", "pass"], cwd=cwd, env={}, policy=policy, capabilities=caps)

    assert excinfo.value.feature == "pty"
    assert not isinstance(excinfo.value, PolicyDenied)


# ---------------------------------------------------------------- 能力声明


def test_local_capabilities_are_honest():
    caps = local_capabilities()

    assert caps.isolation == "none", "argv/cwd/env 是策略约束，不是隔离"
    assert caps.supports_job_objects is False
    assert caps.supports_network_isolation is False
    assert caps.supports_pty is POSIX
    assert caps.supports_process_group is POSIX
    assert caps.supports_signals is POSIX


def test_local_exec_capabilities_constant_matches_factory():
    assert LOCAL_EXEC_CAPABILITIES == local_capabilities()
    assert LocalExecBackend().capabilities == LOCAL_EXEC_CAPABILITIES


# ---------------------------------------------------------------- POSIX 专属


@pytest.mark.skipif(not POSIX, reason="Windows 标准库没有 PTY")
def test_posix_pty_merges_streams(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run(
        [PY, "-c", "print('pty-hello')"],
        cwd,
        policy=_policy(cwd, allow_pty=True),
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "pty-hello" in result.stdout
    assert result.stderr == "", "PTY 下 stderr 合并进 stdout"


@pytest.mark.skipif(not POSIX, reason="Windows 没有 Unix 信号")
def test_posix_signal_death_is_reported_as_signal(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run(
        [
            PY,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        cwd,
    )

    assert result.exit_code is None
    assert result.signal == signal.SIGTERM
    assert result.timed_out is False, "被信号杀死不是超时"


# ---------------------------------------------------------------- Windows 专属
#
# 本机（macOS）不会执行下面这些用例。PR 里明确写了：它们在本机不可执行，
# 等 Windows 加回 CI 后才有证据。不要把它们改写成"默认通过"。


@pytest.mark.skipif(not WINDOWS, reason="仅 Windows：本机不可执行")
def test_windows_timeout_admits_descendants_may_survive(tmp_path):
    cwd = _cwd(tmp_path)
    result = _run(
        [PY, "-c", "import time; time.sleep(30)"],
        cwd,
        policy=_policy(cwd, timeout_ms=300),
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.descendants_may_survive is True, "零依赖拿不到 Job Object，必须如实说"
    assert local_capabilities().supports_process_group is False


@pytest.mark.skipif(not WINDOWS, reason="仅 Windows：本机不可执行")
def test_windows_pty_request_is_unsupported(tmp_path):
    cwd = _cwd(tmp_path)
    # 即使调用方伪造了 supports_pty=True，实现也不能用管道假装 PTY
    caps = _caps(supports_pty=True)

    with pytest.raises(PolicyUnsupported):
        spawn(
            [PY, "-c", "pass"],
            cwd=cwd,
            env={},
            policy=_policy(cwd, allow_pty=True),
            capabilities=caps,
        )


# ---------------------------------------------------------------- 全仓库约束


def test_repo_never_uses_shell_true():
    """全仓库不许出现 `shell=True`（docstring 里提到不算）。"""
    root = Path(__file__).resolve().parents[1] / "src" / "tianshu"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg != "shell":
                        continue
                    if (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"shell=True 出现在：{offenders}"


def test_spawn_requires_bounded_limits(tmp_path):
    """没有上限的策略不允许执行——F1 不绕过 F2 的默认全拒。"""
    cwd = _cwd(tmp_path)
    unlimited = ExecPolicy(
        argv_allowlist=(PY,), cwd_roots=(cwd,), timeout_ms=0, max_output_bytes=0
    )
    with pytest.raises(PolicyDenied):
        spawn([PY, "-c", "pass"], cwd=cwd, env={}, policy=unlimited, capabilities=_caps())


def test_spawn_rejects_wrong_types(tmp_path):
    cwd = _cwd(tmp_path)
    with pytest.raises(TypeError):
        spawn(  # type: ignore[arg-type]
            [PY, "-c", "pass"], cwd=cwd, env={}, policy="policy", capabilities=_caps()
        )
    with pytest.raises(TypeError):
        spawn(  # type: ignore[arg-type]
            [PY, "-c", "pass"], cwd=cwd, env={}, policy=_policy(cwd), capabilities="caps"
        )


def test_blob_dir_is_created_when_needed(tmp_path):
    cwd = _cwd(tmp_path)
    blob_dir = tmp_path / "nested" / "blobs"
    result = _run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'q' * 2000)"],
        cwd,
        policy=_policy(cwd, max_output_bytes=100),
        blob_dir=blob_dir,
    )

    assert result.stdout_ref is not None
    assert (blob_dir / result.stdout_ref).is_file()
