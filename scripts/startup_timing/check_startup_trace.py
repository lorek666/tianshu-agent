"""Verify and summarize trace spans for every retained startup sample."""

import argparse
import json
from pathlib import Path


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
    helper_label = "tianshu.host._daemon_helper:snapshot"
    helper = exactly_one(
        [
            r
            for r in records
            if r["role"] == "helper" and r["ppid"] == host["pid"] and spans(r, helper_label)
        ],
        f"{sample} snapshot helper",
    )
    host_snapshot = exactly_one(
        spans(host, "tianshu.host.DaemonSupervisor._run_daemon_helper:snapshot"), "Host snapshot"
    )
    setup = exactly_one(spans(daemon, "tianshu.daemon._Daemon._setup"), "Daemon setup")
    child_snapshot = exactly_one(spans(helper, helper_label), "helper snapshot")
    if not host_snapshot[0] <= child_snapshot[0] <= child_snapshot[1] <= host_snapshot[1]:
        raise ValueError(f"{sample} snapshot PID or clock mismatch")
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
    return {
        "sample": sample,
        "gate_phase": phase,
        "daemon_setup": timing(setup),
        "setup_spans": detail,
        "host_snapshot": timing(host_snapshot),
        "helper_snapshot": timing(child_snapshot),
        "pids": [host["pid"], daemon["pid"], helper["pid"]],
        "span_counts": [len(r["spans"]) for r in (host, daemon, helper)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_directory", type=Path)
    parser.add_argument("phase_file", type=Path)
    parser.add_argument("raw_samples", type=Path, nargs="?")
    args = parser.parse_args()
    records = [json.loads(path.read_bytes()) for path in args.trace_directory.glob("*.json")]
    if not records or any(r["dropped"] or len(r["spans"]) > r["limit"] for r in records):
        raise ValueError("missing process trace or span limit exceeded")
    phases = json.loads(args.phase_file.read_bytes())
    if phases["schema_version"] != "slo-startup-phases-v1" or any(
        len(phases[k]) != 3 for k in ("cold", "warm")
    ):
        raise ValueError("expected original three cold and three warm gate samples")
    if args.raw_samples:
        raw = json.loads(args.raw_samples.read_bytes())
        if raw["schema_version"] != "slo-samples-v1" or any(
            raw["samples"][f"startup_{kind}_ms"]
            != [phase["task_created_ms"] for phase in phases[kind]]
            for kind in ("cold", "warm")
        ):
            raise ValueError("phase durations disagree with the raw gate samples")
    rows = [summarize(records, "warmup_cold", None)]
    for kind in ("cold", "warm"):
        rows.extend(
            summarize(records, f"{kind[0]}{index}", phase)
            for index, phase in enumerate(phases[kind])
        )
    print(json.dumps(rows, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
