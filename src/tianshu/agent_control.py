"""任务图、验收和有限授权的认证本机控制面；没有模型工具注册。"""

from __future__ import annotations

from .consent import (
    ConsentBindingV1,
    ConsentStore,
    command_matcher,
    directory_matcher,
    tool_binding,
    workspace_binding_digest,
)
from .leases import current_process
from .permissions import HumanIdentity
from .plans import PlanStore, read_material
from .verification import decide_check, request_check


def resolve_consent_binding(stack, run_id):
    from .mission import mission_for
    from .verification_driver import binding_for

    run = stack.tasks.get_run(run_id)
    if run is None:
        return None
    task = stack.tasks.get_task(run.task_id)
    mission = mission_for(stack.log, run_id)
    if mission is None or task is None or run.goal_revision != task.goal_revision:
        return None
    plan_id = None
    verification = binding_for(stack.log, run_id)
    if verification is not None:
        plan_id = verification["plan_id"]
    else:
        changes = stack.log.read_types(("plan.step_changed",), task_id=run.task_id)
        linked = [e for e in changes if e.payload["execution_run_id"] == run_id]
        if linked:
            plan_id = linked[-1].payload["plan_id"]
    if plan_id is None:
        # 新 Goal 阶段事件也是权威绑定；不根据客户端给的摘要猜测归属。
        for event in stack.log.read_run(run_id):
            if event.type == "goal.v2_phase_started":
                plan_id = event.payload.get("plan_id")
    if not plan_id:
        return None
    view = PlanStore(stack).get(plan_id)
    if (view["task_id"] != task.task_id or not view["confirmed"]
            or view["document"]["mission_digest"] != mission.digest):
        return None
    return ConsentBindingV1(
        task.task_id, run_id, task.goal_revision, workspace_binding_digest(task.workspaces),
        mission.digest, view["document_digest"],
    )


def ensure_consents(stack, *, runtime_session_id=None):
    """控制和驱动连接共享进程身份，重启后的未使用 Scope 失效。"""
    existing = stack.dispatcher._consents
    if existing is not None:
        return existing
    store = ConsentStore(
        stack.log, tasks=stack.tasks, data_root=stack.data_root,
        runtime_session_id=runtime_session_id or current_process().instance_id,
        binding_resolver=lambda run_id: resolve_consent_binding(stack, run_id),
    )
    stack.dispatcher.bind_consents(store)
    return store


def graph_view(stack, view):
    events = stack.log.read_types(
        ("verification.requested", "verification.result"), task_id=view["task_id"])
    done = {e.payload["attempt_id"] for e in events if e.type == "verification.result"}
    pending = [e.payload for e in events if e.type == "verification.requested"
               and e.payload["plan_id"] == view["plan_id"]
               and e.payload["revision"] == view["revision"]
               and e.payload["attempt_id"] not in done]
    latest = {}
    for request in pending:
        latest[(request["step_id"], request["check_id"])] = request
    return {"plan": view, "verification_requests": list(latest.values())}


def handle(plane, action, params):
    from .control import ControlError
    stack = plane.stack
    fields = {
        "get": {"plan_id"},
        "confirm": {"plan_id", "expected_revision", "document_digest", "request_id"},
        "replan": {"plan_id", "expected_revision", "document", "manifest", "request_id"},
        "request": {"plan_id", "step_id", "check_id", "expected_state_revision", "request_id"},
        "verify": {"plan_id", "step_id", "expected_state_revision", "request_id"},
        "material": {"task_id", "content_ref"},
    }
    if action == "decide":
        return {"result": decide_check(stack, params, actor=HumanIdentity.local_cli())}
    if action not in fields or set(params) != fields[action]:
        raise ControlError("bad_params", "任务图操作参数字段不匹配")
    if action == "material":
        return {"material": read_material(stack, params["task_id"], params["content_ref"])}
    plans = PlanStore(stack)
    view = plans.get(params["plan_id"])
    if action == "get":
        return graph_view(stack, view)
    if action == "confirm":
        return graph_view(stack, plans.confirm(**params))
    if action == "replan":
        return graph_view(stack, plans.replan(
            params["plan_id"], view["mission"], params["document"], params["manifest"],
            expected_revision=params["expected_revision"], request_id=params["request_id"],
        ))
    if params["expected_state_revision"] != view["state_revision"]:
        raise ControlError("conflict", "计划状态已经改变，请刷新")
    if action == "request":
        check = next((c for c in view["manifest"]["checks"]
                      if c["check_id"] == params["check_id"]), None)
        if check is None or check["kind"] != "human":
            raise ControlError("bad_params", "检查必须由独立验证 Run 执行")
        return {"request": request_check(
            stack, view, params["step_id"], params["check_id"],
            run_id="", request_id=params["request_id"],
        )}
    from .verification_driver import start_verification
    run = start_verification(stack, view, params["step_id"], request_id=params["request_id"],
                             mission=view["mission"])
    return {**graph_view(stack, plans.get(view["plan_id"])), "run": plane._run_view(run)}


def consent_prepare(plane, params):
    from pathlib import Path

    from .control import ControlError

    kind = params.get("kind")
    fields = {
        "directory_overwrite": {"run_id", "kind", "directory", "recursive"},
        "exact_command": {"run_id", "kind", "request_id"},
    }
    if kind not in fields or set(params) != fields[kind]:
        raise ControlError("bad_params", "授权预览参数无效")
    binding = resolve_consent_binding(plane.stack, params["run_id"])
    if binding is None:
        raise ControlError("conflict", "此 Run 没有已确认的 Mission/Plan 绑定")
    task = plane.stack.tasks.get_task(binding.task_id)
    if kind == "directory_overwrite":
        if type(params["recursive"]) is not bool:
            raise ControlError("bad_params", "recursive 必须为布尔值")
        directory = Path(params["directory"]).resolve(strict=True)
        if not any(
            b.can_write() and (
                directory == Path(b.root_path).resolve()
                or Path(b.root_path).resolve() in directory.parents)
            for b in task.workspaces
        ):
            raise ControlError("bad_params", "目录不属于任务工作区")
        matcher = directory_matcher(directory, recursive=params["recursive"],
                                    max_file_bytes=256 * 1024, max_total_bytes=2 * 1024 * 1024)
        tool_id, action = "modify_text", "overwrite"
    else:
        stored = plane.stack.dispatcher.get_request(params["request_id"])
        if stored is None or stored.request.run_id != binding.run_id:
            raise ControlError("bad_params", "命令不属于当前 Run")
        if stored.request.tool_id != "run_command":
            raise ControlError("bad_params", "只能为精确命令建立授权")
        matcher = command_matcher(task, args=stored.args, cwd=Path(stored.args.get("cwd", ".")))
        tool_id, action = "run_command", "exec"
    return {"preview": {
        **binding.to_dict(), "action": action,
        "tool_binding": tool_binding(plane.stack.dispatcher.registry.spec(tool_id)),
        "matcher": matcher,
    }}
