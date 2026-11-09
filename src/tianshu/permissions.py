"""C4 权限：动作分类、授权摘要、审批与一次性 grant（M2 · PR B 的 Task 3）。

这里只做**判断与记录**，不碰文件。真正的执行由 PR B 的 Task 4（Dispatcher）
在一个事务里消费审批 + 登记 intent 后交给执行器。分开的理由：

- 判断要能被单独测试（每个字段变了摘要必须变）;
- 审批是"人"的动作，必须与模型无关；模型不能自己批准自己;
- 消费必须与 intent 同事务，否则会出现"批准被用掉但没执行"或"执行了两次"。

三条硬约束：

1. **审批绑定的是完整动作摘要**，不是"允许写这个目录"。参数、目标路径、
   执行前状态、revision、epoch、工具版本，任一变化摘要就变，旧审批无法复用。
2. **批准一次 = 消费一次**。`remaining_uses` 默认 1，且不能由模型指定。
3. **批准人只能来自本地 CLI 控制端**。`HumanIdentity` 只能由 CLI 构造，
   模型的工具参数里出现同名结构一律视为伪造。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .canonical import jcs_sha256
from .errors import TianshuError
from .events import EventLog
from .workspace import WorkspaceBinding, is_ancestor_or_self, overlap_key

APPROVAL_TTL_MS = 10 * 60_000
GRANT_TTL_MS = 30_000

APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
APPROVAL_REVOKED = "approval.revoked"
APPROVAL_EXPIRED = "approval.expired"
APPROVAL_SUPERSEDED = "approval.superseded"
GRANT_ISSUED = "grant.issued"
GRANT_CONSUMED = "grant.consumed"
POLICY_DENIED = "policy.denied"

APPROVAL_STATES = {
    "pending",
    "approved",
    "rejected",
    "expired",
    "revoked",
    "superseded",
}

# 支持的动作。不在表里的动作一律拒绝，而不是给一个无法履行的
# REQUIRE_ELEVATION——"拒绝了但没拒绝"比直接拒绝更危险。
ACTION_READ = "read"
ACTION_CREATE = "create"
ACTION_OVERWRITE = "overwrite"
ACTION_DELETE = "delete"

SUPPORTED_ACTIONS = {ACTION_READ, ACTION_CREATE, ACTION_OVERWRITE, ACTION_DELETE}

# 不需要人批的动作
AUTO_ACTIONS = {ACTION_READ, ACTION_CREATE}
# 必须人批的动作
APPROVAL_ACTIONS = {ACTION_OVERWRITE, ACTION_DELETE}

DECISION_AUTO = "auto"
DECISION_APPROVE = "approve"
DECISION_DENY = "deny"


class ApprovalError(TianshuError):
    """审批状态不允许这个操作。"""


def _now() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class HumanIdentity:
    """批准人。只能由 CLI 控制端构造。

    `actor` 是给人看的标识（"cli:local"），不是认证凭据——本项目不声称防同 OS
    用户篡改。这里只保证一件事：**模型无法构造它**，所以模型不能自己批准自己。
    """

    actor: str
    source: str = "cli"

    @classmethod
    def local_cli(cls) -> HumanIdentity:
        """CLI 控制端的身份。这是构造批准人的唯一入口。"""
        return cls(actor="cli:local", source="cli")

    def to_dict(self) -> dict[str, Any]:
        return {"actor": self.actor, "source": self.source}


@dataclass(frozen=True)
class ActionRequest:
    """一次"想做什么"的完整描述。摘要必须绑住所有授权相关字段。"""

    request_id: str
    task_id: str
    run_id: str
    job_id: str
    claim_epoch: int
    owner_instance_id: str
    goal_revision: int
    lease_epoch: int
    call_id: str
    attempt_id: int
    tool_id: str
    tool_version: str
    schema_hash: str
    action: str
    resources: tuple[str, ...]
    args_hash: str
    preconditions: dict[str, Any] = field(default_factory=dict)
    policy_version: str = "c4-1"
    contract_version: str = "1.0"

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "args_hash": self.args_hash,
            "attempt_id": self.attempt_id,
            "call_id": self.call_id,
            "claim_epoch": self.claim_epoch,
            "contract_version": self.contract_version,
            "goal_revision": self.goal_revision,
            "job_id": self.job_id,
            "lease_epoch": self.lease_epoch,
            "owner_instance_id": self.owner_instance_id,
            "policy_version": self.policy_version,
            "preconditions": self.preconditions,
            "request_id": self.request_id,
            "resources": sorted(self.resources),
            "run_id": self.run_id,
            "schema_hash": self.schema_hash,
            "task_id": self.task_id,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
        }

    def digest(self) -> str:
        return jcs_sha256(self.to_canonical_dict())


@dataclass(frozen=True)
class PolicyDecision:
    kind: str  # auto / approve / deny
    reason: str
    rule_id: str

    @property
    def is_auto(self) -> bool:
        return self.kind == DECISION_AUTO

    @property
    def needs_approval(self) -> bool:
        return self.kind == DECISION_APPROVE

    @property
    def is_denied(self) -> bool:
        return self.kind == DECISION_DENY


@dataclass
class Approval:
    approval_id: str
    request_id: str
    request_digest: str
    task_id: str
    run_id: str
    goal_revision: int
    lease_epoch: int
    action: str
    resources: list[str]
    state: str
    reason: str = ""
    decided_by: str | None = None
    created_at: int = 0
    decided_at: int | None = None
    expires_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "goal_revision": self.goal_revision,
            "lease_epoch": self.lease_epoch,
            "action": self.action,
            "resources": self.resources,
            "state": self.state,
            "reason": self.reason,
            "decided_by": self.decided_by,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "expires_at": self.expires_at,
        }


@dataclass
class Grant:
    grant_id: str
    subject: str
    request_digest: str
    action: str
    issuer: str
    remaining_uses: int
    created_at: int
    expires_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "subject": self.subject,
            "request_digest": self.request_digest,
            "action": self.action,
            "issuer": self.issuer,
            "remaining_uses": self.remaining_uses,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    goal_revision   INTEGER NOT NULL,
    lease_epoch     INTEGER NOT NULL,
    action          TEXT NOT NULL,
    resources       TEXT NOT NULL,
    state           TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    decided_by      TEXT,
    created_at      INTEGER NOT NULL,
    decided_at      INTEGER,
    expires_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_digest ON approvals(request_digest);

CREATE TABLE IF NOT EXISTS grants (
    grant_id       TEXT PRIMARY KEY,
    subject        TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    action         TEXT NOT NULL,
    issuer         TEXT NOT NULL,
    remaining_uses INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grants_digest ON grants(request_digest);
"""


class PolicyEngine:
    """唯一策略判定点。

    它只看"请求 + 授权目录绑定"，不看调用者是谁——执行侧（Host/Worker）
    不许重新判定策略，只负责核验"这张 grant/审批是不是对这次动作的"。
    """

    def __init__(self, *, policy_version: str = "c4-1") -> None:
        self.policy_version = policy_version

    def classify(
        self,
        action: str,
        resources: list[str],
        bindings: list[WorkspaceBinding],
        preconditions: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        if action not in SUPPORTED_ACTIONS:
            return PolicyDecision(
                DECISION_DENY, f"不支持的动作：{action}", "c4.unsupported"
            )
        if not resources:
            return PolicyDecision(DECISION_DENY, "动作没有声明资源", "c4.no_resource")

        bindings = [WorkspaceBinding.of(b) for b in bindings]
        if not bindings:
            # 0 目录任务不能隐式获得 cwd 权限
            return PolicyDecision(
                DECISION_DENY, "任务没有绑定任何工作区", "c4.no_binding"
            )

        for resource in resources:
            matched = [b for b in bindings if self._covers(b, resource)]
            if not matched:
                return PolicyDecision(
                    DECISION_APPROVE,
                    f"目录外访问：{resource}",
                    "c4.outside_workspace",
                )
            if action == ACTION_READ:
                continue
            if not any(b.can_write for b in matched):
                return PolicyDecision(
                    DECISION_DENY,
                    f"目标目录是只读绑定：{resource}",
                    "c4.readonly_binding",
                )

        preconditions = preconditions or {}

        if action in AUTO_ACTIONS:
            # "新建"只有在目标确实不存在时才是新建。目标已存在而调用方仍称新建，
            # 那就是覆盖——按删除类处理。执行侧还会再核一次 before_state。
            if action == ACTION_CREATE and preconditions.get("target_exists") is True:
                return PolicyDecision(
                    DECISION_APPROVE,
                    "目标已存在，按覆盖处理",
                    "c4.create_over_existing",
                )
            return PolicyDecision(DECISION_AUTO, "目录内读/新建", "c4.auto")

        if action == ACTION_OVERWRITE:
            return PolicyDecision(
                DECISION_APPROVE, "覆盖已有内容是删除类操作", "c4.overwrite"
            )
        if action == ACTION_DELETE:
            return PolicyDecision(DECISION_APPROVE, "删除需要审批", "c4.delete")
        return PolicyDecision(DECISION_DENY, f"未覆盖的动作：{action}", "c4.unhandled")

    @staticmethod
    def _covers(binding: WorkspaceBinding, resource: str) -> bool:
        root = overlap_key(binding.root_path)
        target = overlap_key(resource)
        return is_ancestor_or_self(root, target)


class ApprovalStore:
    def __init__(self, log: EventLog, *, clock=_now) -> None:
        if log.in_transaction:
            raise TianshuError("ApprovalStore 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._clock = clock
        self._conn.executescript(SCHEMA)

    @property
    def transaction(self):
        return self._log.transaction()

    # ---- 请求 ----

    def request(
        self, request: ActionRequest, decision: PolicyDecision, *,
        ttl_ms: int = APPROVAL_TTL_MS,
    ) -> Approval:
        """登记一个待批请求。同一个摘要已有 pending/approved 时不重复创建。"""
        if decision.is_denied:
            raise ApprovalError(f"策略已拒绝，不需要审批：{decision.reason}")
        if not decision.needs_approval:
            raise ApprovalError(f"该动作不需要审批：{decision.rule_id}")

        digest = request.digest()
        now = self._clock()
        with self._log.transaction() as tx:
            existing = self._find(("pending", "approved"), digest, request.run_id)
            if existing is not None:
                return self._row(existing)

            approval_id = f"appr_{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO approvals (approval_id, request_id, request_digest,"
                " task_id, run_id, goal_revision, lease_epoch, action, resources,"
                " state, reason, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    approval_id,
                    request.request_id,
                    digest,
                    request.task_id,
                    request.run_id,
                    request.goal_revision,
                    request.lease_epoch,
                    request.action,
                    ",".join(sorted(request.resources)),
                    decision.reason,
                    now,
                    now + ttl_ms,
                ),
            )
            tx.append(
                request.run_id,
                APPROVAL_REQUESTED,
                {
                    "approval_id": approval_id,
                    "request_id": request.request_id,
                    "request_digest": digest,
                    "action": request.action,
                    "resources": sorted(request.resources),
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                    "expires_at": now + ttl_ms,
                },
                task_id=request.task_id,
                actor_kind="core",
            )
        return self.get(approval_id)  # type: ignore[return-value]

    def get(self, approval_id: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        return self._row(row) if row else None

    def pending(self, *, run_id: str | None = None) -> list[Approval]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE state='pending' ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE state='pending' AND run_id=?"
                " ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    # ---- 人的决定 ----

    def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        actor: HumanIdentity,
    ) -> Approval:
        """批准或否决。`actor` 必须是 CLI 造出来的 HumanIdentity。"""
        if not isinstance(actor, HumanIdentity):
            raise ApprovalError(
                "批准人必须是 HumanIdentity（只能由本地 CLI 构造），"
                "不能来自模型参数"
            )
        if decision not in ("approved", "rejected"):
            raise ApprovalError(f"未知决定：{decision}")

        now = self._clock()
        with self._log.transaction() as tx:
            current = self.get(approval_id)
            if current is None:
                raise ApprovalError(f"审批不存在：{approval_id}")
            self._assert_pending(current, now)
            if current.goal_revision is None:  # pragma: no cover - 表结构保证非空
                raise ApprovalError("审批缺少 revision")

            cur = self._conn.execute(
                "UPDATE approvals SET state=?, decided_by=?, decided_at=?"
                " WHERE approval_id=? AND state='pending'",
                (decision, actor.actor, now, approval_id),
            )
            if cur.rowcount == 0:
                raise ApprovalError(f"审批状态已变：{approval_id}")
            tx.append(
                current.run_id,
                APPROVAL_DECIDED,
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "decided_by": actor.to_dict(),
                    "request_digest": current.request_digest,
                },
                task_id=current.task_id,
                actor_kind="human",
                actor_id=actor.actor,
            )
        return self.get(approval_id)  # type: ignore[return-value]

    def revoke(self, approval_id: str, *, actor: HumanIdentity) -> Approval:
        if not isinstance(actor, HumanIdentity):
            raise ApprovalError("撤销人必须是 HumanIdentity")
        now = self._clock()
        with self._log.transaction() as tx:
            current = self.get(approval_id)
            if current is None:
                raise ApprovalError(f"审批不存在：{approval_id}")
            if current.state not in ("pending", "approved"):
                raise ApprovalError(f"不能撤销这个状态：{current.state}")
            cur = self._conn.execute(
                "UPDATE approvals SET state='revoked', decided_by=?, decided_at=?"
                " WHERE approval_id=? AND state IN ('pending','approved')",
                (actor.actor, now, approval_id),
            )
            if cur.rowcount == 0:
                raise ApprovalError(f"审批状态已变：{approval_id}")
            tx.append(
                current.run_id,
                APPROVAL_REVOKED,
                {"approval_id": approval_id, "decided_by": actor.to_dict()},
                task_id=current.task_id,
                actor_kind="human",
                actor_id=actor.actor,
            )
        return self.get(approval_id)  # type: ignore[return-value]

    def supersede_for_run(self, run_id: str, *, reason: str) -> int:
        """目标/epoch 变了：这个 Run 的旧审批立刻作废，不能"暗中重新绑定"。"""
        return self._bulk_close(run_id, "superseded", APPROVAL_SUPERSEDED, reason)

    def expire_due(self) -> int:
        """把过期未处理的审批标成 expired。"""
        now = self._clock()
        rows = self._conn.execute(
            "SELECT approval_id FROM approvals"
            " WHERE state='pending' AND expires_at <= ?",
            (now,),
        ).fetchall()
        n = 0
        for row in rows:
            n += self._bulk_close_ids(
                [row["approval_id"]], "expired", APPROVAL_EXPIRED, "ttl"
            )
        return n

    def _bulk_close(self, run_id: str, state: str, event: str, reason: str) -> int:
        rows = self._conn.execute(
            "SELECT approval_id FROM approvals"
            " WHERE run_id=? AND state IN ('pending','approved')",
            (run_id,),
        ).fetchall()
        return self._bulk_close_ids(
            [r["approval_id"] for r in rows], state, event, reason
        )

    def _bulk_close_ids(
        self, approval_ids: list[str], state: str, event: str, reason: str
    ) -> int:
        if not approval_ids:
            return 0
        n = 0
        with self._log.transaction() as tx:
            for approval_id in approval_ids:
                current = self.get(approval_id)
                if current is None:
                    continue
                cur = self._conn.execute(
                    "UPDATE approvals SET state=?, decided_at=?"
                    " WHERE approval_id=? AND state IN ('pending','approved')",
                    (state, self._clock(), approval_id),
                )
                if cur.rowcount == 0:
                    continue
                n += 1
                tx.append(
                    current.run_id,
                    event,
                    {"approval_id": approval_id, "reason": reason},
                    task_id=current.task_id,
                    actor_kind="core",
                )
        return n

    # ---- 内部 ----

    def _assert_pending(self, approval: Approval, now: int) -> None:
        if approval.state != "pending":
            raise ApprovalError(f"审批不在待批状态：{approval.state}")
        if approval.expires_at <= now:
            raise ApprovalError("审批已过期，请重新发起")

    def _find(self, states: tuple[str, ...], digest: str, run_id: str):
        marks = ",".join("?" * len(states))
        return self._conn.execute(
            f"SELECT * FROM approvals WHERE state IN ({marks})"
            " AND request_digest=? AND run_id=? ORDER BY created_at LIMIT 1",
            (*states, digest, run_id),
        ).fetchone()

    @staticmethod
    def _row(row: sqlite3.Row) -> Approval:
        return Approval(
            approval_id=row["approval_id"],
            request_id=row["request_id"],
            request_digest=row["request_digest"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            goal_revision=row["goal_revision"],
            lease_epoch=row["lease_epoch"],
            action=row["action"],
            resources=row["resources"].split(",") if row["resources"] else [],
            state=row["state"],
            reason=row["reason"],
            decided_by=row["decided_by"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            expires_at=row["expires_at"],
        )


class GrantStore:
    """一次性执行许可。

    grant 存在的意义是"把一次判定变成一张只能兑现一次的票据"。它必须短命
    （默认 30 秒，且不得超过资源租约剩余期限），因为它的内容是"此刻可以写"，
    而"此刻"是会过期的。
    """

    def __init__(self, log: EventLog, *, clock=_now) -> None:
        if log.in_transaction:
            raise TianshuError("GrantStore 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._clock = clock
        self._conn.executescript(SCHEMA)

    @property
    def transaction(self):
        return self._log.transaction()

    def issue(
        self,
        *,
        subject: str,
        request: ActionRequest,
        issuer: str,
        ttl_ms: int = GRANT_TTL_MS,
        max_expires_at: int | None = None,
    ) -> Grant:
        now = self._clock()
        expires = now + ttl_ms
        if max_expires_at is not None:
            # 不得比资源租约活得更久
            expires = min(expires, max_expires_at)
        if expires <= now:
            raise ApprovalError("资源租约已失效，无法签发执行许可")

        grant_id = f"grant_{uuid.uuid4().hex[:12]}"
        with self._log.transaction() as tx:
            self._conn.execute(
                "INSERT INTO grants (grant_id, subject, request_digest, action,"
                " issuer, remaining_uses, created_at, expires_at)"
                " VALUES (?,?,?,?,?,1,?,?)",
                (
                    grant_id,
                    subject,
                    request.digest(),
                    request.action,
                    issuer,
                    now,
                    expires,
                ),
            )
            tx.append(
                request.run_id,
                GRANT_ISSUED,
                {
                    "grant_id": grant_id,
                    "subject": subject,
                    "request_digest": request.digest(),
                    "action": request.action,
                    "issuer": issuer,
                    "expires_at": expires,
                },
                task_id=request.task_id,
            )
        return self.get(grant_id)  # type: ignore[return-value]

    def get(self, grant_id: str) -> Grant | None:
        row = self._conn.execute(
            "SELECT * FROM grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            return None
        return Grant(
            grant_id=row["grant_id"],
            subject=row["subject"],
            request_digest=row["request_digest"],
            action=row["action"],
            issuer=row["issuer"],
            remaining_uses=row["remaining_uses"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def consume(self, grant_id: str, *, request_digest: str, subject: str) -> Grant:
        """兑现一次。票据过期、用尽、或摘要/主体不符都拒绝。

        消费与派发必须在同一个事务里（Task 4），这里只提供原子扣减。
        """
        now = self._clock()
        with self._log.transaction() as tx:
            cur = self._conn.execute(
                "UPDATE grants SET remaining_uses = remaining_uses - 1"
                " WHERE grant_id=? AND request_digest=? AND subject=?"
                "   AND remaining_uses > 0 AND expires_at > ?",
                (grant_id, request_digest, subject, now),
            )
            if cur.rowcount == 0:
                raise ApprovalError(f"执行许可不可用或已用尽：{grant_id}")
            grant = self.get(grant_id)
            if grant is None:  # pragma: no cover - 刚更新过
                raise ApprovalError(f"执行许可不存在：{grant_id}")
            tx.append(
                grant.subject,
                GRANT_CONSUMED,
                {
                    "grant_id": grant_id,
                    "request_digest": request_digest,
                    "remaining_uses": grant.remaining_uses,
                },
            )
        return grant


__all__ = [
    "ACTION_CREATE",
    "ACTION_DELETE",
    "ACTION_OVERWRITE",
    "ACTION_READ",
    "APPROVAL_ACTIONS",
    "APPROVAL_STATES",
    "APPROVAL_TTL_MS",
    "AUTO_ACTIONS",
    "ActionRequest",
    "Approval",
    "ApprovalError",
    "ApprovalStore",
    "DECISION_APPROVE",
    "DECISION_AUTO",
    "DECISION_DENY",
    "GRANT_TTL_MS",
    "Grant",
    "GrantStore",
    "HumanIdentity",
    "PolicyDecision",
    "PolicyEngine",
    "SUPPORTED_ACTIONS",
]