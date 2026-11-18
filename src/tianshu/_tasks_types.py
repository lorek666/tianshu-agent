"""Task / Run / Subtask 常量、值对象、建表 Schema 与行映射（内部子模块）。

本模块承载 `tasks.py` 的纯数据类型、封闭常量集、SQLite 投影建表语句与行映射函数；
所有公开与测试依赖的符号均由 `tianshu.tasks` 门面统一重导出，零契约变更。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError
from .execpolicy import ExecPolicy
from .workspace import WorkspaceBinding, parse_bindings

#: MCP server 名形状：与 `resources.McpTarget` 的资源段**逐字一致**（两处各写
#: 一份是刻意的：tasks 不该为拿一个正则去 import 解析器；但改一处必须改另一处，
#: `tests/test_mcp_dispatch.py` 有交叉断言）。
_MCP_SERVER_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    goal          TEXT NOT NULL,
    goal_revision INTEGER NOT NULL DEFAULT 1,
    lease_epoch   INTEGER NOT NULL DEFAULT 1,
    active_run_id TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    workspaces    TEXT NOT NULL DEFAULT '[]',
    exec_policy   TEXT NOT NULL DEFAULT '',
    is_control    INTEGER NOT NULL DEFAULT 0,
    memory_scopes TEXT NOT NULL DEFAULT '[]',
    outbound_allow TEXT NOT NULL DEFAULT '[]',
    mcp_servers   TEXT NOT NULL DEFAULT '[]',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    goal_revision INTEGER NOT NULL,
    lease_epoch   INTEGER NOT NULL,
    state         TEXT NOT NULL DEFAULT 'created',
    outcome       TEXT,
    effect_state  TEXT,
    control_requested TEXT,
    created_at    INTEGER NOT NULL,
    ended_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, created_at);

CREATE TABLE IF NOT EXISTS run_cursors (
    run_id        TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    goal_revision INTEGER NOT NULL,
    lease_epoch   INTEGER NOT NULL,
    claim_epoch   INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_turns (
    run_id     TEXT NOT NULL,
    turn_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, turn_id)
);

-- 子任务（G2 委派 D1）：一行 = 一次委派。与事件库同一个 SQLite，
-- 和事件同生共死——父 Run 说"派了"而这张表里没有行，等于在撒谎。
-- 主键是 (parent_run_id, subtask_id)：不同父 Run 可以各有一个同名 slug，
-- 同一父 Run 里重复 spawn 同一 slug 是拒绝（dispatch.subtask_exists）。
CREATE TABLE IF NOT EXISTS subtasks (
    subtask_id      TEXT NOT NULL,
    parent_task_id  TEXT NOT NULL,
    parent_run_id   TEXT NOT NULL,
    child_task_id   TEXT NOT NULL,
    child_run_id    TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    goal            TEXT NOT NULL,
    limits          TEXT NOT NULL,
    state           TEXT NOT NULL,
    -- G2 委派 D2：spawn 时抄下父 Run 的 (goal_revision, lease_epoch)。
    -- 子 Run 每次派发前 / 收尾时核验父任务当前版本是否仍是这两个值：
    -- 不一致 = 父目标已改，子任务被 fence，结果不能回填父 Run。
    parent_goal_revision INTEGER NOT NULL DEFAULT 0,
    parent_lease_epoch   INTEGER NOT NULL DEFAULT 0,
    result_ref      TEXT,
    summary         TEXT,
    error           TEXT,
    created_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    PRIMARY KEY (parent_run_id, subtask_id)
);

CREATE INDEX IF NOT EXISTS idx_subtasks_parent_task ON subtasks(parent_task_id);
"""

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
RUN_OUTCOMES = TERMINAL_STATES
EFFECT_STATES = {"none_applied", "applied", "partial", "unknown"}

# 非终态也必须落库。把 PAUSED / WAITING_APPROVAL / RECONCILING 都塞进
# "还没成功"这一句话里，用户就分不清"停下了可以继续"和"正在执行"，
# CLI/UI 也报不出真实状态。这些名字与 PRD §4.3 的状态机一一对应。
RUN_CREATED = "created"
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_WAITING_APPROVAL = "waiting_approval"
RUN_RECONCILING = "reconciling"
RUN_PAUSED = "paused"
RUN_CANCELLING = "cancelling"
RUN_FENCED = "fenced"

NON_TERMINAL_STATES = {
    RUN_CREATED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_WAITING_APPROVAL,
    RUN_RECONCILING,
    RUN_PAUSED,
    RUN_CANCELLING,
}
RUN_STATES = NON_TERMINAL_STATES | TERMINAL_STATES | {RUN_FENCED}

# 控制请求与生效状态是两件事：RUNNING 时只记请求，执行器在安全点确认。
CONTROL_PAUSE = "pause"
CONTROL_CANCEL = "cancel"
CONTROL_REQUESTS = {CONTROL_PAUSE, CONTROL_CANCEL}

# 取消的统一说明。**只保证不再派发新的副作用**，不撤回已经发生的修改；
# 这句同时写进 run.end 的 error 与 CLI 输出，两边不能各说一套。
CANCEL_REASON = "取消：已停止派发新的副作用（已发生的修改不撤回）"

# 这些状态里 Run 已经停下或正在停，不能直接当"执行完了"来收尾。
# `created` / `queued` 例外：从没跑过的 Run 被判定为"无事可做"是合法的。
STOPPED_STATES = {RUN_PAUSED, RUN_WAITING_APPROVAL, RUN_RECONCILING, RUN_CANCELLING}

TASK_CREATED = "task.created"
TASK_REVISED = "task.revised"
EXECUTION_STARTED = "execution.started"
RUN_CURSOR_SAVED = "run.cursor"
RUN_MODEL_TURN = "run.model_turn"
RUN_START = "run.start"
RUN_FINISH = "run.finish"
# 事件类型必须与状态常量分开命名：`RUN_FENCED` 是状态 `fenced`，
# 这个模块里原先又写了一个同名常量 `RUN_FENCED = "run.fenced"`，
# 后者把前者覆盖掉，于是**所有** `run.state == RUN_FENCED` 判断恒为 False
# （暂停/取消/fence 收尾全部走错分支，作业还可能被永久挂起）。
RUN_FENCED_EVENT = "run.fenced"
RUN_STATE = "run.state"
RUN_END = "run.end"
RUN_CONTROL = "run.control"
RUN_EFFECT = "run.effect"

# Task 级事件不属于某个 Run；用空 run_id 把它们与 run 作用域事件分开。
TASK_SCOPE = ""

# ---- 子任务（G2 委派 D1） ----
#
# 任务级声明 `max_subtasks` 是**能力声明**：0 = 这个任务不能委派（与
# `outbound_allow` 空 = 一条包都不发同一条纪律）。上限是固定值，不做配置化扩容：
# 每父 Run 最多 2 个未完成、每根 Task 累计最多 4 个、深度固定 1。
SUBTASK_MAX_UNFINISHED_PER_RUN = 2
SUBTASK_MAX_PER_ROOT_TASK = 4
SUBTASK_MAX_DEPTH = 1
SUBTASK_TURNS_RANGE = (1, 8)
SUBTASK_TOKENS_RANGE = (256, 8000)
SUBTASK_DEFAULT_MAX_TURNS = 8
SUBTASK_DEFAULT_MAX_TOKENS = 2000
#: 子任务 deadline 固定 300 秒，**不可由模型抬高**（D1 §2.4）。
SUBTASK_DEADLINE_MS = 300_000
#: 子任务目标的有界摘要（D1 §2.1：`goal` ≤ 2 KiB）。
SUBTASK_GOAL_MAX_BYTES = 2048

SUBTASK_QUEUED = "queued"
SUBTASK_RUNNING = "running"
#: D1 只记录**取消请求**：真正停下在 D2。写成 `cancelled` 是在假装撤销已完成。
SUBTASK_CANCELLING = "cancelling"
SUBTASK_SUCCEEDED = "succeeded"
SUBTASK_FAILED = "failed"
SUBTASK_CANCELLED = "cancelled"
SUBTASK_FENCED = "fenced"

#: 规格 §2.1 的 DDL 注释列了 6 个状态，§2.6 又要求取消先落 `cancelling`。
#: 两个口径合起来才是完整封闭集（这一点写进 PR body 对账）。
SUBTASK_STATES = frozenset(
    {
        SUBTASK_QUEUED,
        SUBTASK_RUNNING,
        SUBTASK_CANCELLING,
        SUBTASK_SUCCEEDED,
        SUBTASK_FAILED,
        SUBTASK_CANCELLED,
        SUBTASK_FENCED,
    }
)
#: "未完成"：还占着并发额度、父 Run 必须等它。终态 + fenced 都不算。
SUBTASK_UNFINISHED_STATES = frozenset(
    {SUBTASK_QUEUED, SUBTASK_RUNNING, SUBTASK_CANCELLING}
)
SUBTASK_TERMINAL_STATES = frozenset(
    {SUBTASK_SUCCEEDED, SUBTASK_FAILED, SUBTASK_CANCELLED, SUBTASK_FENCED}
)

#: 子任务结果摘要上限（规格 §2.5：≤4 KiB 内联 + 全文引用）。
SUBTASK_SUMMARY_MAX_BYTES = 4096


def subtask_budget_attempt(parent_run_id: str, subtask_id: str) -> str:
    """子任务在父账户 `subtasks` 维度上的**预占身份**。

    身份必须可重算：D1 只在 spawn 时预占，D2 收尾时要结算同一张预占。把
    `(父 Run, slug)` 拼成稳定字符串，就不必在 `subtasks` 行里再存一个预算 id
    （规格 §2.1 的 DDL 没有这一列）。`BudgetLedger.reserve()` 对同一
    `(account_id, attempt_id)` 幂等，重算不会重复扣。
    """
    return f"subtask:{parent_run_id}:{subtask_id}"


def clean_subtask_limits(raw: dict[str, Any] | None) -> dict[str, int]:
    """子任务局部上限：补默认值、**越界拒绝**（绝不静默夹到上限）。

    `deadline_ms` 是固定值，不接受调用方抬高：传进来也和
    `SUBTASK_DEADLINE_MS` 不同就拒绝。返回值**总是**含三项（含固定的 deadline），
    因为 `subtasks.limits` 与 `subtask.spawned` 的契约要求三项齐全；派发层据此
    在 spawn 那一刻把它换算成账户的绝对 deadline（`now + 300s`）。
    """
    raw = dict(raw or {})
    unknown = sorted(set(raw) - {"max_turns", "max_tokens", "deadline_ms"})
    if unknown:
        raise TianshuError(f"未知的子任务上限字段：{unknown}")
    if "deadline_ms" in raw and raw["deadline_ms"] != SUBTASK_DEADLINE_MS:
        raise TianshuError(
            f"deadline_ms 固定 {SUBTASK_DEADLINE_MS}，不能由模型抬高："
            f"{raw['deadline_ms']}"
        )
    turns = raw.get("max_turns", SUBTASK_DEFAULT_MAX_TURNS)
    tokens = raw.get("max_tokens", SUBTASK_DEFAULT_MAX_TOKENS)
    for name, value in (("max_turns", turns), ("max_tokens", tokens)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TianshuError(f"{name} 必须是整数：{value!r}")
    low, high = SUBTASK_TURNS_RANGE
    if not low <= turns <= high:
        raise TianshuError(
            f"max_turns 必须在 {low}–{high} 之间：{turns}（超过上限就拒绝，不夹取）"
        )
    low, high = SUBTASK_TOKENS_RANGE
    if not low <= tokens <= high:
        raise TianshuError(
            f"max_tokens 必须在 {low}–{high} 之间：{tokens}（超过上限就拒绝，不夹取）"
        )
    return {
        "max_turns": turns,
        "max_tokens": tokens,
        "deadline_ms": SUBTASK_DEADLINE_MS,
    }


def truncate_utf8(text: str, limit: int = SUBTASK_SUMMARY_MAX_BYTES) -> str:
    """按 **UTF-8 字节**截断，且不切出半个字符。

    为什么不是 `text[:limit]`：那是按字符数截的，一个中文占 3 字节，4 KiB
    的预算会被读成 4096 个汉字（12 KiB）——"有界"就只是句空话。反过来直接
    `text.encode()[:limit].decode()` 又会在多字节字符中间切断，抛
    `UnicodeDecodeError` 或产生替换字符。所以按字节切完再 `errors="ignore"`
    丢掉末尾半个码点。
    """
    if len(text.encode("utf-8")) <= limit:
        return text
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _ensure_column(
    conn: sqlite3.Connection, table: str, name: str, ddl: str
) -> None:
    """给早期版本的库补列。

    投影表是 `CREATE TABLE IF NOT EXISTS` 建的，已存在的库不会被改写，
    所以新列必须显式补齐，否则老库读 `control_requested` 直接报错。
    """
    have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _now() -> int:
    return int(time.time() * 1000)


@dataclass
class Task:
    task_id: str
    goal: str
    goal_revision: int
    lease_epoch: int
    active_run_id: str | None
    status: str
    workspaces: list[WorkspaceBinding] = field(default_factory=list)
    #: 任务级执行策略（F2）。None = 未配置 = run_command 默认全拒。
    #: 开启 exec 是显式动作：`task create --allow-exec-cmd`。
    exec_policy: ExecPolicy | None = None
    #: 控制面 Run（CLI / UI 直接发起）：只有它能用控制面专属工具与记忆作用域。
    is_control: bool = False
    #: 控制面声明的记忆作用域（`memory://task/...` 等 URI）；模型不可设置。
    memory_scopes: tuple[str, ...] = ()
    #: 任务级外发白名单（PRD §5.2.3 / G2）：允许出网的**源**，形如
    #: `network://https/example.com`。空 = 这个任务一条包都不发。
    #:
    #: 与 `exec_policy` 同一条理由，创建时定死、`task revise` 不可改：
    #: 扩权必须是新任务，不能让旧 Run 已经批准的请求跟着悄悄扩大范围。
    outbound_allow: tuple[str, ...] = ()
    #: 任务级 MCP server 白名单（G2 MCP 阶段）：允许这个任务使用的 server 名。
    #: 空 = 一个都不许。与 `outbound_allow` 同形：创建时定死、`task revise` 不可改。
    mcp_servers: tuple[str, ...] = ()
    #: 父链（G2 委派 D1）。根任务三项都是 None、depth=0；
    #: 子任务的 depth 恒为 1，并记住是谁派的（事件与投影都要能回答）。
    parent_task_id: str | None = None
    parent_run_id: str | None = None
    subtask_id: str | None = None
    depth: int = 0
    #: 任务级委派能力声明：0 = 不能委派（**未声明即无委派能力**）。
    #: 与 `outbound_allow` 同一条纪律：扩权必须是新任务，运行中不可改。
    max_subtasks: int = 0
    created_at: int = 0
    updated_at: int = 0


@dataclass
class Run:
    run_id: str
    task_id: str
    goal_revision: int
    lease_epoch: int
    state: str
    outcome: str | None = None
    effect_state: str | None = None
    control_requested: str | None = None
    #: 子 Run 的父链（G2 委派 D1）：父 Run + 父作用域内的 slug。
    parent_run_id: str | None = None
    subtask_id: str | None = None
    created_at: int = 0
    ended_at: int | None = None


@dataclass
class Subtask:
    """一次委派（`subtasks` 表的一行）。

    `state` 是**子任务**的状态机（queued/running/cancelling/succeeded/failed/
    cancelled/fenced），不是子 Run 的状态机：两者都给出，不互相冒充。
    """

    subtask_id: str
    parent_task_id: str
    parent_run_id: str
    child_task_id: str
    child_run_id: str
    job_id: str
    goal: str
    limits: dict[str, int]
    state: str
    #: spawn 时父 Run 绑定的版本（D2 fence 的对照值）。
    parent_goal_revision: int = 0
    parent_lease_epoch: int = 0
    result_ref: str | None = None
    summary: str | None = None
    error: str | None = None
    created_at: int = 0
    ended_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "child_task_id": self.child_task_id,
            "child_run_id": self.child_run_id,
            "job_id": self.job_id,
            "goal": self.goal,
            "limits": dict(self.limits),
            "state": self.state,
            "parent_goal_revision": self.parent_goal_revision,
            "parent_lease_epoch": self.parent_lease_epoch,
            "result_ref": self.result_ref,
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
        }


@dataclass
class RunCursor:
    """Run 的持久游标：重启后从这里继续，而不是重新问模型"刚才做到哪"。

    字段对应计划 §2.4：已提交的消息、尚未执行完的工具提案、已完成的调用、
    预算账本、绑定过的审批，以及这份游标签发时的 revision/epoch。
    最后两项不是装饰：resume 时必须能判断"这份游标还是不是当前目标的"。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    #: 与 `messages` 平行的 seq 归因（F5 上下文编译的溯源材料）。
    #:
    #: 带默认值不是风格问题：F5 之前写下的游标没有这个键，`RunCursor.of` 用
    #: `known = {... if k in cls.__dataclass_fields__}` 过滤，缺键即空列表——
    #: 旧游标必须能原样反序列化，否则一次升级就让所有暂停中的 Run 打不开。
    message_seqs: list[int] = field(default_factory=list)
    pending_proposals: list[dict[str, Any]] = field(default_factory=list)
    completed_call_ids: list[str] = field(default_factory=list)
    approval_ids: list[str] = field(default_factory=list)
    model_turn: dict[str, Any] | None = None
    budget_ledger: dict[str, Any] = field(default_factory=dict)
    resource_epochs: dict[str, int] = field(default_factory=dict)
    output_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "message_seqs": self.message_seqs,
            "pending_proposals": self.pending_proposals,
            "completed_call_ids": self.completed_call_ids,
            "approval_ids": self.approval_ids,
            "model_turn": self.model_turn,
            "budget_ledger": self.budget_ledger,
            "resource_epochs": self.resource_epochs,
            "output_ref": self.output_ref,
        }

    @classmethod
    def of(cls, raw: dict[str, Any] | None) -> RunCursor:
        raw = raw or {}
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True)
class ExecutionPermit:
    """"马上就改文件"的线性化证据。

    只在 `begin_execution` 的 with 块内有效，块结束即失效。拿到它就意味着：
    在**同一把执行守卫**的保护下，Run 没被 fence、租约还在本实例手上，
    并且 `execution.started` 已经落库。
    """

    run_id: str
    task_id: str
    goal_revision: int
    lease_epoch: int
    resources: tuple[str, ...]
    seq: int
    started_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "goal_revision": self.goal_revision,
            "lease_epoch": self.lease_epoch,
            "resources": list(self.resources),
            "seq": self.seq,
            "started_at": self.started_at,
        }


def row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        goal=row["goal"],
        goal_revision=row["goal_revision"],
        lease_epoch=row["lease_epoch"],
        active_run_id=row["active_run_id"],
        status=row["status"],
        workspaces=parse_bindings(json.loads(row["workspaces"])),
        exec_policy=ExecPolicy.from_dict(
            json.loads(row["exec_policy"]) if row["exec_policy"] else None
        ),
        is_control=bool(row["is_control"]),
        memory_scopes=tuple(json.loads(row["memory_scopes"] or "[]")),
        outbound_allow=tuple(json.loads(row["outbound_allow"] or "[]")),
        mcp_servers=tuple(json.loads(row["mcp_servers"] or "[]")),
        parent_task_id=row["parent_task_id"],
        parent_run_id=row["parent_run_id"],
        subtask_id=row["subtask_id"],
        depth=int(row["depth"] or 0),
        max_subtasks=int(row["max_subtasks"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_run(row: sqlite3.Row) -> Run:
    keys = row.keys()
    return Run(
        run_id=row["run_id"],
        task_id=row["task_id"],
        goal_revision=row["goal_revision"],
        lease_epoch=row["lease_epoch"],
        state=row["state"],
        outcome=row["outcome"] if "outcome" in keys else None,
        effect_state=row["effect_state"] if "effect_state" in keys else None,
        control_requested=(
            row["control_requested"] if "control_requested" in keys else None
        ),
        parent_run_id=(
            row["parent_run_id"] if "parent_run_id" in keys else None
        ),
        subtask_id=row["subtask_id"] if "subtask_id" in keys else None,
        created_at=row["created_at"],
        ended_at=row["ended_at"] if "ended_at" in keys else None,
    )


def row_to_subtask(row: sqlite3.Row) -> Subtask:
    limits = json.loads(row["limits"] or "{}")
    return Subtask(
        subtask_id=row["subtask_id"],
        parent_task_id=row["parent_task_id"],
        parent_run_id=row["parent_run_id"],
        child_task_id=row["child_task_id"],
        child_run_id=row["child_run_id"],
        job_id=row["job_id"],
        goal=row["goal"],
        limits={k: int(v) for k, v in limits.items()},
        state=row["state"],
        parent_goal_revision=int(row["parent_goal_revision"] or 0),
        parent_lease_epoch=int(row["parent_lease_epoch"] or 0),
        result_ref=row["result_ref"],
        summary=row["summary"],
        error=row["error"],
        created_at=row["created_at"],
        ended_at=row["ended_at"],
    )
