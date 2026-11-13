import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from tianshu.backup import create_backup, verify_backup
from tianshu.events import EventLog

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-wal-sidecars-"))
root = base / "root"
log = EventLog(root / "events.db")
log.close()
dest = base / "backup"
create_backup(root, dest)
conn = sqlite3.connect(dest / "events.db")
conn.execute("pragma journal_mode=WAL")
conn.execute(
    "insert into events(seq,run_id,type,ts,actor_kind,actor_id,payload,payload_hash) "
    "values(1,'wal-only','run.start',1,'core','','{}',?)",
    (hashlib.sha256(b"{}").hexdigest(),),
)
conn.commit()
try:
    assert conn.execute("select count(*) from events").fetchone()[0] == 1
    frozen = sqlite3.connect((dest / "events.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    assert frozen.execute("select count(*) from events").fetchone()[0] == 0
    frozen.close()
    m = json.loads((dest / "backup.json").read_bytes())
    names = {f["path"] for f in m["files"]}
    for path in dest.iterdir():
        if path.name == "backup.json":
            continue
        raw = path.read_bytes()
        entry = {
            "path": path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "category": "event_log",
        }
        if path.name in names:
            m["files"] = [entry if f["path"] == path.name else f for f in m["files"]]
        else:
            m["files"].append(entry)
    (dest / "backup.json").write_text(json.dumps(m))
    before = {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in dest.iterdir()
        if p.is_file()
    }
    check = verify_backup(dest)
    after = {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in dest.iterdir()
        if p.is_file()
    }
    assert not check.ok, check.to_dict()
    assert before == after, "verification wrote saved database/WAL/SHM"
    print(
        json.dumps(
            {
                "ok": True,
                "accepted_backup_with_declared_wal": check.ok,
                "unchanged": before == after,
                "problems": check.problems,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
finally:
    conn.close()
