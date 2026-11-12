"""MCP 动态工具准入的回归测试（PR C：注册表 + schema 归一 + 身份绑定）。

对端的形状用真实的 `mcp.McpServerState` / `mcp.McpTool` 对象手工构造（准入是纯函数，
不需要真子进程）；文件末尾另有一条**真子进程**的端到端用例，用
`tests/fixtures/fake_mcp_server.py` 走 `McpClients` → `build_registry` 的整条路。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tianshu.errors import ToolSchemaError
from tianshu.mcp import McpClients, McpServerConfig, McpServerState, McpTool
from tianshu.tool_registry import (
    MAX_DESCRIPTION_LENGTH,
    MAX_TOTAL_MCP_TOOLS,
    McpAdmission,
    ToolRegistry,
    admit_mcp_tools,
    build_registry,
    current_registry,
    reset_registry,
    set_registry,
    tool_version_for,
)
from tianshu.tools import SPECS, description_digest, schema_digest, tool_manifest
from tianshu.toolschema import normalize_object_schema

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


class _StubClients:
    """只实现准入用到的两个查询（`configs` / `states()`）。

    状态对象是真的 `McpServerState`，所以被测代码走的是生产形状，不是另造一套。
    """

    def __init__(
        self,
        states: list[McpServerState],
        configs: dict[str, McpServerConfig] | None = None,
    ):
        self._states = tuple(states)
        self.configs = dict(configs or {})

    def states(self) -> tuple[McpServerState, ...]:
        return self._states


def _tool(name: str, schema: dict, description: str = "") -> McpTool:
    return McpTool.from_raw(
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
        }
    )


def _state(name: str, *tools: McpTool) -> McpServerState:
    return McpServerState(
        name=name,
        status="ready",
        protocol_version="2025-06-18",
        tools=tuple(tools),
    )


def _object_schema(**properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
    }


# ---------------------------------------------------------------- 归一


def test_normalize_narrows_additional_properties():
    normalized, narrowed = normalize_object_schema(
        {"type": "object", "properties": {"a": {"type": "string"}}}
    )
    assert normalized["additionalProperties"] is False
    assert narrowed == ("additionalProperties",)


def test_normalize_keeps_explicit_additional_properties_true_as_is():
    normalized, narrowed = normalize_object_schema(
        {"type": "object", "properties": {}, "additionalProperties": True}
    )
    assert normalized["additionalProperties"] is True
    assert narrowed == ()


@pytest.mark.parametrize(
    "schema, match",
    [
        ({"type": "object", "properties": {"a": {"$ref": "#/b"}}}, r"\$ref"),
        ({"type": "object", "properties": {"a": {"oneOf": [{"type": "string"}]}}}, "oneOf"),
        ({"type": "object", "properties": {"a": {"type": "number", "minimum": 1.5}}}, "整数"),
        ({"type": "object", "properties": {"a": {"type": ["string", "null"]}}}, "单一类型"),
        ({"type": "object", "properties": {"a": {"type": "array"}}}, "items"),
        (
            {"type": "object", "properties": {"a": {"type": "object", "additionalProperties": {}}}},
            "bool",
        ),
        ({"properties": {"a": {"type": "string"}}}, "object"),
        ({"type": "object", "properties": {}, "required": ["missing"]}, "未声明"),
    ],
)
def test_normalize_rejects_unsupported_shapes(schema, match):
    with pytest.raises(ToolSchemaError, match=match):
        normalize_object_schema(schema, where="mcp__t__x")


# ---------------------------------------------------------------- 准入


def test_admission_binds_identity_and_manifest_row():
    clients = _StubClients(
        [_state("fs", _tool("read", _object_schema(a={"type": "string"}), "读一个路径"))]
    )
    admission = admit_mcp_tools(clients)
    assert [item.binding.tool_id for item in admission.admitted] == ["mcp__fs__read"]
    item = admission.admitted[0]
    assert item.binding.server == "fs" and item.binding.tool == "read"
    assert item.binding.tool_version.startswith("mcp1:")
    assert item.binding.narrowed == ("additionalProperties",)
    assert item.binding.uri == "mcp://fs/read"
    assert item.spec.description.startswith("[MCP fs/read] ")
    # 保守档位：MCP 工具没有可回滚的本地写前状态。
    assert item.spec.action == "exec"
    assert item.spec.side_effect_level == "non_idempotent"
    assert item.spec.exec_model == "E3"
    assert item.spec.exposed is True
    assert item.spec.path_arg == ""
    assert item.spec.input_schema["additionalProperties"] is False
    assert admission.rejected == ()

    registry = ToolRegistry(SPECS, admission=admission)
    row = next(tool for tool in registry.manifest() if tool["name"] == "mcp__fs__read")
    assert row["input_schema"]["properties"] == {"a": {"type": "string"}}
    assert registry.mcp_binding("mcp__fs__read") is not None
    assert registry.is_mcp("mcp__fs__read") is True
    assert registry.is_mcp("read_text") is False


def test_tool_version_binds_the_raw_schema():
    """原始 schema 必须进版本摘要：归一后的 schema 相同也要换版本。

    为什么单独测 `tool_version_for()`：归一现在是严格拒绝（类型不匹配的关键字
    不再被"丢掉"），能通过准入的两个不同原始 schema 一定会归一出不同的 schema，
    所以"只差原始摘要"这条性质只能在这个纯函数上钉住。它的意义是：将来归一逻辑
    变宽松时，旧批准不能因为"归一结果恰好相同"被静默复用。
    """
    normalized = {"type": "object", "properties": {}, "additionalProperties": False}
    desc = "[MCP s/t] x"
    first = tool_version_for(
        server="s", tool="t", raw_schema={"a": 1}, normalized=normalized, description=desc
    )
    second = tool_version_for(
        server="s", tool="t", raw_schema={"a": 2}, normalized=normalized, description=desc
    )
    assert first != second


@pytest.mark.parametrize(
    "scalar, match",
    [
        ({"type": "string", "minimum": 1}, "minimum"),
        ({"type": "integer", "minLength": 1}, "minLength"),
        ({"type": "number", "pattern": "^x$"}, "pattern"),
        ({"type": "boolean", "minimum": 0}, "minimum"),
    ],
)
def test_normalize_rejects_type_mismatched_keywords(scalar, match):
    """JSON Schema 里 `"type": "string"` 配 `minimum` 没有语义；必须拒绝工具，
    不能静默丢掉关键字（§3.3 的"不静默丢"包括这一种）。"""
    with pytest.raises(ToolSchemaError, match=match):
        normalize_object_schema({"type": "object", "properties": {"a": scalar}})


def test_tool_version_changes_with_description():
    schema = {"type": "object", "properties": {}}
    first = admit_mcp_tools(
        _StubClients([_state("s", _tool("t", schema, "旧描述"))])
    ).admitted[0].binding
    second = admit_mcp_tools(
        _StubClients([_state("s", _tool("t", schema, "新描述"))])
    ).admitted[0].binding
    assert first.description_hash != second.description_hash
    assert first.tool_version != second.tool_version


def test_rejection_does_not_take_other_tools_down():
    clients = _StubClients(
        [
            _state(
                "s",
                _tool("good", _object_schema(a={"type": "integer"})),
                _tool("bad", {"type": "object", "properties": {"x": {"$ref": "#/y"}}}),
            )
        ]
    )
    admission = admit_mcp_tools(clients)
    assert [item.binding.tool_id for item in admission.admitted] == ["mcp__s__good"]
    assert [(row.server, row.tool) for row in admission.rejected] == [("s", "bad")]
    assert "$ref" in admission.rejected[0].reason


def test_name_outside_resource_charset_is_rejected():
    clients = _StubClients([_state("s", _tool("Bad Tool", {"type": "object", "properties": {}}))])
    admission = admit_mcp_tools(clients)
    assert admission.admitted == ()
    assert "字符集" in admission.rejected[0].reason


def test_tool_id_collision_rejects_both_sides():
    schema = {"type": "object", "properties": {}}
    clients = _StubClients(
        [
            _state("a-b", _tool("c", schema)),
            _state("a_b", _tool("c", schema)),
        ]
    )
    admission = admit_mcp_tools(clients)
    assert admission.admitted == ()
    assert len(admission.rejected) == 2
    assert {row.server for row in admission.rejected} == {"a-b", "a_b"}
    assert all("映射冲突" in row.reason for row in admission.rejected)


def test_tool_collision_covers_tool_segment_too():
    schema = {"type": "object", "properties": {}}
    clients = _StubClients([_state("s", _tool("a-b", schema), _tool("a_b", schema))])
    admission = admit_mcp_tools(clients)
    assert admission.admitted == ()
    assert len(admission.rejected) == 2


def test_per_server_tool_cap_is_enforced_by_name_order():
    schema = {"type": "object", "properties": {}}
    config = McpServerConfig(
        name="s", command=(sys.executable, str(FIXTURE)), max_tools=1
    )
    clients = _StubClients(
        [_state("s", _tool("b", schema), _tool("a", schema))], {"s": config}
    )
    admission = admit_mcp_tools(clients)
    assert [item.binding.tool for item in admission.admitted] == ["a"]
    assert [(row.tool, "max_tools=1" in row.reason) for row in admission.rejected] == [
        ("b", True)
    ]


def test_total_cap_is_enforced():
    schema = {"type": "object", "properties": {}}
    clients = _StubClients(
        [_state("s1", _tool("a", schema)), _state("s2", _tool("b", schema))]
    )
    admission = admit_mcp_tools(clients, total_limit=1)
    assert len(admission.admitted) == 1
    assert "上限" in admission.rejected[0].reason
    assert MAX_TOTAL_MCP_TOOLS >= 64


def test_description_is_prefixed_and_bounded():
    schema = {"type": "object", "properties": {}}
    long_text = "很长的描述" * 400
    binding = admit_mcp_tools(
        _StubClients([_state("s", _tool("t", schema, long_text))])
    ).admitted[0]
    description = binding.spec.description
    assert description.startswith("[MCP s/t] ")
    assert len(description) == MAX_DESCRIPTION_LENGTH
    assert description.endswith("…")
    assert binding.binding.description_hash == description_digest(description)


def test_truncated_discovery_is_reported_as_rejected_row():
    schema = {"type": "object", "properties": {}}
    clients = _StubClients(
        [
            McpServerState(
                name="s",
                status="ready",
                tools=(_tool("a", schema),),
                tools_truncated=True,
            )
        ]
    )
    admission = admit_mcp_tools(clients)
    assert [item.binding.tool for item in admission.admitted] == ["a"]
    assert [(row.server, row.tool) for row in admission.rejected] == [("s", "<truncated>")]
    assert "截断" in admission.rejected[0].reason


def test_unavailable_server_admits_nothing():
    schema = {"type": "object", "properties": {}}
    clients = _StubClients(
        [
            McpServerState(
                name="off",
                status="unavailable",
                diagnostics=("握手失败",),
                tools=(_tool("t", schema),),
            )
        ]
    )
    admission = admit_mcp_tools(clients)
    assert admission.admitted == ()
    assert admission.rejected == ()


# ---------------------------------------------------------------- 注册表


def test_registry_without_clients_equals_builtins():
    registry = build_registry(None)
    assert registry.ids() == tuple(sorted(SPECS))
    assert registry.builtin_ids() == tuple(sorted(SPECS))
    # **顺序也要一致**：断言用 list 相等而不是 sorted，否则"逐字节不变"漏掉顺序。
    assert registry.manifest() == tool_manifest()
    assert registry.rejected_tools() == []


def test_manifest_keeps_builtin_order_and_appends_dynamic_tools():
    schema = {"type": "object", "properties": {}}
    admission = admit_mcp_tools(_StubClients([_state("zz", _tool("t", schema))]))
    registry = ToolRegistry(SPECS, admission=admission)
    manifest = registry.manifest()
    names = [row["name"] for row in manifest]
    assert names[: len(tool_manifest())] == [row["name"] for row in tool_manifest()]
    assert names[-1] == "mcp__zz__t"


def test_registry_lists_dynamic_tools_after_builtins_only_by_sort():
    schema = {"type": "object", "properties": {}}
    admission = admit_mcp_tools(_StubClients([_state("zz", _tool("t", schema))]))
    registry = ToolRegistry(SPECS, admission=admission)
    ids = registry.ids()
    assert ids == tuple(sorted(ids))
    assert "mcp__zz__t" in ids
    assert len(ids) == len(SPECS) + 1
    assert registry.spec("mcp__zz__t") is not None
    assert registry.spec("not_a_tool") is None


def test_process_registry_set_and_reset():
    try:
        assert current_registry().ids() == tuple(sorted(SPECS))
        admission = admit_mcp_tools(
            _StubClients([_state("s", _tool("t", {"type": "object", "properties": {}}))])
        )
        set_registry(ToolRegistry(SPECS, admission=admission))
        assert "mcp__s__t" in current_registry().ids()
        reset_registry()
        assert current_registry().ids() == tuple(sorted(SPECS))
    finally:
        reset_registry()


def test_empty_admission_has_empty_views():
    empty = McpAdmission()
    assert empty.specs == {} and empty.bindings == {} and empty.rejected_rows() == []


# ---------------------------------------------------------------- 端到端（真子进程）


def test_end_to_end_real_client_truncates_at_max_tools():
    config = McpServerConfig(name="fake", command=(sys.executable, str(FIXTURE)), max_tools=1)
    clients = McpClients({"fake": config})
    try:
        clients.start_all()
        registry = build_registry(clients)
        names = [row["name"] for row in registry.manifest() if row["name"].startswith("mcp__")]
        assert names == ["mcp__fake__echo"]
        assert any(row["tool"] == "<truncated>" for row in registry.rejected_tools())
    finally:
        clients.close_all()


def test_end_to_end_with_real_stdio_server():
    config = McpServerConfig(
        name="fake", command=(sys.executable, str(FIXTURE))
    )
    clients = McpClients({"fake": config})
    try:
        clients.start_all()
        assert clients.ready_names() == ("fake",)
        registry = build_registry(clients)
        ids = {tool["name"] for tool in registry.manifest()}
        assert {"mcp__fake__echo", "mcp__fake__add"} <= ids
        echo = registry.spec("mcp__fake__echo")
        assert echo is not None
        assert echo.version.startswith("mcp1:")
        assert echo.input_schema["additionalProperties"] is False
        assert echo.input_schema["properties"]["message"] == {"type": "string"}
        binding = registry.mcp_binding("mcp__fake__echo")
        assert binding is not None and binding.uri == "mcp://fake/echo"
        assert schema_digest(echo.input_schema) == binding.schema_hash
    finally:
        clients.close_all()