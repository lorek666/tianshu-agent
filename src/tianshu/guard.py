"""阻断检查。

上一版把"workspace 有未核实副作用就别再写"只放在 CLI 入口，
于是直接调用 `run()` 或 `execute()` 就能绕过去。

这里把它下沉到执行侧，并明确两种口径：
- 只读工具：不受阻断影响（读了也改不了什么）
- 有副作用的工具：必须有 log 可用，且 workspace 未被阻断

两处来自 M1 复审（[DEBUG-M0M5.md](../docs/DEBUG-M0M5.md)）的修正：

- **中-4**：回放时对畸形 payload `except: continue`，等于替一条看不懂的阻断
  事件宣布"没有阻断"。现在把回放结果和"判定不了的事件"一起交出来，写入守卫
  见到后者就拒绝——无法判定即不得写（红线 5/11），不是静默放行。
- **中-1**：阻断事件可能在派发 phase-1 之后、落盘之前才落库，所以核验不能只做
  一次。`assert_paths_not_blocked` 是给执行边界（执行守卫内、打开文件前）的
  二次核验，按"具体要写的路径落在哪个已阻断目录下"判定。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import EventLogCorrupt, WorkspaceBlocked
from .events import EventLog, sha256_hex


@dataclass(frozen=True)
class BlockScan:
    """一次阻断回放的结果。

    `blocked` 是当前被阻断的目录集合；`malformed` 是**判定不了**的事件 seq
    （payload 不是 JSON 对象、缺 `workspace` 键、或者值不是非空字符串）。
    两者必须一起看：只看 `blocked` 就会把"不知道"当成"没阻断"。
    """

    blocked: frozenset[str]
    malformed: tuple[int, ...]


def scan_blocked(log: EventLog) -> BlockScan:
    """按 seq 顺序回放 blocked / unblocked，后者生效即解除。

    解析失败**不跳过**，只记下来交给调用方决定；本函数自己不下"没阻断"的结论。
    """
    rows = log._conn.execute(
        "SELECT seq, type, payload FROM events "
        "WHERE type IN ('recovery.blocked','recovery.unblocked') ORDER BY seq"
    ).fetchall()
    blocked: set[str] = set()
    malformed: list[int] = []
    for row in rows:
        try:
            workspace = json.loads(row["payload"])["workspace"]
        except (TypeError, ValueError, KeyError):
            malformed.append(int(row["seq"]))
            continue
        if not isinstance(workspace, str) or not workspace:
            malformed.append(int(row["seq"]))
            continue
        if row["type"] == "recovery.blocked":
            blocked.add(workspace)
        else:
            blocked.discard(workspace)
    return BlockScan(frozenset(blocked), tuple(malformed))


def _malformed_hint(scan: BlockScan) -> str:
    return ",".join(str(seq) for seq in scan.malformed)


def blocked_workspaces(log: EventLog) -> set[str]:
    """当前被阻断的目录集合。

    有判定不了的事件时抛 `EventLogCorrupt`：这份日志已经不能回答"哪些目录被
    阻断"了，返回一个凑出来的集合会是第二种谎言。写入侧请用
    `ensure_not_blocked` / `assert_paths_not_blocked`，它们转成
    `WorkspaceBlocked`（可以正常拒绝，而不是把一次派发炸成崩溃）。
    """
    scan = scan_blocked(log)
    if scan.malformed:
        raise EventLogCorrupt(
            "阻断记录无法判定（seq="
            f"{_malformed_hint(scan)} 解析不出 workspace），"
            "无法回答哪些目录被阻断"
        )
    return set(scan.blocked)


def is_blocked(log: EventLog, workspace_dir: Path | str) -> bool:
    return str(Path(workspace_dir).resolve()) in blocked_workspaces(log)


def _guard_roots(log: EventLog) -> set[str]:
    """写入守卫用的阻断根集合：判定不了就按"已阻断"处理。"""
    scan = scan_blocked(log)
    if scan.malformed:
        raise WorkspaceBlocked(
            "存在无法判定的阻断记录（seq="
            f"{_malformed_hint(scan)} 解析不出 workspace）："
            "无法确定哪些目录可写，按已阻断处理"
        )
    return set(scan.blocked)


def ensure_not_blocked(log: EventLog, workspace_dir: Path | str) -> None:
    ws = str(Path(workspace_dir).resolve())
    if ws in _guard_roots(log):
        raise WorkspaceBlocked(
            f"workspace 存在未核实的副作用（{ws}），"
            "请先处理 unknown 记录再写入"
        )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def assert_paths_not_blocked(log: EventLog, paths: Iterable[Path | str]) -> None:
    """执行边界上的二次阻断核验：要写的路径落在任何已阻断目录下就拒绝。

    与 `ensure_not_blocked(目录)` 的分工：那边问的是"这个 workspace 根禁了没"，
    这边问的是"这次真的要写的这些路径禁了没"。派发 phase-1 到落盘之间可能有
    新的 `recovery.blocked` 落库（复审 中-1），所以第二次必须贴着写操作做。
    """
    roots = _guard_roots(log)
    if not roots:
        return
    for raw in paths:
        path = Path(raw).resolve()
        for root in roots:
            if _is_under(path, Path(root)):
                raise WorkspaceBlocked(
                    f"workspace 存在未核实的副作用（{root}），"
                    f"拒绝写入 {path}：请先处理 unknown 记录再写入"
                )


def unblock(log: EventLog, workspace_dir: Path | str, *, reason: str) -> str:
    """人工核查完成后解除阻断。解除本身是一条审计事件，不可删除。"""
    ws = str(Path(workspace_dir).resolve())
    # 合成 run_id 必须跨进程可复现：以前用 `abs(hash(ws)) % 10**10`，解释器
    # 哈希随机化让同一个目录在两次运行里得到不同 id（复审 轻-3）。改用内容摘要。
    run_id = f"unblock_{sha256_hex(ws.encode('utf-8'))[:10]}"
    log.append(
        run_id,
        "recovery.unblocked",
        {"workspace": ws, "reason": reason},
        actor_kind="human",
    )
    return run_id


__all__ = [
    "BlockScan",
    "assert_paths_not_blocked",
    "blocked_workspaces",
    "ensure_not_blocked",
    "is_blocked",
    "scan_blocked",
    "unblock",
]
