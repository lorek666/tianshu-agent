"""DEBUG-M0M1 报告的回归用例。

每条都对应报告里复现出来的一个具体失效，防止以后被"优化"回去。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from support import tool_capable_profile

from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import (
    blocked_workspaces,
    ensure_not_blocked,
    is_blocked,
    unblock,
)
from tianshu.loop import Limits, run
from tianshu.model import ModelError, ModelResponse, ScriptedModel, ToolCall
from tianshu.recovery import recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.tools import execute
from tianshu.workspace import Workspace


def write_exact(path, text: str) -> None:
    """按字节落盘：Windows 的文本模式会把 \\n 翻成 \\r\\n，
    而恢复/证据算的是 sha256(原始字节)。"""
    path.write_bytes(text.encode("utf-8"))


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ws(tmp_path: Path, files: dict[str, str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in files.items():
        write_exact(ws / name, content)
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
    store.init_run("r1")  # 清单存在但没有该调用条目 → 确定未进入写入
    report = recover_run(log, "r1", ws, store)

    assert report.applied == [], "没有证据不得判为已执行"
    assert report.none_applied == ["c1"]
    assert not [e for e in log.read_run("r1") if e.type == "receipt"], \
        "不得凭空生成凭据"
    log.close()


def test_r1_missing_manifest_is_unknown_not_none_applied(tmp_path):
    """清单整个缺失是异常状态，不能据此断言「从未写入」。"""
    ws = _ws(tmp_path, {"note.md": "旧"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "modify_text",
            {"path": "note.md", "content": "新"}, ws)
    # 故意不调 init_run，模拟清单被清理/损坏
    report = recover_run(log, "r1", ws, store)

    assert report.unresolved == ["c1"], "清单缺失必须判 unknown"
    assert str(ws.resolve()) in blocked_workspaces(log), "并且要阻断"
    log.close()


def test_s1_modify_to_identical_content_is_not_applied(tmp_path):
    """写前写后内容一致：没有可观测副作用，既不声称成功也不阻断。"""
    ws = _ws(tmp_path, {"note.md": "不变"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    _intent(log, "r1", "c1", "modify_text",
            {"path": "note.md", "content": "不变"}, ws)
    store.init_run("r1")
    store.record_preimage("r1", "c1", "note.md",
                          before="不变".encode(), expected_after=_sha("不变"), root=ws)

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
    store.init_run("r1")
    store.record_preimage("r1", "c1", "note.md",
                          before="旧内容".encode(), expected_after=_sha("新内容"), root=ws)

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
    store.init_run("r1")
    before = _sha("旧内容\n")
    store.record_preimage("r1", "c1", "note.md",
                          before="旧内容\n".encode(),
                          expected_after=_sha("新内容\n"), root=ws)
    write_exact(ws / "note.md", "新内容\n")

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
    store.init_run("r1")
    store.record_preimage("r1", "c1", "a.md", b"\xe6\x97\xa7", _sha("x"), root=ws)
    write_exact(ws / "a.md", "第三种内容")
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
    store.init_run("r1")
    store.record_preimage("r1", "c1", "a.md", b"\xe5\xa5\xbd", _sha("x"), root=ws)
    write_exact(ws / "a.md", "第三种")
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
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    store.record_preimage("r1", "c1", "a.md", "A原".encode(), _sha("A改"), root=ws)
    store.record_preimage("r1", "c2", "b.md", "B原".encode(), _sha("B改"), root=ws)
    write_exact(ws / "a.md", "A改")
    write_exact(ws / "b.md", "B改")

    # 删掉 b 的写前 blob
    for c in store.load("r1"):
        if c.path == "b.md" and c.blob_ref:
            Path(c.blob_ref).unlink()

    report = store.undo("r1", Workspace(ws), guard=lambda: ensure_not_blocked(log, ws),
               allow_direct_write=True)
    assert not report.ok
    assert report.missing_blobs
    assert (ws / "a.md").read_text(encoding="utf-8") == "A改", \
        "预检不通过时，a 也不能被还原"


# ---------------------------------------------------------------- M3


class _FailingModel:
    """传输层失败的替身：抛 ModelError，而不是伪造一个 finish_reason。"""

    def capability_profile(self):
        # F9a：准入要求每个模型如实声明能力（没声明会被拒绝，不是"先试试看"）。
        return tool_capable_profile("test-double", "_FailingModel")

    def complete(self, messages, tools):  # noqa: ANN001, ARG002
        raise ModelError.from_http(500, "upstream exploded")


def test_m3_provider_error_is_failure_not_noop(tmp_path):
    """provider 报错不能被当成"没事发生"的成功。"""
    ws = _ws(tmp_path, {"a.md": "内容"})
    log = EventLog(tmp_path / "e.db")
    result = run(
        task="t", workspace_dir=ws, out_name="r.md",
        model=_FailingModel(), log=log,
        # F9a 起 5xx 是可重试错误：3 次尝试都会失败。退避置 0 让用例不睡觉
        # （退避序列本身由 test_capability 用注入的 sleeper 验）。
        limits=Limits(4, 1000, retry_backoff_ms=0),
    )
    assert result.outcome == "failed"
    assert "HTTP 500" in (result.error or "")
    assert "3 次都失败" in (result.error or ""), "重试耗尽必须说清尝试次数"
    # 错误类别要落到事件里：事后才能判断该重试还是该换 key。
    # 每次 attempt 一条：3 次尝试 = 3 条，这是审计要的粒度。
    errors = [e for e in log.read_run(result.run_id) if e.type == "model.error"]
    assert len(errors) == 3
    assert [e.payload["attempt"] for e in errors] == [1, 2, 3]
    assert "kind=http_status" in errors[0].payload["error"]
    assert errors[0].payload["retryable"] is True
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


def test_startup_crash_before_manifest_is_safe_to_initialize(tmp_path):
    """run.start 后还没有 tool intent，崩溃可安全补建空清单，无需永久阻断。"""
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    log.append("r1", "run.start", {"task": "t", "workspace": str(ws), "out": "x"})

    report = recover_run(log, "r1", ws, store)
    assert report.none_applied == []
    assert report.unresolved == []
    assert store.has_manifest("r1")
    log.close()


# ---------------------------------------------------------------- R2 / R3


def test_r2_undo_refused_on_blocked_workspace(tmp_path):
    """撤销也是写操作：workspace 被阻断时不得执行。"""
    ws = _ws(tmp_path, {"a.md": "原"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    store.init_run("r1")
    store.record_preimage("r1", "c1", "a.md", "原".encode(), _sha("新"), root=ws)
    write_exact(ws / "a.md", "新")

    # 制造阻断
    log.append("r1", "recovery.blocked",
               {"workspace": str(ws.resolve()), "call_ids": ["c9"],
                "reason": "test"}, actor_kind="system")

    with pytest.raises(WorkspaceBlocked):
        store.undo("r1", Workspace(ws),
                   guard=lambda: ensure_not_blocked(log, ws),
                   allow_direct_write=True)
    assert (ws / "a.md").read_text(encoding="utf-8") == "新", "被拒时不得修改"
    log.close()


def test_r3_create_then_modify_same_path_can_be_undone(tmp_path):
    """同一 run 里先 create 后 modify 同一文件，撤销要能回到最初状态。"""
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    store.init_run("r1")

    # c1: create a.md = "v1"
    store.record_preimage("r1", "c1", "a.md", None, _sha("v1"), root=ws)
    write_exact(ws / "a.md", "v1")
    # c2: modify a.md -> "v2"
    store.record_preimage("r1", "c2", "a.md", b"v1", _sha("v2"), root=ws)
    write_exact(ws / "a.md", "v2")

    report = store.undo("r1", Workspace(ws), guard=lambda: ensure_not_blocked(log, ws),
               allow_direct_write=True)
    assert report.ok, report.problems
    assert not (ws / "a.md").exists(), "回到最初状态：文件不存在"


def test_r3_undo_appends_audit_event(tmp_path):
    """撤销必须留痕，否则旧 applied 记录永远看起来成立。

    M2 之后撤销要人工批准：不加 --approve 时只预演（退出码 3，什么都不改），
    加了才真正删除并留下 undo.completed。
    """
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "artifacts")
    store.init_run("r1")
    store.record_preimage("r1", "c1", "a.md", None, _sha("v1"), root=ws)
    write_exact(ws / "a.md", "v1")

    from tianshu.cli import main

    db = str(tmp_path / "e.db")
    assert main(["undo", "--run-id", "r1", "--workspace", str(ws),
                 "--db", db]) == 3
    assert (ws / "a.md").exists(), "未批准时一个字节都不能动"

    assert main(["undo", "--run-id", "r1", "--workspace", str(ws),
                 "--db", db, "--approve"]) == 0
    assert not (ws / "a.md").exists()

    types = [e.type for e in log.read_run("r1")]
    assert "undo.completed" in types
    log.close()


def test_r4_blocked_tool_makes_run_fail_not_noop(tmp_path):
    """阻断不能被吞成 noop：任务必须显式失败。"""
    ws = _ws(tmp_path, {"a.md": "内容"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")

    log.append("r0", "recovery.blocked",
               {"workspace": str(ws.resolve()), "call_ids": ["x"],
                "reason": "test"}, actor_kind="system")

    result = run(
        task="t", workspace_dir=ws, out_name="r.md",
        model=ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "create_report", {"name": "r.md", "content": "x"})]),
            ModelResponse("end_turn", content="ok"),
        ]),
        log=log, limits=Limits(4, 1000), snapshots=store,
    )

    assert result.outcome == "failed", f"应为 failed，实际 {result.outcome}"
    assert "阻断" in (result.error or "")
    assert not (ws / "r.md").exists()
    assert result.effect_state == "none_applied"
    assert blocked_workspaces(log) == {str(ws.resolve())}, "原有阻断必须保留"
    log.close()


def test_m1_large_payload_is_durable_before_reference_commit(tmp_path):
    from tianshu.events import MAX_INLINE_BYTES

    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    log.append("r1", "model.result", {"content": "x" * MAX_INLINE_BYTES})
    event = log.read_run("r1")[0]
    assert event.payload["content"] == "x" * MAX_INLINE_BYTES
    refs = [row[0] for row in log._conn.execute(
        "SELECT payload_ref FROM events WHERE payload_ref IS NOT NULL"
    )]
    assert len(refs) == 1 and Path(refs[0]).is_file()
    log.close()


def test_m3_truncated_finish_reason_is_failure(tmp_path):
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "e.db")
    result = run(
        task="t", workspace_dir=ws, out_name="x",
        model=ScriptedModel([ModelResponse("limit_output_tokens", content="truncated")]),
        log=log,
    )
    assert result.outcome == "failed"
    log.close()


def test_undo_requires_guard_at_library_boundary(tmp_path):
    ws = _ws(tmp_path, {"a.md": "agent"})
    store = SnapshotStore(tmp_path / "art")
    store.init_run("r1")
    store.record_preimage("r1", "c1", "a.md", b"original", _sha("agent"), root=ws)

    with pytest.raises(Exception, match="guard"):
        store.undo("r1", Workspace(ws))
    assert (ws / "a.md").read_text() == "agent"


def test_three_writes_undo_to_original_and_middle_conflict_is_atomic(tmp_path):
    for corrupt_call in (None, "c2", "c3"):
        case = tmp_path / str(corrupt_call)
        ws = case / "ws"
        ws.mkdir(parents=True)
        log = EventLog(case / "e.db")
        store = SnapshotStore(case / "art")
        store.init_run("r1")
        for cid, before, after in (
            ("c1", "v0", "v1"), ("c2", "v1", "v2"), ("c3", "v2", "v3")
        ):
            store.record_preimage("r1", cid, "note.md", before.encode(), _sha(after), root=ws)
            write_exact(ws / "note.md", after)
        if corrupt_call is not None:
            manifest = store.manifest_dir / "r1.json"
            import json

            payload = json.loads(manifest.read_text())
            for change in payload["changes"]:
                if change["call_id"] == corrupt_call:
                    change["expected_after"] = _sha("unexpected")
            manifest.write_text(json.dumps(payload))

        report = store.undo(
            "r1",
            Workspace(ws),
            guard=lambda log=log, ws=ws: ensure_not_blocked(log, ws),
            allow_direct_write=True,
        )
        if corrupt_call is None:
            assert report.ok
            assert (ws / "note.md").read_text() == "v0"
        else:
            assert not report.ok
            assert report.conflicts
            assert report.restored == [] and report.deleted == []
            assert (ws / "note.md").read_text() == "v3"
        log.close()


def test_undo_rejects_corrupt_preimage_before_touching_any_file(tmp_path):
    ws = _ws(tmp_path, {"a.md": "A-after", "b.md": "B-after"})
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "art")
    store.init_run("r1")
    store.record_preimage("r1", "c1", "a.md", b"A-before", _sha("A-after"), root=ws)
    store.record_preimage("r1", "c2", "b.md", b"B-before", _sha("B-after"), root=ws)
    for change in store.load("r1"):
        if change.path == "b.md":
            Path(change.blob_ref).write_bytes(b"corrupt")

    report = store.undo("r1", Workspace(ws), guard=lambda: ensure_not_blocked(log, ws),
               allow_direct_write=True)
    assert not report.ok
    assert report.restored == [] and report.deleted == []
    assert (ws / "a.md").read_text() == "A-after"
    assert (ws / "b.md").read_text() == "B-after"
    log.close()
