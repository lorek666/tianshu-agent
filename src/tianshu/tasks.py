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
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError
from .events import EventLog
from .leases import LeaseStore
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
"""

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
RUN_OUTCOMES = TERMINAL_STATES
EFFECT_STATES = {"none_applied", "applied", "partial", "unknown"}

TASK_CREATED = "task.created"
TASK_REVISED = "task.revised"
RUN_START = "run.start"
RUN_FINISH = "run.finish"
RUN_FENCED = "run.fenced"

# Task 级事件不属于某个 Run；用空 run_id 把它们与 run 作用域事件分开。
TASK_SCOPE = ""


class RunFenced(TianshuError):
    """Run 已被目标变更或租约变更 fence，不得继续。"""


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


def _now() -> int:
    return int(time.time() * 1000)


class TaskStore:
    def __init__(self, log: EventLog, *, leases: LeaseStore | None = None) -> None:
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._leases = leases

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def leases(self) -> LeaseStore | None:
        return self._leases

    def transaction(self):
        """与事件日志共用的可组合事务；已在事务中时复用外层事务。"""
        return self._log.transaction()

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
        """执行前核验：这个 Run 现在还有资格写入吗。

        与 `LeaseStore.assert_held` 一起构成执行侧双检；两者都通过仍不是
        线性化保证（需要 execution_guard 把核验与开始执行放进同一把锁）。
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
        """按事件重建 tasks/runs 投影。事件是事实源，投影是可丢的缓存。"""
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
