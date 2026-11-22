"""缓存策略只改 provider wire 副本，测试使用真实工具契约。"""

import copy
import json

import pytest
from test_model_contract import FakeTransport, _payload
from test_responses_model import Transport, function, message, reasoning, response

from tianshu._loop_bookkeeping import _assistant_message
from tianshu.model import AnthropicMessagesModel, OpenAICompatibleModel
from tianshu.openai_responses import OpenAIResponsesModel
from tianshu.tools import tool_manifest

ADAPTERS = [OpenAICompatibleModel, OpenAIResponsesModel, AnthropicMessagesModel]
MESSAGES = [
    {"role": "system", "content": "固定约束\n逐字保留。"},
    {"role": "user", "content": "任务 A"},
    {"role": "assistant", "content": "已确认的历史"},
    {"role": "user", "content": "动态尾部"},
]


@pytest.fixture(autouse=True)
def cache_env(monkeypatch):
    monkeypatch.delenv("TIANSHU_PROMPT_CACHE_POLICY", raising=False)


def make(cls, **kwargs):
    return cls("test-model", api_key="test-key", **kwargs)


def body(model, messages=None, tools=None):
    return model._request(
        copy.deepcopy(MESSAGES) if messages is None else messages,
        tool_manifest() if tools is None else tools,
    )[1]


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def test_official_openai_default_has_stable_nonpersonal_routing_key():
    for cls in (OpenAICompatibleModel, OpenAIResponsesModel):
        request = body(make(cls))
        assert request["prompt_cache_key"].startswith("ts-prefix-v1-")
        assert "任务" not in request["prompt_cache_key"]
        assert "prompt_cache_retention" not in request
        assert "prompt_cache_options" not in request


@pytest.mark.parametrize("cls", ADAPTERS)
def test_nonofficial_auto_is_unchanged_and_does_not_assume_support(cls):
    request = body(make(cls, base_url="https://example.invalid/v1"))
    assert "cache_control" not in dump(request)
    assert "prompt_cache" not in dump(request)


@pytest.mark.parametrize("cls", ADAPTERS)
def test_opted_in_wire_is_stable_without_mutating_internal_messages_or_manifest(cls):
    model = make(cls, cache_policy="prefix-v1", base_url="https://example.invalid/v1")
    tools = tool_manifest()
    messages = copy.deepcopy(MESSAGES)
    saved = copy.deepcopy((messages, tools))
    def reverse_keys(value):
        if isinstance(value, dict):
            return {key: reverse_keys(value[key]) for key in reversed(value)}
        if isinstance(value, list):
            return [reverse_keys(item) for item in value]
        return value

    reordered = reverse_keys(list(reversed(tools)))
    assert dump(body(model, messages, tools)) == dump(body(model, messages, reordered))
    assert (messages, tools) == saved
    request = body(model, messages, tools)
    if cls is not AnthropicMessagesModel:
        assert request["prompt_cache_key"] == body(
            model, [*messages[:-1], {"role": "user", "content": "尾部已经变化"}], tools,
        )["prompt_cache_key"]
        changed = copy.deepcopy(messages)
        changed[0]["content"] += " "
        assert body(model, changed, tools)["prompt_cache_key"] != request["prompt_cache_key"]
        changed_tools = copy.deepcopy(tools)
        changed_tools[0]["description"] += " "
        assert body(model, messages, changed_tools)["prompt_cache_key"] != (
            request["prompt_cache_key"]
        )


def test_anthropic_has_at_most_four_distinct_breakpoints_and_excludes_dynamic_tail():
    request = body(make(AnthropicMessagesModel, cache_policy="prefix-v1"))
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request["system"] == [{
        "type": "text", "text": MESSAGES[0]["content"],
        "cache_control": {"type": "ephemeral"},
    }]
    assert request["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in dump(request["messages"][-1])
    assert dump(request).count('"cache_control"') == 4


@pytest.mark.parametrize("cls", ADAPTERS)
def test_off_env_override_and_policy_freezes_at_adapter_construction(cls, monkeypatch):
    monkeypatch.setenv("TIANSHU_PROMPT_CACHE_POLICY", "prefix-v1")
    enabled = make(cls)
    disabled = make(cls, cache_policy="off")
    monkeypatch.setenv("TIANSHU_PROMPT_CACHE_POLICY", "off")
    assert enabled.cache_policy == "prefix-v1"
    assert disabled.cache_policy == "off"
    assert "cache_control" not in dump(body(disabled))
    assert "prompt_cache" not in dump(body(disabled))


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize("policy", ["unknown", "", True, 1])
def test_invalid_policy_never_silently_falls_back(cls, policy):
    with pytest.raises(ValueError, match="cache|缓存"):
        make(cls, cache_policy=policy)


@pytest.mark.parametrize("cls", [OpenAICompatibleModel, AnthropicMessagesModel])
def test_responses_explicit_policy_rejects_other_protocols(cls):
    with pytest.raises(ValueError, match="Responses"):
        make(cls, cache_policy="responses-explicit-v1")


def test_responses_explicit_preserves_opaque_items_and_store_false():
    model = make(OpenAIResponsesModel, cache_policy="responses-explicit-v1")
    turn = model._parse(response([reasoning(), function()]))
    assistant = _assistant_message(turn)
    messages = [*copy.deepcopy(MESSAGES[:2]), assistant, {
        "role": "tool", "tool_call_id": "call_1", "name": "list_files", "content": "[]",
    }]
    original = copy.deepcopy(messages)
    request = body(model, messages)
    assert request["store"] is False
    assert "previous_response_id" not in request
    assert request["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert request["input"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit",
    }
    assert request["input"][2:4] == assistant["continuation"]["output_items"]
    assert "prompt_cache_breakpoint" not in dump(request["input"][-1])
    assert messages == original


def test_responses_implicit_roundtrip_preserves_complete_historical_prefix():
    model = make(OpenAIResponsesModel, cache_policy="prefix-v1")
    first = body(model, MESSAGES[:2])
    turn = model._parse(response([reasoning(), message()]))
    second = body(model, [*MESSAGES[:2], _assistant_message(turn),
                          {"role": "user", "content": "继续"}])
    assert second["input"][:len(first["input"])] == first["input"]
    assert second["input"][2:4] == turn.continuation.to_payload()["output_items"]
    assert first["prompt_cache_key"] == second["prompt_cache_key"]


def test_anthropic_official_default_does_not_enable_billable_cache_writes():
    assert "cache_control" not in dump(body(make(AnthropicMessagesModel)))


def test_chat_native_cached_tokens_are_parsed_without_double_counting():
    transport = FakeTransport(_payload("openai", usage={
        "prompt_tokens": 100, "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 80},
    }))
    model = make(OpenAICompatibleModel, transport=transport)
    result = model.complete(MESSAGES, tool_manifest())
    assert result.usage == {
        "input_tokens": 100, "output_tokens": 4, "cache_read_input_tokens": 80,
    }


@pytest.mark.parametrize("cached", [None, 0, 80])
def test_chat_sse_requests_and_persists_final_usage_snapshot(cached):
    transport = Transport(frames=[
        ("", json.dumps({"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]})),
        ("", json.dumps({"choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": cached},
        }})),
        ("", "[DONE]"),
    ])
    model = make(OpenAICompatibleModel, transport=transport)
    result = model.stream_complete(MESSAGES, tool_manifest(), lambda _: None)
    assert transport.calls[0]["body"]["stream_options"] == {"include_usage": True}
    assert result.usage.get("cache_read_input_tokens", "absent") == cached


@pytest.mark.parametrize("details", [
    [], "bad", {"cached_tokens": True}, {"cached_tokens": -1},
    {"cached_tokens": 1.5}, {"cached_tokens": 101},
])
def test_chat_rejects_illegal_or_contradictory_native_cache_usage(details):
    from tianshu.model import ModelError

    model = make(OpenAICompatibleModel)
    with pytest.raises(ModelError, match="usage", check=lambda exc: exc.kind == "protocol"):
        model._parse(_payload("openai", usage={
            "prompt_tokens": 100, "completion_tokens": 4, "prompt_tokens_details": details,
        }))


def test_native_chat_usage_survives_real_loop_events_rebuild_and_replay(tmp_path):
    from support import engine_for, stack_for

    from tianshu.events import EventLog
    from tianshu.replay import replay

    transport = FakeTransport(_payload("openai", usage={
        "prompt_tokens": 100, "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 80},
    }))
    model = make(OpenAICompatibleModel, transport=transport)
    log = EventLog(tmp_path / "events.db")
    try:
        stack = stack_for(log, data_root=tmp_path / "data")
        task = stack.tasks.create_task("离线缓存用量测试", [])
        result = engine_for(stack, model).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        facts = [event for event in log.read_run(result.run_id)
                 if event.type in ("model.result", "run.model_turn")]
        assert [event.payload_version for event in facts] == [2, 2]
        assert all(event.payload["usage"] == {
            "input_tokens": 100, "output_tokens": 4, "cache_read_input_tokens": 80,
        } for event in facts)
        assert result.tokens == 104
        assert result.unknown_cost_calls == 1
        assert replay(log, result.run_id).ok
        stack.tasks.rebuild_from_events()
        assert stack.tasks.list_model_turns(result.run_id)[0]["usage"] == facts[0].payload["usage"]
        assert len(transport.calls) == 1
    finally:
        log.close()


@pytest.mark.parametrize("cls", [OpenAICompatibleModel, AnthropicMessagesModel])
@pytest.mark.parametrize("field", ["cache_read_input_tokens", "cache_creation_input_tokens"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "8"])
def test_cache_usage_does_not_silently_drop_invalid_provider_values(cls, field, value):
    from tianshu.model import ModelError

    vendor = "openai" if cls is OpenAICompatibleModel else "anthropic"
    usage = {"prompt_tokens": 10, "completion_tokens": 4} if vendor == "openai" else {
        "input_tokens": 10, "output_tokens": 4,
    }
    usage[field] = value
    with pytest.raises(ModelError, check=lambda exc: exc.kind == "protocol"):
        make(cls)._parse(_payload(vendor, usage=usage))


@pytest.mark.parametrize("cls", ADAPTERS)
def test_sse_and_json_use_the_same_cache_strategy_and_tool_bytes(cls):
    from test_responses_model import frames_for

    if cls is OpenAIResponsesModel:
        transport = Transport(response(), frames=frames_for([message()]))
    elif cls is OpenAICompatibleModel:
        transport = Transport(_payload("openai"), frames=[
            ("", json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})),
            ("", "[DONE]"),
        ])
    else:
        transport = Transport(_payload("anthropic"), frames=[
            ("message_start", json.dumps({"type": "message_start", "message": {"usage": {}}})),
            ("message_delta", json.dumps({
                "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            })),
            ("message_stop", json.dumps({"type": "message_stop"})),
        ])
    model = make(cls, cache_policy="prefix-v1", transport=transport)
    model.complete(MESSAGES, tool_manifest())
    model.stream_complete(MESSAGES, tool_manifest(), lambda _: None)
    full, streaming = [call["body"] for call in transport.calls]
    del streaming["stream"]
    streaming.pop("stream_options", None)
    assert full == streaming


@pytest.mark.parametrize("cls", [OpenAICompatibleModel, OpenAIResponsesModel])
@pytest.mark.parametrize("creation", [None, 0, 15])
def test_openai_native_cache_writes_are_preserved_separately_from_inclusive_input(cls, creation):
    if cls is OpenAIResponsesModel:
        data = response(usage={
            "input_tokens": 100, "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": creation},
        })
    else:
        data = _payload("openai", usage={
            "prompt_tokens": 100, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": creation},
        })
    usage = make(cls)._parse(data).usage
    assert usage == {
        "input_tokens": 100, "output_tokens": 4,
        "cache_read_input_tokens": 80, "cache_creation_input_tokens": creation,
    }


@pytest.mark.parametrize("cls", [OpenAICompatibleModel, OpenAIResponsesModel])
@pytest.mark.parametrize("creation", [True, -1, 1.5, "15", 21])
def test_openai_invalid_cache_writes_and_read_plus_write_overflow_are_rejected(cls, creation):
    from tianshu.model import ModelError

    details = {"cached_tokens": 80, "cache_write_tokens": creation}
    data = response(usage={
        "input_tokens": 100, "output_tokens": 4, "input_tokens_details": details,
    }) if cls is OpenAIResponsesModel else _payload("openai", usage={
        "prompt_tokens": 100, "completion_tokens": 4, "prompt_tokens_details": details,
    })
    with pytest.raises(ModelError, check=lambda exc: exc.kind == "protocol"):
        make(cls)._parse(data)


def test_native_responses_cache_writes_persist_through_real_loop(tmp_path):
    from support import engine_for, stack_for

    from tianshu.events import EventLog
    from tianshu.replay import replay

    transport = Transport(response(usage={
        "input_tokens": 100, "output_tokens": 4,
        "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 15},
    }))
    # 使用只有 JSON 的真实 transport 替身，以直接检查持久化路径。
    class JsonTransport:
        def post(self, *args, **kwargs):
            return transport.post(*args, **kwargs)

    log = EventLog(tmp_path / "events.db")
    try:
        stack = stack_for(log, data_root=tmp_path / "data")
        task = stack.tasks.create_task("离线缓存写入测试", [])
        result = engine_for(stack, make(
            OpenAIResponsesModel, transport=JsonTransport(),
        )).start(task.task_id, out_name="")
        assert result.outcome == "noop", result.error
        facts = [event for event in log.read_run(result.run_id)
                 if event.type in ("model.result", "run.model_turn")]
        assert [event.payload_version for event in facts] == [2, 2]
        assert all(event.payload["usage"]["cache_creation_input_tokens"] == 15 for event in facts)
        assert result.tokens == 104
        assert replay(log, result.run_id).ok
        assert len(transport.calls) == 1
    finally:
        log.close()


def test_cache_policy_enum_has_an_exact_machine_readable_mirror():
    from pathlib import Path

    from tianshu.prompt_cache import POLICIES

    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/prompt-cache-policy.json").read_text())
    assert schema["enum"] == list(POLICIES)
    section = (root / "docs/PRD-v1.md").read_text().split("### 7.0.1", 1)[1].split("### 7.1", 1)[0]
    assert all(f"`{policy}`" in section for policy in POLICIES)


def test_task_and_output_changes_leave_both_system_prompts_constant():
    from tianshu.conversation import GENERAL_SYSTEM_PROMPT, seed_messages
    from tianshu.loop import SYSTEM_PROMPT, initial_messages

    first = initial_messages("任务 A", "a.md")
    second = initial_messages("任务 B", "b.md")
    assert first[0] == second[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert first[1] != second[1]
    first_desktop = seed_messages(first, {"turns": []})
    second_desktop = seed_messages(second, {"turns": []})
    assert first_desktop[0] == second_desktop[0] == {
        "role": "system", "content": GENERAL_SYSTEM_PROMPT,
    }


def test_anthropic_keeps_task_breakpoint_before_adjacent_volatile_user_blocks():
    messages = [
        *MESSAGES[:2], {"role": "user", "content": "这次选中的记忆/摘要"},
        {"role": "assistant", "content": "历史"}, MESSAGES[-1],
    ]
    request = body(make(AnthropicMessagesModel, cache_policy="prefix-v1"), messages)
    initial = request["messages"][0]["content"]
    assert initial[0] == {
        "type": "text", "text": MESSAGES[1]["content"], "cache_control": {"type": "ephemeral"},
    }
    assert initial[1] == {"type": "text", "text": "这次选中的记忆/摘要"}


@pytest.mark.parametrize("cls", [OpenAICompatibleModel, OpenAIResponsesModel])
def test_json_and_sse_native_cache_write_usage_are_the_same_snapshot(cls):
    from test_responses_model import frames_for

    if cls is OpenAIResponsesModel:
        data = response(usage={
            "input_tokens": 100, "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 15},
        })
        frames = frames_for([message()])
        name, final = frames[-1]
        payload = json.loads(final)
        payload["response"] = data
        frames[-1] = name, json.dumps(payload)
    else:
        data = _payload("openai", usage={
            "prompt_tokens": 100, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 15},
        })
        frames = [
            ("", json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})),
            ("", json.dumps({"choices": [], "usage": data["usage"]})), ("", "[DONE]"),
        ]
    transport = Transport(data, frames=frames)
    model = make(cls, transport=transport)
    buffered = model.complete(MESSAGES, tool_manifest())
    streamed = model.stream_complete(MESSAGES, tool_manifest(), lambda _: None)
    assert buffered.usage == streamed.usage == {
        "input_tokens": 100, "output_tokens": 4,
        "cache_read_input_tokens": 80, "cache_creation_input_tokens": 15,
    }


@pytest.mark.parametrize("cls", [OpenAICompatibleModel, OpenAIResponsesModel])
@pytest.mark.parametrize("policy", ["prefix-v1", "off"])
def test_system_wire_order_is_stable_only_when_policy_enabled(cls, policy):
    messages = copy.deepcopy(MESSAGES)
    changed = copy.deepcopy(messages)
    changed[0] = dict(reversed(changed[0].items()))
    original = copy.deepcopy(changed)
    model = make(cls, cache_policy=policy)
    key = "input" if cls is OpenAIResponsesModel else "messages"
    left = body(model, messages)[key]
    right = body(model, changed)[key]
    assert (dump(left) == dump(right)) is (policy != "off")
    assert changed == original


def _explicit_markers(messages):
    request = body(make(OpenAIResponsesModel, cache_policy="responses-explicit-v1"), messages)
    markers = []
    for item in request["input"]:
        content = item.get("content")
        if isinstance(content, list):
            markers.extend(block["text"] for block in content if "prompt_cache_breakpoint" in block)
    assert len(markers) <= 4
    return markers


def test_explicit_prefix_marks_last_leading_system_and_real_initial_task():
    assert _explicit_markers([
        {"role": "system", "content": "policy"},
        {"role": "system", "content": "domain"},
        {"role": "user", "content": "task"},
    ]) == ["domain", "task"]


def test_explicit_prefix_without_system_does_not_write_the_dynamic_tail():
    assert _explicit_markers([
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "volatile"},
    ]) == ["task"]


def test_explicit_prefix_preserves_prior_history_lookup_over_three_appends():
    messages = [*copy.deepcopy(MESSAGES[:2]), {"role": "assistant", "content": "answer"}]
    messages.extend({"role": "user", "content": text} for text in ("one", "two", "three"))
    earlier = set(_explicit_markers(messages))
    assert earlier == {MESSAGES[0]["content"], MESSAGES[1]["content"], "one", "two"}
    for history, tail in (("three", "four"), ("four", "five"), ("five", "six")):
        messages += [{"role": "assistant", "content": "answer"},
                     {"role": "user", "content": tail}]
        current = set(_explicit_markers(messages))
        assert (earlier & current) - {MESSAGES[0]["content"], MESSAGES[1]["content"]}
        assert history in current and tail not in current
        earlier = current


def test_explicit_compaction_does_not_misidentify_latest_user_as_initial_task():
    model = make(OpenAIResponsesModel, cache_policy="responses-explicit-v1",
                 compact_threshold=2000)
    assistant = _assistant_message(model._parse(response([
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"}, message(),
    ])))
    request = body(model, [
        MESSAGES[0], assistant, {"role": "user", "content": "current supplement"},
    ])
    assert request["input"][1:3] == assistant["continuation"]["output_items"]
    assert "prompt_cache_breakpoint" not in dump(request["input"][-1])


@pytest.mark.parametrize("official", [True, False])
@pytest.mark.parametrize("details", [
    None, {}, {"audio_tokens": 0}, {"cached_tokens": 8, "cache_write_tokens": 7},
])
def test_flat_cache_usage_respects_known_inclusive_input_only_for_official_chat(
    official, details,
):
    from tianshu.model import ModelError

    model = make(OpenAICompatibleModel, **(
        {} if official else {"base_url": "https://example.invalid/v1"}
    ))
    payload = _payload("openai", usage={
        "prompt_tokens": 10, "completion_tokens": 2,
        "cache_read_input_tokens": 8, "cache_creation_input_tokens": 7,
    })
    if details is not None:
        payload["usage"]["prompt_tokens_details"] = details
    if official:
        with pytest.raises(ModelError, check=lambda exc: exc.kind == "protocol"):
            model._parse(payload)
    else:
        assert model._parse(payload).usage == {
            "input_tokens": 10, "output_tokens": 2,
            "cache_read_input_tokens": 8, "cache_creation_input_tokens": 7,
        }


@pytest.mark.parametrize("details", [
    {"cached_tokens": True}, {"cache_write_tokens": -1},
    {"cached_tokens": 6}, {"cache_write_tokens": None},
])
def test_third_party_native_cache_types_and_alias_conflicts_are_still_rejected(details):
    from tianshu.model import ModelError

    model = make(OpenAICompatibleModel, base_url="https://example.invalid/v1")
    with pytest.raises(ModelError, check=lambda exc: exc.kind == "protocol"):
        model._parse(_payload("openai", usage={
            "prompt_tokens": 10, "completion_tokens": 2,
            "cache_read_input_tokens": 8, "cache_creation_input_tokens": 7,
            "prompt_tokens_details": details,
        }))
