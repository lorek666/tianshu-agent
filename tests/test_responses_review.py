"""独立第二读者冻结复审的真实回归探针。"""

import copy
import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing

import pytest
from support import engine_for, stack_for

from tianshu import event_schema
from tianshu.events import EventLog, canonical_bytes
from tianshu.model import ModelError, ScriptedModel
from tianshu.openai_responses import OpenAIResponsesModel
from tianshu.tools import tool_manifest


def msg(text="done"):
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call(arguments='{"path":"."}', name="list_files"):
    return {
        "type": "function_call",
        "id": "item-1",
        "call_id": "call-1",
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def answer(output):
    return {
        "id": "resp-1",
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


class LocalTransport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.bodies = []
        self.on_response = None

    def post(self, url, *, body, **kwargs):
        self.bodies.append(copy.deepcopy(body))
        if self.on_response:
            self.on_response()
        return next(self.responses)


def adapter(transport):
    return OpenAIResponsesModel(
        "gpt-4o-mini",
        api_key="review-only",
        base_url="https://api.openai.com/v1",
        transport=transport,
    )


@pytest.fixture
def opened(tmp_path):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(tmp_path / "authority")
    assert os.environ["TIANSHU_TEST_LOCK_ROOT"]
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        task = stack.tasks.create_task("inspect note", [str(ws)])
        yield log, stack, task, ws


def test_committed_end_then_switch_model(opened):
    log, stack, task, _ = opened
    origin = LocalTransport(answer([msg()]))
    origin.on_response = lambda: stack.tasks.request_pause(
        stack.tasks.get_task(task.task_id).active_run_id
    )
    paused = engine_for(stack, adapter(origin)).start(task.task_id, out_name="")
    assert paused.state == "paused"
    other = ScriptedModel([])
    resumed = engine_for(stack, other).resume_run(paused.run_id)
    print("terminal:", resumed.outcome, resumed.error, "new model calls", other.calls)
    assert other.calls == 0
    assert resumed.outcome == "noop"


def test_rebuild_preserves_saved_approval_identity(opened):
    log, stack, task, ws = opened
    (ws / "note").write_text("before")
    origin = LocalTransport(answer([call('{"path":"note","content":"after"}', "modify_text")]))
    paused = engine_for(stack, adapter(origin)).start(task.task_id, out_name="")
    assert paused.state == "waiting_approval"
    before, before_meta = stack.tasks.load_cursor(paused.run_id)
    old = before.to_dict()
    digest = next(
        event.payload["payload_hash"]
        for event in reversed(log.read_run(paused.run_id))
        if event.type == "run.cursor"
    )
    assert hashlib.sha256(canonical_bytes(old)).hexdigest() == digest
    with log.transaction():
        log.conn.execute("DELETE FROM run_cursors WHERE run_id=?", (paused.run_id,))
        log.conn.execute("DELETE FROM run_turns WHERE run_id=?", (paused.run_id,))
    stack.tasks.rebuild_from_events()
    after, after_meta = stack.tasks.load_cursor(paused.run_id)
    new = after.to_dict()
    print(
        "approval proposal bindings before/after:",
        [
            {key: (bool(old["pending_proposals"][0][key]), bool(new["pending_proposals"][0][key]))}
            for key in ("approval_id", "request_id")
        ],
        "cursor digest matches:",
        hashlib.sha256(canonical_bytes(new)).hexdigest() == digest,
        "claim epochs:",
        before_meta["claim_epoch"],
        after_meta["claim_epoch"],
    )
    assert old == new and before_meta["claim_epoch"] == after_meta["claim_epoch"]


def test_direct_resume_without_cursor(opened):
    log, stack, task, _ = opened
    origin = LocalTransport(answer([msg()]))
    origin.on_response = lambda: stack.tasks.request_pause(
        stack.tasks.get_task(task.task_id).active_run_id
    )
    paused = engine_for(stack, adapter(origin)).start(task.task_id, out_name="")
    assert paused.state == "paused"
    with log.transaction():
        log.conn.execute("DELETE FROM run_cursors WHERE run_id=?", (paused.run_id,))
        log.conn.execute("DELETE FROM run_turns WHERE run_id=?", (paused.run_id,))
    retry = LocalTransport(answer([msg("different")]))
    resumed = engine_for(stack, adapter(retry)).resume_run(paused.run_id)
    print("resume without derived rows:", len(retry.bodies), resumed.outcome, resumed.error)
    assert len(retry.bodies) == 0 and resumed.outcome == "noop"


def test_c3_distinguishes_boolean_from_number():
    resp = adapter(LocalTransport(answer([call('{"limit": true}', "memory.search")]))).complete(
        [], tool_manifest()
    )
    payload = {**event_schema.minimal_payload("model.result"), **resp.to_payload()}
    payload["tool_calls"][0]["arguments"]["limit"] = 1
    errors = event_schema.payload_errors("model.result", payload, 2)
    print("C3 conflicting argument boolean/numeric:", errors)
    assert errors


@pytest.mark.parametrize("mutation", ["added_status", "added_cipher", "done_tool_name"])
def test_sse_observed_lifecycle_conflict(mutation):
    reasoning = {"id": "reason-1", "type": "reasoning", "summary": [], "encrypted_content": "final"}
    function = call()
    added_reason = {**reasoning, "status": "in_progress", "encrypted_content": None}
    added_call = {**function, "status": "in_progress", "arguments": ""}
    done_args = {
        "type": "response.function_call_arguments.done",
        "output_index": 1,
        "item_id": function["id"],
        "name": function["name"],
        "arguments": function["arguments"],
    }
    if mutation == "added_status":
        added_call["status"] = "not-a-status"
    elif mutation == "added_cipher":
        added_reason["encrypted_content"] = "different-initial"
    else:
        done_args["name"] = "different-in-done"
    frames = [
        {
            "type": "response.created",
            "response": {"id": "resp-1", "status": "in_progress", "output": []},
        },
        {"type": "response.output_item.added", "item": added_reason, "output_index": 0},
        {"type": "response.output_item.done", "item": reasoning, "output_index": 0},
        {"type": "response.output_item.added", "item": added_call, "output_index": 1},
        done_args,
        {"type": "response.output_item.done", "item": function, "output_index": 1},
        {"type": "response.completed", "response": answer([reasoning, function])},
    ]

    class Stream:
        def post_stream(self, *args, **kwargs):
            yield from [
                (event["type"], json.dumps({**event, "sequence_number": index}))
                for index, event in enumerate(frames)
            ]

    with pytest.raises(ModelError):
        result = adapter(Stream()).stream_complete([], tool_manifest(), lambda _: None)
        print("accepted conflicting SSE:", mutation, result.finish_reason)


def test_precommit_crash_reports_sent_attempts(tmp_path):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(tmp_path / "authority")
    from test_responses_integration import _CHILD

    child = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        timeout=30,
        env={
            **os.environ,
            "RESPONSES_CASE": str(tmp_path),
            "RESPONSES_TERMINAL": "1",
            "TIANSHU_FAILPOINT": "model-result-before-commit",
        },
    )
    assert child.returncode == 77
    root = tmp_path / "state"
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        stack = stack_for(log, data_root=root)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        tr = LocalTransport(answer([msg()]))
        resume = engine_for(stack, adapter(tr)).resume_run(run_id)
        intents = [
            event.payload["attempt"]
            for event in log.read_run(run_id)
            if event.type == "model.intent"
        ]
        ledger = stack.tasks.load_cursor(run_id)[0].budget_ledger
        end = next(event.payload for event in log.read_run(run_id) if event.type == "run.end")
        print(
            "crash accounting: sent attempts",
            intents,
            "returned",
            resume.model_attempts,
            "saved",
            ledger["model_attempts"],
            "unbilled",
            end["unbilled_model_intents"],
        )
        assert resume.model_attempts == ledger["model_attempts"] == len(intents)


def test_rebuilt_saved_cursor_does_not_absorb_later_tool_result(opened):
    from tianshu._responses_rebuild import rebuild_responses_progress

    log, stack, task, _ = opened
    result = engine_for(stack, adapter(LocalTransport(answer([call()]), answer([msg()])))).start(
        task.task_id, out_name=""
    )
    events = log.read_run(result.run_id)
    result_index = next(i for i, event in enumerate(events) if event.type == "tool.result")
    prefix = events[: result_index + 1]
    saved = next(event for event in reversed(prefix) if event.type == "run.cursor")
    cursor, _, _ = rebuild_responses_progress(log, prefix)
    assert (
        hashlib.sha256(canonical_bytes(cursor.to_dict())).hexdigest()
        == saved.payload["payload_hash"]
    )
