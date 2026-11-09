"""M2 · PR B Task 3 验收：动作分类、授权摘要、审批与一次性 grant。

要证明的是四件事：

1. **分类表是完整的**：目录内读/新建自动放行，覆盖/删除/目录外必须人批，
   未支持动作直接拒绝（不是给一个无法履行的"需要提权"）。
2. **摘要绑住每一个授权字段**：参数、路径、执行前状态、revision、epoch、
   工具版本、claim —— 改任何一个，摘要必须变。旧审批因此无法复用。
3. **审批是人做的**：模型不能构造 HumanIdentity，批准一次只能消费一次。
4. **grant 短命**：不超过 30 秒，且不得超过资源租约剩余期限。
"""

from __future__ import annotations

import pytest

from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.permissions import (
    APPROVAL_DECIDED,
    APPROVAL_EXPIRED,
    APPROVAL_REQUESTED,
    APPROVAL_REVOKED,
    APPROVAL_SUPERSEDED,
    APPROVAL_TTL_MS,
    GRANT_CONSUMED,
    GRANT_ISSUED,
    GRANT_TTL_MS,
    ActionRequest,
    ApprovalError,
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.workspace import WorkspaceBinding


def _request(**overrides) -> ActionRequest:
    base = dict(
        request_id="req_1",
        task_id="task_1",
        run_id="run_1",
        job_id="job_1",
        claim_epoch=1,
        owner_instance_id="inst_1",
        goal_revision=1,
        lease_epoch=1,
        call_id="call_1",
        attempt_id=1,
        tool_id="create_report",
        tool_version="1.0",
        schema_hash="schema_abc",
        action="create",
        resources=("/ws/a.txt",),
        args_hash="args_abc",
        preconditions={"target": "absent"},
    )
    base.update(overrides)
    return ActionRequest(**base)


@pytest.fixture()
def env(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    now = [1_000_000]
    approvals = ApprovalStore(log, clock=lambda: now[0])
    grants = GrantStore(log, clock=lambda: now[0])
    yield log, approvals, grants, now, tmp_path
    log.close()


def _cli() -> HumanIdentity:
    return HumanIdentity.local_cli()


# ---------------------------------------------------------------- 分类


def test_classification_table(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = PolicyEngine()
    bindings = [WorkspaceBinding(str(ws))]

    inside = str(ws / "a.txt")
    assert policy.classify("read", [inside], bindings).rule_id == "c4.auto"
    assert policy.classify("create", [inside], bindings).rule_id == "c4.auto"
    assert policy.classify("overwrite", [inside], bindings).rule_id == "c4.overwrite"
    assert policy.classify("delete", [inside], bindings).rule_id == "c4.delete"


def test_overwrite_and_delete_need_approval(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = PolicyEngine()
    bindings = [WorkspaceBinding(str(ws))]

    for action in ("overwrite", "delete"):
        decision = policy.classify(action, [str(ws / "a.txt")], bindings)
        assert decision.needs_approval
        assert not decision.is_auto


def test_create_over_existing_is_denied_with_a_pointer_to_overwrite(tmp_path):
    """调用方说"新建"但目标已存在：不是"批准一下就能覆盖"。

    `create_report` 明确拒绝覆盖（M1 的 `OverwriteRefused`）。这里若返回
    "需审批"，用户会批一张**永远执行不了**的批准；正确结论是拒绝，并指出
    该改用 `modify_text`（那才是删除类、走审批的那条路）。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    bindings = [WorkspaceBinding(str(ws))]
    decision = PolicyEngine().classify(
        "create", [str(ws / "a.txt")], bindings, {"target_exists": True}
    )
    assert decision.is_denied
    assert decision.rule_id == "c4.create_over_existing"
    assert "modify_text" in decision.reason, "拒绝理由要给出下一步该怎么做"


def test_outside_workspace_needs_approval_not_silent_deny(tmp_path):
    """目录外不是"直接拒绝"，是"得有人同意这一次"。
    但同意一次不等于永久扩大作用范围——那是 binding 变更，要走 fencing。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = PolicyEngine()
    decision = policy.classify("read", ["/etc/passwd"], [WorkspaceBinding(str(ws))])
    assert decision.needs_approval
    assert decision.rule_id == "c4.outside_workspace"


def test_readonly_binding_rejects_writes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = PolicyEngine()
    ro = [WorkspaceBinding(str(ws), mode="readonly")]
    assert policy.classify("read", [str(ws / "a")], ro).is_auto
    assert policy.classify("create", [str(ws / "a")], ro).is_denied
    assert policy.classify("overwrite", [str(ws / "a")], ro).is_denied


def test_zero_binding_task_denies_everything(tmp_path):
    """0 目录合法，但不能隐式获得 cwd 权限。"""
    policy = PolicyEngine()
    assert policy.classify("read", [str(tmp_path / "a")], []).is_denied


def test_unsupported_action_is_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = PolicyEngine()
    decision = policy.classify("shell", [str(ws)], [WorkspaceBinding(str(ws))])
    assert decision.is_denied
    assert decision.rule_id == "c4.unsupported"


def test_action_without_resources_is_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = PolicyEngine()
    assert policy.classify("read", [], [WorkspaceBinding(str(ws))]).is_denied


def test_parent_dir_binding_covers_child(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    policy = PolicyEngine()
    assert policy.classify(
        "create", [str(ws / "sub" / "a.txt")], [WorkspaceBinding(str(ws))]
    ).is_auto


def test_child_binding_does_not_cover_parent(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    policy = PolicyEngine()
    decision = policy.classify(
        "create", [str(ws / "a.txt")], [WorkspaceBinding(str(ws / "sub"))]
    )
    assert decision.needs_approval


# ---------------------------------------------------------------- 摘要


def test_digest_is_stable_for_same_request():
    assert _request().digest() == _request().digest()


def test_digest_ignores_resource_order():
    a = _request(resources=("/ws/a", "/ws/b"))
    b = _request(resources=("/ws/b", "/ws/a"))
    assert a.digest() == b.digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "req_2"),
        ("task_id", "task_2"),
        ("run_id", "run_2"),
        ("job_id", "job_2"),
        ("claim_epoch", 2),
        ("owner_instance_id", "inst_2"),
        ("goal_revision", 2),
        ("lease_epoch", 2),
        ("call_id", "call_2"),
        ("attempt_id", 2),
        ("tool_id", "modify_text"),
        ("tool_version", "2.0"),
        ("schema_hash", "schema_def"),
        ("action", "overwrite"),
        ("resources", ("/ws/other.txt",)),
        ("args_hash", "args_def"),
        ("preconditions", {"target": "sha256:abc"}),
        ("policy_version", "c4-2"),
        ("contract_version", "1.1"),
    ],
)
def test_digest_binds_every_authority_field(field, value):
    """少绑一个字段，旧审批就能被复用到新动作上。逐个字段证明它被绑住了。"""
    assert _request().digest() != _request(**{field: value}).digest()


# ---------------------------------------------------------------- 审批


def _pending(approvals, request=None, decision=None):
    request = request or _request(action="overwrite")
    decision = decision or PolicyEngine().classify(
        "overwrite", list(request.resources), [WorkspaceBinding("/ws")]
    )
    return approvals.request(request, decision)


def test_request_creates_pending_approval(env):
    _, approvals, _, _, _ = env
    approval = _pending(approvals)
    assert approval.state == "pending"
    assert approval.request_digest == _request(action="overwrite").digest()
    assert approval.decided_by is None
    assert approvals.pending() == [approval]


def test_request_is_recorded_as_event(env):
    log, approvals, _, _, _ = env
    approval = _pending(approvals)
    events = log.read_types([APPROVAL_REQUESTED])
    assert len(events) == 1
    assert events[0].payload["approval_id"] == approval.approval_id
    assert events[0].payload["request_digest"] == approval.request_digest


def test_approve_transitions_to_approved(env):
    _, approvals, _, _, _ = env
    approval = _pending(approvals)
    decided = approvals.decide(
        approval.approval_id, decision="approved", actor=_cli()
    )
    assert decided.state == "approved"
    assert decided.decided_by == _cli().actor
    assert approvals.pending() == []


def test_reject_transitions_to_rejected(env):
    log, approvals, _, _, _ = env
    approval = _pending(approvals)
    decided = approvals.decide(
        approval.approval_id, decision="rejected", actor=_cli()
    )
    assert decided.state == "rejected"
    assert log.read_types([APPROVAL_DECIDED])[0].payload["decision"] == "rejected"


def test_cannot_decide_twice(env):
    _, approvals, _, _, _ = env
    approval = _pending(approvals)
    approvals.decide(approval.approval_id, decision="approved", actor=_cli())
    with pytest.raises(ApprovalError):
        approvals.decide(approval.approval_id, decision="rejected", actor=_cli())


def test_model_cannot_forge_a_human_identity(env):
    """模型的工具参数里出现"批准人"结构，一律视为伪造。"""
    _, approvals, _, _, _ = env
    approval = _pending(approvals)
    forged = {"actor": "cli:local", "source": "cli"}
    with pytest.raises(ApprovalError):
        approvals.decide(approval.approval_id, decision="approved", actor=forged)
    with pytest.raises(ApprovalError):
        approvals.revoke(approval.approval_id, actor=forged)
    assert approvals.get(approval.approval_id).state == "pending"


def test_approval_expires(env):
    _, approvals, _, now, _ = env
    approval = _pending(approvals)
    now[0] += APPROVAL_TTL_MS + 1
    with pytest.raises(ApprovalError) as ei:
        approvals.decide(approval.approval_id, decision="approved", actor=_cli())
    assert "过期" in str(ei.value)


def test_expire_due_marks_pending_as_expired(env):
    log, approvals, _, now, _ = env
    kept = _pending(approvals)
    doomed = _pending(approvals, request=_request(request_id="req_2",
                                                 resources=("/ws/b",)))
    now[0] += APPROVAL_TTL_MS + 1
    assert approvals.expire_due() == 2
    assert approvals.get(doomed.approval_id).state == "expired"
    assert approvals.get(kept.approval_id).state == "expired"
    assert len(log.read_types([APPROVAL_EXPIRED])) == 2


def test_revoke_blocks_approved_approval(env):
    log, approvals, _, _, _ = env
    approval = _pending(approvals)
    approvals.decide(approval.approval_id, decision="approved", actor=_cli())
    revoked = approvals.revoke(approval.approval_id, actor=_cli())
    assert revoked.state == "revoked"
    assert log.read_types([APPROVAL_REVOKED])


def test_supersede_kills_approvals_of_a_run(env):
    """改目标后旧审批立刻作废——不能悄悄重新绑定到新目标上。"""
    log, approvals, _, _, _ = env
    first = _pending(approvals)
    approvals.decide(first.approval_id, decision="approved", actor=_cli())
    assert approvals.supersede_for_run("run_1", reason="goal_revised") == 1
    assert approvals.get(first.approval_id).state == "superseded"
    assert log.read_types([APPROVAL_SUPERSEDED])


def test_same_digest_is_not_duplicated(env):
    """同一个动作重复请求不应该刷出一堆待批条目。"""
    _, approvals, _, _, _ = env
    first = _pending(approvals)
    again = _pending(approvals)
    assert first.approval_id == again.approval_id
    assert len(approvals.pending()) == 1


def test_expired_pending_does_not_block_a_new_request(env):
    """过期的待批项不能被当成"已有审批"复用，否则这个动作再也发不出来。"""
    _, approvals, _, now, _ = env
    stale = _pending(approvals)
    now[0] += APPROVAL_TTL_MS + 1
    fresh = _pending(approvals)
    assert fresh.approval_id != stale.approval_id
    assert fresh.state == "pending"


def test_denied_request_cannot_be_filed(env):
    _, approvals, _, _, _ = env
    denied = PolicyEngine().classify("shell", ["/ws"], [WorkspaceBinding("/ws")])
    with pytest.raises(ApprovalError):
        approvals.request(_request(), denied)


def test_auto_decision_does_not_create_approval(env):
    _, approvals, _, _, _ = env
    auto = PolicyEngine().classify("read", ["/ws/a"], [WorkspaceBinding("/ws")])
    with pytest.raises(ApprovalError):
        approvals.request(_request(), auto)


# ---------------------------------------------------------------- grant


def _auto_grant(grants, request=None):
    request = request or _request(action="read", resources=("/ws/a",))
    return grants.issue(subject="run_1", request=request, issuer="c4.auto")


def test_grant_is_issued_and_consumed_once(env):
    log, _, grants, _, _ = env
    request = _request(action="read", resources=("/ws/a",))
    grant = _auto_grant(grants, request)
    assert grant.remaining_uses == 1
    assert log.read_types([GRANT_ISSUED])

    used = grants.consume(
        grant.grant_id, request_digest=request.digest(), subject="run_1"
    )
    assert used.remaining_uses == 0
    assert log.read_types([GRANT_CONSUMED])

    with pytest.raises(ApprovalError):
        grants.consume(grant.grant_id, request_digest=request.digest(), subject="run_1")


def test_grant_cannot_be_used_for_other_digest(env):
    _, _, grants, _, _ = env
    request = _request(action="read", resources=("/ws/a",))
    grant = _auto_grant(grants, request)
    other = _request(action="read", resources=("/ws/b",))
    with pytest.raises(ApprovalError):
        grants.consume(grant.grant_id, request_digest=other.digest(), subject="run_1")


def test_grant_cannot_be_used_by_other_subject(env):
    _, _, grants, _, _ = env
    request = _request(action="read", resources=("/ws/a",))
    grant = _auto_grant(grants, request)
    with pytest.raises(ApprovalError):
        grants.consume(
            grant.grant_id, request_digest=request.digest(), subject="run_other"
        )


def test_grant_consumed_event_is_scoped_to_run(env):
    """grant 事件必须挂在 run 上；不能拿 subject 冒充 run_id。"""
    log, _, grants, _, _ = env
    request = _request(action="read", resources=("/ws/a",))
    grant = _auto_grant(grants, request)
    grants.consume(grant.grant_id, request_digest=request.digest(), subject="run_1")
    event = log.read_types([GRANT_CONSUMED])[0]
    assert event.run_id == "run_1"
    assert event.payload["grant_id"] == grant.grant_id


def test_grant_records_run_and_task(env):
    _, _, grants, _, _ = env
    grant = _auto_grant(grants)
    assert grant.run_id == "run_1"
    assert grant.task_id == "task_1"


def test_grant_expires(env):
    _, _, grants, now, _ = env
    request = _request(action="read", resources=("/ws/a",))
    grant = _auto_grant(grants, request)
    now[0] += GRANT_TTL_MS + 1
    with pytest.raises(ApprovalError):
        grants.consume(grant.grant_id, request_digest=request.digest(), subject="run_1")


def test_grant_never_outlives_the_lease(env):
    _, _, grants, now, _ = env
    request = _request(action="read", resources=("/ws/a",))
    grant = grants.issue(
        subject="run_1",
        request=request,
        issuer="c4.auto",
        ttl_ms=GRANT_TTL_MS,
        max_expires_at=now[0] + 1_000,
    )
    assert grant.expires_at == now[0] + 1_000


def test_grant_refuses_to_issue_after_lease_expired(env):
    _, _, grants, now, _ = env
    with pytest.raises(ApprovalError):
        grants.issue(
            subject="run_1",
            request=_request(),
            issuer="c4.auto",
            max_expires_at=now[0] - 1,
        )


def test_store_cannot_be_built_inside_transaction(env):
    log, _, _, _, _ = env
    with log.transaction():
        with pytest.raises(TianshuError):
            ApprovalStore(log)
        with pytest.raises(TianshuError):
            GrantStore(log)