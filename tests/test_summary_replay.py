"""摘要回放（H2 的判据）：读当时保存的那一份，不重新生成。

PRD §7.1 第 3 条是这一组的立据：

> replay 读取当时保存的摘要和选中材料；不重新生成摘要、不重新检索当前索引。
> 需要刷新时追加新版本，历史运行继续引用旧版本。

做法：手工造一条带长历史的 Run（事件是唯一事实源），把一份**真实落库**的摘要
引用写进 `model.intent`，然后看回放能不能核对上；再逐项破坏（记录缺失、正文被
换、版本不符、来源被改），每一次都必须报 problem 而不是"跳过算通过"。
"""

from __future__ import annotations

import hashlib
import json

from support import stack_for

from tianshu.context import (
    RUN_START_SEQ,
    ContextBudget,
    SummarySlice,
    compile_context,
    context_payload,
)
from tianshu.event_schema import _MODEL_CONTEXT  # noqa: PLC2701 - 契约形状要按实现核
from tianshu.events import EventLog
from tianshu.loop import SYSTEM_PROMPT
from tianshu.replay import replay
from tianshu.summaries import MaterialBlock, SummaryStore, build_material
from tianshu.tasks import RUN_RUNNING

ROUNDS = 6
BUDGET = ContextBudget(max_bytes=1500)
SUMMARY_TEXT = "硬约束：报告只用中文，最后一行给 sha256。"


def _assistant_round(index: int) -> dict:
    return {
        "request_id": f"req_{index}",
        "content": f"第 {index} 轮：先读文件",
        "finish_reason": "tool_use",
        "tool_calls": [
            {"call_id": f"c{index}", "name": "read_text", "arguments": {"path": "a.md"}}
        ],
    }


def _tool_round(index: int) -> dict:
    return {
        "call_id": f"c{index}",
        "tool_id": "read_text",
        "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": f"内容-{index}-" + "x" * 300},
    }


def _as_message(payload: dict) -> dict:
    """与 `replay` 重建消息的规则**完全一致**（否则 digest 必错）。"""
    return {
        "role": "assistant",
        "content": payload["content"],
        "tool_calls": [
            {
                "id": call["call_id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(
                        call["arguments"], ensure_ascii=False, sort_keys=True
                    ),
                },
            }
            for call in payload["tool_calls"]
        ],
    }


def _as_tool_message(payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": payload["call_id"],
        "name": payload["tool_id"],
        "content": json.dumps(
            payload["output"], ensure_ascii=False, sort_keys=True
        ),
    }


def _long_run(tmp_path, *, rounds: int = ROUNDS):
    """造一条带 `rounds` 个工具往返的 Run，返回装配好的现场。"""
    (tmp_path / "ws").mkdir()
    (tmp_path / "authority").mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path / "authority")
    goal = "长任务：整理资料并写报告"
    task = stack.tasks.create_task(goal, [str(tmp_path / "ws")])
    run = stack.tasks.start_run(task.task_id, payload={"task": goal, "out": ""})
    stack.tasks.transition(run.run_id, RUN_RUNNING)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "长任务：整理资料并写报告"},
    ]
    # 头两条由 run.start 派生，没有属于自己的消息事件：归因就是 `RUN_START_SEQ`（0），
    # 与 `message_source_seqs` / 主循环同一份口径（编一个真 seq 会让回放必然对不上）。
    seqs: list[int] = [RUN_START_SEQ, RUN_START_SEQ]
    for index in range(rounds):
        assistant = _assistant_round(index)
        seq = log.append(
            run.run_id, "model.result", assistant, task_id=task.task_id,
            idem_key=f"req_{index}", legacy_write=True,
        )
        messages.append(_as_message(assistant))
        seqs.append(seq)
        tool = _tool_round(index)
        seq = log.append(
            run.run_id, "tool.result", tool, task_id=task.task_id,
            idem_key=f"c{index}", legacy_write=True,
        )
        messages.append(_as_tool_message(tool))
        seqs.append(seq)
    return log, stack, task, run, messages, seqs


def _persist_summary(log, task, run, messages, seqs, *, text: str = SUMMARY_TEXT):
    """把第一组往返写成一份**真实落库**的摘要，返回它的切片。"""
    store = SummaryStore(log)
    blocks = [
        MaterialBlock(seq=seqs[2], role="assistant", content=messages[2]["content"]),
        MaterialBlock(seq=seqs[3], role="tool", content=messages[3]["content"]),
    ]
    material = build_material(
        blocks, scope=f"task:{task.task_id}", task_id=task.task_id, run_id=run.run_id
    )
    record = store.begin(
        material, model="fake:test:model", run_id=run.run_id, task_id=task.task_id
    )
    record = store.complete(record, text)
    return store, record


def _slice_for(record, seqs) -> SummarySlice:
    return SummarySlice(
        summary_id=record.summary_id,
        version=record.version,
        text=record.text or "",
        text_digest=record.text_digest or "",
        source_seqs=(seqs[2], seqs[3]),
        summary_version=record.summary_version,
    )


def _intent(log, stack, task, run, *, messages_hash, context):
    log.append(
        run.run_id,
        "model.intent",
        {
            "request_id": "req_final",
            "attempt": 1,
            "messages_hash": messages_hash,
            "context": context,
        },
        task_id=task.task_id,
        idem_key="req_final",
        legacy_write=True,
    )


def _finish(log, stack, task, run):
    log.append(
        run.run_id,
        "model.result",
        {"request_id": "req_final", "content": "完成", "finish_reason": "end_turn"},
        task_id=task.task_id,
        idem_key="req_final",
        legacy_write=True,
    )
    stack.tasks.finish_run(run.run_id, outcome="succeeded", effect_state="none_applied")


def _ok_case(tmp_path):
    log, stack, task, run, messages, seqs = _long_run(tmp_path)
    store, record = _persist_summary(log, task, run, messages, seqs)
    view = compile_context(messages, seqs, BUDGET, summaries=(_slice_for(record, seqs),))
    assert view.summaries, "夹具没让摘要进入视图：后面就测不到回放"
    _intent(log, stack, task, run, messages_hash=view.digest,
            context=context_payload(BUDGET, view))
    _finish(log, stack, task, run)
    return log, stack, task, run, store, record, view


# ---------------------------------------------------------------- 正例


def test_replay_reads_saved_summary_and_verifies(tmp_path):
    log, _stack, _task, run, _store, _record, view = _ok_case(tmp_path)
    report = replay(log, run.run_id)
    assert report.ok, report.problems
    assert report.messages_verified == 1
    assert len(view.summaries) == 1
    log.close()


def test_payload_summary_shape_matches_event_contract(tmp_path):
    """`context.summaries` 的形状必须与事件契约一致（不是"差不多"）。"""
    log, _stack, _task, run, _store, _record, view = _ok_case(tmp_path)
    intents = [e for e in log.read_run(run.run_id) if e.type == "model.intent"]
    payload = intents[0].payload["context"]
    assert set(payload["summaries"][0]) == {
        "summary_id",
        "version",
        "text_digest",
        "source_seqs",
    }
    contract = _MODEL_CONTEXT["properties"]["summaries"]
    item = contract["items"]
    assert set(item["properties"]) == set(payload["summaries"][0])
    assert "summaries" not in _MODEL_CONTEXT["required"]  # 可选：旧日志没有这个键
    log.close()


# ---------------------------------------------------------------- 反例


def test_missing_summary_record_is_a_problem_not_a_skip(tmp_path):
    """库里没有这条摘要 ⇒ 报 problem。回放**不重新生成**摘要。"""
    log, stack, task, run, messages, seqs = _long_run(tmp_path)
    fake = SummarySlice(
        summary_id="f" * 64,
        version=1,
        text=SUMMARY_TEXT,
        text_digest=hashlib.sha256(SUMMARY_TEXT.encode("utf-8")).hexdigest(),
        source_seqs=(seqs[2], seqs[3]),
    )
    view = compile_context(messages, seqs, BUDGET, summaries=(fake,))
    _intent(log, stack, task, run, messages_hash=view.digest,
            context=context_payload(BUDGET, view))
    _finish(log, stack, task, run)

    report = replay(log, run.run_id)
    assert not report.ok
    assert any("摘要记录缺失" in problem for problem in report.problems), report.problems
    log.close()


def test_tampered_text_digest_is_a_problem(tmp_path):
    """记录里的正文摘要与库里那一份不符 ⇒ 报 problem（不"以库为准"洗白）。"""
    log, stack, task, run, messages, seqs = _long_run(tmp_path)
    store, record = _persist_summary(log, task, run, messages, seqs)
    item = _slice_for(record, seqs)
    forged = SummarySlice(
        summary_id=item.summary_id,
        version=item.version,
        text=item.text,
        text_digest=hashlib.sha256(b"another text").hexdigest(),
        source_seqs=item.source_seqs,
        summary_version=item.summary_version,
    )
    # 编译侧先拒绝伪造的 digest：这里是"记录与正文不符"的第一道闸。
    view = compile_context(messages, seqs, BUDGET, summaries=(item,))
    payload = context_payload(BUDGET, view)
    payload["summaries"][0]["text_digest"] = forged.text_digest
    _intent(log, stack, task, run, messages_hash=view.digest, context=payload)
    _finish(log, stack, task, run)

    report = replay(log, run.run_id)
    assert not report.ok
    assert any("正文摘要不符" in problem for problem in report.problems), report.problems
    log.close()


def test_version_mismatch_is_a_problem(tmp_path):
    log, stack, task, run, messages, seqs = _long_run(tmp_path)
    _store, record = _persist_summary(log, task, run, messages, seqs)
    item = _slice_for(record, seqs)
    view = compile_context(messages, seqs, BUDGET, summaries=(item,))
    payload = context_payload(BUDGET, view)
    payload["summaries"][0]["version"] = 7
    _intent(log, stack, task, run, messages_hash=view.digest, context=payload)
    _finish(log, stack, task, run)

    report = replay(log, run.run_id)
    assert not report.ok
    assert any("版本不符" in problem for problem in report.problems), report.problems
    log.close()


def test_forged_source_seqs_is_a_problem(tmp_path):
    """记录的来源被改 ⇒ 重算编译对不上 / 校验拒绝：都要红。"""
    log, stack, task, run, messages, seqs = _long_run(tmp_path)
    _store, record = _persist_summary(log, task, run, messages, seqs)
    item = _slice_for(record, seqs)
    view = compile_context(messages, seqs, BUDGET, summaries=(item,))
    payload = context_payload(BUDGET, view)
    payload["summaries"][0]["source_seqs"] = [999, 1000]
    _intent(log, stack, task, run, messages_hash=view.digest, context=payload)
    _finish(log, stack, task, run)

    report = replay(log, run.run_id)
    assert not report.ok
    assert any(
        "摘要" in problem or "重算上下文编译失败" in problem
        for problem in report.problems
    ), report.problems
    log.close()


def test_old_intent_without_summaries_still_replays(tmp_path):
    """没有 `summaries` 键的旧形状继续走原路径（旧夹具一行不改）。"""
    log, stack, task, run, messages, seqs = _long_run(tmp_path)
    budget = ContextBudget(max_bytes=1_048_576)  # 不压缩：没有 dropped，也没有摘要
    view = compile_context(messages, seqs, budget)
    _intent(log, stack, task, run, messages_hash=view.digest,
            context=context_payload(budget, view))
    _finish(log, stack, task, run)

    report = replay(log, run.run_id)
    assert report.ok, report.problems
    intents = [e for e in log.read_run(run.run_id) if e.type == "model.intent"]
    assert "summaries" not in intents[0].payload["context"]
    log.close()
