"""假 MCP server（stdio）：测试用的**独立**对端。

刻意不 import `tianshu.mcp`：客户端的行为要拿一个**不是它自己**的进程来验，
否则"两边一起写错"会互相掩盖。它只按 MCP 的 JSON-RPC 形状说话：

- 换行分隔的单条 JSON（每行一条消息）；
- `initialize` → 回 `protocolVersion` / `capabilities` / `serverInfo`；
- `notifications/initialized` → 不回；
- `tools/list`（`cursor` 分页）→ 回 `tools` / `nextCursor`；
- `tools/call` → 按 `--mode` 产出正常结果、`isError`、JSON-RPC 错误、超时或崩溃；
- `notifications/cancelled` → 记一行（如果给了 `--cancel-log`）。

它也是"副作用发生了"的证据：`--call-log` 每收到一次 `tools/call` 就追加一行，
用它可以断言"取消后一条调用都没发出去"这类外部可观察事实。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_TOOLS = [
    {
        "name": "echo",
        "description": "回显一段文本",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add",
        "description": "两个整数相加",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
]


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id: int, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: int, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _append_line(path: str | None, payload: dict) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    # MCP stdio 是 UTF-8；Windows 重定向的标准流默认可能是 cp1252。
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol-version", default="2025-06-18")
    parser.add_argument(
        "--mode",
        default="normal",
        choices=[
            "normal",
            "no_handshake",
            "bad_json",
            "crash_on_call",
            "hang_on_call",
            "big_output",
            "huge_message",
            "is_error",
            "jsonrpc_error",
            "internal_error",
            "late_on_call",
            "bad_json_at_runtime",
            "structured_output",
        ],
    )
    parser.add_argument("--tools-file", default="")
    parser.add_argument("--page-size", type=int, default=0)
    parser.add_argument("--call-log", default="")
    # 工具**真的执行了**的证据（在算完结果、写响应之前追加）：call-log 只证明
    # server 收到了 tools/call，进程在"响应已回、结果未落"处崩掉时，这一行才
    # 能证明副作用真的发生过。
    parser.add_argument("--effect-log", default="")
    parser.add_argument("--cancel-log", default="")
    parser.add_argument("--stderr-bytes", type=int, default=0)
    parser.add_argument("--env-probe", default="")
    parser.add_argument("--server-name", default="fake")
    args = parser.parse_args(argv)

    tools = DEFAULT_TOOLS
    if args.tools_file:
        with open(args.tools_file, encoding="utf-8") as handle:
            tools = json.load(handle)

    if args.stderr_bytes:
        sys.stderr.write("x" * args.stderr_bytes)
        sys.stderr.flush()

    if args.mode == "bad_json":
        sys.stdout.write("{not json}\n")
        sys.stdout.flush()

    if args.mode == "huge_message":
        # 一行超过客户端的单条消息上限（1 MiB）。
        sys.stdout.write('{"jsonrpc":"2.0","id":9999,"result":{"pad":"' + "y" * 1_100_000 + '"}}\n')
        sys.stdout.flush()

    server_info = {"name": args.server_name, "version": "0.0.1"}
    if args.env_probe:
        server_info["env_probe"] = os.environ.get(args.env_probe, "<unset>")

    first_call = True
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            if args.mode == "no_handshake":
                continue
            _result(
                request_id,
                {
                    "protocolVersion": args.protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": server_info,
                },
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            if args.page_size and args.page_size > 0:
                start = int(params.get("cursor") or 0)
                page = tools[start : start + args.page_size]
                result: dict = {"tools": page}
                if start + args.page_size < len(tools):
                    result["nextCursor"] = str(start + args.page_size)
                _result(request_id, result)
            else:
                _result(request_id, {"tools": tools})
            continue

        if method == "tools/call":
            name = params.get("name")
            call_arguments = params.get("arguments") or {}
            _append_line(
                args.call_log,
                {"name": name, "arguments": call_arguments, "_meta": params.get("_meta")},
            )
            if args.mode == "bad_json_at_runtime":
                sys.stdout.write("{not json}\n")
                sys.stdout.flush()
                continue
            if args.mode == "late_on_call":
                if first_call:
                    first_call = False
                    time.sleep(0.3)
            else:
                first_call = False
            if args.mode == "crash_on_call":
                os._exit(3)
            if args.mode == "hang_on_call":
                continue
            if args.mode == "structured_output":
                _result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {"rows": ["x" * 100] * 50},
                    },
                )
                continue
            if args.mode == "is_error":
                _append_line(args.effect_log, {"name": name, "executed": True})
                _result(
                    request_id,
                    {"content": [{"type": "text", "text": "工具说它失败了"}], "isError": True},
                )
                continue
            if args.mode == "jsonrpc_error":
                _error(request_id, -32602, "invalid params（假 server 的协议层拒绝）")
                continue
            if args.mode == "internal_error":
                _error(request_id, -32603, "internal error（假 server 的内部失败）")
                continue
            if args.mode == "big_output":
                _result(
                    request_id,
                    {"content": [{"type": "text", "text": "z" * 300_000}]},
                )
                continue
            if name == "echo":
                text = str(call_arguments.get("message", ""))
                content = [
                    {"type": "text", "text": f"echo:{text}"},
                    {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                ]
            elif name == "add":
                try:
                    total = int(call_arguments.get("a", 0)) + int(call_arguments.get("b", 0))
                except (TypeError, ValueError):
                    total = 0
                content = [{"type": "text", "text": str(total)}]
            else:
                content = [{"type": "text", "text": f"unknown tool {name}"}]
            _append_line(args.effect_log, {"name": name, "executed": True})
            _result(request_id, {"content": content})
            continue

        if method == "notifications/cancelled":
            _append_line(args.cancel_log, {"requestId": params.get("requestId")})
            continue

        if request_id is not None:
            _error(request_id, -32601, f"method not found: {method}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
