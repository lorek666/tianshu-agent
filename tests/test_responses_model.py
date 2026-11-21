"""公开 Responses 协议的请求/输出与 SSE 正负控，不联网。"""

import copy
import json

import pytest

from tianshu._loop_bookkeeping import _assistant_message
from tianshu.model import ModelError, ToolCall
from tianshu.tools import tool_manifest


def message(text="完成", id="msg_1", phase="final_answer"):
    return {
        "type": "message",
        "id": id,
        "role": "assistant",
        "status": "completed",
        "phase": phase,
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function(id="fc_1", call_id="call_1", arguments='{"path":"."}'):
    return {
        "type": "function_call",
        "id": id,
        "call_id": call_id,
        "name": "list_files",
        "arguments": arguments,
        "status": "completed",
    }


def reasoning():
    return {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque-secret-reasoning",
    }


def response(output=None, status="completed", **extra):
    return {
        "id": "resp_1",
        "status": status,
        "model": "test-model",
        "output": output if output is not None else [message()],
        "usage": {
            "input_tokens": 13,
            "output_tokens": 8,
            "output_tokens_details": {"reasoning_tokens": 6},
        },
        **extra,
    }


class Transport:
    def __init__(self, *data, frames=None):
        self.data = list(data)
        self.frames = frames
        self.calls = []

    def post(self, url, **kw):
        self.calls.append({"url": url, **copy.deepcopy(kw)})
        assert self.data, "意外重发模型请求"
        return self.data.pop(0)

    def post_stream(self, url, **kw):
        self.calls.append({"url": url, **copy.deepcopy(kw)})
        yield from self.frames


def model(transport, **kwargs):
    from tianshu.openai_responses import OpenAIResponsesModel

    return OpenAIResponsesModel("test-model", api_key="fake-key", transport=transport, **kwargs)


def test_request_uses_real_manifest_and_stateless_defaults():
    t = Transport(response())
    m = model(t, base_url="https://API.OPENAI.COM:443/v1/")
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    before = copy.deepcopy(messages)
    result = m.complete(messages, tool_manifest())
    assert result.content == "完成" and result.finish_reason == "end_turn"
    assert result.usage == {"input_tokens": 13, "output_tokens": 8}
    request = t.calls[0]
    assert request["url"] == "https://api.openai.com/v1/responses"
    body = request["body"]
    assert body["store"] is False and body["include"] == ["reasoning.encrypted_content"]
    assert not {"temperature", "previous_response_id", "conversation", "reasoning"} & body.keys()
    assert body["input"] == before == messages
    for sent, spec in zip(body["tools"], tool_manifest(), strict=True):
        assert sent == {
            "type": "function",
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["input_schema"],
            "strict": False,
        }


def test_two_turn_reasoning_functions_preserve_raw_order_and_call_id():
    items = [
        reasoning(),
        message("先检查", phase="commentary"),
        function(),
        function("fc_2", "call_2"),
    ]
    t = Transport(response(items), response())
    m = model(t)
    history = [{"role": "user", "content": "列目录"}]
    first = m.complete(history, tool_manifest())
    assert first.finish_reason == "tool_use"
    assert first.tool_calls == [
        ToolCall("call_1", "list_files", {"path": "."}),
        ToolCall("call_2", "list_files", {"path": "."}),
    ]
    history += [
        _assistant_message(first),
        {"role": "tool", "tool_call_id": "call_1", "name": "list_files", "content": "[]"},
        {"role": "tool", "tool_call_id": "call_2", "name": "list_files", "content": "[]"},
    ]
    second = m.complete(history, tool_manifest())
    assert second.finish_reason == "end_turn"
    assert t.calls[1]["body"]["input"] == [
        {"role": "user", "content": "列目录"},
        *items,
        {"type": "function_call_output", "call_id": "call_1", "output": "[]"},
        {"type": "function_call_output", "call_id": "call_2", "output": "[]"},
    ]


@pytest.mark.parametrize("raw", ['{"path":', "[]", "42", '"text"'])
def test_invalid_arguments_remain_raw_for_execution_side_rejection(raw):
    resp = model(Transport(response([function(arguments=raw)]))).complete([], [])
    assert resp.tool_calls[0].arguments == raw


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "invented"},
        {"status": None},
        {"status": "failed"},
        {"status": "in_progress"},
        {"output": [{"type": "web_search_call", "id": "ws_1"}]},
        {"output": None},
        {"output": [function(call_id="")]},
        {"output": [function(), function()]},
        {"output": [message(phase="invented")]},
        {"output": [{**reasoning(), "encrypted_content": None}]},
        {"output": [{**message(), "status": "in_progress"}]},
        {"output": [{**function(), "status": "incomplete"}]},
    ],
)
def test_invalid_terminal_or_output_is_protocol_error(patch):
    with pytest.raises(ModelError) as exc:
        model(Transport({**response(), **patch})).complete([], [])
    assert exc.value.kind == "protocol" and not exc.value.retryable


@pytest.mark.parametrize(
    "reason,finish",
    [("max_output_tokens", "limit_output_tokens"), ("content_filter", "content_filtered")],
)
def test_incomplete_output_is_never_dispatchable(reason, finish):
    f = {**function(arguments='{"path":'), "status": "incomplete"}
    r = response([f], status="incomplete", incomplete_details={"reason": reason})
    assert model(Transport(r)).complete([], []).finish_reason == finish


def test_refusal_and_cache_usage_unknown_price():
    msg = message()
    msg["content"] = [{"type": "refusal", "refusal": "不能执行"}]
    r = response([msg])
    r["usage"]["input_tokens_details"] = {"cached_tokens": 4}
    m = model(Transport(r), base_url="https://gateway.invalid/v1")
    resp = m.complete([], [])
    assert resp.finish_reason == "content_filtered" and resp.content == "不能执行"
    assert resp.usage["cache_read_input_tokens"] == 4
    assert resp.usage["output_tokens"] == 8
    assert m.price_identity().vendor is None


@pytest.mark.parametrize("change", [{"model": "other"}, {"base_url": "https://else.invalid/v1"}])
def test_scope_change_rejected_before_transport(change):
    from tianshu.openai_responses import OpenAIResponsesModel

    first = model(Transport(response([reasoning(), function()]))).complete([], [])
    t = Transport()
    other = OpenAIResponsesModel(
        change.get("model", "test-model"),
        base_url=change.get("base_url"),
        api_key="fake",
        transport=t,
    )
    with pytest.raises(ModelError, match="新 Run"):
        other.complete([_assistant_message(first)], [])
    assert not t.calls


def frames_for(items):
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_1", "status": "in_progress", "output": []},
        },
        {
            "type": "response.in_progress",
            "response": {"id": "resp_1", "status": "in_progress", "output": []},
        },
    ]
    for index, item in enumerate(items):
        start = copy.deepcopy(item)
        if item["type"] == "message":
            start["content"] = []
        if item["type"] == "function_call":
            start["arguments"] = ""
        start["status"] = "in_progress"
        events.append({"type": "response.output_item.added", "output_index": index, "item": start})
    # 有意交错多个 output item 的文本/参数。
    for text_index in (0, 1):
        for index, item in enumerate(items):
            common = {"output_index": index, "item_id": item["id"]}
            if item["type"] == "message":
                text = item["content"][0]["text"]
                events.append(
                    {
                        "type": "response.output_text.delta",
                        **common,
                        "content_index": 0,
                        "delta": text[:1] if text_index == 0 else text[1:],
                    }
                )
            if item["type"] == "function_call":
                text = item["arguments"]
                events.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        **common,
                        "delta": text[:4] if text_index == 0 else text[4:],
                    }
                )
    for index, item in enumerate(items):
        if item["type"] == "function_call":
            events.append({
                "type": "response.function_call_arguments.done", "output_index": index,
                "item_id": item["id"], "arguments": item["arguments"],
            })
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed", "response": response(items)})
    return [(ev["type"], json.dumps({**ev, "sequence_number": n})) for n, ev in enumerate(events)]


def test_sse_interleaved_items_and_final_usage():
    items = [
        reasoning(),
        message("检查中", phase="commentary"),
        function(),
        function("fc_2", "call_2"),
    ]
    preview = []
    r = model(Transport(frames=frames_for(items))).stream_complete(
        [], tool_manifest(), preview.append
    )
    assert r.tool_calls == [
        ToolCall("call_1", "list_files", {"path": "."}),
        ToolCall("call_2", "list_files", {"path": "."}),
    ]
    assert r.continuation.to_payload()["output_items"] == items
    assert "".join(d.text for d in preview) == "检查中"
    assert [d.tool_name for d in preview if d.kind == "tool_call_start"] == [
        "list_files",
        "list_files",
    ]
    assert r.usage == {"input_tokens": 13, "output_tokens": 8}
    assert "opaque-secret-reasoning" not in repr(preview)


@pytest.mark.parametrize(
    "mutation",
    ["eof", "terminal_conflict", "delta_conflict", "item_id", "failed", "unknown", "sequence"],
)
def test_sse_corruption_rejected(mutation):
    frames = frames_for([message("ok"), function()])
    if mutation == "eof":
        frames = frames[:-1]
    elif mutation == "terminal_conflict":
        ev = json.loads(frames[-1][1])
        ev["response"]["output"][0]["phase"] = "commentary"
        frames[-1] = (ev["type"], json.dumps(ev))
    elif mutation in ("delta_conflict", "item_id"):
        at = next(i for i, (k, _) in enumerate(frames) if k == "response.output_text.delta")
        ev = json.loads(frames[at][1])
        ev["delta" if mutation == "delta_conflict" else "item_id"] = "wrong"
        frames[at] = (ev["type"], json.dumps(ev))
    elif mutation == "failed":
        frames[-1] = (
            "response.failed",
            json.dumps({"type": "response.failed", "response": response(status="failed")}),
        )
    elif mutation == "sequence":
        ev = json.loads(frames[-1][1])
        ev["sequence_number"] = 0
        frames[-1] = (ev["type"], json.dumps(ev))
    else:
        frames.insert(1, ("response.invented", '{"type":"response.invented"}'))
    with pytest.raises(ModelError):
        model(Transport(frames=frames)).stream_complete([], [], lambda _: None)


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_other_protocol_cannot_consume_responses_continuation(vendor):
    from test_model_contract import _model

    first = model(Transport(response([reasoning(), function()]))).complete([], [])
    t = Transport()
    with pytest.raises(ModelError, match="新 Run"):
        _model(vendor, t).complete([_assistant_message(first)], [])
    assert not t.calls


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#fragment",
        "file:///tmp/endpoint",
        "https://host:bad/v1",
    ],
)
def test_endpoint_scope_rejects_ambiguous_or_credential_urls(url):
    with pytest.raises((ValueError, ModelError)):
        model(Transport(), base_url=url)


def test_scope_normalizes_default_port_but_keeps_model_and_path():
    m = model(Transport(), base_url="https://API.OPENAI.COM:443/v1///")
    assert m.continuation_scope() == (
        "openai-responses",
        "https://api.openai.com/v1/responses",
        "test-model",
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"store": True},
        {"error": {"message": "boom"}},
        {"output": []},
        {"output": [reasoning()]},
        {"id": None},
    ],
)
def test_response_cannot_silently_claim_success_without_valid_result(patch):
    with pytest.raises(ModelError):
        model(Transport({**response(), **patch})).complete([], [])


def test_transport_error_does_not_echo_encrypted_input():
    first = model(Transport(response([reasoning(), function()]))).complete([], [])

    class EchoTransport:
        def post(self, url, **kwargs):
            raise ModelError.from_http(400, json.dumps(kwargs["body"]))

    with pytest.raises(ModelError) as caught:
        model(EchoTransport()).complete([_assistant_message(first)], [])
    assert "opaque-secret-reasoning" not in str(caught.value)
    assert caught.value.status_code == 400 and caught.value.kind == "http_status"


def _renumber(events):
    return [
        (ev["type"], json.dumps({**ev, "sequence_number": n}))
        for n, ev in enumerate(events)
    ]


def test_sse_public_content_part_lifecycle_and_encrypted_done_snapshot():
    items = [reasoning(), message("正常输出")]
    events = [json.loads(data) for _, data in frames_for(items)]
    # reasoning 的 added 快照还没有密文；只有 output_item.done 才可作为续接事实。
    next(ev for ev in events if ev["type"] == "response.output_item.added")[
        "item"
    ]["encrypted_content"] = None
    added_index = next(
        n for n, ev in enumerate(events) if ev["type"] == "response.output_text.delta"
    )
    common = {"output_index": 1, "item_id": "msg_1", "content_index": 0}
    events.insert(added_index, {
        "type": "response.content_part.added", **common,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })
    done_index = next(
        n for n, ev in enumerate(events) if ev["type"] == "response.output_item.done"
    )
    events[done_index:done_index] = [
        {"type": "response.output_text.done", **common, "text": "正常输出"},
        {"type": "response.content_part.done", **common,
         "part": items[1]["content"][0]},
    ]
    result = model(Transport(frames=_renumber(events))).stream_complete([], [], lambda _: None)
    assert result.content == "正常输出"
    assert result.continuation.to_payload()["output_items"] == items


@pytest.mark.parametrize(
    "change",
    ["part_done_conflict", "text_done_conflict", "delta_after_item_done",
     "duplicate_terminal", "missing_item_done", "response_id", "event_type"],
)
def test_sse_lifecycle_conflicts_are_not_hidden_by_complete_terminal(change):
    item = message("ok")
    events = [json.loads(data) for _, data in frames_for([item])]
    common = {"output_index": 0, "item_id": "msg_1", "content_index": 0}
    at = next(n for n, ev in enumerate(events) if ev["type"] == "response.output_item.done")
    if change == "part_done_conflict":
        events.insert(at, {
            "type": "response.content_part.done", **common,
            "part": {"type": "output_text", "text": "conflict", "annotations": []},
        })
    elif change == "text_done_conflict":
        events.insert(at, {"type": "response.output_text.done", **common, "text": "conflict"})
    elif change == "delta_after_item_done":
        events.insert(at + 1, {"type": "response.output_text.delta", **common, "delta": "late"})
    elif change == "duplicate_terminal":
        events.append(copy.deepcopy(events[-1]))
    elif change == "missing_item_done":
        events.pop(at)
    elif change == "response_id":
        events[-1]["response"]["id"] = "resp_other"
    else:
        frames = _renumber(events)
        frames[0] = ("response.in_progress", frames[0][1])
        with pytest.raises(ModelError):
            model(Transport(frames=frames)).stream_complete([], [], lambda _: None)
        return
    with pytest.raises(ModelError):
        model(Transport(frames=_renumber(events))).stream_complete([], [], lambda _: None)


@pytest.mark.parametrize("usage", [
    {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": -1},
    {"input_tokens": 1, "output_tokens": 1,
     "input_tokens_details": {"cached_tokens": 2}},
])
def test_invalid_usage_is_not_persisted_as_a_real_measurement(usage):
    with pytest.raises(ModelError):
        model(Transport(response(usage=usage))).complete([], [])


@pytest.mark.parametrize("conflict", [False, True])
def test_sse_refusal_public_lifecycle_and_done_consistency(conflict):
    item = message()
    item["content"] = [{"type": "refusal", "refusal": "不能执行"}]
    common = {"output_index": 0, "item_id": item["id"], "content_index": 0}
    events = [
        {"type": "response.created", "response": {
            "id": "resp_1", "status": "in_progress", "output": [],
        }},
        {"type": "response.output_item.added", "output_index": 0, "item": {
            **item, "content": [], "status": "in_progress",
        }},
        {"type": "response.content_part.added", **common,
         "part": {"type": "refusal", "refusal": ""}},
        {"type": "response.refusal.delta", **common, "delta": "不能"},
        {"type": "response.refusal.delta", **common, "delta": "执行"},
        {"type": "response.refusal.done", **common,
         "refusal": "conflict" if conflict else "不能执行"},
        {"type": "response.content_part.done", **common, "part": item["content"][0]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response([item])},
    ]
    m = model(Transport(frames=_renumber(events)))
    if conflict:
        with pytest.raises(ModelError):
            m.stream_complete([], [], lambda _: None)
    else:
        result = m.stream_complete([], [], lambda _: None)
        assert result.finish_reason == "content_filtered" and result.content == "不能执行"


def test_sse_function_arguments_done_cannot_override_observed_delta():
    events = [json.loads(data) for _, data in frames_for([function()])]
    next(ev for ev in events if ev["type"] == "response.function_call_arguments.done")[
        "arguments"
    ] = '{"path":"other"}'
    with pytest.raises(ModelError):
        model(Transport(frames=_renumber(events))).stream_complete([], [], lambda _: None)


def test_completed_function_accepts_public_optional_item_id_and_status():
    item = function()
    item.pop("id")
    item.pop("status")
    result = model(Transport(response([item]))).complete([], [])
    assert result.finish_reason == "tool_use"
    assert result.tool_calls == [ToolCall("call_1", "list_files", {"path": "."})]
    assert result.continuation.to_payload()["output_items"] == [item]


@pytest.mark.parametrize("change", ["initial_arguments", "initial_content", "response_id"])
def test_sse_initial_snapshot_and_explicit_response_identity_cannot_conflict(change):
    events = [json.loads(data) for _, data in frames_for([message("ok"), function()])]
    if change == "initial_arguments":
        next(e["item"] for e in events if e["type"] == "response.output_item.added"
             and e["item"]["type"] == "function_call")["arguments"] = "conflict"
    elif change == "initial_content":
        next(e["item"] for e in events if e["type"] == "response.output_item.added"
             and e["item"]["type"] == "message")["content"] = [
                 {"type": "output_text", "text": "conflict", "annotations": []},
             ]
    else:
        next(e for e in events if e["type"] == "response.output_text.delta")[
            "response_id"
        ] = "other"
    with pytest.raises(ModelError):
        model(Transport(frames=_renumber(events))).stream_complete([], [], lambda _: None)


def test_unsupported_multimodal_input_is_rejected_before_transport():
    transport = Transport(response())
    with pytest.raises(ModelError):
        model(transport).complete([{
            "role": "user", "content": [{"type": "input_image", "image_url": "https://invalid/"}],
        }], [])
    assert not transport.calls


@pytest.mark.parametrize("stream_kind", ["summary", "reasoning"])
@pytest.mark.parametrize("conflict", [False, True])
def test_sse_reasoning_lifecycle_preserves_items_without_preview(stream_kind, conflict):
    item = reasoning()
    text = "private reasoning summary"
    if stream_kind == "summary":
        item["summary"] = [{"type": "summary_text", "text": text}]
        index_key, text_event = "summary_index", "response.reasoning_summary_text"
        part_key, part_type = "summary", "summary_text"
    else:
        item["content"] = [{"type": "reasoning_text", "text": text}]
        index_key, text_event = "content_index", "response.reasoning_text"
        part_key, part_type = "content", "reasoning_text"
    start = {**item, part_key: [], "encrypted_content": None, "status": "in_progress"}
    common = {"output_index": 0, "item_id": item["id"], index_key: 0}
    events = [
        {"type": "response.created", "response": {
            "id": "resp_1", "status": "in_progress", "output": [],
        }},
        {"type": "response.output_item.added", "output_index": 0, "item": start},
    ]
    if stream_kind == "summary":
        events.append({
            "type": "response.reasoning_summary_part.added", **common,
            "part": {"type": part_type, "text": ""},
        })
    events.extend([
        {"type": f"{text_event}.delta", **common, "delta": text[:8]},
        {"type": f"{text_event}.delta", **common, "delta": text[8:]},
        {"type": f"{text_event}.done", **common,
         "text": "conflict" if conflict else text},
    ])
    if stream_kind == "summary":
        events.append({
            "type": "response.reasoning_summary_part.done", **common,
            "part": item["summary"][0],
        })
    events.extend([
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {**message(), "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "output_index": 1, "item_id": "msg_1",
         "content_index": 0, "delta": "完成"},
        {"type": "response.output_item.done", "output_index": 1, "item": message()},
        {"type": "response.completed", "response": response([item, message()])},
    ])
    preview = []
    m = model(Transport(frames=_renumber(events)))
    if conflict:
        with pytest.raises(ModelError):
            m.stream_complete([], [], preview.append)
    else:
        result = m.stream_complete([], [], preview.append)
        assert result.continuation.to_payload()["output_items"] == [item, message()]
        assert result.content == "完成"
        assert "".join(d.text for d in preview) == "完成"
        assert text not in repr(preview)
