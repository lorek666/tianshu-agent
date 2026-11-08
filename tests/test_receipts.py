"""M1-PR1 验收：操作凭据。

核心命题：**Agent 改了什么，必须能不看对话就查清楚。**
一条 Receipt 要能独立核对——只凭它自己 + 磁盘现状。

覆盖鸡屁踢列的验收点：
- 每次写入都能对应一份 Receipt
- 新建文件的执行前状态明确记为「不存在」
- 失败与结果未知不能冒充成功
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import receipts_of, verify_receipts

REPORT = "summary.md"


def make_ws(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in (files or {}).items():
        (ws / name).write_text(content, encoding="utf-8")
    return ws


def _run(tmp_path, ws, responses, out=REPORT):
    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    result = run(
        task="生成摘要",
        workspace_dir=ws,
        out_name=out,
        model=ScriptedModel(responses),
        log=log,
        limits=Limits(8, 20_000),
    )
    return result, log


BODY = "# 摘要\n\n- 要点一（来源：a.md）\n"


def test_receipt_written_for_successful_create(tmp_path):
    ws = make_ws(tmp_path, {"a.md": "内容"})
    result, log = _run(
        tmp_path,
        ws,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": REPORT, "content": BODY})]),
            ModelResponse("end_turn", content="完成"),
        ],
    )

    rs = receipts_of(log, result.run_id)
    assert len(rs) == 1, "写入成功必须恰好产生一份凭据"

    r = rs[0]
    assert r["effect_state"] == "applied"
    assert r["before_state"] == "absent", "新建文件的执行前状态必须是 absent"
    assert r["resource"] == REPORT
    assert r["tool_id"] == "create_report"
    assert r["issuer"].startswith("tianshu/")

    # 凭据里的 after_state 必须等于磁盘真实内容
    on_disk = (ws / REPORT).read_bytes()
    assert r["after_state"] == hashlib.sha256(on_disk).hexdigest()
    assert verify_receipts(log, result.run_id) == []
    log.close()


def test_receipt_binds_arguments(tmp_path):
    """args_hash 必须能独立重算出内容哈希——这样凭据才能证明「写的是这个内容」。"""
    ws = make_ws(tmp_path, {"a.md": "内容"})
    result, log = _run(
        tmp_path,
        ws,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": REPORT, "content": BODY})]),
            ModelResponse("end_turn", content="完成"),
        ],
    )

    receipt = receipts_of(log, result.run_id)[0]
    intent = [e.payload for e in log.read_run(result.run_id)
              if e.type == "tool.intent"][0]

    assert receipt["args_hash"] == intent["args_hash"], "凭据必须绑定参数摘要"
    recomputed = hashlib.sha256(intent["args"]["content"].encode()).hexdigest()
    assert recomputed == receipt["after_state"], "用参数能重算出产物哈希"
    log.close()


def test_no_receipt_when_overwrite_refused(tmp_path):
    """失败不能冒充成功：被拒绝时不得产生 applied 凭据。"""
    ws = make_ws(tmp_path, {"a.md": "内容", REPORT: "原内容"})
    result, log = _run(
        tmp_path,
        ws,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": REPORT, "content": BODY})]),
            ModelResponse("end_turn", content="被拒"),
        ],
    )

    assert receipts_of(log, result.run_id) == [], "被拒绝的写入不该有凭据"
    assert (ws / REPORT).read_text(encoding="utf-8") == "原内容"
    assert verify_receipts(log, result.run_id) == []
    log.close()


def test_no_receipt_for_rejected_escape(tmp_path):
    """越界写入同样不产凭据。"""
    tmp_path.joinpath("outside.md").write_text("外", encoding="utf-8")
    ws = make_ws(tmp_path, {"a.md": "内容"})
    result, log = _run(
        tmp_path,
        ws,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": "../outside.md", "content": "x"})]),
            ModelResponse("end_turn", content="被拒"),
        ],
    )

    assert receipts_of(log, result.run_id) == []
    assert result.effect_state == "none_applied"
    log.close()


def test_run_end_lists_receipt_ids(tmp_path):
    """run.end 要能直接索引到凭据，不用翻全表。"""
    ws = make_ws(tmp_path, {"a.md": "内容"})
    result, log = _run(
        tmp_path,
        ws,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": REPORT, "content": BODY})]),
            ModelResponse("end_turn", content="完成"),
        ],
    )

    end = [e.payload for e in log.read_run(result.run_id) if e.type == "run.end"][0]
    assert end["receipt_ids"] == result.receipts
    assert len(end["receipt_ids"]) == 1
    assert end["receipt_ids"][0].startswith("rcpt_")
    log.close()


def test_receipt_detects_tampered_hash(tmp_path):
    """凭据与工具结果对不上时必须报出来（防止事后篡改）。"""
    ws = make_ws(tmp_path, {"a.md": "内容"})
    result, log = _run(
        tmp_path,
        ws,
        [
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "create_report", {"name": REPORT, "content": BODY})]),
            ModelResponse("end_turn", content="完成"),
        ],
    )

    # 伪造一条哈希不匹配的凭据，验证对账能发现
    log.append(result.run_id, "receipt",
               {**receipts_of(log, result.run_id)[0], "after_state": "0" * 64},
               actor_kind="core")
    problems = verify_receipts(log, result.run_id)
    assert problems, "哈希不一致必须被检出"
    assert any("不一致" in p for p in problems)
    log.close()
