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
from typing import Any

from . import authority
from .authority import AuthorityUnavailable, AuthorityWriteFailed
from .errors import EventLogCorrupt, SnapshotCorrupt, TianshuError, WorkspaceViolation
from .events import EventLog
from .guard import (
    blocked_workspaces,
    ensure_not_blocked,
    mirror_blocks_to_authority,
    scan_blocked,
)
from .leases import Lease, LeaseStore, execution_guard
from .memory_exec import MEMORY_TOOL_IDS
from .receipt import build_receipt
from .sandbox import current_backend
from .snapshot import ABSENT, Change, SnapshotStore
from .tools import SPECS, ToolResult, action_for
from .workspace import Workspace

__all__ = [
    "RecoveryReport",
    "RunAssessment",
    "assess_run",
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
    #: 本次确认"未生效"的记忆写入（投影/事件层可证明）。单独列出来，不混进
    #: 文件写统计。
    memory_not_applied: list[str] = field(default_factory=list)
    #: 记忆写里**不能**宣布"未生效"的那一类：`memory.erase` 删正文文件发生在
    #: 提交事务之外且不可逆，崩溃窗口里可能"文件已删、行未标记"。这类只报
    #: unknown，不报 none_applied，也不阻断目录（记忆没有目录 preimage）。
    memory_unresolved: list[str] = field(default_factory=list)
    #: 恢复时清掉的孤儿正文 blob 的 `content_ref`（写正文在提交之前落盘，
    #: 崩溃会留下没有投影行引用的字节）。
    memory_orphan_blobs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.unresolved
            and not self.memory_unresolved
            and not self.problems
        )


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


@dataclass(frozen=True)
class RunAssessment:
    """接管前对账的只读结论（issue #61.2）。

    它是 `recover_run` 的**只读前身**：只回答"这条被打断的 run 现在能不能被
    别人安全接手"，不补事件、不写凭据、不阻断、不落盘。阻断由调用方在拒绝
    接管时显式登记（否则"只读对账"就变成了"顺手改状态"）。

    六个状态对应规格要求：
    - `applied` / `none_applied`：能确定的效果；
    - `unknown`：磁盘既不是写前也不是写后（写了一半 / 被第三方改过）；
    - `unresolved`：无法完成对账（未知工具、证据根不可用、清单读不出……）；
    - `corrupt`：快照清单存在但坏了（证据不可信）；
    - `missing_manifest`：有写入 intent 却没有清单（"写了却没清单"）。
    """

    run_id: str
    applied: tuple[str, ...] = ()
    none_applied: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    corrupt: bool = False
    missing_manifest: bool = False
    #: 本次对账覆盖的待决写调用数（含只读之外的写工具）。
    pending_writes: int = 0
    problems: tuple[str, ...] = ()

    @property
    def blocks_takeover(self) -> bool:
        """能不能接手：有未核实副作用 / 坏清单 / 写了却没清单 → 不允许。"""
        return (
            bool(self.unresolved)
            or bool(self.unknown)
            or self.corrupt
            or (self.missing_manifest and self.pending_writes > 0)
        )

    def to_payload(self) -> dict[str, Any]:
        """写进审计事件的形状（`run.control` 的 `checked` 字段）。"""
        return {
            "applied": list(self.applied),
            "none_applied": list(self.none_applied),
            "unresolved": list(self.unresolved),
            "unknown": list(self.unknown),
            "corrupt": self.corrupt,
            "missing_manifest": self.missing_manifest,
            "pending_writes": self.pending_writes,
        }


def _read_only_snapshot_store(data_root: Path) -> SnapshotStore:
    """只读快照视图：复用 `SnapshotStore` 的解析/校验，但**不走构造函数**。

    `SnapshotStore.__init__` 会 `mkdir` 两个目录——对账必须只读，不能在别人的
    数据根上顺手建目录。这里用 `__new__` 挂上同样的三个属性，`load/has_manifest`
    只用 `manifest_dir`，既拿到同一套 `SnapshotCorrupt` 校验，又不落盘。
    """
    artifacts = Path(data_root) / "artifacts"
    store = SnapshotStore.__new__(SnapshotStore)
    store.root = artifacts
    store.blob_dir = artifacts / "blobs"
    store.manifest_dir = artifacts / "snapshots"
    return store


def assess_run(
    data_root: Path | str, run_id: str, *, log: EventLog | None = None
) -> RunAssessment:
    """接管前的**只读**对账（不写事件、不落盘、不阻断）。

    和 `recover_run` 的关键区别：这里不下结论、不补 `tool.result`、不写
    `recovery.blocked`。它只回答"前任留下的状态能不能被安全接手"，供租约接管
    的 `reconcile` 回调使用；拒绝时由调用方登记权威根阻断。

    清单损坏/缺失必须**返回状态而不是抛异常**（接管方要据此拒绝，不是崩溃）。
    事件日志本身读不出来时返回 `corrupt=True`——校验器自己崩了不算"干净"。
    """
    root = Path(data_root)
    own_log = None
    if log is None:
        db = root / "events.db"
        if not db.exists():
            raise TianshuError(
                f"无法对账：数据根里没有事件库（{root}）；"
                "调用方应传入自己的 EventLog，避免只读对账创建/迁移库文件"
            )
        log = EventLog(db, blob_dir=root / "blobs")
        own_log = log
    try:
        events = log.read_run(run_id)
    except EventLogCorrupt as exc:
        return RunAssessment(
            run_id=run_id,
            corrupt=True,
            problems=(f"事件日志读不出来，无法对账：{exc}",),
        )
    finally:
        if own_log is not None:
            own_log.close()

    intents = {e.payload["call_id"]: e for e in events if e.type == "tool.intent"}
    results = {e.payload["call_id"] for e in events if e.type == "tool.result"}
    pending = {cid: ev for cid, ev in intents.items() if cid not in results}

    applied: list[str] = []
    none_applied: list[str] = []
    unresolved: list[str] = []
    unknown: list[str] = []
    problems: list[str] = []
    corrupt = False
    missing_manifest = False
    pending_writes = 0
    store = _read_only_snapshot_store(root)
    manifest_checked = False
    manifest_state = "unknown"  # unknown / ok / missing / corrupt
    changes: list[Change] | None = None

    for call_id, ev in pending.items():
        payload = ev.payload
        tool_id = payload["tool_id"]
        spec = SPECS.get(tool_id)
        if spec is None:
            # 语义未知的工具：不能替它假设"没写"（与 recover 同一口径）。
            unresolved.append(call_id)
            continue
        if spec.side_effect_level == "pure":
            none_applied.append(call_id)
            continue
        if tool_id in MEMORY_TOOL_IDS:
            # 大部分记忆写没有文件副作用可对账：result 与投影同事务，没有
            # result 就是没生效。不占 pending_writes，不影响接管判定。
            #
            # **例外是 `memory.erase`**：它在提交事务之外先删正文文件（F10 §10
            # 的删除顺序），崩溃窗口里"文件已删、行未标记"是可能发生的不可逆
            # 部分生效。只读对账拿不到投影时**必须**报 unresolved（阻断接管），
            # 不能替它宣布没生效。
            if tool_id == _ERASE_TOOL_ID:
                # 擦除的删文件在提交之外且不可逆：看不到投影时只能报 unknown，
                # 不能报"没生效"。`unknown` 同样让接管被拒（保守正确）。
                unknown.append(call_id)
                continue
            none_applied.append(call_id)
            continue

        pending_writes += 1
        if not manifest_checked:
            manifest_checked = True
            if store.has_manifest(run_id):
                try:
                    changes = store.load(run_id)
                    manifest_state = "ok"
                except SnapshotCorrupt as exc:
                    corrupt = True
                    manifest_state = "corrupt"
                    problems.append(f"快照清单损坏，无法核实任何副作用（{exc}）")
            else:
                manifest_state = "missing"
        if manifest_state == "missing":
            missing_manifest = True
            unresolved.append(call_id)
            continue
        if manifest_state == "corrupt":
            unresolved.append(call_id)
            continue
        assert changes is not None  # manifest_state == "ok" 时必然已加载

        change = next((c for c in changes if c.call_id == call_id), None)
        if change is None:
            # 清单存在但没有本次条目 → 没进入写前证据阶段 → 确定未写。
            none_applied.append(call_id)
            continue
        if change.before_state == change.expected_after:
            none_applied.append(call_id)
            continue
        if not change.root:
            # 旧格式证据没有 workspace 根：无法定位是哪个目录，不猜。
            unresolved.append(call_id)
            continue
        try:
            ws = Workspace(Path(change.root))
            current = _observe(ws, change.path)
        except (TianshuError, OSError) as exc:
            unresolved.append(call_id)
            problems.append(f"{call_id}：证据所在目录不可用（{exc}）")
            continue
        if current == change.before_state:
            none_applied.append(call_id)
        elif current == change.expected_after:
            applied.append(call_id)
        else:
            # 既非写前也非写后：被 kill 在写一半、或被第三方改过 → 不可接手。
            unknown.append(call_id)

    return RunAssessment(
        run_id=run_id,
        applied=tuple(applied),
        none_applied=tuple(none_applied),
        unresolved=tuple(unresolved),
        unknown=tuple(unknown),
        corrupt=corrupt,
        missing_manifest=missing_manifest,
        pending_writes=pending_writes,
        problems=tuple(problems),
    )


def _block_unfinished_undo(
    log: EventLog, data_root: Path, workspace_dir: Path
) -> list[str]:
    """未完成的撤销必须挡住对应工作区（undo journal 的恢复侧接线）。

    journal 记录"撤销做到哪一步"。它 `in_progress`（崩溃在动作之间）或读不懂时，
    那些目录的写前状态还没结算完——在此之上继续写，等于在"改了一半"的地板上施工。
    这里只做**报告 + 阻断**：不续做、不动文件、不猜；续做由 `tianshu undo --run-id`
    （幂等）完成，解除阻断由人在核对后显式 `unblock`。

    `completed` 的 journal 不挡：文件已全部还原，只差一条完成事件（幂等补写），
    没有"改了一半"的风险。
    """
    # 延迟 import：undo_journal ↔ snapshot/undo 有依赖，模块级 import 容易成环。
    from . import undo_journal

    try:
        pending = undo_journal.outstanding(data_root, log=log)
    except Exception as exc:  # noqa: BLE001 - 扫描器自己崩了不能当成"没有未完成撤销"
        return [
            f"撤销意图日志扫描失败（{type(exc).__name__}: {exc}）；"
            "无法确认是否存在未完成的撤销——按最坏情况处理"
        ]

    problems: list[str] = []
    active = set(scan_blocked(log).blocked)
    fallback = str(Path(workspace_dir).resolve())
    for item in pending:
        if item.corrupt:
            roots = tuple(item.roots) or (fallback,)
            detail = "撤销意图日志损坏，无法判断撤销做到哪一步"
        elif item.status == undo_journal.STATUS_IN_PROGRESS:
            roots = tuple(item.roots) or (fallback,)
            detail = (
                f"撤销未完成（{item.done_count}/{item.total} 个动作已完成）"
            )
        else:
            continue
        reason = f"{detail}：阻断后续写入，先续做或人工核对撤销"
        problems.append(
            f"{detail}：{item.journal_path}（run={item.run_id}）；"
            f"相关目录已阻断，请先 `tianshu undo --run-id {item.run_id}` 续做"
            "（或人工核对后 unblock）"
        )
        for root in roots:
            if root in active:
                continue
            active.add(root)
            log.append(
                item.run_id,
                "recovery.blocked",
                {"workspace": root, "call_ids": [], "reason": reason},
                actor_kind="system",
            )
            try:
                authority.register_blocked(root, run_id=item.run_id, reason=reason)
            except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
                problems.append(f"跨数据根阻断未生效：{root}（{exc}）")
    return problems


def recover_run(
    log: EventLog,
    run_id: str,
    workspace_dir: Path,
    snapshots: SnapshotStore | None = None,
    *,
    leases: LeaseStore | None = None,
    data_root: Path | None = None,
    memory: object | None = None,
    blob_dir: Path | None = None,
) -> RecoveryReport:
    """核对并补全一次被打断的 run。幂等。

    `leases` 非空时，恢复会先看"这条路径现在是不是别人在写"。正在被别的活
    Run 占用的路径一律记 unresolved 并阻断后续写入——恢复不做实时观测的猜测。

    `data_root` 给了就**和派发共享同一把本机执行守卫**（复审 中-1）：恢复写
    `recovery.blocked` 与派发真正落盘必须排进同一个全序，否则"查过阻断 → 写入"
    之间还能被插进一条阻断事件，而写入侧看不到它。不给（库调用方保持原样）时
    判定口径不变，只是少了这层跨进程线性化。

    每次恢复都先做一次**权威根补登记**（issue #61.1）：本数据根日志里当前有效
    的阻断，如果用户级权威根里还没有（旧版本的库、或换过数据根），先补进去。
    补登记失败只在报告里写「跨数据根阻断未生效」，不炸恢复流程。
    """
    migration_problems = mirror_blocks_to_authority(log)
    undo_problems: list[str] = []
    if data_root is None:
        report = _recover_run(
            log, run_id, workspace_dir, snapshots, leases=leases,
            memory=memory, blob_dir=blob_dir,
        )
    else:
        with execution_guard(Path(data_root)):
            report = _recover_run(
                log, run_id, workspace_dir, snapshots, leases=leases,
                memory=memory, blob_dir=blob_dir,
            )
            # 未完成的撤销也在这个全序里判：它和"恢复判 unknown"是同一类事实
            # （磁盘上有一批文件的最终状态还没结算），写入侧必须同时看到。
            undo_problems = _block_unfinished_undo(
                log, Path(data_root), Path(workspace_dir)
            )
    report.problems.extend(migration_problems)
    report.problems.extend(undo_problems)
    return report


def _recover_run(
    log: EventLog,
    run_id: str,
    workspace_dir: Path,
    snapshots: SnapshotStore | None = None,
    *,
    leases: LeaseStore | None = None,
    memory: object | None = None,
    blob_dir: Path | None = None,
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
    # 一个字节都不猜：写工具与语义未知的工具全部判 unknown 并阻断到调用方显式
    # 给的那个目录。只读工具不读这份证据（它本来就没有副作用），结论不受影响，
    # 否则会把一个只是"清单文件坏了"的目录额外挡上——过度阻断也是误报。
    if pending and snapshots is not None:
        try:
            snapshots.load(run_id)
        except SnapshotCorrupt as exc:
            unverified: list[str] = []
            for call_id, ev in pending.items():
                payload = ev.payload
                tool_id = payload["tool_id"]
                spec = SPECS.get(tool_id)
                if tool_id in MEMORY_TOOL_IDS:
                    # 记忆写的结论不依赖这份快照清单（投影/事件同事务），
                    # 清单坏了也不该把它们推成 unknown 并挡住目录。
                    _settle_memory_write(
                        log, report, run_id, call_id, tool_id, payload, ev.ts,
                        memory=memory, blob_dir=blob_dir,
                    )
                    continue
                if spec is not None and spec.side_effect_level == "pure":
                    _settle(log, report, run_id, call_id, tool_id, payload,
                            ToolResult("failed", "none_applied",
                                       {"error": "recovered: 只读工具无副作用",
                                        "recovered": True}),
                            bucket=report.none_applied)
                    continue
                unverified.append(call_id)
                _settle(log, report, run_id, call_id, tool_id, payload,
                        ToolResult("unknown", "unknown",
                                   {"error": "recovered: 快照清单损坏，无法核实任何"
                                             f"副作用（{exc}）",
                                    "recovered": True}),
                        bucket=report.unresolved)
            if unverified:
                _block_workspace(log, report, run_id, str(ws.root), unverified)
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

        # 记忆写：见 `_settle_memory_write`（含 erase 的不可逆窗口、孤儿 blob）
        if tool_id in MEMORY_TOOL_IDS:
            _settle_memory_write(
                log, report, run_id, call_id, tool_id, payload, ev.ts,
                memory=memory, blob_dir=blob_dir,
            )
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
            #
            # 删除类动作（`delete_file`）的写后状态是 ABSENT：文件已经被删掉，
            # 再去 `size()` 它只会抛 FileNotFoundError，裸异常穿出恢复入口。
            # 恢复对 delete 的判据只能是「写前内容仍在」或「已删除」——这里
            # 走的是后者，output 与活体 `_delete_file` 保持同形状（after_state
            # 显式是 absent，不是空串也不是 0 字节文件）。
            if change.expected_after == ABSENT:
                output = {
                    "path": change.path,
                    "deleted": True,
                    "after_state": ABSENT,
                    "before_state": change.before_state,
                    "recovered": True,
                }
            else:
                output = {
                    "path": change.path,
                    "bytes": current_backend().size(obs_ws.resolve(change.path)),
                    "sha256": change.expected_after,
                    "after_state": change.expected_after,
                    "before_state": change.before_state,
                    "recovered": True,
                }
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("succeeded", "applied", output),
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

    同时登记**用户级权威根**（issue #61.1）：只写本数据根日志的话，换个
    `--db` 就看不见这条阻断。权威根写失败不能假装成功——在报告里如实写
    「跨数据根阻断未生效」，本数据根这一侧仍然是阻断的（日志已经写了）。
    """
    report.blocked_workspaces.append(root)
    reason = "存在无法核实的副作用，阻断后续写入"
    log.append(
        run_id,
        "recovery.blocked",
        {
            "workspace": root,
            "call_ids": call_ids,
            "reason": reason,
        },
        actor_kind="system",
    )
    try:
        authority.register_blocked(root, run_id=run_id, reason=reason)
    except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
        report.problems.append(f"跨数据根阻断未生效：{root}（{exc}）")


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


#: 记忆写里唯一"副作用在提交事务之外且不可逆"的工具（先删正文文件、再提交）。
_ERASE_TOOL_ID = "memory.erase"


def _open_memory_projection(log: EventLog, blob_dir: Path | None = None):
    """恢复用的记忆投影（与事件日志共用同一连接）。打不开就返回 None。

    `blob_dir` 要给装配方自定义的正文目录：恢复按 DB 旁默认目录去找 blob 时，
    自定义目录下的正文会"找不到"，擦除结论与孤儿清理都会偏。CLI / daemon 直接
    把 `stack.memory` 传进来就是为了避开这个坑。

    延迟 import：`memory` → `resources` 那条链不该在纯文件恢复路径上也被拖进来。
    """
    try:
        from .memory import MemoryProjection
    except ImportError:  # pragma: no cover - 装配缺模块
        return None
    try:
        projection = MemoryProjection(log.conn, log=log, blob_dir=blob_dir)
        projection.ensure_schema()
    except Exception:  # noqa: BLE001 - 投影坏了：结论只能是"核不了"
        return None
    return projection


def _clean_orphan_blob(projection: Any, content_ref: str) -> bool:
    """清掉"正文已落盘、但没有任何投影行引用"的孤儿 blob。

    内容型记忆写（propose / remember / edit）在提交事务**之前**就把正文写进
    `memory_blobs/`（F10 §3.1 的内容寻址）；崩溃在提交前就留下一个没有任何行
    指向的字节副本——既不进召回，`memory.erase` 也找不到它。恢复时按引用计数
    为 0 才删，绝不碰别的记忆共享的正文。

    只在 `blob_dir` 真实存在（非纯内存库）时动手；失败一律当成"没清掉"返回
    False，让报告如实留白，不在这里抛错打断恢复。
    """
    try:
        path = projection.blob_path(content_ref)
    except Exception:  # noqa: BLE001 - 引用形状不合法：不是我们写的 blob
        return False
    if path is None or not path.is_file():
        return False
    try:
        others, _ = projection.count_content_ref(content_ref, exclude_memory_id="")
    except Exception:  # noqa: BLE001 - 数不清引用就不删（宁可留字节，不可误删）
        return False
    if others:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _memory_effect(
    log: EventLog,
    intent_payload: dict,
    *,
    memory: object | None = None,
    blob_dir: Path | None = None,
) -> tuple[str, dict[str, Any], str | None]:
    """一条"有 intent 无 result"的记忆写，到底能下什么结论。

    返回 `(bucket, output_extras, problem)`：`bucket` 是 `"none_applied"` 或
    `"unknown"`；`problem` 非空时要写进报告。

    - 投影更新与 `tool.result` 同事务 ⇒ 除 `memory.erase` 外的记忆写**可证明**
      未生效（事件层与投影层都没有半条记录）；
    - `memory.erase` 先删正文文件、后提交，崩溃窗口里可能部分生效：文件还在 →
      确实没生效；文件没了 → 不可逆部分生效，只能报 unknown；
    - 正文 blob 写在提交之前 ⇒ 恢复顺手清掉本调用留下的孤儿副本（引用计数为 0）。
    """
    raw_args = intent_payload.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    projection = (
        memory
        if memory is not None
        else _open_memory_projection(log, blob_dir)
    )
    tool_id = intent_payload.get("tool_id")
    if projection is None:
        # **非 erase** 的结论不依赖投影（投影/事件同事务，没有 result 就是没写），
        # 投影打不开只影响"能不能顺手清孤儿 blob"。
        if tool_id != _ERASE_TOOL_ID:
            return (
                "none_applied",
                {"error": "recovered: 记忆写入未生效（结果与投影同事务）",
                 "recovered": True, "memory_write": tool_id},
                "记忆投影打不开：可证明这次写没生效，但孤儿正文副本无法清查，需人工核对",
            )
        return (
            "unknown",
            {"error": "recovered: 记忆投影不可用，无法核实这次擦除",
             "recovered": True},
            "擦除的副作用在提交之外且不可逆；投影打不开就无法判断，需人工核对",
        )
    if tool_id == _ERASE_TOOL_ID:
        memory_id = args.get("memory_id")
        observed = (
            projection.content_ref_of(memory_id)
            if isinstance(memory_id, str) and memory_id
            else None
        )
        if observed is None:
            # 投影里没有这一行：可能是"本来就擦不到"，也可能是"投影被重建时丢了"。
            # 后者无法证明"一个字节都没删"，所以不宣布没生效，报 unknown 让人核对。
            return (
                "unknown",
                {"error": "recovered: 投影里没有这条记忆，无法证明擦除没有删掉任何正文",
                 "recovered": True, "memory_write": tool_id},
                "擦除待决但投影中找不到该记忆行（可能是投影重建丢失），需人工核对",
            )
        content_ref, erased_at = observed
        if erased_at is not None:
            # 行已被**更早的**擦除标掉（它有自己的 result）：本次调用不可能再产生
            # 新副作用，最多是"重复擦除被拒前崩溃"。
            return (
                "none_applied",
                {"error": "recovered: 该记忆已被更早的擦除标记，本次没有新副作用",
                 "recovered": True, "memory_write": tool_id},
                None,
            )
        path = projection.blob_path(content_ref)
        if path is None:
            # 纯内存库 / 没有正文目录：擦除从不落文件副作用。
            return (
                "none_applied",
                {"error": "recovered: 没有正文目录，擦除没有文件副作用",
                 "recovered": True, "memory_write": tool_id},
                None,
            )
        if path.is_file():
            return (
                "none_applied",
                {"error": "recovered: 正文文件仍在，擦除未生效",
                 "recovered": True, "memory_write": tool_id},
                None,
            )
        return (
            "unknown",
            {"error": "recovered: 正文文件已删除（不可逆），但结果未提交、"
                      "行未标记 erased_at；这次擦除部分生效",
             "recovered": True, "memory_write": tool_id},
            None,
        )
    # 其余记忆写：投影/事件层可证明未生效。
    extras: dict[str, Any] = {
        "error": "recovered: 记忆写入未生效（结果与投影同事务，没有 result 就是没写）",
        "recovered": True,
        "memory_write": tool_id,
    }
    raw_ref = args.get("content_sha256")
    if isinstance(raw_ref, str) and _clean_orphan_blob(projection, raw_ref):
        extras["orphan_blob_removed"] = raw_ref
    return ("none_applied", extras, None)


def _settle_memory_write(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    call_id: str,
    tool_id: str,
    intent_payload: dict,
    ts: int,
    *,
    memory: object | None = None,
    blob_dir: Path | None = None,
) -> None:
    """结算一条被打断的记忆写（含 `dispatches` 表状态推进）。

    恢复不只在事件里说"这次没成"，也要让 `dispatch_record()` 别继续显示
    `intent` / `executing`——否则后续 resume / 审计会看到"仍在执行"与"已确认
    未生效"两种说法。
    """
    bucket_name, extras, problem = _memory_effect(
        log, intent_payload, memory=memory, blob_dir=blob_dir
    )
    if bucket_name == "none_applied":
        bucket = report.none_applied
        report.memory_not_applied.append(call_id)
    else:
        # **不**放进 `report.unresolved`：那个列表在 `_recover_run` 末尾会被拿去
        # 阻断"无法核实的目录"，而记忆写没有目录可言（硬挡一个无关目录比不挡
        # 更糟）。单独成栏、并让 `ok` 变假，人能看到这次擦除没结算。
        bucket = report.memory_unresolved
    orphan = extras.pop("orphan_blob_removed", None)
    if isinstance(orphan, str):
        report.memory_orphan_blobs.append(orphan)
    if problem:
        report.problems.append(f"{call_id}：{problem}")
    result = ToolResult(
        "failed" if bucket_name == "none_applied" else "unknown",
        "none_applied" if bucket_name == "none_applied" else "unknown",
        extras,
    )
    _settle(
        log, report, run_id, call_id, tool_id, intent_payload, result,
        bucket=bucket, dispatch_ts=ts,
    )


def _mark_dispatch_settled(
    log: EventLog, run_id: str, call_id: str, ts: int, reason: str
) -> None:
    """把 `dispatches` 行从 intent/executing 推进到 error（恢复补写结果时）。"""
    from .dispatcher import DISPATCH_ERROR

    log.conn.execute(
        "UPDATE dispatches SET state=?, reason=?, updated_at=?"
        " WHERE run_id=? AND call_id=?",
        (DISPATCH_ERROR, reason, ts, run_id, call_id),
    )


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
    dispatch_ts: int | None = None,
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

    if dispatch_ts is None:
        log.append_many(batch)
    else:
        # 事件与 dispatches 状态一次提交：恢复写的结果与"这次派发已结算"
        # 不能出现一个有一个没有。
        with log.transaction():
            log.append_many(batch)
            _mark_dispatch_settled(
                log, run_id, call_id, dispatch_ts, str(result.output.get("error", ""))
            )
    bucket.append(call_id)
