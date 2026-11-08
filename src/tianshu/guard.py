"""阻断检查。

上一版把"workspace 有未核实副作用就别再写"只放在 CLI 入口，
于是直接调用 `run()` 或 `execute()` 就能绕过去。

这里把它下沉到执行侧，并明确两种口径：
- 只读工具：不受阻断影响（读了也改不了什么）
- 有副作用的工具：必须有 log 可用，且 workspace 未被阻断
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import WorkspaceBlocked
from .events import EventLog


def blocked_workspaces(log: EventLog) -> set[str]:
    """按 seq 顺序回放 blocked / unblocked，后者生效即解除。"""
    rows = log._conn.execute(
        "SELECT type, payload FROM events "
        "WHERE type IN ('recovery.blocked','recovery.unblocked') ORDER BY seq"
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            ws = json.loads(r["payload"])["workspace"]
        except Exception:
            continue
        if r["type"] == "recovery.blocked":
            out.add(ws)
        else:
            out.discard(ws)
    return out


def is_blocked(log: EventLog, workspace_dir: Path | str) -> bool:
    return str(Path(workspace_dir).resolve()) in blocked_workspaces(log)


def ensure_not_blocked(log: EventLog, workspace_dir: Path | str) -> None:
    ws = str(Path(workspace_dir).resolve())
    if ws in blocked_workspaces(log):
        raise WorkspaceBlocked(
            f"workspace 存在未核实的副作用（{ws}），"
            "请先处理 unknown 记录再写入"
        )


def unblock(log: EventLog, workspace_dir: Path | str, *, reason: str) -> str:
    """人工核查完成后解除阻断。解除本身是一条审计事件，不可删除。"""
    ws = str(Path(workspace_dir).resolve())
    run_id = f"unblock_{abs(hash(ws)) % 10**10}"
    log.append(
        run_id,
        "recovery.unblocked",
        {"workspace": ws, "reason": reason},
        actor_kind="human",
    )
    return run_id


__all__ = [
    "blocked_workspaces",
    "is_blocked",
    "ensure_not_blocked",
    "unblock",
]
