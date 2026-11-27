"""Record fixed labels, process IDs and bounded timing data without writing on startup."""

import atexit
import json
import os
import sys
import threading
import uuid
from functools import wraps
from importlib import _bootstrap
from pathlib import Path
from time import perf_counter_ns, thread_time_ns

LIMIT = 384
CONTEXT = threading.local()
SPANS = []
SEEN = set()
DROPPED = 0
CHILDREN = {}
RECOVERY = {}
METRICS = []
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
        "DaemonSupervisor._terminate_owned",
        "UiBridge.wait_until_healthy",
    ),
    "tianshu.daemon": (
        "_Daemon.__init__",
        "_Daemon.run",
        "_Daemon._setup",
        "_Daemon._recover_startup",
        "_Daemon._recover_run",
        "_Daemon._hold_for_human",
        "_Daemon._park_dead_job",
        "_Daemon._shutdown",
        "_unpaired_intents",
        "request_shutdown",
        "acquire_lock",
        "_open_event_log",
        "build_stack",
        "_Daemon._await_endpoint_ready",
    ),
    "tianshu.ipc": ("Client.connect", "Client.call", "Server.start", "self_connect", "probe"),
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
    "tianshu.recovery": (
        "recover_run",
        "_recover_run",
        "_observe",
        "_settle",
        "_block_events",
        "_block_workspace",
    ),
    "tianshu.authority": ("register_blocked",),
    "tianshu.metrics": ("MetricsRecorder.record_duration_ms",),
    "tianshu.jobs": ("JobStore.__init__",),
    "tianshu.permissions": ("ApprovalStore.__init__", "GrantStore.__init__"),
    "tianshu.snapshot": ("SnapshotStore.__init__",),
    "tianshu.budget": ("BudgetLedger.__init__",),
    "tianshu.contents": ("ContentStore.__init__",),
    "tianshu.tasks": ("TaskStore.__init__",),
    "tianshu.summaries": ("SummaryStore.__init__",),
    "sqlite3": ("connect",),
}
RECOVERY_SCENARIOS = {
    "r0": "approved_tool_after_intent",
    "r1": "approved_tool_after_write",
    "r2": "unknown_effect_reconciling_blocked",
    "r3": "unsettled_model_and_budget",
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
    if "--_child-role" in argv and "--data-root" in argv:
        child_at = argv.index("--_child-role")
        sample = Path(argv[argv.index("--data-root") + 1]).name
        if (
            argv[child_at + 1] == "recovery-daemon"
            and sample in RECOVERY_SCENARIOS
            and "--_scenario" in argv
            and argv[argv.index("--_scenario") + 1] == RECOVERY_SCENARIOS[sample]
        ):
            return "recovery-daemon", sample
    if "-m" in argv and "--data-root" in argv:
        module_at = argv.index("-m")
        if argv[module_at + 1 : module_at + 3] not in (["tianshu", "host"], ["tianshu", "daemon"]):
            return None, None
        sample = Path(argv[argv.index("--data-root") + 1]).name
        if sample in {"warmup_cold", "c0", "c1", "c2", "w0", "w1", "w2"}:
            return argv[module_at + 2], sample
    if "-c" in argv and "from tianshu.host import _daemon_helper" in argv[argv.index("-c") + 1]:
        return "helper", None
    if len(argv) > 1 and Path(argv[1]).name == "measure_slo.py" and "--smoke" in argv:
        return "measure", None
    return None, None


def instrument_popen(module):
    original_init = module.Popen.__init__
    original_wait = module.Popen.wait
    original_kill = module.Popen.kill

    @wraps(original_init)
    def init(proc, args, *rest, **kwargs):
        child_role, child_sample = (
            role_and_sample(args) if isinstance(args, (list, tuple)) else (None, None)
        )
        relevant = (ROLE == "host" and child_role == "daemon") or (
            ROLE == "measure" and child_role == "recovery-daemon"
        )
        if not relevant:
            return original_init(proc, args, *rest, **kwargs)
        started = perf_counter_ns()
        original_init(proc, args, *rest, **kwargs)
        fact = {
            "sample": child_sample,
            "pid": proc.pid,
            "spawn_started_ns": started,
            "spawn_returned_ns": perf_counter_ns(),
            "exit_code": None,
            "exit_observed_ns": None,
            "kill_called": False,
            "wait_timeouts": 0,
            "terminate_called": False,
        }
        CHILDREN[child_sample] = fact
        proc._startup_trace_child = fact

    @wraps(original_wait)
    def wait(proc, *args, **kwargs):
        fact = getattr(proc, "_startup_trace_child", None)
        if fact is None:
            return original_wait(proc, *args, **kwargs)
        try:
            code = original_wait(proc, *args, **kwargs)
        except module.TimeoutExpired:
            fact["wait_timeouts"] += 1
            raise
        fact["exit_code"] = code
        if fact["exit_observed_ns"] is None:
            fact["exit_observed_ns"] = perf_counter_ns()
        return code

    @wraps(original_kill)
    def kill(proc):
        fact = getattr(proc, "_startup_trace_child", None)
        if fact is not None:
            fact["kill_called"] = True
        return original_kill(proc)

    module.Popen.__init__ = init
    module.Popen.wait = wait
    module.Popen.kill = kill


def observe_recovery_parent(label, args, kwargs, original):
    data_root = args[0].data_root if label.startswith("tianshu.ipc.Client.") else args[0]
    sample = Path(data_root).name
    if sample not in RECOVERY_SCENARIOS:
        return original(*args, **kwargs)
    if label == "tianshu.ipc.Client.call" and (len(args) < 2 or args[1] != "run.status"):
        return original(*args, **kwargs)
    facts = RECOVERY.setdefault(sample, {})
    key = label.rsplit(".", 1)[-1]
    observations = facts.setdefault(key, {"first_started_ns": perf_counter_ns(), "calls": 0})
    observations["calls"] += 1
    result = original(*args, **kwargs)
    ended = perf_counter_ns()
    observations.setdefault("first_completed_ns", ended)
    observations["last_completed_ns"] = ended
    if (key == "probe" and result) or (
        key == "call"
        and result["run"]["state"] == ("reconciling" if sample == "r2" else "succeeded")
    ):
        observations.setdefault("first_satisfied_ns", ended)
    if key == "request_shutdown":
        observations["sent"] = result
    return result


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
    recovery_parent = label in {
        "tianshu.ipc.probe",
        "tianshu.ipc.Client.connect",
        "tianshu.ipc.Client.call",
        "tianshu.daemon.request_shutdown",
    }
    quiet_elsewhere = recovery_parent and label != "tianshu.ipc.Client.connect"
    metric = label == "tianshu.metrics.MetricsRecorder.record_duration_ms"
    termination = label == "tianshu.host.DaemonSupervisor._terminate_owned"

    @wraps(original)
    def traced(*args, **kwargs):
        if ROLE == "measure" and recovery_parent:
            return observe_recovery_parent(label, args, kwargs, original)
        if quiet_elsewhere:
            return original(*args, **kwargs)
        if termination and (fact := getattr(args[1], "_startup_trace_child", None)) is not None:
            fact["terminate_called"] = True
        if (
            metric
            and ROLE == "recovery-daemon"
            and len(args) > 2
            and args[1] == "restart_recover_ms"
        ):
            METRICS.append({"value_ms": args[2], "runs_scanned": kwargs.get("runs_scanned")})
        if detail and not getattr(CONTEXT, "in_setup", False):
            return original(*args, **kwargs)
        span = label
        if sql:
            command = args[1] if len(args) >= 2 else ""
            if command in {
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
                "PRAGMA busy_timeout=5000",
                "BEGIN IMMEDIATE",
                "COMMIT",
                "ROLLBACK",
            }:
                span = (
                    f"sqlite:{command.split()[1].split('=')[0]}"
                    if command.startswith("PRAGMA ")
                    else f"sqlite:{command}"
                )
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
    popen = name == "subprocess" and ROLE in {"host", "measure"}
    if (name not in PHASES and not popen) or name in SEEN:
        return LOAD(name, import_)
    started = perf_counter_ns()
    cpu = thread_time_ns()
    loaded = LOAD(name, import_)
    if name not in SEEN:
        SEEN.add(name)
        if popen:
            instrument_popen(loaded)
            return loaded
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
    if not SPANS and not CHILDREN:
        return
    root = Path(os.environ["TIANSHU_STARTUP_TIMING_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    identity = f"{os.getpid()}-{TRACE_STARTED}-{uuid.uuid4().hex}"
    output = root / f"{identity}.json"
    partial = root / f"{identity}.tmp"
    with partial.open("xb") as stream:
        stream.write(
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
                        "children": CHILDREN,
                        "recovery": RECOVERY,
                        "metrics": METRICS,
                    },
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
    partial.rename(output)


ROLE, SAMPLE = role_and_sample(sys.orig_argv)
if ROLE:
    atexit.register(write)
    _bootstrap._find_and_load = traced_import
