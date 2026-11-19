"""Developer delivery evidence: actual fixture actions, honest sample accounting."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tianshu.eval import REAL_USER_CASES, build_report, run_real_user_tasks
from tianshu.eval.cases import Case
from tianshu.eval.checks import Observation, artifact_exists, harness_error_free, no_unknown_effect
from tianshu.eval.report import MIN_REPEAT
from tianshu.eval.runner import SuiteRun
from tianshu.loop import RunResult

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_task_delivery.py"


@pytest.fixture
def delivery():
    assert SCRIPT.is_file(), "task delivery measurement tool is not implemented"
    spec = importlib.util.spec_from_file_location("measure_task_delivery_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample_suite(tmp_path):
    case = Case(
        case_id="DELIVERY-FIXTURE",
        category="real_user:local",
        title="Delivery accounting fixture",
        task="Write a summary",
        scope="real_user",
        checks=(artifact_exists(), harness_error_free(), no_unknown_effect()),
    )
    suite = SuiteRun(
        observations=[
            Observation(
                case_id=case.case_id,
                category=case.category,
                run_index=index,
                run_id=f"sample-{index}",
                out_name=case.out_name,
                files={case.out_name: b"test summary"},
                result=RunResult(
                    run_id=f"sample-{index}",
                    outcome="succeeded",
                    state="succeeded",
                    effect_state="none_applied",
                    unknown_cost_calls=2,
                ),
            )
            for index in range(MIN_REPEAT)
        ],
        workdir=tmp_path,
        elapsed_ms=1,
        identity={"adapter": "scripted", "model": "harness-scripted", "vendor": None},
        suite_version="delivery-test",
        repeat=MIN_REPEAT,
    )
    return case, suite


def test_failures_remain_in_valid_denominator_and_invalid_samples_do_not(delivery, sample_suite):
    case, suite = sample_suite
    suite.observations[1] = replace(
        suite.observations[1], files={}, error="test-only private exception detail"
    )
    suite.observations[2] = replace(
        suite.observations[2], invalid_reason="取证失败：test-only private path", result=None
    )
    report = delivery.build_task_delivery_report(suite, cases=(case,))
    cat = report["categories"][case.category]
    assert (cat["successful_runs"], cat["valid_runs"], cat["failed_runs"]) == (1, 2, 1)
    assert cat["invalid_runs"] == 1
    assert cat["success_rate"] == 0.5
    assert report["status"] != "pass"
    ok, failed, invalid = report["cases"][0]["runs"]
    assert [row["status"] for row in (ok, failed, invalid)] == ["passed", "failed", "invalid"]
    assert {check["check_id"] for check in failed["failed_checks"]} == {
        "artifact.exists", "harness.no_error",
    }
    assert failed["primary_cost"] == {"known_micros": 0, "unknown_calls": 2}
    assert invalid["invalid_reason"] == "evidence_collection_failed"
    assert invalid["primary_cost"] == {"known_micros": None, "unknown_calls": None}
    serialized = json.dumps(report)
    assert "test-only private" not in serialized
    assert "test summary" not in serialized


def test_partial_validity_cannot_inherit_c5_pass(delivery, sample_suite):
    case, suite = sample_suite
    suite.observations[2] = replace(suite.observations[2], invalid_reason="取证失败：fixture")
    assert build_report(suite, (case,))["verdict"] == "pass"
    report = delivery.build_task_delivery_report(suite, cases=(case,))
    assert report["c5"]["verdict"] == "pass"
    assert report["status"] == "incomplete"
    assert report["categories"][case.category]["invalid_runs"] == 1


def test_unavailable_has_no_delivery_denominator(delivery, sample_suite):
    case, suite = sample_suite
    suite.observations = [replace(obs, unavailable="fixture not run") for obs in suite.observations]
    report = delivery.build_task_delivery_report(suite, cases=(case,))
    category = report["categories"][case.category]
    assert category["successful_runs"] == category["valid_runs"] == category["invalid_runs"] == 0
    assert category["unavailable_runs"] == MIN_REPEAT
    assert category["success_rate"] is None
    assert report["status"] == "incomplete"


def test_c5_safety_veto_survives_partial_evidence(delivery, sample_suite):
    case, suite = sample_suite
    obs = suite.observations[0]
    suite.observations[0] = replace(
        obs, result=replace(obs.result, effect_state="unknown", n_unknown=1)
    )
    suite.observations[1] = replace(suite.observations[1], invalid_reason="取证失败：fixture")
    report = delivery.build_task_delivery_report(suite, cases=(case,))
    assert report["status"] == "unsafe"
    assert report["c5"]["safety_gate"]["passed"] is False
    assert report["c5"]["safety_gate"]["violations"][0]["run_index"] == 0


@pytest.mark.parametrize("bad_input", ["missing", "duplicate", "real-model"])
def test_rejects_misidentified_or_incomplete_inputs(delivery, sample_suite, bad_input):
    case, suite = sample_suite
    if bad_input == "missing":
        suite.observations.pop()
    elif bad_input == "duplicate":
        suite.observations[-1] = suite.observations[0]
    else:
        suite.identity = {"adapter": "anthropic", "model": "test-only", "vendor": None}
    with pytest.raises(ValueError):
        delivery.build_task_delivery_report(suite, cases=(case,))


def test_counts_captured_fixture_actions_not_requests(delivery, tmp_path, monkeypatch):
    delivery.pin_local_fixture_inputs(monkeypatch)
    case_ids = {"RU-C2", "RU-C3", "RU-M1"}
    selected = tuple(case for case in REAL_USER_CASES if case.case_id in case_ids)
    suite = run_real_user_tasks(tmp_path / "action-observation", repeat=1, cases=selected)
    samples = {obs.case_id: obs for obs in suite.observations}
    approved = samples["RU-C3"]
    rejected = samples["RU-C2"]
    memory = samples["RU-M1"]
    assert approved.events_of("approval.requested") and rejected.events_of("approval.requested")
    assert delivery.observed_fixture_actions(rejected)["approvals_rejected"] == 1
    assert delivery.observed_fixture_actions(approved) == {
        "approvals_approved": 1,
        "approvals_rejected": 0,
        "memory_confirmed": 0,
        "simulated_manual_edits": 1,
        "snapshot_undo_preflights": 1,
    }
    assert delivery.observed_fixture_actions(memory)["memory_confirmed"] == 1
    no_decision = replace(approved, events=approved.events_of("approval.requested"), post_run={})
    assert not any(delivery.observed_fixture_actions(no_decision).values())
    repeated_evidence = replace(approved, subtask_events=list(approved.events))
    assert delivery.observed_fixture_actions(repeated_evidence)["approvals_approved"] == 1
    no_confirmation = replace(memory, post_run={"confirmed": True})
    assert delivery.observed_fixture_actions(no_confirmation)["memory_confirmed"] == 0


@pytest.mark.parametrize("repeat", ["2", "3"])
def test_launcher_rejects_low_repeat_or_existing_output(delivery, tmp_path, repeat, monkeypatch):
    output = tmp_path / "delivery.json"
    if repeat == "3":
        output.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exc:
        delivery.main(["--out", str(output), "--repeat", repeat])
    assert exc.value.code == 2
    if repeat == "3":
        assert output.read_text(encoding="utf-8") == "existing"


def test_launcher_filters_configuration_and_requires_report(delivery, tmp_path, monkeypatch):
    output = tmp_path / "delivery.json"
    for key in ("TIANSHU_EVAL_REAL_MODEL", "TIANSHU_EMBEDDING_PROVIDER", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fixture-only")

    def success_without_report(args, *, cwd, env, check):
        assert args[-1] == delivery.MEASUREMENT_TEST
        assert cwd == delivery.ROOT and not check
        assert env[delivery.OUTPUT_ENV] == str(output)
        assert env[delivery.REPEAT_ENV] == str(MIN_REPEAT)
        assert set(env).isdisjoint({
            "TIANSHU_EVAL_REAL_MODEL", "TIANSHU_EMBEDDING_PROVIDER", "OPENAI_API_KEY",
        })
        return subprocess.CompletedProcess(args, returncode=0)

    monkeypatch.setattr(subprocess, "run", success_without_report)
    assert delivery.main(["--out", str(output)]) != 0


def test_record_frozen_task_delivery(delivery, tmp_path, monkeypatch):
    output = os.environ.get(delivery.OUTPUT_ENV)
    if output is None:
        pytest.skip("从 scripts/measure_task_delivery.py 显式启动冻结集复跑")
    delivery.pin_local_fixture_inputs(monkeypatch)
    repeat = int(os.environ[delivery.REPEAT_ENV])
    assert repeat >= MIN_REPEAT
    suite = run_real_user_tasks(tmp_path / "frozen-tasks", repeat=repeat)
    report = delivery.build_task_delivery_report(suite)
    # 红色样本也先写证据；断言只是开发入口的退出门禁。
    with Path(output).open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    assert report["status"] == "pass", f"任务夹具测量：{report['status']}，逐次证据见 {output}"
    assert set(report["categories"]) == {case.category for case in REAL_USER_CASES}
    assert report["model"]["real_model"] == "NOT_RUN"
    assert report["human_takeover"] == "NOT_OBSERVED"
