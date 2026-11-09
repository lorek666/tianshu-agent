"""M4 报告与比较的验收：安全门槛、逐次归因、可比性守卫。

这些用例用**合成的** Observation/Case，不跑真实循环——它们要证明的是报告
生成与比较逻辑本身的规则，跑真循环只会让失败原因变模糊。
"""

from __future__ import annotations

from pathlib import Path

from tianshu.eval import SUITE_VERSION, build_report, compare_reports, fixture_digest
from tianshu.eval.cases import Case
from tianshu.eval.checks import (
    Observation,
    artifact_exists,
    artifact_min_bytes,
    file_unchanged,
)
from tianshu.eval.runner import SuiteRun
from tianshu.events import Event
from tianshu.loop import RunResult

FAKE_IDENTITY = {"adapter": "fake", "vendor": None, "model": "fake-1"}


def _case(case_id: str = "X1", *, checks, category: str = "normal", fixture=None,
          task: str = "合成任务") -> Case:
    return Case(
        case_id=case_id,
        category=category,
        title=f"合成 case {case_id}",
        task=task,
        fixture=fixture if fixture is not None else {"a.md": "原始\n"},
        checks=checks,
    )


def _event(kind: str, payload: dict | None = None, seq: int = 1) -> Event:
    return Event(
        seq=seq,
        run_id="run_x",
        task_id=None,
        type=kind,
        ts=0,
        actor_kind="core",
        actor_id="",
        payload=payload or {},
        payload_hash="",
        parent_seq=None,
        idem_key=None,
    )


def _result(**overrides) -> RunResult:
    base = {
        "run_id": "run_x",
        "outcome": "succeeded",
        "effect_state": "applied",
        "state": "succeeded",
    }
    base.update(overrides)
    return RunResult(**base)


def _obs(case: Case, *, files: dict | None = None, events=(), result=None,
         run_index: int = 0) -> Observation:
    final = {"summary.md": "ok\n"} if files is None else files
    return Observation(
        case_id=case.case_id,
        category=case.category,
        run_index=run_index,
        run_id=f"run_{case.case_id}_{run_index}",
        out_name=case.out_name,
        fixture={rel: text.encode() for rel, text in case.fixture.items()},
        files={rel: text.encode() for rel, text in final.items()},
        events=list(events),
        result=result if result is not None else _result(),
    )


def _suite(obs: list[Observation], *, repeat: int = 1, version: str = SUITE_VERSION) -> SuiteRun:
    return SuiteRun(
        observations=obs,
        workdir=Path("."),
        elapsed_ms=1,
        identity=dict(FAKE_IDENTITY),
        suite_version=version,
        repeat=repeat,
    )


# ---------------------------------------------------------------- 结论


def test_verdict_pass_when_all_green():
    case = _case(checks=(artifact_exists(),))
    report = build_report(_suite([_obs(case)]), (case,))
    assert report["verdict"] == "pass"
    assert report["overall"]["cases_all_green"] == 1
    assert report["safety_gate"]["passed"] is True


def test_verdict_fail_when_non_safety_check_fails():
    case = _case(checks=(artifact_min_bytes(10),))
    report = build_report(_suite([_obs(case, files={"summary.md": "ok\n"})]), (case,))
    assert report["verdict"] == "fail"
    assert report["safety_gate"]["passed"] is True


def test_safety_gate_is_a_hard_veto():
    """一个 case 安全红 + 另一个 case 全绿 → verdict 必须是 unsafe，不是 fail。

    "fail" 会被读成"这次没做好"；"unsafe" 才是"不许拿分数换"。
    """
    unsafe_case = _case(
        "S1", category="malicious", checks=(file_unchanged("a.md"),)
    )
    green_case = _case("G1", checks=(artifact_exists(),))
    obs = [
        _obs(unsafe_case, files={"a.md": "被改过\n", "summary.md": "ok\n"}),
        _obs(green_case),
    ]
    report = build_report(_suite(obs), (unsafe_case, green_case))

    assert report["safety_gate"]["passed"] is False
    assert report["verdict"] == "unsafe"
    assert report["safety_gate"]["violations"][0]["check_id"] == "file.unchanged"
    assert report["overall"]["cases_all_green"] == 1, "其它 case 的分数照常展示"
    green = next(c for c in report["cases"] if c["case_id"] == "G1")
    assert green["pass_rate"] == 1.0


# ---------------------------------------------------------------- 归因


def _attribution_of(case: Case, obs: Observation) -> list[str]:
    report = build_report(_suite([obs]), (case,))
    entry = next(c for c in report["cases"] if c["case_id"] == case.case_id)
    return entry["attribution"]


def test_attribution_model_from_model_error_event():
    case = _case(checks=(artifact_exists(),))
    obs = _obs(case, files={}, events=[_event("model.error", {"error": "429"})])
    assert "model" in _attribution_of(case, obs)


def test_attribution_tool_from_failed_tool_result():
    case = _case(checks=(artifact_exists(),))
    obs = _obs(
        case,
        files={},
        events=[_event("tool.result", {"tool_id": "read_text", "status": "failed"})],
    )
    assert "tool" in _attribution_of(case, obs)


def test_attribution_limit_when_run_never_terminated():
    case = _case(checks=(artifact_exists(),))
    obs = _obs(
        case,
        files={},
        result=_result(outcome="", state="running"),
    )
    assert "limit" in _attribution_of(case, obs)


def test_cost_and_unknown_cost_are_never_folded():
    """F13：'花了 0 元'和'不知道花了多少'必须分开报。"""
    case = _case(checks=(artifact_exists(),))
    obs = _obs(
        case,
        result=_result(cost_total_micros=0, unknown_cost_calls=3, unknown_usage_calls=3),
    )
    report = build_report(_suite([obs]), (case,))
    run = report["cases"][0]["run_details"][0]
    assert run["cost_total_micros"] == 0
    assert run["unknown_cost_calls"] == 3
    assert run["unknown_usage_calls"] == 3


# ---------------------------------------------------------------- 可比性


def test_identical_reports_are_comparable():
    case = _case(checks=(artifact_exists(),))
    report = build_report(_suite([_obs(case)]), (case,))
    comparison = compare_reports(report, report)
    assert comparison["comparable"] is True
    assert comparison["guards"] == []
    assert comparison["summary"]["delta"] == 0.0
    assert comparison["summary"]["regressions"] == []


def test_different_suite_version_is_incomparable():
    case = _case(checks=(artifact_exists(),))
    base = build_report(_suite([_obs(case)]), (case,))
    cand = build_report(_suite([_obs(case)], version="m4.999"), (case,))
    comparison = compare_reports(base, cand)
    assert comparison["comparable"] is False
    assert any("suite_version" in guard for guard in comparison["guards"])


def test_different_repeat_is_incomparable():
    case = _case(checks=(artifact_exists(),))
    base = build_report(_suite([_obs(case)], repeat=3), (case,))
    # 报告里的 repeat 是声明值；分母不同就不许比
    cand = build_report(_suite([_obs(case)], repeat=1), (case,))
    comparison = compare_reports(base, cand)
    assert comparison["comparable"] is False
    assert any("repeat" in guard for guard in comparison["guards"])


def test_changed_case_definition_is_incomparable():
    """同 id 但 fixture/判据被改过：这不是同一条 case，分数不能比。"""
    base_case = _case("X1", checks=(artifact_exists(),), fixture={"a.md": "原始\n"})
    cand_case = _case("X1", checks=(artifact_exists(),), fixture={"a.md": "换过了\n"})
    assert fixture_digest(base_case) != fixture_digest(cand_case)
    base = build_report(_suite([_obs(base_case)]), (base_case,))
    cand = build_report(_suite([_obs(cand_case)]), (cand_case,))
    comparison = compare_reports(base, cand)
    assert comparison["comparable"] is False
    assert any("X1" in guard for guard in comparison["guards"])


def test_missing_case_in_candidate_is_incomparable():
    case_a = _case("X1", checks=(artifact_exists(),))
    case_b = _case("X2", checks=(artifact_exists(),))
    base = build_report(_suite([_obs(case_a), _obs(case_b)]), (case_a, case_b))
    cand = build_report(_suite([_obs(case_a)]), (case_a,))
    comparison = compare_reports(base, cand)
    assert comparison["comparable"] is False
    assert any("case 集合" in guard for guard in comparison["guards"])


def test_regression_points_to_failed_check_and_layer():
    case = _case(checks=(artifact_exists(),))
    base = build_report(_suite([_obs(case)]), (case,))
    broken = _obs(case, files={}, events=[_event("model.error", {"error": "429"})])
    cand = build_report(_suite([broken]), (case,))
    comparison = compare_reports(base, cand)

    assert comparison["comparable"] is True
    assert len(comparison["summary"]["regressions"]) == 1
    regression = comparison["summary"]["regressions"][0]
    assert regression["case_id"] == "X1"
    assert regression["baseline_pass_rate"] == 1.0
    assert regression["candidate_pass_rate"] == 0.0
    assert "artifact.exists" in regression["candidate_failures"]
    assert "model" in regression["candidate_attribution"]


def test_unsafe_candidate_is_not_masked_by_comparison():
    case = _case(checks=(artifact_exists(), file_unchanged("a.md")))
    base = build_report(_suite([_obs(case)]), (case,))
    cand = build_report(
        _suite([_obs(case, files={"a.md": "被改过\n", "summary.md": "ok\n"})]), (case,)
    )
    comparison = compare_reports(base, cand)
    assert comparison["summary"]["candidate_safety_gate_passed"] is False
    assert comparison["candidate"]["verdict"] == "unsafe"