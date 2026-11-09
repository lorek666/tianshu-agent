"""工作区租约（M2 · PR A 的 Task 2）。

解决的是：**两个进程/两个任务同时改同一棵目录树**。

要点：
1. 租约按**资源身份**（目录 inode）互斥，不按 Task、也不按路径字符串。
   同一个 Task 的两个 Run 一样要抢；不同 Task 指向同一目录也一样要抢。
2. **父子目录也算同一资源**：inode 只能回答"是不是同一个目录"，
   `/ws` 与 `/ws/sub` 是两个 inode 却是同一棵树。只比 inode 会让两个
   Run 同时写同一棵子树——所以身份 + 规范路径前缀一起判。
3. 跨进程有效性靠 SQLite 的写锁（BEGIN IMMEDIATE），不靠内存标志。
4. 租约有时限：持有者崩溃后，过期即可被接管，不会永久锁死目录。
5. 多目录 all-or-nothing：要么全拿到，要么一个都不占。
6. **owner 身份是进程实例**（pid + 每次启动生成的 instance_id + 启动时间），
   不是裸 pid：pid 会被复用，"同一个 pid"不等于"同一个进程"。
   只有同一个实例才能续租/释放/通过写前校验；别的进程即使拿着同一个
   run_id 也不能替持有者写。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .events import EventLog
from .workspace import ResourceIdentity, is_ancestor_or_self, overlap_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    resource_key      TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    owner_pid         INTEGER NOT NULL,
    owner_instance_id TEXT NOT NULL DEFAULT '',
    canonical_path    TEXT NOT NULL DEFAULT '',
    path_key          TEXT NOT NULL DEFAULT '',
    acquired_at       INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    wall_acquired_at  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_leases_run ON leases(run_id);
"""

# 计划 §2.2 的默认值：TTL 30s、heartbeat 5s。TTL 短于一轮模型调用，
# 所以持有者必须续租；不续租就意味着持有者可能已经死了。
DEFAULT_TTL_MS = 30_000
HEARTBEAT_MS = 5_000
GUARD_TIMEOUT_MS = 5_000

# 判定"pid 被复用"的容差。macOS 的 `ps -o etime=` 只有秒级精度，
# 加上取整会把启动时间算早/晚最多 1 秒，所以要留余量。
# 方向很重要：只有"进程启动时间晚于租约获取时间"才算复用，
# 因为合法持有者不可能比自己的租约晚启动。留余量只会让检测偏保守（少接管），
# 不会让两个写者同时上场。
PID_REUSE_MARGIN_MS = 2_000


class LeaseConflict(TianshuError):
    """资源已被别的持有者占用（含父子目录重叠）。"""


class LeaseLost(TianshuError):
    """租约已不属于自己，或已过期——此时继续写入是不安全的。"""


class GuardTimeout(TianshuError):
    """执行守卫在限定时间内没拿到锁。"""


@dataclass(frozen=True)
class ProcessIdentity:
    """进程实例身份。pid 会被 OS 复用，instance_id 不会。"""

    instance_id: str
    pid: int
    started_at: int

    @classmethod
    def mint(cls) -> ProcessIdentity:
        return cls(
            instance_id=uuid.uuid4().hex,
            pid=os.getpid(),
            started_at=int(time.time() * 1000),
        )


_CURRENT: ProcessIdentity | None = None


def current_process() -> ProcessIdentity:
    """本进程的实例身份。整个进程共用一个，多个 store 必须一致。"""
    global _CURRENT
    if _CURRENT is None or _CURRENT.pid != os.getpid():
        _CURRENT = ProcessIdentity.mint()
    return _CURRENT


def _now() -> int:
    return int(time.time() * 1000)


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    POSIX 用 signal 0；Windows 用 OpenProcess + WaitForSingleObject，
    不必等句柄对象——目标进程退出后句柄会变成有信号状态。
    """
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不属于我：活着
        return True
    return True


def _win_pid_alive(pid: int) -> bool:  # pragma: no cover - 仅 Windows 可测
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        # 5 = ERROR_ACCESS_DENIED：进程在，但没权限。其余视为已退出。
        return ctypes.get_last_error() == 5 or kernel32.GetLastError() == 5
    try:
        # WAIT_OBJECT_0(0) = 已退出；WAIT_TIMEOUT(258) = 仍在跑
        return kernel32.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel32.CloseHandle(handle)


def pid_started_at(pid: int) -> int | None:
    """进程启动时间（epoch ms）。拿不到就返回 None。

    用途是识别 **pid 复用**：租约里记的 pid 现在可能属于另一个进程，
    此时 `os.kill(pid, 0)` 会说"活着"，但那个"活着"跟租约毫无关系。
    """
    try:
        if os.name == "nt":
            return _win_pid_started_at(pid)
        if os.path.exists("/proc/stat"):
            return _linux_pid_started_at(pid)
        return _bsd_pid_started_at(pid)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _linux_pid_started_at(pid: int) -> int | None:  # pragma: no cover - 仅 Linux
    with open(f"/proc/{pid}/stat", "rb") as fh:
        stat = fh.read().decode("utf-8", "replace")
    # comm 里可能有空格和括号，从最后一个 ')' 之后数才不会错位
    fields = stat.rsplit(")", 1)[1].split()
    starttime_ticks = int(fields[19])  # 第 22 字段 starttime（从 state 起算第 19 个）
    btime = None
    with open("/proc/stat", "rb") as fh:
        for line in fh.read().decode("utf-8", "replace").splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
    if btime is None:
        return None
    hz = os.sysconf("SC_CLK_TCK")
    return int(btime * 1000 + starttime_ticks * 1000 / hz)


def _bsd_pid_started_at(pid: int) -> int | None:  # pragma: no cover - 仅 macOS/BSD
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "etime="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    text = out.stdout.strip()
    if not text:
        return None
    elapsed_ms = _parse_etime_ms(text)
    if elapsed_ms is None:
        return None
    return int(time.time() * 1000) - elapsed_ms


def _parse_etime_ms(text: str) -> int | None:
    """解析 ps 的 etime 格式：[[dd-]hh:]mm:ss。"""
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in text.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, (minutes, seconds) = 0, parts
    else:
        return None
    total = days * 86_400 + hours * 3_600 + minutes * 60 + seconds
    return total * 1000


def _win_pid_started_at(pid: int) -> int | None:  # pragma: no cover - 仅 Windows
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks_1601 = (created.dwHighDateTime << 32) | created.dwLowDateTime
        # FILETIME 是 1601 起的 100ns 计数
        return int(ticks_1601 // 10_000 - 11_644_473_600_000)
    finally:
        kernel32.CloseHandle(handle)


@dataclass(frozen=True)
class Lease:
    resource_key: str
    run_id: str
    owner_pid: int
    acquired_at: int
    expires_at: int
    owner_instance_id: str = ""
    canonical_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_key": self.resource_key,
            "run_id": self.run_id,
            "owner_pid": self.owner_pid,
            "owner_instance_id": self.owner_instance_id,
            "canonical_path": self.canonical_path,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class LeaseSet:
    """一次 acquire 的结果，绑定了 owner 身份；续租/释放都从它出发。"""

    run_id: str
    identity: ProcessIdentity
    leases: tuple[Lease, ...]

    @property
    def resource_keys(self) -> list[str]:
        return [lease.resource_key for lease in self.leases]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "owner_instance_id": self.identity.instance_id,
            "leases": [lease.to_dict() for lease in self.leases],
        }


def _ensure_columns(
    conn: sqlite3.Connection, table: str, wanted: dict[str, str]
) -> None:
    """给早期版本的库补列。

    旧库只有 resource_key/run_id/owner_pid/acquired_at/expires_at；
    新列默认空值——空 instance_id 意味着"身份未知"，不能当作自己，
    宁可多挡一次也不能误判成持有者。
    """
    have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in wanted.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _normalize(resource: ResourceIdentity | Path | str) -> ResourceIdentity:
    if isinstance(resource, ResourceIdentity):
        return resource
    return ResourceIdentity.of(Path(resource))


class LeaseStore:
    def __init__(
        self,
        log: EventLog,
        *,
        identity: ProcessIdentity | None = None,
        clock: Callable[[], int] = _now,
        pid_started: Callable[[int], int | None] = pid_started_at,
    ) -> None:
        if log.in_transaction:
            # executescript 会先隐式 COMMIT 掉外层事务，构造 store 就等于把半份
            # 状态提交出去。只能在事务外构造。
            raise TianshuError("LeaseStore 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._identity = identity or current_process()
        self._clock = clock
        self._pid_started = pid_started
        self._conn.executescript(SCHEMA)
        _ensure_columns(
            self._conn,
            "leases",
            {
                "owner_instance_id": "TEXT NOT NULL DEFAULT ''",
                "canonical_path": "TEXT NOT NULL DEFAULT ''",
                "path_key": "TEXT NOT NULL DEFAULT ''",
                "wall_acquired_at": "INTEGER NOT NULL DEFAULT 0",
            },
        )

    @property
    def identity(self) -> ProcessIdentity:
        return self._identity

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def transaction(self):
        """与外层状态共用同一个事务；已在事务中时复用，不重复 BEGIN。"""
        return self._log.transaction()

    # ---- 获取 ----

    def acquire(
        self,
        resources: list[ResourceIdentity | Path | str],
        run_id: str,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        steal_stale: bool = True,
    ) -> LeaseSet:
        """多资源一次性获取。任一冲突则整批不占——避免半绑定状态。"""
        with self._log.transaction():
            leases = self._acquire_locked(resources, run_id, ttl_ms, steal_stale)
        return LeaseSet(run_id, self._identity, tuple(leases))

    def acquire_many(
        self,
        resources: list[ResourceIdentity | Path | str],
        run_id: str,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        steal_stale: bool = True,
    ) -> list[Lease]:
        return list(self.acquire(
            resources, run_id, ttl_ms=ttl_ms, steal_stale=steal_stale
        ).leases)

    def _acquire_locked(
        self,
        resources: list[ResourceIdentity | Path | str],
        run_id: str,
        ttl_ms: int,
        steal_stale: bool,
    ) -> list[Lease]:
        if not resources:
            return []

        now = self._clock()
        expires = now + ttl_ms
        wanted = [_normalize(r) for r in resources]
        rows = self._conn.execute("SELECT * FROM leases").fetchall()

        # 先判冲突，再写：全部通过才落库。
        for want in wanted:
            for row in rows:
                if row["resource_key"] == want.key:
                    if self._is_owner(row, run_id):
                        continue
                    if steal_stale and self._is_stale(row, now):
                        continue
                    raise LeaseConflict(
                        f"资源已被占用：{want.path or want.key}（run={row['run_id']} "
                        f"pid={row['owner_pid']} 到期={row['expires_at']}）"
                    )
                row_path = row["path_key"]
                if not row_path:
                    continue
                if not (
                    is_ancestor_or_self(row_path, want.path_key)
                    or is_ancestor_or_self(want.path_key, row_path)
                ):
                    continue
                # 父子目录重叠：同一棵树不能被两个持有者同时写
                if self._is_owner(row, run_id):
                    continue
                if steal_stale and self._is_stale(row, now):
                    # 死掉的持有者留下的覆盖租约顺手清掉，否则 TTL 内会一直挡住
                    self._conn.execute(
                        "DELETE FROM leases WHERE resource_key=?",
                        (row["resource_key"],),
                    )
                    continue
                raise LeaseConflict(
                    f"目录重叠：{want.path or want.key} 与 "
                    f"{row['canonical_path'] or row['resource_key']} 属于同一棵子树"
                    f"（持有者 run={row['run_id']}）"
                )

        acquired: list[Lease] = []
        for want in wanted:
            self._conn.execute(
                "INSERT INTO leases (resource_key, run_id, owner_pid,"
                " owner_instance_id, canonical_path, path_key, acquired_at,"
                " expires_at, wall_acquired_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(resource_key) DO UPDATE SET"
                " run_id=excluded.run_id, owner_pid=excluded.owner_pid,"
                " owner_instance_id=excluded.owner_instance_id,"
                " canonical_path=excluded.canonical_path,"
                " path_key=excluded.path_key,"
                " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at,"
                " wall_acquired_at=excluded.wall_acquired_at",
                (
                    want.key,
                    run_id,
                    self._identity.pid,
                    self._identity.instance_id,
                    want.path,
                    want.path_key,
                    now,
                    expires,
                    _now(),  # 真实墙钟：pid 复用判定必须用它，不能用可注入的逻辑时钟
                ),
            )
            acquired.append(
                Lease(
                    resource_key=want.key,
                    run_id=run_id,
                    owner_pid=self._identity.pid,
                    acquired_at=now,
                    expires_at=expires,
                    owner_instance_id=self._identity.instance_id,
                    canonical_path=want.path,
                )
            )
        return acquired

    def _is_owner(self, row: sqlite3.Row, run_id: str) -> bool:
        return (
            row["run_id"] == run_id
            and row["owner_instance_id"] == self._identity.instance_id
            and row["owner_pid"] == self._identity.pid
        )

    def _is_stale(self, row: sqlite3.Row, now: int | None = None) -> bool:
        """这条租约还能不能算"持有中"。

        三个条件任一成立即失效：
        1. 过期（TTL）；
        2. 进程已经不在；
        3. **pid 被复用了**——现在跑在那个 pid 上的进程比租约还晚启动。
           只看 1、2 会留一个洞：崩溃后 pid 很快被系统分给别的进程，
           `os.kill(pid, 0)` 会说"活着"，死租约就被当成活的，直到 TTL 到期。

        第 3 条刻意做成单向判断：只有"启动时间晚于租约获取时间"才算复用。
        合法持有者不可能比自己的租约晚启动，所以这条不会被时钟抖动误伤，
        代价只是检测偏保守（复用间隔小于容差时不接管，等 TTL）。
        宁可晚接管，也不能误接管——误接管就等于两个写者。
        """
        now = self._clock() if now is None else now
        if row["expires_at"] <= now:
            return True
        if not _pid_alive(row["owner_pid"]):
            return True
        acquired_wall = row["wall_acquired_at"] if "wall_acquired_at" in row.keys() else 0
        started = self._pid_started(row["owner_pid"])
        if started is not None and acquired_wall:
            if started > acquired_wall + PID_REUSE_MARGIN_MS:
                return True
        return False

    # ---- 校验 ----

    def is_held_by(
        self, resource: ResourceIdentity | Path | str, run_id: str
    ) -> bool:
        """当前是否仍由这个 run 的**本实例**有效持有。"""
        key = _normalize(resource).key
        row = self._conn.execute(
            "SELECT * FROM leases WHERE resource_key = ?", (key,)
        ).fetchone()
        if row is None or not self._is_owner(row, run_id):
            return False
        return not self._is_stale(row)

    def assert_held(
        self, resources: list[ResourceIdentity | Path | str], run_id: str
    ) -> None:
        """写操作之前必须调用。

        租约会过期、会被别人接管，所以"曾经拿到过"不等于"现在还持有"；
        别的进程拿着同一个 run_id 也不算持有。在执行侧校验，才能挡住
        "过期之后继续写"和"冒用 run_id 写"。
        """
        for resource in resources:
            if not self.is_held_by(resource, run_id):
                raise LeaseLost(
                    f"租约已失效或被接管：{_describe(resource)}（run={run_id}）"
                )

    def live_paths(self, run_id: str) -> list[str]:
        """本实例当前真正有效的租约路径（已排除过期/进程已死/pid 被复用）。"""
        rows = self._conn.execute(
            "SELECT * FROM leases WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [
            r["canonical_path"]
            for r in rows
            if r["canonical_path"]
            and self._is_owner(r, run_id)
            and not self._is_stale(r)
        ]

    def held_paths(self, run_id: str) -> list[str]:
        return self.live_paths(run_id)

    def assert_covers(
        self, resources: list[ResourceIdentity | Path | str], run_id: str
    ) -> None:
        """文件级目标是否被本实例持有的**目录租约**覆盖。

        为什么需要这个而不是 `assert_held`：租约是按"资源"（通常是工作区目录）
        取得的，而真正要写的是一条路径。目录租约授权的是"这棵子树"，所以这里
        只认**祖先方向**的覆盖——持有 `/ws/sub` 不授权写 `/ws/other`，
        持有 `/ws` 才授权写 `/ws/a.md`。

        过期、进程已死、pid 被复用的租约统统不算覆盖：`_is_stale` 已经把它们
        判成失效，这里不再重复一套判断。
        """
        paths = self.live_paths(run_id)
        held = [ResourceIdentity.of(Path(p)) for p in paths]
        for resource in resources:
            ident = _normalize(resource)
            covered = any(
                lease.key == ident.key
                or is_ancestor_or_self(lease.path_key, ident.path_key)
                for lease in held
            )
            if not covered:
                raise LeaseLost(
                    f"目标不在任何已持有的租约子树内：{_describe(resource)}"
                    f"（run={run_id}；持有 {paths}）"
                )

    # ---- 续租 / 释放 ----

    def renew(
        self,
        resources: list[ResourceIdentity | Path | str],
        run_id: str,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
    ) -> int:
        """续租。只续本实例持有的；有一个没续到就算心跳失败（抛错）。"""
        now = self._clock()
        expires = now + ttl_ms
        n = 0
        with self._log.transaction():
            for resource in resources:
                cur = self._conn.execute(
                    "UPDATE leases SET expires_at=?"
                    " WHERE resource_key=? AND run_id=? AND owner_instance_id=?"
                    "   AND owner_pid=?",
                    (
                        expires,
                        _normalize(resource).key,
                        run_id,
                        self._identity.instance_id,
                        self._identity.pid,
                    ),
                )
                n += cur.rowcount
            if n != len(resources):
                # 在事务内抛：续不满就是心跳失败，不能只续一半还当成功
                raise LeaseLost(
                    f"续租失败：期望 {len(resources)} 条，实际 {n} 条（run={run_id}）"
                )
        return n

    def release(
        self, resources: list[ResourceIdentity | Path | str], run_id: str
    ) -> int:
        """释放本实例持有的租约；别人的租约不能由我删掉。"""
        return self._delete(
            "resource_key=? AND run_id=? AND owner_instance_id=?",
            lambda resource: (
                _normalize(resource).key,
                run_id,
                self._identity.instance_id,
            ),
            resources,
        )

    def release_run(self, run_id: str, *, instance_id: str | None = None) -> int:
        """释放某个 Run 的全部租约。

        Run 收尾或被 fence 时必须调用，否则下一个 Run 会被自己的残留租约
        挡在门外，直到 TTL 到期。
        """
        instance = self._identity.instance_id if instance_id is None else instance_id
        with self._log.transaction():
            cur = self._conn.execute(
                "DELETE FROM leases WHERE run_id=? AND owner_instance_id=?",
                (run_id, instance),
            )
        return cur.rowcount

    def _delete(self, where: str, params: Callable[[Any], tuple], items: list) -> int:
        n = 0
        with self._log.transaction():
            for item in items:
                cur = self._conn.execute(
                    f"DELETE FROM leases WHERE {where}", params(item)
                )
                n += cur.rowcount
        return n

    # ---- 查询 ----

    def holder(self, resource: ResourceIdentity | Path | str) -> Lease | None:
        row = self._conn.execute(
            "SELECT * FROM leases WHERE resource_key = ?",
            (_normalize(resource).key,),
        ).fetchone()
        return self._row_to_lease(row) if row else None

    def live_holder(self, resource: ResourceIdentity | Path | str) -> Lease | None:
        """仍然有效的持有者。用于"别人是不是正在写这条路径"这类判断。

        过期的、进程已死的、pid 被复用的租约都不算——把它们当成"有人正在写"
        会让恢复永久卡在无法核实的状态上。
        """
        row = self._conn.execute(
            "SELECT * FROM leases WHERE resource_key = ?",
            (_normalize(resource).key,),
        ).fetchone()
        if row is None or self._is_stale(row):
            return None
        return self._row_to_lease(row)

    def list_for_run(self, run_id: str) -> list[Lease]:
        rows = self._conn.execute(
            "SELECT * FROM leases WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [self._row_to_lease(r) for r in rows]

    def list_all(self) -> list[Lease]:
        return [
            self._row_to_lease(r)
            for r in self._conn.execute("SELECT * FROM leases")
        ]

    @staticmethod
    def _row_to_lease(row: sqlite3.Row) -> Lease:
        keys = row.keys()
        return Lease(
            resource_key=row["resource_key"],
            run_id=row["run_id"],
            owner_pid=row["owner_pid"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            owner_instance_id=(
                row["owner_instance_id"] if "owner_instance_id" in keys else ""
            ),
            canonical_path=row["canonical_path"] if "canonical_path" in keys else "",
        )


def _describe(resource: ResourceIdentity | Path | str) -> str:
    if isinstance(resource, ResourceIdentity):
        return resource.path or resource.key
    return str(resource)


@dataclass(frozen=True)
class ExecutionGuard:
    path: str
    acquired_at: int


@contextlib.contextmanager
def execution_guard(
    data_root: Path,
    *,
    timeout_ms: int = GUARD_TIMEOUT_MS,
    poll_ms: int = 5,
):
    """跨进程执行守卫：受控文件操作、undo、epoch 切换的线性化点。

    为什么需要它：`assert_held` 是"读一下"，读完到真正写之间另一个进程
    可以接管。只有把"核验 + 开始执行"放进同一把 OS 锁里，接管才不可能
    插进中间。锁是 advisory 的，但本项目的写入方是同一份代码。

    释放由 OS 保证：进程硬退（os._exit）时内核仍会释放文件锁，
    所以不需要"清理死锁"逻辑（残租约另由 TTL 兜底）。
    """
    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    lock_path = data_root / "execution.guard"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GuardTimeout(
                        f"{timeout_ms}ms 内没拿到执行守卫：{lock_path}"
                    ) from None
                time.sleep(poll_ms / 1000)
        try:
            yield ExecutionGuard(str(lock_path), int(time.time() * 1000))
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = [
    "DEFAULT_TTL_MS",
    "HEARTBEAT_MS",
    "GUARD_TIMEOUT_MS",
    "ExecutionGuard",
    "GuardTimeout",
    "Lease",
    "LeaseConflict",
    "LeaseLost",
    "LeaseSet",
    "LeaseStore",
    "PID_REUSE_MARGIN_MS",
    "ProcessIdentity",
    "current_process",
    "execution_guard",
    "overlap_key",
    "pid_started_at",
]