"""Task / Run / 游标（M2 · PR A Task 1 + PR C Task 5）。

一件用户的事 = 一个 Task；一次执行 = 一个 Run。一个 Task 可以有多个 Run，
失败重试就是新开一个 Run，历史不覆盖。

核心是 **fencing**：用户中途改主意时，旧 Run 必须立刻失效，不能拿着旧目标
继续改文件。
- `goal_revision`：目标改一次加一
- `lease_epoch`：目标或工作区绑定变更加一

Run 绑定创建时的这两个值；任一与 Task 当前值不符，即为被 fence。

Run 还有第二个维度：**状态**。非终态（`queued` / `running` /
`waiting_approval` / `reconciling` / `paused` / `cancelling`）都要落库，
否则"停下来了可以继续"和"正在执行"在数据里长得一样，UI 与 CLI 只能瞎猜。
用户的暂停/取消请求单独存在 `control_requested`：执行中的 Run 只记录请求，
执行器在**安全点**确认成状态，确认与清空请求是同一次提交。

**事实源是事件日志，不是这几张表。** tasks/runs 只是投影：每次状态变更都和
对应事件（`task.created` / `task.revised` / `run.start` / `run.state` /
`run.control` / `run.effect` / `run.cursor` / `run.finish` / `run.fenced`）
在同一个 SQLite 事务里提交，投影被删掉也能按事件重建。否则"用户改过目标"
这件事只存在于一张可被覆写的表里，回放无法解释某个 Run 为什么被 fence。

**锁序（全局唯一顺序）：执行守卫 → SQLite 事务。**

守卫是跨进程文件锁，SQLite 事务是跨进程写锁，两者都只有一把。如果某条路径
先拿事务再等守卫，就会和"先拿守卫再等事务"的另一条路径互相卡死。
所以凡是需要守卫的方法（`begin_execution` / `revise_task`）都显式拒绝在
已打开的事务里被调用，而不是靠调用方自觉。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import RunFenced, TianshuError
from .events import EventLog, canonical_bytes, sha256_hex
from .jobs import JobClaim, JobStore
from .leases import GUARD_TIMEOUT_MS, LeaseStore, execution_guard
from .workspace import ResourceIdentity, WorkspaceBinding, parse_bindings

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行期不导入
    from .permissions import ApprovalStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    goal          TEXT NOT NULL,
    goal_revision INTEGER NOT NULL DEFAULT 1,
    lease_epoch   INTEGER NOT NULL DEFAULT 1,
    active_run_id TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    workspaces    TEXT NOT NULL DEFAULT '[]',
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


@dataclass
class Task:
    task_id: str
    goal: str
    goal_revision: int
    lease_epoch: int
    active_run_id: str | None
    status: str
    workspaces: list[WorkspaceBinding] = field(default_factory=list)
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
    created_at: int = 0
    ended_at: int | None = None


@dataclass
class RunCursor:
    """Run 的持久游标：重启后从这里继续，而不是重新问模型"刚才做到哪"。

    字段对应计划 §2.4：已提交的消息、尚未执行完的工具提案、已完成的调用、
    预算账本、绑定过的审批，以及这份游标签发时的 revision/epoch。
    最后两项不是装饰：resume 时必须能判断"这份游标还是不是当前目标的"。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
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


def _now() -> int:
    return int(time.time() * 1000)


class TaskStore:
    def __init__(
        self,
        log: EventLog,
        *,
        leases: LeaseStore | None = None,
        claims: JobStore | None = None,
        approvals: ApprovalStore | None = None,
        data_root: Path | None = None,
        guard_timeout_ms: int = GUARD_TIMEOUT_MS,
    ) -> None:
        if log.in_transaction:
            # executescript 会先隐式 COMMIT 掉外层事务，构造 store 就等于把半份
            # 状态提交出去。只能在事务外构造。
            raise TianshuError("TaskStore 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        _ensure_column(self._conn, "runs", "control_requested", "TEXT")
        self._leases = leases
        self._claims = claims
        # 审批存储是可选的：fence 时要作废旧审批，但 TaskStore 不该因此依赖
        # 整个权限层。传进来就作废，没传就只做 fencing（授权那边仍然会自动
        # 失败——旧 epoch 的请求过不了 _authority）。
        self._approvals = approvals
        self._data_root = data_root
        self._guard_timeout_ms = guard_timeout_ms

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def leases(self) -> LeaseStore | None:
        return self._leases

    @property
    def claims(self) -> JobStore | None:
        return self._claims

    @property
    def data_root(self) -> Path | None:
        """authority 数据根：执行守卫文件放在这里，与工作区无关。"""
        return self._data_root

    @property
    def log(self) -> EventLog:
        return self._log

    def transaction(self):
        """与事件日志共用的可组合事务；已在事务中时复用外层事务。"""
        return self._log.transaction()

    def _guard(self, data_root: Path | None = None):
        """本机执行守卫。所有"会改变执行资格"的写操作都要走这里。

        必须成对：`begin_execution` 拿守卫确认"可以开始写"，`revise_goal` 拿
        同一把守卫改 revision/epoch。只让其中一方拿锁，就等于没锁——复核通过的
        下一刻目标被人改掉，写出去的仍然是旧目标的成果。
        """
        root = data_root or self._data_root
        if root is None:
            raise TianshuError(
                "未配置 data_root：无法串行化目标变更与执行开始（会留下 TOCTOU 窗口）"
            )
        if self._log.in_transaction:
            # 锁序必须是"守卫 → 事务"。已经在事务里再去等守卫，会和
            # "持有守卫、等事务"的另一条路径形成循环等待。
            raise TianshuError(
                "不能在已打开的事务中获取执行守卫（锁序必须是：守卫 → 事务）"
            )
        return execution_guard(root, timeout_ms=self._guard_timeout_ms)

    # ---- Task ----

    def create_task(
        self,
        goal: str,
        bindings: list[WorkspaceBinding | str | dict[str, Any]] | None = None,
    ) -> Task:
        goal = (goal or "").strip()
        if not goal:
            raise TianshuError("goal 不能为空")
        parsed = parse_bindings(
            [b.to_dict() if isinstance(b, WorkspaceBinding) else b
             for b in (bindings or [])]
        )
        now = _now()
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        with self._log.transaction() as tx:
            self._conn.execute(
                "INSERT INTO tasks (task_id, goal, goal_revision, lease_epoch,"
                " active_run_id, status, workspaces, created_at, updated_at)"
                " VALUES (?,?,1,1,NULL,'open',?,?,?)",
                (
                    task_id,
                    goal,
                    json.dumps([b.to_dict() for b in parsed], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            tx.append(
                TASK_SCOPE,
                TASK_CREATED,
                {
                    "task_id": task_id,
                    "goal": goal,
                    "goal_revision": 1,
                    "lease_epoch": 1,
                    "workspaces": [b.to_dict() for b in parsed],
                },
                task_id=task_id,
                actor_kind="human",
            )
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def revise_goal(self, task_id: str, new_goal: str) -> Task:
        """用户改主意。旧目标下的 Run 立即失效，历史保留。"""
        return self.revise_task(task_id, goal=new_goal)

    def revise_task(
        self,
        task_id: str,
        *,
        goal: str | None = None,
        bindings: list[WorkspaceBinding | str | dict[str, Any]] | None = None,
    ) -> Task:
        """改目标，或改工作区绑定，或两者一起。

        两件事都在**同一把执行守卫**里做，因为它们在同一个意义上改变"谁能写
        什么"：改目标让旧 Run 的 revision 过期，改绑定让旧 Run 的授权目录过期，
        任一先落地而另一件还在路上，就会出现"用旧绑定执行新目标"。
        绑定变更是**收窄/替换**，不是扩张：新绑定只对之后的新 Run 生效，
        旧 Run 已经失败在 fence 上，不存在"改个目录顺便续命"的通道。

        `keep_epoch` 之类的余地故意不留：改任何一项都让 `lease_epoch` +1，
        否则旧 Run 的租约和审批还能在同一个 epoch 下继续用。
        """
        if goal is None and bindings is None:
            raise TianshuError("revise_task 至少要改目标或改绑定之一")
        if goal is not None:
            goal = goal.strip()
            if not goal:
                raise TianshuError("goal 不能为空")
        parsed = None
        if bindings is not None:
            parsed = parse_bindings(
                [
                    b.to_dict() if isinstance(b, WorkspaceBinding) else b
                    for b in bindings
                ]
            )
        # 与 begin_execution 抢同一把守卫：要么改目标先完成（旧 Run 立刻 fence），
        # 要么执行已经开始（改目标排队到这次执行结束），不会出现"复核通过后目标被改"。
        with self._guard():
            with self._log.transaction() as tx:
                task = self.get_task(task_id)
                if task is None:
                    raise TianshuError(f"task 不存在：{task_id}")
                if task.status != "open":
                    raise TianshuError(f"task 已关闭，不能改目标：{task.status}")
                self._revise_locked(tx, task, goal, parsed)
        return self.get_task(task_id)  # type: ignore[return-value]

    def _revise_locked(
        self,
        tx,
        task: Task,
        new_goal: str | None,
        bindings: list[WorkspaceBinding] | None = None,
    ) -> None:
        now = _now()
        revision = task.goal_revision + 1
        epoch = task.lease_epoch + 1
        goal = new_goal if new_goal is not None else task.goal
        workspaces = bindings if bindings is not None else task.workspaces
        self._conn.execute(
            "UPDATE tasks SET goal=?, goal_revision=?,"
            " lease_epoch=?, active_run_id=NULL, workspaces=?, updated_at=?"
            " WHERE task_id=?",
            (
                goal,
                revision,
                epoch,
                json.dumps([b.to_dict() for b in workspaces], ensure_ascii=False),
                now,
                task.task_id,
            ),
        )
        tx.append(
            TASK_SCOPE,
            TASK_REVISED,
            {
                "task_id": task.task_id,
                "goal": goal,
                "goal_revision": revision,
                "lease_epoch": epoch,
                "workspaces": [b.to_dict() for b in workspaces],
            },
            task_id=task.task_id,
            actor_kind="human",
        )
        # 旧的活跃 Run 标记为 fenced（已终结的保持原状）
        if task.active_run_id:
            self._fence_run(tx, task.active_run_id, now, reason="goal_revised")

    def _fence_run(self, tx, run_id: str, now: int, *, reason: str) -> None:
        cur = self._conn.execute(
            "UPDATE runs SET state='fenced', control_requested=NULL, ended_at=?"
            " WHERE run_id=? AND state NOT IN ('succeeded','failed','cancelled','fenced')",
            (now, run_id),
        )
        if cur.rowcount == 0:
            return
        run = self.get_run(run_id)
        tx.append(
            run_id,
            RUN_FENCED_EVENT,
            {
                "run_id": run_id,
                "task_id": run.task_id if run else None,
                "reason": reason,
            },
            task_id=run.task_id if run else None,
        )
        # fence 触发时旧 Run 的审批立即作废：不能让一张旧 epoch 的批准在
        # 新目标下"暗中重新绑定"——人批的是旧目标的动作，不是新目标的。
        if self._approvals is not None:
            self._approvals.supersede_for_run(run_id, reason=f"run_fenced:{reason}")
        # fence 之后旧 Run 不再拥有工作区，租约必须交出来；
        # 否则同一 Task 的新 Run 会被上一个 Run 的残留租约挡住，直到 TTL 到期。
        if self._leases is not None:
            self._leases.release_run(run_id)
        # 作业也必须一起收掉。被 fence 的 Run 已经没有结论，不会有任何执行器
        # 再去推进它：`worker` 认领后立刻早退、`claim_next` 不认领 `waiting`、
        # `_finish_locked` 只在 outcome=cancelled 时收作业。留着这条作业的后果
        # 是它永远 `pending`/`waiting`——队列里一条永远轮不到的僵尸。
        if self._claims is not None:
            job = self._claims.job_for_run(run_id)
            if job is not None and job.state in ("pending", "claimed", "waiting"):
                self._claims.cancel(job.job_id, reason=f"run_fenced:{reason}")

    # ---- Run ----

    def start_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Run:
        """开一次执行。

        `payload` 是 `run.start` 事件的附加字段（目标文本、输出文件名、预算）。
        它必须进事件而不是只留在内存里：`replay` 要能仅凭日志重建当时的
        提示消息序列，否则"消息哈希对得上"这条验证就没有意义。
        """
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        with self._log.transaction() as tx:
            task = self.get_task(task_id)
            if task is None:
                raise TianshuError(f"task 不存在：{task_id}")
            if task.status != "open":
                raise TianshuError(f"task 已关闭：{task.status}")

            # 同一个 Task 同时只能有一个活跃 Run，否则两个 Run 会互相覆盖成果
            if task.active_run_id:
                active = self.get_run(task.active_run_id)
                if active is not None and active.state not in TERMINAL_STATES:
                    raise TianshuError(
                        f"task {task_id} 已有活跃 run {active.run_id}"
                        f"（state={active.state}），请先收尾或取消"
                    )

            now = _now()
            self._conn.execute(
                "INSERT INTO runs (run_id, task_id, goal_revision, lease_epoch,"
                " state, created_at) VALUES (?,?,?,?,'created',?)",
                (run_id, task_id, task.goal_revision, task.lease_epoch, now),
            )
            self._conn.execute(
                "UPDATE tasks SET active_run_id=?, updated_at=? WHERE task_id=?",
                (run_id, now, task_id),
            )
            tx.append(
                run_id,
                RUN_START,
                {
                    **(payload or {}),
                    "run_id": run_id,
                    "task_id": task_id,
                    "goal_revision": task.goal_revision,
                    "lease_epoch": task.lease_epoch,
                },
                task_id=task_id,
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    def run_payload(self, run_id: str) -> dict[str, Any]:
        """`run.start` 的原始字段（目标、输出文件名、预算）。"""
        row = self._conn.execute(
            "SELECT payload FROM events WHERE run_id=? AND type=? ORDER BY seq LIMIT 1",
            (run_id, RUN_START),
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else {}

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_run(row) if row else None

    def is_fenced(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        if run is None:
            return True
        task = self.get_task(run.task_id)
        if task is None:
            return True
        return (
            run.goal_revision != task.goal_revision
            or run.lease_epoch != task.lease_epoch
        )

    def assert_current(self, run_id: str) -> Run:
        """这个 Run 现在还有资格写入吗（只读判断，不留证据）。

        单用不够：读完到真正写之间目标可能被改。真正要写文件时必须改用
        `begin_execution`——它把同样的核验放进执行守卫，并留下
        `execution.started` 作为"已在执行"的线性化证据。
        """
        run = self.get_run(run_id)
        if run is None:
            raise RunFenced(f"run 不存在：{run_id}")
        if run.state in TERMINAL_STATES:
            raise RunFenced(f"run 已终结：{run_id}（state={run.state}）")
        if run.state == "fenced" or self.is_fenced(run_id):
            raise RunFenced(f"run 已被 fence：{run_id}")
        return run

    def assert_write_allowed(
        self, run_id: str, resources: list[ResourceIdentity] | None = None
    ) -> Run:
        """执行侧统一入口：Run 仍有效 + 资源租约仍在手。"""
        run = self.assert_current(run_id)
        if resources:
            if self._leases is None:
                raise TianshuError(
                    "未配置 LeaseStore：有写入资源的操作必须同时校验租约"
                )
            self._leases.assert_held(resources, run_id)
        return run

    @contextlib.contextmanager
    def begin_execution(
        self,
        data_root: Path | None,
        run_id: str,
        resources: list[ResourceIdentity | Path | str] | None = None,
    ) -> Iterator[ExecutionPermit]:
        """进入实际文件操作前必须经过这里。

        为什么不能只用 `assert_write_allowed`：那是"读一下再返回"，读完到真正
        打开文件之间，另一个进程可以改目标（fence 这个 Run）或接管租约。
        把"复核 + 登记 execution.started"放进**同一把执行守卫**，这个窗口才关得上。

        with 块内的代码才是在"已获准执行"状态下跑的；块一结束守卫就释放，
        所以不要把模型调用、人工等待塞进来（那会让所有实例跟着等）。

        只接受 `RUNNING`：`PAUSED` / `WAITING_APPROVAL` 的 Run 手里可能还拿着
        有效的租约和 claim（等待审批期间必须续租，否则每轮 TTL 都要重新审批），
        但它们**不该执行**。把这条判断放在执行入口，而不是指望调用方自觉。

        尚未包含审批/grant 核验——那是 PR-B 的派发层。这里覆盖的是
        revision/epoch/租约/状态四件事，缺一项就拒绝。
        """
        resources = list(resources or [])
        if not resources:
            # 有副作用的执行必须绑定到具体资源上，否则"没传资源"就等于
            # 绕过租约校验。纯对话任务不会走到这里。
            raise TianshuError("begin_execution 必须声明要写的资源（不能为空）")
        if self._leases is None:
            raise TianshuError("未配置 LeaseStore：begin_execution 需要校验租约")
        with self._guard(data_root):
            run = self.assert_current(run_id)
            if run.state != RUN_RUNNING:
                raise RunFenced(
                    f"run 不在执行状态，拒绝开始写入：{run_id}（state={run.state}）"
                )
            self._leases.assert_held(resources, run_id)
            if resources:
                # 守卫内续一次租：接下来的写入不该因为"复核花了几秒"而掉出 TTL
                self._leases.renew(resources, run_id)
            started_at = _now()
            with self._log.transaction() as tx:
                seq = tx.append(
                    run_id,
                    EXECUTION_STARTED,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "goal_revision": run.goal_revision,
                        "lease_epoch": run.lease_epoch,
                        "resources": [str(r) for r in resources],
                    },
                    task_id=run.task_id,
                )
            yield ExecutionPermit(
                run_id=run_id,
                task_id=run.task_id,
                goal_revision=run.goal_revision,
                lease_epoch=run.lease_epoch,
                resources=tuple(str(r) for r in resources),
                seq=seq,
                started_at=started_at,
            )

    # ---- 状态机与非终态落库（M2 · Task 5） ----

    def transition(
        self,
        run_id: str,
        to_state: str,
        *,
        claim: JobClaim | None = None,
        reason: str = "",
    ) -> Run:
        """把一个还没结束的 Run 推进到另一个**非终态**。

        终态必须走 `finish_run`：那里还要写 outcome/effect_state 两个维度，
        而且拒绝在被 fence 的 Run 上报成功。这里只挪状态，不许冒充结论。

        `claim` 传了就顺带核验写的人是不是当前 owner；状态机是 Run 的事实，
        旧 worker 醒过来改状态同样是事故。
        """
        if to_state not in NON_TERMINAL_STATES:
            raise TianshuError(
                f"transition 只接受非终态：{to_state}"
                f"（终态请用 finish_run；可选 {sorted(NON_TERMINAL_STATES)}）"
            )
        with self._log.transaction() as tx:
            if claim is not None:
                self._assert_writer(run_id, claim)
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                raise RunFenced(
                    f"run 已终结，不能改状态：{run_id}（state={run.state}）"
                )
            if run.state == to_state:
                return run
            cur = self._conn.execute(
                "UPDATE runs SET state=?"
                " WHERE run_id=? AND state=?"
                "   AND state NOT IN ('succeeded','failed','cancelled','fenced')",
                (to_state, run_id, run.state),
            )
            if cur.rowcount == 0:
                raise RunFenced(f"run 状态已被别的进程改掉：{run_id}")
            tx.append(
                run_id,
                RUN_STATE,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "from": run.state,
                    "to": to_state,
                    "reason": reason,
                    **({"claim_epoch": claim.claim_epoch} if claim else {}),
                },
                task_id=run.task_id,
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    def note_effect(
        self,
        run_id: str,
        *,
        effect_state: str,
        n_applied: int = 0,
        n_unknown: int = 0,
        claim: JobClaim | None = None,
    ) -> Run:
        """把"到目前为止到底改了没有"落库，中途也能查。

        没有这一步，一个停下来的 Run 只能回答"还没结束"，回答不了"改了什么"，
        而 PRD 要求 UI 必须能显示"已停止，但部分修改已生效"。
        这只是投影：真正的证据仍是每条 tool.result / receipt。
        """
        if effect_state not in EFFECT_STATES:
            raise TianshuError(f"未知 effect_state：{effect_state}")
        with self._log.transaction() as tx:
            if claim is not None:
                self._assert_writer(run_id, claim)
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            self._conn.execute(
                "UPDATE runs SET effect_state=? WHERE run_id=?",
                (effect_state, run_id),
            )
            tx.append(
                run_id,
                RUN_EFFECT,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "effect_state": effect_state,
                    "n_applied": n_applied,
                    "n_unknown": n_unknown,
                },
                task_id=run.task_id,
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    # ---- 用户控制：暂停 / 取消 ----

    # 这些状态里"没有副作用在飞"是结构性事实，不是猜测：
    # - created / queued：还没开始执行
    # - waiting_approval：正等人批准，派发还没发生
    # - paused：安全点已经过去，影响它的那次执行已经收尾
    # - reconciling：执行器已经退出、等人工核查，**没有驱动者在跑**
    # 因此请求可以就地生效。RUNNING / CANCELLING 里可能有有限副作用在执行，
    # 只记录请求，由执行器在下一个安全点确认。
    #
    # RECONCILING 必须在这里：它是主循环主动返回、然后就不再看这个 Run 的状态。
    # 不就地生效的话，用户按取消之后会一直等一个永远不会来的安全点。
    SAFE_CONTROL_STATES = (
        RUN_CREATED,
        RUN_QUEUED,
        RUN_WAITING_APPROVAL,
        RUN_RECONCILING,
        RUN_PAUSED,
    )

    def request_pause(self, run_id: str) -> Run:
        """请求暂停。返回的是"现在"的 Run，不承诺已经停下。

        调用方要读 `control_requested` 才知道请求是否还在排队：
        它是非空就说明执行器还没走到安全点。
        """
        safe = tuple(self.SAFE_CONTROL_STATES)
        marks = ",".join("?" * len(safe))
        with self._log.transaction() as tx:
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                raise RunFenced(
                    f"run 已终结，不能暂停：{run_id}（state={run.state}）"
                )
            cur = self._conn.execute(
                f"UPDATE runs SET state=?, control_requested=NULL"
                f" WHERE run_id=? AND state IN ({marks})",
                (RUN_PAUSED, run_id, *safe),
            )
            immediate = bool(cur.rowcount)
            queued = False
            if not immediate:
                # 排队中的取消不会被后来的暂停顶掉：取消是更强的决定。
                # 无条件写 'pause' 会让"取消"凭空消失——用户按了取消，
                # 看到的却是"已暂停"，然后 Run 永远停在那里。
                queued = bool(
                    self._conn.execute(
                        "UPDATE runs SET control_requested=?"
                        " WHERE run_id=?"
                        "   AND (control_requested IS NULL OR control_requested=?)",
                        (CONTROL_PAUSE, run_id, CONTROL_PAUSE),
                    ).rowcount
                )
            tx.append(
                run_id,
                RUN_CONTROL,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "action": CONTROL_PAUSE,
                    "applied_immediately": immediate,
                    # 已经有更强的请求在排队时，这次暂停不生效——
                    # 必须记下来，否则日志里会出现"用户暂停了但没停"且无法解释。
                    "request_effective": immediate or queued,
                    "stronger_request": None
                    if immediate or queued
                    else self._control_now(run_id),
                },
                task_id=run.task_id,
                actor_kind="human",
            )
            if immediate:
                tx.append(
                    run_id,
                    RUN_STATE,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "from": run.state,
                        "to": RUN_PAUSED,
                        "reason": "pause_requested",
                        "control_cleared": True,
                    },
                    task_id=run.task_id,
                )
                # 暂停就交还工作区：不然别人会以为这个目录还被占着。
                # resume 必须重新获取租约（拿不到就说明目录被别人用了）。
                if self._leases is not None:
                    self._leases.release_run(run_id)
        return self.get_run(run_id)  # type: ignore[return-value]

    def _control_now(self, run_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT control_requested FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["control_requested"] if row is not None else None

    def request_cancel(self, run_id: str) -> Run:
        """请求取消。安全状态下就地终结，否则留给执行器在安全点确认。

        "取消"只保证不再派发新的副作用，不撤销已经发生的那一次：
        已经 applied 的 effect_state 会如实留在 Run 上。
        """
        safe = tuple(self.SAFE_CONTROL_STATES)
        marks = ",".join("?" * len(safe))
        with self._log.transaction() as tx:
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                return run
            tx.append(
                run_id,
                RUN_CONTROL,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "action": CONTROL_CANCEL,
                    "applied_immediately": run.state in safe,
                    "effect_state": run.effect_state,
                },
                task_id=run.task_id,
                actor_kind="human",
            )
            cur = self._conn.execute(
                f"UPDATE runs SET state=?, control_requested=NULL"
                f" WHERE run_id=? AND state IN ({marks})",
                (RUN_CANCELLING, run_id, *safe),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "UPDATE runs SET control_requested=?"
                    " WHERE run_id=?",
                    (CONTROL_CANCEL, run_id),
                )
                return self.get_run(run_id)  # type: ignore[return-value]
            tx.append(
                run_id,
                RUN_STATE,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "from": run.state,
                    "to": RUN_CANCELLING,
                    "reason": "cancel_requested",
                },
                task_id=run.task_id,
            )
            self._finish_locked(
                tx,
                run_id,
                outcome="cancelled",
                effect_state=run.effect_state or "none_applied",
                reason=CANCEL_REASON,
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    def confirm_control(self, run_id: str, *, claim: JobClaim) -> str | None:
        """安全点：把排队中的控制请求变成状态并清掉请求。

        确认与清除必须是同一次提交。分两步的话，进程在中间被杀，恢复后会把
        同一条暂停请求再执行一遍——刚 resume 又立刻停下，用户看到的是
        "点了继续没反应"。

        返回 `pause` / `cancel` / `None`。`cancel` 已经就地收尾。
        """
        with self._log.transaction() as tx:
            self._assert_writer(run_id, claim)
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            action = run.control_requested
            if action is None or action not in CONTROL_REQUESTS:
                return None
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                # 请求还没确认，Run 就已经被别处结束了。清掉请求，别再执行它。
                self._conn.execute(
                    "UPDATE runs SET control_requested=NULL WHERE run_id=?", (run_id,)
                )
                return None
            if action == CONTROL_PAUSE:
                cur = self._conn.execute(
                    "UPDATE runs SET state=?, control_requested=NULL"
                    " WHERE run_id=? AND state=? AND control_requested=?",
                    (RUN_PAUSED, run_id, run.state, CONTROL_PAUSE),
                )
                if cur.rowcount == 0:
                    return None
                tx.append(
                    run_id,
                    RUN_STATE,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "from": run.state,
                        "to": RUN_PAUSED,
                        "reason": "pause_confirmed",
                        "claim_epoch": claim.claim_epoch,
                    "control_cleared": True,
                    },
                    task_id=run.task_id,
                )
                return CONTROL_PAUSE
            cur = self._conn.execute(
                "UPDATE runs SET state=?, control_requested=NULL"
                " WHERE run_id=? AND state=? AND control_requested=?",
                (RUN_CANCELLING, run_id, run.state, CONTROL_CANCEL),
            )
            if cur.rowcount == 0:
                return None
            tx.append(
                run_id,
                RUN_STATE,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "from": run.state,
                    "to": RUN_CANCELLING,
                    "reason": "cancel_confirmed",
                    "claim_epoch": claim.claim_epoch,
                    "control_cleared": True,
                },
                task_id=run.task_id,
            )
            self._finish_locked(
                tx,
                run_id,
                outcome="cancelled",
                effect_state=run.effect_state or "none_applied",
                reason=CANCEL_REASON,
            )
            return CONTROL_CANCEL

    # ---- 游标与模型结果（M2 · Task 5 的存储层） ----

    def _require_claims(self) -> JobStore:
        if self._claims is None:
            raise TianshuError(
                "未配置 JobStore：cursor/模型结果必须绑定有效 claim，"
                "不能用裸 run_id 写入"
            )
        return self._claims

    def _assert_writer(self, run_id: str, claim: JobClaim) -> Run:
        """写这个 Run 状态的人，必须同时是：还没被 fence 的 Run 的当前 owner。"""
        if not isinstance(claim, JobClaim):
            raise TianshuError("必须传 JobClaim；不能用裸 run_id 写 Run 的状态")
        if claim.run_id != run_id:
            raise TianshuError(
                f"claim 与 run 不匹配：claim.run_id={claim.run_id} run_id={run_id}"
            )
        self._require_claims().assert_claim(claim)
        return self.assert_current(run_id)

    def save_cursor(self, run_id: str, cursor: RunCursor, claim: JobClaim) -> RunCursor:
        """保存游标。写入前核验 claim，写入与事件同一事务。

        投影和事件都带 claim_epoch：事后能看出"这份游标是谁在第几次认领时写的"，
        重启接管争议才有据可查。
        """
        if not isinstance(cursor, RunCursor):
            raise TianshuError("cursor 必须是 RunCursor")
        payload = canonical_bytes(cursor.to_dict()).decode("utf-8")
        now = _now()
        with self._log.transaction() as tx:
            run = self._assert_writer(run_id, claim)
            self._conn.execute(
                "INSERT INTO run_cursors (run_id, task_id, goal_revision,"
                " lease_epoch, claim_epoch, payload, updated_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " task_id=excluded.task_id, goal_revision=excluded.goal_revision,"
                " lease_epoch=excluded.lease_epoch, claim_epoch=excluded.claim_epoch,"
                " payload=excluded.payload, updated_at=excluded.updated_at",
                (
                    run_id,
                    run.task_id,
                    run.goal_revision,
                    run.lease_epoch,
                    claim.claim_epoch,
                    payload,
                    now,
                ),
            )
            tx.append(
                run_id,
                RUN_CURSOR_SAVED,
                {
                    "run_id": run_id,
                    "claim_epoch": claim.claim_epoch,
                    "goal_revision": run.goal_revision,
                    "lease_epoch": run.lease_epoch,
                    "payload_hash": sha256_hex(canonical_bytes(cursor.to_dict())),
                    "pending_proposals": len(cursor.pending_proposals),
                    "completed_call_ids": len(cursor.completed_call_ids),
                },
                task_id=run.task_id,
            )
        return cursor

    def load_cursor(self, run_id: str) -> tuple[RunCursor, dict[str, Any]] | None:
        row = self._conn.execute(
            "SELECT * FROM run_cursors WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        meta = {
            "task_id": row["task_id"],
            "goal_revision": row["goal_revision"],
            "lease_epoch": row["lease_epoch"],
            "claim_epoch": row["claim_epoch"],
            "updated_at": row["updated_at"],
        }
        return RunCursor.of(json.loads(row["payload"])), meta

    def commit_model_turn(
        self, run_id: str, turn: dict[str, Any], claim: JobClaim
    ) -> int:
        """提交一次模型结果，返回该 Run 内递增的序号。

        重复提交同一个 `turn_id`：payload 相同就直接返回原序号（重启重放安全），
        不同则报错——同一个 turn_id 出现两种内容，说明状态已经不一致，
        静默覆盖会把"到底花了多少钱"这条账改掉。
        """
        turn_id = turn.get("turn_id")
        if not turn_id:
            raise TianshuError("turn 缺少 turn_id")
        payload = canonical_bytes(turn).decode("utf-8")
        now = _now()
        with self._log.transaction() as tx:
            run = self._assert_writer(run_id, claim)
            existing = self._conn.execute(
                "SELECT seq, payload FROM run_turns WHERE run_id=? AND turn_id=?",
                (run_id, turn_id),
            ).fetchone()
            if existing is not None:
                if existing["payload"] == payload:
                    return int(existing["seq"])
                raise TianshuError(
                    f"turn_id 重复但内容不同：run={run_id} turn={turn_id}"
                )
            seq = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_turns WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            self._conn.execute(
                "INSERT INTO run_turns (run_id, turn_id, seq, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, turn_id, seq, payload, now),
            )
            tx.append(
                run_id,
                RUN_MODEL_TURN,
                {
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "seq": seq,
                    "claim_epoch": claim.claim_epoch,
                    "finish_reason": turn.get("finish_reason"),
                    "usage": turn.get("usage"),
                    "payload_hash": sha256_hex(canonical_bytes(turn)),
                },
                task_id=run.task_id,
            )
        return seq

    def list_model_turns(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT turn_id, seq, payload FROM run_turns WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            {"turn_id": r["turn_id"], "seq": r["seq"], **json.loads(r["payload"])}
            for r in rows
        ]

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        effect_state: str,
        reason: str | None = None,
        report_outcome: str | None = None,
    ) -> Run:
        """收尾。被 fence 的 Run 不许报成功——否则旧目标会"复活"。

        校验与写入放在同一条条件 UPDATE 里，避免「检查完到写入之间被改目标」
        的竞态。

        已经停下（暂停/等审批/待核查/正在取消）的 Run 不能直接收尾：那些状态
        要经过 resume 或 cancel 才能变成终态。否则"暂停"可以被一句
        `finish_run(succeeded)` 悄悄改写成成功，PRD §4.3 的状态机就白写了。
        """
        with self._log.transaction() as tx:
            return self._finish_locked(
                tx,
                run_id,
                outcome=outcome,
                effect_state=effect_state,
                reason=reason,
                report_outcome=report_outcome,
                require_running=True,
            )

    def _finish_locked(
        self,
        tx,
        run_id: str,
        *,
        outcome: str,
        effect_state: str,
        reason: str | None = None,
        report_outcome: str | None = None,
        require_running: bool = False,
    ) -> Run:
        if outcome not in RUN_OUTCOMES:
            raise TianshuError(
                f"未知 outcome：{outcome}（可选 {sorted(RUN_OUTCOMES)}）"
            )
        if effect_state not in EFFECT_STATES:
            raise TianshuError(
                f"未知 effect_state：{effect_state}（可选 {sorted(EFFECT_STATES)}）"
            )
        now = _now()
        run = self.get_run(run_id)
        if run is None:
            raise TianshuError(f"run 不存在：{run_id}")
        if require_running and run.state in STOPPED_STATES:
            raise RunFenced(
                f"run 停在这些状态里不能直接收尾：{run_id}（state={run.state}）；"
                "请先 resume 或 cancel"
            )

        # 公开收尾要求"这个 Run 现在不是在停着的状态里"；把这条也塞进同一条
        # 条件 UPDATE，否则"检查通过 → 另一个进程把它暂停了 → 仍然收尾成功"
        # 这个窗口还开着。检查与写入必须在同一条语句里，这是本模块的老规矩。
        stopped = (
            " AND state NOT IN ('paused','waiting_approval','reconciling','cancelling')"
            if require_running
            else ""
        )
        cur = self._conn.execute(
            "UPDATE runs SET state=?, outcome=?, effect_state=?,"
            " control_requested=NULL, ended_at=?"
            " WHERE run_id=?"
            "   AND state NOT IN ('succeeded','failed','cancelled','fenced')"
            "   AND goal_revision = (SELECT goal_revision FROM tasks"
            "                        WHERE task_id = runs.task_id)"
            "   AND lease_epoch = (SELECT lease_epoch FROM tasks"
            "                      WHERE task_id = runs.task_id)" + stopped,
            (outcome, outcome, effect_state, now, run_id),
        )
        if cur.rowcount == 0:
            raise RunFenced(f"run {run_id} 已被 fence 或已终结，不得提交结果")

        self._conn.execute(
            "UPDATE tasks SET active_run_id=NULL, updated_at=?"
            " WHERE task_id=? AND active_run_id=?",
            (now, run.task_id, run_id),
        )
        # `run.end` 是"这次执行有结论了"的唯一事件。以前它由主循环单独写，
        # 于是 CLI 直接取消的 Run 只有 run.finish 没有 run.end——M0 的
        # S3「run.start → … → run.end」在取消路径上根本不成立，而取消恰恰是
        # 最需要让用户看见"到底改了什么"的路径。现在所有终态都从这一处写。
        tx.append(
            run_id,
            RUN_END,
            self._run_end_payload(
                run_id,
                outcome=report_outcome or outcome,
                effect_state=effect_state,
                reason=reason,
            ),
            task_id=run.task_id,
            actor_kind="core",
        )
        tx.append(
            run_id,
            RUN_FINISH,
            {
                "run_id": run_id,
                "task_id": run.task_id,
                "outcome": outcome,
                "effect_state": effect_state,
                **({"reason": reason} if reason else {}),
            },
            task_id=run.task_id,
        )
        # Run 结束即交还工作区，下一个 Run 才拿得到租约
        if self._leases is not None:
            self._leases.release_run(run_id)
        # 取消时作业必须一起收掉：留着 claimed 的作业会让下一个 worker 认领到
        # 一个已经结束的 Run，然后在一个终态上报"成功"。
        # 正常收尾则相反——作业要由 owner 用**自己的 claim** 来收，那才是
        # "我还活着且我还是 owner"的证据；这里替它收等于把这条证据抹掉。
        if outcome == "cancelled" and self._approvals is not None:
            # 取消之后旧批准不能再挂在待批队列里：它们属于一个已经结束的 Run，
            # 批了也不会执行，只会让人以为"还有事情没做完"。
            self._approvals.supersede_for_run(run_id, reason="run_cancelled")
        if outcome == "cancelled" and self._claims is not None:
            job = self._claims.job_for_run(run_id)
            # `waiting` 也要收：它是"等外面的事件"，而 Run 已经终结，那个事件
            # 永远不会再让它跑起来——留着就是一条永远不结束的作业。
            if job is not None and job.state in ("pending", "claimed", "waiting"):
                self._claims.cancel(job.job_id, reason="run_cancelled")
        return self.get_run(run_id)  # type: ignore[return-value]

    def _run_end_payload(
        self,
        run_id: str,
        *,
        outcome: str,
        effect_state: str,
        reason: str | None,
    ) -> dict[str, Any]:
        """终态结算：全部从事件与游标里读，不接受调用方"报个数"。

        为什么不让主循环传入自己的计数器：CLI 直接取消时没有主循环在场，
        那条路径就永远写不出结算；两套写法还会让"改了几个文件"的账在两种
        收尾方式下不一致。统一从事实源汇总，两条路径自动一样。
        """
        events = self._log.read_run(run_id)
        receipts = [
            e.payload["receipt_id"]
            for e in events
            if e.type == "receipt" and e.payload.get("receipt_id")
        ]
        artifact: dict[str, Any] | None = None
        for ev in events:
            if ev.type != "tool.result":
                continue
            if ev.payload.get("tool_id") != "create_report":
                continue
            if ev.payload.get("status") != "succeeded":
                continue
            out = ev.payload.get("output") or {}
            if out.get("sha256"):
                artifact = out
        intent_count = sum(1 for e in events if e.type == "model.intent")
        result_count = sum(1 for e in events if e.type == "model.result")
        # 按**事件条数**算，不按 request_id 去重：崩溃后重发的那一轮用的是
        # 同一个 request_id，去重会把"已经付过一次钱、没记进账"抹成 0。
        unbilled = max(0, intent_count - result_count)
        stored = self.load_cursor(run_id)
        ledger = (stored[0].budget_ledger if stored is not None else {}) or {}
        unknown_usage = int(ledger.get("unknown_usage_calls", 0) or 0)
        return {
            "outcome": outcome,
            "effect_state": effect_state,
            "artifact": artifact,
            "turns": int(ledger.get("turns", 0) or 0),
            "tokens": int(ledger.get("tokens", 0) or 0),
            "tokens_unknown_calls": unknown_usage,
            "receipt_ids": receipts,
            # 预算可信 = 没有任何"可能花了但没记账"的调用
            "budget_enforced": unknown_usage == 0 and unbilled == 0,
            "unbilled_model_intents": unbilled,
            "error": reason,
        }

    def list_runs(self, task_id: str) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # ---- 投影重建 ----

    def rebuild_from_events(self) -> None:
        """按事件重建 tasks/runs 投影。事件是事实源，投影是可丢的缓存。

        只对"状态变更都记了事件"的库成立。PR #7 之前建的库只有投影表、
        没有对应事件，重建会把它们清空——那种库只能当只读历史，不能重建。
        """
        events = self._log.read_types(
            [
                TASK_CREATED,
                TASK_REVISED,
                RUN_START,
                RUN_FINISH,
                RUN_FENCED_EVENT,
                RUN_STATE,
                RUN_CONTROL,
                RUN_EFFECT,
            ]
        )
        tasks: dict[str, dict[str, Any]] = {}
        runs: dict[str, dict[str, Any]] = {}

        for ev in events:
            p = ev.payload
            if ev.type == TASK_CREATED:
                tasks[p["task_id"]] = {
                    "task_id": p["task_id"],
                    "goal": p["goal"],
                    "goal_revision": p["goal_revision"],
                    "lease_epoch": p["lease_epoch"],
                    "active_run_id": None,
                    "status": "open",
                    "workspaces": json.dumps(
                        [
                            b.to_dict()
                            for b in parse_bindings(p.get("workspaces") or [])
                        ],
                        ensure_ascii=False,
                    ),
                    "created_at": ev.ts,
                    "updated_at": ev.ts,
                }
            elif ev.type == TASK_REVISED:
                t = tasks.get(p["task_id"])
                if t is None:
                    continue
                t.update(
                    goal=p["goal"],
                    goal_revision=p["goal_revision"],
                    lease_epoch=p["lease_epoch"],
                    active_run_id=None,
                    updated_at=ev.ts,
                )
                if "workspaces" in p:
                    t["workspaces"] = json.dumps(
                        [
                            b.to_dict()
                            for b in parse_bindings(p.get("workspaces") or [])
                        ],
                        ensure_ascii=False,
                    )
            elif ev.type == RUN_START:
                runs[p["run_id"]] = {
                    "run_id": p["run_id"],
                    "task_id": p["task_id"],
                    "goal_revision": p["goal_revision"],
                    "lease_epoch": p["lease_epoch"],
                    "state": "created",
                    "outcome": None,
                    "effect_state": None,
                    "control_requested": None,
                    "created_at": ev.ts,
                    "ended_at": None,
                }
                t = tasks.get(p["task_id"])
                if t is not None:
                    t["active_run_id"] = p["run_id"]
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_FINISH:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(
                    state=p["outcome"],
                    outcome=p["outcome"],
                    effect_state=p["effect_state"],
                    # 终态没有"排队的控制请求"这回事：请求要么已被确认，
                    # 要么随着 Run 结束作废。重建时也得清掉，否则投影里会
                    # 留一个永不生效的 pause。
                    control_requested=None,
                    ended_at=ev.ts,
                )
                t = tasks.get(p["task_id"])
                if t is not None and t["active_run_id"] == p["run_id"]:
                    t["active_run_id"] = None
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_FENCED_EVENT:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(state="fenced", control_requested=None, ended_at=ev.ts)
                t = tasks.get(r["task_id"])
                if t is not None and t["active_run_id"] == p["run_id"]:
                    t["active_run_id"] = None
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_STATE:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                # 只有**显式标了** control_cleared 的状态迁移才清请求。
                # 无条件清会把"请求已排队、还没到安全点"这件事抹掉：一个
                # 运行中的 Run 先被暂停请求、随后因为别的原因过一次状态迁移
                # （比如转 waiting_approval），重建之后请求就凭空消失了。
                cleared = p.get("control_cleared") is True
                r.update(state=p["to"], updated_at=ev.ts)
                if cleared:
                    r["control_requested"] = None
            elif ev.type == RUN_CONTROL:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(control_requested=p["action"], updated_at=ev.ts)
            elif ev.type == RUN_EFFECT:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(effect_state=p["effect_state"], updated_at=ev.ts)

        with self._log.transaction():
            self._conn.execute("DELETE FROM tasks")
            self._conn.execute("DELETE FROM runs")
            for t in tasks.values():
                self._conn.execute(
                    "INSERT INTO tasks (task_id, goal, goal_revision, lease_epoch,"
                    " active_run_id, status, workspaces, created_at, updated_at)"
                    " VALUES (:task_id,:goal,:goal_revision,:lease_epoch,"
                    " :active_run_id,:status,:workspaces,:created_at,:updated_at)",
                    t,
                )
            for r in runs.values():
                self._conn.execute(
                    "INSERT INTO runs (run_id, task_id, goal_revision, lease_epoch,"
                    " state, outcome, effect_state, control_requested,"
                    " created_at, ended_at)"
                    " VALUES (:run_id,:task_id,:goal_revision,:lease_epoch,"
                    " :state,:outcome,:effect_state,:control_requested,"
                    " :created_at,:ended_at)",
                    r,
                )

    # ---- 行映射 ----

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            goal=row["goal"],
            goal_revision=row["goal_revision"],
            lease_epoch=row["lease_epoch"],
            active_run_id=row["active_run_id"],
            status=row["status"],
            workspaces=parse_bindings(json.loads(row["workspaces"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
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
            created_at=row["created_at"],
            ended_at=row["ended_at"] if "ended_at" in keys else None,
        )


__all__: list[Any] = [
    "TaskStore",
    "Task",
    "Run",
    "RunFenced",
    "TERMINAL_STATES",
    "NON_TERMINAL_STATES",
    "RUN_STATES",
    "RUN_OUTCOMES",
    "EFFECT_STATES",
]
