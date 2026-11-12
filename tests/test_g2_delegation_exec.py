"""G2 子代理 D2：执行、等待/唤醒、取消传播与结果回填。

判据（规格 §3 D2-c1…c5、c7、c8）：子 Run 由**既有 Worker** 驱动；父 Run 在
还有未完成子任务时把作业挂起（Run 保持 RUNNING），子任务收尾的**同一个事务**
写行、写父 Run 的 `subtask.finished`、结算预算、唤醒父作业；父取消把取消传给
子 Run；结果以有界摘要 + `content://` 引用回填。

这一组测试刻意走真实路径：`AgentLoop` 建 Run/派发/收尾，`Worker` 认领子 Job，
`TaskStore._finish_locked` 写终态。唯一替身是 `ScriptedModel` 和"人批准"。
"""

from __future__ import annotations

from pathlib import Path

from support import approve_pending, engine_for, stack_for

from tianshu.events import EventLog
from tianshu.jobs import JobClaim
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.worker import Worker

# ---------------------------------------------------------------- 夹具


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    return log, stack_for(log, data_root=tmp_path / "data")


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.md").write_text("# 文档\n子任务要读的内容。\n", encoding="utf-8")
    return ws


def _spawn_then_park(stack, task):
    """父模型派一个子任务并说"等结果"→ 父作业应当挂起。返回 (engine, result)。"""
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
            ModelResponse("end_turn", content="派好了，等结果。"),
        ]
    )
    engine = engine_for(stack, model)
    result = engine.start(task.task_id, out_name="out.md")
    assert result.state == "waiting_approval", result
    approve_pending(stack, result.run_id)
    result = engine.resume_run(result.run_id)
    assert result.state == "running", result
    return engine, result


def _spawn_two_then_park(stack, task):
    """父模型一次派两个子任务 → 两轮批准 → 父作业挂起（2 个未完成）。"""
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        "spawn-a",
                        "subtask.spawn",
                        {"subtask_id": "reader-a", "goal": "读 doc.md（A）"},
                    ),
                    ToolCall(
                        "spawn-b",
                        "subtask.spawn",
                        {"subtask_id": "reader-b", "goal": "读 doc.md（B）"},
                    ),
                ],
            ),
            ModelResponse("end_turn", content="派好了，等结果。"),
        ]
    )
    engine = engine_for(stack, model)
    result = engine.start(task.task_id, out_name="out.md")
    for _ in range(3):
        if result.state != "waiting_approval":
            break
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    assert result.state == "running", result
    assert stack.tasks.count_unfinished_subtasks(result.run_id) == 2
    return engine, result


def _child_model(output: str = "doc.md 说：子任务要读的内容。") -> ScriptedModel:
    return ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("read-1", "read_text", {"path": "doc.md"})],
            ),
            ModelResponse("end_turn", content=output),
        ]
    )


def _drive_child(stack, *, output: str = "doc.md 说：子任务要读的内容。", max_jobs=1):
    model = _child_model(output)
    worker = Worker(stack, model=model, heartbeat_interval_ms=None)
    try:
        ran = worker.drain(max_jobs=max_jobs)
    finally:
        worker.close()
    return worker, model, ran


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


# ---------------------------------------------------------------- D2-c1


def test_worker_drives_child_run(tmp_path):
    """子 Run 由既有 Worker 从 `created` 驱到 `succeeded`，父 Run 状态不被碰。

    D1 的"只排队"到此为止：子 Job 是普通 pending 作业，`Worker` 用同一条
    `resume_run` 路径认领它。驱动子 Run 的过程中父 Run 仍是 running——父 Run
    只会在子任务收尾的**事务**里被唤醒，不会被子 Run 的执行顺手改写。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        assert row.state == "queued"
        assert stack.tasks.get_run(row.child_run_id).state == "created"
        parent_before = stack.tasks.get_run(parent_run_id)
        parent_job_before = stack.claims.job_for_run(parent_run_id)
        assert parent_job_before.state == "waiting"

        worker, model, ran = _drive_child(stack)
        assert ran == [row.child_run_id], ran
        child = stack.tasks.get_run(row.child_run_id)
        assert child.state == "succeeded"
        assert child.outcome == "succeeded"

        (after,) = stack.tasks.list_subtasks(parent_run_id)
        assert after.state == "succeeded"
        assert after.summary and "子任务要读的内容" in after.summary
        assert after.result_ref and after.result_ref.startswith("content://sha256:")
        assert after.ended_at is not None

        assert model.calls == 2, "子任务读了文档并做了一次收尾模型调用"
        # 父 Run 的两条状态在"驱动子 Run"这一步之后没有变化：wake 只把
        # **作业**置回 pending，不会把父 Run 改写成 succeeded。
        parent_after = stack.tasks.get_run(parent_run_id)
        assert (parent_after.state, parent_after.outcome) == (
            parent_before.state,
            parent_before.outcome,
        )
        assert parent_after.state == "running"
        assert stack.claims.job_for_run(parent_run_id).state == "pending"
        assert _events(log, parent_run_id, "run.end") == [], "父 Run 还没有结论"
    finally:
        log.close()


def test_child_finish_is_one_transaction(tmp_path):
    """子任务收尾的行 / 事件 / 预算在**同一个事务**里，失败一起回滚。

    注意覆盖面：这里的注入点在 `_settle_subtask_budget`（行 → 事件 → 预算的
    中段），证明这三件同生共死；**不含** `claims.finish` 那一笔——作业收尾是
    `_finish` 里更早的一次提交，两笔之间的窗口登记在规格 §5 边界 12。
    `wake` 在本例里走不到（唯一的子任务正是被收尾的那个），所以本测试不断言
    "唤醒同事务"；唤醒的原子性由 `JobStore.wake` 的条件 UPDATE 自己保证。

    注入发生在预算结算（收尾事务的中段）：如果实现把 `subtasks` 行或
    `subtask.finished` 提前提交了，回滚之后就只剩"Run 还是 running、子任务却
    已经 succeeded"这种无法解释的中间态——父 Run 会永远等一个已完成的孩子。
    这条断言看的是**回滚之后的磁盘事实**，不是代码里写了几个 with。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)

        def boom(row, *, state, never_executed):  # noqa: ARG001 - 注入点签名
            raise RuntimeError("注入：结算失败后整个收尾事务必须回滚")

        stack.tasks._settle_subtask_budget = boom  # noqa: SLF001 - 负控注入点
        child_engine = engine_for(stack, _child_model())
        result = child_engine.resume_run(row.child_run_id)
        # 第二次收尾会因 claim 已交还而如实失败；关键是磁盘状态被整体回滚。
        assert result.state == "running", result

        child = stack.tasks.get_run(row.child_run_id)
        assert child.state == "running", "run.end 与终态写入必须一起回滚"
        assert _events(log, row.child_run_id, "run.end") == []
        still = stack.tasks.get_subtask(parent_run_id, "reader")
        assert still.state == "running", "subtasks 行不能先于事务提交"
        assert still.summary is None and still.ended_at is None
        assert _events(log, parent_run_id, "subtask.finished") == []
        assert stack.budget.usage(f"run:{parent_run_id}")["reserved"]["subtasks"] == 1
        assert stack.claims.job_for_run(parent_run_id).state == "waiting"
    finally:
        log.close()


# ---------------------------------------------------------------- D2-c2


def test_parent_parks_while_children_pending(tmp_path):
    """父 Run 想正常收尾但还有子任务 → 作业 `waiting`、Run 保持 `running`。

    挂起必须是幂等的：resume 一次、子任务仍未完成，只应多一条 `job.parked`，
    不该出现 `run.end`、也不该把作业放到 pending（那会让 worker 反复认领）。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        engine, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        run = stack.tasks.get_run(parent_run_id)
        job = stack.claims.job_for_run(parent_run_id)
        assert run.state == "running"
        assert job.state == "waiting"
        assert _events(log, parent_run_id, "job.parked")[-1]["reason"] == (
            "waiting_children"
        )
        assert "等待子任务" in (parent_result.error or "")
        assert _events(log, parent_run_id, "run.end") == []

        # 再 resume 一次：子任务仍未完成 → 还是 park（幂等），不提前结束。
        second = engine.resume_run(parent_run_id)
        assert second.state == "running", second
        assert stack.tasks.get_run(parent_run_id).state == "running"
        assert stack.claims.job_for_run(parent_run_id).state == "waiting"
        assert _events(log, parent_run_id, "run.end") == []
        assert len(_events(log, parent_run_id, "job.parked")) == 2
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        assert row.state == "queued"
    finally:
        log.close()


def test_worker_settle_does_not_touch_parked_job(tmp_path):
    """Worker 结算前先看作业状态：已经不是 `claimed` 就什么都不做。

    父 Run 在 `_drive` 里用自己的 claim 挂起作业；`run_once` 的 `_settle` 随后
    看到的是一条 `waiting` 作业。如果它无条件再 park 一次，每轮都会多一条
    `job.parked`，而真正的问题是它可能替新 owner 写状态。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        job = stack.claims.job_for_run(parent_run_id)
        assert job.state == "waiting"
        parked_before = len(_events(log, parent_run_id, "job.parked"))
        claim = JobClaim(
            job_id=job.job_id,
            run_id=parent_run_id,
            owner_instance_id=stack.claims.identity.instance_id,
            owner_pid=stack.claims.identity.pid,
            claim_epoch=job.claim_epoch,
        )
        worker = Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=None)
        try:
            worker._settle(claim)  # noqa: SLF001 - 直接测结算守卫
        finally:
            worker.close()
        assert stack.claims.job_for_run(parent_run_id).state == "waiting"
        assert len(_events(log, parent_run_id, "job.parked")) == parked_before
        assert worker.errors == []
    finally:
        log.close()


def test_missing_child_output_is_not_fabricated(tmp_path):
    """子 Run 没有最后输出 → `summary` 为空、`error` 说明「输出不可用」。

    这是"不许编造"的直接判据：不能拿子任务目标、状态或"已完成"拼一句看起来
    像结论的话；`result_ref` 也不能凭空生成（没有正文就没有内容引用）。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        worker = Worker(
            stack,
            model=ScriptedModel([ModelResponse("end_turn", content="")]),
            heartbeat_interval_ms=None,
        )
        try:
            worker.drain(max_jobs=1)
        finally:
            worker.close()
        settled = stack.tasks.get_subtask(parent_run_id, "reader")
        assert settled.state == "succeeded"
        assert settled.summary == "", "没有输出就写空，不拿目标冒充摘要"
        assert settled.result_ref is None, "没有正文就没有 content:// 引用"
        assert settled.error and "输出不可用" in settled.error
        finished = _events(log, parent_run_id, "subtask.finished")[-1]
        assert finished["state"] == "succeeded"
        assert "result_ref" not in finished
        assert "输出不可用" in finished["error"]
    finally:
        log.close()


# ---------------------------------------------------------------- D2-c3


def test_last_child_wakes_parent(tmp_path):
    """最后一个子任务收尾 → 父作业 `waiting → pending`，父 Run 可继续收尾。"""
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        engine, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        assert stack.claims.job_for_run(parent_run_id).state == "waiting"

        _drive_child(stack)
        job = stack.claims.job_for_run(parent_run_id)
        assert job.state == "pending", "子任务收尾必须在同一事务里唤醒父作业"
        woke = _events(log, parent_run_id, "job.requeued")
        assert woke and woke[-1]["by"] == "wake"
        assert woke[-1]["reason"] == "children_finished"
        assert woke[-1]["owner_pid"] is None, "唤醒不是 owner 交还：owner 已清空"

        # 父 Run 继续：模型现在看到 subtask.finished 后收尾成功。
        finished = engine.resume_run(parent_run_id)
        assert finished.state == "succeeded", finished
        assert stack.tasks.get_run(parent_run_id).outcome == "succeeded"
        assert stack.claims.job_for_run(parent_run_id).state == "done"
        end = _events(log, parent_run_id, "run.end")[-1]
        # 没改文件 → run.end 按 M0 语义报 noop（Run.outcome 仍是 succeeded）。
        assert end["outcome"] in ("succeeded", "noop")
    finally:
        log.close()


# ---------------------------------------------------------------- D2-c4


def test_subtask_result_shape(tmp_path):
    """`subtask.result`：未完成 `done:false`；完成 `done:true` + 摘要 + 引用。

    两段都用**真实父 Run** 的模型回合：先派子任务、问一次结果（还没有），
    子任务被 Worker 跑完后再问一次（有摘要与 `content://`）。结果读的是
    `subtasks` 行，不会触发子任务重跑——`model.calls` 的差值就是证据。
    """
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
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("result-pending", "subtask.result",
                                 {"subtask_id": "reader"})
                    ],
                ),
                ModelResponse("end_turn", content="先等结果。"),
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("result-done", "subtask.result",
                                 {"subtask_id": "reader"})
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

        pending = _results(log, result.run_id)["result-pending"]
        assert pending["status"] == "succeeded"
        assert pending["output"] == {
            "subtask_id": "reader",
            "state": "queued",
            "done": False,
        }

        _drive_child(stack)
        calls_before = model.calls
        finished = engine.resume_run(result.run_id)
        assert finished.state == "succeeded", finished
        output = _results(log, result.run_id)["result-done"]["output"]
        assert output["done"] is True
        assert output["state"] == "succeeded"
        assert output["summary"] and "子任务要读的内容" in output["summary"]
        assert output["result_ref"].startswith("content://sha256:")
        assert len(output["result_ref"].rsplit(":", 1)[-1]) == 64
        assert output["usage"]["turns"] >= 1
        assert model.calls - calls_before >= 1
    finally:
        log.close()


# ---------------------------------------------------------------- D2-c5


def test_parent_cancel_propagates(tmp_path):
    """父取消：未开始的子任务当场 cancelled+abandon；执行过的照实结算。

    两个子任务、两种结局：
    - A 已被 Worker 跑完 → 已发生的费用照实计入（settled 1），取消不退款；
    - B 从未开始（子 Run created、作业从未被认领）→ 取消传播时直接以
      `cancelled` 收尾并 `abandon_unsent`（这一笔确实没发生，才允许释放）。
    父 `run.end` 的 reason 带 `subtasks_cancelled=1`（只数被取消的 B）。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        engine, parent_result = _spawn_two_then_park(stack, task)
        parent_run_id = parent_result.run_id
        rows = {row.subtask_id: row for row in stack.tasks.list_subtasks(parent_run_id)}
        assert set(rows) == {"reader-a", "reader-b"}
        assert stack.budget.usage(f"run:{parent_run_id}")["reserved"]["subtasks"] == 2

        # 先让 A 真正跑完（这次调用会产生真实费用）。
        worker, model_a, ran = _drive_child(stack)
        assert ran == [rows["reader-a"].child_run_id], ran
        assert model_a.calls == 2
        a = stack.tasks.get_subtask(parent_run_id, "reader-a")
        assert a.state == "succeeded"
        assert a.result_ref and a.result_ref.startswith("content://sha256:")
        assert stack.budget.usage(f"run:{parent_run_id}")["settled"]["subtasks"] == 1

        cancelled = stack.tasks.request_cancel(parent_run_id)
        assert cancelled.state == "running", "执行侧还没到安全点：不能假装已终态"
        assert cancelled.control_requested == "cancel"
        # 父作业在 waiting：取消请求必须叫醒它，否则永远没人读取这个请求。
        assert stack.claims.job_for_run(parent_run_id).state == "pending"
        a = stack.tasks.get_subtask(parent_run_id, "reader-a")
        b = stack.tasks.get_subtask(parent_run_id, "reader-b")
        assert a.state == "succeeded", "已完成的子任务不被取消改写"
        assert b.state == "cancelled", "确定未开始 → 当场取消，不留 created 孤儿"
        assert b.error and "未开始" in b.error
        child_b = stack.tasks.get_run(b.child_run_id)
        assert child_b.state == "cancelled"
        # 未开始的 B：子 Run 取消请求仍要留痕（审计上能看出"为什么要停"）。
        assert child_b.control_requested is None, (
            "已经就地取消时的 control_requested 会被终态清空（run.finish 语义）"
        )
        usage = stack.budget.usage(f"run:{parent_run_id}")
        assert usage["settled"]["subtasks"] == 1, "A 的费用照实计入"
        assert usage["reserved"]["subtasks"] == 0, "B 未发生 → 预占释放"

        # Worker 认领父作业：安全点确认取消，不再调模型。
        worker = Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=None)
        try:
            ran = worker.drain(max_jobs=1)
        finally:
            worker.close()
        assert ran == [parent_run_id], ran
        assert stack.tasks.get_run(parent_run_id).state == "cancelled"
        end = _events(log, parent_run_id, "run.end")[-1]
        assert "subtasks_cancelled=1" in (end.get("error") or "")
        assert _events(log, parent_run_id, "subtask.finished")[-1]["state"] == (
            "cancelled"
        )
        assert model_a.calls == 2, "取消收尾不应重跑已完成的子任务"
    finally:
        log.close()


# ---------------------------------------------------------------- D2-c7


def test_subtask_cancel_is_not_instant(tmp_path):
    """`subtask.cancel` 只落 `cancelling`；执行侧在安全点确认成 `cancelled`。"""
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
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("cancel-1", "subtask.cancel",
                                 {"subtask_id": "reader"})
                    ],
                ),
                ModelResponse("end_turn", content="不要了。"),
            ]
        )
        engine = engine_for(stack, model)
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval"
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
        assert result.state == "running", result
        (row,) = stack.tasks.list_subtasks(result.run_id)
        assert row.state == "cancelling", "cancel 不是瞬时完成"
        assert row.ended_at is None, "执行侧没确认前不写结束时间"
        child = stack.tasks.get_run(row.child_run_id)
        assert child.state == "created"
        assert child.control_requested == "cancel"
        job = stack.claims.get(row.job_id)
        assert job.state == "pending" and job.cancel_requested is True
        assert _results(log, result.run_id)["cancel-1"]["output"]["state"] == (
            "cancelling"
        )

        # 执行侧确认：Worker 认领后第一个安全点就取消，不调模型。
        worker, child_model, _ = _drive_child(stack)
        assert child_model.calls == 0
        assert stack.tasks.get_run(row.child_run_id).state == "cancelled"
        (row,) = stack.tasks.list_subtasks(result.run_id)
        assert row.state == "cancelled"

        # 已完成再 cancel：幂等返回，不假装撤销。
        again = stack.tasks.request_subtask_cancel(result.run_id, "reader")
        assert again is not None
        assert again.state == "cancelled"
        assert again.ended_at is not None
    finally:
        log.close()


# ---------------------------------------------------------------- D2-c8


def test_recovery_wakes_stale_parent(tmp_path):
    """父作业 waiting、子任务已终结（崩溃窗口）→ worker 启动恢复补 wake。

    模拟的是"收尾事务提交了、唤醒却丢了"的旧状态：把已完成的子任务行改回
    `running`、父作业改回 `waiting`。Worker 启动时应先以子 Run 终态修正行
    （证据是 run.finish，不是进程退出），再因"没有未完成子任务"唤醒父作业。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        engine, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        _drive_child(stack)
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        assert row.state == "succeeded"
        # 制造"丢了一次收尾"的旧状态（D1 库升级 / 提前退出的实现会留下它）。
        log.conn.execute(
            "UPDATE subtasks SET state='running', ended_at=NULL,"
            " summary=NULL, result_ref=NULL WHERE parent_run_id=?",
            (parent_run_id,),
        )
        log.conn.execute(
            "UPDATE jobs SET state='waiting' WHERE run_id=?", (parent_run_id,)
        )
        log.conn.commit()
        assert stack.tasks.count_unfinished_subtasks(parent_run_id) == 1

        worker = Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=None)
        try:
            ran = worker.drain(max_jobs=1)
        finally:
            worker.close()
        assert any("启动恢复" in note for note in worker.notes), worker.notes
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        assert row.state == "succeeded", "以子 Run 终态为准修正"
        assert row.summary and "子任务要读的内容" in row.summary
        assert ran == [parent_run_id], "父作业被唤醒后应由同一个 worker 继续跑"
        assert stack.tasks.get_run(parent_run_id).state == "succeeded"
    finally:
        log.close()


def test_terminal_parent_is_not_woken(tmp_path):
    """父 Run 已终态时，恢复路径**不能**把它的作业拉回队列。

    终态只由 `_finish_locked` 写；但"作业恰好还停在 waiting"可以由崩溃或旧库
    留下。启动恢复若只判"没有未完成子任务"就 wake，会让 worker 认领一个已经
    结束的 Run，然后在一个终态上报"成功"。所以判据里必须有"父 Run 仍 RUNNING"。
    """
    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        # 人为把父 Run 收尾（子任务还没完成），作业留在 waiting——崩溃/旧库形状。
        stack.tasks.finish_run(
            parent_run_id, outcome="succeeded", effect_state="none_applied"
        )
        job = stack.claims.job_for_run(parent_run_id)
        assert job.state == "waiting"
        woken = stack.tasks.wake_stale_parents()
        assert woken == []
        assert stack.claims.job_for_run(parent_run_id).state == "waiting"
        assert stack.tasks.get_run(parent_run_id).state == "succeeded"
    finally:
        log.close()


# ---------------------------------------------------------------- CLI（§2.7）


def test_cli_status_shows_waiting_children_and_resume(tmp_path):
    """`run status` 说明"在等几个子任务"；`run resume` 能从 waiting 作业继续。"""
    import json as _json

    from tianshu.cli import main

    ws = _workspace(tmp_path)
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        _, parent_result = _spawn_then_park(stack, task)
        parent_run_id = parent_result.run_id
        db = str(log.db_path)
    finally:
        log.close()

    import contextlib
    import io

    def _cli(*argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue() + err.getvalue()

    code, text = _cli(
        "run", "status", "--run-id", parent_run_id,
        "--db", db, "--data-root", str(tmp_path / "data"),
    )
    assert code == 0, text
    assert "等待子任务" in text and "1 个未完成" in text

    # 子任务已终结（模拟崩溃窗口）→ `run resume` 直接认领 waiting 作业收尾。
    from tianshu.events import EventLog as _Log

    reopened = _Log(Path(db), blob_dir=tmp_path / "blobs")
    try:
        reopened.conn.execute(
            "UPDATE subtasks SET state='succeeded', ended_at=1 WHERE parent_run_id=?",
            (parent_run_id,),
        )
        reopened.conn.commit()
    finally:
        reopened.close()
    code, text = _cli(
        "run", "resume", "--run-id", parent_run_id,
        "--db", db, "--data-root", str(tmp_path / "data"), "--json",
    )
    assert code == 0, text
    payload = _json.loads(text)
    assert payload["run"]["state"] == "succeeded", payload
