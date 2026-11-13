"""事件日志：SQLite WAL 单提交点，append-only。

设计约束来自 docs/PRD-v1.md §5.3：
- `seq` 单调递增、不可跳号（SQLite AUTOINCREMENT，不复用已删除的 rowid）
- 无 UPDATE / DELETE（用触发器强制，不是靠自觉）
- `type` 是**封闭集合**：只有 `EVENT_TYPES` 里登记的类型允许写入（见下）
- payload > MAX_INLINE_BYTES 落盘，表内只留 payload_ref + payload_hash
- 恢复裁决只看本表，不看导出文件
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import event_schema
from .errors import EventLogCorrupt, TianshuError
from .event_schema import PayloadVersionUnavailable
from .fsutil import atomic_write_bytes
from .maintenance import DatabaseAccess, MaintenanceConnection, acquire_database_access

MAX_INLINE_BYTES = 256 * 1024

# ---------------------------------------------------------------- C3 类型注册表
#
# `seq` 与 `payload_hash` 只能证明"这条记录没被改过"，证明不了"这条记录的
# 类型是契约里承认的类型"。在加上这张表之前，`type` 是"代码写什么就是什么"：
# 一个拼错的类型名会原样进库，回放、审计、投影重建都发现不了。
#
# 规则：
# 1. 只有这里登记的类型允许写入。`EventLog.append` 在**任何副作用之前**校验，
#    未知类型抛 `TianshuError`（信息带上传入的类型名）。
# 2. 机器可校验的副本是 `schemas/c3/event.json` 的 `type.enum`；
#    `tests/test_events.py` 把两边绑在一起，改一处忘另一处会红。
# 3. 族 = 类型的名字空间前缀（PRD §5.3 的映射表按它排）。`receipt` 是唯一的
#    裸类型：历史名字，改名会让既有事件日志失去可比性，所以保留。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        # 审批（C4）
        "approval.requested",
        "approval.decided",
        "approval.revoked",
        "approval.consumed",
        "approval.expired",
        "approval.superseded",
        # 派发（C4）
        "dispatch.requested",
        "dispatch.admitted",
        "dispatch.rejected",
        "dispatch.revoked",
        # 执行守卫
        "execution.started",
        # 票据
        "grant.issued",
        "grant.consumed",
        # 作业队列
        "job.enqueued",
        "job.claimed",
        "job.requeued",
        "job.parked",
        "job.finished",
        # 模型（C1）
        "model.intent",
        "model.result",
        "model.error",
        # 策略判定
        "policy.denied",
        # 副作用凭据
        "receipt",
        # 恢复
        "recovery.blocked",
        "recovery.unblocked",
        # 阻断对象（G2 MCP 后续工单：对象级阻断）。与上面两个是**不同对象
        # 模型**：目录 vs `mcp://server/tool`。不能合用一个类型——目录回放会
        # 把 `mcp://…` 当成一个不存在的目录路径，那既是假阻断也是假解除。
        "recovery.object_blocked",
        "recovery.object_unblocked",
        # Run 生命周期
        "run.start",
        "run.state",
        "run.control",
        "run.cursor",
        "run.model_turn",
        "run.effect",
        "run.end",
        "run.finish",
        "run.fenced",
        # Task
        "task.created",
        "task.revised",
        # 子任务（G2 委派）：父 Run 侧的委派审计
        "subtask.spawned",
        "subtask.finished",
        # 工具（C2）
        "tool.intent",
        "tool.result",
        "tool.not_dispatched",
        # 撤销
        "undo.completed",
        "undo.failed",
    }
)

#: 注册表与逐类型 payload 契约必须一一对应：少一个类型 = 该类型无法写入
#: （fail-closed）。这里是导入期校验，测试里还会再绑一次（改一处忘一处会红）。
event_schema.ensure_registry(EVENT_TYPES)

#: 已定义、但当前**没有任何发射点**的类型。
#:
#: 单独列出而不是混进 `EVENT_TYPES`：注册表要回答"代码实际会写什么"。把一个
#: 没人写的类型登记成合法，等于让校验替它作证——那是另一种撒谎。
#:
#: 接上发射点时**必须**把它移进 `EVENT_TYPES`（同时改 `schemas/c3/event.json`
#: 与 PRD §5.3）。校验会先拒绝它，这不是障碍，而是刻意的：注册表不能悄悄变长。
#:
#: **当前为空。** 曾经唯一的成员 `job.lost` 已删除：`jobs.JOB_LOST` 常量存在过
#: 但全库零发射点，而"作业丢失"这件事已经由租约过期 + `job.parked` /
#: `job.requeued` 表达。留一个没人写的常量只会让人以为存在这种状态转移。
#: 机制保留给将来"先定义形状、后接发射点"的类型。
DECLARED_UNEMITTED_EVENT_TYPES: frozenset[str] = frozenset()

#: PRD §5.3 曾经声明过、但代码里**从未实现**的抽象族。
#:
#: 登记在这里是为了让"未实现"有一个机器可读的位置。它们不是事件类型前缀，
#: 作为事件类型在代码里零命中（`model.py` 的 `checkpoint` 是模型结束原因，
#: 与事件无关）；不写下来，读者会以为 §5.3 的九值枚举每个都有实现。
UNIMPLEMENTED_EVENT_FAMILIES: frozenset[str] = frozenset(
    {"compaction", "checkpoint", "audit"}
)


def _check_event_type(type_name: str) -> None:
    """写入前的类型校验。必须在产生任何副作用之前调用。"""
    if type_name in EVENT_TYPES:
        return
    hint = ""
    if type_name in DECLARED_UNEMITTED_EVENT_TYPES:
        hint = "（该类型已定义但当前没有发射点；接上发射点时必须移入 EVENT_TYPES）"
    raise TianshuError(
        f"未知事件类型：{type_name!r}。事件类型是封闭集合（C3），"
        f"新增类型必须同时登记 src/tianshu/events.py 的 EVENT_TYPES 与 "
        f"schemas/c3/event.json 的 type.enum{hint}"
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    task_id      TEXT,
    type         TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    actor_kind   TEXT    NOT NULL,
    actor_id     TEXT    NOT NULL DEFAULT '',
    payload      TEXT,
    payload_ref  TEXT,
    payload_hash TEXT    NOT NULL,
    parent_seq   INTEGER,
    idem_key     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

-- 阻断回放按 type 过滤（`guard.scan_blocked`），而它在每次写入前都要跑一遍
-- （派发 phase-1、执行守卫内、改文件前各一次；复审 中-1 之后是三次）。没有
-- 这个索引时每次都要扫全表：事件越写越多，单次写入就越慢。索引不改变事件
-- 类型集合（C3 封闭集），也不是契约的一部分，只是让"读阻断状态"不随日志增长。
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, seq);

-- payload 版本旁表：与事件行同一事务写入，不改 events 表、不 UPDATE 历史行。
-- 旧库升级后这张表是空的，历史行读取时按 version=0（冻结前旧版）映射。
CREATE TABLE IF NOT EXISTS event_versions (
    seq     INTEGER PRIMARY KEY,
    type    TEXT    NOT NULL,
    version INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden');
END;
"""


def canonical_bytes(payload: Any) -> bytes:
    """规范 JSON：键排序、无多余空白、保留中文。哈希与回放都依赖它。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Event:
    """一条事件。

    `parent_seq` 是**保留字段**：列还在（历史库里已经有这一列，删列要迁移），
    schema 里不是 required，当前没有任何写入点会传值——新写入恒为 `None`。
    保留的原因和"什么时候会真的用上"写在 PRD §5.3；一句话：现有的因果边都能
    从 payload（`call_id` / `intent_seq` / `run_id`/`seq` 顺序）推出来，接一套
    没人读的指针只会制造第二套事实源。
    """

    seq: int
    run_id: str
    task_id: str | None
    type: str
    ts: int
    actor_kind: str
    actor_id: str
    payload: dict[str, Any]
    payload_hash: str
    parent_seq: int | None
    idem_key: str | None
    #: payload 契约版本：0 = 冻结前旧版（历史行/旧形状），1 = 当前逐类型契约。
    #: 由 event_versions 旁表给出；旁表无记录的历史行按 0 读取（见 event_schema）。
    payload_version: int = 0


class EventTransaction:
    """一个已打开的写事务。

    用途：状态投影（tasks/runs/leases）与事件必须同一次提交。各服务不再自己
    新开连接、自己 `BEGIN`，否则两个服务无法组合成一个事务，只能写半份状态。
    """

    __slots__ = ("_log",)

    def __init__(self, log: EventLog) -> None:
        self._log = log

    @property
    def conn(self) -> sqlite3.Connection:
        return self._log.conn

    def append(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        actor_kind: str = "core",
        actor_id: str = "",
        parent_seq: int | None = None,
        idem_key: str | None = None,
        legacy_write: bool = False,
    ) -> int:
        return self._log.append(
            run_id,
            type,
            payload,
            task_id=task_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            parent_seq=parent_seq,
            idem_key=idem_key,
            legacy_write=legacy_write,
        )


class EventLog:
    """单 writer 的 append-only 事件日志。"""

    def __init__(
        self,
        db_path: Path,
        blob_dir: Path | None = None,
        *,
        cross_thread: bool = False,
    ) -> None:
        """`cross_thread=True` 只给"连接建在一个线程、用在同一线程"的通道用。

        sqlite3 默认禁止跨线程用同一个连接，因为并发读写连接内部状态会错乱。
        心跳线程的场景是例外：这条连接**只**由心跳线程使用（主线程用自己的
        那条），所以关掉这个检查是安全的。除此之外不要传它。
        """
        access = acquire_database_access(db_path)
        try:
            self._open_database(db_path, blob_dir, cross_thread, access)
        except BaseException:
            if hasattr(self, "_conn"):
                self._conn.close()
            else:
                access.close()
            raise

    def _open_database(
        self,
        db_path: Path,
        blob_dir: Path | None,
        cross_thread: bool,
        access: DatabaseAccess,
    ) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._blob_dir = blob_dir
        if blob_dir is not None:
            blob_dir.mkdir(parents=True, exist_ok=True)
        # 打开之前先记下"这个库本来就有内容"。0 字节文件按**新库**处理：
        # 首次启动可能在建文件之后、建 schema 之前崩掉，那种情况下没有任何
        # 历史可丢，判 corrupt 是误报。判据只看主库文件本身，不因为 `-wal`
        # 存在与否就换口径。
        pre_existing = False
        try:
            pre_existing = db_path.exists() and db_path.stat().st_size > 0
        except OSError:
            pre_existing = False
        self._conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,
            check_same_thread=not cross_thread,
            factory=MaintenanceConnection,
        )
        self._conn.database_access = access
        self._conn.row_factory = sqlite3.Row
        # 先读 schema、再设 journal_mode。
        #
        # 顺序是契约：`SELECT sqlite_master` 会触发 WAL 回放——"库里到底有没有
        # 历史"必须以回放结果为准。反过来先执行 `PRAGMA journal_mode=WAL`，
        # 在 WAL 不可回放时可能重建/丢弃日志文件，等于在检查证据之前先把证据
        # 毁了（实测：撕裂 WAL 被丢掉后，旧代码静默建出空 schema，历史全没）。
        #
        # 为什么必须在这里失败：文件存在且有内容、却读不出 `events` 表，
        # 说明事实源丢了，**不是一个新库**。继续 `executescript(_SCHEMA)`
        # 会把"读不出来"洗成"什么都没发生"——恢复会据此报告干净结论，而磁盘
        # 上的副作用可能已经发生（本地实测过）。这正是 "无法判定 ≠ 没有"
        # 那条口径：读不懂 = 域错误，交给人处理。
        try:
            has_schema = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
            ).fetchone() is not None
        except sqlite3.DatabaseError as exc:
            raise EventLogCorrupt(
                f"事件库读不出来：{db_path}（{type(exc).__name__}: {exc}）。"
                "这份日志不能作为结论来源。**不要删除 events.db**：先备份整个"
                "数据目录，再用 `tianshu recover` 核对，人工确认损失范围后再处理。"
            ) from exc
        if pre_existing and not has_schema:
            raise EventLogCorrupt(
                f"事件库文件存在但读不出 schema：{db_path}。"
                "通常意味着 WAL 被撕裂/损坏后历史已经丢失（这不是一个新库）。"
                "**不要删除 events.db**：先备份整个数据目录，再用 `tianshu recover`"
                " 核对，人工确认损失范围后再处理。"
            )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        # 有界的锁等待：两个进程同时 BEGIN IMMEDIATE 时，后来的那个等一小会
        # 儿再报 Busy，而不是把"别人正在写"当成"数据库坏了"。
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._tx_depth = 0

    @property
    def db_path(self) -> Path:
        """数据库文件路径。authority 根目录默认取它的父目录。"""
        return self._db_path

    @property
    def blob_dir(self) -> Path | None:
        """payload 超长时的落盘目录。心跳线程重开连接要沿用同一个。"""
        return self._blob_dir

    @property
    def conn(self) -> sqlite3.Connection:
        """给同库的其他存储（tasks/leases）复用同一连接。

        桌面单用户场景下，一个写者一个连接，避免多连接互相抢锁。
        """
        return self._conn

    @property
    def in_transaction(self) -> bool:
        return self._tx_depth > 0

    @contextlib.contextmanager
    def transaction(self) -> Iterator[EventTransaction]:
        """跨存储的可组合写事务。

        已在事务中时**不重复 BEGIN**（SQLite 不允许嵌套），而是复用外层事务；
        这样 TaskStore + LeaseStore + 事件可以一次提交，异常一起回滚。
        """
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield EventTransaction(self)
            finally:
                self._tx_depth -= 1
            return

        self._conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield EventTransaction(self)
        except BaseException:
            self._tx_depth = 0
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise
        self._tx_depth = 0
        try:
            self._conn.execute("COMMIT")
        except BaseException:
            # 提交失败时事务可能仍然开着；不清理会让下一次 BEGIN 直接报错。
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise

    # ---- 写 ----

    def append(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        actor_kind: str = "core",
        actor_id: str = "",
        parent_seq: int | None = None,
        idem_key: str | None = None,
        legacy_write: bool = False,
    ) -> int:
        # 第一行就校验：不能等 payload 落盘之后才发现类型是错的——
        # 那样会留下一个没有行的孤儿 blob。类型之后立刻校验 payload 契约
        # （PRD §5.3.5）：非法 payload 同样必须在任何副作用之前被拒。
        _check_event_type(type)
        # 默认只接受当前契约；`legacy_write=True` 只给夹具/历史播种/迁移测试，
        # 生产路径一个调用点都不许传（传了就等于给自己开写入免检通道）。
        payload_version = event_schema.detect_version(
            type, payload, allow_legacy=legacy_write
        )
        raw = canonical_bytes(payload)
        digest = sha256_hex(raw)

        inline: str | None = None
        ref: str | None = None
        if len(raw) <= MAX_INLINE_BYTES:
            inline = raw.decode("utf-8")
        else:
            if self._blob_dir is None:
                raise EventLogCorrupt(
                    f"payload {len(raw)}B exceeds inline limit and no blob_dir configured"
                )
            ref = str(self._blob_dir / digest)
            # 先完整持久化内容，再允许事务提交引用；数据库事务本身不覆盖文件。
            if not Path(ref).exists():
                atomic_write_bytes(Path(ref), raw)

        # 事件行与版本旁表必须同生同死：分开提交会留下"没有版本记录的新事件"，
        # 读取时会被当成 version=0，等于把新版语义伪装成旧版。
        with self.transaction():
            cur = self._conn.execute(
                """
                INSERT INTO events
                  (run_id, task_id, type, ts, actor_kind, actor_id,
                   payload, payload_ref, payload_hash, parent_seq, idem_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    type,
                    int(time.time() * 1000),
                    actor_kind,
                    actor_id,
                    inline,
                    ref,
                    digest,
                    parent_seq,
                    idem_key,
                ),
            )
            seq = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO event_versions (seq, type, version) VALUES (?,?,?)",
                (seq, type, payload_version),
            )
        return seq

    def append_many(self, items: list[dict[str, Any]]) -> list[int]:
        """同一事务内写多条事件。

        用途：result 与 receipt 必须同生同死——只写出 result 没写 receipt，
        就产生"记为已执行但无凭据"的窗口。
        """
        with self.transaction() as tx:
            return [tx.append(**item) for item in items]

    # ---- 读 ----

    def seqs_in_range(self, start: int, end: int) -> set[int]:
        """[start, end] 区间里实际存在的 seq。

        给回放校验用：判断"全库序号有没有缺口"不该让别的模块直接摸 `_conn`，
        那样每个用的人都在赌内部结构不变。
        """
        rows = self._conn.execute(
            "SELECT seq FROM events WHERE seq BETWEEN ? AND ?", (start, end)
        ).fetchall()
        return {int(r[0]) for r in rows}

    def _versions_for(self, seqs: list[int]) -> dict[int, int]:
        """seq → payload 版本。旁表写不出来的记录按历史行（0）读取。

        旁表**本身**缺失/损坏时不能把一切当成 0：那会把"读不懂精确版本"
        洗成"是旧版"。这里抛 `PayloadVersionUnavailable`，调用方必须显式
        处理（阻断依赖精确版本的读取），不猜。
        """
        if not seqs:
            return {}
        lo, hi = min(seqs), max(seqs)
        try:
            rows = self._conn.execute(
                "SELECT seq, version FROM event_versions WHERE seq BETWEEN ? AND ?",
                (lo, hi),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PayloadVersionUnavailable(
                f"事件 payload 版本旁表读不出来（{self._db_path}）："
                f"{type(exc).__name__}: {exc}。无法判定精确版本，"
                "不能把所有事件都按旧版解释"
            ) from exc
        known = {int(r["seq"]): int(r["version"]) for r in rows}
        return {
            seq: known.get(seq, event_schema.LEGACY_PAYLOAD_VERSION) for seq in seqs
        }

    def read_run(self, run_id: str, *, verify: bool = True) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        versions = self._versions_for([int(r["seq"]) for r in rows])
        return [
            self._row_to_event(r, verify=verify, version=versions[int(r["seq"])])
            for r in rows
        ]

    def seq_head(self) -> int:
        """全库最大 seq（空库为 0）。推送游标与 `event_seq_head` 用。

        只读、不解释语义：它回答"最后一条事件是几号"，不回答"有没有缺口"。
        缺口校验是 `seqs_in_range` 的事。
        """
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def read_after(self, seq: int, *, limit: int | None = None) -> list[Event]:
        """取 `seq > after` 的全局事件（跨 run），按 seq 升序。

        为什么需要它：推送补洞的游标是**全库**序号，而 `read_run` 只按 run 过滤。
        让 daemon 自己去 `_conn` 上拼 SQL 就等于把表结构变成第二个公开 API。

        `limit` 为 None 时一次读完（调用方自己保证有界）；给了就必须为正。
        """
        if limit is None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq", (int(seq),)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (int(seq), max(1, int(limit))),
            ).fetchall()
        versions = self._versions_for([int(r["seq"]) for r in rows])
        return [
            self._row_to_event(r, verify=True, version=versions[int(r["seq"])])
            for r in rows
        ]

    def read_types(self, types: Iterable[str]) -> list[Event]:
        """按类型读事件（跨 run）。投影重建用。"""
        wanted = list(types)
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE type IN ({marks}) ORDER BY seq", wanted
        ).fetchall()
        versions = self._versions_for([int(r["seq"]) for r in rows])
        return [
            self._row_to_event(r, verify=True, version=versions[int(r["seq"])])
            for r in rows
        ]

    def all_event_types(self) -> Iterable[str]:
        for r in self._conn.execute("SELECT DISTINCT type FROM events"):
            yield r["type"]

    def _row_to_event(
        self, row: sqlite3.Row, *, verify: bool, version: int
    ) -> Event:
        # 不认识的版本 = 读不懂未来数据，绝不按当前 schema 猜着解释。
        event_schema.schema_for(row["type"], version)
        if row["payload_ref"]:
            ref = row["payload_ref"]
            try:
                raw = Path(ref).read_bytes()
            except OSError as exc:
                # 外部 blob 缺失/不可读是**日志损坏**，不是调用方传错了路径。
                # 裸 FileNotFoundError 会被读成"我自己 IO 用错了"，而且不带 seq，
                # 回放/恢复拿不到"坏的是哪一条事件"；统一收敛成域错误。
                raise EventLogCorrupt(
                    f"seq={row['seq']} payload blob 缺失或不可读：{ref}"
                    f"（{type(exc).__name__}: {exc}）"
                ) from exc
        else:
            raw = row["payload"].encode("utf-8")

        actual = sha256_hex(raw)
        if verify and actual != row["payload_hash"]:
            # 验收 A8：篡改必须被发现，不能静默通过
            raise EventLogCorrupt(
                f"seq={row['seq']} payload_hash 不符："
                f"记录 {row['payload_hash'][:12]}…，实际 {actual[:12]}…"
            )

        return Event(
            seq=row["seq"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            type=row["type"],
            ts=row["ts"],
            actor_kind=row["actor_kind"],
            actor_id=row["actor_id"],
            payload=json.loads(raw.decode("utf-8")),
            payload_hash=row["payload_hash"],
            parent_seq=row["parent_seq"],
            idem_key=row["idem_key"],
            payload_version=version,
        )

    def close(self) -> None:
        self._conn.close()
