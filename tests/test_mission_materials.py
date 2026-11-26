"""Mission 首轮输入是持久事实：真实 GC 与备份必须追踪两类事件里的正文引用。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from support import engine_for, stack_for

from tianshu.backup import (
    MANIFEST_NAME,
    BackupInvalid,
    ReferenceUnknown,
    collect_references,
    create_backup,
    verify_backup,
)
from tianshu.control import ControlPlane
from tianshu.data_gc import run_data_gc
from tianshu.events import EventLog, canonical_bytes
from tianshu.mission import mission_messages, pending_mission_for
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.replay import replay


def _mission_root(tmp_path, *, pending, blob_dir="blobs", inline=False, large=False):
    root = tmp_path / "data"
    with closing(EventLog(
        root / "events.db", blob_dir=None if inline else root / blob_dir,
    )) as log:
        stack = stack_for(log, data_root=root)
        task = stack.tasks.create_task(
            "保存首轮输入，不需要文件工作区。" + ("资料" * 50_000 if large else ""),
            mcp_servers=["probe"] if pending else [],
        )
        plane = ControlPlane(stack, model=ScriptedModel([]))
        rid = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
        event_type = "run.mission_pending" if pending else "run.mission"
        fact = log.read_run(rid, event_types=(event_type,))[0]
        row = log.conn.execute(
            "SELECT payload_ref FROM events WHERE seq=?", (fact.seq,),
        ).fetchone()
        assert (row["payload_ref"] is not None) == large
        assert fact.payload["material_storage"] == ("inline" if inline else "blob")
    paths = {
        "messages_ref": f"{blob_dir}/{fact.payload['messages_ref'].rsplit(':', 1)[-1]}",
        "prompt.content_ref": (
            f"{blob_dir}/{fact.payload['mission']['prompt']['content_ref'].rsplit(':', 1)[-1]}"
        ),
    }
    return root, rid, fact, paths


def _orphan(root, blob_dir="blobs"):
    data = b"actually unreferenced material"
    path = root / blob_dir / hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _assert_saved_input(root, rid, fact, *, blob_dir="blobs"):
    with closing(EventLog(root / "events.db", blob_dir=root / blob_dir)) as log:
        if fact.type == "run.mission_pending":
            assert pending_mission_for(log, rid) == fact.payload
        else:
            assert mission_messages(log, rid) == fact.payload["messages"]
            stack = stack_for(log, data_root=root)
            model = ScriptedModel([ModelResponse("end_turn", content="输入仍可恢复。")])
            assert engine_for(stack, model).resume_run(rid).state == "succeeded"
        report = replay(log, rid)
        assert report.ok, report.problems


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("blob_dir", ["blobs", "artifacts/blobs"])
def test_mission_blobs_survive_real_gc_and_remain_readable(tmp_path, pending, blob_dir):
    root, rid, fact, paths = _mission_root(tmp_path, pending=pending, blob_dir=blob_dir)
    orphan = _orphan(root, blob_dir)
    # 正文远超保留期也必须保护；真正孤儿须仍可回收，不能通过年龄或禁用 GC 掩盖。
    for rel in [*paths.values(), orphan.relative_to(root).as_posix()]:
        os.utime(root / rel, (1, 1))
    index = collect_references(root, require_static_db=True)
    assert set(paths.values()) <= set(index.protected_blobs)
    assert not set(paths.values()) & set(index.orphan_blobs)
    assert index.orphan_blobs == [orphan.relative_to(root).as_posix()]

    preview, code = run_data_gc(root, dry_run=True, clock=lambda: 4_102_444_800_000)
    assert code == 3, preview
    assert {item["path"] for item in preview["candidates"]["orphan_blobs"]} == {
        orphan.relative_to(root).as_posix(),
    }
    report, code = run_data_gc(root, approve=True, clock=lambda: 4_102_444_800_000)
    if os.name == "posix":
        assert code == 0, report
        assert report["deleted"]["orphan_blobs_count"] == 1
        assert not orphan.exists()
    else:
        assert code == 2 and report["code"] == "incompatible", report
        assert set(report["deleted"].values()) == {0}
        assert orphan.is_file()
    assert all((root / rel).is_file() for rel in paths.values())
    _assert_saved_input(root, rid, fact, blob_dir=blob_dir)

    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)
    assert {item["path"] for item in manifest.references["mission_blobs"]} == set(paths.values())
    checked = verify_backup(dest)
    assert checked.ok, checked.problems


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("inline", [False, True])
def test_historical_mission_keeps_blob_and_inline_semantics(tmp_path, pending, inline):
    root, rid, fact, paths = _mission_root(tmp_path, pending=pending, inline=inline)
    assert all((root / rel).exists() is not inline for rel in paths.values())
    index = collect_references(root, require_static_db=True)
    if inline:
        assert not set(paths.values()) & set(index.protected_blobs)
    else:
        assert set(paths.values()) <= set(index.protected_blobs)
    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)
    assert {item["path"] for item in manifest.references["mission_blobs"]} == (
        set() if inline else set(paths.values())
    )
    # 旧清单没有新增引用组仍合法；校验须由保存的事件事实推导，而非依赖新字段。
    raw = json.loads((dest / MANIFEST_NAME).read_bytes())
    del raw["references"]["mission_blobs"]
    (dest / MANIFEST_NAME).write_bytes(canonical_bytes(raw))
    _assert_saved_input(root, rid, fact)
    shutil.rmtree(root)
    checked = verify_backup(dest)
    assert checked.ok, checked.problems


@pytest.mark.parametrize("pending", [False, True])
def test_inline_mission_does_not_protect_incidental_content_files(tmp_path, pending):
    root, rid, fact, paths = _mission_root(tmp_path, pending=pending, inline=True)
    (root / "blobs").mkdir()
    (root / paths["messages_ref"]).write_bytes(canonical_bytes(fact.payload["messages"]))
    (root / paths["prompt.content_ref"]).write_bytes(fact.payload["template_content"].encode())
    index = collect_references(root, require_static_db=True)
    assert not set(paths.values()) & set(index.protected_blobs)
    assert set(index.orphan_blobs) == set(paths.values())
    # inline 的内容寻址身份不等于持久 blob 依赖，删掉偶然副本后历史输入仍可读。
    for rel in paths.values():
        (root / rel).unlink()
    _assert_saved_input(root, rid, fact)


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("material", ["messages_ref", "prompt.content_ref"])
@pytest.mark.parametrize("fault", ["missing", "tampered"])
def test_saved_db_rederives_mission_refs_when_manifest_hides_damage(
    tmp_path, pending, material, fault,
):
    root, _, _, paths = _mission_root(tmp_path, pending=pending, large=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    rel = paths[material]
    raw = json.loads((dest / MANIFEST_NAME).read_bytes())
    raw["references"] = {}
    if fault == "missing":
        (dest / rel).unlink()
        raw["files"] = [item for item in raw["files"] if item["path"] != rel]
    else:
        data = b"replaced while claiming a complete backup"
        (dest / rel).write_bytes(data)
        item = next(item for item in raw["files"] if item["path"] == rel)
        item.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    (dest / MANIFEST_NAME).write_bytes(canonical_bytes(raw))
    shutil.rmtree(root)

    checked = verify_backup(dest)
    assert not checked.ok
    assert any(Path(rel).name in problem for problem in checked.problems), checked.problems


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("material", ["messages_ref", "prompt.content_ref"])
@pytest.mark.parametrize("fault", ["missing", "tampered"])
def test_damaged_mission_material_refuses_gc_and_backup(tmp_path, pending, material, fault):
    root, _, _, paths = _mission_root(tmp_path, pending=pending)
    orphan = _orphan(root)
    path = root / paths[material]
    if fault == "missing":
        path.unlink()
    else:
        path.write_bytes(b"tampered mission material")
    with pytest.raises(ReferenceUnknown, match=path.name):
        collect_references(root, require_static_db=True)
    report, code = run_data_gc(root, approve=True, clock=lambda: 4_102_444_800_000)
    assert code == 2 and report["code"] == "reference_unknown", report
    assert set(report["deleted"].values()) == {0}
    assert orphan.is_file()
    dest = tmp_path / "backup"
    with pytest.raises(BackupInvalid, match=path.name):
        create_backup(root, dest)
    assert not dest.exists()


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("fault", ["storage", "digest"])
def test_invalid_mission_event_fails_closed_even_with_consistent_event_hash(
    tmp_path, pending, fault,
):
    root, rid, fact, _ = _mission_root(tmp_path, pending=pending)
    payload = json.loads(canonical_bytes(fact.payload))
    if fault == "storage":
        payload["material_storage"] = "unsupported"
    else:
        payload["messages_digest"] = "0" * 64
    encoded = canonical_bytes(payload)
    # 仅播种非法历史行；事件本身哈希一致不能洗白损坏的 Mission 契约。
    with closing(sqlite3.connect(root / "events.db")) as conn, conn:
        conn.execute(
            "INSERT INTO events(run_id,type,ts,actor_kind,actor_id,payload,payload_hash) "
            "VALUES (?,?,1,'core','',?,?)",
            (rid, fact.type, encoded.decode(), hashlib.sha256(encoded).hexdigest()),
        )
    with pytest.raises(ReferenceUnknown, match="Mission"):
        collect_references(root, require_static_db=True)
    with pytest.raises(BackupInvalid, match="Mission"):
        create_backup(root, tmp_path / "backup")
