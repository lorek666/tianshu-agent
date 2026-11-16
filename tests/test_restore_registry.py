"""恢复 T1：固定 registry 的严格初始化、观察、追加与坏现场闭锁。"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_restore_contract import UUID, samples

from tianshu import maintenance
from tianshu.maintenance import acquire_database_access, acquire_restore_gate
from tianshu.platform_identity import observe_entity
from tianshu.restore_contract import domain_digest
from tianshu.restore_registry import (
    REGISTRY_SCHEMA,
    RestoreRegistry,
    RestoreRegistryBusy,
    RestoreRegistryCorrupt,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")


@pytest.fixture(autouse=True)
def isolated_lock_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        maintenance, "_lock_root", lambda: tmp_path / "system-lock-root", raising=False
    )


def _registry(tmp_path: Path) -> RestoreRegistry:
    return RestoreRegistry._for_home(tmp_path)


def _child_env(lock_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["TIANSHU_TEST_LOCK_ROOT"] = str(lock_root)
    return env


def _read_json(process: subprocess.Popen[str]) -> dict:
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
        raise AssertionError("registry child did not reach its pipe barrier") from None
    if not line:
        assert process.stderr is not None
        raise AssertionError(f"registry child exited early: {process.stderr.read()}")
    return json.loads(line)


def _spawn(code: str, *args: object, lock_root: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code, *(str(arg) for arg in args)],
        env=_child_env(lock_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def test_absent_observation_does_not_create_authority(tmp_path):
    registry = _registry(tmp_path)
    observation = registry.observe()
    assert observation.present is False
    assert observation.authority_directory == tmp_path / ".tianshu/restore-registry-v1"
    assert not (tmp_path / ".tianshu").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows known-folder locator")
def test_windows_registry_lookup_uses_native_profile_not_environment(
    monkeypatch,
):
    from tianshu.restore_registry import _windows_home

    expected = _windows_home()
    monkeypatch.setenv("HOME", r"C:\wrong-home")
    monkeypatch.setenv("USERPROFILE", r"C:\wrong-profile")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\wrong-local")
    assert _windows_home() == expected
    assert expected.is_dir()
    assert observe_entity(expected, "directory") == observe_entity(
        expected, "directory"
    )


def test_initialization_is_private_canonical_and_identity_bound(tmp_path):
    registry = _registry(tmp_path)
    observation = registry.initialize(registry_id=UUID)
    assert observation.present is True
    assert observation.meta.registry_id == UUID
    assert observation.events == ()
    assert observation.next_sequence == 1
    assert observation.head_sha256 is None
    assert registry.path == tmp_path / ".tianshu/restore-registry-v1/registry.db"
    assert registry.path.is_file()
    assert not list(registry.path.parent.parent.glob(".restore-registry-v1.init-*"))

    actual_home = observe_entity(tmp_path, "directory")
    actual_authority = observe_entity(registry.path.parent, "directory")
    actual_database = observe_entity(registry.path, "file")
    assert observation.meta.home == observation.meta.home.__class__(
        observation.meta.home.path, actual_home
    )
    assert observation.meta.authority_directory.identity == actual_authority
    assert observation.meta.registry_database.identity == actual_database
    if os.name != "nt":
        assert stat.S_IMODE(registry.path.parent.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(registry.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600

    reopened = _registry(tmp_path).observe()
    assert reopened.meta == observation.meta
    assert reopened.head_sha256 == observation.head_sha256
    assert REGISTRY_SCHEMA.encode("utf-8") == (
        ROOT / "schemas/data/restore-registry-v1.sql"
    ).read_bytes()


def test_second_initialization_observes_existing_authority(tmp_path):
    registry = _registry(tmp_path)
    first = registry.initialize(registry_id=UUID)
    second = registry.initialize(registry_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert second.meta == first.meta
    assert second.meta.registry_id == UUID


def test_append_requires_observed_head_and_uses_batch_kind_chain(tmp_path):
    registry = _registry(tmp_path)
    before = registry.initialize(registry_id=UUID)
    event = samples(tmp_path)["e1"]
    namespace = acquire_database_access(tmp_path / "events.db", exclusive=True)
    try:
        with pytest.raises(RestoreRegistryBusy, match="restore gate或批次操作独占"):
            registry.append_event(event, expected=before, operation_access=namespace)
    finally:
        namespace.close()
    gate = acquire_restore_gate(exclusive=True)
    try:
        after = registry.append_event(event, expected=before, operation_access=gate)
    finally:
        gate.close()
    assert after.events == (event,)
    assert after.next_sequence == 2
    assert after.head_sha256 == domain_digest("event", event)

    with pytest.raises(RestoreRegistryBusy):
        namespace = acquire_database_access(tmp_path / "unrelated.db", exclusive=True)
        try:
            second = tmp_path / "second"
            second.mkdir()
            registry.append_event(
                samples(second)["e1"],
                expected=before,
                operation_access=namespace,
            )
        finally:
            namespace.close()


def test_append_rejects_duplicate_kind_and_noncanonical_event(tmp_path):
    registry = _registry(tmp_path)
    before = registry.initialize(registry_id=UUID)
    event = samples(tmp_path)["e1"]
    gate = acquire_restore_gate(exclusive=True)
    try:
        current = registry.append_event(event, expected=before, operation_access=gate)
        assert registry.append_event(event, expected=current, operation_access=gate) == current
        with pytest.raises(RestoreRegistryCorrupt):
            registry.append_event(object(), expected=current, operation_access=gate)
    finally:
        gate.close()


def test_append_reports_commit_without_reobserving_after_unlock(tmp_path, monkeypatch):
    """提交后的返回观察必须来自本事务，不能再去抢一次共享锁。

    并发写入方在 append 提交后可能仍持命名空间独占锁；若 append 末尾重新
    ``observe()``，共享锁争用会被报成 Busy——已提交却报 busy 是假阴性
    （issue #171：两个进程同时 append 同一 head，双方都 busy，但事件已落盘）。
    这里把 ``observe`` 钉成必然 Busy：修复前 append 会传播 Busy，修复后正常
    返回本事务构造的新观察。
    """
    registry = _registry(tmp_path)
    before = registry.initialize(registry_id=UUID)
    event = samples(tmp_path)["e1"]

    def busy_observe(_self):
        raise RestoreRegistryBusy("registry维护锁正在使用中")

    gate = acquire_restore_gate(exclusive=True)
    try:
        monkeypatch.setattr(RestoreRegistry, "observe", busy_observe)
        after = registry.append_event(event, expected=before, operation_access=gate)
    finally:
        gate.close()

    assert after.present is True
    assert after.head_sequence == before.head_sequence + 1
    assert after.events[-1] == event
    assert after.meta == before.meta and after.meta_bytes == before.meta_bytes
    assert after.home_identity == before.home_identity
    # 幂等重复追加仍返回同一观察，且同样不依赖 observe。
    again_gate = acquire_restore_gate(exclusive=True)
    try:
        again = registry.append_event(event, expected=after, operation_access=again_gate)
    finally:
        again_gate.close()
    assert again == after


def test_partial_authority_is_closed_not_reinitialized(tmp_path):
    registry = _registry(tmp_path)
    registry.initialize(registry_id=UUID)
    registry.path.unlink()
    with pytest.raises(RestoreRegistryCorrupt):
        registry.observe()
    with pytest.raises(RestoreRegistryCorrupt):
        registry.initialize(registry_id=UUID)
    assert not any(
        path.name.startswith(".restore-registry-v1.init-")
        for path in (tmp_path / ".tianshu").iterdir()
    )


def test_unknown_or_symlinked_authority_is_closed(tmp_path):
    registry = _registry(tmp_path)
    registry.initialize(registry_id=UUID)
    if os.name == "nt":
        pytest.skip("Windows reparse coverage runs in the native CI probe")
    moved = tmp_path / "moved-registry"
    registry.path.parent.rename(moved)
    registry.path.parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(RestoreRegistryCorrupt):
        registry.observe()


def test_chain_head_event_hash_and_schema_tampering_are_detected(tmp_path):
    registry = _registry(tmp_path)
    before = registry.initialize(registry_id=UUID)
    gate = acquire_restore_gate(exclusive=True)
    try:
        event = samples(tmp_path)["e1"]
        registry.append_event(event, expected=before, operation_access=gate)
    finally:
        gate.close()
    connection = sqlite3.connect(registry.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE restore_events SET sequence=sequence")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM restore_events")
        connection.execute("DROP TRIGGER registry_head_advance_guard")
        connection.execute("UPDATE registry_head SET sequence=99")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RestoreRegistryCorrupt):
        registry.observe()


def test_same_named_trigger_with_altered_ddl_is_still_corrupt(tmp_path):
    registry = _registry(tmp_path)
    registry.initialize(registry_id=UUID)
    connection = sqlite3.connect(registry.path)
    try:
        connection.execute("DROP TRIGGER registry_events_chain_guard")
        connection.execute(
            "CREATE TRIGGER registry_events_chain_guard BEFORE INSERT ON restore_events "
            "BEGIN SELECT 1; END"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RestoreRegistryCorrupt, match="DDL不符"):
        registry.observe()


def test_event_bytes_are_canonical_and_hash_bound(tmp_path):
    registry = _registry(tmp_path)
    before = registry.initialize(registry_id=UUID)
    gate = acquire_restore_gate(exclusive=True)
    try:
        event = samples(tmp_path)["e1"]
        registry.append_event(event, expected=before, operation_access=gate)
    finally:
        gate.close()
    connection = sqlite3.connect(registry.path)
    try:
        raw = connection.execute("SELECT event_bytes FROM restore_events").fetchone()[0]
        connection.execute("DROP TRIGGER registry_events_no_update")
        connection.execute(
            "UPDATE restore_events SET event_bytes=?",
            (raw.replace(b'"version":1', b'"version": 1', 1),),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RestoreRegistryCorrupt):
        registry.observe()


def test_authority_entity_replacement_is_detected_even_with_copied_bytes(tmp_path):
    registry = _registry(tmp_path)
    observation = registry.initialize(registry_id=UUID)
    original = tmp_path / "original-registry"
    registry.path.parent.rename(original)
    registry.path.parent.mkdir()
    shutil.copy2(original / "registry.db", registry.path)
    with pytest.raises(RestoreRegistryCorrupt):
        registry.observe()
    assert observation.meta.registry_database.identity != observe_entity(registry.path, "file")


def test_append_closes_when_fixed_home_identity_disappears(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry(home)
    expected = registry.initialize(registry_id=UUID)
    home.rename(tmp_path / "moved-home")
    gate = acquire_restore_gate(exclusive=True)
    try:
        with pytest.raises(RestoreRegistryBusy, match="registry实体"):
            registry.append_event(
                samples(tmp_path)["e1"],
                expected=expected,
                operation_access=gate,
            )
    finally:
        gate.close()


@pytest.mark.parametrize(
    ("failpoint", "published"),
    [
        ("restore-registry-init-before-file-fsync", False),
        ("restore-registry-init-before-publish", False),
        ("restore-registry-init-after-publish", True),
        ("restore-registry-init-after-parent-fsync", True),
    ],
)
def test_initialization_failpoints_leave_only_interpretable_states(
    tmp_path, failpoint, published
):
    lock_root = tmp_path / "system-lock-root"
    home = tmp_path / "home"
    home.mkdir()
    code = r"""
import sys
from pathlib import Path
from tianshu import maintenance
from tianshu.restore_registry import RestoreRegistry

maintenance._lock_root = lambda: Path(sys.argv[2])
RestoreRegistry._for_home(Path(sys.argv[1])).initialize(
    registry_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
)
"""
    env = _child_env(lock_root)
    env["TIANSHU_FAILPOINT"] = failpoint
    result = subprocess.run(
        [sys.executable, "-u", "-c", code, str(home), str(lock_root)],
        env=env,
        capture_output=True,
        timeout=20,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 77
    registry = _registry(home)
    if published:
        assert registry.observe().present is True
        assert not list(
            (home / ".tianshu").glob(".restore-registry-v1.init-*")
        )
    else:
        assert list((home / ".tianshu").glob(".restore-registry-v1.init-*"))
        with pytest.raises(RestoreRegistryCorrupt, match="初始化现场"):
            registry.observe()
        with pytest.raises(RestoreRegistryCorrupt):
            registry.initialize(registry_id=UUID)


def test_operation_access_is_required_before_any_registry_write(tmp_path):
    registry = _registry(tmp_path)
    expected = registry.initialize(registry_id=UUID)
    with pytest.raises(RestoreRegistryCorrupt):
        registry.append_event(samples(tmp_path)["e1"], expected=expected, operation_access=None)
    assert registry.observe() == expected


def test_two_processes_with_the_same_head_only_commit_one_event(
    tmp_path, monkeypatch
):
    lock_root = tmp_path / "system-lock-root"
    monkeypatch.setattr(maintenance, "_lock_root", lambda: lock_root)
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry(home)
    expected = registry.initialize(registry_id=UUID)
    event_path = tmp_path / "event.json"
    event_path.write_bytes(samples(tmp_path)["e1"].to_bytes())
    code = r"""
import json, os, sys
from pathlib import Path
from tianshu import maintenance
from tianshu.maintenance import acquire_database_access, compose_access
from tianshu.restore_contract import RestoreEventV1
from tianshu.restore_registry import RestoreRegistry, RestoreRegistryBusy

maintenance._lock_root = lambda: Path(os.environ["TIANSHU_TEST_LOCK_ROOT"])
registry = RestoreRegistry._for_home(Path(sys.argv[1]))
event = RestoreEventV1.from_bytes(Path(sys.argv[2]).read_bytes())
expected = registry.observe()
print(json.dumps({"ready": True, "head": expected.head_sha256}), flush=True)
if sys.stdin.readline().strip() != "go":
    raise RuntimeError("CAS barrier not released")
operation = compose_access(
    acquire_database_access(Path(sys.argv[3]), exclusive=True),
    operation=True,
)
try:
    try:
        current = registry.append_event(event, expected=expected, operation_access=operation)
    except RestoreRegistryBusy:
        print(json.dumps({"result": "busy"}), flush=True)
    else:
        print(json.dumps({"result": "committed", "head": current.head_sha256}), flush=True)
finally:
    operation.close()
"""
    first = _spawn(
        code,
        home,
        event_path,
        tmp_path / "operation-first.lock",
        lock_root=lock_root,
    )
    second = None
    processes = []
    try:
        assert _read_json(first) == {
            "ready": True,
            "head": expected.head_sha256,
        }
        second = _spawn(
            code,
            home,
            event_path,
            tmp_path / "operation-second.lock",
            lock_root=lock_root,
        )
        assert _read_json(second) == {
            "ready": True,
            "head": expected.head_sha256,
        }
        processes = [first, second]
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("go\n")
            process.stdin.flush()
        results = [_read_json(process) for process in processes]
        assert sorted(result["result"] for result in results) == ["busy", "committed"]
        for process in processes:
            assert process.communicate(timeout=20)[0] == ""
            assert process.returncode == 0
    finally:
        cleanup = processes or [first, second]
        for process in cleanup:
            if process is None:
                continue
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=20)
    current = registry.observe()
    assert current.events == (samples(tmp_path)["e1"],)


def test_two_process_initialization_competition_creates_one_authority(
    tmp_path
):
    lock_root = tmp_path / "system-lock-root"
    home = tmp_path / "home"
    home.mkdir()
    code = r"""
import json, os, sys
from pathlib import Path
from tianshu import maintenance, restore_registry
from tianshu.maintenance import MaintenanceBusy

maintenance._lock_root = lambda: Path(os.environ["TIANSHU_TEST_LOCK_ROOT"])
original = restore_registry.acquire_restore_gate
def paused_gate(*, exclusive=False):
    gate = original(exclusive=exclusive)
    print(json.dumps({"ready": True}), flush=True)
    if sys.stdin.readline().strip() != "go":
        gate.close()
        raise RuntimeError("initialization barrier not released")
    return gate
restore_registry.acquire_restore_gate = paused_gate
registry = restore_registry.RestoreRegistry._for_home(Path(sys.argv[1]))
try:
    current = registry.initialize(registry_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
except MaintenanceBusy:
    print(json.dumps({"result": "busy"}), flush=True)
else:
    print(json.dumps({"result": "present", "registry_id": current.meta.registry_id}), flush=True)
"""
    first = _spawn(code, home, lock_root=lock_root)
    try:
        assert _read_json(first) == {"ready": True}
        second = subprocess.run(
            [sys.executable, "-u", "-c", code, str(home)],
            env=_child_env(lock_root),
            input="",
            capture_output=True,
            timeout=20,
            text=True,
            encoding="utf-8",
        )
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout.splitlines()[-1]) == {"result": "busy"}
        assert not (home / ".tianshu/restore-registry-v1").exists()
        assert first.stdin is not None
        first.stdin.write("go\n")
        first.stdin.flush()
        created = _read_json(first)
        assert created == {
            "result": "present",
            "registry_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
        assert first.communicate(timeout=20)[0] == ""
        assert first.returncode == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=20)

    reopened = _registry(home).initialize(
        registry_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    assert reopened.meta.registry_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert not list(
        (home / ".tianshu").glob(".restore-registry-v1.init-*")
    )


def test_observation_is_immediately_busy_while_another_process_owns_write_access(
    tmp_path,
):
    lock_root = tmp_path / "system-lock-root"
    home = tmp_path / "home"
    home.mkdir()
    registry = _registry(home)
    registry.initialize(registry_id=UUID)
    code = r"""
import json, sqlite3, sys
from pathlib import Path
from tianshu import maintenance
from tianshu.maintenance import acquire_database_access, acquire_entity_access
from tianshu.platform_identity import observe_entity

maintenance._lock_root = lambda: Path(sys.argv[2])
path = Path(sys.argv[1])
namespace = acquire_database_access(path, exclusive=True)
entity = acquire_entity_access(observe_entity(path, "file"), exclusive=True)
connection = sqlite3.connect(path)
connection.execute("PRAGMA busy_timeout=0")
connection.execute("BEGIN IMMEDIATE")
print(json.dumps({"ready": True}), flush=True)
if sys.stdin.readline().strip() != "commit":
    raise RuntimeError("registry writer barrier not released")
connection.execute("COMMIT")
connection.close()
entity.close()
namespace.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(registry.path), str(lock_root)],
        env=_child_env(lock_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert _read_json(process) == {"ready": True}
        with pytest.raises(RestoreRegistryBusy):
            registry.observe()
        assert process.stdin is not None
        process.stdin.write("commit\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=20)
        assert (stdout, stderr, process.returncode) == ("", "", 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=20)
    assert registry.observe().present is True
