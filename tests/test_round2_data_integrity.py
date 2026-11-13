"""第二轮数据审计反例：真实凭据、原生材料读取与只读恢复核对。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import backup
from tianshu.dispatcher import KIND_WAITING, build_request
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.permissions import HumanIdentity
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING
from tianshu.undo_journal import JournalItem, JournalStore, UndoJournal


def _root(tmp_path: Path, *, final_state: str = "running") -> tuple[Path, dict]:
    root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "note.md"
    target.write_bytes(b"original preimage\n")
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        stack = build_stack(log, data_root=root)
        task = stack.tasks.create_task("preserve write evidence", [str(workspace)])
        run = stack.tasks.start_run(task.task_id)
        stack.leases.acquire([workspace], run.run_id)
        claim = stack.claims.claim(stack.claims.enqueue(run.run_id).job_id)
        stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        stack.snapshots.init_run(run.run_id)
        args = {"path": "note.md", "content": "changed by dispatcher\n"}
        request = build_request(
            task=task, run=run, claim=claim, call_id="overwrite", tool_id="modify_text",
            target=target, args=args,
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.kind == KIND_WAITING
        stack.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli(),
        )
        written = stack.dispatcher.dispatch(request.request_id, claim)
        assert written.status == "succeeded" and written.effect_state == "applied"
        receipt = next(event.payload for event in log.read_run(run.run_id)
                       if event.type == "receipt")
        change = stack.snapshots.find(run.run_id, "overwrite")
        assert change is not None and receipt["blob_ref"] == change.blob_ref
        JournalStore(root / "artifacts").begin(
            run.run_id, control_run="control", items=(JournalItem(
                root=str(workspace), path="note.md", call_id="overwrite",
                before_state=change.before_state, expected_after=change.expected_after,
                action="restore",
            ),),
        )
        if final_state == "succeeded":
            stack.tasks.finish_run(run.run_id, outcome="succeeded", effect_state="applied")
        elif final_state == "fenced":
            stack.tasks.revise_task(task.task_id, goal="revised goal")
        return root, receipt


def _saved(tmp_path: Path, *, final_state: str = "running") -> tuple[Path, Path, dict]:
    root, receipt = _root(tmp_path, final_state=final_state)
    saved = tmp_path / "backup"
    backup.create_backup(root, saved)
    assert backup.verify_backup(saved).ok
    shutil.rmtree(root)
    return root, saved, receipt


def _tree(root: Path) -> dict:
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                         path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def _live_state(root: Path) -> tuple:
    # 活 WAL 的只读连接允许 SQLite 管理锁/SHM；核对逻辑历史和持久材料，
    # 不把连接临时旁文件变化误当成数据被改写。静态备份仍用 _tree 全字节核对。
    with closing(sqlite3.connect(f"{(root / 'events.db').as_uri()}?mode=ro", uri=True)) as conn:
        logical = tuple(conn.iterdump())
    materials = {name: value for name, value in _tree(root).items()
                 if name not in {"events.db", "events.db-wal", "events.db-shm"}}
    return logical, materials


def _write_json(path: Path, raw: object) -> None:
    path.write_bytes(json.dumps(raw, ensure_ascii=False).encode("utf-8"))


def _refresh(saved: Path, rel: str) -> None:
    """隔离语义反例：清单声明故障注入后的真实文件字节，不靠旧哈希报错。"""
    manifest = json.loads((saved / "backup.json").read_bytes())
    body = (saved / rel).read_bytes()
    entry = next(item for item in manifest["files"] if item["path"] == rel)
    entry.update(sha256=hashlib.sha256(body).hexdigest(), size=len(body))
    _write_json(saved / "backup.json", manifest)


def _cli(*args: object) -> subprocess.CompletedProcess[str]:
    # 保留 pytest 注入的临时 authority 根；所有调用只接触本测试的临时数据。
    env = dict(os.environ)
    # 子进程跟随实际被测包；把新版测试用于历史源码负控时也不能偷偷跑新CLI。
    env["PYTHONPATH"] = str(Path(backup.__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-m", "tianshu", "data", *(str(arg) for arg in args), "--json"],
        env=env, capture_output=True, timeout=30, **CHILD_TEXT,
    )


def _plan_refused(root: Path, saved: Path) -> dict:
    before = _tree(saved)
    result = _cli("restore-plan", saved, "--data-root", root)
    assert _tree(saved) == before
    assert not root.exists()
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False and report["reconciliation"]["complete"] is False
    return report


def test_receipt_blob_is_independently_listed_as_backup_reference(tmp_path):
    root, receipt = _root(tmp_path)
    manifest = backup.create_backup(root, tmp_path / "backup")
    refs = manifest.references.get("receipt_blobs", [])
    assert any(ref["path"] == f"artifacts/blobs/{receipt['before_state']}"
               and ref["sha256"] == receipt["before_state"] for ref in refs)


@pytest.mark.parametrize("blob_missing", [False, True])
def test_missing_snapshot_cannot_turn_receipt_preimage_into_cleanup_candidate(
    tmp_path, blob_missing,
):
    root, receipt = _root(tmp_path)
    (root / "artifacts/snapshots" / f"{receipt['run_id']}.json").unlink()
    (root / "artifacts/undo" / f"{receipt['run_id']}.json").unlink()
    if blob_missing:
        Path(receipt["blob_ref"]).unlink()
    before = _live_state(root)
    with pytest.raises(backup.ReferenceUnknown):
        backup.cleanup_plan(root)
    assert _live_state(root) == before


@pytest.mark.parametrize("blob_missing", [False, True])
def test_backup_does_not_publish_missing_receipt_snapshot(tmp_path, blob_missing):
    root, receipt = _root(tmp_path)
    (root / "artifacts/snapshots" / f"{receipt['run_id']}.json").unlink()
    (root / "artifacts/undo" / f"{receipt['run_id']}.json").unlink()
    if blob_missing:
        Path(receipt["blob_ref"]).unlink()
    before = _live_state(root)
    saved = tmp_path / "backup"
    with pytest.raises(backup.BackupInvalid):
        backup.create_backup(root, saved)
    assert not saved.exists() and not list(tmp_path.glob(".backup.partial-*"))
    assert _live_state(root) == before


def test_saved_receipt_cannot_hide_missing_preimage_by_removing_manifest_claims(tmp_path):
    root, saved, receipt = _saved(tmp_path)
    removed = {
        f"artifacts/snapshots/{receipt['run_id']}.json",
        f"artifacts/undo/{receipt['run_id']}.json",
        f"artifacts/blobs/{receipt['before_state']}",
    }
    for rel in removed:
        (saved / rel).unlink()
    raw = json.loads((saved / "backup.json").read_bytes())
    raw["files"] = [entry for entry in raw["files"] if entry["path"] not in removed]
    raw["references"] = {}
    _write_json(saved / "backup.json", raw)
    assert not backup.verify_backup(saved).ok
    _plan_refused(root, saved)


@pytest.mark.parametrize("fault", [
    "snapshot-version", "snapshot-change", "undo-version", "undo-items",
    "undo-completed", "undo-run-id",
])
def test_verify_rejects_material_rejected_by_native_reader(tmp_path, fault):
    root, saved, receipt = _saved(tmp_path)
    run_id = receipt["run_id"]
    is_snapshot = fault.startswith("snapshot")
    rel = f"artifacts/{'snapshots' if is_snapshot else 'undo'}/{run_id}.json"
    path = saved / rel
    raw = json.loads(path.read_bytes())
    if fault.endswith("version"):
        raw["version"] = 999
    elif fault == "snapshot-change":
        raw["changes"] = [{}]
    elif fault == "undo-items":
        del raw["items"]
    elif fault == "undo-completed":
        raw["status"] = "completed"
    else:
        raw["run_id"] = "another-run"
    _write_json(path, raw)
    _refresh(saved, rel)
    # 正式读取器确实拒绝相同字节；不使用副本解释器来证明它坏了。
    with pytest.raises(TianshuError):
        if is_snapshot:
            SnapshotStore(saved / "artifacts").load(run_id)
        else:
            UndoJournal.from_dict(raw, run_id=run_id)
    before = _tree(saved)
    verification = backup.verify_backup(saved)
    assert _tree(saved) == before
    assert not verification.ok, verification.to_dict()
    _plan_refused(root, saved)


@pytest.mark.parametrize("event_type", ["run.state", "receipt", "task.created", "tool.intent"])
def test_restore_plan_checks_versions_beyond_tool_summaries(tmp_path, event_type):
    root, saved, _ = _saved(tmp_path)
    with closing(sqlite3.connect(saved / "events.db")) as conn:
        changed = conn.execute("UPDATE event_versions SET version=999 WHERE type=?",
                               (event_type,)).rowcount
        assert changed > 0
        conn.commit()
    _refresh(saved, "events.db")
    _plan_refused(root, saved)


@pytest.mark.parametrize("fault", ["missing", "wrong-state", "wrong-task"])
def test_restore_plan_requires_runs_projection_to_match_events(tmp_path, fault):
    root, saved, receipt = _saved(tmp_path)
    with closing(sqlite3.connect(saved / "events.db")) as conn:
        if fault == "missing":
            conn.execute("DELETE FROM runs WHERE run_id=?", (receipt["run_id"],))
        elif fault == "wrong-state":
            conn.execute("UPDATE runs SET state='succeeded' WHERE run_id=?",
                         (receipt["run_id"],))
        else:
            conn.execute("UPDATE runs SET task_id='another-task' WHERE run_id=?",
                         (receipt["run_id"],))
        conn.commit()
    _refresh(saved, "events.db")
    _plan_refused(root, saved)


@pytest.mark.parametrize("final_state", ["running", "succeeded", "fenced"])
def test_restore_plan_preserves_readable_run_states(tmp_path, final_state):
    root, saved, receipt = _saved(tmp_path, final_state=final_state)
    before = _tree(saved)
    result = _cli("restore-plan", saved, "--data-root", root)
    assert result.returncode == 3, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["reconciliation"]["complete"] is True
    active = report["reconciliation"]["non_terminal_runs"]
    if final_state == "running":
        assert len(active) == 1
        assert active[0]["run_id"] == receipt["run_id"]
        assert active[0]["state"] == "running"
        assert isinstance(active[0]["task_id"], str) and active[0]["task_id"]
    else:
        assert active == []
    assert not root.exists() and _tree(saved) == before


@pytest.mark.parametrize("value", [1.9, True, "1", float("inf")])
def test_verify_rejects_non_integer_format_version_with_structured_cli_error(tmp_path, value):
    _, saved, _ = _saved(tmp_path)
    raw = json.loads((saved / "backup.json").read_bytes())
    raw["format_version"] = value
    _write_json(saved / "backup.json", raw)
    before = _tree(saved)
    result = _cli("verify", saved)
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is False
    assert _tree(saved) == before


@pytest.mark.parametrize("field", [
    "db_user_version", "created_at_ms", "event_count", "event_seq_head",
])
def test_backup_manifest_numeric_fields_do_not_coerce_strings(tmp_path, field):
    _, saved, _ = _saved(tmp_path)
    raw = json.loads((saved / "backup.json").read_bytes())
    raw[field] = str(raw[field])
    _write_json(saved / "backup.json", raw)
    assert not backup.verify_backup(saved).ok


def test_verify_rejects_future_database_version_even_when_manifest_agrees(tmp_path):
    root, saved, _ = _saved(tmp_path)
    with closing(sqlite3.connect(saved / "events.db")) as conn:
        conn.execute("PRAGMA user_version=999")
        conn.commit()
    _refresh(saved, "events.db")
    raw = json.loads((saved / "backup.json").read_bytes())
    raw["db_user_version"] = 999
    _write_json(saved / "backup.json", raw)
    before = _tree(saved)
    result = _cli("verify", saved)
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is False
    assert _tree(saved) == before
    _plan_refused(root, saved)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_native_snapshot_parser_preserves_supported_history_without_creating_store(
    tmp_path, version,
):
    root, saved, receipt = _saved(tmp_path)
    rel = f"artifacts/snapshots/{receipt['run_id']}.json"
    raw = json.loads((saved / rel).read_bytes())
    if version < 3:
        for entry in raw["changes"]:
            entry.pop("root")
    historical = raw["changes"] if version == 1 else dict(raw, version=version)
    _write_json(saved / rel, historical)
    _refresh(saved, rel)
    body = (saved / rel).read_bytes()
    before = _tree(saved)
    changes = SnapshotStore._parse(body)
    assert len(changes) == 1
    assert changes[0].call_id == "overwrite"
    assert changes[0].before_state == receipt["before_state"]
    assert backup.verify_backup(saved).ok
    assert _tree(saved) == before and not root.exists()


def test_completed_native_undo_journal_remains_valid_backup_material(tmp_path):
    root, saved, receipt = _saved(tmp_path)
    rel = f"artifacts/undo/{receipt['run_id']}.json"
    raw = json.loads((saved / rel).read_bytes())
    raw["done"] = [dict(item, post_state=item["before_state"]) for item in raw["items"]]
    raw["status"] = "completed"
    _write_json(saved / rel, raw)
    _refresh(saved, rel)
    assert UndoJournal.from_dict(raw, run_id=receipt["run_id"]).completed
    before = _tree(saved)
    assert backup.verify_backup(saved).ok
    assert _tree(saved) == before and not root.exists()


@pytest.mark.parametrize("channel", ["blob_ref", "evidence"])
def test_receipt_reference_channels_protect_blobs_without_snapshot_dependency(tmp_path, channel):
    root = tmp_path / "data"
    preimage = b"legacy file receipt preimage"
    digest = hashlib.sha256(preimage).hexdigest()
    blob = root / "artifacts/blobs" / digest
    # 历史无 tool/backend 的凭据仍携带有效审计引用；两条通道分别测试，
    # 不允许一条通道的实现替另一条遗漏作证。
    payload = {"call_id": "legacy", "before_state": digest}
    if channel == "blob_ref":
        payload["blob_ref"] = str(blob)
    else:
        payload["evidence"] = [{"kind": "blob", "path": str(blob), "sha256": digest}]
    with closing(EventLog(root / "events.db")) as log:
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(preimage)
        log.append("legacy", "receipt", payload, legacy_write=True)
    plan = backup.cleanup_plan(root)
    assert plan["protected_count"] == 1 and plan["candidates"] == []
    saved = tmp_path / "backup"
    backup.create_backup(root, saved)
    shutil.rmtree(root)
    manifest = json.loads((saved / "backup.json").read_bytes())
    manifest["references"] = {}
    rel = f"artifacts/blobs/{digest}"
    manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != rel]
    (saved / rel).unlink()
    _write_json(saved / "backup.json", manifest)
    checked = backup.verify_backup(saved)
    assert not checked.ok and any(digest in problem for problem in checked.problems)


@pytest.mark.parametrize("version", [-1, 999])
def test_backup_refuses_to_publish_unrecognized_database_version(tmp_path, version):
    root, _ = _root(tmp_path)
    with closing(sqlite3.connect(root / "events.db")) as conn:
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
    before = _live_state(root)
    saved = tmp_path / "backup"
    with pytest.raises(backup.BackupInvalid):
        backup.create_backup(root, saved)
    assert not saved.exists() and _live_state(root) == before
