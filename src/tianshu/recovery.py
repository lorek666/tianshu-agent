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

from .errors import WorkspaceViolation
from .events import EventLog
from .guard import blocked_workspaces, ensure_not_blocked
from .leases import Lease, LeaseStore
from .receipt import build_receipt
from .sandbox import current_backend
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
    """工作区当前的写后状态。经后端读——恢复与写入必须看同一个世界。"""
    target = ws.resolve(rel)
    io = current_backend()
    if not io.exists(target):
        return ABSENT
    return _sha(io.read_bytes(target)[0])


def _foreign_lease(
    leases: LeaseStore | None, ws: Workspace, run_id: str, payload: dict
) -> Lease | None:
    """这条路径（或它所在的工作区）此刻是不是被**另一个活着的 Run**占着。

    为什么恢复要在意这个：恢复的判据是"磁盘现在是什么"。如果别人正在写这条
    路径，读到的可能是半截内容，据此判成 applied/unknown 都是在编造结论。
    宁可报 unresolved 并继续阻断，也不要在一个正在变的文件上做判断。

    租约过期、持有进程已死、pid 被复用的都不算占用——否则一次崩溃就会让恢复
    永久卡在无法核实的状态。
    """
    if leases is None:
        return None
    candidates: list[Path] = [ws.root]
    args = payload.get("args") or {}
    raw = args.get("path") or args.get("name")
    if isinstance(raw, str) and raw:
        try:
            candidates.append(ws.resolve(raw))
        except WorkspaceViolation:
            pass
    for path in candidates:
        lease = leases.live_holder(path)
        if lease is not None and lease.run_id != run_id:
            return lease
    return None


def recover_run(
    log: EventLog,
    run_id: str,
    workspace_dir: Path,
    snapshots: SnapshotStore | None = None,
    *,
    leases: LeaseStore | None = None,
) -> RecoveryReport:
    """核对并补全一次被打断的 run。幂等。

    `leases` 非空时，恢复会先看"这条路径现在是不是别人在写"。正在被别的活
    Run 占用的路径一律记 unresolved 并阻断后续写入——恢复不做实时观测的猜测。
    """
    ws = Workspace(workspace_dir)
    events = log.read_run(run_id)

    intents = {e.payload["call_id"]: e for e in events if e.type == "tool.intent"}
    results = {e.payload["call_id"] for e in events if e.type == "tool.result"}
    pending = {cid: ev for cid, ev in intents.items() if cid not in results}

    if (snapshots is not None and not snapshots.has_manifest(run_id)
            and len(events) == 1 and events[0].type == "run.start"):
        # 最早的启动窗口：尚无 tool intent，不可能已经发生受控文件写入。
        snapshots.init_run(run_id)

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

        # 有别的活 Run 正在这条路径上写：现在读到的字节不可信，不下结论
        holder = _foreign_lease(leases, ws, run_id, payload)
        if holder is not None:
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 该路径正被另一个 Run 占用"
                                         "（租约有效），不做实时判断",
                                "recovered": True,
                                "held_by_run": holder.run_id,
                                "held_by_instance": holder.owner_instance_id}),
                    bucket=report.unresolved)
            continue

        # 写工具：唯一判据是写前证据
        if snapshots is None:
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 未提供快照存储，无法核实",
                                "recovered": True}),
                    bucket=report.unresolved)
            continue

        # run.start 与 init_run 之间崩溃时尚未产生任何 tool intent，能安全补建
        # 空清单；一旦已有写 intent，缺清单仍是异常，绝不能推断为未写入。
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
                         "bytes": current_backend().size(ws.resolve(change.path)),
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
            backend=current_backend().name,
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
