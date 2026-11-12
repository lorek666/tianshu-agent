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
- **对象级阻断（MCP 后续工单 §5-8）**：MCP 的副作用发生在 server 那一侧，阻断
  对象不是本地目录，而是 `mcp://<server>/<tool>`（工具级）或 `mcp://<server>`
  （server 级）。这一层与目录阻断**并存**：目录阻断问"这个目录还能不能写"，
  对象阻断问"这个工具还能不能再发外"。解除也分开——`unblock()` 只解目录，
  `unblock_object()` 才解对象；核对过工作区文件不代表核对过 server 侧副作用。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import authority
from .authority import AuthorityUnavailable, AuthorityWriteFailed
from .errors import EventLogCorrupt, McpObjectBlocked, WorkspaceBlocked
from .events import EventLog, sha256_hex
from .pathkeys import overlap_key
from .resources import ResourceError, parse_mcp_object_key


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


@dataclass(frozen=True)
class ObjectScan:
    """一次对象阻断回放的结果（与 `BlockScan` 同构，对象集合是 `mcp://…` 键）。"""

    blocked: frozenset[str]
    malformed: tuple[int, ...]


def scan_blocked_objects(log: EventLog) -> ObjectScan:
    """按 seq 顺序回放 object_blocked / object_unblocked，后者生效即解除。

    与目录回放同一条纪律：解析失败**不跳过**，只记下来交给调用方决定。
    """
    rows = log._conn.execute(
        "SELECT seq, type, payload FROM events "
        "WHERE type IN ('recovery.object_blocked','recovery.object_unblocked')"
        " ORDER BY seq"
    ).fetchall()
    blocked: set[str] = set()
    malformed: list[int] = []
    for row in rows:
        try:
            obj = json.loads(row["payload"])["object"]
        except (TypeError, ValueError, KeyError):
            malformed.append(int(row["seq"]))
            continue
        if not isinstance(obj, str) or not obj:
            malformed.append(int(row["seq"]))
            continue
        try:
            parse_mcp_object_key(obj)
        except ResourceError:
            # 形状不合法的对象键**永远**匹配不上真实目标（真实目标只接受
            # `[a-z0-9_.-]` 段）。把它当成"一条不匹配的有效阻断"就是 fail-open：
            # 日志里出现 `not-a-key` / `mcp://FAKE/echo` 时，真实的 `mcp://fake/echo`
            # 调用会被放行。判定不了 = 按已阻断处理。
            malformed.append(int(row["seq"]))
            continue
        if row["type"] == "recovery.object_blocked":
            blocked.add(obj)
        else:
            blocked.discard(obj)
    return ObjectScan(frozenset(blocked), tuple(malformed))


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


def blocked_objects(log: EventLog) -> set[str]:
    """当前被阻断的 MCP 对象集合：本日志回放 ∪ 用户级权威根。

    判定不了时抛错（`EventLogCorrupt` / `AuthorityUnavailable`），与
    `blocked_workspaces()` 同样的理由：返回一个凑出来的集合是第二种谎言。
    写入/发外路径请用 `ensure_object_not_blocked`，它把两种情况都转成
    `McpObjectBlocked`（正常拒绝）。
    """
    scan = scan_blocked_objects(log)
    if scan.malformed:
        seqs = ",".join(str(seq) for seq in scan.malformed)
        raise EventLogCorrupt(
            f"对象阻断记录无法判定（seq={seqs} 解析不出 object），"
            "无法回答哪些 MCP 对象被阻断"
        )
    return set(scan.blocked) | set(authority.active_object_keys())


def _guard_object_keys(log: EventLog) -> set[str]:
    """发外守卫用的对象阻断集合：日志 ∪ 权威根，任一侧判定不了就按"已阻断"处理。"""
    scan = scan_blocked_objects(log)
    if scan.malformed:
        seqs = ",".join(str(seq) for seq in scan.malformed)
        raise McpObjectBlocked(
            f"存在无法判定的对象阻断记录（seq={seqs} 解析不出 object）："
            "无法确定哪些 MCP 对象可调用，按已阻断处理"
        )
    try:
        return set(scan.blocked) | set(authority.active_object_keys())
    except AuthorityUnavailable as exc:
        raise McpObjectBlocked(
            f"权威对象阻断状态读不出来（{exc}）：无法确定哪些 MCP 对象可调用，"
            "按已阻断处理"
        ) from exc


def ensure_object_not_blocked(
    log: EventLog, object_keys: str | Iterable[str]
) -> None:
    """发外前的对象阻断核验：这批键里**任何一个**被阻断就拒绝。

    `object_keys` 由调用方一次给全（工具级 + server 级，见
    `resources.mcp_block_keys`）：server 级阻断必须能遮住它下面所有工具，所以
    不能只查工具级那一个键。

    这里**不做前缀/后缀匹配**：`mcp://a/b` 只等于它自己。拿字符串前缀猜"同一个
    server 的工具"会把 `mcp://ab` 也算进 `mcp://a` 的阻断里——一个键只能等于
    自己，server 级关系由调用方显式给第二个键表达。
    """
    if isinstance(object_keys, str):
        # 单个键（`ensure_object_not_blocked(log, "mcp://s/t")`）必须当成一个键：
        # 按字符迭代会拆成 `m`/`c`/`p`…，所有比较都落空 → 静默放行（fail-open）。
        raw: Iterable[str] = (object_keys,)
    else:
        raw = object_keys
    keys = tuple(dict.fromkeys(str(key) for key in raw))
    if not keys:
        return
    roots = _guard_object_keys(log)
    hits = tuple(key for key in keys if key in roots)
    if hits:
        raise McpObjectBlocked(
            "MCP 对象有未核实的副作用（" + "、".join(hits) + "）："
            "拒绝再发外；先核对 server 侧是否已执行，再人工解除对象阻断"
            "（解除 workspace 阻断不会解除它）",
            object_keys=hits,
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
    problems = authority.backfill_blocked(
        sorted(scan.blocked), reason="启动补登记：本数据根日志里的有效阻断"
    )
    # 对象阻断同样要跨数据根（否则换个 --db 就能绕开"这个工具不许再调"）。
    # 与目录阻断一起补登记：两条路径共用同一次启动对账。
    obj_scan = scan_blocked_objects(log)
    if obj_scan.malformed:
        seqs = ",".join(str(seq) for seq in obj_scan.malformed)
        problems.append(
            "跨数据根对象阻断未生效：本数据根存在无法判定的对象阻断记录"
            f"（seq={seqs}），无法补登记"
        )
    else:
        problems += authority.backfill_object_blocked(
            sorted(obj_scan.blocked),
            reason="启动补登记：本数据根日志里的有效对象阻断",
        )
    return problems


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


def unblock_object(log: EventLog, object_key: str, *, reason: str) -> str:
    """人工核对 server 侧之后，解除一个 MCP 对象的阻断。

    顺序与 `unblock()` 相同、理由相同：**先写日志审计事件、再释放权威根**。
    释放失败 → 日志已经不再挡、权威根仍挡（安全方向：最多"多挡一会儿"），
    异常如实抛出去，绝不假装解除成功。

    与 `unblock()` 分开而不是加参数：目录与对象是两种对象模型（键的规范化、
    解除后的含义都不同）。混成一个函数会让人以为"解了工作区就等于解了工具"，
    而那正是这次要消掉的错误口径。
    """
    # 只有形状合法的键才值得解除：解除一个永远匹配不上的键，等于什么都没做
    # 却回一句"已解除"。
    from .resources import parse_mcp_object_key

    parse_mcp_object_key(object_key)
    run_id = f"unblock_obj_{sha256_hex(object_key.encode('utf-8'))[:10]}"
    log.append(
        run_id,
        "recovery.object_unblocked",
        {"object": object_key, "reason": reason},
        actor_kind="human",
    )
    try:
        authority.release_object_blocked(object_key, reason=reason)
    except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
        raise AuthorityWriteFailed(
            f"解除事件已写入日志，但权威根释放失败：{object_key} 仍保持阻断"
            f"（{type(exc).__name__}: {exc}）"
        ) from exc
    return run_id


__all__ = [
    "AuthorityUnavailable",
    "AuthorityWriteFailed",
    "BlockScan",
    "ObjectScan",
    "assert_paths_not_blocked",
    "blocked_objects",
    "blocked_workspaces",
    "ensure_not_blocked",
    "ensure_object_not_blocked",
    "is_blocked",
    "mirror_blocks_to_authority",
    "scan_blocked",
    "scan_blocked_objects",
    "unblock",
    "unblock_object",
]
