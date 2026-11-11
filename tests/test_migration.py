"""W4 验收：版本化迁移与中断恢复（PRD §5.3.5 第 3 条）。

要证明的事：

1. 结构版本号是唯一权威：每步 DDL 与 `PRAGMA user_version` 在**同一个事务**
   里提交，不存在"表改了版本没改"的混合状态。
2. 迁移前必须有 `verify_backup()` 通过的备份；没有备份、备份坏了、有活着
   写者（或判定不了）一律拒绝，且**不动库**。
3. 每步前/后都能被 `os._exit` 打断（子进程真实崩溃），重启后能续做；
   最终版本一致、历史事件一个字节都没变、事件 append-only 触发器仍在。
4. 不支持/更新版本的库：拒绝写入并给恢复路径；`migration.json` 损坏时
   不猜，直接拒绝。

负控（回退后本文件必须红，见 docs/backup-restore.md）：去掉备份前置检查 →
`test_migrate_requires_verified_backup` 红；把每步改成"先删后建"（或去掉
user_version 同事务提交）→ 中断续做/版本一致性用例红。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import migrate
from tianshu.backup import create_backup
from tianshu.events import EventLog
from tianshu.migrate import (
    SCHEMA_VERSION,
    MigrationIncomplete,
    MigrationRefused,
    MigrationStateCorrupt,
    UnsupportedSchemaVersion,
    assert_writable,
    plan_migrations,
    read_schema_version,
    read_state,
    writers_active,
)
from tianshu.migrate import migrate as run_migration

SRC = str(Path(__file__).resolve().parents[1] / "src")


# ---------------------------------------------------------------- 夹具


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "artifacts").mkdir(parents=True)
    log = EventLog(root / "events.db", blob_dir=root / "artifacts" / "blobs")
    log.append("r1", "run.start", {
        "task": "迁移夹具", "workspace": str(tmp_path), "out": "x.md",
        "limits": {}, "tools": []}, actor_kind="core", legacy_write=True)
    log.append("r1", "tool.intent", {
        "call_id": "c1", "tool_id": "read_text", "args": {"path": "a.txt"},
        "args_hash": "0" * 64}, actor_kind="core", legacy_write=True)
    log.close()
    return root


def _make_backup(root: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "backup" if tmp_path.name != "backup" else tmp_path
    create_backup(root, dest)
    return dest


def _event_digest(db: Path) -> str:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT seq, run_id, type, payload_hash FROM events ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _migration_subprocess(
    root: Path, backup: Path, failpoint: str
) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from pathlib import Path\n"
        "from tianshu.migrate import migrate\n"
        f"migrate(Path({str(root)!r}), backup=Path({str(backup)!r}))\n"
    )
    env = dict(os.environ)
    env.pop("TIANSHU_FAILPOINT", None)
    env.pop("TIANSHU_FAILPOINT_PATH", None)
    env["TIANSHU_FAILPOINT"] = failpoint
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True,
        timeout=120, **CHILD_TEXT,
    )


# ---------------------------------------------------------------- 正常路径


def test_migrate_is_versioned_idempotent_and_never_touches_events(tmp_path):
    root = _make_root(tmp_path)
    db = root / "events.db"
    digest_before = _event_digest(db)
    backup = _make_backup(root, tmp_path)
    assert read_schema_version(db) == 0

    report = run_migration(root, backup=backup)

    assert report.from_version == 0
    assert report.to_version == SCHEMA_VERSION
    assert report.applied_steps == [step.to_version for step in migrate.STEPS]
    assert report.resumed is False
    assert read_schema_version(db) == SCHEMA_VERSION
    # 步骤真的落地：有 migration_log、v2 的 note 列也在
    assert "migration_log" in _tables(db)
    assert "note" in _columns(db, "migration_log")
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT step FROM migration_log ORDER BY step").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [1, 2]
    # 历史事件一个字节没变，append-only 触发器还在
    assert _event_digest(db) == digest_before
    conn = sqlite3.connect(db)
    try:
        triggers = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        conn.close()
    assert {"events_no_update", "events_no_delete"} <= triggers
    with pytest.raises(sqlite3.IntegrityError):
        sqlite3.connect(db).execute("UPDATE events SET actor_id='x'")
    # 进度文件完成，写入门放行
    state = read_state(root)
    assert state is not None and state.status == "completed"
    assert state.target_version == SCHEMA_VERSION
    assert assert_writable(root) == SCHEMA_VERSION

    # 幂等：再跑一次不重复应用
    digest_after = _event_digest(db)
    again = run_migration(root, backup=backup)
    assert again.already_current is True
    assert again.applied_steps == []
    assert read_schema_version(db) == SCHEMA_VERSION
    assert _event_digest(db) == digest_after


def test_migrate_requires_verified_backup(tmp_path):
    root = _make_root(tmp_path)
    db = root / "events.db"

    # 没有备份路径
    with pytest.raises(MigrationRefused) as excinfo:
        run_migration(root, backup=tmp_path / "no-such-backup")
    assert "备份" in str(excinfo.value)
    # 库完全没动
    assert read_schema_version(db) == 0
    assert "migration_log" not in _tables(db)
    assert read_state(root) is None

    # 备份损坏（缺文件）→ 明确拒绝，给补救路径
    backup = _make_backup(root, tmp_path)
    victim = backup / "events.db"  # 先删事件库：最常见的"备份不完整"形态
    victim.unlink()
    with pytest.raises(MigrationRefused) as excinfo2:
        run_migration(root, backup=backup)
    assert "校验" in str(excinfo2.value) or "备份" in str(excinfo2.value)
    assert read_schema_version(db) == 0
    assert "migration_log" not in _tables(db)


def test_migrate_refuses_active_or_unknown_writer(tmp_path):
    root = _make_root(tmp_path)
    backup = _make_backup(root, tmp_path)
    run_dir = root / "run"
    run_dir.mkdir()
    lock = run_dir / "daemon.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": 1}),
                    encoding="utf-8")
    assert writers_active(root) is True
    with pytest.raises(MigrationRefused) as excinfo:
        run_migration(root, backup=backup)
    assert "写者" in str(excinfo.value)
    assert read_schema_version(root / "events.db") == 0

    # 锁文件读不懂 = unknown：同样拒绝，不猜"没有写者"
    lock.write_text("{broken", encoding="utf-8")
    assert writers_active(root) is None
    with pytest.raises(MigrationRefused) as excinfo2:
        run_migration(root, backup=backup)
    assert "无法判定" in str(excinfo2.value)
    assert read_schema_version(root / "events.db") == 0


def test_unsupported_versions_are_refused_with_recovery_path(tmp_path):
    root = _make_root(tmp_path)
    db = root / "events.db"
    backup = _make_backup(root, tmp_path)

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        plan_migrations(root)
    assert "高于" in str(excinfo.value)
    with pytest.raises(UnsupportedSchemaVersion):
        assert_writable(root)
    with pytest.raises(UnsupportedSchemaVersion):
        run_migration(root, backup=backup)

    # 注册表断链（模拟缺步骤）：不允许跳到更高版本
    root2 = _make_root(tmp_path / "gap")
    monkeypatch_backup = _make_backup(root2, tmp_path / "backup-gap")
    import tianshu.migrate as mod

    original = mod.STEPS
    try:
        mod.STEPS = original[:1]
        with pytest.raises(UnsupportedSchemaVersion) as excinfo2:
            plan_migrations(root2)
        assert "达不到目标" in str(excinfo2.value)
    finally:
        mod.STEPS = original
    assert read_schema_version(root2 / "events.db") == 0
    assert monkeypatch_backup.exists()


def test_assert_writable_gate_states(tmp_path):
    root = _make_root(tmp_path)
    # 低版本：要求先迁移
    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        assert_writable(root)
    assert "低于" in str(excinfo.value)

    # 迁移中：拒绝写入（unknown + 阻断方向）
    backup = _make_backup(root, tmp_path)
    proc = _migration_subprocess(root, backup, "migration-before-step-2")
    assert proc.returncode == 77, proc.stderr
    assert read_state(root).status == "in_progress"
    with pytest.raises(MigrationIncomplete) as excinfo2:
        assert_writable(root)
    assert "续做" in str(excinfo2.value)


def test_corrupt_migration_state_is_refused(tmp_path):
    root = _make_root(tmp_path)
    backup = _make_backup(root, tmp_path)
    (root / migrate.STATE_NAME).write_bytes(b"{not json")
    with pytest.raises(MigrationStateCorrupt):
        run_migration(root, backup=backup)
    with pytest.raises(MigrationStateCorrupt):
        read_state(root)


# ---------------------------------------------------------------- 中断 / 续做


@pytest.mark.parametrize(
    "failpoint,version_after_crash,current_step",
    [
        ("migration-before-step-1", 0, 1),
        ("migration-after-step-1", 1, 1),
        ("migration-before-step-2", 1, 2),
        ("migration-after-step-2", 2, 2),
    ],
)
def test_interrupted_migration_resumes_to_same_final_state(
    tmp_path, failpoint, version_after_crash, current_step
):
    root = _make_root(tmp_path)
    db = root / "events.db"
    digest_before = _event_digest(db)
    backup = _make_backup(root, tmp_path)

    proc = _migration_subprocess(root, backup, failpoint)
    assert proc.returncode == 77, (
        f"{failpoint} 应当在注入点硬退：rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert f"[failpoint] {failpoint}" in proc.stderr, proc.stderr

    # 中断现场：版本 = 该步骤提交前/后的确定值，进度文件说"这一步正在做"
    assert read_schema_version(db) == version_after_crash
    state = read_state(root)
    assert state is not None and state.status == "in_progress"
    assert state.current_step == current_step
    assert state.backup["path"] == str(backup.resolve())
    # DDL 与版本号同事务：version=1 时 migration_log 一定在；version=0 时一定不在
    assert ("migration_log" in _tables(db)) is (version_after_crash >= 1)
    if version_after_crash == 2:
        assert "note" in _columns(db, "migration_log")

    # 重启续做：可完成、无混合版本半成品
    report = run_migration(root, backup=backup)
    assert report.resumed is True
    assert read_schema_version(db) == SCHEMA_VERSION
    assert "migration_log" in _tables(db)
    assert "note" in _columns(db, "migration_log")
    final_state = read_state(root)
    assert final_state is not None and final_state.status == "completed"
    assert final_state.applied_steps == [1, 2]
    assert assert_writable(root) == SCHEMA_VERSION
    # 历史事件一个字节都没变
    assert _event_digest(db) == digest_before
    # 每一步只应用一次（INSERT OR REPLACE 的审计表）
    conn = sqlite3.connect(db)
    try:
        rows = [r[0] for r in conn.execute(
            "SELECT step FROM migration_log ORDER BY step")]
    finally:
        conn.close()
    assert rows == [1, 2]


def test_resume_requires_the_recorded_backup(tmp_path):
    """续做仍要求备份：进度文件钉住的备份被删掉 → 拒绝续做（不猜）。"""
    root = _make_root(tmp_path)
    backup = _make_backup(root, tmp_path)
    proc = _migration_subprocess(root, backup, "migration-before-step-2")
    assert proc.returncode == 77

    # 备份目录整个搬走后：续做必须拒绝
    moved = tmp_path / "backup.moved"
    backup.rename(moved)
    with pytest.raises(MigrationRefused) as excinfo:
        run_migration(root, backup=backup)
    assert "备份" in str(excinfo.value)
    assert read_schema_version(root / "events.db") == 1  # 停在第 1 步之后

    # 备份搬回来即可续做
    moved.rename(backup)
    report = run_migration(root, backup=backup)
    assert report.resumed is True
    assert read_schema_version(root / "events.db") == SCHEMA_VERSION


def test_resume_detects_state_whose_plan_belongs_to_another_version(tmp_path):
    root = _make_root(tmp_path)
    backup = _make_backup(root, tmp_path)
    state = {
        "format": "tianshu-migration",
        "format_version": 1,
        "target_version": 99,
        "status": "in_progress",
        "applied_steps": [],
        "current_step": 99,
        "backup": {"path": str(backup.resolve())},
        "started_at_ms": 1,
        "updated_at_ms": 1,
    }
    (root / migrate.STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(MigrationStateCorrupt) as excinfo:
        run_migration(root, backup=backup)
    assert "另一套迁移计划" in str(excinfo.value)
    assert read_schema_version(root / "events.db") == 0
