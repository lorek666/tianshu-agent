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

from .credentials import redact
from .dispatcher import build_request
from .errors import TianshuError, ToolNotAllowed
from .guard import ensure_not_blocked
from .permissions import HumanIdentity
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


def plan_undo(stack, run_id: str, *, scope: Path | None = None) -> UndoPlan:
    """只读预演：不写文件、不登记审批、不改事件日志。

    拒绝条件都发生在**动手之前**——撤销写错目录比撤销失败严重得多。
    """
    items = _load_items(stack, run_id)
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
    """执行撤销。前置拒绝抛 `UndoRefused`；执行中的失败返回 `ok=False` + partial。"""
    plan = plan_undo(stack, run_id, scope=scope)

    def guard() -> None:
        # 每个会被写到的目录都要过阻断检查，不是只看其中一个。
        for root in plan.roots:
            ensure_not_blocked(stack.log, root)

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
        stack.log.append(
            run_id,
            "undo.completed",
            {
                "run_id": run_id,
                "restored": report.restored,
                "deleted": report.deleted,
                # 带根的逐条记录：verify_receipts 用它认出"这次改动已被撤销"，
                # 否则撤销过的文件会一直显示"与凭据不一致"。
                "entries": report.entries,
                "control_run": control.run.run_id,
            },
            actor_kind="human",
        )
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