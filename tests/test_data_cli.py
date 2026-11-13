"""数据维护 CLI 的真实子进程验收；不操作用户数据，不调用模型。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import memory_control
from tianshu.backup import create_backup
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.permissions import ActionRequest, PolicyEngine
from tianshu.resources import MemoryScope

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _cli(*args: object, failpoint: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TIANSHU_")}
    env["PYTHONPATH"] = SRC
    if failpoint is not None:
        env["TIANSHU_FAILPOINT"] = failpoint
    return subprocess.run(
        [sys.executable, "-m", "tianshu", "data", *(str(a) for a in args)],
        capture_output=True, timeout=30, env=env, **CHILD_TEXT,
    )


def _make_root(tmp_path: Path, *, projections: bool = False) -> tuple[Path, str]:
    root = tmp_path / "data"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    run_id = "run_fixture"
    try:
        if projections:
            stack = build_stack(log, data_root=root)
            workspace = tmp_path / "workspace"
            workspace.mkdir()
            target = workspace / "existing.txt"
            target.write_bytes(b"before\n")
            task = stack.tasks.create_task("审查恢复证据", [str(workspace)])
            run_id = stack.tasks.start_run(task.task_id).run_id
            stack.leases.acquire([workspace], run_id, ttl_ms=3_600_000)
            request = ActionRequest(
                request_id="req_pending", task_id=task.task_id, run_id=run_id,
                job_id="job_fixture", claim_epoch=1, owner_instance_id="fixture",
                goal_revision=1, lease_epoch=1, call_id="pending_approval", attempt_id=1,
                tool_id="modify_text", tool_version="1.0", schema_hash="a" * 64,
                action="overwrite", resources=(str(target),), args_hash="b" * 64,
                preconditions={"target": "c" * 64},
            )
            decision = PolicyEngine().classify("overwrite", [str(target)], task.workspaces)
            stack.approvals.request(request, decision)
            remembered = memory_control.remember(
                stack, MemoryScope.user(), kind="preference", content="先列结论，再列来源。"
            )
            assert remembered.ok
        else:
            log.append(run_id, "run.start", {
                "task": "维护夹具", "workspace": str(tmp_path), "out": "report.md",
                "limits": {}, "tools": [],
            }, actor_kind="core", legacy_write=True)
        # 真正落到 CLI 的根 blobs 目录，预演不能回源 root 读它。
        log.append(run_id, "tool.intent", {
            "call_id": "pending_call", "tool_id": "modify_text",
            "args": {"path": "target.txt", "content": "x" * 300_000},
            "args_hash": "d" * 64,
        }, actor_kind="core", legacy_write=True)
        return root, run_id
    finally:
        log.close()


def _tree_state(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _refresh_db_file_hash(dest: Path) -> None:
    """声明故障注入后的数据库字节，隔离语义校验与普通哈希校验。"""
    manifest = json.loads((dest / "backup.json").read_bytes())
    data = (dest / "events.db").read_bytes()
    entry = next(item for item in manifest["files"] if item["path"] == "events.db")
    entry.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    (dest / "backup.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_data_backup_then_verify_json_in_real_cli(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    made = _cli("backup", "--data-root", root, "--dest", dest, "--json")
    assert made.returncode == 0, made.stderr
    report = json.loads(made.stdout)
    assert report["operation"] == "backup"
    assert report["verification"]["ok"] is True
    assert report["manifest"]["source_path"] == str(root.resolve())
    assert any(item["path"].startswith("blobs/") for item in report["manifest"]["files"])
    before = _tree_state(dest)
    checked = _cli("verify", dest, "--json")
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["ok"] is True
    assert _tree_state(dest) == before


@pytest.mark.skipif(sys.platform != "darwin", reason="验证 macOS /tmp → /private/tmp 系统别名")
@pytest.mark.parametrize("root_input", ["raw", "canonical"])
def test_data_cli_preserves_historical_tmp_alias_after_source_disappears(root_input):
    with tempfile.TemporaryDirectory(prefix="tianshu-data-cli-alias-", dir="/tmp") as name:
        base = Path(name)
        root, run_id = _make_root(base, projections=True)
        canonical = root.resolve()
        assert canonical != root
        conn = sqlite3.connect(root / "events.db")
        try:
            row = conn.execute(
                "SELECT payload_ref FROM events WHERE type='tool.intent'"
                " AND payload_ref IS NOT NULL"
            ).fetchone()
            assert row is not None and row[0].startswith(str(root) + "/blobs/")
        finally:
            conn.close()
        target = root if root_input == "raw" else canonical
        dest = base / "backup"
        made = _cli("backup", "--data-root", target, "--dest", dest, "--json")
        assert made.returncode == 0, made.stderr
        report = json.loads(made.stdout)
        assert report["manifest"]["source_path"] == str(canonical)
        assert str(root) in report["manifest"]["source_aliases"]
        shutil.rmtree(root)
        before = _tree_state(dest)
        verified = _cli("verify", dest, "--json")
        assert verified.returncode == 0, verified.stderr
        planned = _cli("restore-plan", dest, "--data-root", target, "--json")
        assert planned.returncode == 3, planned.stderr
        plan = json.loads(planned.stdout)
        assert plan["ok"] is True
        assert plan["reconciliation"]["complete"] is True
        assert plan["reconciliation"]["pending_intents"][0]["run_id"] == run_id
        assert not root.exists()
        assert _tree_state(dest) == before


def test_data_verify_invalid_is_structured_failure_without_creating_path(tmp_path):
    missing = tmp_path / "missing-backup"
    result = _cli("verify", missing, "--json")
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["problems"]
    assert not missing.exists()


def test_data_restore_plan_is_readonly_and_never_activates_memory(tmp_path):
    root, run_id = _make_root(tmp_path, projections=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    assert any((dest / "memory_blobs").iterdir())
    shutil.rmtree(root)
    before = _tree_state(dest)
    result = _cli("restore-plan", dest, "--data-root", root, "--json")
    assert result.returncode == 3, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["apply_supported"] is False
    assert report["same_path"] is True
    assert report["target_clean"] is True
    assert report["memory_reactivation"] == "requires_quarantine_and_review"
    assert report["later_forget_delete_history"] == "completeness_unproven"
    reconciliation = report["reconciliation"]
    assert reconciliation["pending_intents"][0]["call_id"] == "pending_call"
    assert reconciliation["pending_intents"][0]["run_id"] == run_id
    assert len(reconciliation["pending_approvals"]) == 1
    assert reconciliation["non_terminal_runs"][0]["run_id"] == run_id
    assert reconciliation["live_leases"][0]["run_id"] == run_id
    assert not root.exists()
    assert _tree_state(dest) == before


def test_restore_plan_human_output_reports_conditions_and_pending_items(tmp_path):
    root, _ = _make_root(tmp_path, projections=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    result = _cli("restore-plan", dest, "--data-root", root)
    assert result.returncode == 3, result.stderr
    assert str(root.resolve()) in result.stdout
    assert "备份格式版本" in result.stdout
    assert "结构版本" in result.stdout
    assert "未决 intent：1" in result.stdout
    assert "未决审批：1" in result.stdout
    assert "未执行恢复" in result.stdout
    assert not root.exists()


@pytest.mark.parametrize("corruption", [
    "unknown-run-state", "unknown-approval-state", "bad-expiry",
    "future-payload-version", "missing-version-table",
])
def test_restore_plan_never_reports_unreadable_state_as_empty(tmp_path, corruption):
    root, _ = _make_root(tmp_path, projections=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    with sqlite3.connect(dest / "events.db") as conn:
        if corruption == "unknown-run-state":
            conn.execute("UPDATE runs SET state='future_state'")
        elif corruption == "unknown-approval-state":
            conn.execute("UPDATE approvals SET state='future_state'")
        elif corruption == "bad-expiry":
            conn.execute("UPDATE approvals SET expires_at='not-a-timestamp'")
        elif corruption == "future-payload-version":
            conn.execute("UPDATE event_versions SET version=999 WHERE type='tool.intent'")
        else:
            conn.execute("DROP TABLE event_versions")
    _refresh_db_file_hash(dest)
    before = _tree_state(dest)
    result = _cli("restore-plan", dest, "--data-root", root, "--json")
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["problems"]
    assert not root.exists()
    assert _tree_state(dest) == before


def test_reconciliation_and_cli_leave_wal_mode_backup_unchanged(tmp_path):
    from tianshu.data_cli import _reconciliation

    root, _ = _make_root(tmp_path, projections=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    with sqlite3.connect(dest / "events.db") as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    _refresh_db_file_hash(dest)
    before = _tree_state(dest)
    reconciliation = _reconciliation(dest, root)
    assert len(reconciliation["pending_intents"]) == 1
    assert _tree_state(dest) == before
    result = _cli("restore-plan", dest, "--data-root", root, "--json")
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    assert _tree_state(dest) == before
    assert not root.exists()


@pytest.mark.parametrize("table", ["runs", "approvals", "leases"])
def test_restore_plan_rejects_missing_required_projection(tmp_path, table):
    root, _ = _make_root(tmp_path, projections=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    with sqlite3.connect(dest / "events.db") as conn:
        conn.execute(f"DROP TABLE {table}")
    _refresh_db_file_hash(dest)
    before = _tree_state(dest)
    result = _cli("restore-plan", dest, "--data-root", root, "--json")
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert any(table in problem for problem in report["problems"])
    assert report["reconciliation"]["complete"] is False
    assert _tree_state(dest) == before
    assert not root.exists()


def test_restore_plan_refuses_to_guess_missing_legacy_projections(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    before = _tree_state(dest)
    result = _cli("restore-plan", dest, "--data-root", root, "--json")
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["verification"]["ok"] is True
    assert report["ok"] is False
    assert report["problems"]
    assert report["reconciliation"]["complete"] is False
    assert _tree_state(dest) == before
    assert not root.exists()


def test_restore_plan_rejects_live_symlink_replacing_original_root(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    root.rename(tmp_path / "original-moved")
    replacement = tmp_path / "unrelated-empty-directory"
    replacement.mkdir()
    try:
        root.symlink_to(replacement, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前平台不能创建符号链接：{exc}")
    before = _tree_state(tmp_path)
    result = _cli("restore-plan", dest, "--data-root", root, "--json")
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["same_path"] is False
    assert report["source_path"] != report["data_root"]
    assert report["problems"]
    assert not any(replacement.iterdir())
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("case", ["different-path", "nonempty", "bad-backup"])
def test_restore_plan_rejects_preconditions_without_writes(tmp_path, case):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    target = root
    if case == "different-path":
        target = tmp_path / "other"
    elif case == "nonempty":
        root.mkdir()
        (root / "keep.txt").write_bytes(b"do not touch")
    else:
        (dest / "events.db").write_bytes(b"broken")
    before = _tree_state(tmp_path)
    result = _cli("restore-plan", dest, "--data-root", target, "--json")
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["apply_supported"] is False
    assert report["problems"]
    assert _tree_state(tmp_path) == before
    if case != "nonempty":
        assert not target.exists()


@pytest.mark.parametrize("failpoint", [
    "backup-after-db", "backup-before-manifest", "backup-after-manifest",
])
def test_backup_cli_crash_never_publishes_partial(tmp_path, failpoint):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    crashed = _cli("backup", "--data-root", root, "--dest", dest, failpoint=failpoint)
    assert crashed.returncode == 77, crashed.stderr
    assert not dest.exists()
    partials = list(tmp_path.glob(".backup.partial-*"))
    assert len(partials) == 1
    checked = _cli("verify", partials[0], "--json")
    assert checked.returncode == 1, checked.stderr
    assert json.loads(checked.stdout)["ok"] is False
    # 硬退必须释放维护锁；真实新进程可重新生成另一份有效备份。
    retried = _cli("backup", "--data-root", root, "--dest", dest, "--json")
    assert retried.returncode == 0, retried.stderr


def test_backup_cli_requires_explicit_root_and_refuses_missing_root(tmp_path):
    dest = tmp_path / "backup"
    no_arg = _cli("backup", "--dest", dest)
    assert no_arg.returncode == 2
    missing = tmp_path / "not-created"
    no_root = _cli("backup", "--data-root", missing, "--dest", dest)
    assert no_root.returncode == 2
    assert "数据根" in no_root.stderr
    assert not missing.exists()
    assert not dest.exists()


def test_backup_cli_refuses_to_overwrite_existing_backup(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    before = _tree_state(dest)
    result = _cli("backup", "--data-root", root, "--dest", dest)
    assert result.returncode == 2
    assert "已存在" in result.stderr
    assert _tree_state(dest) == before


def test_backup_cli_refuses_live_eventlog_without_stopping_it(tmp_path):
    root, run_id = _make_root(tmp_path)
    log = EventLog(root / "events.db")
    try:
        dest = tmp_path / "backup"
        result = _cli("backup", "--data-root", root, "--dest", dest, "--json")
        assert result.returncode == 2
        assert not dest.exists()
        # 维护失败不清理或杀死原使用者，原连接仍能真实追加事件。
        log.append(run_id, "tool.result", {
            "call_id": "after_refusal", "tool_id": "read_text", "status": "succeeded",
            "effect_state": "none_applied", "output": {"content": "still alive"},
        }, actor_kind="core")
    finally:
        log.close()


def test_restore_apply_and_migrate_are_not_exposed(tmp_path):
    for name in ("restore", "migrate"):
        result = _cli(name, "--data-root", tmp_path, "--approve")
        assert result.returncode == 2
        assert "invalid choice" in result.stderr
