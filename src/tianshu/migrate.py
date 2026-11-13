"""标准根离线迁移：完整一致点绑定、单连接事务与严格可达续做。

0→2只改变迁移机制对象；普通v0兼容归data_admission，恢复apply另行实现。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .backup import MANIFEST_NAME, _connect_saved_readonly, inspect_writers, verify_backup
from .crashpoint import failpoint
from .data_admission import check_new_file_evidence, inspect_connection
from .data_digest import (
    MATERIAL_ALGORITHM,
    MECHANISM_NAMES,
    SQLITE_ALGORITHM,
    MaterialRecord,
    _mechanism_snapshot,
    material_digest,
    material_inventory,
    mechanism_digest,
    sqlite_digest,
)
from .data_state import (
    SCHEMA_VERSION,
    AdmissionRefused,
    BackupBinding,
    BaselineDigest,
    MigrationState,
    MigrationStateCorrupt,
    MigrationStateV2,
    associated_state_paths,
    classify_progress,
    encode_state,
)
from .data_state import STATE_FORMAT as STATE_FORMAT
from .data_state import STATE_FORMAT_VERSION as STATE_FORMAT_VERSION
from .fsutil import atomic_write_bytes
from .maintenance import MaintenanceBusy, MaintenanceError, acquire_database_access
from .pathkeys import overlap_key

STATE_NAME = "events.db.migration.json"
MigrationError = AdmissionRefused


class UnsupportedSchemaVersion(AdmissionRefused):
    def __init__(self, message):
        super().__init__("incompatible", message)


class MigrationRefused(AdmissionRefused):
    def __init__(self, message, code="backup_mismatch"):
        super().__init__(code, message)


class MigrationFailed(AdmissionRefused):
    def __init__(self, message):
        super().__init__("migration_failed", message)


class MigrationIncomplete(AdmissionRefused):
    def __init__(self, message):
        super().__init__("migration_incomplete", message)


@dataclass(frozen=True)
class MigrationStep:
    """一步迁移。

    `statements` 在**同一个事务**里执行，并和 `PRAGMA user_version = to_version`
    一起提交。步骤必须是幂等/可重放的（续做时可能重跑一次未提交的步骤）。
    """

    from_version: int
    to_version: int
    description: str
    statements: tuple[str, ...]
    apply: Callable[[sqlite3.Connection], None] | None = None

    def run(self, conn: sqlite3.Connection) -> None:
        if self.apply is not None:
            self.apply(conn)
            return
        for statement in self.statements:
            conn.execute(statement)


STEPS: tuple[MigrationStep, ...] = (
    MigrationStep(
        from_version=0,
        to_version=1,
        description="建立迁移审计表 migration_log（记录每次成功迁移的步骤与时间）",
        statements=(
            "CREATE TABLE IF NOT EXISTS migration_log ("
            " step INTEGER PRIMARY KEY,"
            " applied_at INTEGER NOT NULL"
            ")",
        ),
    ),
    MigrationStep(
        from_version=1,
        to_version=2,
        description="给 migration_log 增加 note 列并建索引（验证 ALTER 步骤也能中断续做）",
        statements=(
            "ALTER TABLE migration_log ADD COLUMN note TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_migration_log_applied ON migration_log(applied_at)",
        ),
    ),
)


@dataclass
class MigrationReport:
    from_version: int
    to_version: int
    applied_steps: list[int]
    resumed: bool
    already_current: bool
    backup_path: str
    event_count: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "applied_steps": list(self.applied_steps),
            "resumed": self.resumed,
            "already_current": self.already_current,
            "backup_path": self.backup_path,
            "event_count": self.event_count,
            "warnings": list(self.warnings),
        }

    def to_text(self) -> str:
        if self.already_current and not self.applied_steps:
            return f"库结构已是当前版本 v{self.to_version}，无需迁移。"
        action = "续做并完成" if self.resumed else "完成"
        return (
            f"迁移{action}：v{self.from_version} → v{self.to_version}；"
            f"应用步骤 {self.applied_steps or '（无）'}；"
            f"备份 {self.backup_path}；事件 {self.event_count} 条未改动。"
        )


def _pid_alive(pid: Any) -> bool | None:
    """pid 是否还活着。`None` = 判不了（不猜）。

    POSIX 用 `os.kill(pid, 0)`；Windows 不能这么判（`os.kill` 在那边是
    TerminateProcess 语义），改用 `OpenProcess(SYNCHRONIZE)` +
    `WaitForSingleObject(0)`——这是零依赖（`ctypes` 标准库）且能在
    windows-latest 上被测试证的经验证据；仍**没有 Windows 实机**。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _pid_alive_windows(pid: int) -> bool | None:
    """Windows 存活判定：句柄拿不到（拒绝访问）按"活着"保守处理。"""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):  # pragma: no cover - 非 Windows 不会走到
        return None
    synchronize = 0x00100000
    wait_timeout = 0x00000102
    wait_object_0 = 0x00000000
    error_access_denied = 5
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover - 同上
        return None
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        if ctypes.get_last_error() == error_access_denied:
            return True
        return None
    try:
        code = kernel32.WaitForSingleObject(handle, 0)
    finally:
        kernel32.CloseHandle(handle)
    if code == wait_timeout:
        return True
    if code == wait_object_0:
        return False
    return None


def writers_active(data_root: Path) -> bool | None:
    """只读判断有没有活着的写者。`True` = 有；`False` = 没有；`None` = 判不了。"""
    info = inspect_writers(data_root)
    if info.get("ipc_probe") is True:
        return True
    unknown = False
    for key in ("daemon_lock", "host_lock"):
        record = info.get(key) or {}
        if record.get("present") and record.get("unreadable"):
            unknown = True
            continue
        if not record.get("present"):
            continue
        alive = _pid_alive(record.get("pid"))
        if alive is True:
            return True
        if alive is None:
            unknown = True
    if unknown:
        return None
    return False


def db_path(data_root: Path) -> Path:
    return Path(data_root) / "events.db"


def state_path(data_root: Path) -> Path:
    return Path(data_root) / STATE_NAME


def _connect(db: Path) -> sqlite3.Connection:
    check_new_file_evidence(db)
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(db: Path) -> int:
    if not Path(db).is_file():
        raise MigrationRefused("事件库不存在", "incompatible")
    try:
        with closing(_connect(Path(db))) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        raise UnsupportedSchemaVersion("无法读取真实机制版本") from exc


def read_state(data_root: Path) -> MigrationState | None:
    db = db_path(data_root).resolve()
    if not any(p.exists() or p.is_symlink() for p in associated_state_paths(db)):
        return None
    with closing(_connect(db)) as conn:
        return inspect_connection(conn, db).progress


def _plan(current: int) -> list[MigrationStep]:
    if type(current) is not int or current not in (0, 1, 2):
        raise UnsupportedSchemaVersion(f"库版本{current}高于或不在支持机制链")
    version = 0
    for step in STEPS:
        if step.from_version != version or step.to_version != version + 1 or step.apply is not None:
            raise UnsupportedSchemaVersion("注册步骤不是可冻结的连续SQL链")
        version = step.to_version
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion("迁移链达不到目标版本")
    return [step for step in STEPS if step.to_version > current]


def plan_migrations(data_root: Path) -> list[MigrationStep]:
    return _plan(read_schema_version(db_path(data_root)))


def _plan_digest() -> str:
    _plan(0)
    raw = {
        "sqlite_algorithm": SQLITE_ALGORITHM,
        "material_algorithm": MATERIAL_ALGORITHM,
        "excluded_objects": MECHANISM_NAMES,
        "steps": [[s.from_version, s.to_version, s.description, s.statements] for s in STEPS],
    }
    return hashlib.sha256(
        json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _no_links(path: Path) -> None:
    for part in [path.absolute(), *path.absolute().parents]:
        if part.is_junction():
            raise MigrationRefused("不支持junction维护布局", "incompatible")
        if part.is_symlink():
            # macOS系统固定别名，不借此放行用户根或根内链接。
            system_alias = (
                sys.platform == "darwin"
                and part in (Path("/var"), Path("/tmp"), Path("/etc"))
                and part.resolve() == Path("/private") / part.name
            )
            if not system_alias:
                raise MigrationRefused("不支持符号链接维护布局", "incompatible")


def _layout(data_root: Path) -> tuple[Path, Path]:
    supplied = Path(data_root).absolute()
    _no_links(supplied)
    _no_links(supplied / "events.db")
    root = supplied.resolve()
    db = root / "events.db"
    try:
        info = db.lstat()
        if (
            not root.is_dir()
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or db.resolve().parent != root
        ):
            raise MigrationRefused("迁移要求标准根内单硬链接普通events.db", "incompatible")
    except OSError as exc:
        raise MigrationRefused("标准根布局不可判定", "incompatible") from exc
    # 不跟随材料链接，且在写进度前验证完整布局。
    material_inventory(root)
    return root, db


def _backup(path: Path, root: Path):
    _no_links(Path(path))
    saved = Path(path).resolve()
    verification = verify_backup(saved)
    if not verification.ok or verification.manifest is None:
        raise MigrationRefused("备份校验未通过：" + "；".join(verification.problems))
    manifest = verification.manifest
    # source_path是保存时canonical事实；不跟随活源后来替换的链接重解释。
    if overlap_key(manifest.source_path) != overlap_key(str(root)):
        raise MigrationRefused("备份源数据根与实际文件不匹配")
    raw = (saved / MANIFEST_NAME).read_bytes()
    return saved, manifest, hashlib.sha256(raw).hexdigest()


def _material_point(root: Path, saved: Path, manifest) -> str:
    try:
        live = material_inventory(root)
        actual = material_inventory(saved)
        declared = tuple(
            sorted(
                MaterialRecord(f.path, f.size, f.sha256)
                for f in manifest.files
                if f.path != "events.db"
            )
        )
        if len({r.path for r in declared}) != len(declared) or live != actual or actual != declared:
            raise MigrationRefused("备份与活根材料清单双向不相等")
        return material_digest(live)
    except AdmissionRefused as exc:
        if exc.code == "backup_mismatch":
            raise
        raise MigrationRefused("备份材料一致点不可判定") from exc


def _write_state(root: Path, state: MigrationStateV2) -> MigrationStateV2:
    state = replace(state, updated_at_ms=max(state.updated_at_ms, int(time.time() * 1000)))
    try:
        atomic_write_bytes(state_path(root), encode_state(state))
    except OSError as exc:
        raise MigrationFailed("进度持久化失败，不能声称完成") from exc
    return state


def _apply_step(conn: sqlite3.Connection, step: MigrationStep) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] != step.from_version:
                raise MigrationFailed("执行步骤与当前真实版本不相符")
            step.run(conn)
            conn.execute(f"PRAGMA user_version={step.to_version}")
            conn.execute(
                "INSERT INTO migration_log(step,applied_at) VALUES(?,?)",
                (step.to_version, int(time.time() * 1000)),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error as exc:
        raise MigrationFailed("步骤DDL/version/audit事务失败并回滚") from exc


def _reachable_mechanism(live: sqlite3.Connection, saved: sqlite3.Connection, origin: int) -> None:
    """从保存起点仅重演已提交机制步骤；继承audit原值不可改写。"""
    current = _mechanism_snapshot(live)
    initial = _mechanism_snapshot(saved)
    if initial["version"] != origin or current["version"] < origin:
        raise MigrationRefused("备份真实起始机制版本不匹配")
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as expected:
        # 只复制机制定义/行，不调用业务initializer或真实迁移回调。
        for kind, _name, _owner, sql in initial["objects"]:
            if kind == "table":
                expected.execute(sql)
        for row in initial["rows"]:
            expected.execute(
                "INSERT INTO migration_log VALUES(" + ",".join("?" for _ in row) + ")", row
            )
        for kind, _name, _owner, sql in initial["objects"]:
            if kind == "index":
                expected.execute(sql)
        expected.execute(f"PRAGMA user_version={origin}")
        for step in _plan(origin):
            if step.to_version > current["version"]:
                break
            step.run(expected)
            expected.execute(f"PRAGMA user_version={step.to_version}")
            audit = next(r for r in current["rows"] if r[0] == step.to_version)
            expected.execute("INSERT INTO migration_log(step,applied_at) VALUES(?,?)", audit[:2])
        if mechanism_digest(expected) != mechanism_digest(live):
            raise MigrationRefused("真实机制变化不只来自已提交注册步骤或继承审计被改写")


def _check_bound(conn, source, root, saved, manifest, raw_hash, state):
    if state.plan_sha256 != _plan_digest():
        raise MigrationStateCorrupt("进度属于另一套迁移计划")
    if state.backup.path != str(saved) or state.backup.manifest_sha256 != raw_hash:
        raise MigrationRefused("原备份路径或manifest字节绑定不符")
    if source.execute("PRAGMA user_version").fetchone()[0] != state.from_version:
        raise MigrationRefused("备份真实版本不等于绑定from_version")
    business = sqlite_digest(conn)
    if business != state.baseline.sqlite_sha256 or sqlite_digest(source) != business:
        raise MigrationRefused("业务逻辑内容已离开绑定基线")
    material = _material_point(root, saved, manifest)
    if material != state.baseline.material_sha256:
        raise MigrationRefused("材料已离开绑定基线")
    _reachable_mechanism(conn, source, state.from_version)


def _run_locked(root, db, backup):
    root, db = _layout(root)
    with closing(_connect(db)) as conn:
        _plan(conn.execute("PRAGMA user_version").fetchone()[0])
        snapshot = inspect_connection(conn, db)
        state = snapshot.progress
        if state is not None and type(state) is not MigrationStateV2:
            raise MigrationIncomplete("旧格式不能授予续做许可")
        if snapshot.mechanism_version == 2 and (state is None or state.status != "completed"):
            # 合法最后一步commit窗口仍可按format2续做；无记录/legacy不能补造。
            if state is None:
                raise MigrationIncomplete("v2缺少format2 completed或合法续做证据")
        if state is not None and state.plan_sha256 != _plan_digest():
            raise MigrationStateCorrupt("进度属于另一套迁移计划")
        saved, manifest, raw_hash = _backup(backup, root)
        with closing(_connect_saved_readonly(saved / "events.db")) as source:
            inspect_connection(source, saved / "events.db")
            origin = snapshot.mechanism_version
            if state is not None:
                position = classify_progress(state, origin)
                fresh = state.status == "completed" and str(saved) != state.backup.path
                if fresh:
                    if source.execute("PRAGMA user_version").fetchone()[0] != 2 or mechanism_digest(
                        source
                    ) != mechanism_digest(conn):
                        raise MigrationRefused("新备份与当前完整机制不相等")
                    if sqlite_digest(source) != sqlite_digest(conn):
                        raise MigrationRefused("新备份与当前业务逻辑不相等")
                    _material_point(root, saved, manifest)
                else:
                    _check_bound(conn, source, root, saved, manifest, raw_hash, state)
                if state.status == "completed":
                    return MigrationReport(
                        2,
                        2,
                        [],
                        False,
                        True,
                        str(saved),
                        conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    )
                resumed = True
                if position == "after_step":
                    state = _write_state(
                        root,
                        replace(
                            state,
                            applied_steps=state.applied_steps + (state.current_step,),
                            current_step=None,
                        ),
                    )
            else:
                if source.execute("PRAGMA user_version").fetchone()[
                    0
                ] != origin or mechanism_digest(source) != mechanism_digest(conn):
                    raise MigrationRefused("首次绑定真实版本或完整机制起点不相等")
                business = sqlite_digest(conn)
                if sqlite_digest(source) != business:
                    raise MigrationRefused("备份不是当前业务一致点")
                materials = _material_point(root, saved, manifest)
                now = int(time.time() * 1000)
                state = _write_state(
                    root,
                    MigrationStateV2(
                        str(db),
                        origin,
                        SCHEMA_VERSION,
                        "in_progress",
                        (),
                        None,
                        _plan_digest(),
                        BackupBinding(str(saved), raw_hash),
                        BaselineDigest(SQLITE_ALGORITHM, business, MATERIAL_ALGORITHM, materials),
                        now,
                        now,
                    ),
                )
                resumed = False
            applied = []
            for step in _plan(conn.execute("PRAGMA user_version").fetchone()[0]):
                state = _write_state(root, replace(state, current_step=step.to_version))
                failpoint(f"migration-before-step-{step.to_version}")
                _apply_step(conn, step)
                failpoint(f"migration-after-step-{step.to_version}")
                applied.append(step.to_version)
                state = _write_state(
                    root,
                    replace(
                        state,
                        applied_steps=state.applied_steps + (step.to_version,),
                        current_step=None,
                    ),
                )
            inspect_connection(conn, db)
            _check_bound(conn, source, root, saved, manifest, raw_hash, state)
            state = _write_state(root, replace(state, status="completed", current_step=None))
            return MigrationReport(
                origin,
                SCHEMA_VERSION,
                applied,
                resumed,
                False,
                str(saved),
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            )


def migrate(data_root: Path, *, backup: Path) -> MigrationReport:
    try:
        root, db = _layout(data_root)
        with acquire_database_access(db, exclusive=True):
            writers = writers_active(root)
            if writers is not False:
                raise MigrationRefused(
                    "写者仍活跃" if writers else "无法判定写者状态", "maintenance_busy"
                )
            return _run_locked(root, db, backup)
    except MaintenanceBusy as exc:
        raise AdmissionRefused("maintenance_busy", "实际数据库存在合作式连接") from exc
    except MaintenanceError as exc:
        raise AdmissionRefused("incompatible", "数据库维护准入不可判定") from exc
    except (OSError, sqlite3.Error) as exc:
        raise MigrationFailed(f"迁移读写失败（{type(exc).__name__}）") from exc


def resume_migration(data_root: Path) -> MigrationReport | None:
    state = read_state(data_root)
    if state is None:
        return None
    if type(state) is not MigrationStateV2:
        raise MigrationIncomplete("旧格式不能续做")
    return migrate(data_root, backup=Path(state.backup.path))


def assert_writable(data_root: Path) -> int:
    """保留旧专用严格helper；普通v0连接不使用它。"""
    version = read_schema_version(db_path(data_root))
    state = read_state(data_root)
    if state is not None and state.status == "in_progress":
        raise MigrationIncomplete("迁移未完成，要求显式续做")
    if version < SCHEMA_VERSION:
        raise UnsupportedSchemaVersion("版本低于迁移目标")
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion("版本高于迁移目标")
    if state is None:
        raise MigrationIncomplete("缺少完成进度")
    return version
