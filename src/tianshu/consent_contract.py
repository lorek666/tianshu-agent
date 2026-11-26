"""ScopedConsentV1 的封闭机器契约；不依赖事件或执行模块，避免注册环。"""

from __future__ import annotations


def _object(properties):
    return {
        "type": "object", "additionalProperties": False,
        "properties": properties, "required": list(properties),
    }


_STR = {"type": "string", "pattern": r"^.+$"}
_DIGEST = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
_POSITIVE = {"type": "integer", "minimum": 1}

TOOL_BINDING_SCHEMA = _object({
    "tool_id": _STR, "version": _STR, "schema_hash": _DIGEST,
    "description_hash": _DIGEST,
})
DIRECTORY_MATCHER_SCHEMA = _object({
    "kind": {"type": "string", "const": "directory_overwrite"},
    "directory": _STR, "directory_identity": _STR,
    "recursive": {"type": "boolean"},
    "allow_content_changes": {"type": "boolean", "const": True},
    "max_file_bytes": _POSITIVE, "max_total_bytes": _POSITIVE,
})
COMMAND_MATCHER_SCHEMA = _object({
    "kind": {"type": "string", "const": "exact_command"}, "executable_path": _STR,
    "executable_digest": _DIGEST,
    "argv": {"type": "array", "minItems": 1, "items": _STR},
    "cwd": _STR, "environment_digest": _DIGEST,
    "timeout_ms": _POSITIVE, "isolation_profile_digest": _DIGEST,
    "pty": {"type": "boolean"},
})
SCOPED_CONSENT_SCHEMA = _object({
    "format_version": {"type": "integer", "const": 1}, "consent_id": _STR,
    "task_id": _STR, "run_id": _STR, "goal_revision": _POSITIVE,
    "workspace_binding_digest": _DIGEST, "mission_digest": _DIGEST,
    "plan_digest": _DIGEST,
    "action": {"type": "string", "enum": ["overwrite", "exec"]},
    "tool_binding": TOOL_BINDING_SCHEMA,
    "matcher": {"oneOf": [DIRECTORY_MATCHER_SCHEMA, COMMAND_MATCHER_SCHEMA]},
    "max_uses": _POSITIVE, "expires_at": _POSITIVE,
    "runtime_session_id": _STR,
    "issued_by": _object({"actor": _STR, "source": {"type": "string", "const": "cli"}}),
    "request_id": _STR,
})
CONSENT_EVENT_SCHEMAS = {
    "consent.issued": SCOPED_CONSENT_SCHEMA,
    "consent.used": _object({
        "format_version": {"type": "integer", "const": 1}, "consent_id": _STR,
        "request_id": _STR, "request_digest": _DIGEST,
        "action_digest": _DIGEST, "attempt_id": _POSITIVE,
        "bytes": {"type": "integer", "minimum": 0},
        "remaining_uses": {"type": "integer", "minimum": 0},
    }),
    "consent.revoked": _object({
        "format_version": {"type": "integer", "const": 1}, "consent_id": _STR,
        "request_id": _STR, "reason": _STR,
    }),
    "consent.expired": _object({
        "format_version": {"type": "integer", "const": 1}, "consent_id": _STR,
        "reason": {"type": "string", "enum": ["deadline", "runtime_restarted"]},
    }),
}
