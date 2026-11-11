"""版本化迁移与中断恢复（PRD §5.3.5 第 3 条）。

## 问题

事件库现在只有 `CREATE TABLE IF NOT EXISTS`：每个模块启动时自己补表/补列
（`ApprovalStore._ensure_column`、`LeaseStore._ensure_columns`……），**没有**
统一的结构版本号，也没有「迁移做到一半崩了怎么办」的答案。模块各补各的，
一个步骤失败就留下"有的表新、有的表旧"的混合状态，而没有任何记录能说明
"迁移到第几步、下一步该做什么"。

## 做法（不是假装两阶段提交）

1. **结构版本号**：`PRAGMA user_version` 是唯一权威。每一步在自己的事务里
   同时改 DDL 与 `user_version`——两者要么一起生效、要么一起回滚，不存在
   "表改了但版本没改"的窗口。
2. **步骤注册表**：`STEPS` 是有序的 `MigrationStep` 列表，`from_version → to_version`
   必须首尾相接。新增结构变化 = 追加一步，不改历史步骤。
3. **进度持久化**：`<data_root>/migration.json` 记录目标版本、当前步骤、
   已完成步骤、备份引用。每步落盘两次（开始 / 完成），都是原子替换。
4. **前置备份**：开始迁移/续做前，必须有一份 `backup.verify_backup()` 通过的
   备份；备份路径与摘要写进进度文件，续做时重新校验。没有备份就不动库。
5. **故障注入**：每步前/后调用 `crashpoint.failpoint`（`os._exit(77)`，仅
   `TIANSHU_FAILPOINT` 命中时生效），测试用子进程在中断后重启，断言可续做、
   最终版本一致、历史事件一个字节都没变。
6. **不支持版本拒绝写入**：`assert_writable()` 在版本更高（来自更新程序）、
   版本更低（尚未迁移）、或迁移未完成时一律拒绝，并给出恢复路径。**注意**：
   本轮没有把它接进 CLI / daemon（文件所有权在别的工作流），接入点与缺口
   写在 `docs/backup-restore.md`。

## 迁移不碰的东西

- **不 UPDATE / DELETE 历史事件**：步骤只做 DDL 与自有表写入；`events` 表的
  append-only 触发器保持原样。
- 不重排、不删除既有列（旧库补列仍由各 store 的 `_ensure_column` 负责）；
  迁移只负责"版本号 + 每个版本必须存在的新对象"。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backup import inspect_writers, verify_backup
from .crashpoint import failpoint
from .fsutil import atomic_write_bytes

STATE_NAME = "migration.json"
STATE_FORMAT = "tianshu-migration"
STATE_FORMAT_VERSION = 1

#: 当前程序支持的结构版本。历史库从 0 开始（基线只有 `CREATE TABLE IF NOT
#: EXISTS`，用户代码层没有版本号，全部按 0 处理）。
SCHEMA_VERSION = 2


class MigrationError(Exception):
    """迁移模块的域错误基类。"""


class UnsupportedSchemaVersion(MigrationError):
    """库结构版本高于本程序支持，或不在已知升级链上：拒绝按猜的语义继续。"""


class MigrationRefused(MigrationError):
    """前置条件不满足（没有备份、备份校验失败、有写者、状态文件损坏）。"""


class MigrationFailed(MigrationError):
    """某一步执行失败；已回滚该步事务，库停在上一步完成的版本。"""


class MigrationIncomplete(MigrationError):
    """存在未完成的迁移：拒绝写入，要求续做或从备份恢复。"""


class MigrationStateCorrupt(MigrationError):
    """`migration.json` 读不懂/与库版本自相矛盾：不猜，人工处置。"""


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


#: 迁移步骤注册表。追加步骤，不改历史步骤——历史步骤是别的机器上已经落地的
#: 事实；改它就等于给已升级的库安排第二次不同的语义。
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
            "CREATE INDEX IF NOT EXISTS idx_migration_log_applied"
            " ON migration_log(applied_at)",
        ),
    ),
)


@dataclass
class MigrationState:
    target_version: int
    status: str  # in_progress | completed
    applied_steps: list[int] = field(default_factory=list)
    current_step: int | None = None
    backup: dict[str, Any] = field(default_factory=dict)
    started_at_ms: int = 0
    updated_at_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": STATE_FORMAT,
            "format_version": STATE_FORMAT_VERSION,
            "target_version": self.target_version,
            "status": self.status,
            "applied_steps": list(self.applied_steps),
            "current_step": self.current_step,
            "backup": self.backup,
            "started_at_ms": self.started_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Any) -> MigrationState:
        if not isinstance(raw, dict):
            raise MigrationStateCorrupt("migration.json 必须是 JSON 对象")
        if raw.get("format") != STATE_FORMAT:
            raise MigrationStateCorrupt(
                f"migration.json 的 format 不认识：{raw.get('format')!r}"
            )
        if int(raw.get("format_version", 0)) != STATE_FORMAT_VERSION:
            raise MigrationStateCorrupt(
                f"migration.json 的 format_version 不支持：{raw.get('format_version')!r}"
            )
        status = raw.get("status")
        if status not in ("in_progress", "completed"):
            raise MigrationStateCorrupt(f"migration.json 的 status 非法：{status!r}")
        applied = raw.get("applied_steps")
        if not isinstance(applied, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in applied
        ):
            raise MigrationStateCorrupt("migration.json 的 applied_steps 必须是整数列表")
        current = raw.get("current_step")
        if current is not None and not isinstance(current, int):
            raise MigrationStateCorrupt("migration.json 的 current_step 必须是整数或 null")
        backup = raw.get("backup")
        if not isinstance(backup, dict):
            raise MigrationStateCorrupt("migration.json 的 backup 必须是对象")
        return cls(
            target_version=int(raw.get("target_version", -1)),
            status=status,
            applied_steps=[int(v) for v in applied],
            current_step=current,
            backup=backup,
            started_at_ms=int(raw.get("started_at_ms", 0)),
            updated_at_ms=int(raw.get("updated_at_ms", 0)),
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


# --------------------------------------------------------------------------- 版本


def db_path(data_root: Path) -> Path:
    return Path(data_root) / "events.db"


def state_path(data_root: Path) -> Path:
    return Path(data_root) / STATE_NAME


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(db: Path) -> int:
    """读 `PRAGMA user_version`。库打不开 → 域错误（不是"版本 0"）。"""
    if not Path(db).is_file():
        raise MigrationRefused(f"事件库不存在：{db}")
    try:
        conn = _connect(Path(db))
    except sqlite3.Error as exc:
        raise MigrationError(f"事件库打不开：{db}（{type(exc).__name__}: {exc}）") from exc
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error as exc:
        raise MigrationError(
            f"读不出事件库结构版本：{db}（{type(exc).__name__}: {exc}）"
        ) from exc
    finally:
        conn.close()


def read_state(data_root: Path) -> MigrationState | None:
    path = state_path(data_root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise MigrationStateCorrupt(
            f"migration.json 读不出来：{path}（{type(exc).__name__}: {exc}）。"
            "迁移进度不可判定：拒绝继续，先备份现场并人工核对。"
        ) from exc
    return MigrationState.from_dict(raw)


def plan_migrations(data_root: Path) -> list[MigrationStep]:
    """返回从当前版本到 `SCHEMA_VERSION` 还需要的步骤；版本不可知/更高则抛错。"""
    root = Path(data_root)
    current = read_schema_version(db_path(root))
    if current > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"库结构版本 {current} 高于本程序支持的 {SCHEMA_VERSION}："
            "不能按旧语义写入。请升级程序；或用匹配版本的备份恢复后再迁移。"
        )
    if current == SCHEMA_VERSION:
        return []
    steps: list[MigrationStep] = []
    version = current
    for step in STEPS:
        if step.to_version <= version:
            continue
        if step.from_version != version:
            raise UnsupportedSchemaVersion(
                f"没有从 v{version} 到 v{step.to_version} 的迁移步骤（注册表首尾不接）："
                "拒绝按猜的链继续；请用与库匹配的程序版本，或从备份恢复"
            )
        steps.append(step)
        version = step.to_version
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"迁移链只到 v{version}，达不到目标 v{SCHEMA_VERSION}：拒绝继续"
        )
    return steps


# --------------------------------------------------------------------------- 写者 / 备份前置


def _pid_alive(pid: Any) -> bool | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == "nt":
        return None  # 没有 Windows 实机：不假装能判定
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


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


def _require_backup(path: Path) -> dict[str, Any]:
    verification = verify_backup(path)
    if not verification.ok:
        raise MigrationRefused(
            "迁移前必须有校验通过的备份，但这份备份不通过："
            + "；".join(verification.problems)
            + "。处置建议："
            + "；".join(verification.remediation)
        )
    manifest = verification.manifest
    assert manifest is not None
    return {
        "path": str(Path(path).resolve()),
        "format_version": manifest.format_version,
        "db_user_version": manifest.db_user_version,
        "event_count": manifest.event_count,
    }


# --------------------------------------------------------------------------- 迁移


def _write_state(data_root: Path, state: MigrationState) -> None:
    state.updated_at_ms = int(time.time() * 1000)
    atomic_write_bytes(state_path(data_root), state.to_json_bytes())


def _apply_step(db: Path, step: MigrationStep) -> None:
    conn = _connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            step.run(conn)
            conn.execute(f"PRAGMA user_version = {int(step.to_version)}")
            # 只写两列：`note` 是 v2 才加的列，v1 还没建出来。迁移审计不依赖
            # 后加的列，步骤说明本来也写在注册表里。
            conn.execute(
                "INSERT OR REPLACE INTO migration_log(step, applied_at)"
                " VALUES (?, ?)",
                (int(step.to_version), int(time.time() * 1000)),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise MigrationFailed(
            f"步骤 v{step.from_version} → v{step.to_version} 失败并已回滚："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        conn.close()


def migrate(data_root: Path, *, backup: Path) -> MigrationReport:
    """把数据根的事件库迁移到 `SCHEMA_VERSION`。可续做。

    前置条件（不满足一律拒绝，不动库）：
    - 有事件库；
    - 没有活着的 daemon/host 写者（判不了也拒绝：unknown + 阻断）；
    - 有一份 `verify_backup()` 通过的备份。
    """
    root = Path(data_root).resolve()
    if not root.is_dir():
        raise MigrationRefused(f"数据根不存在或不是目录：{root}")
    db = db_path(root)
    if not db.is_file():
        raise MigrationRefused(f"数据根里没有事件库：{root}")

    writers = writers_active(root)
    if writers is not False:
        raise MigrationRefused(
            "迁移要求没有活着的写者，但"
            + ("检测到 daemon/host 仍在运行" if writers else "无法判定写者状态（unknown）")
            + "：先停止 host/daemon 再迁移，避免迁移与写入交错。"
        )

    current = read_schema_version(db)
    state = read_state(root)
    if state is not None and state.target_version != SCHEMA_VERSION:
        raise MigrationStateCorrupt(
            f"migration.json 记录的目标版本 {state.target_version} 与当前程序支持的 "
            f"{SCHEMA_VERSION} 不一致：这份进度属于另一套迁移计划。"
            "拒绝按猜的语义续做；请用与进度文件匹配的程序版本，或从备份恢复。"
        )
    if state is not None and state.status == "completed" and current != SCHEMA_VERSION:
        raise MigrationStateCorrupt(
            f"migration.json 声称已完成，但库结构版本是 v{current}（目标 v{SCHEMA_VERSION}）："
            "进度与事实矛盾，拒绝继续。先备份现场并人工核对。"
        )

    if state is not None and state.status == "in_progress":
        backup_info = _require_backup(Path(str(state.backup.get("path", ""))))
        resumed = True
        started_at = state.started_at_ms or int(time.time() * 1000)
    else:
        backup_info = _require_backup(Path(backup))
        resumed = False
        started_at = int(time.time() * 1000)

    steps = plan_migrations(root)
    if not steps:
        # 已经是最新版本：如果留下的是"完成但未收尾"的进度文件，补一次收尾。
        if state is not None and state.status != "completed":
            # 最后一步提交后、写"完成"之前崩溃：库版本已经是目标版本，
            # 把进度补成完成；applied_steps 以库版本为权威补齐。
            state.applied_steps = [
                step.to_version for step in STEPS if step.to_version <= current
            ]
            state.status = "completed"
            state.current_step = None
            _write_state(root, state)
        return MigrationReport(
            from_version=current,
            to_version=current,
            applied_steps=[],
            resumed=resumed,
            already_current=True,
            backup_path=backup_info["path"],
            event_count=_event_count(db),
        )

    state = state or MigrationState(
        target_version=SCHEMA_VERSION,
        status="in_progress",
        applied_steps=[],
        current_step=None,
        backup=backup_info,
        started_at_ms=started_at,
    )
    state.status = "in_progress"
    state.target_version = SCHEMA_VERSION
    state.backup = backup_info
    state.started_at_ms = started_at
    # 续做时把进度记录与库版本对齐：库版本是唯一权威。库里已经到达的版本
    # 一律算已完成（崩溃可能发生在"提交了 DDL 但没来得及写进度"的窗口）；
    # 库里没到、进度却声称完成的步骤不采信（那会让续做跳过一步）。
    state.applied_steps = sorted(
        {int(v) for v in state.applied_steps if int(v) <= current}
        | {step.to_version for step in STEPS if step.to_version <= current}
    )
    applied: list[int] = []
    for step in steps:
        state.current_step = step.to_version
        _write_state(root, state)
        failpoint(f"migration-before-step-{step.to_version}")
        _apply_step(db, step)
        failpoint(f"migration-after-step-{step.to_version}")
        applied.append(step.to_version)
        state.applied_steps = sorted(set(state.applied_steps) | {step.to_version})
        state.current_step = None
        _write_state(root, state)

    final = read_schema_version(db)
    if final != SCHEMA_VERSION:
        raise MigrationFailed(
            f"迁移结束后库结构版本是 v{final}，期望 v{SCHEMA_VERSION}："
            "拒绝声称成功；请从备份恢复后重试"
        )
    state.status = "completed"
    state.current_step = None
    _write_state(root, state)
    return MigrationReport(
        from_version=current,
        to_version=final,
        applied_steps=applied,
        resumed=resumed,
        already_current=False,
        backup_path=backup_info["path"],
        event_count=_event_count(db),
        warnings=[
            "备份前置要求见依赖链：迁移期间的每一步都在备份之后，"
            "失败可从该备份恢复",
        ],
    )


def _event_count(db: Path) -> int:
    conn = _connect(db)
    try:
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error:
        return -1
    finally:
        conn.close()


def resume_migration(data_root: Path) -> MigrationReport | None:
    """如果存在未完成迁移，按进度文件续做；没有未完成迁移返回 None。"""
    root = Path(data_root).resolve()
    state = read_state(root)
    if state is None or state.status != "in_progress":
        return None
    backup = Path(str(state.backup.get("path", "")))
    return migrate(root, backup=backup)


def assert_writable(data_root: Path) -> int:
    """写入前必须调的版本门。返回当前版本，或抛出带恢复路径的域错误。

    **本轮没有把它接进 CLI / daemon**（文件所有权不在本工作流）：接入点与
    缺口写在 `docs/backup-restore.md`。
    """
    root = Path(data_root).resolve()
    db = db_path(root)
    version = read_schema_version(db)
    state = read_state(root)
    if state is not None and state.status == "in_progress":
        raise MigrationIncomplete(
            f"迁移未完成（目标 v{state.target_version}，当前 v{version}）："
            "拒绝写入。续做：resume_migration(data_root)；或从迁移前备份恢复。"
        )
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"库结构版本 v{version} 高于本程序支持的 v{SCHEMA_VERSION}："
            "拒绝写入。请升级程序；或从匹配版本的备份恢复。"
        )
    if version < SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"库结构版本 v{version} 低于本程序要求的 v{SCHEMA_VERSION}："
            "拒绝写入。先创建备份并运行 migrate。"
        )
    return version


__all__ = [
    "SCHEMA_VERSION",
    "STATE_NAME",
    "MigrationError",
    "MigrationFailed",
    "MigrationIncomplete",
    "MigrationRefused",
    "MigrationReport",
    "MigrationState",
    "MigrationStateCorrupt",
    "MigrationStep",
    "STEPS",
    "UnsupportedSchemaVersion",
    "assert_writable",
    "db_path",
    "migrate",
    "plan_migrations",
    "read_schema_version",
    "read_state",
    "resume_migration",
    "state_path",
    "writers_active",
]
