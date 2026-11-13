import json
import os
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from tianshu.errors import EventLogCorrupt
from tianshu.events import EventLog
from tianshu.maintenance import database_lock_path

root = Path(tempfile.mkdtemp(prefix="tianshu-g3-locks-"))
env = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
}
env["TIANSHU_FAILPOINT"] = "independent-maintenance"
ACTOR = """
import json,sys
from pathlib import Path
from tianshu.events import EventLog
from tianshu.maintenance import acquire_database_access,MaintenanceBusy
from tianshu.crashpoint import failpoint
path=Path(sys.argv[1]);mode=sys.argv[2]
try:
 if mode == 'eventlog':
  obj = EventLog(path)
 else:
  obj = acquire_database_access(path,exclusive=mode=='exclusive')
except MaintenanceBusy:
 print('BUSY',flush=True);sys.exit(2)
print('READY',flush=True)
if len(sys.argv)>3 and sys.argv[3]=='once':obj.close();sys.exit(0)
for line in sys.stdin:
 if line.strip()=='crash':failpoint('independent-maintenance')
 if line.strip()=='close':obj.close();print('CLOSED',flush=True);break
"""
actor = [sys.executable, "-u", "-c", ACTOR]


def line(p):
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    assert sel.select(10), "barrier timeout"
    result = p.stdout.readline().strip()
    sel.close()
    return result


def holder(db, mode):
    p = subprocess.Popen(
        [*actor, str(db), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert line(p) == "READY"
    return p


def attempt(db, mode):
    p = subprocess.run(
        [*actor, str(db), mode, "once"], env=env, text=True, capture_output=True, timeout=15
    )
    return p.returncode, p.stdout.strip()


def close(p):
    p.stdin.write("close\n")
    p.stdin.flush()
    assert line(p) == "CLOSED"
    assert p.wait(10) == 0


def crash(p):
    p.stdin.write("crash\n")
    p.stdin.flush()
    assert p.wait(10) == 77


rows = []
db = root / "data" / "events.db"
a = holder(db, "eventlog")
b = holder(db, "eventlog")
assert attempt(db, "shared") == (0, "READY")
assert attempt(db, "exclusive") == (2, "BUSY")
close(a)
assert attempt(db, "exclusive") == (2, "BUSY")
close(b)
assert attempt(db, "exclusive") == (0, "READY")
rows.append("two real EventLog children coexist; exclusive BUSY until both close")
for mode in ["shared", "exclusive", "eventlog"]:
    p = holder(db, mode)
    assert attempt(db, "exclusive") == (2, "BUSY")
    crash(p)
    assert attempt(db, "exclusive") == (0, "READY")
    rows.append(f"{mode} child hard-exit=77 releases access")
absent = root / "not-created" / "events.db"
p = holder(absent, "exclusive")
assert attempt(absent, "eventlog") == (2, "BUSY")
assert not absent.parent.exists()
close(p)
rows.append("exclusive rejects EventLog before root/database creation")
corrupt = root / "broken" / "events.db"
corrupt.parent.mkdir()
corrupt.write_bytes(b"not a database\0independent")
try:
    EventLog(corrupt)
except EventLogCorrupt:
    pass
else:
    raise AssertionError("corruption accepted")
assert corrupt.read_bytes() == b"not a database\0independent"
assert attempt(corrupt, "exclusive") == (0, "READY")
rows.append("corrupt constructor fails closed, preserves bytes, releases access")
log = EventLog(db)
caught = []


def wrong():
    try:
        log.close()
    except sqlite3.ProgrammingError as e:
        caught.append(type(e).__name__)


t = threading.Thread(target=wrong)
t.start()
t.join(10)
assert caught == ["ProgrammingError"]
log.conn.execute("CREATE TABLE independent_probe (x)")
assert attempt(db, "exclusive") == (2, "BUSY")
log.close()
assert attempt(db, "exclusive") == (0, "READY")
rows.append("wrong-thread close retains lock while actual connection remains writable")
lock = database_lock_path(db)
p = holder(db, "exclusive")
db.parent.rename(root / "moved")
db.parent.mkdir()
assert database_lock_path(db) == lock
assert attempt(db, "eventlog") == (2, "BUSY")
close(p)
rows.append("replacing root at original path cannot replace lock identity")
print(json.dumps({"ok": True, "cases": rows}, ensure_ascii=False, indent=2))
