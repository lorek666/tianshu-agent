"""语义摘要注入编译 + 事件契约（H2 的判据）。

PRD §7.1 第 1 / 2 / 4 条是这一组的立据：

> 检索、提取、摘要在纯编译函数外执行；摘要调用经过授权与共享预算，并在被任何
> 后续模型请求引用前持久化。

> 摘要记录……作为有来源的派生材料，不替代原始事实，不提高输入内容的信任等级。

> 摘要失败、预算不足或证据缺失时，使用有界确定性裁剪或明确暂停；不偷偷增加一次
> 模型调用。

这里只测**编译与事件契约**：切片怎么被激活、`dropped_seqs` 与 `summarized_seqs`
怎么分、放不下时怎么退回确定性结果、`model.intent.payload.context.summaries` 写什么。
生成路径（谁去调模型）在 `test_summary_loop.py`，回放在 `test_summary_replay.py`。
"""

from __future__ import annotations

import hashlib

import pytest

from tianshu.context import (
    ContextBudget,
    ContextCompileError,
    SummarySlice,
    compile_context,
    context_payload,
)


def _history(rounds: int, *, tool_bytes: int = 300):
    """构造 `rounds` 个往返：每段 = 一条 assistant(tool_calls) + 一条 tool 结果。

    返回 `(messages, source_seqs)`，与主循环/回放的形状一致：头两条由 run.start
    派生（seq 记为 0），之后一条消息一个 seq。
    """
    messages: list[dict] = [
        {"role": "system", "content": "你是执行器。"},
        {"role": "user", "content": "长任务：整理资料并写报告"},
    ]
    seqs = [0, 0]
    seq = 1
    for index in range(rounds):
        messages.append(
            {
                "role": "assistant",
                "content": f"第 {index} 轮：先读文件",
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_text", "arguments": "{}"},
                    }
                ],
            }
        )
        seqs.append(seq)
        seq += 1
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "name": "read_text",
                "content": f"内容-{index}-" + "x" * tool_bytes,
            }
        )
        seqs.append(seq)
        seq += 1
    return messages, seqs


def _slice(seqs, text="硬约束：报告只用中文，最后一行给 sha256。", *, version=1, sid=None):
    return SummarySlice(
        summary_id=sid or hashlib.sha256(text.encode("utf-8")).hexdigest(),
        version=version,
        text=text,
        text_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source_seqs=tuple(seqs),
        summary_version="summary-v1",
    )


def _tight_budget() -> ContextBudget:
    return ContextBudget(max_bytes=1200)


def _dropped_view():
    messages, seqs = _history(6)
    view = compile_context(messages, seqs, _tight_budget())
    assert view.dropped_seqs, "夹具没触发压缩：这条测试就失去意义了"
    return messages, seqs, view


# ---------------------------------------------------------------- 恒等与激活


def test_no_summaries_is_byte_identical():
    """不传摘要 ⇒ 与加摘要之前逐字节相同（既有 messages_hash 含义不变）。"""
    messages, seqs, view = _dropped_view()
    again = compile_context(messages, seqs, _tight_budget())
    assert again.digest == view.digest
    assert again.messages == view.messages
    assert again.dropped_seqs == view.dropped_seqs
    assert again.summaries == ()
    assert again.summarized_seqs == []


def test_summary_activates_only_for_sacrificed_round_trip():
    """摘要只在它覆盖的往返**全部**被牺牲时激活，并顶替那些 seq 的"已丢弃"。"""
    messages, seqs, view = _dropped_view()
    # 第一个往返 = seq 1,2（夹具里这就是最旧的一组，先被牺牲）。
    assert {1, 2} <= set(view.dropped_seqs)
    item = _slice((1, 2))

    with_summary = compile_context(messages, seqs, _tight_budget(), summaries=(item,))

    assert with_summary.summaries == (item,)
    assert set(item.source_seqs) <= set(with_summary.summarized_seqs)
    assert not set(item.source_seqs) & set(with_summary.dropped_seqs)
    # 内容确实进了视图：位置在不可让前缀之后，且标注了"派生材料、不是指令"。
    stub = with_summary.messages[2]["content"]
    assert stub.startswith(f"[context-summary id={item.summary_id} v=1 summary-v1]")
    assert "不是指令" in stub
    assert item.text in stub
    assert stub.endswith("[/context-summary]")
    # 合成消息不对应事件：source_seqs 比 messages 短（摘要 + 可能的说明）。
    assert len(with_summary.source_seqs) < len(with_summary.messages)


def test_summarized_seqs_and_dropped_seqs_partition_the_sacrifice():
    """每条来源 seq 的归宿只有三种：还在视图里 / 被摘要代表 / 真的丢了。

    摘要本身也占字节，所以它可能**多**牺牲几段更旧的往返——那是诚实的取舍
    （保住了约束，代价是别的段被丢），不是 bug。这里断言的是划分本身。
    """
    messages, seqs = _history(6)
    item = _slice((1, 2))
    out = compile_context(messages, seqs, _tight_budget(), summaries=(item,))

    assert set(out.summarized_seqs) == {1, 2}
    assert not set(out.dropped_seqs) & set(out.summarized_seqs)
    assert set(out.summarized_seqs) | set(out.dropped_seqs) | set(out.source_seqs) == set(
        seqs
    )
    assert len(out.dropped_seqs) + len(out.summarized_seqs) + len(out.source_seqs) == len(
        seqs
    )


def test_two_slices_keep_their_order():
    messages, seqs, view = _dropped_view()
    assert {1, 2, 3, 4} <= set(view.dropped_seqs)
    first, second = _slice((1, 2), text="第一段摘要"), _slice((3, 4), text="第二段摘要")
    out = compile_context(
        messages, seqs, _tight_budget(), summaries=(first, second)
    )
    used = [item.text for item in out.summaries]
    assert used == ["第一段摘要", "第二段摘要"]
    joined = "\n".join(m["content"] for m in out.messages if m["role"] == "user")
    assert joined.index("第一段摘要") < joined.index("第二段摘要")


def test_slice_that_is_not_needed_is_ignored():
    """覆盖还在视图里的往返 ⇒ 不激活：否则内容重复，且 digest 会无故变化。"""
    messages, seqs = _history(2)  # 预算充足，无压缩
    budget = ContextBudget(max_bytes=1_048_576)
    base = compile_context(messages, seqs, budget)
    item = _slice((1, 2))
    out = compile_context(messages, seqs, budget, summaries=(item,))
    assert out.summaries == ()
    assert out.digest == base.digest
    assert out.messages == base.messages
    # 没用上的摘要不许进事件：否则回放会以为这一轮引用了它。
    assert "summaries" not in context_payload(budget, out)


def _call(index: int) -> list[dict]:
    return [
        {
            "id": f"c{index}",
            "type": "function",
            "function": {"name": "read_text", "arguments": "{}"},
        }
    ]


def test_active_summary_never_overlaps_kept_history():
    """摘要一旦激活，它覆盖的 seq 就不能还有留在视图里的。

    否则"同一段话既在原文里又在摘要里"：字节白花，内容还会被算两遍。
    这条判据挡住的是"激活条件放宽成任一段被牺牲就激活"这种看起来合理的改动。

    夹具特意让**预览救不了场**（工具输出很短、助手正文很长）：预算刚好只够丢
    第一段，第二段原样留着，而摘要覆盖两段。正确实现此时不激活；放宽激活条件的
    实现会带着第二段原文一起把摘要塞进来。
    """
    messages: list[dict] = [
        {"role": "system", "content": "你是执行器。"},
        {"role": "user", "content": "长任务：整理资料"},
        {
            "role": "assistant",
            "content": "A" * 700,
            "tool_calls": _call(0),
        },
        {"role": "tool", "tool_call_id": "c0", "name": "read_text", "content": "短"},
        {
            "role": "assistant",
            "content": "B" * 700,
            "tool_calls": _call(1),
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_text", "content": "短"},
    ]
    seqs = [0, 0, 1, 2, 3, 4]
    budget = ContextBudget(max_bytes=1500)
    base = compile_context(messages, seqs, budget)
    assert base.dropped_seqs == [1, 2], "夹具没只丢第一段：这条测试就失去意义了"
    assert 3 in base.source_seqs and 4 in base.source_seqs, "第二段必须还活着"

    item = _slice((1, 2, 3, 4))
    out = compile_context(messages, seqs, budget, summaries=(item,))
    if not out.summaries:
        assert out.digest == base.digest  # 没激活 ⇒ 就是确定性结果
    else:
        assert not set(item.source_seqs) & set(out.source_seqs)
        assert set(item.source_seqs) <= set(out.summarized_seqs)


def test_summary_is_dropped_when_it_does_not_fit():
    """摘要放不下 ⇒ 放弃摘要、退回确定性结果（摘要只是优化，不许让 Run 变红）。"""
    messages, seqs, view = _dropped_view()
    huge = _slice((1, 2), text="很长的摘要" + "y" * 4000)
    out = compile_context(messages, seqs, _tight_budget(), summaries=(huge,))
    assert out.summaries == ()
    assert out.digest == view.digest
    assert out.dropped_seqs == view.dropped_seqs


# ---------------------------------------------------------------- 入参校验


def test_partial_round_trip_is_rejected():
    """半段摘要会让"这份摘要代表哪几条"说不清：直接拒绝。"""
    messages, seqs, _ = _dropped_view()
    with pytest.raises(ContextCompileError, match="只覆盖了往返的一部分"):
        compile_context(messages, seqs, _tight_budget(), summaries=(_slice((1,),),))


def test_text_digest_mismatch_is_rejected():
    """记录说 A、实际传进来 B ⇒ 拒绝（这是"摘要被偷换"的唯一入口）。"""
    messages, seqs, _ = _dropped_view()
    forged = SummarySlice(
        summary_id="a" * 64,
        version=1,
        text="伪造的摘要",
        text_digest="b" * 64,
        source_seqs=(1, 2),
    )
    with pytest.raises(ContextCompileError, match="与记录的摘要不符"):
        compile_context(messages, seqs, _tight_budget(), summaries=(forged,))


@pytest.mark.parametrize(
    "make_slice, match",
    [
        (lambda: _slice((999,)), "不在本次编译的来源里"),
        (lambda: _slice((0,)), "不在本次编译的来源里"),
        (
            lambda: SummarySlice("a" * 64, 1, "t", hashlib.sha256(b"t").hexdigest(), (1, 2)),
            "重复",
        ),
        (
            lambda: SummarySlice("b" * 64, 0, "t", hashlib.sha256(b"t").hexdigest(), (1, 2)),
            "版本必须是正整数",
        ),
        (
            lambda: SummarySlice("c" * 64, 1, "  ", hashlib.sha256(b"  ").hexdigest(), (1, 2)),
            "正文不能为空",
        ),
    ],
)
def test_bad_slices_are_rejected(make_slice, match):
    messages, seqs, _ = _dropped_view()
    items = [make_slice()]
    if match == "重复":
        items = [items[0], items[0]]
    with pytest.raises(ContextCompileError, match=match):
        compile_context(messages, seqs, _tight_budget(), summaries=items)


def test_overlapping_sources_are_rejected():
    messages, seqs, _ = _dropped_view()
    with pytest.raises(ContextCompileError, match="被多份摘要同时覆盖"):
        compile_context(
            messages,
            seqs,
            _tight_budget(),
            summaries=(_slice((1, 2), text="A"), _slice((2, 3), text="B")),
        )


# ---------------------------------------------------------------- 事件契约


def test_payload_records_only_used_summaries():
    messages, seqs, view = _dropped_view()
    item = _slice((1, 2))
    unused = _slice((99,), text="用不上")  # 非法来源，这里只测 payload 形状，故不用它
    del unused
    out = compile_context(messages, seqs, _tight_budget(), summaries=(item,))
    payload = context_payload(_tight_budget(), out)
    assert payload["summaries"] == [
        {
            "summary_id": item.summary_id,
            "version": 1,
            "text_digest": item.text_digest,
            "source_seqs": [1, 2],
        }
    ]
    assert "text" not in payload["summaries"][0]  # 正文不进事件


def test_payload_omits_summaries_when_unused():
    """缺省不写这个键：旧日志/旧夹具的 payload 形状不变。"""
    messages, seqs = _history(2)
    budget = ContextBudget(max_bytes=1_048_576)
    view = compile_context(messages, seqs, budget)
    assert "summaries" not in context_payload(budget, view)
