"""只从不可变事件还原 Responses cursor/turn；不发事件、不调用模型或工具。"""

from __future__ import annotations

import json
from typing import Any

from .errors import TianshuError
from .events import Event, canonical_bytes, sha256_hex
from .model_messages import assistant_message_from_payload
from .tool_registry import current_registry
from .tools import SUBTASK_TOOL_IDS


def rebuild_responses_progress(events: list[Event]):
    # Loop 初始化函数没有 I/O；局部导入避开 TaskStore/Loop 装配环。
    from .loop import _fresh_progress

    if not any(
        (e.type == "model.result" and "continuation" in e.payload)
        or (e.type == "model.intent" and
            (e.payload.get("context") or {}).get("budget_version") == "ctx-responses-v1")
        for e in events
    ):
        return None
    start = next(e for e in events if e.type == "run.start")
    progress = _fresh_progress(start.payload["task"], start.payload.get("out") or None)
    turns: list[tuple[Event, dict[str, Any]]] = []
    result: dict[str, Any] | None = None
    last_cursor: Event | None = None
    for ev in events:
        p = ev.payload
        if ev.type == "model.intent":
            progress.model_attempts += 1
        elif ev.type == "model.result":
            result = p
            progress.turns += 1
            progress.messages.append(assistant_message_from_payload(p))
            progress.message_seqs.append(ev.seq)
            inp, out = (p["usage"].get(k) for k in ("input_tokens", "output_tokens"))
            progress.tokens += (inp or 0) + (out or 0)
            progress.unknown_usage_calls += int(inp is None or out is None)
            cost = (p.get("cost") or {}).get("cost")
            if cost is None:
                progress.unknown_cost_calls += 1
            else:
                progress.cost_total_micros += cost
            progress.model_turn = {
                "turn_id": f"t{progress.turns}",
                **{k: v for k, v in p.items() if k != "cost"},
            }
            progress.pending = []
            for call in p.get("tool_calls", []):
                if p["finish_reason"] != "tool_use" or not isinstance(call["arguments"], dict):
                    continue
                spec = current_registry().spec(call["name"])
                # 已拒提案在后面的 tool.not_dispatched 事实中移除。
                progress.pending.append({
                    "call_id": call["call_id"], "tool_id": call["name"],
                    "kind": "write" if spec is None or spec.side_effect_level != "pure"
                    or call["name"] in SUBTASK_TOOL_IDS else "read",
                    "args": call["arguments"], "request_id": None, "approval_id": None,
                    "attempt_id": 1, "args_hash": sha256_hex(canonical_bytes(call["arguments"])),
                })
        elif ev.type == "run.model_turn":
            if result is None or progress.model_turn is None:
                raise TianshuError("Responses turn 缺少持久化模型结果")
            turn = {**progress.model_turn, "turn_id": p["turn_id"]}
            if sha256_hex(canonical_bytes(turn)) != p["payload_hash"]:
                raise TianshuError("Responses turn 与持久化模型结果摘要不一致")
            turns.append((ev, turn))
            if turn["finish_reason"] == "tool_use" and not progress.pending:
                progress.model_turn = None
        elif ev.type in ("tool.result", "tool.not_dispatched"):
            output = p.get("output", {}) if ev.type == "tool.result" else {
                "error": f"未派发：{p.get('reason')}"
            }
            progress.messages.append({
                "role": "tool", "tool_call_id": p["call_id"], "name": p.get("tool_id", ""),
                "content": json.dumps(output, ensure_ascii=False, sort_keys=True),
            })
            progress.message_seqs.append(ev.seq)
            progress.pending = [
                prop for prop in progress.pending if prop["call_id"] != p["call_id"]
            ]
            if ev.type == "tool.result":
                progress.completed.append(p["call_id"])
                progress.n_applied += int(p.get("effect_state") == "applied")
                progress.n_unknown += int(p.get("effect_state") in ("partial", "unknown"))
                if not progress.pending:
                    progress.model_turn = None
        elif ev.type == "job.parked" and p.get("reason") == "waiting_children":
            progress.model_turn = None
        elif ev.type == "approval.requested":
            if p["approval_id"] not in progress.approvals:
                progress.approvals.append(p["approval_id"])
        elif ev.type == "run.cursor_saved":
            last_cursor = ev
    return progress.to_cursor(), turns, last_cursor or start
