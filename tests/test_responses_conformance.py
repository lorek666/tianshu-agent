"""同一套 C1 验收对三个真实协议适配器执行，不用替代 schema。"""

import io
import urllib.error
import urllib.request

import pytest
from test_model_contract import MESSAGES, FakeTransport, _model, _payload
from test_responses_model import function, model, response

from tianshu.model import ModelError
from tianshu.tools import tool_manifest

VENDORS = ["openai", "anthropic", "openai-responses"]


def make(vendor, transport):
    if vendor == "openai-responses":
        return model(transport)
    return _model(vendor, transport)


def payload(vendor, *, usage=None, raw=None):
    if vendor == "openai-responses":
        p = response([function(arguments=raw)]) if raw is not None else response()
        if usage is not None:
            p["usage"] = usage
        return p
    return _payload(
        vendor,
        usage=usage,
        tool={"call_id": "call_1", "name": "list_files", "arguments_json": raw}
        if raw is not None
        else None,
    )


@pytest.mark.parametrize("vendor", VENDORS)
def test_all_adapters_share_neutral_contract_and_real_schema(vendor):
    t = FakeTransport(payload(vendor))
    resp = make(vendor, t).complete(list(MESSAGES), tool_manifest())
    assert resp.finish_reason == "end_turn"
    assert isinstance(resp.content, str)
    for tool in t.calls[0]["body"]["tools"]:
        sent = tool.get("function", tool)
        schema = sent.get("parameters", sent.get("input_schema"))
        spec = next(s for s in tool_manifest() if s["name"] == sent["name"])
        assert schema == spec["input_schema"]


@pytest.mark.parametrize("vendor", VENDORS)
def test_all_adapters_preserve_unknown_usage(vendor):
    resp = make(vendor, FakeTransport(payload(vendor, usage={}))).complete(list(MESSAGES), [])
    assert resp.usage == {"input_tokens": None, "output_tokens": None}


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("status,kind", [(401, "auth"), (429, "rate_limit"), (500, "http_status")])
def test_all_adapters_use_shared_http_error_semantics(vendor, status, kind, monkeypatch):
    m = make(vendor, None)
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda self, req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(b"bad"))
        ),
    )
    with pytest.raises(ModelError) as caught:
        m.complete(list(MESSAGES), [])
    assert caught.value.kind == kind
    assert caught.value.status_code == status


@pytest.mark.parametrize("vendor", VENDORS)
def test_all_adapters_do_not_repair_truncated_function_args(vendor):
    # Anthropic 的 input 本身是 JSON object，畸形 JSON 只存在两种 OpenAI 字符串协议。
    if vendor == "anthropic":
        p = _payload(
            vendor, tool={"call_id": "call_1", "name": "list_files", "args": {"path": "."}}
        )
        expected = {"path": "."}
    else:
        p = payload(vendor, raw='{"path":')
        expected = '{"path":'
    result = make(vendor, FakeTransport(p)).complete(list(MESSAGES), tool_manifest())
    assert result.finish_reason == "tool_use"
    assert result.tool_calls[0].arguments == expected
