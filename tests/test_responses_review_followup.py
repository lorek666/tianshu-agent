"""独立第二读者追加探针：保存点前后计数、挂起父任务与协议负控。"""

import copy
import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from support import approve_pending, engine_for, stack_for
from test_responses_review import LocalTransport, adapter, answer, call, msg

from tianshu import event_schema
from tianshu._responses_rebuild import rebuild_responses_progress
from tianshu.control import ControlPlane
from tianshu.errors import TianshuError
from tianshu.events import EventLog, canonical_bytes
from tianshu.model import ModelError
from tianshu.replay import replay
from tianshu.tools import tool_manifest


@pytest.fixture(autouse=True)
def synthetic_credentials(monkeypatch):
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "env")
    monkeypatch.setenv("TIANSHU_API_KEY", "review-only")


def digest(cursor):
    return hashlib.sha256(canonical_bytes(cursor.to_dict())).hexdigest()


def select(events, kind):
    return [e for e in events if e.type == kind]


def drop_projections(log, run_id):
    with log.transaction():
        log.conn.execute("DELETE FROM run_cursors WHERE run_id=?", (run_id,))
        log.conn.execute("DELETE FROM run_turns WHERE run_id=?", (run_id,))


def child_process(root, case):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(root / "authority")
    assert os.environ["TIANSHU_TEST_LOCK_ROOT"]
    assert os.environ["TIANSHU_CREDENTIAL_BACKEND"] == "env"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case, str(root)],
        capture_output=True,
        text=True,
        timeout=30,
        env={k: v for k, v in os.environ.items() if k != "TIANSHU_FAILPOINT"},
    )
    print("child:", proc.stdout.strip(), proc.stderr.strip())
    point = "after_result" if case == "late_tool" else "model-result-before-commit"
    assert proc.returncode == 77 and f"[failpoint] {point}" in proc.stderr, proc.stderr


def child(case, root):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(root / "authority")
    ws = root / "ws"
    ws.mkdir()
    with closing(EventLog(root / "db", blob_dir=root / "blobs")) as log:
        stack = stack_for(log, data_root=root)
        task = stack.tasks.create_task("inspect local fixtures", [str(ws)])
        if case == "late_tool":
            (ws / "note").write_text("before")
            reads = {**call(), "call_id": "read-first", "id": "item-read"}
            write = call('{"path":"note","content":"after"}', "modify_text")
            model = adapter(LocalTransport(answer([reads, write])))
            first = engine_for(stack, model).start(task.task_id, out_name="")
            assert first.state == "waiting_approval"
            bound = stack.tasks.load_cursor(first.run_id)[0].pending_proposals[0]
            assert bound["request_id"] and approve_pending(stack, first.run_id) == [
                bound["approval_id"]
            ]
            os.environ["TIANSHU_FAILPOINT"] = "after_result"
            engine_for(stack, model).resume_run(first.run_id)
        elif case == "no_checkpoint":
            model = adapter(LocalTransport(answer([msg()])))
            rpc = ControlPlane(stack, model=model)
            run_id = rpc.handle("run.start", {"task_id": task.task_id, "out_name": ""})["run"][
                "run_id"
            ]
            assert stack.tasks.load_cursor(run_id) is None
            os.environ["TIANSHU_FAILPOINT"] = "model-result-before-commit"
            engine_for(stack, model).resume_run(run_id)
        elif case == "with_checkpoint":
            tr = LocalTransport(answer([call()]), answer([msg()]))

            def crash_second_call():
                if len(tr.bodies) == 2:
                    os.environ["TIANSHU_FAILPOINT"] = "model-result-before-commit"

            tr.on_response = crash_second_call
            engine_for(stack, adapter(tr)).start(task.task_id, out_name="")
        else:
            raise AssertionError(case)
        raise AssertionError("child did not reach the expected crashpoint")


@pytest.mark.parametrize("restore_first", [False, True], ids=["direct", "explicit-rebuild"])
def test_committed_tool_after_approval_checkpoint(tmp_path, restore_first):
    child_process(tmp_path, "late_tool")
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        (run_id,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        before = log.read_run(run_id)
        saved = select(before, "run.cursor")[-1]
        tool = select(before, "tool.result")[-1]
        assert saved.seq < tool.seq and tool.payload["call_id"] == "call-1"
        assert tool.payload["effect_state"] == "applied"
        cursor, meta = stack.tasks.load_cursor(run_id)
        (proposal,) = cursor.pending_proposals
        assert digest(cursor) == saved.payload["payload_hash"]
        assert cursor.completed_call_ids == ["read-first"]
        assert meta["claim_epoch"] == saved.payload["claim_epoch"]
        assert proposal["request_id"] and proposal["approval_id"]
        assert len(select(before, "tool.intent")) == len(select(before, "receipt")) == 1
        drop_projections(log, run_id)
        if restore_first:
            stack.tasks.rebuild_from_events()
            restored, restored_meta = stack.tasks.load_cursor(run_id)
            assert restored.to_dict() == cursor.to_dict()
            assert restored_meta["claim_epoch"] == meta["claim_epoch"]
            assert digest(restored) == saved.payload["payload_hash"]
            assert [(e.seq, e.payload_hash) for e in before] == [
                (e.seq, e.payload_hash) for e in log.read_run(run_id)
            ]
        tr = LocalTransport(answer([msg()]))
        resumed = engine_for(stack, adapter(tr)).resume_run(run_id)
        after = log.read_run(run_id)
        assert len(tr.bodies) == 1 and resumed.outcome == "succeeded"
        tool_inputs = [i for i in tr.bodies[0]["input"] if i.get("type") == "function_call_output"]
        assert [i["call_id"] for i in tool_inputs] == ["read-first", "call-1"]
        assert json.loads(tool_inputs[1]["output"]) == tool.payload["output"]
        for kind in ("tool.intent", "tool.result", "receipt", "approval.requested"):
            assert [e.seq for e in select(after, kind)] == [e.seq for e in select(before, kind)]
        assert (tmp_path / "ws" / "note").read_text() == "after"
        assert replay(log, run_id).ok
        final, final_meta = stack.tasks.load_cursor(run_id)
        stack.tasks.rebuild_from_events()
        rebuilt, re_meta = stack.tasks.load_cursor(run_id)
        assert final.to_dict() == rebuilt.to_dict()
        assert final_meta["claim_epoch"] == re_meta["claim_epoch"]
        print(
            "late tool:",
            saved.seq,
            "<",
            tool.seq,
            "replayed outputs",
            [i["call_id"] for i in tool_inputs],
            "write intents / receipts",
            len(select(after, "tool.intent")),
            len(select(after, "receipt")),
        )


@pytest.mark.parametrize("case", ["with_checkpoint", "no_checkpoint"])
@pytest.mark.parametrize("restore", ["retained", "dropped", "rebuilt"])
def test_attempts_on_both_sides_of_checkpoint(tmp_path, case, restore):
    child_process(tmp_path, case)
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        (run_id,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        before = log.read_run(run_id)
        checkpoints = select(before, "run.cursor")
        seq = checkpoints[-1].seq if checkpoints else 0
        settled = case == "with_checkpoint"
        assert bool(checkpoints) is settled
        intents = select(before, "model.intent")
        assert len([e for e in intents if e.seq <= seq]) == int(settled)
        assert len([e for e in intents if e.seq > seq]) == 1
        if restore != "retained":
            drop_projections(log, run_id)
        if restore == "rebuilt":
            stack.tasks.rebuild_from_events()
        tr = LocalTransport(answer([msg()]))
        resumed = engine_for(stack, adapter(tr)).resume_run(run_id)
        assert resumed.outcome == "noop" and len(tr.bodies) == 1
        after = log.read_run(run_id)
        intents = select(after, "model.intent")
        assert [i.payload["attempt"] for i in intents] == ([1, 1, 2] if settled else [1, 2])
        ledger = stack.tasks.load_cursor(run_id)[0].budget_ledger
        assert select(after, "run.end")[-1].payload["unbilled_model_intents"] == 1
        assert resumed.model_attempts_known and ledger["model_attempts_known"]
        print(
            case,
            "restore",
            restore,
            "last cursor seq",
            seq,
            "intents",
            len(intents),
            "reported",
            resumed.model_attempts,
            "saved",
            ledger["model_attempts"],
        )
        assert resumed.model_attempts == ledger["model_attempts"] == len(intents)
        final, _ = stack.tasks.load_cursor(run_id)
        stack.tasks.rebuild_from_events()
        assert stack.tasks.load_cursor(run_id)[0].to_dict() == final.to_dict()


def test_parked_cursor_hash_round_trip(tmp_path):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(tmp_path / "authority")
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        task = stack.tasks.create_task("inspect a pending local job", [str(ws)], max_subtasks=1)
        item = call('{"subtask_id":"queued","goal":"read local fixture"}', "subtask.spawn")
        tr = LocalTransport(answer([item]), answer([msg()]))
        engine = engine_for(stack, adapter(tr))
        waiting = engine.start(task.task_id, out_name="")
        run_id = waiting.run_id
        assert waiting.state == "waiting_approval" and approve_pending(stack, run_id)
        parked = engine.resume_run(run_id)
        assert parked.state == "running" and stack.claims.job_for_run(run_id).state == "waiting"
        assert stack.tasks.count_unfinished_subtasks(run_id) == 1
        events = log.read_run(run_id)
        saved = select(events, "run.cursor")[-1]
        park = select(events, "job.parked")[-1]
        assert saved.seq < park.seq and park.payload["reason"] == "waiting_children"
        cursor, _ = stack.tasks.load_cursor(run_id)
        assert cursor.model_turn is None and digest(cursor) == saved.payload["payload_hash"]
        print(
            "parked: durable cursor seq",
            saved.seq,
            "park seq",
            park.seq,
            "original digest matches",
            digest(cursor) == saved.payload["payload_hash"],
        )
        # The documented rebuild must also work with intact, known-good derived rows.
        stack.tasks.rebuild_from_events()
        assert digest(stack.tasks.load_cursor(run_id)[0]) == digest(cursor)
        assert len(tr.bodies) == 2
        # 配对只解释真实的清空顺序，不豁免摘要，也不借别的 claim/原因清空。
        for mutation in ("hash", "claim", "reason"):
            changed = []
            for event in events:
                payload = event.payload
                if mutation == "hash" and event.seq == saved.seq:
                    payload = {**payload, "payload_hash": "0" * 64}
                elif event.seq == park.seq:
                    if mutation == "claim":
                        payload = {**payload, "claim_epoch": payload["claim_epoch"] + 1}
                    elif mutation == "reason":
                        payload = {**payload, "reason": "paused"}
                changed.append(replace(event, payload=payload))
            with pytest.raises(TianshuError, match="游标不能"):
                rebuild_responses_progress(changed)


@pytest.mark.parametrize("conflict", [False, True])
def test_done_call_id_identity(conflict):
    item = call()
    start = {**item, "status": "in_progress", "arguments": ""}
    done = {
        "type": "response.function_call_arguments.done",
        "output_index": 0,
        "item_id": item["id"],
        "call_id": "other" if conflict else item["call_id"],
        "name": item["name"],
        "arguments": item["arguments"],
    }
    frames = [
        {
            "type": "response.created",
            "response": {"id": "resp-1", "status": "in_progress", "output": []},
        },
        {"type": "response.output_item.added", "output_index": 0, "item": start},
        done,
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": answer([item])},
    ]

    class Stream:
        def post_stream(self, *_, **__):
            for i, frame in enumerate(frames):
                yield frame["type"], json.dumps({**frame, "sequence_number": i})

    def read():
        return adapter(Stream()).stream_complete([], tool_manifest(), lambda _: None)

    if conflict:
        with pytest.raises(ModelError):
            read()
    else:
        response = read()
        assert (
            response.finish_reason == "tool_use"
            and response.tool_calls[0].call_id == item["call_id"]
        )


@pytest.mark.parametrize(
    "left,right,valid", [(True, 1, False), (0, False, False), ("1", 1, False), (True, True, True)]
)
def test_nested_projection_types(left, right, valid):
    nested = {"outer": [{"field": left, "empty": []}]}
    tr = LocalTransport(answer([call(json.dumps(nested))]))
    resp = adapter(tr).complete([], tool_manifest())
    body = copy.deepcopy({**event_schema.minimal_payload("model.result"), **resp.to_payload()})
    assert not event_schema.payload_errors("model.result", body, 2)
    body["tool_calls"][0]["arguments"] = {"outer": [{"empty": [], "field": right}]}
    errors = event_schema.payload_errors("model.result", body, 2)
    assert bool(errors) is not valid


def test_control_crash_then_full_projection_rebuild(tmp_path):
    child_process(tmp_path, "no_checkpoint")
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        (rid,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        result = engine_for(stack, adapter(LocalTransport(answer([msg()])))).resume_run(rid)
        assert result.outcome == "noop"
        events = log.read_run(rid)
        (saved,) = select(events, "run.cursor")
        cursor, _ = stack.tasks.load_cursor(rid)
        assert digest(cursor) == saved.payload["payload_hash"]
        print(
            "control crash: committed checkpoint matches original stored row;",
            "attempts",
            cursor.budget_ledger["model_attempts"],
            "/",
            len(select(events, "model.intent")),
        )
        stack.tasks.rebuild_from_events()
        assert digest(stack.tasks.load_cursor(rid)[0]) == digest(cursor)


def test_direct_resume_parked_when_derived_rows_missing(tmp_path):
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        task = stack.tasks.create_task("inspect local fixture", [str(ws)], max_subtasks=1)
        spawn = call('{"subtask_id":"one","goal":"read fixture"}', "subtask.spawn")
        tr = LocalTransport(answer([spawn]), answer([msg()]))
        engine = engine_for(stack, adapter(tr))
        first = engine.start(task.task_id, out_name="")
        rid = first.run_id
        assert first.state == "waiting_approval" and approve_pending(stack, rid)
        assert (
            engine.resume_run(rid).state == "running"
            and stack.claims.job_for_run(rid).state == "waiting"
        )
        events = log.read_run(rid)
        saved = select(events, "run.cursor")[-1]
        park = select(events, "job.parked")[-1]
        assert park.seq == saved.seq + 1
        preceding = [e for e in events if e.seq < saved.seq]
        previous_cursor, _, previous = rebuild_responses_progress(preceding)
        assert previous_cursor.model_turn["finish_reason"] == "end_turn"
        assert previous.payload["payload_hash"] == digest(previous_cursor)
        original, _ = stack.tasks.load_cursor(rid)
        assert digest(original) == saved.payload["payload_hash"] and original.model_turn is None
        original_payload, previous_payload = original.to_dict(), previous_cursor.to_dict()
        assert {k for k in original_payload if original_payload[k] != previous_payload[k]} == {
            "model_turn"
        }
        print(
            "park: earlier checkpoints valid; only model_turn changes; cursor / park",
            saved.seq,
            park.seq,
        )
        drop_projections(log, rid)
        retry = LocalTransport(answer([msg()]))
        try:
            resumed = engine_for(stack, adapter(retry)).resume_run(rid)
        finally:
            print("park: new model calls", len(retry.bodies))
        assert resumed.state == "running" and stack.claims.job_for_run(rid).state == "waiting"
        assert stack.tasks.count_unfinished_subtasks(rid) == 1


if __name__ == "__main__":
    child(sys.argv[1], Path(sys.argv[2]))
