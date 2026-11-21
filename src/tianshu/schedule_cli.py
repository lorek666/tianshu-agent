"""`tianshu schedule` 子命令：定时任务的管理面 CLI（W5）。

仿 `data_cli.py` 先例：独立模块 `add_parser` + `run`，在 `cli.py` 两处接线
（注册 + 分发）。桌面端控制面亦通过 `schedule.*` RPC 共用同一套 `Scheduler`。

时间和目录轮询只在 daemon 驱动线程里运行。`signal` 通过本机认证连接向
运行中的 daemon 提交外部事件 ID。CLI 管理命令仍走本地数据库。

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
        help="任务触发：固定间隔、日历、目录新增文件或本机事件",
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
    triggers = create.add_mutually_exclusive_group(required=True)
    triggers.add_argument(
        "--interval-seconds", type=int, metavar="N",
        help="触发间隔（秒，>= 1）；到点补发一次并从当前时间重排",
    )
    triggers.add_argument("--at", metavar="HH:MM", help="按当地时钟的指定时刻触发")
    triggers.add_argument("--watch-directory", metavar="ABS_PATH", help="监听只读工作区中的目录")
    triggers.add_argument("--on-event", action="store_true", help="只响应已认证的本机事件")
    create.add_argument("--timezone", help="日历时区：UTC、固定偏移或 IANA 名称")
    create.add_argument("--weekdays", help="日历触发星期，逗号分隔（周一 1；默认每天）")
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
    create.add_argument(
        "--desktop",
        action="store_true",
        default=argparse.SUPPRESS,
        help="使用桌面端默认数据目录",
    )
    create.add_argument("--json", action="store_true")

    signal = commands.add_parser("signal", help="向运行中的本机 daemon 提交去重事件")
    signal.add_argument("schedule_id", metavar="SCHEDULE_ID")
    signal.add_argument("event_id", metavar="EVENT_ID")
    signal.add_argument("--data-root", type=Path, help="daemon 数据目录；默认使用桌面端目录")
    signal.add_argument("--json", action="store_true")

    webhook = commands.add_parser("webhook-serve", help="启动绑定一条本机事件日程的 webhook 接收器")
    webhook.add_argument("schedule_id")
    webhook.add_argument("--port", type=int, default=8793, help="仅监听 127.0.0.1；0 为自动选择")
    webhook.add_argument("--data-root", type=Path, help="daemon 数据目录")
    webhook.add_argument("--secret-env", required=True, help="签名密钥的环境变量名")

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
        cmd.add_argument(
            "--desktop",
            action="store_true",
            default=argparse.SUPPRESS,
            help="使用桌面端默认数据目录",
        )
        cmd.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    if args.schedule_action == "webhook-serve":
        from . import ipc
        from .schedule_webhook import serve

        return serve(args.data_root or ipc.default_data_root(), args.schedule_id,
                     port=args.port, secret_env=args.secret_env)
    if args.schedule_action == "signal":
        from . import ipc

        with ipc.Client(args.data_root or ipc.default_data_root()) as client:
            result = client.call("schedule.signal", {
                "schedule_id": args.schedule_id, "event_id": args.event_id,
            })
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            label = "已入队" if result["accepted"] else "事件已接收过"
            print(f"{label}：{result['run_id']}")
        return 0
    from .restore_access import default_material_paths, preflight_legacy_material_layout

    db = getattr(args, "db", None) or Path(".tianshu/events.db")
    data_root = db.parent
    preflight_legacy_material_layout(db, data_root)
    log = EventLog(db, blob_dir=default_material_paths(db, data_root)[1])
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
    trigger = None
    if args.at is not None:
        try:
            days = (
                [int(day) for day in args.weekdays.split(",")]
                if args.weekdays is not None else list(range(1, 8))
            )
        except ValueError as exc:
            raise TianshuError("--weekdays 需要逗号分隔的星期整数") from exc
        trigger = {
            "kind": "calendar", "time": args.at,
            "timezone": args.timezone if args.timezone is not None else "UTC", "weekdays": days,
        }
    elif args.timezone is not None or args.weekdays is not None:
        raise TianshuError("--timezone / --weekdays 仅适用于 --at")
    elif args.watch_directory is not None:
        trigger = {"kind": "file_arrival", "directory": args.watch_directory}
    elif args.on_event:
        trigger = {"kind": "event"}
    row = scheduler.create(
        goal, workspaces,
        interval_seconds=args.interval_seconds,
        trigger=trigger,
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
        trigger = row["trigger"]
        next_label = "下次触发"
        if trigger["kind"] == "interval":
            label = f"每 {row['interval_seconds']}s"
        elif trigger["kind"] == "calendar":
            days = ",".join(str(day) for day in trigger["weekdays"])
            label = f"星期 {days} {trigger['time']}（{trigger['timezone']}）"
        elif trigger["kind"] == "file_arrival":
            label = f"目录新增文件：{trigger['directory']}"
            next_label = "下次检查"
        else:
            label = "等待本机事件"
        next_fire = row["next_fire_at"]
        next_text = f"  {next_label} {next_fire}" if next_fire is not None else ""
        print(f"{row['schedule_id']}  [{state}]  {label}{next_text}")
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
