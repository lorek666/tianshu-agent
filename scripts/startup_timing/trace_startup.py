"""Collect bounded numeric phase timings for the startup smoke probe."""
import atexit
import json
import os
from functools import wraps
from importlib import _bootstrap
from pathlib import Path
from time import perf_counter_ns

OUTPUT = Path(os.environ['TIANSHU_STARTUP_TIMING_DIR'])
START = perf_counter_ns()
PHASES = {
    'tianshu.cli': ('_build_parser', '_cmd_host', '_cmd_daemon'),
    'tianshu.host': (
        'run_host', '_acquire_host_info', '_write_host_info', '_daemon_helper',
        'DaemonSupervisor.ensure', 'DaemonSupervisor._spawn_and_wait',
        'DaemonSupervisor._run_daemon_helper', 'UiBridge.wait_until_healthy',
    ),
    'tianshu.daemon': (
        '_Daemon.__init__', '_Daemon._setup', '_Daemon._recover_startup',
        'acquire_lock', '_open_event_log', 'build_stack',
        '_Daemon._await_endpoint_ready',
    ),
    'tianshu.ipc': ('Client.connect', 'Server.start', 'self_connect'),
    'tianshu.restore_access': ('ensure_path_not_held',),
}
IMPORTS = {'tianshu.loop', 'tianshu.execution_admission', *PHASES}
spans = []
seen = {}
load = _bootstrap._find_and_load


def record(label, started):
    if len(spans) < 256:
        spans.append((label, started, perf_counter_ns()))


def wrap(target, name, label):
    original = getattr(target, name)

    @wraps(original)
    def traced(*args, **kwargs):
        started = perf_counter_ns()
        try:
            return original(*args, **kwargs)
        finally:
            record(label, started)

    setattr(target, name, traced)


def traced_import(name, import_):
    if name not in IMPORTS or name in seen:
        return load(name, import_)
    started = perf_counter_ns()
    loaded = load(name, import_)
    if name not in seen:
        seen[name] = True
        record('import:' + name, started)
        for path in PHASES.get(name, ()):
            owner, _, member = path.rpartition('.')
            wrap(getattr(loaded, owner) if owner else loaded, member, name + '.' + path)
    return loaded


@atexit.register
def write():
    if not spans:
        return
    OUTPUT.mkdir(parents=True, exist_ok=True)
    output = OUTPUT / f'{os.getpid()}.json'
    output.write_text(json.dumps({
        'pid': os.getpid(), 'ppid': os.getppid(), 'trace_start_ns': START,
        'spans': spans,
    }, allow_nan=False) + '\n', encoding='utf-8')


_bootstrap._find_and_load = traced_import
