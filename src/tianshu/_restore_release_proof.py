"""Private, transaction-local Restore C R2 commitment; not an admission credential."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
from collections.abc import Callable

from .data_digest import _quote

_TABLE = "_restore_release_r2_proof"
_DDL = f"""CREATE TABLE {_TABLE} (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    release_intent_sha256 TEXT NOT NULL,
    state_sha256 TEXT NOT NULL
)"""
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseProofError(ValueError):
    """Cannot establish the exact committed R2 state."""


class ReleaseProofIntentMismatch(ReleaseProofError):
    """The stored witness belongs to a different R1 intent."""


def _row_hash(
    update: Callable[[bytes], None],
    conn: sqlite3.Connection,
    relation: str,
    columns: list[str],
    *,
    clause: str,
    args: tuple = (),
) -> None:
    # Storage tags are taken from SQLite. CAST only TEXT to BLOB to preserve the
    # exact stored bytes, including noncanonical text and embedded zero bytes.
    expressions = ",".join(
        f"typeof({c}), CASE typeof({c}) WHEN 'text' THEN CAST({c} AS BLOB) ELSE {c} END"
        for c in columns
    )
    label = relation.encode("utf-8")
    update(b"T" + struct.pack(">Q", len(label)) + label)
    for row in conn.execute(f"SELECT {expressions} FROM {relation} {clause}", args):
        update(b"R")
        for i in range(0, len(row), 2):
            tag, val = row[i], row[i + 1]
            if tag == "null" and val is None:
                update(b"N")
            elif tag == "integer" and type(val) is int:
                update(b"I" + struct.pack(">q", val))
            elif tag == "real" and type(val) is float:
                update(b"F" + struct.pack(">d", val))
            elif tag in {"text", "blob"} and type(val) is bytes:
                update((b"S" if tag == "text" else b"B") + struct.pack(">Q", len(val)))
                update(val)
            else:
                raise ReleaseProofError("SQLite 值与其存储类型不符")
        update(b";")
    update(b"E")


def _hash_table(
    update: Callable[[bytes], None],
    conn: sqlite3.Connection,
    name: str,
    *,
    without_rowid: bool,
    seq_head: int | None = None,
) -> None:
    table = _quote(name)
    info = list(conn.execute(f"PRAGMA main.table_xinfo({table})"))
    if not info or any(r[6] == 1 for r in info):
        raise ReleaseProofError(f"持久表 {name!r} 的列不可完整读取")
    columns = [_quote(r[1]) for r in info]
    if without_rowid:
        pk = sorted((r[5], _quote(r[1])) for r in info if r[5])
        if not pk:
            raise ReleaseProofError(f"WITHOUT ROWID 表 {name!r} 缺少主键")
        ordering = ",".join(f"{col} COLLATE BINARY" for _, col in pk)
    else:
        names = {r[1].lower() for r in info}
        rowid = next((n for n in ("rowid", "_rowid_", "oid") if n not in names), None)
        if rowid is None:
            raise ReleaseProofError(f"持久表 {name!r} 的 rowid 不可寻址")
        columns.insert(0, rowid)
        ordering = rowid
    prefix = "WHERE seq<=? " if seq_head is not None else ""
    _row_hash(
        update,
        conn,
        f"main.{table}",
        columns,
        clause=f"{prefix}ORDER BY {ordering}",
        args=(seq_head,) if seq_head is not None else (),
    )


def prefix_digest(conn: sqlite3.Connection, seq_head: int) -> str:
    """Compare the historical rows and version side table before/after fresh R2."""
    digest = hashlib.sha256(b"tianshu-restore-release-prefix-v1\0")
    for table in ("events", "event_versions"):
        _hash_table(digest.update, conn, table, without_rowid=False, seq_head=seq_head)
    return digest.hexdigest()


def state_digest(
    conn: sqlite3.Connection, release_intent_sha256: str, *, include_witness: bool = False
) -> str:
    """Hash a single caller-owned SQLite snapshot in stable typed row order."""
    if not conn.in_transaction:
        raise ReleaseProofError("主库见证必须在同一 SQLite 快照内计算")
    digest = hashlib.sha256(b"tianshu-restore-release-r2-v1\0")
    digest.update(bytes.fromhex(release_intent_sha256))
    for pragma in ("user_version", "application_id", "encoding"):
        value = conn.execute(f"PRAGMA main.{pragma}").fetchone()[0]
        digest.update(f"{pragma}={value}\0".encode("ascii"))
    _row_hash(
        digest.update,
        conn,
        "main.sqlite_schema",
        ["type", "name", "tbl_name", "sql"],
        clause="ORDER BY type COLLATE BINARY, name COLLATE BINARY",
    )
    flags = {
        row[1]: (row[2], row[4]) for row in conn.execute("PRAGMA main.table_list")
        if row[0] == "main"
    }
    for (name,) in conn.execute(
        "SELECT name FROM main.sqlite_schema WHERE type='table' ORDER BY name COLLATE BINARY"
    ):
        if name == _TABLE and not include_witness:
            continue  # Its schema is covered; its sole row commits to the rest.
        kind, without_rowid = flags.get(name, (None, None))
        if kind == "virtual":
            continue  # No own stored rows; persistent shadow tables are covered below.
        if kind not in {"table", "shadow"}:
            raise ReleaseProofError(f"持久表 {name!r} 不可辨识")
        _hash_table(digest.update, conn, name, without_rowid=bool(without_rowid))
    return digest.hexdigest()


def _proof_table_present(conn: sqlite3.Connection) -> bool:
    objects = [
        tuple(row)
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "WHERE name=? COLLATE NOCASE OR tbl_name=? COLLATE NOCASE",
            (_TABLE, _TABLE),
        )
    ]
    if not objects:
        return False
    if objects != [("table", _TABLE, _TABLE, _DDL)]:
        raise ReleaseProofError("R2 事务见证表结构损坏")
    return True


def _read_proof(conn: sqlite3.Connection) -> tuple[str, str]:
    rows = conn.execute(
        f"SELECT singleton,release_intent_sha256,state_sha256 FROM main.{_TABLE} LIMIT 2"
    ).fetchall()
    if not rows:
        raise ReleaseProofError("缺少 R2 事务见证行，不能补写")
    singleton, intent_sha, state_sha = rows[0]
    if (
        len(rows) != 1 or type(singleton) is not int or singleton != 1
        or type(intent_sha) is not str or not _SHA256.fullmatch(intent_sha)
        or type(state_sha) is not str or not _SHA256.fullmatch(state_sha)
    ):
        raise ReleaseProofError("R2 事务见证行损坏")
    return intent_sha, state_sha


def record(conn: sqlite3.Connection, release_intent_sha256: str) -> str:
    """Write only from exact untouched BindingV1, inside the R2 mutation transaction."""
    if not conn.in_transaction:
        raise ReleaseProofError("R2 事务见证不得脱离收口事务写入")
    if _proof_table_present(conn):
        # Legal backups may inherit the previous batch's now-stale witness.
        prior_intent, _ = _read_proof(conn)
        if prior_intent == release_intent_sha256:
            raise ReleaseProofIntentMismatch("未对账现场已存在同一 R1 的 R2 见证")
    else:
        conn.execute(_DDL)
    state_sha = state_digest(conn, release_intent_sha256)
    conn.execute(
        f"INSERT INTO main.{_TABLE} VALUES (1,?,?) ON CONFLICT(singleton) "
        "DO UPDATE SET release_intent_sha256=excluded.release_intent_sha256, "
        "state_sha256=excluded.state_sha256",
        (release_intent_sha256, state_sha),
    )
    return state_sha


def verify(conn: sqlite3.Connection, release_intent_sha256: str) -> str:
    if not _proof_table_present(conn):
        raise ReleaseProofError("缺少 R2 事务见证表，不能补写")
    intent_sha, state_sha = _read_proof(conn)
    if intent_sha != release_intent_sha256:
        raise ReleaseProofIntentMismatch("R2 事务见证不属于当前 release_intent")
    if state_digest(conn, intent_sha) != state_sha:
        raise ReleaseProofError("主库精确状态已偏离 R2 事务见证")
    return state_sha
