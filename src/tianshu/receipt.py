"""操作凭据（Receipt）。

回答第一个问题：**Agent 到底改了什么？**

一条 Receipt 必须能独立核对——不看对话、不看模型输出，只看它自己。
字段对齐 PRD §3.5：
    receipt_id / run_id / call_id / action / args_hash / resource
    before_state / after_state / backend / evidence / issuer

硬约束：
- 只有 effect_state = applied 才产生 receipt
- 失败与结果未知不得伪造成功（对应「不能冒充」）
- 新建文件的 before_state 显式记为 "absent"，不是空字符串、不是 null
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import canonical_bytes
from .tools import ToolResult

ISSUER = "tianshu/0.1"
BACKEND = "local_fs"


@dataclass
class Receipt:
    receipt_id: str
    run_id: str
    call_id: str
    tool_id: str
    action: str
    args_hash: str
    resource: str
    effect_state: str
    before_state: str  # "absent" | sha256 | "unknown"
    after_state: str | None
    backend: str
    evidence: list[str] = field(default_factory=list)
    issued_at: int = 0
    issuer: str = ISSUER

    def to_payload(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "action": self.action,
            "args_hash": self.args_hash,
            "resource": self.resource,
            "effect_state": self.effect_state,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "backend": self.backend,
            "evidence": self.evidence,
            "issued_at": self.issued_at,
            "issuer": self.issuer,
        }


def build_receipt(
    *,
    run_id: str,
    call_id: str,
    tool_id: str,
    args_hash: str,
    result: ToolResult,
) -> Receipt | None:
    """副作用已发生时产出一条凭据；否则返回 None。

    只认 applied。partial / unknown 也返回 None——它们的事实状态是「不确定」，
    不能被一张看起来完整的凭据粉饰过去，应当留在 tool.result 里等人工核查。
    """
    if result.effect_state != "applied":
        return None

    out = result.output
    resource = str(out.get("path", ""))
    after = out.get("sha256")
    if not resource or not after:
        # applied 但缺路径或哈希：这是执行侧的 bug，宁可炸也不能产出半张凭据
        raise ValueError(
            f"effect_state=applied 但缺少 path/sha256，无法产出凭据：{out!r}"
        )

    return Receipt(
        receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        call_id=call_id,
        tool_id=tool_id,
        action=f"{tool_id}",
        args_hash=args_hash,
        resource=resource,
        effect_state=result.effect_state,
        before_state="absent",  # M1-PR1 只有新建；修改已有文件在 PR2 里实现
        after_state=str(after),
        backend=BACKEND,
        evidence=[f"tool.result:{result.status}"],
        issued_at=int(time.time() * 1000),
    )


def receipts_of(log, run_id: str) -> list[dict[str, Any]]:
    return [e.payload for e in log.read_run(run_id) if e.type == "receipt"]


def verify_receipts(log, run_id: str) -> list[str]:
    """凭据与工具结果互相对账。返回问题列表，空表示一致。"""
    events = log.read_run(run_id)
    results = {
        e.payload["call_id"]: e.payload
        for e in events
        if e.type == "tool.result"
    }
    problems: list[str] = []

    for ev in events:
        if ev.type != "receipt":
            continue
        p = ev.payload
        call_id = p["call_id"]
        res = results.get(call_id)
        if res is None:
            problems.append(f"凭据 {p['receipt_id']} 找不到对应的 tool.result")
            continue
        if res.get("effect_state") != "applied":
            problems.append(
                f"凭据 {p['receipt_id']} 声称 applied，但工具结果是 "
                f"{res.get('effect_state')}"
            )
        actual_after = (res.get("output") or {}).get("sha256")
        if actual_after != p.get("after_state"):
            problems.append(
                f"凭据 {p['receipt_id']} 的 after_state 与工具结果哈希不一致"
            )

    # 反向：所有 applied 的结果都应当有凭据
    receipt_calls = {e.payload["call_id"] for e in events if e.type == "receipt"}
    for call_id, res in results.items():
        if res.get("effect_state") == "applied" and call_id not in receipt_calls:
            problems.append(f"副作用已发生但没有凭据：call_id={call_id}")

    return problems
