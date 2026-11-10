"""离线演示：不需要 API key，把 M5 的三进程闭环跑起来。

它证明的不是"界面好看"，而是三件事：

1. `关闭 UI 后任务继续`：Daemon 的驱动线程不依赖任何客户端；
2. 审批是真的卡点：删文件必须人点"允许"，点之前文件一个字节不动；
3. `重启后恢复`由 `tianshu host` 的监护与 Daemon 的启动扫描负责（本脚本不断言，
   断言在 `tests/test_integration_m5.py` 里，用真 `kill -9`）。

模型是脚本替身（`DemoModel`），不会联网、不花钱，也不代表真实模型能力。

用法：

    uv run python scripts/demo_m5.py --data-root /tmp/m5-demo
    uv run python scripts/demo_m5.py --data-root /tmp/m5-demo --auto   # 无人值守跑一遍

`--auto` 会：建任务 → 等审批卡片出现 → **断开所有客户端**（等价关掉 UI）→
用新连接批准 → 等 Run 走到终态 → 打印事件摘要 → 关 Host 与 Daemon。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tianshu import daemon, ipc  # noqa: E402
from tianshu.capability import CapabilityProfile  # noqa: E402
from tianshu.model import ModelIdentity, ModelResponse, ToolCall  # noqa: E402


class DemoModel:
    """第一轮提议删掉 `旧账单.txt`（需要审批），之后收尾。"""

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="demo", vendor=None, model="demo")

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="demo",
            model="demo",
            supports_tool_calling=True,
            supports_parallel_tool_calls=False,
            supports_streaming=False,
            declared_by="scripts/demo_m5.py（脚本替身）",
        )

    def complete(self, messages, tools):  # noqa: ARG002
        first = not any(
            message.get("role") == "tool" for message in messages if isinstance(message, dict)
        )
        if first:
            return ModelResponse(
                finish_reason="tool_use",
                tool_calls=[ToolCall("c1", "delete_file", {"path": "旧账单.txt"})],
            )
        return ModelResponse(finish_reason="end_turn", content="已清理旧账单。")


def _seed_workspace(root: Path) -> Path:
    ws = root / "demo-workspace"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    (ws / "旧账单.txt").write_bytes("2025 年 3 月的旧账单（演示用，可删除）\n".encode())
    (ws / "本月账单.txt").write_bytes("2025 年 11 月的账单（演示用，不要动）\n".encode())
    return ws


def _status(client: ipc.Client, run_id: str) -> dict:
    return client.call("run.status", {"run_id": run_id})["run"]


def _wait(client: ipc.Client, run_id: str, wanted: str, *, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _status(client, run_id)
        if last.get("state") == wanted:
            return last
        time.sleep(0.1)
    raise SystemExit(f"等待 {wanted} 超时：{last}")


def _wait_terminal(client: ipc.Client, run_id: str, *, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _status(client, run_id)
        if last.get("state") in ("succeeded", "failed", "cancelled"):
            return last
        time.sleep(0.1)
    raise SystemExit(f"等待终态超时：{last}")


def _create_task(client: ipc.Client, ws: Path) -> str:
    result = client.call(
        "task.create",
        {
            "goal": "把工作区里的旧账单删掉，保留本月账单",
            "workspaces": [{"root_path": str(ws), "mode": "writable"}],
        },
    )
    return result["task"]["task_id"]


def _start(client: ipc.Client, task_id: str) -> str:
    return client.call("run.start", {"task_id": task_id, "out_name": "summary.md"})["run"][
        "run_id"
    ]


def _run_auto(root: Path, ws: Path, url_holder: dict[str, str]) -> int:
    client = ipc.Client(root, timeout=10)
    client.connect()
    task_id = _create_task(client, ws)
    run_id = _start(client, task_id)
    run = _wait(client, run_id, "waiting_approval")
    print(f"Run 停在审批：{run['run_id']}（这期间文件不能动）", flush=True)
    approvals = client.call("approvals.list", {"run_id": run_id})["approvals"]
    if not approvals:
        raise SystemExit("没有拿到审批卡片")
    card = approvals[0]
    print(
        f"审批卡片：动作={card['action']} 目标={card['resources']} 原因={card['reason']}",
        flush=True,
    )
    print(f"文件还在吗：{'旧账单.txt' in [p.name for p in ws.iterdir()]}", flush=True)

    # —— 关掉 UI：这里把唯一的客户端断开，之后全部用新连接 ——
    client.close()
    print("已断开所有客户端（等价于关掉 UI）", flush=True)
    time.sleep(0.5)

    decider = ipc.Client(root, timeout=10)
    decider.connect()
    decider.call("approvals.decide", {"approval_id": card["approval_id"], "decision": "approve"})
    print("已批准，等待任务自己跑完（没有 UI 参与）", flush=True)
    run = _wait_terminal(decider, run_id)
    events = decider.call("events.run", {"run_id": run_id})["events"]
    print(
        json.dumps(
            {
                "final_state": run["state"],
                "outcome": run["outcome"],
                "effect_state": run["effect_state"],
                "files": sorted(p.name for p in ws.iterdir()),
                "event_types": [event["type"] for event in events],
                "url": url_holder.get("url", ""),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    decider.close()
    if run["state"] != "succeeded":
        return 1
    if (ws / "旧账单.txt").exists():
        print("失败：被批准的删除没有生效", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="天枢 Agent M5 离线演示（脚本模型，不花钱）")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--auto", action="store_true", help="无人值守跑一遍并退出")
    parser.add_argument("--open", action="store_true", help="打开系统浏览器")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    root = args.data_root or Path(tempfile.mkdtemp(prefix="m5-demo-", dir="/tmp"))
    root.mkdir(parents=True, exist_ok=True)
    ws = _seed_workspace(root)

    stop = threading.Event()
    ready = threading.Event()

    def run_daemon() -> None:
        daemon.serve(root, model=DemoModel(), on_ready=ready.set, stop_event=stop)

    thread = threading.Thread(target=run_daemon, name="demo-daemon", daemon=True)
    thread.start()
    if not ready.wait(20):
        print("Daemon 没有在 20s 内就绪", file=sys.stderr)
        return 1

    ui_dir = REPO / "ui" / "dist"
    url_holder: dict[str, str] = {}
    from tianshu.host import (  # noqa: E402
        BridgeConfig,
        UiBridge,
        acquire_host_info,
        clear_host_info,
        write_host_info,
    )

    info = acquire_host_info(root, ui_dir=ui_dir)
    bridge = UiBridge(BridgeConfig(data_root=root, ui_dir=ui_dir, token=info.token, port=args.port))
    info.port = bridge.port
    write_host_info(root, info)
    bridge.serve_background()
    url_holder["url"] = bridge.url
    print(f"界面地址：{bridge.url}", flush=True)
    print(f"演示工作区：{ws}", flush=True)
    if args.open:
        import webbrowser

        webbrowser.open_new_tab(bridge.url)

    try:
        if args.auto:
            return _run_auto(root, ws, url_holder)
        client = ipc.Client(root, timeout=10)
        client.connect()
        task_id = _create_task(client, ws)
        run_id = _start(client, task_id)
        _wait(client, run_id, "waiting_approval")
        client.close()
        print("任务已创建并在等你审批：界面右侧应出现「删除 旧账单.txt」卡片。", flush=True)
        print("按 Ctrl-C 结束演示（关掉浏览器不影响任务推进）", flush=True)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("正在关闭…", flush=True)
        return 0
    finally:
        bridge.stop()
        clear_host_info(root, pid=info.pid)
        try:
            daemon.request_shutdown(root, reason="demo", timeout=5)
        except (ipc.IpcError, OSError):
            stop.set()
        thread.join(5)


if __name__ == "__main__":
    raise SystemExit(main())