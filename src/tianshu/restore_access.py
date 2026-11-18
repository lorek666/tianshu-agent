"""恢复 T1/B 普通连接、独立材料入口与批次操作的组合访问与双锚点隔离守卫。

固定顺序是 restore gate → canonical namespace → native entity → registry 短读/事务。
gate 只覆盖 anchor/身份固定窗口；namespace 与 entity access 从 SQLite 打开前一直持续到
真实 close 成功。外部 OS 锁是协作式准入，不把路径字符串当安全边界。
"""

from __future__ import annotations

import base64
import os
import sqlite3
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from .restore_contract import (
    EntityIdentityV1,
    OriginV1,
    PublishedHoldV1,
    PublishIntentV1,
    ReleasedV1,
    ReleaseIntentV1,
    ReservedV1,
    RestoreContractError,
    SealedHoldV1,
    StageOwnedV1,
    TokenV1,
    domain_digest,
)
from .restore_registry import (
    AbsentRegistryObservation,
    PresentRegistryObservation,
    RegistryObservation,
    RestoreRegistry,
    RestoreRegistryCorrupt,
)


class RestoreAccessError(TianshuError):
    """组合准入无法形成连续、可解释的实体访问。"""


class RestoreScopeBusy(RestoreAccessError):
    """普通数据库或材料路径落入某个尚未终结/尚未解除隔离的恢复批次范围。"""


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


def _read_regular_single_link_bytes(path: Path, label: str) -> tuple[bytes, EntityIdentityV1]:
    try:
        handle = open_entity(path, "file")
    except NativeEntityError as exc:
        raise RestoreRegistryCorrupt(f"{label}不是普通非链接文件：{path}") from exc
    try:
        if handle.link_count != 1:
            raise RestoreRegistryCorrupt(f"{label}必须是单硬链接普通文件：{path}")
        identity = handle.identity
        raw = path.read_bytes()
        handle.verify_path(path)
        return raw, identity
    except OSError as exc:
        raise RestoreRegistryCorrupt(f"{label}无法读取：{path}") from exc
    finally:
        handle.close()


def _candidate_directories(target: Path) -> list[Path]:
    seen: set[str] = set()
    ordered: list[Path] = []
    probes = [target]
    try:
        resolved = target.resolve(strict=False)
        if str(resolved) != str(target):
            probes.append(resolved)
    except (OSError, RuntimeError):
        pass
    for probe in probes:
        chain = [probe, *probe.parents]
        for entry in chain:
            key = overlap_key(str(entry))
            if key in seen:
                continue
            try:
                if entry.is_symlink() or entry.is_junction() or not entry.is_dir():
                    continue
            except OSError:
                continue
            seen.add(key)
            ordered.append(entry)
    return ordered


def _live_ancestor_suffixes(
    target: Path,
) -> tuple[list[tuple[EntityIdentityV1, tuple[str, ...]]], set[EntityIdentityV1]]:
    pairs: list[tuple[EntityIdentityV1, tuple[str, ...]]] = []
    dir_identities: set[EntityIdentityV1] = set()
    probes = [target]
    try:
        resolved = target.resolve(strict=False)
        if str(resolved) != str(target):
            probes.append(resolved)
    except (OSError, RuntimeError):
        pass
    for probe in probes:
        current = probe
        suffix: list[str] = []
        while True:
            try:
                if not current.is_symlink() and not current.is_junction() and current.is_dir():
                    ident = observe_entity(current, "directory")
                    dir_identities.add(ident)
                    pairs.append((ident, tuple(reversed(suffix))))
            except (NativeEntityError, OSError):
                pass
            if current.parent == current:
                break
            suffix.append(current.name)
            current = current.parent
    return pairs, dir_identities


def _batch_histories(observation: RegistryObservation) -> dict[str, list]:
    if isinstance(observation, AbsentRegistryObservation):
        return {}
    histories: dict[str, list] = {}
    for event in observation.events:
        histories.setdefault(event.batch_id, []).append(event)
    return histories


def _placement_matches_target(
    reserved: ReservedV1,
    owned: StageOwnedV1 | None,
    ancestor_suffixes: list[tuple[EntityIdentityV1, tuple[str, ...]]],
) -> bool:
    placement = reserved.placement
    final_root = Path(placement.canonical_root.text)
    parent_path = final_root.parent
    ancestors = list(placement.existing_ancestors)
    if owned is not None:
        ancestors.extend(owned.created_parent_chain)
        ancestors.append(owned.parent)
    for anc_ref in ancestors:
        anc_path = Path(anc_ref.path.text)
        try:
            rel_to_parent = parent_path.relative_to(anc_path).parts
        except ValueError:
            continue
        expected_final = tuple(overlap_key(s) for s in (*rel_to_parent, placement.final_name))
        expected_stage = tuple(overlap_key(s) for s in (*rel_to_parent, reserved.stage_name))
        prefix_len = len(expected_final)
        for ident, suffix in ancestor_suffixes:
            if ident != anc_ref.identity or len(suffix) < prefix_len:
                continue
            actual_prefix = tuple(overlap_key(s) for s in suffix[:prefix_len])
            if actual_prefix in (expected_final, expected_stage):
                return True
    return False


def _verify_released_batch_live_entities(
    batch_events: list,
    live_root: Path,
) -> tuple[EntityIdentityV1, EntityIdentityV1]:
    tail = batch_events[-1]
    if tail.kind != "released":
        raise RestoreRegistryCorrupt("已解除批次缺少合法 released 尾事件")
    try:
        resolved_root = live_root.resolve(strict=True)
        actual_root_id = observe_entity(resolved_root, "directory")
        actual_parent_id = observe_entity(resolved_root.parent, "directory")
    except (NativeEntityError, OSError) as exc:
        raise RestoreRegistryCorrupt(f"已解除恢复根缺失或不可核验：{live_root}") from exc

    origin_path = resolved_root / ".restore-origin.json"
    raw_origin, _ = _read_regular_single_link_bytes(origin_path, ".restore-origin.json")

    db_file = resolved_root / "events.db"
    try:
        db_handle = open_entity(db_file, "file")
    except NativeEntityError as exc:
        raise RestoreRegistryCorrupt(
            f"已解除恢复根的主库 events.db 缺失或被替换：{db_file}"
        ) from exc
    try:
        if db_handle.link_count != 1:
            raise RestoreRegistryCorrupt(f"已解除主库必须是单硬链接普通文件：{db_file}")
        actual_db_id = db_handle.identity
    finally:
        db_handle.close()

    if type(tail.payload) is not ReleasedV1:
        sealed_ev = next((e for e in batch_events if e.kind == "sealed_hold"), None)
        final_root_ref = getattr(tail.payload, "final_root", None)
        final_db_ref = getattr(tail.payload, "final_database", None)
        root_ref_id = getattr(final_root_ref, "identity", None)
        db_ref_id = getattr(final_db_ref, "identity", None)
        try:
            origin = OriginV1.from_bytes(raw_origin)
        except (ValueError, RestoreContractError) as exc:
            raise RestoreRegistryCorrupt("已解除恢复根的 .restore-origin.json 损坏") from exc
        if (
            sealed_ev is None
            or type(sealed_ev.payload) is not SealedHoldV1
            or root_ref_id != actual_root_id
            or db_ref_id != actual_db_id
            or getattr(tail.payload, "binding_sha256", None) != origin.binding_sha256
            or getattr(tail.payload, "origin_sha256", None) != domain_digest("origin", origin)
        ):
            raise RestoreRegistryCorrupt("已解除恢复批次实体或来源摘要核验失败")
        return actual_root_id, actual_db_id

    released: ReleasedV1 = tail.payload
    rel_intent_ev = next((e for e in batch_events if e.kind == "release_intent"), None)
    intent_ev = next((e for e in batch_events if e.kind == "publish_intent"), None)
    if (
        rel_intent_ev is None
        or type(rel_intent_ev.payload) is not ReleaseIntentV1
        or intent_ev is None
        or type(intent_ev.payload) is not PublishIntentV1
    ):
        raise RestoreRegistryCorrupt("已解除批次缺少完整 publish_intent / release_intent 链")
    if actual_root_id != released.final_root.identity:
        raise RestoreRegistryCorrupt(f"已解除恢复根 S 实体身份已被替换：{live_root}")
    if overlap_key(str(resolved_root)) != released.final_root.path.comparison_key:
        raise RestoreRegistryCorrupt(f"已解除恢复根路径已偏离绑定最终位置：{live_root}")
    if actual_parent_id != rel_intent_ev.payload.stable_parent.identity:
        raise RestoreRegistryCorrupt(f"已解除恢复根稳定父目录 P 身份已变更：{resolved_root.parent}")

    expected_origin = base64.b64decode(intent_ev.payload.origin_bytes_b64)
    if raw_origin != expected_origin:
        raise RestoreRegistryCorrupt("已解除恢复根的 .restore-origin.json 字节已被篡改")
    if actual_db_id != released.final_database.identity:
        raise RestoreRegistryCorrupt(f"已解除主库 D 实体身份已被替换：{db_file}")
    return actual_root_id, actual_db_id


def _verify_local_witnesses(
    observation: RegistryObservation,
    target: Path,
    histories: dict[str, list],
) -> None:
    for cand in _candidate_directories(target):
        for tmp_name in (".restore-token.json.tmp", ".restore-origin.json.tmp"):
            tmp_path = cand / tmp_name
            try:
                has_tmp = tmp_path.is_symlink() or tmp_path.is_junction() or tmp_path.exists()
            except OSError as exc:
                raise RestoreRegistryCorrupt(f"恢复临时见证状态不可判定：{tmp_path}") from exc
            if has_tmp:
                try:
                    cand_id = observe_entity(cand, "directory")
                except NativeEntityError as exc:
                    raise RestoreRegistryCorrupt(f"恢复见证目录不可核验：{cand}") from exc
                for events in histories.values():
                    if events[-1].kind in (
                        "aborted",
                        "published_hold",
                        "release_intent",
                        "released",
                    ):
                        continue
                    owned_ev = next((e for e in events if e.kind == "stage_owned"), None)
                    if owned_ev is not None and owned_ev.payload.stage.identity == cand_id:
                        raise RestoreScopeBusy(f"路径处于未完成的恢复暂存范围：{target}")
                raise RestoreRegistryCorrupt(f"存在未登记或孤立的恢复临时见证：{tmp_path}")

        origin_path = cand / ".restore-origin.json"
        token_path = cand / ".restore-token.json"
        try:
            has_origin = (
                origin_path.is_symlink() or origin_path.is_junction() or origin_path.exists()
            )
            has_token = token_path.is_symlink() or token_path.is_junction() or token_path.exists()
        except OSError as exc:
            raise RestoreRegistryCorrupt(f"恢复控制见证状态不可判定：{cand}") from exc

        if has_origin:
            raw_origin, _ = _read_regular_single_link_bytes(origin_path, ".restore-origin.json")
            try:
                origin = OriginV1.from_bytes(raw_origin)
            except (ValueError, RestoreContractError) as exc:
                raise RestoreRegistryCorrupt(f".restore-origin.json 损坏：{origin_path}") from exc
            if isinstance(observation, AbsentRegistryObservation):
                raise RestoreRegistryCorrupt(
                    f"存在 .restore-origin.json 但固定 RestoreRegistry 缺失：{origin_path}"
                )
            if observation.meta.registry_id != origin.registry_id:
                raise RestoreRegistryCorrupt("OriginV1.registry_id 与固定 RestoreRegistry 不符")
            batch_events = histories.get(origin.batch_id)
            if not batch_events or batch_events[-1].kind == "aborted":
                raise RestoreRegistryCorrupt("OriginV1 指向缺失或已中止的恢复批次")
            sealed_ev = next((e for e in batch_events if e.kind == "sealed_hold"), None)
            if sealed_ev is None or type(sealed_ev.payload) is not SealedHoldV1:
                raise RestoreRegistryCorrupt("OriginV1 对应批次缺少 sealed_hold")
            if (
                domain_digest("event", sealed_ev) != origin.sealed_sha256
                or sealed_ev.payload.binding_sha256 != origin.binding_sha256
            ):
                raise RestoreRegistryCorrupt("OriginV1 摘要与 registry sealed_hold 不符")
            try:
                cand_id = observe_entity(cand, "directory")
            except NativeEntityError as exc:
                raise RestoreRegistryCorrupt(f"无法核验恢复根实体身份：{cand}") from exc
            if cand_id != sealed_ev.payload.future_root_identity:
                raise RestoreRegistryCorrupt("OriginV1 所在目录实体身份与 sealed_hold 不符")
            if has_token:
                raw_token, _ = _read_regular_single_link_bytes(token_path, ".restore-token.json")
                try:
                    token = TokenV1.from_bytes(raw_token)
                except (ValueError, RestoreContractError) as exc:
                    raise RestoreRegistryCorrupt(f".restore-token.json 损坏：{token_path}") from exc
                owned_ev = next((e for e in batch_events if e.kind == "stage_owned"), None)
                if (
                    owned_ev is None
                    or type(owned_ev.payload) is not StageOwnedV1
                    or base64.b64encode(raw_token).decode("ascii")
                    != owned_ev.payload.token_bytes_b64
                ):
                    raise RestoreRegistryCorrupt(".restore-token.json 与 stage_owned 绑定字节不符")
                _ = token
            if batch_events[-1].kind == "released":
                _verify_released_batch_live_entities(batch_events, cand)
                continue
            raise RestoreScopeBusy(
                f"数据根处于恢复持久隔离态（batch={origin.batch_id}, "
                f"phase={batch_events[-1].kind}）：{target}"
            )

        if has_token:
            raw_token, _ = _read_regular_single_link_bytes(token_path, ".restore-token.json")
            try:
                token = TokenV1.from_bytes(raw_token)
            except (ValueError, RestoreContractError) as exc:
                raise RestoreRegistryCorrupt(f".restore-token.json 损坏：{token_path}") from exc
            if isinstance(observation, AbsentRegistryObservation):
                raise RestoreRegistryCorrupt(
                    f"存在 .restore-token.json 但固定 RestoreRegistry 缺失：{token_path}"
                )
            if observation.meta.registry_id != token.registry_id:
                raise RestoreRegistryCorrupt("TokenV1.registry_id 与固定 RestoreRegistry 不符")
            batch_events = histories.get(token.batch_id)
            if not batch_events or batch_events[-1].kind in (
                "aborted",
                "published_hold",
                "release_intent",
                "released",
            ):
                raise RestoreRegistryCorrupt(
                    "存在孤立 .restore-token.json（缺少 origin 或批次已终结）"
                )
            owned_ev = next((e for e in batch_events if e.kind == "stage_owned"), None)
            if (
                owned_ev is None
                or type(owned_ev.payload) is not StageOwnedV1
                or base64.b64encode(raw_token).decode("ascii") != owned_ev.payload.token_bytes_b64
            ):
                raise RestoreRegistryCorrupt(".restore-token.json 与 stage_owned 绑定字节不符")
            try:
                cand_id = observe_entity(cand, "directory")
            except NativeEntityError as exc:
                raise RestoreRegistryCorrupt(f"无法核验暂存目录身份：{cand}") from exc
            if cand_id != owned_ev.payload.stage.identity:
                raise RestoreRegistryCorrupt(".restore-token.json 所在目录实体与 stage_owned 不符")
            raise RestoreScopeBusy(
                f"路径处于恢复暂存范围（batch={token.batch_id}, "
                f"phase={batch_events[-1].kind}）：{target}"
            )


def _active_scope_blocks(
    observation: RegistryObservation,
    target: Path,
    existing_identity: EntityIdentityV1 | None,
) -> bool:
    histories = _batch_histories(observation)
    _verify_local_witnesses(observation, target, histories)
    if isinstance(observation, AbsentRegistryObservation):
        return False

    ancestor_suffixes, dir_identities = _live_ancestor_suffixes(target)
    target_keys = _target_scope_keys(target)

    for events in histories.values():
        tail = events[-1]
        if tail.kind == "aborted":
            continue
        reserved_ev = next((event for event in events if event.kind == "reserved"), None)
        if reserved_ev is None or type(reserved_ev.payload) is not ReservedV1:
            raise RestoreRegistryCorrupt("registry活动批次缺少reserved本体")
        reserved = reserved_ev.payload
        owned_ev = next((event for event in events if event.kind == "stage_owned"), None)
        owned = (
            owned_ev.payload
            if owned_ev is not None and type(owned_ev.payload) is StageOwnedV1
            else None
        )
        sealed_ev = next((event for event in events if event.kind == "sealed_hold"), None)
        sealed = (
            sealed_ev.payload
            if sealed_ev is not None and type(sealed_ev.payload) is SealedHoldV1
            else None
        )

        final_root = Path(reserved.placement.canonical_root.text)
        stage = final_root.parent / reserved.stage_name
        matched = (
            _in_scope(target, final_root)
            or _in_scope(target, stage)
            or _placement_matches_target(reserved, owned, ancestor_suffixes)
        )
        if not matched and owned is not None and owned.stage.identity in dir_identities:
            matched = True
        if not matched and sealed is not None:
            binding = sealed.binding
            if overlap_key(binding.final_database.text) in target_keys:
                matched = True
            elif existing_identity is not None and binding.database_identity == existing_identity:
                matched = True

        if not matched:
            continue

        if tail.kind == "released":
            _verify_released_batch_live_entities(events, final_root)
            continue

        if tail.kind in ("published_hold", "release_intent"):
            intent_ev = next((e for e in events if e.kind == "publish_intent"), None)
            pub_ev = next((e for e in events if e.kind == "published_hold"), None)
            if (
                intent_ev is None
                or type(intent_ev.payload) is not PublishIntentV1
                or pub_ev is None
                or type(pub_ev.payload) is not PublishedHoldV1
            ):
                raise RestoreRegistryCorrupt(f"{tail.kind} 缺少 publish_intent 或 published_hold")
            expected_root_id = pub_ev.payload.final_root.identity
            live_root = final_root
            if not live_root.exists():
                for cand in _candidate_directories(target):
                    try:
                        if observe_entity(cand, "directory") == expected_root_id:
                            live_root = cand
                            break
                    except NativeEntityError:
                        continue
            try:
                actual_root_id = observe_entity(live_root, "directory")
            except NativeEntityError as exc:
                raise RestoreRegistryCorrupt(
                    f"registry 记录 {tail.kind} 但数据根缺失或被替换：{live_root}"
                ) from exc
            if actual_root_id != expected_root_id:
                raise RestoreRegistryCorrupt(f"{tail.kind} 数据根实体身份已被替换：{live_root}")
            origin_path = live_root / ".restore-origin.json"
            raw_origin, _ = _read_regular_single_link_bytes(origin_path, ".restore-origin.json")
            expected_origin = base64.b64decode(intent_ev.payload.origin_bytes_b64)
            if raw_origin != expected_origin:
                raise RestoreRegistryCorrupt(
                    f"{tail.kind} 的 .restore-origin.json 字节与 registry 绑定不符"
                )
        return True
    return False


def _has_released_database_proof(
    observation: RegistryObservation,
    target: Path,
    db_identity: EntityIdentityV1 | None,
) -> bool:
    if db_identity is None or isinstance(observation, AbsentRegistryObservation):
        return False
    origin_path = target.parent / ".restore-origin.json"
    try:
        if origin_path.is_symlink() or origin_path.is_junction() or not origin_path.is_file():
            return False
    except OSError:
        return False
    raw_origin, _ = _read_regular_single_link_bytes(origin_path, ".restore-origin.json")
    try:
        origin = OriginV1.from_bytes(raw_origin)
    except (ValueError, RestoreContractError) as exc:
        raise RestoreRegistryCorrupt(f".restore-origin.json 损坏：{origin_path}") from exc
    if observation.meta.registry_id != origin.registry_id:
        raise RestoreRegistryCorrupt("OriginV1.registry_id 与固定 RestoreRegistry 不符")
    histories = _batch_histories(observation)
    batch_events = histories.get(origin.batch_id)
    if (
        not batch_events
        or batch_events[-1].kind != "released"
        or type(batch_events[-1].payload) is not ReleasedV1
    ):
        return False
    _, verified_db_id = _verify_released_batch_live_entities(batch_events, target.parent)
    released: ReleasedV1 = batch_events[-1].payload
    return (
        verified_db_id == db_identity
        and overlap_key(str(target.resolve())) == released.final_database.path.comparison_key
    )


def ensure_path_not_held(
    path: Path | str,
    *,
    registry: RestoreRegistry | None = None,
) -> None:
    """独立材料/后台/维护入口的隔离守卫；不反向抢 restore_gate，遵守全局锁序。"""

    if str(path) == ":memory:":
        return
    target = _absolute_target(Path(path))
    reg = _registry_factory() if registry is None else registry
    observed = reg.observe()
    existing_identity: EntityIdentityV1 | None = None
    try:
        if not target.is_symlink() and not target.is_junction() and target.is_file():
            existing_identity = observe_entity(target, "file")
    except (NativeEntityError, OSError):
        existing_identity = None
    if _active_scope_blocks(observed, target, existing_identity):
        raise RestoreScopeBusy(f"路径落入尚未终结/尚未解除隔离的恢复范围：{target}")


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
    has_released_proof: bool = False
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
        unregister_admitted_eventlog(self)


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
        released_proof = _has_released_database_proof(
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
            has_released_proof=released_proof,
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


_ACTIVE_EVENTLOG_LOCK = threading.Lock()
_ACTIVE_EVENTLOG_ACCESSES: weakref.WeakValueDictionary[int, RestoreDatabaseAccess] = (
    weakref.WeakValueDictionary()
)
_STANDARD_EVENTS_MARKERS: tuple[str, ...] = (
    "events.db",
    "events.db-wal",
    "events.db-journal",
    "events.db-shm",
    "events.db.migration.json",
    ".migration.json",
    ".restore-origin.json",
    ".restore-origin.json.tmp",
    ".restore-token.json",
    ".restore-token.json.tmp",
)
_DISALLOWED_CUSTOM_MAIN_NAMES = frozenset({"custom.db", "other.db"})


def register_admitted_eventlog(db_path: Path, access: RestoreDatabaseAccess) -> None:
    """在 EventLog 完成只读准入与 schema 初始化后登记活跃 main 连续 access。"""
    if str(db_path) == ":memory:" or not access.active:
        return
    with _ACTIVE_EVENTLOG_LOCK:
        _ACTIVE_EVENTLOG_ACCESSES[id(access)] = access


def unregister_admitted_eventlog(access: RestoreDatabaseAccess) -> None:
    with _ACTIVE_EVENTLOG_LOCK:
        _ACTIVE_EVENTLOG_ACCESSES.pop(id(access), None)


def _standard_material_root_and_domain(target: Path) -> tuple[Path, str] | None:
    for entry in (target, *target.parents):
        name = entry.name
        if name == "blobs" and entry.parent.name == "artifacts":
            continue
        if name in ("memory_blobs", "artifacts", "blobs"):
            if entry.parent == entry:
                return None
            return entry.parent, name
    return None


def _has_standard_events_db_marker(root: Path) -> bool:
    for marker in _STANDARD_EVENTS_MARKERS:
        cand = root / marker
        try:
            if cand.is_symlink() or cand.is_junction() or cand.exists():
                return True
        except OSError:
            return True
    return False


def _active_initialized_accesses_in_root(root: Path) -> list[RestoreDatabaseAccess]:
    root_key = overlap_key(str(root.resolve(strict=False)))
    with _ACTIVE_EVENTLOG_LOCK:
        snapshot = list(_ACTIVE_EVENTLOG_ACCESSES.values())
    matched: list[RestoreDatabaseAccess] = []
    for acc in snapshot:
        if not acc.active or str(acc.path) == ":memory:":
            continue
        try:
            resolved_db = acc.path.resolve(strict=True)
            if overlap_key(str(resolved_db.parent)) != root_key:
                continue
            if resolved_db.name.lower() in _DISALLOWED_CUSTOM_MAIN_NAMES:
                continue
            if resolved_db.stat().st_size <= 0:
                continue
            acc.verify()
        except (RestoreAccessError, NativeEntityError, OSError):
            continue
        matched.append(acc)
    return matched


def ensure_log_write_allowed(owner_log: Any) -> None:
    """验证所属 EventLog 未落入隔离范围且仍持有本进程已初始化连续准入 access。"""
    db_path = getattr(owner_log, "db_path", None)
    if db_path is None or str(db_path) == ":memory:":
        return
    ensure_path_not_held(db_path)
    conn = getattr(owner_log, "_conn", None)
    access = getattr(conn, "database_access", None) if conn is not None else None
    if not isinstance(access, RestoreDatabaseAccess) or not access.active:
        raise RestoreAccessError(f"所属 EventLog 未持有活跃连续准入锁：{db_path}")
    with _ACTIVE_EVENTLOG_LOCK:
        registered = _ACTIVE_EVENTLOG_ACCESSES.get(id(access)) is access
    if not registered:
        raise RestoreAccessError(f"所属 EventLog 尚未完成初始化与准入：{db_path}")
    try:
        if Path(db_path).resolve(strict=True).stat().st_size <= 0:
            raise RestoreAccessError(f"所属 EventLog 主库为空/未初始化：{db_path}")
        access.verify()
    except (NativeEntityError, OSError) as exc:
        raise RestoreAccessError(f"所属 EventLog 连续准入核验失败：{db_path}") from exc


class _MaterialWriteLease:
    """独立材料写调用期间持有的连续准入租约；已有活跃 EventLog 时为零开销空租约。"""

    __slots__ = ("_access",)

    def __init__(self, access: RestoreDatabaseAccess | None = None) -> None:
        self._access = access

    def __enter__(self) -> _MaterialWriteLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        acc, self._access = self._access, None
        if acc is not None:
            acc.close()


_NOOP_MATERIAL_LEASE = _MaterialWriteLease(None)


def _acquire_verified_existing_main_access(std_events_db: Path) -> RestoreDatabaseAccess:
    """独立材料 API 在无进程内活跃 EventLog 时，仅对已存在非空且准入通过的 events.db 取锁。"""
    from contextlib import closing

    from .data_admission import check_connection, check_new_file_evidence
    from .data_state import AdmissionRefused

    try:
        if (
            std_events_db.is_symlink()
            or std_events_db.is_junction()
            or not std_events_db.is_file()
            or std_events_db.stat().st_size <= 0
        ):
            raise RestoreAccessError(
                f"标准数据根缺少已初始化的非空 events.db：{std_events_db}"
            )
    except OSError as exc:
        raise RestoreAccessError(f"无法检查标准主库状态：{std_events_db}") from exc

    access = acquire_restore_database_access(
        std_events_db,
        exclusive=False,
        prewrite_guard=lambda: check_new_file_evidence(std_events_db),
    )
    try:
        ro_uri = std_events_db.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(ro_uri, uri=True, timeout=5.0, isolation_level=None)) as conn:
            conn.execute("PRAGMA query_only=ON")
            verify_sqlite_main(conn, std_events_db, access.identity)
            snapshot = check_connection(
                conn,
                std_events_db,
                released_v2_admitted=access.has_released_proof,
            )
            if snapshot.is_new:
                raise RestoreAccessError(f"标准主库尚未初始化事件 schema：{std_events_db}")
        return access
    except BaseException as exc:
        access.close()
        if isinstance(exc, RestoreAccessError):
            raise
        if isinstance(exc, (AdmissionRefused, sqlite3.Error, OSError)):
            raise RestoreAccessError(
                f"标准主库未通过只读准入核验（{exc}）：{std_events_db}"
            ) from exc
        raise


def ensure_material_write_allowed(
    material_path: Path | str,
    *,
    owner_log: Any = None,
) -> _MaterialWriteLease:
    """三材料域每个写 API 统一写前守卫：动态重验隔离范围 + 绑定同根已初始化准入 main。"""
    if str(material_path) == ":memory:":
        return _NOOP_MATERIAL_LEASE
    ensure_path_not_held(material_path)
    if owner_log is not None:
        ensure_log_write_allowed(owner_log)

    target = _absolute_target(Path(material_path))
    located = _standard_material_root_and_domain(target)
    if located is None:
        return _NOOP_MATERIAL_LEASE
    standard_root, domain = located
    ensure_path_not_held(standard_root)

    active_in_root = _active_initialized_accesses_in_root(standard_root)
    std_events_db = (standard_root / "events.db").resolve(strict=False)
    std_events_key = overlap_key(str(std_events_db))
    std_root_key = overlap_key(str(standard_root.resolve(strict=False)))
    has_std_marker = _has_standard_events_db_marker(standard_root)
    requires_standard_events_db = domain == "memory_blobs" or has_std_marker

    if requires_standard_events_db:
        if owner_log is not None:
            owner_db = getattr(owner_log, "db_path", None)
            if owner_db is None or str(owner_db) == ":memory:":
                raise RestoreAccessError(
                    f"标准数据根材料写入不能使用 :memory: 连接：{target}"
                )
            resolved_owner_db = Path(owner_db).resolve(strict=False)
            if resolved_owner_db.name.lower() in _DISALLOWED_CUSTOM_MAIN_NAMES:
                raise RestoreAccessError(
                    f"标准数据根材料写入不能使用 custom.db / other.db 连接（{owner_db}）：{target}"
                )
            if domain == "memory_blobs":
                if overlap_key(str(resolved_owner_db)) != std_events_key:
                    raise RestoreAccessError(
                        f"标准根 memory_blobs 写入须绑定同根 events.db（{owner_db}）：{target}"
                    )
            elif overlap_key(str(resolved_owner_db.parent)) != std_root_key:
                # 允许 build_stack(EventLog(a/events.db), data_root=b) 仅当 b 无独立 events.db；
                # 此处 b 已有标准 events.db 标记，异根连接一律拒绝！
                raise RestoreAccessError(
                    f"标准数据根材料写入不能使用异根连接（{owner_db}）：{target}"
                )

        has_std_main = any(
            overlap_key(str(acc.path.resolve(strict=False))) == std_events_key
            for acc in active_in_root
        )
        if has_std_main:
            return _NOOP_MATERIAL_LEASE
        if owner_log is None and domain == "artifacts":
            return _MaterialWriteLease(_acquire_verified_existing_main_access(std_events_db))
        if owner_log is not None:
            # 同目录非 custom 辅助库（如 cli.db）旁侧有 events.db 时同样须先验证主库准入
            temp_acc = _acquire_verified_existing_main_access(std_events_db)
            temp_acc.close()
            return _NOOP_MATERIAL_LEASE
        raise RestoreAccessError(
            f"标准数据根材料写入必须绑定已初始化、已准入的 events.db 连续 access：{target}"
        )

    if owner_log is not None:
        owner_db = getattr(owner_log, "db_path", None)
        if owner_db is None or str(owner_db) == ":memory:":
            raise RestoreAccessError(f"材料目录写入不能使用 :memory: 连接：{target}")
        resolved_owner_db = Path(owner_db).resolve(strict=False)
        if resolved_owner_db.name.lower() in _DISALLOWED_CUSTOM_MAIN_NAMES:
            raise RestoreAccessError(
                f"材料目录写入不能使用 custom.db / other.db 连接（{owner_db}）：{target}"
            )
        owner_blob_dir = getattr(owner_log, "blob_dir", None)
        explicitly_configured_log_blob_dir = (
            owner_blob_dir is not None
            and overlap_key(str(_absolute_target(Path(owner_blob_dir))))
            == overlap_key(str(target))
            and not active_in_root
        )
        if (
            domain != "artifacts"
            and not explicitly_configured_log_blob_dir
            and overlap_key(str(resolved_owner_db.parent)) != std_root_key
        ):
            raise RestoreAccessError(
                f"材料写入连接（{owner_db}）与目标材料根不属于同一数据根：{target}"
            )
        return _NOOP_MATERIAL_LEASE

    if not active_in_root:
        raise RestoreAccessError(
            f"标准材料写入被拒绝（缺少同根已初始化、已准入 EventLog 连续 access）：{target}"
        )
    return _NOOP_MATERIAL_LEASE


__all__ = [
    "RestoreAccessError",
    "RestoreDatabaseAccess",
    "RestoreScopeBusy",
    "acquire_restore_database_access",
    "ensure_log_write_allowed",
    "ensure_material_write_allowed",
    "ensure_path_not_held",
    "register_admitted_eventlog",
    "unregister_admitted_eventlog",
    "verify_sqlite_main",
]
