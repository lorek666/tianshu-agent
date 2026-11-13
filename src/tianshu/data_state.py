"""迁移进度的严格类型与纯校验；不依赖迁移执行器或业务存储。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .errors import TianshuError

SCHEMA_VERSION = 2
STATE_FORMAT = "tianshu-migration"
STATE_FORMAT_VERSION = 2
DataFailure = Literal[
    "incompatible",
    "migration_incomplete",
    "progress_corrupt",
    "backup_mismatch",
    "maintenance_busy",
    "migration_failed",
]
ProgressPosition = Literal["idle", "before_step", "after_step", "completed"]
_FAILURES = frozenset(
    {
        "incompatible",
        "migration_incomplete",
        "progress_corrupt",
        "backup_mismatch",
        "maintenance_busy",
        "migration_failed",
    }
)


class AdmissionRefused(TianshuError):
    def __init__(self, code: DataFailure, message: str) -> None:
        if code not in _FAILURES:
            raise ValueError(f"未知数据错误类别：{code!r}")
        self.code = code
        super().__init__(
            f"[{code}] {message}。请保全数据目录，使用匹配版本核对备份和恢复；"
            "不要手改版本或删除数据库。"
        )


class MigrationStateCorrupt(AdmissionRefused):
    def __init__(self, message: str) -> None:
        super().__init__("progress_corrupt", message)


def _integer(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise MigrationStateCorrupt(f"{name}必须是非负整数，不能强制转换：{value!r}")


def _text(value: object, name: str) -> None:
    if type(value) is not str or not value:
        raise MigrationStateCorrupt(f"{name}必须是非空字符串")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise MigrationStateCorrupt(f"{name}必须是小写SHA256")


def _canonical(value: object, name: str) -> None:
    _text(value, name)
    try:
        p = Path(value)
        valid = p.is_absolute() and str(p.resolve()) == value
    except (OSError, RuntimeError, ValueError) as exc:
        raise MigrationStateCorrupt(f"{name}无法确定实际路径") from exc
    if not valid:
        raise MigrationStateCorrupt(f"{name}必须是canonical绝对路径")


def _progress(origin, target, status, applied, current, started, updated) -> None:
    _integer(origin, "from_version")
    _integer(target, "target_version")
    if origin > SCHEMA_VERSION or target != SCHEMA_VERSION:
        raise MigrationStateCorrupt("迁移起止版本不在本程序机制链内")
    if type(status) is not str or status not in ("in_progress", "completed"):
        raise MigrationStateCorrupt("未知迁移状态")
    expected = tuple(range(origin + 1, target + 1))
    if type(applied) is not tuple or any(type(v) is not int for v in applied):
        raise MigrationStateCorrupt("applied_steps必须是严格整数序列")
    if len(applied) > len(expected) or applied != expected[: len(applied)]:
        raise MigrationStateCorrupt("applied_steps不是从起点开始的连续前缀")
    if current is not None:
        _integer(current, "current_step")
        if len(applied) == len(expected) or current != expected[len(applied)]:
            raise MigrationStateCorrupt("current_step不是紧随已完成前缀的下一步")
    if status == "completed" and (applied != expected or current is not None):
        raise MigrationStateCorrupt("completed没有完整步骤或仍有当前步骤")
    _integer(started, "started_at_ms")
    _integer(updated, "updated_at_ms")
    if updated < started:
        raise MigrationStateCorrupt("updated_at_ms早于started_at_ms")


@dataclass(frozen=True)
class BaselineDigest:
    sqlite_algorithm: str
    sqlite_sha256: str
    material_algorithm: str
    material_sha256: str

    def __post_init__(self) -> None:
        if (
            self.sqlite_algorithm != "tianshu-sqlite-logical-v1"
            or self.material_algorithm != "tianshu-material-inventory-v1"
        ):
            raise MigrationStateCorrupt("未知基线算法")
        _digest(self.sqlite_sha256, "sqlite_sha256")
        _digest(self.material_sha256, "material_sha256")


@dataclass(frozen=True)
class BackupBinding:
    path: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _canonical(self.path, "backup.path")
        _digest(self.manifest_sha256, "manifest_sha256")


@dataclass(frozen=True)
class MigrationStateV2:
    db_path: str
    from_version: int
    target_version: int
    status: Literal["in_progress", "completed"]
    applied_steps: tuple[int, ...]
    current_step: int | None
    plan_sha256: str
    backup: BackupBinding
    baseline: BaselineDigest
    started_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        _canonical(self.db_path, "db_path")
        _digest(self.plan_sha256, "plan_sha256")
        if type(self.backup) is not BackupBinding or type(self.baseline) is not BaselineDigest:
            raise MigrationStateCorrupt("缺少强类型备份/基线绑定")
        _progress(
            self.from_version,
            self.target_version,
            self.status,
            self.applied_steps,
            self.current_step,
            self.started_at_ms,
            self.updated_at_ms,
        )


@dataclass(frozen=True)
class LegacyBackupV1:
    path: str
    format_version: int
    db_user_version: int
    event_count: int

    def __post_init__(self) -> None:
        _text(self.path, "legacy.backup.path")
        for name in ("format_version", "db_user_version", "event_count"):
            _integer(getattr(self, name), f"legacy.backup.{name}")


@dataclass(frozen=True)
class LegacyStateV1:
    target_version: int
    status: Literal["in_progress", "completed"]
    applied_steps: tuple[int, ...]
    current_step: int | None
    backup: LegacyBackupV1
    started_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        if type(self.backup) is not LegacyBackupV1:
            raise MigrationStateCorrupt("旧备份记录类型不合法")
        _progress(
            0,
            self.target_version,
            self.status,
            self.applied_steps,
            self.current_step,
            self.started_at_ms,
            self.updated_at_ms,
        )


MigrationState = MigrationStateV2 | LegacyStateV1


def _fields(raw: object, names: set[str]) -> dict:
    if type(raw) is not dict or set(raw) != names:
        raise MigrationStateCorrupt(f"进度字段必须恰为{sorted(names)}")
    return raw


def parse_state(raw: object) -> MigrationState:
    if type(raw) is not dict or raw.get("format") != STATE_FORMAT:
        raise MigrationStateCorrupt("未知进度格式")
    version = raw.get("format_version")
    if type(version) is not int or version not in (1, 2):
        raise MigrationStateCorrupt("不支持的进度格式版本")
    names = {
        "target_version",
        "status",
        "applied_steps",
        "current_step",
        "backup",
        "started_at_ms",
        "updated_at_ms",
    }
    if version == 2:
        names |= {"db_path", "from_version", "plan_sha256", "baseline"}
    _fields(raw, names | {"format", "format_version"})
    if type(raw["applied_steps"]) is not list:
        raise MigrationStateCorrupt("JSON applied_steps必须是数组")
    args = {name: raw[name] for name in names}
    args["applied_steps"] = tuple(raw["applied_steps"])
    if version == 1:
        args["backup"] = LegacyBackupV1(
            **_fields(raw["backup"], {"path", "format_version", "db_user_version", "event_count"})
        )
        return LegacyStateV1(**args)
    args["backup"] = BackupBinding(**_fields(raw["backup"], {"path", "manifest_sha256"}))
    args["baseline"] = BaselineDigest(
        **_fields(
            raw["baseline"],
            {"sqlite_algorithm", "sqlite_sha256", "material_algorithm", "material_sha256"},
        )
    )
    return MigrationStateV2(**args)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MigrationStateCorrupt(f"JSON重复字段：{key}")
        result[key] = value
    return result


def read_state_file(path: Path) -> MigrationState | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MigrationStateCorrupt(f"进度无法检查：{path}") from exc
    try:
        if path.is_symlink():
            raise MigrationStateCorrupt(f"进度文件不能是符号链接：{path}")
        return parse_state(
            json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_unique_pairs)
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise MigrationStateCorrupt(f"进度不可读：{path}（{type(exc).__name__}）") from exc


def associated_state_paths(db_path: Path) -> tuple[Path, ...]:
    if str(db_path) == ":memory:":
        return ()
    try:
        actual = db_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise MigrationStateCorrupt("数据库实际路径不可确定") from exc
    paths = (actual.with_name(actual.name + ".migration.json"),)
    return paths + (actual.parent / "migration.json",) if actual.name == "events.db" else paths


def encode_state(state: MigrationStateV2) -> bytes:
    if type(state) is not MigrationStateV2:
        raise MigrationStateCorrupt("只允许写入format2；不自动转换旧进度")
    state.__post_init__()
    raw = {"format": STATE_FORMAT, "format_version": 2, **asdict(state)}
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def classify_progress(state: MigrationStateV2, actual_version: int) -> ProgressPosition:
    if type(state) is not MigrationStateV2:
        raise MigrationStateCorrupt("旧格式无新续做许可")
    state.__post_init__()
    _integer(actual_version, "actual_version")
    done_version = state.applied_steps[-1] if state.applied_steps else state.from_version
    if state.status == "completed" and actual_version == state.target_version:
        return "completed"
    if state.status == "in_progress":
        if actual_version == done_version:
            return "idle" if state.current_step is None else "before_step"
        if state.current_step is not None and actual_version == state.current_step:
            return "after_step"
    raise MigrationStateCorrupt("实际版本不在进度可达位置，不能补造完成步骤")
