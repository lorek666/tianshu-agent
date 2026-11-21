"""Responses 复用的逐事件 usage v2：冻结历史、封闭字段与真实 loop 提交。"""

import copy
import json
from contextlib import closing
from pathlib import Path

import pytest
from support import engine_for, stack_for
from test_responses_conformance import VENDORS, make, payload
from test_responses_model import Transport, function, model, response

from tianshu import event_schema
from tianshu.events import EventLog
from tianshu.replay import replay


@pytest.mark.parametrize("event_type", ["model.result", "run.model_turn"])
@pytest.mark.parametrize("key", ["cache_read_input_tokens", "cache_creation_input_tokens"])
@pytest.mark.parametrize("value", [None, 0, 3])
def test_both_usage_events_preserve_new_cache_fields_only_at_v2(event_type, key, value):
    assert event_schema.current_version(event_type) == 2
    assert event_schema.known_versions(event_type) == {0, 1, 2}
    p = event_schema.minimal_payload(event_type)
    p["usage"][key] = value
    assert not event_schema.payload_errors(event_type, p, 2)
    assert event_schema.payload_errors(event_type, p, 1)


@pytest.mark.parametrize("event_type", ["model.result", "run.model_turn"])
@pytest.mark.parametrize("key,value", [
    ("cache_read_input_tokens", True), ("cache_creation_input_tokens", False),
    ("cache_read_input_tokens", -1), ("cache_creation_input_tokens", -1.0),
    ("cache_read_input_tokens", 1.5), ("invented_cache_tokens", 1),
])
def test_new_usage_fields_remain_closed_and_nonnegative(event_type, key, value):
    p = event_schema.minimal_payload(event_type)
    p["usage"][key] = value
    assert event_schema.payload_errors(event_type, p, 2)


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("cached", ["absent", None, 0, 3])
def test_real_adapters_loop_commit_cache_usage_and_unknown_price(tmp_path, vendor, cached):
    usage = {"input_tokens": 10, "output_tokens": 5}
    if vendor == "openai-responses":
        raw = copy.deepcopy(usage)
        if cached != "absent":
            raw["input_tokens_details"] = {"cached_tokens": cached}
    else:
        raw = copy.deepcopy(usage)
        if cached != "absent":
            raw["cache_read_input_tokens"] = cached
    if cached != "absent":
        usage["cache_read_input_tokens"] = cached
    data = payload(vendor, usage=raw)
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("hello", [])
        result = engine_for(stack, make(vendor, Transport(data))).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        facts = [e for e in log.read_run(result.run_id)
                 if e.type in ("model.result", "run.model_turn")]
        assert len(facts) == 2
        assert all(e.payload_version == 2 and e.payload["usage"] == usage for e in facts)
        assert result.tokens == 15
        assert stack.tasks.list_model_turns(result.run_id)[0]["usage"] == usage
        if cached in (None, 3):
            assert next(e.payload["cost"] for e in facts if e.type == "model.result")[
                "cost"
            ] is None
        assert replay(log, result.run_id).ok


def test_second_event_failure_rolls_back_result_turn_cursor_and_prevents_dispatch(
    tmp_path, monkeypatch
):
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("hello", [])
        original = EventLog.append

        def reject_turn(self, run_id, kind, data, **kw):
            if kind == "run.model_turn":
                raise ValueError("injected second event failure")
            return original(self, run_id, kind, data, **kw)

        monkeypatch.setattr(EventLog, "append", reject_turn)
        result = engine_for(stack, model(Transport(response([function()])))).start(
            task.task_id, out_name=""
        )
        assert result.outcome == "failed"
        events = log.read_run(result.run_id)
        assert not [e for e in events if e.type in (
            "model.result", "run.model_turn", "tool.intent", "tool.result",
        )]
        assert not stack.tasks.list_model_turns(result.run_id)
        cursor = stack.tasks.load_cursor(result.run_id)[0]
        assert len(cursor.messages) == 2
        assert not cursor.pending_proposals
        end = next(e.payload for e in events if e.type == "run.end")
        assert end["unbilled_model_intents"] == 1
        assert not end["budget_enforced"]
        assert replay(log, result.run_id).ok


def test_independent_model_turn_commit_preserves_cache_and_exact_version(tmp_path):
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("hello", [])
        run = stack.tasks.start_run(task.task_id)
        _, claim = stack.claims.enqueue_and_claim(run.run_id)
        turn = {
            "turn_id": "t1", "finish_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": None, "cache_creation_input_tokens": 3},
        }
        assert stack.tasks.commit_model_turn(run.run_id, turn, claim) == 1
        assert stack.tasks.commit_model_turn(run.run_id, turn, claim) == 1
        saved = [e for e in log.read_run(run.run_id) if e.type == "run.model_turn"]
        assert len(saved) == 1
        assert saved[0].payload_version == 2
        assert saved[0].payload["usage"] == turn["usage"]
        assert stack.tasks.list_model_turns(run.run_id)[0]["usage"] == turn["usage"]


def test_c3_json_preserves_both_historical_usage_schemas():
    root = Path(__file__).resolve().parents[1]
    defs = json.loads((root / "schemas/c3/event.json").read_text())["$defs"]
    for event_type in ("model.result", "run.model_turn"):
        assert defs["payload"][event_type] == event_schema.schema_for(event_type, 2)
        assert defs["historicalPayloadV1"][event_type] == event_schema.schema_for(event_type, 1)
        assert defs["legacyPayload"][event_type] == event_schema.schema_for(event_type, 0)
    for event_type in event_schema.CURRENT_SCHEMAS:
        expected = 2 if event_type in (
            "run.start", "run.end", "model.result", "run.model_turn",
        ) else 1
        assert event_schema.current_version(event_type) == expected


def test_version_row_failure_keeps_immediate_result_and_reopened_state_consistent(tmp_path):
    db = tmp_path / "events.db"
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("hello", [])
        log.conn.execute("""
            CREATE TEMP TRIGGER reject_turn_version BEFORE INSERT ON event_versions
            WHEN NEW.type = 'run.model_turn'
            BEGIN SELECT RAISE(ABORT, 'injected version write failure'); END
        """)
        data = payload("openai", usage={"prompt_tokens": 10, "completion_tokens": 5})
        result = engine_for(stack, make("openai", Transport(data))).start(
            task.task_id, out_name=""
        )
        assert result.outcome == "failed"
        assert result.turns == 0 and result.tokens == 0
        assert not stack.tasks.list_model_turns(result.run_id)
        cursor = stack.tasks.load_cursor(result.run_id)[0]
        assert len(cursor.messages) == 2 and not cursor.pending_proposals
        assert not [e for e in log.read_run(result.run_id)
                    if e.type in ("model.result", "run.model_turn", "tool.intent")]
        assert replay(log, result.run_id).ok
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as reopened:
        stack = stack_for(reopened, data_root=tmp_path)
        assert stack.tasks.get_run(result.run_id).outcome == "failed"
        assert len(stack.tasks.load_cursor(result.run_id)[0].messages) == 2
