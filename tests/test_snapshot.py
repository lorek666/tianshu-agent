"""M1-PR2 验收：写前快照与冲突安全撤销。

要证明三件事：
1. 改之前真的存了原内容（不是改完再存）
2. 撤销能把文件还原成原样
3. 用户改过之后撤销会报冲突，且**一个字节都不动**

第 3 条是重点：静默覆盖用户的手动修改，比不能撤销更糟。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import receipts_of
from tianshu.snapshot import ABSENT, SnapshotStore
from tianshu.workspace import Workspace


def setup(tmp_path, files: dict[str, str]):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    for name, content in files.items():
        p = ws_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    store = SnapshotStore(tmp_path / "artifacts")
    return ws_dir, store


def run_with(tmp_path, ws_dir, store, responses):
    """跑到结束；中途出现的覆盖类审批用本地人工身份批准后 resume。

    M2 起 `modify_text`（整文件覆盖）按"删除类"处理，必须有批准才执行。
    这里把"人批准"这一步做掉，之后的快照/撤销语义与 M1 完全一致。
    """
    from support import approve_pending, engine_for, stack_for

    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel(responses)
    limits = Limits(8, 20_000)
    result = run(
        task="改文件",
        workspace_dir=ws_dir,
        out_name="summary.md",
        model=model,
        log=log,
        limits=limits,
        snapshots=store,
    )
    while result.state == "waiting_approval":
        stack = stack_for(log, snapshots=store)
        approved = approve_pending(stack, result.run_id)
        assert approved, "等待审批却找不到待批请求"
        result = engine_for(stack, model, limits=limits).resume_run(result.run_id)
    return result, log


def undo_with(log, run_id, ws_dir):
    """所有调用方都必须显式通过同一执行侧阻断检查。"""
    from tianshu.guard import ensure_not_blocked

    # 这些是 M1 语义的库级用例：直写分支需要显式开关。
    # 走授权派发的路径由 tests/test_dispatch.py 覆盖。
    return SnapshotStore(log._db_path.parent / "artifacts").undo(
        run_id,
        Workspace(ws_dir),
        guard=lambda: ensure_not_blocked(log, ws_dir),
        allow_direct_write=True,
    )


ORIGINAL = "原始内容\n"


def test_modify_captures_preimage_and_undo_restores(tmp_path):
    ws_dir, store = setup(tmp_path, {"note.md": ORIGINAL})
    result, log = run_with(
        tmp_path, ws_dir, store,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "modify_text", {"path": "note.md", "content": "改后的内容\n"})]),
            ModelResponse("end_turn", content="改完"),
        ],
    )

    assert (ws_dir / "note.md").read_text(encoding="utf-8") == "改后的内容\n"

    changes = store.load(result.run_id)
    assert len(changes) == 1
    c = changes[0]
    assert c.before_state == hashlib.sha256(ORIGINAL.encode()).hexdigest(), \
        "写前状态必须是原文哈希，不是 absent"
    assert c.expected_after == hashlib.sha256("改后的内容\n".encode()).hexdigest()
    assert c.blob_ref and Path(c.blob_ref).exists(), "写前内容必须真的落盘了"

    # 凭据的 before_state 也必须是真哈希
    r = receipts_of(log, result.run_id)[0]
    assert r["before_state"] == c.before_state, "凭据必须记录写前状态"
    assert r["effect_state"] == "applied"

    undo = undo_with(log, result.run_id, ws_dir)
    assert undo.ok and undo.restored == ["note.md"]
    assert (ws_dir / "note.md").read_text(encoding="utf-8") == ORIGINAL
    log.close()


def test_undo_deletes_newly_created_file(tmp_path):
    ws_dir, store = setup(tmp_path, {"a.md": "内容"})
    result, log = run_with(
        tmp_path, ws_dir, store,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": "out.md", "content": "产出"})]),
            ModelResponse("end_turn", content="完成"),
        ],
    )
    assert (ws_dir / "out.md").exists()
    assert store.load(result.run_id)[0].before_state == ABSENT

    undo = undo_with(log, result.run_id, ws_dir)
    assert undo.ok and undo.deleted == ["out.md"]
    assert not (ws_dir / "out.md").exists(), "新建的文件撤销后应消失"
    log.close()


def test_undo_refuses_when_user_modified_after(tmp_path):
    """核心用例：用户后来手动改过，撤销必须报冲突且一个字节都不动。"""
    ws_dir, store = setup(tmp_path, {"note.md": ORIGINAL})
    result, log = run_with(
        tmp_path, ws_dir, store,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "modify_text", {"path": "note.md", "content": "agent 写的\n"})]),
            ModelResponse("end_turn", content="改完"),
        ],
    )

    # 用户手动又改了一次
    user_text = "用户后来手写的\n"
    (ws_dir / "note.md").write_text(user_text, encoding="utf-8")

    undo = undo_with(log, result.run_id, ws_dir)
    assert not undo.ok
    assert undo.conflicts, "必须报告冲突"
    assert "拒绝覆盖" in undo.conflicts[0]
    assert (ws_dir / "note.md").read_text(encoding="utf-8") == user_text, \
        "冲突时绝不允许覆盖用户内容"
    log.close()


def test_undo_is_atomic_across_multiple_files(tmp_path):
    """两个文件里有一个冲突，整体不执行——避免撤一半的尴尬状态。"""
    ws_dir, store = setup(tmp_path, {"a.md": "甲原", "b.md": "乙原"})
    result, log = run_with(
        tmp_path, ws_dir, store,
        [
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "modify_text", {"path": "a.md", "content": "甲改"}),
                ToolCall("c2", "modify_text", {"path": "b.md", "content": "乙改"}),
            ]),
            ModelResponse("end_turn", content="改完"),
        ],
    )

    (ws_dir / "b.md").write_text("乙又被用户改了", encoding="utf-8")

    undo = undo_with(log, result.run_id, ws_dir)
    assert not undo.ok
    assert (ws_dir / "a.md").read_text(encoding="utf-8") == "甲改", \
        "有冲突时不应部分还原"
    assert (ws_dir / "b.md").read_text(encoding="utf-8") == "乙又被用户改了"
    log.close()


def test_modify_nonexistent_creates_no_snapshot(tmp_path):
    ws_dir, store = setup(tmp_path, {})
    result, log = run_with(
        tmp_path, ws_dir, store,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "modify_text", {"path": "ghost.md", "content": "x"})]),
            ModelResponse("end_turn", content="失败"),
        ],
    )
    assert store.load(result.run_id) == [], "失败的操作不该留快照"
    assert receipts_of(log, result.run_id) == []
    log.close()


def test_snapshot_dedups_identical_content(tmp_path):
    """同样的写前内容只存一份 blob。"""
    ws_dir, store = setup(tmp_path, {"a.md": "同样内容", "b.md": "同样内容"})
    result, log = run_with(
        tmp_path, ws_dir, store,
        [
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "modify_text", {"path": "a.md", "content": "改 A"}),
                ToolCall("c2", "modify_text", {"path": "b.md", "content": "改 B"}),
            ]),
            ModelResponse("end_turn", content="改完"),
        ],
    )
    changes = store.load(result.run_id)
    assert len({c.blob_ref for c in changes}) == 1, "相同内容应去重成同一 blob"
    log.close()
