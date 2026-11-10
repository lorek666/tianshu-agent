"""T1 验收：持久化边界上的掉电/崩溃一致性。

两层证据，各自能证明什么，见 `docs/powercut-evidence.md`：

1. **进程级持久化边界注入**（本文件主体）：在 `fsutil.atomic_write_bytes` 的
   四个边界上 `os._exit`，矩阵 = 4 故障点 × {新建, 覆盖写, 删除}，每格三条断言：
   - 不变量：目标文件只能是"写前字节"或"写后字节"，绝不出现半截；
   - 恢复判据：`recover_run` 不崩、不猜，给出可核对的 applied / none_applied，
     结果与磁盘逐字节一致；能产出可独立核对的凭据；
   - 撤销可还原：副作用确实发生时，`undo` 按 preimage 重建回原字节；没发生
     时撤销必须拒绝且一个字节都不改。
2. **撕裂/丢尾**：清单截断、blob 截断、blob 位翻转、`events.db`-WAL 截断，
   断言恢复/撤销不崩、不把"读不出来"当"没事"。

**边界先说清**：`os._exit` 只证明"进程死在这一步"，不证明页缓存里未 fsync 的
字节真的丢了。真实摘卷实验见 `tests/test_powercut_image.py`（opt-in，CI 默认
skip）。所以本文件证明的是 **恢复逻辑在四个确定边界上的行为**，不是硬件语义。

删除类动作（`delete_file`）的四个 `fsutil` 边界都落在"写前证据落盘"期间
（`record_preimage` → `unlink`），进程死在任何边界时 `unlink` 都还没发生。
evidence 已持久的那两格（rename / dir-fsync）再模拟"证据已持久、进程随后执行了
删除、然后断电"这一真实可达状态，用来验证 applied + 撤销重建；evidence 未持久
的两格（tmp-write / tmp-fsync）下删除不可能先于证据发生（代码顺序保证），只
断言"撤销拒绝且文件仍是写前内容"。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import CHILD_TEXT, write_exact

from tianshu.errors import EventLogCorrupt, SnapshotCorrupt, WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces, ensure_not_blocked, unblock
from tianshu.permissions import ACTION_CREATE, ACTION_DELETE, ACTION_OVERWRITE
from tianshu.receipt import verify_receipts
from tianshu.recovery import recover_run
from tianshu.snapshot import ABSENT, SnapshotStore
from tianshu.workspace import Workspace

RUN = "run_t1"
CALL = "c1"
TARGET = "report.md"
NOTE = "诱饵文件，任何写入都不该碰它\n"
BEFORE = "写前内容\n"
AFTER = "写入内容\n"
NOTE_BYTES = NOTE.encode("utf-8")
BEFORE_BYTES = BEFORE.encode("utf-8")
AFTER_BYTES = AFTER.encode("utf-8")
BEFORE_SHA = hashlib.sha256(BEFORE_BYTES).hexdigest()
AFTER_SHA = hashlib.sha256(AFTER_BYTES).hexdigest()

#: `fsutil.atomic_write_bytes` 里的四个持久化边界。
FAILPOINTS = (
    "after-tmp-write",   # tmp 写完，未 fsync
    "after-tmp-fsync",   # tmp 已 fsync，rename 未发生
    "after-rename",      # rename 已发生，目录项未 fsync
    "after-dir-fsync",   # 整条原子替换已持久
)
#: rename 之后副作用才可见；前两个边界读到的必须是写前状态。
VISIBLE = frozenset({"after-rename", "after-dir-fsync"})

KINDS = ("create", "overwrite", "delete")
TOOL = {"create": "create_report", "overwrite": "modify_text", "delete": "delete_file"}
ACTION = {"create": ACTION_CREATE, "overwrite": ACTION_OVERWRITE, "delete": ACTION_DELETE}

SRC = str(Path(__file__).resolve().parents[1] / "src")


# 子进程入口：按真实写入顺序（先落写前证据，再动目标文件）在指定边界硬退。
#
# 为什么不直接走 AgentLoop：这里要测的是"目标文件写到哪一步断电"，而一次 Run
# 里 `atomic_write_bytes` 会被调用很多次（快照清单 / blob / 目标）。走完整
# Run 的话第一个命中的多半是清单，测不到目标文件。`TIANSHU_FAILPOINT_PATH`
# 把注入限定到本次要观察的那个目标，下面两次调用与产品路径同序：
#   ctx.prewrite()            → SnapshotStore.record_preimage()
#   io.write_bytes_atomic()   → fsutil.atomic_write_bytes()
# 删除路径同理（prewrite 后 io.unlink）。
_CHILD = r'''
import json, os, pathlib, sys
sys.path.insert(0, os.environ["TIANSHU_SRC"])
from tianshu import sandbox
sandbox.configure_backend(sandbox.DEFAULT_BACKEND)
from tianshu.events import EventLog
from tianshu.snapshot import ABSENT, SnapshotStore

cfg = json.loads(os.environ["TIANSHU_T1_CASE"])
ws = pathlib.Path(cfg["ws"])
target = ws / cfg["target"]
log = EventLog(pathlib.Path(cfg["db"]))
store = SnapshotStore(pathlib.Path(cfg["artifacts"]))
run_id = cfg["run_id"]
store.init_run(run_id)

log.append(run_id, "run.start", {
    "task": "掉电一致性", "workspace": str(ws), "out": "x.md",
    "limits": {}, "tools": []}, actor_kind="core")
kind = cfg["kind"]
args = ({"name": cfg["target"], "content": cfg["after"]} if kind == "create"
        else {"path": cfg["target"]})
log.append(run_id, "tool.intent", {
    "call_id": cfg["call"],
    "tool_id": cfg["tool"],
    "args": args,
    "args_hash": "0" * 64,
}, actor_kind="core")

def arm():
    """在真实写边界之前才武装注入：init_run / blob 落盘都不该命中。"""
    os.environ["TIANSHU_FAILPOINT"] = cfg["fp"]
    os.environ["TIANSHU_FAILPOINT_PATH"] = cfg["scope"]

if kind == "delete":
    # scope 指向清单：死在这一步 → unlink 还没发生
    arm()
    store.record_preimage(run_id, cfg["call"], cfg["target"],
                          cfg["before"].encode(), ABSENT, root=ws)
    sandbox.current_backend().unlink(target)
else:
    before = None if kind == "create" else cfg["before"].encode()
    store.record_preimage(run_id, cfg["call"], cfg["target"], before,
                          cfg["after_sha"], root=ws)
    arm()
    sandbox.current_backend().write_bytes_atomic(target, cfg["after"].encode())
sys.exit(0)  # 只有注入没命中才会走到这里
'''


@dataclass(frozen=True)
class Case:
    ws: Path
    artifacts: Path
    db: Path

    @property
    def target(self) -> Path:
        return self.ws / TARGET

    @property
    def manifest(self) -> Path:
        return self.artifacts / "snapshots" / f"{RUN}.json"

    def target_bytes(self) -> bytes | None:
        return self.target.read_bytes() if self.target.exists() else None

    def open(self) -> tuple[EventLog, SnapshotStore]:
        return EventLog(self.db), SnapshotStore(self.artifacts)


def _spawn(case: Case, kind: str, fp: str) -> subprocess.CompletedProcess[str]:
    """在指定边界硬退；断言的 failure 里带 stderr，避免"起不来"只能靠猜。"""
    env = dict(os.environ)
    for key in ("TIANSHU_FAILPOINT", "TIANSHU_FAILPOINT_PATH"):
        env.pop(key, None)
    env["TIANSHU_SRC"] = SRC
    env["TIANSHU_SANDBOX_BACKEND"] = "local_fs"
    env["TIANSHU_T1_CASE"] = json.dumps({
        "run_id": RUN,
        "call": CALL,
        "kind": kind,
        "tool": TOOL[kind],
        "target": TARGET,
        "ws": str(case.ws),
        "artifacts": str(case.artifacts),
        "db": str(case.db),
        "before": BEFORE,
        "after": AFTER,
        "after_sha": AFTER_SHA,
        "fp": fp,
        # 新建 / 覆盖写注入在目标文件；删除注入在写前证据（清单）上。
        "scope": str(case.manifest if kind == "delete" else case.target),
    })
    return subprocess.run(
        [sys.executable, "-c", _CHILD], env=env, capture_output=True,
        timeout=120, **CHILD_TEXT,
    )


def _crash(tmp_path: Path, kind: str, fp: str, tag: str = "") -> Case:
    root = tmp_path / f"{kind}-{fp}{tag}"
    ws = root / "ws"
    ws.mkdir(parents=True)
    ws = ws.resolve()
    write_exact(ws / "note.md", NOTE)
    if kind in ("overwrite", "delete"):
        write_exact(ws / TARGET, BEFORE)
    artifacts = (root / "artifacts").resolve()
    artifacts.mkdir()
    db = (root / "events.db").resolve()
    case = Case(ws=ws, artifacts=artifacts, db=db)

    proc = _spawn(case, kind, fp)
    assert proc.returncode == 77, (
        f"{kind}/{fp} 应当在故障点退出，实际 {proc.returncode}；"
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    # 退出码 77 不够：要确认炸的是**这次指定的**故障点，而不是别的路径碰巧 77。
    assert f"[failpoint] {fp}" in proc.stderr, (
        f"{kind}/{fp}：stderr 里没有该故障点标记；stderr={proc.stderr!r}"
    )
    return case


def _result(log: EventLog, run_id: str = RUN) -> dict:
    rows = [
        e.payload
        for e in log.read_run(run_id)
        if e.type == "tool.result" and e.payload["call_id"] == CALL
    ]
    assert len(rows) == 1, f"call {CALL} 应当恰好一条 result，实际 {len(rows)}"
    return rows[0]


def _receipts(log: EventLog, run_id: str = RUN) -> list[dict]:
    return [e.payload for e in log.read_run(run_id) if e.type == "receipt"]


def _undo(log: EventLog, store: SnapshotStore, ws: Path):
    """M1 库级撤销语义：阻断检查照走，直写要显式打开。"""
    return store.undo(
        RUN, Workspace(ws),
        guard=lambda: ensure_not_blocked(log, ws),
        allow_direct_write=True,
    )


def _check_applied(kind: str, log: EventLog, *, verify: bool = True) -> None:
    """applied 的三方一致：result / output / 凭据 / 磁盘。

    `verify=False` 只给"写前 blob 已被破坏"的用例：恢复本身不读 blob（它只用
    before/after 哈希判磁盘），凭据磁盘核对也仍然成立；只有"凭据引用的
    preimage 还能不能还原"这一层会红——那正是撤销该拒绝的原因。
    """
    result = _result(log)
    assert result["status"] == "succeeded", result
    assert result["effect_state"] == "applied", result
    out = result["output"]
    assert out["path"] == TARGET
    assert out["after_state"] == AFTER_SHA
    assert out["sha256"] == AFTER_SHA
    assert out["before_state"] == (ABSENT if kind == "create" else BEFORE_SHA)
    assert out["recovered"] is True

    receipts = _receipts(log)
    assert len(receipts) == 1, f"applied 必须恰好一张凭据，实际 {len(receipts)}"
    p = receipts[0]
    assert p["call_id"] == CALL
    assert p["effect_state"] == "applied"
    assert p["action"] == ACTION[kind]
    assert p["after_state"] == AFTER_SHA
    assert p["before_state"] == (ABSENT if kind == "create" else BEFORE_SHA)
    if verify:
        assert verify_receipts(log, RUN) == [], "凭据必须能被独立核对"


def _check_recovered_none_applied(log: EventLog) -> None:
    result = _result(log)
    assert result["status"] == "failed", result
    assert result["effect_state"] == "none_applied", result
    assert _receipts(log) == [], "未执行不得产凭据"


# ---------------------------------------------------------------- 4 × {新建,覆盖写}


@pytest.mark.parametrize("fp", FAILPOINTS)
@pytest.mark.parametrize("kind", ("create", "overwrite"))
def test_boundary_crash_create_overwrite(tmp_path, kind, fp):
    case = _crash(tmp_path, kind, fp)
    visible = fp in VISIBLE

    # ---- 断言 1：不变量（目标只能是写前或写后，逐字节） ----
    expected = AFTER_BYTES if visible else (None if kind == "create" else BEFORE_BYTES)
    assert case.target_bytes() == expected, f"{kind}/{fp}：出现第三种磁盘状态"
    assert (case.ws / "note.md").read_bytes() == NOTE_BYTES, "诱饵文件被写了"

    # ---- 断言 2：恢复判据 ----
    log, store = case.open()
    report = recover_run(log, RUN, case.ws, store)
    assert report.problems == [] and report.unresolved == [], report
    if visible:
        assert report.applied == [CALL] and report.none_applied == []
        _check_applied(kind, log)
        # 幂等：再跑一次不补事件、不重复发凭据、不再改文件
        n1 = len(log.read_run(RUN))
        recover_run(log, RUN, case.ws, store)
        assert len(log.read_run(RUN)) == n1, "第二次恢复又补了事件"
        assert len(_receipts(log)) == 1, "第二次恢复又发了一张凭据"
    else:
        assert report.none_applied == [CALL] and report.applied == []
        _check_recovered_none_applied(log)
    assert blocked_workspaces(log) == set()
    log.close()

    # ---- 断言 3：撤销可还原 ----
    log, store = case.open()
    undo = _undo(log, store, case.ws)
    if visible:
        # 副作用确实发生了 → 按 preimage 重建
        assert undo.ok, undo.problems
        if kind == "create":
            assert undo.deleted == [TARGET] and case.target_bytes() is None
        else:
            assert undo.restored == [TARGET] and case.target_bytes() == BEFORE_BYTES
        assert undo.entries and undo.entries[0]["root"] == str(case.ws)
    else:
        # 副作用没发生 → 撤销必须拒绝，且一个字节都不改（写前内容仍在）
        assert not undo.ok, f"{kind}/{fp}：没执行的写入不该被撤销"
        assert undo.conflicts, f"{kind}/{fp}：应当报冲突而不是静默还原"
        assert case.target_bytes() == expected, f"{kind}/{fp}：撤销动了没执行的写入"
        assert undo.entries == [], "拒绝的撤销不得留下任何动作记录"
    log.close()


# ---------------------------------------------------------------- 4 × 删除


@pytest.mark.parametrize("fp", FAILPOINTS)
def test_boundary_crash_delete(tmp_path, fp):
    """删除的四个边界都死在写前证据落盘期间：unlink 一定还没发生。

    因此本格证明的是"写前证据的持久化边界不会让删除变成半截状态"，以及
    "证据没落盘时撤销拒绝、文件保持写前内容"。副作用真实发生后的 applied +
    撤销重建在下一条用例（第二个子进程）里钉住。
    """
    case = _crash(tmp_path, "delete", fp)
    visible = fp in VISIBLE

    # ---- 断言 1：不变量 ----
    assert case.target_bytes() == BEFORE_BYTES, "删除边界上目标不该被动过"
    assert (case.ws / "note.md").read_bytes() == NOTE_BYTES

    # 清单：rename 前两个边界读不到这条证据；rename 之后证据完整可解析
    log, store = case.open()
    changes = store.load(RUN)
    if visible:
        assert len(changes) == 1
        assert changes[0].before_state == BEFORE_SHA
        assert changes[0].expected_after == ABSENT
    else:
        assert changes == [], "tmp 边界上的证据不该已经可见"

    # ---- 断言 2：恢复判据（文件仍在 = 未执行） ----
    report = recover_run(log, RUN, case.ws, store)
    assert report.problems == [] and report.unresolved == [], report
    assert report.none_applied == [CALL] and report.applied == []
    _check_recovered_none_applied(log)
    assert case.target_bytes() == BEFORE_BYTES
    log.close()

    # ---- 断言 3：撤销可还原 ----
    log, store = case.open()
    undo = _undo(log, store, case.ws)
    if visible:
        # 当前文件内容 == 写前内容 ≠ 证据里的写后状态 absent：撤销必须拒绝
        assert not undo.ok, "删除没发生，撤销不该把文件当成已删除"
        assert undo.conflicts, "应当报冲突而不是静默"
    else:
        assert not undo.ok
        assert undo.problems, "没有证据时必须明确拒绝"
    assert case.target_bytes() == BEFORE_BYTES, "撤销一个字节都不该动"
    assert undo.entries == []
    log.close()


@pytest.mark.parametrize("fp", ("after-rename", "after-dir-fsync"))
def test_delete_completed_after_durable_evidence_recovers_and_undoes(tmp_path, fp):
    """证据已持久 → 进程随后执行了删除 → 断电（真实可达状态）。

    这条路径是 issue #54 的核心：删除类动作的 applied 状态是"文件不存在"，
    恢复不能去读它（会抛 FileNotFoundError），也不能把它当成 0 字节文件。
    """
    case = _crash(tmp_path, "delete", fp, tag="-completed")
    # 证据已持久，进程继续走到 unlink 之后才断电
    assert case.manifest.is_file() and len(store_changes(case)) == 1
    case.target.unlink()

    log, store = case.open()
    report = recover_run(log, RUN, case.ws, store)
    assert report.problems == [] and report.unresolved == [], report
    assert report.applied == [CALL]

    # 恢复必须给出 applied + after_state=absent，且目标确实不存在
    result = _result(log)
    assert result["status"] == "succeeded"
    assert result["effect_state"] == "applied"
    out = result["output"]
    assert out["path"] == TARGET
    assert out["after_state"] == ABSENT
    assert out["deleted"] is True
    assert out["before_state"] == BEFORE_SHA
    assert not case.target.exists(), "恢复后目标必须不存在（不是 0 字节文件）"

    receipts = _receipts(log)
    assert len(receipts) == 1
    p = receipts[0]
    assert p["action"] == ACTION_DELETE
    assert p["after_state"] == ABSENT
    assert p["before_state"] == BEFORE_SHA
    assert verify_receipts(log, RUN) == [], "删除恢复的凭据也必须能独立核对"

    # 幂等：再恢复一次不补事件、不重复发凭据
    n1 = len(log.read_run(RUN))
    recover_run(log, RUN, case.ws, store)
    assert len(log.read_run(RUN)) == n1
    assert len(_receipts(log)) == 1
    log.close()

    # 撤销按 preimage 重建回原字节
    log, store = case.open()
    undo = _undo(log, store, case.ws)
    assert undo.ok, undo.problems
    assert undo.restored == [TARGET]
    assert case.target_bytes() == BEFORE_BYTES
    log.close()


def store_changes(case: Case):
    store = SnapshotStore(case.artifacts)
    return store.load(RUN)


# ---------------------------------------------------------------- 撕裂 / 丢尾


def test_torn_manifest_is_unknown_and_blocks(tmp_path):
    """清单被截断：恢复不能崩，也不能当"没写"；撤销必须拒绝。"""
    case = _crash(tmp_path, "create", "after-rename")
    raw = case.manifest.read_bytes()
    case.manifest.write_bytes(raw[: len(raw) // 2])
    assert case.target_bytes() == AFTER_BYTES

    log, store = case.open()
    report = recover_run(log, RUN, case.ws, store)
    assert report.unresolved == [CALL], "清单读不出来必须判 unknown"
    assert report.applied == [] and report.none_applied == []
    assert str(case.ws) in report.blocked_workspaces
    assert str(case.ws) in blocked_workspaces(log), "unknown 必须阻断后续写入"
    unknown = _result(log)
    assert unknown["effect_state"] == "unknown"
    assert _receipts(log) == [], "unknown 不得产凭据"

    # 幂等：已经结算过的调用不会因为清单还坏着就重复写事件
    n1 = len(log.read_run(RUN))
    again = recover_run(log, RUN, case.ws, store)
    assert again.unresolved == []
    assert len(log.read_run(RUN)) == n1
    log.close()

    # 撤销：类型化拒绝，零修改。先撞阻断（unknown 必须挡写入），
    # 人工解除阻断后再撞坏清单——两道门都拒绝，没有一条会静默放行。
    log, store = case.open()
    with pytest.raises(WorkspaceBlocked):
        _undo(log, store, case.ws)
    unblock(log, case.ws, reason="测试：人工核查后解除阻断，验证撤销侧对坏清单仍拒绝")
    with pytest.raises(SnapshotCorrupt):
        _undo(log, store, case.ws)
    assert case.target_bytes() == AFTER_BYTES
    log.close()


@pytest.mark.parametrize("damage", ("truncate", "flip"))
def test_torn_blob_refuses_undo_without_modification(tmp_path, damage):
    """写前 blob 截断 / 位翻转：恢复照常（它只看 before/after 哈希），
    但撤销必须拒绝——读不出 preimage 就不许把文件改成任何东西。"""
    case = _crash(tmp_path, "overwrite", "after-rename")
    blob = case.artifacts / "blobs" / BEFORE_SHA
    raw = blob.read_bytes()
    if damage == "truncate":
        blob.write_bytes(raw[:5])
    else:
        blob.write_bytes(bytes([raw[0] ^ 0xFF]) + raw[1:])

    log, store = case.open()
    report = recover_run(log, RUN, case.ws, store)
    assert report.applied == [CALL] and report.unresolved == []
    _check_applied("overwrite", log, verify=False)
    log.close()

    log, store = case.open()
    undo = _undo(log, store, case.ws)
    assert not undo.ok
    assert undo.problems, "blob 坏了必须明确拒绝"
    assert "blob" in " ".join(undo.problems), undo.problems
    assert case.target_bytes() == AFTER_BYTES, "拒绝的撤销一个字节都不许动"
    assert undo.entries == []
    log.close()


def test_torn_wal_tail_keeps_recovery_legal(tmp_path):
    """`events.db`-WAL 的**最后一个提交帧**被撕裂（模拟"存储撒谎"）。

    SQLite 的 WAL 语义是"断尾丢弃、保留已提交前缀"，所以最后一个事务整体丢
    失（这里恰好是写前 `tool.intent`），`run.start` 仍在。恢复必须：
    - 不崩；
    - 不凭空报 applied（它看不到那次调用）；
    - 磁盘只能是写前/写后之一；
    - 撤销不依赖事件日志，仍按 manifest 的 preimage 还原。

    边界：`synchronous=FULL` 下已提交事务本应持久；截断 WAL 模拟的是存储层
    违背 fsync 承诺。这个窗口里恢复"看不见"那次调用是事实源丢记录，不是恢复
    误判——**不能**因此宣称"掉电后一定不会漏"。
    """
    case = _crash(tmp_path, "overwrite", "after-rename")
    wal = case.db.parent / (case.db.name + "-wal")
    assert wal.is_file(), "崩溃后应当留下 WAL"
    raw = wal.read_bytes()
    wal.write_bytes(raw[:-100])  # 撕掉最后 100 字节：落在最后一个提交帧里

    log, store = case.open()
    types = [e.type for e in log.read_run(RUN)]
    assert types == ["run.start"], f"断尾语义变了：{types}"

    assert case.target_bytes() == AFTER_BYTES, "副作用已经落盘，文件必须是写后内容"
    report = recover_run(log, RUN, case.ws, store)
    assert report.unresolved == [] and report.problems == []
    assert report.applied == [] and report.none_applied == []
    assert _receipts(log) == [], "没有证据时不许凭空补凭据"

    undo = _undo(log, store, case.ws)
    assert undo.ok and case.target_bytes() == BEFORE_BYTES
    log.close()


def test_torn_wal_schema_loss_is_domain_error_not_empty_log(tmp_path):
    """#54 之外发现的第二个 bug：撕裂到 schema 首帧时，旧代码把历史洗成空库。

    复现（修复前）：`EventLog(db)` 打开成功，`read_run` 返回 `[]`，原 WAL 被
    丢弃、main db 被重新建成只有空 schema 的新库 —— 恢复据此报告"干净"，
    而磁盘上的副作用可能已经发生。修复后：文件存在且有内容却读不出 `events`
    表 → `EventLogCorrupt`（域错误），不再静默当空库。
    """
    case = _crash(tmp_path, "overwrite", "after-rename")
    wal = case.db.parent / (case.db.name + "-wal")
    raw = wal.read_bytes()
    wal.write_bytes(raw[: 32 + 24 + 100])  # 连 schema 首帧都不完整

    with pytest.raises(EventLogCorrupt) as excinfo:
        EventLog(case.db)
    message = str(excinfo.value)
    assert str(case.db) in message, "域错误必须点名是哪个库"
    assert "不要删除" in message, "域错误必须给出可行动的处置建议"
    # 修复前这里会"打开成功 + read_run()==[]"——那是最坏的一种回答。
    #
    # 注意：SQLite 在回放阶段就会丢弃"不可回放"的 WAL 文件，所以打开检查本身
    # 会消耗掉它。防线是"立刻判 corrupt 并拒绝继续"，不是"把 WAL 留着"。
    assert case.db.exists(), "主库文件不该被删"


def test_cli_recover_on_torn_wal_reports_domain_error(tmp_path):
    """CLI 边界：坏库 → 退出码 2、打印「错误：」、不抛栈、不给干净结论。"""
    case = _crash(tmp_path, "overwrite", "after-rename")
    wal = case.db.parent / (case.db.name + "-wal")
    raw = wal.read_bytes()
    wal.write_bytes(raw[: 32 + 24 + 100])

    env = dict(os.environ)
    env.pop("TIANSHU_FAILPOINT", None)
    env.pop("TIANSHU_FAILPOINT_PATH", None)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "tianshu", "recover",
         "--run-id", RUN, "--workspace", str(case.ws), "--db", str(case.db)],
        env=env, capture_output=True, timeout=120, **CHILD_TEXT,
    )
    assert proc.returncode != 0, f"坏库不该返回 0；stdout={proc.stdout!r}"
    assert "错误：" in proc.stderr, f"CLI 必须打印可读错误；stderr={proc.stderr!r}"
    assert "Traceback" not in proc.stderr, "CLI 边界不许漏出栈"
    assert "已确认执行" not in proc.stdout, "坏库不能给出干净结论"
    assert "无法核实" not in proc.stdout, "坏库不能给出干净结论"


def test_fresh_and_reopened_event_log_are_not_misjudged(tmp_path):
    """护栏：别把新库/第二连接误杀。

    - 0 字节文件 = 新库（建了文件还没写 schema 就崩）：允许建 schema；
    - 同一库第二条连接（daemon 的 control/driver 两条连接）在 schema 已存在
      时不得误报损坏。
    """
    # 0 字节现存文件：不能按"存在但没 schema"判 corrupt
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    log = EventLog(empty)
    log.append("r1", "run.start", {"task": "t", "workspace": str(tmp_path),
                                   "out": "x", "limits": {}, "tools": []},
               actor_kind="core")
    assert len(log.read_run("r1")) == 1

    # 同一库第二条连接（同一进程、非跨线程）不得误报
    second = EventLog(empty)
    assert [e.type for e in second.read_run("r1")] == ["run.start"]
    second.close()
    log.close()

    # 关掉再开（重启）也不得误报
    third = EventLog(empty)
    assert [e.type for e in third.read_run("r1")] == ["run.start"]
    third.close()
