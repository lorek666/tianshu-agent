"""未完成撤销的恢复侧接线（recovery ↔ undo_journal）。

T3（PR #66）把撤销变成"写前意图日志 + 崩溃后幂等续做"，但它只提供了库契约：
`undo_journal.outstanding(data_root)` / `resume(stack, run_id)`。产品代码里没有
任何地方把"有未完成的撤销"这件事变成**阻断**——恢复照样报告干净，写入照样能进。

这一组用例钉住接线后的语义：

1. `in_progress` 的 journal → 恢复报告写明、对应目录被阻断（日志 + 用户级权威根）；
2. 损坏的 journal → 同样阻断，且明确要求人工核对（不解成"没有撤销"）；
3. `completed` 但缺 `undo.completed` 事件的 journal → **不**阻断（文件已全部还原，
   只差一条幂等补写的事件，没有"改了一半"的风险）；
4. 阻断可被 `guard.unblock` 解除；只要 journal 还在，下一次恢复会重新挡住——
   journal 是事实源，阻断只是它的投影。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import write_exact as put_file

from tianshu import authority
from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import ensure_not_blocked, unblock
from tianshu.loop import build_stack
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.undo_journal import (
    ACTION_RESTORE,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    JournalItem,
    JournalStore,
)

RUN = "run_undo_me"


@pytest.fixture()
def env(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    put_file(ws / "a.md", "A\n")
    put_file(ws / "b.md", "B\n")
    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    build_stack(log, data_root=data_root)  # 建库/迁移（恢复要求 stack 可用）
    snapshots = SnapshotStore(data_root / "artifacts")
    snapshots.init_run(RUN)
    try:
        yield data_root, ws, log, snapshots
    finally:
        log.close()


def _items(root: str) -> tuple[JournalItem, ...]:
    return (
        JournalItem(
            root=root,
            path="a.md",
            call_id="c1",
            before_state="0" * 64,
            expected_after="1" * 64,
            action=ACTION_RESTORE,
        ),
        JournalItem(
            root=root,
            path="b.md",
            call_id="c2",
            before_state="absent",
            expected_after="2" * 64,
            action="delete",
        ),
    )


def _journal(data_root: Path) -> JournalStore:
    return JournalStore(data_root / "artifacts")


def _begin_in_progress(data_root: Path, root: str, *, done: int = 1) -> Path:
    store = _journal(data_root)
    journal = store.begin(RUN, control_run="ctrl_undo", items=_items(root))
    for index in range(done):
        journal = store.record_done(journal, index, journal.items[index].before_state)
    return store.path(RUN)


def _recover(data_root: Path, ws: Path, log: EventLog, snapshots: SnapshotStore):
    return recover_run(log, RUN, ws, snapshots, data_root=data_root)


def test_unfinished_undo_blocks_workspace(env):
    """in_progress 的撤销 → 恢复报告点名 + 目录阻断（日志与权威根都要有）。"""
    data_root, ws, log, snapshots = env
    journal_path = _begin_in_progress(data_root, str(ws.resolve()), done=1)
    assert journal_path.is_file()

    report = _recover(data_root, ws, log, snapshots)

    assert report.problems, "未完成的撤销必须在报告里说出来"
    assert any("撤销未完成（1/2" in problem for problem in report.problems), report.problems
    assert any(str(journal_path) in problem for problem in report.problems)

    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)
    assert authority.is_blocked(ws), "换数据根也绕不过去的那一侧也要挡住"

    blocked = [
        event.payload
        for event in log.read_run(RUN)
        if event.type == "recovery.blocked"
    ]
    assert len(blocked) == 1, blocked
    assert blocked[0]["workspace"] == str(ws.resolve())
    assert "撤销未完成" in blocked[0]["reason"]


def test_completed_journal_missing_event_does_not_block(env):
    """completed 只差补事件 → 不阻断（文件已全部还原，没有"改了一半"）。"""
    data_root, ws, log, snapshots = env
    store = _journal(data_root)
    journal = store.begin(RUN, control_run="ctrl_undo", items=_items(str(ws.resolve())))
    for index in range(len(journal.items)):
        journal = store.record_done(journal, index, journal.items[index].before_state)
    journal = store.complete(journal)
    assert journal.status == STATUS_COMPLETED

    report = _recover(data_root, ws, log, snapshots)

    assert not any("撤销" in problem for problem in report.problems), report.problems
    ensure_not_blocked(log, ws)  # 不抛


def test_corrupt_journal_blocks_and_asks_for_human(env):
    """journal 读不懂 → 阻断 + 要求人工核对；不能解成"没有撤销"。"""
    data_root, ws, log, snapshots = env
    store = _journal(data_root)
    path = store.path(RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps({"version": 1, "run_id": "wrong"}).encode("utf-8"))

    report = _recover(data_root, ws, log, snapshots)

    assert any("撤销意图日志损坏" in problem for problem in report.problems), report.problems
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)
    assert authority.is_blocked(ws)


def test_block_is_a_projection_of_the_journal(env):
    """解除阻断后只要 journal 还在，下一次恢复会重新挡住——事实源不是阻断标记。"""
    data_root, ws, log, snapshots = env
    _begin_in_progress(data_root, str(ws.resolve()), done=1)
    _recover(data_root, ws, log, snapshots)
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)

    unblock(log, ws, reason="人工核对")
    ensure_not_blocked(log, ws)  # 已解除

    _recover(data_root, ws, log, snapshots)
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)

    # journal 完成之后，恢复不再挡
    store = _journal(data_root)
    journal = store.load(RUN)
    assert journal is not None
    for index in range(len(journal.done), len(journal.items)):
        journal = store.record_done(journal, index, journal.items[index].before_state)
    store.complete(journal)
    unblock(log, ws, reason="撤销已续做完成")
    _recover(data_root, ws, log, snapshots)
    ensure_not_blocked(log, ws)


def test_in_progress_status_is_what_blocks(env):
    """边界：同一份 journal 从 in_progress 变 completed 后，阻断理由必须消失。"""
    data_root, ws, log, snapshots = env
    store = _journal(data_root)
    journal = store.begin(RUN, control_run="ctrl_undo", items=_items(str(ws.resolve())))
    assert journal.status == STATUS_IN_PROGRESS
    report = _recover(data_root, ws, log, snapshots)
    assert any("撤销未完成" in problem for problem in report.problems)
    unblock(log, ws, reason="核对")

    for index in range(len(journal.items)):
        journal = store.record_done(journal, index, journal.items[index].before_state)
    store.complete(journal)
    report = _recover(data_root, ws, log, snapshots)
    assert not any("撤销" in problem for problem in report.problems), report.problems
    ensure_not_blocked(log, ws)
