"""F10 记忆派发 / 授权 / 事务契约（**真路径，不 mock 授权**）。

这一组测试盯的是最高危的那条线：记忆写操作**真的**经唯一派发入口
（`Dispatcher.prepare()` / `dispatch()`），并因此获得：

- `tool.intent` → `tool.result` 的顺序；
- confirm / forget 的 C4 审批（一次性消费、执行侧二次核验）；
- 与 `tool.result` **同事务**的投影更新（失败时事件/投影一起回滚）；
- 执行侧独立的作用域核验（workspace A 读不到 workspace B 的私有记忆）。

这里用真实的 `EventLog` + `MemoryProjection`。**没有 mock 掉授权**：审批、
grant、dispatch 记录都从 sqlite 权威库读。唯一被替换的是"制造故障"的那一处
（`test_mid_write_failure_is_unknown_and_leaves_no_half_projection` 故意让
`apply_event` 抛错），因为真实 I/O 不会按测试要求坏掉。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tianshu.dispatcher import Dispatcher, build_request
from tianshu.errors import TianshuError, ToolNotAllowed
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.memory import MEMORY_KINDS, MemoryProjection
from tianshu.permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_OVERWRITE,
    ActionRequest,
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
    supported_memory_kinds,
)
from tianshu.receipt import verify_receipts
from tianshu.resources import MemoryScope
from tianshu.rules import REJECTION_RULE_IDS, RULE_IDS
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, ExecutionPermit, TaskStore
from tianshu.tools import SPECS, AdmittedCall, tool_manifest

NOW = 1_700_000_000_000


@dataclass
class Env:
    root: Path
    ws_a: Path
    ws_b: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    memory: MemoryProjection
    dispatcher: Dispatcher

    def new_run(self, *, root: Path | None = None):
        root = self.ws_a if root is None else root
        task = self.tasks.create_task("整理笔记", [{"root_path": str(root)}])
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([root], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim

    def new_empty_run(self):
        """零目录任务：user 作用域的记忆仍然可用（F10 §6.1）。"""
        task = self.tasks.create_task("只聊天", [])
        run = self.tasks.start_run(task.task_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim

    def request(self, task, run, claim, *, tool_id, target, args, call_id):
        return build_request(
            task=task,
            run=run,
            claim=claim,
            call_id=call_id,
            tool_id=tool_id,
            target=target,
            args=args,
        )


@pytest.fixture()
def env(tmp_path: Path):
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / "note.md").write_bytes("原始内容\n".encode())
    data = tmp_path / "authority"
    data.mkdir()

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    memory = MemoryProjection(
        log.conn,
        log=log,
        blob_dir=tmp_path / "memory_blobs",
        clock=lambda: NOW,
    )
    memory.ensure_schema()
    leases = LeaseStore(log)
    claims = JobStore(log)
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=data)
    approvals = ApprovalStore(log, clock=lambda: NOW)
    grants = GrantStore(log, clock=lambda: NOW)
    snapshots = SnapshotStore(tmp_path / "artifacts")
    dispatcher = Dispatcher(
        log,
        tasks,
        leases,
        PolicyEngine(),
        approvals,
        grants=grants,
        claims=claims,
        snapshots=snapshots,
        data_root=data,
        memory=memory,
        clock=lambda: NOW,
    )
    e = Env(
        root=tmp_path,
        ws_a=ws_a,
        ws_b=ws_b,
        log=log,
        leases=leases,
        claims=claims,
        tasks=tasks,
        approvals=approvals,
        grants=grants,
        snapshots=snapshots,
        memory=memory,
        dispatcher=dispatcher,
    )
    try:
        yield e
    finally:
        log.close()


def _events(log: EventLog, run_id: str):
    return log.read_run(run_id)


def _event_types(log: EventLog, run_id: str) -> list[str]:
    return [event.type for event in _events(log, run_id)]


def _approve(env: Env, approval_id: str) -> None:
    env.approvals.decide(
        approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )


def _propose(env: Env, task, run, claim, *, content: str, call_id="p1", scope=None):
    scope = MemoryScope.user() if scope is None else scope
    args = {"kind": "preference", "content": content}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.propose",
        target=scope.to_uri(),
        args=args,
        call_id=call_id,
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    return request, args, decision


def _proposed(env: Env, *, content="报告用中文，先列结论") -> tuple:
    task, run, claim = env.new_run()
    request, args, decision = _propose(env, task, run, claim, content=content)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok and result.effect_state == "applied", result.output
    return task, run, claim, result.output


def _confirm_request(
    env: Env,
    task,
    run,
    claim,
    memory_id: str,
    *,
    revision: int = 1,
    call_id="c1",
    target: str | None = None,
):
    target = MemoryScope.user().to_uri() if target is None else target
    args = {"memory_id": memory_id, "revision": revision}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.confirm",
        target=target,
        args=args,
        call_id=call_id,
    )
    return request, args


def _forget_request(
    env: Env,
    task,
    run,
    claim,
    memory_id: str,
    *,
    revision: int = 2,
    call_id="f1",
):
    target = MemoryScope.user().to_uri()
    args = {"memory_id": memory_id, "revision": revision, "status": "active"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.forget",
        target=target,
        args=args,
        call_id=call_id,
    )
    return request, args


def _seed_memory(
    env: Env,
    run_id: str,
    *,
    memory_id: str,
    content: str,
    scope: MemoryScope | None = None,
    origin: str = "user_explicit",
) -> None:
    """直接写一条历史记忆事件（模拟之前的 propose 已提交）。

    这是**测试数据播种**，不是绕过派发：后续 confirm/forget 仍然走真实
    prepare/dispatch/审批/执行侧核验。
    """
    scope = MemoryScope.user() if scope is None else scope
    content_ref = env.memory.put_content(content)
    payload = {
        "call_id": "seed",
        "tool_id": "memory.propose",
        "status": "succeeded",
        "effect_state": "applied",
        "output": {
            "path": scope.to_uri(),
            "memory_uri": scope.to_uri(),
            "memory_id": memory_id,
            "revision": 1,
            "scope": scope.scope,
            "scope_id": scope.scope_id,
            "kind": "preference",
            "content_ref": content_ref,
            "source_refs": ["event:seed"],
            "evidence": [],
            "proof_count": 0,
            "origin": origin,
            "status": "candidate",
            "created_at": 1,
            "expires_at": None,
            "deduped": False,
        },
    }
    with env.log.transaction() as tx:
        seq = tx.append(run_id, "tool.result", payload)
        env.memory.apply_event("tool.result", payload, seq=seq)


# ---------------------------------------------------------------- 1. 工具契约


def test_memory_tools_are_real_specs_without_path_args():
    names = {item["name"] for item in tool_manifest()}
    memory_tools = {
        "memory.search",
        "memory.get",
        "memory.propose",
        "memory.confirm",
        "memory.forget",
        "memory.edit",
    }
    assert memory_tools <= names

    expected_actions = {
        "memory.search": "read",
        "memory.get": "read",
        "memory.propose": ACTION_CREATE,
        "memory.confirm": ACTION_OVERWRITE,
        "memory.forget": ACTION_DELETE,
        "memory.edit": ACTION_OVERWRITE,
    }
    # 契约 v1.2：propose 升 2.0（加 scope/scope_id）；其余工具仍是 1.0。
    expected_versions = {
        "memory.search": "1.0",
        "memory.get": "1.0",
        "memory.propose": "2.0",
        "memory.confirm": "1.0",
        "memory.forget": "1.0",
        "memory.edit": "1.0",
    }
    for tool_id, action in expected_actions.items():
        spec = SPECS[tool_id]
        assert spec.version == expected_versions[tool_id]
        assert spec.exposed is True
        assert spec.path_arg == ""
        assert spec.action == action
        # schema 是真实 schema（不是手写替身）；additionalProperties 必须关闭
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema["additionalProperties"] is False

    # 控制面专属工具：注册在 SPECS 里，但**不**出现在模型清单中（exposed=False）。
    remember = SPECS["memory.remember"]
    assert remember.version == "1.0"
    assert remember.exposed is False
    assert remember.action == ACTION_CREATE
    assert remember.path_arg == ""
    assert "memory.remember" not in names
    promote = SPECS["memory.promote"]
    assert promote.version == "1.0"
    assert promote.exposed is False
    assert promote.action == ACTION_OVERWRITE
    assert promote.path_arg == ""
    assert "memory.promote" not in names

    # 授权层不能维护第二份"看起来一样"的 kind 封闭集：memory.py 存在时必须
    # 直接用它。回退集只在集成前缺模块时兜底。
    assert supported_memory_kinds() == MEMORY_KINDS


# ---------------------------------------------------------------- 2. 真走派发


def test_propose_really_goes_through_dispatcher_and_orders_intent_before_result(env):
    task, run, claim = env.new_run()
    request, args, decision = _propose(env, task, run, claim, content="报告用中文")
    assert decision.is_admitted, decision
    assert decision.rule_id == "c4.memory_scope"

    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    assert result.effect_state == "applied"

    events = _events(env.log, run.run_id)
    types = [event.type for event in events]
    assert "tool.intent" in types and "tool.result" in types
    assert types.index("tool.intent") < types.index("tool.result")

    # F10 §3.1：事件日志只存引用/审计元数据，不重复写记忆正文。
    intent = next(event for event in events if event.type == "tool.intent")
    assert "content" not in intent.payload["args"]
    assert intent.payload["args"]["content_omitted"] is True
    assert intent.payload["args"]["content_bytes"] == len("报告用中文".encode())
    assert intent.payload["args"]["content_sha256"] == sha256_hex(
        "报告用中文".encode()
    )
    assert intent.payload["args_hash"] == request.args_hash
    assert "报告用中文" not in canonical_bytes(intent.payload).decode("utf-8")
    result_event = next(event for event in events if event.type == "tool.result")
    assert "报告用中文" not in canonical_bytes(result_event.payload).decode("utf-8")

    memory = env.memory.get(result.output["memory_id"], allowed=(MemoryScope.user(),))
    assert memory is not None
    assert memory.status == "candidate"
    assert memory.revision == 1

    # 候选不会进入召回
    assert env.memory.search("报告", allowed=(MemoryScope.user(),)) == []

    # 实时投影与按事件重建必须逐位一致（dispatcher 传 intent_seq；
    # rebuild 必须用 dispatch.admitted.payload.intent_seq 还原同一个号）。
    before_rebuild = env.memory.projection_hash()
    assert env.memory.rebuild() == 1
    assert env.memory.projection_hash() == before_rebuild
    assert env.memory.get(result.output["memory_id"], allowed=(MemoryScope.user(),)) is not None

    # 派发记录与 grant 都是真实消费过的
    row = env.dispatcher.dispatch_record(run.run_id, request.call_id)
    assert row is not None and row.state == "done"
    grant = env.grants.get(row.grant_id)
    assert grant is not None and grant.remaining_uses == 0
    target = env.dispatcher._conn.execute(
        "SELECT target FROM dispatches WHERE dispatch_id=?", (row.dispatch_id,)
    ).fetchone()[0]
    assert target == MemoryScope.user().to_uri()


def test_propose_works_for_zero_directory_task(env):
    """记忆不是文件：零目录任务也能 propose / 检索 user 作用域。"""
    task, run, claim = env.new_empty_run()
    _, _, decision = _propose(env, task, run, claim, content="用户偏好：中文")
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    assert result.output["status"] == "candidate"


# ---------------------------------------------------------------- 3. 审批


def test_confirm_requires_approval_and_rejection_changes_no_projection(env):
    task, run, claim, proposed = _proposed(env)
    memory_id = proposed["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id)

    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == "waiting", decision
    assert decision.rule_id == "c4.overwrite"

    # 没有批准就派发：确定被拒，投影一个字节都不变
    refused = env.dispatcher.dispatch(decision.request_id, claim)
    assert refused.status == "rejected"
    assert refused.effect_state == "none_applied"
    assert refused.output["rule_id"] in REJECTION_RULE_IDS
    assert env.memory.observe(memory_id, allowed=(MemoryScope.user(),)) == (
        "candidate",
        1,
    )
    assert "receipt" not in _event_types(env.log, run.run_id)

    # 批准之后重新判定同一张请求：审批仍然可用（同一 call_id / 同一动作），
    # 只是上一次在'还没批准'时被派发调用拒绝过，state 需要重新落成 admitted。
    _approve(env, decision.approval_id)
    decision2 = env.dispatcher.prepare(request, claim, args=args)
    assert decision2.is_admitted, decision2
    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.ok, result.output

    memory = env.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert memory is not None and memory.status == "active" and memory.revision == 2
    assert env.memory.search("报告", allowed=(MemoryScope.user(),))
    assert verify_receipts(env.log, run.run_id) == []

    receipts = [event.payload for event in _events(env.log, run.run_id) if event.type == "receipt"]
    assert receipts, "confirm 必须产出凭据"
    receipt = receipts[-1]
    assert receipt["root"] == MemoryScope.user().to_uri()
    assert receipt["resource"] == memory_id
    assert receipt["action"] == ACTION_OVERWRITE
    assert receipt["after_state"] == f"memory:{memory_id}:active:rev2"


def test_forget_requires_approval_and_tombstones(env):
    task, run, claim, proposed = _proposed(env)
    memory_id = proposed["memory_id"]

    # 先确认成 active
    confirm, confirm_args = _confirm_request(env, task, run, claim, memory_id)
    first = env.dispatcher.prepare(confirm, claim, args=confirm_args)
    _approve(env, first.approval_id)
    assert env.dispatcher.dispatch(first.request_id, claim).ok

    forget, forget_args = _forget_request(env, task, run, claim, memory_id)
    decision = env.dispatcher.prepare(forget, claim, args=forget_args)
    assert decision.kind == "waiting", decision
    assert decision.rule_id == "c4.delete"

    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output

    memory = env.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert memory is not None and memory.status == "forgotten"
    assert env.memory.search("报告", allowed=(MemoryScope.user(),)) == []
    receipts = [event.payload for event in _events(env.log, run.run_id) if event.type == "receipt"]
    assert receipts[-1]["action"] == ACTION_DELETE
    assert receipts[-1]["after_state"] == f"memory:{memory_id}:forgotten:rev3"
    assert verify_receipts(env.log, run.run_id) == []


def test_user_explicit_confirm_needs_no_approval(env):
    """用户明示来源的确认免审批；执行侧从投影读 origin，不信模型参数。"""
    task, run, claim = env.new_run()
    _seed_memory(env, run.run_id, memory_id="mem_explicit", content="用户明确要求：中文")
    request, args = _confirm_request(
        env, task, run, claim, "mem_explicit", call_id="exp1"
    )

    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_admitted, decision
    assert decision.rule_id == "c4.memory_scope"
    assert decision.approval_id is None

    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    assert env.memory.observe("mem_explicit", allowed=(MemoryScope.user(),)) == (
        "active",
        2,
    )


# ---------------------------------------------------------------- 4. 作用域越权


def test_workspace_scope_is_denied_cross_task_and_rule_is_registered(env):
    task_a, run_a, claim_a = env.new_run()
    task_b, run_b, claim_b = env.new_run(root=env.ws_b)
    scope_b = MemoryScope.workspace(env.ws_b)
    _seed_memory(
        env,
        run_b.run_id,
        memory_id="mem_b_private",
        content="B 工作区的私有记忆",
        scope=scope_b,
    )

    # A 的任务拿着 B 的 URI 去确认：必须在派发前被策略/权威层拒绝
    scope_b_uri = scope_b.to_uri()
    args = {"memory_id": "mem_b_private", "revision": 1}
    request = env.request(
        task_a,
        run_a,
        claim_a,
        tool_id="memory.confirm",
        target=scope_b_uri,
        args=args,
        call_id="cross1",
    )
    decision = env.dispatcher.prepare(request, claim_a, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_scope_denied"
    assert decision.rule_id in RULE_IDS

    rejected = [
        event
        for event in _events(env.log, run_a.run_id)
        if event.type == "dispatch.rejected"
    ]
    assert rejected and rejected[-1].payload["rule_id"] == "c4.memory_scope_denied"

    # 存储层也没变：B 的记忆仍是 candidate
    assert env.memory.observe("mem_b_private", allowed=(scope_b,)) == ("candidate", 1)


def test_model_cannot_propose_into_workspace_scope(env):
    """契约 v1.2：workspace 是共享边界，模型不能直接往里写。

    写进去的路径只有一条：人经 `memory.promote`（N1 后续提交）。模型即使
    本任务绑定了这个目录，也只能提候选到 user / 本任务 task 作用域。
    """
    task, run, claim = env.new_run()
    scope = MemoryScope.workspace(env.ws_a)
    _request, _args, decision = _propose(
        env, task, run, claim, content="这个工作区里的约定", scope=scope
    )
    # 拒绝点已经提前到派发层（执行侧还有同名核验）：不再"先放行再执行失败"。
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_scope_denied"
    assert env.memory.list_memories(allowed=(scope,)) == []


# ---------------------------------------------------------------- 5. 事务性


def test_projection_update_runs_inside_tool_result_transaction(env):
    """apply_event 必须在 tool.result 的同一事务内被调用。

    不是断言"最后两边都在"——那只能证明事后重试补上了。这里在调用瞬间读
    `log.in_transaction`，证明它没有另开/延后事务。
    """
    task, run, claim, _proposed_output = _proposed(env)
    memory_id = _proposed_output["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id, revision=1)
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)

    original = env.memory.apply_event
    seen: list[bool] = []

    def wrapped(event_type, payload, *, seq):
        seen.append(env.log.in_transaction)
        return original(event_type, payload, seq=seq)

    env.memory.apply_event = wrapped  # type: ignore[method-assign]
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    assert seen == [True], f"apply_event 必须在同一事务内：{seen}"


def test_mid_write_failure_is_unknown_and_leaves_no_half_projection(env, monkeypatch):
    task, run, claim, proposed = _proposed(env)
    memory_id = proposed["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id, revision=1)
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)

    def explode(event_type, payload, *, seq):
        raise RuntimeError("投影写入中途炸了")

    monkeypatch.setattr(env.memory, "apply_event", explode)

    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.status == "failed"
    assert result.effect_state == "unknown"

    # 投影没有半条：还是 candidate/1，没有成功 result、没有 receipt
    assert env.memory.observe(memory_id, allowed=(MemoryScope.user(),)) == (
        "candidate",
        1,
    )
    events = _events(env.log, run.run_id)
    results = [event.payload for event in events if event.type == "tool.result"]
    assert results[-1]["tool_id"] == "memory.confirm"
    assert results[-1]["status"] == "failed"
    assert results[-1]["effect_state"] == "unknown"
    assert "receipt" not in [event.type for event in events]
    assert "recovery.blocked" not in [event.type for event in events]
    row = env.dispatcher.dispatch_record(run.run_id, request.call_id)
    assert row is not None and row.state == "error"


# ---------------------------------------------------------------- 6. 老 file 路径不退化


def test_file_dispatch_still_uses_bare_absolute_path_and_receipt(env):
    task, run, claim = env.new_run()
    target = env.ws_a / "note.md"
    args = {"path": "note.md", "content": "改后的内容\n"}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id="file1",
        tool_id="modify_text",
        target=target,
        args=args,
    )
    # 历史形状：resources 是裸绝对路径，逐字节不变；没有记忆字段混进来
    assert request.resources == (str(target),)
    assert "memory" not in request.to_canonical_dict()
    assert request.action == ACTION_OVERWRITE

    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == "waiting"
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    assert target.read_text(encoding="utf-8") == "改后的内容\n"

    receipts = [event.payload for event in _events(env.log, run.run_id) if event.type == "receipt"]
    assert receipts, "文件覆盖必须仍产出凭据"
    assert receipts[-1]["root"] == str(env.ws_a)
    assert receipts[-1]["resource"] == str(target)
    assert receipts[-1]["action"] == ACTION_OVERWRITE
    assert verify_receipts(env.log, run.run_id) == []

def test_duplicate_propose_is_policy_denied_before_approval(env):
    task, run, claim, first = _proposed(env, content="重复的候选")
    args = {"kind": "preference", "content": "重复的候选"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.propose",
        target=MemoryScope.user().to_uri(),
        args=args,
        call_id="dup1",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.create_over_existing"
    assert decision.rule_id in RULE_IDS
    # 原候选没有变化
    assert env.memory.observe(first["memory_id"], allowed=(MemoryScope.user(),)) == (
        "candidate",
        1,
    )


def test_tool_and_resource_kind_must_match(env):
    """文件工具拿 memory://、记忆工具拿绝对路径，都必须 fail-closed。"""
    task, run, claim = env.new_run()
    file_request = env.request(
        task,
        run,
        claim,
        tool_id="modify_text",
        target=env.ws_a / "note.md",
        args={"path": "note.md", "content": "x"},
        call_id="kind1",
    )
    forged_file = replace(
        file_request,
        tool_id="memory.propose",
        tool_version=SPECS["memory.propose"].version,
        schema_hash=sha256_hex(canonical_bytes(SPECS["memory.propose"].input_schema)),
        action=ACTION_CREATE,
        args_hash=sha256_hex(
            canonical_bytes({"kind": "preference", "content": "x"})
        ),
    )
    decision = env.dispatcher.prepare(
        forged_file,
        claim,
        args={"kind": "preference", "content": "x"},
    )
    assert decision.is_rejected and decision.rule_id == "dispatch.memory_resource_mismatch"
    assert decision.rule_id in RULE_IDS

    # 反方向：文件工具拿 memory://
    mem_args = {"path": "note.md", "content": "x"}
    mem_request = ActionRequest(
        request_id="req_kind2",
        task_id=task.task_id,
        run_id=run.run_id,
        job_id=claim.job_id,
        claim_epoch=claim.claim_epoch,
        owner_instance_id=claim.owner_instance_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        call_id="kind2",
        attempt_id=1,
        tool_id="modify_text",
        tool_version=SPECS["modify_text"].version,
        schema_hash=sha256_hex(canonical_bytes(SPECS["modify_text"].input_schema)),
        action=ACTION_OVERWRITE,
        resources=(MemoryScope.user().to_uri(),),
        args_hash=sha256_hex(canonical_bytes(mem_args)),
        preconditions={"before_state": "x"},
    )
    decision2 = env.dispatcher.prepare(mem_request, claim, args=mem_args)
    assert decision2.is_rejected
    assert decision2.rule_id == "dispatch.memory_resource_mismatch"


def test_absolute_file_path_with_double_colon_slash_stays_a_file(env):
    """`/a/b://c` 在 POSIX 是普通绝对路径，不能因为含 `://` 被当成记忆。"""
    weird = f"{env.ws_a}/b://c"  # 合法绝对路径：目录名 `b:` + 文件名 `c`
    decision = PolicyEngine().classify(
        ACTION_CREATE,
        [weird],
        [{"root_path": str(env.ws_a)}],
    )
    assert decision.rule_id == "c4.auto", decision


def test_memory_executor_refuses_a_forged_ticket_without_a_dispatch_record(env):
    """票据可伪造，库里的派发记录不能。执行器必须自己回权威库核。"""
    task, run, claim = env.new_run()
    args = {"kind": "preference", "content": "伪造"}
    req = ActionRequest(
        request_id="req_forged_mem",
        task_id=task.task_id,
        run_id=run.run_id,
        job_id=claim.job_id,
        claim_epoch=claim.claim_epoch,
        owner_instance_id=claim.owner_instance_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        call_id="forged_mem",
        attempt_id=1,
        tool_id="memory.propose",
        tool_version="1.0",
        schema_hash=sha256_hex(canonical_bytes(SPECS["memory.propose"].input_schema)),
        action=ACTION_CREATE,
        resources=(MemoryScope.user().to_uri(),),
        args_hash=sha256_hex(canonical_bytes(args)),
        preconditions={"memory_kind": "preference", "memory_exists": False},
    )
    forged = AdmittedCall(
        dispatch_id="disp_forged_mem",
        request=req,
        request_digest=req.digest(),
        claim=claim,
        tool_id="memory.propose",
        action=ACTION_CREATE,
        args=args,
        target=MemoryScope.user().to_uri(),
        rel_path="",
        workspace_root="local",
        resource_key="memory:user:local",
        resources=(MemoryScope.user().to_uri(),),
        intent_seq=0,
    )
    permit = ExecutionPermit(
        run_id=run.run_id,
        task_id=task.task_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        resources=(MemoryScope.user().to_uri(),),
        seq=1,
        started_at=0,
    )
    executor = env.dispatcher._memory_executor_for()
    with pytest.raises(ToolNotAllowed) as excinfo:
        executor.execute(forged, permit)
    assert "派发记录" in str(excinfo.value)


def test_task_scope_is_only_for_its_own_task(env):
    task, run, claim = env.new_run()
    scope = MemoryScope.task(task.task_id)
    _, _, decision = _propose(env, task, run, claim, content="本任务的上下文", scope=scope)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    memory_id = result.output["memory_id"]

    # 本任务可以观察
    assert env.memory.observe(memory_id, allowed=(scope,)) == ("candidate", 1)

    # 另一个任务拿着同一个 URI 来确认：作用域不属于它
    other_task, other_run, other_claim = env.new_run(root=env.ws_b)
    args = {"memory_id": memory_id, "revision": 1}
    request = env.request(
        other_task,
        other_run,
        other_claim,
        tool_id="memory.confirm",
        target=scope.to_uri(),
        args=args,
        call_id="task_cross",
    )
    rejected = env.dispatcher.prepare(request, other_claim, args=args)
    assert rejected.is_rejected
    assert rejected.rule_id == "c4.memory_scope_denied"


def test_unknown_memory_kind_is_policy_denied(env):
    task, run, claim = env.new_run()
    args = {"kind": "telepathy", "content": "没有这种种类"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.propose",
        target=MemoryScope.user().to_uri(),
        args=args,
        call_id="badkind",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_kind_unsupported"
    assert decision.rule_id in RULE_IDS


def test_zero_binding_control_run_cannot_write_files(env):
    """零绑定控制 Run 是给记忆 user 作用域的，不是文件写入的新后门。"""
    control = env.dispatcher.open_control_run(goal="检查", workspaces=[])
    try:
        target = env.ws_a / "note.md"
        args = {"path": "note.md", "content": "不允许"}
        request = build_request(
            task=control.task,
            run=control.run,
            claim=control.claim,
            call_id="control_file",
            tool_id="modify_text",
            target=target,
            args=args,
        )
        decision = env.dispatcher.prepare(request, control.claim, args=args)
        assert decision.is_rejected, decision
        assert decision.rule_id in RULE_IDS
        assert target.read_text(encoding="utf-8") == "原始内容\n"
    finally:
        env.dispatcher.close_control_run(
            control, outcome="failed", effect_state="none_applied"
        )


def test_dispatcher_refuses_a_projection_from_another_event_log(env) -> None:
    """错装配的投影必须在装配处就炸，不许"先跑通再说"。

    投影更新必须和事件在同一个事务里。传进一个**别的日志**的投影时，
    `_commit_result()` 会在 log1 的事务里写事件、却在 log2 的连接上独立提交
    投影：log1 回滚、投影留下——两份事实源静默分叉，而且没有裁决者能判定
    哪一份对。这条约束不能靠"装配代码记得传对"。
    """
    other_log = EventLog(env.root / "other.db", blob_dir=env.root / "other_blobs")
    foreign = MemoryProjection(other_log.conn, log=other_log, clock=lambda: NOW)
    foreign.ensure_schema()
    with pytest.raises(TianshuError):
        Dispatcher(
            env.log,
            env.tasks,
            env.leases,
            PolicyEngine(),
            env.approvals,
            grants=env.grants,
            claims=env.claims,
            snapshots=env.snapshots,
            memory=foreign,
            clock=lambda: NOW,
        )


def test_rejected_memory_write_keeps_no_raw_content_in_the_request_table(env) -> None:
    """被拒绝的记忆写操作，审计表里也不留正文原文。

    拒绝的请求**永远不会执行**，所以在这里脱敏不会破坏任何派发/审批绑定
    （执行器的参数也从这张表取，所以"被采纳"的行绝不能脱敏）。而
    `dispatch_requests` 没有擦除路径、`memory.forget` 也不碰它——原文落进去
    就是一份永久副本。
    """
    body = "重复的候选 SECRET-1234"
    task, run, claim, _first = _proposed(env, content=body)
    args = {"kind": "preference", "content": body}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.propose",
        target=MemoryScope.user().to_uri(),
        args=args,
        call_id="dup-raw",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision

    rows = env.log.conn.execute(
        "SELECT args_payload FROM dispatch_requests WHERE request_id = ?",
        (request.request_id,),
    ).fetchall()
    assert len(rows) == 1
    stored = rows[0][0] or ""
    assert "SECRET-1234" not in stored
    # 审计仍然可核对"要写的是哪一段正文"：哈希与长度都在。
    assert sha256_hex(body.encode()) in stored


# ---------------------------------------------------------------- 6. 契约 v1.2：控制面与作用域


def test_control_run_can_forget_an_old_task_scope_memory(env: Env):
    """控制面能替**旧 task** 的记忆做确认/遗忘，且不因此拿到目录租约。"""
    task, run, claim = env.new_run()
    scope = MemoryScope.task(task.task_id)
    _request, _args, decision = _propose(
        env, task, run, claim, content="这个任务的约定", scope=scope
    )
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    memory_id = result.output["memory_id"]
    assert env.memory.observe(memory_id, allowed=(scope,)) == ("candidate", 1)
    env.tasks.finish_run(run.run_id, outcome="succeeded", effect_state="applied")

    control = env.dispatcher.open_control_run(
        goal=f"memory.forget:{memory_id}", workspaces=[], memory_scopes=[scope]
    )
    assert control.task.is_control is True
    assert control.memory_scopes == (scope.to_uri(),)
    assert control.lease_paths == ()

    args = {"memory_id": memory_id, "revision": 1, "status": "candidate"}
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.forget",
        target=scope.to_uri(),
        args=args,
        call_id="ctl-forget-1",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=args)
    assert decision.is_waiting, decision
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, control.claim)
    try:
        assert result.ok, result.output
        assert env.memory.observe(memory_id, allowed=(scope,)) == ("forgotten", 2)
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=result.effect_state
        )


def test_remember_is_control_plane_only(env: Env):
    """普通任务不能用控制面专属工具；控制 Run 用它创建 active + user_explicit。"""
    user = MemoryScope.user()
    args = {"kind": "preference", "content": "以后用中文回答"}

    task, run, claim = env.new_run()
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.remember",
        target=user.to_uri(),
        args=args,
        call_id="remember-1",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_control_scope_binding"

    control = env.dispatcher.open_control_run(goal="memory.remember", workspaces=[])
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.remember",
        target=user.to_uri(),
        args=args,
        call_id="remember-2",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=args)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, control.claim)
    try:
        assert result.ok, result.output
        assert result.output["status"] == "active"
        assert result.output["origin"] == "user_explicit"
        memory = env.memory.get(result.output["memory_id"], allowed=(user,))
        assert memory is not None
        assert (memory.status, memory.origin, memory.revision) == ("active", "user_explicit", 1)
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=result.effect_state
        )


def test_human_approval_upgrades_origin_to_user_confirmed(env: Env):
    task, run, claim, output = _proposed(env)
    memory_id = output["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id, revision=1)
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    memory = env.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert memory is not None
    assert memory.status == "active"
    assert memory.origin == "user_confirmed"


def test_control_marker_and_scope_declaration_survive_rebuild(env: Env):
    scope = MemoryScope.task("task_old_scope")
    control = env.dispatcher.open_control_run(
        goal="memory.confirm:mem_x", workspaces=[], memory_scopes=[scope]
    )
    env.tasks.rebuild_from_events()
    task = env.tasks.get_task(control.task.task_id)
    assert task is not None
    assert task.is_control is True
    assert task.memory_scopes == (scope.to_uri(),)


def test_model_can_only_propose_into_its_own_task_scope(env: Env):
    task, run, claim = env.new_run()
    own = MemoryScope.task(task.task_id)
    _request, _args, decision = _propose(
        env, task, run, claim, content="本任务的事实", scope=own, call_id="p-own"
    )
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    assert env.memory.observe(result.output["memory_id"], allowed=(own,)) == (
        "candidate",
        1,
    )

    other = MemoryScope.task("task_other")
    args = {"kind": "fact", "content": "别人的任务"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.propose",
        target=other.to_uri(),
        args=args,
        call_id="p-other",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_scope_denied"


def test_workspace_propose_is_refused_even_on_a_control_task(env: Env):
    """控制面标记是 **Task 级**的：同一 Task 上后开的模型 Run 也不能写 workspace。

    独立复审复现过这个洞：CLI 为 workspace 作用域的确认/遗忘创建的控制任务带
    `is_control=1` 与 workspace 绑定；人再在这个 task 上开一个模型 Run，模型
    `memory.propose` 到 workspace 会成功（继承控制面身份）。现在 propose→workspace
    **无条件**拒，共享写只能走 exposed=False 的 `memory.promote`。
    """
    scope = MemoryScope.workspace(env.ws_a)
    control = env.dispatcher.open_control_run(
        goal="memory.confirm:mem_x",
        workspaces=[str(env.ws_a)],
        memory_scopes=[scope],
    )
    env.dispatcher.close_control_run(
        control, outcome="succeeded", effect_state="none_applied"
    )

    run = env.tasks.start_run(control.task.task_id)
    env.leases.acquire([env.ws_a], run.run_id)
    job = env.claims.enqueue(run.run_id)
    claim = env.claims.claim(job.job_id)
    env.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)

    args = {"kind": "fact", "content": "共享边界试探"}
    request = env.request(
        control.task,
        run,
        claim,
        tool_id="memory.propose",
        target=scope.to_uri(),
        args=args,
        call_id="ws-probe",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_scope_denied"
    assert env.memory.list_memories(allowed=(scope,)) == []


# ---------------------------------------------------------------- 7. 编辑与提升（N1 后半）


def test_edit_with_approval_keeps_old_row_and_activates_new(env: Env):
    """一次审批 = 旧版退役 + 新版立即生效；旧行 revision 不变、仍可按 id 查到。"""
    user = MemoryScope.user()
    task, run, claim, output = _proposed(env, content="报告用中文，先列结论")
    memory_id = output["memory_id"]

    args = {"memory_id": memory_id, "revision": 1, "content": "报告用中文，先列风险"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.edit",
        target=user.to_uri(),
        args=args,
        call_id="edit-1",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_waiting, decision
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok and result.effect_state == "applied", result.output

    old = env.memory.get(memory_id, allowed=(user,))
    assert old is not None
    assert (old.status, old.revision) == ("superseded", 1)
    new_id = result.output["memory_id"]
    new = env.memory.get(new_id, allowed=(user,))
    assert new is not None
    assert (new.status, new.origin, new.revision) == ("active", "user_confirmed", 1)
    assert new.supersedes == memory_id
    assert [hit.memory.memory_id for hit in env.memory.search("风险", allowed=(user,))] == [
        new_id
    ]
    assert env.memory.search("结论", allowed=(user,)) == []
    # 凭据可独立核对（edit 也产 Receipt）。
    verify_receipts(env.log, run_id=run.run_id)


def test_edit_requires_the_revision_the_human_saw(env: Env):
    user = MemoryScope.user()
    task, run, claim, output = _proposed(env)
    args = {
        "memory_id": output["memory_id"],
        "revision": 99,
        "content": "改个版本对不上的正文",
    }
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.edit",
        target=user.to_uri(),
        args=args,
        call_id="edit-stale",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "dispatch.memory_stale_revision"


def test_promote_is_control_only_and_moves_memory(env: Env):
    """提升是移动：原行退役（原作用域不再召回），目标作用域新行是候选。"""
    user = MemoryScope.user()
    target = MemoryScope.task("task_promoted")
    task, run, claim, output = _proposed(env, content="这条以后要给项目用")
    memory_id = output["memory_id"]
    args = {
        "memory_id": memory_id,
        "revision": 1,
        "scope": target.scope,
        "scope_id": target.scope_id,
    }

    # 普通任务（模型路径）不能提升：exposed=False + 派发层核 is_control。
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.promote",
        target=user.to_uri(),
        args=args,
        call_id="promote-model",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_control_scope_binding"

    control = env.dispatcher.open_control_run(
        goal=f"memory.promote:{memory_id}", workspaces=[], memory_scopes=[target]
    )
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.promote",
        target=user.to_uri(),
        args=args,
        call_id="promote-ctl",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=args)
    assert decision.is_waiting, decision
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, control.claim)
    try:
        assert result.ok, result.output
        old = env.memory.get(memory_id, allowed=(user,))
        assert old is not None and old.status == "superseded"
        new_id = result.output["memory_id"]
        promoted = env.memory.get(new_id, allowed=(target,))
        assert promoted is not None
        assert (promoted.status, promoted.scope, promoted.revision) == (
            "candidate",
            "task",
            1,
        )
        assert memory_id in promoted.source_refs
        assert env.memory.search("项目用", allowed=(user,)) == []
        assert verify_receipts(env.log, run_id=control.run.run_id) == []
        # 凭据必须针对**原行**（移动的是它），root 是原作用域。
        receipts = [
            event
            for event in env.log.read_run(control.run.run_id)
            if event.type == "receipt"
        ]
        assert receipts, "promote 必须产 Receipt"
        assert receipts[-1].payload["resource"] == memory_id
        assert receipts[-1].payload["root"] == user.to_uri()
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=result.effect_state
        )


def test_promote_to_workspace_canonicalizes_the_target_scope(env: Env):
    """提升到 workspace 时，参数里的原始路径必须归一到声明里的 canonical 根。

    否则 CLI / UI / 未来的 API 传 `/a/b/.` 会被判"不在控制 Run 的声明内"，
    而它和 `/a/b` 是同一个目录。
    """
    user = MemoryScope.user()
    canonical = MemoryScope.workspace(env.ws_a)
    raw = str(env.ws_a) + "/."
    _task, _run, _claim, output = _proposed(env, content="工作区共享约定")
    memory_id = output["memory_id"]
    control = env.dispatcher.open_control_run(
        goal=f"memory.promote:{memory_id}",
        workspaces=[],
        memory_scopes=[canonical],
    )
    args = {
        "memory_id": memory_id,
        "revision": 1,
        "scope": "workspace",
        "scope_id": raw,
    }
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.promote",
        target=user.to_uri(),
        args=args,
        call_id="promote-ws",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=args)
    assert decision.is_waiting, decision
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, control.claim)
    try:
        assert result.ok, result.output
        assert result.output["scope_id"] == canonical.scope_id
        promoted = env.memory.get(result.output["memory_id"], allowed=(canonical,))
        assert promoted is not None
        assert (promoted.scope, promoted.scope_id) == ("workspace", canonical.scope_id)
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=result.effect_state
        )


# ---------------------------------------------------------------- 8. 物理擦除（N4）


def test_erase_deletes_unique_blob_and_marks_erased(env: Env):
    """擦除：删正文副本 + 标 erased_at；重建不复活；回执列出删除项。"""
    user = MemoryScope.user()
    task, run, claim, output = _proposed(env, content="这条要被擦掉")
    memory_id = output["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id, revision=1)
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)
    assert env.dispatcher.dispatch(decision.request_id, claim).ok
    blob = env.memory.blob_path(output["content_ref"])
    assert blob is not None and blob.is_file()

    control = env.dispatcher.open_control_run(
        goal=f"memory.erase:{memory_id}", workspaces=[], memory_scopes=[]
    )
    erase_args = {"memory_id": memory_id, "revision": 2}
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.erase",
        target=user.to_uri(),
        args=erase_args,
        call_id="erase-1",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=erase_args)
    assert decision.is_waiting, decision
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, control.claim)
    try:
        assert result.ok, result.output
        assert result.output["erasure"]["deleted"] is True
        assert blob is not None and not blob.exists()
        memory = env.memory.get(memory_id, allowed=(user,))
        assert memory is not None
        assert memory.status == "forgotten"
        assert memory.erased_at is not None
        assert env.memory.read_content(memory) is None
        assert env.memory.search("擦掉", allowed=(user,)) == []

        # 重建不复活：事件里有 erased_at，重建后照旧不可读。
        env.memory.rebuild()
        revived = env.memory.get(memory_id, allowed=(user,))
        assert revived is not None
        assert revived.erased_at is not None
        assert env.memory.read_content(revived) is None

        assert verify_receipts(env.log, run_id=control.run.run_id) == []
        receipts = [
            event
            for event in env.log.read_run(control.run.run_id)
            if event.type == "receipt"
        ]
        erasures = [
            item
            for item in receipts[-1].payload["evidence"]
            if item.get("kind") == "memory_erasure"
        ]
        assert erasures and erasures[0]["deleted"] is True

        # 重复擦除是确定的拒绝，不是 unknown。
        again = {"memory_id": memory_id, "revision": 3}
        request2 = env.request(
            control.task,
            control.run,
            control.claim,
            tool_id="memory.erase",
            target=user.to_uri(),
            args=again,
            call_id="erase-2",
        )
        assert env.dispatcher.prepare(request2, control.claim, args=again).is_rejected
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=result.effect_state
        )


def test_erase_keeps_shared_blob_and_reports_retained_by(env: Env):
    """两条记忆共享同一 content_ref：擦一条只标不可用，另一条正文不能凭空消失。"""
    task_a = MemoryScope.task("task_erase_a")
    task_b = MemoryScope.task("task_erase_b")
    content = "两条记忆共享的同一段正文"

    def remember(scope: MemoryScope, call_id: str):
        control = env.dispatcher.open_control_run(
            goal=f"memory.remember:{scope.to_uri()}",
            workspaces=[],
            memory_scopes=[scope],
        )
        args = {"kind": "fact", "content": content}
        request = env.request(
            control.task,
            control.run,
            control.claim,
            tool_id="memory.remember",
            target=scope.to_uri(),
            args=args,
            call_id=call_id,
        )
        decision = env.dispatcher.prepare(request, control.claim, args=args)
        assert decision.is_admitted, decision
        result = env.dispatcher.dispatch(decision.request_id, control.claim)
        assert result.ok, result.output
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=result.effect_state
        )
        return result.output["memory_id"], result.output["content_ref"]

    id_a, ref_a = remember(task_a, "rem-a")
    id_b, ref_b = remember(task_b, "rem-b")
    assert ref_a == ref_b
    blob = env.memory.blob_path(ref_a)
    assert blob is not None and blob.is_file()

    def erase(memory_id: str, scope: MemoryScope, call_id: str):
        control = env.dispatcher.open_control_run(
            goal=f"memory.erase:{memory_id}", workspaces=[], memory_scopes=[scope]
        )
        args = {"memory_id": memory_id, "revision": 1}
        request = env.request(
            control.task,
            control.run,
            control.claim,
            tool_id="memory.erase",
            target=scope.to_uri(),
            args=args,
            call_id=call_id,
        )
        decision = env.dispatcher.prepare(request, control.claim, args=args)
        assert decision.is_waiting, decision
        _approve(env, decision.approval_id)
        result = env.dispatcher.dispatch(decision.request_id, control.claim)
        try:
            assert result.ok, result.output
            return result.output["erasure"]
        finally:
            env.dispatcher.close_control_run(
                control, outcome="succeeded", effect_state=result.effect_state
            )

    first = erase(id_a, task_a, "erase-a")
    assert first["deleted"] is False
    assert first["retained_by"] == [id_b]
    assert blob is not None and blob.is_file()
    still_there = env.memory.get(id_b, allowed=(task_b,))
    assert still_there is not None
    assert env.memory.read_content(still_there) == content
    # 文件仍被另一条引用（没删），重建后第一条照样不复活。
    env.memory.rebuild()
    erased_a = env.memory.get(id_a, allowed=(task_a,))
    assert erased_a is not None and erased_a.erased_at is not None
    assert env.memory.read_content(erased_a) is None
    assert env.memory.read_content(
        env.memory.get(id_b, allowed=(task_b,))
    ) == content

    second = erase(id_b, task_b, "erase-b")
    assert second["deleted"] is True
    assert blob is not None and not blob.exists()


def test_erase_is_control_plane_only(env: Env):
    user = MemoryScope.user()
    task, run, claim, output = _proposed(env, content="模型不能擦")
    args = {"memory_id": output["memory_id"], "revision": 1}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.erase",
        target=user.to_uri(),
        args=args,
        call_id="erase-model",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_control_scope_binding"
    assert "memory.erase" not in {item["name"] for item in tool_manifest()}


def test_erase_commit_runs_inside_the_execution_guard(env: Env):
    """擦除的引用计数/删文件/提交必须在**同一把跨进程守卫**内。

    否则两个进程同时擦共享同一 blob 的两条记忆，会各自看到"对方还没擦"→
    都保留文件 → 两行都标 erased、孤儿文件永久留存（复审探针复现）。
    这里直接把守卫换成可观测的上下文管理器，断言 commit 发生在守卫内。
    """
    from contextlib import contextmanager

    import tianshu.dispatcher as dispatcher_module

    depth = {"n": 0}
    real_guard = dispatcher_module.execution_guard

    @contextmanager
    def spy_guard(*args, **kwargs):
        depth["n"] += 1
        try:
            with real_guard(*args, **kwargs):
                yield
        finally:
            depth["n"] -= 1

    user = MemoryScope.user()
    task, run, claim, output = _proposed(env, content="守卫内的擦除")
    memory_id = output["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id, revision=1)
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)
    assert env.dispatcher.dispatch(decision.request_id, claim).ok

    control = env.dispatcher.open_control_run(
        goal=f"memory.erase:{memory_id}", workspaces=[], memory_scopes=[]
    )
    erase_args = {"memory_id": memory_id, "revision": 2}
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.erase",
        target=user.to_uri(),
        args=erase_args,
        call_id="erase-guard",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=erase_args)
    _approve(env, decision.approval_id)

    real_commit = env.dispatcher._commit_result
    seen: list[int] = []

    def spy_commit(*a, **k):
        seen.append(depth["n"])
        return real_commit(*a, **k)

    dispatcher_module.execution_guard = spy_guard  # type: ignore[assignment]
    env.dispatcher._commit_result = spy_commit  # type: ignore[method-assign]
    try:
        result = env.dispatcher.dispatch(decision.request_id, control.claim)
    finally:
        dispatcher_module.execution_guard = real_guard  # type: ignore[assignment]
        env.dispatcher._commit_result = real_commit  # type: ignore[method-assign]
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state="applied"
        )
    assert result.ok, result.output
    assert seen == [1], seen
    assert depth["n"] == 0



# ---------------------------------------------------------------- 9. 终态审计脱敏与崩溃恢复（N5）


def _request_row(env: Env, request_id: str):
    return env.log.conn.execute(
        "SELECT * FROM dispatch_requests WHERE request_id = ?", (request_id,)
    ).fetchone()


def test_accepted_memory_write_args_are_redacted_at_terminal_state(env: Env):
    """被采纳的请求进终态后，审计表不再保存记忆正文原文（F10 §13 #26）。"""
    import hashlib
    import json

    content = "以后用中文回答"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(env, task, run, claim, content=content)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output

    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 1
    stored = json.loads(row["args_payload"])
    assert "content" not in stored
    assert stored["content_redacted"] is True
    assert stored["content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert stored["content_bytes"] == len(content.encode("utf-8"))
    # 审计仍能核对"要写是哪条记忆/哪个作用域"。
    assert stored["kind"] == "preference"


def test_gate_blocked_memory_write_is_redacted_after_dispatch(env: Env):
    """被采纳又被内容闸门挡掉的请求：终态后同样脱敏。"""
    import json

    secret = "-----BEGIN RSA PRIVATE KEY-----MIIEow"
    user = MemoryScope.user()
    task, run, claim = env.new_run()
    args = {"kind": "fact", "content": secret}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.propose",
        target=user.to_uri(),
        args=args,
        call_id="blocked-propose",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert not result.ok
    assert result.effect_state == "none_applied"

    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 1
    stored = json.loads(row["args_payload"])
    assert "content" not in stored
    assert secret not in json.dumps(stored)
    assert env.memory.list_memories(allowed=(user,)) == []


def test_request_stored_before_approval_is_redacted_when_it_is_later_rejected(env):
    """`prepare()` 就落库的行：之后走拒绝路径（ON CONFLICT 分支）也必须脱敏。

    只修插入路径不够——需要审批的记忆写（`memory.edit` 带新正文）在 `prepare()`
    就存了 waiting 行，人否决 / 前置条件过期都会在**同一个 `request_id`** 上再写
    一次；那条 ON CONFLICT 分支若不覆盖 `args_payload`，正文就永久留在审计表里。
    """
    import json

    user = MemoryScope.user()
    content = "审批前就已经落库的新正文"
    task, run, claim, proposed = _proposed(env, content="报告用中文，先列结论")
    memory_id = proposed["memory_id"]

    args = {"memory_id": memory_id, "revision": 1, "content": content}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.edit",
        target=user.to_uri(),
        args=args,
        call_id="edit-rejected",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_waiting, decision
    row = _request_row(env, decision.request_id)
    assert row is not None and row["state"] == "waiting"
    assert json.loads(row["args_payload"])["content"] == content

    # 不批准直接派发：请求在**已存在的行**上被拒（ON CONFLICT 路径）
    refused = env.dispatcher.dispatch(decision.request_id, claim)
    assert refused.status == "rejected"
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert row["state"] == "rejected"
    assert int(row["args_redacted"]) == 1
    assert content not in row["args_payload"]


def test_memory_write_that_crashes_mid_projection_is_still_redacted(env, monkeypatch):
    """三种终态都要脱敏：执行中途炸掉（请求转 rejected / result unknown）也不例外。

    这条走的是 `_fail_unknown` 的记忆分支——它把请求收成 `rejected`，所以
    `_redact_terminal_args()` 照样会被调到。正文不能因为"没写成功"就永久留下。
    """
    import json

    content = "崩溃时还没提交的正文"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(env, task, run, claim, content=content)

    def explode(event_type, payload, *, seq):
        raise RuntimeError("投影写入中途炸了")

    monkeypatch.setattr(env.memory, "apply_event", explode)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.effect_state == "unknown"

    row = _request_row(env, decision.request_id)
    assert row is not None
    assert row["state"] == "rejected"
    assert int(row["args_redacted"]) == 1
    stored = json.loads(row["args_payload"])
    assert content not in json.dumps(stored)
    assert stored["content_bytes"] == len(content.encode("utf-8"))


def test_file_write_args_are_not_redacted(env: Env):
    """脱敏只针对记忆请求：文件写请求的审计原样保留。"""
    import json

    args = {"name": "report.md", "content": "正文内容"}
    task, run, claim = env.new_run()
    request = env.request(
        task,
        run,
        claim,
        tool_id="create_report",
        target=env.ws_a / "report.md",
        args=args,
        call_id="file-write",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 0
    assert json.loads(row["args_payload"])["content"] == "正文内容"


def test_recovery_marks_pending_memory_write_as_not_applied(env: Env):
    """有 intent 无 result 的记忆写：恢复判"未生效"，不写 unknown、不阻断目录。"""
    from tianshu.recovery import RecoveryReport, assess_run, recover_run

    user = MemoryScope.user()
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content="崩溃前写了一半的候选", call_id="crash-propose"
    )
    assert decision.is_admitted, decision
    # 模拟"intent 已提交、tool.result 未落库"的崩溃窗口：执行器在真正写入前
    # 被硬退（SystemExit 是 BaseException，dispatcher 不会把它接成 unknown）。
    executor = env.dispatcher._memory_executor_for()
    original = executor.execute

    def crash(admitted, permit):
        raise SystemExit("simulated power loss")

    executor.execute = crash  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit):
            env.dispatcher.dispatch(decision.request_id, claim)
    finally:
        executor.execute = original  # type: ignore[method-assign]

    data_root = env.root / "authority"
    assessment = assess_run(data_root, run.run_id, log=env.log)
    assert "crash-propose" in assessment.none_applied
    assert not assessment.blocks_takeover

    report: RecoveryReport = recover_run(
        env.log, run.run_id, env.ws_a, env.snapshots,
        leases=env.leases, data_root=data_root,
    )
    assert "crash-propose" in report.memory_not_applied
    assert "crash-propose" in report.none_applied
    assert report.blocked_workspaces == []
    assert env.memory.list_memories(allowed=(user,)) == []

    # 恢复之后可以重新 propose（新 call_id），投影不受影响。
    _request2, _args2, decision2 = _propose(
        env, task, run, claim, content="崩溃前写了一半的候选", call_id="crash-propose-retry"
    )
    assert decision2.is_admitted, decision2
    result = env.dispatcher.dispatch(decision2.request_id, claim)
    assert result.ok, result.output
    assert env.memory.observe(result.output["memory_id"], allowed=(user,)) == (
        "candidate",
        1,
    )
