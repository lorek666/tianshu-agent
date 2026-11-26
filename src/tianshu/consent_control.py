"""认证本机控制面的有限授权入口；不注册为模型工具。"""

from __future__ import annotations

from typing import Any

from .canonical import jcs_sha256
from .permissions import HumanIdentity


def handle(plane, action: str, params: dict[str, Any]) -> dict[str, Any]:
    from .control import ControlError, _require_str

    fields = {
        "issue": {
            "run_id", "goal_revision", "workspace_binding_digest", "mission_digest",
            "plan_digest", "action", "tool_binding", "matcher", "max_uses",
            "expires_at", "request_id",
        },
        "list": {"run_id"},
        "revoke": {"consent_id", "request_id", "reason"},
    }
    if action not in fields or set(params) - fields[action]:
        raise ControlError("bad_params", "有限授权参数包含未定义字段")
    store = plane.stack.dispatcher._consents
    if store is None:
        raise ControlError("unsupported", "当前 runtime 尚未装配有限授权绑定")
    if action == "list":
        run_id = _require_str(params, "run_id") if "run_id" in params else None
        return {"consents": store.list(run_id=run_id)}
    actor = HumanIdentity.local_cli()
    request_id = _require_str(params, "request_id")
    if action == "revoke":
        return {"consent": store.revoke(
            _require_str(params, "consent_id"), actor=actor, request_id=request_id,
            reason=params.get("reason", "revoked by human"),
        )}
    run_id = _require_str(params, "run_id")
    run = store._tasks.get_run(run_id)
    if run is None:
        raise ControlError("not_found", "Run 不存在")
    document = {
        "max_uses": 1, **params, "format_version": 1,
        "consent_id": "consent_" + jcs_sha256({"request_id": request_id})[:24],
        "task_id": run.task_id, "runtime_session_id": store._runtime,
        "issued_by": actor.to_dict(),
    }
    issued = store.issue(document, actor=actor)
    return {"consent": next(
        item for item in store.list(run_id=run_id)
        if item["consent_id"] == issued.consent_id
    )}
