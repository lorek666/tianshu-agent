"""事件日志：SQLite WAL 单提交点，append-only。

设计约束来自 docs/PRD-v1.md §5.3：
- `seq` 单调递增、不可跳号（SQLite AUTOINCREMENT，不复用已删除的 rowid）
- 无 UPDATE / DELETE（用触发器强制，不是靠自觉）
- payload > MAX_INLINE_BYTES 落盘，表内只留 payload_ref + payload_hash
- 恢复裁决只看本表，不看导出文件
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import EventLogCorrupt

MAX_INLINE_BYTES = 256 * 1024

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


class EventLog:
    """单 writer 的 append-only 事件日志。"""

    def __init__(self, db_path: Path, blob_dir: Path | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._blob_dir = blob_dir
        if blob_dir is not None:
            blob_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

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
    ) -> int:
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
            Path(ref).write_bytes(raw)

        cur = self._conn.execute(
            """
            INSERT INTO events
              (run_id, task_id, type, ts, actor_kind, actor_id,
               payload, payload_ref, payload_hash, parent_seq, idem_key)
            VALUES (?,?  ,?,    ?,     ?,          ?,        ?,       ?,          ?,           ?,          ?)
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
        return int(cur.lastrowid)

    # ---- 读 ----

    def read_run(self, run_id: str, *, verify: bool = True) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [self._row_to_event(r, verify=verify) for r in rows]

    def all_event_types(self) -> Iterable[str]:
        for r in self._conn.execute("SELECT DISTINCT type FROM events"):
            yield r["type"]

    def _row_to_event(self, row: sqlite3.Row, *, verify: bool) -> Event:
        if row["payload_ref"]:
            raw = Path(row["payload_ref"]).read_bytes()
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
        )

    def close(self) -> None:
        self._conn.close()
