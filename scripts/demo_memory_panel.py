"""记忆面板演示（G2 记忆阶段 M1）：真 Daemon + 真 `ui/dist` + 真 Host 桥。

用途有两个，都不是"界面好看"：

1. **人眼验收**：打开页面点右上角「记忆」，能看到系统记住了什么、凭什么
   （六项判据）、还算不算数，并能确认 / 忘记 / 删除；
2. **截图证据**：`docs/m1-memory-panel.png` 就是从这个进程拍下来的（1280×900，
   headless Chrome 真连接，不是静态图）。

种子数据全部经由**控制面人路径**写入（`memory_control.remember` → dispatcher →
intent/result + 一次性审批 + 执行侧二次核验），不是直接改投影。模型是空脚本
（`ScriptedModel([])`），不联网、不花钱，也不代表真实模型能力。

用法：

    uv run python scripts/demo_memory_panel.py --data-root /tmp/m1-memory
    # 打印「界面地址：http://127.0.0.1:<port>/」后用浏览器打开，点右上角「记忆」

收尾（另开一个终端）：

    uv run python -c "from pathlib import Path; from tianshu import daemon; \
      print(daemon.request_shutdown(Path('/tmp/m1-memory'), reason='done', timeout=5))"
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tianshu import daemon  # noqa: E402
from tianshu.events import EventLog  # noqa: E402
from tianshu.loop import build_stack  # noqa: E402
from tianshu.memory_control import remember  # noqa: E402
from tianshu.model import ScriptedModel  # noqa: E402
from tianshu.resources import MemoryScope  # noqa: E402


def _seed(root: Path) -> list[str]:
    """用控制面人路径写几条记忆（与 CLI / UI 同一个入口）。"""
    log = EventLog(root / "events.db")
    try:
        stack = build_stack(log, data_root=root)
        seeds = [
            (
                MemoryScope.user(),
                "preference",
                "以后写周报都用中文，先给结论，再列细节。",
            ),
            (
                MemoryScope.user(),
                "fact",
                "我的项目叫天枢，代码在 /Volumes/Date/天枢Agent。",
            ),
            (
                MemoryScope("task", "task_demo"),
                "decision",
                "这个任务的导出格式定了 CSV，不做打印。",
            ),
        ]
        ids: list[str] = []
        for scope, kind, content in seeds:
            result = remember(stack, scope, kind=kind, content=content)
            memory_id = (result.output or {}).get("memory_id")
            ids.append(str(memory_id))
            print(f"已写入记忆：{scope.to_uri()} {kind} -> {memory_id}", flush=True)
        return ids
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记忆面板演示（脚本模型，不花钱）")
    parser.add_argument("--data-root", type=Path, default=Path("/tmp/m1-memory-demo"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args(argv)

    root = args.data_root
    root.mkdir(parents=True, exist_ok=True)
    if not args.no_seed:
        _seed(root)

    stop = threading.Event()
    ready = threading.Event()

    def run_daemon() -> None:
        daemon.serve(root, model=ScriptedModel([]), on_ready=ready.set, stop_event=stop)

    thread = threading.Thread(target=run_daemon, name="demo-daemon", daemon=True)
    thread.start()
    if not ready.wait(20):
        print("Daemon 没有在 20s 内就绪", file=sys.stderr)
        return 1

    ui_dir = REPO / "ui" / "dist"
    from tianshu.host import (  # noqa: E402
        BridgeConfig,
        UiBridge,
        acquire_host_info,
        write_host_info,
    )

    info = acquire_host_info(root, ui_dir=ui_dir)
    bridge = UiBridge(BridgeConfig(data_root=root, ui_dir=ui_dir, token=info.token, port=args.port))
    info.port = bridge.port
    write_host_info(root, info)
    bridge.serve_background()
    print(f"界面地址：{bridge.url}", flush=True)
    print("点右上角「记忆」打开面板；Ctrl-C 或 request_shutdown 收尾。", flush=True)

    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
