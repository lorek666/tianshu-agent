"""Exercise saved source-root aliases without consulting the original source during verification."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tianshu.backup import BackupError, create_backup, read_saved_event_payload, verify_backup
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.snapshot import SnapshotStore

base = Path(tempfile.mkdtemp(prefix="tianshu-g3-source-alias-fixed-")).resolve()
actual = base / "actual"
actual.mkdir()
alias = base / "alias"
alias.symlink_to(actual, target_is_directory=True)
root = alias / "data"
canonical = root.resolve()


def fixture(data_root):
    workspace = base / "workspace"
    workspace.mkdir(exist_ok=True)
    before = b"independent before-image"
    (workspace / "target.txt").write_bytes(before)
    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    try:
        stack = build_stack(log, data_root=data_root)
        task = stack.tasks.create_task("independent alias probe", [str(workspace)])
        run = stack.tasks.start_run(task.task_id)
        body = "a" * 330001
        log.append(
            run.run_id,
            "tool.intent",
            {
                "call_id": "alias-pending",
                "tool_id": "modify_text",
                "args": {"path": "target.txt", "content": body},
                "args_hash": "a" * 64,
            },
            actor_kind="core",
            legacy_write=True,
        )
        snapshots = SnapshotStore(data_root / "artifacts")
        snapshots.init_run(run.run_id)
        snapshots.record_preimage(
            run.run_id,
            "alias-pending",
            "target.txt",
            before,
            hashlib.sha256(body.encode()).hexdigest(),
            root=workspace,
        )
        return log.conn.execute(
            "select payload_ref from events where payload_ref is not null"
        ).fetchone()[0]
    finally:
        log.close()


def read_manifest(dest):
    return json.loads((dest / "backup.json").read_bytes())


def write_manifest(dest, manifest):
    (dest / "backup.json").write_text(json.dumps(manifest), encoding="utf8")


reference = fixture(root)
results = []
backups = []
for name, supplied_root in [("raw-input", root), ("canonical-input", canonical)]:
    dest = base / name
    manifest = create_backup(supplied_root, dest)
    assert str(root) in manifest.source_aliases
    assert verify_backup(dest).ok
    backups.append(dest)
    results.append({"case": name, "aliases": manifest.source_aliases, "ok": True})
cli_dest = base / "cli-input"
made = subprocess.run(
    [
        sys.executable,
        "-m",
        "tianshu",
        "data",
        "backup",
        "--data-root",
        str(canonical),
        "--dest",
        str(cli_dest),
        "--json",
    ],
    text=True,
    capture_output=True,
    timeout=25,
)
assert made.returncode == 0, made.stderr
assert str(root) in json.loads(made.stdout)["manifest"]["source_aliases"]
backups.append(cli_dest)
shutil.rmtree(canonical)
alias.unlink()
trap = base / "unrelated-root"
trap.mkdir()
alias.symlink_to(trap, target_is_directory=True)
original_resolve = Path.resolve


def forbid_live_source(path, *args, **kwargs):
    assert not any(
        str(path) == prefix or str(path).startswith(prefix + "/")
        for prefix in [str(root), str(canonical)]
    ), str(path)
    return original_resolve(path, *args, **kwargs)


for dest in backups:
    with patch.object(Path, "resolve", forbid_live_source):
        checked = verify_backup(dest)
        assert checked.ok, checked.problems
        conn = sqlite3.connect((dest / "events.db").as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select payload as payload_inline,payload_ref,payload_hash from events "
            "where payload_ref is not null"
        ).fetchone()
        conn.close()
        manifest = checked.manifest
        payload = read_saved_event_payload(
            dest,
            row,
            source_root=Path(manifest.source_path),
            source_aliases=manifest.source_aliases,
        )
        assert payload["call_id"] == "alias-pending"
        assert len(payload["args"]["content"]) == 330001
    plan = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianshu",
            "data",
            "restore-plan",
            str(dest),
            "--data-root",
            str(canonical),
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=25,
    )
    assert plan.returncode == 3, (plan.stdout, plan.stderr)
    assert json.loads(plan.stdout)["reconciliation"]["pending_intents"]
    assert not canonical.exists()
results.append({"case": "source deleted and alias replaced; no live resolve", "ok": True})
for fault in ["remove-alias", "malformed-alias", "material-symlink"]:
    dest = base / fault
    shutil.copytree(backups[0], dest)
    manifest = read_manifest(dest)
    if fault == "remove-alias":
        manifest.pop("source_aliases")
        manifest["references"] = {}
    elif fault == "malformed-alias":
        manifest["source_aliases"] = ["../outside"]
    else:
        payload = next(item for item in manifest["files"] if item["path"].startswith("blobs/"))
        path = dest / payload["path"]
        outside = base / "outside-identical-body"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    write_manifest(dest, manifest)
    checked = verify_backup(dest)
    assert not checked.ok, fault
    results.append({"case": fault, "accepted": checked.ok})
legacy_root = base / "legacy-canonical"
fixture(legacy_root)
legacy = base / "legacy-backup"
create_backup(legacy_root, legacy)
manifest = read_manifest(legacy)
manifest.pop("source_aliases")
manifest["references"] = {}
write_manifest(legacy, manifest)
assert verify_backup(legacy).ok
results.append({"case": "legacy canonical v1 without alias field", "ok": True})
external_root = base / "external-reference-root"
external_blobs = base / "outside" / "blobs"
log = EventLog(external_root / "events.db", blob_dir=external_blobs)
log.append(
    "outside",
    "tool.result",
    {
        "call_id": "outside",
        "tool_id": "read_text",
        "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": "b" * 330001},
    },
    actor_kind="core",
)
log.close()
try:
    create_backup(external_root, base / "external-reference-backup")
except BackupError:
    pass
else:
    raise AssertionError("external same-name blobs accepted as a root alias")
assert not (base / "external-reference-backup").exists()
results.append({"case": "external same-name blobs rejected", "ok": True})
if Path("/tmp").exists() and Path("/tmp").resolve() != Path("/tmp"):
    mac_root = Path(tempfile.mkdtemp(prefix="tianshu-g3-standard-tmp-", dir="/tmp")) / "data"
    fixture(mac_root)
    mac_backup = base / "mac-standard-tmp"
    manifest = create_backup(mac_root, mac_backup)
    assert str(mac_root) in manifest.source_aliases
    shutil.rmtree(mac_root)
    assert verify_backup(mac_backup).ok
    results.append({"case": "real macOS /tmp to /private/tmp alias", "ok": True})
print(
    json.dumps(
        {"ok": True, "cases": results, "original_payload_ref": reference},
        ensure_ascii=False,
        indent=2,
    )
)
