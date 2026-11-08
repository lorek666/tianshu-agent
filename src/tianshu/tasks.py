"""Task 与 Run（M2 · PR A 的 Task 1）。

一件用户的事 = 一个 Task；一次执行 = 一个 Run。一个 Task 可以有多个 Run，
失败重试就是新开一个 Run，历史不覆盖。

核心是 **fencing**：用户中途改主意时，旧 Run 必须立刻失效，
不能拿着旧目标继续改文件。
- `goal_revision`：目标改一次加一
- `lease_epoch`：工作区所有权变更加一
Run 绑定创建时的这两个值；任一与 Task 当前值不符，即为被 fence。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError

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
    workspaces: list[str] = field(default_factory=list)
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
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.executescript(SCHEMA)

    # ---- Task ----

    def create_task(self, goal: str, workspaces: list[str] | None = None) -> Task:
        now = _now()
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO tasks (task_id, goal, goal_revision, lease_epoch,"
            " active_run_id, status, workspaces, created_at, updated_at)"
            " VALUES (?,?,1,1,NULL,'open',?,?,?)",
            (task_id, goal, json.dumps(workspaces or [], ensure_ascii=False), now, now),
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def revise_goal(self, task_id: str, new_goal: str) -> Task:
        """用户改主意。旧目标下的 Run 立即失效，历史保留。"""
        task = self.get_task(task_id)
        if task is None:
            raise TianshuError(f"task 不存在：{task_id}")

        now = _now()
        self._conn.execute(
            "UPDATE tasks SET goal=?, goal_revision=goal_revision+1,"
            " lease_epoch=lease_epoch+1, active_run_id=NULL, updated_at=?"
            " WHERE task_id=?",
            (new_goal, now, task_id),
        )
        # 旧的活跃 Run 标记为 fenced（不是在跑的话就保持原状）
        if task.active_run_id:
            self._conn.execute(
                "UPDATE runs SET state='fenced', ended_at=?"
                " WHERE run_id=? AND state NOT IN ('succeeded','failed','cancelled')",
                (now, task.active_run_id),
            )
        return self.get_task(task_id)

    # ---- Run ----

    def start_run(self, task_id: str) -> Run:
        task = self.get_task(task_id)
        if task is None:
            raise TianshuError(f"task 不存在：{task_id}")
        if task.status != "open":
            raise TianshuError(f"task 已关闭：{task.status}")

        now = _now()
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO runs (run_id, task_id, goal_revision, lease_epoch,"
            " state, created_at) VALUES (?,?,?,?,'created',?)",
            (run_id, task_id, task.goal_revision, task.lease_epoch, now),
        )
        self._conn.execute(
            "UPDATE tasks SET active_run_id=?, updated_at=? WHERE task_id=?",
            (run_id, now, task_id),
        )
        return self.get_run(run_id)

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

    def finish_run(
        self, run_id: str, *, outcome: str, effect_state: str
    ) -> Run:
        """收尾。被 fence 的 Run 不许报成功——否则旧目标会"复活"。"""
        run = self.get_run(run_id)
        if run is None:
            raise TianshuError(f"run 不存在：{run_id}")
        if run.state == "fenced" or self.is_fenced(run_id):
            raise RunFenced(f"run {run_id} 已被 fence，不得提交结果")

        now = _now()
        self._conn.execute(
            "UPDATE runs SET state=?, outcome=?, effect_state=?, ended_at=?"
            " WHERE run_id=?",
            (outcome, outcome, effect_state, now, run_id),
        )
        self._conn.execute(
            "UPDATE tasks SET active_run_id=NULL, updated_at=?"
            " WHERE task_id=? AND active_run_id=?",
            (now, run.task_id, run_id),
        )
        return self.get_run(run_id)

    def list_runs(self, task_id: str) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

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
            workspaces=json.loads(row["workspaces"]),
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


__all__: list[Any] = ["TaskStore", "Task", "Run", "RunFenced", "TERMINAL_STATES"]
