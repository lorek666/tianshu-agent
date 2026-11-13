import json
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from tianshu.events import EventLog
from tianshu.maintenance import MaintenanceError, database_lock_path

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-permission-"))
parent = base / "restricted-parent"
writable = parent / "writable-database-directory"
writable.mkdir(parents=True)
db = writable / "custom.db"
source = subprocess.run(
    ["git", "show", "ccff93e79cfeb93e47e5a35012fd92fae366e941:src/tianshu/events.py"],
    text=True,
    capture_output=True,
    check=True,
).stdout
module = types.ModuleType("tianshu.independent_baseline_events")
module.__package__ = "tianshu"
sys.modules[module.__name__] = module
exec(compile(source, "BASELINE/events.py", "exec"), module.__dict__)
old = module.EventLog(db)
old.close()
parent.chmod(0o555)
rows = {}
try:
    old = module.EventLog(db)
    old.append(
        "permission-probe",
        "run.start",
        {"task": "baseline usable"},
        actor_kind="core",
        legacy_write=True,
    )
    old.close()
    rows["baseline_EventLog"] = "open+append+close ok"
    conn = sqlite3.connect(db)
    conn.execute("create table independent_writable (x)")
    conn.commit()
    conn.close()
    rows["raw_sqlite"] = "create+commit+close ok"
    try:
        log = EventLog(db)
        log.append(
            "candidate-permission-probe",
            "run.start",
            {"task": "candidate usable"},
            actor_kind="core",
            legacy_write=True,
        )
        log.close()
    except MaintenanceError as e:
        rows["candidate_EventLog"] = type(e).__name__
        rows["error"] = str(e)
    else:
        rows["candidate_EventLog"] = "open+append+close ok"
    rows["lock_outside_writable_directory"] = not database_lock_path(db).is_relative_to(writable)
    assert rows["candidate_EventLog"] == "open+append+close ok", rows
    print(json.dumps(rows, ensure_ascii=False, indent=2))
finally:
    parent.chmod(0o755)
