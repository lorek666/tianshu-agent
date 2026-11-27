"""真实游标编解码、存储和续跑，模型请求使用离线脚本。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import closing, contextmanager
from threading import Event

import pytest
from support import CHILD_TEXT, engine_for, stack_for

from tianshu._loop_bookkeeping import _Progress
from tianshu.cli import _run_status
from tianshu.control import ControlPlane
from tianshu.daemon import _Daemon
from tianshu.errors import TianshuError
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.jobs import JobStore
from tianshu.leases import ProcessIdentity
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.run_policy import PURE_ONLY
from tianshu.tasks import RUN_CURSOR_SAVED, RUN_QUEUED, RunCursor, TaskStore
from tianshu.worker import Worker

_KNOWNNESS_CASES = [
    pytest.param({}, 0, False, id="legacy-empty"),
    pytest.param({"turns": 2}, 2, False, id="legacy-turns-lower-bound"),
    pytest.param(
        {"turns": 2, "model_attempts_known": True},
        2, False, id="missing-counter-true-is-not-precision",
    ),
    pytest.param(
        {"model_attempts_known": True},
        0, False, id="missing-counter-true-without-turns",
    ),
    pytest.param(
        {"turns": 2, "model_attempts_known": False},
        2, False, id="missing-counter-false",
    ),
    pytest.param(
        {"turns": 2, "model_attempts": 5},
        5, True, id="historical-counter-without-flag",
    ),
    pytest.param(
        {"turns": 2, "model_attempts": 5, "model_attempts_known": True},
        5, True, id="explicit-true",
    ),
    pytest.param(
        {"turns": 2, "model_attempts": 5, "model_attempts_known": False},
        5, False, id="explicit-false-nonzero",
    ),
    pytest.param(
        {"model_attempts": 0, "model_attempts_known": False},
        0, False, id="explicit-false-zero",
    ),
]
_BAD_FLAGS = [None, 0, 1, -1, 0.0, 1.0, "", "false", "true", [], {}, [False]]


def _cursor_round_trip(cursor):
    return RunCursor.of(json.loads(canonical_bytes(cursor.to_dict())))


@pytest.mark.parametrize("ledger,attempts,known", _KNOWNNESS_CASES)
def test_progress_cursor_codec_preserves_attempt_knownness(ledger, attempts, known):
    cursor = _cursor_round_trip(RunCursor(budget_ledger=ledger))
    original = canonical_bytes(cursor.to_dict())
    progress = _Progress.of(cursor)
    assert canonical_bytes(cursor.to_dict()) == original, "解码不得改写原游标"
    for _ in range(3):
        assert progress.model_attempts == attempts
        assert progress.model_attempts_known is known
        encoded = _cursor_round_trip(progress.to_cursor())
        assert encoded.budget_ledger["model_attempts"] == attempts
        assert encoded.budget_ledger["model_attempts_known"] is known
        progress = _Progress.of(encoded)


def test_additional_count_does_not_make_a_legacy_lower_bound_exact():
    progress = _Progress.of(RunCursor(budget_ledger={"turns": 2}))
    for expected in (3, 4, 5):
        # 仅验证计数累加/编解码，不声称实际发送了模型请求。
        progress.model_attempts += 1
        progress = _Progress.of(_cursor_round_trip(progress.to_cursor()))
        assert progress.model_attempts == expected
        assert progress.model_attempts_known is False


@pytest.mark.parametrize("flag", _BAD_FLAGS)
@pytest.mark.parametrize("has_counter", [False, True], ids=["no-counter", "counter"])
def test_progress_decoder_rejects_non_bool_flag_even_without_counter(flag, has_counter):
    ledger = {"turns": 2, "model_attempts_known": flag}
    if has_counter:
        ledger["model_attempts"] = 5
    cursor = _cursor_round_trip(RunCursor(budget_ledger=ledger))
    before = canonical_bytes(cursor.to_dict())
    with pytest.raises(TianshuError, match="model_attempts_known"):
        _Progress.of(cursor)
    assert canonical_bytes(cursor.to_dict()) == before


@pytest.mark.parametrize("ledger,attempts,known", _KNOWNNESS_CASES)
def test_taskstore_cursor_repeated_reopen_decode_save_keeps_knownness(
    tmp_path, ledger, attempts, known
):
    """真实保存→关库→重开→读取→解码→保存；不是 recover_run/完整 resume 验收。"""
    db = tmp_path / "events.db"
    identity = ProcessIdentity.mint()
    with closing(EventLog(db)) as log:
        jobs = JobStore(log, identity=identity, clock=lambda: 1_000)
        tasks = TaskStore(log, claims=jobs)
        task = tasks.create_task("attempt knownness cursor regression")
        run = tasks.start_run(task.task_id, execution_policy=PURE_ONLY)
        _, claim = jobs.enqueue_and_claim(run.run_id)
        tasks.save_cursor(run.run_id, RunCursor(budget_ledger=ledger), claim)

    for cycle in range(3):
        with closing(EventLog(db)) as log:
            jobs = JobStore(log, identity=identity, clock=lambda: 1_000)
            tasks = TaskStore(log, claims=jobs)
            stored = tasks.load_cursor(run.run_id)
            assert stored is not None
            cursor, meta = stored
            assert meta["claim_epoch"] == claim.claim_epoch
            assert meta["task_id"] == task.task_id
            progress = _Progress.of(cursor)
            assert progress.model_attempts == attempts
            assert progress.model_attempts_known is known
            tasks.save_cursor(run.run_id, progress.to_cursor(), claim)
            saved = tasks.load_cursor(run.run_id)
            assert saved is not None
            persisted, saved_meta = saved
            assert saved_meta["claim_epoch"] == claim.claim_epoch
            assert persisted.budget_ledger["model_attempts"] == attempts
            assert persisted.budget_ledger["model_attempts_known"] is known
            again = _Progress.of(persisted)
            assert again.model_attempts == attempts
            assert again.model_attempts_known is known
            events = [e for e in log.read_run(run.run_id) if e.type == RUN_CURSOR_SAVED]
            assert len(events) == cycle + 2
            assert events[-1].payload["claim_epoch"] == claim.claim_epoch
            assert events[-1].payload["payload_hash"] == sha256_hex(
                canonical_bytes(persisted.to_dict())
            )


@pytest.mark.parametrize("flag", _BAD_FLAGS)
@pytest.mark.parametrize("has_counter", [False, True], ids=["no-counter", "counter"])
def test_taskstore_saved_invalid_flag_is_not_laundered_by_progress_decode(
    tmp_path, flag, has_counter
):
    """存储层保留原 JSON；解码边界拒绝畸形标记，不偷偷修正或回写。"""
    ledger = {"turns": 2, "model_attempts_known": flag}
    if has_counter:
        ledger["model_attempts"] = 5
    db = tmp_path / "events.db"
    with closing(EventLog(db)) as log:
        jobs = JobStore(log, clock=lambda: 1_000)
        tasks = TaskStore(log, claims=jobs)
        task = tasks.create_task("malformed attempt knownness cursor")
        run = tasks.start_run(task.task_id, execution_policy=PURE_ONLY)
        _, claim = jobs.enqueue_and_claim(run.run_id)
        tasks.save_cursor(run.run_id, RunCursor(budget_ledger=ledger), claim)

    with closing(EventLog(db)) as log:
        tasks = TaskStore(log)
        stored = tasks.load_cursor(run.run_id)
        assert stored is not None
        cursor, _ = stored
        assert canonical_bytes(cursor.budget_ledger) == canonical_bytes(ledger)
        before = canonical_bytes(cursor.to_dict())
        events_before = len(log.read_run(run.run_id))
        for _ in range(2):
            with pytest.raises(TianshuError, match="model_attempts_known"):
                _Progress.of(cursor)
            assert canonical_bytes(cursor.to_dict()) == before
            loaded = tasks.load_cursor(run.run_id)
            assert loaded is not None
            assert canonical_bytes(loaded[0].to_dict()) == before
            assert len(log.read_run(run.run_id)) == events_before


def _enqueue_with_cursor(stack, ledger, *, paused=False):
    task = stack.tasks.create_task("resume saved attempt count")
    run = stack.tasks.start_run(task.task_id, execution_policy=PURE_ONLY)
    stack.tasks.transition(run.run_id, RUN_QUEUED, reason="enqueued")
    _, claim = stack.claims.enqueue_and_claim(run.run_id)
    stack.tasks.save_cursor(run.run_id, RunCursor(
        messages=[{"role": "user", "content": task.goal}], budget_ledger=ledger,
    ), claim)
    if paused:
        stack.claims.park(claim, reason="fixture: explicit desktop resume")
        assert stack.tasks.request_pause(run.run_id).state == "paused"
    else:
        stack.claims.requeue(claim)
    return run.run_id


def test_resume_adds_one_attempt_but_keeps_a_lower_bound_unknown(tmp_path):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path)
        run_id = _enqueue_with_cursor(stack, {"model_attempts": 2, "model_attempts_known": False})
        model = ScriptedModel([ModelResponse("end_turn", content="finished")])
        result = engine_for(stack, model).resume_run(run_id)
        assert result.state == "succeeded"
        assert [e.type for e in log.read_run(run_id)].count("model.intent") == 1
        assert result.model_attempts == 3
        assert result.model_attempts_known is False
        cursor, _ = stack.tasks.load_cursor(run_id)
        assert cursor.budget_ledger["model_attempts_known"] is False


_SAVE_AND_PARK_CURSOR = """
import json
import os
import sys
from contextlib import closing
from pathlib import Path

from tianshu.events import EventLog
from tianshu.jobs import JobStore
from tianshu.tasks import TaskStore

root, run_id, flag = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
with closing(EventLog(root / "events.db")) as log:
    claims = JobStore(log)
    tasks = TaskStore(log, data_root=root, claims=claims)
    claim = claims.claim(claims.job_for_run(run_id).job_id)
    cursor, _ = tasks.load_cursor(run_id)
    cursor.budget_ledger.update(model_attempts=25, model_attempts_known=flag)
    tasks.save_cursor(run_id, cursor, claim)
    claims.park(claim, reason="fixture: hand back the saved cursor")
    print(json.dumps({"pid": os.getpid(), "epoch": claim.claim_epoch}))
"""


@pytest.mark.parametrize("flag", [False, 0], ids=["updated-lower-bound", "malformed-flag"])
def test_manual_resume_uses_cursor_saved_before_claim_transaction(tmp_path, monkeypatch, flag):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path)
        run_id = _enqueue_with_cursor(
            stack, {"model_attempts": 7, "model_attempts_known": False}, paused=True,
        )
        before_transaction = log.transaction
        handed_back = []

        @contextmanager
        def transaction_after_handoff():
            if not handed_back:
                # Let another actual owner commit and park immediately before the
                # first resume write transaction. No wall-clock race is required.
                result = subprocess.run(
                    [sys.executable, "-c", _SAVE_AND_PARK_CURSOR,
                     str(tmp_path), run_id, json.dumps(flag)],
                    capture_output=True, timeout=30, **CHILD_TEXT,
                )
                assert result.returncode == 0, result.stderr
                observed = json.loads(result.stdout)
                assert observed["pid"] != os.getpid() and observed["epoch"] == 2
                handed_back.append((log.read_run(run_id), stack.claims.job_for_run(run_id)))
            with before_transaction() as tx:
                yield tx

        monkeypatch.setattr(log, "transaction", transaction_after_handoff)
        model = ScriptedModel([ModelResponse("end_turn", content="finished")])
        engine = engine_for(stack, model)
        if type(flag) is bool:
            result = engine.resume_run(run_id)
            assert result.state == "succeeded"
            assert result.model_attempts == 26 and result.model_attempts_known is False
            assert [e.type for e in log.read_run(run_id)].count("model.intent") == 1
        else:
            with pytest.raises(TianshuError, match="model_attempts_known"):
                engine.resume_run(run_id)
            events, job = handed_back[0]
            assert log.read_run(run_id) == events
            assert stack.claims.job_for_run(run_id) == job
            assert job.state == "waiting" and job.claim_epoch == 2
        cursor, _ = stack.tasks.load_cursor(run_id)
        expected = 26 if type(flag) is bool else 25
        assert cursor.budget_ledger["model_attempts"] == expected
        persisted = cursor.budget_ledger["model_attempts_known"]
        assert persisted == flag and type(persisted) is type(flag)


@pytest.mark.parametrize("entry", ["direct", "worker"])
def test_invalid_flag_never_advances_run_or_sends_request(tmp_path, entry):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path)
        run_id = _enqueue_with_cursor(stack, {"model_attempts": 2, "model_attempts_known": "false"})
        model = ScriptedModel([ModelResponse("end_turn", content="must not be sent")])
        if entry == "direct":
            before = log.read_run(run_id)
            for _ in range(2):
                with pytest.raises(TianshuError, match="model_attempts_known"):
                    engine_for(stack, model).resume_run(run_id)
            assert log.read_run(run_id) == before
            assert stack.claims.job_for_run(run_id).state == "pending"
        else:
            worker = Worker(stack, model=model, heartbeat_interval_ms=0)
            assert worker.run_once()
            assert any("model_attempts_known" in error for error in worker.errors)
            assert stack.claims.job_for_run(run_id).state == "waiting"
            assert not worker.run_once()
        assert stack.tasks.get_run(run_id).state == RUN_QUEUED
        assert not any(e.type == "model.intent" for e in log.read_run(run_id))
        cursor, _ = stack.tasks.load_cursor(run_id)
        assert cursor.budget_ledger["model_attempts_known"] == "false"


@pytest.mark.parametrize("flag", [0, "false", None])
def test_desktop_resume_rejects_invalid_flag_before_claim(tmp_path, capsys, flag):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path)
        run_id = _enqueue_with_cursor(
            stack, {"model_attempts": 2, "model_attempts_known": flag}, paused=True,
        )
        model = ScriptedModel([ModelResponse("end_turn", content="must not be sent")])
        driver = _Daemon(
            tmp_path, model=model, limits=None, driver_interval_s=0.1,
            stop_event=Event(), on_ready=None,
        )
        driver.driver_log, driver.driver_stack = log, stack
        driver.plane = ControlPlane(stack, model=model, resume_requests=driver.resume_requests)
        before = log.read_run(run_id)
        job = stack.claims.job_for_run(run_id)
        saved = stack.tasks.load_cursor(run_id)
        for _ in range(2):
            driver.plane.handle("run.resume", {"run_id": run_id})
            assert driver.resume_requests.get_nowait() == run_id
            try:
                driver._resume_one(engine_for(stack, model), run_id)
            finally:
                driver._close_resume_channel()
            assert "model_attempts_known 必须是布尔值" in capsys.readouterr().err
            assert log.read_run(run_id) == before
            assert stack.claims.job_for_run(run_id) == job
            assert stack.tasks.load_cursor(run_id) == saved


def test_status_does_not_report_malformed_knownness_as_precision(tmp_path, capsys):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path)
        run_id = _enqueue_with_cursor(stack, {"model_attempts": 2, "model_attempts_known": 0})
        with pytest.raises(TianshuError, match="model_attempts_known"):
            _run_status(stack, argparse.Namespace(run_id=run_id, json=True))
        assert capsys.readouterr().out == ""
