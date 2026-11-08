"""工具注册表与三个受限工具。

M0 白名单只有三个（docs/M0-scope.md §4）。任何额外写入路径都是 bug。
参数校验在执行侧做，不信任模型给出的参数。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import OverwriteRefused, ToolArgumentError, ToolNotAllowed
from .workspace import Workspace, normalize_name

READ_TEXT_LIMIT = 64 * 1024  # 单次读取上限，超出截断并标记


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    version: str
    description: str
    input_schema: dict[str, Any]
    side_effect_level: str  # pure | idempotent | non_idempotent
    exec_model: str = "E1"  # E1 | E3


@dataclass
class ToolResult:
    status: str  # succeeded | failed | rejected | cancelled | unknown
    effect_state: str  # none_applied | applied | partial | unknown
    output: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


# ---------------------------------------------------------------- schema 校验


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def validate_args(args: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """最小 JSON Schema 子集：type / required / properties / additionalProperties。

    够用即可；M0 不引入 jsonschema 依赖，避免四个目标架构的轮子问题。
    """
    if not isinstance(args, dict):
        raise ToolArgumentError(f"参数必须是对象，实际是 {type(args).__name__}")

    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    missing = [k for k in required if k not in args]
    if missing:
        raise ToolArgumentError(f"缺少必填参数：{missing}")

    if schema.get("additionalProperties") is False:
        extra = [k for k in args if k not in props]
        if extra:
            raise ToolArgumentError(f"不允许的参数：{extra}")

    for key, value in args.items():
        spec = props.get(key)
        if spec and "type" in spec and not _type_ok(value, spec["type"]):
            raise ToolArgumentError(
                f"参数 {key} 期望 {spec['type']}，实际 {type(value).__name__}"
            )
    return args


# ---------------------------------------------------------------- 具体实现


def _list_files(ws: Workspace, args: dict[str, Any]) -> ToolResult:
    sub = args.get("path", ".")
    target = ws.resolve(sub)
    if not target.is_dir():
        return ToolResult("failed", "none_applied", {"error": f"不是目录：{sub}"})

    entries = []
    for p in sorted(target.iterdir(), key=lambda x: x.name):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": normalize_name(p.name),
                "is_dir": p.is_dir(),
                "size": st.st_size,
            }
        )
    return ToolResult("succeeded", "none_applied", {"entries": entries})


def _read_text(ws: Workspace, args: dict[str, Any]) -> ToolResult:
    target = ws.resolve(args["path"])
    if not target.is_file():
        return ToolResult("failed", "none_applied", {"error": "不是文件"})

    total = target.stat().st_size
    with target.open("rb") as fh:
        raw = fh.read(READ_TEXT_LIMIT)
    truncated = total > len(raw)

    # 按字节截断可能切断多字节字符：回退最多 3 字节以落在码点边界上
    for back in range(4):
        try:
            text = raw[:len(raw) - back].decode("utf-8") if back else raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    else:
        return ToolResult(
            "failed", "none_applied", {"error": "不是 UTF-8 文本，拒绝读取"}
        )

    return ToolResult(
        "succeeded",
        "none_applied",
        {
            "path": ws.relative(target),
            "content": text,
            "bytes": len(raw),
            "total_bytes": total,
            "truncated": truncated,
        },
    )


def _create_report(ws: Workspace, args: dict[str, Any]) -> ToolResult:
    target = ws.resolve(args["name"])

    # 验收 A6：已存在必须拒绝，不留覆盖的可能
    if target.exists():
        raise OverwriteRefused(f"目标已存在，拒绝覆盖：{args['name']}")

    target.parent.mkdir(parents=True, exist_ok=True)
    data = args["content"].encode("utf-8")
    target.write_bytes(data)

    return ToolResult(
        "succeeded",
        "applied",
        {
            "path": ws.relative(target),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )


# ---------------------------------------------------------------- 注册表

_HANDLERS: dict[str, Callable[[Workspace, dict[str, Any]], ToolResult]] = {
    "list_files": _list_files,
    "read_text": _read_text,
    "create_report": _create_report,
}

SPECS: dict[str, ToolSpec] = {
    "list_files": ToolSpec(
        tool_id="list_files",
        version="1.0",
        description="列出目录下的文件与子目录，返回名字、大小、是否目录。",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        side_effect_level="pure",
    ),
    "read_text": ToolSpec(
        tool_id="read_text",
        version="1.0",
        description="读取一个 UTF-8 文本文件，超长会截断并标记 truncated。",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effect_level="pure",
    ),
    "create_report": ToolSpec(
        tool_id="create_report",
        version="1.0",
        description="在 workspace 内新建报告文件。目标已存在时拒绝，不覆盖。",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
    ),
}


def tool_manifest() -> list[dict[str, Any]]:
    """给模型看的工具清单（C2 的 ToolDefinition 投影）。"""
    return [
        {
            "name": s.tool_id,
            "description": s.description,
            "input_schema": s.input_schema,
        }
        for s in SPECS.values()
    ]


def execute(ws: Workspace, tool_id: str, args: dict[str, Any]) -> ToolResult:
    """执行入口。这里是唯一的授权/校验点（PRD §8.1）。"""
    if tool_id not in _HANDLERS:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")

    validate_args(args, SPECS[tool_id].input_schema)
    return _HANDLERS[tool_id](ws, args)
