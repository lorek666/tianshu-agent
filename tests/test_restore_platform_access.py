"""恢复 T1：原生实体身份、复合锁与真实子进程连续性。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import maintenance
from tianshu.maintenance import (
    MaintenanceBusy,
    acquire_entity_access,
    acquire_restore_gate,
    compose_access,
)
from tianshu.platform_identity import NativeEntityError, observe_entity

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture(autouse=True)
def isolated_lock_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        maintenance, "_lock_root", lambda: tmp_path / "system-lock-root", raising=False
    )


def _line(process: subprocess.Popen[str]) -> dict:
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
        raise AssertionError("entity holder did not reach the pipe barrier") from None
    if not line:
        assert process.stderr is not None
        raise AssertionError(f"entity holder exited early: {process.stderr.read()}")
    return json.loads(line)


HOLDER = """
import json
import sys
from pathlib import Path
from tianshu import maintenance
from tianshu.crashpoint import failpoint
from tianshu.maintenance import acquire_entity_access
from tianshu.platform_identity import observe_entity

maintenance._lock_root = lambda: Path(sys.argv[3])
path = Path(sys.argv[1])
exclusive = sys.argv[2] == "exclusive"
identity = observe_entity(path, "file")
access = acquire_entity_access(identity, exclusive=exclusive)
print(json.dumps({"ready": True, "identity": identity.to_dict()}), flush=True)
action = sys.stdin.readline().strip()
if action == "crash":
    failpoint("restore-entity-held")
access.close()
print(json.dumps({"closed": True}), flush=True)
"""


@contextmanager
def _entity_holder(path: Path, *, exclusive: bool):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["TIANSHU_FAILPOINT"] = "restore-entity-held"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            HOLDER,
            str(path),
            "exclusive" if exclusive else "shared",
            str(maintenance._lock_root()),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **CHILD_TEXT,
    )
    try:
        ready = _line(process)
        assert ready["ready"] is True
        yield process, ready["identity"]
    finally:
        if process.poll() is None:
            assert process.stdin is not None
            try:
                process.stdin.write("close\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=20)


def test_posix_or_windows_file_identity_comes_from_a_real_entity(tmp_path):
    path = tmp_path / "events.db"
    path.write_bytes(b"database")
    identity = observe_entity(path, "file")
    if os.name == "nt":
        assert identity.os == "windows"
        assert identity.volume_serial >= 0
        assert len(identity.file_id_hex) == 32
        int(identity.file_id_hex, 16)
    else:
        stat = path.stat()
        assert identity.os == "posix"
        assert identity.device == stat.st_dev
        assert identity.inode == stat.st_ino


def test_hardlink_aliases_share_the_same_entity_identity_and_lock(tmp_path):
    first = tmp_path / "events.db"
    alias = tmp_path / "alias.db"
    first.write_bytes(b"database")
    try:
        os.link(first, alias)
    except OSError as exc:
        pytest.skip(f"host cannot create hard links: {exc}")
    assert observe_entity(first, "file") == observe_entity(alias, "file")
    with acquire_entity_access(observe_entity(first, "file"), exclusive=True):
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(observe_entity(alias, "file"))


def test_directory_identity_is_distinct_from_its_children(tmp_path):
    directory = tmp_path / "directory"
    child = directory / "events.db"
    directory.mkdir()
    child.write_bytes(b"database")
    assert observe_entity(directory, "directory") != observe_entity(child, "file")


def test_symlink_and_windows_reparse_points_are_not_accepted(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"database")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("host cannot create file symlinks")
    with pytest.raises(NativeEntityError):
        observe_entity(alias, "file")


@pytest.mark.parametrize("mode", ["shared", "exclusive"])
def test_true_process_entity_holders_share_or_block_correctly(tmp_path, mode):
    path = tmp_path / "events.db"
    path.write_bytes(b"database")
    identity = observe_entity(path, "file")
    with _entity_holder(path, exclusive=(mode == "exclusive")) as (process, child_identity):
        assert child_identity == observe_entity(path, "file").to_dict()
        with pytest.raises(MaintenanceBusy):
            acquire_entity_access(identity, exclusive=True)
        if mode == "shared":
            with acquire_entity_access(identity):
                pass
        else:
            with pytest.raises(MaintenanceBusy):
                acquire_entity_access(identity)
        assert process.stdin is not None
        process.stdin.write("close\n")
        process.stdin.flush()
        assert _line(process) == {"closed": True}
        assert process.wait(timeout=20) == 0


def test_hard_exit_releases_entity_access(tmp_path):
    path = tmp_path / "events.db"
    path.write_bytes(b"database")
    identity = observe_entity(path, "file")
    with _entity_holder(path, exclusive=True) as (process, _child_identity):
        assert process.stdin is not None
        process.stdin.write("crash\n")
        process.stdin.flush()
        assert process.wait(timeout=20) == 77
    with acquire_entity_access(identity, exclusive=True):
        pass


def test_restore_gate_is_nonblocking_shared_or_exclusive(tmp_path):
    with acquire_restore_gate():
        with acquire_restore_gate():
            with pytest.raises(MaintenanceBusy):
                acquire_restore_gate(exclusive=True)
    with acquire_restore_gate(exclusive=True):
        with pytest.raises(MaintenanceBusy):
            acquire_restore_gate()


def test_access_capabilities_distinguish_gate_namespace_entity_and_batch_operation(
    tmp_path,
):
    path = tmp_path / "events.db"
    path.write_bytes(b"database")
    gate = acquire_restore_gate(exclusive=True)
    namespace = maintenance.acquire_database_access(path, exclusive=True)
    entity = acquire_entity_access(observe_entity(path, "file"), exclusive=True)
    try:
        assert gate.kind == "gate" and gate.exclusive
        assert gate.authorizes_registry_write
        assert namespace.kind == "namespace" and namespace.exclusive
        assert not namespace.authorizes_registry_write
        assert entity.kind == "entity" and entity.exclusive
        operation = compose_access(namespace, entity, operation=True)
        try:
            assert operation.operation and operation.authorizes_registry_write
        finally:
            operation.close()
        namespace = None
        entity = None
    finally:
        if entity is not None:
            entity.close()
        if namespace is not None:
            namespace.close()
        gate.close()


def test_composite_close_releases_in_reverse_order_and_stops_on_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "events.db"
    path.write_bytes(b"database")
    identity = observe_entity(path, "file")
    gate = acquire_restore_gate()
    namespace = maintenance.acquire_database_access(path)
    entity = acquire_entity_access(identity)

    original_close = maintenance.DatabaseAccess.close
    close_calls = []

    def reject_once(access):
        close_calls.append(access)
        if access is entity:
            raise maintenance.MaintenanceError("injected close failure")
        original_close(access)

    monkeypatch.setattr(maintenance.DatabaseAccess, "close", reject_once)
    composite = compose_access(gate, namespace, entity)
    with pytest.raises(maintenance.MaintenanceError, match="injected close failure"):
        composite.close()
    assert close_calls == [composite, entity]

    monkeypatch.setattr(maintenance.DatabaseAccess, "close", original_close)
    entity.close()
    composite.close()
    with acquire_restore_gate(exclusive=True):
        pass


def test_zero_length_file_handle_is_real_and_not_a_fabricated_identity(tmp_path):
    path = tmp_path / "empty"
    with path.open("xb"):
        pass
    identity = observe_entity(path, "file")
    assert path.stat().st_size == 0
    assert identity == observe_entity(path, "file")


def test_close_failure_can_be_retried_without_unlocking_live_handle(
    tmp_path, monkeypatch
):
    path = tmp_path / "events.db"
    path.write_bytes(b"database")
    identity = observe_entity(path, "file")
    access = acquire_entity_access(identity, exclusive=True)
    real_file, _unlock = access._parts[0]

    class RejectingFile:
        def close(self):
            raise OSError("injected FileIO close failure")

    access._parts[0] = (RejectingFile(), None)
    with pytest.raises(maintenance.MaintenanceError):
        access.close()
    with pytest.raises(MaintenanceBusy):
        acquire_entity_access(identity)

    access._parts[0] = (real_file, None)
    access.close()
    with acquire_entity_access(identity, exclusive=True):
        pass
