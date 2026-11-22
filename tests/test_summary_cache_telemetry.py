"""辅助摘要遥测来自真实调用，不把主对话事件或未知用量伪装成缓存结果。"""

import json
import os
import shutil
import sqlite3
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


def test_sent_summary_hard_exit_leaves_unknown_record_and_resume_does_not_resend(tmp_path):
    from support import engine_for, stack_for

    ws = _ws(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    proc = _spawn_crash(tmp_path, ws, ROUNDS)
    assert proc.returncode == 77, proc.stderr
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    before = calls(log)
    assert len(before) == 1
    assert before[0]["response"] is None
    model = ConstraintModel(rounds=0)
    engine = engine_for(
        stack_for(log, data_root=tmp_path / "data"), model,
        limits=_limits(summary_mode="model"),
    )
    engine.resume_run(before[0]["request"]["run_id"])
    assert calls(log) == before
    assert model.summary_calls == 0
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
    assert result.tokens == ROUNDS * 48 + 36
    counters = {row["dimension"]: row for row in log.conn.execute(
        "SELECT * FROM budget_counters WHERE account_id=?", (f"run:{result.run_id}",),
    )}
    assert counters["tokens"]["settled"] == result.tokens
    assert counters["requests"]["settled"] == result.turns
    assert counters["tokens"]["reserved"] == counters["requests"]["reserved"] == 0
    log.close()


def test_backup_database_roundtrip_and_gc_preserve_auxiliary_facts(tmp_path):
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
    # GC 只删除其封闭允许集；新表即使已过保留期仍保留。
    approved = os.name == "posix"
    report, code = run_data_gc(root, approve=approved, clock=lambda: 10**14)
    assert code == (0 if approved else 3), report
    after = EventLog(root / "events.db")
    assert calls(after) == recorded
    after.close()
