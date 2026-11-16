from __future__ import annotations

import platform
from pathlib import Path

import pytest

from tianshu import release_smoke
from tianshu.host import HostInfo, HostReleaseIdentity
from tianshu.leases import pid_alive
from tianshu.release_smoke import ReleaseSmokeError, run_smoke

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"
HOST_TARGET = release_smoke.host_target()
PACKAGED_PYTHON = platform.python_version()


def _run_smoke():
    assert HOST_TARGET is not None
    return run_smoke(
        ui_dir=ROOT / "ui" / "dist",
        expected_version="0.1.0",
        expected_commit=COMMIT,
        expected_target=HOST_TARGET,
        expected_python=PACKAGED_PYTHON,
    )


@pytest.mark.skipif(
    HOST_TARGET is None,
    reason="打包 sidecar 运行时只支持 macOS/Windows 原生目标",
)
def test_release_smoke_starts_real_sidecar_host_and_daemon():
    result = _run_smoke()

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

    assert result["status"] == "pass"
    assert not marker.exists()


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
