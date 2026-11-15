"""确定性最小调度器（W5，路线图「调度/定时任务」）。

职责边界（SPEC：docs/SPEC-W5-SCHEDULER-2026-11-15.md）：

- **只负责「到点创建任务并入队 Run」**。它自己不执行任何工具、不产出任何
  副作用——Run 被驱动后的每个动作照旧走派发层 PolicyEngine / 人工审批 /
  执行侧重判 / Receipt 的既有链（红线 4：授权只在执行侧判定，调度器不是
  授权输入，只是又一个任务创建者，与 CLI/RPC 的调用者同地位）。
- **触发路径与 `task.create` + `run.start` 完全同路径**：`create_task` →
  `prepare_run` → `start_run` → `enqueue`，事件流出现的是既有封闭集
  （`task.created`、`run.start`…），v1 不新增事件族。
- **幂等不靠事件层 `idem_key`**（核对结论：`events.idem_key` 无 UNIQUE、
  `EventLog.append` 不去重、task 创建路径不使用它）。v1 的触发幂等 = 
  「记账与建任务同事务」+「单写者」（daemon 锁保证同数据根单实例、tick
  只被驱动线程调用）。这就是 schedules 表必须建在事件库同一条连接上的
  结构性理由：一旦挪到第二个数据库文件，「建任务」与「记账」就回到两个
  事务，出现真正的双发窗口——届时必须引入显式 idem_key 去重。
- **追赶策略**：到期未触发的 schedule（daemon 停机期间落下的）在下一个
  tick **补发一次并从当前时间重排**——不按落下的 interval 数补跑 N 次
  （一次性制造 N 个 Run 既烧钱又不可预期）。因此不需要单独的启动钩子：
  daemon 启动后的第一个 tick 天然就是追赶。
- **cron 表达式 v1 不做**（要么引第三方依赖、要么自写解析器，都不该由
  v1 背正确性负担）；RPC / UI 可观测性同样留未决，管理面只走 CLI。

schedules 表是「未来的意图」而非「已发生的事实」：它不是事件投影、不参与
回放，生命周期 v1 不走事件流（v2 需要 schedule.created/fired/missed 等事件
的评估见 docs/CONTRACT-W5-SCHEDULER-2026-11-15.md）。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from .errors import TianshuError
from .events import EventLog
from .execpolicy import ExecPolicy
from .workspace import parse_bindings

#: 触发失败原因在表里的截断长度——这列是给人看的（`schedule list`），不是
#: 机器可读事件；完整 traceback 在 daemon 日志里。
_MAX_ERROR_TEXT = 500

#: interval 下限（秒）。调度 tick 的节拍是 daemon 的 `driver_interval_s`
#:（默认 0.05s），不设下限会让一个手滑的 `--interval-seconds 0` 变成
#: 每个 tick 发一个任务的 Run 风暴。
_MIN_INTERVAL_SECONDS = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id      TEXT PRIMARY KEY,
    goal             TEXT NOT NULL,
    workspaces_json  TEXT NOT NULL,
    exec_policy_json TEXT,
    interval_seconds INTEGER NOT NULL,
    next_fire_at     INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL,
    last_fired_at    INTEGER,
    last_error       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_fire_at);
"""


class Scheduler:
    """interval 调度器。schedules 表建在事件库同一条连接上（先例：
    `budget.py` / `dispatcher.py` 的模块自建表），因此触发时的「建任务 +
    记账」能与事件同事务提交。

    管理面（create/list/enable/disable/remove）只需要 EventLog；触发面
    （tick）需要同一 Stack 的 tasks/dispatcher/claims——daemon 驱动线程
    装配齐全后再 tick，缺装配时 tick 报错而不是静默不触发。
    """

    def __init__(
        self,
        log: EventLog,
        *,
        tasks: Any = None,
        dispatcher: Any = None,
        claims: Any = None,
        clock: Callable[[], int] | None = None,
        limits: Any = None,
        note: Callable[[str], None] | None = None,
    ) -> None:
        self._log = log
        self._conn = log.conn
        self._tasks = tasks
        self._dispatcher = dispatcher
        self._claims = claims
        self._clock: Callable[[], int] = clock or (
            lambda: int(time.time() * 1000)
        )
        from .loop import Limits

        self._limits = limits if limits is not None else Limits()
        self._note = note
        # 模块自建表（budget.py:84 / dispatcher.py SCHEMA 先例）：建在事件库
        # 同一个 SQLite 文件、同一条连接上，幂等。
        self._conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- 管理面

    def create(
        self,
        goal: str,
        workspaces: list[str | dict[str, Any]] | None,
        *,
        interval_seconds: int,
        exec_policy: ExecPolicy | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """创建一条 schedule。**创建时点做全部预校验**——把非法 schedule
        挡在这里，而不是让它在每次触发时永久失败（触发失败只能跳过并记
        `last_error`，没有任何补偿机制能让坏配置自己变好）。
        """
        if not isinstance(goal, str) or not goal.strip():
            raise TianshuError("goal 不能为空")
        if (
            not isinstance(interval_seconds, int)
            or isinstance(interval_seconds, bool)
            or interval_seconds < _MIN_INTERVAL_SECONDS
        ):
            raise TianshuError(
                f"interval_seconds 必须是 >= {_MIN_INTERVAL_SECONDS} 的整数"
            )
        # 绑定/策略在这里解析一遍：非法输入当场报错（WorkspaceViolation /
        # ValueError 不包装成静默降级）。规范化后的形状落库，触发时再走
        # `create_task` 的正式入口（同一条校验路径跑第二次是纵深，不是冗余）。
        try:
            parsed = [b.to_dict() for b in parse_bindings(list(workspaces or []))]
        except TypeError as exc:
            # WorkspaceBinding.of 对"缺字段的 dict"抛 TypeError——那是输入形状
            # 错误，在库边界统一成 TianshuError（CLI/调用方按业务错误处理）。
            raise TianshuError(f"workspaces 形状不合法：{exc}") from exc
        policy_dict = exec_policy.to_dict() if exec_policy is not None else None
        now_ms = int(now) if now is not None else self._clock()
        schedule_id = f"sched_{uuid.uuid4().hex[:12]}"
        with self._log.transaction():
            self._conn.execute(
                """
                INSERT INTO schedules (
                    schedule_id, goal, workspaces_json, exec_policy_json,
                    interval_seconds, next_fire_at, enabled, created_at,
                    last_fired_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, '')
                """,
                (
                    schedule_id,
                    goal.strip(),
                    json.dumps(parsed, ensure_ascii=False),
                    (
                        json.dumps(policy_dict, ensure_ascii=False)
                        if policy_dict is not None
                        else None
                    ),
                    interval_seconds,
                    now_ms + interval_seconds * 1000,
                    now_ms,
                ),
            )
        created = self.get(schedule_id)
        assert created is not None  # 刚插入的行必然存在
        return created

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        return _row_to_view(row) if row is not None else None

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM schedules ORDER BY created_at, schedule_id"
        ).fetchall()
        return [_row_to_view(row) for row in rows]

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        """启停一条 schedule。disable 保留配置行（不删），re-enable 后从
        当前的 `next_fire_at` 继续判定——已停用期间落下的到期会在重新启用
        后的第一个 tick 追赶补发一次（与 §追赶策略同一语义）。
        """
        with self._log.transaction():
            cur = self._conn.execute(
                "UPDATE schedules SET enabled = ? WHERE schedule_id = ?",
                (1 if enabled else 0, schedule_id),
            )
            if cur.rowcount == 0:
                raise TianshuError(f"没有这个 schedule：{schedule_id}")
        found = self.get(schedule_id)
        assert found is not None  # 刚更新过的行必然存在
        return found

    def remove(self, schedule_id: str) -> None:
        """删除一条 schedule。schedules 不是事件投影，DELETE 在这里合法
        （append-only 纪律约束的是事件表，不是这张意图表）。"""
        with self._log.transaction():
            cur = self._conn.execute(
                "DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,)
            )
            if cur.rowcount == 0:
                raise TianshuError(f"没有这个 schedule：{schedule_id}")

    # ---------------------------------------------------------------- 触发面

    def tick(self) -> list[str]:
        """触发所有到期的 enabled schedule。返回本次触发产生的 run_id 列表。

        每条 schedule 独立 try/except：单条失败不拖垮其他 schedule、不把
        异常抛回驱动循环。追赶语义见模块注释：补发一次、从当前时间重排。
        """
        now = self._clock()
        fired: list[str] = []
        for row in self._due_rows(now):
            try:
                fired.append(self._fire(row, now))
            except Exception as exc:  # noqa: BLE001 - 单条失败不该带走整批
                self._record_error(row, now, exc)
        return fired

    def _due_rows(self, now: int) -> list[Any]:
        return self._conn.execute(
            """
            SELECT * FROM schedules
            WHERE enabled = 1 AND next_fire_at <= ?
            ORDER BY next_fire_at, schedule_id
            """,
            (now,),
        ).fetchall()

    def _fire(self, row: Any, now: int) -> str:
        """触发一条 schedule：建任务 + 入队 Run + 记账，**同一个事务**。

        事务回滚语义就是崩溃窗口的答案：BEGIN 之后任何一步失败（或进程被
        kill -9）都会整体回滚——没有任务行、没有事件、schedule 仍到期，
        下个 tick 重试属于 `none_applied` 的安全重试（红线 5）。不存在
        「任务已建但记账丢失」的中间态，所以不需要事件层 idem_key 去重。
        """
        if self._tasks is None or self._dispatcher is None or self._claims is None:
            raise TianshuError(
                "调度器没有装配 tasks/dispatcher/claims，不能触发"
                "（daemon 驱动线程才会装配触发面）"
            )
        from .control import _exposed_tool_ids
        from .run_policy import EFFECTS

        workspaces = json.loads(row["workspaces_json"])
        raw_policy = row["exec_policy_json"]
        exec_policy = (
            ExecPolicy.from_dict(json.loads(raw_policy))
            if raw_policy
            else None
        )
        with self._log.transaction():
            task = self._tasks.create_task(
                row["goal"], workspaces, exec_policy=exec_policy
            )
            # 与 control._run_start 逐字同路径（只入队不驱动）：Run 由同一
            # 驱动循环的 worker.run_once() 认领，租约在 drive 路径获取。
            self._dispatcher.prepare_run(task.task_id, EFFECTS)
            payload = {
                "task": task.goal,
                "out": "",
                "limits": {
                    "max_turns": self._limits.max_turns,
                    "max_total_tokens": self._limits.max_total_tokens,
                },
                "tools": _exposed_tool_ids(EFFECTS),
            }
            run = self._tasks.start_run(
                task.task_id, payload=payload, execution_policy=EFFECTS
            )
            self._claims.enqueue(run.run_id)
            # 记账与效果同事务（承重墙，NC2 拆它必须红）：last_fired_at 落
            # 定 = 这次到期已被消费；next_fire_at 从 now 重排 = 追赶只补一次。
            self._conn.execute(
                """
                UPDATE schedules
                SET last_fired_at = ?, next_fire_at = ?, last_error = ''
                WHERE schedule_id = ?
                """,
                (now, now + int(row["interval_seconds"]) * 1000,
                 row["schedule_id"]),
            )
        return run.run_id

    def _record_error(self, row: Any, now: int, exc: Exception) -> None:
        """触发失败：记 `last_error` 并把 next_fire_at 从 now 重排（跳过本次，
        下个 interval 再试）。回滚过的触发什么都没发生，重试是安全的；但
        确定性坏 schedule 不能每个 tick（默认 50ms）刷重试风暴，所以仍然
        前移 next_fire_at。失败细节：daemon 日志（note）+ `schedule list`
        的 last_error 列；v1 没有 schedule.missed 事件（契约分析文档论证）。
        """
        reason = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_TEXT]
        next_fire = now + int(row["interval_seconds"]) * 1000
        try:
            with self._log.transaction():
                self._conn.execute(
                    """
                    UPDATE schedules
                    SET last_error = ?, next_fire_at = ?
                    WHERE schedule_id = ?
                    """,
                    (reason, next_fire, row["schedule_id"]),
                )
        except Exception as record_exc:  # noqa: BLE001 - 不掩盖原始异常
            if self._note is not None:
                self._note(
                    f"调度器记录触发失败时出错（schedule={row['schedule_id']}）："
                    f"{type(record_exc).__name__}: {record_exc}"
                )
        if self._note is not None:
            self._note(
                f"调度器触发失败（schedule={row['schedule_id']}，"
                f"下次 {next_fire}）：{reason}"
            )


def _row_to_view(row: Any) -> dict[str, Any]:
    """行 → CLI 可见的 dict。JSON 列原样解析；解析失败如实抛错（历史库
    只可能是本模块写入的，坏 JSON 说明库被外部改过，不洗白）。"""
    return {
        "schedule_id": row["schedule_id"],
        "goal": row["goal"],
        "workspaces": json.loads(row["workspaces_json"]),
        "exec_policy": (
            json.loads(row["exec_policy_json"])
            if row["exec_policy_json"]
            else None
        ),
        "interval_seconds": int(row["interval_seconds"]),
        "next_fire_at": int(row["next_fire_at"]),
        "enabled": bool(row["enabled"]),
        "created_at": int(row["created_at"]),
        "last_fired_at": (
            int(row["last_fired_at"]) if row["last_fired_at"] is not None else None
        ),
        "last_error": row["last_error"],
    }
