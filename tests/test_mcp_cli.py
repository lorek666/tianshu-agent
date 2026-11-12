"""`tianshu mcp list` 与 `--allow-mcp-server` 的 CLI 判据（c14 + c8 的入口侧）。

全部用真子进程假 server（`tests/fixtures/fake_mcp_server.py`）；CLI 通过
`TIANSHU_MCP_CONFIG` 读进程级配置，所以这里也是在验"配置入口只有一个"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tianshu.cli import main
from tianshu.mcp import MCP_CONFIG_ENV

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _write_config(tmp_path: Path, *args: str) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": {
                    "fake": {"command": [sys.executable, str(FIXTURE), *args]}
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_mcp_list_reports_unconfigured_without_starting_anything(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(MCP_CONFIG_ENV, raising=False)
    code, out, _err = _run(capsys, ["mcp", "list", "--json"])
    assert code == 0
    payload = json.loads(out)
    assert payload["configured"] is False
    assert payload["servers"] == []


def test_mcp_list_reports_servers_tools_and_protocol(tmp_path, capsys, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setenv(MCP_CONFIG_ENV, str(config))
    code, out, _err = _run(
        capsys, ["mcp", "list", "--json", "--data-root", str(tmp_path / "data")]
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["configured"] is True
    assert [row["name"] for row in payload["servers"]] == ["fake"]
    server = payload["servers"][0]
    assert server["status"] == "ready"
    assert server["protocol_version"] == "2025-06-18"
    assert set(server["tools"]) == {"echo", "add"}
    ids = {row["tool_id"] for row in payload["tools"]}
    assert {"mcp__fake__echo", "mcp__fake__add"} <= ids
    echo = next(row for row in payload["tools"] if row["tool_id"] == "mcp__fake__echo")
    assert echo["uri"] == "mcp://fake/echo"
    assert echo["tool_version"].startswith("mcp1:")
    # 假 server 自己的 schema 已显式 `additionalProperties: false`，所以没有收窄；
    # "缺省→补 false 并记 narrowed" 的判据在 tests/test_mcp_registry.py。
    assert echo["narrowed"] == []
    assert payload["rejected_tools"] == []


def test_mcp_list_surfaces_rejected_tools_with_reason(tmp_path, capsys, monkeypatch):
    """schema 用了方言外关键字 → 工具不准入，但**不能被静默丢掉**。"""
    tools_file = tmp_path / "tools.json"
    tools_file.write_text(
        json.dumps(
            [
                {
                    "name": "good",
                    "description": "",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "bad",
                    "description": "",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"x": {"$ref": "#/defs/x"}},
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    config = _write_config(tmp_path, "--tools-file", str(tools_file))
    monkeypatch.setenv(MCP_CONFIG_ENV, str(config))
    code, out, _err = _run(
        capsys, ["mcp", "list", "--json", "--data-root", str(tmp_path / "data")]
    )
    assert code == 0
    payload = json.loads(out)
    assert [row["tool"] for row in payload["tools"]] == ["good"]
    rejected = [(row["tool"], "$ref" in row["reason"]) for row in payload["rejected_tools"]]
    assert rejected == [("bad", True)]


def test_task_create_records_mcp_server_allowlist(tmp_path, capsys):
    db = tmp_path / "events.db"
    code, out, _err = _run(
        capsys,
        [
            "task",
            "create",
            "--goal",
            "x",
            "--allow-mcp-server",
            "fake",
            "--json",
            "--db",
            str(db),
            "--data-root",
            str(tmp_path / "data"),
        ],
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["task"]["mcp_servers"] == ["fake"]


def test_task_create_rejects_bad_server_name(tmp_path, capsys):
    db = tmp_path / "events.db"
    code, _out, err = _run(
        capsys,
        [
            "task",
            "create",
            "--goal",
            "x",
            "--allow-mcp-server",
            "Bad Name",
            "--db",
            str(db),
            "--data-root",
            str(tmp_path / "data"),
        ],
    )
    assert code == 2
    assert "server 名不合法" in err


def test_mcp_config_relative_path_is_refused_by_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(MCP_CONFIG_ENV, "mcp.json")
    code, _out, err = _run(capsys, ["mcp", "list", "--json"])
    assert code == 2
    assert "绝对路径" in err