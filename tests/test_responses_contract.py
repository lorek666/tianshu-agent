"""Responses C1/C3 与不改写上下文的版本兼容验收。"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tianshu import event_schema, model
from tianshu._loop_bookkeeping import _assistant_message
from tianshu.context import (
    ContextBudget,
    ContextOverflow,
    budget_from_payload,
    compile_context,
    context_payload,
)
from tianshu.events import canonical_bytes


def ref_payload():
    return {
        "schema_version": "1.0",
        "adapter": "openai-responses",
        "endpoint": "https://api.openai.com/v1/responses",
        "model": "test-model",
        "output_items": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "opaque-ciphertext",
            },
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "先检查", "annotations": []}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "list_files",
                "arguments": '{"path":"."}',
                "status": "completed",
            },
        ],
    }


def _ref():
    assert hasattr(model, "ContinuationRef"), "C1 尚未提供 ContinuationRef"
    return model.ContinuationRef.from_payload(ref_payload())


def test_old_payload_and_assistant_are_byte_identical():
    resp = model.ModelResponse(
        "tool_use", "先看", [model.ToolCall("c", "list_files", {"path": "."})]
    )
    assert resp.to_payload() == {
        "finish_reason": "tool_use",
        "content": "先看",
        "tool_calls": [{"call_id": "c", "name": "list_files", "arguments": {"path": "."}}],
        "usage": {"input_tokens": None, "output_tokens": None},
    }
    assert _assistant_message(resp) == {
        "role": "assistant",
        "content": "先看",
        "tool_calls": [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "list_files", "arguments": '{"path": "."}'},
            }
        ],
    }
    assert model.ModelResponse.from_payload(resp.to_payload()).to_payload() == resp.to_payload()


def test_continuation_roundtrip_detaches_and_preserves_order():
    ref = _ref()
    resp = model.ModelResponse(
        "tool_use",
        "先检查",
        [model.ToolCall("call_1", "list_files", {"path": "."})],
        continuation=ref,
    )
    payload = resp.to_payload()
    assert payload["continuation"] == ref_payload()
    assert _assistant_message(resp)["continuation"] == ref_payload()
    assert model.ModelResponse.from_payload(payload).to_payload() == payload
    payload["continuation"]["output_items"][0]["encrypted_content"] = "mutated"
    assert ref.to_payload() == ref_payload()
    assert "opaque-ciphertext" not in repr(ref)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", "2.0"),
        ("adapter", "invented"),
        ("endpoint", "https://api.openai.com/v1/responses?key=secret"),
        ("model", ""),
        ("output_items", [None]),
    ],
)
def test_continuation_bad_fields_fail_closed(key, value):
    _ref()
    raw = ref_payload()
    raw[key] = value
    with pytest.raises(ValueError):
        model.ContinuationRef.from_payload(raw)


def test_continuation_input_is_detached_and_rejects_unknown_envelope_fields():
    _ref()
    raw = ref_payload()
    ref = model.ContinuationRef.from_payload(raw)
    raw["output_items"].clear()
    assert ref.to_payload() == ref_payload()
    with pytest.raises(ValueError):
        model.ContinuationRef.from_payload({**ref_payload(), "invented": True})


def test_c1_schema_validates_real_ref_and_rejects_adapter_drift():
    ref = _ref()
    doc = json.loads((Path(__file__).resolve().parents[1] / "schemas/c1/model.json").read_text())
    node = doc["$defs"]["continuationRef"]
    errs = []
    event_schema._check_value(node, ref.to_payload(), "$", errs, "probe")
    assert not errs
    event_schema._check_value(node, {**ref.to_payload(), "adapter": "invented"}, "$", errs, "probe")
    assert errs
    payload = model.ModelResponse(
        "tool_use",
        "先检查",
        [model.ToolCall("call_1", "list_files", {"path": "."})],
        continuation=ref,
    ).to_payload()
    errs = []
    event_schema._check_value(doc["$defs"]["modelResponse"], payload, "$", errs, "probe")
    assert not errs


def test_model_result_v2_preserves_frozen_v0_v1_and_cache_usage():
    assert event_schema.current_version("model.result") == 2
    assert event_schema.known_versions("model.result") == {0, 1, 2}
    p = event_schema.minimal_payload("model.result")
    p.update(model.ModelResponse(
        "tool_use", "先检查", [model.ToolCall("call_1", "list_files", {"path": "."})],
        continuation=_ref(),
    ).to_payload())
    p["usage"]["cache_read_input_tokens"] = 3
    assert not event_schema.payload_errors("model.result", p, 2)
    assert event_schema.payload_errors("model.result", p, 1)
    assert event_schema.payload_errors("model.result", p, 0)
    p.pop("continuation")
    p["usage"].pop("cache_read_input_tokens")
    assert not event_schema.payload_errors("model.result", p, 1)


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("cache_key", [
    "cache_read_input_tokens", "cache_creation_input_tokens",
])
def test_new_cache_field_alone_cannot_expand_historical_or_model_turn_schema(version, cache_key):
    p = event_schema.minimal_payload("model.result")
    p["usage"][cache_key] = 3
    assert event_schema.payload_errors("model.result", p, version)
    assert not event_schema.payload_errors("model.result", p, 2)
    turn = event_schema.minimal_payload("run.model_turn")
    turn["usage"][cache_key] = 3
    assert event_schema.payload_errors("run.model_turn", turn, 1)


@pytest.mark.parametrize("name,version,digest", [
    ("model.result", 0, "d4d81cd8d4510ef0b8013c401936a55bfe1220ea10598c8327b3fe7f22554ecc"),
    ("model.result", 1, "785c1bbe7d117e23f6b3ac1189fc9a26e8296aa8f5ac7c13d5aa9a75bfe4d48a"),
    ("run.model_turn", 0, "719fb326f9c328978dfd3df668600768583834f5ce2db2e390736a1c928e2660"),
    ("run.model_turn", 1, "a4ab3ec4fc38b85759dc79e191ba39a9938567dafdc865e1ff8db626d51ad2db"),
])
def test_historical_schema_identity_is_frozen(name, version, digest):
    schema = event_schema.schema_for(name, version)
    assert hashlib.sha256(canonical_bytes(schema)).hexdigest() == digest


def test_responses_context_preserves_large_tool_output_and_version():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "continuation": _ref().to_payload()},
        {"role": "tool", "tool_call_id": "call_1", "name": "list_files", "content": "x" * 10000},
    ]
    original = copy.deepcopy(messages)
    budget = ContextBudget(max_bytes=30000, spill_threshold=2, version="ctx-responses-v1")
    view = compile_context(messages, [0, 0, 1, 2], budget)
    assert view.messages == original == messages
    assert not view.spills and not view.dropped_seqs and not view.compacted
    assert view.digest == hashlib.sha256(canonical_bytes(messages)).hexdigest()
    saved = context_payload(budget, view)
    assert saved["budget_version"] == "ctx-responses-v1"
    assert budget_from_payload(saved) == budget
    with pytest.raises(ContextOverflow):
        compile_context(messages, [0, 0, 1, 2], ContextBudget(1000, version="ctx-responses-v1"))
    # 旧策略对旧消息仍保持 spill 行为，不能全局禁用裁剪来规避续接问题。
    legacy = [{k: v for k, v in m.items() if k != "continuation"} for m in messages]
    assert compile_context(legacy, [0, 0, 1, 2], ContextBudget(30000, 2)).spills


def test_continuation_must_match_normalized_proposals_and_text():
    ref = _ref()
    with pytest.raises(ValueError):
        model.ModelResponse(
            "tool_use",
            "另一组内容",
            [model.ToolCall("call_1", "list_files", {"path": "."})],
            continuation=ref,
        )


def test_event_write_cannot_bypass_continuation_projection_validation():
    p = event_schema.minimal_payload("model.result")
    p.update(model.ModelResponse(
        "tool_use", "先检查", [model.ToolCall("call_1", "list_files", {"path": "."})],
        continuation=_ref(),
    ).to_payload())
    assert not event_schema.payload_errors("model.result", p, 2)
    p["content"] = "divergent"
    assert event_schema.payload_errors("model.result", p, 2)
    with pytest.raises(ValueError):
        model.ModelResponse(
            "tool_use",
            "先检查",
            [model.ToolCall("call_1", "list_files", {"path": "other"})],
            continuation=_ref(),
        )
