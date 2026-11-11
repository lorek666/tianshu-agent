"""#61.2 完整 takeover 的回归证据。

被证明的坏行为：租约可以被过期/死主接管，但接管**不看前任有没有未决副作用**。
一个正在覆盖写、被 `kill -9` 打断的 Run，其 stale 租约被另一个进程抢走后，
新 owner 会在一个"有没有写、写到哪一步"都没核实的目录上继续写。

修法：`LeaseStore.acquire*` 增加可选 `reconcile` 回调（默认 None = 旧行为），
`Dispatcher` 一律传自己的对账函数；抢占 stale lease 前对前任 run 调
`recovery.assess_run`（只读：不写事件、不落盘、不阻断）。有未核实副作用 →
拒绝接管（`LeaseConflict`）+ 登记权威根阻断；干净 → 允许接管并写
`run.control`（`action="lease_takeover"`）审计。

这里的"前任"由**真子进程**扮演：进程内建好 run / lease / 写前证据，落半截
内容后 `SIGKILL` 自己（不是 `return`、不是 `SystemExit`，没有任何清理机会）。
接管也是**另一个真子进程**——如果只在同进程里调两次 `LeaseStore`，就证不了
"两个实例"这件事。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import authority
from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import ensure_not_blocked
from tianshu.leases import LeaseStore
from tianshu.recovery import assess_run
from tianshu.snapshot import SnapshotStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
BEFORE = "写前内容\n"
AFTER = "写入内容\n"
TORN = "写到一半"

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）",
)


# ---------------------------------------------------------------- 夹具


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_bytes(BEFORE.encode("utf-8"))
    return ws


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


#: 前任：建 run + 租约 + 写前证据 → （可选）写半截 → 真 SIGKILL。
_PREV = """
import hashlib, os, signal, sys
sys.path.insert(0, {src!r})
from pathlib import Path

from tianshu.events import EventLog
from tianshu.loop import build_stack

ws = Path({ws!r})
data_root = Path({data_root!r})
run_id = {run_id!r}
log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
stack = build_stack(log, data_root=data_root)
task = stack.tasks.create_task("覆盖写", [{{"root_path": str(ws)}}])
run = stack.tasks.start_run(task.task_id, run_id=run_id)
stack.leases.acquire([ws], run.run_id)
args = {{"path": "note.md", "content": {after!r}}}
log.append(
    run_id,
    "run.start",
    {{"task": "覆盖写", "workspace": str(ws.resolve()), "out": "x",
      "limits": {{}}, "tools": []}},
    actor_kind="core",
    legacy_write=True,
)
log.append(
    run_id,
    "tool.intent",
    {{"call_id": "c1", "tool_id": "modify_text", "args": args,
      "args_hash": hashlib.sha256(b"args").hexdigest()}},
    actor_kind="core",
    legacy_write=True,
)
stack.snapshots.init_run(run_id)
stack.snapshots.record_preimage(
    run_id, "c1", "note.md",
    {before!r}.encode("utf-8"),
    hashlib.sha256({after!r}.encode("utf-8")).hexdigest(),
    root=ws,
)
if {torn}:
    # 覆盖写写到一半被 kill：磁盘既不是写前也不是写后 → 对账无法判定。
    with open(ws / "note.md", "wb") as fh:
        fh.write({torn_text!r}.encode("utf-8"))
        fh.flush()
print("SEEDED", run_id, flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""

#: 接管方：另一个进程，走 Dispatcher 的控制 Run 入口抢同一棵目录树。
_TAKEOVER = """
import sys
sys.path.insert(0, {src!r})
from pathlib import Path

from tianshu.events import EventLog
from tianshu.leases import LeaseConflict
from tianshu.loop import build_stack

ws = Path({ws!r})
data_root = Path({data_root!r})
log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
stack = build_stack(log, data_root=data_root)
try:
    control = stack.dispatcher.open_control_run(
        goal="接管核对", workspaces=[{{"root_path": str(ws)}}]
    )
except LeaseConflict as exc:
    print("REFUSED", str(exc), flush=True)
    log.close()
    sys.exit(2)
print("ALLOWED", control.run.run_id, flush=True)
log.close()
"""


def _run_child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        env=dict(os.environ),
        capture_output=True,
        **CHILD_TEXT,
        timeout=120,
    )


def _seed_dead_run(tmp_path: Path, ws: Path, data_root: Path, *, torn: bool) -> str:
    """让真子进程建好"写到一半的前任"，并以 SIGKILL 收场。"""
    run_id = "run_dead"
    code = _PREV.format(
        src=SRC, ws=str(ws), data_root=str(data_root), run_id=run_id,
        before=BEFORE, after=AFTER, torn=bool(torn), torn_text=TORN,
    )
    proc = _run_child(code)
    assert proc.returncode == -9, (
        f"前任应当被 SIGKILL（rc=-9），实际 rc={proc.returncode}；stderr={proc.stderr}"
    )
    assert "SEEDED" in proc.stdout
    return run_id


def _takeover(tmp_path: Path, ws: Path, data_root: Path) -> subprocess.CompletedProcess:
    return _run_child(
        _TAKEOVER.format(src=SRC, ws=str(ws), data_root=str(data_root))
    )


def _open(data_root: Path) -> EventLog:
    return EventLog(data_root / "events.db", blob_dir=data_root / "blobs")


# ---------------------------------------------------------------- 用例


def test_takeover_refused_when_unresolved_side_effects(tmp_path):
    """kill -9 在覆盖写中途 → 另一进程接管必须先对账：有未核实副作用就拒绝并阻断。"""
    ws = _workspace(tmp_path)
    data_root = _data_root(tmp_path)
    run_id = _seed_dead_run(tmp_path, ws, data_root, torn=True)
    assert (ws / "note.md").read_bytes() == TORN.encode("utf-8")

    proc = _takeover(tmp_path, ws, data_root)
    assert proc.returncode == 2, (
        f"接管必须被拒绝（rc=2），实际 rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "REFUSED" in proc.stdout
    assert "未核实" in proc.stdout, "拒绝原因要能让调度方看懂"

    # ① 工作区被用户级权威根阻断（换数据根也不能绕过）
    assert authority.is_blocked(ws)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_log = EventLog(other_root / "events.db")
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(other_log, ws)
    other_log.close()

    # ② 前任的租约没有被抢走（拒绝 = 一行都不写）
    log = _open(data_root)
    try:
        assert LeaseStore(log).holder(ws).run_id == run_id, "拒绝接管不得改租约"
        blocked = [
            e.payload for e in log.read_run(run_id) if e.type == "recovery.blocked"
        ]
        assert len(blocked) == 1, "拒绝接管必须留下日志侧阻断事件"
        assert blocked[0]["takeover_refused_by"], "要能看出是接管被拒导致的阻断"
        assert blocked[0]["call_ids"] == ["c1"]
    finally:
        log.close()

    # ③ 目标文件没有被任何接管方改动
    assert (ws / "note.md").read_bytes() == TORN.encode("utf-8")


def test_clean_takeover_is_allowed_and_audited(tmp_path):
    """前任只留下"确定未写"的 pending intent（写前证据在、磁盘仍是写前）→ 允许接管并审计。"""
    ws = _workspace(tmp_path)
    data_root = _data_root(tmp_path)
    run_id = _seed_dead_run(tmp_path, ws, data_root, torn=False)
    assert (ws / "note.md").read_bytes() == BEFORE.encode("utf-8")

    proc = _takeover(tmp_path, ws, data_root)
    assert proc.returncode == 0, (
        f"干净状态应当允许接管，实际 rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "ALLOWED" in proc.stdout
    new_run = proc.stdout.split("ALLOWED", 1)[1].strip()

    log = _open(data_root)
    try:
        # ① 租约确实被接管
        assert LeaseStore(log).holder(ws).run_id == new_run
        # ② 用 run.control 留审计（不新增事件类型）
        controls = [e.payload for e in log.read_run(new_run) if e.type == "run.control"]
        assert len(controls) == 1, controls
        audit = controls[0]
        assert audit["action"] == "lease_takeover"
        assert audit["previous_run"] == run_id
        assert audit["checked"]["none_applied"] == ["c1"]
        assert audit["checked"]["unresolved"] == []
    finally:
        log.close()

    # ③ 干净接管不登记阻断
    assert not authority.is_blocked(ws)


# ---------------------------------------------------------------- assess_run 的只读契约


def test_assess_run_is_read_only(tmp_path):
    """对账不许写事件、不许阻断、不许落盘——它只是接管前的"看一眼"。"""
    ws = _workspace(tmp_path)
    data_root = _data_root(tmp_path)
    run_id = _seed_dead_run(tmp_path, ws, data_root, torn=True)

    log = _open(data_root)
    try:
        before_events = len(log.read_run(run_id))
        before_files = sorted(p.name for p in (data_root / "artifacts").rglob("*"))
        assessment = assess_run(data_root, run_id, log=log)
        after_events = len(log.read_run(run_id))
        after_files = sorted(p.name for p in (data_root / "artifacts").rglob("*"))
    finally:
        log.close()

    assert assessment.unknown == ("c1",)
    assert assessment.blocks_takeover
    assert before_events == after_events, "只读对账不得追加事件"
    assert before_files == after_files, "只读对账不得创建/修改快照文件"
    assert not authority.is_blocked(ws), "阻断由调用方在拒绝时登记，对账自己不阻断"


def test_assess_run_reports_missing_manifest_instead_of_guessing(tmp_path):
    """写了 intent 却没清单 → missing_manifest，不能替它判"没写"（也不许抛异常）。"""
    _workspace(tmp_path)
    data_root = _data_root(tmp_path)
    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    log.append("run_no_manifest", "tool.intent",
               {"call_id": "c1", "tool_id": "modify_text",
                "args": {"path": "note.md", "content": AFTER}, "args_hash": "x"},
               actor_kind="core", legacy_write=True)

    assessment = assess_run(data_root, "run_no_manifest", log=log)
    assert assessment.missing_manifest is True
    assert assessment.pending_writes == 1
    assert assessment.blocks_takeover
    log.close()


def test_assess_run_reports_corrupt_manifest_instead_of_raising(tmp_path):
    """清单坏了 → corrupt=True（不是抛异常）；接管方据此拒绝，校验器不许先崩。"""
    _workspace(tmp_path)
    data_root = _data_root(tmp_path)
    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    log.append("run_corrupt", "tool.intent",
               {"call_id": "c1", "tool_id": "modify_text",
                "args": {"path": "note.md", "content": AFTER}, "args_hash": "x"},
               actor_kind="core", legacy_write=True)
    shots = SnapshotStore(data_root / "artifacts")
    shots.manifest_path("run_corrupt").write_bytes(b"{not json")

    assessment = assess_run(data_root, "run_corrupt", log=log)
    assert assessment.corrupt is True
    assert assessment.blocks_takeover
    log.close()


# ---------------------------------------------------------------- 主循环（真正的写入路径）

#: 第二个进程：用 `AgentLoop.start` 起一个**新 Run**，它必须抢前任的 stale 租约。
#: 这条路径以前没有对账——接管保护只挂在控制 Run 上，真正的写入入口照样能接管。
_LOOP_START = """
import sys
sys.path.insert(0, {src!r})
from pathlib import Path

from tianshu.events import EventLog
from tianshu.leases import LeaseConflict
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ScriptedModel

ws = Path({ws!r})
data_root = Path({data_root!r})
log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
stack = build_stack(log, data_root=data_root)
task = stack.tasks.create_task("新 Run 想接管", [{{"root_path": str(ws)}}])
engine = AgentLoop(
    tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
    dispatcher=stack.dispatcher, approvals=stack.approvals,
    snapshots=stack.snapshots, model=ScriptedModel([]),
    limits=Limits(2, 1000),
)
try:
    engine.start(task.task_id)
except LeaseConflict as exc:
    print("REFUSED", str(exc), flush=True)
    log.close()
    sys.exit(2)
print("ALLOWED", flush=True)
log.close()
"""


def test_main_loop_refuses_to_steal_unresolved_lease(tmp_path):
    """主循环入口也必须先对账：`AgentLoop.start` 抢 stale 租约时拒绝未核实的前任。

    这是"完整 takeover"真正的落点。控制 Run（撤销/恢复）只是少数入口；
    用户起的每一个新 Run 都走 `start()`，它不核对，接管保护就是个摆设。
    """
    ws = _workspace(tmp_path)
    data_root = _data_root(tmp_path)
    run_id = _seed_dead_run(tmp_path, ws, data_root, torn=True)
    assert (ws / "note.md").read_bytes() == TORN.encode("utf-8")

    proc = _run_child(_LOOP_START.format(
        src=SRC, ws=str(ws), data_root=str(data_root)
    ))
    assert proc.returncode == 2, (
        f"主循环接管必须被拒绝（rc=2），实际 rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "REFUSED" in proc.stdout
    assert "未核实" in proc.stdout

    # ① 工作区被用户级权威根阻断（换数据根也绕不过去）
    assert authority.is_blocked(ws)
    # ② 前任的租约没被抢（拒绝 = 一行都不写）
    log = _open(data_root)
    try:
        assert LeaseStore(log).holder(ws).run_id == run_id
        blocked = [
            e.payload for e in log.read_run(run_id) if e.type == "recovery.blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["call_ids"] == ["c1"]
    finally:
        log.close()
    # ④ 目标文件一个字节都没被接管方改
    assert (ws / "note.md").read_bytes() == TORN.encode("utf-8")

