"""派发层（M2 · PR B · Task 4）。

这一组测试盯的是四件必须同时成立的事，每一件都有一个"看起来差不多、其实
已经破了"的反例：

1. 审批消费与 `tool.intent` 是**同一个提交**——分开就会出现"批了没执行"或
   "执行了两次"。
2. 同一张审批**只能兑现一次**，并发也一样。
3. 撤销与执行抢同一把守卫，谁先谁赢，而"赢"的含义必须如实记录。
4. 执行器**不信任**调用方给的票据，只相信库里的记录。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from tianshu.dispatcher import (
    KIND_ADMITTED,
    KIND_REJECTED,
    KIND_WAITING,
    Dispatcher,
    DispatchError,
    build_request,
)
from tianshu.errors import ToolNotAllowed, WorkspaceBlocked
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.guard import ensure_not_blocked
from tianshu.jobs import JobClaim, JobStore
from tianshu.leases import LeaseStore, ProcessIdentity
from tianshu.permissions import (
    ActionRequest,
    ApprovalError,
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.tools import (
    SPECS,
    AdmittedCall,
    FileExecutor,
    ToolContext,
    execute,
    observe_state,
)
from tianshu.workspace import Workspace, WorkspaceBinding

ORIGINAL = "原始内容\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _types(log: EventLog, run_id: str) -> list[str]:
    return [e.type for e in log.read_run(run_id)]


# ---------------------------------------------------------------- 夹具


@dataclass
class Env:
    root: Path
    ws: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    def new_run(self, *, goal: str = "整理目录里的笔记", root: Path | None = None):
        root = Path(root) if root is not None else self.ws
        task = self.tasks.create_task(goal, [{"root_path": str(root)}])
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([root], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim


def _env(tmp_path: Path) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text(ORIGINAL, encoding="utf-8")
    data = tmp_path / "authority"
    data.mkdir()

    log = EventLog(tmp_path / "events.db")
    leases = LeaseStore(log)
    claims = JobStore(log)
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=data)
    approvals = ApprovalStore(log)
    grants = GrantStore(log)
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
    )
    return Env(
        root=tmp_path,
        ws=ws,
        log=log,
        leases=leases,
        claims=claims,
        tasks=tasks,
        approvals=approvals,
        grants=grants,
        snapshots=snapshots,
        dispatcher=dispatcher,
    )


@pytest.fixture
def env(tmp_path):
    e = _env(tmp_path)
    try:
        yield e
    finally:
        e.log.close()


def _overwrite_request(env: Env, task, run, claim, *, text="改后的内容\n", call_id="c1"):
    target = env.ws / "note.md"
    args = {"path": "note.md", "content": text}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id="modify_text",
        target=target,
        args=args,
    )
    return request, args


def _approve(env: Env, approval_id: str) -> None:
    env.approvals.decide(
        approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )


def _admit_overwrite(env: Env, **kwargs):
    """走到"已批准、可派发"这一步。"""
    task, run, claim = env.new_run()
    request, args = _overwrite_request(env, task, run, claim, **kwargs)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_WAITING, decision
    _approve(env, decision.approval_id)
    return task, run, claim, request, args, decision


# ---------------------------------------------------------------- 1. 原子性


def test_consume_and_intent_are_atomic(env, monkeypatch):
    """审批消费与 intent 必须同生同死。

    反例（上一版会这样）：先消费审批并提交，再写 intent。中间崩了就是
    "批准被用掉但什么都没执行"，用户只能再批一次，而账上写着用过了。
    这里让 grant 消费在**审批消费之后**失败，断言两者一起回滚。
    """
    task, run, claim = env.new_run()
    request, args = _overwrite_request(env, task, run, claim)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_WAITING
    _approve(env, decision.approval_id)

    class BrokenGrants:
        """模拟"签发票据时发现租约不在了"。"""

        def __init__(self, real):
            self.real = real

        def issue(self, **kwargs):
            raise ApprovalError("资源租约已失效，无法签发执行许可")

        def __getattr__(self, name):
            return getattr(self.real, name)

    monkeypatch.setattr(env.dispatcher, "_grants", BrokenGrants(env.grants))

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "rejected"
    assert result.effect_state == "none_applied"

    # 审批没有被消费掉（事务回滚）
    assert env.approvals.get(decision.approval_id).state == "approved"
    # 没有留下半份意图，也没有派发记录
    assert "tool.intent" not in _types(env.log, run.run_id)
    assert env.dispatcher.dispatch_record(run.run_id, request.call_id) is None
    # 文件一个字节没动
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_successful_dispatch_consumes_once_and_writes(env):
    task, run, claim, request, args, decision = _admit_overwrite(env)

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.ok, result.output
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "改后的内容\n"

    approval = env.approvals.get(decision.approval_id)
    assert approval.state == "consumed"
    assert approval.consumed_at is not None

    types = _types(env.log, run.run_id)
    assert "tool.intent" in types
    assert "execution.started" in types
    assert "tool.result" in types
    assert "receipt" in types

    row = env.dispatcher.dispatch_record(run.run_id, request.call_id)
    assert row.state == "done"
    assert row.grant_id

    grant = env.grants.get(row.grant_id)
    assert grant.remaining_uses == 0
    assert grant.issuer.startswith("approval:")


# ---------------------------------------------------------------- 2. 并发


def test_concurrent_approval_consumes_once(env, tmp_path):
    """两个**各自的连接**同时派发同一张批准：必须只有一个能执行。

    不是"在线程里调两次"就算并发：每个派发方在自己的线程里建自己的
    EventLog 连接（sqlite 连接不能跨线程用），真正考验 SQLite 的写序列化
    与条件 UPDATE，而不是 Python 的 GIL。
    """
    task, run, claim, request, args, decision = _admit_overwrite(env)

    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def fire():
        barrier.wait(timeout=5)
        log = EventLog(tmp_path / "events.db")
        try:
            leases = LeaseStore(log)
            claims = JobStore(log)
            tasks = TaskStore(
                log,
                leases=leases,
                claims=claims,
                data_root=tmp_path / "authority",
            )
            dispatcher = Dispatcher(
                log,
                tasks,
                leases,
                PolicyEngine(),
                ApprovalStore(log),
                grants=GrantStore(log),
                claims=claims,
                snapshots=SnapshotStore(tmp_path / "artifacts"),
                data_root=tmp_path / "authority",
            )
            results.append(dispatcher.dispatch(request.request_id, claim))
        except DispatchError as exc:  # 输家的合法结局之一
            results.append(exc)
        except BaseException as exc:  # pragma: no cover - 排障用
            errors.append(exc)
        finally:
            log.close()

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    applied = [
        r for r in results
        if getattr(r, "effect_state", "none_applied") == "applied"
    ]
    assert len(applied) == 1, f"只能执行一次，实际：{results}"
    assert len(results) == 2

    rows = env.log.conn.execute(
        "SELECT * FROM dispatches WHERE run_id=? AND call_id=?",
        (run.run_id, request.call_id),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "done"
    assert env.approvals.get(decision.approval_id).state == "consumed"

    intents = [e for e in env.log.read_run(run.run_id) if e.type == "tool.intent"]
    assert len(intents) == 1, "同一 call_id 不能出现两份 intent"


# ---------------------------------------------------------------- 3. 撤销


def test_revoke_between_intent_and_open(env, monkeypatch):
    """intent 已提交、文件还没打开时被撤销 → 拒绝执行，字节不变。

    这正是守卫存在的理由。撤销先拿到守卫，旧操作就必须作废；如果反过来
    是执行先拿到守卫，那次调用已经是 in-flight（见下一个用例）。
    """
    task, run, claim, request, args, decision = _admit_overwrite(env)

    original_begin = env.tasks.begin_execution

    def begin_after_revoke(data_root, run_id, resources):
        env.dispatcher.revoke(
            decision.approval_id,
            actor=HumanIdentity.local_cli(),
            reason="测试：在 intent 之后、打开文件之前撤销",
        )
        return original_begin(data_root, run_id, resources)

    monkeypatch.setattr(env.tasks, "begin_execution", begin_after_revoke)

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "rejected"
    assert result.effect_state == "none_applied"
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL

    assert env.approvals.get(decision.approval_id).state == "revoked"
    row = env.dispatcher.dispatch_record(run.run_id, request.call_id)
    assert row.state == "revoked"
    assert "dispatch.rejected" in _types(env.log, run.run_id)


def test_revoke_after_execution_started_is_refused(env):
    """已经越过 execution.started 的调用，撤销必须明确失败，不发假消息。"""
    task, run, claim, request, args, decision = _admit_overwrite(env)
    assert env.dispatcher.dispatch(request.request_id, claim).ok

    with pytest.raises(ApprovalError) as ei:
        env.dispatcher.revoke(decision.approval_id, actor=HumanIdentity.local_cli())
    assert "已经开始执行" in str(ei.value) or "不能撤销" in str(ei.value)
    # 文件仍然是执行后的内容，不会被"撤销"悄悄写回
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "改后的内容\n"


def test_revoke_requires_human_identity(env):
    task, run, claim, request, args, decision = _admit_overwrite(env)
    with pytest.raises(ApprovalError):
        env.dispatcher.revoke(decision.approval_id, actor="cli:local")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 4. 执行器不信任票据


def test_direct_execute_requires_admission(env):
    """伪造票据去调执行器 → 拒绝。

    票据是普通 dataclass，谁都能构造。它挡不住伪造，挡得住伪造的是
    "执行器自己回库核对"这一步。
    """
    task, run, claim = env.new_run()
    forged_request = ActionRequest(
        request_id="req_forged",
        task_id=task.task_id,
        run_id=run.run_id,
        job_id=claim.job_id,
        claim_epoch=claim.claim_epoch,
        owner_instance_id=claim.owner_instance_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        call_id="forged_call",
        attempt_id=1,
        tool_id="modify_text",
        tool_version="1.0",
        schema_hash=sha256_hex(canonical_bytes(SPECS["modify_text"].input_schema)),
        action="overwrite",
        resources=(str(env.ws / "note.md"),),
        args_hash=sha256_hex(canonical_bytes({"path": "note.md", "content": "坏"})),
        preconditions={"before_state": _sha(ORIGINAL)},
    )
    forged = AdmittedCall(
        dispatch_id="disp_forged",
        request=forged_request,
        request_digest=forged_request.digest(),
        claim=claim,
        tool_id="modify_text",
        action="overwrite",
        args={"path": "note.md", "content": "坏"},
        target=str(env.ws / "note.md"),
        rel_path="note.md",
        workspace_root=str(env.ws),
        resource_key=__import__("tianshu.workspace", fromlist=["ResourceIdentity"])
        .ResourceIdentity.of(env.ws / "note.md")
        .key,
        resources=(str(env.ws / "note.md"),),
        intent_seq=0,
        approval_id=None,
    )

    executor: FileExecutor = env.dispatcher._executor  # noqa: SLF001
    with pytest.raises(ToolNotAllowed) as ei:
        executor.execute(forged, None)
    assert "AdmittedCall" in str(ei.value) or "execution_guard" in str(ei.value)

    # 没有许可（不在守卫内）也一样
    with pytest.raises(ToolNotAllowed):
        executor.execute(forged, object())  # type: ignore[arg-type]

    # 没有派发记录 + 带着合法形状的许可，也要被库里的记录挡住
    from tianshu.tasks import ExecutionPermit

    permit = ExecutionPermit(
        run_id=run.run_id,
        task_id=task.task_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        resources=(str(env.ws),),
        seq=1,
        started_at=0,
    )
    with pytest.raises(ToolNotAllowed) as ei:
        executor.execute(forged, permit)
    assert "派发记录" in str(ei.value)

    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_legacy_tool_entry_cannot_write(env):
    """不走派发、直接调工具写 → 拒绝。唯一例外是 loop.run 的显式遗留开关。"""
    ws = Workspace(env.ws)
    ctx = ToolContext(log=env.log, run_id="r_legacy", call_id="c_legacy")
    with pytest.raises(ToolNotAllowed) as ei:
        execute(ws, "modify_text", {"path": "note.md", "content": "偷偷写"}, ctx)
    assert "Dispatcher" in str(ei.value)
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL

    # 纯读工具不受影响
    assert execute(ws, "read_text", {"path": "note.md"}, ctx).ok


# ---------------------------------------------------------------- 5. 阻断与租约


def test_undo_and_recovery_obey_resource_block(env):
    """undo 与 recover 都必须服从"资源被占用/阻断"这条约束。"""
    task, run, claim = env.new_run()
    env.snapshots.record_preimage(run.run_id, "c1", "note.md", b"old", _sha("x"))

    # (a) 撤销的直写分支默认不可用
    with pytest.raises(ToolNotAllowed):
        env.snapshots.undo(
            run.run_id,
            Workspace(env.ws),
            guard=lambda: ensure_not_blocked(env.log, env.ws),
        )

    # (b) 阻断的 workspace 撤销被拒，且字节不变
    env.log.append(
        run.run_id,
        "recovery.blocked",
        {"workspace": str(env.ws.resolve()), "call_ids": ["c9"], "reason": "test"},
        actor_kind="system",
    )
    with pytest.raises(WorkspaceBlocked):
        env.snapshots.undo(
            run.run_id,
            Workspace(env.ws),
            guard=lambda: ensure_not_blocked(env.log, env.ws),
            allow_direct_write=True,
        )
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL

    # (c) 恢复：这条路径正被另一个活着的 Run 占着 → 不下结论，记 unresolved
    env.leases.release_run(run.run_id)
    other = env.new_run(goal="另一个运行")
    _t, other_run, _c = other
    env.leases.acquire([env.ws / "note.md"], other_run.run_id)

    env.log.append(
        run.run_id,
        "tool.intent",
        {
            "call_id": "c_pending",
            "tool_id": "modify_text",
            "args": {"path": "note.md", "content": "x"},
            "args_hash": _sha("args"),
        },
        actor_kind="core",
    )
    report = recover_run(
        env.log, run.run_id, env.ws, env.snapshots, leases=env.leases
    )
    assert "c_pending" in report.unresolved
    assert not report.ok


def test_dispatch_rejected_when_lease_not_covering(env):
    """租约持有的是 /ws，目标在 /outside：不覆盖就拒绝（目录外不授权）。"""
    task, run, claim = env.new_run()
    outside = env.root / "outside.md"
    outside.write_text("外面\n", encoding="utf-8")
    args = {"path": str(outside), "content": "改外面"}
    request = build_request(
        task=task, run=run, claim=claim, call_id="c_out",
        tool_id="modify_text", target=outside, args=args,
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_REJECTED
    assert decision.rule_id in ("c4.outside_workspace_unsupported", "dispatch.lease_lost")
    assert outside.read_text(encoding="utf-8") == "外面\n"


# ---------------------------------------------------------------- 6. 其余不变量


def test_policy_deny_leaves_no_intent_and_no_bytes(env):
    """只读绑定上的写请求：策略拒绝，不登记 intent、不动文件。"""
    readonly = env.tasks.create_task("只读任务", [{"root_path": str(env.ws), "mode": "readonly"}])
    run = env.tasks.start_run(readonly.task_id)
    env.leases.acquire([env.ws], run.run_id)
    job = env.claims.enqueue(run.run_id)
    claim = env.claims.claim(job.job_id)
    env.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)

    request, args = _overwrite_request(env, readonly, run, claim)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_REJECTED
    assert decision.rule_id == "c4.readonly_binding"
    assert "tool.intent" not in _types(env.log, run.run_id)
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_changed_file_after_approval_is_rejected(env):
    """批准之后用户改了目标 → 写前状态不符，旧审批不能执行。"""
    task, run, claim, request, args, decision = _admit_overwrite(env)

    (env.ws / "note.md").write_text("用户自己改的\n", encoding="utf-8")

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "rejected"
    assert "写前状态已改变" in result.output["error"]
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "用户自己改的\n"

    # 审批没有被执行掉：状态仍是 approved，但这次派发已经拒绝并留痕
    assert env.approvals.get(decision.approval_id).state == "approved"
    assert "dispatch.rejected" in _types(env.log, run.run_id)


def test_old_claim_after_takeover_cannot_dispatch(env):
    """旧 owner 还活着，但 claim 已被接管 → 旧 claim 不能发起写入。"""
    task, run, claim, request, args, decision = _admit_overwrite(env)

    intruder = JobStore(env.log, identity=ProcessIdentity.mint())
    new_claim = intruder.claim(claim.job_id)
    assert new_claim.claim_epoch == claim.claim_epoch + 1

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "rejected"
    assert result.effect_state == "none_applied"
    assert "claim" in result.output["error"].lower()
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_auto_create_needs_no_approval_but_gets_audited_grant(env):
    """目录内新建：自动放行，但仍要签一张有来源的票据（不是"没记录"）。"""
    task, run, claim = env.new_run()
    target = env.ws / "report.md"
    args = {"name": "report.md", "content": "产出\n"}
    request = build_request(
        task=task, run=run, claim=claim, call_id="c_new",
        tool_id="create_report", target=target, args=args,
    )

    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_ADMITTED
    assert decision.approval_id is None

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.ok, result.output
    assert target.read_text(encoding="utf-8") == "产出\n"

    row = env.dispatcher.dispatch_record(run.run_id, "c_new")
    grant = env.grants.get(row.grant_id)
    assert grant.issuer == "policy:auto"
    assert grant.remaining_uses == 0

    # 没有伪造一张"人工 approved"
    assert env.log.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_delete_file_is_approved_and_undoable(env):
    """删除要审批；删完还能按写前内容还原。"""
    task, run, claim = env.new_run()
    target = env.ws / "note.md"
    args = {"path": "note.md"}
    request = build_request(
        task=task, run=run, claim=claim, call_id="c_del",
        tool_id="delete_file", target=target, args=args,
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_WAITING
    _approve(env, decision.approval_id)

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.ok, result.output
    assert not target.exists()
    assert result.output["after_state"] == "absent"

    # 凭据里 after_state 是显式的 absent，不是空值
    receipts = [e for e in env.log.read_run(run.run_id) if e.type == "receipt"]
    assert receipts and receipts[-1].payload["after_state"] == "absent"

    # undo：删除的逆操作是"还原写前内容"
    seen: list[tuple[str, bytes | None]] = []

    def apply(change, preimage):
        seen.append((change.path, preimage))
        target.write_bytes(preimage)

    report = env.snapshots.undo(
        run.run_id,
        Workspace(env.ws),
        guard=lambda: ensure_not_blocked(env.log, env.ws),
        apply=apply,
    )
    assert report.ok, report.problems
    assert seen and seen[0][0] == "note.md"
    assert target.read_text(encoding="utf-8") == ORIGINAL


def test_second_dispatch_of_same_call_is_refused(env):
    task, run, claim, request, args, decision = _admit_overwrite(env)
    assert env.dispatcher.dispatch(request.request_id, claim).ok

    with pytest.raises(DispatchError) as ei:
        env.dispatcher.dispatch(request.request_id, claim)
    assert "已经派发过" in str(ei.value)


def test_grant_expired_before_open_is_rejected(env, monkeypatch):
    """派发事务已提交，但守卫排队太久导致 grant 过期 → 不许写。"""
    task, run, claim, request, args, decision = _admit_overwrite(env)

    real_clock = env.dispatcher._clock  # noqa: SLF001
    base = real_clock()
    monkeypatch.setattr(
        env.dispatcher, "_clock", lambda: base + 10 * 60_000
    )
    monkeypatch.setattr(env.dispatcher._executor, "_clock", lambda: base + 10 * 60_000)  # noqa: SLF001

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "rejected"
    assert "过期" in result.output["error"]
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_control_run_has_its_own_identity(env):
    """undo 的控制 Run 不能借用历史 Run 的身份。"""
    task, run, claim = env.new_run()
    env.leases.release_run(run.run_id)
    control = env.dispatcher.open_control_run(
        goal=f"undo:{run.run_id}",
        workspaces=[WorkspaceBinding(str(env.ws))],
    )
    assert control.run.run_id != run.run_id
    assert control.task.task_id != task.task_id
    assert control.claim.run_id == control.run.run_id
    assert control.lease_paths == (str(env.ws.resolve()),)

    env.dispatcher.close_control_run(control, outcome="succeeded", effect_state="applied")
    finished = env.tasks.get_run(control.run.run_id)
    assert finished.state == "succeeded"


def test_prepare_twice_does_not_duplicate_pending(env):
    """反复 prepare 只该有一张待批，不能刷屏。"""
    task, run, claim = env.new_run()
    request, args = _overwrite_request(env, task, run, claim)
    first = env.dispatcher.prepare(request, claim, args=args)
    second = env.dispatcher.prepare(request, claim, args=args)
    assert first.kind == second.kind == KIND_WAITING
    assert first.approval_id == second.approval_id
    assert len(env.approvals.pending()) == 1


def test_stale_revision_request_is_rejected(env):
    """改目标之后，旧 revision 的请求不得派发。"""
    task, run, claim, request, args, decision = _admit_overwrite(env)

    env.tasks.revise_goal(task.task_id, "换个目标")

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "rejected"
    assert "fence" in result.output["error"] or "revision/epoch" in result.output["error"]
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_dispatch_rejects_foreign_instance(env):
    """拿别的实例的 claim 在本进程派发 → 拒绝（本进程不可能持有它的租约）。"""
    task, run, claim, request, args, decision = _admit_overwrite(env)

    foreign = JobClaim(
        job_id=claim.job_id,
        run_id=claim.run_id,
        owner_instance_id="not-this-process",
        owner_pid=claim.owner_pid,
        claim_epoch=claim.claim_epoch,
    )
    result = env.dispatcher.dispatch(request.request_id, foreign)
    assert result.status == "rejected"
    assert result.effect_state == "none_applied"
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL


def test_prepare_rejects_tampered_args(env):
    task, run, claim = env.new_run()
    request, args = _overwrite_request(env, task, run, claim)
    with pytest.raises(DispatchError) as ei:
        env.dispatcher.prepare(request, claim, args={**args, "content": "别的"})
    assert "args_hash" in str(ei.value)


def test_awaiting_approval_blocks_nothing_else(env):
    """等批准期间不该持守卫/事务：别的 Run 仍能正常派发自己的自动动作。"""
    task, run, claim = env.new_run()
    request, args = _overwrite_request(env, task, run, claim)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_WAITING

    ws2 = env.root / "ws2"
    ws2.mkdir()
    other = env.new_run(goal="另一个任务", root=ws2)
    _ot, other_run, other_claim = other
    target = ws2 / "another.md"
    other_args = {"name": "another.md", "content": "x\n"}
    other_request = build_request(
        task=_ot, run=other_run, claim=other_claim, call_id="c_other",
        tool_id="create_report", target=target, args=other_args,
    )
    assert env.dispatcher.prepare(other_request, other_claim, args=other_args).is_admitted
    result = env.dispatcher.dispatch(other_request.request_id, other_claim)
    assert result.ok, result.output


def test_observe_state_tracks_content(env):
    """写前状态是"不存在"或内容哈希，不是空字符串。审批绑定的就是它。"""
    assert observe_state(env.ws / "missing.md") == "absent"
    assert observe_state(env.ws / "note.md") == _sha(ORIGINAL)

def test_same_call_id_in_two_runs_does_not_collide(env):
    """不同 Run 里同名 call_id（模型就爱给 c1）：派发记录各归各的。

    provider 的短 ID 不是全局身份。若拿它当派发主键，两个 run 的同名调用会
    互相顶掉——一个 run 的写入会认成另一个 run 的。
    """
    ws_b = env.root / "ws_b"
    ws_b.mkdir()
    (ws_b / "note.md").write_text(ORIGINAL, encoding="utf-8")

    task_a, run_a, claim_a = env.new_run(goal="甲", root=env.ws)
    task_b, run_b, claim_b = env.new_run(goal="乙", root=ws_b)

    req_a, args_a = _overwrite_request(env, task_a, run_a, claim_a, call_id="c1")
    dec_a = env.dispatcher.prepare(req_a, claim_a, args=args_a)
    _approve(env, dec_a.approval_id)

    args_b = {"path": "note.md", "content": "乙改的\n"}
    req_b = build_request(
        task=task_b, run=run_b, claim=claim_b, call_id="c1",
        tool_id="modify_text", target=ws_b / "note.md", args=args_b,
    )
    dec_b = env.dispatcher.prepare(req_b, claim_b, args=args_b)
    _approve(env, dec_b.approval_id)

    assert env.dispatcher.dispatch(req_a.request_id, claim_a).ok
    assert env.dispatcher.dispatch(req_b.request_id, claim_b).ok

    rows = env.log.conn.execute(
        "SELECT run_id, dispatch_id FROM dispatches WHERE call_id='c1' ORDER BY run_id"
    ).fetchall()
    assert len(rows) == 2, "两个 run 的同名调用必须各有一行"
    assert rows[0]["dispatch_id"] != rows[1]["dispatch_id"]
    assert {(env.ws / "note.md").read_text(encoding="utf-8"),
            (ws_b / "note.md").read_text(encoding="utf-8")} == {"改后的内容\n", "乙改的\n"}


def test_internal_restore_tool_is_not_exposed_to_the_model(env):
    """`restore_file` 只给 undo 用，绝不能出现在模型可见的工具清单里。"""
    from tianshu.tools import SPECS, tool_manifest

    names = {item["name"] for item in tool_manifest()}
    assert "restore_file" not in names
    assert "delete_file" in names
    assert "restore_file" in SPECS


def test_execution_crash_is_recorded_as_unknown_and_blocks(env, monkeypatch):
    """写入阶段炸了 → unknown + 立即阻断；不能记成"没写成功"。

    "确定没写"和"不知道写没写"是两件事。后者必须挡住这个目录的后续写入，
    直到有人用 recover 核对过。
    """

    class ExplodingExecutor:
        def execute(self, admitted, permit):
            raise OSError("磁盘满了")

    task, run, claim, request, args, decision = _admit_overwrite(env)
    monkeypatch.setattr(env.dispatcher, "_executor", ExplodingExecutor())

    result = env.dispatcher.dispatch(request.request_id, claim)
    assert result.status == "failed"
    assert result.effect_state == "unknown"

    row = env.dispatcher.dispatch_record(run.run_id, request.call_id)
    assert row.state == "error"

    types = _types(env.log, run.run_id)
    assert "tool.result" in types
    assert "recovery.blocked" in types

    # 这个工作区现在被挡住了：下一次写入直接拒绝
    target = env.ws / "another.md"
    other_args = {"name": "another.md", "content": "x\n"}
    other = build_request(
        task=task, run=run, claim=claim, call_id="c_next",
        tool_id="create_report", target=target, args=other_args,
    )
    decision2 = env.dispatcher.prepare(other, claim, args=other_args)
    assert decision2.kind == KIND_REJECTED
    assert decision2.rule_id == "dispatch.workspace_blocked"
