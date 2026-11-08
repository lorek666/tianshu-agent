"""崩溃恢复（v2：只认写前证据）。

上一版用「磁盘现在像不像执行后的样子」推断「本次调用执行过」。
这在文件本来就有相同内容时必然误报成功——哈希相同不能证明写者是谁，
更不能证明这次调用发生过。

v2 的判据只有一条：**写前证据是否存在**。
证据（preimage + expected_after，以 call_id 为键）在写文件之前原子落盘，
所以：
- 没有证据 → 本次调用根本没进入写入阶段 → `none_applied`
- 有证据 → 用证据里的 before/after 对磁盘做三方比对 → applied / none_applied / unknown

其余一律 unknown，绝不编造 before_state，绝不生成成功凭据。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .events import EventLog
from .guard import blocked_workspaces, ensure_not_blocked
from .receipt import build_receipt
from .snapshot import ABSENT, SnapshotStore
from .tools import SPECS, ToolResult
from .workspace import Workspace

__all__ = [
    "RecoveryReport",
    "recover_run",
    "blocked_workspaces",
    "ensure_not_blocked",
]


@dataclass
class RecoveryReport:
    run_id: str
    applied: list[str] = field(default_factory=list)
    none_applied: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved and not self.problems


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _observe(ws: Workspace, rel: str) -> str:
    target = ws.resolve(rel)
    if not target.exists():
        return ABSENT
    return _sha(target.read_bytes())


def recover_run(
    log: EventLog,
    run_id: str,
    workspace_dir: Path,
    snapshots: SnapshotStore | None = None,
) -> RecoveryReport:
    """核对并补全一次被打断的 run。幂等。"""
    ws = Workspace(workspace_dir)
    events = log.read_run(run_id)

    intents = {e.payload["call_id"]: e for e in events if e.type == "tool.intent"}
    results = {e.payload["call_id"] for e in events if e.type == "tool.result"}
    pending = {cid: ev for cid, ev in intents.items() if cid not in results}

    report = RecoveryReport(run_id=run_id)

    for call_id, ev in pending.items():
        payload = ev.payload
        tool_id = payload["tool_id"]
        spec = SPECS.get(tool_id)

        # 只读工具：没有副作用，直接被中断也不影响任何东西
        if spec is not None and spec.side_effect_level == "pure":
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("failed", "none_applied",
                               {"error": "recovered: 只读工具无副作用",
                                "recovered": True}),
                    bucket=report.none_applied)
            continue

        # 写工具：唯一判据是写前证据
        if snapshots is None:
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 未提供快照存储，无法核实",
                                "recovered": True}),
                    bucket=report.unresolved)
            continue

        # 清单不存在 = 异常（正常 run 启动时就建了空清单）。
        # 不能解释成「什么都没写」——那会把有副作用的 intent 判成未执行。
        if not snapshots.has_manifest(run_id):
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 清单缺失，无法确认是否写入",
                                "recovered": True}),
                    bucket=report.unresolved)
            continue

        change = snapshots.find(run_id, call_id)

        if change is None:
            # 清单存在但没有本次调用的条目 → 没进到写前证据那一步 → 确定未写
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("failed", "none_applied",
                               {"error": "recovered: 清单中无本次调用证据，写入未开始",
                                "recovered": True}),
                    bucket=report.none_applied)
            continue

        current = _observe(ws, change.path)

        if change.before_state == change.expected_after:
            # 写前写后内容一致：没有任何可观测副作用。不声称成功、也不阻断。
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult(
                        "failed", "none_applied",
                        {"error": "recovered: 写入前后内容一致，无可观测副作用",
                         "recovered": True}),
                    bucket=report.none_applied)
            continue

        if current == change.before_state:
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("failed", "none_applied",
                               {"error": "recovered: 内容仍为写前状态，未执行",
                                "recovered": True}),
                    bucket=report.none_applied)
            continue

        if current == change.expected_after:
            # 有证据 + 磁盘与证据一致 → 可以确认执行过，且写前状态来自证据本身
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult(
                        "succeeded", "applied",
                        {"path": change.path,
                         "bytes": (ws.resolve(change.path)).stat().st_size,
                         "sha256": change.expected_after,
                         "after_state": change.expected_after,
                         "before_state": change.before_state,
                         "recovered": True}),
                    bucket=report.applied, with_receipt=True)
            continue

        # 既不是写前也不是写后：被第三方改过，或写到一半
        _settle(log, report, run_id, call_id, tool_id, payload,
                ToolResult("unknown", "unknown",
                           {"error": "recovered: 磁盘状态既非写前也非写后，需人工核查",
                            "recovered": True,
                            "observed": current,
                            "expected_after": change.expected_after,
                            "before_state": change.before_state}),
                bucket=report.unresolved)

    if report.unresolved:
        log.append(
            run_id,
            "recovery.blocked",
            {
                "workspace": str(ws.root),
                "call_ids": report.unresolved,
                "reason": "存在无法核实的副作用，阻断后续写入",
            },
            actor_kind="system",
        )

    return report


def _settle(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    call_id: str,
    tool_id: str,
    intent_payload: dict,
    result: ToolResult,
    *,
    bucket: list[str],
    with_receipt: bool = False,
) -> None:
    batch = [{
        "run_id": run_id,
        "type": "tool.result",
        "payload": {
            "call_id": call_id,
            "tool_id": tool_id,
            "status": result.status,
            "effect_state": result.effect_state,
            "output": result.output,
        },
        "actor_kind": "system",
        "idem_key": call_id,
    }]

    if with_receipt:
        receipt = build_receipt(
            run_id=run_id,
            call_id=call_id,
            tool_id=tool_id,
            args_hash=intent_payload.get("args_hash", ""),
            result=result,
        )
        if receipt is not None:
            batch.append({
                "run_id": run_id,
                "type": "receipt",
                "payload": receipt.to_payload(),
                "actor_kind": "system",
                "idem_key": receipt.receipt_id,
            })

    log.append_many(batch)
    bucket.append(call_id)
