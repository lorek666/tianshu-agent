"""辅助摘要遥测来自真实调用，不把主对话事件或未知用量伪装成缓存结果。"""

import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from test_summary_loop import (
    CONSTRAINT,
    ROUNDS,
    ConstraintModel,
    _limits,
    _run,
    _spawn_crash,
    _ws,
)

from tianshu.events import EventLog
from tianshu.model import ModelError, ModelIdentity
from tianshu.replay import replay


def calls(log):
    from tianshu.summary_telemetry import read_calls

    return read_calls(log)


class CachedSummaryModel(ConstraintModel):
    cache_policy = "prefix-v1"

    def price_identity(self):
        return ModelIdentity(adapter="openai-compatible", vendor="openai", model="gpt-4o")

    def complete(self, messages, tools):
        response = super().complete(messages, tools)
        if not tools:
            response.usage.update(
                cache_read_input_tokens=80, cache_creation_input_tokens=0,
            )
        return response


@pytest.mark.parametrize("adapter,vendor,model_name", [
    ("openai-compatible", "openai", "gpt-4o"),
    ("anthropic", "anthropic", "claude-sonnet-4-5"),
])
def test_summary_usage_survives_reopen_without_becoming_a_main_turn(
    tmp_path, adapter, vendor, model_name,
):
    class ProviderSummary(CachedSummaryModel):
        def price_identity(self):
            return ModelIdentity(adapter=adapter, vendor=vendor, model=model_name)

    model = ProviderSummary()
    result, _, log = _run(tmp_path, model, summary_mode="model")
    recorded = calls(log)
    assert len(recorded) == result.summary_calls == model.summary_calls > 0
    assert result.model_attempts == result.turns + len(recorded)
    assert result.tokens == ROUNDS * 48 + 36 + len(recorded) * 140
    assert len(log.read_run(result.run_id, event_types=("model.result",))) == result.turns
    for call in recorded:
        request, response = call["request"], call["response"]
        assert request["run_id"] == result.run_id
        assert request["provider"] == vendor
        assert request["adapter"] == adapter
        assert request["model"] == model_name
        assert request["policy"] == "prefix-v1"
        assert response["request_hash"] == call["request_hash"]
        assert response["usage"] == {
            "input_tokens": 120, "output_tokens": 20,
            "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0,
        }
        assert response["cost"]["cost"] is None  # 没有缓存倍率，不猜费用。
        assert CONSTRAINT not in json.dumps(call, ensure_ascii=False)
    assert replay(log, result.run_id).ok
    log.close()
    reopened = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    assert calls(reopened) == recorded
    assert replay(reopened, result.run_id).ok
    reopened.close()


@pytest.mark.parametrize("body", ["", "x" * 3000])
def test_rejected_summary_body_retains_real_returned_usage(tmp_path, body):
    class RejectedBody(CachedSummaryModel):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            return replace(response, content=body) if not tools else response

    result, _, log = _run(tmp_path, RejectedBody(), summary_mode="model")
    recorded = calls(log)
    assert len(recorded) == result.summary_calls > 0
    assert all(call["response"]["usage"]["cache_read_input_tokens"] == 80 for call in recorded)
    assert all(row[0] == "failed" for row in log.conn.execute(
        "SELECT status FROM summary_versions",
    ))
    assert replay(log, result.run_id).ok
    log.close()


def test_non_utf8_summary_falls_back_after_preserving_call_and_settlement(tmp_path):
    from httpserver import NetFixture

    from tianshu.model import OpenAICompatibleModel
    from tianshu.summaries import FAILED, SummaryStore

    wire = (
        r'{"choices":[{"finish_reason":"stop","message":{"content":"前缀\ud800"}}],'
        r'"usage":{"prompt_tokens":23,"completion_tokens":7}}'
    )
    # 真实 OpenAICompatibleModel.complete 走本机 HTTP transport 和 JSON 解析；
    # JSON 转义可产生孤立 surrogate，但正文不是可写入 UTF-8 的合法文本。
    with NetFixture() as fixture:
        fixture.route("/v1/chat/completions", body=wire, content_type="application/json")
        adapter = OpenAICompatibleModel(
            "offline-non-utf8", base_url=f"http://{fixture.base}/v1",
            api_key="offline-fixture", cache_policy="prefix-v1",
        )

        class NonUtf8Summary(CachedSummaryModel):
            def complete(self, messages, tools):
                if tools:
                    return super().complete(messages, tools)
                self.calls += 1
                self.requests.append(sum(len(m.get("content") or "") for m in messages))
                self.summary_calls += 1
                return adapter.complete(messages, tools)

        model = NonUtf8Summary()
        result, stack, log = _run(
            tmp_path, model, summary_mode="model", limits_kwargs={"summary_max_calls": 1},
        )
    assert fixture.hit_count == 1 and fixture.paths() == ["/v1/chat/completions"]
    assert result.outcome == "noop" and result.turns == ROUNDS + 1
    assert model.summary_calls == 1
    assert result.model_attempts == result.turns + 1
    assert result.tokens == ROUNDS * 48 + 36 + 30
    receipt, = calls(log)
    assert receipt["response"]["usage"]["input_tokens"] == 23
    assert receipt["response"]["usage"]["output_tokens"] == 7
    record = SummaryStore(log, create=False).read(receipt["request"]["summary_id"])
    assert record.status == FAILED and record.text is None
    assert "UTF-8" in record.error
    reservation = stack.budget.reservation_for(f"run:{result.run_id}", record.attempt_id)
    assert reservation.state == "settled"
    saved = log.conn.execute(
        "SELECT usage_json FROM budget_reservations WHERE reservation_id=?",
        (reservation.reservation_id,),
    ).fetchone()
    usage = json.loads(saved["usage_json"])
    assert usage["requests"] == 1 and usage["tokens"] == 30 and usage["micros"] == 128
    assert stack.budget.usage(f"run:{result.run_id}")["settled"]["requests"] == model.calls
    assert not any(event.payload["context"].get("summaries") for event in log.read_run(
        result.run_id, event_types=("model.intent",),
    ))
    assert replay(log, result.run_id).ok
    log.close()


def test_failed_summary_call_preserves_unknown_usage_without_retry(tmp_path):
    class FailingSummary(CachedSummaryModel):
        def complete(self, messages, tools):
            if not tools:
                self.summary_calls += 1
                raise ModelError("offline failure", kind="auth")
            return super().complete(messages, tools)

    model = FailingSummary()
    result, _, log = _run(tmp_path, model, summary_mode="model")
    recorded = calls(log)
    assert len(recorded) == model.summary_calls == result.summary_calls > 0
    assert all(call["response"] is None and call["response_hash"] is None for call in recorded)
    assert result.unknown_usage_calls >= len(recorded)
    log.close()


@pytest.mark.parametrize("cap", [1, 3])
def test_sent_summary_hard_exit_leaves_unknown_record_and_resume_does_not_resend(tmp_path, cap):
    from support import engine_for, stack_for

    from tianshu.budget import PENDING
    from tianshu.summaries import FAILED, SummaryStore

    ws = _ws(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    proc = _spawn_crash(tmp_path, ws, ROUNDS)
    assert proc.returncode == 77, proc.stderr
    log = EventLog(tmp_path / "data" / "events.db", blob_dir=tmp_path / "data" / "blobs")
    before = calls(log)
    assert len(before) == 1
    assert before[0]["response"] is None
    request = before[0]["request"]
    run_id, attempt_id = request["run_id"], request["attempt_id"]
    account_id = f"run:{run_id}"
    stack = stack_for(log, data_root=tmp_path / "data")
    old_reservation = stack.budget.reservation_for(account_id, attempt_id)
    assert old_reservation is not None and old_reservation.state == PENDING
    model = ConstraintModel(rounds=0)
    engine = engine_for(
        stack, model, limits=replace(_limits(summary_mode="model"), summary_max_calls=cap),
    )
    old_main_attempts = len(log.read_run(run_id, event_types=("model.intent",)))
    result = engine.resume_run(run_id)
    assert result.model_attempts_known is False
    assert result.model_attempts == old_main_attempts + model.calls
    assert calls(log) == before
    assert model.summary_calls == 0
    records = SummaryStore(log, create=False).versions(request["material_id"])
    assert [record.status for record in records] == [FAILED]
    assert records[0].attempt_id == attempt_id
    assert stack.budget.reservation_for(account_id, attempt_id) == old_reservation
    log.close()


@pytest.mark.parametrize("table", ["summary_model_calls", "summary_model_usage"])
def test_summary_telemetry_rejects_deletion_overwrite_and_duplicates(tmp_path, table):
    _, _, log = _run(tmp_path, CachedSummaryModel(), summary_mode="model")
    assert calls(log)
    with pytest.raises(sqlite3.IntegrityError):
        log.conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        log.conn.execute(f"UPDATE {table} SET payload='{{}}'")
    with pytest.raises(sqlite3.IntegrityError):
        log.conn.execute(f"INSERT INTO {table} SELECT * FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        log.conn.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
    log.close()


def test_read_calls_run_filter_preserves_unfiltered_reader(tmp_path):
    from tianshu.pricing import CostRecord
    from tianshu.summaries import MaterialBlock, SummaryStore, build_material
    from tianshu.summary_telemetry import begin_call, finish_call, read_calls

    with closing(EventLog(tmp_path / "events.db")) as log:
        store = SummaryStore(log)
        expected = {}
        for run_id in ("run-a", "run-b"):
            material = build_material(
                [MaterialBlock(seq=1, role="tool", content=run_id)],
                scope="task:t", run_id=run_id,
            )
            record = store.begin(material, model="offline", run_id=run_id)
            messages = [{"role": "user", "content": run_id}]
            begin_call(log, record, messages=messages, identity=None, policy=None)
            if run_id == "run-a":
                finish_call(
                    log, record.summary_id,
                    usage={"input_tokens": 1, "output_tokens": 2},
                    cost=CostRecord(
                        None, "", None,
                        units={"input_tokens": 1, "output_tokens": 2},
                    ),
                )
            expected[run_id] = record.summary_id

        unfiltered = read_calls(log)
        assert len(unfiltered) == 2
        assert {call["request"]["summary_id"] for call in unfiltered} == set(expected.values())
        for run_id, summary_id in expected.items():
            selected = read_calls(log, run_id=run_id)
            assert len(selected) == 1
            assert selected[0]["request"]["run_id"] == run_id
            assert selected[0]["request"]["summary_id"] == summary_id
            assert (selected[0]["response"] is not None) == (run_id == "run-a")
        assert read_calls(log, run_id="missing") == []
        assert read_calls(log) == unfiltered, "旧的无过滤调用方仍读取并校验全库"


def test_legacy_read_is_unknown_and_does_not_create_tables(tmp_path):
    log = EventLog(tmp_path / "legacy.db")
    assert calls(log) == []
    assert not log.conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'summary_model_%'",
    ).fetchall()
    log.close()


@pytest.mark.parametrize("damage", [
    "json", "hash", "version", "usage-negative", "usage-bool", "usage-float",
    "usage-unknown", "usage-over-input", "request-binding", "orphan", "missing-table",
    "summary-binding",
])
def test_corrupt_telemetry_never_supplies_a_valid_cache_sample(tmp_path, damage):
    from tianshu.errors import TianshuError
    from tianshu.events import canonical_bytes, sha256_hex

    _, _, log = _run(tmp_path, CachedSummaryModel(), summary_mode="model")
    assert calls(log)
    log.conn.execute("DROP TRIGGER summary_model_usage_no_update")
    log.conn.execute("DROP TRIGGER summary_model_calls_no_update")
    row = log.conn.execute("SELECT * FROM summary_model_usage LIMIT 1").fetchone()
    payload = json.loads(row["payload"])
    if damage == "json":
        log.conn.execute("UPDATE summary_model_usage SET payload='{'")
    elif damage == "hash":
        log.conn.execute("UPDATE summary_model_usage SET payload_hash=?", ("0" * 64,))
    elif damage == "orphan":
        log.conn.execute("DROP TRIGGER summary_model_calls_no_delete")
        log.conn.execute("DELETE FROM summary_model_calls WHERE summary_id=?", (row["summary_id"],))
    elif damage == "missing-table":
        log.conn.execute("DROP TABLE summary_model_usage")
    elif damage == "summary-binding":
        log.conn.execute("UPDATE summary_versions SET attempt_id='different'")
    else:
        if damage == "version":
            payload["schema"] = "tianshu.summary-usage.v999"
        elif damage == "request-binding":
            payload["request_hash"] = "0" * 64
        elif damage == "usage-unknown":
            payload["usage"]["invented_cache"] = 10
        else:
            payload["usage"]["cache_read_input_tokens"] = {
                "usage-negative": -1, "usage-bool": True, "usage-float": 1.25,
                "usage-over-input": 121,
            }[damage]
        raw = canonical_bytes(payload)
        log.conn.execute(
            "UPDATE summary_model_usage SET payload=?,payload_hash=? WHERE summary_id=?",
            (raw.decode("utf-8"), sha256_hex(raw), row["summary_id"]),
        )
    with pytest.raises(TianshuError, match="遥测"):
        calls(log)
    log.close()


@pytest.mark.parametrize("column,value", [
    ("version", 999), ("version", 1.25), ("model", "wrong:model:identity"),
    ("status", "invented"),
])
def test_corrupt_summary_identity_is_rejected_without_altering_telemetry(
    tmp_path, column, value,
):
    from tianshu.errors import TianshuError

    _, _, log = _run(tmp_path, CachedSummaryModel(), summary_mode="model")
    assert calls(log)
    row = log.conn.execute("SELECT summary_id FROM summary_versions LIMIT 1").fetchone()
    log.conn.execute(
        f"UPDATE summary_versions SET {column}=? WHERE summary_id=?", (value, row[0]),
    )
    with pytest.raises(TianshuError, match="遥测"):
        calls(log)
    log.close()


def test_usage_storage_failure_preserves_returned_accounting_and_falls_back(tmp_path):
    class UsageWriteFailure(CachedSummaryModel):
        def complete(self, messages, tools):
            if not tools:
                with sqlite3.connect(tmp_path / "events.db") as conn:
                    conn.execute(
                        "CREATE TRIGGER IF NOT EXISTS test_usage_refusal "
                        "BEFORE INSERT ON summary_model_usage BEGIN "
                        "SELECT RAISE(ABORT, 'injected usage storage refusal'); END",
                    )
            return super().complete(messages, tools)

    model = UsageWriteFailure()
    result, _, log = _run(tmp_path, model, summary_mode="model")
    assert result.outcome in ("succeeded", "noop")
    assert model.summary_calls > 0
    assert result.tokens == ROUNDS * 48 + 36 + model.summary_calls * 140
    assert result.model_attempts == result.turns + model.summary_calls
    assert result.unknown_usage_calls == 0  # 返回用量已知，旁路回执缺失单列。
    recorded = calls(log)
    assert len(recorded) == model.summary_calls
    assert all(call["response"] is None for call in recorded)
    assert all(row["status"] == "failed" for row in log.conn.execute(
        "SELECT status FROM summary_versions",
    ))
    counters = {row["dimension"]: row for row in log.conn.execute(
        "SELECT * FROM budget_counters WHERE account_id=?", (f"run:{result.run_id}",),
    )}
    assert counters["tokens"]["settled"] == result.tokens
    assert counters["requests"]["settled"] == result.model_attempts
    end = log.read_run(result.run_id, event_types=("run.end",))[-1].payload
    assert end["tokens"] == result.tokens
    assert replay(log, result.run_id).ok
    log.close()


def test_call_storage_failure_does_not_send_or_consume_reserved_budget(tmp_path, monkeypatch):
    from tianshu import _loop_summary

    original = _loop_summary.begin_call

    def refuse_call(log, *args, **kwargs):
        log.conn.execute(
            "CREATE TRIGGER IF NOT EXISTS test_call_refusal "
            "BEFORE INSERT ON summary_model_calls BEGIN "
            "SELECT RAISE(ABORT, 'injected call storage refusal'); END",
        )
        return original(log, *args, **kwargs)

    monkeypatch.setattr(_loop_summary, "begin_call", refuse_call)
    model = CachedSummaryModel()
    result, _, log = _run(tmp_path, model, summary_mode="model")
    assert result.outcome in ("succeeded", "noop")
    assert model.summary_calls == 0
    assert calls(log) == []
    assert result.summary_calls == 0
    assert log.conn.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0] == 0
    assert log.conn.execute(
        "SELECT COUNT(*) FROM budget_reservations WHERE call_id LIKE 'summary#%'",
    ).fetchone()[0] == 0
    assert result.tokens == ROUNDS * 48 + 36
    counters = {row["dimension"]: row for row in log.conn.execute(
        "SELECT * FROM budget_counters WHERE account_id=?", (f"run:{result.run_id}",),
    )}
    assert counters["tokens"]["settled"] == result.tokens
    assert counters["requests"]["settled"] == result.turns
    assert counters["tokens"]["reserved"] == counters["requests"]["reserved"] == 0
    log.close()


@pytest.mark.parametrize("approve_gc", [
    False,
    pytest.param(True, marks=pytest.mark.skipif(
        os.name != "posix", reason="Approved file GC requires directory fsync (#200)",
    )),
])
def test_backup_database_roundtrip_and_gc_preserve_auxiliary_facts(tmp_path, approve_gc):
    from support import engine_for, stack_for

    from tianshu.backup import create_backup, verify_backup
    from tianshu.data_gc import run_data_gc

    root = tmp_path / "root"
    root.mkdir()
    ws = _ws(tmp_path / "workspace")
    log = EventLog(
        root / "events.db", blob_dir=root / "artifacts" / "blobs", data_root=root,
    )
    stack = stack_for(log, data_root=root)
    engine = engine_for(stack, CachedSummaryModel(), limits=_limits(summary_mode="model"))
    task = stack.tasks.create_task("长任务：整理笔记并写报告", [str(ws)])
    engine.start(task.task_id, out_name="")
    recorded = calls(log)
    assert recorded
    log.close()
    destination = tmp_path / "saved"
    create_backup(root, destination)
    verification = verify_backup(destination)
    assert verification.ok, verification.problems
    roundtrip = tmp_path / "roundtrip.db"
    shutil.copyfile(destination / "events.db", roundtrip)
    reopened = EventLog(roundtrip)
    assert calls(reopened) == recorded
    reopened.close()
    # 在线备份的只读源连接可能留下 WAL/SHM。通过正常 SQLite 生命周期
    # 完成源库关闭，不手动删除旁文件，也不放宽 GC 的零写入预演前置条件。
    source = EventLog(
        root / "events.db", blob_dir=root / "artifacts" / "blobs", data_root=root,
    )
    source.close()
    assert not any((root / f"events.db{suffix}").exists()
                   for suffix in ("-wal", "-shm", "-journal"))
    # GC 只删除其封闭允许集；新表即使已过保留期仍保留。
    report, code = run_data_gc(root, approve=approve_gc, clock=lambda: 10**14)
    assert code == (0 if approve_gc else 3), report
    after = EventLog(root / "events.db")
    assert calls(after) == recorded
    after.close()


@pytest.mark.parametrize("blocked_statuses", [("ready",), ("ready", "failed")])
def test_summary_body_write_error_falls_back_and_keeps_single_settlement(
    tmp_path, blocked_statuses,
):
    from support import engine_for, stack_for

    model = CachedSummaryModel()
    ws = _ws(tmp_path)
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        for status in blocked_statuses:
            log.conn.execute(
                f"CREATE TRIGGER reject_{status} BEFORE UPDATE ON summary_versions "
                f"WHEN NEW.status = '{status}' BEGIN SELECT RAISE(ABORT, 'summary write'); END"
            )
        engine = engine_for(
            stack, model, limits=replace(_limits(summary_mode="model"), summary_max_calls=1),
        )
        task = stack.tasks.create_task("摘要正文落盘失败后继续任务", [str(ws)])
        result = engine.start(task.task_id)
        assert result.outcome == "noop" and result.turns == ROUNDS + 1
        assert result.model_attempts_known and result.model_attempts == ROUNDS + 2
        assert result.tokens == ROUNDS * 48 + 36 + 140
        entry, = calls(log)
        record = stack.summaries.read(entry["request"]["summary_id"])
        expected_status = "failed" if len(blocked_statuses) == 1 else "pending"
        assert record.status == expected_status
        assert model.summary_calls == 1
        account = f"run:{result.run_id}"
        reservation = stack.budget.reservation_for(account, record.attempt_id)
        assert reservation is not None
        row = next(row for row in stack.budget.reconcile(account)
                   if row["reservation_id"] == reservation.reservation_id)
        assert row["usage"] == {"requests": 1, "tokens": 140}
        assert stack.budget.usage(account)["settled"]["requests"] == model.calls
        assert entry["response"]["usage"]["input_tokens"] == 120
        assert not any(event.payload["context"].get("summaries") for event in log.read_run(
            result.run_id, event_types=("model.intent",),
        ))
        assert replay(log, result.run_id).ok


def test_recovery_reuses_a_confirmed_summary_without_recounting_after_pause(tmp_path):
    from support import engine_for, stack_for

    ws = _ws(tmp_path)
    root = tmp_path / "data"
    root.mkdir()
    proc = _spawn_crash(tmp_path, ws, ROUNDS, after_ready=True)
    assert proc.returncode == 77 and "summary-test-after-ready" in proc.stderr
    limits = replace(_limits(summary_mode="model"), summary_max_calls=1)
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        stack = stack_for(log, data_root=root)
        receipt, = calls(log)
        request = receipt["request"]
        run_id = request["run_id"]
        assert receipt["response"]["usage"] == {"input_tokens": 5, "output_tokens": 2}
        prior_main_calls = len(log.read_run(run_id, event_types=("model.result",)))
        saved = stack.tasks.load_cursor(run_id)[0]
        assert saved.budget_ledger["model_attempts"] == prior_main_calls
        reservation = stack.budget.reservation_for(f"run:{run_id}", request["attempt_id"])
        row = next(row for row in stack.budget.reconcile(f"run:{run_id}")
                   if row["reservation_id"] == reservation.reservation_id)
        assert row["usage"] == {"requests": 1, "tokens": 7}

        class PauseAfterModel(ConstraintModel):
            def complete(self, messages, tools):
                response = super().complete(messages, tools)
                stack.tasks.request_pause(run_id)
                return response

        model = PauseAfterModel(rounds=0)
        paused = engine_for(stack, model, limits=limits).resume_run(run_id)
        assert paused.state == "paused" and model.round_calls == 1 and model.summary_calls == 0
        assert paused.model_attempts == prior_main_calls + 2
        assert paused.model_attempts_known is False
        assert calls(log) == [receipt]

    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        stack = stack_for(log, data_root=root)
        model = ConstraintModel(rounds=0)
        finished = engine_for(stack, model, limits=limits).resume_run(run_id)
        assert finished.outcome == "noop" and model.calls == 0
        assert finished.model_attempts == paused.model_attempts
        assert finished.model_attempts_known is False
        assert calls(log) == [receipt]
        assert stack.budget.reservation_for(f"run:{run_id}", request["attempt_id"]) == reservation
        assert replay(log, run_id).ok
