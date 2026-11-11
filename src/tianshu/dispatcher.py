"""唯一派发入口（M2 · PR B · Task 4）。

派发回答的是："这次调用**现在**可以真的改文件吗，凭什么？"

它把四件事放进**同一个 SQLite 提交**：

1. 当前状态复核（Run 没被 fence、revision/epoch/租约/claim 都对得上）；
2. 审批的**唯一消费**（条件 UPDATE，两个并发派发只有一个能赢）；
3. grant 的签发与消费（短命票据：它说的是"此刻可以写"，而"此刻"会过期）；
4. `tool.intent` 的提交（写前证据，崩溃恢复靠它判断"有没有进入写入阶段"）。

四件事必须同生同死。分开提交就会出现两种坏状态：批准被用掉但没执行（人要再批
一次，账上却是"用过了"），或者执行了两次（两份 effect，只有一份被记录）。

然后是第二个阶段：**执行守卫内**再核一遍，记 `execution.started`，才真正打开文件。
为什么核两遍？因为派发事务提交之后到这个文件被打开之间还有一个窗口，另一个进程
可以撤销审批、接管 claim、换掉目标。"先撤销就赢"还是"先开始就赢"由执行守卫裁决：

- 撤销先拿到守卫 → 旧操作作废（`approval.revoked`，派发记录标 revoked，文件不动）；
- 执行先拿到守卫 → `execution.started` 已落库，这次调用是 in-flight，
  撤销不再承诺回滚（也拦不住已经发生的写）。

这个顺序是刻意的：宁可让用户看到"已经在执行了，撤销没生效"，也不假装撤销成功而
文件其实被改了。

只读动作（list_files / read_text）不进派发：它们没有可授权的副作用，被拦的是越界
读取，那由 `Workspace.resolve` 在执行侧完成。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crashpoint import failpoint
from .errors import (
    OverwriteRefused,
    RunFenced,
    TianshuError,
    ToolArgumentError,
    ToolNotAllowed,
    WorkspaceBlocked,
    WorkspaceViolation,
)
from .events import EventLog, canonical_bytes, sha256_hex
from .exec import LOCAL_EXEC_CAPABILITIES
from .execpolicy import (
    PolicyDenied,
    PolicyUnsupported,
    check_capabilities,
    check_spawn,
    filtered_env,
)
from .guard import ensure_not_blocked
from .jobs import JobClaim, JobLost, JobStore
from .leases import (
    GUARD_TIMEOUT_MS,
    ExecutionGuard,
    GuardTimeout,
    LeaseConflict,
    LeaseLost,
    LeaseStore,
    ReconcileFn,
    execution_guard,
)
from .memory_exec import (
    MEMORY_ALL_TOOL_IDS,
    MEMORY_TOOL_IDS,
    MemoryExecutor,
    memory_scope_allowed,
)
from .permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EXEC,
    ACTION_OVERWRITE,
    APPROVAL_ACTIONS,
    POLICY_DENIED,
    ActionRequest,
    Approval,
    ApprovalError,
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyDecision,
    PolicyEngine,
    supported_memory_kinds,
)
from .receipt import build_exec_receipt, build_memory_receipt, build_receipt
from .resources import (
    KIND_FILE,
    KIND_MEMORY,
    MemoryScope,
    ResourceError,
    ResourceRef,
    build_memory_scope,
    is_memory_resource,
    parse_memory_uri,
    parse_resource,
)
from .rules import check_rule_id
from .sandbox import FileBackend, current_backend
from .snapshot import SnapshotStore
from .tasks import RUN_RUNNING, ExecutionPermit, Run, Task, TaskStore
from .tools import (
    SPECS,
    AdmittedCall,
    DispatchRecord,
    FileExecutor,
    ToolResult,
    check_request_preconditions,
    observe_state,
)
from .workspace import (
    ResourceIdentity,
    Workspace,
    WorkspaceBinding,
    is_ancestor_or_self,
    overlap_key,
)

# call_id 被另一个请求占用（同一 run 里换个参数复用同一个 id）
D_DISPATCH_CALL_ID_REUSED = "dispatch.call_id_reused"

#: 同一 Run 内、同一动作内容上人已经否决过：直接拒绝，**不再发起新审批**。
#: 这是 issue #40 的落点——否决是终局，不是"再问一次"。
APPROVAL_REJECTED = "c4.approval_rejected"
#: 给模型看的话必须同时给出下一步："要做就改参数或改目标，然后重新发起"，
#: 否则模型只会原样重试同一个动作。
APPROVAL_REJECTED_REASON = (
    "你之前拒绝了这个操作；要做就改参数或改目标，然后重新发起"
)

DISPATCH_REQUESTED = "dispatch.requested"
DISPATCH_REJECTED = "dispatch.rejected"
DISPATCH_ADMITTED = "dispatch.admitted"
DISPATCH_REVOKED = "dispatch.revoked"

#: 审计表一次性迁移的持久标记（见 `_migrate_redacted_args`）。
_BACKFILL_MARKER = "args_redacted_backfilled"

STATE_WAITING = "waiting"
STATE_ADMITTED = "admitted"
STATE_REJECTED = "rejected"
STATE_DONE = "done"

# 派发记录的状态：intent 之后还没开始执行，正是"可以被撤销"的窗口
DISPATCH_INTENT = "intent"
DISPATCH_EXECUTING = "executing"
DISPATCH_FINISHED = "done"
DISPATCH_ROW_REVOKED = "revoked"
DISPATCH_ROW_REJECTED = "rejected"
DISPATCH_ERROR = "error"

KIND_WAITING = "waiting"
KIND_ADMITTED = "admitted"
KIND_REJECTED = "rejected"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_requests (
    request_id     TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    payload        TEXT NOT NULL,
    args_payload   TEXT NOT NULL,
    -- 请求进入**终态**后 args_payload 已脱敏（只留正文摘要）：审计表不该永久
    -- 保存记忆正文原文。执行期要用完整参数，所以只能在终态之后做。
    args_redacted  INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    rule_id        TEXT NOT NULL DEFAULT '',
    approval_id    TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_requests_run
    ON dispatch_requests(run_id, state);

CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    call_id           TEXT NOT NULL,
    request_id        TEXT NOT NULL,
    request_digest    TEXT NOT NULL,
    tool_id           TEXT NOT NULL,
    action            TEXT NOT NULL,
    resources         TEXT NOT NULL,
    target            TEXT NOT NULL,
    rel_path          TEXT NOT NULL,
    workspace_root    TEXT NOT NULL,
    resource_key      TEXT NOT NULL,
    approval_id       TEXT,
    grant_id          TEXT NOT NULL DEFAULT '',
    claim_epoch       INTEGER NOT NULL,
    owner_instance_id TEXT NOT NULL,
    attempt_id        INTEGER NOT NULL DEFAULT 1,
    intent_seq        INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- call_id 是**本次 Run 内的逻辑身份**。唯一约束加在 (run_id, call_id) 上：
-- provider 给的短 ID 在不同 run 之间会重名，跨 run 唯一会误伤别的运行。
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatches_call
    ON dispatches(run_id, call_id);

-- 一次性数据迁移的**持久标记**：只有迁移事务成功提交后才置位。
-- 用标记而不是"看列在不在"：`ALTER TABLE` 会先落盘，补扫中途被 kill 时
-- 列已存在，靠列存在性判断会让隐私补扫永远不再重试。
CREATE TABLE IF NOT EXISTS dispatch_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DispatchError(TianshuError):
    """派发层的编程/状态错误（请求不存在、重复派发、类型不对）。"""


class DispatchDenied(TianshuError):
    """这次派发不被允许。带一个可审计的规则编号。"""

    def __init__(self, reason: str, rule_id: str = "dispatch.denied") -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_id = rule_id


@dataclass(frozen=True)
class DispatchDecision:
    kind: str  # waiting | admitted | rejected
    request_id: str
    request_digest: str
    approval_id: str | None = None
    reason: str = ""
    rule_id: str = ""

    @property
    def is_waiting(self) -> bool:
        return self.kind == KIND_WAITING

    @property
    def is_admitted(self) -> bool:
        return self.kind == KIND_ADMITTED

    @property
    def is_rejected(self) -> bool:
        return self.kind == KIND_REJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "approval_id": self.approval_id,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True)
class StoredRequest:
    request: ActionRequest
    args: dict[str, Any]
    state: str
    reason: str
    rule_id: str
    approval_id: str | None


@dataclass(frozen=True)
class ControlRun:
    """undo / recover 这类"用户直接发起的写"的控制 Run。

    它有自己的 Task、Run 和 JobClaim，不借用被撤销那个历史 Run 的
    已经失效的身份；同时把"操作的是哪个历史 Run"写在目标里。

    `memory_scopes` 是控制面显式声明的记忆作用域（`memory://...` URI）：
    有了它，CLI 能替**旧 task** 的记忆做确认 / 遗忘，而不必因此拿到那个 task
    的 workspace 绑定。它只对记忆动作生效，不是文件权限。
    """

    task: Task
    run: Run
    claim: JobClaim
    lease_paths: tuple[str, ...]
    memory_scopes: tuple[str, ...] = ()


#: 有**执行器**的资源种类。其余种类（network / mcp / subtask）解析得出来、
#: 也能做许可判定，但没有执行器——它们的执行器属 G2。
EXECUTABLE_KINDS: frozenset[str] = frozenset({KIND_FILE, KIND_MEMORY})


def resolve_scheme_resource(raw: str) -> ResourceRef:
    """解析 `resources` 里一个**带 scheme** 的资源，并按"有没有执行器"拒绝。

    为什么单独抽出来：这是"资源种类"这条契约在执行侧的唯一落点，测试要能
    直接打它，而不是为了测一句拒绝去搭一整套 dispatcher 依赖（那样测出来的
    其实还是"环境搭对了没有"）。`_resolve_target()` 与本函数同源，不另写一份
    判定。
    """
    try:
        ref = parse_resource(raw)
    except ResourceError as exc:
        raise DispatchDenied(f"资源引用不合法：{exc}", "c4.unsupported") from exc
    if ref.kind not in EXECUTABLE_KINDS:
        raise DispatchDenied(
            f"{ref.kind} 资源的执行器尚未实现（G2），拒绝派发：{ref.uri}",
            "dispatch.resource_kind_unsupported",
        )
    return ref


@dataclass(frozen=True)
class _Authority:
    run: Run
    task: Task
    bindings: list[WorkspaceBinding]
    resources: list[ResourceIdentity]
    lease_paths: list[str]


@dataclass(frozen=True)
class _FileTarget:
    path: Path
    rel_path: str
    workspace_root: str
    identity: ResourceIdentity


@dataclass(frozen=True)
class _MemoryTarget:
    """记忆目标：只有作用域，没有文件路径。

    `rel_path` / `workspace_root` 在派发表里是历史列，记忆行填 scope_id 与空
    相对路径；任何文件执行器都不会拿到这个对象。
    """

    scope: MemoryScope
    uri: str
    kind: str = KIND_MEMORY


_Target = _FileTarget | _MemoryTarget


def _now() -> int:
    return int(time.time() * 1000)


def precondition_rule_id(
    action: str, target: Path, *, io: FileBackend | None = None
) -> str:
    """把"前置条件不成立"翻译成稳定的 rule id。

    用户看到这个 id 要知道下一步该干什么，所以不能一律写
    `dispatch.stale_precondition`：

    - `dispatch.target_is_directory`：换成一个文件目标（覆盖/删除不作用于目录）；
    - `dispatch.create_over_existing`：换工具（要覆盖请用 modify_text）；
    - `dispatch.stale_precondition`：目标在被批准后被改过，需要重新发起。
    """
    impl = io if io is not None else current_backend()
    if impl.is_dir(target):
        # 覆盖/删除作用于目录没有 preimage 边界；新建也不能建在目录上。
        return "dispatch.target_is_directory"
    if action == ACTION_CREATE and impl.exists(target):
        return "dispatch.create_over_existing"
    return "dispatch.stale_precondition"


def _audit_args(request: ActionRequest, args: dict[str, Any]) -> dict[str, Any]:
    """给**被拒绝**的派发请求脱敏：记忆正文只留摘要。

    能脱敏的只有**永远不会执行**的行。被采纳的行**不能**脱敏——`args_payload`
    会被读回来做 `args_hash` 绑定校验，执行器也从这张表取参数（详见
    `_intent_args` 的注释）；落库前统一脱敏会直接把派发打红。所以本函数只挂在
    `_reject()` 上。

    为什么拒绝的行也要处理：`dispatch_requests` 没有擦除路径，`memory.forget`
    也不碰它，留一份记忆正文原文就是永久副本。换成 `content_sha256 +
    content_bytes` 后，审计仍能核对"要写的是同一段正文"（哈希可比、长度可比），
    原文不再落地；`content` 之外的字段（kind / scope / 记忆 id / 写前 revision）
    照常留。

    **另一半**：被采纳的请求在**进入终态之后**由 `_set_request_state()` 调
    `_redact_terminal_args()` 补做同一套脱敏（执行期要用完整参数，所以不能更早）。
    两个半场合起来，审计表里不再永久保存记忆正文原文（F10 §13 #26 已收口）。
    """
    if "content" not in args:
        return args
    if not any(is_memory_resource(resource) for resource in request.resources):
        return args
    content = args.get("content")
    text = content if isinstance(content, str) else ""
    raw = text.encode("utf-8")
    redacted = {key: value for key, value in args.items() if key != "content"}
    redacted["content_sha256"] = sha256_hex(raw)
    redacted["content_bytes"] = len(raw)
    redacted["content_redacted"] = True
    return redacted


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> bool:
    """老库补列（幂等）：`CREATE TABLE IF NOT EXISTS` 不会给已有表加列。

    返回"这次真的加了列没有"。调用方据此决定要不要做**一次性的历史数据补扫**：
    每次打开都全表解析历史行，在大库上是纯浪费，还会带来"补扫把刚刚重新发起
    的请求又脱敏回去"的竞态。
    """
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return True


class Dispatcher:
    """唯一判定"能不能执行"，并登记执行的地方。"""

    def __init__(
        self,
        log: EventLog,
        tasks: TaskStore,
        leases: LeaseStore,
        policy: PolicyEngine,
        approvals: ApprovalStore,
        executor: FileExecutor | None = None,
        *,
        grants: GrantStore | None = None,
        claims: JobStore | None = None,
        snapshots: SnapshotStore | None = None,
        data_root: Path | None = None,
        memory: object | None = None,
        clock=_now,
        guard_timeout_ms: int = GUARD_TIMEOUT_MS,
    ) -> None:
        if log.in_transaction:
            raise TianshuError("Dispatcher 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # 列不存在时**先**作废持久标记、再加列：`ALTER TABLE` 是 autocommit
        # 落盘的，若先 ALTER 再删标记，两条语句之间崩溃就会留下"列在、标记也
        # 在"的状态，下次打开看到标记直接跳过 → 旧终态正文永久漏脱敏。
        # 反过来先删标记：哪怕在删完之后、加列之前崩溃，下次打开列仍不存在、
        # 标记仍缺失，两条语句会再跑一遍（幂等）。正常升级路径（标记与列同批
        # 维护）里列本来就在，这段不执行。
        columns = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(dispatch_requests)"
            ).fetchall()
        }
        if "args_redacted" not in columns:
            # 反向边界：标记在、列却不在（迁移工具或人手把列删了）⇒ 旧终态行的
            # 正文又回到"未脱敏"，必须作废标记强制重扫，不然永久漏。
            self._conn.execute(
                "DELETE FROM dispatch_meta WHERE key = ?", (_BACKFILL_MARKER,)
            )
        _ensure_column(
            self._conn, "dispatch_requests", "args_redacted",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # 用持久标记判"补扫做过没有"：中途被 kill / 抛错时标记不会置位，
        # 下次打开会重试（否则列已存在，靠列存在性判断就永远不补了）。
        self._migrate_redacted_args()
        self._tasks = tasks
        self._leases = leases
        self._policy = policy
        self._approvals = approvals
        self._grants = grants or GrantStore(log, clock=clock)
        self._claims = claims or tasks.claims
        if self._claims is None:
            raise TianshuError(
                "Dispatcher 需要 JobStore：派发必须绑定当前 owner 的 claim，"
                "不能用裸 run_id 发起写入"
            )
        self._snapshots = snapshots
        self._data_root = Path(data_root) if data_root is not None else tasks.data_root
        self._clock = clock
        self._guard_timeout_ms = guard_timeout_ms
        # 记忆投影必须与事件日志共用同一个 SQLite 连接：投影更新要放进
        # `_commit_result()` 的同一个事务，另开连接就等于另开事实源。
        self._memory = memory
        # `MemoryProjection` 构造处的检查只保证"它自己和一个日志同连接"，
        # 管不了派发方传进来的是**哪个**日志的投影。那样 `_commit_result()`
        # 会在 log 的事务里写事件、却在另一条连接上独立提交投影——一边回滚
        # 一边留下，两份事实源静默分叉，而且没有任何裁决者能判定哪份对。
        # 约束必须在边界上强制，不能靠"装配代码记得传对"。
        if memory is not None and getattr(memory, "conn", None) is not self._conn:
            raise TianshuError(
                "Dispatcher 收到的记忆投影不属于同一个 SQLite 连接："
                "投影必须和事件在同一个事务里落库，否则事件与投影会分叉"
            )
        self._memory_executor: MemoryExecutor | None = None
        self._executor = executor or FileExecutor(
            tasks=tasks,
            leases=leases,
            grants=self._grants,
            approvals=approvals,
            claims=self._claims,
            dispatch_lookup=self.dispatch_record,
            dispatch_mark=self.mark_dispatch,
            clock=clock,
            log=log,
            snapshots=snapshots,
        )

    # ---- 只读查询 ----

    @property
    def log(self) -> EventLog:
        return self._log

    @property
    def claims(self) -> JobStore:
        return self._claims

    def _memory_projection(self) -> object:
        """拿到与事件日志同库的记忆投影。

        延迟构造：没有记忆请求的运行不需要加载记忆模块，也不改变既有启动路径。
        `memory.py` 由队友实现；这里只调用锚定签名，不复制一份实现。
        """
        if self._memory is None:
            try:
                from .memory import MemoryProjection
            except ImportError as exc:  # pragma: no cover - 集成期缺模块
                raise TianshuError(
                    "收到 memory 资源，但记忆投影不可用（缺少 tianshu.memory）"
                ) from exc
            # **不**显式传 blob_dir：正文目录由 `MemoryProjection` 自己的规则
            # 决定（DB 旁的 `memory_blobs/`）。正文与事件 payload 的保留策略
            # 不同（前者将来要能按 F10 §10 物理擦除，后者必须留存供 replay），
            # 共用 `log.blob_dir` 会让按策略清理误伤日志材料。所有装配点都
            # 不传，正文目录才有唯一来源。
            self._memory = MemoryProjection(
                self._conn,
                log=self._log,
                clock=self._clock,
            )
            self._memory.ensure_schema()
        elif not getattr(self, "_memory_ready", False):
            self._memory.ensure_schema()
        self._memory_ready = True
        if self._memory_executor is None:
            self._memory_executor = MemoryExecutor(
                tasks=self._tasks,
                approvals=self._approvals,
                grants=self._grants,
                claims=self._claims,
                projection=self._memory,
                dispatch_lookup=self.dispatch_record,
                dispatch_mark=self.mark_dispatch,
                clock=self._clock,
                log=self._log,
            )
        return self._memory

    def _memory_executor_for(self) -> MemoryExecutor:
        self._memory_projection()
        assert self._memory_executor is not None
        return self._memory_executor

    def dispatch_record(self, run_id: str, call_id: str) -> DispatchRecord | None:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE run_id=? AND call_id=?",
            (run_id, call_id),
        ).fetchone()
        return _row_to_dispatch(row) if row is not None else None

    def get_request(self, request_id: str) -> StoredRequest | None:
        row = self._conn.execute(
            "SELECT * FROM dispatch_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        return StoredRequest(
            request=ActionRequest.of(json.loads(row["payload"])),
            args=json.loads(row["args_payload"]),
            state=row["state"],
            reason=row["reason"],
            rule_id=row["rule_id"],
            approval_id=row["approval_id"],
        )

    def request_ids_for_run(self, run_id: str) -> list[str]:
        """这个 Run 签发过的请求号（按时间顺序）。

        恢复时要用它回答一个问题："同一个动作是不是已经有一张请求/审批在库里
        了？" 只靠游标不够——崩溃窗口里游标可能还没记下请求号。
        """
        rows = self._conn.execute(
            "SELECT request_id FROM dispatch_requests WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [r["request_id"] for r in rows]

    def waiting(self, *, run_id: str | None = None) -> list[StoredRequest]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_requests WHERE state=? ORDER BY created_at",
                (STATE_WAITING,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_requests"
                " WHERE state=? AND run_id=? ORDER BY created_at",
                (STATE_WAITING, run_id),
            ).fetchall()
        out = []
        for row in rows:
            stored = self.get_request(row["request_id"])
            if stored is not None:
                out.append(stored)
        return out

    # ---- 阶段一：判定 ----

    def prepare(
        self, request: ActionRequest, claim: JobClaim, *, args: dict[str, Any]
    ) -> DispatchDecision:
        """判定这次动作该怎么走：直接放行、等人工批准、还是拒绝。

        这里**不写文件**，也不消费审批。可以反复调用（幂等）：同一个摘要上已经
        有一张 pending/approved 的审批就复用它，不会给用户刷出一堆待批项。
        """
        self._typecheck(request, claim, args)
        if sha256_hex(canonical_bytes(args)) != request.args_hash:
            raise DispatchError("args 与 request.args_hash 不符（请求被改过）")

        try:
            auth = self._authority(request, claim)
            target = self._resolve_target(request, auth)
            if isinstance(target, _MemoryTarget):
                # 记忆前置条件是 status/revision；文件分支保持原样，连调用
                # 顺序都不改（file 的 digest/approval 行为不能因记忆上线变化）。
                self._check_memory_preconditions(request, target, args)
            else:
                if request.action == ACTION_EXEC:
                    self._precheck_exec(request, args, target.path)
                ok, reason = check_request_preconditions(
                    request.action, target.path, request.preconditions or {}
                )
                if not ok:
                    raise DispatchDenied(
                        reason, precondition_rule_id(request.action, target.path)
                    )
        except DispatchDenied as exc:
            return self._reject(request, args, exc.reason, exc.rule_id, event=DISPATCH_REJECTED)

        policy_preconditions = self._policy_preconditions(request, target, args)
        decision = self._policy.classify(
            request.action,
            list(request.resources),
            auth.bindings,
            policy_preconditions,
            task_id=request.task_id,
        )
        # 放行/需审批的判定结果也会写进 payload.rule_id（dispatch.requested），
        # 所以和拒绝路径一样要过同一张表；否则"为什么放行"仍然没有契约。
        check_rule_id(decision.rule_id, where="prepare:classify")

        if decision.is_denied:
            return self._reject(
                request, args, decision.reason, decision.rule_id, event=POLICY_DENIED
            )

        if decision.is_auto:
            self._store_request(request, args, STATE_ADMITTED, decision.reason, decision.rule_id)
            self._append(
                request,
                DISPATCH_REQUESTED,
                {
                    "request_id": request.request_id,
                    "request_digest": request.digest(),
                    "action_digest": request.action_digest(),
                    "action": request.action,
                    "resources": list(request.resources),
                    "decision": "auto",
                    "rule_id": decision.rule_id,
                },
            )
            return DispatchDecision(
                KIND_ADMITTED,
                request.request_id,
                request.digest(),
                reason=decision.reason,
                rule_id=decision.rule_id,
            )

        # 人已经否决过这个动作？否决是终局——**在发起审批之前**就拒绝。
        #
        # 这里是 issue #40 的修复点：以前 `find_for()` 只找 pending/approved，
        # 被拒的审批等于不存在，于是 resume 会再建一张新审批，Run 又回到
        # waiting_approval，用户被迫反复点拒绝而 Run 永远收不了尾。
        # `rejected_for()` 认的是动作内容（不含 call_id / owner / claim_epoch）：
        # 换参数/换目标就查不到（允许重新发起），换调用编号仍然命中。
        if self._approvals.rejected_for(request) is not None:
            return self._reject(
                request,
                args,
                APPROVAL_REJECTED_REASON,
                APPROVAL_REJECTED,
                event=DISPATCH_REJECTED,
            )

        approval = self._approvals.find_for(request)
        if (
            approval is not None
            and approval.state == "approved"
            and approval.expires_at <= self._clock()
        ):
            # 已批准但审批本身过期了：作废它，重新发起，别让用户对着一个不能用的
            # 批准反复重试。
            self._approvals.expire(approval.approval_id)
            approval = None

        if approval is None:
            approval = self._approvals.request(request, decision)

        state = STATE_WAITING if approval.state == "pending" else STATE_ADMITTED
        self._store_request(
            request,
            args,
            state,
            decision.reason,
            decision.rule_id,
            approval_id=approval.approval_id,
        )
        self._append(
            request,
            DISPATCH_REQUESTED,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "action": request.action,
                "resources": list(request.resources),
                "decision": decision.kind,
                "rule_id": decision.rule_id,
                "approval_id": approval.approval_id,
                "approval_state": approval.state,
            },
        )
        if state == STATE_WAITING:
            return DispatchDecision(
                KIND_WAITING,
                request.request_id,
                request.digest(),
                approval_id=approval.approval_id,
                reason=decision.reason,
                rule_id=decision.rule_id,
            )
        return DispatchDecision(
            KIND_ADMITTED,
            request.request_id,
            request.digest(),
            approval_id=approval.approval_id,
            reason=decision.reason,
            rule_id=decision.rule_id,
        )

    # ---- 阶段二：派发 ----

    def dispatch(self, request_id: str, claim: JobClaim) -> ToolResult:
        """消费审批、登记 intent、在守卫内执行。

        返回的 `ToolResult` 里，`effect_state=none_applied` 表示"一个字节都没动"；
        `unknown` 表示"可能动了，去 recover"。任何不是这两种的结论都必须有证据。
        """
        if not isinstance(claim, JobClaim):
            raise DispatchError("必须传 JobClaim；不能用裸 run_id 派发")

        stored = self.get_request(request_id)
        if stored is None:
            raise DispatchError(f"派发请求不存在：{request_id}")
        request, args = stored.request, stored.args

        if stored.state == STATE_REJECTED:
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": stored.reason,
                    "rule_id": stored.rule_id,
                    "request_id": request_id,
                },
            )

        # ---- 阶段一：同一事务里核验 + 消费审批 + 签发票据 + 登记 intent ----
        try:
            with self._log.transaction() as tx:
                auth = self._authority(request, claim)
                target = self._resolve_target(request, auth)
                if not isinstance(target, _MemoryTarget) and request.action == ACTION_EXEC:
                    self._precheck_exec(request, args, target.path)

                existing = self.dispatch_record(request.run_id, request.call_id)
                if existing is not None:
                    # 同一个 call_id 换个参数再来一次：唯一索引会挡住双写，
                    # 但那是 `IntegrityError` 从 INSERT 里炸出来，调用方只能
                    # 看到"主循环异常"。这里显式区分两种拒绝原因。
                    if existing.request_digest != request.digest():
                        raise DispatchDenied(
                            f"call_id 已被另一次请求占用：{request.call_id}"
                            f"（已有 state={existing.state}）",
                            D_DISPATCH_CALL_ID_REUSED,
                        )
                    raise DispatchError(
                        f"这次调用已经派发过：call_id={request.call_id}"
                        f"（state={existing.state}）"
                    )

                if isinstance(target, _MemoryTarget):
                    self._check_memory_preconditions(request, target, args)
                else:
                    ok, reason = check_request_preconditions(
                        request.action, target.path, request.preconditions or {}
                    )
                    if not ok:
                        # 派发阶段（已批准、还没打开文件）的拒绝统一记 stale：
                        # 对这张已消费的批准而言，结论是"状态被改过了，需要重新
                        # 发起"，而不是"该换工具"（那是 prepare 阶段的判定）。
                        raise DispatchDenied(reason, "dispatch.stale_precondition")

                approval_id, grant_id = self._consume_authority(
                    request, auth, stored, target=target, args=args
                )
                dispatch_id = self._dispatch_id(request)

                seq = tx.append(
                    request.run_id,
                    "tool.intent",
                    {
                        "call_id": request.call_id,
                        "tool_id": request.tool_id,
                        "args": self._intent_args(request, args),
                        "args_hash": request.args_hash,
                        "request_id": request.request_id,
                        "request_digest": request.digest(),
                        "dispatch_id": dispatch_id,
                        "attempt_id": request.attempt_id,
                        "approval_id": approval_id,
                        "grant_id": grant_id,
                        "claim_epoch": request.claim_epoch,
                    },
                    task_id=request.task_id,
                    actor_kind="core",
                    idem_key=request.call_id,
                )
                self._insert_dispatch(
                    request, auth, target, seq, approval_id, grant_id
                )  # dispatch_id 由 _dispatch_id 稳定推导，与 intent 里的一致
                self._set_request_state(
                    request.request_id, STATE_ADMITTED, approval_id=approval_id
                )
                self._append(
                    request,
                    DISPATCH_ADMITTED,
                    {
                        "request_id": request.request_id,
                        "request_digest": request.digest(),
                        "call_id": request.call_id,
                        "approval_id": approval_id,
                        "grant_id": grant_id,
                        "intent_seq": seq,
                    },
                )
        except DispatchDenied as exc:
            self._reject(request, args, exc.reason, exc.rule_id, event=DISPATCH_REJECTED)
            return ToolResult(
                "rejected",
                "none_applied",
                {"error": exc.reason, "rule_id": exc.rule_id, "request_id": request_id},
            )
        except JobLost as exc:
            return self._reject_runtime(request, exc, "dispatch.claim_lost")
        except LeaseLost as exc:
            return self._reject_runtime(request, exc, "dispatch.lease_lost")
        except ApprovalError as exc:
            return self._reject_runtime(request, exc, "dispatch.approval_unusable")
        except WorkspaceBlocked as exc:
            return self._reject_runtime(request, exc, "dispatch.workspace_blocked")

        # intent / 审批消费 / grant 都已提交，文件一个字节都没动。
        failpoint("after_intent")
        admitted = self._admitted(request, claim, target, seq, approval_id, grant_id)
        dispatch_id = admitted.dispatch_id

        # ---- 阶段二：守卫内再核一遍，然后才真正写 ----
        memory_committed = False
        try:
            if isinstance(target, _MemoryTarget):
                # 记忆没有目录租约：不能走 `begin_execution`（它强制要求资源
                # 落在租约上）。但仍然要拿同一把执行守卫、写 execution.started，
                # 否则撤销与执行的先后就没有全序。
                with self._begin_memory_execution(request, claim) as permit:
                    result = self._memory_executor_for().execute(admitted, permit)
                    if request.tool_id == "memory.erase":
                        # 擦除的"引用计数 + 删文件 + 行更新"必须与**提交**在同一把
                        # 跨进程守卫内：否则两个进程同时擦共享同一 blob 的两条
                        # 记忆，会各自看到"对方还没擦"→ 都保留文件 → 两行都标
                        # erased、孤儿文件永久留存（复审探针确定性复现过）。
                        self._commit_result(request, dispatch_id, result, target, seq)
                        memory_committed = True
            else:
                with self._tasks.begin_execution(
                    self._data_root, request.run_id, auth.lease_paths
                ) as permit:
                    result = self._executor.execute(admitted, permit)
        except (
            DispatchDenied,
            ToolNotAllowed,
            RunFenced,
            JobLost,
            LeaseLost,
            GuardTimeout,
            ApprovalError,
            WorkspaceBlocked,
            OverwriteRefused,
            ToolArgumentError,
            WorkspaceViolation,
        ) as exc:
            # 全部发生在"打开文件之前"：拒绝是确定的，一个字节都没动
            return self._abort_dispatch(request, dispatch_id, exc)
        except Exception as exc:  # noqa: BLE001 - 写入阶段炸了：效果未知
            # 已经进入执行、intent 已提交，抛异常说明"可能写了一半"。
            # 这种情况唯一诚实的结论是 unknown，并且立刻阻断这个工作区的后续
            # 写入——否则下一个人会在一个状态不明的目录上继续改。
            return self._fail_unknown(request, dispatch_id, target, exc)

        if isinstance(target, _MemoryTarget):
            if not memory_committed:
                try:
                    self._commit_result(request, dispatch_id, result, target, seq)
                except Exception as exc:  # noqa: BLE001 - 投影事务失败：效果未知
                    # `_commit_result` 的事务已回滚（事件、凭据、投影一起消失）。
                    # 但执行器可能已经写了内容 blob，结论仍只能是 unknown；这里
                    # 补一条 tool.result(unknown)，且**不**再调用 apply_event。
                    return self._fail_unknown(request, dispatch_id, target, exc)
        else:
            self._commit_result(request, dispatch_id, result, target, seq)
        # 结果与凭据已经同事务落库。M0 时代"结果后"和"凭据后"是两个故障点，
        # M2 把两件事合成一次提交，所以这两个点落在同一处：能证明的仍然是
        # "重放不会补出第二条效果"。
        failpoint("after_result")
        failpoint("after_receipt")
        return result

    # ---- 撤销 ----

    def revoke(
        self, approval_id: str, *, actor: HumanIdentity, reason: str = "revoked by human"
    ) -> Approval:
        """撤销一次批准。

        与执行抢同一把守卫：撤销先拿到锁，就把还没开始执行的那次派发作废；
        执行先拿到锁并写下 `execution.started`，这次调用已经是 in-flight，
        撤销会明确报错而不是假装成功。
        """
        if not isinstance(actor, HumanIdentity):
            raise ApprovalError("撤销人必须是 HumanIdentity（只能由本地 CLI 构造）")
        root = self._require_data_root()
        with execution_guard(root, timeout_ms=self._guard_timeout_ms):
            with self._log.transaction():
                current = self._approvals.get(approval_id)
                if current is None:
                    raise ApprovalError(f"审批不存在：{approval_id}")
                if current.state in ("pending", "approved"):
                    return self._approvals.revoke(approval_id, actor=actor)
                if current.state != "consumed":
                    raise ApprovalError(f"不能撤销这个状态：{current.state}")

                row = self._conn.execute(
                    "SELECT * FROM dispatches WHERE approval_id=?", (approval_id,)
                ).fetchone()
                if row is not None and row["state"] != DISPATCH_INTENT:
                    raise ApprovalError(
                        f"这次调用已经开始执行（state={row['state']}），"
                        "撤销不承诺回滚；请用 recover 核查实际效果"
                    )
                revoked = self._approvals.revoke_consumed(
                    approval_id, actor=actor, reason=reason
                )
                if row is not None:
                    self._conn.execute(
                        "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                        " WHERE dispatch_id=?",
                        (DISPATCH_ROW_REVOKED, reason, self._clock(), row["dispatch_id"]),
                    )
                    self._log.append(
                        row["run_id"],
                        DISPATCH_REVOKED,
                        {
                            "dispatch_id": row["dispatch_id"],
                            "call_id": row["call_id"],
                            "approval_id": approval_id,
                            "reason": reason,
                        },
                        task_id=row["task_id"],
                        actor_kind="human",
                        actor_id=actor.actor,
                    )
        return revoked

    # ---- 控制 Run（undo / recover 的写入口） ----

    def _takeover_refusal_reason(self, previous_run: str, assessment: Any) -> str:
        """接管被拒时给人看的说明：要说清"前任留下了什么没核实"。"""
        bits: list[str] = []
        if assessment.unresolved:
            bits.append(f"无法核实的调用 {list(assessment.unresolved)}")
        if assessment.unknown:
            bits.append(f"磁盘状态既非写前也非写后 {list(assessment.unknown)}")
        if assessment.corrupt:
            bits.append("快照清单损坏")
        if assessment.missing_manifest and assessment.pending_writes:
            bits.append("有写入意图却没有写前清单")
        detail = "；".join(bits) or "对账未通过"
        return (
            f"拒绝接管：前任 run {previous_run} 有未核实副作用（{detail}）。"
            "请先用 tianshu recover 核对并解除阻断，再重试；"
            "在核实之前该工作区不允许被新 owner 继续写"
        )

    def _make_takeover_reconciler(
        self, *, run_id: str, task_id: str, refusals: list[dict[str, Any]]
    ):
        """构造租约接管的对账回调（issue #61.2）。

        回调本身**只读**：调 `recovery.assess_run` 取前任状态。干净才在
        `acquire` 的事务里写 `run.control` 审计（与租约同一个提交）；不干净
        只把拒绝信息记进 `refusals` 并抛 `LeaseConflict`。拒绝的持久证据
        （`recovery.blocked` + 权威根）由 `open_control_run` 在事务回滚**之后**
        落——在回调里写事件会跟着 `acquire` 的事务一起回滚，日志会少一条。
        """

        def reconcile(previous_run_id: str, resource_path: str) -> None:
            # 局部 import：dispatcher / recovery 有共同的底层依赖，模块级互相
            # import 没必要，也容易在别人的重构里变成环。
            from .recovery import assess_run

            assessment = assess_run(self._data_root, previous_run_id, log=self._log)
            if assessment.blocks_takeover:
                reason = self._takeover_refusal_reason(previous_run_id, assessment)
                refusals.append(
                    {
                        "previous_run": previous_run_id,
                        "resource": resource_path,
                        "assessment": assessment.to_payload(),
                        "reason": reason,
                        "by_run": run_id,
                    }
                )
                raise LeaseConflict(reason)
            # 干净：留审计。用现有 run.control（不新增事件类型）；
            # `checked` 记录这次对账看到的状态，事后能回答"凭什么允许接管"。
            self._log.append(
                run_id,
                "run.control",
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "action": "lease_takeover",
                    "previous_run": previous_run_id,
                    "checked": assessment.to_payload(),
                },
                task_id=task_id,
                actor_kind="system",
            )

        return reconcile

    def _record_takeover_refusal(self, refusal: dict[str, Any]) -> None:
        """把"拒绝接管"落成可审计的阻断：日志事件 + 用户级权威根。

        必须在租约 acquire 的事务回滚之后调用（事件写在同一个事务里会被回滚，
        日志就丢了这条拒绝）。两步都做：只写日志的话换个数据根还是能绕过去，
        只写权威根的话本数据根回放不出"为什么被挡"。
        """
        previous_run = refusal["previous_run"]
        resource = str(Path(refusal["resource"]).resolve())
        checked = refusal["assessment"]
        call_ids = [*checked.get("unresolved", ()), *checked.get("unknown", ())]
        reason = (
            "接管被拒：前任 run 存在未核实副作用"
            f"（previous_run={previous_run}），阻断后续写入"
        )
        self._log.append(
            previous_run,
            "recovery.blocked",
            {
                "workspace": resource,
                "call_ids": call_ids,
                "reason": reason,
                "takeover_refused_by": refusal["by_run"],
            },
            actor_kind="system",
        )
        from . import authority
        from .authority import AuthorityUnavailable, AuthorityWriteFailed

        try:
            authority.register_blocked(resource, run_id=previous_run, reason=reason)
        except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
            raise AuthorityWriteFailed(
                f"接管已拒绝，但跨数据根阻断未生效：{resource}（{exc}）"
            ) from exc

    def takeover_reconcile(
        self, *, run_id: str, task_id: str, refusals: list[dict[str, Any]]
    ) -> ReconcileFn:
        """租约接管的对账回调。

        主循环（`AgentLoop.start` / `resume_run`）与撤销/恢复的控制 Run 共用这
        一条实现：**所有**会抢 stale 租约的路径都走同一次对账，否则"完整 takeover"
        只保护控制 Run，真正的写入路径照样能被接管。
        """
        return self._make_takeover_reconciler(
            run_id=run_id, task_id=task_id, refusals=refusals
        )

    def record_takeover_refusals(self, refusals: list[dict[str, Any]]) -> None:
        """把被拒的接管落成阻断证据（必须在租约事务回滚**之后**调用）。

        只回答"证据登记是否完整"：全部成功就返回；有一条失败就抛
        `AuthorityWriteFailed`，调用方保留原 `LeaseConflict` 并附上这条失败——
        拒绝已经发生，不能因为写不进阻断证据就把"拒绝"改写成"允许"。
        """
        from .authority import AuthorityWriteFailed

        problems: list[str] = []
        for refusal in refusals:
            try:
                self._record_takeover_refusal(refusal)
            except TianshuError as exc:
                problems.append(str(exc))
        if problems:
            raise AuthorityWriteFailed("；".join(problems))

    def open_control_run(
        self,
        *,
        goal: str,
        workspaces: list[WorkspaceBinding | str | dict[str, Any]],
        memory_scopes: list[MemoryScope | str] = (),
    ) -> ControlRun:
        """为"用户直接发起的写"建立自己的 Task/Run/JobClaim。

        不借用历史 Run 的身份：那个 Run 早就结束了，它的 revision/epoch/claim
        都不再有效。撤销操作用它自己的身份，并把"撤销的是谁"写进目标里。

        抢 stale 租约前**必须对账**（issue #61.2）：前任 run 有未核实副作用时
        拒绝接管、登记阻断，而不是在同一棵目录树上接着写。

        `memory_scopes`（契约 v1.2）：控制面显式声明的记忆作用域，落进
        `task.created` payload 与 `Task.is_control`。记忆动作据此授权；文件动作
        仍然只认目录绑定 + 租约，不因这组声明多拿一个字节。
        """
        goal = (goal or "").strip()
        if not goal:
            raise DispatchError("控制 Run 的目标不能为空")
        declared: list[str] = []
        for item in memory_scopes:
            scope = item if isinstance(item, MemoryScope) else parse_memory_uri(item)
            declared.append(scope.to_uri())
        task = self._tasks.create_task(
            goal, workspaces, control=True, memory_scopes=declared
        )
        run = self._tasks.start_run(task.task_id)
        writable = [b.identity() for b in task.workspaces if b.can_write]
        # **零绑定控制 Run 是合法的**：人直接发起的记忆操作（CLI 的
        # `memory confirm` / `memory forget`，scope=user 时没有任何目录可绑）
        # 走的就是这条路。以前这里强制要求至少一个可写绑定，等于让"人的记忆
        # 决定"无法被记录成一个带 intent/result + 审批事件的副作用。
        #
        # 这不是在放宽文件写入权限：没有可写绑定就没有租约，而文件动作在派发
        # 层还必须过两道**独立**判定——
        #   1. `_authority()`：`_leases.held_paths()` 为空 →
        #      `dispatch.lease_lost`（"本实例没有持有任何租约"）；
        #   2. `_resolve_target()`：目标不被任何绑定覆盖 →
        #      `c4.outside_workspace_unsupported`。
        # 记忆动作则由 `memory_scope_allowed()` 单独核验作用域，它本来就不
        # 依赖目录租约（记忆没有目录）。
        #
        # 有绑定时仍然**先对账再接管**（issue #61.2）：主循环与撤销/恢复共用
        # 同一条对账实现，不因为"控制 Run 可以不绑目录"就跳过。
        refusals: list[dict[str, Any]] = []
        if writable:
            try:
                self._leases.acquire(
                    writable,
                    run.run_id,
                    reconcile=self.takeover_reconcile(
                        run_id=run.run_id, task_id=task.task_id, refusals=refusals
                    ),
                )
            except LeaseConflict as exc:
                try:
                    self.record_takeover_refusals(refusals)
                except TianshuError as record_exc:
                    raise LeaseConflict(
                        f"{exc}；且拒绝接管的阻断证据登记不完整：{record_exc}"
                    ) from exc
                raise
        job = self._claims.enqueue(run.run_id)
        claim = self._claims.claim(job.job_id)
        run = self._tasks.transition(run.run_id, RUN_RUNNING, claim=claim,
                                     reason="control_run")
        return ControlRun(
            task=task,
            run=run,
            claim=claim,
            lease_paths=tuple(str(b.root_path) for b in task.workspaces if b.can_write),
            memory_scopes=tuple(task.memory_scopes),
        )

    def close_control_run(self, control: ControlRun, *, outcome: str = "succeeded",
                          effect_state: str = "none_applied") -> None:
        self._leases.release_run(control.run.run_id)
        self._tasks.finish_run(
            control.run.run_id, outcome=outcome, effect_state=effect_state
        )
        try:
            self._claims.finish(control.claim, outcome=outcome)
        except (JobLost, TianshuError):
            # 作业已经被别人收尾：控制 Run 的结论仍以 run.finish 为准
            pass

    # ---- 内部：核验 ----

    def _precheck_exec(
        self, request: ActionRequest, args: dict[str, Any], target: Path
    ) -> None:
        """在发起审批**之前**跑一遍 F2 的判定。

        两件事必须在审批前知道，否则用户会为一条注定跑不了的命令签字：
        - argv / cwd / env 是否被 exec 策略允许（`PolicyDenied`）；
        - 本次请求的执行方式后端做不做得到（`PolicyUnsupported`）。

        这里调用的是 F2 的同一个 `check_spawn()`，不是第二套策略实现；执行时
        `spawn()` 会再核一次（两边看到的必须是同一个世界）。
        """
        task = self._tasks.get_task(request.task_id)
        policy = task.exec_policy if task is not None else None
        if policy is None:
            # 没配置策略：交给 classify 报 c4.exec_not_configured。
            return

        argv = args.get("argv")
        try:
            check_spawn(
                policy,
                argv,
                target,
                filtered_env(policy, dict(os.environ)),
            )
            if args.get("pty"):
                if not policy.allow_pty:
                    raise PolicyDenied(
                        "本次调用请求了 PTY，但策略没有允许"
                        "（ExecPolicy.allow_pty=False）",
                        rule_id="f2.pty_not_allowed",
                    )
                check_capabilities(policy, LOCAL_EXEC_CAPABILITIES)
        except PolicyDenied as exc:
            raise DispatchDenied(
                f"exec 策略拒绝：{exc}", "c4.exec_policy_denied"
            ) from exc
        except PolicyUnsupported as exc:
            raise DispatchDenied(
                f"exec 后端不支持：{exc}", "c4.exec_unsupported"
            ) from exc

    @contextlib.contextmanager
    def _begin_memory_execution(self, request: ActionRequest, claim: JobClaim):
        """记忆写入的执行守卫。

        与 `TaskStore.begin_execution()` 做同样三件事——拿执行守卫、核 Run 状态、
        写 `execution.started`——但**不核目录租约**：记忆作用域不是目录。租约
        被跳过的那部分授权，由 `_authority()` 与 `MemoryExecutor._verify()` 的
        作用域核验补上。
        """
        root = self._require_data_root()
        with execution_guard(root, timeout_ms=self._guard_timeout_ms):
            self._claims.assert_claim(claim)
            run = self._tasks.assert_current(request.run_id)
            if run.state != RUN_RUNNING:
                raise RunFenced(
                    f"run 不在执行状态，拒绝开始写入：{request.run_id}"
                    f"（state={run.state}）"
                )
            started_at = self._clock()
            with self._log.transaction() as tx:
                seq = tx.append(
                    request.run_id,
                    "execution.started",
                    {
                        "run_id": request.run_id,
                        "task_id": request.task_id,
                        "goal_revision": run.goal_revision,
                        "lease_epoch": run.lease_epoch,
                        "resources": list(request.resources),
                    },
                    task_id=request.task_id,
                )
            yield ExecutionPermit(
                run_id=run.run_id,
                task_id=run.task_id,
                goal_revision=run.goal_revision,
                lease_epoch=run.lease_epoch,
                resources=tuple(request.resources),
                seq=seq,
                started_at=started_at,
            )

    def _policy_preconditions(
        self, request: ActionRequest, target: _Target, args: dict[str, Any]
    ) -> dict[str, Any]:
        """策略层需要的"执行前状态"。

        文件动作直接用请求里绑定的 preconditions，**一个字都不补**——那些值
        是 `build_request()` 当场从磁盘读出来、进过 action_digest 的。记忆动作
        则必须由派发层从权威投影补两项：种类是否存在、目标记忆是否已存在/
        它的 origin 是什么。它们只用于策略判定，不进 action_digest（记忆的
        审批绑定仍靠 memory_id/revision/args_hash）。
        """
        pre = dict(request.preconditions or {})
        if not isinstance(target, _MemoryTarget):
            return pre

        projection = self._memory_projection()
        scope = target.scope
        # 控制面声明的作用域（只对 is_control 的任务生效）。**无条件覆盖**请求
        # 自带的同名键：这个值只能由派发层从 task 投影算出来，不能由调用方填。
        task = self._tasks.get_task(request.task_id)
        is_control = bool(getattr(task, "is_control", False)) if task else False
        pre["memory_declared_scopes"] = (
            list(getattr(task, "memory_scopes", ()) or ()) if is_control else []
        )
        pre["memory_is_control"] = is_control
        # 策略要区分"哪个记忆工具"：`user_explicit` 的免审批只属于 confirm，
        # 不属于 edit / promote（它们改的是长期召回的正文或作用域）。
        pre["memory_tool"] = request.tool_id
        if request.action == ACTION_CREATE:
            kind = args.get("kind")
            pre["memory_kind"] = kind if isinstance(kind, str) else ""
            pre["memory_exists"] = False
            content = args.get("content")
            if (
                isinstance(kind, str)
                and kind in supported_memory_kinds()
                and isinstance(content, str)
            ):
                try:
                    redacted, blocked = projection.redact_or_block(content)
                except (AttributeError, TianshuError):
                    redacted, blocked = content, False
                if not blocked:
                    try:
                        pre["memory_exists"] = (
                            projection.find_duplicate(
                                scope=scope, kind=kind, content=redacted
                            )
                            is not None
                        )
                    except TianshuError:
                        # 投影读失败不是"目标不存在"的证明；保持 False 让
                        # 策略先放行候选，执行侧会用同一投影再判一次并拒绝。
                        pre["memory_exists"] = False
            return pre

        memory_id = args.get("memory_id")
        if isinstance(memory_id, str) and memory_id:
            try:
                memory = projection.get(memory_id, allowed=(scope,))
            except TianshuError:
                # 读不到 origin 时**不能**推断 user_explicit：宁可要求审批。
                memory = None
            pre["memory_exists"] = memory is not None
            pre["memory_origin"] = (
                str(getattr(memory, "origin", "")) if memory is not None else ""
            )
        else:
            pre["memory_exists"] = False
            pre["memory_origin"] = ""
        return pre

    def _check_memory_preconditions(
        self,
        request: ActionRequest,
        target: _MemoryTarget,
        args: dict[str, Any],
    ) -> None:
        """记忆的 status/revision 从权威投影读；不成立就抛确定的拒绝。"""
        task = self._tasks.get_task(request.task_id)
        is_control = bool(getattr(task, "is_control", False)) if task else False
        if not is_control and request.tool_id in (
            "memory.remember",
            "memory.promote",
            "memory.erase",
        ):
            # 契约 v1.2：控制面专属工具不能在普通任务里执行。拒绝点在派发层，
            # 执行侧还有一次独立核验（memory_exec._verify）。
            raise DispatchDenied(
                f"控制面专属工具不能在普通任务里执行：{request.tool_id}",
                "c4.memory_control_scope_binding",
            )
        if is_control and request.tool_id == "memory.promote":
            # 提升目标作用域必须由控制 Run 显式声明（user 恒可）。
            try:
                new_scope = build_memory_scope(
                    args.get("scope"), args.get("scope_id")
                )
            except TianshuError as exc:
                raise DispatchDenied(
                    f"提升目标作用域不合法：{exc}", "c4.memory_scope_denied"
                ) from exc
            declared = list(getattr(task, "memory_scopes", ()) or ())
            if new_scope.to_uri() == target.uri:
                raise DispatchDenied(
                    "提升目标作用域与原作用域相同", "c4.memory_scope_denied"
                )
            if new_scope.scope != "user" and new_scope.to_uri() not in declared:
                raise DispatchDenied(
                    f"提升目标作用域不在控制 Run 的声明内：{new_scope.to_uri()}",
                    "c4.memory_scope_denied",
                )
        if (
            request.tool_id in ("memory.propose", "memory.consolidate")
            and target.scope.scope == "workspace"
        ):
            # workspace 是共享边界：模型可见的 propose **无条件**拒，不因
            # Task.is_control 放行（控制面标记会被同一 Task 上后开的模型 Run
            # 继承）。执行侧还有一道同名核验，这里只是把拒绝提前到派发。
            raise DispatchDenied(
                f"workspace 作用域只能由人经 memory.promote 进入："
                f"{target.scope.to_uri()}",
                "c4.memory_scope_denied",
            )
        if request.action == ACTION_CREATE:
            return

        memory_id = args.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            raise DispatchDenied(
                "memory_id 必须是非空字符串", "dispatch.memory_not_found"
            )
        declared = (request.preconditions or {}).get("memory_id")
        if declared is not None and declared != memory_id:
            raise DispatchDenied(
                f"请求声明的 memory_id 与参数不一致：{declared!r} ≠ {memory_id!r}",
                "dispatch.memory_resource_mismatch",
            )
        try:
            observed = self._memory_projection().observe(
                memory_id, allowed=(target.scope,)
            )
        except TianshuError as exc:
            raise DispatchDenied(
                f"记忆观察失败：{exc}", "dispatch.memory_not_found"
            ) from exc
        if observed is None:
            raise DispatchDenied(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        ok, reason = check_request_preconditions(
            request.action,
            target.uri,
            request.preconditions or {},
            memory_observed=observed,
        )
        if not ok:
            raise DispatchDenied(reason, "dispatch.memory_stale_revision")
        if request.tool_id == "memory.erase":
            # 已擦除 = 确定拒绝，别让人为一次注定失败的擦除签字。
            row = self._memory_projection().get(memory_id, allowed=(target.scope,))
            if row is not None and row.erased_at is not None:
                raise DispatchDenied(
                    f"记忆已经擦除过：{memory_id}", "dispatch.memory_stale_revision"
                )

    def _typecheck(
        self, request: ActionRequest, claim: JobClaim, args: dict[str, Any]
    ) -> None:
        if not isinstance(request, ActionRequest):
            raise DispatchError("request 必须是 ActionRequest")
        if not isinstance(claim, JobClaim):
            raise DispatchError("必须传 JobClaim")
        if not isinstance(args, dict):
            raise DispatchError("args 必须是对象")
        if request.action not in APPROVAL_ACTIONS and request.action != ACTION_CREATE:
            raise DispatchError(
                f"只读动作不经过派发（没有可授权的副作用）：{request.action}"
            )
        if request.tool_id not in SPECS:
            raise DispatchError(f"工具不在白名单：{request.tool_id}")

    def _authority(self, request: ActionRequest, claim: JobClaim) -> _Authority:
        if claim.run_id != request.run_id:
            raise DispatchDenied(
                f"claim 属于另一个 Run：claim.run_id={claim.run_id}，"
                f"request.run_id={request.run_id}",
                "dispatch.claim_mismatch",
            )
        if (
            claim.owner_instance_id != request.owner_instance_id
            or claim.claim_epoch != request.claim_epoch
        ):
            raise DispatchDenied(
                "请求绑定的 owner/claim_epoch 与当前 claim 不符",
                "dispatch.claim_mismatch",
            )
        if self._leases.identity.instance_id != claim.owner_instance_id:
            raise DispatchDenied(
                "claim 的 owner 不是本进程实例：不能替别的实例派发写入",
                "dispatch.foreign_instance",
            )
        try:
            self._claims.assert_claim(claim)
        except JobLost as exc:
            raise DispatchDenied(f"claim 已失效：{exc}", "dispatch.claim_lost") from exc

        try:
            run = self._tasks.assert_current(request.run_id)
        except RunFenced as exc:
            raise DispatchDenied(f"Run 不可写：{exc}", "dispatch.run_fenced") from exc
        if run.task_id != request.task_id:
            raise DispatchDenied(
                f"Run 不属于这个 Task：{run.task_id} ≠ {request.task_id}",
                "dispatch.task_mismatch",
            )
        if run.state != RUN_RUNNING:
            # PAUSED / WAITING_APPROVAL 的 Run 手里可能还拿着有效租约和 claim
            # （等待审批期间必须续租，否则每轮 TTL 都要重新审批），但它们
            # 不该执行。这条判断如果只放在主循环里，绕过主循环直接派发就能写。
            raise DispatchDenied(
                f"run 不在执行状态，拒绝派发：{request.run_id}（state={run.state}）",
                "dispatch.not_running",
            )
        if (run.goal_revision, run.lease_epoch) != (
            request.goal_revision,
            request.lease_epoch,
        ):
            raise DispatchDenied(
                "请求绑定的 revision/epoch 已过期"
                f"（当前 {run.goal_revision}/{run.lease_epoch}，"
                f"请求 {request.goal_revision}/{request.lease_epoch}）",
                "dispatch.stale_revision",
            )

        task = self._tasks.get_task(request.task_id)
        if task is None:
            raise DispatchDenied(f"task 不存在：{request.task_id}", "dispatch.no_task")
        if not request.resources:
            raise DispatchDenied("动作没有声明资源", "c4.no_resource")

        bindings = list(task.workspaces or [])
        # POSIX 上 `/a/b://c` 是合法绝对路径；判序必须与 resources.py 一致：
        # 绝对路径优先判为 file，只有非绝对路径才按 scheme 分流。
        scheme_resources = [
            r
            for r in request.resources
            if isinstance(r, str) and not os.path.isabs(r) and "://" in r
        ]
        if scheme_resources:
            try:
                parsed = [parse_resource(r) for r in request.resources]
            except ResourceError as exc:
                raise DispatchDenied(
                    f"资源引用不合法：{exc}", "c4.unsupported"
                ) from exc
            if len(parsed) != 1 or parsed[0].kind != KIND_MEMORY:
                if request.tool_id in MEMORY_TOOL_IDS:
                    raise DispatchDenied(
                        "记忆工具的资源必须是单个 memory:// 作用域",
                        "dispatch.memory_resource_mismatch",
                    )
                raise DispatchDenied(
                    "带 scheme 的资源只支持单个 memory:// 作用域",
                    "c4.multi_resource",
                )
            if request.tool_id not in MEMORY_TOOL_IDS:
                raise DispatchDenied(
                    f"文件工具收到记忆资源：{request.resources[0]!r}",
                    "dispatch.memory_resource_mismatch",
                )
            scope = parsed[0].memory_scope
            is_control = bool(getattr(task, "is_control", False))
            declared = (
                tuple(getattr(task, "memory_scopes", ()) or ()) if is_control else ()
            )
            allowed, reason = memory_scope_allowed(
                scope,
                task_id=request.task_id,
                bindings=bindings,
                declared=declared,
            )
            if not allowed:
                # 租约是给目录的；记忆跳过租约，但**不跳过**作用域授权。
                raise DispatchDenied(reason, "c4.memory_scope_denied")
            return _Authority(run, task, bindings, [], [])

        if request.tool_id in MEMORY_TOOL_IDS:
            raise DispatchDenied(
                f"记忆工具必须使用 memory:// 资源：{request.resources[0]!r}",
                "dispatch.memory_resource_mismatch",
            )

        resources = [ResourceIdentity.of(Path(r)) for r in request.resources]

        try:
            self._leases.assert_covers(resources, request.run_id)
        except LeaseLost as exc:
            # 只读绑定永远不会有写租约，所以"租约不覆盖"在这里几乎是必然的。
            # 但那句话对用户没有意义——真实原因是"这个目录你只让我读"。
            # 判定用的仍是同一个 `_covers`，不是另写一套路径规则。
            readonly = [
                r
                for r in request.resources
                if any(PolicyEngine._covers(b, r) and not b.can_write for b in bindings)
                and not any(
                    PolicyEngine._covers(b, r) and b.can_write for b in bindings
                )
            ]
            if readonly:
                raise DispatchDenied(
                    f"目标目录是只读绑定：{readonly[0]}", "c4.readonly_binding"
                ) from exc
            raise DispatchDenied(
                f"资源租约不覆盖目标：{exc}", "dispatch.lease_lost"
            ) from exc

        lease_paths = self._leases.held_paths(request.run_id)
        if not lease_paths:
            raise DispatchDenied("本实例没有持有任何租约", "dispatch.lease_lost")

        # 阻断是"这个目录有无法核实的副作用"，按目录查而不是按文件查
        for resource in request.resources:
            for binding in bindings:
                if PolicyEngine._covers(binding, resource):
                    try:
                        ensure_not_blocked(self._log, binding.root_path)
                    except WorkspaceBlocked as exc:
                        raise DispatchDenied(
                            str(exc), "dispatch.workspace_blocked"
                        ) from exc

        return _Authority(run, task, bindings, resources, lease_paths)

    def _resolve_target(self, request: ActionRequest, auth: _Authority) -> _Target:
        if len(request.resources) != 1:
            # 文件分支的历史文案与编号保持不变（旧日志/测试逐字依赖它）。
            raise DispatchDenied(
                "文件动作只支持单个资源（多资源一次写入无法给出单一 preimage 边界）",
                "c4.multi_resource",
            )

        raw = request.resources[0]
        if isinstance(raw, str) and raw.startswith("memory://"):
            if request.tool_id not in MEMORY_TOOL_IDS:
                raise DispatchDenied(
                    f"文件工具收到记忆资源：{raw}", "dispatch.memory_resource_mismatch"
                )
            try:
                scope = parse_memory_uri(raw)
            except ResourceError as exc:
                raise DispatchDenied(
                    f"记忆资源引用不合法：{exc}", "dispatch.memory_resource_mismatch"
                ) from exc
            # 记忆不 Path.resolve，也不要求目录租约：作用域授权已在
            # `_authority()` 里独立核验。
            return _MemoryTarget(scope=scope, uri=scope.to_uri())

        # 资源种类按 scheme 判定（见 resources.py）。**没有执行器**的种类在这里
        # 就拒绝：既不回退成"当成文件路径试试"，也不进审批流程给用户一个
        # "批了就能用"的错觉（网络 / MCP / 委派的执行器属 G2）。
        if isinstance(raw, str) and not os.path.isabs(raw) and "://" in raw:
            ref = resolve_scheme_resource(raw)
            if ref.kind != KIND_MEMORY:  # pragma: no cover - 记忆在上面已返回
                raise DispatchDenied(
                    f"{ref.kind} 资源的执行器尚未实现（G2），拒绝派发：{ref.uri}",
                    "dispatch.resource_kind_unsupported",
                )
        if request.tool_id in MEMORY_TOOL_IDS:
            raise DispatchDenied(
                f"记忆工具必须使用 memory:// 资源：{raw}",
                "dispatch.memory_resource_mismatch",
            )

        target = Path(raw).resolve()

        covering = [
            b for b in auth.bindings if PolicyEngine._covers(b, str(target))
        ]
        writable = [b for b in covering if b.can_write]
        candidates = writable
        if not candidates and covering:
            # 目标在绑定目录内但只能读：让策略层去拒绝（c4.readonly_binding），
            # 而不是含糊地报成"目录外"。
            candidates = covering
        if not candidates:
            # 策略层对目录外访问给的是"需审批"，但 M2 还没有目录外的证据边界
            # （证据按 workspace 根存 manifest），所以执行侧明确拒绝而不是假装支持。
            raise DispatchDenied(
                f"暂不支持目录外文件写入（M2 只支持绑定目录内）：{target}",
                "c4.outside_workspace_unsupported",
            )
        binding = max(candidates, key=lambda b: len(overlap_key(b.root_path)))
        ws = Workspace(Path(binding.root_path))
        rel = ws.relative(target)
        if request.action == ACTION_CREATE and not current_backend().is_dir(
            target.parent
        ):
            raise DispatchDenied(
                f"目标父目录不存在：{target.parent}", "dispatch.no_parent"
            )
        return _FileTarget(target, rel, str(ws.root), ResourceIdentity.of(target))

    def _requires_approval(
        self, request: ActionRequest, target: _Target, args: dict[str, Any]
    ) -> bool:
        """这次动作是否必须有一张已批准的审批。

        文件动作完全沿用原判定（动作集合）。记忆的 `confirm` 是覆盖类动作，
        唯一例外是记忆本身的 `origin=user_explicit`：用户本人刚说的话就是
        授权。这里从**权威投影**读 origin，不接受模型参数或调用方声明。
        """
        if request.action not in APPROVAL_ACTIONS:
            return False
        if (
            isinstance(target, _MemoryTarget)
            and request.action == ACTION_OVERWRITE
            # 免审批只属于 `memory.confirm`：用户本人刚说的话可以直接确认。
            # `memory.edit` / `memory.promote` 也在 overwrite 里，但它们改的是
            # 长期召回的正文或作用域，**永远**要一张人批准的审批。
            and request.tool_id == "memory.confirm"
        ):
            memory_id = args.get("memory_id")
            if isinstance(memory_id, str) and memory_id:
                try:
                    memory = self._memory_projection().get(
                        memory_id, allowed=(target.scope,)
                    )
                except TianshuError:
                    # 读不到来源时**不能**推断"用户明示"：那等于凭空免掉审批。
                    # 后续执行侧会以确定拒绝收尾，而不是 unknown。
                    return True
                if memory is not None and getattr(memory, "origin", "") == "user_explicit":
                    return False
        return True

    def _consume_authority(
        self,
        request: ActionRequest,
        auth: _Authority,
        stored: StoredRequest,
        *,
        target: _Target,
        args: dict[str, Any],
    ) -> tuple[str | None, str]:
        """消费审批（如果有）并签发+消费一次性 grant。必须在派发事务内调用。"""
        digest = request.digest()
        action = request.action_digest()
        approval_id: str | None = None
        issuer = "policy:auto"

        if self._requires_approval(request, target, args):
            approval = (
                self._approvals.get(stored.approval_id)
                if stored.approval_id
                else self._approvals.find_for(request)
            )
            if approval is None:
                raise DispatchDenied(
                    "没有可用的批准（请先 approvals approve）", "c4.no_approval"
                )
            if approval.run_id != request.run_id:
                raise DispatchDenied(
                    "审批属于另一个 Run", "c4.approval_run_mismatch"
                )
            if approval.action_digest and approval.action_digest != action:
                # 审批对的是另一个动作（换了参数/目标/revision/绑定）。这里必须
                # 自己比一次，而不能只信"按动作摘要查找到的那一张"——旧库里的
                # 审批没有动作摘要，回退路径靠的仍是完整请求摘要。
                raise DispatchDenied(
                    "审批绑定的动作与本次请求不符", "c4.approval_mismatch"
                )
            if approval.state != "approved":
                raise DispatchDenied(
                    f"审批状态不是已批准：{approval.state}", "c4.approval_state"
                )
            if approval.expires_at <= self._clock():
                # 已批准但自己过期了：与"没批准"不同，要说清楚是过期。
                raise DispatchDenied(
                    "审批已过期（请重新发起）", "c4.approval_expired"
                )
            self._approvals.consume(
                approval.approval_id, action_digest=action, request_digest=digest
            )
            approval_id = approval.approval_id
            issuer = f"approval:{approval.decided_by or 'unknown'}"

        lease_expires = self._lease_deadline(auth.lease_paths, request.run_id)
        subject = f"{request.run_id}#{request.claim_epoch}"
        grant = self._grants.issue(
            subject=subject,
            request=request,
            issuer=issuer,
            max_expires_at=lease_expires,
        )
        self._grants.consume(
            grant.grant_id, request_digest=digest, subject=subject
        )
        return approval_id, grant.grant_id

    def _lease_deadline(self, lease_paths: list[str], run_id: str) -> int | None:
        expiries = []
        for path in lease_paths:
            lease = self._leases.holder(Path(path))
            if lease is not None and lease.run_id == run_id:
                expiries.append(lease.expires_at)
        return min(expiries) if expiries else None

    # ---- 内部：登记 ----

    def _insert_dispatch(
        self,
        request: ActionRequest,
        auth: _Authority,
        target: _Target,
        seq: int,
        approval_id: str | None,
        grant_id: str,
    ) -> None:
        now = self._clock()
        if isinstance(target, _MemoryTarget):
            # 记忆没有目录。`target` 记 scope URI，`workspace_root` 记 scope_id
            # （人类可读），`resource_key` 用稳定前缀避免与文件路径混淆。
            target_value = target.uri
            rel_path = ""
            workspace_root = target.scope.scope_id
            resource_key = f"memory:{target.scope.scope}:{target.scope.scope_id}"
        else:
            target_value = str(target.path)
            rel_path = target.rel_path
            workspace_root = target.workspace_root
            resource_key = target.identity.key
        self._conn.execute(
            "INSERT INTO dispatches (dispatch_id, run_id, task_id, call_id,"
            " request_id, request_digest, tool_id, action, resources, target,"
            " rel_path, workspace_root, resource_key, approval_id, grant_id,"
            " claim_epoch, owner_instance_id, attempt_id, intent_seq, state,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'intent',?,?)",
            (
                self._dispatch_id(request),
                request.run_id,
                request.task_id,
                request.call_id,
                request.request_id,
                request.digest(),
                request.tool_id,
                request.action,
                json.dumps(list(request.resources), ensure_ascii=False),
                target_value,
                rel_path,
                workspace_root,
                resource_key,
                approval_id,
                grant_id,
                request.claim_epoch,
                request.owner_instance_id,
                request.attempt_id,
                seq,
                now,
                now,
            ),
        )

    @staticmethod
    def _dispatch_id(request: ActionRequest) -> str:
        """派发记录的稳定身份。

        不能用裸 call_id：provider 给的短 ID（"c1"、"call_1"）在不同 Run 之间
        会重名，拿它当主键会让"另一个 run 的同名调用"互相覆盖。这里把 run 和
        call 一起哈希，既稳定（重算得出同一个值）又不会跨 run 误碰。
        """
        digest = sha256_hex(f"{request.run_id}:{request.call_id}".encode())
        return f"disp_{digest[:16]}"

    def mark_dispatch(self, run_id: str, call_id: str, state: str) -> None:
        """执行器把派发记录推进到 executing / done。"""
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=?"
                " WHERE run_id=? AND call_id=?",
                (state, self._clock(), run_id, call_id),
            )

    def _admitted(
        self,
        request: ActionRequest,
        claim: JobClaim,
        target: _Target,
        seq: int,
        approval_id: str | None,
        grant_id: str,
    ) -> AdmittedCall:
        stored = self.get_request(request.request_id)
        if isinstance(target, _MemoryTarget):
            target_value = target.uri
            rel_path = ""
            workspace_root = target.scope.scope_id
            resource_key = f"memory:{target.scope.scope}:{target.scope.scope_id}"
        else:
            target_value = str(target.path)
            rel_path = target.rel_path
            workspace_root = target.workspace_root
            resource_key = target.identity.key
        return AdmittedCall(
            dispatch_id=self._dispatch_id(request),
            request=request,
            request_digest=request.digest(),
            claim=claim,
            tool_id=request.tool_id,
            action=request.action,
            args=stored.args if stored is not None else {},
            target=target_value,
            rel_path=rel_path,
            workspace_root=workspace_root,
            resource_key=resource_key,
            resources=tuple(request.resources),
            intent_seq=seq,
            attempt_id=request.attempt_id,
            approval_id=approval_id,
            grant_id=grant_id,
        )

    def _store_request(
        self,
        request: ActionRequest,
        args: dict[str, Any],
        state: str,
        reason: str,
        rule_id: str,
        *,
        approval_id: str | None = None,
        args_redacted: bool = False,
    ) -> None:
        payload = canonical_bytes(request.to_canonical_dict()).decode("utf-8")
        args_payload = canonical_bytes(args).decode("utf-8")
        now = self._clock()
        with self._log.transaction():
            self._conn.execute(
                "INSERT INTO dispatch_requests (request_id, task_id, run_id,"
                " request_digest, payload, args_payload, args_redacted, state,"
                " reason, rule_id, approval_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(request_id) DO UPDATE SET"
                " state=excluded.state, reason=excluded.reason,"
                " rule_id=excluded.rule_id, approval_id=excluded.approval_id,"
                # 同一 `request_id` 会被写多次：`prepare()` 先存完整参数，之后
                # 被拒绝改成脱敏形态；反过来，终态之后同一请求被**重新发起**时
                # 又要拿回完整参数（执行期要用它重算 `args_hash`）。所以这里取
                # "最后一次写入"的形态，不做单向合并——单向保留会让重新发起
                # 的合法请求永远判成"参数被改过"。
                " args_payload=excluded.args_payload,"
                " args_redacted=excluded.args_redacted,"
                " updated_at=excluded.updated_at",
                (
                    request.request_id,
                    request.task_id,
                    request.run_id,
                    request.digest(),
                    payload,
                    args_payload,
                    1 if args_redacted else 0,
                    state,
                    reason,
                    rule_id,
                    approval_id,
                    now,
                    now,
                ),
            )

    def _set_request_state(
        self, request_id: str, state: str, *, approval_id: str | None = None
    ) -> None:
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatch_requests SET state=?, approval_id=COALESCE(?,"
                " approval_id), updated_at=? WHERE request_id=?",
                (state, approval_id, self._clock(), request_id),
            )
            if state in (STATE_DONE, STATE_REJECTED):
                # 终态之后不会再有人从这张表取参数去执行，正文可以脱敏了。
                self._redact_terminal_args(request_id)

    def _migrate_redacted_args(self) -> None:
        """一次性迁移：给升级前的终态行补脱敏，成功后才落持久标记。

        标记在**同一个事务**里写入，所以中途失败/被 kill 时标记不会留下，
        下次打开会重试；只有标记存在才算"这件事真的做过"。
        """
        row = self._conn.execute(
            "SELECT value FROM dispatch_meta WHERE key = ?",
            (_BACKFILL_MARKER,),
        ).fetchone()
        if row is not None:
            return
        with self._log.transaction():
            self._backfill_redacted_args()
            self._conn.execute(
                "INSERT INTO dispatch_meta(key, value) VALUES (?, '1')"
                " ON CONFLICT(key) DO NOTHING",
                (_BACKFILL_MARKER,),
            )

    def _backfill_redacted_args(self) -> None:
        """升级前就已处于终态的行：补做脱敏。**必须在调用方的事务里**。

        `args_redacted` 列是 N5 才加的，老库里那些 `done` / `rejected` 行不会
        自己回到终态，所以迁移时必须主动扫一遍。只处理 `args_redacted = 0` 的
        终态行；解析失败的行**保持原样**——宁可让审计看到"这一行没脱敏"，也不
        要把读不懂的行直接抹成不可审计。非记忆行 `_audit_args` 原样返回，不动。
        """
        rows = self._conn.execute(
            "SELECT request_id, payload, args_payload FROM dispatch_requests"
            " WHERE args_redacted = 0 AND state IN (?, ?)",
            (STATE_DONE, STATE_REJECTED),
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            try:
                request = ActionRequest.of(json.loads(row["payload"]))
                args = json.loads(row["args_payload"])
            except (TypeError, ValueError):  # 老库/外力改坏：跳过并保留原样
                continue
            if not isinstance(args, dict):
                continue
            redacted = _audit_args(request, args)
            if redacted is args:
                continue
            updates.append(
                (canonical_bytes(redacted).decode("utf-8"), row["request_id"])
            )
        for payload, request_id in updates:
            self._conn.execute(
                "UPDATE dispatch_requests SET args_payload = ?,"
                " args_redacted = 1 WHERE request_id = ? AND args_redacted = 0"
                " AND state IN (?, ?)",
                (payload, request_id, STATE_DONE, STATE_REJECTED),
            )

    def _redact_terminal_args(self, request_id: str) -> None:
        """请求进终态后，把 `args_payload` 里的记忆正文换成摘要。

        执行期的绑定校验与执行器取参数都发生在终态之前；此后这张表只剩审计
        用途。已经脱敏过的行直接跳过（幂等）。非记忆请求不动。
        """
        row = self._conn.execute(
            "SELECT payload, args_payload, args_redacted FROM dispatch_requests"
            " WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None or int(row["args_redacted"]):
            return
        try:
            request = ActionRequest.of(json.loads(row["payload"]))
            args = json.loads(row["args_payload"])
        except (TypeError, ValueError):  # pragma: no cover - 库被外力改坏
            return
        if not isinstance(args, dict):
            return
        redacted = _audit_args(request, args)
        if redacted is args:
            return
        self._conn.execute(
            "UPDATE dispatch_requests SET args_payload = ?, args_redacted = 1"
            " WHERE request_id = ?",
            (canonical_bytes(redacted).decode("utf-8"), request_id),
        )

    @staticmethod
    def _intent_args(request: ActionRequest, args: dict[str, Any]) -> dict[str, Any]:
        """写进 `tool.intent` 的参数（事件日志**不存记忆正文**）。

        F10 §3.1 要求事件日志"只存引用与审计元数据，不重复写记忆正文"。
        `memory.confirm` / `memory.forget` / `memory.erase` 的参数只有
        memory_id / revision / status，本身不是正文；**凡带 `content` 的记忆写**
        （`memory.propose` / `memory.remember` / `memory.edit`）都必须在写事件前
        拿掉正文，只留下可核对的长度与哈希。判断口径是"记忆资源 + 参数里有
        content"，不是罗列工具名——新增一个带正文的记忆写不会因为忘了登记就把
        正文漏进 append-only 的事件日志。完整参数仍在派发请求表里（受一次审批/
        派发绑定的操作记录），执行器也从那里取，不依赖事件里的副本。`args_hash`
        仍然绑完整参数，不能被这段脱敏改写。
        """
        if "content" not in args:
            return args
        if not any(is_memory_resource(resource) for resource in request.resources):
            return args
        safe = dict(args)
        content = safe.pop("content", None)
        if isinstance(content, str):
            raw = content.encode("utf-8")
            safe["content_omitted"] = True
            safe["content_bytes"] = len(raw)
            safe["content_sha256"] = sha256_hex(raw)
        return safe

    def _append(self, request: ActionRequest, event: str, payload: dict[str, Any]) -> int:
        return self._log.append(
            request.run_id,
            event,
            payload,
            task_id=request.task_id,
            actor_kind="core",
        )

    def _reject(
        self,
        request: ActionRequest,
        args: dict[str, Any],
        reason: str,
        rule_id: str,
        *,
        event: str,
    ) -> DispatchDecision:
        # 校验在**任何写入之前**：未登记的编号是代码 bug，不能先落一条日志再让人猜。
        rule_id = check_rule_id(rule_id, where=f"_reject:{event}")
        # 拒绝的行**永远不会执行**，所以这里可以脱敏而不破坏任何
        # 派发/审批绑定：被拒的尝试不该把记忆正文原文永久留在审计表里。
        redacted_args = _audit_args(request, args)
        self._store_request(
            request,
            redacted_args,
            STATE_REJECTED,
            reason,
            rule_id,
            # `args_redacted` 是审计事实字段：非记忆请求（或没有正文的记忆
            # 请求）本来就不该被标成"已脱敏"。
            args_redacted=redacted_args is not args,
        )
        self._append(
            request,
            event,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "action": request.action,
                "resources": list(request.resources),
                "reason": reason,
                "rule_id": rule_id,
            },
        )
        return DispatchDecision(
            KIND_REJECTED, request.request_id, request.digest(), reason=reason,
            rule_id=rule_id,
        )

    def _reject_runtime(
        self, request: ActionRequest, exc: BaseException, rule_id: str
    ) -> ToolResult:
        rule_id = check_rule_id(rule_id, where="_reject_runtime")
        reason = f"{type(exc).__name__}: {exc}"
        self._set_request_state(request.request_id, STATE_REJECTED)
        self._append(
            request,
            DISPATCH_REJECTED,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "reason": reason,
                "rule_id": rule_id,
            },
        )
        return ToolResult(
            "rejected",
            "none_applied",
            {"error": reason, "rule_id": rule_id, "request_id": request.request_id},
        )

    def _abort_dispatch(
        self, request: ActionRequest, dispatch_id: str, exc: BaseException
    ) -> ToolResult:
        """守卫内复核没过：一个字节都没写，明确记成 rejected。"""
        reason = f"{type(exc).__name__}: {exc}"
        rule_id = getattr(exc, "rule_id", None) or {
            ApprovalError: "dispatch.approval_revoked",
            RunFenced: "dispatch.run_fenced",
            ToolNotAllowed: "dispatch.not_admitted",
            GuardTimeout: "dispatch.guard_timeout",
            JobLost: "dispatch.claim_lost",
            LeaseLost: "dispatch.lease_lost",
            # 守卫内二次阻断核验（复审 中-1）被拒：和 phase-1 那条用同一个
            # rule id，用户看到的仍然只是"这个目录有未核实的副作用"。
            WorkspaceBlocked: "dispatch.workspace_blocked",
        }.get(type(exc), "dispatch.refused")
        rule_id = check_rule_id(rule_id, where="_abort_dispatch")

        with self._log.transaction():
            # 只在"还没人把它作废"时推进：撤销已经把它标成 revoked 的，
            # 不能被后到的拒绝改写成另一个结论。
            self._conn.execute(
                "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                " WHERE dispatch_id=? AND state=?",
                (
                    DISPATCH_ROW_REJECTED,
                    reason,
                    self._clock(),
                    dispatch_id,
                    DISPATCH_INTENT,
                ),
            )
            self._log.append(
                request.run_id,
                DISPATCH_REJECTED,
                {
                    "dispatch_id": dispatch_id,
                    "call_id": request.call_id,
                    "request_id": request.request_id,
                    "reason": reason,
                    "rule_id": rule_id,
                    "phase": "guard",
                },
                task_id=request.task_id,
                actor_kind="core",
            )
        # 审批/许可已经消费掉了（一次性），所以这次请求必须重新发起
        self._set_request_state(request.request_id, STATE_REJECTED)
        return ToolResult(
            "rejected",
            "none_applied",
            {"error": reason, "rule_id": rule_id, "request_id": request.request_id},
        )

    def _fail_unknown(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _Target,
        exc: BaseException,
    ) -> ToolResult:
        """执行中途炸了：结论只能是 unknown，并且立刻阻断后续写入。

        "没写成功"和"不知道写了没有"是两件事。后者必须挡住这个目录的后续
        写入，直到有人用 recover 核对过——否则下一个人是在一个状态不明的
        目录上继续改。
        """
        reason = f"{type(exc).__name__}: {exc}"
        result = ToolResult(
            "failed",
            "unknown",
            {"error": reason, "request_id": request.request_id},
        )
        result_event = {
            "run_id": request.run_id,
            "type": "tool.result",
            "payload": {
                "call_id": request.call_id,
                "tool_id": request.tool_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            "task_id": request.task_id,
            "actor_kind": "tool",
            "idem_key": request.call_id,
        }
        if isinstance(target, _MemoryTarget):
            # 记忆没有可阻断的目录；而且**绝不**在这里调用 apply_event：
            # 结果未知就是"投影是否更新不知道"，补写一条投影会让未知变成
            # 一个编出来的确定结论。事件与派发状态仍同事务落库。
            with self._log.transaction():
                self._log.append(**result_event)
                self._conn.execute(
                    "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                    " WHERE dispatch_id=?",
                    (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
                )
                self._set_request_state(request.request_id, STATE_REJECTED)
            return result

        batch = [
            result_event,
            {
                "run_id": request.run_id,
                "type": "recovery.blocked",
                "payload": {
                    "workspace": target.workspace_root,
                    "call_ids": [request.call_id],
                    "reason": f"执行中断且效果未知：{reason}",
                },
                "task_id": request.task_id,
                "actor_kind": "system",
            },
        ]
        self._log.append_many(batch)
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                " WHERE dispatch_id=?",
                (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
            )
            self._set_request_state(request.request_id, STATE_REJECTED)
        return result

    def _commit_result(
        self,
        request: ActionRequest,
        dispatch_id: str,
        result: ToolResult,
        target: _Target,
        intent_seq: int,
    ) -> None:
        """结果与凭据同事务落库，然后把派发记录推进到 done。

        记忆分支多一件事：`MemoryProjection.apply_event("tool.result", ...)`
        必须和 `tool.result`、凭据、派发状态在**同一个 SQLite 事务**里。投影
        更新放在事务外（或另开事务）会出现两种无法裁决的状态："事件在、投影
        不在"或"投影在、事件不在"。这里用同一个连接、同一个 `transaction()`，
        谁失败就一起回滚。
        """
        is_memory = isinstance(target, _MemoryTarget)
        projection = self._memory_projection() if is_memory else None

        result_event = {
            "run_id": request.run_id,
            "type": "tool.result",
            "payload": {
                "call_id": request.call_id,
                "tool_id": request.tool_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            "task_id": request.task_id,
            "actor_kind": "tool",
            "idem_key": request.call_id,
        }
        batch: list[dict[str, Any]] = [result_event]

        if is_memory:
            if request.tool_id in (
                "memory.confirm",
                "memory.forget",
                "memory.remember",
                "memory.edit",
                "memory.promote",
                "memory.erase",
            ):
                receipt = build_memory_receipt(
                    run_id=request.run_id,
                    call_id=request.call_id,
                    tool_id=request.tool_id,
                    args_hash=request.args_hash,
                    result=result,
                    root=target.uri,
                    # promote 的凭据针对**原行**（移动的是它）；其余工具就是本行。
                    resource=str(
                        result.output.get("receipt_memory_id")
                        or result.output.get("memory_id")
                        or ""
                    ),
                    action=request.action,
                    intent_seq=intent_seq,
                    backend="memory",
                    extra_evidence=(
                        [dict(result.output["erasure"])]
                        if isinstance(result.output.get("erasure"), dict)
                        else None
                    ),
                )
            else:
                # propose 是候选草稿，不产凭据（F10 §7）。
                receipt = None
        elif request.action == ACTION_EXEC:
            # 命令的"写后状态"不是文件哈希：凭据记的是执行摘要，磁盘核对
            # 由 verify_receipts 明确跳过（见 receipt.build_exec_receipt）。
            receipt = build_exec_receipt(
                run_id=request.run_id,
                call_id=request.call_id,
                tool_id=request.tool_id,
                args_hash=request.args_hash,
                result=result,
                backend=self._executor.backend_name,
                root=target.workspace_root,
                resource=str(target.path),
                action=request.action,
                intent_seq=intent_seq,
            )
        else:
            receipt = build_receipt(
                run_id=request.run_id,
                call_id=request.call_id,
                tool_id=request.tool_id,
                args_hash=request.args_hash,
                result=result,
                backend=self._executor.backend_name,
                # 证据键是 (workspace 根, 相对路径)：根从**这次派发**解析出来的
                # 目标里取，而不是从某个默认目录取。多 workspace 任务里只有这里
                # 知道文件真正落在哪个目录。
                root=target.workspace_root,
                resource=str(target.path),
                action=request.action,
                intent_seq=intent_seq,
                blob_ref=self._snapshot_blob(request.run_id, request.call_id),
            )

        if receipt is not None:
            batch.append(
                {
                    "run_id": request.run_id,
                    "type": "receipt",
                    "payload": receipt.to_payload(),
                    "task_id": request.task_id,
                    "actor_kind": "core",
                    "idem_key": receipt.receipt_id,
                }
            )

        if is_memory:
            assert projection is not None
            with self._log.transaction():
                self._log.append_many(batch)
                if result.status == "succeeded" and result.effect_state == "applied":
                    # seq 用 intent_seq：契约要求投影能指回这次写入的意图；
                    # 传 None 或另取一个号都会制造第二份因果。
                    projection.apply_event(
                        "tool.result", result_event["payload"], seq=intent_seq
                    )
                self._conn.execute(
                    "UPDATE dispatches SET state=?, updated_at=? WHERE dispatch_id=?",
                    (DISPATCH_FINISHED, self._clock(), dispatch_id),
                )
                self._set_request_state(request.request_id, STATE_DONE)
            return

        # 文件路径保持原有事务边界：结果+凭据一次提交，派发状态随后推进。
        self._log.append_many(batch)
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=? WHERE dispatch_id=?",
                (DISPATCH_FINISHED, self._clock(), dispatch_id),
            )
            self._set_request_state(request.request_id, STATE_DONE)

    def _snapshot_blob(self, run_id: str, call_id: str) -> str | None:
        """这次调用的写前快照 blob。

        凭据必须指向**同一条**证据：快照记录写前内容在哪里，凭据不能只有
        "我之前是什么"却指不回任何东西。
        """
        if self._snapshots is None:
            return None
        change = self._snapshots.find(run_id, call_id)
        return change.blob_ref if change is not None else None

    def _require_data_root(self) -> Path:
        if self._data_root is None:
            raise TianshuError(
                "未配置 data_root：派发与撤销必须共用同一把执行守卫"
            )
        return Path(self._data_root)


# ---------------------------------------------------------------- 请求构造


def build_request(
    *,
    task: Task,
    run: Run,
    claim: JobClaim,
    call_id: str,
    tool_id: str,
    target: Path | str,
    args: dict[str, Any],
    request_id: str | None = None,
    attempt_id: int = 1,
    preconditions: dict[str, Any] | None = None,
) -> ActionRequest:
    """从"我要做什么"生成一份可派发的请求。

    `preconditions` 在这里**当场从磁盘读**，不是调用方说了算：
    审批绑定的必须是真实写前状态，否则"批准时改成 false"就能绕过覆盖审批。

    记忆目标不是路径：`target` 可以是 `memory://<scope>/<scope_id>`。这条分支
    不做 `Path.resolve()`，也不读文件哈希；写前状态是 memory_id + revision，
    由参数带进 `preconditions`（执行侧会从权威投影再读一次核对）。显式传入
    `preconditions` 只对记忆分支生效——文件分支的写前状态仍然由这里读盘决定。
    """
    spec = SPECS.get(tool_id)
    if spec is None:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")

    raw_target = str(target)
    if raw_target.startswith("memory://"):
        if tool_id not in MEMORY_ALL_TOOL_IDS:
            raise ToolNotAllowed(f"文件工具不能使用记忆资源：{tool_id} → {raw_target}")
        try:
            scope = parse_memory_uri(raw_target)
        except ResourceError as exc:
            raise ToolArgumentError(f"记忆资源引用不合法：{exc}") from exc
        if spec.action == ACTION_CREATE:
            memory_pre: dict[str, Any] = {
                "memory_kind": args.get("kind"),
                "memory_exists": False,
            }
        elif spec.action == ACTION_OVERWRITE:
            # 覆盖类记忆工具不止 confirm 一个（edit / promote 也在这里），目标
            # 可能是 candidate 也可能是 active——**不能**写死 "candidate"。
            # 状态由调用方声明（`status` 可选）；真正的状态约束由投影的迁移
            # 校验（`_apply_confirm` / `_apply_edit`）兜底。
            memory_pre = {
                "memory_id": args.get("memory_id"),
                "before_status": args.get("status"),
                "before_revision": args.get("revision"),
            }
        elif spec.action == ACTION_DELETE:
            memory_pre = {
                "memory_id": args.get("memory_id"),
                # 删除类目标可以是 candidate / active / superseded / forgotten：
                # 不声明状态就只按 revision 绑定；状态约束由执行侧与投影校验兜底。
                "before_status": args.get("status"),
                "before_revision": args.get("revision"),
            }
        else:  # pragma: no cover - 记忆读工具不走派发
            memory_pre = {}
        if preconditions is not None:
            memory_pre = dict(preconditions)
        return ActionRequest(
            request_id=request_id or f"req_{uuid.uuid4().hex[:12]}",
            task_id=task.task_id,
            run_id=run.run_id,
            job_id=claim.job_id,
            claim_epoch=claim.claim_epoch,
            owner_instance_id=claim.owner_instance_id,
            goal_revision=run.goal_revision,
            lease_epoch=run.lease_epoch,
            call_id=call_id,
            attempt_id=attempt_id,
            tool_id=tool_id,
            tool_version=spec.version,
            schema_hash=sha256_hex(canonical_bytes(spec.input_schema)),
            description_hash=spec.description_digest(),
            action=spec.action,
            resources=(scope.to_uri(),),
            args_hash=sha256_hex(canonical_bytes(args)),
            preconditions=memory_pre,
        )

    if tool_id in MEMORY_ALL_TOOL_IDS:
        raise ToolNotAllowed(f"记忆工具必须使用 memory:// 资源：{raw_target}")

    target = Path(target).resolve()
    io = current_backend()
    if spec.action == ACTION_CREATE:
        file_preconditions: dict[str, Any] = {"target_exists": io.exists(target)}
    elif spec.action in (ACTION_OVERWRITE, ACTION_DELETE):
        file_preconditions = {"before_state": observe_state(target, backend=io)}
    elif spec.action == ACTION_EXEC:
        # 这个标记进 actions 摘要：任务有没有开启 exec 策略，必须由审批绑住。
        file_preconditions = {"exec_configured": task.exec_policy is not None}
    else:  # pragma: no cover - 只读动作不走派发
        file_preconditions = {}

    return ActionRequest(
        request_id=request_id or f"req_{uuid.uuid4().hex[:12]}",
        task_id=task.task_id,
        run_id=run.run_id,
        job_id=claim.job_id,
        claim_epoch=claim.claim_epoch,
        owner_instance_id=claim.owner_instance_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        call_id=call_id,
        attempt_id=attempt_id,
        tool_id=tool_id,
        tool_version=spec.version,
        schema_hash=sha256_hex(canonical_bytes(spec.input_schema)),
        description_hash=spec.description_digest(),
        action=spec.action,
        resources=(str(target),),
        args_hash=sha256_hex(canonical_bytes(args)),
        preconditions=file_preconditions,
    )


def _row_to_dispatch(row: sqlite3.Row) -> DispatchRecord:
    return DispatchRecord(
        dispatch_id=row["dispatch_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        call_id=row["call_id"],
        request_id=row["request_id"],
        request_digest=row["request_digest"],
        tool_id=row["tool_id"],
        action=row["action"],
        state=row["state"],
        claim_epoch=row["claim_epoch"],
        owner_instance_id=row["owner_instance_id"],
        attempt_id=row["attempt_id"],
        approval_id=row["approval_id"],
        grant_id=row["grant_id"],
    )


__all__ = [
    "APPROVAL_REJECTED",
    "APPROVAL_REJECTED_REASON",
    "ControlRun",
    "DISPATCH_ADMITTED",
    "DISPATCH_INTENT",
    "DISPATCH_REJECTED",
    "DISPATCH_REQUESTED",
    "DISPATCH_REVOKED",
    "DispatchDecision",
    "DispatchDenied",
    "DispatchError",
    "Dispatcher",
    "ExecutionGuard",
    "KIND_ADMITTED",
    "KIND_REJECTED",
    "KIND_WAITING",
    "PolicyDecision",
    "STATE_ADMITTED",
    "STATE_DONE",
    "STATE_REJECTED",
    "STATE_WAITING",
    "StoredRequest",
    "build_request",
    "is_ancestor_or_self",
]