"""G2 子代理 D1：spawn 的实体、审批、限额与共享预算。

判据（规格 §3 D1-c2…c6、D1-c8、D1-c9）+ §2.3 的六道门。

一条主线：**spawn 只在"任务声明了委派能力 + 人批准 + 数量/局部上限都满足 +
父账户额度够"四件事同时成立时才会发生**，而且一件事成功后子任务只是 `queued`
（D1 不执行子 Run，这是刻意的）。

测试用两层：`Dispatcher.prepare/dispatch` 直接打，验拒绝编号与事务内容；
`AgentLoop` 端到端，验"批准之前一个子任务都不存在 / 批准之后才排队"。
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.dispatcher import DispatchDenied, build_request
from tianshu.events import EventLog, sha256_hex
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.tasks import (
    RUN_RUNNING,
    SUBTASK_DEADLINE_MS,
    SUBTASK_DEFAULT_MAX_TOKENS,
    SUBTASK_DEFAULT_MAX_TURNS,
    subtask_budget_attempt,
)

SUBTASK_SPAWNED = "subtask.spawned"


# ---------------------------------------------------------------- 夹具


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    return log, stack_for(log, data_root=tmp_path / "data")


def _task(stack, ws: Path | None = None, *, max_subtasks: int = 2, goal="父任务"):
    bindings = [str(ws)] if ws is not None else []
    return stack.tasks.create_task(goal, bindings, max_subtasks=max_subtasks)


def _parent_run(stack, task):
    """给父任务开一个 running 的 Run（委派不需要目录租约）。"""
    run = stack.tasks.start_run(task.task_id)
    job = stack.claims.enqueue(run.run_id)
    claim = stack.claims.claim(job.job_id)
    stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    return run, claim


def _spawn_request(stack, task, run, claim, subtask_id: str, **extra):
    args = {"subtask_id": subtask_id, "goal": f"调查 {subtask_id}"}
    args.update(extra)
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=f"call-{subtask_id}",
        tool_id="subtask.spawn",
        target=f"subtask://{subtask_id}",
        args=args,
    )
    return request, args


def _spawn(
    stack,
    task,
    run,
    claim,
    subtask_id: str,
    *,
    approve: bool = True,
    **extra,
):
    """走完整准备 + 批准 + 派发，返回 (decision, result)。"""
    from tianshu.permissions import HumanIdentity

    request, args = _spawn_request(stack, task, run, claim, subtask_id, **extra)
    decision = stack.dispatcher.prepare(request, claim, args=args)
    if decision.is_waiting and approve:
        stack.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
        )
        # 与真实主循环一致：批准之后重新判定（同一 request_id 复用同一张批准）。
        decision = stack.dispatcher.prepare(request, claim, args=args)
    if decision.is_rejected:
        return decision, None
    return decision, stack.dispatcher.dispatch(decision.request_id, claim)


def _results(log: EventLog, run_id: str) -> dict[str, dict]:
    return {
        event.payload["call_id"]: event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == "tool.result"
    }


def _events(log: EventLog, run_id: str, type_name: str) -> list[dict]:
    return [
        event.payload
        for event in log.read_run(run_id, verify=False)
        if event.type == type_name
    ]


def _finish_rows(log: EventLog, parent_run_id: str, *, state: str = "succeeded") -> None:
    """把已有子任务行标成终态（模拟 D2 收尾；D1 自己不会写这些状态）。"""
    log.conn.execute(
        "UPDATE subtasks SET state=?, ended_at=? WHERE parent_run_id=?",
        (state, int(time.time() * 1000), parent_run_id),
    )
    log.conn.commit()


# ---------------------------------------------------------------- D1-c2


def test_spawn_without_declaration_is_rejected_before_approval(tmp_path):
    """`max_subtasks=0`（=未声明）→ `c4.subtask_not_configured`，**不弹批准**。

    为什么强调"在弹批准之前"：审批是给人做决定的。为一个注定派不出去的能力
    弹批准，等于给用户一个"批了就能派"的错觉（与 `outbound_allow` 同一条纪律）。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    log, stack = _stack(tmp_path)
    try:
        # 直接打派发层：拒绝编号 + 一条审批都不产生。
        task = _task(stack, ws, max_subtasks=0)
        run, claim = _parent_run(stack, task)
        decision, result = _spawn(stack, task, run, claim, "s1")
        assert decision.is_rejected
        assert decision.rule_id == "c4.subtask_not_configured"
        assert result is None
        assert stack.approvals.pending(run_id=run.run_id) == []
        assert _events(log, run.run_id, "approval.requested") == []
        assert stack.tasks.list_subtasks(run.run_id) == []
    finally:
        log.close()

    # 端到端：模型尝试 spawn，Run 不进入 waiting_approval，事件里是拒绝。
    log, stack = _stack(tmp_path / "e2e")
    try:
        task = stack.tasks.create_task("没声明委派", [str(ws)])
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("c1", "subtask.spawn", {"subtask_id": "s1", "goal": "g"})
                    ],
                ),
                ModelResponse("end_turn", content="派不了。"),
            ]
        )
        engine = engine_for(stack, model)
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state != "waiting_approval", "注定派不出去的请求不该弹批准"
        tool_result = _results(log, result.run_id)["c1"]
        assert tool_result["status"] == "rejected"
        assert tool_result["output"]["rule_id"] == "c4.subtask_not_configured"
        assert _events(log, result.run_id, "approval.requested") == []
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c3


def test_child_task_is_read_only_and_depth_one(tmp_path):
    """子 Task 的字段是固定的：只读绑定（去重）、无 exec/外发/记忆/再委派。

    这不是"顺便抄一份父绑定"：写类动作在子任务里必须是**拒绝**而不是"弹审批"，
    所以执行侧看到的世界只能是只读的。`depth=1` + `max_subtasks=0` 一起把
    "子任务再委派"这条路径从根上关掉。
    """
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task(
            "父任务",
            [
                str(ws1),
                {"root_path": str(ws2), "mode": "readonly", "snapshot_policy": "none"},
            ],
            max_subtasks=2,
        )
        run, claim = _parent_run(stack, task)
        decision, result = _spawn(stack, task, run, claim, "reader")
        assert decision.is_admitted, decision
        assert result is not None and result.status == "succeeded"
        subtask = stack.tasks.list_subtasks(run.run_id)[0]
        child = stack.tasks.get_task(subtask.child_task_id)
        assert child is not None
        assert child.depth == 1
        assert child.max_subtasks == 0, "子任务不能再委派"
        assert child.exec_policy is None, "子任务不能执行命令"
        assert child.outbound_allow == (), "子任务不能出网"
        assert child.memory_scopes == (), "子任务不能声明记忆作用域"
        assert child.is_control is False
        assert child.parent_task_id == task.task_id
        assert child.parent_run_id == run.run_id
        assert child.subtask_id == "reader"
        assert len(child.workspaces) == 2, "父任务的两个目录都要（去重后）保留"
        assert all(b.mode == "readonly" for b in child.workspaces), (
            f"子任务必须只有只读绑定：{[b.mode for b in child.workspaces]}"
        )
        # 子 Run 也在 Run 侧记下父链：投影重建后仍能回答"谁派的"。
        child_run = stack.tasks.get_run(subtask.child_run_id)
        assert child_run is not None
        assert child_run.parent_run_id == run.run_id
        assert child_run.subtask_id == "reader"
        created = [
            event.payload
            for event in log.read_run("", verify=False)
            if event.type == "task.created"
            and event.payload.get("task_id") == child.task_id
        ]
        assert created and created[0]["depth"] == 1
        assert created[0]["parent_run_id"] == run.run_id
        assert created[0]["subtask_id"] == "reader"
        assert created[0]["max_subtasks"] == 0
    finally:
        log.close()


def test_depth_one_task_cannot_delegate(tmp_path):
    """depth=1 的任务自己不能再派（`dispatch.subtask_depth_exceeded`）。

    子任务的 `max_subtasks` 恒为 0，但深度检查在它之前：拒绝原因必须说
    "这一层不能再派"，而不是含糊的"没声明能力"。
    """
    log, stack = _stack(tmp_path)
    try:
        parent = stack.tasks.create_task("根", max_subtasks=2)
        parent_run = stack.tasks.start_run(parent.task_id)
        # 直接建一个 depth=1 的任务（父子链三项必须齐）
        child_task = stack.tasks.create_task(
            "子任务",
            max_subtasks=2,  # 就算声明了也不该放行：深度是硬限制
            parent_task_id=parent.task_id,
            parent_run_id=parent_run.run_id,
            subtask_id="root-child",
            depth=1,
        )
        run, claim = _parent_run(stack, child_task)
        decision, _ = _spawn(stack, child_task, run, claim, "grandchild")
        assert decision.is_rejected
        assert decision.rule_id == "dispatch.subtask_depth_exceeded"
        assert stack.tasks.list_subtasks(run.run_id) == []
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c4


def test_spawn_is_atomic(tmp_path):
    """spawn 的五件事要么一起出现、要么都不出现：子 Task / 子 Run / 子 Job /
    `subtasks` 行 / 父 Run 的 `subtask.spawned`。

    "少一件"的两种坏状态都是灾祸：少子 Task = 父任务说派了、子任务不存在；
    少子 Job = 子 Run 永远不会被认领；少 `subtasks` 行 = 父任务连自己派过什么
    都答不出来。这里逐条把身份对上，并核对它们指向同一次委派。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    log, stack = _stack(tmp_path)
    try:
        task = _task(stack, ws)
        run, claim = _parent_run(stack, task)
        decision, result = _spawn(stack, task, run, claim, "atomic")
        assert decision.is_admitted, decision
        assert result is not None and result.status == "succeeded"

        (row,) = stack.tasks.list_subtasks(run.run_id)
        child_task = stack.tasks.get_task(row.child_task_id)
        child_run = stack.tasks.get_run(row.child_run_id)
        job = stack.claims.get(row.job_id)
        spawned = _events(log, run.run_id, SUBTASK_SPAWNED)
        tool_result = _results(log, run.run_id)["call-atomic"]

        assert child_task is not None
        assert child_run is not None and child_run.state == "created"
        assert job is not None and job.state == "pending"
        assert len(spawned) == 1
        assert spawned[0]["subtask_id"] == row.subtask_id
        assert spawned[0]["child_task_id"] == row.child_task_id
        assert spawned[0]["child_run_id"] == row.child_run_id
        assert spawned[0]["job_id"] == row.job_id
        assert spawned[0]["limits"] == row.limits
        # 父 Run 只留目标摘要，不留正文（正文在子 Task 的 goal 里）。
        assert spawned[0]["goal_digest"] == sha256_hex(
            ("调查 atomic").encode()
        )
        assert "goal" not in spawned[0]
        # 结果里的身份与行、与事件一致（不是重新生成的 id）
        assert tool_result["status"] == "succeeded"
        assert tool_result["effect_state"] == "none_applied"
        assert tool_result["output"]["child_task_id"] == row.child_task_id
        assert tool_result["output"]["child_run_id"] == row.child_run_id
        assert tool_result["output"]["job_id"] == row.job_id
        assert tool_result["output"]["state"] == "queued"
        # 子 Job 属于子 Run，父 Run 的作业没有被顶掉。
        assert job.run_id == row.child_run_id
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c5


def test_subtask_slug_scoped_to_parent_run(tmp_path):
    """slug 在**父 Run** 内唯一：重复 spawn 拒绝；换一个父 Run 同名合法。

    主键是 `(parent_run_id, subtask_id)`。把 slug 当成全局唯一会让"两次独立
    执行各派一个 `read-docs`"变成不可能；反过来，同一父 Run 里静默复用 slug
    会让 `subtask.status` 指向哪个孩子都说不清。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=2)
        run, claim = _parent_run(stack, task)
        first, first_result = _spawn(stack, task, run, claim, "read-docs")
        assert first.is_admitted
        assert first_result is not None and first_result.status == "succeeded"

        duplicate, _ = _spawn(stack, task, run, claim, "read-docs")
        assert duplicate.is_rejected
        assert duplicate.rule_id == "dispatch.subtask_exists"
        assert len(stack.tasks.list_subtasks(run.run_id)) == 1, "拒绝不能留下第二行"

        # 换一个父 Run：先收尾旧 Run，再开新的。
        stack.tasks.finish_run(run.run_id, outcome="succeeded", effect_state="none_applied")
        run2, claim2 = _parent_run(stack, task)
        again, again_result = _spawn(stack, task, run2, claim2, "read-docs")
        assert again.is_admitted, again
        assert again_result is not None and again_result.status == "succeeded"
        assert len(stack.tasks.list_subtasks(run2.run_id)) == 1
        assert len(stack.tasks.list_subtasks(run.run_id)) == 1
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c6


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"max_turns": 0}, "max_turns"),
        ({"max_turns": 9}, "max_turns"),
        ({"max_tokens": 255}, "max_tokens"),
        ({"max_tokens": 8001}, "max_tokens"),
    ],
)
def test_spawn_limits_are_exact(tmp_path, extra, needle):
    """局部上限越界 → `dispatch.subtask_limit`，**拒绝**而不是静默夹取。

    夹到上限会让"我批的是 100 轮"和"实际只给 8 轮"变成同一张批准，而用户
    永远不会知道自己的请求被改过。边界值（1/8/256/8000）另有用例证明合法。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=2)
        run, claim = _parent_run(stack, task)
        decision, _ = _spawn(stack, task, run, claim, "too-big", **extra)
        assert decision.is_rejected
        assert decision.rule_id == "dispatch.subtask_limit"
        assert needle in decision.reason
        assert stack.tasks.list_subtasks(run.run_id) == []
    finally:
        log.close()


def test_spawn_boundary_limits_are_accepted(tmp_path):
    """边界值 1/8/256/8000 合法；未给时用默认 8 / 2000。"""
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=4)
        run, claim = _parent_run(stack, task)
        for index, extra in enumerate(
            (
                {"max_turns": 1, "max_tokens": 256},
                {"max_turns": 8, "max_tokens": 8000},
                {},
            )
        ):
            decision, result = _spawn(
                stack, task, run, claim, f"edge-{index}", **extra
            )
            assert decision.is_admitted, decision
            assert result is not None and result.status == "succeeded"
            # 每父 Run 的未完成上限是 2：收尾刚派的那个再派下一个（累计仍 < 4）。
            _finish_rows(log, run.run_id)
        rows = {row.subtask_id: row for row in stack.tasks.list_subtasks(run.run_id)}
        assert rows["edge-0"].limits["max_turns"] == 1
        assert rows["edge-0"].limits["max_tokens"] == 256
        assert rows["edge-1"].limits["max_turns"] == 8
        assert rows["edge-1"].limits["max_tokens"] == 8000
        assert rows["edge-2"].limits["max_turns"] == SUBTASK_DEFAULT_MAX_TURNS
        assert rows["edge-2"].limits["max_tokens"] == SUBTASK_DEFAULT_MAX_TOKENS
        for row in rows.values():
            assert row.limits["deadline_ms"] == SUBTASK_DEADLINE_MS
    finally:
        log.close()


def test_spawn_enforces_unfinished_and_total_quotas(tmp_path):
    """每父 Run 未完成 < 2、每根任务累计 < 4；超了拒绝，不排队等。

    两条限额回答的是两个不同问题：前者防"一个父 Run 同时等一堆孩子"，后者防
    "一个父任务无限派、预算与审计被稀释"。所以已完成的子任务释放前者、不释放
    后者——这里用 SQL 把行标成终态来模拟 D2 收尾，正是为了证明这一点。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=4)
        run, claim = _parent_run(stack, task)
        for slug in ("a", "b"):
            decision, result = _spawn(stack, task, run, claim, slug)
            assert decision.is_admitted, decision
            assert result is not None and result.status == "succeeded"
        third, _ = _spawn(stack, task, run, claim, "c")
        assert third.is_rejected
        assert third.rule_id == "dispatch.subtask_limit"
        assert "未完成子任务" in third.reason

        # 两个都收尾之后，未完成额度回到 0；但累计数已经是 2。
        _finish_rows(log, run.run_id)
        for slug in ("c", "d"):
            decision, result = _spawn(stack, task, run, claim, slug)
            assert decision.is_admitted, decision
            assert result is not None and result.status == "succeeded"
        _finish_rows(log, run.run_id)
        fifth, _ = _spawn(stack, task, run, claim, "e")
        assert fifth.is_rejected
        assert fifth.rule_id == "dispatch.subtask_limit"
        assert "每根任务最多" in fifth.reason
        assert len(stack.tasks.list_subtasks(run.run_id)) == 4
    finally:
        log.close()


def test_spawn_rejects_oversized_goal(tmp_path):
    """goal > 2 KiB → `dispatch.subtask_limit`（有界摘要，不落大正文）。"""
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=2)
        run, claim = _parent_run(stack, task)
        huge = "描" * 700  # 3 字节/字 → 2100 字节
        decision, _ = _spawn(stack, task, run, claim, "huge", goal=huge)
        assert decision.is_rejected
        assert decision.rule_id == "dispatch.subtask_limit"
        # schema 的 maxLength 在准入阶段挡住更极端的长度；派发层挡住字节超限。
        assert stack.tasks.list_subtasks(run.run_id) == []
    finally:
        log.close()


def test_subtask_tools_are_the_only_ones_allowed_on_the_resource(tmp_path):
    """工具归属：非子任务工具拿 `subtask://` → `dispatch.subtask_rejected`。

    分类器不回答"这个工具能不能用这个资源"；执行侧必须自己判，而不是把
    `subtask://` 当成一个文件路径回退下去。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=2)
        run, claim = _parent_run(stack, task)
        request, args = _spawn_request(stack, task, run, claim, "owned")
        # 把工具换成文件工具（资源保持 subtask://）：这是"调用侧拼错了"的形状。
        wrong = dataclasses.replace(request, tool_id="read_text")
        with pytest.raises(DispatchDenied) as excinfo:
            stack.dispatcher._authority(wrong, claim, args=args)
        assert excinfo.value.rule_id == "dispatch.subtask_rejected"

        # 反向：子任务工具拿文件目标，在 build_request 就拒绝（不会走到派发）。
        from tianshu.errors import ToolNotAllowed

        with pytest.raises(ToolNotAllowed):
            build_request(
                task=task,
                run=run,
                claim=claim,
                call_id="c-file",
                tool_id="subtask.status",
                target=str(tmp_path / "a.md"),
                args={"subtask_id": "owned"},
            )
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c8


def test_spawn_reserves_subtasks_dimension(tmp_path):
    """spawn 在父账户 `subtasks` 维度**真实**预占 1；子账户父链指向根 Run 账户。

    这是共享预算的接线点：父链负责扣减，子账户只声明局部上限。不预占的话，
    "父额度耗尽时子任务也拒发"这条验收在派发层没有任何事实可核。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=2)
        run, claim = _parent_run(stack, task)
        account_id = f"run:{run.run_id}"
        stack.budget.create_account(account_id, run_id=run.run_id, label="run")
        decision, result = _spawn(
            stack, task, run, claim, "budgeted", max_tokens=1200, max_turns=3
        )
        assert decision.is_admitted, decision
        assert result is not None and result.status == "succeeded"
        usage = stack.budget.usage(account_id)
        assert usage["reserved"]["subtasks"] == 1
        assert usage["settled"]["subtasks"] == 0, "D1 不结算：收尾在 D2"
        reservation = stack.budget.reserve(
            account_id,
            attempt_id=subtask_budget_attempt(run.run_id, "budgeted"),
            need={"subtasks": 1},
        )
        assert reservation.need == {"subtasks": 1}
        assert reservation.state == "pending"

        row = stack.tasks.list_subtasks(run.run_id)[0]
        child_account = stack.budget.account(f"run:{row.child_run_id}")
        assert child_account is not None
        assert child_account["parent_id"] == account_id
        assert child_account["label"] == "subtask"
        # 子账户只声明局部上限：tokens = max_tokens，requests = max_turns，
        # deadline = spawn 时刻 + 300s。
        child_limits = stack.budget._limits(f"run:{row.child_run_id}")
        assert child_limits == {"tokens": 1200, "requests": 3}
        assert child_account["deadline_ms"] is not None
        # deadline 从子任务 created_at 起算（不重复读墙钟）：这条是**精确**不变量，
        # 不是"大约 300 秒"。CI 上曾经因为两次读钟相差 1ms 让 <= 断言偶发红。
        assert child_account["deadline_ms"] - row.created_at == SUBTASK_DEADLINE_MS
    finally:
        log.close()


def test_spawn_refused_when_parent_budget_exhausted(tmp_path):
    """父账户 `subtasks` 额度耗尽 → `dispatch.budget_refused`，一行都不建。

    拒绝发生在建行之前（预占在同一事务的最前面）：否则会留下"子任务排上了但
    额度没占"的半条记录，而账本看起来还有余量。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("父", max_subtasks=2)
        run, claim = _parent_run(stack, task)
        stack.budget.create_account(
            f"run:{run.run_id}",
            run_id=run.run_id,
            label="run",
            limits={"subtasks": 0},
        )
        decision, result = _spawn(stack, task, run, claim, "no-budget")
        assert decision.is_admitted, "预算拒绝发生在 guard（dispatch）阶段"
        assert result is not None
        assert result.status == "rejected"
        assert result.effect_state == "none_applied"
        assert result.output["rule_id"] == "dispatch.budget_refused"
        assert stack.tasks.list_subtasks(run.run_id) == []
        assert _events(log, run.run_id, SUBTASK_SPAWNED) == []
        usage = stack.budget.usage(f"run:{run.run_id}")
        assert int((usage["reserved"] or {}).get("subtasks", 0)) == 0
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c9


def test_spawn_requires_approval_then_queues(tmp_path):
    """`subtask.spawn` 必须人工批准；批准后子任务 **queued**，父作业挂起等待。

    D2 改变的是父 Run 的收尾方式：父模型说"派好了，等结果"时，父 Run 不再
    直接 succeeded，而是把作业 `park` 成 `waiting`、Run 保持 `running`——因为
    还有未完成子任务。子任务本身仍是 `queued`：执行由既有 Worker 认领子 Job
    来完成（见 `test_worker_drives_child_run`），spawn 自己不会顺手跑它。
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
                        ToolCall(
                            "c1",
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
        assert result.state == "waiting_approval"
        pending = stack.approvals.pending(run_id=result.run_id)
        assert len(pending) == 1
        approval = pending[0]
        assert approval.action == "delegate"
        assert approval.resources == ["subtask://reader"]
        requested = _events(log, result.run_id, "approval.requested")
        assert requested and requested[0]["rule_id"] == "c4.subtask_spawn"
        # 批准之前：一个子任务都不存在（不能"先建后批"）。
        assert stack.tasks.list_subtasks(result.run_id) == []

        approve_pending(stack, result.run_id)
        assert stack.tasks.list_subtasks(result.run_id) == []
        result = engine.resume_run(result.run_id)
        # D2 口径：还有未完成子任务 → 父作业 waiting、Run 保持 running。
        assert result.state == "running", result
        assert stack.tasks.get_run(result.run_id).state == "running"
        parent_job = stack.claims.job_for_run(result.run_id)
        assert parent_job is not None and parent_job.state == "waiting"

        (row,) = stack.tasks.list_subtasks(result.run_id)
        assert row.state == "queued"
        child_run = stack.tasks.get_run(row.child_run_id)
        assert child_run is not None and child_run.state == "created"
        job = stack.claims.get(row.job_id)
        assert job is not None and job.state == "pending"
        assert _events(log, result.run_id, "subtask.finished") == [], (
            "spawn 不执行子任务：不许写 finished 事件假装跑完"
        )
        tool_result = _results(log, result.run_id)["c1"]
        assert tool_result["status"] == "succeeded"
        assert tool_result["effect_state"] == "none_applied"
        assert tool_result["output"]["subtask_id"] == "reader"
        assert tool_result["output"]["state"] == "queued"
    finally:
        log.close()
