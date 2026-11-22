"""Goal V2 的本机人工 RPC；不注册为模型工具。"""

from __future__ import annotations

from .goal_v2 import GoalConflict, GoalCoordinator


def handle(plane, action, params):
    from .control import ControlError, _require_str
    fields = {
        "create": {
            "task_id", "goal_revision", "criteria", "execution_policy", "request_id",
            "mode", "completion_policy", "replan_policy", "max_rounds", "deadline_ms",
            "budget_limits", "budget_bound", "max_no_progress_rounds", "isolation_requirements",
            "predecessor_goal_id",
        },
        "get": {"goal_id", "task_id"},
        "advance": {"goal_id", "request_id", "expected_state_revision", "orchestration_epoch"},
        "pause": {"goal_id", "request_id", "expected_state_revision"},
        "resume": {"goal_id", "request_id", "expected_state_revision"},
        "recover": {
            "goal_id", "request_id", "expected_state_revision", "expected_block_event_seq",
            "resolution", "evidence_refs", "denied_content_digest",
        },
        "close": {"goal_id", "request_id", "expected_state_revision", "outcome"},
    }
    if action not in fields or set(params) - fields[action]:
        raise ControlError("bad_params", "Goal V2 参数包含未定义字段")
    required = {
        "create": {"task_id", "goal_revision", "criteria", "execution_policy", "request_id"},
        "get": set(),
        "advance": {"goal_id", "request_id", "expected_state_revision", "orchestration_epoch"},
        "pause": {"goal_id", "request_id", "expected_state_revision"},
        "resume": {"goal_id", "request_id", "expected_state_revision"},
        "recover": {
            "goal_id", "request_id", "expected_state_revision", "expected_block_event_seq",
            "resolution", "evidence_refs",
        },
        "close": {"goal_id", "request_id", "expected_state_revision", "outcome"},
    }
    if required[action] - set(params):
        raise ControlError("bad_params", "Goal V2 缺少必填参数")
    if action == "get" and len(params) != 1:
        raise ControlError("bad_params", "get 必须且只能指定 task_id 或 goal_id")
    coordinator = GoalCoordinator(plane.stack, model=plane._model, limits=plane._limits)
    try:
        if action == "create":
            goal = coordinator.create(params)
        elif action == "get":
            if "goal_id" in params:
                goal = coordinator.get(_require_str(params, "goal_id"))
            else:
                events = plane.stack.log.read_types(
                    ("goal.v2_created",), task_id=_require_str(params, "task_id"))
                goal = coordinator.get(events[-1].payload["config"]["goal_id"]) if events else None
        elif action == "advance":
            goal = coordinator.tick(
                _require_str(params, "goal_id"), requested=True,
                request_id=_require_str(params, "request_id"),
                expected_state_revision=params["expected_state_revision"],
                orchestration_epoch=params["orchestration_epoch"])
        else:
            goal = coordinator.control(_require_str(params, "goal_id"), action, params)
    except GoalConflict as exc:
        raise ControlError("conflict", str(exc)) from exc
    return {"goal": goal}
