"""独立第二读者复审阻断缺陷回归矩阵（DEFECT-1 / 2A / 2B / 2C / 3 + W15 Safe GC 融合）。

覆盖项：
1. [DEFECT-1] RestoreFailureV1 / RestoreResponseV1 运行时严格只接受 7 个 v1 Phase，拒绝
   release_intent / released；RestoreFailureV2 / RestoreResponseV2 接受全部 9 个 PhaseV2；
   contract_schema() 与 schemas/data/restore-v1.json、contract_schema_v2() 与
   schemas/data/restore-v2.json 逐字 100% 全等。
2. [DEFECT-2A] migrate.py 全部入口（_connect / read_schema_version / plan_migrations /
   read_state / resume_migration / assert_writable / migrate）在 sqlite3.connect 前强制
   隔离守卫，即便存在热回滚日志 events.db-journal 亦零触碰主库与伴随日志字节。
3. [DEFECT-2B] MetricsRecorder 与 RuntimeModelSlot（构造 + 预构造后写方法）对 reserved /
   published_hold 根零创建目录与文件，不污染 BindingV1；解除隔离后恢复可写。
4. [DEFECT-2C] SnapshotStore / JournalStore / MemoryProjection / ContentStore 每个写 API
   动态重验隔离守卫并绑定同根已初始化准入 events.db 连续 access：
   - 预构造实例在进入 reserved / published_hold 后写调用全部被拒；
   - 标准根尚无已初始化/活跃准入 events.db（缺失 / 0 字节 / 已关闭）时禁止先造材料；
   - 持 elsewhere/custom.db 或同根 custom.db 的连接禁止写标准材料（memory_blobs / blobs /
     artifacts）。
5. [DEFECT-3] restore-review --later-backup B2（及 --body-scope）产出的 review_sha256 与
   无可选参数全脱敏复核完全一致，可直接通过真子进程 restore-release --expect-review。
6. [W15 融合] tianshu data gc 在 reserved / published_hold 根上以 restore_held 退 2（0 删除、
   不破坏 BindingV1）；经 restore-release 解除隔离后的 v2 根（无 .migration.json、保留合法
   .restore-origin.json 与 .restore-token.json）真子进程 gc --dry-run 退 3、--approve 退 0。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from restore_test_support import record_reserved_batch, requires_managed_restore_writes
from support import CHILD_TEXT

from tianshu import backup, migrate, restore_access, restore_manager
from tianshu import snapshot as snapshot_mod
from tianshu.backup import create_backup
from tianshu.contents import ContentStore
from tianshu.data_gc import DAY_MS
from tianshu.disk_admission import DiskAdmission
from tianshu.events import MAX_INLINE_BYTES, EventLog
from tianshu.loop import build_stack, open_memory
from tianshu.maintenance import MaintenanceBusy, acquire_entity_access
from tianshu.memory import MemoryProjection
from tianshu.metrics import MetricsRecorder
from tianshu.model import ScriptedModel
from tianshu.platform_identity import observe_entity
from tianshu.recovery import _clean_orphan_blob
from tianshu.resources import MemoryScope
from tianshu.restore_access import (
    RestoreAccessError,
    RestoreScopeBusy,
    default_material_paths,
    ensure_material_write_allowed,
)
from tianshu.restore_contract import (
    PHASES,
    PHASES_V2,
    RestoreContractError,
    RestoreFailureV1,
    RestoreFailureV2,
    RestoreResponseV1,
    RestoreResponseV2,
    contract_schema,
    contract_schema_v2,
)
from tianshu.restore_registry import RestoreRegistry
from tianshu.settings_control import RuntimeModelSlot
from tianshu.snapshot import SnapshotStore
from tianshu.undo_journal import ACTION_DELETE, JournalItem, JournalStore
from tianshu.workspace import Workspace, WorkspaceBinding

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
STARTUP = str(ROOT / "tests" / "_startup")
SAMPLE_UUID = "11111111-2222-4333-8444-555555555555"


def _child_env(*, failpoint: str | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
    }
    for passthrough in (
        "TIANSHU_TEST_LOCK_ROOT",
        "PYTEST_TIANSHU_REGISTRY_HOME",
        "TIANSHU_AUTHORITY_ROOT",
    ):
        val = os.environ.get(passthrough)
        if val:
            env[passthrough] = val
    env["PYTHONPATH"] = os.pathsep.join([STARTUP, SRC, env.get("PYTHONPATH", "")])
    if failpoint is not None:
        env["TIANSHU_FAILPOINT"] = failpoint
    return env


def _cli(*args: object, failpoint: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tianshu", *(str(a) for a in args)],
        env=_child_env(failpoint=failpoint),
        capture_output=True,
        timeout=45,
        **CHILD_TEXT,
    )


def _legacy_material_fixture(tmp_path, *, db_parent=None):
    from tianshu.contents import make_ref
    from tianshu.undo_journal import outstanding

    root = tmp_path / "legacy"
    ws = tmp_path / "workspace"
    ws.mkdir()
    db = root / "events.db"
    log = EventLog(db)
    try:
        stack = build_stack(log, data_root=root)
        legacy = stack.snapshots
        content = b"retained legacy content"
        change = legacy.record_preimage(
            "legacy-run", "call-1", "note.txt", content, "absent", root=ws
        )
        digest = hashlib.sha256(content).hexdigest()
        assert legacy.find("legacy-run", "call-1") == change
        assert stack.contents.record(digest, content, task_id="legacy-task")
        assert stack.contents.read(make_ref(digest), task_id="legacy-task").data == (
            content.decode()
        )
        legacy.journal.begin(
            "legacy-run", control_run="legacy-control",
            items=(JournalItem(
                root=str(ws.resolve()), path="created.txt", call_id="call-2",
                before_state="absent", expected_after="2" * 64, action=ACTION_DELETE,
            ),),
        )
        assert [item.run_id for item in outstanding(root, log=log)] == ["legacy-run"]
        mem_ref = stack.memory.put_content("retained legacy memory")
        assert mem_ref
    finally:
        log.close()
    return db, root, ws


@pytest.mark.parametrize("separate_root", [False, True], ids=["custom-db", "separate-root"])
def test_legacy_nonstandard_cli_refuses_without_touching_materials(tmp_path, separate_root):
    if separate_root:
        db, root, ws = _legacy_material_fixture(tmp_path)
        new_parent = tmp_path / "db-location"
        new_parent.mkdir()
        db = db.rename(new_parent / "events.db")
        (root / "memory_blobs").rename(new_parent / "memory_blobs")
    else:
        db, root, ws = _legacy_material_fixture(tmp_path)
        db = db.rename(root / "custom.db")

    def inventory():
        return {
            p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None
            for parent in (root, db.parent, ws)
            for p in parent.rglob("*")
        }

    before = inventory()
    for command in (
        ("task", "show", "--task-id", "missing-task"),
        ("recover", "--run-id", "legacy-run", "--workspace", ws),
    ):
        result = _cli(*command, "--db", db, "--data-root", root)
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "旧版材料布局" in result.stderr, result.stderr
        assert "离线备份" in result.stderr
        assert inventory() == before


@pytest.mark.parametrize(
    "command",
    [
        ("schedule", "list"),
        ("replay", "--run-id", "missing"),
    ],
    ids=["schedule-list", "missing-run-replay"],
)
def test_legacy_nonstandard_read_entrypoints_refuse_before_eventlog(
    tmp_path, command
):
    db, root, ws = _legacy_material_fixture(tmp_path)
    db = db.rename(root / "custom.db")

    def inventory():
        return {
            path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
            for parent in {root, db.parent, ws}
            for path in parent.rglob("*")
        }

    before = inventory()
    db_digest = hashlib.sha256(db.read_bytes()).hexdigest()
    result = _cli(*command, "--db", db)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "旧版材料布局" in result.stderr, result.stderr
    assert "离线备份" in result.stderr
    assert inventory() == before
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_digest
    assert not (root / ".custom.db.materials").exists()


def test_legacy_nonstandard_default_readers_do_not_report_empty(tmp_path):
    from tianshu.undo_journal import outstanding

    db, root, ws = _legacy_material_fixture(tmp_path)
    db = db.rename(root / "custom.db")
    log = EventLog(db)
    try:
        changes = log.conn.total_changes
        tables = tuple(log.conn.execute("SELECT name FROM sqlite_master ORDER BY name"))
        from tianshu.recovery import recover_run

        calls = (
            lambda: build_stack(log, data_root=root),
            lambda: open_memory(log, data_root=root),
            lambda: ContentStore(log, data_root=root),
            lambda: outstanding(root, log=log),
            lambda: recover_run(log, "legacy-run", ws, data_root=root),
        )
        for call in calls:
            with pytest.raises(RestoreAccessError, match="旧版材料布局"):
                call()
        assert log.conn.total_changes == changes
        assert tuple(log.conn.execute("SELECT name FROM sqlite_master ORDER BY name")) == tables
        assert not default_material_paths(db, root)[0].exists()
    finally:
        log.close()


def test_legacy_empty_nonstandard_scans_only_changed_locations(tmp_path):
    root = tmp_path / "data"
    db = tmp_path / "db" / "custom.db"
    for path in (
        root / "artifacts/snapshots/empty",
        root / "artifacts/undo",
        root / "artifacts/blobs",
        db.parent / "memory_blobs/empty",
        db.parent / "blobs",
    ):
        path.mkdir(parents=True)
    (root / "artifacts" / "unrelated").write_bytes(b"not read from a changed material location")
    (db.parent / "blobs" / "legacy-payload").write_bytes(b"event payload path is durable")
    result = _cli(
        "task", "create", "--goal", "smoke", "--workspace", tmp_path / "ws",
        "--db", db, "--data-root", root,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert default_material_paths(db, root)[0].is_dir()


def test_standard_db_with_separate_data_root_uses_private_material_paths(tmp_path):
    from tianshu.restore_access import preflight_legacy_material_layout

    db_parent = tmp_path / "dbroot"
    authority_root = tmp_path / "authority-root"
    db_parent.mkdir()
    authority_root.mkdir()
    db = db_parent / "events.db"
    db.touch()

    old_memory = db_parent / "memory_blobs"
    old_memory.mkdir()
    memory_marker = old_memory / "legacy-memory"
    memory_marker.write_bytes(b"preserve legacy memory")
    old_event_blobs = db_parent / "blobs"
    old_event_blobs.mkdir()
    event_marker = old_event_blobs / "legacy-event-payload"
    event_marker.write_bytes(b"preserve historical payload")

    snapshots_root, event_blobs, memory_blobs = default_material_paths(
        db, authority_root
    )
    db_materials = db_parent / ".events.db.materials"
    assert snapshots_root == authority_root / ".events.db.materials" / "snapshots_root"
    assert event_blobs == db_materials / "event_blobs"
    assert memory_blobs == db_materials / "mem_blobs"

    with pytest.raises(RestoreAccessError, match="旧版材料布局"):
        preflight_legacy_material_layout(db, authority_root)
    assert memory_marker.read_bytes() == b"preserve legacy memory"
    assert event_marker.read_bytes() == b"preserve historical payload"
    assert not db_materials.exists()

    memory_marker.unlink()
    event_marker.unlink()
    preflight_legacy_material_layout(db, authority_root)
    log = EventLog(db, blob_dir=event_blobs, data_root=authority_root)
    try:
        stack = build_stack(log)
        assert stack.data_root == authority_root
        memory_ref = stack.memory.put_content("new private memory")
        assert memory_ref is not None
        assert (memory_blobs / memory_ref).is_file()

        event_marker.write_bytes(b"preserve historical payload")
        oversized_payload = {
            "call_id": "call_private_blob",
            "tool_id": "fs.read",
            "status": "succeeded",
            "effect_state": "none_applied",
            "output": {"content": "X" * (MAX_INLINE_BYTES + 1024)},
        }
        log.append(
            "run-private-material-path",
            "tool.result",
            oversized_payload,
            actor_kind="system",
            legacy_write=True,
        )
        payload_ref = log.conn.execute(
            "SELECT payload_ref FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
        assert payload_ref is not None
        assert Path(payload_ref).parent == event_blobs
        assert Path(payload_ref).is_file()
        assert not list(old_memory.iterdir())
        assert [path.name for path in old_event_blobs.iterdir()] == [event_marker.name]
    finally:
        log.close()


@pytest.mark.parametrize("empty_main", [False, True], ids=["missing", "zero-byte"])
@pytest.mark.parametrize("separate_root", [False, True], ids=["custom-db", "separate-root"])
def test_legacy_eventlog_refuses_before_initializing_main(tmp_path, empty_main, separate_root):
    root = tmp_path / "data"
    db = (
        tmp_path / "db" / "events.db" if separate_root else root / "custom.db"
    )
    marker = root / "artifacts" / "snapshots" / "retained-preimage"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"retained legacy material")
    if empty_main:
        db.parent.mkdir(parents=True, exist_ok=True)
        db.touch()
    _, event_blobs, _ = default_material_paths(db, root)
    options = {"data_root": root} if separate_root else {}

    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(RestoreAccessError, match="旧版材料布局"):
        EventLog(db, blob_dir=event_blobs, **options).close()
    assert sorted(tmp_path.rglob("*")) == before
    assert marker.read_bytes() == b"retained legacy material"
    if empty_main:
        assert db.read_bytes() == b""


def test_legacy_preflight_refuses_dangling_artifacts_parent(tmp_path):
    artifacts = tmp_path / "artifacts"
    try:
        artifacts.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(RestoreAccessError, match="旧版材料布局"):
        restore_access.preflight_legacy_material_layout(tmp_path / "custom.db", tmp_path)
    assert artifacts.is_symlink()
    assert not (tmp_path / "missing-target").exists()


def test_legacy_preflight_keeps_canonical_same_root_layout(tmp_path):
    root = tmp_path / "data"
    material = root / "artifacts" / "snapshots" / "retained-preimage"
    material.parent.mkdir(parents=True)
    material.write_bytes(b"material with unchanged location")
    alias = tmp_path / "data-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    restore_access.preflight_legacy_material_layout(root / "events.db", alias)
    assert material.read_bytes() == b"material with unchanged location"


def test_legacy_nested_empty_tree_does_not_require_direntry_file_ids(tmp_path, monkeypatch):
    snapshots = tmp_path / "artifacts" / "snapshots"
    (snapshots / "empty-child" / "empty-grandchild").mkdir(parents=True)
    real_scandir = os.scandir

    class EntryWithoutWindowsFileId:
        def __init__(self, entry):
            self._entry = entry
            self.path = entry.path
            self.name = entry.name

        def stat(self, *, follow_symlinks=True):
            info = self._entry.stat(follow_symlinks=follow_symlinks)
            return os.stat_result((info.st_mode, 0, 0, *tuple(info)[3:]))

    @contextmanager
    def scandir(path):
        with real_scandir(path) as entries:
            if Path(path).is_relative_to(snapshots):
                yield (EntryWithoutWindowsFileId(entry) for entry in entries)
            else:
                yield entries

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        restore_access.preflight_legacy_material_layout(tmp_path / "custom.db", tmp_path)


def test_legacy_material_preflight_allows_initially_absent_locations(tmp_path):
    from tianshu.restore_access import preflight_legacy_material_layout

    db = tmp_path / "custom.db"
    db.touch()
    preflight_legacy_material_layout(db, tmp_path)


def test_legacy_material_scan_fails_closed_if_missing_root_appears_during_observation(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "artifacts" / "snapshots"
    original_lstat = restore_access.os.lstat
    injected = False

    def appear_after_missing_observation(path, *args, **kwargs):
        nonlocal injected
        if Path(path) == candidate and not injected:
            injected = True
            candidate.mkdir(parents=True)
            (candidate / "late-material").write_bytes(b"legacy")
            raise FileNotFoundError(str(path))
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(restore_access.os, "lstat", appear_after_missing_observation)
    assert restore_access._material_tree_has_entries(candidate)
    assert injected


def test_legacy_material_scan_fails_closed_on_entry_added_after_enumeration(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "artifacts" / "snapshots"
    candidate.mkdir(parents=True)
    monkeypatch.setattr(Path, "is_junction", lambda _path: False)
    original_lstat = restore_access.os.lstat
    observations = 0
    first_info = None

    def insert_after_enumeration(path, *args, **kwargs):
        nonlocal first_info, observations
        if Path(path) == candidate:
            observations += 1
            if observations == 1:
                first_info = original_lstat(path, *args, **kwargs)
                return first_info
            if observations == 2:
                (candidate / "late-material").write_bytes(b"legacy")
                assert first_info is not None
                return first_info
            return original_lstat(path, *args, **kwargs)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(restore_access.os, "lstat", insert_after_enumeration)
    assert restore_access._material_tree_has_entries(candidate) is True
    assert observations >= 2


def test_legacy_material_scan_fails_closed_on_nested_entry_added_after_descent(
    tmp_path, monkeypatch
):
    root = tmp_path / "artifacts" / "snapshots"
    child = root / "empty-child"
    child.mkdir(parents=True)
    monkeypatch.setattr(Path, "is_junction", lambda _path: False)
    original_lstat = restore_access.os.lstat
    root_observations = 0

    def insert_after_child_descent(path, *args, **kwargs):
        nonlocal root_observations
        if Path(path) == root:
            root_observations += 1
            if root_observations == 2:
                (child / "late-material").write_bytes(b"legacy")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(restore_access.os, "lstat", insert_after_child_descent)
    assert restore_access._material_tree_has_entries(root) is True
    assert root_observations >= 2


@pytest.mark.parametrize("mutation", ["remove", "replace"], ids=["missing", "replaced"])
def test_legacy_material_scan_fails_closed_on_child_identity_race(
    tmp_path, monkeypatch, mutation
):
    from tianshu.restore_access import preflight_legacy_material_layout

    db = tmp_path / "custom.db"
    db.touch()
    snapshots = tmp_path / "artifacts" / "snapshots"
    child = snapshots / "empty-child"
    child.mkdir(parents=True)
    original_scan = restore_access._material_tree_has_entries
    mutated = False

    def mutate_before_descent(path, *, expected_identity=None):
        nonlocal mutated
        if Path(path) == child and not mutated:
            mutated = True
            if mutation == "remove":
                child.rmdir()
            else:
                replacement = snapshots / "replacement"
                replacement.mkdir()
                child.rename(snapshots / "original")
                replacement.rename(child)
        return original_scan(Path(path), expected_identity=expected_identity)

    monkeypatch.setattr(restore_access, "_material_tree_has_entries", mutate_before_descent)
    with pytest.raises(RestoreAccessError, match="旧版材料布局"):
        preflight_legacy_material_layout(db, tmp_path)
    assert mutated


def _seed_standard_root(
    tmp_path: Path,
    name: str = "data_root",
    *,
    with_memory: str | None = None,
) -> Path:
    root = (tmp_path / name).resolve()
    ws = (tmp_path / f"ws_{name}").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        stack.tasks._disk_admission = DiskAdmission(
            stack.tasks.disk_admission.context,
            probe=lambda _p: 1 << 62,
        )
        task = stack.tasks.create_task(
            "复审回归种子任务",
            [WorkspaceBinding(str(ws), mode="writable")],
        )
        run_obj = stack.tasks.start_run(task.task_id)
        stack.tasks.transition(run_obj.run_id, "running", reason="seed")
        if with_memory is not None:
            ref = stack.memory.put_content(with_memory)
            payload = {
                "call_id": "call_mem_seed",
                "tool_id": "memory.remember",
                "status": "succeeded",
                "effect_state": "applied",
                "output": {
                    "memory_id": "mem_seed_1",
                    "revision": 1,
                    "scope": "user",
                    "scope_id": "local",
                    "kind": "preference",
                    "content_ref": ref,
                    "source_refs": ["event:1"],
                    "evidence": [{"type": "user_note", "detail": "seed"}],
                    "proof_count": 1,
                    "origin": "user_explicit",
                    "status": "active",
                    "created_at": 1_700_000_000_000,
                    "deduped": False,
                },
            }
            seq = log.append(
                run_obj.run_id,
                "tool.result",
                payload,
                task_id=task.task_id,
                actor_kind="human",
                legacy_write=True,
            )
            stack.memory.apply_event("tool.result", payload, seq=seq)
        stack.tasks.finish_run(
            run_obj.run_id,
            outcome="succeeded",
            effect_state="none_applied",
            reason="done",
        )
    finally:
        log.close()
    return root


# ================================================================ DEFECT-1


def test_defect_1_v1_closed_set_rejects_v2_phases_and_schemas_match_frozen_files() -> None:
    """DEFECT-1：RestoreFailureV1 / RestoreResponseV1 严拒 v2 阶段，且 v1/v2 schema 100% 全等。"""
    v1_file = json.loads((ROOT / "schemas" / "data" / "restore-v1.json").read_bytes())
    v2_file = json.loads((ROOT / "schemas" / "data" / "restore-v2.json").read_bytes())
    assert contract_schema() == v1_file
    assert contract_schema_v2() == v2_file

    # 正控：7 个 v1 Phase 全部允许构造与反序列化 RestoreFailureV1 / RestoreResponseV1
    for phase in sorted(PHASES):
        f1 = RestoreFailureV1(
            category="held",
            message="ok",
            batch_id=SAMPLE_UUID,
            phase=phase,
            plan_sha256=None,
            retained_paths=(),
            next_commands=(),
            cause=None,
        )
        r1 = RestoreResponseV1(
            version=1,
            operation="restore-apply",
            ok=False,
            result=None,
            error=f1,
        )
        assert RestoreResponseV1.from_bytes(r1.to_bytes()) == r1

    # 负控：v2 专属阶段 release_intent / released 必须在 RestoreFailureV1 与 RestoreResponseV1 抛错
    for v2_only in ("release_intent", "released"):
        with pytest.raises(RestoreContractError):
            RestoreFailureV1(
                category="held",
                message="must fail",
                batch_id=SAMPLE_UUID,
                phase=v2_only,
                plan_sha256=None,
                retained_paths=(),
                next_commands=(),
                cause=None,
            )

        raw_err = {
            "category": "held",
            "message": "must fail",
            "batch_id": SAMPLE_UUID,
            "phase": v2_only,
            "plan_sha256": None,
            "retained_paths": [],
            "next_commands": [],
            "cause": None,
        }
        with pytest.raises(RestoreContractError):
            RestoreFailureV1.from_dict(raw_err)
        with pytest.raises(RestoreContractError):
            RestoreResponseV1.from_dict(
                {
                    "version": 1,
                    "operation": "restore-apply",
                    "ok": False,
                    "result": None,
                    "error": raw_err,
                }
            )

        # 正控 v2：RestoreFailureV2 与 RestoreResponseV2 接受全部 9 阶段，to_v1() 拒绝 v2 专属阶段
        f2 = RestoreFailureV2.from_dict(raw_err)
        assert f2.phase == v2_only
        with pytest.raises(RestoreContractError):
            f2.to_v1()
        r2 = RestoreResponseV2(
            version=2,
            operation="restore-release",
            ok=False,
            result=None,
            error=f2,
        )
        assert RestoreResponseV2.from_bytes(r2.to_bytes()) == r2

    for phase in sorted(PHASES_V2):
        f2_all = RestoreFailureV2(
            category="restore_failed",
            message="v2",
            batch_id=SAMPLE_UUID,
            phase=phase,
            plan_sha256=None,
            retained_paths=(),
            next_commands=(),
            cause=None,
        )
        assert f2_all.phase == phase


# ================================================================ DEFECT-2A


@requires_managed_restore_writes
def test_defect_2a_migrate_entrypoints_blocked_before_sqlite_connect_and_preserve_hot_journal(
    tmp_path: Path,
) -> None:
    """DEFECT-2A：migrate 全部公开与内部连接入口均先调隔离守卫，零触发热日志回滚。"""
    root = _seed_standard_root(tmp_path, "mig_root")
    bkp = tmp_path / "bkp"
    create_backup(root, bkp)
    shutil.rmtree(root)

    # 1. reserved 阶段（目录尚不存在）：所有入口均抛 RestoreScopeBusy，不抛普通不存在错误
    planned = restore_manager.plan(bkp, root)
    crashed = _cli(
        "data",
        "restore-apply",
        bkp,
        "--data-root",
        root,
        "--expect-plan",
        planned.plan_sha256,
        failpoint="FP-RESERVED",
    )
    assert crashed.returncode == 77
    assert not root.exists()

    for fn in (
        lambda: migrate._connect(root / "events.db"),
        lambda: migrate.read_schema_version(root / "events.db"),
        lambda: migrate.plan_migrations(root),
        lambda: migrate.read_state(root),
        lambda: migrate.resume_migration(root),
        lambda: migrate.assert_writable(root),
        lambda: migrate.migrate(root, backup=bkp),
    ):
        with pytest.raises(RestoreScopeBusy):
            fn()
    assert not root.exists()

    # 推进至 published_hold
    resumed = restore_manager.resume(root)
    assert resumed.phase == "published_hold"

    # 2. 在独立副本上通过子进程硬退制造真实 SQLite 未提交 DELETE 模式热回滚日志 (-journal)，
    #    并拷入隔离根旁侧作为探针：若任何入口调了 sqlite3.connect(?mode=rw)，SQLite 将立即回滚并
    #    删除 events.db-journal 或改写主库字节！
    probe_dir = tmp_path / "journal_factory"
    probe_dir.mkdir()
    probe_db = probe_dir / "events.db"
    shutil.copy2(root / "events.db", probe_db)
    mk_journal_code = (
        "import os, sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "conn.execute('PRAGMA journal_mode=DELETE')\n"
        "conn.execute('PRAGMA synchronous=FULL')\n"
        "conn.execute('BEGIN EXCLUSIVE')\n"
        "conn.execute('CREATE TABLE hot_probe(x TEXT)')\n"
        "conn.execute(\"INSERT INTO hot_probe VALUES ('dirty')\")\n"
        "conn.execute('PRAGMA cache_size=1')\n"
        "for i in range(400):\n"
        "    conn.execute('INSERT INTO hot_probe VALUES (?)', ('X' * 1024,))\n"
        "os._exit(77)\n"
    )
    j_proc = subprocess.run(
        [sys.executable, "-c", mk_journal_code, str(probe_db)],
        capture_output=True,
        timeout=30,
        **CHILD_TEXT,
    )
    assert j_proc.returncode == 77
    probe_journal = probe_dir / "events.db-journal"
    assert probe_journal.is_file() and probe_journal.stat().st_size > 0

    held_db = root / "events.db"
    held_journal = root / "events.db-journal"
    orig_db_bytes = held_db.read_bytes()
    # 暂存热日志探针后测试所有 migrate 入口，再还原干净隔离现场验证 review 通过
    held_db.write_bytes(probe_db.read_bytes())
    held_journal.write_bytes(probe_journal.read_bytes())
    dirty_db_sha = hashlib.sha256(held_db.read_bytes()).hexdigest()
    dirty_journal_sha = hashlib.sha256(held_journal.read_bytes()).hexdigest()

    try:
        for fn in (
            lambda: migrate._connect(held_db),
            lambda: migrate.read_schema_version(held_db),
            lambda: migrate.plan_migrations(root),
            lambda: migrate.read_state(root),
            lambda: migrate.resume_migration(root),
            lambda: migrate.assert_writable(root),
            lambda: migrate.migrate(root, backup=bkp),
        ):
            with pytest.raises(RestoreScopeBusy):
                fn()

        cli_mig = _cli("data", "migrate", "--data-root", root, "--backup", bkp, "--json")
        assert cli_mig.returncode == 2
        mig_payload = json.loads(cli_mig.stdout)
        assert mig_payload["ok"] is False
        assert mig_payload["code"] == "held"

        # 核心不变量：热日志仍在且主库、热日志 1 个字节都没被动过！
        assert held_journal.is_file()
        assert hashlib.sha256(held_db.read_bytes()).hexdigest() == dirty_db_sha
        assert hashlib.sha256(held_journal.read_bytes()).hexdigest() == dirty_journal_sha
    finally:
        held_journal.unlink(missing_ok=True)
        held_db.write_bytes(orig_db_bytes)

    rev = restore_manager.review(root)
    assert rev.review_complete is True


# ================================================================ DEFECT-2B


@requires_managed_restore_writes
def test_defect_2b_metrics_recorder_and_runtime_model_slot_blocked_on_held_roots(
    tmp_path: Path,
) -> None:
    """DEFECT-2B：MetricsRecorder 与 RuntimeModelSlot 严拒隔离根，不污染 BindingV1。"""
    root = _seed_standard_root(tmp_path, "metrics_slot_root")

    # 预构造实例（模拟进入恢复预约前已存在的内存对象）
    pre_recorder = MetricsRecorder(root, enabled=True)
    pre_slot = RuntimeModelSlot(ScriptedModel([]), data_root=root, stores={})
    # 确保备份前数据根不含额外未纳管文件
    (root / "metrics.jsonl").unlink(missing_ok=True)
    (root / "model_config.json").unlink(missing_ok=True)

    bkp = tmp_path / "bkp_2b"
    create_backup(root, bkp)
    shutil.rmtree(root)

    # 1. reserved 阶段（root 不存在）：新构造与预构造写入全部被拒且不创建目录
    planned = restore_manager.plan(bkp, root)
    crashed = _cli(
        "data",
        "restore-apply",
        bkp,
        "--data-root",
        root,
        "--expect-plan",
        planned.plan_sha256,
        failpoint="FP-RESERVED",
    )
    assert crashed.returncode == 77
    assert not root.exists()

    with pytest.raises(RestoreScopeBusy):
        MetricsRecorder(root, enabled=True)
    with pytest.raises(RestoreScopeBusy):
        RuntimeModelSlot(ScriptedModel([]), data_root=root, stores={})
    with pytest.raises(RestoreScopeBusy):
        pre_recorder.record("rss_mib", 42.0)
    with pytest.raises(RestoreScopeBusy):
        pre_slot.configure({"mode": "scripted"})
    assert not root.exists()

    # 2. published_hold 阶段：新构造与预构造全部写路径被拒，不产生 metrics.jsonl / model_config.json
    resumed = restore_manager.resume(root)
    assert resumed.phase == "published_hold"

    with pytest.raises(RestoreScopeBusy):
        MetricsRecorder(root, enabled=True)
    with pytest.raises(RestoreScopeBusy):
        MetricsRecorder(root, enabled=False)
    with pytest.raises(RestoreScopeBusy):
        RuntimeModelSlot(ScriptedModel([]), data_root=root, stores={})

    for action in (
        lambda: pre_recorder.record("rss_mib", 16.0),
        lambda: pre_recorder.record_duration_ms("startup_ready_ms", 5.0),
        lambda: pre_recorder.counter("slow_subscriber_dropped", 1),
        lambda: pre_recorder.note("event_seq_head", 3),
        lambda: pre_recorder.sample_once(),
        lambda: pre_recorder.start_sampler(),
        lambda: pre_slot.configure({"mode": "scripted"}),
    ):
        with pytest.raises(RestoreScopeBusy):
            action()

    assert not (root / "metrics.jsonl").exists()
    assert not (root / "model_config.json").exists()

    # BindingV1 完全零污染：review 完整通过且可正常 restore-release！
    rev = restore_manager.review(root)
    assert rev.review_complete is True
    rel = restore_manager.release(
        root,
        expect_review_sha256=rev.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert rel.phase == "released"

    # 正控：解除隔离后 MetricsRecorder 与 RuntimeModelSlot 均恢复正常落盘
    post_recorder = MetricsRecorder(root, enabled=True)
    post_recorder.record("rss_mib", 24.5)
    assert (root / "metrics.jsonl").is_file()
    post_slot = RuntimeModelSlot(ScriptedModel([]), data_root=root, stores={})
    post_slot.configure({"mode": "scripted"})
    assert (root / "model_config.json").is_file()


# ================================================================ DEFECT-2C


@requires_managed_restore_writes
def test_defect_2c_preconstructed_material_stores_blocked_after_reserved_and_published_hold(
    tmp_path: Path,
) -> None:
    """DEFECT-2C (Probe 1)：预约/导入前已构造的四类材料 Store 在进入隔离态后写 API 全部被拒。"""
    root = (tmp_path / "mat_held_root").resolve()
    ws = (tmp_path / "mat_ws").resolve()
    ws.mkdir(parents=True)
    (ws / "a.txt").write_bytes(b"hello\n")

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    build_stack(log, data_root=root)
    pre_snap = SnapshotStore(root / "artifacts")
    pre_journal = JournalStore(root / "artifacts")
    pre_mem = MemoryProjection(log.conn, log=log)
    pre_mem.ensure_schema()
    pre_contents = ContentStore(log, blob_dir=root / "blobs")
    # 预先构造一份合法的 UndoJournal 对象供 record_done / set_control_run 负控调用
    sample_journal = pre_journal.begin(
        "run_pre_1",
        control_run="ctrl_1",
        items=(
            JournalItem(
                root=str(ws),
                path="a.txt",
                call_id="c1",
                before_state="absent",
                expected_after=hashlib.sha256(b"hello\n").hexdigest(),
                action=ACTION_DELETE,
            ),
        ),
    )
    # 删掉预备用的 undo journal 文件后再做干净备份，保证后续检验零新文件产生
    pre_journal.path("run_pre_1").unlink()
    log.close()

    bkp = tmp_path / "bkp_2c"
    create_backup(root, bkp)
    shutil.rmtree(root)

    # 进入 published_hold
    planned = restore_manager.plan(bkp, root)
    applied = restore_manager.apply(bkp, root, expect_plan_sha256=planned.plan_sha256)
    assert applied.phase == "published_hold"

    before_entries = sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )

    for write_call in (
        lambda: pre_snap.init_run("run_leak"),
        lambda: pre_snap.record_preimage(
            "run_leak", "c1", "a.txt", b"hello\n", "0" * 64, root=ws
        ),
        lambda: pre_snap.undo(
            "run_leak", Workspace(ws), guard=lambda: None, allow_direct_write=True
        ),
        lambda: pre_journal.begin(
            "run_leak_j",
            control_run="ctrl",
            items=sample_journal.items,
        ),
        lambda: pre_journal.record_done(sample_journal, 0, "absent"),
        lambda: pre_journal.set_control_run(sample_journal, "ctrl_2"),
        lambda: pre_mem.put_content("企图写穿隔离根的正文"),
        lambda: pre_contents.record("a" * 64, b"x", task_id="t1", run_id="r1"),
    ):
        with pytest.raises((RestoreScopeBusy, RestoreAccessError)):
            write_call()

    after_entries = sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )
    assert after_entries == before_entries
    assert restore_manager.review(root).review_complete is True


@requires_managed_restore_writes
def test_orphan_memory_blob_cleanup_is_blocked_by_restore_hold(tmp_path: Path) -> None:
    """Restore hold must fence recovery's orphan-memory deletion window."""
    root = _seed_standard_root(tmp_path, "orphan_cleanup_hold")
    content_ref = "a" * 64
    orphan = root / "memory_blobs" / content_ref
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan memory bytes")

    backup = tmp_path / "orphan_cleanup_backup"
    create_backup(root, backup)
    shutil.rmtree(root)

    planned = restore_manager.plan(backup, root)
    applied = restore_manager.apply(backup, root, expect_plan_sha256=planned.plan_sha256)
    assert applied.phase == "published_hold"

    class StubProjection:
        log = None

        def blob_path(self, _content_ref: str) -> Path:
            return root / "memory_blobs" / content_ref

        def count_content_ref(self, _content_ref: str, *, exclude_memory_id: str):
            assert exclude_memory_id == ""
            return 0, []

    with pytest.raises(RestoreScopeBusy):
        _clean_orphan_blob(StubProjection(), content_ref)
    assert orphan.read_bytes() == b"orphan memory bytes"

    review = restore_manager.review(root)
    released = restore_manager.release(
        root,
        expect_review_sha256=review.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert released.phase == "released"

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        projection = MemoryProjection(log.conn, log=log)
        projection.ensure_schema()
        assert _clean_orphan_blob(projection, content_ref) is True
    finally:
        log.close()
    assert not orphan.exists()


def test_defect_2c_standard_material_requires_initialized_admitted_events_db_and_rejects_custom_db(
    tmp_path: Path,
) -> None:
    """DEFECT-2C (Probe 2 & 3)：未初始化/0字节/关闭主库及异根或 custom.db 严禁写标准材料。"""
    ws = (tmp_path / "ws_std").resolve()
    ws.mkdir(parents=True)

    # Probe 2A：目标标准根完全没有 events.db 时，SnapshotStore / JournalStore 首次写前拒绝
    empty_std = (tmp_path / "empty_std").resolve()
    snap_missing = SnapshotStore(empty_std / "artifacts")
    j_missing = JournalStore(empty_std / "artifacts")
    sample_items = (
        JournalItem(
            root=str(ws),
            path="f.txt",
            call_id="c1",
            before_state="absent",
            expected_after="1" * 64,
            action=ACTION_DELETE,
        ),
    )
    with pytest.raises(RestoreAccessError):
        snap_missing.init_run("r_missing")
    with pytest.raises(RestoreAccessError):
        snap_missing.record_preimage("r_missing", "c1", "f.txt", b"raw", "1" * 64, root=ws)
    with pytest.raises(RestoreAccessError):
        j_missing.begin("r_missing", control_run="ctrl", items=sample_items)
    assert not list((empty_std / "artifacts").rglob("*.json"))

    # Probe 2B：目标标准根存在 0 字节未初始化 events.db 时，首次写前同样拒绝
    zero_std = (tmp_path / "zero_std").resolve()
    zero_std.mkdir(parents=True)
    (zero_std / "events.db").write_bytes(b"")
    snap_zero = SnapshotStore(zero_std / "artifacts")
    j_zero = JournalStore(zero_std / "artifacts")
    with pytest.raises(RestoreAccessError):
        snap_zero.init_run("r_zero")
    with pytest.raises(RestoreAccessError):
        j_zero.begin("r_zero", control_run="ctrl", items=sample_items)
    assert (zero_std / "events.db").stat().st_size == 0
    assert not list((zero_std / "artifacts").rglob("*.json"))

    # Probe 3A：持 elsewhere/custom.db 锁的 MemoryProjection / ContentStore 严禁写另一标准根
    elsewhere = (tmp_path / "elsewhere").resolve()
    target_std = (tmp_path / "target_std").resolve()
    target_std.mkdir(parents=True)
    custom_elsewhere = EventLog(elsewhere / "custom.db", blob_dir=elsewhere / "custom_blobs")
    try:
        foreign_mem = MemoryProjection(
            custom_elsewhere.conn,
            log=custom_elsewhere,
            blob_dir=target_std / "memory_blobs",
        )
        with pytest.raises(RestoreAccessError):
            foreign_mem.put_content("异根 custom.db 企图写标准根 memory_blobs")
        assert not (target_std / "memory_blobs").exists()

        foreign_cs = ContentStore(custom_elsewhere, blob_dir=target_std / "blobs")
        with pytest.raises(RestoreAccessError):
            foreign_cs.record("b" * 64, b"payload", task_id="t1", run_id="r1")
    finally:
        custom_elsewhere.close()

    # Probe 3B：即使在同一目录打开 custom.db，也不能拿 custom.db 锁写标准材料目录
    same_root = (tmp_path / "same_custom_root").resolve()
    custom_same = EventLog(same_root / "custom.db", blob_dir=same_root / "custom_blobs")
    try:
        same_mem = MemoryProjection(
            custom_same.conn, log=custom_same, blob_dir=same_root / "memory_blobs"
        )
        with pytest.raises(RestoreAccessError):
            same_mem.put_content("同目录 custom.db 企图写标准 memory_blobs")
        assert not (same_root / "memory_blobs").exists()

        snap_custom = SnapshotStore(same_root / "artifacts")
        j_custom = JournalStore(same_root / "artifacts")
        with pytest.raises(RestoreAccessError):
            snap_custom.init_run("r_custom")
        with pytest.raises(RestoreAccessError):
            j_custom.begin("r_custom", control_run="ctrl", items=sample_items)
        assert not list((same_root / "artifacts").rglob("*.json"))
    finally:
        custom_same.close()

    # 正控：标准根打开已初始化、已准入的 EventLog(events.db) 后，四类材料写 API 全部正常放行；
    # 关闭 EventLog 后（失去连续 access），后续独立材料写入再次被拒绝！
    admitted_root = (tmp_path / "admitted_std").resolve()
    main_log = EventLog(admitted_root / "events.db", blob_dir=admitted_root / "blobs")
    try:
        snap_bound = SnapshotStore(admitted_root / "artifacts", owner_log=main_log)
        j_bound = JournalStore(admitted_root / "artifacts", owner_log=main_log)
        mem_ok = MemoryProjection(main_log.conn, log=main_log)
        cs_ok = ContentStore(main_log, blob_dir=admitted_root / "blobs")

        snap_bound.init_run("r_ok")
        assert snap_bound.manifest_path("r_ok").is_file()
        j_created = j_bound.begin("r_ok_j", control_run="ctrl", items=sample_items)
        assert j_created.run_id == "r_ok_j"
        ref = mem_ok.put_content("合法准入的记忆正文")
        assert (admitted_root / "memory_blobs" / ref).is_file()
        assert cs_ok.record("c" * 64, b"ok", task_id="t1", run_id="r_ok") is True
    finally:
        main_log.close()

    with pytest.raises(RestoreAccessError):
        snap_bound.init_run("r_after_close")
    with pytest.raises(RestoreAccessError):
        mem_ok.put_content("关闭后企图继续写")


def _parse_v2(stdout: str) -> RestoreResponseV2:
    return RestoreResponseV2.from_bytes(stdout.rstrip("\r\n").encode("utf-8"))


# ================================================================ DEFECT-3


@requires_managed_restore_writes
def test_defect_3_review_with_later_backup_and_body_scope_matches_release_expect_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFECT-3：带 --later-backup 与 --body-scope 的复核摘要恒定，restore-release 放行。"""
    root = _seed_standard_root(tmp_path, "defect3_root", with_memory="第一版受保护偏好")
    b1 = tmp_path / "backup_b1"
    with monkeypatch.context() as patch:
        patch.setattr(backup, "time", SimpleNamespace(time=lambda: 100))
        create_backup(root, b1)

    # 在活根产生后续历史变化并制作 B2
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        mem = MemoryProjection(log.conn, log=log)
        mem.ensure_schema()
        cur = mem.get("mem_seed_1", allowed=[MemoryScope.user()])
        assert cur is not None
        forget_payload = {
            "call_id": "call_forget_later",
            "tool_id": "memory.forget",
            "status": "succeeded",
            "effect_state": "applied",
            "output": {
                "memory_id": "mem_seed_1",
                "revision": 2,
                "before_status": "active",
                "status": "forgotten",
                "memory_uri": "memory://user/local",
            },
        }
        seq = log.append(
            "run_later_forget",
            "tool.result",
            forget_payload,
            actor_kind="human",
            legacy_write=True,
        )
        mem.apply_event("tool.result", forget_payload, seq=seq)
    finally:
        log.close()

    b2 = tmp_path / "backup_b2"
    with monkeypatch.context() as patch:
        patch.setattr(backup, "time", SimpleNamespace(time=lambda: 200))
        create_backup(root, b2)
    shutil.rmtree(root)

    # 导入 B1 至 published_hold
    planned = restore_manager.plan(b1, root)
    applied = _cli(
        "data",
        "restore-apply",
        b1,
        "--data-root",
        root,
        "--expect-plan",
        planned.plan_sha256,
        "--json",
    )
    assert applied.returncode == 0, applied.stderr

    # 1) 无参复核
    rev_plain_proc = _cli("data", "restore-review", "--data-root", root, "--json")
    assert rev_plain_proc.returncode == 0, rev_plain_proc.stderr
    rev_plain = _parse_v2(rev_plain_proc.stdout).result
    assert rev_plain is not None

    # 2) 附带 --later-backup B2 复核（必须检出 known_later_changes 且 review_sha256 全等！）
    rev_later_proc = _cli(
        "data",
        "restore-review",
        "--data-root",
        root,
        "--later-backup",
        b2,
        "--json",
    )
    assert rev_later_proc.returncode == 0, rev_later_proc.stderr
    rev_later = _parse_v2(rev_later_proc.stdout).result
    assert rev_later is not None
    assert len(rev_later.known_later_changes) >= 1
    assert rev_later.review_sha256 == rev_plain.review_sha256

    # 3) 同时附带 --later-backup B2 与 --body-scope 显式正文披露，review_sha256 仍全等！
    selector = next(
        b.selector for obj in rev_later.objects for b in obj.bodies
    )
    rev_both_proc = _cli(
        "data",
        "restore-review",
        "--data-root",
        root,
        "--later-backup",
        b2,
        "--body-scope",
        selector,
        "--json",
    )
    assert rev_both_proc.returncode == 0, rev_both_proc.stderr
    rev_both = _parse_v2(rev_both_proc.stdout).result
    assert rev_both is not None
    assert rev_both.review_sha256 == rev_later.review_sha256

    # 4) 直接拿 --later-backup B2 产出的 review_sha256 执行真子进程 restore-release：必须退 0！
    rel_proc = _cli(
        "data",
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        rev_later.review_sha256,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert rel_proc.returncode == 0, (rel_proc.stdout, rel_proc.stderr)
    rel_resp = _parse_v2(rel_proc.stdout)
    assert rel_resp.ok is True
    assert rel_resp.result is not None
    assert rel_resp.result.phase == "released"
    assert rel_resp.result.held is False


# ================================================================ W15 + Restore B/C 融合


@requires_managed_restore_writes
def test_w15_safe_gc_refuses_held_roots_and_succeeds_after_v2_restore_release(
    tmp_path: Path,
) -> None:
    """W15 融合：data gc 对 published_hold 闭锁退 2（不污染 BindingV1），v2 解除隔离后退 3 / 0。"""
    root = _seed_standard_root(tmp_path, "gc_restore_v2")
    # 先迁移至 v2（带 migration_log），再放一个孤儿 blob 随备份进入恢复流
    b0 = tmp_path / "b0_pre_mig"
    create_backup(root, b0)
    migrate.migrate(root, backup=b0)
    orphan_rel = Path("blobs") / ("f" * 64)
    (root / orphan_rel).parent.mkdir(parents=True, exist_ok=True)
    (root / orphan_rel).write_bytes(b"orphan-across-restore")
    old_mtime = (1_700_000_000_000 - 120 * DAY_MS) / 1000.0
    os.utime(root / orphan_rel, (old_mtime, old_mtime))

    b_v2 = tmp_path / "backup_v2_with_orphan"
    create_backup(root, b_v2)
    shutil.rmtree(root)

    planned = restore_manager.plan(b_v2, root)
    applied = restore_manager.apply(b_v2, root, expect_plan_sha256=planned.plan_sha256)
    assert applied.phase == "published_hold"
    assert not (root / "events.db.migration.json").exists()
    assert (root / ".restore-origin.json").is_file()
    assert (root / ".restore-token.json").is_file()

    # 1) published_hold 期间：gc --dry-run 与 --approve 均以 restore_held 退 2（0 删除）
    for flag in ("--dry-run", "--approve"):
        gc_held = _cli("data", "gc", "--data-root", root, flag, "--json")
        assert gc_held.returncode == 2, (flag, gc_held.stdout, gc_held.stderr)
        gc_err = json.loads(gc_held.stdout)
        assert gc_err["ok"] is False
        assert gc_err["code"] == "restore_held"
        assert (root / orphan_rel).is_file()

    # 2) restore-review + restore-release 解除隔离
    rev = restore_manager.review(root)
    assert rev.review_complete is True
    rel = restore_manager.release(
        root,
        expect_review_sha256=rev.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert rel.post_release_admission == "admitted_v2_released_proof"
    # 重刷孤儿文件旧 mtime（restore-apply 复制时赋予当前 mtime）
    os.utime(root / orphan_rel, (old_mtime, old_mtime))

    # 3) 解除后（v2 无 .migration.json，保留合法 .restore-origin.json 与 .restore-token.json）：
    #    --dry-run 退 3，--approve 退 0 并清理孤儿 blob！
    gc_dry = _cli(
        "data", "gc", "--data-root", root, "--older-than-days", "1", "--dry-run", "--json"
    )
    assert gc_dry.returncode == 3, (gc_dry.stdout, gc_dry.stderr)
    dry_rep = json.loads(gc_dry.stdout)
    assert dry_rep["ok"] is True
    assert dry_rep["candidates"]["orphan_blobs_count"] == 1
    assert (root / orphan_rel).is_file()

    gc_app = _cli(
        "data", "gc", "--data-root", root, "--older-than-days", "1", "--approve", "--json"
    )
    assert gc_app.returncode == 0, (gc_app.stdout, gc_app.stderr)
    app_rep = json.loads(gc_app.stdout)
    assert app_rep["ok"] is True
    assert app_rep["deleted"]["orphan_blobs_count"] == 1
    assert not (root / orphan_rel).exists()


# ================================================================ Round-2 对抗回归
# [DEFECT-2C-R2] 零黑名单纯结构白名单 + 独立写连续租约保持 + 非标准 DB 默认材料收口
# [DEFECT-6-GC-RESERVED] reserved（AbsentTargetV1 / EmptyTargetV1）下 data gc 返回 restore_held


def test_defect_2c_r2_pure_whitelist_blocks_arbitrary_db_names_and_holds_lease_during_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFECT-2C-R2：任意非标准库名及异根主库一律拒写标准材料，独立写全程持连续锁。"""
    ws = (tmp_path / "ws_r2").resolve()
    ws.mkdir(parents=True)
    (ws / "sample.txt").write_bytes(b"hello-r2\n")
    sample_items = (
        JournalItem(
            root=str(ws),
            path="sample.txt",
            call_id="c_r2",
            before_state="absent",
            expected_after="2" * 64,
            action=ACTION_DELETE,
        ),
    )
    oversized_body = "X" * (MAX_INLINE_BYTES + 4096)
    oversized_payload = {
        "call_id": "c_spill",
        "tool_id": "fs.read",
        "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": oversized_body},
    }

    # 1) 跨根 + 任意非标准库名 aux.db -> 写空目标标准材料目录全部拒绝
    elsewhere = (tmp_path / "elsewhere_r2").resolve()
    target_empty = (tmp_path / "target_empty_r2").resolve()
    target_empty.mkdir(parents=True)
    aux_elsewhere = EventLog(elsewhere / "aux.db")
    try:
        for bad_call in (
            lambda: SnapshotStore(target_empty / "artifacts", owner_log=aux_elsewhere).init_run(
                "r_cross_aux"
            ),
            lambda: SnapshotStore(target_empty / "artifacts").init_run("r_cross_unbound"),
            lambda: JournalStore(target_empty / "artifacts", owner_log=aux_elsewhere).begin(
                "r_cross_j", control_run="ctrl", items=sample_items
            ),
            lambda: JournalStore(target_empty / "artifacts").begin(
                "r_cross_j_u", control_run="ctrl", items=sample_items
            ),
            lambda: ContentStore(aux_elsewhere, blob_dir=target_empty / "blobs").record(
                "3" * 64, b"payload", task_id="t1", run_id="r1"
            ),
            lambda: MemoryProjection(
                aux_elsewhere.conn,
                log=aux_elsewhere,
                blob_dir=target_empty / "memory_blobs",
            ).put_content("跨根 aux.db 企图写标准 memory_blobs"),
        ):
            with pytest.raises(RestoreAccessError):
                bad_call()
    finally:
        aux_elsewhere.close()

    spill_cross = EventLog(elsewhere / "alpha_spill.db", blob_dir=target_empty / "blobs")
    try:
        with pytest.raises(RestoreAccessError):
            spill_cross.append(
                "r_spill_cross",
                "tool.result",
                oversized_payload,
                actor_kind="system",
                legacy_write=True,
            )
    finally:
        spill_cross.close()
    assert not [p for p in target_empty.rglob("*") if p.is_file()]

    # 2) 跨根 + 源库名为 events.db，严禁写另一根（含已有已关闭 events.db 的目标根）
    elsewhere_std = (tmp_path / "elsewhere_std_r2").resolve()
    r_closed_main = (tmp_path / "closed_main_r2").resolve()
    # 初始化辅助库后再写主库材料，以便下方独立验证材料写入门禁。
    EventLog(r_closed_main / "aux.db").close()
    EventLog(r_closed_main / "alpha.db").close()
    r_closed_main = _seed_standard_root(tmp_path, "closed_main_r2")
    closed_before_files = sorted(
        str(p.relative_to(r_closed_main)) for p in r_closed_main.rglob("*") if p.is_file()
    )
    foreign_main = EventLog(elsewhere_std / "events.db")
    try:
        for bad_foreign_call in (
            lambda: SnapshotStore(
                r_closed_main / "artifacts", owner_log=foreign_main
            ).init_run("r_foreign_main"),
            lambda: JournalStore(r_closed_main / "artifacts", owner_log=foreign_main).begin(
                "r_foreign_j", control_run="ctrl", items=sample_items
            ),
            lambda: ContentStore(foreign_main, blob_dir=r_closed_main / "blobs").record(
                "4" * 64, b"foreign", task_id="t1", run_id="r1"
            ),
            lambda: MemoryProjection(
                foreign_main.conn,
                log=foreign_main,
                blob_dir=r_closed_main / "memory_blobs",
            ).put_content("异根 events.db 企图写目标根 memory_blobs"),
        ):
            with pytest.raises(RestoreAccessError):
                bad_foreign_call()
    finally:
        foreign_main.close()

    elsewhere_std2 = (tmp_path / "elsewhere_std2_r2").resolve()
    foreign_spill_main = EventLog(
        elsewhere_std2 / "events.db", blob_dir=r_closed_main / "blobs"
    )
    try:
        with pytest.raises(RestoreAccessError):
            foreign_spill_main.append(
                "r_foreign_spill",
                "tool.result",
                oversized_payload,
                actor_kind="system",
                legacy_write=True,
            )
    finally:
        foreign_spill_main.close()
    assert (
        sorted(
            str(p.relative_to(r_closed_main))
            for p in r_closed_main.rglob("*")
            if p.is_file() and p.relative_to(r_closed_main).parts[0] in (
                "artifacts",
                "blobs",
                "memory_blobs",
            )
        )
        == [
            rel
            for rel in closed_before_files
            if Path(rel).parts[0] in ("artifacts", "blobs", "memory_blobs")
        ]
    )

    # 3) 同根无 events.db + 持 aux.db / alpha.db -> 标准材料目录全部拒写（含软链别名尝试）
    same_no_main = (tmp_path / "same_no_main_r2").resolve()
    aux_same = EventLog(same_no_main / "aux.db")
    try:
        for bad_same_call in (
            lambda: SnapshotStore(same_no_main / "artifacts", owner_log=aux_same).init_run(
                "r_same_bound"
            ),
            lambda: SnapshotStore(same_no_main / "artifacts").init_run("r_same_unbound"),
            lambda: SnapshotStore(same_no_main / "artifacts").record_preimage(
                "r_same_pre", "c1", "sample.txt", b"raw", "5" * 64, root=ws
            ),
            lambda: JournalStore(same_no_main / "artifacts", owner_log=aux_same).begin(
                "r_same_jb", control_run="ctrl", items=sample_items
            ),
            lambda: JournalStore(same_no_main / "artifacts").begin(
                "r_same_ju", control_run="ctrl", items=sample_items
            ),
            lambda: ContentStore(aux_same, blob_dir=same_no_main / "blobs").record(
                "5" * 64, b"same", task_id="t1", run_id="r1"
            ),
            lambda: MemoryProjection(
                aux_same.conn, log=aux_same, blob_dir=same_no_main / "memory_blobs"
            ).put_content("同根无 events.db 持 aux.db 企图写 memory_blobs"),
        ):
            with pytest.raises(RestoreAccessError):
                bad_same_call()
    finally:
        aux_same.close()

    alpha_spill_same = EventLog(same_no_main / "alpha.db", blob_dir=same_no_main / "blobs")
    try:
        with pytest.raises(RestoreAccessError):
            alpha_spill_same.append(
                "r_same_spill",
                "tool.result",
                oversized_payload,
                actor_kind="system",
                legacy_write=True,
            )
    finally:
        alpha_spill_same.close()

    # 符号链接指向 <same_no_main>/artifacts 也不得绕过白名单
    (same_no_main / "artifacts").mkdir(parents=True, exist_ok=True)
    sneaky_link = tmp_path / "sneaky_artifacts_alias"
    try:
        sneaky_link.symlink_to(same_no_main / "artifacts", target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(RestoreAccessError):
            SnapshotStore(sneaky_link).init_run("r_sneaky")

    assert not list((same_no_main / "artifacts").rglob("*.json"))
    assert not (same_no_main / "blobs").exists()
    assert not (same_no_main / "memory_blobs").exists()

    # 4) 同根有已关闭 events.db 时，持同目录 aux.db / alpha.db 绑定写标准材料仍一律拒绝
    aux_in_closed = EventLog(r_closed_main / "aux.db")
    try:
        for bad_closed_call in (
            lambda: SnapshotStore(
                r_closed_main / "artifacts", owner_log=aux_in_closed
            ).init_run("r_closed_aux_snap"),
            lambda: JournalStore(
                r_closed_main / "artifacts", owner_log=aux_in_closed
            ).begin("r_closed_aux_j", control_run="ctrl", items=sample_items),
            lambda: ContentStore(aux_in_closed, blob_dir=r_closed_main / "blobs").record(
                "6" * 64, b"closed_aux", task_id="t1", run_id="r1"
            ),
            lambda: MemoryProjection(
                aux_in_closed.conn, log=aux_in_closed, blob_dir=r_closed_main / "memory_blobs"
            ).put_content("同根 events.db 已关闭时持 aux.db 企图写 memory_blobs"),
        ):
            with pytest.raises(RestoreAccessError):
                bad_closed_call()
    finally:
        aux_in_closed.close()

    alpha_in_closed = EventLog(r_closed_main / "alpha.db", blob_dir=r_closed_main / "blobs")
    try:
        with pytest.raises(RestoreAccessError):
            alpha_in_closed.append(
                "r_closed_alpha_spill",
                "tool.result",
                oversized_payload,
                actor_kind="system",
                legacy_write=True,
            )
    finally:
        alpha_in_closed.close()
    assert (
        sorted(
            str(p.relative_to(r_closed_main))
            for p in r_closed_main.rglob("*")
            if p.is_file() and p.relative_to(r_closed_main).parts[0] in (
                "artifacts",
                "blobs",
                "memory_blobs",
            )
        )
        == [
            rel
            for rel in closed_before_files
            if Path(rel).parts[0] in ("artifacts", "blobs", "memory_blobs")
        ]
    )

    # 5) 正控 A：独立 SnapshotStore(r_closed_main / "artifacts")（owner_log=None，且同根存在合法
    #    已初始化 events.db）在写期间真实持有连续 _MaterialWriteLease，绝不提前释放！
    closed_db_path = r_closed_main / "events.db"
    with ensure_material_write_allowed(r_closed_main / "artifacts") as lease:
        assert lease.active is True
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(closed_db_path, "file"), exclusive=True)
    assert lease.active is False
    probe_excl = acquire_entity_access(observe_entity(closed_db_path, "file"), exclusive=True)
    probe_excl.close()

    real_atomic_write = snapshot_mod.atomic_write_bytes
    observed_locks_during_io: list[bool] = []

    def _assert_locked_during_write(path: Path, data: bytes) -> None:
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(closed_db_path, "file"), exclusive=True)
        observed_locks_during_io.append(True)
        real_atomic_write(path, data)

    monkeypatch.setattr(snapshot_mod, "atomic_write_bytes", _assert_locked_during_write)
    try:
        standalone_snap = SnapshotStore(r_closed_main / "artifacts")
        standalone_snap.record_preimage(
            "r_standalone_locked", "c_locked", "sample.txt", b"hello-r2\n", "7" * 64, root=ws
        )
    finally:
        monkeypatch.setattr(snapshot_mod, "atomic_write_bytes", real_atomic_write)
    assert len(observed_locks_during_io) >= 2  # blob + manifest 两次落盘均在连续锁保护下完成！
    post_excl = acquire_entity_access(observe_entity(closed_db_path, "file"), exclusive=True)
    post_excl.close()

    # 6) 正控 B：非标准库名（aux_cli.db）走默认收口路径，不触碰标准 artifacts / blobs / memory_blobs
    conv_dir = (tmp_path / "converged_aux_dir").resolve()
    _, conv_ev_blobs, _ = default_material_paths(conv_dir / "aux_cli.db", conv_dir)
    aux_conv_log = EventLog(conv_dir / "aux_cli.db", blob_dir=conv_ev_blobs)
    try:
        stack = build_stack(aux_conv_log, data_root=conv_dir)
        mem_conv = open_memory(aux_conv_log)
        mem_ref = mem_conv.put_content("收口至私有非标准目录的记忆正文")
        assert (mem_conv.blob_dir / mem_ref).is_file()
        stack.snapshots.init_run("r_conv_ok")
        assert stack.snapshots.manifest_path("r_conv_ok").is_file()
        aux_conv_log.append(
            "r_conv_ok",
            "tool.result",
            oversized_payload,
            actor_kind="system",
            legacy_write=True,
        )
    finally:
        aux_conv_log.close()

    cli_conv = _cli(
        "task",
        "create",
        "--goal",
        "验证 CLI 非标准库名收口",
        "--db",
        conv_dir / "cli_aux.db",
        "--workspace",
        ws,
    )
    assert cli_conv.returncode == 0, cli_conv.stderr

    assert not (conv_dir / "artifacts").exists()
    assert not (conv_dir / "blobs").exists()
    assert not (conv_dir / "memory_blobs").exists()
    assert (conv_dir / ".aux_cli.db.materials").is_dir()


@pytest.mark.parametrize("target_exists", [False, True], ids=["absent", "empty"])
def test_defect_6_gc_reserved_returns_restore_held_for_absent_and_empty_targets(
    tmp_path: Path, target_exists: bool,
) -> None:
    """GC refuses both kinds of clean targets when the native registry has a reservation."""
    root = _seed_standard_root(tmp_path, "gc_reserved")
    bkp = tmp_path / "bkp_gc_reserved"
    create_backup(root, bkp)
    shutil.rmtree(root)
    if target_exists:
        (root / "skeleton_a" / "skeleton_b").mkdir(parents=True)

    registry = RestoreRegistry()
    record_reserved_batch(registry, restore_manager.plan(bkp, root, registry=registry))
    before = registry.observe()
    assert before.events[-1].kind == "reserved"
    skeleton = set(root.rglob("*"))
    for mode_flag in ("--dry-run", "--approve"):
        proc = _cli("data", "gc", "--data-root", root, mode_flag, "--json")
        assert proc.returncode == 2, (mode_flag, proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert payload["code"] == "restore_held", payload
        assert root.exists() is target_exists
        assert set(root.rglob("*")) == skeleton
        assert registry.observe() == before
