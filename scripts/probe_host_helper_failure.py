"""Exercise the frozen Host test helper's real failure cleanup in its pytest process."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HOST_SHA256 = "68ea19824784ff3b079e1696adb0088ff9227c7ba2a53a6e94fe78a044976ffd"
TEST_SHA256 = "dc274a06197b1b3fc67bbbad817d330e0242838b65c0976a1654c38a5d695048"


def source_hashes(path: Path) -> dict:
    raw = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "lf_sha256": hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
    }


def raw_output(path: Path, raw: bytes) -> dict:
    path.write_bytes(raw)
    return {"path": path.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class ProcessWitness:
    """Hold a Windows process handle so PID reuse cannot counterfeit termination."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.handle = None
        self.open_error = None
        if os.name == "nt":
            from ctypes import wintypes

            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            self.api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            self.api.OpenProcess.restype = wintypes.HANDLE
            self.api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.api.WaitForSingleObject.restype = wintypes.DWORD
            self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.api.CloseHandle.restype = wintypes.BOOL
            self.handle = self.api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not self.handle:
                error = ctypes.get_last_error()
                self.open_error = error
                assert error == 87, ("OpenProcess", pid, error)
                # The naturally failing child can exit before its PID is observed.

    def exited(self, timeout: float = 0) -> bool:
        if os.name == "nt":
            if not self.handle:
                return True
            result = self.api.WaitForSingleObject(self.handle, int(timeout * 1000))
            assert result in (0, 258), ("WaitForSingleObject", self.pid, result)
            return result == 0
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def close(self) -> None:
        if self.handle:
            assert self.api.CloseHandle(self.handle), ("CloseHandle", self.pid)
            self.handle = None


def witness_record(witnesses: dict) -> dict:
    return {
        role: {
            "pid": witness.pid, "native_process_handle": bool(witness.handle),
            "open_error": witness.open_error,
            "method": "WaitForSingleObject" if os.name == "nt" else "POSIX kill(pid, 0)",
        }
        for role, witness in witnesses.items()
    }


def observe_communication(peer, case: dict, output: Path, label: str) -> None:
    """Record the real calls and bytes; never change arguments, results or errors."""
    original = peer.proc.communicate
    case["communicate"] = []

    def observed(*args, **kwargs):
        started = time.monotonic()
        entry = {"timeout": kwargs.get("timeout")}
        case["communicate"].append(entry)
        try:
            result = original(*args, **kwargs)
        except BaseException as error:
            entry.update(error=type(error).__name__, duration_s=time.monotonic() - started)
            if isinstance(error, subprocess.TimeoutExpired):
                entry["stderr_partial"] = raw_output(
                    output / f"{label}.partial.stderr.bin", error.stderr or b""
                )
            raise
        entry.update(duration_s=time.monotonic() - started, exit=peer.proc.returncode)
        entry["stderr"] = raw_output(output / f"{label}.stderr.bin", result[1])
        return result

    peer.proc.communicate = observed


@pytest.fixture(scope="session")
def candidate():
    repo = Path(os.environ["C007_HELPER_REPO"]).resolve()
    output = Path(os.environ["C007_HELPER_OUTPUT"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    host_path = repo / "src/tianshu/host.py"
    test_path = repo / "tests/test_host_cleanup.py"
    record = {
        "platform": platform.platform(), "python": sys.version,
        "executable": sys.executable, "pytest_pid": os.getpid(),
        "environment": {key: os.environ.get(key) for key in ("PYTHONUTF8", "PYTHONIOENCODING")},
        "host_source": source_hashes(host_path), "test_source": source_hashes(test_path),
        "probe_source": source_hashes(Path(__file__)), "cases": [],
    }
    try:
        assert record["host_source"]["lf_sha256"] == HOST_SHA256, record["host_source"]
        assert record["test_source"]["lf_sha256"] == TEST_SHA256, record["test_source"]
        sys.path.insert(0, str(repo / "src"))
        spec = importlib.util.spec_from_file_location("frozen_host_cleanup_helper", test_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert Path(module.__file__).resolve() == test_path
        assert Path(module.host.__file__).resolve() == host_path
        record["loaded_host_source"] = source_hashes(Path(module.host.__file__))
        record["loaded_test_source"] = source_hashes(Path(module.__file__))
        assert record["loaded_host_source"] == record["host_source"]
        assert record["loaded_test_source"] == record["test_source"]
        yield module, record, output.parent
        record["sources_stable"] = (
            source_hashes(host_path) == record["host_source"]
            and source_hashes(test_path) == record["test_source"]
            and source_hashes(Path(__file__)) == record["probe_source"]
        )
        assert record["sources_stable"], "Bound source changed during probe"
    finally:
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_natural_child_failure_preserves_exception_and_full_stderr(candidate):
    module, record, output = candidate
    case = {"case": "natural_child_failure"}
    record["cases"].append(case)
    witnesses = {}
    original_error = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="hfail-", dir=None if os.name == "nt" else "/tmp"
        ) as directory:
            try:
                with module._peer(Path(directory), "not-a-mode") as peer:
                    case.update(worker_pid=peer.pid, launcher_pid=peer.proc.pid)
                    witnesses = {
                        "worker": ProcessWitness(peer.pid),
                        "launcher": ProcessWitness(peer.proc.pid),
                    }
                    case["process_witnesses"] = witness_record(witnesses)
                    observe_communication(peer, case, output, "natural-child")
                    try:
                        peer.receive()
                    except BaseException as error:
                        original_error = error
                        raise
            except BaseException as error:
                case.update(
                    original_error=str(error), notes=getattr(error, "__notes__", []),
                    original_error_type=type(error).__name__,
                    original_exception_identity=error is original_error,
                    exit=peer.proc.returncode,
                )
                assert error is original_error, case
                assert isinstance(error, (AssertionError, ConnectionResetError)), case
                assert peer.proc.returncode == 1, case
                stderr = (output / "natural-child.stderr.bin").read_bytes()
                traceback = stderr.replace(b"\r\n", b"\n")
                assert traceback.startswith(b"Traceback (most recent call last):\n"), stderr
                assert traceback.endswith(b"AssertionError: not-a-mode\n"), stderr
                assert "stderr_complete=True" in "\n".join(case["notes"]), case
                case["exited"] = {role: witness.exited() for role, witness in witnesses.items()}
                assert all(case["exited"].values()), case
            else:
                raise AssertionError("Natural child failure was swallowed")
    finally:
        for witness in witnesses.values():
            witness.close()


def test_parent_failure_runs_real_worker_tree_cleanup(candidate):
    module, record, output = candidate
    case = {"case": "parent_failure_after_host_ready", "taskkill": []}
    record["cases"].append(case)
    witnesses = {}
    original_error = RuntimeError("intentional parent failure after real Host/Daemon ready")
    original_run = module.subprocess.run

    def observe_run(command, *args, **kwargs):
        result = original_run(command, *args, **kwargs)
        if command[0] == "taskkill":
            case["taskkill"].append({
                "command": list(command), "timeout": kwargs.get("timeout"),
                "exit": result.returncode,
                "stdout": raw_output(output / "taskkill.stdout.bin", result.stdout),
                "stderr": raw_output(output / "taskkill.stderr.bin", result.stderr),
            })
        return result

    module.subprocess.run = observe_run
    directory = tempfile.TemporaryDirectory(
        prefix="hfail-", dir=None if os.name == "nt" else "/tmp"
    )
    root = Path(directory.name)
    failure = None
    try:
        try:
            with module._peer(root, "host:second") as peer:
                assert peer.receive() == {"ready": True}
                daemon_pid = module.host._daemon_pid(root)
                assert isinstance(daemon_pid, int) and daemon_pid != peer.pid
                case.update(worker_pid=peer.pid, launcher_pid=peer.proc.pid, daemon_pid=daemon_pid)
                witnesses = {
                    "worker": ProcessWitness(peer.pid),
                    "launcher": ProcessWitness(peer.proc.pid),
                    "daemon": ProcessWitness(daemon_pid),
                }
                case["process_witnesses"] = witness_record(witnesses)
                assert not any(witness.exited() for witness in witnesses.values())
                assert module.ipc.probe(root, timeout=1)
                case["real_host_and_daemon_ready"] = True
                observe_communication(peer, case, output, "active-host")
                raise original_error
        except BaseException as error:
            case.update(
                original_error=str(error), notes=getattr(error, "__notes__", []),
                original_exception_identity=error is original_error,
                exit=peer.proc.returncode,
            )
            assert error is original_error, case
            notes = "\n".join(case["notes"])
            assert "stderr_complete=True" in notes, case
            assert "子进程清理异常" not in notes, case
            assert (output / "active-host.stderr.bin").read_bytes() == b"", case
            first, last = case["communicate"]
            assert first["timeout"] == 10 and first["error"] == "TimeoutExpired", first
            assert first["duration_s"] >= 10, first
            assert last["timeout"] == 10 and "error" not in last, last
            assert peer.proc.returncode != 0, case
            if os.name == "nt":
                assert len(case["taskkill"]) == 1, case
                kill = case["taskkill"][0]
                assert kill["command"] == ["taskkill", "/PID", str(peer.pid), "/T", "/F"], kill
                assert kill["timeout"] == 10 and kill["exit"] == 0, kill
                assert "taskkill exit=0" in notes, case
                # Observe taskkill /T alone, before any manual Daemon shutdown.
                case["pre_fallback_exited"] = {
                    role: witness.exited(timeout=5 if role == "daemon" else 0)
                    for role, witness in witnesses.items()
                }
                case["pre_fallback_ipc_alive"] = module.ipc.probe(root, timeout=0.5)
                assert set(case["pre_fallback_exited"]) == {"worker", "launcher", "daemon"}
                assert all(case["pre_fallback_exited"].values()), case
                assert not case["pre_fallback_ipc_alive"], case
                case["exited"] = dict(case["pre_fallback_exited"])
                case["ipc_alive_after"] = case["pre_fallback_ipc_alive"]
            else:
                assert case["taskkill"] == [] and peer.proc.returncode == -9, case
                case["shutdown_requested"] = module.daemon.request_shutdown(
                    root, reason="test_cleanup", timeout=5
                )
                case["exited"] = {
                    role: witness.exited(timeout=5 if role == "daemon" else 0)
                    for role, witness in witnesses.items()
                }
                case["ipc_alive_after"] = module.ipc.probe(root, timeout=0.5)
                assert all(case["exited"].values()) and not case["ipc_alive_after"], case
            log = root / "logs/daemon.log"
            assert log.is_file(), "Real daemon log was never created"
            log.unlink()
            case["daemon_log_removed"] = not log.exists()
            assert case["daemon_log_removed"], case
        else:
            raise AssertionError("Intentional parent failure was swallowed")
    except BaseException as error:
        failure = error
        case["validation_failure"] = repr(error)
        raise
    finally:
        cleanup_errors = []
        if failure is not None:
            # Cleanup after a failed verdict cannot change it into passing evidence.
            try:
                case["fallback_shutdown_requested"] = module.daemon.request_shutdown(
                    root, reason="failed_probe_cleanup", timeout=5
                )
                if "daemon" in witnesses:
                    case["fallback_daemon_exited"] = witnesses["daemon"].exited(timeout=5)
                    assert case["fallback_daemon_exited"], "Fallback left Daemon alive"
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            directory.cleanup()
        except BaseException as error:
            cleanup_errors.append(error)
        module.subprocess.run = original_run
        for witness in witnesses.values():
            try:
                witness.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            case["fallback_cleanup_errors"] = [repr(error) for error in cleanup_errors]
            if failure is None:
                raise cleanup_errors[0]
            for error in cleanup_errors:
                failure.add_note(f"Probe cleanup also failed: {error!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    os.environ["C007_HELPER_REPO"] = str(Path(args.repo).resolve())
    os.environ["C007_HELPER_OUTPUT"] = str(output)
    # Binding is checked by the fixture inside this very pytest process.
    code = pytest.main([
        str(Path(__file__).resolve()), "-o", "pythonpath=", "-o", "addopts=",
        "-vv", "-rA", "--tb=short", "-p", "no:cacheprovider",
    ])
    if output.exists():
        record = json.loads(output.read_text(encoding="utf-8"))
        record["pytest_exit"] = int(code)
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
