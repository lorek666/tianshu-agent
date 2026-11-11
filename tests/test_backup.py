"""W4 验收：一致点备份 / 校验 / 干净根恢复 / 保留期引用保护（PRD §8.5）。

要证明的事：

1. 备份取的是**一致点**：事件库走 SQLite 在线备份 API（不是复制正在写的文件），
   blob/清单/撤销日志逐个复制并记录 sha256；`backup.json` 能自解释（版本、引用、
   恢复须知、保留期）。
2. 校验有牙：缺文件、内容被改、事件 seq 缺口、引用丢失、格式/结构版本不兼容
   一律 `ok=False` 并给补救路径；不把"读不出来"当成"空历史"。
3. 恢复只接受**干净根**，且只能回原数据根（绝对引用决定）；失败不破坏原目录。
4. 备份 ≠ 快照 ≠ 撤销：恢复出来的是证据，外部 payload blob 必须能从恢复副本读到。
5. 保留期：引用保护可测试；未引用 blob 才进候选；读不出引用关系时拒绝给结论。
6. 负控（回退后本文件必须红，见 docs/backup-restore.md）：去掉中断标记检查 →
   中断临时目录会被当成有效备份；去掉引用保护 → 孤儿/被引用 blob 混为一谈。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import CHILD_TEXT, write_exact

from tianshu import backup
from tianshu.backup import (
    BackupRefused,
    ReferenceUnknown,
    RestoreRefused,
    cleanup_plan,
    collect_references,
    create_backup,
    inspect_writers,
    restore_backup,
    usage_report,
    verify_backup,
)
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.snapshot import SnapshotStore
from tianshu.undo_journal import ACTION_RESTORE, JournalItem, JournalStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
TARGET = "a.txt"
BEFORE = "写前内容\n"


# ---------------------------------------------------------------- 夹具


def _make_data_root(tmp_path: Path) -> Path:
    """一个有事件/外部 payload blob/快照/撤销日志的真实数据根。"""
    root = tmp_path / "data"
    (root / "artifacts").mkdir(parents=True)
    db = root / "events.db"
    blobs = root / "artifacts" / "blobs"
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / TARGET, BEFORE)

    log = EventLog(db, blob_dir=blobs)
    log.append("r1", "run.start", {
        "task": "备份夹具", "workspace": str(ws.resolve()), "out": "x.md",
        "limits": {}, "tools": []}, actor_kind="core", legacy_write=True)
    log.append("r1", "tool.intent", {
        "call_id": "c1", "tool_id": "modify_text",
        "args": {"path": TARGET, "content": "写后内容\n"},
        "args_hash": "0" * 64}, actor_kind="core", legacy_write=True)
    log.append("r1", "tool.result", {
        "call_id": "c1", "tool_id": "modify_text", "status": "succeeded",
        "effect_state": "applied",
        "output": {"path": TARGET, "sha256": "a" * 64, "after_state": "a" * 64,
                   "before_state": "b" * 64}}, actor_kind="core")
    # 超过 MAX_INLINE_BYTES 的 payload 会落到 blob（备份必须带上并校验引用）
    log.append("r1", "tool.result", {
        "call_id": "c2", "tool_id": "read_text", "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": "x" * 300_000}}, actor_kind="core")

    store = SnapshotStore(root / "artifacts")
    store.init_run("r1")
    store.record_preimage(
        "r1", "c1", TARGET, BEFORE.encode(), "a" * 64, root=ws,
    )
    journal = JournalStore(root / "artifacts")
    before_sha = hashlib.sha256(BEFORE.encode()).hexdigest()
    journal.begin(
        "r1",
        control_run="run_control",
        items=(
            JournalItem(
                root=str(ws), path=TARGET, call_id="c1",
                before_state=before_sha, expected_after="a" * 64,
                action=ACTION_RESTORE,
            ),
        ),
    )
    log.close()
    return root


def _digest_db(db: Path) -> str:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT seq, run_id, type, payload_hash FROM events ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _run_child(root: Path, dest: Path, failpoint: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from pathlib import Path\n"
        "from tianshu.backup import create_backup\n"
        f"create_backup(Path({str(root)!r}), Path({str(dest)!r}))\n"
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


def _assert_backup_files_present(manifest, expected: set[str]) -> None:
    paths = {f.path for f in manifest.files}
    missing = expected - paths
    assert not missing, f"备份清单缺少文件：{sorted(missing)}"


# ---------------------------------------------------------------- 一致点备份


def test_create_backup_is_a_consistent_self_describing_point(tmp_path):
    root = _make_data_root(tmp_path)
    digest_before = _digest_db(root / "events.db")
    dest = tmp_path / "backup"

    manifest = create_backup(root, dest)

    # 版本清单：格式/应用/库结构/时间/来源
    assert manifest.format == backup.FORMAT_NAME
    assert manifest.format_version == backup.FORMAT_VERSION
    assert manifest.app_version
    assert manifest.db_user_version == 0
    assert manifest.source_path == str(root.resolve())
    assert manifest.created_at_ms > 0
    # 事件统计与 run 清单
    assert manifest.event_count == 4
    assert manifest.event_seq_head == 4
    assert manifest.run_ids == ["r1"]
    # 三类材料都进备份，且多了“外部 payload blob”
    _assert_backup_files_present(
        manifest,
        {
            "events.db",
            "artifacts/snapshots/r1.json",
            "artifacts/undo/r1.json",
        },
    )
    categories = {f.category for f in manifest.files}
    assert {"event_log", "snapshot_manifest", "undo_journal", "blob"} <= categories
    assert len(manifest.references["event_payload_refs"]) == 1
    assert len(manifest.references["snapshot_blobs"]) == 1
    assert len(manifest.references["undo_journal_blobs"]) == 1
    assert manifest.references["missing"] == []
    assert manifest.references["seq_contiguous"] is True
    # 恢复须知 + 保留期 + 排障上下文（写者状态）
    assert any("只能恢复到" in note for note in manifest.restore_notes)
    assert {"event_log", "snapshot_blobs", "diagnostic_trace"} <= set(
        manifest.retention
    )
    assert "ipc_probe" in manifest.writer
    # 源库一个字节都没被改（在线备份不是复制后清理）
    assert _digest_db(root / "events.db") == digest_before

    # 校验通过，且清单文件确实可解析
    verification = verify_backup(dest)
    assert verification.ok, verification.problems
    assert verification.files_checked == len(manifest.files)
    assert verification.bytes_checked == manifest.total_bytes
    on_disk = json.loads((dest / backup.MANIFEST_NAME).read_bytes())
    assert on_disk["source_path"] == str(root.resolve())


def test_backup_excludes_secrets_run_dir_and_logs(tmp_path):
    root = _make_data_root(tmp_path)
    (root / "run").mkdir(parents=True)
    (root / "run" / "ipc.key").write_bytes(b"secret" * 8)
    (root / "run" / "daemon.lock").write_text('{"pid": 1}', encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "daemon.log").write_text("trace" * 10, encoding="utf-8")
    (root / "metrics.jsonl").write_text("{}\n", encoding="utf-8")

    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)

    rels = {f.path for f in manifest.files}
    assert not any(rel.startswith("run/") for rel in rels)
    assert not any(rel.startswith("logs/") for rel in rels)
    assert "metrics.jsonl" not in rels
    for path in (dest / "run", dest / "logs"):
        assert not path.exists()
    assert not (dest / "metrics.jsonl").exists()
    # ipc.key 的内容一个字节都不许出现在备份里
    for path in dest.rglob("*"):
        if path.is_file():
            assert b"secret" * 8 not in path.read_bytes()


def test_restore_same_path_after_losing_original_data(tmp_path):
    """忘记/丢掉原数据后，从备份恢复回原路径：外部 payload 也必须能读。"""
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)
    moved = tmp_path / "data.moved"
    root.rename(moved)  # 模拟原数据根被移走/丢失

    report = restore_backup(dest, root)

    assert report.ok
    assert report.files_restored == len(manifest.files)
    assert report.bytes_restored == manifest.total_bytes
    assert report.data_root == root.resolve()
    assert report.reconciliation["pending_intents"] == []
    # 从恢复出来的副本读外部 payload：旧根已 rename，读到的一定是备份副本
    log = EventLog(root / "events.db", blob_dir=root / "artifacts" / "blobs")
    try:
        events = log.read_run("r1")
    finally:
        log.close()
    big = [e for e in events if e.payload.get("call_id") == "c2"][0]
    assert len(big.payload["output"]["content"]) == 300_000
    # 快照 blob 也在：恢复后仍可撤销（备份 ≠ 撤销，但备份让撤销证据活着）
    store = SnapshotStore(root / "artifacts")
    change = store.find("r1", "c1")
    assert change is not None and change.blob_ref
    assert Path(change.blob_ref).read_bytes() == BEFORE.encode()
    assert not moved.exists() or moved.is_dir()  # 旧数据未被触碰


def test_restore_refuses_non_clean_target_and_wrong_path(tmp_path):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)

    # 非空目标拒绝（且不覆盖）
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    write_exact(dirty / "keep.txt", "用户数据\n")
    with pytest.raises(BackupRefused):
        restore_backup(dest, dirty)
    assert (dirty / "keep.txt").read_bytes() == "用户数据\n".encode()

    # 换路径恢复拒绝：绝对引用决定只能回原路径
    with pytest.raises(RestoreRefused) as excinfo:
        restore_backup(dest, tmp_path / "elsewhere")
    assert "只能恢复到原数据根" in str(excinfo.value) or "原路径" in str(excinfo.value)


def test_restore_failure_keeps_target_untouched(tmp_path, monkeypatch):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    root.rename(tmp_path / "data.moved")

    calls = {"n": 0}
    real = backup._copy_file_verified

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device (模拟磁盘满)")
        return real(src, dst)

    monkeypatch.setattr(backup, "_copy_file_verified", flaky)
    with pytest.raises(OSError):
        restore_backup(dest, root)
    # 原目标没被创建/替换；临时目录已清理
    assert not root.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".data.restore-")]
    assert leftovers == []


# ---------------------------------------------------------------- 中断与损坏


@pytest.mark.parametrize(
    "failpoint",
    ["backup-after-db", "backup-before-manifest", "backup-after-manifest"],
)
def test_interrupted_backup_is_never_a_valid_backup(tmp_path, failpoint):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"

    proc = _run_child(root, dest, failpoint)
    assert proc.returncode == 77, (
        f"{failpoint} 应当在注入点硬退：rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert f"[failpoint] {failpoint}" in proc.stderr, proc.stderr
    # 最终目标不存在；中断现场是一个带 .partial 标记的临时目录
    assert not dest.exists()
    partials = list(tmp_path.glob(".backup.partial-*"))
    assert len(partials) == 1, partials

    # 中断目录绝不能被当成有效备份（先验"拒绝"，再看拒绝的依据）
    verification = verify_backup(partials[0])
    assert verification.ok is False
    assert any(
        "未完成" in p or "partial" in p for p in verification.problems
    ), verification.problems
    assert (partials[0] / backup.PARTIAL_MARKER).exists()
    assert verify_backup(dest).ok is False

    # 重新备份到同一目标可以成功（旧的中断目录不挡路）
    manifest = create_backup(root, tmp_path / "backup2")
    assert verify_backup(tmp_path / "backup2").ok
    assert manifest.event_count == 4


def test_create_backup_failure_cleans_temporary_directory(tmp_path, monkeypatch):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"

    def disk_full(src, dst):
        raise OSError(28, "No space left on device (模拟磁盘满)")

    monkeypatch.setattr(backup, "_copy_file_verified", disk_full)
    with pytest.raises(OSError):
        create_backup(root, dest)
    assert not dest.exists()
    assert list(tmp_path.glob(".backup.partial-*")) == []
    # 源数据根没有被半写坏
    assert (root / "events.db").is_file()
    assert _digest_db(root / "events.db")


def test_verify_detects_missing_and_tampered_files(tmp_path):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)
    assert verify_backup(dest).ok

    # 内容被改
    blob = next(f for f in manifest.files if f.category == "blob")
    target = dest / blob.path
    raw = bytearray(target.read_bytes())
    raw[0] ^= 0xFF
    target.write_bytes(bytes(raw))
    bad = verify_backup(dest)
    assert bad.ok is False
    assert any("内容被改过" in p for p in bad.problems), bad.problems

    # 文件缺失
    target.write_bytes(target.read_bytes())  # 先恢复字节以便只测缺失
    manifest2 = create_backup(root, tmp_path / "backup2")
    victim = next(f for f in manifest2.files if f.category == "snapshot_manifest")
    (tmp_path / "backup2" / victim.path).unlink()
    bad2 = verify_backup(tmp_path / "backup2")
    assert bad2.ok is False
    assert any("缺失" in p for p in bad2.problems), bad2.problems

    # 清单本身坏掉
    (dest / backup.MANIFEST_NAME).write_bytes(b"{not json")
    bad3 = verify_backup(dest)
    assert bad3.ok is False
    assert any("backup.json" in p for p in bad3.problems), bad3.problems


def test_verify_rejects_unknown_newer_format_and_missing_source_path(tmp_path):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    raw = json.loads((dest / backup.MANIFEST_NAME).read_bytes())

    raw_newer = dict(raw, format_version=backup.FORMAT_VERSION + 1)
    (dest / backup.MANIFEST_NAME).write_text(json.dumps(raw_newer), encoding="utf-8")
    bad = verify_backup(dest)
    assert bad.ok is False
    assert any("比本程序支持" in p for p in bad.problems), bad.problems

    raw_missing = dict(raw_newer)
    raw_missing.pop("source_path")
    (dest / backup.MANIFEST_NAME).write_text(json.dumps(raw_missing), encoding="utf-8")
    bad2 = verify_backup(dest)
    assert bad2.ok is False
    assert any("source_path" in p or "backup.json" in p for p in bad2.problems)


def test_verify_detects_seq_gap(tmp_path):
    """事件 seq 有缺口的历史不是完整历史：verify 必须拒绝。"""
    root = _make_data_root(tmp_path)
    db = root / "events.db"
    conn = sqlite3.connect(db)
    try:
        # 直接插一条跳过 seq 的记录（trigger 只禁 UPDATE/DELETE）
        conn.execute(
            "INSERT INTO events(seq, run_id, type, ts, actor_kind, actor_id,"
            " payload, payload_hash, idem_key)"
            " VALUES (99, 'r1', 'run.cursor', 1, 'core', '', '{}', ?, 'gap')",
            (hashlib.sha256(b"{}").hexdigest(),),
        )
        conn.commit()
    finally:
        conn.close()
    dest = tmp_path / "backup"
    create_backup(root, dest)
    bad = verify_backup(dest)
    assert bad.ok is False
    assert any("seq 不连续" in p for p in bad.problems), bad.problems


def test_create_backup_refuses_root_without_event_log(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackupRefused) as excinfo:
        create_backup(empty, tmp_path / "backup")
    assert "没有事件库" in str(excinfo.value)


def test_verify_reports_missing_referenced_blob(tmp_path):
    root = _make_data_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    # 删掉一个被快照清单引用的 blob：清单还在，引用断了
    blob = next(
        f for f in verify_backup(dest).manifest.files
        if f.category == "blob"
    )
    (dest / blob.path).unlink()
    bad = verify_backup(dest)
    assert bad.ok is False
    assert any("缺失" in p for p in bad.problems), bad.problems


# ---------------------------------------------------------------- 恢复后核对


def test_restore_reconciliation_reports_pending_work(tmp_path):
    root = _make_data_root(tmp_path)
    log = EventLog(root / "events.db", blob_dir=root / "artifacts" / "blobs")
    build_stack(log, data_root=root)  # 建出投影表（审批/租约/Run）
    now = int(time.time() * 1000)
    # 未决 intent：有 intent 无 result
    log.append("r_live", "tool.intent", {
        "call_id": "c9", "tool_id": "modify_text",
        "args": {"path": "a.txt"}, "args_hash": "0" * 64}, actor_kind="core", legacy_write=True)
    conn = log.conn
    conn.execute(
        "INSERT INTO runs(run_id, task_id, goal_revision, lease_epoch, state,"
        " created_at) VALUES ('r_live','t1',1,1,'running',?)", (now,))
    conn.execute(
        "INSERT INTO approvals(approval_id, request_id, request_digest,"
        " action_digest, task_id, run_id, goal_revision, lease_epoch, action,"
        " resources, state, reason, created_at, expires_at)"
        " VALUES ('ap1','req1','d1','a1','t1','r_live',1,1,'overwrite',"
        " '[\"/x\"]','pending','',?,?)", (now, now + 600_000))
    conn.execute(
        "INSERT INTO leases(resource_key, run_id, owner_pid, owner_instance_id,"
        " canonical_path, path_key, acquired_at, expires_at, wall_acquired_at)"
        " VALUES ('k1','r_live',?, 'inst', '/x', 'k1', ?, ?, ?)",
        (os.getpid(), now, now + 600_000, now))
    log.close()
    dest = tmp_path / "backup"
    create_backup(root, dest)
    root.rename(tmp_path / "data.moved")

    report = restore_backup(dest, root)
    rec = report.reconciliation
    assert [item["call_id"] for item in rec["pending_intents"]] == ["c9"]
    assert [item["approval_id"] for item in rec["pending_approvals"]] == ["ap1"]
    assert [item["run_id"] for item in rec["non_terminal_runs"]] == ["r_live"]
    assert [item["resource_key"] for item in rec["live_leases"]] == ["k1"]
    assert any("未决 intent" in w for w in report.warnings)
    assert any("租约" in w for w in report.warnings)
    assert "恢复后核对" in report.to_text()
    # hashlib/stack 只是为了建表；确认恢复后事件仍在
    assert report.manifest.event_count >= 4


# ---------------------------------------------------------------- 保留期 / 引用


def test_reference_protection_only_orphans_are_candidates(tmp_path):
    root = _make_data_root(tmp_path)
    blobs = root / "artifacts" / "blobs"
    orphan = blobs / ("f" * 64)
    orphan.write_bytes(b"no one references me")

    index = collect_references(root)
    assert index.orphan_blobs == [f"artifacts/blobs/{'f' * 64}"]
    assert len(index.protected_blobs) >= 2  # payload blob + 快照/撤销共用的 blob
    assert index.undo_journals == {"artifacts/undo/r1.json": "in_progress"}

    plan = cleanup_plan(root)
    assert plan["dry_run"] is True
    candidates = {item["path"] for item in plan["candidates"]}
    assert candidates == {f"artifacts/blobs/{'f' * 64}"}
    assert plan["undo_journals_in_progress"] == ["artifacts/undo/r1.json"]
    # 只是候选清单：一个文件都没删
    assert orphan.exists()
    assert index.protected_blobs


def test_reference_protection_refuses_when_evidence_unreadable(tmp_path):
    root = _make_data_root(tmp_path)
    manifest = root / "artifacts" / "snapshots" / "r1.json"
    manifest.write_bytes(b"{truncated")
    with pytest.raises(ReferenceUnknown):
        collect_references(root)
    with pytest.raises(ReferenceUnknown):
        cleanup_plan(root)


def test_usage_report_matches_files_and_flags_over_budget(tmp_path, monkeypatch):
    root = _make_data_root(tmp_path)
    report = usage_report(root)
    assert report.total_bytes > 0
    assert report.categories["event_log"]["bytes"] == (root / "events.db").stat().st_size
    assert report.categories["event_log"]["files"] == 1
    assert report.categories["snapshot_blobs"]["files"] >= 2
    assert report.over_budget == []
    # 额度只是声明：调小后必须被点名，但不自动清理
    monkeypatch.setitem(backup.RETENTION_POLICY["snapshot_blobs"], "budget_bytes", 10)
    report2 = usage_report(root)
    assert "snapshot_blobs" in report2.over_budget
    assert (root / "artifacts").is_dir()


def test_inspect_writers_is_read_only_and_records_unknown(tmp_path):
    root = _make_data_root(tmp_path)
    info = inspect_writers(root)
    assert info["ipc_probe"] is False
    assert info["daemon_lock"] == {"present": False}

    (root / "run").mkdir(parents=True)
    (root / "run" / "daemon.lock").write_text("{broken", encoding="utf-8")
    info2 = inspect_writers(root)
    assert info2["daemon_lock"]["present"] is True
    assert "unreadable" in info2["daemon_lock"]


def test_fsync_file_propagates_failure_on_posix(tmp_path, monkeypatch):
    """POSIX 上 fsync 失败必须抛：没有落盘证据不能静默。"""
    if os.name == "nt":
        pytest.skip("POSIX 语义")
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")

    def boom(fd):
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(backup.os, "fsync", boom)
    with pytest.raises(OSError):
        backup._fsync_file(target)


def test_fsync_file_degrades_on_windows_readonly_handles(tmp_path, monkeypatch):
    """Windows 分支：改用可写句柄；fsync 仍失败时只降级该边界，不让备份崩。

    用 monkeypatch 把 `os.name` 伪装成 `nt` 走 Windows 分支（本机 POSIX 无法
    真的复现 `EBADF`）。断言 `os.fsync` 真被调用过——不是一打开就跳过。
    """
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    calls = []

    def boom(fd):
        calls.append(fd)
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(backup.os, "fsync", boom)
    monkeypatch.setattr(backup.os, "name", "nt")
    backup._fsync_file(target)  # 不应抛
    assert calls, "Windows 分支也必须真的尝试 fsync"
