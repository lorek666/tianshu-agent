"""M1/M2 复审修法的回归测试（docs/DEBUG-M0M5.md）。

每一条都对着复审报告里的一条发现，而且都**先造出复审描述的那个坏状态**，
再断言修法真的把它堵住了：

- 中-1：派发 phase-1 之后插入 `recovery.blocked`，写入必须被拒；
  以及恢复写阻断事件必须与派发落盘抢同一把执行守卫。
- 中-2：未知 `tool_id` 的恢复必须判 unknown 并阻断，不能猜成"没写"。
- 中-4：解析不出 `workspace` 的阻断事件必须 fail-closed，不能被静默跳过。
- 轻-1：同内容的覆盖写判 `none_applied`，不产出 applied 凭据。
- 轻-3：`unblock` 的合成 run_id 跨进程稳定（内容摘要，不是 `hash()`）。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import CHILD_TEXT, write_exact

from tianshu.dispatcher import (
    KIND_ADMITTED,
    KIND_WAITING,
    Dispatcher,
    build_request,
)
from tianshu.errors import EventLogCorrupt, WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces, ensure_not_blocked, unblock
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore, execution_guard
from tianshu.permissions import (
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.tools import FileExecutor

ORIGINAL = "原始内容\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _types(log: EventLog, run_id: str) -> list[str]:
    return [e.type for e in log.read_run(run_id)]


@dataclass
class Env:
    root: Path
    ws: Path
    data: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    def new_run(self):
        task = self.tasks.create_task("整理目录里的笔记", [{"root_path": str(self.ws)}])
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([self.ws], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim


def _env(tmp_path: Path) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", ORIGINAL)
    data = tmp_path / "authority"
    data.mkdir()

    log = EventLog(tmp_path / "events.db")
    leases = LeaseStore(log)
    claims = JobStore(log)
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=data)
    approvals = ApprovalStore(log)
    grants = GrantStore(log)
    snapshots = SnapshotStore(tmp_path / "artifacts")
    dispatcher = Dispatcher(
        log,
        tasks,
        leases,
        PolicyEngine(),
        approvals,
        grants=grants,
        claims=claims,
        snapshots=snapshots,
        data_root=data,
    )
    return Env(
        root=tmp_path,
        ws=ws,
        data=data,
        log=log,
        leases=leases,
        claims=claims,
        tasks=tasks,
        approvals=approvals,
        grants=grants,
        snapshots=snapshots,
        dispatcher=dispatcher,
    )


@pytest.fixture
def env(tmp_path):
    e = _env(tmp_path)
    try:
        yield e
    finally:
        e.log.close()


def _create_request(env: Env, task, run, claim, *, name="new.md", call_id="c1"):
    args = {"name": name, "content": "新文件\n"}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id="create_report",
        target=env.ws / name,
        args=args,
    )
    return request, args


# ---------------------------------------------------------------- 中-1


def test_block_inserted_inside_execution_guard_still_blocks_the_write(
    env: Env, monkeypatch
):
    """派发 phase-1 查过阻断之后才落库的阻断，必须仍然拦住这次写入。

    复审复现的窗口：`_authority` 查完 → 真正写文件之间插入 `recovery.blocked`。
    这里用**另一条连接**（等价于另一个进程）在守卫内、执行器动手之前落一条阻断，
    断言写入被拒且文件没被创建。
    """
    task, run, claim = env.new_run()
    request, args = _create_request(env, task, run, claim)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_ADMITTED

    other = EventLog(env.log.db_path)
    original = FileExecutor.execute

    def patched(self, admitted, permit):
        # phase-1（含 ensure_not_blocked）已经提交，此时正持有执行守卫。
        other.append(
            "other_run",
            "recovery.blocked",
            {
                "workspace": str(env.ws.resolve()),
                "call_ids": ["cx"],
                "reason": "另一个进程的恢复判定无法核实",
            },
            actor_kind="system",
        )
        return original(self, admitted, permit)

    monkeypatch.setattr(FileExecutor, "execute", patched)
    try:
        result = env.dispatcher.dispatch(decision.request_id, claim)
    finally:
        other.close()

    assert result.status == "rejected", result
    assert result.effect_state == "none_applied"
    assert result.output["rule_id"] == "dispatch.workspace_blocked"
    assert not (env.ws / "new.md").exists(), "被阻断后仍然写出了文件"
    assert "receipt" not in _types(env.log, run.run_id)


def test_recovery_blocking_write_contends_for_the_execution_guard(tmp_path: Path):
    """恢复写 `recovery.blocked` 要和派发落盘抢同一把守卫，而不是各写各的。

    复审 中-1 的第二种修法：两件事排进同一个全序。守卫被占着的时候，恢复
    应该**等着**（而不是抢先落阻断），释放之后才落。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", "原始内容\n")
    data = tmp_path / "authority"
    data.mkdir()
    db = tmp_path / "events.db"
    snapshots = SnapshotStore(tmp_path / "artifacts")

    # 造一个"核实不了"的 run：有写前证据，磁盘却是第三种内容
    setup = EventLog(db)
    setup.append(
        "run_x",
        "run.start",
        {"task": "t", "workspace": str(ws.resolve()), "out": "note.md",
         "limits": {}, "tools": []},
        actor_kind="core",
    )
    setup.append(
        "run_x",
        "tool.intent",
        {"call_id": "c1", "tool_id": "modify_text",
         "args": {"path": "note.md", "content": "写后内容\n"},
         "args_hash": _sha("x")},
        actor_kind="core",
    )
    snapshots.record_preimage(
        "run_x", "c1", "note.md",
        before="原始内容\n".encode(),
        expected_after=_sha("写后内容\n"),
        root=ws,
    )
    write_exact(ws / "note.md", "完全不相干的内容\n")
    setup.close()

    started = threading.Event()
    done = threading.Event()
    failure: list[BaseException] = []

    def run_recovery() -> None:
        log = EventLog(db)
        try:
            started.set()
            recover_run(log, "run_x", ws, snapshots, data_root=data)
        except BaseException as exc:  # noqa: BLE001 - 线程里的错要带回主线程断言
            failure.append(exc)
        finally:
            log.close()
            done.set()

    with execution_guard(data):
        worker = threading.Thread(target=run_recovery, name="recovery")
        worker.start()
        assert started.wait(10), "恢复线程没起来"
        time.sleep(0.3)
        reader = EventLog(db)
        try:
            # 守卫还在我们手里：恢复必须等着，不能先落阻断
            assert [e.type for e in reader.read_run("run_x")].count(
                "recovery.blocked"
            ) == 0
        finally:
            reader.close()
    assert done.wait(15), "守卫释放后恢复没有完成"
    assert not failure, failure
    worker.join(5)

    reader = EventLog(db)
    try:
        assert "recovery.blocked" in [e.type for e in reader.read_run("run_x")]
        assert str(ws.resolve()) in blocked_workspaces(reader)
    finally:
        reader.close()


# ---------------------------------------------------------------- 中-2


def test_unknown_tool_recovery_is_unknown_and_blocks(tmp_path: Path):
    """未知 tool_id 不能走"清单里没有条目 = 确定没写"的推理。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", "原始内容\n")
    log = EventLog(tmp_path / "events.db")
    log.append(
        "run_legacy",
        "run.start",
        {"task": "t", "workspace": str(ws.resolve()), "out": "x.md",
         "limits": {}, "tools": []},
        actor_kind="core",
    )
    log.append(
        "run_legacy",
        "tool.intent",
        # 这个工具在当前代码里不存在（更早/更新版本写下的日志）
        {"call_id": "c1", "tool_id": "rename_everything",
         "args": {"path": "note.md"}, "args_hash": "deadbeef"},
        actor_kind="core",
    )

    # 故意不给快照存储：旧实现会走 "没有清单 → 确定未写" 的分支
    report = recover_run(log, "run_legacy", ws)

    assert report.unresolved == ["c1"], report
    assert report.none_applied == []
    assert report.applied == []
    assert str(ws.resolve()) in report.blocked_workspaces
    assert "recovery.blocked" in _types(log, "run_legacy")
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)
    log.close()


# ---------------------------------------------------------------- 中-4


def test_malformed_block_event_fails_closed(env: Env):
    """解析不出 workspace 的阻断事件 = "不知道哪个目录被阻断"，不是"没阻断"。"""
    env.log.append(
        "r_legacy",
        "recovery.blocked",
        {"call_ids": ["c9"], "reason": "payload 里没有 workspace"},
        actor_kind="system",
    )

    with pytest.raises(EventLogCorrupt):
        blocked_workspaces(env.log)
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(env.log, env.ws)

    # 写路径同样 fail-closed：拒绝并说明原因，而不是照写
    task, run, claim = env.new_run()
    request, args = _create_request(env, task, run, claim)
    decision = env.dispatcher.prepare(request, claim, args=args)
    result = env.dispatcher.dispatch(decision.request_id, claim)
    assert result.status == "rejected"
    assert result.output["rule_id"] == "dispatch.workspace_blocked"
    assert not (env.ws / "new.md").exists()


# ---------------------------------------------------------------- 轻-1


def test_same_content_overwrite_is_none_applied(env: Env):
    """同内容覆盖 = 没有可观测副作用：不声称写入、不产出 applied 凭据。"""
    task, run, claim = env.new_run()
    args = {"path": "note.md", "content": ORIGINAL}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id="c1",
        tool_id="modify_text",
        target=env.ws / "note.md",
        args=args,
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_WAITING, decision
    env.approvals.decide(
        decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )

    result = env.dispatcher.dispatch(decision.request_id, claim)

    assert result.ok, result
    assert result.effect_state == "none_applied"
    assert "receipt" not in _types(env.log, run.run_id)
    assert (env.ws / "note.md").read_bytes() == ORIGINAL.encode("utf-8")


# ---------------------------------------------------------------- 轻-3


def test_unblock_run_id_is_stable_across_processes(env: Env):
    """解除阻断的合成 run_id 必须跨进程可复现（旧实现用 `hash()`）。"""
    ws = env.ws.resolve()
    run_id = unblock(env.log, ws, reason="人工核查过")
    expected = "unblock_" + hashlib.sha256(str(ws).encode("utf-8")).hexdigest()[:10]
    assert run_id == expected

    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "from pathlib import Path\n"
        "from tianshu.events import EventLog\n"
        "from tianshu.guard import unblock\n"
        f"log = EventLog(Path({str(env.log.db_path)!r}))\n"
        f"print(unblock(log, Path({str(ws)!r}), reason='另一次进程'))\n"
        "log.close()\n"
    )
    ids = []
    for seed in ("1", "2"):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env={**_child_env(), "PYTHONHASHSEED": seed},
            cwd=str(env.root),
            capture_output=True,
            **CHILD_TEXT,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        ids.append(proc.stdout.strip())
    assert ids == [run_id, run_id], f"跨进程不稳定：{ids}"


def _child_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if k != "TIANSHU_FAILPOINT"}
