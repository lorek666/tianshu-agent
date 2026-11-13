"""G1b：§5.6 共享预算账本（施工规格 `docs/SPEC-G1-ADMISSION-2026-11-11.md` §3）。

每条测试对应 PRD §5.6 的一条规则：

1. 发送前原子预占；attempt 独立身份；幂等结算
2. 父子预算：子任务同时受根与局部上限约束；父汇总不二次计入
3. 多维度记账；估算不冒充上界
4. 未知用量保留未决占用，不按零结算
5. 预算耗尽只保存状态并通知，不再调用模型
6. deadline / 取消传递；不把取消当退款
7. 等待时不持写事务；恢复先核对预占、不猜测为零

加三组接线测试（主循环 + 控制面），否则"账本实现了"只是模块测试，不是门禁。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from support import stack_for
from test_model_contract import VENDORS, FakeTransport, _model, _payload

from tianshu.budget import (
    BOUND_UPPER,
    DIMENSIONS,
    PARTIAL,
    PENDING,
    RELEASED,
    SETTLED,
    UNKNOWN,
    BudgetLedger,
    estimate_need,
    estimate_tokens,
    upper_bound_tokens,
    upper_need,
)
from tianshu.control import ControlPlane
from tianshu.errors import BudgetCancelled, BudgetRefused, TianshuError
from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall


def _ledger(tmp_path: Path, name: str = "events.db") -> tuple[EventLog, BudgetLedger]:
    log = EventLog(tmp_path / name)
    return log, BudgetLedger(log)


# ---------------------------------------------------------------- 1 预占 / 结算


def test_reserve_refuses_when_exhausted(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10})
        ledger.reserve("a", attempt_id="t1", need={"tokens": 6})
        with pytest.raises(BudgetRefused) as caught:
            ledger.reserve("a", attempt_id="t2", need={"tokens": 6})
        assert caught.value.dimension == "tokens"
        assert caught.value.account_id == "a"
        assert caught.value.limit == 10 and caught.value.used == 6
    finally:
        log.close()


def test_reserve_is_idempotent_per_attempt(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10})
        first = ledger.reserve("a", attempt_id="t1", need={"tokens": 6})
        again = ledger.reserve("a", attempt_id="t1", need={"tokens": 6})
        assert again.reservation_id == first.reservation_id
        # 重复预占没有重复扣：仍能再预占 4，不能预占 5。
        ledger.reserve("a", attempt_id="t2", need={"tokens": 4})
        with pytest.raises(BudgetRefused):
            ledger.reserve("a", attempt_id="t3", need={"tokens": 5})
    finally:
        log.close()


def test_settlement_is_idempotent(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 100})
        reservation = ledger.reserve("a", attempt_id="t1", need={"tokens": 40})
        first = ledger.settle(reservation.reservation_id, usage={"tokens": 30})
        second = ledger.settle(reservation.reservation_id, usage={"tokens": 30})
        assert first.state == SETTLED and not first.duplicate
        assert second.duplicate and second.state == SETTLED
        usage = ledger.usage("a")
        assert usage["settled"]["tokens"] == 30
        assert usage["reserved"]["tokens"] == 0
    finally:
        log.close()


def test_real_usage_above_estimate_is_recorded_not_clipped(tmp_path):
    """provider 的 usage 超过估算时照实记——账本要反映真实消费。"""
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10})
        reservation = ledger.reserve("a", attempt_id="t1", need={"tokens": 2})
        ledger.settle(reservation.reservation_id, usage={"tokens": 9})
        assert ledger.usage("a")["settled"]["tokens"] == 9
        with pytest.raises(BudgetRefused):
            ledger.reserve("a", attempt_id="t2", need={"tokens": 2})
    finally:
        log.close()


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("value", [-1, True, 1.5, "3"])
def test_settlement_rejects_invalid_usage_without_changing_accounts(
    tmp_path, dimension, value,
):
    """结算入口也要守非负整数，不依赖某个 adapter 恰好先校验过。"""
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("root", limits={dimension: 10})
        ledger.create_account("child", limits={dimension: 10}, parent_id="root")
        reservation = ledger.reserve(
            "child", attempt_id="t1", need={dimension: 6},
        )
        before = {account: ledger.usage(account) for account in ("root", "child")}
        with pytest.raises(TianshuError, match="非负整数"):
            ledger.settle(reservation.reservation_id, usage={dimension: value})
        assert ledger.get(reservation.reservation_id).state == PENDING
        assert {account: ledger.usage(account) for account in before} == before
        # 被拒的回执不能破坏随后合法结算的机会。
        ledger.settle(reservation.reservation_id, usage={dimension: 4})
        for account in before:
            assert ledger.usage(account)["settled"][dimension] == 4
            assert ledger.usage(account)["reserved"][dimension] == 0
    finally:
        log.close()


# ---------------------------------------------------------------- 2 未知 / 部分


def test_unknown_settlement_keeps_occupation(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10, "requests": 5})
        reservation = ledger.reserve("a", attempt_id="t1", need={"tokens": 6})
        result = ledger.settle(reservation.reservation_id, known=False)
        assert result.state == UNKNOWN
        usage = ledger.usage("a")
        assert usage["settled"].get("tokens", 0) == 0
        assert usage["reserved"]["tokens"] == 6, "未知不能按零结算，占用必须保留"
        assert usage["unknown_reservations"] == 1
        with pytest.raises(BudgetRefused):
            ledger.reserve("a", attempt_id="t2", need={"tokens": 6})
    finally:
        log.close()


def test_partial_settlement_keeps_only_unknown_dimensions(tmp_path):
    """token 已知、金额未知：token 结算，金额占用保留（状态 partial）。"""
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 100, "micros": 1000})
        reservation = ledger.reserve(
            "a", attempt_id="t1", need={"tokens": 40, "micros": 200}
        )
        result = ledger.settle(
            reservation.reservation_id,
            usage={"tokens": 30},
            known_dimensions={"tokens"},
        )
        assert result.state == PARTIAL
        usage = ledger.usage("a")
        assert usage["settled"]["tokens"] == 30
        assert usage["reserved"]["micros"] == 200
        assert usage["reserved"].get("tokens", 0) == 0
        assert usage["unknown_reservations"] == 1
    finally:
        log.close()


def test_abandon_unsent_releases_only_pending(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10})
        reservation = ledger.reserve("a", attempt_id="t1", need={"tokens": 8})
        assert ledger.abandon_unsent(reservation.reservation_id)
        assert ledger.usage("a")["reserved"]["tokens"] == 0
        assert ledger.get(reservation.reservation_id).state == RELEASED
        # 已结算的预占不能被"取消"退回来。
        second = ledger.reserve("a", attempt_id="t2", need={"tokens": 8})
        ledger.settle(second.reservation_id, usage={"tokens": 5})
        assert ledger.abandon_unsent(second.reservation_id) is False
        assert ledger.usage("a")["settled"]["tokens"] == 5
    finally:
        log.close()


# ---------------------------------------------------------------- 3 父子预算


def test_parent_limit_binds_child(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("root", limits={"tokens": 10})
        ledger.create_account("child", limits={"tokens": 100}, parent_id="root")
        ledger.reserve("child", attempt_id="t1", need={"tokens": 8})
        # 子账户自己还有 92，但根只剩 2。
        with pytest.raises(BudgetRefused) as caught:
            ledger.reserve("child", attempt_id="t2", need={"tokens": 5})
        assert caught.value.account_id == "root"
        # 结算只发生一次。父账户计数器**本身**就是子树聚合（预占沿链扣），
        # 所以 root 与 child 各记 8 是同一笔在两级的占用，不是重复计入。
        reservation = ledger.pending("child")[0]
        ledger.settle(reservation.reservation_id, usage={"tokens": 8})
        assert ledger.usage("root")["settled"]["tokens"] == 8
        assert ledger.usage("child")["settled"]["tokens"] == 8
    finally:
        log.close()


def test_missing_parent_is_rejected(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        with pytest.raises(TianshuError, match="父预算账户不存在"):
            ledger.create_account("child", parent_id="nope")
    finally:
        log.close()


def test_unknown_dimension_is_rejected(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        with pytest.raises(TianshuError, match="未知的预算维度"):
            ledger.create_account("a", limits={"dollars": 1})
        ledger.create_account("a", limits={"tokens": 10})
        with pytest.raises(TianshuError, match="未知的预算维度"):
            ledger.reserve("a", attempt_id="t1", need={"dollars": 1})
    finally:
        log.close()


# ---------------------------------------------------------------- 4 取消 / deadline


def test_cancel_blocks_new_reservations_and_keeps_pending(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10})
        reservation = ledger.reserve("a", attempt_id="t1", need={"tokens": 4})
        assert ledger.cancel("a") == 1
        with pytest.raises(BudgetCancelled):
            ledger.reserve("a", attempt_id="t2", need={"tokens": 1})
        # 取消不是退款：已发出的预占仍在账上，仍可结算。
        assert ledger.get(reservation.reservation_id).state == PENDING
        ledger.settle(reservation.reservation_id, usage={"tokens": 3})
        assert ledger.usage("a")["settled"]["tokens"] == 3
    finally:
        log.close()


def test_cancel_propagates_to_descendants(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("root", limits={"tokens": 100})
        ledger.create_account("child", limits={"tokens": 100}, parent_id="root")
        assert ledger.cancel("root") == 2
        with pytest.raises(BudgetCancelled):
            ledger.reserve("child", attempt_id="t1", need={"tokens": 1})
    finally:
        log.close()


def test_deadline_refuses_new_reservations(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 10}, deadline_ms=1)
        with pytest.raises(BudgetRefused) as caught:
            ledger.reserve("a", attempt_id="t1", need={"tokens": 1})
        assert caught.value.dimension == "deadline"
    finally:
        log.close()


# ---------------------------------------------------------------- 5 恢复核对


def test_reconcile_reports_pending_and_unknown_without_zeroing(tmp_path):
    log, ledger = _ledger(tmp_path)
    try:
        ledger.create_account("a", limits={"tokens": 100})
        pending = ledger.reserve("a", attempt_id="t1", need={"tokens": 10})
        unknown = ledger.reserve("a", attempt_id="t2", need={"tokens": 20})
        ledger.settle(unknown.reservation_id, known=False)
        rows = ledger.reconcile("a")
        assert {row["state"] for row in rows} == {PENDING, UNKNOWN}
        assert {row["reservation_id"] for row in rows} == {
            pending.reservation_id, unknown.reservation_id,
        }
        # 不清零：占用还是 30。
        assert ledger.usage("a")["reserved"]["tokens"] == 30
    finally:
        log.close()


# ---------------------------------------------------------------- 6 并发


def test_concurrent_last_unit_is_admitted_once(tmp_path):
    """真并发抢最后一份额度：只放行一次（两条独立连接 = 两个进程的等价物）。"""
    db_path = tmp_path / "events.db"
    seed_log, seed = _ledger(tmp_path)
    seed.create_account("a", limits={"tokens": 10})
    seed_log.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(attempt: str) -> None:
        log = EventLog(db_path)
        try:
            ledger = BudgetLedger(log)
            barrier.wait(timeout=5)
            try:
                ledger.reserve("a", attempt_id=attempt, need={"tokens": 6})
                results.append(f"{attempt}:ok")
            except BudgetRefused:
                results.append(f"{attempt}:refused")
        finally:
            log.close()

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results) == ["t1:ok", "t2:refused"] or sorted(results) == [
        "t1:refused", "t2:ok",
    ], results

    verify_log = EventLog(db_path)
    try:
        usage = BudgetLedger(verify_log).usage("a")
        assert usage["reserved"]["tokens"] == 6, "只能扣一份"
    finally:
        verify_log.close()


# ---------------------------------------------------------------- 7 估算


def test_estimate_is_documented_estimate_not_a_guarantee():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(1) == 1
    assert estimate_tokens(400) == 100
    need = estimate_need(input_chars=400, output_reserve=256)
    assert need == {"tokens": 100 + 25 + 256, "requests": 1}
    with pytest.raises(ValueError):
        estimate_need(input_chars=1, margin_ratio=2)


# ---------------------------------------------------------------- 8 接线：主循环


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_bytes("# 甲\n\n事件日志是唯一事实源。\n".encode())
    return ws


def test_budget_exhaustion_stops_before_any_model_call(tmp_path):
    """规则 5：预算耗尽只保存状态并通知，**不得再调用模型**。"""
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse(
            "end_turn", content="不该被调用",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log,
            limits=Limits(4, 20_000, budget_limits={"tokens": 1}),
        )
        assert model.calls == 0, "预占失败后一次模型调用都不能发生"
        assert result.outcome == "failed"
        assert "预算账本拒绝" in (result.error or ""), result.error
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["settled"].get("tokens", 0) == 0
        assert usage["reserved"].get("tokens", 0) == 0
    finally:
        log.close()


def test_run_settles_actual_usage_into_the_ledger(tmp_path):
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse(
            "tool_use",
            tool_calls=[ToolCall("c1", "read_text", {"path": "a.md"})],
            usage={"input_tokens": 40, "output_tokens": 10},
        ),
        ModelResponse(
            "end_turn", content="读完了。",
            usage={"input_tokens": 60, "output_tokens": 8},
        ),
    ])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log,
            limits=Limits(4, 20_000, budget_limits={"tokens": 5000, "requests": 10}),
        )
        assert result.tokens == 118
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["settled"]["tokens"] == 118
        assert usage["settled"]["requests"] == 2
        assert usage["reserved"] == {} or all(
            value == 0 for value in usage["reserved"].values()
        )
    finally:
        log.close()


def test_model_without_usage_leaves_unknown_occupation(tmp_path):
    """provider 不给 usage：tokens 维度记未知、占用不释放、绝不记 0。"""
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse("end_turn", content="ok", usage={}),
    ])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log,
            limits=Limits(4, 20_000, budget_limits={"tokens": 5000, "requests": 10}),
        )
        assert result.unknown_usage_calls == 1
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["settled"].get("tokens", 0) == 0
        assert usage["reserved"]["tokens"] > 0, "未知用量必须保留占用"
        assert usage["unknown_reservations"] == 1
    finally:
        log.close()


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("known_field", ["input_tokens", "output_tokens"])
def test_partial_provider_usage_keeps_token_occupation(tmp_path, vendor, known_field):
    """只报输入或输出都不代表总用量已知；保留预占并继续阻止超预算。"""
    raw_field = {
        "input_tokens": "prompt_tokens", "output_tokens": "completion_tokens",
    }[known_field] if vendor == "openai" else known_field
    transport = FakeTransport(_payload(vendor, usage={raw_field: 7}))
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        result = run(
            task="读一下", workspace_dir=_ws(tmp_path), out_name="",
            model=_model(vendor, transport), log=log,
            limits=Limits(4, 20_000, budget_limits={"tokens": 5000, "requests": 10}),
        )
        ledger = BudgetLedger(log)
        account = f"run:{result.run_id}"
        usage = ledger.usage(account)
        assert usage["reserved"]["tokens"] > 0, "缺一半的 token 用量不能释放预占"
        assert usage["settled"].get("tokens", 0) == 0
        assert usage["settled"]["requests"] == len(transport.calls) == 1
        assert usage["unknown_reservations"] == 1
        assert result.unknown_usage_calls == 1
        assert result.tokens == 7, "已知部分仍是可核对的下界，不丢失"
        with pytest.raises(BudgetRefused):
            ledger.reserve(account, attempt_id="next", need={"tokens": 5000})
    finally:
        log.close()


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("negative_field", ["input_tokens", "output_tokens"])
def test_negative_provider_usage_cannot_refund_run_budget(
    tmp_path, vendor, negative_field,
):
    raw = {"input_tokens": 11, "output_tokens": 7}
    raw[negative_field] = -100
    if vendor == "openai":
        raw = {"prompt_tokens": raw["input_tokens"],
               "completion_tokens": raw["output_tokens"]}
    transport = FakeTransport(_payload(vendor, usage=raw))
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        result = run(
            task="读一下", workspace_dir=_ws(tmp_path), out_name="",
            model=_model(vendor, transport), log=log,
            limits=Limits(4, 20_000, budget_limits={"tokens": 5000, "requests": 10}),
        )
        assert result.outcome == "failed"
        errors = [event.payload for event in log.read_run(result.run_id)
                  if event.type == "model.error"]
        assert errors and "kind=protocol" in errors[-1]["error"]
        assert errors[-1]["retryable"] is False
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["reserved"]["tokens"] > 0
        assert usage["settled"].get("tokens", 0) == result.tokens == 0
        assert usage["settled"]["requests"] == len(transport.calls) == 1
    finally:
        log.close()


# ---------------------------------------------------------------- 9 接线：控制面取消


def test_control_plane_cancel_propagates_to_the_ledger(tmp_path):
    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log, data_root=tmp_path)
    try:
        plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=3))
        ws = _ws(tmp_path)
        task = plane.handle(
            "task.create",
            {"goal": "读", "workspaces": [{"root_path": str(ws), "mode": "readonly"}]},
        )["task"]
        run_view = plane.handle(
            "run.start", {"task_id": task["task_id"], "out_name": "o.md"}
        )["run"]
        run_id = run_view["run_id"]
        ledger = stack.budget
        ledger.create_account(f"run:{run_id}", limits={"tokens": 100})
        plane.handle("run.cancel", {"run_id": run_id})
        assert ledger.is_cancelled(f"run:{run_id}")
        with pytest.raises(BudgetCancelled):
            ledger.reserve(f"run:{run_id}", attempt_id="t1", need={"tokens": 1})
    finally:
        log.close()


# ---------------------------------------------------------------- 10 记账默认打开


def test_ledger_default_is_accounting_without_gates(tmp_path):
    """默认路径：账户照建、预占照记、结算照做，但**不设闸**。

    PRD §5.6 第 4 条（尝试过但全失败不得显示没有模型调用）与第 7 条（恢复先核对
    未决预占）要成立，前提是账本在默认路径上就有记录。所以默认不再是"不启用"。
    """
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse(
            "end_turn", content="ok", usage={"input_tokens": 40, "output_tokens": 10}
        ),
    ])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log,
        )  # 不传 budget_limits
        assert result.outcome == "noop"
        usage = BudgetLedger(log).usage(f"run:{result.run_id}")
        assert usage["limits"] == {}, "记账模式不设任何闸"
        assert usage["settled"]["tokens"] == 50, "默认路径必须真的记账"
        assert usage["settled"]["requests"] == 1
        assert all(value == 0 for value in usage["reserved"].values())
        assert usage["bound"] == "estimate"
        assert usage["overrun"] is False
    finally:
        log.close()


def test_accounting_mode_never_refuses_even_an_absurd_need(tmp_path):
    """没有上限就没有闸：这不是"额度过大所以放过"，是"这条路径不看额度"。"""
    log, ledger = _ledger(tmp_path)
    ledger.create_account("a")  # 无上限
    reservation = ledger.reserve("a", attempt_id="x", need={"tokens": 10**9})
    assert reservation.need == {"tokens": 10**9}
    assert ledger.usage("a")["limits"] == {}


def test_cancel_before_any_reservation_still_blocks_new_requests(tmp_path):
    """取消发生在第一次预占之前：意图必须保留（否则之后新建的账户是没取消的）。"""
    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log, data_root=tmp_path)
    try:
        plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=3))
        ws = _ws(tmp_path)
        task = plane.handle(
            "task.create",
            {"goal": "读", "workspaces": [{"root_path": str(ws), "mode": "readonly"}]},
        )["task"]
        run_view = plane.handle(
            "run.start", {"task_id": task["task_id"], "out_name": "o.md"}
        )["run"]
        run_id = run_view["run_id"]
        ledger = stack.budget
        assert ledger.account(f"run:{run_id}") is None, "还没有任何预占"
        plane.handle("run.cancel", {"run_id": run_id})
        assert ledger.is_cancelled(f"run:{run_id}")
        with pytest.raises(BudgetCancelled):
            ledger.reserve(f"run:{run_id}", attempt_id="t1", need={"tokens": 1})
    finally:
        log.close()


# ---------------------------------------------------------------- 11 可验证上界


def test_upper_bound_is_utf8_bytes_not_quarter_chars():
    """上界与估算**方向不同**：CJK 下 4 字符≈1 token 会低估，字节数不会。"""
    text = "中" * 100
    assert upper_bound_tokens(text) == 300, "UTF-8 下每个汉字 3 字节"
    assert estimate_tokens(len(text)) == 25
    assert upper_bound_tokens(text) > 4 * estimate_tokens(len(text))
    assert upper_need(input_bytes=300, output_bound=64) == {
        "tokens": 364,
        "requests": 1,
    }
    with pytest.raises(TianshuError):
        upper_need(input_bytes=300, output_bound=0)
    with pytest.raises(TianshuError):
        upper_need(input_bytes=-1, output_bound=64)


def test_estimate_overrun_is_recorded_but_does_not_block(tmp_path):
    """估算模式：结算超过预占**如实记账**（不夹回），但不据此拒发。"""
    log, ledger = _ledger(tmp_path)
    ledger.create_account("a", limits={"tokens": 100}, bound="estimate")
    reservation = ledger.reserve("a", attempt_id="x", need={"tokens": 10})
    settlement = ledger.settle(
        reservation.reservation_id, usage={"tokens": 50}, known_dimensions={"tokens"}
    )
    assert settlement.overrun == ("tokens",), "真实用量超过预占必须被记下来"
    usage = ledger.usage("a")
    assert usage["settled"]["tokens"] == 50, "照实记，不夹到预占值上"
    assert usage["overrun"] is True
    assert usage["bound"] == "estimate"
    # 估算口径下 overrun 不阻断：余额 100-50=50 仍然够下一次 5。
    ledger.reserve("a", attempt_id="y", need={"tokens": 5})


def test_upper_overrun_blocks_further_reservations(tmp_path):
    """上界模式：越界 = 这条保证已经不成立，此后一律拒发。"""
    log, ledger = _ledger(tmp_path)
    ledger.create_account("a", limits={"tokens": 1000}, bound=BOUND_UPPER)
    reservation = ledger.reserve("a", attempt_id="x", need={"tokens": 10})
    ledger.settle(
        reservation.reservation_id, usage={"tokens": 50}, known_dimensions={"tokens"}
    )
    usage = ledger.usage("a")
    assert usage["overrun"] is True
    assert usage["bound"] == BOUND_UPPER
    with pytest.raises(BudgetRefused) as excinfo:
        ledger.reserve("a", attempt_id="y", need={"tokens": 1})
    assert excinfo.value.dimension == "overrun"
    assert "上界" in str(excinfo.value)


def test_unknown_bound_is_rejected(tmp_path):
    log, ledger = _ledger(tmp_path)
    with pytest.raises(TianshuError, match="未知的预占口径"):
        ledger.create_account("a", bound="hard")


class _BoundScriptedModel(ScriptedModel):
    """脚本模型 + 一条真实的输出上界声明（`upper` 模式只认能力声明）。"""

    def __init__(self, responses, *, output_tokens: int) -> None:
        super().__init__(responses)
        self._output_tokens = output_tokens

    def capability_profile(self):
        return replace(
            super().capability_profile(), output_tokens=self._output_tokens
        )


def test_upper_mode_refuses_to_start_without_a_declared_output_bound(tmp_path):
    """拿不到输出上界 ⇒ 不进入上界模式，而且一次模型调用都不发生。"""
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([ModelResponse("end_turn", content="不该被调用")])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log,
            limits=Limits(
                4, 20_000, budget_limits={"tokens": 100_000}, budget_bound="upper"
            ),
        )
        assert model.calls == 0
        assert result.outcome == "failed"
        assert "预算配置拒绝" in (result.error or ""), result.error
        assert "output_tokens" in (result.error or "")
    finally:
        log.close()


def test_upper_mode_bad_value_is_refused_at_construction(tmp_path):
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        with pytest.raises(TianshuError, match="未知的预占口径"):
            run(
                task="读一下", workspace_dir=ws, out_name="out.md",
                model=ScriptedModel([]), log=log,
                limits=Limits(4, 20_000, budget_bound="hard"),
            )
    finally:
        log.close()


def test_upper_mode_reserves_bytes_plus_declared_output(tmp_path):
    """`upper` 口径下的预占 = 请求体 UTF-8 字节 + 声明的输出上界。

    用"provider 不给 usage"的响应把预占留在未决状态，好把 need 读出来核对；
    同一场景再跑一遍 `estimate` 口径作对照——两条路必须给出**不同量级**的数，
    否则说明上界模式其实还在用估算。
    """
    ws = _ws(tmp_path)
    goal = "中" * 200

    def _needs(limits: Limits, name: str) -> dict[str, int]:
        log = EventLog(tmp_path / name, blob_dir=tmp_path / f"{name}-blobs")
        model = _BoundScriptedModel(
            [ModelResponse("end_turn", content="ok", usage={})], output_tokens=64
        )
        try:
            result = run(
                task=goal, workspace_dir=ws, out_name="out.md",
                model=model, log=log, limits=limits,
            )
            rows = BudgetLedger(log).reconcile(f"run:{result.run_id}")
            assert len(rows) == 1, "无 usage ⇒ 一张未决预占"
            return rows[0]["need"]
        finally:
            log.close()

    limits = {"tokens": 100_000}
    upper = _needs(Limits(4, 20_000, budget_limits=limits, budget_bound="upper"), "u.db")
    estimate = _needs(Limits(4, 20_000, budget_limits=limits), "e.db")

    # 200 个汉字 → ASCII 转义后每个 6 字节 ⇒ 仅任务文本就 ≥ 1200 字节上界。
    assert upper["tokens"] >= 64 + 6 * len(goal), upper
    assert estimate["tokens"] * 3 < upper["tokens"], (estimate, upper)
    assert upper["requests"] == estimate["requests"] == 1
