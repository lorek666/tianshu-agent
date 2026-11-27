"""CLI 运维与生命周期子命令（内部模块，对外入口见 `tianshu.cli`）。

承载 `mcp` / `worker` / `recover` / `undo` / `approvals` / `daemon` / `host`
及权威存储装配 `_authority_stack`。

注意：子进程验收测试（如 `test_low_disk_entrypoints` / `test_m2_acceptance` /
`test_mcp_crash`）会 monkeypatch `cli._scripted_model`，因此 `_cmd_worker`
在运行期经 `cli._model_from(args)` 动态取模型，不在导入期按值绑定。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .credentials import redact
from .events import EventLog
from .loop import Limits, build_stack
from .permissions import HumanIdentity
from .recovery import recover_run
from .undo import UndoRefused, apply_undo, plan_undo


def add_mcp_parser(sub: argparse._SubParsersAction) -> None:
    m = sub.add_parser(
        "mcp",
        help="MCP server 与工具准入（只读）：读 TIANSHU_MCP_CONFIG 起一次、列出"
        "server 状态与准入/被拒工具，然后关掉",
    )
    m.add_argument("action", choices=["list"])
    m.add_argument("--json", action="store_true", help="输出 JSON")
    m.add_argument("--data-root", type=Path, default=None)


def add_worker_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    w = sub.add_parser(
        "worker",
        help="作业循环（有界批次 drain）：认领排队中的 Run 并推进到它停下为止；"
        "队空或撞上被占用的工作区就退出，不是常驻守护进程",
    )
    w.add_argument("--once", action="store_true", help="只跑一个作业就退出")
    w.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="一次最多跑几个作业；不指定时按 64 个一批直到队空",
    )
    w.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    w.add_argument("--provider", default=None, choices=list(cli.MODEL_PROVIDERS),
                   help="真实模型走哪个线上协议（默认取 TIANSHU_PROVIDER，再默认 openai）")
    w.add_argument("--model-name", default=None,
                   help="模型 id；openai 默认 gpt-4o-mini，anthropic 必须显式给")
    w.add_argument("--base-url", default=None)
    w.add_argument("--max-turns", type=int, default=8)
    w.add_argument("--max-total-tokens", type=int, default=20_000)
    w.add_argument("--data-root", type=Path, default=None)
    w.add_argument("--db", type=Path, default=cli.DEFAULT_DB)
    w.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                   help="使用桌面端默认数据目录")
    w.add_argument("--json", action="store_true")


def add_recover_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    rc = sub.add_parser("recover", help="核对并补全一次被打断的 run")
    rc.add_argument("--run-id", required=True)
    rc.add_argument("--workspace", required=True, type=Path)
    rc.add_argument("--data-root", type=Path, default=None,
                    help="authority 数据根（快照清单在这里；默认与 --db 同目录）")
    rc.add_argument("--db", type=Path, default=cli.DEFAULT_DB)
    rc.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                    help="使用桌面端默认数据目录")
    rc.add_argument("--json", action="store_true")


def add_undo_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    u = sub.add_parser("undo", help="撤销某次 run 的文件变更（需 human 批准）")
    u.add_argument("--run-id", required=True)
    u.add_argument(
        "--workspace", type=Path, default=None,
        help="可选：只撤销这个目录下的变更（落盘目录由快照证据自己的 root 决定；"
             "旧格式记录没有 root，会被拒绝而不是猜一个目录）",
    )
    u.add_argument("--db", type=Path, default=cli.DEFAULT_DB)
    u.add_argument("--data-root", type=Path, default=None,
                   help="authority 数据根（默认与 --db 同目录）")
    u.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                   help="使用桌面端默认数据目录")
    u.add_argument("--approve", action="store_true",
                   help="以本地人工身份批准本次撤销的具体动作并执行")
    u.add_argument("--json", action="store_true")


def add_approvals_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    ap = sub.add_parser(
        "approvals", aliases=["approval"], help="审批队列：列出/批准/否决/撤销"
    )
    ap.add_argument("action", choices=["list", "approve", "reject", "revoke"])
    ap.add_argument("approval_id", nargs="?")
    ap.add_argument("--run-id", default=None, help="只看某个 Run 的待批请求")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=cli.DEFAULT_DB)
    ap.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                    help="使用桌面端默认数据目录")
    ap.add_argument("--json", action="store_true")


def add_daemon_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    d = sub.add_parser(
        "daemon",
        help="常驻守护进程：长驻、驱动任务、崩溃恢复，并通过本地 IPC 对外提供控制面；"
        "不是一次性 drain（那是 worker）",
    )
    d.add_argument("--data-root", type=Path, default=None,
                   help="运行数据根（默认平台标准目录，可用 TIANSHU_DATA_ROOT 覆盖）")
    d.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    d.add_argument("--provider", default=None, choices=list(cli.MODEL_PROVIDERS))
    d.add_argument("--model-name", default=None)
    d.add_argument("--base-url", default=None)
    d.add_argument("--max-turns", type=int, default=8)
    d.add_argument("--max-total-tokens", type=int, default=20_000)


def add_host_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    h = sub.add_parser(
        "host",
        help="桌面外壳：单实例 + 监护 daemon + 把 UI 的 HTTP/SSE 桥到本地 IPC",
    )
    h.add_argument("action", nargs="?", default="run", choices=["run", "stop", "status"])
    h.add_argument("--data-root", type=Path, default=None,
                   help="运行数据根（默认平台标准目录，可用 TIANSHU_DATA_ROOT 覆盖）")
    h.add_argument("--port", type=int, default=0, help="UI 端口；0 = 随机（推荐）")
    h.add_argument("--ui-dir", type=Path, default=None,
                   help="静态资源目录；默认仓库内 ui/dist")
    h.add_argument("--open", action="store_true", help="启动后打开系统浏览器")
    h.add_argument("--no-supervise", action="store_true",
                   help="不监护 daemon 崩溃重启（调试用）")
    h.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    h.add_argument("--provider", default=None, choices=list(cli.MODEL_PROVIDERS))
    h.add_argument("--model-name", default=None)
    h.add_argument("--base-url", default=None)
    h.add_argument("--max-turns", type=int, default=8)
    h.add_argument("--max-total-tokens", type=int, default=20_000)
    h.add_argument(
        "--release-version",
        default=None,
        help="打包桌面壳传入的发行版本；与另两个 release 参数必须同时提供",
    )
    h.add_argument(
        "--release-commit",
        default=None,
        help="打包桌面壳传入的 40 位源码提交",
    )
    h.add_argument(
        "--release-target",
        default=None,
        help="打包桌面壳传入的四目标之一",
    )
    h.add_argument("--json", action="store_true")


def _mcp_config():
    """进程级 MCP 配置（`TIANSHU_MCP_CONFIG`）；没配置 → 空。

    只有**会执行工具**的入口（run / worker / daemon 驱动栈 / mcp list）才读它；
    控制面只读命令不启动外部进程。
    """
    from .mcp import config_from_env

    return config_from_env()


def _authority_stack(
    db: Path, data_root: Path | None = None, *, mcp: bool = False
):
    """把 M2 的权威存储装配起来。

    CLI 只做装配与格式化，判定全在库里——也包括"用了哪些 store"这件事：
    测试和 CLI 走同一个 `build_stack`，否则"CLI 能用但库函数不行"这类
    绕过点会一直存在。
    """
    from .restore_access import default_material_paths, preflight_legacy_material_layout

    resolved_root = data_root if data_root is not None else db.parent
    preflight_legacy_material_layout(db, resolved_root)
    _, ev_blobs, _ = default_material_paths(db, resolved_root)
    log = EventLog(db, blob_dir=ev_blobs, data_root=resolved_root)
    configs = _mcp_config() if mcp else {}
    return build_stack(
        log,
        data_root=resolved_root,
        mcp_servers=configs,
    )


def _cmd_mcp(args: argparse.Namespace) -> int:
    """`tianshu mcp list`：只读地起一次、列状态与准入结论、关掉。

    为什么值得一条命令：MCP 的"没做"有三种——server 没有这个工具、schema 不准入、
    被 `max_tools` 截断。只给模型一个 boolean 或静默丢弃，用户永远查不出区别。
    """
    from .mcp import McpClients
    from .tool_registry import build_registry

    configs = _mcp_config()
    if not configs:
        payload = {
            "configured": False,
            "reason": "没有配置 MCP server（设置 TIANSHU_MCP_CONFIG 指向 JSON 配置）",
            "servers": [],
            "tools": [],
            "rejected_tools": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["reason"])
        return 0
    clients = McpClients(configs)
    try:
        clients.start_all()
        registry = build_registry(clients)
        servers = []
        for state in clients.states():
            servers.append(
                {
                    "name": state.name,
                    "status": state.status,
                    "protocol_version": state.protocol_version,
                    "tools": [tool.name for tool in state.tools],
                    "tools_truncated": state.tools_truncated,
                    "diagnostics": list(state.diagnostics),
                    "pid": state.pid,
                }
            )
        tools = []
        for tool_id in registry.ids():
            binding = registry.mcp_binding(tool_id)
            if binding is None:
                continue
            tools.append(
                {
                    "tool_id": tool_id,
                    "uri": binding.uri,
                    "server": binding.server,
                    "tool": binding.tool,
                    "tool_version": binding.tool_version,
                    "schema_hash": binding.schema_hash,
                    "narrowed": list(binding.narrowed),
                }
            )
        payload = {
            "configured": True,
            "servers": servers,
            "tools": tools,
            "rejected_tools": registry.rejected_tools(),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for row in servers:
                state = "可用" if row["status"] == "ready" else "不可用"
                print(f"{row['name']}  {state}  protocol={row['protocol_version'] or '-'}"
                      f"  工具 {len(row['tools'])}")
                for line in row["diagnostics"]:
                    print(f"  ! {line}")
            print(f"准入工具：{len(tools)}")
            for row in tools:
                narrowed = f"  收窄={row['narrowed']}" if row["narrowed"] else ""
                print(f"  {row['tool_id']}  ← {row['uri']}  {row['tool_version']}{narrowed}")
            if payload["rejected_tools"]:
                print(f"未准入：{len(payload['rejected_tools'])}")
                for row in payload["rejected_tools"]:
                    print(f"  {row['server']}/{row['tool']}  {row['reason']}")
        return 0
    finally:
        clients.close_all()


def _cmd_worker(args: argparse.Namespace) -> int:
    """作业循环入口。

    与 `run start/resume` 走**同一条**执行路径（`Worker` → `AgentLoop.resume_run`），
    区别只在于"谁来认领"：前台是命令行自己建 Run 再跑，worker 是从队里领活。
    它不会自己写文件，也不重新判定策略——那些都在派发层。
    """
    from . import cli
    from .worker import Worker

    # worker 会真的执行工具 → 必须启动 MCP（没有配置时就是内置工具面）。
    stack = _authority_stack(args.db, args.data_root, mcp=True)
    try:
        worker = Worker(
            stack,
            model=cli._model_from(args),
            limits=Limits(args.max_turns, args.max_total_tokens),
        )
        with worker:
            if args.once:
                ran = [worker.run_ids[-1]] if worker.run_once() else []
                idle = not ran
            else:
                # 未指定 --max-jobs 时，按 Worker 的单轮预算分批跑到队空。
                # 显式指定时保留“一次最多 N 个”的边界，便于脚本控制批次。
                ran = []
                seen: set[str] = set()
                while True:
                    batch = (
                        worker.drain()
                        if args.max_jobs is None
                        else worker.drain(max_jobs=args.max_jobs)
                    )
                    for run_id in batch:
                        if run_id not in seen:
                            seen.add(run_id)
                            ran.append(run_id)
                    if args.max_jobs is not None or worker.exhausted or worker.contended:
                        break
                # 只有“跑空”才算 idle：显式跑满 --max-jobs 或撞上被占用的工作区
                # 都说明队里可能还有活，报 idle 是骗人。
                idle = worker.exhausted
        payload = {
            "run_ids": ran,
            "idle": idle,
            "beats": worker.beats,
            "errors": worker.errors,
            "notes": worker.notes,
            "contended": worker.contended,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if not ran:
                print("没有可认领的作业")
            for run_id in ran:
                run = stack.tasks.get_run(run_id)
                print(f"{run_id}  {run.state if run else '?'}"
                      f"  outcome={run.outcome if run else '?'}")
            for message in worker.notes:
                print(f"  · {message}")
            for message in worker.errors:
                print(f"  ! {message}", file=sys.stderr)
        return 1 if worker.errors else 0
    finally:
        stack.log.close()


def _cmd_recover(args: argparse.Namespace) -> int:
    from .restore_access import default_material_paths, preflight_legacy_material_layout

    data_root = args.data_root if args.data_root is not None else args.db.parent
    preflight_legacy_material_layout(args.db, data_root)
    _, ev_blobs, _ = default_material_paths(args.db, data_root)
    log = EventLog(args.db, blob_dir=ev_blobs, data_root=data_root)
    stack = build_stack(log, data_root=data_root)
    snapshots, leases = stack.snapshots, stack.leases
    try:
        # 传 data_root：恢复写阻断事件与派发落盘要抢同一把执行守卫（复审 中-1）。
        report = recover_run(
            log, args.run_id, args.workspace, snapshots,
            leases=leases, data_root=data_root,
            # 把装配好的记忆投影传进去：正文目录可能不是 DB 旁的默认值，
            # 恢复若按默认目录找 blob，擦除结论与孤儿清理都会偏。
            memory=stack.memory,
        )
    finally:
        log.close()

    if args.json:
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    else:
        print(f"run_id : {report.run_id}")
        print(f"已确认执行 : {report.applied or '（无）'}")
        print(f"确认未执行 : {report.none_applied or '（无）'}")
        print(f"无法核实   : {report.unresolved or '（无）'}")
        if report.blocked_objects:
            # 对象阻断与目录阻断是两件事：解除目录阻断不会解除它，所以这里有
            # 单独一行——否则用户对着一个"已 unblock 却还是发不出去"的工具猜。
            print(
                "MCP 对象已阻断 : "
                f"{report.blocked_objects}（核对 server 侧后用 unblock_object 解除）"
            )
        if report.memory_not_applied:
            print(f"记忆未生效 : {report.memory_not_applied}")
        if report.memory_unresolved:
            # 擦除的删文件在提交之外且不可逆：有这一栏时退出码一定是 1，
            # 不打印原因等于让人对着一个失败码猜。
            print(f"记忆待核实 : {report.memory_unresolved}")
        if report.memory_orphan_blobs:
            print(f"已清孤儿正文 : {report.memory_orphan_blobs}")
        for p in report.problems:
            print(f"  ! {p}")
    return 0 if report.ok else 1


def _cmd_approvals(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db, args.data_root)
    try:
        if args.action == "list":
            # 过期的待批项留在队列里只能骗人：批了也不会执行。
            # 清一次再列，队列里剩下的才是真的可以操作的。
            stack.approvals.expire_due()
            rows = stack.approvals.pending(run_id=args.run_id)
            if args.json:
                print(json.dumps([a.to_dict() for a in rows],
                                 ensure_ascii=False, indent=2))
            elif not rows:
                print("没有待批的请求")
            else:
                for a in rows:
                    print(f"{a.approval_id}  {a.action}  {','.join(a.resources)}")
                    print(f"    原因: {a.reason}")
                    print(f"    摘要: {a.request_digest[:16]}…  "
                          f"过期: {a.expires_at}")
            return 0

        if not args.approval_id:
            print("错误：需要 approval_id", file=sys.stderr)
            return 2

        actor = HumanIdentity.local_cli()
        if args.action == "approve":
            approval = stack.approvals.decide(
                args.approval_id, decision="approved", actor=actor
            )
        elif args.action == "reject":
            approval = stack.approvals.decide(
                args.approval_id, decision="rejected", actor=actor
            )
        else:
            approval = stack.dispatcher.revoke(args.approval_id, actor=actor)

        if args.json:
            print(json.dumps(approval.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"{approval.approval_id}: {approval.state}")
        return 0
    finally:
        stack.log.close()


def _cmd_undo(args: argparse.Namespace) -> int:
    stack = None
    try:
        stack = _authority_stack(args.db, args.data_root)
        run_id = args.run_id
        try:
            if args.approve:
                outcome = apply_undo(stack, run_id, scope=args.workspace)
            else:
                plan = plan_undo(stack, run_id, scope=args.workspace)
                print("撤销会做这些事（尚未执行）：")
                for action, desc in plan.describe():
                    print(f"  - {action}: {desc}")
                print("")
                print("这些都属于覆盖/删除类操作，需要人工批准。")
                print("确认无误后加 --approve 重跑同一条命令。")
                return 3
        except UndoRefused as refused:
            # 拒绝的文案与退出码都由 undo 模块给出：UI 也走同一条判定，
            # 不能出现"CLI 说没可撤销、UI 说撤销失败"这种两种说法。
            stream = sys.stdout if refused.code == 1 else sys.stderr
            for line in refused.lines or [str(refused)]:
                print(line, file=stream)
            return refused.code

        except Exception as exc:  # noqa: BLE001 - 撤销中断必须如实报告，不能吞
            print(
                f"撤销中断（可能已部分生效，请用 recover 核查）：{redact(str(exc))}",
                file=sys.stderr,
            )
            return 1

        if args.json:
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "ok": outcome.ok,
                        "restored": outcome.restored,
                        "deleted": outcome.deleted,
                        "conflicts": outcome.conflicts,
                        "problems": outcome.problems,
                        "entries": outcome.entries,
                        "effect": outcome.effect,
                        "control_run": outcome.control_run,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"结论   : {'已撤销' if outcome.ok else '未撤销'}")
            for p in outcome.restored:
                print(f"  还原 : {p}")
            for p in outcome.deleted:
                print(f"  删除 : {p}")
            for c in outcome.conflicts:
                print(f"  冲突 : {c}")
            for problem in outcome.problems:
                print(f"  ! {problem}")
        return 0 if outcome.ok else 1
    finally:
        if stack is not None:
            stack.log.close()


def _cmd_daemon(args: argparse.Namespace) -> int:
    """常驻入口。**只做参数装配**：驱动、恢复、IPC 全在 daemon 模块里。

    进程内不提前装配 Stack：daemon 自己要在 bind 之前完成恢复，两处装配会变成
    "到底哪一份是权威"的问题。
    """
    from . import cli
    from .daemon import serve
    from .ipc import default_data_root
    from .settings_control import OFFLINE_DEMO_MODEL

    data_root = Path(args.data_root or default_data_root())
    serve(
        data_root,
        model=OFFLINE_DEMO_MODEL if args.model == "scripted" else cli._model_from(args),
        limits=Limits(args.max_turns, args.max_total_tokens),
    )
    return 0


def _cmd_host(args: argparse.Namespace) -> int:
    from .host import HostReleaseIdentity, read_host_info, run_host, stop_host
    from .ipc import default_data_root

    data_root = Path(args.data_root or default_data_root())
    if args.action == "stop":
        return stop_host(data_root)
    if args.action == "status":
        info = read_host_info(data_root)
        if args.json:
            payload = None if info is None else {
                "running": True,
                "pid": info.pid,
                "port": info.port,
                "url": f"http://127.0.0.1:{info.port}/",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif info is None:
            print("Host 没有在运行")
        else:
            print(f"Host 在跑：http://127.0.0.1:{info.port}/  (pid={info.pid})")
        return 0 if info is not None else 1

    ui_dir = args.ui_dir
    if ui_dir is None:
        ui_dir = Path(__file__).resolve().parents[2] / "ui" / "dist"
    release_values = (args.release_version, args.release_commit, args.release_target)
    release = None
    if any(value is not None for value in release_values):
        if not all(value is not None for value in release_values):
            print(
                "错误：--release-version、--release-commit、--release-target 必须同时提供",
                file=sys.stderr,
            )
            return 2
        try:
            release = HostReleaseIdentity(*release_values)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
    command = [
        sys.executable, "-m", "tianshu", "daemon",
        "--data-root", str(data_root),
        "--model", args.model,
        "--max-turns", str(args.max_turns),
        "--max-total-tokens", str(args.max_total_tokens),
    ]
    for flag, value in (
        ("--provider", args.provider),
        ("--model-name", args.model_name),
        ("--base-url", args.base_url),
    ):
        if value:
            command += [flag, value]
    return run_host(
        data_root,
        ui_dir=ui_dir,
        daemon_command=command,
        release=release,
        port=args.port,
        open_browser=args.open,
        supervise=not args.no_supervise,
    )
