"""一致点备份 / 校验 / 干净根恢复 / 保留期与引用保护（PRD §8.5）。

## 备份 ≠ 快照 ≠ 撤销

- **写前快照**（`snapshot.SnapshotStore`）：为**一次 Run 的撤销**保存被覆盖文件
  的写前内容，落在 `<data_root>/artifacts/` 里，只覆盖受控写入。
- **备份**（本模块）：为**整份数据根**留一个可校验、可恢复到干净根的一致副本，
  含事件库、投影（投影与事件同库）、快照清单、blob、撤销意图日志。
- **撤销**：按快照证据把**工作区文件**改回写前状态。工作区文件**不在**备份里
  ——备份只承诺"证据还在"，不承诺"用户文件还能被还原"，除非工作区本身另有备份。
  恢复后能不能撤销，取决于恢复出来的快照清单/blob 是否完整（`verify_backup`
  会逐条核对引用）。

## 一致性怎么来

事件库用 SQLite **在线备份 API**（`sqlite3.Connection.backup()`）取一致点，
不是复制正在写入的 `events.db`。在线备份期间写者不会被打断，拿到的是单个
事务边界上的一致快照。材料（blob / 快照清单 / 撤销日志）都是内容寻址或原子
重写的文件：blob 不可变，清单/日志用 `os.replace` 整文件替换——所以按文件
逐个复制的读到的是"替换前"或"替换后"的完整文件，不存在半截。

备份过程是"临时目录 → 逐文件 fsync → 清单 → 去掉 `.partial` 标记 → 原子改名"。
中断留下的临时目录带 `.partial` 标记，`verify_backup` 会**拒绝**它，不会被当成
有效备份。

## 不放进备份的东西（有意为之）

- `<data_root>/run/`：daemon/host 锁、IPC socket、**`ipc.key` 认证密钥**——
  密钥不进备份。
- `<data_root>/logs/`：诊断日志（保留期见 `RETENTION_POLICY`）。
- `events.db-wal` / `events.db-shm`：在线备份已经给出单文件一致点。
- 工作区里的用户文件：备份不是"整机备份"，也不该混淆两种语义。

## 版本

`backup.json` 有独立的 `format_version`（本模块契约）与 `db_user_version`
（事件库结构版本，由 `migrate.py` 管理）。恢复来自更新格式/更新结构版本的备份
时**拒绝**并给补救路径，不按猜的语义解释。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from . import __version__
from .crashpoint import failpoint
from .fsutil import atomic_write_bytes

MANIFEST_NAME = "backup.json"
FORMAT_NAME = "tianshu-backup"
FORMAT_VERSION = 1
#: 构建中的临时目录名片段；verify 看到它/它的标记就拒绝。
PARTIAL_MARKER = ".partial"
_PARTIAL_TAG = ".partial-"

#: 备份排除的数据根顶层条目（相对 data_root）。
EXCLUDED_TOP_LEVEL = {"run", "logs"}
EXCLUDED_FILE_PREFIXES = ("events.db-wal", "events.db-shm", "metrics.jsonl")

#: 保留期声明（§8.5）。**声明不是自动 GC**：清理必须人工触发，且被引用的
#: 材料（快照 manifest / 事件 payload_ref / 未完成撤销 journal）永远不可清理。
RETENTION_POLICY: dict[str, dict[str, Any]] = {
    "event_log": {
        "retention": "永久。事件日志是唯一事实源，任何自动清理都不得删除。",
        "budget_bytes": None,
        "owner": "用户显式操作；本模块不提供删除路径。",
        "protected_by": "append-only 触发器（events_no_update / events_no_delete）",
    },
    "snapshot_manifests": {
        "retention": "永久（未撤销/未完成的 Run 需要它做恢复与续做）。",
        "budget_bytes": None,
        "owner": "用户显式操作。被条目引用的 manifest 不可单独删除。",
        "protected_by": "事件库中的 run_id 与未决 intent/result",
    },
    "snapshot_blobs": {
        "retention": "被 manifest / 事件 payload_ref / 未完成撤销 journal 引用即永久保护。",
        "budget_bytes": 2 * 1024**3,
        "owner": "人工清理；本模块只给候选清单（cleanup_plan），不执行删除。",
        "protected_by": "collect_references() 的引用索引",
    },
    "undo_journals": {
        "retention": "status=in_progress 的必须保留到续做完成；completed 可归档。",
        "budget_bytes": 64 * 1024**2,
        "owner": "人工清理。",
        "protected_by": "undo_journal.outstanding() 的未完成集合",
    },
    "diagnostic_trace": {
        "retention": "默认 30 天（logs/daemon.log）；只用于排障，不是审计事实源。",
        "budget_bytes": 50 * 1024**2,
        "owner": "人工清理；不参与备份。",
        "protected_by": "无（可随时删）",
    },
    "indexes": {
        "retention": "可重建，不是备份对象。",
        "budget_bytes": 0,
        "owner": "重建代替清理。",
        "protected_by": "无",
    },
}

#: 备份格式的恢复须知（写进 backup.json，恢复时也回显给人看）。
_RESTORE_NOTES = [
    "只能恢复到干净根（不存在或空目录）；非空目标一律拒绝，不会覆盖现有数据。",
    "只能恢复到备份记录的原数据根路径：事件 payload_ref 与快照 blob_ref 是绝对"
    "路径，换路径会让引用指向旧位置（换路径重定位未实现）。",
    "备份不含工作区里的用户文件：恢复的是事件/投影/快照证据，不是用户目录。",
    "备份不含 run/（IPC socket、锁、ipc.key）：daemon 首次启动会重建。",
    "若备份的 db_user_version 小于当前程序支持版本，恢复后需要运行迁移（migrate）。",
    "来自更新程序的备份（格式或结构版本更高）会被拒绝：请用匹配版本恢复后再迁移。",
]


class BackupError(Exception):
    """备份模块的域错误基类。"""


class BackupRefused(BackupError):
    """前置条件不满足（目标已存在、目录非空、数据根不可用）。"""


class BackupInvalid(BackupError):
    """校验失败：备份不完整/被改过/引用丢失/版本不兼容。"""

    def __init__(self, problems: list[str], remediation: list[str] | None = None) -> None:
        self.problems = list(problems)
        self.remediation = list(remediation or [])
        joined = "；".join(self.problems) or "未知原因"
        message = f"备份校验失败：{joined}"
        if self.remediation:
            message += "。处置建议：" + "；".join(self.remediation)
        super().__init__(message)


class RestoreRefused(BackupError):
    """恢复前置条件不满足（目标路径与备份不一致、目标不是干净根）。"""


class RestoreFailed(BackupError):
    """恢复中途失败；原数据保留（目标根没有被原子替换）。"""


class ReferenceUnknown(BackupError):
    """引用关系读不出来（清单/日志/事件库损坏）：拒绝给出清理结论。"""


@dataclass(frozen=True)
class BackupFile:
    path: str  # data_root 相对、`/` 分隔
    sha256: str
    size: int
    category: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> BackupFile:
        if not isinstance(raw, dict):
            raise ValueError("files 条目必须是对象")
        path = raw.get("path")
        sha = raw.get("sha256")
        size = raw.get("size")
        category = raw.get("category")
        if not isinstance(path, str) or not path:
            raise ValueError("files.path 必须是非空字符串")
        if not _is_digest(sha):
            raise ValueError(f"files.sha256 不是 64 位十六进制：{sha!r}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"files.size 必须是非负整数：{size!r}")
        if not isinstance(category, str) or not category:
            raise ValueError("files.category 必须是非空字符串")
        return cls(path=path, sha256=sha, size=size, category=category)


@dataclass
class BackupManifest:
    format: str
    format_version: int
    app_version: str
    db_user_version: int
    created_at_ms: int
    source_name: str
    #: 源数据根的绝对路径（resolve 后）。**必须记录**：事件 `payload_ref` 与
    #: 快照清单里的 `blob_ref` 都是绝对路径，恢复只能回到同一个数据根，否则
    #: 引用会指向旧位置（读旧数据或读不到）。换路径重定位不在本轮范围。
    source_path: str
    files: list[BackupFile]
    event_count: int
    event_seq_head: int
    run_ids: list[str]
    references: dict[str, Any]
    writer: dict[str, Any] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    retention: dict[str, Any] = field(default_factory=dict)
    restore_notes: list[str] = field(default_factory=list)
    #: 仅创建时核对与 source_path 同根的词法别名；保存后的读取绝不回源解析。
    source_aliases: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "format_version": self.format_version,
            "app_version": self.app_version,
            "db_user_version": self.db_user_version,
            "created_at_ms": self.created_at_ms,
            "source_name": self.source_name,
            "source_path": self.source_path,
            "source_aliases": list(self.source_aliases),
            "files": [f.to_dict() for f in self.files],
            "event_count": self.event_count,
            "event_seq_head": self.event_seq_head,
            "run_ids": list(self.run_ids),
            "references": self.references,
            "writer": self.writer,
            "excluded": list(self.excluded),
            "retention": self.retention,
            "restore_notes": list(self.restore_notes),
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Any) -> BackupManifest:
        if not isinstance(raw, dict):
            raise ValueError("backup.json 必须是 JSON 对象")
        files_raw = raw.get("files")
        if not isinstance(files_raw, list) or not files_raw:
            raise ValueError("backup.json 缺少 files 列表")
        run_ids = raw.get("run_ids")
        if not isinstance(run_ids, list) or not all(
            isinstance(r, str) for r in run_ids
        ):
            raise ValueError("backup.json 的 run_ids 必须是字符串列表")
        references = raw.get("references")
        if not isinstance(references, dict):
            raise ValueError("backup.json 的 references 必须是对象")
        source_path = raw.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            raise ValueError("backup.json 缺少 source_path（恢复必须回到原数据根）")
        root = _source_root(source_path)
        source_aliases = raw.get("source_aliases", [])
        if not isinstance(source_aliases, list):
            raise ValueError("backup.json 的 source_aliases 必须是绝对路径列表")
        for alias in source_aliases:
            if type(_source_root(alias)) is not type(root):
                raise ValueError("source_aliases 必须与 source_path 使用同一种平台路径")
        return cls(
            format=str(raw.get("format", "")),
            format_version=int(raw.get("format_version", 0)),
            app_version=str(raw.get("app_version", "")),
            db_user_version=int(raw.get("db_user_version", -1)),
            created_at_ms=int(raw.get("created_at_ms", 0)),
            source_name=str(raw.get("source_name", "")),
            source_path=source_path,
            files=[BackupFile.from_dict(item) for item in files_raw],
            event_count=int(raw.get("event_count", -1)),
            event_seq_head=int(raw.get("event_seq_head", -1)),
            run_ids=run_ids,
            references=references,
            writer=raw.get("writer") if isinstance(raw.get("writer"), dict) else {},
            excluded=raw.get("excluded")
            if isinstance(raw.get("excluded"), list) else [],
            retention=raw.get("retention")
            if isinstance(raw.get("retention"), dict) else {},
            restore_notes=raw.get("restore_notes")
            if isinstance(raw.get("restore_notes"), list) else [],
            source_aliases=source_aliases,
        )


@dataclass
class BackupVerification:
    ok: bool
    manifest: BackupManifest | None
    problems: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    files_checked: int = 0
    bytes_checked: int = 0
    db_user_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest": self.manifest.to_dict() if self.manifest is not None else None,
            "problems": list(self.problems),
            "remediation": list(self.remediation),
            "files_checked": self.files_checked,
            "bytes_checked": self.bytes_checked,
            "db_user_version": self.db_user_version,
        }

    def require_ok(self) -> BackupManifest:
        if not self.ok or self.manifest is None:
            raise BackupInvalid(self.problems, self.remediation)
        return self.manifest


@dataclass
class RestoreReport:
    ok: bool
    data_root: Path
    manifest: BackupManifest
    files_restored: int
    bytes_restored: int
    reconciliation: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data_root": str(self.data_root),
            "backup": {
                "format_version": self.manifest.format_version,
                "app_version": self.manifest.app_version,
                "db_user_version": self.manifest.db_user_version,
                "created_at_ms": self.manifest.created_at_ms,
                "event_count": self.manifest.event_count,
                "event_seq_head": self.manifest.event_seq_head,
                "total_bytes": self.manifest.total_bytes,
            },
            "files_restored": self.files_restored,
            "bytes_restored": self.bytes_restored,
            "reconciliation": self.reconciliation,
            "warnings": list(self.warnings),
        }

    def to_text(self) -> str:
        rec = self.reconciliation
        lines = [
            f"已恢复备份到：{self.data_root}",
            f"  备份格式 v{self.manifest.format_version}，应用 {self.manifest.app_version}，"
            f"库结构版本 {self.manifest.db_user_version}",
            f"  恢复文件 {self.files_restored} 个 / {self.bytes_restored} 字节；"
            f"事件 {self.manifest.event_count} 条（seq_head={self.manifest.event_seq_head}）",
            "恢复后核对：",
            f"  未决 intent（有 intent 无 result）：{len(rec['pending_intents'])}",
            f"  待批 / 已批未消费审批：{len(rec['pending_approvals'])}",
            f"  未终态 Run：{len(rec['non_terminal_runs'])}",
            f"  仍有效的租约：{len(rec['live_leases'])}",
        ]
        for item in rec["pending_intents"]:
            lines.append(
                f"    ! intent run={item['run_id']} call={item['call_id']} "
                f"tool={item['tool_id']}"
            )
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 工具


def _sha256_stream(fh) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = fh.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def sha256_file(path: Path) -> str:
    with path.open("rb") as fh:
        return _sha256_stream(fh)[0]


def _fsync_file(path: Path) -> None:
    """fsync 单个文件。

    Windows 的 `os.fsync`（`_commit`）对**只读**句柄会报 `EBADF`：
    `os.open(path, os.O_RDONLY)` 打开的 fd 不是可写句柄。这里在 Windows 上
    改用可写句柄（备份目录里的文件都是本进程刚写出的普通文件，可写）；
    连可写句柄都打不开时才放弃，并在该边界如实降级——备份仍可被哈希/引用
    校验，但「掉电后目录项已落盘」在那种情况下不能承诺（见
    docs/backup-restore.md §9）。

    POSIX 保持只读 + fsync，且 fsync 失败**不吞**：那是真的没有落盘证据。
    """
    if os.name == "nt":
        try:
            fd = os.open(str(path), os.O_RDWR)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            # Windows 上 `_commit` 对某些文件系统/句柄仍可能拒绝；备份仍由
            # 哈希与引用清单校验，不把「fsync 失败」伪装成「已落盘」。
            pass
        finally:
            os.close(fd)
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """fsync 目录项（Windows 不支持目录 open，静默跳过）。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    for base, dirs, files in os.walk(root):
        for name in files:
            _fsync_file(Path(base) / name)
        for name in dirs:
            _fsync_dir(Path(base) / name)


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _validate_rel(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("相对路径必须是非空字符串")
    if "\\" in raw or "\x00" in raw:
        raise ValueError(f"相对路径必须用 / 分隔：{raw!r}")
    if Path(raw).is_absolute() or PureWindowsPath(raw).drive:
        raise ValueError(f"不允许绝对路径或盘符：{raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"相对路径不允许空段 / . / ..：{raw!r}")
    return raw


def _is_digest(raw: Any) -> bool:
    return isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{64}", raw) is not None


def _checked_file(root: Path, rel: str) -> Path:
    """只在指定根里寻址；先拒绝任一层符号链接，再检查文件内容。"""
    _validate_rel(rel)
    if root.is_symlink():
        raise ValueError(f"不允许符号链接材料根：{root}")
    path = root
    for part in rel.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError(f"不允许符号链接材料：{rel}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"材料路径不在备份根内：{rel}")
    return path


def _source_root(raw: Any) -> PurePosixPath | PureWindowsPath:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("源数据根必须是非空绝对路径")
    root = PureWindowsPath(raw) if PureWindowsPath(raw).drive else PurePosixPath(raw)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError(f"源数据根必须是无 .. 的绝对路径：{raw!r}")
    return root


def _reference_rel(
    raw: Any, source_root: Path, *, source_aliases: Sequence[str] = (),
) -> str:
    """把历史绝对引用映射为备份根内路径，不读取源材料的内容。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError("引用必须是非空路径")
    # 使用纯路径对象：验证备份绝不因源目录被移走或替换成链接而访问活源。
    root = _source_root(str(source_root))
    path_type = type(root)
    roots = [root, *(_source_root(alias) for alias in source_aliases)]
    if any(type(candidate) is not path_type for candidate in roots):
        raise ValueError("源数据根与别名的平台路径不一致")
    path = path_type(raw)
    if path.is_absolute():
        for candidate in roots:
            try:
                rel = path.relative_to(candidate)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"引用不在记录的数据根或已验证别名内：{raw!r}")
    else:
        if path.drive or path.root:
            raise ValueError(f"引用不允许不完整的绝对路径：{raw!r}")
        rel = path
        parts = rel.parts
        # CLI 可以把相对于启动目录的 .tianshu/blobs/... 写进事件。
        # 只接纳与已记录根路径尾部一致的前缀，不把任意外部路径洗成根内。
        for offset in range(len(parts) - 1, 0, -1):
            if any(tuple(candidate.parts[-offset:]) == tuple(parts[:offset])
                   for candidate in roots):
                rel = path_type(*parts[offset:])
                break
    result = _validate_rel(rel.as_posix())
    if result.split("/", 1)[0] not in {"artifacts", "blobs", "memory_blobs"}:
        raise ValueError(f"不支持的数据材料布局：{raw!r}")
    return result


def _reference_rel_live(
    raw: Any, source_root: Path, *, source_aliases: list[str],
) -> str:
    """只在活源阶段发现别名；同根身份及完整材料后缀必须同时吻合。"""
    try:
        return _reference_rel(raw, source_root, source_aliases=source_aliases)
    except ValueError:
        pass
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError(f"无法核对源根别名：{raw!r}")
    path = Path(raw)
    resolved = path.resolve(strict=True)
    rel = Path(_reference_rel(str(resolved), source_root))
    # 只允许根本身的别名；材料路径内部的链接不能被洗成根别名。
    if Path(*path.parts[-len(rel.parts):]) != rel:
        raise ValueError(f"源根别名的材料后缀不一致：{raw!r}")
    alias = path.parents[len(rel.parts) - 1]
    _source_root(str(alias))
    if alias.resolve(strict=True) != source_root:
        raise ValueError(f"路径不是同一数据根的别名：{raw!r}")
    if str(alias) not in source_aliases:
        source_aliases.append(str(alias))
    return _reference_rel(raw, source_root, source_aliases=source_aliases)


def read_saved_event_payload(
    backup_dir: Path,
    row: sqlite3.Row | Mapping[str, Any],
    *,
    source_root: Path,
    source_aliases: Sequence[str] = (),
) -> Any:
    """只读保存的事件 payload；外部引用仅按原根词法映射至备份内。

    调用方查询时将 ``payload`` 列命名为 ``payload_inline``。哈希和 JSON
    核验失败即拒绝；不得把损坏事件降级成空对象或读取原数据根。
    """
    try:
        inline = row["payload_inline"]
        ref = row["payload_ref"]
        expected = row["payload_hash"]
        if (inline is None) == (ref is None):
            raise ValueError("payload_inline 与 payload_ref 必须且只能提供一个")
        if not _is_digest(expected):
            raise ValueError("payload_hash 不是 sha256")
        if ref is not None:
            rel = _reference_rel(ref, source_root, source_aliases=source_aliases)
            raw = _checked_file(Path(backup_dir), rel).read_bytes()
        elif isinstance(inline, str):
            raw = inline.encode("utf-8")
        else:
            raise ValueError("payload_inline 必须是字符串")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("payload_hash 不符")
        return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, KeyError, IndexError, TypeError, UnicodeError) as exc:
        raise BackupInvalid([f"保存的事件 payload 无法核验：{exc}"]) from exc


def _category_for(rel: str) -> str:
    if rel == "events.db":
        return "event_log"
    if rel.startswith("artifacts/snapshots/"):
        return "snapshot_manifest"
    if rel.startswith("artifacts/blobs/"):
        return "blob"
    if rel.startswith("artifacts/undo/"):
        return "undo_journal"
    return "artifact_other"


def _is_excluded(rel: str) -> bool:
    first = rel.split("/", 1)[0]
    if first in EXCLUDED_TOP_LEVEL:
        return True
    if "/" not in rel and rel.startswith(EXCLUDED_FILE_PREFIXES):
        return True
    return first.startswith(".") and rel not in ("",)


def _iter_backup_inputs(data_root: Path) -> list[tuple[Path, str]]:
    """标准布局的材料；不能把链接指向的根外文件带进备份。"""
    entries: list[tuple[Path, str]] = []
    db = data_root / "events.db"
    if not db.is_file():
        raise BackupRefused(
            f"数据根里没有事件库：{data_root}。这不像一个天枢数据根，拒绝生成空备份。"
        )
    for name in ("artifacts", "blobs", "memory_blobs"):
        material_dir = data_root / name
        if material_dir.is_symlink():
            raise BackupRefused(f"不允许符号链接材料目录：{material_dir}")
        if not material_dir.is_dir():
            continue
        for path in sorted(material_dir.rglob("*")):
            if path.is_symlink():
                raise BackupRefused(f"不允许符号链接材料：{path}")
            if not path.is_file():
                continue
            rel = _rel_posix(path, data_root)
            if path.name.startswith("."):
                continue
            if path.name.endswith(".tmp"):
                continue
            entries.append((path, rel))
    return entries


def _copy_file_verified(src: Path, dst: Path) -> tuple[str, int]:
    """流式复制并 fsync；返回 (sha256, size)。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            fout.write(chunk)
            total += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    if os.name != "nt":
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
    return digest.hexdigest(), total


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """严格只读；失败不得降级成会创建数据库的普通连接。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _connect_saved_readonly(db_path: Path) -> sqlite3.Connection:
    """只读自包含静态快照，不创建 WAL/SHM；绝不用于仍有写者的活源。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _db_snapshot(src_db: Path, dst_db: Path) -> None:
    """SQLite 在线备份 API：取一致点，不复制正在写入的文件。"""
    src = _connect_readonly(src_db)
    try:
        src.row_factory = sqlite3.Row
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    for suffix in ("-wal", "-shm"):
        leftover = Path(str(dst_db) + suffix)
        if leftover.exists():
            leftover.unlink()


def _db_meta(db_path: Path, *, saved: bool = False) -> dict[str, Any]:
    conn = _connect_saved_readonly(db_path) if saved else _connect_readonly(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(seq),0) AS head,"
                           " COALESCE(MIN(seq),0) AS first_seq FROM events").fetchone()
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        runs = [r[0] for r in conn.execute(
            "SELECT DISTINCT run_id FROM events ORDER BY run_id")]
    finally:
        conn.close()
    count, head, first_seq = int(row[0]), int(row[1]), int(row[2])
    contiguous = (count == 0 and head == 0) or (
        count > 0 and first_seq == 1 and count == head
    )
    return {
        "event_count": count,
        "event_seq_head": head,
        "first_seq": first_seq,
        "seq_contiguous": contiguous,
        "db_user_version": int(user_version),
        "run_ids": runs,
    }


def _db_references(
    db_path: Path, data_root: Path, *, saved: bool = False,
    source_aliases: Sequence[str] = (), discover_aliases: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """外部 payload blob 引用。返回 (引用列表, 读不出来的问题)。"""
    refs: list[dict[str, Any]] = []
    problems: list[str] = []
    conn = _connect_saved_readonly(db_path) if saved else _connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, payload_ref, payload_hash FROM events"
            " WHERE payload_ref IS NOT NULL ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    for seq, ref, payload_hash in rows:
        try:
            if not _is_digest(payload_hash):
                raise ValueError(f"payload_hash 不是 sha256：{payload_hash!r}")
            rel = (
                _reference_rel_live(ref, data_root, source_aliases=discover_aliases)
                if discover_aliases is not None else
                _reference_rel(ref, data_root, source_aliases=source_aliases)
            )
            refs.append({
                "seq": int(seq),
                "path": rel,
                "sha256": str(payload_hash),
            })
        except (OSError, ValueError) as exc:
            problems.append(f"seq={seq} 的 payload_ref 不在数据根内/无法解析：{ref}（{exc}）")
    return refs, problems


def _snapshot_references(
    data_root: Path,
    *,
    source_root: Path | None = None,
    source_aliases: Sequence[str] = (), discover_aliases: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """快照清单引用的 blob；manifest 本身损坏记为问题，不猜。"""
    refs: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        snapshots = _checked_file(data_root, "artifacts/snapshots")
    except ValueError as exc:
        return refs, [str(exc)]
    if not snapshots.is_dir():
        return refs, problems
    for manifest in sorted(snapshots.glob("*.json")):
        rel = _rel_posix(manifest, data_root)
        try:
            raw = json.loads(_checked_file(data_root, rel).read_bytes().decode("utf-8"))
            items = raw["changes"] if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ValueError("快照 changes 必须是列表")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("快照 changes 条目必须是对象")
                blob_ref = item.get("blob_ref")
                before_state = item.get("before_state")
                if not blob_ref:
                    continue
                if not _is_digest(before_state):
                    raise ValueError(f"before_state 不是 sha256：{before_state!r}")
                origin = source_root or data_root.resolve()
                blob_rel = (
                    _reference_rel_live(blob_ref, origin, source_aliases=discover_aliases)
                    if discover_aliases is not None else
                    _reference_rel(blob_ref, origin, source_aliases=source_aliases)
                )
                refs.append({
                    "manifest": rel,
                    "call_id": str(item.get("call_id", "")),
                    "path": blob_rel,
                    "sha256": str(before_state or ""),
                })
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            problems.append(f"快照清单读不出来：{rel}（{type(exc).__name__}: {exc}）")
    return refs, problems


def _undo_references(data_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """撤销意图日志引用的 blob（before_state 内容寻址）。"""
    refs: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        journals = _checked_file(data_root, "artifacts/undo")
    except ValueError as exc:
        return refs, [str(exc)]
    if not journals.is_dir():
        return refs, problems
    for journal in sorted(journals.glob("*.json")):
        rel = _rel_posix(journal, data_root)
        try:
            raw = json.loads(_checked_file(data_root, rel).read_bytes().decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("撤销日志必须是对象")
            items = raw.get("items", [])
            if not isinstance(items, list):
                raise ValueError("撤销日志 items 必须是列表")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("撤销日志 items 条目必须是对象")
                before = item.get("before_state")
                action = item.get("action")
                if action == "restore" and isinstance(before, str) and before != "absent":
                    if not _is_digest(before):
                        raise ValueError(f"before_state 不是 sha256：{before!r}")
                    refs.append({
                        "journal": rel,
                        "call_id": str(item.get("call_id", "")),
                        "path": f"artifacts/blobs/{before}",
                        "sha256": before,
                    })
            status = raw.get("status")
            if status not in ("in_progress", "completed"):
                problems.append(f"撤销日志状态无法识别：{rel}（status={status!r}）")
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            problems.append(f"撤销日志读不出来：{rel}（{type(exc).__name__}: {exc}）")
    return refs, problems


def _projection_references(
    db_path: Path, *, saved: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """从已保存数据库读出内容与记忆引用，而非相信 manifest 的可选字段。"""
    contents: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    problems: list[str] = []
    conn = _connect_saved_readonly(db_path) if saved else _connect_readonly(db_path)
    try:
        has_contents = _has_table(conn, "content_objects")
        has_grants = _has_table(conn, "content_grants")
        if has_contents != has_grants:
            problems.append("内容投影不完整：content_objects 与 content_grants 必须同时存在")
        if has_contents:
            for digest, size in conn.execute(
                "SELECT digest, size FROM content_objects ORDER BY digest"
            ):
                if not _is_digest(digest) or not isinstance(size, int) or size < 0:
                    problems.append(f"content_objects 引用非法：digest={digest!r}, size={size!r}")
                    continue
                contents.append({"path": f"artifacts/blobs/{digest}",
                                 "sha256": digest, "size": size, "content_digest": digest})
        if has_contents and has_grants:
            for (digest,) in conn.execute(
                "SELECT DISTINCT g.digest FROM content_grants AS g "
                "LEFT JOIN content_objects AS o ON o.digest = g.digest WHERE o.digest IS NULL"
            ):
                problems.append(f"content_grants 引用了未登记的内容：{digest!r}")
        if _has_table(conn, "memories"):
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            erased = "erased_at" if "erased_at" in columns else "NULL"
            for memory_id, digest, erased_at in conn.execute(
                f"SELECT memory_id, content_ref, {erased} FROM memories ORDER BY memory_id"
            ):
                if erased_at is not None:
                    if not isinstance(erased_at, int) or erased_at < 0:
                        problems.append(f"记忆 {memory_id} 的 erased_at 非法：{erased_at!r}")
                    continue
                if not _is_digest(digest):
                    problems.append(f"记忆 {memory_id} 的 content_ref 不是 sha256：{digest!r}")
                    continue
                memories.append({"path": f"memory_blobs/{digest}", "sha256": digest,
                                 "memory_id": str(memory_id)})
    finally:
        conn.close()
    return contents, memories, problems


def _material_references(
    data_root: Path, *, source_root: Path,
    source_aliases: Sequence[str] = (), discover_aliases: list[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    db = _checked_file(data_root, "events.db")
    payload, payload_problems = _db_references(
        db, source_root, saved=True, source_aliases=source_aliases,
        discover_aliases=discover_aliases,
    )
    snapshots, snapshot_problems = _snapshot_references(
        data_root, source_root=source_root, source_aliases=source_aliases,
        discover_aliases=discover_aliases,
    )
    undo, undo_problems = _undo_references(data_root)
    contents, memories, projection_problems = _projection_references(db, saved=True)
    return {
        "event_payload_refs": payload,
        "snapshot_blobs": snapshots,
        "undo_journal_blobs": undo,
        "content_blobs": contents,
        "memory_blobs": memories,
    }, payload_problems + snapshot_problems + undo_problems + projection_problems


def inspect_writers(data_root: Path) -> dict[str, Any]:
    """只读判断"现在有没有写者"，不抢锁、不停止任何进程。

    备份用 SQLite 在线 API，本身能在一写者存在时取一致点；这里只是把事实记进
    清单，给恢复者/排障者一个上下文。读不出来记 `unknown`，不当成"没有写者"。
    """
    data_root = Path(data_root)
    result: dict[str, Any] = {"checked_at_ms": int(time.time() * 1000)}

    try:
        from . import ipc

        result["ipc_probe"] = bool(ipc.probe(data_root))
    except Exception as exc:  # noqa: BLE001 - 探活失败只影响信息字段，不影响备份
        result["ipc_probe"] = None
        result["ipc_probe_error"] = f"{type(exc).__name__}: {exc}"

    for name, rel in (("daemon_lock", "run/daemon.lock"), ("host_lock", "run/host.lock")):
        path = data_root / rel
        if not path.exists():
            result[name] = {"present": False}
            continue
        try:
            payload = json.loads(path.read_bytes().decode("utf-8"))
            pid = payload.get("pid") if isinstance(payload, dict) else None
            result[name] = {
                "present": True,
                "pid": pid if isinstance(pid, int) else None,
                "payload": payload if isinstance(payload, dict) else None,
            }
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            result[name] = {"present": True, "unreadable": f"{type(exc).__name__}: {exc}"}
    return result


# --------------------------------------------------------------------------- 备份


def create_backup(data_root: Path, dest: Path) -> BackupManifest:
    """在同一一致点导出整个数据根；原子发布到 `dest`，永不覆盖已有备份。"""
    data_root = Path(data_root).resolve()
    dest = Path(dest).resolve()
    if not data_root.is_dir():
        raise BackupRefused(f"数据根不存在或不是目录：{data_root}")
    if dest.exists():
        raise BackupRefused(
            f"备份目标已存在：{dest}。本模块永不覆盖备份：换一个路径，"
            "或先人工确认旧备份可以移走。"
        )
    if dest == data_root or data_root in dest.parents:
        raise BackupRefused(f"备份目标不能放在数据根里：{dest}")
    if not (data_root / "events.db").is_file():
        raise BackupRefused(
            f"数据根里没有事件库：{data_root}。这不像一个天枢数据根，拒绝生成空备份。"
        )
    if (data_root / "events.db").is_symlink():
        raise BackupRefused("不允许符号链接事件库：events.db")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}{_PARTIAL_TAG}{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    marker = tmp / PARTIAL_MARKER
    atomic_write_bytes(marker, b"incomplete backup: interrupted create_backup\n")
    try:
        # 先取事件库的一致点，再复制材料。
        #
        # 顺序的理由：写路径是"先落 blob，再写事件行"（EventLog.append 的
        # 大 payload 也是先写 blob）。DB 先快照 → 之后复制的材料至少包含 DB
        # 快照引用到的 blob；反过来（先复制材料再快照 DB）则可能漏掉窗口内
        # 新写的 payload blob，而 DB 已经引用它——verify 会（正确地）判备份
        # 无效。多复制到的、比 DB 新的材料只是"多出来的证据"，不破坏一致性。
        # 真正要跨"事件库 + 材料"严格一致，必须停写者；本模块不停止写者，
        # 而是用在线备份 API + 上述顺序把窗口压小，并在文档里如实写清边界。
        _db_snapshot(data_root / "events.db", tmp / "events.db")
        failpoint("backup-after-db")
        files: list[BackupFile] = []
        with (tmp / "events.db").open("rb") as fh:
            sha, size = _sha256_stream(fh)
        files.append(BackupFile("events.db", sha, size, "event_log"))
        # 材料（blob / 清单 / 撤销日志）都是内容寻址或原子替换的文件：逐个复制。
        for src, rel in _iter_backup_inputs(data_root):
            sha, size = _copy_file_verified(src, tmp / rel)
            files.append(BackupFile(rel, sha, size, _category_for(rel)))
        files.sort(key=lambda f: f.path)

        meta = _db_meta(tmp / "events.db", saved=True)
        source_aliases: list[str] = []
        references, reference_problems = _material_references(
            tmp, source_root=data_root, discover_aliases=source_aliases
        )
        failpoint("backup-before-manifest")

        manifest = BackupManifest(
            format=FORMAT_NAME,
            format_version=FORMAT_VERSION,
            app_version=__version__,
            db_user_version=int(meta["db_user_version"]),
            created_at_ms=int(time.time() * 1000),
            source_name=data_root.name,
            source_path=str(data_root),
            source_aliases=sorted(source_aliases),
            files=files,
            event_count=int(meta["event_count"]),
            event_seq_head=int(meta["event_seq_head"]),
            run_ids=list(meta["run_ids"]),
            references={
                **references,
                "missing": [],
                "problems": reference_problems,
                "seq_contiguous": bool(meta["seq_contiguous"]),
            },
            writer=inspect_writers(data_root),
            excluded=sorted(EXCLUDED_TOP_LEVEL) + ["events.db-wal", "events.db-shm"],
            retention=RETENTION_POLICY,
            restore_notes=list(_RESTORE_NOTES),
        )
        atomic_write_bytes(tmp / MANIFEST_NAME, manifest.to_json_bytes())
        # 清单写完、还没去掉 .partial 标记/改名：如果在这里断电，目录里"看起来
        # 什么都有"，只有 .partial 标记能证明它不是一份已发布的备份。
        failpoint("backup-after-manifest")
        # 内部校验只忽略本次构建标记；公共 verify 始终拒绝未发布目录。
        _verify_contents(tmp, manifest, allow_partial_marker=True).require_ok()
        _fsync_tree(tmp)
        marker.unlink()
        _fsync_dir(tmp)
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
        return manifest
    except BaseException:
        # os._exit 注入不会走到这里（那正是"断电"语义）；正常异常才清理。
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _ref_owner(ref: dict[str, Any]) -> str:
    if "manifest" in ref:
        return f"快照清单 {ref['manifest']}"
    if "journal" in ref:
        return f"撤销日志 {ref['journal']}"
    if "seq" in ref:
        return f"事件 seq={ref['seq']} 的 payload_ref"
    if "memory_id" in ref:
        return f"记忆 {ref['memory_id']}"
    if "content_digest" in ref:
        return f"内容对象 {ref['content_digest']}"
    return "备份引用"


# --------------------------------------------------------------------------- 校验


def _load_manifest(dest: Path, problems: list[str]) -> BackupManifest | None:
    try:
        path = _checked_file(dest, MANIFEST_NAME)
        if not path.is_file():
            problems.append(f"缺少 {MANIFEST_NAME}（不是备份目录，或备份没有写完）")
            return None
        raw = json.loads(path.read_bytes().decode("utf-8"))
        manifest = BackupManifest.from_dict(raw)
    except (OSError, ValueError, TypeError, UnicodeDecodeError) as exc:
        problems.append(f"{MANIFEST_NAME} 读不出来：{type(exc).__name__}: {exc}")
        return None
    if manifest.format != FORMAT_NAME:
        problems.append(f"备份格式不认识：{manifest.format!r}（期望 {FORMAT_NAME!r}）")
        return None
    if manifest.format_version > FORMAT_VERSION:
        problems.append(
            f"备份格式版本 {manifest.format_version} 比本程序支持的 {FORMAT_VERSION} 新；"
            "不能按旧语义解释新备份"
        )
        return manifest
    if not manifest.files:
        problems.append("备份清单里没有任何文件")
    return manifest


def _check_files(
    dest: Path, manifest: BackupManifest, problems: list[str], *,
    allow_partial_marker: bool = False,
) -> tuple[int, int]:
    checked = 0
    total = 0
    declared: dict[str, BackupFile] = {}
    for entry in manifest.files:
        try:
            rel = _validate_rel(entry.path)
        except ValueError as exc:
            problems.append(f"清单里的路径非法：{exc}")
            continue
        if rel in declared:
            problems.append(f"清单里的路径重复：{rel}")
        declared[rel] = entry
    actual: set[str] = set()
    for path in dest.rglob("*"):
        rel = _rel_posix(path, dest)
        if path.is_symlink():
            problems.append(f"不允许符号链接材料：{rel}")
            continue
        if path.is_file():
            if rel == MANIFEST_NAME or (allow_partial_marker and rel == PARTIAL_MARKER):
                continue
            actual.add(rel)
    for rel in sorted(actual - set(declared)):
        problems.append(f"备份里有清单之外的文件：{rel}")
    for rel, entry in sorted(declared.items()):
        try:
            path = _checked_file(dest, rel)
            if not path.is_file():
                problems.append(f"清单声明的文件缺失：{rel}")
                continue
            size = path.stat().st_size
            if size != entry.size:
                problems.append(f"{rel} 大小不符：清单 {entry.size}，实际 {size}")
                continue
            sha = sha256_file(path)
        except (OSError, ValueError) as exc:
            problems.append(f"清单文件无法核验：{rel}（{exc}）")
            continue
        if sha != entry.sha256:
            problems.append(f"{rel} 内容被改过：清单 {entry.sha256[:12]}…，实际 {sha[:12]}…")
            continue
        checked += 1
        total += size
    return checked, total


def _check_db(dest: Path, manifest: BackupManifest, problems: list[str]) -> int | None:
    try:
        db = _checked_file(dest, "events.db")
        if not db.is_file():
            problems.append("备份里没有 events.db")
            return None
        meta = _db_meta(db, saved=True)
        conn = _connect_saved_readonly(db)
        try:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT seq,payload AS payload_inline,payload_ref,payload_hash "
                "FROM events ORDER BY seq"
            ):
                try:
                    read_saved_event_payload(
                        dest, row, source_root=Path(manifest.source_path),
                        source_aliases=manifest.source_aliases,
                    )
                except BackupInvalid as exc:
                    problems.extend(f"事件 seq={row['seq']}：{p}" for p in exc.problems)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        problems.append(f"备份的事件库打不开：{type(exc).__name__}: {exc}")
        return None
    if int(meta["db_user_version"]) != manifest.db_user_version:
        problems.append(
            f"事件库结构版本不符：清单 {manifest.db_user_version}，"
            f"实际 {meta['db_user_version']}"
        )
    if int(meta["event_count"]) != manifest.event_count:
        problems.append(
            f"事件条数不符：清单 {manifest.event_count}，实际 {meta['event_count']}"
        )
    if int(meta["event_seq_head"]) != manifest.event_seq_head:
        problems.append(
            f"事件 seq_head 不符：清单 {manifest.event_seq_head}，"
            f"实际 {meta['event_seq_head']}"
        )
    if not meta["seq_contiguous"]:
        problems.append(
            "事件 seq 不连续（count != max 或首条不是 1）：这份事件库不能作为完整历史"
        )
    return int(meta["db_user_version"])


def _check_references(
    dest: Path, manifest: BackupManifest, problems: list[str]
) -> None:
    for key, label in (("missing", "引用就已缺失"), ("problems", "引用关系就读不出来")):
        values = manifest.references.get(key, [])
        if not isinstance(values, list):
            problems.append(f"references.{key} 必须是列表")
            continue
        problems.extend(f"备份时{label}：{value}" for value in values)
    _check_reference_groups(dest, manifest, manifest.references, problems)
    try:
        saved, saved_problems = _material_references(
            dest, source_root=Path(manifest.source_path),
            source_aliases=manifest.source_aliases,
        )
    except (sqlite3.Error, OSError, ValueError) as exc:
        problems.append(f"保存的材料引用无法推导：{type(exc).__name__}: {exc}")
        return
    problems.extend(saved_problems)
    _check_reference_groups(dest, manifest, saved, problems)


def _check_reference_groups(
    dest: Path, manifest: BackupManifest, references: dict[str, Any], problems: list[str],
) -> None:
    declared = {f.path for f in manifest.files}
    for key in ("event_payload_refs", "snapshot_blobs", "undo_journal_blobs",
                "content_blobs", "memory_blobs"):
        refs = references.get(key, [])
        if not isinstance(refs, list):
            problems.append(f"references.{key} 必须是列表")
            continue
        for ref in refs:
            try:
                if not isinstance(ref, dict):
                    raise ValueError("引用必须是对象")
                rel = _validate_rel(ref.get("path"))
                expected = ref.get("sha256")
                if not _is_digest(expected):
                    raise ValueError(f"引用 sha256 非法：{expected!r}")
            except ValueError as exc:
                problems.append(f"{key} 的引用路径非法：{exc}")
                continue
            if rel not in declared:
                problems.append(
                    f"{key} 引用的文件不在备份里：{rel}（{_ref_owner(ref)}）"
                )
                continue
            try:
                path = _checked_file(dest, rel)
                if not path.is_file():
                    problems.append(f"{key} 引用的文件缺失：{rel}")
                    continue
                actual = sha256_file(path)
                if actual != expected:
                    problems.append(
                        f"{key} 引用 {rel} 的内容哈希不符"
                        f"（引用 {expected[:12]}…，实际 {actual[:12]}…）"
                    )
                if "size" in ref:
                    size = ref["size"]
                    if (not isinstance(size, int) or isinstance(size, bool)
                            or size < 0 or path.stat().st_size != size):
                        problems.append(f"{key} 引用 {rel} 的大小不符：登记 {size!r}")
            except (OSError, ValueError) as exc:
                problems.append(f"{key} 引用无法核验：{rel}（{exc}）")


def _verify_contents(
    dest: Path, manifest: BackupManifest, *,
    allow_partial_marker: bool = False, problems: list[str] | None = None,
) -> BackupVerification:
    problems = list(problems or [])
    files_checked, bytes_checked = _check_files(
        dest, manifest, problems, allow_partial_marker=allow_partial_marker
    )
    companion_names = {"events.db-wal", "events.db-shm", "events.db-journal"}
    companions = sorted(path.name for path in dest.iterdir()
                        if path.name.casefold() in companion_names)
    if companions:
        problems.extend(f"备份不是自包含数据库，拒绝伴随文件：{rel}" for rel in companions)
        return BackupVerification(
            ok=False, manifest=manifest, problems=problems,
            files_checked=files_checked, bytes_checked=bytes_checked,
        )
    db_user_version = _check_db(dest, manifest, problems)
    _check_references(dest, manifest, problems)
    if manifest.format_version < 1:
        problems.append(f"备份格式版本非法：{manifest.format_version}")
    return BackupVerification(
        ok=not problems, manifest=manifest, problems=problems,
        files_checked=files_checked, bytes_checked=bytes_checked,
        db_user_version=db_user_version,
    )


def verify_backup(dest: Path) -> BackupVerification:
    """校验备份：哈希、引用完整性、事件 seq 连续、清单可解析、版本兼容。

    读不懂/不完整一律 `ok=False` 并给补救路径；**不**把它当成"空历史"。
    """
    dest = Path(dest)
    problems: list[str] = []
    remediation: list[str] = [
        "确认路径指向一个完整的备份目录（含 backup.json）",
        "如果是中断留下的 .partial-* 目录：删除它，重新执行 create_backup",
        "如果备份文件缺失/被改过：换一个备份恢复，或用旧版本程序核对现场后再备份",
    ]
    if dest.is_symlink():
        return BackupVerification(False, None, ["备份目录不允许符号链接"], remediation)
    if not dest.exists():
        return BackupVerification(
            False, None, [f"备份目录不存在：{dest}"], remediation
        )
    if not dest.is_dir():
        return BackupVerification(
            False, None, [f"备份路径不是目录：{dest}"], remediation
        )
    if (PARTIAL_MARKER in dest.name or (dest / PARTIAL_MARKER).exists()
            or (dest / PARTIAL_MARKER).is_symlink()):
        return BackupVerification(
            False, None,
            [f"这是一个未完成的临时备份目录（含 {PARTIAL_MARKER} 标记）：不是有效备份"],
            ["删除这个临时目录；有效的备份只会出现在 create_backup 的最终目标路径"],
        )

    manifest = _load_manifest(dest, problems)
    if manifest is not None:
        result = _verify_contents(dest, manifest, problems=problems)
        result.remediation = remediation if result.problems else []
        return result
    return BackupVerification(False, None, problems, remediation)


# --------------------------------------------------------------------------- 恢复


def _target_is_clean(data_root: Path) -> bool:
    if not data_root.exists():
        return True
    if not data_root.is_dir():
        return False
    return not any(data_root.iterdir())


def _reconcile(data_root: Path) -> dict[str, Any]:
    """恢复后核对：未决 intent/result、审批、非终态 Run、仍有效的租约。"""
    db = data_root / "events.db"
    conn = _connect_readonly(db)
    try:
        conn.row_factory = sqlite3.Row
        intents = conn.execute(
            "SELECT seq, run_id, payload FROM events WHERE type='tool.intent'"
            " ORDER BY seq"
        ).fetchall()
        results = conn.execute(
            "SELECT run_id, payload FROM events WHERE type='tool.result'"
        ).fetchall()
        pending: list[dict[str, Any]] = []
        result_calls: dict[tuple[str, str], None] = {}
        for row in results:
            payload = _json_payload(row["payload"])
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                result_calls[(row["run_id"], call_id)] = None
        for row in intents:
            payload = _json_payload(row["payload"])
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                continue
            if (row["run_id"], call_id) not in result_calls:
                pending.append({
                    "run_id": row["run_id"],
                    "call_id": call_id,
                    "tool_id": str(payload.get("tool_id", "")),
                    "seq": int(row["seq"]),
                })

        approvals: list[dict[str, Any]] = []
        if _has_table(conn, "approvals"):
            for row in conn.execute(
                "SELECT approval_id, run_id, action, state, resources, expires_at"
                " FROM approvals WHERE state IN ('pending','approved')"
                " ORDER BY created_at"
            ):
                approvals.append({
                    "approval_id": row["approval_id"],
                    "run_id": row["run_id"],
                    "action": row["action"],
                    "state": row["state"],
                    "resources": row["resources"],
                    "expires_at": int(row["expires_at"]),
                })

        runs: list[dict[str, Any]] = []
        if _has_table(conn, "runs"):
            for row in conn.execute(
                "SELECT run_id, task_id, state FROM runs"
                " WHERE state NOT IN ('succeeded','failed','cancelled','fenced')"
                " ORDER BY created_at"
            ):
                runs.append({
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "state": row["state"],
                })

        now_ms = int(time.time() * 1000)
        leases: list[dict[str, Any]] = []
        if _has_table(conn, "leases"):
            for row in conn.execute(
                "SELECT resource_key, run_id, owner_pid, owner_instance_id, expires_at"
                " FROM leases WHERE expires_at > ? ORDER BY resource_key",
                (now_ms,),
            ):
                leases.append({
                    "resource_key": row["resource_key"],
                    "run_id": row["run_id"],
                    "owner_pid": int(row["owner_pid"]),
                    "owner_instance_id": row["owner_instance_id"],
                    "expires_at": int(row["expires_at"]),
                })
    finally:
        conn.close()
    return {
        "pending_intents": pending,
        "pending_approvals": approvals,
        "non_terminal_runs": runs,
        "live_leases": leases,
    }


def _json_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def restore_backup(dest: Path, data_root: Path) -> RestoreReport:
    """把校验通过的备份恢复到**干净根**；失败时原目标保持原样。"""
    dest = Path(dest).resolve()
    data_root = Path(data_root).resolve()
    verification = verify_backup(dest)
    manifest = verification.require_ok()

    # 结构版本兼容性：备份来自更新版本 → 拒绝（不按猜的语义恢复）。
    from .migrate import SCHEMA_VERSION  # 延迟导入，避免 backup ↔ migrate 循环

    if manifest.db_user_version > SCHEMA_VERSION:
        raise BackupInvalid(
            [
                f"备份的结构版本 {manifest.db_user_version} 比本程序支持的 "
                f"{SCHEMA_VERSION} 新"
            ],
            [
                "用与备份匹配的新版本程序恢复；或用旧版备份",
                "不要手工改 PRAGMA user_version：版本号与表结构必须由迁移步骤一起落地",
            ],
        )

    if not _target_is_clean(data_root):
        raise BackupRefused(
            f"恢复目标不是干净根：{data_root}。只允许恢复到不存在或空目录，"
            "避免覆盖现有数据；请先人工移走原数据再恢复。"
        )
    source_path = Path(manifest.source_path).resolve()
    if data_root != source_path:
        raise RestoreRefused(
            f"恢复目标与备份记录的原数据根不一致：目标 {data_root}，"
            f"备份来自 {source_path}。事件 payload_ref 与快照 blob_ref 都是绝对"
            "路径，换路径恢复会让引用指向旧位置（读旧数据或读不到）。"
            "请恢复到原路径；若必须换路径，先恢复到原路径，等支持重定位的版本"
            "落地后再搬迁（本轮未实现）。"
        )

    parent = data_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{data_root.name}.restore-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    restored: list[BackupFile] = []
    total = 0
    try:
        for entry in manifest.files:
            rel = _validate_rel(entry.path)
            src = dest / rel
            dst = tmp / rel
            sha, size = _copy_file_verified(src, dst)
            if sha != entry.sha256 or size != entry.size:
                raise RestoreFailed(
                    f"恢复时 {rel} 与清单不符（清单 {entry.sha256[:12]}…/{entry.size}，"
                    f"实际 {sha[:12]}…/{size}）：中止，原目标未改动"
                )
            restored.append(entry)
            total += size
        _fsync_tree(tmp)
        _fsync_dir(tmp)
        if data_root.exists():
            data_root.rmdir()  # 已确认是空目录
        os.replace(tmp, data_root)
        _fsync_dir(parent)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    warnings: list[str] = []
    if manifest.db_user_version < SCHEMA_VERSION:
        warnings.append(
            f"备份结构版本 {manifest.db_user_version} 小于当前 {SCHEMA_VERSION}："
            "恢复后需要运行迁移（migrate）才能写入"
        )
    if manifest.references.get("missing"):
        warnings.append("备份时已有缺失引用（见 backup.json references.missing）")
    reconciliation = _reconcile(data_root)
    if reconciliation["pending_intents"]:
        warnings.append(
            "恢复后仍有未决 intent（有 intent 无 result）：恢复证据在后，需人工核对"
        )
    if reconciliation["live_leases"]:
        warnings.append(
            "恢复时仍有效的租约只是备份时刻的投影；持有进程可能已不存在，"
            "需人工确认后再写入"
        )
    return RestoreReport(
        ok=True,
        data_root=data_root,
        manifest=manifest,
        files_restored=len(restored),
        bytes_restored=total,
        reconciliation=reconciliation,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- 保留期 / 引用保护


@dataclass
class ReferenceIndex:
    protected_blobs: dict[str, list[str]] = field(default_factory=dict)  # blob rel → 引用者
    snapshot_manifests: list[str] = field(default_factory=list)
    undo_journals: dict[str, str] = field(default_factory=dict)  # rel → status
    orphan_blobs: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protected_blobs": {
                path: sorted(owners) for path, owners in sorted(self.protected_blobs.items())
            },
            "snapshot_manifests": list(self.snapshot_manifests),
            "undo_journals": dict(sorted(self.undo_journals.items())),
            "orphan_blobs": list(self.orphan_blobs),
            "unknown": list(self.unknown),
        }


def collect_references(data_root: Path) -> ReferenceIndex:
    """算出"哪些 blob 被引用、哪些是孤儿、哪些判定不了"。

    判定不了（清单/日志/事件库读不出来，或登记的内容/未擦除记忆正文无法通过
    哈希与大小核验）必须抛 `ReferenceUnknown`：不给出清理结论。误删证据比
    留一份多余 blob 严重得多；本函数不执行删除。
    """
    data_root = Path(data_root).resolve()
    db = data_root / "events.db"
    if not db.is_file():
        raise ReferenceUnknown(f"数据根里没有事件库，无法判定引用：{data_root}")
    index = ReferenceIndex()
    try:
        db = _checked_file(data_root, "events.db")
        source_aliases: list[str] = []
        payload_refs, payload_problems = _db_references(
            db, data_root, discover_aliases=source_aliases
        )
        snapshot_refs, snapshot_problems = _snapshot_references(
            data_root, source_root=data_root, discover_aliases=source_aliases
        )
        undo_refs, undo_problems = _undo_references(data_root)
        content_refs, memory_refs, projection_problems = _projection_references(db)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ReferenceUnknown(f"事件库读不出来，无法判定引用：{exc}") from exc
    for problem in payload_problems + snapshot_problems + undo_problems + projection_problems:
        index.unknown.append(problem)
    for ref in payload_refs:
        index.protected_blobs.setdefault(ref["path"], []).append(
            f"事件 seq={ref['seq']}"
        )
    for ref in snapshot_refs:
        index.protected_blobs.setdefault(ref["path"], []).append(
            f"快照清单 {ref['manifest']}"
        )
        if ref["manifest"] not in index.snapshot_manifests:
            index.snapshot_manifests.append(ref["manifest"])
    for ref in undo_refs:
        index.protected_blobs.setdefault(ref["path"], []).append(
            f"撤销日志 {ref['journal']}"
        )
    for ref in content_refs + memory_refs:
        rel = ref["path"]
        index.protected_blobs.setdefault(rel, []).append(_ref_owner(ref))
        try:
            with _checked_file(data_root, rel).open("rb") as fh:
                digest, size = _sha256_stream(fh)
            if digest != ref["sha256"]:
                raise ValueError("内容哈希不符")
            if "size" in ref and size != ref["size"]:
                raise ValueError(f"大小不符：登记 {ref['size']}，实际 {size}")
        except (OSError, ValueError) as exc:
            index.unknown.append(f"投影材料无法核验：{rel}（{exc}）")
    if index.unknown:
        raise ReferenceUnknown("；".join(index.unknown))
    journals = data_root / "artifacts" / "undo"
    if journals.is_dir():
        for journal in sorted(journals.glob("*.json")):
            rel = _rel_posix(journal, data_root)
            try:
                raw = json.loads(journal.read_bytes().decode("utf-8"))
                status = raw.get("status") if isinstance(raw, dict) else None
                index.undo_journals[rel] = str(status)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                index.unknown.append(f"撤销日志读不出来：{rel}（{exc}）")
    blobs_dir = data_root / "artifacts" / "blobs"
    if blobs_dir.is_dir():
        for blob in sorted(blobs_dir.iterdir()):
            if not blob.is_file():
                continue
            rel = _rel_posix(blob, data_root)
            if rel not in index.protected_blobs:
                index.orphan_blobs.append(rel)
    if index.unknown:
        raise ReferenceUnknown("；".join(index.unknown))
    return index


@dataclass
class UsageReport:
    data_root: Path
    categories: dict[str, dict[str, Any]]
    over_budget: list[str] = field(default_factory=list)
    total_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_root": str(self.data_root),
            "categories": self.categories,
            "over_budget": list(self.over_budget),
            "total_bytes": self.total_bytes,
        }


def usage_report(data_root: Path) -> UsageReport:
    """按保留期声明的类别统计磁盘占用，并对照声明额度。不清理任何东西。"""
    data_root = Path(data_root).resolve()
    sizes = {
        "event_log": 0,
        "snapshot_manifests": 0,
        "snapshot_blobs": 0,
        "undo_journals": 0,
        "diagnostic_trace": 0,
    }
    counts = dict.fromkeys(sizes, 0)
    db = data_root / "events.db"
    if db.is_file():
        sizes["event_log"] += db.stat().st_size
        counts["event_log"] += 1
    artifacts = data_root / "artifacts"
    if artifacts.is_dir():
        for path in artifacts.rglob("*"):
            if not path.is_file():
                continue
            rel = _rel_posix(path, data_root)
            category = _category_for(rel)
            if category == "snapshot_manifest":
                sizes["snapshot_manifests"] += path.stat().st_size
                counts["snapshot_manifests"] += 1
            elif category == "blob":
                sizes["snapshot_blobs"] += path.stat().st_size
                counts["snapshot_blobs"] += 1
            elif category == "undo_journal":
                sizes["undo_journals"] += path.stat().st_size
                counts["undo_journals"] += 1
    logs = data_root / "logs"
    if logs.is_dir():
        for path in logs.rglob("*"):
            if path.is_file():
                sizes["diagnostic_trace"] += path.stat().st_size
                counts["diagnostic_trace"] += 1
    categories: dict[str, dict[str, Any]] = {}
    over_budget: list[str] = []
    for name, policy in RETENTION_POLICY.items():
        budget = policy.get("budget_bytes")
        used = sizes.get(name, 0)
        record = {
            "bytes": used,
            "files": counts.get(name, 0),
            "budget_bytes": budget,
            "retention": policy["retention"],
            "owner": policy["owner"],
        }
        if isinstance(budget, int) and used > budget:
            over_budget.append(name)
        categories[name] = record
    total = db.stat().st_size if db.is_file() else 0
    if artifacts.is_dir():
        total += sum(p.stat().st_size for p in artifacts.rglob("*") if p.is_file())
    if logs.is_dir():
        total += sum(p.stat().st_size for p in logs.rglob("*") if p.is_file())
    return UsageReport(data_root, categories, over_budget, total)


def cleanup_plan(data_root: Path) -> dict[str, Any]:
    """给出"可人工清理"的候选清单——不删除任何东西，也不自动 GC。

    候选范围仅 artifacts/blobs；只有未被清单/事件/撤销/内容对象/未擦除记忆
    引用的 blob 才是候选。引用或投影材料无法核验时 `collect_references` 抛
    `ReferenceUnknown`，本函数跟着拒绝，不扩大为自动 GC。
    """
    index = collect_references(data_root)
    return {
        "data_root": str(Path(data_root).resolve()),
        "dry_run": True,
        "candidates": [
            {"path": rel, "category": "artifact_blob"}
            for rel in index.orphan_blobs
        ],
        "protected_count": len(index.protected_blobs),
        "undo_journals_in_progress": [
            rel for rel, status in index.undo_journals.items()
            if status == "in_progress"
        ],
        "retention": RETENTION_POLICY,
        "notes": [
            "本函数只出候选清单，不删除任何文件；自动 GC 不在本轮范围。",
            "清单/事件/撤销/内容对象/未擦除记忆引用的材料永远不进候选。",
        ],
    }


__all__ = [
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "MANIFEST_NAME",
    "RETENTION_POLICY",
    "BackupError",
    "BackupFile",
    "BackupInvalid",
    "BackupManifest",
    "BackupRefused",
    "BackupVerification",
    "ReferenceIndex",
    "ReferenceUnknown",
    "RestoreFailed",
    "RestoreRefused",
    "RestoreReport",
    "UsageReport",
    "cleanup_plan",
    "collect_references",
    "create_backup",
    "inspect_writers",
    "restore_backup",
    "sha256_file",
    "usage_report",
    "verify_backup",
]
