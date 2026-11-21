"""Run 持久游标、模型回合提交、终态结算与事件投影重建（内部 Mixin，由 `TaskStore` 继承）。

所有游标写入、模型回合落账、终态 `run.end`/`run.finish` 原子提交与投影重建
均通过 `TaskStore` 与事件日志共享同一 SQLite 事务完成。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from ._tasks_types import (
    CANCEL_REASON,
    CONTROL_REQUESTS,
    EFFECT_STATES,
    RUN_CONTROL,
    RUN_CURSOR_SAVED,
    RUN_EFFECT,
    RUN_END,
    RUN_FENCED_EVENT,
    RUN_FINISH,
    RUN_MODEL_TURN,
    RUN_OUTCOMES,
    RUN_START,
    RUN_STATE,
    STOPPED_STATES,
    TASK_CREATED,
    TASK_REVISED,
    Run,
    RunCursor,
    _now,
)
from .errors import RunFenced, TianshuError
from .events import EventLog, canonical_bytes, sha256_hex
from .jobs import JobClaim, JobStore
from .leases import LeaseStore
from .workspace import parse_bindings

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行期不导入
    from .permissions import ApprovalStore


class _TaskCursorRebuildMixin:
    """游标读写、模型回合提交、Run 终态收尾与事件投影重建，供 `TaskStore` 组合继承。"""

    _log: EventLog
    _conn: sqlite3.Connection
    _leases: LeaseStore | None
    _claims: JobStore | None
    _approvals: ApprovalStore | None

    # ---- 游标与模型结果（M2 · Task 5 的存储层） ----

    def _require_claims(self) -> JobStore:
        if self._claims is None:
            raise TianshuError(
                "未配置 JobStore：cursor/模型结果必须绑定有效 claim，"
                "不能用裸 run_id 写入"
            )
        return self._claims

    def _assert_writer(self, run_id: str, claim: JobClaim) -> Run:
        """写这个 Run 状态的人，必须同时是：还没被 fence 的 Run 的当前 owner。"""
        if not isinstance(claim, JobClaim):
            raise TianshuError("必须传 JobClaim；不能用裸 run_id 写 Run 的状态")
        if claim.run_id != run_id:
            raise TianshuError(
                f"claim 与 run 不匹配：claim.run_id={claim.run_id} run_id={run_id}"
            )
        self._require_claims().assert_claim(claim)
        return self.assert_current(run_id)

    def save_cursor(self, run_id: str, cursor: RunCursor, claim: JobClaim) -> RunCursor:
        """保存游标。写入前核验 claim，写入与事件同一事务。

        投影和事件都带 claim_epoch：事后能看出"这份游标是谁在第几次认领时写的"，
        重启接管争议才有据可查。
        """
        if not isinstance(cursor, RunCursor):
            raise TianshuError("cursor 必须是 RunCursor")
        payload = canonical_bytes(cursor.to_dict()).decode("utf-8")
        now = _now()
        with self._log.transaction() as tx:
            run = self._assert_writer(run_id, claim)
            self._conn.execute(
                "INSERT INTO run_cursors (run_id, task_id, goal_revision,"
                " lease_epoch, claim_epoch, payload, updated_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " task_id=excluded.task_id, goal_revision=excluded.goal_revision,"
                " lease_epoch=excluded.lease_epoch, claim_epoch=excluded.claim_epoch,"
                " payload=excluded.payload, updated_at=excluded.updated_at",
                (
                    run_id,
                    run.task_id,
                    run.goal_revision,
                    run.lease_epoch,
                    claim.claim_epoch,
                    payload,
                    now,
                ),
            )
            tx.append(
                run_id,
                RUN_CURSOR_SAVED,
                {
                    "run_id": run_id,
                    "claim_epoch": claim.claim_epoch,
                    "goal_revision": run.goal_revision,
                    "lease_epoch": run.lease_epoch,
                    "payload_hash": sha256_hex(canonical_bytes(cursor.to_dict())),
                    "pending_proposals": len(cursor.pending_proposals),
                    "completed_call_ids": len(cursor.completed_call_ids),
                },
                task_id=run.task_id,
            )
        return cursor

    def load_cursor(self, run_id: str) -> tuple[RunCursor, dict[str, Any]] | None:
        row = self._conn.execute(
            "SELECT * FROM run_cursors WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        meta = {
            "task_id": row["task_id"],
            "goal_revision": row["goal_revision"],
            "lease_epoch": row["lease_epoch"],
            "claim_epoch": row["claim_epoch"],
            "updated_at": row["updated_at"],
        }
        return RunCursor.of(json.loads(row["payload"])), meta

    def commit_model_turn(
        self, run_id: str, turn: dict[str, Any], claim: JobClaim
    ) -> int:
        """提交一次模型结果，返回该 Run 内递增的序号。

        重复提交同一个 `turn_id`：payload 相同就直接返回原序号（重启重放安全），
        不同则报错——同一个 turn_id 出现两种内容，说明状态已经不一致，
        静默覆盖会把"到底花了多少钱"这条账改掉。
        """
        turn_id = turn.get("turn_id")
        if not turn_id:
            raise TianshuError("turn 缺少 turn_id")
        payload = canonical_bytes(turn).decode("utf-8")
        now = _now()
        with self._log.transaction() as tx:
            run = self._assert_writer(run_id, claim)
            existing = self._conn.execute(
                "SELECT seq, payload FROM run_turns WHERE run_id=? AND turn_id=?",
                (run_id, turn_id),
            ).fetchone()
            if existing is not None:
                if existing["payload"] == payload:
                    return int(existing["seq"])
                raise TianshuError(
                    f"turn_id 重复但内容不同：run={run_id} turn={turn_id}"
                )
            seq = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_turns WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            self._conn.execute(
                "INSERT INTO run_turns (run_id, turn_id, seq, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, turn_id, seq, payload, now),
            )
            tx.append(
                run_id,
                RUN_MODEL_TURN,
                {
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "seq": seq,
                    "claim_epoch": claim.claim_epoch,
                    "finish_reason": turn.get("finish_reason"),
                    "usage": turn.get("usage"),
                    "payload_hash": sha256_hex(canonical_bytes(turn)),
                },
                task_id=run.task_id,
            )
        return seq

    def list_model_turns(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT turn_id, seq, payload FROM run_turns WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            {"turn_id": r["turn_id"], "seq": r["seq"], **json.loads(r["payload"])}
            for r in rows
        ]

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        effect_state: str,
        reason: str | None = None,
        report_outcome: str | None = None,
    ) -> Run:
        """收尾。被 fence 的 Run 不许报成功——否则旧目标会"复活"。

        校验与写入放在同一条条件 UPDATE 里，避免「检查完到写入之间被改目标」
        的竞态。

        已经停下（暂停/等审批/待核查/正在取消）的 Run 不能直接收尾：那些状态
        要经过 resume 或 cancel 才能变成终态。否则"暂停"可以被一句
        `finish_run(succeeded)` 悄悄改写成成功，PRD §4.3 的状态机就白写了。
        """
        with self._log.transaction() as tx:
            return self._finish_locked(
                tx,
                run_id,
                outcome=outcome,
                effect_state=effect_state,
                reason=reason,
                report_outcome=report_outcome,
                require_running=True,
            )

    def _finish_locked(
        self,
        tx,
        run_id: str,
        *,
        outcome: str,
        effect_state: str,
        reason: str | None = None,
        report_outcome: str | None = None,
        require_running: bool = False,
    ) -> Run:
        if outcome not in RUN_OUTCOMES:
            raise TianshuError(
                f"未知 outcome：{outcome}（可选 {sorted(RUN_OUTCOMES)}）"
            )
        if effect_state not in EFFECT_STATES:
            raise TianshuError(
                f"未知 effect_state：{effect_state}（可选 {sorted(EFFECT_STATES)}）"
            )
        now = _now()
        run = self.get_run(run_id)
        if run is None:
            raise TianshuError(f"run 不存在：{run_id}")
        # 父 Run 取消时把"被取消传播覆盖了几个子任务"写进 reason（规格 §2.6）。
        # 数字从 `subtasks` 同一张表算：`cancelling` = 已请求但执行侧还没确认，
        # `cancelled` = 已确认。已经成功/失败/被 fence 的子任务不算"被取消"。
        if outcome == "cancelled" and self._has_subtasks(run_id):
            cancelled_children = self._count_cancelled_subtasks(run_id)
            reason = (
                f"{reason or CANCEL_REASON}；subtasks_cancelled={cancelled_children}"
            )
        if require_running and run.state in STOPPED_STATES:
            raise RunFenced(
                f"run 停在这些状态里不能直接收尾：{run_id}（state={run.state}）；"
                "请先 resume 或 cancel"
            )

        # 公开收尾要求"这个 Run 现在不是在停着的状态里"；把这条也塞进同一条
        # 条件 UPDATE，否则"检查通过 → 另一个进程把它暂停了 → 仍然收尾成功"
        # 这个窗口还开着。检查与写入必须在同一条语句里，这是本模块的老规矩。
        stopped = (
            " AND state NOT IN ('paused','waiting_approval','reconciling','cancelling')"
            if require_running
            else ""
        )
        cur = self._conn.execute(
            "UPDATE runs SET state=?, outcome=?, effect_state=?,"
            " control_requested=NULL, ended_at=?"
            " WHERE run_id=?"
            "   AND state NOT IN ('succeeded','failed','cancelled','fenced')"
            "   AND goal_revision = (SELECT goal_revision FROM tasks"
            "                        WHERE task_id = runs.task_id)"
            "   AND lease_epoch = (SELECT lease_epoch FROM tasks"
            "                      WHERE task_id = runs.task_id)" + stopped,
            (outcome, outcome, effect_state, now, run_id),
        )
        if cur.rowcount == 0:
            raise RunFenced(f"run {run_id} 已被 fence 或已终结，不得提交结果")

        self._conn.execute(
            "UPDATE tasks SET active_run_id=NULL, updated_at=?"
            " WHERE task_id=? AND active_run_id=?",
            (now, run.task_id, run_id),
        )
        # `run.end` 是"这次执行有结论了"的唯一事件。以前它由主循环单独写，
        # 于是 CLI 直接取消的 Run 只有 run.finish 没有 run.end——M0 的
        # S3「run.start → … → run.end」在取消路径上根本不成立，而取消恰恰是
        # 最需要让用户看见"到底改了什么"的路径。现在所有终态都从这一处写。
        tx.append(
            run_id,
            RUN_END,
            self._run_end_payload(
                run_id,
                outcome=report_outcome or outcome,
                effect_state=effect_state,
                reason=reason,
            ),
            task_id=run.task_id,
            actor_kind="core",
        )
        tx.append(
            run_id,
            RUN_FINISH,
            {
                "run_id": run_id,
                "task_id": run.task_id,
                "outcome": outcome,
                "effect_state": effect_state,
                **({"reason": reason} if reason else {}),
            },
            task_id=run.task_id,
        )
        # G2 D2：子 Run 的终态写入是 `subtasks` 行 / 父 Run `subtask.finished` /
        # 预算结算 / 父作业唤醒的**唯一**收尾点，且必须与 run.end 同一事务。
        # 顺序在这里无关正确性（同一事务），放在 run.end 之后是为了让
        # "子 Run 先有结论，父 Run 再收到完成事件"的事件次序可读。
        self._settle_child_subtask_locked(
            tx,
            run,
            pre_state=run.state,
            outcome=outcome,
            reason=reason,
        )
        # Run 结束即交还工作区，下一个 Run 才拿得到租约
        if self._leases is not None:
            self._leases.release_run(run_id)
        # 取消时作业必须一起收掉：留着 claimed 的作业会让下一个 worker 认领到
        # 一个已经结束的 Run，然后在一个终态上报"成功"。
        # 正常收尾则相反——作业要由 owner 用**自己的 claim** 来收，那才是
        # "我还活着且我还是 owner"的证据；这里替它收等于把这条证据抹掉。
        if outcome == "cancelled" and self._approvals is not None:
            # 取消之后旧批准不能再挂在待批队列里：它们属于一个已经结束的 Run，
            # 批了也不会执行，只会让人以为"还有事情没做完"。
            self._approvals.supersede_for_run(run_id, reason="run_cancelled")
        if outcome == "cancelled" and self._claims is not None:
            job = self._claims.job_for_run(run_id)
            # `waiting` 也要收：它是"等外面的事件"，而 Run 已经终结，那个事件
            # 永远不会再让它跑起来——留着就是一条永远不结束的作业。
            if job is not None and job.state in ("pending", "claimed", "waiting"):
                self._claims.cancel(job.job_id, reason="run_cancelled")
        finished = self.get_run(run_id)
        if finished is None:  # pragma: no cover - 同一连接刚更新的行
            raise TianshuError(f"run 投影读取失败：{run_id}")
        return finished

    def _run_end_payload(
        self,
        run_id: str,
        *,
        outcome: str,
        effect_state: str,
        reason: str | None,
    ) -> dict[str, Any]:
        """终态结算：全部从事件与游标里读，不接受调用方"报个数"。

        为什么不让主循环传入自己的计数器：CLI 直接取消时没有主循环在场，
        那条路径就永远写不出结算；两套写法还会让"改了几个文件"的账在两种
        收尾方式下不一致。统一从事实源汇总，两条路径自动一样。
        """
        events = self._log.read_run(run_id)
        receipts = [
            e.payload["receipt_id"]
            for e in events
            if e.type == "receipt" and e.payload.get("receipt_id")
        ]
        artifact: dict[str, Any] | None = None
        for ev in events:
            if ev.type != "tool.result":
                continue
            if ev.payload.get("tool_id") != "create_report":
                continue
            if ev.payload.get("status") != "succeeded":
                continue
            out = ev.payload.get("output") or {}
            if out.get("sha256"):
                artifact = {
                    k: out.get(k)
                    for k in ("path", "sha256", "bytes", "before_state", "after_state")
                }
        intent_count = sum(1 for e in events if e.type == "model.intent")
        result_count = sum(1 for e in events if e.type == "model.result")
        # 按**事件条数**算，不按 request_id 去重：崩溃后重发的那一轮用的是
        # 同一个 request_id，去重会把"已经付过一次钱、没记进账"抹成 0。
        unbilled = max(0, intent_count - result_count)
        stored = self.load_cursor(run_id)
        ledger = (stored[0].budget_ledger if stored is not None else {}) or {}
        unknown_usage = int(ledger.get("unknown_usage_calls", 0) or 0)
        unknown_cost = int(ledger.get("unknown_cost_calls", 0) or 0)
        known_cost_sum = int(ledger.get("cost_total_micros", 0) or 0)
        has_known_price_call = known_cost_sum > 0 or any(
            isinstance((ev.payload.get("cost") or {}).get("cost"), int)
            and not isinstance((ev.payload.get("cost") or {}).get("cost"), bool)
            for ev in events
            if ev.type == "model.result"
        )
        had_any_model_call = result_count > 0 or unknown_cost > 0 or unbilled > 0
        cost_total_micros: int | None = (
            known_cost_sum
            if (has_known_price_call or not had_any_model_call)
            else None
        )
        return {
            "outcome": outcome,
            "effect_state": effect_state,
            "artifact": artifact,
            "turns": int(ledger.get("turns", 0) or 0),
            "tokens": int(ledger.get("tokens", 0) or 0),
            "tokens_unknown_calls": unknown_usage,
            "cost_total_micros": cost_total_micros,
            "cost_unknown_calls": unknown_cost,
            "receipt_ids": receipts,
            # 预算可信 = 没有任何"可能花了但没记账"的调用
            "budget_enforced": unknown_usage == 0 and unbilled == 0,
            "unbilled_model_intents": unbilled,
            "error": reason,
        }

    def list_runs(self, task_id: str) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # ---- 投影重建 ----

    def rebuild_from_events(self) -> None:
        """按事件重建 tasks/runs 投影。事件是事实源，投影是可丢的缓存。

        只对"状态变更都记了事件"的库成立。PR #7 之前建的库只有投影表、
        没有对应事件，重建会把它们清空——那种库只能当只读历史，不能重建。
        """
        events = self._log.read_types(
            [
                TASK_CREATED,
                TASK_REVISED,
                RUN_START,
                RUN_FINISH,
                RUN_FENCED_EVENT,
                RUN_STATE,
                RUN_CONTROL,
                RUN_EFFECT,
            ]
        )
        tasks: dict[str, dict[str, Any]] = {}
        runs: dict[str, dict[str, Any]] = {}

        for ev in events:
            p = ev.payload
            if ev.type == TASK_CREATED:
                tasks[p["task_id"]] = {
                    "task_id": p["task_id"],
                    "goal": p["goal"],
                    "goal_revision": p["goal_revision"],
                    "lease_epoch": p["lease_epoch"],
                    "active_run_id": None,
                    "status": "open",
                    "workspaces": json.dumps(
                        [
                            b.to_dict()
                            for b in parse_bindings(p.get("workspaces") or [])
                        ],
                        ensure_ascii=False,
                    ),
                    "is_control": 1 if p.get("control") else 0,
                    "memory_scopes": json.dumps(
                        list(p.get("memory_scopes") or []), ensure_ascii=False
                    ),
                    "outbound_allow": json.dumps(
                        list(p.get("outbound_allow") or []), ensure_ascii=False
                    ),
                    "mcp_servers": json.dumps(
                        list(p.get("mcp_servers") or []), ensure_ascii=False
                    ),
                    # 旧事件没有这些键：按"根任务、不能委派"重建（v0 不补值）。
                    "parent_task_id": p.get("parent_task_id"),
                    "parent_run_id": p.get("parent_run_id"),
                    "subtask_id": p.get("subtask_id"),
                    "depth": int(p.get("depth") or 0),
                    "max_subtasks": int(p.get("max_subtasks") or 0),
                    "created_at": ev.ts,
                    "updated_at": ev.ts,
                }
            elif ev.type == TASK_REVISED:
                t = tasks.get(p["task_id"])
                if t is None:
                    continue
                t.update(
                    goal=p["goal"],
                    goal_revision=p["goal_revision"],
                    lease_epoch=p["lease_epoch"],
                    active_run_id=None,
                    updated_at=ev.ts,
                )
                if "workspaces" in p:
                    t["workspaces"] = json.dumps(
                        [
                            b.to_dict()
                            for b in parse_bindings(p.get("workspaces") or [])
                        ],
                        ensure_ascii=False,
                    )
            elif ev.type == RUN_START:
                runs[p["run_id"]] = {
                    "run_id": p["run_id"],
                    "task_id": p["task_id"],
                    "goal_revision": p["goal_revision"],
                    "lease_epoch": p["lease_epoch"],
                    "state": "created",
                    "outcome": None,
                    "effect_state": None,
                    "control_requested": None,
                    # 子 Run 的父链（子任务身份在 Run 侧的证据）。
                    "parent_run_id": p.get("parent_run_id"),
                    "subtask_id": p.get("subtask_id"),
                    "created_at": ev.ts,
                    "ended_at": None,
                }
                t = tasks.get(p["task_id"])
                if t is not None:
                    t["active_run_id"] = p["run_id"]
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_FINISH:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(
                    state=p["outcome"],
                    outcome=p["outcome"],
                    effect_state=p["effect_state"],
                    # 终态没有"排队的控制请求"这回事：请求要么已被确认，
                    # 要么随着 Run 结束作废。重建时也得清掉，否则投影里会
                    # 留一个永不生效的 pause。
                    control_requested=None,
                    ended_at=ev.ts,
                )
                t = tasks.get(p["task_id"])
                if t is not None and t["active_run_id"] == p["run_id"]:
                    t["active_run_id"] = None
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_FENCED_EVENT:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(state="fenced", control_requested=None, ended_at=ev.ts)
                t = tasks.get(r["task_id"])
                if t is not None and t["active_run_id"] == p["run_id"]:
                    t["active_run_id"] = None
                    t["updated_at"] = ev.ts
            elif ev.type == RUN_STATE:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                # 只有**显式标了** control_cleared 的状态迁移才清请求。
                # 无条件清会把"请求已排队、还没到安全点"这件事抹掉：一个
                # 运行中的 Run 先被暂停请求、随后因为别的原因过一次状态迁移
                # （比如转 waiting_approval），重建之后请求就凭空消失了。
                cleared = p.get("control_cleared") is True
                r.update(state=p["to"], updated_at=ev.ts)
                if cleared:
                    r["control_requested"] = None
            elif ev.type == RUN_CONTROL:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                # `run.control` 不只承载"控制请求"：租约接管的审计
                # （action=lease_takeover）也用它，但那个动作不改 Run 的控制意图。
                # 无条件 `control_requested=action` 会把审计动作写进投影，而
                # run.json 的 control_requested enum 只认 pause/cancel——重建一次
                # 就写出非法状态。只认真正的控制请求。
                if p.get("action") not in CONTROL_REQUESTS:
                    continue
                r.update(control_requested=p["action"], updated_at=ev.ts)
            elif ev.type == RUN_EFFECT:
                r = runs.get(p["run_id"])
                if r is None:
                    continue
                r.update(effect_state=p["effect_state"], updated_at=ev.ts)

        with self._log.transaction():
            self._conn.execute("DELETE FROM tasks")
            self._conn.execute("DELETE FROM runs")
            for t in tasks.values():
                self._conn.execute(
                    "INSERT INTO tasks (task_id, goal, goal_revision, lease_epoch,"
                    " active_run_id, status, workspaces, is_control, memory_scopes,"
                    " outbound_allow, mcp_servers, parent_task_id, parent_run_id,"
                    " subtask_id, depth, max_subtasks, created_at, updated_at)"
                    " VALUES (:task_id,:goal,:goal_revision,:lease_epoch,"
                    " :active_run_id,:status,:workspaces,:is_control,:memory_scopes,"
                    " :outbound_allow,:mcp_servers,:parent_task_id,:parent_run_id,"
                    " :subtask_id,:depth,:max_subtasks,:created_at,:updated_at)",
                    t,
                )
            for r in runs.values():
                self._conn.execute(
                    "INSERT INTO runs (run_id, task_id, goal_revision, lease_epoch,"
                    " state, outcome, effect_state, control_requested,"
                    " parent_run_id, subtask_id, created_at, ended_at)"
                    " VALUES (:run_id,:task_id,:goal_revision,:lease_epoch,"
                    " :state,:outcome,:effect_state,:control_requested,"
                    " :parent_run_id,:subtask_id,:created_at,:ended_at)",
                    r,
                )
                self._rebuild_responses_cursor(r["run_id"])

    def restore_responses_cursor(self, run_id: str) -> tuple[RunCursor, dict[str, Any]] | None:
        """仅恢复可从事件识别的 Responses 派生行，不追加事实或调用模型。"""
        with self._log.transaction():
            self._rebuild_responses_cursor(run_id)
        return self.load_cursor(run_id)

    def _rebuild_responses_cursor(self, run_id: str) -> None:
        from ._responses_rebuild import rebuild_responses_progress

        rebuilt = rebuild_responses_progress(self._log, self._log.read_run(run_id))
        if rebuilt is None:
            return
        cursor, turns, saved = rebuilt
        run = self.get_run(run_id)
        assert run is not None
        self._conn.execute("DELETE FROM run_turns WHERE run_id=?", (run_id,))
        for ev, turn in turns:
            self._conn.execute(
                "INSERT INTO run_turns (run_id, turn_id, seq, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, turn["turn_id"], ev.payload["seq"],
                 canonical_bytes(turn).decode("utf-8"), ev.ts),
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO run_cursors"
            " (run_id, task_id, goal_revision, lease_epoch, claim_epoch, payload, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, run.task_id, run.goal_revision, run.lease_epoch,
             saved.payload.get("claim_epoch", 0),
             canonical_bytes(cursor.to_dict()).decode("utf-8"), saved.ts),
        )
