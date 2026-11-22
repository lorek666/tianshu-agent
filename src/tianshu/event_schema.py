"""逐类型事件 payload 契约（PRD §5.3.5 第 1–2 条）。

在拿到这张表之前，C3 只冻结了**类型名**：`payload` 在 `schemas/c3/event.json`
里是 `type: object`，任何字段都能写进去。"类型封闭 ≠ 字段契约完整"——错误
字段名、错误类型、缺字段都要等到读的时候才炸，甚至永远不炸。

本模块提供三件事：

1. **逐类型 payload schema**（JSON Schema 2020-12 的一个明确子集，见
   `SUPPORTED_KEYWORDS`）。不支持的关键字在**注册时**就拒绝（fail-closed），
   而不是运行时静默忽略——"写进契约但不生效"比没写更糟。
2. **写入前校验**：`EventLog.append` 在落 blob / 写库之前调用
   `detect_version()`，非法 payload 抛 `PayloadContractError`（带类型与失败
   路径），一个字节都不产生。
3. **精确版本**：每条事件有一个 payload 版本（`run.start` 当前为 2，其余类型为 1），
   存在旁表 `event_versions(seq, type, version)`，与事件行同一事务写入。
   `version=0` 是"冻结前的旧版"：本模块为每种类型保留一份 `LEGACY_SCHEMAS`，
   只放宽（required 清空 + 历史字段并集 + 少量类型放宽），**不按新版默认补值**。
   历史行在旁表里没有记录 → 读取时按 0；旁表本身缺失/损坏 → 抛
   `PayloadVersionUnavailable`（无法判定版本 = 不能假装是旧版或新版）。

版本旁表出现本程序不认识的版本（更新的二进制写的）→ `UnknownPayloadVersion`：
读不懂就报错，不按当前 schema 解释未来数据。
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .agent_contracts import AGENT_SCHEMAS
from .consent_contract import CONSENT_EVENT_SCHEMAS
from .continuation import (
    COMPACTION_REF_VERSION,
    COMPACTION_SCHEMA,
    CONTINUATION_SCHEMA,
    ContinuationRef,
)
from .conversation import validate_guidance, validate_turns
from .errors import TianshuError
from .execpolicy import ISOLATION_BOUNDARIES, ISOLATION_LEVELS
from .mission import MISSION_EVENT_SCHEMA
from .model_route import ModelRouteV1
from .run_policy import RunExecutionPolicyV1
from .task_attachments import (
    MEDIA_TYPES,
    AttachmentError,
    normalize_attachments,
    validated_attachments,
)

__all__ = [
    "CURRENT_PAYLOAD_VERSION",
    "SUPPORTED_DIALECT",
    "CURRENT_SCHEMAS",
    "LEGACY_PAYLOAD_VERSION",
    "LEGACY_SCHEMAS",
    "PayloadContractError",
    "PayloadVersionUnavailable",
    "SUPPORTED_KEYWORDS",
    "UnknownPayloadVersion",
    "UnsupportedSchemaKeyword",
    "current_version",
    "detect_version",
    "ensure_registry",
    "known_versions",
    "minimal_payload",
    "payload_errors",
    "require_supported_dialect",
    "schema_for",
    "validate_payload",
    "validate_schema_definition",
]

CURRENT_PAYLOAD_VERSION = 1
LEGACY_PAYLOAD_VERSION = 0

#: 契约文件声明的 JSON Schema dialect。别的 dialect 一律拒绝：本模块只实现
#: 2020-12 的一个子集，拿一份 draft-07 文档进来就会把没实现的语义当成已实现。
SUPPORTED_DIALECT = "https://json-schema.org/draft/2020-12/schema"

JSON_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)

#: 本模块实现的 JSON Schema 关键字。**注册时**遇到不在表里的关键字直接拒绝：
#: 未实现的关键字如果被静默忽略，契约里写着的约束就是假的。
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "prefixItems",
        "oneOf",
        "enum",
        "const",
        "minimum",
        "pattern",
        "description",
        "title",
    }
)


class PayloadContractError(TianshuError):
    """payload 不满足该类型的契约。带事件类型与字段路径，写入前抛出。"""

    def __init__(self, event_type: str, path: str, message: str) -> None:
        self.event_type = event_type
        self.path = path
        super().__init__(f"事件 payload 违反契约：type={event_type} path={path}：{message}")


class UnsupportedSchemaKeyword(TianshuError):
    """注册了本模块不支持的 JSON Schema 关键字（fail-closed，不是忽略）。"""


class UnknownPayloadVersion(TianshuError):
    """遇到本程序不认识的 payload 版本（无法按当前 schema 解释）。"""


class PayloadVersionUnavailable(TianshuError):
    """版本旁表缺失/损坏：无法判定精确版本，不能猜。"""


# ---------------------------------------------------------------- schema 构造


def _s(**kw: Any) -> dict[str, Any]:
    return {"type": "string", **kw}


def _i(**kw: Any) -> dict[str, Any]:
    return {"type": "integer", **kw}


def _b() -> dict[str, Any]:
    return {"type": "boolean"}


def _obj(
    properties: dict[str, Any], *, required: tuple[str, ...] = (), open: bool = False
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": True if open else False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _arr(items: dict[str, Any] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array"}
    if items is not None:
        schema["items"] = items
    return schema


def _opt(schema: dict[str, Any]) -> dict[str, Any]:
    """把 schema 放宽成 `T | null`（可选字段允许显式 null，不允许缺失后补值）。"""
    wrapped = copy.deepcopy(schema)
    t = wrapped["type"]
    types = [t] if isinstance(t, str) else list(t)
    if "null" not in types:
        types.append("null")
    wrapped["type"] = types
    if "enum" in wrapped and None not in wrapped["enum"]:
        wrapped["enum"] = [*wrapped["enum"], None]
    return wrapped


def _enum(*values: Any, nullable: bool = False) -> dict[str, Any]:
    vals = list(values) + ([None] if nullable else [])
    types = sorted({_type_of(v) for v in vals})
    return {"type": types[0] if len(types) == 1 else types, "enum": vals}


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


_SHA256 = _s(pattern="^[0-9a-f]{64}$")
_RULE_ID = _s(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")
_TOOL_ID = _enum(
    "create_report",
    "delete_file",
    "list_files",
    "memory.consolidate",
    "memory.confirm",
    "memory.edit",
    "memory.erase",
    "memory.forget",
    "memory.get",
    "memory.promote",
    "memory.propose",
    "memory.remember",
    "memory.search",
    "modify_text",
    "network.fetch",
    "network.search",
    "browser.navigate",
    "browser.submit",
    "read_content",
    "read_text",
    "restore_file",
    "run_command",
    "subtask.cancel",
    "subtask.result",
    "subtask.spawn",
    "subtask.status",
)
#: 动态工具 id 的形状。MCP 工具有 id 是运行时生成的（`mcp__server__tool`），
#: 枚举放不下；而 `tool.result.tool_id` 从 F10 起就按**形状**收口（注释写明
#: "红线 8：schema 变即新工具"）。这里把 `tool.intent` / `tool.not_dispatched` /
#: `receipt` 三处统一成同一条 pattern：**放宽的是数据契约，不是授权**——白名单仍由
#: 工具注册表判定，旧事件仍合法（枚举 ⊂ pattern）。
_DYNAMIC_TOOL_ID = _s(pattern=r"^[a-z][a-z0-9_.]*$")

_ACTION = _enum("read", "create", "overwrite", "delete", "exec", "fetch", "delegate")
_EFFECT_STATE = _enum("applied", "none_applied", "partial", "unknown")
_OUTCOME = _enum("succeeded", "failed", "cancelled")
#: `run.end` 额外记 noop：没有可做的工作也是成功，但 `run.finish` 仍按
#: RUN_OUTCOMES 记 succeeded（loop.py 的 M0 noop 语义，两条口径不同是刻意的）。
_RUN_END_OUTCOME = _enum("succeeded", "failed", "cancelled", "noop")
_RUN_STATE = _enum(
    "created",
    "queued",
    "running",
    "waiting_approval",
    "paused",
    "reconciling",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "fenced",
)
_FINISH_REASON = _enum(
    "cancelled",
    "checkpoint",
    "content_filtered",
    "end_turn",
    "guardrail_intervened",
    "interrupt",
    "limit_output_tokens",
    "limit_structured_output_attempts",
    "limit_total_tokens",
    "limit_turns",
    "max_tokens",
    "stop_sequence",
    "tool_use",
)
_TOOL_STATUS = _enum("succeeded", "failed", "rejected", "unknown")
_APPROVAL_STATE = _enum(
    "pending", "approved", "rejected", "consumed", "revoked", "expired", "superseded"
)
_ACTOR = _obj(
    {"actor": _s(), "source": _s()}, required=("actor", "source")
)
_WORKSPACE_BINDING = _obj(
    {
        "root_path": _s(),
        "mode": _enum("readonly", "writable"),
        "snapshot_policy": _enum("none", "on_write"),
    },
    required=("root_path", "mode", "snapshot_policy"),
)
#: 四边界探针的结论三态。与 `exec.BOUNDARY_*` 同口径；封闭集，未知值写不进去。
_BOUNDARY_STATUS = _enum("enforced", "not_enforced", "unknown")

#: 一次执行的隔离视图（F2）。机器可读，不许出现"人话"字段——中文映射在 UI。
#: `level` 是**累计显示**（`measured_level`），`boundaries` 才是逐边界证据；
#: `unverified` / `contradicted` 必须由 UI 按优先级先于 `level` 处理。
#: 与 `execpolicy.ISOLATION_LEVELS` 同一套封闭集（能力声明与输出契约必须一致）。
_ISOLATION_LEVEL = _enum(*ISOLATION_LEVELS)

_ISOLATION_VIEW = _obj(
    {
        "backend": _s(),
        # 自述级别：封闭集（后端只能声明这几个；v1 实际只有 none）。
        "declared": _ISOLATION_LEVEL,
        # 实测级别：可 null（没有报告），但一旦有值必须是已知级别。
        "measured": _opt(copy.deepcopy(_ISOLATION_LEVEL)),
        "level": copy.deepcopy(_ISOLATION_LEVEL),
        "unverified": _b(),
        "contradicted": _b(),
        # 四边界必须齐全：缺一条就等于"不知道的那条"被静默当成 null。
        "boundaries": _obj(
            {boundary: _BOUNDARY_STATUS for boundary in ISOLATION_BOUNDARIES},
            required=tuple(ISOLATION_BOUNDARIES),
        ),
        # 已证明的边界也只能是四边界名（不是任意字符串）。
        "enforced_boundaries": _arr(_enum(*ISOLATION_BOUNDARIES)),
    },
    required=(
        "backend",
        "declared",
        "measured",
        "level",
        "unverified",
        "contradicted",
        "boundaries",
        "enforced_boundaries",
    ),
)

_EXEC_POLICY = _obj(
    {
        "argv_allowlist": _arr(_s()),
        "argv_denylist": _arr(_s()),
        "cwd_roots": _arr(_s()),
        "env_allowlist": _arr(_s()),
        "max_output_bytes": _i(),
        "timeout_ms": _i(),
        "allow_pty": _b(),
        # 可选：老任务/老事件没有这个字段，仍必须合法；边界名用封闭枚举，
        # 非法值在**写入前**就拒绝（不能等到任务加载才炸）。
        "isolation_requires": _arr(_enum(*ISOLATION_BOUNDARIES)),
    },
    required=(
        "argv_allowlist",
        "argv_denylist",
        "cwd_roots",
        "env_allowlist",
        "max_output_bytes",
        "timeout_ms",
        "allow_pty",
    ),
)
_CONTEXT_MEMORY = _obj(
    {
        "memory_id": _s(),
        "revision": _i(),
        "content_ref": _s(),
        "scope": _s(),
        "scope_id": _s(),
        "tool_call_id": _s(),
    },
    required=("memory_id", "revision", "content_ref", "scope", "scope_id", "tool_call_id"),
)
_CONTEXT_SUMMARY = _obj(
    {
        # 摘要身份（PRD §7.1）：正文不进事件，进事件的是"这一轮用了哪一版"。
        "summary_id": _SHA256,
        "version": _i(minimum=1),
        "text_digest": _SHA256,
        "source_seqs": _arr(_i(minimum=1)),
    },
    required=("summary_id", "version", "text_digest", "source_seqs"),
)
_MODEL_CONTEXT = _obj(
    {
        "budget_version": _s(),
        "max_bytes": _i(),
        "spill_threshold": _i(),
        "source_seqs": _arr(_i()),
        "dropped_seqs": _arr(_i()),
        "memories": _arr(_CONTEXT_MEMORY),
        # PRD §7.1：这一轮视图里**实际用到**的模型摘要（缺省不写 = 没用摘要）。
        "summaries": _arr(_CONTEXT_SUMMARY),
    },
    required=("budget_version", "max_bytes", "spill_threshold", "source_seqs", "dropped_seqs"),
)
_MODEL_USAGE = _obj(
    {"input_tokens": _opt(_i()), "output_tokens": _opt(_i())},
    required=("input_tokens", "output_tokens"),
)
_MODEL_COST = _obj(
    {
        "provider": _opt(_s()),
        "adapter": _s(),
        "model": _opt(_s()),
        "units": _obj(
            {"input_tokens": _opt(_i()), "output_tokens": _opt(_i())},
            required=("input_tokens", "output_tokens"),
        ),
        "unit_price_ver": _opt(_s()),
        "cost": _opt(_i()),
        "currency": _opt(_s()),
    },
    required=("provider", "adapter", "model", "units", "unit_price_ver", "cost", "currency"),
)
_TOOL_CALL = _obj(
    {
        "call_id": _s(),
        "name": _s(),
        # 模型给的参数**原样保留**：半截 JSON / null / 数组都是真实出现过的形状，
        # adapter 的契约是"不修好它"（见 C1 契约与 tests/test_model_contract.py）。
        # 这里冻结的是字段存在，不冒充已经冻结了"参数一定是对象"。
        "arguments": {},
    },
    required=("call_id", "name", "arguments"),
)
_RECEIPT_EVIDENCE = _obj(
    {
        "kind": _enum(
            "result", "intent", "blob", "memory", "memory_erasure", "mcp_call"
        ),
        "call_id": _opt(_s()),
        "seq": _opt(_i()),
        "path": _opt(_s()),
        "sha256": _opt(_SHA256),
        "memory_id": _opt(_s()),
        # blob 内容不存在时凭据要如实写 absent=true，而不是删掉 evidence 看起来完好
        "absent": _opt(_b()),
        # 擦除回执的附加证据（memory_erasure）：删了什么、删了多少、还被谁引用
        "content_ref": _opt(_SHA256),
        # MCP 调用的证据（mcp_call）：哪个 server 的哪个工具、哪次请求。
        # 响应摘要复用上面的 `sha256`，不另造一个字段。
        "server": _opt(_s()),
        "tool": _opt(_s()),
        "request_id": _opt(_i()),
        "deleted": _opt(_b()),
        "deleted_bytes": _opt(_i()),
        "retained_by": _opt(_arr(_s())),
    },
    required=("kind",),
)
_ARTIFACT = _obj(
    {
        "path": _s(),
        "sha256": _SHA256,
        "bytes": _i(),
        "before_state": _opt(_s()),
        "after_state": _opt(_s()),
    },
    required=("path", "sha256", "bytes", "before_state", "after_state"),
)
_TAKEOVER_CHECKED = _obj(
    {
        "applied": _arr(_s()),
        "none_applied": _arr(_s()),
        "unresolved": _arr(_s()),
        "unknown": _arr(_s()),
        "corrupt": _b(),
        "missing_manifest": _b(),
        "pending_writes": _i(),
    },
    required=(
        "applied",
        "none_applied",
        "unresolved",
        "unknown",
        "corrupt",
        "missing_manifest",
        "pending_writes",
    ),
)
_UNDO_ENTRY = _obj(
    {"root": _s(), "path": _s(), "result": _s()},
    required=("root", "path", "result"),
)

#: 子任务状态机（G2 委派）。与 `tasks.SUBTASK_STATES` 同一个封闭集：
#: 规格 §2.1 的 DDL 注释列了 6 个，§2.6 又要求取消先落 `cancelling`（真正停下
#: 在 D2），合起来才是完整的 7 个。未知状态写不进去。
_SUBTASK_STATE = _enum(
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "fenced",
)
#: 子任务局部上限。`deadline_ms` 固定 300000，不由模型抬高。
_SUBTASK_LIMITS = _obj(
    {
        "max_turns": _i(minimum=1),
        "max_tokens": _i(minimum=1),
        "deadline_ms": _i(minimum=1),
    },
    required=("max_turns", "max_tokens", "deadline_ms"),
)
#: 子任务用量（D2 收尾时写）。`subtask.finished` 里可选：D1 不写这条事件。
_SUBTASK_USAGE = _obj(
    {"turns": _i(minimum=0), "tokens": _i(minimum=0)},
    required=("turns", "tokens"),
)

#: 当前版本（1）的逐类型契约。键必须与 `events.EVENT_TYPES` 完全一致
#: （`ensure_registry()` 在导入时校验，测试再次绑定）。
_EXECUTION_SUBJECT = _obj({
    "format_version": {"type": "integer", "const": 1},
    "subject_id": _SHA256,
    "kind": _enum("command", "local_mcp", "remote_mcp"),
    "task_id": _s(), "run_id": _s(),
    **{name: _SHA256 for name in (
        "config_digest", "executable_digest", "environment_digest",
        "workspace_binding_digest", "data_root_digest", "isolation_profile_digest",
    )},
})
_EXECUTION_ADMISSION = _obj({
    "format_version": {"type": "integer", "const": 1}, "subject_digest": _SHA256,
    "backend": _s(), "backend_identity": _s(), "platform": _s(), "architecture": _s(),
    "probe_version": _s(), "workspace": _s(), "data_root": _s(),
    "required_boundaries": _arr(_enum(*ISOLATION_BOUNDARIES)),
    "boundaries": _obj({
        name: _enum("enforced", "not_enforced", "unknown") for name in ISOLATION_BOUNDARIES
    }),
    "evidence_refs": _arr(_s()), "lifecycle_id": _s(),
})
_EXECUTION_LIFECYCLE = _obj({
    "lifecycle_id": _s(), "pid": _opt(_i(minimum=1)), "birth_identity": _opt(_s()),
})
for _execution_schema in (
    _EXECUTION_SUBJECT, _EXECUTION_ADMISSION, _EXECUTION_LIFECYCLE,
    _EXECUTION_ADMISSION["properties"]["boundaries"],
):
    _execution_schema["required"] = list(_execution_schema["properties"])

CURRENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "execution.admitted", "execution.ended",
