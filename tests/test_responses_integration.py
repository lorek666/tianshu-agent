"""Responses 完整 Agent 往返、scope、审批恢复与真实进程事务崩溃验收。"""

import os
import subprocess
import sys

import pytest
from support import approve_pending, engine_for, stack_for
from test_responses_model import Transport, function, message, model, reasoning, response

from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.replay import replay


def _stack(tmp_path):
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    stack = stack_for(log, data_root=root)
    task = stack.tasks.create_task("检查目录", [str(ws)])
    return log, stack, task, ws


def test_real_tool_roundtrip_persists_continuation_replay_and_projection(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    try:
        transport = Transport(response([reasoning(), function()]), response())
        result = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        events = log.read_run(result.run_id)
        results = [e for e in events if e.type == "model.result"]
        assert results[0].payload_version == 2
        saved = results[0].payload["continuation"]
        assert stack.tasks.list_model_turns(result.run_id)[0]["continuation"] == saved
        assert stack.tasks.load_cursor(result.run_id)[0].messages[2]["continuation"] == saved
        assert [e for e in events if e.type == "tool.result"][0].payload["call_id"] == "call_1"
        assert replay(log, result.run_id).ok
        before = [(e.seq, e.payload_hash) for e in events]
        # 删除真实投影，不能把“保留原行后重建 tasks/runs”当成续接恢复证据。
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        assert stack.tasks.list_model_turns(result.run_id)[0]["continuation"] == saved
        assert stack.tasks.load_cursor(result.run_id)[0].messages[2]["continuation"] == saved
        assert [(e.seq, e.payload_hash) for e in log.read_run(result.run_id)] == before
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_responses_over_budget_does_not_spill_or_summarize(tmp_path):
    log, stack, task, ws = _stack(tmp_path)
    (ws / "large.txt").write_bytes(b"x" * 25000)
    f = function()
    f.update(name="read_text", arguments='{"path":"large.txt"}')
    transport = Transport(response([reasoning(), f]), response())
    try:
        result = engine_for(
            stack, model(transport), limits=Limits(context_budget_bytes=12000, summary_mode="model")
        ).start(task.task_id, out_name="")
        assert result.outcome == "failed"
        assert len(transport.calls) == 1
        assert "预算" in result.error
        events = log.read_run(result.run_id)
        intents = [e for e in events if e.type == "model.intent"]
        assert intents[0].payload["context"]["budget_version"] == "ctx-responses-v1"
        assert not intents[0].payload["context"]["dropped_seqs"]
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_cached_usage_survives_both_v2_model_events_in_real_loop(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    data = response()
    data["usage"]["input_tokens_details"] = {"cached_tokens": 4}
    try:
        result = engine_for(stack, model(Transport(data))).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        events = log.read_run(result.run_id)
        saved = next(e.payload for e in events if e.type == "model.result")
        assert saved["usage"]["cache_read_input_tokens"] == 4
        assert saved["cost"]["cost"] is None
        assert stack.tasks.list_model_turns(result.run_id)[0]["usage"] == saved["usage"]
        turn_event = next(e.payload for e in events if e.type == "run.model_turn")
        assert turn_event["usage"] == saved["usage"]
        assert next(e.payload_version for e in events if e.type == "run.model_turn") == 2
        assert replay(log, result.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("arguments", ['{"path":', '[]', '"not-an-object"'])
def test_invalid_arguments_are_refused_and_raw_continuation_survives_replay(tmp_path, arguments):
    log, stack, task, _ = _stack(tmp_path)
    transport = Transport(response([function(arguments=arguments)]), response())
    try:
        result = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        events = log.read_run(result.run_id)
        assert not [e for e in events if e.type in ("tool.intent", "tool.result")]
        assert len([e for e in events if e.type == "tool.not_dispatched"]) == 1
        function_input = next(
            item for item in transport.calls[1]["body"]["input"]
            if item.get("type") == "function_call"
        )
        assert function_input["arguments"] == arguments
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_incomplete_response_with_valid_arguments_never_executes_tool(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    incomplete = response(
        [function()], status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    transport = Transport(incomplete)
    try:
        result = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert result.outcome == "failed"
        events = log.read_run(result.run_id)
        assert len(transport.calls) == 1
        assert not [e for e in events if e.type in ("tool.intent", "tool.result")]
        assert len([e for e in events if e.type == "tool.not_dispatched"]) == 1
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_approval_resume_reuses_saved_model_after_projection_loss(tmp_path):
    log, stack, task, ws = _stack(tmp_path)
    (ws / "note.txt").write_text("before")
    f = function()
    f.update(name="modify_text", arguments='{"path":"note.txt","content":"after"}')
    transport = Transport(response([reasoning(), f]), response())
    try:
        first = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert first.state == "waiting_approval"
        assert len(transport.calls) == 1
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        assert stack.tasks.load_cursor(first.run_id)[0].approval_ids == first.approval_ids
        approve_pending(stack, first.run_id)
        # 批准后换一个 loop 实例，保存的函数提案不能重新问模型。
        second = engine_for(stack, model(transport)).resume_run(first.run_id)
        assert second.outcome in {"noop", "succeeded"}
        assert len(transport.calls) == 2
        assert (ws / "note.txt").read_text() == "after"
        assert len([e for e in log.read_run(first.run_id) if e.type == "tool.intent"]) == 1
        assert replay(log, first.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic", "scripted"])
def test_scope_switch_after_approval_cannot_drop_saved_continuation(tmp_path, vendor):
    from test_model_contract import FakeTransport, _model

    from tianshu.model import ModelResponse, ScriptedModel

    log, stack, task, ws = _stack(tmp_path)
    (ws / "note.txt").write_text("before")
    f = function()
    f.update(name="modify_text", arguments='{"path":"note.txt","content":"after"}')
    transport = Transport(response([reasoning(), f]))
    try:
        first = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert first.state == "waiting_approval"
        approve_pending(stack, first.run_id)
        other = (
            ScriptedModel([ModelResponse("end_turn")])
            if vendor == "scripted"
            else _model(vendor, FakeTransport({}))
        )
        result = engine_for(stack, other).resume_run(first.run_id)
        assert result.outcome == "failed"
        assert "新 Run" in result.error
        assert len([e for e in log.read_run(first.run_id) if e.type == "model.result"]) == 1
    finally:
        log.close()


_CHILD = r"""
import json, os
from pathlib import Path
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, build_stack
from tianshu.openai_responses import OpenAIResponsesModel
root = Path(os.environ["RESPONSES_CASE"])
ws = root / "ws"; ws.mkdir(exist_ok=True)
state = root / "state"; state.mkdir(exist_ok=True)
log = EventLog(state / "events.db", blob_dir=state / "blobs")
stack = build_stack(log, data_root=state)
task = stack.tasks.create_task("检查目录", [str(ws)])
class T:
    def post(self, url, **kw):
        if os.environ.get("RESPONSES_TERMINAL"):
            return {"id":"resp_1","status":"completed","output":[
                {"type":"message","id":"msg_1","role":"assistant","status":"completed",
                 "content":[{"type":"output_text","text":"done","annotations":[]}]}
            ],"usage":{"input_tokens":1,"output_tokens":1}}
        return {"id":"resp_1", "status":"completed", "model":"test-model", "output":[
            {"type":"reasoning","id":"rs_1","summary":[],"encrypted_content":"opaque"},
            {"type":"function_call","id":"fc_1","call_id":"call_1","name":"list_files","arguments":json.dumps({"path":"."}),"status":"completed"}
        ],"usage":{"input_tokens":1,"output_tokens":1}}
m = OpenAIResponsesModel("test-model", api_key="fake", transport=T())
engine = AgentLoop(tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
    dispatcher=stack.dispatcher, approvals=stack.approvals, snapshots=stack.snapshots,
    contents=stack.contents, budget=stack.budget, memory=stack.memory,
    summaries=stack.summaries, model=m)
engine.start(task.task_id, out_name="")
"""


@pytest.mark.parametrize(
    "point,committed", [("model-result-before-commit", False), ("model-result-after-commit", True)]
)
def test_real_subprocess_crash_preserves_atomic_result_turn_cursor(tmp_path, point, committed):
    env = {**os.environ, "TIANSHU_FAILPOINT": point, "RESPONSES_CASE": str(tmp_path)}
    p = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, timeout=45)
    assert p.returncode == 77, p.stderr.decode("utf-8", "replace")
    assert f"[failpoint] {point}".encode() in p.stderr
    root = tmp_path / "state"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        results = [e for e in log.read_run(run_id) if e.type == "model.result"]
        turns = stack.tasks.list_model_turns(run_id)
        cursor = stack.tasks.load_cursor(run_id)
        assert bool(results) is committed
        assert bool(turns) is committed
        assert bool(cursor and any("continuation" in m for m in cursor[0].messages)) is committed
        data = [response()] if committed else [response([reasoning(), function()]), response()]
        transport = Transport(*data)
        result = engine_for(stack, model(transport)).resume_run(run_id)
        assert result.outcome == "noop"
        assert len(transport.calls) == (1 if committed else 2)
        assert len([e for e in log.read_run(run_id) if e.type == "tool.result"]) == 1
        assert replay(log, run_id).ok
    finally:
        log.close()


def test_committed_terminal_result_resumes_without_new_generation(tmp_path):
    env = {
        **os.environ,
        "TIANSHU_FAILPOINT": "model-result-after-commit",
        "RESPONSES_CASE": str(tmp_path),
        "RESPONSES_TERMINAL": "1",
    }
    child = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, timeout=45)
    assert child.returncode == 77, child.stderr.decode("utf-8", "replace")
    root = tmp_path / "state"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        transport = Transport()  # 任何模型调用都会失败，已提交的 end_turn 应直接收尾。
        result = engine_for(stack, model(transport)).resume_run(run_id)
        assert result.outcome == "noop"
        assert not transport.calls
        assert len([e for e in log.read_run(run_id) if e.type == "model.result"]) == 1
        assert replay(log, run_id).ok
    finally:
        log.close()


def test_pause_after_terminal_response_resumes_without_new_generation(tmp_path):
    log, stack, task, _ = _stack(tmp_path)

    class PauseTransport(Transport):
        def post(self, url, **kwargs):
            result = super().post(url, **kwargs)
            run_id = stack.tasks.get_task(task.task_id).active_run_id
            stack.tasks.request_pause(run_id)
            return result

    transport = PauseTransport(response())
    try:
        first = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert first.state == "paused" and len(transport.calls) == 1
        resumed_transport = Transport()
        second = engine_for(stack, model(resumed_transport)).resume_run(first.run_id)
        assert second.outcome == "noop"
        assert not resumed_transport.calls
        assert replay(log, first.run_id).ok
    finally:
        log.close()


def test_default_diagnostic_archive_excludes_continuation(tmp_path):
    import zipfile

    from tianshu.diagnostics_export import export_diagnostics

    log, stack, task, _ = _stack(tmp_path)
    try:
        result = engine_for(stack, model(Transport(response([reasoning(), message()])))).start(
            task.task_id, out_name=""
        )
        assert result.outcome == "noop"
        assert any("continuation" in e.payload for e in log.read_run(result.run_id))
        exported = export_diagnostics(
            tmp_path / "state", tmp_path / "export", sections=["events_timeline"]
        )
        with zipfile.ZipFile(exported["archive_path"]) as archive:
            content = b"".join(archive.read(p) for p in archive.namelist())
        assert b"opaque-secret-reasoning" not in content
        assert b"output_items" not in content
    finally:
        log.close()
