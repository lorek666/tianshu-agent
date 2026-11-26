"""Goal V2 封闭配置、阶段状态与每轮评估契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .budget import BOUNDS, DIMENSIONS
from .execpolicy import ISOLATION_BOUNDARIES
from .run_policy import RunExecutionPolicyV1

STATUSES = (
    "ready", "active", "awaiting_input", "awaiting_review", "paused", "blocked",
    "completed", "cancelled", "stale",
)
PHASES = ("plan", "execute", "verify", "ask", "replan", "finish")
BLOCKERS = (
    "verification_failed", "execution_failed", "policy_denied", "approval_denied",
    "budget_exhausted", "deadline_exceeded", "round_limit", "no_progress",
    "isolation_unavailable", "effect_unknown", "usage_unknown", "protocol_error",
    "recovery_required",
)
RESOLUTIONS = ("retry", "replan", "reconciled", "new_observation", "change_decision")
S = {"type": "string"}
D = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
N = {"type": "integer", "minimum": 0}
STRINGS = {"type": "array", "items": S}
NULL_STRING = {"type": ["string", "null"]}
V2 = {"type": "integer", "const": 2}


def obj(**fields):
    return {"type": "object", "additionalProperties": False,
            "properties": fields, "required": list(fields)}


CONFIG_SCHEMA = obj(
    format_version=V2, goal_id=S, task_id=S,
    goal_revision={"type": "integer", "minimum": 1}, mission_id=S,
    mission_digest=D, mode={"type": "string", "enum": ["manual", "bounded_auto"]},
    completion_policy={"type": "string", "enum": ["human", "verified"]},
    replan_policy={"type": "string", "enum": ["confirm", "bounded"]},
    max_rounds={"type": "integer", "minimum": 1},
    deadline_ms={"type": "integer", "minimum": 1},
    budget_limits={"type": "object", "additionalProperties": False,
                   "properties": {d: N for d in DIMENSIONS}},
    budget_bound={"type": "string", "enum": sorted(BOUNDS)},
    max_no_progress_rounds={"type": "integer", "minimum": 1},
    execution_policy=obj(format_version={"type": "integer", "const": 1},
                         mode={"type": "string", "enum": ["pure_only", "effects"]}),
    isolation_requirements={"type": "array", "items": {
        "type": "string", "enum": sorted(ISOLATION_BOUNDARIES)}},
    predecessor_goal_id=NULL_STRING,
)
BLOCK_SCHEMA = obj(
    category={"type": "string", "enum": list(BLOCKERS)}, source_event_seq=N,
    safe_retry={"type": "boolean"}, available_actions={
        "type": "array", "items": {"type": "string", "enum": ["cancel", "recover"]}},
    denied_content_digest=NULL_STRING,
)
STATE_SCHEMA = obj(
    status={"type": "string", "enum": list(STATUSES)},
    phase={"type": "string", "enum": list(PHASES)}, round=N,
    phase_run_id=NULL_STRING, plan_id=NULL_STRING, plan_digest=NULL_STRING,
    step_id=NULL_STRING, execution_run_id=NULL_STRING, verification_run_id=NULL_STRING,
    no_progress_streak=N, satisfied_criterion_ids=STRINGS, completed_step_ids=STRINGS,
    baseline_step_ids=STRINGS, last_assessed_round=N, progress_fingerprint=D,
    blocker={"oneOf": [BLOCK_SCHEMA, {"type": "null"}]},
    orchestration_epoch=N, clock_wall_ms=N, deadline_remaining_ms=N,
    clock_monotonic_ms=N, deadline_monotonic_ms=N,
    runtime_session_id=S, denied_content_digests=STRINGS, recovered_denial_digests=STRINGS,
)
TRANSITION = obj(
    format_version=V2, goal_id=S, expected_state_revision=N,
    orchestration_epoch=N, request_id=S, cause=S, state=STATE_SCHEMA,
)
ASSESSMENT_SCHEMA = obj(
    format_version={"type": "integer", "const": 1}, goal_id=S, round=N,
    mission_digest=D, plan_digest=NULL_STRING, target_criterion_ids=STRINGS,
    execution_run_id=NULL_STRING, verification_run_id=NULL_STRING,
    satisfied_criterion_ids=STRINGS, completed_step_ids=STRINGS, remaining_step_ids=STRINGS,
    evidence_refs=STRINGS, blocker={"oneOf": [BLOCK_SCHEMA, {"type": "null"}]},
    progress_fingerprint=D, no_progress_streak=N,
)
GOAL_V2_SCHEMAS = {
    "goal.v2_created": obj(
        format_version=V2, config=CONFIG_SCHEMA, mission_ref=S, request_id=S,
        request_digest=D, state=STATE_SCHEMA,
    ),
    "goal.v2_advance_requested": obj(
        format_version=V2, goal_id=S, request_id=S, request_digest=D,
    ),
    "goal.v2_transitioned": TRANSITION,
    "goal.v2_closed": TRANSITION,
    "goal.v2_recovered": obj(
        **TRANSITION["properties"], expected_block_event_seq=N,
        resolution={"type": "string", "enum": list(RESOLUTIONS)},
        evidence_refs=STRINGS, denied_content_digest=NULL_STRING,
    ),
    "goal.v2_phase_started": obj(
        format_version=V2, goal_id=S, expected_state_revision=N, orchestration_epoch=N,
        phase={"type": "string", "enum": list(PHASES)}, round=N,
        mission_digest=D, plan_id=NULL_STRING, plan_digest=NULL_STRING,
        step_id=NULL_STRING, request_id=S, budget_account_id=S,
    ),
    "goal.v2_assessed": ASSESSMENT_SCHEMA,
    "goal.v2_admission_refused": obj(
        format_version=V2, goal_id=S,
        category={"type": "string", "enum": list(BLOCKERS)},
        source={"type": "string", "enum": ["model", "summary", "dispatch"]},
        request_id=S,
    ),
}
GOAL_EVENTS = tuple(GOAL_V2_SCHEMAS)


@dataclass(frozen=True)
class GoalConfigV2:
    document: dict[str, Any]

    @classmethod
    def of(cls, raw):
        from .event_schema import payload_errors
        if payload_errors("goal.v2_created", {
            "format_version": 2, "config": raw, "mission_ref": "",
            "request_id": "", "request_digest": "0" * 64, "state": initial_state(1, 1, ""),
        }, 1):
            raise ValueError("GoalConfigV2 不满足封闭契约")
        for key, maximum in (
            ("goal_revision", 2**31), ("max_rounds", 100),
            ("max_no_progress_rounds", 100), ("deadline_ms", 2**63 - 1),
        ):
            if type(raw[key]) is not int or not 1 <= raw[key] <= maximum:
                raise ValueError(f"{key} 必须为有限正整数")
        if (raw["mode"] == "bounded_auto"
                and (type(raw["budget_limits"].get("requests")) is not int
                     or raw["budget_limits"]["requests"] < 1)):
            raise ValueError("自动模式必须设置累计模型请求上限")
        if any(type(v) is not int or v < 0 for v in raw["budget_limits"].values()):
            raise ValueError("预算上限必须为非负整数")
        RunExecutionPolicyV1.of(raw["execution_policy"])
        if len(raw["isolation_requirements"]) != len(set(raw["isolation_requirements"])):
            raise ValueError("隔离要求不能重复")
        return cls(raw)

    def to_dict(self):
        import copy
        return copy.deepcopy(self.document)


def initial_state(now, deadline, runtime, monotonic=0):
    from .canonical import jcs_sha256
    return {
        "status": "ready", "phase": "plan", "round": 0, "phase_run_id": None,
        "plan_id": None, "plan_digest": None, "step_id": None,
        "execution_run_id": None, "verification_run_id": None,
        "no_progress_streak": 0, "satisfied_criterion_ids": [], "completed_step_ids": [],
        "baseline_step_ids": [], "last_assessed_round": 0,
        "progress_fingerprint": jcs_sha256([[], []]), "blocker": None,
        "orchestration_epoch": 1, "clock_wall_ms": now,
        "deadline_remaining_ms": max(0, deadline - now), "runtime_session_id": runtime,
        "clock_monotonic_ms": monotonic,
        "deadline_monotonic_ms": monotonic + max(0, deadline - now),
        "denied_content_digests": [], "recovered_denial_digests": [],
    }


@dataclass(frozen=True)
class GoalViewV2:
    document: dict[str, Any]

    def to_dict(self):
        import copy
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class RoundAssessmentV1:
    document: dict[str, Any]

    def to_dict(self):
        import copy
        return copy.deepcopy(self.document)
