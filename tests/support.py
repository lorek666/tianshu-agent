"""测试用的"本地人审"替身。

M2 之后覆盖/删除类写入必须有批准才能执行（PRD §2.3）。测试要证明的往往是
"批准之后到底做了什么"，而不是"没批准会怎样"——后者各有用例单独覆盖。
这个模块把"人在这条命令上敲 approve"这一步自动化，其余路径全走真实服务层：

    看到待批 → approvals.decide(approved) → resume_run

它不是库的一部分（所以不在 `src/` 里）：在生产代码里放一个自动批准函数，
迟早会有人把它当默认路径用。
"""

from __future__ import annotations

from pathlib import Path

from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, Stack, build_stack
from tianshu.model import Model
from tianshu.permissions import HumanIdentity
from tianshu.snapshot import SnapshotStore


def stack_for(
    log: EventLog,
    *,
    snapshots: SnapshotStore | None = None,
    data_root: Path | None = None,
) -> Stack:
    return build_stack(log, snapshots=snapshots, data_root=data_root)


def engine_for(
    stack: Stack, model: Model, *, limits: Limits | None = None
) -> AgentLoop:
    return AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        model=model,
        limits=limits,
    )


def pending_approvals(stack: Stack, run_id: str) -> list[str]:
    return [a.approval_id for a in stack.approvals.pending(run_id=run_id)]


def approve_pending(stack: Stack, run_id: str) -> list[str]:
    """批准这个 Run 当前所有待批请求。返回被批准的 id。"""
    ids = pending_approvals(stack, run_id)
    actor = HumanIdentity.local_cli()
    for approval_id in ids:
        stack.approvals.decide(approval_id, decision="approved", actor=actor)
    return ids


__all__ = [
    "approve_pending",
    "engine_for",
    "pending_approvals",
    "stack_for",
]