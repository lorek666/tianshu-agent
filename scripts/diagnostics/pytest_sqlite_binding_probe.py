"""Observe the migration audit fixture on the original Windows runner configuration."""

import json
import os
import sqlite3
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest


def inspect(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return {
            "header": path.read_bytes()[:100].hex(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "integrity": conn.execute("PRAGMA integrity_check").fetchall(),
            "audit_table": conn.execute("SELECT * FROM migration_log NOT INDEXED").fetchall(),
            "audit_index": conn.execute(
                "SELECT step, applied_at FROM migration_log INDEXED BY idx_migration_log_applied"
            ).fetchall(),
        }
    finally:
        conn.close()


def pytest_configure(config):
    with closing(sqlite3.connect(":memory:")) as conn:
        source_id = conn.execute("SELECT sqlite_source_id()").fetchone()[0]
    print("sqlite probe identity", sys.version, sqlite3.sqlite_version, source_id, flush=True)


@pytest.fixture(autouse=True)
def observe(request, monkeypatch):
    if not str(request.node.path).endswith("test_data_binding.py"):
        return
    original = request.module.execute

    def execute(root, sql):
        try:
            original(root, sql)
        except sqlite3.Error as exc:
            if sql.startswith("UPDATE migration_log SET applied_at"):
                path = root / "events.db"
                evidence = {
                    "node": request.node.nodeid,
                    "sql": sql,
                    "type": type(exc).__name__,
                    "code": exc.sqlite_errorcode,
                    "name": exc.sqlite_errorname,
                    "message": str(exc),
                    "size": path.stat().st_size,
                    "companion_names": sorted(p.name for p in path.parent.glob("events.db*")),
                }
                try:
                    evidence["snapshot"] = inspect(path)
                except sqlite3.Error as diagnostic:
                    evidence["inspect_error"] = str(diagnostic)
                out_name = f"failure-{path.parent.parent.name}.json"
                out = Path(os.environ["BINDING_PROBE_DIR"]) / out_name
                with out.open("x", encoding="utf-8") as stream:
                    json.dump(evidence, stream, indent=2)
            raise

    monkeypatch.setattr(request.module, "execute", execute)


def pytest_collection_modifyitems(items):
    suffix = "test_fresh_noop_compares_full_mechanism_audit_and_definitions[applied_at]"
    for position in range(len(items) - 1, -1, -1):
        node = items[position]
        if node.nodeid.endswith(suffix):
            items[position : position + 1] = [
                pytest.Function.from_parent(
                    node.parent,
                    name=f"{node.name}-{i}",
                    callobj=node.obj,
                    callspec=node.callspec,
                    fixtureinfo=node._fixtureinfo,
                    originalname=node.originalname,
                )
                for i in range(72)
            ]
