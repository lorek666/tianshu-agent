"""Local task triggers: intervals, calendar times, file arrivals and IPC events.

A trigger only queues a normal task/Run. The scheduler never runs tools or
supplies approval. Recording the consumed trigger and queuing the Run are
one event-store transaction, including the persisted directory baseline.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .events import EventLog
from .execpolicy import ExecPolicy
from .schedule_triggers import FILE_POLL_MS, calendar_next, scan_directory, validate_trigger
from .workspace import parse_bindings

#: 触发失败原因在表里的截断长度——这列是给人看的（`schedule list`），不是
#: 机器可读事件；完整 traceback 在 daemon 日志里。
_MAX_ERROR_TEXT = 500

#: interval 下限（秒）。调度 tick 的节拍是 daemon 的 `driver_interval_s`
#:（默认 0.05s），不设下限会让一个手滑的 `--interval-seconds 0` 变成
#: 每个 tick 发一个任务的 Run 风暴。
_MIN_INTERVAL_SECONDS = 1
_MAX_SQLITE_INT = (1 << 63) - 1

# New kinds use their own table so a downgrade cannot interpret one as an
# interval schedule or consume its baseline. Legacy rows need no migration.
_TABLES = ("schedules", "schedules_triggers_v1")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_COMMON_COLUMNS = """
    schedule_id      TEXT PRIMARY KEY,
    goal             TEXT NOT NULL,
    workspaces_json  TEXT NOT NULL,
    exec_policy_json TEXT,
    interval_seconds INTEGER NOT NULL,
    next_fire_at     {next_fire_type},
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL,
    last_fired_at    INTEGER,
    last_error       TEXT NOT NULL DEFAULT ''
"""
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schedules ({_COMMON_COLUMNS.format(next_fire_type="INTEGER NOT NULL")});
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_fire_at);
CREATE TABLE IF NOT EXISTS schedules_triggers_v1 (
    {_COMMON_COLUMNS.format(next_fire_type="INTEGER")},
    trigger_json TEXT NOT NULL,
    trigger_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_triggers_due
ON schedules_triggers_v1(enabled, next_fire_at);
CREATE TABLE IF NOT EXISTS schedule_event_receipts_v1 (
    schedule_id TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (schedule_id, event_digest)
);
"""


class ScheduleMissing(TianshuError):
    """The event target does not exist."""


class ScheduleInactive(TianshuError):
    """The schedule is disabled or has another trigger kind."""


class Scheduler:
    """任务调度器。schedules 表建在事件库同一条连接上（先例：
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
        interval_seconds: int | None = None,
        trigger: Any = None,
        exec_policy: ExecPolicy | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """创建一条 schedule。**创建时点做全部预校验**——把非法 schedule
        挡在这里，而不是让它在每次触发时永久失败（触发失败只能跳过并记
        `last_error`，没有任何补偿机制能让坏配置自己变好）。
        """
        if not isinstance(goal, str) or not goal.strip():
            raise TianshuError("goal 不能为空")
        if trigger is None:
            if (not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool)
                    or interval_seconds < _MIN_INTERVAL_SECONDS):
                raise TianshuError(f"interval_seconds 必须是 >= {_MIN_INTERVAL_SECONDS} 的整数")
        elif interval_seconds is not None:
            raise TianshuError("interval_seconds 和 trigger 只能提供一个")
        # 绑定/策略在这里解析一遍：非法输入当场报错（WorkspaceViolation /
        # ValueError 不包装成静默降级）。规范化后的形状落库，触发时再走
        # `create_task` 的正式入口（同一条校验路径跑第二次是纵深，不是冗余）。
        try:
            bindings = parse_bindings(list(workspaces or []))
            parsed = [b.to_dict() for b in bindings]
        except TypeError as exc:
            # WorkspaceBinding.of 对"缺字段的 dict"抛 TypeError——那是输入形状
            # 错误，在库边界统一成 TianshuError（CLI/调用方按业务错误处理）。
            raise TianshuError(f"workspaces 形状不合法：{exc}") from exc
        policy_dict = exec_policy.to_dict() if exec_policy is not None else None
        now_ms = int(now) if now is not None else self._clock()
        trigger_config, trigger_state = (
            (None, None) if trigger is None else validate_trigger(trigger, bindings)
        )
        stored_interval = interval_seconds if interval_seconds is not None else 0
        next_fire = _next_fire(stored_interval, trigger_config or {"kind": "interval"}, now_ms)
        if next_fire is not None and next_fire > _MAX_SQLITE_INT:
            raise TianshuError("触发间隔超出可存储范围")
        workspaces_json = json.dumps(parsed, ensure_ascii=False)
        policy_json = (
            json.dumps(policy_dict, ensure_ascii=False) if policy_dict is not None else None
        )
        schedule_id = f"sched_{uuid.uuid4().hex[:12]}"
        with self._log.transaction():
            common = (schedule_id, goal.strip(), workspaces_json, policy_json,
                      stored_interval, next_fire, now_ms)
            if trigger_config is None:
                self._conn.execute(
                    """INSERT INTO schedules
                    (schedule_id, goal, workspaces_json, exec_policy_json, interval_seconds,
                    next_fire_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""", common,
                )
            else:
                self._conn.execute(
                    """INSERT INTO schedules_triggers_v1
                    (schedule_id, goal, workspaces_json, exec_policy_json, interval_seconds,
                    next_fire_at, created_at, trigger_json, trigger_state)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*common, json.dumps(trigger_config), json.dumps(trigger_state)),
                )
        created = self.get(schedule_id)
        assert created is not None  # 刚插入的行必然存在
        return created

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        for table in _TABLES:
            row = self._conn.execute(
                f"SELECT * FROM {table} WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
            if row is not None:
                return _row_to_view(row)
        return None

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            (_row_to_view(row) for table in _TABLES
             for row in self._conn.execute(f"SELECT * FROM {table}")),
            key=lambda row: (row["created_at"], row["schedule_id"]),
        )

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        """启停一条 schedule。disable 保留配置行（不删），re-enable 后从
        当前的 `next_fire_at` 继续判定——已停用期间落下的到期会在重新启用
        后的第一个 tick 追赶补发一次（与 §追赶策略同一语义）。
        """
        with self._log.transaction():
            for table in _TABLES:
                cur = self._conn.execute(
                    f"UPDATE {table} SET enabled = ? WHERE schedule_id = ?",
                    (1 if enabled else 0, schedule_id),
                )
                if cur.rowcount:
                    break
            else:
                raise TianshuError(f"没有这个 schedule：{schedule_id}")
        found = self.get(schedule_id)
        assert found is not None  # 刚更新过的行必然存在
        return found

    def remove(self, schedule_id: str) -> None:
        """删除一条 schedule。schedules 不是事件投影，DELETE 在这里合法
        （append-only 纪律约束的是事件表，不是这张意图表）。"""
        with self._log.transaction():
            for table in _TABLES:
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE schedule_id = ?", (schedule_id,)
                )
                if cur.rowcount:
                    if table == _TABLES[1]:
                        self._conn.execute(
                            "DELETE FROM schedule_event_receipts_v1 WHERE schedule_id = ?",
                            (schedule_id,),
                        )
                    return
            raise TianshuError(f"没有这个 schedule：{schedule_id}")

    # ---------------------------------------------------------------- 触发面

    def signal(self, schedule_id: str, event_id: str) -> dict[str, Any]:
        """Queue a configured task for a signal from the authenticated control plane.

        The event ID only deduplicates traffic. It never supplies task input,
        paths, capabilities or approval. A repeated ID returns the original Run.
        """
        if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
            raise ValueError(
                "event_id 必须以字母或数字开头，且只含 1 至 128 个 ASCII 字母、数字或 ._:-"
            )
        digest = hashlib.sha256(event_id.encode("ascii")).hexdigest()
        with self._log.transaction():
            row = self._conn.execute(
                "SELECT * FROM schedules_triggers_v1 WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                legacy = self._conn.execute(
                    "SELECT 1 FROM schedules WHERE schedule_id = ?", (schedule_id,)
                ).fetchone()
                if legacy is not None:
                    raise ScheduleInactive("此定时任务不接受事件触发")
                raise ScheduleMissing("找不到指定的定时任务")
            if not row["enabled"] or _trigger_of(row)["kind"] != "event":
                raise ScheduleInactive("此定时任务已停用或不接受事件触发")
            receipt = self._conn.execute(
                "SELECT run_id FROM schedule_event_receipts_v1"
                " WHERE schedule_id = ? AND event_digest = ?",
                (schedule_id, digest),
            ).fetchone()
            if receipt is not None:
                return {"accepted": False, "run_id": receipt["run_id"]}
            now = self._clock()
            run_id = self._fire(row)
            self._conn.execute(
                "INSERT INTO schedule_event_receipts_v1 VALUES (?, ?, ?, ?)",
                (schedule_id, digest, run_id, now),
            )
            self._conn.execute(
                "UPDATE schedules_triggers_v1 SET last_fired_at = ?, last_error = ''"
                " WHERE schedule_id = ?",
                (now, schedule_id),
            )
            return {"accepted": True, "run_id": run_id}

    def tick(self) -> list[str]:
        """触发所有到期的 enabled schedule。返回本次触发产生的 run_id 列表。

        每条 schedule 独立 try/except：单条失败不拖垮其他 schedule、不把
        异常抛回驱动循环。追赶语义见模块注释：补发一次、从当前时间重排。
        """
        now = self._clock()
        fired: list[str] = []
        for table, row in self._due_rows(now):
            try:
                run_id = self._process(table, row, now)
                if run_id is not None:
                    fired.append(run_id)
            except Exception as exc:  # noqa: BLE001 - 单条失败不该带走整批
                self._record_error(table, row, now, exc)
        return fired

    def _due_rows(self, now: int) -> list[tuple[str, Any]]:
        return sorted(
            ((table, row) for table in _TABLES for row in self._conn.execute(
                f"SELECT * FROM {table} WHERE enabled = 1 AND next_fire_at <= ?", (now,)
            )),
            key=lambda pair: (pair[1]["next_fire_at"], pair[1]["schedule_id"]),
        )

    def _process(self, table: str, row: Any, now: int) -> str | None:
        trigger = _trigger_of(row)
        next_fire = _next_fire(row["interval_seconds"], trigger, now)
        state_json = row["trigger_state"] if table == _TABLES[1] else None
        should_fire = True
        if trigger["kind"] == "file_arrival":
            saved = json.loads(state_json)
            observed = scan_directory(Path(trigger["directory"]), expected=saved["identity"])
            should_fire = bool(set(observed["files"]) - set(saved["files"]))
            state_json = json.dumps(observed)
        with self._log.transaction():
            # CLI/RPC may disable or delete the schedule while a directory scan
            # is in progress. Check the observed row before queueing anything.
            state_sql, state_params = (
                (", trigger_state = ?", (state_json,)) if table == _TABLES[1] else ("", ())
            )
            consumed = self._conn.execute(
                f"""UPDATE {table}
                SET next_fire_at = ?, last_error = ''{state_sql}
                WHERE schedule_id = ? AND enabled = 1 AND next_fire_at = ?""",
                (next_fire, *state_params, row["schedule_id"], row["next_fire_at"]),
            )
            if not consumed.rowcount or not should_fire:
                return None
            run_id = self._fire(row)
            self._conn.execute(
                f"UPDATE {table} SET last_fired_at = ? WHERE schedule_id = ?",
                (now, row["schedule_id"]),
            )
            return run_id

    def _fire(self, row: Any) -> str:
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
        task = self._tasks.create_task(
            row["goal"], workspaces, exec_policy=exec_policy
        )
        # 与 control._run_start 逐字同路径（只入队不驱动）：Run 由同一
        # 驱动循环的 worker.run_once() 认领，租约在 drive 路径获取。
        self._dispatcher.prepare_run(task.task_id, EFFECTS, discover_mcp=False)
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
        return run.run_id

    def _record_error(self, table: str, row: Any, now: int, exc: Exception) -> None:
        """触发失败：记 `last_error` 并把 next_fire_at 从 now 重排（跳过本次，
        下个 interval 再试）。回滚过的触发什么都没发生，重试是安全的；但
        确定性坏 schedule 不能每个 tick（默认 50ms）刷重试风暴，所以仍然
        前移 next_fire_at。失败细节：daemon 日志（note）+ `schedule list`
        的 last_error 列；v1 没有 schedule.missed 事件（契约分析文档论证）。
        """
        reason = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_TEXT]
        try:
            next_fire = _next_fire(row["interval_seconds"], _trigger_of(row), now)
        except (TianshuError, TypeError, ValueError, KeyError):
            # Persist the error even if timezone data was removed or a row is corrupt.
            next_fire = now + 60_000
        try:
            with self._log.transaction():
                self._conn.execute(
                    f"""
                    UPDATE {table}
                    SET last_error = ?, next_fire_at = ?
                    WHERE schedule_id = ? AND enabled = 1 AND next_fire_at = ?
                    """,
                    (reason, next_fire, row["schedule_id"], row["next_fire_at"]),
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


def _trigger_of(row: Any) -> dict[str, Any]:
    if "trigger_json" not in row.keys():
        return {"kind": "interval"}
    trigger = json.loads(row["trigger_json"])
    if not isinstance(trigger, dict) or trigger.get("kind") not in (
        "calendar", "file_arrival", "event",
    ):
        raise TianshuError("未知持久化触发类型")
    return trigger


def _next_fire(interval: int, trigger: dict[str, Any], now: int) -> int | None:
    if trigger["kind"] == "interval":
        return now + interval * 1000
    if trigger["kind"] == "calendar":
        return calendar_next(trigger, now)
    if trigger["kind"] == "file_arrival":
        return now + FILE_POLL_MS
    if trigger["kind"] == "event":
        return None
    raise TianshuError("未知持久化触发类型")


def _row_to_view(row: Any) -> dict[str, Any]:
    """行 → CLI 可见的 dict。JSON 列原样解析；解析失败如实抛错（历史库
    只可能是本模块写入的，坏 JSON 说明库被外部改过，不洗白）。"""
    trigger = _trigger_of(row)
    return {
        "schedule_id": row["schedule_id"],
        "trigger": trigger,
        "goal": row["goal"],
        "workspaces": json.loads(row["workspaces_json"]),
        "exec_policy": (
            json.loads(row["exec_policy_json"])
            if row["exec_policy_json"]
            else None
        ),
        "interval_seconds": int(row["interval_seconds"]) if trigger["kind"] == "interval" else None,
        "next_fire_at": int(row["next_fire_at"]) if row["next_fire_at"] is not None else None,
        "enabled": bool(row["enabled"]),
        "created_at": int(row["created_at"]),
        "last_fired_at": (
            int(row["last_fired_at"]) if row["last_fired_at"] is not None else None
        ),
        "last_error": row["last_error"],
    }
