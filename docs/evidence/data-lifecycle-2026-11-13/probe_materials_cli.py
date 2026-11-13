"""Independent public-API fixture and actual CLI checks; does not import repository tests."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tianshu import memory_control
from tianshu.backup import verify_backup
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.resources import MemoryScope

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-materials-cli-"))
rows = []
env = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
}


def cli(*args, failpoint=None):
    child_env = env.copy()
    if failpoint:
        child_env["TIANSHU_FAILPOINT"] = failpoint
    return subprocess.run(
        [sys.executable, "-m", "tianshu", "data", *map(str, args), "--json"],
        env=child_env,
        text=True,
        capture_output=True,
        timeout=25,
    )


def state(root):
    return {
        str(p.relative_to(root)): (
            p.is_dir(),
            None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in root.rglob("*")
    }


def refresh_manifest_db(dest):
    path = dest / "backup.json"
    manifest = json.loads(path.read_bytes())
    raw = (dest / "events.db").read_bytes()
    entry = next(f for f in manifest["files"] if f["path"] == "events.db")
    entry.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    path.write_text(json.dumps(manifest), encoding="utf8")


def fixture(root):
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        workspace = root.parent / "workspace"
        workspace.mkdir(exist_ok=True)
        task = stack.tasks.create_task("independent lifecycle probe", [str(workspace)])
        run = stack.tasks.start_run(task.task_id)
        log.append(
            run.run_id,
            "tool.intent",
            {
                "call_id": "independent-pending",
                "tool_id": "modify_text",
                "args": {"path": "target.txt", "content": "p" * 330001},
                "args_hash": "a" * 64,
            },
            actor_kind="core",
            legacy_write=True,
        )
        payload = log.conn.execute(
            "select payload_ref,payload_hash from events where payload_ref is not null"
        ).fetchone()
        remembered = memory_control.remember(
            stack, MemoryScope.user(), kind="preference", content="独立探针：报告必须保留证据来源。"
        )
        assert remembered.ok, remembered
        memory = stack.memory.get(remembered.output["memory_id"], allowed=[MemoryScope.user()])
        assert memory
        raw = "独立保留的content正文。".encode()
        digest = hashlib.sha256(raw).hexdigest()
        blob = stack.contents.blob_dir / digest
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(raw)
        stack.contents.record(digest, raw, task_id=task.task_id)
        return {
            "payload": "blobs/" + payload["payload_hash"],
            "memory": "memory_blobs/" + memory.content_ref,
            "content": "artifacts/blobs/" + digest,
        }, raw
    finally:
        log.close()


root = base / "root"
refs, raw = fixture(root)
dest = base / "backup"
made = cli("backup", "--data-root", root, "--dest", dest)
assert made.returncode == 0, made.stderr
report = json.loads(made.stdout)
assert report["offline"] and report["verification"]["ok"]
assert report["manifest"]["format_version"] == 1
assert (dest / refs["memory"]).read_text() == "独立探针：报告必须保留证据来源。"
assert (dest / refs["content"]).read_bytes() == raw
rows.append({"case": "full CLI backup", "exit": made.returncode, "materials": refs})
shutil.rmtree(root)
before = state(dest)
checked = cli("verify", dest)
assert checked.returncode == 0, checked.stderr
assert state(dest) == before
planned = cli("restore-plan", dest, "--data-root", root)
assert planned.returncode == 3, planned.stderr
plan = json.loads(planned.stdout)
assert plan["ok"] and not plan["apply_supported"]
assert plan["memory_reactivation"] == "requires_quarantine_and_review"
assert any(p["call_id"] == "independent-pending" for p in plan["reconciliation"]["pending_intents"])
assert len(plan["reconciliation"]["non_terminal_runs"]) >= 1
assert state(dest) == before and not root.exists()
rows.append(
    {
        "case": "source removed + readonly verify/plan",
        "verify_exit": checked.returncode,
        "plan_exit": planned.returncode,
        "hash_and_mtime_unchanged": True,
        "target_exists": root.exists(),
        "pending_call": plan["reconciliation"]["pending_intents"],
    }
)
for kind, rel in refs.items():
    bad = base / ("hidden-" + kind)
    shutil.copytree(dest, bad)
    (bad / rel).unlink()
    m = json.loads((bad / "backup.json").read_bytes())
    m["references"] = {}
    m["files"] = [f for f in m["files"] if f["path"] != rel]
    (bad / "backup.json").write_text(json.dumps(m))
    check = verify_backup(bad)
    assert not check.ok and any(rel in p for p in check.problems), (kind, check.problems)
    rows.append(
        {"case": "manifest hides missing " + kind, "accepted": check.ok, "problems": check.problems}
    )
for fault in ["bad-run-state", "future-tool-version", "missing-event-versions"]:
    bad = base / fault
    shutil.copytree(dest, bad)
    c = sqlite3.connect(bad / "events.db")
    if fault == "bad-run-state":
        c.execute("update runs set state='unrecognized-new-state'")
    elif fault == "future-tool-version":
        c.execute("update event_versions set version=999 where type='tool.intent'")
    else:
        c.execute("drop table event_versions")
    c.commit()
    c.close()
    refresh_manifest_db(bad)
    before = state(bad)
    result = cli("restore-plan", bad, "--data-root", root)
    p = json.loads(result.stdout)
    assert result.returncode == 2 and not p["ok"] and p["problems"]
    assert state(bad) == before and not root.exists()
    rows.append({"case": fault, "exit": result.returncode, "problems": p["problems"]})
crashroot = base / "crash-root"
fixture(crashroot)
for failpoint in ["backup-after-db", "backup-before-manifest", "backup-after-manifest"]:
    destination = base / failpoint
    crashed = cli("backup", "--data-root", crashroot, "--dest", destination, failpoint=failpoint)
    assert crashed.returncode == 77, crashed.stderr
    assert not destination.exists()
    partials = list(base.glob("." + failpoint + ".partial-*"))
    assert len(partials) == 1
    check = cli("verify", partials[0])
    assert check.returncode == 1 and not json.loads(check.stdout)["ok"]
    retry = cli("backup", "--data-root", crashroot, "--dest", destination)
    assert retry.returncode == 0, retry.stderr
    rows.append({"case": failpoint, "crash_exit": 77, "partial_verify_exit": 1, "retry_exit": 0})
print(json.dumps({"ok": True, "cases": rows}, ensure_ascii=False, indent=2))
