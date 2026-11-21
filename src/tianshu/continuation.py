"""C1 不透明续接值与纯协议投影；不读取凭据、不调用模型。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CONTINUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "adapter", "endpoint", "model", "output_items"],
    "properties": {
        "schema_version": {"type": "string", "const": "1.0"},
        "adapter": {"type": "string", "enum": ["openai-responses"]},
        "endpoint": {"type": "string", "pattern": r"^https?://[^?#]+/responses$"},
        "model": {"type": "string", "pattern": r"\S"},
        "output_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": [
                        "reasoning", "message", "function_call",
                    ]},
                },
            },
        },
    },
}


COMPACTION_REF_VERSION = "1.1"
COMPACTION_SCHEMA = copy.deepcopy(CONTINUATION_SCHEMA)
COMPACTION_SCHEMA["properties"]["schema_version"]["const"] = COMPACTION_REF_VERSION
COMPACTION_SCHEMA["properties"]["output_items"]["items"]["properties"]["type"]["enum"].append(
    "compaction"
)


def validate_compact_threshold(value: Any, *, responses: bool) -> int | None:
    if value is not None and (not responses or type(value) is not int
                              or not 1024 <= value <= 2_000_000):
        raise ValueError("Responses 压缩阈值须为 1024–2000000 的整数，或为空")
    return value


def normalize_endpoint(url: str) -> str:
    """规范化 scope 地址；错误不反射可能含凭据的原始 URL。"""
    if not isinstance(url, str) or not url or any(c.isspace() for c in url):
        raise ValueError("Responses 端点必须是合法 http(s) URL")
    try:
        p = urlsplit(url)
        port = p.port
        if (
            p.scheme not in ("http", "https") or not p.hostname
            or p.username is not None or p.password is not None
            or "?" in url or "#" in url or "\\" in url
        ):
            raise ValueError
        host = p.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != (443 if p.scheme == "https" else 80):
            host = f"{host}:{port}"
        return urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        raise ValueError("Responses 端点不得包含凭据、查询或 fragment，且端口必须合法") from None


def responses_endpoint(base_url: str) -> str:
    return normalize_endpoint(base_url.rstrip("/") + "/responses")


def _text(value: Any, *, nonempty: bool = False) -> bool:
    return isinstance(value, str) and (not nonempty or bool(value.strip()))


def _same_json(left: Any, right: Any) -> bool:
    """Python 的 True == 1 不构成同一 JSON 提案。"""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def project_output(
    items: Any, *, compaction: bool = False,
) -> tuple[str, list[dict[str, Any]], bool, bool, bool]:
    """验证公开文本/函数/reasoning items，并给出中立投影。

    后三项分别为 refusal、全部完成、存在文本或函数项。密文只保存在原始值里，
    从不参与文本投影。非法参数字符串保持原样，交给执行侧拒绝。
    """
    if not isinstance(items, list):
        raise ValueError("Responses output 必须是数组")
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    ids: set[str] = set()
    call_ids: set[str] = set()
    refusal = False
    complete = True
    visible = False
    compacted = False
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("Responses output item 必须是对象")
        kind = item.get("type")
        if kind not in ("reasoning", "message", "function_call", "compaction"):
            raise ValueError("Responses output item 类型不受支持")
        item_id = item.get("id")
        if "id" in item or kind != "function_call":
            if not _text(item_id, nonempty=True) or item_id in ids:
                raise ValueError("Responses item id 缺失或重复")
            ids.add(item_id)
        status = item.get("status")
        if "status" in item or kind == "message":
            if status not in ("completed", "incomplete", "in_progress"):
                raise ValueError("Responses item status 非法")
            complete = complete and status == "completed"
        if kind == "compaction":
            if (not compaction or index != 0 or status not in (None, "completed")
                    or not _text(item.get("encrypted_content"), nonempty=True)):
                raise ValueError("Responses compaction 必须是有密文的首个输出项")
            compacted = True
        elif kind == "reasoning":
            if not _text(item.get("encrypted_content"), nonempty=True):
                raise ValueError("Responses reasoning 缺少加密续接内容")
            summary = item.get("summary")
            if not isinstance(summary, list) or any(
                not isinstance(part, dict) or part.get("type") != "summary_text"
                or not _text(part.get("text")) for part in summary
            ):
                raise ValueError("Responses reasoning summary 非法")
            content = item.get("content", [])
            if not isinstance(content, list) or any(
                not isinstance(part, dict) or part.get("type") != "reasoning_text"
                or not _text(part.get("text")) for part in content
            ):
                raise ValueError("Responses reasoning content 非法")
        elif kind == "message":
            visible = True
            if item.get("role") != "assistant":
                raise ValueError("Responses output message 必须来自 assistant")
            if "phase" in item and item["phase"] not in (
                None, "commentary", "final_answer",
            ):
                raise ValueError("Responses message phase 非法")
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("Responses message content 必须是数组")
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError("Responses content part 必须是对象")
                if part.get("type") == "output_text" and _text(part.get("text")):
                    if not isinstance(part.get("annotations"), list):
                        raise ValueError("Responses output_text annotations 非法")
                    text.append(part["text"])
                elif part.get("type") == "refusal" and _text(part.get("refusal")):
                    refusal = True
                    text.append(part["refusal"])
                else:
                    raise ValueError("Responses content 类型或字段不受支持")
        else:
            visible = True
            call_id, name, args = (item.get(k) for k in ("call_id", "name", "arguments"))
            if (
                not _text(call_id, nonempty=True) or call_id in call_ids
                or not _text(name, nonempty=True) or not isinstance(args, str)
            ):
                raise ValueError("Responses function_call 缺字段或 call_id 重复")
            call_ids.add(call_id)
            try:
                parsed = json.loads(args)
            except (ValueError, RecursionError):
                parsed = args
            calls.append({
                "call_id": call_id, "name": name,
                "arguments": parsed if isinstance(parsed, dict) else args,
            })
    if compaction and not compacted:
        raise ValueError("Responses 缺少承诺的 compaction 边界")
    return "".join(text), calls, refusal, complete, visible


@dataclass(frozen=True, repr=False, init=False)
class ContinuationRef:
    """JSON 字节持有的不可变值；输入/输出均分离，repr 不含 output_items。"""

    _encoded: str
    _scope: tuple[str, str, str]
    _has_compaction: bool

    def __init__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or set(payload) != set(
            CONTINUATION_SCHEMA["required"]
        ):
            raise ValueError("ContinuationRef 字段不完整或含未知字段")
        version = payload["schema_version"]
        if (version not in ("1.0", COMPACTION_REF_VERSION)
                or payload["adapter"] != "openai-responses"):
            raise ValueError("ContinuationRef 版本或 adapter 不受支持")
        if not _text(payload["model"], nonempty=True):
            raise ValueError("ContinuationRef model 必须显式指定")
        endpoint = normalize_endpoint(payload["endpoint"])
        if not endpoint.endswith("/responses"):
            raise ValueError("ContinuationRef endpoint 必须是完整 Responses 地址")
        try:
            encoded = json.dumps({**payload, "endpoint": endpoint}, allow_nan=False)
            detached = json.loads(encoded)
            project_output(detached["output_items"], compaction=version == COMPACTION_REF_VERSION)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("ContinuationRef output_items 不满足续接契约") from None
        object.__setattr__(self, "_encoded", encoded)
        object.__setattr__(self, "_has_compaction", version == COMPACTION_REF_VERSION)
        object.__setattr__(self, "_scope", (payload["adapter"], endpoint, payload["model"]))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ContinuationRef:
        return cls(payload)

    @property
    def scope(self) -> tuple[str, str, str]:
        return self._scope

    @property
    def has_compaction(self) -> bool:
        return self._has_compaction

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._encoded)

    def __repr__(self) -> str:
        return f"ContinuationRef(scope={self._scope!r})"

    def validate_projection(self, payload: dict[str, Any]) -> None:
        content, calls, refusal, complete, visible = project_output(
            self.to_payload()["output_items"], compaction=self.has_compaction,
        )
        if content != payload.get("content", "") or not _same_json(
            calls, payload.get("tool_calls") or [],
        ):
            raise ValueError("ContinuationRef 与规范化文本/函数提案不一致")
        finish = payload.get("finish_reason")
        if self.has_compaction and (not complete or refusal
                                    or finish not in (None, "tool_use", "end_turn")):
            raise ValueError("未完整结束的 Responses 回合不能成为压缩边界")
        if finish == "tool_use" and (not calls or refusal or not complete):
            raise ValueError("未完成的 Responses 输出不能成为可派发提案")
        if finish == "end_turn" and (calls or refusal or not complete or not visible):
            raise ValueError("Responses 输出不构成正常文本终态")


def require_scope(
    messages: list[dict[str, Any]], scope: tuple[str, str, str] | None,
) -> None:
    for message in messages:
        if "continuation" not in message:
            continue
        ref = ContinuationRef.from_payload(message["continuation"])
        if message.get("role") != "assistant" or ref.scope != scope:
            raise ValueError("模型续接作用域不兼容，请创建新 Run")
