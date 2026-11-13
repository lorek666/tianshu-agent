"""迁移一致点算法v1；只读SQLite与材料，不装配业务store。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import struct
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .data_state import AdmissionRefused

SQLITE_ALGORITHM = "tianshu-sqlite-logical-v1"
MATERIAL_ALGORITHM = "tianshu-material-inventory-v1"
MECHANISM_NAMES = ("migration_log", "idx_migration_log_applied")


def _refuse(message: str) -> AdmissionRefused:
    return AdmissionRefused("incompatible", message)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )


def _atom(value: object) -> list:
    if value is None:
        return ["null"]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float:
        return ["float64", struct.pack(">d", value).hex()]
    if type(value) is str:
        return ["text", value]
    if type(value) is bytes:
        return ["blob", value.hex()]
    raise _refuse(f"不支持的SQLite值类型{type(value).__name__}")


@contextmanager
def _snapshot(conn: sqlite3.Connection):
    own = not conn.in_transaction
    try:
        if own:
            conn.execute("BEGIN")
        integrity = [r[0] for r in conn.execute("PRAGMA main.integrity_check")]
        if integrity != ["ok"]:
            raise _refuse("SQLite integrity_check未通过")
        yield
    except (sqlite3.Error, UnicodeError, ValueError) as exc:
        raise _refuse(f"完整SQLite摘要不可判定（{type(exc).__name__}）") from exc
    finally:
        if own and conn.in_transaction:
            conn.rollback()


def _mechanism_snapshot(conn: sqlite3.Connection) -> dict:
    """经形状核验后才能从business摘要排除的精确对象。"""
    version = conn.execute("PRAGMA main.user_version").fetchone()[0]
    objects = [
        tuple(r)
        for r in conn.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "WHERE name IN (?,?) ORDER BY type,name",
            MECHANISM_NAMES,
        )
    ]
    if version not in (0, 1, 2):
        raise _refuse("未知迁移机制版本")
    if version == 0:
        if objects:
            raise _refuse("v0存在未登记机制对象")
        return {"version": 0, "objects": [], "columns": [], "rows": []}
    if not any(r[:3] == ("table", "migration_log", "migration_log") for r in objects):
        raise _refuse("迁移机制表缺失")
    columns = [tuple(r) for r in conn.execute("PRAGMA main.table_xinfo(migration_log)")]
    expected = [(0, "step", "INTEGER", 0, None, 1, 0), (1, "applied_at", "INTEGER", 1, None, 0, 0)]
    if version == 2:
        expected.append((2, "note", "TEXT", 1, "''", 0, 0))
    if columns != expected:
        raise _refuse("迁移机制列形状不符")
    indices = {r[1]: tuple(r) for r in conn.execute("PRAGMA main.index_list(migration_log)")}
    index = indices.get(MECHANISM_NAMES[1])
    if version == 1 and index is not None:
        raise _refuse("v1包含未来机制索引")
    if version == 2:
        if index is None or index[2] != 0 or index[4] != 0:
            raise _refuse("v2机制索引不符")
        keys = [
            tuple(r[2:])
            for r in conn.execute("PRAGMA main.index_xinfo(idx_migration_log_applied)")
            if r[5]
        ]
        if keys != [("applied_at", 0, "BINARY", 1)]:
            raise _refuse("v2机制索引目标/排序不符")
    rows = [tuple(r) for r in conn.execute("SELECT * FROM migration_log ORDER BY step")]
    if tuple(r[0] for r in rows) != tuple(range(1, version + 1)):
        raise _refuse("迁移审计步骤不完整")
    if any(
        type(r[0]) is not int
        or type(r[1]) is not int
        or r[1] < 0
        or (version == 2 and type(r[2]) is not str)
        for r in rows
    ):
        raise _refuse("迁移审计类型不符")
    return {"version": version, "objects": objects, "columns": columns, "rows": rows}


def mechanism_digest(conn: sqlite3.Connection) -> str:
    with _snapshot(conn):
        raw = _mechanism_snapshot(conn)
        raw["rows"] = [[_atom(v) for v in row] for row in raw["rows"]]
        return hashlib.sha256(_encode(raw)).hexdigest()


def sqlite_digest(conn: sqlite3.Connection) -> str:
    """全部持久schema/typed行；hidden=1是虚表查询控制列，仅记录元信息。

    generated列读取值，FTS虚拟行及所有shadow持久表各自读取。rowid必须可寻址；
    三个别名全部被遮蔽时拒绝，而不是漏掉。复用外部事务，不提交/回滚调用者。
    """
    with _snapshot(conn):
        _mechanism_snapshot(conn)
        objects = [
            tuple(r)
            for r in conn.execute(
                "SELECT type,name,tbl_name,sql FROM main.sqlite_schema ORDER BY type,name"
            )
            if r[1] not in MECHANISM_NAMES
        ]
        flags = {
            r[1]: (r[2], r[4]) for r in conn.execute("PRAGMA main.table_list") if r[0] == "main"
        }
        tables = []
        for kind, name, _owner, _sql in objects:
            if kind != "table":
                continue
            columns = [
                tuple(r) for r in conn.execute("PRAGMA main.table_xinfo(" + _quote(name) + ")")
            ]
            if not columns or name not in flags:
                raise _refuse("持久表列元信息不可读")
            table_kind, without = flags[name]
            if any(c[6] == 1 for c in columns) and table_kind != "virtual":
                raise _refuse("不支持的持久hidden列")
            names = {c[1].casefold() for c in columns}
            rowid = None
            if not without:
                rowid = next((n for n in ("rowid", "_rowid_", "oid") if n not in names), None)
                if rowid is None:
                    raise _refuse("rowid所有别名被遮蔽，不能生成完整摘要")
            selected = ([rowid] if rowid else []) + [_quote(c[1]) for c in columns if c[6] != 1]
            rows = sorted(
                _encode([_atom(v) for v in row]).decode("ascii")
                for row in conn.execute(
                    "SELECT " + ",".join(selected) + " FROM main." + _quote(name)
                )
            )
            tables.append({"name": name, "columns": columns, "rowid": bool(rowid), "rows": rows})
        return hashlib.sha256(
            _encode({"algorithm": SQLITE_ALGORITHM, "objects": objects, "tables": tables})
        ).hexdigest()


@dataclass(frozen=True, order=True)
class MaterialRecord:
    path: str
    size: int
    sha256: str


def material_inventory(root: Path) -> tuple[MaterialRecord, ...]:
    root = Path(root)
    records = []
    try:
        for name in ("artifacts", "blobs", "memory_blobs"):
            top = root / name
            if not top.exists() and not top.is_symlink():
                continue
            pending = [top]
            while pending:
                path = pending.pop()
                info = path.lstat()
                if (
                    path.is_symlink()
                    or path.is_junction()
                    or not path.resolve().is_relative_to(root.resolve())
                ):
                    raise _refuse("材料链接或根外布局不支持迁移")
                if stat.S_ISDIR(info.st_mode):
                    pending.extend(path.iterdir())
                elif stat.S_ISREG(info.st_mode):
                    digest = hashlib.sha256()
                    size = 0
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                    records.append(
                        MaterialRecord(path.relative_to(root).as_posix(), size, digest.hexdigest())
                    )
                else:
                    raise _refuse("材料不是普通文件或目录")
        return tuple(sorted(records))
    except (OSError, RuntimeError, ValueError) as exc:
        raise _refuse(f"材料清单不可判定（{type(exc).__name__}）") from exc


def material_digest(records: tuple[MaterialRecord, ...]) -> str:
    if tuple(sorted(records)) != records or len({r.path for r in records}) != len(records):
        raise _refuse("材料清单不规范或重复")
    return hashlib.sha256(
        _encode({"algorithm": MATERIAL_ALGORITHM, "files": [asdict(r) for r in records]})
    ).hexdigest()
