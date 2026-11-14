"""W10：子代理写权限（`subtask.spawn` 2.0 的 `write` 声明）授权面判据。

规格：[SPEC-W10-SUBAGENT-DEPTH-2026-11-14.md](../docs/SPEC-W10-SUBAGENT-DEPTH-2026-11-14.md)
§3 判据表（W10-c1…c12）与 §4 负控清单。

一条主线：**写权限不是新授权面，而是"授权上限恒为父绑定"**——默认 spawn 与
W10 之前逐字节一致（RU-D1@1.0.0 钉死的行为不动）；`write=true` 只把父任务
自己可写的目录按原样交给子任务，子 Run 里的每个写动作仍走既有派发审批链
（prepare 判定 → 人批准 → 执行侧重判 + 消费 → intent → 守卫内执行 → result
+ Receipt），与父 Run 完全同构，没有任何子代理免审通道。

出网/MCP/执行命令/记忆写入/再委派在 `write=true` 下依旧全拒；深度门不变。
"""

from __future__ import annotations

from pathlib import Path

from support import approve_pending, engine_for, write_exact

from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ApprovalError, HumanIdentity
from tianshu.replay import replay
from tianshu.tasks import RUN_RUNNING
from tianshu.worker import Worker

# ---------------------------------------------------------------- 夹具

BASE_MS = 1_700_000_000_000


def _stack(tmp_path: Path, clock: list[int] | None = None):
    """装配真实栈；clock 传可变列表即注入逻辑时钟（租约/审批 TTL 用它推进）。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    if clock is None:
        from support import stack_for

        return log, stack_for(log, data_root=tmp_path / "data")
    stack = build_stack(log, data_root=tmp_path / "data", clock=lambda: clock[0])
    return log, stack


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "doc.md", "# 文档\n子任务要改的内容。\n")
    return ws


def _readonly_only(tmp_path: Path) -> Path:
    ws = tmp_path / "ro"
    ws.mkdir()
    write_exact(ws / "doc.md", "# 只读资料\n")
    return ws


def _parent_run(stack, task):
    """给父任务开一个 running 的 Run（委派与写动作的派发都要挂在 claim 上）。"""
    run = stack.tasks.start_run(task.task_id)
    job = stack.claims.enqueue(run.run_id)
    claim = stack.claims.claim(job.job_id)
    stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    return run, claim


def _spawn_request(stack, task, run, claim, subtask_id: str, **extra):
    args = {"subtask_id": subtask_id, "goal": f"调查 {subtask_id}"}
    args.update(extra)
    return build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=f"call-{subtask_id}",
        tool_id="subtask.spawn",
        target=f"subtask://{subtask_id}",
        args=args,
    ), args


def _spawn(stack, task, run, claim, subtask_id: str, *, approve: bool = True, **extra):
    """走完整准备 + 批准 + 派发，返回 (decision, result)。"""
    request, args = _spawn_request(stack, task, run, claim, subtask_id, **extra)
    decision = stack.dispatcher.prepare(request, claim, args=args)
    if decision.is_waiting and approve:
        stack.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
    if decision.is_rejected:
        return decision, None
    return decision, stack.dispatcher.dispatch(decision.request_id, claim)


def _write_request(stack, task, run, claim, call_id: str, content: str):
    """子/父 Run 名下的一次 modify_text（覆盖写）请求：同一动作内容可换 Run 重放。"""
    return build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id="modify_text",
        target=str(Path(task.workspaces[0].root_path) / "doc.md"),
        args={"path": "doc.md", "content": content},
    ), {"path": "doc.md", "content": content}


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


def _spawn_write_child(stack, tmp_path: Path, *, write: bool = True):
    """端到端：父模型 spawn 一个（默认/可写）子任务并挂起等结果。"""
    ws = _workspace(tmp_path)
    task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
    spawn_args = {"subtask_id": "writer", "goal": "改 doc.md 并总结"}
    if write:
        spawn_args["write"] = True
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("spawn-1", "subtask.spawn", spawn_args)],
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
    return task, result, ws


def _child_write_model(content: str) -> ScriptedModel:
    return ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall("w1", "modify_text", {"path": "doc.md", "content": content})
                ],
            ),
            ModelResponse("end_turn", content="改好了。"),
        ]
    )


# ---------------------------------------------------------------- W10-c1/c2/c3


def test_default_spawn_stays_read_only_and_pure_only(tmp_path):
    """W10-c1：不带 `write` 的 spawn 与 W10 之前逐字节一致。

    RU-D1@1.0.0 钉死的评测行为（子任务只读、写文件/出网被拒）必须原样保留：
    默认路径是"父绑定逐条改只读 + 子 Run pure_only"，不是"继承父 mode"。
    """
    log, stack = _stack(tmp_path)
    try:
        ws = _workspace(tmp_path)
        task = stack.tasks.create_task("父任务", [str(ws)], max_subtasks=2)
        run, claim = _parent_run(stack, task)
        decision, result = _spawn(stack, task, run, claim, "reader")
        assert decision.is_admitted and result.status == "succeeded"
        (row,) = stack.tasks.list_subtasks(run.run_id)
        child_task = stack.tasks.get_task(row.child_task_id)
        assert child_task is not None
        assert all(b.mode == "readonly" for b in child_task.workspaces)
        assert child_task.depth == 1 and child_task.max_subtasks == 0
        assert child_task.exec_policy is None and child_task.outbound_allow == ()
        assert child_task.mcp_servers == () and child_task.memory_scopes == ()
        assert stack.tasks.run_execution_policy(row.child_run_id).mode == "pure_only"
    finally:
        log.close()


def test_write_spawn_grants_exactly_the_parent_writable_bindings(tmp_path):
    """W10-c2：write=true 只把**父任务自己可写**的绑定按原 mode 交给子任务。

    授权上限恒为父绑定：父可写根 → 子可写；父只读根 → 子仍然只读。子 Run
    是 effects 资格（能看见写工具），但每个写动作仍要过派发审批。子 Task 的
    `task.created` 事件与投影一致——本 PR 没有给任何事件 payload 加键。
    """
    log, stack = _stack(tmp_path)
    try:
        ws1 = _workspace(tmp_path)
        ws2 = tmp_path / "ro2"
        ws2.mkdir()
        write_exact(ws2 / "note.md", "只读资料\n")
        task = stack.tasks.create_task(
            "父任务",
            [
                str(ws1),
                {"root_path": str(ws2), "mode": "readonly", "snapshot_policy": "none"},
            ],
            max_subtasks=2,
        )
        run, claim = _parent_run(stack, task)
        decision, result = _spawn(stack, task, run, claim, "writer", write=True)
        assert decision.is_admitted, decision
        assert result is not None and result.status == "succeeded"
        (row,) = stack.tasks.list_subtasks(run.run_id)
        child_task = stack.tasks.get_task(row.child_task_id)
        assert child_task is not None
        modes = {b.root_path: b.mode for b in child_task.workspaces}
        assert modes[str(ws1)] == "writable", modes
        assert modes[str(ws2)] == "readonly", "父只读的根不得被 write=true 升级"
        assert child_task.max_subtasks == 0 and child_task.depth == 1
        assert child_task.outbound_allow == () and child_task.mcp_servers == ()
        assert child_task.memory_scopes == () and child_task.exec_policy is None
        assert stack.tasks.run_execution_policy(row.child_run_id).mode == "effects"
        created = [
            event.payload
            for event in log.read_run("", verify=False)
            if event.type == "task.created"
            and event.payload.get("task_id") == child_task.task_id
        ]
        assert created, "子 Task 的 task.created 事件是授权面的权威记录"
        by_root = {w["root_path"]: w["mode"] for w in created[0]["workspaces"]}
        assert by_root == {str(ws1): "writable", str(ws2): "readonly"}
    finally:
        log.close()


def test_write_spawn_on_readonly_parent_degrades_honestly(tmp_path):
    """W10-c3：父全只读时 write=true 如实降级（不凭空升级、不新增拒绝编号）。"""
    log, stack = _stack(tmp_path)
    try:
        ws = _readonly_only(tmp_path)
        task = stack.tasks.create_task(
            "父任务",
            [{"root_path": str(ws), "mode": "readonly", "snapshot_policy": "none"}],
            max_subtasks=2,
        )
        run, claim = _parent_run(stack, task)
        decision, result = _spawn(stack, task, run, claim, "writer", write=True)
        assert decision.is_admitted and result is not None
        (row,) = stack.tasks.list_subtasks(run.run_id)
        child_task = stack.tasks.get_task(row.child_task_id)
        assert child_task is not None
        assert all(b.mode == "readonly" for b in child_task.workspaces)
        assert stack.tasks.run_execution_policy(row.child_run_id).mode == "pure_only"
        # 后续真实写尝试会被策略层以既有编号拒绝（W10-c3 的只读防线）。
        # spawn 已给子 Run 建了作业，这里认领既有作业（不能重复 enqueue）。
        child_run = stack.tasks.get_run(row.child_run_id)
        child_job = stack.claims.job_for_run(child_run.run_id)
        child_claim = stack.claims.claim(child_job.job_id)
        stack.tasks.transition(child_run.run_id, RUN_RUNNING, claim=child_claim)
        request, args = _write_request(
            stack, child_task, child_run, child_claim, "w1", "新内容"
        )
        decision = stack.dispatcher.prepare(request, child_claim, args=args)
        assert decision.is_rejected, decision
        assert decision.rule_id == "c4.readonly_binding"
    finally:
        log.close()


# ---------------------------------------------------------------- W10-c12


def test_spawn_tool_contract_binds_write_parameter(tmp_path):
    """W10-c12：注册表里的 subtask.spawn 是 2.0，write 是布尔可选参数。

    schema 变化即新工具（红线 8）：版本与 schema 摘要进派发请求与审批摘要，
    旧批准自动失效。模型清单只投影 name/description/input_schema。
    """
    log, stack = _stack(tmp_path)
    try:
        from tianshu.tools import validate_args, validate_registry

        validate_registry()
        spec = stack.dispatcher.registry.spec("subtask.spawn")
        assert spec.version == "2.0"
        assert spec.input_schema["properties"]["write"] == {"type": "boolean"}
        assert set(spec.input_schema["properties"]) == {
            "subtask_id", "goal", "write", "max_turns", "max_tokens",
        }
        # 布尔 write 合法；非布尔在准入层被 schema 校验拒绝（不去派发层）。
        validate_args({"subtask_id": "s", "goal": "g", "write": True}, spec.input_schema)
        try:
            validate_args({"subtask_id": "s", "goal": "g", "write": "yes"}, spec.input_schema)
        except Exception as exc:  # noqa: BLE001 - 只断言"被拒"，不绑定异常类型
            assert type(exc).__name__ == "ToolArgumentError", exc
        else:
            raise AssertionError("非布尔 write 必须在准入层被拒")
        # 缺省（不带 write）与旧调用形状兼容。
        validate_args({"subtask_id": "s", "goal": "g"}, spec.input_schema)
    finally:
        log.close()


# ---------------------------------------------------------------- W10-c9


def test_write_child_still_cannot_delegate_fetch_or_write_memory(tmp_path):
    """W10-c9：write=true 只放开文件写——再委派/出网/记忆写依旧按既有编号拒绝。

    深度门（`dispatch.subtask_depth_exceeded`）不因 write 声明松动；出网白名单
    与记忆作用域在子 Task 上就是空的，派发层按既有编号拒绝。
    """
    log, stack = _stack(tmp_path)
    try:
        task, parent_result, _ws = _spawn_write_child(stack, tmp_path, write=True)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        child_task = stack.tasks.get_task(row.child_task_id)
        child_run = stack.tasks.get_run(row.child_run_id)
        child_job = stack.claims.job_for_run(child_run.run_id)
        child_claim = stack.claims.claim(child_job.job_id)
        stack.tasks.transition(child_run.run_id, RUN_RUNNING, claim=child_claim)

        def _denied(call_id, tool_id, target, args, resource_kind=None):
            request = build_request(
                task=child_task,
                run=child_run,
                claim=child_claim,
                call_id=call_id,
                tool_id=tool_id,
                target=target,
                args=args,
            )
            decision = stack.dispatcher.prepare(request, child_claim, args=args)
            assert decision.is_rejected, (tool_id, decision)
            return decision.rule_id

        # 子任务自己再派子任务：真实产生路径是门 2（子任务 max_subtasks 恒 0，
        # 未声明委派能力）；深度门（dispatch.subtask_depth_exceeded）在其后，
        # 由既有 test_depth_one_task_cannot_delegate 与本 PR 负控 W10-n3 钉住。
        assert _denied(
            "nested-1", "subtask.spawn", "subtask://grandchild",
            {"subtask_id": "grandchild", "goal": "再派一层"},
        ) == "c4.subtask_not_configured"

        # 出网：子任务没有外发白名单，弹批准之前就拒。
        assert _denied(
            "net-1", "network.fetch", "network://https/example.test/a",
            {"url": "http://example.test/a"},
        ) == "c4.outbound_not_allowed"

        # 记忆写入：子 Run（depth=1 / parent_run_id 非空）恒拒。
        assert _denied(
            "mem-1", "memory.propose", "memory://user/local",
            {"kind": "fact", "content": "子任务想写记忆"},
        ) == "c4.memory_scope_denied"

        # 记忆确认（覆盖类写）同样恒拒：不是"弹审批"，是"没资格发起"。
        assert _denied(
            "mem-2", "memory.confirm", "memory://user/local",
            {"memory_id": "mem_x", "revision": 1},
        ) == "c4.memory_scope_denied"
    finally:
        log.close()


def test_write_child_cannot_write_outside_parent_bindings(tmp_path):
    """W10-c9b：write=true 不扩大目录面——绑定外目标在派发层被拒。

    派发层的守卫次序是先核租约覆盖再分类：子 Run 只持父绑定目录的租约，
    绑定外目标先命中 `dispatch.lease_lost`（与既有 test_dispatch.py 的口径
    一致：该场景可产出 `c4.outside_workspace_unsupported` 或
    `dispatch.lease_lost`，取决于租约持有状态）；工作区分类层的
    `c4.outside_workspace` 由 test_permissions.py 单独钉住。
    """
    log, stack = _stack(tmp_path)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        (row,) = stack.tasks.list_subtasks(parent_result.run_id)
        child_task = stack.tasks.get_task(row.child_task_id)
        child_run = stack.tasks.get_run(row.child_run_id)
        child_job = stack.claims.job_for_run(child_run.run_id)
        child_claim = stack.claims.claim(child_job.job_id)
        stack.tasks.transition(child_run.run_id, RUN_RUNNING, claim=child_claim)
        outside = tmp_path / "outside.md"
        write_exact(outside, "绑定外\n")
        outside_args = {"path": str(outside), "content": "越界"}
        request = build_request(
            task=child_task,
            run=child_run,
            claim=child_claim,
            call_id="out-1",
            tool_id="modify_text",
            target=str(outside),
            args=outside_args,
        )
        decision = stack.dispatcher.prepare(request, child_claim, args=outside_args)
        assert decision.is_rejected
        assert decision.rule_id == "dispatch.lease_lost", decision
        assert outside.read_text(encoding="utf-8") == "绑定外\n"
    finally:
        log.close()


# ---------------------------------------------------------------- W10-c4/c5/c6/c7


def _hand_workspace_to_child(stack, clock, parent_run_id):
    """把父 Run 挂起时仍持有的工作区写租约**按生产路径**交给子 Run。

    生产顺序：父挂起等子任务（租约仍在手）→ 子 Run 认领后 acquire 遇
    `LeaseConflict` 交还作业 → 父租约 TTL 过期 → 下次认领按 stale 接管 +
    `takeover_reconcile` 对账（父挂起时无未决 intent，对账通过）。这里用注入
    时钟推进过 TTL、走同一条带对账的接管路径（不 sleep、不绕对账）。
    """
    from tianshu.leases import DEFAULT_TTL_MS

    clock[0] += DEFAULT_TTL_MS + 1
    (row,) = stack.tasks.list_subtasks(parent_run_id)
    child_task = stack.tasks.get_task(row.child_task_id)
    child_run = stack.tasks.get_run(row.child_run_id)
    # 认领既有子作业（spawn 时建好）：要在时钟推进**之后**认领，claim 才新鲜。
    child_job = stack.claims.job_for_run(child_run.run_id)
    child_claim = stack.claims.claim(child_job.job_id)
    stack.tasks.transition(child_run.run_id, RUN_RUNNING, claim=child_claim)
    roots = [b.identity() for b in child_task.workspaces if b.can_write]
    assert roots, "此助手只用于可写子任务"
    refusals: list[dict] = []
    stack.leases.acquire(
        roots,
        child_run.run_id,
        reconcile=stack.dispatcher.takeover_reconcile(
            run_id=child_run.run_id,
            task_id=child_task.task_id,
            refusals=refusals,
        ),
    )
    assert not refusals, f"接管对账不应拒绝干净挂起的父 Run：{refusals}"
    return child_task, child_run, child_claim


def _approved_child_write(stack, clock, parent_run_id, *, content: str):
    """拿到一个"已批准、未消费"的子 Run 写审批。

    返回 (request, args, claim, child_run, approval_id)。先按生产路径把工作区
    租约交给子 Run——写动作的授权链要求 Run 持有目标子树的写租约。
    """
    child_task, child_run, child_claim = _hand_workspace_to_child(
        stack, clock, parent_run_id
    )
    request, args = _write_request(
        stack, child_task, child_run, child_claim, "w1", content
    )
    decision = stack.dispatcher.prepare(request, child_claim, args=args)
    assert decision.is_waiting, decision
    stack.approvals.decide(
        decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )
    return request, args, child_claim, child_run, decision.approval_id


def test_unapproved_child_write_dispatch_is_rejected_not_executed(tmp_path):
    """W10-c4：子 Run 的写动作在人批准之前派发 → `c4.approval_state`，一字未动。

    `approval.requested` 落在子 Run 名下；被拒结论是确定的
    `none_applied`（"一个字节都没动"可以证明，不许记成 unknown）。
    """
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        child_task, child_run, child_claim = _hand_workspace_to_child(
            stack, clock, parent_result.run_id
        )
        request, args = _write_request(
            stack, child_task, child_run, child_claim, "w1", "未批就要写"
        )
        decision = stack.dispatcher.prepare(request, child_claim, args=args)
        assert decision.is_waiting, decision
        approval_id = decision.approval_id
        # 审批在子 Run 名下（不是父 Run 代持），请求事件也进子 Run 的事件流。
        assert stack.approvals.get(approval_id).run_id == child_run.run_id
        assert _events(log, child_run.run_id, "approval.requested")
        # 人还没批准：把这份请求直接送派发 → 编号化拒绝，磁盘不变。
        result = stack.dispatcher.dispatch(decision.request_id, child_claim)
        assert result.status == "rejected"
        assert result.effect_state == "none_applied"
        assert result.output["rule_id"] == "c4.approval_state", result.output
        assert (ws / "doc.md").read_text(encoding="utf-8") == "# 文档\n子任务要改的内容。\n"
        # 审批仍是待批：被拒的是"未批就派发"这一次执行，不是审批本身。
        assert stack.approvals.get(approval_id).state == "pending"
    finally:
        log.close()


def test_child_write_approval_is_single_use(tmp_path):
    """W10-c5：一次批准只兑现一次——consumed 后不可复用，再写必须重新批。

    `consume` 的条件 UPDATE 是唯一保证（数据库语义，不靠代码判断）；二次
    consume 抛 `ApprovalError`（派发层对应 `dispatch.approval_unusable`）。
    """
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        request, args, claim, _child_run, approval_id = _approved_child_write(
            stack, clock, parent_result.run_id, content="第一次写入"
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.is_admitted, decision
        result = stack.dispatcher.dispatch(decision.request_id, claim)
        assert result.status == "succeeded" and result.effect_state == "applied"
        assert stack.approvals.get(approval_id).state == "consumed"
        assert (ws / "doc.md").read_text(encoding="utf-8") == "第一次写入"
        # 同一张批准再兑一次：批准存储层直接拒绝（一次性消费的唯一保证）。
        try:
            stack.approvals.consume(
                approval_id,
                action_digest=request.action_digest(),
                request_digest=request.digest(),
            )
        except ApprovalError:
            pass
        else:
            raise AssertionError("同一张批准不能消费两次")
        # consumed 对 find_for 不可见：同内容第二次调用必须重新获得人的批准。
        child_run2 = stack.tasks.get_run(request.run_id)
        child_task2 = stack.tasks.get_task(child_run2.task_id)
        request2, args2 = _write_request(
            stack, child_task2, child_run2, claim, "w2", "第一次写入"
        )
        assert stack.approvals.find_for(request2) is None
        decision2 = stack.dispatcher.prepare(request2, claim, args=args2)
        assert decision2.is_waiting, "旧批准不能自动续用：要弹一张新批准"
        assert decision2.approval_id != approval_id
    finally:
        log.close()


def test_parent_run_cannot_use_child_run_approval(tmp_path):
    """W10-c6：父不能代批子——子的批准对父 Run 的同内容请求不可见、不可绑定。

    摘要绑 Run：同一段写动作从父 Run 发起，`find_for` 查不到子 Run 的批准，
    只能在父 Run 名下另发一张**新的**待批；子的批准保持已批准、未消费。
    """
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        request, _args, _claim, child_run, approval_id = _approved_child_write(
            stack, clock, parent_result.run_id, content="只有子任务批了的内容"
        )
        child_approval = stack.approvals.get(approval_id)
        assert child_approval.state == "approved"
        assert child_approval.run_id == child_run.run_id
        # 子 Run 像暂停一样交还工作区（"暂停就交还租约"是既有语义），父 Run
        # 重新拿回租约后才能为自己的写动作发起审批——这是真实顺序。
        stack.leases.release_run(child_run.run_id)
        parent_run = stack.tasks.get_run(parent_result.run_id)
        parent_job = stack.claims.job_for_run(parent_run.run_id)
        parent_claim = stack.claims.claim(parent_job.job_id)
        writable = [b.identity() for b in task.workspaces if b.can_write]
        stack.leases.acquire(writable, parent_run.run_id)
        # 父模型发起**同 call_id、同内容**的写动作：两个 Run 的模型各自从
        # call_0 编号，撞号完全现实。动作摘要里 call_id/task_id/run_id 都在，
        # 但查找层还按 run_id 过滤——两层都要钉住（负控 W10-n1 拆后者）。
        p_request, p_args = _write_request(
            stack, task, parent_run, parent_claim, "w1", "只有子任务批了的内容"
        )
        # 子的批准对父的请求不可见：父此刻还没有任何同摘要批准。
        assert stack.approvals.find_for(p_request) is None
        p_decision = stack.dispatcher.prepare(p_request, parent_claim, args=p_args)
        assert p_decision.is_waiting, "父 Run 不能继承子的批准，必须自己弹一张"
        assert p_decision.approval_id != approval_id
        p_approval = stack.approvals.get(p_decision.approval_id)
        assert p_approval.run_id == parent_run.run_id
        found = stack.approvals.find_for(p_request)
        assert found is not None and found.run_id == parent_run.run_id, (
            "父 Run 的摘要查找永远只能命中父 Run 自己的批准"
        )
        # 子的批准原封不动：状态、消费时间都没被父 Run 的动作碰过。
        after = stack.approvals.get(approval_id)
        assert (after.state, after.consumed_at) == (
            child_approval.state, child_approval.consumed_at,
        )
        assert after.state == "approved"
        # 磁盘无变化：谁都没写。
        assert (ws / "doc.md").read_text(encoding="utf-8") == "# 文档\n子任务要改的内容。\n"
    finally:
        log.close()


def test_modified_args_after_approval_need_a_new_approval(tmp_path):
    """W10-c7：digest 绑内容——改了写入内容的动作绝不可能挂在旧批准下执行。"""
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        request, _args, claim, child_run, approval_id = _approved_child_write(
            stack, clock, parent_result.run_id, content="批准时看到的内容"
        )
        approved = stack.approvals.get(approval_id)
        # 同一子 Run 换 call_id 改内容：动作摘要变 → 旧批准不可见、不可用。
        changed, changed_args = _write_request(
            stack, stack.tasks.get_task(child_run.task_id), child_run, claim,
            "w2", "改成别的",
        )
        assert changed.action_digest() != approved.action_digest
        assert stack.approvals.find_for(changed) is None
        decision = stack.dispatcher.prepare(changed, claim, args=changed_args)
        assert decision.is_waiting
        assert decision.approval_id != approval_id, "改内容必须重新批准"
        # 人没批新请求 → 磁盘还是批准时看到的原文，旧批准没有被消费。
        assert (ws / "doc.md").read_text(encoding="utf-8") == "# 文档\n子任务要改的内容。\n"
        assert stack.approvals.get(approval_id).state == "approved"
    finally:
        log.close()


def test_expired_child_write_approval_is_rejected(tmp_path):
    """W10-c7b：批准在派发前过期 → `c4.approval_expired`，一字未动。

    prepare（批准有效）与 dispatch（已过期）之间用注入时钟推进——生产里就是
    "批了但没来得及 resume"的真实窗口。
    """
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        request, args, claim, child_run, approval_id = _approved_child_write(
            stack, clock, parent_result.run_id, content="过期前批的"
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.is_admitted, decision
        clock[0] += 24 * 60 * 60 * 1000  # 推进一天：批准 10 分钟就过期
        # 心跳语义：租约随注入时钟续期（心跳只续租不续批准——批准过期是人的
        # 决定过期，不是"持有者死了"）。先续租，再派发，命中审批过期。
        stack.leases.renew(
            [b.identity() for b in task.workspaces if b.can_write],
            child_run.run_id,
        )
        result = stack.dispatcher.dispatch(decision.request_id, claim)
        assert result.status == "rejected"
        assert result.effect_state == "none_applied"
        assert result.output["rule_id"] == "c4.approval_expired", result.output
        assert (ws / "doc.md").read_text(encoding="utf-8") == "# 文档\n子任务要改的内容。\n"
    finally:
        log.close()


# ---------------------------------------------------------------- W10-c10


def test_rejected_child_write_veto_is_final(tmp_path):
    """W10-c10：人在子 Run 里否决一次写动作后，同内容重发直接 `c4.approval_rejected`。

    否决是终局（issue #40）：不再弹新审批；换 call_id 重发同一动作也命中
    （否决键是 subject_digest，不含 call_id/owner）。换内容才是新的决定。
    """
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        child_task, child_run, child_claim = _hand_workspace_to_child(
            stack, clock, parent_result.run_id
        )
        request, args = _write_request(
            stack, child_task, child_run, child_claim, "w1", "被否决的内容"
        )
        decision = stack.dispatcher.prepare(request, child_claim, args=args)
        assert decision.is_waiting
        requested_before = len(_events(log, child_run.run_id, "approval.requested"))
        assert requested_before == 1
        stack.approvals.decide(
            decision.approval_id, decision="rejected", actor=HumanIdentity.local_cli()
        )
        # 同内容换 call_id 重发：不弹新审批，直接编号化拒绝。
        resent, resent_args = _write_request(
            stack, child_task, child_run, child_claim, "w2", "被否决的内容"
        )
        assert stack.approvals.rejected_for(resent) is not None
        decision2 = stack.dispatcher.prepare(resent, child_claim, args=resent_args)
        assert decision2.is_rejected, decision2
        assert decision2.rule_id == "c4.approval_rejected", decision2
        assert len(_events(log, child_run.run_id, "approval.requested")) == (
            requested_before
        ), "否决之后不得再发起新审批"
        # 磁盘不变；换内容是新的决定，可以重新走审批。
        assert (ws / "doc.md").read_text(encoding="utf-8") == "# 文档\n子任务要改的内容。\n"
        changed, changed_args = _write_request(
            stack, child_task, child_run, child_claim, "w3", "改主意后的内容"
        )
        decision3 = stack.dispatcher.prepare(changed, child_claim, args=changed_args)
        assert decision3.is_waiting, "换参数/换内容是新的动作，允许重新发起审批"
    finally:
        log.close()


# ---------------------------------------------------------------- W10-c8


def test_child_write_end_to_end_with_receipt_and_parent_wake(tmp_path):
    """W10-c8：可写子 Run 由**既有 Worker** 驱动到终态，写动作经人批准后落盘。

    全链真实路径（唯一替身是脚本模型与"人批准"）：worker 认领子作业 → 遇父
    挂起时的活租约交还（contended）→ 父租约 TTL 过期后接管对账 → 子 Run
    提议覆盖写 → 子 Run 停 `waiting_approval`（作业挂起）→ 人批准 + resume
    → intent / 守卫内执行 / result + Receipt 落库 → 子任务收束、父作业被同一
    事务唤醒 → 父 Run 继续并读 `subtask.result`。子 Run 可回放。
    """
    clock = [BASE_MS]
    log, stack = _stack(tmp_path, clock)
    try:
        task, parent_result, ws = _spawn_write_child(stack, tmp_path, write=True)
        parent_run_id = parent_result.run_id
        (row,) = stack.tasks.list_subtasks(parent_run_id)
        child_run_id = row.child_run_id
        content = "# 文档\n子任务改好的内容。\n"

        child_model = _child_write_model(content)
        worker = Worker(stack, model=child_model, heartbeat_interval_ms=None)
        try:
            # 第一轮：父租约还活着（挂起不交还），子作业如实交还。
            ran = worker.drain(max_jobs=1)
            assert ran == [child_run_id], ran
            assert worker.contended, "活租约冲突必须走交还路径，而不是硬抢"
            job = stack.claims.job_for_run(child_run_id)
            assert job.state == "pending", "交还后作业回 pending"
            assert stack.tasks.get_run(child_run_id).state == "created"
            # 第二轮：父租约过期，接管对账通过，子 Run 被驱动到等审批。
            clock[0] += 31_000
            ran = worker.drain(max_jobs=1)
            assert ran == [child_run_id]
            child = stack.tasks.get_run(child_run_id)
            assert child.state == "waiting_approval", child
            assert (ws / "doc.md").read_text(encoding="utf-8") == "# 文档\n子任务要改的内容。\n"
        finally:
            worker.close()

        # 人批准的是子 Run 的动作：批准 + resume 都落在子 Run 名下。
        approvals = stack.approvals.pending(run_id=child_run_id)
        assert len(approvals) == 1, approvals
        stack.approvals.decide(
            approvals[0].approval_id, decision="approved",
            actor=HumanIdentity.local_cli(),
        )
        child_engine = engine_for(stack, child_model)
        child_result = child_engine.resume_run(child_run_id)
        assert child_result.state == "succeeded", child_result

        # 磁盘真的变了，且 Receipt 可独立核对（sha256 与磁盘字节一致）。
        assert (ws / "doc.md").read_text(encoding="utf-8") == content
        results = _results(log, child_run_id)
        (write_result,) = [
            p for p in results.values() if p.get("tool_id") == "modify_text"
        ]
        assert write_result["status"] == "succeeded"
        assert write_result["effect_state"] == "applied"
        import hashlib

        on_disk = (ws / "doc.md").read_bytes()
        assert write_result["output"]["sha256"] == hashlib.sha256(on_disk).hexdigest()
        intents = _events(log, child_run_id, "tool.intent")
        (intent,) = [p for p in intents if p["tool_id"] == "modify_text"]
        assert intent["approval_id"] == approvals[0].approval_id
        assert intent["grant_id"], "执行票据必须绑定一次性 grant"

        # 子任务收束 + 父作业被唤醒（同一事务），父 Run 继续收尾。
        (after,) = stack.tasks.list_subtasks(parent_run_id)
        assert after.state == "succeeded"
        assert after.summary == "改好了。", after.summary
        assert after.result_ref and after.result_ref.startswith("content://sha256:")
        assert stack.claims.job_for_run(parent_run_id).state == "pending"
        parent_model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("r1", "subtask.result", {"subtask_id": "writer"})],
                ),
                ModelResponse("end_turn", content="子任务改好了，见引用。"),
            ]
        )
        parent_engine = engine_for(stack, parent_model)
        parent_final = parent_engine.resume_run(parent_run_id)
        assert parent_final.state == "succeeded", parent_final

        # 子 Run 与父 Run 都可回放（写链不破坏 messages_hash 校验）。
        child_report = replay(log, child_run_id)
        assert child_report.ok, child_report.problems
        parent_report = replay(log, parent_run_id)
        assert parent_report.ok, parent_report.problems
    finally:
        log.close()
