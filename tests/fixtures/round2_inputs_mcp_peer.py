"""Synthetic stdio peer for malformed-result admission regression tests."""
import json
import sys
from pathlib import Path

result_path, call_log = map(Path, sys.argv[1:])
for line in sys.stdin.buffer:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic-peer", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "noop", "description": "synthetic no-op",
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        }]}
    elif method == "tools/call":
        with call_log.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"id": request["id"], "method": method}) + "\n")
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        raise RuntimeError(f"unexpected method: {method}")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
