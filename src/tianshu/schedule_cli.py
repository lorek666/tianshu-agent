"""`tianshu schedule` 子命令：定时任务的管理面 CLI（W5）。

仿 `data_cli.py` 先例：独立模块 `add_parser` + `run`，在 `cli.py` 两处接线
（注册 + 分发）。v1 只有 CLI——不加 RPC 方法（那要动 M5-desktop.md 契约表），
UI 可观测性留未决。

**只有管理面，没有触发面**：触发（tick）只发生在 daemon 驱动线程里
（`daemon._driver_loop` 装配 Scheduler 后每个节拍调用）。CLI 在 daemon
运行期间也可以增删改查——WAL 下短写事务与控制面写审批是同一性质。

exec 策略语义与 `cli.py task create` 同口径（`_exec_policy` 就地复刻并注明
来源）：`--allow-exec-cmd` 必须是可执行文件绝对路径列表（布尔拒绝），
`--require-isolation` 必须伴随 `--allow-exec-cmd`；不传 = 任务无 exec 策略
（exec 默认全拒）。这是任务级声明，不是调度器自己解释的字符串规则；触发后
Run 内的每次 exec 仍走执行侧判定与审批。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .control import EXEC_DEFAULT_MAX_OUTPUT_BYTES, EXEC_DEFAULT_TIMEOUT_MS
from .errors import TianshuError
from .events import EventLog
from .execpolicy import ISOLATION_BOUNDARIES, ExecPolicy
from .scheduler import Scheduler


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "schedule",
        help="定时任务：到点由 daemon 自动创建任务并入队 Run"
             "（interval 调度；v1 无 cron / 无 RPC / 无 UI）",
    )
    commands = parser.add_subparsers(dest="schedule_action", required=True)

    create = commands.add_parser("create", help="创建一条定时任务")
    create.add_argument("--goal", required=True, help="到点创建的任务目标")
    create.add_argument(
        "--workspace", type=Path, default=None, action="append",
        help="可写绑定目录（可重复）；不传 = 纯对话任务",
    )
    create.add_argument(
        "--readonly", type=Path, default=None, action="append",
        help="只读绑定目录（可重复）",
    )
    create.add_argument(
        "--interval-seconds", type=int, required=True, metavar="N",
        help="触发间隔（秒，>= 1）；到点补发一次并从当前时间重排",
    )
    create.add_argument(
        "--allow-exec-cmd", action="append", default=None, metavar="ABS_PATH",
        help="任务级 exec 白名单：允许的可执行文件绝对路径（可重复）。"
        "不传 = exec 默认全拒",
    )
    create.add_argument(
        "--require-isolation", action="append", default=None, metavar="BOUNDARY",
        help="要求实测证明的隔离边界（需同时给 --allow-exec-cmd；可重复）",
    )
    create.add_argument("--db", type=Path, default=None, help=argparse.SUPPRESS)
    create.add_argument("--json", action="store_true")

    for action, help_text, with_id in (
        ("list", "列出全部定时任务", False),
        ("show", "查看一条定时任务", True),
        ("enable", "启用一条定时任务", True),
        ("disable", "停用一条定时任务（保留配置，不删除）", True),
        ("remove", "删除一条定时任务", True),
    ):
        cmd = commands.add_parser(action, help=help_text)
        if with_id:
            cmd.add_argument("schedule_id", metavar="SCHEDULE_ID")
        cmd.add_argument("--db", type=Path, default=None, help=argparse.SUPPRESS)
        cmd.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    db = getattr(args, "db", None) or Path(".tianshu/events.db")
    log = EventLog(db, blob_dir=db.parent / "blobs")
    try:
        scheduler = Scheduler(log)
        action = args.schedule_action
        if action == "create":
            return _create(scheduler, args)
        if action == "list":
            return _print_rows(scheduler.list(), args)
        if action == "show":
            return _show(scheduler, args)
        if action == "enable":
            return _print_one(scheduler.set_enabled(args.schedule_id, True), args)
        if action == "disable":
            return _print_one(scheduler.set_enabled(args.schedule_id, False), args)
        if action == "remove":
            scheduler.remove(args.schedule_id)
            if not args.json:
                print(f"已删除：{args.schedule_id}")
            return 0
        print(f"错误：未知的 schedule 动作：{action}", file=sys.stderr)
        return 2
    except TianshuError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # ExecPolicy 构造期的策略校验（如相对路径 argv）抛 ValueError；
        # 与 cli.py task create 同样打成可读错误，不抛栈。
        print(f"错误：exec 策略不合法：{exc}", file=sys.stderr)
        return 2
    finally:
        log.close()


def _create(scheduler: Scheduler, args: argparse.Namespace) -> int:
    goal = args.goal or ""
    workspaces: list[dict[str, Any]] = []
    for path in args.workspace or []:
        workspaces.append({"root_path": str(path), "mode": "writable"})
    for path in args.readonly or []:
        workspaces.append({"root_path": str(path), "mode": "readonly"})
    exec_policy = _exec_policy(args, workspaces)
    row = scheduler.create(
        goal, workspaces,
        interval_seconds=args.interval_seconds,
        exec_policy=exec_policy,
    )
    return _print_one(row, args)


def _exec_policy(
    args: argparse.Namespace, workspaces: list[dict[str, Any]]
) -> ExecPolicy | None:
    """与 cli.py `_exec_policy` 同口径（task create 的同一套默认值）。

    参数是**可执行文件绝对路径列表**；布尔值进不来（argparse append 只收
    字符串），但没有白名单却声明隔离要求同样是"缺参数"，这里照搬拒绝。
    """
    cmds = args.allow_exec_cmd
    required = tuple(args.require_isolation or ())
    for boundary in required:
        if boundary not in ISOLATION_BOUNDARIES:
            raise TianshuError(
                f"未知隔离边界：{boundary!r}（可选：{list(ISOLATION_BOUNDARIES)}）"
            )
    if not cmds:
        if required:
            raise TianshuError(
                "--require-isolation 需要同时给 --allow-exec-cmd："
                "没有执行策略的隔离要求是空话"
            )
        return None
    roots = [w["root_path"] for w in workspaces if w["mode"] == "writable"]
    return ExecPolicy(
        argv_allowlist=tuple(cmds),
        cwd_roots=tuple(roots),
        env_allowlist=("PATH",),
        max_output_bytes=EXEC_DEFAULT_MAX_OUTPUT_BYTES,
        timeout_ms=EXEC_DEFAULT_TIMEOUT_MS,
        isolation_requires=required,
    )


def _show(scheduler: Scheduler, args: argparse.Namespace) -> int:
    row = scheduler.get(args.schedule_id)
    if row is None:
        print(f"错误：没有这个 schedule：{args.schedule_id}", file=sys.stderr)
        return 2
    return _print_one(row, args)


def _print_one(row: dict[str, Any], args: argparse.Namespace) -> int:
    return _print_rows([row], args)


def _print_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("没有定时任务（tianshu schedule create 创建）")
        return 0
    for row in rows:
        state = "启用" if row["enabled"] else "停用"
        print(
            f"{row['schedule_id']}  [{state}]  每 {row['interval_seconds']}s"
            f"  下次 {row['next_fire_at']}"
        )
        print(f"    goal: {row['goal']}")
        for binding in row["workspaces"]:
            print(
                f"    workspace: {binding['root_path']}"
                f"（{binding['mode']}）"
            )
        policy = row["exec_policy"]
        if policy:
            print(f"    exec: {', '.join(policy['argv_allowlist'])}")
        if row["last_fired_at"] is not None:
            print(f"    上次触发: {row['last_fired_at']}")
        if row["last_error"]:
            print(f"    上次失败: {row['last_error']}")
    return 0
