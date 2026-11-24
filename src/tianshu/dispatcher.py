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

## 内部模块拆分（零契约变更）

- `_dispatcher_types.py`：封闭常量集、建表 Schema、异常、决策/目标值对象与前置条件辅助
- `_dispatcher_authority.py`：`_DispatcherAuthorityMixin`（身份、租约、策略、审批消费与目标解析）
- `_dispatcher_exec.py`：`_DispatcherExecMixin`（网络、MCP、子任务委派执行与接管/ControlRun）
- `_dispatcher_ledger.py`：`_DispatcherLedgerMixin`（派发表、外发边界、脱敏、拒绝与结果凭据提交）
- 本模块：保留 `Dispatcher` 门面（构造、只读查询、`prepare`/`dispatch`/`revoke`）、`build_request`
  及模块级可替换入口（`execution_guard`/`failpoint`/`net_fetch`/`require_isolation_requirements`），
  完整重导出全部公开及测试依赖符号。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from ._dispatcher_authority import _DispatcherAuthorityMixin
from ._dispatcher_exec import _DispatcherExecMixin
from ._dispatcher_ledger import _DispatcherLedgerMixin
from ._dispatcher_types import (
    _BACKFILL_MARKER,
    APPROVAL_REJECTED,
    APPROVAL_REJECTED_REASON,
    D_DISPATCH_CALL_ID_REUSED,
    DISPATCH_ADMITTED,
    DISPATCH_ERROR,
    DISPATCH_EXECUTING,
    DISPATCH_FINISHED,
    DISPATCH_INTENT,
    DISPATCH_REJECTED,
    DISPATCH_REQUESTED,
    DISPATCH_REVOKED,
    DISPATCH_ROW_REJECTED,
    DISPATCH_ROW_REVOKED,
    EXECUTABLE_KINDS,
    KIND_ADMITTED,
    KIND_REJECTED,
    KIND_WAITING,
    SCHEMA,
    STATE_ADMITTED,
    STATE_DONE,
    STATE_REJECTED,
    STATE_WAITING,
    ControlRun,
    DispatchDecision,
    DispatchDenied,
    DispatchError,
    StoredRequest,
    _audit_args,
    _Authority,
    _ensure_column,
    _FileTarget,
    _McpTarget,
    _MemoryTarget,
    _NetworkTarget,
    _now,
    _row_to_dispatch,
    _SubtaskTarget,
    _Target,
    precondition_rule_id,
    resolve_scheme_resource,
)
from .browser_forms import SUBMIT_TOOL
from .crashpoint import failpoint
from .disk_admission import DiskAdmissionError
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
from .exec_backends import ExecBackend
from .exec_backends import exec_backend as resolve_exec_backend
from .isolation import require_isolation_requirements
from .jobs import JobClaim, JobLost, JobStore
from .leases import (
    GUARD_TIMEOUT_MS,
    ExecutionGuard,
    GuardTimeout,
    LeaseLost,
    LeaseStore,
    execution_guard,
)
from .mcp import McpClients
from .memory_exec import MEMORY_ALL_TOOL_IDS, MemoryExecutor
from .net import NetLimits
from .net import fetch as net_fetch
from .permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EXEC,
    ACTION_OVERWRITE,
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
from .resources import (
    KIND_NETWORK,
    KIND_SUBTASK,
    ResourceError,
    parse_memory_uri,
    parse_resource,
)
from .rules import check_rule_id
from .sandbox import current_backend
from .snapshot import SnapshotStore
from .tasks import Run, Task, TaskStore
from .tool_registry import ToolRegistry, current_registry
from .tools import (
    NETWORK_TOOL_IDS,
    SUBTASK_TOOL_IDS,
    DispatchRecord,
    FileExecutor,
    ToolResult,
    check_request_preconditions,
    observe_state,
)
from .workspace import is_ancestor_or_self


class Dispatcher(
    _DispatcherAuthorityMixin, _DispatcherExecMixin, _DispatcherLedgerMixin
):
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
        memory: object | None = None,
        contents: object | None = None,
        net_limits: NetLimits | None = None,
        budget: object | None = None,
        exec_backend: ExecBackend | None = None,
        mcp_clients: McpClients | None = None,
        registry: ToolRegistry | None = None,
        clock=_now,
        guard_timeout_ms: int = GUARD_TIMEOUT_MS,
    ) -> None:
        if log.in_transaction:
            raise TianshuError("Dispatcher 不能在已打开的事务中构造")
        self._data_root = Path(data_root) if data_root is not None else tasks.data_root
        from .restore_access import preflight_legacy_material_layout

        preflight_legacy_material_layout(log.db_path, self._data_root)
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # 列不存在时**先**作废持久标记、再加列：`ALTER TABLE` 是 autocommit
        # 落盘的，若先 ALTER 再删标记，两条语句之间崩溃就会留下"列在、标记也
        # 在"的状态，下次打开看到标记直接跳过 → 旧终态正文永久漏脱敏。
        # 反过来先删标记：哪怕在删完之后、加列之前崩溃，下次打开列仍不存在、
        # 标记仍缺失，两条语句会再跑一遍（幂等）。正常升级路径（标记与列同批
        # 维护）里列本来就在，这段不执行。
        columns = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(dispatch_requests)"
            ).fetchall()
        }
        if "args_redacted" not in columns:
            # 反向边界：标记在、列却不在（迁移工具或人手把列删了）⇒ 旧终态行的
            # 正文又回到"未脱敏"，必须作废标记强制重扫，不然永久漏。
            self._conn.execute(
                "DELETE FROM dispatch_meta WHERE key = ?", (_BACKFILL_MARKER,)
            )
        _ensure_column(
            self._conn, "dispatch_requests", "args_redacted",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # 外发边界协议标记同理：`ALTER TABLE` 只给**已有的旧行**补 0，
        # 新行由 `_insert_dispatch()` 显式写 1。恢复据此把旧行排除在
        # 「确定没发出去」之外（旧版本写请求前不推进派发行，旧行的状态不能证明
        # 请求没写出——这是升级兼容的硬边界，不是可选优化）。
        _ensure_column(
            self._conn, "dispatches", "boundary_protocol",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # 用持久标记判"补扫做过没有"：中途被 kill / 抛错时标记不会置位，
        # 下次打开会重试（否则列已存在，靠列存在性判断就永远不补了）。
        self._migrate_redacted_args()
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

        self._clock = clock
        self._guard_timeout_ms = guard_timeout_ms
        # 记忆投影必须与事件日志共用同一个 SQLite 连接：投影更新要放进
        # `_commit_result()` 的同一个事务，另开连接就等于另开事实源。
        self._memory = memory
        # 内容引用（PRD §5.2.3）：网络结果必须有界落盘并给引用，不能把大 body
        # 直接塞进事件。与记忆同一条理由：它必须和事件在同一个 SQLite 里，
        # 否则"事件在、内容不在"没有裁决者。
        self._contents = contents
        if (
            contents is not None
            and getattr(contents, "_conn", None) is not None
            and getattr(contents, "_conn", None) is not self._conn
        ):
            raise TianshuError(
                "Dispatcher 收到的内容存储不属于同一个 SQLite 连接："
                "内容引用必须和事件同生共死"
            )
        # 网络抓取上限（超时 / 字节 / 跳数）：默认模块常量，测试可注入。
        self._net_limits = net_limits or NetLimits()
        # MCP 客户端（G2 MCP 阶段）：进程级、新 effects 预检通过后启动；None = 没启用
        # MCP（工具面与旧版逐字节相同）。
        self._mcp_clients = mcp_clients
        self._mcp_prepared = bool(getattr(mcp_clients, "_started", False))
        # 工具注册表：显示传进来的优先；None = 每次现取进程级注册表（与
        # `exec_backend()` 同一套"默认进程级 + 可显式注入"的模式，测试与
        # 多实例装配都靠它，而不是让构造顺序决定用哪张表）。
        self._registry = registry
        # 共享预算账本（PRD §5.6）：网络抓取按 `bytes` 维度预占/结算。
        self._budget = budget
        # `MemoryProjection` 构造处的检查只保证"它自己和一个日志同连接"，
        # 管不了派发方传进来的是**哪个**日志的投影。那样 `_commit_result()`
        # 会在 log 的事务里写事件、却在另一条连接上独立提交投影——一边回滚
        # 一边留下，两份事实源静默分叉，而且没有任何裁决者能判定哪份对。
        # 约束必须在边界上强制，不能靠"装配代码记得传对"。
        if memory is not None and getattr(memory, "conn", None) is not self._conn:
            raise TianshuError(
                "Dispatcher 收到的记忆投影不属于同一个 SQLite 连接："
                "投影必须和事件在同一个事务里落库，否则事件与投影会分叉"
            )
        self._memory_executor: MemoryExecutor | None = None
        # 进程级解析一次：预检与真正的执行必须看到同一个后端（同一份能力声明），
        # 否则会出现"预检说能做、执行说做不到"的分裂。
        self._exec_backend = (
            exec_backend if exec_backend is not None else resolve_exec_backend()
        )
        if executor is not None:
            # 自定义 executor 也让"预检看到的后端"与"执行看到的后端"必须同一个：
            # 只核默认构造等于给旁路留门。数据根同理——审批 probe 与执行
            # probe 必须是同一个现场。
            executor_data_root = getattr(executor, "data_root", None)
            same_root = executor_data_root == self._data_root
            if not same_root and (
                executor_data_root is not None and self._data_root is not None
            ):
                same_root = (
                    Path(executor_data_root).resolve()
                    == Path(self._data_root).resolve()
                )
            if not same_root:
                raise TianshuError(
                    "Dispatcher 收到的执行器数据根与派发层不一致："
                    f"{executor_data_root} vs {self._data_root}"
                    "（隔离证据必须来自同一个探测现场）"
                )
            executor_backend = getattr(executor, "exec_backend", None)
            if executor_backend is not self._exec_backend:
                raise TianshuError(
                    "Dispatcher 收到的执行器与派发层使用的执行后端不是同一个："
                    f"{getattr(executor_backend, 'capabilities', None)!r} vs "
                    f"{self._exec_backend.capabilities!r}"
                    "（预检与执行必须看到同一个后端）"
                )
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
            exec_backend=self._exec_backend,
            data_root=self._data_root,
        )
        if snapshots is not None and hasattr(snapshots, "_bind_owner_log"):
            snapshots._bind_owner_log(log)

    # ---- 只读查询 ----

    @property
    def log(self) -> EventLog:
        return self._log

    @property
    def claims(self) -> JobStore:
        return self._claims

    def _memory_projection(self) -> object:
        """拿到与事件日志同库的记忆投影。

        延迟构造：没有记忆请求的运行不需要加载记忆模块，也不改变既有启动路径。
        `memory.py` 由队友实现；这里只调用锚定签名，不复制一份实现。
        """
        if self._memory is None:
            try:
                from .memory import MemoryProjection
            except ImportError as exc:  # pragma: no cover - 集成期缺模块
                raise TianshuError(
                    "收到 memory 资源，但记忆投影不可用（缺少 tianshu.memory）"
                ) from exc
            # **不**显式传 blob_dir：正文目录由 `MemoryProjection` 自己的规则
            # 决定（DB 旁的 `memory_blobs/`）。正文与事件 payload 的保留策略
            # 不同（前者将来要能按 F10 §10 物理擦除，后者必须留存供 replay），
            # 共用 `log.blob_dir` 会让按策略清理误伤日志材料。所有装配点都
            # 不传，正文目录才有唯一来源。
            from .restore_access import default_material_paths

            self._memory = MemoryProjection(
                self._conn,
                log=self._log,
                blob_dir=default_material_paths(self._log.db_path, self._data_root)[2],
                data_root=self._data_root,
                clock=self._clock,
            )
            self._memory.ensure_schema()
        elif not getattr(self, "_memory_ready", False):
            self._memory.ensure_schema()
        self._memory_ready = True
        if self._memory_executor is None:
            self._memory_executor = MemoryExecutor(
                tasks=self._tasks,
                approvals=self._approvals,
                grants=self._grants,
                claims=self._claims,
                projection=self._memory,
                dispatch_lookup=self.dispatch_record,
                dispatch_mark=self.mark_dispatch,
                clock=self._clock,
                log=self._log,
            )
        return self._memory

    def _memory_executor_for(self) -> MemoryExecutor:
        self._memory_projection()
        assert self._memory_executor is not None
        return self._memory_executor

    @property
    def registry(self) -> ToolRegistry:
        """当前生效的工具注册表（内置 + 已准入 MCP）。"""
        return self._registry if self._registry is not None else current_registry()

    def dispatch_record(self, run_id: str, call_id: str) -> DispatchRecord | None:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE run_id=? AND call_id=?",
            (run_id, call_id),
        ).fetchone()
        return _row_to_dispatch(row) if row is not None else None

    def _dispatched_result(self, request: ActionRequest) -> ToolResult | None:
        """读取同一次调用已提交的结果，供幂等重放原样返回。"""
        events = self._log.read_run(request.run_id, event_types=("tool.result",))
        for event in reversed(events):
            payload = event.payload
            if (
                payload.get("call_id") == request.call_id
                and payload.get("tool_id") == request.tool_id
            ):
                return ToolResult(
                    payload["status"], payload["effect_state"], payload["output"]
                )
        return None

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

        isolation: dict[str, Any] | None = None
        command: dict[str, Any] | None = None
        try:
            auth = self._authority(request, claim, args=args)
            target = self._resolve_target(request, auth)
            self._check_run_execution(request, target)
            if isinstance(target, _MemoryTarget):
                # 记忆前置条件是 status/revision；文件分支保持原样，连调用
                # 顺序都不改（file 的 digest/approval 行为不能因记忆上线变化）。
                self._check_memory_preconditions(request, target, args)
            elif isinstance(target, (_NetworkTarget, _SubtaskTarget)):
                # 网络没有"写前状态"：没有本地副作用，也没有可撤销内容；
                # 子任务同样没有文件写前状态（身份/限额在 `_authority` 判完）。
                pass
            elif isinstance(target, _McpTarget):
                # MCP 没有本地写前状态、没有可对账的 after_state：真正的门是
                # 任务白名单 + 逐次人工批准（`_authority` 已判完），执行期结论
                # 由 server 的响应决定（超时/断线一律 unknown）。
                pass
            else:
                if request.action == ACTION_EXEC:
                    isolation = self._precheck_exec(request, args, target.path)
                    # 与真正 spawn 使用同一份已校验 argv 和解析后的 cwd；后端取
                    # 当前实际装配，而不是请求字符串或 capability 声明另造来源。
                    command = {
                        "argv": list(args["argv"]),
                        "cwd": str(target.path),
                        "backend": self._exec_backend.capabilities.name,
                    }
                else:
                    isolation = None
                ok, reason = check_request_preconditions(
                    request.action, target.path, request.preconditions or {}
                )
                if not ok:
                    raise DispatchDenied(
                        reason, precondition_rule_id(request.action, target.path)
                    )
        except DispatchDenied as exc:
            return self._reject(request, args, exc.reason, exc.rule_id, event=DISPATCH_REJECTED)

        # 网络还要多查一道：字节额度够不够**这一次**抓取。放在 prepare（弹批准
        # 之前）而不是只在 dispatch 里预占，是为了不给人一个"批了也发不出去"的
        # 批准——那道批准会白占一次人工决定。
        if isinstance(target, _NetworkTarget):
            try:
                self._precheck_network_budget(request, args=args)
            except DispatchDenied as exc:
                return self._reject(
                    request, args, exc.reason, exc.rule_id, event=DISPATCH_REJECTED
                )

        policy_preconditions = self._policy_preconditions(request, target, args)
        decision = self._policy.classify(
            request.action,
            list(request.resources),
            auth.bindings,
            policy_preconditions,
            task_id=request.task_id,
            # 子任务的决策形状按**工具**区分（spawn 审批 / cancel 自动）：
            # 只看 action 的话两者都是 delegate，分不开。
            tool_id=request.tool_id,
        )
        # 放行/需审批的判定结果也会写进 payload.rule_id（dispatch.requested），
        # 所以和拒绝路径一样要过同一张表；否则"为什么放行"仍然没有契约。
        check_rule_id(decision.rule_id, where="prepare:classify")

        if decision.is_denied:
            return self._reject(
                request, args, decision.reason, decision.rule_id, event=POLICY_DENIED
            )

        if decision.is_auto:
            self._store_request(request, args, STATE_ADMITTED, decision.reason, decision.rule_id)
            payload = {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "action_digest": request.action_digest(),
                "action": request.action,
                "resources": list(request.resources),
                "decision": "auto",
                "rule_id": decision.rule_id,
            }
            if isolation is not None:
                payload["isolation"] = isolation
            self._append(request, DISPATCH_REQUESTED, payload)
            return DispatchDecision(
                KIND_ADMITTED,
                request.request_id,
                request.digest(),
                reason=decision.reason,
                rule_id=decision.rule_id,
            )

        # 人已经否决过这个动作？否决是终局——**在发起审批之前**就拒绝。
        #
        # 这里是 issue #40 的修复点：以前 `find_for()` 只找 pending/approved，
        # 被拒的审批等于不存在，于是 resume 会再建一张新审批，Run 又回到
        # waiting_approval，用户被迫反复点拒绝而 Run 永远收不了尾。
        # `rejected_for()` 认的是动作内容（不含 call_id / owner / claim_epoch）：
        # 换参数/换目标就查不到（允许重新发起），换调用编号仍然命中。
        if self._approvals.rejected_for(request) is not None:
            return self._reject(
                request,
                args,
                APPROVAL_REJECTED_REASON,
                APPROVAL_REJECTED,
                event=DISPATCH_REJECTED,
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
            if request.tool_id == SUBMIT_TOOL:
                fields = json.dumps(args["fields"], ensure_ascii=False)
                decision = replace(
                    decision, reason=f"POST {request.resources[0]} · 完整字段：{fields}",
                )
            if isinstance(target, _McpTarget) and self._mcp_clients is not None:
                config = self._mcp_clients.configs.get(target.server)
                if config is not None and config.url:
                    decision = replace(decision, reason=f"{decision.reason} · 端点：{config.url}")
            approval = self._approvals.request(
                request, decision, isolation=isolation, command=command
            )

        state = STATE_WAITING if approval.state == "pending" else STATE_ADMITTED
        self._store_request(
            request,
            args,
            state,
            decision.reason,
            decision.rule_id,
            approval_id=approval.approval_id,
        )
        approval_payload = {
            "request_id": request.request_id,
            "request_digest": request.digest(),
            "action": request.action,
            "resources": list(request.resources),
            "decision": decision.kind,
            "rule_id": decision.rule_id,
            "approval_id": approval.approval_id,
            "approval_state": approval.state,
        }
        if isolation is not None:
            approval_payload["isolation"] = isolation
        self._append(request, DISPATCH_REQUESTED, approval_payload)
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

        # 终态请求的 args_payload 可能已按审计策略脱敏，不能拿它重算完整参数摘要。
        # 先认已提交的 dispatch/result；同一调用只返回原结论，绝不重复副作用或改写终态。
        existing = self.dispatch_record(request.run_id, request.call_id)
        if existing is not None and existing.state in (
            DISPATCH_FINISHED,
            DISPATCH_ERROR,
        ):
            if existing.request_digest != request.digest():
                rejected = self._reject(
                    request,
                    args,
                    f"call_id 已被另一次请求占用：{request.call_id}"
                    f"（已有 state={existing.state}）",
                    D_DISPATCH_CALL_ID_REUSED,
                    event=DISPATCH_REJECTED,
                )
                return ToolResult(
                    "rejected",
                    "none_applied",
                    {
                        "error": rejected.reason,
                        "rule_id": rejected.rule_id,
                        "request_id": request_id,
                    },
                )
            result = self._dispatched_result(request)
            if result is None:
                raise DispatchError(
                    f"这次调用已派发但缺少 tool.result：call_id={request.call_id}"
                    f"（state={existing.state}）"
                )
            return result

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

        if existing is not None:
            if existing.request_digest != request.digest():
                rejected = self._reject(
                    request,
                    args,
                    f"call_id 已被另一次请求占用：{request.call_id}"
                    f"（已有 state={existing.state}）",
                    D_DISPATCH_CALL_ID_REUSED,
                    event=DISPATCH_REJECTED,
                )
                return ToolResult(
                    "rejected",
                    "none_applied",
                    {
                        "error": rejected.reason,
                        "rule_id": rejected.rule_id,
                        "request_id": request_id,
                    },
                )
            raise DispatchError(
                f"这次调用已经派发过：call_id={request.call_id}"
                f"（state={existing.state}）"
            )

        if stored.state == STATE_DONE:
            result = self._dispatched_result(request)
            if result is None:
                raise DispatchError(
                    f"这次调用已完成但缺少 tool.result：call_id={request.call_id}"
                )
            return result

        # 审批绑定的是 request.args_hash / action_digest，不是后来从权威请求表
        # 读出的 args。必须在消费审批、签 grant、写 intent 之前核对真实参数；
        # 否则改了 args_payload 的攻击会先消费审批，再由执行器发现不匹配。
        if sha256_hex(canonical_bytes(args)) != request.args_hash:
            rejected = self._reject(
                request,
                args,
                "args 与 request.args_hash 不符（请求被改过）",
                "dispatch.not_admitted",
                event=DISPATCH_REJECTED,
            )
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": rejected.reason,
                    "rule_id": rejected.rule_id,
                    "request_id": request_id,
                },
            )

        # ---- 阶段一：同一事务里核验 + 消费审批 + 签发票据 + 登记 intent ----
        try:
            with self._log.transaction() as tx:
                # 外层读取之后仍可能被另一进程抢先派发；事务内先复核既有记录，
                # 再允许任何审批消费/grant/intent 写入。
                existing = self.dispatch_record(request.run_id, request.call_id)
                if existing is not None:
                    if existing.request_digest != request.digest():
                        raise DispatchDenied(
                            f"call_id 已被另一次请求占用：{request.call_id}"
                            f"（已有 state={existing.state}）",
                            D_DISPATCH_CALL_ID_REUSED,
                        )
                    if existing.state in (DISPATCH_FINISHED, DISPATCH_ERROR):
                        result = self._dispatched_result(request)
                        if result is None:
                            raise DispatchError(
                                f"这次调用已派发但缺少 tool.result：call_id={request.call_id}"
                                f"（state={existing.state}）"
                            )
                        return result
                    raise DispatchError(
                        f"这次调用已经派发过：call_id={request.call_id}"
                        f"（state={existing.state}）"
                    )
                auth = self._authority(request, claim, args=args)
                target = self._resolve_target(request, auth)
                self._check_run_execution(request, target)
                if (
                    not isinstance(
                        target,
                        (
                            _MemoryTarget,
                            _NetworkTarget,
                            _McpTarget,
                            _SubtaskTarget,
                        ),
                    )
                    and request.action == ACTION_EXEC
                ):
                    self._precheck_exec(request, args, target.path)

                if isinstance(target, _MemoryTarget):
                    self._check_memory_preconditions(request, target, args)
                elif isinstance(
                    target, (_NetworkTarget, _McpTarget, _SubtaskTarget)
                ):
                    pass
                else:
                    ok, reason = check_request_preconditions(
                        request.action, target.path, request.preconditions or {}
                    )
                    if not ok:
                        # 派发阶段（已批准、还没打开文件）的拒绝统一记 stale：
                        # 对这张已消费的批准而言，结论是"状态被改过了，需要重新
                        # 发起"，而不是"该换工具"（那是 prepare 阶段的判定）。
                        raise DispatchDenied(reason, "dispatch.stale_precondition")

                approval_id, grant_id = self._consume_authority(
                    request, auth, stored, target=target, args=args
                )
                dispatch_id = self._dispatch_id(request)

                seq = tx.append(
                    request.run_id,
                    "tool.intent",
                    {
                        "call_id": request.call_id,
                        "tool_id": request.tool_id,
                        "args": self._intent_args(request, args),
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

        # ---- 网络分支：没有目录、没有租约、没有本地副作用 ----
        if isinstance(target, _NetworkTarget):
            return self._dispatch_network(
                request, dispatch_id, target, seq, admitted
            )

        # ---- MCP 分支：外部 server 执行，副作用在它那边 ----
        if isinstance(target, _McpTarget):
            return self._dispatch_mcp(request, dispatch_id, target, seq, admitted)

        # ---- 委派分支：没有目录、没有租约；spawn 在一个事务里建五件事 ----
        if isinstance(target, _SubtaskTarget):
            try:
                with execution_guard(
                    self._require_data_root(), timeout_ms=self._guard_timeout_ms
                ):
                    return self._dispatch_subtask(
                        request, dispatch_id, target, seq, args, admitted
                    )
            except (
                DispatchDenied,
                DiskAdmissionError,
                GuardTimeout,
                ApprovalError,
                JobLost,
                RunFenced,
            ) as exc:
                return self._abort_dispatch(request, dispatch_id, exc)

        # ---- 阶段二：守卫内再核一遍，然后才真正写 ----
        memory_committed = False
        try:
            if isinstance(target, _MemoryTarget):
                # 记忆没有目录租约：不能走 `begin_execution`（它强制要求资源
                # 落在租约上）。但仍然要拿同一把执行守卫、写 execution.started，
                # 否则撤销与执行的先后就没有全序。
                with self._begin_memory_execution(request, claim) as permit:
                    result = self._memory_executor_for().execute(admitted, permit)
                    if request.tool_id == "memory.erase":
                        # 擦除的"引用计数 + 删文件 + 行更新"必须与**提交**在同一把
                        # 跨进程守卫内：否则两个进程同时擦共享同一 blob 的两条
                        # 记忆，会各自看到"对方还没擦"→ 都保留文件 → 两行都标
                        # erased、孤儿文件永久留存（复审探针确定性复现过）。
                        self._commit_result(request, dispatch_id, result, target, seq)
                        memory_committed = True
            else:
                with self._tasks.begin_execution(
                    self._data_root, request.run_id, auth.lease_paths
                ) as permit:
                    result = self._executor.execute(admitted, permit)
        except (
            DispatchDenied,
            DiskAdmissionError,
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

        if isinstance(target, _MemoryTarget):
            if not memory_committed:
                try:
                    self._commit_result(request, dispatch_id, result, target, seq)
                except Exception as exc:  # noqa: BLE001 - 投影事务失败：效果未知
                    # `_commit_result` 的事务已回滚（事件、凭据、投影一起消失）。
                    # 但执行器可能已经写了内容 blob，结论仍只能是 unknown；这里
                    # 补一条 tool.result(unknown)，且**不**再调用 apply_event。
                    return self._fail_unknown(request, dispatch_id, target, exc)
        else:
            try:
                self._commit_result(request, dispatch_id, result, target, seq)
            except Exception as exc:  # noqa: BLE001 - 结果事务已回滚：磁盘可能已改动
                # `_commit_result` 单事务失败即整笔回滚（事件、凭据、派发状态同生同死），
                # 但文件/命令副作用已在执行守卫内完成：绝不能让裸异常穿过派发边界被
                # 主循环按 none_applied 收尾（#145），必须转入 unknown 并阻断工作区。
                return self._fail_unknown(request, dispatch_id, target, exc)
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



# ---------------------------------------------------------------- 请求构造


def build_request(
    *,
    task: Task,
    run: Run,
    claim: JobClaim,
    call_id: str,
    tool_id: str,
    target: Path | str,
    args: dict[str, Any],
    request_id: str | None = None,
    attempt_id: int = 1,
    preconditions: dict[str, Any] | None = None,
    registry: ToolRegistry | None = None,
) -> ActionRequest:
    """从"我要做什么"生成一份可派发的请求。

    `preconditions` 在这里**当场从磁盘读**，不是调用方说了算：
    审批绑定的必须是真实写前状态，否则"批准时改成 false"就能绕过覆盖审批。

    记忆目标不是路径：`target` 可以是 `memory://<scope>/<scope_id>`。这条分支
    不做 `Path.resolve()`，也不读文件哈希；写前状态是 memory_id + revision，
    由参数带进 `preconditions`（执行侧会从权威投影再读一次核对）。显式传入
    `preconditions` 只对记忆分支生效——文件分支的写前状态仍然由这里读盘决定。
    """
    table = registry if registry is not None else current_registry()
    resolved = table.spec(tool_id)
    if resolved is None:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")

    raw_target = str(target)
    if raw_target.startswith("mcp://"):
        target_ref = parse_resource(raw_target)
        found = table.mcp_binding(tool_id)
        if found is None:
            raise ToolNotAllowed(f"工具不在已准入的 MCP 注册表里：{tool_id}")
        return ActionRequest(
            request_id=request_id or f"req_{uuid.uuid4().hex[:12]}",
            task_id=task.task_id,
            run_id=run.run_id,
            job_id=claim.job_id,
            claim_epoch=claim.claim_epoch,
            owner_instance_id=claim.owner_instance_id,
            call_id=call_id,
            goal_revision=run.goal_revision,
            lease_epoch=run.lease_epoch,
            tool_id=tool_id,
            tool_version=found.tool_version,
            schema_hash=found.schema_hash,
            description_hash=found.description_hash,
            action=resolved.action,
            resources=(target_ref.uri,),
            args_hash=sha256_hex(canonical_bytes(args)),
            attempt_id=attempt_id,
            preconditions={},
        )
    if raw_target.startswith("memory://"):
        if tool_id not in MEMORY_ALL_TOOL_IDS:
            raise ToolNotAllowed(f"文件工具不能使用记忆资源：{tool_id} → {raw_target}")
        try:
            scope = parse_memory_uri(raw_target)
        except ResourceError as exc:
            raise ToolArgumentError(f"记忆资源引用不合法：{exc}") from exc
        if resolved.action == ACTION_CREATE:
            memory_pre: dict[str, Any] = {
                "memory_kind": args.get("kind"),
                "memory_exists": False,
            }
        elif resolved.action == ACTION_OVERWRITE:
            # 覆盖类记忆工具不止 confirm 一个（edit / promote 也在这里），目标
            # 可能是 candidate 也可能是 active——**不能**写死 "candidate"。
            # 状态由调用方声明（`status` 可选）；真正的状态约束由投影的迁移
            # 校验（`_apply_confirm` / `_apply_edit`）兜底。
            memory_pre = {
                "memory_id": args.get("memory_id"),
                "before_status": args.get("status"),
                "before_revision": args.get("revision"),
            }
        elif resolved.action == ACTION_DELETE:
            memory_pre = {
                "memory_id": args.get("memory_id"),
                # 删除类目标可以是 candidate / active / superseded / forgotten：
                # 不声明状态就只按 revision 绑定；状态约束由执行侧与投影校验兜底。
                "before_status": args.get("status"),
                "before_revision": args.get("revision"),
            }
        else:  # pragma: no cover - 记忆读工具不走派发
            memory_pre = {}
        if preconditions is not None:
            memory_pre = dict(preconditions)
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
            tool_version=resolved.version,
            schema_hash=sha256_hex(canonical_bytes(resolved.input_schema)),
            description_hash=resolved.description_digest(),
            action=resolved.action,
            resources=(scope.to_uri(),),
            args_hash=sha256_hex(canonical_bytes(args)),
            preconditions=memory_pre,
        )

    if raw_target.startswith("network://"):
        if tool_id not in NETWORK_TOOL_IDS:
            raise ToolNotAllowed(
                f"非网络工具不能使用网络资源：{tool_id} → {raw_target}"
            )
        try:
            net_ref = parse_resource(raw_target)
        except ResourceError as exc:
            raise ToolArgumentError(f"网络资源引用不合法：{exc}") from exc
        if net_ref.kind != KIND_NETWORK:  # pragma: no cover - 前缀已判
            raise ToolArgumentError(f"不是网络资源：{raw_target}")
        # 网络动作的"写前状态"不是磁盘：没有本地副作用，也没有可撤销内容。
        # 白名单在派发阶段从权威任务再读一次（见 `_authority`），这里不复制
        # 一份声明进请求——审批摘要绑住的是**目标**，不是当时的内存快照。
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
            tool_version=resolved.version,
            schema_hash=sha256_hex(canonical_bytes(resolved.input_schema)),
            description_hash=resolved.description_digest(),
            action=resolved.action,
            resources=(net_ref.uri,),
            args_hash=sha256_hex(canonical_bytes(args)),
            preconditions={},
        )

    if raw_target.startswith("subtask://"):
        if tool_id not in SUBTASK_TOOL_IDS:
            raise ToolNotAllowed(
                f"非子任务工具不能使用子任务资源：{tool_id} → {raw_target}"
            )
        try:
            sub_ref = parse_resource(raw_target)
        except ResourceError as exc:
            raise ToolArgumentError(f"子任务资源引用不合法：{exc}") from exc
        if sub_ref.kind != KIND_SUBTASK:  # pragma: no cover - 前缀已判
            raise ToolArgumentError(f"不是子任务资源：{raw_target}")
        # 委派没有"写前状态"：没有本地副作用，也没有可撤销内容。真正的门是
        # 父任务声明、父 Run 作用域与限额（派发层从权威投影再读一次）。
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
            tool_version=resolved.version,
            schema_hash=sha256_hex(canonical_bytes(resolved.input_schema)),
            description_hash=resolved.description_digest(),
            action=resolved.action,
            resources=(sub_ref.uri,),
            args_hash=sha256_hex(canonical_bytes(args)),
            preconditions={},
        )

    if tool_id in MEMORY_ALL_TOOL_IDS:
        raise ToolNotAllowed(f"记忆工具必须使用 memory:// 资源：{raw_target}")

    if tool_id in SUBTASK_TOOL_IDS:
        # 子任务工具拿文件目标：在请求构造处就拒绝，不给"回退成文件动作"留路。
        raise ToolNotAllowed(
            f"子任务工具必须使用 subtask:// 资源：{tool_id} → {raw_target}"
        )

    target = Path(target).resolve()
    io = current_backend()
    if resolved.action == ACTION_CREATE:
        file_preconditions: dict[str, Any] = {"target_exists": io.exists(target)}
    elif resolved.action in (ACTION_OVERWRITE, ACTION_DELETE):
        file_preconditions = {"before_state": observe_state(target, backend=io)}
    elif resolved.action == ACTION_EXEC:
        # 这个标记进 actions 摘要：任务有没有开启 exec 策略，必须由审批绑住。
        file_preconditions = {"exec_configured": task.exec_policy is not None}
    else:  # pragma: no cover - 只读动作不走派发
        file_preconditions = {}

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
        tool_version=resolved.version,
        schema_hash=sha256_hex(canonical_bytes(resolved.input_schema)),
        description_hash=resolved.description_digest(),
        action=resolved.action,
        resources=(str(target),),
        args_hash=sha256_hex(canonical_bytes(args)),
        preconditions=file_preconditions,
    )


# 跨模块/测试重导出兼容符号（保持与拆分前 `tianshu.dispatcher` 命名空间完全一致）。
_COMPAT_EXPORTS = (
    DISPATCH_ERROR,
    DISPATCH_EXECUTING,
    DISPATCH_FINISHED,
    DISPATCH_ROW_REJECTED,
    DISPATCH_ROW_REVOKED,
    EXECUTABLE_KINDS,
    _Authority,
    _FileTarget,
    _Target,
    _audit_args,
    net_fetch,
    require_isolation_requirements,
    resolve_scheme_resource,
)

__all__ = [
    "APPROVAL_REJECTED",
    "APPROVAL_REJECTED_REASON",
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
