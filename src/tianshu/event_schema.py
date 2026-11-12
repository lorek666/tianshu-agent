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
3. **精确版本**：每条事件有一个 payload 版本（当前 `CURRENT_PAYLOAD_VERSION=1`），
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

from .errors import TianshuError
from .execpolicy import ISOLATION_BOUNDARIES, ISOLATION_LEVELS

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
CURRENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "approval.consumed": _obj(
        {
            "approval_id": _s(),
            "request_digest": _SHA256,
            "action_digest": _SHA256,
            "decided_by": _s(),
        },
        required=("approval_id", "request_digest", "action_digest", "decided_by"),
    ),
    "approval.decided": _obj(
        {
            "approval_id": _s(),
            "request_digest": _SHA256,
            "decision": _enum("approved", "rejected"),
            "decided_by": _ACTOR,
        },
        required=("approval_id", "request_digest", "decision", "decided_by"),
    ),
    "approval.expired": _obj(
        {"approval_id": _s(), "reason": _s()}, required=("approval_id", "reason")
    ),
    "approval.requested": _obj(
        {
            "approval_id": _s(),
            "request_id": _s(),
            "request_digest": _SHA256,
            "action_digest": _SHA256,
            "action_subject": _SHA256,
            "action": _ACTION,
            "resources": _arr(_s()),
            "rule_id": _RULE_ID,
            "reason": _s(),
            "expires_at": _i(minimum=1),
            # 与 dispatch.requested 同一个视图；审批卡片从事件回读它。
            "isolation": _ISOLATION_VIEW,
        },
        required=(
            "approval_id",
            "request_id",
            "request_digest",
            "action_digest",
            "action_subject",
            "action",
            "resources",
            "rule_id",
            "reason",
            "expires_at",
        ),
    ),
    "approval.revoked": _obj(
        {
            "approval_id": _s(),
            "decided_by": _ACTOR,
            "reason": _s(),
            "stage": _APPROVAL_STATE,
        },
        required=("approval_id", "decided_by"),
    ),
    "approval.superseded": _obj(
        {"approval_id": _s(), "reason": _s()}, required=("approval_id", "reason")
    ),
    "dispatch.admitted": _obj(
        {
            "request_id": _s(),
            "request_digest": _SHA256,
            "call_id": _s(),
            "grant_id": _s(),
            "intent_seq": _i(minimum=1),
            "approval_id": _opt(_s()),
        },
        required=("request_id", "request_digest", "call_id", "grant_id", "intent_seq"),
    ),
    "dispatch.rejected": _obj(
        {
            "request_id": _s(),
            "request_digest": _opt(_SHA256),
            "rule_id": _RULE_ID,
            "reason": _s(),
            "action": _ACTION,
            "resources": _arr(_s()),
            "call_id": _s(),
            "dispatch_id": _s(),
            "phase": _enum("prepare", "guard"),
        },
        required=("request_id", "rule_id", "reason"),
    ),
    "dispatch.requested": _obj(
        {
            "request_id": _s(),
            "request_digest": _SHA256,
            "action_digest": _opt(_SHA256),
            "action": _ACTION,
            "resources": _arr(_s()),
            "decision": _enum("auto", "approve"),
            "rule_id": _RULE_ID,
            "approval_id": _opt(_s()),
            "approval_state": _opt(_APPROVAL_STATE),
            # 可选的隔离视图：exec 动作在**审批前**就带上，UI 审批卡片据此说
            # 实话（"受信任执行/自述未实测/自述与实测冲突"）。
            "isolation": _ISOLATION_VIEW,
        },
        required=("request_id", "request_digest", "action", "resources", "decision", "rule_id"),
    ),
    "dispatch.revoked": _obj(
        {
            "approval_id": _s(),
            "call_id": _s(),
            "dispatch_id": _s(),
            "reason": _s(),
        },
        required=("approval_id", "call_id", "dispatch_id", "reason"),
    ),
    "execution.started": _obj(
        {
            "run_id": _s(),
            "task_id": _s(),
            "goal_revision": _i(minimum=1),
            "lease_epoch": _i(minimum=1),
            "resources": _arr(_s()),
        },
        required=("run_id", "task_id", "goal_revision", "lease_epoch", "resources"),
    ),
    "grant.consumed": _obj(
        {
            "grant_id": _s(),
            "request_digest": _SHA256,
            "remaining_uses": _i(minimum=0),
        },
        required=("grant_id", "request_digest", "remaining_uses"),
    ),
    "grant.issued": _obj(
        {
            "grant_id": _s(),
            "request_digest": _SHA256,
            "subject": _s(),
            "action": _ACTION,
            "issuer": _s(),
            "expires_at": _i(minimum=1),
        },
        required=("grant_id", "request_digest", "subject", "action", "issuer", "expires_at"),
    ),
    "job.claimed": _obj(
        {
            "run_id": _s(),
            "job_id": _s(),
            "claim_epoch": _i(minimum=0),
            "previous_epoch": _i(minimum=0),
            "owner_pid": _i(minimum=0),
            "owner_instance_id": _s(),
            "deadline_at": _i(minimum=0),
        },
        required=(
            "run_id",
            "job_id",
            "claim_epoch",
            "previous_epoch",
            "owner_pid",
            "owner_instance_id",
            "deadline_at",
        ),
    ),
    "job.enqueued": _obj({"run_id": _s(), "job_id": _s()}, required=("run_id", "job_id")),
    "job.finished": _obj(
        {
            "run_id": _s(),
            "job_id": _s(),
            "claim_epoch": _i(minimum=0),
            "owner_pid": _opt(_i(minimum=0)),
            "owner_instance_id": _opt(_s()),
            "outcome": _OUTCOME,
            "by": _s(),
            "reason": _s(),
        },
        required=("run_id", "job_id", "claim_epoch", "owner_pid", "owner_instance_id", "outcome"),
    ),
    "job.parked": _obj(
        {
            "run_id": _s(),
            "job_id": _s(),
            "claim_epoch": _i(minimum=0),
            "owner_pid": _i(minimum=0),
            "owner_instance_id": _s(),
            "reason": _s(),
        },
        required=("run_id", "job_id", "claim_epoch", "owner_pid", "owner_instance_id", "reason"),
    ),
    "job.requeued": _obj(
        {
            "run_id": _s(),
            "job_id": _s(),
            "claim_epoch": _i(minimum=0),
            # G2 子代理 D2：`JobStore.wake()` 由子任务收尾触发，不是 owner 交还。
            # 唤醒时 owner 已经被 park 清空，如实写 null；旧事件（owner 交还）
            # 仍带完整 owner 字段，向后兼容。
            "owner_pid": _opt(_i(minimum=0)),
            "owner_instance_id": _opt(_s()),
            # 谁让它回到队列：`owner`（主动交还）或 `wake`（子任务收尾）。
            "by": _opt(_s()),
            "reason": _opt(_s()),
        },
        required=("run_id", "job_id", "claim_epoch"),
    ),
    "model.error": _obj(
        {
            "request_id": _s(),
            "error": _s(),
            "attempt": _opt(_i(minimum=0)),
            "retryable": _opt(_b()),
        },
        required=("request_id", "error"),
    ),
    "model.intent": _obj(
        {
            "request_id": _s(),
            "attempt": _i(minimum=0),
            "messages_hash": _SHA256,
            "context": _MODEL_CONTEXT,
        },
        required=("request_id", "attempt", "messages_hash", "context"),
    ),
    "model.result": _obj(
        {
            "request_id": _s(),
            "content": _s(),
            "finish_reason": _FINISH_REASON,
            "tool_calls": _arr(_TOOL_CALL),
            "usage": _MODEL_USAGE,
            "cost": _MODEL_COST,
        },
        required=("request_id", "content", "finish_reason", "tool_calls", "usage", "cost"),
    ),
    "policy.denied": _obj(
        {
            "request_id": _s(),
            "request_digest": _SHA256,
            "action": _ACTION,
            "resources": _arr(_s()),
            "rule_id": _RULE_ID,
            "reason": _s(),
        },
        required=("request_id", "request_digest", "action", "resources", "rule_id", "reason"),
    ),
    "receipt": _obj(
        {
            "receipt_id": _s(pattern=r"^rcpt_[0-9a-f]{12}$"),
            "run_id": _s(),
            "call_id": _s(),
            "tool_id": _DYNAMIC_TOOL_ID,
            "action": _ACTION,
            "args_hash": _SHA256,
            "effect_state": _EFFECT_STATE,
            "backend": _enum("local_fs", "memfs", "memory", "mcp"),
            "issuer": _s(),
            "issued_at": _i(minimum=0),
            "root": _s(),
            "resource": _s(),
            "before_state": _s(),
            "after_state": _s(),
            "intent_seq": _i(minimum=1),
            "blob_ref": _opt(_s()),
            "evidence": _arr(_RECEIPT_EVIDENCE),
        },
        required=(
            "receipt_id",
            "run_id",
            "call_id",
            "tool_id",
            "action",
            "args_hash",
            "effect_state",
            "backend",
            "issuer",
            "issued_at",
            "root",
            "resource",
            "before_state",
            "after_state",
            "intent_seq",
            "blob_ref",
            "evidence",
        ),
    ),
    "recovery.blocked": _obj(
        {
            "workspace": _s(),
            "call_ids": _arr(_s()),
            "reason": _s(),
            "takeover_refused_by": _opt(_s()),
        },
        required=("workspace", "call_ids", "reason"),
    ),
    "recovery.unblocked": _obj(
        {"workspace": _s(), "reason": _s()}, required=("workspace", "reason")
    ),
    "run.control": _obj(
        {
            "run_id": _s(),
            "task_id": _s(),
            "action": _enum("pause", "cancel", "lease_takeover"),
            "applied_immediately": _opt(_b()),
            "request_effective": _opt(_b()),
            "stronger_request": _opt(_s()),
            "effect_state": _opt(_EFFECT_STATE),
            # G2 D2：控制请求的发起原因（`subtask_cancel_requested` /
            # `parent_cancelled` / `parent_fenced`）。旧事件不带这个键。
            "reason": _opt(_s()),
            "previous_run": _opt(_s()),
            "checked": _opt(_TAKEOVER_CHECKED),
        },
        required=("run_id", "task_id", "action"),
    ),
    "run.cursor": _obj(
        {
            "run_id": _s(),
            "goal_revision": _i(minimum=1),
            "lease_epoch": _i(minimum=1),
            "claim_epoch": _i(minimum=0),
            "completed_call_ids": _i(minimum=0),
            "pending_proposals": _i(minimum=0),
            "payload_hash": _SHA256,
        },
        required=(
            "run_id",
            "goal_revision",
            "lease_epoch",
            "claim_epoch",
            "completed_call_ids",
            "pending_proposals",
            "payload_hash",
        ),
    ),
    "run.effect": _obj(
        {
            "run_id": _s(),
            "task_id": _s(),
            "effect_state": _EFFECT_STATE,
            "n_applied": _i(minimum=0),
            "n_unknown": _i(minimum=0),
        },
        required=("run_id", "task_id", "effect_state", "n_applied", "n_unknown"),
    ),
    "run.end": _obj(
        {
            "outcome": _RUN_END_OUTCOME,
            "effect_state": _EFFECT_STATE,
            "artifact": _opt(_ARTIFACT),
            "error": _opt(_s()),
            "receipt_ids": _arr(_s()),
            "tokens": _i(minimum=0),
            "tokens_unknown_calls": _i(minimum=0),
            "turns": _i(minimum=0),
            "unbilled_model_intents": _i(minimum=0),
            "budget_enforced": _b(),
        },
        required=(
            "outcome",
            "effect_state",
            "artifact",
            "error",
            "receipt_ids",
            "tokens",
            "tokens_unknown_calls",
            "turns",
            "unbilled_model_intents",
            "budget_enforced",
        ),
    ),
    "run.fenced": _obj(
        {"run_id": _s(), "task_id": _s(), "reason": _s()},
        required=("run_id", "task_id", "reason"),
    ),
    "run.finish": _obj(
        {
            "run_id": _s(),
            "task_id": _s(),
            "outcome": _OUTCOME,
            "effect_state": _EFFECT_STATE,
            "reason": _s(),
        },
        required=("run_id", "task_id", "outcome", "effect_state"),
    ),
    "run.model_turn": _obj(
        {
            "run_id": _s(),
            "turn_id": _s(),
            "seq": _i(minimum=1),
            "claim_epoch": _i(minimum=0),
            "finish_reason": _opt(_FINISH_REASON),
            "usage": _opt(_MODEL_USAGE),
            "payload_hash": _SHA256,
        },
        required=(
            "run_id",
            "turn_id",
            "seq",
            "claim_epoch",
            "finish_reason",
            "usage",
            "payload_hash",
        ),
    ),
    "run.start": _obj(
        {
            "run_id": _s(),
            "task_id": _s(),
            "goal_revision": _i(minimum=1),
            "lease_epoch": _i(minimum=1),
            "task": _s(),
            "out": _s(),
            "limits": _obj(
                {"max_turns": _i(minimum=0), "max_total_tokens": _i(minimum=0)}
            ),
            "tools": _arr(_s()),
            # G2 委派 D1：子 Run 的父链（可选；根 Run 不写这两个键）。
            # 子任务身份必须在 Run 侧留下事件证据，否则投影重建后
            # "这个 Run 是谁派的"就只能从另一张表猜。
            "parent_run_id": _s(),
            "subtask_id": _s(),
        },
        required=("run_id", "task_id", "goal_revision", "lease_epoch"),
    ),
    "run.state": _obj(
        {
            "run_id": _s(),
            "task_id": _s(),
            "from": _RUN_STATE,
            "to": _RUN_STATE,
            "reason": _s(),
            "claim_epoch": _opt(_i(minimum=1)),
            "control_cleared": _opt(_b()),
        },
        required=("run_id", "task_id", "from", "to", "reason"),
    ),
    "subtask.spawned": _obj(
        {
            # 父 Run 侧的委派审计：派了谁、子任务身份是什么、给了多少额度。
            "subtask_id": _s(),
            "child_task_id": _s(),
            "child_run_id": _s(),
            "job_id": _s(),
            # 目标正文**不进**父 Run：只留摘要，正文在子 Task 的 goal 里。
            "goal_digest": _SHA256,
            "limits": _SUBTASK_LIMITS,
        },
        required=(
            "subtask_id",
            "child_task_id",
            "child_run_id",
            "job_id",
            "goal_digest",
            "limits",
        ),
    ),
    "subtask.finished": _obj(
        {
            "subtask_id": _s(),
            # 子任务状态机（不是子 Run 状态机）。
            "state": _SUBTASK_STATE,
            "result_ref": _opt(_s()),
            "error": _opt(_s()),
            "usage": _SUBTASK_USAGE,
        },
        required=("subtask_id", "state"),
    ),
    "task.created": _obj(
        {
            "task_id": _s(),
            "goal": _s(),
            "goal_revision": _i(minimum=1),
            "lease_epoch": _i(minimum=1),
            "workspaces": _arr(_WORKSPACE_BINDING),
            "exec_policy": _opt(_EXEC_POLICY),
            # 记忆契约 v1.2（PR #78）：控制面标记与作用域声明必须落进事件。
            "control": _b(),
            "memory_scopes": _arr(_s()),
            # G2：任务级外发白名单（源级）。新写入恒写（空列表也写）；
            # 旧事件没有这个键 → detect_version 落到 v0，不按新版补值。
            "outbound_allow": _arr(_s()),
            # G2 MCP 阶段：任务级 server 白名单（能力白名单，与 outbound_allow
            # 同层）。新写入恒写（空列表也写）；升级前的旧行缺这个键，读取侧
            # 按空元组处理（`Task.mcp_servers` 默认 ()），不是"按 v0 读"。
            "mcp_servers": _arr(_s()),
            # G2 委派 D1：任务级委派能力声明（0–4）。**必填**：
            # 未声明即无委派能力这条纪律只有在"每个新事件都显式写了"时才成立。
            # 用 enum 而不是 maximum（本方言没有 maximum，见 SUPPORTED_KEYWORDS）。
            "max_subtasks": _enum(0, 1, 2, 3, 4),
            # 父链（可选；根任务不写）。子任务的 depth 恒为 1。
            "parent_task_id": _s(),
            "parent_run_id": _s(),
            "subtask_id": _s(),
            "depth": _enum(0, 1),
        },
        required=(
            "task_id",
            "goal",
            "goal_revision",
            "lease_epoch",
            "workspaces",
            "exec_policy",
            "control",
            "memory_scopes",
            "outbound_allow",
            "mcp_servers",
            "max_subtasks",
        ),
    ),
    "task.revised": _obj(
        {
            "task_id": _s(),
            "goal": _s(),
            "goal_revision": _i(minimum=1),
            "lease_epoch": _i(minimum=1),
            "workspaces": _arr(_WORKSPACE_BINDING),
        },
        required=("task_id", "goal", "goal_revision", "lease_epoch", "workspaces"),
    ),
    "tool.intent": _obj(
        {
            "call_id": _s(),
            "tool_id": _DYNAMIC_TOOL_ID,
            "args": _obj({}, open=True),
            "args_hash": _SHA256,
            "request_id": _s(),
            "request_digest": _SHA256,
            "dispatch_id": _s(),
            "grant_id": _s(),
            "approval_id": _opt(_s()),
            "attempt_id": _i(minimum=1),
            "claim_epoch": _i(minimum=0),
        },
        required=(
            "call_id",
            "tool_id",
            "args",
            "args_hash",
            "request_id",
            "request_digest",
            "dispatch_id",
            "grant_id",
            "attempt_id",
            "claim_epoch",
        ),
    ),
    "tool.not_dispatched": _obj(
        {"call_id": _s(), "tool_id": _DYNAMIC_TOOL_ID, "reason": _s()},
        required=("call_id", "tool_id", "reason"),
    ),
    "tool.result": _obj(
        {
            "call_id": _s(),
            # 恢复旧日志时，历史 intent 里的工具 id 可能已不在当前白名单
            # （红线 8：schema 变即新工具）。新 intent 由 dispatcher 用 SPECS
            # 校验，这里按"工具 id 形状"而不是当前白名单收口。
            "tool_id": _s(pattern=r"^[a-z][a-z0-9_.]*$"),
            "status": _TOOL_STATUS,
            "effect_state": _EFFECT_STATE,
            # 工具输出由各自的 C2 工具契约决定（7+ 个工具、参数与返回各不同），
            # C3 只冻结"它是一个对象、与 status/effect_state 同层"，不冒充
            # 已经冻结了每个工具的输出字段。未冻结项在文档里列明。
            "output": _obj({}, open=True),
        },
        required=("call_id", "tool_id", "status", "effect_state", "output"),
    ),
    "undo.completed": _obj(
        {
            "run_id": _s(),
            "control_run": _s(),
            "restored": _arr(_s()),
            "deleted": _arr(_s()),
            "entries": _arr(_UNDO_ENTRY),
        },
        required=("run_id", "control_run", "restored", "deleted", "entries"),
    ),
    "undo.failed": _obj(
        {"run_id": _s(), "control_run": _s(), "error": _s()},
        required=("run_id", "control_run", "error"),
    ),
}


#: 旧版（0）里出现过、但当前契约不再写的字段。键 → 宽松 schema。
#: 它们只用于**读取/兼容写入**旧数据，不是新写入允许的字段。
LEGACY_EXTRA_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "run.start": {
        "workspace": _s(),
        "a": {"type": ["string", "integer", "boolean"]},
        "x": {"type": ["string", "integer", "boolean"]},
    },
    "run.end": {"x": {"type": ["string", "integer", "boolean"]}},
    "model.intent": {"ok": _b()},
}

#: 旧版里出现过、但当前 schema 类型/枚举更紧的字段：按"旧版允许"放宽。
LEGACY_RELAXATIONS: dict[str, dict[str, dict[str, Any]]] = {
    # 旧版 run.model_turn 的收尾原因/用量来自旧 adapter，未必满足当前封闭集/形状
    "run.model_turn": {
        "finish_reason": {"type": ["string", "null"]},
        "usage": {"type": ["object", "null"]},
    },
    # 旧版 run.start 的 limits 形状不统一（有的只有部分键）
    "run.start": {"limits": {"type": "object"}},
    # 旧版/测试夹具里的 tool.intent 可以是"未知工具 + 非规范哈希"
    "tool.intent": {"tool_id": {"type": "string"}, "args_hash": {"type": "string"}},
    # 旧版/测试夹具里的 model.intent 哈希字段不一定是 64-hex
    "model.intent": {"messages_hash": {"type": "string"}},
    # 旧夹具的 receipt 形状早于当前约束（rcpt_legacy / action 里放旧字段 /
    # evidence 是合成字符串数组）
    "receipt": {
        "receipt_id": {"type": "string"},
        "action": {"type": "string"},
        "args_hash": {"type": "string"},
        "evidence": {"type": "array"},
    },
}


def _legacy_schema(current: dict[str, Any], extras: dict[str, dict[str, Any]]) -> dict:
    schema = copy.deepcopy(current)
    schema["required"] = []
    for key, sub in extras.items():
        schema["properties"].setdefault(key, sub)
    schema["description"] = (
        "冻结前旧版（payload_version=0）：只放宽并集，不按新版默认补值；"
        "读取旧事件时按字段“本来就没有”处理。"
    )
    return schema


def _relax(schema: dict[str, Any], relaxations: dict[str, dict[str, Any]]) -> dict:
    for key, sub in relaxations.items():
        schema["properties"][key] = sub
    return schema


LEGACY_SCHEMAS: dict[str, dict[str, Any]] = {
    name: _relax(
        _legacy_schema(schema, LEGACY_EXTRA_PROPERTIES.get(name, {})),
        LEGACY_RELAXATIONS.get(name, {}),
    )
    for name, schema in CURRENT_SCHEMAS.items()
}

_RECOGNISED_VERSIONS: dict[str, frozenset[int]] = {
    name: frozenset({LEGACY_PAYLOAD_VERSION, CURRENT_PAYLOAD_VERSION})
    for name in CURRENT_SCHEMAS
}


# ---------------------------------------------------------------- 注册时校验


def _check_type_decl(node_type: Any, where: str) -> None:
    if isinstance(node_type, str):
        if node_type not in JSON_TYPES:
            raise UnsupportedSchemaKeyword(f"{where}：未知 type={node_type!r}")
        return
    if isinstance(node_type, list) and node_type:
        for item in node_type:
            if item not in JSON_TYPES:
                raise UnsupportedSchemaKeyword(f"{where}：未知 type={item!r}")
        if len(set(node_type)) != len(node_type):
            raise UnsupportedSchemaKeyword(f"{where}：type 列表有重复值")
        return
    raise UnsupportedSchemaKeyword(f"{where}：type 必须是类型名字符串或列表")


def _check_schema_node(node: Any, where: str) -> None:
    if not isinstance(node, dict):
        raise UnsupportedSchemaKeyword(f"{where}：schema 必须是对象")
    unknown = set(node) - SUPPORTED_KEYWORDS
    if unknown:
        raise UnsupportedSchemaKeyword(
            f"{where}：不支持的关键字 {sorted(unknown)}（只实现 {sorted(SUPPORTED_KEYWORDS)}）"
        )
    if "type" in node:
        _check_type_decl(node["type"], where)
    if "properties" in node:
        props = node["properties"]
        if not isinstance(props, dict):
            raise UnsupportedSchemaKeyword(f"{where}：properties 必须是对象")
        for key, sub in props.items():
            if not isinstance(key, str):
                raise UnsupportedSchemaKeyword(f"{where}：properties 的键必须是字符串")
            _check_schema_node(sub, f"{where}.{key}")
    if "required" in node:
        req = node["required"]
        if not isinstance(req, list) or not all(isinstance(k, str) for k in req):
            raise UnsupportedSchemaKeyword(f"{where}：required 必须是字符串数组")
        props = node.get("properties", {})
        missing = [k for k in req if k not in props]
        if missing:
            raise UnsupportedSchemaKeyword(
                f"{where}：required 指向未声明的字段 {missing}"
            )
    if "additionalProperties" in node:
        ap = node["additionalProperties"]
        if isinstance(ap, bool):
            pass
        elif isinstance(ap, dict):
            _check_schema_node(ap, f"{where}.additionalProperties")
        else:
            raise UnsupportedSchemaKeyword(
                f"{where}：additionalProperties 必须是布尔或 schema"
            )
    if "items" in node:
        _check_schema_node(node["items"], f"{where}.items")
    if "enum" in node:
        if not isinstance(node["enum"], list) or not node["enum"]:
            raise UnsupportedSchemaKeyword(f"{where}：enum 必须是非空数组")
    if "pattern" in node:
        if not isinstance(node["pattern"], str):
            raise UnsupportedSchemaKeyword(f"{where}：pattern 必须是字符串")
        try:
            re.compile(node["pattern"])
        except re.error as exc:  # pragma: no cover - 常量写错才会到
            raise UnsupportedSchemaKeyword(
                f"{where}：pattern 不是合法正则（{exc}）"
            ) from exc
    if "minimum" in node and not isinstance(node["minimum"], int):
        raise UnsupportedSchemaKeyword(f"{where}：minimum 必须是整数")
    for key in ("description", "title"):
        if key in node and not isinstance(node[key], str):
            raise UnsupportedSchemaKeyword(f"{where}：{key} 必须是字符串")


def _check_registry_schema(name: str, schema: dict[str, Any], version: int) -> None:
    where = f"{name}@v{version}"
    _check_schema_node(schema, where)
    if schema.get("type") != "object":
        raise UnsupportedSchemaKeyword(f"{where}：payload 根必须是 object")
    if schema.get("additionalProperties") is not False:
        raise UnsupportedSchemaKeyword(
            f"{where}：payload 根必须显式 additionalProperties=false"
        )


for _name, _schema in CURRENT_SCHEMAS.items():
    _check_registry_schema(_name, _schema, CURRENT_PAYLOAD_VERSION)
for _name, _schema in LEGACY_SCHEMAS.items():
    _check_registry_schema(_name, _schema, LEGACY_PAYLOAD_VERSION)


def require_supported_dialect(dialect: Any) -> None:
    """契约文档的 `$schema` 必须是本模块实现的 dialect（fail-closed）。"""
    if dialect != SUPPORTED_DIALECT:
        raise UnsupportedSchemaKeyword(
            f"不支持的 JSON Schema dialect：{dialect!r}"
            f"（只实现 {SUPPORTED_DIALECT}）"
        )


def validate_schema_definition(schema: Any, *, where: str = "<schema>") -> None:
    """注册时校验一份 schema 定义：关键字白名单 + 结构合法性（fail-closed）。"""
    _check_schema_node(schema, where)


def ensure_registry(event_types: frozenset[str] | set[str]) -> None:
    """注册表与契约必须一一对应：少一个类型 = 该类型无法写入（fail-closed）。"""
    declared = set(event_types)
    missing = declared - set(CURRENT_SCHEMAS)
    extra = set(CURRENT_SCHEMAS) - declared
    if missing or extra:
        raise UnsupportedSchemaKeyword(
            "payload 契约与 EVENT_TYPES 不一致："
            f"缺少 schema={sorted(missing)}，多出 schema={sorted(extra)}"
        )


def current_version(event_type: str) -> int:
    return CURRENT_PAYLOAD_VERSION if event_type in CURRENT_SCHEMAS else 0


def known_versions(event_type: str) -> frozenset[int]:
    if event_type not in _RECOGNISED_VERSIONS:
        raise UnknownPayloadVersion(f"未知事件类型，没有 payload 契约：{event_type!r}")
    return _RECOGNISED_VERSIONS[event_type]


def schema_for(event_type: str, version: int) -> dict[str, Any]:
    if event_type not in CURRENT_SCHEMAS:
        raise UnknownPayloadVersion(f"未知事件类型，没有 payload 契约：{event_type!r}")
    if version == CURRENT_PAYLOAD_VERSION:
        return CURRENT_SCHEMAS[event_type]
    if version == LEGACY_PAYLOAD_VERSION:
        return LEGACY_SCHEMAS[event_type]
    raise UnknownPayloadVersion(
        f"事件类型 {event_type!r} 的 payload 版本 {version} 本程序不认识"
        f"（已知 {sorted(_RECOGNISED_VERSIONS[event_type])}）"
    )


# ---------------------------------------------------------------- 校验


def _json_type(value: Any) -> str:
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
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__  # pragma: no cover - JSON 里不会出现


def _type_matches(expected: Any, value: Any) -> bool:
    actual = _json_type(value)
    wanted = [expected] if isinstance(expected, str) else expected
    if actual == "number" and "integer" in wanted:
        # JSON Schema：integer 不接受 1.5；但 2.0 是合法 integer，Python 里
        # 解析成 float 2.0 时按值判断。
        return float(value).is_integer()
    return actual in wanted


def _check_value(
    schema: dict[str, Any], value: Any, path: str, errors: list[str], event_type: str
) -> None:
    if "type" in schema and not _type_matches(schema["type"], value):
        wanted = schema["type"]
        errors.append(f"{path}：期望类型 {wanted}，实际 {_json_type(value)}")
        return
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}：值 {value!r} 不在封闭集 {schema['enum']} 内")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}：值 {value!r} != const {schema['const']!r}")
        return
    if isinstance(value, str) and "pattern" in schema:
        if re.search(schema["pattern"], value) is None:
            errors.append(f"{path}：字符串 {value!r} 不匹配 {schema['pattern']!r}")
            return
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            errors.append(f"{path}：{value} < minimum {schema['minimum']}")
            return
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}：缺少必填字段 {key!r}")
        additional = schema.get("additionalProperties", True)
        for key, sub in value.items():
            child = f"{path}.{key}"
            if key in props:
                _check_value(props[key], sub, child, errors, event_type)
            elif additional is False:
                errors.append(
                    f"{child}：不允许的字段 {key!r}（additionalProperties=false）"
                )
            elif isinstance(additional, dict):
                _check_value(additional, sub, child, errors, event_type)
        return
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check_value(schema["items"], item, f"{path}[{index}]", errors, event_type)


def payload_errors(
    event_type: str, payload: Any, version: int = CURRENT_PAYLOAD_VERSION
) -> list[str]:
    """返回该 payload 在指定版本下的全部契约错误（空列表 = 合法）。"""
    schema = schema_for(event_type, version)
    errors: list[str] = []
    _check_value(schema, payload, "$", errors, event_type)
    return errors


def validate_payload(
    event_type: str, payload: Any, version: int = CURRENT_PAYLOAD_VERSION
) -> None:
    errors = payload_errors(event_type, payload, version)
    if errors:
        raise PayloadContractError(event_type, errors[0].split("：", 1)[0], "；".join(errors[:5]))


def detect_version(
    event_type: str, payload: Any, *, allow_legacy: bool = False
) -> int:
    """写入口的版本判定：**默认只接受当前契约（v1）**。

    - `allow_legacy=False`（默认，生产路径）：只按 v1 校验；不合法直接抛
      `PayloadContractError`。**绝不**自动退回 v0——否则"新写入写漏字段"会被
      静默记成一条冒充旧版的记录，等于给写入校验开免检通道。
    - `allow_legacy=True`（仅显式请求）：v1 不合法时再按 v0（封闭的历史并集）
      校验，通过则返回 0；两边都不合法仍拒绝。只给夹具/历史播种/迁移测试用，
      生产调用点一律不许传。

    事件行会用旁表记下实际版本，读取时不会假装它是新版。
    """
    if not payload_errors(event_type, payload, CURRENT_PAYLOAD_VERSION):
        return CURRENT_PAYLOAD_VERSION
    if allow_legacy and not payload_errors(
        event_type, payload, LEGACY_PAYLOAD_VERSION
    ):
        return LEGACY_PAYLOAD_VERSION
    first = payload_errors(event_type, payload, CURRENT_PAYLOAD_VERSION)[0]
    raise PayloadContractError(
        event_type, first.split("：", 1)[0], first.split("：", 1)[1]
    )


# ---------------------------------------------------------------- 样例


_SAMPLE_STRINGS = (
    "x",
    "run_x",
    "c1",
    "0" * 64,
    "rcpt_0123456789ab",
    "c4.auto",
    "end_turn",
    "applied",
)


def _sample_for(schema: dict[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    node_type = schema.get("type")
    types = [node_type] if isinstance(node_type, str) else list(node_type or ["object"])
    for candidate in types:
        if candidate == "null":
            continue
        if candidate == "string":
            pattern = schema.get("pattern")
            if pattern is None:
                return _SAMPLE_STRINGS[0]
            for sample in _SAMPLE_STRINGS:
                if re.search(pattern, sample) is not None:
                    return sample
            raise UnsupportedSchemaKeyword(f"无法为 pattern={pattern!r} 生成样例")
        if candidate == "integer":
            minimum = schema.get("minimum", 0)
            return int(minimum)
        if candidate == "number":
            return float(schema.get("minimum", 0))
        if candidate == "boolean":
            return False
        if candidate == "array":
            return []
        if candidate == "object":
            props = schema.get("properties", {})
            required = schema.get("required", list(props))
            return {key: _sample_for(props[key]) for key in required if key in props}
    return None


def minimal_payload(event_type: str) -> dict[str, Any]:
    """构造该类型的最小合法 v1 payload（测试与夹具用，且会被校验兜底）。"""
    schema = schema_for(event_type, CURRENT_PAYLOAD_VERSION)
    payload = _sample_for(schema)
    validate_payload(event_type, payload)
    return payload
