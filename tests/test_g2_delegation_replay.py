"""G2 子代理 D2：回放对子任务结果的形状校验（D2-c9）。

回放的三条硬边界（规格 §2.8）：
1. **绝不执行子任务**：只读事件，不认领作业、不调用模型；
2. **绝不唤醒作业 / 重算预算**：`job.requeued` 不会多一条，账本不变；
3. 形状不对要报 `problem`：`subtask.spawn` 缺身份、`subtask.result` 的
   `done:true` 缺 `content://` 引用或缺 `summary`。

形状校验只覆盖"缺了就无法事后核对"的字段，不冒充对结果内容的复核——子任务
到底读到了什么，仍在子 Run 自己的事件里。
"""

from __future__ import annotations

from pathlib import Path

from support import approve_pending, engine_for, stack_for

from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.replay import replay
from tianshu.worker import Worker


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    return log, stack_for(log, data_root=tmp_path / "data")


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.md").write_text("doc.md 内容\n", encoding="utf-8")
    return ws


def _run_whole_flow(stack, task):
    """父派子 → Worker 跑子任务 → 父读结果并收尾。返回 (parent_run_id, child_run_id)。"""
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        "spawn-1",
                        "subtask.spawn",
                        {"subtask_id": "reader", "goal": "读 doc.md 并总结"},
                    )
                ],
            ),
            ModelResponse("end_turn", content="等结果。"),
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall("result-1", "subtask.result", {"subtask_id": "reader"})
                ],
            ),
            ModelResponse("end_turn", content="汇总完成。"),
        ]
    )
    engine = engine_for(stack, model)
    result = engine.start(task.task_id, out_name="out.md")
    assert result.state == "waiting_approval"
    approve_pending(stack, result.run_id)
    result = engine.resume_run(result.run_id)
    assert result.state == "running"
    (row,) = stack.tasks.list_subtasks(result.run_id)
    worker = Worker(
        stack,
        model=ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("read-1", "read_text", {"path": "doc.md"})],
                ),
                ModelResponse("end_turn", content="doc.md 说：内容。"),
            ]
        ),
        heartbeat_interval_ms=None,
    )
    try:
        worker.drain(max_jobs=1)
    finally:
        worker.close()
    finished = engine.resume_run(result.run_id)
    assert finished.state == "succeeded", finished
    return result.run_id, row.child_run_id


def test_replay_subtask_shapes(tmp_path):
    """真实子任务事件能通过回放（包括子 Run 自己的回放）。"""
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        parent_run_id, child_run_id = _run_whole_flow(stack, task)
        parent_report = replay(log, parent_run_id)
        assert parent_report.ok, parent_report.problems
        # 子 Run 的 run.start 必须带 task（否则回放拿空字符串重建首条消息，
        # messages_hash 必红——这是 D2 接执行后才暴露的回放缺口）。
        child_report = replay(log, child_run_id)
        assert child_report.ok, child_report.problems
        assert child_report.turns == 2
    finally:
        log.close()


def test_replay_flags_missing_subtask_identity(tmp_path):
    """`subtask.spawn` 结果缺身份字段 → problem（不是静默通过）。"""
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        parent_run_id, _ = _run_whole_flow(stack, task)
        log.append(
            parent_run_id,
            "tool.result",
            {
                "call_id": "forged-spawn",
                "tool_id": "subtask.spawn",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {"subtask_id": "ghost"},
            },
            task_id=task.task_id,
        )
        report = replay(log, parent_run_id)
        assert not report.ok
        text = "\n".join(report.problems)
        assert "subtask.spawn 结果缺 child_run_id" in text
        assert "subtask.spawn 结果缺 job_id" in text
    finally:
        log.close()


def test_replay_flags_done_without_ref_or_summary(tmp_path):
    """`subtask.result` 的 `done:true` 缺引用 / 缺 summary → problem；空摘要合法。"""
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        parent_run_id, _ = _run_whole_flow(stack, task)
        log.append(
            parent_run_id,
            "tool.result",
            {
                "call_id": "forged-done",
                "tool_id": "subtask.result",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {"subtask_id": "ghost", "state": "succeeded", "done": True},
            },
            task_id=task.task_id,
        )
        report = replay(log, parent_run_id)
        text = "\n".join(report.problems)
        assert "缺合法 content:// 引用" in text
        assert "缺 summary" in text

        # 空摘要 + 合法引用 = 合法形状：子任务确实可能没有输出，不能因为空串判红。
        log.append(
            parent_run_id,
            "tool.result",
            {
                "call_id": "forged-empty-summary",
                "tool_id": "subtask.result",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {
                    "subtask_id": "ghost",
                    "state": "succeeded",
                    "done": True,
                    "summary": "",
                    "result_ref": "content://sha256:" + "0" * 64,
                },
            },
            task_id=task.task_id,
        )
        report = replay(log, parent_run_id)
        assert not any("forged-empty-summary" in p for p in report.problems)
    finally:
        log.close()


def test_replay_does_not_execute_or_wake_children(tmp_path):
    """回放停在"等子任务"的父 Run：不认领、不唤醒、不重算预算。"""
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "spawn-1",
                            "subtask.spawn",
                            {"subtask_id": "reader", "goal": "读 doc.md 并总结"},
                        )
                    ],
                ),
                ModelResponse("end_turn", content="等结果。"),
            ]
        )
        engine = engine_for(stack, model)
        result = engine.start(task.task_id, out_name="out.md")
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
        assert result.state == "running"
        (row,) = stack.tasks.list_subtasks(result.run_id)
        before = {
            "model_calls": model.calls,
            "child_run": stack.tasks.get_run(row.child_run_id).state,
            "child_job": stack.claims.get(row.job_id).state,
            "parent_job": stack.claims.job_for_run(result.run_id).state,
            "subtask": stack.tasks.get_subtask(result.run_id, "reader").to_dict(),
            "budget": stack.budget.usage(f"run:{result.run_id}"),
            "requeued": len(
                [
                    event
                    for event in log.read_run(result.run_id, verify=False)
                    if event.type == "job.requeued"
                ]
            ),
        }

        report = replay(log, result.run_id)
        assert report.ok, report.problems

        after = {
            "model_calls": model.calls,
            "child_run": stack.tasks.get_run(row.child_run_id).state,
            "child_job": stack.claims.get(row.job_id).state,
            "parent_job": stack.claims.job_for_run(result.run_id).state,
            "subtask": stack.tasks.get_subtask(result.run_id, "reader").to_dict(),
            "budget": stack.budget.usage(f"run:{result.run_id}"),
            "requeued": len(
                [
                    event
                    for event in log.read_run(result.run_id, verify=False)
                    if event.type == "job.requeued"
                ]
            ),
        }
        assert after == before, "回放只读：不得执行/唤醒/重算"
    finally:
        log.close()
