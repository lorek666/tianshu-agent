"""工作区租约（M2 · PR A 的 Task 2）。

解决的是：**两个进程/两个任务同时改同一个目录**。

要点：
1. 租约按**资源身份**（目录 inode）互斥，不按 Task、也不按路径字符串。
   同一个 Task 的两个 Run 一样要抢；不同 Task 指向同一目录也一样要抢。
2. 跨进程有效性靠 SQLite 的写锁（BEGIN IMMEDIATE），不靠内存标志。
3. 租约有时限：持有者崩溃后，过期即可被接管，不会永久锁死目录。
4. 多目录 all-or-nothing：要么全拿到，要么一个都不占。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

from .errors import TianshuError

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    resource_key TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    owner_pid    INTEGER NOT NULL,
    acquired_at  INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leases_run ON leases(run_id);
"""

DEFAULT_TTL_MS = 60_000


class LeaseConflict(TianshuError):
    """资源已被别的持有者占用。"""


class LeaseLost(TianshuError):
    """租约已不属于自己，或已过期——此时继续写入是不安全的。"""


@dataclass
class Lease:
    resource_key: str
    run_id: str
    owner_pid: int
    acquired_at: int
    expires_at: int

    def to_dict(self) -> dict:
        return {
            "resource_key": self.resource_key,
            "run_id": self.run_id,
            "owner_pid": self.owner_pid,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }


def _now() -> int:
    return int(time.time() * 1000)


def _pid_alive(pid: int) -> bool:
    """POSIX 下用 signal 0 探测；Windows 上只靠 TTL（不虚报能力）。"""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LeaseStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.executescript(SCHEMA)

    def acquire_many(
        self,
        resource_keys: list[str],
        run_id: str,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        steal_stale: bool = True,
    ) -> list[Lease]:
        """多资源一次性获取。任一冲突则整批不占——避免半绑定状态。"""
        if not resource_keys:
            return []

        now = _now()
        expires = now + ttl_ms
        pid = os.getpid()
        acquired: list[Lease] = []

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for key in resource_keys:
                row = self._conn.execute(
                    "SELECT * FROM leases WHERE resource_key = ?", (key,)
                ).fetchone()

                if row is not None:
                    same_owner = row["run_id"] == run_id and row["owner_pid"] == pid
                    stale = self._is_stale(row, now)
                    if not same_owner and not (steal_stale and stale):
                        raise LeaseConflict(
                            f"资源已被占用：{key}（run={row['run_id']} "
                            f"pid={row['owner_pid']} 到期={row['expires_at']}）"
                        )

                self._conn.execute(
                    "INSERT INTO leases (resource_key, run_id, owner_pid,"
                    " acquired_at, expires_at) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(resource_key) DO UPDATE SET"
                    " run_id=excluded.run_id, owner_pid=excluded.owner_pid,"
                    " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                    (key, run_id, pid, now, expires),
                )
                acquired.append(Lease(key, run_id, pid, now, expires))

            self._conn.execute("COMMIT")
            return acquired
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def is_held_by(self, resource_key: str, run_id: str) -> bool:
        """当前是否仍由这个 run 有效持有（未过期、未失效）。"""
        row = self._conn.execute(
            "SELECT * FROM leases WHERE resource_key = ?", (resource_key,)
        ).fetchone()
        if row is None or row["run_id"] != run_id:
            return False
        return not self._is_stale(row, _now())

    def assert_held(self, resource_keys: list[str], run_id: str) -> None:
        """写操作之前必须调用。

        租约会过期、会被别人接管，所以"曾经拿到过"不等于"现在还持有"。
        在执行侧校验，才能保证过期后不会再写。
        """
        for key in resource_keys:
            if not self.is_held_by(key, run_id):
                raise LeaseLost(f"租约已失效或被接管：{key}（run={run_id}）")

    def renew(
        self, resource_keys: list[str], run_id: str, *, ttl_ms: int = DEFAULT_TTL_MS
    ) -> int:
        """续租。只续自己持有的；返回实际续到的数量。"""
        now = _now()
        expires = now + ttl_ms
        n = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for key in resource_keys:
                cur = self._conn.execute(
                    "UPDATE leases SET expires_at=?"
                    " WHERE resource_key=? AND run_id=? AND owner_pid=?",
                    (expires, key, run_id, os.getpid()),
                )
                n += cur.rowcount
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return n

    def release(self, resource_keys: list[str], run_id: str) -> int:
        n = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for key in resource_keys:
                cur = self._conn.execute(
                    "DELETE FROM leases WHERE resource_key=? AND run_id=?",
                    (key, run_id),
                )
                n += cur.rowcount
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return n

    def holder(self, resource_key: str) -> Lease | None:
        row = self._conn.execute(
            "SELECT * FROM leases WHERE resource_key = ?", (resource_key,)
        ).fetchone()
        if row is None:
            return None
        return Lease(
            resource_key=row["resource_key"],
            run_id=row["run_id"],
            owner_pid=row["owner_pid"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    def list_for_run(self, run_id: str) -> list[Lease]:
        rows = self._conn.execute(
            "SELECT * FROM leases WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [
            Lease(r["resource_key"], r["run_id"], r["owner_pid"],
                  r["acquired_at"], r["expires_at"])
            for r in rows
        ]

    @staticmethod
    def _is_stale(row: sqlite3.Row, now: int) -> bool:
        if row["expires_at"] <= now:
            return True
        return not _pid_alive(row["owner_pid"])


__all__ = [
    "LeaseStore",
    "Lease",
    "LeaseConflict",
    "LeaseLost",
    "DEFAULT_TTL_MS",
]
