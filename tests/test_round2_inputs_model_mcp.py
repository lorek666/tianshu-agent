"""R2 INPUT-01/02/04: external structures never become invented success."""
from __future__ import annotations

import copy
import json
import sys
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from support import approve_pending, engine_for

from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig
from tianshu.model import (
    AnthropicMessagesModel,
    ModelError,
    ModelResponse,
    OpenAICompatibleModel,
    ScriptedModel,
    ToolCall,
)
from tianshu.tools import tool_manifest

VENDORS = ("openai", "anthropic")
PEER = Path(__file__).parent / "fixtures" / "round2_inputs_mcp_peer.py"


def _payload(vendor, *, call=True):
    if vendor == "openai":
        message = {"content": "" if call else "done"}
        if call:
            message["tool_calls"] = [{
                "id": "c1", "type": "function",
                "function": {"name": "list_files", "arguments": "{}"},
            }]
        return {"choices": [{"message": message,
                             "finish_reason": "tool_calls" if call else "stop"}]}
    return {"content": ([{"type": "tool_use", "id": "c1", "name": "list_files",
                          "input": {}}] if call else [{"type": "text", "text": "done"}]),
            "stop_reason": "tool_use" if call else "end_turn"}


class Responses:
    def __init__(self, vendor, first):
        self.vendor, self.first = vendor, first
        self.count = 0

    def post(self, url, *, body, headers, timeout):
        self.count += 1
        return copy.deepcopy(self.first if self.count == 1 else _payload(self.vendor, call=False))


def _model(vendor, **kwargs):
    cls = OpenAICompatibleModel if vendor == "openai" else AnthropicMessagesModel
    return cls("synthetic-model", api_key="SYNTHETIC-NOT-A-REAL-KEY", **kwargs)


def _malformed(vendor, shape):
    data = _payload(vendor)
    if vendor == "openai":
        call = data["choices"][0]["message"]["tool_calls"][0]
        args, key = call["function"], "arguments"
    else:
        call = data["content"][0]
        args, key = call, "input"
    if shape == "missing":
        del args[key]
    elif shape == "null":
        args[key] = None
    elif shape == "array":
        args[key] = []
    elif shape == "false":
        args[key] = False
    elif shape == "empty_id":
        call["id"] = ""
    elif shape == "bad_name":
        (call["function"] if vendor == "openai" else call)["name"] = []
    elif shape == "nested":
        if vendor == "openai":
            call["function"] = ["invalid"]
        else:
            data["content"].insert(0, {"type": "text", "text": {"bad": "object"}})
    return data


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize(
    "shape", ("missing", "null", "array", "false", "empty_id", "bad_name", "nested")
)
def test_malformed_provider_call_is_protocol_error(vendor, shape):
    model = _model(vendor, transport=Responses(vendor, _malformed(vendor, shape)))
    with pytest.raises(ModelError) as exc:
        model.complete([{"role": "user", "content": "synthetic"}], tool_manifest())
    assert exc.value.kind == "protocol"


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("shape", ("missing", "null", "array"))
def test_malformed_provider_arguments_never_execute_list_files(tmp_path, vendor, shape):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "synthetic.txt").write_text("synthetic", encoding="utf-8")
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = build_stack(log, data_root=tmp_path / "data")
        task = stack.tasks.create_task("list synthetic workspace", [str(workspace)])
        model = _model(vendor, transport=Responses(vendor, _malformed(vendor, shape)))
        result = engine_for(stack, model).start(task.task_id, out_name="out.md")
        events = list(log.read_run(result.run_id, verify=False))
        assert result.state == "failed"
        assert not [event for event in events if event.type == "tool.result"]
        errors = [event.payload for event in events if event.type == "model.error"]
        assert len(errors) == 1
        assert "protocol" in str(errors[0])


@pytest.mark.parametrize("vendor", VENDORS)
def test_valid_empty_arguments_remain_valid(vendor):
    model = _model(vendor, transport=Responses(vendor, _payload(vendor)))
    result = model.complete([{"role": "user", "content": "synthetic"}], tool_manifest())
    assert result.tool_calls == [ToolCall("c1", "list_files", {})]


def test_unknown_openai_tool_type_is_rejected():
    payload = _payload("openai")
    payload["choices"][0]["message"]["tool_calls"][0]["type"] = "future_unknown"
    with pytest.raises(ModelError) as exc:
        _model("openai", transport=Responses("openai", payload)).complete(
            [{"role": "user", "content": "synthetic"}], tool_manifest()
        )
    assert exc.value.kind == "protocol"


@contextmanager
def _server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, payload, *, status=200, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
def test_model_cross_origin_redirect_never_forwards_credentials(vendor, status):
    hits = []

    class Sink(_QuietHandler):
        def do_GET(self):
            hits.append(dict(self.headers))
            self.reply(_payload(vendor, call=False))

    with _server(Sink) as sink:
        class Origin(_QuietHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                credential = self.headers.get("Authorization") or self.headers.get("x-api-key")
                assert "SYNTHETIC-NOT-A-REAL-KEY" in credential
                self.reply({}, status=status, headers={
                    "Location": f"http://127.0.0.1:{sink.server_port}/other-origin",
                })

        with _server(Origin) as origin:
            model = _model(vendor, base_url=f"http://127.0.0.1:{origin.server_port}/v1")
            with pytest.raises(ModelError) as exc:
                model.complete([{"role": "user", "content": "synthetic"}], tool_manifest())
            assert exc.value.kind == "protocol"
            assert exc.value.retryable is False
    assert hits == []


MALFORMED_MCP_RESULTS = [
    None, "not a result", {"content": ["not a block"]}, {},
    {"content": "not a list"}, {"content": [{"type": "text", "text": 1}]},
    {"content": [], "isError": "false"},
]


@pytest.mark.parametrize("wire_result", MALFORMED_MCP_RESULTS)
def test_malformed_mcp_result_is_unknown_without_receipt_and_blocks_target(tmp_path, wire_result):
    _run_mcp_result(tmp_path, wire_result, invalid=True)


@pytest.mark.parametrize("wire_result", [
    {"content": []},
    {"content": [{"type": "text", "text": "ok"}], "isError": False},
    {"content": [{"type": "text", "text": "tool failed"}], "isError": True},
])
def test_valid_mcp_result_preserves_applied_semantics(tmp_path, wire_result):
    _run_mcp_result(tmp_path, wire_result, invalid=False)


def _run_mcp_result(tmp_path, wire_result, *, invalid):
    result_path, calls_path = tmp_path / "result.json", tmp_path / "calls.jsonl"
    result_path.write_text(json.dumps(wire_result), encoding="utf-8")
    config = McpServerConfig(name="probe", command=(sys.executable, str(PEER),
                                                   str(result_path), str(calls_path)))
    clients = McpClients({"probe": config})
    try:
        with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
            stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
            task = stack.tasks.create_task("synthetic MCP", [], mcp_servers=["probe"])
            model = ScriptedModel([
                ModelResponse("tool_use", tool_calls=[ToolCall("c1", "mcp__probe__noop", {})]),
                ModelResponse("end_turn", content="done"),
            ])
            engine = engine_for(stack, model)
            run = engine.start(task.task_id, out_name="out.md")
            assert run.state == "waiting_approval"
            approve_pending(stack, run.run_id)
            run = engine.resume_run(run.run_id)
            events = list(log.read_run(run.run_id, verify=False))
            results = [e.payload for e in events if e.type == "tool.result"]
            receipts = [e for e in events if e.type == "receipt"]
            blocks = [e.payload for e in events if e.type == "recovery.object_blocked"]
            assert len(calls_path.read_text(encoding="utf-8").splitlines()) == 1
            assert len(results) == 1
            if invalid:
                assert results[0]["status"] == "failed"
                assert results[0]["effect_state"] == "unknown"
                assert not receipts
                assert len(blocks) == 1
                assert "mcp://probe/noop" in str(blocks[0])
            else:
                status = "failed" if wire_result.get("isError") else "succeeded"
                assert results[0]["status"] == status
                assert results[0]["effect_state"] == "applied"
                assert len(receipts) == 1
                assert not blocks
    finally:
        clients.close_all()


@pytest.mark.parametrize("vendor", VENDORS)
def test_model_same_origin_redirect_keeps_configured_credentials(vendor):
    hits = []

    class Origin(_QuietHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply({}, status=302, headers={"Location": "/same-origin"})

        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")
                         or self.headers.get("x-api-key")))
            self.reply(_payload(vendor, call=False))

    with _server(Origin) as origin:
        model = _model(vendor, base_url=f"http://127.0.0.1:{origin.server_port}/v1")
        result = model.complete([{"role": "user", "content": "synthetic"}], tool_manifest())
    assert result.finish_reason == "end_turn"
    expected_key = "SYNTHETIC-NOT-A-REAL-KEY"
    if vendor == "openai":
        expected_key = "Bearer " + expected_key
    assert hits == [("/same-origin", expected_key)]
