"""Record fixed labels, process IDs and bounded timing data without writing on startup."""

import atexit
import json
import os
import sys
import threading
from functools import wraps
from importlib import _bootstrap
from pathlib import Path
from time import perf_counter_ns, thread_time_ns

LIMIT = 384
CONTEXT = threading.local()
SPANS = []
SEEN = set()
DROPPED = 0
TRACE_STARTED = perf_counter_ns()
LOAD = _bootstrap._find_and_load
PHASES = {
    "tianshu.cli": ("_build_parser", "_cmd_host", "_cmd_daemon"),
    "tianshu.host": (
        "run_host",
        "_acquire_host_info",
        "_write_host_info",
        "_daemon_helper",
        "DaemonSupervisor.ensure",
        "DaemonSupervisor._spawn_and_wait",
        "DaemonSupervisor._run_daemon_helper",
        "UiBridge.wait_until_healthy",
    ),
    "tianshu.daemon": (
        "_Daemon.__init__",
        "_Daemon.run",
        "_Daemon._setup",
        "_Daemon._recover_startup",
        "acquire_lock",
        "_open_event_log",
        "build_stack",
        "_Daemon._await_endpoint_ready",
    ),
    "tianshu.ipc": ("Client.connect", "Server.start", "self_connect"),
    "tianshu.events": ("EventLog._open_database", "EventLog.__init__"),
    "tianshu.restore_access": (
        "acquire_restore_database_access",
        "verify_sqlite_main",
        "ensure_path_not_held",
        "ensure_material_write_allowed",
        "_verify_local_witnesses",
        "preflight_legacy_material_layout",
    ),
    "tianshu.data_admission": ("check_connection", "check_new_file_evidence"),
    "tianshu.maintenance": (
        "MaintenanceConnection.execute",
        "MaintenanceConnection.executescript",
    ),
    "tianshu.leases": ("LeaseStore.__init__",),
    "tianshu.jobs": ("JobStore.__init__",),
    "tianshu.permissions": ("ApprovalStore.__init__", "GrantStore.__init__"),
    "tianshu.snapshot": ("SnapshotStore.__init__",),
    "tianshu.budget": ("BudgetLedger.__init__",),
    "tianshu.contents": ("ContentStore.__init__",),
    "tianshu.tasks": ("TaskStore.__init__",),
    "tianshu.summaries": ("SummaryStore.__init__",),
    "sqlite3": ("connect",),
}
DETAIL_MODULES = frozenset(PHASES) - {
    "tianshu.cli",
    "tianshu.host",
    "tianshu.daemon",
    "tianshu.ipc",
}
IMPORTS = {
    "tianshu.cli",
    "tianshu.host",
    "tianshu.daemon",
    "tianshu.ipc",
    "tianshu.restore_access",
}


def role_and_sample(argv):
    if "-m" in argv and "--data-root" in argv:
        module_at = argv.index("-m")
        if argv[module_at + 1 : module_at + 3] not in (["tianshu", "host"], ["tianshu", "daemon"]):
            return None, None
        sample = Path(argv[argv.index("--data-root") + 1]).name
        if sample in {"warmup_cold", "c0", "c1", "c2", "w0", "w1", "w2"}:
            return argv[module_at + 2], sample
    if "-c" in argv and "from tianshu.host import _daemon_helper" in argv[argv.index("-c") + 1]:
        return "helper", None
    return None, None


def record(label, started, cpu):
    global DROPPED
    ended = perf_counter_ns()
    elapsed_cpu = thread_time_ns() - cpu
    if len(SPANS) >= LIMIT:
        DROPPED += 1
        return
    SPANS.append((label, started, ended, elapsed_cpu))


def wrap(target, name, label, detail):
    original = getattr(target, name)
    setup = label == "tianshu.daemon._Daemon._setup"
    sql = label == "tianshu.maintenance.MaintenanceConnection.execute"
    helper = label in {
        "tianshu.host.DaemonSupervisor._run_daemon_helper",
        "tianshu.host._daemon_helper",
    }

    @wraps(original)
    def traced(*args, **kwargs):
        if detail and not getattr(CONTEXT, "in_setup", False):
            return original(*args, **kwargs)
        span = label
        if sql:
            command = args[1] if len(args) >= 2 else ""
            if command in {
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
                "PRAGMA busy_timeout=5000",
            }:
                span = f"sqlite:{command.split()[1].split('=')[0]}"
            else:
                return original(*args, **kwargs)
        if helper:
            operation = args[1] if name == "_run_daemon_helper" else args[0]["operation"]
            if operation in {"snapshot", "shutdown"}:
                span = f"{label}:{operation}"
        if setup:
            CONTEXT.in_setup = True
        started = perf_counter_ns()
        cpu = thread_time_ns()
        try:
            return original(*args, **kwargs)
        finally:
            record(span, started, cpu)
            if setup:
                CONTEXT.in_setup = False

    setattr(target, name, traced)


def traced_import(name, import_):
    if name not in PHASES or name in SEEN:
        return LOAD(name, import_)
    started = perf_counter_ns()
    cpu = thread_time_ns()
    loaded = LOAD(name, import_)
    if name not in SEEN:
        SEEN.add(name)
        if name in IMPORTS:
            record(f"import:{name}", started, cpu)
        for path in PHASES[name]:
            owner, _, member = path.rpartition(".")
            wrap(
                getattr(loaded, owner) if owner else loaded,
                member,
                f"{name}.{path}",
                name in DETAIL_MODULES,
            )
    return loaded


def write():
    if not SPANS:
        return
    root = Path(os.environ["TIANSHU_STARTUP_TIMING_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{os.getpid()}.json"
    output.write_bytes(
        (
            json.dumps(
                {
                    "role": ROLE,
                    "sample": SAMPLE,
                    "pid": os.getpid(),
                    "ppid": os.getppid(),
                    "trace_started_ns": TRACE_STARTED,
                    "limit": LIMIT,
                    "dropped": DROPPED,
                    "spans": SPANS,
                },
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )


ROLE, SAMPLE = role_and_sample(sys.orig_argv)
if ROLE:
    atexit.register(write)
    _bootstrap._find_and_load = traced_import
