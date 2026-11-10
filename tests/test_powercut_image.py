"""真实卷丢缓存实验（opt-in：`TIANSHU_POWERCUT=1`，CI 默认 skip）。

规格 T1 第 3 条：`hdiutil` 建 64MB HFS+ 镜像 → attach → 子进程跑**真实写入**
→ `hdiutil detach -force`（不给文件系统 flush 机会）→ 重新 attach → 跑
`recover`，对四条不变量断言。

**这个实验能证明什么、不能证明什么**（详见 docs/powercut-evidence.md）：

能证明：
- `fsutil.atomic_write_bytes` 走完「tmp→fsync→rename→目录 fsync」之后，
  在真实 HFS+ 卷被强制摘下、重新挂上后，字节与事件日志仍然可读可解析；
- 在四个持久化边界上崩溃、随后卷被强制摘下时，目标文件只会落在「写前」或
  「写后」两种合法状态，恢复/撤销不崩、不猜。

不能证明：
- 「没有 fsync 的字节一定丢」。本机实测 `detach -force` 仍会刷脏页（20MB
  未 fsync 的写入照样存活），所以这个手段无法制造"丢缓存"；真实掉电的
  页缓存语义必须靠块设备/断电实验，不在本轮范围。
- HFS+ 以外的文件系统、APFS、外置 SSD 的真实断电行为。

为什么仍然值得跑：它把"只进程内 `os._exit`"补成"真实文件系统 + 强制摘卷"，
证明我们依赖的 fsync/rename 顺序在真实卷上成立，且恢复对重挂后的状态给出的
是合法裁决——这是 tempdir 上的测试拿不到的证据。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_TEXT, write_exact

from tianshu.events import EventLog
from tianshu.guard import ensure_not_blocked
from tianshu.receipt import verify_receipts
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.workspace import Workspace

pytestmark = pytest.mark.skipif(
    os.environ.get("TIANSHU_POWERCUT") != "1",
    reason="真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）",
)

TARGET = "report.md"
BEFORE = "写前内容\n"
AFTER = "写入内容\n"
BEFORE_BYTES = BEFORE.encode("utf-8")
AFTER_BYTES = AFTER.encode("utf-8")

SRC = str(Path(__file__).resolve().parents[1] / "src")


# 子进程：用公开 API 装配一次真实 Run（与 `tianshu run` 同一条装配路径），
# 数据根 / 工作区 / 快照全部落在挂载卷上。跑完直接用 os._exit 退出、不 close，
# 模拟"进程被断电带走"（SQLite WAL 不 checkpoint）。
_CHILD = r'''
import json, os, pathlib, sys
sys.path.insert(0, os.environ["TIANSHU_SRC"])
from tianshu import sandbox
sandbox.configure_backend(sandbox.DEFAULT_BACKEND)
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import HumanIdentity

cfg = json.loads(os.environ["TIANSHU_POWERCUT_CASE"])
ws = pathlib.Path(cfg["ws"])
log = EventLog(pathlib.Path(cfg["db"]))
stack = build_stack(log)
target = cfg["target"]
if cfg["tool"] == "create_report":
    call = ToolCall("c2", "create_report", {"name": target, "content": cfg["after"]})
else:
    call = ToolCall("c2", "modify_text", {"path": target, "content": cfg["after"]})
model = ScriptedModel([
    ModelResponse("tool_use", tool_calls=[ToolCall("c1", "list_files", {"path": "."})]),
    ModelResponse("tool_use", tool_calls=[call]),
    ModelResponse("end_turn", content="完成"),
])
engine = AgentLoop(
    tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
    dispatcher=stack.dispatcher, approvals=stack.approvals,
    snapshots=stack.snapshots, model=model, limits=Limits(8, 20_000),
)
task = stack.tasks.create_task("掉电实验", [str(ws)])
result = engine.start(task.task_id, out_name=target)
if result.state == "waiting_approval":
    actor = HumanIdentity.local_cli()
    for approval in stack.approvals.pending(run_id=result.run_id):
        stack.approvals.decide(approval.approval_id, decision="approved", actor=actor)
    result = engine.resume_run(result.run_id)
rc = 0 if result.terminal else 1
# 不 close：模拟被断电带走，WAL / 页缓存都留在内核里
os._exit(rc)
'''


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, **CHILD_TEXT)


def _hdiutil(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["hdiutil", *args])


@pytest.fixture()
def powercut_volume(tmp_path: Path):
    """64MB HFS+ 镜像；attach/detach -force 由用例显式调用。"""
    if sys.platform != "darwin":
        pytest.skip("真实摘卷实验只在 macOS 上成立（hdiutil / HFS+）")
    if shutil.which("hdiutil") is None:
        pytest.skip("找不到 hdiutil")

    image = tmp_path / "powercut.dmg"
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    created = _hdiutil(
        "create", "-size", "64m", "-fs", "HFS+",
        "-volname", f"TIANSHU_POWERCUT_{os.getpid()}",
        "-layout", "NONE", str(image),
    )
    assert created.returncode == 0, (
        f"hdiutil create 失败：{created.returncode}\n"
        f"stdout={created.stdout}\nstderr={created.stderr}"
    )

    def attach() -> None:
        out = _hdiutil("attach", "-nobrowse", "-noverify",
                       "-mountpoint", str(mnt), str(image))
        assert out.returncode == 0, (
            f"hdiutil attach 失败：{out.returncode}\n"
            f"stdout={out.stdout}\nstderr={out.stderr}"
        )

    def detach_force() -> None:
        out = _hdiutil("detach", "-force", str(mnt))
        assert out.returncode == 0, (
            f"hdiutil detach -force 失败：{out.returncode}\n"
            f"stdout={out.stdout}\nstderr={out.stderr}"
        )

    attach()
    try:
        yield mnt, attach, detach_force
    finally:
        # 尽力收尾：已经 detach 过就忽略错误，别让清理掩盖真正的失败。
        if mnt.is_mount():
            _hdiutil("detach", "-force", str(mnt))
        shutil.rmtree(mnt, ignore_errors=True)


def _case_dir(mnt: Path, name: str) -> tuple[Path, Path, Path]:
    """(workspace, data_root, db) 全部落在挂载卷上。"""
    root = mnt / name
    ws = root / "ws"
    data = root / "data"
    ws.mkdir(parents=True)
    data.mkdir(parents=True)
    return ws, data, data / "events.db"


def _spawn_child(
    ws: Path, db: Path, *, tool: str, failpoint: str | None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key in ("TIANSHU_FAILPOINT", "TIANSHU_FAILPOINT_PATH"):
        env.pop(key, None)
    if failpoint is not None:
        env["TIANSHU_FAILPOINT"] = failpoint
        # 只对这一次目标写入生效：清单 / blob 的原子写入不引爆
        env["TIANSHU_FAILPOINT_PATH"] = str(ws / TARGET)
    env["TIANSHU_SRC"] = SRC
    env["TIANSHU_SANDBOX_BACKEND"] = "local_fs"
    env["TIANSHU_POWERCUT_CASE"] = json.dumps({
        "ws": str(ws), "db": str(db), "tool": tool,
        "target": TARGET, "after": AFTER,
    })
    return subprocess.run(
        [sys.executable, "-c", _CHILD], env=env, capture_output=True,
        timeout=180, **CHILD_TEXT,
    )


def _open(db: Path) -> tuple[EventLog, SnapshotStore]:
    return EventLog(db), SnapshotStore(db.parent / "artifacts")


def _latest_run(log: EventLog) -> str:
    row = log._conn.execute(
        "SELECT run_id FROM events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "事件库是空的（断电后日志没恢复）"
    return row["run_id"]


def _result(log: EventLog, run_id: str) -> dict:
    """写工具的 result（不是只读 list_files 那条）。"""
    rows = [
        e.payload for e in log.read_run(run_id)
        if e.type == "tool.result"
        and e.payload.get("tool_id") in ("create_report", "modify_text")
    ]
    assert rows, "没有写工具的 tool.result"
    return rows[0]


def _undo(log: EventLog, data: Path, run_id: str, ws: Path):
    store = SnapshotStore(data / "artifacts")
    return store.undo(
        run_id, Workspace(ws),
        guard=lambda: ensure_not_blocked(log, ws),
        allow_direct_write=True,
    )


def test_powercut_complete_write_survives_forced_detach(powercut_volume, tmp_path):
    """真实写入跑完 → 不给 flush 机会强制摘卷 → 重挂：fsync 过的产物必须可恢复。"""
    mnt, attach, detach_force = powercut_volume
    ws, data, db = _case_dir(mnt, "complete")
    write_exact(ws / TARGET, BEFORE)

    proc = _spawn_child(ws, db, tool="modify_text", failpoint=None)
    assert proc.returncode == 0, (
        f"真实写入没跑完：rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    print(f"[powercut] 写入完成：target={ws / TARGET} 字节={len(AFTER_BYTES)}", flush=True)
    print(f"[powercut] 数据根：{data}", flush=True)

    detach_force()
    print("[powercut] hdiutil detach -force 完成（未给文件系统 flush 机会）", flush=True)
    attach()
    print("[powercut] 重新 attach 完成", flush=True)

    # 四条不变量 #1：目标只能是写前/写后，不能半截
    assert (ws / TARGET).read_bytes() == AFTER_BYTES, "重挂后目标不是写后字节"

    # #2：事件日志与快照清单必须可读（fsync 后的字节在强制摘卷后仍可恢复）
    log, store = _open(db)
    run_id = _latest_run(log)
    events = log.read_run(run_id)
    assert [e.type for e in events][:1] == ["run.start"], events[:1]
    changes = store.load(run_id)
    assert len(changes) == 1 and changes[0].path == TARGET

    # #3：恢复裁决与磁盘一致；不补事件、不误报
    report = recover_run(log, run_id, ws, store)
    assert report.ok and report.unresolved == [], report
    result = _result(log, run_id)
    assert result["effect_state"] == "applied"
    assert result["output"]["sha256"] == hashlib.sha256(AFTER_BYTES).hexdigest()
    assert verify_receipts(log, run_id) == [], "重挂后凭据仍必须能独立核对"

    # #4：撤销按 preimage 重建回原字节
    undo = _undo(log, data, run_id, ws)
    assert undo.ok, undo.problems
    assert undo.restored == [TARGET]
    assert (ws / TARGET).read_bytes() == BEFORE_BYTES
    log.close()
    print("[powercut] 四不变量全部成立", flush=True)


@pytest.mark.parametrize(
    "failpoint,expect_applied",
    [
        ("after-tmp-write", False),  # rename 未发生 → 目标仍是写前内容
        ("after-dir-fsync", True),   # 原子替换已持久 → 目标已是写后内容
    ],
)
def test_powercut_crash_at_persistence_boundary(
    powercut_volume, tmp_path, failpoint, expect_applied
):
    """在持久化边界崩溃 + 强制摘卷：重挂后恢复只能给出合法状态，不许半截。"""
    mnt, attach, detach_force = powercut_volume
    ws, data, db = _case_dir(mnt, f"crash-{failpoint}")
    # create_report 不触发审批，也不要求目标已存在；目标文件写是观察的边界
    proc = _spawn_child(ws, db, tool="create_report", failpoint=failpoint)
    assert proc.returncode == 77, (
        f"{failpoint} 应当在持久化边界退出：rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert f"[failpoint] {failpoint}" in proc.stderr, proc.stderr
    print(f"[powercut] {failpoint}：子进程已在持久化边界硬退", flush=True)

    detach_force()
    attach()
    print(f"[powercut] {failpoint}：强制摘卷 + 重挂完成", flush=True)

    # #1：崩溃点决定目标状态；只有"写前（不存在）"或"写后"两种
    observed = (ws / TARGET).read_bytes() if (ws / TARGET).exists() else None
    assert observed in (None, AFTER_BYTES), f"{failpoint}：出现半截内容 {observed!r}"
    assert (observed is not None) == expect_applied

    # #2：日志与清单必须可读（或者给出类型化损坏，不许裸崩）
    log, store = _open(db)
    run_id = _latest_run(log)
    changes = store.load(run_id)
    assert len(changes) == 1, "写前证据必须已落盘（fsync 后强制摘卷仍可读）"

    # #3：恢复裁决必须与磁盘一致；不确定就 unknown + 阻断
    report = recover_run(log, run_id, ws, store)
    if report.unresolved:
        assert report.blocked_workspaces, "unknown 必须阻断工作区"
    else:
        result = _result(log, run_id)
        if expect_applied:
            assert result["effect_state"] == "applied", result
            assert result["output"]["after_state"] == hashlib.sha256(
                AFTER_BYTES
            ).hexdigest()
            assert verify_receipts(log, run_id) == []
        else:
            assert result["effect_state"] == "none_applied", result

    # #4：撤销可还原——写前状态是 absent，applied 时撤销必须把新建的文件删掉；
    # 没发生时撤销必须拒绝且零修改（文件本来就该不存在）。
    undo = _undo(log, data, run_id, ws)
    if expect_applied:
        assert undo.ok, undo.problems
        assert undo.deleted == [TARGET]
    else:
        assert not undo.ok
        assert undo.entries == []
    assert not (ws / TARGET).exists(), "撤销后目标必须不存在（回到写前 absent）"
    log.close()
    print(f"[powercut] {failpoint}：四不变量成立（applied={expect_applied}）", flush=True)
