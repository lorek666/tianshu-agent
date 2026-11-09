"""回放（S4 的判定器）。

不重新调用模型、不重新执行工具副作用。只做三件事：
  1. 校验 seq 连续、payload_hash 与内容一致（验收 A8）
  2. 按事件重建消息序列，与每条 model.intent 记录的 messages_hash 比对
  3. 用 tool.intent 里记录的参数重算产物哈希，与 tool.result / run.end 三方核对

能把这三件事对上，才叫「事件日志足以重建终态」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError
from .events import EventLog, canonical_bytes
from .loop import SYSTEM_PROMPT


@dataclass
class ReplayReport:
    run_id: str
    ok: bool
    events: int
    turns: int
    messages_verified: int
    artifact_sha256: str | None
    problems: list[str] = field(default_factory=list)


def replay(log: EventLog, run_id: str) -> ReplayReport:
    events = log.read_run(run_id)  # verify=True：篡改在这里就会抛
    if not events:
        raise TianshuError(f"没有找到 run {run_id} 的事件")

    problems: list[str] = []

    # ---- 1. seq 单调且日志无缺口 ----
    #
    # M0 时一个库只有一条 Run，所以"该 Run 的 seq 连续"等价于"日志连续"。
    # M2 之后 task.* / 控制 Run 的事件会和同一个 Run 的事件交织，按 run 过滤
    # 出来的 seq 必然有洞——那不是损坏。真正的不变量是两条：本 Run 的 seq 严格
    # 递增（没有重放/回退），以及全局日志在 [首, 尾] 区间内没有缺号（没有丢事件）。
    seqs = [e.seq for e in events]
    if seqs != sorted(set(seqs)):
        problems.append("seq 不单调：存在重复或倒序的事件")
    present = log.seqs_in_range(seqs[0], seqs[-1])
    gaps = sorted(set(range(seqs[0], seqs[-1] + 1)) - present)
    if gaps:
        problems.append(f"全局事件日志存在缺号，缺口：{gaps[:10]}")

    # ---- 2. 重建消息序列 ----
    start = events[0]
    if start.type != "run.start":
        problems.append(f"首条事件不是 run.start，而是 {start.type}")

    start_text = start.payload.get("task", "")
    out_name = start.payload.get("out", "")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{start_text}\n\n输出文件：{out_name}"
            if out_name
            else start_text,
        },
    ]

    verified = 0
    turns = 0
    artifact_sha: str | None = None

    for ev in events:
        if ev.type == "model.intent":
            recorded = ev.payload.get("messages_hash")
            actual = hashlib.sha256(canonical_bytes(messages)).hexdigest()
            if recorded != actual:
                problems.append(
                    f"seq={ev.seq} messages_hash 不符：重建的消息序列与首次执行不同"
                )
            else:
                verified += 1
            turns += 1

        elif ev.type == "model.result":
            p = ev.payload
            assistant: dict[str, Any] = {"role": "assistant", "content": p.get("content", "")}
            tcs = p.get("tool_calls") or []
            if tcs:
                assistant["tool_calls"] = [
                    {
                        "id": c["call_id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(
                                c["arguments"], ensure_ascii=False, sort_keys=True),
                        },
                    }
                    for c in tcs
                ]
            messages.append(assistant)

        elif ev.type == "tool.result":
            p = ev.payload
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": p.get("call_id"),
                    "name": p.get("tool_id", ""),
                    "content": json.dumps(p.get("output", {}), ensure_ascii=False, sort_keys=True),
                }
            )
            if p.get("status") == "succeeded":
                out = p.get("output") or {}
                if "sha256" in out:
                    artifact_sha = out["sha256"]

        elif ev.type == "tool.not_dispatched":
            p = ev.payload
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": p.get("call_id"),
                    "name": p.get("tool_id", ""),
                    "content": json.dumps(
                        {"error": f"未派发：{p.get('reason')}"},
                        ensure_ascii=False, sort_keys=True),
                }
            )

        elif ev.type == "run.end":
            reported = (ev.payload.get("artifact") or {}).get("sha256")
            if reported and artifact_sha and reported != artifact_sha:
                problems.append(
                    f"run.end 记录的产物哈希 {reported[:12]}… 与 tool.result 的 "
                    f"{artifact_sha[:12]}… 不一致"
                )
            if reported:
                artifact_sha = reported

    # ---- 3. 用 tool.intent 的参数重算产物哈希 ----
    #
    # 逐调用核对，不是逐 Run：一次执行可以新建多个文件（M2 的一轮里可以有多个
    # create_report），拿"整个 Run 最后一个产物"去核每一个 intent，会让第二个
    # 文件必然对不上——那种红是假红，会掩盖真正的篡改。
    outputs = {
        ev.payload.get("call_id"): (ev.payload.get("output") or {})
        for ev in events
        if ev.type == "tool.result"
    }
    for ev in events:
        if ev.type != "tool.intent" or ev.payload.get("tool_id") != "create_report":
            continue
        content = (ev.payload.get("args") or {}).get("content")
        if not isinstance(content, str):
            problems.append(f"seq={ev.seq} create_report 的参数不可重建")
            continue
        recomputed = hashlib.sha256(content.encode("utf-8")).hexdigest()
        recorded = outputs.get(ev.payload.get("call_id"), {}).get("sha256")
        if recorded and recomputed != recorded:
            problems.append(
                f"seq={ev.seq} 用事件参数重算的产物哈希 {recomputed[:12]}… 与 "
                f"tool.result 记录的 {recorded[:12]}… 不一致"
            )

    return ReplayReport(
        run_id=run_id,
        ok=not problems,
        events=len(events),
        turns=turns,
        messages_verified=verified,
        artifact_sha256=artifact_sha,
        problems=problems,
    )
