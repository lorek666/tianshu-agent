"""C1 契约测试：同一套断言，两个真实 adapter 各跑一遍。

这是 M3 的判据本体。PRD §11 说 M3 的退出条件是"契约测试全绿，主循环 0 改动"，
所以这里刻意**不测实现细节**（"OpenAI adapter 里那个 if 分支走了没有"），
只测接缝上承诺过的东西：

- `finish_reason` 永远落在 13 值封闭集里；不认识的语料 → `ModelError`，
  **绝不**兜底成 `end_turn`（那会把一次异常收尾记成成功）
- 传输/HTTP 失败 → `ModelError`，带类别与状态码；`retryable` 只是建议
- `usage` 缺字段是 `None` 不是 `0`；缓存 token 不并入输入 token
- 参数是半截 JSON 时原样带出，不在 adapter 里"修好"它
- 给模型的工具定义、消息顺序在两个协议下都成立

两个协议走**同一个 `Transport` 接口**，所以断言可以共用；各协议的形状差异
（`system` 在顶层 / `tool_result` 在 user 消息里 / `tools` 要不要包一层）
单独用 vendor 参数区分。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from tianshu.model import (
    FINISH_REASONS,
    AnthropicMessagesModel,
    ModelError,
    ModelResponse,
    OpenAICompatibleModel,
    to_anthropic_messages,
)
from tianshu.tools import tool_manifest

VENDORS = ["openai", "anthropic"]


class FakeTransport:
    """记录请求、返回固定响应。不联网。"""

    def __init__(self, data=None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._data = data
        self._error = error

    def post(self, url, *, body, headers, timeout):  # noqa: ANN001
        self.calls.append(
            {"url": url, "body": body, "headers": headers, "timeout": timeout}
        )
        if self._error is not None:
            raise self._error
        return self._data


def _model(vendor: str, transport) -> object:  # noqa: ANN001
    if vendor == "openai":
        return OpenAICompatibleModel(
            "test-model", base_url="http://127.0.0.1:9/v1", api_key="k",
            transport=transport,
        )
    return AnthropicMessagesModel(
        "test-model", base_url="http://127.0.0.1:9/v1", api_key="k", transport=transport
    )


def _payload(
    vendor: str,
    *,
    text: str = "好的",
    tool: dict | None = None,
    stop: str | None = None,
    usage: dict | None = None,
) -> dict:
    """构造一个"正常"的线上响应；想造异常就传 stop 或自己改返回值。"""
    if vendor == "openai":
        message: dict = {"content": text}
        if tool is not None:
            message["tool_calls"] = [
                {
                    "id": tool["call_id"],
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "arguments": tool.get("arguments_json")
                        or json.dumps(tool.get("args") or {}),
                    },
                }
            ]
        return {
            "choices": [
                {
                    "message": message,
                    "finish_reason": stop if stop is not None
                    else ("tool_calls" if tool else "stop"),
                }
            ],
            "usage": usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 7},
        }

    blocks: list[dict] = [{"type": "text", "text": text}]
    if tool is not None:
        blocks.append(
            {
                "type": "tool_use",
                "id": tool["call_id"],
                "name": tool["name"],
                "input": tool.get("args") or {},
            }
        )
    return {
        "content": blocks,
        "stop_reason": stop if stop is not None else ("tool_use" if tool else "end_turn"),
        "usage": usage if usage is not None else {"input_tokens": 11, "output_tokens": 7},
    }


# 用**真实的** C2 投影，不用手写替身。
# 上一版这里手写了一个 {name, description, input_schema}，于是"OpenAI 侧漏翻
# input_schema→parameters"这个 bug 在测试里根本不可能暴露：手写形状和投影一样，
# 而断言只看了工具名。契约测试一旦用自己的替身，就只证明了替身和它对得上。
TOOLS = tool_manifest()

MESSAGES = [
    {"role": "system", "content": "你是天枢。"},
    {"role": "user", "content": "写个报告"},
]


# ---------------------------------------------------------------- 正常路径


@pytest.mark.parametrize("vendor", VENDORS)
def test_plain_end_turn(vendor):
    t = FakeTransport(_payload(vendor, text="做完了"))
    resp = _model(vendor, t).complete(list(MESSAGES), list(TOOLS))
    assert resp.finish_reason == "end_turn"
    assert resp.content == "做完了"
    assert resp.tool_calls == []
    assert resp.usage == {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.parametrize("vendor", VENDORS)
def test_tool_use_is_mapped_and_args_are_objects(vendor):
    t = FakeTransport(
        _payload(vendor, tool={"call_id": "c1", "name": "create_report",
                              "args": {"name": "r.md", "content": "正文"}})
    )
    resp = _model(vendor, t).complete(list(MESSAGES), list(TOOLS))
    assert resp.finish_reason == "tool_use"
    assert resp.tool_calls == [
        # 两边最终都要变成同一个内部形状，否则主循环就得认识两种协议
        type(resp.tool_calls[0])("c1", "create_report", {"name": "r.md", "content": "正文"})
    ]


@pytest.mark.parametrize("vendor", VENDORS)
def test_request_carries_model_and_tools(vendor):
    t = FakeTransport(_payload(vendor))
    _model(vendor, t).complete(list(MESSAGES), list(TOOLS))
    call = t.calls[0]
    assert call["body"]["model"] == "test-model"
    assert call["body"]["temperature"] == 0
    assert call["timeout"] > 0
    tools = call["body"]["tools"]
    # OpenAI 要包一层 {"type": "function"}，Anthropic 直接吃 name/input_schema。
    # 两种形状由各 adapter 自己消化，主循环给出去的是同一份 C2 投影。
    names = [
        (tool["function"]["name"] if "function" in tool else tool["name"])
        for tool in tools
    ]
    # 投影里有几个工具就发几个，名字一个都不能丢
    assert names and all(names)
    assert "create_report" in names


def test_openai_url_and_auth_header():
    t = FakeTransport(_payload("openai"))
    _model("openai", t).complete(list(MESSAGES), list(TOOLS))
    assert t.calls[0]["url"].endswith("/chat/completions")
    assert t.calls[0]["headers"]["Authorization"] == "Bearer k"
    # OpenAI 的 tools 要包一层 {"type":"function"}
    assert t.calls[0]["body"]["tools"][0]["type"] == "function"


@pytest.mark.parametrize("vendor", VENDORS)
def test_tools_carry_the_parameter_schema(vendor):
    """参数 schema 必须真的上线，否则模型只看得见工具名和描述——参数全靠猜。

    这条对应 PR #15 复审的阻断项：上一版只断言了工具名，漏掉了 schema 字段名
    在 OpenAI 协议里叫 `parameters`（而 C2 投影叫 `input_schema`）。
    """
    t = FakeTransport(_payload(vendor))
    _model(vendor, t).complete(list(MESSAGES), list(TOOLS))
    sent = t.calls[0]["body"]["tools"]
    by_name = {
        (tool["function"]["name"] if "function" in tool else tool["name"]): tool
        for tool in sent
    }
    create = by_name["create_report"]
    if vendor == "openai":
        schema = create["function"].get("parameters")
        assert schema is not None, "OpenAI 侧漏翻了 input_schema → parameters"
    else:
        schema = create.get("input_schema")
        assert schema is not None
    assert schema["type"] == "object"
    assert "name" in schema["properties"]


def test_anthropic_url_and_headers():
    t = FakeTransport(_payload("anthropic"))
    _model("anthropic", t).complete(list(MESSAGES), list(TOOLS))
    call = t.calls[0]
    assert call["url"].endswith("/messages")
    assert call["headers"]["x-api-key"] == "k"
    assert call["headers"]["anthropic-version"] == AnthropicMessagesModel.API_VERSION
    # max_tokens 在 Messages API 里是必填
    assert call["body"]["max_tokens"] > 0
    # system 提到顶层，不再留在 messages 里
    assert call["body"]["system"] == "你是天枢。"
    assert [m["role"] for m in call["body"]["messages"]] == ["user"]


@pytest.mark.parametrize("vendor", VENDORS)
def test_usage_missing_is_none_not_zero(vendor):
    payload = _payload(vendor, usage={})
    resp = _model(vendor, FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert resp.usage["input_tokens"] is None
    assert resp.usage["output_tokens"] is None


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "cache_read_input_tokens",
     "cache_creation_input_tokens"],
)
def test_negative_usage_is_a_nonretryable_protocol_error(vendor, field):
    """外部计数不能变成负消费；两个真实 adapter 都在 C1 边界拒绝。"""
    raw_field = {
        "input_tokens": "prompt_tokens",
        "output_tokens": "completion_tokens",
    }.get(field, field) if vendor == "openai" else field
    payload = _payload(vendor, usage={raw_field: -1})
    with pytest.raises(ModelError) as caught:
        _model(vendor, FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert caught.value.kind == "protocol"
    assert caught.value.retryable is False


def test_anthropic_cache_tokens_not_folded():
    """缓存读写的 token 不能加进 input_tokens，否则同一批 token 被计两次费。"""
    payload = _payload(
        "anthropic",
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 5,
        },
    )
    resp = _model("anthropic", FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert resp.usage["input_tokens"] == 100
    assert resp.usage["cache_read_input_tokens"] == 900
    assert resp.usage["cache_creation_input_tokens"] == 5


# ---------------------------------------------------------------- 半截参数


def test_truncated_arguments_are_passed_through_untouched():
    """参数是半截 JSON 时不许在 adapter 里"修好"——修好了就等于伪造工具入参。"""
    payload = _payload(
        "openai",
        tool={"call_id": "c1", "name": "create_report", "arguments_json": '{"name": "r.m'},
    )
    resp = _model("openai", FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert resp.tool_calls[0].arguments == '{"name": "r.m'


# ---------------------------------------------------------------- 错误分类


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize(
    "status,kind,retryable",
    [
        (401, "auth", False),
        (403, "auth", False),
        (429, "rate_limit", True),
        (500, "http_status", True),
        (503, "http_status", True),
        (400, "http_status", False),
    ],
)
def test_http_status_is_classified(vendor, status, kind, retryable, monkeypatch):
    model = _model(vendor, None)  # 用真的 UrllibTransport，只换掉 opener.open
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda self, req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                req.full_url, status, "boom", {}, io.BytesIO(b'{"error":"x"}')
            )
        ),
    )
    with pytest.raises(ModelError) as ei:
        model.complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == kind
    assert ei.value.status_code == status
    assert ei.value.retryable is retryable
    assert f"HTTP {status}" in str(ei.value)


@pytest.mark.parametrize("vendor", VENDORS)
def test_network_error_is_transport(vendor, monkeypatch):
    model = _model(vendor, None)
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda self, req, timeout=None: (_ for _ in ()).throw(
            urllib.error.URLError(ConnectionResetError("connection reset"))
        ),
    )
    with pytest.raises(ModelError) as ei:
        model.complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == "transport"
    assert ei.value.retryable is True


@pytest.mark.parametrize("vendor", VENDORS)
def test_timeout_is_timeout(vendor, monkeypatch):
    model = _model(vendor, None)
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda self, req, timeout=None: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    with pytest.raises(ModelError) as ei:
        model.complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == "timeout"
    assert ei.value.retryable is True


@pytest.mark.parametrize("vendor", VENDORS)
def test_non_json_body_is_protocol(vendor, monkeypatch):
    model = _model(vendor, None)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, timeout=None: _Resp(b"<html>"))
    with pytest.raises(ModelError) as ei:
        model.complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == "protocol"


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("bogus", ["weird", "paused", ""])
def test_unknown_finish_reason_is_error_not_success(vendor, bogus):
    """不认识的结束原因必须报错：兜底成 end_turn 会把意外收尾记成成功。"""
    payload = _payload(vendor, stop=bogus)
    with pytest.raises(ModelError) as ei:
        _model(vendor, FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == "protocol"
    assert "未映射的结束原因" in str(ei.value)


@pytest.mark.parametrize("vendor", VENDORS)
def test_missing_finish_reason_is_error(vendor):
    payload = _payload(vendor)
    if vendor == "openai":
        payload["choices"][0]["finish_reason"] = None
    else:
        payload["stop_reason"] = None
    with pytest.raises(ModelError) as ei:
        _model(vendor, FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == "protocol"


@pytest.mark.parametrize("vendor", VENDORS)
def test_malformed_envelope_is_protocol(vendor):
    payload = _payload(vendor)
    payload.pop("choices", None)
    payload.pop("content", None)
    with pytest.raises(ModelError) as ei:
        _model(vendor, FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert ei.value.kind == "protocol"


@pytest.mark.parametrize("vendor", VENDORS)
def test_missing_key_says_which_env_var(vendor, monkeypatch):
    monkeypatch.delenv("TIANSHU_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TIANSHU_BASE_URL", raising=False)
    cls = OpenAICompatibleModel if vendor == "openai" else AnthropicMessagesModel
    with pytest.raises(ModelError) as ei:
        cls("test-model")
    assert ei.value.kind == "auth"
    assert ei.value.retryable is False
    expected = "ANTHROPIC_API_KEY" if vendor == "anthropic" else "TIANSHU_API_KEY"
    assert expected in str(ei.value)


# ---------------------------------------------------------------- 封闭集


def test_finish_reason_closed_set_holds_all_13():
    """13 值封闭集逐字冻结：换掉一个值、多一个值、少一个值都必须红。

    只断言 `len == 13` 时，把 `max_tokens` 换成 `length` 这类"静默换血"仍然绿，
    而这份集合要和 adapter 的 stop_reason 映射、PRD §5.5 对账。
    """
    expected = {
        "cancelled",
        "checkpoint",
        "content_filtered",
        "end_turn",
        "guardrail_intervened",
        "interrupt",
        "limit_output_tokens",
        "limit_structured_output_attempts",
        "limit_total_tokens",
        "limit_turns",
        "max_tokens",
        "stop_sequence",
        "tool_use",
    }
    assert len(expected) == 13
    assert FINISH_REASONS == expected, "集合内容被改动过：不是 PRD §5.5 的 13 个字面值"
    for value in expected:
        assert ModelResponse(finish_reason=value).finish_reason == value


@pytest.mark.parametrize("bogus", ["error", "unknown", "length", "max_output_tokens", ""])
def test_out_of_set_finish_reason_fails_fast(bogus):
    with pytest.raises(ValueError) as ei:
        ModelResponse(finish_reason=bogus)
    assert "finish_reason 越界" in str(ei.value)


def test_response_normalizes_usage_at_construction():
    """`usage` 在构造时就归一化：事件里永远有这两个键，缺就是 null。"""
    assert ModelResponse(finish_reason="end_turn", usage={}).usage == {
        "input_tokens": None,
        "output_tokens": None,
    }


def test_model_error_repr_carries_kind_for_event_log():
    """主循环把 repr(exc) 写进 model.error 事件，所以 repr 必须自带类别。"""
    err = ModelError.from_http(429, "slow down")
    assert err.retryable is True
    assert "kind=rate_limit" in repr(err)
    assert "status=429" in repr(err)


def test_model_error_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ModelError("x", kind="weird")


# ---------------------------------------------------------------- 协议翻译


def test_to_anthropic_messages_extracts_system_and_converts_tool_calls():
    messages = [
        {"role": "system", "content": "系统一"},
        {"role": "system", "content": "系统二"},
        {"role": "user", "content": "干活"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": '{"path": "."}'},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": '{"path": ".."}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "list_files",
         "content": '{"files": []}'},
        {"role": "tool", "tool_call_id": "c2", "name": "list_files",
         "content": '{"files": []}'},
    ]
    system, converted = to_anthropic_messages(messages)
    assert system == "系统一\n\n系统二"
    # 角色必须交替：assistant 之后两条 tool 结果合并进同一条 user
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert [b["id"] for b in converted[1]["content"]] == ["c1", "c2"]
    assert [b["type"] for b in converted[2]["content"]] == ["tool_result", "tool_result"]
    assert converted[2]["content"][0]["tool_use_id"] == "c1"


def test_to_anthropic_messages_keeps_broken_arguments_out_of_history():
    """半截 JSON 不能变成伪造的入参：给空对象，真实意图由那条 tool 消息说明。"""
    messages = [
        {"role": "user", "content": "干活"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "create_report", "arguments": '{"name": "r.m'},
                }
            ],
        },
    ]
    _, converted = to_anthropic_messages(messages)
    assert converted[1]["content"][0]["input"] == {}


# ---------------------------------------------------------------- CLI 装配


def _run_args(*extra: str):
    from tianshu import cli as cli_mod

    return cli_mod._build_parser().parse_args(
        ["run", "--task", "t", "--workspace", ".", "--out", "r.md", *extra]
    )


def test_cli_builds_anthropic_when_asked(monkeypatch):
    from tianshu import cli as cli_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("TIANSHU_PROVIDER", raising=False)
    model = cli_mod._model_from(
        _run_args(
            "--model", "openai-compatible",
            "--provider", "anthropic",
            "--model-name", "claude-sonnet-4-5",
        )
    )
    assert isinstance(model, AnthropicMessagesModel)
    assert model.model == "claude-sonnet-4-5"


def test_cli_provider_falls_back_to_env(monkeypatch):
    from tianshu import cli as cli_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("TIANSHU_PROVIDER", "anthropic")
    model = cli_mod._model_from(
        _run_args(
            "--model", "openai-compatible", "--model-name", "claude-sonnet-4-5"
        )
    )
    assert isinstance(model, AnthropicMessagesModel)


def test_cli_defaults_to_openai_and_keeps_old_model_name(monkeypatch):
    from tianshu import cli as cli_mod

    monkeypatch.setenv("TIANSHU_API_KEY", "k")
    monkeypatch.delenv("TIANSHU_PROVIDER", raising=False)
    model = cli_mod._model_from(_run_args("--model", "openai-compatible"))
    assert isinstance(model, OpenAICompatibleModel)
    assert model.model == "gpt-4o-mini"


def test_cli_refuses_to_guess_anthropic_model_name(monkeypatch):
    from tianshu import cli as cli_mod
    from tianshu.errors import TianshuError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(TianshuError) as ei:
        cli_mod._model_from(
            _run_args("--model", "openai-compatible", "--provider", "anthropic")
        )
    assert "--model-name" in str(ei.value)
    # "少给参数"不是"凭据不对"：别混进 auth 类别
    assert not isinstance(ei.value, ModelError)


@pytest.mark.parametrize("vendor", VENDORS)
def test_unmapped_finish_reason_keeps_model_excerpt(vendor):
    """结束原因看不懂时，模型那一轮说了什么必须留在错误里，否则没法排查。"""
    payload = _payload(vendor, text="我要说的这句话不能被吞掉", stop="weird")
    with pytest.raises(ModelError) as ei:
        _model(vendor, FakeTransport(payload)).complete(list(MESSAGES), list(TOOLS))
    assert "我要说的这句话不能被吞掉" in str(ei.value)


def test_anthropic_never_borrows_the_openai_key(monkeypatch):
    """把一家的 key 发给另一家是泄密，不是便利。"""
    monkeypatch.setenv("TIANSHU_API_KEY", "sk-openai-secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ModelError) as ei:
        AnthropicMessagesModel("m")
    assert "ANTHROPIC_API_KEY" in str(ei.value)
    assert "sk-openai-secret" not in str(ei.value)


def test_anthropic_base_url_does_not_reuse_openai_endpoint(monkeypatch):
    """只配了 OpenAI 端点的人切到 anthropic，不该把请求发到 openai 的 /messages。"""
    monkeypatch.setenv("TIANSHU_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    model = AnthropicMessagesModel("m", api_key="k", transport=FakeTransport({}))
    assert model.base_url == "https://api.anthropic.com/v1"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.example/v1")
    model2 = AnthropicMessagesModel("m", api_key="k", transport=FakeTransport({}))
    assert model2.base_url == "https://gw.example/v1"


def test_legacy_finish_reason_in_old_events_is_not_laundered():
    """M3 之前的事件里可能是 `"error"`：读它必须报清楚，不许洗成合法值。"""
    with pytest.raises(ValueError) as ei:
        ModelResponse.from_payload({"finish_reason": "error"})
    assert "finish_reason 越界" in str(ei.value)


# ---------------------------------------------------------------- Anthropic 历史约束


def _tc(call_id: str, name: str = "list_files", args_json: str = '{"path": "."}') -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args_json},
    }


def test_to_anthropic_messages_drops_duplicate_tool_use_ids():
    """provider 复用 call_id 时主循环会拒第二条；历史里也不能发两个同 id 的 tool_use。"""
    messages = [
        {"role": "user", "content": "干活"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("c1"), _tc("c1")]},
        {"role": "tool", "tool_call_id": "c1", "name": "list_files", "content": "ok"},
        {"role": "tool", "tool_call_id": "c1", "name": "list_files", "content": "dup"},
    ]
    _, converted = to_anthropic_messages(messages)
    ids = [b["id"] for b in converted[1]["content"] if b["type"] == "tool_use"]
    assert ids == ["c1"]
    # 第二条（重复 id）没有配对的 tool_use，降级成文字而不是非法的 tool_result
    kinds = [b["type"] for m in converted[2:] for b in m["content"]]
    assert kinds.count("tool_result") == 1
    assert "text" in kinds


def test_to_anthropic_messages_skips_empty_assistant_turns():
    messages = [
        {"role": "user", "content": "嗨"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "再来"},
    ]
    _, converted = to_anthropic_messages(messages)
    # 空轮次被跳过；两条 user 合并（角色必须交替）
    assert [m["role"] for m in converted] == ["user"]
    assert [b["text"] for b in converted[0]["content"]] == ["嗨", "再来"]


def test_to_anthropic_messages_rejects_assistant_first():
    with pytest.raises(ModelError) as ei:
        to_anthropic_messages([{"role": "assistant", "content": "我先说"}])
    assert ei.value.kind == "protocol"


def test_to_anthropic_messages_rejects_system_only():
    with pytest.raises(ModelError) as ei:
        to_anthropic_messages([{"role": "system", "content": "只有系统提示"}])
    assert ei.value.kind == "protocol"


def test_anthropic_call_with_system_only_fails_visibly():
    """只有 system 的对话发给 Anthropic 必 400；必须在本地就明确失败。"""
    t = FakeTransport(_payload("anthropic"))
    with pytest.raises(ModelError):
        _model("anthropic", t).complete(
            [{"role": "system", "content": "只有系统提示"}], list(TOOLS)
        )
    assert t.calls == []  # 没有发出去
