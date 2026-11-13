"""Real child pytest runs, including expected failures and fatal negative controls."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pytest_with_stacks.py"
BOOTSTRAP = r'''
import faulthandler
import json
import os
from pathlib import Path
import sys
import threading
import pytest_with_stacks as wrapper

mode, audit = sys.argv[1:3]
original_init = wrapper.StackWatchdog.__init__
original_snapshot = wrapper.StackWatchdog.snapshot
original_os_write = os.write
instance = None

def observe_init(self, fd):
    global instance
    original_init(self, fd)
    instance = self
    self.observed = threading.Event()
    self.snapshot_entered = threading.Event()
    self.snapshot_ready = threading.Event()
    self.write_entered = threading.Event()
    if mode == "blocked":
        reader, writer = os.pipe()
        self.test_reader = reader
        os.set_blocking(writer, False)
        while True:
            try:
                original_os_write(writer, b"x" * 4096)
            except BlockingIOError:
                break
        os.set_blocking(writer, True)
        os.dup2(writer, fd)
        os.close(writer)

def observe_snapshot(self):
    self.snapshot_entered.set()
    try:
        if not self.snapshot_ready.wait(5):
            raise RuntimeError("test did not reach the target diagnostic phase")
        original_snapshot(self)
        if mode == "deadlock":
            print("STACK_DUMP_READY", flush=True)
        if mode == "partial":
            raise RuntimeError("PRIVATE_ERROR_SENTINEL")
    finally:
        self.observed.set()

def write_failure(self, data):
    raise OSError("PRIVATE_ERROR_SENTINEL")

def observe_blocked_write(fd, data):
    if fd == instance.fd:
        instance.write_entered.set()
        instance.observed.set()
    return original_os_write(fd, data)

def forbid_native_timer(*args, **kwargs):
    raise AssertionError("native timed dump was called")

class WaitFailure(threading.Event):
    def wait(self, timeout=None):
        if threading.current_thread().name == wrapper.THREAD_NAME:
            instance.observed.set()
            raise OverflowError("PRIVATE_ERROR_SENTINEL")
        return super().wait(timeout)

wrapper.StackWatchdog.__init__ = observe_init
wrapper.StackWatchdog.snapshot = observe_snapshot
faulthandler.dump_traceback_later = forbid_native_timer
if mode == "before":
    wrapper.StackWatchdog.write = write_failure
elif mode == "zero":
    wrapper.os.write = lambda fd, data: 0
elif mode == "blocked":
    wrapper.os.write = observe_blocked_write
elif mode == "wait":
    wrapper.threading.Event = WaitFailure

result = wrapper.main(sys.argv[3:])
try:
    os.fstat(instance.fd)
except OSError:
    descriptor_open = False
else:
    descriptor_open = True
Path(audit).write_text(json.dumps({
    "active": len(instance.active), "failures": instance.failures,
    "live": sum(t.name == wrapper.THREAD_NAME for t in threading.enumerate()),
    "descriptor_open": descriptor_open, "write_entered": instance.write_entered.is_set(),
}))
raise SystemExit(result)
'''
HEADER = '''import faulthandler
import os
from pathlib import Path
import sys
import threading
import pytest

def diagnostic_plugin(pytestconfig):
    return next(p for p in pytestconfig.pluginmanager.get_plugins()
                if type(p).__name__ == "StackWatchdog")

def wait_for_diagnostic(pytestconfig):
    plugin = diagnostic_plugin(pytestconfig)
    plugin.snapshot_ready.set()
    assert plugin.observed.wait(5), "diagnostic did not run"
'''


def child(tmp_path, body, *, mode="normal", timeout="0.02", extra=()):
    test = tmp_path / "test_subject.py"
    test.write_text(HEADER + body, encoding="utf-8")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    audit = tmp_path / "audit.json"
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(SCRIPT.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    })
    env.pop("PYTHONFAULTHANDLER", None)
    args = [
        sys.executable, "-c", BOOTSTRAP, mode, str(audit), str(test),
        "-c", str(config), "--confcutdir", str(tmp_path), "-q",
        "--stack-timeout", timeout, *extra,
    ]
    return args, env, audit


def run_child(tmp_path, body, **kwargs):
    args, env, audit = child(tmp_path, body, **kwargs)
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, timeout=15)
    details = json.loads(audit.read_text()) if audit.exists() else None
    return result, details


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_timed_dump_covers_the_entire_test_protocol(tmp_path, phase):
    body, frame = {
        "setup": (
            "@pytest.fixture\ndef setup_phase(pytestconfig):\n"
            "    wait_for_diagnostic(pytestconfig)\n"
            "def test_phase(setup_phase):\n    pass\n",
            b", in setup_phase\n",
        ),
        "call": (
            "def test_call_phase(pytestconfig):\n    wait_for_diagnostic(pytestconfig)\n",
            b", in test_call_phase\n",
        ),
        "teardown": (
            "@pytest.fixture\ndef teardown_phase(pytestconfig):\n    yield\n"
            "    wait_for_diagnostic(pytestconfig)\n"
            "def test_phase(teardown_phase):\n    pass\n",
            b", in teardown_phase\n",
        ),
    }[phase]
    result, audit = run_child(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"PYTHON STACK WATCHDOG" in result.stderr
    assert frame in result.stderr
    assert audit == {"active": 0, "failures": [], "live": 0,
                     "descriptor_open": False, "write_entered": False}


@pytest.mark.parametrize("timeout", ["60", str(threading.TIMEOUT_MAX)])
def test_quick_test_cancels_timer_and_does_not_leak_threads(tmp_path, timeout):
    result, audit = run_child(tmp_path, "def test_quick():\n    pass\n", timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"PYTHON STACK WATCHDOG" not in result.stderr
    assert audit == {"active": 0, "failures": [], "live": 0,
                     "descriptor_open": False, "write_entered": False}


@pytest.mark.parametrize("mode", ["normal", "partial", "blocked", "wait"])
@pytest.mark.parametrize("action, code", [
    ("pass", 0), ("assert False, 'original failure'", 1),
    ("raise KeyboardInterrupt('original interrupt')", 2),
])
def test_background_failure_is_not_green_and_original_exit_is_preserved(
    tmp_path, mode, action, code,
):
    body = f"def test_result(pytestconfig):\n    wait_for_diagnostic(pytestconfig)\n    {action}\n"
    result, audit = run_child(tmp_path, body, mode=mode)
    expected = 3 if mode != "normal" and code == 0 else code
    assert result.returncode == expected, result.stdout + result.stderr
    assert bool(audit["failures"]) == (mode != "normal")
    assert audit["active"] == (1 if mode == "blocked" else 0)
    if mode == "blocked":
        assert audit["live"] <= 1
    else:
        assert audit["live"] == 0
    assert audit["descriptor_open"] == (mode == "blocked")
    assert audit["write_entered"] == (mode == "blocked")
    if mode != "normal":
        assert b"Python stack watchdog failed" in result.stdout
    if mode == "partial":
        assert b"PYTHON STACK WATCHDOG" in result.stderr
    if mode == "wait":
        assert audit["failures"] == ["stack collection failed (OverflowError)"]
    assert b"PRIVATE_ERROR_SENTINEL" not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["before", "zero"])
def test_diagnostic_write_error_is_observable_without_partial_output(tmp_path, mode):
    body = "def test_error(pytestconfig):\n    wait_for_diagnostic(pytestconfig)\n"
    result, audit = run_child(tmp_path, body, mode=mode)
    assert result.returncode == 3, result.stdout + result.stderr
    assert audit == {"active": 0, "failures": ["stack collection failed (OSError)"], "live": 0,
                     "descriptor_open": False, "write_entered": False}
    assert b"Python stack watchdog failed" in result.stdout
    assert b"PYTHON STACK WATCHDOG" not in result.stderr


def test_diagnostic_does_not_print_source_or_locals(tmp_path):
    body = ("def test_privacy(pytestconfig):\n"
            "    private_local = 'PRIVATE_LOCAL_SENTINEL'\n"
            "    wait_for_diagnostic(pytestconfig); endpoint = 'PRIVATE_SOURCE_SENTINEL'\n"
            "    assert private_local and endpoint\n")
    result, _ = run_child(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"PYTHON STACK WATCHDOG" in result.stderr
    assert b", in test_privacy\n" in result.stderr
    assert b"PRIVATE_LOCAL_SENTINEL" not in result.stderr
    assert b"PRIVATE_SOURCE_SENTINEL" not in result.stderr


@pytest.mark.parametrize("timeout, extra", [
    ("nan", ()), ("inf", ()), ("-1", ()), ("1e30", ()),
    ("60", ("-o", "faulthandler_timeout=60")),
    ("60", ("-p", "no:faulthandler")),
])
def test_invalid_or_conflicting_diagnostics_are_rejected(tmp_path, timeout, extra):
    result, _ = run_child(tmp_path, "def test_unused():\n    pass\n", timeout=timeout, extra=extra)
    assert result.returncode == 4, result.stdout + result.stderr


@pytest.mark.parametrize("enabled", [True, False])
def test_fatal_handler_survives_real_abort_with_disabled_negative_control(tmp_path, enabled):
    marker = tmp_path / "abort.marker"
    body = ("def test_expected_abort():\n"
            + ("    faulthandler.disable()\n" if not enabled else "")
            + f"    Path({str(marker)!r}).write_text('abort reached')\n"
            + "    os.abort()\n")
    result, audit = run_child(tmp_path, body, timeout="60")
    assert marker.read_text() == "abort reached"
    assert result.returncode != 0 and audit is None
    assert (b"Fatal Python error" in result.stderr) == enabled
    assert (b"test_expected_abort" in result.stderr) == enabled
    assert b"INTERNALERROR" not in result.stdout + result.stderr


@pytest.mark.parametrize("enabled", [True, False])
def test_real_lock_deadlock_has_stack_and_disabled_negative_control(tmp_path, enabled):
    body = ("def test_deadlock(pytestconfig):\n"
            "    lock = threading.Lock()\n    lock.acquire()\n"
            "    print('LOCK_READY', flush=True)\n"
            "    diagnostic_plugin(pytestconfig).snapshot_ready.set()\n"
            "    lock.acquire()\n")
    args, env, _ = child(tmp_path, body, mode="deadlock",
                         timeout="0.02" if enabled else "0", extra=("-s",))
    stderr_path = tmp_path / "deadlock.stderr"
    with stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(args, cwd=tmp_path, env=env,
                                stdout=subprocess.PIPE, stderr=stderr_file)
    ready = queue.Queue()

    def read_ready():
        lock_ready = False
        for line in proc.stdout:
            if b"LOCK_READY" in line:
                lock_ready = True
                if not enabled:
                    ready.put(True)
                    return
            if enabled and b"STACK_DUMP_READY" in line:
                ready.put(lock_ready)
                return
        ready.put(False)

    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        assert ready.get(timeout=10), "child did not reach the lock/diagnostic barriers"
        reader.join(timeout=1)
        assert not reader.is_alive()
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(timeout=0.3)
    finally:
        proc.kill()
        proc.communicate(timeout=5)
        reader.join(timeout=1)
    stderr = stderr_path.read_bytes()
    assert (b"PYTHON STACK WATCHDOG" in stderr) == enabled
    assert (b"test_deadlock" in stderr) == enabled


def test_stack_output_has_frame_thread_and_byte_bounds():
    spec = importlib.util.spec_from_file_location("stack_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def deep_frame(depth):
        return deep_frame(depth - 1) if depth else sys._getframe()

    frame = deep_frame(140)
    deep = module.render_stacks({1: frame})
    assert deep.count(b'  File "') == 100 and b"[frame limit reached]" in deep
    shallow = []

    def capture_shallow_frame():
        shallow.append(sys._getframe())

    thread = threading.Thread(target=capture_shallow_frame)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    wide = module.render_stacks({i: shallow[0] for i in range(110)})
    assert wide.count(b"Thread ") == 100 and b"[thread limit reached]" in wide
    bounded = module.render_stacks({i: frame for i in range(100)})
    assert len(bounded) <= module.MAX_STACK_BYTES
    assert b"[stack output truncated]" in bounded
