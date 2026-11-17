"""W12 C1 / C2 / C5 字段级 JSON Schema 双向绑定与正负控契约测试。

用真实 `FINISH_REASONS`、`ModelError.KINDS`、`ModelResponse`、`ModelStreamDelta`、
真实 `SPECS`、`tool_manifest()` 以及真实 `run_suite()` / `build_report()` /
`compare_reports()` 输出核验 `schemas/c1/model.json`、`schemas/c2/tool.json`、
`schemas/c5/eval.json`，并包含篡改字段/枚举时的确定性负控。
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from tianshu import event_schema
from tianshu.eval.cases import CASES
from tianshu.eval.report import DIMENSIONS, LAYERS, build_report, compare_reports
from tianshu.eval.runner import run_suite
from tianshu.model import (
    FINISH_REASONS,
    ModelError,
    ModelResponse,
    ModelStreamDelta,
    ScriptedModel,
    ToolCall,
)
from tianshu.permissions import (
    ACTION_CREATE,
    ACTION_DELEGATE,
    ACTION_DELETE,
    ACTION_EXEC,
    ACTION_FETCH,
    ACTION_OVERWRITE,
    ACTION_READ,
)
from tianshu.tools import SPECS, tool_manifest

ROOT = Path(__file__).resolve().parents[1]
C1_SCHEMA_PATH = ROOT / "schemas" / "c1" / "model.json"
C2_SCHEMA_PATH = ROOT / "schemas" / "c2" / "tool.json"
C5_SCHEMA_PATH = ROOT / "schemas" / "c5" / "eval.json"


def _load_defs(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    event_schema.require_supported_dialect(doc["$schema"])
    defs = doc["$defs"]
    for name, node in defs.items():
        event_schema.validate_schema_definition(node, where=f"{path.name}#{name}")
    return defs


def _errors(schema: dict, value: object, tag: str = "contract") -> list[str]:
    errs: list[str] = []
    event_schema._check_value(schema, value, "$", errs, tag)
    return errs


def test_c1_model_schema_mirrors_closed_sets_and_real_objects() -> None:
    c1 = _load_defs(C1_SCHEMA_PATH)
    assert set(c1["finishReason"]["enum"]) == set(FINISH_REASONS)
    assert set(c1["errorKind"]["enum"]) == set(ModelError.KINDS)
    assert c1["modelResponse"]["properties"]["finish_reason"]["enum"] == c1["finishReason"]["enum"]

    resp = ModelResponse(
        finish_reason="tool_use",
        content="分析目录",
        tool_calls=[ToolCall("call_1", "list_files", {"path": "."})],
        usage={"input_tokens": 12, "output_tokens": 6, "cache_read_input_tokens": 3},
    ).to_payload()
    assert _errors(c1["modelResponse"], resp) == []

    # 负控：伪造 finish_reason 或额外未知字段必须被 C1 schema 拒绝
    bad_resp = copy.deepcopy(resp)
    bad_resp["finish_reason"] = "error"
    assert _errors(c1["modelResponse"], bad_resp) != []

    delta_txt = ModelStreamDelta(kind="text", text="hello").to_payload()
    delta_tool = ModelStreamDelta(kind="tool_call_start", tool_name="read_text").to_payload()
    assert _errors(c1["modelStreamDelta"], delta_txt) == []
    assert _errors(c1["modelStreamDelta"], delta_tool) == []
    assert _errors(c1["modelStreamDelta"], {**delta_txt, "kind": "unknown_delta"}) != []


def test_c2_tool_schema_validates_all_real_specs_and_tool_manifest() -> None:
    c2 = _load_defs(C2_SCHEMA_PATH)
    expected_actions = {
        ACTION_READ,
        ACTION_CREATE,
        ACTION_OVERWRITE,
        ACTION_DELETE,
        ACTION_EXEC,
        ACTION_FETCH,
        ACTION_DELEGATE,
    }
    assert set(c2["actionKind"]["enum"]) == expected_actions

    assert len(SPECS) >= 23
    for tool_id, spec in sorted(SPECS.items()):
        as_dict = dataclasses.asdict(spec)
        assert _errors(c2["toolSpec"], as_dict) == [], tool_id

    manifest = tool_manifest()
    assert len(manifest) >= 19
    for item in manifest:
        assert _errors(c2["manifestEntry"], item) == [], item["name"]

    # 负控：篡改 side_effect_level 或放开 input_schema.additionalProperties=True 必须红
    tampered = dataclasses.asdict(next(iter(SPECS.values())))
    tampered["side_effect_level"] = "unsafe_magic"
    assert _errors(c2["toolSpec"], tampered) != []

    bad_manifest = copy.deepcopy(manifest[0])
    bad_manifest["input_schema"]["additionalProperties"] = True
    assert _errors(c2["manifestEntry"], bad_manifest) != []


def test_c5_eval_schema_validates_real_suite_and_compare_reports(
    tmp_path: Path,
) -> None:
    c5 = _load_defs(C5_SCHEMA_PATH)
    assert set(c5["dimensionName"]["enum"]) == set(DIMENSIONS)
    assert set(c5["attributionLayer"]["enum"]) == set(LAYERS)

    case0 = CASES[0]
    suite = run_suite(
        (case0,),
        repeat=3,
        model_for=lambda c: ScriptedModel(list(c.script)),
        workdir=tmp_path / "eval_c5",
    )
    report = build_report(
        suite,
        (case0,),
        selection={"complete": False, "requested": [case0.case_id], "case_count": 1},
    )
    assert _errors(c5["evalReport"], report) == []

    cmp_report = compare_reports(report, report)
    assert _errors(c5["compareReport"], cmp_report) == []

    # 负控：repeat < 3 或非法 verdict 必须被 C5 schema 拒绝
    bad_rep = copy.deepcopy(report)
    bad_rep["repeat"] = 1
    assert _errors(c5["evalReport"], bad_rep) != []

    bad_verdict = copy.deepcopy(report)
    bad_verdict["verdict"] = "almost_safe"
    assert _errors(c5["evalReport"], bad_verdict) != []


@pytest.mark.parametrize("known_price", [True, False])
def test_run_end_v2_records_cost_total_micros_without_faking_zero(
    tmp_path: Path, known_price: bool
) -> None:
    """`run.end` v2 汇总微美元成本：官方已知单价写整数，未知单价写 None 绝不冒充 0。"""
    from tianshu.events import EventLog
    from tianshu.loop import run as run_loop
    from tianshu.model import OpenAICompatibleModel

    class _FakePostTransport:
        def post(self, url, *, body, headers, timeout):  # noqa: ANN001, ARG002
            return {
                "choices": [
                    {
                        "message": {"content": "已完成回答"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }

    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        model = (
            OpenAICompatibleModel(
                "gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-known-price-1234",
                transport=_FakePostTransport(),
            )
            if known_price
            else ScriptedModel(
                [
                    ModelResponse(
                        finish_reason="end_turn",
                        content="脚本回答",
                        usage={"input_tokens": 100, "output_tokens": 50},
                    )
                ]
            )
        )
        res = run_loop(
            task="只读回答",
            workspace_dir=ws,
            out_name="out.md",
            model=model,
            log=log,
        )
        end_ev = [e for e in log.read_run(res.run_id) if e.type == "run.end"][-1]
        assert end_ev.payload_version == 2
        if known_price:
            assert isinstance(end_ev.payload["cost_total_micros"], int)
            assert end_ev.payload["cost_total_micros"] > 0
            assert end_ev.payload["cost_unknown_calls"] == 0
        else:
            assert end_ev.payload["cost_total_micros"] is None
            assert end_ev.payload["cost_unknown_calls"] == 1
    finally:
        log.close()


def test_run_end_v2_edge_cases_zero_turns_zero_tokens_and_mixed_calls(
    tmp_path: Path,
) -> None:
    """`run.end` v2 边界：0 轮次为 0、已知+未知混合调用保留已知微美元并累加未知次数。"""
    from tianshu.events import EventLog
    from tianshu.loop import Limits
    from tianshu.loop import run as run_loop
    from tianshu.model import OpenAICompatibleModel

    ws = tmp_path / "ws"
    ws.mkdir()

    # 1. max_turns=0：零模型调用 -> cost_total_micros=0, cost_unknown_calls=0
    log0 = EventLog(tmp_path / "e0.db", blob_dir=tmp_path / "b0")
    try:
        res0 = run_loop(
            task="零轮次",
            workspace_dir=ws,
            out_name="out.md",
            model=ScriptedModel([]),
            log=log0,
            limits=Limits(max_turns=0),
        )
        ev0 = [e for e in log0.read_run(res0.run_id) if e.type == "run.end"][-1]
        assert ev0.payload["cost_total_micros"] == 0
        assert ev0.payload["cost_unknown_calls"] == 0
    finally:
        log0.close()

    # 2. 混合调用：第 1 轮已知官方价格（>0 微美元），第 2 轮 usage 缺失（未知费用）
    class _MixedTransport:
        def __init__(self) -> None:
            self.n = 0

        def post(self, url, *, body, headers, timeout):  # noqa: ANN001, ARG002
            self.n += 1
            if self.n == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "c_mix_1",
                                        "type": "function",
                                        "function": {
                                            "name": "list_files",
                                            "arguments": '{"path":"."}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                }
            return {
                "choices": [{"message": {"content": "收尾"}, "finish_reason": "stop"}],
                "usage": None,
            }

    log_mix = EventLog(tmp_path / "emix.db", blob_dir=tmp_path / "bmix")
    try:
        model_mix = OpenAICompatibleModel(
            "gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-mix-1234",
            transport=_MixedTransport(),
        )
        res_mix = run_loop(
            task="混合计费",
            workspace_dir=ws,
            out_name="out.md",
            model=model_mix,
            log=log_mix,
        )
        ev_mix = [e for e in log_mix.read_run(res_mix.run_id) if e.type == "run.end"][-1]
        assert isinstance(ev_mix.payload["cost_total_micros"], int)
        assert ev_mix.payload["cost_total_micros"] > 0
        assert ev_mix.payload["cost_unknown_calls"] == 1
    finally:
        log_mix.close()
