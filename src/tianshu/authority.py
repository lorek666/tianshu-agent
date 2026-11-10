"""固定用户级「权威根」：跨数据根的阻断状态。

`--db` / `--data-root` / cwd 都能换，换一个数据根就能让同一个工作区的阻断
「看不见」——那是 M2 遗留的 authority 数据根逃逸（issue #61.1）。本模块把
「这个目录被阻断过」这件事钉到**固定的用户级位置**，与调用方选哪个事件库无关：

- 路径 = `TIANSHU_AUTHORITY_ROOT`，否则 `Path.home()/".tianshu"/"authority"`；
- 只有**阻断**状态进这里。事件日志仍是唯一事实源，这里存的是「跨数据根的
  安全告示牌」：数据根自己的日志（append-only）记"为什么阻断"，本模块记
  "整台机器上这些目录现在不许写"。

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
from dataclasses import dataclass
from pathlib import Path

from .errors import TianshuError
from .pathkeys import overlap_key

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
"""


class AuthorityUnavailable(TianshuError):
    """权威根存在但读不出来。调用方必须按**已阻断**处理（不能当成没阻断）。"""


class AuthorityWriteFailed(TianshuError):
    """权威根写入失败。跨数据根阻断没有生效，调用方必须如实报告。"""


@dataclass(frozen=True)
class AuthorityBlock:
    """一条当前有效的阻断记录。"""

    workspace_key: str
    workspace_path: str
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


__all__ = [
    "AUTHORITY_ROOT_ENV",
    "AuthorityBlock",
    "AuthorityUnavailable",
    "AuthorityWriteFailed",
    "active_blocks",
    "active_paths",
    "authority_root",
    "backfill_blocked",
    "is_blocked",
    "register_blocked",
    "release_blocked",
    "workspace_key",
]
