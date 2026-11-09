"""唯一派发入口（M2 · PR B · Task 4）。

派发回答的是："这次调用**现在**可以真的改文件吗，凭什么？"

它把四件事放进**同一个 SQLite 提交**：

1. 当前状态复核（Run 没被 fence、revision/epoch/租约/claim 都对得上）；
2. 审批的**唯一消费**（条件 UPDATE，两个并发派发只有一个能赢）；
3. grant 的签发与消费（短命票据：它说的是"此刻可以写"，而"此刻"会过期）；
4. `tool.intent` 的提交（写前证据，崩溃恢复靠它判断"有没有进入写入阶段"）。

四件事必须同生同死。分开提交就会出现两种坏状态：批准被用掉但没执行（人要再批
一次，账上却是"用过了"），或者执行了两次（两份 effect，只有一份被记录）。

然后是第二个阶段：**执行守卫内**再核一遍，记 `execution.started`，才真正打开文件。
为什么核两遍？因为派发事务提交之后到这个文件被打开之间还有一个窗口，另一个进程
可以撤销审批、接管 claim、换掉目标。"先撤销就赢"还是"先开始就赢"由执行守卫裁决：

- 撤销先拿到守卫 → 旧操作作废（`approval.revoked`，派发记录标 revoked，文件不动）；
- 执行先拿到守卫 → `execution.started` 已落库，这次调用是 in-flight，
  撤销不再承诺回滚（也拦不住已经发生的写）。

这个顺序是刻意的：宁可让用户看到"已经在执行了，撤销没生效"，也不假装撤销成功而
文件其实被改了。

只读动作（list_files / read_text）不进派发：它们没有可授权的副作用，被拦的是越界
读取，那由 `Workspace.resolve` 在执行侧完成。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crashpoint import failpoint
from .errors import (
    OverwriteRefused,
    RunFenced,
    TianshuError,
    ToolArgumentError,
    ToolNotAllowed,
    WorkspaceBlocked,
    WorkspaceViolation,
)
from .events import EventLog, canonical_bytes, sha256_hex
from .guard import ensure_not_blocked
from .jobs import JobClaim, JobLost, JobStore
from .leases import (
    GUARD_TIMEOUT_MS,
    ExecutionGuard,
    GuardTimeout,
    LeaseLost,
    LeaseStore,
    execution_guard,
)
from .permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_OVERWRITE,
    APPROVAL_ACTIONS,
    POLICY_DENIED,
    ActionRequest,
    Approval,
    ApprovalError,
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyDecision,
    PolicyEngine,
)
from .receipt import build_receipt
from .snapshot import SnapshotStore
from .tasks import RUN_RUNNING, Run, Task, TaskStore
from .tools import (
    SPECS,
    AdmittedCall,
    DispatchRecord,
    FileExecutor,
    ToolResult,
    check_request_preconditions,
    observe_state,
)
from .workspace import (
    ResourceIdentity,
    Workspace,
    WorkspaceBinding,
    is_ancestor_or_self,
    overlap_key,
)

# call_id 被另一个请求占用（同一 run 里换个参数复用同一个 id）
D_DISPATCH_CALL_ID_REUSED = "dispatch.call_id_reused"

DISPATCH_REQUESTED = "dispatch.requested"
DISPATCH_REJECTED = "dispatch.rejected"
DISPATCH_ADMITTED = "dispatch.admitted"
DISPATCH_REVOKED = "dispatch.revoked"

STATE_WAITING = "waiting"
STATE_ADMITTED = "admitted"
STATE_REJECTED = "rejected"
STATE_DONE = "done"

# 派发记录的状态：intent 之后还没开始执行，正是"可以被撤销"的窗口
DISPATCH_INTENT = "intent"
DISPATCH_EXECUTING = "executing"
DISPATCH_FINISHED = "done"
DISPATCH_ROW_REVOKED = "revoked"
DISPATCH_ROW_REJECTED = "rejected"
DISPATCH_ERROR = "error"

KIND_WAITING = "waiting"
KIND_ADMITTED = "admitted"
KIND_REJECTED = "rejected"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_requests (
    request_id     TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    payload        TEXT NOT NULL,
    args_payload   TEXT NOT NULL,
    state          TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    rule_id        TEXT NOT NULL DEFAULT '',
    approval_id    TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_requests_run
    ON dispatch_requests(run_id, state);

CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    call_id           TEXT NOT NULL,
    request_id        TEXT NOT NULL,
    request_digest    TEXT NOT NULL,
    tool_id           TEXT NOT NULL,
    action            TEXT NOT NULL,
    resources         TEXT NOT NULL,
    target            TEXT NOT NULL,
    rel_path          TEXT NOT NULL,
    workspace_root    TEXT NOT NULL,
    resource_key      TEXT NOT NULL,
    approval_id       TEXT,
    grant_id          TEXT NOT NULL DEFAULT '',
    claim_epoch       INTEGER NOT NULL,
    owner_instance_id TEXT NOT NULL,
    attempt_id        INTEGER NOT NULL DEFAULT 1,
    intent_seq        INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- call_id 是**本次 Run 内的逻辑身份**。唯一约束加在 (run_id, call_id) 上：
-- provider 给的短 ID 在不同 run 之间会重名，跨 run 唯一会误伤别的运行。
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatches_call
    ON dispatches(run_id, call_id);
"""


class DispatchError(TianshuError):
    """派发层的编程/状态错误（请求不存在、重复派发、类型不对）。"""


class DispatchDenied(TianshuError):
    """这次派发不被允许。带一个可审计的规则编号。"""

    def __init__(self, reason: str, rule_id: str = "dispatch.denied") -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_id = rule_id


@dataclass(frozen=True)
class DispatchDecision:
    kind: str  # waiting | admitted | rejected
    request_id: str
    request_digest: str
    approval_id: str | None = None
    reason: str = ""
    rule_id: str = ""

    @property
    def is_waiting(self) -> bool:
        return self.kind == KIND_WAITING

    @property
    def is_admitted(self) -> bool:
        return self.kind == KIND_ADMITTED

    @property
    def is_rejected(self) -> bool:
        return self.kind == KIND_REJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "approval_id": self.approval_id,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True)
class StoredRequest:
    request: ActionRequest
    args: dict[str, Any]
    state: str
    reason: str
    rule_id: str
    approval_id: str | None


@dataclass(frozen=True)
class ControlRun:
    """undo / recover 这类"用户直接发起的写"的控制 Run。

    它有自己的 Task、Run 和 JobClaim，不借用被撤销那个历史 Run 的
    已经失效的身份；同时把"操作的是哪个历史 Run"写在目标里。
    """

    task: Task
    run: Run
    claim: JobClaim
    lease_paths: tuple[str, ...]


@dataclass(frozen=True)
class _Authority:
    run: Run
    task: Task
    bindings: list[WorkspaceBinding]
    resources: list[ResourceIdentity]
    lease_paths: list[str]


@dataclass(frozen=True)
class _Target:
    path: Path
    rel_path: str
    workspace_root: str
    identity: ResourceIdentity


def _now() -> int:
    return int(time.time() * 1000)


class Dispatcher:
    """唯一判定"能不能执行"，并登记执行的地方。"""

    def __init__(
        self,
        log: EventLog,
        tasks: TaskStore,
        leases: LeaseStore,
        policy: PolicyEngine,
        approvals: ApprovalStore,
        executor: FileExecutor | None = None,
        *,
        grants: GrantStore | None = None,
        claims: JobStore | None = None,
        snapshots: SnapshotStore | None = None,
        data_root: Path | None = None,
        clock=_now,
        guard_timeout_ms: int = GUARD_TIMEOUT_MS,
    ) -> None:
        if log.in_transaction:
            raise TianshuError("Dispatcher 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._tasks = tasks
        self._leases = leases
        self._policy = policy
        self._approvals = approvals
        self._grants = grants or GrantStore(log, clock=clock)
        self._claims = claims or tasks.claims
        if self._claims is None:
            raise TianshuError(
                "Dispatcher 需要 JobStore：派发必须绑定当前 owner 的 claim，"
                "不能用裸 run_id 发起写入"
            )
        self._snapshots = snapshots
        self._data_root = Path(data_root) if data_root is not None else tasks.data_root
        self._clock = clock
        self._guard_timeout_ms = guard_timeout_ms
        self._executor = executor or FileExecutor(
            tasks=tasks,
            leases=leases,
            grants=self._grants,
            approvals=approvals,
            claims=self._claims,
            dispatch_lookup=self.dispatch_record,
            dispatch_mark=self.mark_dispatch,
            clock=clock,
            log=log,
            snapshots=snapshots,
        )

    # ---- 只读查询 ----

    @property
    def log(self) -> EventLog:
        return self._log

    @property
    def claims(self) -> JobStore:
        return self._claims

    def dispatch_record(self, run_id: str, call_id: str) -> DispatchRecord | None:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE run_id=? AND call_id=?",
            (run_id, call_id),
        ).fetchone()
        return _row_to_dispatch(row) if row is not None else None

    def get_request(self, request_id: str) -> StoredRequest | None:
        row = self._conn.execute(
            "SELECT * FROM dispatch_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        return StoredRequest(
            request=ActionRequest.of(json.loads(row["payload"])),
            args=json.loads(row["args_payload"]),
            state=row["state"],
            reason=row["reason"],
            rule_id=row["rule_id"],
            approval_id=row["approval_id"],
        )

    def request_ids_for_run(self, run_id: str) -> list[str]:
        """这个 Run 签发过的请求号（按时间顺序）。

        恢复时要用它回答一个问题："同一个动作是不是已经有一张请求/审批在库里
        了？" 只靠游标不够——崩溃窗口里游标可能还没记下请求号。
        """
        rows = self._conn.execute(
            "SELECT request_id FROM dispatch_requests WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [r["request_id"] for r in rows]

    def waiting(self, *, run_id: str | None = None) -> list[StoredRequest]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_requests WHERE state=? ORDER BY created_at",
                (STATE_WAITING,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_requests"
                " WHERE state=? AND run_id=? ORDER BY created_at",
                (STATE_WAITING, run_id),
            ).fetchall()
        out = []
        for row in rows:
            stored = self.get_request(row["request_id"])
            if stored is not None:
                out.append(stored)
        return out

    # ---- 阶段一：判定 ----

    def prepare(
        self, request: ActionRequest, claim: JobClaim, *, args: dict[str, Any]
    ) -> DispatchDecision:
        """判定这次动作该怎么走：直接放行、等人工批准、还是拒绝。

        这里**不写文件**，也不消费审批。可以反复调用（幂等）：同一个摘要上已经
        有一张 pending/approved 的审批就复用它，不会给用户刷出一堆待批项。
        """
        self._typecheck(request, claim, args)
        if sha256_hex(canonical_bytes(args)) != request.args_hash:
            raise DispatchError("args 与 request.args_hash 不符（请求被改过）")

        try:
            auth = self._authority(request, claim)
            target = self._resolve_target(request, auth)
            ok, reason = check_request_preconditions(
                request.action, target.path, request.preconditions or {}
            )
            if not ok:
                # "新建但目标已存在"不是"状态过期"，是"这个工具不做这件事"：
                # 两者对用户的下一步动作含义完全不同（前者重试/重批，后者换工具）。
                rule = (
                    "dispatch.create_over_existing"
                    if request.action == ACTION_CREATE and target.path.exists()
                    else "dispatch.stale_precondition"
                )
                raise DispatchDenied(reason, rule)
        except DispatchDenied as exc:
            return self._reject(request, args, exc.reason, exc.rule_id, event=DISPATCH_REJECTED)

        decision = self._policy.classify(
            request.action,
            list(request.resources),
            auth.bindings,
            request.preconditions or {},
        )

        if decision.is_denied:
            return self._reject(
                request, args, decision.reason, decision.rule_id, event=POLICY_DENIED
            )

        if decision.is_auto:
            self._store_request(request, args, STATE_ADMITTED, decision.reason, decision.rule_id)
            self._append(
                request,
                DISPATCH_REQUESTED,
                {
                    "request_id": request.request_id,
                    "request_digest": request.digest(),
                    "action_digest": request.action_digest(),
                    "action": request.action,
                    "resources": list(request.resources),
                    "decision": "auto",
                    "rule_id": decision.rule_id,
                },
            )
            return DispatchDecision(
                KIND_ADMITTED,
                request.request_id,
                request.digest(),
                reason=decision.reason,
                rule_id=decision.rule_id,
            )

        approval = self._approvals.find_for(request)
        if (
            approval is not None
            and approval.state == "approved"
            and approval.expires_at <= self._clock()
        ):
            # 已批准但审批本身过期了：作废它，重新发起，别让用户对着一个不能用的
            # 批准反复重试。
            self._approvals.expire(approval.approval_id)
            approval = None

        if approval is None:
            approval = self._approvals.request(request, decision)

        state = STATE_WAITING if approval.state == "pending" else STATE_ADMITTED
        self._store_request(
            request,
            args,
            state,
            decision.reason,
            decision.rule_id,
            approval_id=approval.approval_id,
        )
        self._append(
            request,
            DISPATCH_REQUESTED,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "action": request.action,
                "resources": list(request.resources),
                "decision": decision.kind,
                "rule_id": decision.rule_id,
                "approval_id": approval.approval_id,
                "approval_state": approval.state,
            },
        )
        if state == STATE_WAITING:
            return DispatchDecision(
                KIND_WAITING,
                request.request_id,
                request.digest(),
                approval_id=approval.approval_id,
                reason=decision.reason,
                rule_id=decision.rule_id,
            )
        return DispatchDecision(
            KIND_ADMITTED,
            request.request_id,
            request.digest(),
            approval_id=approval.approval_id,
            reason=decision.reason,
            rule_id=decision.rule_id,
        )

    # ---- 阶段二：派发 ----

    def dispatch(self, request_id: str, claim: JobClaim) -> ToolResult:
        """消费审批、登记 intent、在守卫内执行。

        返回的 `ToolResult` 里，`effect_state=none_applied` 表示"一个字节都没动"；
        `unknown` 表示"可能动了，去 recover"。任何不是这两种的结论都必须有证据。
        """
        if not isinstance(claim, JobClaim):
            raise DispatchError("必须传 JobClaim；不能用裸 run_id 派发")

        stored = self.get_request(request_id)
        if stored is None:
            raise DispatchError(f"派发请求不存在：{request_id}")
        request, args = stored.request, stored.args

        if stored.state == STATE_REJECTED:
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": stored.reason,
                    "rule_id": stored.rule_id,
                    "request_id": request_id,
                },
            )

        # ---- 阶段一：同一事务里核验 + 消费审批 + 签发票据 + 登记 intent ----
        try:
            with self._log.transaction() as tx:
                auth = self._authority(request, claim)
                target = self._resolve_target(request, auth)

                existing = self.dispatch_record(request.run_id, request.call_id)
                if existing is not None:
                    # 同一个 call_id 换个参数再来一次：唯一索引会挡住双写，
                    # 但那是 `IntegrityError` 从 INSERT 里炸出来，调用方只能
                    # 看到"主循环异常"。这里显式区分两种拒绝原因。
                    if existing.request_digest != request.digest():
                        raise DispatchDenied(
                            f"call_id 已被另一次请求占用：{request.call_id}"
                            f"（已有 state={existing.state}）",
                            D_DISPATCH_CALL_ID_REUSED,
                        )
                    raise DispatchError(
                        f"这次调用已经派发过：call_id={request.call_id}"
                        f"（state={existing.state}）"
                    )

                ok, reason = check_request_preconditions(
                    request.action, target.path, request.preconditions or {}
                )
                if not ok:
                    raise DispatchDenied(reason, "dispatch.stale_precondition")

                approval_id, grant_id = self._consume_authority(request, auth, stored)
                dispatch_id = self._dispatch_id(request)

                seq = tx.append(
                    request.run_id,
                    "tool.intent",
                    {
                        "call_id": request.call_id,
                        "tool_id": request.tool_id,
                        "args": args,
                        "args_hash": request.args_hash,
                        "request_id": request.request_id,
                        "request_digest": request.digest(),
                        "dispatch_id": dispatch_id,
                        "attempt_id": request.attempt_id,
                        "approval_id": approval_id,
                        "grant_id": grant_id,
                        "claim_epoch": request.claim_epoch,
                    },
                    task_id=request.task_id,
                    actor_kind="core",
                    idem_key=request.call_id,
                )
                self._insert_dispatch(
                    request, auth, target, seq, approval_id, grant_id
                )  # dispatch_id 由 _dispatch_id 稳定推导，与 intent 里的一致
                self._set_request_state(
                    request.request_id, STATE_ADMITTED, approval_id=approval_id
                )
                self._append(
                    request,
                    DISPATCH_ADMITTED,
                    {
                        "request_id": request.request_id,
                        "request_digest": request.digest(),
                        "call_id": request.call_id,
                        "approval_id": approval_id,
                        "grant_id": grant_id,
                        "intent_seq": seq,
                    },
                )
        except DispatchDenied as exc:
            self._reject(request, args, exc.reason, exc.rule_id, event=DISPATCH_REJECTED)
            return ToolResult(
                "rejected",
                "none_applied",
                {"error": exc.reason, "rule_id": exc.rule_id, "request_id": request_id},
            )
        except JobLost as exc:
            return self._reject_runtime(request, exc, "dispatch.claim_lost")
        except LeaseLost as exc:
            return self._reject_runtime(request, exc, "dispatch.lease_lost")
        except ApprovalError as exc:
            return self._reject_runtime(request, exc, "dispatch.approval_unusable")
        except WorkspaceBlocked as exc:
            return self._reject_runtime(request, exc, "dispatch.workspace_blocked")

        # intent / 审批消费 / grant 都已提交，文件一个字节都没动。
        failpoint("after_intent")
        admitted = self._admitted(request, claim, target, seq, approval_id, grant_id)
        dispatch_id = admitted.dispatch_id

        # ---- 阶段二：守卫内再核一遍，然后才真正写 ----
        try:
            with self._tasks.begin_execution(
                self._data_root, request.run_id, auth.lease_paths
            ) as permit:
                result = self._executor.execute(admitted, permit)
        except (
            DispatchDenied,
            ToolNotAllowed,
            RunFenced,
            JobLost,
            LeaseLost,
            GuardTimeout,
            ApprovalError,
            WorkspaceBlocked,
            OverwriteRefused,
            ToolArgumentError,
            WorkspaceViolation,
        ) as exc:
            # 全部发生在"打开文件之前"：拒绝是确定的，一个字节都没动
            return self._abort_dispatch(request, dispatch_id, exc)
        except Exception as exc:  # noqa: BLE001 - 写入阶段炸了：效果未知
            # 已经进入执行、intent 已提交，抛异常说明"可能写了一半"。
            # 这种情况唯一诚实的结论是 unknown，并且立刻阻断这个工作区的后续
            # 写入——否则下一个人会在一个状态不明的目录上继续改。
            return self._fail_unknown(request, dispatch_id, target, exc)

        self._commit_result(request, dispatch_id, result)
        # 结果与凭据已经同事务落库。M0 时代"结果后"和"凭据后"是两个故障点，
        # M2 把两件事合成一次提交，所以这两个点落在同一处：能证明的仍然是
        # "重放不会补出第二条效果"。
        failpoint("after_result")
        failpoint("after_receipt")
        return result

    # ---- 撤销 ----

    def revoke(
        self, approval_id: str, *, actor: HumanIdentity, reason: str = "revoked by human"
    ) -> Approval:
        """撤销一次批准。

        与执行抢同一把守卫：撤销先拿到锁，就把还没开始执行的那次派发作废；
        执行先拿到锁并写下 `execution.started`，这次调用已经是 in-flight，
        撤销会明确报错而不是假装成功。
        """
        if not isinstance(actor, HumanIdentity):
            raise ApprovalError("撤销人必须是 HumanIdentity（只能由本地 CLI 构造）")
        root = self._require_data_root()
        with execution_guard(root, timeout_ms=self._guard_timeout_ms):
            with self._log.transaction():
                current = self._approvals.get(approval_id)
                if current is None:
                    raise ApprovalError(f"审批不存在：{approval_id}")
                if current.state in ("pending", "approved"):
                    return self._approvals.revoke(approval_id, actor=actor)
                if current.state != "consumed":
                    raise ApprovalError(f"不能撤销这个状态：{current.state}")

                row = self._conn.execute(
                    "SELECT * FROM dispatches WHERE approval_id=?", (approval_id,)
                ).fetchone()
                if row is not None and row["state"] != DISPATCH_INTENT:
                    raise ApprovalError(
                        f"这次调用已经开始执行（state={row['state']}），"
                        "撤销不承诺回滚；请用 recover 核查实际效果"
                    )
                revoked = self._approvals.revoke_consumed(
                    approval_id, actor=actor, reason=reason
                )
                if row is not None:
                    self._conn.execute(
                        "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                        " WHERE dispatch_id=?",
                        (DISPATCH_ROW_REVOKED, reason, self._clock(), row["dispatch_id"]),
                    )
                    self._log.append(
                        row["run_id"],
                        DISPATCH_REVOKED,
                        {
                            "dispatch_id": row["dispatch_id"],
                            "call_id": row["call_id"],
                            "approval_id": approval_id,
                            "reason": reason,
                        },
                        task_id=row["task_id"],
                        actor_kind="human",
                        actor_id=actor.actor,
                    )
        return revoked

    # ---- 控制 Run（undo / recover 的写入口） ----

    def open_control_run(
        self, *, goal: str, workspaces: list[WorkspaceBinding | str | dict[str, Any]]
    ) -> ControlRun:
        """为"用户直接发起的写"建立自己的 Task/Run/JobClaim。

        不借用历史 Run 的身份：那个 Run 早就结束了，它的 revision/epoch/claim
        都不再有效。撤销操作用它自己的身份，并把"撤销的是谁"写进目标里。
        """
        goal = (goal or "").strip()
        if not goal:
            raise DispatchError("控制 Run 的目标不能为空")
        task = self._tasks.create_task(goal, workspaces)
        run = self._tasks.start_run(task.task_id)
        writable = [b.identity() for b in task.workspaces if b.can_write]
        if not writable:
            raise DispatchError("控制 Run 需要至少一个可写绑定目录")
        self._leases.acquire(writable, run.run_id)
        job = self._claims.enqueue(run.run_id)
        claim = self._claims.claim(job.job_id)
        run = self._tasks.transition(run.run_id, RUN_RUNNING, claim=claim,
                                     reason="control_run")
        return ControlRun(
            task=task,
            run=run,
            claim=claim,
            lease_paths=tuple(str(b.root_path) for b in task.workspaces if b.can_write),
        )

    def close_control_run(self, control: ControlRun, *, outcome: str = "succeeded",
                          effect_state: str = "none_applied") -> None:
        self._leases.release_run(control.run.run_id)
        self._tasks.finish_run(
            control.run.run_id, outcome=outcome, effect_state=effect_state
        )
        try:
            self._claims.finish(control.claim, outcome=outcome)
        except (JobLost, TianshuError):
            # 作业已经被别人收尾：控制 Run 的结论仍以 run.finish 为准
            pass

    # ---- 内部：核验 ----

    def _typecheck(
        self, request: ActionRequest, claim: JobClaim, args: dict[str, Any]
    ) -> None:
        if not isinstance(request, ActionRequest):
            raise DispatchError("request 必须是 ActionRequest")
        if not isinstance(claim, JobClaim):
            raise DispatchError("必须传 JobClaim")
        if not isinstance(args, dict):
            raise DispatchError("args 必须是对象")
        if request.action not in APPROVAL_ACTIONS and request.action != ACTION_CREATE:
            raise DispatchError(
                f"只读动作不经过派发（没有可授权的副作用）：{request.action}"
            )
        if request.tool_id not in SPECS:
            raise DispatchError(f"工具不在白名单：{request.tool_id}")

    def _authority(self, request: ActionRequest, claim: JobClaim) -> _Authority:
        if claim.run_id != request.run_id:
            raise DispatchDenied(
                f"claim 属于另一个 Run：claim.run_id={claim.run_id}，"
                f"request.run_id={request.run_id}",
                "dispatch.claim_mismatch",
            )
        if (
            claim.owner_instance_id != request.owner_instance_id
            or claim.claim_epoch != request.claim_epoch
        ):
            raise DispatchDenied(
                "请求绑定的 owner/claim_epoch 与当前 claim 不符",
                "dispatch.claim_mismatch",
            )
        if self._leases.identity.instance_id != claim.owner_instance_id:
            raise DispatchDenied(
                "claim 的 owner 不是本进程实例：不能替别的实例派发写入",
                "dispatch.foreign_instance",
            )
        try:
            self._claims.assert_claim(claim)
        except JobLost as exc:
            raise DispatchDenied(f"claim 已失效：{exc}", "dispatch.claim_lost") from exc

        try:
            run = self._tasks.assert_current(request.run_id)
        except RunFenced as exc:
            raise DispatchDenied(f"Run 不可写：{exc}", "dispatch.run_fenced") from exc
        if run.task_id != request.task_id:
            raise DispatchDenied(
                f"Run 不属于这个 Task：{run.task_id} ≠ {request.task_id}",
                "dispatch.task_mismatch",
            )
        if run.state != RUN_RUNNING:
            # PAUSED / WAITING_APPROVAL 的 Run 手里可能还拿着有效租约和 claim
            # （等待审批期间必须续租，否则每轮 TTL 都要重新审批），但它们
            # 不该执行。这条判断如果只放在主循环里，绕过主循环直接派发就能写。
            raise DispatchDenied(
                f"run 不在执行状态，拒绝派发：{request.run_id}（state={run.state}）",
                "dispatch.not_running",
            )
        if (run.goal_revision, run.lease_epoch) != (
            request.goal_revision,
            request.lease_epoch,
        ):
            raise DispatchDenied(
                "请求绑定的 revision/epoch 已过期"
                f"（当前 {run.goal_revision}/{run.lease_epoch}，"
                f"请求 {request.goal_revision}/{request.lease_epoch}）",
                "dispatch.stale_revision",
            )

        task = self._tasks.get_task(request.task_id)
        if task is None:
            raise DispatchDenied(f"task 不存在：{request.task_id}", "dispatch.no_task")
        if not request.resources:
            raise DispatchDenied("动作没有声明资源", "c4.no_resource")

        bindings = list(task.workspaces or [])
        resources = [ResourceIdentity.of(Path(r)) for r in request.resources]

        try:
            self._leases.assert_covers(resources, request.run_id)
        except LeaseLost as exc:
            # 只读绑定永远不会有写租约，所以"租约不覆盖"在这里几乎是必然的。
            # 但那句话对用户没有意义——真实原因是"这个目录你只让我读"。
            # 判定用的仍是同一个 `_covers`，不是另写一套路径规则。
            readonly = [
                r
                for r in request.resources
                if any(PolicyEngine._covers(b, r) and not b.can_write for b in bindings)
                and not any(
                    PolicyEngine._covers(b, r) and b.can_write for b in bindings
                )
            ]
            if readonly:
                raise DispatchDenied(
                    f"目标目录是只读绑定：{readonly[0]}", "c4.readonly_binding"
                ) from exc
            raise DispatchDenied(
                f"资源租约不覆盖目标：{exc}", "dispatch.lease_lost"
            ) from exc

        lease_paths = self._leases.held_paths(request.run_id)
        if not lease_paths:
            raise DispatchDenied("本实例没有持有任何租约", "dispatch.lease_lost")

        # 阻断是"这个目录有无法核实的副作用"，按目录查而不是按文件查
        for resource in request.resources:
            for binding in bindings:
                if PolicyEngine._covers(binding, resource):
                    try:
                        ensure_not_blocked(self._log, binding.root_path)
                    except WorkspaceBlocked as exc:
                        raise DispatchDenied(
                            str(exc), "dispatch.workspace_blocked"
                        ) from exc

        return _Authority(run, task, bindings, resources, lease_paths)

    def _resolve_target(self, request: ActionRequest, auth: _Authority) -> _Target:
        if len(request.resources) != 1:
            raise DispatchDenied(
                "文件动作只支持单个资源（多资源一次写入无法给出单一 preimage 边界）",
                "c4.multi_resource",
            )
        target = Path(request.resources[0]).resolve()

        covering = [
            b for b in auth.bindings if PolicyEngine._covers(b, str(target))
        ]
        writable = [b for b in covering if b.can_write]
        candidates = writable
        if not candidates and covering:
            # 目标在绑定目录内但只能读：让策略层去拒绝（c4.readonly_binding），
            # 而不是含糊地报成"目录外"。
            candidates = covering
        if not candidates:
            # 策略层对目录外访问给的是"需审批"，但 M2 还没有目录外的证据边界
            # （证据按 workspace 根存 manifest），所以执行侧明确拒绝而不是假装支持。
            raise DispatchDenied(
                f"暂不支持目录外文件写入（M2 只支持绑定目录内）：{target}",
                "c4.outside_workspace_unsupported",
            )
        binding = max(candidates, key=lambda b: len(overlap_key(b.root_path)))
        ws = Workspace(Path(binding.root_path))
        rel = ws.relative(target)
        if request.action == ACTION_CREATE and not target.parent.is_dir():
            raise DispatchDenied(
                f"目标父目录不存在：{target.parent}", "dispatch.no_parent"
            )
        return _Target(target, rel, str(ws.root), ResourceIdentity.of(target))

    def _consume_authority(
        self, request: ActionRequest, auth: _Authority, stored: StoredRequest
    ) -> tuple[str | None, str]:
        """消费审批（如果有）并签发+消费一次性 grant。必须在派发事务内调用。"""
        digest = request.digest()
        action = request.action_digest()
        approval_id: str | None = None
        issuer = "policy:auto"

        if request.action in APPROVAL_ACTIONS:
            approval = (
                self._approvals.get(stored.approval_id)
                if stored.approval_id
                else self._approvals.find_for(request)
            )
            if approval is None:
                raise DispatchDenied(
                    "没有可用的批准（请先 approvals approve）", "c4.no_approval"
                )
            if approval.run_id != request.run_id:
                raise DispatchDenied(
                    "审批属于另一个 Run", "c4.approval_run_mismatch"
                )
            if approval.action_digest and approval.action_digest != action:
                # 审批对的是另一个动作（换了参数/目标/revision/绑定）。这里必须
                # 自己比一次，而不能只信"按动作摘要查找到的那一张"——旧库里的
                # 审批没有动作摘要，回退路径靠的仍是完整请求摘要。
                raise DispatchDenied(
                    "审批绑定的动作与本次请求不符", "c4.approval_mismatch"
                )
            if approval.state != "approved":
                raise DispatchDenied(
                    f"审批状态不是已批准：{approval.state}", "c4.approval_state"
                )
            if approval.expires_at <= self._clock():
                # 已批准但自己过期了：与"没批准"不同，要说清楚是过期。
                raise DispatchDenied(
                    "审批已过期（请重新发起）", "c4.approval_expired"
                )
            self._approvals.consume(
                approval.approval_id, action_digest=action, request_digest=digest
            )
            approval_id = approval.approval_id
            issuer = f"approval:{approval.decided_by or 'unknown'}"

        lease_expires = self._lease_deadline(auth.lease_paths, request.run_id)
        subject = f"{request.run_id}#{request.claim_epoch}"
        grant = self._grants.issue(
            subject=subject,
            request=request,
            issuer=issuer,
            max_expires_at=lease_expires,
        )
        self._grants.consume(
            grant.grant_id, request_digest=digest, subject=subject
        )
        return approval_id, grant.grant_id

    def _lease_deadline(self, lease_paths: list[str], run_id: str) -> int | None:
        expiries = []
        for path in lease_paths:
            lease = self._leases.holder(Path(path))
            if lease is not None and lease.run_id == run_id:
                expiries.append(lease.expires_at)
        return min(expiries) if expiries else None

    # ---- 内部：登记 ----

    def _insert_dispatch(
        self,
        request: ActionRequest,
        auth: _Authority,
        target: _Target,
        seq: int,
        approval_id: str | None,
        grant_id: str,
    ) -> None:
        now = self._clock()
        self._conn.execute(
            "INSERT INTO dispatches (dispatch_id, run_id, task_id, call_id,"
            " request_id, request_digest, tool_id, action, resources, target,"
            " rel_path, workspace_root, resource_key, approval_id, grant_id,"
            " claim_epoch, owner_instance_id, attempt_id, intent_seq, state,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'intent',?,?)",
            (
                self._dispatch_id(request),
                request.run_id,
                request.task_id,
                request.call_id,
                request.request_id,
                request.digest(),
                request.tool_id,
                request.action,
                json.dumps(list(request.resources), ensure_ascii=False),
                str(target.path),
                target.rel_path,
                target.workspace_root,
                target.identity.key,
                approval_id,
                grant_id,
                request.claim_epoch,
                request.owner_instance_id,
                request.attempt_id,
                seq,
                now,
                now,
            ),
        )

    @staticmethod
    def _dispatch_id(request: ActionRequest) -> str:
        """派发记录的稳定身份。

        不能用裸 call_id：provider 给的短 ID（"c1"、"call_1"）在不同 Run 之间
        会重名，拿它当主键会让"另一个 run 的同名调用"互相覆盖。这里把 run 和
        call 一起哈希，既稳定（重算得出同一个值）又不会跨 run 误碰。
        """
        digest = sha256_hex(f"{request.run_id}:{request.call_id}".encode())
        return f"disp_{digest[:16]}"

    def mark_dispatch(self, run_id: str, call_id: str, state: str) -> None:
        """执行器把派发记录推进到 executing / done。"""
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=?"
                " WHERE run_id=? AND call_id=?",
                (state, self._clock(), run_id, call_id),
            )

    def _admitted(
        self,
        request: ActionRequest,
        claim: JobClaim,
        target: _Target,
        seq: int,
        approval_id: str | None,
        grant_id: str,
    ) -> AdmittedCall:
        stored = self.get_request(request.request_id)
        return AdmittedCall(
            dispatch_id=self._dispatch_id(request),
            request=request,
            request_digest=request.digest(),
            claim=claim,
            tool_id=request.tool_id,
            action=request.action,
            args=stored.args if stored is not None else {},
            target=str(target.path),
            rel_path=target.rel_path,
            workspace_root=target.workspace_root,
            resource_key=target.identity.key,
            resources=tuple(request.resources),
            intent_seq=seq,
            attempt_id=request.attempt_id,
            approval_id=approval_id,
            grant_id=grant_id,
        )

    def _store_request(
        self,
        request: ActionRequest,
        args: dict[str, Any],
        state: str,
        reason: str,
        rule_id: str,
        *,
        approval_id: str | None = None,
    ) -> None:
        payload = canonical_bytes(request.to_canonical_dict()).decode("utf-8")
        args_payload = canonical_bytes(args).decode("utf-8")
        now = self._clock()
        with self._log.transaction():
            self._conn.execute(
                "INSERT INTO dispatch_requests (request_id, task_id, run_id,"
                " request_digest, payload, args_payload, state, reason, rule_id,"
                " approval_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(request_id) DO UPDATE SET"
                " state=excluded.state, reason=excluded.reason,"
                " rule_id=excluded.rule_id, approval_id=excluded.approval_id,"
                " updated_at=excluded.updated_at",
                (
                    request.request_id,
                    request.task_id,
                    request.run_id,
                    request.digest(),
                    payload,
                    args_payload,
                    state,
                    reason,
                    rule_id,
                    approval_id,
                    now,
                    now,
                ),
            )

    def _set_request_state(
        self, request_id: str, state: str, *, approval_id: str | None = None
    ) -> None:
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatch_requests SET state=?, approval_id=COALESCE(?,"
                " approval_id), updated_at=? WHERE request_id=?",
                (state, approval_id, self._clock(), request_id),
            )

    def _append(self, request: ActionRequest, event: str, payload: dict[str, Any]) -> int:
        return self._log.append(
            request.run_id,
            event,
            payload,
            task_id=request.task_id,
            actor_kind="core",
        )

    def _reject(
        self,
        request: ActionRequest,
        args: dict[str, Any],
        reason: str,
        rule_id: str,
        *,
        event: str,
    ) -> DispatchDecision:
        self._store_request(request, args, STATE_REJECTED, reason, rule_id)
        self._append(
            request,
            event,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "action": request.action,
                "resources": list(request.resources),
                "reason": reason,
                "rule_id": rule_id,
            },
        )
        return DispatchDecision(
            KIND_REJECTED, request.request_id, request.digest(), reason=reason,
            rule_id=rule_id,
        )

    def _reject_runtime(
        self, request: ActionRequest, exc: BaseException, rule_id: str
    ) -> ToolResult:
        reason = f"{type(exc).__name__}: {exc}"
        self._set_request_state(request.request_id, STATE_REJECTED)
        self._append(
            request,
            DISPATCH_REJECTED,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "reason": reason,
                "rule_id": rule_id,
            },
        )
        return ToolResult(
            "rejected",
            "none_applied",
            {"error": reason, "rule_id": rule_id, "request_id": request.request_id},
        )

    def _abort_dispatch(
        self, request: ActionRequest, dispatch_id: str, exc: BaseException
    ) -> ToolResult:
        """守卫内复核没过：一个字节都没写，明确记成 rejected。"""
        reason = f"{type(exc).__name__}: {exc}"
        rule_id = {
            ApprovalError: "dispatch.approval_revoked",
            RunFenced: "dispatch.run_fenced",
            ToolNotAllowed: "dispatch.not_admitted",
            GuardTimeout: "dispatch.guard_timeout",
            JobLost: "dispatch.claim_lost",
            LeaseLost: "dispatch.lease_lost",
        }.get(type(exc), "dispatch.refused")

        with self._log.transaction():
            # 只在"还没人把它作废"时推进：撤销已经把它标成 revoked 的，
            # 不能被后到的拒绝改写成另一个结论。
            self._conn.execute(
                "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                " WHERE dispatch_id=? AND state=?",
                (
                    DISPATCH_ROW_REJECTED,
                    reason,
                    self._clock(),
                    dispatch_id,
                    DISPATCH_INTENT,
                ),
            )
            self._log.append(
                request.run_id,
                DISPATCH_REJECTED,
                {
                    "dispatch_id": dispatch_id,
                    "call_id": request.call_id,
                    "request_id": request.request_id,
                    "reason": reason,
                    "rule_id": rule_id,
                    "phase": "guard",
                },
                task_id=request.task_id,
                actor_kind="core",
            )
        # 审批/许可已经消费掉了（一次性），所以这次请求必须重新发起
        self._set_request_state(request.request_id, STATE_REJECTED)
        return ToolResult(
            "rejected",
            "none_applied",
            {"error": reason, "rule_id": rule_id, "request_id": request.request_id},
        )

    def _fail_unknown(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _Target,
        exc: BaseException,
    ) -> ToolResult:
        """执行中途炸了：结论只能是 unknown，并且立刻阻断后续写入。

        "没写成功"和"不知道写了没有"是两件事。后者必须挡住这个目录的后续
        写入，直到有人用 recover 核对过——否则下一个人是在一个状态不明的
        目录上继续改。
        """
        reason = f"{type(exc).__name__}: {exc}"
        result = ToolResult(
            "failed",
            "unknown",
            {"error": reason, "request_id": request.request_id},
        )
        batch = [
            {
                "run_id": request.run_id,
                "type": "tool.result",
                "payload": {
                    "call_id": request.call_id,
                    "tool_id": request.tool_id,
                    "status": result.status,
                    "effect_state": result.effect_state,
                    "output": result.output,
                },
                "task_id": request.task_id,
                "actor_kind": "tool",
                "idem_key": request.call_id,
            },
            {
                "run_id": request.run_id,
                "type": "recovery.blocked",
                "payload": {
                    "workspace": target.workspace_root,
                    "call_ids": [request.call_id],
                    "reason": f"执行中断且效果未知：{reason}",
                },
                "task_id": request.task_id,
                "actor_kind": "system",
            },
        ]
        self._log.append_many(batch)
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                " WHERE dispatch_id=?",
                (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
            )
            self._set_request_state(request.request_id, STATE_REJECTED)
        return result

    def _commit_result(
        self, request: ActionRequest, dispatch_id: str, result: ToolResult
    ) -> None:
        """结果与凭据同事务落库，然后把派发记录推进到 done。"""
        batch = [
            {
                "run_id": request.run_id,
                "type": "tool.result",
                "payload": {
                    "call_id": request.call_id,
                    "tool_id": request.tool_id,
                    "status": result.status,
                    "effect_state": result.effect_state,
                    "output": result.output,
                },
                "task_id": request.task_id,
                "actor_kind": "tool",
                "idem_key": request.call_id,
            }
        ]
        receipt = build_receipt(
            run_id=request.run_id,
            call_id=request.call_id,
            tool_id=request.tool_id,
            args_hash=request.args_hash,
            result=result,
        )
        if receipt is not None:
            batch.append(
                {
                    "run_id": request.run_id,
                    "type": "receipt",
                    "payload": receipt.to_payload(),
                    "task_id": request.task_id,
                    "actor_kind": "core",
                    "idem_key": receipt.receipt_id,
                }
            )
        self._log.append_many(batch)
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=? WHERE dispatch_id=?",
                (DISPATCH_FINISHED, self._clock(), dispatch_id),
            )
            self._set_request_state(request.request_id, STATE_DONE)

    def _require_data_root(self) -> Path:
        if self._data_root is None:
            raise TianshuError(
                "未配置 data_root：派发与撤销必须共用同一把执行守卫"
            )
        return Path(self._data_root)


# ---------------------------------------------------------------- 请求构造


def build_request(
    *,
    task: Task,
    run: Run,
    claim: JobClaim,
    call_id: str,
    tool_id: str,
    target: Path,
    args: dict[str, Any],
    request_id: str | None = None,
    attempt_id: int = 1,
) -> ActionRequest:
    """从"我要做什么"生成一份可派发的请求。

    `preconditions` 在这里**当场从磁盘读**，不是调用方说了算：
    审批绑定的必须是真实写前状态，否则"批准时改成 false"就能绕过覆盖审批。
    """
    spec = SPECS.get(tool_id)
    if spec is None:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")

    target = Path(target).resolve()
    if spec.action == ACTION_CREATE:
        preconditions: dict[str, Any] = {"target_exists": target.exists()}
    elif spec.action in (ACTION_OVERWRITE, ACTION_DELETE):
        preconditions = {"before_state": observe_state(target)}
    else:  # pragma: no cover - 只读动作不走派发
        preconditions = {}

    return ActionRequest(
        request_id=request_id or f"req_{uuid.uuid4().hex[:12]}",
        task_id=task.task_id,
        run_id=run.run_id,
        job_id=claim.job_id,
        claim_epoch=claim.claim_epoch,
        owner_instance_id=claim.owner_instance_id,
        goal_revision=run.goal_revision,
        lease_epoch=run.lease_epoch,
        call_id=call_id,
        attempt_id=attempt_id,
        tool_id=tool_id,
        tool_version=spec.version,
        schema_hash=sha256_hex(canonical_bytes(spec.input_schema)),
        action=spec.action,
        resources=(str(target),),
        args_hash=sha256_hex(canonical_bytes(args)),
        preconditions=preconditions,
    )


def _row_to_dispatch(row: sqlite3.Row) -> DispatchRecord:
    return DispatchRecord(
        dispatch_id=row["dispatch_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        call_id=row["call_id"],
        request_id=row["request_id"],
        request_digest=row["request_digest"],
        tool_id=row["tool_id"],
        action=row["action"],
        state=row["state"],
        claim_epoch=row["claim_epoch"],
        owner_instance_id=row["owner_instance_id"],
        attempt_id=row["attempt_id"],
        approval_id=row["approval_id"],
        grant_id=row["grant_id"],
    )


__all__ = [
    "ControlRun",
    "DISPATCH_ADMITTED",
    "DISPATCH_INTENT",
    "DISPATCH_REJECTED",
    "DISPATCH_REQUESTED",
    "DISPATCH_REVOKED",
    "DispatchDecision",
    "DispatchDenied",
    "DispatchError",
    "Dispatcher",
    "ExecutionGuard",
    "KIND_ADMITTED",
    "KIND_REJECTED",
    "KIND_WAITING",
    "PolicyDecision",
    "STATE_ADMITTED",
    "STATE_DONE",
    "STATE_REJECTED",
    "STATE_WAITING",
    "StoredRequest",
    "build_request",
    "is_ancestor_or_self",
]