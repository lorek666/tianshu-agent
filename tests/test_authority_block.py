"""#61.1 authority 数据根逃逸的回归证据。

被证明的坏行为：阻断状态原来只存在**指定数据根**的事件日志里。换一个
`--db`（或换 cwd / `--data-root`）就看不见同一个工作区上的阻断，于是一台
机器上出现两个事实："A 说这个目录被挡住了"、"B 说可以写"。

修法：固定的**用户级权威根**（`authority.py`，`TIANSHU_AUTHORITY_ROOT`
否则 `~/.tianshu/authority`，与 `--db` 无关），guard 双读「本日志回放 ∪
权威根」，任一侧读不出来都按已阻断处理。

这一组用例用**真子进程**跑 B 侧写入：子进程有自己的 EventLog、自己的
Task/Run/Claim/Lease，唯一共享的是 workspace 目录与（通过环境变量继承的）
用户级权威根。如果只在同进程里断言，就等于用"同一个对象的两个引用"冒充
"两个数据根"，那正是这次要抓的洞。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import authority
from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import (
    blocked_workspaces,
    ensure_not_blocked,
    mirror_blocks_to_authority,
    unblock,
)
from tianshu.recovery import RecoveryReport, recover_run
from tianshu.snapshot import SnapshotStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
BEFORE = "写前内容\n"
AFTER = "写入内容\n"
TORN = "写到一半的内容"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 夹具


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_bytes(BEFORE.encode("utf-8"))
    return ws


def _seed_crashed_write(ws: Path, data_root: Path, run_id: str = "run_old") -> EventLog:
    """在当前数据根里造一条"写到一半、无法核实"的 run（不经过 B 侧）。

    这是 `recover_run` 会判 unknown 的标准形态：有写前证据 + 磁盘既不是写前
    也不是写后（第三方改动 / 写了一半），与 DEBUG-M0M1 的 S1 反例同源。
    不调用 recovery 的阻断：本函数只负责把日志造成那样，阻断由用例显式触发。
    """
    data_root.mkdir(parents=True, exist_ok=True)
    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    shots = SnapshotStore(data_root / "artifacts")
    log.append(
        run_id,
        "run.start",
        {"task": "改文件", "workspace": str(ws.resolve()), "out": "x",
         "limits": {}, "tools": []},
        actor_kind="core",
        legacy_write=True,
    )
    log.append(
        run_id,
        "tool.intent",
        {"call_id": "c1", "tool_id": "modify_text",
         "args": {"path": "note.md", "content": AFTER}, "args_hash": _sha("args")},
        actor_kind="core",
        legacy_write=True,
    )
    shots.init_run(run_id)
    shots.record_preimage(
        run_id, "c1", "note.md", BEFORE.encode("utf-8"), _sha(AFTER), root=ws
    )
    (ws / "note.md").write_bytes(TORN.encode("utf-8"))
    return log


def _block_in_data_root(ws: Path, data_root: Path) -> tuple[EventLog, RecoveryReport]:
    """A 数据根里真的跑一次恢复，把 unknown 落成阻断（日志 + 权威根）。"""
    log = _seed_crashed_write(ws, data_root)
    shots = SnapshotStore(data_root / "artifacts")
    report = recover_run(log, "run_old", ws, shots, data_root=data_root)
    assert report.unresolved == ["c1"], "前置：这条 run 必须是无法核实的"
    assert str(ws.resolve()) in report.blocked_workspaces
    return log, report


# ---------------------------------------------------------------- B 侧写入（真子进程）

#: 子进程：在**另一个数据根**上走完整派发链路（自己的 db / Task / Run / Claim /
#: Lease），尝试 modify_text。判断标准只有"派发判定"这一条：被阻断时
#: `prepare` 必须返回 rejected + `dispatch.workspace_blocked`，不由 repo 外的
#: 调用方替它兜底。
_B_CHILD = """
import sys
sys.path.insert(0, {src!r})
from pathlib import Path

from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.tasks import RUN_RUNNING

ws = Path({ws!r})
data_root = Path({data_root!r})
log = EventLog(Path({db!r}), blob_dir=data_root / "blobs")
stack = build_stack(log, data_root=data_root)
task = stack.tasks.create_task("B 侧想写", [{{"root_path": str(ws)}}])
run = stack.tasks.start_run(task.task_id)
stack.leases.acquire([ws], run.run_id)
job = stack.claims.enqueue(run.run_id)
claim = stack.claims.claim(job.job_id)
stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
args = {{"path": "note.md", "content": "B 侧写入"}}
request = build_request(
    task=task, run=run, claim=claim, call_id="c_b",
    tool_id="modify_text", target=ws / "note.md", args=args,
)
decision = stack.dispatcher.prepare(request, claim, args=args)
print("DECISION", decision.kind, decision.rule_id)
print("REASON", decision.reason)
log.close()
sys.exit(0)
"""


def _run_b_side(tmp_path: Path, ws: Path, *, name: str) -> subprocess.CompletedProcess:
    """用独立数据根跑一次 B 侧派发判定，返回子进程结果。"""
    data_root = tmp_path / name
    data_root.mkdir()
    code = _B_CHILD.format(
        src=SRC, ws=str(ws), data_root=str(data_root), db=str(data_root / "events.db")
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env=dict(os.environ),
        capture_output=True,
        **CHILD_TEXT,
        timeout=120,
    )


def _decision(proc: subprocess.CompletedProcess) -> tuple[str, str]:
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("DECISION ")), ""
    )
    if not line:
        raise AssertionError(f"子进程没有输出判定；rc={proc.returncode}\n{proc.stderr}")
    _, kind, rule_id = line.split(" ", 2)
    return kind, rule_id


# ---------------------------------------------------------------- 用例


def test_new_user_has_no_authority_records_and_is_not_blocked(tmp_path):
    """权威根**不存在** = 空集，不是损坏：新用户第一次用不能被自己挡住。

    反例：把"文件不在"当成"读不出来 → 已阻断"，每个干净环境都会自锁。
    """
    ws = _workspace(tmp_path)
    log = EventLog(tmp_path / "events.db")
    assert not (tmp_path / "authority" / "authority.db").exists()
    ensure_not_blocked(log, ws)  # 不抛
    assert blocked_workspaces(log) == set()
    assert not (tmp_path / "authority").exists(), "读路径不能顺手创建权威根"
    log.close()


def test_cross_data_root_block_is_visible_to_another_db(tmp_path):
    """A 数据根恢复出阻断 → B 数据根（不同 --db）的写入必须被拒。"""
    ws = _workspace(tmp_path)
    log_a, _report = _block_in_data_root(ws, tmp_path / "data_a")
    assert authority.is_blocked(ws), "恢复阻断必须同步登记用户级权威根"

    proc = _run_b_side(tmp_path, ws, name="data_b")
    assert proc.returncode == 0, proc.stderr
    kind, rule_id = _decision(proc)
    assert (kind, rule_id) == ("rejected", "dispatch.workspace_blocked"), (
        f"换一个 --db 仍必须被拒，实际 {kind}/{rule_id}"
    )
    log_a.close()


def test_unblock_releases_cross_data_root_block(tmp_path):
    """unblock 之后 B 数据根可写（modify_text 会进入审批，不再是阻断拒绝）。"""
    ws = _workspace(tmp_path)
    log_a, _report = _block_in_data_root(ws, tmp_path / "data_a")
    assert authority.is_blocked(ws)

    unblock(log_a, ws, reason="人工核查：未写入")
    assert not authority.is_blocked(ws), "解除必须同时释放权威根"

    proc = _run_b_side(tmp_path, ws, name="data_b_after")
    assert proc.returncode == 0, proc.stderr
    kind, rule_id = _decision(proc)
    assert kind == "waiting", f"解除后应进入正常审批流，实际 {kind}/{rule_id}"
    log_a.close()


def test_corrupt_authority_root_fails_closed(tmp_path):
    """权威根存在但坏掉 = 已阻断（读不出来 ≠ 没阻断）。"""
    ws = _workspace(tmp_path)
    root = tmp_path / "authority"
    root.mkdir()
    (root / "authority.db").write_bytes(b"this is not a sqlite database")

    log_b = EventLog(tmp_path / "events_b.db")
    with pytest.raises(WorkspaceBlocked) as ei:
        ensure_not_blocked(log_b, ws)
    assert "权威" in str(ei.value), "必须说清是权威状态读不出来，不是普通阻断"
    log_b.close()


def test_authority_record_without_log_event_still_blocks(tmp_path):
    """权威根里登记了、本数据根日志里没有 → 仍然阻断（双向合并，不是"以日志为准"）。"""
    ws = _workspace(tmp_path)
    authority.register_blocked(ws, run_id="run_elsewhere", reason="别的数据根恢复出的阻断")

    log = EventLog(tmp_path / "events.db")
    assert str(ws.resolve()) in blocked_workspaces(log)
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)
    log.close()


def test_migration_backfills_effective_blocks_and_does_not_resurrect_released(tmp_path):
    """迁移：日志里当前有效的阻断补登记进权威根（幂等）；已解除的不复活。"""
    ws = _workspace(tmp_path)
    log = EventLog(tmp_path / "events.db")
    log.append("run_old", "recovery.blocked",
               {"workspace": str(ws.resolve()), "call_ids": ["c1"],
                "reason": "旧版本的阻断只在日志里"},
               actor_kind="system")

    assert mirror_blocks_to_authority(log) == []
    first = authority.active_blocks()
    assert [b.workspace_path for b in first] == [str(ws.resolve())]
    blocked_at = first[0].blocked_at

    # 幂等：再补一次不新增、也不刷新 blocked_at（否则每次启动都像"刚阻断"）
    assert mirror_blocks_to_authority(log) == []
    again = authority.active_blocks()
    assert len(again) == 1 and again[0].blocked_at == blocked_at

    # 日志里已解除的阻断不得被补登记复活
    unblock(log, ws, reason="人工核查")
    assert authority.active_blocks() == []
    assert mirror_blocks_to_authority(log) == []
    assert authority.active_blocks() == []
    log.close()


def test_authority_write_failure_is_reported_not_faked(tmp_path, monkeypatch):
    """写权威根失败 → 恢复报告如实写「跨数据根阻断未生效」，不许假装成功。"""
    ws = _workspace(tmp_path)
    blocker = tmp_path / "not_a_dir"
    blocker.write_bytes(b"x")  # 父路径是文件 → mkdir(parents=True) 必失败
    monkeypatch.setenv("TIANSHU_AUTHORITY_ROOT", str(blocker / "authority"))

    data_root = tmp_path / "data_a"
    log = _seed_crashed_write(ws, data_root)
    shots = SnapshotStore(data_root / "artifacts")
    report = recover_run(log, "run_old", ws, shots, data_root=data_root)

    # 日志侧仍然阻断（先写事件、再写权威根）
    assert report.unresolved == ["c1"]
    assert str(ws.resolve()) in report.blocked_workspaces
    assert not report.ok
    assert any("跨数据根阻断未生效" in p for p in report.problems), report.problems
    log.close()
