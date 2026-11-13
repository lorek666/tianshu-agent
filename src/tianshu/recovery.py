"""崩溃恢复（v2：只认写前证据）。

上一版用「磁盘现在像不像执行后的样子」推断「本次调用执行过」。
这在文件本来就有相同内容时必然误报成功——哈希相同不能证明写者是谁，
更不能证明这次调用发生过。

受控文件写入的判据是：**写前证据是否存在**。
证据（preimage + expected_after，以 call_id 为键）在写文件之前原子落盘，
所以：
- 没有证据 → 本次调用根本没进入写入阶段 → `none_applied`
- 有证据 → 用证据里的 before/after 对磁盘做三方比对 → applied / none_applied / unknown

其余一律 unknown，绝不编造 before_state，绝不生成成功凭据。
命令与 MCP 不遵循文件快照协议：只允许用派发边界证明尚未执行，越界后未知。
"""

from __future__ import annotations

import hashlib
import time
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
from .resources import (
    ResourceError,
    mcp_object_key,
    mcp_server_key,
    parse_mcp_object_key,
)
from .sandbox import current_backend
from .snapshot import ABSENT, Change, SnapshotStore
from .tool_registry import MCP_TOOL_PREFIX
from .tools import SPECS, SUBTASK_TOOL_IDS, ToolResult, action_for
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
    #: 本次被阻断的 **MCP 对象**（`mcp://server/tool` / `mcp://server`）。与
    #: `blocked_workspaces` 并存而不是替代：目录问"还能不能写这个目录"，对象问
    #: "还能不能调这个工具"。能证明是哪个对象时才登记；证明不了就不登记（目录
    #: 阻断仍然照旧，不放松保守方向）。
    blocked_objects: list[str] = field(default_factory=list)
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
    #: 子任务派发里**不能**判断"建了没有"的那一类（intent 已提交、`subtasks`
    #: 表里却没有对应行）。与记忆同一条理由：委派没有目录 preimage，硬挡一个
    #: 无关目录比不挡更糟；但报告必须 not ok，让人看到这次派发没结算。
    subtask_unresolved: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.unresolved
            and not self.memory_unresolved
            and not self.subtask_unresolved
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
        intents = {
            e.payload["call_id"]: e for e in events if e.type == "tool.intent"
        }
        results = {e.payload["call_id"] for e in events if e.type == "tool.result"}
        pending = {cid: ev for cid, ev in intents.items() if cid not in results}
        # 外发边界标记要在日志还开着的时候读出来：`own_log` 会在 finally 里关掉，
        # 而 MCP 的判据（发送前标记）就存在同一张 `dispatches` 表里。
        mcp_never_sent = _read_mcp_never_sent(log, run_id, pending)
        exec_not_started = {
            cid: _dispatch_not_started(_dispatch_row_states(log, run_id, cid))
            for cid, ev in pending.items() if ev.payload["tool_id"] == "run_command"
        }
    except EventLogCorrupt as exc:
        return RunAssessment(
            run_id=run_id,
            corrupt=True,
            problems=(f"事件日志读不出来，无法对账：{exc}",),
        )
    finally:
        if own_log is not None:
            own_log.close()

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
        if tool_id.startswith(MCP_TOOL_PREFIX):
            # MCP 没有本地 preimage：唯一判据是"请求有没有越过外发边界"（发送前
            # 提交的派发行状态，与 `_recover_run` 同一口径；按 id 形状判身份，
            # 不靠 `SPECS` 缺条目）。停在外发边界之前 → 确定没发，可以接手；
            # 越过边界或无从证明 → 拒绝接手，等人核对。
            if mcp_never_sent.get(call_id, False):
                none_applied.append(call_id)
            else:
                unresolved.append(call_id)
            continue
        if tool_id == "run_command":
            # 命令没有受控文件写前证据；空清单不能证明进程没有执行。
            pending_writes += 1
            if exec_not_started.get(call_id, False):
                none_applied.append(call_id)
            else:
                unknown.append(call_id)
            continue
        if spec is None:
            # 语义未知的工具：不能替它假设"没写"（与 recover 同一口径）。
            unresolved.append(call_id)
            continue
        if spec.side_effect_level == "pure":
            none_applied.append(call_id)
            continue
        if tool_id in SUBTASK_TOOL_IDS:
            # 委派没有文件副作用：最坏情况是多了一条 queued 子任务行（以及一个
            # 排队中的子作业），它不会让任何目录的磁盘状态变得不可核实。
            # 所以不占 pending_writes——否则一次"派了子任务"会让整个工作区被
            # 判成"证据缺失"而拒绝接管，那是误报。
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

    `memory` / `blob_dir` 决定"记忆正文的 blob 去哪找"（擦除结论与孤儿清理都要
    用它）：`memory` 优先（装配方已经打开的那个投影，CLI 传 `stack.memory`、
    daemon 传 `control_stack.memory`）；只给 `blob_dir` 时按该目录新建一个投影；
    两者都不给就按 `MemoryProjection` 的默认规则（DB 旁的 `memory_blobs/`）。
    正文目录与默认值不同却没传这两个参数时，擦除会被判成"文件已没"、孤儿也清
    不到——这是调用方的契约责任，不是恢复在猜。

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
    _repair_orphan_recovery_blocks(log, report, events, intents, snapshots, ws)

    # 清单存在但读不出内容：证据**曾经存在过**，只是现在不可信。这比"清单整个
    # 缺失"更糟——不能像"没有证据=没写"那样替任何一条未完成的调用下结论。
    # 一个字节都不猜：写工具与语义未知的工具全部判 unknown 并阻断到调用方显式
    # 给的那个目录。只读工具不读这份证据（它本来就没有副作用），结论不受影响，
    # 否则会把一个只是"清单文件坏了"的目录额外挡上——过度阻断也是误报。
    if pending and snapshots is not None:
        try:
            snapshots.load(run_id)
        except SnapshotCorrupt as exc:
            for call_id, ev in pending.items():
                payload = ev.payload
                tool_id = payload["tool_id"]
                spec = SPECS.get(tool_id)
                roots = _unresolved_roots(
                    log, run_id, call_id,
                    None if tool_id == "run_command" else snapshots, ws,
                )
                if tool_id.startswith(MCP_TOOL_PREFIX):
                    # MCP 不读这份快照清单：它的判据是外发边界标记。清单坏了
                    # 也不该把一条"请求都没写出"的调用推成 unknown 并挡住目录
                    # ——那是拿无关证据的损毁去冒充 MCP 的证据。
                    _settle_mcp_pending(
                        log, report, run_id, call_id, tool_id, payload,
                        never_sent=_mcp_never_sent(
                            _dispatch_row_states(log, run_id, call_id)
                        ),
                        objects=_dispatch_row_targets(log, run_id, call_id),
                        workspace_roots=roots,
                    )
                    continue
                if tool_id == "run_command":
                    _settle_exec_pending(log, report, run_id, call_id, payload, roots)
                    continue
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
                if tool_id in SUBTASK_TOOL_IDS:
                    # 委派不读快照清单：它的证据在 `subtasks` 表里。清单坏了
                    # 也不该把它推成"无法核实"并挡住任何目录。
                    _settle_subtask_intent(
                        log, report, run_id, call_id, tool_id, payload, ev.ts
                    )
                    continue
                _settle(log, report, run_id, call_id, tool_id, payload,
                        ToolResult("unknown", "unknown",
                                   {"error": "recovered: 快照清单损坏，无法核实任何"
                                             f"副作用（{exc}）",
                                    "recovered": True}),
                        bucket=report.unresolved, blocked_roots=roots)
            return report

    for call_id, ev in pending.items():
        payload = ev.payload
        tool_id = payload["tool_id"]
        spec = SPECS.get(tool_id)
        roots = _unresolved_roots(
            log, run_id, call_id,
            None if tool_id == "run_command" else snapshots, ws,
        )

        # MCP（G2 MCP 阶段）：副作用发生在 **server 那一侧**，本地没有 preimage
        # 可查。唯一判据是"请求有没有越过外发边界"——派发行在发包**之前**被
        # 提交为 `executing`（`dispatcher._mark_mcp_send_boundary`）：仍是
        # `intent`/`revoked`/`rejected` 说明请求一定没写出，可以判 `none_applied`
        # 且不阻断；`executing` 或无从证明，就只能 unknown + 阻断。
        #
        # 为什么按前缀而不是按 `SPECS` 有没有条目：MCP 工具**故意不进** `SPECS`，
        # 但"不在表里"是个会变的前提（有人把动态 id 塞进静态表、或注册表装配错
        # 了）。身份由 id 形状决定，恢复不该依赖一张静态表来认出"这不是文件工
        # 具"——认错了就会去查磁盘，然后把"没有写前证据"读成"确定没写"。
        if tool_id.startswith(MCP_TOOL_PREFIX):
            _settle_mcp_pending(
                log, report, run_id, call_id, tool_id, payload,
                never_sent=_mcp_never_sent(
                    _dispatch_row_states(log, run_id, call_id)
                ),
                objects=_dispatch_row_targets(log, run_id, call_id),
                workspace_roots=roots,
            )
            continue

        if tool_id == "run_command":
            _settle_exec_pending(log, report, run_id, call_id, payload, roots)
            continue

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
                    bucket=report.unresolved, blocked_roots=roots)
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

        # 委派（G2 子代理 D1）：证据在 `subtasks` 表里，不在快照清单里。
        # 有行 → 补身份一致的长结果；没行 → unknown（可能已建也可能没建），
        # 且**不**阻断任何目录（委派没有目录 preimage）。
        if tool_id in SUBTASK_TOOL_IDS:
            _settle_subtask_intent(
                log, report, run_id, call_id, tool_id, payload, ev.ts
            )
            continue

        # 写工具：唯一判据是写前证据
        if snapshots is None:
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 未提供快照存储，无法核实",
                                "recovered": True}),
                    bucket=report.unresolved, blocked_roots=roots)
            continue

        # run.start 与 init_run 之间崩溃时尚未产生任何 tool intent，能安全补建
        # 空清单；一旦已有写 intent，缺清单仍是异常，绝不能推断为未写入。
        if not snapshots.has_manifest(run_id):
            _settle(log, report, run_id, call_id, tool_id, payload,
                    ToolResult("unknown", "unknown",
                               {"error": "recovered: 清单缺失，无法确认是否写入",
                                "recovered": True}),
                    bucket=report.unresolved, blocked_roots=roots)
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
                    bucket=report.unresolved, blocked_roots=roots)
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
                    bucket=report.unresolved, blocked_roots=roots)
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
                    bucket=report.unresolved, blocked_roots=roots)
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
                bucket=report.unresolved, blocked_roots=roots)

    return report


_WORKSPACE_BLOCK_REASON = "存在无法核实的副作用，阻断后续写入"
_MCP_BLOCK_REASON = "MCP 工具可能已在 server 侧执行、副作用无法核实：阻断再次调用"


def _block_events(
    run_id: str,
    call_id: str,
    roots: tuple[str, ...],
    objects: tuple[str, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": run_id,
            "type": event_type,
            "payload": {key: target, "call_ids": [call_id], "reason": reason},
            "actor_kind": "system",
        }
        for event_type, key, targets, reason in (
            ("recovery.blocked", "workspace", roots, _WORKSPACE_BLOCK_REASON),
            ("recovery.object_blocked", "object", objects, _MCP_BLOCK_REASON),
        )
        for target in targets
    ]


def _repair_orphan_recovery_blocks(
    log: EventLog,
    report: RecoveryReport,
    events: list[Any],
    intents: dict[str, Any],
    snapshots: SnapshotStore | None,
    ws: Workspace,
) -> None:
    """补偿旧版本在 result 与 block 之间崩溃留下的孤立 unknown。

    不改写既有结果；只追加缺少的阻断。该调用已有阻断历史，或人在结果之后
    已明确解除对应目标时，不重建它，避免把已完成的人工核查撤销。
    """
    uncertain = [
        event for event in events
        if event.type == "tool.result"
        and event.payload.get("effect_state") == "unknown"
        and event.payload.get("output", {}).get("recovered") is True
    ]
    if not uncertain:
        return
    history = log.read_types({
        "recovery.blocked", "recovery.unblocked",
        "recovery.object_blocked", "recovery.object_unblocked",
    })
    for result in uncertain:
        call_id = result.payload["call_id"]
        intent = intents.get(call_id)
        if intent is None:
            report.problems.append(f"{call_id}：旧恢复结果缺少 intent，无法补齐阻断归属")
            continue
        tool_id = intent.payload["tool_id"]
        spec = SPECS.get(tool_id)
        if tool_id in MEMORY_TOOL_IDS or tool_id in SUBTASK_TOOL_IDS:
            continue
        if spec is not None and spec.side_effect_level == "pure":
            continue
        own_blocks = [
            event for event in history
            if event.run_id == report.run_id
            and call_id in event.payload.get("call_ids", [])
        ]
        roots = () if any(e.type == "recovery.blocked" for e in own_blocks) else (
            _unresolved_roots(
                log, report.run_id, call_id,
                None if tool_id == "run_command" else snapshots, ws,
            )
        )
        roots = tuple(root for root in roots if not any(
            e.type == "recovery.unblocked" and e.seq > result.seq
            and e.payload.get("workspace") == root for e in history
        ))
        objects = (
            _dispatch_row_targets(log, report.run_id, call_id)
            if tool_id.startswith(MCP_TOOL_PREFIX) else ()
        )
        objects = tuple(obj for obj in objects if not any(
            e.type == "recovery.object_blocked" and e.payload.get("object") == obj
            for e in own_blocks
        ) and not any(
            e.type == "recovery.object_unblocked" and e.seq > result.seq
            and e.payload.get("object") == obj for e in history
        ))
        if not roots and not objects:
            continue
        with log.transaction():
            log.append_many(_block_events(report.run_id, call_id, roots, objects))
            if _has_dispatch_table(log):
                _mark_dispatch_settled(
                    log, report.run_id, call_id, _now_ms(),
                    "recovered: 补齐旧 unknown 结果缺失的阻断",
                )
        report.unresolved.append(call_id)
        for root in roots:
            _block_workspace(log, report, report.run_id, root, [call_id], persist=False)
        for obj in objects:
            _block_mcp_object(log, report, report.run_id, obj, [call_id], persist=False)


def _block_workspace(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    root: str,
    call_ids: list[str],
    *,
    persist: bool = True,
) -> None:
    """把"这个目录里有核实不了的副作用"落成事件并记进报告。

    阻断事件是 append-only 的：它不会被后面的"已解除"覆盖，解除由
    `guard.unblock` 再追加一条，两者一起才算出当前状态。

    同时登记**用户级权威根**（issue #61.1）：只写本数据根日志的话，换个
    `--db` 就看不见这条阻断。权威根写失败不能假装成功——在报告里如实写
    「跨数据根阻断未生效」，本数据根这一侧仍然是阻断的（日志已经写了）。
    """
    if root not in report.blocked_workspaces:
        report.blocked_workspaces.append(root)
    reason = _WORKSPACE_BLOCK_REASON
    if persist:
        log.append(
            run_id, "recovery.blocked",
            {"workspace": root, "call_ids": call_ids, "reason": reason},
            actor_kind="system",
        )
    try:
        authority.register_blocked(root, run_id=run_id, reason=reason)
    except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
        report.problems.append(f"跨数据根阻断未生效：{root}（{exc}）")


def _unresolved_roots(
    log: EventLog,
    run_id: str,
    call_id: str,
    snapshots: SnapshotStore | None,
    fallback: Workspace,
) -> tuple[str, ...]:
    """优先按写前证据归属；命令无快照时按派发时记录的工作区阻断。"""
    try:
        change = snapshots.find(run_id, call_id) if snapshots is not None else None
    except SnapshotCorrupt:
        change = None
    if change is not None and change.root:
        return (change.root,)
    if _has_dispatch_table(log):
        columns = {
            str(row[1]) for row in log.conn.execute("PRAGMA table_info(dispatches)")
        }
        if "workspace_root" in columns:
            roots = {
                str(Path(row[0]).resolve())
                for row in log.conn.execute(
                    "SELECT workspace_root FROM dispatches WHERE run_id=? AND call_id=?",
                    (run_id, call_id),
                )
                if row[0] and Path(row[0]).is_absolute()
            }
            if roots:
                return tuple(sorted(roots))
    return (str(fallback.root),)


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

    `memory` 与 `blob_dir` 同时给时**`memory` 胜出**（`blob_dir` 只在没有装配投影
    时用来新建一个）；要覆盖目录就只传 `blob_dir`，或传一个指向该目录的投影——
    这里不做"猜你想用哪个"的合并。
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
    log: EventLog,
    run_id: str,
    call_id: str,
    ts: int,
    reason: str,
    *,
    state: str | None = None,
) -> None:
    """把 `dispatches` 行从 intent/executing 推进到终态（恢复补写结果时）。

    `state` 默认 `error`（"这次派发没执行成功"）；委派恢复在**证据证明它已经
    发生**（`subtasks` 行存在）时显式传 `done`——那时把派发行写成 error 就是
    在日志里留一句与事实相反的话。

    **只从非终态推进**（复审 R2-2）：`revoked` / `rejected` / `done` / `error`
    已经是终态，恢复补写一条结果不该把它们改写——撤销先于结算时把 `revoked`
    改成 `error`，是在审计里抹掉"这次派发被撤销过"这条事实。结果照写，行不动。
    """
    from .dispatcher import DISPATCH_ERROR, DISPATCH_EXECUTING, DISPATCH_INTENT

    log.conn.execute(
        "UPDATE dispatches SET state=?, reason=?, updated_at=?"
        " WHERE run_id=? AND call_id=? AND state IN (?, ?)",
        (
            state or DISPATCH_ERROR,
            reason,
            ts,
            run_id,
            call_id,
            DISPATCH_INTENT,
            DISPATCH_EXECUTING,
        ),
    )


def _settle_subtask_intent(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    call_id: str,
    tool_id: str,
    intent_payload: dict,
    ts: int,
) -> None:
    """结算一条被打断的委派调用（D1-c10）。

    **以证据为准，不按进程退出猜**：

    - `subtask.spawn`：`(父 Run, slug)` 在 `subtasks` 表里有行 → 补一条**身份
      一致**的 `succeeded`（child_task / child_run / job 从行里读，不重新生成）；
      没有行 → `unknown`：intent 已提交，建行可能就发生在崩溃窗口里，"没看到
      行"证明不了"没建"。这类不挡目录（委派没有目录 preimage），但报告 not ok。
    - `subtask.cancel`：行已经是 `cancelling` → 请求确实记下了；否则就是没记下。
      两种都是确定结论，不产生 unknown。
    """
    from .dispatcher import DISPATCH_FINISHED

    args = intent_payload.get("args")
    subtask_id = args.get("subtask_id") if isinstance(args, dict) else None
    row = None
    if isinstance(subtask_id, str) and subtask_id:
        row = log.conn.execute(
            "SELECT * FROM subtasks WHERE parent_run_id=? AND subtask_id=?",
            (run_id, subtask_id),
        ).fetchone()
    if tool_id == "subtask.cancel":
        if row is not None and row["state"] == "cancelling":
            _settle(
                log, report, run_id, call_id, tool_id, intent_payload,
                ToolResult(
                    "succeeded",
                    "none_applied",
                    {
                        "subtask_id": row["subtask_id"],
                        "state": row["state"],
                        "cancel_requested": True,
                        "recovered": True,
                    },
                ),
                bucket=report.none_applied,
                dispatch_ts=ts,
                dispatch_state=DISPATCH_FINISHED,
            )
            return
        if row is None:
            reason = f"recovered: 子任务不存在（{subtask_id!r}），取消请求没有记下"
        else:
            reason = (
                f"recovered: 子任务状态是 {row['state']}，取消请求没有记下"
            )
        _settle(
            log, report, run_id, call_id, tool_id, intent_payload,
            ToolResult("failed", "none_applied", {"error": reason, "recovered": True}),
            bucket=report.none_applied,
            # 派发记录也要离开 intent：否则后续 resume / 审计会看到
            # "仍在执行"与"已确认没记下"两种说法。
            dispatch_ts=ts,
        )
        return
    if row is None:
        # intent 已提交、行却不在：可能已建也可能没建。**不猜**。
        _settle(
            log, report, run_id, call_id, tool_id, intent_payload,
            ToolResult(
                "unknown",
                "unknown",
                {
                    "error": "recovered: 子任务派发的副作用无法核实"
                             "（subtasks 表里没有这一行；可能已建也可能没建）",
                    "recovered": True,
                    "subtask_id": subtask_id,
                },
            ),
            bucket=report.subtask_unresolved,
            # 结论是 unknown，派发记录按 error 收：它既不是"没执行"，
            # 也不能继续显示"正在派发"。
            dispatch_ts=ts,
        )
        return
    import json

    limits = json.loads(row["limits"] or "{}")
    _settle(
        log, report, run_id, call_id, tool_id, intent_payload,
        ToolResult(
            "succeeded",
            "none_applied",
            {
                "subtask_id": row["subtask_id"],
                "child_task_id": row["child_task_id"],
                "child_run_id": row["child_run_id"],
                "job_id": row["job_id"],
                "state": row["state"],
                "limits": limits,
                "recovered": True,
            },
        ),
        bucket=report.none_applied,
        dispatch_ts=ts,
        dispatch_state=DISPATCH_FINISHED,
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
    dispatch_state: str | None = None,
    blocked_roots: tuple[str, ...] = (),
    blocked_objects: tuple[str, ...] = (),
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
    # unknown 结果与本地阻断必须同生同死。若先提交结果再写阻断，恢复进程
    # 在两者之间崩溃后，下一次会因“已有 result”跳过这条 intent，永远漏挡。
    batch.extend(_block_events(run_id, call_id, blocked_roots, blocked_objects))

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

    # 事务只含本事件库的写入：结果、阻断与派发终态一次提交。历史库没有
    # 派发表/行时只补事件，不编造派发行。updated_at 默认使用恢复时钟。
    with log.transaction():
        log.append_many(batch)
        if _has_dispatch_table(log):
            _mark_dispatch_settled(
                log, run_id, call_id,
                dispatch_ts if dispatch_ts is not None else _now_ms(),
                str(result.output.get("error", "")),
                state=_default_dispatch_state(result, dispatch_state),
            )
    bucket.append(call_id)
    # authority 是另一个库，不能伪称跨库原子。先保证本地事实已提交，再登记；
    # 失败仍报告 problem，或在二次崩溃后的 mirror_blocks_to_authority 补登记。
    for root in blocked_roots:
        _block_workspace(log, report, run_id, root, [call_id], persist=False)
    for object_key in blocked_objects:
        _block_mcp_object(log, report, run_id, object_key, [call_id], persist=False)


def _now_ms() -> int:
    """恢复自己写事件/推进派发行用的时钟（毫秒，与 `dispatcher._now` 同口径）。"""
    return int(time.time() * 1000)


def _has_dispatch_table(log: EventLog) -> bool:
    """这个库里有没有 `dispatches` 表（恢复可能被叫到很老的库上）。"""
    return log.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'"
    ).fetchone() is not None


def _dispatch_row_states(
    log: EventLog, run_id: str, call_id: str
) -> list[tuple[str, int]] | None:
    """一次调用的派发行 `(state, boundary_protocol)`；`None` = 没有派发表。

    返回列表而不是单个值：派发表的主键是 `dispatch_id`（由 run+call 哈希），
    正常情况下一次调用只有一行；将来若出现重试行，只要**有任意一行**越过了
    外发边界（或不知道某一行有没有），就不能宣布"没发出去"。

    老库的 `dispatches` 可能**没有** `boundary_protocol` 列（迁移只在
    `Dispatcher.__init__` 里跑，恢复可能先开库）：没有这一列就按 0（旧行）读，
    不因为"读不到列"就崩掉恢复。
    """
    if not _has_dispatch_table(log):
        return None
    columns = {
        str(row[1]) for row in log.conn.execute("PRAGMA table_info(dispatches)")
    }
    if "boundary_protocol" not in columns:
        return [
            (str(row["state"]), 0)
            for row in log.conn.execute(
                "SELECT state FROM dispatches WHERE run_id=? AND call_id=?",
                (run_id, call_id),
            )
        ]
    return [
        (str(row["state"]), int(row["boundary_protocol"]))
        for row in log.conn.execute(
            "SELECT state, boundary_protocol FROM dispatches"
            " WHERE run_id=? AND call_id=?",
            (run_id, call_id),
        )
    ]


def _dispatch_row_targets(log: EventLog, run_id: str, call_id: str) -> tuple[str, ...]:
    """这次调用派发时用的**对象**（派发行的 `target`，已规范化为 `mcp://s/t`）。

    为什么从派发行读而不是从 `tool_id` 反解：工具 id 是 `mcp__server__tool`，
    规范化把 `-`/`.` 换成了 `_`，反解出来的名字可能不是用户配的那个
    （`_settle_mcp_pending` 的既有结论）。派发行的 `target` 是派发当时的原始
    URI，那是**事实**；反解只是猜。

    没有派发表 / **没有 `target` 列** / 没有行 / 形状不是 `mcp://…` 或解析不了 →
    空元组。空元组 = "证明不了是哪个对象"，调用方**不**登记对象阻断（目录阻断仍
    照旧）。缺列这一条是复审抓到的：`target` 与派发表同时引入，正常升级不会缺，
    但损坏/手改/部分迁移的库会让 `SELECT target` 直接抛错——恢复在写任何阻断之前
    崩掉，等于安全方向上的 fail-open。
    """
    if not _has_dispatch_table(log):
        return ()
    columns = {
        str(row[1]) for row in log.conn.execute("PRAGMA table_info(dispatches)")
    }
    if "target" not in columns:
        return ()
    rows = log.conn.execute(
        "SELECT target FROM dispatches WHERE run_id=? AND call_id=?",
        (run_id, call_id),
    ).fetchall()
    keys: list[str] = []
    for row in rows:
        raw = str(row["target"] or "")
        if not raw:
            continue
        try:
            server, tool = parse_mcp_object_key(raw)
        except ResourceError:
            continue
        # 用解析出的段重新拼规范键：别名/编码差异将来会拼出"同一对象的两个键"，
        # 而对象阻断的判定是逐字节相等。**今天这一步是恒等的**（写入侧只写规范
        # 形状），所以没有判据能区分它——留着是把"同一对象只有一个键"写进代码，
        # 而不是等将来出现别名时才发现两个键。
        canonical = (
            mcp_object_key(server, tool) if tool is not None else mcp_server_key(server)
        )
        if canonical not in keys:
            keys.append(canonical)
    return tuple(keys)


#: 写请求前提交外发边界标记的协议版本（0 = 升级前写的旧行）。
BOUNDARY_PROTOCOL = 1


def _mcp_never_sent(rows: list[tuple[str, int]] | None) -> bool:
    """能不能证明这次 MCP 请求没写出。

    两个条件**同时**成立才算：

    1. 行由带外发边界标记的版本写的（`boundary_protocol >= 1`）。升级前的旧行
       在写请求之前**不推进**派发行，所以旧行的 `intent`（甚至 `revoked`——旧版
       撤销与发送没有互斥）**不能**证明没发；它们一律按"可能已发出"处理。
    2. 行状态停在边界之前：`intent`（还没到边界）、`revoked`/`rejected`（撤销/
       作废先到；标记只从 `intent` 推进，且与撤销共用同一把执行守卫）。

    没有表、没有行、旧行、已 `executing`/`done`/`error`、或任何不认识的状态 →
    False："证明不了没发"就按"可能已发出"处理。
    """
    return _dispatch_not_started(rows)


def _dispatch_not_started(rows: list[tuple[str, int]] | None) -> bool:
    """只有带执行前持久化标记的派发行停在边界前，才可证明尚未执行。"""
    from .dispatcher import (
        DISPATCH_INTENT,
        DISPATCH_ROW_REJECTED,
        DISPATCH_ROW_REVOKED,
    )

    if not rows:
        return False
    proven = {DISPATCH_INTENT, DISPATCH_ROW_REJECTED, DISPATCH_ROW_REVOKED}
    return all(
        protocol >= BOUNDARY_PROTOCOL and state in proven
        for state, protocol in rows
    )


def _settle_exec_pending(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    call_id: str,
    payload: dict,
    workspace_roots: tuple[str, ...],
) -> None:
    """命令可能影响任意文件；不拿文件快照冒充命令副作用证据。"""
    not_started = _dispatch_not_started(_dispatch_row_states(log, run_id, call_id))
    _settle(
        log, report, run_id, call_id, "run_command", payload,
        ToolResult(
            "failed" if not_started else "unknown",
            "none_applied" if not_started else "unknown",
            {
                "error": (
                    "recovered: 命令尚未越过执行边界，没有启动"
                    if not_started else
                    "recovered: 命令可能已经执行，无法从文件快照核实副作用，需人工核对"
                ),
                "reason": "exec.not_started" if not_started else "exec.crash_unsettled",
                "recovered": True,
            },
        ),
        bucket=report.none_applied if not_started else report.unresolved,
        blocked_roots=() if not_started else workspace_roots,
    )


def _read_mcp_never_sent(
    log: EventLog, run_id: str, pending: dict[str, Any]
) -> dict[str, bool]:
    """只读对账用：一次读出所有待决 MCP 调用的"没发出"结论。"""
    return {
        call_id: _mcp_never_sent(_dispatch_row_states(log, run_id, call_id))
        for call_id, ev in pending.items()
        if str(ev.payload.get("tool_id", "")).startswith(MCP_TOOL_PREFIX)
    }


def _settle_mcp_pending(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    call_id: str,
    tool_id: str,
    payload: dict,
    *,
    never_sent: bool,
    objects: tuple[str, ...] = (),
    workspace_roots: tuple[str, ...] = (),
) -> bool:
    """结算一条没有 `tool.result` 的 MCP 调用；返回它是否被判成了 `unknown`。

    MCP 的副作用在 **server 那一侧**，本地没有 preimage。判据只有"请求有没有
    越过外发边界"（发送前提交的派发行状态）：

    - 没越过 → 请求没写出 → `none_applied`，**不阻断**任何对象/目录；
    - 越过或无从证明 → `unknown` + 阻断（目录 + 对象），等人核对后再解除。

    `objects` 是派发行 `target` 读出来的**对象键**（`mcp://server/tool`）：只有
    看得见派发行、且形状合法时才登记对象阻断。证明不了是哪个对象（没有派发
    表/没有行/target 不是 mcp URI）就不登记对象阻断——目录阻断仍由调用方照旧
    落下（保守方向不放松）。

    两种结论都不补凭据（`build_mcp_receipt` 只对 `applied` 出凭据）：没有越过
    边界就没有可归因的效果；越过边界但结论未知时，凭据会把"不知道"写成一个
    可核对的结论。也**不**按 `__` 反解 server/tool 名字（规范化把 `-`/`.` 换成
    了 `_`，反解出来的名字可能不是用户配的那个）——对象键只从派发行读。
    """
    common = {
        "recovered": True,
        "tool_id": tool_id,
        "mcp": {"tool_id": tool_id, "lookup": "tianshu mcp list"},
    }
    if never_sent:
        _settle(
            log, report, run_id, call_id, tool_id, payload,
            ToolResult(
                "failed", "none_applied",
                {
                    "error": "recovered: 这次 MCP 调用没越过外发边界"
                             "（派发记录停在 intent/revoked/rejected），"
                             "请求没有写出",
                    "reason": "mcp.not_sent",
                    **common,
                },
            ),
            bucket=report.none_applied,
        )
        return False
    _settle(
        log, report, run_id, call_id, tool_id, payload,
        ToolResult(
            "unknown", "unknown",
            {
                "error": "recovered: MCP 调用已越过外发边界，server 侧可能已经"
                         "执行（本地目录也可能被它改过），需人工核对后再解除阻断",
                "reason": "mcp.crash_unsettled",
                **common,
            },
        ),
        bucket=report.unresolved,
        blocked_roots=workspace_roots,
        blocked_objects=objects,
    )
    return True


def _block_mcp_object(
    log: EventLog,
    report: RecoveryReport,
    run_id: str,
    object_key: str,
    call_ids: list[str],
    *,
    persist: bool = True,
) -> None:
    """把"这个 MCP 对象可能已经在 server 侧执行过"落成事件并记进报告。

    与 `_block_workspace` 同构（append-only 事件 + 权威根 + 失败进报告），但
    对象是外部的 `mcp://…`。解除由 `guard.unblock_object` 追加一条
    `recovery.object_unblocked`；**目录阻断的解除不会碰它**——核对过工作区文件
    不等于核对过 server 侧副作用（那件事本地根本没法核对，只能由人看 server
    的输出/日志后确认）。
    """
    # 报告里同一对象只列一次（它是"本次被阻断的对象集合"）；但**每条**未结算调用
    # 都要落一条事件：`call_ids` 是归因材料，去重会丢掉第二条调用的证据。事件
    # 回放按下标做集合语义，重复阻断无害。
    if object_key not in report.blocked_objects:
        report.blocked_objects.append(object_key)
    reason = _MCP_BLOCK_REASON
    if persist:
        log.append(
            run_id, "recovery.object_blocked",
            {"object": object_key, "call_ids": call_ids, "reason": reason},
            actor_kind="system",
        )
    try:
        authority.register_object_blocked(object_key, run_id=run_id, reason=reason)
    except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
        report.problems.append(f"跨数据根对象阻断未生效：{object_key}（{exc}）")


def _default_dispatch_state(result: ToolResult, explicit: str | None) -> str:
    """恢复补写结果时派发行该写什么终态。

    确定落地（`applied`）→ `done`；其余（none_applied / unknown）→ `error`。
    把 applied 写成 error 会在日志里留一句与事实相反的话；把 unknown 写成 done
    则是替一次没核实过的调用宣布成功。
    """
    if explicit is not None:
        return explicit
    from .dispatcher import DISPATCH_ERROR, DISPATCH_FINISHED

    return DISPATCH_FINISHED if result.effect_state == "applied" else DISPATCH_ERROR
