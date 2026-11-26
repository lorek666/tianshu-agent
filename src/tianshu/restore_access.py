"""恢复 T1/B 普通连接、独立材料入口与批次操作的组合访问与双锚点隔离守卫。

固定顺序是 restore gate → canonical namespace → native entity → registry 短读/事务。
gate 只覆盖 anchor/身份固定窗口；namespace 与 entity access 从 SQLite 打开前一直持续到
真实 close 成功。外部 OS 锁是协作式准入，不把路径字符串当安全边界。材料守卫只核验
检查时的具体 target；同 uid 仍可在检查后替换目录链接，本轮不以 fd-relative/openat
建立物理封闭，也不把 canonical containment 冒充 TOCTOU 隔离。
"""

from __future__ import annotations

import base64
import os
import sqlite3
import stat
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .data_state import is_standard_db_basename
from .errors import TianshuError
from .maintenance import (
    DatabaseAccess,
    acquire_database_access,
    acquire_entity_access,
    acquire_restore_gate,
)
from .pathkeys import is_ancestor_or_self, overlap_key
from .platform_identity import (
    ManagedMainHandle,
    NativeEntityError,
    NativeEntityHandle,
    observe_entity,
    observe_entity_with_links,
    open_entity,
    open_managed_main,
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


def _scope_key(path: Path | str) -> str:
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return overlap_key(text)


def _target_scope_keys(path: Path) -> set[str]:
    keys = {_scope_key(path), _scope_key(path.parent)}
    try:
        keys.add(_scope_key(path.resolve(strict=False)))
    except (OSError, RuntimeError):
        pass
    return keys


def _in_scope(target: Path, root: Path) -> bool:
    for target_key in _target_scope_keys(target):
        if is_ancestor_or_self(_scope_key(root), target_key):
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
        actual_db_id, link_count = observe_entity_with_links(db_file, "file")
    except NativeEntityError as exc:
        raise RestoreRegistryCorrupt(
            f"已解除恢复根的主库 events.db 缺失或被替换：{db_file}"
        ) from exc
    if link_count != 1:
        raise RestoreRegistryCorrupt(f"已解除主库必须是单硬链接普通文件：{db_file}")

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
            if _scope_key(binding.final_database.text) in target_keys:
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
    _ensure_path_not_held(Path(path), registry, set())


def _ensure_path_not_held(
    path: Path,
    registry: RestoreRegistry | None,
    seen: set[str],
) -> None:
    if str(path) == ":memory:":
        return
    target = _absolute_target(path)
    target_key = _scope_key(target)
    if target_key in seen:
        return
    seen.add(target_key)
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

    # realpath(strict=False) is not a complete scope oracle: Windows raises
    # WinError 267 for a directory-kind leaf link aimed at an existing file,
    # before ntpath reaches its readlink fallback. Inspect explicit link targets
    # independently, including ancestors, before any caller checks exists().
    for entry in (target, *target.parents):
        try:
            is_link = entry.is_symlink() or entry.is_junction()
        except OSError as exc:
            raise RestoreAccessError(f"无法检查路径链接状态：{entry}") from exc
        if not is_link:
            continue
        try:
            linked = Path(os.readlink(entry))
        except (OSError, ValueError) as exc:
            raise RestoreAccessError(f"无法读取路径链接目标：{entry}") from exc
        if not linked.is_absolute():
            linked = entry.parent / linked
        _ensure_path_not_held(linked, reg, seen)


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
    database_handle: ManagedMainHandle | NativeEntityHandle | None
    directory_handles: tuple[NativeEntityHandle, ...]
    registry_observation: RegistryObservation
    has_released_proof: bool = False
    _closed: bool = field(default=False, init=False)
    _closing: bool = field(default=False, init=False)
    _borrows: int = field(default=0, init=False)
    _lifetime: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def active(self) -> bool:
        with self._lifetime:
            return not self._closed

    def __enter__(self) -> RestoreDatabaseAccess:
        with self._lifetime:
            if self._closing:
                raise RestoreAccessError("access已经关闭")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def verify(self) -> None:
        with self._lifetime:
            self._verify_locked()

    def _verify_locked(self) -> None:
        if self._closed:
            raise RestoreAccessError("access已经关闭")
        if self.database_handle is not None:
            self.database_handle.verify_path(self.path)
        for handle in self.directory_handles:
            handle.verify_path()

    def borrow_initialized(self) -> _MaterialWriteLease:
        return self._borrow_for_material(require_registered=True)

    def _borrow_for_material(self, *, require_registered: bool) -> _MaterialWriteLease:
        with self._lifetime:
            if self._closing:
                raise RestoreAccessError(f"所属 EventLog 未持有活跃连续准入锁：{self.path}")
            if require_registered:
                with _ACTIVE_EVENTLOG_LOCK:
                    registered = _ACTIVE_EVENTLOG_ACCESSES.get(id(self)) is self
                if not registered:
                    raise RestoreAccessError(f"所属 EventLog 尚未完成初始化与准入：{self.path}")
            try:
                if self.path.stat().st_size <= 0:
                    raise RestoreAccessError(f"所属 EventLog 主库为空/未初始化：{self.path}")
                self._verify_locked()
            except (NativeEntityError, OSError) as exc:
                raise RestoreAccessError(f"所属 EventLog 连续准入核验失败：{self.path}") from exc
            self._borrows += 1
        return _MaterialWriteLease(self, borrowed=True)

    def _release_borrow(self) -> None:
        with self._lifetime:
            self._borrows -= 1
            if self._closing and not self._borrows:
                self._close_locked()

    def close(self) -> None:
        with self._lifetime:
            self._closing = True
            unregister_admitted_eventlog(self)
            if not self._borrows:
                self._close_locked()

    def _close_locked(self) -> None:
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
    allow_create: bool = True,
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
    existing_identity = None
    if target.exists() or target.is_symlink():
        existing_identity = observe_entity(target, "file")
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

        missing = not target.exists() and not target.is_symlink()
        if missing:
            if not allow_create:
                raise RestoreAccessError(f"标准数据根缺少已初始化的非空 events.db：{target}")
            if prewrite_guard is not None:
                prewrite_guard()
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "posix":
                try:
                    descriptor = os.open(
                        target,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                except FileExistsError:
                    pass  # Another opener won O_EXCL; open its actual entity.
                else:
                    os.close(descriptor)

        database_handle = open_managed_main(target, create=missing)
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


def _material_directory_stat_identity(
    info: os.stat_result,
) -> tuple[int, int] | None:
    device = getattr(info, "st_dev", None)
    inode = getattr(info, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int) or inode == 0:
        return None
    return device, inode


def _material_directory_state(
    info: os.stat_result,
) -> tuple[tuple[int, int], int | None, int | None, int | None] | None:
    identity = _material_directory_stat_identity(info)
    if identity is None:
        return None
    return (
        identity,
        getattr(info, "st_mtime_ns", None),
        getattr(info, "st_ctime_ns", None),
        getattr(info, "st_size", None),
    )


type _MaterialDirectoryState = tuple[tuple[int, int], int | None, int | None, int | None]
type _MaterialDirectoryObservations = dict[
    Path, tuple[_MaterialDirectoryState, dict[str, _MaterialDirectoryState]]
]


def _material_tree_has_entries(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> bool:
    """Return true for material, links, unknown nodes, or any uninspectable path.

    Directory stat identities are race detectors only, not authorization identities.
    Keep the original descent checks, then re-enumerate every observed directory once:
    delayed directory metadata must not hide material added to an already scanned child.
    This is bounded cooperative observation, not an atomic filesystem snapshot.
    """
    observed_directories: _MaterialDirectoryObservations = {}
    if _scan_material_tree(
        path,
        expected_identity=expected_identity,
        observed_directories=observed_directories,
    ):
        return True
    try:
        for directory, (directory_state, observed_children) in observed_directories.items():
            before = os.lstat(directory)
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISDIR(before.st_mode)
                or directory.is_junction()
                or _material_directory_state(before) != directory_state
            ):
                return True
            final_children: dict[str, _MaterialDirectoryState] = {}
            with os.scandir(directory) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    info = os.lstat(child)
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or not stat.S_ISDIR(info.st_mode)
                        or child.is_junction()
                    ):
                        return True
                    child_state = _material_directory_state(info)
                    if child_state is None:
                        return True
                    final_children[entry.name] = child_state
            after = os.lstat(directory)
            if (
                final_children != observed_children
                or stat.S_ISLNK(after.st_mode)
                or not stat.S_ISDIR(after.st_mode)
                or directory.is_junction()
                or _material_directory_state(after) != directory_state
            ):
                return True
    except (OSError, RuntimeError, RecursionError):
        return True
    return False


def _scan_material_tree(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    observed_directories: _MaterialDirectoryObservations,
) -> bool:
    """Record each clean directory once while retaining the descent race checks."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if expected_identity is not None:
            return True
        # An absent legacy root is normally harmless, but it may be created between
        # the first observation and the scan. Re-observe before treating it as empty.
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True
    except OSError:
        return True

    if stat.S_ISLNK(info.st_mode):
        return True
    try:
        if path.is_junction():
            return True
    except (OSError, RuntimeError):
        return True
    if not stat.S_ISDIR(info.st_mode):
        return True
    identity = _material_directory_stat_identity(info)
    if identity is None or (
        expected_identity is not None and identity != expected_identity
    ):
        return True
    directory_state = _material_directory_state(info)
    if directory_state is None:
        return True

    try:
        observed_children: dict[
            str, tuple[tuple[int, int], int | None, int | None, int | None]
        ] = {}
        with os.scandir(path) as entries:
            for entry in entries:
                child = Path(entry.path)
                try:
                    # Windows DirEntry.stat() can omit the directory identity fields.
                    child_info = os.lstat(child)
                except OSError:
                    return True
                if stat.S_ISLNK(child_info.st_mode):
                    return True
                try:
                    if child.is_junction():
                        return True
                except (OSError, RuntimeError):
                    return True
                if not stat.S_ISDIR(child_info.st_mode):
                    return True
                child_state = _material_directory_state(child_info)
                if child_state is None:
                    return True
                observed_children[entry.name] = child_state
                if _scan_material_tree(
                    child,
                    expected_identity=child_state[0],
                    observed_directories=observed_directories,
                ):
                    return True

        try:
            final_info = os.lstat(path)
        except OSError:
            return True
        final_identity = _material_directory_stat_identity(final_info)
        final_state = _material_directory_state(final_info)
        if (
            stat.S_ISLNK(final_info.st_mode)
            or not stat.S_ISDIR(final_info.st_mode)
            or final_identity != identity
            or final_state != directory_state
        ):
            return True
        try:
            if path.is_junction():
                return True
        except (OSError, RuntimeError):
            return True

        # A direct child can appear after scandir has reached EOF. Re-list once and
        # compare directory identities so that such a late mutation is not mistaken
        # for an empty legacy material tree.
        final_children: dict[
            str, tuple[tuple[int, int], int | None, int | None, int | None]
        ] = {}
        with os.scandir(path) as entries:
            for entry in entries:
                child = Path(entry.path)
                try:
                    # Windows DirEntry.stat() can omit the directory identity fields.
                    child_info = os.lstat(child)
                except OSError:
                    return True
                if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(
                    child_info.st_mode
                ):
                    return True
                try:
                    if child.is_junction():
                        return True
                except (OSError, RuntimeError):
                    return True
                child_state = _material_directory_state(child_info)
                if child_state is None:
                    return True
                final_children[entry.name] = child_state
        if final_children != observed_children:
            return True
        observed_directories[path] = (directory_state, observed_children)
        return False
    except (OSError, RuntimeError, RecursionError):
        return True


def _legacy_artifacts_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    identity = _material_directory_stat_identity(info)
    if not stat.S_ISDIR(info.st_mode) or path.is_junction() or identity is None:
        raise OSError(f"旧版材料父目录不是可核对的普通目录：{path}")
    return identity


def preflight_legacy_material_layout(
    db_path: Path | str, data_root: Path | str | None = None
) -> None:
    """Refuse layouts whose historical material paths would no longer be read.

    Empty directory skeletons are harmless. This never moves, copies, or cleans up
    material; unreadable paths and links fail closed because emptiness is unknown.
    """
    if str(db_path) == ":memory:":
        return
    db = _absolute_target(Path(db_path))
    try:
        # Accept aliases of the data root itself, but do not resolve away a link
        # at a material directory whose interpretation changed.
        root = db.parent if data_root is None else Path(data_root).resolve(strict=False)
        current_snapshots, current_event_blobs, current_memory = default_material_paths(
            db, root
        )
    except (OSError, RuntimeError) as exc:
        raise RestoreAccessError(f"无法判定旧版材料布局路径，拒绝打开 {db}：{exc}") from exc
    old_artifacts = root / "artifacts"
    old_memory = db.parent / "memory_blobs"

    current_content_blobs = (
        current_snapshots / "blobs"
        if overlap_key(current_snapshots.name) == overlap_key("artifacts")
        else current_snapshots / "preimage_blobs"
    )
    legacy_candidates = [
        (old_artifacts / "snapshots", current_snapshots / "snapshots"),
        (old_artifacts / "undo", current_snapshots / "undo"),
        (old_artifacts / "blobs", current_content_blobs),
        (old_memory, current_memory),
    ]
    # Old separate-root events.db used its DB-parent blobs/; custom names did not.
    if (
        is_standard_db_basename(db.name)
        and overlap_key(str(root)) != overlap_key(str(db.parent))
    ):
        legacy_candidates.append((db.parent / "blobs", current_event_blobs))
    changed = [
        old for old, current in legacy_candidates
        if current is not None and overlap_key(str(old)) != overlap_key(str(current))
    ]

    # Missing children are not evidence of absence under a broken or replaced
    # parent. Check that parent without enumerating unrelated artifacts entries.
    artifact_children = [path for path in changed if path.parent == old_artifacts]
    populated = [
        path for path in changed
        if path.parent != old_artifacts and _material_tree_has_entries(path)
    ]
    if artifact_children:
        try:
            identity = _legacy_artifacts_identity(old_artifacts)
            if identity is not None:
                populated.extend(
                    path for path in artifact_children if _material_tree_has_entries(path)
                )
            if _legacy_artifacts_identity(old_artifacts) != identity:
                populated.append(old_artifacts)
        except (OSError, RuntimeError):
            populated.append(old_artifacts)
    if populated:
        locations = ", ".join(str(path) for path in populated)
        raise RestoreAccessError(
            "检测到旧版材料布局仍有材料或无法确认材料是否存在："
            f"{locations}。请保留原布局，先做离线备份，再用旧兼容版复核并完成未决事务；"
            "本版本不会迁移或移动这些材料。"
        )


def default_material_paths(
    db_path: Path | str,
    data_root: Path | str | None = None,
) -> tuple[Path, Path | None, Path | None]:
    """返回 (snapshots_root, event_blob_dir, memory_blob_dir)。

    仅当 db_path 名为 events.db 且 data_root 与 db_path.parent 同根时使用标准
    artifacts / blobs / memory_blobs 目录；非标准库名或异根 data_root 一律收口至
    私有非标准目录名，避免触碰任何标准数据根材料空间。
    """
    if str(db_path) == ":memory:":
        root = Path(data_root) if data_root is not None else Path(".")
        base = root / ".memory.materials"
        return base / "snapshots_root", None, None

    db = Path(db_path)
    db_parent = db.parent
    root = Path(data_root) if data_root is not None else db_parent
    is_std_db = is_standard_db_basename(db.name)
    same_root = overlap_key(str(root.resolve(strict=False))) == overlap_key(
        str(db_parent.resolve(strict=False))
    )
    if is_std_db and same_root:
        return root / "artifacts", db_parent / "blobs", db_parent / "memory_blobs"

    db_base = db_parent / f".{db.name}.materials"
    root_base = root / f".{db.name}.materials"
    ev_blobs = db_base / "event_blobs"
    mem_blobs = db_base / "mem_blobs"
    snap_root = root_base / "snapshots_root"
    return snap_root, ev_blobs, mem_blobs


def register_admitted_eventlog(db_path: Path, access: RestoreDatabaseAccess) -> None:
    """在 EventLog 完成只读准入与 schema 初始化后登记活跃 main 连续 access。"""
    if str(db_path) == ":memory:":
        return
    with access._lifetime:
        if not access._closing:
            with _ACTIVE_EVENTLOG_LOCK:
                _ACTIVE_EVENTLOG_ACCESSES[id(access)] = access


def unregister_admitted_eventlog(access: RestoreDatabaseAccess) -> None:
    with _ACTIVE_EVENTLOG_LOCK:
        _ACTIVE_EVENTLOG_ACCESSES.pop(id(access), None)


def _private_material_container(target: Path) -> Path | None:
    for entry in (target, *target.parents):
        name_key = overlap_key(entry.name)
        if (
            name_key.startswith(".")
            and name_key.endswith(".materials")
            and len(name_key) > len(".materials") + 1
        ):
            return entry
    return None


def _standard_material_root_and_domain(target: Path) -> tuple[Path, str] | None:
    for entry in (target, *target.parents):
        name_key = overlap_key(entry.name)
        parent_name_key = overlap_key(entry.parent.name)
        # Private containers are naming boundaries, not unrelated standard domains.
        if (
            name_key.startswith(".")
            and name_key.endswith(".materials")
            and len(name_key) > len(".materials") + 1
        ):
            return None
        if name_key == overlap_key("blobs") and parent_name_key == overlap_key("artifacts"):
            continue
        domains = {
            overlap_key("memory_blobs"): "memory_blobs",
            overlap_key("artifacts"): "artifacts",
            overlap_key("blobs"): "blobs",
        }
        domain = domains.get(name_key)
        if domain is not None:
            if entry.parent == entry:
                return None
            return entry.parent, domain
    return None


def _borrow_initialized_access_in_root(
    std_events_key: str,
) -> _MaterialWriteLease | None:
    with _ACTIVE_EVENTLOG_LOCK:
        snapshot = list(_ACTIVE_EVENTLOG_ACCESSES.values())
    for access in snapshot:
        try:
            if overlap_key(str(access.path.resolve(strict=True))) == std_events_key:
                return access.borrow_initialized()
        except (RestoreAccessError, OSError):
            continue
    return None


def _owner_material_lease(owner_log: Any) -> _MaterialWriteLease:
    db_path = getattr(owner_log, "db_path", None)
    if db_path is None or str(db_path) == ":memory:":
        return _NOOP_MATERIAL_LEASE
    ensure_path_not_held(db_path)
    conn = getattr(owner_log, "_conn", None)
    access = getattr(conn, "database_access", None)
    if not isinstance(access, RestoreDatabaseAccess):
        raise RestoreAccessError(f"所属 EventLog 未持有活跃连续准入锁：{db_path}")
    if overlap_key(str(_absolute_target(Path(db_path)))) != overlap_key(str(access.path)):
        raise RestoreAccessError(f"所属 EventLog 与持续准入的主库路径不符：{db_path}")
    return access.borrow_initialized()


def ensure_log_write_allowed(owner_log: Any) -> None:
    """Check the actual owner's initialized access for non-material callers."""
    with _owner_material_lease(owner_log):
        pass


_VERIFIED_MAIN_MATERIAL_SCOPES = threading.local()


class _MaterialWriteLease:
    """Pin an admitted owner or hold independently verified main access."""

    __slots__ = ("_access", "_borrowed", "_verified_main_key", "_scope")

    def __init__(
        self, access: RestoreDatabaseAccess | None = None, *, borrowed: bool = False,
        verified_main_key: str | None = None,
    ) -> None:
        self._access = access
        self._borrowed = borrowed
        self._verified_main_key = (
            (os.getpid(), verified_main_key) if verified_main_key is not None else None
        )
        self._scope: dict[tuple[int, str], RestoreDatabaseAccess] | None = None

    @property
    def active(self) -> bool:
        return self._access is not None and self._access.active

    def __enter__(self) -> _MaterialWriteLease:
        if self._verified_main_key is not None and self._access is not None:
            # Only this process and thread may reuse the verified exclusive main.
            # A forked child inherits the thread-local values but not this PID key.
            scope = getattr(_VERIFIED_MAIN_MATERIAL_SCOPES, "mains", None)
            if scope is None:
                scope = _VERIFIED_MAIN_MATERIAL_SCOPES.mains = {}
            scope[self._verified_main_key] = self._access
            self._scope = scope
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._scope is not None and self._verified_main_key is not None:
            self._scope.pop(self._verified_main_key, None)
            self._scope = None
        acc, self._access = self._access, None
        if acc is not None:
            if self._borrowed:
                acc._release_borrow()
            else:
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

    # An ownerless writer may have missed the active EventLog borrow just before
    # this fallback. Require exclusive admission so that race fails closed
    # instead of opening a second SQLite handle beside the live connection.
    access = acquire_restore_database_access(
        std_events_db,
        exclusive=True,
        prewrite_guard=lambda: check_new_file_evidence(std_events_db),
        allow_create=False,
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
    """守卫具体材料 target，并动态重验隔离范围与唯一标准主库连续准入。

    私有 ``.<db>.materials`` / ``.memory.materials`` 容器内的 target 解析后不得
    逃逸其 canonical 容器；标准材料域仍要求 raw/canonical 同根同域。这里是检查时
    的协作式边界，同 uid 在检查后换链的竞态仍需 fd-relative/openat 才能物理封闭。
    """
    if str(material_path) == ":memory:":
        return _NOOP_MATERIAL_LEASE
    ensure_path_not_held(material_path)

    target = Path(material_path).absolute()
    raw_located = _standard_material_root_and_domain(target)
    raw_private_container = _private_material_container(target)
    try:
        resolved_target = target.resolve(strict=False)
        if raw_private_container is not None:
            canonical_container = raw_private_container.resolve(strict=False)
            if not is_ancestor_or_self(
                overlap_key(str(canonical_container)),
                overlap_key(str(resolved_target)),
            ):
                raise RestoreAccessError(
                    "私有材料 target 解析后逃逸 canonical 容器："
                    f"{target} -> {resolved_target}"
                )
        located = _standard_material_root_and_domain(resolved_target)
        if raw_located is not None and (
            located is None
            or raw_located[1] != located[1]
            or overlap_key(str(raw_located[0].resolve(strict=False)))
            != overlap_key(str(located[0]))
        ):
            raise RestoreAccessError(
                "材料路径 canonical 解析后改变标准根/域，不能沿用原始同根 events.db 绑定："
                f"{target}"
            )
    except (OSError, RuntimeError) as exc:
        raise RestoreAccessError(f"无法解析材料写入目标路径：{target}") from exc
    if located is None:
        return _owner_material_lease(owner_log) if owner_log is not None else _NOOP_MATERIAL_LEASE
    standard_root, domain = located
    ensure_path_not_held(standard_root)

    std_events_db = standard_root / "events.db"
    std_events_key = overlap_key(str(_absolute_target(std_events_db)))
    if owner_log is not None:
        owner_db = getattr(owner_log, "db_path", None)
        if owner_db is None or str(owner_db) == ":memory:":
            raise RestoreAccessError(
                f"标准数据根材料写入不能使用 :memory: 连接：{target}"
            )
        resolved_owner_db = _absolute_target(Path(owner_db))
        if overlap_key(str(resolved_owner_db)) != std_events_key:
            raise RestoreAccessError(
                f"标准数据根材料写入必须绑定同根 events.db（收到 {owner_db}）：{target}"
            )
        return _owner_material_lease(owner_log)

    borrowed = _borrow_initialized_access_in_root(std_events_key)
    if borrowed is not None:
        return borrowed
    if domain in ("artifacts", "blobs"):
        scope = getattr(_VERIFIED_MAIN_MATERIAL_SCOPES, "mains", None)
        contained = None if scope is None else scope.get((os.getpid(), std_events_key))
        if contained is not None:
            return contained._borrow_for_material(require_registered=False)
        return _MaterialWriteLease(
            _acquire_verified_existing_main_access(std_events_db),
            verified_main_key=std_events_key,
        )
    raise RestoreAccessError(
        f"标准数据根材料写入必须绑定已初始化、已准入的 events.db 连续 access：{target}"
    )


__all__ = [
    "RestoreAccessError",
    "RestoreDatabaseAccess",
    "RestoreScopeBusy",
    "acquire_restore_database_access",
    "default_material_paths",
    "ensure_log_write_allowed",
    "ensure_material_write_allowed",
    "ensure_path_not_held",
    "preflight_legacy_material_layout",
    "register_admitted_eventlog",
    "unregister_admitted_eventlog",
    "verify_sqlite_main",
]
