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

from tianshu import maintenance, restore_access
from tianshu.data_cli import _backup
from tianshu.events import EventLog
from tianshu.maintenance import (
    MaintenanceBusy,
    MaintenanceConnection,
    acquire_database_access,
    acquire_entity_access,
    acquire_restore_gate,
)
from tianshu.platform_identity import observe_entity
from tianshu.restore_access import RestoreAccessError, RestoreScopeBusy
from tianshu.restore_registry import RestoreRegistry

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


def test_database_path_replacement_is_detected_while_access_is_live(
    tmp_path, registry
):
    db = tmp_path / "data/events.db"
    log = EventLog(db)
    access = log.conn.database_access
    try:
        moved = tmp_path / "moved.db"
        db.rename(moved)
        db.write_bytes(b"replacement")
        with pytest.raises(Exception, match="已经不匹配"):
            access.verify()
    finally:
        log.close()


def test_ancestor_replacement_is_detected_while_access_is_live(tmp_path, registry):
    parent = tmp_path / "data"
    db = parent / "events.db"
    log = EventLog(db)
    access = log.conn.database_access
    try:
        parent.rename(tmp_path / "old-data")
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
