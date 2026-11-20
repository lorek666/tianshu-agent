from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from _startup.release_smoke_test_launcher import ISOLATION_ENV, MODULE_NAME, SmokeTestIsolation

from tianshu import release_smoke
from tianshu.host import HostInfo, HostReleaseIdentity
from tianshu.leases import pid_alive
from tianshu.release_smoke import ReleaseSmokeError, run_smoke

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"
HOST_TARGET = release_smoke.host_target()
PACKAGED_PYTHON = platform.python_version()


def _test_isolation(root):
    return SmokeTestIsolation(
        root, app_dir=Path(release_smoke.__file__).resolve().parent.parent,
        ui_dir=ROOT / "ui" / "dist",
        release={"version": "0.1.0", "commit": COMMIT, "target": HOST_TARGET},
        python_version=PACKAGED_PYTHON,
        runtime_python=release_smoke._runtime_python_executable(),
    )


@pytest.fixture
def isolated_smoke_processes(monkeypatch, tmp_path):
    """Retain real source services, but prove test roots before CLI startup."""
    isolation = _test_isolation(tmp_path / "smoke-isolation")
    isolation.bind(monkeypatch)
    # Do not patch shared subprocess.Popen or any production file.
    monkeypatch.setattr(release_smoke, "subprocess", isolation.proxy())
    return isolation


@pytest.mark.parametrize("field", [
    "TIANSHU_TEST_LOCK_ROOT", "PYTEST_TIANSHU_REGISTRY_HOME",
    "TIANSHU_TEST_REGISTRY_HOME", "TIANSHU_AUTHORITY_ROOT", "PYTHONPATH",
])
def test_smoke_test_adapter_rejects_drift_before_process_start(tmp_path, field):
    isolation = _test_isolation(tmp_path / "smoke-isolation")
    calls = []
    isolation.native_popen = lambda *args, **kwargs: calls.append((args, kwargs))
    environment = {**isolation.environment, "PYTHONPATH": str(isolation.app_dir)}
    environment[field] = str(tmp_path / "wrong")
    with pytest.raises(RuntimeError, match="smoke isolation"):
        isolation.popen(
            [sys.executable, "-P", "-m", "tianshu", "daemon"],
            env=environment,
            cwd=isolation.app_dir,
        )
    assert calls == []
    assert not (isolation.root / "daemon.json").exists()


@pytest.mark.parametrize("fault", [
    "missing_startup_hook", "wrong_lock_root", "wrong_actual_lock_root",
])
def test_smoke_test_launcher_rejects_unisolated_child_before_cli(tmp_path, fault):
    isolation = _test_isolation(tmp_path / "smoke-isolation")
    for value in set(isolation.environment.values()):
        Path(value).mkdir(parents=True)
    marker = tmp_path / "cli-started"
    environment = {**os.environ, **isolation.environment}
    environment[ISOLATION_ENV] = json.dumps(isolation.payload())
    environment["PYTHONPATH"] = str(isolation.app_dir)
    if fault != "missing_startup_hook":
        environment["PYTHONPATH"] = os.pathsep.join((
            str(isolation.startup), str(isolation.app_dir),
        ))
    if fault == "wrong_lock_root":
        environment["TIANSHU_TEST_LOCK_ROOT"] = str(tmp_path / "wrong-locks")
    inject = ""
    if fault == "wrong_actual_lock_root":
        inject = (
            "from tianshu import maintenance; "
            f"maintenance._lock_root=lambda: pathlib.Path({str(tmp_path / 'wrong-locks')!r}); "
        )
    # The missing-hook case loads the launcher by path, without ever importing
    # sitecustomize. A run_module sentinel proves failure is before real CLI.
    script = (
        "import pathlib,runpy,sys; "
        f"{inject}"
        f"runpy.run_module=lambda *a,**k: pathlib.Path({str(marker)!r}).touch(); "
        "sys.argv=['launcher','cli','daemon']; "
        f"runpy.run_path({str(isolation.startup / (MODULE_NAME + '.py'))!r}, "
        "run_name='__main__')"
    )
    completed = subprocess.run(
        [sys.executable, "-P", "-c", script], env=environment,
        capture_output=True, timeout=20,
    )
    assert completed.returncode != 0
    assert b"smoke isolation" in completed.stderr
    assert not marker.exists()
    assert not (isolation.root / "daemon.json").exists()
    assert not (tmp_path / "wrong-locks").exists()


def _daemon_launch(isolation, tmp_path):
    data_root = str(tmp_path / "data")
    environment = {
        **isolation.environment, "PYTHONPATH": str(isolation.app_dir),
        "TIANSHU_DATA_ROOT": data_root,
    }
    command = [
        isolation.runtime_python, "-P", "-m", "tianshu", "daemon", "--data-root", data_root,
    ]
    return command, environment


@pytest.mark.parametrize("fault", ["parent_root", "argv", "cwd"])
def test_smoke_test_adapter_rejects_actual_launch_drift(tmp_path, monkeypatch, fault):
    from tianshu import maintenance

    isolation = _test_isolation(tmp_path / "smoke-isolation")
    isolation.bind(monkeypatch)
    calls = []
    isolation.native_popen = lambda *args, **kwargs: calls.append((args, kwargs))
    command, environment = _daemon_launch(isolation, tmp_path)
    cwd = isolation.app_dir
    if fault == "parent_root":
        monkeypatch.setattr(maintenance, "_lock_root", lambda: tmp_path / "wrong")
    elif fault == "argv":
        command.append("--unexpected")
    else:
        cwd = tmp_path
    with pytest.raises(RuntimeError, match="smoke isolation"):
        isolation.popen(command, env=environment, cwd=cwd)
    assert calls == []


@pytest.mark.parametrize("runtime", [False, True])
def test_smoke_test_adapter_preserves_launch_after_guard(tmp_path, monkeypatch, runtime):
    isolation = _test_isolation(tmp_path / "smoke-isolation")
    isolation.bind(monkeypatch)
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return type("TestProcess", (), {"pid": 123})()

    isolation.native_popen = record
    command, environment = _daemon_launch(isolation, tmp_path)
    cwd = isolation.app_dir
    if runtime:
        command = [
            sys.executable, "-P", "-m", "tianshu.release_smoke",
            "--ui-dir", str(isolation.ui_dir), "--expected-version", "0.1.0",
            "--expected-commit", COMMIT, "--expected-target", HOST_TARGET,
            "--expected-python", PACKAGED_PYTHON,
        ]
        cwd = isolation.app_dir.parent
    original = command.copy()
    launch = isolation.popen_runtime if runtime else isolation.popen
    launch(command, env=environment, cwd=cwd, start_new_session=True)
    adapted, options = calls[0]
    assert command == original
    assert adapted == [*original[:3], MODULE_NAME, "runtime" if runtime else "cli", *original[4:]]
    assert options["cwd"] == cwd
    assert options["start_new_session"] is True
    assert options["env"]["PYTHONPATH"] == os.pathsep.join((
        str(isolation.startup), str(isolation.app_dir),
    ))
    assert environment["PYTHONPATH"] == str(isolation.app_dir)


@pytest.mark.parametrize("service", ["daemon", "host"])
@pytest.mark.parametrize("fault", ["missing", "wrong_pid"])
def test_smoke_test_receipt_absence_or_pid_drift_is_not_success(tmp_path, service, fault):
    isolation = _test_isolation(tmp_path / "smoke-isolation")
    isolation.root.mkdir()
    result = {"daemon_pid": 11, "host_pid": 12}
    for role in ("daemon", "host"):
        receipt = {
            "pid": result[f"{role}_pid"], "service": role,
            "package": str(isolation.app_dir / "tianshu" / "__init__.py"),
            "roots": {kind: str(isolation.root / kind) for kind in (
                "locks", "registry", "authority",
            )},
        }
        (isolation.root / f"{role}.json").write_text(json.dumps(receipt), encoding="utf-8")
        (isolation.root / f"launched-{role}.pid").write_text(
            str(result[f"{role}_pid"]), encoding="ascii",
        )
    isolation.assert_children(result)
    path = isolation.root / f"{service}.json"
    if fault == "missing":
        path.unlink()
    else:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["pid"] += 100
        path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises((AssertionError, FileNotFoundError)):
        isolation.assert_children(result)


def _run_smoke():
    assert HOST_TARGET is not None
    return run_smoke(
        ui_dir=ROOT / "ui" / "dist",
        expected_version="0.1.0",
        expected_commit=COMMIT,
        expected_target=HOST_TARGET,
        expected_python=PACKAGED_PYTHON,
    )


def test_runtime_python_prefers_existing_base_executable(monkeypatch, tmp_path):
    base_executable = tmp_path / "base-python"
    base_executable.write_bytes(b"")
    monkeypatch.setattr(release_smoke.sys, "_base_executable", str(base_executable))

    assert release_smoke._runtime_python_executable() == str(base_executable)


def test_runtime_python_falls_back_when_base_executable_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_smoke.sys,
        "_base_executable",
        str(tmp_path / "missing-python"),
    )

    assert release_smoke._runtime_python_executable() == release_smoke.sys.executable


@pytest.mark.skipif(
    HOST_TARGET is None,
    reason="打包 sidecar 运行时只支持 macOS/Windows 原生目标",
)
def test_release_smoke_starts_real_sidecar_host_and_daemon(isolated_smoke_processes):
    result = _run_smoke()
    isolated_smoke_processes.assert_children(result)

    assert result["status"] == "pass"
    assert result["mode"] == "owned_daemon_then_attached_host"
    assert result["host_pid"] != result["daemon_pid"]
    assert result["release"] == {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": COMMIT,
        "target": HOST_TARGET,
    }
    assert result["python_runtime"] == {
        "implementation": "CPython",
        "version": PACKAGED_PYTHON,
    }
    assert result["checks"] == [
        "import",
        "daemon_ipc",
        "daemon_identity",
        "release_identity",
        "python_runtime",
        "health",
        "session_token_required",
        "session",
        "state",
        "stop",
    ]
    assert not pid_alive(result["host_pid"])
    assert not pid_alive(result["daemon_pid"])
    assert "token" not in result


def test_release_smoke_rejects_package_version_drift():
    with pytest.raises(ReleaseSmokeError, match="版本.*不一致"):
        run_smoke(
            ui_dir=ROOT / "ui" / "dist",
            expected_version="9.9.9",
            expected_commit=COMMIT,
            expected_target="macos-arm64",
            expected_python=PACKAGED_PYTHON,
        )


def test_release_smoke_rejects_python_runtime_drift():
    with pytest.raises(ReleaseSmokeError, match="Python 运行时身份不匹配"):
        run_smoke(
            ui_dir=ROOT / "ui" / "dist",
            expected_version="0.1.0",
            expected_commit=COMMIT,
            expected_target="macos-arm64",
            expected_python="3.12.999",
        )


@pytest.mark.skipif(
    HOST_TARGET is None,
    reason="打包 sidecar 运行时只支持 macOS/Windows 原生目标",
)
def test_release_smoke_isolates_bundled_package_from_caller_cwd(
    tmp_path,
    monkeypatch,
    isolated_smoke_processes,
):
    shadow = tmp_path / "tianshu"
    shadow.mkdir()
    marker = tmp_path / "shadow-executed"
    (shadow / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (shadow / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('shadow', encoding='ascii')\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = _run_smoke()
    isolated_smoke_processes.assert_children(result)

    assert result["status"] == "pass"
    assert not marker.exists()


@pytest.mark.parametrize(
    ("returncode", "expected_clean"),
    [
        (0, True),
        (7, False),
    ],
)
@pytest.mark.parametrize("request_result", [False, True])
def test_cleanup_accepts_owned_daemon_exit_after_duplicate_shutdown_miss(
    tmp_path,
    monkeypatch,
    returncode,
    expected_clean,
    request_result,
):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(b'ready'); "
                "sys.stdout.buffer.flush(); "
                "sys.stdin.buffer.read(1); "
                "raise SystemExit(int(sys.argv[1]))"
            ),
            str(returncode),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    assert process.stdout.read(5) == b"ready"
    process.stdout.close()

    def request_shutdown(*_args, **_kwargs):
        assert process.stdin is not None
        process.stdin.write(b"x")
        process.stdin.flush()
        process.stdin.close()
        return request_result

    monkeypatch.setattr(release_smoke, "request_shutdown", request_shutdown)
    try:
        clean = release_smoke._cleanup(
            data_root=tmp_path,
            daemon_process=process,
            daemon_identity={"instance_id": "daemon_0123456789ab"},
            host_process=None,
            host_endpoint=None,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert clean is expected_clean


def test_cleanup_rejects_owned_daemon_that_already_exited_nonzero(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait()

    clean = release_smoke._cleanup(
        data_root=tmp_path,
        daemon_process=process,
        daemon_identity={"instance_id": "daemon_0123456789ab"},
        host_process=None,
        host_endpoint=None,
    )

    assert clean is False


def test_cleanup_rejects_owned_host_that_already_exited_nonzero(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait()

    clean = release_smoke._cleanup(
        data_root=tmp_path,
        daemon_process=None,
        daemon_identity=None,
        host_process=process,
        host_endpoint=None,
    )

    assert clean is False


@pytest.mark.parametrize(
    ("returncode", "expected_clean"),
    [
        (0, True),
        (7, False),
    ],
)
def test_cleanup_requires_owned_host_to_exit_zero(
    tmp_path,
    monkeypatch,
    returncode,
    expected_clean,
):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(b'ready'); "
                "sys.stdout.buffer.flush(); "
                "sys.stdin.buffer.read(1); "
                "raise SystemExit(int(sys.argv[1]))"
            ),
            str(returncode),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    assert process.stdout.read(5) == b"ready"
    process.stdout.close()

    def request_json(*_args, **_kwargs):
        assert process.stdin is not None
        process.stdin.write(b"x")
        process.stdin.flush()
        process.stdin.close()
        return {"ok": True, "shutting_down": True}

    monkeypatch.setattr(release_smoke, "_request_json", request_json)
    try:
        clean = release_smoke._cleanup(
            data_root=tmp_path,
            daemon_process=None,
            daemon_identity=None,
            host_process=process,
            host_endpoint={
                "pid": process.pid,
                "port": 43210,
                "token": "host_token_0123456789abcdef",
                "started_at": 1,
            },
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert clean is expected_clean


def test_host_probe_rejects_missing_and_wrong_tokens(monkeypatch):
    pid = 4242
    good = "good_token_0123456789abcdef"
    wrong = release_smoke.SMOKE_WRONG_TOKEN
    release = HostReleaseIdentity(
        version="0.1.0",
        commit=COMMIT,
        target="macos-arm64",
    )

    class Process:
        def poll(self):
            return None

    process = Process()
    process.pid = pid
    monkeypatch.setattr(
        release_smoke,
        "read_host_info",
        lambda _root: HostInfo(
            pid=pid,
            port=43210,
            token=good,
            started_at=1,
            ui_dir="/ui",
            release=release,
        ),
    )
    calls = []

    def request(port, method, path, *, token, expected_status=200):
        calls.append((method, path, token, expected_status))
        if path == "/health":
            return {"ok": True, "pid": pid}
        if path == "/session" and token is None:
            return {"ok": False}
        if path == "/session" and token == wrong:
            return {"ok": False}
        if path == "/session":
            return {
                "ok": True,
                "pid": pid,
                "release": release.payload(),
            }
        if path == "/state":
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(release_smoke, "_request_json", request)

    endpoint = release_smoke._host_endpoint(process, Path("/unused"), release)

    assert endpoint["pid"] == pid
    assert calls == [
        ("GET", "/health", None, 200),
        ("GET", "/session", None, 401),
        ("GET", "/session", wrong, 401),
        ("GET", "/session", good, 200),
        ("GET", "/state", good, 200),
    ]


def test_host_probe_rejects_pid_drift(monkeypatch):
    release = HostReleaseIdentity(
        version="0.1.0",
        commit=COMMIT,
        target="macos-arm64",
    )

    class Process:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        release_smoke,
        "read_host_info",
        lambda _root: HostInfo(
            pid=4243,
            port=43210,
            token="good_token_0123456789abcdef",
            started_at=1,
            ui_dir="/ui",
            release=release,
        ),
    )
    monkeypatch.setattr(release_smoke, "STARTUP_TIMEOUT_S", 0.01)

    with pytest.raises(ReleaseSmokeError, match="Host PID"):
        release_smoke._host_endpoint(Process(), Path("/unused"), release)


def test_host_probe_rejects_missing_release_identity(monkeypatch):
    pid = 4242
    expected = HostReleaseIdentity(
        version="0.1.0",
        commit=COMMIT,
        target="macos-arm64",
    )

    class Process:
        def poll(self):
            return None

    process = Process()
    process.pid = pid
    monkeypatch.setattr(
        release_smoke,
        "read_host_info",
        lambda _root: HostInfo(
            pid=pid,
            port=43210,
            token="good_token_0123456789abcdef",
            started_at=1,
            ui_dir="/ui",
        ),
    )
    monkeypatch.setattr(
        release_smoke,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "pid": pid},
    )

    with pytest.raises(ReleaseSmokeError, match="发行身份"):
        release_smoke._host_endpoint(process, Path("/unused"), expected)


def test_host_probe_rejects_host_lock_release_identity_drift(monkeypatch):
    expected = HostReleaseIdentity(
        version="0.1.0",
        commit=COMMIT,
        target="macos-arm64",
    )
    wrong = HostReleaseIdentity(
        version="0.1.0",
        commit="0" * 40,
        target="macos-arm64",
    )

    class Process:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        release_smoke,
        "read_host_info",
        lambda _root: HostInfo(
            pid=4242,
            port=43210,
            token="good_token_0123456789abcdef",
            started_at=1,
            ui_dir="/ui",
            release=wrong,
        ),
    )

    with pytest.raises(ReleaseSmokeError, match="Host lock 发行身份"):
        release_smoke._host_endpoint(Process(), Path("/unused"), expected)


@pytest.mark.parametrize("session_release", [None, "wrong"])
def test_host_probe_rejects_session_release_identity_drift(
    monkeypatch,
    session_release,
):
    expected = HostReleaseIdentity(
        version="0.1.0",
        commit=COMMIT,
        target="macos-arm64",
    )
    if session_release == "wrong":
        session_release = {
            **expected.payload(),
            "commit": "0" * 40,
        }

    class Process:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        release_smoke,
        "read_host_info",
        lambda _root: HostInfo(
            pid=4242,
            port=43210,
            token="good_token_0123456789abcdef",
            started_at=1,
            ui_dir="/ui",
            release=expected,
        ),
    )

    def request(_port, _method, path, *, token, expected_status=200):
        if path == "/health":
            return {"ok": True, "pid": 4242}
        if path == "/session" and token is None:
            return {"ok": False}
        if path == "/session" and token == release_smoke.SMOKE_WRONG_TOKEN:
            return {"ok": False}
        if path == "/session":
            payload = {"ok": True, "pid": 4242}
            if session_release is not None:
                payload["release"] = session_release
            return payload
        raise AssertionError(path)

    monkeypatch.setattr(release_smoke, "_request_json", request)

    with pytest.raises(ReleaseSmokeError, match="Host /session 发行身份"):
        release_smoke._host_endpoint(Process(), Path("/unused"), expected)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "aarch64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Darwin", "amd64", "macos-x64"),
        ("Windows", "arm64", "windows-arm64"),
        ("Windows", "AMD64", "windows-x64"),
        ("Linux", "x86_64", None),
        ("Darwin", "riscv64", None),
        ("Windows", "x86", None),
    ],
)
def test_host_target_accepts_only_declared_platform_architectures(
    monkeypatch,
    system,
    machine,
    expected,
):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)

    assert release_smoke.host_target() == expected
