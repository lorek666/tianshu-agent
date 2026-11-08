"""C1 的 Usage 归一化（PRD §5.1）。

重点是那条容易被忽略的约束：**未知必须是 None，不能是 0。**
把未知当 0 会让预算统计"看起来正常"，实际已经失控——比直接报错更危险。
"""

from __future__ import annotations

from pathlib import Path

from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, normalize_usage
from tianshu.replay import replay

ENTITY = ""


def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("内容", encoding="utf-8")
    return ws


# ---------------------------------------------------------------- 归一化


def test_openai_shape():
    assert normalize_usage({"prompt_tokens": 10, "completion_tokens": 5}) == {
        "input_tokens": 10,
        "output_tokens": 5,
    }


def test_anthropic_shape():
    assert normalize_usage({"input_tokens": 8, "output_tokens": 3}) == {
        "input_tokens": 8,
        "output_tokens": 3,
    }


def test_gemini_shape():
    assert normalize_usage({"promptTokenCount": 12, "candidatesTokenCount": 7}) == {
        "input_tokens": 12,
        "output_tokens": 7,
    }


def test_missing_is_none_not_zero():
    for raw in (None, {}, {"seen": "unexpected"}):
        u = normalize_usage(raw)
        assert u["input_tokens"] is None, f"{raw} 的 input 必须是 None 而不是 0"
        assert u["output_tokens"] is None


def test_partial_usage_keeps_other_side_none():
    u = normalize_usage({"prompt_tokens": 9})
    assert u["input_tokens"] == 9
    assert u["output_tokens"] is None, "没给的一半必须是 None"


def test_bool_is_not_int():
    # isinstance(True, int) 为真，别让 True 变成 1 token
    assert normalize_usage({"prompt_tokens": True})["input_tokens"] is None


# ---------------------------------------------------------------- 与主循环联动


def _run(tmp_path: Path, usage: dict):
    ws = make_ws(tmp_path)
    model = ScriptedModel([ModelResponse("end_turn", content="完成", usage=usage)])
    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    result = run(
        task="t", workspace_dir=ws, out_name="s.md", model=model,
        log=log, limits=Limits(8, 1000),
    )
    report = replay(log, result.run_id)
    log.close()
    return result, report


def test_unknown_usage_marks_budget_unenforced(tmp_path):
    result, report = _run(tmp_path, {})

    assert result.tokens == 0
    assert result.unknown_usage_calls == 1
    assert report.ok
    # run.end 必须如实记录预算是否可信
    end = [e for e in EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
           .read_run(result.run_id) if e.type == "run.end"][0]
    assert end.payload["budget_enforced"] is False
    assert end.payload["tokens_unknown_calls"] == 1


def test_known_usage_enforces_budget(tmp_path):
    result, report = _run(tmp_path, {"input_tokens": 100, "output_tokens": 50})
    assert result.tokens == 150
    assert result.unknown_usage_calls == 0
    assert report.ok


def test_budget_exceeded_stops_run(tmp_path):
    ws = make_ws(tmp_path)
    model = ScriptedModel([
        ModelResponse("end_turn", content="ok",
                      usage={"input_tokens": 900, "output_tokens": 900}),
    ])
    log = EventLog(tmp_path / "e2.db", blob_dir=tmp_path / "blobs")
    result = run(task="t", workspace_dir=ws, out_name="s.md", model=model,
                 log=log, limits=Limits(8, 1000))
    log.close()

    assert result.outcome == "failed"
    assert "token 预算耗尽" in (result.error or "")
