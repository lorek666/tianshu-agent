"""真实事件库的只读准入前缀；不装配store、不执行迁移。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import event_schema
from .data_state import (
    SCHEMA_VERSION,
    AdmissionRefused,
    LegacyStateV1,
    MigrationState,
    MigrationStateCorrupt,
    MigrationStateV2,
    associated_state_paths,
    classify_progress,
    read_state_file,
)
from .errors import TianshuError

SUPPORTED_MEMORY_SCHEMA_VERSION = 3  # 测试绑定memory版本，不导入业务store。


@dataclass(frozen=True)
class AdmissionSnapshot:
    db_path: str
    mechanism_version: int
    progress: MigrationState | None
    is_new: bool


def _incompatible(message: str) -> AdmissionRefused:
    return AdmissionRefused("incompatible", message)


def _has_material(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    pending = [path]
    while pending:
        entry = pending.pop()
        if entry.is_symlink() or not entry.is_dir():
            return True
        pending.extend(entry.iterdir())
    return False


def check_new_file_evidence(db_path: Path) -> None:
    """SQLite打开前排除孤立旁文件；读取空主库可能丢弃它们。"""
    if str(db_path) == ":memory:":
        return
    try:
        if db_path.stat().st_size > 0:
            return
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _incompatible("无法检查原数据库文件") from exc
    try:
        actual = db_path.resolve()
        for path in associated_state_paths(actual):
            if path.is_symlink() or path.exists():
                raise AdmissionRefused("migration_incomplete", "新空主库旁存在迁移历史")
        for suffix in ("-wal", "-journal", "-shm"):
            path = actual.with_name(actual.name + suffix)
            if path.is_symlink() or (path.exists() and path.stat().st_size > 0):
                raise _incompatible(f"新空主库旁存在历史{suffix}")
        if actual.name == "events.db":
            for name in ("artifacts", "blobs", "memory_blobs"):
                path = actual.parent / name
                # SnapshotStore可先建空目录骨架；任何文件/链接仍是历史证据。
                if _has_material(path):
                    raise _incompatible(f"新空事件库旁存在历史材料{name}")
    except (OSError, RuntimeError) as exc:
        raise _incompatible("新空库的历史证据不可判定") from exc


def _shape(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    # 标识符只取代码常量。双引号SELECT会回退为字符串，先核真实列。
    kind = conn.execute("SELECT type FROM sqlite_schema WHERE name=?", (table,)).fetchone()
    if kind is None or kind[0] != "table":
        raise _incompatible(f"事件库缺少可读的{table}表")
    present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if not set(columns) <= present:
        raise _incompatible(f"{table}缺少必要列")
    quoted = ",".join('"' + col + '"' for col in columns)
    conn.execute(f'SELECT {quoted} FROM "{table}" LIMIT 0')


def _versions(conn: sqlite3.Connection) -> None:
    _shape(
        conn,
        "events",
        (
            "seq",
            "run_id",
            "task_id",
            "type",
            "ts",
            "actor_kind",
            "actor_id",
            "payload",
            "payload_ref",
            "payload_hash",
            "parent_seq",
            "idem_key",
        ),
    )
    _shape(conn, "event_versions", ("seq", "type", "version"))
    for seq, kind, version, actual_kind in conn.execute(
        "SELECT v.seq,v.type,v.version,e.type FROM event_versions v "
        "LEFT JOIN events e ON e.seq=v.seq"
    ):
        if type(seq) is not int or seq <= 0 or type(kind) is not str or kind != actual_kind:
            raise _incompatible("已登记payload版本没有匹配的真实事件")
        try:
            supported = event_schema.known_versions(kind)
        except TianshuError as exc:
            raise _incompatible("已登记事件类型没有可识别的payload版本") from exc
        if type(version) is not int or version not in supported:
            raise _incompatible(f"{kind}已登记payload版本不受支持：{version!r}")
    exists = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_meta'").fetchone()
    if exists is not None:
        _shape(conn, "memory_meta", ("key", "value"))
        rows = list(conn.execute("SELECT value FROM memory_meta WHERE key='schema_version'"))
        allowed = {str(i) for i in range(SUPPORTED_MEMORY_SCHEMA_VERSION + 1)}
        if len(rows) > 1 or any(type(row[0]) is not str or row[0] not in allowed for row in rows):
            raise _incompatible("记忆投影已登记schema_version不受支持或类型畸形")


def _mechanism(conn: sqlite3.Connection, version: int) -> None:
    table = conn.execute("SELECT type FROM sqlite_schema WHERE name='migration_log'").fetchone()
    if version == 0:
        if table is not None:
            raise _incompatible("v0存在未登记的迁移机制对象")
        return
    if table is None or table[0] != "table":
        raise _incompatible("迁移机制表缺失")
    columns = [tuple(row[1:]) for row in conn.execute("PRAGMA table_xinfo(migration_log)")]
    expected = [("step", "INTEGER", 0, None, 1, 0), ("applied_at", "INTEGER", 1, None, 0, 0)]
    if version == 2:
        expected.append(("note", "TEXT", 1, "''", 0, 0))
    if columns != expected:
        raise _incompatible("migration_log列/约束与实际机制版本不符")
    indices = {row[1]: row for row in conn.execute("PRAGMA index_list(migration_log)")}
    index = indices.get("idx_migration_log_applied")
    if version == 1:
        if index is not None:
            raise _incompatible("v1含尚未登记的step2索引")
    else:
        if index is None or index[2] != 0 or index[4] != 0:
            raise _incompatible("v2迁移索引缺失或形状不符")
        if [row[2] for row in conn.execute("PRAGMA index_info(idx_migration_log_applied)")] != [
            "applied_at"
        ]:
            raise _incompatible("v2迁移索引目标不符")
        keys = [
            tuple(row[2:])
            for row in conn.execute("PRAGMA index_xinfo(idx_migration_log_applied)")
            if row[5]
        ]
        if keys != [("applied_at", 0, "BINARY", 1)]:
            raise _incompatible("v2迁移索引排序/比较规则不符")
    names = "step,applied_at,note" if version == 2 else "step,applied_at"
    rows = list(conn.execute(f"SELECT {names} FROM migration_log ORDER BY step"))
    if tuple(row[0] for row in rows) != tuple(range(1, version + 1)):
        raise _incompatible("迁移审计缺少完整已提交步骤或有额外步骤")
    for row in rows:
        if type(row[0]) is not int or type(row[1]) is not int or row[1] < 0:
            raise _incompatible("迁移审计步骤/时间类型不合法")
        if version == 2 and type(row[2]) is not str:
            raise _incompatible("迁移审计note类型不合法")


def _progress_for(db_path: Path, actual: int) -> MigrationState | None:
    records = []
    for i, path in enumerate(associated_state_paths(db_path)):
        state = read_state_file(path)
        if state is None:
            continue
        if (
            i == 0
            and type(state) is not MigrationStateV2
            or i == 1
            and type(state) is not LegacyStateV1
        ):
            raise MigrationStateCorrupt("进度格式放在错误的sidecar位置")
        if isinstance(state, MigrationStateV2):
            if state.db_path != str(db_path):
                raise MigrationStateCorrupt("进度绑定的不是实际打开文件")
            classify_progress(state, actual)
        elif state.status == "completed" and actual != state.target_version:
            raise MigrationStateCorrupt("旧完成进度与实际版本矛盾")
        records.append(state)
    if len(records) > 1 and any(s.status != "completed" for s in records):
        raise MigrationStateCorrupt("两份关联进度不具有共同completed事实")
    # 每份先独立严格验证；completed只共享完成到v2事实，不比较历史时间/backup。
    return records[0] if records else None


def inspect_connection(conn: sqlite3.Connection, db_path: Path) -> AdmissionSnapshot:
    """底层只读观察；迁移可检查合法中间态，正常策略另在check执行。"""
    try:
        row = next(row for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        memory = str(db_path) == ":memory:"
        actual = ":memory:" if memory else str(db_path.resolve())
        if (memory and row[2] != "") or (not memory and Path(row[2]).resolve() != Path(actual)):
            raise _incompatible("连接的main数据库与待检查实际文件不符")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if type(version) is not int or version not in range(SCHEMA_VERSION + 1):
            raise _incompatible(f"不支持的机制版本{version!r}")
        objects = list(
            conn.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
        )
        is_new = not objects and version == 0
        if is_new and not memory and Path(actual).stat().st_size > 0:
            raise _incompatible("已有非空事件库缺少事件schema")
        if not is_new:
            _versions(conn)
            _mechanism(conn, version)
        progress = None if memory else _progress_for(Path(actual), version)
        return AdmissionSnapshot(actual, version, progress, is_new)
    except (sqlite3.Error, OSError, RuntimeError, StopIteration) as exc:
        raise _incompatible(f"实际数据库的只读准入证据不可读（{type(exc).__name__}）") from exc


def check_connection(conn: sqlite3.Connection, db_path: Path) -> AdmissionSnapshot:
    snapshot = inspect_connection(conn, db_path)
    if snapshot.progress is not None and snapshot.progress.status == "in_progress":
        raise AdmissionRefused("migration_incomplete", "存在未完成迁移，须显式离线续做")
    if snapshot.mechanism_version == 1:
        raise AdmissionRefused("migration_incomplete", "v1是迁移中间版本")
    if snapshot.mechanism_version == 2 and snapshot.progress is None:
        raise AdmissionRefused("migration_incomplete", "v2缺少合法completed进度")
    return snapshot
