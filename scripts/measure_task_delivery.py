#!/usr/bin/env python3
"""Measure frozen real-user scenarios with scripted models and local fixtures only."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import Counter, defaultdict
from itertools import chain
from pathlib import Path
from typing import Any

from tianshu.eval import REAL_USER_CASES, build_report
from tianshu.eval.cases import Case
from tianshu.eval.checks import Observation, memory_confirmed_by_human
from tianshu.eval.report import MIN_REPEAT
from tianshu.eval.runner import SuiteRun

ROOT = Path(__file__).resolve().parents[1]
MEASUREMENT_TEST = "tests/test_measure_task_delivery.py::test_record_frozen_task_delivery"
OUTPUT_ENV = "TIANSHU_TASK_DELIVERY_OUTPUT"
REPEAT_ENV = "TIANSHU_TASK_DELIVERY_REPEAT"
ACTION_KEYS = (
    "approvals_approved",
    "approvals_rejected",
    "memory_confirmed",
    "simulated_manual_edits",
    "snapshot_undo_preflights",
)
INVALID_REASON_CODES = {
    "取证失败": "evidence_collection_failed",
    "第二段取证失败": "followup_evidence_collection_failed",
    "来源离线核验失败": "offline_source_verification_failed",
    "评测基础设施无效": "assessment_infrastructure_failed",
}


def pin_local_fixture_inputs(monkeypatch: Any) -> None:
    """For the pytest node only; keep its already-installed test isolation."""
    for key in tuple(os.environ):
        if key in {"TIANSHU_MCP_CONFIG", "TIANSHU_FAILPOINT", "TIANSHU_FAILPOINT_PATH"} or (
            key.startswith("TIANSHU_EMBEDDING_")
        ):
            monkeypatch.delenv(key)


def observed_fixture_actions(obs: Observation) -> dict[str, int]:
    """Count recorded substitute-operator actions, not requests or actual people."""
    actions = dict.fromkeys(ACTION_KEYS, 0)
    followup_events = () if obs.followup is None else obs.followup.events
    seen: set[tuple[str, int]] = set()
    for event in chain(obs.events, obs.subtask_events, followup_events):
        if event.type != "approval.decided" or event.actor_kind != "human":
            continue
        decision = event.payload.get("decision")
        key = (event.run_id, event.seq)
        if key not in seen and decision in ("approved", "rejected"):
            seen.add(key)
            actions[f"approvals_{decision}"] += 1

    # Confirmation runs separately from these task event streams. Use the frozen
    # case check's control-plane result, not an inferred approval or fixture setup.
    confirmed, _ = memory_confirmed_by_human().fn(obs)
    actions["memory_confirmed"] = int(confirmed)
    post = obs.post_run
    actions["simulated_manual_edits"] = post.get("manual_edits", 0)
    actions["snapshot_undo_preflights"] = post.get("preflights", 0)
    return actions


def _run_row(run: dict[str, Any], obs: Observation) -> dict[str, Any]:
    invalid_reason = None
    if run["invalid"]:
        status = "invalid"
        invalid_reason = INVALID_REASON_CODES.get(
            run["invalid_reason"].partition("：")[0], "assessment_infrastructure_failed"
        )
    elif run["unavailable"] is not None:
        status = "unavailable"
    else:
        status = "passed" if run["passed"] else "failed"
    return {
        "run_index": run["run_index"],
        "status": status,
        "outcome": run["outcome"],
        "effect_state": run["effect_state"],
        "invalid_reason": invalid_reason,
        "failed_checks": [
            {"check_id": check["check_id"], "kind": check["kind"]}
            for check in run["failed_checks"]
        ] if status == "failed" else [],
        "primary_cost": {
            "known_micros": run["cost_total_micros"],
            "unknown_calls": run["unknown_cost_calls"],
        },
        "observed_fixture_actions": observed_fixture_actions(obs),
    }


def _category_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["status"] for row in rows)
    valid = counts["passed"] + counts["failed"]
    return {
        "successful_runs": counts["passed"],
        "valid_runs": valid,
        "failed_runs": counts["failed"],
        "invalid_runs": counts["invalid"],
        "unavailable_runs": counts["unavailable"],
        "success_rate": counts["passed"] / valid if valid else None,
        "observed_fixture_actions_on_valid_runs": {
            key: sum(
                row["observed_fixture_actions"][key]
                for row in rows
                if row["status"] in ("passed", "failed")
            )
            for key in ACTION_KEYS
        },
    }


def build_task_delivery_report(
    suite: SuiteRun, *, cases: tuple[Case, ...] = REAL_USER_CASES
) -> dict[str, Any]:
    """Project the existing C5 acceptance decisions into a separate developer report."""
    if suite.identity.get("adapter") != "scripted":
        raise ValueError("本工具只接受脚本模型证据，不能给真实模型打夹具标签")
    categories_by_id = {case.case_id: case.category for case in cases}
    observations = {(obs.case_id, obs.run_index): obs for obs in suite.observations}
    expected = {(case.case_id, index) for case in cases for index in range(suite.repeat)}
    if (
        not cases or len(categories_by_id) != len(cases)
        or len(observations) != len(suite.observations) or observations.keys() != expected
        or any(obs.category != categories_by_id[obs.case_id] for obs in suite.observations)
    ):
        raise ValueError("任务观察记录缺失、重复或不匹配，不能产出可比报告")
    c5 = build_report(suite, cases)
    rows_by_category = defaultdict(list)
    case_entries = []
    for case in c5["cases"]:
        case_id = case["case_id"]
        rows = [
            _run_row(run, observations[case_id, run["run_index"]])
            for run in case["run_details"]
        ]
        rows_by_category[case["category"]].extend(rows)
        case_entries.append({
            key: case[key] for key in ("case_id", "category", "version", "fixture_digest")
        } | {"runs": rows})
    categories = {key: _category_summary(rows) for key, rows in sorted(rows_by_category.items())}
    gate = c5["safety_gate"]
    if not gate["passed"]:
        status = "unsafe"
    elif any(cat["invalid_runs"] or cat["unavailable_runs"] for cat in categories.values()):
        status = "incomplete"
    else:
        status = c5["verdict"]
    return {
        "schema": "task-delivery-v1",
        "suite_version": c5["suite_version"],
        "fixture_set": "real_user_tasks" if cases == REAL_USER_CASES else "selected_cases",
        "generated_at": c5["generated_at"],
        "elapsed_ms": c5["elapsed_ms"],
        "repeat": c5["repeat"],
        "model": {"mode": "scripted_fixture", "real_model": "NOT_RUN"},
        "network": {"source": "loopback_fixture", "public_web": "NOT_RUN"},
        "operator": "fixture_operator_substitute",
        "human_takeover": "NOT_OBSERVED",
        "real_model_cost": "NOT_MEASURED",
        "g3_release": "NOT_ASSESSED",
        "status": status,
        "c5": {
            "schema": c5["schema"],
            "verdict": c5["verdict"],
            "safety_gate": {
                "passed": gate["passed"],
                "violations": [
                    {key: hit[key] for key in ("case_id", "run_index", "check_id")}
                    for hit in gate["violations"]
                ],
            },
        },
        "categories": categories,
        "cases": case_entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new JSON report file")
    parser.add_argument("--repeat", type=int, default=MIN_REPEAT, help="repetitions per case (≥3)")
    args = parser.parse_args(argv)
    if args.repeat < MIN_REPEAT:
        parser.error(f"--repeat must be at least {MIN_REPEAT}")
    output = args.out.absolute()
    if os.path.lexists(output) or not output.parent.is_dir():
        parser.error("--out must be a new file in an existing directory")

    # Run through the repository's pytest isolation, never a production data root
    # or OS-user maintenance lock. No inherited runtime/provider configuration.
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("TIANSHU_", "OPENAI_", "ANTHROPIC_", "PYTEST_"))
    }
    env.update({
        OUTPUT_ENV: str(output),
        REPEAT_ENV: str(args.repeat),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "-q", MEASUREMENT_TEST],
        cwd=ROOT,
        env=env,
        check=False,
    )
    if result.returncode == 0 and not output.is_file():
        print("未生成任务交付报告；没有可用测量证据", file=sys.stderr)
        return 2
    return result.returncode if result.returncode >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
