from __future__ import annotations

import platform
from pathlib import Path

import pytest

from tianshu import release_smoke
from tianshu.host import HostInfo
from tianshu.leases import pid_alive
from tianshu.release_smoke import ReleaseSmokeError, run_smoke

ROOT = Path(__file__).resolve().parents[1]


def test_release_smoke_starts_real_sidecar_host_and_daemon():
    result = run_smoke(
        ui_dir=ROOT / "ui" / "dist",
        expected_version="0.1.0",
    )

    assert result["status"] == "pass"
    assert result["mode"] == "owned_daemon_then_attached_host"
    assert result["host_pid"] != result["daemon_pid"]
    assert result["checks"] == [
        "import",
        "daemon_ipc",
        "daemon_identity",
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

    result = run_smoke(
        ui_dir=ROOT / "ui" / "dist",
        expected_version="0.1.0",
    )

    assert result["status"] == "pass"
    assert not marker.exists()


def test_host_probe_rejects_missing_and_wrong_tokens(monkeypatch):
    pid = 4242
    good = "good_token_0123456789abcdef"
    wrong = release_smoke.SMOKE_WRONG_TOKEN

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
            return {"ok": True, "pid": pid}
        if path == "/state":
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(release_smoke, "_request_json", request)

    endpoint = release_smoke._host_endpoint(process, Path("/unused"))

    assert endpoint["pid"] == pid
    assert calls == [
        ("GET", "/health", None, 200),
        ("GET", "/session", None, 401),
        ("GET", "/session", wrong, 401),
        ("GET", "/session", good, 200),
        ("GET", "/state", good, 200),
    ]


def test_host_probe_rejects_pid_drift(monkeypatch):
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
        ),
    )
    monkeypatch.setattr(release_smoke, "STARTUP_TIMEOUT_S", 0.01)

    with pytest.raises(ReleaseSmokeError, match="Host PID"):
        release_smoke._host_endpoint(Process(), Path("/unused"))


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
