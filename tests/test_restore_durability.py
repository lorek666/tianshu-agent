"""Managed restore must refuse before changing data when its durability is unavailable."""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from restore_test_support import record_reserved_batch, requires_managed_restore_writes
from support import CHILD_TEXT

from tianshu import restore_manager
from tianshu.backup import RestoreFailed, create_backup, restore_backup
from tianshu.cli import main
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.restore_access import RestoreScopeBusy
from tianshu.restore_contract import RestoreResponseV1, domain_digest
from tianshu.restore_registry import RestoreRegistry


def _back_up(tmp_path: Path) -> tuple[Path, Path]:
    root = (tmp_path / "data").resolve()
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        build_stack(log, data_root=root)
    finally:
        log.close()
    backup = tmp_path / "backup"
    create_backup(root, backup)
    shutil.rmtree(root)
    return backup, root


def _snapshot(root: Path) -> dict[Path, tuple[int, int, int, bytes | None]]:
    snapshot = {}
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        snapshot[path.relative_to(root)] = (
            info.st_mode,
            info.st_ino,
            info.st_mtime_ns,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )
    return snapshot


def _child(*args: object, failpoint: str | None = None) -> subprocess.CompletedProcess[str]:
    # conftest's startup hook pins the child's registry and maintenance locks.
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("TIANSHU_") and not key.endswith(("BASE_URL", "API_KEY"))
    }
    for key in ("TIANSHU_TEST_LOCK_ROOT", "TIANSHU_AUTHORITY_ROOT"):
        if key in os.environ:
            env[key] = os.environ[key]
    if failpoint is not None:
        env["TIANSHU_FAILPOINT"] = failpoint
    return subprocess.run(
        [sys.executable, "-m", "tianshu", "data", *(str(arg) for arg in args)],
        env=env, capture_output=True, timeout=45, **CHILD_TEXT,
    )


@pytest.fixture
def _unavailable_cli(monkeypatch, capsys):
    # Native Windows exercises the actual CLI process with no platform patch.
    # On POSIX this tests the same policy; it is not evidence of Windows durability.
    if os.name != "nt":
        monkeypatch.setattr(
            restore_manager, "_managed_restore_writes_supported", lambda: False,
            raising=False,
        )

    def invoke(*args: object) -> tuple[int, str]:
        if os.name == "nt":
            proc = _child(*args)
            return proc.returncode, proc.stdout
        code = main(["data", *(str(arg) for arg in args)])
        return code, capsys.readouterr().out

    return invoke


@pytest.mark.parametrize("target_exists", [False, True], ids=["absent", "empty"])
@pytest.mark.parametrize("entrypoint", ["cli", "facade"])
def test_unavailable_apply_does_not_create_registry_or_modify_target(
    tmp_path, _unavailable_cli, target_exists, entrypoint,
):
    backup, root = _back_up(tmp_path)
    if target_exists:
        # The facade accepts strictly empty roots; the CLI also allows skeletons.
        clean_target = root / "parent" / "child" if entrypoint == "cli" else root
        clean_target.mkdir(parents=True)
    registry = RestoreRegistry()
    before_data = _snapshot(tmp_path)
    before_home = _snapshot(registry.home)
    assert not registry.observe().present

    code, stdout = _unavailable_cli("restore-plan", backup, "--data-root", root, "--json")
    assert code == 3, stdout
    planned = json.loads(stdout)
    assert planned["ok"] is True
    assert planned["apply_supported"] is False
    plan_sha = planned["plan_sha256"]
    assert len(plan_sha) == 64

    if entrypoint == "cli":
        code, stdout = _unavailable_cli(
            "restore-apply", backup, "--data-root", root, "--expect-plan", plan_sha, "--json",
        )
        assert code == 2, stdout
        response = RestoreResponseV1.from_bytes(stdout.strip().encode("utf-8"))
        assert response.operation == "restore-apply"
        assert response.ok is False and response.result is None
        failure = response.error
    else:
        with pytest.raises(RestoreFailed) as caught:
            restore_backup(backup, root)
        assert isinstance(caught.value.__cause__, restore_manager.RestoreOperationError)
        failure = caught.value.__cause__.failure
    assert failure is not None
    assert failure.category == "durability_unavailable"
    assert failure.batch_id is None and failure.phase is None
    assert _snapshot(tmp_path) == before_data
    assert _snapshot(registry.home) == before_home
    assert not registry.observe().present


@pytest.mark.parametrize("operation", ["restore-resume", "restore-abort"])
def test_unavailable_reserved_reentry_changes_no_registry_or_data(
    tmp_path, _unavailable_cli, operation,
):
    backup, root = _back_up(tmp_path)
    registry = RestoreRegistry()
    planned = restore_manager.plan(backup, root, registry=registry)
    batch_id = record_reserved_batch(registry, planned)
    before = registry.observe()
    assert before.events[-1].kind == "reserved"
    before_data = _snapshot(tmp_path)
    before_home = _snapshot(registry.home)

    code, stdout = _unavailable_cli(operation, "--data-root", root, "--batch", batch_id, "--json")
    assert code == 2, stdout
    response = RestoreResponseV1.from_bytes(stdout.strip().encode("utf-8"))
    assert response.operation == operation
    assert response.ok is False and response.result is None
    assert response.error is not None
    assert response.error.category == "durability_unavailable"
    assert response.error.batch_id == batch_id
    assert response.error.phase == "reserved"
    assert response.error.plan_sha256 == planned.plan_sha256
    assert registry.observe() == before
    assert _snapshot(tmp_path) == before_data
    assert _snapshot(registry.home) == before_home


def _published(tmp_path: Path) -> tuple[Path, str, str]:
    backup, root = _back_up(tmp_path)
    planned = restore_manager.plan(backup, root)
    applied = restore_manager.apply(backup, root, expect_plan_sha256=planned.plan_sha256)
    assert applied.phase == "published_hold"
    return root, applied.batch_id, restore_manager.review(root).review_sha256


def _release_args(root: Path, batch_id: str, review_sha: str) -> tuple[object, ...]:
    return (
        "restore-release", "--data-root", root, "--batch", batch_id,
        "--expect-review", review_sha, "--acknowledge-unproven-later-history", "--json",
    )


def _release(root: Path, batch_id: str, review_sha: str):
    return restore_manager.release(
        root, batch_id=batch_id, expect_review_sha256=review_sha,
        acknowledge_unproven_later_history=True,
    )


def _crash_after_release_intent(root: Path, batch_id: str, review_sha: str) -> None:
    crashed = _child(*_release_args(root, batch_id, review_sha), failpoint="FP-RELEASE-INTENT")
    assert crashed.returncode == 77, (crashed.stdout, crashed.stderr)
    assert "[failpoint] FP-RELEASE-INTENT\n" in crashed.stderr


@requires_managed_restore_writes
@pytest.mark.parametrize("phase", ["published_hold", "release_intent"])
def test_unavailable_release_preserves_the_authoritative_hold(tmp_path, monkeypatch, phase):
    root, batch_id, review_sha = _published(tmp_path)
    if phase == "release_intent":
        _crash_after_release_intent(root, batch_id, review_sha)
    registry = RestoreRegistry()
    before = registry.observe()
    assert before.events[-1].kind == phase
    before_data = _snapshot(tmp_path)
    before_home = _snapshot(registry.home)
    monkeypatch.setattr(restore_manager, "_managed_restore_writes_supported", lambda: False)

    if phase == "published_hold":
        assert restore_manager.review(root).review_sha256 == review_sha
        observation = restore_manager.resume(root, batch_id=batch_id)
        assert observation.phase == "published_hold" and observation.held
    with pytest.raises(restore_manager.RestoreOperationError) as caught:
        _release(root, batch_id, review_sha)
    assert caught.value.failure.category == "durability_unavailable"
    assert caught.value.failure.batch_id == batch_id
    assert caught.value.failure.phase == phase
    with pytest.raises(RestoreScopeBusy):
        EventLog(root / "events.db", blob_dir=root / "blobs")
    assert registry.observe() == before
    assert _snapshot(tmp_path) == before_data
    assert _snapshot(registry.home) == before_home


@requires_managed_restore_writes
def test_unavailable_release_still_acknowledges_committed_terminal_state(tmp_path, monkeypatch):
    root, batch_id, review_sha = _published(tmp_path)
    released = _release(root, batch_id, review_sha)
    assert released.phase == "released" and not released.held
    registry = RestoreRegistry()
    before = registry.observe()
    before_data = _snapshot(tmp_path)
    before_home = _snapshot(registry.home)
    monkeypatch.setattr(restore_manager, "_managed_restore_writes_supported", lambda: False)

    assert _release(root, batch_id, review_sha) == released
    assert registry.observe() == before
    assert _snapshot(tmp_path) == before_data
    assert _snapshot(registry.home) == before_home


@requires_managed_restore_writes
def test_release_directory_fsync_failure_after_r2_retains_hold_until_retry(tmp_path, monkeypatch):
    root, batch_id, review_sha = _published(tmp_path)
    _crash_after_release_intent(root, batch_id, review_sha)
    registry = RestoreRegistry()
    before = registry.observe()
    intent = before.events[-1]
    assert intent.kind == "release_intent"
    intent_sha = domain_digest("event", intent)
    real_fsync = os.fsync
    root_stat = root.stat()
    injected = 0

    def fail_fsync(fd: int) -> None:
        nonlocal injected
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and os.path.samestat(info, root_stat):
            injected += 1
            raise OSError(errno.EIO, "injected release directory fsync failure")
        real_fsync(fd)

    db = root / "events.db"
    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail_fsync)
        for attempt in (1, 2):
            with pytest.raises(restore_manager.RestoreOperationError) as caught:
                _release(root, batch_id, review_sha)
            assert caught.value.failure.category == "durability_unavailable"
            assert caught.value.failure.phase == "release_intent"
            assert injected == attempt
            assert registry.observe() == before
            with pytest.raises(RestoreScopeBusy):
                EventLog(db, blob_dir=root / "blobs")
            conn = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                assert conn.execute(
                    "SELECT release_intent_sha256 FROM _restore_release_r2_proof WHERE singleton=1"
                ).fetchone() == (intent_sha,)
            finally:
                conn.close()

    released = _release(root, batch_id, review_sha)
    assert released.phase == "released" and not released.held
    after = registry.observe()
    assert after.events[:-1] == before.events
    assert after.events[-1].kind == "released"
    assert _release(root, batch_id, review_sha) == released
    assert registry.observe() == after
