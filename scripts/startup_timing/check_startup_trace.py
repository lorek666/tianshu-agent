"""Verify and summarize trace spans for every retained startup sample."""

import argparse
import json
from functools import partial
from pathlib import Path

from trace_startup_detailed import RECOVERY_SCENARIOS


def exactly_one(items, label):
    if len(items) != 1:
        raise ValueError(f"expected one {label}, got {len(items)}")
    return items[0]


def spans(record, label):
    return sorted(
        ((start, end, cpu) for name, start, end, cpu in record["spans"] if name == label),
        key=lambda span: span[0],
    )


def timing(span):
    start, end, cpu = span
    return {"wall_ms": round((end - start) / 1e6, 3), "thread_cpu_ms": round(cpu / 1e6, 3)}


def summarize(records, sample, phase):
    host = exactly_one(
        [r for r in records if r["sample"] == sample and r["role"] == "host"], f"{sample} Host"
    )
    daemon = exactly_one(
        [r for r in records if r["sample"] == sample and r["role"] == "daemon"], f"{sample} Daemon"
    )
    if daemon["ppid"] != host["pid"]:
        raise ValueError(f"{sample} Daemon parent mismatch")
    fact = host["children"][sample]
    check_child(fact, daemon)
    helper_label = "tianshu.host._daemon_helper:snapshot"
    host_snapshot = exactly_one(
        spans(host, "tianshu.host.DaemonSupervisor._run_daemon_helper:snapshot"), "Host snapshot"
    )
    helper = exactly_one(
        [
            r
            for r in records
            if r["role"] == "helper"
            and r["ppid"] == host["pid"]
            and spans(r, helper_label)
            and host_snapshot[0] <= r["trace_started_ns"] <= host_snapshot[1]
        ],
        f"{sample} snapshot helper",
    )
    child_snapshot = exactly_one(spans(helper, helper_label), "helper snapshot")
    if not host_snapshot[0] <= child_snapshot[0] <= child_snapshot[1] <= host_snapshot[1]:
        raise ValueError(f"{sample} snapshot PID or clock mismatch")
    setup, detail = setup_spans(daemon, sample)
    return {
        "sample": sample,
        "gate_phase": phase,
        "daemon_setup": timing(setup),
        "setup_spans": detail,
        "host_snapshot": timing(host_snapshot),
        "helper_snapshot": timing(child_snapshot),
        "pids": [host["pid"], daemon["pid"], helper["pid"]],
        "span_counts": [len(r["spans"]) for r in (host, daemon, helper)],
        "daemon_lifecycle": fact,
    }


def check_child(fact, process):
    observed = fact["exit_observed_ns"]
    if (
        fact["pid"] != process["pid"]
        or observed is None
        or not (fact["spawn_started_ns"] <= process["trace_started_ns"] <= observed)
    ):
        raise ValueError(f"{process['sample']} Daemon does not match parent lifecycle")


def setup_spans(daemon, sample):
    setup = exactly_one(spans(daemon, "tianshu.daemon._Daemon._setup"), "Daemon setup")
    detail = {
        name: [timing((begin, end, cpu)) for begin, end, cpu in spans(daemon, name)]
        for name in sorted(
            {
                name
                for name, begin, end, cpu in daemon["spans"]
                if setup[0] <= begin <= end <= setup[1]
            }
        )
    }
    if (
        len(detail.get("tianshu.daemon._open_event_log", [])) != 3
        or len(detail.get("tianshu.daemon.build_stack", [])) != 2
    ):
        raise ValueError(f"{sample} missing setup boundaries")
    if len(detail.get("sqlite:journal_mode", [])) != 3 or not detail.get(
        "tianshu.maintenance.MaintenanceConnection.executescript"
    ):
        raise ValueError(f"{sample} missing SQLite trace")
    return setup, detail


def recover_summary(records, measure, sample, gate_ms):
    process = exactly_one(
        [p for p in records if p["role"] == "recovery-daemon" and p["sample"] == sample],
        f"{sample} recovery Daemon",
    )
    fact = measure["children"][sample]
    if process["ppid"] != measure["pid"]:
        raise ValueError(f"{sample} recovery Daemon parent mismatch")
    check_child(fact, process)
    metrics = exactly_one(process["metrics"], f"{sample} recover metric")
    setup, detail = setup_spans(process, sample)
    scan = exactly_one(
        spans(process, "tianshu.daemon._Daemon._recover_startup"), f"{sample} recover scan"
    )
    parent = measure["recovery"][sample]
    start = fact["spawn_started_ns"]
    endpoint = parent["probe"]["first_satisfied_ns"]
    connected = parent["connect"]["first_completed_ns"]
    terminal = parent["call"]["first_satisfied_ns"]
    shutdown_requested = parent["request_shutdown"]["first_started_ns"]
    if not start <= scan[0] <= scan[1] <= endpoint <= connected <= terminal <= shutdown_requested:
        raise ValueError(f"{sample} recovery phase clocks are inconsistent")
    observed_ms = (terminal - start) / 1e6
    if observed_ms > gate_ms + 0.001:
        raise ValueError(f"{sample} recovery observation exceeds its raw gate duration")
    return {
        "sample": sample,
        "scenario": RECOVERY_SCENARIOS[sample],
        "gate_ms": gate_ms,
        "spawn_to_trace_ms": round((process["trace_started_ns"] - start) / 1e6, 3),
        "trace_to_setup_ms": round((setup[0] - process["trace_started_ns"]) / 1e6, 3),
        "setup": timing(setup),
        "setup_spans": detail,
        "recover_scan": timing(scan),
        "original_recover_metric": metrics,
        "scan_end_to_endpoint_ms": round((endpoint - scan[1]) / 1e6, 3),
        "endpoint_to_connected_ms": round((connected - endpoint) / 1e6, 3),
        "connected_to_target_state_ms": round((terminal - connected) / 1e6, 3),
        "parent_span_ms": round(observed_ms, 3),
        "parent_observations": parent,
        "lifecycle": fact,
        "span_count": len(process["spans"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_directory", type=Path)
    parser.add_argument("phase_file", type=Path)
    parser.add_argument("raw_samples", type=Path)
    parser.add_argument("slo_report", type=Path, nargs="?")
    args = parser.parse_args()
    records = [json.loads(path.read_bytes()) for path in args.trace_directory.glob("*.json")]
    if not records or any(r["dropped"] or len(r["spans"]) > r["limit"] for r in records):
        raise ValueError("missing process trace or span limit exceeded")
    phases = json.loads(args.phase_file.read_bytes())
    if phases["schema_version"] != "slo-startup-phases-v1" or any(
        len(phases[k]) != 3 for k in ("cold", "warm")
    ):
        raise ValueError("expected original three cold and three warm gate samples")
    raw = json.loads(args.raw_samples.read_bytes())
    if raw["schema_version"] != "slo-samples-v1" or any(
        raw["samples"][f"startup_{kind}_ms"] != [phase["task_created_ms"] for phase in phases[kind]]
        for kind in ("cold", "warm")
    ):
        raise ValueError("phase durations disagree with the raw gate samples")
    recovery_raw = raw["samples"]["restart_recovery_ms"]
    if len(recovery_raw) != 4:
        raise ValueError("expected original four recovery samples")
    measure = exactly_one([r for r in records if r["role"] == "measure"], "measure process")
    result = {"startup": [], "recovery": [], "errors": []}

    def attempt(kind, sample, action, parent):
        try:
            result[kind].append(action())
        except (KeyError, ValueError) as error:
            result["errors"].append(
                {
                    "sample": sample,
                    "error": str(error),
                    "parent_lifecycle": parent.get("children", {}).get(sample),
                }
            )

    all_startup = [("warmup_cold", None)]
    for kind in ("cold", "warm"):
        all_startup.extend((f"{kind[0]}{index}", phase) for index, phase in enumerate(phases[kind]))
    for sample, phase in all_startup:
        parent = next((r for r in records if r["role"] == "host" and r["sample"] == sample), {})
        attempt("startup", sample, partial(summarize, records, sample, phase), parent)
    for sample, ms in zip(RECOVERY_SCENARIOS, recovery_raw, strict=True):
        attempt("recovery", sample, partial(recover_summary, records, measure, sample, ms), measure)
    if args.slo_report and len(result["recovery"]) == 4:
        report = json.loads(args.slo_report.read_bytes())
        actual = round(max(r["original_recover_metric"]["value_ms"] for r in result["recovery"]), 3)
        expected = report["recovery_audit"]["daemon_internal_recover_p95_ms"]
        if actual != expected:
            result["errors"].append(
                {
                    "error": "recovery metrics do not match original p95",
                    "actual": actual,
                    "expected": expected,
                }
            )
    print(json.dumps(result, allow_nan=False, indent=2))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
