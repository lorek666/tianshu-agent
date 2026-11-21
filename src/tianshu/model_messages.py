"""loop 与离线 replay 共用的中立 assistant 消息构造。"""

from __future__ import annotations

import json
from typing import Any

from .continuation import ContinuationRef


def assistant_message_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ref = None
    if "continuation" in payload:
        ref = ContinuationRef.from_payload(payload["continuation"])
        ref.validate_projection(payload)
    message: dict[str, Any] = {"role": "assistant", "content": payload.get("content", "")}
    calls = payload.get("tool_calls") or []
    if calls:
        message["tool_calls"] = [
            {
                "id": c["call_id"], "type": "function",
                "function": {
                    "name": c["name"],
                    # 无 ref 的历史字符串保留原 json.dumps 行为，不重写旧 hash。
                    "arguments": c["arguments"]
                    if ref is not None and isinstance(c["arguments"], str)
                    else json.dumps(c["arguments"], ensure_ascii=False, sort_keys=True),
                },
            }
            for c in calls
        ]
    if ref is not None:
        message["continuation"] = ref.to_payload()
    return message
