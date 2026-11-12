"""固定用户级「权威根」：跨数据根的阻断状态。

`--db` / `--data-root` / cwd 都能换，换一个数据根就能让同一个工作区的阻断
「看不见」——那是 M2 遗留的 authority 数据根逃逸（issue #61.1）。本模块把
「这个目录被阻断过」这件事钉到**固定的用户级位置**，与调用方选哪个事件库无关：

- 路径 = `TIANSHU_AUTHORITY_ROOT`，否则 `Path.home()/".tianshu"/"authority"`；
- 只有**阻断**状态进这里。事件日志仍是唯一事实源，这里存的是「跨数据根的
  安全告示牌」：数据根自己的日志（append-only）记"为什么阻断"，本模块记
  "整台机器上这些目录现在不许写"。
- 阻断对象有**两种**，分别两张表：`blocked_workspaces`（目录，文件写用）与
  `blocked_objects`（外部对象，当前只有 `mcp://server/tool` 与 server 级
  `mcp://server`）。**不合并成一张表**：目录键经过 `overlap_key` 规范化（大小
  写/Unicode 折叠、resolved 路径），对象键是 URI，两者的"同一个"判定规则不同；
  混在一张表里会让"同一目录两种写法"与"两个不同 server"用同一套比较。

失败语义（安全方向，全是「宁可挡住」）：

- 库/文件**不存在** = 这台机器还没有任何阻断记录 = **空集**。不能当成损坏，
  否则每个新用户第一次用都会被自己挡住；读路径也**不**创建目录/库。
- 文件存在但读不出来（坏库、锁死、权限）→ `AuthorityUnavailable`，
  调用方必须按**已阻断**处理（不是「没阻断」）。
- 写失败 → `AuthorityWriteFailed`，调用方必须如实说「跨数据根阻断未生效」，
  **不许**当成写成功。

并发：多个进程会同时打开这同一个库。用 WAL + busy_timeout(5s) + 极短事务，
事务绝不跨到派发/执行期间。写入失败区分「锁忙」（暂时不可用）与「坏」，
两者都如实抛错。

键口径与 `ResourceIdentity` / `pathkeys` 一致：`Path.resolve()` 之后再
`overlap_key`（Unicode NFC + 大小写折叠），否则同一目录的两种写法就能绕过。
原始绝对路径另存一列用于排障。

注意：SQLite 的 WAL 库在第一次读取时会由 SQLite 自己建 `-wal`/`-shm`
（执行 SQLite 的 WAL 语义所必需）。那是 sqlite 内部文件，不改变本模块的
「缺失=空集 / 坏=阻断」判定；本模块自己绝不在读路径上 mkdir 或写记录。
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import TianshuError
from .pathkeys import overlap_key
from .resources import ResourceError, parse_mcp_object_key

AUTHORITY_ROOT_ENV = "TIANSHU_AUTHORITY_ROOT"
DB_NAME = "authority.db"
BUSY_TIMEOUT_MS = 5_000

#: 表结构是本模块的契约：一个目录一行，`released_at` 非空表示已解除。
#: 解除不删行——"曾经阻断过、谁因为什么解除"是审计材料。
SCHEMA = """
CREATE TABLE IF NOT EXISTS blocked_workspaces (
    workspace_key   TEXT PRIMARY KEY,
    workspace_path  TEXT NOT NULL,
    run_id          TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    blocked_at      INTEGER NOT NULL,
    released_at     INTEGER,
    release_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_blocked_workspaces_active
    ON blocked_workspaces(released_at);

-- MCP 对象阻断（对象级阻断，见 docs/mcp-g2.md §5-10）：阻断对象是外部工具，
-- 不是本地目录。键 = `mcp://<server>/<tool>` 或 server 级 `mcp://<server>`
-- （遮住该 server 的全部工具）。与 blocked_workspaces 分开建表，理由见模块
-- 说明：两种键的"同一个"判定规则不同。
-- 旧版本库没有这张表 = 从来没有过对象阻断 = 空集（不是损坏：旧版本写不出
-- 对象阻断）。读路径不创建任何东西，写路径才建表。
CREATE TABLE IF NOT EXISTS blocked_objects (
    object_key      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    blocked_at      INTEGER NOT NULL,
    released_at     INTEGER,
    release_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_blocked_objects_active
    ON blocked_objects(released_at);
"""


class AuthorityUnavailable(TianshuError):
    """权威根存在但读不出来。调用方必须按**已阻断**处理（不能当成没阻断）。"""


class AuthorityWriteFailed(TianshuError):
    """权威根写入失败。跨数据根阻断没有生效，调用方必须如实报告。"""


@dataclass(frozen=True)
class AuthorityBlock:
    """一条当前有效的目录阻断记录。"""

    workspace_key: str
    workspace_path: str
    run_id: str
    reason: str
    blocked_at: int


@dataclass(frozen=True)
class AuthorityObjectBlock:
    """一条当前有效的**对象**阻断记录（`mcp://server/tool` / `mcp://server`）。

    没有 `path`：对象不是本地路径。原始键原样保存（URI 已是规范形状：
    `McpTarget` 只接受 `[a-z0-9_.-]` 段），比较用逐字节相等。
    """

    object_key: str
    run_id: str
    reason: str
    blocked_at: int


def authority_root() -> Path:
    """固定的用户级权威根。每次调用现读环境变量（测试可注入，进程内可切换）。"""
    env = os.environ.get(AUTHORITY_ROOT_ENV)
    raw = Path(env) if env else Path.home() / ".tianshu" / "authority"
    return raw.expanduser().resolve()


def workspace_key(path: Path | str) -> str:
    """与 `ResourceIdentity` / `pathkeys` 一致的目录键（不是资源 inode 键）。

    权威根要跨重启/备份存活，所以不能存 inode：resolved 路径 + Unicode NFC
    + 大小写折叠（大小写不敏感文件系统上）。两种写法指向同一目录时键相同，
    同一目录就不可能因为换一种写法绕过阻断。
    """
    return overlap_key(str(Path(path).resolve()))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _db_path(root: Path | None = None) -> Path:
    return (root if root is not None else authority_root()) / DB_NAME


def _is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """只读连接上的"这张表在不在"。

    用途只有一个：区分「旧版本库没有这张表 = 空集」与「表在但读坏了 = 按阻断
    处理」。拿"查表失败"当空集会替一个读不出来的状态撒谎，拿"缺表"当损坏则
    让每个升级用户第一次用就被自己挡住。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _open_ro(db: Path) -> sqlite3.Connection | None:
    """只读打开。库不存在返回 None（= 空集），**不**创建任何目录/文件。"""
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{db}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # 触发一次真实读取：坏库/坏表在这里暴露，而不是在调用方手里变成空集。
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise AuthorityUnavailable(
            f"权威阻断状态读不出来（{db}）：{type(exc).__name__}: {exc}。"
            "按已阻断处理（读不出来 ≠ 没阻断）"
        ) from exc


def _open_rw(root: Path) -> sqlite3.Connection:
    """读写打开：只在写路径用，这里才创建目录。"""
    try:
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(root / DB_NAME), timeout=BUSY_TIMEOUT_MS / 1000
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA)
        return conn
    except (sqlite3.Error, OSError) as exc:
        hint = "权威状态暂时不可用（锁忙）" if _is_lock_error(exc) else "权威根损坏"
        raise AuthorityWriteFailed(
            f"{hint}：无法打开权威根 {root}（{type(exc).__name__}: {exc}）"
        ) from exc


def _query_active(conn: sqlite3.Connection) -> list[AuthorityBlock]:
    rows = conn.execute(
        "SELECT workspace_key, workspace_path, run_id, reason, blocked_at"
        " FROM blocked_workspaces WHERE released_at IS NULL ORDER BY blocked_at"
    ).fetchall()
    return [
        AuthorityBlock(
            workspace_key=row["workspace_key"],
            workspace_path=row["workspace_path"],
            run_id=row["run_id"],
            reason=row["reason"],
            blocked_at=row["blocked_at"],
        )
        for row in rows
    ]


def active_blocks(root: Path | None = None) -> list[AuthorityBlock]:
    """当前有效的阻断记录。库不存在 = 空集；存在但读不出 → AuthorityUnavailable。"""
    conn = _open_ro(_db_path(root))
    if conn is None:
        return []
    try:
        return _query_active(conn)
    except sqlite3.Error as exc:
        raise AuthorityUnavailable(
            f"权威阻断状态读不出来（{_db_path(root)}）：{type(exc).__name__}: {exc}。"
            "按已阻断处理（读不出来 ≠ 没阻断）"
        ) from exc
    finally:
        conn.close()


def active_paths(root: Path | None = None) -> list[str]:
    """当前有效阻断的目录路径（原始绝对路径，用于排障与祖先判断）。"""
    return [b.workspace_path for b in active_blocks(root)]


def _query_active_objects(conn: sqlite3.Connection) -> list[AuthorityObjectBlock]:
    rows = conn.execute(
        "SELECT object_key, run_id, reason, blocked_at FROM blocked_objects"
        " WHERE released_at IS NULL ORDER BY blocked_at"
    ).fetchall()
    for row in rows:
        try:
            parse_mcp_object_key(row["object_key"])
        except ResourceError as exc:
            # 表里出现形状不合法的键（只可能来自手工写库/旧 bug）：它永远匹配不上
            # 真实目标，当成"一条不匹配的阻断"就是 fail-open。判定不了 → 按已阻断
            # 处理（读侧抛 AuthorityUnavailable，守卫会转成 McpObjectBlocked）。
            raise AuthorityUnavailable(
                "权威对象阻断表里有形状不合法的键"
                f"（{row['object_key']!r}）：{exc}。按已阻断处理"
            ) from exc
    return [
        AuthorityObjectBlock(
            object_key=row["object_key"],
            run_id=row["run_id"],
            reason=row["reason"],
            blocked_at=row["blocked_at"],
        )
        for row in rows
    ]


def active_object_blocks(root: Path | None = None) -> list[AuthorityObjectBlock]:
    """当前有效的对象阻断记录。

    库不存在 = 空集；**表不存在**（旧版本库）= 空集——旧版本没有对象阻断这个
    概念，写不出这类记录，所以"没有表"能被证明就是空集；表在但读不出来 →
    `AuthorityUnavailable`（不可当成空集）。
    """
    conn = _open_ro(_db_path(root))
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "blocked_objects"):
            return []
        return _query_active_objects(conn)
    except sqlite3.Error as exc:
        raise AuthorityUnavailable(
            f"权威对象阻断状态读不出来（{_db_path(root)}）："
            f"{type(exc).__name__}: {exc}。按已阻断处理（读不出来 ≠ 没阻断）"
        ) from exc
    finally:
        conn.close()


def active_object_keys(root: Path | None = None) -> list[str]:
    """当前有效对象阻断的原始键（排障与文档用）。"""
    return [b.object_key for b in active_object_blocks(root)]


def is_blocked(path: Path | str, root: Path | None = None) -> bool:
    """这个目录是否在权威根里被标成阻断。

    单行查询（`SELECT 1 ... LIMIT 1`），不做全表 `select *`：写入守卫每次都要
    调它，权威记录数应当很少，但不该因此变成"日志越大越慢"。
    """
    db = _db_path(root)
    conn = _open_ro(db)
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM blocked_workspaces"
            " WHERE workspace_key=? AND released_at IS NULL LIMIT 1",
            (workspace_key(path),),
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        raise AuthorityUnavailable(
            f"权威阻断状态读不出来（{db}）：{type(exc).__name__}: {exc}。"
            "按已阻断处理（读不出来 ≠ 没阻断）"
        ) from exc
    finally:
        conn.close()


def is_object_blocked(
    object_keys: str | Iterable[str], root: Path | None = None
) -> bool:
    """这些对象键里有没有**当前有效**的阻断。

    调用方一次传 `(工具级, server 级)` 两个键：server 级阻断要能遮住它的全部
    工具，所以"查一个键"不够。单行查询、`LIMIT 1`：写入路径每次发外之前都要问，
    这不是可以随记录变多而变慢的查询。
    """
    if isinstance(object_keys, str):
        keys = (object_keys,)
    else:
        keys = tuple(str(key) for key in object_keys)
    if not keys:
        return False
    db = _db_path(root)
    conn = _open_ro(db)
    if conn is None:
        return False
    try:
        if not _table_exists(conn, "blocked_objects"):
            return False
        placeholders = ",".join("?" * len(keys))
        row = conn.execute(
            "SELECT 1 FROM blocked_objects"
            f" WHERE object_key IN ({placeholders}) AND released_at IS NULL"
            " LIMIT 1",
            keys,
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        raise AuthorityUnavailable(
            f"权威对象阻断状态读不出来（{db}）：{type(exc).__name__}: {exc}。"
            "按已阻断处理（读不出来 ≠ 没阻断）"
        ) from exc
    finally:
        conn.close()


def register_object_blocked(
    object_key: str, *, run_id: str = "", reason: str = "",
    root: Path | None = None,
) -> None:
    """登记一条对象阻断（幂等：已有效则保持原记录；已解除则重新激活）。"""
    key = str(object_key)
    try:
        parse_mcp_object_key(key)
    except ResourceError:
        # 写路径先校验：`mcp://A` / `mcp://` / `mcp://a/b/c` 写进去只会变成一条
        # **永远读不到**的阻断记录（真实目标段只接受 [a-z0-9_.-]），那等于"看起来
        # 记了、实际没记"。直接拒绝，让调用方当场发现。
        raise
    target = root if root is not None else authority_root()
    conn = _open_rw(target)
    try:
        with conn:  # 极短事务：单条 upsert，绝不跨到调用方的执行期间
            row = conn.execute(
                "SELECT released_at FROM blocked_objects WHERE object_key=?",
                (key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO blocked_objects"
                    " (object_key, run_id, reason, blocked_at)"
                    " VALUES (?,?,?,?)",
                    (key, run_id, reason, _now_ms()),
                )
            elif row["released_at"] is not None:
                conn.execute(
                    "UPDATE blocked_objects SET run_id=?, reason=?, blocked_at=?,"
                    " released_at=NULL, release_reason=NULL WHERE object_key=?",
                    (run_id, reason, _now_ms(), key),
                )
            # 已有效：幂等返回，不刷新 blocked_at（否则每次恢复都像"刚阻断"）
    except sqlite3.Error as exc:
        hint = "权威状态暂时不可用（锁忙）" if _is_lock_error(exc) else "权威根损坏"
        raise AuthorityWriteFailed(
            f"{hint}：跨数据根对象阻断未生效（{key}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        conn.close()


def register_blocked(
    path: Path | str, *, run_id: str = "", reason: str = "",
    root: Path | None = None,
) -> None:
    """登记一条阻断（幂等：已有效则保持原记录；已解除则重新激活）。"""
    key = workspace_key(path)
    resolved = str(Path(path).resolve())
    target = root if root is not None else authority_root()
    conn = _open_rw(target)
    try:
        with conn:  # 极短事务：单条 upsert，绝不跨到调用方的执行期间
            row = conn.execute(
                "SELECT released_at FROM blocked_workspaces WHERE workspace_key=?",
                (key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO blocked_workspaces"
                    " (workspace_key, workspace_path, run_id, reason, blocked_at)"
                    " VALUES (?,?,?,?,?)",
                    (key, resolved, run_id, reason, _now_ms()),
                )
            elif row["released_at"] is not None:
                conn.execute(
                    "UPDATE blocked_workspaces SET workspace_path=?, run_id=?,"
                    " reason=?, blocked_at=?, released_at=NULL, release_reason=NULL"
                    " WHERE workspace_key=?",
                    (resolved, run_id, reason, _now_ms(), key),
                )
            # 已有效：幂等返回，不刷新 blocked_at（否则每次恢复都像"刚阻断"）
    except sqlite3.Error as exc:
        hint = "权威状态暂时不可用（锁忙）" if _is_lock_error(exc) else "权威根损坏"
        raise AuthorityWriteFailed(
            f"{hint}：跨数据根阻断未生效（{resolved}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        conn.close()


def backfill_blocked(
    paths: list[str], *, run_id: str = "", reason: str = "",
    root: Path | None = None,
) -> list[str]:
    """把日志里「当前有效但权威根没有」的阻断补登记（幂等，`INSERT OR IGNORE`）。

    只补不覆盖：已有的有效记录保持原 `blocked_at`/`reason`；已解除的记录
    **不会**被日志里的旧阻断复活（调用方只应传"日志当前有效"的目录）。
    返回失败项（路径 + 原因），调用方写进恢复报告，不炸启动。
    """
    if not paths:
        return []
    target = root if root is not None else authority_root()
    problems: list[str] = []
    try:
        conn = _open_rw(target)
    except AuthorityWriteFailed as exc:
        return [f"跨数据根阻断未生效：{p}（{exc}）" for p in paths]
    try:
        with conn:
            for raw in sorted(set(paths)):
                key = workspace_key(raw)
                resolved = str(Path(raw).resolve())
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO blocked_workspaces"
                        " (workspace_key, workspace_path, run_id, reason, blocked_at)"
                        " VALUES (?,?,?,?,?)",
                        (key, resolved, run_id, reason, _now_ms()),
                    )
                except sqlite3.Error as exc:
                    problems.append(f"跨数据根阻断未生效：{resolved}（{exc}）")
    finally:
        conn.close()
    return problems


def backfill_object_blocked(
    object_keys: list[str], *, run_id: str = "", reason: str = "",
    root: Path | None = None,
) -> list[str]:
    """把日志里「当前有效但权威根没有」的对象阻断补登记（幂等，`INSERT OR IGNORE`）。

    与目录版同一条理由（issue #61.1）：只写在某个数据根的日志里，换个 `--db`
    就看不见。只补不覆盖：已解除的记录**不会**被日志里的旧阻断复活——调用方
    只应传"日志当前有效"的对象。返回失败项，调用方写进报告，不炸启动。
    """
    if not object_keys:
        return []
    target = root if root is not None else authority_root()
    problems: list[str] = []
    try:
        conn = _open_rw(target)
    except AuthorityWriteFailed as exc:
        return [f"跨数据根对象阻断未生效：{key}（{exc}）" for key in object_keys]
    try:
        with conn:
            for key in sorted(set(object_keys)):
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO blocked_objects"
                        " (object_key, run_id, reason, blocked_at)"
                        " VALUES (?,?,?,?)",
                        (str(key), run_id, reason, _now_ms()),
                    )
                except sqlite3.Error as exc:
                    problems.append(f"跨数据根对象阻断未生效：{key}（{exc}）")
    finally:
        conn.close()
    return problems


def release_blocked(
    path: Path | str, *, reason: str = "", root: Path | None = None
) -> bool:
    """释放一条阻断。返回是否真的释放了一条有效记录。

    库不存在/没有有效记录 = 没有东西要释放（不是错误）；写失败抛
    `AuthorityWriteFailed`——调用方必须保持阻断方向并如实报错，不能假装成功。
    """
    db = _db_path(root)
    if not db.exists():
        return False
    key = workspace_key(path)
    try:
        conn = sqlite3.connect(str(db), timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            cur = conn.execute(
                "UPDATE blocked_workspaces SET released_at=?, release_reason=?"
                " WHERE workspace_key=? AND released_at IS NULL",
                (_now_ms(), reason, key),
            )
            released = cur.rowcount > 0
        conn.close()
        return released
    except (sqlite3.Error, OSError) as exc:
        hint = "权威状态暂时不可用（锁忙）" if _is_lock_error(exc) else "权威根损坏"
        raise AuthorityWriteFailed(
            f"{hint}：阻断未释放（{db}）：{type(exc).__name__}: {exc}"
        ) from exc


def release_object_blocked(
    object_key: str, *, reason: str = "", root: Path | None = None
) -> bool:
    """释放一条对象阻断。返回是否真的释放了一条有效记录。

    库不存在/表不存在/没有有效记录 = 没有东西要释放（不是错误）；写失败抛
    `AuthorityWriteFailed`——调用方必须保持阻断方向并如实报错。
    """
    db = _db_path(root)
    if not db.exists():
        return False
    key = str(object_key)
    try:
        conn = sqlite3.connect(str(db), timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            if not _table_exists(conn, "blocked_objects"):
                return False
            with conn:
                cur = conn.execute(
                    "UPDATE blocked_objects SET released_at=?, release_reason=?"
                    " WHERE object_key=? AND released_at IS NULL",
                    (_now_ms(), reason, key),
                )
                released = cur.rowcount > 0
            return released
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        hint = "权威状态暂时不可用（锁忙）" if _is_lock_error(exc) else "权威根损坏"
        raise AuthorityWriteFailed(
            f"{hint}：对象阻断未释放（{db}）：{type(exc).__name__}: {exc}"
        ) from exc


__all__ = [
    "AUTHORITY_ROOT_ENV",
    "AuthorityBlock",
    "AuthorityObjectBlock",
    "AuthorityUnavailable",
    "AuthorityWriteFailed",
    "active_blocks",
    "active_object_blocks",
    "active_object_keys",
    "active_paths",
    "authority_root",
    "backfill_blocked",
    "backfill_object_blocked",
    "is_blocked",
    "is_object_blocked",
    "register_blocked",
    "register_object_blocked",
    "release_blocked",
    "release_object_blocked",
    "workspace_key",
]
