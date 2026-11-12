"""G2 子代理 D2：父目标变更 → 子任务 fence（D2-c6）。

规格 §2.6：子 Run 在**每次派发前**核验父 Run 的 `(goal_revision, lease_epoch)`
与 `subtasks` 行里记录的一致。不一致 → `dispatch.parent_fenced`，子任务置
`fenced`，结果不能回填父 Run。

两条路径都要钉住：
1. 子 Run 还要派发 → 派发前拒绝（编号是 `dispatch.parent_fenced`，不是文件
   或策略层别的编号）；
2. 子 Run 只做纯读、直接收尾 → 收尾事务里核验失败，同样不能回填（不写
   `subtask.finished`，也不写摘要/引用）。
"""

from __future__ import annotations

from pathlib import Path

from support import approve_pending, engine_for, stack_for

from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    return log, stack_for(log, data_root=tmp_path / "data")


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.md").write_text("doc.md 内容\n", encoding="utf-8")
    return ws


def _spawn_then_park(stack, task):
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
    assert result.state == "waiting_approval"
    approve_pending(stack, result.run_id)
    result = engine.resume_run(result.run_id)
    assert result.state == "running", result
    return engine, result


def _events(log: EventLog, run_id: str, type_name: str) -> list[dict]:
    return [
        event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == type_name
    ]


def _results(log: EventLog, run_id: str) -> dict[str, dict]:
    return {
        event.payload["call_id"]: event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == "tool.result"
    }


def test_parent_revision_change_fences_children(tmp_path):
    """父改目标 → 子 Run 派发前被拒 `dispatch.parent_fenced`，结果不回填。

    为什么 fence 核验必须排在资源解析前面：父目标已经改了，这次调用无论
    指向文件、记忆还是网络都不该再被当作旧目标下的一部分执行。测试让子
    Run 去写文件——如果 fence 检查排得靠后，这里会先撞上 `c4.readonly_binding`
    （子任务绑定是只读的），断言就抓不到真正的拒绝原因。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        assert row.parent_goal_revision == 1
        assert row.parent_lease_epoch == 1

        revised = stack.tasks.revise_goal(task.task_id, "换成新目标")
        assert (revised.goal_revision, revised.lease_epoch) == (2, 2)
        assert stack.tasks.get_run(parent_run_id).state == "fenced"
        assert stack.claims.job_for_run(parent_run_id).state == "cancelled"

        child_model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "write-1",
                            "create_report",
                            {
                                "name": "sub.md",
                                "content": "子任务不该写这个文件",
                            },
                        )
                    ],
                ),
                ModelResponse("end_turn", content="不该走到这里"),
            ]
        )
        child_engine = engine_for(stack, child_model)
        result = child_engine.resume_run(row.child_run_id)
        assert result.state == "cancelled", result
        assert child_model.calls == 1, "派发被拒后安全点确认取消，不再调模型"

        rejected = _results(log, row.child_run_id)["write-1"]
        assert rejected["status"] == "rejected"
        assert rejected["output"]["rule_id"] == "dispatch.parent_fenced"
        dispatch_rejected = [
            event.payload
            for event in log.read_run(row.child_run_id, verify=False)
            if event.type == "dispatch.rejected"
        ]
        assert dispatch_rejected[-1]["rule_id"] == "dispatch.parent_fenced"

        fenced = stack.tasks.get_subtask(parent_run_id, "reader")
        assert fenced.state == "fenced"
        assert fenced.error and (
            "父目标已变更" in fenced.error or "父 Run 已被 fence" in fenced.error
        )
        assert fenced.summary is None and fenced.result_ref is None
        # fence 的结果不能回填：父 Run 的日志里没有 subtask.finished。
        assert _events(log, parent_run_id, "subtask.finished") == []
        # 子 Run 真的跑过一次模型调用 → 费用照实结算，不退款。
        usage = stack.budget.usage(f"run:{parent_run_id}")
        assert usage["settled"]["subtasks"] == 1
        assert usage["reserved"]["subtasks"] == 0
    finally:
        log.close()


def test_child_finish_after_parent_revision_is_not_backfilled(tmp_path):
    """子 Run 只做纯读、直接收尾：收尾事务里核验父版本，同样不回填。

    纯读不走派发（`read_text` 没有副作用），所以派发前的 fence 检查根本不会
    被触发。如果收尾时不核验，一次"父目标已改"之后完成的子任务会以 succeeded
    回填父 Run——那正是规格禁止的"旧目标结论复活"。这条测试钉住收尾路径。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)

        stack.tasks.revise_goal(task.task_id, "换成新目标")

        child_model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("read-1", "read_text", {"path": "doc.md"})],
                ),
                ModelResponse("end_turn", content="父目标改之前读到的内容。"),
            ]
        )
        child_engine = engine_for(stack, child_model)
        result = child_engine.resume_run(row.child_run_id)
        # 子 Run 自己的任务没被改目标，它可以正常成功；但结果不能回填父 Run。
        assert result.state == "succeeded", result

        fenced = stack.tasks.get_subtask(parent_run_id, "reader")
        assert fenced.state == "fenced"
        assert fenced.summary is None and fenced.result_ref is None
        assert _events(log, parent_run_id, "subtask.finished") == []
        # 子 Run 的执行轨迹仍完整可查（它的模型调用、读文件结果都在自己的日志里）。
        results = _results(log, row.child_run_id)
        assert results["read-1"]["status"] == "succeeded"
    finally:
        log.close()


def test_recovery_fences_instead_of_backfilling(tmp_path):
    """启动恢复与收尾路径用**同一套** fence 判定：父目标变了就不回填。

    场景是"子 Run 已终结、`subtasks` 行没来得及收尾"（崩溃窗口 / 旧库升级），
    而父目标此时已经改了。恢复只有一条正确结论：记 `fenced`、**不写**
    `subtask.finished`。如果恢复路径只看子 Run 的 `outcome`，同一份事实会得到
    两个相反答案（执行侧判 fence、恢复侧判 succeeded）。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)

        # 子 Run 真的跑完（无输出，succceeded），行也随之收尾。
        child_engine = engine_for(
            stack, ScriptedModel([ModelResponse("end_turn", content="")])
        )
        assert child_engine.resume_run(row.child_run_id).state == "succeeded"
        assert stack.tasks.get_subtask(parent_run_id, "reader").state == "succeeded"
        before = len(_events(log, parent_run_id, "subtask.finished"))
        assert before == 1

        # 模拟"收尾事务没提交"留下的窗口：行回到未收尾。
        stack.tasks._conn.execute(  # noqa: SLF001 - 造窗口，只改投影
            "UPDATE subtasks SET state='running', summary=NULL, result_ref=NULL,"
            " ended_at=NULL WHERE parent_run_id=? AND subtask_id=?",
            (parent_run_id, "reader"),
        )
        # 父改目标 → 子任务身份作废。
        stack.tasks.revise_goal(task.task_id, "换成新目标")

        fixed = stack.tasks.reconcile_finished_subtasks()
        assert fixed == ["reader"], fixed
        recovered = stack.tasks.get_subtask(parent_run_id, "reader")
        assert recovered.state == "fenced", "父目标已变，不能按子 Run 的 outcome 回填"
        assert recovered.error and (
            "父目标已变更" in recovered.error or "父 Run 已被 fence" in recovered.error
        )
        assert recovered.summary is None and recovered.result_ref is None
        assert len(_events(log, parent_run_id, "subtask.finished")) == before, (
            "fence 不写 subtask.finished：恢复路径也不许把作废结论写进父 Run"
        )
    finally:
        log.close()
