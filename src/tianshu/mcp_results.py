"""MCP 入站结果的内部 canonical content-ref codec。

事件与游标只保存白名单元数据；完整有序 content blocks 与 structuredContent
保存在内容寻址 blob。解码一律 fail closed：未知 schema、畸形 JSON、超限、摘要或
元数据不符都不恢复正文，也不把引用降级成普通输出。
"""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .contents import ContentError, parse_ref
from .errors import TianshuError
from .events import canonical_bytes, sha256_hex

SCHEMA = "tianshu.mcp-result.v1"
MAX_RESULT_BYTES = 1_048_576
MAX_STRUCTURED_BYTES = 1_048_576

_FIELDS = frozenset({
    "schema", "server", "tool", "tool_version", "protocol_version", "request_id",
    "is_error", "content_blocks", "text_parts", "unsupported_types", "structured",
    "bytes", "truncated", "dropped_bytes", "dropped_blocks", "duration_ms",
})
_SAFE_FIELDS = frozenset({
    "schema", "ref", "size", "sha256", "after_state", "blob_kind", "blob_path",
    "server", "tool", "tool_version", "protocol_version", "request_id",
    "is_error", "bytes", "truncated", "dropped_bytes", "dropped_blocks", "duration_ms",
})
_BOUND_FIELDS = (
    "server", "tool", "tool_version", "protocol_version", "request_id", "is_error",
    "bytes", "truncated", "dropped_bytes", "dropped_blocks", "duration_ms",
)


class McpResultError(TianshuError):
    """canonical MCP 结果不可信、不可读或超过本版本上限。"""


def _canonical_bytes(value: Any) -> bytes:
    """把所有 canonical 编码失败封闭在 MCP codec 边界内。"""
    try:
        return canonical_bytes(value)
    except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise McpResultError("MCP 结果无法 canonical 编码") from exc


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 100:
        raise McpResultError("MCP 结果 JSON 嵌套过深")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise McpResultError("MCP 结果含非有限数字")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise McpResultError("MCP 结果 JSON 对象键必须是字符串")
        return {key: _json_value(item, depth=depth + 1) for key, item in value.items()}
    raise McpResultError("MCP 结果含非 JSON 值")


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise McpResultError(f"MCP 结果 {name} 必须是非负整数")
    return value


def _text_parts(blocks: list[dict[str, Any]]) -> list[str]:
    return [block["text"] for block in blocks
            if block.get("type") == "text" and isinstance(block.get("text"), str)]


def _unsupported(blocks: list[dict[str, Any]]) -> list[str]:
    return [block["type"] for block in blocks
            if block.get("type") != "text" and isinstance(block.get("type"), str)]


def _validate(envelope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != _FIELDS:
        raise McpResultError("MCP canonical 结果字段集合不符")
    if envelope["schema"] != SCHEMA:
        raise McpResultError("不支持的 MCP canonical 结果 schema")
    for name in ("server", "tool", "tool_version", "protocol_version"):
        if not isinstance(envelope[name], str):
            raise McpResultError(f"MCP 结果 {name} 必须是字符串")
    if not isinstance(envelope["is_error"], bool) or not isinstance(envelope["truncated"], bool):
        raise McpResultError("MCP 结果布尔字段形状不符")
    for name in ("request_id", "bytes", "dropped_bytes", "dropped_blocks", "duration_ms"):
        _integer(envelope[name], name)
    blocks = envelope["content_blocks"]
    if not isinstance(blocks, list):
        raise McpResultError("MCP content_blocks 必须是数组")
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise McpResultError("MCP content block 形状不符")
        if block["type"] == "text" and not isinstance(block.get("text"), str):
            raise McpResultError("MCP text block 缺少字符串 text")
    if envelope["text_parts"] != _text_parts(blocks):
        raise McpResultError("MCP text_parts 与有序 content blocks 不符")
    if envelope["unsupported_types"] != _unsupported(blocks):
        raise McpResultError("MCP unsupported_types 与 content blocks 不符")
    structured_size = len(_canonical_bytes(_json_value(envelope["structured"])))
    if structured_size > MAX_STRUCTURED_BYTES:
        raise McpResultError("MCP structuredContent 超过 canonical 上限")
    return envelope


def encode_mcp_result(called: Any, metadata: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """返回 canonical bytes 与可持久化的 ref-only 安全输出。"""
    blocks = _json_value(list(called.content))
    envelope = {
        "schema": SCHEMA,
        "server": metadata["server"],
        "tool": metadata["tool"],
        "tool_version": metadata.get("tool_version", ""),
        "protocol_version": metadata.get("protocol_version", ""),
        "request_id": called.request_id,
        "is_error": called.is_error,
        "content_blocks": blocks,
        "text_parts": list(called.text_parts),
        "unsupported_types": list(called.unsupported_types),
        "structured": _json_value(called.structured),
        "bytes": called.bytes,
        "truncated": called.truncated,
        "dropped_bytes": called.dropped_bytes,
        "dropped_blocks": called.dropped_blocks,
        "duration_ms": called.duration_ms,
    }
    _validate(envelope)
    body = _canonical_bytes(envelope)
    if len(body) > MAX_RESULT_BYTES:
        raise McpResultError("MCP canonical 结果超过上限")
    return body, safe_mcp_output(envelope, body)


def _decode_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise McpResultError("MCP canonical JSON 含重复键")
        result[key] = value
    return result


def _bad_constant(value: str) -> None:
    raise McpResultError(f"MCP canonical JSON 含非法常量：{value}")


def decode_mcp_result(body: bytes) -> dict[str, Any]:
    if not isinstance(body, bytes) or len(body) > MAX_RESULT_BYTES:
        raise McpResultError("MCP canonical blob 为空、非字节或超限")
    try:
        raw = json.loads(
            body.decode("utf-8"), object_pairs_hook=_decode_object,
            parse_constant=_bad_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise McpResultError("MCP canonical blob 不是严格 UTF-8 JSON") from exc
    envelope = _validate(raw)
    if _canonical_bytes(envelope) != body:
        raise McpResultError("MCP canonical blob 不是规范编码")
    return envelope


def safe_mcp_output(envelope: dict[str, Any], body: bytes) -> dict[str, Any]:
    digest = sha256_hex(body)
    output = {name: deepcopy(envelope[name]) for name in _BOUND_FIELDS}
    output.update({
        "schema": SCHEMA,
        "ref": f"content://sha256:{digest}",
        "size": len(body),
        "sha256": digest,
        "after_state": "mcp:" + digest,
        "blob_kind": "event",
        "blob_path": "",
    })
    return output


def set_blob_kind(
    output: dict[str, Any], *, event: bool, blob_dir: Path | None = None, log: Any = None,
) -> dict[str, Any]:
    output["blob_kind"] = "event" if event else "snapshot"
    directory = getattr(log, "blob_dir", None) if event and log is not None else blob_dir
    if directory is None or log is None or str(log.db_path) == ":memory:":
        raise McpResultError("MCP canonical blob 缺少可持久化定位")
    relative = os.path.relpath(Path(directory), Path(log.db_path).parent)
    output["blob_path"] = relative
    return output


def is_ref_output(output: Any) -> bool:
    """识别 content-ref 标记；任一标记出现都必须严格校验，不能当旧 inline 放行。"""
    if not isinstance(output, dict) or not isinstance(output.get("mcp"), dict):
        return False
    markers = {"schema", "ref", "size", "sha256", "blob_kind", "blob_path"}
    return bool(markers & set(output["mcp"]))


def project_mcp_output(output: dict[str, Any]) -> dict[str, Any]:
    """完整 live 输出 → ref-only；旧 inline 输出原样返回。"""
    if not is_ref_output(output):
        return deepcopy(output)
    mcp = output["mcp"]
    missing = _SAFE_FIELDS - set(mcp)
    if missing:
        raise McpResultError(f"MCP ref 输出缺少安全字段：{sorted(missing)}")
    allowed = _SAFE_FIELDS | {"content", "structured", "unsupported_content"}
    if set(mcp) - allowed:
        raise McpResultError("MCP ref 输出含白名单以外字段")
    return {"mcp": {name: deepcopy(mcp[name]) for name in sorted(_SAFE_FIELDS)}}


def _resolved_path(path: Any) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise McpResultError("MCP canonical blob 路径无法解析") from exc


def _inside_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _blob_candidate(root: Any, digest: str, data_root: Path | None = None) -> Path:
    resolved_root = _resolved_path(root)
    if data_root is not None and not _inside_root(resolved_root, data_root):
        raise McpResultError("MCP canonical blob 路径逃逸允许的数据根")
    candidate = _resolved_path(resolved_root / digest)
    if not _inside_root(candidate, resolved_root):
        raise McpResultError("MCP canonical blob 路径逃逸允许的数据根")
    return candidate


def blob_candidates(log: Any, output: dict[str, Any], blob_dir: Path | None = None) -> list[Path]:
    mcp = output["mcp"]
    data_root = None
    if log is not None and str(log.db_path) != ":memory:":
        data_root = _resolved_path(Path(log.db_path).parent)
    try:
        digest = parse_ref(mcp.get("ref"))
    except Exception as exc:  # noqa: BLE001 - 统一成 codec 的显式失败
        raise McpResultError("MCP ref 输出 content-ref 畸形") from exc
    locator = mcp.get("blob_path")
    if not isinstance(locator, str):
        raise McpResultError("MCP ref 输出 blob_path 必须是字符串")
    if locator:
        if Path(locator).is_absolute() or log is None or str(log.db_path) == ":memory:":
            raise McpResultError("MCP ref 输出 blob_path 定位不合法")
        directory = _resolved_path(data_root / locator)
        if not _inside_root(directory, data_root):
            raise McpResultError("MCP ref 输出 blob_path 逃逸数据根")
        return [_blob_candidate(directory, digest, data_root)]
    if mcp.get("blob_kind") == "event":
        roots = [blob_dir, getattr(log, "blob_dir", None), getattr(log, "_mcp_blob_dir", None)]
    elif mcp.get("blob_kind") == "snapshot":
        roots = [blob_dir, getattr(log, "_mcp_blob_dir", None)]
        if log is not None and roots[-1] is None:
            from .restore_access import default_material_paths

            roots.append(default_material_paths(log.db_path, log.data_root)[0] / "blobs")
            roots.append(default_material_paths(log.db_path, log.data_root)[0] / "preimage_blobs")
    else:
        raise McpResultError("MCP ref 输出 blob_kind 不合法")
    return [_blob_candidate(root, digest, data_root) for root in roots if root is not None]


def read_mcp_result(
    output: dict[str, Any], *, log: Any = None, blob_dir: Path | None = None,
) -> tuple[dict[str, Any], bytes]:
    if not is_ref_output(output):
        raise McpResultError("不是 MCP content-ref 输出")
    mcp = output["mcp"]
    allowed = _SAFE_FIELDS | {"content", "structured", "unsupported_content"}
    if not _SAFE_FIELDS <= set(mcp) or set(mcp) - allowed:
        raise McpResultError("MCP ref 输出安全字段集合不符")
    if mcp.get("schema") != SCHEMA:
        raise McpResultError("不支持的 MCP ref 输出 schema")
    try:
        digest = parse_ref(mcp.get("ref"))
    except ContentError as exc:
        raise McpResultError("MCP ref 输出 content-ref 畸形") from exc
    if mcp.get("sha256") != digest or mcp.get("after_state") != "mcp:" + digest:
        raise McpResultError("MCP ref/sha256/after_state 不一致")
    if log is None and blob_dir is None:
        raise McpResultError("读取 MCP canonical blob 需要 EventLog 或 blob_dir")
    errors: list[str] = []
    for path in blob_candidates(log, output, blob_dir):
        try:
            body = path.read_bytes()
        except OSError as exc:
            errors.append(str(exc))
            continue
        if len(body) != mcp.get("size") or sha256_hex(body) != digest:
            raise McpResultError("MCP canonical blob 尺寸或摘要不符")
        envelope = decode_mcp_result(body)
        for name in _BOUND_FIELDS:
            if mcp.get(name) != envelope[name]:
                raise McpResultError(f"MCP 安全元数据与 canonical blob 不符：{name}")
        return envelope, body
    raise McpResultError("MCP canonical blob 缺失或不可读：" + "; ".join(errors))


def hydrate_mcp_output(
    output: dict[str, Any], *, log: Any = None, blob_dir: Path | None = None,
) -> dict[str, Any]:
    """在临时副本上恢复完整正文；旧 inline 形状不变。"""
    result = deepcopy(output)
    if not is_ref_output(result):
        return result
    envelope, body = read_mcp_result(result, log=log, blob_dir=blob_dir)
    mcp = result["mcp"]
    mcp.update({
        "content": list(envelope["text_parts"]),
        "unsupported_content": list(envelope["unsupported_types"]),
        "structured": deepcopy(envelope["structured"]),
    })
    # size/sha256 已由 read_mcp_result 对 canonical bytes 重算。
    assert len(body) == mcp["size"]
    return result


def hydrate_mcp_messages(
    messages: list[dict[str, Any]], *, log: Any = None, blob_dir: Path | None = None,
) -> list[dict[str, Any]]:
    result = deepcopy(messages)
    for message in result:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        try:
            output = json.loads(message["content"])
        except json.JSONDecodeError:
            continue
        if is_ref_output(output):
            message["content"] = json.dumps(
                hydrate_mcp_output(output, log=log, blob_dir=blob_dir),
                ensure_ascii=False, sort_keys=True,
            )
    return result


def sanitize_cursor_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """所有 cursor 持久化入口共用：只保留 MCP ref-only 安全形状。"""
    result = deepcopy(payload)
    messages = result.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            try:
                output = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if is_ref_output(output):
                message["content"] = json.dumps(
                    project_mcp_output(output), ensure_ascii=False, sort_keys=True,
                )
    return result


def referenced_mcp_digests(log: Any) -> set[str]:
    """从 append-only tool.result 投影 MCP blob 引用；Receipt 与它同事务同摘要。"""
    from .contents import ContentError

    digests: set[str] = set()
    try:
        events = log.read_types(("tool.result",))
    except Exception as exc:  # noqa: BLE001 - 数不清引用时调用方必须保留 blob
        raise McpResultError("无法投影 MCP blob 引用") from exc
    for event in events:
        output = event.payload.get("output")
        if not is_ref_output(output):
            continue
        try:
            digests.add(parse_ref(output["mcp"].get("ref")))
        except ContentError as exc:
            raise McpResultError("MCP 事件含畸形 content-ref") from exc
    return digests


__all__ = [
    "MAX_RESULT_BYTES", "MAX_STRUCTURED_BYTES", "SCHEMA", "McpResultError",
    "decode_mcp_result", "encode_mcp_result", "hydrate_mcp_messages",
    "hydrate_mcp_output", "is_ref_output", "project_mcp_output", "read_mcp_result",
    "referenced_mcp_digests", "sanitize_cursor_payload", "set_blob_kind",
]
