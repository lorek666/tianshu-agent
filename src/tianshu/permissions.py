"""C4 权限：动作分类、授权摘要、审批与一次性 grant（M2 · PR B 的 Task 3）。

这里只做**判断与记录**，不碰文件。真正的执行由 PR B 的 Task 4（Dispatcher）
在一个事务里消费审批 + 登记 intent 后交给执行器。分开的理由：

- 判断要能被单独测试（每个字段变了摘要必须变）;
- 审批是"人"的动作，必须与模型无关；模型不能自己批准自己;
- 消费必须与 intent 同事务，否则会出现"批准被用掉但没执行"或"执行了两次"。

三条硬约束：

1. **审批绑定的是动作摘要**，不是"允许写这个目录"，也**不是**"哪个进程去改"。
   参数、目标路径、执行前状态、goal_revision、lease_epoch、工具版本，任一
   变化动作摘要就变，旧审批无法复用；而 owner / claim_epoch 变化**不影响**
   审批——它们只影响执行资格（见 `ActionRequest.action_digest` 的注释）。
2. **批准一次 = 消费一次**。`remaining_uses` 默认 1，且不能由模型指定。
3. **批准人只能来自本地 CLI 控制端**。`HumanIdentity` 只能由 CLI 构造，
   模型的工具参数里出现同名结构一律视为伪造。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .canonical import jcs_sha256
from .errors import TianshuError
from .events import EventLog
from .resources import (
    KIND_MEMORY,
    MEMORY_SCOPES,
    ResourceError,
    parse_resource,
)
from .workspace import WorkspaceBinding, is_ancestor_or_self, overlap_key

APPROVAL_TTL_MS = 10 * 60_000
GRANT_TTL_MS = 30_000

APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
APPROVAL_CONSUMED = "approval.consumed"
APPROVAL_REVOKED = "approval.revoked"
APPROVAL_EXPIRED = "approval.expired"
APPROVAL_SUPERSEDED = "approval.superseded"
GRANT_ISSUED = "grant.issued"
GRANT_CONSUMED = "grant.consumed"
POLICY_DENIED = "policy.denied"

APPROVAL_STATES = {
    "pending",
    "approved",
    "consumed",
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
#: 执行命令（F1 的 spawn）。它是**全系统最高危的动作**：不经文件动作授权，
#: 但能读写任意文件、联网、启动子进程。独立成一个动作，不许映射成 read。
ACTION_EXEC = "exec"

SUPPORTED_ACTIONS = {
    ACTION_READ,
    ACTION_CREATE,
    ACTION_OVERWRITE,
    ACTION_DELETE,
    ACTION_EXEC,
}

# 不需要人批的动作
AUTO_ACTIONS = {ACTION_READ, ACTION_CREATE}
# 必须人批的动作
APPROVAL_ACTIONS = {ACTION_OVERWRITE, ACTION_DELETE, ACTION_EXEC}

DECISION_AUTO = "auto"
DECISION_APPROVE = "approve"
DECISION_DENY = "deny"

#: 记忆种类的**兼容回退集**。
#:
#: 权威定义在 `memory.py` 的 `MEMORY_KINDS`（队友实现，锚定签名）。这里只在
#: 它还没加载/不存在时保持 fail-closed 校验可用；真正的模块一出现，下面的
#: `supported_memory_kinds()` 就会用模块里的集合覆盖它。两份集合不一致属于
#: 契约漂移，`tests/test_memory_dispatch.py` 会在 `memory.py` 存在时直接比对。
_MEMORY_KINDS_FALLBACK: frozenset[str] = frozenset(
    {"preference", "fact", "decision", "procedure"}
)


def supported_memory_kinds() -> frozenset[str]:
    """返回当前进程里权威的 `MEMORY_KINDS`。

    **延迟导入**：`permissions` 是授权层，不能因为记忆模块尚未加载就把整个
    权限系统拖挂；也不能在这里复制一份"看起来一样"的集合当作事实。导入失败
    时回退到 F10 §5 的封闭集，并保持 fail-closed（未知 kind 仍然拒绝）。
    """
    try:
        from .memory import MEMORY_KINDS as kinds

        return frozenset(kinds)
    except ImportError:  # pragma: no cover - 集成阶段由真实模块覆盖
        return _MEMORY_KINDS_FALLBACK


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

    #: 这四个字段回答的是"**谁**在什么身份下执行"，不是"执行**什么**"。
    #:
    #: 它们必须留在 `digest()` 里：派发前拿当前 claim 逐项核验，旧 claim 拿
    #: 着未过期的许可也发不出新效果。但它们不能进 `action_digest()`：人批准的
    #: 是"把这个文件改成什么"，一旦把 owner 也绑进审批，进程重启一次
    #: （claim_epoch +1）就作废一次人工决定——`approvals approve` 之后再
    #: `run resume` 永远还要再批一次，主流程根本走不完。实测见
    #: `docs/REVIEW-PRA.md` §4.6。
    OWNER_FIELDS = ("request_id", "job_id", "claim_epoch", "owner_instance_id")

    #: 这两个字段是**调用身份**，不是"动作内容"：provider 每轮从 `call_0`
    #: 重新编号是常见的，模型重发同一个动作时它们会变。人**否决**的是那件事
    #: 本身，所以否决缓存键（`subject_digest`）必须把它们去掉；否则模型只要换
    #: 一个 call_id 重发同一个动作，就能绕过人的否决（issue #40 的复现路径）。
    CALL_FIELDS = ("call_id", "attempt_id")

    def action_fields(self) -> dict[str, Any]:
        """人审批时真正看过的那部分。"""
        raw = self.to_canonical_dict()
        for key in self.OWNER_FIELDS:
            raw.pop(key, None)
        return raw

    def digest(self) -> str:
        return jcs_sha256(self.to_canonical_dict())

    def action_digest(self) -> str:
        return jcs_sha256(self.action_fields())

    def subject_fields(self) -> dict[str, Any]:
        """人**否决**时绑住的那部分：动作内容，不含调用编号与执行身份。

        批准绑定的是"哪一次调用"（`action_digest` 里含 call_id / attempt_id），
        因为一次批准只能被消费一次；否决绑定的却是"这件事本身"：否决是终局，
        不能因为模型换了个 call_id 重发就重新变成一张待批门票。
        """
        raw = self.action_fields()
        for key in self.CALL_FIELDS:
            raw.pop(key, None)
        return raw

    def subject_digest(self) -> str:
        return jcs_sha256(self.subject_fields())

    @classmethod
    def of(cls, raw: dict[str, Any]) -> ActionRequest:
        """从库里存的规范字典还原（派发时重算摘要必须逐字节一致）。"""
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        known["resources"] = tuple(known.get("resources") or ())
        return cls(**known)


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
    consumed_at: int | None = None
    expires_at: int = 0
    #: 动作摘要：人批准的是"做什么"，不含 owner / claim_epoch。
    action_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "action_digest": self.action_digest,
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
            "consumed_at": self.consumed_at,
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
    run_id: str = ""
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "subject": self.subject,
            "run_id": self.run_id,
            "task_id": self.task_id,
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
    action_digest   TEXT NOT NULL DEFAULT '',
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
    consumed_at     INTEGER,
    expires_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_digest ON approvals(request_digest);

-- 否决缓存键：approval_id → "人否决时真正看过的那件事"（subject_digest）。
--
-- 为什么单独一张表而不是 approvals 加一列：`Approval` 的字段集合是与
-- `schemas/m2/approval.json` 逐字段绑定的契约（tests/test_schemas.py），
-- 而这里存的是审批的**内部索引**，不是审批对象的一部分。表是后加的，旧库
-- 升级后自然为空；历史行回退到 action_digest / request_digest 比较。
--
-- 索引含 run_id 是因为否决的执行作用域是 Run（见 rejected_for 的说明：
-- 语义键是 (task, subject_digest)，Run 边界只由人/控制面移动）。
CREATE TABLE IF NOT EXISTS approval_subjects (
    approval_id    TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    subject_digest TEXT NOT NULL,
    created_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approval_subjects
    ON approval_subjects(run_id, subject_digest);

CREATE TABLE IF NOT EXISTS grants (
    grant_id       TEXT PRIMARY KEY,
    subject        TEXT NOT NULL,
    run_id         TEXT NOT NULL DEFAULT '',
    task_id        TEXT NOT NULL DEFAULT '',
    request_digest TEXT NOT NULL,
    action         TEXT NOT NULL,
    issuer         TEXT NOT NULL,
    remaining_uses INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grants_digest ON grants(request_digest);
"""


def _ensure_column(
    conn: sqlite3.Connection, table: str, name: str, ddl: str
) -> None:
    """给早期版本的库补列（旧库没有 consumed_at）。"""
    have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


#: 依赖"后来才加的列"的索引。**必须**在 `_ensure_column` 之后建：
#: 旧库的表里没有 `action_digest`，先建索引会直接
#: `no such column: action_digest`，整个 stack 起不来（所有命令都不可用）。
INDEXES_AFTER_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_approvals_action ON approvals(run_id, action_digest);
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
        *,
        task_id: str | None = None,
    ) -> PolicyDecision:
        if action not in SUPPORTED_ACTIONS:
            return PolicyDecision(
                DECISION_DENY, f"不支持的动作：{action}", "c4.unsupported"
            )
        if not resources:
            return PolicyDecision(DECISION_DENY, "动作没有声明资源", "c4.no_resource")

        # 资源种类由字符串自己的 scheme 决定（见 resources.py）。只有真的带
        # `://` 的资源才进记忆分支；裸绝对路径继续走原来的文件判定，逐字节
        # 不变——历史审批与派发摘要不因记忆能力上线而失效。
        if any(
            isinstance(r, str) and not os.path.isabs(r) and "://" in r
            for r in resources
        ):
            return self._classify_memory(
                action,
                resources,
                bindings,
                preconditions or {},
                task_id=task_id,
            )

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
            # "新建"只有在目标确实不存在时才是新建。目标已存在时**不能**靠
            # "批准一下"就放行：`create_report` 本身拒绝覆盖（M1 的
            # `OverwriteRefused`），批准了也执行不了，只会留下一张用不掉的批准。
            # 要覆盖就用 `modify_text`——它是删除类动作，走审批。
            if action == ACTION_CREATE and preconditions.get("target_exists") is True:
                return PolicyDecision(
                    DECISION_DENY,
                    "create_report 不覆盖已有文件；要覆盖请改用 modify_text"
                    "（删除类，需审批）",
                    "c4.create_over_existing",
                )
            return PolicyDecision(DECISION_AUTO, "目录内读/新建", "c4.auto")

        if action == ACTION_EXEC:
            # 执行命令是全系统最高危的动作：永远要审批，且必须先显式开启 exec
            # 策略。没有策略不是"默认白名单"——那等于默认放行任意代码。
            if not preconditions.get("exec_configured"):
                return PolicyDecision(
                    DECISION_DENY,
                    "未配置执行策略：run_command 默认全拒。"
                    "开启 exec 是显式动作（task create --allow-exec-cmd）",
                    "c4.exec_not_configured",
                )
            return PolicyDecision(
                DECISION_APPROVE,
                "执行命令是全系统最高危的动作：可读写任意文件、联网、起子进程",
                "c4.exec",
            )
        if action == ACTION_OVERWRITE:
            return PolicyDecision(
                DECISION_APPROVE, "覆盖已有内容是删除类操作", "c4.overwrite"
            )
        if action == ACTION_DELETE:
            return PolicyDecision(DECISION_APPROVE, "删除需要审批", "c4.delete")
        return PolicyDecision(DECISION_DENY, f"未覆盖的动作：{action}", "c4.unhandled")

    def _classify_memory(
        self,
        action: str,
        resources: list[str],
        bindings: list[WorkspaceBinding],
        preconditions: dict[str, Any],
        *,
        task_id: str | None,
    ) -> PolicyDecision:
        """记忆资源的策略判定。

        记忆不落在目录里，所以不能复用 `_covers` 判定"能不能碰"。但**授权
        不能因此跳过**：作用域必须能被本任务的绑定/身份解释清楚，否则就是
        fail-closed 拒绝。规则：
        - `user`：D2 单用户，恒可访问（读与写都归本地用户）；
        - `workspace`：scope_id 必须是本任务某个绑定的 **canonical 根**，
          不是"在某个子树里"，也不是字符串前缀；
        - `task`：scope_id 必须等于本任务 id；
        - 其它/未知 scope：拒绝。
        """
        if len(resources) != 1:
            return PolicyDecision(
                DECISION_DENY,
                "记忆动作一次只能作用于一个作用域",
                "c4.multi_resource",
            )

        try:
            ref = parse_resource(resources[0])
        except ResourceError as exc:
            # 未登记的 scheme / 畸形记忆 URI：这是资源契约问题，不是"当成
            # 文件继续试"。fail-closed。
            return PolicyDecision(DECISION_DENY, str(exc), "c4.unsupported")
        if ref.kind != KIND_MEMORY:
            return PolicyDecision(
                DECISION_DENY,
                f"记忆工具收到非记忆资源：{resources[0]!r}",
                "dispatch.memory_resource_mismatch",
            )

        scope = ref.memory_scope
        if scope.scope not in MEMORY_SCOPES:
            return PolicyDecision(
                DECISION_DENY,
                f"未知记忆作用域：{scope.scope!r}",
                "c4.memory_scope_denied",
            )

        if scope.scope == "user":
            allowed = True
        elif scope.scope == "workspace":
            # 绑定的根已是 canonical 路径；`overlap_key` 再折叠大小写/Unicode
            # 规范化（Windows、macOS 默认文件系统），但**不做祖先判断**：
            # 作用域必须精确等于某个绑定的根，不能是它的子目录。
            roots = {overlap_key(str(WorkspaceBinding.of(b).root_path)) for b in bindings}
            allowed = overlap_key(scope.scope_id) in roots
        elif scope.scope == "task":
            allowed = task_id is not None and scope.scope_id == task_id
        else:  # pragma: no cover - MEMORY_SCOPES 已经封死
            allowed = False

        if not allowed:
            return PolicyDecision(
                DECISION_DENY,
                f"记忆作用域不在本任务授权范围内：{scope.to_uri()}",
                "c4.memory_scope_denied",
            )

        if action == ACTION_CREATE:
            kind = preconditions.get("memory_kind")
            if not isinstance(kind, str) or kind not in supported_memory_kinds():
                return PolicyDecision(
                    DECISION_DENY,
                    f"不支持的记忆种类：{kind!r}",
                    "c4.memory_kind_unsupported",
                )
            if preconditions.get("memory_exists") is True:
                # 复用既有编号的语义：新建遇到已存在目标时不能"批一下"就
                # 放行，要改用覆盖类动作。
                return PolicyDecision(
                    DECISION_DENY,
                    "目标记忆已存在；要更新请先忘记/确认，不能重复新建候选",
                    "c4.create_over_existing",
                )
            return PolicyDecision(
                DECISION_AUTO,
                "记忆作用域内新建候选，自动放行",
                "c4.memory_scope",
            )

        if action == ACTION_READ:
            return PolicyDecision(
                DECISION_AUTO, "记忆作用域内读取", "c4.memory_scope"
            )

        if action == ACTION_OVERWRITE:
            if preconditions.get("memory_origin") == "user_explicit":
                return PolicyDecision(
                    DECISION_AUTO,
                    "用户本人明示的来源，确认无需再次审批",
                    "c4.memory_scope",
                )
            return PolicyDecision(
                DECISION_APPROVE,
                "激活记忆会改变长期召回行为，属覆盖类操作",
                "c4.overwrite",
            )

        if action == ACTION_DELETE:
            return PolicyDecision(
                DECISION_APPROVE,
                "忘记记忆等同删除，需要审批",
                "c4.delete",
            )

        return PolicyDecision(
            DECISION_DENY,
            f"记忆工具不支持的动作：{action}",
            "c4.unsupported",
        )

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
        _ensure_column(self._conn, "approvals", "consumed_at", "INTEGER")
        _ensure_column(
            self._conn, "approvals", "action_digest", "TEXT NOT NULL DEFAULT ''"
        )
        self._conn.executescript(INDEXES_AFTER_MIGRATION)

    @property
    def transaction(self):
        return self._log.transaction()

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
        action = request.action_digest()
        subject = request.subject_digest()
        now = self._clock()
        with self._log.transaction() as tx:
            existing = self._find(
                ("pending", "approved"), action, request.run_id, by="action_digest"
            )
            if existing is not None:
                return self._row(existing)

            approval_id = f"appr_{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO approvals (approval_id, request_id, request_digest,"
                " action_digest, task_id, run_id, goal_revision, lease_epoch,"
                " action, resources, state, reason, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    approval_id,
                    request.request_id,
                    digest,
                    action,
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
            # 否决缓存键：人一旦拒绝这张审批，同一 Run 内"同一件事"就被否决了。
            # 写在同一个事务里，否则崩溃窗口里会出现"审批被拒但否决查不到"。
            self._conn.execute(
                "INSERT INTO approval_subjects (approval_id, run_id,"
                " subject_digest, created_at) VALUES (?,?,?,?)",
                (approval_id, request.run_id, subject, now),
            )
            tx.append(
                request.run_id,
                APPROVAL_REQUESTED,
                {
                    "approval_id": approval_id,
                    "request_id": request.request_id,
                    "request_digest": digest,
                    "action_digest": action,
                    "action_subject": subject,
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

    def supersede(self, approval_id: str, *, reason: str) -> int:
        """作废**单张**审批。

        恢复同一个 Run 时会出现这种情况：绝大多数待批请求还是对的，只有一张
        因为目标文件变了、目标 revision 变了或 claim 被接管而绑定了旧摘要。
        按 Run 批量作废会把仍然有效的那些一起打死，用户得重新批一堆本来没问题
        的动作。
        """
        return self._bulk_close_ids(
            [approval_id], "superseded", APPROVAL_SUPERSEDED, reason
        )

    # ---- 兑现 ----

    def consume(
        self, approval_id: str, *, action_digest: str, request_digest: str
    ) -> Approval:
        """把一次批准兑现成"已消费"。**必须在派发事务里调用**。

        条件 UPDATE 是"一次批准只能执行一次"的唯一保证：两个并发派发里只有
        一个能把 approved → consumed 改成功，另一个 rowcount=0 直接报错。
        先查后改（check-then-act）不行——两者之间足够另一个进程插进来。
        """
        now = self._clock()
        with self._log.transaction() as tx:
            cur = self._conn.execute(
                "UPDATE approvals SET state='consumed', consumed_at=?"
                " WHERE approval_id=? AND state='approved' AND expires_at > ?"
                "   AND (action_digest=? OR (action_digest='' AND request_digest=?))",
                (now, approval_id, now, action_digest, request_digest),
            )
            if cur.rowcount == 0:
                raise ApprovalError(
                    f"审批不可消费：{approval_id}（未批准、已消费、已撤销或已过期）"
                )
            current = self.get(approval_id)
            if current is None:  # pragma: no cover - 刚更新过
                raise ApprovalError(f"审批不存在：{approval_id}")
            tx.append(
                current.run_id,
                APPROVAL_CONSUMED,
                {
                    "approval_id": approval_id,
                    "action_digest": action_digest,
                    "request_digest": request_digest,
                    "decided_by": current.decided_by,
                },
                task_id=current.task_id,
                actor_kind="core",
            )
        return current

    def revoke_consumed(
        self, approval_id: str, *, actor: HumanIdentity, reason: str
    ) -> Approval:
        """撤销一次"已消费但尚未开始执行"的批准。

        这是"撤销先拿到执行守卫，就拒绝旧操作"的落点。执行一旦越过
        `execution.started`，这次调用就是 in-flight，撤销不再承诺回滚。
        """
        if not isinstance(actor, HumanIdentity):
            raise ApprovalError("撤销人必须是 HumanIdentity")
        with self._log.transaction() as tx:
            cur = self._conn.execute(
                "UPDATE approvals SET state='revoked'"
                " WHERE approval_id=? AND state='consumed'",
                (approval_id,),
            )
            if cur.rowcount == 0:
                raise ApprovalError(
                    f"审批不在'已消费未执行'状态，不能按此路径撤销：{approval_id}"
                )
            current = self.get(approval_id)
            if current is None:  # pragma: no cover
                raise ApprovalError(f"审批不存在：{approval_id}")
            tx.append(
                current.run_id,
                APPROVAL_REVOKED,
                {
                    "approval_id": approval_id,
                    "decided_by": actor.to_dict(),
                    "reason": reason,
                    "stage": "consumed",
                },
                task_id=current.task_id,
                actor_kind="human",
                actor_id=actor.actor,
            )
        return current

    # ---- 查询 ----

    def find(self, request_digest: str, *, run_id: str) -> Approval | None:
        """同一**完整请求摘要**上还能用的那张审批（pending 或 approved）。

        只在"请求本身一字未变"时命中；进程重启换了 claim_epoch 的请求用
        `find_for`。
        """
        row = self._find(("pending", "approved"), request_digest, run_id)
        return self._row(row) if row is not None else None

    def find_for(self, request: ActionRequest) -> Approval | None:
        """这张动作还能用的审批（pending 或 approved），**与 owner/epoch 无关**。

        回退一次完整摘要：旧库里早先写下的审批只有 `request_digest`
        （`action_digest` 为空），只有请求一字未变（同一 owner、同一 epoch）
        才认它——不能让旧记录变成"随便哪个新 epoch 都能用"。
        """
        action = request.action_digest()
        row = self._find(("pending", "approved"), action, request.run_id,
                         by="action_digest")
        if row is not None:
            return self._row(row)
        row = self._find(("pending", "approved"), request.digest(), request.run_id)
        if row is None:
            return None
        found = self._row(row)
        return found if found.action_digest in ("", action) else None

    def rejected_for(self, request: ActionRequest) -> Approval | None:
        """这个动作在**本次 Run 内**是否已经被人否决（issue #40 的否决缓存）。

        命中 = "这个动作被否决了"：派发要直接拒绝，**不得**再发起一张新审批。
        否则 `approvals reject` → `run resume` 会立刻再建一张待批，Run 又回到
        `waiting_approval`，用户只能反复点拒绝，Run 永远收不了尾。

        比较的是 `subject_digest`（动作内容：参数/资源/目标/preconditions/
        goal_revision/lease_epoch/工具版本），**不含** call_id / attempt_id /
        owner / claim_epoch：

        - 换参数、换目标 → subject 变 → 旧否决失效，人可以改主意重新批准；
        - 换 call_id（模型重发同一个动作）、换进程（claim_epoch 推进）→
          subject 不变 → 否决继续有效，不能靠重发把否决变成新的待批项。

        旧库升级前的审批行没有 subject 记录（表是后加的），按 `find_for` /
        `consume` 同一套回退口径退到 `action_digest`，再退到 `request_digest`。

        ## 作用域：Run 是边界，语义键是 (task, subject_digest)（#53 静态疑点 1）

        SQL 里按 `run_id` 过滤，意思是"否决在这个 Run 内是终局"。那么"同一 Task
        换个新 Run 会不会忘掉否决"就是唯一要紧的问题。答案是：**不会构成绕过**，
        因为能启动 Run 的只有人/控制面——

        - CLI：`run start` / `run --task`;
        - 控制面 RPC：`control.ControlPlane` 的 `run.start`（Host/UI 经带 token
          的本地 IPC 调用）；
        - `Dispatcher.open_control_run`：undo / recover 由 CLI 发起的控制 Run。

        模型的能力面只有 `tool_manifest()` 里的工具（`SPECS` 的 `exposed`
        投影），里面没有"启动 Run"这个动作，`tools.py` 连 `TaskStore` 都只在
        `TYPE_CHECKING` 下出现（运行时拿不到）。三层钉子见
        `tests/test_veto_scope.py`。所以"换个 Run 再问一次"必然是人主动发起的
        新执行——那是人的决定，不是模型把否决变回了待批门票。

        前提如果变了（出现模型可达的建 Run 通道），这里必须改成按 task 查询
        （`approval_subjects` 没有 task_id 列，需要 join `approvals.task_id`
        或补列）。这个前提不是"约定俗成"，是有测试钉住的能力面事实。
        """
        row = self._conn.execute(
            "SELECT a.* FROM approvals a"
            " LEFT JOIN approval_subjects s ON s.approval_id = a.approval_id"
            " WHERE a.state = 'rejected' AND a.run_id = ?"
            "   AND (s.subject_digest = ?"
            "        OR (s.approval_id IS NULL AND a.action_digest = ?)"
            "        OR (s.approval_id IS NULL AND a.action_digest = ''"
            "            AND a.request_digest = ?))"
            " ORDER BY a.created_at LIMIT 1",
            (
                request.run_id,
                request.subject_digest(),
                request.action_digest(),
                request.digest(),
            ),
        ).fetchone()
        return self._row(row) if row is not None else None

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

    def expire(self, approval_id: str) -> bool:
        """把某一张已过期的 pending/approved 标成 expired。

        `expire_due` 只扫 pending；但"已批准、还没派发时审批自己过期了"也是常态
        （TTL 10 分钟，人去喝杯咖啡就过了）。这种必须能单独作废，否则用户会对着
        一个永远不能用的批准反复重试。
        """
        now = self._clock()
        with self._log.transaction() as tx:
            current = self.get(approval_id)
            if current is None:
                return False
            if current.state not in ("pending", "approved") or current.expires_at > now:
                return False
            cur = self._conn.execute(
                "UPDATE approvals SET state='expired' WHERE approval_id=?"
                " AND state IN ('pending','approved')",
                (approval_id,),
            )
            if cur.rowcount == 0:  # pragma: no cover - 事务内不会变
                return False
            tx.append(
                current.run_id,
                APPROVAL_EXPIRED,
                {"approval_id": approval_id, "reason": "ttl"},
                task_id=current.task_id,
                actor_kind="core",
            )
        return True

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

    def _find(
        self,
        states: tuple[str, ...],
        digest: str,
        run_id: str,
        *,
        by: str = "request_digest",
    ):
        """找可以复用的同摘要审批。

        已过期的 pending 不算——否则过了 TTL 之后同一个动作再也发不出新审批，
        用户只能手动跑一次 expire_due 才能重试。
        """
        # `by` 只允许这两个列名：它是内插进 SQL 的，不接受任意字符串。
        if by not in ("request_digest", "action_digest"):
            raise TianshuError(f"不支持的摘要列：{by}")
        marks = ",".join("?" * len(states))
        return self._conn.execute(
            f"SELECT * FROM approvals WHERE state IN ({marks})"
            " AND (state != 'pending' OR expires_at > ?)"
            f" AND {by}=? AND run_id=? ORDER BY created_at LIMIT 1",
            (*states, self._clock(), digest, run_id),
        ).fetchone()

    @staticmethod
    def _row(row: sqlite3.Row) -> Approval:
        return Approval(
            approval_id=row["approval_id"],
            request_id=row["request_id"],
            request_digest=row["request_digest"],
            action_digest=(
                row["action_digest"] if "action_digest" in row.keys() else ""
            ),
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
            consumed_at=(
                row["consumed_at"]
                if "consumed_at" in row.keys()
                else None
            ),
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
                "INSERT INTO grants (grant_id, subject, run_id, task_id,"
                " request_digest, action, issuer, remaining_uses, created_at,"
                " expires_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
                (
                    grant_id,
                    subject,
                    request.run_id,
                    request.task_id,
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
        keys = row.keys()
        return Grant(
            grant_id=row["grant_id"],
            subject=row["subject"],
            run_id=row["run_id"] if "run_id" in keys else "",
            task_id=row["task_id"] if "task_id" in keys else "",
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
                grant.run_id or grant.subject,
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
    "ACTION_EXEC",
    "ACTION_OVERWRITE",
    "ACTION_READ",
    "APPROVAL_ACTIONS",
    "APPROVAL_CONSUMED",
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
    "supported_memory_kinds",
]
