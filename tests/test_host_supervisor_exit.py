"""A supervisor must reap its own child before it reports a completed stop."""

from __future__ import annotations

import errno
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import support
from support import shutdown_daemon_and_wait

from tianshu import host


def _child_env():
    # Child protocols and the Host banner must not depend on the Windows OEM
    # code page inherited by a CI runner.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if os.name == "nt":
        paths = list(sys.path)
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _test_interpreter() -> str:
    return sys._base_executable if os.name == "nt" else sys.executable


def test_native_windows_test_interpreter_keeps_project_imports(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "nt")
        expected = list(sys.path)
        if os.environ.get("PYTHONPATH"):
            expected.append(os.environ["PYTHONPATH"])
        assert _child_env()["PYTHONPATH"] == os.pathsep.join(expected)


def test_shutdown_daemon_and_wait_waits_for_observed_process_exit(tmp_path, monkeypatch):
    from tianshu import daemon, execution_admission, host, leases

    lock_path = tmp_path / "daemon.lock"
    lock_path.write_text(json.dumps({"instance_id": "daemon-a", "pid": 123}), encoding="utf-8")
    monkeypatch.setattr(daemon, "lock_path", lambda _root: lock_path)
    requests = []

    def request_shutdown(*_args, **kwargs):
        requests.append(kwargs)
        return True

    monkeypatch.setattr(daemon, "request_shutdown", request_shutdown)
    monkeypatch.setattr(leases, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(host, "_unreaped_child_state", lambda _pid: "running")
    births = iter(["birth-a", "birth-a", "birth-b"])
    monkeypatch.setattr(
        execution_admission, "process_birth_identity", lambda _pid: next(births),
    )

    shutdown_daemon_and_wait(tmp_path)

    assert next(births, None) is None
    assert len(requests) == 1
    assert requests[0]["expected_instance_id"] == "daemon-a"
    assert requests[0]["timeout"] > 0


def test_shutdown_daemon_and_wait_times_out_without_signalling(tmp_path, monkeypatch):
    from tianshu import daemon, execution_admission, host, leases

    lock_path = tmp_path / "daemon.lock"
    lock_path.write_text(json.dumps({"instance_id": "daemon-a", "pid": 123}), encoding="utf-8")
    monkeypatch.setattr(daemon, "lock_path", lambda _root: lock_path)
    monkeypatch.setattr(
        daemon, "request_shutdown", lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        execution_admission, "process_birth_identity", lambda _pid: "birth-a",
    )
    monkeypatch.setattr(leases, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(host, "_unreaped_child_state", lambda _pid: "running")
    monkeypatch.setattr(
        support.threading, "Event",
        lambda: pytest.fail("an expired cleanup deadline must not keep waiting"),
    )

    with pytest.raises(AssertionError, match="原 Daemon OS 进程未退出"):
        shutdown_daemon_and_wait(tmp_path, timeout=0)


def test_shutdown_daemon_and_wait_accepts_pid_reuse_as_original_exit(tmp_path, monkeypatch):
    from tianshu import daemon, execution_admission

    lock_path = tmp_path / "daemon.lock"
    lock_path.write_text(json.dumps({"instance_id": "daemon-a", "pid": 123}), encoding="utf-8")
    monkeypatch.setattr(daemon, "lock_path", lambda _root: lock_path)
    monkeypatch.setattr(daemon, "request_shutdown", lambda *_a, **_k: True)
    births = iter(["birth-a", "birth-b"])
    monkeypatch.setattr(
        execution_admission, "process_birth_identity", lambda _pid: next(births),
    )

    shutdown_daemon_and_wait(tmp_path)

    assert next(births, None) is None


@pytest.fixture
def held_child():
    # Use the interpreter itself, not a Windows venv launcher plus its worker.
    child = subprocess.Popen(
        [sys._base_executable, "-u", "-c",
         "import os,sys; os.write(1, b'ready\\n'); sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=_child_env(),
    )
    try:
        assert child.stdout is not None and child.stdout.readline() == b"ready\n"
        yield child
    finally:
        if child.stdin is not None:
            try:
                child.stdin.close()
            except BrokenPipeError:
                pass
        try:
            child.wait(timeout=10)
        finally:
            if child.stdout is not None:
                child.stdout.close()


@pytest.mark.parametrize(
    ("argv", "windows", "bypass"),
    [
        (["python", "custom-daemon.py"], True, True),
        (["python", "custom-daemon.py"], False, False),
        (["python", "-m", "tianshu", "daemon"], False, True),
        (["custom-daemon"], True, False),
    ],
)
def test_owned_python_command_bypasses_only_a_windows_launcher(
    tmp_path, monkeypatch, argv, windows, bypass,
):
    executable = tmp_path / "venv" / "python.exe"
    base_executable = tmp_path / "base" / "python.exe"
    argv = [str(executable) if item == "python" else item for item in argv]
    with monkeypatch.context() as patch:
        patch.setattr(sys, "executable", str(executable))
        patch.setattr(sys, "_base_executable", str(base_executable))
        command, env = host._owned_process_command(argv, windows=windows)
    assert command[0] == (str(base_executable) if bypass else argv[0])
    assert command[1:] == argv[1:]
    if bypass:
        assert env is not None
        assert env["PYTHONPATH"] == os.pathsep.join(sys.path)
    else:
        assert env is None


@pytest.mark.parametrize("alias", ["dot", "parent", "relative"])
def test_owned_python_command_normalizes_a_launcher_alias(
    tmp_path, monkeypatch, alias,
):
    executable = tmp_path / "venv" / "python.exe"
    if alias == "dot":
        command = f"{executable.parent}{os.sep}.{os.sep}python.exe"
    elif alias == "parent":
        command = f"{executable.parent}{os.sep}missing{os.sep}..{os.sep}python.exe"
    else:
        monkeypatch.chdir(tmp_path)
        command = os.path.relpath(executable, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "executable", str(executable))
        patch.setattr(sys, "_base_executable", str(tmp_path / "base" / "python.exe"))
        selected, env = host._owned_process_command(
            [command, "custom-daemon.py"], windows=True,
        )
    assert selected == [str(tmp_path / "base" / "python.exe"), "custom-daemon.py"]
    assert env is not None and env["PYTHONPATH"] == os.pathsep.join(sys.path)


@pytest.mark.parametrize("configure_utf8", [True, False])
def test_direct_host_child_sets_utf8_before_chinese_failure(tmp_path, configure_utf8):
    configure = "_force_utf8_streams()" if configure_utf8 else "pass"
    script = f"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
{configure}
def fail(self):
    raise host.HostError("受控启动失败")
host.DaemonSupervisor.ensure = fail
raise SystemExit(host.run_host(
    Path(sys.argv[1]), ui_dir=Path(sys.argv[1]) / "ui",
    daemon_command=[], supervise=False,
))
"""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, "-u", "-c", script, str(tmp_path)],
        capture_output=True, env=env, timeout=20, check=False,
    )
    if configure_utf8:
        assert result.returncode == 1
        assert "启动守护进程失败：受控启动失败" in result.stderr.decode("utf-8")
    else:
        assert result.returncode == 1
        assert b"\\u542f\\u52a8" in result.stderr
        assert "启动守护进程失败" not in result.stderr.decode("utf-8")


@pytest.mark.parametrize("same_executable", [True, False])
def test_owned_python_command_checks_a_windows_path_launcher(
    tmp_path, monkeypatch, same_executable,
):
    executable = tmp_path / "venv" / "python.exe"
    resolved = executable if same_executable else tmp_path / "other" / "python.exe"
    with monkeypatch.context() as patch:
        patch.setattr(sys, "executable", str(executable))
        patch.setattr(sys, "_base_executable", str(tmp_path / "base" / "python.exe"))
        patch.setattr(host.shutil, "which", lambda name: str(resolved))
        command, env = host._owned_process_command(
            ["python", "custom-daemon.py"], windows=True,
        )
    if same_executable:
        assert command == [str(tmp_path / "base" / "python.exe"), "custom-daemon.py"]
        assert env is not None and env["PYTHONPATH"] == os.pathsep.join(sys.path)
    else:
        assert command == ["python", "custom-daemon.py"]
        assert env is None


@pytest.fixture
def ready_owned_root(tmp_path, held_child):
    from tianshu import daemon

    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    snapshot = json.dumps({
        "pid": held_child.pid,
        "started_at": 1,
        "instance_id": "owned-ready",
        "daemon_version": daemon.DAEMON_VERSION,
    }).encode("utf-8")
    supervisor._owned_snapshot = snapshot
    daemon.lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    daemon.lock_path(tmp_path).write_bytes(snapshot)
    return supervisor, snapshot


def test_stop_waits_for_owned_child_after_endpoint_is_gone(
    tmp_path, monkeypatch, held_child,
):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    original_wait = held_child.wait
    events = queue.Queue()
    terminated = []
    errors = []

    def observed_wait(*args, **kwargs):
        events.put("wait")
        return original_wait(*args, **kwargs)

    def stop():
        try:
            supervisor.stop(timeout=2)
        except BaseException as exc:
            errors.append(exc)
        finally:
            events.put("stopped")

    monkeypatch.setattr(held_child, "wait", observed_wait)
    monkeypatch.setattr(supervisor, "_terminate", lambda proc, **kw: terminated.append(proc.pid))
    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        first = events.get(timeout=10)
        assert first == "wait", "stop discarded a still-running owned child"
        assert held_child.poll() is None
        assert held_child.stdin is not None
        held_child.stdin.write(b"G")
        held_child.stdin.flush()
    finally:
        assert held_child.stdin is not None
        held_child.stdin.close()
        stopper.join(timeout=10)
    assert not stopper.is_alive() and not errors
    assert terminated == []
    assert held_child.returncode == 0
    assert supervisor._process is None


def test_stop_reaps_owned_child_after_termination(tmp_path, monkeypatch, held_child):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    terminated = []

    def terminate(proc, **kwargs):
        assert proc is held_child
        terminated.append(proc.pid)
        held_child.terminate()

    monkeypatch.setattr(supervisor, "_terminate", terminate)
    supervisor.stop(timeout=2)
    assert terminated == [held_child.pid]
    assert held_child.returncode is not None, "termination alone does not reap the child"
    assert supervisor._process is None


def test_stop_retains_owned_child_when_exit_cannot_be_confirmed(
    tmp_path, monkeypatch, held_child,
):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    terminated = []
    monkeypatch.setattr(supervisor, "_terminate", lambda proc, **kw: terminated.append(proc.pid))
    with pytest.raises(host.HostError, match="退出"):
        supervisor.stop(timeout=0.05)
    assert terminated == [held_child.pid]
    assert held_child.poll() is None
    assert supervisor._process is held_child
    assert held_child.stdin is not None
    held_child.stdin.write(b"G")
    held_child.stdin.flush()
    held_child.wait(timeout=10)
    supervisor.stop(timeout=0.05)
    assert supervisor._process is None


def test_failed_start_gracefully_reaps_ready_owned_root_and_clears_lock(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    requests = []

    def graceful_shutdown(*, reason, instance_id, timeout):
        requests.append((reason, instance_id, timeout))
        assert held_child is not None and held_child.stdin is not None
        held_child.stdin.write(b"G")
        held_child.stdin.flush()
        return True

    def forbidden_terminate(*args, **kwargs):
        raise AssertionError("graceful shutdown must not force-terminate the owned root")

    monkeypatch.setattr(supervisor, "_request_shutdown", graceful_shutdown)
    monkeypatch.setattr(held_child, "terminate", forbidden_terminate)
    supervisor._abort_start(held_child)
    assert requests and requests[0][0:2] == ("startup_abort", "owned-ready")
    assert held_child.returncode == 0
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert not daemon.lock_path(tmp_path).exists()


def test_failed_start_force_reaps_owned_root_after_graceful_failure(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, _snapshot = ready_owned_root
    held_child = supervisor._process
    requests = []

    def failed_shutdown(*, reason, instance_id, timeout):
        requests.append((reason, instance_id, timeout))
        return False

    monkeypatch.setattr(supervisor, "_request_shutdown", failed_shutdown)
    supervisor._abort_start(held_child)
    assert requests and requests[0][0:2] == ("startup_abort", "owned-ready")
    assert held_child.returncode is not None
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert not daemon.lock_path(tmp_path).exists()


def test_failed_start_preserves_owned_identity_when_lock_is_initially_replaced(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    replacement = json.dumps({
        "pid": held_child.pid + 1,
        "started_at": 2,
        "instance_id": "replacement",
        "daemon_version": daemon.DAEMON_VERSION,
    }).encode("utf-8")
    daemon.lock_path(tmp_path).write_bytes(replacement)

    def forbidden_shutdown(**kwargs):
        raise AssertionError("a replacement lock must not receive a shutdown request")

    def forbidden_external_terminate(*args, **kwargs):
        raise AssertionError("a replacement lock PID must never be signalled by command")

    real_kill = os.kill

    def checked_signal(pid, sig):
        assert pid == held_child.pid, "signalled the replacement lock PID"
        return real_kill(pid, sig)

    monkeypatch.setattr(supervisor, "_request_shutdown", forbidden_shutdown)
    monkeypatch.setattr(host.subprocess, "run", forbidden_external_terminate)
    if os.name != "nt":
        monkeypatch.setattr(host.os, "kill", checked_signal)
    with pytest.raises(host.HostError, match="锁实例已变更"):
        supervisor._abort_start(held_child)
    assert held_child.returncode is not None
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot == snapshot
    assert daemon.lock_path(tmp_path).read_bytes() == replacement

    supervisor._abort_start(held_child)
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert daemon.lock_path(tmp_path).read_bytes() == replacement


def test_failed_start_preserves_owned_identity_when_lock_replacement_is_late(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    replacement = json.dumps({
        "pid": held_child.pid + 1,
        "started_at": 2,
        "instance_id": "replacement",
        "daemon_version": daemon.DAEMON_VERSION,
    }).encode("utf-8")

    def shutdown_then_replace(*, reason, instance_id, timeout):
        assert held_child.stdin is not None
        held_child.stdin.write(b"G")
        held_child.stdin.flush()
        daemon.lock_path(tmp_path).write_bytes(replacement)
        return True

    monkeypatch.setattr(supervisor, "_request_shutdown", shutdown_then_replace)
    with pytest.raises(host.HostError, match="锁实例已变更"):
        supervisor._abort_start(held_child)
    assert held_child.returncode == 0
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot == snapshot
    assert daemon.lock_path(tmp_path).read_bytes() == replacement

    # Retry observes the replacement before cleanup and only drops the dead
    # owned handle; it must not send another request or unlink the replacement.
    supervisor._abort_start(held_child)
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert daemon.lock_path(tmp_path).read_bytes() == replacement


def test_failed_start_serializes_cleanup_with_real_lock_takeover(
    tmp_path, monkeypatch, ready_owned_root,
):
    """A real acquirer cannot replace the lock between Host read and unlink."""
    from tianshu import daemon

    supervisor, _snapshot = ready_owned_root
    held_child = supervisor._process
    lock_path = daemon.lock_path(tmp_path)
    replacement_script = r"""
import sys
from pathlib import Path
from tianshu import daemon, leases
root = Path(sys.argv[1])
original_try_lock = leases._try_lock
announced = False
def observed_try_lock(fd):
    global announced
    try:
        return original_try_lock(fd)
    except OSError:
        if not announced:
            announced = True
            print("blocked", flush=True)
        raise
leases._try_lock = observed_try_lock
print("ready", flush=True)
assert sys.stdin.buffer.read(1) == b"G"
lock = None
try:
    lock = daemon.acquire_lock(root)
    print("acquired " + lock.instance_id, flush=True)
    sys.stdin.buffer.read(1)
finally:
    if lock is not None:
        daemon._unlink_lock(lock)
"""
    replacement = subprocess.Popen(
        [sys._base_executable, "-u", "-c", replacement_script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**_child_env(), "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    lines: queue.Queue[str] = queue.Queue()

    def read_lines():
        assert replacement.stdout is not None
        for raw in replacement.stdout:
            lines.put(raw.decode("utf-8").strip())

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=15) == "ready"
        original_read_bytes = Path.read_bytes
        lock_reads = 0
        takeover_observation = []

        def graceful_shutdown(*, reason, instance_id, timeout):
            assert held_child is not None and held_child.stdin is not None
            held_child.stdin.write(b"G")
            held_child.stdin.flush()
            return True

        def read_then_release_acquirer(path):
            nonlocal lock_reads
            raw = original_read_bytes(path)
            if path == lock_path:
                lock_reads += 1
                if lock_reads == 2:
                    # Host now owns daemon.lock.guard. The fixed acquirer must
                    # report that it is blocked; an unguarded takeover reports
                    # "acquired" here and makes the assertion below fail.
                    assert replacement.stdin is not None
                    replacement.stdin.write(b"G")
                    replacement.stdin.flush()
                    takeover_observation.append(lines.get(timeout=15))
            return raw

        monkeypatch.setattr(supervisor, "_request_shutdown", graceful_shutdown)
        monkeypatch.setattr(Path, "read_bytes", read_then_release_acquirer)
        try:
            supervisor._abort_start(held_child)
        finally:
            monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
        assert takeover_observation == ["blocked"]
        assert not lock_path.exists()

        # Once Host releases the guard, the same real acquirer may create its lock.
        acquired = lines.get(timeout=15)
        assert acquired.startswith("acquired "), acquired
        replacement_id = acquired.removeprefix("acquired ")
        assert json.loads(lock_path.read_bytes())["instance_id"] == replacement_id
        assert replacement.stdin is not None
        replacement.stdin.write(b"X")
        replacement.stdin.flush()
        assert replacement.wait(timeout=15) == 0
        assert not lock_path.exists()
        assert supervisor._process is None
        assert supervisor._owned_snapshot is None
    finally:
        if replacement.stdin is not None:
            replacement.stdin.close()
        if replacement.poll() is None:
            replacement.kill()
        replacement.wait(timeout=15)
        if replacement.stdout is not None:
            replacement.stdout.close()
        reader.join(timeout=5)


def test_failed_start_retains_owned_identity_after_lock_cleanup_error(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    lock_path = daemon.lock_path(tmp_path)
    original_unlink = Path.unlink

    def graceful_shutdown(*, reason, instance_id, timeout):
        assert held_child.stdin is not None
        held_child.stdin.write(b"G")
        held_child.stdin.flush()
        return True

    def controlled_unlink(path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("controlled lock cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(supervisor, "_request_shutdown", graceful_shutdown)
    monkeypatch.setattr(Path, "unlink", controlled_unlink)
    with pytest.raises(host.HostError, match="清理原实例锁失败"):
        supervisor._abort_start(held_child)
    assert held_child.returncode == 0
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot == snapshot
    assert lock_path.read_bytes() == snapshot

    monkeypatch.setattr(Path, "unlink", original_unlink)
    supervisor._abort_start(held_child)
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert not lock_path.exists()


def test_failed_start_retains_owned_identity_after_lock_read_error(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    lock_path = daemon.lock_path(tmp_path)
    original_read_bytes = Path.read_bytes

    def controlled_read(path):
        if path == lock_path:
            raise PermissionError("controlled lock read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", controlled_read)
    with pytest.raises(host.HostError, match="无法核验其锁"):
        supervisor._abort_start(held_child)
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    assert held_child.returncode is not None
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot == snapshot
    assert lock_path.read_bytes() == snapshot

    supervisor._abort_start(held_child)
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert not lock_path.exists()


def test_stop_never_signals_an_already_reaped_child(tmp_path, monkeypatch, held_child):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    assert held_child.stdin is not None
    held_child.stdin.write(b"G")
    held_child.stdin.flush()
    assert held_child.wait(timeout=10) == 0

    def forbidden(pid):
        raise AssertionError(f"signalled an already reaped process: {pid}")

    monkeypatch.setattr(supervisor, "_terminate", forbidden)
    supervisor.stop(timeout=0.05)
    assert supervisor._process is None


def test_stop_does_not_signal_a_child_that_exits_at_the_wait_boundary(
    tmp_path, monkeypatch, held_child,
):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    real_wait = held_child.wait
    waits = 0

    def exit_then_report_timeout(*args, **kwargs):
        nonlocal waits
        waits += 1
        if waits == 1:
            assert held_child.stdin is not None
            held_child.stdin.write(b"G")
            held_child.stdin.flush()
            assert real_wait(timeout=10) == 0
            raise subprocess.TimeoutExpired("owned-daemon", 0.01)
        return real_wait(*args, **kwargs)

    def forbidden_signal(*args, **kwargs):
        raise AssertionError("a reaped child PID must never be signalled")

    monkeypatch.setattr(held_child, "wait", exit_then_report_timeout)
    monkeypatch.setattr(host.os, "kill", forbidden_signal)
    supervisor.stop(timeout=0.01)
    assert waits >= 2
    assert held_child.returncode == 0
    assert supervisor._process is None


def test_host_exit_does_not_bypass_owned_process_lifecycle_lock():
    import ast

    tree = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
    bypasses = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"poll", "wait", "terminate", "kill"}:
            continue
        owner = node.func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "_process":
            bypasses.append((node.lineno, node.func.attr))
    assert not bypasses, f"owned process lifecycle bypasses: {bypasses}"


def test_owned_process_lifecycle_serializes_poll_wait_and_terminate(tmp_path, monkeypatch):
    """No owned wait/poll can run concurrently with termination's identity check."""
    supervisor = host.DaemonSupervisor(tmp_path, [])
    release = threading.Event()
    operation_started = threading.Event()
    terminate_entered = threading.Event()
    poll_entered = threading.Event()
    errors = []

    class OwnedProcess:
        returncode = None
        pid = 4321

        def poll(self):
            poll_entered.set()
            return None

        def wait(self, timeout=None):
            operation_started.set()
            assert release.wait(timeout=10)
            return 0

    proc = OwnedProcess()

    def terminate_owned(*args, **kwargs):
        terminate_entered.set()

    def run(operation):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(supervisor, "_terminate", terminate_owned)
    waiter = threading.Thread(
        target=run, args=(lambda: supervisor._wait_owned(proc, timeout=10),),
    )
    waiter.start()
    threads = [waiter]
    try:
        assert operation_started.wait(timeout=10), "owned wait did not start"
        terminator = threading.Thread(
            target=run, args=(lambda: supervisor._terminate_owned(proc),),
        )
        poller = threading.Thread(
            target=run, args=(lambda: supervisor._poll_owned(proc),),
        )
        threads.extend((terminator, poller))
        terminator.start()
        poller.start()
        assert not terminate_entered.wait(
            timeout=0.05
        ), "terminate bypassed the process lifecycle lock"
        assert not poll_entered.wait(
            timeout=0.05
        ), "poll bypassed the process lifecycle lock"
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert terminate_entered.is_set() and poll_entered.is_set()


def test_stop_force_reaps_owned_root_and_clears_exact_lock(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    lock_path = daemon.lock_path(tmp_path)

    def force_exit(proc, *, force=False):
        assert proc is held_child
        os.kill(proc.pid, 9)
        return {"outcome": "signal_sent"}

    monkeypatch.setattr(supervisor, "_terminate", force_exit)
    supervisor.stop(timeout=0.01)
    assert held_child.returncode is not None
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None
    assert not lock_path.exists()


def test_stop_fails_closed_when_replacement_appears_during_forced_cleanup(
    tmp_path, monkeypatch, ready_owned_root,
):
    from contextlib import contextmanager

    from tianshu import daemon

    supervisor, _snapshot = ready_owned_root
    held_child = supervisor._process
    lock_path = daemon.lock_path(tmp_path)
    replacement = json.dumps({
        "pid": held_child.pid + 1,
        "started_at": 2,
        "instance_id": "replacement-during-force-cleanup",
        "daemon_version": daemon.DAEMON_VERSION,
    }).encode("utf-8")

    def failed_shutdown(**kwargs):
        return False

    def force_exit(proc, *, force=False):
        assert proc is held_child
        os.kill(proc.pid, 9)
        return {"outcome": "signal_sent"}

    real_guard = daemon.daemon_lock_guard

    @contextmanager
    def replacing_guard(*args, **kwargs):
        with real_guard(*args, **kwargs):
            lock_path.write_bytes(replacement)
            yield

    monkeypatch.setattr(supervisor, "_request_shutdown", failed_shutdown)
    monkeypatch.setattr(supervisor, "_terminate", force_exit)
    monkeypatch.setattr(daemon, "daemon_lock_guard", replacing_guard)
    with pytest.raises(host.HostError, match="锁实例已变更"):
        supervisor.stop(timeout=0.01)
    assert held_child.returncode is not None
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot is not None
    assert lock_path.read_bytes() == replacement


@pytest.mark.skipif(os.name == "nt", reason="native POSIX WNOWAIT child observation")
def test_owned_termination_detects_an_unreaped_exit_without_signalling(
    tmp_path, monkeypatch, held_child,
):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    assert held_child.stdin is not None
    held_child.stdin.write(b"G")
    held_child.stdin.flush()
    deadline = time.monotonic() + 10
    while host._unreaped_child_state(held_child.pid) != "exited":
        assert time.monotonic() < deadline, "child did not exit"
        threading.Event().wait(0.01)
    assert held_child.returncode is None, "identity check must not reap the child"

    def forbidden_signal(*args, **kwargs):
        raise AssertionError("an exited unreaped child must not be signalled")

    monkeypatch.setattr(host.os, "kill", forbidden_signal)
    assert supervisor._terminate(held_child) == {"outcome": "not_running"}
    assert held_child.wait(timeout=10) == 0


def test_owned_termination_treats_waitid_without_status_as_running(monkeypatch):
    signals = []
    waitid_os = SimpleNamespace(
        name="posix",
        P_PID=getattr(os, "P_PID", 1),
        WEXITED=getattr(os, "WEXITED", 4),
        WNOHANG=getattr(os, "WNOHANG", 8),
        WNOWAIT=getattr(os, "WNOWAIT", 0),
        waitid=lambda *args: None,
        kill=lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(host, "os", waitid_os)

    class RunningProcess:
        pid = 123
        returncode = None

    assert host.DaemonSupervisor._terminate(RunningProcess()) == {
        "outcome": "signal_sent",
    }
    assert signals == [(123, host.signal.SIGTERM)]


def test_windows_owned_termination_uses_the_retained_process_handle(monkeypatch):
    import ctypes

    calls = []

    class Terminate:
        argtypes = None
        restype = None

        def __call__(self, handle, exit_code):
            calls.append((handle, exit_code))
            return 1

    kernel32 = SimpleNamespace(TerminateProcess=Terminate())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    monkeypatch.setattr(host, "os", SimpleNamespace(name="nt"))

    class OwnedProcess:
        returncode = None
        _handle = 4321

        @property
        def pid(self):
            raise AssertionError("Windows termination must not use the reusable PID")

    assert host.DaemonSupervisor._terminate(OwnedProcess()) == {
        "outcome": "process_terminated",
    }
    assert calls == [(4321, 1)]


def test_windows_owned_termination_fails_closed_without_a_handle(monkeypatch):
    import ctypes

    def forbidden_dll(*args, **kwargs):
        raise AssertionError("a missing process handle must not trigger a PID fallback")

    monkeypatch.setattr(ctypes, "WinDLL", forbidden_dll, raising=False)
    monkeypatch.setattr(host, "os", SimpleNamespace(name="nt"))

    class OwnedProcess:
        returncode = None
        _handle = None

        @property
        def pid(self):
            raise AssertionError("Windows termination must not use the reusable PID")

    assert host.DaemonSupervisor._terminate(OwnedProcess()) == {
        "outcome": "handle_unavailable",
    }


def test_windows_owned_termination_failure_has_no_command_or_pid_fallback(monkeypatch):
    import ctypes

    class Terminate:
        argtypes = None
        restype = None

        def __call__(self, handle, exit_code):
            assert (handle, exit_code) == (4321, 1)
            return 0

    kernel32 = SimpleNamespace(
        TerminateProcess=Terminate(), GetLastError=lambda: 5,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(host, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        host.subprocess, "run",
        lambda *a, **kw: pytest.fail("Windows termination must not invoke taskkill"),
    )

    class OwnedProcess:
        returncode = None
        _handle = 4321

        @property
        def pid(self):
            raise AssertionError("Windows termination must not use the reusable PID")

    assert host.DaemonSupervisor._terminate(OwnedProcess()) == {
        "outcome": "os_error", "errno": 5,
    }


def test_attached_escalation_never_signals_a_bare_pid(tmp_path, monkeypatch, held_child):
    from tianshu import ipc

    lock = ipc.run_dir(tmp_path) / "daemon.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({
        "pid": held_child.pid, "started_at": 1, "instance_id": "attached-owner",
    }))
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._attached_snapshot = lock.read_bytes()
    clock = [0.0]
    monkeypatch.setattr(supervisor, "_request_shutdown", lambda *a, **kw: True)
    monkeypatch.setattr(host.ipc, "probe_result", lambda *a, **kw: ipc.ProbeResult(
        True, "controlled-endpoint", "still serving", None, True,
    ))
    monkeypatch.setattr(host, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        monotonic_ns=lambda: round(clock[0] * 1e9),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))

    def forbidden(*args, **kwargs):
        raise AssertionError("an attached PID is not an owned process handle")

    monkeypatch.setattr(supervisor, "_terminate", forbidden)
    monkeypatch.setattr(host.os, "kill", forbidden)
    with pytest.raises(host.HostError, match="退出"):
        supervisor.stop(timeout=0.2)
    assert held_child.poll() is None
    assert lock.read_text() == supervisor._attached_snapshot.decode()


def test_normal_ensure_retry_finishes_retained_lock_cleanup_before_spawn(
    tmp_path, monkeypatch, ready_owned_root,
):
    from tianshu import daemon

    supervisor, snapshot = ready_owned_root
    held_child = supervisor._process
    lock_path = daemon.lock_path(tmp_path)
    real_unlink = Path.unlink

    def graceful_shutdown(*, reason, instance_id, timeout):
        assert held_child is not None and held_child.stdin is not None
        held_child.stdin.write(b"G")
        held_child.stdin.flush()
        return True

    def failed_unlink(path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("controlled lock cleanup failure")
        return real_unlink(path, *args, **kwargs)

    class SpawnAttempted(Exception):
        pass

    monkeypatch.setattr(supervisor, "alive", lambda: False)
    monkeypatch.setattr(supervisor, "_request_shutdown", graceful_shutdown)
    monkeypatch.setattr(supervisor, "_open_log", lambda: None)
    monkeypatch.setattr(Path, "unlink", failed_unlink)
    with pytest.raises(host.HostError, match="清理原实例锁失败"):
        supervisor._abort_start(held_child)
    assert held_child.returncode == 0
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot == snapshot

    monkeypatch.setattr(Path, "unlink", real_unlink)

    def checked_spawn(*args, **kwargs):
        assert not lock_path.exists(), "retry spawned before old lock cleanup"
        assert supervisor._process is None
        assert supervisor._owned_snapshot is None
        raise SpawnAttempted

    monkeypatch.setattr(host.subprocess, "Popen", checked_spawn)
    with pytest.raises(SpawnAttempted):
        supervisor.ensure()
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None


def test_failed_readiness_retains_lock_cleanup_evidence_for_retry(
    tmp_path, monkeypatch, held_child,
):
    from tianshu import daemon

    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    lock_path = daemon.lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = json.dumps({
        "pid": held_child.pid,
        "started_at": 1,
        "instance_id": "not-ready-yet",
        "daemon_version": daemon.DAEMON_VERSION,
    }).encode("utf-8")
    lock_path.write_bytes(snapshot)
    real_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("controlled pre-readiness lock cleanup failure")
        return real_unlink(path, *args, **kwargs)

    class SpawnAttempted(Exception):
        pass

    monkeypatch.setattr(Path, "unlink", failed_unlink)
    with pytest.raises(host.HostError, match="清理原实例锁失败"):
        supervisor._abort_start(held_child)
    assert held_child.returncode is not None
    assert supervisor._process is held_child
    assert supervisor._owned_snapshot is None
    assert lock_path.read_bytes() == snapshot

    monkeypatch.setattr(Path, "unlink", real_unlink)

    def checked_spawn(*args, **kwargs):
        assert not lock_path.exists(), "retry spawned before pre-readiness lock cleanup"
        assert supervisor._process is None
        assert supervisor._owned_snapshot is None
        raise SpawnAttempted

    monkeypatch.setattr(supervisor, "alive", lambda: False)
    monkeypatch.setattr(host.subprocess, "Popen", checked_spawn)
    with pytest.raises(SpawnAttempted):
        supervisor.ensure()
    assert supervisor._process is None
    assert supervisor._owned_snapshot is None


def test_host_keeps_its_identity_and_accepts_retry_after_a_failed_stop():
    script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
original = host.DaemonSupervisor.stop
def fail_after_real_stop(self, *args, **kwargs):
    original(self, *args, **kwargs)
    if not (root / "failed").exists():
        (root / "failed").touch()
        raise host.HostError("CONTROLLED_STOP_UNCONFIRMED")
host.DaemonSupervisor.stop = fail_after_real_stop
root = Path(sys.argv[1])
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    supervise=False,
))
"""
    # AF_UNIX paths must stay short even in deeply nested pytest basetemps.
    with tempfile.TemporaryDirectory(
        prefix="tsh-exit-", dir=None if os.name == "nt" else "/tmp",
    ) as d:
        root = Path(d)
        with (root / "stderr.log").open("wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c", script, str(root)],
                stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
            )
        try:
            deadline = time.monotonic() + 20
            while True:
                assert proc.poll() is None
                info = host.read_host_info(root)
                if info is not None and info.port and host._http_alive(info):
                    break
                assert time.monotonic() < deadline, "host never became ready"
                threading.Event().wait(0.01)
            assert host.stop_host(root, timeout=2) == 1
            assert (root / "failed").exists()
            assert proc.poll() is None
            assert host.read_host_info(root) == info
            assert host._http_alive(info)
            assert "CONTROLLED_STOP_UNCONFIRMED" in (
                root / "stderr.log"
            ).read_bytes().decode("utf-8")
            assert host.stop_host(root, timeout=10) == 0
            assert proc.wait(timeout=15) == 0
            assert not host.host_lock_path(root).exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_stop_records_owned_process_wait_facts(tmp_path, held_child, capsys, monkeypatch):
    monkeypatch.setenv("TIANSHU_HOST_PROCESS_DIAGNOSTICS", "1")
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    assert held_child.stdin is not None
    held_child.stdin.write(b"G")
    held_child.stdin.flush()
    supervisor.stop(timeout=2)
    records = [
        json.loads(line)["host_supervisor_process"]
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"host_supervisor_process":')
    ]
    assert records, "the owning supervisor must expose its actual wait result"
    assert records[-1]["pid"] == held_child.pid
    assert records[-1]["returncode"] == 0
    assert any(
        record["action"] == "wait_exited" and record["returncode"] == 0
        for record in records
    ), records
    assert type(records[-1]["monotonic_ns"]) is int


def test_stop_failure_restores_monitor_without_unconfirmed_shutdown(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    monkeypatch.setattr(supervisor, "alive", lambda: True)
    supervisor.start_monitor()
    try:
        assert supervisor._thread is not None and supervisor._thread.is_alive()

        def fail_shutdown(**_kwargs):
            supervisor._shutdown_unconfirmed = False
            raise host.HostError("controlled stop failure")

        with monkeypatch.context() as patch:
            patch.setattr(supervisor, "_shutdown_daemon", fail_shutdown)
            with pytest.raises(host.HostError, match="controlled stop failure"):
                supervisor.stop(timeout=0.1)
        assert not supervisor._stop.is_set()
        assert supervisor._thread.is_alive()
    finally:
        supervisor.stop_monitor()


def test_stop_lock_contention_does_not_cancel_monitor(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    monkeypatch.setattr(supervisor, "alive", lambda: True)
    supervisor.start_monitor()
    try:
        assert supervisor._stop_lock.acquire(blocking=False)
        try:
            with pytest.raises(host.HostError, match="停止核验"):
                supervisor.stop(timeout=0.01)
        finally:
            supervisor._stop_lock.release()
        assert not supervisor._stop.is_set()
        assert supervisor._thread is not None and supervisor._thread.is_alive()
    finally:
        supervisor.stop_monitor()


def test_spawn_and_wait_does_not_start_after_stop(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(tmp_path, [sys._base_executable, "-c", "pass"])
    supervisor._stop.set()
    opened = []
    monkeypatch.setattr(supervisor, "_open_log", lambda: opened.append(True))
    supervisor._spawn_and_wait()
    assert not opened
    assert supervisor._process is None


def test_monitor_loop_rejects_stale_generation_before_waiting(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._monitor_generation = 1

    def forbidden_wait(_timeout):
        raise AssertionError("过期监控代在等待前未退出")

    monkeypatch.setattr(supervisor._stop, "wait", forbidden_wait)
    supervisor._monitor_loop(0)


def test_monitor_loop_rejects_generation_change_during_wait(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    alive_calls = []

    def replace_generation(_timeout):
        supervisor._monitor_generation = 1
        return False

    monkeypatch.setattr(supervisor._stop, "wait", replace_generation)
    monkeypatch.setattr(supervisor, "alive", lambda: alive_calls.append(True) or False)
    supervisor._monitor_loop(0)
    assert not alive_calls


def test_resume_monitor_replaces_a_live_old_generation(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    entered = threading.Event()
    release = threading.Event()
    alive = threading.Event()

    def held_spawn():
        entered.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr(supervisor, "alive", alive.is_set)
    monkeypatch.setattr(supervisor, "_spawn_and_wait", held_spawn)
    monkeypatch.setattr(host, "RESTART_BACKOFF_S", (0,))
    supervisor.start_monitor()
    old = supervisor._thread
    try:
        assert old is not None and entered.wait(timeout=10)
        alive.set()
        supervisor._resume_monitor()
        current = supervisor._thread
        assert current is not None and current is not old and current.is_alive()
        release.set()
        old.join(timeout=10)
        assert not old.is_alive()
        assert supervisor._thread is current and current.is_alive()
    finally:
        release.set()
        supervisor.stop_monitor()


def test_stop_monitor_waits_for_inflight_popen_before_stopping(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(
        tmp_path, [sys._base_executable, "-u", "-c", "pass"],
    )
    entered = threading.Event()
    release = threading.Event()
    popen = host.subprocess.Popen

    def paused_popen(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return popen(*args, **kwargs)

    monkeypatch.setattr(host.subprocess, "Popen", paused_popen)
    supervisor.start_monitor()
    errors = []

    def stop_monitor():
        try:
            supervisor.stop_monitor()
        except BaseException as exc:
            errors.append(exc)

    stopper = threading.Thread(target=stop_monitor)
    try:
        assert entered.wait(timeout=10)
        stopper.start()
        stopper.join(timeout=0.05)
        assert stopper.is_alive()
        assert not supervisor._stop.is_set()
        release.set()
        stopper.join(timeout=10)
        assert not stopper.is_alive() and not errors
        assert supervisor._stop.is_set()
        assert supervisor._thread is not None and not supervisor._thread.is_alive()
    finally:
        release.set()
        stopper.join(timeout=10)
        if supervisor._thread is not None:
            supervisor._thread.join(timeout=10)
        if supervisor._process is not None:
            supervisor._process.wait(timeout=10)
        supervisor._close_log()


def test_spawn_rechecks_stop_before_popen(tmp_path, monkeypatch):
    supervisor = host.DaemonSupervisor(
        tmp_path, [sys._base_executable, "-u", "-c", "pass"],
    )
    entered = threading.Event()
    release = threading.Event()
    open_log = supervisor._open_log
    popen_calls = []

    def paused_open_log():
        entered.set()
        assert release.wait(timeout=10)
        open_log()

    def forbidden_popen(*_args, **_kwargs):
        popen_calls.append(True)
        raise AssertionError("Popen started after the final stop check")

    monkeypatch.setattr(supervisor, "_open_log", paused_open_log)
    monkeypatch.setattr(host.subprocess, "Popen", forbidden_popen)
    supervisor.start_monitor()
    try:
        assert entered.wait(timeout=10)
        supervisor._stop.set()
        release.set()
        assert supervisor._thread is not None
        supervisor._thread.join(timeout=10)
        assert not supervisor._thread.is_alive()
        assert not popen_calls
        assert supervisor._process is None
    finally:
        release.set()
        if supervisor._thread is not None:
            supervisor._thread.join(timeout=10)
        supervisor._close_log()


def test_stop_confirms_monitor_exit_before_returning(tmp_path, monkeypatch):
    script = (
        "import sys,threading; from pathlib import Path; root=Path(sys.argv[1]); "
        "(root/'ready').touch()\n"
        "while not (root/'release').exists(): threading.Event().wait(0.01)\n"
    )
    supervisor = host.DaemonSupervisor(
        tmp_path, [sys._base_executable, "-u", "-c", script, str(tmp_path)],
    )
    entered = threading.Event()
    release = threading.Event()
    spawn = supervisor._spawn_and_wait

    def held_spawn():
        entered.set()
        assert release.wait(timeout=10)
        spawn()

    monkeypatch.setattr(supervisor, "alive", (tmp_path / "ready").exists)
    monkeypatch.setattr(host.ipc, "probe_result", lambda *a, **kw: host.ipc.ProbeResult(
        False, "controlled-endpoint", "absent", errno.ENOENT, False,
    ))
    monkeypatch.setattr(supervisor, "_spawn_and_wait", held_spawn)
    monkeypatch.setattr(host, "RESTART_BACKOFF_S", (0,))
    supervisor.start_monitor()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(host.HostError, match="监控"):
            supervisor.stop(timeout=0.1)
    finally:
        supervisor._stop.set()
        release.set()
        assert supervisor._thread is not None
        supervisor._thread.join(timeout=10)
        supervisor._close_log()
    assert not supervisor._thread.is_alive()
    assert not (tmp_path / "ready").exists()
    assert supervisor._process is None


def test_monitor_cannot_spawn_after_stop_was_requested(tmp_path, monkeypatch):
    script = (
        "import sys,threading; from pathlib import Path; root=Path(sys.argv[1]); "
        "(root/'ready').touch()\n"
        "while not (root/'release').exists(): threading.Event().wait(0.01)\n"
    )
    supervisor = host.DaemonSupervisor(
        tmp_path, [sys._base_executable, "-u", "-c", script, str(tmp_path)],
    )
    entered = threading.Event()
    release = threading.Event()
    spawn = supervisor._spawn_and_wait

    def held_spawn():
        entered.set()
        assert release.wait(timeout=10)
        spawn()

    monkeypatch.setattr(supervisor, "alive", (tmp_path / "ready").exists)
    monkeypatch.setattr(host.ipc, "probe_result", lambda *a, **kw: host.ipc.ProbeResult(
        False, "controlled-endpoint", "absent", errno.ENOENT, False,
    ))
    monkeypatch.setattr(supervisor, "_spawn_and_wait", held_spawn)
    monkeypatch.setattr(host, "RESTART_BACKOFF_S", (0,))
    supervisor.start_monitor()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(host.HostError, match="监控"):
            supervisor.stop_monitor()
    finally:
        release.set()
        assert supervisor._thread is not None
        supervisor._thread.join(timeout=10)
        (tmp_path / "release").touch()
        if supervisor._process is not None:
            supervisor._process.wait(timeout=10)
        supervisor._close_log()
    assert not supervisor._thread.is_alive()
    assert not (tmp_path / "ready").exists(), "monitor spawned a child after stop"
    assert supervisor._process is None
    supervisor.stop(timeout=0.1)


def test_stop_preserves_the_full_grace_period(tmp_path, monkeypatch, held_child):
    from tianshu import ipc

    clock = [0.0]
    lock = ipc.run_dir(tmp_path) / "daemon.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({
        "pid": held_child.pid, "started_at": 1, "instance_id": "grace-owner",
    }))
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    supervisor._owned_snapshot = lock.read_bytes()
    real_wait = held_child.wait
    monkeypatch.setattr(supervisor, "_request_shutdown", lambda *a, **kw: True)
    monkeypatch.setattr(host.ipc, "probe_result", lambda *a, **kw: ipc.ProbeResult(
        clock[0] < 3, "controlled-endpoint", "clock", None if clock[0] < 3 else errno.ENOENT,
        clock[0] < 3,
    ))
    monkeypatch.setattr(host, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        monotonic_ns=lambda: round(clock[0] * 1e9),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))

    def terminate(proc, **kwargs):
        raise AssertionError(f"graceful cleanup was interrupted at {clock[0]}s")

    def wait(*args, **kwargs):
        assert clock[0] >= 3
        if held_child.returncode is None:
            assert held_child.stdin is not None
            held_child.stdin.write(b"G")
            held_child.stdin.flush()
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_terminate", terminate)
    monkeypatch.setattr(held_child, "wait", wait)
    try:
        supervisor.stop(timeout=5)
        assert held_child.returncode == 0 and supervisor._process is None
    finally:
        monkeypatch.setattr(held_child, "wait", real_wait)


def test_reused_daemon_cannot_report_success_after_shutdown_timeout(
    tmp_path, monkeypatch, held_child,
):
    from tianshu import ipc

    clock = [0.0]
    lock = ipc.run_dir(tmp_path) / "daemon.lock"
    lock.parent.mkdir(parents=True)
    snapshot = json.dumps({
        "pid": held_child.pid, "started_at": 1, "instance_id": "attached-owner",
    })
    lock.write_text(snapshot)
    supervisor = host.DaemonSupervisor(tmp_path, [])
    requests = []

    def request(*args, **kwargs):
        requests.append((args, kwargs))
        return True

    def forbidden(*args, **kwargs):
        raise AssertionError("an attached Daemon must never be force-signalled by PID")

    monkeypatch.setattr(supervisor, "_request_shutdown", request)
    monkeypatch.setattr(supervisor, "_terminate", forbidden)
    monkeypatch.setattr(host.ipc, "probe_result", lambda *a, **kw: ipc.ProbeResult(
        True, "controlled-endpoint", "still serving", None, True,
    ))
    monkeypatch.setattr(host, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        monotonic_ns=lambda: round(clock[0] * 1e9),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))
    with pytest.raises(host.HostError, match="退出"):
        supervisor.stop(timeout=0.2)
    assert len(requests) == 1
    assert held_child.poll() is None and lock.read_text() == snapshot
    replacement = json.dumps({
        "pid": os.getpid(), "started_at": 2, "instance_id": "replacement-owner",
    })
    lock.write_text(replacement)

    def forbidden_request(*args, **kwargs):
        raise AssertionError("a retry sent shutdown to the replacement owner")

    monkeypatch.setattr(supervisor, "_request_shutdown", forbidden_request)
    with pytest.raises(host.HostError, match="退出"):
        supervisor.stop(timeout=0.2)
    assert len(requests) == 1
    assert held_child.poll() is None and lock.read_text() == replacement


def test_diagnostic_write_failure_cannot_erase_a_confirmed_exit(
    tmp_path, monkeypatch, held_child,
):
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._process = held_child
    assert held_child.stdin is not None
    held_child.stdin.write(b"G")
    held_child.stdin.flush()
    assert held_child.wait(timeout=10) == 0
    monkeypatch.setenv("TIANSHU_HOST_PROCESS_DIAGNOSTICS", "1")

    class ClosedSink:
        def write(self, text):
            raise BrokenPipeError("controlled diagnostics sink failure")

    monkeypatch.setattr(host.sys, "stderr", ClosedSink())
    supervisor.stop(timeout=0.1)
    assert supervisor._process is None
    assert supervisor.last_error == "process_diagnostics_unavailable"


def test_attachment_rejects_a_lock_replaced_after_the_real_handshake():
    """A welcome from A must never authorize adopting the later lock of B."""
    from tianshu import daemon, ipc

    script = r"""
import sys,threading,time
from pathlib import Path
from tianshu import host,ipc
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
host._DAEMON_HELPER_CODE = r'''
import json,sys,threading,time
payload = json.load(sys.stdin)
sys.path[:] = payload.pop("sys_path")
from pathlib import Path
from tianshu import ipc,host
root = Path(payload["data_root"])
connect = ipc.Client.connect
def paused(self, **kwargs):
    welcome = connect(self, **kwargs)
    (root / "handshake").write_text(welcome["instance_id"])
    deadline = time.monotonic() + 20
    while not (root / "release").exists():
        if time.monotonic() >= deadline:
            raise AssertionError("handshake barrier was not released")
        threading.Event().wait(0.01)
    return welcome
ipc.Client.connect = paused
host._daemon_helper(payload)
'''
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui", daemon_command=[], supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-bind-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        children = []
        proc = None

        def wait_until(ready):
            deadline = time.monotonic() + 20
            while not ready():
                assert time.monotonic() < deadline, "barrier was not reached"
                threading.Event().wait(0.01)

        def start_daemon():
            child = subprocess.Popen(
                [sys.executable, "-m", "tianshu", "daemon", "--model", "scripted",
                 "--data-root", str(root)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(),
            )
            children.append(child)
            wait_until(lambda: child.poll() is not None or ipc.probe(root, timeout=0.1))
            assert child.poll() is None
            return child, json.loads(daemon.lock_path(root).read_bytes())

        try:
            original, owner = start_daemon()
            with (root / "stderr.log").open("wb") as stderr:
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-c", script, str(root)],
                    stdout=subprocess.DEVNULL, stderr=stderr, env=_child_env(),
                )
            wait_until(lambda: (root / "handshake").exists() or proc.poll() is not None)
            assert (root / "handshake").exists(), "Host did not bind a real handshake"
            assert (root / "handshake").read_text() == owner["instance_id"]
            assert daemon.request_shutdown(root, expected_instance_id=owner["instance_id"])
            assert original.wait(timeout=10) == 0
            replacement, new_owner = start_daemon()
            assert new_owner["instance_id"] != owner["instance_id"]
            (root / "release").touch()
            assert proc.wait(timeout=10) == 1
            assert host.read_host_info(root) is None
            assert replacement.poll() is None and ipc.probe(root, timeout=0.1)
            assert json.loads(daemon.lock_path(root).read_bytes()) == new_owner
        finally:
            (root / "release").touch()
            try:
                current = json.loads(daemon.lock_path(root).read_bytes())
            except FileNotFoundError:
                current = None
            if current is not None:
                daemon.request_shutdown(root, expected_instance_id=current["instance_id"])
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_owned_supervisor_does_not_adopt_an_endpoint_replaced_before_stop(monkeypatch):
    from tianshu import daemon, ipc

    with tempfile.TemporaryDirectory(
        prefix="tsh-own-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        command = [
            sys.executable, "-m", "tianshu", "daemon", "--model", "scripted",
            "--data-root", str(root),
        ]
        supervisor = host.DaemonSupervisor(root, command)
        replacement = None
        owned = None
        try:
            supervisor.ensure()
            owned = supervisor._process
            assert owned is not None
            owner = json.loads(daemon.lock_path(root).read_bytes())
            assert daemon.request_shutdown(root, expected_instance_id=owner["instance_id"])
            assert owned.wait(timeout=10) == 0
            replacement = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(),
            )
            deadline = time.monotonic() + 20
            while not ipc.probe(root, timeout=0.1):
                assert replacement.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.01)
            new_owner = daemon.lock_path(root).read_bytes()

            def forbidden(*args, **kwargs):
                raise AssertionError("shutdown was sent to a replacement endpoint")

            with monkeypatch.context() as patch:
                patch.setattr(supervisor, "_request_shutdown", forbidden)
                patch.setattr(supervisor, "_terminate", forbidden)
                supervisor.stop(timeout=0.2)
            assert supervisor._process is None
            assert replacement.poll() is None and ipc.probe(root, timeout=0.1)
            assert daemon.lock_path(root).read_bytes() == new_owner
        finally:
            try:
                owner = json.loads(daemon.lock_path(root).read_bytes())
            except FileNotFoundError:
                owner = None
            if owner is not None:
                daemon.request_shutdown(root, expected_instance_id=owner["instance_id"])
            for child in (owned, replacement):
                if child is not None:
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
            supervisor._close_log()


def test_attachment_authentication_stall_is_bounded_and_reaped():
    from tianshu import ipc

    server_script = r"""
import sys
from pathlib import Path
from multiprocessing.connection import Listener
from tianshu import ipc
root = Path(sys.argv[1])
ipc.load_authkey(root)
listener = Listener(ipc.endpoint_path(root), family=ipc._family(), authkey=None)
import os
os.write(1, b"ready\n")
connections = []
while True:
    connections.append(listener.accept())
"""
    host_script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
children = []
popen = host.subprocess.Popen
def observed(*args, **kwargs):
    child = popen(*args, **kwargs)
    children.append(child)
    return child
host.subprocess.Popen = observed
result = host.run_host(
    root, ui_dir=root / "ui", daemon_command=[], supervise=False, start_timeout_s=0.3,
)
assert children and all(child.poll() is not None for child in children), "helper was not reaped"
raise SystemExit(result)
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-auth-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        server = subprocess.Popen(
            [sys.executable, "-u", "-c", server_script, str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env(),
        )
        proc = None
        try:
            assert server.stdout is not None
            assert server.stdout.readline() in {b"ready\n", b"ready\r\n"}
            assert ipc.probe(root, timeout=0.1)
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c", host_script, str(root)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            try:
                _, stderr = proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                pytest.fail("Host remained blocked in attachment authentication")
            assert proc.returncode == 1, stderr
            assert host.read_host_info(root) is None
        finally:
            for child in (proc, server):
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)


def test_shutdown_authentication_stall_after_readiness_is_bounded():
    from tianshu import ipc

    daemon_script = r"""
import multiprocessing.connection,sys,threading
from pathlib import Path
from tianshu import cli
root = Path(sys.argv[1])
deliver = multiprocessing.connection.deliver_challenge
def blocked(connection, *args, **kwargs):
    if (root / "stall").exists():
        (root / "entered").touch()
        threading.Event().wait()
    return deliver(connection, *args, **kwargs)
multiprocessing.connection.deliver_challenge = blocked
raise SystemExit(cli.main(["daemon", "--model", "scripted", "--data-root", str(root)]))
"""
    supervisor_script = r"""
import sys
from pathlib import Path
from tianshu import host
root = Path(sys.argv[1])
children = []
popen = host.subprocess.Popen
def observed(*args, **kwargs):
    child = popen(*args, **kwargs)
    children.append(child)
    return child
host.subprocess.Popen = observed
supervisor = host.DaemonSupervisor(root, [], start_timeout_s=2)
supervisor.ensure()
# ensure() has already completed a real authentication handshake. Leave the
# accept loop free so the shutdown helper, not a warm-up probe, enters the stall.
(root / "stall").touch()
try:
    supervisor.stop(timeout=5.0)
except host.HostError:
    assert (root / "entered").exists(), "shutdown helper never reached authentication"
    assert all(child.poll() is not None for child in children), "helper was not reaped"
    print("bounded-stop", flush=True)
else:
    raise AssertionError("stalled endpoint was reported stopped")
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-stopauth-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        daemon_proc = subprocess.Popen(
            [sys.executable, "-u", "-c", daemon_script, str(root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(),
        )
        proc = None
        try:
            deadline = time.monotonic() + 20
            while not ipc.probe(root, timeout=0.1):
                assert daemon_proc.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.01)
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c", supervisor_script, str(root)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env(),
            )
            started = time.monotonic()
            try:
                stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail("Host remained blocked in shutdown authentication after readiness")
            assert time.monotonic() - started < 14, "shutdown authentication exceeded its bound"
            assert proc.returncode == 0 and b"bounded-stop" in stdout, stderr
        finally:
            for child in (proc, daemon_proc):
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)


def test_monitor_retains_a_live_root_instead_of_spawning_the_next_generation(
    tmp_path, monkeypatch, held_child,
):
    supervisor = host.DaemonSupervisor(
        tmp_path, [sys._base_executable, "-c", "pass"], start_timeout_s=0.1,
    )
    supervisor._process = held_child
    second_attempt = threading.Event()
    release = threading.Event()
    attempts = []
    created = []
    spawn = supervisor._spawn_and_wait
    popen = subprocess.Popen

    def observed(*args, **kwargs):
        child = popen(*args, **kwargs)
        created.append(child)
        return child

    def retried():
        attempts.append(True)
        if len(attempts) == 2:
            second_attempt.set()
            assert release.wait(timeout=10)
        spawn()

    monkeypatch.setattr(host.subprocess, "Popen", observed)
    monkeypatch.setattr(supervisor, "alive", lambda: False)
    monkeypatch.setattr(supervisor, "_spawn_and_wait", retried)
    monkeypatch.setattr(host, "RESTART_BACKOFF_S", (0,))
    supervisor.start_monitor()
    try:
        assert second_attempt.wait(timeout=10)
        assert not created, "monitor overwrote the unreaped previous generation"
        assert supervisor._process is held_child
    finally:
        supervisor._stop.set()
        release.set()
        assert supervisor._thread is not None
        supervisor._thread.join(timeout=10)
        for child in created:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        supervisor._close_log()
    assert not supervisor._thread.is_alive()
    assert held_child.stdin is not None
    held_child.stdin.write(b"G")
    held_child.stdin.flush()
    supervisor._process = held_child
    supervisor.stop(timeout=0.1)
    assert held_child.returncode == 0 and supervisor._process is None


def test_bridge_failure_is_not_masked_by_host_cleanup_error(
    tmp_path, monkeypatch, capsys,
):
    bridge_error = OSError(errno.EADDRINUSE, "bridge occupied")
    cleanup_error = PermissionError(errno.EACCES, "host lock held")
    original_clear = host.clear_host_info

    def fail_bridge(config):
        raise bridge_error

    def fail_clear(*args, **kwargs):
        raise cleanup_error

    monkeypatch.setattr(host.DaemonSupervisor, "ensure", lambda self: None)
    monkeypatch.setattr(host, "UiBridge", fail_bridge)
    monkeypatch.setattr(host, "clear_host_info", fail_clear)
    try:
        with pytest.raises(OSError) as raised:
            host.run_host(
                tmp_path, ui_dir=tmp_path / "ui", daemon_command=[], supervise=False,
            )
        assert raised.value is bridge_error
        assert "清理 Host 信息失败" in capsys.readouterr().err
    finally:
        original_clear(tmp_path)


def test_publish_failure_reaps_owned_root_despite_updated_port():
    from tianshu import daemon, ipc

    script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original_write = host._write_host_info
writes = []
def fail_second(data_root, info, *, expected=None):
    writes.append(info.port)
    if len(writes) == 2:
        raise host.HostError("controlled publish failure")
    return original_write(data_root, info, expected=expected)
host._write_host_info = fail_second
original_stop = host.DaemonSupervisor.stop
def fail_start_cleanup(self, *args, **kwargs):
    if kwargs.get("reason") == "host-start-failure":
        raise host.HostError("controlled start cleanup failure")
    return original_stop(self, *args, **kwargs)
host.DaemonSupervisor.stop = fail_start_cleanup
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-publishfail-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        result = subprocess.run(
            [_test_interpreter(), "-u", "-c", script, str(root)],
            capture_output=True, timeout=30, check=False, env=_child_env(),
        )
        assert result.returncode == 1, result.stderr.decode("utf-8", "replace")
        assert "发布 Host 实例失败" in result.stderr.decode("utf-8")
        assert not host.host_lock_path(root).exists()
        deadline = time.monotonic() + 5
        while daemon.lock_path(root).exists() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        assert not daemon.lock_path(root).exists(), "publish failure leaked its Daemon lock"
        detail = ipc.probe_result(root, timeout=0.2)
        assert not detail.ok and detail.errno == errno.ENOENT
        assert not detail.endpoint_exists


@pytest.mark.parametrize("owned", [True, False])
def test_post_readiness_bridge_bind_failure_reaps_only_owned_root(owned):
    import socket

    from tianshu import daemon, ipc

    script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original_stop = host.DaemonSupervisor.stop
def observed_stop(self, *args, **kwargs):
    (root / "bridge-stop").write_text(kwargs.get("reason", "user"), encoding="utf-8")
    return original_stop(self, *args, **kwargs)
host.DaemonSupervisor.stop = observed_stop
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=int(sys.argv[2]), supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-bridgefail-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        daemon_proc = None
        attached_snapshot = None
        try:
            if not owned:
                daemon_proc = subprocess.Popen(
                    [sys.executable, "-m", "tianshu", "daemon", "--model", "scripted",
                     "--data-root", str(root)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(),
                )
                deadline = time.monotonic() + 20
                while not ipc.probe(root, timeout=0.1):
                    assert daemon_proc.poll() is None and time.monotonic() < deadline
                    threading.Event().wait(0.02)
                attached_snapshot = daemon.lock_path(root).read_bytes()

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                reserved.bind(("127.0.0.1", 0))
                reserved.listen(1)
                result = subprocess.run(
                    [sys.executable, "-u", "-c", script, str(root),
                     str(reserved.getsockname()[1])],
                    capture_output=True, timeout=30, check=False,
                )
            assert result.returncode == 1, result.stderr.decode("utf-8", "replace")
            assert not host.host_lock_path(root).exists()
            if owned:
                assert (root / "bridge-stop").read_text(encoding="utf-8") == "host-start-failure"
                deadline = time.monotonic() + 5
                while daemon.lock_path(root).exists() and time.monotonic() < deadline:
                    threading.Event().wait(0.02)
                assert not daemon.lock_path(root).exists(), "failed Host leaked its Daemon lock"
                detail = ipc.probe_result(root, timeout=0.2)
                assert not detail.ok and detail.errno == errno.ENOENT
                assert not detail.endpoint_exists
            else:
                assert not (root / "bridge-stop").exists()
                assert daemon_proc is not None and daemon_proc.poll() is None
                assert daemon.lock_path(root).read_bytes() == attached_snapshot
                assert ipc.probe(root, timeout=0.2)
        finally:
            shutdown_daemon_and_wait(root, timeout=5)
            if daemon_proc is not None:
                try:
                    daemon_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon_proc.kill()
                    daemon_proc.wait(timeout=10)


@pytest.mark.parametrize("replacement_stop_fails", [False, True])
def test_takeover_bridge_failure_reaps_the_previous_owned_daemon(replacement_stop_fails):
    import errno
    import socket

    from tianshu import daemon, ipc

    first_script = r"""
import sys,time
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original = host.UiBridge.serve_forever
def paused(self):
    (root / "first-serving").touch()
    deadline = time.monotonic() + 20
    while not (root / "first-release").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return original(self)
host.UiBridge.serve_forever = paused
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    second_script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
if sys.argv[3] == "fail":
    def refuse_stop(self, *args, **kwargs):
        raise host.HostError("controlled replacement cleanup failure")
    host.DaemonSupervisor.stop = refuse_stop
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=int(sys.argv[2]), supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-takeover-fail-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        first = None
        try:
            with (root / "first.stderr").open("wb") as err:
                first = subprocess.Popen(
                    [_test_interpreter(), "-u", "-c", first_script, str(root)],
                    stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
                )
                deadline = time.monotonic() + 30
                while not (root / "first-serving").exists():
                    assert first.poll() is None and time.monotonic() < deadline
                    threading.Event().wait(0.02)
                starting = host.read_host_info(root)
                assert starting is not None and starting.pid == first.pid
                assert starting.port == 0
                assert daemon.lock_path(root).exists()
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                    reserved.bind(("127.0.0.1", 0))
                    reserved.listen(1)
                    second = subprocess.run(
                        [_test_interpreter(), "-u", "-c", second_script, str(root),
                         str(reserved.getsockname()[1]),
                         "fail" if replacement_stop_fails else "stop"],
                        capture_output=True, timeout=30, check=False, env=_child_env(),
                    )
                assert second.returncode == 1, second.stderr.decode("utf-8", "replace")
                assert daemon.lock_path(root).exists() is replacement_stop_fails
                (root / "first-release").touch()
                assert first.wait(timeout=15) == 1
            assert not host.host_lock_path(root).exists()
            assert not daemon.lock_path(root).exists(), "failed takeover leaked the shared Daemon"
            detail = ipc.probe_result(root, timeout=0.2)
            assert not detail.ok and detail.errno == errno.ENOENT
            assert not detail.endpoint_exists
        finally:
            (root / "first-release").touch()
            if first is not None and first.poll() is None:
                first.kill()
                first.wait(timeout=10)
            shutdown_daemon_and_wait(root, timeout=5)


def test_takeover_health_failure_reaps_the_adopted_daemon():
    import socket

    from tianshu import daemon, ipc

    script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
def fail_health(self, *, timeout):
    raise host.HostError("CONTROLLED_BRIDGE_UNHEALTHY")
host.UiBridge.wait_until_healthy = fail_health
raise SystemExit(host.run_host(
    Path(sys.argv[1]), ui_dir=Path(sys.argv[1]) / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-health-fail-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        daemon_proc = None
        try:
            with (root / "daemon.stderr").open("wb") as err:
                daemon_proc = subprocess.Popen(
                    [_test_interpreter(), "-m", "tianshu", "daemon", "--model", "scripted",
                     "--data-root", str(root)],
                    stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
                )
            deadline = time.monotonic() + 20
            while not ipc.probe(root, timeout=0.1):
                assert daemon_proc.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                reserved.bind(("127.0.0.1", 0))
                stale_port = reserved.getsockname()[1]
            host.write_host_info(root, host.HostInfo(
                pid=os.getpid(), port=stale_port, token="stale-host", started_at=1,
            ))
            result = subprocess.run(
                [_test_interpreter(), "-u", "-c", script, str(root)],
                capture_output=True, timeout=30, check=False, env=_child_env(),
            )
            assert result.returncode == 1, result.stderr.decode("utf-8", "replace")
            assert not host.host_lock_path(root).exists()
            assert not daemon.lock_path(root).exists(), (
                "unhealthy replacement leaked adopted Daemon"
            )
            detail = ipc.probe_result(root, timeout=0.2)
            assert not detail.ok and detail.errno == errno.ENOENT
            assert not detail.endpoint_exists
        finally:
            if daemon_proc is not None and daemon_proc.poll() is None:
                daemon_proc.kill()
                daemon_proc.wait(timeout=10)
            shutdown_daemon_and_wait(root, timeout=5)


@pytest.mark.parametrize("serialize_disappeared_owner", [False, True])
def test_shutdown_cannot_stop_a_daemon_after_another_host_took_over(
    serialize_disappeared_owner,
):
    from tianshu import daemon, ipc

    first_script = r"""
import sys,threading,time
from contextlib import contextmanager
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original_guard = host._host_info_guard
@contextmanager
def observed_guard(data_root):
    with original_guard(data_root) as lock:
        yield lock
    if (
        (root / "pause-after-stop-guard").exists()
        and threading.current_thread() is not threading.main_thread()
    ):
        (root / "stop-guard-released").touch()
        marker = "second-read-existing" if sys.argv[2] == "1" else "second-published"
        deadline = time.monotonic() + 10
        while not (root / marker).exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
host._host_info_guard = observed_guard
original_clear = host.clear_host_info
def observed_clear(data_root, **kwargs):
    original_clear(data_root, **kwargs)
    (root / "first-cleared").touch()
host.clear_host_info = observed_clear
original_get = host._Handler.do_GET
def blocked_get(self):
    if self.path == "/health" and (root / "block-health").exists():
        deadline = time.monotonic() + 20
        while not (root / "release-health").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    return original_get(self)
host._Handler.do_GET = blocked_get
original_stop = host.DaemonSupervisor.stop
def paused_stop(self, *args, **kwargs):
    if not getattr(self, "test_stop_paused", False):
        self.test_stop_paused = True
        (root / "shutdown-stop-entered").touch()
        deadline = time.monotonic() + 20
        while not (root / "release-stop").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    return original_stop(self, *args, **kwargs)
host.DaemonSupervisor.stop = paused_stop
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    second_script = r"""
import os,sys,time
from contextlib import contextmanager
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original_read = host.read_host_info
def observed_read(data_root):
    result = original_read(data_root)
    if result is not None and result.pid != os.getpid():
        (root / "second-read-existing").touch()
    return result
host.read_host_info = observed_read
original_guard = host._host_info_guard
@contextmanager
def observed_guard(data_root):
    (root / "second-guard").touch()
    if (
        sys.argv[2] == "1"
        and (root / "second-read-existing").exists()
        and not (root / "first-cleared").exists()
    ):
        deadline = time.monotonic() + 10
        while not (root / "first-cleared").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    with original_guard(data_root):
        yield
host._host_info_guard = observed_guard
original_write = host._write_host_info
def observed_write(data_root, info, *, expected=None):
    original_write(data_root, info, expected=expected)
    if info.pid == os.getpid() and info.port > 0:
        (root / "second-published").touch()
host._write_host_info = observed_write
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-stop-takeover-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        first = second = None
        first_err = second_err = None
        requester = None
        request_errors = []
        request_responses = []
        try:
            first_err = (root / "first.stderr").open("wb")
            first = subprocess.Popen(
                [_test_interpreter(), "-u", "-c", first_script, str(root),
                 str(int(serialize_disappeared_owner))],
                stdout=subprocess.DEVNULL, stderr=first_err, env=_child_env(),
            )
            deadline = time.monotonic() + 30
            while True:
                first_info = host.read_host_info(root)
                if (
                    first_info is not None and first_info.pid == first.pid
                    and first_info.port > 0 and host._http_alive(first_info)
                ):
                    break
                assert first.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            original_daemon = daemon.lock_path(root).read_bytes()
            original_host = host.host_lock_path(root).read_bytes()
            (root / "block-health").touch()

            def request_shutdown():
                try:
                    request_responses.append(host._http_post(
                        first_info.port, "/shutdown", first_info.token, timeout=20,
                    ))
                except OSError as exc:
                    request_errors.append(exc)

            requester = threading.Thread(target=request_shutdown)
            requester.start()
            while not (root / "shutdown-stop-entered").exists():
                assert first.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            second_err = (root / "second.stderr").open("wb")
            second = subprocess.Popen(
                [_test_interpreter(), "-u", "-c", second_script, str(root),
                 str(int(serialize_disappeared_owner))],
                stdout=subprocess.DEVNULL, stderr=second_err, env=_child_env(),
            )
            while not (root / "second-guard").exists():
                assert second.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            publish_deadline = time.monotonic() + 0.5
            while (
                not (root / "second-published").exists()
                and time.monotonic() < publish_deadline
            ):
                threading.Event().wait(0.01)
            assert not (root / "second-published").exists(), (
                "replacement Host took over while the old Host was stopping its Daemon"
            )
            assert host.host_lock_path(root).read_bytes() == original_host
            (root / "pause-after-stop-guard").touch()
            (root / "release-stop").touch()
            requester.join(timeout=25)
            assert not requester.is_alive() and not request_errors
            assert request_responses and b"200" in request_responses[0]
            assert first.wait(timeout=15) == 0
            while True:
                winner = host.read_host_info(root)
                if (
                    winner is not None and winner.pid == second.pid
                    and winner.port > 0 and host._http_alive(winner)
                ):
                    break
                assert second.poll() is None and time.monotonic() < deadline, (
                    root / "second.stderr"
                ).read_bytes()
                threading.Event().wait(0.02)
            assert daemon.lock_path(root).read_bytes() != original_daemon
            assert ipc.probe(root, timeout=0.2)
            (root / "release-health").touch()
            assert host.stop_host(root, timeout=15) == 0
            assert second.wait(timeout=15) == 0
            assert not daemon.lock_path(root).exists()
        finally:
            (root / "release-stop").touch()
            (root / "release-health").touch()
            if requester is not None:
                requester.join(timeout=25)
            for proc, stream in ((first, first_err), (second, second_err)):
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                if stream is not None:
                    stream.close()
            shutdown_daemon_and_wait(root, timeout=5)


    from tianshu import daemon, ipc

    first_script = r"""
import sys,time
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original = host._Handler.do_GET
def blocked(self):
    if self.path == "/health" and (root / "block-health").exists():
        deadline = time.monotonic() + 20
        while not (root / "release-health").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    return original(self)
host._Handler.do_GET = blocked
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    second_script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-published-takeover-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        first = second = None
        first_err = second_err = None
        try:
            first_err = (root / "first.stderr").open("wb")
            first = subprocess.Popen(
                [_test_interpreter(), "-u", "-c", first_script, str(root)],
                stdout=subprocess.DEVNULL, stderr=first_err, env=_child_env(),
            )
            deadline = time.monotonic() + 30
            while True:
                info = host.read_host_info(root)
                if info is not None and info.pid == first.pid and info.port > 0:
                    break
                assert first.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            assert host._http_alive(info)
            attached_snapshot = daemon.lock_path(root).read_bytes()
            (root / "block-health").touch()
            second_err = (root / "second.stderr").open("wb")
            second = subprocess.Popen(
                [_test_interpreter(), "-u", "-c", second_script, str(root)],
                stdout=subprocess.DEVNULL, stderr=second_err, env=_child_env(),
            )
            while True:
                winner = host.read_host_info(root)
                if (
                    winner is not None
                    and winner.pid == second.pid
                    and winner.port > 0
                    and host._http_alive(winner)
                ):
                    break
                assert second.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            assert first.wait(timeout=15) == 1
            (root / "release-health").touch()
            assert second.poll() is None
            assert host.read_host_info(root) == winner
            assert daemon.lock_path(root).read_bytes() == attached_snapshot
            assert ipc.probe(root, timeout=0.2)
            assert host.stop_host(root, timeout=15) == 0
            assert second.wait(timeout=15) == 0
            assert not daemon.lock_path(root).exists()
        finally:
            (root / "release-health").touch()
            for proc, stream in ((first, first_err), (second, second_err)):
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                if stream is not None:
                    stream.close()
            shutdown_daemon_and_wait(root, timeout=5)


def test_bridge_failure_does_not_stop_a_daemon_already_attached_by_another_host():
    import socket

    from tianshu import daemon, ipc

    first_script = r"""
import sys,time
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original = host.UiBridge
def delayed(config):
    (root / "first-bridge-pending").touch()
    deadline = time.monotonic() + 20
    while not (root / "second-ready").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return original(config)
host.UiBridge = delayed
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=int(sys.argv[2]), supervise=False,
))
"""
    second_script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-bridge-takeover-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        first = second = None
        first_err = second_err = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                reserved.bind(("127.0.0.1", 0))
                reserved.listen(1)
                first_err = (root / "first.stderr").open("wb")
                first = subprocess.Popen(
                    [_test_interpreter(), "-u", "-c", first_script, str(root),
                     str(reserved.getsockname()[1])],
                    stdout=subprocess.DEVNULL, stderr=first_err, env=_child_env(),
                )
                deadline = time.monotonic() + 30
                while not (root / "first-bridge-pending").exists():
                    assert first.poll() is None and time.monotonic() < deadline
                    threading.Event().wait(0.02)
                attached_snapshot = daemon.lock_path(root).read_bytes()
                second_err = (root / "second.stderr").open("wb")
                second = subprocess.Popen(
                    [_test_interpreter(), "-u", "-c", second_script, str(root)],
                    stdout=subprocess.DEVNULL, stderr=second_err, env=_child_env(),
                )
                while True:
                    info = host.read_host_info(root)
                    if info is not None and info.pid == second.pid and info.port > 0:
                        break
                    assert second.poll() is None and time.monotonic() < deadline
                    threading.Event().wait(0.02)
                assert ipc.probe(root, timeout=0.2)
                (root / "second-ready").touch()
                assert first.wait(timeout=15) == 1
            assert second.poll() is None, "first Host cleanup stopped the replacement Host"
            assert daemon.lock_path(root).read_bytes() == attached_snapshot
            assert ipc.probe(root, timeout=0.2), "first Host stopped the attached Daemon"
        finally:
            (root / "second-ready").touch()
            for proc, stream in ((first, first_err), (second, second_err)):
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                if stream is not None:
                    stream.close()
            shutdown_daemon_and_wait(root, timeout=5)


def test_exit_cleanup_holds_host_identity_until_owned_root_is_reaped():
    import socket

    from tianshu import ipc

    first_script = r"""
import sys,time
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root=Path(sys.argv[1])
def refuse_graceful_stop(self, *args, **kwargs):
    raise host.HostError("controlled cleanup fallback")
host.DaemonSupervisor.stop = refuse_graceful_stop
original=host.DaemonSupervisor._abort_start
def paused(self,proc):
    (root/'cleanup-paused').write_text(str(proc.pid))
    deadline=time.monotonic()+20
    while not (root/'cleanup-release').exists():
        assert time.monotonic()<deadline
        time.sleep(0.01)
    return original(self,proc)
host.DaemonSupervisor._abort_start=paused
raise SystemExit(host.run_host(
    root,ui_dir=root/'ui',
    daemon_command=[sys.executable,'-m','tianshu','daemon','--model','scripted'],
    port=int(sys.argv[2]),supervise=False,
))
"""
    second_script = r"""
import sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root=Path(sys.argv[1])
raise SystemExit(host.run_host(
    root,ui_dir=root/'ui',
    daemon_command=[sys.executable,'-m','tianshu','daemon','--model','scripted'],
    port=0,supervise=False,
))
"""

    def direct_info(root):
        try:
            return json.loads(host.host_lock_path(root).read_bytes())
        except FileNotFoundError:
            return None

    with tempfile.TemporaryDirectory(
        prefix="tsh-takeover-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        first = second = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                reserved.bind(("127.0.0.1", 0))
                reserved.listen(1)
                with (root / "first.stderr").open("wb") as err:
                    first = subprocess.Popen(
                        [_test_interpreter(), "-u", "-c", first_script, str(root),
                         str(reserved.getsockname()[1])],
                        stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
                    )
                deadline = time.monotonic() + 30
                while not (root / "cleanup-paused").exists():
                    assert first.poll() is None and time.monotonic() < deadline
                    threading.Event().wait(0.02)
                assert ipc.probe(root, timeout=0.2)
                first_info = direct_info(root)
                assert first_info is not None
                with (root / "second.stderr").open("wb") as err:
                    second = subprocess.Popen(
                        [_test_interpreter(), "-u", "-c", second_script, str(root)],
                        stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
                    )
                deadline = time.monotonic() + 2
                takeover_before_release = False
                while time.monotonic() < deadline:
                    current = direct_info(root)
                    if current is not None and current["token"] != first_info["token"]:
                        takeover_before_release = True
                        break
                    assert second.poll() is None, (root / "second.stderr").read_text()
                    threading.Event().wait(0.02)
                assert not takeover_before_release, "new Host took over before owned-root reap"
                (root / "cleanup-release").touch()
                assert first.wait(timeout=15) == 1

                deadline = time.monotonic() + 20
                while True:
                    current = direct_info(root)
                    if (
                        current is not None
                        and current["token"] != first_info["token"]
                        and host._http_alive(host.HostInfo(**current))
                        and ipc.probe(root, timeout=0.2)
                    ):
                        break
                    assert second.poll() is None, (root / "second.stderr").read_text()
                    assert time.monotonic() < deadline, "replacement Host never became healthy"
                    threading.Event().wait(0.05)
                assert host.stop_host(root, timeout=10) == 0
                assert second.wait(timeout=15) == 0
                assert not host.host_lock_path(root).exists()
        finally:
            (root / "cleanup-release").touch()
            for proc in (second, first):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            shutdown_daemon_and_wait(root, timeout=5)


def test_failed_owned_readiness_reaps_the_started_root(monkeypatch):
    from tianshu import daemon

    script = r"""
import json,os,sys
from pathlib import Path
from multiprocessing.connection import Listener
from tianshu import ipc
root = Path(sys.argv[1])
ipc.load_authkey(root)
(ipc.run_dir(root) / "daemon.lock").write_text(json.dumps({
    "pid": os.getpid(), "instance_id": "blocked-start",
}))
listener = Listener(ipc.endpoint_path(root), family=ipc._family(), authkey=None)
connections = []
while True:
    connections.append(listener.accept())
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-ownauth-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        supervisor = host.DaemonSupervisor(
            Path(directory), [sys.executable, "-u", "-c", script, directory],
            start_timeout_s=1,
        )
        children = []
        popen = subprocess.Popen

        def observed(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(host.subprocess, "Popen", observed)
        try:
            with pytest.raises(host.HostError):
                supervisor.ensure()
            assert children and all(child.poll() is not None for child in children)
            assert supervisor._process is None
            assert not daemon.lock_path(Path(directory)).exists()
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
            supervisor._close_log()


def test_owned_readiness_cannot_adopt_another_process_endpoint():
    from tianshu import daemon, ipc

    with tempfile.TemporaryDirectory(
        prefix="tsh-ownswap-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        script = (
            "import sys,threading; from pathlib import Path; "
            "Path(sys.argv[1]).touch(); threading.Event().wait()"
        )
        supervisor = host.DaemonSupervisor(
            root, [sys._base_executable, "-c", script, str(root / "started")],
            start_timeout_s=10,
        )
        errors = []

        def ensure():
            try:
                supervisor.ensure()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=ensure)
        replacement = None
        owned = None
        worker.start()
        try:
            deadline = time.monotonic() + 20
            while not (root / "started").exists():
                assert worker.is_alive() and time.monotonic() < deadline
                threading.Event().wait(0.01)
            owned = supervisor._process
            assert owned is not None and owned.poll() is None
            replacement = subprocess.Popen(
                [sys.executable, "-m", "tianshu", "daemon", "--model", "scripted",
                 "--data-root", str(root)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(),
            )
            worker.join(timeout=15)
            assert not worker.is_alive()
            assert errors and isinstance(errors[0], host.HostError), (
                "readiness adopted another process's endpoint"
            )
            assert owned.poll() is not None and supervisor._process is None
            assert replacement.poll() is None and ipc.probe(root, timeout=0.1)
        finally:
            try:
                owner = json.loads(daemon.lock_path(root).read_bytes())
            except FileNotFoundError:
                owner = None
            if owner is not None:
                daemon.request_shutdown(root, expected_instance_id=owner["instance_id"])
            supervisor._stop.set()
            for child in (owned, replacement):
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=10)
            worker.join(timeout=10)
            supervisor._close_log()


@pytest.mark.parametrize("probe_errno", [None, errno.EACCES, errno.ETIMEDOUT, errno.ECONNREFUSED])
@pytest.mark.parametrize("phase", ["missing_lock", "grace", "escalation"])
def test_attached_stop_cannot_treat_an_unknown_endpoint_as_exited(
    tmp_path, monkeypatch, held_child, probe_errno, phase,
):
    from tianshu import ipc

    lock = ipc.run_dir(tmp_path) / "daemon.lock"
    lock.parent.mkdir(parents=True)
    snapshot = json.dumps({
        "pid": held_child.pid, "instance_id": "uncertain-owner",
    }).encode()
    if phase != "missing_lock":
        lock.write_bytes(snapshot)
    supervisor = host.DaemonSupervisor(tmp_path, [])
    supervisor._shutdown_snapshot_taken = True
    supervisor._shutdown_snapshot = snapshot
    supervisor._shutdown_unconfirmed = True
    clock = [0.0]
    monkeypatch.setattr(host, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))
    monkeypatch.setattr(supervisor, "_request_shutdown", lambda *a, **kw: True)
    monkeypatch.setattr(supervisor, "_terminate", lambda *a, **kw: None)
    monkeypatch.setattr(ipc, "probe", lambda *a, **kw: phase == "escalation" and clock[0] < 0.2)
    monkeypatch.setattr(ipc, "probe_result", lambda *a, **kw: ipc.ProbeResult(
        False, "controlled-endpoint", "unconfirmed", probe_errno, False,
    ))
    with pytest.raises(host.HostError, match="退出"):
        supervisor.stop(timeout=0.2)
    assert held_child.poll() is None
    assert supervisor._shutdown_unconfirmed


def test_attached_stop_failure_keeps_monitoring_and_can_stop_a_later_owned_root():
    from tianshu import daemon, ipc

    script = r"""
import sys,time
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
original = host.DaemonSupervisor._request_shutdown
original_spawn = host.DaemonSupervisor._spawn_and_wait
def fail_once(self, **kwargs):
    with (root / "shutdown-requested").open("a") as marker:
        marker.write(f"{kwargs.get('instance_id')}\n")
    if not (root / "failed-stop").exists():
        (root / "failed-stop").touch()
        return False
    return original(self, **kwargs)
def observe_spawn(self):
    original_spawn(self)
    if (root / "failed-stop").exists() and self._process is not None:
        (root / "spawn-ready").touch()
host.DaemonSupervisor._request_shutdown = fail_once
host.DaemonSupervisor._spawn_and_wait = observe_spawn
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    port=0, supervise=True,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-resume-monitor-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        daemon_proc = proc = None
        try:
            with (root / "external.stderr").open("wb") as err:
                daemon_proc = subprocess.Popen(
                    [_test_interpreter(), "-m", "tianshu", "daemon", "--model", "scripted",
                     "--data-root", str(root)],
                    stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
                )
            deadline = time.monotonic() + 20
            while not ipc.probe(root, timeout=0.1):
                assert daemon_proc.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            original_lock = daemon.lock_path(root).read_bytes()
            with (root / "host.stderr").open("wb") as err:
                proc = subprocess.Popen(
                    [_test_interpreter(), "-u", "-c", script, str(root)],
                    stdout=subprocess.DEVNULL, stderr=err, env=_child_env(),
                )
            while True:
                info = host.read_host_info(root)
                if info is not None and info.port > 0 and host._http_alive(info):
                    break
                assert proc.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.02)
            assert host.stop_host(root, timeout=1) == 1
            assert (root / "failed-stop").exists()
            assert proc.poll() is None
            assert host.read_host_info(root) == info
            assert host._http_alive(info)
            assert ipc.probe(root, timeout=0.2)

            daemon_proc.kill()
            daemon_proc.wait(timeout=10)
            deadline = time.monotonic() + 30
            while True:
                try:
                    current_lock = daemon.lock_path(root).read_bytes()
                except FileNotFoundError:
                    current_lock = None
                if (
                    current_lock is not None
                    and current_lock != original_lock
                    and ipc.probe(root, timeout=0.1)
                ):
                    break
                assert proc.poll() is None and time.monotonic() < deadline, (
                    "monitor did not restart the Daemon after an unconfirmed attached stop"
                )
                threading.Event().wait(0.05)
            assert current_lock is not None
            new_owner = json.loads(current_lock)
            deadline = time.monotonic() + 10
            while not (root / "spawn-ready").exists():
                assert proc.poll() is None and time.monotonic() < deadline
                threading.Event().wait(0.01)
            requests_path = root / "shutdown-requested"
            requests_before = requests_path.read_text()
            assert host.stop_host(root, timeout=15) == 0
            requests_after = requests_path.read_text()
            assert requests_after != requests_before
            assert new_owner["instance_id"] in requests_after.splitlines()
            returncode = proc.wait(timeout=20)
            assert returncode == 0, (root / "host.stderr").read_bytes()
            assert not host.host_lock_path(root).exists()
            assert not daemon.lock_path(root).exists()
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if daemon_proc is not None and daemon_proc.poll() is None:
                daemon_proc.kill()
                daemon_proc.wait(timeout=10)
            shutdown_daemon_and_wait(root, timeout=5)


@pytest.mark.parametrize("replace_after_failure", [False, True])
def test_attached_host_preserves_original_target_after_a_lock_read_error(
    replace_after_failure,
):
    """An EIO cannot mean stopped, and retry cannot adopt a replacement owner."""
    from tianshu import daemon, ipc

    script = r"""
import errno,sys
from pathlib import Path
from tianshu import host
from tianshu.cli import _force_utf8_streams
_force_utf8_streams()
root = Path(sys.argv[1])
read_bytes = Path.read_bytes
def read(path):
    if path.name == "daemon.lock" and (root / "arm").exists():
        if not (root / "consumed").exists():
            (root / "consumed").touch()
            raise OSError(errno.EIO, "controlled lock read failure")
    return read_bytes(path)
Path.read_bytes = read
raise SystemExit(host.run_host(
    root, ui_dir=root / "ui",
    daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
    supervise=False,
))
"""
    with tempfile.TemporaryDirectory(
        prefix="tsh-eio-", dir=None if os.name == "nt" else "/tmp",
    ) as directory:
        root = Path(directory)
        children = []
        proc = None

        def wait_until(ready):
            deadline = time.monotonic() + 20
            while True:
                result = ready()
                if result:
                    return result
                assert time.monotonic() < deadline, "process did not become ready"
                threading.Event().wait(0.01)

        def start_daemon():
            child = subprocess.Popen(
                [sys.executable, "-m", "tianshu", "daemon", "--model", "scripted",
                 "--data-root", str(root)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(),
            )
            children.append(child)

            def ready():
                assert child.poll() is None, "daemon exited before readiness"
                return ipc.probe(root, timeout=0.1)

            wait_until(ready)
            return child, json.loads(daemon.lock_path(root).read_bytes())

        try:
            original, owner = start_daemon()
            with (root / "stderr.log").open("wb") as stderr:
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-c", script, str(root)],
                    stdout=subprocess.DEVNULL, stderr=stderr, env=_child_env(),
                )

            def serving():
                assert proc.poll() is None, "host exited before readiness"
                info = host.read_host_info(root)
                return info if info and info.port and host._http_alive(info) else None

            info = wait_until(serving)
            (root / "arm").touch()
            assert host.stop_host(root, timeout=1) == 1
            assert (root / "consumed").exists()
            assert proc.poll() is None and original.poll() is None
            assert host.read_host_info(root) == info and host._http_alive(info)
            assert json.loads(daemon.lock_path(root).read_bytes()) == owner
            assert ipc.probe(root, timeout=0.2)
            if replace_after_failure:
                assert daemon.request_shutdown(
                    root, expected_instance_id=owner["instance_id"],
                )
                assert original.wait(timeout=10) == 0
                replacement, new_owner = start_daemon()
                assert new_owner["instance_id"] != owner["instance_id"]
                assert host.stop_host(root, timeout=1) == 1
                assert proc.poll() is None and replacement.poll() is None
                assert host.read_host_info(root) == info and host._http_alive(info)
                assert json.loads(daemon.lock_path(root).read_bytes()) == new_owner
                assert ipc.probe(root, timeout=0.2)
            else:
                assert host.stop_host(root, timeout=10) == 0
                assert proc.wait(timeout=10) == original.wait(timeout=10) == 0
                assert not host.host_lock_path(root).exists()
        finally:
            try:
                current = json.loads(daemon.lock_path(root).read_bytes())
            except FileNotFoundError:
                current = None
            if current is not None:
                daemon.request_shutdown(root, expected_instance_id=current["instance_id"])
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            if proc is not None and proc.poll() is None:
                host.stop_host(root, timeout=1)
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
