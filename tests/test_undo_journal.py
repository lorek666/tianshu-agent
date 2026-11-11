"""T3 验收：跨多文件撤销的事务性（写前意图日志 + 崩溃后幂等续做）。

要证明的四件事：
1. 撤销**动手前**就把完整计划原子落盘；每完成一个动作原子重写；
2. 中途 `os._exit` 硬退后，重启能识别 `in_progress`、`done` 项保持，
   按断点**续做**（不是从头再来），最终三份文件都回到写前字节，
   `undo.completed` 恰好一条；
3. journal 损坏 / journal 与磁盘冲突 / 完成事件与 journal 矛盾 → 拒绝续做、
   零修改（不猜）；
4. journal 已完成但完成事件缺失 → 幂等补写事件，不动文件、不重复。

崩溃注入用真子进程 + `TIANSHU_FAILPOINT`（`crashpoint.failpoint` 是
`os._exit(77)`），父进程只做只读断言与续做。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tianshu import undo_journal
from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import ensure_not_blocked
from tianshu.loop import build_stack
from tianshu.snapshot import SnapshotStore
from tianshu.undo import UndoRefused
from tianshu.workspace import Workspace

RUN = "r1"
BEFORE = {"a.md": "A 原内容\n", "b.md": "B 原内容\n", "c.md": "C 原内容\n"}
AFTER = {"a.md": "A 被 Agent 改过\n", "b.md": "B 被 Agent 改过\n", "c.md": "C 被 Agent 改过\n"}
#: 调用顺序：c1→a, c2→b, c3→c。撤销执行顺序是**逆序**：先 c3，再 c2，最后 c1。
CALLS = [("c1", "a.md"), ("c2", "b.md"), ("c3", "c.md")]

# 子进程入口：用公开 API 装配 stack 后执行撤销；故障点命中时 os._exit(77)。
_CRASH_CHILD = """
import sys, pathlib
sys.path.insert(0, {src!r})
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.undo import apply_undo

stack = build_stack(EventLog(pathlib.Path({db!r})))
outcome = apply_undo(stack, {run!r})
print("UNEXPECTED_OK", outcome.ok, outcome.restored, outcome.deleted)
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_map(ws: Path) -> dict[str, bytes]:
    return {name: (ws / name).read_bytes() for name in sorted(BEFORE)}


def _prepare(tmp_path: Path) -> tuple[Path, EventLog, SnapshotStore]:
    """造一个「Agent 改过 3 个文件」的 run 快照（写前内容 + 写后落盘）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, text in BEFORE.items():
        (ws / name).write_bytes(text.encode("utf-8"))
    log = EventLog(tmp_path / "events.db")
    log.append(
        RUN,
        "run.start",
        {
            "task": "改三个文件",
            "workspace": str(ws.resolve()),
            "out": "report.md",
            "limits": {},
            "tools": [],
        },
        actor_kind="core",
        legacy_write=True,
    )
    store = SnapshotStore(tmp_path / "artifacts")
    for call_id, name in CALLS:
        store.record_preimage(
            RUN,
            call_id,
            name,
            BEFORE[name].encode("utf-8"),
            _sha(AFTER[name]),
            root=ws,
        )
        (ws / name).write_bytes(AFTER[name].encode("utf-8"))
    return ws, log, store


def _spawn_crash(tmp_path: Path, failpoint: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("TIANSHU_FAILPOINT", None)
    env["TIANSHU_FAILPOINT"] = failpoint
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = _CRASH_CHILD.format(src=src, db=str(tmp_path / "events.db"), run=RUN)
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _assert_crashed(proc: subprocess.CompletedProcess, failpoint: str) -> None:
    assert proc.returncode == 77, f"未在 {failpoint} 硬退：rc={proc.returncode}\n{proc.stderr}"
    assert f"[failpoint] {failpoint}" in proc.stderr, proc.stderr


def _journal_path(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "undo" / f"{RUN}.json"


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    return log, build_stack(log)


def _events_of(log: EventLog, event_type: str) -> list:
    return [e for e in log.read_run(RUN) if e.type == event_type]


def _blocked_event(log: EventLog, ws: Path, reason: str) -> None:
    """模拟 recover 在看到未完成撤销后写阻断（recover 接线属 lead，不在本分支）。"""
    log.append(
        RUN,
        "recovery.blocked",
        {"workspace": str(ws.resolve()), "call_ids": [], "reason": reason},
        actor_kind="system",
    )


def _unblocked_event(log: EventLog, ws: Path) -> None:
    log.append(
        RUN,
        "recovery.unblocked",
        {"workspace": str(ws.resolve()), "reason": "undo resumed"},
        actor_kind="system",
    )


# ---------------------------------------------------------------- 崩溃后续做


def test_crash_between_actions_resumes_from_breakpoint(tmp_path):
    """3 文件撤销：第 2 个动作前硬退 → 重启识别 in_progress → 续做完成。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_action")
    _assert_crashed(crash, "after_undo_action")

    # journal：完整计划 3 项、已完成 1 项（c3），状态 in_progress。
    journal = undo_journal.read(tmp_path, RUN)
    assert journal is not None
    assert journal.status == undo_journal.STATUS_IN_PROGRESS
    assert [item.call_id for item in journal.items] == ["c3", "c2", "c1"]
    assert len(journal.done) == 1
    assert journal.done[0].item.call_id == "c3"
    assert journal.done[0].post_state == _sha(BEFORE["c.md"]), "done 项要记写前状态"
    assert journal.control_run, "journal 必须记录控制 Run"

    # done 项保持：c.md 已还原，a/b 还是 Agent 写后的内容。
    assert (ws / "c.md").read_bytes() == BEFORE["c.md"].encode("utf-8")
    assert (ws / "a.md").read_bytes() == AFTER["a.md"].encode("utf-8")
    assert (ws / "b.md").read_bytes() == AFTER["b.md"].encode("utf-8")

    # outstanding：recover 据此阻断/报告。
    found = undo_journal.outstanding(tmp_path)
    assert len(found) == 1
    assert found[0].run_id == RUN
    assert found[0].status == undo_journal.STATUS_IN_PROGRESS
    assert found[0].done_count == 1 and found[0].total == 3
    assert found[0].roots == (str(ws.resolve()),)
    assert found[0].resumable is True

    # 模拟 recover：看到未完成撤销 → 阻断工作区；此时续做必须被守卫拒绝。
    _blocked_event(log, ws, "未完成的撤销")
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)
    files_before_retry = _file_map(ws)
    with pytest.raises(WorkspaceBlocked):
        _resume(tmp_path)
    assert _file_map(ws) == files_before_retry, "被阻断时续做不得改一个字节"
    assert not _events_of(log, "undo.completed"), "被阻断时不得补完成事件"

    # 解除阻断后续做：从断点继续，绝不从头再来（c.md 不会再被写）。
    _unblocked_event(log, ws)
    ensure_not_blocked(log, ws)
    outcome = _resume(tmp_path)
    assert outcome.ok, outcome.problems
    for name in BEFORE:
        assert (ws / name).read_bytes() == BEFORE[name].encode("utf-8"), name

    journal = undo_journal.read(tmp_path, RUN)
    assert journal is not None and journal.completed
    assert len(journal.done) == 3
    assert len(_events_of(log, "undo.completed")) == 1, "完成事件恰好一条"
    assert undo_journal.outstanding(tmp_path, log=log) == []

    # 再续做一次：幂等，不补第二条事件、不再改文件。
    files_done = _file_map(ws)
    again = _resume(tmp_path)
    assert again.ok
    assert _file_map(ws) == files_done
    assert len(_events_of(log, "undo.completed")) == 1, "重复续做不得补第二条事件"
    log.close()


def _resume(tmp_path: Path, run_id: str = RUN):
    """用新的进程身份续做（与真实重启同构），结束后关掉事件连接。"""
    log, stack = _stack(tmp_path)
    try:
        return undo_journal.resume(stack, run_id)
    finally:
        stack.log.close()


# ---------------------------------------------------------------- 完成事件缺失


def test_completed_journal_missing_event_is_backfilled_once(tmp_path):
    """journal 已完成、事件缺失 → 幂等补写；不动文件、不重复。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_journal_complete")
    _assert_crashed(crash, "after_undo_journal_complete")

    # 动作全部完成、journal 已 completed，但事件还没来得及写。
    assert all(
        (ws / name).read_bytes() == BEFORE[name].encode("utf-8") for name in BEFORE
    )
    journal = undo_journal.read(tmp_path, RUN)
    assert journal is not None and journal.completed
    assert len(journal.done) == 3
    assert _events_of(log, "undo.completed") == []
    found = undo_journal.outstanding(tmp_path, log=log)
    assert len(found) == 1 and found[0].needs_completion_event is True

    outcome = _resume(tmp_path)
    assert outcome.ok, outcome.problems
    events = _events_of(log, "undo.completed")
    assert len(events) == 1, "完成事件只补一条"
    payload = events[0].payload
    assert payload["control_run"] == journal.control_run
    assert sorted(payload["restored"]) == ["a.md", "b.md", "c.md"]
    assert payload["deleted"] == []
    assert undo_journal.outstanding(tmp_path, log=log) == []

    # 再跑一次：事件不重复、文件不变。
    files_done = _file_map(ws)
    again = _resume(tmp_path)
    assert again.ok
    assert _file_map(ws) == files_done
    assert len(_events_of(log, "undo.completed")) == 1
    log.close()


# ---------------------------------------------------------------- 损坏 / 冲突


def test_corrupt_journal_refuses_resume_with_zero_modification(tmp_path):
    """journal 读不懂 → 拒绝续做（→ recover 阻断），不猜、零修改。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_action")
    _assert_crashed(crash, "after_undo_action")
    files_at_crash = _file_map(ws)

    path = _journal_path(tmp_path)
    path.write_bytes(b"{not json")
    with pytest.raises(undo_journal.UndoJournalCorrupt):
        undo_journal.read(tmp_path, RUN)

    found = undo_journal.outstanding(tmp_path)
    assert len(found) == 1
    assert found[0].corrupt is True
    assert found[0].resumable is False
    assert found[0].run_id == RUN
    assert found[0].journal_path == path

    # 续做入口拒绝（UndoRefused，code=2），且一个字节都不动。
    with pytest.raises(UndoRefused) as refused:
        _resume(tmp_path)
    assert refused.value.code == 2
    assert any("日志" in line for line in refused.value.lines)
    assert _file_map(ws) == files_at_crash, "损坏 journal 下不得改任何文件"
    assert path.read_bytes() == b"{not json", "拒绝续做不得改写损坏的证据"
    assert not _events_of(log, "undo.completed")

    # 库入口同样 fail-closed：类型化错误，先读后写。
    direct = SnapshotStore(tmp_path / "artifacts")
    with pytest.raises(undo_journal.UndoJournalCorrupt):
        direct.undo(RUN, Workspace(ws), guard=lambda: None, allow_direct_write=True)
    assert _file_map(ws) == files_at_crash

    # 模拟 recover 对损坏 journal 的处置：阻断工作区，后续写入被拒。
    _blocked_event(log, ws, "撤销日志损坏")
    with pytest.raises(WorkspaceBlocked):
        ensure_not_blocked(log, ws)
    log.close()


def test_conflict_on_done_item_refuses_resume(tmp_path):
    """done 项被改过（磁盘 ≠ post_state）→ 拒绝续做、零修改。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_action")
    _assert_crashed(crash, "after_undo_action")

    # 已完成的是 c.md；用户后来手改了它。
    user_text = "用户后来又改的 C\n"
    (ws / "c.md").write_bytes(user_text.encode("utf-8"))
    files_now = _file_map(ws)

    with pytest.raises(UndoRefused) as refused:
        _resume(tmp_path)
    assert refused.value.code == 2
    assert any("冲突" in line for line in refused.value.lines + [str(refused.value)])
    assert _file_map(ws) == files_now, "冲突时必须零修改"
    assert not _events_of(log, "undo.completed")
    assert undo_journal.read(tmp_path, RUN).status == undo_journal.STATUS_IN_PROGRESS
    log.close()


def test_conflict_on_remaining_item_refuses_resume(tmp_path):
    """剩余项被改过（磁盘既非写后也非写前）→ 拒绝续做、零修改。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_action")
    _assert_crashed(crash, "after_undo_action")

    # 剩余项 b.md 被用户改成了第三种内容。
    (ws / "b.md").write_bytes("用户插进来的 B\n".encode())
    files_now = _file_map(ws)

    with pytest.raises(UndoRefused) as refused:
        _resume(tmp_path)
    assert refused.value.code == 2
    assert _file_map(ws) == files_now, "冲突时必须零修改（连已完成项也不许重写）"
    assert _events_of(log, "undo.completed") == []
    log.close()


def test_completed_journal_then_disk_changed_refuses_backfill(tmp_path):
    """journal 已完成但磁盘又被改过 → 拒绝补写完成事件，零修改。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_journal_complete")
    _assert_crashed(crash, "after_undo_journal_complete")

    (ws / "a.md").write_bytes("撤销之后用户又改的 A\n".encode())
    files_now = _file_map(ws)

    with pytest.raises(UndoRefused) as refused:
        _resume(tmp_path)
    assert refused.value.code == 2
    assert any("已完成" in line for line in refused.value.lines)
    assert _file_map(ws) == files_now, "拒绝时不得改文件"
    assert _events_of(log, "undo.completed") == [], "一致性不成立时不得补假事件"
    log.close()


def test_completed_event_without_completed_journal_is_refused(tmp_path):
    """事件说完成了、journal 还没完成 → 矛盾，按损坏拒绝，不猜。"""
    ws, log, store = _prepare(tmp_path)
    crash = _spawn_crash(tmp_path, "after_undo_action")
    _assert_crashed(crash, "after_undo_action")

    # 人为制造"完成事件存在但 journal 仍 in_progress"的矛盾证据。
    log.append(
        RUN,
        "undo.completed",
        {
            "run_id": RUN,
            "restored": ["a.md", "b.md", "c.md"],
            "deleted": [],
            "entries": [],
            "control_run": "",
        },
        actor_kind="human",
    )
    files_now = _file_map(ws)
    with pytest.raises(UndoRefused) as refused:
        _resume(tmp_path)
    assert refused.value.code == 2
    assert any("事件" in line for line in refused.value.lines)
    assert _file_map(ws) == files_now
    log.close()
