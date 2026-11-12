"""G2 子代理 D2：回放对子任务结果的形状校验（D2-c9）。

回放的三条硬边界（规格 §2.8）：
1. **绝不执行子任务**：只读事件，不认领作业、不调用模型；
2. **绝不唤醒作业 / 重算预算**：`job.requeued` 不会多一条，账本不变；
3. 形状不对要报 `problem`：`subtask.spawn` 缺身份；`subtask.result` 的
   `done:true` 缺 `summary` 键、**有正文**却缺合法 `content://` 引用、或
   `result_ref` 存在但不合法。空摘要 + 无引用是合法形状（没有正文就没有引用）。

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


def _results(log: EventLog, run_id: str) -> dict[str, dict]:
    return {
        event.payload["call_id"]: event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == "tool.result"
    }


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

        # 有正文却**没有**引用：这是真残缺（正文承诺了一份存不下来的内容）。
        # 这条钉住 `needs_ref` 里的"有正文才要求引用"——只判"缺 summary 键"是不够的。
        log.append(
            parent_run_id,
            "tool.result",
            {
                "call_id": "forged-body-no-ref",
                "tool_id": "subtask.result",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {
                    "subtask_id": "ghost",
                    "state": "succeeded",
                    "done": True,
                    "summary": "这是正文，但没有 content:// 引用",
                },
            },
            task_id=task.task_id,
        )
        problems = replay(log, parent_run_id).problems
        assert any(
            "forged-body-no-ref" in p and "缺合法 content:// 引用" in p
            for p in problems
        ), problems
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


# ---------------------------------------------------------------- 回归：合法空结果


def _parent_reads_after_child(tmp_path: Path, *, mode: str):
    """父派子 → 子以「无输出」或「取消」收尾 → 父读一次结果。

    两条路径都会产出 `done:true` 但 `summary=""`、`result_ref=None` 的
    **合法**记录——D2 的"拿不到输出就不编造"就是要求它长这样。回放若把这种
    形状判成 problem，审计工具就在自己的正常产出上误报。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
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
    approve_pending(stack, result.run_id)
    result = engine.resume_run(result.run_id)
    assert result.state == "running", result
    (row,) = stack.tasks.list_subtasks(result.run_id)

    if mode == "cancelled":
        # 取消一个从未执行过的子任务：行直接以 cancelled 收尾，没有正文。
        stack.tasks.request_subtask_cancel(result.run_id, "reader")
        worker = Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=None)
        try:
            worker.drain(max_jobs=1)
        finally:
            worker.close()
        assert stack.tasks.get_subtask(result.run_id, "reader").state == "cancelled"
    elif mode == "fenced":
        # fence 只改行、**不写摘要**（`_mark_subtask_fenced_locked`），所以
        # 行里的 `summary` 是 NULL —— 这是另一条"合法但 None"的路径。
        stack.tasks.fence_child_run(
            row.child_run_id, reason="父目标已变更（测试夹具）"
        )
        assert stack.tasks.get_subtask(result.run_id, "reader").state == "fenced"
    elif mode == "empty":
        # 子 Run 正常收尾，但最后一轮没有输出（content 为空串）。
        worker = Worker(
            stack,
            model=ScriptedModel([ModelResponse("end_turn", content="")]),
            heartbeat_interval_ms=None,
        )
        try:
            worker.drain(max_jobs=1)
        finally:
            worker.close()
        assert stack.tasks.get_subtask(result.run_id, "reader").state == "succeeded"

    finished = engine.resume_run(result.run_id)
    assert finished.state == "succeeded", finished
    out = _results(log, result.run_id)["result-1"]["output"]
    assert out["done"] is True
    assert out["summary"] == "", "没有正文就写空串（不是 None）"
    assert out["result_ref"] is None, "没有正文就没有 content:// 引用"
    return log, result.run_id, task.task_id


def test_replay_accepts_legit_empty_result(tmp_path):
    """无输出的 succeeded 子任务被父读一次 → 回放**不许**报 problem（回归 B1）。

    这是本次复审抓到的假红：`done:true` + 空摘要 + 无引用是 D2 的既定形状
    （`test_missing_child_output_is_not_fabricated` 就钉着它），而形状校验原来
    无条件要求 `content://` 引用，于是父 Run 只要读过一次结果，回放就红。
    """
    log, parent_run_id, _ = _parent_reads_after_child(tmp_path, mode="empty")
    try:
        report = replay(log, parent_run_id)
        assert report.ok, report.problems
    finally:
        log.close()


def test_replay_accepts_legit_cancelled_result(tmp_path):
    """已取消的子任务被父读一次 → 回放不报 problem；但**坏引用**仍要报。"""
    log, parent_run_id, task_id = _parent_reads_after_child(tmp_path, mode="cancelled")
    try:
        assert replay(log, parent_run_id).ok

        # 空摘要 + 一条**不合法**的 result_ref：这不是"没有正文"，是记录坏了。
        log.append(
            parent_run_id,
            "tool.result",
            {
                "call_id": "forged-bad-ref",
                "tool_id": "subtask.result",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {
                    "subtask_id": "ghost",
                    "state": "cancelled",
                    "done": True,
                    "summary": "",
                    "result_ref": "content://sha256:zzzz",
                },
            },
            task_id=task_id,
        )
        report = replay(log, parent_run_id)
        assert not report.ok
        assert any("result_ref 不是合法" in p for p in report.problems), report.problems
    finally:
        log.close()


def test_replay_accepts_fenced_result(tmp_path):
    """fenced 子任务的 `summary` 是 NULL（fence 只改行）→ 回放仍不报 problem。

    这条钉住派发层的归一：终态 `summary` 恒为字符串（NULL → ""）。否则
    `subtask.result` 会把 None 交给回放，合法的 fenced 行被判成
    "summary 不是字符串"。
    """
    log, parent_run_id, _ = _parent_reads_after_child(tmp_path, mode="fenced")
    try:
        report = replay(log, parent_run_id)
        assert report.ok, report.problems
    finally:
        log.close()
