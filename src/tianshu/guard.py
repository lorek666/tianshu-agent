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
- **#61.1 authority 数据根逃逸**：阻断状态原来只在本数据根的事件日志里，换个
  `--db` 就"看不见"了。现在守卫**双读**：本日志回放 ∪ 固定的用户级权威根
  （`authority.py`，与 `--db`/cwd 无关）。任一侧判定不了（日志有畸形阻断事件、
  权威根读不出来）都按**已阻断**处理——"不知道"绝不当成"没阻断"。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import authority
from .authority import AuthorityUnavailable, AuthorityWriteFailed
from .errors import EventLogCorrupt, WorkspaceBlocked
from .events import EventLog, sha256_hex
from .pathkeys import overlap_key


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
    """当前被阻断的目录集合：本日志回放 ∪ 用户级权威根。

    有判定不了的日志事件时抛 `EventLogCorrupt`：这份日志已经不能回答"哪些目录
    被阻断"了，返回一个凑出来的集合会是第二种谎言。权威根读不出来时抛
    `AuthorityUnavailable`——信息查询不替调用方下"没阻断"的结论；**写入侧**
    请用 `ensure_not_blocked` / `assert_paths_not_blocked`，它们把两种情况都
    转成 `WorkspaceBlocked`（正常拒绝），而不是把一次派发炸成崩溃。
    """
    scan = scan_blocked(log)
    if scan.malformed:
        raise EventLogCorrupt(
            "阻断记录无法判定（seq="
            f"{_malformed_hint(scan)} 解析不出 workspace），"
            "无法回答哪些目录被阻断"
        )
    return set(scan.blocked) | set(authority.active_paths())


def is_blocked(log: EventLog, workspace_dir: Path | str) -> bool:
    ws = str(Path(workspace_dir).resolve())
    return ws in blocked_workspaces(log)


def _guard_roots(log: EventLog) -> set[str]:
    """写入守卫用的阻断根集合：日志 ∪ 权威根，任一侧判定不了就按"已阻断"处理。

    权威根读不出来（坏库 / 锁死 / 权限）时抛 `WorkspaceBlocked`：这是安全方向，
    不是"没阻断"。日志里的畸形阻断事件同理（中-4）。
    """
    scan = scan_blocked(log)
    if scan.malformed:
        raise WorkspaceBlocked(
            "存在无法判定的阻断记录（seq="
            f"{_malformed_hint(scan)} 解析不出 workspace）："
            "无法确定哪些目录可写，按已阻断处理"
        )
    try:
        return set(scan.blocked) | set(authority.active_paths())
    except AuthorityUnavailable as exc:
        raise WorkspaceBlocked(
            f"权威阻断状态读不出来（{exc}）：无法确定哪些目录可写，按已阻断处理"
        ) from exc


def _same_root(a: str, b: str) -> bool:
    """两个目录是不是同一个（resolved 后比 pathkeys 键，折叠大小写/Unicode）。"""
    return overlap_key(a) == overlap_key(b)


def ensure_not_blocked(log: EventLog, workspace_dir: Path | str) -> None:
    ws = str(Path(workspace_dir).resolve())
    roots = _guard_roots(log)
    # 用 pathkeys 口径比较：同一目录换个大小写写法不能绕过权威根里的记录
    # （日志里的历史记录本来就是 resolved 字符串，两者都过一遍键不丢信息）。
    if any(_same_root(ws, root) for root in roots):
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
    阻断根来自日志 ∪ 权威根：换数据根不能把这里变成"看起来没阻断"。
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


def mirror_blocks_to_authority(log: EventLog) -> list[str]:
    """启动时把本数据根日志里**当前有效**的阻断补登记进用户级权威根（幂等）。

    这是迁移入口：上个版本的库只在日志里记阻断，不补登记的话，"换个 --db
    就绕过阻断"的洞在升级后仍然存在（老数据根挡不住新数据根）。
    对齐 `unblock` 的方向：只补「当前有效」的目录，日志里已解除的不会复活。
    返回失败项（写成"跨数据根阻断未生效"），由调用方写进恢复报告，不炸启动。
    """
    scan = scan_blocked(log)
    if scan.malformed:
        # 判定不了的记录无法推导 workspace 根：不算补登记成功，也不能假装没发生。
        return [
            "跨数据根阻断未生效：本数据根存在无法判定的阻断记录（seq="
            f"{_malformed_hint(scan)}），无法补登记"
        ]
    return authority.backfill_blocked(
        sorted(scan.blocked), reason="启动补登记：本数据根日志里的有效阻断"
    )


def unblock(log: EventLog, workspace_dir: Path | str, *, reason: str) -> str:
    """人工核查完成后解除阻断。

    顺序是**先写日志审计事件、再释放权威根**：
    - 先写日志：解除动作本身 append-only 留痕，不可删除；
    - 后释放权威根：万一释放失败，日志已经不再挡、但权威根仍挡 → 安全方向
      （最多"多挡一会儿"），且异常如实抛出去，绝不假装解除成功。
    """
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
    try:
        authority.release_blocked(ws, reason=reason)
    except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
        # 日志已经记了解除，但权威根没释放：跨数据根那一侧仍然挡着。
        # 抛出去让调用方知道"没有完全解除"，而不是回一个成功的 run_id。
        raise AuthorityWriteFailed(
            f"解除事件已写入日志，但权威根释放失败：{ws} 仍保持阻断"
            f"（{type(exc).__name__}: {exc}）"
        ) from exc
    return run_id


__all__ = [
    "AuthorityUnavailable",
    "AuthorityWriteFailed",
    "BlockScan",
    "assert_paths_not_blocked",
    "blocked_workspaces",
    "ensure_not_blocked",
    "is_blocked",
    "mirror_blocks_to_authority",
    "scan_blocked",
    "unblock",
]
