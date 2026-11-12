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
    instance.cancel(request_id, reason="test")
    time.sleep(0.05)
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