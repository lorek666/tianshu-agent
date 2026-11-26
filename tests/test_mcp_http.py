"""Real HTTP protocol and Dispatcher, with test socket substituted for pinned TLS."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from support import approve_pending, engine_for

from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpConfigError, McpServerConfig
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import verify_receipts
from tianshu.run_policy import EFFECTS
from tianshu.settings_control import mcp_list_view


@pytest.fixture
def connector(monkeypatch):
    requests = []
    behavior = {"status": 200}
    observed_after_intent = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            requests.append((self.path, dict(self.headers), body))
            method = body["method"]
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            else:
                if "probe" in behavior:
                    observed_after_intent.append(behavior["probe"]())
                if behavior["status"] != 200:
                    self.send_response(behavior["status"])
                    self.send_header("Location", "https://other.example/steal")
                    self.end_headers()
                    return
                result = {
                    "content": [{"type": "text", "text": body["params"]["arguments"]["message"]}]
                }
            response = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
            framing = behavior.get("framing", "sse") if method == "tools/call" else "fixed"
            use_sse = framing in {"sse", "sse-open", "chunked-sse-open", "chunked-sse-bad"}
            if use_sse:
                response = b": heartbeat\r\ndata: " + response + b"\r\n\r\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if use_sse else "application/json")
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "fixture-session")
            self.send_header("Connection", "close")
            if framing.startswith("chunked"):
                self.send_header("Transfer-Encoding", "chunked")
                if framing == "chunked-conflict":
                    self.send_header("Content-Length", str(len(response)))
                end = b"0\r\n\r\n"
                if framing == "chunked-trailers":
                    end = b"0\r\nX-Fixture: trailer\r\n\r\n"
                elif framing == "chunked-short-trailer":
                    end = b"0\r\nX-Fixture: trailer\r\n"
                delimiter = b"xx" if framing == "chunked-bad-delimiter" else b"\r\n"
                extension = ";fixture=yes" if framing == "chunked-trailers" else ""
                def chunk(data, separator=b"\r\n", params=""):
                    return f"{len(data):X}{params}\r\n".encode() + data + separator

                if use_sse:
                    middle = len(response) // 2
                    first_delimiter = b"xx" if framing == "chunked-sse-bad" else b"\r\n"
                    response = chunk(response[:middle], first_delimiter) + chunk(response[middle:])
                else:
                    response = chunk(response, delimiter, extension) + end
            elif framing != "close-delimited":
                length = len(response)
                if framing in {"fixed-short", "sse-open"}:
                    length += 7
                elif framing == "fixed-oversized":
                    length += 1_048_576
                elif framing == "fixed-duplicate":
                    self.send_header("Content-Length", str(length))
                self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(response)
            if framing in {"sse-open", "chunked-sse-open", "chunked-sse-bad"}:
                self.wfile.flush()
                behavior["released_before_close"] = behavior["close_stream"].wait(timeout=15)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from tianshu import net

    def connect(target, timeout):
        assert (target.origin + target.path) == "https://fixture.example/mcp"
        return http.client.HTTPConnection(*server.server_address, timeout=timeout)

    monkeypatch.setattr(net, "_connect", connect)
    monkeypatch.setenv("TEST_REMOTE_MCP_TOKEN", "fixture-token")
    try:
        yield requests, behavior, observed_after_intent
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def config():
    return McpServerConfig.from_dict(
        "web", {"url": "https://fixture.example/mcp", "bearer_token_env": "TEST_REMOTE_MCP_TOKEN"}
    )


def test_mission_remote_schema_identity_survives_new_session(connector):
    from tianshu.mission import tool_identity
    from tianshu.tool_registry import build_registry

    clients = [McpClients({"web": config()}), McpClients({"web": config()})]
    try:
        registries = []
        for client in clients:
            client.start_remotes(("web",))
            registries.append(build_registry(client))
        first, second = registries
        one, two = first.spec("mcp__web__echo"), second.spec("mcp__web__echo")
        assert one.version != two.version, "审批仍须绑定具体传输 Session"
        assert tool_identity(one, first) == tool_identity(two, second)
    finally:
        for client in clients:
            client.close_all()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://x:y@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com/mcp#other",
    ],
)
def test_rejects_untrusted_endpoint_configuration(url):
    with pytest.raises(McpConfigError):
        McpServerConfig.from_dict("web", {"url": url})


@pytest.mark.parametrize(("status", "framing", "expected_effect"), [
    (307, "sse", "unknown"),
    (200, "sse", "applied"),
    (200, "sse-open", "applied"),
    (200, "fixed", "applied"),
    (200, "fixed-short", "unknown"),
    (200, "fixed-oversized", "unknown"),
    (200, "fixed-duplicate", "unknown"),
    (200, "close-delimited", "unknown"),
    (200, "chunked", "applied"),
    (200, "chunked-trailers", "applied"),
    (200, "chunked-short-trailer", "unknown"),
    (200, "chunked-bad-delimiter", "unknown"),
    (200, "chunked-conflict", "unknown"),
    (200, "chunked-sse-open", "applied"),
    (200, "chunked-sse-bad", "unknown"),
])
def test_remote_tool_requires_approval_and_complete_result(
    tmp_path, connector, status, framing, expected_effect,
):
    requests, behavior, observed = connector
    behavior["status"] = status
    behavior["framing"] = framing
    close_stream = behavior["close_stream"] = threading.Event()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    clients = McpClients({"web": config()})
    stack = build_stack(log, data_root=tmp_path, mcp_clients=clients)
    script = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("c1", "mcp__web__echo", {"message": "approved arg"})],
            ),
            ModelResponse("end_turn", content="完成"),
        ]
    )
    engine = engine_for(stack, script)
    try:
        mcp_list_view({"web": config()})
        unscoped = stack.tasks.create_task("不使用连接器", [])
        stack.dispatcher.prepare_run(unscoped.task_id, EFFECTS)
        assert requests == []
        task = stack.tasks.create_task("使用连接器", [], mcp_servers=["web"])
        result = engine.start(task.task_id)
        assert result.state == "waiting_approval"
        [approval] = stack.approvals.pending(run_id=result.run_id)
        assert "https://fixture.example/mcp" in approval.reason
        assert [r[2]["method"] for r in requests] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        run_id = result.run_id

        def inspect_committed_intent():
            conn = sqlite3.connect(log.db_path)
            try:
                return (
                    conn.execute(
                        "SELECT 1 FROM events WHERE run_id = ? AND type = 'tool.intent'", (run_id,)
                    ).fetchone()
                    is not None
                )
            finally:
                conn.close()

        behavior["probe"] = inspect_committed_intent
        approve_pending(stack, run_id)
        engine.resume_run(run_id)
        rows = list(log.read_run(run_id))
        assert [r[2]["method"] for r in requests].count("tools/call") == 1
        assert len(requests) == 4
        assert observed == [True]
        assert requests[-1][1]["Mcp-Session-Id"] == "fixture-session"
        assert requests[-1][1]["Authorization"] == "Bearer fixture-token"
        assert requests[-1][1]["MCP-Protocol-Version"] == "2025-06-18"
        assert "fixture-token" not in repr(rows)
        result_row = next(e.payload for e in rows if e.type == "tool.result")
        receipts = [e for e in rows if e.type == "receipt"]
        assert result_row["effect_state"] == expected_effect, result_row["output"]
        if expected_effect == "applied":
            assert result_row["status"] == "succeeded" and len(receipts) == 1
            assert "content" not in result_row["output"]["mcp"]
            assert result_row["output"]["mcp"]["schema"] == "tianshu.mcp-result.v1"
            assert verify_receipts(log, run_id) == []
        else:
            assert result_row["status"] == "failed" and not receipts
        if framing in {"sse-open", "chunked-sse-open", "chunked-sse-bad"}:
            assert "released_before_close" not in behavior, "SSE waited for the stream to close"
    finally:
        close_stream.set()
        clients.close_all()
        log.close()


def test_remote_session_generation_cannot_reuse_approval(tmp_path, connector, monkeypatch):
    requests, _, _ = connector
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    first = McpClients({"web": config()})
    second = McpClients({"web": config()})
    try:
        stack = build_stack(log, data_root=tmp_path, mcp_clients=first)
        task = stack.tasks.create_task("工具会话", [], mcp_servers=["web"])
        original = engine_for(stack, ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "mcp__web__echo", {"message": "old approval"})]),
        ])).start(task.task_id)
        assert original.state == "waiting_approval"
        assert approve_pending(stack, original.run_id)
        original_version = stack.dispatcher.registry.spec("mcp__web__echo").version
        first.close_all()
        monkeypatch.setenv("TEST_REMOTE_MCP_TOKEN", "second-credential")
        resumed = build_stack(log, data_root=tmp_path, mcp_clients=second)
        resumed.dispatcher.prepare_run(task.task_id, EFFECTS)
        assert resumed.dispatcher.registry.spec("mcp__web__echo").version != original_version
        result = engine_for(resumed, ScriptedModel([
            ModelResponse("end_turn", content="结束"),
        ])).resume_run(original.run_id)
        assert result.terminal
        assert all(row[2]["method"] != "tools/call" for row in requests)
        assert len(requests) == 6 and requests[3][1]["Authorization"] == "Bearer second-credential"
        assert not any(e.type == "tool.intent" for e in log.read_run(original.run_id))
    finally:
        first.close_all()
        second.close_all()
        log.close()


@pytest.mark.parametrize("mode", ["run", "goal"])
def test_queued_desktop_run_discovers_connector_in_driver_only(tmp_path, connector, mode):
    from tianshu.control import ControlPlane

    requests, _, _ = connector
    db = tmp_path / "events.db"
    control_log = EventLog(db, blob_dir=tmp_path / "blobs")
    driver_log = EventLog(db, blob_dir=tmp_path / "blobs")
    clients = McpClients({"web": config()})
    control_clients = McpClients({"web": config()})
    try:
        control_stack = build_stack(
            control_log, data_root=tmp_path, mcp_clients=control_clients,
        )
        driver_stack = build_stack(driver_log, data_root=tmp_path, mcp_clients=clients)

        class ChoosingModel(ScriptedModel):
            def complete(self, messages, tools):
                assert any(tool["name"] == "mcp__web__echo" for tool in tools)
                return super().complete(messages, tools)

        model = ChoosingModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "mcp__web__echo", {"message": "desktop"})]),
            ModelResponse("end_turn", content="结束"),
        ])
        plane = ControlPlane(control_stack, model=model, mcp_configs={"web": config()})
        task = plane.handle("task.create", {"goal": "使用工具", "mcp_servers": ["web"]})["task"]
        if mode == "goal":
            goal = plane.handle("goal.create", {
                "task_id": task["task_id"], "goal_revision": task["goal_revision"],
                "criteria": ["工具结果可核对"], "request_id": "create",
                "execution_policy": EFFECTS.to_dict(),
            })["goal"]
            rid = plane.handle("goal.advance", {
                "goal_id": goal["goal_id"], "request_id": "advance",
            })["run"]["run_id"]
        else:
            rid = plane.handle("run.start", {"task_id": task["task_id"]})["run"]["run_id"]
        assert requests == []
        engine = engine_for(driver_stack, model)
        assert engine.resume_run(rid).state == "waiting_approval"
        assert len(requests) == 3
        assert approve_pending(driver_stack, rid)
        assert engine.resume_run(rid).terminal
        assert len(requests) == 4
    finally:
        control_clients.close_all()
        clients.close_all()
        driver_log.close()
        control_log.close()
