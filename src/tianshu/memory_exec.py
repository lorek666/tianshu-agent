"""受信任的记忆执行器（F10）。

这个模块和 `tools.FileExecutor` 是同一层的两个执行器：

- 文件执行器负责"打开文件、再核一遍租约/审批"；
- 记忆执行器负责"把投影更新交给 Dispatcher 同事务提交、再核一遍身份"。

**它不是另一条授权路径。** 拿到 `AdmittedCall` 之后，它自己回权威库逐条核验：
claim、Run 的 revision/epoch、派发记录 state=intent、grant 已消费且未过期、审批
`action_digest` 匹配且 state=consumed。唯一不同的是**资源和前置条件**：

- 记忆不落在目录里，所以没有目录租约可核。作用域授权（user / workspace / task）
  由 `memory_scope_allowed()` 独立核验，不能因为"不是文件"就跳过；
- 记忆的写前状态是 `(status, revision)`，不是文件哈希。必须由执行侧自己从
  投影里读出来，和请求/审批绑定过的 preconditions 比较。

`MemoryExecutor` **不改投影**。生命周期由 `Dispatcher._commit_result()` 在写
`tool.result` 的同一个 SQLite 事务里调用 `MemoryProjection.apply_event()`；
执行器只返回 `ToolResult.output`（含投影需要的字段）。这样崩溃时要么事件和
投影一起存在，要么一起不存在。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .disk_admission import DiskAdmissionError
from .errors import RunFenced, TianshuError, ToolArgumentError, ToolNotAllowed
from .events import canonical_bytes, sha256_hex
from .jobs import JobClaim
from .permissions import (
    ACTION_OVERWRITE,
    APPROVAL_ACTIONS,
    ActionRequest,
    GrantStore,
    supported_memory_kinds,
)
from .resources import (
    MEMORY_SCOPES,
    MemoryScope,
    ResourceError,
    build_memory_scope,
    parse_memory_uri,
)
from .tasks import RUN_RUNNING, ExecutionPermit, TaskStore
from .tools import (
    SPECS,
    AdmittedCall,
    ToolResult,
    check_request_preconditions,
    validate_args,
)
from .workspace import WorkspaceBinding, overlap_key

if TYPE_CHECKING:  # pragma: no cover - 只要类型，不把 memory.py 拖成硬依赖
    from .memory import MemoryProjection

MEMORY_TOOL_IDS = frozenset(
    {
        "memory.propose",
        "memory.consolidate",
        "memory.remember",
        "memory.edit",
        "memory.promote",
        "memory.confirm",
        "memory.forget",
        "memory.erase",
    }
)

#: 只有控制 Run（`Task.is_control`）能调用的记忆写工具。
CONTROL_ONLY_MEMORY_TOOLS = frozenset(
    {"memory.remember", "memory.promote", "memory.erase"}
)
MEMORY_READ_TOOL_IDS = frozenset({"memory.search", "memory.get"})
MEMORY_ALL_TOOL_IDS = MEMORY_TOOL_IDS | MEMORY_READ_TOOL_IDS

#: 写前/写后状态的稳定形状。凭据核对与测试都依赖它，不允许临时改字符串。
MEMORY_STATE_PREFIX = "memory:"

#: 从记忆对象里读取（兼容真实 Memory 与测试投影的最小接口）。
_USER_EXPLICIT = "user_explicit"


class MemoryDispatchRefused(ToolNotAllowed):
    """执行守卫内的记忆拒绝，带一个已登记的 rule_id。

    `FileExecutor` 的拒绝靠异常类型映射到规则编号；记忆的拒绝原因更细
    （不存在 / revision 过期 / 资源不匹配），所以把 rule_id 放在异常上，
    由 `Dispatcher._abort_dispatch()` 直接采用并校验。它仍然是"一个字节都
    没改"的确定性拒绝。
    """

    def __init__(self, reason: str, rule_id: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_id = rule_id


def memory_state(memory_id: str, status: str, revision: int) -> str:
    return f"{MEMORY_STATE_PREFIX}{memory_id}:{status}:rev{revision}"


def memory_scope_allowed(
    scope: MemoryScope,
    *,
    task_id: str,
    bindings: list[WorkspaceBinding],
    declared: Sequence[str] = (),
) -> tuple[bool, str]:
    """判断一个记忆作用域是否在本任务授权范围内。

    与 `PolicyEngine._classify_memory()` 使用同一套规则，但这段代码在执行侧
    再跑一遍：策略判定不是授权依据（红线 4），执行器不能因为"prepare 说可以"
    就信任票据。

    - user：D2 单用户，恒可访问；
    - workspace：scope_id 必须**精确等于**某个绑定的 canonical 根；不是祖先、
      不是字符串前缀、不是"解析后落在里面"；
    - task：scope_id 必须等于本任务 id；
    - `declared`：控制 Run 在 `task.created` 里显式声明的记忆作用域 URI。它只
      能由控制面写入，调用方**必须**确认 `Task.is_control` 后才传进来——否则
      等于让普通任务自造作用域。
    """
    if not isinstance(scope, MemoryScope):
        return False, f"作用域类型不合法：{type(scope).__name__}"
    if scope.scope not in MEMORY_SCOPES:
        return False, f"未知记忆作用域：{scope.scope!r}"
    if declared:
        uri = scope.to_uri()
        if uri in set(declared):
            return True, ""
        # 声明了作用域的控制 Run 也**只**能用声明过的那些：`user` 恒可，其它
        # 作用域一律按声明集合判定，不再回落到绑定规则（旧 task 没有绑定）。
        if scope.scope != "user":
            return False, f"作用域不在控制 Run 的声明内：{uri}"
    if scope.scope == "user":
        return True, ""
    if scope.scope == "workspace":
        # 精确根 + 平台规范化（大小写/Unicode）；不接受"落在子树里"。
        roots = {overlap_key(str(WorkspaceBinding.of(b).root_path)) for b in bindings}
        if overlap_key(scope.scope_id) not in roots:
            return False, f"workspace 作用域不是本任务绑定的 canonical 根：{scope.scope_id}"
        return True, ""
    if scope.scope == "task":
        if scope.scope_id != task_id:
            return False, (
                f"task 作用域不属于本任务：{scope.scope_id!r} ≠ {task_id!r}"
            )
        return True, ""
    return False, f"未知记忆作用域：{scope.scope!r}"  # pragma: no cover


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryExecutor:
    """记忆写入的唯一执行器。

    它**不接受**调用方说"已经批准了"：票据可以伪造，库里的记录不会。
    """

    def __init__(
        self,
        *,
        tasks: TaskStore,
        approvals: object,
        grants: GrantStore,
        claims: object,
        projection: MemoryProjection,
        dispatch_lookup,
        dispatch_mark,
        clock,
        log: object | None = None,
    ) -> None:
        self._tasks = tasks
        self._approvals = approvals
        self._grants = grants
        self._claims = claims
        self._projection = projection
        self._lookup = dispatch_lookup
        self._mark = dispatch_mark
        self._clock = clock
        self._log = log

    @property
    def backend_name(self) -> str:
        """进凭据的 backend。记忆没有磁盘后端，明确写 memory。"""
        return "memory"

    def _observe(self, memory_id: str, scope: MemoryScope) -> tuple[str, int] | None:
        """从权威投影读 (status, revision)。

        投影的输入校验异常（畸形 memory_id 等）是**确定拒绝**，不是"效果未知"：
        这时还没有任何写入，记成 unknown 会让可证明的结论降级成猜测。
        """
        try:
            return self._projection.observe(memory_id, allowed=(scope,))
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆观察失败：{exc}", "dispatch.memory_not_found"
            ) from exc

    # ---- 独立核验 ----

    def _verify(self, admitted: AdmittedCall, permit: ExecutionPermit | None) -> None:
        if not isinstance(admitted, AdmittedCall):
            raise ToolNotAllowed(
                "记忆执行器只接受 AdmittedCall；直接执行参数是被禁止的"
            )
        if not isinstance(permit, ExecutionPermit):
            raise ToolNotAllowed(
                "记忆执行器必须在 execution_guard 内调用"
                "（缺少 ExecutionPermit）"
            )

        req = admitted.request
        if not isinstance(req, ActionRequest) or not isinstance(
            admitted.claim, JobClaim
        ):
            raise ToolNotAllowed("票据类型不合法")
        if req.digest() != admitted.request_digest:
            raise ToolNotAllowed("请求摘要与票据不一致（票据被改过）")
        if admitted.tool_id != req.tool_id or admitted.action != req.action:
            raise ToolNotAllowed("票据的工具/动作与请求不一致")
        if tuple(admitted.resources) != tuple(req.resources):
            raise MemoryDispatchRefused(
                "票据资源与请求不一致", "dispatch.memory_resource_mismatch"
            )
        if sha256_hex(canonical_bytes(admitted.args)) != req.args_hash:
            raise ToolNotAllowed("票据参数与请求 args_hash 不一致（参数被改过）")
        if admitted.claim.owner_instance_id != req.owner_instance_id:
            raise ToolNotAllowed("票据的 owner 与请求不符")
        if permit.run_id != req.run_id or permit.task_id != req.task_id:
            raise ToolNotAllowed("执行许可属于另一个 Run")
        if (permit.goal_revision, permit.lease_epoch) != (
            req.goal_revision,
            req.lease_epoch,
        ):
            raise RunFenced(
                f"执行许可的 revision/epoch 与请求不符：许可 "
                f"{permit.goal_revision}/{permit.lease_epoch}，请求 "
                f"{req.goal_revision}/{req.lease_epoch}"
            )

        # 1. claim 仍是本实例的、epoch 没变
        self._claims.assert_claim(admitted.claim)  # type: ignore[attr-defined]
        if admitted.claim.claim_epoch != req.claim_epoch:
            raise ToolNotAllowed("票据的 claim_epoch 与请求不符")

        # 2. Run 没被 fence
        run = self._tasks.assert_current(req.run_id)
        if (run.goal_revision, run.lease_epoch) != (
            req.goal_revision,
            req.lease_epoch,
        ):
            raise RunFenced(
                f"Run 的 revision/epoch 已改变：当前 {run.goal_revision}/"
                f"{run.lease_epoch}，请求 {req.goal_revision}/{req.lease_epoch}"
            )
        if run.state != RUN_RUNNING:
            raise RunFenced(
                f"run 不在执行状态，拒绝开始写入：{req.run_id}（state={run.state}）"
            )

        # 3. 这次调用真的被派发过、且还停在 intent
        row = self._lookup(req.run_id, req.call_id)
        if row is None:
            raise ToolNotAllowed(f"没有派发记录，拒绝执行：call_id={req.call_id}")
        if row.state != "intent":
            raise ToolNotAllowed(f"派发记录状态不是 intent：{row.state}")
        if row.request_digest != admitted.request_digest:
            raise ToolNotAllowed("派发记录的摘要与票据不符")
        if row.tool_id != req.tool_id or row.action != req.action:
            raise ToolNotAllowed("派发记录的工具/动作与请求不符")
        if row.grant_id != admitted.grant_id:
            raise ToolNotAllowed("派发记录的 grant 与票据不符")
        if row.claim_epoch != admitted.claim.claim_epoch:
            raise ToolNotAllowed("派发记录的 claim_epoch 与票据不符")
        if row.owner_instance_id != admitted.claim.owner_instance_id:
            raise ToolNotAllowed("派发记录的 owner 与票据不符")

        # 4. grant：一次性、未过期、绑定同一个请求摘要
        grant = self._grants.get(admitted.grant_id)
        if grant is None:
            raise ToolNotAllowed("执行许可不存在")
        if grant.request_digest != admitted.request_digest:
            raise ToolNotAllowed("执行许可绑定的是另一个请求摘要")
        if grant.remaining_uses != 0:
            raise ToolNotAllowed("执行许可尚未被消费——派发不完整")
        if grant.expires_at <= self._clock():
            raise ToolNotAllowed("执行许可已过期")

        # 5. 资源：必须是 memory://，且工具与资源种类匹配
        if len(admitted.resources) != 1:
            raise MemoryDispatchRefused(
                "记忆动作一次只能作用于一个作用域",
                "dispatch.memory_resource_mismatch",
            )
        try:
            target_scope = parse_memory_uri(admitted.resources[0])
        except ResourceError as exc:
            raise MemoryDispatchRefused(
                f"记忆资源引用不合法：{exc}", "dispatch.memory_resource_mismatch"
            ) from exc
        if admitted.tool_id not in MEMORY_TOOL_IDS:
            raise MemoryDispatchRefused(
                f"工具不是记忆写入工具：{admitted.tool_id}",
                "dispatch.memory_resource_mismatch",
            )
        spec = SPECS.get(admitted.tool_id)
        if spec is None or req.action != spec.action:
            # 工具与 C4 动作必须一一对应：`memory.confirm` 不能被伪装成
            # `create` 绕过覆盖审批。这是执行侧自己的核验，不依赖 _typecheck。
            raise MemoryDispatchRefused(
                f"工具 {admitted.tool_id} 与动作 {req.action} 不匹配",
                "dispatch.memory_resource_mismatch",
            )
        if admitted.target != target_scope.to_uri():
            raise MemoryDispatchRefused(
                "票据目标与记忆作用域不一致",
                "dispatch.memory_resource_mismatch",
            )

        task = self._tasks.get_task(req.task_id)
        if task is None:
            raise ToolNotAllowed(f"task 不存在：{req.task_id}")
        if task.depth or run.parent_run_id:
            raise MemoryDispatchRefused(
                "只读子任务不能写入记忆", "c4.memory_scope_denied"
            )
        # 契约 v1.2：控制面 Run 的标记与作用域声明都从 `task.created` 投影读，
        # **不**信任调用方传进来的任何"我是控制面"。普通任务拿不到声明作用域。
        is_control = bool(getattr(task, "is_control", False))
        declared: Sequence[str] = (
            tuple(getattr(task, "memory_scopes", ()) or ()) if is_control else ()
        )
        if (
            admitted.tool_id in ("memory.propose", "memory.consolidate")
            and target_scope.scope == "workspace"
        ):
            # workspace 是**共享边界**：它**无条件**拒绝模型可见的 propose，
            # 不因 Task.is_control 放行——控制面标记是 Task 级的，同一 Task 上
            # 后开的模型 Run 会继承它（独立复审的探针复现过这个洞）。
            # 共享写只有一条路：控制面专属的 `memory.promote`（exposed=False）。
            raise MemoryDispatchRefused(
                f"workspace 作用域只能由人经 memory.promote 进入："
                f"{target_scope.to_uri()}",
                "c4.memory_scope_denied",
            )
        if not is_control and admitted.tool_id in CONTROL_ONLY_MEMORY_TOOLS:
            raise MemoryDispatchRefused(
                f"控制面专属工具不能在普通任务里执行：{admitted.tool_id}",
                "c4.memory_control_scope_binding",
            )
        if is_control and admitted.tool_id == "memory.promote":
            # 提升的目标作用域必须由控制 Run 显式声明（user 除外——它恒可）。
            try:
                new_scope = build_memory_scope(
                    admitted.args.get("scope"), admitted.args.get("scope_id")
                )
            except TianshuError as exc:
                raise MemoryDispatchRefused(
                    f"提升目标作用域不合法：{exc}", "c4.memory_scope_denied"
                ) from exc
            if new_scope == target_scope:
                raise MemoryDispatchRefused(
                    "提升目标作用域与原作用域相同", "c4.memory_scope_denied"
                )
            if new_scope.scope != "user" and new_scope.to_uri() not in set(declared):
                raise MemoryDispatchRefused(
                    f"提升目标作用域不在控制 Run 的声明内：{new_scope.to_uri()}",
                    "c4.memory_scope_denied",
                )
        if not is_control:
            # 注：authority / policy 通常已经先拒了"写别人的 task"，这里保留
            # 同名判定作纵深防御——执行侧不因为"上游说不行"就少一道核验。
            if (
                admitted.tool_id in ("memory.propose", "memory.consolidate")
                and target_scope.scope == "task"
                and target_scope.scope_id != req.task_id
            ):
                # 模型只能写**本任务**的 task 作用域；别人的 task 作用域
                # 只有控制面（CLI/UI）能替它操作。
                raise MemoryDispatchRefused(
                    f"模型只能写本任务的 task 作用域：{target_scope.to_uri()}",
                    "c4.memory_scope_denied",
                )
        allowed, reason = memory_scope_allowed(
            target_scope,
            task_id=req.task_id,
            bindings=list(task.workspaces or []),
            declared=declared,
        )
        if not allowed:
            raise MemoryDispatchRefused(reason, "c4.memory_scope_denied")

        # 6. 审批：覆盖/删除必须有，且必须已"被消费一次"。
        #    `origin=user_explicit` 的 confirm 是唯一例外：用户本人刚说的话
        #    就是授权（F10 §7）。执行侧自己从投影里读 origin，不信任模型参数。
        exempt = (
            req.action == ACTION_OVERWRITE
            and self._origin_of(admitted, target_scope) == _USER_EXPLICIT
        )
        if req.action in APPROVAL_ACTIONS and not exempt and not admitted.approval_id:
            raise ToolNotAllowed(f"{req.action} 缺少审批绑定")
        if admitted.approval_id:
            appr = self._approvals.get(admitted.approval_id)  # type: ignore[attr-defined]
            if appr is None:
                raise ToolNotAllowed("审批记录不存在")
            if appr.run_id != req.run_id:
                raise MemoryDispatchRefused(
                    "审批属于另一个 Run", "c4.approval_run_mismatch"
                )
            if appr.action_digest:
                if appr.action_digest != req.action_digest():
                    raise ToolNotAllowed("审批绑定的动作与本次请求不符")
            elif appr.request_digest != admitted.request_digest:
                raise ToolNotAllowed("审批绑定的是另一个请求摘要")
            if appr.state != "consumed":
                raise ToolNotAllowed(f"审批未消费或已被撤销：{appr.state}")

        # 7. 记忆特有：resource 与参数必须指向同一条记忆；status/revision 必须
        #    与请求声明一致。
        args = admitted.args
        tool_id = admitted.tool_id
        if tool_id in ("memory.confirm", "memory.forget"):
            memory_id = args.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id:
                raise ToolArgumentError("memory_id 必须是非空字符串")
            declared_id = (req.preconditions or {}).get("memory_id")
            if declared_id is not None and declared_id != memory_id:
                raise MemoryDispatchRefused(
                    "请求声明的 memory_id 与参数不一致",
                    "dispatch.memory_resource_mismatch",
                )
            observed = self._observe(memory_id, target_scope)
            if observed is None:
                raise MemoryDispatchRefused(
                    f"记忆不存在或不属于当前作用域：{memory_id}",
                    "dispatch.memory_not_found",
                )
            ok, why = check_request_preconditions(
                req.action,
                target_scope.to_uri(),
                req.preconditions or {},
                memory_observed=observed,
            )
            if not ok:
                raise MemoryDispatchRefused(
                    why, "dispatch.memory_stale_revision"
                )
        elif tool_id == "memory.propose":
            kind = args.get("kind")
            if not isinstance(kind, str) or kind not in supported_memory_kinds():
                # 执行侧独立复核 kind 封闭集；不依赖 prepare 的判定。
                raise MemoryDispatchRefused(
                    f"不支持的记忆种类：{kind!r}", "c4.memory_kind_unsupported"
                )

    def _origin_of(self, admitted: AdmittedCall, scope: MemoryScope) -> str:
        if admitted.tool_id != "memory.confirm":
            return ""
        memory_id = admitted.args.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            return ""
        try:
            memory = self._projection.get(memory_id, allowed=(scope,))
        except TianshuError:
            # 观察失败时**不能**推断"用户明示"——那会凭空免掉审批。
            return ""
        if memory is None:
            return ""
        return str(getattr(memory, "origin", ""))

    # ---- 执行 ----

    def execute(
        self, admitted: AdmittedCall, permit: ExecutionPermit | None
    ) -> ToolResult:
        self._verify(admitted, permit)

        spec = SPECS[admitted.tool_id]
        req = admitted.request
        if (req.tool_version != spec.version or req.action != spec.action
                or req.schema_hash != sha256_hex(canonical_bytes(spec.input_schema))):
            raise ToolNotAllowed("执行工具与真实 ToolSpec 不符")
        validate_args(admitted.args, spec.input_schema)
        scope = parse_memory_uri(admitted.target)
        self._tasks.require_effects_allowed(req.run_id)
        context = self._tasks.disk_admission.context
        if (context is None or context.log is not self._log
                or context.memory is not self._projection):
            raise DiskAdmissionError(
                "dispatch.disk_space_unknown", "记忆执行器与 Run context 不一致",
            )
        self._tasks.require_effect_space(
            req.run_id, targets=(), data_root=self._tasks.data_root,
        )

        if admitted.tool_id == "memory.propose":
            return self._propose(admitted, scope)
        if admitted.tool_id == "memory.consolidate":
            return self._consolidate(admitted, scope)
        if admitted.tool_id == "memory.remember":
            return self._remember(admitted, scope)
        if admitted.tool_id == "memory.edit":
            return self._edit(admitted, scope)
        if admitted.tool_id == "memory.promote":
            return self._promote(admitted, scope)
        if admitted.tool_id == "memory.erase":
            return self._erase(admitted, scope)
        if admitted.tool_id == "memory.confirm":
            return self._transition(admitted, scope, to_status="active")
        if admitted.tool_id == "memory.forget":
            return self._transition(admitted, scope, to_status="forgotten")
        raise MemoryDispatchRefused(
            f"未知记忆工具：{admitted.tool_id}",
            "dispatch.memory_resource_mismatch",
        )

    def _propose(self, admitted: AdmittedCall, scope: MemoryScope) -> ToolResult:
        args = admitted.args
        kind = args["kind"]
        content = args["content"]
        source_refs = _string_list(args.get("source_refs"), "source_refs")
        evidence = _object_list(args.get("evidence"), "evidence")
        expires_at = args.get("expires_at")
        if expires_at is not None and (
            not isinstance(expires_at, int) or isinstance(expires_at, bool)
        ):
            raise ToolArgumentError("expires_at 必须是整数毫秒时间戳")

        try:
            redacted, blocked = self._projection.redact_or_block(content)
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆写入闸门检查失败：{exc}", "dispatch.refused"
            ) from exc
        if blocked:
            # 写入闸门是 fail-closed，不把未脱敏内容送进索引，也不产生投影。
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": "记忆内容触发 PII/密钥写入闸门，候选未写入",
                    "path": scope.to_uri(),
                },
            )

        try:
            existing = self._projection.find_duplicate(
                scope=scope, kind=kind, content=redacted
            )
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆去重检查失败：{exc}", "dispatch.refused"
            ) from exc
        if existing is not None:
            raise MemoryDispatchRefused(
                "目标记忆已存在（重复候选不应再次入索引）",
                "c4.create_over_existing",
            )

        # 到这里为止都是"还没写"：上面任何拒绝都应让派发记录停在 intent，
        # 由 `_abort_dispatch()` 确定地记成 rejected。真正写入前才标记执行中。
        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        content_ref = self._projection.put_content(redacted)
        if not isinstance(content_ref, str) or not content_ref:
            raise TianshuError("记忆投影没有返回 content_ref")
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = self._clock()
        after = memory_state(memory_id, "candidate", 1)
        return ToolResult(
            "succeeded",
            "applied",
            {
                "path": scope.to_uri(),
                "memory_uri": scope.to_uri(),
                "memory_id": memory_id,
                "revision": 1,
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "kind": kind,
                "content_ref": content_ref,
                "source_refs": source_refs,
                "evidence": evidence,
                "proof_count": len(evidence),
                # 模型不能自己声明 user_explicit（否则一句"记住"就能绕过
                # confirm 审批）。候选来源固定 inferred；显式用户来源必须由
                # 可信调用方在另一个入口产生，不在本工具参数里。
                "origin": "inferred",
                "status": "candidate",
                "created_at": now,
                "expires_at": expires_at,
                "deduped": False,
                "before_state": "absent",
                "after_state": after,
                "content_bytes": len(redacted.encode("utf-8")),
                "content_sha256": _sha(redacted.encode("utf-8")),
                "redacted": redacted != content,
            },
        )

    def _consolidate(self, admitted: AdmittedCall, scope: MemoryScope) -> ToolResult:
        """L2/L3 确定性合成：只读 L1 证据、产出**候选页**（人 confirm 才召回）。"""
        from .memory_distill import consolidate, page_memory_id

        sources = self._projection.consolidatable_sources(scope)
        pages = consolidate(sources, scope=scope)
        # 合成是纯函数：没有可合成的内容就明确返回空页，不写任何行。
        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        now = self._clock()

        # ---- 阶段一：**先把每一页都预检完**，一行正文都还没写 ----
        #
        # 为什么不能"边检边写"：页 id 由 (作用域, 层, 正文) 决定，正文回退会撞回
        # 已退役的 id。若边检边写，前几页的正文已经落盘、后面的页才失败——那些
        # 正文就是没人引用的孤儿，而恢复流程的孤儿清理只认
        # `tool.intent.args.content_sha256`（consolidate 的 args 只有 scope），
        # 清不到它。所以先预检全部，全部合法再统一写。
        planned: list[tuple[Any, str, Any]] = []
        for page in pages:
            memory_id = page_memory_id(scope.to_uri(), page)
            existing = self._projection.get(memory_id, allowed=(scope,))
            if existing is not None and existing.status not in ("candidate", "active"):
                raise TianshuError(
                    f"合成页 {memory_id!r} 已存在且状态是 {existing.status!r}："
                    "同一段正文不能再作为新的候选页写入"
                )
            planned.append((page, memory_id, existing))

        # ---- 阶段二：预检全过，才开始写正文 ----
        out_pages: list[dict[str, Any]] = []
        created = 0
        existing_pages = 0
        for page, memory_id, existing in planned:
            item = page.to_output()
            if existing is not None:
                # candidate / active：这一页已经在库里，不重复写行；output 里如实给
                # 出现有状态（不能一律写 "candidate"——已确认的页会变成"事件说候选、
                # 投影是 active"，审计与后续 confirm 都对不上）。
                item.update(
                    {
                        "memory_id": memory_id,
                        "content_ref": existing.content_ref,
                        "consolidated_at": existing.consolidated_at or now,
                        "status": existing.status,
                        "origin": existing.origin,
                        "existing": True,
                    }
                )
                existing_pages += 1
                out_pages.append(item)
                continue
            content_ref = self._projection.put_content(page.content)
            if not isinstance(content_ref, str) or not content_ref:
                raise TianshuError("记忆投影没有返回 content_ref")
            item.update(
                {
                    "memory_id": memory_id,
                    "content_ref": content_ref,
                    "consolidated_at": now,
                    "status": "candidate",
                    "origin": "inferred",
                    "existing": False,
                }
            )
            out_pages.append(item)
            created += 1
        return ToolResult(
            "succeeded",
            "applied",
            {
                "path": scope.to_uri(),
                "memory_uri": scope.to_uri(),
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "sources": len(sources),
                "pages": out_pages,
                # `created` / `existing` 分开报：重复合成不是"又写了一遍"，
                # 读结果的人要能分清这几页是新写的还是本来就有的。
                "created": created,
                "existing": existing_pages,
                "before_state": "absent",
                "after_state": f"memory:{scope.to_uri()}:l2/l3:{len(out_pages)}",
                "redacted": False,
            },
        )

    def _edit(self, admitted: AdmittedCall, scope: MemoryScope) -> ToolResult:
        """改正文：旧版退役 + 新版立即生效，**一次审批**。

        新版是新 `memory_id`、`revision=1`、`status=active`、`supersedes=旧 id`；
        旧行 `revision` 不变、仍可按 id 查到（"旧版可查"）。不允许改 kind。
        """
        args = admitted.args
        memory_id = args["memory_id"]
        content = args["content"]
        observed = self._observe(memory_id, scope)
        if observed is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        before_status, before_revision = observed
        ok, why = check_request_preconditions(
            admitted.request.action,
            scope.to_uri(),
            admitted.request.preconditions or {},
            memory_observed=observed,
        )
        if not ok:
            raise MemoryDispatchRefused(why, "dispatch.memory_stale_revision")
        if before_status not in ("candidate", "active"):
            raise MemoryDispatchRefused(
                f"记忆当前状态不允许编辑：{before_status!r}",
                "dispatch.memory_stale_revision",
            )
        old = self._projection.get(memory_id, allowed=(scope,))
        if old is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )

        try:
            redacted, blocked = self._projection.redact_or_block(content)
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆写入闸门检查失败：{exc}", "dispatch.refused"
            ) from exc
        if blocked:
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": "新版正文触发 PII/密钥写入闸门，编辑未生效",
                    "path": scope.to_uri(),
                },
            )
        try:
            existing = self._projection.find_duplicate(
                scope=scope, kind=old.kind, content=redacted
            )
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆去重检查失败：{exc}", "dispatch.refused"
            ) from exc
        if existing is not None:
            if existing.memory_id == memory_id:
                raise MemoryDispatchRefused(
                    "新版与旧版正文相同，不需要编辑", "dispatch.refused"
                )
            raise MemoryDispatchRefused(
                "同作用域已有相同正文的记忆", "c4.create_over_existing"
            )

        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        content_ref = self._projection.put_content(redacted)
        if not isinstance(content_ref, str) or not content_ref:
            raise TianshuError("记忆投影没有返回 content_ref")
        new_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = self._clock()
        edit_task = self._tasks.get_task(admitted.request.task_id)
        # origin 描述的是**这一版正文**的来源，不是记忆的终身属性：
        # 控制面 edit（人自己写的新正文）→ user_explicit；模型写、人批准 →
        # user_confirmed。旧版的 origin 留在旧行里，不会被改写。
        author_origin = (
            "user_explicit"
            if edit_task is not None and getattr(edit_task, "is_control", False)
            else "user_confirmed"
        )
        return ToolResult(
            "succeeded",
            "applied",
            {
                "path": scope.to_uri(),
                "memory_uri": scope.to_uri(),
                "memory_id": new_id,
                "revision": 1,
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "kind": old.kind,
                "content_ref": content_ref,
                "source_refs": list(old.source_refs),
                "evidence": [],
                "proof_count": 0,
                "origin": author_origin,
                "status": "active",
                "created_at": now,
                "expires_at": old.expires_at,
                "supersedes": memory_id,
                "deduped": False,
                "before_state": memory_state(memory_id, before_status, before_revision),
                "after_state": memory_state(new_id, "active", 1),
                "content_bytes": len(redacted.encode("utf-8")),
                "content_sha256": _sha(redacted.encode("utf-8")),
                "redacted": redacted != content,
            },
        )

    def _promote(self, admitted: AdmittedCall, scope: MemoryScope) -> ToolResult:
        """把记忆移动（共享）到新作用域：原行退役、目标作用域新建候选。

        `scope` 是**原记忆**的作用域（资源）；目标作用域在参数里。原行立即
        `superseded`（原作用域不再召回），新行 `candidate` 需要人再确认。
        """
        args = admitted.args
        memory_id = args["memory_id"]
        observed = self._observe(memory_id, scope)
        if observed is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        before_status, before_revision = observed
        ok, why = check_request_preconditions(
            admitted.request.action,
            scope.to_uri(),
            admitted.request.preconditions or {},
            memory_observed=observed,
        )
        if not ok:
            raise MemoryDispatchRefused(why, "dispatch.memory_stale_revision")
        if before_status not in ("candidate", "active"):
            raise MemoryDispatchRefused(
                f"记忆当前状态不允许提升：{before_status!r}",
                "dispatch.memory_stale_revision",
            )
        old = self._projection.get(memory_id, allowed=(scope,))
        if old is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        try:
            new_scope = build_memory_scope(args.get("scope"), args.get("scope_id"))
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"提升目标作用域不合法：{exc}", "c4.memory_scope_denied"
            ) from exc
        if (new_scope.scope, new_scope.scope_id) == (old.scope, old.scope_id):
            raise MemoryDispatchRefused(
                "提升目标作用域与原作用域相同", "c4.memory_scope_denied"
            )
        content = self._projection.read_content(old)
        if content is None:
            raise MemoryDispatchRefused(
                "记忆正文不可用，不能提升（先恢复正文再搬）", "dispatch.refused"
            )
        try:
            existing = self._projection.find_duplicate(
                scope=new_scope, kind=old.kind, content=content
            )
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆去重检查失败：{exc}", "dispatch.refused"
            ) from exc
        if existing is not None:
            raise MemoryDispatchRefused(
                f"目标作用域已有相同正文的记忆：{existing.memory_id}",
                "c4.create_over_existing",
            )

        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        new_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = self._clock()
        source_refs = list(old.source_refs)
        if memory_id not in source_refs:
            source_refs.append(memory_id)
        return ToolResult(
            "succeeded",
            "applied",
            {
                "path": new_scope.to_uri(),
                "memory_uri": new_scope.to_uri(),
                "memory_id": new_id,
                "receipt_memory_id": memory_id,
                "revision": 1,
                "scope": new_scope.scope,
                "scope_id": new_scope.scope_id,
                "kind": old.kind,
                # 正文没有变：复用同一个内容寻址 blob，不复制字节。
                "content_ref": old.content_ref,
                "source_refs": source_refs,
                "evidence": [],
                "proof_count": 0,
                # 搬的是同一段正文：来源随行，**不**因为换了作用域就降档。
                "origin": old.origin,
                "status": "candidate",
                "created_at": now,
                "expires_at": old.expires_at,
                "supersedes": memory_id,
                "deduped": False,
                "before_state": memory_state(memory_id, before_status, before_revision),
                "after_state": memory_state(new_id, "candidate", 1),
            },
        )

    def _erase(self, admitted: AdmittedCall, scope: MemoryScope) -> ToolResult:
        """物理擦除：删正文副本（引用计数为 0 才删）+ 标 `erased_at`。

        顺序写死：**先删文件 → 再返回结果**（结果由 dispatcher 与事件同事务提交）。
        崩在两者之间只会留下"文件没了、行还没标擦除"，重建时正文如实不可用；
        反过来（先标擦除、文件还在）才是隐私泄漏。
        """
        args = admitted.args
        memory_id = args["memory_id"]
        observed = self._observe(memory_id, scope)
        if observed is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        before_status, before_revision = observed
        ok, why = check_request_preconditions(
            admitted.request.action,
            scope.to_uri(),
            admitted.request.preconditions or {},
            memory_observed=observed,
        )
        if not ok:
            raise MemoryDispatchRefused(why, "dispatch.memory_stale_revision")
        old = self._projection.get(memory_id, allowed=(scope,))
        if old is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        if old.erased_at is not None:
            raise MemoryDispatchRefused(
                f"记忆已经擦除过：{memory_id}", "dispatch.memory_stale_revision"
            )

        others_count, others = self._projection.count_content_ref(
            old.content_ref, exclude_memory_id=memory_id
        )
        deleted = others_count == 0
        deleted_bytes = 0
        if deleted:
            path = self._projection.blob_path(old.content_ref)
            if path is not None:
                try:
                    deleted_bytes = path.stat().st_size
                    path.unlink()
                except FileNotFoundError:
                    # 文件已经不在了：幂等对待（正文本来就不可用）。
                    deleted_bytes = 0
                except OSError as exc:
                    # 删文件失败：**不**标 erased_at，让整次擦除如实失败。
                    raise MemoryDispatchRefused(
                        f"擦除失败：正文文件删不掉（{exc}）", "dispatch.refused"
                    ) from exc

        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        revision = before_revision + 1
        now = self._clock()
        erasure = {
            "content_ref": old.content_ref,
            "deleted": deleted,
            "deleted_bytes": deleted_bytes,
            "retained_by": others,
        }
        return ToolResult(
            "succeeded",
            "applied",
            {
                "path": scope.to_uri(),
                "memory_uri": scope.to_uri(),
                "memory_id": memory_id,
                "revision": revision,
                "before_status": before_status,
                "status": "forgotten",
                "erased_at": now,
                "before_state": memory_state(memory_id, before_status, before_revision),
                "after_state": memory_state(memory_id, "forgotten", revision),
                "erasure": erasure,
                "content_bytes": deleted_bytes,
            },
        )

    def _transition(
        self, admitted: AdmittedCall, scope: MemoryScope, *, to_status: str
    ) -> ToolResult:
        args = admitted.args
        memory_id = args["memory_id"]
        observed = self._observe(memory_id, scope)
        if observed is None:
            raise MemoryDispatchRefused(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        before_status, before_revision = observed
        ok, why = check_request_preconditions(
            admitted.request.action,
            scope.to_uri(),
            admitted.request.preconditions or {},
            memory_observed=observed,
        )
        if not ok:
            raise MemoryDispatchRefused(why, "dispatch.memory_stale_revision")

        if before_status not in ("candidate", "active"):
            raise MemoryDispatchRefused(
                f"记忆当前状态不允许迁移：{before_status!r}",
                "dispatch.memory_stale_revision",
            )
        if to_status == "active" and before_status != "candidate":
            raise MemoryDispatchRefused(
                f"只能确认候选记忆：当前 status={before_status!r}",
                "dispatch.memory_stale_revision",
            )
        if to_status == "forgotten" and before_status == "forgotten":
            raise MemoryDispatchRefused(
                "记忆已经是 forgotten，不能重复遗忘",
                "dispatch.memory_stale_revision",
            )

        # 所有确定性拒绝都发生在标记 executing 之前；标记之后只构造结果，
        # 真正的投影更新由 Dispatcher 在 tool.result 同事务里完成。
        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        revision = before_revision + 1
        now = self._clock()
        after = memory_state(memory_id, to_status, revision)
        before = memory_state(memory_id, before_status, before_revision)
        out: dict[str, Any] = {
            "path": scope.to_uri(),
            "memory_uri": scope.to_uri(),
            "memory_id": memory_id,
            "revision": revision,
            "before_status": before_status,
            "status": to_status,
            "before_state": before,
            "after_state": after,
        }
        if to_status == "active":
            out["consolidated_at"] = now
            # 契约 v1.2：人批准 confirm → 把 inferred 升为 user_confirmed。
            # 出处是审批记录（只有 HumanIdentity 能批准），不是任何参数。
            approval = (
                self._approvals.get(admitted.approval_id)
                if admitted.approval_id
                else None
            )
            if approval is not None and getattr(approval, "decided_by", None):
                out["origin"] = "user_confirmed"
        return ToolResult("succeeded", "applied", out)

    def _remember(self, admitted: AdmittedCall, scope: MemoryScope) -> ToolResult:
        """控制面"人直接记住"：创建即 active + origin=user_explicit。

        与 `_propose` 共用写入闸门与去重；区别只有一个：不需要再走 confirm——
        人敲的命令就是授权。`_verify` 已经核过 `Task.is_control`。
        """
        args = admitted.args
        kind = args["kind"]
        content = args["content"]
        source_refs = _string_list(args.get("source_refs"), "source_refs")
        expires_at = args.get("expires_at")
        if expires_at is not None and (
            not isinstance(expires_at, int) or isinstance(expires_at, bool)
        ):
            raise ToolArgumentError("expires_at 必须是整数毫秒时间戳")

        try:
            redacted, blocked = self._projection.redact_or_block(content)
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆写入闸门检查失败：{exc}", "dispatch.refused"
            ) from exc
        if blocked:
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": "记忆内容触发 PII/密钥写入闸门，记忆未写入",
                    "path": scope.to_uri(),
                },
            )
        try:
            existing = self._projection.find_duplicate(
                scope=scope, kind=kind, content=redacted
            )
        except TianshuError as exc:
            raise MemoryDispatchRefused(
                f"记忆去重检查失败：{exc}", "dispatch.refused"
            ) from exc
        if existing is not None:
            raise MemoryDispatchRefused(
                "目标记忆已存在（同一作用域 + 种类 + 正文）",
                "c4.create_over_existing",
            )

        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        content_ref = self._projection.put_content(redacted)
        if not isinstance(content_ref, str) or not content_ref:
            raise TianshuError("记忆投影没有返回 content_ref")
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = self._clock()
        return ToolResult(
            "succeeded",
            "applied",
            {
                "path": scope.to_uri(),
                "memory_uri": scope.to_uri(),
                "memory_id": memory_id,
                "revision": 1,
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "kind": kind,
                "content_ref": content_ref,
                "source_refs": source_refs,
                "evidence": [],
                "proof_count": 0,
                # 人本人签发的记忆：直接可召回。模型参数里没有 origin，
                # 这个值由执行器按工具身份写死。
                "origin": "user_explicit",
                "status": "active",
                "created_at": now,
                "expires_at": expires_at,
                "deduped": False,
                "before_state": "absent",
                "after_state": memory_state(memory_id, "active", 1),
                "content_bytes": len(redacted.encode("utf-8")),
                "content_sha256": _sha(redacted.encode("utf-8")),
                "redacted": redacted != content,
            },
        )


def _string_list(raw: Any, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ToolArgumentError(f"{label} 必须是字符串数组")
    return list(raw)


def _object_list(raw: Any, label: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ToolArgumentError(f"{label} 必须是对象数组")
    return [dict(item) for item in raw]


__all__ = [
    "MEMORY_ALL_TOOL_IDS",
    "MEMORY_READ_TOOL_IDS",
    "MEMORY_TOOL_IDS",
    "MemoryDispatchRefused",
    "MemoryExecutor",
    "memory_scope_allowed",
    "memory_state",
]