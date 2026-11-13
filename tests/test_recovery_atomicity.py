"""恢复本身二次崩溃，不能留下 unknown 结果却没有阻断（AB-02）。"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import closing

import pytest
from support import CHILD_TEXT

from tianshu import authority
from tianshu.events import EventLog
from tianshu.guard import blocked_objects, blocked_workspaces, unblock, unblock_object
from tianshu.loop import build_stack
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore


def _seed_intent(log: EventLog, *, mcp: bool = False) -> None:
    tool_id = "mcp__local__write" if mcp else "modify_text"
    seq = log.append("run-probe", "tool.intent", {
        "call_id": "uncertain", "tool_id": tool_id,
        "args": {} if mcp else {"path": "target.txt", "content": "EXPECTED"},
        "args_hash": "0" * 64, "request_id": "req",
        "request_digest": "0" * 64, "dispatch_id": "disp",
        "attempt_id": 1, "approval_id": None, "grant_id": "grant",
        "claim_epoch": 1,
    })
    if mcp:
        # 使用 Dispatcher 的真实 schema；只注入崩溃后必需的投影事实。
        build_stack(log)
        log.conn.execute(
            "INSERT INTO dispatches (dispatch_id,run_id,task_id,call_id,"
            "request_id,request_digest,tool_id,action,resources,target,rel_path,"
            "workspace_root,resource_key,approval_id,grant_id,claim_epoch,"
            "owner_instance_id,attempt_id,intent_seq,boundary_protocol,state,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("disp", "run-probe", "task", "uncertain", "req", "0" * 64,
             tool_id, "overwrite", "[]", "mcp://local/write", "", "", "mcp:local/write",
             None, "grant", 1, "instance", 1, seq, 1, "executing", 1, 1),
        )

_CHILD = """
import os, sys
from pathlib import Path
from tianshu import authority
from tianshu.crashpoint import failpoint
from tianshu.events import EventLog
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore

root = Path(sys.argv[1])
log = EventLog(root / 'events.db')
original = log.append_many
def append_then_crash(items):
    result = original(items)
    if any(item['type'] == 'tool.result' for item in items):
        failpoint('after_result')
    return result
if sys.argv[2] == 'after_result':
    log.append_many = append_then_crash
else:
    def crash_before_authority(*args, **kwargs):
        failpoint('after_result')
    authority.register_blocked = crash_before_authority
    authority.register_object_blocked = crash_before_authority
os.environ['TIANSHU_FAILPOINT'] = 'after_result'
recover_run(log, 'run-probe', root / 'ws', SnapshotStore(root / 'artifacts'),
            data_root=root)
raise AssertionError('crashpoint was not reached')
"""


@pytest.mark.parametrize("manifest", ["missing", "corrupt"])
@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize("stage", ["after_result", "before_authority"])
def test_unknown_result_and_block_survive_interrupted_recovery_together(
    tmp_path, manifest, mcp, stage,
):
    """真实 append_many 之后硬退；只补 result、稍后补 block 的实现必红。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "target.txt").write_bytes(b"PARTIAL UNKNOWN CONTENT")
    snapshots = SnapshotStore(tmp_path / "artifacts")
    if manifest == "corrupt":
        (snapshots.manifest_dir / "run-probe.json").write_bytes(b"corrupt")
    with closing(EventLog(tmp_path / "events.db")) as log:
        _seed_intent(log, mcp=mcp)
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), stage],
        env=dict(os.environ), capture_output=True, timeout=15, **CHILD_TEXT,
    )
    assert child.returncode == 77, (child.returncode, child.stdout, child.stderr)
    with closing(EventLog(tmp_path / "events.db")) as log:
        events = log.read_run("run-probe")
        results = [e for e in events if e.type == "tool.result"]
        blocks = [e for e in events if e.type == "recovery.blocked"]
        # 同事务可同时提交或同时回滚；不能只提交其中一半。
        assert bool(results) == bool(blocks)
        if mcp:
            objects = [e for e in events if e.type == "recovery.object_blocked"]
            assert bool(results) == bool(objects)
        if stage == "before_authority":
            assert results and blocks
            assert authority.active_paths() == []
            if mcp:
                assert log.conn.execute(
                    "SELECT state FROM dispatches WHERE dispatch_id='disp'"
                ).fetchone()[0] == "error"
        recover_run(log, "run-probe", ws, snapshots, data_root=tmp_path)
        assert blocked_workspaces(log) == {str(ws.resolve())}
        assert authority.active_paths() == [str(ws.resolve())]
        if mcp:
            assert blocked_objects(log) == {"mcp://local/write"}
            assert authority.active_object_keys() == ["mcp://local/write"]
        events = log.read_run("run-probe")
        assert len([e for e in events if e.type == "tool.result"]) == 1
        assert len([e for e in events if e.type == "recovery.blocked"]) == 1
        head = log.seq_head()
        recover_run(log, "run-probe", ws, snapshots, data_root=tmp_path)
        assert log.seq_head() == head


@pytest.mark.parametrize("mcp", [False, True])
def test_legacy_unknown_without_blocks_is_repaired_once_and_unblock_is_preserved(
    tmp_path, mcp,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    snapshots = SnapshotStore(tmp_path / "artifacts")
    with closing(EventLog(tmp_path / "events.db")) as log:
        _seed_intent(log, mcp=mcp)
        log.append("run-probe", "tool.result", {
            "call_id": "uncertain",
            "tool_id": "mcp__local__write" if mcp else "modify_text",
            "status": "unknown", "effect_state": "unknown",
            "output": {"recovered": True, "error": "interrupted old recovery"},
        })
        report = recover_run(log, "run-probe", ws, snapshots, data_root=tmp_path)
        assert report.unresolved == ["uncertain"]
        assert blocked_workspaces(log) == {str(ws.resolve())}
        if mcp:
            assert blocked_objects(log) == {"mcp://local/write"}
        results = [e for e in log.read_run("run-probe") if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0].payload["output"]["error"] == "interrupted old recovery"
        head = log.seq_head()
        recover_run(log, "run-probe", ws, snapshots, data_root=tmp_path)
        assert log.seq_head() == head
        unblock(log, ws, reason="checked by human")
        if mcp:
            unblock_object(log, "mcp://local/write", reason="checked by human")
        head = log.seq_head()
        recover_run(log, "run-probe", ws, snapshots, data_root=tmp_path)
        assert log.seq_head() == head
        assert blocked_workspaces(log) == set()
        assert blocked_objects(log) == set()
