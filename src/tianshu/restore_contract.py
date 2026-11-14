"""恢复B v1纯值契约。控制codec/SQL storage codec分离，不签发操作权威。

路径仅重算当前OS词法规范值；T1仍须以真实句柄证明实体、祖先、锁与持久化。
本模块没有manager、文件系统操作或可从JSON构造的RegistryObservation。
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import os
import re
import struct
import types
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, Self, Union, get_args, get_origin, get_type_hints

from .backup import BackupFile, BackupManifest, BackupVerification
from .pathkeys import overlap_key

Phase = Literal[
    "reserved",
    "stage_owned",
    "sealed_hold",
    "publish_intent",
    "published_hold",
    "abort_intent",
    "aborted",
]
ErrorCategory = Literal[
    "held",
    "record_corrupt",
    "binding_mismatch",
    "incompatible",
    "capacity_unavailable",
    "durability_unavailable",
    "maintenance_busy",
    "restore_failed",
]
PHASES = frozenset(get_args(Phase))
ERROR_CATEGORIES = frozenset(get_args(ErrorCategory))
DIGEST_LABELS = frozenset(
    {
        "meta",
        "event",
        "plan",
        "inventory",
        "sql-row",
        "sql-relation",
        "sql-schema",
        "scope",
        "binding",
        "cleanup",
        "token",
        "origin",
        "lock",
    }
)
CODEC = "restore-json-v1"
Version = Literal[1]
Sha256 = Annotated[str, "sha256"]
UUID = Annotated[str, "uuid"]
Base64 = Annotated[str, "base64"]
NonNegative = Annotated[int, "nonnegative"]
SequenceNumber = Annotated[int, "sequence"]
Name = Annotated[str, "name"]
NonEmpty = Annotated[str, "nonempty"]
RelPath = Annotated[tuple[str, ...], "relpath"]
_SQL_MIN, _SQL_MAX = -(2**63), 2**63 - 1


class RestoreContractError(ValueError):
    """值不符合此版契约；由调用层按真实上下文映射用户错误类别。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RestoreContractError(message)


def _json_value(value: object) -> object:
    if isinstance(value, (SqlValueV1, LockKeyV1)):
        return value.to_wire()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_value(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) in (list, tuple):
        return [_json_value(item) for item in value]
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "JSON对象键必须是字符串")
        return {key: _json_value(item) for key, item in value.items()}
    raise RestoreContractError(f"控制JSON不接受{type(value).__name__}，不强转/编码浮点")


def restore_json_v1(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        _require(key not in result, f"重复JSON字段：{key}")
        result[key] = value
    return result


def _reject_number(value: str) -> None:
    raise RestoreContractError(f"控制JSON禁止浮点/非有限数：{value}")


def decode_restore_json_v1(raw: bytes) -> object:
    _require(type(raw) is bytes, "控制记录必须为bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreContractError("不是严格UTF-8 JSON") from exc
    _require(restore_json_v1(value) == raw, "持久记录不是restore-json-v1规范字节")
    return value


def domain_digest(label: str, payload: object) -> str:
    _require(type(label) is str and label in DIGEST_LABELS, "未知恢复摘要域")
    return hashlib.sha256(
        b"tianshu.restore.v1\x00" + label.encode("ascii") + b"\x00" + restore_json_v1(payload),
    ).hexdigest()


def _unbase64(value: str) -> bytes:
    _require(type(value) is str, "base64必须为字符串")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RestoreContractError("非法标准base64") from exc
    _require(base64.b64encode(raw).decode("ascii") == value, "非规范带padding base64")
    return raw


def _name(value: str) -> None:
    _require(
        bool(value)
        and value not in (".", "..")
        and not any(c in value for c in "/\\\x00")
        and not PureWindowsPath(value).drive,
        "相对名称不能有空段、根、分隔符或穿越",
    )


def _metadata(value: object, tags: tuple[object, ...]) -> None:
    for tag in tags:
        if tag == "sha256":
            _require(re.fullmatch("[0-9a-f]{64}", value) is not None, "必须为64位小写SHA256")
        elif tag == "uuid":
            try:
                valid = str(uuid.UUID(value)) == value
            except (ValueError, AttributeError) as exc:
                raise RestoreContractError("非法UUID文本") from exc
            _require(valid, "UUID必须为标准小写文本")
        elif tag == "base64":
            _unbase64(value)
        elif tag == "nonnegative":
            _require(value >= 0, "整数必须非负")
        elif tag == "sequence":
            _require(0 < value <= _SQL_MAX, "sequence超出SQLite正整数范围")
        elif tag == "nonempty":
            _require(bool(value), "字符串不能为空")
        elif tag == "name":
            _name(value)
        elif tag == "relpath":
            for segment in value:
                _name(segment)
        else:
            raise RestoreContractError("未知内部字段约束")


@cache
def _hints(cls: type) -> dict[str, object]:
    return get_type_hints(cls, include_extras=True)


class ContractV1:
    """所有nullable字段仍必填；直接构造/解析/再次序列化均验证。"""

    def __post_init__(self) -> None:
        _validate(self)

    def to_dict(self) -> dict[str, object]:
        _validate(self)
        return _json_value(self)

    def to_bytes(self) -> bytes:
        return restore_json_v1(self.to_dict())

    @classmethod
    def from_dict(cls, raw: object, *, path_codec: NativePathCodecV1 | None = None) -> Self:
        return _convert(cls, raw, decode=True, path_codec=path_codec)

    @classmethod
    def from_bytes(cls, raw: bytes, *, path_codec: NativePathCodecV1 | None = None) -> Self:
        return cls.from_dict(decode_restore_json_v1(raw), path_codec=path_codec)


class NativePathCodecV1:
    """T0可注入词法边界，只支持当前OS，不能作为T1原生adapter。"""

    def canonical_parts(self, text: str) -> tuple[str, tuple[str, ...], str]:
        _require(type(text) is str and "\x00" not in text, "路径必须是无NUL文本")
        _require(
            os.path.isabs(text)
            and os.path.normpath(text) == text
            and os.path.abspath(text) == text,
            "路径必须为当前OS canonical绝对路径",
        )
        current = "windows" if os.name == "nt" else "posix"
        path = PureWindowsPath(text) if current == "windows" else PurePosixPath(text)
        return current, path.parts, overlap_key(text)

    def from_path(self, path: Path) -> NativePathV1:
        text = str(path)
        current, parts, key = self.canonical_parts(text)
        return NativePathV1(current, text, parts, key)


class EntityIdentityV1(ContractV1):
    """封闭OS身份值；没有句柄来源证明或原生查询能力。"""


class OriginalTargetV1(ContractV1):
    """absent无directory字段；empty必须保存原目录实体。"""


class RestorePayloadV1(ContractV1):
    """由envelope.kind选择的封闭payload，不从head猜测状态。"""


@dataclass(frozen=True)
class NativePathV1(ContractV1):
    os: Literal["posix", "windows"]
    text: str
    segments: tuple[str, ...]
    comparison_key: str


@dataclass(frozen=True)
class PosixEntityIdentityV1(EntityIdentityV1):
    os: Literal["posix"]
    device: NonNegative
    inode: NonNegative


@dataclass(frozen=True)
class WindowsEntityIdentityV1(EntityIdentityV1):
    os: Literal["windows"]
    volume_serial: NonNegative
    file_id_hex: str


@dataclass(frozen=True)
class DirRefV1(ContractV1):
    path: NativePathV1
    identity: EntityIdentityV1


@dataclass(frozen=True)
class EntityRefV1(ContractV1):
    path: NativePathV1
    identity: EntityIdentityV1


@dataclass(frozen=True)
class FileSnapshotV1(ContractV1):
    path: NativePathV1
    identity: EntityIdentityV1
    size: NonNegative
    sha256: Sha256


@dataclass(frozen=True)
class StablePlacementV1(ContractV1):
    canonical_root: NativePathV1
    final_name: Name
    existing_ancestors: tuple[DirRefV1, ...]
    pending_parent_suffix: tuple[Name, ...]


@dataclass(frozen=True)
class AbsentTargetV1(OriginalTargetV1):
    kind: Literal["absent"]


@dataclass(frozen=True)
class EmptyTargetV1(OriginalTargetV1):
    kind: Literal["empty"]
    directory: DirRefV1


@dataclass(frozen=True)
class CopyFileV1(ContractV1):
    path: RelPath
    size: NonNegative
    sha256: Sha256


@dataclass(frozen=True)
class CopyInventoryV1(ContractV1):
    version: Version
    directories: tuple[RelPath, ...]
    files: tuple[CopyFileV1, ...]


@dataclass(frozen=True)
class ManifestRefV1(ContractV1):
    saved_bytes_b64: Base64
    sha256: Sha256


@dataclass(frozen=True)
class DependencyRefV1(ContractV1):
    name: str
    version: str
    sha256: Sha256


@dataclass(frozen=True)
class PlanInputsV1(ContractV1):
    version: Version
    backup_root: DirRefV1
    manifest: ManifestRefV1
    copy_inventory: CopyInventoryV1
    inventory_sha256: Sha256
    saved_source_path: str
    source_aliases: tuple[str, ...]
    placement: StablePlacementV1
    original_target: OriginalTargetV1
    registry_id: UUID | None
    registry_head_sha256: Sha256 | None
    dependency_identities: tuple[DependencyRefV1, ...]
    layout_version: Version
    scope_mapping_version: Version


@dataclass(frozen=True)
class RegistryMetaV1(ContractV1):
    version: Version
    registry_id: UUID
    home: DirRefV1
    authority_directory: DirRefV1
    registry_database: EntityRefV1


@dataclass(frozen=True)
class ControlLayoutV1(ContractV1):
    formal: RelPath
    temporary: RelPath


@dataclass(frozen=True)
class TokenV1(ContractV1):
    version: Version
    registry_id: UUID
    batch_id: UUID
    reserved_sha256: Sha256
    stage: DirRefV1
    parent: DirRefV1


@dataclass(frozen=True)
class OriginV1(ContractV1):
    version: Version
    registry_id: UUID
    batch_id: UUID
    sealed_sha256: Sha256
    binding_sha256: Sha256


@dataclass(frozen=True)
class SqlValueV1(ContractV1):
    tag: Literal["null", "integer", "real", "text", "blob"]
    value: str | None

    def __post_init__(self) -> None:
        _require(
            type(self.tag) is str and self.tag in ("null", "integer", "real", "text", "blob"),
            "未知SQL storage tag",
        )
        if self.tag == "null":
            _require(self.value is None, "null不能附带值")
            return
        _require(type(self.value) is str, "SQL wire值必须为字符串，不强转")
        if self.tag == "integer":
            _require(
                re.fullmatch(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)", self.value) is not None,
                "SQL INTEGER不是规范十进制",
            )
            digits = self.value.removeprefix("-")
            bound = str(-_SQL_MIN if self.value.startswith("-") else _SQL_MAX)
            _require(
                len(digits) < len(bound) or (len(digits) == len(bound) and digits <= bound),
                "SQL INTEGER超出signed64",
            )
        elif self.tag == "real":
            _require(
                re.fullmatch("[0-9a-f]{16}", self.value) is not None, "REAL必须为16位小写位模式"
            )
        else:
            _unbase64(self.value)

    def to_wire(self) -> list[str]:
        self.__post_init__()
        return ["null"] if self.tag == "null" else [self.tag, self.value]

    def to_dict(self) -> list[str]:
        return self.to_wire()

    @classmethod
    def from_wire(cls, raw: object) -> Self:
        _require(type(raw) is list, "SQL wire必须为array")
        _require(bool(raw) and type(raw[0]) is str, "SQL tag必须为严格字符串")
        if raw == ["null"]:
            return cls("null", None)
        _require(len(raw) == 2 and raw[0] != "null", "SQL tag/长度不符")
        return cls(raw[0], raw[1])


@dataclass(frozen=True)
class ColumnV1(ContractV1):
    ordinal: NonNegative
    name: str
    declared_type: str
    not_null: bool
    default_value: SqlValueV1
    primary_key_ordinal: NonNegative


@dataclass(frozen=True)
class ColumnRefV1(ContractV1):
    relation: str
    column: str


@dataclass(frozen=True)
class RelationDigestV1(ContractV1):
    name: str
    columns: tuple[ColumnV1, ...]
    definition: SqlValueV1
    row_count: NonNegative
    rows_sha256: Sha256


@dataclass(frozen=True)
class FamilyCoverageV1(ContractV1):
    family: str
    relations: tuple[str, ...]
    columns: tuple[ColumnRefV1, ...]
    material_paths: tuple[RelPath, ...]
    checks: tuple[str, ...]


@dataclass(frozen=True)
class ScopeManifestV1(ContractV1):
    version: Version
    mapping_version: Version
    schema_sha256: Sha256
    relations: tuple[RelationDigestV1, ...]
    coverage: tuple[FamilyCoverageV1, ...]
    materials: CopyInventoryV1


@dataclass(frozen=True)
class DatabaseBytesV1(ContractV1):
    size: NonNegative
    sha256: Sha256


@dataclass(frozen=True)
class BindingV1(ContractV1):
    version: Version
    registry_id: UUID
    batch_id: UUID
    final_root: NativePathV1
    final_database: NativePathV1
    root_identity: EntityIdentityV1
    database_identity: EntityIdentityV1
    saved_source_path: str
    source_aliases: tuple[str, ...]
    manifest: ManifestRefV1
    copy_inventory: CopyInventoryV1
    inventory_sha256: Sha256
    database_bytes: DatabaseBytesV1
    sqlite_scope: ScopeManifestV1
    scope_sha256: Sha256
    dependency_identities: tuple[DependencyRefV1, ...]


@dataclass(frozen=True)
class CleanupFileV1(ContractV1):
    path: RelPath
    identity: EntityIdentityV1
    size: NonNegative
    sha256: Sha256
    copy_entry: RelPath | None
    control_formal: RelPath | None
    control_expected_bytes_b64: Base64 | None


@dataclass(frozen=True)
class CleanupDirectoryV1(ContractV1):
    path: RelPath
    identity: EntityIdentityV1
    original_direct_children: tuple[Name, ...]


@dataclass(frozen=True)
class CleanupPlanV1(ContractV1):
    version: Version
    stage_observation: Literal["absent_before_stage_owned", "present"]
    stage: DirRefV1 | None
    parent: DirRefV1 | None
    observed_parent_chain: tuple[DirRefV1, ...]
    missing_parent_suffix: tuple[Name, ...]
    files: tuple[CleanupFileV1, ...]
    directories_postorder: tuple[CleanupDirectoryV1, ...]


@dataclass(frozen=True)
class DirectoryBarrierV1(ContractV1):
    directory_identity: EntityIdentityV1
    after_removed_path: RelPath


@dataclass(frozen=True)
class ReservedV1(RestorePayloadV1):
    plan: PlanInputsV1
    plan_sha256: Sha256
    copy_inventory: CopyInventoryV1
    inventory_sha256: Sha256
    placement: StablePlacementV1
    original_target: OriginalTargetV1
    stage_name: Name


@dataclass(frozen=True)
class StageOwnedV1(RestorePayloadV1):
    reserved_sha256: Sha256
    stage: DirRefV1
    parent: DirRefV1
    created_parent_chain: tuple[DirRefV1, ...]
    token_layout: ControlLayoutV1
    token_bytes_b64: Base64
    token_sha256: Sha256
    origin_layout: ControlLayoutV1


@dataclass(frozen=True)
class SealedHoldV1(RestorePayloadV1):
    stage_owned_sha256: Sha256
    future_root_identity: EntityIdentityV1
    future_database_identity: EntityIdentityV1
    binding: BindingV1
    binding_sha256: Sha256


@dataclass(frozen=True)
class PublishIntentV1(RestorePayloadV1):
    sealed_sha256: Sha256
    binding_sha256: Sha256
    placement: StablePlacementV1
    original_target: OriginalTargetV1
    future_root_identity: EntityIdentityV1
    future_database_identity: EntityIdentityV1
    origin_bytes_b64: Base64
    origin_sha256: Sha256


@dataclass(frozen=True)
class PublishedHoldV1(RestorePayloadV1):
    publish_intent_sha256: Sha256
    binding_sha256: Sha256
    final_root: DirRefV1
    final_database: EntityRefV1
    stage_absent: Literal[True]
    origin_sha256: Sha256


@dataclass(frozen=True)
class AbortIntentV1(RestorePayloadV1):
    from_event_sha256: Sha256
    from_kind: Literal["reserved", "stage_owned", "sealed_hold"]
    never_publish_intent: Literal[True]
    cleanup_plan: CleanupPlanV1
    cleanup_plan_sha256: Sha256


@dataclass(frozen=True)
class AbortedV1(RestorePayloadV1):
    abort_intent_sha256: Sha256
    cleanup_plan_sha256: Sha256
    accounted_paths: tuple[RelPath, ...]
    directory_barriers: tuple[DirectoryBarrierV1, ...]
    stable_parent: DirRefV1 | None
    absence_anchor_barrier: DirRefV1 | None
    stage_absent: Literal[True]
    original_target: OriginalTargetV1


@dataclass(frozen=True)
class RestoreEventV1(ContractV1):
    version: Version
    registry_id: UUID
    sequence: SequenceNumber
    batch_id: UUID
    previous_sha256: Sha256 | None
    kind: Phase
    payload: RestorePayloadV1


@dataclass(frozen=True)
class CapacityCheckV1(ContractV1):
    location: NativePathV1
    observed_entity: EntityIdentityV1
    available_bytes: NonNegative
    required_known_bytes: NonNegative
    metadata_only: bool


@dataclass(frozen=True)
class LegacyIntentV1(ContractV1):
    run_id: NonEmpty
    call_id: NonEmpty
    seq: SequenceNumber
    tool_id: NonEmpty


@dataclass(frozen=True)
class LegacyApprovalV1(ContractV1):
    approval_id: NonEmpty
    run_id: NonEmpty
    action: NonEmpty
    state: Literal["pending", "approved"]
    resources: NonEmpty
    expires_at: int


@dataclass(frozen=True)
class LegacyRunV1(ContractV1):
    run_id: NonEmpty
    task_id: NonEmpty
    state: str


@dataclass(frozen=True)
class LegacyLeaseV1(ContractV1):
    resource_key: NonEmpty
    run_id: NonEmpty
    owner_pid: int
    owner_instance_id: str
    expires_at: int


@dataclass(frozen=True)
class ProjectionTablesV1(ContractV1):
    approvals: Literal["present", "absent"]
    runs: Literal["present", "absent"]
    leases: Literal["present", "absent"]


@dataclass(frozen=True)
class LegacyReconciliation(ContractV1):
    version: Version
    pending_intents: tuple[LegacyIntentV1, ...]
    pending_approvals: tuple[LegacyApprovalV1, ...]
    non_terminal_runs: tuple[LegacyRunV1, ...]
    live_leases: tuple[LegacyLeaseV1, ...]
    projection_tables: ProjectionTablesV1
    complete: bool
    reconciliation_scope: Literal["legacy_summary_only"]

    def to_legacy_dict(self) -> dict[str, object]:
        result = self.to_dict()
        del result["version"]
        del result["reconciliation_scope"]
        return result


@dataclass(frozen=True)
class RestorePlanV1(ContractV1):
    version: Version
    inputs: PlanInputsV1
    plan_sha256: Sha256
    apply_supported: bool
    capacity_checks: tuple[CapacityCheckV1, ...]
    verification: BackupVerification
    legacy_reconciliation: LegacyReconciliation
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RestoreActionResultV1(ContractV1):
    batch_id: UUID
    phase: Phase
    held: bool
    data_root: NativePathV1
    plan_sha256: Sha256
    binding_sha256: Sha256 | None
    event_sha256: Sha256
    files_restored: NonNegative
    bytes_restored: NonNegative
    review_command: tuple[str, ...] | None


@dataclass(frozen=True)
class RestoreCauseV1(ContractV1):
    type: str
    message: str


@dataclass(frozen=True)
class RestoreFailureV1(ContractV1):
    category: ErrorCategory
    message: str
    batch_id: UUID | None
    phase: Phase | None
    plan_sha256: Sha256 | None
    retained_paths: tuple[NativePathV1, ...]
    next_commands: tuple[tuple[str, ...], ...]
    cause: RestoreCauseV1 | None


@dataclass(frozen=True)
class CheckV1(ContractV1):
    name: str
    passed: bool
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class NamedValueV1(ContractV1):
    name: str
    value: SqlValueV1


@dataclass(frozen=True)
class SourceRefV1(ContractV1):
    relation: str
    key: tuple[SqlValueV1, ...]
    column: str | None
    material_path: RelPath | None


@dataclass(frozen=True)
class BodySelectorV1(ContractV1):
    family: str
    key: tuple[SqlValueV1, ...]
    field: str

    def to_selector(self) -> str:
        return "v1:" + base64.urlsafe_b64encode(self.to_bytes()).decode("ascii").rstrip("=")

    @classmethod
    def from_selector(cls, selector: str) -> Self:
        _require(type(selector) is str and selector.startswith("v1:"), "未知selector版本")
        encoded = selector[3:]
        _require(
            bool(encoded) and re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is not None,
            "selector必须是无padding base64url",
        )
        try:
            raw = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        except (ValueError, binascii.Error) as exc:
            raise RestoreContractError("非法selector外壳") from exc
        result = cls.from_bytes(raw)
        _require(result.to_selector() == selector, "非规范selector编码")
        return result

    def matches(self, family: str, key: tuple[SqlValueV1, ...], field: str) -> bool:
        return self.family == family and self.key == key and self.field == field


@dataclass(frozen=True)
class BodyViewV1(ContractV1):
    field: str
    size: NonNegative
    sha256: Sha256
    selector: str
    text: str | None
    bytes_b64: Base64 | None


@dataclass(frozen=True)
class ReviewObjectV1(ContractV1):
    family: str
    key: tuple[SqlValueV1, ...]
    status: tuple[NamedValueV1, ...]
    sources: tuple[SourceRefV1, ...]
    bodies: tuple[BodyViewV1, ...]


@dataclass(frozen=True)
class LaterChangeV1(ContractV1):
    family: str
    key: tuple[SqlValueV1, ...]
    before_sha256: Sha256 | None
    after_sha256: Sha256 | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class RestoreReviewV1(ContractV1):
    batch_id: UUID
    phase: Literal["published_hold"]
    held: Literal[True]
    data_root: NativePathV1
    binding_sha256: Sha256
    scope_sha256: Sha256
    review_complete: bool
    checks: tuple[CheckV1, ...]
    objects: tuple[ReviewObjectV1, ...]
    known_later_changes: tuple[LaterChangeV1, ...]
    unverifiable: tuple[str, ...]
    later_forget_delete_history: Literal["completeness_unproven"]
    body_scopes: tuple[str, ...]


@dataclass(frozen=True)
class RestoreResponseV1(ContractV1):
    version: Version
    operation: Literal["restore-apply", "restore-resume", "restore-abort", "restore-review"]
    ok: bool
    result: RestoreActionResultV1 | RestoreReviewV1 | None
    error: RestoreFailureV1 | None


@dataclass(frozen=True)
class SqlRelationV1(ContractV1):
    name: str
    columns: tuple[ColumnV1, ...]
    definition: SqlValueV1
    rows: tuple[tuple[SqlValueV1, ...], ...]


@dataclass(frozen=True)
class SqlRelationRefV1(ContractV1):
    name: str
    columns: tuple[ColumnV1, ...]


@dataclass(frozen=True)
class SqlSchemaV1(ContractV1):
    user_version: SqlValueV1
    sqlite_schema_rows: tuple[tuple[SqlValueV1, ...], ...]
    relations: tuple[SqlRelationRefV1, ...]


@dataclass(frozen=True)
class NodeInfoV1(ContractV1):
    kind: Literal["file", "directory"]
    link_count: NonNegative
    size: NonNegative
    identity: EntityIdentityV1


@dataclass(frozen=True)
class ChildEntry(ContractV1):
    name: Name
    info: NodeInfoV1


@dataclass(frozen=True)
class LockKeyV1(ContractV1):
    kind: Literal["gate", "namespace", "entity"]
    value: str | EntityIdentityV1 | None

    def __post_init__(self) -> None:
        _require(
            type(self.kind) is str and self.kind in ("gate", "namespace", "entity"),
            "未知LockKey分支",
        )
        if self.kind == "gate":
            _require(self.value is None, "gate不附带值")
        elif self.kind == "namespace":
            _require(type(self.value) is str, "namespace必须是comparison_key字符串")
            _require(
                NativePathCodecV1().canonical_parts(self.value)[2] == self.value,
                "namespace必须是当前OS canonical comparison_key",
            )
        else:
            _convert(EntityIdentityV1, self.value)

    def to_wire(self) -> list[object]:
        self.__post_init__()
        return ["gate"] if self.kind == "gate" else [self.kind, _json_value(self.value)]

    def to_dict(self) -> list[object]:
        return self.to_wire()

    @classmethod
    def from_wire(cls, raw: object) -> Self:
        _require(type(raw) is list and bool(raw) and type(raw[0]) is str, "非法LockKey array")
        if raw == ["gate"]:
            return cls("gate", None)
        _require(len(raw) == 2 and raw[0] in ("namespace", "entity"), "LockKey tag/长度不符")
        value = EntityIdentityV1.from_dict(raw[1]) if raw[0] == "entity" else raw[1]
        return cls(raw[0], value)


_PAYLOADS = dict(
    zip(
        get_args(Phase),
        (
            ReservedV1,
            StageOwnedV1,
            SealedHoldV1,
            PublishIntentV1,
            PublishedHoldV1,
            AbortIntentV1,
            AbortedV1,
        ),
        strict=True,
    )
)
_SUM_TYPES = {
    EntityIdentityV1: (PosixEntityIdentityV1, WindowsEntityIdentityV1),
    OriginalTargetV1: (AbsentTargetV1, EmptyTargetV1),
    RestorePayloadV1: tuple(_PAYLOADS.values()),
}
CONTRACT_TYPES = {
    cls.__name__: cls
    for cls in tuple(globals().values())
    if isinstance(cls, type) and issubclass(cls, ContractV1) and cls is not ContractV1
}


def _convert(
    annotation: object,
    value: object,
    *,
    decode: bool = False,
    path_codec: NativePathCodecV1 | None = None,
) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        result = _convert(args[0], value, decode=decode, path_codec=path_codec)
        _metadata(result, args[1:])
        return result
    if origin is Literal:
        _require(
            any(type(value) is type(item) and value == item for item in args),
            f"值不在封闭集：{value!r}",
        )
        return value
    if origin in (Union, types.UnionType) or annotation in _SUM_TYPES:
        for candidate in _SUM_TYPES.get(annotation, args):
            try:
                return _convert(candidate, value, decode=decode, path_codec=path_codec)
            except RestoreContractError:
                pass
        raise RestoreContractError("值不符合任何已登记分支")
    if origin in (tuple, list):
        expected = list if decode or origin is list else tuple
        _require(type(value) is expected, f"字段必须是{expected.__name__}，不强转")
        _require(origin is list or (len(args) == 2 and args[1] is Ellipsis), "未知tuple类型")
        items = [_convert(args[0], item, decode=decode, path_codec=path_codec) for item in value]
        return tuple(items) if origin is tuple else items
    if origin is dict or annotation is Any:
        if origin is dict:
            _require(type(value) is dict and args[0] is str, "backup扩展字段必须为字符串键对象")
        _json_value(value)
        return value
    if annotation is type(None):
        _require(value is None, "字段必须为null")
        return None
    if annotation in (str, int, bool):
        _require(type(value) is annotation, f"字段必须是严格{annotation.__name__}")
        return value
    if annotation in (SqlValueV1, LockKeyV1):
        if decode:
            return annotation.from_wire(value)
        _require(type(value) is annotation, "字段必须为对应的严格wire契约值")
        value.__post_init__()
        return value
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if not decode:
            _require(type(value) is annotation, f"字段必须为{annotation.__name__}")
            _validate(value)
            return value
        fields = dataclasses.fields(annotation)
        _require(
            type(value) is dict and set(value) == {f.name for f in fields},
            f"{annotation.__name__}字段缺失/多余",
        )
        hints, values = _hints(annotation), {}
        for field in fields:
            field_type = hints[field.name]
            if annotation is RestoreEventV1 and field.name == "payload":
                _require(type(value["kind"]) is str and value["kind"] in _PAYLOADS, "未知事件kind")
                field_type = _PAYLOADS[value["kind"]]
            values[field.name] = _convert(
                field_type, value[field.name], decode=True, path_codec=path_codec
            )
        result = annotation(**values)
        if not isinstance(result, ContractV1):
            _validate(result)
        if isinstance(result, NativePathV1) and path_codec is not None:
            _require(
                path_codec.canonical_parts(result.text)
                == (
                    result.os,
                    result.segments,
                    result.comparison_key,
                ),
                "注入路径codec重算不符",
            )
        return result
    raise RestoreContractError(f"未登记的契约类型：{annotation}")


def _validate(value: object) -> None:
    if isinstance(value, (SqlValueV1, LockKeyV1)):
        value.__post_init__()
        return
    hints = _hints(type(value))
    for field in dataclasses.fields(value):
        _convert(hints[field.name], getattr(value, field.name))
    _semantics(value)


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise RestoreContractError("SQL/算法名称必须有保真UTF-8字节") from exc


def _ordered_unique(values: tuple, key=restore_json_v1) -> None:
    keys = tuple(key(item) for item in values)
    _require(keys == tuple(sorted(set(keys))), "集合必须唯一且按规定字节序排序")


def _path_key(path: tuple[str, ...], *, postorder: bool = False) -> tuple[bytes, ...]:
    parts = tuple(restore_json_v1(segment) for segment in path)
    return (*parts, b"\xff") if postorder else parts


def _parent_chain(chain: tuple[DirRefV1, ...]) -> None:
    _require(bool(chain), "既有祖先链不能为空")
    _require(len({entry.path.text for entry in chain}) == len(chain), "祖先链不能重复同一路径")
    for parent, child in zip(chain, chain[1:], strict=False):
        _require(os.path.dirname(child.path.text) == parent.path.text, "祖先链必须完整逐段相邻")


def _columns(columns: tuple[ColumnV1, ...]) -> None:
    _require(tuple(c.ordinal for c in columns) == tuple(range(len(columns))), "列ordinal不连续")
    _require(len({c.name for c in columns}) == len(columns), "列名称重复")


def _check_digest(label: str, value: object, expected: str) -> None:
    _require(domain_digest(label, value) == expected, f"{label}摘要与完整本体不符")


def _semantics(value: object) -> None:
    if isinstance(value, NativePathV1):
        _require(
            NativePathCodecV1().canonical_parts(value.text)
            == (
                value.os,
                value.segments,
                value.comparison_key,
            ),
            "路径OS/segments/comparison_key与当前OS重算不符",
        )
    elif isinstance(value, WindowsEntityIdentityV1):
        _require(
            re.fullmatch("[0-9a-f]{32}", value.file_id_hex) is not None,
            "Windows file identity必须完整128位",
        )
    elif isinstance(value, (DirRefV1, EntityRefV1, FileSnapshotV1)):
        _require(value.path.os == value.identity.os, "路径与实体身份OS不符")
    elif isinstance(value, StablePlacementV1):
        _parent_chain(value.existing_ancestors)
        expected = os.path.join(
            value.existing_ancestors[-1].path.text, *value.pending_parent_suffix, value.final_name
        )
        _require(value.canonical_root.text == expected, "placement根与祖先/未来父后缀不符")
    elif isinstance(value, CopyFileV1):
        _require(bool(value.path), "copy文件不能为stage根")
    elif isinstance(value, CopyInventoryV1):
        _ordered_unique(value.directories, _path_key)
        _ordered_unique(value.files, lambda item: restore_json_v1(item.path))
        directories, files = set(value.directories), {item.path for item in value.files}
        _require(
            () not in directories and () not in files and not directories & files,
            "inventory含stage根或文件/目录冲突",
        )
        for path in directories | files:
            _require(
                all(path[:n] in directories for n in range(1, len(path))),
                "inventory缺派生父目录或把文件作为父目录",
            )
    elif isinstance(value, ManifestRefV1):
        _require(
            hashlib.sha256(_unbase64(value.saved_bytes_b64)).hexdigest() == value.sha256,
            "manifest必须绑定完整原字节的裸SHA256",
        )
    elif isinstance(value, PlanInputsV1):
        _check_digest("inventory", value.copy_inventory, value.inventory_sha256)
        _require(
            (value.registry_id is None) == (value.registry_head_sha256 is None),
            "registry absence的ID/head必须同时null",
        )
        _ordered_unique(value.dependency_identities, lambda item: _utf8(item.name))
        if isinstance(value.original_target, EmptyTargetV1):
            _require(
                value.original_target.directory.path == value.placement.canonical_root,
                "原empty目标不是最终根",
            )
    elif isinstance(value, RegistryMetaV1):
        authority = os.path.join(value.home.path.text, ".tianshu", "restore-registry-v1")
        _require(
            value.authority_directory.path.text == authority
            and value.registry_database.path.text == os.path.join(authority, "registry.db"),
            "meta必须保存固定最终权威路径，不能保存临时路径",
        )
    elif isinstance(value, ControlLayoutV1):
        _require(
            bool(value.formal) and bool(value.temporary) and value.formal != value.temporary,
            "control需要不同的正式/临时相对路径",
        )
    elif isinstance(value, TokenV1):
        _require(
            os.path.dirname(value.stage.path.text) == value.parent.path.text,
            "token的stage不在固定P内",
        )
    elif isinstance(value, (RelationDigestV1, SqlRelationV1, SqlRelationRefV1)):
        _columns(value.columns)
        if isinstance(value, SqlRelationV1):
            _require(all(len(row) == len(value.columns) for row in value.rows), "SQL行列数不符")
            keys = tuple(restore_json_v1(row) for row in value.rows)
            _require(keys == tuple(sorted(keys)), "SQL rows未按规范bytes排序")
    elif isinstance(value, FamilyCoverageV1):
        _ordered_unique(value.relations, _utf8)
        _ordered_unique(value.columns, lambda item: (_utf8(item.relation), _utf8(item.column)))
        _ordered_unique(value.material_paths)
        _ordered_unique(value.checks, _utf8)
    elif isinstance(value, ScopeManifestV1):
        _ordered_unique(value.relations, lambda item: _utf8(item.name))
        _ordered_unique(value.coverage, lambda item: _utf8(item.family))
        relations = {item.name: {c.name for c in item.columns} for item in value.relations}
        material_paths = {f.path for f in value.materials.files} | set(value.materials.directories)
        for family in value.coverage:
            _require(all(name in relations for name in family.relations), "coverage含未知relation")
            _require(
                all(
                    ref.relation in relations and ref.column in relations[ref.relation]
                    for ref in family.columns
                ),
                "coverage含未知column",
            )
            _require(set(family.material_paths) <= material_paths, "coverage含inventory外材料")
    elif isinstance(value, BindingV1):
        _check_digest("inventory", value.copy_inventory, value.inventory_sha256)
        _check_digest("scope", value.sqlite_scope, value.scope_sha256)
        _require(value.sqlite_scope.materials == value.copy_inventory, "scope/copy inventory不符")
        _require(
            value.final_database.text == os.path.join(value.final_root.text, "events.db"),
            "Binding只接受标准events.db",
        )
        _require(
            value.final_root.os == value.root_identity.os == value.database_identity.os,
            "Binding路径/身份OS不符",
        )
        db = tuple(f for f in value.copy_inventory.files if f.path == ("events.db",))
        _require(
            len(db) == 1
            and db[0].size == value.database_bytes.size
            and db[0].sha256 == value.database_bytes.sha256,
            "DB物理字节与inventory不符",
        )
        _ordered_unique(value.dependency_identities, lambda item: _utf8(item.name))
    elif isinstance(value, CleanupFileV1):
        _require(bool(value.path), "cleanup文件不能为stage根")
        _require(
            (value.copy_entry is None) != (value.control_formal is None),
            "copy_entry/control_formal必须恰一非null",
        )
        if value.copy_entry is not None:
            _require(
                value.copy_entry == value.path and value.control_expected_bytes_b64 is None,
                "copy清理条目路径/控制字节不符",
            )
        else:
            _require(
                value.control_formal in ((".restore-token.json",), (".restore-origin.json",)),
                "未知控制文件不能纳入清理",
            )
            formal = value.control_formal[0]
            _require(
                value.path in ((formal,), (formal + ".tmp",))
                and value.control_expected_bytes_b64 is not None,
                "控制清理条目必须保留原正式路径与预期字节",
            )
            cls = TokenV1 if formal == ".restore-token.json" else OriginV1
            expected = _unbase64(value.control_expected_bytes_b64)
            cls.from_bytes(expected)
            _require(
                value.size <= len(expected)
                and value.sha256 == hashlib.sha256(expected[: value.size]).hexdigest(),
                "控制文件内容不是精确预期字节的合法前缀",
            )
            _require(
                value.path != (formal,) or value.size == len(expected),
                "正式控制文件不能是未完成的前缀",
            )
    elif isinstance(value, CleanupDirectoryV1):
        _ordered_unique(value.original_direct_children, restore_json_v1)
    elif isinstance(value, CleanupPlanV1):
        _cleanup_plan(value)
    elif isinstance(value, ReservedV1):
        _check_digest("plan", value.plan, value.plan_sha256)
        _require(
            value.copy_inventory == value.plan.copy_inventory
            and value.inventory_sha256 == value.plan.inventory_sha256
            and value.placement == value.plan.placement
            and value.original_target == value.plan.original_target,
            "reserved重复字段必须与完整plan一致",
        )
    elif isinstance(value, StageOwnedV1):
        _require(
            value.token_layout
            == ControlLayoutV1(
                (".restore-token.json",),
                (".restore-token.json.tmp",),
            )
            and value.origin_layout
            == ControlLayoutV1(
                (".restore-origin.json",),
                (".restore-origin.json.tmp",),
            ),
            "stage_owned不是固定v1控制布局",
        )
        token = TokenV1.from_bytes(_unbase64(value.token_bytes_b64))
        _check_digest("token", token, value.token_sha256)
        _require(
            token.stage == value.stage
            and token.parent == value.parent
            and token.reserved_sha256 == value.reserved_sha256,
            "token/stage_owned不符",
        )
        if value.created_parent_chain:
            _parent_chain(value.created_parent_chain)
            _require(value.created_parent_chain[-1] == value.parent, "新增父链未到P")
    elif isinstance(value, SealedHoldV1):
        _check_digest("binding", value.binding, value.binding_sha256)
        _require(
            value.future_root_identity == value.binding.root_identity
            and value.future_database_identity == value.binding.database_identity,
            "sealed未来实体与Binding不符",
        )
    elif isinstance(value, PublishIntentV1):
        origin = OriginV1.from_bytes(_unbase64(value.origin_bytes_b64))
        _check_digest("origin", origin, value.origin_sha256)
        _require(
            origin.sealed_sha256 == value.sealed_sha256
            and origin.binding_sha256 == value.binding_sha256,
            "origin/intent不符",
        )
    elif isinstance(value, AbortIntentV1):
        _check_digest("cleanup", value.cleanup_plan, value.cleanup_plan_sha256)
        if value.cleanup_plan.stage_observation == "absent_before_stage_owned":
            _require(value.from_kind == "reserved", "absent cleanup仅允许reserved前驱")
    elif isinstance(value, AbortedV1):
        _require(
            len(set(value.accounted_paths)) == len(value.accounted_paths), "accounted_paths重复"
        )
    elif isinstance(value, RestoreEventV1):
        _event_envelope(value)
    elif isinstance(value, CapacityCheckV1):
        _require(value.location.os == value.observed_entity.os, "容量位置/观察实体OS不符")
    elif isinstance(value, LegacyRunV1):
        from .tasks import NON_TERMINAL_STATES

        _require(value.state in NON_TERMINAL_STATES, "legacy非终态Run状态非法")
    elif isinstance(value, LegacyReconciliation):
        _require(
            not value.complete
            or all(v == "present" for v in dataclasses.astuple(value.projection_tables)),
            "投影缺失不能报告legacy complete",
        )
    elif isinstance(value, RestorePlanV1):
        _check_digest("plan", value.inputs, value.plan_sha256)
    elif isinstance(value, RestoreActionResultV1):
        _require(
            value.phase in ("published_hold", "aborted")
            and value.held == (value.phase == "published_hold"),
            "成功phase/held不符",
        )
        _require(not value.held or value.binding_sha256 is not None, "published必须有Binding")
    elif isinstance(value, RestoreFailureV1):
        _require(value.phase is None or value.batch_id is not None, "有phase必须有真实batch")
    elif isinstance(value, BodyViewV1):
        _require(
            BodySelectorV1.from_selector(value.selector).field == value.field,
            "正文selector字段不符",
        )
        _require(value.text is None or value.bytes_b64 is None, "正文不能同时披露text/bytes")
        raw = None
        if value.text is not None:
            raw = _utf8(value.text)
        elif value.bytes_b64 is not None:
            raw = _unbase64(value.bytes_b64)
            try:
                raw.decode("utf-8")
            except UnicodeError:
                pass
            else:
                raise RestoreContractError("可严格UTF-8解码的正文应使用text")
        if raw is not None:
            _require(
                len(raw) == value.size and hashlib.sha256(raw).hexdigest() == value.sha256,
                "正文与size/hash不符",
            )
    elif isinstance(value, ReviewObjectV1):
        _ordered_unique(value.status, lambda item: _utf8(item.name))
        _require(len({b.field for b in value.bodies}) == len(value.bodies), "正文槽位重复")
        for body in value.bodies:
            _require(
                BodySelectorV1.from_selector(body.selector).matches(
                    value.family,
                    value.key,
                    body.field,
                ),
                "selector未全等绑定对象槽位",
            )
    elif isinstance(value, RestoreReviewV1):
        _require(
            not value.review_complete
            or (not value.unverifiable and all(c.passed for c in value.checks)),
            "缺检查/来源不能报告完整review",
        )
        available = {b.selector for item in value.objects for b in item.bodies}
        for scope in value.body_scopes:
            _require(
                BodySelectorV1.from_selector(scope).to_selector() in available,
                "正文scope没有匹配同一scan槽位",
            )
        for item in value.objects:
            for body in item.bodies:
                _require(
                    (body.text is None and body.bytes_b64 is None)
                    or body.selector in value.body_scopes,
                    "未显式选择正文必须脱敏",
                )
    elif isinstance(value, RestoreResponseV1):
        _response(value)
    elif isinstance(value, SqlSchemaV1):
        _require(value.user_version.tag == "integer", "user_version必须是SQLite INTEGER")
        _require(
            all(len(row) == 5 for row in value.sqlite_schema_rows), "sqlite_schema必须保留真实五列"
        )
        _require(
            all(
                tuple(item.tag for item in row[:4]) == ("text", "text", "text", "integer")
                and row[4].tag in ("text", "null")
                for row in value.sqlite_schema_rows
            ),
            "sqlite_schema五列storage class与真实查询不符",
        )
        keys = tuple(restore_json_v1(row) for row in value.sqlite_schema_rows)
        _require(keys == tuple(sorted(keys)), "sqlite_schema rows未按规范bytes排序")
        _ordered_unique(value.relations, lambda item: _utf8(item.name))
    elif isinstance(value, (BackupManifest, BackupVerification, BackupFile)):
        for name in (
            "format_version",
            "db_user_version",
            "created_at_ms",
            "event_count",
            "event_seq_head",
            "files_checked",
            "bytes_checked",
            "size",
        ):
            number = getattr(value, name, None)
            _require(number is None or number >= 0, "既有backup整数字段必须非负")


def _cleanup_plan(value: CleanupPlanV1) -> None:
    _parent_chain(value.observed_parent_chain)
    if value.missing_parent_suffix:
        _require(value.parent is None, "父目录不存在时不能制造parent实体")
    else:
        _require(value.parent == value.observed_parent_chain[-1], "parent不是观察链最后真实P")
    if value.stage_observation == "absent_before_stage_owned":
        _require(
            value.stage is None and not value.files and not value.directories_postorder,
            "absent cleanup没有可删stage/文件/目录",
        )
        return
    _require(
        value.stage is not None and value.parent is not None and not value.missing_parent_suffix,
        "present cleanup必须有真实S/P",
    )
    _require(os.path.dirname(value.stage.path.text) == value.parent.path.text, "S/P路径不相邻")
    _ordered_unique(value.files, lambda item: restore_json_v1(item.path))
    controls = tuple(f.control_formal for f in value.files if f.control_formal is not None)
    _require(len(controls) == len(set(controls)), "同一控制文件的正式路径与tmp不能共存")
    _ordered_unique(value.directories_postorder, lambda item: _path_key(item.path, postorder=True))
    directories = {d.path: d for d in value.directories_postorder}
    files = {f.path for f in value.files}
    _require(
        () in directories and value.directories_postorder[-1].identity == value.stage.identity,
        "present cleanup必须以原stage根结尾",
    )
    _require(not files & directories.keys(), "cleanup文件/目录冲突")
    all_paths = files | directories.keys()
    for path in all_paths:
        _require(not path or path[:-1] in directories, "cleanup缺真实父目录")
    for path, directory in directories.items():
        children = tuple(
            sorted((p[-1] for p in all_paths if p and p[:-1] == path), key=restore_json_v1)
        )
        _require(
            directory.original_direct_children == children, "cleanup必须精确保存原直接子节点全集"
        )


def _event_envelope(event: RestoreEventV1) -> None:
    _require(type(event.payload) is _PAYLOADS[event.kind], "事件kind/typed payload不符")
    _require((event.sequence == 1) == (event.previous_sha256 is None), "首事件/previous不符")
    payload = event.payload
    if isinstance(payload, ReservedV1):
        _require(
            payload.stage_name == f".{payload.placement.final_name}.restore-{event.batch_id}",
            "stage_name没有绑定完整batch UUID",
        )
        _require(
            payload.plan.registry_id in (None, event.registry_id), "plan registry/envelope不符"
        )
    elif isinstance(payload, StageOwnedV1):
        token = TokenV1.from_bytes(_unbase64(payload.token_bytes_b64))
        _require(
            token.registry_id == event.registry_id and token.batch_id == event.batch_id,
            "token registry/batch与envelope不符",
        )
    elif isinstance(payload, SealedHoldV1):
        _require(
            payload.binding.registry_id == event.registry_id
            and payload.binding.batch_id == event.batch_id,
            "Binding/envelope不符",
        )
    elif isinstance(payload, PublishIntentV1):
        origin = OriginV1.from_bytes(_unbase64(payload.origin_bytes_b64))
        _require(
            origin.registry_id == event.registry_id and origin.batch_id == event.batch_id,
            "origin/envelope不符",
        )


def _response(value: RestoreResponseV1) -> None:
    if value.ok:
        _require(value.error is None and value.result is not None, "成功必须有真实result且无error")
        if value.operation == "restore-review":
            _require(
                type(value.result) is RestoreReviewV1 and value.result.review_complete,
                "成功review必须完整",
            )
        else:
            _require(type(value.result) is RestoreActionResultV1, "管理动作必须有action结果")
            if value.operation == "restore-apply":
                _require(value.result.phase == "published_hold", "apply成功必须published_hold")
            elif value.operation == "restore-abort":
                _require(value.result.phase == "aborted", "abort成功必须aborted")
    else:
        _require(value.error is not None, "失败必须带类型化原因")
        _require(
            value.result is None
            or (
                value.operation == "restore-review"
                and type(value.result) is RestoreReviewV1
                and not value.result.review_complete
            ),
            "失败只能附真实partial review，不能附成功action",
        )


def sql_value_from_sqlite(
    storage_class: str,
    value: object,
    *,
    text_bytes: bytes | None = None,
) -> SqlValueV1:
    """消费同一快照的真实typeof/value/CAST三元组，不从Python猜storage tag。"""
    _require(type(storage_class) is str, "typeof必须为字符串")
    if storage_class == "null":
        _require(value is None and text_bytes is None, "typeof null与值不符")
        return SqlValueV1("null", None)
    if storage_class == "integer":
        _require(type(value) is int and text_bytes is None, "typeof integer与值不符")
        return SqlValueV1("integer", str(value))
    if storage_class == "real":
        _require(type(value) is float and text_bytes is None, "typeof real与值不符")
        return SqlValueV1("real", struct.pack(">d", value).hex())
    if storage_class == "text":
        _require(
            type(value) in (str, bytes) and type(text_bytes) is bytes,
            "TEXT必须带同一快照CAST AS BLOB原字节",
        )
        return SqlValueV1("text", base64.b64encode(text_bytes).decode("ascii"))
    if storage_class == "blob":
        _require(type(value) is bytes and text_bytes is None, "typeof blob与值不符")
        return SqlValueV1("blob", base64.b64encode(value).decode("ascii"))
    raise RestoreContractError("未知SQLite typeof，不能降为TEXT或unknown")


def make_sql_relation(
    name: str,
    columns: tuple[ColumnV1, ...],
    definition: SqlValueV1,
    rows: tuple[tuple[SqlValueV1, ...], ...],
) -> SqlRelationV1:
    return SqlRelationV1(name, columns, definition, tuple(sorted(rows, key=restore_json_v1)))


def make_sql_schema(
    user_version: SqlValueV1,
    sqlite_schema_rows: tuple[tuple[SqlValueV1, ...], ...],
    relations: tuple[SqlRelationRefV1, ...],
) -> SqlSchemaV1:
    return SqlSchemaV1(
        user_version,
        tuple(sorted(sqlite_schema_rows, key=restore_json_v1)),
        tuple(sorted(relations, key=lambda relation: _utf8(relation.name))),
    )


def validate_event_chain(events: tuple[RestoreEventV1, ...]) -> None:
    """纯值全链验证，不把返回值当成Observation、head或文件访问许可。"""
    _require(type(events) is tuple, "全链必须是不可变事件tuple")
    _require(all(type(event) is RestoreEventV1 for event in events), "全链含非RestoreEventV1")
    previous_hash = None
    registry_id = events[0].registry_id if events else None
    batches: dict[str, dict[str, RestoreEventV1]] = {}
    for sequence, event in enumerate(events, 1):
        _require(type(event) is RestoreEventV1, "全链含非RestoreEventV1")
        _validate(event)
        _require(
            event.registry_id == registry_id
            and event.sequence == sequence
            and event.previous_sha256 == previous_hash,
            "全局registry/sequence/previous链不符",
        )
        history = batches.setdefault(event.batch_id, {})
        _require(event.kind not in history, "每kind每batch最多一条")
        predecessor = next(reversed(history.values())) if history else None
        expected = {
            "reserved": (None,),
            "stage_owned": ("reserved",),
            "sealed_hold": ("stage_owned",),
            "publish_intent": ("sealed_hold",),
            "published_hold": ("publish_intent",),
            "abort_intent": ("reserved", "stage_owned", "sealed_hold"),
            "aborted": ("abort_intent",),
        }[event.kind]
        _require((predecessor.kind if predecessor else None) in expected, "非法批次前驱")
        if predecessor is not None:
            _event_references(event, predecessor, history)
        history[event.kind] = event
        previous_hash = domain_digest("event", event)


def _event_references(
    event: RestoreEventV1,
    predecessor: RestoreEventV1,
    history: dict[str, RestoreEventV1],
) -> None:
    payload, prior = event.payload, predecessor.payload
    prior_hash = domain_digest("event", predecessor)
    reserved = history["reserved"].payload
    if isinstance(payload, StageOwnedV1):
        _require(payload.reserved_sha256 == prior_hash, "stage_owned未引用同批reserved")
        _require(
            payload.stage.path.text == os.path.join(payload.parent.path.text, reserved.stage_name),
            "stage_owned不是原约定唯一stage",
        )
        _require(
            payload.parent.path.text == os.path.dirname(reserved.placement.canonical_root.text),
            "stage_owned的P偏离placement",
        )
        expected_chain = (*reserved.placement.existing_ancestors, *payload.created_parent_chain)
        _parent_chain(expected_chain)
        _require(expected_chain[-1] == payload.parent, "新增父链与placement不符")
    elif isinstance(payload, SealedHoldV1):
        _require(
            payload.stage_owned_sha256 == prior_hash
            and payload.future_root_identity == prior.stage.identity,
            "sealed未绑定同一S",
        )
        binding = payload.binding
        _require(
            binding.final_root == reserved.placement.canonical_root
            and binding.copy_inventory == reserved.copy_inventory
            and binding.manifest == reserved.plan.manifest
            and binding.saved_source_path == reserved.plan.saved_source_path
            and binding.source_aliases == reserved.plan.source_aliases
            and binding.dependency_identities == reserved.plan.dependency_identities,
            "Binding偏离reserved完整输入",
        )
    elif isinstance(payload, PublishIntentV1):
        _require(
            payload.sealed_sha256 == prior_hash
            and payload.binding_sha256 == prior.binding_sha256
            and payload.future_root_identity == prior.future_root_identity
            and payload.future_database_identity == prior.future_database_identity
            and payload.placement == reserved.placement
            and payload.original_target == reserved.original_target,
            "publish_intent偏离同批sealed/placement/E",
        )
    elif isinstance(payload, PublishedHoldV1):
        binding = history["sealed_hold"].payload.binding
        _require(
            payload.publish_intent_sha256 == prior_hash
            and payload.binding_sha256 == prior.binding_sha256
            and payload.origin_sha256 == prior.origin_sha256
            and payload.final_root.path == binding.final_root
            and payload.final_root.identity == binding.root_identity
            and payload.final_database.path == binding.final_database
            and payload.final_database.identity == binding.database_identity,
            "published_hold未核对同一intent/S/D",
        )
    elif isinstance(payload, AbortIntentV1):
        _require(
            payload.from_event_sha256 == prior_hash and payload.from_kind == predecessor.kind,
            "abort_intent不是当前未发布前驱",
        )
        cleanup = payload.cleanup_plan
        fixed = reserved.placement.existing_ancestors
        _require(
            cleanup.observed_parent_chain[: len(fixed)] == fixed,
            "cleanup未保持reserved完整固定祖先",
        )
        parent_text = os.path.join(
            cleanup.observed_parent_chain[-1].path.text, *cleanup.missing_parent_suffix
        )
        _require(
            parent_text == os.path.dirname(reserved.placement.canonical_root.text),
            "cleanup观察链/缺失后缀偏离原P",
        )
        _require(
            all(
                not directory.path or directory.path in reserved.copy_inventory.directories
                for directory in cleanup.directories_postorder
            ),
            "cleanup目录不在原inventory，不能扩大清理范围",
        )
        if "stage_owned" in history:
            owned = history["stage_owned"].payload
            _require(
                cleanup.stage_observation == "present"
                and cleanup.stage == owned.stage
                and cleanup.parent == owned.parent,
                "已stage_owned不能洗成absent或另一S/P",
            )
            requires_token = any(
                item.copy_entry is not None or item.control_formal != owned.token_layout.formal
                for item in cleanup.files
            ) or any(directory.path for directory in cleanup.directories_postorder)
            _require(
                not requires_token
                or any(
                    item.path == item.control_formal == owned.token_layout.formal
                    for item in cleanup.files
                ),
                "首次cleanup已有payload/目录/origin时必须保留正式token",
            )
        elif cleanup.stage_observation == "present":
            _require(
                cleanup.stage.path.text == os.path.join(parent_text, reserved.stage_name)
                and not cleanup.files
                and len(cleanup.directories_postorder) == 1,
                "reserved下present仅允许原约定空S",
            )
        for item in cleanup.files:
            if item.copy_entry is not None:
                entries = tuple(
                    f for f in reserved.copy_inventory.files if f.path == item.copy_entry
                )
                _require(
                    len(entries) == 1 and item.size <= entries[0].size,
                    "cleanup文件不在inventory或超过完整声明大小",
                )
                _require(
                    item.size != entries[0].size or item.sha256 == entries[0].sha256,
                    "完整copy清理文件与原inventory字节不符",
                )
            else:
                owned = history.get("stage_owned")
                _require(owned is not None, "stage_owned之前不存在合法控制文件")
                if item.control_formal == (".restore-token.json",):
                    expected = owned.payload.token_bytes_b64
                else:
                    sealed = history.get("sealed_hold")
                    _require(sealed is not None, "sealed之前不存在合法origin")
                    origin = OriginV1(
                        1,
                        event.registry_id,
                        event.batch_id,
                        domain_digest("event", sealed),
                        sealed.payload.binding_sha256,
                    )
                    expected = base64.b64encode(origin.to_bytes()).decode("ascii")
                _require(
                    item.control_expected_bytes_b64 == expected,
                    "cleanup控制预期字节偏离原stage_owned/sealed事实",
                )
    elif isinstance(payload, AbortedV1):
        _require(
            payload.abort_intent_sha256 == prior_hash
            and payload.cleanup_plan_sha256 == prior.cleanup_plan_sha256
            and payload.original_target == reserved.original_target,
            "aborted未绑定同一abort_intent/cleanup/E",
        )
        cleanup = prior.cleanup_plan
        _require(payload.stable_parent == cleanup.parent, "aborted改变/虚构stable_parent")
        if cleanup.stage_observation == "absent_before_stage_owned":
            _require(
                not payload.accounted_paths
                and not payload.directory_barriers
                and payload.absence_anchor_barrier == cleanup.observed_parent_chain[-1],
                "absent aborted必须记录原最深真实祖先屏障且不声称删除",
            )
        else:
            _require(payload.absence_anchor_barrier is None, "present aborted不能改用absent屏障")
            paths = {f.path for f in cleanup.files} | {
                d.path for d in cleanup.directories_postorder
            }
            _require(set(payload.accounted_paths) == paths, "aborted未逐项结清原计划路径")
            directories = {d.path: d.identity for d in cleanup.directories_postorder}
            expected = {
                path: directories[path[:-1]] if path else cleanup.parent.identity for path in paths
            }
            actual = {
                barrier.after_removed_path: barrier.directory_identity
                for barrier in payload.directory_barriers
            }
            _require(
                len(actual) == len(payload.directory_barriers)
                and actual == expected
                and payload.directory_barriers[-1].after_removed_path == (),
                "aborted缺逐删除目录屏障或最后P屏障",
            )


_SCHEMA_CLASSES = {
    **CONTRACT_TYPES,
    **{cls.__name__: cls for cls in (BackupVerification, BackupManifest, BackupFile)},
}


def _schema_type(annotation: object) -> dict[str, object]:
    if annotation == Phase:
        return {"$ref": "#/$defs/Phase"}
    if annotation == ErrorCategory:
        return {"$ref": "#/$defs/ErrorCategory"}
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        base = _schema_type(args[0])
        for tag in args[1:]:
            if tag == "sha256":
                base["pattern"] = "^[0-9a-f]{64}$"
            elif tag == "uuid":
                base["pattern"] = "^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
                base["format"] = "uuid"
            elif tag == "base64":
                base["pattern"] = (
                    "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/][AQgw]==|"
                    "[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=)?$"
                )
            elif tag in ("nonnegative", "sequence"):
                base["minimum"] = 0 if tag == "nonnegative" else 1
                if tag == "sequence":
                    base["maximum"] = _SQL_MAX
            elif tag == "nonempty":
                base["minLength"] = 1
            elif tag == "name":
                base["pattern"] = r"^(?!\.{1,2}$)(?![A-Za-z]:)[^/\\\u0000]+$"
            elif tag == "relpath":
                base["items"] = _schema_type(Name)
        return base
    if origin is Literal:
        literal_type = (
            "boolean"
            if type(args[0]) is bool
            else ("integer" if type(args[0]) is int else "string")
        )
        return {"type": literal_type, "enum": list(args)}
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema_type(item) for item in args]}
    if origin in (tuple, list):
        return {"type": "array", "items": _schema_type(args[0])}
    if origin is dict:
        return {"type": "object", "additionalProperties": {"$ref": "#/$defs/JsonValue"}}
    if annotation is Any:
        return {"$ref": "#/$defs/JsonValue"}
    if annotation is type(None):
        return {"type": "null"}
    if annotation in (str, int, bool):
        return {"type": {str: "string", int: "integer", bool: "boolean"}[annotation]}
    if isinstance(annotation, type) and annotation.__name__ in _SCHEMA_CLASSES:
        return {"$ref": f"#/$defs/{annotation.__name__}"}
    raise RestoreContractError(f"schema未登记类型：{annotation}")


def contract_schema() -> dict[str, object]:
    """真实schema与类型对账；JSON Schema不替代canonical/引用等语义校验。"""
    definitions: dict[str, object] = {
        "Phase": {"type": "string", "enum": list(get_args(Phase))},
        "ErrorCategory": {"type": "string", "enum": list(get_args(ErrorCategory))},
        "JsonValue": {
            "anyOf": [
                {"type": "null"},
                {"type": "boolean"},
                {"type": "integer"},
                {"type": "string"},
                {"type": "array", "items": {"$ref": "#/$defs/JsonValue"}},
                {"type": "object", "additionalProperties": {"$ref": "#/$defs/JsonValue"}},
            ]
        },
    }
    for name, cls in _SCHEMA_CLASSES.items():
        if cls in _SUM_TYPES:
            definitions[name] = {"oneOf": [_schema_type(item) for item in _SUM_TYPES[cls]]}
        elif cls is LockKeyV1:
            definitions[name] = {
                "oneOf": [
                    {
                        "type": "array",
                        "prefixItems": [{"const": "gate"}],
                        "minItems": 1,
                        "maxItems": 1,
                    },
                    {
                        "type": "array",
                        "prefixItems": [{"const": "namespace"}, {"type": "string"}],
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    {
                        "type": "array",
                        "prefixItems": [{"const": "entity"}, _schema_type(EntityIdentityV1)],
                        "minItems": 2,
                        "maxItems": 2,
                    },
                ]
            }
        elif cls is SqlValueV1:
            branches = []
            for tag in ("null", "integer", "real", "text", "blob"):
                items = [{"const": tag}]
                if tag != "null":
                    item = {"type": "string"}
                    if tag == "integer":
                        item["pattern"] = r"^(?:0|-[1-9][0-9]*|[1-9][0-9]*)$"
                    elif tag == "real":
                        item["pattern"] = "^[0-9a-f]{16}$"
                    else:
                        item = _schema_type(Base64)
                    items.append(item)
                branches.append(
                    {
                        "type": "array",
                        "prefixItems": items,
                        "minItems": len(items),
                        "maxItems": len(items),
                    }
                )
            definitions[name] = {"oneOf": branches}
        else:
            properties = {
                f.name: _schema_type(_hints(cls)[f.name]) for f in dataclasses.fields(cls)
            }
            definitions[name] = {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            }
    definitions["RestoreEventV1"]["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": kind}}},
            "then": {"properties": {"payload": _schema_type(cls)}},
        }
        for kind, cls in _PAYLOADS.items()
    ]
    _schema_semantics(definitions)
    definitions["RestoreWireV1"] = {
        "anyOf": [_schema_type(cls) for cls in CONTRACT_TYPES.values() if cls not in _SUM_TYPES]
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "tianshu:restore:v1",
        "title": "Managed held restore v1",
        "description": (
            "Exact required nullable fields; no quotas. The strict Python codec additionally "
            "enforces canonical bytes, exact integer types (rejecting 1.0/bool), signed64 SQL "
            "integer strings, UUID/base64 canonicality, current-OS NativePath recomputation, "
            "collection order, complete hashes, phase/reference/cleanup and disclosure invariants. "
            "Backup extension dictionaries retain the actual BackupManifest shape. "
            "No serializable RegistryObservation or execution authority is defined."
        ),
        "$ref": "#/$defs/RestoreWireV1",
        "$defs": definitions,
    }


def _schema_semantics(definitions: dict[str, object]) -> None:
    """标准schema可表达的分支关系也拒绝；排序/字节/跨事件核验仍归纯parser。"""
    from .tasks import NON_TERMINAL_STATES

    definitions["WindowsEntityIdentityV1"]["properties"]["file_id_hex"]["pattern"] = (
        "^[0-9a-f]{32}$"
    )
    definitions["LegacyRunV1"]["properties"]["state"]["enum"] = sorted(NON_TERMINAL_STATES)
    definitions["BodyViewV1"]["properties"]["selector"]["pattern"] = "^v1:[A-Za-z0-9_-]+$"
    definitions["RestoreReviewV1"]["properties"]["body_scopes"]["items"]["pattern"] = (
        "^v1:[A-Za-z0-9_-]+$"
    )
    for name in ("CopyFileV1", "CleanupFileV1"):
        definitions[name]["properties"]["path"]["minItems"] = 1
    definitions["CopyInventoryV1"]["properties"]["directories"]["items"]["minItems"] = 1
    definitions["CleanupPlanV1"]["properties"]["observed_parent_chain"]["minItems"] = 1
    definitions["CleanupPlanV1"]["allOf"] = [
        {
            "if": {"properties": {"stage_observation": {"const": "absent_before_stage_owned"}}},
            "then": {
                "properties": {
                    "stage": {"type": "null"},
                    "files": {"maxItems": 0},
                    "directories_postorder": {"maxItems": 0},
                }
            },
            "else": {
                "properties": {
                    "stage": _schema_type(DirRefV1),
                    "parent": _schema_type(DirRefV1),
                    "missing_parent_suffix": {"maxItems": 0},
                    "directories_postorder": {"minItems": 1},
                }
            },
        },
        {
            "if": {"properties": {"missing_parent_suffix": {"minItems": 1}}},
            "then": {"properties": {"parent": {"type": "null"}}},
            "else": {"properties": {"parent": _schema_type(DirRefV1)}},
        },
    ]
    definitions["CleanupFileV1"]["oneOf"] = [
        {
            "properties": {
                "copy_entry": {"type": "array", "minItems": 1},
                "control_formal": {"type": "null"},
                "control_expected_bytes_b64": {"type": "null"},
            }
        },
        {
            "properties": {
                "copy_entry": {"type": "null"},
                "control_formal": {"enum": [[".restore-token.json"], [".restore-origin.json"]]},
                "control_expected_bytes_b64": _schema_type(Base64),
            }
        },
    ]
    definitions["PlanInputsV1"]["anyOf"] = [
        {"properties": {"registry_id": {"type": "null"}, "registry_head_sha256": {"type": "null"}}},
        {
            "properties": {
                "registry_id": _schema_type(UUID),
                "registry_head_sha256": _schema_type(Sha256),
            }
        },
    ]
    for name in ("token", "origin"):
        definitions["StageOwnedV1"]["properties"][f"{name}_layout"] = {
            "const": {
                "formal": [f".restore-{name}.json"],
                "temporary": [f".restore-{name}.json.tmp"],
            }
        }
    action = definitions["RestoreActionResultV1"]
    action["properties"]["phase"] = {"enum": ["published_hold", "aborted"]}
    action["allOf"] = [
        {
            "if": {"properties": {"phase": {"const": "published_hold"}}},
            "then": {
                "properties": {"held": {"const": True}, "binding_sha256": _schema_type(Sha256)}
            },
            "else": {"properties": {"held": {"const": False}}},
        }
    ]
    partial_review = {
        "allOf": [
            _schema_type(RestoreReviewV1),
            {
                "properties": {"review_complete": {"const": False}},
            },
        ]
    }
    response = definitions["RestoreResponseV1"]
    response["allOf"] = [
        {
            "if": {"properties": {"ok": {"const": True}}},
            "then": {
                "properties": {
                    "error": {"type": "null"},
                    "result": {
                        "anyOf": [
                            _schema_type(RestoreReviewV1),
                            _schema_type(RestoreActionResultV1),
                        ]
                    },
                }
            },
            "else": {
                "properties": {
                    "error": _schema_type(RestoreFailureV1),
                    "result": {"anyOf": [{"type": "null"}, partial_review]},
                }
            },
        },
        {
            "if": {"properties": {"ok": {"const": True}, "operation": {"const": "restore-review"}}},
            "then": {
                "properties": {
                    "result": {
                        "allOf": [
                            _schema_type(RestoreReviewV1),
                            {
                                "properties": {"review_complete": {"const": True}},
                            },
                        ]
                    }
                }
            },
        },
        {
            "if": {
                "properties": {
                    "operation": {
                        "enum": [
                            "restore-apply",
                            "restore-resume",
                            "restore-abort",
                        ]
                    }
                }
            },
            "then": {
                "if": {"properties": {"ok": {"const": True}}},
                "then": {"properties": {"result": _schema_type(RestoreActionResultV1)}},
                "else": {"properties": {"result": {"type": "null"}}},
            },
        },
    ]
    for operation, phase in (("restore-apply", "published_hold"), ("restore-abort", "aborted")):
        response["allOf"].append(
            {
                "if": {"properties": {"ok": {"const": True}, "operation": {"const": operation}}},
                "then": {"properties": {"result": {"properties": {"phase": {"const": phase}}}}},
            }
        )
    definitions["RestoreReviewV1"]["allOf"] = [
        {
            "if": {"properties": {"review_complete": {"const": True}}},
            "then": {
                "properties": {
                    "unverifiable": {"maxItems": 0},
                    "checks": {"items": {"properties": {"passed": {"const": True}}}},
                }
            },
        }
    ]
    definitions["SqlSchemaV1"]["properties"]["sqlite_schema_rows"]["items"] = {
        "type": "array",
        "minItems": 5,
        "maxItems": 5,
        "prefixItems": [
            {"allOf": [_schema_type(SqlValueV1), {"prefixItems": [{"enum": list(tags)}]}]}
            for tags in (("text",), ("text",), ("text",), ("integer",), ("text", "null"))
        ],
    }
    definitions["SqlSchemaV1"]["properties"]["user_version"] = {
        "allOf": [
            _schema_type(SqlValueV1),
            {"prefixItems": [{"const": "integer"}]},
        ]
    }
