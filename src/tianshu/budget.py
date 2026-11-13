"""共享预算账本（PRD §5.6 的落地）。

## 为什么单 Run 的累加计数不够

`loop._Progress` 里的 `tokens` / `cost_total_micros` 是**事后**记账：它只知道
"已经花了多少"，回答不了"现在还能不能发下一个请求"。一旦并发（子任务、并行
工具、多个 Run 共享一份用户额度）或崩溃（预占了但没结算），事后计数就会给出
错误答案：

- 两个请求同时读到最后一份余额，都以为自己是最后一个 → 花超；
- 请求发出后进程崩了，账本上什么都没记 → 用户以为没花钱；
- provider 断流、没有 usage → 记 0 或者干脆不记，两者都在撒谎。

本模块把"能不能发"变成数据库里的一次**原子预占**，并要求每个真实 attempt 有
独立身份、按最终 usage 幂等结算。它不替代 `_Progress` 的累加（那个仍然负责
展示已知费用），而是它前面的那道闸。

## 七条规则与代码位置的对应

| PRD §5.6 | 这里 |
|---|---|
| 1 请求前原子预占；attempt 独立身份；幂等结算 | `reserve()` / `settle()`；
  唯一键 `(account_id, attempt_id)` |
| 2 子任务同时受根任务与局部上限约束；父汇总不二次计入 |
  `reserve()` 沿 `parent_id` 链逐级扣减；结算只发生一次 |
| 3 多维度记账；估算不冒充上界 | `DIMENSIONS`；`estimate_tokens()` 的注释写明是估算 |
| 4 未知用量保留未决占用，不记零 | `settle(known=False)` → 状态 `unknown`、占用**不释放** |
| 5 预算耗尽只保存状态并通知，不再调模型 | 拒绝时抛 `BudgetRefused`；调用方必须停止派发 |
| 6 deadline / 取消传递；不把取消当退款 | `cancel()` 阻断后续预占；已发出的不自动释放 |
| 7 等待时不持写事务；恢复先核对预占 |
  事务只在预占/结算内；`reconcile()` 只报告不清零 |

## 明确不说的话

- **估算不是保证**：`estimate_tokens()` 是 4 字符 ≈ 1 token 的本地估算，不是
  provider 的 tokenizer（PRD §5.7 明确 "adapter 给的是估算"）。硬上限模式必须
  有可验证上界，本模块不提供"绝对不超额"的承诺。
- **不防本机用户篡改**（D2 单用户 + §8.1 威胁边界）：账本在本机 SQLite 里，
  能改文件的人能改账本。它防的是并发与崩溃，不是恶意用户。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .errors import BudgetCancelled, BudgetRefused, TianshuError

#: 记账维度。**按维度分别扣**：token 够但请求次数用完了，同样拒发。
#:
#: 截止时间不在这里：它不是"花了多少"，是"什么时候必须停"，由
#: `budget_accounts.deadline_ms` 单独表达。
DIMENSIONS: tuple[str, ...] = (
    "tokens",     # 模型 token（输入 + 输出，实际结算时按 provider usage）
    "micros",     # 可计价金额（微美元）。未知价格时**不许**记 0。
    "requests",   # 请求次数（真实 attempt，含失败与重试）
    "bytes",      # 下载 / 外发字节
    "subtasks",   # 子任务数量
)

#: 账户链最大深度。防御 `parent_id` 成环或有人手写了一条 1000 层的链。
MAX_CHAIN = 16

#: 已知 / 未知状态。
PENDING = "pending"
SETTLED = "settled"
#: 部分维度已知、部分未知（例如 token 有 usage 但价格查不到）。
PARTIAL = "partial"
UNKNOWN = "unknown"
RELEASED = "released"

#: 预占口径。账户建立时定死，之后不能改——否则"上界"随时可以被降级成"估算"，
#: 那这条保证就没有意义了。
BOUND_ESTIMATE = "estimate"
BOUND_UPPER = "upper"
BOUNDS: frozenset[str] = frozenset({BOUND_ESTIMATE, BOUND_UPPER})

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_accounts (
    account_id  TEXT PRIMARY KEY,
    parent_id   TEXT,
    run_id      TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    cancelled   INTEGER NOT NULL DEFAULT 0,
    deadline_ms INTEGER,
    -- 上界模式（bound='upper'）下，结算值一旦超过该次预占，这里就置 1，
    -- 此后该账户（及其后代，因为拒绝沿链检查）拒绝一切新预占。
    -- 估算模式也会记这个标记（如实反映），但不据此拒发。
    overrun     INTEGER NOT NULL DEFAULT 0,
    -- 'estimate' 或 'upper'：账户是按哪种口径立起来的。审计与拒绝判定都用它。
    bound       TEXT NOT NULL DEFAULT 'estimate',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_limits (
    account_id  TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    limit_value INTEGER NOT NULL,
    PRIMARY KEY (account_id, dimension)
);

-- 计数器：reserved = 未结算（含未知）的占用，settled = 已结算的已知用量。
-- "还能用多少" = limit - (reserved + settled)；未知保留在 reserved 里，
-- 所以它**继续占位**，不会被读成 0。
CREATE TABLE IF NOT EXISTS budget_counters (
    account_id TEXT NOT NULL,
    dimension  TEXT NOT NULL,
    reserved   INTEGER NOT NULL DEFAULT 0,
    settled    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, dimension)
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    run_id         TEXT NOT NULL DEFAULT '',
    call_id        TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL,
    need_json      TEXT NOT NULL,
    usage_json     TEXT,
    known          INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    settled_at     INTEGER,
    UNIQUE (account_id, attempt_id)
);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_state
    ON budget_reservations(state, account_id);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def estimate_tokens(chars: int) -> int:
    """字符数 → token 的**本地估算**（4 字符 ≈ 1 token，向上取整）。

    这是估算，不是保证（PRD §5.7：连官方计数接口返回的也是估计值）。调用方
    必须把它当"预占用的估算值"，结算时用 provider 的最终 usage 替换。
    """
    if chars <= 0:
        return 0
    return (chars + 3) // 4


def estimate_need(
    *,
    input_chars: int,
    output_reserve: int = 0,
    requests: int = 1,
    margin_ratio: float = 0.25,
) -> dict[str, int]:
    """一次请求的预占需求：输入估算 + 安全余量 + 输出预留（PRD §5.7 两阶段）。

    `margin_ratio` 是给"估算偏低"留的余量。余量不是精确值，写在这里是为了
    让调用方知道**我们选择了多保守**，而不是把猜测藏在代码深处。
    """
    if not 0 <= margin_ratio <= 1:
        raise ValueError(f"margin_ratio 必须在 [0,1]：{margin_ratio!r}")
    estimate = estimate_tokens(input_chars)
    margin = int(estimate * margin_ratio)
    need = {"tokens": estimate + margin + max(0, output_reserve)}
    if requests:
        need["requests"] = requests
    return need


def upper_bound_tokens(text: str) -> int:
    """文本的 token **上界**：UTF-8 字节数。

    依据只有一个、但够硬：字节级 BPE 的每个 token 至少覆盖 1 个字节，所以
    `tokens ≤ bytes`。反过来 `chars / 4` 在 CJK 上会**低估**（一个汉字 3 字节，
    折算下来 0.75 token/字，而实际常常接近 1 token/字），拿它当上界必然被打穿。

    边界（如实写）：这是**文本**的字节上界；协议层每消息的固定开销（几个 token）
    不在里面。所以它只在"provider 不给每条消息加固定计费 token"时是严格上界；
    真被打穿时账本会记 `overrun` 并（上界模式下）停机，而不是继续假装算得准。
    """
    if not isinstance(text, str):
        raise TianshuError(f"上界只对文本定义：{type(text).__name__}")
    return len(text.encode("utf-8"))


def upper_need(
    *,
    input_bytes: int,
    output_bound: int,
    requests: int = 1,
) -> dict[str, int]:
    """一次 attempt 的**可验证**预占：输入上界 + 输出上界。

    与 `estimate_need()` 的区别不是"更保守"，是**不同性质**：这里的两个数都必须
    是上界，拿不到就调用方拒绝进入这个模式（`BudgetConfigError`），不做"少估一点
    也没事"的折中。
    """
    if isinstance(input_bytes, bool) or not isinstance(input_bytes, int) or input_bytes < 0:
        raise TianshuError(f"input_bytes 必须是非负整数：{input_bytes!r}")
    if isinstance(output_bound, bool) or not isinstance(output_bound, int):
        raise TianshuError(f"output_bound 必须是整数：{output_bound!r}")
    if output_bound <= 0:
        raise TianshuError(
            f"输出上界必须为正（没有输出上界就不叫上界）：{output_bound!r}"
        )
    need = {"tokens": input_bytes + output_bound}
    if requests:
        need["requests"] = requests
    return need


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    account_id: str
    attempt_id: str
    run_id: str
    call_id: str
    need: dict[str, int]
    state: str


@dataclass(frozen=True)
class Settlement:
    reservation_id: str
    state: str
    usage: dict[str, int] = field(default_factory=dict)
    known: bool = True
    #: True = 这次调用没有改动账本（重复结算 / 已释放）。幂等的证据。
    duplicate: bool = False
    #: 结算值超过该次预占的维度（上界模式下的"上界被突破"，估算模式下如实记录）。
    overrun: tuple[str, ...] = ()


class BudgetLedger:
    """建在事件库同一个 SQLite 文件里的预算账本。"""

    def __init__(
        self, log: Any, *, clock: Callable[[], int] | None = None
    ) -> None:
        self._log = log
        self._conn: sqlite3.Connection = log._conn
        self._clock = clock or _now_ms
        self._conn.executescript(SCHEMA)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """给旧库补列：`_ensure_column` 与其它 store 同一套做法（幂等）。"""
        for table, column, decl in (
            ("budget_accounts", "deadline_ms", "INTEGER"),
            ("budget_accounts", "label", "TEXT NOT NULL DEFAULT ''"),
            ("budget_accounts", "overrun", "INTEGER NOT NULL DEFAULT 0"),
            ("budget_accounts", "bound", "TEXT NOT NULL DEFAULT 'estimate'"),
        ):
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在

    # ------------------------------------------------------------ 事务

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """复用 EventLog 的可组合事务：预占/结算与事件同生共死。

        不在事务外做"先读再写"：SQLite 的 BEGIN IMMEDIATE 会挡住其它写者，
        读-改-写才成立。这也是并发竞争测试能成立的原因。
        """
        with self._log.transaction():
            yield

    # ------------------------------------------------------------ 账户

    def create_account(
        self,
        account_id: str,
        *,
        limits: dict[str, int] | None = None,
        parent_id: str | None = None,
        run_id: str = "",
        label: str = "",
        deadline_ms: int | None = None,
        bound: str = BOUND_ESTIMATE,
    ) -> None:
        """建账户（幂等）。`limits` 只认 `DIMENSIONS` 里的维度。

        `bound` 决定"结算超过预占"要不要拒发：`estimate` 只如实记、不停机；
        `upper` 视其为上界被突破，此后拒绝该账户链上的一切新预占。
        """
        if not account_id:
            raise TianshuError("预算账户 id 不能为空")
        if bound not in BOUNDS:
            raise TianshuError(
                f"未知的预占口径：{bound!r}（合法值：{sorted(BOUNDS)}）"
            )
        unknown = sorted(set(limits or {}) - set(DIMENSIONS))
        if unknown:
            raise TianshuError(
                f"未知的预算维度：{unknown}（合法值：{list(DIMENSIONS)}）"
            )
        if limits:
            for dimension, value in limits.items():
                if not isinstance(value, int) or isinstance(value, bool):
                    raise TianshuError(f"预算上限必须是整数：{dimension}={value!r}")
                if value < 0:
                    raise TianshuError(f"预算上限不能为负：{dimension}={value}")
        with self._tx():
            if parent_id is not None:
                parent = self._conn.execute(
                    "SELECT account_id FROM budget_accounts WHERE account_id = ?",
                    (parent_id,),
                ).fetchone()
                if parent is None:
                    raise TianshuError(f"父预算账户不存在：{parent_id}")
            existing = self._conn.execute(
                "SELECT account_id FROM budget_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if existing is not None:
                return
            self._conn.execute(
                "INSERT INTO budget_accounts (account_id, parent_id, run_id,"
                " label, cancelled, deadline_ms, bound, created_at)"
                " VALUES (?,?,?,?,0,?,?,?)",
                (account_id, parent_id, run_id, label, deadline_ms, bound,
                 self._clock()),
            )
            for dimension, value in (limits or {}).items():
                self._conn.execute(
                    "INSERT INTO budget_limits (account_id, dimension, limit_value)"
                    " VALUES (?,?,?)",
                    (account_id, dimension, value),
                )

    def account(self, account_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM budget_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "account_id": row["account_id"],
            "parent_id": row["parent_id"],
            "run_id": row["run_id"],
            "label": row["label"],
            "cancelled": bool(row["cancelled"]),
            "deadline_ms": row["deadline_ms"],
            "overrun": bool(row["overrun"]),
            "bound": row["bound"],
        }

    def _chain(self, account_id: str) -> list[str]:
        """从**根到自身**的账户链。预占要逐级扣，所以顺序无关、集合相关。"""
        chain: list[str] = []
        cursor: str | None = account_id
        while cursor is not None:
            if cursor in chain:
                raise TianshuError(f"预算账户成环：{' -> '.join(chain + [cursor])}")
            if len(chain) >= MAX_CHAIN:
                raise TianshuError(f"预算账户链超过 {MAX_CHAIN} 层：{account_id}")
            row = self._conn.execute(
                "SELECT parent_id FROM budget_accounts WHERE account_id = ?",
                (cursor,),
            ).fetchone()
            if row is None:
                raise TianshuError(f"预算账户不存在：{cursor}")
            chain.append(cursor)
            cursor = row["parent_id"]
        return chain

    def _limits(self, account_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT dimension, limit_value FROM budget_limits WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        return {row["dimension"]: row["limit_value"] for row in rows}

    def _counters(self, account_id: str) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            "SELECT dimension, reserved, settled FROM budget_counters"
            " WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        return {
            row["dimension"]: {
                "reserved": row["reserved"],
                "settled": row["settled"],
            }
            for row in rows
        }

    # ------------------------------------------------------------ 预占

    def reserve(
        self,
        account_id: str,
        *,
        attempt_id: str,
        run_id: str = "",
        call_id: str = "",
        need: dict[str, int] | None = None,
    ) -> Reservation:
        """原子预占。额度不足 / 已取消 / 已过 deadline 一律拒绝。

        `attempt_id` 是**真实 attempt 的身份**（例如 `run#turn#attempt`），
        不是"第几次调用"的模糊编号：同一个 attempt 重复预占返回同一张占位，
        不会重复扣。
        """
        need = dict(need or {})
        unknown = sorted(set(need) - set(DIMENSIONS))
        if unknown:
            raise TianshuError(
                f"未知的预算维度：{unknown}（合法值：{list(DIMENSIONS)}）"
            )
        for dimension, value in need.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TianshuError(
                    f"预占数量必须是非负整数：{dimension}={value!r}"
                )
        if not attempt_id:
            raise TianshuError("预占必须有 attempt 身份")

        with self._tx():
            existing = self._conn.execute(
                "SELECT * FROM budget_reservations"
                " WHERE account_id = ? AND attempt_id = ?",
                (account_id, attempt_id),
            ).fetchone()
            if existing is not None:
                return self._row_to_reservation(existing)

            chain = self._chain(account_id)
            now = self._clock()
            for level in chain:
                account = self.account(level)
                assert account is not None
                if account["cancelled"]:
                    raise BudgetCancelled(
                        f"预算账户已取消，拒绝新请求：{level}", account_id=level
                    )
                if account["overrun"] and account["bound"] == BOUND_UPPER:
                    # 上界被突破 = 这条保证已经不成立。此时继续发请求是把
                    # "保证"降级成"希望"，所以拒绝，并把原因报清楚。
                    raise BudgetRefused(
                        f"预算上界已被突破，拒绝新请求：{level}"
                        "（上界模式：结算值超过预占即视为保证失效）",
                        account_id=level,
                        dimension="overrun",
                    )
                if account["deadline_ms"] is not None and now > account["deadline_ms"]:
                    raise BudgetRefused(
                        f"预算账户已过 deadline：{level}",
                        account_id=level,
                        dimension="deadline",
                        need=need.get("requests", 1),
                    )
                limits = self._limits(level)
                counters = self._counters(level)
                for dimension, value in need.items():
                    limit = limits.get(dimension)
                    if limit is None:
                        # 没设上限的维度照常记账，但不设闸（PRD：分维度记账）。
                        continue
                    used = counters.get(dimension, {}).get("reserved", 0) + (
                        counters.get(dimension, {}).get("settled", 0)
                    )
                    if used + value > limit:
                        raise BudgetRefused(
                            f"预算不足：账户 {level} 的 {dimension} 已用 {used}，"
                            f"本次预占 {value}，上限 {limit}",
                            account_id=level,
                            dimension=dimension,
                            limit=limit,
                            used=used,
                            need=value,
                        )
            reservation_id = f"resv_{uuid.uuid4().hex[:16]}"
            self._conn.execute(
                "INSERT INTO budget_reservations (reservation_id, account_id,"
                " attempt_id, run_id, call_id, state, need_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    account_id,
                    attempt_id,
                    run_id,
                    call_id,
                    PENDING,
                    json.dumps(need, sort_keys=True),
                    now,
                ),
            )
            for level in chain:
                for dimension, value in need.items():
                    self._conn.execute(
                        "INSERT INTO budget_counters (account_id, dimension,"
                        " reserved, settled) VALUES (?,?,?,0)"
                        " ON CONFLICT(account_id, dimension) DO UPDATE SET"
                        " reserved = reserved + excluded.reserved",
                        (level, dimension, value),
                    )
        return self.get(reservation_id)  # type: ignore[return-value]

    def get(self, reservation_id: str) -> Reservation | None:
        row = self._conn.execute(
            "SELECT * FROM budget_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return self._row_to_reservation(row) if row else None

    def reservation_for(
        self, account_id: str, attempt_id: str
    ) -> Reservation | None:
        """按 `(账户, attempt)` 找回预占（G2 D2 子任务收尾时结算用）。

        收尾时手里只有 `(父 Run, subtask slug)`，没有 D1 时生成的随机
        `reservation_id`。用**可重算的身份**找回同一张预占，比把 id 存进
        `subtasks` 行更稳：身份错了会查不到（明确不结算），而存错的 id
        会结算到别人的预占上。
        """
        row = self._conn.execute(
            "SELECT * FROM budget_reservations"
            " WHERE account_id = ? AND attempt_id = ?",
            (account_id, attempt_id),
        ).fetchone()
        return self._row_to_reservation(row) if row else None

    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        return Reservation(
            reservation_id=row["reservation_id"],
            account_id=row["account_id"],
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            call_id=row["call_id"],
            need=json.loads(row["need_json"]),
            state=row["state"],
        )

    # ------------------------------------------------------------ 结算

    def settle(
        self,
        reservation_id: str,
        *,
        usage: dict[str, int] | None = None,
        known: bool = True,
        known_dimensions: set[str] | None = None,
    ) -> Settlement:
        """按最终 usage 结算。**幂等**：重复回执不重复扣、不重复释放。

        `known=False`（provider 断流 / 没给 usage / 可能已计费）时保留预占
        占用，状态记 `unknown`，**不按零结算**——"不知道花了多少"和"没花钱"
        是两件事（PRD §5.6 第 4 条）。

        `known_dimensions` 支持**分维度**结算：token 有 usage 但价格查不到时，
        token 维度结算、金额维度保留占用（状态 `partial`）。全有或全无会把
        "金额未知"抹成"金额为 0"，也会把"token 已知"也一起拖成未知。
        """
        usage = dict(usage or {})
        unknown = sorted(set(usage) - set(DIMENSIONS))
        if unknown:
            raise TianshuError(
                f"未知的预算维度：{unknown}（合法值：{list(DIMENSIONS)}）"
            )
        for dimension, value in usage.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TianshuError(
                    f"结算数量必须是非负整数：{dimension}={value!r}"
                )
        with self._tx():
            row = self._conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise TianshuError(f"预占不存在：{reservation_id}")
            if row["state"] != PENDING:
                # 幂等：第二次回执不改账本，只把已记录的结果再报一次。
                return Settlement(
                    reservation_id=reservation_id,
                    state=row["state"],
                    usage=json.loads(row["usage_json"] or "{}"),
                    known=bool(row["known"]),
                    duplicate=True,
                )
            need = json.loads(row["need_json"])
            chain = self._chain(row["account_id"])
            now = self._clock()
            if known_dimensions is None:
                known_dims: set[str] = set(need) if known else set()
            else:
                known_dims = set(known_dimensions)
            unresolved = sorted(set(need) - known_dims)
            for level in chain:
                # 已知维度：释放预占，写入实际用量。实际可能小于预占（正常），
                # 也可能大于预占（provider 的 usage 超过估算）——后者照实记，
                # 不夹到预占值上：账本要反映真实消费，而不是我们的猜测。
                for dimension in known_dims & set(need):
                    self._conn.execute(
                        "UPDATE budget_counters SET reserved = reserved - ?"
                        " WHERE account_id = ? AND dimension = ?",
                        (need[dimension], level, dimension),
                    )
                for dimension, value in usage.items():
                    if dimension in known_dims:
                        self._conn.execute(
                            "INSERT INTO budget_counters (account_id, dimension,"
                            " reserved, settled) VALUES (?,?,0,?)"
                            " ON CONFLICT(account_id, dimension) DO UPDATE SET"
                            " settled = settled + excluded.settled",
                            (level, dimension, value),
                        )
            # 结算值 > 预占 = 预占口径被事实打穿。**照实记**（不是把用量夹回预占，
            # 那等于改账），并把账户链标成 overrun：上界模式据此拒绝后续请求，
            # 估算模式只如实展示（估算本来就会偏，拿它当上界才是错的）。
            overrun_dims = tuple(
                dimension
                for dimension, value in sorted(usage.items())
                if dimension in known_dims and value > need.get(dimension, 0)
            )
            if overrun_dims:
                for level in chain:
                    self._conn.execute(
                        "UPDATE budget_accounts SET overrun = 1 WHERE account_id = ?",
                        (level,),
                    )
            # 未结清的维度占用**不释放**：之后由 `reconcile()` 报告，人工/恢复
            # 流程核对执行证据，不由进程退出猜测为零。
            if not unresolved:
                state = SETTLED
            elif known_dims & set(need):
                state = PARTIAL
            else:
                state = UNKNOWN
            self._conn.execute(
                "UPDATE budget_reservations SET state = ?, usage_json = ?,"
                " known = ?, settled_at = ? WHERE reservation_id = ?",
                (
                    state,
                    json.dumps(usage, sort_keys=True),
                    1 if known else 0,
                    now,
                    reservation_id,
                ),
            )
        return Settlement(
            reservation_id=reservation_id,
            state=state,
            usage=usage,
            known=known,
            duplicate=False,
            overrun=overrun_dims,
        )

    def abandon_unsent(self, reservation_id: str, *, reason: str = "") -> bool:
        """释放一次**确认没有发出**的预占（例如请求在发送前被取消）。

        只对 `pending` 生效；对已结算 / 未知的预占返回 False——取消不是退款
        （PRD §5.6 第 6 条）。
        """
        with self._tx():
            row = self._conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise TianshuError(f"预占不存在：{reservation_id}")
            if row["state"] != PENDING:
                return False
            need = json.loads(row["need_json"])
            for level in self._chain(row["account_id"]):
                for dimension, value in need.items():
                    self._conn.execute(
                        "UPDATE budget_counters SET reserved = reserved - ?"
                        " WHERE account_id = ? AND dimension = ?",
                        (value, level, dimension),
                    )
            self._conn.execute(
                "UPDATE budget_reservations SET state = ?, settled_at = ?,"
                " usage_json = ? WHERE reservation_id = ?",
                (RELEASED, self._clock(), json.dumps({"reason": reason}),
                 reservation_id),
            )
        return True

    # ------------------------------------------------------------ 取消

    def cancel(self, account_id: str) -> int:
        """取消账户及其所有后代。返回被取消的账户数。

        取消只阻断**后续**预占；已发出的请求保持原状（未知就不当零），
        不把取消当退款。
        """
        with self._tx():
            rows = self._conn.execute(
                """
                WITH RECURSIVE subtree(account_id) AS (
                    SELECT account_id FROM budget_accounts WHERE account_id = ?
                    UNION ALL
                    SELECT a.account_id FROM budget_accounts a
                    JOIN subtree s ON a.parent_id = s.account_id
                )
                SELECT account_id FROM subtree
                """,
                (account_id,),
            ).fetchall()
            ids = [row["account_id"] for row in rows]
            for item in ids:
                self._conn.execute(
                    "UPDATE budget_accounts SET cancelled = 1 WHERE account_id = ?",
                    (item,),
                )
        return len(ids)

    def is_cancelled(self, account_id: str) -> bool:
        account = self.account(account_id)
        return bool(account and account["cancelled"])

    # ------------------------------------------------------------ 报告

    def usage(self, account_id: str) -> dict[str, Any]:
        """当前账目。已知与未知**分开报**，不合成一个数字。

        父账户的计数器**本身**就含子账户的花费（预占沿链逐级扣），所以这里是
        子树聚合值，不需要也不允许再对后代求和——再求一次就把同一笔花两次
        （PRD §5.6 第 2 条）。`unknown_reservations` 统计整棵子树。
        """
        counters = self._counters(account_id)
        settled = {dim: value["settled"] for dim, value in counters.items()}
        reserved = {dim: value["reserved"] for dim, value in counters.items()}
        unknown = self._conn.execute(
            """
            WITH RECURSIVE subtree(account_id) AS (
                SELECT account_id FROM budget_accounts WHERE account_id = ?
                UNION ALL
                SELECT a.account_id FROM budget_accounts a
                JOIN subtree s ON a.parent_id = s.account_id
            )
            SELECT COUNT(*) AS n FROM budget_reservations r
            WHERE r.state IN (?,?) AND r.account_id IN (SELECT account_id FROM subtree)
            """,
            (account_id, UNKNOWN, PARTIAL),
        ).fetchone()["n"]
        account = self.account(account_id)
        return {
            "account_id": account_id,
            "limits": self._limits(account_id),
            "settled": settled,
            "reserved": reserved,
            "unknown_reservations": unknown,
            "cancelled": self.is_cancelled(account_id),
            # 上界是否被打穿、这个账户当初按哪种口径建立：两个都要能被读到，
            # 否则"上界保证"就只活在代码注释里。
            "overrun": bool(account and account["overrun"]),
            "bound": (account or {}).get("bound", ""),
        }

    def pending(self, account_id: str | None = None) -> list[Reservation]:
        if account_id is None:
            rows = self._conn.execute(
                "SELECT * FROM budget_reservations WHERE state = ?"
                " ORDER BY created_at",
                (PENDING,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM budget_reservations WHERE state = ?"
                " AND account_id = ? ORDER BY created_at",
                (PENDING, account_id),
            ).fetchall()
        return [self._row_to_reservation(row) for row in rows]

    def reconcile(self, account_id: str | None = None) -> list[dict[str, Any]]:
        """恢复时的核对材料：列出未结清的预占，**不清零、不猜测**。

        PRD §5.6 第 7 条："恢复先核对未决预占与执行证据，不按进程退出猜测
        消费为零。" 这里只给材料；判定留给调用方（有无执行证据）。
        """
        if account_id is None:
            rows = self._conn.execute(
                "SELECT * FROM budget_reservations WHERE state IN (?,?,?)"
                " ORDER BY created_at",
                (PENDING, UNKNOWN, PARTIAL),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM budget_reservations WHERE state IN (?,?,?)"
                " AND account_id = ? ORDER BY created_at",
                (PENDING, UNKNOWN, PARTIAL, account_id),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({
                "reservation_id": row["reservation_id"],
                "account_id": row["account_id"],
                "attempt_id": row["attempt_id"],
                "run_id": row["run_id"],
                "call_id": row["call_id"],
                "state": row["state"],
                "need": json.loads(row["need_json"]),
                "usage": json.loads(row["usage_json"] or "{}"),
                "created_at": row["created_at"],
            })
        return out


__all__ = [
    "BOUNDS",
    "BOUND_ESTIMATE",
    "BOUND_UPPER",
    "BudgetLedger",
    "DIMENSIONS",
    "MAX_CHAIN",
    "PARTIAL",
    "PENDING",
    "RELEASED",
    "Reservation",
    "SETTLED",
    "Settlement",
    "UNKNOWN",
    "estimate_need",
    "estimate_tokens",
    "upper_bound_tokens",
    "upper_need",
]
