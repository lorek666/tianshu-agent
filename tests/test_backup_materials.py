"""备份必须由保存的数据库证明材料完整，不能只相信清单里的自述。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from tianshu import backup, memory_control
from tianshu.backup import BackupInvalid, BackupRefused, create_backup, verify_backup
from tianshu.contents import ContentStore
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.resources import MemoryScope
from tianshu.snapshot import SnapshotStore

MEMORY_TEXT = "每次报告先列结论，再写可核对的来源。"
CONTENT_BYTES = "保存的联网研究原始资料。".encode()


def _make_root(
    tmp_path: Path,
    *,
    payload_dir: str = "blobs",
    memory: bool = True,
    content: bool = True,
    erased: bool = False,
) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "data"
    log = EventLog(root / "events.db", blob_dir=root / payload_dir)
    try:
        stack = build_stack(log, data_root=root)
        log.append(
            "r_materials", "tool.result",
            {"call_id": "c_read", "tool_id": "read_text", "status": "succeeded",
             "effect_state": "none_applied", "output": {"content": "x" * 300_000}},
        )
        row = log.conn.execute(
            "SELECT payload_ref, payload_hash FROM events WHERE payload_ref IS NOT NULL"
        ).fetchone()
        refs = {"payload": row["payload_hash"], "payload_path": row["payload_ref"]}
        if memory:
            result = memory_control.remember(
                stack, MemoryScope.user(), kind="preference", content=MEMORY_TEXT
            )
            assert result.ok, result
            memory_id = result.output["memory_id"]
            entry = stack.memory.get(memory_id, allowed=[MemoryScope.user()])
            assert entry is not None
            refs["memory"] = entry.content_ref
            if erased:
                result = memory_control.erase(stack, entry)
                assert result.ok, result
                assert not (root / "memory_blobs" / entry.content_ref).exists()
        if content:
            digest = hashlib.sha256(CONTENT_BYTES).hexdigest()
            path = stack.contents.blob_dir / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(CONTENT_BYTES)
            stack.contents.record(digest, CONTENT_BYTES, task_id="t_materials")
            refs["content"] = digest
    finally:
        log.close()
    return root, refs


def _read_manifest(dest: Path) -> dict:
    return json.loads((dest / backup.MANIFEST_NAME).read_bytes())


def _write_manifest(dest: Path, raw: dict) -> None:
    (dest / backup.MANIFEST_NAME).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )


def _remove_material_and_claims(dest: Path, rel: str) -> None:
    """模拟旧备份漏材料，也移除清单中的对应声明，不能靠清单自身抓漏。"""
    (dest / rel).unlink(missing_ok=True)
    raw = _read_manifest(dest)
    raw["files"] = [entry for entry in raw["files"] if entry["path"] != rel]
    raw["references"] = {}
    _write_manifest(dest, raw)


def _refresh_db_file_hash(dest: Path) -> None:
    raw = _read_manifest(dest)
    data = (dest / "events.db").read_bytes()
    entry = next(item for item in raw["files"] if item["path"] == "events.db")
    entry.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    _write_manifest(dest, raw)


def test_backup_covers_cli_payload_memory_and_content(tmp_path):
    root, refs = _make_root(tmp_path)
    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)

    expected = {
        f"blobs/{refs['payload']}",
        f"memory_blobs/{refs['memory']}",
        f"artifacts/blobs/{refs['content']}",
    }
    assert expected <= {entry.path for entry in manifest.files}
    assert (dest / "memory_blobs" / refs["memory"]).read_text() == MEMORY_TEXT
    assert (dest / "artifacts" / "blobs" / refs["content"]).read_bytes() == CONTENT_BYTES
    checked = verify_backup(dest)
    assert checked.ok, checked.problems
    assert checked.to_dict()["ok"] is True
    assert checked.to_dict()["manifest"]["format_version"] == 1


@pytest.mark.parametrize("kind", ["payload", "memory", "content"])
def test_verify_rederives_references_when_manifest_hides_material(tmp_path, kind):
    root, refs = _make_root(tmp_path, payload_dir="artifacts/blobs")
    dest = tmp_path / "backup"
    create_backup(root, dest)
    rel = (
        f"memory_blobs/{refs[kind]}" if kind == "memory"
        else f"artifacts/blobs/{refs[kind]}"
    )
    _remove_material_and_claims(dest, rel)

    checked = verify_backup(dest)
    assert not checked.ok
    assert any(refs[kind] in problem for problem in checked.problems), checked.problems


@pytest.mark.parametrize("kind", ["memory", "content"])
def test_create_refuses_to_publish_missing_registered_material(tmp_path, kind):
    root, refs = _make_root(tmp_path, payload_dir="artifacts/blobs")
    rel = (
        f"memory_blobs/{refs[kind]}" if kind == "memory"
        else f"artifacts/blobs/{refs[kind]}"
    )
    (root / rel).unlink()
    dest = tmp_path / "backup"

    with pytest.raises(BackupInvalid, match=refs[kind]):
        create_backup(root, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob(".backup.partial-*"))


def test_erased_memory_does_not_require_deleted_body(tmp_path):
    root, refs = _make_root(tmp_path, payload_dir="artifacts/blobs", erased=True)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    assert not (dest / "memory_blobs" / refs["memory"]).exists()
    checked = verify_backup(dest)
    assert checked.ok, checked.problems


def test_verify_uses_saved_materials_after_original_root_is_gone(tmp_path):
    root, _ = _make_root(tmp_path, payload_dir="artifacts/blobs")
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    checked = verify_backup(dest)
    assert checked.ok, checked.problems


def test_verify_ignores_original_root_replaced_by_symlink(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    shutil.rmtree(root)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _make_symlink(root, replacement)

    checked = verify_backup(dest)
    assert checked.ok, checked.problems


def test_create_does_not_publish_database_with_seq_gap(tmp_path):
    root, _ = _make_root(tmp_path)
    with sqlite3.connect(root / "events.db") as conn:
        conn.execute(
            "INSERT INTO events(seq,run_id,type,ts,actor_kind,actor_id,"
            "payload,payload_hash,idem_key) "
            "VALUES (99,'gap','run.cursor',1,'core','','{}',?,'gap')",
            (hashlib.sha256(b"{}").hexdigest(),),
        )
    dest = tmp_path / "backup"

    with pytest.raises(BackupInvalid, match="seq 不连续"):
        create_backup(root, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob(".backup.partial-*"))


@pytest.mark.parametrize("external", [False, True])
def test_saved_event_payload_reads_only_verified_backup_materials(tmp_path, external):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)
    with backup._connect_readonly(dest / "events.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT seq,payload AS payload_inline,payload_ref,payload_hash "
            f"FROM events WHERE payload_ref IS {'NOT ' if external else ''}NULL LIMIT 1"
        ).fetchone()
    assert row is not None
    shutil.rmtree(root)

    payload = backup.read_saved_event_payload(
        dest, row, source_root=Path(manifest.source_path)
    )
    assert isinstance(payload, dict)
    if external:
        assert payload["output"]["content"] == "x" * 300_000


@pytest.mark.parametrize("fault", ["hash", "both", "neither", "json", "outside"])
def test_saved_event_payload_refuses_invalid_payload(tmp_path, fault):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    manifest = create_backup(root, dest)
    row = {"seq": 1, "payload_inline": "{}", "payload_ref": None,
           "payload_hash": hashlib.sha256(b"{}").hexdigest()}
    if fault == "hash":
        row["payload_hash"] = "0" * 64
    elif fault == "both":
        row["payload_ref"] = str(root / "blobs" / ("0" * 64))
    elif fault == "neither":
        row["payload_inline"] = None
    elif fault == "json":
        row["payload_inline"] = "not JSON"
        row["payload_hash"] = hashlib.sha256(b"not JSON").hexdigest()
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        _make_symlink(outside / "alias", root)
        row["payload_inline"] = None
        row["payload_ref"] = str(outside / "alias" / "blobs" / ("0" * 64))

    with pytest.raises(backup.BackupError):
        backup.read_saved_event_payload(
            dest, row, source_root=Path(manifest.source_path)
        )


def test_verify_rejects_content_size_mismatch_even_with_updated_db_hash(tmp_path):
    root, refs = _make_root(tmp_path, payload_dir="artifacts/blobs")
    dest = tmp_path / "backup"
    create_backup(root, dest)
    with sqlite3.connect(dest / "events.db") as conn:
        conn.execute("UPDATE content_objects SET size = size + 1 WHERE digest = ?",
                     (refs["content"],))
    _refresh_db_file_hash(dest)

    checked = verify_backup(dest)
    assert not checked.ok
    assert any("大小" in problem and refs["content"] in problem
               for problem in checked.problems), checked.problems


def test_verify_rejects_invalid_memory_digest_without_reading_outside(tmp_path):
    root, _ = _make_root(tmp_path, payload_dir="artifacts/blobs")
    dest = tmp_path / "backup"
    create_backup(root, dest)
    with sqlite3.connect(dest / "events.db") as conn:
        conn.execute("UPDATE memories SET content_ref = '../outside'")
    _refresh_db_file_hash(dest)
    checked = verify_backup(dest)
    assert not checked.ok
    assert any("content_ref" in problem for problem in checked.problems), checked.problems


def _make_symlink(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前平台不能创建符号链接：{exc}")


def test_backup_refuses_symlink_material_instead_of_copying_external_data(tmp_path):
    root, _ = _make_root(tmp_path, payload_dir="artifacts/blobs")
    outside = tmp_path / "outside"
    outside.write_bytes(b"not part of the data root")
    _make_symlink(root / "artifacts" / "external", outside)
    dest = tmp_path / "backup"
    with pytest.raises(BackupRefused, match="符号链接"):
        create_backup(root, dest)
    assert not dest.exists()


def test_verify_refuses_symlink_even_when_hash_matches(tmp_path):
    root, refs = _make_root(tmp_path, payload_dir="artifacts/blobs")
    dest = tmp_path / "backup"
    create_backup(root, dest)
    blob = dest / "artifacts" / "blobs" / refs["content"]
    outside = tmp_path / "outside"
    outside.write_bytes(blob.read_bytes())
    blob.unlink()
    _make_symlink(blob, outside)
    checked = verify_backup(dest)
    assert not checked.ok
    assert any("符号链接" in problem for problem in checked.problems), checked.problems


def test_invalid_manifest_path_is_not_opened(tmp_path, monkeypatch):
    root, _ = _make_root(tmp_path, payload_dir="artifacts/blobs")
    dest = tmp_path / "backup"
    create_backup(root, dest)
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not read")
    raw = _read_manifest(dest)
    raw["files"].append({"path": "../outside", "sha256": "0" * 64,
                         "size": outside.stat().st_size, "category": "artifact_other"})
    _write_manifest(dest, raw)
    original = backup.sha256_file

    def guarded(path):
        assert Path(path).resolve().is_relative_to(dest.resolve()), path
        return original(path)

    monkeypatch.setattr(backup, "sha256_file", guarded)
    checked = verify_backup(dest)
    assert not checked.ok
    assert any("路径非法" in problem for problem in checked.problems), checked.problems


def test_readonly_connection_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "not-a-database.db"
    with pytest.raises(sqlite3.Error):
        backup._connect_readonly(path)
    assert not path.exists()


@pytest.mark.parametrize("rel,raw", [
    ("artifacts/snapshots/invalid.json", [1]),
    ("artifacts/undo/invalid.json", []),
])
def test_create_refuses_malformed_saved_material_manifest(tmp_path, rel, raw):
    root, _ = _make_root(tmp_path)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    dest = tmp_path / "backup"

    with pytest.raises(BackupInvalid, match="读不出来"):
        create_backup(root, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob(".backup.partial-*"))


def test_cleanup_protects_registered_content_and_unerased_memory(tmp_path):
    root, refs = _make_root(tmp_path)
    log = EventLog(root / "events.db")
    try:
        contents = ContentStore(log, blob_dir=root / "artifacts" / "blobs")
        assert contents.object(refs["content"]) is not None
        assert contents.visible(refs["content"], "t_materials")
    finally:
        log.close()
    # 本用例检查无删除/改写；活源 WAL 的协调文件行为由下方专门用例覆盖。
    with closing(sqlite3.connect(root / "events.db")) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    orphan_rel = f"artifacts/blobs/{'f' * 64}"
    (root / orphan_rel).write_bytes(b"unreferenced artifact")
    before = {path.relative_to(root).as_posix(): path.read_bytes()
              for path in root.rglob("*") if path.is_file()}

    index = backup.collect_references(root)
    assert f"artifacts/blobs/{refs['content']}" in index.protected_blobs
    assert f"memory_blobs/{refs['memory']}" in index.protected_blobs
    assert index.orphan_blobs == [orphan_rel]
    plan = backup.cleanup_plan(root)
    assert plan["dry_run"] is True
    assert plan["candidates"] == [{"path": orphan_rel, "category": "artifact_blob"}]
    after = {path.relative_to(root).as_posix(): path.read_bytes()
             for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_cleanup_does_not_expand_candidates_to_memory_or_require_erased_body(tmp_path):
    root, refs = _make_root(tmp_path, erased=True)
    unregistered = root / "memory_blobs" / ("f" * 64)
    unregistered.write_bytes(b"outside the artifact-only candidate scope")

    index = backup.collect_references(root)
    assert f"memory_blobs/{refs['memory']}" not in index.protected_blobs
    assert backup.cleanup_plan(root)["candidates"] == []
    assert unregistered.is_file()


@pytest.mark.parametrize("fault", [
    "content_digest", "content_size", "memory_digest", "memory_erased_at", "table_shape",
    "missing_content_table", "dangling_content_grant",
])
def test_cleanup_refuses_invalid_projection_references(tmp_path, fault):
    root, _ = _make_root(tmp_path)
    sql = {
        "content_digest": "UPDATE content_objects SET digest = '../outside'",
        "content_size": "UPDATE content_objects SET size = -1",
        "memory_digest": "UPDATE memories SET content_ref = '../outside'",
        "memory_erased_at": "UPDATE memories SET erased_at = -1",
        "table_shape": "ALTER TABLE content_objects RENAME COLUMN digest TO invalid_digest",
        "missing_content_table": "DROP TABLE content_objects",
        "dangling_content_grant": "DELETE FROM content_objects",
    }[fault]
    with closing(sqlite3.connect(root / "events.db")) as conn:
        conn.execute(sql)
        conn.commit()

    with pytest.raises(backup.ReferenceUnknown):
        backup.collect_references(root)
    with pytest.raises(backup.ReferenceUnknown):
        backup.cleanup_plan(root)


@pytest.mark.parametrize("kind", ["content", "memory"])
@pytest.mark.parametrize("fault", ["missing", "corrupt", "symlink"])
def test_cleanup_refuses_unverifiable_projection_material(tmp_path, kind, fault):
    root, refs = _make_root(tmp_path)
    rel = (f"artifacts/blobs/{refs[kind]}" if kind == "content"
           else f"memory_blobs/{refs[kind]}")
    material = root / rel
    if fault == "missing":
        material.unlink()
    elif fault == "corrupt":
        material.write_bytes(b"modified material")
    else:
        outside = tmp_path / "outside"
        outside.write_bytes(material.read_bytes())
        material.unlink()
        _make_symlink(material, outside)

    with pytest.raises(backup.ReferenceUnknown, match=refs[kind]):
        backup.collect_references(root)
    with pytest.raises(backup.ReferenceUnknown, match=refs[kind]):
        backup.cleanup_plan(root)


def test_cleanup_refuses_content_size_mismatch(tmp_path):
    root, refs = _make_root(tmp_path)
    with closing(sqlite3.connect(root / "events.db")) as conn:
        conn.execute("UPDATE content_objects SET size = size + 1")
        conn.commit()

    with pytest.raises(backup.ReferenceUnknown, match=refs["content"]):
        backup.cleanup_plan(root)


def test_verify_wal_mode_saved_database_never_creates_companion_files(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    with closing(sqlite3.connect(dest / "events.db")) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    _refresh_db_file_hash(dest)
    before = {path.relative_to(dest).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in dest.rglob("*") if path.is_file()}

    checked = verify_backup(dest)
    assert checked.ok, checked.problems
    after = {path.relative_to(dest).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in dest.rglob("*") if path.is_file()}
    assert after == before
    repeated = verify_backup(dest)
    assert repeated.ok, repeated.problems


@pytest.mark.parametrize("rel", [
    "events.db-wal", "events.db-shm", "events.db-journal",
    "EVENTS.DB-WAL", "events.db-SHM", "Events.Db-Journal",
])
def test_verify_rejects_database_companions_even_when_manifest_declares_them(tmp_path, rel):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    data = b"an unmerged database companion must not be ignored"
    (dest / rel).write_bytes(data)
    raw = _read_manifest(dest)
    raw["files"].append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data), "category": "event_log_aux"})
    _write_manifest(dest, raw)

    checked = verify_backup(dest)
    assert not checked.ok
    assert any("伴随文件" in problem and rel in problem
               for problem in checked.problems), checked.problems
    assert (dest / rel).read_bytes() == data


def test_online_backup_includes_committed_source_wal_before_checkpoint(tmp_path):
    root, _ = _make_root(tmp_path)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        log.conn.execute("PRAGMA wal_autocheckpoint=0")
        log.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with closing(sqlite3.connect(
            (root / "events.db").as_uri() + "?mode=ro&immutable=1", uri=True
        )) as checkpointed:
            before = checkpointed.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        log.append(
            "r_source_wal", "tool.result",
            {"call_id": "wal_call", "tool_id": "read_text", "status": "succeeded",
             "effect_state": "none_applied", "output": {"content": "committed in WAL"}},
        )
        with closing(sqlite3.connect(
            (root / "events.db").as_uri() + "?mode=ro&immutable=1", uri=True
        )) as checkpointed:
            assert checkpointed.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        assert (root / "events.db-wal").stat().st_size > 0
        dest = tmp_path / "backup"

        manifest = create_backup(root, dest)
        assert manifest.event_count == before + 1
        with closing(sqlite3.connect(dest / "events.db")) as saved:
            row = saved.execute(
                "SELECT payload FROM events WHERE run_id = 'r_source_wal'"
            ).fetchone()
            assert json.loads(row[0])["output"]["content"] == "committed in WAL"
        checked = verify_backup(dest)
        assert checked.ok, checked.problems
    finally:
        log.close()


def test_cleanup_protects_content_committed_only_in_active_source_wal(tmp_path):
    root, _ = _make_root(tmp_path)
    log = EventLog(root / "events.db")
    try:
        contents = ContentStore(log, blob_dir=root / "artifacts" / "blobs")
        log.conn.execute("PRAGMA wal_autocheckpoint=0")
        log.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        data = b"new content projection committed only in the active WAL"
        digest = hashlib.sha256(data).hexdigest()
        material = root / "artifacts" / "blobs" / digest
        material.write_bytes(data)
        assert contents.record(digest, data, task_id="t_active_wal")
        assert contents.visible(digest, "t_active_wal")
        with closing(sqlite3.connect(
            (root / "events.db").as_uri() + "?mode=ro&immutable=1", uri=True
        )) as checkpointed:
            assert checkpointed.execute(
                "SELECT 1 FROM content_objects WHERE digest = ?", (digest,)
            ).fetchone() is None

        rel = f"artifacts/blobs/{digest}"
        index = backup.collect_references(root)
        assert rel in index.protected_blobs
        assert rel not in {item["path"] for item in backup.cleanup_plan(root)["candidates"]}
        assert material.read_bytes() == data
    finally:
        log.close()


def _make_aliased_root(tmp_path):
    canonical_parent = tmp_path / "canonical"
    canonical_parent.mkdir()
    alias_parent = tmp_path / "alias"
    _make_symlink(alias_parent, canonical_parent)
    root, refs = _make_root(alias_parent)
    snapshots = SnapshotStore(root / "artifacts")
    snapshots.init_run("r_alias")
    snapshots.record_preimage(
        "r_alias", "c_alias", "target.txt", b"snapshot through the source root alias",
        "a" * 64, root=tmp_path,
    )
    return root, canonical_parent / "data", alias_parent, refs


@pytest.mark.parametrize("canonical_input", [False, True])
def test_backup_records_only_verified_source_root_aliases(tmp_path, canonical_input):
    alias_root, canonical_root, _, refs = _make_aliased_root(tmp_path)
    dest = tmp_path / "backup"

    manifest = create_backup(canonical_root if canonical_input else alias_root, dest)
    assert manifest.source_path == str(canonical_root)
    assert manifest.source_aliases == [str(alias_root)]
    assert manifest.to_dict()["source_aliases"] == [str(alias_root)]
    assert manifest.references["snapshot_blobs"]
    assert (dest / "blobs" / refs["payload"]).is_file()
    checked = verify_backup(dest)
    assert checked.ok, checked.problems
    assert backup.collect_references(canonical_root).protected_blobs


@pytest.mark.parametrize("canonical_input", [False, True])
def test_backup_accepts_actual_mac_tmp_alias_with_either_input_spelling(canonical_input):
    if not Path("/tmp").is_dir() or Path("/tmp").resolve() == Path("/tmp"):
        pytest.skip("当前平台没有 /tmp 的系统路径别名")
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="tianshu-alias-") as raw:
        root, _ = _make_root(Path(raw))
        dest = Path(raw) / "backup"
        manifest = create_backup(root.resolve() if canonical_input else root, dest)
        assert str(root) in manifest.source_aliases
        assert manifest.source_path == str(root.resolve())
        checked = verify_backup(dest)
        assert checked.ok, checked.problems


@pytest.mark.parametrize("replace_alias", [False, True])
def test_saved_aliases_verify_without_resolving_any_source(tmp_path, monkeypatch, replace_alias):
    alias_root, canonical_root, alias_parent, _ = _make_aliased_root(tmp_path)
    dest = tmp_path / "backup"
    manifest = create_backup(canonical_root, dest)
    shutil.rmtree(canonical_root)
    if replace_alias:
        alias_parent.unlink()
        replacement = tmp_path / "replacement"
        (replacement / "data").mkdir(parents=True)
        _make_symlink(alias_parent, replacement)
    resolve = Path.resolve

    def forbid_source_resolve(path, *args, **kwargs):
        assert not any(path == root or root in path.parents
                       for root in (alias_root, canonical_root)), path
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", forbid_source_resolve)
    checked = verify_backup(dest)
    assert checked.ok, checked.problems
    with closing(backup._connect_saved_readonly(dest / "events.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload AS payload_inline,payload_ref,payload_hash "
            "FROM events WHERE payload_ref IS NOT NULL LIMIT 1"
        ).fetchone()
    payload = backup.read_saved_event_payload(
        dest, row, source_root=Path(manifest.source_path),
        source_aliases=manifest.source_aliases,
    )
    assert payload["output"]["content"] == "x" * 300_000


def test_removing_saved_source_alias_cannot_hide_reference_problem(tmp_path):
    _, canonical_root, _, _ = _make_aliased_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(canonical_root, dest)
    raw = _read_manifest(dest)
    raw.pop("source_aliases")
    raw["references"] = {}
    _write_manifest(dest, raw)
    shutil.rmtree(canonical_root)

    checked = verify_backup(dest)
    assert not checked.ok
    assert any("payload" in problem or "快照清单" in problem
               for problem in checked.problems), checked.problems


def test_create_cannot_promote_unrelated_external_blobs_to_source_alias(tmp_path):
    outside = tmp_path / "unrelated" / "blobs"
    root, _ = _make_root(tmp_path, payload_dir=str(outside))
    dest = tmp_path / "backup"
    with pytest.raises(BackupInvalid):
        create_backup(root, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob(".backup.partial-*"))


def test_source_root_alias_does_not_allow_symlink_material(tmp_path):
    alias_root, canonical_root, _, refs = _make_aliased_root(tmp_path)
    blob = canonical_root / "artifacts" / "blobs" / refs["content"]
    outside = tmp_path / "outside"
    outside.write_bytes(blob.read_bytes())
    blob.unlink()
    _make_symlink(blob, outside)
    with pytest.raises(BackupRefused, match="符号链接"):
        create_backup(alias_root, tmp_path / "backup")


def test_legacy_backup_without_alias_field_still_verifies_canonical_references(tmp_path):
    root, _ = _make_root(tmp_path)
    dest = tmp_path / "backup"
    create_backup(root, dest)
    raw = _read_manifest(dest)
    raw.pop("source_aliases", None)
    _write_manifest(dest, raw)
    checked = verify_backup(dest)
    assert checked.ok, checked.problems
