import json
import selectors
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tianshu.backup import create_backup, verify_backup
from tianshu.events import EventLog

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-live-wal-"))
db = base / "root" / "events.db"
log = EventLog(db)
log.close()
ACTOR = """
import sys
from pathlib import Path
from tianshu.events import EventLog
log=EventLog(Path(sys.argv[1]))
log.conn.execute('PRAGMA wal_autocheckpoint=0')
log.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
log.append(
 'independent-wal','run.start',{'task':'committed only in WAL'},
 actor_kind='core',legacy_write=True,
)
print('READY',flush=True)
assert sys.stdin.readline().strip()=='close'
log.close()
"""
p = subprocess.Popen(
    [sys.executable, "-u", "-c", ACTOR, str(db)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
try:
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    assert sel.select(15)
    assert p.stdout.readline().strip() == "READY"
    sel.close()
    c = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    live = c.execute("select count(*) from events").fetchone()[0]
    c.close()
    c = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
    base_count = c.execute("select count(*) from events").fetchone()[0]
    c.close()
    assert live == 1 and base_count == 0, (live, base_count)
    dest = base / "backup"
    manifest = create_backup(db.parent, dest)
    check = verify_backup(dest)
    assert check.ok and manifest.event_count == 1
    c = sqlite3.connect((dest / "events.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    row = c.execute("select run_id,payload from events").fetchone()
    c.close()
    assert row[0] == "independent-wal" and json.loads(row[1])["task"] == "committed only in WAL"
    print(
        json.dumps(
            {
                "ok": True,
                "source_base_file_events": base_count,
                "source_live_with_wal_events": live,
                "online_backup_events": manifest.event_count,
                "backup_verified": check.ok,
            }
        )
    )
finally:
    if p.poll() is None:
        p.stdin.write("close\n")
        p.stdin.flush()
    assert p.wait(timeout=15) == 0, p.stderr.read()
