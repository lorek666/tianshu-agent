# 独立复审探针原文

原始文件按字节收录；提取方法与边界见 [README](README.md)。

## test_independent.py

<!-- source-file: test_independent.py; sha256: 5d46fd8bcd3f231668512b43c474a26003100e6f141f41fda47e01b207e24f93; bytes: 11824 -->
```python
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
from tianshu.model import ModelError, ModelResponse, ScriptedModel
from tianshu.openai_responses import OpenAIResponsesModel
from tianshu.tools import tool_manifest


def msg(text="done"):
    return {"id": "msg-1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def call(arguments='{"path":"."}', name="list_files"):
    return {"type": "function_call", "id": "item-1", "call_id": "call-1",
            "name": name, "arguments": arguments, "status": "completed"}


def answer(output):
    return {"id": "resp-1", "status": "completed", "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


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
    return OpenAIResponsesModel("gpt-4o-mini", api_key="review-only",
                                base_url="https://api.openai.com/v1", transport=transport)


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
    digest = next(event.payload["payload_hash"] for event in reversed(log.read_run(paused.run_id))
                  if event.type == "run.cursor")
    assert hashlib.sha256(canonical_bytes(old)).hexdigest() == digest
    with log.transaction():
        log.conn.execute("DELETE FROM run_cursors WHERE run_id=?", (paused.run_id,))
        log.conn.execute("DELETE FROM run_turns WHERE run_id=?", (paused.run_id,))
    stack.tasks.rebuild_from_events()
    after, after_meta = stack.tasks.load_cursor(paused.run_id)
    new = after.to_dict()
    print("approval proposal bindings before/after:", [
        {key: (bool(old["pending_proposals"][0][key]), bool(new["pending_proposals"][0][key]))}
        for key in ("approval_id", "request_id")
    ], "cursor digest matches:", hashlib.sha256(canonical_bytes(new)).hexdigest() == digest,
          "claim epochs:", before_meta["claim_epoch"], after_meta["claim_epoch"])
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
    resp = adapter(LocalTransport(answer([call('{"limit": true}', "memory.search")]))).complete([], tool_manifest())
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
    done_args = {"type": "response.function_call_arguments.done", "output_index": 1,
                 "item_id": function["id"], "name": function["name"], "arguments": function["arguments"]}
    if mutation == "added_status":
        added_call["status"] = "not-a-status"
    elif mutation == "added_cipher":
        added_reason["encrypted_content"] = "different-initial"
    else:
        done_args["name"] = "different-in-done"
    frames = [
        {"type": "response.created", "response": {"id": "resp-1", "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "item": added_reason, "output_index": 0},
        {"type": "response.output_item.done", "item": reasoning, "output_index": 0},
        {"type": "response.output_item.added", "item": added_call, "output_index": 1},
        done_args,
        {"type": "response.output_item.done", "item": function, "output_index": 1},
        {"type": "response.completed", "response": answer([reasoning, function])},
    ]

    class Stream:
        def post_stream(self, *args, **kwargs):
            yield from [(event["type"], json.dumps({**event, "sequence_number": index}))
                        for index, event in enumerate(frames)]

    with pytest.raises(ModelError):
        result = adapter(Stream()).stream_complete([], tool_manifest(), lambda _: None)
        print("accepted conflicting SSE:", mutation, result.finish_reason)


def test_manifest_names():
    import re
    tr = LocalTransport(answer([msg()]))
    adapter(tr).complete([], tool_manifest())
    names = [tool["name"] for tool in tr.bodies[0]["tools"]]
    invalid = [name for name in names if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)]
    print("default wire tool names outside portable function charset:", invalid)


def test_precommit_crash_reports_sent_attempts(tmp_path):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(tmp_path / "authority")
    from test_responses_integration import _CHILD
    child = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, timeout=30,
                           env={**os.environ, "RESPONSES_CASE": str(tmp_path),
                                "RESPONSES_TERMINAL": "1", "TIANSHU_FAILPOINT": "model-result-before-commit"})
    assert child.returncode == 77
    root = tmp_path / "state"
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        stack = stack_for(log, data_root=root)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        tr = LocalTransport(answer([msg()]))
        resume = engine_for(stack, adapter(tr)).resume_run(run_id)
        intents = [event.payload["attempt"] for event in log.read_run(run_id) if event.type == "model.intent"]
        ledger = stack.tasks.load_cursor(run_id)[0].budget_ledger
        end = next(event.payload for event in log.read_run(run_id) if event.type == "run.end")
        print("crash accounting: sent attempts", intents, "returned", resume.model_attempts,
              "saved", ledger["model_attempts"], "unbilled", end["unbilled_model_intents"])
        assert resume.model_attempts == ledger["model_attempts"] == len(intents)


def test_exact_prefix_private_diagnostics_and_old_reader(opened, tmp_path, tmp_path_factory):
    import io
    import tarfile
    import zipfile
    from pathlib import Path
    from tianshu.diagnostics_export import export_diagnostics
    from tianshu.loop import Limits
    from tianshu.replay import replay

    log, stack, task, ws = opened
    sentinel = "opaque-only-independent-review-731"
    opaque = {"id": "rs-1", "type": "reasoning", "encrypted_content": sentinel, "summary": []}
    first = [opaque, call('{"path":"large.txt"}', "read_text")]
    (ws / "large.txt").write_text("PREFIX" * 2000)
    tr = LocalTransport(answer(first), answer([msg()]))
    result = engine_for(stack, adapter(tr), limits=Limits(
        summary_mode="model", context_budget_bytes=70000)).start(task.task_id, out_name="")
    assert result.outcome == "noop"
    initial, following = [body["input"] for body in tr.bodies]
    assert following[:len(initial)] == initial and following[len(initial):-1] == first
    assert following[-1]["call_id"] == "call-1" and "PREFIX" * 2000 in following[-1]["output"]
    events = log.read_run(result.run_id)
    assert all(event.payload["context"]["budget_version"] == "ctx-responses-v1" and
               not event.payload["context"]["dropped_seqs"]
               for event in events if event.type == "model.intent")
    assert replay(log, result.run_id).ok
    archive = export_diagnostics(tmp_path, tmp_path_factory.mktemp("diagnostics"))
    with zipfile.ZipFile(archive["archive_path"]) as bundle:
        assert all(sentinel.encode() not in bundle.read(member) for member in bundle.namelist())
    commit = "a0732d9881247998df24db7af9f1c7989c1ae7a4"
    old_dir = tmp_path / "old"
    raw = subprocess.check_output(["git", "archive", commit, "src/tianshu"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        archive.extractall(old_dir, filter="data")
    program = """
import os, pathlib
from tianshu import events, data_admission
assert pathlib.Path(events.__file__).is_relative_to(os.environ['OLD_REVIEW_PATH'])
try:
    events.EventLog(pathlib.Path(os.environ['REVIEW_DB']))
except data_admission.AdmissionRefused as err:
    assert 'incompatible' in str(err)
    print('old reader:', type(err).__name__, str(err))
else:
    raise AssertionError('old reader accepted new database')
"""
    startup = Path.cwd() / "tests" / "_startup"
    old_proc = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, [startup, old_dir / "src"])),
                                   "OLD_REVIEW_PATH": str(old_dir), "REVIEW_DB": str(tmp_path / "events.db")})
    print("prefix, canonical items, call_id, 12k output, replay and default archive verified;", old_proc.stdout)
    assert old_proc.returncode == 0, old_proc.stderr
```

## test_followup.py

<!-- source-file: test_followup.py; sha256: b989c5f766967c1ffc305db5eddb00e2c045fb5edd7ec9643e1c628f93e4ea75; bytes: 12274 -->
```python
"""Independent offline recovery and wire-contract checks for 6e9bf34."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from support import approve_pending, engine_for, stack_for
from test_independent import LocalTransport, adapter, answer, call, msg

from tianshu import event_schema
from tianshu.control import ControlPlane
from tianshu.events import EventLog, canonical_bytes
from tianshu.model import ModelError
from tianshu.replay import replay
from tianshu.tools import tool_manifest


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
        capture_output=True, text=True, timeout=30,
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
            assert bound["request_id"] and approve_pending(stack, first.run_id) == [bound["approval_id"]]
            os.environ["TIANSHU_FAILPOINT"] = "after_result"
            engine_for(stack, model).resume_run(first.run_id)
        elif case == "no_checkpoint":
            model = adapter(LocalTransport(answer([msg()])))
            rpc = ControlPlane(stack, model=model)
            run_id = rpc.handle("run.start", {"task_id": task.task_id, "out_name": ""})["run"]["run_id"]
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
        proposal, = cursor.pending_proposals
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
        print("late tool:", saved.seq, "<", tool.seq, "replayed outputs", [i["call_id"] for i in tool_inputs],
              "write intents / receipts", len(select(after, "tool.intent")), len(select(after, "receipt")))


@pytest.mark.parametrize("case,drop", [("with_checkpoint", False), ("with_checkpoint", True),
                                        ("no_checkpoint", False)])
def test_attempts_on_both_sides_of_checkpoint(tmp_path, case, drop):
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
        if drop:
            drop_projections(log, run_id)
        tr = LocalTransport(answer([msg()]))
        resumed = engine_for(stack, adapter(tr)).resume_run(run_id)
        assert resumed.outcome == "noop" and len(tr.bodies) == 1
        after = log.read_run(run_id)
        intents = select(after, "model.intent")
        assert [i.payload["attempt"] for i in intents] == ([1, 1, 2] if settled else [1, 2])
        ledger = stack.tasks.load_cursor(run_id)[0].budget_ledger
        assert select(after, "run.end")[-1].payload["unbilled_model_intents"] == 1
        assert resumed.model_attempts_known and ledger["model_attempts_known"]
        print(case, "drop", drop, "last cursor seq", seq, "intents", len(intents),
              "reported", resumed.model_attempts, "saved", ledger["model_attempts"])
        assert resumed.model_attempts == ledger["model_attempts"] == len(intents)


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
        print("parked: durable cursor seq", saved.seq, "park seq", park.seq,
              "original digest matches", digest(cursor) == saved.payload["payload_hash"])
        # The documented rebuild must also work with intact, known-good derived rows.
        stack.tasks.rebuild_from_events()
        assert digest(stack.tasks.load_cursor(run_id)[0]) == digest(cursor)
        assert len(tr.bodies) == 2


@pytest.mark.parametrize("conflict", [False, True])
def test_done_call_id_identity(conflict):
    item = call()
    start = {**item, "status": "in_progress", "arguments": ""}
    done = {"type": "response.function_call_arguments.done", "output_index": 0,
            "item_id": item["id"], "call_id": "other" if conflict else item["call_id"],
            "name": item["name"], "arguments": item["arguments"]}
    frames = [
        {"type": "response.created", "response": {"id": "resp-1", "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": start}, done,
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
        assert response.finish_reason == "tool_use" and response.tool_calls[0].call_id == item["call_id"]


@pytest.mark.parametrize("left,right,valid", [(True, 1, False), (0, False, False),
                                             ("1", 1, False), (True, True, True)])
def test_nested_projection_types(left, right, valid):
    nested = {"outer": [{"field": left, "empty": []}]}
    tr = LocalTransport(answer([call(json.dumps(nested))]))
    resp = adapter(tr).complete([], tool_manifest())
    body = copy.deepcopy({**event_schema.minimal_payload("model.result"), **resp.to_payload()})
    assert not event_schema.payload_errors("model.result", body, 2)
    body["tool_calls"][0]["arguments"] = {"outer": [{"empty": [], "field": right}]}
    errors = event_schema.payload_errors("model.result", body, 2)
    assert bool(errors) is not valid


if __name__ == "__main__":
    child(sys.argv[1], Path(sys.argv[2]))
```

## test_followup_impact.py

<!-- source-file: test_followup_impact.py; sha256: 4d68daaa76126ae3e4db8aac5f817fb5cf9bbf8cf7a448fbda389348cc4e95be; bytes: 3415 -->
```python
from contextlib import closing

from support import approve_pending, engine_for, stack_for
from test_followup import child_process, digest, drop_projections, select
from test_independent import LocalTransport, adapter, answer, call, msg

from tianshu._responses_rebuild import rebuild_responses_progress
from tianshu.events import EventLog


def test_control_crash_then_full_projection_rebuild(tmp_path):
    child_process(tmp_path, "no_checkpoint")
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        (rid,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        result = engine_for(stack, adapter(LocalTransport(answer([msg()])))).resume_run(rid)
        assert result.outcome == "noop"
        events = log.read_run(rid)
        saved, = select(events, "run.cursor")
        cursor, _ = stack.tasks.load_cursor(rid)
        assert digest(cursor) == saved.payload["payload_hash"]
        print("control crash: committed checkpoint matches original stored row;",
              "attempts", cursor.budget_ledger["model_attempts"], "/", len(select(events, "model.intent")))
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
        assert engine.resume_run(rid).state == "running" and stack.claims.job_for_run(rid).state == "waiting"
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
        assert {k for k in original_payload if original_payload[k] != previous_payload[k]} == {"model_turn"}
        print("park: earlier checkpoints valid; only model_turn changes; cursor / park", saved.seq, park.seq)
        drop_projections(log, rid)
        retry = LocalTransport(answer([msg()]))
        try:
            resumed = engine_for(stack, adapter(retry)).resume_run(rid)
        finally:
            print("park: new model calls", len(retry.bodies))
        assert resumed.state == "running" and stack.claims.job_for_run(rid).state == "waiting"
        assert stack.tasks.count_unfinished_subtasks(rid) == 1
```
