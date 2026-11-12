"""G2 子代理 D1：崩溃恢复按**证据**补结果（D1-c10）。

规格 §2.3：`subtask.spawn` 的 intent 没有 result 时，按 `(run_id, slug)` 查
`subtasks` 表——

- 有行 → 补一条与当时**身份一致**的 `succeeded`（child_task / child_run / job
  从行里读，不重新生成、不猜）；
- 没有行 → 补 `unknown`：intent 已提交，建行可能就发生在崩溃窗口里，
  "没看到行"证明不了"没建"。

为什么不能套用文件写那条"清单里没有条目 → 确定没写"的推理：那条的前提是
"我知道这个工具的写入协议（先落写前证据、再写文件）"，而委派的证据在
`subtasks` 表里，不在快照清单里。用错证据 = 替一个没核实的副作用作证。

测试用真实派发路径制造崩溃窗口（把结果写入打炸 / 把派发打炸），而不是手写
半截状态：这样测到的才是产品真正会留下的形状。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import stack_for

from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.permissions import HumanIdentity
from tianshu.recovery import recover_run
from tianshu.tasks import RUN_RUNNING

# ---------------------------------------------------------------- 夹具


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    ws = tmp_path / "ws"
    ws.mkdir()
    return log, stack_for(log, data_root=tmp_path / "data"), ws


def _task(stack, ws: Path):
    return stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)


def _parent_run(stack, task):
    run = stack.tasks.start_run(task.task_id)
    job = stack.claims.enqueue(run.run_id)
    claim = stack.claims.claim(job.job_id)
    stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    return run, claim


def _admitted_spawn(stack, task, run, claim, subtask_id="crashed"):
    """准备 + 批准到 admitted，但不派发（调用方自己打炸某一步）。"""
    args = {"subtask_id": subtask_id, "goal": f"调查 {subtask_id}"}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=f"call-{subtask_id}",
        tool_id="subtask.spawn",
        target=f"subtask://{subtask_id}",
        args=args,
    )
    decision = stack.dispatcher.prepare(request, claim, args=args)
    assert decision.is_waiting
    stack.approvals.decide(
        decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )
    admitted = stack.dispatcher.prepare(request, claim, args=args)
    assert admitted.is_admitted, admitted
    return admitted.request_id


def _tool_results(log: EventLog, run_id: str) -> list[dict]:
    return [
        event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == "tool.result"
    ]


def _dispatch_state(log: EventLog, run_id: str, call_id: str) -> str:
    row = log.conn.execute(
        "SELECT state FROM dispatches WHERE run_id=? AND call_id=?",
        (run_id, call_id),
    ).fetchone()
    return row["state"] if row is not None else "<missing>"


def _crash_window(
    stack, request_id: str, claim, *, patch: str, message: str
) -> None:
    """在真实派发路径上打炸一个阶段，留下 intent 而没有 result。"""
    if patch == "after_spawn":
        # spawn 事务已提交（子任务行在），结果写入炸了：崩溃窗口的形状。
        def boom(*args, **kwargs):
            raise RuntimeError(message)

        original = stack.dispatcher._commit_result
        stack.dispatcher._commit_result = boom
        try:
            with pytest.raises(RuntimeError, match=message):
                stack.dispatcher.dispatch(request_id, claim)
        finally:
            stack.dispatcher._commit_result = original
        return
    # 派发（建子任务）本身炸了：intent 在，subtasks 表里没有行。
    original = stack.dispatcher._dispatch_subtask

    def boom(*args, **kwargs):
        raise RuntimeError(message)

    stack.dispatcher._dispatch_subtask = boom
    try:
        with pytest.raises(RuntimeError, match=message):
            stack.dispatcher.dispatch(request_id, claim)
    finally:
        stack.dispatcher._dispatch_subtask = original


# ---------------------------------------------------------------- D1-c10


def test_recover_spawn_is_evidence_based(tmp_path):
    """行存在 → 身份一致的 succeeded；行不存在 → unknown，且不阻断目录。

    同一份 fixture 里跑两种情况，是因为它们必须**只有**这一个判据不同：
    否则"补成功"与"补未知"可能来自别的差异（例如有没有 workspace、有没有快照）。
    """
    log, stack, ws = _stack(tmp_path)
    try:
        task = _task(stack, ws)
        run, claim = _parent_run(stack, task)
        request_id = _admitted_spawn(stack, task, run, claim, "crashed")
        _crash_window(
            stack, request_id, claim, patch="after_spawn", message="模拟崩溃：结果未落库"
        )
        (row,) = stack.tasks.list_subtasks(run.run_id)
        assert _tool_results(log, run.run_id) == [], "崩在结果之前：没有 tool.result"
        assert _dispatch_state(log, run.run_id, "call-crashed") == "intent"

        report = recover_run(
            log, run.run_id, ws, stack.snapshots, data_root=tmp_path / "data"
        )
        assert report.ok, report.problems
        assert report.none_applied == ["call-crashed"]
        assert report.subtask_unresolved == []
        assert report.blocked_workspaces == []
        (result,) = _tool_results(log, run.run_id)
        assert result["call_id"] == "call-crashed"
        assert result["status"] == "succeeded"
        assert result["effect_state"] == "none_applied"
        output = result["output"]
        assert output["recovered"] is True
        # 身份是**存下来的**：与行逐字一致，不是重新生成的 id。
        assert output["subtask_id"] == row.subtask_id
        assert output["child_task_id"] == row.child_task_id
        assert output["child_run_id"] == row.child_run_id
        assert output["job_id"] == row.job_id
        assert output["state"] == "queued"
        assert output["limits"] == row.limits
        # 派发记录不再是 intent：否则后来的 resume / 审计会看到"仍在执行"。
        assert _dispatch_state(log, run.run_id, "call-crashed") == "done"

        # 幂等：第二次恢复不重复写结果。
        before = len(log.read_run(run.run_id, verify=False))
        again = recover_run(
            log, run.run_id, ws, stack.snapshots, data_root=tmp_path / "data"
        )
        assert again.none_applied == []
        assert len(log.read_run(run.run_id, verify=False)) == before
    finally:
        log.close()


def test_recover_spawn_without_row_is_unknown(tmp_path):
    """行不存在 → `unknown`：不猜"没建"，也不把无关目录挡上。

    这条是"不知道"与"没发生"的分界线。把 intent 已提交当成"确定没建"，会让
    一个可能已经存在的子任务从账上消失；而拿它去阻断工作区，则是把一个
    没有目录副作用的动作说成"这个目录状态不明"——两种都是假话。
    """
    log, stack, ws = _stack(tmp_path)
    try:
        task = _task(stack, ws)
        run, claim = _parent_run(stack, task)
        request_id = _admitted_spawn(stack, task, run, claim, "ghost")
        _crash_window(
            stack, request_id, claim, patch="before_spawn", message="模拟崩溃：派发中断"
        )
        assert stack.tasks.list_subtasks(run.run_id) == []
        assert _tool_results(log, run.run_id) == []

        report = recover_run(
            log, run.run_id, ws, stack.snapshots, data_root=tmp_path / "data"
        )
        assert not report.ok, "无法核实的委派不能让报告变成 ok"
        assert report.subtask_unresolved == ["call-ghost"]
        assert report.unresolved == [], "委派没有目录 preimage：不进文件 unknown 桶"
        assert report.blocked_workspaces == [], "不该为一个 queued 子任务挡目录"
        assert report.none_applied == []
        (result,) = _tool_results(log, run.run_id)
        assert result["status"] == "unknown"
        assert result["effect_state"] == "unknown"
        assert result["output"]["recovered"] is True
        assert "可能已建也可能没建" in result["output"]["error"]
        assert _dispatch_state(log, run.run_id, "call-ghost") == "error"
        # 没有写入任何 recovery.blocked：目录仍然可写。
        blocked = [
            event
            for event in log.read_run(run.run_id, verify=False)
            if event.type == "recovery.blocked"
        ]
        assert blocked == []
    finally:
        log.close()


def test_recover_cancel_is_evidence_based(tmp_path):
    """`subtask.cancel` 的 intent 没有 result：行已是 cancelling → 请求确实记下了。

    与 spawn 分开，是因为它的证据不同：取消不改身份、也不建东西，只问
    "那个请求位写下去了没有"。写下去了就是成功（恢复后再取消是幂等的），
    没写下去就是确定没发生——两种都不该产生 unknown。
    """
    log, stack, ws = _stack(tmp_path)
    try:
        task = _task(stack, ws)
        run, claim = _parent_run(stack, task)
        # 先正常派一个子任务（这一步有 result，不参与恢复）。
        spawn_id = _admitted_spawn(stack, task, run, claim, "reader")
        stack.dispatcher.dispatch(spawn_id, claim)
        assert _tool_results(log, run.run_id)[0]["status"] == "succeeded"

        args = {"subtask_id": "reader"}
        request = build_request(
            task=task,
            run=run,
            claim=claim,
            call_id="call-cancel",
            tool_id="subtask.cancel",
            target="subtask://reader",
            args=args,
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.is_admitted
        _crash_window(
            stack,
            decision.request_id,
            claim,
            patch="after_spawn",
            message="模拟崩溃：取消结果未落库",
        )
        (row,) = stack.tasks.list_subtasks(run.run_id)
        assert row.state == "cancelling", "请求位已经写下（这一步在结果之前）"

        report = recover_run(
            log, run.run_id, ws, stack.snapshots, data_root=tmp_path / "data"
        )
        assert report.ok, report.problems
        results = {item["call_id"]: item for item in _tool_results(log, run.run_id)}
        recovered = results["call-cancel"]
        assert recovered["status"] == "succeeded"
        assert recovered["output"]["state"] == "cancelling"
        assert recovered["output"]["cancel_requested"] is True
        assert recovered["output"]["recovered"] is True
    finally:
        log.close()
