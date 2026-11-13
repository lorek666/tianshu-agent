"""实际EventLog/CLI前缀，历史SQL来自固定Git对象而非新初始化器。"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tianshu.errors import TianshuError
from tianshu.events import EventLog

FIXTURES = Path(__file__).parent / "fixtures" / "data_admission"


def make_db(root: Path, *, name="events.db", legacy=False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db = root / name
    with sqlite3.connect(db) as conn:
        conn.executescript(
            (FIXTURES / ("legacy-v2.sql" if legacy else "v0.sql")).read_text(encoding="utf-8")
        )
    return db


def execute(db, sql, params=()):
    with sqlite3.connect(db) as conn:
        conn.execute(sql, params)


def legacy_state(db):
    state = json.loads((FIXTURES / "legacy-completed.json").read_text(encoding="utf-8"))
    (db.parent / "migration.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def state_v2(db, *, origin=0, status="completed"):
    return {
        "format": "tianshu-migration",
        "format_version": 2,
        "db_path": str(db.resolve()),
        "from_version": origin,
        "target_version": 2,
        "status": status,
        "applied_steps": list(range(origin + 1, 3)),
        "current_step": None,
        "plan_sha256": "1" * 64,
        "backup": {"path": str((db.parent / "backup").resolve()), "manifest_sha256": "2" * 64},
        "baseline": {
            "sqlite_algorithm": "tianshu-sqlite-logical-v1",
            "sqlite_sha256": "3" * 64,
            "material_algorithm": "tianshu-material-inventory-v1",
            "material_sha256": "4" * 64,
        },
        "started_at_ms": 100,
        "updated_at_ms": 200,
    }


def write_state(db, state):
    db.with_name(db.name + ".migration.json").write_text(json.dumps(state), encoding="utf-8")


def snapshot(db):
    with sqlite3.connect(db) as conn:
        logical = tuple(conn.iterdump())
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    material = {
        str(p.relative_to(db.parent)): p.read_bytes()
        for p in db.parent.rglob("*")
        if p.is_file() and p != db and not p.name.endswith(("-wal", "-shm", "-journal"))
    }
    return logical, version, mode, material


def refuse_unchanged(db, *, code=None):
    before = snapshot(db)
    with pytest.raises(TianshuError) as exc:
        EventLog(db, blob_dir=db.parent / "new-payload")
    if code:
        assert exc.value.code == code
    assert snapshot(db) == before
    assert not (db.parent / "new-payload").exists()
    return exc.value


def test_future_actual_db_refused_before_ddl_and_blob_mkdir(tmp_path):
    db = make_db(tmp_path, name="custom.sqlite")
    execute(db, "PRAGMA user_version=999")
    refuse_unchanged(db, code="incompatible")


@pytest.mark.parametrize("version", [1, -1, 3, 999])
def test_unsupported_mechanism_refused(tmp_path, version):
    db = make_db(tmp_path)
    execute(db, f"PRAGMA user_version={version}")
    refuse_unchanged(db)


def test_v0_missing_version_row_allowed_but_missing_table_refused(tmp_path):
    db = make_db(tmp_path)
    log = EventLog(db)
    assert log.read_run("legacy")[0].payload_version == 0
    assert log.conn.execute("PRAGMA user_version").fetchone()[0] == 0
    log.close()
    execute(db, "DROP TABLE event_versions")
    refuse_unchanged(db)


def test_missing_entire_event_versions_refused_after_close_reopen(tmp_path):
    db = make_db(tmp_path)
    execute(db, "DROP TABLE event_versions")
    refuse_unchanged(db)
    refuse_unchanged(db)


@pytest.mark.parametrize("version", [999, -1, "broken", 1.5])
def test_bad_registered_payload_version_refused(tmp_path, version):
    db = make_db(tmp_path)
    execute(db, "INSERT INTO event_versions VALUES(1,'run.start',?)", (version,))
    refuse_unchanged(db, code="incompatible")


@pytest.mark.parametrize("version", ["4", "-1", "bad", "1.5", "true", " 3", "03"])
def test_bad_registered_memory_version_refused_before_alter(tmp_path, version):
    db = make_db(tmp_path)
    execute(db, "CREATE TABLE memory_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    execute(db, "INSERT INTO memory_meta VALUES('schema_version',?)", (version,))
    refuse_unchanged(db, code="incompatible")


@pytest.mark.parametrize("version", ["0", "1", "2", "3"])
def test_registered_historical_memory_versions_are_compatible(tmp_path, version):
    db = make_db(tmp_path)
    execute(db, "CREATE TABLE memory_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    execute(db, "INSERT INTO memory_meta VALUES('schema_version',?)", (version,))
    EventLog(db).close()


def test_custom_db_ignores_unrelated_default_db_progress(tmp_path):
    standard = make_db(tmp_path, legacy=True)
    legacy_state(standard)
    custom = make_db(tmp_path, name="custom.sqlite")
    EventLog(custom).close()
    execute(custom, "PRAGMA user_version=999")
    refuse_unchanged(custom)


def test_custom_db_obeys_own_sidecar_progress(tmp_path):
    db = make_db(tmp_path, name="custom.sqlite", legacy=True)
    write_state(db, state_v2(db, status="in_progress"))
    refuse_unchanged(db, code="migration_incomplete")


@pytest.mark.parametrize("origin", [0, 1, 2])
def test_both_completed_records_share_fact_not_bytes(tmp_path, origin):
    db = make_db(tmp_path, legacy=True)
    legacy_state(db)
    write_state(db, state_v2(db, origin=origin))
    EventLog(db).close()


@pytest.mark.parametrize("bad", ["json", "in_progress", "path", "steps", "missing_binding"])
def test_both_progress_files_must_agree(tmp_path, bad):
    db = make_db(tmp_path, legacy=True)
    legacy_state(db)
    state = state_v2(db)
    if bad == "in_progress":
        state["status"] = "in_progress"
    if bad == "path":
        state["db_path"] = str(tmp_path / "other.db")
    if bad == "steps":
        state["applied_steps"] = [2]
    if bad == "missing_binding":
        del state["backup"]
    write_state(db, state)
    if bad == "json":
        (tmp_path / "migration.json").write_text("{", encoding="utf-8")
    refuse_unchanged(db)


@pytest.mark.parametrize("new", [False, True])
def test_progress_format_in_wrong_sidecar_refused(tmp_path, new):
    db = make_db(tmp_path, legacy=True)
    if new:
        state = json.loads((FIXTURES / "legacy-completed.json").read_text(encoding="utf-8"))
        write_state(db, state)
    else:
        (tmp_path / "migration.json").write_text(json.dumps(state_v2(db)), encoding="utf-8")
    refuse_unchanged(db, code="progress_corrupt")


def test_v2_without_progress_refuses_eventlog_and_cli_unchanged(tmp_path):
    db = make_db(tmp_path, legacy=True)
    refuse_unchanged(db, code="migration_incomplete")
    before = snapshot(db)
    result = subprocess.run(
        [sys.executable, "-m", "tianshu.cli", "replay", "--run-id", "legacy", "--db", str(db)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "migration_incomplete" in result.stderr
    assert snapshot(db) == before


def test_v2_valid_completed_opens_and_legacy_only_remains_ordinary_compatible(tmp_path):
    db = make_db(tmp_path, legacy=True)
    legacy_state(db)
    before = (tmp_path / "migration.json").read_bytes()
    EventLog(db).close()
    assert (tmp_path / "migration.json").read_bytes() == before


def test_completed_state_does_not_freeze_later_business_writes(tmp_path):
    db = make_db(tmp_path, legacy=True)
    write_state(db, state_v2(db))
    before = db.with_name(db.name + ".migration.json").read_bytes()
    log = EventLog(db)
    log.append(
        "later",
        "run.start",
        {"task": "later", "workspace": "/fixture", "out": "x", "limits": {}, "tools": []},
        legacy_write=True,
    )
    log.close()
    EventLog(db).close()
    assert db.with_name(db.name + ".migration.json").read_bytes() == before


@pytest.mark.parametrize("evidence", ["-wal", "-journal", "-shm", ".migration.json", "blobs"])
@pytest.mark.parametrize("exists", [False, True])
def test_zero_main_with_historical_evidence_refused_before_connect(tmp_path, evidence, exists):
    db = tmp_path / "events.db"
    if exists:
        db.touch()
    if evidence == "blobs":
        (tmp_path / "blobs").mkdir()
        (tmp_path / "blobs" / "old").write_bytes(b"old history")
    else:
        db.with_name(db.name + evidence).write_bytes(b"historical evidence")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(TianshuError):
        EventLog(db, blob_dir=tmp_path / "new-payload")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    assert not (tmp_path / "new-payload").exists()


@pytest.mark.parametrize("exists", [False, True])
def test_true_new_empty_v0(tmp_path, exists):
    db = tmp_path / "events.db"
    if exists:
        db.touch()
    log = EventLog(db)
    assert log.conn.execute("PRAGMA user_version").fetchone()[0] == 0
    log.close()


@pytest.mark.parametrize("material", ["artifacts", "blobs", "memory_blobs"])
@pytest.mark.parametrize("exists", [False, True])
def test_new_database_allows_empty_material_directory_tree(tmp_path, material, exists):
    db = tmp_path / "events.db"
    if exists:
        db.touch()
    (tmp_path / material / "nested" / "empty").mkdir(parents=True)
    log = EventLog(db)
    try:
        assert log.conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        log.close()


@pytest.mark.parametrize("material", ["artifacts", "blobs", "memory_blobs"])
@pytest.mark.parametrize("content", [b"", b"old history"])
def test_new_database_refuses_nested_material_files(tmp_path, material, content):
    db = tmp_path / "events.db"
    old = tmp_path / material / "nested" / "old"
    old.parent.mkdir(parents=True)
    old.write_bytes(content)
    with pytest.raises(TianshuError, match="incompatible"):
        EventLog(db, blob_dir=tmp_path / "new-payload")
    assert not db.exists()
    assert old.read_bytes() == content
    assert not (tmp_path / "new-payload").exists()


def test_memory_db_does_not_read_cwd_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "migration.json").write_text("{", encoding="utf-8")
    EventLog(Path(":memory:")).close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["migration.json"]


def test_admission_checks_actual_connection(tmp_path):
    from tianshu.data_admission import check_connection

    good = make_db(tmp_path / "good")
    other = make_db(tmp_path / "other")
    with sqlite3.connect(other) as conn, pytest.raises(TianshuError):
        check_connection(conn, good)


def test_admission_forbidden_import_boundaries():
    root = Path(__file__).parents[1] / "src" / "tianshu"
    forbidden = {"backup", "migrate", "events", "memory", "loop", "store"}
    for name in ["data_state", "data_admission"]:
        tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not forbidden.intersection((node.module or "").split("."))
            elif isinstance(node, ast.Import):
                assert all(
                    not forbidden.intersection(alias.name.split(".")) for alias in node.names
                )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import tianshu.data_admission; "
            "assert not any('tianshu.'+m in sys.modules "
            "for m in ('backup','migrate','events','memory','loop'))",
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_memory_supported_version_bound_to_real_store():
    from tianshu.data_admission import SUPPORTED_MEMORY_SCHEMA_VERSION
    from tianshu.memory import _SCHEMA_VERSION

    assert SUPPORTED_MEMORY_SCHEMA_VERSION == _SCHEMA_VERSION


@pytest.mark.parametrize("bad", ["audit_type", "audit_steps", "index", "note"])
def test_completed_requires_real_mechanism_objects_and_audit(tmp_path, bad):
    db = make_db(tmp_path, legacy=True)
    legacy_state(db)
    if bad == "audit_type":
        execute(db, "UPDATE migration_log SET applied_at='bad' WHERE step=1")
    if bad == "audit_steps":
        execute(db, "DELETE FROM migration_log WHERE step=1")
    if bad == "index":
        execute(db, "DROP INDEX idx_migration_log_applied")
    if bad == "note":
        execute(db, "ALTER TABLE migration_log DROP COLUMN note")
    refuse_unchanged(db)


def test_live_wal_future_version_is_not_ignored(tmp_path):
    db = make_db(tmp_path)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA user_version=999")
        assert db.with_name(db.name + "-wal").stat().st_size > 0
        refuse_unchanged(db, code="incompatible")
    finally:
        conn.close()


@pytest.mark.parametrize("bad", ["hidden_column", "descending_index", "collation"])
def test_completed_rejects_unregistered_mechanism_shape(tmp_path, bad):
    db = make_db(tmp_path, legacy=True)
    legacy_state(db)
    if bad == "hidden_column":
        execute(
            db,
            "ALTER TABLE migration_log ADD COLUMN extra INTEGER "
            "GENERATED ALWAYS AS (step*2) VIRTUAL",
        )
    else:
        execute(db, "DROP INDEX idx_migration_log_applied")
        suffix = "DESC" if bad == "descending_index" else "COLLATE NOCASE"
        execute(db, f"CREATE INDEX idx_migration_log_applied ON migration_log(applied_at {suffix})")
    refuse_unchanged(db)


def test_refusal_releases_shared_access(tmp_path):
    from tianshu.maintenance import acquire_database_access

    db = make_db(tmp_path)
    execute(db, "PRAGMA user_version=999")
    refuse_unchanged(db)
    with acquire_database_access(db, exclusive=True):
        pass


def test_actual_custom_future_cli_and_reopen_refuse_unrelated_v2(tmp_path):
    standard = make_db(tmp_path / "unrelated", legacy=True)
    legacy_state(standard)
    db = make_db(tmp_path / "actual", name="custom.sqlite")
    execute(db, "PRAGMA user_version=999")
    before = snapshot(db)
    result = subprocess.run(
        [sys.executable, "-m", "tianshu.cli", "replay", "--run-id", "legacy", "--db", str(db)],
        cwd=standard.parent,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 2
    assert "incompatible" in result.stderr
    assert snapshot(db) == before
    refuse_unchanged(db)
    refuse_unchanged(db)
