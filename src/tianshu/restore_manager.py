"""受管备份导入、持久隔离、只读复核与中止/续做执行引擎（Restore B）。

严格遵守 docs/restore-held-contract.md 与 schemas/data/restore-v1.json：
- P0 只读预检 → P1 durable reserved → P2 stage_owned + token + 验证复制
  → P3 sealed_hold + .restore-origin.json → P4 publish_intent + 同卷原子发布
  → P5 published_hold（持久隔离态，不激活记忆、不启动服务、不签发业务事件）。
- review 仅使用 `mode=ro` + `PRAGMA query_only=ON` 静态只读连接，零实例化 EventLog /
  TaskStore / AgentLoop，默认全部正文脱敏，仅披露显式 `--body-scope` 选择器。
- abort 仅允许在 `publish_intent` 之前，先提交冻结 `CleanupPlanV1` 的 `abort_intent`，
  再逐项后序 unlink/rmdir 并持久化受影响目录句柄与最终稳定父目录 P，最后提交 `aborted`。
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import backup as backup_mod
from . import data_admission, event_schema, summaries
from .backup import (
    MANIFEST_NAME,
    BackupError,
    BackupManifest,
    read_saved_event_payload,
    sha256_file,
    verify_backup,
)
from .canonical import jcs_sha256
from .crashpoint import failpoint
from .data_state import SCHEMA_VERSION
from .errors import TianshuError
from .execpolicy import ExecPolicy
from .maintenance import (
    DatabaseAccess,
    MaintenanceBusy,
    MaintenanceError,
    acquire_database_access,
    acquire_entity_access,
    acquire_restore_gate,
    compose_access,
)
from .pathkeys import is_ancestor_or_self, overlap_key
from .permissions import APPROVAL_STATES
from .platform_identity import (
    NativeEntityError,
    NativeEntityHandle,
    observe_entity,
    open_entity,
)
from .restore_access import _default_registry
from .restore_contract import (
    AbortedV1,
    AbortIntentV1,
    AbsentTargetV1,
    BindingV1,
    BodySelectorV1,
    BodyViewV1,
    CapacityCheckV1,
    CheckV1,
    CleanupDirectoryV1,
    CleanupFileV1,
    CleanupPlanV1,
    ColumnRefV1,
    ColumnV1,
    ControlLayoutV1,
    CopyFileV1,
    CopyInventoryV1,
    DatabaseBytesV1,
    DependencyRefV1,
    DirectoryBarrierV1,
    DirRefV1,
    EmptyTargetV1,
    EntityIdentityV1,
    EntityRefV1,
    ErrorCategory,
    FamilyCoverageV1,
    LaterChangeV1,
    LegacyApprovalV1,
    LegacyIntentV1,
    LegacyLeaseV1,
    LegacyReconciliation,
    LegacyRunV1,
    ManifestRefV1,
    NamedValueV1,
    NativePathCodecV1,
    NativePathV1,
    OriginalTargetV1,
    OriginV1,
    Phase,
    PhaseV2,
    PlanInputsV1,
    PostReleaseAdmissionStatusV2,
    ProjectionTablesV1,
    PublishedHoldV1,
    PublishIntentV1,
    ReconciledCountsV1,
    ReconcileIntentDispositionV1,
    ReconcilePlanV1,
    RelationDigestV1,
    ReleasedV1,
    ReleaseIntentV1,
    ReservedV1,
    RestoreActionResultV1,
    RestoreCauseV1,
    RestoreContractError,
    RestoreEventV1,
    RestoreEventV2,
    RestoreFailureV1,
    RestorePlanV1,
    RestoreReleaseResultV2,
    RestoreReviewV1,
    RestoreReviewV2,
    ReviewObjectV1,
    ScopeManifestV1,
    SealedHoldV1,
    SourceRefV1,
    SqlRelationRefV1,
    SqlValueV1,
    StablePlacementV1,
    StageOwnedV1,
    TokenV1,
    _path_key,
    domain_digest,
    make_sql_relation,
    make_sql_schema,
    restore_json_v1,
    sql_value_from_sqlite,
)
from .restore_registry import (
    AbsentRegistryObservation,
    PresentRegistryObservation,
    RestoreRegistry,
    RestoreRegistryBusy,
    RestoreRegistryCorrupt,
    RestoreRegistryDurabilityUnavailable,
    RestoreRegistryError,
)
from .tasks import NON_TERMINAL_STATES, RUN_STATES

TOKEN_LAYOUT = ControlLayoutV1((".restore-token.json",), (".restore-token.json.tmp",))
ORIGIN_LAYOUT = ControlLayoutV1((".restore-origin.json",), (".restore-origin.json.tmp",))
_ALLOWED_MATERIAL_ROOTS = frozenset({"artifacts", "blobs", "memory_blobs"})
_EXCLUDED_ROOT_NAMES = frozenset({"run", "logs", ".restore-token.json", ".restore-origin.json"})


class RestoreOperationError(TianshuError):
    """结构化恢复控制失败；保留阶段、batch_id、保留路径与下一合法命令。"""

    def __init__(
        self,
        failure: RestoreFailureV1,
        *,
        partial_review: RestoreReviewV1 | RestoreReviewV2 | None = None,
        raw_cause: BaseException | None = None,
    ) -> None:
        super().__init__(failure.message)
        self.failure = failure
        self.partial_review = partial_review
        self.raw_cause = raw_cause


def _default_disk_free_bytes(existing_dir: Path) -> int:
    return shutil.disk_usage(existing_dir).free


_disk_usage_probe: Callable[[Path], int] = _default_disk_free_bytes


def _cause_from(exc: BaseException | None) -> RestoreCauseV1 | None:
    if exc is None:
        return None
    return RestoreCauseV1(type(exc).__name__, str(exc))


def _next_commands_for_phase(
    root_text: str,
    phase: str | None,
    *,
    has_abort_intent: bool = False,
    review_sha256: str | None = None,
) -> tuple[tuple[str, ...], ...]:
    if phase is None or phase in ("aborted", "released"):
        return ()
    if has_abort_intent or phase == "abort_intent":
        return (
            ("tianshu", "data", "restore-abort", "--data-root", root_text),
            ("tianshu", "data", "restore-resume", "--data-root", root_text),
        )
    if phase in ("reserved", "stage_owned", "sealed_hold"):
        return (
            ("tianshu", "data", "restore-resume", "--data-root", root_text),
            ("tianshu", "data", "restore-abort", "--data-root", root_text),
        )
    if phase == "publish_intent":
        return (("tianshu", "data", "restore-resume", "--data-root", root_text),)
    if phase == "published_hold":
        return (("tianshu", "data", "restore-review", "--data-root", root_text),)
    if phase == "release_intent":
        rev_arg = review_sha256 or "<REVIEW_SHA256>"
        return (
            (
                "tianshu",
                "data",
                "restore-release",
                "--data-root",
                root_text,
                "--expect-review",
                rev_arg,
                "--acknowledge-unproven-later-history",
            ),
        )
    return ()


def _fail(
    category: ErrorCategory,
    message: str,
    *,
    batch_id: str | None = None,
    phase: PhaseV2 | None = None,
    plan_sha256: str | None = None,
    retained_paths: tuple[NativePathV1, ...] = (),
    next_commands: tuple[tuple[str, ...], ...] = (),
    cause: BaseException | None = None,
    partial_review: RestoreReviewV1 | RestoreReviewV2 | None = None,
) -> RestoreOperationError:
    actual_phase = phase if batch_id is not None else None
    failure = RestoreFailureV1(
        category=category,
        message=message,
        batch_id=batch_id,
        phase=actual_phase,
        plan_sha256=plan_sha256,
        retained_paths=retained_paths,
        next_commands=next_commands,
        cause=_cause_from(cause),
    )
    return RestoreOperationError(failure, partial_review=partial_review, raw_cause=cause)


def _registry_or_default(registry: RestoreRegistry | None) -> RestoreRegistry:
    if registry is not None:
        return registry
    from . import restore_access

    return restore_access._registry_factory() or _default_registry()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_fd(fd: int, path: Path, *, batch_ctx: dict[str, Any] | None = None) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        ctx = batch_ctx or {}
        raise _fail(
            "durability_unavailable",
            f"文件持久化 fsync 失败：{path}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc


def _fsync_file_path(path: Path, *, batch_ctx: dict[str, Any] | None = None) -> None:
    flags = (os.O_RDWR if os.name == "nt" else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        ctx = batch_ctx or {}
        raise _fail(
            "durability_unavailable",
            f"无法打开文件执行持久化 fsync：{path}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc
    try:
        _fsync_fd(fd, path, batch_ctx=batch_ctx)
    finally:
        os.close(fd)


def _fsync_dir_handle(
    handle: NativeEntityHandle,
    expected_path: Path | None = None,
    *,
    batch_ctx: dict[str, Any] | None = None,
) -> None:
    ctx = batch_ctx or {}
    try:
        handle.verify_path(expected_path)
    except NativeEntityError as exc:
        raise _fail(
            "record_corrupt",
            f"目录实体在持久化屏障前已失配：{expected_path or handle.path}",
            cause=exc,
            **ctx,
        ) from exc
    if os.name == "nt":  # pragma: no cover - native Windows
        return
    if handle._fd is None:
        raise _fail(
            "durability_unavailable",
            f"目录句柄已关闭，无法执行目录 fsync：{handle.path}",
            **ctx,
        )
    try:
        os.fsync(handle._fd)
    except OSError as exc:
        raise _fail(
            "durability_unavailable",
            f"目录持久化屏障 fsync 失败：{expected_path or handle.path}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc


def _atomic_rename_file(
    src: Path,
    dst: Path,
    *,
    batch_ctx: dict[str, Any] | None = None,
) -> None:
    ctx = batch_ctx or {}
    try:
        if os.name != "nt":
            os.replace(src, dst)
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        flags = 0x00000001 | 0x00000008  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        if not move(str(src), str(dst), flags):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as exc:
        category: ErrorCategory = (
            "capacity_unavailable" if exc.errno == errno.ENOSPC else "durability_unavailable"
        )
        raise _fail(
            category,
            f"控制文件原子重命名失败：{src} -> {dst}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc


def _atomic_publish_directory(
    stage: Path,
    final: Path,
    *,
    batch_ctx: dict[str, Any] | None = None,
) -> None:
    ctx = batch_ctx or {}
    try:
        if os.name != "nt":
            os.replace(stage, final)
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(stage), str(final), 0x00000008):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as exc:
        category: ErrorCategory = (
            "capacity_unavailable" if exc.errno == errno.ENOSPC else "restore_failed"
        )
        raise _fail(
            category,
            f"暂存目录同卷原子发布失败：{stage} -> {final}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc


def _dependency_identities() -> tuple[DependencyRefV1, ...]:
    items = []
    for name, module in (
        ("data_admission", data_admission),
        ("event_schema", event_schema),
        ("restore_contract", __import__(__package__ + ".restore_contract", fromlist=["*"])),
    ):
        mod_path = Path(module.__file__).resolve()
        items.append(DependencyRefV1(name, "1", sha256_file(mod_path)))
    return tuple(sorted(items, key=lambda item: item.name.encode("utf-8")))


def _canonical_target_and_placement(
    raw_target: Path,
    codec: NativePathCodecV1,
) -> tuple[Path, StablePlacementV1]:
    supplied = Path(os.path.abspath(os.fspath(raw_target)))
    try:
        if supplied.is_symlink() or supplied.is_junction():
            raise _fail("incompatible", f"恢复目标不能是符号链接或 junction：{supplied}")
    except OSError as exc:
        raise _fail("incompatible", f"恢复目标路径状态不可判定：{supplied}", cause=exc) from exc

    pending: list[str] = []
    cursor = supplied.parent
    while True:
        try:
            if cursor.is_symlink() or cursor.is_junction():
                raise _fail("incompatible", f"恢复目标待建父路径含链接：{cursor}")
            if cursor.exists():
                if not cursor.is_dir():
                    raise _fail("incompatible", f"恢复目标祖先不是目录：{cursor}")
                break
        except RestoreOperationError:
            raise
        except OSError as exc:
            raise _fail("incompatible", f"恢复目标祖先不可检查：{cursor}", cause=exc) from exc
        if cursor.parent == cursor:
            raise _fail("incompatible", f"恢复目标找不到任何既有祖先目录：{supplied}")
        pending.append(cursor.name)
        cursor = cursor.parent

    deepest = cursor.resolve(strict=True)
    ancestors_rev: list[DirRefV1] = []
    walk = deepest
    while True:
        try:
            ident = observe_entity(walk, "directory")
        except NativeEntityError as exc:
            if ancestors_rev:
                break
            raise _fail(
                "incompatible",
                f"恢复目标既有祖先身份不可取得：{walk}",
                cause=exc,
            ) from exc
        ancestors_rev.append(DirRefV1(codec.from_path(walk), ident))
        if walk.parent == walk:
            break
        walk = walk.parent

    existing_ancestors = tuple(reversed(ancestors_rev))
    pending_suffix = tuple(reversed(pending))
    canonical_root_path = Path(os.path.join(str(deepest), *pending_suffix, supplied.name))
    if not canonical_root_path.name or canonical_root_path.parent == canonical_root_path:
        raise _fail("incompatible", f"恢复目标不能是文件系统根目录：{supplied}")
    placement = StablePlacementV1(
        canonical_root=codec.from_path(canonical_root_path),
        final_name=canonical_root_path.name,
        existing_ancestors=existing_ancestors,
        pending_parent_suffix=pending_suffix,
    )
    return canonical_root_path, placement


def _inspect_original_target(
    canonical_root: Path,
    codec: NativePathCodecV1,
    *,
    batch_ctx: dict[str, Any] | None = None,
) -> tuple[OriginalTargetV1, list[Path]]:
    """检查目标根是 absent 还是任意深度的纯空目录骨架（后序返回空目录列表）。"""
    ctx = batch_ctx or {}
    try:
        if canonical_root.is_symlink() or canonical_root.is_junction():
            raise _fail("incompatible", f"恢复目标不能是符号链接：{canonical_root}", **ctx)
        if not canonical_root.exists():
            return AbsentTargetV1("absent"), []
        if not canonical_root.is_dir():
            raise _fail("incompatible", f"恢复目标不是干净空目录：{canonical_root}", **ctx)
    except RestoreOperationError:
        raise
    except OSError as exc:
        raise _fail(
            "incompatible",
            f"恢复目标状态不可读取：{canonical_root}",
            cause=exc,
            **ctx,
        ) from exc

    try:
        root_handle = open_entity(canonical_root, "directory")
    except NativeEntityError as exc:
        raise _fail(
            "incompatible",
            f"恢复目标目录身份不可取得：{canonical_root}",
            cause=exc,
            **ctx,
        ) from exc
    try:
        root_identity = root_handle.identity
    finally:
        root_handle.close()

    postorder_subdirs: list[Path] = []
    stack: list[tuple[Path, bool]] = [(canonical_root, False)]
    try:
        while stack:
            current, visited = stack.pop()
            if visited:
                if current != canonical_root:
                    postorder_subdirs.append(current)
                continue
            stack.append((current, True))
            children = sorted(current.iterdir(), key=lambda p: p.name)
            for child in reversed(children):
                if child.is_symlink() or child.is_junction() or not child.is_dir():
                    raise _fail(
                        "incompatible",
                        f"恢复目标不是干净根（含非空目录项 {child}）：只接受不存在或全空目录骨架",
                        **ctx,
                    )
                _ = observe_entity(child, "directory")
                stack.append((child, False))
    except RestoreOperationError:
        raise
    except (OSError, NativeEntityError) as exc:
        raise _fail(
            "incompatible",
            f"恢复目标空目录骨架核验失败：{canonical_root}",
            cause=exc,
            **ctx,
        ) from exc

    return (
        EmptyTargetV1("empty", DirRefV1(codec.from_path(canonical_root), root_identity)),
        postorder_subdirs,
    )


def _probe_capacity(
    target_location: Path,
    required_known_bytes: int,
    *,
    metadata_only: bool,
    codec: NativePathCodecV1,
    batch_ctx: dict[str, Any] | None = None,
) -> CapacityCheckV1:
    ctx = batch_ctx or {}
    probe_dir = target_location
    while True:
        try:
            if not probe_dir.is_symlink() and not probe_dir.is_junction() and probe_dir.is_dir():
                break
        except OSError as exc:
            raise _fail(
                "capacity_unavailable",
                f"容量探测祖先状态不可判定：{probe_dir}",
                cause=exc,
                **ctx,
            ) from exc
        if probe_dir.parent == probe_dir:
            raise _fail(
                "capacity_unavailable",
                f"容量探测找不到可用既有目录：{target_location}",
                **ctx,
            )
        probe_dir = probe_dir.parent

    resolved_dir = probe_dir.resolve(strict=True)
    try:
        observed_id = observe_entity(resolved_dir, "directory")
        available = _disk_usage_probe(resolved_dir)
    except (NativeEntityError, OSError) as exc:
        raise _fail(
            "capacity_unavailable",
            f"磁盘可用容量探测失败：{resolved_dir}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc
    if type(available) is not int or isinstance(available, bool) or available < 0:
        raise _fail(
            "capacity_unavailable",
            f"磁盘可用容量探测返回非法数值：{available!r}（{resolved_dir}）",
            **ctx,
        )
    if metadata_only and available <= 0:
        raise _fail(
            "capacity_unavailable",
            f"目录元数据操作所在卷可用空间为 0：{resolved_dir}",
            **ctx,
        )
    if available < required_known_bytes:
        raise _fail(
            "capacity_unavailable",
            f"磁盘可用空间不足（{resolved_dir}）："
            f"可用 {available}B，已知至少需要 {required_known_bytes}B",
            **ctx,
        )
    canonical_loc = Path(os.path.abspath(os.fspath(target_location)))
    return CapacityCheckV1(
        location=codec.from_path(canonical_loc),
        observed_entity=observed_id,
        available_bytes=available,
        required_known_bytes=required_known_bytes,
        metadata_only=metadata_only,
    )


def _check_paths_disjoint(
    backup_root: Path,
    target_root: Path,
    stage_probe: Path,
    authority_dir: Path,
) -> None:
    named = (
        ("backup", overlap_key(str(backup_root))),
        ("target", overlap_key(str(target_root))),
        ("staging", overlap_key(str(stage_probe))),
        ("registry", overlap_key(str(authority_dir))),
    )
    for idx, (left_name, left_key) in enumerate(named):
        for right_name, right_key in named[idx + 1 :]:
            if is_ancestor_or_self(left_key, right_key) or is_ancestor_or_self(right_key, left_key):
                raise _fail(
                    "incompatible",
                    f"{left_name} 与 {right_name} 路径重叠或互为包含，拒绝恢复",
                )


def _build_copy_inventory(backup_root: Path, manifest: BackupManifest) -> CopyInventoryV1:
    dirs: set[tuple[str, ...]] = set()
    files: list[CopyFileV1] = []
    seen_files: set[tuple[str, ...]] = set()
    for entry in manifest.files:
        parts = tuple(entry.path.split("/"))
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise _fail("incompatible", f"备份清单包含非法相对路径：{entry.path!r}")
        if parts == ("events.db",):
            pass
        elif parts[0] in _ALLOWED_MATERIAL_ROOTS and len(parts) >= 2:
            if parts[-1].startswith(".") or parts[-1].endswith(".tmp"):
                raise _fail("incompatible", f"备份材料包含隐藏或临时文件：{entry.path!r}")
        else:
            raise _fail("incompatible", f"备份清单包含不受管路径：{entry.path!r}")
        if parts in seen_files:
            raise _fail("incompatible", f"备份清单包含重复路径：{entry.path!r}")
        seen_files.add(parts)
        files.append(CopyFileV1(parts, entry.size, entry.sha256))
        for depth in range(1, len(parts)):
            dirs.add(parts[:depth])

    for mat_name in sorted(_ALLOWED_MATERIAL_ROOTS):
        mat_dir = backup_root / mat_name
        if not mat_dir.exists():
            continue
        if mat_dir.is_symlink() or mat_dir.is_junction() or not mat_dir.is_dir():
            raise _fail("incompatible", f"备份材料目录状态非法：{mat_dir}")
        stack = [mat_dir]
        while stack:
            cur = stack.pop()
            rel_parts = cur.relative_to(backup_root).parts
            for depth in range(1, len(rel_parts) + 1):
                dirs.add(rel_parts[:depth])
            for child in cur.iterdir():
                if child.is_symlink() or child.is_junction():
                    raise _fail("incompatible", f"备份材料含链接：{child}")
                if child.is_dir():
                    stack.append(child)

    sorted_dirs = tuple(sorted(dirs, key=_path_key))
    sorted_files = tuple(sorted(files, key=lambda item: restore_json_v1(item.path)))
    if not any(f.path == ("events.db",) for f in sorted_files):
        raise _fail("incompatible", "备份清单缺少标准 events.db")
    return CopyInventoryV1(version=1, directories=sorted_dirs, files=sorted_files)


def _open_static_readonly_sqlite(
    db_path: Path,
    *,
    batch_ctx: dict[str, Any] | None = None,
) -> sqlite3.Connection:
    ctx = batch_ctx or {}
    for suffix in ("-journal", "-wal", "-shm"):
        companion = db_path.with_name(db_path.name + suffix)
        try:
            if companion.is_symlink() or companion.is_junction() or companion.exists():
                raise _fail(
                    "record_corrupt",
                    f"拒绝打开带伴随日志/热恢复文件的事件库：{companion}",
                    **ctx,
                )
        except RestoreOperationError:
            raise
        except OSError as exc:
            raise _fail(
                "record_corrupt",
                f"事件库伴随文件状态不可判定：{companion}",
                cause=exc,
                **ctx,
            ) from exc

    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn
    except sqlite3.Error as exc:
        raise _fail(
            "incompatible",
            f"无法以严格只读静态模式打开事件库：{db_path}（{exc}）",
            cause=exc,
            **ctx,
        ) from exc


def _verify_fts_and_summaries_and_policies(
    conn: sqlite3.Connection,
    material_root: Path,
    *,
    source_root: Path,
    source_aliases: Sequence[str],
) -> list[str]:
    problems: list[str] = []
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }

    if "tasks" in tables:
        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("tasks")')}
        if "exec_policy" in cols:
            for task_id, raw_policy in conn.execute("SELECT task_id, exec_policy FROM tasks"):
                if raw_policy:
                    try:
                        parsed = json.loads(raw_policy)
                        ExecPolicy.from_dict(parsed)
                    except (ValueError, TypeError, TianshuError) as exc:
                        problems.append(f"任务 {task_id} 的保存 exec_policy 损坏：{exc}")

    if "memories" in tables:
        if "memory_fts" not in tables:
            problems.append("存在 memories 表但缺少 memory_fts 索引表")
        else:
            from .memory import _tokenize

            fts_rows = {
                int(r[0]): (str(r[1]), str(r[2]))
                for r in conn.execute("SELECT rowid, memory_id, tokens FROM memory_fts")
            }
            active_fts_ids: set[int] = set()
            for row in conn.execute(
                "SELECT memory_id, content_ref, status, fts_rowid, erased_at FROM memories"
            ):
                mem_id = str(row["memory_id"])
                erased_at = row["erased_at"]
                fts_rowid = row["fts_rowid"]
                if erased_at is not None:
                    if fts_rowid is not None and int(fts_rowid) in fts_rows:
                        problems.append(f"已擦除记忆 {mem_id} 仍残留在 memory_fts")
                    continue
                blob_path = material_root / "memory_blobs" / str(row["content_ref"])
                if not blob_path.is_file():
                    problems.append(f"记忆 {mem_id} 缺少正文材料：{blob_path.name}")
                    continue
                try:
                    content_text = blob_path.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    problems.append(f"记忆 {mem_id} 正文不可解码：{exc}")
                    continue
                if fts_rowid is not None:
                    rid = int(fts_rowid)
                    active_fts_ids.add(rid)
                    fts_entry = fts_rows.get(rid)
                    expected_tokens = " ".join(_tokenize(content_text))
                    if fts_entry is None:
                        problems.append(f"记忆 {mem_id} 缺少对应的 memory_fts 行（rowid={rid}）")
                    elif fts_entry != (mem_id, expected_tokens):
                        problems.append(f"记忆 {mem_id} 的 memory_fts 词项与正文不一致")
            extra_fts = set(fts_rows) - active_fts_ids
            if extra_fts:
                problems.append(f"memory_fts 存在孤立索引行：{sorted(extra_fts)}")

    summary_set = {"summary_materials", "summary_sources", "summary_versions"}
    present_summary = summary_set & tables
    if present_summary:
        if present_summary != summary_set:
            missing_sum = sorted(summary_set - present_summary)
            problems.append(
                f"摘要表不完整：存在 {sorted(present_summary)}，缺少 {missing_sum}"
            )
        else:
            event_seqs = {int(r[0]) for r in conn.execute("SELECT seq FROM events")}
            mat_ids: set[str] = set()
            for mrow in conn.execute(
                "SELECT material_id, scope, bytes, text_digest FROM summary_materials"
            ):
                mid = str(mrow["material_id"])
                mat_ids.add(mid)
                srows = list(
                    conn.execute(
                        "SELECT ordinal, kind, ref, digest FROM summary_sources "
                        "WHERE material_id=? ORDER BY ordinal",
                        (mid,),
                    )
                )
                if not srows or [int(s["ordinal"]) for s in srows] != list(range(len(srows))):
                    problems.append(f"摘要材料 {mid} 来源列表为空或序号不连续")
                    continue
                for srow in srows:
                    ref = str(srow["ref"])
                    if not ref.startswith("seq:"):
                        problems.append(f"摘要材料 {mid} 来源引用非法：{ref!r}")
                        continue
                    try:
                        seq_num = int(ref[4:])
                    except ValueError:
                        problems.append(f"摘要材料 {mid} 来源序号非法：{ref!r}")
                        continue
                    if seq_num not in event_seqs:
                        problems.append(f"摘要材料 {mid} 引用了不存在的事件 seq={seq_num}")
                expected_mid = jcs_sha256(
                    {
                        "material_version": summaries.MATERIAL_VERSION,
                        "scope": str(mrow["scope"]),
                        "sources": [
                            {
                                "kind": str(s["kind"]),
                                "ref": str(s["ref"]),
                                "digest": str(s["digest"]),
                            }
                            for s in srows
                        ],
                    }
                )
                if expected_mid != mid:
                    problems.append(f"摘要材料 {mid} 的身份哈希与来源列表不符")
            for vrow in conn.execute(
                "SELECT summary_id, material_id, status, text, text_digest FROM summary_versions"
            ):
                sid = str(vrow["summary_id"])
                vmid = str(vrow["material_id"])
                status = str(vrow["status"])
                if vmid not in mat_ids:
                    problems.append(f"摘要版本 {sid} 引用了不存在的 material_id={vmid}")
                if status not in summaries.STATUSES:
                    problems.append(f"摘要版本 {sid} 状态非法：{status!r}")
                elif status == summaries.READY:
                    text = vrow["text"]
                    if (
                        not isinstance(text, str)
                        or _sha256_bytes(text.encode("utf-8")) != vrow["text_digest"]
                    ):
                        problems.append(f"摘要版本 {sid} 的正文与 text_digest 不一致")

    _ = (source_root, source_aliases)
    return problems


def _build_legacy_reconciliation_and_semantic_check(
    material_root: Path,
    *,
    source_root: Path,
    source_aliases: Sequence[str],
    db_user_version: int,
    batch_ctx: dict[str, Any] | None = None,
) -> LegacyReconciliation:
    ctx = batch_ctx or {}
    db_path = material_root / "events.db"
    conn = _open_static_readonly_sqlite(db_path, batch_ctx=ctx)
    try:
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise _fail("incompatible", f"SQLite integrity_check 失败：{integrity}", **ctx)
            actual_uv = conn.execute("PRAGMA user_version").fetchone()[0]
            if type(actual_uv) is not int or actual_uv != db_user_version:
                raise _fail("incompatible", "事件库 user_version 与清单不符", **ctx)
            if actual_uv not in range(SCHEMA_VERSION + 1):
                raise _fail("incompatible", f"不支持的事件库结构版本：{actual_uv}", **ctx)
            objects = list(
                conn.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
            )
            if objects or actual_uv != 0:
                data_admission._versions(conn)
                data_admission._mechanism(conn, actual_uv)
        except RestoreOperationError:
            raise
        except (TianshuError, sqlite3.Error, ValueError, TypeError) as exc:
            raise _fail(
                "incompatible",
                f"事件库结构或版本旁表校验未通过：{exc}",
                cause=exc,
                **ctx,
            ) from exc

        rows = conn.execute(
            "SELECT e.seq, e.run_id, e.type, e.payload AS payload_inline,"
            " e.payload_ref, e.payload_hash, v.type AS version_type,"
            " COALESCE(v.version, 0) AS version FROM events e"
            " LEFT JOIN event_versions v ON v.seq=e.seq"
            " ORDER BY e.seq"
        )
        intents: list[LegacyIntentV1] = []
        last_result: dict[tuple[str, str], int] = {}
        expected_runs: dict[str, tuple[str, str]] = {}
        try:
            for row in rows:
                seq = row["seq"]
                if type(seq) is not int or isinstance(seq, bool) or seq <= 0:
                    raise ValueError("events.seq 必须是正整数")
                event_type = row["type"]
                if row["version_type"] is not None and row["version_type"] != event_type:
                    raise ValueError(f"seq={seq} 的事件类型与版本旁表不一致")
                payload = read_saved_event_payload(
                    material_root,
                    row,
                    source_root=source_root,
                    source_aliases=source_aliases,
                )
                event_schema.validate_payload(event_type, payload, row["version"])
                if (
                    event_type == "run.start"
                    and row["version"] == 0
                    and "task_id" not in payload
                    and "run_id" not in payload
                ):
                    continue
                if event_type in {"run.start", "run.state", "run.finish", "run.fenced"}:
                    run_id = payload.get("run_id")
                    task_id = payload.get("task_id")
                    if not isinstance(run_id, str) or not run_id or run_id != row["run_id"]:
                        raise ValueError(f"seq={seq} 的 Run 身份与事件不一致")
                    if not isinstance(task_id, str) or not task_id:
                        raise ValueError(f"seq={seq} 缺少有效 task_id")
                    if event_type == "run.start":
                        if run_id in expected_runs:
                            raise ValueError(f"Run {run_id} 存在重复开始事件")
                        expected_runs[run_id] = (task_id, "created")
                    else:
                        prior = expected_runs.get(run_id)
                        if prior is None or prior[0] != task_id:
                            raise ValueError(f"Run {run_id} 的状态事件缺少一致的开始事件")
                        if event_type == "run.state":
                            if payload.get("from") != prior[1]:
                                raise ValueError(f"Run {run_id} 的状态事件不连续")
                            state = payload.get("to")
                        else:
                            state = (
                                "fenced" if event_type == "run.fenced" else payload.get("outcome")
                            )
                        if state not in RUN_STATES:
                            raise ValueError(f"Run 事件状态无法识别：{state!r}")
                        expected_runs[run_id] = (task_id, state)
                if event_type not in {"tool.intent", "tool.result"}:
                    continue
                run_id = row["run_id"]
                call_id = payload.get("call_id")
                if (
                    not isinstance(run_id, str)
                    or not run_id
                    or not isinstance(call_id, str)
                    or not call_id
                ):
                    raise ValueError(f"seq={seq} 工具事件缺少有效 run_id/call_id")
                if event_type == "tool.result":
                    last_result[(run_id, call_id)] = seq
                else:
                    tool_id = payload.get("tool_id")
                    if not isinstance(tool_id, str) or not tool_id:
                        raise ValueError(f"seq={seq} tool.intent 缺少有效 tool_id")
                    intents.append(LegacyIntentV1(run_id, call_id, seq, tool_id))
        except (BackupError, TianshuError, ValueError, TypeError) as exc:
            raise _fail("incompatible", f"无法核验保存的事件链：{exc}", cause=exc, **ctx) from exc

        pending_intents = tuple(
            intent
            for intent in intents
            if last_result.get((intent.run_id, intent.call_id), 0) < intent.seq
        )

        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        missing = sorted({"approvals", "runs", "leases"} - tables)
        if missing:
            raise _fail(
                "incompatible",
                f"恢复核对所需投影缺失：{', '.join(missing)}。"
                "无法判定未决审批、Run 和租约，不把缺失状态当成空状态。",
                **ctx,
            )

        semantic_problems = _verify_fts_and_summaries_and_policies(
            conn,
            material_root,
            source_root=source_root,
            source_aliases=source_aliases,
        )
        if semantic_problems:
            raise _fail("incompatible", "；".join(semantic_problems), **ctx)

        pending_approvals: list[LegacyApprovalV1] = []
        try:
            for row in conn.execute(
                "SELECT approval_id, run_id, action, state, resources, expires_at"
                " FROM approvals ORDER BY created_at, approval_id"
            ):
                if row["state"] not in APPROVAL_STATES:
                    raise ValueError(f"审批状态无法识别：{row['state']!r}")
                if row["state"] not in {"pending", "approved"}:
                    continue
                expires_at = row["expires_at"]
                if type(expires_at) is not int or isinstance(expires_at, bool):
                    raise ValueError("approvals.expires_at 必须是整数")
                pending_approvals.append(
                    LegacyApprovalV1(
                        approval_id=str(row["approval_id"]),
                        run_id=str(row["run_id"]),
                        action=str(row["action"]),
                        state=row["state"],
                        resources=str(row["resources"]),
                        expires_at=expires_at,
                    )
                )

            actual_runs: dict[str, tuple[str, str]] = {}
            non_terminal_runs: list[LegacyRunV1] = []
            for row in conn.execute(
                "SELECT run_id, task_id, state FROM runs ORDER BY created_at, run_id"
            ):
                if row["state"] not in RUN_STATES:
                    raise ValueError(f"Run 状态无法识别：{row['state']!r}")
                run_id = str(row["run_id"])
                task_id = str(row["task_id"])
                actual_runs[run_id] = (task_id, row["state"])
                if row["state"] in NON_TERMINAL_STATES:
                    non_terminal_runs.append(LegacyRunV1(run_id, task_id, row["state"]))
            if actual_runs != expected_runs:
                raise ValueError("runs 投影与事件中的 Run 身份/最终状态不一致")

            now_ms = int(time.time() * 1000)
            live_leases: list[LegacyLeaseV1] = []
            for row in conn.execute(
                "SELECT resource_key, run_id, owner_pid, owner_instance_id, expires_at"
                " FROM leases ORDER BY resource_key"
            ):
                expires = row["expires_at"]
                pid = row["owner_pid"]
                if (
                    type(expires) is not int
                    or isinstance(expires, bool)
                    or type(pid) is not int
                    or isinstance(pid, bool)
                ):
                    raise ValueError("leases 整数字段类型非法")
                if expires > now_ms:
                    live_leases.append(
                        LegacyLeaseV1(
                            resource_key=str(row["resource_key"]),
                            run_id=str(row["run_id"]),
                            owner_pid=pid,
                            owner_instance_id=str(row["owner_instance_id"]),
                            expires_at=expires,
                        )
                    )
        except (ValueError, TypeError, RestoreContractError) as exc:
            raise _fail("incompatible", f"投影状态核验失败：{exc}", cause=exc, **ctx) from exc

        return LegacyReconciliation(
            version=1,
            pending_intents=pending_intents,
            pending_approvals=tuple(pending_approvals),
            non_terminal_runs=tuple(non_terminal_runs),
            live_leases=tuple(live_leases),
            projection_tables=ProjectionTablesV1("present", "present", "present"),
            complete=True,
            reconciliation_scope="legacy_summary_only",
        )
    finally:
        conn.close()


def _family_for_relation(table_name: str) -> str:
    if table_name in ("events", "event_versions"):
        return "events"
    if table_name in ("memories", "memory_meta") or table_name.startswith("memory_"):
        return "memories"
    if table_name.startswith("summary_"):
        return "summaries"
    if table_name in ("approvals", "grants"):
        return "approvals"
    if table_name.startswith("dispatch"):
        return "dispatches"
    if table_name.startswith("budget_"):
        return "budget"
    if table_name.startswith("content_"):
        return "contents"
    if table_name in ("tasks", "runs", "run_cursors", "run_turns", "subtasks"):
        return "runs"
    if table_name in ("jobs", "leases"):
        return "leases_and_jobs"
    return "schema"


def _compute_scope_manifest(
    db_path: Path,
    copy_inventory: CopyInventoryV1,
    *,
    batch_ctx: dict[str, Any] | None = None,
) -> ScopeManifestV1:
    ctx = batch_ctx or {}
    conn = _open_static_readonly_sqlite(db_path, batch_ctx=ctx)
    conn.text_factory = bytes
    try:
        uv_raw = conn.execute("PRAGMA user_version").fetchone()[0]
        user_version = SqlValueV1("integer", str(int(uv_raw)))

        schema_rows_raw = conn.execute(
            "SELECT typeof(type), type, CAST(type AS BLOB), "
            "typeof(name), name, CAST(name AS BLOB), "
            "typeof(tbl_name), tbl_name, CAST(tbl_name AS BLOB), "
            "typeof(rootpage), rootpage, NULL, "
            "typeof(sql), sql, CAST(sql AS BLOB) "
            "FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        sqlite_schema_rows: list[tuple[SqlValueV1, ...]] = []
        table_defs: list[tuple[str, SqlValueV1]] = []
        for row in schema_rows_raw:
            vals: list[SqlValueV1] = []
            for idx in range(5):
                tag = row[idx * 3].decode("ascii")
                val = row[idx * 3 + 1]
                tbytes = row[idx * 3 + 2] if tag == "text" else None
                vals.append(sql_value_from_sqlite(tag, val, text_bytes=tbytes))
            tup = tuple(vals)
            sqlite_schema_rows.append(tup)
            obj_type = row[1].decode("utf-8")
            obj_name = row[4].decode("utf-8")
            if obj_type == "table":
                table_defs.append((obj_name, tup[4]))

        relation_digests: list[RelationDigestV1] = []
        relation_refs: list[SqlRelationRefV1] = []
        family_rels: dict[str, set[str]] = {}
        family_cols: dict[str, set[ColumnRefV1]] = {}

        for table_name, ddl_val in sorted(table_defs, key=lambda item: item[0].encode("utf-8")):
            escaped = table_name.replace('"', '""')
            info_rows = conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
            columns: list[ColumnV1] = []
            select_exprs: list[str] = []
            for c_row in info_rows:
                ordinal = int(c_row[0])
                col_name = c_row[1].decode("utf-8")
                col_type = c_row[2].decode("utf-8")
                not_null = bool(c_row[3])
                dflt_raw = c_row[4]
                dflt_val = (
                    SqlValueV1("null", None)
                    if dflt_raw is None
                    else SqlValueV1("text", base64.b64encode(dflt_raw).decode("ascii"))
                )
                pk_ord = int(c_row[5])
                columns.append(ColumnV1(ordinal, col_name, col_type, not_null, dflt_val, pk_ord))
                col_esc = col_name.replace('"', '""')
                select_exprs.append(f'typeof("{col_esc}"), "{col_esc}", CAST("{col_esc}" AS BLOB)')
            col_tuple = tuple(columns)
            relation_refs.append(SqlRelationRefV1(table_name, col_tuple))

            typed_rows: list[tuple[SqlValueV1, ...]] = []
            if col_tuple:
                query = f'SELECT {", ".join(select_exprs)} FROM "{escaped}"'
                for data_row in conn.execute(query):
                    row_vals: list[SqlValueV1] = []
                    for c_idx in range(len(col_tuple)):
                        tag = data_row[c_idx * 3].decode("ascii")
                        val = data_row[c_idx * 3 + 1]
                        tbytes = data_row[c_idx * 3 + 2] if tag == "text" else None
                        row_vals.append(sql_value_from_sqlite(tag, val, text_bytes=tbytes))
                    typed_rows.append(tuple(row_vals))

            sql_rel = make_sql_relation(table_name, col_tuple, ddl_val, tuple(typed_rows))
            rel_sha = domain_digest("sql-relation", sql_rel)
            relation_digests.append(
                RelationDigestV1(
                    name=table_name,
                    columns=col_tuple,
                    definition=ddl_val,
                    row_count=len(sql_rel.rows),
                    rows_sha256=rel_sha,
                )
            )
            fam = _family_for_relation(table_name)
            family_rels.setdefault(fam, set()).add(table_name)
            for col in col_tuple:
                family_cols.setdefault(fam, set()).add(ColumnRefV1(table_name, col.name))

        sql_schema = make_sql_schema(
            user_version,
            tuple(sqlite_schema_rows),
            tuple(relation_refs),
        )
        schema_sha256 = domain_digest("sql-schema", sql_schema)

        family_materials: dict[str, set[tuple[str, ...]]] = {}
        for f_entry in copy_inventory.files:
            if f_entry.path == ("events.db",):
                family_materials.setdefault("events", set()).add(f_entry.path)
            elif f_entry.path[0] == "memory_blobs":
                family_materials.setdefault("memories", set()).add(f_entry.path)
            elif f_entry.path[0] == "blobs":
                family_materials.setdefault("events", set()).add(f_entry.path)
            elif f_entry.path[0] == "artifacts":
                family_materials.setdefault("artifacts", set()).add(f_entry.path)

        all_families = sorted(set(family_rels) | set(family_materials))
        coverage_items: list[FamilyCoverageV1] = []
        for fam in all_families:
            rels_sorted = tuple(
                sorted(family_rels.get(fam, set()), key=lambda s: s.encode("utf-8"))
            )
            cols_sorted = tuple(
                sorted(
                    family_cols.get(fam, set()),
                    key=lambda item: (item.relation.encode("utf-8"), item.column.encode("utf-8")),
                )
            )
            mats_sorted = tuple(sorted(family_materials.get(fam, set()), key=restore_json_v1))
            coverage_items.append(
                FamilyCoverageV1(
                    family=fam,
                    relations=rels_sorted,
                    columns=cols_sorted,
                    material_paths=mats_sorted,
                    checks=("binding_scope", "reference_closure"),
                )
            )

        return ScopeManifestV1(
            version=1,
            mapping_version=1,
            schema_sha256=schema_sha256,
            relations=tuple(relation_digests),
            coverage=tuple(coverage_items),
            materials=copy_inventory,
        )
    except RestoreOperationError:
        raise
    except (sqlite3.Error, ValueError, RestoreContractError) as exc:
        raise _fail(
            "incompatible",
            f"无法构建确定性 SQLite 作用域清单：{exc}",
            cause=exc,
            **ctx,
        ) from exc
    finally:
        conn.close()


def plan(
    backup_dir: Path,
    data_root: Path,
    *,
    registry: RestoreRegistry | None = None,
) -> RestorePlanV1:
    """只读生成受管恢复计划（P0 预检）；不创建目标目录、暂存目录或注册表。"""
    raw_backup = Path(backup_dir)
    raw_target = Path(data_root)
    try:
        if raw_backup.is_symlink() or raw_backup.is_junction():
            raise _fail("incompatible", f"备份目录不允许符号链接：{raw_backup}")
    except RestoreOperationError:
        raise
    except OSError as exc:
        raise _fail("incompatible", f"备份目录状态不可判定：{raw_backup}", cause=exc) from exc

    verification = verify_backup(raw_backup)
    if not verification.ok or verification.manifest is None:
        msg = "；".join(verification.problems) if verification.problems else "备份完整性校验失败"
        raise _fail("incompatible", f"备份校验未通过：{msg}")
    manifest = verification.manifest
    if manifest.db_user_version > SCHEMA_VERSION:
        raise _fail(
            "incompatible",
            f"备份结构版本 {manifest.db_user_version} 高于本程序支持的 {SCHEMA_VERSION}",
        )

    codec = NativePathCodecV1()
    resolved_backup = raw_backup.resolve(strict=True)
    try:
        backup_id = observe_entity(resolved_backup, "directory")
    except NativeEntityError as exc:
        raise _fail(
            "incompatible", f"备份目录实体身份不可取得：{resolved_backup}", cause=exc
        ) from exc
    backup_ref = DirRefV1(codec.from_path(resolved_backup), backup_id)

    canonical_root, placement = _canonical_target_and_placement(raw_target, codec)
    source_raw = manifest.source_path
    if not isinstance(source_raw, str) or not os.path.isabs(source_raw):
        raise _fail("incompatible", f"备份保存的原数据根不是绝对路径：{source_raw!r}")
    allowed_keys = {overlap_key(source_raw)}
    for alias in manifest.source_aliases:
        if isinstance(alias, str) and os.path.isabs(alias):
            allowed_keys.add(overlap_key(alias))
    if overlap_key(str(canonical_root)) not in allowed_keys:
        raise _fail(
            "incompatible",
            f"恢复目标与备份原数据根不同（目标 {canonical_root}，原根 {source_raw}）；"
            "尚不支持换路径恢复",
        )

    reg = _registry_or_default(registry)
    try:
        reg_obs = reg.observe()
    except RestoreRegistryBusy as exc:
        raise _fail("maintenance_busy", str(exc), cause=exc) from exc
    except RestoreRegistryCorrupt as exc:
        raise _fail("record_corrupt", str(exc), cause=exc) from exc

    stage_probe = canonical_root.parent / f".{canonical_root.name}.restore-probe"
    _check_paths_disjoint(resolved_backup, canonical_root, stage_probe, reg_obs.authority_directory)

    from .restore_access import _active_scope_blocks

    try:
        if _active_scope_blocks(reg_obs, canonical_root / "events.db", None):
            raise _fail(
                "held", f"目标数据根已存在尚未终结或尚未解除隔离的恢复批次：{canonical_root}"
            )
    except RestoreRegistryCorrupt as exc:
        raise _fail("record_corrupt", str(exc), cause=exc) from exc

    original_target, _ = _inspect_original_target(canonical_root, codec)
    copy_inventory = _build_copy_inventory(resolved_backup, manifest)
    inventory_sha256 = domain_digest("inventory", copy_inventory)

    manifest_raw = (resolved_backup / MANIFEST_NAME).read_bytes()
    manifest_ref = ManifestRefV1(
        saved_bytes_b64=base64.b64encode(manifest_raw).decode("ascii"),
        sha256=_sha256_bytes(manifest_raw),
    )

    legacy_rec = _build_legacy_reconciliation_and_semantic_check(
        resolved_backup,
        source_root=Path(source_raw),
        source_aliases=manifest.source_aliases,
        db_user_version=manifest.db_user_version,
    )
    _ = _compute_scope_manifest(resolved_backup / "events.db", copy_inventory)

    deps = _dependency_identities()
    if isinstance(reg_obs, PresentRegistryObservation) and reg_obs.head_sha256 is not None:
        reg_id: str | None = reg_obs.meta.registry_id
        reg_head: str | None = reg_obs.head_sha256
    else:
        reg_id = None
        reg_head = None

    inputs = PlanInputsV1(
        version=1,
        backup_root=backup_ref,
        manifest=manifest_ref,
        copy_inventory=copy_inventory,
        inventory_sha256=inventory_sha256,
        saved_source_path=source_raw,
        source_aliases=tuple(manifest.source_aliases),
        placement=placement,
        original_target=original_target,
        registry_id=reg_id,
        registry_head_sha256=reg_head,
        dependency_identities=deps,
        layout_version=1,
        scope_mapping_version=1,
    )
    plan_sha256 = domain_digest("plan", inputs)

    total_copy_bytes = sum(f.size for f in copy_inventory.files) + 4096
    cap_staging = _probe_capacity(
        canonical_root.parent,
        total_copy_bytes,
        metadata_only=False,
        codec=codec,
    )
    cap_registry = _probe_capacity(
        reg_obs.authority_directory,
        max(len(inputs.to_bytes()) * 2, 4096),
        metadata_only=False,
        codec=codec,
    )
    cap_parent = _probe_capacity(
        canonical_root.parent,
        0,
        metadata_only=True,
        codec=codec,
    )

    notes = [
        "只读预演；不会创建恢复目标、执行恢复、启动服务或激活记忆。",
        "备份无法证明其创建后发生的 forget / erase / delete 历史完整。",
        "记忆恢复须先隔离并人工复核；备份校验成功不代表可安全激活。",
        "未决 intent 不可盲目重放；有效期未过的租约记录不证明原进程仍存活。",
    ]
    if manifest.db_user_version < SCHEMA_VERSION:
        notes.append("恢复后仍需显式离线迁移；受管恢复不会自动升级数据库版本。")

    return RestorePlanV1(
        version=1,
        inputs=inputs,
        plan_sha256=plan_sha256,
        apply_supported=True,
        capacity_checks=(cap_staging, cap_registry, cap_parent),
        verification=verification,
        legacy_reconciliation=legacy_rec,
        notes=tuple(notes),
    )


def _write_control_file_4window(
    stage_path: Path,
    stage_handle: NativeEntityHandle,
    layout: ControlLayoutV1,
    expected_bytes: bytes,
    *,
    prefix_tag: str,
    batch_ctx: dict[str, Any],
) -> None:
    """严格执行 §5.1 四窗口控制见证持久化（支持恢复重入与精确前缀核验）。"""
    formal_path = stage_path / layout.formal[0]
    tmp_path = stage_path / layout.temporary[0]
    stage_handle.verify_path(stage_path)

    has_formal = formal_path.is_symlink() or formal_path.is_junction() or formal_path.exists()
    has_tmp = tmp_path.is_symlink() or tmp_path.is_junction() or tmp_path.exists()

    if has_formal and has_tmp:
        raise _fail(
            "record_corrupt",
            f"正式控制文件与临时文件同时存在，拒绝猜测：{formal_path}",
            **batch_ctx,
        )

    if has_formal:
        try:
            f_handle = open_entity(formal_path, "file")
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt", f"控制文件不是普通文件：{formal_path}", cause=exc, **batch_ctx
            ) from exc
        try:
            if f_handle.link_count != 1:
                raise _fail("record_corrupt", f"控制文件硬链接数非法：{formal_path}", **batch_ctx)
            raw = formal_path.read_bytes()
        finally:
            f_handle.close()
        if raw != expected_bytes:
            raise _fail(
                "record_corrupt", f"正式控制文件字节与已提交事实不符：{formal_path}", **batch_ctx
            )
        _fsync_file_path(formal_path, batch_ctx=batch_ctx)
        _fsync_dir_handle(stage_handle, stage_path, batch_ctx=batch_ctx)
        failpoint(f"FP-{prefix_tag}-DIR-FSYNC", path=stage_path)
        if prefix_tag == "MARKER":
            failpoint("FP-MARKER", path=stage_path)
        return

    if has_tmp:
        try:
            t_handle = open_entity(tmp_path, "file")
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt", f"临时控制文件不是普通文件：{tmp_path}", cause=exc, **batch_ctx
            ) from exc
        try:
            if t_handle.link_count != 1:
                raise _fail("record_corrupt", f"临时控制文件硬链接数非法：{tmp_path}", **batch_ctx)
            raw = tmp_path.read_bytes()
        finally:
            t_handle.close()
        if len(raw) > len(expected_bytes) or raw != expected_bytes[: len(raw)]:
            raise _fail(
                "record_corrupt", f"临时控制文件内容不是预期字节的合法前缀：{tmp_path}", **batch_ctx
            )
        try:
            tmp_path.unlink()
        except OSError as exc:
            raise _fail(
                "restore_failed",
                f"无法重建已核实的临时控制文件：{tmp_path}",
                cause=exc,
                **batch_ctx,
            ) from exc
        _fsync_dir_handle(stage_handle, stage_path, batch_ctx=batch_ctx)

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(tmp_path, flags, 0o600)
    except OSError as exc:
        cat: ErrorCategory = (
            "capacity_unavailable" if exc.errno == errno.ENOSPC else "restore_failed"
        )
        raise _fail(
            cat, f"无法创建临时控制文件：{tmp_path}（{exc}）", cause=exc, **batch_ctx
        ) from exc

    closed = False
    try:
        mid = max(1, len(expected_bytes) // 2)
        written = 0
        first_part = expected_bytes[:mid]
        while written < len(first_part):
            written += os.write(fd, first_part[written:])
        failpoint(f"FP-{prefix_tag}-TMP-PARTIAL", path=tmp_path)
        rest = expected_bytes[mid:]
        r_written = 0
        while r_written < len(rest):
            r_written += os.write(fd, rest[r_written:])
        failpoint(f"FP-{prefix_tag}-TMP-WRITE", path=tmp_path)
        _fsync_fd(fd, tmp_path, batch_ctx=batch_ctx)
        os.close(fd)
        closed = True
    except RestoreOperationError:
        if not closed:
            os.close(fd)
        raise
    except OSError as exc:
        if not closed:
            os.close(fd)
        cat = "capacity_unavailable" if exc.errno == errno.ENOSPC else "restore_failed"
        raise _fail(
            cat, f"写入临时控制文件失败：{tmp_path}（{exc}）", cause=exc, **batch_ctx
        ) from exc

    failpoint(f"FP-{prefix_tag}-TMP-FSYNC", path=tmp_path)
    _atomic_rename_file(tmp_path, formal_path, batch_ctx=batch_ctx)
    failpoint(f"FP-{prefix_tag}-RENAME", path=formal_path)
    _fsync_dir_handle(stage_handle, stage_path, batch_ctx=batch_ctx)
    failpoint(f"FP-{prefix_tag}-DIR-FSYNC", path=stage_path)
    if prefix_tag == "MARKER":
        failpoint("FP-MARKER", path=stage_path)


def _append_registry_cas(
    reg: RestoreRegistry,
    obs: PresentRegistryObservation,
    make_event: Callable[[PresentRegistryObservation], RestoreEventV1 | RestoreEventV2],
    op_access: DatabaseAccess,
    *,
    codec: NativePathCodecV1,
    batch_ctx: dict[str, Any],
) -> tuple[PresentRegistryObservation, RestoreEventV1 | RestoreEventV2]:
    """在持有批次操作独占的前提下提交短事务（若其他根推进了 head 则重观察一次）。"""
    current = obs
    for attempt in range(2):
        event = make_event(current)
        _probe_capacity(
            current.authority_directory,
            len(event.to_bytes()),
            metadata_only=False,
            codec=codec,
            batch_ctx=batch_ctx,
        )
        try:
            updated = reg.append_event(event, expected=current, operation_access=op_access)
            return updated, event
        except RestoreRegistryBusy as exc:
            if attempt == 0:
                refreshed = reg.observe()
                if isinstance(refreshed, PresentRegistryObservation):
                    current = refreshed
                    continue
            raise _fail("maintenance_busy", str(exc), cause=exc, **batch_ctx) from exc
        except RestoreRegistryDurabilityUnavailable as exc:
            raise _fail("durability_unavailable", str(exc), cause=exc, **batch_ctx) from exc
        except RestoreRegistryCorrupt as exc:
            raise _fail("record_corrupt", str(exc), cause=exc, **batch_ctx) from exc
        except RestoreRegistryError as exc:
            raise _fail("restore_failed", str(exc), cause=exc, **batch_ctx) from exc
    raise AssertionError("unreachable")


def _verify_placement_ancestors(
    placement: StablePlacementV1,
    *,
    batch_ctx: dict[str, Any],
) -> None:
    for anc in placement.existing_ancestors:
        anc_path = Path(anc.path.text)
        try:
            actual = observe_entity(anc_path, "directory")
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt",
                f"稳定祖先目录缺失或不可打开：{anc_path}",
                cause=exc,
                **batch_ctx,
            ) from exc
        if actual != anc.identity:
            raise _fail(
                "binding_mismatch",
                f"稳定祖先目录身份已变更：{anc_path}",
                **batch_ctx,
            )


def _copy_stream_into_fd(
    src: Path,
    fd: int,
    dst_display: Path,
    *,
    batch_ctx: dict[str, Any],
) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with src.open("rb") as fin:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    written += os.write(fd, view[written:])
                total += len(chunk)
        _fsync_fd(fd, dst_display, batch_ctx=batch_ctx)
    except RestoreOperationError:
        raise
    except OSError as exc:
        cat: ErrorCategory = (
            "capacity_unavailable" if exc.errno == errno.ENOSPC else "restore_failed"
        )
        raise _fail(
            cat,
            f"复制恢复文件失败：{src} -> {dst_display}（{exc}）",
            cause=exc,
            **batch_ctx,
        ) from exc
    return digest.hexdigest(), total


def _verify_material_tree_matches_binding(
    root_dir: Path,
    binding: BindingV1,
    *,
    allow_token: bool,
    require_origin_bytes: bytes | None,
    batch_ctx: dict[str, Any],
) -> None:
    expected_files = {item.path: item for item in binding.copy_inventory.files}
    expected_dirs = set(binding.copy_inventory.directories)

    actual_files: set[tuple[str, ...]] = set()
    stack = [root_dir]
    while stack:
        cur = stack.pop()
        for child in sorted(cur.iterdir(), key=lambda p: p.name):
            rel = child.relative_to(root_dir).parts
            if child.is_symlink() or child.is_junction():
                raise _fail("binding_mismatch", f"恢复根内出现非法符号链接：{child}", **batch_ctx)
            if rel == (".restore-token.json",):
                if not allow_token:
                    raise _fail("binding_mismatch", f"出现未预期 token 文件：{child}", **batch_ctx)
                continue
            if rel == (".restore-origin.json",):
                if require_origin_bytes is None:
                    raise _fail(
                        "binding_mismatch", f"尚未封存即出现 origin 文件：{child}", **batch_ctx
                    )
                continue
            if child.is_dir():
                if rel not in expected_dirs:
                    raise _fail("binding_mismatch", f"恢复根内出现清单外目录：{child}", **batch_ctx)
                stack.append(child)
            elif child.is_file():
                actual_files.add(rel)
            else:
                raise _fail("binding_mismatch", f"恢复根内出现未知特殊文件：{child}", **batch_ctx)

    if actual_files != set(expected_files):
        missing = sorted(set(expected_files) - actual_files)
        extra = sorted(actual_files - set(expected_files))
        raise _fail(
            "binding_mismatch",
            f"恢复文件集合与 BindingV1 不一致（缺失={missing}，多余={extra}）",
            **batch_ctx,
        )

    for rel, spec in expected_files.items():
        fpath = root_dir / Path(*rel)
        try:
            fhandle = open_entity(fpath, "file")
        except NativeEntityError as exc:
            raise _fail(
                "binding_mismatch", f"恢复文件不可核验：{fpath}", cause=exc, **batch_ctx
            ) from exc
        try:
            if rel == ("events.db",) and fhandle.link_count != 1:
                raise _fail("binding_mismatch", "受管 events.db 硬链接数不为 1", **batch_ctx)
            if rel == ("events.db",) and fhandle.identity != binding.database_identity:
                raise _fail("binding_mismatch", "events.db 实体身份与 BindingV1 不符", **batch_ctx)
            st_size = fpath.stat().st_size
            sha = sha256_file(fpath)
        finally:
            fhandle.close()
        if st_size != spec.size or sha != spec.sha256:
            raise _fail(
                "binding_mismatch", f"恢复文件内容或大小与 BindingV1 不符：{fpath}", **batch_ctx
            )

    if require_origin_bytes is not None:
        origin_path = root_dir / ".restore-origin.json"
        try:
            ohandle = open_entity(origin_path, "file")
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt",
                f"缺少 .restore-origin.json：{origin_path}",
                cause=exc,
                **batch_ctx,
            ) from exc
        try:
            if ohandle.link_count != 1:
                raise _fail(
                    "record_corrupt", ".restore-origin.json 必须是单硬链接普通文件", **batch_ctx
                )
            raw_origin = origin_path.read_bytes()
        finally:
            ohandle.close()
        if raw_origin != require_origin_bytes:
            raise _fail("record_corrupt", ".restore-origin.json 字节与已提交事实不符", **batch_ctx)

    actual_scope = _compute_scope_manifest(
        root_dir / "events.db",
        binding.copy_inventory,
        batch_ctx=batch_ctx,
    )
    if domain_digest("scope", actual_scope) != binding.scope_sha256:
        raise _fail("binding_mismatch", "SQLite 逻辑作用域摘要与已封存 BindingV1 不符", **batch_ctx)


def _advance_to_published_hold(
    reg: RestoreRegistry,
    obs: PresentRegistryObservation,
    batch_id: str,
    op_access: DatabaseAccess,
    *,
    has_db_entity_lock: bool,
) -> RestoreActionResultV1:
    """从 reserved / stage_owned / sealed_hold / publish_intent 确定性推进到 published_hold。"""
    codec = NativePathCodecV1()
    history = {e.kind: e for e in obs.events if e.batch_id == batch_id}
    tail_kind = [e.kind for e in obs.events if e.batch_id == batch_id][-1]
    reserved_ev = history["reserved"]
    reserved: ReservedV1 = reserved_ev.payload
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    parent_path = final_root.parent
    stage_path = parent_path / reserved.stage_name
    plan_sha256 = reserved.plan_sha256

    if tail_kind == "published_hold":
        pub_ev = history["published_hold"]
        sealed_ev = history["sealed_hold"]
        return RestoreActionResultV1(
            batch_id=batch_id,
            phase="published_hold",
            held=True,
            data_root=placement.canonical_root,
            plan_sha256=plan_sha256,
            binding_sha256=sealed_ev.payload.binding_sha256,
            event_sha256=domain_digest("event", pub_ev),
            files_restored=len(reserved.copy_inventory.files),
            bytes_restored=sum(f.size for f in reserved.copy_inventory.files),
            review_command=("tianshu", "data", "restore-review", "--data-root", str(final_root)),
        )

    def _ctx(phase_name: Phase, retained: tuple[Path, ...] = ()) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "phase": phase_name,
            "plan_sha256": plan_sha256,
            "retained_paths": tuple(codec.from_path(p) for p in retained if p.exists()),
            "next_commands": _next_commands_for_phase(str(final_root), phase_name),
        }

    # ---------------- P2a: reserved -> stage_owned ----------------
    if "stage_owned" not in history:
        ctx_r = _ctx("reserved", (stage_path,))
        _verify_placement_ancestors(placement, batch_ctx=ctx_r)
        cur_target, _ = _inspect_original_target(final_root, codec, batch_ctx=ctx_r)
        if cur_target != reserved.original_target:
            raise _fail("binding_mismatch", "恢复目标状态在预约后被外部改动", **ctx_r)

        created_chain: list[DirRefV1] = []
        cursor = Path(placement.existing_ancestors[-1].path.text)
        for seg in placement.pending_parent_suffix:
            parent_h = open_entity(cursor, "directory")
            try:
                cursor = cursor / seg
                if not cursor.exists():
                    _probe_capacity(
                        cursor.parent, 0, metadata_only=True, codec=codec, batch_ctx=ctx_r
                    )
                    cursor.mkdir(mode=0o700)
                    _fsync_dir_handle(parent_h, cursor.parent, batch_ctx=ctx_r)
                seg_id = observe_entity(cursor, "directory")
                created_chain.append(DirRefV1(codec.from_path(cursor), seg_id))
            finally:
                parent_h.close()

        p_handle = open_entity(parent_path, "directory")
        try:
            if stage_path.is_symlink() or stage_path.is_junction():
                raise _fail("record_corrupt", f"暂存路径被替换为链接：{stage_path}", **ctx_r)
            if stage_path.exists():
                s_check = open_entity(stage_path, "directory")
                try:
                    children = list(stage_path.iterdir())
                finally:
                    s_check.close()
                if children:
                    raise _fail(
                        "record_corrupt",
                        f"stage_owned 提交前发现非空暂存目录，拒绝凭名字认领或删除：{stage_path}",
                        **_ctx("reserved", (stage_path,)),
                    )
                stage_path.rmdir()
                _fsync_dir_handle(p_handle, parent_path, batch_ctx=ctx_r)

            _probe_capacity(parent_path, 0, metadata_only=True, codec=codec, batch_ctx=ctx_r)
            stage_path.mkdir(mode=0o700)
            _fsync_dir_handle(p_handle, parent_path, batch_ctx=_ctx("reserved", (stage_path,)))
            failpoint("FP-P2-AFTER-STAGE-MKDIR", path=stage_path)

            s_handle = open_entity(stage_path, "directory")
            try:
                s_ref = DirRefV1(codec.from_path(stage_path), s_handle.identity)
                p_ref = DirRefV1(codec.from_path(parent_path), p_handle.identity)
                reserved_sha = domain_digest("event", reserved_ev)
                token_obj = TokenV1(
                    version=1,
                    registry_id=obs.meta.registry_id,
                    batch_id=batch_id,
                    reserved_sha256=reserved_sha,
                    stage=s_ref,
                    parent=p_ref,
                )
                token_bytes = token_obj.to_bytes()
                token_b64 = base64.b64encode(token_bytes).decode("ascii")
                token_sha = domain_digest("token", token_obj)
                owned_payload = StageOwnedV1(
                    reserved_sha256=reserved_sha,
                    stage=s_ref,
                    parent=p_ref,
                    created_parent_chain=tuple(created_chain),
                    token_layout=TOKEN_LAYOUT,
                    token_bytes_b64=token_b64,
                    token_sha256=token_sha,
                    origin_layout=ORIGIN_LAYOUT,
                )
                failpoint("FP-STAGE-OWNED-BEFORE", path=stage_path)
                obs, owned_ev = _append_registry_cas(
                    reg,
                    obs,
                    lambda cur: RestoreEventV1(
                        1,
                        cur.meta.registry_id,
                        cur.next_sequence,
                        batch_id,
                        cur.head_sha256,
                        "stage_owned",
                        owned_payload,
                    ),
                    op_access,
                    codec=codec,
                    batch_ctx=_ctx("reserved", (stage_path,)),
                )
                history["stage_owned"] = owned_ev
                failpoint("FP-STAGE-OWNED", path=stage_path)
            finally:
                s_handle.close()
        finally:
            p_handle.close()

    # ---------------- P2b + P3: stage_owned -> sealed_hold ----------------
    owned_ev = history["stage_owned"]
    owned: StageOwnedV1 = owned_ev.payload
    token_bytes = base64.b64decode(owned.token_bytes_b64)

    if "sealed_hold" not in history:
        ctx_s = _ctx("stage_owned", (stage_path,))
        _verify_placement_ancestors(placement, batch_ctx=ctx_s)
        p_handle = open_entity(parent_path, "directory")
        s_handle = None
        d_handle = None
        try:
            if p_handle.identity != owned.parent.identity:
                raise _fail("binding_mismatch", "稳定父目录 P 身份与 stage_owned 不符", **ctx_s)
            try:
                s_handle = open_entity(stage_path, "directory")
            except NativeEntityError as exc:
                raise _fail(
                    "record_corrupt", f"已登记暂存目录缺失：{stage_path}", cause=exc, **ctx_s
                ) from exc
            if s_handle.identity != owned.stage.identity:
                raise _fail("record_corrupt", "暂存目录 S 身份与 stage_owned 不符", **ctx_s)

            # 若已有复制产物而正式 token 缺失，禁止通过补写 token 洗白。
            formal_token_path = stage_path / TOKEN_LAYOUT.formal[0]
            other_entries = [
                p.name
                for p in stage_path.iterdir()
                if p.name not in (TOKEN_LAYOUT.formal[0], TOKEN_LAYOUT.temporary[0])
            ]
            if other_entries and not formal_token_path.is_file():
                raise _fail(
                    "record_corrupt",
                    "暂存目录已存在复制内容但缺少正式 .restore-token.json，拒绝补造见证",
                    **ctx_s,
                )

            _probe_capacity(
                stage_path,
                len(token_bytes),
                metadata_only=False,
                codec=codec,
                batch_ctx=ctx_s,
            )
            _write_control_file_4window(
                stage_path,
                s_handle,
                TOKEN_LAYOUT,
                token_bytes,
                prefix_tag="TOKEN",
                batch_ctx=ctx_s,
            )

            backup_root = Path(reserved.plan.backup_root.path.text)
            if not backup_root.is_dir():
                raise _fail("restore_failed", f"封存前原备份目录不可访问：{backup_root}", **ctx_s)
            try:
                if observe_entity(backup_root, "directory") != reserved.plan.backup_root.identity:
                    raise _fail("binding_mismatch", "原备份目录实体身份已改变", **ctx_s)
            except NativeEntityError as exc:
                raise _fail(
                    "restore_failed", f"原备份目录无法打开：{backup_root}", cause=exc, **ctx_s
                ) from exc

            # 检查暂存目录内无未知文件，计算尚需复制字节并校验容量。
            expected_file_map = {f.path: f for f in reserved.copy_inventory.files}
            expected_dir_set = set(reserved.copy_inventory.directories)
            remaining_bytes = 0
            verified_existing: set[tuple[str, ...]] = set()

            stack = [stage_path]
            while stack:
                cur = stack.pop()
                for child in cur.iterdir():
                    rel = child.relative_to(stage_path).parts
                    if rel == TOKEN_LAYOUT.formal:
                        continue
                    if child.is_symlink() or child.is_junction():
                        raise _fail("record_corrupt", f"暂存目录内发现非法链接：{child}", **ctx_s)
                    if child.is_dir():
                        if rel not in expected_dir_set:
                            raise _fail(
                                "record_corrupt", f"暂存目录内发现未登记子目录：{child}", **ctx_s
                            )
                        stack.append(child)
                    elif child.is_file():
                        spec = expected_file_map.get(rel)
                        if spec is None:
                            raise _fail(
                                "record_corrupt", f"暂存目录内发现未登记文件：{child}", **ctx_s
                            )
                        if child.stat().st_size == spec.size and sha256_file(child) == spec.sha256:
                            verified_existing.add(rel)
                    else:
                        raise _fail(
                            "record_corrupt", f"暂存目录内发现非法特殊节点：{child}", **ctx_s
                        )

            for rel, spec in expected_file_map.items():
                if rel not in verified_existing:
                    remaining_bytes += spec.size

            _probe_capacity(
                stage_path,
                remaining_bytes + 4096,
                metadata_only=False,
                codec=codec,
                batch_ctx=ctx_s,
            )

            # 创建派生子目录
            for rel_dir in reserved.copy_inventory.directories:
                sub = stage_path / Path(*rel_dir)
                if not sub.exists():
                    sub.mkdir(mode=0o700)

            # 先锁定 stage/events.db 真实实体 D，再原地流式写入字节（不替换 D 实体）。
            stage_db = stage_path / "events.db"
            db_spec = expected_file_map[("events.db",)]
            if not stage_db.exists():
                db_fd = os.open(
                    stage_db,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                os.close(db_fd)
                _fsync_dir_handle(s_handle, stage_path, batch_ctx=ctx_s)

            d_handle = open_entity(stage_db, "file")
            if d_handle.link_count != 1:
                raise _fail("record_corrupt", "暂存 events.db 必须是单硬链接普通文件", **ctx_s)
            if not has_db_entity_lock:
                try:
                    d_lock = acquire_entity_access(d_handle.identity, exclusive=True)
                except MaintenanceBusy as exc:
                    raise _fail("maintenance_busy", str(exc), cause=exc, **ctx_s) from exc
                op_access._add_child(d_lock)
                has_db_entity_lock = True

            if ("events.db",) not in verified_existing:
                rw_fd = os.open(
                    stage_db,
                    os.O_WRONLY
                    | os.O_TRUNC
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                )
                try:
                    sha, sz = _copy_stream_into_fd(
                        backup_root / "events.db",
                        rw_fd,
                        stage_db,
                        batch_ctx=ctx_s,
                    )
                finally:
                    os.close(rw_fd)
                d_handle.verify_path(stage_db)
                if sz != db_spec.size or sha != db_spec.sha256:
                    raise _fail(
                        "binding_mismatch", "复制后的 events.db 哈希或大小与清单不符", **ctx_s
                    )

            failpoint("FP-STAGE", path=stage_path)

            # 复制并校验其余业务材料文件
            for entry in reserved.copy_inventory.files:
                if entry.path == ("events.db",) or entry.path in verified_existing:
                    continue
                src_file = backup_root / Path(*entry.path)
                dst_file = stage_path / Path(*entry.path)
                if dst_file.exists():
                    dst_file.unlink()
                try:
                    sha, sz = backup_mod._copy_file_verified(src_file, dst_file)
                except OSError as exc:
                    cat = "capacity_unavailable" if exc.errno == errno.ENOSPC else "restore_failed"
                    raise _fail(
                        cat,
                        f"复制恢复材料失败：{dst_file}（{exc}）",
                        cause=exc,
                        **ctx_s,
                    ) from exc
                if sz != entry.size or sha != entry.sha256:
                    raise _fail("binding_mismatch", f"复制材料与清单不符：{dst_file}", **ctx_s)

            # 持久化所有派生子目录与 stage 根
            for rel_dir in reversed(reserved.copy_inventory.directories):
                sub_h = open_entity(stage_path / Path(*rel_dir), "directory")
                try:
                    _fsync_dir_handle(sub_h, stage_path / Path(*rel_dir), batch_ctx=ctx_s)
                finally:
                    sub_h.close()
            _fsync_dir_handle(s_handle, stage_path, batch_ctx=ctx_s)

            # 构造 ScopeManifestV1 与 BindingV1 并提交 SealedHoldV1
            scope_manifest = _compute_scope_manifest(
                stage_db,
                reserved.copy_inventory,
                batch_ctx=ctx_s,
            )
            scope_sha = domain_digest("scope", scope_manifest)
            final_db_native = codec.from_path(final_root / "events.db")
            binding = BindingV1(
                version=1,
                registry_id=obs.meta.registry_id,
                batch_id=batch_id,
                final_root=placement.canonical_root,
                final_database=final_db_native,
                root_identity=s_handle.identity,
                database_identity=d_handle.identity,
                saved_source_path=reserved.plan.saved_source_path,
                source_aliases=reserved.plan.source_aliases,
                manifest=reserved.plan.manifest,
                copy_inventory=reserved.copy_inventory,
                inventory_sha256=reserved.inventory_sha256,
                database_bytes=DatabaseBytesV1(db_spec.size, db_spec.sha256),
                sqlite_scope=scope_manifest,
                scope_sha256=scope_sha,
                dependency_identities=reserved.plan.dependency_identities,
            )
            binding_sha = domain_digest("binding", binding)
            _verify_material_tree_matches_binding(
                stage_path,
                binding,
                allow_token=True,
                require_origin_bytes=None,
                batch_ctx=ctx_s,
            )
            s_handle.verify_path(stage_path)
            d_handle.verify_path(stage_db)
            p_handle.verify_path(parent_path)

            sealed_payload = SealedHoldV1(
                stage_owned_sha256=domain_digest("event", owned_ev),
                future_root_identity=s_handle.identity,
                future_database_identity=d_handle.identity,
                binding=binding,
                binding_sha256=binding_sha,
            )
            obs, sealed_ev = _append_registry_cas(
                reg,
                obs,
                lambda cur: RestoreEventV1(
                    1,
                    cur.meta.registry_id,
                    cur.next_sequence,
                    batch_id,
                    cur.head_sha256,
                    "sealed_hold",
                    sealed_payload,
                ),
                op_access,
                codec=codec,
                batch_ctx=ctx_s,
            )
            history["sealed_hold"] = sealed_ev
            failpoint("FP-SEALED", path=stage_path)
        finally:
            if d_handle is not None:
                d_handle.close()
            if s_handle is not None:
                s_handle.close()
            p_handle.close()

    # ---------------- P3b + P4: sealed_hold -> publish_intent & atomic publish ----------------
    sealed_ev = history["sealed_hold"]
    sealed: SealedHoldV1 = sealed_ev.payload
    sealed_sha = domain_digest("event", sealed_ev)
    origin_obj = OriginV1(
        version=1,
        registry_id=obs.meta.registry_id,
        batch_id=batch_id,
        sealed_sha256=sealed_sha,
        binding_sha256=sealed.binding_sha256,
    )
    origin_bytes = origin_obj.to_bytes()
    origin_b64 = base64.b64encode(origin_bytes).decode("ascii")
    origin_sha = domain_digest("origin", origin_obj)

    if "publish_intent" not in history:
        ctx_sh = _ctx("sealed_hold", (stage_path,))
        _verify_placement_ancestors(placement, batch_ctx=ctx_sh)
        p_handle = open_entity(parent_path, "directory")
        s_handle = None
        d_handle = None
        try:
            if p_handle.identity != owned.parent.identity:
                raise _fail("binding_mismatch", "稳定父目录 P 身份在封存后变更", **ctx_sh)
            try:
                s_handle = open_entity(stage_path, "directory")
                d_handle = open_entity(stage_path / "events.db", "file")
            except NativeEntityError as exc:
                raise _fail(
                    "record_corrupt", f"封存暂存实体缺失：{stage_path}", cause=exc, **ctx_sh
                ) from exc
            if (
                s_handle.identity != sealed.future_root_identity
                or d_handle.identity != sealed.future_database_identity
                or d_handle.link_count != 1
            ):
                raise _fail("binding_mismatch", "暂存 S/D 实体身份与 sealed_hold 不符", **ctx_sh)
            if not has_db_entity_lock:
                try:
                    d_lock = acquire_entity_access(d_handle.identity, exclusive=True)
                except MaintenanceBusy as exc:
                    raise _fail("maintenance_busy", str(exc), cause=exc, **ctx_sh) from exc
                op_access._add_child(d_lock)
                has_db_entity_lock = True

            _probe_capacity(
                stage_path,
                len(origin_bytes),
                metadata_only=False,
                codec=codec,
                batch_ctx=ctx_sh,
            )
            _write_control_file_4window(
                stage_path,
                s_handle,
                ORIGIN_LAYOUT,
                origin_bytes,
                prefix_tag="MARKER",
                batch_ctx=ctx_sh,
            )

            _verify_material_tree_matches_binding(
                stage_path,
                sealed.binding,
                allow_token=True,
                require_origin_bytes=origin_bytes,
                batch_ctx=ctx_sh,
            )
            cur_target, _ = _inspect_original_target(final_root, codec, batch_ctx=ctx_sh)
            if cur_target != reserved.original_target:
                raise _fail(
                    "binding_mismatch", "发布前原目标状态不再匹配预约时的 absent/empty E", **ctx_sh
                )
            _probe_capacity(parent_path, 0, metadata_only=True, codec=codec, batch_ctx=ctx_sh)

            intent_payload = PublishIntentV1(
                sealed_sha256=sealed_sha,
                binding_sha256=sealed.binding_sha256,
                placement=placement,
                original_target=reserved.original_target,
                future_root_identity=sealed.future_root_identity,
                future_database_identity=sealed.future_database_identity,
                origin_bytes_b64=origin_b64,
                origin_sha256=origin_sha,
            )
            failpoint("FP-PUBLISH-INTENT-BEFORE", path=stage_path)
            obs, intent_ev = _append_registry_cas(
                reg,
                obs,
                lambda cur: RestoreEventV1(
                    1,
                    cur.meta.registry_id,
                    cur.next_sequence,
                    batch_id,
                    cur.head_sha256,
                    "publish_intent",
                    intent_payload,
                ),
                op_access,
                codec=codec,
                batch_ctx=ctx_sh,
            )
            history["publish_intent"] = intent_ev
            failpoint("FP-PUBLISH-INTENT", path=stage_path)
        finally:
            if d_handle is not None:
                d_handle.close()
            if s_handle is not None:
                s_handle.close()
            p_handle.close()

    # ---------------- P4b + P5: publish_intent -> published_hold ----------------
    intent_ev = history["publish_intent"]
    intent: PublishIntentV1 = intent_ev.payload
    ctx_pi = _ctx("publish_intent", (stage_path, final_root))
    _verify_placement_ancestors(placement, batch_ctx=ctx_pi)

    p_handle = open_entity(parent_path, "directory")
    try:
        if p_handle.identity != owned.parent.identity:
            raise _fail("binding_mismatch", "发布期间稳定父目录 P 身份不符", **ctx_pi)

        stage_exists = stage_path.is_symlink() or stage_path.is_junction() or stage_path.exists()
        final_exists = final_root.is_symlink() or final_root.is_junction() or final_root.exists()

        if stage_exists:
            s_check = open_entity(stage_path, "directory")
            d_check = open_entity(stage_path / "events.db", "file")
            try:
                if (
                    s_check.identity != intent.future_root_identity
                    or d_check.identity != intent.future_database_identity
                    or d_check.link_count != 1
                ):
                    raise _fail("record_corrupt", "publish_intent 后 stage S/D 实体不符", **ctx_pi)
                if not has_db_entity_lock:
                    try:
                        d_lock = acquire_entity_access(d_check.identity, exclusive=True)
                    except MaintenanceBusy as exc:
                        raise _fail("maintenance_busy", str(exc), cause=exc, **ctx_pi) from exc
                    op_access._add_child(d_lock)
                    has_db_entity_lock = True
            finally:
                d_check.close()
                s_check.close()

            if final_exists:
                if not isinstance(intent.original_target, EmptyTargetV1):
                    raise _fail(
                        "record_corrupt",
                        "publish_intent 原目标为 absent，但发布前出现了未知目标实体",
                        **ctx_pi,
                    )
                cur_target, empty_subdirs_postorder = _inspect_original_target(
                    final_root,
                    codec,
                    batch_ctx=ctx_pi,
                )
                if cur_target != intent.original_target:
                    raise _fail(
                        "record_corrupt",
                        "publish_intent 后空目标目录 E 已被替换或变为非空",
                        **ctx_pi,
                    )
                for sub in empty_subdirs_postorder:
                    sub.rmdir()
                final_root.rmdir()
                _fsync_dir_handle(p_handle, parent_path, batch_ctx=ctx_pi)
                failpoint("FP-AFTER-EMPTY-RMDIR", path=final_root)

            # 注意：Windows 下重命名目录前不能持有目录或其内部文件的原生句柄，
            # 但外置锁根下的 namespace + entity 锁全程持续持有不释放！
            _probe_capacity(parent_path, 0, metadata_only=True, codec=codec, batch_ctx=ctx_pi)
            _atomic_publish_directory(stage_path, final_root, batch_ctx=ctx_pi)
            _fsync_dir_handle(
                p_handle, parent_path, batch_ctx=_ctx("publish_intent", (final_root,))
            )
            failpoint("FP-RENAME", path=final_root)
        else:
            if not final_exists:
                raise _fail(
                    "record_corrupt",
                    "publish_intent 后暂存目录与最终目录同时缺失，无法恢复",
                    **ctx_pi,
                )
            _fsync_dir_handle(
                p_handle, parent_path, batch_ctx=_ctx("publish_intent", (final_root,))
            )

        # P5 最终核对（不依赖外部原备份目录是否仍存在）
        ctx_p5 = _ctx("publish_intent", (final_root,))
        if stage_path.exists() or stage_path.is_symlink() or stage_path.is_junction():
            raise _fail("record_corrupt", "发布后暂存目录名仍未消失", **ctx_p5)

        final_s = open_entity(final_root, "directory")
        final_d = open_entity(final_root / "events.db", "file")
        try:
            if (
                final_s.identity != intent.future_root_identity
                or final_d.identity != intent.future_database_identity
                or final_d.link_count != 1
            ):
                raise _fail("record_corrupt", "最终目录或主库实体身份与预绑定的 S/D 不符", **ctx_p5)
            if not has_db_entity_lock:
                try:
                    d_lock = acquire_entity_access(final_d.identity, exclusive=True)
                except MaintenanceBusy as exc:
                    raise _fail("maintenance_busy", str(exc), cause=exc, **ctx_p5) from exc
                op_access._add_child(d_lock)
                has_db_entity_lock = True

            _verify_material_tree_matches_binding(
                final_root,
                sealed.binding,
                allow_token=True,
                require_origin_bytes=origin_bytes,
                batch_ctx=ctx_p5,
            )

            published_payload = PublishedHoldV1(
                publish_intent_sha256=domain_digest("event", intent_ev),
                binding_sha256=sealed.binding_sha256,
                final_root=DirRefV1(placement.canonical_root, final_s.identity),
                final_database=EntityRefV1(sealed.binding.final_database, final_d.identity),
                stage_absent=True,
                origin_sha256=origin_sha,
            )
            failpoint("FP-PUBLISHED-BEFORE", path=final_root)
            obs, pub_ev = _append_registry_cas(
                reg,
                obs,
                lambda cur: RestoreEventV1(
                    1,
                    cur.meta.registry_id,
                    cur.next_sequence,
                    batch_id,
                    cur.head_sha256,
                    "published_hold",
                    published_payload,
                ),
                op_access,
                codec=codec,
                batch_ctx=ctx_p5,
            )
            failpoint("FP-PUBLISHED", path=final_root)
            return RestoreActionResultV1(
                batch_id=batch_id,
                phase="published_hold",
                held=True,
                data_root=placement.canonical_root,
                plan_sha256=plan_sha256,
                binding_sha256=sealed.binding_sha256,
                event_sha256=domain_digest("event", pub_ev),
                files_restored=len(reserved.copy_inventory.files),
                bytes_restored=sum(f.size for f in reserved.copy_inventory.files),
                review_command=(
                    "tianshu",
                    "data",
                    "restore-review",
                    "--data-root",
                    str(final_root),
                ),
            )
        finally:
            final_d.close()
            final_s.close()
    finally:
        p_handle.close()


def apply(
    backup_dir: Path,
    data_root: Path,
    *,
    expect_plan_sha256: str,
    registry: RestoreRegistry | None = None,
) -> RestoreActionResultV1:
    """执行受管导入（P0–P5），完成后停在持久隔离态 `published_hold`。"""
    reg = _registry_or_default(registry)
    current_plan = plan(backup_dir, data_root, registry=reg)
    if current_plan.plan_sha256 != expect_plan_sha256:
        raise _fail(
            "binding_mismatch",
            f"恢复计划摘要不匹配：当前现场计算为 {current_plan.plan_sha256}，"
            f"传入 --expect-plan 为 {expect_plan_sha256}",
            plan_sha256=current_plan.plan_sha256,
        )

    codec = NativePathCodecV1()
    placement = current_plan.inputs.placement
    final_root = Path(placement.canonical_root.text)
    final_db = final_root / "events.db"

    gate = None
    namespace = None
    op_access = None
    try:
        try:
            gate = acquire_restore_gate(exclusive=True)
            namespace = acquire_database_access(final_db, exclusive=True)
        except MaintenanceBusy as exc:
            raise _fail(
                "maintenance_busy",
                f"无法取得恢复独占门禁或命名空间锁：{exc}",
                plan_sha256=current_plan.plan_sha256,
                cause=exc,
            ) from exc
        except MaintenanceError as exc:
            raise _fail(
                "restore_failed",
                str(exc),
                plan_sha256=current_plan.plan_sha256,
                cause=exc,
            ) from exc

        # 在 gate 内复核目标、祖先、备份与注册表线性化点
        _verify_placement_ancestors(
            placement,
            batch_ctx={"plan_sha256": current_plan.plan_sha256},
        )
        cur_target, _ = _inspect_original_target(
            final_root,
            codec,
            batch_ctx={"plan_sha256": current_plan.plan_sha256},
        )
        if cur_target != current_plan.inputs.original_target:
            raise _fail(
                "binding_mismatch",
                "恢复目标在预约门禁线性化前已变化",
                plan_sha256=current_plan.plan_sha256,
            )

        backup_path = Path(current_plan.inputs.backup_root.path.text)
        try:
            if observe_entity(backup_path, "directory") != current_plan.inputs.backup_root.identity:
                raise _fail(
                    "binding_mismatch",
                    "备份目录实体在预约门禁线性化前已变化",
                    plan_sha256=current_plan.plan_sha256,
                )
        except NativeEntityError as exc:
            raise _fail(
                "restore_failed",
                f"预约前无法重核备份目录：{backup_path}",
                plan_sha256=current_plan.plan_sha256,
                cause=exc,
            ) from exc

        try:
            reg_obs = reg.observe()
            if isinstance(reg_obs, AbsentRegistryObservation):
                if current_plan.inputs.registry_id is not None:
                    raise _fail(
                        "record_corrupt",
                        "计划记录了既有 RestoreRegistry，但预约时权威缺失",
                        plan_sha256=current_plan.plan_sha256,
                    )
                _probe_capacity(
                    reg_obs.authority_directory,
                    4096,
                    metadata_only=False,
                    codec=codec,
                    batch_ctx={"plan_sha256": current_plan.plan_sha256},
                )
                present_obs = reg.initialize_under_gate(gate)
                failpoint("FP-INIT", path=reg.path)
            else:
                present_obs = reg_obs
                expected_reg_id = (
                    present_obs.meta.registry_id if present_obs.head_sha256 is not None else None
                )
                if (
                    current_plan.inputs.registry_id != expected_reg_id
                    or current_plan.inputs.registry_head_sha256 != present_obs.head_sha256
                ):
                    raise _fail(
                        "binding_mismatch",
                        "RestoreRegistry 头已在预演后推进，请重新执行 restore-plan",
                        plan_sha256=current_plan.plan_sha256,
                    )
        except RestoreRegistryBusy as exc:
            raise _fail(
                "maintenance_busy", str(exc), plan_sha256=current_plan.plan_sha256, cause=exc
            ) from exc
        except RestoreRegistryDurabilityUnavailable as exc:
            raise _fail(
                "durability_unavailable", str(exc), plan_sha256=current_plan.plan_sha256, cause=exc
            ) from exc
        except RestoreRegistryCorrupt as exc:
            raise _fail(
                "record_corrupt", str(exc), plan_sha256=current_plan.plan_sha256, cause=exc
            ) from exc

        from .restore_access import _active_scope_blocks

        if _active_scope_blocks(present_obs, final_db, None):
            raise _fail(
                "held",
                f"目标数据根已存在未终结的恢复范围：{final_root}",
                plan_sha256=current_plan.plan_sha256,
            )

        batch_id = str(uuid.uuid4())
        stage_name = f".{placement.final_name}.restore-{batch_id}"
        reserved_payload = ReservedV1(
            plan=current_plan.inputs,
            plan_sha256=current_plan.plan_sha256,
            copy_inventory=current_plan.inputs.copy_inventory,
            inventory_sha256=current_plan.inputs.inventory_sha256,
            placement=placement,
            original_target=current_plan.inputs.original_target,
            stage_name=stage_name,
        )
        reserved_event = RestoreEventV1(
            version=1,
            registry_id=present_obs.meta.registry_id,
            sequence=present_obs.next_sequence,
            batch_id=batch_id,
            previous_sha256=present_obs.head_sha256,
            kind="reserved",
            payload=reserved_payload,
        )
        _probe_capacity(
            present_obs.authority_directory,
            len(reserved_event.to_bytes()),
            metadata_only=False,
            codec=codec,
            batch_ctx={"plan_sha256": current_plan.plan_sha256},
        )
        try:
            present_obs = reg.append_event(
                reserved_event,
                expected=present_obs,
                operation_access=gate,
            )
        except RestoreRegistryBusy as exc:
            raise _fail(
                "maintenance_busy", str(exc), plan_sha256=current_plan.plan_sha256, cause=exc
            ) from exc
        except RestoreRegistryDurabilityUnavailable as exc:
            raise _fail(
                "durability_unavailable", str(exc), plan_sha256=current_plan.plan_sha256, cause=exc
            ) from exc
        except RestoreRegistryCorrupt as exc:
            raise _fail(
                "record_corrupt", str(exc), plan_sha256=current_plan.plan_sha256, cause=exc
            ) from exc

        op_access = compose_access(namespace, operation=True)
        namespace = None
        gate.close()
        gate = None
        failpoint("FP-RESERVED", path=final_root)

        return _advance_to_published_hold(
            reg,
            present_obs,
            batch_id,
            op_access,
            has_db_entity_lock=False,
        )
    finally:
        if op_access is not None:
            op_access.close()
        if namespace is not None:
            namespace.close()
        if gate is not None:
            gate.close()


def _resolve_batch_observation(
    reg: RestoreRegistry,
    data_root: Path,
    batch_id: str | None,
    *,
    allow_aborted: bool = False,
    allow_released: bool = False,
) -> tuple[PresentRegistryObservation, str, list[RestoreEventV1 | RestoreEventV2]]:
    codec = NativePathCodecV1()
    canonical_root, _ = _canonical_target_and_placement(data_root, codec)
    try:
        obs = reg.observe()
    except RestoreRegistryBusy as exc:
        raise _fail("maintenance_busy", str(exc), cause=exc) from exc
    except RestoreRegistryCorrupt as exc:
        raise _fail("record_corrupt", str(exc), cause=exc) from exc

    if isinstance(obs, AbsentRegistryObservation):
        origin_file = canonical_root / ".restore-origin.json"
        if origin_file.exists() or origin_file.is_symlink() or origin_file.is_junction():
            raise _fail(
                "record_corrupt",
                f"存在 .restore-origin.json 但固定 RestoreRegistry 缺失：{canonical_root}",
            )
        raise _fail("incompatible", f"找不到目标数据根的恢复批次记录：{canonical_root}")

    histories: dict[str, list[RestoreEventV1 | RestoreEventV2]] = {}
    for event in obs.events:
        histories.setdefault(event.batch_id, []).append(event)

    target_key = overlap_key(str(canonical_root))
    matching_batches: list[str] = []
    for bid, events in histories.items():
        reserved_ev = events[0]
        if type(reserved_ev.payload) is not ReservedV1:
            raise _fail("record_corrupt", "批次首事件不是 reserved")
        root_key = reserved_ev.payload.placement.canonical_root.comparison_key
        if root_key == target_key:
            matching_batches.append(bid)

    if batch_id is not None:
        if batch_id not in histories:
            raise _fail("incompatible", f"RestoreRegistry 中不存在批次 {batch_id}")
        b_events = histories[batch_id]
        if b_events[0].payload.placement.canonical_root.comparison_key != target_key:
            raise _fail(
                "binding_mismatch",
                f"批次 {batch_id} 绑定的目标根与 --data-root ({canonical_root}) 不符",
                batch_id=batch_id,
                phase=b_events[-1].kind,
            )
        if not allow_aborted and b_events[-1].kind == "aborted":
            raise _fail(
                "incompatible",
                f"批次 {batch_id} 已中止（aborted）",
                batch_id=batch_id,
                phase="aborted",
            )
        if not allow_released and b_events[-1].kind == "released":
            raise _fail(
                "incompatible",
                f"批次 {batch_id} 已解除隔离（released）",
                batch_id=batch_id,
                phase="released",
            )
        return obs, batch_id, b_events

    active = [
        bid
        for bid in matching_batches
        if histories[bid][-1].kind not in ("aborted", "released")
    ]
    if len(active) == 1:
        return obs, active[0], histories[active[0]]
    if len(active) > 1:
        raise _fail("record_corrupt", f"同一数据根存在多个未终结恢复批次：{active}")
    if (
        allow_released
        and matching_batches
        and histories[matching_batches[-1]][-1].kind == "released"
    ):
        last_bid = matching_batches[-1]
        return obs, last_bid, histories[last_bid]
    if allow_aborted and matching_batches and histories[matching_batches[-1]][-1].kind == "aborted":
        last_bid = matching_batches[-1]
        return obs, last_bid, histories[last_bid]
    if matching_batches and histories[matching_batches[-1]][-1].kind == "released":
        last_bid = matching_batches[-1]
        raise _fail(
            "incompatible",
            f"数据根 {canonical_root} 的恢复批次 {last_bid} 已解除隔离（released）",
            batch_id=last_bid,
            phase="released",
        )
    raise _fail("incompatible", f"数据根 {canonical_root} 没有活动的恢复批次")


def _acquire_batch_operation_exclusive(
    reg: RestoreRegistry,
    obs: PresentRegistryObservation,
    batch_id: str,
) -> tuple[PresentRegistryObservation, DatabaseAccess, bool]:
    """按 §4.3 取得批次操作独占（gate -> final namespace -> existing D entity，随后释放 gate）。"""
    b_events = [e for e in obs.events if e.batch_id == batch_id]
    reserved: ReservedV1 = b_events[0].payload
    final_root = Path(reserved.placement.canonical_root.text)
    final_db = final_root / "events.db"
    stage_db = final_root.parent / reserved.stage_name / "events.db"
    plan_sha = reserved.plan_sha256
    phase = b_events[-1].kind

    gate = None
    namespace = None
    entity_locks: list[DatabaseAccess] = []
    has_db_entity_lock = False
    try:
        try:
            gate = acquire_restore_gate(exclusive=True)
            namespace = acquire_database_access(final_db, exclusive=True)
            reg.revalidate(obs)
        except (MaintenanceBusy, RestoreRegistryBusy) as exc:
            raise _fail(
                "maintenance_busy",
                f"无法取得批次操作独占锁：{exc}",
                batch_id=batch_id,
                phase=phase,
                plan_sha256=plan_sha,
                next_commands=_next_commands_for_phase(str(final_root), phase),
                cause=exc,
            ) from exc
        except RestoreRegistryCorrupt as exc:
            raise _fail(
                "record_corrupt",
                str(exc),
                batch_id=batch_id,
                phase=phase,
                plan_sha256=plan_sha,
                cause=exc,
            ) from exc

        seen_ids: set[EntityIdentityV1] = set()
        for cand_db in (stage_db, final_db):
            if cand_db.exists() and not cand_db.is_symlink() and not cand_db.is_junction():
                try:
                    ident = observe_entity(cand_db, "file")
                except NativeEntityError as exc:
                    raise _fail(
                        "record_corrupt",
                        f"批次关联数据库实体不可打开：{cand_db}",
                        batch_id=batch_id,
                        phase=phase,
                        plan_sha256=plan_sha,
                        cause=exc,
                    ) from exc
                if ident not in seen_ids:
                    seen_ids.add(ident)
                    try:
                        entity_locks.append(acquire_entity_access(ident, exclusive=True))
                        has_db_entity_lock = True
                    except MaintenanceBusy as exc:
                        raise _fail(
                            "maintenance_busy",
                            f"批次数据库实体正在被占用：{cand_db}",
                            batch_id=batch_id,
                            phase=phase,
                            plan_sha256=plan_sha,
                            next_commands=_next_commands_for_phase(str(final_root), phase),
                            cause=exc,
                        ) from exc

        op_access = compose_access(namespace, *entity_locks, operation=True)
        namespace = None
        entity_locks = []
        gate.close()
        gate = None
        return obs, op_access, has_db_entity_lock
    except BaseException:
        for elock in reversed(entity_locks):
            elock.close()
        if namespace is not None:
            namespace.close()
        if gate is not None:
            gate.close()
        raise


def _build_cleanup_plan(
    obs: PresentRegistryObservation,
    batch_id: str,
    *,
    codec: NativePathCodecV1,
    batch_ctx: dict[str, Any],
) -> CleanupPlanV1:
    history = {e.kind: e for e in obs.events if e.batch_id == batch_id}
    reserved: ReservedV1 = history["reserved"].payload
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    parent_path = final_root.parent
    stage_path = parent_path / reserved.stage_name

    _verify_placement_ancestors(placement, batch_ctx=batch_ctx)
    cur_target, _ = _inspect_original_target(final_root, codec, batch_ctx=batch_ctx)
    if cur_target != reserved.original_target:
        raise _fail("binding_mismatch", "中止前检测到原目标状态已被改动", **batch_ctx)

    observed_chain = list(placement.existing_ancestors)
    missing_suffix: list[str] = []
    cursor = Path(placement.existing_ancestors[-1].path.text)
    for idx, seg in enumerate(placement.pending_parent_suffix):
        nxt = cursor / seg
        if nxt.is_symlink() or nxt.is_junction():
            raise _fail("record_corrupt", f"待建父路径出现非法链接：{nxt}", **batch_ctx)
        if nxt.exists():
            if not nxt.is_dir():
                raise _fail("record_corrupt", f"待建父路径不是目录：{nxt}", **batch_ctx)
            observed_chain.append(DirRefV1(codec.from_path(nxt), observe_entity(nxt, "directory")))
            cursor = nxt
        else:
            missing_suffix = list(placement.pending_parent_suffix[idx:])
            break

    parent_ref = None if missing_suffix else observed_chain[-1]

    if "stage_owned" not in history:
        if missing_suffix or not stage_path.exists():
            if not missing_suffix and (stage_path.is_symlink() or stage_path.is_junction()):
                raise _fail("record_corrupt", f"暂存路径被替换为链接：{stage_path}", **batch_ctx)
            return CleanupPlanV1(
                version=1,
                stage_observation="absent_before_stage_owned",
                stage=None,
                parent=parent_ref,
                observed_parent_chain=tuple(observed_chain),
                missing_parent_suffix=tuple(missing_suffix),
                files=(),
                directories_postorder=(),
            )
        if stage_path.is_symlink() or stage_path.is_junction():
            raise _fail("record_corrupt", f"暂存路径是链接：{stage_path}", **batch_ctx)
        s_h = open_entity(stage_path, "directory")
        try:
            if list(stage_path.iterdir()):
                raise _fail(
                    "record_corrupt",
                    f"stage_owned 提交前发现非空暂存目录，拒绝纳入清理计划：{stage_path}",
                    **batch_ctx,
                )
            s_ref = DirRefV1(codec.from_path(stage_path), s_h.identity)
        finally:
            s_h.close()
        return CleanupPlanV1(
            version=1,
            stage_observation="present",
            stage=s_ref,
            parent=parent_ref,
            observed_parent_chain=tuple(observed_chain),
            missing_parent_suffix=(),
            files=(),
            directories_postorder=(CleanupDirectoryV1((), s_ref.identity, ()),),
        )

    owned: StageOwnedV1 = history["stage_owned"].payload
    if parent_ref is None or parent_ref != owned.parent:
        raise _fail("record_corrupt", "stage_owned 的父目录 P 已缺失或身份变更", **batch_ctx)
    try:
        s_h = open_entity(stage_path, "directory")
    except NativeEntityError as exc:
        raise _fail(
            "record_corrupt", f"已登记暂存目录缺失：{stage_path}", cause=exc, **batch_ctx
        ) from exc
    try:
        if s_h.identity != owned.stage.identity:
            raise _fail("record_corrupt", "暂存目录 S 实体与 stage_owned 不符", **batch_ctx)
    finally:
        s_h.close()

    inv_files = {f.path: f for f in reserved.copy_inventory.files}
    inv_dirs = set(reserved.copy_inventory.directories)
    expected_token_b64 = owned.token_bytes_b64
    expected_token_bytes = base64.b64decode(expected_token_b64)

    expected_origin_b64: str | None = None
    expected_origin_bytes: bytes | None = None
    if "sealed_hold" in history:
        sealed_ev = history["sealed_hold"]
        origin_obj = OriginV1(
            1,
            obs.meta.registry_id,
            batch_id,
            domain_digest("event", sealed_ev),
            sealed_ev.payload.binding_sha256,
        )
        expected_origin_bytes = origin_obj.to_bytes()
        expected_origin_b64 = base64.b64encode(expected_origin_bytes).decode("ascii")

    cleanup_files: list[CleanupFileV1] = []
    cleanup_dirs: list[CleanupDirectoryV1] = []

    def _walk_dir(cur: Path, rel_dir: tuple[str, ...]) -> None:
        d_h = open_entity(cur, "directory")
        try:
            d_id = d_h.identity
            children_entries = sorted(cur.iterdir(), key=lambda p: restore_json_v1(p.name))
            child_names = tuple(c.name for c in children_entries)
        finally:
            d_h.close()

        for child in children_entries:
            child_rel = (*rel_dir, child.name)
            if child.is_symlink() or child.is_junction():
                raise _fail(
                    "record_corrupt", f"暂存目录内含未知符号链接，拒绝清理：{child}", **batch_ctx
                )
            if child.is_dir():
                if child_rel not in inv_dirs:
                    raise _fail(
                        "record_corrupt", f"暂存目录内含未知子目录，拒绝清理：{child}", **batch_ctx
                    )
                _walk_dir(child, child_rel)
            elif child.is_file():
                f_h = open_entity(child, "file")
                try:
                    if f_h.link_count != 1:
                        raise _fail(
                            "record_corrupt", f"暂存文件硬链接数不为 1：{child}", **batch_ctx
                        )
                    f_id = f_h.identity
                    raw = child.read_bytes()
                finally:
                    f_h.close()
                f_size = len(raw)
                f_sha = _sha256_bytes(raw)

                if child_rel in (TOKEN_LAYOUT.formal, TOKEN_LAYOUT.temporary):
                    if f_size > len(expected_token_bytes) or raw != expected_token_bytes[:f_size]:
                        raise _fail(
                            "record_corrupt",
                            f"token 控制文件内容异常，拒绝清理：{child}",
                            **batch_ctx,
                        )
                    if child_rel == TOKEN_LAYOUT.formal and f_size != len(expected_token_bytes):
                        raise _fail(
                            "record_corrupt", "正式 .restore-token.json 不完整", **batch_ctx
                        )
                    cleanup_files.append(
                        CleanupFileV1(
                            path=child_rel,
                            identity=f_id,
                            size=f_size,
                            sha256=f_sha,
                            copy_entry=None,
                            control_formal=TOKEN_LAYOUT.formal,
                            control_expected_bytes_b64=expected_token_b64,
                        )
                    )
                elif child_rel in (ORIGIN_LAYOUT.formal, ORIGIN_LAYOUT.temporary):
                    if expected_origin_bytes is None or expected_origin_b64 is None:
                        raise _fail(
                            "record_corrupt",
                            f"sealed_hold 前出现未登记 origin：{child}",
                            **batch_ctx,
                        )
                    if f_size > len(expected_origin_bytes) or raw != expected_origin_bytes[:f_size]:
                        raise _fail(
                            "record_corrupt",
                            f"origin 控制文件内容异常，拒绝清理：{child}",
                            **batch_ctx,
                        )
                    if child_rel == ORIGIN_LAYOUT.formal and f_size != len(expected_origin_bytes):
                        raise _fail(
                            "record_corrupt", "正式 .restore-origin.json 不完整", **batch_ctx
                        )
                    cleanup_files.append(
                        CleanupFileV1(
                            path=child_rel,
                            identity=f_id,
                            size=f_size,
                            sha256=f_sha,
                            copy_entry=None,
                            control_formal=ORIGIN_LAYOUT.formal,
                            control_expected_bytes_b64=expected_origin_b64,
                        )
                    )
                elif child_rel in inv_files:
                    spec = inv_files[child_rel]
                    if f_size > spec.size or (f_size == spec.size and f_sha != spec.sha256):
                        raise _fail(
                            "record_corrupt",
                            f"暂存文件超出预期或内容不符，拒绝清理：{child}",
                            **batch_ctx,
                        )
                    cleanup_files.append(
                        CleanupFileV1(
                            path=child_rel,
                            identity=f_id,
                            size=f_size,
                            sha256=f_sha,
                            copy_entry=child_rel,
                            control_formal=None,
                            control_expected_bytes_b64=None,
                        )
                    )
                else:
                    raise _fail(
                        "record_corrupt", f"暂存目录内存在未知文件，拒绝清理：{child}", **batch_ctx
                    )
            else:
                raise _fail(
                    "record_corrupt", f"暂存目录内存在特殊节点，拒绝清理：{child}", **batch_ctx
                )

        cleanup_dirs.append(CleanupDirectoryV1(rel_dir, d_id, child_names))

    _walk_dir(stage_path, ())
    sorted_files = tuple(sorted(cleanup_files, key=lambda item: restore_json_v1(item.path)))
    sorted_dirs = tuple(sorted(cleanup_dirs, key=lambda item: _path_key(item.path, postorder=True)))
    try:
        return CleanupPlanV1(
            version=1,
            stage_observation="present",
            stage=owned.stage,
            parent=owned.parent,
            observed_parent_chain=tuple(observed_chain),
            missing_parent_suffix=(),
            files=sorted_files,
            directories_postorder=sorted_dirs,
        )
    except RestoreContractError as exc:
        raise _fail(
            "record_corrupt", f"无法构造合法清理计划：{exc}", cause=exc, **batch_ctx
        ) from exc


def _execute_frozen_cleanup(
    reg: RestoreRegistry,
    obs: PresentRegistryObservation,
    batch_id: str,
    op_access: DatabaseAccess,
) -> RestoreActionResultV1:
    codec = NativePathCodecV1()
    history = {e.kind: e for e in obs.events if e.batch_id == batch_id}
    reserved: ReservedV1 = history["reserved"].payload
    abort_ev = history["abort_intent"]
    abort_payload: AbortIntentV1 = abort_ev.payload
    cleanup = abort_payload.cleanup_plan
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    stage_path = final_root.parent / reserved.stage_name
    plan_sha = reserved.plan_sha256

    if "aborted" in history:
        ab_ev = history["aborted"]
        return RestoreActionResultV1(
            batch_id=batch_id,
            phase="aborted",
            held=False,
            data_root=placement.canonical_root,
            plan_sha256=plan_sha,
            binding_sha256=None,
            event_sha256=domain_digest("event", ab_ev),
            files_restored=0,
            bytes_restored=0,
            review_command=None,
        )

    ctx_ab = {
        "batch_id": batch_id,
        "phase": "abort_intent",
        "plan_sha256": plan_sha,
        "retained_paths": (codec.from_path(stage_path),) if stage_path.exists() else (),
        "next_commands": _next_commands_for_phase(
            str(final_root), "abort_intent", has_abort_intent=True
        ),
    }

    _verify_placement_ancestors(placement, batch_ctx=ctx_ab)
    cur_target, _ = _inspect_original_target(final_root, codec, batch_ctx=ctx_ab)
    if cur_target != reserved.original_target:
        raise _fail("record_corrupt", "清理期间检测到原目标状态变化", **ctx_ab)

    if cleanup.stage_observation == "absent_before_stage_owned":
        if stage_path.exists() or stage_path.is_symlink() or stage_path.is_junction():
            raise _fail("record_corrupt", "absent 清理计划下出现了未知暂存路径", **ctx_ab)
        anchor_ref = cleanup.observed_parent_chain[-1]
        anchor_path = Path(anchor_ref.path.text)
        anchor_h = open_entity(anchor_path, "directory")
        try:
            if anchor_h.identity != anchor_ref.identity:
                raise _fail("record_corrupt", "清理锚点祖先目录身份变化", **ctx_ab)
            _fsync_dir_handle(anchor_h, anchor_path, batch_ctx=ctx_ab)
        finally:
            anchor_h.close()

        aborted_payload = AbortedV1(
            abort_intent_sha256=domain_digest("event", abort_ev),
            cleanup_plan_sha256=abort_payload.cleanup_plan_sha256,
            accounted_paths=(),
            directory_barriers=(),
            stable_parent=cleanup.parent,
            absence_anchor_barrier=anchor_ref,
            stage_absent=True,
            original_target=reserved.original_target,
        )
        failpoint("FP-ABORTED-BEFORE", path=anchor_path)
        obs, ab_ev = _append_registry_cas(
            reg,
            obs,
            lambda cur: RestoreEventV1(
                1,
                cur.meta.registry_id,
                cur.next_sequence,
                batch_id,
                cur.head_sha256,
                "aborted",
                aborted_payload,
            ),
            op_access,
            codec=codec,
            batch_ctx=ctx_ab,
        )
        failpoint("FP-ABORTED", path=anchor_path)
        return RestoreActionResultV1(
            batch_id=batch_id,
            phase="aborted",
            held=False,
            data_root=placement.canonical_root,
            plan_sha256=plan_sha,
            binding_sha256=None,
            event_sha256=domain_digest("event", ab_ev),
            files_restored=0,
            bytes_restored=0,
            review_command=None,
        )

    assert cleanup.parent is not None and cleanup.stage is not None
    parent_path = Path(cleanup.parent.path.text)
    p_handle = open_entity(parent_path, "directory")
    try:
        if p_handle.identity != cleanup.parent.identity:
            raise _fail("record_corrupt", "稳定父目录 P 身份与 CleanupPlanV1 不符", **ctx_ab)

        planned_files = {f.path: f for f in cleanup.files}
        planned_dirs = {d.path: d for d in cleanup.directories_postorder}

        # 先递归核对现存 stage 子树：只允许原计划的子集（含未达屏障重现项），任何新增/篡改立即拒绝。
        if stage_path.is_symlink() or stage_path.is_junction():
            raise _fail("record_corrupt", f"清理期间暂存路径变为链接：{stage_path}", **ctx_ab)
        if stage_path.exists():
            stack = [(stage_path, ())]
            while stack:
                cur_dir, rel_dir = stack.pop()
                d_spec = planned_dirs.get(rel_dir)
                if d_spec is None:
                    raise _fail("record_corrupt", f"清理现场出现计划外目录：{cur_dir}", **ctx_ab)
                d_h = open_entity(cur_dir, "directory")
                try:
                    if d_h.identity != d_spec.identity:
                        raise _fail(
                            "record_corrupt", f"清理目录身份与原计划不符：{cur_dir}", **ctx_ab
                        )
                    live_children = list(cur_dir.iterdir())
                finally:
                    d_h.close()
                allowed_names = set(d_spec.original_direct_children)
                for child in live_children:
                    if child.name not in allowed_names:
                        raise _fail(
                            "record_corrupt", f"清理目录出现计划外子节点：{child}", **ctx_ab
                        )
                    child_rel = (*rel_dir, child.name)
                    if child.is_symlink() or child.is_junction():
                        raise _fail("record_corrupt", f"清理现场出现链接：{child}", **ctx_ab)
                    if child.is_dir():
                        stack.append((child, child_rel))
                    elif child.is_file():
                        f_spec = planned_files.get(child_rel)
                        if f_spec is None:
                            raise _fail(
                                "record_corrupt", f"清理现场出现计划外文件：{child}", **ctx_ab
                            )
                        f_h = open_entity(child, "file")
                        try:
                            if f_h.identity != f_spec.identity or f_h.link_count != 1:
                                raise _fail(
                                    "record_corrupt", f"清理文件身份与原计划不符：{child}", **ctx_ab
                                )
                            raw = child.read_bytes()
                        finally:
                            f_h.close()
                        if len(raw) != f_spec.size or _sha256_bytes(raw) != f_spec.sha256:
                            raise _fail(
                                "record_corrupt", f"清理文件字节与原计划不符：{child}", **ctx_ab
                            )
                    else:
                        raise _fail("record_corrupt", f"清理现场出现特殊节点：{child}", **ctx_ab)

        accounted: list[tuple[str, ...]] = []
        barriers: list[DirectoryBarrierV1] = []
        first_unlink_done = False

        for f_spec in cleanup.files:
            f_path = stage_path / Path(*f_spec.path)
            parent_rel = f_spec.path[:-1]
            parent_dir_path = stage_path / Path(*parent_rel) if parent_rel else stage_path
            parent_dir_id = planned_dirs[parent_rel].identity
            if f_path.exists():
                try:
                    f_path.unlink()
                except OSError as exc:
                    raise _fail(
                        "restore_failed", f"清理删除文件失败：{f_path}", cause=exc, **ctx_ab
                    ) from exc
                if not first_unlink_done:
                    first_unlink_done = True
                    failpoint("FP-ABORT-AFTER-UNLINK", path=f_path)
            if parent_dir_path.exists():
                failpoint("FP-ABORT-BEFORE-DIR-BARRIER", path=parent_dir_path)
                pd_h = open_entity(parent_dir_path, "directory")
                try:
                    if pd_h.identity != parent_dir_id:
                        raise _fail(
                            "record_corrupt", f"清理父目录身份失配：{parent_dir_path}", **ctx_ab
                        )
                    _fsync_dir_handle(pd_h, parent_dir_path, batch_ctx=ctx_ab)
                finally:
                    pd_h.close()
                failpoint("FP-ABORT-AFTER-DIR-BARRIER", path=parent_dir_path)
            accounted.append(f_spec.path)
            barriers.append(DirectoryBarrierV1(parent_dir_id, f_spec.path))

        for d_spec in cleanup.directories_postorder:
            d_path = stage_path / Path(*d_spec.path) if d_spec.path else stage_path
            if d_path.exists():
                try:
                    d_path.rmdir()
                except OSError as exc:
                    raise _fail(
                        "restore_failed", f"清理移除空目录失败：{d_path}", cause=exc, **ctx_ab
                    ) from exc
                if not d_spec.path:
                    failpoint("FP-ABORT-AFTER-STAGE-RMDIR", path=parent_path)
            if d_spec.path:
                parent_rel = d_spec.path[:-1]
                parent_dir_path = stage_path / Path(*parent_rel) if parent_rel else stage_path
                parent_dir_id = planned_dirs[parent_rel].identity
                if parent_dir_path.exists():
                    failpoint("FP-ABORT-BEFORE-DIR-BARRIER", path=parent_dir_path)
                    pd_h = open_entity(parent_dir_path, "directory")
                    try:
                        if pd_h.identity != parent_dir_id:
                            raise _fail(
                                "record_corrupt", f"清理父目录身份失配：{parent_dir_path}", **ctx_ab
                            )
                        _fsync_dir_handle(pd_h, parent_dir_path, batch_ctx=ctx_ab)
                    finally:
                        pd_h.close()
                    failpoint("FP-ABORT-AFTER-DIR-BARRIER", path=parent_dir_path)
                accounted.append(d_spec.path)
                barriers.append(DirectoryBarrierV1(parent_dir_id, d_spec.path))
            else:
                # 即使 S 此前已缺失，也必须重做稳定父 P 的持久化屏障！
                _fsync_dir_handle(p_handle, parent_path, batch_ctx=ctx_ab)
                failpoint("FP-ABORT-AFTER-PARENT-BARRIER", path=parent_path)
                accounted.append(())
                barriers.append(DirectoryBarrierV1(cleanup.parent.identity, ()))

        if stage_path.exists() or stage_path.is_symlink() or stage_path.is_junction():
            raise _fail("record_corrupt", f"清理完成后暂存路径仍存在：{stage_path}", **ctx_ab)
        cur_target, _ = _inspect_original_target(final_root, codec, batch_ctx=ctx_ab)
        if cur_target != reserved.original_target:
            raise _fail("record_corrupt", "清理完成后原目标状态偏离预约事实", **ctx_ab)

        aborted_payload = AbortedV1(
            abort_intent_sha256=domain_digest("event", abort_ev),
            cleanup_plan_sha256=abort_payload.cleanup_plan_sha256,
            accounted_paths=tuple(accounted),
            directory_barriers=tuple(barriers),
            stable_parent=cleanup.parent,
            absence_anchor_barrier=None,
            stage_absent=True,
            original_target=reserved.original_target,
        )
        failpoint("FP-ABORTED-BEFORE", path=parent_path)
        obs, ab_ev = _append_registry_cas(
            reg,
            obs,
            lambda cur: RestoreEventV1(
                1,
                cur.meta.registry_id,
                cur.next_sequence,
                batch_id,
                cur.head_sha256,
                "aborted",
                aborted_payload,
            ),
            op_access,
            codec=codec,
            batch_ctx={**ctx_ab, "retained_paths": ()},
        )
        failpoint("FP-ABORTED", path=parent_path)
        return RestoreActionResultV1(
            batch_id=batch_id,
            phase="aborted",
            held=False,
            data_root=placement.canonical_root,
            plan_sha256=plan_sha,
            binding_sha256=None,
            event_sha256=domain_digest("event", ab_ev),
            files_restored=0,
            bytes_restored=0,
            review_command=None,
        )
    finally:
        p_handle.close()


def abort(
    data_root: Path,
    *,
    batch_id: str | None = None,
    registry: RestoreRegistry | None = None,
) -> RestoreActionResultV1:
    """中止尚未进入 `publish_intent` 的恢复批次，或幂等续做既有 `abort_intent`。"""
    reg = _registry_or_default(registry)
    obs, resolved_bid, b_events = _resolve_batch_observation(
        reg,
        data_root,
        batch_id,
        allow_aborted=True,
    )
    reserved: ReservedV1 = b_events[0].payload
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    stage_path = final_root.parent / reserved.stage_name
    plan_sha = reserved.plan_sha256

    if b_events[-1].kind == "aborted":
        return RestoreActionResultV1(
            batch_id=resolved_bid,
            phase="aborted",
            held=False,
            data_root=placement.canonical_root,
            plan_sha256=plan_sha,
            binding_sha256=None,
            event_sha256=domain_digest("event", b_events[-1]),
            files_restored=0,
            bytes_restored=0,
            review_command=None,
        )

    obs, op_access, _ = _acquire_batch_operation_exclusive(reg, obs, resolved_bid)
    try:
        b_events = [e for e in obs.events if e.batch_id == resolved_bid]
        history = {e.kind: e for e in b_events}
        tail = b_events[-1]

        if "publish_intent" in history or tail.kind in (
            "publish_intent",
            "published_hold",
            "release_intent",
            "released",
        ):
            raise _fail(
                "held" if tail.kind != "released" else "incompatible",
                f"批次 {resolved_bid} 已进入 {tail.kind}，不可逆禁止 abort",
                batch_id=resolved_bid,
                phase=tail.kind,
                plan_sha256=plan_sha,
                retained_paths=(codec_path,)
                if (codec_path := placement.canonical_root) and final_root.exists()
                else (),
                next_commands=_next_commands_for_phase(str(final_root), tail.kind),
            )

        if "abort_intent" not in history:
            codec = NativePathCodecV1()
            ctx_init = {
                "batch_id": resolved_bid,
                "phase": tail.kind,
                "plan_sha256": plan_sha,
                "retained_paths": (codec.from_path(stage_path),) if stage_path.exists() else (),
                "next_commands": _next_commands_for_phase(str(final_root), tail.kind),
            }
            cleanup_plan = _build_cleanup_plan(obs, resolved_bid, codec=codec, batch_ctx=ctx_init)
            cleanup_sha = domain_digest("cleanup", cleanup_plan)
            abort_payload = AbortIntentV1(
                from_event_sha256=domain_digest("event", tail),
                from_kind=tail.kind,
                never_publish_intent=True,
                cleanup_plan=cleanup_plan,
                cleanup_plan_sha256=cleanup_sha,
            )
            failpoint("FP-ABORT-INTENT-BEFORE", path=final_root.parent)
            obs, _ = _append_registry_cas(
                reg,
                obs,
                lambda cur: RestoreEventV1(
                    1,
                    cur.meta.registry_id,
                    cur.next_sequence,
                    resolved_bid,
                    cur.head_sha256,
                    "abort_intent",
                    abort_payload,
                ),
                op_access,
                codec=codec,
                batch_ctx=ctx_init,
            )
            failpoint("FP-ABORT-INTENT", path=final_root.parent)

        return _execute_frozen_cleanup(reg, obs, resolved_bid, op_access)
    finally:
        op_access.close()


def resume(
    data_root: Path,
    *,
    batch_id: str | None = None,
    registry: RestoreRegistry | None = None,
) -> RestoreActionResultV1:
    """续做同一恢复批次：若有 `abort_intent` 则只续清理，否则确定性推进至 `published_hold`。"""
    reg = _registry_or_default(registry)
    obs, resolved_bid, b_events = _resolve_batch_observation(
        reg,
        data_root,
        batch_id,
        allow_aborted=False,
    )
    obs, op_access, has_db_entity_lock = _acquire_batch_operation_exclusive(reg, obs, resolved_bid)
    try:
        b_events_cur = [e for e in obs.events if e.batch_id == resolved_bid]
        history = {e.kind: e for e in b_events_cur}
        tail = b_events_cur[-1]
        if tail.kind in ("release_intent", "released"):
            reserved_pl: ReservedV1 = b_events_cur[0].payload
            f_root = str(Path(reserved_pl.placement.canonical_root.text))
            rev_sha = (
                history["release_intent"].payload.review_sha256
                if "release_intent" in history
                else None
            )
            raise _fail(
                "held" if tail.kind == "release_intent" else "incompatible",
                f"批次 {resolved_bid} 已进入 {tail.kind}，请使用 restore-release 收口",
                batch_id=resolved_bid,
                phase=tail.kind,
                plan_sha256=reserved_pl.plan_sha256,
                next_commands=_next_commands_for_phase(
                    f_root, tail.kind, review_sha256=rev_sha
                ),
            )
        if "abort_intent" in history:
            return _execute_frozen_cleanup(reg, obs, resolved_bid, op_access)
        return _advance_to_published_hold(
            reg,
            obs,
            resolved_bid,
            op_access,
            has_db_entity_lock=has_db_entity_lock,
        )
    finally:
        op_access.close()


def _make_body_view(
    family: str,
    key: tuple[SqlValueV1, ...],
    field_name: str,
    raw_bytes: bytes,
    disclosed_scopes: set[str],
) -> BodyViewV1:
    selector = BodySelectorV1(family, key, field_name).to_selector()
    size = len(raw_bytes)
    sha = _sha256_bytes(raw_bytes)
    if selector not in disclosed_scopes:
        return BodyViewV1(field_name, size, sha, selector, None, None)
    try:
        decoded = raw_bytes.decode("utf-8")
        if decoded.encode("utf-8") != raw_bytes:
            raise UnicodeError("surrogate")
    except UnicodeError:
        return BodyViewV1(
            field_name,
            size,
            sha,
            selector,
            None,
            base64.b64encode(raw_bytes).decode("ascii"),
        )
    return BodyViewV1(field_name, size, sha, selector, decoded, None)


def _sql_text(val: str) -> SqlValueV1:
    return SqlValueV1("text", base64.b64encode(val.encode("utf-8")).decode("ascii"))


def _sql_int(val: int) -> SqlValueV1:
    return SqlValueV1("integer", str(int(val)))


def _sql_opt_text(val: str | None) -> SqlValueV1:
    return SqlValueV1("null", None) if val is None else _sql_text(val)


def _sql_opt_int(val: int | None) -> SqlValueV1:
    return SqlValueV1("null", None) if val is None else _sql_int(val)


def _scan_review_objects(
    material_root: Path,
    *,
    source_root: Path,
    source_aliases: Sequence[str],
    disclosed_scopes: set[str],
    unverifiable: list[str],
) -> tuple[ReviewObjectV1, ...]:
    db_path = material_root / "events.db"
    conn = _open_static_readonly_sqlite(db_path)
    objects: list[ReviewObjectV1] = []
    try:
        tables = {
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }

        if "events" in tables:
            for row in conn.execute(
                "SELECT seq, run_id, task_id, type, ts, actor_kind, payload AS payload_inline, "
                "payload_ref, payload_hash FROM events ORDER BY seq"
            ):
                seq = int(row["seq"])
                key = (_sql_int(seq),)
                raw_payload: bytes | None = None
                mat_rel: tuple[str, ...] | None = None
                if row["payload_ref"] is not None:
                    try:
                        rel_str = backup_mod._reference_rel(
                            row["payload_ref"],
                            source_root,
                            source_aliases=source_aliases,
                        )
                        mat_rel = tuple(rel_str.split("/"))
                        pfile = material_root / Path(*mat_rel)
                        raw_payload = pfile.read_bytes()
                        if _sha256_bytes(raw_payload) != row["payload_hash"]:
                            unverifiable.append(f"events seq={seq} 外部 payload 哈希不符")
                            raw_payload = None
                    except (OSError, ValueError, BackupError) as exc:
                        unverifiable.append(f"events seq={seq} 外部 payload 不可核验：{exc}")
                elif isinstance(row["payload_inline"], str):
                    raw_payload = row["payload_inline"].encode("utf-8")

                bodies = (
                    (_make_body_view("events", key, "payload", raw_payload, disclosed_scopes),)
                    if raw_payload is not None
                    else ()
                )
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("actor_kind", _sql_text(str(row["actor_kind"]))),
                            NamedValueV1("run_id", _sql_text(str(row["run_id"]))),
                            NamedValueV1("task_id", _sql_opt_text(row["task_id"])),
                            NamedValueV1("ts", _sql_int(int(row["ts"]))),
                            NamedValueV1("type", _sql_text(str(row["type"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                sources = [SourceRefV1("events", key, "payload", mat_rel)]
                objects.append(ReviewObjectV1("events", key, status, tuple(sources), bodies))

        if "memories" in tables:
            cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("memories")')}
            erased_expr = "erased_at" if "erased_at" in cols else "NULL AS erased_at"
            for row in conn.execute(
                f"SELECT memory_id, revision, scope, scope_id, kind, content_ref, "
                f"origin, status, content_state, last_seq, {erased_expr} "
                f"FROM memories ORDER BY memory_id"
            ):
                mem_id = str(row["memory_id"])
                key = (_sql_text(mem_id),)
                cref = str(row["content_ref"])
                erased_at = row["erased_at"]
                mat_rel = None
                bodies: tuple[BodyViewV1, ...] = ()
                if erased_at is None:
                    mat_rel = ("memory_blobs", cref)
                    bpath = material_root / "memory_blobs" / cref
                    if bpath.is_file():
                        raw_b = bpath.read_bytes()
                        if _sha256_bytes(raw_b) != cref:
                            unverifiable.append(f"memory {mem_id} 正文哈希不符")
                        else:
                            bodies = (
                                _make_body_view(
                                    "memories", key, "content", raw_b, disclosed_scopes
                                ),
                            )
                    else:
                        unverifiable.append(f"memory {mem_id} 缺少正文材料 {cref}")
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("content_ref", _sql_text(cref)),
                            NamedValueV1("content_state", _sql_text(str(row["content_state"]))),
                            NamedValueV1("erased_at", _sql_opt_int(erased_at)),
                            NamedValueV1("kind", _sql_text(str(row["kind"]))),
                            NamedValueV1("last_seq", _sql_int(int(row["last_seq"]))),
                            NamedValueV1("origin", _sql_text(str(row["origin"]))),
                            NamedValueV1("revision", _sql_int(int(row["revision"]))),
                            NamedValueV1("scope", _sql_text(str(row["scope"]))),
                            NamedValueV1("scope_id", _sql_text(str(row["scope_id"]))),
                            NamedValueV1("status", _sql_text(str(row["status"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                sources = (SourceRefV1("memories", key, "content_ref", mat_rel),)
                objects.append(ReviewObjectV1("memories", key, status, sources, bodies))

        if "tasks" in tables:
            for row in conn.execute(
                "SELECT task_id, goal, goal_revision, lease_epoch, active_run_id, status "
                "FROM tasks ORDER BY task_id"
            ):
                tid = str(row["task_id"])
                key = (_sql_text(tid),)
                goal_bytes = str(row["goal"]).encode("utf-8")
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("active_run_id", _sql_opt_text(row["active_run_id"])),
                            NamedValueV1("goal_revision", _sql_int(int(row["goal_revision"]))),
                            NamedValueV1("lease_epoch", _sql_int(int(row["lease_epoch"]))),
                            NamedValueV1("status", _sql_text(str(row["status"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                bodies = (_make_body_view("tasks", key, "goal", goal_bytes, disclosed_scopes),)
                objects.append(
                    ReviewObjectV1(
                        "tasks",
                        key,
                        status,
                        (SourceRefV1("tasks", key, "goal", None),),
                        bodies,
                    )
                )

        if "runs" in tables:
            for row in conn.execute(
                "SELECT run_id, task_id, goal_revision, lease_epoch, state, outcome, effect_state "
                "FROM runs ORDER BY run_id"
            ):
                rid = str(row["run_id"])
                key = (_sql_text(rid),)
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("effect_state", _sql_opt_text(row["effect_state"])),
                            NamedValueV1("goal_revision", _sql_int(int(row["goal_revision"]))),
                            NamedValueV1("lease_epoch", _sql_int(int(row["lease_epoch"]))),
                            NamedValueV1("outcome", _sql_opt_text(row["outcome"])),
                            NamedValueV1("state", _sql_text(str(row["state"]))),
                            NamedValueV1("task_id", _sql_text(str(row["task_id"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                objects.append(
                    ReviewObjectV1(
                        "runs",
                        key,
                        status,
                        (SourceRefV1("runs", key, "state", None),),
                        (),
                    )
                )

        if "run_cursors" in tables:
            for row in conn.execute(
                "SELECT run_id, task_id, claim_epoch, payload FROM run_cursors ORDER BY run_id"
            ):
                rid = str(row["run_id"])
                key = (_sql_text(rid),)
                raw_c = str(row["payload"]).encode("utf-8")
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("claim_epoch", _sql_int(int(row["claim_epoch"]))),
                            NamedValueV1("task_id", _sql_text(str(row["task_id"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                objects.append(
                    ReviewObjectV1(
                        "run_cursors",
                        key,
                        status,
                        (SourceRefV1("run_cursors", key, "payload", None),),
                        (_make_body_view("run_cursors", key, "payload", raw_c, disclosed_scopes),),
                    )
                )

        if "summary_versions" in tables:
            for row in conn.execute(
                "SELECT summary_id, material_id, version, status, text, model "
                "FROM summary_versions ORDER BY summary_id"
            ):
                sid = str(row["summary_id"])
                key = (_sql_text(sid),)
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("material_id", _sql_text(str(row["material_id"]))),
                            NamedValueV1("model", _sql_text(str(row["model"]))),
                            NamedValueV1("status", _sql_text(str(row["status"]))),
                            NamedValueV1("version", _sql_int(int(row["version"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                bodies = (
                    (
                        _make_body_view(
                            "summaries",
                            key,
                            "text",
                            str(row["text"]).encode("utf-8"),
                            disclosed_scopes,
                        ),
                    )
                    if row["text"] is not None
                    else ()
                )
                objects.append(
                    ReviewObjectV1(
                        "summaries",
                        key,
                        status,
                        (SourceRefV1("summary_versions", key, "text", None),),
                        bodies,
                    )
                )

        if "approvals" in tables:
            for row in conn.execute(
                "SELECT approval_id, run_id, action, state, expires_at "
                "FROM approvals ORDER BY approval_id"
            ):
                aid = str(row["approval_id"])
                key = (_sql_text(aid),)
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("action", _sql_text(str(row["action"]))),
                            NamedValueV1("expires_at", _sql_int(int(row["expires_at"]))),
                            NamedValueV1("run_id", _sql_text(str(row["run_id"]))),
                            NamedValueV1("state", _sql_text(str(row["state"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                objects.append(
                    ReviewObjectV1(
                        "approvals",
                        key,
                        status,
                        (SourceRefV1("approvals", key, "state", None),),
                        (),
                    )
                )

        if "grants" in tables:
            for row in conn.execute(
                "SELECT grant_id, run_id, action, remaining_uses, expires_at "
                "FROM grants ORDER BY grant_id"
            ):
                gid = str(row["grant_id"])
                key = (_sql_text(gid),)
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("action", _sql_text(str(row["action"]))),
                            NamedValueV1("expires_at", _sql_int(int(row["expires_at"]))),
                            NamedValueV1("remaining_uses", _sql_int(int(row["remaining_uses"]))),
                            NamedValueV1("run_id", _sql_text(str(row["run_id"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                objects.append(
                    ReviewObjectV1(
                        "grants",
                        key,
                        status,
                        (SourceRefV1("grants", key, "remaining_uses", None),),
                        (),
                    )
                )

        if "leases" in tables:
            for row in conn.execute(
                "SELECT resource_key, run_id, owner_pid, expires_at "
                "FROM leases ORDER BY resource_key"
            ):
                rkey = str(row["resource_key"])
                key = (_sql_text(rkey),)
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("expires_at", _sql_int(int(row["expires_at"]))),
                            NamedValueV1("owner_pid", _sql_int(int(row["owner_pid"]))),
                            NamedValueV1("run_id", _sql_text(str(row["run_id"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                objects.append(
                    ReviewObjectV1(
                        "leases",
                        key,
                        status,
                        (SourceRefV1("leases", key, "expires_at", None),),
                        (),
                    )
                )

        if "jobs" in tables:
            for row in conn.execute("SELECT job_id, run_id, state FROM jobs ORDER BY job_id"):
                jid = str(row["job_id"])
                key = (_sql_text(jid),)
                status = tuple(
                    sorted(
                        (
                            NamedValueV1("run_id", _sql_text(str(row["run_id"]))),
                            NamedValueV1("state", _sql_text(str(row["state"]))),
                        ),
                        key=lambda item: item.name.encode("utf-8"),
                    )
                )
                objects.append(
                    ReviewObjectV1(
                        "jobs",
                        key,
                        status,
                        (SourceRefV1("jobs", key, "state", None),),
                        (),
                    )
                )
    finally:
        conn.close()

    for sub_family, sub_rel in (
        ("snapshots", ("artifacts", "snapshots")),
        ("undo_journals", ("artifacts", "undo")),
    ):
        sub_dir = material_root / Path(*sub_rel)
        if sub_dir.is_dir():
            for path in sorted(sub_dir.glob("*.json"), key=lambda p: p.name):
                rel_parts = path.relative_to(material_root).parts
                rel_posix = "/".join(rel_parts)
                key = (_sql_text(rel_posix),)
                raw_j = path.read_bytes()
                status = (NamedValueV1("size", _sql_int(len(raw_j))),)
                objects.append(
                    ReviewObjectV1(
                        sub_family,
                        key,
                        status,
                        (SourceRefV1("materials", key, None, rel_parts),),
                        (_make_body_view(sub_family, key, "json", raw_j, disclosed_scopes),),
                    )
                )

    return tuple(
        sorted(objects, key=lambda obj: (obj.family.encode("utf-8"), restore_json_v1(obj.key)))
    )


def _object_state_digest(obj: ReviewObjectV1) -> str:
    body_summaries = [(b.field, b.size, b.sha256) for b in obj.bodies]
    return _sha256_bytes(
        restore_json_v1(
            {
                "family": obj.family,
                "key": [k.to_wire() for k in obj.key],
                "status": [(s.name, s.value.to_wire()) for s in obj.status],
                "bodies": body_summaries,
            }
        )
    )


def _compute_review_locked(
    resolved_bid: str,
    reserved: ReservedV1,
    sealed: SealedHoldV1,
    expected_origin_bytes: bytes,
    *,
    later_backup: Path | None,
    normalized_scopes: Sequence[str],
    seen_scopes: set[str],
) -> RestoreReviewV2:
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    checks: list[CheckV1] = []
    unverifiable: list[str] = []

    try:
        _verify_placement_ancestors(
            placement,
            batch_ctx={"batch_id": resolved_bid, "phase": "published_hold"},
        )
        checks.append(CheckV1("stable_placement", True, (placement.canonical_root.text,)))
    except RestoreOperationError as exc:
        checks.append(CheckV1("stable_placement", False, (exc.failure.message,)))
        unverifiable.append(exc.failure.message)

    try:
        _verify_material_tree_matches_binding(
            final_root,
            sealed.binding,
            allow_token=True,
            require_origin_bytes=expected_origin_bytes,
            batch_ctx={"batch_id": resolved_bid, "phase": "published_hold"},
        )
        checks.append(
            CheckV1(
                "binding_and_materials",
                True,
                (sealed.binding_sha256, sealed.binding.scope_sha256),
            )
        )
    except RestoreOperationError as exc:
        checks.append(CheckV1("binding_and_materials", False, (exc.failure.message,)))
        unverifiable.append(exc.failure.message)

    try:
        manifest_raw = base64.b64decode(sealed.binding.manifest.saved_bytes_b64)
        saved_manifest = BackupManifest.from_dict(json.loads(manifest_raw.decode("utf-8")))
        _ = _build_legacy_reconciliation_and_semantic_check(
            final_root,
            source_root=Path(sealed.binding.saved_source_path),
            source_aliases=sealed.binding.source_aliases,
            db_user_version=saved_manifest.db_user_version,
            batch_ctx={"batch_id": resolved_bid, "phase": "published_hold"},
        )
        checks.append(
            CheckV1("semantic_closures", True, ("events", "projections", "fts", "summaries"))
        )
    except (RestoreOperationError, ValueError, TypeError) as exc:
        msg = exc.failure.message if isinstance(exc, RestoreOperationError) else str(exc)
        checks.append(CheckV1("semantic_closures", False, (msg,)))
        unverifiable.append(msg)

    objects = _scan_review_objects(
        final_root,
        source_root=Path(sealed.binding.saved_source_path),
        source_aliases=sealed.binding.source_aliases,
        disclosed_scopes=seen_scopes,
        unverifiable=unverifiable,
    )

    available_selectors = {b.selector for obj in objects for b in obj.bodies}
    unknown_scopes = [s for s in normalized_scopes if s not in available_selectors]
    if unknown_scopes:
        raise _fail(
            "incompatible",
            f"--body-scope 没有匹配本次复核中的任何正文槽位：{unknown_scopes}",
            batch_id=resolved_bid,
            phase="published_hold",
            plan_sha256=reserved.plan_sha256,
        )

    later_changes: list[LaterChangeV1] = []
    if later_backup is not None:
        later_path = Path(later_backup)
        later_ver = verify_backup(later_path)
        if not later_ver.ok or later_ver.manifest is None:
            reason = "；".join(later_ver.problems) if later_ver.problems else "后续备份校验失败"
            checks.append(CheckV1("later_backup", False, (reason,)))
            unverifiable.append(f"later_backup: {reason}")
        else:
            l_manifest = later_ver.manifest
            same_src = overlap_key(l_manifest.source_path) == overlap_key(
                sealed.binding.saved_source_path
            )
            if not same_src:
                reason = (
                    f"后续备份源路径 {l_manifest.source_path} 与当前恢复源 "
                    f"{sealed.binding.saved_source_path} 不一致"
                )
                checks.append(CheckV1("later_backup", False, (reason,)))
                unverifiable.append(reason)
            else:
                later_unv: list[str] = []
                later_objs = _scan_review_objects(
                    later_path.resolve(strict=True),
                    source_root=Path(l_manifest.source_path),
                    source_aliases=l_manifest.source_aliases,
                    disclosed_scopes=set(),
                    unverifiable=later_unv,
                )
                if later_unv:
                    checks.append(CheckV1("later_backup", False, tuple(later_unv)))
                    unverifiable.extend(later_unv)
                else:
                    checks.append(CheckV1("later_backup", True, (str(later_path.resolve()),)))
                    cur_map = {(o.family, o.key): o for o in objects}
                    lat_map = {(o.family, o.key): o for o in later_objs}
                    all_keys = sorted(
                        set(cur_map) | set(lat_map),
                        key=lambda fk: (fk[0].encode("utf-8"), restore_json_v1(fk[1])),
                    )
                    for fk in all_keys:
                        c_obj = cur_map.get(fk)
                        l_obj = lat_map.get(fk)
                        c_sha = _object_state_digest(c_obj) if c_obj is not None else None
                        l_sha = _object_state_digest(l_obj) if l_obj is not None else None
                        if c_sha == l_sha:
                            continue
                        ev_notes: list[str] = []
                        if c_obj is None:
                            ev_notes.append("added_in_later_backup")
                        elif l_obj is None:
                            ev_notes.append("absent_in_later_backup")
                        else:
                            c_stat = {s.name: s.value for s in c_obj.status}
                            l_stat = {s.name: s.value for s in l_obj.status}
                            for s_name in sorted(set(c_stat) | set(l_stat)):
                                if c_stat.get(s_name) != l_stat.get(s_name):
                                    ev_notes.append(f"field_changed:{s_name}")
                            if not ev_notes:
                                ev_notes.append("body_changed")
                        later_changes.append(
                            LaterChangeV1(
                                family=fk[0],
                                key=fk[1],
                                before_sha256=c_sha,
                                after_sha256=l_sha,
                                evidence=tuple(ev_notes),
                            )
                        )

    review_complete = not unverifiable and all(c.passed for c in checks)
    redacted_objs = tuple(
        ReviewObjectV1(
            family=obj.family,
            key=obj.key,
            status=obj.status,
            sources=obj.sources,
            bodies=tuple(
                BodyViewV1(b.field, b.size, b.sha256, b.selector, None, None)
                for b in obj.bodies
            ),
        )
        for obj in objects
    )
    redacted_v1 = RestoreReviewV1(
        batch_id=resolved_bid,
        phase="published_hold",
        held=True,
        data_root=placement.canonical_root,
        binding_sha256=sealed.binding_sha256,
        scope_sha256=sealed.binding.scope_sha256,
        review_complete=review_complete,
        checks=tuple(checks),
        objects=redacted_objs,
        known_later_changes=tuple(later_changes),
        unverifiable=tuple(unverifiable),
        later_forget_delete_history="completeness_unproven",
        body_scopes=(),
    )
    review_sha = domain_digest("review", redacted_v1)
    return RestoreReviewV2(
        batch_id=resolved_bid,
        phase="published_hold",
        held=True,
        data_root=placement.canonical_root,
        binding_sha256=sealed.binding_sha256,
        scope_sha256=sealed.binding.scope_sha256,
        review_complete=review_complete,
        checks=tuple(checks),
        objects=objects,
        known_later_changes=tuple(later_changes),
        unverifiable=tuple(unverifiable),
        later_forget_delete_history="completeness_unproven",
        body_scopes=tuple(normalized_scopes),
        review_sha256=review_sha,
    )


def review(
    data_root: Path,
    *,
    batch_id: str | None = None,
    later_backup: Path | None = None,
    body_scopes: Sequence[str] = (),
    registry: RestoreRegistry | None = None,
) -> RestoreReviewV2:
    """对处于 `published_hold` 的数据根做零副作用只读复核；默认脱敏全部正文。"""
    reg = _registry_or_default(registry)
    obs, resolved_bid, b_events = _resolve_batch_observation(
        reg,
        data_root,
        batch_id,
        allow_aborted=False,
    )
    reserved: ReservedV1 = b_events[0].payload
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    final_db = final_root / "events.db"
    tail = b_events[-1]

    if tail.kind != "published_hold":
        raise _fail(
            "held" if tail.kind not in ("aborted", "released") else "incompatible",
            f"restore-review 只接受处于 published_hold 的恢复批次（当前为 {tail.kind}）",
            batch_id=resolved_bid,
            phase=tail.kind,
            plan_sha256=reserved.plan_sha256,
            next_commands=_next_commands_for_phase(str(final_root), tail.kind),
        )

    history = {e.kind: e for e in b_events}
    sealed: SealedHoldV1 = history["sealed_hold"].payload
    intent: PublishIntentV1 = history["publish_intent"].payload
    published: PublishedHoldV1 = tail.payload
    expected_origin_bytes = base64.b64decode(intent.origin_bytes_b64)

    normalized_scopes: list[str] = []
    seen_scopes: set[str] = set()
    for raw_scope in body_scopes:
        try:
            sel = BodySelectorV1.from_selector(raw_scope).to_selector()
        except RestoreContractError as exc:
            raise _fail(
                "incompatible",
                f"--body-scope 选择器格式非法：{raw_scope!r}（{exc}）",
                batch_id=resolved_bid,
                phase="published_hold",
                plan_sha256=reserved.plan_sha256,
                cause=exc,
            ) from exc
        if sel not in seen_scopes:
            seen_scopes.add(sel)
            normalized_scopes.append(sel)

    gate = None
    ns_shared = None
    ent_shared = None
    s_handle = None
    d_handle = None
    try:
        try:
            gate = acquire_restore_gate(exclusive=False)
            ns_shared = acquire_database_access(final_db, exclusive=False)
            reg.revalidate(obs)
            s_handle = open_entity(final_root, "directory")
            d_handle = open_entity(final_db, "file")
            ent_shared = acquire_entity_access(d_handle.identity, exclusive=False)
        except (MaintenanceBusy, RestoreRegistryBusy) as exc:
            raise _fail(
                "maintenance_busy",
                f"无法取得只读复核共享访问锁：{exc}",
                batch_id=resolved_bid,
                phase="published_hold",
                plan_sha256=reserved.plan_sha256,
                cause=exc,
            ) from exc
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt",
                f"published_hold 数据根或事件库缺失/被替换：{exc}",
                batch_id=resolved_bid,
                phase="published_hold",
                plan_sha256=reserved.plan_sha256,
                cause=exc,
            ) from exc

        if (
            s_handle.identity != published.final_root.identity
            or d_handle.identity != published.final_database.identity
            or d_handle.link_count != 1
        ):
            raise _fail(
                "binding_mismatch",
                "published_hold 数据根或事件库实体身份已偏离已发布事实",
                batch_id=resolved_bid,
                phase="published_hold",
                plan_sha256=reserved.plan_sha256,
            )

        gate.close()
        gate = None

        review_report = _compute_review_locked(
            resolved_bid,
            reserved,
            sealed,
            expected_origin_bytes,
            later_backup=later_backup,
            normalized_scopes=normalized_scopes,
            seen_scopes=seen_scopes,
        )
        if not review_report.review_complete:
            raise _fail(
                "incompatible" if later_backup is not None else "binding_mismatch",
                "只读复核未完整通过："
                + ("；".join(review_report.unverifiable) or "存在失败检查项"),
                batch_id=resolved_bid,
                phase="published_hold",
                plan_sha256=reserved.plan_sha256,
                retained_paths=(placement.canonical_root,),
                next_commands=_next_commands_for_phase(str(final_root), "published_hold"),
                partial_review=review_report,
            )
        return review_report
    finally:
        if d_handle is not None:
            d_handle.close()
        if s_handle is not None:
            s_handle.close()
        if ent_shared is not None:
            ent_shared.close()
        if ns_shared is not None:
            ns_shared.close()
        if gate is not None:
            gate.close()


def _dispatch_proven_unsent(conn: sqlite3.Connection, run_id: str, call_id: str) -> bool:
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if "dispatches" not in tables:
        return False
    cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("dispatches")')}
    if "boundary_protocol" not in cols:
        return False
    rows = conn.execute(
        "SELECT state, boundary_protocol FROM dispatches WHERE run_id=? AND call_id=?",
        (run_id, call_id),
    ).fetchall()
    if not rows:
        return False
    return all(
        int(r["boundary_protocol"]) >= 1 and str(r["state"]) in ("intent", "rejected", "revoked")
        for r in rows
    )


def _scan_active_blocks_on_conn(
    conn: sqlite3.Connection,
    max_seq: int,
) -> tuple[set[str], set[str]]:
    blocked_ws: set[str] = set()
    blocked_objs: set[str] = set()
    for row in conn.execute(
        "SELECT seq, type, payload FROM events "
        "WHERE seq <= ? AND type IN ("
        "'recovery.blocked','recovery.unblocked',"
        "'recovery.object_blocked','recovery.object_unblocked'"
        ") ORDER BY seq",
        (max_seq,),
    ):
        etype = str(row["type"])
        raw = row["payload"]
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if etype in ("recovery.blocked", "recovery.unblocked"):
            ws = parsed.get("workspace")
            if isinstance(ws, str) and ws:
                if etype == "recovery.blocked":
                    blocked_ws.add(ws)
                else:
                    blocked_ws.discard(ws)
        else:
            obj = parsed.get("object")
            if isinstance(obj, str) and obj:
                if etype == "recovery.object_blocked":
                    blocked_objs.add(obj)
                else:
                    blocked_objs.discard(obj)
    return blocked_ws, blocked_objs


def _unknown_call_targets_on_conn(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str | None,
    call_id: str,
    tool_id: str,
) -> tuple[list[str], list[str]]:
    """返回单条未知副作用调用涉及的 `(workspace_roots, mcp_object_keys)`。"""
    from .memory_exec import MEMORY_ALL_TOOL_IDS
    from .resources import ResourceError, mcp_object_key, mcp_server_key, parse_mcp_object_key
    from .tools import NETWORK_TOOL_IDS, SUBTASK_TOOL_IDS

    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    ws_roots: list[str] = []
    obj_keys: list[str] = []

    if tool_id.startswith("mcp__"):
        if "dispatches" in tables:
            cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("dispatches")')}
            if "target" in cols:
                for row in conn.execute(
                    "SELECT target FROM dispatches WHERE run_id=? AND call_id=?",
                    (run_id, call_id),
                ):
                    raw_t = str(row["target"] or "")
                    if not raw_t:
                        continue
                    try:
                        server, tool = parse_mcp_object_key(raw_t)
                    except ResourceError:
                        continue
                    canonical = (
                        mcp_object_key(server, tool)
                        if tool is not None
                        else mcp_server_key(server)
                    )
                    if canonical not in obj_keys:
                        obj_keys.append(canonical)
        return ws_roots, obj_keys

    if (
        tool_id in MEMORY_ALL_TOOL_IDS
        or tool_id in SUBTASK_TOOL_IDS
        or tool_id in NETWORK_TOOL_IDS
    ):
        return ws_roots, obj_keys

    if "dispatches" in tables:
        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("dispatches")')}
        if "workspace_root" in cols and "resource_key" in cols:
            for row in conn.execute(
                "SELECT workspace_root, resource_key FROM dispatches WHERE run_id=? AND call_id=?",
                (run_id, call_id),
            ):
                rkey = str(row["resource_key"] or "")
                wroot = str(row["workspace_root"] or "")
                if rkey.startswith(("memory:", "network:", "mcp:", "subtask:")) or not wroot:
                    continue
                if wroot not in ws_roots:
                    ws_roots.append(wroot)

    if not ws_roots and task_id and "tasks" in tables:
        trow = conn.execute(
            "SELECT workspaces FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if trow is not None and trow["workspaces"]:
            try:
                bindings = json.loads(str(trow["workspaces"]))
                if isinstance(bindings, list):
                    writable = [
                        str(b["root_path"])
                        for b in bindings
                        if isinstance(b, dict)
                        and isinstance(b.get("root_path"), str)
                        and b.get("mode") == "writable"
                    ]
                    chosen = writable or [
                        str(b["root_path"])
                        for b in bindings
                        if isinstance(b, dict) and isinstance(b.get("root_path"), str)
                    ]
                    for w in chosen:
                        if w not in ws_roots:
                            ws_roots.append(w)
            except (ValueError, TypeError):
                pass
    return ws_roots, obj_keys


def _plan_release_reconciliation(
    final_root: Path,
    binding: BindingV1,
    *,
    batch_ctx: dict[str, Any],
) -> tuple[int, int, ReconcilePlanV1]:
    db_path = final_root / "events.db"
    conn = _open_static_readonly_sqlite(db_path, batch_ctx=batch_ctx)
    try:
        uv_raw = conn.execute("PRAGMA user_version").fetchone()[0]
        if type(uv_raw) is not int or uv_raw not in (0, 1, 2):
            raise _fail(
                "incompatible",
                f"不支持的事件库 user_version：{uv_raw!r}",
                **batch_ctx,
            )
        seq_head = int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])
        max_ts = int(conn.execute("SELECT COALESCE(MAX(ts), 0) FROM events").fetchone()[0])
        reconciled_at_ms = max(int(time.time() * 1000), max_ts)

        tables = {
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        fenced_runs = (
            sorted(
                {
                    str(r["run_id"])
                    for r in conn.execute("SELECT run_id, state FROM runs")
                    if str(r["state"]) in NON_TERMINAL_STATES
                },
                key=lambda s: s.encode("utf-8"),
            )
            if "runs" in tables
            else []
        )
        expired_approvals = (
            sorted(
                {
                    str(r["approval_id"])
                    for r in conn.execute("SELECT approval_id, state FROM approvals")
                    if str(r["state"]) in ("pending", "approved")
                },
                key=lambda s: s.encode("utf-8"),
            )
            if "approvals" in tables
            else []
        )
        cleared_leases = (
            sorted(
                {str(r["resource_key"]) for r in conn.execute("SELECT resource_key FROM leases")},
                key=lambda s: s.encode("utf-8"),
            )
            if "leases" in tables
            else []
        )
        cancelled_jobs = (
            sorted(
                {
                    str(r["job_id"])
                    for r in conn.execute("SELECT job_id, state FROM jobs")
                    if str(r["state"]) not in ("done", "cancelled")
                },
                key=lambda s: s.encode("utf-8"),
            )
            if "jobs" in tables
            else []
        )

        source_root = Path(binding.saved_source_path)
        intents: list[dict[str, Any]] = []
        last_result: dict[tuple[str, str], int] = {}
        for row in conn.execute(
            "SELECT seq, run_id, task_id, type, payload AS payload_inline, "
            "payload_ref, payload_hash FROM events "
            "WHERE type IN ('tool.intent', 'tool.result') ORDER BY seq"
        ):
            seq = int(row["seq"])
            etype = str(row["type"])
            run_id = str(row["run_id"])
            payload = read_saved_event_payload(
                final_root,
                row,
                source_root=source_root,
                source_aliases=binding.source_aliases,
            )
            call_id = str(payload["call_id"])
            if etype == "tool.result":
                last_result[(run_id, call_id)] = seq
            else:
                intents.append(
                    {
                        "seq": seq,
                        "run_id": run_id,
                        "task_id": str(row["task_id"]) if row["task_id"] else None,
                        "call_id": call_id,
                        "tool_id": str(payload["tool_id"]),
                    }
                )

        active_ws, active_objs = _scan_active_blocks_on_conn(conn, seq_head)
        active_ws_keys = {overlap_key(w) for w in active_ws}
        new_blocked_ws: set[str] = set()
        new_blocked_objs: set[str] = set()
        dispositions: list[ReconcileIntentDispositionV1] = []

        for item in intents:
            if last_result.get((item["run_id"], item["call_id"]), 0) >= item["seq"]:
                continue
            from .tools import SPECS

            spec = SPECS.get(item["tool_id"])
            proven_unsent = (
                (spec is not None and spec.side_effect_level == "pure")
                or _dispatch_proven_unsent(conn, item["run_id"], item["call_id"])
            )
            disp_val = "none_applied" if proven_unsent else "unknown"
            dispositions.append(
                ReconcileIntentDispositionV1(
                    run_id=item["run_id"],
                    call_id=item["call_id"],
                    seq=item["seq"],
                    tool_id=item["tool_id"],
                    disposition=disp_val,
                )
            )
            if disp_val == "unknown":
                ws_list, obj_list = _unknown_call_targets_on_conn(
                    conn,
                    run_id=item["run_id"],
                    task_id=item["task_id"],
                    call_id=item["call_id"],
                    tool_id=item["tool_id"],
                )
                for ws in ws_list:
                    wkey = overlap_key(ws)
                    if wkey not in active_ws_keys:
                        active_ws_keys.add(wkey)
                        new_blocked_ws.add(ws)
                for obj in obj_list:
                    if obj not in active_objs:
                        active_objs.add(obj)
                        new_blocked_objs.add(obj)

        sorted_disps = tuple(
            sorted(
                dispositions,
                key=lambda d: (d.seq, d.run_id.encode("utf-8"), d.call_id.encode("utf-8")),
            )
        )
        plan_obj = ReconcilePlanV1(
            version=1,
            reconciled_at_ms=reconciled_at_ms,
            fenced_run_ids=tuple(fenced_runs),
            expired_approval_ids=tuple(expired_approvals),
            cleared_lease_keys=tuple(cleared_leases),
            unresolved_intents=sorted_disps,
            cancelled_job_ids=tuple(cancelled_jobs),
            blocked_workspaces=tuple(sorted(new_blocked_ws, key=lambda s: s.encode("utf-8"))),
            blocked_objects=tuple(sorted(new_blocked_objs, key=lambda s: s.encode("utf-8"))),
        )
        return uv_raw, seq_head, plan_obj
    finally:
        conn.close()


def _build_expected_r2_events(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    reconcile_plan: ReconcilePlanV1,
    pre_seq_head: int,
) -> list[dict[str, Any]]:
    """从不可变历史前缀与持久投影键确定性生成阶段 R2 应追加的事件序列。"""
    from .events import canonical_bytes, sha256_hex

    ts = reconcile_plan.reconciled_at_ms
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}

    intent_task_map: dict[int, str | None] = {}
    for row in conn.execute(
        "SELECT seq, task_id FROM events WHERE seq <= ? AND type='tool.intent'",
        (pre_seq_head,),
    ):
        intent_task_map[int(row["seq"])] = str(row["task_id"]) if row["task_id"] else None

    expected: list[dict[str, Any]] = []
    ws_attr: dict[str, tuple[str, list[str]]] = {}
    obj_attr: dict[str, tuple[str, list[str]]] = {}

    # 1) 未决工具意图 -> tool.result
    for disp in reconcile_plan.unresolved_intents:
        t_id = intent_task_map.get(disp.seq)
        if disp.disposition == "none_applied":
            payload = {
                "call_id": disp.call_id,
                "tool_id": disp.tool_id,
                "status": "failed",
                "effect_state": "none_applied",
                "output": {"reason": "restore.reconciled_unsent", "batch_id": batch_id},
            }
        else:
            payload = {
                "call_id": disp.call_id,
                "tool_id": disp.tool_id,
                "status": "unknown",
                "effect_state": "unknown",
                "output": {"reason": "restore.reconciled_unknown", "batch_id": batch_id},
            }
            ws_list, obj_list = _unknown_call_targets_on_conn(
                conn,
                run_id=disp.run_id,
                task_id=t_id,
                call_id=disp.call_id,
                tool_id=disp.tool_id,
            )
            for ws in ws_list:
                prev_run, cids = ws_attr.get(ws, (disp.run_id, []))
                if disp.call_id not in cids:
                    cids.append(disp.call_id)
                ws_attr[ws] = (prev_run, cids)
            for obj in obj_list:
                prev_run, cids = obj_attr.get(obj, (disp.run_id, []))
                if disp.call_id not in cids:
                    cids.append(disp.call_id)
                obj_attr[obj] = (prev_run, cids)

        expected.append(
            {
                "run_id": disp.run_id,
                "task_id": t_id,
                "type": "tool.result",
                "ts": ts,
                "actor_kind": "core",
                "actor_id": "",
                "payload": payload,
                "idem_key": disp.call_id,
            }
        )

    # 1b) 新增目录与 MCP 对象阻断事件
    for ws in reconcile_plan.blocked_workspaces:
        b_run_id, cids = ws_attr.get(ws, ("", []))
        expected.append(
            {
                "run_id": b_run_id,
                "task_id": None,
                "type": "recovery.blocked",
                "ts": ts,
                "actor_kind": "system",
                "actor_id": "",
                "payload": {
                    "workspace": ws,
                    "call_ids": sorted(cids),
                    "reason": "restore.release_reconciled",
                },
                "idem_key": None,
            }
        )

    for obj in reconcile_plan.blocked_objects:
        b_run_id, cids = obj_attr.get(obj, ("", []))
        expected.append(
            {
                "run_id": b_run_id,
                "task_id": None,
                "type": "recovery.object_blocked",
                "ts": ts,
                "actor_kind": "system",
                "actor_id": "",
                "payload": {
                    "object": obj,
                    "call_ids": sorted(cids),
                    "reason": "restore.release_reconciled",
                },
                "idem_key": None,
            }
        )

    # 2) 陈旧审批 -> approval.expired
    if "approvals" in tables:
        app_map = {
            str(r["approval_id"]): (
                str(r["run_id"]),
                str(r["task_id"]) if r["task_id"] else None,
            )
            for r in conn.execute("SELECT approval_id, run_id, task_id FROM approvals")
        }
        for aid in reconcile_plan.expired_approval_ids:
            a_run_id, a_task_id = app_map[aid]
            expected.append(
                {
                    "run_id": a_run_id,
                    "task_id": a_task_id,
                    "type": "approval.expired",
                    "ts": ts,
                    "actor_kind": "core",
                    "actor_id": "",
                    "payload": {"approval_id": aid, "reason": "restore.release_reconciled"},
                    "idem_key": None,
                }
            )

    # 3) 导入的非终态 runs -> run.fenced
    if "runs" in tables:
        run_map = {
            str(r["run_id"]): str(r["task_id"])
            for r in conn.execute("SELECT run_id, task_id FROM runs")
        }
        for rid in reconcile_plan.fenced_run_ids:
            r_task_id = run_map[rid]
            expected.append(
                {
                    "run_id": rid,
                    "task_id": r_task_id,
                    "type": "run.fenced",
                    "ts": ts,
                    "actor_kind": "core",
                    "actor_id": "",
                    "payload": {
                        "run_id": rid,
                        "task_id": r_task_id,
                        "reason": "restore.release_reconciled",
                    },
                    "idem_key": None,
                }
            )

    # 4) 导入的非终态 jobs -> job.finished
    if "jobs" in tables:
        job_map = {
            str(r["job_id"]): r
            for r in conn.execute(
                "SELECT job_id, run_id, claim_epoch, owner_pid, owner_instance_id FROM jobs"
            )
        }
        for jid in reconcile_plan.cancelled_job_ids:
            jrow = job_map[jid]
            expected.append(
                {
                    "run_id": str(jrow["run_id"]),
                    "task_id": None,
                    "type": "job.finished",
                    "ts": ts,
                    "actor_kind": "core",
                    "actor_id": "",
                    "payload": {
                        "run_id": str(jrow["run_id"]),
                        "job_id": jid,
                        "claim_epoch": int(jrow["claim_epoch"] or 0),
                        "owner_pid": (
                            int(jrow["owner_pid"]) if jrow["owner_pid"] is not None else None
                        ),
                        "owner_instance_id": (
                            str(jrow["owner_instance_id"])
                            if jrow["owner_instance_id"] is not None
                            else None
                        ),
                        "outcome": "cancelled",
                        "by": "restore_release",
                        "reason": "restore.release_reconciled",
                    },
                    "idem_key": None,
                }
            )

    for item in expected:
        ver = event_schema.detect_version(item["type"], item["payload"], allow_legacy=False)
        raw_bytes = canonical_bytes(item["payload"])
        item["version"] = ver
        item["payload_json"] = raw_bytes.decode("utf-8")
        item["payload_hash"] = sha256_hex(raw_bytes)
    return expected


def _verify_events_prefix_matches_scope(
    db_path: Path,
    scope: ScopeManifestV1,
    pre_seq_head: int,
    *,
    batch_ctx: dict[str, Any],
) -> None:
    events_rel = next((r for r in scope.relations if r.name == "events"), None)
    if events_rel is None:
        raise _fail("record_corrupt", "ScopeManifestV1 缺少 events 表摘要", **batch_ctx)
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    conn.text_factory = bytes
    try:
        ddl_row = conn.execute(
            "SELECT typeof(sql), sql, CAST(sql AS BLOB) FROM sqlite_schema "
            "WHERE type='table' AND name='events'"
        ).fetchone()
        if ddl_row is None:
            raise _fail("record_corrupt", "主库缺少 events 表定义", **batch_ctx)
        ddl_tag = ddl_row[0].decode("ascii")
        ddl_val = sql_value_from_sqlite(
            ddl_tag, ddl_row[1], text_bytes=ddl_row[2] if ddl_tag == "text" else None
        )
        select_exprs = []
        for col in events_rel.columns:
            col_esc = col.name.replace('"', '""')
            select_exprs.append(f'typeof("{col_esc}"), "{col_esc}", CAST("{col_esc}" AS BLOB)')
        typed_rows: list[tuple[SqlValueV1, ...]] = []
        query = f'SELECT {", ".join(select_exprs)} FROM "events" WHERE seq <= ?'
        for data_row in conn.execute(query, (pre_seq_head,)):
            row_vals: list[SqlValueV1] = []
            for c_idx in range(len(events_rel.columns)):
                tag = data_row[c_idx * 3].decode("ascii")
                val = data_row[c_idx * 3 + 1]
                tbytes = data_row[c_idx * 3 + 2] if tag == "text" else None
                row_vals.append(sql_value_from_sqlite(tag, val, text_bytes=tbytes))
            typed_rows.append(tuple(row_vals))
        sql_rel = make_sql_relation("events", events_rel.columns, ddl_val, tuple(typed_rows))
        if (
            len(sql_rel.rows) != events_rel.row_count
            or domain_digest("sql-relation", sql_rel) != events_rel.rows_sha256
        ):
            raise _fail(
                "record_corrupt",
                "主库历史事件前缀已偏离封存 BindingV1",
                **batch_ctx,
            )
    finally:
        conn.close()


def _derive_run_effect_state(
    conn: sqlite3.Connection,
    run_id: str,
    final_root: Path,
    binding: BindingV1,
) -> str:
    source_root = Path(binding.saved_source_path)
    states: list[str] = []
    for row in conn.execute(
        "SELECT seq, run_id, type, payload AS payload_inline, payload_ref, payload_hash "
        "FROM events WHERE run_id=? AND type='tool.result' ORDER BY seq",
        (run_id,),
    ):
        payload = read_saved_event_payload(
            final_root,
            row,
            source_root=source_root,
            source_aliases=binding.source_aliases,
        )
        est = str(payload.get("effect_state", "none_applied"))
        states.append(est)
    if "unknown" in states:
        return "unknown"
    if "partial" in states:
        return "partial"
    if "applied" in states:
        return "partial" if "none_applied" in states else "applied"
    return "none_applied"


def _budget_account_chain(conn: sqlite3.Connection, account_id: str) -> list[str]:
    chain: list[str] = []
    cur: str | None = account_id
    for _ in range(16):
        if not cur:
            break
        row = conn.execute(
            "SELECT account_id, parent_id FROM budget_accounts WHERE account_id=?",
            (cur,),
        ).fetchone()
        if row is None:
            break
        chain.append(str(row["account_id"]))
        cur = str(row["parent_id"]) if row["parent_id"] else None
    return chain


def _execute_release_reconciliation(
    final_root: Path,
    d_handle: NativeEntityHandle,
    sealed: SealedHoldV1,
    rel_intent: ReleaseIntentV1,
    *,
    batch_id: str,
    batch_ctx: dict[str, Any],
) -> None:
    """阶段 R2：主库单事务 append-only 对账收口（支持崩溃重入幂等验证）。"""
    from . import authority
    from .authority import AuthorityUnavailable, AuthorityWriteFailed

    final_db = final_root / "events.db"
    pre_seq_head = rel_intent.pre_release_event_seq_head
    rplan = rel_intent.reconcile_plan
    ts = rplan.reconciled_at_ms

    # 检查是否存在未提交崩溃残留伴随日志：打开普通读写连接触发 SQLite 原生回滚后重核字节/状态。
    has_companions = any(
        final_db.with_name(final_db.name + sfx).exists() for sfx in ("-journal", "-wal", "-shm")
    )
    if has_companions:
        rec_conn = sqlite3.connect(str(final_db), isolation_level=None, timeout=0)
        try:
            rec_conn.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
            jmode = str(rec_conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if jmode == "wal":
                rec_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                rec_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            rec_conn.close()
        _fsync_file_path(final_db, batch_ctx=batch_ctx)

    cur_size = final_db.stat().st_size
    cur_sha = sha256_file(final_db)
    untouched = (
        cur_size == sealed.binding.database_bytes.size
        and cur_sha == sealed.binding.database_bytes.sha256
    )

    conn = sqlite3.connect(str(final_db), isolation_level=None, timeout=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=0")
    conn.execute("PRAGMA synchronous=FULL")
    try:
        expected_events = _build_expected_r2_events(
            conn,
            batch_id=batch_id,
            reconcile_plan=rplan,
            pre_seq_head=pre_seq_head,
        )
        cur_max_seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])

        if untouched and cur_max_seq == pre_seq_head:
            proven_unsent_calls = {
                (d.run_id, d.call_id)
                for d in rplan.unresolved_intents
                if d.disposition == "none_applied"
            }
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur_uv = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if cur_uv != rel_intent.pre_release_db_user_version:
                    raise _fail("binding_mismatch", "对账前主库 user_version 发生漂移", **batch_ctx)

                for ev in expected_events:
                    cur = conn.execute(
                        "INSERT INTO events "
                        "(run_id, task_id, type, ts, actor_kind, actor_id, "
                        "payload, payload_ref, payload_hash, parent_seq, idem_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)",
                        (
                            ev["run_id"],
                            ev["task_id"],
                            ev["type"],
                            ev["ts"],
                            ev["actor_kind"],
                            ev["actor_id"],
                            ev["payload_json"],
                            ev["payload_hash"],
                            ev["idem_key"],
                        ),
                    )
                    seq_inserted = int(cur.lastrowid)
                    conn.execute(
                        "INSERT INTO event_versions (seq, type, version) VALUES (?, ?, ?)",
                        (seq_inserted, ev["type"], ev["version"]),
                    )

                tables = {
                    str(r[0])
                    for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
                }

                if "dispatches" in tables:
                    for disp in rplan.unresolved_intents:
                        reason_val = (
                            "restore.reconciled_unsent"
                            if disp.disposition == "none_applied"
                            else "restore.reconciled_unknown"
                        )
                        conn.execute(
                            "UPDATE dispatches SET state='error', reason=?, updated_at=? "
                            "WHERE run_id=? AND call_id=? AND state IN ('intent', 'executing')",
                            (reason_val, ts, disp.run_id, disp.call_id),
                        )
                    conn.execute(
                        "UPDATE dispatches SET state='error', reason='restore.release_reconciled', "
                        "updated_at=? WHERE state IN ('intent', 'executing')",
                        (ts,),
                    )

                if "dispatch_requests" in tables:
                    conn.execute(
                        "UPDATE dispatch_requests SET state='rejected', "
                        "reason='restore.release_reconciled', updated_at=? "
                        "WHERE state IN ('waiting', 'admitted')",
                        (ts,),
                    )

                if "approvals" in tables and rplan.expired_approval_ids:
                    cols_app = {str(r[1]) for r in conn.execute('PRAGMA table_info("approvals")')}
                    for aid in rplan.expired_approval_ids:
                        if "decided_at" in cols_app:
                            conn.execute(
                                "UPDATE approvals SET state='expired', decided_at=? "
                                "WHERE approval_id=? AND state IN ('pending', 'approved')",
                                (ts, aid),
                            )
                        else:
                            conn.execute(
                                "UPDATE approvals SET state='expired' "
                                "WHERE approval_id=? AND state IN ('pending', 'approved')",
                                (aid,),
                            )

                if "grants" in tables:
                    conn.execute(
                        "UPDATE grants SET remaining_uses=0, "
                        "expires_at=CASE WHEN expires_at > ? THEN ? ELSE expires_at END "
                        "WHERE remaining_uses > 0",
                        (ts, ts),
                    )

                if "leases" in tables:
                    conn.execute("DELETE FROM leases")

                if "runs" in tables and rplan.fenced_run_ids:
                    for rid in rplan.fenced_run_ids:
                        eff = _derive_run_effect_state(conn, rid, final_root, sealed.binding)
                        conn.execute(
                            "UPDATE runs SET state='fenced', outcome='cancelled', "
                            "control_requested=NULL, effect_state=?, ended_at=? "
                            "WHERE run_id=?",
                            (eff, ts, rid),
                        )
                        if "tasks" in tables:
                            conn.execute(
                                "UPDATE tasks SET active_run_id=NULL, updated_at=? "
                                "WHERE active_run_id=?",
                                (ts, rid),
                            )

                if "jobs" in tables and rplan.cancelled_job_ids:
                    for jid in rplan.cancelled_job_ids:
                        conn.execute(
                            "UPDATE jobs SET state='cancelled', outcome='cancelled', ended_at=? "
                            "WHERE job_id=?",
                            (ts, jid),
                        )

                if "subtasks" in tables:
                    conn.execute(
                        "UPDATE subtasks SET state='fenced', ended_at=?, "
                        "error='restore.release_reconciled' "
                        "WHERE state IN ('queued', 'running', 'cancelling')",
                        (ts,),
                    )

                if "budget_reservations" in tables:
                    res_rows = conn.execute(
                        "SELECT reservation_id, account_id, run_id, call_id, need_json "
                        "FROM budget_reservations WHERE state IN ('pending', 'reserved') "
                        "ORDER BY reservation_id"
                    ).fetchall()
                    for rrow in res_rows:
                        res_id = str(rrow["reservation_id"])
                        acc_id = str(rrow["account_id"])
                        r_run = str(rrow["run_id"] or "")
                        r_call = str(rrow["call_id"] or "")
                        if r_call and (r_run, r_call) in proven_unsent_calls:
                            try:
                                need = json.loads(str(rrow["need_json"] or "{}"))
                            except ValueError:
                                need = {}
                            if isinstance(need, dict) and "budget_counters" in tables:
                                for lvl in _budget_account_chain(conn, acc_id):
                                    for dim, val in need.items():
                                        if type(val) is int and val > 0:
                                            conn.execute(
                                                "UPDATE budget_counters "
                                                "SET reserved = MAX(0, reserved - ?) "
                                                "WHERE account_id=? AND dimension=?",
                                                (val, lvl, str(dim)),
                                            )
                            conn.execute(
                                "UPDATE budget_reservations SET state='released', known=1, "
                                "settled_at=?, usage_json=? WHERE reservation_id=?",
                                (
                                    ts,
                                    json.dumps({"reason": "restore.reconciled_unsent"}),
                                    res_id,
                                ),
                            )
                        else:
                            # 未知用量保留未决占用，绝不按 0 退款（红线 5/11）。
                            conn.execute(
                                "UPDATE budget_reservations SET state='unknown', known=0, "
                                "settled_at=?, usage_json='{}' WHERE reservation_id=?",
                                (ts, res_id),
                            )

                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        else:
            # 中断重入验证：历史前缀逐位匹配封存快照，新增尾部精确等于预期确定性事件。
            _verify_events_prefix_matches_scope(
                final_db,
                sealed.binding.sqlite_scope,
                pre_seq_head,
                batch_ctx=batch_ctx,
            )
            tail_rows = list(
                conn.execute(
                    "SELECT seq, run_id, task_id, type, ts, actor_kind, actor_id, "
                    "payload_hash, idem_key FROM events WHERE seq > ? ORDER BY seq",
                    (pre_seq_head,),
                )
            )
            if len(tail_rows) != len(expected_events):
                raise _fail(
                    "record_corrupt",
                    f"release_intent 重入时主库尾部事件条数失配："
                    f"实际 {len(tail_rows)}，预期 {len(expected_events)}",
                    **batch_ctx,
                )
            for idx, (actual_row, exp_ev) in enumerate(
                zip(tail_rows, expected_events, strict=True), start=1
            ):
                if (
                    int(actual_row["seq"]) != pre_seq_head + idx
                    or str(actual_row["run_id"]) != exp_ev["run_id"]
                    or (
                        str(actual_row["task_id"])
                        if actual_row["task_id"] is not None
                        else None
                    )
                    != exp_ev["task_id"]
                    or str(actual_row["type"]) != exp_ev["type"]
                    or int(actual_row["ts"]) != exp_ev["ts"]
                    or str(actual_row["actor_kind"]) != exp_ev["actor_kind"]
                    or str(actual_row["actor_id"]) != exp_ev["actor_id"]
                    or str(actual_row["payload_hash"]) != exp_ev["payload_hash"]
                    or (
                        str(actual_row["idem_key"])
                        if actual_row["idem_key"] is not None
                        else None
                    )
                    != exp_ev["idem_key"]
                ):
                    raise _fail(
                        "record_corrupt",
                        f"release_intent 重入时 seq={actual_row['seq']} 与确定性对账计划不符",
                        **batch_ctx,
                    )
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }
            if "leases" in tables:
                rem_leases = int(conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0])
                if rem_leases != 0:
                    raise _fail("record_corrupt", "重入发现主库残留未清理租约", **batch_ctx)

        all_active_ws, all_active_objs = _scan_active_blocks_on_conn(
            conn,
            pre_seq_head + len(expected_events),
        )
        jmode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if jmode == "wal":
            conn.execute("PRAGMA journal_mode=DELETE")
    except RestoreOperationError:
        raise
    except (sqlite3.Error, TianshuError) as exc:
        raise _fail(
            "restore_failed",
            f"主库对账收口失败：{exc}",
            cause=exc,
            **batch_ctx,
        ) from exc
    finally:
        conn.close()

    _fsync_file_path(final_db, batch_ctx=batch_ctx)
    try:
        d_handle.verify_path(final_db)
    except NativeEntityError as exc:
        raise _fail(
            "record_corrupt",
            "对账收口后主库 D 实体身份失配",
            cause=exc,
            **batch_ctx,
        ) from exc

    # 同步登记本机用户级 authority 阻断库（幂等；写失败闭锁报错，绝不宣称解除成功）。
    try:
        for ws in sorted(all_active_ws | set(rplan.blocked_workspaces)):
            authority.register_blocked(ws, reason="restore.release_reconciled")
        for obj in sorted(all_active_objs | set(rplan.blocked_objects)):
            authority.register_object_blocked(obj, reason="restore.release_reconciled")
    except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
        raise _fail(
            "durability_unavailable",
            f"同步本机权威阻断根失败：{exc}",
            cause=exc,
            **batch_ctx,
        ) from exc

    failpoint("FP-RELEASE-RECONCILE", path=final_db)


def _make_release_result(
    batch_id: str,
    released_ev: RestoreEventV1 | RestoreEventV2,
    rel_payload: ReleasedV1,
) -> RestoreReleaseResultV2:
    uv = rel_payload.post_release_db_user_version
    adm_map: dict[int, PostReleaseAdmissionStatusV2] = {
        0: "admitted_v0",
        1: "requires_explicit_migrate_v1",
        2: "admitted_v2_released_proof",
    }
    root_str = rel_payload.final_root.path.text
    next_cmds: tuple[tuple[str, ...], ...] = (
        (
            ("tianshu", "data", "backup", "--data-root", root_str, "--dest", "<BACKUP_DIR>"),
            ("tianshu", "data", "migrate", "--data-root", root_str, "--backup", "<BACKUP_DIR>"),
        )
        if uv == 1
        else ()
    )
    return RestoreReleaseResultV2(
        batch_id=batch_id,
        phase="released",
        held=False,
        data_root=rel_payload.final_root.path,
        binding_sha256=rel_payload.binding_sha256,
        review_sha256=rel_payload.review_sha256,
        reconcile_plan_sha256=rel_payload.reconcile_plan_sha256,
        event_sha256=domain_digest("event", released_ev),
        db_user_version=uv,
        migration_required=rel_payload.migration_required,
        post_release_admission=adm_map[uv],
        reconciled_counts=rel_payload.reconciled_counts,
        next_commands=next_cmds,
    )


def release(
    data_root: Path,
    *,
    batch_id: str | None = None,
    expect_review_sha256: str,
    acknowledge_unproven_later_history: bool,
    registry: RestoreRegistry | None = None,
) -> RestoreReleaseResultV2:
    """阶段 R0–R3：解除 `published_hold` 持久隔离、执行确定性 append-only 对账并移交准入。"""
    if acknowledge_unproven_later_history is not True:
        raise _fail(
            "incompatible",
            "解除恢复隔离必须显式传入 --acknowledge-unproven-later-history "
            "（确认已知晓备份点之后的遗忘/擦除/删除历史不可证明完整）",
        )
    if (
        type(expect_review_sha256) is not str
        or len(expect_review_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expect_review_sha256)
    ):
        raise _fail(
            "incompatible",
            f"--expect-review 必须是 64 位小写十六进制 SHA256：{expect_review_sha256!r}",
        )

    reg = _registry_or_default(registry)
    obs, resolved_bid, b_events = _resolve_batch_observation(
        reg,
        data_root,
        batch_id,
        allow_aborted=False,
        allow_released=True,
    )
    reserved: ReservedV1 = b_events[0].payload
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    parent_path = final_root.parent
    final_db = final_root / "events.db"
    plan_sha = reserved.plan_sha256
    tail = b_events[-1]

    if tail.kind not in ("published_hold", "release_intent", "released"):
        raise _fail(
            "held" if tail.kind != "aborted" else "incompatible",
            f"restore-release 只接受 published_hold / release_intent / released 批次"
            f"（当前为 {tail.kind}）",
            batch_id=resolved_bid,
            phase=tail.kind,
            plan_sha256=plan_sha,
            next_commands=_next_commands_for_phase(str(final_root), tail.kind),
        )

    obs, op_access, has_db_entity_lock = _acquire_batch_operation_exclusive(reg, obs, resolved_bid)
    codec = NativePathCodecV1()
    p_handle = None
    s_handle = None
    d_handle = None
    try:
        b_events = [e for e in obs.events if e.batch_id == resolved_bid]
        history = {e.kind: e for e in b_events}
        tail = b_events[-1]
        owned: StageOwnedV1 = history["stage_owned"].payload
        sealed: SealedHoldV1 = history["sealed_hold"].payload
        intent: PublishIntentV1 = history["publish_intent"].payload
        pub_ev = history["published_hold"]
        pub_payload: PublishedHoldV1 = pub_ev.payload
        expected_origin_bytes = base64.b64decode(intent.origin_bytes_b64)

        def _rctx(phase_name: PhaseV2, rev_sha: str | None = None) -> dict[str, Any]:
            return {
                "batch_id": resolved_bid,
                "phase": phase_name,
                "plan_sha256": plan_sha,
                "retained_paths": (placement.canonical_root,) if final_root.exists() else (),
                "next_commands": _next_commands_for_phase(
                    str(final_root), phase_name, review_sha256=rev_sha
                ),
            }

        cur_ctx = _rctx(tail.kind, expect_review_sha256)

        from .migrate import writers_active

        if writers_active(final_root) is not False:
            raise _fail(
                "maintenance_busy",
                f"数据根存在活跃或状态不可判定的 daemon/host 写者：{final_root}",
                **cur_ctx,
            )

        _verify_placement_ancestors(placement, batch_ctx=cur_ctx)
        try:
            p_handle = open_entity(parent_path, "directory")
            s_handle = open_entity(final_root, "directory")
            d_handle = open_entity(final_db, "file")
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt",
                f"恢复数据根或主库实体缺失/被替换：{exc}",
                cause=exc,
                **cur_ctx,
            ) from exc

        if p_handle.identity != owned.parent.identity:
            raise _fail("binding_mismatch", "稳定父目录 P 身份已变更", **cur_ctx)
        if (
            s_handle.identity != pub_payload.final_root.identity
            or d_handle.identity != pub_payload.final_database.identity
            or d_handle.link_count != 1
        ):
            raise _fail("binding_mismatch", "恢复根 S 或主库 D 实体身份已被替换", **cur_ctx)
        if not has_db_entity_lock:
            try:
                d_lock = acquire_entity_access(d_handle.identity, exclusive=True)
            except MaintenanceBusy as exc:
                raise _fail("maintenance_busy", str(exc), cause=exc, **cur_ctx) from exc
            op_access._add_child(d_lock)
            has_db_entity_lock = True

        origin_path = final_root / ".restore-origin.json"
        try:
            ohandle = open_entity(origin_path, "file")
        except NativeEntityError as exc:
            raise _fail(
                "record_corrupt",
                f"缺少 .restore-origin.json：{origin_path}",
                cause=exc,
                **cur_ctx,
            ) from exc
        try:
            if ohandle.link_count != 1:
                raise _fail(
                    "record_corrupt",
                    ".restore-origin.json 必须是单硬链接普通文件",
                    **cur_ctx,
                )
            raw_origin = origin_path.read_bytes()
        finally:
            ohandle.close()
        if raw_origin != expected_origin_bytes:
            raise _fail(
                "record_corrupt",
                ".restore-origin.json 字节与已发布事实不符",
                **cur_ctx,
            )

        # 幂等重入：若批次已处于 released，核验 review_sha256 一致后直接返回已提交结果。
        if tail.kind == "released":
            rel_done: ReleasedV1 = tail.payload
            if rel_done.review_sha256 != expect_review_sha256:
                raise _fail(
                    "binding_mismatch",
                    f"批次 {resolved_bid} 已解除隔离，传入 --expect-review 与绑定摘要不符",
                    **cur_ctx,
                )
            return _make_release_result(resolved_bid, tail, rel_done)

        # ---------------- R0b + R1: published_hold -> release_intent ----------------
        if "release_intent" not in history:
            ctx_ph = _rctx("published_hold")
            _verify_material_tree_matches_binding(
                final_root,
                sealed.binding,
                allow_token=True,
                require_origin_bytes=expected_origin_bytes,
                batch_ctx=ctx_ph,
            )
            rev = _compute_review_locked(
                resolved_bid,
                reserved,
                sealed,
                expected_origin_bytes,
                later_backup=None,
                normalized_scopes=(),
                seen_scopes=set(),
            )
            if not rev.review_complete:
                raise _fail(
                    "incompatible",
                    "只读复核未完整通过，拒绝解除隔离："
                    + ("；".join(rev.unverifiable) or "存在失败检查项"),
                    **ctx_ph,
                )
            if rev.review_sha256 != expect_review_sha256:
                raise _fail(
                    "binding_mismatch",
                    f"只读复核摘要不匹配：当前现场为 {rev.review_sha256}，"
                    f"传入 --expect-review 为 {expect_review_sha256}",
                    **ctx_ph,
                )

            pre_uv, pre_seq_head, reconcile_plan = _plan_release_reconciliation(
                final_root,
                sealed.binding,
                batch_ctx=ctx_ph,
            )
            reconcile_sha = domain_digest("reconcile", reconcile_plan)
            est_reconcile_bytes = (
                4096
                + 512
                * (
                    len(reconcile_plan.fenced_run_ids)
                    + len(reconcile_plan.expired_approval_ids)
                    + len(reconcile_plan.unresolved_intents)
                    + len(reconcile_plan.cancelled_job_ids)
                    + len(reconcile_plan.blocked_workspaces)
                    + len(reconcile_plan.blocked_objects)
                )
            )
            _probe_capacity(
                final_root,
                est_reconcile_bytes,
                metadata_only=False,
                codec=codec,
                batch_ctx=ctx_ph,
            )

            rel_intent_payload = ReleaseIntentV1(
                published_hold_sha256=domain_digest("event", pub_ev),
                binding_sha256=pub_payload.binding_sha256,
                review_sha256=rev.review_sha256,
                acknowledge_unproven_later_history=True,
                final_root=pub_payload.final_root,
                final_database=pub_payload.final_database,
                stable_parent=owned.parent,
                origin_sha256=pub_payload.origin_sha256,
                pre_release_db_user_version=pre_uv,
                pre_release_event_seq_head=pre_seq_head,
                reconcile_plan=reconcile_plan,
                reconcile_plan_sha256=reconcile_sha,
            )
            failpoint("FP-RELEASE-INTENT-BEFORE", path=final_root)
            obs, rel_intent_ev = _append_registry_cas(
                reg,
                obs,
                lambda cur: RestoreEventV2(
                    2,
                    cur.meta.registry_id,
                    cur.next_sequence,
                    resolved_bid,
                    cur.head_sha256,
                    "release_intent",
                    rel_intent_payload,
                ),
                op_access,
                codec=codec,
                batch_ctx=ctx_ph,
            )
            history["release_intent"] = rel_intent_ev
            failpoint("FP-RELEASE-INTENT", path=final_root)

        # ---------------- R2 + R3: release_intent -> reconcile -> released ----------------
        rel_intent_ev = history["release_intent"]
        rel_intent: ReleaseIntentV1 = rel_intent_ev.payload
        ctx_ri = _rctx("release_intent", rel_intent.review_sha256)
        if rel_intent.review_sha256 != expect_review_sha256:
            raise _fail(
                "binding_mismatch",
                f"release_intent 续做传入的 --expect-review "
                f"与已提交意图 ({rel_intent.review_sha256}) 不符",
                **ctx_ri,
            )

        _execute_release_reconciliation(
            final_root,
            d_handle,
            sealed,
            rel_intent,
            batch_id=resolved_bid,
            batch_ctx=ctx_ri,
        )

        # R3 终态重核（.restore-origin.json 逐字节不变，S/D/P 实体不变）
        p_handle.verify_path(parent_path)
        s_handle.verify_path(final_root)
        d_handle.verify_path(final_db)
        if origin_path.read_bytes() != expected_origin_bytes:
            raise _fail("record_corrupt", "R3 核验发现 .restore-origin.json 被改动", **ctx_ri)

        ro_conn = _open_static_readonly_sqlite(final_db, batch_ctx=ctx_ri)
        try:
            post_uv = int(ro_conn.execute("PRAGMA user_version").fetchone()[0])
            post_seq_head = int(
                ro_conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            )
        finally:
            ro_conn.close()
        if post_uv != rel_intent.pre_release_db_user_version:
            raise _fail("record_corrupt", "对账收口后主库 user_version 发生意外变化", **ctx_ri)

        post_db_bytes = DatabaseBytesV1(final_db.stat().st_size, sha256_file(final_db))
        rplan = rel_intent.reconcile_plan
        counts = ReconciledCountsV1(
            fenced_runs=len(rplan.fenced_run_ids),
            expired_approvals=len(rplan.expired_approval_ids),
            cleared_leases=len(rplan.cleared_lease_keys),
            resolved_none_applied_intents=sum(
                1 for d in rplan.unresolved_intents if d.disposition == "none_applied"
            ),
            preserved_unknown_intents=sum(
                1 for d in rplan.unresolved_intents if d.disposition == "unknown"
            ),
            cancelled_jobs=len(rplan.cancelled_job_ids),
        )
        released_payload = ReleasedV1(
            release_intent_sha256=domain_digest("event", rel_intent_ev),
            binding_sha256=rel_intent.binding_sha256,
            review_sha256=rel_intent.review_sha256,
            reconcile_plan_sha256=rel_intent.reconcile_plan_sha256,
            final_root=rel_intent.final_root,
            final_database=rel_intent.final_database,
            origin_sha256=rel_intent.origin_sha256,
            post_release_database_bytes=post_db_bytes,
            post_release_db_user_version=post_uv,
            post_release_event_seq_head=post_seq_head,
            reconciled_counts=counts,
            migration_required=(post_uv == 1),
        )
        failpoint("FP-RELEASED-BEFORE", path=final_root)
        obs, released_ev = _append_registry_cas(
            reg,
            obs,
            lambda cur: RestoreEventV2(
                2,
                cur.meta.registry_id,
                cur.next_sequence,
                resolved_bid,
                cur.head_sha256,
                "released",
                released_payload,
            ),
            op_access,
            codec=codec,
            batch_ctx=ctx_ri,
        )
        failpoint("FP-RELEASED", path=final_root)
        return _make_release_result(resolved_bid, released_ev, released_payload)
    finally:
        if d_handle is not None:
            d_handle.close()
        if s_handle is not None:
            s_handle.close()
        if p_handle is not None:
            p_handle.close()
        op_access.close()


__all__ = [
    "ORIGIN_LAYOUT",
    "TOKEN_LAYOUT",
    "RestoreOperationError",
    "abort",
    "apply",
    "plan",
    "release",
    "resume",
    "review",
]
