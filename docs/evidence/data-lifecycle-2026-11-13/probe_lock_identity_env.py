import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tianshu.maintenance import acquire_database_access

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-lock-env-"))
db = base / "data" / "events.db"
clean = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
}
for name in ["alternate-home", "alternate-temp", "alternate-data-root"]:
    (base / name).mkdir()
source = """
import sys
from pathlib import Path
from tianshu.maintenance import database_lock_path
print(database_lock_path(Path(sys.argv[1])))
"""
paths = []
environments = []
for extra in [
    {},
    {"HOME": str(base / "alternate-home"), "TMPDIR": str(base / "alternate-temp")},
    {"TIANSHU_DATA_ROOT": str(base / "alternate-data-root")},
]:
    env = clean | extra
    environments.append(env)
    p = subprocess.run(
        [sys.executable, "-c", source, str(db)], env=env, text=True, capture_output=True, timeout=15
    )
    assert p.returncode == 0, p.stderr
    paths.append(p.stdout.strip())
assert len(set(paths)) == 1, paths
writer = """
import sys
from pathlib import Path
from tianshu.events import EventLog
from tianshu.maintenance import MaintenanceBusy
try:
    log = EventLog(Path(sys.argv[1]))
except MaintenanceBusy:
    print("BUSY", flush=True)
    sys.exit(2)
else:
    log.close()
    raise AssertionError("writer bypassed exclusive access")
"""
with acquire_database_access(db, exclusive=True):
    for env in environments:
        p = subprocess.run(
            [sys.executable, "-c", writer, str(db)],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert p.returncode == 2 and p.stdout.strip() == "BUSY", (p.stdout, p.stderr)
    assert not db.parent.exists()
print(
    json.dumps(
        {
            "ok": True,
            "same_database_same_lock_under_HOME_TMPDIR_data_root_variations": True,
            "lock_path": paths[0],
            "all_environment_variants_blocked_before_database_creation": True,
        }
    )
)
