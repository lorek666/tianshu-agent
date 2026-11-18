"""派发层常量、异常、决策/目标值对象与前置条件辅助（内部子模块）。

承载 `dispatcher.py` 的封闭常量集、SQLite 建表 Schema、异常与值对象以及前置条件规则映射；
全部公开及测试依赖符号由 `tianshu.dispatcher` 统一重导出，零契约变更。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .events import sha256_hex
from .jobs import JobClaim
from .permissions import ACTION_CREATE, ActionRequest
from .resources import (
    KIND_FILE,
    KIND_MCP,
    KIND_MEMORY,
    KIND_NETWORK,
    KIND_SUBTASK,
    MemoryScope,
    NetworkTarget,
    ResourceError,
    ResourceRef,
    is_memory_resource,
    parse_resource,
)
from .sandbox import FileBackend, current_backend
from .tasks import Run, Task
from .tools import DispatchRecord
from .workspace import ResourceIdentity, WorkspaceBinding

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
    -- 这一行是不是由"发包前提交外发边界标记"的版本写的（0=升级前的旧行）。
    -- 旧版本在请求写出前**不推进派发行**，所以旧行的 `intent`/`revoked` 不能
    -- 证明"请求没发出去"；恢复必须靠这个标记把旧行排除在 `none_applied` 之外。
    boundary_protocol INTEGER NOT NULL DEFAULT 0,
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


#: 有**执行器**的资源种类。网络在 G2 网络阶段补上、`mcp` 在 G2 MCP 阶段补上
#: （stdio 客户端；恒需人工批准 + 任务级 server 白名单）、委派在 G2 子代理 D1
#: 补上（只排队，不执行子 Run）。五种资源种类现在都有执行器。
EXECUTABLE_KINDS: frozenset[str] = frozenset(
    {KIND_FILE, KIND_MEMORY, KIND_NETWORK, KIND_MCP, KIND_SUBTASK}
)


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


@dataclass(frozen=True)
class _NetworkTarget:
    """网络目标：没有文件路径、没有目录租约、也没有本地副作用。

    `rel_path` / `workspace_root` 在派发表里是历史列，网络行留空；任何文件
    执行器都不会拿到这个对象（`dispatch()` 的网络分支独立于文件分支）。
    """

    uri: str
    target: NetworkTarget
    kind: str = KIND_NETWORK


@dataclass(frozen=True)
class _McpTarget:
    """MCP 目标：没有文件路径、没有目录租约，也没有可回滚的本地写前状态。

    `rel_path` / `workspace_root` 在派发表里是历史列，MCP 行留空；任何文件执行器
    都不会拿到这个对象（`dispatch()` 的 MCP 分支独立于文件分支）。
    """

    uri: str
    server: str
    tool: str
    tool_id: str
    kind: str = KIND_MCP


@dataclass(frozen=True)
class _SubtaskTarget:
    """子任务目标：父 Run 作用域内的一个 slug，没有路径、没有目录租约。

    零目录任务同样合法：委派与"在哪个目录里工作"无关。子任务自己的绑定是父
    任务绑定的只读改写，由 `TaskStore.create_subtask()` 在创建时定死。
    """

    uri: str
    subtask_id: str
    kind: str = KIND_SUBTASK


_Target = (
    _FileTarget | _MemoryTarget | _NetworkTarget | _McpTarget | _SubtaskTarget
)


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
