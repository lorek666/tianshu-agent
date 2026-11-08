"""DEBUG-M0M1 报告的回归用例。

每条都对应报告里复现出来的一个具体失效，防止以后被"优化"回去。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import ensure_not_blocked, is_blocked, unblock
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.tools import execute
from tianshu.workspace import Workspace


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ws(tmp_path: Path, files: dict[str, str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in files.items():
        (ws / name).write_text(content, encoding="utf-8")
    return ws


def _intent(log, run_id, call_id, tool_id, args, workspace):
    log.append(run_id, "run.start",
               {"task": "t", "workspace": str(workspace.resolve()),
                "out": "x", "limits": {}, "tools": []}, actor_kind="core")
    log.append(run_id, "tool.intent",
               {"call_id": call_id, "tool_id": tool_id, "args": args,
                "args_hash": _sha("args")}, actor_kind="core")


# ---------------------------------------------------------------- S1


def test_s1_existing_identical_file_is_not_treated_as_executed(tmp_path):
    """目标本来就存在且内容相同 —— 不能因此断定本次调用写过。"""
    ws = _ws(tmp_path, {"report.md": "同样的内容"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "create_report",
            {"name": "report.md", "content": "同样的内容"}, ws)
    # 没有写前证据：说明在写文件之前就中断了
    report = recover_run(log, "r1", ws, store)

    assert report.applied == [], "没有证据不得判为已执行"
    assert report.none_applied == ["c1"]
    assert not [e for e in log.read_run("r1") if e.type == "receipt"], \
        "不得凭空生成凭据"
    log.close()


def test_s1_modify_to_identical_content_is_not_applied(tmp_path):
    """写前写后内容一致：没有可观测副作用，既不声称成功也不阻断。"""
    ws = _ws(tmp_path, {"note.md": "不变"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "modify_text",
            {"path": "note.md", "content": "不变"}, ws)
    store.record_preimage("r1", "c1", "note.md",
                          before="不变".encode(), expected_after=_sha("不变"))

    report = recover_run(log, "r1", ws, store)
    assert report.applied == []
    assert report.unresolved == []
    assert report.none_applied == ["c1"]
    log.close()


def test_s1_deleted_target_is_unknown_not_none_applied(tmp_path):
    """修改类：目标消失了，不能断言"没执行"——可能是执行后被删掉。"""
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "modify_text",
            {"path": "note.md", "content": "新内容"}, ws)
    store.record_preimage("r1", "c1", "note.md",
                          before="旧内容".encode(), expected_after=_sha("新内容"))

    report = recover_run(log, "r1", ws, store)
    assert report.unresolved == ["c1"], "目标消失必须判 unknown"
    log.close()


def test_s1_no_fabricated_before_state(tmp_path):
    """恢复产出的 result 里，before_state 必须来自证据，不能编 absent。"""
    ws = _ws(tmp_path, {"note.md": "旧内容\n"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "modify_text",
            {"path": "note.md", "content": "新内容\n"}, ws)
    before = _sha("旧内容\n")
    store.record_preimage("r1", "c1", "note.md",
                          before="旧内容\n".encode(),
                          expected_after=_sha("新内容\n"))
    (ws / "note.md").write_text("新内容\n", encoding="utf-8")

    report = recover_run(log, "r1", ws, store)
    assert report.applied == ["c1"]
    result = [e for e in log.read_run("r1") if e.type == "tool.result"][0]
    assert result.payload["output"]["before_state"] == before
    log.close()


# ---------------------------------------------------------------- S2


def test_s2_blocking_is_enforced_at_dispatcher(tmp_path):
    """绕过 CLI 直接调 execute 也必须被拦住。"""
    ws = _ws(tmp_path, {"a.md": "内容"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "modify_text",
            {"path": "a.md", "content": "x"}, ws)
    store.record_preimage("r1", "c1", "a.md", b"\xe6\x97\xa7", _sha("x"))
    (ws / "a.md").write_text("第三种内容", encoding="utf-8")
    recover_run(log, "r1", ws, store)
    assert is_blocked(log, ws)

    from tianshu.tools import ToolContext

    ctx = ToolContext(log=log, run_id="r2", call_id="c2")
    with pytest.raises(WorkspaceBlocked):
        execute(Workspace(ws), "modify_text",
                {"path": "a.md", "content": "还想写"}, ctx)
    log.close()


def test_s2_write_tool_without_log_is_refused(tmp_path):
    ws = _ws(tmp_path, {"a.md": "内容"})
    with pytest.raises(Exception) as ei:
        execute(Workspace(ws), "modify_text", {"path": "a.md", "content": "x"})
    assert "ToolContext.log" in str(ei.value) or "log" in str(ei.value)


def test_s2_unblock_restores_writability(tmp_path):
    ws = _ws(tmp_path, {"a.md": "坏"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    _intent(log, "r1", "c1", "modify_text", {"path": "a.md", "content": "x"}, ws)
    store.record_preimage("r1", "c1", "a.md", b"\xe5\xa5\xbd", _sha("x"))
    (ws / "a.md").write_text("第三种", encoding="utf-8")
    recover_run(log, "r1", ws, store)

    assert is_blocked(log, ws)
    unblock(log, ws, reason="人工核查：未写入")
    assert not is_blocked(log, ws)
    ensure_not_blocked(log, ws)  # 不抛
    log.close()


# ---------------------------------------------------------------- S3 / S4


def test_s3_atomic_write_leaves_no_partial_file(tmp_path):
    """写入过程中若抛异常，原文件必须完好（不留半截）。"""
    from tianshu.fsutil import atomic_write_bytes

    target = tmp_path / "f.txt"
    target.write_bytes(b"original")
    with pytest.raises(RuntimeError):
        # 用不可序列化之外的方式模拟中断：直接让 replace 前抛错
        import tianshu.fsutil as fs

        original = fs.os.replace
        fs.os.replace = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            atomic_write_bytes(target, b"new content")
        finally:
            fs.os.replace = original

    assert target.read_bytes() == b"original", "必须还是原文件，不能是半截"
    assert not list(tmp_path.glob(".f.txt.*.tmp")), "临时文件要清理掉"


def test_s4_undo_aborts_when_blob_missing(tmp_path):
    """一个 blob 丢失，另一个文件也不能被还原——不留部分撤销。"""
    ws = _ws(tmp_path, {"a.md": "A原", "b.md": "B原"})
    store = SnapshotStore(tmp_path / "art")
    store.record_preimage("r1", "c1", "a.md", "A原".encode(), _sha("A改"))
    store.record_preimage("r1", "c2", "b.md", "B原".encode(), _sha("B改"))
    (ws / "a.md").write_text("A改", encoding="utf-8")
    (ws / "b.md").write_text("B改", encoding="utf-8")

    # 删掉 b 的写前 blob
    for c in store.load("r1"):
        if c.path == "b.md" and c.blob_ref:
            Path(c.blob_ref).unlink()

    report = store.undo("r1", Workspace(ws))
    assert not report.ok
    assert report.missing_blobs
    assert (ws / "a.md").read_text(encoding="utf-8") == "A改", \
        "预检不通过时，a 也不能被还原"


# ---------------------------------------------------------------- M3


def test_m3_provider_error_is_failure_not_noop(tmp_path):
    """provider 报错不能被当成"没事发生"的成功。"""
    ws = _ws(tmp_path, {"a.md": "内容"})
    log = EventLog(tmp_path / "e.db")
    result = run(
        task="t", workspace_dir=ws, out_name="r.md",
        model=ScriptedModel([ModelResponse("error", content="HTTP 500")]),
        log=log, limits=Limits(4, 1000),
    )
    assert result.outcome == "failed"
    assert "HTTP 500" in (result.error or "")
    log.close()


# ---------------------------------------------------------------- 事件原子性


def test_m1_result_and_receipt_are_atomic(tmp_path):
    """apply 时 result 与 receipt 必须成对出现。"""
    ws = _ws(tmp_path, {"a.md": "内容"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    result = run(
        task="t", workspace_dir=ws, out_name="r.md",
        model=ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "create_report", {"name": "r.md", "content": "产出"})]),
            ModelResponse("end_turn", content="ok"),
        ]),
        log=log, limits=Limits(4, 1000), snapshots=store,
    )
    events = log.read_run(result.run_id)
    applied = [e for e in events
               if e.type == "tool.result" and e.payload["effect_state"] == "applied"]
    receipts = [e for e in events if e.type == "receipt"]
    assert len(applied) == len(receipts) == 1
    log.close()
