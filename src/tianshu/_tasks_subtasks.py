"""子任务委派、执行、取消传播与恢复收尾（内部 Mixin，由 `TaskStore` 继承）。

所有子任务投影与父 Run 事件仍必须在 `TaskStore` 的同一 SQLite 事务内提交；
本模块不独立持有连接、不对外暴露新的公共类型。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from ._tasks_types import (
    CONTROL_CANCEL,
    RUN_CONTROL,
    RUN_CREATED,
    RUN_FENCED,
    RUN_RUNNING,
    SUBTASK_CANCELLED,
    SUBTASK_CANCELLING,
    SUBTASK_FAILED,
    SUBTASK_FENCED,
    SUBTASK_GOAL_MAX_BYTES,
    SUBTASK_MAX_DEPTH,
    SUBTASK_QUEUED,
    SUBTASK_RUNNING,
    SUBTASK_SUCCEEDED,
    SUBTASK_TERMINAL_STATES,
    SUBTASK_UNFINISHED_STATES,
    TERMINAL_STATES,
    Run,
    Subtask,
    Task,
    _now,
    clean_subtask_limits,
    subtask_budget_attempt,
    truncate_utf8,
)
from .contents import REF_PREFIX
from .errors import TianshuError
from .events import EventLog, sha256_hex
from .fsutil import atomic_write_bytes
from .jobs import JOB_PENDING, JOB_WAITING, JobStore
from .run_policy import EFFECTS, PURE_ONLY
from .workspace import WorkspaceBinding


class _SubtaskStoreMixin:
    """G2 委派 D1/D2/W10 子任务生命周期与启动恢复辅助，供 `TaskStore` 组合继承。"""

    _log: EventLog
    _conn: sqlite3.Connection
    _claims: JobStore | None
    _budget: Any | None
    _contents: Any | None
    _blob_dir: Path | None

    # ---- 子任务（G2 委派 D1） ----

    def create_subtask(
        self,
        parent_task: Task,
        parent_run: Run,
        *,
        subtask_id: str,
        goal: str,
        limits: dict[str, int] | None = None,
        write: bool = False,
    ) -> Subtask:
        """建一次委派：子 Task + 子 Run + 子 Job + `subtasks` 行，**同一事务**。

        四件事必须同生同死：少建子 Task 就是"父任务说派了、子任务不存在"；
        少建子 Job 就是"子 Run 永远不会被任何 worker 认领"；少了 `subtasks`
        行则父任务连自己派过什么都答不出来。父 Run 的 `subtask.spawned`
        事件由派发层在**同一个事务**里补写（它需要预算预占的结果）。

        子 Task 的字段默认与 W10 之前一致（规格 §2.1）：父任务的全部绑定逐个
        改写成只读、去重；没有 exec 策略、没有外发白名单、没有记忆作用域、
        `max_subtasks=0`、`depth=1`；子 Run `pure_only`。写文件/出网/exec/
        MCP 都是**拒绝**，不是"弹审批"。空 memory_scopes 不会禁止 user/task
        记忆；派发层和记忆执行器还必须根据 depth/父链独立拒绝所有记忆写入。

        W10（`write=True`，`subtask.spawn` 2.0 显式声明）：子任务可以写**父任务
        自己就能写**的目录——绑定逐条保留父的 mode（父只读的根仍然只读，授权
        上限恒为父绑定），子 Run 改为 `effects` 资格。出网/MCP/exec/记忆写/
        再委派依旧全拒；子 Run 里的每个写动作仍走既有派发审批（与父 Run 完全
        同构），没有任何"子代理免审通道"。
        """
        if not isinstance(write, bool):
            raise TianshuError("write 必须是布尔值")
        if self._claims is None:
            raise TianshuError(
                "未配置 JobStore：创建子任务必须同时建子作业"
                "（否则子 Run 永远不会被认领）"
            )
        if parent_task.depth != 0:
            raise TianshuError(
                f"深度 {parent_task.depth} 的任务不能再委派：{parent_task.task_id}"
            )
        if parent_run.task_id != parent_task.task_id:
            raise TianshuError(
                f"父 Run 不属于这个父任务：{parent_run.run_id}"
                f"（run.task_id={parent_run.task_id}）"
            )
        goal = (goal or "").strip()
        if not goal:
            raise TianshuError("子任务目标不能为空")
        raw = goal.encode("utf-8")
        if len(raw) > SUBTASK_GOAL_MAX_BYTES:
            raise TianshuError(
                f"子任务目标超过 {SUBTASK_GOAL_MAX_BYTES} 字节：{len(raw)}"
            )
        clean = clean_subtask_limits(limits)
        # 绑定改写：默认全部只读（W10 之前的固定行为）；`write=True` 时逐条
        # **保留父的 mode**——父可写的根子任务才可写，父只读的根仍然只读。
        # 授权上限恒为父绑定：子任务永远拿不到父任务没有的目录或模式。
        # 去重按 (canonical 根, 快照策略) —— 同一个目录的两种写法在
        # WorkspaceBinding 构造时就归一到同一个根了，这里只需按根去重。
        rewritten: dict[tuple[str, str], WorkspaceBinding] = {}
        for binding in parent_task.workspaces:
            mode = binding.mode if write else "readonly"
            child_binding = WorkspaceBinding(
                root_path=binding.root_path,
                mode=mode,
                snapshot_policy=binding.snapshot_policy,
            )
            rewritten[
                (child_binding.root_path, child_binding.snapshot_policy)
            ] = child_binding
        bindings = list(rewritten.values())
        # effects 资格只在"实际拿到了可写绑定"时才给：write=true 但父任务全只读
        # 是**如实降级**（子任务就是只读的），子 Run 仍是 pure_only——不凭空给一个
        # 没有可写面的 effects 资格。effects 只放开"既有派发审批链路里的文件写"：
        # pure_only 的子 Run 连写工具都看不见（清单过滤），effects 的每个写动作
        # 仍要过 prepare 判定 + 人工批准 + 执行侧 digest 复核。
        write_effective = write and any(b.mode == "writable" for b in bindings)
        child_policy = EFFECTS if write_effective else PURE_ONLY
        # 与 start_run 同口径：可写子任务会创建 effects 子 Run（驱动时建快照清单并开放文件写），
        # 必须在开启 SQLite 写事务与落任何子 Task/Run/Job 行之前过统一准入门；
        # pure_only 子任务（含 write=True 但父绑定全只读的如实降级）保持无 2 GiB 阈值门。
        self.require_new_run(  # type-checked via TaskStore composition
            child_policy,
            writable_roots=tuple(
                Path(b.root_path) for b in bindings if b.can_write
            ),
        )
        now = _now()
        with self._log.transaction() as tx:
            child_task_id = self._create_task_locked(
                tx,
                goal=goal,
                bindings=bindings,
                exec_policy=None,
                control=False,
                memory_scopes=[],
                outbound_allow=(),
                # 子任务不能调 MCP：白名单为空（write=True 也只放开文件写）。
                mcp_servers=[],
                max_subtasks=0,
                parent_task_id=parent_task.task_id,
                parent_run_id=parent_run.run_id,
                subtask_id=subtask_id,
                depth=SUBTASK_MAX_DEPTH,
                actor_kind="core",
            )
            child_task = self.get_task(child_task_id)
            if child_task is None:  # pragma: no cover - 同一事务里刚写的行
                raise TianshuError("子任务创建失败：子 Task 投影不存在")
            child_run_id = f"run_{uuid.uuid4().hex[:12]}"
            self._insert_run_locked(
                tx,
                task=child_task,
                run_id=child_run_id,
                now=now,
                execution_policy=child_policy,
                # `task` 必须进 run.start：`replay` 用它重建首条用户消息。
                # 子 Run 少了它，回放会拿空字符串去比 messages_hash，必然红。
                payload={"task": goal},
                parent_run_id=parent_run.run_id,
                subtask_id=subtask_id,
            )
            job = self._claims.enqueue(child_run_id)
            self._conn.execute(
                "INSERT INTO subtasks (subtask_id, parent_task_id, parent_run_id,"
                " child_task_id, child_run_id, job_id, goal, limits, state,"
                " parent_goal_revision, parent_lease_epoch, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    subtask_id,
                    parent_task.task_id,
                    parent_run.run_id,
                    child_task_id,
                    child_run_id,
                    job.job_id,
                    goal,
                    json.dumps(clean, ensure_ascii=False, sort_keys=True),
                    SUBTASK_QUEUED,
                    # 抄**父 Run 创建时绑定的版本**，不是当时任务的当前值：
                    # fence 要判的是"父 Run 还能不能代表这个目标"。
                    parent_run.goal_revision,
                    parent_run.lease_epoch,
                    now,
                ),
            )
        created = self.get_subtask(parent_run.run_id, subtask_id)
        if created is None:  # pragma: no cover - 同一连接刚写入的行
            raise TianshuError(f"子任务创建失败：subtasks 投影不存在：{subtask_id}")
        return created

    def get_subtask(self, parent_run_id: str, subtask_id: str) -> Subtask | None:
        row = self._conn.execute(
            "SELECT * FROM subtasks WHERE parent_run_id=? AND subtask_id=?",
            (parent_run_id, subtask_id),
        ).fetchone()
        return self._row_to_subtask(row) if row is not None else None

    def list_subtasks(self, parent_run_id: str) -> list[Subtask]:
        rows = self._conn.execute(
            "SELECT * FROM subtasks WHERE parent_run_id=? ORDER BY created_at,"
            " subtask_id",
            (parent_run_id,),
        ).fetchall()
        return [self._row_to_subtask(row) for row in rows]

    def count_unfinished_subtasks(self, parent_run_id: str) -> int:
        marks = ",".join("?" * len(SUBTASK_UNFINISHED_STATES))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM subtasks WHERE parent_run_id=?"
            f" AND state IN ({marks})",
            (parent_run_id, *sorted(SUBTASK_UNFINISHED_STATES)),
        ).fetchone()
        return int(row["n"])

    def count_subtasks_for_task(self, parent_task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM subtasks WHERE parent_task_id=?",
            (parent_task_id,),
        ).fetchone()
        return int(row["n"])

    def request_subtask_cancel(
        self, parent_run_id: str, subtask_id: str
    ) -> Subtask | None:
        """记录取消请求：状态置 `cancelling` + 子作业与子 Run 取消请求。

        **这里不写 `cancelled`**：真正停下由执行侧在安全点确认（D2）。D1 只
        记录请求，D2 起子 Run 也会看到 `control_requested=cancel`，但它仍然
        由 `confirm_control()` 在安全点收尾——不假装撤销已经完成。已终结的
        子任务原样返回（幂等：不报错，也不假装撤销）。
        """
        with self._log.transaction():
            row = self.get_subtask(parent_run_id, subtask_id)
            if row is None:
                return None
            if row.state in SUBTASK_TERMINAL_STATES:
                return row
            if row.state == SUBTASK_CANCELLING:
                return row
            self._conn.execute(
                "UPDATE subtasks SET state=? WHERE parent_run_id=? AND subtask_id=?"
                " AND state=?",
                (SUBTASK_CANCELLING, parent_run_id, subtask_id, row.state),
            )
            if self._claims is not None:
                # 只置请求位：`request_cancel` 不写 `cancelled`，作业状态仍是
                # pending/claimed/waiting，执行侧自己决定何时在安全点收手。
                self._claims.request_cancel(row.job_id)
            # D2：子 Run 也要看到取消请求。用 `defer` 变体——子 Run 可能还在
            # `created`（属于安全状态），就地收尾会违反"取消先落 cancelling、
            # 由执行侧在安全点确认"的口径。
            self.request_cancel_deferred(
                row.child_run_id, reason="subtask_cancel_requested"
            )
        return self.get_subtask(parent_run_id, subtask_id)

    # ---- 子任务执行与收尾（G2 委派 D2） ----

    def mark_subtask_running(self, child_run_id: str) -> Subtask | None:
        """子 Run 真正开始被驱动：`queued → running`。

        只允许从 `queued` 推进：`cancelling` 不能被"我开始了"覆盖（人已经
        要求停），终态更不能被改写。已经在 `running` 时保持原状（恢复驱动
        不该产生第二条状态迁移）。
        """
        run = self.get_run(child_run_id)
        if run is None or not run.parent_run_id or not run.subtask_id:
            return None
        with self._log.transaction():
            self._conn.execute(
                "UPDATE subtasks SET state=? WHERE parent_run_id=? AND subtask_id=?"
                " AND state=?",
                (SUBTASK_RUNNING, run.parent_run_id, run.subtask_id, SUBTASK_QUEUED),
            )
        return self.get_subtask(run.parent_run_id, run.subtask_id)

    def request_cancel_deferred(self, run_id: str, *, reason: str) -> Run | None:
        """只记下取消请求，**不**就地收尾（G2 D2 子 Run / 父取消传播用）。

        与 `request_cancel` 的区别只有一处，但很关键：`request_cancel` 对
        `created/queued/waiting_approval/paused/reconciling` 会**立即**终结
        Run；子任务的取消口径是"先落 cancelling，由执行侧在安全点确认"，
        所以这里一律只写请求。真正的终态仍由 `confirm_control()` 写。
        """
        with self._log.transaction() as tx:
            run = self.get_run(run_id)
            if run is None:
                return None
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                return run
            if run.control_requested == CONTROL_CANCEL:
                return run
            tx.append(
                run_id,
                RUN_CONTROL,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "action": CONTROL_CANCEL,
                    "applied_immediately": False,
                    "effect_state": run.effect_state,
                    "reason": reason,
                },
                task_id=run.task_id,
                actor_kind="core",
            )
            self._conn.execute(
                "UPDATE runs SET control_requested=? WHERE run_id=?"
                " AND state NOT IN ('succeeded','failed','cancelled','fenced')",
                (CONTROL_CANCEL, run_id),
            )
        return self.get_run(run_id)

    def parent_fence_reason(self, child_run_id: str) -> str | None:
        """子 Run 的父目标是否还和 spawn 时记录的一致（只读判断）。

        为什么要比 `parent_task` 的当前值而不是 `parent_run` 行上的值：
        改目标时 `revise_task` 更新的是 **task** 的 revision/epoch，父 Run
        行上的旧值保持不变（它代表"创建时绑定的版本"）。只看 Run 行会永远
        看不出目标已改。两个值都比，是为了同时覆盖"父 Run 自己被 fence"。
        """
        run = self.get_run(child_run_id)
        if run is None or not run.parent_run_id or not run.subtask_id:
            return None
        row = self.get_subtask(run.parent_run_id, run.subtask_id)
        if row is None:
            return f"子任务行缺失：{run.parent_run_id}/{run.subtask_id}"
        return self._parent_fence_reason_for_row(row)

    def _parent_fence_reason_for_row(self, row: Subtask) -> str | None:
        parent = self.get_run(row.parent_run_id)
        if parent is None:
            return f"父 Run 不存在：{row.parent_run_id}"
        parent_task = self.get_task(parent.task_id)
        if parent_task is None:
            return f"父 Task 不存在：{parent.task_id}"
        if parent.state == RUN_FENCED:
            return f"父 Run 已被 fence：{row.parent_run_id}"
        current = (parent_task.goal_revision, parent_task.lease_epoch)
        bound = (parent.goal_revision, parent.lease_epoch)
        recorded = (row.parent_goal_revision, row.parent_lease_epoch)
        if current != recorded or bound != recorded:
            return (
                "父目标已变更（子任务被 fence）：spawn 时 "
                f"{recorded[0]}/{recorded[1]}，父 Run 绑定 {bound[0]}/{bound[1]}，"
                f"父 Task 当前 {current[0]}/{current[1]}"
            )
        return None

    def fence_child_run(self, child_run_id: str, *, reason: str) -> Subtask | None:
        """把父目标已变更的子任务置为 `fenced`（D2 §2.6）。

        三条纪律：
        1. **不写 `subtask.finished`**：fence 的结果不能回填父 Run（写一条
           "完成"事件会让父 Run 的审计里出现一个不该被采信的结论）。
        2. 子 Run 只**请求取消**（安全点由执行侧确认），不在这里假装停住。
        3. 预算不退款：确定没执行过的走 `abandon_unsent`，其余 `settle 1`。
        """
        run = self.get_run(child_run_id)
        if run is None or not run.parent_run_id or not run.subtask_id:
            return None
        with self._log.transaction() as tx:
            row = self.get_subtask(run.parent_run_id, run.subtask_id)
            if row is None or row.state in SUBTASK_TERMINAL_STATES:
                return row
            self._mark_subtask_fenced_locked(tx, row, reason=reason)
            self.request_cancel_deferred(
                child_run_id, reason="parent_fenced"
            )
            if self._claims is not None:
                job = self._claims.job_for_run(child_run_id)
                if job is not None and job.state in ("pending", "claimed", "waiting"):
                    self._claims.request_cancel(job.job_id)
            self._maybe_wake_parent(row.parent_run_id)
        return self.get_subtask(run.parent_run_id, run.subtask_id)

    def _mark_subtask_fenced_locked(self, tx, row: Subtask, *, reason: str) -> None:
        now = _now()
        cur = self._conn.execute(
            "UPDATE subtasks SET state=?, error=?, ended_at=?"
            " WHERE parent_run_id=? AND subtask_id=?"
            "   AND state NOT IN (?,?,?,?)",
            (
                SUBTASK_FENCED,
                reason,
                now,
                row.parent_run_id,
                row.subtask_id,
                SUBTASK_SUCCEEDED,
                SUBTASK_FAILED,
                SUBTASK_CANCELLED,
                SUBTASK_FENCED,
            ),
        )
        if cur.rowcount == 0:
            return
        self._settle_subtask_budget(
            row, state=SUBTASK_FENCED, never_executed=self._subtask_never_executed(row)
        )

    def _subtask_never_executed(self, row: Subtask) -> bool:
        """子任务是否**确定**没执行过（只在这个条件下才允许 `abandon_unsent`）。

        判据是三条同时成立：子 Run 仍 `created`、作业从未被认领
        （`claim_epoch == 0`）、作业仍是 `pending`。任何一条不成立都按"可能
        执行过"结算——取消不退款，宁可多记一笔，也不把未知洗成零。
        """
        if self._claims is None:
            return False
        child = self.get_run(row.child_run_id)
        job = self._claims.get(row.job_id)
        return (
            child is not None
            and child.state == RUN_CREATED
            and job is not None
            and int(job.claim_epoch) == 0
            and job.state == JOB_PENDING
        )

    def _settle_subtask_budget(
        self, row: Subtask, *, state: str, never_executed: bool
    ) -> None:
        """结算父账户 `subtasks` 维度上的那 1 个预占（幂等）。"""
        if self._budget is None:
            return
        account_id = f"run:{row.parent_run_id}"
        lookup = getattr(self._budget, "reservation_for", None)
        if lookup is None:  # pragma: no cover - 单测替身没有账本
            return
        reservation = lookup(account_id, subtask_budget_attempt(
            row.parent_run_id, row.subtask_id
        ))
        if reservation is None:
            return
        if never_executed:
            self._budget.abandon_unsent(
                reservation.reservation_id,
                reason=f"子任务未开始即终止（{state}）",
            )
            return
        self._budget.settle(
            reservation.reservation_id,
            usage={"subtasks": 1},
            known_dimensions={"subtasks"},
        )

    def _maybe_wake_parent(self, parent_run_id: str) -> bool:
        """父 Run 已无未完成子任务、且正停在 `waiting` → 唤醒父作业。

        只在父 Run 仍是 `RUNNING` 时唤醒：`waiting_approval` / `paused` 的
        父作业是在等人，不是等子任务；把它叫醒只会让 worker 认领后立刻重新
        park，每轮涨一次 `claim_epoch` 并把用户刚批的审批作废。
        父 Run 已终态时是 no-op（不能把终态作业拉回队列）。
        """
        if self._claims is None:
            return False
        if self.count_unfinished_subtasks(parent_run_id) != 0:
            return False
        parent = self.get_run(parent_run_id)
        if parent is None or parent.state != RUN_RUNNING:
            return False
        job = self._claims.job_for_run(parent_run_id)
        if job is None or job.state != JOB_WAITING:
            return False
        self._claims.wake(job.job_id, reason="children_finished")
        return True

    def _child_result(self, row: Subtask) -> tuple[str, str | None, str | None]:
        """子 Run 最后一轮有界输出 →（summary, result_ref, 错误说明）。

        **不编造**：拿不到输出时 summary 为空并返回一句"输出不可用"，不会拿
        子任务目标或状态拼一句看起来像结论的话。内容引用先落盘再登记
        （与网络阶段 `_write_content` 同一纪律：孤儿 blob 无害，日志承诺一份
        不存在的内容有害）。
        """
        text = ""
        for event in self._log.read_run(row.child_run_id, verify=False):
            if event.type != "model.result":
                continue
            content = event.payload.get("content")
            if isinstance(content, str) and content.strip():
                # 取**最后一条非空**输出：后面可能有空 end_turn。
                text = content
        if not text:
            return "", None, "输出不可用（子 Run 没有可读取的最后一轮输出）"
        summary = truncate_utf8(text)
        if self._contents is None or self._blob_dir is None:
            return summary, None, "内容存储未配置，结果没有 content:// 引用"
        data = text.encode("utf-8")
        digest = sha256_hex(data)
        blob = self._blob_dir / digest
        if not blob.exists():
            from .restore_access import ensure_material_write_allowed

            ensure_material_write_allowed(self._blob_dir, owner_log=self._log)
            atomic_write_bytes(blob, data)
        self._contents.record(
            digest,
            data,
            task_id=row.parent_task_id,
            run_id=row.parent_run_id,
        )
        return summary, f"{REF_PREFIX}{digest}", None

    def _usage_for_child(self, child_run_id: str) -> dict[str, int] | None:
        stored = self.load_cursor(child_run_id)
        if stored is None:
            return None
        ledger = stored[0].budget_ledger or {}
        usage = {
            key: int(ledger[key])
            for key in ("turns", "tokens")
            if isinstance(ledger.get(key), int)
            and not isinstance(ledger.get(key), bool)
        }
        return usage or None

    def _settle_subtask_row_locked(
        self,
        tx,
        row: Subtask,
        *,
        state: str,
        summary: str,
        result_ref: str | None,
        error: str | None,
        usage: dict[str, int] | None,
        never_executed: bool,
        write_event: bool,
    ) -> bool:
        """写 `subtasks` 行 + 父 Run 的 `subtask.finished` + 预算结算。

        这三件事必须在**同一个事务**里：只写行不写事件，父 Run 的日志里没有
        "子任务完成了"的证据；只写事件不写行，`subtask.result` 永远读不到。
        行是"当前结论"，事件是"当时结论"，两者对不上就只能靠猜。
        """
        cur = self._conn.execute(
            "UPDATE subtasks SET state=?, result_ref=?, summary=?, error=?,"
            " ended_at=? WHERE parent_run_id=? AND subtask_id=?"
            "   AND state NOT IN (?,?,?,?)",
            (
                state,
                result_ref,
                summary,
                error,
                _now(),
                row.parent_run_id,
                row.subtask_id,
                SUBTASK_SUCCEEDED,
                SUBTASK_FAILED,
                SUBTASK_CANCELLED,
                SUBTASK_FENCED,
            ),
        )
        if cur.rowcount == 0:
            return False
        if write_event:
            payload: dict[str, Any] = {
                "subtask_id": row.subtask_id,
                "state": state,
            }
            if result_ref:
                payload["result_ref"] = result_ref
            if error:
                payload["error"] = error
            if usage:
                payload["usage"] = usage
            tx.append(
                row.parent_run_id,
                "subtask.finished",
                payload,
                task_id=row.parent_task_id,
                actor_kind="core",
            )
        self._settle_subtask_budget(
            row, state=state, never_executed=never_executed
        )
        return True

    def _settle_child_subtask_locked(
        self,
        tx,
        run: Run,
        *,
        pre_state: str,
        outcome: str,
        reason: str | None,
    ) -> None:
        """子 Run 收尾时结算它的 `subtasks` 行（在 `_finish_locked` 同一事务里）。"""
        if self._claims is None:
            return
        parent_run_id = run.parent_run_id
        subtask_id = run.subtask_id
        if not parent_run_id or not subtask_id:
            return
        row = self.get_subtask(parent_run_id, subtask_id)
        if row is None or row.state in SUBTASK_TERMINAL_STATES:
            # 已经被 fence / 父取消传播结算过：不重复写、不重复结算预算。
            return
        fence_reason = self._parent_fence_reason_for_row(row)
        if fence_reason is not None:
            self._mark_subtask_fenced_locked(tx, row, reason=fence_reason)
            self._maybe_wake_parent(parent_run_id)
            return
        state = {
            "succeeded": SUBTASK_SUCCEEDED,
            "failed": SUBTASK_FAILED,
            "cancelled": SUBTASK_CANCELLED,
        }.get(outcome)
        if state is None:  # pragma: no cover - Run outcome 是封闭集
            return
        summary, result_ref, note = self._child_result(row)
        errors = [part for part in (reason, note) if part]
        error = "；".join(errors) if errors else None
        usage = self._usage_for_child(run.run_id)
        never = pre_state == RUN_CREATED and self._subtask_never_executed(row)
        if self._settle_subtask_row_locked(
            tx,
            row,
            state=state,
            summary=summary,
            result_ref=result_ref,
            error=error,
            usage=usage,
            never_executed=never,
            write_event=True,
        ):
            self._maybe_wake_parent(parent_run_id)

    def _propagate_cancel_locked(self, tx, parent_run_id: str) -> int:
        """父 Run 取消时把取消传给未完成子任务；返回"被要求停"的个数。

        对**确定没执行过**的子任务（子 Run 仍 created、作业从未被认领）当场
        以 `cancelled` 收尾：它们的作业还没被任何人认领，留着只会产生一条
        永远没人跑的 `cancelling` 行。其余一律只置 `cancelling` + 子 Run
        取消请求 + 子作业 `request_cancel`，由执行侧在安全点确认。
        """
        rows = [
            row
            for row in self.list_subtasks(parent_run_id)
            if row.state in SUBTASK_UNFINISHED_STATES
        ]
        for row in rows:
            if row.state in SUBTASK_TERMINAL_STATES:
                continue
            if self._subtask_never_executed(row):
                self._settle_subtask_row_locked(
                    tx,
                    row,
                    state=SUBTASK_CANCELLED,
                    summary="",
                    result_ref=None,
                    error="父 Run 取消：子任务未开始，未产生任何输出",
                    usage=None,
                    never_executed=True,
                    write_event=True,
                )
                # 子 Run 也要有结论：它从未被驱动，但一条永远 created 的
                # Run 会让"这次委派去哪了"没有答案。不要求 require_running
                # （created 本来就不是 running）。
                self._finish_locked(
                    tx,
                    row.child_run_id,
                    outcome="cancelled",
                    effect_state="none_applied",
                    reason="父 Run 取消（子任务未开始）",
                )
                continue
            self._conn.execute(
                "UPDATE subtasks SET state=? WHERE parent_run_id=? AND subtask_id=?"
                " AND state IN (?,?,?)",
                (
                    SUBTASK_CANCELLING,
                    row.parent_run_id,
                    row.subtask_id,
                    SUBTASK_QUEUED,
                    SUBTASK_RUNNING,
                    SUBTASK_CANCELLING,
                ),
            )
            self.request_cancel_deferred(
                row.child_run_id, reason="parent_cancelled"
            )
            if self._claims is not None:
                self._claims.request_cancel(row.job_id)
        return len(rows)

    def _count_cancelled_subtasks(self, parent_run_id: str) -> int:
        """父 Run 取消时"被要求停/已停"的子任务数（写进 run.end 的 reason）。"""
        marks = (SUBTASK_CANCELLING, SUBTASK_CANCELLED)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM subtasks WHERE parent_run_id=?"
            " AND state IN (?,?)",
            (parent_run_id, *marks),
        ).fetchone()
        return int(row["n"])

    def _has_subtasks(self, parent_run_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM subtasks WHERE parent_run_id=? LIMIT 1",
            (parent_run_id,),
        ).fetchone()
        return row is not None

    def wake_stale_parents(self) -> list[str]:
        """启动恢复：父作业停在 `waiting`、子任务却已经全终结 → 补一次 wake。

        崩溃窗口里"子任务收尾事务"要么全提交（含 wake）要么全回滚（子任务
        仍未完成），所以正常路径不会留下这种状态。但旧库升级、或将来某个
        在 wake 之前就退出的实现会留下它。判据只认三件事实：作业 waiting、
        父 Run 仍 RUNNING、`subtasks` 表里没有未完成行——不按进程退出猜。
        """
        if self._claims is None:
            return []
        woken: list[str] = []
        with self._log.transaction():
            for job in self._claims.list_jobs(state=JOB_WAITING):
                run = self.get_run(job.run_id)
                if run is None or run.state != RUN_RUNNING:
                    continue
                if not self._has_subtasks(run.run_id):
                    continue
                if self.count_unfinished_subtasks(run.run_id) != 0:
                    continue
                if self._claims.wake(
                    job.job_id, reason="stale_parent_recovery"
                ) is not None:
                    woken.append(run.run_id)
        return woken

    def reconcile_finished_subtasks(self) -> list[str]:
        """启动恢复：子 Run 已终结但 `subtasks` 行还停在未完成 → 以 Run 为准修正。

        证据是子 Run 的终态（`run.finish` 事件 + 投影），不是"进程还活着吗"。
        已终结的 Run 行不可能再变，按它补写子任务行是安全的；反过来按进程
        退出猜"它大概没跑完"就会把一次成功说成失败。
        """
        if self._claims is None:
            return []
        fixed: list[str] = []
        with self._log.transaction() as tx:
            rows = self._conn.execute(
                "SELECT * FROM subtasks WHERE state IN (?,?,?)",
                (
                    SUBTASK_QUEUED,
                    SUBTASK_RUNNING,
                    SUBTASK_CANCELLING,
                ),
            ).fetchall()
            for raw in rows:
                row = self._row_to_subtask(raw)
                child = self.get_run(row.child_run_id)
                if child is None:
                    continue
                # 与执行侧的收尾路径用**同一套** fence 判定：父目标已变的子任务
                # 只能记 `fenced`（不写 `subtask.finished`），不能因为"是恢复
                # 路径"就按子 Run 的 outcome 把一份作废的结论写回父 Run。
                fence_reason = self._parent_fence_reason_for_row(row)
                if fence_reason is not None:
                    self._mark_subtask_fenced_locked(
                        tx, row, reason=f"启动恢复：{fence_reason}"
                    )
                    fixed.append(row.subtask_id)
                    continue
                if child.state == RUN_FENCED:
                    self._mark_subtask_fenced_locked(
                        tx, row, reason="子 Run 已被 fence（启动恢复，以 Run 为准）"
                    )
                    fixed.append(row.subtask_id)
                    continue
                if child.state not in TERMINAL_STATES:
                    continue
                outcome = child.outcome or "failed"
                state = {
                    "succeeded": SUBTASK_SUCCEEDED,
                    "failed": SUBTASK_FAILED,
                    "cancelled": SUBTASK_CANCELLED,
                }.get(outcome)
                if state is None:  # pragma: no cover - 终态 outcome 是封闭集
                    continue
                summary, result_ref, note = self._child_result(row)
                usage = self._usage_for_child(child.run_id)
                error = note or f"启动恢复：按子 Run 终态 {outcome} 修正"
                if self._settle_subtask_row_locked(
                    tx,
                    row,
                    state=state,
                    summary=summary,
                    result_ref=result_ref,
                    error=error,
                    usage=usage,
                    never_executed=False,
                    write_event=True,
                ):
                    fixed.append(row.subtask_id)
            for parent_run_id in {
                self._row_to_subtask(raw).parent_run_id for raw in rows
            }:
                self._maybe_wake_parent(parent_run_id)
        return fixed
