"""Regression for independently found collector defect; no repository-test imports."""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from tianshu import memory_control
from tianshu.backup import ReferenceUnknown, cleanup_plan, collect_references
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.resources import MemoryScope

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-cleanup-fixed-"))
raw = b"content still registered and task-visible"
digest = hashlib.sha256(raw).hexdigest()
content = "artifacts/blobs/" + digest
log = EventLog(base / "events.db")
try:
    stack = build_stack(log, data_root=base)
    path = base / content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    stack.contents.record(digest, raw, task_id="independent-task")
    remembered = memory_control.remember(
        stack, MemoryScope.user(), kind="preference", content="独立探针保护仍有引用的记忆正文。"
    )
    assert remembered.ok
    memory = stack.memory.get(remembered.output["memory_id"], allowed=[MemoryScope.user()])
    assert memory
    memrel = "memory_blobs/" + memory.content_ref
finally:
    log.close()
index = collect_references(base)
plan = cleanup_plan(base)
assert content not in index.orphan_blobs and content in index.protected_blobs
assert memrel in index.protected_blobs
assert content not in {c["path"] for c in plan["candidates"]}
results = [
    {"case": "live content protected", "protected": index.protected_blobs[content]},
    {"case": "live memory protected", "protected": index.protected_blobs[memrel]},
]
for table, column in [("content_objects", "digest"), ("memories", "content_ref")]:
    conn = sqlite3.connect(base / "events.db")
    before = conn.execute(f"select {column} from {table} limit 1").fetchone()[0]
    conn.execute(f"update {table} set {column}=?", ("invalid/../../digest",))
    conn.commit()
    conn.close()
    try:
        collect_references(base)
    except ReferenceUnknown as e:
        results.append(
            {"case": "bad " + table + " reference", "error": type(e).__name__, "detail": str(e)}
        )
    else:
        raise AssertionError(table + " invalid reference accepted")
    conn = sqlite3.connect(base / "events.db")
    conn.execute(f"update {table} set {column}=?", (before,))
    conn.commit()
    conn.close()
print(json.dumps({"ok": True, "cases": results}, ensure_ascii=False, indent=2))
