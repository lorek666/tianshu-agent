"""真实 stdio 管道背压必须受 MCP 调用期限约束。"""

from __future__ import annotations

import contextlib
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from support import approve_pending, engine_for

from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig, McpTimeout, StdioServer
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.replay import replay


@contextlib.contextmanager
def _server_config(tmp_path: Path, *, stalled: bool = True):
    """控制 socket 只用于屏障/清理；MCP 仍走真实子进程的 stdin/stdout。"""
    with socket.socket() as control:
        control.bind(("127.0.0.1", 0))
        control.listen(1)
        control.settimeout(5)
        script = tmp_path / "backpressure_server.py"
        script.write_text(
            "import hashlib,json,socket,sys\n"
            "for line in sys.stdin.buffer:\n"
            " m=json.loads(line)\n"
            " method=m.get('method')\n"
            " if method=='initialize':\n"
            "  result={'protocolVersion':m['params']['protocolVersion'],"
            "'capabilities':{'tools':{}},'serverInfo':{'name':'backpressure'}}\n"
            " elif method=='tools/list':\n"
            "  result={'tools':[{'name':'echo','description':'echo hash',"
            "'inputSchema':{'type':'object','properties':{'message':{'type':'string'}},"
            "'required':['message'],'additionalProperties':False}}]}\n"
            " elif method=='tools/call':\n"
            "  digest=hashlib.sha256(m['params']['arguments']['message'].encode()).hexdigest()\n"
            "  result={'content':[{'type':'text','text':digest}]}\n"
            " else:\n"
            "  continue\n"
            " print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)\n"
            f" if method=='tools/list' and {stalled!r}:\n"
            f"  s=socket.create_connection(('127.0.0.1',{control.getsockname()[1]}),timeout=10)\n"
            "  s.sendall(b'ready'); s.recv(1); s.close(); break\n",
            encoding="utf-8",
        )
        config = McpServerConfig(
            name="backpressure",
            command=(sys.executable, str(script)),
            call_timeout_ms=200,
        )
        yield config, control


def _release(peer: socket.socket, worker: threading.Thread) -> None:
    try:
        peer.sendall(b"x")
    except OSError:
        pass  # 超时修复会回收不再可用的服务端；坏实现则靠屏障释放才退出。
    peer.close()
    worker.join(5)
    assert not worker.is_alive(), "MCP 调用线程未退出"


@pytest.mark.parametrize("notification", [False, True])
def test_stalled_stdin_cannot_escape_send_timeout(tmp_path, notification):
    with _server_config(tmp_path) as (config, control):
        server = StdioServer(config)
        server.start()
        peer, _ = control.accept()
        assert peer.recv(5) == b"ready"
        done = threading.Event()
        result = {}

        def send():
            try:
                params = {"message": "x" * 1_000_000}
                if notification:
                    server.notify("probe", params)
                else:
                    server.request("probe", params, timeout_ms=200)
            except BaseException as exc:
                result["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        try:
            returned = done.wait(1.5)
        finally:
            _release(peer, worker)
            server.close()
        assert returned, "200ms 的发送期限未能限制被对端堵住的 stdin"
        assert isinstance(result.get("error"), McpTimeout), result
        assert server.exit_code() is not None


def test_large_request_is_written_completely_to_reading_server(tmp_path):
    with _server_config(tmp_path, stalled=False) as (config, _):
        server = StdioServer(config)
        server.start()
        try:
            message = "x" * 1_000_000
            result = server.call_tool("echo", {"message": message}, timeout_ms=5000)
            assert result.text_parts == (hashlib.sha256(message.encode()).hexdigest(),)
            assert server.is_alive
        finally:
            server.close()


def test_stalled_send_is_unknown_and_replay_does_not_retry(tmp_path, monkeypatch):
    with _server_config(tmp_path) as (config, control):
        clients = McpClients({config.name: config})
        call_ready = threading.Event()
        done = threading.Event()
        call_times = {}
        outcome = {}
        original_call_tool = StdioServer.call_tool

        def observed_call_tool(server, *args, **kwargs):
            call_times["start_ns"] = time.monotonic_ns()
            call_ready.set()
            try:
                return original_call_tool(server, *args, **kwargs)
            finally:
                call_times["end_ns"] = time.monotonic_ns()
                done.set()

        monkeypatch.setattr(StdioServer, "call_tool", observed_call_tool)

        def resume():
            log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
            try:
                stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
                task = stack.tasks.create_task("调用 MCP", [], mcp_servers=[config.name])
                model = ScriptedModel(
                    [
                        ModelResponse(
                            "tool_use",
                            tool_calls=[
                                ToolCall(
                                    "c1", "mcp__backpressure__echo", {"message": "x" * 1_000_000}
                                )
                            ],
                        ),
                        ModelResponse("end_turn", content="结束。"),
                    ]
                )
                engine = engine_for(stack, model)
                pending = engine.start(task.task_id, out_name="out.md")
                outcome["run_id"] = pending.run_id
                assert pending.state == "waiting_approval"
                approve_pending(stack, pending.run_id)
                outcome["result"] = engine.resume_run(pending.run_id)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                try:
                    log.close()
                finally:
                    call_ready.set()  # 审批等前置阶段失败，也要唤醒主线程传播原错。

        worker = threading.Thread(target=resume, daemon=True)
        peer = None
        try:
            worker.start()
            peer, _ = control.accept()
            assert peer.recv(5) == b"ready"
            assert call_ready.wait(5), "MCP 调用未进入，前置阶段未完成"
            assert "error" not in outcome, outcome
            assert "start_ns" in call_times, "worker 结束却未进入 MCP 调用"
            returned = done.wait(2)
        finally:
            if peer is not None:
                try:
                    _release(peer, worker)
                finally:
                    clients.close_all()
            else:
                try:
                    clients.close_all()
                finally:
                    if worker.ident is not None:
                        worker.join(5)
                assert not worker.is_alive(), "MCP 前置阶段失败后 worker 未退出"
        assert returned, "真实派发因 MCP stdin 背压突破调用期限"
        # 主线程即使被延迟调度，也不能让已经超时返回的调用冒充按期完成。
        assert call_times["end_ns"] - call_times["start_ns"] <= 2_000_000_000
        assert "error" not in outcome, outcome
        log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
        run_id = outcome["run_id"]
        before = list(log.read_run(run_id, verify=True))
        results = [event.payload for event in before if event.type == "tool.result"]
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["effect_state"] == "unknown"
        assert results[0]["output"]["reason"] == "mcp.timeout"
        assert not [event for event in before if event.type == "receipt"]
        replay(log, run_id)
        replay(log, run_id)
        assert list(log.read_run(run_id, verify=True)) == before
        log.close()
