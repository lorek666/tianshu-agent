"""撤销：把一次 Run 的写入还原或删除。

D3 定稿的「默认可写就必须默认可撤销」是这一模块存在的唯一理由：普通用户第一次
被 agent 改坏文件就会卸载，所以撤销必须是产品级能力，而不是排障工具。

**撤销本身也是写操作。** 它必须走和普通写入同一条路径：guard（阻断检查）→
派发 → 审批 → 租约 → receipt。`SnapshotStore.undo()` 只负责"该改什么"和预检，
真正落盘必须经 `apply` 回调，回调里每一条都是一次经审批的派发。

从 CLI 里搬出来的理由：M5 的桌面 UI 要通过 Daemon 调它。逻辑留在 `cli.py` 会让
"CLI 能撤销、UI 不能"变成第二套实现——撤销是最不该有两份代码的地方。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import undo_journal
from .crashpoint import failpoint
from .credentials import redact
from .dispatcher import ControlRun, build_request
from .errors import SnapshotCorrupt, TianshuError, ToolNotAllowed
from .guard import ensure_not_blocked
from .jobs import JOB_CLAIMED, TERMINAL_JOB_STATES, JobClaim, JobLost, JobUnavailable
from .permissions import HumanIdentity
from .tasks import TERMINAL_STATES
from .workspace import Workspace, WorkspaceBinding


class UndoRefused(TianshuError):
    """撤销在动手之前就被拒绝。

    `code` 对应 CLI 退出码（2 = 前置条件错、1 = 没有可撤销的东西）。把
    "拒绝的原因"和"退出码"绑在一起，是因为 UI/CLI 都不能靠 message 文本分支。
    `lines` 是要原样给用户看的解释（多行，按顺序）。
    """

    def __init__(self, message: str, *, code: int = 2, lines: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.lines = lines or []


@dataclass(frozen=True)
class UndoItem:
    """清单里的一条。`path` 是相对 workspace 根的路径，`root` 才是落盘目录。"""

    path: str
    root: str
    call_id: str
    before_state: str


@dataclass
class UndoPlan:
    run_id: str
    items: list[UndoItem]
    roots: list[str]
    scope: Path | None = None
    scope_workspace: Workspace | None = None

    def describe(self) -> list[tuple[str, str]]:
        """给用户看的预演。

        撤销方向是**调用逆序**：同一路径被写过多次时正序会自相冲突。
        显示**绝对路径**：多 workspace 任务里只写相对路径，人看不出动的是哪个目录。
        """
        plan: list[tuple[str, str]] = []
        for item in reversed(self.items):
            target = Path(item.root) / item.path
            if item.before_state == "absent":
                plan.append(("delete ", f"{target}（原本不存在，撤销=删掉）"))
            else:
                plan.append(
                    ("restore", f"{target}（还原到 {item.before_state[:12]}…）")
                )
        return plan


@dataclass
class UndoOutcome:
    """一次撤销的结局。`effect` 与 C2 的语义一致，不许用 partial 冒充成功。"""

    ok: bool
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    effect: str = "none_applied"
    control_run: str = ""


def _load_items(stack, run_id: str) -> list[UndoItem]:
    changes = stack.snapshots.load(run_id)
    return [
        UndoItem(
            path=change.path,
            # 旧格式证据没有根：必须保持空串而不是 "None"——字符串 "None"
            # 是真值，会把"缺根"伪装成"有个叫 None 的目录"。
            root="" if change.root is None else str(change.root),
            call_id=change.call_id,
            before_state=change.before_state,
        )
        for change in changes
    ]


def _load_journal(store, run_id: str) -> undo_journal.UndoJournal | None:
    """读意图日志；读不懂就拒绝，绝不当成"没有 journal"从头再来。"""
    try:
        return store.journal.load(run_id)
    except undo_journal.UndoJournalCorrupt as exc:
        raise UndoRefused(
            "撤销日志损坏",
            code=2,
            lines=[
                "拒绝撤销：这个 run 的撤销意图日志（artifacts/undo/<run_id>.json）"
                "读不出来",
                f"  - {exc}",
                "日志损坏时无法确认撤销做到哪一步。当成空日志从头再来会重复"
                "删除/还原；按猜的日志续做会覆盖用户后来的修改。所以一个字节都不动。",
                "处置建议：先备份 artifacts/undo/ 与 artifacts/snapshots/ 下的文件，"
                "再用 `tianshu recover` 核对后人工处理。",
            ],
        ) from exc


def _check_event_consistency(log, journal: undo_journal.UndoJournal) -> None:
    """完成事件与 journal 状态必须一致：两者矛盾时谁都不许继续。

    - journal 未完成却有 `undo.completed`：不可能。要么事件被伪造，要么
      journal 被改过；补写/续做都会把矛盾盖住。
    - journal 已完成而事件缺失：合法（崩溃窗口），由 `_emit_completed_once`
      幂等补写。
    """
    events = [
        event
        for event in log.read_run(journal.run_id)
        if event.type == "undo.completed"
    ]
    if journal.status != undo_journal.STATUS_IN_PROGRESS or not events:
        return
    raise UndoRefused(
        "撤销日志与完成事件矛盾",
        code=2,
        lines=[
            "拒绝继续：事件日志里已有 undo.completed，但撤销意图日志仍是"
            " in_progress",
            f"  - journal：artifacts/undo/{journal.run_id}.json"
            f"（status=in_progress，done={len(journal.done)}/{len(journal.items)}）",
            "两者至少有一个不是真的。补写事件或重新撤销都会掩盖真实状态，"
            "所以一个字节都不动。",
            "处置建议：先备份 artifacts/undo/ 与事件库，人工核对后再处理。",
        ],
    )


def _journal_items(journal: undo_journal.UndoJournal) -> list[UndoItem]:
    return [
        UndoItem(
            path=item.path,
            root=item.root,
            call_id=item.call_id,
            before_state=item.before_state,
        )
        for item in journal.remaining
    ]


def _plan_journal_undo(
    store, journal: undo_journal.UndoJournal, scope: Path | None
) -> UndoPlan:
    """未完成/已完成 journal 的只读预演：先核对磁盘，再说明还要做什么。"""
    roots = sorted({item.root for item in journal.items})
    resolved = scope.resolve() if scope is not None else None
    if resolved is not None and roots != [str(resolved)]:
        raise UndoRefused(
            "撤销日志的范围与 --workspace 不一致",
            code=2,
            lines=[
                "拒绝续做：该 run 的撤销意图日志记录了这些 workspace："
                f"{'、'.join(roots)}",
                f"本次只给了 --workspace {resolved}。只续做一部分会让 journal 的"
                "全局进度与磁盘对不上，所以要么不传 --workspace，要么传原来的范围。",
            ],
        )
    scope_workspace: Workspace | None = None
    if resolved is not None:
        try:
            scope_workspace = Workspace(resolved)
        except TianshuError as exc:
            raise UndoRefused(
                "workspace 不可用",
                code=2,
                lines=[f"拒绝撤销：workspace 不可用：{resolved}（{redact(str(exc))}）"],
            ) from exc
    try:
        store.check_undo_journal(journal, scope_workspace)
    except undo_journal.UndoJournalConflict as exc:
        raise UndoRefused(
            "撤销日志与磁盘状态冲突",
            code=2,
            lines=[
                "拒绝续做：撤销意图日志与当前磁盘对不上，不能猜着继续",
                f"  - {exc}",
                "继续写会覆盖用户后来的修改，或者重复已经生效的动作。",
                "处置建议：先备份现场，再用 `tianshu recover` 核对；确认后人工"
                "处理（或删除 artifacts/undo/<run_id>.json 后重新评估）。",
            ],
        ) from exc
    return UndoPlan(
        run_id=journal.run_id,
        # 已完成的 journal 没有"还要做"的动作；CLI 预览会显示为空。
        items=[] if journal.completed else _journal_items(journal),
        roots=roots,
        scope=resolved,
        scope_workspace=scope_workspace,
    )


def _journal_payload(
    journal: undo_journal.UndoJournal,
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    restored: list[str] = []
    deleted: list[str] = []
    entries: list[dict[str, str]] = []
    for done in journal.done:
        item = done.item
        if item.action == undo_journal.ACTION_DELETE:
            deleted.append(item.path)
            result = "deleted"
        else:
            restored.append(item.path)
            result = "restored"
        entries.append({"root": item.root, "path": item.path, "result": result})
    return restored, deleted, entries


def _outcome_from_journal(journal: undo_journal.UndoJournal) -> UndoOutcome:
    restored, deleted, entries = _journal_payload(journal)
    return UndoOutcome(
        ok=True,
        restored=restored,
        deleted=deleted,
        entries=entries,
        effect="applied",
        control_run=journal.control_run,
    )


def _emit_completed_once(stack, journal: undo_journal.UndoJournal | None) -> None:
    """完成事件只能有一条：journal 已完成但事件缺失时补写，已存在则不重复。"""
    if journal is None:  # pragma: no cover - 撤销成功必有 journal
        raise TianshuError("撤销完成却没有意图日志终态：拒绝写假事件")
    if any(
        event.type == "undo.completed" for event in stack.log.read_run(journal.run_id)
    ):
        return
    restored, deleted, entries = _journal_payload(journal)
    stack.log.append(
        journal.run_id,
        "undo.completed",
        {
            "run_id": journal.run_id,
            "restored": restored,
            "deleted": deleted,
            "entries": entries,
            "control_run": journal.control_run,
        },
        actor_kind="human",
        idem_key=f"undo.completed:{journal.run_id}",
    )


def _close_orphan_control(stack, control_run_id: str) -> None:
    """尽力收掉崩溃/失败遗留的控制 Run；收不掉也不把它当成功。"""
    if not control_run_id:
        return
    run = stack.tasks.get_run(control_run_id)
    job = stack.claims.job_for_run(control_run_id)
    if run is None or job is None or run.state in TERMINAL_STATES:
        return
    task = stack.tasks.get_task(run.task_id)
    claim = JobClaim(
        job_id=job.job_id,
        run_id=control_run_id,
        owner_instance_id=job.owner_instance_id or "",
        owner_pid=job.owner_pid or 0,
        claim_epoch=job.claim_epoch,
    )
    try:
        stack.dispatcher.close_control_run(
            ControlRun(task=task, run=run, claim=claim, lease_paths=()),
            outcome="failed",
            effect_state="partial",
        )
    except TianshuError:
        # 收尾失败不阻断续做：续做会用新的控制 Run，并把它写回 journal。
        pass


def _control_for_resume(stack, journal: undo_journal.UndoJournal) -> ControlRun:
    """接管上次没来得及收尾的控制 Run；不可复用则另开一个并改写 journal。

    崩溃后控制 Run 还是 `running`、作业仍被已死的实例 claimed。复用同一个
    Run 保留审计链（完成事件里的 control_run 不变）；只有当它已收尾/无法认领
    时才另开。**并发的活实例**持有作业时拒绝，不抢。
    """
    control_run_id = journal.control_run
    run = stack.tasks.get_run(control_run_id) if control_run_id else None
    job = stack.claims.job_for_run(control_run_id) if run is not None else None
    if run is not None and job is not None and run.state not in TERMINAL_STATES:
        if job.state == JOB_CLAIMED and stack.claims.is_live(job):
            raise UndoRefused(
                "撤销正由另一个活着的实例执行",
                code=2,
                lines=[
                    "拒绝续做：这个未完成的撤销正被另一个活着的实例持有"
                    "（作业状态仍是 claimed）。",
                    "并发续做会让两个进程同时写同一批文件；请等它结束或确认它已退出。",
                ],
            )
        if job.state not in TERMINAL_JOB_STATES:
            try:
                claim = stack.claims.claim(job.job_id)
            except (JobLost, JobUnavailable):
                claim = None
            if claim is not None:
                task = stack.tasks.get_task(run.task_id)
                if task is not None:
                    writable = [b.identity() for b in task.workspaces if b.can_write]
                    stack.leases.acquire(writable, run.run_id)
                    return ControlRun(
                        task=task,
                        run=run,
                        claim=claim,
                        lease_paths=tuple(
                            str(b.root_path) for b in task.workspaces if b.can_write
                        ),
                    )
    # 旧控制 Run 不可复用：先尽力收尾，再为续做另开一个控制 Run。
    _close_orphan_control(stack, control_run_id)
    roots = sorted({item.root for item in journal.items})
    control = stack.dispatcher.open_control_run(
        goal=f"undo:{journal.run_id}",
        workspaces=[WorkspaceBinding(root) for root in roots],
    )
    stack.snapshots.init_run(control.run.run_id)
    stack.snapshots.journal.set_control_run(journal, control.run.run_id)
    return control


def plan_undo(stack, run_id: str, *, scope: Path | None = None) -> UndoPlan:
    """只读预演：不写文件、不登记审批、不改事件日志。

    拒绝条件都发生在**动手之前**——撤销写错目录比撤销失败严重得多。
    已有未完成 journal 时，这里只核对「能不能续做、还要做哪些」，不重排计划。
    """
    journal = _load_journal(stack.snapshots, run_id)
    if journal is not None:
        _check_event_consistency(stack.log, journal)
        return _plan_journal_undo(stack.snapshots, journal, scope)
    try:
        items = _load_items(stack, run_id)
    except SnapshotCorrupt as exc:
        # 清单读不懂：既不知道改过哪些文件，也不知道该还原成什么。按猜的
        # 清单动手就是数据破坏，所以这里和"没有根"同一档——拒绝，并说清
        # 还能怎么补救。裸的解析异常会漏到 CLI 边界，只说"Expecting value"。
        raise UndoRefused(
            "撤销证据损坏",
            code=2,
            lines=[
                "拒绝撤销：这次 Run 的写前证据（快照清单）读不出来",
                f"  - {exc}",
                "清单损坏时无法确认哪些文件被改过、该还原成什么；",
                "按猜的清单撤销就是数据破坏，所以一个字节都不动。",
                "处置建议：先用 `tianshu recover --run-id <ID> --workspace <目录>` "
                "核对，或按 artifacts/snapshots/<ID>.json 手工还原（先备份）。",
            ],
        ) from exc
    if not items:
        raise UndoRefused(
            "没有可撤销的变更",
            code=1,
            lines=[f"run {run_id} 没有可撤销的变更"],
        )

    legacy = [item for item in items if not item.root]
    if legacy:
        # 没有 root 就不知道这些写入落在哪个目录。用 --workspace 顶上就是猜：
        # 猜错 = 把另一个目录下的同名文件改掉，这是数据破坏。
        lines = [
            "拒绝撤销：该 run 的快照是旧格式（v2，没有 workspace 根）",
            *[f"  - {item.path}（call_id={item.call_id}）" for item in legacy],
            "没有根就无法确定这些写入落在哪个目录。撤销写错目录比撤销失败"
            "严重得多，所以这里不猜。",
        ]
        if scope is not None:
            lines.append(f"已忽略 --workspace {scope}：旧记录没有根，拿它顶上就是猜。")
        lines.append(
            "处置建议：用 `tianshu recover --run-id <ID> --workspace <目录>` "
            "逐目录核对，或按 artifacts/snapshots/<ID>.json 手工还原。"
        )
        raise UndoRefused("撤销证据缺少 workspace 根", code=2, lines=lines)

    resolved = scope.resolve() if scope is not None else None
    selected = (
        items if resolved is None else [i for i in items if i.root == str(resolved)]
    )
    if not selected:
        raise UndoRefused(
            f"该 run 在 {resolved} 下没有可撤销的变更",
            code=2,
            lines=[
                f"拒绝撤销：该 run 在 {resolved} 下没有可撤销的变更",
                f"已记录的 workspace：{'、'.join(sorted({i.root for i in items}))}",
            ],
        )
    scope_workspace: Workspace | None = None
    if resolved is not None:
        try:
            scope_workspace = Workspace(resolved)
        except TianshuError as exc:
            # 目录不在/不是目录：说清楚再退出，不要伪装成"撤销中断"。
            raise UndoRefused(
                "workspace 不可用",
                code=2,
                lines=[f"拒绝撤销：workspace 不可用：{resolved}（{redact(str(exc))}）"],
            ) from exc

    return UndoPlan(
        run_id=run_id,
        items=selected,
        roots=sorted({item.root for item in selected}),
        scope=resolved,
        scope_workspace=scope_workspace,
    )


def _dispatch_apply(stack, control):
    """把每个"还原/删除"都变成一次经审批的派发。

    落盘目录由**证据自己的 root** 决定：撤销 wsA 的变更时，wsB 下的同名文件
    必须一个字节都不动。
    """

    def apply(change, preimage: bytes | None) -> None:
        if not change.root:  # 预检已经拒绝过旧格式，这里是第二道防线
            raise ToolNotAllowed("撤销证据缺少 workspace 根，拒绝写盘")
        target = Workspace(Path(change.root)).resolve(change.path)
        if preimage is None:
            tool_id = "delete_file"
            call_id = f"undo_del_{change.call_id}"
            tool_args = {"path": change.path}
        else:
            tool_id = "restore_file"
            call_id = f"undo_res_{change.call_id}"
            tool_args = {
                "path": change.path,
                "content_b64": base64.b64encode(preimage).decode("ascii"),
                "sha256": change.before_state,
            }

        request = build_request(
            task=control.task,
            run=control.run,
            claim=control.claim,
            call_id=call_id,
            tool_id=tool_id,
            target=target,
            args=tool_args,
        )
        decision = stack.dispatcher.prepare(request, control.claim, args=tool_args)
        if decision.is_rejected:
            raise ToolNotAllowed(
                f"撤销动作被拒绝：{decision.reason}（{decision.rule_id}）"
            )
        if decision.is_waiting:
            # 人在 UI/CLI 上明确点了"撤销"就是批准：显式、可审计，且仍然走
            # 完整的"批准一次只能执行一次"记录。
            stack.approvals.decide(
                decision.approval_id,
                decision="approved",
                actor=HumanIdentity.local_cli(),
            )
        result = stack.dispatcher.dispatch(decision.request_id, control.claim)
        if not result.ok:
            raise ToolNotAllowed(f"撤销动作未执行：{result.output.get('error')}")

    return apply


def apply_undo(
    stack,
    run_id: str,
    *,
    scope: Path | None = None,
) -> UndoOutcome:
    """执行撤销（或续做未完成的撤销）。

    前置拒绝抛 `UndoRefused`；执行中的失败返回 `ok=False` + partial。

    journal 语义：
    - 没有 journal：预检 → 落盘完整计划 → 逐动作执行并记录 → completed；
    - 有 in_progress：先核对磁盘（done 与剩余项），从断点继续，绝不从头再来；
    - 有 completed 但缺 `undo.completed` 事件：只核对 + 幂等补写事件，不动文件。
    """
    store = stack.snapshots
    journal = _load_journal(store, run_id)
    if journal is not None:
        _check_event_consistency(stack.log, journal)

    if journal is not None and journal.completed:
        # 全是已完成的动作：不再动文件；只核对"记录与磁盘一致"后补事件。
        try:
            store.check_undo_journal(journal)
        except undo_journal.UndoJournalConflict as exc:
            raise UndoRefused(
                "撤销已完成，但磁盘状态与日志不一致",
                code=2,
                lines=[
                    "拒绝补写完成事件：撤销日志说这个 run 已经撤完，磁盘却对不上",
                    f"  - {exc}",
                    "补写一条「已完成」事件会把不一致盖住；所以事件也不写，"
                    "一个字节都不动。",
                    "处置建议：先备份现场，再人工核对 artifacts/undo/ 与工作区。",
                ],
            ) from exc
        # 崩溃可能留下未收尾的控制 Run：补写事件前顺手收掉（幂等，失败不阻断）。
        _close_orphan_control(stack, journal.control_run)
        _emit_completed_once(stack, journal)
        return _outcome_from_journal(journal)

    plan = plan_undo(stack, run_id, scope=scope)

    def guard() -> None:
        # 每个会被写到的目录都要过阻断检查，不是只看其中一个。
        for root in plan.roots:
            ensure_not_blocked(stack.log, root)

    # 重新读一次：plan_undo 只读，但两次读之间若有人写了 journal（并发），
    # 以磁盘上的最新状态为准；绝不用第一份内存快照覆盖第二份证据。
    journal = _load_journal(store, run_id)
    if journal is not None:
        control = _control_for_resume(stack, journal)
    else:
        control = stack.dispatcher.open_control_run(
            goal=f"undo:{run_id}",
            workspaces=[WorkspaceBinding(root) for root in plan.roots],
        )
        stack.snapshots.init_run(control.run.run_id)
    try:
        report = stack.snapshots.undo(
            run_id,
            plan.scope_workspace,
            guard=guard,
            apply=_dispatch_apply(stack, control),
            journal_control_run=control.run.run_id,
        )
    except Exception as exc:
        # 撤销是多文件操作：中途失败必须如实记成 partial，不能悄悄停在
        # 一半还让日志看起来完好。已经完成的那些文件是真的改了。
        stack.log.append(
            run_id,
            "undo.failed",
            {
                "run_id": run_id,
                "control_run": control.run.run_id,
                # 事件日志是永久证据，写进去就擦不掉：先打码。
                "error": redact(f"{type(exc).__name__}: {exc}"),
            },
            actor_kind="system",
        )
        stack.dispatcher.close_control_run(
            control, outcome="failed", effect_state="partial"
        )
        raise

    if report.ok:
        # journal 已经 completed；先落这个故障点是为了证明"完成事件缺失"
        # 也能被幂等补写，而不是靠顺序假设。
        failpoint("after_undo_journal_complete")
        _emit_completed_once(stack, report.journal)
    # 一个字节都没动的时候就记 none_applied，别用 partial 冒充"改了一半"。
    if report.ok:
        effect = "applied"
    elif report.restored or report.deleted:
        effect = "partial"
    else:
        effect = "none_applied"
    stack.dispatcher.close_control_run(
        control,
        outcome="succeeded" if report.ok else "failed",
        effect_state=effect,
    )
    return UndoOutcome(
        ok=report.ok,
        restored=list(report.restored),
        deleted=list(report.deleted),
        conflicts=list(report.conflicts),
        problems=list(report.problems),
        entries=list(report.entries),
        effect=effect,
        control_run=control.run.run_id,
    )


__all__ = [
    "UndoItem",
    "UndoOutcome",
    "UndoPlan",
    "UndoRefused",
    "apply_undo",
    "plan_undo",
]