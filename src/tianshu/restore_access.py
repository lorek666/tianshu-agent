"""恢复 T1 普通连接和批次操作的组合访问。

固定顺序是 restore gate → canonical namespace → native entity。gate 只覆盖
anchor/身份固定窗口；namespace 与 entity access 从 SQLite 打开前一直持续到真实
close 成功。外部 OS 锁是协作式准入，不把路径字符串当安全边界。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import TianshuError
from .maintenance import (
    DatabaseAccess,
    acquire_database_access,
    acquire_entity_access,
    acquire_restore_gate,
)
from .pathkeys import is_ancestor_or_self, overlap_key
from .platform_identity import (
    NativeEntityError,
    NativeEntityHandle,
    observe_entity,
    open_entity,
)
from .restore_contract import EntityIdentityV1, ReservedV1, SealedHoldV1
from .restore_registry import (
    AbsentRegistryObservation,
    PresentRegistryObservation,
    RegistryObservation,
    RestoreRegistry,
)


class RestoreAccessError(TianshuError):
    """组合准入无法形成连续、可解释的实体访问。"""


class RestoreScopeBusy(RestoreAccessError):
    """普通数据库落入某个尚未终结的恢复批次范围。"""


def _default_registry() -> RestoreRegistry:
    return RestoreRegistry()


_registry_factory: Callable[[], RestoreRegistry] = _default_registry


def _absolute_target(path: Path) -> Path:
    value = Path(path).absolute()
    try:
        parent = value.parent.resolve(strict=False)
        return parent / value.name
    except (OSError, RuntimeError) as exc:
        raise RestoreAccessError(f"目标路径不能规范化：{value}") from exc


def _target_scope_keys(path: Path) -> set[str]:
    keys = {overlap_key(str(path)), overlap_key(str(path.parent))}
    try:
        keys.add(overlap_key(str(path.resolve(strict=False))))
    except (OSError, RuntimeError):
        pass
    return keys


def _in_scope(target: Path, root: Path) -> bool:
    for target_key in _target_scope_keys(target):
        if is_ancestor_or_self(overlap_key(str(root)), target_key):
            return True
    return False


def _active_scope_blocks(
    observation: RegistryObservation,
    target: Path,
    existing_identity: EntityIdentityV1 | None,
) -> bool:
    if isinstance(observation, AbsentRegistryObservation):
        return False
    histories: dict[object, list] = {}
    for event in observation.events:
        histories.setdefault(event.batch_id, []).append(event)
    for events in histories.values():
        if events[-1].kind == "aborted":
            continue
        reserved = next((event for event in events if event.kind == "reserved"), None)
        if reserved is None or type(reserved.payload) is not ReservedV1:
            raise RestoreAccessError("registry活动批次缺少reserved本体")
        final_root = Path(reserved.payload.placement.canonical_root.text)
        parent = Path(reserved.payload.placement.canonical_root.text).parent
        stage = parent / reserved.payload.stage_name
        if _in_scope(target, final_root) or _in_scope(target, stage):
            return True
        for event in events:
            if type(event.payload) is SealedHoldV1:
                binding = event.payload.binding
                if overlap_key(binding.final_database.text) in _target_scope_keys(target):
                    return True
                if (
                    existing_identity is not None
                    and binding.database_identity == existing_identity
                ):
                    return True
    return False


def _revalidate_registry_under_gate(
    registry: RestoreRegistry,
    expected: RegistryObservation,
    target: Path,
    existing_identity: EntityIdentityV1 | None,
) -> RegistryObservation:
    """防止 gate 外的 absent/head 快照在准入线性化点之前过期。"""

    current: RegistryObservation = expected
    if isinstance(expected, PresentRegistryObservation):
        registry.revalidate(expected)
    else:
        candidate = registry.observe()
        if isinstance(candidate, PresentRegistryObservation):
            current = candidate
            registry.revalidate(candidate)
    if _active_scope_blocks(current, target, existing_identity):
        raise RestoreScopeBusy(f"数据库落入尚未终结的恢复范围：{target}")
    return current


def _nearest_existing_directory(path: Path) -> NativeEntityHandle:
    candidate = path
    while True:
        try:
            return open_entity(candidate, "directory")
        except NativeEntityError as exc:
            if candidate.parent == candidate:
                raise RestoreAccessError(f"目标没有可取得的稳定祖先：{path}") from exc
            candidate = candidate.parent


def _open_parent_chain(parent: Path) -> list[NativeEntityHandle]:
    resolved = parent.resolve(strict=True)
    chain = [resolved, *resolved.parents]
    handles: list[NativeEntityHandle] = []
    try:
        for path in reversed(chain):
            handles.append(open_entity(path, "directory"))
        return handles
    except BaseException:
        for handle in reversed(handles):
            handle.close()
        raise


@dataclass
class RestoreDatabaseAccess:
    """EventLog 专用连续 access；数据库实体句柄与 namespace/entity 锁同寿命。"""

    path: Path
    identity: EntityIdentityV1 | None
    namespace: DatabaseAccess
    entity: DatabaseAccess | None
    database_handle: NativeEntityHandle | None
    directory_handles: tuple[NativeEntityHandle, ...]
    registry_observation: RegistryObservation
    _closed: bool = False

    @property
    def active(self) -> bool:
        return not self._closed

    def __enter__(self) -> RestoreDatabaseAccess:
        if self._closed:
            raise RestoreAccessError("access已经关闭")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def verify(self) -> None:
        if self._closed:
            raise RestoreAccessError("access已经关闭")
        if self.database_handle is not None:
            self.database_handle.verify_path(self.path)
        for handle in self.directory_handles:
            handle.verify_path()

    def close(self) -> None:
        if self._closed:
            return
        if self.database_handle is not None:
            self.database_handle.close()
        for handle in reversed(self.directory_handles):
            handle.close()
        if self.entity is not None:
            self.entity.close()
        self.namespace.close()
        self._closed = True


def acquire_restore_database_access(
    db_path: Path,
    *,
    exclusive: bool = False,
    prewrite_guard: Callable[[], None] | None = None,
    registry: RestoreRegistry | None = None,
) -> RestoreDatabaseAccess:
    """固定普通连接所需的全部 access；所有锁均非阻塞。"""

    if str(db_path) == ":memory:":
        return RestoreDatabaseAccess(
            Path(":memory:"),
            None,
            DatabaseAccess(),
            None,
            None,
            (),
            AbsentRegistryObservation(
                False,
                Path(":memory:"),
                Path(":memory:"),
                None,
            ),
        )

    target = _absolute_target(db_path)
    registry = _registry_factory() if registry is None else registry
    observed_registry = registry.observe()
    existing_handle = None
    existing_identity = None
    if target.exists() or target.is_symlink():
        existing_handle = open_entity(target, "file")
        existing_identity = existing_handle.identity
        existing_handle.close()
        existing_handle = None
    if _active_scope_blocks(observed_registry, target, existing_identity):
        raise RestoreScopeBusy(f"数据库落入尚未终结的恢复范围：{target}")

    gate = acquire_restore_gate(exclusive=exclusive)
    namespace = None
    entity = None
    database_handle = None
    directory_handles: tuple[NativeEntityHandle, ...] = ()
    try:
        namespace = acquire_database_access(target, exclusive=exclusive)
        observed_registry = _revalidate_registry_under_gate(
            registry,
            observed_registry,
            target,
            existing_identity,
        )

        ancestor = _nearest_existing_directory(target.parent)
        ancestor_path = ancestor.path
        ancestor_identity = ancestor.identity
        ancestor.close()

        if not target.exists() and not target.is_symlink():
            if prewrite_guard is not None:
                prewrite_guard()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    target,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                # 同一 canonical namespace 的另一个普通写者赢得了 O_EXCL；继续
                # 打开赢家的真实实体，而不是创建第二份身份。
                pass
            else:
                os.close(descriptor)

        database_handle = open_entity(target, "file")
        if database_handle.link_count != 1:
            raise RestoreAccessError("受管数据库必须是单硬链接普通文件")
        entity = acquire_entity_access(database_handle.identity, exclusive=exclusive)
        database_handle.verify_path(target)
        directory_handles = tuple(_open_parent_chain(target.parent))
        if observe_entity(ancestor_path, "directory") != ancestor_identity:
            raise RestoreAccessError("目标稳定祖先在准入期间被替换")
        if prewrite_guard is not None:
            prewrite_guard()
        observed_registry = _revalidate_registry_under_gate(
            registry,
            observed_registry,
            target,
            database_handle.identity,
        )

        gate.close()
        gate = None
        return RestoreDatabaseAccess(
            target,
            database_handle.identity,
            namespace,
            entity,
            database_handle,
            directory_handles,
            observed_registry,
        )
    except BaseException:
        for handle in reversed(directory_handles):
            handle.close()
        if database_handle is not None:
            database_handle.close()
        if entity is not None:
            entity.close()
        if namespace is not None:
            namespace.close()
        if gate is not None:
            gate.close()
        raise


def verify_sqlite_main(
    connection: sqlite3.Connection,
    expected_path: Path,
    expected_identity: EntityIdentityV1 | None,
) -> None:
    """核对 SQLite 的 main 路径与锁定的真实文件实体。"""

    memory = str(expected_path) == ":memory:"
    row = next(
        (item for item in connection.execute("PRAGMA database_list") if item[1] == "main"),
        None,
    )
    if row is None or (memory and row[2] != ""):
        raise RestoreAccessError("SQLite :memory: main路径不符")
    if not memory and not row[2]:
        raise RestoreAccessError("SQLite没有真实main文件")
    if memory:
        if expected_identity is not None:
            raise RestoreAccessError("SQLite :memory:不得声明持久实体")
        return
    actual = Path(row[2])
    if overlap_key(str(actual.resolve())) != overlap_key(str(expected_path.resolve())):
        raise RestoreAccessError("SQLite database_list.main路径与准入目标不符")
    if expected_identity is None:
        return
    try:
        actual_identity = observe_entity(actual, "file")
    except NativeEntityError as exc:
        raise RestoreAccessError("SQLite main实体身份不可重核") from exc
    if actual_identity != expected_identity:
        raise RestoreAccessError("SQLite database_list.main实体与已锁实体不符")


__all__ = [
    "RestoreAccessError",
    "RestoreDatabaseAccess",
    "RestoreScopeBusy",
    "acquire_restore_database_access",
    "verify_sqlite_main",
]
