"""四维评测与无效样本分离（PRD §10.4 / §10.2）。

四维不是"四种分数相加"：它们是**四条独立记录**
- safety：硬门槛，红 = unsafe；
- completion：完成度，红 = 任务没做完（**不得**记成不安全）；
- reliability：恢复/故障事实（recovery bucket、重复副作用、超时、取消）；
- efficiency：attempt 数、token、已知/未知费用、端到端耗时（缺数据 unknown，不是 0）。

无效样本（评测基础设施故障）不参加分子/分母；产品按契约应持久化的证据缺失
**仍是产品失败**，不许当无效剔除——那样会把"产品坏了"洗成"这次不算"。
"""

from __future__ import annotations

from pathlib import Path

from tianshu.eval import (
    CASES,
    SUITE_VERSION,
    Case,
    Observation,
    SuiteRun,
    build_report,
    run_suite,
)
from tianshu.eval.checks import (
    artifact_exists,
    file_equals,
    file_unchanged,
    receipts_for_target,
    tool_attempted,
)
from tianshu.eval.report import DIMENSIONS, PATH_COVERAGE_RULE, VALIDITY_RULE
from tianshu.events import Event
from tianshu.loop import RunResult
from tianshu.model import ScriptedModel
from tianshu.recovery import RecoveryReport

REPEAT = 3


# ---------------------------------------------------------------- 合成观察


def _event(kind: str, payload: dict, seq: int = 1) -> Event:
    return Event(
        seq=seq,
        run_id="run_x",
        task_id=None,
        type=kind,
        ts=1,
        actor_kind="core",
        actor_id="",
        payload=payload,
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
        "turns": 2,
        "model_attempts": 2,
        "tokens": 120,
        "cost_total_micros": 0,
        "unknown_cost_calls": 2,
    }
    base.update(overrides)
    return RunResult(**base)


def _case(checks, *, case_id: str = "X1", fixture=None) -> Case:
    return Case(
        case_id=case_id,
        category="normal",
        title=f"合成 case {case_id}",
        task="合成任务",
        fixture=fixture if fixture is not None else {"a.md": "原始\n"},
        checks=checks,
        scope="model",
    )


def _obs(case: Case, *, run_index: int = 0, **overrides) -> Observation:
    base = {
        "case_id": case.case_id,
        "category": case.category,
        "run_index": run_index,
        "run_id": f"run_{case.case_id}_{run_index}",
        "out_name": case.out_name,
        "fixture": {rel: text.encode() for rel, text in case.fixture.items()},
        "files": {
            **{rel: text.encode() for rel, text in case.fixture.items()},
            "summary.md": b"ok\n",
        },
        "events": [],
        "result": _result(),
        "elapsed_ms": 10 + run_index,
    }
    base.update(overrides)
    return Observation(**base)


def _suite(cases: tuple[Case, ...], obs_by_case: dict[str, list[Observation]]) -> SuiteRun:
    return SuiteRun(
        observations=[obs for case in cases for obs in obs_by_case[case.case_id]],
        workdir=Path("."),
        elapsed_ms=1,
        identity={"adapter": "scripted", "vendor": None, "model": "harness"},
        suite_version=SUITE_VERSION,
        repeat=REPEAT,
    )


def _report(cases, obs_by_case):
    return build_report(_suite(cases, obs_by_case), cases)


def _runs(case: Case, **overrides) -> list[Observation]:
    return [_obs(case, run_index=i, **overrides) for i in range(REPEAT)]


# ---------------------------------------------------------------- 四维结构


def test_report_has_stable_four_dimensions():
    """报告里必须有四个稳定的维度键，每个都带自己的依据与状态。"""
    case = _case((artifact_exists(),))
    report = _report((case,), {case.case_id: _runs(case)})
    dims = report["dimensions"]
    assert set(DIMENSIONS) == {"safety", "completion", "reliability", "efficiency"}
    assert set(dims) == set(DIMENSIONS)
    for name in ("safety", "completion", "reliability"):
        assert {"samples", "failed", "failures", "check_ids", "status"} <= set(dims[name])
        assert dims[name]["status"] in {"pass", "fail", "unknown"}
    assert {"samples", "model_attempts", "tokens", "cost_total_micros", "elapsed_ms"} <= set(
        dims["efficiency"]
    )
    assert dims["efficiency"]["status"] in {"measured", "unknown"}
    assert report["dimensions"]["safety"]["rule"]
    assert report["dimensions"]["completion"]["rule"]


def test_scripted_baseline_has_four_dimension_records(tmp_path):
    """契约基线实跑：安全/完成度全绿，可靠性有 9 个故障样本，效率已测量。"""
    suite = run_suite(
        CASES,
        model_for=lambda case: ScriptedModel(list(case.script)),
        repeat=3,
        workdir=Path(tmp_path) / "eval",
    )
    report = build_report(suite, CASES)
    dims = report["dimensions"]
    assert report["verdict"] == "pass"
    assert dims["safety"]["status"] == "pass"
    assert dims["completion"]["status"] == "pass"
    # R1–R3 是仅有的崩溃/恢复样本：3 条 case × 3 次
    assert dims["reliability"]["samples"] == 9
    assert dims["reliability"]["status"] == "pass"
    assert dims["efficiency"]["status"] == "measured"
    assert dims["efficiency"]["tokens"]["value"] == 0  # scripted 模型没有用量
    assert dims["efficiency"]["model_attempts"]["value"] is not None


# ---------------------------------------------------------------- 独立性（负控）


def test_completion_failure_is_fail_not_unsafe():
    """完成度红只让 verdict=fail；安全维度必须仍然 pass。

    负控：如果有人把完成度失败并进安全维度/门槛，这里立刻红。
    """
    case = _case((file_equals("a.md", "期望值\n"),))
    report = _report((case,), {case.case_id: _runs(case)})
    assert report["verdict"] == "fail"
    assert report["safety_gate"]["passed"] is True
    assert report["dimensions"]["safety"]["status"] == "pass"
    assert report["dimensions"]["safety"]["failed"] == 0
    # 负控锚点：完成度失败**不许**出现在安全维度的失败清单里
    assert report["dimensions"]["safety"]["failures"] == []
    assert report["dimensions"]["completion"]["status"] == "fail"
    assert report["dimensions"]["completion"]["failed"] == REPEAT


def test_safety_failure_is_unsafe_and_dimension_fails():
    case = _case((file_unchanged("a.md"),))
    changed = {"a.md": "被改过\n".encode(), "summary.md": b"ok\n"}
    report = _report((case,), {case.case_id: _runs(case, files=changed)})
    assert report["verdict"] == "unsafe"
    assert report["dimensions"]["safety"]["status"] == "fail"
    assert report["dimensions"]["safety"]["failed"] == REPEAT
    assert {
        failure["check_id"] for failure in report["dimensions"]["safety"]["failures"]
    } == {"file.unchanged"}


def test_safety_and_completion_dimensions_are_disjoint():
    """同一条判据只能落在一个维度里：两栏的 check_id 不许有交集。"""
    case = _case((artifact_exists(), file_unchanged("a.md")))
    report = _report((case,), {case.case_id: _runs(case)})
    safety_ids = set(report["dimensions"]["safety"]["check_ids"])
    completion_ids = set(report["dimensions"]["completion"]["check_ids"])
    assert not safety_ids & completion_ids
    assert report["dimensions"]["safety"]["failures"] == []


# ---------------------------------------------------------------- 无效样本


def test_invalid_samples_are_excluded_from_rates():
    """无效样本不参加分子/分母，但原因必须留在报告里。"""
    case = _case((artifact_exists(),))
    obs = _runs(case)
    obs[0].invalid_reason = "取证失败：EventLogCorrupt: events.db 损坏"
    report = _report((case,), {case.case_id: obs})
    assert report["validity"] == {
        "samples": 3,
        "valid": 2,
        "invalid": 1,
        "invalid_samples": [
            {
                "case_id": case.case_id,
                "run_index": 0,
                "run_id": obs[0].run_id,
                "reason": "取证失败：EventLogCorrupt: events.db 损坏",
            }
        ],
        "rule": VALIDITY_RULE,
    }
    case_entry = report["cases"][0]
    assert case_entry["valid_runs"] == 2
    assert case_entry["invalid_runs"] == 1
    assert case_entry["runs_count"] == 3
    assert case_entry["passes"] == 2
    assert case_entry["pass_rate"] == 1.0  # 分母是 2，不是 3
    assert report["overall"]["run_pass_rate"] == 1.0
    assert report["dimensions"]["safety"]["samples"] == 2


def test_invalid_sample_does_not_create_safety_unsafe():
    """无效样本里即便"看起来有安全违规"，也不能据此判 unsafe。"""
    case = _case((file_unchanged("a.md"),))
    obs = _runs(case, files={"a.md": "被改过\n".encode()})
    obs[0].invalid_reason = "评测基础设施无效：夹具不匹配"
    report = _report((case,), {case.case_id: obs})
    # 有效样本仍然违反 file.unchanged → 依然是 unsafe，但违规计数只算有效样本
    assert report["verdict"] == "unsafe"
    assert report["dimensions"]["safety"]["failed"] == 2
    assert {v["run_index"] for v in report["safety_gate"]["violations"]} == {1, 2}


def test_missing_product_evidence_is_a_product_failure_not_invalid():
    """产品没写 intent/result/Receipt → 产品失败，**不许**当无效剔除。"""
    case = _case((artifact_exists(), receipts_for_target("a.md", 1)))
    obs = _runs(case)
    # 模拟"产品该持久化的证据缺失"：没有 events、没有 result
    for run in obs:
        run.events = []
        run.result = None
    report = _report((case,), {case.case_id: obs})
    assert report["validity"]["invalid"] == 0
    assert all(run["invalid"] is False for run in report["cases"][0]["run_details"])
    assert report["verdict"] != "pass", "证据缺失不能洗成通过"


def test_verdict_unknown_when_no_valid_samples():
    """一个有效样本都没有：不许编 pass/fail。"""
    case = _case((artifact_exists(),))
    obs = _runs(case)
    for run in obs:
        run.invalid_reason = "子进程起不来：OSError"
    report = _report((case,), {case.case_id: obs})
    assert report["validity"]["valid"] == 0
    assert report["verdict"] == "unknown"
    assert report["dimensions"]["safety"]["status"] == "unknown"
    assert report["dimensions"]["completion"]["status"] == "unknown"


# ---------------------------------------------------------------- 可靠性与效率


def test_reliability_unknown_when_no_fault_evidence():
    """没有恢复/故障事实时是 unknown，不是 0（PRD §10.4）。"""
    case = _case((artifact_exists(),))
    report = _report((case,), {case.case_id: _runs(case)})
    reliability = report["dimensions"]["reliability"]
    assert reliability["samples"] == 0
    assert reliability["status"] == "unknown"
    assert reliability["failures"] == []


def test_reliability_records_recovery_and_duplicates():
    """恢复事实与重复副作用进可靠性维度（failures 非空）。"""
    case = _case((artifact_exists(),))
    dup_events = [
        _event(
            "tool.result",
            {
                "call_id": "c_same",
                "tool_id": "modify_text",
                "status": "succeeded",
                "effect_state": "applied",
                "output": {"path": "a.md", "sha256": "0" * 64},
            },
        ),
        _event(
            "tool.result",
            {
                "call_id": "c_same",
                "tool_id": "modify_text",
                "status": "succeeded",
                "effect_state": "applied",
                "output": {"path": "a.md", "sha256": "0" * 64},
            },
            seq=2,
        ),
    ]
    obs = _runs(case)
    obs[0].events = dup_events
    obs[1].recovery_report = RecoveryReport(run_id="run_x", unresolved=["c_write"])
    report = _report((case,), {case.case_id: obs})
    reliability = report["dimensions"]["reliability"]
    assert reliability["samples"] == 2
    assert reliability["status"] == "fail"
    assert len(reliability["failures"]) == 2
    assert reliability["facts"][0]["duplicate_tool_results"] == 1
    assert reliability["facts"][1]["recovery"]["unresolved"] == 1


def test_efficiency_unknown_is_not_zero():
    """没有 RunResult 时效率是 unknown/None，不许写成 0。"""
    case = _case((artifact_exists(),))
    obs = _runs(case)
    for run in obs:
        run.result = None
    report = _report((case,), {case.case_id: obs})
    efficiency = report["dimensions"]["efficiency"]
    assert efficiency["samples"] == 0
    assert efficiency["status"] == "unknown"
    assert efficiency["tokens"]["value"] is None
    assert efficiency["model_attempts"]["value"] is None
    assert efficiency["elapsed_ms"]["total"] is None


def test_efficiency_unknown_attempt_count_is_not_a_number():
    """attempt 账本不可信时，值为 None + unknown_samples，而不是编一个数。"""
    case = _case((artifact_exists(),))
    obs = _runs(case, result=_result(model_attempts=3, model_attempts_known=False))
    report = _report((case,), {case.case_id: obs})
    efficiency = report["dimensions"]["efficiency"]
    assert efficiency["model_attempts"]["value"] is None
    assert efficiency["model_attempts"]["unknown_samples"] == REPEAT
    # token 这类有明确数字的照常显示
    assert efficiency["tokens"]["value"] == 120 * REPEAT


def test_efficiency_separates_known_and_unknown_cost():
    """已知费用与未知费用分开记录（F13）。"""
    case = _case((artifact_exists(),))
    obs = _runs(
        case, result=_result(cost_total_micros=0, unknown_cost_calls=3)
    )
    report = _report((case,), {case.case_id: obs})
    efficiency = report["dimensions"]["efficiency"]
    assert efficiency["cost_total_micros"]["value"] == 0
    assert efficiency["unknown_cost_calls"]["value"] == 3 * REPEAT


# ---------------------------------------------------------------- path_coverage


def test_report_records_path_coverage_probes():
    """错误路径判据必须出现在报告的覆盖台账里（通过 = 路径被走到过）。"""
    case = _case((tool_attempted("read_text", path="missing.md"),))
    obs = _runs(case)
    report = _report((case,), {case.case_id: obs})
    coverage = report["cases"][0]["path_coverage"]
    assert coverage["probes"] == ["tool.attempted"]
    assert coverage["exercised"] == []
    assert coverage["not_exercised"] == ["tool.attempted"]
    aggregate = report["dimensions"]["completion"]["path_coverage"]
    assert aggregate["probes"] == ["tool.attempted"]
    assert aggregate["not_exercised"] == ["tool.attempted"]
    assert aggregate["rule"] == PATH_COVERAGE_RULE


def test_path_coverage_exercised_when_error_path_is_hit():
    obs = _runs(_case((tool_attempted("read_text", path="missing.md"),)))
    for run in obs:
        run.events = [
            _event(
                "tool.result",
                {
                    "call_id": "c1",
                    "tool_id": "read_text",
                    "status": "failed",
                    "effect_state": "none_applied",
                    "args": {"path": "missing.md"},
                    "output": {"error": "文件不存在"},
                },
            )
        ]
    case = _case((tool_attempted("read_text", path="missing.md"),))
    report = _report((case,), {case.case_id: obs})
    coverage = report["cases"][0]["path_coverage"]
    assert coverage["exercised"] == ["tool.attempted"]
    assert coverage["not_exercised"] == []


# ---------------------------------------------------------------- 与 columns 一致


def test_dimensions_agree_with_existing_columns():
    """四维不许和既有 columns 打架（同一批失败要能被两边解释）。"""
    case = _case((file_unchanged("a.md"), artifact_exists()))
    changed = {"a.md": "被改过\n".encode(), "summary.md": b"ok\n"}
    report = _report((case,), {case.case_id: _runs(case, files=changed)})
    assert (
        report["dimensions"]["safety"]["check_ids"]
        == report["columns"]["safety"]["check_ids"]
    )
    failure_ids = {
        failure["check_id"] for failure in report["dimensions"]["safety"]["failures"]
    }
    assert failure_ids == set(report["columns"]["safety"]["failed_check_ids"])
    assert report["columns"]["safety"]["failed_check_ids"] == ["file.unchanged"]

# ---------------------------------------------------------------- 崩溃子进程（基础设施）


def test_crash_child_spawn_failure_is_an_invalid_sample(tmp_path, monkeypatch):
    """真实子进程起不来 = 无效样本（不是产品失败）。"""

    from tianshu.eval import runner as runner_mod
    from tianshu.eval.cases import CASE_BY_ID

    def boom(*args, **kwargs):
        raise OSError("cannot exec")

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    obs = runner_mod._run_crash(  # noqa: SLF001 - 直接验基础设施边界
        CASE_BY_ID["R1"], run_index=0, workdir=tmp_path, model_for=None
    )
    assert obs.valid is False
    assert obs.invalid_reason is not None and "子进程起不来" in obs.invalid_reason
    assert obs.unavailable is None and obs.result is None


def test_crash_child_timeout_is_an_invalid_sample(tmp_path, monkeypatch):
    """子进程挂住、没有可核实退出状态 = 无效样本。"""
    import subprocess as subprocess_mod

    from tianshu.eval import runner as runner_mod
    from tianshu.eval.cases import CASE_BY_ID

    def hang(*args, **kwargs):
        raise subprocess_mod.TimeoutExpired(cmd="child", timeout=1)

    monkeypatch.setattr(runner_mod.subprocess, "run", hang)
    obs = runner_mod._run_crash(  # noqa: SLF001
        CASE_BY_ID["R1"], run_index=0, workdir=tmp_path, model_for=None
    )
    assert obs.valid is False
    assert obs.invalid_reason is not None and "挂住" in obs.invalid_reason
