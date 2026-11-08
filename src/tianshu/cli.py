"""命令行入口。

    python -m tianshu run    --workspace DIR --task TEXT --out FILE
    python -m tianshu replay --run-id ID

设计原则：CLI 只做参数解析与输出格式化，逻辑全在库函数里——这样测试可以直接调库，
不必 spawn 子进程。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .events import EventLog
from .loop import Limits, run
from .model import OpenAICompatibleModel, ScriptedModel
from .replay import replay

DEFAULT_DB = Path(".tianshu/events.db")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tianshu", description="天枢 Agent (M0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="执行一次任务")
    r.add_argument("--workspace", required=True, type=Path)
    r.add_argument("--task", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    r.add_argument("--model-name", default="gpt-4o-mini")
    r.add_argument("--base-url", default=None)
    r.add_argument("--max-turns", type=int, default=8)
    r.add_argument("--max-total-tokens", type=int, default=20_000)
    r.add_argument("--db", type=Path, default=DEFAULT_DB)
    r.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")

    v = sub.add_parser("replay", help="从事件日志回放并校验")
    v.add_argument("--run-id", required=True)
    v.add_argument("--db", type=Path, default=DEFAULT_DB)
    v.add_argument("--json", action="store_true")

    return p


def _cmd_run(args: argparse.Namespace) -> int:
    log = EventLog(args.db, blob_dir=args.db.parent / "blobs")

    if args.model == "scripted":
        # 无 key 时的兜底：脚本化模型只做一次 list_files 加一次写报告，
        # 用来验证链路是否通，不代表模型能力。
        from .model import ModelResponse, ToolCall

        model = ScriptedModel(
            [
                ModelResponse(
                    finish_reason="tool_use",
                    tool_calls=[ToolCall("c1", "list_files", {"path": "."})],
                ),
                ModelResponse(
                    finish_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            "c2",
                            "create_report",
                            {
                                "name": args.out,
                                "content": "# 摘要\n\n（scripted 模式占位，未调用真实模型）\n",
                            },
                        )
                    ],
                ),
                ModelResponse(finish_reason="end_turn", content="完成"),
            ]
        )
    else:
        model = OpenAICompatibleModel(args.model_name, base_url=args.base_url)

    result = run(
        task=args.task,
        workspace_dir=args.workspace,
        out_name=args.out,
        model=model,
        log=log,
        limits=Limits(args.max_turns, args.max_total_tokens),
    )

    report = replay(log, result.run_id)

    if args.json:
        print(json.dumps(
            {"run": result.__dict__, "replay": report.__dict__},
            ensure_ascii=False, indent=2, default=str))
    else:
        print(f"run_id : {result.run_id}")
        print(f"outcome: {result.outcome}  ({result.error or 'ok'})")
        print(f"turns  : {result.turns}   tokens: {result.tokens}")
        if result.artifact:
            print(f"report : {result.artifact['path']}  "
                  f"({result.artifact['bytes']}B)")
        print(f"replay : {'通过' if report.ok else '失败'}"
              f"  ({report.events} 事件 / {report.messages_verified} 次消息校验)")
        for p in report.problems:
            print(f"  ! {p}")

    log.close()
    return 0 if result.outcome in {"succeeded", "noop"} and report.ok else 1


def _cmd_replay(args: argparse.Namespace) -> int:
    log = EventLog(args.db, blob_dir=args.db.parent / "blobs")
    try:
        report = replay(log, args.run_id)
    finally:
        log.close()

    if args.json:
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    else:
        print(f"run_id : {report.run_id}")
        print(f"事件数 : {report.events}")
        print(f"轮数   : {report.turns}")
        print(f"校验   : {report.messages_verified} 次 messages_hash")
        print(f"产物   : {report.artifact_sha256 or '（无）'}")
        print(f"结论   : {'通过' if report.ok else '失败'}")
        for p in report.problems:
            print(f"  ! {p}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "run":
            return _cmd_run(args)
        return _cmd_replay(args)
    except Exception as exc:  # CLI 边界：打印可读错误，不抛栈
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
