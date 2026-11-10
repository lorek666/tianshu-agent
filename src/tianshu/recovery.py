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

from .errors import SnapshotCorrupt, TianshuError, WorkspaceViolation
from .events import EventLog
from .guard import blocked_workspaces, ensure_not_blocked
from .leases import Lease, LeaseStore, execution_guard
from .receipt import build_receipt
from .sandbox import current_backend
from .snapshot import ABSENT, Change, SnapshotStore
from .tools import SPECS, ToolResult, action_for
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
    #: 本次被阻断的 workspace 根。多目录任务里要能看出到底挡的是哪一个。
    blocked_workspaces: list[str] = field(default_factory=list)

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


def _evidence_workspace(change: Change, fallback: Workspace) -> Workspace:
    """这条证据该在哪个目录里核实。

    证据自带 workspace 根时**只信证据**：多 workspace 的 Run 里，另一个目录
    下的同名文件与本条无关，拿调用方的 workspace 去读就是在看错地方。
    旧格式（v2 清单没有 root）只能退回显式传入的 workspace——恢复必须显式给
    目录，这个退回是"用户说了在哪看"，不是猜；真正不能猜的是 undo（写操作）。
    """
    if not change.root:
        return fallback
    return Workspace(Path(change.root))


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
    data_root: Path | None = None,
) -> RecoveryReport:
    """核对并补全一次被打断的 run。幂等。

    `leases` 非空时，恢复会先看"这条路径现在是不是别人在写"。正在被别的活
    Run 占用的路径一律记 unresolved 并阻断后续写入——恢复不做实时观测的猜测。

    `data_root` 给了就**和派发共享同一把本机执行守卫**（复审 中-1）：恢复写
    `recovery.blocked` 与派发真正落盘必须排进同一个全序，否则"查过阻断 → 写入"
    之间还能被插进一条阻断事件，而写入侧看不到它。不给（库调用方保持原样）时
    判定口径不变，只是少了这层跨进程线性化。
    """
    if data_root is None:
        return _recover_run(log, run_id, workspace_dir, snapshots, leases=leases)
    with execution_guard(Path(data_root)):
        return _recover_run(log, run_id, workspace_dir, snapshots, leases=leases)


def _recover_run(
    log: EventLog,
    run_id: str,
    workspace_dir: Path,
    snapshots: SnapshotStore | None = None,
    *,
    leases: LeaseStore | None = None,
) -> RecoveryReport:
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

    # 清单存在但读不出内容：证据**曾经存在过**，只是现在不可信。这比"清单整个
    # 缺失"更糟——不能像"没有证据=没写"那样替任何一条未完成的调用下结论。
    # 一个字节都不猜：全部判 unknown，并阻断到调用方显式给的那个目录。
    if pending and snapshots is not None:
        try:
            snapshots.load(run_id)
        except SnapshotCorrupt as exc:
            for call_id, ev in pending.items():
                payload = ev.payload
                _settle(log, report, run_id, call_id, payload["tool_id"], payload,
                        ToolResult("unknown", "unknown",
                                   {"error": "recovered: 快照清单损坏，无法核实任何"
                                             f"副作用（{exc}）",
                                    "recovered": True}),
                        bucket=report.unresolved)
            _block_workspace(log, report, run_id, str(ws.root), list(pending))
            return report

    for call_id, ev in pending.items():
        payload = ev.payload
        tool_id = payload["tool_id"]
        spec = SPECS.get(tool_id)

        if spec is None:
            # 复审 中-2：tool_id 不在当前工具清单里（更早/更新版本的日志）时，
            # 不能套用"清单里没有条目 → 确定没写"那条推理——它的前提是"我知
            # 道这个工具的写入协议"（先落证据、再写文件）。语义未知的工具没有
            # 这个前提，拿通用 path/content 去猜等于替它宣布"没写"。
            # 无法判定就标 unknown 并阻断，让人去看。
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 未知工具的副作用无法核实"
                                         "（tool_id 不在当前工具清单）",
                                "recovered": True,
                                "tool_id": tool_id}),
                    bucket=report.unresolved)
            continue

        # 只读工具：没有副作用，直接被中断也不影响任何东西
        if spec.side_effect_level == "pure":
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

        # run.start 与 init_run 之间崩溃时尚未产生任何 tool intent，能安全补建
        # 空清单；一旦已有写 intent，缺清单仍是异常，绝不能推断为未写入。
        if not snapshots.has_manifest(run_id):
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 清单缺失，无法确认是否写入",
                                "recovered": True}),
                    bucket=report.unresolved)
            continue

        try:
            change = snapshots.find(run_id, call_id)
        except SnapshotCorrupt as exc:
            # 整份清单在开跑前已核对过一次；这里是"核对之后、逐条取证之前"才
            # 坏掉的窄窗口。同一口径：读不懂就 unknown，不退回"没证据=没写"。
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 快照清单损坏，无法核实"
                                         f"（{exc}）",
                                "recovered": True}),
                    bucket=report.unresolved)
            continue

        if change is None:
            # 清单存在但没有本次调用的条目 → 没进到写前证据那一步 → 确定未写
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("failed", "none_applied",
                               {"error": "recovered: 清单中无本次调用证据，写入未开始",
                                "recovered": True}),
                    bucket=report.none_applied)
            continue

        try:
            obs_ws = _evidence_workspace(change, ws)
        except TianshuError as exc:
            # 证据里的根不可用（目录被删/搬走）：此时读到的任何东西都不能
            # 算作"这个工作区现在是什么"，只能记 unresolved。
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 证据里的 workspace 根不可用"
                                         f"（{exc}），无法核实",
                                "recovered": True,
                                "root": change.root}),
                    bucket=report.unresolved)
            continue

        # 有别的活 Run 正在这条路径上写：现在读到的字节不可信，不下结论。
        # 检查的是**证据指向的那个目录**里的那条路径，不是调用方传进来的目录。
        holder = _foreign_lease(leases, obs_ws, run_id, payload)
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

        current = _observe(obs_ws, change.path)

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
                         "bytes": current_backend().size(obs_ws.resolve(change.path)),
                         "sha256": change.expected_after,
                         "after_state": change.expected_after,
                         "before_state": change.before_state,
                         "recovered": True}),
                    bucket=report.applied, with_receipt=True,
                    # 凭据要能独立核对：根、意图事件 seq、快照 blob 都写进去。
                    # 旧格式记录没有 root 时，用**显式** --workspace 作根；
                    # 这是唯一一处允许"外部给的目录"进证据的地方，而且它只
                    # 描述"在这个目录里看到了什么"，不用于 undo 的落盘决策。
                    receipt_root=str(obs_ws.root),
                    receipt_change=change,
                    intent_seq=ev.seq)
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
        # 阻断要落在**无法核实的那个目录**上：多 workspace 的 Run 里，
        # 拿调用方传进来的 ws 一概阻断会挡错目录，而真正状态不明的目录却
        # 还开着——那是比不阻断更糟的结果。
        for root, call_ids in _unresolved_by_root(
            snapshots, run_id, report.unresolved, ws
        ).items():
            _block_workspace(log, report, run_id, root, call_ids)

    return report


def _block_workspace(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    root: str,
    call_ids: list[str],
) -> None:
    """把"这个目录里有核实不了的副作用"落成事件并记进报告。

    阻断事件是 append-only 的：它不会被后面的"已解除"覆盖，解除由
    `guard.unblock` 再追加一条，两者一起才算出当前状态。
    """
    report.blocked_workspaces.append(root)
    log.append(
        run_id,
        "recovery.blocked",
        {
            "workspace": root,
            "call_ids": call_ids,
            "reason": "存在无法核实的副作用，阻断后续写入",
        },
        actor_kind="system",
    )


def _unresolved_by_root(
    snapshots: SnapshotStore | None,
    run_id: str,
    unresolved: list[str],
    fallback: Workspace,
) -> dict[str, list[str]]:
    """无法核实的调用分别属于哪个目录（根 → call_id 列表，按根排序）。

    旧格式记录（没有 root）只能算调用方显式给的目录；除此之外一切都按证据
    自己的根分组——每个目录的阻断事件只列属于它自己的那些调用。
    """
    grouped: dict[str, list[str]] = {}
    for call_id in unresolved:
        try:
            change = snapshots.find(run_id, call_id) if snapshots is not None else None
        except SnapshotCorrupt:
            # 清单在这条路径上坏掉时，唯一还能说的根是调用方显式给的那个：
            # 宁可阻断这个目录（可能偏保守），也不能因为读不懂就什么都不挡。
            change = None
        root = change.root if change is not None and change.root else str(fallback.root)
        grouped.setdefault(root, []).append(call_id)
    return dict(sorted(grouped.items()))


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
    receipt_root: str | None = None,
    receipt_change: Change | None = None,
    intent_seq: int | None = None,
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
        if receipt_root is None or receipt_change is None or intent_seq is None:
            # 内部装配错误：凭据缺任何一条引用都不可独立核对。宁可炸在这里，
            # 也不要往日志里塞一张"看起来完整"的凭据。
            raise TianshuError(
                "恢复补凭据缺少必要引用（root / 快照条目 / intent seq）"
            )
        change = receipt_change
        receipt = build_receipt(
            run_id=run_id,
            call_id=call_id,
            tool_id=tool_id,
            args_hash=intent_payload.get("args_hash", ""),
            result=result,
            backend=current_backend().name,
            root=receipt_root,
            resource=str(Workspace(Path(receipt_root)).resolve(change.path)),
            action=action_for(tool_id),
            intent_seq=intent_seq,
            blob_ref=change.blob_ref,
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
