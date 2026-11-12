"""MCP 客户端（stdio）的回归测试。对端是 `tests/fixtures/fake_mcp_server.py`：
一个**不 import 客户端**的真子进程，只按协议形状说话。

为什么用真子进程：这一层要证明的正是"管道、线程、超时、崩溃"这些只有真进程
才有的行为。同进程的假对象能证明的只是"我们的两次函数调用前后一致"。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from tianshu.mcp import (
    MCP_CONFIG_ENV,
    MCP_STDERR_TAIL_BYTES,
    McpCallError,
    McpClients,
    McpClosed,
    McpConfigError,
    McpError,
    McpServerConfig,
    McpServerUnavailable,
    McpTimeout,
    StdioServer,
    config_from_env,
    load_mcp_config,
    parse_mcp_config,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _config(name: str = "fake", *args: str, **kwargs) -> McpServerConfig:
    extras = list(args)
    for key, value in kwargs.pop("extras", {}).items():  # pragma: no cover - 未使用
        extras.extend([f"--{key}", str(value)])
    return McpServerConfig(
        name=name,
        command=(sys.executable, str(FIXTURE), *extras),
        **kwargs,
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def server():
    created: list[StdioServer] = []

    def make(*args: str, **kwargs) -> StdioServer:
        instance = StdioServer(_config("fake", *args, **kwargs))
        created.append(instance)
        instance.start()
        return instance

    yield make
    for instance in created:
        instance.close()


# ---------------------------------------------------------------- 配置


def test_config_rejects_relative_command():
    with pytest.raises(McpConfigError, match="绝对路径"):
        McpServerConfig(name="x", command=("python3", "server.py"))


@pytest.mark.parametrize(
    "raw, match",
    [
        ({"name": "Bad Name", "command": ["/bin/echo"]}, "server 名"),
        ({"name": "x", "command": []}, "command"),
        ({"name": "x", "command": ["/bin/echo"], "shell": True}, "不认识的字段"),
        ({"name": "x", "command": ["/bin/echo"], "cwd": "relative"}, "cwd"),
        ({"name": "x", "command": ["/bin/echo"], "env": {"A=B": "v"}}, "env 键"),
        ({"name": "x", "command": ["/bin/echo"], "call_timeout_ms": 0}, "call_timeout_ms"),
        (
            {"name": "x", "command": ["/bin/echo"], "max_result_bytes": 1_048_577},
            "max_result_bytes",
        ),
    ],
)
def test_config_validation_is_fail_closed(raw, match):
    name = raw.pop("name")
    with pytest.raises(McpConfigError, match=match):
        McpServerConfig.from_dict(name, raw)


def test_config_file_roundtrip_and_missing_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"servers": {"fs": {"command": ["/bin/echo", "hi"], "env": {"A": "1"}}}}),
        encoding="utf-8",
    )
    configs = load_mcp_config(path)
    assert set(configs) == {"fs"}
    assert configs["fs"].command == ("/bin/echo", "hi")
    assert configs["fs"].env == {"A": "1"}
    with pytest.raises(McpConfigError, match="不存在"):
        load_mcp_config(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(McpConfigError, match="不是合法 JSON"):
        load_mcp_config(bad)


def test_parse_config_rejects_unknown_root_keys():
    with pytest.raises(McpConfigError, match="不认识的键"):
        parse_mcp_config({"servers": {}, "transport": "http"})


def test_config_from_env_absent_means_disabled(tmp_path):
    assert config_from_env({}) == {}
    assert McpClients.from_env(environ={}) is None
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"fs": {"command": ["/bin/echo"]}}}), encoding="utf-8")
    configs = config_from_env({MCP_CONFIG_ENV: str(path)})
    assert set(configs) == {"fs"}


# ---------------------------------------------------------------- 握手与发现


def test_handshake_records_version_and_tools(server):
    instance = server()
    assert instance.protocol_version == "2025-06-18"
    assert instance.server_info["name"] == "fake"
    assert {tool.name for tool in instance.tools} == {"echo", "add"}
    echo = next(tool for tool in instance.tools if tool.name == "echo")
    assert echo.description == "回显一段文本"
    assert echo.input_schema["required"] == ["message"]
    assert instance.is_alive


def test_stdio_protocol_is_utf8_under_non_utf8_process_default(server):
    instance = server(env={"PYTHONIOENCODING": "cp1252"})
    echo = next(tool for tool in instance.tools if tool.name == "echo")
    assert echo.description == "回显一段文本"
    result = instance.call_tool("echo", {"message": "中文与 café"})
    assert result.text_parts == ("echo:中文与 café",)


def test_tools_list_pagination_is_followed(server):
    instance = server("--page-size", "1")
    assert {tool.name for tool in instance.tools} == {"echo", "add"}


def test_unknown_protocol_version_is_refused_not_downgraded():
    instance = StdioServer(_config("fake", "--protocol-version", "2099-01-01"))
    with pytest.raises(McpServerUnavailable, match="不认识的协议版本"):
        instance.start()
    assert instance.tools == ()
    assert not instance.is_alive


def test_malformed_json_marks_server_failed():
    instance = StdioServer(_config("fake", "--mode", "bad_json"))
    with pytest.raises(McpServerUnavailable, match="畸形 JSON"):
        instance.start()
    assert not instance.is_alive


def test_oversized_single_message_is_refused():
    instance = StdioServer(_config("fake", "--mode", "huge_message"))
    with pytest.raises(McpServerUnavailable, match="单条消息"):
        instance.start()
    assert not instance.is_alive


def test_start_twice_is_refused(server):
    instance = server()
    with pytest.raises(McpError, match="已经启动过"):
        instance.start()


# ---------------------------------------------------------------- 调用


def test_call_tool_returns_text_and_keeps_unsupported_content(server):
    instance = server()
    result = instance.call_tool("echo", {"message": "hi"}, call_id="call_1")
    assert result.text_parts == ("echo:hi",)
    assert result.unsupported_types == ("image",)
    assert result.is_error is False
    assert result.duration_ms >= 0


def test_call_tool_is_error_is_reported_not_raised(server):
    instance = server("--mode", "is_error")
    result = instance.call_tool("echo", {"message": "x"})
    assert result.is_error is True
    assert result.text_parts == ("工具说它失败了",)


def test_protocol_rejection_error_code_is_marked(server):
    instance = server("--mode", "jsonrpc_error")
    with pytest.raises(McpCallError) as caught:
        instance.call_tool("echo", {"message": "x"})
    assert caught.value.code == -32602
    assert caught.value.protocol_rejection is True


def test_internal_error_code_is_not_a_protocol_rejection(server):
    instance = server("--mode", "internal_error")
    with pytest.raises(McpCallError) as caught:
        instance.call_tool("echo", {"message": "x"})
    assert caught.value.code == -32603
    assert caught.value.protocol_rejection is False


def test_timeout_then_cancel_notification_reaches_the_server(tmp_path):
    cancel_log = tmp_path / "cancel.jsonl"
    instance = StdioServer(
        _config("fake", "--mode", "hang_on_call", "--cancel-log", str(cancel_log)),
    )
    instance.start()
    started = time.monotonic()
    with pytest.raises(McpTimeout) as caught:
        instance.call_tool("echo", {"message": "x"}, timeout_ms=200)
    assert time.monotonic() - started >= 0.15
    request_id = caught.value.request_id
    assert request_id > 0
    # 超时由客户端**自动**发取消通知（`cancel()` 只是手动入口的备份）。
    time.sleep(0.1)
    instance.close()
    assert _read_jsonl(cancel_log) == [{"requestId": request_id}]


def test_crash_during_call_raises_closed(server):
    instance = server("--mode", "crash_on_call")
    with pytest.raises(McpClosed):
        instance.call_tool("echo", {"message": "x"}, timeout_ms=5_000)
    assert not instance.is_alive


def test_call_timeout_must_be_positive(server):
    instance = server()
    with pytest.raises(McpError, match="正整数"):
        instance.call_tool("echo", {"message": "x"}, timeout_ms=0)


def test_call_meta_carries_call_id(tmp_path):
    call_log = tmp_path / "calls.jsonl"
    instance = StdioServer(_config("fake", "--call-log", str(call_log)))
    instance.start()
    instance.call_tool("add", {"a": 1, "b": 2}, call_id="call_abc")
    instance.close()
    rows = _read_jsonl(call_log)
    assert rows and rows[0]["_meta"] == {"tianshu/call_id": "call_abc"}


# ---------------------------------------------------------------- 环境与 stderr


def test_child_env_only_carries_path_and_declared_keys():
    config = McpServerConfig(
        name="fake", command=(sys.executable, str(FIXTURE)), env={"DECLARED": "1"}
    )
    environ = {"PATH": "/usr/bin", "HOME": "/home/x", "TIANSHU_SECRET": "s3cr3t"}
    instance = StdioServer(config, environ=environ)
    child = instance.child_env()
    assert child["DECLARED"] == "1"
    assert child["PATH"] == "/usr/bin"
    assert "HOME" not in child
    assert "TIANSHU_SECRET" not in child


def test_secret_env_is_not_visible_to_the_server(tmp_path):
    call_log = tmp_path / "calls.jsonl"
    instance = StdioServer(
        _config("fake", "--env-probe", "TIANSHU_SECRET", "--call-log", str(call_log)),
        environ={"PATH": "/usr/bin", "TIANSHU_SECRET": "s3cr3t"},
    )
    instance.start()
    assert instance.server_info["env_probe"] == "<unset>"
    instance.close()


def test_stderr_is_captured_but_bounded(server):
    instance = server("--stderr-bytes", "50000")
    time.sleep(0.1)
    tail = instance.stderr_tail()
    assert tail
    assert len(tail.encode("utf-8")) <= MCP_STDERR_TAIL_BYTES
    assert instance.is_alive


def test_close_is_idempotent_and_reaps_the_process():
    instance = StdioServer(_config("fake"))
    instance.start()
    assert instance.exit_code() is None
    instance.close()
    instance.close()
    assert instance.exit_code() is not None


# ---------------------------------------------------------------- 多 server


def test_clients_start_all_isolates_one_bad_server():
    configs = {
        "good": _config("good"),
        "bad": _config("bad", "--protocol-version", "2099-01-01"),
    }
    clients = McpClients(configs)
    try:
        clients.start_all()
        states = {state.name: state for state in clients.states()}
        assert states["good"].status == "ready"
        assert states["bad"].status == "unavailable"
        assert "不认识的协议版本" in states["bad"].diagnostics[0]
        assert clients.ready_names() == ("good",)
        assert clients.server("bad") is None
        assert clients.diagnostics()
    finally:
        clients.close_all()


def test_clients_close_all_stops_servers():
    clients = McpClients({"one": _config("one")})
    clients.start_all()
    server = clients.server("one")
    assert server is not None and server.is_alive
    clients.close_all()
    assert server.exit_code() is not None
    assert clients.ready_names() == ()


def test_missing_binary_is_reported_not_raised_at_construction():
    config = McpServerConfig(
        name="missing", command=(str(Path("/nonexistent/definitely-not-a-binary")),)
    )
    clients = McpClients({"missing": config})
    try:
        clients.start_all()
        state = clients.states()[0]
        assert state.status == "unavailable"
        assert state.diagnostics
    finally:
        clients.close_all()

# ---------------------------------------------------------------- 复审补的边界


def test_max_tools_is_enforced_at_discovery():
    instance = StdioServer(_config("fake", max_tools=1))
    instance.start()
    try:
        assert [tool.name for tool in instance.tools] == ["echo"]
        assert instance.tools_truncated is True
    finally:
        instance.close()


def test_max_result_bytes_truncates_and_reports_bytes():
    instance = StdioServer(_config("fake", "--mode", "big_output", max_result_bytes=100))
    instance.start()
    try:
        result = instance.call_tool("echo", {"message": "x"})
        assert result.truncated is True
        assert result.bytes == 300_000
        assert result.dropped_bytes == 300_000 - 100
        assert len(result.text_parts[0].encode("utf-8")) == 100
    finally:
        instance.close()


def test_max_result_bytes_counts_utf8_bytes_not_characters():
    instance = StdioServer(_config("fake", max_result_bytes=12))
    instance.start()
    try:
        # 假 server 回 "echo:" + 文本（"中" 3 字节）再跟一个 image 块。
        # 12 字节预算只装得下 "echo:中中"（11 字节），image 块被丢弃并按个数记。
        result = instance.call_tool("echo", {"message": "中" * 10})
        assert result.bytes == len(("echo:" + "中" * 10).encode("utf-8")) == 35
        assert result.truncated is True
        assert result.text_parts[0] == "echo:中中"
        assert result.dropped_bytes == 24
        assert result.dropped_blocks == 1
    finally:
        instance.close()


def test_runtime_protocol_failure_reaps_the_process():
    instance = StdioServer(_config("fake", "--mode", "bad_json_at_runtime"))
    instance.start()
    with pytest.raises(McpClosed, match="畸形 JSON"):
        instance.call_tool("echo", {"message": "x"}, timeout_ms=5_000)
    deadline = time.monotonic() + 3
    while instance.exit_code() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert instance.exit_code() is not None, "协议失败后进程必须被收掉，不能只是标失效"
    instance.close()


def test_late_response_after_timeout_does_not_poison_the_next_call():
    instance = StdioServer(_config("fake", "--mode", "late_on_call"))
    instance.start()
    try:
        with pytest.raises(McpTimeout):
            instance.call_tool("echo", {"message": "late"}, timeout_ms=100)
        time.sleep(0.4)  # 让晚到的响应进队列
        result = instance.call_tool("add", {"a": 1, "b": 2}, timeout_ms=5_000)
        assert result.text_parts == ("3",)
        assert instance.is_alive is True
    finally:
        instance.close()


def test_relative_config_env_path_is_refused():
    with pytest.raises(McpConfigError, match="绝对路径"):
        config_from_env({MCP_CONFIG_ENV: "mcp.json"})


def test_structured_content_counts_against_the_result_budget():
    instance = StdioServer(
        _config("fake", "--mode", "structured_output", max_result_bytes=200)
    )
    instance.start()
    try:
        result = instance.call_tool("echo", {"message": "x"})
        # 文本 "ok" 占 2 字节；structuredContent 的紧凑 JSON 远大于剩余预算 →
        # 被丢弃并计入 dropped_bytes（不是"只统计文本"）。
        assert result.text_parts == ("ok",)
        assert result.structured is None
        assert result.truncated is True
        assert result.bytes > 200
        assert result.dropped_bytes == result.bytes - 2
    finally:
        instance.close()


def test_structured_content_within_budget_is_kept():
    instance = StdioServer(_config("fake", "--mode", "structured_output"))
    instance.start()
    try:
        result = instance.call_tool("echo", {"message": "x"})
        assert result.structured == {"rows": ["x" * 100] * 50}
        assert result.truncated is False
        assert result.bytes > 0
    finally:
        instance.close()


def test_abandoned_timeout_ids_are_bounded():
    instance = StdioServer(_config("fake"))
    try:
        # 不必真起 server：`_abandon` 在连接不可用时只记 id，发不出取消也不改变结论。
        for request_id in range(200):
            instance._abandon(request_id, "tools/call")
        assert instance.pending_timeouts <= 64
        assert 0 not in instance._abandoned, "最老的超时 id 必须被淘汰（FIFO）"
        assert 199 in instance._abandoned
        assert 0 not in instance._abandoned, "最老的超时 id 必须被淘汰（FIFO）"
        assert 199 in instance._abandoned
    finally:
        instance.close()


def test_exact_max_tools_without_more_pages_is_not_truncation():
    """恰好装满 max_tools 且没有 nextCursor：server 就只有这么多，不算截断。"""
    instance = StdioServer(_config("fake", max_tools=2))
    instance.start()
    try:
        assert len(instance.tools) == 2
        assert instance.tools_truncated is False
    finally:
        instance.close()


def test_paged_server_hitting_max_tools_is_truncation(tmp_path):
    tools_file = tmp_path / "tools.json"
    tools_file.write_text(
        json.dumps(
            [
                {
                    "name": f"t{i}",
                    "description": "",
                    "inputSchema": {"type": "object", "properties": {}},
                }
                for i in range(3)
            ]
        ),
        encoding="utf-8",
    )
    instance = StdioServer(
        _config("fake", "--page-size", "1", "--tools-file", str(tools_file), max_tools=2)
    )
    instance.start()
    try:
        assert len(instance.tools) == 2
        assert instance.tools_truncated is True, "server 还有第三个工具，必须报截断"
    finally:
        instance.close()
