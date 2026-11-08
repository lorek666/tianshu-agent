"""崩溃恢复。

回答第三个问题：**进程在中间被杀掉，重启后怎么知道上一次到底改了没改？**

核心思路：不猜。用磁盘现状和事件里的参数去**核实**。
- 能核实 → 补一条 result，标明是恢复出来的
- 核实不了 → 标 unknown，并阻断依赖它的后续写入
- 绝不把"不知道"写成"成功"
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import EventLog
from .receipt import Receipt, build_receipt
from .snapshot import ABSENT, SnapshotStore
from .tools import SPECS, ToolResult
from .workspace import Workspace


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


def _current_state(ws: Workspace, rel: str) -> str:
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
    """核对并补全一次被打断的 run。幂等：重复调用不会再补事件。"""
    ws = Workspace(workspace_dir)
    events = log.read_run(run_id)

    intents = {
        e.payload["call_id"]: e
        for e in events
        if e.type == "tool.intent"
    }
    results = {e.payload["call_id"] for e in events if e.type == "tool.result"}

    pending = {cid: ev for cid, ev in intents.items() if cid not in results}
    report = RecoveryReport(run_id=run_id)

    for call_id, ev in pending.items():
        payload = ev.payload
        tool_id = payload["tool_id"]
        args = payload.get("args") or {}
        rel = args.get("name") or args.get("path")
        content = args.get("content")

        # 只读工具没有副作用，无论何时被杀都不需要核查
        spec = SPECS.get(tool_id)
        if spec is not None and spec.side_effect_level == "pure":
            result = ToolResult(
                "failed", "none_applied",
                {"error": "recovered: 只读工具无副作用", "recovered": True},
            )
            _record_result(log, run_id, call_id, tool_id, result)
            report.none_applied.append(call_id)
            continue

        if not isinstance(rel, str) or not isinstance(content, str):
            # 写工具但参数不完整：无法核实 → 记 unknown 并阻断。
            # 但必须补一条 result，否则这条 intent 会永远悬空（intent/result 不配对）。
            result = ToolResult(
                "unknown", "unknown",
                {"error": "recovered: 参数不可重建，无法判定", "recovered": True},
            )
            _record_result(log, run_id, call_id, tool_id, result)
            report.unresolved.append(call_id)
            report.problems.append(f"{call_id}：参数不可重建，无法核实")
            continue

        expected_after = _sha(content.encode("utf-8"))
        current = _current_state(ws, rel)

        if current == expected_after:
            # 副作用确实发生了：补 result + 凭据
            result = ToolResult(
                "succeeded",
                "applied",
                {
                    "path": rel,
                    "bytes": len(content.encode("utf-8")),
                    "sha256": expected_after,
                    "after_state": expected_after,
                    "before_state": ABSENT,
                    "recovered": True,
                },
            )
            _record_result(log, run_id, call_id, tool_id, result)
            _record_receipt(log, run_id, call_id, tool_id,
                            payload.get("args_hash", ""), result)
            report.applied.append(call_id)

        elif current == ABSENT:
            # 文件不在：新建类工具肯定没写成功，修改类需要写前状态才能确认
            result = ToolResult(
                "failed", "none_applied",
                {"error": "recovered: 未执行（目标不存在）", "recovered": True},
            )
            _record_result(log, run_id, call_id, tool_id, result)
            report.none_applied.append(call_id)

        else:
            # 别的哈希：可能是没执行（原内容），也可能被别人改过——分不清
            before = None
            if snapshots is not None:
                for c in snapshots.load(run_id):
                    if c.path == rel and c.before_state != ABSENT:
                        before = c.before_state
            if before is not None and current == before:
                result = ToolResult(
                    "failed", "none_applied",
                    {"error": "recovered: 未执行（内容仍为写前状态）", "recovered": True},
                )
                _record_result(log, run_id, call_id, tool_id, result)
                report.none_applied.append(call_id)
            else:
                result = ToolResult(
                    "unknown", "unknown",
                    {"error": "recovered: 无法判定是否已写入，需人工核查",
                     "recovered": True},
                )
                _record_result(log, run_id, call_id, tool_id, result)
                report.unresolved.append(call_id)

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


def _record_result(log, run_id, call_id, tool_id, result: ToolResult) -> None:
    log.append(
        run_id,
        "tool.result",
        {
            "call_id": call_id,
            "tool_id": tool_id,
            "status": result.status,
            "effect_state": result.effect_state,
            "output": result.output,
        },
        actor_kind="system",
        idem_key=call_id,
    )


def _record_receipt(log, run_id, call_id, tool_id, args_hash, result) -> None:
    receipt = build_receipt(
        run_id=run_id,
        call_id=call_id,
        tool_id=tool_id,
        args_hash=args_hash,
        result=result,
    )
    if receipt is not None:
        log.append(run_id, "receipt", receipt.to_payload(),
                   actor_kind="system", idem_key=receipt.receipt_id)


def blocked_workspaces(log: EventLog) -> set[str]:
    """哪些 workspace 上有未核实的副作用。新 run 写这些目录前必须先核查。"""
    out: set[str] = set()
    rows = log._conn.execute(
        "SELECT payload FROM events WHERE type = 'recovery.blocked'"
    ).fetchall()
    import json as _json

    for r in rows:
        try:
            out.add(_json.loads(r["payload"])["workspace"])
        except Exception:
            continue
    return out


def ensure_not_blocked(log: EventLog, workspace_dir: Path) -> None:
    ws = str(Path(workspace_dir).resolve())
    if ws in blocked_workspaces(log):
        raise RuntimeError(
            f"workspace 存在未核实的副作用（{ws}），"
            "请先处理 unknown 记录再写入"
        )
