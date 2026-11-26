"""任务图、检查和证据的封闭 V1 文档契约；JSON schema 与运行时共用。"""

from __future__ import annotations

from .agent_contracts import D, S, V, obj

ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}
CRITERION = {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"}
REF = {"type": "string", "pattern": "^content://sha256:[0-9a-f]{64}$"}
WORKSPACE = {"type": "string", "pattern": "^(0|[1-9][0-9]{0,3})$"}
POSITIVE = {"type": "integer", "minimum": 1}


def array(item, *, required=False):
    return {"type": "array", "items": item, "minItems": 1 if required else 0}


ARTIFACT_SCHEMA = obj(
    artifact_id=ID, kind={"type": "string", "enum": ["file", "content", "source"]},
    locator=S,
)
STEP_SCHEMA = obj(
    step_id=ID, title=S, instruction=S, depends_on=array(ID),
    criterion_ids=array(CRITERION, required=True),
    expected_artifacts=array(ARTIFACT_SCHEMA), verification_ids=array(ID, required=True),
)
PLAN_SCHEMA = obj(
    format_version=V, plan_id=ID, revision=POSITIVE, mission_id=S, mission_digest=D,
    previous_digest={"oneOf": [D, {"type": "null"}]},
    verification_manifest=obj(content_ref=REF, digest=D),
    steps=array(STEP_SCHEMA, required=True),
)

FILE_INPUTS = {"workspace_binding_id": WORKSPACE, "relative_path": S,
               "file_kind": {"type": "string", "const": "regular"}}
CHECK_INPUT_SCHEMAS = {
    "file_exists": obj(**FILE_INPUTS),
    "file_hash": obj(**FILE_INPUTS, algorithm={"type": "string", "const": "sha256"},
                     expected_digest=D),
    "command_exit": obj(
        executable=S, executable_digest=D, argv=array(S, required=True),
        cwd_binding_id=WORKSPACE, env_profile_digest=D,
        expected_exit_codes=array({"type": "integer"}, required=True), timeout_ms=POSITIVE,
        isolation_profile_digest=D,
    ),
    "source_reference": obj(
        content_ref=REF, source_event_seq=POSITIVE, cited_digest=D, locator=S,
    ),
    "human": obj(
        question=S, required_artifact_ids=array(ID), required_check_ids=array(ID),
    ),
}
CHECK_SCHEMA = {"oneOf": [
    obj(format_version=V, check_id=ID, criterion_ids=array(CRITERION, required=True),
        required={"type": "boolean", "const": True},
        kind={"type": "string", "const": kind}, inputs=inputs)
    for kind, inputs in CHECK_INPUT_SCHEMAS.items()
]}
MANIFEST_SCHEMA = obj(
    format_version=V, mission_id=S, mission_digest=D,
    checks=array(CHECK_SCHEMA, required=True),
)
EVIDENCE_SCHEMA = obj(
    format_version=V, evidence_id=ID, task_id=S, mission_id=S, plan_digest=D, step_id=ID,
    run_id=S, attempt_id=ID,
    kind={"type": "string", "enum": [
        "file_observation", "command_result", "source_snapshot", "human_decision",
    ]},
    event_seqs=array(POSITIVE), content_refs=array(REF), input_fingerprint=D,
)


def validate_document(schema, value, label):
    from .event_schema import _check_value
    errors = []
    _check_value(schema, value, "$", errors, label)
    if errors:
        raise ValueError(f"{label} 违反封闭契约：{errors[0]}")
