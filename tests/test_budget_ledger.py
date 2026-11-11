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
from pathlib import Path

import pytest
from support import stack_for

from tianshu.budget import (
    PARTIAL,
    PENDING,
    RELEASED,
    SETTLED,
    UNKNOWN,
    BudgetLedger,
    estimate_need,
    estimate_tokens,
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
