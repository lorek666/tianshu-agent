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

import hashlib
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
from tianshu.recovery import assess_run, recover_run
from tianshu.resources import MemoryScope
from tianshu.rules import REJECTION_RULE_IDS, RULE_IDS
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, ExecutionPermit, TaskStore
from tianshu.tools import SPECS, AdmittedCall, tool_manifest

NOW = 1_700_000_000_000
RUN_NAME = "run_memory"


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


def _proposed(env: Env, *, content="报告用中文，先列结论", root=None) -> tuple:
    task, run, claim = env.new_run(root=root)
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


# ---------------------------------------------------------------- 10. 崩溃窗口：擦除 / 孤儿 blob


def _crash_at_commit(monkeypatch, dispatcher):
    """让 `_commit_result` 直接硬退，模拟"副作用已经发生、提交前进程没了"。

    `SystemExit` 是 BaseException：`dispatch()` 的 `except Exception` 不会把它
    接成 unknown（那条路会补一条 tool.result，等于伪造了一次"已经结算"）。
    """

    def crash(*args, **kwargs):
        raise SystemExit("simulated power loss before commit")

    monkeypatch.setattr(dispatcher, "_commit_result", crash)


def _recover(env: Env, run_id: str):
    return recover_run(
        env.log,
        run_id,
        env.ws_a,
        env.snapshots,
        leases=env.leases,
        data_root=env.root / "authority",
    )


def test_erase_crash_after_deletion_is_not_reported_as_none_applied(env, monkeypatch):
    """擦除的副作用在提交事务**之外**：崩溃窗口里"文件已删、行未标记"不可逆。

    恢复**不能**把它报成"一定没生效"——否则一个内容已经销毁的擦除会被宣布成
    "什么都没发生"。正确结论是 unknown（`memory_unresolved`），不阻断目录。
    """
    user = MemoryScope.user()
    _task, _run, _claim, output = _proposed(env, content="崩溃窗口里被删掉的正文")
    memory_id = output["memory_id"]
    blob = env.memory.blob_path(output["content_ref"])
    assert blob is not None and blob.is_file()
    control = env.dispatcher.open_control_run(
        goal=f"memory.erase:{memory_id}", workspaces=[], memory_scopes=[]
    )
    erase_args = {"memory_id": memory_id, "revision": 1}
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.erase",
        target=user.to_uri(),
        args=erase_args,
        call_id="crash-erase",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=erase_args)
    assert decision.is_waiting, decision
    _approve(env, decision.approval_id)

    _crash_at_commit(monkeypatch, env.dispatcher)
    try:
        with pytest.raises(SystemExit):
            env.dispatcher.dispatch(decision.request_id, control.claim)
    finally:
        env.dispatcher.close_control_run(
            control, outcome="failed", effect_state="none_applied"
        )

    # 崩溃现场：正文文件已经没了，但投影行仍是 candidate、没标 erased_at。
    assert not blob.exists()
    assert env.memory.content_ref_of(memory_id) == (output["content_ref"], None)
    current = env.memory.get(memory_id, allowed=(user,))
    assert current is not None
    assert env.memory.read_content(current) is None

    assessment = assess_run(env.root / "authority", control.run.run_id, log=env.log)
    assert "crash-erase" not in assessment.none_applied
    assert "crash-erase" in assessment.unknown
    assert assessment.blocks_takeover

    report = _recover(env, control.run.run_id)
    assert "crash-erase" in report.memory_unresolved
    assert "crash-erase" not in report.memory_not_applied
    assert "crash-erase" not in report.none_applied
    assert report.blocked_workspaces == []
    assert report.ok is False
    # 派发记录不能继续显示"仍在执行"。
    record = env.dispatcher.dispatch_record(control.run.run_id, "crash-erase")
    assert record is not None and record.state == "error"


def test_content_write_crash_leaves_no_orphan_blob_after_recovery(env, monkeypatch):
    """正文在提交前就落了 blob：崩溃后恢复要清掉没有行引用的孤儿副本。"""
    content = "崩溃前已经落盘的正文"
    scope = MemoryScope.user()
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content=content, call_id="crash-blob"
    )
    assert decision.is_admitted, decision
    ref = hashlib.sha256(content.encode("utf-8")).hexdigest()
    blob = env.memory.blob_path(ref)
    assert blob is not None

    _crash_at_commit(monkeypatch, env.dispatcher)
    with pytest.raises(SystemExit):
        env.dispatcher.dispatch(decision.request_id, claim)

    # 崩溃现场：字节在盘上，投影里一行都没有（提交回滚）。
    assert blob.is_file()
    assert env.memory.list_memories(allowed=(scope,)) == []

    report = _recover(env, run.run_id)
    assert "crash-blob" in report.memory_not_applied
    assert ref in report.memory_orphan_blobs
    assert not blob.exists(), "孤儿正文副本必须被清掉，否则它既不进召回也擦不掉"


def test_recovery_cleanup_never_deletes_a_blob_another_memory_shares(env, monkeypatch):
    """孤儿清理按引用计数下手：另一条记忆共享的同一个 blob 绝不能删。"""
    content = "两条记忆共享的正文"
    scope = MemoryScope.user()
    ref = hashlib.sha256(content.encode("utf-8")).hexdigest()
    task, run, claim = env.new_run()
    first_request, first_args, first = _propose(
        env, task, run, claim, content=content, call_id="share-1"
    )
    assert first.is_admitted
    assert env.dispatcher.dispatch(first.request_id, claim).ok
    blob = env.memory.blob_path(ref)
    assert blob is not None and blob.is_file()

    second_request, second_args, second = _propose(
        env,
        task,
        run,
        claim,
        content=content,
        call_id="share-2",
        scope=MemoryScope.task(task.task_id),
    )
    assert second.is_admitted
    _crash_at_commit(monkeypatch, env.dispatcher)
    with pytest.raises(SystemExit):
        env.dispatcher.dispatch(second.request_id, claim)

    report = _recover(env, run.run_id)
    assert "share-2" in report.memory_not_applied
    assert ref not in report.memory_orphan_blobs
    assert blob.is_file(), "另一条记忆还引用这个 blob，删了就是把别人的正文抹掉"
    assert len(env.memory.list_memories(allowed=(scope,))) == 1
    assert second_args["content"] == content
    assert first_request and second_request


def test_memory_write_is_settled_even_when_the_snapshot_manifest_is_corrupt(
    env, monkeypatch
):
    """清单损坏不该把记忆写推成 unknown 并挡住目录：它的结论不依赖那份证据。"""
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content="清单坏掉时也照样不生效", call_id="crash-corrupt"
    )
    _crash_at_commit(monkeypatch, env.dispatcher)
    with pytest.raises(SystemExit):
        env.dispatcher.dispatch(decision.request_id, claim)
    monkeypatch.undo()

    # 制造"清单存在但读不出来"：清单文件路径由 SnapshotStore 自己给出。
    manifest = env.snapshots.manifest_dir / f"{run.run_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{ not json", encoding="utf-8")

    assessment = assess_run(env.root / "authority", run.run_id, log=env.log)
    assert "crash-corrupt" in assessment.none_applied
    assert not assessment.blocks_takeover

    report = _recover(env, run.run_id)
    assert "crash-corrupt" in report.memory_not_applied
    assert report.blocked_workspaces == []


# ---------------------------------------------------------------- 11. 审计表脱敏的边角


def test_rejected_file_request_is_not_marked_as_redacted(env: Env):
    """`args_redacted` 是事实字段：被拒的文件请求没脱敏，就不能标成已脱敏。"""
    import json

    task, run, claim = env.new_run()
    args = {"path": "note.md", "content": "文件正文"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="modify_text",
        target=env.ws_a / "note.md",
        args=args,
        call_id="file-rejected",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_waiting, decision
    refused = env.dispatcher.dispatch(decision.request_id, claim)
    assert refused.status == "rejected"
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 0
    assert json.loads(row["args_payload"])["content"] == "文件正文"


def test_intent_events_drop_content_for_every_content_memory_write(env: Env):
    """`tool.intent` 不存记忆正文：propose / remember / edit 都要换成摘要。

    事件日志是 append-only、不可删的（#26 只收口审计表时漏了这里），所以判断
    口径必须是"记忆资源 + 参数里有 content"，而不是罗列工具名。
    """
    import json

    user = MemoryScope.user()
    control = env.dispatcher.open_control_run(
        goal="memory.remember", workspaces=[], memory_scopes=[]
    )
    content = "直接写进长期记忆的正文"
    args = {"kind": "preference", "content": content}
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.remember",
        target=user.to_uri(),
        args=args,
        call_id="intent-remember",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=args)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, control.claim)
    assert result.ok, result.output
    memory_id = result.output["memory_id"]
    env.dispatcher.close_control_run(
        control, outcome="succeeded", effect_state=result.effect_state
    )

    new_content = "改过之后的正文"
    edit_args = {"memory_id": memory_id, "revision": 1, "content": new_content}
    control2 = env.dispatcher.open_control_run(
        goal=f"memory.edit:{memory_id}", workspaces=[], memory_scopes=[]
    )
    edit_request = env.request(
        control2.task,
        control2.run,
        control2.claim,
        tool_id="memory.edit",
        target=user.to_uri(),
        args=edit_args,
        call_id="intent-edit",
    )
    edit_decision = env.dispatcher.prepare(edit_request, control2.claim, args=edit_args)
    assert edit_decision.is_waiting, edit_decision
    _approve(env, edit_decision.approval_id)
    edit_result = env.dispatcher.dispatch(edit_decision.request_id, control2.claim)
    try:
        assert edit_result.ok, edit_result.output
    finally:
        env.dispatcher.close_control_run(
            control2, outcome="succeeded", effect_state=edit_result.effect_state
        )

    intents = [
        event.payload
        for event in _events(env.log, control.run.run_id)
        if event.type == "tool.intent"
    ]
    remembered = next(item for item in intents if item["tool_id"] == "memory.remember")
    assert "content" not in remembered["args"]
    assert remembered["args"]["content_omitted"] is True
    assert remembered["args"]["content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert content not in json.dumps(remembered["args"], ensure_ascii=False)

    edit_intents = [
        event.payload
        for event in _events(env.log, control2.run.run_id)
        if event.type == "tool.intent"
    ]
    edited = next(item for item in edit_intents if item["tool_id"] == "memory.edit")
    assert "content" not in edited["args"]
    assert new_content not in json.dumps(edited["args"], ensure_ascii=False)
    assert edited["args"]["content_bytes"] == len(new_content.encode("utf-8"))

    # 投影照旧可重建：正文引用在 result 里，不依赖 intent 的副本。
    env.memory.rebuild()
    rebuilt = env.memory.get(edit_result.output["memory_id"], allowed=(user,))
    assert rebuilt is not None
    assert env.memory.read_content(rebuilt) == new_content


def test_terminal_rows_from_an_older_library_are_redacted_when_opened(env: Env):
    """升级前的终态行不会自己回到终态：打开库时要补做脱敏。

    模拟方式是把列真的删掉（老库没有这一列），而不是把标志改回 0——补扫只在
    "刚加上这一列"时跑一次（避免每次启动全表解析历史行 + 与重新发起竞态）。
    """
    import json

    content = "升级前就留在审计表里的正文"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content=content, call_id="legacy-row"
    )
    assert env.dispatcher.dispatch(decision.request_id, claim).ok
    # 老库没有 args_redacted 这一列、也没有迁移标记：删掉它，再写回完整参数。
    with env.log.transaction():
        env.log.conn.execute("ALTER TABLE dispatch_requests DROP COLUMN args_redacted")
        env.log.conn.execute(
            "DELETE FROM dispatch_meta WHERE key = 'args_redacted_backfilled'"
        )
        env.log.conn.execute(
            "UPDATE dispatch_requests SET args_payload = ? WHERE request_id = ?",
            (
                json.dumps({"kind": "preference", "content": content}),
                decision.request_id,
            ),
        )
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert json.loads(row["args_payload"])["content"] == content

    # 重新打开同一个库（模拟升级后第一次启动）：迁移要顺手把终态行脱敏。
    Dispatcher(
        env.log,
        env.tasks,
        env.leases,
        PolicyEngine(),
        env.approvals,
        grants=env.grants,
        claims=env.claims,
        snapshots=env.snapshots,
        data_root=env.root / "authority",
        memory=env.memory,
        clock=lambda: NOW,
    )
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 1
    assert "content" not in json.loads(row["args_payload"])


def test_interrupted_backfill_is_retried_on_the_next_open(env: Env, monkeypatch):
    """补扫中途死在事务里：标记不会置位，下次打开必须重试（否则永久漏脱敏）。"""
    import json

    content = "补扫中断也要最终脱敏"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content=content, call_id="interrupted-row"
    )
    assert env.dispatcher.dispatch(decision.request_id, claim).ok
    with env.log.transaction():
        env.log.conn.execute("ALTER TABLE dispatch_requests DROP COLUMN args_redacted")
        env.log.conn.execute(
            "DELETE FROM dispatch_meta WHERE key = 'args_redacted_backfilled'"
        )
        env.log.conn.execute(
            "UPDATE dispatch_requests SET args_payload = ? WHERE request_id = ?",
            (
                json.dumps({"kind": "preference", "content": content}),
                decision.request_id,
            ),
        )

    def boom(self):
        raise SystemExit("simulated power loss during migration")

    monkeypatch.setattr(Dispatcher, "_backfill_redacted_args", boom)
    try:
        with pytest.raises(SystemExit):
            Dispatcher(
                env.log,
                env.tasks,
                env.leases,
                PolicyEngine(),
                env.approvals,
                grants=env.grants,
                claims=env.claims,
                snapshots=env.snapshots,
                data_root=env.root / "authority",
                memory=env.memory,
                clock=lambda: NOW,
            )
    finally:
        monkeypatch.undo()
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 0, "中断时不该留下「已脱敏」的假象"

    # 重开：标记不存在 ⇒ 重试补扫 ⇒ 这一次真的脱敏。
    Dispatcher(
        env.log,
        env.tasks,
        env.leases,
        PolicyEngine(),
        env.approvals,
        grants=env.grants,
        claims=env.claims,
        snapshots=env.snapshots,
        data_root=env.root / "authority",
        memory=env.memory,
        clock=lambda: NOW,
    )
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 1
    assert "content" not in json.loads(row["args_payload"])


def test_reissued_request_after_terminal_state_keeps_full_args(env: Env):
    """终态后再判定同一个请求：参数要能完整写回，否则执行侧判"参数被改"。"""
    import json

    user = MemoryScope.user()
    task, run, claim, proposed = _proposed(env, content="原正文")
    memory_id = proposed["memory_id"]
    content = "重新发起时要保留的正文"
    args = {"memory_id": memory_id, "revision": 1, "content": content}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.edit",
        target=user.to_uri(),
        args=args,
        call_id="reissued-edit",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_waiting, decision
    # 还没批准就派发：确定被拒，键入终态 → 正文被脱敏。
    refused = env.dispatcher.dispatch(decision.request_id, claim)
    assert refused.status == "rejected"
    row = _request_row(env, decision.request_id)
    assert row is not None and int(row["args_redacted"]) == 1

    # 批准后重新判定同一张请求（真实路径：先被拒、后补批准）。
    _approve(env, decision.approval_id)
    again = env.dispatcher.prepare(request, claim, args=args)
    assert again.request_id == decision.request_id
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 0, "重新发起后要拿回完整参数才能执行"
    assert json.loads(row["args_payload"])["content"] == content

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.ok, result.output
    assert env.memory.read_content(
        env.memory.get(result.output["memory_id"], allowed=(user,))
    ) == content


def test_cli_recover_names_the_unresolved_erasure_and_cleaned_orphans(env, monkeypatch, capsys):
    """CLI `recover` 退出码 1 时必须给原因：新栏位要打印出来。"""
    from argparse import Namespace

    from tianshu.cli import _cmd_recover

    user = MemoryScope.user()
    content = "擦除崩溃窗口里的正文"
    _task, _run, _claim, output = _proposed(env, content=content)
    memory_id = output["memory_id"]
    control = env.dispatcher.open_control_run(
        goal=f"memory.erase:{memory_id}", workspaces=[], memory_scopes=[]
    )
    erase_args = {"memory_id": memory_id, "revision": 1}
    request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.erase",
        target=user.to_uri(),
        args=erase_args,
        call_id="cli-erase",
    )
    decision = env.dispatcher.prepare(request, control.claim, args=erase_args)
    _approve(env, decision.approval_id)
    _crash_at_commit(monkeypatch, env.dispatcher)
    try:
        with pytest.raises(SystemExit):
            env.dispatcher.dispatch(decision.request_id, control.claim)
    finally:
        env.dispatcher.close_control_run(
            control, outcome="failed", effect_state="none_applied"
        )
    monkeypatch.undo()
    capsys.readouterr()

    rc = _cmd_recover(
        Namespace(
            db=env.root / "events.db",
            data_root=env.root / "authority",
            run_id=control.run.run_id,
            workspace=env.ws_a,
            json=False,
        )
    )
    out = capsys.readouterr().out
    assert rc == 1, "有未结算的不可逆副作用时恢复不能报成功"
    assert "记忆待核实" in out and "cli-erase" in out


def test_recovery_uses_the_supplied_memory_projection_for_blob_checks(env):
    """恢复必须用装配方给的投影找 blob：自定义 `blob_dir` 下不能按默认目录判。"""
    from tianshu.memory import MemoryProjection
    from tianshu.recovery import _memory_effect

    output = _proposed(env, content="自定义目录里的正文")[3]
    ref = output["content_ref"]
    default_blob = env.memory.blob_path(ref)
    assert default_blob is not None and default_blob.is_file()

    custom_dir = env.root / "custom_blobs"
    custom_dir.mkdir()
    default_blob.rename(custom_dir / default_blob.name)
    custom = MemoryProjection(env.log.conn, log=env.log, blob_dir=custom_dir)

    payload = {"tool_id": "memory.erase", "args": {"memory_id": output["memory_id"]}}
    bucket, _extras, _problem = _memory_effect(env.log, payload, memory=custom)
    assert bucket == "none_applied", "文件在自定义目录里还在 ⇒ 擦除没生效"
    # 对照：按默认目录找会误判成"文件已没"（不可逆部分生效 → unknown）。
    bucket_default, _extras, _problem = _memory_effect(env.log, payload)
    assert bucket_default == "unknown"


# ---------------------------------------------------------------- 12. 复审第三轮：判据分支与接线


def _inject_pending_erase(env, run, memory_id: str, call_id: str = "inject-erase") -> None:
    """直接落一条"有 intent 无 result"的擦除意图（复现崩溃窗口的产物状态）。"""
    args = {"memory_id": memory_id, "revision": 1}
    env.log.append(
        run.run_id,
        "tool.intent",
        {
            "call_id": call_id,
            "tool_id": "memory.erase",
            "args": args,
            "args_hash": sha256_hex(canonical_bytes(args)),
            "request_id": f"req_{call_id}",
            "request_digest": sha256_hex(canonical_bytes({"call_id": call_id})),
            "dispatch_id": f"disp_{call_id}",
            "attempt_id": 1,
            "approval_id": None,
            "grant_id": "",
            "claim_epoch": 0,
        },
        task_id=run.task_id,
        actor_kind="core",
        idem_key=call_id,
    )


def test_recover_run_honors_blob_dir_for_a_pending_erasure(env):
    """`recover_run(blob_dir=...)` 真的接线：自定义正文目录下不能按默认目录判。"""
    user = MemoryScope.user()
    content = "自定义目录里的待擦正文"
    task, run, claim, output = _proposed(env, content=content)
    memory_id = output["memory_id"]
    ref = output["content_ref"]
    custom = env.root / "custom_blobs"
    custom.mkdir()
    (custom / ref).write_text(content, encoding="utf-8")
    default_blob = env.memory.blob_path(ref)
    assert default_blob is not None
    default_blob.unlink()  # 默认目录里"看不到"，自定义目录里有
    _inject_pending_erase(env, run, memory_id)

    report = recover_run(
        env.log,
        run.run_id,
        env.ws_a,
        env.snapshots,
        leases=env.leases,
        data_root=env.root / "authority",
        blob_dir=custom,
    )
    assert "inject-erase" in report.memory_not_applied
    assert "inject-erase" not in report.memory_unresolved
    # 对照：不给 blob_dir 时按默认目录判成"文件已没"（unknown）。
    # 换一个 workspace 起新 run：同一目录的租约还在第一个 run 手里。
    task2, run2, claim2, output2 = _proposed(
        env, content="另一条待擦正文", root=env.ws_b
    )
    blob2 = env.memory.blob_path(output2["content_ref"])
    assert blob2 is not None
    blob2.unlink()  # 默认目录里看不到，恢复又没拿到自定义目录 ⇒ 只能 unknown
    _inject_pending_erase(env, run2, output2["memory_id"], call_id="inject-erase-default")
    report2 = recover_run(
        env.log,
        run2.run_id,
        env.ws_a,
        env.snapshots,
        leases=env.leases,
        data_root=env.root / "authority",
    )
    assert "inject-erase-default" in report2.memory_unresolved
    assert user  # 作用域在夹具里被用上


def test_recover_run_honors_a_supplied_memory_projection(env):
    """`recover_run(memory=...)` 也走同一条判定（装配方给的投影赢）。"""
    content = "传给 recover 的正文"
    task, run, claim, output = _proposed(env, content=content)
    memory_id = output["memory_id"]
    ref = output["content_ref"]
    custom = env.root / "custom_blobs"
    custom.mkdir()
    (custom / ref).write_text(content, encoding="utf-8")
    default_blob = env.memory.blob_path(ref)
    assert default_blob is not None
    default_blob.unlink()
    _inject_pending_erase(env, run, memory_id, call_id="inject-erase-memory")
    custom_projection = MemoryProjection(env.log.conn, log=env.log, blob_dir=custom)

    report = recover_run(
        env.log,
        run.run_id,
        env.ws_a,
        env.snapshots,
        leases=env.leases,
        data_root=env.root / "authority",
        memory=custom_projection,
    )
    assert "inject-erase-memory" in report.memory_not_applied
    assert "inject-erase-memory" not in report.memory_unresolved


def test_erased_and_missing_rows_have_distinct_recovery_conclusions(env):
    """分支：行已被更早擦除 → none_applied；行不存在 → unknown（不冒充没删）。"""
    from tianshu.recovery import _memory_effect

    user = MemoryScope.user()
    task, run, claim, output = _proposed(env, content="先擦掉再判")
    memory_id = output["memory_id"]
    request, args = _confirm_request(env, task, run, claim, memory_id, revision=1)
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)
    assert env.dispatcher.dispatch(decision.request_id, claim).ok
    control = env.dispatcher.open_control_run(
        goal=f"memory.erase:{memory_id}", workspaces=[], memory_scopes=[]
    )
    erase_args = {"memory_id": memory_id, "revision": 2}
    erase_request = env.request(
        control.task, control.run, control.claim,
        tool_id="memory.erase", target=user.to_uri(), args=erase_args,
        call_id="done-erase",
    )
    erase_decision = env.dispatcher.prepare(erase_request, control.claim, args=erase_args)
    _approve(env, erase_decision.approval_id)
    erase_result = env.dispatcher.dispatch(erase_decision.request_id, control.claim)
    try:
        assert erase_result.ok, erase_result.output
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=erase_result.effect_state
        )

    bucket, _extras, problem = _memory_effect(
        env.log, {"tool_id": "memory.erase", "args": {"memory_id": memory_id}}
    )
    assert bucket == "none_applied", "已被更早擦除标记 ⇒ 本次没有新副作用"
    assert problem is None

    bucket, extras, problem = _memory_effect(
        env.log, {"tool_id": "memory.erase", "args": {"memory_id": "mem_never_existed"}}
    )
    assert bucket == "unknown", "证明不了没删，就不许宣布没生效"
    assert extras["recovered"] is True
    assert problem is not None


def test_erase_without_a_body_directory_has_no_file_side_effect():
    """分支：纯内存库没有正文目录 ⇒ 擦除不落文件副作用 ⇒ none_applied。"""
    from pathlib import Path as _Path

    from tianshu.memory import MemoryProjection as _Projection
    from tianshu.recovery import _memory_effect

    scope = MemoryScope.user()
    log = EventLog(_Path(":memory:"))
    projection = _Projection(log.conn, log=log, clock=lambda: NOW)
    projection.ensure_schema()
    ref = projection.put_content("内存库里的正文")
    payload = {
        "tool_id": "memory.propose",
        "status": "succeeded",
        "output": {
            "memory_id": "mem_mem",
            "revision": 1,
            "scope": scope.scope,
            "scope_id": scope.scope_id,
            "kind": "preference",
            "content_ref": ref,
            "source_refs": ["event:1"],
            "evidence": [{"source_memory_id": "s", "quote": "q"}],
            "proof_count": 1,
            "origin": "user_explicit",
            "status": "candidate",
            "created_at": 1,
            "expires_at": None,
            "deduped": False,
        },
    }
    try:
        with log.transaction() as tx:
            seq = tx.append(RUN_NAME, "tool.result", payload, legacy_write=True)
            projection.apply_event("tool.result", payload, seq=seq)
        bucket, _extras, _problem = _memory_effect(
            log,
            {"tool_id": "memory.erase", "args": {"memory_id": "mem_mem"}},
            memory=projection,
        )
        assert bucket == "none_applied"
    finally:
        log.close()


def test_projection_unavailable_still_settles_provable_memory_writes(env, monkeypatch):
    """分支：投影打不开时，非擦除写仍可判"没生效"，擦除只能 unknown。"""
    from tianshu import recovery as recovery_module
    from tianshu.recovery import _memory_effect

    monkeypatch.setattr(recovery_module, "_open_memory_projection", lambda *a, **k: None)
    bucket, _extras, problem = _memory_effect(
        env.log, {"tool_id": "memory.propose", "args": {"content_sha256": "0" * 64}}
    )
    assert bucket == "none_applied", "投影/事件层证明不依赖投影本身"
    assert problem is not None, "孤儿清理没能核查要留 problem"

    bucket, _extras, problem = _memory_effect(
        env.log, {"tool_id": "memory.erase", "args": {"memory_id": "mem_x"}}
    )
    assert bucket == "unknown"
    assert problem is not None


def test_cli_recover_names_cleaned_orphan_blobs(env, monkeypatch, capsys):
    """CLI 第二栏：清掉的孤儿正文也要有可读出口（否则运维只能猜）。"""
    from argparse import Namespace

    from tianshu.cli import _cmd_recover

    content = "崩溃留下的孤儿正文"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content=content, call_id="cli-orphan"
    )
    _crash_at_commit(monkeypatch, env.dispatcher)
    with pytest.raises(SystemExit):
        env.dispatcher.dispatch(decision.request_id, claim)
    monkeypatch.undo()
    capsys.readouterr()

    rc = _cmd_recover(
        Namespace(
            db=env.root / "events.db",
            data_root=env.root / "authority",
            run_id=run.run_id,
            workspace=env.ws_a,
            json=False,
        )
    )
    out = capsys.readouterr().out
    ref = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert rc == 0, "孤儿清理成功后恢复应当报成功"
    assert "已清孤儿正文" in out and ref in out


def test_marker_without_the_column_forces_a_rescan(env: Env):
    """边界：标记在、列却被删（迁移工具/人手误删）⇒ 必须作废标记并重扫。"""
    import json

    content = "标记在但列没了"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content=content, call_id="marker-only"
    )
    assert env.dispatcher.dispatch(decision.request_id, claim).ok
    with env.log.transaction():
        env.log.conn.execute("ALTER TABLE dispatch_requests DROP COLUMN args_redacted")
        env.log.conn.execute(
            "UPDATE dispatch_requests SET args_payload = ? WHERE request_id = ?",
            (
                json.dumps({"kind": "preference", "content": content}),
                decision.request_id,
            ),
        )
    marker = env.log.conn.execute(
        "SELECT value FROM dispatch_meta WHERE key = 'args_redacted_backfilled'"
    ).fetchone()
    assert marker is not None, "夹具前提：标记原本就在"

    Dispatcher(
        env.log,
        env.tasks,
        env.leases,
        PolicyEngine(),
        env.approvals,
        grants=env.grants,
        claims=env.claims,
        snapshots=env.snapshots,
        data_root=env.root / "authority",
        memory=env.memory,
        clock=lambda: NOW,
    )
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 1
    assert "content" not in json.loads(row["args_payload"])


def test_memory_argument_wins_over_blob_dir(env):
    """`memory` 与 `blob_dir` 同时给：`memory` 胜出，不做静默合并。"""
    from tianshu.recovery import _memory_effect

    content = "优先级用例"
    output = _proposed(env, content=content)[3]
    ref = output["content_ref"]
    custom = env.root / "custom_blobs"
    custom.mkdir()
    (custom / ref).write_text(content, encoding="utf-8")
    default_blob = env.memory.blob_path(ref)
    assert default_blob is not None
    default_blob.unlink()  # 默认目录看不到 → 只有"赢的那个"能决定结论

    payload = {"tool_id": "memory.erase", "args": {"memory_id": output["memory_id"]}}
    # 传 memory（默认目录）+ 一个"看起来对"的自定义 blob_dir：memory 优先 ⇒ unknown
    bucket, _extras, _problem = _memory_effect(
        env.log, payload, memory=env.memory, blob_dir=custom
    )
    assert bucket == "unknown", "memory 优先：它看不到文件就是看不到"
    # 只传 blob_dir（不传 memory）⇒ 按该目录 ⇒ none_applied
    bucket, _extras, _problem = _memory_effect(env.log, payload, blob_dir=custom)
    assert bucket == "none_applied"


def test_crash_between_add_column_and_backfill_is_retried(env: Env, monkeypatch):
    """注入：列刚补上、补扫还没跑就崩溃 → 下次打开必须重扫（不许永久漏）。"""
    import json

    content = "加列之后补扫之前崩溃"
    task, run, claim = env.new_run()
    _request, _args, decision = _propose(
        env, task, run, claim, content=content, call_id="between-add-and-scan"
    )
    assert env.dispatcher.dispatch(decision.request_id, claim).ok
    with env.log.transaction():
        env.log.conn.execute("ALTER TABLE dispatch_requests DROP COLUMN args_redacted")
        env.log.conn.execute(
            "UPDATE dispatch_requests SET args_payload = ? WHERE request_id = ?",
            (
                json.dumps({"kind": "preference", "content": content}),
                decision.request_id,
            ),
        )
    # 标记仍在（列是被"人工/迁移工具"删掉的）。

    def boom(self):
        raise SystemExit("simulated power loss right after ADD COLUMN")

    monkeypatch.setattr(Dispatcher, "_migrate_redacted_args", boom)
    try:
        with pytest.raises(SystemExit):
            Dispatcher(
                env.log,
                env.tasks,
                env.leases,
                PolicyEngine(),
                env.approvals,
                grants=env.grants,
                claims=env.claims,
                snapshots=env.snapshots,
                data_root=env.root / "authority",
                memory=env.memory,
                clock=lambda: NOW,
            )
    finally:
        monkeypatch.undo()
    # 列已补上（ALTER 是 autocommit），但补扫没跑：标记必须已被作废。
    marker = env.log.conn.execute(
        "SELECT value FROM dispatch_meta WHERE key = 'args_redacted_backfilled'"
    ).fetchone()
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 0
    assert "content" in json.loads(row["args_payload"])
    assert marker is None, "标记必须在加列之前就被作废（否则重开不重扫）"

    Dispatcher(
        env.log,
        env.tasks,
        env.leases,
        PolicyEngine(),
        env.approvals,
        grants=env.grants,
        claims=env.claims,
        snapshots=env.snapshots,
        data_root=env.root / "authority",
        memory=env.memory,
        clock=lambda: NOW,
    )
    row = _request_row(env, decision.request_id)
    assert row is not None
    assert int(row["args_redacted"]) == 1
    assert "content" not in json.loads(row["args_payload"])


# ---------------------------------------------------------------- 13. N3 L2/L3 合成（端到端）


def _consolidate_request(env: Env, task, run, claim, *, scope, call_id="cns-1"):
    args = {"scope": scope.scope, "scope_id": scope.scope_id}
    request = env.request(
        task,
        run,
        claim,
        tool_id="memory.consolidate",
        target=scope.to_uri(),
        args=args,
        call_id=call_id,
    )
    return request, args


def _confirm_active(env: Env, task, run, claim, memory_id: str, revision: int, *, call_id: str):
    request, args = _confirm_request(
        env, task, run, claim, memory_id, revision=revision, call_id=call_id
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    _approve(env, decision.approval_id)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    return result


def test_consolidate_is_a_real_model_tool_and_writes_candidate_pages(env: Env):
    """模型可见的 `memory.consolidate` 走真实派发：产出 L2 候选页，未确认不召回。"""
    user = MemoryScope.user()
    task, run, claim = env.new_run()
    ids = []
    for call_id, content in (
        ("cns-a", "报告用中文写，先列结论"),
        ("cns-b", "报告先列结论再讲依据"),
    ):
        decision = _propose(env, task, run, claim, content=content, call_id=call_id)[2]
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.ok, result.output
        ids.append(result.output["memory_id"])
        _confirm_active(env, task, run, claim, ids[-1], 1, call_id=f"{call_id}-c")

    assert "memory.consolidate" in {
        spec["name"] for spec in tool_manifest()
    }, "合成必须是模型可见工具"
    request, args = _consolidate_request(env, task, run, claim, scope=user)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    pages = result.output["pages"]
    assert [page["layer"] for page in pages] == ["l2", "l3"]
    for page in pages:
        assert page["status"] == "candidate"
        assert page["origin"] == "inferred"
        assert page["proof_count"] == len(page["evidence"])
        assert any(ref.startswith("seq:") for ref in page["source_refs"])

    rows = env.memory.list_memories(allowed=(user,))
    l2 = [row for row in rows if row.layer == "l2"]
    assert len(l2) == 1 and l2[0].status == "candidate"
    recalled = env.memory.search("报告", allowed=(user,))
    assert recalled, "L1 仍应召回"
    assert all(item.memory.memory_id != l2[0].memory_id for item in recalled), (
        "候选页未确认前不得召回"
    )


def test_consolidate_refuses_workspace_scope_at_dispatch(env: Env):
    """workspace 是共享边界：模型可见的合成也必须拒（与 propose 同口径）。"""
    task, run, claim = env.new_run()
    ws_scope = MemoryScope.workspace(env.ws_a)
    request, args = _consolidate_request(env, task, run, claim, scope=ws_scope)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_scope_denied"


def test_consolidate_task_scope_only_for_own_task(env: Env):
    """模型只能合成**本任务**的 task 作用域；别人的 task 作用域要拒。"""
    task, run, claim = env.new_run()
    other = MemoryScope.task("task_someone_else")
    request, args = _consolidate_request(env, task, run, claim, scope=other)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_rejected, decision
    assert decision.rule_id == "c4.memory_scope_denied"


def test_confirmed_page_joins_recall_and_stale_after_new_l1(env: Env):
    """人 confirm 后 L2 页进入召回；之后新增 L1 ⇒ 旧页标 stale 且降权。"""
    user = MemoryScope.user()
    task, run, claim = env.new_run()
    l1_ids: list[str] = []
    for call_id, content in (
        ("p1", "报告用中文写，先列结论"),
        ("p2", "报告先列结论再讲依据"),
    ):
        decision = _propose(env, task, run, claim, content=content, call_id=call_id)[2]
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.ok, result.output
        l1_ids.append(result.output["memory_id"])
        _confirm_active(env, task, run, claim, l1_ids[-1], 1, call_id=f"{call_id}-c")

    request, args = _consolidate_request(env, task, run, claim, scope=user)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_admitted, decision
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.ok, result.output
    page_id = result.output["pages"][0]["memory_id"]
    _confirm_active(env, task, run, claim, page_id, 1, call_id="confirm-page")

    recalled = env.memory.search("报告", allowed=(user,))
    stale_before = {item.memory.memory_id: item.stale for item in recalled}
    assert page_id in stale_before and stale_before[page_id] is False

    fresh = _propose(env, task, run, claim, content="报告里术语保留英文", call_id="p3")[2]
    fresh_result = env.dispatcher.dispatch(fresh.request_id, claim)
    assert fresh_result.ok, fresh_result.output
    fresh_id = fresh_result.output["memory_id"]
    _confirm_active(env, task, run, claim, fresh_id, 1, call_id="p3-c")
    # 默认生产路径是 `retrieve()`（`memory.search` 工具走它），不是 FTS-only 的
    # `search()`；复审指出只测 `search()` 会把"无索引分支没有 stale 排序"的洞漏掉。
    # 注意：这条 e2e 的 L1 是"人批准 confirm"过的，`origin` 会升成 `user_confirmed`，
    # 页仍是 `inferred`——排序本来就会偏向 L1，所以这里**只断言 stale 标记**，
    # 不把"排后"当归证（那是 `origin` 权重干的）。真正的排序判据在
    # `test_retrieve_demotes_stale_pages_even_without_a_vector_index`
    # （页与 L1 同 origin、同 created_at）。
    after = env.memory.retrieve("报告", allowed=(user,))
    stale_after = {item.memory.memory_id: item.stale for item in after.items}
    assert stale_after[page_id] is True, "新增 L1 后旧页应变陈旧"
    assert stale_after[fresh_id] is None, "L1 没有合成层新鲜度，不该被标"
    # 词面路与默认路必须给出同一组 stale 标记：排序/标注唯一实现。
    plain = {
        item.memory.memory_id: item.stale
        for item in env.memory.search("报告", allowed=(user,))
    }
    assert plain.get(page_id) is True


def test_repeated_consolidate_reports_existing_status_not_candidate(env: Env):
    """重复合成已 confirm 的页：output 必须如实说 active，不能一律写 candidate。

    事件是审计与回放的唯一依据。若 output 写 "candidate"、投影里其实是 active，人再
    confirm 就会撞状态，事后读事件也看不出这一页早就被确认过（复审指出的边界）。
    """
    user = MemoryScope.user()
    task, run, claim = env.new_run()
    for call_id, content in (("r1", "数据库迁移分三步"), ("r2", "数据库迁移前先备份")):
        decision = _propose(env, task, run, claim, content=content, call_id=call_id)[2]
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.ok, result.output
        l1_id = result.output["memory_id"]
        _confirm_active(env, task, run, claim, l1_id, 1, call_id=f"{call_id}-c")

    request, args = _consolidate_request(env, task, run, claim, scope=user)
    decision = env.dispatcher.prepare(request, claim, args=args)
    first = env.dispatcher.dispatch(decision.request_id, claim)
    assert first.ok and first.output["created"] == len(first.output["pages"])
    page_id = first.output["pages"][0]["memory_id"]
    _confirm_active(env, task, run, claim, page_id, 1, call_id="confirm-page")

    request2, args2 = _consolidate_request(
        env, task, run, claim, scope=user, call_id="cns-2"
    )
    decision2 = env.dispatcher.prepare(request2, claim, args=args2)
    second = env.dispatcher.dispatch(decision2.request_id, claim)
    assert second.ok, second.output
    assert second.output["created"] == 0, second.output
    assert second.output["existing"] == len(second.output["pages"])
    repeated = next(
        page for page in second.output["pages"] if page["memory_id"] == page_id
    )
    assert repeated["status"] == "active", "output 不能把已确认的页说成候选"
    assert repeated["existing"] is True
    # 投影没有被第二次合成改动：仍 active、没有多出行。
    still = env.memory.get(page_id, allowed=(user,))
    assert still is not None and still.status == "active"


def test_failed_consolidate_writes_no_orphan_blob(env: Env):
    """正文撞回**已擦除**的页 id：在写正文**之前**就拒，不留孤儿 blob。

    恢复流程的孤儿清理只认 `tool.intent.args.content_sha256`，而 consolidate 的
    args 只有 scope/scope_id——所以失败路径必须先拒后写。用**擦除**过的页来测：
    擦除会把正文副本删掉，若先 `put_content` 再失败，就会重新落一个没有任何投影
    行引用的文件（superseded 页的 blob 还在，测不出这个洞）。
    """
    user = MemoryScope.user()
    task, run, claim = env.new_run()
    first_ids: list[str] = []
    for call_id, content in (("o1", "数据库迁移分三步"), ("o2", "数据库迁移前先备份")):
        decision = _propose(env, task, run, claim, content=content, call_id=call_id)[2]
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.ok, result.output
        first_ids.append(result.output["memory_id"])
        _confirm_active(
            env, task, run, claim, first_ids[-1], 1, call_id=f"{call_id}-c"
        )
    request, args = _consolidate_request(env, task, run, claim, scope=user, call_id="c1")
    decision = env.dispatcher.prepare(request, claim, args=args)
    first = env.dispatcher.dispatch(decision.request_id, claim)
    assert first.ok, first.output
    page_id = first.output["pages"][0]["memory_id"]
    _confirm_active(env, task, run, claim, page_id, 1, call_id="confirm-page")

    # 擦掉这一页（控制面专属 + 一次性审批）：它的正文副本会被删掉。
    control = env.dispatcher.open_control_run(
        goal=f"memory.erase:{page_id}", workspaces=[], memory_scopes=[]
    )
    erase_args = {"memory_id": page_id, "revision": 2}
    erase_request = env.request(
        control.task,
        control.run,
        control.claim,
        tool_id="memory.erase",
        target=user.to_uri(),
        args=erase_args,
        call_id="erase-page",
    )
    erase_decision = env.dispatcher.prepare(erase_request, control.claim, args=erase_args)
    _approve(env, erase_decision.approval_id)
    erased = env.dispatcher.dispatch(erase_decision.request_id, control.claim)
    try:
        assert erased.ok, erased.output
        assert erased.output["erasure"]["deleted"] is True
    finally:
        env.dispatcher.close_control_run(
            control, outcome="succeeded", effect_state=erased.effect_state
        )

    # 换一条源 → 新页取代（不同 id）；再把这条源 forget 掉，正文回退到第一次。
    third = _propose(
        env, task, run, claim, content="数据库迁移要有回滚方案", call_id="o3"
    )[2]
    result = env.dispatcher.dispatch(third.request_id, claim)
    assert result.ok, result.output
    third_l1 = result.output["memory_id"]
    _confirm_active(env, task, run, claim, third_l1, 1, call_id="o3-c")
    request2, args2 = _consolidate_request(
        env, task, run, claim, scope=user, call_id="c2"
    )
    decision2 = env.dispatcher.prepare(request2, claim, args=args2)
    assert env.dispatcher.dispatch(decision2.request_id, claim).ok

    f_request, f_args = _forget_request(
        env, task, run, claim, third_l1, call_id="forget-o3"
    )
    f_decision = env.dispatcher.prepare(f_request, claim, args=f_args)
    _approve(env, f_decision.approval_id)
    assert env.dispatcher.dispatch(f_decision.request_id, claim).ok

    blob_dir = env.memory.blob_dir
    assert blob_dir is not None
    before = sorted(path.name for path in blob_dir.iterdir())
    request3, args3 = _consolidate_request(
        env, task, run, claim, scope=user, call_id="c3"
    )
    decision3 = env.dispatcher.prepare(request3, claim, args=args3)
    failed = env.dispatcher.dispatch(decision3.request_id, claim)
    assert not failed.ok, "已擦除页 id 被撞回时必须失败，而不是静默复活"
    assert "已存在" in str(failed.output.get("error"))
    after = sorted(path.name for path in blob_dir.iterdir())
    assert after == before, "失败路径不该往 blob 目录里写新正文（孤儿没人清）"
