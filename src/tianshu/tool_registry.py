"""动态工具注册表：内置 `SPECS` + 运行时准入的 MCP 工具。

## 为什么要有这一层

`tools.SPECS` 是**导入期静态表**（导入时 `validate_registry()` 跑一遍），模型可见
清单、派发白名单、`run.start.tools`、控制面清单全直接读它。MCP 工具是**运行时
发现**的，没位置可放；而"随手往全局 dict 里塞"会让三件事同时失真：

1. **身份**：红线 8 要求「工具 schema 变化即视为新工具」。MCP server 改一个字段，
   旧批准必须失效——这要求每个工具带一个由**原始 schema + 归一 schema + 描述**
   算出的版本，而不是一个固定字符串。
2. **准入**：MCP 的 `inputSchema` 是任意 JSON Schema，我们只认
   `toolschema` 的封闭方言。认不出的工具必须**不准入**（模型看不见、派发拒绝），
   而不是"先放进来再想办法"。
3. **可解释**：被拒绝的工具与原因要能被 `tianshu mcp list` 读到；静默丢弃等于让
   用户以为 server 没有这个工具。

## 边界（本层不做的事）

- **不做授权**：准入 ≠ 可以调用。能不能调用由派发层判定（任务白名单 + 逐次人工
  批准），本层只回答"这个工具在不在注册表里、身份是什么"。
- **不启动 server**：`McpClients` 已经启动并发现完了，这里只做映射与准入。
- **不碰 `SPECS`**：内置表只读；动态工具放 `mcp` 侧表，两处查询用同一个
  `ToolRegistry`。没有 MCP 配置时，注册表的可见面与 `SPECS` **逐字节相同**。

## 名字映射

`mcp://<server>/<tool>` 的 `server`/`tool` 由资源解析器限定为 `[a-z0-9_.-]{1,64}`；
工具 id 还要满足事件契约的形状（`^[a-z][a-z0-9_.]*$`，不含 `-`）。所以：

- 工具 id = `mcp__` + `server` 与 `tool` 的 `-`/`.` 都换成 `_`；
- **两段都做冲突检查**：归一后撞成同一个 id（例如 `a-b` 与 `a_b`，或 server 段与
  tool 段各撞一次）→ **两个都拒绝**，不做"先到先得"；
- tool 名不在资源字符集里 → 该工具不准入（资源引用表达不了它，批准也就无从绑定）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ToolSchemaError
from .mcp import MCP_DEFAULT_MAX_TOOLS, McpClients, McpTool
from .permissions import ACTION_EXEC
from .toolschema import compile_schema, normalize_object_schema

__all__ = [
    "MCP_TOOL_PREFIX",
    "AdmittedMcpTool",
    "McpAdmission",
    "McpToolBinding",
    "RejectedMcpTool",
    "ToolRegistry",
    "admit_mcp_tools",
    "build_registry",
    "current_registry",
    "reset_registry",
    "set_registry",
    "tool_id_for_server",
]

#: MCP 工具在模型清单里的统一前缀（一眼看出它是外部程序）。
MCP_TOOL_PREFIX = "mcp__"
#: 工具 id 的形状（与事件契约里动态工具 id 的 pattern 一致）。
_TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
#: 资源段允许的形状（与 `resources.McpTarget` 逐字一致）。
_SEGMENT_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
#: 工具 id 上限（太长的事件/日志对排障没好处，且契约 pattern 没有长度上限）。
MAX_TOOL_ID_LENGTH = 96
#: 进工具描述的外部文本上限（描述会进模型请求，也会进审批摘要）。
MAX_DESCRIPTION_LENGTH = 1_024
#: 全进程最多准入多少 MCP 工具（防止一个 server 把上下文塞满）。
MAX_TOTAL_MCP_TOOLS = 128


# ---------------------------------------------------------------- 绑定与准入记录


@dataclass(frozen=True)
class McpToolBinding:
    """一个 MCP 工具的身份。

    `tool_version` 由**原始 schema + 归一 schema + 描述**共同决定：server 改了一个
    我们本来就不认的关键字，也算换了一个工具（红线 8：schema 变即新工具，不热替换
    既有批准）。
    """

    server: str
    tool: str
    tool_id: str
    tool_version: str
    schema_hash: str
    raw_schema_hash: str
    description_hash: str
    narrowed: tuple[str, ...] = ()

    @property
    def uri(self) -> str:
        from .resources import McpTarget

        return McpTarget(server=self.server, tool=self.tool).to_uri()


@dataclass(frozen=True)
class AdmittedMcpTool:
    binding: McpToolBinding
    spec: Any  # tools.ToolSpec（延后 import，避免模块环）


@dataclass(frozen=True)
class RejectedMcpTool:
    server: str
    tool: str
    reason: str


@dataclass(frozen=True)
class McpAdmission:
    """一次准入的结果：谁进来了、谁没进来、为什么。"""

    admitted: tuple[AdmittedMcpTool, ...] = ()
    rejected: tuple[RejectedMcpTool, ...] = ()

    @property
    def specs(self) -> dict[str, Any]:
        return {item.binding.tool_id: item.spec for item in self.admitted}

    @property
    def bindings(self) -> dict[str, McpToolBinding]:
        return {item.binding.tool_id: item.binding for item in self.admitted}

    def rejected_rows(self) -> list[dict[str, str]]:
        return [
            {"server": item.server, "tool": item.tool, "reason": item.reason}
            for item in self.rejected
        ]


# ---------------------------------------------------------------- 名字映射


def _digest(payload: Any) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sanitize_segment(raw: str) -> str:
    """资源段 → 工具 id 的一段（`-`/`.` 换 `_`；其它字符在准入时已被拒）。"""
    return raw.replace("-", "_").replace(".", "_")


def tool_id_for_server(server: str, tool: str) -> str:
    """`mcp://s/t` → `mcp__s__t` 的工具 id（冲突由 `admit_mcp_tools` 处理）。"""
    return f"{MCP_TOOL_PREFIX}{sanitize_segment(server)}__{sanitize_segment(tool)}"


def tool_version_for(
    *, server: str, tool: str, raw_schema: dict[str, Any], normalized: dict[str, Any],
    description: str,
) -> str:
    return "mcp1:" + _digest(
        {
            "server": server,
            "tool": tool,
            "raw_schema": raw_schema,
            "normalized_schema": normalized,
            "description": description,
        }
    )[:16]


def _bounded_description(server: str, tool: str, raw: str) -> str:
    prefix = f"[MCP {server}/{tool}] "
    text = " ".join(raw.split()) or "（server 没有给描述）"
    room = MAX_DESCRIPTION_LENGTH - len(prefix)
    if len(text) > room:
        text = text[: max(room - 1, 0)] + "…"
    return prefix + text


# ---------------------------------------------------------------- 准入


def _reject(rejected: list[RejectedMcpTool], server: str, tool: str, reason: str) -> None:
    rejected.append(RejectedMcpTool(server=server, tool=tool, reason=reason))


def _admit_one(
    server: str,
    tool: McpTool,
    *,
    rejected: list[RejectedMcpTool],
) -> AdmittedMcpTool | None:
    """把一个发现到的工具归一/绑定成 `ToolSpec`；任何一步不成立就记拒绝原因。"""
    from .tools import ToolSpec, description_digest, schema_digest

    if not _SEGMENT_RE.match(tool.name):
        _reject(
            rejected,
            server,
            tool.name,
            f"工具名 {tool.name!r} 不在资源段字符集 {_SEGMENT_RE.pattern} 内，"
            "mcp:// 资源引用表达不了它（批准也无法绑定）",
        )
        return None
    tool_id = tool_id_for_server(server, tool.name)
    if len(tool_id) > MAX_TOOL_ID_LENGTH:
        _reject(
            rejected, server, tool.name, f"映射出的工具 id 超过 {MAX_TOOL_ID_LENGTH} 字符"
        )
        return None
    if not _TOOL_ID_RE.match(tool_id):
        _reject(
            rejected, server, tool.name, f"映射出的工具 id 形状不合法：{tool_id!r}"
        )
        return None
    try:
        normalized, narrowed = normalize_object_schema(
            tool.input_schema, where=tool_id
        )
    except ToolSchemaError as exc:
        _reject(rejected, server, tool.name, f"inputSchema 不准入：{exc}")
        return None
    description = _bounded_description(server, tool.name, tool.description)
    try:
        compile_schema(normalized, where=tool_id)
    except ToolSchemaError as exc:  # pragma: no cover - 归一函数自己已编译过
        _reject(rejected, server, tool.name, f"归一后的 schema 编译失败：{exc}")
        return None
    binding = McpToolBinding(
        server=server,
        tool=tool.name,
        tool_id=tool_id,
        tool_version=tool_version_for(
            server=server,
            tool=tool.name,
            raw_schema=tool.raw.get("inputSchema", {}),
            normalized=normalized,
            description=description,
        ),
        schema_hash=schema_digest(normalized),
        raw_schema_hash=_digest(tool.raw.get("inputSchema", {})),
        description_hash=description_digest(description),
        narrowed=narrowed,
    )
    spec = ToolSpec(
        tool_id=tool_id,
        version=binding.tool_version,
        description=description,
        input_schema=normalized,
        # MCP 工具没有可回滚的本地写前状态：按最保守的一档记（非幂等、E3）。
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_EXEC,
        exposed=True,
        path_arg="",
    )
    return AdmittedMcpTool(binding=binding, spec=spec)


def admit_mcp_tools(
    clients: McpClients,
    *,
    total_limit: int = MAX_TOTAL_MCP_TOOLS,
) -> McpAdmission:
    """从已启动的 `McpClients` 里做一次准入（只处理 `ready` 的 server）。

    顺序是固定的（server 名、工具名排序），所以同一份 server 清单每次得到**同一组**
    id 与版本——回放、审批摘要与测试都依赖这一点。
    """
    if isinstance(total_limit, bool) or not isinstance(total_limit, int) or total_limit <= 0:
        raise ValueError(f"total_limit 必须是正整数：{total_limit!r}")
    rejected: list[RejectedMcpTool] = []
    candidate: list[tuple[str, McpTool]] = []
    configs = clients.configs
    for state in clients.states():
        if not state.ready:
            continue
        config = configs.get(state.name)
        limit = MCP_DEFAULT_MAX_TOOLS if config is None else config.max_tools
        tools = sorted(state.tools, key=lambda item: item.name)
        if len(tools) > limit:
            for extra in tools[limit:]:
                _reject(
                    rejected,
                    state.name,
                    extra.name,
                    f"超过该 server 的 max_tools={limit}（按工具名排序后保留前 {limit} 个）",
                )
            tools = tools[:limit]
        for tool in tools:
            candidate.append((state.name, tool))

    # 冲突检查：工具 id 是"server + tool 各段归一"的产物，两段都可能撞。
    by_id: dict[str, list[tuple[str, str]]] = {}
    for server, tool in candidate:
        by_id.setdefault(tool_id_for_server(server, tool.name), []).append(
            (server, tool.name)
        )
    for tool_id, owners in sorted(by_id.items()):
        if len(owners) > 1:
            detail = ", ".join(f"{server}/{tool}" for server, tool in owners)
            for server, tool_name in owners:
                _reject(
                    rejected,
                    server,
                    tool_name,
                    f"工具 id 映射冲突：{detail} 都归一到 {tool_id}（两段都检查，"
                    "不做先到先得）",
                )
    colliding = {tool_id for tool_id, owners in by_id.items() if len(owners) > 1}

    admitted: list[AdmittedMcpTool] = []
    for server, tool in candidate:
        if tool_id_for_server(server, tool.name) in colliding:
            continue
        item = _admit_one(server, tool, rejected=rejected)
        if item is None:
            continue
        if len(admitted) >= total_limit:
            _reject(
                rejected,
                server,
                tool.name,
                f"超过全进程 MCP 工具上限 {total_limit}（按 server/工具名排序后截断）",
            )
            continue
        admitted.append(item)
    return McpAdmission(admitted=tuple(admitted), rejected=tuple(rejected))


# ---------------------------------------------------------------- 注册表


class ToolRegistry:
    """只读工具注册表：内置 `SPECS`（引用，不复制）+ 已准入的 MCP 工具。

    `base` 刻意保存**同一个 dict 对象**：既有测试会 `monkeypatch` `SPECS` 来验证
    注册期校验，复制一份会让那些补丁对注册表不可见。
    """

    def __init__(
        self,
        base: Mapping[str, Any],
        *,
        admission: McpAdmission | None = None,
    ) -> None:
        self._base = base
        self._mcp = {} if admission is None else admission.specs
        self.admission = admission or McpAdmission()

    # ---- 查询

    def spec(self, tool_id: str) -> Any | None:
        found = self._base.get(tool_id)
        if found is not None:
            return found
        return self._mcp.get(tool_id)

    def __contains__(self, tool_id: object) -> bool:
        return self.spec(tool_id) is not None if isinstance(tool_id, str) else False

    def ids(self) -> tuple[str, ...]:
        """全部工具 id（含 `exposed=False`）。与 `run.start.tools` 的旧口径
        `sorted(SPECS)` 在无 MCP 时逐字相同。"""
        return tuple(sorted(set(self._base) | set(self._mcp)))

    def builtin_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._base))

    def manifest(self) -> list[dict[str, Any]]:
        """给模型看的清单（只投影 `exposed=True`，按 id 排序）。"""
        rows: list[dict[str, Any]] = []
        for tool_id in self.ids():
            spec = self.spec(tool_id)
            if spec is None or not spec.exposed:
                continue
            rows.append(
                {
                    "name": spec.tool_id,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                }
            )
        return rows

    def mcp_binding(self, tool_id: str) -> McpToolBinding | None:
        return self.admission.bindings.get(tool_id)

    def is_mcp(self, tool_id: str) -> bool:
        return tool_id in self.admission.bindings

    def mcp_specs(self) -> dict[str, Any]:
        return dict(self._mcp)

    def rejected_tools(self) -> list[dict[str, str]]:
        return self.admission.rejected_rows()


#: 进程级默认注册表。与 `exec_backend()` 同一套模式：装配点设一次，测试复位。
_CURRENT: ToolRegistry | None = None


def current_registry() -> ToolRegistry:
    """当前生效的注册表；没设过 = 只有内置 `SPECS`（工具面与旧版逐字节相同）。"""
    global _CURRENT
    if _CURRENT is None:
        from .tools import SPECS

        _CURRENT = ToolRegistry(SPECS)
    return _CURRENT


def set_registry(registry: ToolRegistry | None) -> None:
    """装配点用：设进程级注册表（None = 回到"只有内置工具"）。"""
    if registry is not None and not isinstance(registry, ToolRegistry):
        raise TypeError(f"registry 必须是 ToolRegistry：{type(registry).__name__}")
    global _CURRENT
    _CURRENT = registry


def reset_registry() -> None:
    """测试用：回到默认注册表（内置 `SPECS`）。"""
    set_registry(None)


def build_registry(clients: McpClients | None) -> ToolRegistry:
    """从已启动的 MCP 客户端造注册表；`clients=None` = 只有内置工具。"""
    from .tools import SPECS

    if clients is None:
        return ToolRegistry(SPECS)
    admission = admit_mcp_tools(clients)
    return ToolRegistry(SPECS, admission=admission)


def target_for(tool_id: str) -> Any | None:
    """MCP 工具的资源引用（`mcp://server/tool`）；内置工具返回 None。"""
    from .resources import McpTarget

    binding = current_registry().mcp_binding(tool_id)
    if binding is None:
        return None
    return McpTarget(server=binding.server, tool=binding.tool)