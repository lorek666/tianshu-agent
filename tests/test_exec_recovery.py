"""命令崩溃不能拿空文件快照证明没有执行（AB-01）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces
from tianshu.recovery import assess_run, recover_run
from tianshu.snapshot import SnapshotStore

_CHILD = """
import json, os, sys
from pathlib import Path
from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.execpolicy import ExecPolicy
from tianshu.loop import build_stack
from tianshu.permissions import HumanIdentity
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING

root = Path(sys.argv[1])
ws = root / 'ws'
ws.mkdir()
data = root / 'data'
data.mkdir()
log = EventLog(data / 'events.db')
snaps = SnapshotStore(data / 'artifacts')
stack = build_stack(log, snapshots=snaps, data_root=data)
task = stack.tasks.create_task(
    'command crash', [str(ws)],
    exec_policy=ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(ws,),
        env_allowlist=('PATH',), timeout_ms=10000, max_output_bytes=65536,
    ),
)
run = stack.tasks.start_run(task.task_id)
stack.leases.acquire([ws], run.run_id)
job = stack.claims.enqueue(run.run_id)
claim = stack.claims.claim(job.job_id)
stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
snaps.init_run(run.run_id)
args = {'argv': [sys.executable, '-c',
    "from pathlib import Path; Path('effect.txt').write_bytes(b'EXECUTED')"]}
request = build_request(
    task=task, run=run, claim=claim, call_id='exec-crash',
    tool_id='run_command', target=ws, args=args,
)
decision = stack.dispatcher.prepare(request, claim, args=args)
assert decision.is_waiting, decision
stack.approvals.decide(
    decision.approval_id, decision='approved', actor=HumanIdentity.local_cli(),
)
(root / 'run.json').write_text(json.dumps({'run_id': run.run_id}))
os.environ['TIANSHU_FAILPOINT'] = sys.argv[2]
stack.dispatcher.dispatch(request.request_id, claim)
raise AssertionError('crashpoint was not reached')
"""


def _crash_command(tmp_path: Path, point: str) -> tuple[str, EventLog, SnapshotStore]:
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), point],
        env={k: v for k, v in os.environ.items() if k != "TIANSHU_FAILPOINT"},
        capture_output=True, timeout=30, **CHILD_TEXT,
    )
    assert child.returncode == 77, (child.returncode, child.stdout, child.stderr)
    run_id = json.loads((tmp_path / "run.json").read_text())["run_id"]
    data = tmp_path / "data"
    return run_id, EventLog(data / "events.db"), SnapshotStore(data / "artifacts")


@pytest.mark.parametrize("point", ["after_intent", "after_write"])
@pytest.mark.parametrize("manifest", ["empty", "missing", "corrupt"])
def test_pending_command_uses_execution_boundary_not_file_manifest(
    tmp_path, point, manifest,
):
    """删除执行边界分支就会把真实已执行的命令误判成 none_applied。"""
    run_id, log, snapshots = _crash_command(tmp_path, point)
    try:
        path = snapshots.manifest_dir / f"{run_id}.json"
        if manifest == "missing":
            path.unlink()
        elif manifest == "corrupt":
            path.write_bytes(b"not a manifest")
        ws = tmp_path / "ws"
        effect = ws / "effect.txt"
        executed = point == "after_write"
        assert effect.exists() is executed
        if executed:
            assert effect.read_bytes() == b"EXECUTED"

        assessment = assess_run(tmp_path / "data", run_id, log=log)
        assert assessment.blocks_takeover is executed
        assert list(assessment.none_applied) == ([] if executed else ["exec-crash"])
        report = recover_run(
            log, run_id, ws, snapshots, data_root=tmp_path / "data",
        )
        assert report.unresolved == (["exec-crash"] if executed else [])
        assert report.none_applied == ([] if executed else ["exec-crash"])
        results = [e for e in log.read_run(run_id) if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0].payload["effect_state"] == (
            "unknown" if executed else "none_applied"
        )
        assert blocked_workspaces(log) == ({str(ws.resolve())} if executed else set())
        assert not [e for e in log.read_run(run_id) if e.type == "receipt"]
        head = log.seq_head()
        recover_run(log, run_id, ws, snapshots, data_root=tmp_path / "data")
        assert log.seq_head() == head
        assert effect.exists() is executed
    finally:
        log.close()


@pytest.mark.parametrize("point", ["after_result", "after_receipt"])
def test_settled_command_is_not_reclassified_or_reexecuted(tmp_path, point):
    run_id, log, snapshots = _crash_command(tmp_path, point)
    try:
        head = log.seq_head()
        report = recover_run(
            log, run_id, tmp_path / "ws", snapshots, data_root=tmp_path / "data",
        )
        assert report.ok
        assert log.seq_head() == head
        events = log.read_run(run_id)
        assert len([e for e in events if e.type == "tool.result"]) == 1
        assert len([e for e in events if e.type == "receipt"]) == 1
        assert (tmp_path / "ws" / "effect.txt").read_bytes() == b"EXECUTED"
    finally:
        log.close()


@pytest.mark.parametrize("evidence", ["missing_row", "legacy", "unrecognized"])
def test_command_without_proven_boundary_fails_closed(tmp_path, evidence):
    run_id, log, snapshots = _crash_command(tmp_path, "after_intent")
    try:
        if evidence == "missing_row":
            log.conn.execute("DELETE FROM dispatches")
        elif evidence == "legacy":
            log.conn.execute("UPDATE dispatches SET boundary_protocol=0")
        else:
            log.conn.execute("UPDATE dispatches SET state='unrecognized'")
        assert assess_run(tmp_path / "data", run_id, log=log).blocks_takeover
        report = recover_run(log, run_id, tmp_path / "ws", snapshots)
        assert report.unresolved == ["exec-crash"]
        assert report.none_applied == []
        assert blocked_workspaces(log) == {str((tmp_path / "ws").resolve())}
    finally:
        log.close()


def test_unknown_command_blocks_recorded_workspace_not_recovery_argument(tmp_path):
    run_id, log, snapshots = _crash_command(tmp_path, "after_write")
    other = tmp_path / "other"
    other.mkdir()
    try:
        report = recover_run(log, run_id, other, snapshots)
        assert report.unresolved == ["exec-crash"]
        assert blocked_workspaces(log) == {str((tmp_path / "ws").resolve())}
    finally:
        log.close()


def test_file_manifest_cannot_redirect_unknown_command_block(tmp_path):
    run_id, log, snapshots = _crash_command(tmp_path, "after_write")
    other = tmp_path / "other"
    other.mkdir()
    try:
        snapshots.record_preimage(
            run_id, "exec-crash", "effect.txt", before=None,
            expected_after="0" * 64, root=other,
        )
        recover_run(log, run_id, other, snapshots)
        assert blocked_workspaces(log) == {str((tmp_path / "ws").resolve())}
    finally:
        log.close()
