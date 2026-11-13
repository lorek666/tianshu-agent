"""维护准入：真实共享锁、独占锁与 EventLog 的连接存活期。

跨进程测试通过 stdin/stdout 屏障排序，不靠 sleep 猜测锁是否已经取得。
Windows 与 POSIX 跑相同的锁核心判据；POSIX uid/mode 与链接能力单独标明。
"""

from __future__ import annotations

import errno
import gc
import json
import os
import queue
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import CHILD_TEXT

from tianshu import events, maintenance
from tianshu.errors import EventLogCorrupt
from tianshu.events import EventLog
from tianshu.maintenance import (
    MaintenanceBusy,
    MaintenanceConnection,
    MaintenanceError,
    acquire_database_access,
    database_lock_path,
)


@pytest.fixture(autouse=True)
def isolated_lock_root(tmp_path, monkeypatch):
    """测试只在临时目录写锁；子进程显式注入同一个根，不改产品选根规则。"""
    original = getattr(maintenance, "_lock_root", None)
    root = tmp_path / "system-lock-root"
    monkeypatch.setattr(maintenance, "_lock_root", lambda: root, raising=False)
    return root, original


SRC = str(Path(__file__).resolve().parents[1] / "src")
HOLDER = """
import json
import sys
from pathlib import Path
from tianshu import maintenance
from tianshu.crashpoint import failpoint
from tianshu.events import EventLog
from tianshu.maintenance import acquire_database_access

maintenance._lock_root = lambda: Path(sys.argv[3])
db = Path(sys.argv[1])
mode = sys.argv[2]
if mode == "eventlog":
    access = EventLog(db)
else:
    access = acquire_database_access(db, exclusive=mode == "exclusive")
print(json.dumps({"ready": True, "mode": mode}), flush=True)
action = sys.stdin.readline().strip()
if action == "crash":
    failpoint("maintenance-test-held")
    raise RuntimeError("expected hard-exit failpoint did not fire")
access.close()
print(json.dumps({"closed": True}), flush=True)
"""


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
        raise AssertionError("holder did not reach the pipe barrier") from None
    if not line:
        assert process.stderr is not None
        raise AssertionError(f"holder exited before barrier: {process.stderr.read()}")
    return json.loads(line)


@contextmanager
def _holder(db: Path, mode: str):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["TIANSHU_FAILPOINT"] = "maintenance-test-held"
    process = subprocess.Popen(
        [
            sys.executable, "-u", "-c", HOLDER, str(db), mode,
            str(maintenance._lock_root()),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **CHILD_TEXT,
    )
    try:
        assert _line(process) == {"ready": True, "mode": mode}
        yield process
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


def _release(process: subprocess.Popen[str], action: str = "close") -> int:
    assert process.stdin is not None
    process.stdin.write(action + "\n")
    process.stdin.flush()
    if action == "close":
        assert _line(process) == {"closed": True}
    return process.wait(timeout=20)


def test_lock_identity_is_canonical_and_outside_replaceable_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    db = root / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    assert not lock.is_relative_to(root)
    assert lock == database_lock_path(root / "unused" / ".." / "events.db")
    with acquire_database_access(db, exclusive=True):
        root.rename(tmp_path / "old-data")
        root.mkdir()
        assert database_lock_path(db) == lock
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db)
    with acquire_database_access(db, exclusive=True):
        pass


def test_writable_database_does_not_require_ancestor_write_access(tmp_path):
    ancestor = tmp_path / "restricted-parent"
    root = ancestor / "writable-database-directory"
    root.mkdir(parents=True)
    db = root / "custom.db"
    connection = sqlite3.connect(db)
    connection.executescript(events._SCHEMA)
    connection.close()
    ancestor.chmod(0o555)
    try:
        if os.name != "nt" and os.geteuid() != 0:
            with pytest.raises(PermissionError):
                (ancestor / "must-not-be-created").touch()
        # SQLite 本身仍可写；维护准入不应要求给上级目录放宽权限。
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE writable_probe (value TEXT)")
        connection.commit()
        connection.close()
        log = EventLog(db)
        try:
            log.append("r1", "run.start", {"task": "usable"}, legacy_write=True)
        finally:
            log.close()
        assert not (ancestor / ".tianshu-maintenance").exists()
    finally:
        ancestor.chmod(0o755)


def test_os_lock_identity_ignores_mutable_environment(
    tmp_path, monkeypatch, isolated_lock_root,
):
    _root, original = isolated_lock_root
    assert original is not None, "stable OS lock root is not implemented"
    monkeypatch.setattr(maintenance, "_lock_root", original)
    db = tmp_path / "data" / "events.db"
    before = database_lock_path(db)
    expected_root = original()
    elsewhere = tmp_path / "other-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for key in ("HOME", "TMPDIR", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.setenv(key, str(elsewhere / key.lower()))
    assert original() == expected_root
    assert database_lock_path(db) == before
    if os.name != "nt":
        assert expected_root == Path("/tmp") / f"tianshu-maintenance-{os.getuid()}"


def test_windows_folder_api_mock_contract_uses_os_known_directory(tmp_path, monkeypatch):
    """调用约定 mock，不把它计作 Windows 原生 API 实测。"""
    import ctypes
    from ctypes import wintypes

    calls = []
    known = tmp_path / "os-known-local-appdata"

    def get_folder(hwnd, csidl, token, flags, buffer):
        calls.append((hwnd, csidl, token, flags, len(buffer)))
        buffer.value = str(known)
        return 0

    def load_library(name, *, use_last_error):
        assert name == "shell32"
        assert use_last_error is True
        return SimpleNamespace(SHGetFolderPathW=get_folder)

    monkeypatch.setattr(ctypes, "WinDLL", load_library, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "fake-env-local-appdata"))
    assert maintenance._windows_lock_root() == known / "Temp" / "tianshu-maintenance"
    assert calls == [(None, 0x001C, None, 0, 260)]
    assert get_folder.restype is wintypes.LONG
    assert get_folder.argtypes == [
        wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ]
    assert not known.exists()


@pytest.mark.parametrize("result", [-2147024891, 1, 0])
def test_windows_folder_api_mock_failure_has_no_environment_fallback(
    tmp_path, monkeypatch, result,
):
    import ctypes

    def get_folder(*args):
        return result  # 失败或空路径，均不能改用 TEMP/HOME。

    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *a, **kw: SimpleNamespace(SHGetFolderPathW=get_folder),
        raising=False,
    )
    monkeypatch.setattr(maintenance, "_lock_root", maintenance._windows_lock_root)
    monkeypatch.setenv("TEMP", str(tmp_path / "must-not-be-used"))
    with pytest.raises(MaintenanceError) as caught:
        database_lock_path(tmp_path / "data" / "events.db")
    assert isinstance(caught.value.__cause__, OSError)
    assert "SHGetFolderPathW" in str(caught.value.__cause__)
    assert not (tmp_path / "must-not-be-used").exists()


def test_lock_root_rejects_symlink_or_windows_junction(tmp_path):
    db = tmp_path / "data" / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    target = tmp_path / "redirected-locks"
    target.mkdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(lock.parent), str(target)],
            check=True, capture_output=True, **CHILD_TEXT,
        )
    else:
        lock.parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(MaintenanceError, match="维护锁目录"):
        acquire_database_access(db)
    assert list(target.iterdir()) == []
    assert not db.parent.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX uid; Windows inherits OS user-directory ACL")
def test_posix_lock_root_rejects_another_owner(tmp_path, monkeypatch):
    db = tmp_path / "data" / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    lock.parent.mkdir()
    original_lstat = Path.lstat

    def foreign_owner(path):
        result = original_lstat(path)
        if path == lock.parent:
            fields = list(result)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    with pytest.raises(MaintenanceError, match="维护锁目录"):
        acquire_database_access(db)
    assert list(lock.parent.iterdir()) == []


def test_lock_metadata_uses_private_posix_modes(tmp_path):
    db = tmp_path / "data" / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    lock.parent.mkdir(mode=0o755)
    lock.write_bytes(b"")
    with acquire_database_access(db):
        if os.name != "nt":
            assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_symbolic_link_alias_uses_the_same_lock(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"host cannot create directory symlinks: {exc}")
    assert database_lock_path(root / "events.db") == database_lock_path(
        alias / "events.db"
    )
    with acquire_database_access(root / "events.db", exclusive=True):
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(alias / "events.db")


def test_same_process_shared_connections_do_not_release_each_other(tmp_path):
    db = tmp_path / "data" / "events.db"
    first = acquire_database_access(db)
    second = acquire_database_access(db)
    try:
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        first.close()
        first.close()
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
    finally:
        first.close()
        second.close()
    with acquire_database_access(db, exclusive=True):
        pass


@pytest.mark.parametrize("mode", ["shared", "eventlog"])
def test_true_processes_can_hold_shared_access_together(tmp_path, mode):
    db = tmp_path / "data" / "events.db"
    with _holder(db, mode) as first, _holder(db, mode) as second:
        with acquire_database_access(db):
            with pytest.raises(MaintenanceBusy):
                acquire_database_access(db, exclusive=True)
        assert _release(first) == 0
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        assert _release(second) == 0
        with acquire_database_access(db, exclusive=True):
            pass


@pytest.mark.parametrize("mode", ["shared", "eventlog"])
def test_live_process_blocks_exclusive_until_close(tmp_path, mode):
    db = tmp_path / "data" / "events.db"
    with _holder(db, mode) as process:
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        assert _release(process) == 0
        with acquire_database_access(db, exclusive=True):
            pass


def test_maintenance_process_blocks_writer_before_any_database_creation(tmp_path):
    root = tmp_path / "data"
    db = root / "events.db"
    with _holder(db, "exclusive") as process:
        with pytest.raises(MaintenanceBusy):
            EventLog(db, blob_dir=root / "artifacts" / "blobs")
        assert not root.exists()
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db)
        assert _release(process) == 0
        log = EventLog(db)
        log.close()


@pytest.mark.parametrize("mode", ["shared", "exclusive", "eventlog"])
def test_hard_exit_releases_os_access(tmp_path, mode):
    db = tmp_path / "data" / "events.db"
    with _holder(db, mode) as process:
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
        assert _release(process, "crash") == 77
        with acquire_database_access(db, exclusive=True):
            pass


def test_two_live_eventlogs_hold_shared_access_independently(tmp_path):
    db = tmp_path / "data" / "events.db"
    first = EventLog(db)
    second = EventLog(db)
    try:
        first.close()
        with pytest.raises(MaintenanceBusy):
            acquire_database_access(db, exclusive=True)
    finally:
        first.close()
        second.close()
    with acquire_database_access(db, exclusive=True):
        pass


def test_corrupt_database_construction_releases_access(tmp_path):
    db = tmp_path / "data" / "events.db"
    db.parent.mkdir()
    db.write_bytes(b"not a SQLite database")
    with pytest.raises(EventLogCorrupt):
        EventLog(db)
    with acquire_database_access(db, exclusive=True):
        assert db.read_bytes() == b"not a SQLite database"


def test_failed_schema_construction_releases_access(tmp_path, monkeypatch):
    db = tmp_path / "data" / "events.db"
    monkeypatch.setattr(events, "_SCHEMA", "THIS IS NOT SQL")
    with pytest.raises(sqlite3.OperationalError):
        EventLog(db)
    with acquire_database_access(db, exclusive=True):
        pass


def test_failed_material_directory_construction_releases_access(tmp_path):
    db = tmp_path / "data" / "events.db"
    bad_blobs = tmp_path / "bad-blobs"
    bad_blobs.write_bytes(b"a file, not a directory")
    with pytest.raises(FileExistsError):
        EventLog(db, blob_dir=bad_blobs)
    with acquire_database_access(db, exclusive=True):
        pass


def test_failed_sqlite_connect_releases_access(tmp_path, monkeypatch):
    db = tmp_path / "data" / "events.db"

    def reject(*args, **kwargs):
        raise sqlite3.OperationalError("injected connection failure")

    monkeypatch.setattr(events.sqlite3, "connect", reject)
    with pytest.raises(sqlite3.OperationalError, match="injected connection failure"):
        EventLog(db)
    with acquire_database_access(db, exclusive=True):
        pass


def test_constructor_close_failure_keeps_retained_connection_protected(
    tmp_path, monkeypatch,
):
    db = tmp_path / "data" / "events.db"
    retained = []
    original_close = MaintenanceConnection.close

    def reject_close(connection):
        retained.append(connection)
        raise sqlite3.OperationalError("injected close failure")

    monkeypatch.setattr(events, "_SCHEMA", "THIS IS NOT SQL")
    monkeypatch.setattr(MaintenanceConnection, "close", reject_close)
    with pytest.raises(sqlite3.OperationalError, match="injected close failure") as caught:
        EventLog(db)
    assert isinstance(caught.value.__context__, sqlite3.OperationalError)
    connection = retained[0]
    connection.execute("CREATE TABLE still_writable (value TEXT)")
    with pytest.raises(MaintenanceBusy):
        acquire_database_access(db, exclusive=True)
    original_close(connection)
    with acquire_database_access(db, exclusive=True):
        pass


@pytest.mark.parametrize("direct_connection", [False, True])
def test_wrong_thread_close_keeps_live_connection_protected(tmp_path, direct_connection):
    db = tmp_path / "data" / "events.db"
    log = EventLog(db)
    errors = []

    def wrong_thread_close():
        try:
            if direct_connection:
                log.conn.close()
            else:
                log.close()
        except sqlite3.ProgrammingError as exc:
            errors.append(exc)

    thread = threading.Thread(target=wrong_thread_close)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert len(errors) == 1
    log.append(
        "r1", "run.start", {"task": "still writable"},
        actor_kind="core", legacy_write=True,
    )
    with pytest.raises(MaintenanceBusy):
        acquire_database_access(db, exclusive=True)
    log.close()
    with acquire_database_access(db, exclusive=True):
        pass


def test_external_connection_keeps_access_after_eventlog_collection(tmp_path):
    db = tmp_path / "data" / "events.db"
    log = EventLog(db)
    connection = log.conn
    del log
    gc.collect()
    connection.execute("CREATE TABLE still_writable (value TEXT)")
    with pytest.raises(MaintenanceBusy):
        acquire_database_access(db, exclusive=True)
    connection.close()
    with acquire_database_access(db, exclusive=True):
        pass


def test_external_cursor_keeps_access_after_eventlog_collection(tmp_path):
    db = tmp_path / "data" / "events.db"
    log = EventLog(db)
    cursor = log.conn.cursor()
    del log
    gc.collect()
    cursor.execute("CREATE TABLE still_writable (value TEXT)")
    with pytest.raises(MaintenanceBusy):
        acquire_database_access(db, exclusive=True)
    cursor.connection.close()
    with acquire_database_access(db, exclusive=True):
        pass


def test_collected_connection_releases_access(tmp_path):
    db = tmp_path / "data" / "events.db"
    log = EventLog(db)
    del log
    gc.collect()
    with acquire_database_access(db, exclusive=True):
        pass


def test_memory_database_needs_no_persistent_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert database_lock_path(Path(":memory:")) is None
    with acquire_database_access(Path(":memory:"), exclusive=True):
        with acquire_database_access(Path(":memory:")):
            log = EventLog(Path(":memory:"))
            log.close()
    assert list(tmp_path.iterdir()) == []


def test_invalid_lock_location_is_an_error_not_contention(tmp_path):
    db = tmp_path / "data" / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    lock.parent.mkdir(parents=True)
    lock.mkdir()
    with pytest.raises(MaintenanceError) as caught:
        acquire_database_access(db)
    assert not isinstance(caught.value, MaintenanceBusy)
    assert caught.value.__cause__ is not None
    shutil.rmtree(lock)


def test_invalid_lock_directory_is_an_error_not_contention(tmp_path):
    db = tmp_path / "data" / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    lock.parent.write_bytes(b"a file, not a lock directory")
    with pytest.raises(MaintenanceError) as caught:
        acquire_database_access(db)
    assert not isinstance(caught.value, MaintenanceBusy)
    assert isinstance(caught.value.__cause__, FileExistsError)
    assert not db.parent.exists()


@pytest.mark.parametrize("phase", ["mkdir", "open"])
def test_lock_permission_errors_are_not_contention(tmp_path, monkeypatch, phase):
    db = tmp_path / "data" / "events.db"
    lock = database_lock_path(db)
    assert lock is not None
    denied = PermissionError(errno.EACCES, "injected lock permission failure")

    def reject(*args, **kwargs):
        raise denied

    with monkeypatch.context() as patch:
        if phase == "mkdir":
            patch.setattr(Path, "mkdir", reject)
        else:
            patch.setattr(maintenance.os, "open", reject)
        with pytest.raises(MaintenanceError) as caught:
            acquire_database_access(db)
    assert not isinstance(caught.value, MaintenanceBusy)
    assert caught.value.__cause__ is denied
    assert not db.parent.exists()
