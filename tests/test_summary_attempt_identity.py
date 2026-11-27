"""摘要决定与写入边界。编译和 SQLite 为真实实现，模型为离线脚本。"""

import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
from support import engine_for
from test_summary_loop import ConstraintModel

from tianshu._loop_bookkeeping import _Progress
from tianshu._loop_summary import summary_blocks
from tianshu.budget import PARTIAL, PENDING, RELEASED, UNKNOWN, BudgetLedger
from tianshu.context import RUN_START_SEQ, ContextBudget, compile_context
from tianshu.events import EventLog
from tianshu.jobs import JobClaim, JobLost
from tianshu.loop import AgentLoop, Limits, Stack, build_stack
from tianshu.model import ModelResponse
from tianshu.run_policy import PURE_ONLY
from tianshu.summaries import FAILED, READY, SummaryStore, build_material
from tianshu.summary_telemetry import read_calls
from tianshu.tasks import Task

BUDGET = ContextBudget(max_bytes=3000)
SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"


class CountedSummary(ConstraintModel):
    def __init__(self):
        super().__init__(rounds=0)
        self.observe = None

    def complete(self, messages, tools):
        if self.observe is not None:
            self.observe()
        response = super().complete(messages, tools)
        if self.summary_calls == 1:
            return ModelResponse("end_turn", content=" ", usage={"input_tokens": 7})
        return response


@dataclass
class Rig:
    root: Path
    log: EventLog
    stack: Stack
    model: ConstraintModel
    limits: Limits
    engine: AgentLoop
    task: Task
    run_id: str
    claim: JobClaim

    @property
    def store(self):
        return SummaryStore(self.log, create=False)

    @property
    def account(self):
        return f"run:{self.run_id}"


@pytest.fixture
def make_rig(tmp_path, request):
    def make(*, model=None, **limits_args):
        log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
        request.addfinalizer(log.close)
        stack = build_stack(log, data_root=tmp_path, clock=lambda: 1_000)
        model = model or ConstraintModel(rounds=0)
        limits_args.setdefault("context_budget_bytes", BUDGET.max_bytes)
        limits = Limits(**limits_args)
        engine = engine_for(stack, model, limits=limits)
        task = stack.tasks.create_task("摘要尝试隔离")
        run = stack.tasks.start_run(task.task_id, execution_policy=PURE_ONLY)
        _, claim = stack.claims.enqueue_and_claim(run.run_id, ttl_ms=30)
        assert engine._ensure_admitted(run.run_id, "summary-test-admission") is None
        assert engine._run_tool_manifest(run.run_id)
        return Rig(tmp_path, log, stack, model, limits, engine, task, run.run_id, claim)

    return make


def _material(rig, *, seq_start=1):
    messages = [
        {"role": "system", "content": "摘要夹具"},
        {"role": "user", "content": "保留早期材料"},
    ]
    seqs = [RUN_START_SEQ, RUN_START_SEQ]
    for seq in range(seq_start, seq_start + 12, 2):
        call_id = f"read-{seq}"
        messages.extend([
            {"role": "assistant", "content": "x" * 400, "tool_calls": [
                {"id": call_id, "type": "function", "function": {
                    "name": "read_text", "arguments": "{}",
                }},
            ]},
            {"role": "tool", "tool_call_id": call_id, "name": "read_text", "content": "y" * 400},
        ])
        seqs.extend([seq, seq + 1])
    progress = _Progress(messages=messages, message_seqs=seqs)
    view = compile_context(messages, seqs, BUDGET)
    assert view.dropped_seqs
    material = build_material(
        summary_blocks(messages, seqs, view.dropped_seqs),
        scope=f"task:{rig.task.task_id}", task_id=rig.task.task_id, run_id=rig.run_id,
    )
    return progress, view, material


def _legacy(rig, material, *, state=None, fail=False):
    old = rig.store.begin(
        material, model=rig.engine._summary_model_id(),
        attempt_id=f"summary#{material.material_id}#1",
    )
    ledger = rig.stack.budget
    rig.engine._ensure_budget_account(rig.run_id)
    if state is not None:
        reservation = rig.engine._reserve_summary(material, old.attempt_id, rig.run_id)
        if state == PARTIAL:
            ledger.settle(reservation.reservation_id, usage={"requests": 1},
                          known_dimensions={"requests"})
        elif state == UNKNOWN:
            ledger.settle(reservation.reservation_id, usage={}, known=False)
    if fail:
        old = rig.store.fail(old, "历史失败", input_tokens=13)
    return old


def _write_explicit_version(rig, progress, material):
    """Exercise the writer for a requested new version, separate from auto-generation."""
    with rig.log.transaction():
        attempt = rig.engine._prepare_summary(material)
    if attempt is None:
        return None
    return rig.engine._generate_summary(progress, attempt, rig.claim)


def test_explicit_new_writes_bind_distinct_versions_to_ledger_and_telemetry(make_rig):
    model = CountedSummary()
    rig = make_rig(model=model, summary_mode="model")
    progress, _, material = _material(rig)
    ledger = rig.stack.budget
    old = _legacy(rig, material, state=PARTIAL, fail=True)
    old_entries = ledger.reconcile(rig.account)
    seen = []

    def observe():
        with closing(EventLog(rig.root / "events.db", blob_dir=rig.root / "blobs")) as reader:
            pending = SummaryStore(reader, create=False).latest(material.material_id)
            reservation = BudgetLedger(reader).reservation_for(rig.account, pending.attempt_id)
            seen.append((pending, reservation, read_calls(reader)))

    model.observe = observe
    assert _write_explicit_version(rig, progress, material) is None
    ready = _write_explicit_version(rig, progress, material)
    records = rig.store.versions(material.material_id)
    assert [record.status for record in records] == [FAILED, FAILED, READY]
    assert records[0] == old
    assert ready == records[-1]
    assert len({record.attempt_id for record in records}) == 3
    assert model.summary_calls == progress.model_attempts == len(seen) == 2
    assert progress.tokens == 147 and progress.unknown_usage_calls == 1
    final_calls = {entry["request"]["summary_id"]: entry for entry in read_calls(rig.log)}
    rows = {row["attempt_id"]: row for row in ledger.reconcile(rig.account)}
    assert rows[old.attempt_id] == old_entries[0]
    assert rows[records[1].attempt_id]["usage"] == {"requests": 1}
    assert rows[records[2].attempt_id]["usage"] == {"requests": 1, "tokens": 140}
    assert ledger.usage(rig.account)["settled"]["requests"] == 3
    assert ledger.usage(rig.account)["reserved"]["tokens"] > 0
    for record, (pending, reservation, visible_calls) in zip(records[1:], seen, strict=True):
        assert pending.status == PENDING and pending.summary_id == record.summary_id
        assert reservation.state == PENDING
        request = next(entry for entry in visible_calls
                       if entry["request"]["summary_id"] == record.summary_id)
        row = rows[record.attempt_id]
        assert request["response"] is None
        assert row["reservation_id"] == reservation.reservation_id
        assert row["attempt_id"] == row["call_id"] == request["request"]["attempt_id"]
        assert row["account_id"] == rig.account
        assert row["run_id"] == request["request"]["run_id"] == rig.run_id
        assert "micros" not in row["usage"]
        assert final_calls[record.summary_id]["response"]["cost"]["cost"] is None


@pytest.mark.parametrize("old_state", [PARTIAL, UNKNOWN])
@pytest.mark.parametrize("gate", ["requests", "cancelled"])
def test_new_write_cannot_reuse_legacy_budget_admission(make_rig, old_state, gate):
    rig = make_rig(
        summary_mode="model", budget_limits={"requests": 1 if gate == "requests" else 2},
    )
    progress, _, material = _material(rig)
    old = _legacy(rig, material, state=old_state, fail=True)
    ledger = rig.stack.budget
    if gate == "cancelled":
        assert ledger.cancel(rig.account) == 1
    before = ledger.reconcile(rig.account)
    assert _write_explicit_version(rig, progress, material) is None
    assert rig.model.summary_calls == progress.model_attempts == 0
    records = rig.store.versions(material.material_id)
    assert [record.status for record in records] == [FAILED, FAILED]
    assert records[0] == old and records[1].attempt_id != old.attempt_id
    assert ledger.reservation_for(rig.account, records[1].attempt_id) is None
    assert ledger.reconcile(rig.account) == before
    assert read_calls(rig.log) == []


@pytest.mark.parametrize("state", [None, PENDING, PARTIAL, UNKNOWN])
@pytest.mark.parametrize("cap", [1, 3])
def test_same_material_stays_quiet_after_pending_is_closed(make_rig, state, cap):
    rig = make_rig(summary_mode="model", summary_max_calls=cap)
    progress, view, material = _material(rig)
    old = _legacy(rig, material, state=state)
    before = rig.stack.budget.reconcile(rig.account)
    for _ in range(3):
        with closing(EventLog(rig.root / "events.db", blob_dir=rig.root / "blobs")) as reopened:
            stack = build_stack(
                reopened, data_root=rig.root, clock=lambda: 1_000,
                identity=rig.stack.claims.identity,
            )
            engine = engine_for(stack, rig.model, limits=rig.limits)
            assert engine._maybe_summarize(
                rig.run_id, rig.claim, rig.task, progress, view, BUDGET,
            ) is view
            records = SummaryStore(reopened, create=False).versions(material.material_id)
            assert [record.status for record in records] == [FAILED]
            assert records[0].summary_id == old.summary_id
            assert records[0].attempt_id == old.attempt_id
            assert records[0].input_tokens is records[0].output_tokens is records[0].micros is None
            assert stack.budget.reconcile(rig.account) == before
            assert read_calls(reopened) == []
    assert rig.model.summary_calls == progress.model_attempts == 0


def test_known_failed_material_falls_back_and_distinct_material_can_be_reused(make_rig):
    rig = make_rig(summary_mode="model", summary_max_calls=2)
    progress, view, material = _material(rig)
    old = _legacy(rig, material, fail=True)
    assert rig.engine._maybe_summarize(
        rig.run_id, rig.claim, rig.task, progress, view, BUDGET,
    ) is view
    assert rig.store.versions(material.material_id) == [old]
    assert rig.model.summary_calls == progress.model_attempts == 0

    progress, view, material = _material(rig, seq_start=100)
    compiled = rig.engine._maybe_summarize(rig.run_id, rig.claim, rig.task, progress, view, BUDGET)
    ready = rig.store.latest(material.material_id)
    assert compiled.summaries[0].summary_id == ready.summary_id
    reused = rig.engine._maybe_summarize(rig.run_id, rig.claim, rig.task, progress, view, BUDGET)
    assert reused.digest == compiled.digest
    assert rig.store.count_for_run(rig.run_id) == 2
    assert rig.model.summary_calls == progress.model_attempts == 1


def test_default_off_neither_begins_nor_reserves(make_rig):
    rig = make_rig()
    progress, view, _ = _material(rig)
    assert rig.engine._maybe_summarize(
        rig.run_id, rig.claim, rig.task, progress, view, BUDGET,
    ) is view
    assert rig.store.count_for_run(rig.run_id) == 0
    assert rig.stack.budget.account(rig.account) is None
    assert read_calls(rig.log) == []
    assert rig.model.summary_calls == progress.model_attempts == 0


_OTHER_WRITER = """
import json
import os
import sys
from contextlib import closing
from pathlib import Path

from tianshu.events import EventLog
from tianshu.jobs import JobStore
from tianshu.summaries import MaterialSource, SummaryMaterial, SummaryStore

request = json.loads(sys.argv[1])
root = Path(request["root"])
with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
    if request["action"] == "claim":
        store = JobStore(log, clock=lambda: 1_031)
        claim = store.claim(request["job_id"], ttl_ms=30)
        print(json.dumps({"pid": os.getpid(), "epoch": claim.claim_epoch}))
    else:
        source = request["material"]
        source["sources"] = tuple(MaterialSource(**item) for item in source["sources"])
        material = SummaryMaterial(**source)
        store = SummaryStore(log, create=False)
        pending = store.begin(material, model="scripted", attempt_id="other-writer")
        if request["action"] == "ready":
            record = store.complete(pending, "已保存的摘要")
        else:
            record = store.fail(pending, "已有结果未知的尝试")
        print(json.dumps({"pid": os.getpid(), "summary_id": record.summary_id}))
"""


def _script_env(root):
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((
            str(SOURCE_DIR), str(SOURCE_DIR.parent / "tests" / "_startup"),
        )),
        "TIANSHU_CREDENTIAL_BACKEND": "env",
        "TIANSHU_AUTHORITY_ROOT": os.environ["TIANSHU_AUTHORITY_ROOT"],
        "TIANSHU_DATA_ROOT": str(root / "data"),
        "TIANSHU_TEST_LOCK_ROOT": os.environ["TIANSHU_TEST_LOCK_ROOT"],
        "PYTEST_TIANSHU_REGISTRY_HOME": os.environ["PYTEST_TIANSHU_REGISTRY_HOME"],
    }


def _run_other_writer(rig, payload):
    process = subprocess.run(
        [sys.executable, "-P", "-c", _OTHER_WRITER, json.dumps(payload)],
        capture_output=True, encoding="utf-8", timeout=30, check=True,
        env=_script_env(rig.root), cwd=SOURCE_DIR.parent,
    )
    result = json.loads(process.stdout)
    assert result["pid"] != os.getpid()
    return result


def _write_before_transaction(rig, monkeypatch, action, material=None):
    before = rig.log.transaction
    observed = []

    @contextmanager
    def at_entry():
        if not observed:
            payload = {"root": str(rig.root), "job_id": rig.claim.job_id, "action": action}
            if material is not None:
                payload["material"] = asdict(material)
            observed.append(_run_other_writer(rig, payload))
        with before() as transaction:
            yield transaction

    monkeypatch.setattr(rig.log, "transaction", at_entry)
    return observed


@pytest.mark.parametrize("action", ["ready", "failed", "cap"])
def test_decision_observes_versions_committed_before_write_lock(make_rig, monkeypatch, action):
    rig = make_rig(summary_mode="model", summary_max_calls=1 if action == "cap" else 3)
    progress, _, current = _material(rig)
    competing = _material(rig, seq_start=100)[2] if action == "cap" else current
    observed = _write_before_transaction(rig, monkeypatch, action, competing)
    main_views = []

    def stop_after_summary(run_id, progress, request_id, view, budget):
        main_views.append(view)
        return None, "摘要阶段结束"

    monkeypatch.setattr(rig.engine, "_complete_with_retry", stop_after_summary)
    rig.engine._model_step_frozen(rig.run_id, rig.claim, progress, rig.task, None, None)
    assert rig.model.summary_calls == progress.model_attempts == 0
    assert rig.store.count_for_run(rig.run_id) == 1
    assert rig.store.latest(competing.material_id).summary_id == observed[0]["summary_id"]
    if action == "ready":
        assert main_views[0].summaries[0].summary_id == observed[0]["summary_id"]
    else:
        assert main_views[0].summaries == ()
    assert read_calls(rig.log) == []


@pytest.mark.parametrize("previous_pending", [False, True])
def test_model_step_checks_live_job_owner_before_summary_writes(
    make_rig, monkeypatch, previous_pending,
):
    rig = make_rig(summary_mode="model")
    progress, _, material = _material(rig)
    previous = _legacy(rig, material, state=PENDING) if previous_pending else None
    before = rig.stack.budget.reconcile(rig.account)
    observed = _write_before_transaction(rig, monkeypatch, "claim")
    with pytest.raises(JobLost):
        rig.engine._model_step_frozen(rig.run_id, rig.claim, progress, rig.task, None, None)
    assert observed[0]["epoch"] == rig.claim.claim_epoch + 1
    assert rig.model.calls == progress.model_attempts == 0
    assert rig.store.latest(material.material_id) == previous
    assert rig.stack.budget.reconcile(rig.account) == before
    assert read_calls(rig.log) == []
    assert not rig.log.read_run(rig.run_id, event_types=("model.intent",))


@pytest.mark.parametrize("invalidated_by", ["takeover", "expiry"])
def test_model_step_rechecks_claim_after_preparation_commit(make_rig, monkeypatch, invalidated_by):
    rig = make_rig(summary_mode="model")
    progress, _, material = _material(rig)
    send = rig.engine._generate_summary
    prepared = []

    def after_commit(*args, **kwargs):
        assert not rig.log.in_transaction
        with closing(EventLog(rig.root / "events.db", blob_dir=rig.root / "blobs")) as reader:
            saved = SummaryStore(reader, create=False).latest(material.material_id)
            call, = read_calls(reader)
            reservation = BudgetLedger(reader).reservation_for(rig.account, saved.attempt_id)
            assert call["response"] is None and reservation.state == PENDING
            prepared.append((saved, call, reservation))
        if invalidated_by == "takeover":
            payload = {"root": str(rig.root), "job_id": rig.claim.job_id, "action": "claim"}
            owner = _run_other_writer(rig, payload)
            assert owner["epoch"] == rig.claim.claim_epoch + 1
        else:
            monkeypatch.setattr(rig.stack.claims, "_clock", lambda: 1_031)
        return send(*args, **kwargs)

    monkeypatch.setattr(rig.engine, "_generate_summary", after_commit)
    with pytest.raises(JobLost):
        rig.engine._model_step_frozen(rig.run_id, rig.claim, progress, rig.task, None, None)
    (saved, call, reservation), = prepared
    assert rig.model.calls == progress.model_attempts == 0
    assert rig.store.latest(material.material_id) == saved
    assert rig.stack.budget.reservation_for(rig.account, saved.attempt_id) == reservation
    assert read_calls(rig.log) == [call]
    assert not rig.log.read_run(rig.run_id, event_types=("model.intent",))


_CANCEL_AFTER_PREPARE = """
import json
import os
import sys
from contextlib import closing
from pathlib import Path

from tianshu.budget import BudgetLedger
from tianshu.events import EventLog

root = Path(sys.argv[1])
account = sys.argv[2]
print(json.dumps({"phase": "ready", "pid": os.getpid()}), flush=True)
assert sys.stdin.readline().strip() == "cancel"
with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
    ledger = BudgetLedger(log)
    assert ledger.cancel(account) == 1
    assert ledger.is_cancelled(account)
print(json.dumps({"phase": "cancelled", "pid": os.getpid()}), flush=True)
assert sys.stdin.readline().strip() == "done"
"""


def _child_line(lines, phase):
    try:
        line = lines.get(timeout=30)
    except queue.Empty as exc:
        raise AssertionError(f"child did not reach {phase}") from exc
    if not line:
        raise AssertionError(f"child exited before {phase}")
    message = json.loads(line)
    assert message["phase"] == phase and message["pid"] != os.getpid()
    return message


@pytest.mark.parametrize("block_fail", [False, True])
def test_budget_cancel_after_prepare_releases_confirmed_unsent_attempt(
    make_rig, monkeypatch, block_fail,
):
    rig = make_rig(summary_mode="model")
    progress, _, material = _material(rig)
    with rig.log.transaction():
        attempt = rig.engine._prepare_summary(material)
    assert attempt is not None
    record, reservation, _ = attempt
    before = rig.stack.budget.reconcile(rig.account)
    assert before[0]["state"] == PENDING
    if block_fail:
        with rig.log.transaction():
            rig.log.conn.execute(
                "CREATE TRIGGER reject_cancel_failed BEFORE UPDATE ON summary_versions "
                "WHEN NEW.status = 'failed' BEGIN "
                "SELECT RAISE(ABORT, 'injected cancellation summary refusal'); END",
            )
    process = subprocess.Popen(
        [sys.executable, "-P", "-c", _CANCEL_AFTER_PREPARE, str(rig.root), rig.account],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_script_env(rig.root), cwd=SOURCE_DIR.parent,
    )
    lines = queue.Queue()

    def read_lines():
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put("")

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    send = rig.engine._generate_summary

    def cancel_before_final_claim_check(*args, **kwargs):
        assert not rig.log.in_transaction
        _child_line(lines, "ready")
        process.stdin.write("cancel\n")
        process.stdin.flush()
        _child_line(lines, "cancelled")
        with closing(EventLog(rig.root / "events.db", blob_dir=rig.root / "blobs")) as reader:
            ledger = BudgetLedger(reader)
            assert ledger.is_cancelled(rig.account)
            assert ledger.reservation_for(rig.account, record.attempt_id) == reservation
        process.stdin.write("done\n")
        process.stdin.flush()
        assert process.wait(timeout=30) == 0, process.stderr.read()
        return send(*args, **kwargs)

    monkeypatch.setattr(rig.engine, "_generate_summary", cancel_before_final_claim_check)
    try:
        assert rig.engine._generate_summary(progress, attempt, rig.claim) is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        process.stdout.close()
        assert process.stderr is not None
        process.stderr.close()
    assert rig.model.calls == progress.model_attempts == 0
    saved = rig.store.read(record.summary_id)
    released = rig.stack.budget.reservation_for(rig.account, record.attempt_id)
    if block_fail:
        assert saved.status == PENDING and saved == record
        assert released == reservation
    else:
        assert saved.status == FAILED and "取消" in saved.error and saved.text is None
        assert released.state == RELEASED
    counters = rig.stack.budget.usage(rig.account)
    assert counters["cancelled"] and all(value == 0 for value in counters["settled"].values())
    if block_fail:
        assert any(value != 0 for value in counters["reserved"].values())
    else:
        assert all(value == 0 for value in counters["reserved"].values())
    call, = read_calls(rig.log)
    assert call["request"]["attempt_id"] == record.attempt_id and call["response"] is None
