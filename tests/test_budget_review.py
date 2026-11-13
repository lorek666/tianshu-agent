"""独立复审 B1/B2/B3 的正式回归：真实 adapter、临时账本、受控 transport。"""

from __future__ import annotations

import json

import pytest
from support import engine_for, stack_for

from tianshu.budget import BudgetLedger
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import AnthropicMessagesModel, ModelError, OpenAICompatibleModel
from tianshu.replay import replay
from tianshu.tasks import TaskStore


class ControlledTransport:
    def __init__(self, vendor, usage, *, summary=False, summary_error=False):
        self.vendor = vendor
        self.usage = usage
        self.summary = summary
        self.summary_error = summary_error
        self.calls = 0
        self.normal_calls = 0
        self.summary_calls = 0

    def post(self, url, *, body, headers, timeout):
        self.calls += 1
        is_summary = self.summary and not body.get("tools")
        tool = None
        text = "本次材料要求仅用中文。"
        if is_summary:
            self.summary_calls += 1
            usage = self.usage
            if self.summary_error:
                raise ModelError("controlled summary response failure", kind="protocol")
        else:
            self.normal_calls += 1
            usage = {"input_tokens": 10, "output_tokens": 5} if self.summary else self.usage
            if self.summary and self.normal_calls <= 6:
                tool = {"id": f"probe-{self.normal_calls}", "path": "source.md"}
        if self.vendor == "openai":
            usage = {
                {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"}.get(k, k): v
                for k, v in usage.items()
            }
            message = {"content": text}
            if tool:
                message["tool_calls"] = [
                    {
                        "id": tool["id"],
                        "type": "function",
                        "function": {
                            "name": "read_text",
                            "arguments": json.dumps({"path": tool["path"]}),
                        },
                    }
                ]
            return {
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if tool else "stop"}
                ],
                "usage": usage,
            }
        content = [{"type": "text", "text": text}]
        if tool:
            content.append(
                {
                    "type": "tool_use",
                    "id": tool["id"],
                    "name": "read_text",
                    "input": {"path": tool["path"]},
                }
            )
        return {
            "content": content,
            "stop_reason": "tool_use" if tool else "end_turn",
            "usage": usage,
        }


def adapter(vendor, transport, *, priced=False):
    cls = OpenAICompatibleModel if vendor == "openai" else AnthropicMessagesModel
    model = (
        {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-6"}[vendor]
        if priced
        else "not-a-price-table-entry"
    )
    host = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1"}[
        vendor
    ]
    return cls(model, base_url=host, api_key="local-verifier-placeholder", transport=transport)


def execute(
    tmp_path,
    vendor,
    usage,
    *,
    summary=False,
    summary_error=False,
    priced=False,
    limits=None,
    observe=None,
    bound="estimate",
):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "source.md").write_text(
        "本次材料要求仅用中文。\n" + "供上下文压缩使用的受控正文。" * 85, encoding="utf-8"
    )
    transport = ControlledTransport(vendor, usage, summary=summary, summary_error=summary_error)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    if observe is not None:
        post = transport.post

        def observed_post(*args, **kwargs):
            observe(log, transport)
            return post(*args, **kwargs)

        transport.post = observed_post
    result = run(
        task="读取材料并给出摘要。",
        workspace_dir=ws,
        out_name="",
        model=adapter(vendor, transport, priced=priced),
        log=log,
        data_root=tmp_path / "data",
        limits=Limits(
            max_turns=10,
            max_total_tokens=100_000,
            context_budget_bytes=3000 if summary else 20000,
            summary_mode="model" if summary else "off",
            summary_max_calls=1,
            retry_backoff_ms=0,
            budget_limits=limits,
            budget_bound=bound,
        ),
    )
    return result, transport, log


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_micros_limit_must_prevent_priced_call(tmp_path, vendor):
    result, transport, log = execute(
        tmp_path,
        vendor,
        {"input_tokens": 10000, "output_tokens": 1000},
        priced=True,
        limits={"micros": 0},
    )
    try:
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        print(
            "MONEY_LIMIT",
            json.dumps(
                {
                    "vendor": vendor,
                    "calls": transport.calls,
                    "known_cost": result.cost_total_micros,
                    "unknown_cost_calls": result.unknown_cost_calls,
                    "ledger": usage,
                },
                ensure_ascii=False,
            ),
        )
        assert transport.calls == 0, "PRD 5.6: amount budget exhausted must stop before request"
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_unknown_price_is_visible_in_money_ledger(tmp_path, vendor):
    result, transport, log = execute(
        tmp_path, vendor, {"input_tokens": 12, "output_tokens": 3}, limits={"micros": 0}
    )
    try:
        ledger = BudgetLedger(log)
        usage = ledger.usage(f"run:{result.run_id}")
        print(
            "UNKNOWN_MONEY",
            json.dumps(
                {
                    "vendor": vendor,
                    "calls": transport.calls,
                    "unknown_cost_calls": result.unknown_cost_calls,
                    "ledger": usage,
                    "reconcile": ledger.reconcile(),
                },
                ensure_ascii=False,
            ),
        )
        assert transport.calls == 0 or usage["unknown_reservations"] > 0
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_failed_summary_marks_run_budget_unknown(tmp_path, vendor):
    result, transport, log = execute(
        tmp_path, vendor, {"input_tokens": -9, "output_tokens": 5}, summary=True, priced=True
    )
    try:
        ledger = BudgetLedger(log)
        end = [e.payload for e in log.read_run(result.run_id) if e.type == "run.end"][-1]
        print(
            "NEGATIVE_SUMMARY",
            json.dumps(
                {
                    "vendor": vendor,
                    "calls": transport.calls,
                    "summary_calls": transport.summary_calls,
                    "unknown_usage_calls": result.unknown_usage_calls,
                    "unknown_cost_calls": result.unknown_cost_calls,
                    "run_end": end,
                    "ledger": ledger.usage(f"run:{result.run_id}"),
                },
                ensure_ascii=False,
            ),
        )
        assert transport.summary_calls == 1
        assert end["budget_enforced"] is False, (
            "Failed summary may be charged, so budget cannot be reported as known"
        )
        assert result.unknown_usage_calls == result.unknown_cost_calls == 1
        assert result.model_attempts == transport.calls
        assert end["tokens_unknown_calls"] == 1
        assert replay(log, result.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_failed_summary_protocol_was_already_unknown(tmp_path, vendor):
    result, transport, log = execute(
        tmp_path, vendor, {}, summary=True, summary_error=True, priced=True
    )
    try:
        end = [e.payload for e in log.read_run(result.run_id) if e.type == "run.end"][-1]
        assert transport.summary_calls == 1
        print(
            "PROTOCOL_SUMMARY",
            json.dumps(
                {
                    "vendor": vendor,
                    "budget_enforced": end["budget_enforced"],
                    "unknown_reservations": BudgetLedger(log).usage(f"run:{result.run_id}")[
                        "unknown_reservations"
                    ],
                }
            ),
        )
        assert end["budget_enforced"] is False
        assert result.unknown_usage_calls == result.unknown_cost_calls == 1
        assert result.model_attempts == transport.calls
        assert end["tokens_unknown_calls"] == 1
        assert replay(log, result.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_failed_summary_unknown_survives_immediate_budget_stop_and_reopen(tmp_path, vendor):
    observed = []

    def observe(log, transport):
        ledger = BudgetLedger(log)
        pending = ledger.pending()
        assert len(pending) == 1
        usage = ledger.usage(pending[0].account_id)
        observed.append(
            (
                pending[0].attempt_id.startswith("summary#"),
                usage["settled"]["micros"] + usage["reserved"]["micros"],
            )
        )

    calibrate = tmp_path / "calibrate"
    calibrate.mkdir()
    _, _, log = execute(
        calibrate,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        summary=True,
        summary_error=True,
        priced=True,
        observe=observe,
    )
    log.close()
    summary_index = next(index for index, row in enumerate(observed) if row[0])
    limit = observed[summary_index][1]
    assert all(amount <= limit for _, amount in observed[:summary_index])
    limited = tmp_path / "limited"
    limited.mkdir()
    result, transport, log = execute(
        limited,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        summary=True,
        summary_error=True,
        priced=True,
        limits={"micros": limit},
    )
    try:
        assert result.outcome == "failed" and "micros" in result.error
        assert transport.normal_calls == summary_index
        assert transport.summary_calls == 1
        assert result.model_attempts == transport.calls == summary_index + 1
        assert result.unknown_usage_calls == result.unknown_cost_calls == 1
        events = log.read_run(result.run_id)
        end = [event.payload for event in events if event.type == "run.end"][-1]
        assert end["tokens_unknown_calls"] == 1
        assert end["budget_enforced"] is False
        assert end["unbilled_model_intents"] == 0
        assert end["tokens"] == result.tokens
        stored = TaskStore(log).load_cursor(result.run_id)
        assert stored is not None
        cursor = stored[0].to_dict()
        assert cursor["budget_ledger"]["unknown_usage_calls"] == 1
        assert cursor["budget_ledger"]["unknown_cost_calls"] == 1
        assert cursor["budget_ledger"]["model_attempts"] == transport.calls
        rows = BudgetLedger(log).reconcile(f"run:{result.run_id}")
        assert len(rows) == 1 and rows[0]["attempt_id"].startswith("summary#")
        assert rows[0]["usage"] == {"requests": 1}
        assert replay(log, result.run_id).ok
    finally:
        log.close()

    reopened = EventLog(limited / "events.db", blob_dir=limited / "blobs")
    try:
        assert TaskStore(reopened).load_cursor(result.run_id)[0].to_dict() == cursor
        assert BudgetLedger(reopened).reconcile(f"run:{result.run_id}") == rows
        reopened_end = [e.payload for e in reopened.read_run(result.run_id) if e.type == "run.end"]
        assert reopened_end == [end]
        assert replay(reopened, result.run_id).ok
    finally:
        reopened.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_default_unknown_price_remains_unresolved_after_reopen(tmp_path, vendor):
    result, transport, log = execute(tmp_path, vendor, {"input_tokens": 12, "output_tokens": 3})
    try:
        account = f"run:{result.run_id}"
        assert transport.calls == result.unknown_cost_calls == 1
        ledger = BudgetLedger(log)
        assert ledger.usage(account)["unknown_reservations"] == 1
        records = ledger.reconcile(account)
        assert len(records) == 1
        assert records[0]["need"]["micros"] == 0, "零预留不代表已知零费用"
        assert "micros" not in records[0]["usage"]
        assert replay(log, result.run_id).ok
    finally:
        log.close()
    reopened = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        assert BudgetLedger(reopened).reconcile(account) == records
    finally:
        reopened.close()


@pytest.mark.parametrize("column", ["reserved", "settled"])
@pytest.mark.parametrize("invalid", [-7, 0.5, "invalid"])
def test_illegal_historical_counters_reject_reads_and_reservations(tmp_path, column, invalid):
    log = EventLog(tmp_path / "historic.db")
    ledger = BudgetLedger(log)
    try:
        ledger.create_account("root", limits={"tokens": 10})
        ledger.create_account("leaf", parent_id="root", limits={"tokens": 100})
        ledger.reserve("root", attempt_id="seed", need={"tokens": 1})
        # 模拟旧版本可能留下的负计数，以及损坏的存储类型。只写测试临时库。
        log.conn.execute(
            f"UPDATE budget_counters SET {column}=? WHERE account_id='root'", (invalid,)
        )
        before = list(log.conn.execute("SELECT * FROM budget_counters"))
        with pytest.raises(TianshuError, match="预算.*非负整数"):
            ledger.usage("root")
        with pytest.raises(TianshuError, match="预算.*非负整数"):
            ledger.reserve("leaf", attempt_id="must-not-admit", need={"tokens": 17})
        assert [tuple(row) for row in log.conn.execute("SELECT * FROM budget_counters")] == [
            tuple(row) for row in before
        ]
        assert ledger.reservation_for("leaf", "must-not-admit") is None
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
@pytest.mark.parametrize("summary", [False, True])
def test_money_is_reserved_before_each_call_and_actual_cost_settles(tmp_path, vendor, summary):
    observed = []

    def before_call(log, transport):
        pending = BudgetLedger(log).pending()
        assert len(pending) == 1
        assert pending[0].need["micros"] > 0
        observed.append(pending[0].reservation_id)

    result, transport, log = execute(
        tmp_path,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        summary=summary,
        priced=True,
        limits={"micros": 1_000_000},
        observe=before_call,
    )
    try:
        assert result.outcome == "noop", result.error
        assert len(set(observed)) == transport.calls
        assert transport.summary_calls == int(summary)
        ledger = BudgetLedger(log)
        usage = ledger.usage(f"run:{result.run_id}")
        assert usage["settled"]["micros"] == result.cost_total_micros > 0
        assert usage["reserved"]["micros"] == usage["unknown_reservations"] == 0
        assert ledger.reconcile() == []
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
@pytest.mark.parametrize("priced", [False, True])
def test_ancestor_money_limit_rejects_child_before_transport(tmp_path, vendor, priced):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path / "data")
    transport = ControlledTransport(vendor, {"input_tokens": 12, "output_tokens": 3})
    try:
        stack.budget.create_account("root", limits={"micros": 0})
        stack.budget.create_account("run:child", parent_id="root", limits={"tokens": 10000})
        task = stack.tasks.create_task("只能使用父账户的额度", [])
        result = engine_for(stack, adapter(vendor, transport, priced=priced)).start(
            task.task_id,
            run_id="child",
        )
        assert result.outcome == "failed"
        assert transport.calls == 0
        assert stack.budget.pending() == []
        assert stack.budget.usage("root")["settled"] == {}
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_retry_retains_first_money_reservation_and_rechecks_limit(tmp_path, vendor):
    observed = []

    def timeout_after_reserving(log, transport):
        pending = BudgetLedger(log).pending()
        assert len(pending) == 1
        observed.append(pending[0].need["micros"])
        transport.calls += 1
        raise ModelError("受控超时，可能已计费", kind="timeout")

    # 首次以无限额运行测得这份固定请求的预占，响应在真实 transport 边界超时。
    first = tmp_path / "first"
    first.mkdir()
    result, transport, log = execute(
        first, vendor, {}, priced=True, observe=timeout_after_reserving
    )
    try:
        assert transport.calls == 3
        limit = observed[0]
        assert limit > 0 and observed == [limit] * 3
    finally:
        log.close()
    observed.clear()
    second = tmp_path / "second"
    second.mkdir()
    result, transport, log = execute(
        second,
        vendor,
        {},
        priced=True,
        limits={"micros": limit},
        observe=timeout_after_reserving,
    )
    try:
        assert transport.calls == 1
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["reserved"]["micros"] == limit
        assert usage["settled"].get("micros", 0) == 0
        assert usage["unknown_reservations"] == 1
        assert "micros" in (result.error or "")
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["anthropic"])
def test_upper_money_limit_without_price_bound_refuses_before_call(tmp_path, vendor):
    result, transport, log = execute(
        tmp_path,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        priced=True,
        limits={"micros": 1000000},
        bound="upper",
    )
    try:
        assert result.outcome == "failed"
        assert transport.calls == 0
        assert "预算配置" in (result.error or "")
        assert "金额" in (result.error or "")
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["anthropic"])
def test_upper_tokens_without_money_limit_does_not_claim_money_bound(tmp_path, vendor):
    result, transport, log = execute(
        tmp_path,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        priced=True,
        summary=True,
        bound="upper",
    )
    try:
        assert result.outcome == "noop", result.error
        assert transport.normal_calls == 7
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["settled"]["micros"] == result.cost_total_micros > 0
        assert usage["overrun"] is False
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_summary_money_budget_refuses_before_sending_summary(tmp_path, vendor):
    requirements = []

    def observe(log, transport):
        ledger = BudgetLedger(log)
        reservation = ledger.pending()[0]
        usage = ledger.usage(reservation.account_id)
        requirements.append(
            (
                reservation.attempt_id.startswith("summary#"),
                usage["settled"].get("micros", 0) + usage["reserved"]["micros"],
            )
        )

    first = tmp_path / "unlimited"
    first.mkdir()
    _, _, log = execute(
        first,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        summary=True,
        priced=True,
        observe=observe,
    )
    log.close()
    summary_index = next(index for index, row in enumerate(requirements) if row[0])
    limit = max(amount for _, amount in requirements[:summary_index])
    assert requirements[summary_index][1] > limit
    second = tmp_path / "limited"
    second.mkdir()
    result, transport, log = execute(
        second,
        vendor,
        {"input_tokens": 12, "output_tokens": 3},
        summary=True,
        priced=True,
        limits={"micros": limit},
    )
    try:
        assert transport.normal_calls == summary_index
        assert transport.summary_calls == 0
        summaries = list(log.conn.execute("SELECT status,error FROM summary_versions"))
        assert len(summaries) == 1
        assert summaries[0]["status"] == "failed"
        assert "micros" in summaries[0]["error"]
        assert result.outcome == "failed"
        assert replay(log, result.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("column", ["need_json", "usage_json"])
def test_illegal_historical_reservation_is_not_read_or_settled(tmp_path, column):
    log = EventLog(tmp_path / "history.db")
    ledger = BudgetLedger(log)
    try:
        ledger.create_account("root", limits={"tokens": 10})
        ledger.create_account("leaf", parent_id="root", limits={"tokens": 10})
        reservation = ledger.reserve("leaf", attempt_id="old", need={"tokens": 6})
        log.conn.execute(
            f"UPDATE budget_reservations SET {column}=? WHERE reservation_id=?",
            ('{"tokens": -7}', reservation.reservation_id),
        )
        tables = ("budget_accounts", "budget_limits", "budget_counters", "budget_reservations")

        def snapshot():
            return {
                table: [tuple(row) for row in log.conn.execute(f"SELECT * FROM {table}")]
                for table in tables
            }

        before = snapshot()
        with pytest.raises(TianshuError, match="非负整数"):
            ledger.get(reservation.reservation_id)
        with pytest.raises(TianshuError, match="非负整数"):
            ledger.reconcile("leaf")
        with pytest.raises(TianshuError, match="非负整数"):
            ledger.settle(reservation.reservation_id, usage={"tokens": 3})
        assert snapshot() == before
    finally:
        log.close()
