"""Record and recheck the workload clock and local materials of a soak run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from itertools import pairwise
from pathlib import Path
from typing import Any

import measure_slo as slo_mod

OBSERVATION_NAME = "soak-observation.json"
NANOSECONDS = 1_000_000_000
MIN_WORKLOAD_NS = 10 * 3600 * NANOSECONDS
GIT_OBJECT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
VERDICT_SCOPE = "soak_v1_diagnostic"
RELEASE_ACCEPTANCE = "NOT_EVALUATED"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n").encode("utf-8")


def capture_source(repo: Path) -> dict[str, Any] | None:
    """Take a Git identity snapshot. Smoke runs also work from source archives."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", LC_ALL="C")
    prefix = ["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(repo)]

    def read(*args: str) -> bytes:
        return subprocess.run(
            [*prefix, *args], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=10, check=True, env=env,
        ).stdout

    try:
        if read("rev-parse", "--show-prefix").strip():
            return None
        identity = read("log", "-1", "--format=%H%n%T", "HEAD", "--").decode("ascii").split()
        if len(identity) != 2 or any(not GIT_OBJECT.fullmatch(value) for value in identity):
            return None
        dirty = bool(read("status", "--porcelain=v1", "-z", "--untracked-files=all",
                          "--ignore-submodules=none"))
        if read("rev-parse", "--verify", "HEAD").decode("ascii").strip() != identity[0]:
            return None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return {"head": identity[0], "tree": identity[1], "dirty": dirty}


def _source_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"head", "tree", "dirty"}
        and type(value["dirty"]) is bool
        and all(isinstance(value[k], str) and GIT_OBJECT.fullmatch(value[k])
                for k in ("head", "tree"))
    )


def measure_workload_window(
    window: dict[str, int], restart_intervals_ns: list[list[int]],
) -> tuple[int, bool]:
    """Subtract non-overlapping restart windows before testing the duration floor."""
    keys = {"start_elapsed_ns", "end_elapsed_ns", "requested_ns"}
    if not isinstance(window, dict) or set(window) != keys or any(
        type(value) is not int or value <= 0 for value in window.values()
    ):
        raise ValueError("invalid_workload_window")
    start = window["start_elapsed_ns"]
    end = window["end_elapsed_ns"]
    if start >= end:
        raise ValueError("invalid_workload_window")
    if not isinstance(restart_intervals_ns, list):
        raise ValueError("invalid_restart_intervals")
    prev_end = start
    excluded_ns = 0
    for interval in restart_intervals_ns:
        if (
            not isinstance(interval, list) or len(interval) != 2
            or any(type(t) is not int for t in interval)
            or not prev_end <= interval[0] < interval[1] <= end
        ):
            raise ValueError("invalid_restart_intervals")
        excluded_ns += interval[1] - interval[0]
        prev_end = interval[1]
    workload_ns = end - start - excluded_ns
    return workload_ns, workload_ns >= max(MIN_WORKLOAD_NS, window["requested_ns"])


def build_observation(
    *,
    window: dict[str, int],
    restart_intervals_ns: list[list[int]],
    source_start: dict[str, Any] | None,
    source_end: dict[str, Any] | None,
    thresholds_sha256: str,
    policy_version: int,
    report_bytes: bytes,
    markdown_bytes: bytes,
    samples_bytes: bytes,
    schema_defs: dict[str, Any],
) -> dict[str, Any]:
    """Derive diagnostic coverage from exact report and JSONL bytes."""
    workload_ns, duration_met = measure_workload_window(window, restart_intervals_ns)
    start = window["start_elapsed_ns"]
    end = window["end_elapsed_ns"]
    requested = window["requested_ns"]
    if any(value is not None and not _source_valid(value) for value in (source_start, source_end)):
        raise ValueError("invalid_source_identity")
    report = json.loads(report_bytes)
    if slo_mod.validate_against_def(schema_defs, "soakReport", report):
        raise ValueError("invalid_soak_report")
    if report["policy_version"] != policy_version:
        raise ValueError("observation_policy_mismatch")
    if (report["verdict"] == "PASS") != (not report["failures"]):
        raise ValueError("observation_verdict_mismatch")
    if len(restart_intervals_ns) != report["workloads"]["graceful_restarts"]:
        raise ValueError("restart_count_mismatch")
    if not samples_bytes.endswith(b"\n"):
        raise ValueError("incomplete_sample_journal")
    samples = [json.loads(line) for line in samples_bytes.splitlines()]
    if len(samples) != report["samples_count"]:
        raise ValueError("sample_count_mismatch")
    if any(slo_mod.validate_against_def(schema_defs, "soakSample", row) for row in samples):
        raise ValueError("invalid_soak_sample")
    elapsed = [row["elapsed_s"] for row in samples]
    if any(a > b for a, b in pairwise(elapsed)):
        raise ValueError("unordered_sample_journal")
    # The legacy JSONL serializes milliseconds. Do not use its rounded values
    # to decide whether the requested observation duration was reached.
    if (
        elapsed[0] != round(start / NANOSECONDS, 3)
        or elapsed[-1] != round(end / NANOSECONDS, 3)
        or report["requested_duration_s"] != round(requested / NANOSECONDS, 3)
        or report["actual_duration_s"] < elapsed[-1]
    ):
        raise ValueError("sample_window_mismatch")
    phase_rows = {"idle": 0, "active": 0}
    ready_rows = {"idle": 0, "active": 0}
    for row in samples:
        phase = row["phase"]
        phase_rows[phase] += 1
        if (
            all(type(row[key]) is int and row[key] > 0 for key in ("host_pid", "daemon_pid"))
            and type(row["event_seq_head"]) is int
            and row["event_seq_head"] >= 0
        ):
            ready_rows[phase] += 1
    issues = []
    if not duration_met:
        issues.append("minimum_workload_duration_unmet")
    if source_start is None or source_start["dirty"] or source_end != source_start:
        issues.append("source_identity_unconfirmed")
    if not all(ready_rows.values()):
        issues.append("missing_connected_phase_samples")
    if report["verdict"] != "PASS":
        issues.append("soak_v1_gate_failed")
    return {
        "schema_version": "soak-observation-v1",
        "release_acceptance": RELEASE_ACCEPTANCE,
        "window": window,
        "restart_intervals_ns": restart_intervals_ns,
        "workload_duration_ns": workload_ns,
        "duration_requirement_met": duration_met,
        "source_start": source_start,
        "source_end": source_end,
        "thresholds_sha256": thresholds_sha256,
        "report_sha256": sha256_bytes(report_bytes),
        "markdown_sha256": sha256_bytes(markdown_bytes),
        "samples_sha256": sha256_bytes(samples_bytes),
        "samples": {
            "count": len(samples),
            "idle_rows": phase_rows["idle"],
            "active_rows": phase_rows["active"],
            "ready_idle_rows": ready_rows["idle"],
            "ready_active_rows": ready_rows["active"],
            "max_gap_s": round(max((b - a for a, b in pairwise(elapsed)), default=0), 3),
        },
        "issues": issues,
    }


def completion_fields(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "completed",
        "progress_pct": 100.0,
        "verdict_scope": VERDICT_SCOPE,
        "release_acceptance": RELEASE_ACCEPTANCE,
        **{key: report[key] for key in (
            "verdict", "started_at", "finished_at", "actual_duration_s", "requested_duration_s",
            "cycles_completed", "samples_count", "workloads", "orphan_pids", "failures",
        )},
    }


def verify_observation(
    output_dir: Path, *, thresholds_sha256: str, policy_version: int, schema_defs: dict[str, Any],
) -> dict[str, Any]:
    """Check local consistency, not release readiness or the provenance of the originals."""
    status = json.loads((output_dir / "status.json").read_bytes())
    if not isinstance(status, dict) or status.get("state") != "completed":
        raise ValueError("observation_not_completed")
    raw_observation = (output_dir / OBSERVATION_NAME).read_bytes()
    if status.get("observation_sha256") != hashlib.sha256(raw_observation).hexdigest():
        raise ValueError("observation_checksum_mismatch")
    observation = json.loads(raw_observation)
    if not isinstance(observation, dict):
        raise ValueError("invalid_observation")
    if observation.get("thresholds_sha256") != thresholds_sha256:
        raise ValueError("observation_thresholds_mismatch")
    report_bytes = (output_dir / "soak-report.json").read_bytes()
    rechecked = build_observation(
        window=observation.get("window"),
        restart_intervals_ns=observation.get("restart_intervals_ns"),
        source_start=observation.get("source_start"),
        source_end=observation.get("source_end"),
        thresholds_sha256=thresholds_sha256,
        policy_version=policy_version,
        report_bytes=report_bytes,
        markdown_bytes=(output_dir / "soak-report.md").read_bytes(),
        samples_bytes=(output_dir / "samples.jsonl").read_bytes(),
        schema_defs=schema_defs,
    )
    if json_bytes(rechecked) != json_bytes(observation):
        raise ValueError("observation_material_mismatch")
    report_status = completion_fields(json.loads(report_bytes))
    if json_bytes({k: status.get(k) for k in report_status}) != json_bytes(report_status):
        raise ValueError("observation_status_mismatch")
    return rechecked
