"""统计依赖真实 provider 回执与 C5 判据，不能依赖 prompt 长度。"""

import json

import pytest
from test_model_contract import FakeTransport, _payload

from tianshu import cli
from tianshu.eval import build_report, run_suite, select_cases
from tianshu.model import OpenAICompatibleModel, ScriptedModel


def metrics_module():
    from tianshu import cache_metrics

    return cache_metrics


@pytest.mark.parametrize("vendor,adapter,expected", [
    ("openai", "openai-compatible", 100),
    ("openai", "openai-responses", 100),
    ("anthropic", "anthropic", 200),
    (None, "openai-compatible", None),
    ("other", "openai-compatible", None),
])
def test_provider_denominators_and_unknown_compatible_endpoint(vendor, adapter, expected):
    sample = metrics_module().cache_usage({
        "input_tokens": 100, "output_tokens": 5,
        "cache_read_input_tokens": 80, "cache_creation_input_tokens": 20,
    }, vendor=vendor, adapter=adapter)
    assert sample["total_input_tokens"] == expected
    assert sample["read_fraction"] == (80 / expected if expected else None)
    assert sample["cache_creation_input_tokens"] == 20


@pytest.mark.parametrize("usage", [
    {}, {"input_tokens": 100}, {"input_tokens": 100, "cache_read_input_tokens": None},
    {"input_tokens": None, "cache_read_input_tokens": 80},
])
def test_unknown_usage_never_becomes_zero_or_a_measured_miss(usage):
    sample = metrics_module().cache_usage(usage, vendor="openai", adapter="openai-responses")
    assert sample["read_fraction"] is None


def test_anthropic_missing_creation_cannot_supply_a_complete_denominator():
    sample = metrics_module().cache_usage(
        {"input_tokens": 100, "cache_read_input_tokens": 80},
        vendor="anthropic", adapter="anthropic",
    )
    assert sample["total_input_tokens"] is None
    assert sample["read_fraction"] is None


def test_weighted_rate_only_uses_complete_samples_and_exposes_coverage():
    module = metrics_module()
    samples = [module.cache_usage(u, vendor="openai", adapter="openai-compatible") for u in [
        {"input_tokens": 100, "cache_read_input_tokens": 80},
        {"input_tokens": 900, "cache_read_input_tokens": 0},
        {"input_tokens": 10000},
    ]]
    summary = module.summarize_usage(samples)
    assert summary["observed_token_read_fraction"] == .08
    assert summary["observed_request_hit_fraction"] == .5
    assert summary["complete_calls"] == 2 and summary["incomplete_calls"] == 1
    assert summary["observed_input_tokens"] == 1000
    assert summary["observed_cache_read_tokens"] == 80


def test_observational_report_keeps_c5_failure_and_does_not_count_turn_twice(tmp_path):
    cases = select_cases(["N1"])
    transport = FakeTransport(_payload("openai", text="没有交付产物", usage={
        "prompt_tokens": 100, "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 80},
    }))
    model = OpenAICompatibleModel("test-model", api_key="fake", transport=transport)
    suite = run_suite(cases, model_for=lambda _: model, repeat=3, workdir=tmp_path / "eval")
    report = build_report(suite, cases)
    cache = metrics_module().build_cache_report(suite, report, policy=model.cache_policy)
    assert cache["schema"] == "tianshu.prompt-cache.v1"
    assert cache["quality_verdict"] == report["verdict"] != "pass"
    assert cache["usage"]["calls"] == 3
    assert cache["usage"]["observed_token_read_fraction"] == .8
    assert len(transport.calls) == 3
    assert len(cache["runs"]) == 3
    assert all(run["quality_passed"] is False for run in cache["runs"])
    assert all(run["fixture_digest"] for run in cache["runs"])
    assert len(cache["requests"]) == 3


def test_cli_cache_sidecar_is_offline_and_does_not_change_primary_report(tmp_path, capsys):
    path = tmp_path / "cache.json"
    assert cli.main([
        "evaluate", "--model", "scripted", "--case", "N1", "--repeat", "3",
        "--workdir", str(tmp_path / "eval"), "--cache-report", str(path), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    cache = json.loads(path.read_text(encoding="utf-8"))
    assert path.read_bytes() == (
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    assert cache["evidence"] == "offline"
    assert cache["configured_primary_policy"] == "off"
    assert cache["usage"]["observed_token_read_fraction"] is None
    assert cache["quality_verdict"] == payload["report"]["verdict"]
    assert "cache" not in payload["report"]


def test_scripted_report_cannot_claim_real_provider_cache_performance(tmp_path):
    cases = select_cases(["N1"])
    suite = run_suite(cases, model_for=lambda c: ScriptedModel(list(c.script)),
                      repeat=3, workdir=tmp_path / "eval")
    cache = metrics_module().build_cache_report(
        suite, build_report(suite, cases), policy="off",
    )
    assert cache["evidence"] == "offline"
    assert cache["usage"]["complete_calls"] == 0
    assert cache["usage"]["incomplete_calls"] == cache["usage"]["calls"]
    assert all(request["read_fraction"] is None for request in cache["requests"])


@pytest.mark.parametrize("context,workflow,expected", [
    (None, None, "unknown"),
    ({"budget_version": "ctx-v1"}, None, "baseline_or_spilled"),
    ({"memories": [{"memory_id": "m1"}]}, None, "memory"),
    ({"summaries": [{"summary_id": "s1"}]}, None, "semantic_summary"),
    ({"dropped_seqs": [3]}, None, "trimmed"),
    ({"dropped_seqs": [3], "budget_version": "ctx-responses-compact-v1"},
     None, "responses_compact"),
    ({}, {"mode": "compact"}, "compact_generation"),
    ({}, {"mode": "execute", "source_goal": "goal"}, "compact"),
    ({"memories": [1], "summaries": [2], "dropped_seqs": [3]}, None,
     "memory+semantic_summary+trimmed"),
])
def test_context_labels_keep_combinations_and_missing_evidence_separate(
    context, workflow, expected,
):
    assert metrics_module().context_condition(context, workflow) == expected


def test_failed_calls_still_have_a_context_condition_and_unknown_usage(tmp_path):
    from dataclasses import replace

    from tianshu.model import ModelError

    class FailingModel(OpenAICompatibleModel):
        def complete(self, *_):
            raise ModelError("offline rejection", kind="auth")

    cases = select_cases(["N1"])
    model = FailingModel("test-model", api_key="fake", transport=FakeTransport())
    suite = run_suite(cases, model_for=lambda _: model, repeat=3, workdir=tmp_path / "eval")
    # 真实 intent 仍保留；删 result 不是测试伪造，因为模型真实在发送边界失败了。
    assert all(any(e.type == "model.intent" for e in obs.events) for obs in suite.observations)
    cache = metrics_module().build_cache_report(
        suite, build_report(suite, cases), policy=model.cache_policy,
    )
    assert all(run["condition"] == "baseline_or_spilled" for run in cache["runs"])
    assert sum(run["attempts_without_result"] for run in cache["runs"]) == 3
    assert cache["usage"]["observed_token_read_fraction"] is None
    assert cache["quality_by_condition"][0]["success_rate"] == 0
    bad = replace(suite, suite_version="different")
    with pytest.raises(ValueError, match="SuiteRun"):
        metrics_module().build_cache_report(bad, build_report(suite, cases), policy="off")


def test_sidecar_cannot_overwrite_primary_or_baseline_before_any_model_call(
    tmp_path, capsys, monkeypatch,
):
    import tianshu.eval

    def forbid_suite(*_args, **_kwargs):
        pytest.fail("输出冲突应在任何评测或模型调用之前拒绝")

    monkeypatch.setattr(tianshu.eval, "run_suite", forbid_suite)
    path = tmp_path / "report.json"
    workdir = tmp_path / "eval"
    result = cli.main([
        "evaluate", "--model", "scripted", "--repeat", "3", "--workdir", str(workdir),
        "--out", str(path), "--cache-report", str(path),
    ])
    assert result == 2
    assert "cache-report" in capsys.readouterr().err
    assert not workdir.exists()
    assert not path.exists()


def test_cache_report_preserves_safety_failure_and_primary_efficiency_facts(tmp_path):
    from dataclasses import replace

    from tianshu.eval.checks import Check

    base = select_cases(["N1"])[0]
    case = replace(base, checks=(*base.checks, Check(
        "injected.safety", safety=True, metric=False,
        fn=lambda _: (False, "评测报告安全门槛负控"),
    )))
    suite = run_suite((case,), model_for=lambda c: ScriptedModel(list(c.script)),
                      repeat=3, workdir=tmp_path / "eval")
    quality = build_report(suite, (case,))
    cache = metrics_module().build_cache_report(suite, quality, policy="off")
    assert cache["quality_verdict"] == "unsafe"
    assert cache["quality_by_condition"][0]["safety_failed"] == 3
    primary = quality["cases"][0]["run_details"][0]
    assert cache["runs"][0]["elapsed_ms"] == primary["elapsed_ms"]
    assert cache["runs"][0]["unknown_cost_calls"] == primary["unknown_cost_calls"]


@pytest.mark.parametrize("conflict", [
    "baseline-hardlink", "out-hardlink", "workdir", "workdir-parent",
    "file-as-parent", "out-as-parent", "kept-db", "existing-sidecar",
])
def test_cache_output_conflicts_are_rejected_before_constructing_or_running_models(
    tmp_path, monkeypatch, capsys, conflict,
):
    import os

    import tianshu.eval

    root = tmp_path / "eval"
    primary = tmp_path / "primary.json"
    side = tmp_path / "cache.json"
    option = "--out"
    if conflict.endswith("hardlink"):
        primary.write_bytes(b"retained")
        os.link(primary, side)
        option = "--baseline" if conflict.startswith("baseline") else "--out"
    elif conflict == "workdir":
        side = root
    elif conflict == "workdir-parent":
        root = side / "eval"
    elif conflict == "file-as-parent":
        side.write_bytes(b"retained")
        side /= "nested.json"
    elif conflict == "out-as-parent":
        side = primary / "nested.json"
    elif conflict == "kept-db":
        side = root / "N1" / "run0" / "events.db"
    else:
        side.write_bytes(b"retained")

    def forbid(*_args, **_kwargs):
        pytest.fail("冲突必须在模型构造和 suite 调用前拒绝")

    monkeypatch.setattr(tianshu.eval, "run_suite", forbid)
    monkeypatch.setattr(cli, "_model_from", forbid)
    assert cli.main([
        "evaluate", "--model", "openai-compatible", "--repeat", "3", "--keep",
        "--workdir", str(root), option, str(primary), "--cache-report", str(side),
    ]) == 2
    assert "cache-report" in capsys.readouterr().err
    assert not root.exists()
    if primary.exists():
        assert primary.read_bytes() == b"retained"


def test_sidecar_refuses_a_file_created_during_evaluation_without_truncating_it(
    tmp_path, monkeypatch, capsys,
):
    import tianshu.cache_metrics

    path = tmp_path / "cache.json"
    build = tianshu.cache_metrics.build_cache_report

    def build_and_keep_evidence(*args, **kwargs):
        result = build(*args, **kwargs)
        path.write_bytes(b"evidence-created-after-preflight")
        return result

    monkeypatch.setattr(tianshu.cache_metrics, "build_cache_report", build_and_keep_evidence)
    assert cli.main([
        "evaluate", "--model", "scripted", "--case", "N1", "--repeat", "3",
        "--workdir", str(tmp_path / "eval"), "--cache-report", str(path),
    ]) == 2
    assert path.read_bytes() == b"evidence-created-after-preflight"
    capsys.readouterr()


@pytest.mark.parametrize("first,second", [
    ("Main", "main"),
    ("caf\u00e9", "cafe\u0301"),
])
@pytest.mark.parametrize("conflict", ["same-output", "output-parent", "workdir-parent"])
def test_uncreated_output_aliases_and_ancestor_components_are_rejected_before_models(
    tmp_path, monkeypatch, capsys, first, second, conflict,
):
    import tianshu.eval
    import tianshu.pathkeys

    # 保守路径键也必须在 Linux CI 覆盖 macOS/Windows 的大小写等价策略。
    monkeypatch.setattr(tianshu.pathkeys, "CASE_INSENSITIVE_FS", True)
    primary = tmp_path / first
    side = tmp_path / second
    root = tmp_path / "eval"
    if conflict == "output-parent":
        side /= "cache.json"
    elif conflict == "workdir-parent":
        root = primary / "eval"
        primary = tmp_path / "primary.json"

    def forbid(*_args, **_kwargs):
        pytest.fail("尚不存在的等价路径必须在模型构造和评测前拒绝")

    monkeypatch.setattr(tianshu.eval, "run_suite", forbid)
    monkeypatch.setattr(cli, "_model_from", forbid)
    assert cli.main([
        "evaluate", "--model", "openai-compatible", "--repeat", "3",
        "--workdir", str(root), "--out", str(primary), "--cache-report", str(side),
    ]) == 2
    assert "cache-report" in capsys.readouterr().err
    assert not root.exists() and not primary.exists() and not side.exists()


def test_cache_quality_groups_separate_scripted_harness_from_the_evaluated_model(tmp_path):
    cases = select_cases(["N1", "R1"])
    model = OpenAICompatibleModel("test-model", api_key="fake", transport=FakeTransport(
        _payload("openai", text="没有交付产物", usage={
            "prompt_tokens": 100, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 80},
        }),
    ))
    suite = run_suite(cases, model_for=lambda _: model, repeat=3, workdir=tmp_path / "eval")
    quality = build_report(suite, cases)
    cache = metrics_module().build_cache_report(suite, quality, policy=model.cache_policy)
    groups = {group["scope"]: group for group in cache["quality_by_condition"]}
    assert groups["model"]["success_rate"] == 0
    assert groups["harness"]["success_rate"] == 1
    assert groups["model"]["samples"] == groups["harness"]["samples"] == 3
    assert all(run["scope"] == ("model" if run["case_id"] == "N1" else "harness")
               for run in cache["runs"])
    assert all(request["scope"] == ("model" if request["case_id"] == "N1" else "harness")
               for request in cache["requests"])
