import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tianshu.backup import create_backup
from tianshu.events import EventLog
from tianshu.loop import build_stack

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-readonly-edges-"))


def state(root):
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "tianshu", "data", *map(str, args), "--json"],
        text=True,
        capture_output=True,
        timeout=15,
    )


def update_hash(dest):
    p = dest / "backup.json"
    m = json.loads(p.read_text())
    raw = (dest / "events.db").read_bytes()
    entry = next(f for f in m["files"] if f["path"] == "events.db")
    entry.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    p.write_text(json.dumps(m))


root = base / "wal-root"
log = EventLog(root / "events.db")
log.close()
dest = base / "wal-backup"
create_backup(root, dest)
c = sqlite3.connect(dest / "events.db")
assert c.execute("pragma journal_mode=WAL").fetchone()[0] == "wal"
c.close()
update_hash(dest)
before = state(dest)
result = cli("verify", dest)
after = state(dest)
assert before == after, {
    "files_added": sorted(after.keys() - before.keys()),
    "files_changed": [k for k in before if before[k] != after.get(k)],
}
print(
    json.dumps(
        {
            "case": "wal-verify",
            "exit": result.returncode,
            "ok": json.loads(result.stdout)["ok"],
            "files_added": sorted(after.keys() - before.keys()),
            "prior_files_changed": [k for k in before if before[k] != after.get(k)],
        }
    )
)
root = base / "missing-root"
log = EventLog(root / "events.db")
stack = build_stack(log, data_root=root)
workspace = base / "workspace"
workspace.mkdir()
task = stack.tasks.create_task("independent state probe", [str(workspace)])
run = stack.tasks.start_run(task.task_id)
log.close()
dest = base / "missing-projection"
create_backup(root, dest)
shutil.rmtree(root)
c = sqlite3.connect(dest / "events.db")
c.execute("drop table runs")
c.commit()
c.close()
update_hash(dest)
result = cli("restore-plan", dest, "--data-root", root)
report = json.loads(result.stdout)
assert result.returncode == 2 and not report["ok"] and report["problems"], report
assert not root.exists()
print(
    json.dumps(
        {
            "case": "missing-runs-projection",
            "exit": result.returncode,
            "ok": report["ok"],
            "problems": report["problems"],
            "reconciliation": report["reconciliation"],
            "target_exists": root.exists(),
        },
        ensure_ascii=False,
    )
)
