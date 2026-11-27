"""派发层执行侧身份、租约、策略前置条件、审批消费与目标解析（内部 Mixin，由 `Dispatcher` 继承）。

所有授权与前置核验均在执行侧完成；测试对 `tianshu.dispatcher` 模块级守卫/隔离探针的
monkeypatch 通过 `sys.modules["tianshu.dispatcher"]` 在调用期动态绑定，零契约变更。
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from ._dispatcher_types import (
    DISPATCH_INTENT,
    STATE_ADMITTED,
    DispatchDenied,
    DispatchError,
    StoredRequest,
    _Authority,
    _FileTarget,
    _McpTarget,
    _MemoryTarget,
    _NetworkTarget,
    _SubtaskTarget,
    _Target,
    resolve_scheme_resource,
)
from .browser_forms import SUBMIT_TOOL
from .browser_navigation import BROWSER_TOOL, verify_navigation, verify_submission
from .disk_admission import DiskAdmissionError
from .errors import (
    RunFenced,
    TianshuError,
    ToolArgumentError,
    WorkspaceBlocked,
    WorkspaceViolation,
)
from .events import EventLog, canonical_bytes, sha256_hex
from .exec_backends import ExecBackend, exec_policy_for_backend
from .execpolicy import (
    ISOLATION_NONE,
    PolicyDenied,
    PolicyUnsupported,
    check_capabilities,
    check_spawn,
    filtered_env,
)
from .guard import ensure_not_blocked
from .isolation import isolation_view, probe_backend_cached
from .jobs import JobClaim, JobLost, JobStore
from .leases import LeaseLost, LeaseStore
from .mcp import McpClients
from .memory_exec import MEMORY_TOOL_IDS, memory_scope_allowed
from .permissions import (
    ACTION_CREATE,
    ACTION_OVERWRITE,
    APPROVAL_ACTIONS,
    ActionRequest,
    ApprovalStore,
    GrantStore,
    PolicyEngine,
    supported_memory_kinds,
)
from .resources import (
    KIND_MCP,
    KIND_MEMORY,
    KIND_NETWORK,
    KIND_SUBTASK,
    ResourceError,
    ResourceRef,
    build_memory_scope,
    network_origin_allowed,
    parse_memory_uri,
    parse_resource,
)
from .run_policy import RunEffectsDisallowed
from .sandbox import current_backend
from .snapshot import SnapshotStore
from .tasks import (
    RUN_RUNNING,
    SUBTASK_GOAL_MAX_BYTES,
    SUBTASK_MAX_PER_ROOT_TASK,
    SUBTASK_MAX_UNFINISHED_PER_RUN,
    ExecutionPermit,
    Run,
    Task,
    TaskStore,
    clean_subtask_limits,
)
from .tool_registry import ToolRegistry
from .tools import (
    NETWORK_TOOL_IDS,
    SUBTASK_TOOL_IDS,
    AdmittedCall,
    check_request_preconditions,
)
from .web_search import SEARCH_TOOL, search_target
from .workspace import ResourceIdentity, Workspace, WorkspaceBinding, overlap_key


class _DispatcherAuthorityMixin:
    """工具身份、Run 资格、exec 隔离预检、作用域/白名单/配额授权与票据消费。"""

    _log: EventLog
    _tasks: TaskStore
    _leases: LeaseStore
    _approvals: ApprovalStore
    _grants: GrantStore
    _consents: Any
    _claims: JobStore
    _snapshots: SnapshotStore | None
    _data_root: Path | None
    _clock: Any
    _guard_timeout_ms: int
    _memory: Any
    _contents: Any
    _mcp_clients: McpClients | None
    _exec_backend: ExecBackend
    registry: ToolRegistry

    def _check_tool_identity(self, request: ActionRequest):
        spec = self.registry.spec(request.tool_id)
        if spec is None or (
            request.tool_version != spec.version or request.action != spec.action
            or request.schema_hash != sha256_hex(canonical_bytes(spec.input_schema))
            or (request.description_hash
                and request.description_hash != spec.description_digest())
        ):
            raise DispatchDenied("工具身份与当前真实 ToolSpec 不符", "dispatch.not_admitted")
        return spec

    def _check_run_execution(self, request: ActionRequest, target: _Target) -> None:
        spec = self._check_tool_identity(request)
        if spec.side_effect_level == "pure":
            self._tasks.run_execution_policy(request.run_id)
            return
        try:
            self._tasks.require_effects_allowed(request.run_id)
            if (request.tool_id == "subtask.cancel" and isinstance(target, _SubtaskTarget)
                    and self._tasks.get_subtask(request.run_id, target.subtask_id) is not None):
                return
            self._tasks.disk_admission.assert_stores(
                self._log, snapshots=self._snapshots, memory=self._memory,
                contents=self._contents, data_root=self._data_root,
            )
            targets = (target.path, target.path.parent) if isinstance(target, _FileTarget) else ()
            self._tasks.require_effect_space(
                request.run_id, targets=targets, data_root=self._data_root,
            )
        except (DiskAdmissionError, RunEffectsDisallowed) as exc:
            raise DispatchDenied(str(exc), exc.rule_id) from exc

    def _cancel_requested(self, run_id: str) -> bool:
        """Run 是否已被要求停下（cancel / pause）。

        暂停也按"不再派发新副作用"处理：网络请求有成本、有外发，不能因为用户
        点了暂停还偷偷再发一个。已经发出的请求无法撤回——这条如实写在文档里。
        """
        from .tasks import CONTROL_REQUESTS

        run = self._tasks.get_run(run_id)
        if run is None:
            return False
        action = getattr(run, "control_requested", None)
        return action in CONTROL_REQUESTS


    # ---- 内部：核验 ----

    def _precheck_exec(
        self, request: ActionRequest, args: dict[str, Any], target: Path
    ) -> dict[str, Any] | None:
        """在发起审批**之前**跑一遍 F2 的判定。

        两件事必须在审批前知道，否则用户会为一条注定跑不了的命令签字：
        - argv / cwd / env 是否被 exec 策略允许（`PolicyDenied`）；
        - 本次请求的执行方式后端做不做得到（`PolicyUnsupported`）。

        这里调用的是 F2 的同一个 `check_spawn()`，不是第二套策略实现；执行时
        `spawn()` 会再核一次（两边看到的必须是同一个世界）。
        """
        self._verify_verification_command(request, args)
        task = self._tasks.get_task(request.task_id)
        policy = task.exec_policy if task is not None else None
        if policy is None:
            # 没配置策略：交给 classify 报 c4.exec_not_configured。
            return None

        policy = exec_policy_for_backend(policy, self._exec_backend)
        require_isolation_requirements = (
            sys.modules["tianshu.dispatcher"].require_isolation_requirements
        )
        argv = args.get("argv")
        try:
            check_spawn(
                policy,
                argv,
                target,
                filtered_env(policy, dict(os.environ)),
            )
            if args.get("pty"):
                if not policy.allow_pty:
                    raise PolicyDenied(
                        "本次调用请求了 PTY，但策略没有允许"
                        "（ExecPolicy.allow_pty=False）",
                        rule_id="f2.pty_not_allowed",
                    )
                check_capabilities(policy, self._exec_backend.capabilities)
            # 预检与执行必须用**同一个探测现场**：执行时的 workspace 是
            # `args["cwd"]` 解析后的目录（`_write_target`），不是请求目标本身。
            # 解析失败（越界/非法类型）必须变成**可解释的拒绝**，不能让
            # `prepare()` 抛裸 `WorkspaceViolation` 把整个 Run 收成 failed。
            try:
                probe_workspace = Workspace(target).resolve(args.get("cwd", "."))
            except (
                WorkspaceViolation, ToolArgumentError, OSError, ValueError, TypeError
            ) as exc:
                raise DispatchDenied(
                    f"执行目录不可用：{exc}", "c4.outside_workspace"
                ) from exc
            report = require_isolation_requirements(
                policy,
                self._exec_backend,
                workspace=probe_workspace,
                data_root=self._data_root,
            )
            if (
                report is None
                and not tuple(policy.isolation_requires)
                and self._exec_backend.capabilities.isolation != ISOLATION_NONE
                and self._data_root is not None
            ):
                report = probe_backend_cached(
                    self._exec_backend,
                    workspace=probe_workspace,
                    data_root=self._data_root,
                )
        except PolicyDenied as exc:
            raise DispatchDenied(
                f"exec 策略拒绝：{exc}", "c4.exec_policy_denied"
            ) from exc
        except PolicyUnsupported as exc:
            raise DispatchDenied(
                f"exec 后端不支持：{exc}", "c4.exec_unsupported"
            ) from exc
        return isolation_view(self._exec_backend, report)

    def _verify_verification_command(self, request, args):
        if request.tool_id != "run_command":
            return
        from types import SimpleNamespace

        from .verification_driver import assert_execution_binding
        try:
            assert_execution_binding(
                SimpleNamespace(log=self._log, tasks=self._tasks, contents=self._contents,
                                dispatcher=self),
                request.run_id, request.call_id, args,
            )
        except (ValueError, OSError, IndexError) as exc:
            raise DispatchDenied(
                f"验证命令前置条件失效：{exc}", "dispatch.stale_precondition",
            ) from exc

    @contextlib.contextmanager
    def _begin_memory_execution(self, request: ActionRequest, claim: JobClaim):
        """记忆写入的执行守卫。

        与 `TaskStore.begin_execution()` 做同样三件事——拿执行守卫、核 Run 状态、
        写 `execution.started`——但**不核目录租约**：记忆作用域不是目录。租约
        被跳过的那部分授权，由 `_authority()` 与 `MemoryExecutor._verify()` 的
        作用域核验补上。
        """
        root = self._require_data_root()
        execution_guard = sys.modules["tianshu.dispatcher"].execution_guard
        with execution_guard(root, timeout_ms=self._guard_timeout_ms):
            self._claims.assert_claim(claim)
            run = self._tasks.assert_current(request.run_id)
            if run.state != RUN_RUNNING:
                raise RunFenced(
                    f"run 不在执行状态，拒绝开始写入：{request.run_id}"
                    f"（state={run.state}）"
                )
            auth = self._authority(request, claim, args=self.get_request(request.request_id).args)
            self._check_run_execution(request, self._resolve_target(request, auth))
            started_at = self._clock()
            with self._log.transaction() as tx:
                seq = tx.append(
                    request.run_id,
                    "execution.started",
                    {
                        "run_id": request.run_id,
                        "task_id": request.task_id,
                        "goal_revision": run.goal_revision,
                        "lease_epoch": run.lease_epoch,
                        "resources": list(request.resources),
                    },
                    task_id=request.task_id,
                )
            yield ExecutionPermit(
                run_id=run.run_id,
                task_id=run.task_id,
                goal_revision=run.goal_revision,
                lease_epoch=run.lease_epoch,
                resources=tuple(request.resources),
                seq=seq,
                started_at=started_at,
            )

    def _verify_resource_execution(self, admitted: AdmittedCall) -> _Authority:
        """非文件资源在守卫与写事务内独立核票，不信任 intent 时的判定。"""
        request = admitted.request
        self._typecheck(request, admitted.claim, admitted.args)
        if (request.digest() != admitted.request_digest
                or sha256_hex(canonical_bytes(admitted.args)) != request.args_hash):
            raise DispatchDenied("执行票据摘要不匹配", "dispatch.not_admitted")
        stored = self.get_request(request.request_id)
        if (stored is None or stored.request.digest() != admitted.request_digest
                or stored.state != STATE_ADMITTED
                or sha256_hex(canonical_bytes(stored.args)) != request.args_hash):
            raise DispatchDenied("权威请求已改变或不可执行", "dispatch.not_admitted")
        auth = self._authority(request, admitted.claim, args=stored.args)
        if self._cancel_requested(request.run_id):
            raise DispatchDenied("Run 已取消或暂停", "dispatch.not_running")
        row = self.dispatch_record(request.run_id, request.call_id)
        if row is None or row.state != DISPATCH_INTENT:
            raise DispatchDenied(
                "派发已被撤销或不再处于 intent", "dispatch.approval_revoked"
            )
        if (row.dispatch_id != admitted.dispatch_id
                or row.request_digest != admitted.request_digest
                or row.grant_id != admitted.grant_id
                or row.approval_id != admitted.approval_id
                or row.tool_id != request.tool_id or row.action != request.action
                or row.claim_epoch != admitted.claim.claim_epoch
                or row.owner_instance_id != admitted.claim.owner_instance_id):
            raise DispatchDenied("派发记录与执行票据不匹配", "dispatch.not_admitted")
        grant = self._grants.get(admitted.grant_id)
        if (grant is None or grant.request_digest != admitted.request_digest
                or grant.subject != f"{request.run_id}#{request.claim_epoch}"
                or grant.remaining_uses != 0 or grant.expires_at <= self._clock()):
            raise DispatchDenied("执行许可已失效或不匹配", "dispatch.not_admitted")
        target = self._resolve_target(request, auth)
        if self._requires_approval(request, target, stored.args) and not admitted.approval_id:
            raise DispatchDenied("执行票据缺少审批", "c4.no_approval")
        if admitted.approval_id:
            approval = self._approvals.get(admitted.approval_id)
            if approval is None or approval.state != "consumed":
                raise DispatchDenied("审批未消费或已撤销", "dispatch.approval_revoked")
            if approval.run_id != request.run_id:
                raise DispatchDenied("审批不属于本 Run", "c4.approval_run_mismatch")
            if (approval.action_digest or approval.request_digest) != (
                request.action_digest() if approval.action_digest else request.digest()
            ):
                raise DispatchDenied("审批动作摘要不匹配", "c4.approval_mismatch")
            if approval.expires_at <= self._clock():
                raise DispatchDenied("审批已过期", "c4.approval_expired")
        self._check_run_execution(request, target)
        return auth

    def _start_resource_execution(self, request: ActionRequest, dispatch_id: str) -> None:
        """调用方已持守卫与事务；标记与复核在同一提交中生效。"""
        if not self._advance_to_executing(dispatch_id):
            raise DispatchDenied("派发已不能执行", "dispatch.approval_revoked")
        self._append(request, "execution.started", {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "goal_revision": request.goal_revision,
            "lease_epoch": request.lease_epoch,
            "resources": list(request.resources),
        })

    def _policy_preconditions(
        self, request: ActionRequest, target: _Target, args: dict[str, Any]
    ) -> dict[str, Any]:
        """策略层需要的"执行前状态"。

        文件动作直接用请求里绑定的 preconditions，**一个字都不补**——那些值
        是 `build_request()` 当场从磁盘读出来、进过 action_digest 的。记忆动作
        则必须由派发层从权威投影补两项：种类是否存在、目标记忆是否已存在/
        它的 origin 是什么。它们只用于策略判定，不进 action_digest（记忆的
        审批绑定仍靠 memory_id/revision/args_hash）。
        """
        pre = dict(request.preconditions or {})
        if not isinstance(target, _MemoryTarget):
            return pre

        projection = self._memory_projection()
        scope = target.scope
        # 控制面声明的作用域（只对 is_control 的任务生效）。**无条件覆盖**请求
        # 自带的同名键：这个值只能由派发层从 task 投影算出来，不能由调用方填。
        task = self._tasks.get_task(request.task_id)
        is_control = bool(getattr(task, "is_control", False)) if task else False
        pre["memory_declared_scopes"] = (
            list(getattr(task, "memory_scopes", ()) or ()) if is_control else []
        )
        pre["memory_is_control"] = is_control
        pre["memory_readonly_task"] = bool(task and task.depth)
        # 策略要区分"哪个记忆工具"：`user_explicit` 的免审批只属于 confirm，
        # 不属于 edit / promote（它们改的是长期召回的正文或作用域）。
        pre["memory_tool"] = request.tool_id
        if request.action == ACTION_CREATE:
            kind = args.get("kind")
            pre["memory_kind"] = kind if isinstance(kind, str) else ""
            pre["memory_exists"] = False
            content = args.get("content")
            if (
                isinstance(kind, str)
                and kind in supported_memory_kinds()
                and isinstance(content, str)
            ):
                try:
                    redacted, blocked = projection.redact_or_block(content)
                except (AttributeError, TianshuError):
                    redacted, blocked = content, False
                if not blocked:
                    try:
                        pre["memory_exists"] = (
                            projection.find_duplicate(
                                scope=scope, kind=kind, content=redacted
                            )
                            is not None
                        )
                    except TianshuError:
                        # 投影读失败不是"目标不存在"的证明；保持 False 让
                        # 策略先放行候选，执行侧会用同一投影再判一次并拒绝。
                        pre["memory_exists"] = False
            return pre

        memory_id = args.get("memory_id")
        if isinstance(memory_id, str) and memory_id:
            try:
                memory = projection.get(memory_id, allowed=(scope,))
            except TianshuError:
                # 读不到 origin 时**不能**推断 user_explicit：宁可要求审批。
                memory = None
            pre["memory_exists"] = memory is not None
            pre["memory_origin"] = (
                str(getattr(memory, "origin", "")) if memory is not None else ""
            )
        else:
            pre["memory_exists"] = False
            pre["memory_origin"] = ""
        return pre

    def _check_memory_preconditions(
        self,
        request: ActionRequest,
        target: _MemoryTarget,
        args: dict[str, Any],
    ) -> None:
        """记忆的 status/revision 从权威投影读；不成立就抛确定的拒绝。"""
        task = self._tasks.get_task(request.task_id)
        is_control = bool(getattr(task, "is_control", False)) if task else False
        if not is_control and request.tool_id in (
            "memory.remember",
            "memory.promote",
            "memory.erase",
        ):
            # 契约 v1.2：控制面专属工具不能在普通任务里执行。拒绝点在派发层，
            # 执行侧还有一次独立核验（memory_exec._verify）。
            raise DispatchDenied(
                f"控制面专属工具不能在普通任务里执行：{request.tool_id}",
                "c4.memory_control_scope_binding",
            )
        if is_control and request.tool_id == "memory.promote":
            # 提升目标作用域必须由控制 Run 显式声明（user 恒可）。
            try:
                new_scope = build_memory_scope(
                    args.get("scope"), args.get("scope_id")
                )
            except TianshuError as exc:
                raise DispatchDenied(
                    f"提升目标作用域不合法：{exc}", "c4.memory_scope_denied"
                ) from exc
            declared = list(getattr(task, "memory_scopes", ()) or ())
            if new_scope.to_uri() == target.uri:
                raise DispatchDenied(
                    "提升目标作用域与原作用域相同", "c4.memory_scope_denied"
                )
            if new_scope.scope != "user" and new_scope.to_uri() not in declared:
                raise DispatchDenied(
                    f"提升目标作用域不在控制 Run 的声明内：{new_scope.to_uri()}",
                    "c4.memory_scope_denied",
                )
        if (
            request.tool_id in ("memory.propose", "memory.consolidate")
            and target.scope.scope == "workspace"
        ):
            # workspace 是共享边界：模型可见的 propose **无条件**拒，不因
            # Task.is_control 放行（控制面标记会被同一 Task 上后开的模型 Run
            # 继承）。执行侧还有一道同名核验，这里只是把拒绝提前到派发。
            raise DispatchDenied(
                f"workspace 作用域只能由人经 memory.promote 进入："
                f"{target.scope.to_uri()}",
                "c4.memory_scope_denied",
            )
        if request.action == ACTION_CREATE:
            return

        memory_id = args.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            raise DispatchDenied(
                "memory_id 必须是非空字符串", "dispatch.memory_not_found"
            )
        declared = (request.preconditions or {}).get("memory_id")
        if declared is not None and declared != memory_id:
            raise DispatchDenied(
                f"请求声明的 memory_id 与参数不一致：{declared!r} ≠ {memory_id!r}",
                "dispatch.memory_resource_mismatch",
            )
        try:
            observed = self._memory_projection().observe(
                memory_id, allowed=(target.scope,)
            )
        except TianshuError as exc:
            raise DispatchDenied(
                f"记忆观察失败：{exc}", "dispatch.memory_not_found"
            ) from exc
        if observed is None:
            raise DispatchDenied(
                f"记忆不存在或不属于当前作用域：{memory_id}",
                "dispatch.memory_not_found",
            )
        ok, reason = check_request_preconditions(
            request.action,
            target.uri,
            request.preconditions or {},
            memory_observed=observed,
        )
        if not ok:
            raise DispatchDenied(reason, "dispatch.memory_stale_revision")
        if request.tool_id == "memory.erase":
            # 已擦除 = 确定拒绝，别让人为一次注定失败的擦除签字。
            row = self._memory_projection().get(memory_id, allowed=(target.scope,))
            if row is not None and row.erased_at is not None:
                raise DispatchDenied(
                    f"记忆已经擦除过：{memory_id}", "dispatch.memory_stale_revision"
                )

    def _typecheck(
        self, request: ActionRequest, claim: JobClaim, args: dict[str, Any]
    ) -> None:
        if not isinstance(request, ActionRequest):
            raise DispatchError("request 必须是 ActionRequest")
        if not isinstance(claim, JobClaim):
            raise DispatchError("必须传 JobClaim")
        if not isinstance(args, dict):
            raise DispatchError("args 必须是对象")
        if (
            request.action not in APPROVAL_ACTIONS
            and request.action != ACTION_CREATE
            # 子任务工具即使 action=read 也必须走派发：父任务身份、父 Run
            # 作用域与"子任务是否存在"都在 `_authority_subtask()` 里判。
            # 绕过派发就等于把"不存在"伪装成空结果。
            and request.tool_id not in SUBTASK_TOOL_IDS
        ):
            raise DispatchError(
                f"只读动作不经过派发（没有可授权的副作用）：{request.action}"
            )
        if request.tool_id not in self.registry:
            raise DispatchError(f"工具不在白名单：{request.tool_id}")

    def _authority(
        self,
        request: ActionRequest,
        claim: JobClaim,
        *,
        args: dict[str, Any] | None = None,
        mcp_discovery: bool = False,
    ) -> _Authority:
        from .goal_v2 import (
            GoalAdmissionRefused,
            action_denied,
            phase_admission,
            record_admission_refused,
        )
        category = phase_admission(
            self._log, request.run_id, clock=self._clock,
            runtime=self._leases.identity.instance_id)
        if category:
            record_admission_refused(
                self._log, request.run_id, GoalAdmissionRefused(category),
                source="dispatch", request_id=request.request_id)
            raise DispatchDenied("Goal 当前不允许新的副作用：" + category, "dispatch.run_fenced")
        if action_denied(self._log, request):
            raise DispatchDenied("Goal 内该具体动作已被人否决", "c4.approval_rejected")
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

        # G2 委派 D2（规格 §2.6）：子 Run 每次派发前核验父目标是否还是 spawn
        # 时那个版本。放在资源解析之前是刻意的——父目标已经改了，这次调用
        # 无论指向文件、记忆还是网络都不该再被当作旧目标下的一部分执行。
        # 一旦发现不一致：子任务置 `fenced`（结果不回填父 Run，也不写
        # `subtask.finished`），并给子 Run 留下取消请求，让它在安全点停下。
        if run.parent_run_id:
            fence_reason = self._tasks.parent_fence_reason(run.run_id)
            if fence_reason is not None:
                self._tasks.fence_child_run(run.run_id, reason=fence_reason)
                raise DispatchDenied(fence_reason, "dispatch.parent_fenced")

        if not request.resources:
            raise DispatchDenied("动作没有声明资源", "c4.no_resource")

        bindings = list(task.workspaces or [])
        # POSIX 上 `/a/b://c` 是合法绝对路径；判序必须与 resources.py 一致：
        # 绝对路径优先判为 file，只有非绝对路径才按 scheme 分流。
        scheme_resources = [
            r
            for r in request.resources
            if isinstance(r, str) and not os.path.isabs(r) and "://" in r
        ]
        if scheme_resources:
            try:
                parsed = [parse_resource(r) for r in request.resources]
            except ResourceError as exc:
                raise DispatchDenied(
                    f"资源引用不合法：{exc}", "c4.unsupported"
                ) from exc
            if len(parsed) != 1:
                raise DispatchDenied(
                    "一次动作只能声明一个带 scheme 的资源", "c4.multi_resource"
                )
            ref = parsed[0]
            if ref.kind == KIND_MEMORY:
                if request.tool_id not in MEMORY_TOOL_IDS:
                    raise DispatchDenied(
                        f"非记忆工具收到记忆资源：{request.tool_id} → "
                        f"{request.resources[0]!r}",
                        "dispatch.memory_resource_mismatch",
                    )
                if task.depth or run.parent_run_id:
                    raise DispatchDenied(
                        "只读子任务不能写入记忆或发起记忆审批", "c4.memory_scope_denied"
                    )
                scope = ref.memory_scope
                is_control = bool(getattr(task, "is_control", False))
                declared = (
                    tuple(getattr(task, "memory_scopes", ()) or ())
                    if is_control
                    else ()
                )
                allowed, reason = memory_scope_allowed(
                    scope,
                    task_id=request.task_id,
                    bindings=bindings,
                    declared=declared,
                )
                if not allowed:
                    # 租约是给目录的；记忆跳过租约，但**不跳过**作用域授权。
                    raise DispatchDenied(reason, "c4.memory_scope_denied")
                return _Authority(run, task, bindings, [], [])
            if ref.kind == KIND_NETWORK:
                return self._authority_network(request, task, run, bindings, ref, args or {})
            if ref.kind == KIND_MCP:
                return self._authority_mcp(
                    request, task, run, bindings, ref, discovery=mcp_discovery,
                )
            if ref.kind == KIND_SUBTASK:
                return self._authority_subtask(
                    request, task, run, bindings, ref, args or {}
                )
            raise DispatchDenied(
                f"{ref.kind} 资源的执行器尚未实现（G2），拒绝派发：{ref.uri}",
                "dispatch.resource_kind_unsupported",
            )

        if request.tool_id in SUBTASK_TOOL_IDS:
            # 反向不匹配：子任务工具拿了文件/记忆资源。没有 `subtask://`
            # 就没有可判定的父作用域，一律拒绝而不是回退成文件动作。
            raise DispatchDenied(
                f"子任务工具必须使用 subtask:// 资源：{request.tool_id} → "
                f"{request.resources[0]!r}",
                "dispatch.subtask_rejected",
            )

        if request.tool_id in MEMORY_TOOL_IDS:
            raise DispatchDenied(
                f"记忆工具必须使用 memory:// 资源：{request.resources[0]!r}",
                "dispatch.memory_resource_mismatch",
            )

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

    def _authority_network(
        self,
        request: ActionRequest,
        task: Task,
        run: Run,
        bindings: list[WorkspaceBinding],
        ref: ResourceRef,
        args: dict[str, Any],
    ) -> _Authority:
        """网络资源的授权：任务级白名单 + 工具归属。

        这是**第一道**门（"这个任务准不准出网、能去哪几个源"），第二道是逐次
        人工批准（`c4.outbound`）。两道门都在 `prepare()` 里走完，所以白名单
        不通过的任务**不会**被弹一个"批了也发不出去"的批准。

        网络没有目录租约、没有 workspace 绑定：零目录的研究任务合法。
        """
        if request.tool_id not in NETWORK_TOOL_IDS:
            raise DispatchDenied(
                f"非网络工具收到网络资源：{request.tool_id} → {ref.uri}",
                "dispatch.network_resource_mismatch",
            )
        if request.tool_id == BROWSER_TOOL:
            try:
                verify_navigation(self._log, run.run_id, args, ref.uri)
            except ToolArgumentError as exc:
                raise DispatchDenied(str(exc), "dispatch.network_resource_mismatch") from exc
        if request.tool_id == SEARCH_TOOL:
            try:
                expected = search_target(args).to_uri()
            except ToolArgumentError as exc:
                raise DispatchDenied(str(exc), "dispatch.network_resource_mismatch") from exc
            if expected != ref.uri:
                raise DispatchDenied("搜索词或搜索源与批准目标不一致",
                                     "dispatch.network_resource_mismatch")
        target = ref.network_target
        declared = tuple(getattr(task, "outbound_allow", ()) or ())
        if not declared:
            raise DispatchDenied(
                "任务没有声明任何外发白名单（outbound_allow），拒绝出网："
                f"{target.origin_uri}（创建任务时用 --allow-outbound 显式声明）",
                "c4.outbound_not_allowed",
            )
        if not network_origin_allowed(target, declared):
            raise DispatchDenied(
                f"外发目标 {target.origin_uri} 不在任务的白名单里"
                f"（允许：{list(declared)}），拒绝出网",
                "c4.outbound_not_allowed",
            )
        form_body = None
        if request.tool_id == SUBMIT_TOOL:
            try:
                form_body = verify_submission(
                    self._log, self._contents, run.run_id, task.task_id, args, ref.uri
                )
            except ToolArgumentError as exc:
                raise DispatchDenied(str(exc), "dispatch.network_resource_mismatch") from exc
        return _Authority(run, task, bindings, [], [], form_body=form_body)

    def _authority_mcp(
        self,
        request: ActionRequest,
        task: Task,
        run: Run,
        bindings: list[WorkspaceBinding],
        ref: ResourceRef,
        *, discovery: bool = False,
    ) -> _Authority:
        """MCP 资源的授权（**弹批准之前**的五步，见 SPEC §3.4）。

        MCP server 是用户装的外部程序：能不能用它由**任务级白名单**决定，能不能
        跑由进程级配置决定，要不要批准由 C4 决定（`c4.mcp_call` 恒需批准）。三道门
        都在这里或之前走完，所以用户不会收到"批了也发不出去"的批准。
        """
        target = ref.mcp_target
        binding = self.registry.mcp_binding(request.tool_id)
        from .tool_registry import tool_id_for_server

        matched = (request.tool_id == tool_id_for_server(target.server, target.tool)
                   if discovery else binding is not None
                   and (binding.server, binding.tool) == (target.server, target.tool))
        if not matched:
            raise DispatchDenied(
                f"工具与 MCP 资源不匹配：tool_id={request.tool_id} → {ref.uri}"
                "（工具不在已准入的 MCP 注册表里，或 (server, tool) 与资源两段不一致）",
                "dispatch.mcp_resource_mismatch",
            )
        if target.server not in tuple(getattr(task, "mcp_servers", ()) or ()):
            raise DispatchDenied(
                f"任务没有声明 MCP server {target.server!r}（task.created.mcp_servers），"
                f"拒绝调用 {ref.uri}（创建任务时用 --allow-mcp-server 显式声明）",
                "c4.mcp_not_allowed",
            )
        policy = task.exec_policy
        if policy is not None and tuple(policy.isolation_requires):
            clients = self._mcp_clients
            server = None if clients is None else clients.server(target.server)
            scope = None if server is None else getattr(server, "_execution_scope", None)
            execution = None if server is None else getattr(server, "_execution", None)
            if (scope is None or execution is None
                    or scope.get("task_id") != task.task_id
                    or scope.get("run_id") != run.run_id):
                raise DispatchDenied(
                    "当前 MCP 没有绑定本 Task/Run 的本地隔离准入",
                    "c4.mcp_isolation_unproven",
                )
            try:
                execution.check_current()
            except TianshuError as exc:
                raise DispatchDenied(str(exc), "c4.mcp_isolation_unproven") from exc
        self._refuse_blocked_mcp_object(target.server, target.tool)
        if discovery:
            return _Authority(run, task, bindings, [], [])
        clients = self._mcp_clients
        server = None if clients is None else clients.server(target.server)
        if server is None or not server.is_alive:
            diagnostics = ""
            if clients is not None:
                for state in clients.states():
                    if state.name == target.server:
                        diagnostics = "；".join(state.diagnostics) or "未启动"
                        break
            raise DispatchDenied(
                f"MCP server {target.server!r} 当前不可用（{diagnostics or '未配置'}），"
                f"拒绝派发 {ref.uri}",
                "dispatch.mcp_server_unavailable",
            )
        # MCP 工具没有目录、没有作用域、没有租约：零目录任务也能用。
        return _Authority(run, task, bindings, [], [])


    def _authority_subtask(
        self,
        request: ActionRequest,
        task: Task,
        run: Run,
        bindings: list[WorkspaceBinding],
        ref: ResourceRef,
        args: dict[str, Any],
    ) -> _Authority:
        """委派资源的授权：六道门全部在**弹批准之前**走完（规格 §2.3）。

        顺序是刻意的——先把"这个任务根本没有委派能力 / 这一层不能再派 /
        slug 已经用过 / 额度不够"判掉，再决定要不要请人批准。给一个注定被拒
        的子任务弹批准，等于给用户一个"批了就能派"的错觉。

        委派没有目录租约、也没有 workspace 绑定：零目录任务同样合法（子任务
        的绑定是父任务绑定的只读改写，由 `TaskStore.create_subtask()` 定死）。
        """
        if request.tool_id not in SUBTASK_TOOL_IDS:
            # 门 1：工具归属。非子任务工具带着 `subtask://` 资源来，说明调用
            # 方把目标拼错了——按文件/网络去试都不对，而是"这不是给你的资源"。
            raise DispatchDenied(
                f"非子任务工具收到子任务资源：{request.tool_id} → {ref.uri}",
                "dispatch.subtask_rejected",
            )
        subtask_id = ref.subtask_id
        declared_id = args.get("subtask_id")
        if declared_id is not None and declared_id != subtask_id:
            # 资源与参数指向两个不同的 slug：审批摘要绑的是资源，执行时用的
            # 是参数——让它们分叉等于让批准覆盖一个不同的目标。
            raise DispatchDenied(
                f"参数里的 subtask_id 与资源不一致：{declared_id!r} ≠ "
                f"{subtask_id!r}",
                "dispatch.subtask_rejected",
            )

        # 门 2：任务级能力声明。未声明即无委派能力（与 outbound_allow 同一条
        # 纪律）：补一个默认值等于凭空给每个老任务新增一种能力。
        if int(getattr(task, "max_subtasks", 0) or 0) <= 0:
            raise DispatchDenied(
                "任务没有声明委派能力（max_subtasks=0），拒绝派生子任务："
                "创建任务时用 --max-subtasks N 显式声明（0 = 不允许委派）",
                "c4.subtask_not_configured",
            )

        # 门 3：深度固定 1。子任务不能再委派（它自己的 max_subtasks 恒为 0，
        # 但这里先判深度，给出比"未声明能力"更准确的拒绝原因）。
        if int(getattr(task, "depth", 0) or 0) != 0:
            raise DispatchDenied(
                f"深度 {task.depth} 的任务不能再委派（委派只支持一层）："
                f"{task.task_id}",
                "dispatch.subtask_depth_exceeded",
            )

        parent_run_id = run.run_id
        existing = self._tasks.get_subtask(parent_run_id, subtask_id)

        if request.tool_id == "subtask.spawn":
            # 门 4：同一父 Run 里 slug 必须唯一。不同父 Run 可以重名——
            # 主键是 (parent_run_id, subtask_id)，不是全局 slug。
            if existing is not None:
                raise DispatchDenied(
                    f"这个父 Run 里已经有同名子任务：{subtask_id!r}"
                    f"（state={existing.state}，child_run={existing.child_run_id}）",
                    "dispatch.subtask_exists",
                )
            # 门 5：数量与局部上限。越界**拒绝**，不静默夹到上限。
            self._check_subtask_quota(task, parent_run_id, args, subtask_id)
        elif existing is None:
            # 门 6：status / result / cancel 的目标必须存在于**该父 Run** 下。
            # 不存在就说"不存在"，不用空结果或"已经完成"冒充成功。
            raise DispatchDenied(
                f"这个父 Run 下没有子任务：{subtask_id!r}",
                "dispatch.subtask_not_found",
            )
        return _Authority(run, task, bindings, [], [])

    def _check_subtask_quota(
        self,
        task: Task,
        parent_run_id: str,
        args: dict[str, Any],
        subtask_id: str,
    ) -> None:
        """数量、轮次、token 三条限额。任一条不满足 → `dispatch.subtask_limit`。

        `max_turns` / `max_tokens` 的越界是**拒绝**而不是夹到上限：悄悄夹取
        会让"我批的是 100 轮"和"实际只给 8 轮"变成同一张批准，而用户永远
        不会知道自己的请求被改过。
        """
        try:
            clean_subtask_limits(
                {
                    key: args[key]
                    for key in ("max_turns", "max_tokens", "deadline_ms")
                    if key in args
                }
            )
        except TianshuError as exc:
            raise DispatchDenied(str(exc), "dispatch.subtask_limit") from exc
        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise DispatchDenied(
                "子任务目标必须是非空字符串", "dispatch.subtask_rejected"
            )
        raw = goal.strip().encode("utf-8")
        if len(raw) > SUBTASK_GOAL_MAX_BYTES:
            raise DispatchDenied(
                f"子任务目标超过 {SUBTASK_GOAL_MAX_BYTES} 字节：{len(raw)}",
                "dispatch.subtask_limit",
            )
        unfinished = self._tasks.count_unfinished_subtasks(parent_run_id)
        if unfinished >= SUBTASK_MAX_UNFINISHED_PER_RUN:
            raise DispatchDenied(
                f"每个父 Run 最多 {SUBTASK_MAX_UNFINISHED_PER_RUN} 个未完成子任务，"
                f"当前已有 {unfinished} 个（slug={subtask_id!r} 未派发）",
                "dispatch.subtask_limit",
            )
        total = self._tasks.count_subtasks_for_task(task.task_id)
        if total >= SUBTASK_MAX_PER_ROOT_TASK:
            raise DispatchDenied(
                f"每根任务最多派生 {SUBTASK_MAX_PER_ROOT_TASK} 个子任务，"
                f"当前已有 {total} 个（slug={subtask_id!r} 未派发）",
                "dispatch.subtask_limit",
            )

    def _resolve_target(self, request: ActionRequest, auth: _Authority) -> _Target:
        if len(request.resources) != 1:
            # 文件分支的历史文案与编号保持不变（旧日志/测试逐字依赖它）。
            raise DispatchDenied(
                "文件动作只支持单个资源（多资源一次写入无法给出单一 preimage 边界）",
                "c4.multi_resource",
            )

        raw = request.resources[0]
        if isinstance(raw, str) and raw.startswith("memory://"):
            if request.tool_id not in MEMORY_TOOL_IDS:
                raise DispatchDenied(
                    f"文件工具收到记忆资源：{raw}", "dispatch.memory_resource_mismatch"
                )
            try:
                scope = parse_memory_uri(raw)
            except ResourceError as exc:
                raise DispatchDenied(
                    f"记忆资源引用不合法：{exc}", "dispatch.memory_resource_mismatch"
                ) from exc
            # 记忆不 Path.resolve，也不要求目录租约：作用域授权已在
            # `_authority()` 里独立核验。
            return _MemoryTarget(scope=scope, uri=scope.to_uri())

        if isinstance(raw, str) and raw.startswith("network://"):
            if request.tool_id not in NETWORK_TOOL_IDS:
                raise DispatchDenied(
                    f"网络资源只能配网络工具：{request.tool_id} → {raw}",
                    "dispatch.network_resource_mismatch",
                )
            ref = resolve_scheme_resource(raw)
            return _NetworkTarget(uri=ref.uri, target=ref.network_target)

        if isinstance(raw, str) and raw.startswith("mcp://"):
            binding = self.registry.mcp_binding(request.tool_id)
            if binding is None:
                raise DispatchDenied(
                    f"工具不在已准入的 MCP 注册表里：{request.tool_id}"
                    "（动态工具必须先由 server 发现并通过 schema 准入）",
                    "dispatch.mcp_tool_unknown",
                )
            ref = resolve_scheme_resource(raw)
            target = _McpTarget(
                uri=ref.uri,
                server=ref.mcp_target.server,
                tool=ref.mcp_target.tool,
                tool_id=request.tool_id,
            )
            # 对象级阻断（MCP 后续工单 §5-8）：这个工具/server 有未核实的副作用，
            # 在**弹批准之前**就拒——不能让人批一个注定不会发的调用。
            # 这里与发外边界前那道复核分工不同：这里拦"已经知道的阻断"，
            # 那里拦"批准期间新落下的阻断"（见 `_mark_mcp_send_boundary`）。
            self._refuse_blocked_mcp_object(target.server, target.tool)
            return target

        if isinstance(raw, str) and raw.startswith("subtask://"):
            if request.tool_id not in SUBTASK_TOOL_IDS:
                raise DispatchDenied(
                    f"子任务资源只能配子任务工具：{request.tool_id} → {raw}",
                    "dispatch.subtask_rejected",
                )
            ref = resolve_scheme_resource(raw)
            if ref.kind != KIND_SUBTASK:  # pragma: no cover - 前缀已判
                raise DispatchDenied(
                    f"不是子任务资源：{raw}", "dispatch.subtask_rejected"
                )
            return _SubtaskTarget(uri=ref.uri, subtask_id=ref.subtask_id)

        # 资源种类按 scheme 判定（见 resources.py）。**没有执行器**的种类在这里
        # 就拒绝：既不回退成"当成文件路径试试"，也不进审批流程给用户一个
        # "批了就能用"的错觉（网络 / MCP / 委派的执行器属 G2）。
        if isinstance(raw, str) and not os.path.isabs(raw) and "://" in raw:
            ref = resolve_scheme_resource(raw)
            if ref.kind != KIND_MEMORY:  # pragma: no cover - 记忆在上面已返回
                raise DispatchDenied(
                    f"{ref.kind} 资源的执行器尚未实现（G2），拒绝派发：{ref.uri}",
                    "dispatch.resource_kind_unsupported",
                )
        if request.tool_id in MEMORY_TOOL_IDS:
            raise DispatchDenied(
                f"记忆工具必须使用 memory:// 资源：{raw}",
                "dispatch.memory_resource_mismatch",
            )

        target = Path(raw).resolve()

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
        if request.action == ACTION_CREATE and not current_backend().is_dir(
            target.parent
        ):
            raise DispatchDenied(
                f"目标父目录不存在：{target.parent}", "dispatch.no_parent"
            )
        return _FileTarget(target, rel, str(ws.root), ResourceIdentity.of(target))

    def _requires_approval(
        self, request: ActionRequest, target: _Target, args: dict[str, Any]
    ) -> bool:
        """这次动作是否必须有一张已批准的审批。

        文件动作完全沿用原判定（动作集合）。记忆的 `confirm` 是覆盖类动作，
        唯一例外是记忆本身的 `origin=user_explicit`：用户本人刚说的话就是
        授权。这里从**权威投影**读 origin，不接受模型参数或调用方声明。
        """
        if isinstance(target, _SubtaskTarget):
            # 子任务的审批形状**按工具**区分：spawn 要人批，cancel 自动放行。
            # 必须与 `PolicyEngine._classify_subtask()` 的口径一致，否则会
            # 出现"prepare 弹了审批、dispatch 却不消费"（或反过来）的分叉。
            return request.tool_id == "subtask.spawn"
        if request.action not in APPROVAL_ACTIONS:
            return False
        if (
            isinstance(target, _MemoryTarget)
            and request.action == ACTION_OVERWRITE
            # 免审批只属于 `memory.confirm`：用户本人刚说的话可以直接确认。
            # `memory.edit` / `memory.promote` 也在 overwrite 里，但它们改的是
            # 长期召回的正文或作用域，**永远**要一张人批准的审批。
            and request.tool_id == "memory.confirm"
        ):
            memory_id = args.get("memory_id")
            if isinstance(memory_id, str) and memory_id:
                try:
                    memory = self._memory_projection().get(
                        memory_id, allowed=(target.scope,)
                    )
                except TianshuError:
                    # 读不到来源时**不能**推断"用户明示"：那等于凭空免掉审批。
                    # 后续执行侧会以确定拒绝收尾，而不是 unknown。
                    return True
                if memory is not None and getattr(memory, "origin", "") == "user_explicit":
                    return False
        return True

    def _consume_authority(
        self,
        request: ActionRequest,
        auth: _Authority,
        stored: StoredRequest,
        *,
        target: _Target,
        args: dict[str, Any],
    ) -> tuple[str | None, str]:
        """消费审批（如果有）并签发+消费一次性 grant。必须在派发事务内调用。"""
        digest = request.digest()
        action = request.action_digest()
        approval_id: str | None = None
        issuer = "policy:auto"

        if self._requires_approval(request, target, args):
            if self._approvals.rejected_for(request) is not None:
                raise DispatchDenied("具体动作已被人否决", "c4.approval_rejected")
            consent_id = self._consents.selected(request) if self._consents else None
            if consent_id is not None:
                decision = self._policy.classify(
                    request.action, list(request.resources), auth.bindings,
                    self._policy_preconditions(request, target, args),
                    task_id=request.task_id, tool_id=request.tool_id,
                )
                if decision.is_denied:
                    raise DispatchDenied(decision.reason, decision.rule_id)
                self._consents.consume(request, args=args)
                issuer = f"consent:{consent_id}"
                lease_expires = self._lease_deadline(auth.lease_paths, request.run_id)
                grant = self._grants.issue(
                    subject=f"{request.run_id}#{request.claim_epoch}",
                    request=request, issuer=issuer, max_expires_at=lease_expires,
                )
                self._grants.consume(
                    grant.grant_id, request_digest=digest,
                    subject=f"{request.run_id}#{request.claim_epoch}",
                )
                return None, grant.grant_id
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
