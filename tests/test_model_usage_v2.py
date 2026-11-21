"""C1 cache facts cross both exact C3 v2 events without changing historical usage."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from support import engine_for, stack_for
from test_model_contract import VENDORS, FakeTransport, _model, _payload

from tianshu import event_schema
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.loop import Limits
from tianshu.model import AnthropicMessagesModel, ModelResponse, OpenAICompatibleModel
from tianshu.replay import replay
from tianshu.tools import tool_manifest

ROOT = Path(__file__).resolve().parents[1]
TYPES = ("model.result", "run.model_turn")
CACHE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens")
FIXED = json.loads(
    (ROOT / "tests/fixtures/events/model_usage_historical.json").read_text(encoding="utf-8")
)
USAGES = [
    pytest.param({"input_tokens": 11, "output_tokens": 7}, id="absent"),
    pytest.param(
        {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        id="zero",
    ),
    pytest.param(
        {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 9,
            "cache_creation_input_tokens": 3,
        },
        id="positive",
    ),
    pytest.param({"input_tokens": None, "output_tokens": None}, id="unknown"),
    pytest.param(
        {"input_tokens": None, "output_tokens": 7, "cache_read_input_tokens": 9}, id="partial"
    ),
]


def _raw_usage(vendor, usage):
    raw = dict(usage)
    if vendor == "openai":
        raw["prompt_tokens"] = raw.pop("input_tokens")
        raw["completion_tokens"] = raw.pop("output_tokens")
    return raw


def _response(vendor, usage, *, tool=None):
    transport = FakeTransport(_payload(vendor, usage=_raw_usage(vendor, usage), tool=tool))
    model = _model(vendor, transport)
    return model, transport


def _setup(tmp_path):
    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log, data_root=tmp_path / "data")
    ws = tmp_path / "ws"
    ws.mkdir()
    return log, stack, ws


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("usage", USAGES)
def test_real_adapters_and_response_preserve_usage_in_both_events(tmp_path, vendor, usage):
    model, transport = _response(vendor, usage)
    resp = model.complete([{"role": "user", "content": "offline"}], tool_manifest())
    assert isinstance(resp, ModelResponse)
    assert resp.usage == usage
    assert resp.to_payload()["usage"] == usage
    log = EventLog(tmp_path / "events.db")
    try:
        for kind in TYPES:
            payload = event_schema.minimal_payload(kind)
            payload["usage"] = resp.usage
            log.append("r", kind, payload)
        assert [(e.type, e.payload_version, e.payload["usage"]) for e in log.read_run("r")] == [
            (t, 2, usage) for t in TYPES
        ]
        assert [
            tuple(row)
            for row in log.conn.execute("SELECT type,version FROM event_versions ORDER BY seq")
        ] == [(t, 2) for t in TYPES]
        assert len(transport.calls) == 1
    finally:
        log.close()


@pytest.mark.parametrize("vendor", VENDORS)
def test_real_loop_persists_cache_and_replays_without_transport(tmp_path, vendor):
    usage = {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 9,
        "cache_creation_input_tokens": 3,
    }
    model, transport = _response(vendor, usage)
    log, stack, ws = _setup(tmp_path)
    try:
        task = stack.tasks.create_task("offline", [str(ws)])
        result = engine_for(stack, model, limits=Limits(3, 1000)).start(task.task_id)
        assert result.outcome == "noop", result.error
        assert result.turns == 1 and result.tokens == 18
        assert result.cost_total_micros == 0 and result.unknown_cost_calls == 1
        events = log.read_run(result.run_id)
        pair = [e for e in events if e.type in TYPES]
        assert [(e.type, e.payload_version) for e in pair] == [(t, 2) for t in TYPES]
        assert all(e.payload["usage"] == usage for e in pair)
        turn = stack.tasks.list_model_turns(result.run_id)[0]
        assert turn["usage"] == usage
        hashed = {k: v for k, v in turn.items() if k != "seq"}
        assert pair[1].payload["payload_hash"] == sha256_hex(canonical_bytes(hashed))
        cursor = stack.tasks.load_cursor(result.run_id)[0]
        assert cursor.budget_ledger["turns"] == 1 and cursor.budget_ledger["tokens"] == 18
        assert cursor.budget_ledger["cost_total_micros"] == 0
        assert cursor.budget_ledger["unknown_cost_calls"] == 1
        end = next(e.payload for e in events if e.type == "run.end")
        assert end["cost_total_micros"] is None and end["cost_unknown_calls"] == 1
        assert end["unbilled_model_intents"] == 0
        assert replay(log, result.run_id).ok
        before = [(e.seq, e.payload_hash, e.payload_version) for e in events]
        stack.tasks.rebuild_from_events()
        assert before == [
            (e.seq, e.payload_hash, e.payload_version) for e in log.read_run(result.run_id)
        ]
        assert len(transport.calls) == 1
        rid = result.run_id
    finally:
        log.close()
    reopened = EventLog(tmp_path / "events.db")
    try:
        assert replay(reopened, rid).ok
        assert all(e.payload["usage"] == usage for e in reopened.read_run(rid) if e.type in TYPES)
        assert len(transport.calls) == 1
    finally:
        reopened.close()


def test_independent_commit_model_turn_preserves_usage_and_idempotency(tmp_path):
    log, stack, ws = _setup(tmp_path)
    try:
        task = stack.tasks.create_task("offline", [str(ws)])
        run = stack.tasks.start_run(task.task_id)
        job, claim = stack.claims.enqueue_and_claim(run.run_id)
        turn = {
            "turn_id": "t1",
            "finish_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 9},
        }
        assert stack.tasks.commit_model_turn(run.run_id, turn, claim) == 1
        assert stack.tasks.commit_model_turn(run.run_id, turn, claim) == 1
        events = [e for e in log.read_run(run.run_id) if e.type == "run.model_turn"]
        assert len(events) == 1 and events[0].payload_version == 2
        assert events[0].payload["usage"] == turn["usage"]
        assert events[0].payload["payload_hash"] == sha256_hex(canonical_bytes(turn))
        assert stack.tasks.list_model_turns(run.run_id) == [{"seq": 1, **turn}]
        stack.claims.finish(claim, outcome="succeeded")
    finally:
        log.close()


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("cache", CACHE_KEYS)
@pytest.mark.parametrize("value", [None, 0, 3, 2.0])
def test_v2_cache_optional_nullable_nonnegative_json_integer(kind, cache, value):
    payload = event_schema.minimal_payload(kind)
    payload["usage"] = {"input_tokens": None, "output_tokens": 7, cache: value}
    event_schema.validate_payload(kind, payload, 2)
    assert event_schema.detect_version(kind, payload) == 2


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("cache", CACHE_KEYS)
@pytest.mark.parametrize("value", [-1, -1.0, True, "3", 1.5])
def test_v2_cache_rejects_illegal_values(kind, cache, value):
    payload = event_schema.minimal_payload(kind)
    payload["usage"] = {"input_tokens": 11, "output_tokens": 7, cache: value}
    with pytest.raises(event_schema.PayloadContractError, match=cache):
        event_schema.validate_payload(kind, payload, 2)


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 1},
        {"output_tokens": None},
        {"input_tokens": 1, "output_tokens": 2, "extra": 1},
    ],
)
def test_v2_usage_still_requires_base_keys_and_is_closed(kind, usage):
    payload = event_schema.minimal_payload(kind)
    payload["usage"] = usage
    with pytest.raises(event_schema.PayloadContractError):
        event_schema.validate_payload(kind, payload, 2)


def test_exact_historical_schemas_and_new_versions_only():
    assert event_schema.CURRENT_PAYLOAD_VERSION == 1
    for kind in event_schema.CURRENT_SCHEMAS:
        expected = 2 if kind in (*TYPES, "run.start", "run.end") else 1
        assert event_schema.current_version(kind) == expected
        assert event_schema.known_versions(kind) == frozenset(range(expected + 1))
    for kind in TYPES:
        for version in (0, 1):
            assert event_schema.schema_for(kind, version) == FIXED["schemas"][kind][str(version)]
        with pytest.raises(event_schema.UnknownPayloadVersion):
            event_schema.schema_for(kind, 3)
        payload = copy.deepcopy(FIXED["payloads"][kind])
        payload["usage"] = {"input_tokens": -1.0, "output_tokens": None}
        event_schema.validate_payload(kind, payload, 1)  # no base-field tightening
        event_schema.validate_payload(kind, payload, 2)
        payload["usage"]["arbitrary_old_field"] = True
        if kind == "run.model_turn":
            event_schema.validate_payload(kind, payload, 0)  # v0 was open object|null
        with pytest.raises(event_schema.PayloadContractError):
            event_schema.validate_payload(kind, payload, 1)
    assert (
        hashlib.sha256(canonical_bytes(event_schema.LEGACY_SCHEMAS)).hexdigest()
        == FIXED["legacy_all_sha256"]
    )
    unchanged = {k: v for k, v in event_schema.CURRENT_SCHEMAS.items() if k not in TYPES}
    assert (
        hashlib.sha256(canonical_bytes(unchanged)).hexdigest() == FIXED["unaffected_current_sha256"]
    )
    doc = json.loads((ROOT / "schemas/c3/event.json").read_text(encoding="utf-8"))
    assert doc["$defs"]["payload"] == event_schema.CURRENT_SCHEMAS
    assert doc["$defs"]["legacyPayload"] == event_schema.LEGACY_SCHEMAS
    for kind in TYPES:
        assert doc["$defs"]["historicalPayloadV1"][kind] == FIXED["schemas"][kind]["1"]


@pytest.mark.parametrize("fault", ["second_event", "version_row", "first_version_row"])
def test_real_loop_commit_failure_rolls_back_pair_turn_and_cursor(tmp_path, monkeypatch, fault):
    model, transport = _response(
        "openai",
        {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 9},
        tool={
            "call_id": "c1",
            "name": "create_report",
            "args": {"name": "never.md", "content": "never"},
        },
    )
    log, stack, ws = _setup(tmp_path)
    original = event_schema.detect_version
    reached = []

    def reject_turn(kind, payload, **kwargs):
        if kind == "run.model_turn":
            reached.append(kind)
            raise event_schema.PayloadContractError(kind, "$.usage", "injected second event")
        return original(kind, payload, **kwargs)

    try:
        if fault == "second_event":
            monkeypatch.setattr(event_schema, "detect_version", reject_turn)
        else:
            target = "model.result" if fault == "first_version_row" else "run.model_turn"
            log.conn.execute(
                "CREATE TEMP TRIGGER reject_usage_version BEFORE INSERT ON event_versions "
                f"WHEN NEW.type='{target}' BEGIN "
                "SELECT RAISE(ABORT,'injected version row'); END"
            )
        task = stack.tasks.create_task("offline", [str(ws)])
        result = engine_for(stack, model, limits=Limits(3, 1000)).start(task.task_id)
        assert result.state == "failed"
        assert result.model_attempts == 1 and result.turns == 1 and result.tokens == 18
        assert result.cost_total_micros == 0 and result.unknown_cost_calls == 1
        assert "injected" in result.error
        assert len(transport.calls) == 1
        events = log.read_run(result.run_id)
        assert not [e for e in events if e.type in (*TYPES, "dispatch.requested", "tool.intent")]
        assert stack.tasks.list_model_turns(result.run_id) == []
        cursor = stack.tasks.load_cursor(result.run_id)[0]
        assert cursor.budget_ledger["turns"] == 0 and cursor.budget_ledger["tokens"] == 0
        assert cursor.model_turn is None
        assert cursor.pending_proposals == []
        assert (
            log.conn.execute(
                "SELECT count(*) FROM event_versions WHERE type IN (?,?)", TYPES
            ).fetchone()[0]
            == 0
        )
        assert not (ws / "never.md").exists()
        end = next(e.payload for e in events if e.type == "run.end")
        assert end["unbilled_model_intents"] == 1 and end["cost_total_micros"] is None
        assert end["turns"] == 0 and end["tokens"] == 0 and end["cost_unknown_calls"] == 0
        assert end["budget_enforced"] is False
        settled = stack.budget.usage(f"run:{result.run_id}")
        assert settled["settled"]["tokens"] == 18 and settled["settled"]["requests"] == 1
        assert settled["unknown_reservations"] == 1
        if fault == "second_event":
            assert reached == ["run.model_turn"]
    finally:
        log.close()


@pytest.mark.parametrize("kind", TYPES)
def test_historical_payload_hash_and_version_roundtrip(tmp_path, kind):
    log = EventLog(tmp_path / "events.db")
    try:
        payload = FIXED["payloads"][kind]
        for version in (0, 1):
            # Explicit historical seeding is test-only; never repair a real event.
            seq = log.append("old", kind, payload, legacy_write=True)
            log.conn.execute("UPDATE event_versions SET version=? WHERE seq=?", (version, seq))
        events = log.read_run("old")
        assert [e.payload_version for e in events] == [0, 1]
        assert all(
            e.payload == payload and e.payload_hash == sha256_hex(canonical_bytes(payload))
            for e in events
        )
    finally:
        log.close()


@pytest.mark.parametrize("vendor", VENDORS)
def test_null_cache_normalizes_to_absence_without_inventing_zero(vendor):
    usage = {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }
    model, transport = _response(vendor, usage)
    response = model.complete([{"role": "user", "content": "offline"}], tool_manifest())
    assert response.usage == {"input_tokens": 11, "output_tokens": 7}
    assert len(transport.calls) == 1


@pytest.mark.parametrize("vendor", VENDORS)
def test_pause_resume_keeps_paid_turn_before_dispatch_and_never_reissues_it(
    tmp_path, monkeypatch, vendor
):
    usage = {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 9,
        "cache_creation_input_tokens": 3,
    }
    log, stack, ws = _setup(tmp_path)
    rid = "run_usage_pause"
    tool = {
        "call_id": "c1",
        "name": "create_report",
        "args": {"name": "once.md", "content": "once"},
    }
    payloads = [
        _payload(vendor, usage=_raw_usage(vendor, usage), tool=tool),
        _payload(vendor, usage=_raw_usage(vendor, usage)),
    ]

    class PausingTransport(FakeTransport):
        def post(self, url, *, body, headers, timeout):
            self._data = payloads[len(self.calls)]
            value = super().post(url, body=body, headers=headers, timeout=timeout)
            if len(self.calls) == 1:
                stack.tasks.request_pause(rid)
            return value

    transport = PausingTransport()
    model = (
        OpenAICompatibleModel("gpt-4o-mini", api_key="offline-test", transport=transport)
        if vendor == "openai"
        else AnthropicMessagesModel(
            "claude-sonnet-4-5", api_key="offline-test", transport=transport
        )
    )
    observed = []
    real_dispatch = stack.dispatcher.dispatch

    def dispatch_after_durable_turn(request_id, claim):
        events = [e for e in log.read_run(rid) if e.type in TYPES]
        assert [(e.type, e.payload_version) for e in events] == [(t, 2) for t in TYPES]
        assert all(e.payload["usage"] == usage for e in events)
        cursor = stack.tasks.load_cursor(rid)[0]
        assert cursor.model_turn["usage"] == usage
        assert stack.tasks.list_model_turns(rid)[0]["usage"] == usage
        observed.append(request_id)
        return real_dispatch(request_id, claim)

    monkeypatch.setattr(stack.dispatcher, "dispatch", dispatch_after_durable_turn)
    try:
        task = stack.tasks.create_task("offline", [str(ws)])
        engine = engine_for(stack, model, limits=Limits(3, 1000))
        paused = engine.start(task.task_id, run_id=rid)
        assert paused.state == "paused", paused.error
        assert len(transport.calls) == 1 and observed == []
        assert not (ws / "once.md").exists()
        cursor = stack.tasks.load_cursor(rid)[0]
        assert cursor.model_turn["usage"] == usage
        assert cursor.budget_ledger["tokens"] == 18
        assert cursor.pending_proposals[0]["call_id"] == "c1"
        ledger_before = stack.budget.usage(f"run:{rid}")
        assert ledger_before["settled"]["tokens"] == 18
        assert ledger_before["settled"]["requests"] == 1
        assert ledger_before["unknown_reservations"] == 1
        assert ledger_before["reserved"]["micros"] > 0
        reservation_id = log.conn.execute(
            "SELECT reservation_id FROM budget_reservations WHERE run_id=?", (rid,)
        ).fetchone()[0]
        duplicate = stack.budget.settle(
            reservation_id,
            usage={"tokens": 18, "requests": 1},
            known_dimensions={"tokens", "requests"},
        )
        assert duplicate.duplicate and duplicate.state == "partial"
        assert stack.budget.usage(f"run:{rid}") == ledger_before
        done = engine.resume_run(rid)
        assert done.state == "succeeded", done.error
        assert len(transport.calls) == 2 and len(observed) == 1
        assert (ws / "once.md").read_text(encoding="utf-8") == "once"
        assert done.turns == 2 and done.tokens == 36 and done.unknown_cost_calls == 2
        events = log.read_run(rid)
        assert len([e for e in events if e.type == "receipt"]) == 1
        assert len([e for e in events if e.type == "model.result"]) == 2
        assert len([e for e in events if e.type == "run.model_turn"]) == 2
        assert replay(log, rid).ok
        assert stack.budget.usage(f"run:{rid}")["settled"]["tokens"] == 36
        assert stack.budget.usage(f"run:{rid}")["unknown_reservations"] == 2
        assert len(transport.calls) == 2
    finally:
        log.close()


@pytest.mark.parametrize("kind", TYPES)
def test_cache_schema_nodes_are_not_shared_with_history_or_each_other(kind):
    new = event_schema.schema_for(kind, 2)["properties"]["usage"]
    old = event_schema.schema_for(kind, 1)["properties"]["usage"]
    assert new is not old and new["properties"] is not old["properties"]
    other = TYPES[1] if kind == TYPES[0] else TYPES[0]
    assert (
        new["properties"]
        is not event_schema.schema_for(other, 2)["properties"]["usage"]["properties"]
    )
    assert set(old["properties"]) == {"input_tokens", "output_tokens"}
    assert set(new["properties"]) == {"input_tokens", "output_tokens", *CACHE_KEYS}


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("fault", ["future", "wrong_type", "missing_table"])
def test_unknown_usage_event_version_or_side_table_is_not_guessed(tmp_path, kind, fault):
    log = EventLog(tmp_path / "events.db")
    try:
        seq = log.append("new", kind, event_schema.minimal_payload(kind))
        if fault == "future":
            log.conn.execute("UPDATE event_versions SET version=3 WHERE seq=?", (seq,))
            expected = event_schema.UnknownPayloadVersion
        elif fault == "wrong_type":
            from tianshu.data_state import AdmissionRefused

            log.conn.execute("UPDATE event_versions SET type='tool.intent' WHERE seq=?", (seq,))
            # Existing live reads do not re-admit the whole side table. The real
            # reopen admission is the boundary that rejects a mismatched type.
            log.close()
            with pytest.raises(AdmissionRefused) as caught:
                EventLog(tmp_path / "events.db")
            assert caught.value.code == "incompatible"
            return
        else:
            log.conn.execute("DROP TABLE event_versions")
            expected = event_schema.PayloadVersionUnavailable
        with pytest.raises(expected):
            log.read_run("new")
    finally:
        log.close()
