"""Runtime smoke for a bundled sidecar, Host, and Daemon.

The module is launched with the bundled interpreter.  It starts its own Daemon
and an attached Host against one temporary data root, then checks the public
health endpoint, token protection, PID/instance identity, and graceful cleanup.
An import check is recorded, but it can never make the smoke pass by itself.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__, ipc
from .daemon import request_shutdown
from .host import HostError, HostReleaseIdentity, read_host_info

STARTUP_TIMEOUT_S = 30.0
SHUTDOWN_TIMEOUT_S = 10.0
FORCE_STOP_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.05
INSTANCE_RE = re.compile(r"^daemon_[0-9a-f]{12}$")
VERSION_RE = re.compile(r"^0\.[0-9]+\.[0-9]+$")
PYTHON_VERSION_RE = re.compile(r"^3\.12\.[0-9]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TARGETS = frozenset({"macos-arm64", "macos-x64", "windows-x64", "windows-arm64"})
SMOKE_WRONG_TOKEN = "release-smoke-invalid-token"


class ReleaseSmokeError(RuntimeError):
    """Bundled entry points failed at runtime."""


def _runtime_python_executable() -> str:
    """Return the interpreter process that should own Host/Daemon children.

    uv-created Windows venvs expose a launcher as ``sys.executable`` while the
    interpreter that runs Python has a different PID.  Using the real base
    executable keeps Popen's PID bound to the process that publishes
    ``daemon.lock``.  Standalone runtimes normally report the same path in both
    attributes, so this also remains the direct bundled interpreter there.
    """
    base_executable = getattr(sys, "_base_executable", None)
    if isinstance(base_executable, str) and Path(base_executable).is_file():
        return base_executable
    return sys.executable


def _request_json(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None,
    expected_status: int = 200,
) -> dict[str, Any]:
    headers = {"X-Tianshu-Token": token} if token is not None else {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        payload = response.read(1024 * 1024)
        try:
            result = json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseSmokeError(
                f"Host {path} 返回非 JSON（HTTP {response.status}）"
            ) from exc
        if response.status != expected_status:
            raise ReleaseSmokeError(
                f"Host {path} 返回 HTTP {response.status}，期望 {expected_status}"
            )
        if not isinstance(result, dict):
            raise ReleaseSmokeError(f"Host {path} 未返回 object")
        return result
    except (OSError, http.client.HTTPException) as exc:
        raise ReleaseSmokeError(
            f"Host {path} 请求失败：{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        connection.close()


def _read_daemon_identity(process: subprocess.Popen[bytes], data_root: Path) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ReleaseSmokeError(f"Daemon 在就绪前退出：{process.returncode}")
        try:
            with ipc.Client(data_root, timeout=2) as client:
                client.call("state", {}, timeout=2)
            payload = json.loads((data_root / "run" / "daemon.lock").read_bytes())
        except (OSError, ValueError, ipc.IpcError):
            time.sleep(POLL_INTERVAL_S)
            continue
        if not isinstance(payload, dict):
            raise ReleaseSmokeError("Daemon 锁不是 object")
        if payload.get("pid") != process.pid:
            raise ReleaseSmokeError("Daemon PID 与 owned child 不一致")
        if (
            type(payload.get("started_at")) is not int
            or payload["started_at"] <= 0
            or not isinstance(payload.get("instance_id"), str)
            or INSTANCE_RE.fullmatch(payload["instance_id"]) is None
            or payload.get("daemon_version") != __version__
        ):
            raise ReleaseSmokeError("Daemon 实例身份不合法")
        return payload
    raise ReleaseSmokeError("等待 Daemon IPC 就绪超时")


def _host_endpoint(
    process: subprocess.Popen[bytes],
    data_root: Path,
    expected_release: HostReleaseIdentity,
) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ReleaseSmokeError(f"Host 在健康检查前退出：{process.returncode}")
        try:
            info = read_host_info(data_root)
            if info is not None:
                if info.pid != process.pid:
                    raise ReleaseSmokeError("Host PID 与 owned child 不一致")
            if (
                info is not None
                and info.port > 0
                and info.started_at > 0
                and re.fullmatch(r"[A-Za-z0-9_-]{16,256}", info.token) is not None
            ):
                if info.release != expected_release:
                    raise ReleaseSmokeError(
                        "Host lock 发行身份与 bundle release.json 不一致"
                    )
                health = _request_json(info.port, "GET", "/health", token=None)
                if health.get("ok") is not True or health.get("pid") != process.pid:
                    raise ReleaseSmokeError("Host /health PID 与 owned child 不一致")

                unauthorized = _request_json(
                    info.port,
                    "GET",
                    "/session",
                    token=None,
                    expected_status=401,
                )
                if unauthorized.get("ok") is not False:
                    raise ReleaseSmokeError("Host /session 未拒绝无 token 请求")

                wrong_token = _request_json(
                    info.port,
                    "GET",
                    "/session",
                    token=SMOKE_WRONG_TOKEN,
                    expected_status=401,
                )
                if wrong_token.get("ok") is not False:
                    raise ReleaseSmokeError("Host /session 未拒绝错误 token 请求")

                session = _request_json(
                    info.port,
                    "GET",
                    "/session",
                    token=info.token,
                )
                if session.get("ok") is not True or session.get("pid") != process.pid:
                    raise ReleaseSmokeError("Host /session PID 与 owned child 不一致")
                if session.get("release") != expected_release.payload():
                    raise ReleaseSmokeError(
                        "Host /session 发行身份与 bundle release.json 不一致"
                    )

                state = _request_json(
                    info.port,
                    "GET",
                    "/state",
                    token=info.token,
                )
                if state.get("ok") is not True:
                    raise ReleaseSmokeError("Host /state 未返回成功对象")
                return {
                    "pid": info.pid,
                    "port": info.port,
                    "token": info.token,
                    "started_at": info.started_at,
                }
        except HostError as exc:
            if "损坏" in str(exc):
                raise ReleaseSmokeError(str(exc)) from exc
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(POLL_INTERVAL_S)
    raise ReleaseSmokeError("等待 Host 健康端点超时")


def _wait_process(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _terminate_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    if not _wait_process(process, FORCE_STOP_TIMEOUT_S):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=FORCE_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass


def _endpoint_for_owned_host(
    process: subprocess.Popen[bytes],
    data_root: Path,
) -> dict[str, Any] | None:
    try:
        info = read_host_info(data_root)
    except HostError:
        return None
    if info is None or info.pid != process.pid or info.port <= 0:
        return None
    return {
        "pid": info.pid,
        "port": info.port,
        "token": info.token,
        "started_at": info.started_at,
    }


def _cleanup(
    *,
    data_root: Path,
    daemon_process: subprocess.Popen[bytes] | None,
    daemon_identity: dict[str, Any] | None,
    host_process: subprocess.Popen[bytes] | None,
    host_endpoint: dict[str, Any] | None,
) -> bool:
    clean = True
    if host_process is not None and host_process.poll() is None:
        endpoint = host_endpoint or _endpoint_for_owned_host(host_process, data_root)
        if endpoint is not None:
            try:
                _request_json(
                    endpoint["port"],
                    "POST",
                    "/shutdown",
                    token=endpoint["token"],
                )
            except ReleaseSmokeError:
                clean = False
        if not _wait_process(host_process, SHUTDOWN_TIMEOUT_S):
            clean = False
            _terminate_owned(host_process)

    if daemon_process is not None:
        if daemon_process.poll() is None:
            expected_instance_id = (
                daemon_identity.get("instance_id")
                if isinstance(daemon_identity, dict)
                else None
            )
            if expected_instance_id is not None:
                try:
                    request_shutdown(
                        data_root,
                        reason="release_smoke",
                        expected_instance_id=expected_instance_id,
                        timeout=3.0,
                    )
                except (OSError, ipc.IpcError):
                    pass
            if not _wait_process(daemon_process, FORCE_STOP_TIMEOUT_S):
                clean = False
                _terminate_owned(daemon_process)
        # Host shutdown may stop the owned Daemon before this cleanup runs. A
        # duplicate request can then miss the already-removed endpoint; the
        # request result is not authoritative. Accept a missed or acknowledged
        # request only after the exact child exits successfully. Check status
        # outside the live-process branch so a child that already exited
        # non-zero cannot pass by having removed its endpoint first.
        if daemon_process.returncode != 0:
            clean = False

    leftover = [
        name
        for name in ("host.lock", "daemon.lock", "agent.sock")
        if (data_root / "run" / name).exists()
    ]
    if leftover:
        clean = False
    return clean


def _log_tail(log: Any) -> str:
    try:
        log.flush()
        log.seek(0)
        return log.read().decode("utf-8", errors="replace")[-4000:].strip()
    except (OSError, ValueError):
        return ""


def host_target() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system not in {"darwin", "windows"}:
        return None
    if machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    elif machine in {"x86_64", "amd64"}:
        architecture = "x64"
    else:
        return None
    platform_name = "macos" if system == "darwin" else "windows"
    return f"{platform_name}-{architecture}"


def _expected_release(
    *,
    version: str,
    commit: str,
    target: str,
) -> HostReleaseIdentity:
    if VERSION_RE.fullmatch(version) is None:
        raise ReleaseSmokeError("expected release version 必须匹配 0.x.y")
    if COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseSmokeError("expected release commit 必须是 40 位小写十六进制")
    if target not in TARGETS:
        raise ReleaseSmokeError("expected release target 不受支持")
    return HostReleaseIdentity(version=version, commit=commit, target=target)


def run_smoke(
    *,
    ui_dir: Path,
    expected_version: str,
    expected_commit: str,
    expected_target: str,
    expected_python: str,
) -> dict[str, Any]:
    ui_dir = Path(ui_dir).resolve()
    if not (ui_dir / "index.html").is_file():
        raise ReleaseSmokeError(f"UI 目录缺少 index.html：{ui_dir}")
    if __version__ != expected_version:
        raise ReleaseSmokeError(
            f"包内版本 {__version__!r} 与发行版本 {expected_version!r} 不一致"
        )
    if PYTHON_VERSION_RE.fullmatch(expected_python) is None:
        raise ReleaseSmokeError("expected Python version 必须是 3.12.x")
    actual_python = platform.python_version()
    if platform.python_implementation() != "CPython" or actual_python != expected_python:
        raise ReleaseSmokeError(
            "bundled Python 运行时身份不匹配："
            f"期望 CPython {expected_python}，实际 "
            f"{platform.python_implementation()} {actual_python}"
        )
    release = _expected_release(
        version=expected_version,
        commit=expected_commit,
        target=expected_target,
    )

    app_dir = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("TIANSHU_FAILPOINT", None)
    environment["PYTHONPATH"] = str(app_dir)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    runtime_python = _runtime_python_executable()

    daemon_process: subprocess.Popen[bytes] | None = None
    daemon_identity: dict[str, Any] | None = None
    host_process: subprocess.Popen[bytes] | None = None
    host_endpoint: dict[str, Any] | None = None
    failure: str | None = None
    clean_stop = True

    temporary_parent = None if os.name == "nt" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="tsrs-", dir=temporary_parent) as temporary:
        data_root = Path(temporary).resolve()
        environment["TIANSHU_DATA_ROOT"] = str(data_root)
        with tempfile.TemporaryFile() as log:
            try:
                daemon_process = subprocess.Popen(
                    [
                        runtime_python,
                        "-P",
                        "-m",
                        "tianshu",
                        "daemon",
                        "--data-root",
                        str(data_root),
                    ],
                    cwd=app_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=environment,
                )
                daemon_identity = _read_daemon_identity(daemon_process, data_root)

                host_process = subprocess.Popen(
                    [
                        runtime_python,
                        "-P",
                        "-m",
                        "tianshu",
                        "host",
                        "run",
                        "--no-supervise",
                        "--port",
                        "0",
                        "--ui-dir",
                        str(ui_dir),
                        "--data-root",
                        str(data_root),
                        "--release-version",
                        release.version,
                        "--release-commit",
                        release.commit,
                        "--release-target",
                        release.target,
                    ],
                    cwd=app_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=environment,
                )
                host_endpoint = _host_endpoint(host_process, data_root, release)
                current_daemon = json.loads(
                    (data_root / "run" / "daemon.lock").read_bytes()
                )
                if current_daemon != daemon_identity:
                    raise ReleaseSmokeError("Daemon 在烟测期间更换了实例")
            except BaseException as exc:
                failure = f"{type(exc).__name__}: {exc}"
            finally:
                clean_stop = _cleanup(
                    data_root=data_root,
                    daemon_process=daemon_process,
                    daemon_identity=daemon_identity,
                    host_process=host_process,
                    host_endpoint=host_endpoint,
                )
                if failure is not None or not clean_stop:
                    detail = failure or "owned Host/Daemon 未正常停止并清理运行端点"
                    tail = _log_tail(log)
                    if tail:
                        detail += f"；日志尾部=\n{tail}"
                    raise ReleaseSmokeError(detail)

    if host_process is None or daemon_process is None or daemon_identity is None:
        raise ReleaseSmokeError("owned Host/Daemon 未完成启动")
    return {
        "status": "pass",
        "version": __version__,
        "host_target": host_target(),
        "mode": "owned_daemon_then_attached_host",
        "host_pid": host_process.pid,
        "daemon_pid": daemon_process.pid,
        "daemon_instance_id": daemon_identity["instance_id"],
        "release": release.payload(),
        "python_runtime": {
            "implementation": platform.python_implementation(),
            "version": actual_python,
        },
        "checks": [
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
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-target", required=True)
    parser.add_argument("--expected-python", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(
            ui_dir=args.ui_dir,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
            expected_target=args.expected_target,
            expected_python=args.expected_python,
        )
    except ReleaseSmokeError as exc:
        print(f"发行运行时烟测失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
