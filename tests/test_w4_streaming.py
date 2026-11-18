"""W4 模型流式输出契约、断流保护与 Daemon/Host SSE 瞬态广播端到端测试。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tianshu import host as host_mod
from tianshu import ipc
from tianshu.daemon import serve
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import (
    AnthropicMessagesModel,
    ModelError,
    ModelResponse,
    ModelStreamDelta,
    OpenAICompatibleModel,
    ScriptedModel,
    ToolCall,
)
from tianshu.replay import replay


class _FakeSseTransport:
    def __init__(self, frames: list[tuple[str, str]]) -> None:
        self._frames = list(frames)

    def post(
        self, url: str, *, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        raise AssertionError("流式测试不应回退到整包 post()")

    def post_stream(
        self, url: str, *, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> Iterator[tuple[str, str]]:
        assert body.get("stream") is True
        yield from self._frames


def test_openai_and_anthropic_stream_complete_emit_deltas_and_reject_truncated_stream() -> None:
    def _sse(ev: str, obj: dict[str, Any]) -> tuple[str, str]:
        return (ev, json.dumps(obj))

    # 1. OpenAI SSE 文本 + 工具调用增量重组
    openai_frames = [
        _sse("message", {"choices": [{"delta": {"content": "正在检查"}, "finish_reason": None}]}),
        _sse("message", {"choices": [{"delta": {"content": "目录…"}, "finish_reason": None}]}),
        _sse(
            "message",
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_oa_1",
                                    "type": "function",
                                    "function": {"name": "list_files", "arguments": '{"pa'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
        ),
        _sse(
            "message",
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": 'th":"."}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        ),
        ("message", "[DONE]"),
    ]
    oa_deltas: list[ModelStreamDelta] = []
    oa = OpenAICompatibleModel(
        "gpt-4o-mini", api_key="sk-oa-stream-1234", transport=_FakeSseTransport(openai_frames)
    )
    oa_resp = oa.stream_complete([{"role": "user", "content": "hi"}], [], oa_deltas.append)
    assert oa_resp.finish_reason == "tool_use"
    assert oa_resp.content == "正在检查目录…"
    assert oa_resp.tool_calls == [ToolCall("call_oa_1", "list_files", {"path": "."})]
    assert oa_resp.usage == {"input_tokens": 20, "output_tokens": 10}
    assert [d.kind for d in oa_deltas] == ["text", "text", "tool_call_start"]
    assert oa_deltas[-1].tool_name == "list_files"

    # 负控：OpenAI 流提前断开（无 finish_reason）必须抛 ModelError，绝不伪造 end_turn
    oa_cut = OpenAICompatibleModel(
        "gpt-4o-mini",
        api_key="sk-oa-stream-1234",
        transport=_FakeSseTransport(openai_frames[:2]),
    )
    with pytest.raises(ModelError, match="缺少 finish_reason"):
        oa_cut.stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)

    # 2. Anthropic SSE 文本 + input_json_delta 增量重组
    anth_frames = [
        _sse(
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 30}}},
        ),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "先看文件"},
            },
        ),
        _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_text",
                    "input": {},
                },
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"a.md"}'},
            },
        ),
        _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": 1},
        ),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 15},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    an_deltas: list[ModelStreamDelta] = []
    anth = AnthropicMessagesModel(
        "claude-sonnet-4-5", api_key="sk-ant-stream-1234", transport=_FakeSseTransport(anth_frames)
    )
    an_resp = anth.stream_complete([{"role": "user", "content": "hi"}], [], an_deltas.append)
    assert an_resp.finish_reason == "tool_use"
    assert an_resp.content == "先看文件"
    assert an_resp.tool_calls == [ToolCall("toolu_1", "read_text", {"path": "a.md"})]
    assert an_resp.usage == {"input_tokens": 30, "output_tokens": 15}
    assert [(d.kind, d.text or d.tool_name) for d in an_deltas] == [
        ("text", "先看文件"),
        ("tool_call_start", "read_text"),
    ]

    # 负控 2a：Anthropic 缺少 message_stop 必须抛 ModelError
    anth_cut = AnthropicMessagesModel(
        "claude-sonnet-4-5",
        api_key="sk-ant-stream-1234",
        transport=_FakeSseTransport(anth_frames[:-1]),
    )
    with pytest.raises(ModelError, match="缺少 message_stop"):
        anth_cut.stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)

    # 负控 2b：Anthropic 缺少 content_block_stop 必须抛 ModelError
    no_block_stop = [f for f in anth_frames if f[0] != "content_block_stop"]
    with pytest.raises(ModelError, match="缺少 content_block_stop"):
        AnthropicMessagesModel(
            "claude-sonnet-4-5",
            api_key="sk-ant-stream-1234",
            transport=_FakeSseTransport(no_block_stop),
        ).stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)

    # 负控 2c：Anthropic input 非对象或 partial_json 非字符串必须抛 ModelError（不洗白为 {}）
    for bad_input in ("not-a-dict", None, [1, 2]):
        bad_start_frames = [
            _sse("message_start", {"type": "message_start", "message": {"usage": {}}}),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_bad",
                        "name": "list_files",
                        "input": bad_input,
                    },
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        with pytest.raises(ModelError, match="tool_use input 不是对象"):
            AnthropicMessagesModel(
                "claude-sonnet-4-5",
                api_key="sk-ant-stream-1234",
                transport=_FakeSseTransport(bad_start_frames),
            ).stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)

    bad_delta_frames = [
        _sse("message_start", {"type": "message_start", "message": {"usage": {}}}),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_bad2",
                    "name": "list_files",
                    "input": {},
                },
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": 12345},
            },
        ),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    with pytest.raises(ModelError, match="partial_json 不是字符串"):
        AnthropicMessagesModel(
            "claude-sonnet-4-5",
            api_key="sk-ant-stream-1234",
            transport=_FakeSseTransport(bad_delta_frames),
        ).stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)

    # 负控 2d：OpenAI 畸形 delta.tool_calls 字段必须抛 ModelError
    bad_oa_frames = [
        _sse(
            "message",
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": "zero", "id": "c1", "type": "function"}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ),
        ("message", "[DONE]"),
    ]
    with pytest.raises(ModelError, match="index 必须是非负整数"):
        OpenAICompatibleModel(
            "gpt-4o-mini",
            api_key="sk-oa-stream-1234",
            transport=_FakeSseTransport(bad_oa_frames),
        ).stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)


def test_urllib_transport_maps_incomplete_read_and_bad_status_line_with_redaction(
    tmp_path: Path,
) -> None:
    """真 loopback socket 验证：chunked 截断与畸形状态行均封装为脱敏 ModelError，零泄露。"""
    # 1. Chunked SSE 半截断连 -> ModelError(kind="transport", retryable=True)
    srv1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv1.bind(("127.0.0.1", 0))
    srv1.listen(1)
    port1 = srv1.getsockname()[1]

    def _serve_truncated_chunked() -> None:
        conn, _ = srv1.accept()
        try:
            conn.recv(4096)
            first_chunk = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
            head = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                + f"{len(first_chunk):x}\r\n".encode("ascii")
                + first_chunk
                + b"\r\nff\r\ndata: {\"incomplete"
            )
            conn.sendall(head)
        finally:
            conn.close()
            srv1.close()

    t1 = threading.Thread(target=_serve_truncated_chunked, daemon=True)
    t1.start()
    oa_stream = OpenAICompatibleModel(
        "gpt-4o-mini",
        base_url=f"http://127.0.0.1:{port1}/v1",
        api_key="sk-chunk-secret-111111",
        timeout=5.0,
    )
    with pytest.raises(ModelError) as exc_info:
        oa_stream.stream_complete([{"role": "user", "content": "hi"}], [], lambda _d: None)
    assert exc_info.value.kind == "transport"
    assert exc_info.value.retryable is True
    t1.join(timeout=3.0)

    # 2. BadStatusLine 回显 Bearer 密钥 -> ModelError(kind="protocol") 且 SQLite 零明文
    secret = "sk-live-supersecret-probe-999999"
    srv2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv2.bind(("127.0.0.1", 0))
    srv2.listen(2)
    port2 = srv2.getsockname()[1]

    def _serve_bad_status() -> None:
        conn, _ = srv2.accept()
        try:
            conn.recv(4096)
            conn.sendall(f"Bearer {secret}\r\n".encode())
        finally:
            conn.close()
            srv2.close()

    t2 = threading.Thread(target=_serve_bad_status, daemon=True)
    t2.start()

    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path)
    try:
        bad_model = OpenAICompatibleModel(
            "gpt-4o-mini",
            base_url=f"http://127.0.0.1:{port2}/v1",
            api_key=secret,
            timeout=5.0,
        )
        loop = AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            contents=stack.contents,
            memory=stack.memory,
            summaries=stack.summaries,
            budget=stack.budget,
            model=bad_model,
            limits=Limits(max_turns=2),
            on_model_delta=lambda *_a: None,
        )
        task = stack.tasks.create_task(
            "测试协议异常脱敏", [{"root_path": str(ws), "mode": "readonly"}]
        )
        res = loop.start(task.task_id)
        assert res.state == "failed"
        assert secret not in (res.error or "")
        err_events = [e for e in log.read_run(res.run_id) if e.type == "model.error"]
        assert len(err_events) == 1
        assert err_events[0].payload.get("retryable") is False
        assert "ModelError(kind=protocol" in err_events[0].payload["error"]
        for ev in log.read_after(0):
            assert secret not in json.dumps(ev.payload, ensure_ascii=False)
        assert secret.encode() not in (tmp_path / "events.db").read_bytes()
    finally:
        t2.join(timeout=3.0)
        log.close()


def test_loop_streaming_does_not_pollute_event_log_and_replay_passes(
    tmp_path: Path,
) -> None:
    """流式增量只通过回调瞬态推送，不写进 SQLite `events` 表，且 `replay` 100% 通过。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path)
    captured: list[tuple[str, int, dict[str, Any] | None, bool]] = []
    try:
        model = ScriptedModel(
            [
                ModelResponse(
                    finish_reason="tool_use",
                    tool_calls=[ToolCall("c1", "list_files", {"path": "."})],
                ),
                ModelResponse(
                    finish_reason="end_turn",
                    content="第一段流式回复；第二段流式收尾。",
                ),
            ]
        )
        loop = AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            contents=stack.contents,
            memory=stack.memory,
            summaries=stack.summaries,
            budget=stack.budget,
            model=model,
            limits=Limits(max_turns=4),
            on_model_delta=lambda rid, _tid, turn, delta, done: captured.append(
                (rid, turn, delta, done)
            ),
        )
        task = stack.tasks.create_task("测试流式", [{"root_path": str(ws), "mode": "readonly"}])
        res = loop.start(task.task_id)
        assert res.state == "succeeded"

        # 回调收到工具开始增量、两段文本增量与每轮 done=True
        text_chunks = [c[2]["text"] for c in captured if c[2] and c[2]["kind"] == "text"]
        assert "".join(text_chunks) == "第一段流式回复；第二段流式收尾。"
        assert sum(1 for c in captured if c[3] is True) == 2

        # 事件日志里没有任何逐片流式事件，且 replay 完全通过
        events = log.read_run(res.run_id)
        assert all("stream" not in ev.type for ev in events)
        rep = replay(log, res.run_id)
        assert rep.ok, rep.problems
    finally:
        log.close()


def test_daemon_and_host_forward_transient_model_stream_over_sse(
    tmp_path: Path,
) -> None:
    """Daemon → IPC `model_stream` → Host `/events` SSE 端到端推送实时增量。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    base_dir = None if os.name == "nt" else "/tmp"
    data_root = Path(tempfile.mkdtemp(prefix="tsw4-", dir=base_dir))
    ui_dir = tmp_path / "ui"
    (ui_dir / "assets").mkdir(parents=True)
    (ui_dir / "index.html").write_text(
        '<!doctype html><html><head><meta name="tianshu-token" '
        'content="__TIANSHU_TOKEN__"></head></html>',
        encoding="utf-8",
    )

    class _DelayedStreamModel:
        def capability_profile(self):
            return ScriptedModel([]).capability_profile()

        def complete(self, messages, tools):
            return ModelResponse(finish_reason="end_turn", content="完整流式文本")

        def stream_complete(self, messages, tools, on_delta):
            for chunk in ("你好，", "这是实时", "流式输出！"):
                on_delta(ModelStreamDelta(kind="text", text=chunk))
                time.sleep(0.02)
            return ModelResponse(finish_reason="end_turn", content="你好，这是实时流式输出！")

    stop_daemon = threading.Event()
    daemon_ready = threading.Event()
    daemon_thread = threading.Thread(
        target=serve,
        args=(data_root,),
        kwargs={
            "model": _DelayedStreamModel(),
            "driver_interval_s": 0.02,
            "on_ready": daemon_ready.set,
            "stop_event": stop_daemon,
        },
        daemon=True,
    )
    daemon_thread.start()
    assert daemon_ready.wait(timeout=10.0)

    token = "test-w4-token"
    bridge = host_mod.UiBridge(
        host_mod.BridgeConfig(data_root=data_root, ui_dir=ui_dir, token=token, port=0)
    )
    bridge.serve_background()

    sse_lines: list[str] = []
    sse_opened = threading.Event()

    def _read_sse() -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{bridge.port}/events?since_seq=1",
            headers={"X-Tianshu-Token": token},
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                sse_opened.set()
                for raw in resp:
                    text = raw.decode("utf-8", "replace").rstrip("\r\n")
                    sse_lines.append(text)
                    if '"type": "run.end"' in text or '"type":"run.end"' in text:
                        break
        except Exception:
            sse_opened.set()

    reader_thread = threading.Thread(target=_read_sse, daemon=True)
    reader_thread.start()
    assert sse_opened.wait(timeout=5.0)
    time.sleep(0.05)

    try:
        client = ipc.Client(data_root, timeout=5.0)
        client.connect()
        try:
            created = client.call(
                "task.create",
                {
                    "goal": "测试 SSE 流式",
                    "workspaces": [{"root_path": str(ws), "mode": "readonly"}],
                },
            )
            client.call("run.start", {"task_id": created["task"]["task_id"]})
        finally:
            client.close()

        reader_thread.join(timeout=8.0)
        joined = "\n".join(sse_lines)
        assert "event: model_stream" in joined
        assert "这是实时" in joined
        assert "event: event" in joined
    finally:
        bridge.shutdown()
        stop_daemon.set()
        daemon_thread.join(timeout=5.0)
        shutil.rmtree(data_root, ignore_errors=True)
