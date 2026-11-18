"""保守离线安全垃圾回收（W15 v2 · CONTRACT-W15-RETENTION §6）。

严格遵守十一条红线与离线独占契约：
1. 默认只读预演（未传 `--approve` 或显式 `--dry-run`）：只读 URI 连接 +
   `PRAGMA query_only=ON`，零文件修改、零 SQL 写语句，退出码 `3`。
2. 显式批准（`--approve`）：持 `acquire_restore_database_access(..., exclusive=True)`
   独占准入、核验无存活 `daemon`/`host`/`worker`、核验非恢复 `held` 隔离态、
   并在独占锁内**重新运行** `collect_references(root)`（零 TOCTOU）。任何引用
   无法核验（`ReferenceUnknown`）、存在未完成撤销日志（`in_progress`）或准入不通过，
   一律 **fail-closed（0 文件删除、0 数据库删除，退出码 `2`）**。
3. 低磁盘豁免：不套用新副作用 Run 的 2 GiB `DiskAdmission` 门槛，确保 `< 2 GiB`
   甚至 `0` 剩余字节场景仍能安全清理孤儿 blob 回收空间。
4. 绝对禁删面：`events`、`event_versions`、`summary_*`、`dispatches`、
   `dispatch_requests`、`dispatch_meta`、`approvals`、`grants`、`runs`、`tasks`、
   `subtasks`、`memories`、`memory_*`、`content_*`、`budget_accounts`、
   `budget_limits`、`budget_counters`、非空快照清单（`changes != []`）、
   `artifacts/undo/*.json`、`protected_blobs` 及一切 `state != 'settled'` 或
   `known == 0` 的 `budget_reservations` 一律 **0 删除、0 改写**。
"""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backup import (
    ReferenceIndex,
    ReferenceUnknown,
    _checked_file,
    _fsync_dir,
    _has_sqlite_companions,
    _rel_posix,
    collect_references,
    read_saved_event_payload,
)
from .budget import PARTIAL, PENDING, RELEASED, SETTLED, UNKNOWN
from .credentials import redact
from .data_admission import check_connection, check_new_file_evidence
from .data_state import AdmissionRefused
from .diagnostics_export import redact_diagnostic_text
from .disk_admission import free_bytes
from .dispatcher import (
    DISPATCH_ERROR,
    DISPATCH_EXECUTING,
    DISPATCH_FINISHED,
    DISPATCH_INTENT,
    DISPATCH_ROW_REJECTED,
    DISPATCH_ROW_REVOKED,
)
from .errors import TianshuError
from .jobs import (
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_DONE,
    JOB_PENDING,
    JOB_WAITING,
    TERMINAL_JOB_STATES,
)
from .maintenance import MaintenanceBusy, MaintenanceError
from .migrate import writers_active
from .platform_identity import NativeEntityError, open_entity
from .restore_access import (
    RestoreAccessError,
    RestoreScopeBusy,
    _active_scope_blocks,
    _registry_factory,
    acquire_restore_database_access,
    verify_sqlite_main,
)
from .restore_registry import PresentRegistryObservation, RestoreRegistryError
from .snapshot import SnapshotStore
from .tasks import EFFECT_STATES, NON_TERMINAL_STATES, RUN_STATES, SUBTASK_TERMINAL_STATES

DEFAULT_OLDER_THAN_DAYS = 90
DAY_MS = 86_400_000
#: 删除 SQLite 行所需的最小 WAL/页缓存余量（64 KiB）；孤儿文件 unlink 需 0 字节。
MIN_DB_GC_HEADROOM_BYTES = 65_536

TERMINAL_RUN_STATES: frozenset[str] = frozenset(RUN_STATES - NON_TERMINAL_STATES)
ALL_JOB_STATES: frozenset[str] = frozenset(
    {JOB_PENDING, JOB_CLAIMED, JOB_WAITING, JOB_DONE, JOB_CANCELLED}
)
ACTIVE_DISPATCH_STATES: frozenset[str] = frozenset({DISPATCH_INTENT, DISPATCH_EXECUTING})
ALL_DISPATCH_STATES: frozenset[str] = frozenset(
    {
        DISPATCH_INTENT,
        DISPATCH_EXECUTING,
        DISPATCH_FINISHED,
        DISPATCH_ROW_REVOKED,
        DISPATCH_ROW_REJECTED,
        DISPATCH_ERROR,
    }
)
ALL_RESERVATION_STATES: frozenset[str] = frozenset(
    {PENDING, SETTLED, PARTIAL, UNKNOWN, RELEASED}
)

GC_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "invalid_arguments",
        "incompatible",
        "migration_incomplete",
        "progress_corrupt",
        "maintenance_busy",
        "restore_held",
        "reference_unknown",
        "undo_in_progress",
        "gc_failed",
    }
)

_RESTORE_CONTROL_FILES: tuple[str, ...] = (
    ".restore-origin.json",
    ".restore-origin.json.tmp",
    ".restore-token.json",
    ".restore-token.json.tmp",
)


class GcRefused(TianshuError):
    """离线安全 GC 前置条件未满足，0 删除闭锁拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        if code not in GC_FAILURE_CODES:
            raise ValueError(f"未知 GC 错误码：{code!r}")
        self.code = code
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class OrphanBlobCandidate:
    path: str
    size: int
    category: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "category": self.category}


@dataclass(frozen=True)
class EmptySnapshotCandidate:
    path: str
    run_id: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "run_id": redact(self.run_id),
            "size": self.size,
        }


@dataclass(frozen=True)
class GcCandidateSet:
    orphan_blobs: tuple[OrphanBlobCandidate, ...]
    empty_snapshots: tuple[EmptySnapshotCandidate, ...]
    job_ids: tuple[str, ...]
    reservation_ids: tuple[str, ...]
    protected_blobs_count: int
    non_empty_snapshots_count: int
    undo_journals_count: int

    @property
    def orphan_blobs_bytes(self) -> int:
        return sum(item.size for item in self.orphan_blobs)

    @property
    def empty_snapshots_bytes(self) -> int:
        return sum(item.size for item in self.empty_snapshots)

    @property
    def reclaimable_file_bytes(self) -> int:
        return self.orphan_blobs_bytes + self.empty_snapshots_bytes


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redact_text(text: str, extra_paths: Sequence[str]) -> str:
    return redact_diagnostic_text(text, extra_paths=extra_paths)


def _no_user_links(path: Path) -> None:
    for part in [path.absolute(), *path.absolute().parents]:
        if part.is_junction():
            raise GcRefused("incompatible", "不支持 junction 维护布局")
        if part.is_symlink():
            system_alias = (
                sys.platform == "darwin"
                and part in (Path("/var"), Path("/tmp"), Path("/etc"))
                and part.resolve() == Path("/private") / part.name
            )
            if not system_alias:
                raise GcRefused("incompatible", "不支持符号链接维护布局")


def _validate_root_layout(data_root: Path) -> tuple[Path, Path]:
    supplied = Path(data_root).absolute()
    _no_user_links(supplied)
    if not supplied.exists() or not supplied.is_dir():
        raise GcRefused("incompatible", "数据根不存在或不是目录")
    db_supplied = supplied / "events.db"
    _no_user_links(db_supplied)
    root = supplied.resolve()
    db = root / "events.db"
    try:
        info = db.lstat()
    except FileNotFoundError as exc:
        raise GcRefused("incompatible", "数据根缺少 events.db") from exc
    except OSError as exc:
        raise GcRefused("incompatible", f"无法检查 events.db 状态：{exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size <= 0
        or db.resolve().parent != root
    ):
        raise GcRefused("incompatible", "events.db 必须是非空单硬链接普通文件")
    try:
        check_new_file_evidence(db)
    except AdmissionRefused as exc:
        raise _map_admission_error(exc) from exc
    return root, db


def _map_admission_error(exc: AdmissionRefused) -> GcRefused:
    if exc.code in ("migration_incomplete", "progress_corrupt", "maintenance_busy"):
        return GcRefused(exc.code, str(exc))
    return GcRefused("incompatible", str(exc))


def _check_not_restore_held(root: Path, db: Path) -> None:
    """拒绝处于未终结恢复范围（held / staging）的数据根。"""
    try:
        db_handle = open_entity(db, "file")
        try:
            db_identity = db_handle.identity
        finally:
            db_handle.close()
        observed = _registry_factory().observe()
        if _active_scope_blocks(observed, db, db_identity):
            raise GcRefused("restore_held", "数据根落入尚未终结的恢复批次范围")
    except GcRefused:
        raise
    except RestoreScopeBusy as exc:
        raise GcRefused("restore_held", str(exc)) from exc
    except (RestoreRegistryError, RestoreAccessError, NativeEntityError) as exc:
        raise GcRefused("incompatible", f"恢复范围核验失败：{exc}") from exc

    has_control_marker = any(
        (root / name).exists() or (root / name).is_symlink()
        for name in _RESTORE_CONTROL_FILES
    )
    if has_control_marker:
        released = False
        if isinstance(observed, PresentRegistryObservation):
            histories: dict[object, list[Any]] = {}
            for event in observed.events:
                histories.setdefault(event.batch_id, []).append(event)
            for events in histories.values():
                if events and events[-1].kind == "released":
                    released = True
        if not released:
            raise GcRefused("restore_held", "数据根存在未解除的恢复隔离标记（held）")

    try:
        parent = root.parent
        if parent.is_dir():
            prefix = f".{root.name}.restore-"
            for entry in parent.iterdir():
                if entry.name.startswith(prefix):
                    raise GcRefused("restore_held", "存在尚未清理的恢复暂存目录")
    except GcRefused:
        raise
    except OSError as exc:
        raise GcRefused("incompatible", f"无法检查数据根父目录暂存状态：{exc}") from exc


def _connect_preview_readonly(db: Path) -> sqlite3.Connection:
    """只读预演连接：静态关闭库用 immutable=1 防创建旁文件，活 WAL 库用 mode=ro。"""
    suffix = "?mode=ro" if _has_sqlite_companions(db) else "?mode=ro&immutable=1"
    conn = sqlite3.connect(
        db.resolve().as_uri() + suffix,
        uri=True,
        timeout=5.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_locked_rw(db: Path) -> sqlite3.Connection:
    """独占锁内读写连接；isolation_level=None 由显式 BEGIN IMMEDIATE 控制单事务。"""
    conn = sqlite3.connect(
        db.resolve().as_uri() + "?mode=rw",
        uri=True,
        timeout=0.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=0")
    return conn


def _verify_db_admission(conn: sqlite3.Connection, db: Path) -> None:
    try:
        snapshot = check_connection(conn, db)
    except AdmissionRefused as exc:
        raise _map_admission_error(exc) from exc
    if snapshot.is_new:
        raise GcRefused("incompatible", "事件库为空或缺少事件 schema")
    try:
        quick = [row[0] for row in conn.execute("PRAGMA main.quick_check").fetchall()]
    except sqlite3.Error as exc:
        raise GcRefused("incompatible", f"SQLite quick_check 失败：{exc}") from exc
    if quick != ["ok"]:
        raise GcRefused("incompatible", "SQLite quick_check 未通过")


def _strict_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise GcRefused("incompatible", f"{label} 必须是整数，实际为 {value!r}")
    return value


def _strict_opt_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _strict_int(value, label)


def _strict_str(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise GcRefused("incompatible", f"{label} 必须是非空字符串")
    return value


def _table_set(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _pending_intent_runs(conn: sqlite3.Connection, root: Path) -> set[str]:
    """返回仍存在未配对 `tool.intent`（无后续同 call_id `tool.result`）的 run_id 集合。"""
    intents: list[tuple[str, str, int]] = []
    last_result: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        "SELECT seq, run_id, type, payload AS payload_inline, payload_ref, payload_hash"
        " FROM events"
        " WHERE type IN ('tool.intent', 'tool.result') ORDER BY seq"
    ).fetchall()
    for row in rows:
        seq = _strict_int(row["seq"], "events.seq")
        run_id = _strict_str(row["run_id"], "events.run_id", allow_empty=True)
        raw = row["payload_inline"]
        call_id: str | None = None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and isinstance(parsed.get("call_id"), str):
                    call_id = parsed["call_id"]
            except ValueError:
                call_id = None
        elif row["payload_ref"] is not None:
            try:
                parsed = read_saved_event_payload(root, row, source_root=root)
                if isinstance(parsed, dict) and isinstance(parsed.get("call_id"), str):
                    call_id = parsed["call_id"]
            except Exception:  # noqa: BLE001
                call_id = None
        if call_id is None:
            if row["type"] == "tool.intent" and run_id:
                intents.append((run_id, f"__seq_{seq}", seq))
            continue
        if row["type"] == "tool.result":
            last_result[(run_id, call_id)] = seq
        else:
            intents.append((run_id, call_id, seq))
    return {
        run_id
        for run_id, call_id, seq in intents
        if last_result.get((run_id, call_id), 0) < seq
    }


def _validate_and_index_runs(
    conn: sqlite3.Connection, tables: set[str]
) -> dict[str, tuple[str, int, int | None, str | None]]:
    """核验 runs 投影封闭集，返回 run_id -> (state, created_at, ended_at, effect_state)。"""
    if "runs" not in tables:
        return {}
    runs: dict[str, tuple[str, int, int | None, str | None]] = {}
    for row in conn.execute(
        "SELECT run_id, state, effect_state, created_at, ended_at FROM runs"
    ).fetchall():
        run_id = _strict_str(row["run_id"], "runs.run_id")
        state = _strict_str(row["state"], "runs.state")
        if state not in RUN_STATES:
            raise GcRefused("incompatible", f"runs.state 含未知枚举值：{state!r}")
        effect = row["effect_state"]
        if effect is not None:
            if type(effect) is not str or effect not in EFFECT_STATES:
                raise GcRefused(
                    "incompatible", f"runs.effect_state 含未知枚举值：{effect!r}"
                )
        created_at = _strict_int(row["created_at"], "runs.created_at")
        ended_at = _strict_opt_int(row["ended_at"], "runs.ended_at")
        runs[run_id] = (state, created_at, ended_at, effect)
    return runs


def _validate_dispatches(conn: sqlite3.Connection, tables: set[str]) -> set[str]:
    """核验 dispatches 封闭集，返回仍含活跃派发（intent/executing）的 run_id 集合。"""
    if "dispatches" not in tables:
        return set()
    active_runs: set[str] = set()
    for row in conn.execute("SELECT run_id, state FROM dispatches").fetchall():
        run_id = _strict_str(row["run_id"], "dispatches.run_id")
        state = _strict_str(row["state"], "dispatches.state")
        if state not in ALL_DISPATCH_STATES:
            raise GcRefused("incompatible", f"dispatches.state 含未知枚举值：{state!r}")
        if state in ACTIVE_DISPATCH_STATES:
            active_runs.add(run_id)
    return active_runs


def _select_expired_jobs(
    conn: sqlite3.Connection,
    tables: set[str],
    cutoff_ms: int,
) -> tuple[str, ...]:
    if "jobs" not in tables or "runs" not in tables:
        return ()
    for row in conn.execute(
        "SELECT job_id, run_id, state, created_at, ended_at FROM jobs"
    ).fetchall():
        _strict_str(row["job_id"], "jobs.job_id")
        _strict_str(row["run_id"], "jobs.run_id")
        state = _strict_str(row["state"], "jobs.state")
        if state not in ALL_JOB_STATES:
            raise GcRefused("incompatible", f"jobs.state 含未知枚举值：{state!r}")
        _strict_int(row["created_at"], "jobs.created_at")
        _strict_opt_int(row["ended_at"], "jobs.ended_at")

    has_dispatches = "dispatches" in tables
    has_subtasks = "subtasks" in tables
    if has_dispatches and has_subtasks:
        rows = conn.execute(
            """
            SELECT j.job_id
            FROM jobs j
            JOIN runs r ON r.run_id = j.run_id
            WHERE j.state IN ('done', 'cancelled')
              AND r.state IN ('succeeded', 'failed', 'cancelled', 'fenced')
              AND COALESCE(j.ended_at, j.created_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM dispatches d
                  WHERE d.run_id = j.run_id
                    AND d.state IN ('intent', 'executing')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM subtasks s
                  WHERE (
                      s.job_id = j.job_id
                      OR s.child_run_id = j.run_id
                      OR s.parent_run_id = j.run_id
                  )
                    AND s.state NOT IN ('succeeded', 'failed', 'cancelled', 'fenced')
              )
            ORDER BY j.created_at, j.job_id
            """,
            (cutoff_ms,),
        ).fetchall()
    elif has_dispatches:
        rows = conn.execute(
            """
            SELECT j.job_id
            FROM jobs j
            JOIN runs r ON r.run_id = j.run_id
            WHERE j.state IN ('done', 'cancelled')
              AND r.state IN ('succeeded', 'failed', 'cancelled', 'fenced')
              AND COALESCE(j.ended_at, j.created_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM dispatches d
                  WHERE d.run_id = j.run_id
                    AND d.state IN ('intent', 'executing')
              )
            ORDER BY j.created_at, j.job_id
            """,
            (cutoff_ms,),
        ).fetchall()
    elif has_subtasks:
        rows = conn.execute(
            """
            SELECT j.job_id
            FROM jobs j
            JOIN runs r ON r.run_id = j.run_id
            WHERE j.state IN ('done', 'cancelled')
              AND r.state IN ('succeeded', 'failed', 'cancelled', 'fenced')
              AND COALESCE(j.ended_at, j.created_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM subtasks s
                  WHERE (
                      s.job_id = j.job_id
                      OR s.child_run_id = j.run_id
                      OR s.parent_run_id = j.run_id
                  )
                    AND s.state NOT IN ('succeeded', 'failed', 'cancelled', 'fenced')
              )
            ORDER BY j.created_at, j.job_id
            """,
            (cutoff_ms,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT j.job_id
            FROM jobs j
            JOIN runs r ON r.run_id = j.run_id
            WHERE j.state IN ('done', 'cancelled')
              AND r.state IN ('succeeded', 'failed', 'cancelled', 'fenced')
              AND COALESCE(j.ended_at, j.created_at) < ?
            ORDER BY j.created_at, j.job_id
            """,
            (cutoff_ms,),
        ).fetchall()
    # 常量交叉绑定断言：确保 SQL 字面量与权威常量保持一致。
    assert TERMINAL_JOB_STATES == {JOB_DONE, JOB_CANCELLED}
    assert TERMINAL_RUN_STATES == {"succeeded", "failed", "cancelled", "fenced"}
    assert set(SUBTASK_TERMINAL_STATES) <= {"succeeded", "failed", "cancelled", "fenced"}
    return tuple(str(r["job_id"]) for r in rows)


def _select_expired_reservations(
    conn: sqlite3.Connection,
    tables: set[str],
    cutoff_ms: int,
) -> tuple[str, ...]:
    if "budget_reservations" not in tables or "runs" not in tables:
        return ()
    for row in conn.execute(
        "SELECT reservation_id, run_id, state, known, created_at, settled_at"
        " FROM budget_reservations"
    ).fetchall():
        _strict_str(row["reservation_id"], "budget_reservations.reservation_id")
        _strict_str(row["run_id"], "budget_reservations.run_id", allow_empty=True)
        state = _strict_str(row["state"], "budget_reservations.state")
        if state not in ALL_RESERVATION_STATES:
            raise GcRefused(
                "incompatible", f"budget_reservations.state 含未知枚举值：{state!r}"
            )
        known = _strict_int(row["known"], "budget_reservations.known")
        if known not in (0, 1):
            raise GcRefused(
                "incompatible", f"budget_reservations.known 非法值：{known!r}"
            )
        _strict_int(row["created_at"], "budget_reservations.created_at")
        _strict_opt_int(row["settled_at"], "budget_reservations.settled_at")

    rows = conn.execute(
        """
        SELECT br.reservation_id
        FROM budget_reservations br
        JOIN runs r ON r.run_id = br.run_id
        WHERE br.state = 'settled'
          AND br.known = 1
          AND br.run_id != ''
          AND r.state IN ('succeeded', 'failed', 'cancelled', 'fenced')
          AND COALESCE(br.settled_at, br.created_at) < ?
        ORDER BY br.created_at, br.reservation_id
        """,
        (cutoff_ms,),
    ).fetchall()
    return tuple(str(r["reservation_id"]) for r in rows)


def _select_empty_snapshots(
    conn: sqlite3.Connection,
    root: Path,
    runs: dict[str, tuple[str, int, int | None, str | None]],
    active_dispatch_runs: set[str],
    pending_intent_runs: set[str],
    index: ReferenceIndex,
    cutoff_ms: int,
) -> tuple[tuple[EmptySnapshotCandidate, ...], int]:
    """扫描超期空终态快照清单（changes == []），统计非空受保护快照数。"""
    snapshots_dir = root / "artifacts" / "snapshots"
    if not snapshots_dir.is_dir():
        return (), 0
    receipt_runs = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT run_id FROM events WHERE type='receipt'"
        ).fetchall()
    }
    candidates: list[EmptySnapshotCandidate] = []
    non_empty_count = 0
    for manifest in sorted(snapshots_dir.glob("*.json")):
        rel = _rel_posix(manifest, root)
        checked = _checked_file(root, rel)
        raw_bytes = checked.read_bytes()
        changes = SnapshotStore._parse(raw_bytes)
        if changes:
            non_empty_count += 1
            continue
        raw_obj = json.loads(raw_bytes.decode("utf-8"))
        pinned = isinstance(raw_obj, dict) and bool(raw_obj.get("pinned"))
        run_id = manifest.stem
        if (snapshots_dir / f"{run_id}.pin").exists():
            pinned = True
        if pinned:
            continue
        run_info = runs.get(run_id)
        if run_info is None:
            continue
        state, created_at, ended_at, effect_state = run_info
        if state not in TERMINAL_RUN_STATES:
            continue
        if effect_state in ("partial", "unknown"):
            continue
        if created_at >= cutoff_ms:
            continue
        if ended_at is not None and ended_at >= cutoff_ms:
            continue
        if run_id in receipt_runs:
            continue
        if run_id in active_dispatch_runs or run_id in pending_intent_runs:
            continue
        if f"artifacts/undo/{run_id}.json" in index.undo_journals:
            continue
        candidates.append(
            EmptySnapshotCandidate(
                path=rel,
                run_id=run_id,
                size=len(raw_bytes),
            )
        )
    return tuple(candidates), non_empty_count


def _check_reference_index_clean(index: ReferenceIndex) -> None:
    in_progress = [
        rel for rel, status in index.undo_journals.items() if status != "completed"
    ]
    if in_progress:
        raise GcRefused(
            "undo_in_progress",
            f"存在未完成撤销日志（{len(in_progress)} 份），拒绝执行 GC",
        )


def _compute_candidates(
    conn: sqlite3.Connection,
    root: Path,
    index: ReferenceIndex,
    cutoff_ms: int,
) -> GcCandidateSet:
    tables = _table_set(conn)
    runs = _validate_and_index_runs(conn, tables)
    active_dispatch_runs = _validate_dispatches(conn, tables)
    pending_intent_runs = _pending_intent_runs(conn, root)

    orphan_candidates: list[OrphanBlobCandidate] = []
    for rel in index.orphan_blobs:
        if rel in index.protected_blobs:
            raise GcRefused("reference_unknown", f"孤儿候选与受保护集合冲突：{rel}")
        checked = _checked_file(root, rel)
        handle = open_entity(checked, "file")
        try:
            if handle.link_count != 1:
                raise GcRefused(
                    "reference_unknown",
                    f"孤儿候选硬链接数不为 1：{rel}（link_count={handle.link_count}）",
                )
            size = checked.stat().st_size
        finally:
            handle.close()
        category = "artifact_blob" if rel.startswith("artifacts/blobs/") else "blob"
        orphan_candidates.append(
            OrphanBlobCandidate(path=rel, size=size, category=category)
        )

    empty_snapshots, non_empty_count = _select_empty_snapshots(
        conn,
        root,
        runs,
        active_dispatch_runs,
        pending_intent_runs,
        index,
        cutoff_ms,
    )
    job_ids = _select_expired_jobs(conn, tables, cutoff_ms)
    reservation_ids = _select_expired_reservations(conn, tables, cutoff_ms)
    return GcCandidateSet(
        orphan_blobs=tuple(orphan_candidates),
        empty_snapshots=empty_snapshots,
        job_ids=job_ids,
        reservation_ids=reservation_ids,
        protected_blobs_count=len(index.protected_blobs),
        non_empty_snapshots_count=non_empty_count,
        undo_journals_count=len(index.undo_journals),
    )


def _unlink_verified_candidates(
    root: Path,
    index: ReferenceIndex,
    orphan_blobs: Sequence[OrphanBlobCandidate],
    empty_snapshots: Sequence[EmptySnapshotCandidate],
) -> tuple[int, int, int, int]:
    """逐个重核实体身份与单硬链接后 unlink，最后同步父目录。"""
    touched_dirs: set[Path] = set()
    deleted_blobs = 0
    deleted_blob_bytes = 0
    for item in orphan_blobs:
        if item.path in index.protected_blobs:
            raise GcRefused("reference_unknown", f"拒绝删除受保护 blob：{item.path}")
        target = _checked_file(root, item.path)
        handle = open_entity(target, "file")
        try:
            if handle.link_count != 1:
                raise GcRefused(
                    "reference_unknown", f"删除前核验发现多硬链接文件：{item.path}"
                )
            actual_size = target.stat().st_size
        finally:
            handle.close()
        target.unlink()
        touched_dirs.add(target.parent)
        deleted_blobs += 1
        deleted_blob_bytes += actual_size

    deleted_snaps = 0
    deleted_snap_bytes = 0
    for snap in empty_snapshots:
        target = _checked_file(root, snap.path)
        handle = open_entity(target, "file")
        try:
            if handle.link_count != 1:
                raise GcRefused(
                    "reference_unknown", f"删除前核验发现多硬链接快照：{snap.path}"
                )
            raw_bytes = target.read_bytes()
        finally:
            handle.close()
        if SnapshotStore._parse(raw_bytes):
            raise GcRefused("reference_unknown", f"删除前核验发现非空快照：{snap.path}")
        target.unlink()
        touched_dirs.add(target.parent)
        deleted_snaps += 1
        deleted_snap_bytes += len(raw_bytes)

    for parent in sorted(touched_dirs):
        _fsync_dir(parent)
    return deleted_blobs, deleted_blob_bytes, deleted_snaps, deleted_snap_bytes


def _delete_db_candidates_in_tx(
    conn: sqlite3.Connection,
    job_ids: Sequence[str],
    reservation_ids: Sequence[str],
) -> tuple[int, int]:
    if not job_ids and not reservation_ids:
        return 0, 0
    deleted_jobs = 0
    deleted_resv = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for job_id in job_ids:
            cur = conn.execute(
                "DELETE FROM jobs"
                " WHERE job_id = ? AND state IN ('done', 'cancelled')",
                (job_id,),
            )
            deleted_jobs += cur.rowcount
        for reservation_id in reservation_ids:
            cur = conn.execute(
                "DELETE FROM budget_reservations"
                " WHERE reservation_id = ?"
                "   AND state = 'settled'"
                "   AND known = 1"
                "   AND run_id != ''",
                (reservation_id,),
            )
            deleted_resv += cur.rowcount
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return deleted_jobs, deleted_resv


def _checkpoint_truncate(conn: sqlite3.Connection) -> dict[str, int] | None:
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None:
        return None
    return {
        "busy": int(row[0]),
        "log": int(row[1]),
        "checkpointed": int(row[2]),
    }


def _empty_deleted() -> dict[str, int]:
    return {
        "orphan_blobs_count": 0,
        "orphan_blobs_bytes": 0,
        "empty_snapshots_count": 0,
        "empty_snapshots_bytes": 0,
        "jobs_count": 0,
        "budget_reservations_count": 0,
        "reclaimed_file_bytes": 0,
    }


def _build_success_report(
    *,
    dry_run: bool,
    older_than_days: int,
    now_ms: int,
    cutoff_ms: int,
    candidates: GcCandidateSet,
    deleted: dict[str, int],
    wal_checkpoint: dict[str, int] | None,
    db_rows_deferred_low_disk: bool,
    notes: Sequence[str],
    extra_paths: Sequence[str],
) -> dict[str, Any]:
    return {
        "operation": "gc",
        "ok": True,
        "dry_run": dry_run,
        "approved": not dry_run,
        "data_root": "<PATH_REDACTED>",
        "older_than_days": older_than_days,
        "now_ms": now_ms,
        "cutoff_ms": cutoff_ms,
        "candidates": {
            "orphan_blobs": [item.to_dict() for item in candidates.orphan_blobs],
            "orphan_blobs_count": len(candidates.orphan_blobs),
            "orphan_blobs_bytes": candidates.orphan_blobs_bytes,
            "empty_snapshots": [item.to_dict() for item in candidates.empty_snapshots],
            "empty_snapshots_count": len(candidates.empty_snapshots),
            "empty_snapshots_bytes": candidates.empty_snapshots_bytes,
            "jobs_count": len(candidates.job_ids),
            "budget_reservations_count": len(candidates.reservation_ids),
            "reclaimable_file_bytes": candidates.reclaimable_file_bytes,
        },
        "protected": {
            "protected_blobs_count": candidates.protected_blobs_count,
            "non_empty_snapshots_count": candidates.non_empty_snapshots_count,
            "undo_journals_count": candidates.undo_journals_count,
        },
        "deleted": dict(deleted),
        "wal_checkpoint": wal_checkpoint,
        "db_rows_deferred_low_disk": db_rows_deferred_low_disk,
        "code": None,
        "error": None,
        "notes": [_redact_text(note, extra_paths) for note in notes],
    }


def build_gc_error_report(
    *,
    code: str,
    message: str,
    dry_run: bool,
    older_than_days: Any,
    extra_paths: Sequence[str] = (),
) -> dict[str, Any]:
    paths = tuple(extra_paths) + (str(Path.home()),)
    days_out = older_than_days if type(older_than_days) is int else None
    return {
        "operation": "gc",
        "ok": False,
        "dry_run": dry_run,
        "approved": not dry_run,
        "data_root": "<PATH_REDACTED>",
        "older_than_days": days_out,
        "candidates": None,
        "protected": None,
        "deleted": _empty_deleted(),
        "wal_checkpoint": None,
        "db_rows_deferred_low_disk": False,
        "code": code,
        "error": _redact_text(message, paths),
        "notes": [],
    }


def run_data_gc(
    data_root: Path | None,
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    dry_run: bool = False,
    approve: bool = False,
    clock: Callable[[], int] = _now_ms,
    disk_free_probe: Callable[[Path], int] = free_bytes,
) -> tuple[dict[str, Any], int]:
    """执行离线安全 GC 预演（返回码 3）或批准清理（返回码 0）；拒绝返回码 2。"""
    extra_paths: list[str] = [str(Path.home())]
    if data_root is not None:
        extra_paths.append(str(data_root))
        try:
            extra_paths.append(str(Path(data_root).resolve()))
        except OSError:
            pass

    effective_dry_run = True if not approve else False
    try:
        if dry_run and approve:
            raise GcRefused(
                "invalid_arguments", "--dry-run 与 --approve 互斥，不能同时指定"
            )
        if data_root is None:
            raise GcRefused(
                "invalid_arguments",
                "data gc 必须显式指定 --data-root（或传 --desktop）",
            )
        if type(older_than_days) is not int or older_than_days < 1:
            raise GcRefused(
                "invalid_arguments",
                f"--older-than-days 必须是 >= 1 的正整数，实际为 {older_than_days!r}",
            )

        now_ms = int(clock())
        cutoff_ms = now_ms - older_than_days * DAY_MS
        root, db = _validate_root_layout(data_root)
        extra_paths.append(str(root))
        _check_not_restore_held(root, db)

        if not approve:
            try:
                with closing(_connect_preview_readonly(db)) as conn:
                    _verify_db_admission(conn, db)
                    try:
                        index = collect_references(root)
                    except ReferenceUnknown as exc:
                        raise GcRefused("reference_unknown", str(exc)) from exc
                    _check_reference_index_clean(index)
                    candidates = _compute_candidates(conn, root, index, cutoff_ms)
            except GcRefused:
                raise
            except (sqlite3.Error, OSError, ValueError, TianshuError) as exc:
                raise GcRefused("incompatible", f"只读预演核验失败：{exc}") from exc

            notes = [
                "只读预演：零文件删除、零 SQLite 改动；"
                "确认无误后请加 --approve 在离线独占锁下执行。",
                "events、summary_*、dispatches、非空快照、protected_blobs、budget_counters"
                " 及未决/未知预占行严格保持 0 删除。",
            ]
            return (
                _build_success_report(
                    dry_run=True,
                    older_than_days=older_than_days,
                    now_ms=now_ms,
                    cutoff_ms=cutoff_ms,
                    candidates=candidates,
                    deleted=_empty_deleted(),
                    wal_checkpoint=None,
                    db_rows_deferred_low_disk=False,
                    notes=notes,
                    extra_paths=extra_paths,
                ),
                3,
            )

        # --approve 离线独占模式：先拿独占维护锁，锁内重核全部不变量与引用索引（防 TOCTOU）。
        try:
            access_cm = acquire_restore_database_access(db, exclusive=True)
        except MaintenanceBusy as exc:
            raise GcRefused("maintenance_busy", f"数据库存在活跃连接或维护操作：{exc}") from exc
        except RestoreScopeBusy as exc:
            raise GcRefused("restore_held", str(exc)) from exc
        except (RestoreAccessError, MaintenanceError, NativeEntityError) as exc:
            raise GcRefused("incompatible", f"无法取得离线独占准入：{exc}") from exc

        with access_cm as access:
            _check_not_restore_held(root, db)
            writers = writers_active(root)
            if writers is not False:
                reason = "存在活跃的 daemon/host 进程" if writers else "无法判定写者存活状态"
                raise GcRefused("maintenance_busy", reason)
            root, db = _validate_root_layout(root)

            with closing(_connect_locked_rw(db)) as conn:
                verify_sqlite_main(conn, db, access.identity)
                _verify_db_admission(conn, db)
                try:
                    index = collect_references(root)
                except ReferenceUnknown as exc:
                    raise GcRefused("reference_unknown", str(exc)) from exc
                _check_reference_index_clean(index)
                candidates = _compute_candidates(conn, root, index, cutoff_ms)

                has_db_deletions = bool(candidates.job_ids or candidates.reservation_ids)
                try:
                    initial_free = int(disk_free_probe(db))
                except Exception:  # noqa: BLE001 - 空间探针异常只保守延后 DB 写，不阻断孤儿文件回收
                    initial_free = 0

                deleted_jobs = 0
                deleted_resv = 0
                wal_checkpoint: dict[str, int] | None = None
                db_rows_deferred_low_disk = False

                if initial_free >= MIN_DB_GC_HEADROOM_BYTES:
                    # 常规及 <2GiB 低盘路径：先单事务提交 DB 行删除（若失败 0 文件已删），
                    # 再 unlink 孤儿文件并 fsync 目录，最后原地截断 WAL。
                    deleted_jobs, deleted_resv = _delete_db_candidates_in_tx(
                        conn, candidates.job_ids, candidates.reservation_ids
                    )
                    (
                        deleted_blobs,
                        deleted_blob_bytes,
                        deleted_snaps,
                        deleted_snap_bytes,
                    ) = _unlink_verified_candidates(
                        root, index, candidates.orphan_blobs, candidates.empty_snapshots
                    )
                    wal_checkpoint = _checkpoint_truncate(conn)
                else:
                    # 极端 0 字节满盘路径：先 unlink 孤儿文件释放空间（unlink 需 0 剩余字节），
                    # 再重测空间决定是否具备最小 WAL 页余量执行 DB 行删除与 checkpoint。
                    (
                        deleted_blobs,
                        deleted_blob_bytes,
                        deleted_snaps,
                        deleted_snap_bytes,
                    ) = _unlink_verified_candidates(
                        root, index, candidates.orphan_blobs, candidates.empty_snapshots
                    )
                    try:
                        post_unlink_free = int(disk_free_probe(db))
                    except Exception:  # noqa: BLE001
                        post_unlink_free = 0
                    if post_unlink_free >= MIN_DB_GC_HEADROOM_BYTES:
                        deleted_jobs, deleted_resv = _delete_db_candidates_in_tx(
                            conn, candidates.job_ids, candidates.reservation_ids
                        )
                        wal_checkpoint = _checkpoint_truncate(conn)
                    else:
                        if has_db_deletions:
                            db_rows_deferred_low_disk = True
                        if not _has_sqlite_companions(db):
                            try:
                                wal_checkpoint = _checkpoint_truncate(conn)
                            except sqlite3.Error:
                                wal_checkpoint = None

            _fsync_dir(root)
            deleted = {
                "orphan_blobs_count": deleted_blobs,
                "orphan_blobs_bytes": deleted_blob_bytes,
                "empty_snapshots_count": deleted_snaps,
                "empty_snapshots_bytes": deleted_snap_bytes,
                "jobs_count": deleted_jobs,
                "budget_reservations_count": deleted_resv,
                "reclaimed_file_bytes": deleted_blob_bytes + deleted_snap_bytes,
            }
            notes = [
                "离线独占安全 GC 已完成；events、summary_*、dispatches、非空快照、"
                "protected_blobs、budget_counters 及未决/未知预占行保持 0 删除。"
            ]
            if db_rows_deferred_low_disk:
                notes.append(
                    "当前磁盘可用空间低于 64 KiB 最小日志安全余量：已完成孤儿文件回收，"
                    "SQLite 表行清理延后至腾出最小日志空间后再次执行。"
                )
            return (
                _build_success_report(
                    dry_run=False,
                    older_than_days=older_than_days,
                    now_ms=now_ms,
                    cutoff_ms=cutoff_ms,
                    candidates=candidates,
                    deleted=deleted,
                    wal_checkpoint=wal_checkpoint,
                    db_rows_deferred_low_disk=db_rows_deferred_low_disk,
                    notes=notes,
                    extra_paths=extra_paths,
                ),
                0,
            )
    except GcRefused as exc:
        return (
            build_gc_error_report(
                code=exc.code,
                message=str(exc),
                dry_run=effective_dry_run,
                older_than_days=older_than_days,
                extra_paths=extra_paths,
            ),
            2,
        )
    except (sqlite3.Error, OSError, ValueError, TianshuError) as exc:
        return (
            build_gc_error_report(
                code="gc_failed",
                message=f"离线安全 GC 失败：{type(exc).__name__}: {exc}",
                dry_run=effective_dry_run,
                older_than_days=older_than_days,
                extra_paths=extra_paths,
            ),
            2,
        )


__all__ = [
    "DEFAULT_OLDER_THAN_DAYS",
    "DAY_MS",
    "GC_FAILURE_CODES",
    "GcCandidateSet",
    "GcRefused",
    "MIN_DB_GC_HEADROOM_BYTES",
    "build_gc_error_report",
    "run_data_gc",
]
