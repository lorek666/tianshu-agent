"""Mission 之外的协调层 C3 契约；不导入存储或执行模块。"""

from __future__ import annotations


def obj(**fields):
    return {"type": "object", "additionalProperties": False,
            "properties": fields, "required": list(fields)}


S = {"type": "string"}
INTEGER = {"type": "integer", "minimum": 0}
D = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
STRINGS = {"type": "array", "items": S}
V = {"type": "integer", "const": 1}
RESULT = {"type": "string", "enum": ["passed", "failed", "inconclusive", "blocked"]}
STATES = (
    "pending", "ready", "running", "verifying", "satisfied", "failed", "blocked",
    "invalidated", "skipped",
)

AGENT_SCHEMAS = {
    "verification.run_started": obj(
        format_version=V, plan_id=S, revision=INTEGER, plan_digest=D, mission_digest=D,
        step_id=S, request_id=S,
    ),
    "plan.created": obj(
        format_version=V, plan_id=S, revision=INTEGER, mission_id=S, mission_digest=D,
        document_ref=S, document_digest=D, manifest_ref=S, mission_ref=S,
        previous_digest={"type": ["string", "null"]},
        changed_step_ids=STRINGS, source_run_id={"type": ["string", "null"]}, request_id=S,
    ),
    "plan.confirmed": obj(format_version=V, plan_id=S, revision=INTEGER, document_digest=D,
                          request_id=S, goal_revision=INTEGER),
    "plan.step_changed": obj(
        format_version=V, plan_id=S, revision=INTEGER, step_id=S,
        state={"type": "string", "enum": list(STATES)}, attempt=INTEGER,
        execution_run_id={"type": ["string", "null"]}, evidence_refs=STRINGS,
        reason_code=S, request_id=S,
    ),
    "verification.requested": obj(
        format_version=V, plan_id=S, revision=INTEGER, step_id=S, check_id=S,
        check_digest=D, attempt_id=S, input_fingerprint=D,
        evidence_refs=STRINGS, request_id=S,
    ),
    "verification.result": obj(
        format_version=V, plan_id=S, revision=INTEGER, step_id=S, check_id=S,
        check_digest=D, attempt_id=S, input_fingerprint=D,
        evidence_refs=STRINGS, result=RESULT, reason_code=S, verified_event_seq=INTEGER,
    ),
    "verification.decided": obj(
        format_version=V, plan_id=S, revision=INTEGER, step_id=S, check_id=S,
        check_digest=D, attempt_id=S, input_fingerprint=D,
        evidence_refs=STRINGS, request_id=S,
        decision={"type": "string", "enum": ["accept", "reject"]}, reason=S,
    ),
}
