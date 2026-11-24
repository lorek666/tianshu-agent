"""恢复 T1：EventLog 从 anchor 到 SQLite close 的连续复合 access。"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_restore_contract import UUID, samples

from tianshu import maintenance, pathkeys, restore_access
from tianshu.contents import ContentStore
from tianshu.data_cli import _backup
from tianshu.events import MAX_INLINE_BYTES, EventLog, canonical_bytes, sha256_hex
from tianshu.maintenance import (
    MaintenanceBusy,
    MaintenanceConnection,
    acquire_database_access,
    acquire_entity_access,
    acquire_restore_gate,
)
from tianshu.memory import MemoryProjection
from tianshu.platform_identity import observe_entity
from tianshu.restore_access import RestoreAccessError, RestoreScopeBusy
from tianshu.restore_registry import RestoreRegistry
from tianshu.snapshot import SnapshotStore
from tianshu.undo_journal import JournalStore

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _read_child(process: subprocess.Popen[str]) -> dict:
    result: queue.Queue[str] = queue.Queue()
    assert process.stdout is not None
    thread = threading.Thread(
        target=lambda: result.put(process.stdout.readline()), daemon=True
    )
    thread.start()
    try:
        line = result.get(timeout=20)
    except queue.Empty:
        process.kill()
        raise AssertionError("EventLog child did not reach its pipe barrier") from None
    if not line:
        assert process.stderr is not None
        raise AssertionError(f"EventLog child exited early: {process.stderr.read()}")
    return json.loads(line)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        maintenance, "_lock_root", lambda: tmp_path / "system-lock-root", raising=False
    )
    home = tmp_path / "home"
    home.mkdir()
    value = RestoreRegistry._for_home(home)
    value.initialize(registry_id=UUID)
    monkeypatch.setattr(restore_access, "_registry_factory", lambda: value)
    return value


def test_eventlog_releases_restore_gate_but_keeps_namespace_and_entity(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    try:
        with acquire_restore_gate(exclusive=True):
            pass
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(db, "file"), exclusive=True)
    finally:
        log.close()
    with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
        pass


def test_two_eventlogs_open_in_parallel_and_block_exclusive_until_both_close(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    first = EventLog(db)
    second = EventLog(db)
    try:
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(db, "file"), exclusive=True)
        first.close()
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        second.close()
        with acquire_database_access(db, exclusive=True):
            with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
                pass
    finally:
        first.close()
        second.close()


def test_offline_backup_blocks_eventlog_before_destination_creation(
    tmp_path, registry
):
    root = tmp_path / "data"
    db = root / "events.db"
    destination = tmp_path / "backup"
    log = EventLog(db)
    try:
        with pytest.raises(MaintenanceBusy):
            _backup(root, destination)
        assert not destination.exists()
    finally:
        log.close()


def test_true_process_eventlog_keeps_hardlink_entity_exclusive_blocked(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    alias = tmp_path / "alias.db"
    child = r"""
import json, sys
from pathlib import Path
from tianshu import maintenance, restore_access
from tianshu.events import EventLog
from tianshu.restore_registry import RestoreRegistry

maintenance._lock_root = lambda: Path(sys.argv[3])
restore_access._registry_factory = lambda: RestoreRegistry._for_home(Path(sys.argv[2]))
log = EventLog(Path(sys.argv[1]))
print(json.dumps({"ready": True}), flush=True)
if sys.stdin.readline().strip() != "close":
    raise RuntimeError("EventLog child barrier not released")
log.close()
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            child,
            str(db),
            str(registry.home),
            str(maintenance._lock_root()),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert _read_child(process) == {"ready": True}
        try:
            os.link(db, alias)
        except OSError as exc:
            pytest.skip(f"host cannot create hard links: {exc}")
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(alias, "file"), exclusive=True)
        with pytest.raises(RestoreAccessError, match="单硬链接"):
            EventLog(alias)
        assert process.stdin is not None
        process.stdin.write("close\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=20)
        assert (stdout, stderr, process.returncode) == ("", "", 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=20)
    with acquire_entity_access(observe_entity(alias, "file"), exclusive=True):
        pass


def test_hardlink_alias_is_blocked_by_connected_entity_access(tmp_path, registry):
    db = tmp_path / "data/events.db"
    alias = tmp_path / "alias.db"
    log = EventLog(db)
    try:
        try:
            os.link(db, alias)
        except OSError as exc:
            pytest.skip(f"host cannot create hard links: {exc}")
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(alias, "file"), exclusive=True)
    finally:
        log.close()
    with acquire_entity_access(observe_entity(alias, "file"), exclusive=True):
        pass


def test_sqlite_main_path_and_entity_are_verified_before_admission(
    tmp_path, registry, monkeypatch
):
    db = tmp_path / "data/events.db"

    from tianshu.restore_access import verify_sqlite_main

    class FakeConnection:
        def execute(self, sql):
            assert str(sql) == "PRAGMA database_list"
            return [(0, "main", str(tmp_path / "wrong.db"))]

    with pytest.raises(Exception, match="database_list.main"):
        verify_sqlite_main(FakeConnection(), db, None)

    def reject_main(*_args):
        raise RuntimeError("database_list.main injected mismatch")

    from tianshu import events

    monkeypatch.setattr(events, "verify_sqlite_main", reject_main)
    with pytest.raises(Exception, match="database_list.main"):
        EventLog(db)
    with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
        pass


def test_database_path_replacement_is_detected_or_blocked_while_access_is_live(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    access = log.conn.database_access
    try:
        moved = tmp_path / "moved.db"
        try:
            db.rename(moved)
        except PermissionError as exc:
            assert os.name == "nt", exc
            assert db.is_file()
            assert not moved.exists()
            access.verify()
        else:
            db.write_bytes(b"replacement")
            with pytest.raises(Exception, match="已经不匹配"):
                access.verify()
    finally:
        log.close()


def test_ancestor_replacement_is_detected_or_blocked_while_access_is_live(
    tmp_path, registry
):
    parent = tmp_path / "data"
    db = parent / "events.db"
    log = EventLog(db)
    access = log.conn.database_access
    try:
        old_parent = tmp_path / "old-data"
        try:
            parent.rename(old_parent)
        except PermissionError as exc:
            assert os.name == "nt", exc
            assert parent.is_dir()
            assert db.is_file()
            assert not old_parent.exists()
            access.verify()
        else:
            parent.mkdir()
            db.write_bytes(b"replacement")
            with pytest.raises(Exception, match="已经不匹配"):
                access.verify()
    finally:
        log.close()


def test_cross_thread_close_failure_keeps_entity_access(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    errors = []

    def wrong_thread_close():
        try:
            log.close()
        except sqlite3.ProgrammingError as exc:
            errors.append(exc)

    thread = threading.Thread(target=wrong_thread_close)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert len(errors) == 1
    with pytest.raises(MaintenanceBusy):
        acquire_entity_access(observe_entity(db, "file"), exclusive=True)
    log.close()
    with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
        pass


def test_native_handle_close_failure_keeps_entity_access(
    tmp_path, registry, monkeypatch
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    access = log.conn.database_access
    assert access is not None and access.database_handle is not None
    database_handle = access.database_handle
    original_close = type(database_handle).close

    def reject_database_handle_once(handle):
        if handle is database_handle:
            raise OSError("injected native handle close failure")
        original_close(handle)

    with monkeypatch.context() as patch:
        patch.setattr(type(database_handle), "close", reject_database_handle_once)
        with pytest.raises(OSError, match="native handle close"):
            log.close()
    with pytest.raises(MaintenanceBusy):
        acquire_entity_access(observe_entity(db, "file"), exclusive=True)
    log.close()
    with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
        pass


@pytest.mark.parametrize(
    ("relative", "domain"),
    [
        ("ARTIFACTS", "artifacts"),
        ("MEMORY_BLOBS", "memory_blobs"),
        ("BLOBS", "blobs"),
        ("ARTIFACTS/BLOBS", "artifacts"),
    ],
)
def test_case_insensitive_material_aliases_keep_standard_domain(
    tmp_path, monkeypatch, relative, domain
):
    monkeypatch.setattr(pathkeys, "CASE_INSENSITIVE_FS", True)
    root = tmp_path / "data"
    target = root.joinpath(*relative.split("/"))

    located = restore_access._standard_material_root_and_domain(target)

    assert located is not None
    standard_root, actual_domain = located
    assert pathkeys.overlap_key(str(standard_root)) == pathkeys.overlap_key(str(root))
    assert actual_domain == domain


@pytest.mark.parametrize(
    "relative",
    ["ARTIFACTS", "MEMORY_BLOBS", "BLOBS", "ARTIFACTS/BLOBS"],
)
def test_case_insensitive_material_alias_rejects_foreign_owner(
    tmp_path, registry, monkeypatch, relative
):
    monkeypatch.setattr(pathkeys, "CASE_INSENSITIVE_FS", True)
    target_root = tmp_path / "target"
    owner = EventLog(tmp_path / "foreign/events.db")
    try:
        target = target_root.joinpath(*relative.split("/"))
        with pytest.raises(RestoreAccessError, match="同根 events.db"):
            restore_access.ensure_material_write_allowed(target, owner_log=owner)
        assert not target_root.exists()
    finally:
        owner.close()


@pytest.mark.parametrize(
    "relative",
    ["ARTIFACTS", "MEMORY_BLOBS", "BLOBS", "ARTIFACTS/BLOBS"],
)
def test_case_insensitive_material_alias_requires_admitted_main_for_ownerless_write(
    tmp_path, registry, monkeypatch, relative
):
    monkeypatch.setattr(pathkeys, "CASE_INSENSITIVE_FS", True)
    root = tmp_path / "data"
    target = root.joinpath(*relative.split("/"))

    with pytest.raises(RestoreAccessError):
        restore_access.ensure_material_write_allowed(target)
    assert not root.exists()


@pytest.mark.parametrize(
    "relative",
    ["ARTIFACTS", "MEMORY_BLOBS", "BLOBS", "ARTIFACTS/BLOBS"],
)
def test_case_insensitive_material_alias_accepts_matching_active_owner(
    tmp_path, registry, monkeypatch, relative
):
    monkeypatch.setattr(pathkeys, "CASE_INSENSITIVE_FS", True)
    root = tmp_path / "data"
    owner = EventLog(root / "events.db")
    try:
        target = root.joinpath(*relative.split("/"))
        with restore_access.ensure_material_write_allowed(target, owner_log=owner) as lease:
            assert lease.active
    finally:
        owner.close()


@pytest.mark.parametrize("ancestor", ["artifacts", "blobs", "memory_blobs"])
def test_private_event_spill_under_unrelated_standard_ancestor(
    tmp_path, registry, ancestor
):
    standard_ancestor = tmp_path / ancestor
    db = standard_ancestor / "nested" / "aux.db"
    _, event_blobs, _ = restore_access.default_material_paths(db)
    assert event_blobs == db.parent / ".aux.db.materials" / "event_blobs"
    payload = {
        "call_id": "c_private_spill",
        "tool_id": "fs.read",
        "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": "X" * (MAX_INLINE_BYTES + 1024)},
    }

    log = EventLog(db, blob_dir=event_blobs)
    try:
        log.append("r_private_spill", "tool.result", payload, actor_kind="system")
        row = log.conn.execute(
            "SELECT payload, payload_ref FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert row["payload"] is None
        assert row["payload_ref"] is not None
        ref = Path(row["payload_ref"])
        assert ref.parent == event_blobs
        assert json.loads(ref.read_bytes()) == payload
        assert log.read_after(0)[0].payload == payload
    finally:
        log.close()

    assert not (standard_ancestor / "events.db").exists()
    assert not (db.parent / "events.db").exists()


@pytest.mark.parametrize("container", [".aux.db.materials", ".memory.materials"])
def test_private_material_container_stops_standard_ancestor_scan(tmp_path, container):
    target = tmp_path / "artifacts" / "nested" / container / "event_blobs"
    assert restore_access._standard_material_root_and_domain(target) is None


@pytest.mark.parametrize(
    "relative",
    ["artifacts", "blobs", "memory_blobs", ".aux.db.materials/artifacts"],
)
def test_private_container_does_not_hide_explicit_standard_domain(
    tmp_path, registry, relative
):
    root = tmp_path / "standard"
    owner = EventLog(tmp_path / "foreign" / "aux.db")
    try:
        target = root / relative
        with pytest.raises(RestoreAccessError, match="同根 events.db"):
            restore_access.ensure_material_write_allowed(target, owner_log=owner)
        assert not root.exists()
    finally:
        owner.close()


def test_private_material_symlink_alias_to_standard_domain_still_rejects(
    tmp_path, registry
):
    root = tmp_path / "standard"
    (root / "artifacts").mkdir(parents=True)
    alias = tmp_path / "alias" / ".aux.db.materials"
    alias.parent.mkdir()
    try:
        alias.symlink_to(root / "artifacts", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前平台不能创建符号链接：{exc}")
    owner = EventLog(tmp_path / "foreign" / "aux.db")
    try:
        with pytest.raises(RestoreAccessError, match="同根 events.db"):
            restore_access.ensure_material_write_allowed(
                alias / "event_blobs", owner_log=owner
            )
        assert not list((root / "artifacts").iterdir())
        assert not (root / "events.db").exists()
    finally:
        owner.close()


@pytest.mark.parametrize("explicit", [True, False], ids=["owner", "ownerless"])
@pytest.mark.parametrize(
    ("destination", "relative"),
    [
        ("foreign-memory", "memory_blobs"),
        ("foreign-artifacts", "artifacts"),
        ("same-root-memory", "memory_blobs"),
        ("outside-standard", "ordinary"),
    ],
)
def test_standard_material_symlink_cannot_rebind_after_resolution(
    tmp_path, registry, explicit, destination, relative
):
    shell_root = tmp_path / "shell"
    owner = EventLog(shell_root / "events.db")
    try:
        if destination == "same-root-memory":
            resolved_target = shell_root / relative
        else:
            resolved_target = tmp_path / destination / relative
        resolved_target.mkdir(parents=True)
        alias = shell_root / "artifacts"
        try:
            alias.symlink_to(resolved_target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"当前平台不能创建符号链接：{exc}")

        raw = restore_access._standard_material_root_and_domain(alias)
        canonical = restore_access._standard_material_root_and_domain(
            alias.resolve(strict=True)
        )
        assert raw == (shell_root, "artifacts")
        assert canonical != raw
        before = sorted(path.relative_to(resolved_target) for path in resolved_target.rglob("*"))
        with pytest.raises(RestoreAccessError, match="canonical"):
            with restore_access.ensure_material_write_allowed(
                alias, owner_log=owner if explicit else None
            ) as lease:
                assert lease.active
                if explicit:
                    store = SnapshotStore(alias, owner_log=owner)
                    store.init_run("r_escape")
                    manifest = resolved_target / "snapshots" / "r_escape.json"
                    assert manifest.is_file()
                else:
                    marker = resolved_target / "escaped.marker"
                    marker.write_bytes(b"written-through-rebound-standard-domain")
                    assert marker.is_file()
        after = sorted(path.relative_to(resolved_target) for path in resolved_target.rglob("*"))
        assert after == before
    finally:
        owner.close()


@pytest.mark.parametrize("explicit", [True, False], ids=["owner", "ownerless"])
def test_nonstandard_material_symlink_rebinds_to_real_standard_root(
    tmp_path, registry, explicit
):
    root = tmp_path / "data"
    owner = EventLog(root / "events.db")
    try:
        standard = root / "artifacts"
        standard.mkdir()
        alias = tmp_path / "private" / ".aux.db.materials"
        alias.parent.mkdir()
        try:
            alias.symlink_to(standard, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"当前平台不能创建符号链接：{exc}")
        assert restore_access._standard_material_root_and_domain(alias) is None
        assert restore_access._standard_material_root_and_domain(
            alias.resolve(strict=True)
        ) == (root, "artifacts")
        with restore_access.ensure_material_write_allowed(
            alias, owner_log=owner if explicit else None
        ) as lease:
            assert lease.active
            marker = alias / "rebound-safe.marker"
            marker.write_bytes(b"real-standard-root")
        assert (standard / "rebound-safe.marker").read_bytes() == b"real-standard-root"
    finally:
        owner.close()


@pytest.mark.parametrize("explicit", [True, False], ids=["owner", "ownerless"])
def test_standard_material_self_symlink_keeps_same_root_and_domain(
    tmp_path, registry, explicit
):
    root = tmp_path / "data"
    owner = EventLog(root / "events.db")
    try:
        artifacts = root / "artifacts"
        artifacts.mkdir()
        alias = artifacts / "self"
        try:
            alias.symlink_to(artifacts, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"当前平台不能创建符号链接：{exc}")
        with restore_access.ensure_material_write_allowed(
            alias, owner_log=owner if explicit else None
        ) as lease:
            assert lease.active
            marker = alias / "safe.marker"
            marker.write_bytes(b"same-standard-domain")
        assert marker.read_bytes() == b"same-standard-domain"
    finally:
        owner.close()


def test_active_restore_scope_still_blocks_private_material_container(
    tmp_path, registry
):
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    reserved = samples(fixture_root)["e1"]
    before = registry.observe()
    gate = acquire_restore_gate(exclusive=True)
    try:
        registry.append_event(reserved, expected=before, operation_access=gate)
    finally:
        gate.close()
    final_root = Path(reserved.payload.placement.canonical_root.text)
    owner = EventLog(tmp_path / "owner" / "aux.db")
    try:
        target = final_root / ".aux.db.materials" / "event_blobs"
        with pytest.raises(RestoreScopeBusy):
            restore_access.ensure_material_write_allowed(target, owner_log=owner)
        assert not target.exists()
    finally:
        owner.close()


@pytest.fixture
def active_reserved_root(tmp_path, registry):
    fixture_root = tmp_path / "reserved-fixture"
    fixture_root.mkdir()
    reserved = samples(fixture_root)["e1"]
    before = registry.observe()
    gate = acquire_restore_gate(exclusive=True)
    try:
        registry.append_event(reserved, expected=before, operation_access=gate)
    finally:
        gate.close()
    root = Path(reserved.payload.placement.canonical_root.text)
    root.mkdir(parents=True)
    return root


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        # Windows records the link kind at creation time. A directory link to an
        # existing file makes non-strict realpath raise WinError 267 instead of
        # following the leaf; POSIX ignores this flag, so derive it from target.
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前平台不能创建符号链接：{exc}")


@pytest.mark.parametrize(
    ("extended", "plain"),
    [
        (r"\\?\C:\data\reserved\leaf", r"C:\data\reserved\leaf"),
        (r"\\?\UNC\server\share\reserved\leaf", r"\\server\share\reserved\leaf"),
    ],
)
def test_restore_scope_key_folds_windows_extended_readlink_paths(extended, plain):
    assert restore_access._scope_key(extended) == restore_access._scope_key(plain)


def test_snapshot_manifest_static_subdirectory_symlink_respects_reserved_root(
    tmp_path, registry, active_reserved_root
):
    root = tmp_path / "safe"
    root.mkdir(parents=True)
    _symlink_or_skip(root / "snapshots", active_reserved_root)
    owner = EventLog(tmp_path / "owner" / "events.db")
    try:
        store = SnapshotStore(root, owner_log=owner)
        with pytest.raises(RestoreScopeBusy):
            store.init_run("static-manifest-escape")
        assert not (active_reserved_root / "static-manifest-escape.json").exists()
    finally:
        owner.close()


def test_journal_static_subdirectory_symlink_respects_reserved_root(
    tmp_path, registry, active_reserved_root
):
    root = tmp_path / "safe"
    root.mkdir(parents=True)
    _symlink_or_skip(root / "undo", active_reserved_root)
    owner = EventLog(tmp_path / "owner" / "events.db")
    try:
        journal = JournalStore(root, owner_log=owner)
        with pytest.raises(RestoreScopeBusy):
            journal.begin("static-journal-escape", control_run="control", items=())
        assert not (active_reserved_root / "static-journal-escape.json").exists()
    finally:
        owner.close()


def test_event_spill_static_leaf_symlink_respects_reserved_root(
    tmp_path, registry, active_reserved_root
):
    root = tmp_path / "safe"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    payload = {
        "call_id": "c_static_spill",
        "tool_id": "fs.read",
        "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": "X" * (MAX_INLINE_BYTES + 1024)},
    }
    digest = sha256_hex(canonical_bytes(payload))
    target = root / "blobs" / digest
    try:
        _symlink_or_skip(target, active_reserved_root / "static-spill.json")
        with pytest.raises(RestoreScopeBusy):
            log.append("r_static_spill", "tool.result", payload, actor_kind="system")
        assert target.is_symlink()
        assert not (active_reserved_root / "static-spill.json").exists()
        assert log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        log.close()


def test_snapshot_preimage_existing_leaf_symlink_respects_reserved_root(
    tmp_path, registry, active_reserved_root
):
    root = tmp_path / "safe"
    owner = EventLog(tmp_path / "owner" / "events.db")
    try:
        store = SnapshotStore(root, owner_log=owner)
        before = b"static preimage symlink negative control"
        target = store.blob_dir / sha256_hex(before)
        escaped = active_reserved_root / "static-preimage.bin"
        escaped.write_bytes(b"reserved bytes must remain untouched")
        target.parent.mkdir(parents=True, exist_ok=True)
        _symlink_or_skip(target, escaped)
        with pytest.raises(RestoreScopeBusy):
            store.record_preimage(
                "static-preimage-escape",
                "c_static_preimage",
                "note.txt",
                before,
                "after",
                root=tmp_path / "workspace",
            )
        assert target.is_symlink()
        assert escaped.read_bytes() == b"reserved bytes must remain untouched"
        assert not store.manifest_path("static-preimage-escape").exists()
    finally:
        owner.close()


def test_material_scope_guard_checks_leaf_link_when_realpath_fails(
    tmp_path, registry, active_reserved_root, monkeypatch
):
    root = tmp_path / "safe"
    owner = EventLog(tmp_path / "owner" / "events.db")
    try:
        before = b"static preimage realpath failure negative control"
        target = root / "preimage_blobs" / sha256_hex(before)
        escaped = active_reserved_root / "static-preimage-realpath.bin"
        escaped.write_bytes(b"reserved bytes must remain untouched")
        target.parent.mkdir(parents=True)
        _symlink_or_skip(target, escaped)
        original_resolve = Path.resolve
        original_readlink = os.readlink

        def fail_leaf_realpath(path, *args, **kwargs):
            if path == target:
                # Model CPython 3.12 ntpath realpath(strict=False): WinError 267
                # is not in its non-strict allowlist, so readlink fallback is
                # never reached. The scope guard must inspect the link itself.
                raise NotADirectoryError("injected Windows leaf realpath WinError 267")
            return original_resolve(path, *args, **kwargs)

        def extended_readlink(path):
            linked = original_readlink(path)
            if os.name == "nt" and Path(path) == target and not linked.startswith("\\\\?\\"):
                return "\\\\?\\" + linked
            return linked

        monkeypatch.setattr(Path, "resolve", fail_leaf_realpath)
        monkeypatch.setattr(os, "readlink", extended_readlink)
        with pytest.raises(RestoreScopeBusy):
            restore_access.ensure_material_write_allowed(target, owner_log=owner)
        assert target.is_symlink()
        assert escaped.read_bytes() == b"reserved bytes must remain untouched"
    finally:
        owner.close()


@pytest.mark.parametrize(
    ("destination", "allowed"),
    [("same-root", True), ("foreign-root", False), ("outside-standard", False)],
)
def test_snapshot_preimage_existing_file_leaf_symlink_keeps_standard_domain(
    tmp_path, registry, destination, allowed
):
    root = tmp_path / "safe"
    owner = EventLog(root / "events.db")
    try:
        store = SnapshotStore(root / "artifacts", owner_log=owner)
        before = b"static preimage file leaf symlink control"
        if destination == "same-root":
            source = root / "artifacts" / "blobs" / "source.bin"
        elif destination == "foreign-root":
            source = tmp_path / "foreign" / "artifacts" / "blobs" / "source.bin"
        else:
            source = tmp_path / "outside" / "source.bin"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(before)
        target = store.blob_dir / sha256_hex(before)
        _symlink_or_skip(target, source)

        if allowed:
            change = store.record_preimage(
                "static-preimage-safe-leaf",
                "c_static_preimage_safe_leaf",
                "note.txt",
                before,
                "after",
                root=tmp_path / "workspace",
            )
            assert change.blob_ref == str(target)
            assert store.manifest_path("static-preimage-safe-leaf").is_file()
        else:
            with pytest.raises(RestoreAccessError, match="canonical"):
                store.record_preimage(
                    "static-preimage-rebound-leaf",
                    "c_static_preimage_rebound_leaf",
                    "note.txt",
                    before,
                    "after",
                    root=tmp_path / "workspace",
                )
            assert not store.manifest_path("static-preimage-rebound-leaf").exists()
        assert target.is_symlink()
        assert source.read_bytes() == before
    finally:
        owner.close()


def test_content_record_static_leaf_symlink_respects_reserved_root(
    tmp_path, registry, active_reserved_root
):
    root = tmp_path / "safe"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    data = b"static content symlink negative control"
    digest = sha256_hex(data)
    target = root / "blobs" / digest
    try:
        store = ContentStore(log, blob_dir=root / "blobs")
        _symlink_or_skip(target, active_reserved_root / "static-content.json")
        with pytest.raises(RestoreScopeBusy):
            store.record(digest, data, task_id="t_static", run_id="r_static")
        assert target.is_symlink()
        assert not (active_reserved_root / "static-content.json").exists()
        assert store.created_count() == 0
    finally:
        log.close()


def test_memory_put_static_leaf_symlink_respects_reserved_root(
    tmp_path, registry, active_reserved_root
):
    root = tmp_path / "safe"
    log = EventLog(root / "events.db", blob_dir=root / "event_blobs")
    content = "static memory symlink negative control"
    digest = sha256_hex(content.encode("utf-8"))
    target = root / "memory_blobs" / digest
    try:
        projection = MemoryProjection(log.conn, log=log, blob_dir=root / "memory_blobs")
        target.parent.mkdir(parents=True, exist_ok=True)
        _symlink_or_skip(target, active_reserved_root / "static-memory.txt")
        with pytest.raises(RestoreScopeBusy):
            projection.put_content(content)
        assert target.is_symlink()
        assert not (active_reserved_root / "static-memory.txt").exists()
    finally:
        log.close()


@pytest.mark.parametrize("container", [".aux.db.materials", ".memory.materials"])
def test_private_material_child_symlink_cannot_escape_canonical_container(
    tmp_path, registry, container
):
    private = tmp_path / "artifacts" / "nested" / container / "event_blobs"
    private.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(private / "escape", outside)
    owner = EventLog(tmp_path / "owner" / "aux.db")
    try:
        with pytest.raises(RestoreAccessError, match="canonical"):
            restore_access.ensure_material_write_allowed(
                private / "escape" / "payload.json", owner_log=owner
            )
        assert not list(outside.iterdir())
    finally:
        owner.close()


@pytest.mark.parametrize("container", [".aux.db.materials", ".memory.materials"])
def test_private_material_self_symlink_stays_inside_canonical_container(
    tmp_path, registry, container
):
    private = tmp_path / "artifacts" / "nested" / container / "event_blobs"
    private.mkdir(parents=True)
    _symlink_or_skip(private / "self", private)
    owner = EventLog(tmp_path / "owner" / "aux.db")
    try:
        with restore_access.ensure_material_write_allowed(
            private / "self" / "payload.json", owner_log=owner
        ):
            pass
    finally:
        owner.close()


@pytest.mark.parametrize("explicit", [True, False], ids=["owner", "ownerless"])
def test_standard_material_lease_outlives_normal_log_shutdown(tmp_path, registry, explicit):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    access = log.conn.database_access
    assert access is not None and access.entity is not None
    try:
        material = db.parent / "memory_blobs"
        with restore_access.ensure_material_write_allowed(
            material, owner_log=log if explicit else None
        ) as lease:
            assert lease.active
            log.close()
            assert access.namespace.active and access.entity.active
            material.mkdir()
        assert not access.namespace.active and not access.entity.active
        assert material.is_dir()
    finally:
        log.close()


def test_ownerless_material_fallback_is_exclusive_with_active_eventlog(
    tmp_path, registry, monkeypatch
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    try:
        # Exercise the race where a close/unregister happens after the active-log
        # lookup. The fallback must not open another SQLite handle under a shared
        # namespace/entity lease while this connection is still active.
        monkeypatch.setattr(
            restore_access, "_borrow_initialized_access_in_root", lambda _key: None
        )
        with pytest.raises(MaintenanceBusy):
            restore_access.ensure_material_write_allowed(db.parent / "artifacts")
        assert log.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        log.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-scoped record locks")
def test_parallel_eventlogs_share_native_main_handle_until_final_close(
    tmp_path, registry, monkeypatch
):
    db = tmp_path / "data/events.db"
    native_open = restore_access.os.open
    opened: list[int] = []

    def track_native_main(path, *args, **kwargs):
        fd = native_open(path, *args, **kwargs)
        if Path(path) == db.resolve():
            opened.append(fd)
        return fd

    monkeypatch.setattr(restore_access.os, "open", track_native_main)
    first = EventLog(db)
    try:
        second = EventLog(db)
        try:
            assert len(opened) == 1
            assert os.fstat(opened[0]).st_ino == db.stat().st_ino
            first.close()
            assert os.fstat(opened[0]).st_ino == db.stat().st_ino
            assert second.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            second.close()
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        first.close()


def test_active_reserved_scope_blocks_standard_and_descendant_databases(
    tmp_path, registry
):
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    fixture = samples(fixture_root)
    reserved = fixture["e1"]
    before = registry.observe()
    gate = acquire_restore_gate(exclusive=True)
    try:
        registry.append_event(reserved, expected=before, operation_access=gate)
    finally:
        gate.close()
    final_root = Path(reserved.payload.placement.canonical_root.text)
    with pytest.raises(RestoreScopeBusy):
        EventLog(final_root / "events.db")
    with pytest.raises(RestoreScopeBusy):
        EventLog(final_root / "descendant/custom.db")


def test_absent_registry_is_rechecked_before_anchor_after_reservation_race(
    tmp_path, monkeypatch
):
    lock_root = tmp_path / "system-lock-root"
    home = tmp_path / "home"
    home.mkdir()
    registry = RestoreRegistry._for_home(home)
    monkeypatch.setattr(maintenance, "_lock_root", lambda: lock_root)
    monkeypatch.setattr(restore_access, "_registry_factory", lambda: registry)

    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    reserved = samples(fixture_root)["e1"]
    event_path = tmp_path / "reserved.json"
    event_path.write_bytes(reserved.to_bytes())
    target = Path(reserved.payload.placement.canonical_root.text) / "events.db"

    observed_absent = threading.Event()
    release_open = threading.Event()
    original_gate = restore_access.acquire_restore_gate

    def pause_before_gate(*, exclusive=False):
        if not exclusive:
            observed_absent.set()
            if not release_open.wait(20):
                raise TimeoutError("ordinary open barrier timed out")
        return original_gate(exclusive=exclusive)

    monkeypatch.setattr(restore_access, "acquire_restore_gate", pause_before_gate)
    opened: queue.Queue[object] = queue.Queue()

    def open_log():
        try:
            value = EventLog(target)
        except BaseException as exc:
            opened.put(exc)
        else:
            opened.put(value)

    thread = threading.Thread(target=open_log)
    thread.start()
    assert observed_absent.wait(20)

    child = r"""
import json, os, sys
from pathlib import Path
from tianshu import maintenance
from tianshu.maintenance import acquire_restore_gate
from tianshu.restore_contract import RestoreEventV1
from tianshu.restore_registry import RestoreRegistry

maintenance._lock_root = lambda: Path(os.environ["TIANSHU_TEST_LOCK_ROOT"])
registry = RestoreRegistry._for_home(Path(sys.argv[1]))
event = RestoreEventV1.from_bytes(Path(sys.argv[2]).read_bytes())
expected = registry.initialize(registry_id=event.registry_id)
gate = acquire_restore_gate(exclusive=True)
try:
    registry.append_event(event, expected=expected, operation_access=gate)
finally:
    gate.close()
print(json.dumps({"reserved": True}), flush=True)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["TIANSHU_TEST_LOCK_ROOT"] = str(lock_root)
    try:
        child_result = subprocess.run(
            [sys.executable, "-u", "-c", child, str(home), str(event_path)],
            env=env,
            capture_output=True,
            timeout=20,
            text=True,
            encoding="utf-8",
        )
        assert child_result.returncode == 0, child_result.stderr
        assert json.loads(child_result.stdout) == {"reserved": True}
    finally:
        release_open.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    try:
        outcome = opened.get(timeout=1)
    except queue.Empty:
        raise AssertionError("ordinary EventLog neither completed nor failed") from None
    if hasattr(outcome, "close"):
        outcome.close()
    assert isinstance(outcome, RestoreScopeBusy)
    assert not target.exists()


def test_sqlite_close_success_releases_composite_access(tmp_path, registry):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    log.close()
    with acquire_restore_gate(exclusive=True):
        with acquire_database_access(db, exclusive=True):
            with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
                pass


def test_maintenance_connection_owns_composite_after_eventlog_is_collected(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    connection: MaintenanceConnection = log.conn
    del log
    with pytest.raises(MaintenanceBusy):
        acquire_entity_access(observe_entity(db, "file"), exclusive=True)
    connection.close()
    with acquire_entity_access(observe_entity(db, "file"), exclusive=True):
        pass
