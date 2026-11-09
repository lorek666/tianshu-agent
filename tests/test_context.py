"""F5 上下文编译：纯函数性、分区预算、8 KiB 落盘、以及 loop/replay 的接线。

这一组测试要钉住四件容易被做错的事：

1. **预算内恒等**：默认预算下 `compile_context` 不改一个字节。否则所有
   `messages_hash` 一起变，历史事件日志与既有 fixture 全废。
2. **同输入必得同输出**：包括跨进程（不同 `PYTHONHASHSEED`）——只要有哪个
   实现细节偷偷依赖 set 顺序，这里就会红。
3. **压缩不许拆对**：`assistant.tool_calls` 与它的 `tool` 结果必须同生同死，
   拆开就是下一轮 400。
4. **回放能重算**：`model.intent` 记下的预算参数必须足以让 `replay` 重算出
   同一个 digest；压缩过的那一轮也要能过。

纯函数部分的用例不碰磁盘、不建 Run：它们只需要消息列表。少数几处 import 了
`tianshu.context` 的私有构造函数（`_compact_note` / `_preview_stub`），用途是
**精确算出"压缩到某个状态需要多少字节"来定预算**，而不是断言内部实现细节。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from support import tool_capable_profile

from tianshu.context import (
    BUDGET_VERSION,
    NOTE_SEQ_LIMIT,
    PREVIEW_BYTES,
    RUN_START_SEQ,
    TOOL_OUTPUT_SPILL_BYTES,
    ContextBudget,
    ContextCompileError,
    ContextOverflow,
    ContextView,
    _compact_note,
    _preview_stub,
    budget_from_payload,
    compile_context,
    context_payload,
)
from tianshu.events import Event, EventLog, canonical_bytes
from tianshu.loop import (
    AgentLoop,
    Limits,
    _Progress,
    build_stack,
    initial_messages,
    message_source_seqs,
    run,
)
from tianshu.model import ModelResponse, ToolCall
from tianshu.replay import replay
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, RunCursor

# ---------------------------------------------------------------- 构件


def _system(text: str = "你是天枢 Agent。") -> dict[str, Any]:
    return {"role": "system", "content": text}


def _user(text: str = "整理目录里的笔记") -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(content: str = "", calls: tuple[str, ...] = ()) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_text", "arguments": "{}"},
            }
            for call_id in calls
        ]
    return message


def _tool(call_id: str, output: Any) -> dict[str, Any]:
    """按 `loop._tool_message` 的同一个形状造工具消息（内容是 JSON 字符串）。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "read_text",
        "content": json.dumps(output, ensure_ascii=False, sort_keys=True),
    }


def _tool_with(call_id: str, content: str) -> dict[str, Any]:
    """直接给工具消息一个已知字节数的 content（测字节边界时用）。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "read_text",
        "content": content,
    }


def _round_trips(count: int, text: str = "x" * 300) -> list[dict[str, Any]]:
    """造 count 个「assistant + tool」往返。"""
    messages: list[dict[str, Any]] = [_system(), _user()]
    for index in range(count):
        messages.append(_assistant("", (f"c{index}",)))
        messages.append(_tool(f"c{index}", {"v": text}))
    return messages


def _size(messages: list[dict[str, Any]]) -> int:
    return len(canonical_bytes(messages))


def _digest(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_bytes(messages)).hexdigest()


def _seqs(count: int, start: int = 2) -> list[int]:
    """前两条是 run.start 派生（0），其余按 start,start+1,… 递增。"""
    return [RUN_START_SEQ, RUN_START_SEQ] + list(range(start, start + count - 2))


def _histories() -> list[list[dict[str, Any]]]:
    """几种形状的历史，都用真实的消息形状（工具内容是 JSON 串）。"""
    return [
        [_system(), _user()],
        [_system(), _user(), _assistant("看完了")],
        [_system(), _user(), _assistant("", ("c1",)), _tool("c1", {"content": "笔记"})],
        [
            _system(),
            _user(),
            _assistant("", ("c1", "c2")),
            _tool("c1", {"content": "甲"}),
            _tool("c2", {"content": "乙"}),
            _assistant("总结完了"),
        ],
    ]


def _pairs(view: ContextView) -> tuple[set[str], set[str]]:
    """视图里活着的 (tool_call id, tool 结果 id)，用来验配对没被拆开。"""
    calls = {
        tc["id"]
        for message in view.messages
        if message["role"] == "assistant"
        for tc in message.get("tool_calls") or []
    }
    results = {
        message["tool_call_id"] for message in view.messages if message["role"] == "tool"
    }
    return calls, results


# ---------------------------------------------------------------- 1. 恒等性


@pytest.mark.parametrize("messages", _histories())
def test_identity_within_budget(messages):
    """预算内：一个字节都不变，digest 就是全量历史的摘要。"""
    seqs = _seqs(len(messages))
    view = compile_context(messages, seqs, ContextBudget(max_bytes=1_048_576))

    assert view.messages == messages, "预算内必须是同一份消息（深相等）"
    assert view.messages is messages, "预算内不必为了安全先复制一份再发出去"
    assert view.digest == _digest(messages)
    assert view.source_seqs == seqs
    assert view.dropped_seqs == []
    assert view.spills == []
    assert view.compacted is False


def test_identity_at_spill_threshold_boundary():
    """恰好 8 KiB 不算"超"，不触发落盘；多一个字节才落。边界不能靠感觉。"""
    exact = _tool_with("c1", "x" * TOOL_OUTPUT_SPILL_BYTES)
    messages = [_system(), _user(), _assistant("", ("c1",)), exact]

    view = compile_context(messages, _seqs(4), ContextBudget(max_bytes=1_048_576))
    assert view.messages is messages
    assert view.spills == []
    assert view.compacted is False

    over = _tool_with("c1", "x" * (TOOL_OUTPUT_SPILL_BYTES + 1))
    messages2 = [_system(), _user(), _assistant("", ("c1",)), over]
    view2 = compile_context(messages2, _seqs(4), ContextBudget(max_bytes=1_048_576))
    assert len(view2.spills) == 1
    assert view2.compacted is True


def test_default_budget_is_the_loop_default():
    """`Limits` 的默认预算必须就是文档里那个数，否则"日常走恒等"是空话。"""
    assert Limits().context_budget_bytes == 1_048_576


# ---------------------------------------------------------------- 2. 同输入同输出


def test_same_input_same_output_repeated():
    """同一份输入连算两次：产物逐字段相同。"""
    messages = _histories()[-1]
    seqs = _seqs(len(messages), start=7)
    budget = ContextBudget(max_bytes=_size(messages) - 50, spill_threshold=64)

    first = compile_context(messages, seqs, budget)
    second = compile_context(messages, seqs, budget)

    assert first.digest == second.digest
    assert first.messages == second.messages
    assert first.source_seqs == second.source_seqs
    assert first.dropped_seqs == second.dropped_seqs
    assert [s for s, _ in first.spills] == [s for s, _ in second.spills]


def test_same_input_same_output_across_processes():
    """跨进程、跨哈希种子：digest / seq 归因必须一致。

    如果实现里哪一步依赖了 `set` 的迭代顺序（比如拼压缩说明时先遍历集合），
    在同一个进程里往往看不出来，换个 `PYTHONHASHSEED` 就露馅了。
    """
    messages = _round_trips(4)
    seqs = _seqs(len(messages), start=7)
    budget = ContextBudget(max_bytes=_size(messages) - 50, spill_threshold=64)
    local = compile_context(messages, seqs, budget)

    code = (
        "import json, sys\n"
        "from tianshu.context import ContextBudget, compile_context\n"
        "spec = json.load(sys.stdin)\n"
        "view = compile_context(\n"
        "    spec['messages'], spec['seqs'],\n"
        "    ContextBudget(max_bytes=spec['max_bytes'],\n"
        "                  spill_threshold=spec['spill_threshold']),\n"
        ")\n"
        "print(view.digest)\n"
        "print(json.dumps(view.source_seqs))\n"
        "print(json.dumps(view.dropped_seqs))\n"
    )
    spec = json.dumps(
        {
            "messages": messages,
            "seqs": seqs,
            "max_bytes": budget.max_bytes,
            "spill_threshold": budget.spill_threshold,
        }
    )

    digests = set()
    for seed in ("0", "1", "424242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code],
            input=spec,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        digest, source_json, dropped_json = out.stdout.splitlines()
        assert digest == local.digest, f"PYTHONHASHSEED={seed} 下编译结果不同"
        assert json.loads(source_json) == local.source_seqs
        assert json.loads(dropped_json) == local.dropped_seqs
        digests.add(digest)

    assert digests == {local.digest}


# ---------------------------------------------------------------- 3. 8 KiB 落盘


def test_tool_output_over_threshold_is_spilled():
    original = json.dumps({"content": "长内容" * 3000}, ensure_ascii=False)
    data = original.encode("utf-8")
    assert len(data) > TOOL_OUTPUT_SPILL_BYTES

    messages = [_system(), _user(), _assistant("", ("c1",)), _tool_with("c1", original)]
    view = compile_context(messages, _seqs(4), ContextBudget(max_bytes=1_048_576))

    placeholder = view.messages[3]["content"]
    digest = hashlib.sha256(data).hexdigest()

    assert placeholder != original
    assert placeholder.startswith("[tool-output-spilled]")
    assert digest in placeholder, "占位必须带上原文 blob 的 sha256"
    assert f"原始字节数: {len(data)}" in placeholder
    assert view.spills == [(digest, data)], "原文必须进 spills（compile 自己不写盘）"
    assert view.compacted is True
    # 只换 content，不换壳：还能看出是哪条调用、哪个工具
    assert view.messages[3]["tool_call_id"] == "c1"
    assert view.messages[3]["name"] == "read_text"
    assert _size(view.messages) < _size(messages), "落盘要真的变小，否则没有意义"


def test_spill_placeholder_carries_head_and_tail_previews():
    head = "头" * 400
    tail = "尾" * 400
    original = json.dumps(
        {"head": head, "middle": "中" * 2000, "tail": tail}, ensure_ascii=False
    )
    messages = [_system(), _user(), _assistant("", ("c1",)), _tool_with("c1", original)]
    view = compile_context(messages, _seqs(4), ContextBudget(max_bytes=1_048_576))
    placeholder = view.messages[3]["content"]

    assert f"头 {PREVIEW_BYTES} 字节预览:" in placeholder
    assert f"尾 {PREVIEW_BYTES} 字节预览:" in placeholder
    assert head[:100] in placeholder, "头部预览要能看见开头"
    assert tail[-100:] in placeholder, "尾部预览要能看见结尾"


def test_preview_never_splits_a_codepoint():
    """512 不是 3 的倍数：中文预览必须在字符边界停，不能出现替换符。"""
    original = "汉" * 4000
    messages = [_system(), _user(), _assistant("", ("c1",)), _tool_with("c1", original)]
    view = compile_context(messages, _seqs(4), ContextBudget(max_bytes=1_048_576))
    placeholder = view.messages[3]["content"]

    assert "\ufffd" not in placeholder
    head_line = placeholder.split(f"头 {PREVIEW_BYTES} 字节预览:\n", 1)[1].split("\n", 1)[0]
    # 512 = 3*170 + 2：完整字符 170 个（510 字节），多出的 2 字节是残缺尾巴，丢掉
    assert head_line == "汉" * 170
    assert len(head_line.encode("utf-8")) <= PREVIEW_BYTES


def test_utf8_byte_count_is_bytes_not_characters():
    """3000 个汉字 = 9000 字节：报"字节数"就必须是 9000，不许报 3000。"""
    original = "汉" * 3000
    data = original.encode("utf-8")
    assert len(original) == 3000 and len(data) == 9000

    messages = [_system(), _user(), _assistant("", ("c1",)), _tool_with("c1", original)]
    view = compile_context(messages, _seqs(4), ContextBudget(max_bytes=1_048_576))

    assert "原始字节数: 9000" in view.messages[3]["content"]
    assert view.spills == [(hashlib.sha256(data).hexdigest(), data)]


def test_emoji_counts_as_four_bytes():
    original = "🙂" * 3000  # 每个 emoji 4 字节 → 12000 字节
    data = original.encode("utf-8")
    assert len(data) == 12000

    messages = [_system(), _user(), _assistant("", ("c1",)), _tool_with("c1", original)]
    view = compile_context(messages, _seqs(4), ContextBudget(max_bytes=1_048_576))

    assert "原始字节数: 12000" in view.messages[3]["content"]
    assert "\ufffd" not in view.messages[3]["content"], "别把 4 字节的 emoji 从中间切开"
    assert view.spills[0][1] == data


def test_spills_are_deduplicated_by_content():
    same = "x" * 9000
    messages = _round_trips(2)
    messages[3] = _tool_with("c0", same)
    messages[5] = _tool_with("c1", same)
    view = compile_context(messages, _seqs(6), ContextBudget(max_bytes=1_048_576))

    assert len(view.spills) == 1, "同一份原文只该出现一次（内容寻址）"


# ---------------------------------------------------------------- 4. 预算压缩


def test_compaction_stays_within_budget():
    messages = _round_trips(5)
    pinned = _size(messages[:2])
    budget = ContextBudget(max_bytes=pinned + 500)

    view = compile_context(messages, _seqs(len(messages)), budget)

    assert _size(view.messages) <= budget.max_bytes, "压完必须真的在预算内"
    assert view.compacted is True
    assert view.dropped_seqs, "五段往返塞不进 500 字节，必须有东西被丢"


def test_compaction_keeps_system_and_task_untouched():
    messages = _round_trips(5)
    pinned_before = [dict(m) for m in messages[:2]]
    budget = ContextBudget(max_bytes=_size(messages[:2]) + 400)

    view = compile_context(messages, _seqs(len(messages)), budget)

    assert view.messages[:2] == pinned_before, "system 与任务陈述永不动"
    assert RUN_START_SEQ not in view.dropped_seqs
    assert view.source_seqs[:2] == [RUN_START_SEQ, RUN_START_SEQ]


def test_compaction_note_is_deterministic_and_lists_dropped_seqs():
    messages = _round_trips(4)
    seqs = _seqs(len(messages), start=11)
    budget = ContextBudget(max_bytes=_size(messages[:2]) + 400)

    view = compile_context(messages, seqs, budget)
    note = view.messages[len(messages[:2])]
    content = note["content"]

    assert note["role"] == "user", "说明消息要能插在任务陈述之后而不破坏角色交替"
    assert content.startswith("[context-compacted]")
    assert f"已丢弃最早的 {len(view.dropped_seqs)} 条消息" in content
    for seq in view.dropped_seqs[:NOTE_SEQ_LIMIT]:
        assert str(seq) in content, "被丢的 seq 必须列出来，不许悄悄丢"
    assert BUDGET_VERSION in content
    assert "确定性生成" in content, "说明必须自己交代它不是模型写的"
    assert re.search(r"\d{4}-\d{2}-\d{2}", content) is None, "不许写日期"
    assert re.search(r"\d{10,}", content) is None, "不许写毫秒时间戳"
    # 同输入再算一遍，说明逐字相同
    again = compile_context(messages, seqs, budget)
    assert again.messages[len(messages[:2])]["content"] == content


def test_compaction_note_is_single_message_even_with_many_dropped():
    messages = _round_trips(20)
    budget = ContextBudget(max_bytes=_size(messages[:2]) + 600)
    view = compile_context(messages, _seqs(len(messages)), budget)

    notes = [
        m
        for m in view.messages
        if str(m.get("content")).startswith("[context-compacted]")
    ]
    assert len(notes) == 1, "被丢的历史只合并成一条说明，不是每条一段"
    assert len(view.dropped_seqs) > NOTE_SEQ_LIMIT
    assert f"…等共 {len(view.dropped_seqs)} 条" in notes[0]["content"], (
        "超过列举上限时要给出总条数，说明本身才是有界的"
    )


def test_dropped_groups_are_the_oldest_prefix():
    """牺牲顺序是"从最旧开始"：丢掉的 seq 必须是一段前缀，活下来的是尾部。"""
    messages = _round_trips(6)
    seqs = _seqs(len(messages))
    # 精确算出"只留最后一段 + 一条说明"需要多少字节，预算就定在这个值上
    expected_dropped = seqs[2:-2]
    target = (
        messages[:2]
        + [{"role": "user", "content": _compact_note(expected_dropped)}]
        + messages[-2:]
    )
    budget = ContextBudget(max_bytes=_size(target))

    view = compile_context(messages, seqs, budget)

    assert view.dropped_seqs == expected_dropped
    assert _size(view.messages) <= budget.max_bytes
    assert len(view.source_seqs) == len(view.messages) - 1, "说明消息不对应事件"
    assert view.source_seqs[-2:] == seqs[-2:], "最后一段往返仍然活着"


def test_compaction_previews_old_tool_outputs_before_dropping():
    """第一档是"降到预览"，不是直接丢：还有救的老输出不该一上来就消失。"""
    messages = _round_trips(2, text="y" * 4000)
    seqs = _seqs(len(messages))
    origin = messages[3]["content"].encode("utf-8")
    stub = _preview_stub(hashlib.sha256(origin).hexdigest(), origin)
    previewed = messages[:3] + [_tool_with("c0", stub)] + messages[4:]
    budget = ContextBudget(max_bytes=_size(previewed))

    view = compile_context(messages, seqs, budget)

    assert view.dropped_seqs == [], "预览之后刚好放得下，就不该丢整段"
    assert view.messages[3]["content"] == stub
    assert "[tool-output-previewed" in view.messages[3]["content"]
    assert view.spills, "被预览替换掉的原文同样要可落盘"


def test_preview_stub_points_at_original_digest():
    """降级预览的 sha256 必须指向工具输出的**原文**，不是上一层占位文本。"""
    big = json.dumps({"v": "z" * 12000})
    messages = [_system(), _user(), _assistant("", ("c1",)), _tool_with("c1", big)]
    pinned = _size(messages[:2])
    budget = ContextBudget(max_bytes=pinned + 700)

    view = compile_context(messages, _seqs(4), budget)

    data = big.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    assert digest in view.messages[3]["content"]
    assert view.spills == [(digest, data)]


# ---------------------------------------------------------------- 5. 配对不拆开


def test_tool_pairing_survives_compaction():
    messages = _round_trips(6)
    budget = ContextBudget(max_bytes=_size(messages[:2]) + 700)
    view = compile_context(messages, _seqs(len(messages)), budget)

    calls, results = _pairs(view)
    assert calls == results, "assistant 的 tool_call 与 tool 结果必须同生同死"
    assert len(view.dropped_seqs) > 0, "这个预算下应当真的丢过东西"


def test_multi_call_turn_is_dropped_atomically():
    """一次 assistant 发三个工具：整段丢或整段留，不许只丢其中一条。"""
    messages = [
        _system(),
        _user(),
        _assistant("", ("c1", "c2", "c3")),
        _tool("c1", {"v": "x" * 300}),
        _tool("c2", {"v": "y" * 300}),
        _tool("c3", {"v": "z" * 300}),
        _assistant("", ("c4",)),
        _tool("c4", {"v": "w" * 300}),
    ]
    seqs = _seqs(len(messages))
    budget = ContextBudget(max_bytes=_size(messages[:2]) + 900)
    view = compile_context(messages, seqs, budget)

    first_turn = set(seqs[2:6])
    dropped = set(view.dropped_seqs)
    assert dropped & first_turn in (set(), first_turn), (
        "一次多工具的往返要么整段留、要么整段丢，不能只丢其中一条"
    )
    calls, results = _pairs(view)
    assert calls == results


def test_pairing_holds_when_turn_is_kept_but_content_previewed():
    """只降级内容、没丢消息时，配对当然也不能变。"""
    messages = _round_trips(2, text="q" * 4000)
    pinned = _size(messages[:2])
    view = compile_context(
        messages, _seqs(len(messages)), ContextBudget(max_bytes=pinned + 900)
    )
    calls, results = _pairs(view)
    assert calls == results


# ---------------------------------------------------------------- 6. 放不下就报错


def test_overflow_when_task_alone_too_big():
    messages = [_system("系统" * 200), _user("任务" * 200)]
    with pytest.raises(ContextOverflow) as exc_info:
        compile_context(messages, _seqs(2), ContextBudget(max_bytes=100))
    assert "上下文预算不足" in str(exc_info.value)
    assert "任务陈述不可截断" in str(exc_info.value)


def test_overflow_when_only_note_left_does_not_fit():
    messages = _round_trips(3)
    pinned = _size(messages[:2])
    with pytest.raises(ContextOverflow) as exc_info:
        compile_context(
            messages, _seqs(len(messages)), ContextBudget(max_bytes=pinned + 50)
        )
    assert "压缩说明" in str(exc_info.value)


def test_non_positive_budget_is_overflow():
    with pytest.raises(ContextOverflow):
        compile_context([_system(), _user()], _seqs(2), ContextBudget(max_bytes=0))


def test_misaligned_source_seqs_is_a_caller_bug():
    with pytest.raises(ContextCompileError):
        compile_context([_system(), _user()], [RUN_START_SEQ], ContextBudget(1024))


def test_negative_spill_threshold_is_rejected():
    with pytest.raises(ContextCompileError):
        compile_context(
            [_system(), _user()],
            _seqs(2),
            ContextBudget(max_bytes=1024, spill_threshold=-1),
        )


# ---------------------------------------------------------------- 7. 接线材料


def test_context_payload_round_trips_budget():
    messages = _round_trips(2)
    budget = ContextBudget(max_bytes=4096, spill_threshold=256)
    view = compile_context(messages, _seqs(len(messages)), budget)
    payload = context_payload(budget, view)

    assert payload["budget_version"] == BUDGET_VERSION
    assert payload["max_bytes"] == 4096
    assert payload["spill_threshold"] == 256
    assert payload["source_seqs"] == view.source_seqs
    assert payload["dropped_seqs"] == view.dropped_seqs
    assert budget_from_payload(payload) == budget


def test_budget_from_payload_rejects_unknown_version():
    with pytest.raises(ContextCompileError):
        budget_from_payload(
            {"budget_version": "ctx-v9", "max_bytes": 1, "spill_threshold": 1}
        )


def test_old_cursor_without_message_seqs_deserializes():
    """F5 之前写下的游标没有 `message_seqs`：必须能原样打开，不能炸。"""
    raw = {
        "messages": [{"role": "user", "content": "旧游标"}],
        "pending_proposals": [],
        "completed_call_ids": ["c0"],
        "approval_ids": [],
        "model_turn": None,
        "budget_ledger": {"turns": 1},
        "resource_epochs": {},
        "output_ref": None,
    }
    cursor = RunCursor.of(raw)
    assert cursor.message_seqs == []
    assert cursor.messages == raw["messages"]
    assert RunCursor.of(cursor.to_dict()).message_seqs == []


def test_progress_drops_misaligned_seqs_instead_of_guessing():
    cursor = RunCursor(
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
        message_seqs=[RUN_START_SEQ],  # 长度对不上：宁可留空，交给事件重建
    )
    assert _Progress.of(cursor).message_seqs == []


def test_message_source_seqs_falls_back_to_events_for_old_cursor(tmp_path):
    """游标里没有 seq 归因（F5 之前写的）时，从事件重建，长度与消息对齐。"""
    ws = _workspace(tmp_path, {"note.md": "原始内容\n"})
    _, result, log, store = _go(
        tmp_path,
        ws,
        [
            ModelResponse(
                "tool_use", tool_calls=[ToolCall("c1", "read_text", {"path": "note.md"})]
            ),
            ModelResponse("end_turn", content="完成"),
        ],
    )
    stack = build_stack(log, data_root=tmp_path / "authority", snapshots=store)
    engine = AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=store,
    )
    stored = stack.tasks.load_cursor(result.run_id)
    assert stored is not None
    progress = _Progress.of(stored[0])
    assert len(progress.message_seqs) == len(progress.messages), (
        "新代码写的游标必须自带对齐的 seq 归因"
    )

    progress.message_seqs = []  # 模拟旧游标
    derived = engine._message_source_seqs(result.run_id, progress)
    assert len(derived) == len(progress.messages)
    assert derived == message_source_seqs(log.read_run(result.run_id))[
        : len(progress.messages)
    ]
    log.close()


def test_message_source_seqs_matches_replay_rebuild():
    """归因规则只有一份：`replay` 直接用这个函数，两边不可能分叉。"""

    def event(seq: int, type_: str) -> Event:
        return Event(
            seq=seq,
            run_id="r1",
            task_id=None,
            type=type_,
            ts=0,
            actor_kind="core",
            actor_id="",
            payload={},
            payload_hash="",
            parent_seq=None,
            idem_key=None,
        )

    events = [
        event(1, "run.start"),
        event(2, "model.intent"),
        event(3, "model.result"),
        event(4, "tool.result"),
        event(5, "model.intent"),
        event(6, "model.result"),
        event(7, "tool.not_dispatched"),
    ]
    assert message_source_seqs(events) == [RUN_START_SEQ, RUN_START_SEQ, 3, 4, 6, 7]


# ---------------------------------------------------------------- 8. loop 接线


class RecordingModel:
    """按脚本返回，同时把每次真正收到的 messages 记下来。

    必须深拷贝：预算内恒等时主循环发出去的就是 `progress.messages` 本体，
    记引用等于记了个会继续长的列表。
    """

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.seen: list[list[dict[str, Any]]] = []

    def capability_profile(self):
        # F9a：准入要求每个模型如实声明能力；没声明会被拒绝，不是"先试试看"。
        return tool_capable_profile("test-double", "RecordingModel")

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        self.seen.append(copy.deepcopy(messages))
        if not self._responses:
            return ModelResponse(finish_reason="end_turn", content="")
        return self._responses.pop(0)


def _workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in files.items():
        (ws / name).write_text(content, encoding="utf-8")
    return ws


def _go(tmp_path: Path, ws: Path, responses, *, limits=None):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    store = SnapshotStore(tmp_path / "artifacts")
    model = RecordingModel(responses)
    result = run(
        task="整理目录里的笔记",
        workspace_dir=ws,
        out_name="summary.md",
        model=model,
        log=log,
        limits=limits,
        snapshots=store,
    )
    return model, result, log, store


def _intents(log: EventLog, run_id: str) -> list[Event]:
    return [e for e in log.read_run(run_id) if e.type == "model.intent"]


def test_loop_records_context_on_every_intent(tmp_path):
    """默认预算：intent 带上 context，且 messages_hash 仍等于全量历史的摘要。"""
    ws = _workspace(tmp_path, {"note.md": "原始内容\n"})
    model, result, log, _ = _go(
        tmp_path,
        ws,
        [
            ModelResponse(
                "tool_use", tool_calls=[ToolCall("c1", "read_text", {"path": "note.md"})]
            ),
            ModelResponse("end_turn", content="完成"),
        ],
    )
    assert result.state in ("succeeded", "noop"), result.error

    intents = _intents(log, result.run_id)
    assert len(intents) == 2
    for event in intents:
        context = event.payload["context"]
        assert context["budget_version"] == BUDGET_VERSION
        assert context["max_bytes"] == Limits().context_budget_bytes
        assert context["spill_threshold"] == TOOL_OUTPUT_SPILL_BYTES
        assert context["dropped_seqs"] == [], "默认预算下不该丢任何消息"

    # 第一轮：历史与 run.start 重建出来的两条完全一致
    first = initial_messages("整理目录里的笔记", "summary.md")
    assert intents[0].payload["messages_hash"] == _digest(first)
    assert intents[0].payload["context"]["source_seqs"] == [
        RUN_START_SEQ,
        RUN_START_SEQ,
    ]

    # 第二轮：模型收到的是完整历史（system/user/assistant/tool 各一条，一条没少），
    # 并且每条消息的归因都能指回写下它的事件。
    assert len(model.seen[1]) == 4
    events = log.read_run(result.run_id)
    result_seq = next(e.seq for e in events if e.type == "model.result")
    tool_seq = next(e.seq for e in events if e.type == "tool.result")
    assert intents[1].payload["context"]["source_seqs"] == [
        RUN_START_SEQ,
        RUN_START_SEQ,
        result_seq,
        tool_seq,
    ]

    report = replay(log, result.run_id)
    assert report.ok, report.problems
    assert report.messages_verified == 2
    log.close()


def test_loop_spills_big_tool_output_and_replay_passes(tmp_path):
    """单条工具输出超 8 KiB：发出去的是占位，原文进内容寻址 blob，replay 仍过。"""
    big = "长" * 5000  # 15000 字节，超过 8 KiB
    ws = _workspace(tmp_path, {"big.md": big})
    model, result, log, store = _go(
        tmp_path,
        ws,
        [
            ModelResponse(
                "tool_use", tool_calls=[ToolCall("c1", "read_text", {"path": "big.md"})]
            ),
            ModelResponse("end_turn", content="完成"),
        ],
    )
    assert result.state in ("succeeded", "noop"), result.error

    sent = model.seen[1][3]["content"]
    assert sent.startswith("[tool-output-spilled]")

    # 原文按 tool.result 的输出重建字节，再核对 blob 真的在盘上
    results = [e for e in log.read_run(result.run_id) if e.type == "tool.result"]
    payload = json.dumps(results[0].payload["output"], ensure_ascii=False, sort_keys=True)
    data = payload.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()

    assert digest in sent
    blob = store.blob_dir / digest
    assert blob.is_file(), "占位说已落盘，就必须真的有这个 blob"
    assert blob.read_bytes() == data

    report = replay(log, result.run_id)
    assert report.ok, report.problems
    log.close()


def test_loop_compacts_under_small_budget_and_still_replays(tmp_path):
    """小预算：真丢历史、真写说明消息，replay 用记录里的参数重算同一个 digest。"""
    ws = _workspace(tmp_path, {"note.md": "长" * 300, "two.md": "短" * 300})
    pinned = _size(
        initial_messages("整理目录里的笔记", "summary.md")
    )
    budget = pinned + 600
    model, result, log, _ = _go(
        tmp_path,
        ws,
        [
            ModelResponse(
                "tool_use", tool_calls=[ToolCall("c1", "read_text", {"path": "note.md"})]
            ),
            ModelResponse(
                "tool_use", tool_calls=[ToolCall("c2", "read_text", {"path": "two.md"})]
            ),
            ModelResponse("end_turn", content="完成"),
        ],
        limits=Limits(
            max_turns=8, max_total_tokens=20_000, context_budget_bytes=budget
        ),
    )
    assert result.state in ("succeeded", "noop"), result.error

    intents = _intents(log, result.run_id)
    assert any(e.payload["context"]["dropped_seqs"] for e in intents), (
        "这个预算下应该真的丢掉过早期的往返"
    )
    last_sent = model.seen[-1]
    assert any(
        str(m.get("content", "")).startswith("[context-compacted]") for m in last_sent
    ), "给模型的上下文里要有那条压缩说明"
    assert len(canonical_bytes(last_sent)) <= budget

    report = replay(log, result.run_id)
    assert report.ok, report.problems
    assert report.messages_verified == len(intents)
    log.close()


def test_loop_context_overflow_records_model_error(tmp_path):
    """预算连 system + 任务都放不下：不调模型，记 model.error，说清是预算问题。"""
    ws = _workspace(tmp_path, {"note.md": "原始内容\n"})
    model, result, log, _ = _go(
        tmp_path,
        ws,
        [ModelResponse("end_turn", content="不该被调用")],
        limits=Limits(
            max_turns=8, max_total_tokens=20_000, context_budget_bytes=200
        ),
    )

    assert result.outcome == "failed"
    assert model.seen == [], "放不下就不该向模型发请求"
    assert result.error and "上下文预算不足" in result.error

    events = log.read_run(result.run_id)
    assert not [e for e in events if e.type == "model.intent"], (
        "拒绝发送时不该留一条指向模型的 intent"
    )
    errors = [e for e in events if e.type == "model.error"]
    assert errors and "上下文预算不足" in errors[0].payload["error"]

    report = replay(log, result.run_id)
    assert report.ok, report.problems
    log.close()


def test_replay_accepts_old_log_without_context(tmp_path):
    """F5 之前的日志（intent 没有 context）走原路径：直接比 messages_hash。"""
    ws = _workspace(tmp_path, {"note.md": "原始内容\n"})
    (tmp_path / "authority").mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "authority")
    task = stack.tasks.create_task("整理目录里的笔记", [str(ws)])
    run_record = stack.tasks.start_run(
        task.task_id, payload={"task": "整理目录里的笔记", "out": ""}
    )
    stack.tasks.transition(run_record.run_id, RUN_RUNNING)

    messages = initial_messages("整理目录里的笔记", None)
    log.append(
        run_record.run_id,
        "model.intent",
        {"request_id": "req_old", "attempt": 1, "messages_hash": _digest(messages)},
        actor_kind="core",
        idem_key="req_old",
    )
    log.append(
        run_record.run_id,
        "model.result",
        {"request_id": "req_old", "finish_reason": "end_turn", "content": "完成"},
        actor_kind="core",
        idem_key="req_old",
    )
    stack.tasks.finish_run(
        run_record.run_id, outcome="succeeded", effect_state="none_applied"
    )

    report = replay(log, run_record.run_id)
    assert report.ok, report.problems
    assert report.messages_verified == 1
    log.close()


def test_replay_detects_tampered_context(tmp_path):
    """context 记的 seq 与实际编译结果不符：replay 必须报出来，而不是放行。"""
    ws = _workspace(tmp_path, {"note.md": "原始内容\n"})
    (tmp_path / "authority").mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "authority")
    task = stack.tasks.create_task("整理目录里的笔记", [str(ws)])
    run_record = stack.tasks.start_run(
        task.task_id, payload={"task": "整理目录里的笔记", "out": ""}
    )
    stack.tasks.transition(run_record.run_id, RUN_RUNNING)

    messages = initial_messages("整理目录里的笔记", None)
    budget = ContextBudget(max_bytes=1_048_576)
    view = compile_context(messages, [RUN_START_SEQ, RUN_START_SEQ], budget)
    forged = context_payload(budget, view)
    forged["source_seqs"] = [999, 999]  # 不可能出现的归因

    log.append(
        run_record.run_id,
        "model.intent",
        {
            "request_id": "req_x",
            "attempt": 1,
            "messages_hash": view.digest,
            "context": forged,
        },
        actor_kind="core",
        idem_key="req_x",
    )
    log.append(
        run_record.run_id,
        "model.result",
        {"request_id": "req_x", "finish_reason": "end_turn", "content": "完成"},
        actor_kind="core",
        idem_key="req_x",
    )
    stack.tasks.finish_run(
        run_record.run_id, outcome="succeeded", effect_state="none_applied"
    )

    report = replay(log, run_record.run_id)
    assert not report.ok
    assert any("source_seqs" in problem for problem in report.problems)
    log.close()


def test_context_view_is_plain_data():
    """ContextView 里只有数据：没有文件句柄、没有时钟、没有回调。"""
    view = compile_context(_histories()[0], _seqs(2), ContextBudget(1_048_576))
    assert isinstance(view, ContextView)
    assert set(vars(view)) == {
        "messages",
        "digest",
        "source_seqs",
        "dropped_seqs",
        "spills",
        "compacted",
    }
