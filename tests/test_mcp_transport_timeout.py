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


def _release_peer(peer: socket.socket) -> None:
    try:
        peer.sendall(b"x")
    except OSError:
        pass  # 超时修复会回收不再可用的服务端；坏实现则靠屏障释放才退出。
    peer.close()


def _release(peer: socket.socket, worker: threading.Thread) -> None:
    _release_peer(peer)
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
        done = threading.Event()
        call_times = {}
        observation = {}
        log = None
        peer = None
        observer = None
        primary_error = None
        original_call_tool = StdioServer.call_tool

        def watch_call():
            try:
                try:
                    observation["returned"] = done.wait(2)
                finally:
                    _release_peer(peer)
            except BaseException as exc:
                observation["error"] = exc

        def observed_call_tool(server, *args, **kwargs):
            nonlocal observer
            call_times["start_ns"] = time.monotonic_ns()
            try:
                try:
                    # 只有真实调用入口启动监控；装配、发现、审批与落盘不占发送期限。
                    observer = threading.Thread(
                        target=watch_call, name="mcp-send-deadline", daemon=True
                    )
                    observer.start()
                except BaseException as exc:
                    observation["entry_error"] = exc
                    raise
                return original_call_tool(server, *args, **kwargs)
            finally:
                call_times["end_ns"] = time.monotonic_ns()
                done.set()

        monkeypatch.setattr(StdioServer, "call_tool", observed_call_tool)

        def cleanup():
            try:
                if peer is not None and (observer is None or observer.ident is None):
                    _release_peer(peer)
            finally:
                try:
                    clients.close_all()
                finally:
                    try:
                        if observer is not None and observer.ident is not None:
                            observer.join(5)
                            assert not observer.is_alive(), "MCP 调用监控线程未退出"
                    finally:
                        if log is not None:
                            log.close()

        try:
            # SQLite 连接的创建、装配、执行、回放与关闭始终属于当前线程。
            log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
            stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
            task = stack.tasks.create_task("调用 MCP", [], mcp_servers=[config.name])
            model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall("c1", "mcp__backpressure__echo", {"message": "x" * 1_000_000})
                        ],
                    ),
                    ModelResponse("end_turn", content="结束。"),
                ]
            )
            engine = engine_for(stack, model)
            pending = engine.start(task.task_id, out_name="out.md")
            assert pending.state == "waiting_approval"
            approve_pending(stack, pending.run_id)
            peer, _ = control.accept()
            assert peer.recv(5) == b"ready"
            engine.resume_run(pending.run_id)
            if "entry_error" in observation:
                raise observation["entry_error"]
            assert observer is not None and observer.ident is not None, "真实派发未进入 MCP 调用"
            observer.join(5)
            assert not observer.is_alive(), "MCP 调用监控线程未退出"
            assert "error" not in observation, observation
            assert observation["returned"], "真实派发因 MCP stdin 背压突破调用期限"
            # 监控线程即使被延迟调度，也不能让已经超时返回的调用冒充按期完成。
            assert call_times["end_ns"] - call_times["start_ns"] <= 2_000_000_000
            before = list(log.read_run(pending.run_id, verify=True))
            results = [event.payload for event in before if event.type == "tool.result"]
            assert len(results) == 1
            assert results[0]["status"] == "failed"
            assert results[0]["effect_state"] == "unknown"
            assert results[0]["output"]["reason"] == "mcp.timeout"
            assert not [event for event in before if event.type == "receipt"]
            replay(log, pending.run_id)
            replay(log, pending.run_id)
            assert list(log.read_run(pending.run_id, verify=True)) == before
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                cleanup()
            except BaseException as exc:
                if primary_error is None:
                    raise
                primary_error.add_note(f"MCP 测试清理同时失败：{type(exc).__name__}: {exc}")


def test_stalled_send_finishes_gated_setup_before_waiting_for_peer(tmp_path, monkeypatch):
    """前置装配停在屏障时，不能由另一个线程先启动 ready 期限。"""
    owner = threading.current_thread()
    entered = threading.Event()
    release = threading.Event()
    setup = {}
    controller_errors = []
    original_build_stack = build_stack
    original_server_config = _server_config

    def gated_build_stack(*args, **kwargs):
        setup["thread"] = threading.current_thread()
        entered.set()
        assert release.wait(5), "受控前置装配屏障未释放"
        assert setup["thread"] is owner, "前置装配离开了调用者的 SQLite owner 线程"
        stack = original_build_stack(*args, **kwargs)
        setup["finished"] = True
        return stack

    @contextlib.contextmanager
    def observed_server_config(*args, **kwargs):
        with original_server_config(*args, **kwargs) as (config, control):
            class ReadyControl:
                def accept(self):
                    assert setup.get("finished"), "前置装配未完成就开始等待 MCP ready"
                    return control.accept()

            yield config, ReadyControl()

    def release_setup():
        try:
            assert entered.wait(5), "未进入受控前置装配"
        except BaseException as exc:
            controller_errors.append(exc)
        finally:
            release.set()

    monkeypatch.setattr(sys.modules[__name__], "build_stack", gated_build_stack)
    monkeypatch.setattr(sys.modules[__name__], "_server_config", observed_server_config)
    controller = threading.Thread(target=release_setup, name="mcp-setup-barrier", daemon=True)
    controller.start()
    try:
        test_stalled_send_is_unknown_and_replay_does_not_retry(tmp_path, monkeypatch)
    finally:
        release.set()
        controller.join(5)
    assert not controller.is_alive(), "前置装配屏障线程未退出"
    assert not controller_errors, controller_errors


@pytest.mark.parametrize("stage", ["build_stack", "approve_pending", "observer_start"])
def test_stalled_send_preserves_prelude_error_and_reaps_children(tmp_path, monkeypatch, stage):
    injected = RuntimeError(f"injected prelude failure: {stage}")
    servers = []
    released_peers = []
    before_threads = set(threading.enumerate())
    original_start = StdioServer.start
    original_thread_start = threading.Thread.start
    original_release_peer = _release_peer

    def observed_start(server):
        servers.append(server)
        return original_start(server)

    def fail_prelude(*args, **kwargs):
        raise injected

    def fail_observer_start(thread, *args, **kwargs):
        if thread.name == "mcp-send-deadline":
            raise injected
        return original_thread_start(thread, *args, **kwargs)

    def observed_release_peer(peer):
        released_peers.append(peer)
        return original_release_peer(peer)

    monkeypatch.setattr(StdioServer, "start", observed_start)
    monkeypatch.setattr(sys.modules[__name__], "_release_peer", observed_release_peer)
    if stage == "observer_start":
        monkeypatch.setattr(threading.Thread, "start", fail_observer_start)
    else:
        monkeypatch.setattr(sys.modules[__name__], stage, fail_prelude)
    caught = None
    try:
        test_stalled_send_is_unknown_and_replay_does_not_retry(tmp_path, monkeypatch)
    except BaseException as exc:
        caught = exc
    assert caught is injected, repr(caught)
    assert len(servers) == (0 if stage == "build_stack" else 1)
    assert len(released_peers) == (1 if stage == "observer_start" else 0)
    assert all(peer.fileno() == -1 for peer in released_peers)
    assert all(server.exit_code() is not None for server in servers)
    assert not [thread for thread in threading.enumerate() if thread not in before_threads]
