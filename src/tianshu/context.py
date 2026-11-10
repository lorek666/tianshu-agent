"""上下文编译（F5 / PRD §7 P0）：把「完整历史」编译成「这一轮真正发出去的字节」。

问题不在"要不要压缩"，而在**压缩能不能被复现**。每次 `model.intent` 都要按第一
原则回答"当时到底发了什么"，否则崩溃重放就只能说"我们大概发过一些东西"。所以
这里把编译做成**纯函数**：

    同输入（messages + source_seqs + budget）→ 同输出（messages + digest + spills）

不读时钟、不取随机数、不写盘。要落盘的原文只作为清单返回给调用方
（`ContextView.spills`），由主循环在发请求前写入内容寻址 blob。

**为什么用字节而不是 token**：本项目零第三方依赖，手上没有 tokenizer。任何
"估算 token"都是假装知道一个它其实不知道的数——按"4 字符≈1 token"估出来的预算
在中文与 emoji 上会偏得离谱，最后既没防住 400，又让"这次为什么被压"无法解释。
字节数是可以精确复现的量：同一串 UTF-8 文本，任何机器上 `len(encode())` 都一样，
中文 3 字节、emoji 4 字节不用猜。

**为什么压缩是确定性摘要而不是让模型总结**：模型总结不可重放。同一个 run 回放
时总结内容可能变（采样、模型版本、网关），`messages_hash` 就再也对不上——那等于
把"事件日志足以重建终态"这条契约换成"大概能重建"。这里宁可少一点信息（确定性
占位 + 可落盘的原文），也不引入一个让回放失效的自由变量。

**分区预算的取舍顺序**（PRD §7 F5「分区预算」）：

1. `system` 与首条 `user`（任务陈述）**永不动**。它们放不下就抛 `ContextOverflow`，
   绝不截断任务陈述；
2. 单条工具输出超过 `spill_threshold`（默认 8 KiB）→ 原文进 `spills`，内容换成
   确定性占位（blob sha256 + 字节数 + 头尾各 512 字节预览）；
3. 仍超 `max_bytes` → 从**最旧的工具往返**开始牺牲：先把它的工具输出降到更短的
   预览，再把整段往返丢掉；
4. 被丢掉的整段合并成**一条**说明消息（列出被丢事件的 seq 与条数），不悄悄丢。

「往返」= 一条 assistant 消息加上紧跟其后的工具结果。整段丢，不拆对：把
`tool_calls` 和它的 `tool` 结果拆开，OpenAI 与 Anthropic 都会直接 400。

**预算内恒等**：没有超阈值的工具输出、且总字节没超预算时，返回的 `messages`
就是入参那个对象（深相等），`digest == sha256(canonical_bytes(messages))`。
这条不是优化，是兼容性要求：它保证默认预算下的 run 与 F5 之前逐字节一致，
历史事件日志里 `messages_hash` 的含义不变。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError
from .events import canonical_bytes

BUDGET_VERSION = "ctx-v1"

#: 单条工具输出超过这个字节数就落盘（PRD §7 F5）。
TOOL_OUTPUT_SPILL_BYTES = 8 * 1024

#: 落盘占位里头部/尾部预览各留多少字节。
PREVIEW_BYTES = 512

#: 预算 squeeze 阶段降级预览的字节上限。比落盘占位更短，因为它是在"实在放不下"
#: 时才用的第二档：目标是把字节数真的降下来，而不是保留可读性。
COMPACT_PREVIEW_BYTES = 160

#: `source_seqs` 里"由 `run.start` 派生"的哨兵值。
#:
#: 事件 `seq` 从 1 开始（SQLite AUTOINCREMENT），所以 0 不会与任何真实事件冲突。
#: 头两条消息（system + 任务陈述）由 `run.start` 的 payload 重建，没有属于自己的
#: 消息事件；用 0 表示"来源是 run.start"，比伪造一个 seq 诚实。
RUN_START_SEQ = 0

#: 压缩说明里最多列出多少个被丢的 seq。超过就标"等共 N 条"——说明本身的字节数
#: 必须是**有界**的，否则"丢得越多、说明越长"会让预算收敛失去保证。
NOTE_SEQ_LIMIT = 32

#: 默认预算：1 MiB。刻意宽松——日常 run 走恒等路径，压缩只在小预算或长 run 时生效。
DEFAULT_MAX_BYTES = 1_048_576


class ContextOverflow(TianshuError):
    """上下文预算不足。

    "不足"指**不可再让**的部分放不下：system + 任务陈述（以及压到只剩说明消息时
    仍然超）。这不是可以降级处理的错误——把任务陈述截断，模型就在解一道被改过的题。
    """


class ContextCompileError(TianshuError):
    """编译入参不成立（例如 source_seqs 与 messages 不平行）。调用方的 bug。"""


@dataclass(frozen=True)
class ContextBudget:
    max_bytes: int
    spill_threshold: int = TOOL_OUTPUT_SPILL_BYTES


@dataclass(frozen=True)
class ContextView:
    """一次编译的结果。纯数据：调用方拿它发请求、记账、落盘。

    `source_seqs` 与 `messages` 的平行关系有一条例外：发生压缩时，说明消息是
    compile 造出来的，**不对应任何事件**，因此 `len(source_seqs) == len(messages) - 1`。
    这比给它编一个假 seq 更诚实：回放时能一眼看出"这一条没有来源"。
    """

    messages: list[dict[str, Any]]
    digest: str
    source_seqs: list[int]
    dropped_seqs: list[int]
    #: (sha256, 原文 UTF-8 字节)。compile 不写盘，落盘是调用方的事。
    spills: list[tuple[str, bytes]] = field(default_factory=list)
    compacted: bool = False


def compile_context(
    messages: list[dict[str, Any]],
    source_seqs: list[int],
    budget: ContextBudget,
) -> ContextView:
    """把完整历史编译成本轮要发的视图。纯函数，同输入必得同输出。"""
    _validate(messages, source_seqs, budget)

    pinned = _pinned_count(messages)
    pinned_bytes = len(canonical_bytes(messages[:pinned]))
    if pinned_bytes > budget.max_bytes:
        raise ContextOverflow(
            f"上下文预算不足：system + 任务陈述已占 {pinned_bytes}B，"
            f"超出预算 {budget.max_bytes}B。任务陈述不可截断，请调大预算"
        )

    # ---- 1) 工具输出落盘（与总预算无关：超阈值就替换，原文进 spills）----
    out: list[dict[str, Any]] = list(messages)
    spills: list[tuple[str, bytes]] = []
    registered: set[str] = set()
    #: 被替换过的工具消息 index → 原始字节。后面降级预览时，占位里的 sha256
    #: 必须仍然指向**原文**，而不是指向第一层占位文本——否则读者顺着哈希取回来
    #: 的是一段"内容已落盘"的说明，不是内容。
    origins: dict[int, bytes] = {}

    def register(data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        if digest not in registered:
            registered.add(digest)
            spills.append((digest, data))
        return digest

    for index in range(pinned, len(out)):
        data = _tool_content_bytes(out[index])
        if data is None or len(data) <= budget.spill_threshold:
            continue
        origins[index] = data
        out[index] = {**out[index], "content": _spill_stub(register(data), data)}

    # ---- 2) 预算内恒等（除落盘替换外）----
    if len(canonical_bytes(out)) <= budget.max_bytes:
        final = out if spills else messages
        return ContextView(
            messages=final,
            digest=hashlib.sha256(canonical_bytes(final)).hexdigest(),
            source_seqs=list(source_seqs),
            dropped_seqs=[],
            spills=spills,
            compacted=bool(spills),
        )

    # ---- 3) 分区预算：从最旧的往返开始牺牲 ----
    groups = _round_trips(out, pinned)
    sacrificed: set[int] = set()
    previewed: set[int] = set()
    dropped_seqs: list[int] = []

    while True:
        view, view_seqs = _assemble(
            out, source_seqs, pinned, groups, sacrificed, dropped_seqs
        )
        if len(canonical_bytes(view)) <= budget.max_bytes:
            break

        victim = next((i for i in range(len(groups)) if i not in sacrificed), None)
        if victim is None:
            # 只剩 system + 任务陈述 + 一条说明消息，还是放不下。
            raise ContextOverflow(
                f"上下文预算不足：丢弃全部 {len(dropped_seqs)} 条消息、"
                f"只留 system + 任务陈述 + 压缩说明后仍需 "
                f"{len(canonical_bytes(view))}B，超出预算 {budget.max_bytes}B"
            )

        if victim not in previewed:
            previewed.add(victim)
            if _preview_tools(out, groups[victim], origins, register):
                # 降级成功：重新量一次，还超再整段丢。
                continue

        sacrificed.add(victim)
        start, end = groups[victim]
        dropped_seqs.extend(source_seqs[start:end])

    return ContextView(
        messages=view,
        digest=hashlib.sha256(canonical_bytes(view)).hexdigest(),
        source_seqs=view_seqs,
        dropped_seqs=dropped_seqs,
        spills=spills,
        compacted=True,
    )


# ---------------------------------------------------------------- 给接线方用


def context_payload(
    budget: ContextBudget,
    view: ContextView,
    *,
    memories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """写进 `model.intent.payload.context` 的那份记录。

    正常情况下只有这五个键：回放要能**仅凭事件**重算出同一个 digest，所以记录
    的必须恰好是编译的入参与产物身份。`spills` 不进来——它的原文是工具输出，
    已经在 `tool.result` 里了；这里再记一份只会让日志翻倍。记忆进入视图时多
    一个 `memories` 身份清单（见下），它不参与 digest 重算。

    `memories` 是 F10 §8 的"实际选中的 `memory_id`/`revision` + 内容快照引用"：
    只在真的有记忆进入这一轮视图时才出现。它记录的是**编译后仍然活着**的那些
    记忆（被预算压掉的不算"进入上下文"），供回放核对"这一轮到底带了什么"。
    它不是第二份记忆正文——正文与出处本来就在 `tool.result` 里，这里只记身份。
    """
    payload: dict[str, Any] = {
        "budget_version": BUDGET_VERSION,
        "max_bytes": budget.max_bytes,
        "spill_threshold": budget.spill_threshold,
        "source_seqs": list(view.source_seqs),
        "dropped_seqs": list(view.dropped_seqs),
    }
    if memories:
        payload["memories"] = list(memories)
    return payload


def budget_from_payload(payload: dict[str, Any]) -> ContextBudget:
    """`context_payload` 的逆运算，给回放用。字段缺失/类型不对一律报错。"""
    version = payload.get("budget_version")
    if version != BUDGET_VERSION:
        raise ContextCompileError(
            f"未知的上下文预算版本：{version!r}（本实现只认 {BUDGET_VERSION}）"
        )
    try:
        max_bytes = int(payload["max_bytes"])
        spill_threshold = int(payload["spill_threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextCompileError(f"context 记录缺字段或类型不对：{exc}") from exc
    return ContextBudget(max_bytes=max_bytes, spill_threshold=spill_threshold)


# ---------------------------------------------------------------- 内部实现


def _validate(
    messages: list[dict[str, Any]], source_seqs: list[int], budget: ContextBudget
) -> None:
    if len(messages) != len(source_seqs):
        raise ContextCompileError(
            f"source_seqs 必须与 messages 平行：{len(messages)} 条消息 vs "
            f"{len(source_seqs)} 个 seq"
        )
    if budget.max_bytes <= 0:
        raise ContextOverflow(f"上下文预算必须为正：max_bytes={budget.max_bytes}")
    if budget.spill_threshold < 0:
        raise ContextCompileError(
            f"spill_threshold 不能为负：{budget.spill_threshold}"
        )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ContextCompileError(f"第 {index} 条消息不是对象")
        if not isinstance(message.get("role"), str):
            raise ContextCompileError(f"第 {index} 条消息没有字符串 role")
    if any(not isinstance(seq, int) or isinstance(seq, bool) for seq in source_seqs):
        raise ContextCompileError("source_seqs 必须是整数 seq")


def _pinned_count(messages: list[dict[str, Any]]) -> int:
    """不可再让的前缀长度：system，以及紧随其后的首条 user（任务陈述）。"""
    pinned = 0
    if messages and messages[0].get("role") == "system":
        pinned = 1
    if pinned < len(messages) and messages[pinned].get("role") == "user":
        pinned += 1
    return pinned


def _tool_content_bytes(message: dict[str, Any]) -> bytes | None:
    """只有 `role == "tool"` 的 content 参与落盘。其他角色一律原样。"""
    if message.get("role") != "tool":
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    return content.encode("utf-8")


def _round_trips(
    messages: list[dict[str, Any]], start: int
) -> list[tuple[int, int]]:
    """把不可让前缀之后的消息切成「往返」段，返回 [start, end) 列表。

    每条 assistant 开一段，其余消息（tool / 额外的 user）并进当前段。这样
    `assistant.tool_calls` 与它的 `tool` 结果**永远在同一段**里——整段丢或整段留，
    不存在"拆开一对"的中间态。
    """
    groups: list[tuple[int, int]] = []
    for index in range(start, len(messages)):
        if messages[index].get("role") == "assistant" or not groups:
            groups.append((index, index + 1))
        else:
            begin, _ = groups[-1]
            groups[-1] = (begin, index + 1)
    return groups


def _assemble(
    messages: list[dict[str, Any]],
    source_seqs: list[int],
    pinned: int,
    groups: list[tuple[int, int]],
    sacrificed: set[int],
    dropped_seqs: list[int],
) -> tuple[list[dict[str, Any]], list[int]]:
    """按当前的牺牲集合拼出视图，以及与之平行的 source_seqs。

    说明消息紧跟不可让前缀，位置就是"第一段被丢掉的往返"原来的位置。
    它不对应任何事件，所以 seq 侧不占位。
    """
    view = list(messages[:pinned])
    seqs = list(source_seqs[:pinned])
    if dropped_seqs:
        view.append({"role": "user", "content": _compact_note(dropped_seqs)})
    for index, (start, end) in enumerate(groups):
        if index in sacrificed:
            continue
        view.extend(messages[start:end])
        seqs.extend(source_seqs[start:end])
    return view, seqs


def _preview_tools(
    messages: list[dict[str, Any]],
    span: tuple[int, int],
    origins: dict[int, bytes],
    register: Callable[[bytes], str],
) -> bool:
    """把一段里的工具输出降到更短的确定性预览。返回是否有改动。

    只替换"真的变短"的那些：一条 90 字节的输出换成 200 字节的预览是负优化，
    会让"压缩"反而离预算更远。

    预览的 sha256 / 字节数一律指向**原文**（`origins`）：哪怕这条内容已经先被
    第一层占位替换过，读者顺着哈希取回来的仍然是工具输出的原文。
    """
    changed = False
    for index in range(*span):
        current = _tool_content_bytes(messages[index])
        if current is None:
            continue
        origin = origins.get(index)
        if origin is None:
            origin = current
            origins[index] = origin
        stub = _preview_stub(hashlib.sha256(origin).hexdigest(), origin)
        if len(stub.encode("utf-8")) >= len(current):
            continue
        register(origin)
        messages[index] = {**messages[index], "content": stub}
        changed = True
    return changed


def _compact_note(dropped_seqs: list[int]) -> str:
    """被丢掉历史的确定性说明。不含时间戳、不含随机数、不含模型输出。"""
    listed = ",".join(str(seq) for seq in dropped_seqs[:NOTE_SEQ_LIMIT])
    tail = "" if len(dropped_seqs) <= NOTE_SEQ_LIMIT else f"…等共 {len(dropped_seqs)} 条"
    return (
        f"[context-compacted] 为适配上下文预算（{BUDGET_VERSION}），已丢弃最早的 "
        f"{len(dropped_seqs)} 条消息（事件 seq：{listed}{tail}）。"
        "这些消息的原始事件仍在事件日志中，可回放核对；"
        "本说明由 compile_context 确定性生成。"
    )


def _spill_stub(digest: str, data: bytes) -> str:
    """落盘占位：blob 身份 + 精确字节数 + 头尾预览。"""
    return (
        "[tool-output-spilled]\n"
        f"sha256: {digest}\n"
        f"原始字节数: {len(data)}\n"
        f"头 {PREVIEW_BYTES} 字节预览:\n"
        f"{_preview(data[:PREVIEW_BYTES], PREVIEW_BYTES)}\n"
        f"尾 {PREVIEW_BYTES} 字节预览:\n"
        f"{_preview(data[-PREVIEW_BYTES:], PREVIEW_BYTES)}\n"
        "完整内容已按内容寻址落盘；本占位由 compile_context 确定性生成，不含时间戳。"
    )


def _preview_stub(digest: str, data: bytes) -> str:
    """预算 squeeze 用的更短占位。"""
    return (
        f"[tool-output-previewed sha256={digest} bytes={len(data)}]\n"
        f"{_preview(data, COMPACT_PREVIEW_BYTES)}"
    )


def _preview(data: bytes, limit: int) -> str:
    """按 UTF-8 解码前 `limit` 字节。

    截断点落在多字节字符中间时，尾部那几个残缺字节会被丢掉（`errors="ignore"`）：
    预览本来就是有损的，而**原文一字不差地在 spills 里**，需要时可以取回。
    这里刻意不用 `errors="replace"`：那会在合法的中文/emoji 截断处插入替换符，
    让"预览"看起来像内容坏了。
    """
    return data[:limit].decode("utf-8", "ignore")


__all__ = [
    "BUDGET_VERSION",
    "COMPACT_PREVIEW_BYTES",
    "DEFAULT_MAX_BYTES",
    "NOTE_SEQ_LIMIT",
    "PREVIEW_BYTES",
    "RUN_START_SEQ",
    "TOOL_OUTPUT_SPILL_BYTES",
    "ContextBudget",
    "ContextCompileError",
    "ContextOverflow",
    "ContextView",
    "budget_from_payload",
    "compile_context",
    "context_payload",
]
