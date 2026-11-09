"""Task 与 Run（M2 · PR A 的 Task 1）。

一件用户的事 = 一个 Task；一次执行 = 一个 Run。一个 Task 可以有多个 Run，
失败重试就是新开一个 Run，历史不覆盖。

核心是 **fencing**：用户中途改主意时，旧 Run 必须立刻失效，
不能拿着旧目标继续改文件。
- `goal_revision`：目标改一次加一
- `lease_epoch`：工作区所有权变更加一

Run 绑定创建时的这两个值；任一与 Task 当前值不符，即为被 fence。

**事实源是事件日志，不是这两张表。** tasks/runs 只是投影：每次状态变更都和
对应事件（`task.created` / `task.revised` / `run.start` / `run.finish` /
`run.fenced`）在同一个 SQLite 事务里提交，投影被删掉也能按事件重建。
否则"用户改过目标"这件事只存在于一张可被覆写的表里，回放无法解释
某个 Run 为什么被 fence。

**锁序（全局唯一顺序）：执行守卫 → SQLite 事务。**

守卫是跨进程文件锁，SQLite 事务是跨进程写锁，两者都只有一把。如果某条路径
先拿事务再等守卫，就会和"先拿守卫再等事务"的另一条路径互相卡死。
所以凡是需要守卫的方法（`begin_execution` / `revise_goal`）都显式拒绝在
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
from typing import Any

from .errors import RunFenced, TianshuError
from .events import EventLog, canonical_bytes, sha256_hex
from .jobs import JobClaim, JobStore
from .leases import GUARD_TIMEOUT_MS, LeaseStore, execution_guard
from .workspace import ResourceIdentity, WorkspaceBinding, parse_bindings

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

TASK_CREATED = "task.created"
TASK_REVISED = "task.revised"
EXECUTION_STARTED = "execution.started"
RUN_CURSOR_SAVED = "run.cursor"
RUN_MODEL_TURN = "run.model_turn"
RUN_START = "run.start"
RUN_FINISH = "run.finish"
RUN_FENCED = "run.fenced"

# Task 级事件不属于某个 Run；用空 run_id 把它们与 run 作用域事件分开。
TASK_SCOPE = ""


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
        self._leases = leases
        self._claims = claims
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
        new_goal = (new_goal or "").strip()
        if not new_goal:
            raise TianshuError("goal 不能为空")
        # 与 begin_execution 抢同一把守卫：要么改目标先完成（旧 Run 立刻 fence），
        # 要么执行已经开始（改目标排队到这次执行结束），不会出现"复核通过后目标被改"。
        with self._guard():
            with self._log.transaction() as tx:
                task = self.get_task(task_id)
                if task is None:
                    raise TianshuError(f"task 不存在：{task_id}")
                if task.status != "open":
                    raise TianshuError(f"task 已关闭，不能改目标：{task.status}")
                self._revise_locked(tx, task, new_goal)
        return self.get_task(task_id)  # type: ignore[return-value]

    def _revise_locked(self, tx, task: Task, new_goal: str) -> None:
        now = _now()
        revision = task.goal_revision + 1
        epoch = task.lease_epoch + 1
        self._conn.execute(
            "UPDATE tasks SET goal=?, goal_revision=?,"
            " lease_epoch=?, active_run_id=NULL, updated_at=?"
            " WHERE task_id=?",
            (new_goal, revision, epoch, now, task.task_id),
        )
        tx.append(
            TASK_SCOPE,
            TASK_REVISED,
            {
                "task_id": task.task_id,
                "goal": new_goal,
                "goal_revision": revision,
                "lease_epoch": epoch,
            },
            task_id=task.task_id,
            actor_kind="human",
        )
        # 旧的活跃 Run 标记为 fenced（已终结的保持原状）
        if task.active_run_id:
            self._fence_run(tx, task.active_run_id, now, reason="goal_revised")

    def _fence_run(self, tx, run_id: str, now: int, *, reason: str) -> None:
        cur = self._conn.execute(
            "UPDATE runs SET state='fenced', ended_at=?"
            " WHERE run_id=? AND state NOT IN ('succeeded','failed','cancelled','fenced')",
            (now, run_id),
        )
        if cur.rowcount == 0:
            return
        run = self.get_run(run_id)
        tx.append(
            run_id,
            RUN_FENCED,
            {
                "run_id": run_id,
                "task_id": run.task_id if run else None,
                "reason": reason,
            },
            task_id=run.task_id if run else None,
        )
        # fence 之后旧 Run 不再拥有工作区，租约必须交出来；
        # 否则同一 Task 的新 Run 会被上一个 Run 的残留租约挡住，直到 TTL 到期。
        if self._leases is not None:
            self._leases.release_run(run_id)

    # ---- Run ----

    def start_run(self, task_id: str) -> Run:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
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
                    "run_id": run_id,
                    "task_id": task_id,
                    "goal_revision": task.goal_revision,
                    "lease_epoch": task.lease_epoch,
                },
                task_id=task_id,
            )
        return self.get_run(run_id)  # type: ignore[return-value]

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

        尚未包含审批/grant 核验——那是 PR B 的派发层。这里覆盖的是
        revision/epoch/租约三件事，缺一项就拒绝。
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
        self, run_id: str, *, outcome: str, effect_state: str
    ) -> Run:
        """收尾。被 fence 的 Run 不许报成功——否则旧目标会"复活"。

        校验与写入放在同一条条件 UPDATE 里，避免「检查完到写入之间被改目标」
        的竞态。
        """
        if outcome not in RUN_OUTCOMES:
            raise TianshuError(
                f"未知 outcome：{outcome}（可选 {sorted(RUN_OUTCOMES)}）"
            )
        if effect_state not in EFFECT_STATES:
            raise TianshuError(
                f"未知 effect_state：{effect_state}（可选 {sorted(EFFECT_STATES)}）"
            )
        now = _now()
        with self._log.transaction() as tx:
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")

            cur = self._conn.execute(
                "UPDATE runs SET state=?, outcome=?, effect_state=?, ended_at=?"
                " WHERE run_id=?"
                "   AND state NOT IN ('succeeded','failed','cancelled','fenced')"
                "   AND goal_revision = (SELECT goal_revision FROM tasks"
                "                        WHERE task_id = runs.task_id)"
                "   AND lease_epoch = (SELECT lease_epoch FROM tasks"
                "                      WHERE task_id = runs.task_id)",
                (outcome, outcome, effect_state, now, run_id),
            )
            if cur.rowcount == 0:
                raise RunFenced(f"run {run_id} 已被 fence 或已终结，不得提交结果")

            self._conn.execute(
                "UPDATE tasks SET active_run_id=NULL, updated_at=?"
                " WHERE task_id=? AND active_run_id=?",
                (now, run.task_id, run_id),
            )
            tx.append(
                run_id,
                RUN_FINISH,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "outcome": outcome,
                    "effect_state": effect_state,
                },
                task_id=run.task_id,
            )
            # Run 结束即交还工作区，下一个 Run 才拿得到租约
            if self._leases is not None:
                self._leases.release_run(run_id)
        return self.get_run(run_id)  # type: ignore[return-value]

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
            [TASK_CREATED, TASK_REVISED, RUN_START, RUN_FINISH, RUN_FENCED]
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
            elif ev.type == RUN_START:
                runs[p["run_id"]] = {
                    "run_id": p["run_id"],
                    "task_id": p["task_id"],
                    "goal_revision": p["goal_revision"],
                    "lease_epoch": p["lease_epoch"],
                    "state": "created",
                    "outcome": None,
                    "effect_state": None,
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
                    ended_at=ev.ts,
                )
                t = tasks.get(p["task_id"])
                if t is not None and t["active_run_id"] == p["run_id"]:
                    t["active_run_id"] = None
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_FENCED:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(state="fenced", ended_at=ev.ts)
                t = tasks.get(r["task_id"])
                if t is not None and t["active_run_id"] == p["run_id"]:
                    t["active_run_id"] = None
                    t["updated_at"] = ev.ts

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
                    " state, outcome, effect_state, created_at, ended_at)"
                    " VALUES (:run_id,:task_id,:goal_revision,:lease_epoch,"
                    " :state,:outcome,:effect_state,:created_at,:ended_at)",
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
            created_at=row["created_at"],
            ended_at=row["ended_at"] if "ended_at" in keys else None,
        )


__all__: list[Any] = [
    "TaskStore",
    "Task",
    "Run",
    "RunFenced",
    "TERMINAL_STATES",
    "RUN_OUTCOMES",
    "EFFECT_STATES",
]
