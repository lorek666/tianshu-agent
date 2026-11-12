"""G2 子代理 D1：status / result / cancel 的查询与取消口径。

判据（规格 §3 D1-c7）+ §2.3 门 6 + §2.6 的 D1 部分：

- 未知 slug 的 status / result / cancel → `dispatch.subtask_not_found`：
  不用空结果或"已完成"冒充成功。
- status 同时给出**子任务状态**与**子 Run 状态**，两者不互相冒充。
- result 未完成 → `done:false`（不是失败、不是空结果）。
- cancel 只记录请求（`cancelling` + 子作业 `cancel_requested`），**不写**
  `cancelled`；已完成 → 幂等返回当前状态，不假装撤销。

为什么把"不存在"单独立判据：`subtask.status` 是纯读，很容易被顺手实现成
"查不到就返回 `{}`"。那会让模型把"我没派过"读成"它还没开始"，再据此编造
进度——这正是 D1 要钉死的形状。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from support import engine_for, stack_for

from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.tasks import RUN_RUNNING

# ---------------------------------------------------------------- 夹具


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    return log, stack_for(log, data_root=tmp_path / "data")


def _task(stack, *, max_subtasks: int = 2):
    return stack.tasks.create_task("父任务", max_subtasks=max_subtasks)


def _parent_run(stack, task):
    run = stack.tasks.start_run(task.task_id)
    job = stack.claims.enqueue(run.run_id)
    claim = stack.claims.claim(job.job_id)
    stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    return run, claim


def _call(
    stack,
    task,
    run,
    claim,
    tool_id: str,
    subtask_id: str,
    *,
    call_id: str | None = None,
    **extra,
):
    """准备 + 派发一次子任务调用（spawn 自动批准，其它工具本来就自动）。"""
    from tianshu.permissions import HumanIdentity

    args = {"subtask_id": subtask_id}
    args.update(extra)
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        # 同一个 call_id 是"这次调用"的身份：重复取消要用新的 call_id，
        # 否则会被派发层当作"同一次调用的重复派发"拒绝。
        call_id=call_id or f"{tool_id}-{subtask_id}",
        tool_id=tool_id,
        target=f"subtask://{subtask_id}",
        args=args,
    )
    decision = stack.dispatcher.prepare(request, claim, args=args)
    if decision.is_waiting:
        stack.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
    result = None if decision.is_rejected else stack.dispatcher.dispatch(
        decision.request_id, claim
    )
    return decision, result


def _spawn(stack, task, run, claim, subtask_id: str = "reader", **extra):
    extra.setdefault("goal", f"调查 {subtask_id}")
    return _call(stack, task, run, claim, "subtask.spawn", subtask_id, **extra)


def _results(log: EventLog, run_id: str) -> dict[str, dict]:
    return {
        event.payload["call_id"]: event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == "tool.result"
    }


def _mark_finished(log: EventLog, parent_run_id: str, *, state="succeeded",
                   summary="读了两份文档，结论如下。", result_ref="content://sha256:" + "ab" * 32):
    """模拟 D2 收尾写下的结果（D1 自己没有执行器，走不到这些状态）。"""
    log.conn.execute(
        "UPDATE subtasks SET state=?, summary=?, result_ref=?, ended_at=?"
        " WHERE parent_run_id=?",
        (state, summary, result_ref, int(time.time() * 1000), parent_run_id),
    )
    log.conn.commit()


# ---------------------------------------------------------------- D1-c7


@pytest.mark.parametrize("tool_id", ["subtask.status", "subtask.result", "subtask.cancel"])
def test_unknown_subtask_is_explicit(tmp_path, tool_id):
    """未知 slug：三个工具都明确 `dispatch.subtask_not_found`，不是空结果。

    三个都测，是因为它们的"失败方式"最容易各不相同——status 可能返回 `{}`，
    result 可能返回 `done:false`，cancel 可能静默成功。三种都是在撒谎。
    """
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        decision, result = _call(stack, task, run, claim, tool_id, "never-spawned")
        assert decision.is_rejected
        assert decision.rule_id == "dispatch.subtask_not_found"
        assert result is None
        # 拒绝也要落事件（用户可见的 rule_id 不能只活在返回值里）。
        rejected = [
            event.payload
            for event in log.read_run(run.run_id, verify=False)
            if event.type == "dispatch.rejected"
        ]
        assert rejected and rejected[-1]["rule_id"] == "dispatch.subtask_not_found"
        assert stack.tasks.list_subtasks(run.run_id) == []
    finally:
        log.close()


# ---------------------------------------------------------------- status


def test_status_reports_both_state_machines(tmp_path):
    """status 同时给子任务状态与子 Run 状态：queued ≠ created，不互相冒充。

    "子任务排队中"与"子 Run 还没开始"是两个维度。只给一个会让模型以为
    "子任务已经在跑了"或"子 Run 已经成功了"。
    """
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        decision, spawned = _spawn(stack, task, run, claim)
        assert decision.is_admitted and spawned is not None
        (row,) = stack.tasks.list_subtasks(run.run_id)

        status_decision, status = _call(
            stack, task, run, claim, "subtask.status", "reader"
        )
        assert status_decision.is_admitted
        assert status_decision.kind == "admitted"
        assert status is not None and status.status == "succeeded"
        output = status.output
        assert output["subtask_id"] == "reader"
        assert output["state"] == "queued", "子任务状态机"
        assert output["child_run_state"] == "created", "子 Run 状态机"
        assert output["child_run_id"] == row.child_run_id
        assert output["job_id"] == row.job_id
        assert output["limits"] == row.limits
        assert output["elapsed_ms"] >= 0
        # 纯读不经审批：dispatch 判定是 auto（rule c4.auto）。
        requested = [
            event.payload
            for event in log.read_run(run.run_id, verify=False)
            if event.type == "dispatch.requested"
            and event.payload.get("action") == "read"
        ]
        assert requested and requested[-1]["decision"] == "auto"
        assert requested[-1]["rule_id"] == "c4.auto"
        assert stack.approvals.pending(run_id=run.run_id) == []
    finally:
        log.close()


def test_result_is_not_a_fake_failure_when_pending(tmp_path):
    """未完成 → `done:false`，状态是 succeeded（"还没有"不是"失败"）。"""
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        assert _spawn(stack, task, run, claim)[0].is_admitted
        decision, result = _call(stack, task, run, claim, "subtask.result", "reader")
        assert decision.is_admitted
        assert result is not None
        assert result.status == "succeeded"
        assert result.effect_state == "none_applied"
        assert result.output == {
            "subtask_id": "reader",
            "state": "queued",
            "done": False,
        }
    finally:
        log.close()


def test_result_reports_bounded_summary_when_finished(tmp_path):
    """完成时（D2 写下的行）→ `done:true` + 摘要 + 内容引用，不重跑子任务。"""
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        assert _spawn(stack, task, run, claim)[0].is_admitted
        _mark_finished(log, run.run_id)
        decision, result = _call(stack, task, run, claim, "subtask.result", "reader")
        assert decision.is_admitted
        assert result is not None and result.status == "succeeded"
        output = result.output
        assert output["done"] is True
        assert output["state"] == "succeeded"
        assert output["summary"].startswith("读了两份文档")
        assert output["result_ref"].startswith("content://sha256:")
        # 读结果不产生执行副作用：没有新的子 Run、没有唤醒作业。
        (row,) = stack.tasks.list_subtasks(run.run_id)
        assert stack.claims.get(row.job_id).state == "pending"
    finally:
        log.close()


# ---------------------------------------------------------------- cancel 的 D1 口径


def test_cancel_records_request_and_does_not_finish(tmp_path):
    """cancel：状态 `cancelling` + 子作业 `cancel_requested=1`，**不写** `cancelled`。

    D1 没有执行器，"真正停下"只能在 D2 的安全点确认。所以这里最多记录
    "人要求停"；写 `cancelled` 是在假装撤销已经完成，而子 Run 可能根本还没跑。
    """
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        assert _spawn(stack, task, run, claim)[0].is_admitted
        (row,) = stack.tasks.list_subtasks(run.run_id)
        assert stack.claims.get(row.job_id).cancel_requested is False

        decision, result = _call(stack, task, run, claim, "subtask.cancel", "reader")
        assert decision.is_admitted, "cancel 自动放行（只减少执行、不扩权）"
        assert result is not None and result.status == "succeeded"
        assert result.effect_state == "none_applied"
        assert result.output["state"] == "cancelling"
        assert result.output["cancel_requested"] is True
        assert result.output["already_finished"] is False
        (row,) = stack.tasks.list_subtasks(run.run_id)
        assert row.state == "cancelling"
        assert row.ended_at is None, "没有收尾：结束时间只能由执行侧写"
        job = stack.claims.get(row.job_id)
        assert job.state == "pending", "D1 不动作业状态机"
        assert job.cancel_requested is True
        # 子 Run 也没有被改写。
        assert stack.tasks.get_run(row.child_run_id).state == "created"
    finally:
        log.close()


def test_cancel_is_idempotent_for_finished_subtask(tmp_path):
    """已完成 → 幂等返回当前状态：不报错，也不假装撤销。"""
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        assert _spawn(stack, task, run, claim)[0].is_admitted
        _mark_finished(log, run.run_id)
        (row,) = stack.tasks.list_subtasks(run.run_id)
        before = (row.state, row.ended_at)

        decision, result = _call(stack, task, run, claim, "subtask.cancel", "reader")
        assert decision.is_admitted
        assert result is not None and result.status == "succeeded"
        assert result.output["state"] == "succeeded"
        assert result.output["already_finished"] is True
        assert result.output["cancel_requested"] is False
        (row,) = stack.tasks.list_subtasks(run.run_id)
        assert (row.state, row.ended_at) == before, "幂等：不覆盖终态与结束时间"
    finally:
        log.close()


def test_cancel_is_idempotent_while_cancelling(tmp_path):
    """已经是 `cancelling`：重复取消不报错、不重复推进。"""
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack)
        run, claim = _parent_run(stack, task)
        assert _spawn(stack, task, run, claim)[0].is_admitted
        first, _ = _call(stack, task, run, claim, "subtask.cancel", "reader")
        second, second_result = _call(
            stack, task, run, claim, "subtask.cancel", "reader", call_id="cancel-2"
        )
        assert first.is_admitted and second.is_admitted
        assert second_result is not None and second_result.status == "succeeded"
        assert second_result.output["state"] == "cancelling"
        assert second_result.output["already_finished"] is False
    finally:
        log.close()


# ---------------------------------------------------------------- 端到端


def test_subtask_reads_go_through_dispatch_without_approval(tmp_path):
    """端到端：模型调 status/result，走的是派发（有 tool.intent/result），但不弹批准。

    为什么"纯读也要走派发"：父任务身份与"子任务是否存在"只有派发层能判。
    这条同时钉住"没有因此变成需要批准"——读本地状态不该打扰人。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("c1", "subtask.spawn", {"subtask_id": "r", "goal": "读文档"})
                    ],
                ),
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c2", "subtask.status", {"subtask_id": "r"})],
                ),
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c3", "subtask.result", {"subtask_id": "r"})],
                ),
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c4", "subtask.cancel", {"subtask_id": "r"})],
                ),
                ModelResponse("end_turn", content="查完了。"),
            ]
        )
        engine = engine_for(stack, model)
        result = engine.start(task.task_id, out_name="out.md")
        from support import approve_pending

        rounds = 0
        while result.state == "waiting_approval" and rounds < 4:
            rounds += 1
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)
        assert result.state in ("succeeded", "noop"), result

        results = _results(log, result.run_id)
        assert results["c2"]["status"] == "succeeded"
        assert results["c2"]["output"]["state"] == "cancelling" or (
            results["c2"]["output"]["state"] == "queued"
        )
        assert results["c3"]["status"] == "succeeded"
        assert results["c3"]["output"]["done"] is False
        assert results["c4"]["output"]["state"] == "cancelling"
        # 只有 spawn 产生过审批；status/result/cancel 都没有打扰人。
        approvals = [
            event.payload
            for event in log.read_run(result.run_id, verify=False)
            if event.type == "approval.requested"
        ]
        assert len(approvals) == 1
        assert approvals[0]["rule_id"] == "c4.subtask_spawn"
        (row,) = stack.tasks.list_subtasks(result.run_id)
        assert row.state == "cancelling"
        assert stack.claims.get(row.job_id).cancel_requested is True
    finally:
        log.close()
