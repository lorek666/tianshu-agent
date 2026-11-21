"""Public-only tools cannot dial private literals, even if the user approves the origin."""

from contextlib import closing

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu import net
from tianshu.events import EventLog
from tianshu.mcp import McpServerConfig, McpServerUnavailable
from tianshu.mcp_http import HttpServer
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.resources import NetworkTarget


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "100.64.0.1"])
@pytest.mark.parametrize("tool", ["browser.navigate", "network.search", "remote MCP"])
def test_public_tool_blocks_private_literal_before_dial(tmp_path, monkeypatch, host, tool):
    attempts = []

    def dial(endpoints, *args, **kwargs):
        attempts.extend(endpoints)
        raise OSError("test stops at dial")

    monkeypatch.setattr(net, "_connect_pinned_endpoints", dial)
    origin = f"network://https/{host}"
    if tool == "remote MCP":
        config = McpServerConfig.from_dict("web", {"url": f"https://{host}/mcp"})
        with pytest.raises(McpServerUnavailable):
            HttpServer(config, environ={}).start()
    else:
        args = {"url": origin + "/"} if tool == "browser.navigate" else {
            "origin": origin, "query": "fixture",
        }
        with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
            stack = stack_for(log, data_root=tmp_path)
            task = stack.tasks.create_task("公开网络", [], outbound_allow=[origin])
            engine = engine_for(stack, ScriptedModel([
                ModelResponse("tool_use", tool_calls=[ToolCall("public", tool, args)]),
                ModelResponse("end_turn", content="结束"),
            ]))
            waiting = engine.start(task.task_id)
            assert waiting.state == "waiting_approval"
            assert approve_pending(stack, waiting.run_id)
            assert engine.resume_run(waiting.run_id).terminal
            [result] = [e.payload for e in log.read_run(waiting.run_id) if e.type == "tool.result"]
            assert result["output"]["reason"] == "net.private_address_blocked"
    assert attempts == []


def test_legacy_fetch_still_accepts_explicit_loopback_ip(monkeypatch):
    attempts = []

    def dial(endpoints, *args, **kwargs):
        attempts.extend(endpoints)
        raise OSError("test stops at dial")

    monkeypatch.setattr(net, "_connect_pinned_endpoints", dial)
    with pytest.raises(net.NetError, match="test stops at dial"):
        net.fetch(NetworkTarget.parse("network://http/127.0.0.1:65432/"))
    assert attempts and {entry.sockaddr for entry in attempts} == {("127.0.0.1", 65432)}
