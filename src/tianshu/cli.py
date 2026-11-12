"""命令行入口。

    python -m tianshu run    --workspace DIR --task TEXT --out FILE   # 兼容入口
    python -m tianshu task   create --goal TEXT --workspace DIR
    python -m tianshu run    start --task-id ID --out FILE
    python -m tianshu run    status|pause|resume|cancel --run-id ID
    python -m tianshu approvals list|approve|reject|revoke
    python -m tianshu undo   --run-id ID [--workspace DIR] [--approve]
    python -m tianshu recover --run-id ID --workspace DIR
    python -m tianshu replay --run-id ID
    python -m tianshu evaluate [--case N1] [--repeat 3] [--model scripted]

设计原则：CLI 只做参数解析与输出格式化，逻辑全在库函数里——这样测试可以直接调库，
不必 spawn 子进程。**不要在解析层做任何权限判断**：判断只有一处（Dispatcher），
多一处在 CLI 就多一条"绕过 CLI 就绕过判断"的路。

退出码：0 有结论且成功 / 1 失败或取消 / 2 前置条件错误 / 3 需要人决定
（等审批、已暂停、待核查）/ 4 评测判 unsafe（安全门槛一票否决）。脚本可以据此
区分"没事了"和"等你"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC
from pathlib import Path

from . import memory_control
from .credentials import redact
from .errors import TianshuError
from .events import EventLog
from .execpolicy import ISOLATION_BOUNDARIES, ExecPolicy
from .loop import AgentLoop, Limits, build_stack
from .memory import MEMORY_KINDS
from .memory_control import memory_view
from .model import AnthropicMessagesModel, Model, OpenAICompatibleModel, ScriptedModel
from .permissions import HumanIdentity
from .pricing import format_cost
from .recovery import recover_run
from .replay import replay
from .resources import MEMORY_SCOPES, MemoryScope, build_memory_scope
from .tasks import (
    RUN_FENCED,
    RUN_RECONCILING,
    RUN_WAITING_APPROVAL,
    TERMINAL_STATES,
)
from .undo import UndoRefused, apply_undo, plan_undo

DEFAULT_DB = Path(".tianshu/events.db")

# 线上协议名。加第三个协议时只改这里 + `_model_from`，不动主循环。
MODEL_PROVIDERS = ("openai", "anthropic")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tianshu", description="天枢 Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "run",
        help="跑一次任务；或用子命令控制已有 Run（start/status/pause/resume/cancel）",
    )
    r.add_argument(
        "action", nargs="?", default=None,
        choices=["start", "status", "pause", "resume", "cancel"],
        help="省略时按兼容入口处理：--task/--workspace 建 Task 后立刻 start",
    )
    r.add_argument("--task-id", default=None,
                   help="对已存在的 Task 开一次执行（start）")
    r.add_argument("--run-id", default=None, help="status/pause/resume/cancel 的目标")
    r.add_argument("--workspace", type=Path, default=None, action="append",
                   help="可重复；兼容入口与 task create 用")
    r.add_argument("--readonly", type=Path, default=None, action="append",
                   help="只读绑定目录（不能写入）")
    r.add_argument("--task", default=None, help="兼容入口的目标文本")
    r.add_argument("--out", default=None, help="兼容入口的输出文件名")
    r.add_argument(
        "--allow-exec-cmd",
        action="append",
        default=None,
        metavar="ABS_PATH",
        help="显式开启 run_command：允许的可执行文件绝对路径（可重复）。"
        "不传 = exec 默认全拒；这是显式动作，不是默认白名单",
    )
    r.add_argument(
        "--allow-outbound",
        action="append",
        default=None,
        metavar="ORIGIN",
        help="显式声明任务级外发白名单：允许出网的**源**（可重复），"
        "例如 network://https/example.com 或 network://http/127.0.0.1:8080。"
        "不传 = 这个任务一条包都不发；声明了源也仍然要逐次人工批准",
    )
    r.add_argument(
        "--allow-mcp-server",
        action="append",
        default=None,
        metavar="NAME",
        help="显式声明任务级 MCP server 白名单（可重复）：这个任务准用哪些已配置的 "
        "server。不传 = 一个都不许；声明了也仍然要逐次人工批准（c4.mcp_call）",
    )
    r.add_argument(
        "--require-isolation",
        action="append",
        default=None,
        choices=list(ISOLATION_BOUNDARIES),
        metavar="BOUNDARY",
        help="要求实测证明的隔离边界（可重复）：files_authority / network / "
        "credentials / process_tree。未给 = 不声明隔离（执行如实标「受信任执行」）；"
        "给了但后端没有 enforced 实测证据 → unsupported",
    )
    r.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    r.add_argument("--provider", default=None, choices=list(MODEL_PROVIDERS),
                   help="真实模型走哪个线上协议（默认取 TIANSHU_PROVIDER，再默认 openai）")
    r.add_argument("--model-name", default=None,
                   help="模型 id；openai 默认 gpt-4o-mini，anthropic 必须显式给")
    r.add_argument("--base-url", default=None)
    r.add_argument("--max-turns", type=int, default=8)
    r.add_argument("--max-total-tokens", type=int, default=20_000)
    r.add_argument("--data-root", type=Path, default=None,
                   help="authority 数据根（默认与 --db 同目录）")
    r.add_argument("--db", type=Path, default=DEFAULT_DB)
    r.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")

    m = sub.add_parser(
        "mcp",
        help="MCP server 与工具准入（只读）：读 TIANSHU_MCP_CONFIG 起一次、列出"
        "server 状态与准入/被拒工具，然后关掉",
    )
    m.add_argument("action", choices=["list"])
    m.add_argument("--json", action="store_true", help="输出 JSON")
    m.add_argument("--data-root", type=Path, default=None)

    t = sub.add_parser("task", help="任务：创建 / 查看 / 改目标或绑定")
    t.add_argument("action", choices=["create", "show", "revise"])
    t.add_argument("--task-id", default=None)
    t.add_argument("--goal", default=None)
    t.add_argument("--workspace", type=Path, default=None, action="append")
    t.add_argument("--readonly", type=Path, default=None, action="append")
    t.add_argument("--data-root", type=Path, default=None)
    t.add_argument("--db", type=Path, default=DEFAULT_DB)
    t.add_argument("--json", action="store_true")
    t.add_argument(
        "--allow-exec-cmd",
        action="append",
        default=None,
        metavar="ABS_PATH",
        help="显式开启 run_command：允许的可执行文件绝对路径（可重复）。"
        "不传 = exec 默认全拒；这是显式动作，不是默认白名单",
    )
    t.add_argument(
        "--allow-outbound",
        action="append",
        default=None,
        metavar="ORIGIN",
        help="显式声明任务级外发白名单：允许出网的**源**（可重复）。"
        "不传 = 一条包都不发；声明后仍要逐次人工批准",
    )
    t.add_argument(
        "--allow-mcp-server",
        action="append",
        default=None,
        metavar="NAME",
        help="显式声明任务级 MCP server 白名单（可重复）。不传 = 一个都不许；"
        "声明后仍要逐次人工批准",
    )
    t.add_argument(
        "--max-subtasks",
        type=int,
        default=0,
        metavar="N",
        help="显式声明任务级委派能力：最多可派生 N 个子任务（0–4）。"
        "不传 = 0 = 不能委派；这是能力声明，不是可选项。"
        "声明后每次 spawn 仍要人工批准",
    )

    w = sub.add_parser(
        "worker",
        help="作业循环（一次性 drain）：认领排队中的 Run 并推进到它停下为止；"
        "队空或撞上被占用的工作区就退出，不是常驻守护进程",
    )
    w.add_argument("--once", action="store_true", help="只跑一个作业就退出")
    w.add_argument("--max-jobs", type=int, default=64, help="一次最多跑几个作业")
    w.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    w.add_argument("--provider", default=None, choices=list(MODEL_PROVIDERS),
                   help="真实模型走哪个线上协议（默认取 TIANSHU_PROVIDER，再默认 openai）")
    w.add_argument("--model-name", default=None,
                   help="模型 id；openai 默认 gpt-4o-mini，anthropic 必须显式给")
    w.add_argument("--base-url", default=None)
    w.add_argument("--max-turns", type=int, default=8)
    w.add_argument("--max-total-tokens", type=int, default=20_000)
    w.add_argument("--data-root", type=Path, default=None)
    w.add_argument("--db", type=Path, default=DEFAULT_DB)
    w.add_argument("--json", action="store_true")

    rc = sub.add_parser("recover", help="核对并补全一次被打断的 run")
    rc.add_argument("--run-id", required=True)
    rc.add_argument("--workspace", required=True, type=Path)
    rc.add_argument("--data-root", type=Path, default=None,
                    help="authority 数据根（快照清单在这里；默认与 --db 同目录）")
    rc.add_argument("--db", type=Path, default=DEFAULT_DB)
    rc.add_argument("--json", action="store_true")

    u = sub.add_parser("undo", help="撤销某次 run 的文件变更（需 human 批准）")
    u.add_argument("--run-id", required=True)
    u.add_argument(
        "--workspace", type=Path, default=None,
        help="可选：只撤销这个目录下的变更（落盘目录由快照证据自己的 root 决定；"
             "旧格式记录没有 root，会被拒绝而不是猜一个目录）",
    )
    u.add_argument("--db", type=Path, default=DEFAULT_DB)
    u.add_argument("--data-root", type=Path, default=None,
                   help="authority 数据根（默认与 --db 同目录）")
    u.add_argument("--approve", action="store_true",
                   help="以本地人工身份批准本次撤销的具体动作并执行")
    u.add_argument("--json", action="store_true")

    ap = sub.add_parser(
        "approvals", aliases=["approval"], help="审批队列：列出/批准/否决/撤销"
    )
    ap.add_argument("action", choices=["list", "approve", "reject", "revoke"])
    ap.add_argument("approval_id", nargs="?")
    ap.add_argument("--run-id", default=None, help="只看某个 Run 的待批请求")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")

    mem = sub.add_parser(
        "memory",
        aliases=["memories"],
        help="记忆控制面：Agent 记住了什么、依据是什么；确认候选 / 忘记一条",
    )
    mem.add_argument(
        "action",
        choices=[
            "list",
            "search",
            "get",
            "confirm",
            "forget",
            "remember",
            "edit",
            "promote",
            "erase",
            "rebuild-vectors",
        ],
    )
    mem.add_argument(
        "value", nargs="?",
        help="search 的查询词；get/confirm/forget 的 memory_id",
    )
    mem.add_argument(
        "--scope", default=None, choices=sorted(MEMORY_SCOPES),
        help="把某个作用域加入可见集合（默认只装备 user 作用域）",
    )
    mem.add_argument(
        "--scope-id", default=None,
        help="与 --scope 一起使用；workspace 作用域必须给绝对路径",
    )
    mem.add_argument(
        "--workspace", type=Path, default=None, action="append",
        help="把该目录的 workspace 作用域加入可见集合（可重复）",
    )
    mem.add_argument(
        "--task-id", default=None, help="把该 task 作用域加入可见集合"
    )
    mem.add_argument("--status", default=None,
                     choices=["candidate", "active", "superseded", "forgotten"])
    mem.add_argument("--limit", type=int, default=50)
    mem.add_argument(
        "--kind", default="preference",
        choices=sorted(MEMORY_KINDS),
        help="remember 的记忆种类（默认 preference）",
    )
    mem.add_argument(
        "--expires-at", type=int, default=None,
        help="remember 的过期时间（毫秒时间戳，可选）",
    )
    mem.add_argument(
        "--content", default=None, help="edit 的新版正文"
    )
    mem.add_argument(
        "--to-scope", default=None, choices=sorted(MEMORY_SCOPES),
        help="promote 的目标作用域",
    )
    mem.add_argument(
        "--to-scope-id", default=None,
        help="promote 的目标 scope_id（workspace 必须绝对路径；task 默认用 --task-id）",
    )
    mem.add_argument("--data-root", type=Path, default=None)
    mem.add_argument("--db", type=Path, default=DEFAULT_DB)
    mem.add_argument("--json", action="store_true")

    v = sub.add_parser("replay", help="从事件日志回放并校验")
    v.add_argument("--run-id", required=True)
    v.add_argument("--db", type=Path, default=DEFAULT_DB)
    v.add_argument("--json", action="store_true")

    d = sub.add_parser(
        "daemon",
        help="常驻守护进程：长驻、驱动任务、崩溃恢复，并通过本地 IPC 对外提供控制面；"
        "不是一次性 drain（那是 worker）",
    )
    d.add_argument("--data-root", type=Path, default=None,
                   help="运行数据根（默认平台标准目录，可用 TIANSHU_DATA_ROOT 覆盖）")
    d.add_argument("--model", default="scripted",
                   choices=["scripted", "openai-compatible"])
    d.add_argument("--provider", default=None, choices=list(MODEL_PROVIDERS))
    d.add_argument("--model-name", default=None)
    d.add_argument("--base-url", default=None)
    d.add_argument("--max-turns", type=int, default=8)
    d.add_argument("--max-total-tokens", type=int, default=20_000)

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
    h.add_argument("--provider", default=None, choices=list(MODEL_PROVIDERS))
    h.add_argument("--model-name", default=None)
    h.add_argument("--base-url", default=None)
    h.add_argument("--max-turns", type=int, default=8)
    h.add_argument("--max-total-tokens", type=int, default=20_000)
    h.add_argument("--json", action="store_true")

    ev = sub.add_parser(
        "evaluate",
        help="评测：干净 fixture 上换模型跑固定 case，产出可比分数与失败归因；"
             "与 replay（审计、不调模型）是两件事",
    )
    ev.add_argument("--suite", default="default", choices=["default"],
                    help="case 集；当前只有 default（20 条，五类）")
    ev.add_argument("--case", action="append", default=None, dest="cases",
                    help="只跑指定 case id（可重复）；未知 id 直接报错，不静默跳过")
    ev.add_argument("--repeat", type=int, default=3,
                    help="每条 case 运行次数；PRD §10.3 要求 >= 3")
    ev.add_argument("--model", default="scripted",
                    choices=["scripted", "openai-compatible"])
    ev.add_argument("--provider", default=None, choices=list(MODEL_PROVIDERS))
    ev.add_argument("--model-name", default=None)
    ev.add_argument("--base-url", default=None)
    ev.add_argument("--workdir", type=Path, default=None,
                    help="评测工作根；必须不存在或为空（干净 fixture 是自己的前提）")
    ev.add_argument("--keep", action="store_true",
                    help="保留每次运行的工作区、事件库与快照（排查用；默认删掉）")
    ev.add_argument("--baseline", type=Path, default=None,
                    help="基线报告 JSON；给了就做可比性检查与退化/改进归因")
    ev.add_argument("--out", type=Path, default=None, help="把报告 JSON 写到这个路径")
    ev.add_argument("--json", action="store_true")

    return p


def _scripted_model(out_name: str) -> ScriptedModel:
    """无 key 时的兜底：脚本化模型只做一次 list_files 加一次写报告。

    它用来验证链路是否通，**不代表模型能力**，也不用于任何真实任务。
    """
    from .model import ModelResponse, ToolCall

    return ScriptedModel(
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
                            "name": out_name,
                            "content": "# 摘要\n\n（scripted 模式占位，未调用真实模型）\n",
                        },
                    )
                ],
            ),
            ModelResponse(finish_reason="end_turn", content="完成"),
        ]
    )


def _provider_of(args: argparse.Namespace) -> str:
    """命令行 > 环境变量 > openai。只此一处决定，别在装配里再判一次。"""
    return getattr(args, "provider", None) or os.environ.get("TIANSHU_PROVIDER") or "openai"


def _model_from(args: argparse.Namespace) -> Model:
    if args.model == "scripted":
        # worker 没有 --out（它不建 Run），用默认产物名做兜底脚本
        return _scripted_model(getattr(args, "out", None) or "summary.md")
    provider = _provider_of(args)
    name = args.model_name
    if provider == "anthropic":
        if not name:
            # 不替用户猜模型 id：猜错的表现是 404，比"必须显式给"难查得多。
            # 用 TianshuError 而不是 ModelError(kind="auth")：这是"命令行少给了
            # 参数"，不是"凭据不对"，塞进 auth 会让日志里两类问题分不开。
            raise TianshuError(
                "anthropic provider 需要显式 --model-name（例如 claude-sonnet-4-5）"
            )
        return AnthropicMessagesModel(name, base_url=args.base_url)
    return OpenAICompatibleModel(name or "gpt-4o-mini", base_url=args.base_url)


def _engine(stack, args: argparse.Namespace) -> AgentLoop:
    return AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        model=_model_from(args),
        limits=Limits(args.max_turns, args.max_total_tokens),
        # 与 Dispatcher 共用同一个投影实例：同一份 blob 目录、同一个连接。
        memory=stack.memory,
        # 语义摘要记录（PRD §7.1）。摘要开关默认关；开着的时候它必须与主循环
        # 共用同一个存储，否则"先落库再引用"和回放读不到同一份。
        summaries=stack.summaries,
    )


def _exit_code(result) -> int:
    """0 = 有结论且成功；1 = 失败/取消；3 = 需要人决定（还没结束）。"""
    if result.state in (RUN_WAITING_APPROVAL, RUN_RECONCILING, "paused"):
        return 3
    return 0 if result.outcome in ("succeeded", "noop") else 1


def _cost_note(
    cost_total_micros: int,
    unknown_cost_calls: int,
    *,
    turns: int,
    attempts: int | None,
) -> str:
    """费用行的唯一口径：**说"打出去几次"，不是"完成了几轮"**（issue #58）。

    `format_cost` 的第三个参数是"有多少次调用的账已经落库"，那是**完成轮次**
    （`turns`），不是尝试次数。全部尝试都失败时 `turns == 0`，照旧口径就会
    印出「无（没有模型调用）」——那句话把"打了 3 次都没回"读成了"什么都没发生"。

    这里按尝试次数分四种情况，每一种都只说自己知道的事：

    - `attempts == 0`：真的没发出过请求，才允许说「无（没有模型调用）」；
    - `attempts is None`（旧游标没有这个键）：不知道尝试次数。`turns == 0` 的
      旧游标只可能写于第一次模型调用之前（失败尝试不落游标），所以那是真的
      0 次；`turns > 0` 时按完成轮次报账，不补"尝试"两个字——不编数；
    - `turns == 0` 且 `attempts > 0`：一次都没完成，但确实打出去过 N 次；
    - `turns > 0`：已完成轮次的账照旧报，另有未完成的尝试单独注明（失败尝试
      可能已被对方计费，但本表只报能核对的钱，所以标"费用未知"）。
    """
    if attempts is None:
        if turns <= 0:
            return format_cost(cost_total_micros, unknown_cost_calls, 0)
        return format_cost(cost_total_micros, unknown_cost_calls, turns)
    if attempts <= 0:
        return format_cost(cost_total_micros, unknown_cost_calls, 0)
    if turns <= 0:
        return f"0 个完成轮次 / {attempts} 次尝试（费用未知）"
    note = format_cost(cost_total_micros, unknown_cost_calls, turns)
    unfinished = attempts - turns
    if unfinished <= 0:
        return note
    # 与 `format_cost` 里"已计价 + 未知"的写法保持一致：能核对的钱照旧报，
    # 没完成的尝试单独标"未知"——它们可能已经被对方计费，但本表不认没拿到的价。
    return f"{note} + 未知（{unfinished} 次尝试未完成，共 {attempts} 次尝试）"


def _print_result(result, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, default=str))
        return
    print(f"run_id : {result.run_id}")
    print(f"state  : {result.state}")
    if result.state in TERMINAL_STATES:
        print(f"outcome: {result.outcome}  ({result.error or 'ok'})")
    else:
        print(f"说明   : {result.error or '已停下，可用 run resume 继续'}")
    print(f"效果   : {result.effect_state}"
          f"（applied={result.n_applied} unknown={result.n_unknown}）")
    print(f"turns  : {result.turns}   tokens: {result.tokens}")
    # F13：费用与"费用未知的次数"分开显示。查不到价格时这里印"未知（N 次调用）"，
    # 绝不再印一个 0 —— 0 会被读成"这次是免费的"。尝试次数与完成轮次必须分开：
    # 前者回答"到底打出去几次"，后者回答"回来了几轮"（issue #58）。
    cost_note = _cost_note(
        result.cost_total_micros,
        result.unknown_cost_calls,
        turns=result.turns,
        # 旧游标里的尝试次数只是下限：按"不知道"处理，不把下限当精确值。
        attempts=(
            result.model_attempts if result.model_attempts_known else None
        ),
    )
    print(f"费用   : {cost_note}")
    if result.artifact:
        print(f"report : {result.artifact['path']}  ({result.artifact['bytes']}B)")
    if result.approval_ids and not result.terminal:
        # 终态 Run 的审批在收尾时已被 supersede，再提示"去批"就是让人批废单
        print("待批请求：")
        for approval_id in result.approval_ids:
            print(f"  - {approval_id}   （tianshu approvals approve {approval_id}）")


def _cmd_run(args: argparse.Namespace) -> int:
    needs_mcp = args.action in (None, "start", "resume")
    stack = _authority_stack(args.db, args.data_root, mcp=needs_mcp)
    try:
        action = args.action
        if action is None:
            return _run_compat(stack, args)
        if action == "status":
            return _run_status(stack, args)
        if action == "pause":
            return _run_pause(stack, args)
        if action == "cancel":
            return _run_cancel(stack, args)
        if action == "start":
            result = _engine(stack, args).start(args.task_id or "", out_name=args.out)
            return _report(stack, result, args)
        # resume
        result = _engine(stack, args).resume_run(args.run_id or "")
        return _report(stack, result, args)
    finally:
        stack.log.close()


def _run_compat(stack, args: argparse.Namespace) -> int:
    """`run --task --workspace` 保留：它等价于 task create 之后立刻 start。

    不绕过任何服务层——同一个 create_task / start 路径，只是把两步合成一条命令。
    """
    if not args.task or not args.workspace:
        print("错误：兼容入口需要 --task 与 --workspace；"
              "或改用 run start --task-id", file=sys.stderr)
        return 2
    bindings = _bindings(args)
    try:
        exec_policy = _exec_policy(args)
    except ValueError as exc:
        print(f"错误：exec 策略不合法：{exc}", file=sys.stderr)
        return 2
    try:
        outbound_allow = _outbound_allow(args)
    except ValueError as exc:
        print(f"错误：outbound_allow 不合法：{exc}", file=sys.stderr)
        return 2
    try:
        mcp_servers = _mcp_servers(args)
    except ValueError as exc:
        print(f"错误：mcp_servers 不合法：{exc}", file=sys.stderr)
        return 2
    task = stack.tasks.create_task(
        args.task,
        bindings,
        exec_policy=exec_policy,
        outbound_allow=outbound_allow,
        mcp_servers=mcp_servers,
    )
    result = _engine(stack, args).start(task.task_id, out_name=args.out)
    return _report(stack, result, args)


def _report(stack, result, args: argparse.Namespace) -> int:
    if args.json:
        report = replay(stack.log, result.run_id)
        print(json.dumps(
            {"run": result.__dict__, "replay": report.__dict__},
            ensure_ascii=False, indent=2, default=str))
        return _exit_code(result) if report.ok else 1
    _print_result(result, as_json=False)
    report = replay(stack.log, result.run_id)
    print(f"replay : {'通过' if report.ok else '失败'}"
          f"  ({report.events} 事件 / {report.messages_verified} 次消息校验)")
    for problem in report.problems:
        print(f"  ! {problem}")
    if result.state in (RUN_WAITING_APPROVAL, RUN_RECONCILING, "paused"):
        print("")
        print("这个 Run 还没结束。下一步：")
        if result.state == "waiting_approval":
            print("  tianshu approvals approve <approval_id> && "
                  "tianshu run resume --run-id " + result.run_id)
        elif result.state == "paused":
            print("  tianshu run resume --run-id " + result.run_id)
        else:
            print(f"  tianshu recover --run-id {result.run_id} --workspace <DIR>")
    return _exit_code(result) if report.ok else 1


def _run_status(stack, args: argparse.Namespace) -> int:
    run_id = args.run_id or ""
    record = stack.tasks.get_run(run_id)
    if record is None:
        print(f"错误：没有这个 run：{run_id}", file=sys.stderr)
        return 2
    cursor = stack.tasks.load_cursor(run_id)
    pending = stack.approvals.pending(run_id=run_id)
    job = stack.claims.job_for_run(run_id)
    # F13：暂停中/运行中的 Run 也要能看已经烧了多少。账在游标的预算账本里，
    # 与收尾时的 RunResult 同源，不另算一套。缺键的旧游标按"没有记录"处理。
    ledger = dict(cursor[0].budget_ledger or {}) if cursor else {}
    cost_total_micros = int(ledger.get("cost_total_micros", 0) or 0)
    unknown_cost_calls = int(ledger.get("unknown_cost_calls", 0) or 0)
    model_turns = int(ledger.get("turns", 0) or 0)
    # 尝试次数是 #58 才进账本的键。旧游标没有它（或明示为"不完整"）→ `None`
    # （不知道），不是 0（"不知道"和"零次"必须分开，否则又变回一句假话）。
    model_attempts = None
    if "model_attempts" in ledger and ledger.get("model_attempts_known") is not False:
        model_attempts = int(ledger["model_attempts"])
    if args.json:
        print(json.dumps(
            {
                "run_id": record.run_id,
                "task_id": record.task_id,
                "state": record.state,
                "outcome": record.outcome,
                "effect_state": record.effect_state,
                "control_requested": record.control_requested,
                "pending_proposals": len(cursor[0].pending_proposals) if cursor else 0,
                "completed_calls": len(cursor[0].completed_call_ids) if cursor else 0,
                "cost_total_micros": cost_total_micros,
                "unknown_cost_calls": unknown_cost_calls,
                "model_attempts": model_attempts,
                "approvals": [a.to_dict() for a in pending],
                "job": (
                    {
                        "job_id": job.job_id,
                        "state": job.state,
                        "claim_epoch": job.claim_epoch,
                        "owner_pid": job.owner_pid,
                        "cancel_requested": job.cancel_requested,
                    }
                    if job is not None
                    else None
                ),
                "subtasks": [
                    item.to_dict() for item in stack.tasks.list_subtasks(run_id)
                ],
            },
            ensure_ascii=False, indent=2, default=str))
        return _status_exit_code(record)
    print(f"run_id    : {record.run_id}")
    print(f"task      : {record.task_id}")
    print(f"state     : {record.state}"
          + (f"（已请求 {record.control_requested}，等执行器确认）"
             if record.control_requested else ""))
    print(f"outcome   : {record.outcome or '（未结束）'}")
    print(f"效果      : {record.effect_state or '（尚无记录）'}")
    if job is not None:
        # 作业状态回答的是"谁在跑 / 在等什么"，Run 状态回答的是"走到哪了"。
        # 只看 Run 状态的话，一个 dead owner 留下的 claimed 作业和"正在跑"
        # 长得一模一样。
        print(f"作业      : {job.state}  epoch={job.claim_epoch}"
              + (f"  pid={job.owner_pid}" if job.owner_pid else ""))
    if cursor:
        print(f"进度      : 待做 {len(cursor[0].pending_proposals)} 个调用 / "
              f"已完成 {len(cursor[0].completed_call_ids)} 个")
        print("费用      : "
              + _cost_note(
                  cost_total_micros,
                  unknown_cost_calls,
                  turns=model_turns,
                  attempts=model_attempts,
              ))
    _print_subtasks(stack, run_id)
    if pending:
        print(f"待批      : {len(pending)} 条")
        for approval in pending:
            print(f"  - {approval.approval_id}  {approval.action}  "
                  f"{','.join(approval.resources)}")
    return _status_exit_code(record)


def _print_subtasks(stack, run_id: str) -> None:
    """父 Run 派了哪些子任务（id / state / child_run）。

    D1 只有排队与查询：这里显示的是**子任务状态机**（queued 等），不是子 Run
    状态机——两者都列出来，不互相冒充。
    """
    subtasks = stack.tasks.list_subtasks(run_id)
    if not subtasks:
        return
    print(f"子任务    : {len(subtasks)} 个")
    for item in subtasks:
        child = stack.tasks.get_run(item.child_run_id)
        print(f"  - {item.subtask_id}  {item.state}"
              f"  child_run={item.child_run_id}"
              f"（{child.state if child is not None else '缺失'}）"
              + (f"  error={item.error}" if item.error else ""))


def _status_exit_code(run) -> int:
    """`status` 也遵守 README 的退出码契约。

    只返回 0 会让脚本把"暂停等人"和"已经跑完"当成同一件事——尤其
    `while status != 0; do sleep` 这种写法，会在一个永远等不到头的状态上转圈。
    """
    if run.state in (RUN_WAITING_APPROVAL, RUN_RECONCILING, "paused"):
        return 3
    if run.state == "fenced":
        # 被 fence 的 Run 永远不会有结论，脚本不能把它当成"还没完"一直等
        return 1
    if run.state in TERMINAL_STATES and run.outcome != "succeeded":
        return 1
    return 0


def _refuse_closed_run(
    stack, run_id: str, *, action: str, as_json: bool = False
) -> int | None:
    """对已经结束/被 fence 的 Run 说清楚"不做了"，别留下一个永远不生效的请求。

    返回 None 表示可以继续（Run 还活着）。
    """
    import json as _json

    run = stack.tasks.get_run(run_id)
    if run is None:
        print(f"错误：没有这个 run：{run_id}", file=sys.stderr)
        return 2
    if run.state not in TERMINAL_STATES and run.state != RUN_FENCED:
        return None
    reason = "已被 fence（目标或绑定改过）" if run.state == RUN_FENCED else "已经结束"
    if as_json:
        print(_json.dumps(
            {"run_id": run.run_id, "state": run.state, "refused": action,
             "reason": reason}, ensure_ascii=False, indent=2))
    else:
        print(f"没有{action}：这个 Run {reason}（state={run.state}）", file=sys.stderr)
    return 1


def _run_pause(stack, args: argparse.Namespace) -> int:
    refused = _refuse_closed_run(
        stack, args.run_id or "", action="暂停", as_json=args.json
    )
    if refused is not None:
        return refused
    record = stack.tasks.request_pause(args.run_id or "")
    if args.json:
        print(json.dumps(record.__dict__, ensure_ascii=False, indent=2, default=str))
        return 0
    if record.control_requested:
        print(f"已记录暂停请求（当前 state={record.state}）：执行器会在下一个安全点停下")
        print("这一点之后再开始的副作用不会发生；已经在执行的那一次会照常完成")
        return 0
    print(f"已暂停：{record.run_id}（交还了工作区租约，resume 会重新获取）")
    return 0


def _run_cancel(stack, args: argparse.Namespace) -> int:
    refused = _refuse_closed_run(
        stack, args.run_id or "", action="取消", as_json=args.json
    )
    if refused is not None:
        return refused
    before = stack.tasks.get_run(args.run_id or "")
    record = stack.tasks.request_cancel(args.run_id or "")
    already_over = before is not None and before.state in TERMINAL_STATES | {"fenced"}
    if args.json:
        print(json.dumps(record.__dict__, ensure_ascii=False, indent=2, default=str))
        return 1 if already_over and record.state != "cancelled" else 0
    if record.control_requested:
        print(f"已记录取消请求（当前 state={record.state}）：执行器会在安全点停下")
        print("已经发生的修改不会被撤回；请用 recover 核查实际效果")
        return 0
    if record.state != "cancelled":
        # 已经结束（或被 fence）的 Run 不是"取消成功"。说"已取消"是撒谎：
        # 它可能是成功结束的，用户会以为自己的取消挡下了什么。
        print(f"没有取消：这个 Run 已经结束（state={record.state}）",
              file=sys.stderr)
        return 1
    print(f"已取消：{record.run_id}（effect_state={record.effect_state or 'none_applied'}）")
    print("已经发生的修改不会被撤回；请用 recover 核查实际效果")
    return 0


def _bindings(args: argparse.Namespace) -> list[dict]:
    bindings: list[dict] = []
    for path in args.workspace or []:
        bindings.append({"root_path": str(path), "mode": "writable"})
    for path in args.readonly or []:
        bindings.append({"root_path": str(path), "mode": "readonly"})
    return bindings


EXEC_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
EXEC_DEFAULT_TIMEOUT_MS = 30_000


def _exec_policy(args: argparse.Namespace):
    """从 CLI 参数构造任务级 ExecPolicy；没给就是 None（= 全拒）。

    显式开启只需要给可执行文件绝对路径；cwd 限制自动取本任务的**可写**绑定，
    env 只放行 PATH（凭据不进子进程），并给一个明确的上限/超时。
    """
    cmds = getattr(args, "allow_exec_cmd", None)
    required = tuple(getattr(args, "require_isolation", None) or ())
    if not cmds:
        if required:
            raise ValueError(
                "--require-isolation 需要同时给 --allow-exec-cmd："
                "没有执行策略的隔离要求是空话"
            )
        return None
    roots = [b["root_path"] for b in _bindings(args) if b["mode"] == "writable"]
    return ExecPolicy(
        argv_allowlist=tuple(cmds),
        cwd_roots=tuple(roots),
        env_allowlist=("PATH",),
        max_output_bytes=EXEC_DEFAULT_MAX_OUTPUT_BYTES,
        timeout_ms=EXEC_DEFAULT_TIMEOUT_MS,
        isolation_requires=required,
    )


def _mcp_servers(args: argparse.Namespace) -> list[str] | None:
    """从 CLI 参数构造任务级 MCP server 白名单；没给就是 None（= 一个都不许）。"""
    items = getattr(args, "allow_mcp_server", None)
    if not items:
        return None
    import re

    pattern = re.compile(r"^[a-z0-9_.-]{1,64}$")
    for name in items:
        if not pattern.match(name):
            raise ValueError(f"server 名不合法：{name!r}（必须是 {pattern.pattern}）")
    return sorted(set(items))


def _mcp_config():
    """进程级 MCP 配置（`TIANSHU_MCP_CONFIG`）；没配置 → 空。

    只有**会执行工具**的入口（run / worker / daemon 驱动栈 / mcp list）才读它；
    控制面只读命令不启动外部进程。
    """
    from .mcp import config_from_env

    return config_from_env()


def _outbound_allow(args: argparse.Namespace) -> list[str] | None:
    """从 CLI 参数构造任务级外发白名单；没给就是 None（= 一条包都不发）。

    非法项**拒绝**（ValueError），不静默丢弃：一个写错的源如果被悄悄忽略，
    任务看起来"声明了"，实际一条都发不出去——那次排障会很难看。
    """
    items = getattr(args, "allow_outbound", None)
    if not items:
        return None
    from .resources import ResourceError, normalize_network_origins

    try:
        return list(normalize_network_origins(items))
    except ResourceError as exc:
        raise ValueError(str(exc)) from exc


def _cmd_task(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db, args.data_root)
    try:
        if args.action == "create":
            if not args.goal:
                print("错误：task create 需要 --goal", file=sys.stderr)
                return 2
            try:
                exec_policy = _exec_policy(args)
            except ValueError as exc:
                print(f"错误：exec 策略不合法：{exc}", file=sys.stderr)
                return 2
            try:
                outbound_allow = _outbound_allow(args)
            except ValueError as exc:
                print(f"错误：outbound_allow 不合法：{exc}", file=sys.stderr)
                return 2
            try:
                mcp_servers = _mcp_servers(args)
            except ValueError as exc:
                print(f"错误：mcp_servers 不合法：{exc}", file=sys.stderr)
                return 2
            task = stack.tasks.create_task(
                args.goal,
                _bindings(args),
                exec_policy=exec_policy,
                outbound_allow=outbound_allow,
                mcp_servers=mcp_servers,
                max_subtasks=args.max_subtasks,
            )
            return _print_task(task, args)
        if args.action == "show":
            task = stack.tasks.get_task(args.task_id or "")
            if task is None:
                print(f"错误：没有这个 task：{args.task_id}", file=sys.stderr)
                return 2
            return _print_task(task, args, stack=stack)
        if not args.task_id or (args.goal is None and args.workspace is None
                                and args.readonly is None):
            print("错误：task revise 需要 --task-id 以及 --goal 或新绑定",
                  file=sys.stderr)
            return 2
        task = stack.tasks.revise_task(
            args.task_id,
            goal=args.goal,
            bindings=_bindings(args) if (args.workspace or args.readonly) else None,
        )
        if not args.json:
            print(f"{task.task_id} 已改：goal_revision={task.goal_revision} "
                  f"lease_epoch={task.lease_epoch}")
            print("旧目标的 Run 已被 fence：它的待批请求已作废，不能继续执行")
        return _print_task(task, args)
    finally:
        stack.log.close()


def _task_json(task) -> dict:
    return {
        "task_id": task.task_id,
        "goal": task.goal,
        "goal_revision": task.goal_revision,
        "lease_epoch": task.lease_epoch,
        "active_run_id": task.active_run_id,
        "exec_policy": task.exec_policy.to_dict() if task.exec_policy else None,
        "is_control": task.is_control,
        "memory_scopes": list(task.memory_scopes),
        "outbound_allow": list(task.outbound_allow),
        "mcp_servers": list(task.mcp_servers),
        "max_subtasks": task.max_subtasks,
        "depth": task.depth,
        "parent_task_id": task.parent_task_id,
        "parent_run_id": task.parent_run_id,
        "subtask_id": task.subtask_id,
        "status": task.status,
        "workspaces": [b.to_dict() for b in task.workspaces],
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _print_task(task, args: argparse.Namespace, *, stack=None) -> int:
    if args.json:
        payload: dict = {"task": _task_json(task)}
        if stack is not None:
            payload["runs"] = [
                {
                    "run_id": r.run_id,
                    "state": r.state,
                    "outcome": r.outcome,
                    "effect_state": r.effect_state,
                }
                for r in stack.tasks.list_runs(task.task_id)
            ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"task_id  : {task.task_id}")
    print(f"goal     : {task.goal}")
    print(f"revision : {task.goal_revision}   epoch: {task.lease_epoch}")
    print(f"active   : {task.active_run_id or '（无）'}")
    if task.workspaces:
        for binding in task.workspaces:
            print(f"  {binding.mode:8s} {binding.root_path}")
    else:
        print("  （没有绑定目录：只能对话，不能读写文件）")
    if task.outbound_allow:
        print(f"外发源   : {', '.join(task.outbound_allow)}")
    else:
        print("外发源   : （未声明：这个任务不能出网）")
    if task.mcp_servers:
        print(f"MCP      : {', '.join(task.mcp_servers)}"
              "（每次调用仍需人工批准）")
    else:
        print("MCP      : （未声明：这个任务不能调用 MCP server）")
    if task.max_subtasks:
        print(f"委派     : 最多 {task.max_subtasks} 个子任务"
              f"（每 Run 最多 2 个未完成；每次 spawn 仍需人工批准）")
    else:
        print("委派     : （未声明：这个任务不能派生子任务）")
    if task.parent_task_id:
        print(f"父任务   : {task.parent_task_id} / run {task.parent_run_id}"
              f"（slug={task.subtask_id}，depth={task.depth}）")
    if stack is not None:
        runs = stack.tasks.list_runs(task.task_id)
        for run in runs:
            print(f"  run {run.run_id}  {run.state}"
                  f"  {run.outcome or ''}  {run.effect_state or ''}")
    return 0


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
    from .worker import Worker

    # worker 会真的执行工具 → 必须启动 MCP（没有配置时就是内置工具面）。
    stack = _authority_stack(args.db, args.data_root, mcp=True)
    try:
        worker = Worker(
            stack,
            model=_model_from(args),
            limits=Limits(args.max_turns, args.max_total_tokens),
        )
        try:
            if args.once:
                ran = [worker.run_ids[-1]] if worker.run_once() else []
                idle = not ran
            else:
                ran = worker.drain(max_jobs=args.max_jobs)
                # 只有"跑空"才算 idle：跑满 --max-jobs 或撞上被占用的工作区
                # 都说明队里可能还有活，报 idle 是骗人。
                idle = worker.exhausted
        finally:
            worker.close()
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
    log = EventLog(args.db, blob_dir=args.db.parent / "blobs")
    # 数据根不一定是 `--db` 的父目录：`run/worker` 允许 `--data-root`，
    # 快照清单跟着数据根走。这里写死 db.parent 会让 recover 找不到清单，
    # 把"其实没写"的调用判成 unresolved 并顺手阻断整个目录。
    data_root = args.data_root if args.data_root is not None else args.db.parent
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


def _authority_stack(
    db: Path, data_root: Path | None = None, *, mcp: bool = False
):
    """把 M2 的权威存储装配起来。

    CLI 只做装配与格式化，判定全在库里——也包括"用了哪些 store"这件事：
    测试和 CLI 走同一个 `build_stack`，否则"CLI 能用但库函数不行"这类
    绕过点会一直存在。
    """
    log = EventLog(db, blob_dir=db.parent / "blobs")
    configs = _mcp_config() if mcp else {}
    return build_stack(
        log,
        data_root=data_root if data_root is not None else db.parent,
        mcp_servers=configs,
    )


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
            plan = plan_undo(stack, run_id, scope=args.workspace)
        except UndoRefused as refused:
            # 拒绝的文案与退出码都由 undo 模块给出：UI 也走同一条判定，
            # 不能出现"CLI 说没可撤销、UI 说撤销失败"这种两种说法。
            stream = sys.stdout if refused.code == 1 else sys.stderr
            for line in refused.lines or [str(refused)]:
                print(line, file=stream)
            return refused.code

        if not args.approve:
            print("撤销会做这些事（尚未执行）：")
            for action, desc in plan.describe():
                print(f"  - {action}: {desc}")
            print("")
            print("这些都属于覆盖/删除类操作，需要人工批准。")
            print("确认无误后加 --approve 重跑同一条命令。")
            return 3

        try:
            outcome = apply_undo(stack, run_id, scope=args.workspace)
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
    from .daemon import serve
    from .ipc import default_data_root

    data_root = Path(args.data_root or default_data_root())
    serve(
        data_root,
        model=_model_from(args),
        limits=Limits(args.max_turns, args.max_total_tokens),
    )
    return 0


def _cmd_host(args: argparse.Namespace) -> int:
    from .host import read_host_info, run_host, stop_host
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
        port=args.port,
        open_browser=args.open,
        supervise=not args.no_supervise,
    )


# ---------------------------------------------------------------- 记忆控制面


def _cli_memory_scopes(args: argparse.Namespace) -> tuple[MemoryScope, ...]:
    """命令行能看到的作用域。**

    默认只有 `user`：单用户产品的本地人当然是 owner，但"默认全库"会让一条
    命令的输出取决于库里恰好有什么。要看 workspace / task 记忆就把作用域
    显式加进来——这与 F10 §6「一个 Run 只装备它有权访问的作用域」是同一套
    口径，只是这里的主体是人，不是模型。
    """
    scopes: list[MemoryScope] = [MemoryScope.user()]
    for path in args.workspace or []:
        scopes.append(MemoryScope.workspace(path))
    if args.task_id:
        scopes.append(MemoryScope.task(args.task_id))
    if args.scope:
        scope_id = args.scope_id
        if scope_id is None:
            if args.scope == "user":
                scope_id = "local"
            elif args.scope == "task":
                if not args.task_id:
                    raise TianshuError("--scope task 需要 --task-id（或 --scope-id）")
                scope_id = args.task_id
            else:
                raise TianshuError("--scope workspace 需要 --scope-id（绝对路径）")
        scopes.append(MemoryScope(args.scope, scope_id))

    seen: set[MemoryScope] = set()
    deduped: list[MemoryScope] = []
    for scope in scopes:
        if scope not in seen:
            seen.add(scope)
            deduped.append(scope)
    return tuple(deduped)


def _memory_public(
    memory, content: str | None = None, *, score=None, score_kind=None
) -> dict:
    """一条记忆对人和 JSON 的统一投影（实现在 `memory_control.memory_view`）。

    六项判据（`scope` / `origin` / `revision` / `status` / `source_refs` /
    `proof_count`）由那个函数保证，CLI 与 UI 视图不许各写一份——写两份就会
    漂移，最后变成"命令行能看见、UI 看不见"。
    """
    return memory_view(memory, content, score=score, score_kind=score_kind)


def _print_memory(
    memory, content: str | None = None, *, score=None, score_kind=None
) -> None:
    payload = _memory_public(memory, content, score=score, score_kind=score_kind)
    print(f"memory_id : {payload['memory_id']}")
    print(f"revision  : {payload['revision']}")
    print(f"scope     : {payload['scope']}  {payload['scope_id']}")
    print(f"status    : {payload['status']}")
    print(f"kind      : {payload['kind']}   origin: {payload['origin']}")
    print(f"source_refs: {', '.join(payload.get('source_refs') or []) or '（无）'}")
    print(f"证据      : proof_count={payload.get('proof_count', 0)}")
    print(f"内容引用  : {payload.get('content_ref') or '（无）'}")
    if payload.get("created_at"):
        print(f"创建/过期 : {payload['created_at']} / {payload.get('expires_at') or '（无）'}")
    if payload.get("supersedes"):
        print(f"取代      : {payload['supersedes']}")
    if content is not None:
        print("内容      :")
        for line in content.splitlines() or [""]:
            print(f"  {line}")


def _cmd_memory(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db, args.data_root)
    try:
        projection = stack.memory
        allowed = _cli_memory_scopes(args)

        if args.action == "list":
            rows = projection.list_memories(
                allowed=allowed, status=args.status, limit=args.limit
            )
            if args.json:
                print(json.dumps(
                    [_memory_public(m) for m in rows],
                    ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("没有符合条件的记忆")
                return 0
            for index, memory in enumerate(rows):
                if index:
                    print("")
                _print_memory(memory)
            return 0

        if args.action == "remember":
            if not args.value:
                print("错误：memory remember 需要记忆内容", file=sys.stderr)
                return 2
            scope = MemoryScope.user()
            if args.task_id:
                scope = MemoryScope.task(args.task_id)
            elif args.scope and args.scope != "user":
                print(
                    "错误：remember 只支持 user / task 作用域；"
                    "共享到 workspace 由 memory promote 负责（N1 后续提交）",
                    file=sys.stderr,
                )
                return 2
            return _remember_memory(stack, scope, args)

        if args.action == "rebuild-vectors":
            # 语义召回的**构建入口**：只装配 provider 还不够，必须显式建一次
            # 索引；否则 availability() 报"尚未建立"，检索永远 FTS-only。
            if getattr(projection, "index", None) is None:
                print(
                    "错误：当前没有配置向量 provider（设置 "
                    "TIANSHU_EMBEDDING_PROVIDER=hash 或由装配方传入）；"
                    "没有索引可建",
                    file=sys.stderr,
                )
                return 2
            written = projection.rebuild_vectors()
            available, reason = projection.index.availability()
            if args.json:
                print(json.dumps(
                    {"rows": written, "available": available, "reason": reason},
                    ensure_ascii=False, indent=2))
                return 0
            print(f"已重建向量索引：{written} 条；可用={available}；原因={reason or '—'}")
            return 0

        if not args.value:
            print(f"错误：memory {args.action} 需要查询词 / memory_id",
                  file=sys.stderr)
            return 2

        if args.action == "search":
            from .memory import MAX_RECALL_BYTES, MAX_RECALL_ITEMS, RecallPriority

            # 控制面检索也带上"离谁更近"的上下文：给了 --task-id / --workspace
            # 就按 F10 §8 排序，否则退化成 origin → 时间（不猜调用方意图）。
            priority = RecallPriority(
                task_id=args.task_id,
                workspace_roots=tuple(scope.scope_id for scope in allowed
                                      if scope.scope == "workspace"),
            )
            outcome = projection.retrieve(
                args.value,
                allowed=allowed,
                limit=min(max(args.limit, 1), MAX_RECALL_ITEMS),
                max_bytes=MAX_RECALL_BYTES,
                priority=priority,
            )
            recalled = outcome.items
            if args.json:
                # 检索结果就是模型会看到的那份（F10 §8：带出处，不是全库）。
                print(json.dumps(
                    {
                        "items": [
                            _memory_public(
                                r.memory,
                                r.content,
                                score=r.score,
                                score_kind=r.score_kind,
                            )
                            for r in recalled
                        ],
                        # 能力边界也进机器可读输出：`semantic=false` 不是"没找到"，
                        # 原因在 `degraded`。
                        "semantic": outcome.semantic,
                        "degraded": outcome.degraded,
                        "fused": outcome.fused,
                    },
                    ensure_ascii=False, indent=2, default=str))
                return 0
            if not recalled:
                print("没有检索到记忆")
                return 0
            print(f"检索到 {len(recalled)} 条（最多 {MAX_RECALL_ITEMS} 条 / "
                  f"{MAX_RECALL_BYTES}B）：")
            if not outcome.semantic:
                print(f"（本次只有词面召回：{outcome.degraded}）")
            for index, item in enumerate(recalled):
                if index:
                    print("")
                _print_memory(
                    item.memory,
                    item.content,
                    score=item.score,
                    score_kind=item.score_kind,
                )
            return 0

        memory = projection.get(args.value, allowed=allowed)
        if memory is None:
            print(
                f"错误：记忆不存在或不在当前作用域集合内：{args.value}\n"
                "（非 user 作用域请用 --workspace / --task-id / --scope 加上）",
                file=sys.stderr,
            )
            return 1

        if args.action == "get":
            content = projection.read_content(memory)
            if args.json:
                print(json.dumps(
                    _memory_public(memory, content),
                    ensure_ascii=False, indent=2, default=str))
            else:
                _print_memory(memory, content)
                if content is None:
                    print("警告：历史证据不可用：内容快照缺失"
                          "（不回落到当前记忆库）", file=sys.stderr)
            return 0 if content is not None else 1

        if args.action == "edit":
            if not args.content:
                print("错误：memory edit 需要 --content（新版正文）", file=sys.stderr)
                return 2
            return _dispatch_memory_write(
                stack,
                memory,
                "memory.edit",
                {
                    "memory_id": memory.memory_id,
                    "revision": memory.revision,
                    "content": args.content,
                },
                args,
            )

        if args.action == "promote":
            if not args.to_scope:
                print("错误：memory promote 需要 --to-scope / --to-scope-id",
                      file=sys.stderr)
                return 2
            return _dispatch_memory_promote(stack, memory, args)

        if args.action == "erase":
            return _dispatch_memory_write(
                stack,
                memory,
                "memory.erase",
                {"memory_id": memory.memory_id, "revision": memory.revision},
                args,
            )

        # confirm / forget 是**人**发起的记忆生命周期操作：人就是批准者本身，
        # 所以这里用本地人身份把那张审批当场签掉。它仍然走完整派发路径——
        # intent/result + Receipt + 一次性消费——不是控制面偷写存储。
        result = _dispatch_memory_decision(
            stack, memory, confirm=args.action == "confirm"
        )
        updated = projection.get(
            memory.memory_id,
            allowed=(MemoryScope(memory.scope, memory.scope_id),),
        )
        if args.json:
            print(json.dumps(
                {
                    "memory_id": memory.memory_id,
                    "action": args.action,
                    "status": result.status,
                    "effect_state": result.effect_state,
                    "output": result.output,
                    "memory": _memory_public(updated) if updated is not None else None,
                },
                ensure_ascii=False, indent=2, default=str))
        else:
            print(f"{memory.memory_id}: {args.action} -> {result.status}")
            if updated is not None:
                # 人做完决定后要看得到"现在是什么状态、依据还在不在"。
                print(f"  scope: {updated.scope} {updated.scope_id}")
                print(f"  revision: {updated.revision}")
                print(f"  status: {updated.status}")
                print(f"  source_refs: {', '.join(updated.source_refs) or '（无）'}")
            if not result.ok:
                print(f"  ! {result.output.get('error')}", file=sys.stderr)
        return 0 if result.ok else 1
    finally:
        stack.log.close()


def _dispatch_memory_decision(stack, memory, *, confirm: bool):
    """把「确认 / 忘记」变成一次经人签字的派发。

    与 `undo` 的 `--approve` 同一形状：用户明确敲了这条命令，就是批准；审批
    仍然落库、仍然一次性消费、执行侧仍然二次核验 digest。**不**给模型开一条
    "人已经批过了"的后门——这里根本没有模型参与。

    实现在 `memory_control.transition`：CLI 与 daemon 的 RPC 共用同一份，避免
    "写记忆要经 dispatcher"这条纪律被抄成两份（一份早晚会图省事直接改投影）。
    """
    return memory_control.transition(stack, memory, confirm=confirm)


def _dispatch_memory_write(stack, memory, tool_id: str, tool_args: dict, args) -> int:
    """把一次"人签字的记忆写"变成经控制 Run 的真实派发（人敲命令 = 批准）。

    `edit` / `erase` 的 args 形状不同，由 `memory_control` 各自的入口负责构造；
    这里只剩"派发 + 打印结果"——打印是 CLI 的职责，不是控制面语义的一部分。
    """
    if tool_id == "memory.edit":
        result = memory_control.edit(stack, memory, content=str(tool_args["content"]))
    elif tool_id == "memory.erase":
        result = memory_control.erase(stack, memory)
    else:  # pragma: no cover - 关集：只允许这两个别的工具没有控制面入口
        raise TianshuError(f"未知的控制面记忆写：{tool_id}")
    if args.json:
        print(json.dumps(
            {
                "action": tool_id,
                "memory_id": memory.memory_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{memory.memory_id}: {tool_id} -> {result.status}")
        if result.output.get("memory_id"):
            print(f"  new memory_id: {result.output['memory_id']}")
        if result.output.get("supersedes"):
            print(f"  supersedes   : {result.output['supersedes']}")
        erasure = result.output.get("erasure")
        if isinstance(erasure, dict):
            state = "已删除" if erasure.get("deleted") else "被其它记忆引用，保留"
            print(f"  正文副本     : {state}（{erasure.get('content_ref')}）")
            if erasure.get("retained_by"):
                print(f"  仍被引用     : {', '.join(erasure['retained_by'])}")
        if not result.ok and result.output.get("error"):
            print(f"  ! {result.output.get('error')}", file=sys.stderr)
    return 0 if result.ok else 1


def _dispatch_memory_promote(stack, memory, args) -> int:
    """把一条记忆移动（共享）到 --to-scope；原行退役、目标作用域新行是候选。"""
    new_scope_id = args.to_scope_id
    if new_scope_id is None:
        if args.to_scope == "user":
            new_scope_id = "local"
        elif args.to_scope == "task":
            if not args.task_id:
                raise TianshuError("--to-scope task 需要 --task-id 或 --to-scope-id")
            new_scope_id = args.task_id
        else:
            raise TianshuError("--to-scope workspace 需要 --to-scope-id（绝对路径）")
    try:
        new_scope = build_memory_scope(args.to_scope, new_scope_id)
    except TianshuError as exc:
        raise TianshuError(f"提升目标作用域不合法：{exc}") from exc

    result = memory_control.promote(stack, memory, new_scope)
    if args.json:
        print(json.dumps(
            {
                "action": "memory.promote",
                "memory_id": memory.memory_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{memory.memory_id}: promote -> {result.status}")
        print(f"  新行: {result.output.get('memory_id')} "
              f"({result.output.get('scope')} {result.output.get('scope_id')}, "
              f"{result.output.get('status')})")
        print("  原行已退役（保留可查）；新行是候选，需要再 confirm 才召回")
    return 0 if result.ok else 1


def _remember_memory(stack, scope, args) -> int:
    """`tianshu memory remember`：人直接记住一条（控制面专属工具）。

    与 `undo --approve` 同一形状：人敲了命令就是签发者，工具本身不需要审批；
    但仍走完整 intent/result + Receipt 与执行侧二次核验。模型清单里没有
    `memory.remember`，所以模型不可能走这条路自封"用户明示"。
    """
    result = memory_control.remember(
        stack,
        scope,
        kind=args.kind,
        content=args.value,
        expires_at=args.expires_at,
    )
    output = result.output or {}
    if args.json:
        print(json.dumps(
            {
                "action": "remember",
                "status": result.status,
                "effect_state": result.effect_state,
                "output": output,
            },
            ensure_ascii=False, indent=2, default=str))
    elif result.ok:
        print(f"{output.get('memory_id')}: remember -> active")
        print(f"  scope: {output.get('scope')} {output.get('scope_id')}")
        print(f"  kind : {output.get('kind')}   origin: {output.get('origin')}")
    else:
        print(f"remember -> {result.status}", file=sys.stderr)
        if output.get("error"):
            print(f"  ! {output.get('error')}", file=sys.stderr)
    return 0 if result.ok else 1


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


def _default_eval_workdir() -> Path:
    from datetime import datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(".tianshu/eval") / stamp


def _cmd_evaluate(args: argparse.Namespace) -> int:
    """评测入口。与 replay 的分工写在 `tianshu.eval` 包的 docstring 里。

    退出码：0 = 全绿 / 1 = 有 case 失败 / 2 = 前置条件错 / 4 = unsafe
    （安全门槛一票否决，新增的码就是为了让"不安全"不被读成普通失败）。
    """
    from .eval import SUITE_VERSION, build_report, compare_reports, run_suite, select_cases
    from .eval.checks import evaluate_checks, run_passed

    if args.repeat < 3:
        raise TianshuError(
            f"PRD §10.3 要求每条 case ≥3 次运行；当前 --repeat={args.repeat}。"
            "要冒烟请直接调库函数，别用评测命令产出「可比分数」。"
        )
    cases = select_cases(args.cases)
    if args.cases and (args.out is not None or args.baseline is not None):
        raise TianshuError(
            "--case 子集只用于冒烟：不接受 --out / --baseline。"
            "子集报告的安全门槛只覆盖选中的几条，拿它当'可比分数'是假证据；"
            "正式评测请跑完整 20 条。"
        )
    workdir = args.workdir or _default_eval_workdir()
    if workdir.exists() and any(workdir.iterdir()):
        raise TianshuError(
            f"评测工作根必须不存在或为空（干净 fixture 是结果可信的前提）：{workdir}"
        )

    if args.model != "scripted" and not any(case.scope == "model" for case in cases):
        # 崩溃 case 恒用离线脚本跑（崩溃点必须确定），拿它给真实模型记分是假证据。
        raise TianshuError(
            "所选 case 全部是 scope=harness（崩溃恢复类），它们始终用离线脚本跑，"
            "不构成对真实模型的评测；请至少选择一条 scope=model 的 case。"
        )

    real_model = _model_from(args) if args.model != "scripted" else None

    def model_for(case):
        # 崩溃 case 恒用 case 自带脚本：崩溃点必须确定，见 eval/runner._run_crash。
        if real_model is None or case.crash:
            return ScriptedModel(list(case.script))
        return real_model

    def on_case(case, observations) -> None:
        passes = sum(
            1
            for obs in observations
            if run_passed(evaluate_checks(obs, case.checks))
        )
        print(
            f"  {case.case_id:<3} {passes}/{len(observations)}  {case.title}",
            file=sys.stderr,
        )

    suite = run_suite(
        cases,
        model_for=model_for,
        repeat=args.repeat,
        workdir=workdir,
        keep=args.keep,
        # 身份写"被评测的那个模型"：崩溃 case 恒用离线脚本，不能拿它冒充。
        identity=(
            {"adapter": "scripted", "vendor": None, "model": ""}
            if real_model is None
            else _identity_payload(real_model)
        ),
        on_case=on_case,
    )
    report = build_report(
        suite,
        cases,
        suite_version=SUITE_VERSION,
        selection={
            "complete": args.cases is None,
            "requested": list(args.cases) if args.cases else None,
            "case_count": len(cases),
        },
    )
    report["workspace_retained"] = bool(args.keep)
    comparison = None
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_bytes().decode("utf-8"))
        comparison = compare_reports(baseline, report)

    payload = {"report": report, "compare": comparison}
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        # 落盘的是**报告本体**，不是上面的信封：这样它可以直接当下一次的
        # `--baseline`，不会被信封的 schema 判成"不可比"。
        args.out.write_bytes(
            (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_eval_summary(report, comparison)

    if report["verdict"] == "unsafe":
        return 4
    return 0 if report["verdict"] == "pass" else 1


def _identity_payload(model: Model) -> dict[str, str | None]:
    from .model import identity_of

    identity = identity_of(model)
    if identity is None:
        return {"adapter": "unknown", "vendor": None, "model": ""}
    return {
        "adapter": identity.adapter,
        "vendor": identity.vendor,
        "model": identity.model,
    }


def _print_eval_summary(report: dict, comparison: dict | None) -> None:
    model = report["model"]
    vendor = model.get("vendor") or "未知（价格不可认领）"
    print(
        f"M4 评测 · suite {report['suite_version']} · repeat {report['repeat']} · "
        f"模型 {model['adapter']} / {vendor} / {model['model'] or '（未指定）'}"
    )
    print("")
    for category, stats in report["categories"].items():
        print(
            f"  {category:<18} case {stats['cases']:>2}  "
            f"runs {stats['runs']:>3}  "
            f"case 通过率 {stats['pass_rate']:.0%}  "
            f"run 通过率 {stats['run_pass_rate']:.0%}"
        )
    overall = report["overall"]
    print("")
    print(
        f"总分   : {overall['cases_all_green']}/{overall['cases']} 条 case 全绿，"
        f"run 通过率 {overall['run_pass_rate']:.0%}"
    )
    gate = report["safety_gate"]
    print(f"安全门槛：{'通过' if gate['passed'] else '失败（一票否决，不可被其它分数抵消）'}")
    for violation in gate["violations"][:10]:
        print(
            f"  ! {violation['case_id']} run{violation['run_index']} "
            f"{violation['check_id']}：{violation['detail']}"
        )
    if report["attribution"]:
        print("失败归因：" + "，".join(f"{k}={v}" for k, v in report["attribution"].items()))
    for case in report["cases"]:
        if case["pass_rate"] >= 1.0:
            continue
        print(f"  {case['case_id']}（{case['category']}）通过率 {case['pass_rate']:.0%}")
        for run in case["run_details"]:
            if run["passed"]:
                continue
            for failed in run["failed_checks"]:
                print(f"    run{run['run_index']} {failed['check_id']}：{failed['detail']}")
    print(f"结论   : {'pass' if report['verdict'] == 'pass' else report['verdict']}")
    if comparison is not None:
        print("")
        _print_eval_comparison(comparison)


def _print_eval_comparison(comparison: dict) -> None:
    base = comparison["baseline"]
    cand = comparison["candidate"]
    print(
        f"对比   : 基线 {base['model']['adapter']}/{base['model']['model'] or '（未指定）'} "
        f"{base['verdict']} → 本次 {cand['model']['adapter']}/"
        f"{cand['model']['model'] or '（未指定）'} {cand['verdict']}"
    )
    if not comparison["comparable"]:
        print("可比性 : 不可比（不把差异读成进步/退步）")
        for guard in comparison["guards"]:
            print(f"  ! {guard}")
        return
    summary = comparison["summary"]
    print(
        f"可比性 : 可比（suite/repeat/case 定义一致）；"
        f"case 平均通过率 {summary['baseline_case_pass_rate']:.0%} → "
        f"{summary['candidate_case_pass_rate']:.0%}"
    )
    for entry in summary["regressions"]:
        print(
            f"  退化 {entry['case_id']}（{entry['category']}）"
            f"{entry['baseline_pass_rate']:.0%} → {entry['candidate_pass_rate']:.0%}，"
            f"失败判据={entry['candidate_failures']}，归因={entry['candidate_attribution']}"
        )
    for entry in summary["improvements"]:
        print(
            f"  改进 {entry['case_id']}（{entry['category']}）"
            f"{entry['baseline_pass_rate']:.0%} → {entry['candidate_pass_rate']:.0%}"
        )
    if not summary["regressions"] and not summary["improvements"]:
        print("  逐条 case 无变化")
def _force_utf8_streams() -> None:
    """把 stdout/stderr 钉成 UTF-8，不跟随控制台代码页。

    为什么必须有：Windows 上 stdout/stderr 被重定向（管道、文件、`python -c`
    的子进程）时，Python 用**系统 ANSI 代码页**（简体中文机器 cp936、英文机器
    cp1252）编码，而 CLI 的输出全是中文。一个 `print("结论   : 已撤销")` 就会抛
    `UnicodeEncodeError: 'charmap' codec can't encode characters`——用户看到的
    是"这个 CLI 崩了"，不是"少打几个字"。

    交互式控制台不受影响：Python 3.6+ 在真控制台上走 `WriteConsoleW`，本来
    就是 UTF-8 语义。这条修的是管道/重定向/子进程这三条路径，而那正是 CI、
    脚本和桌面前端读 CLI 的方式。

    `errors="backslashreplace"` 只是最后一道保险：UTF-8 能编码所有码点，
    唯一编不了的是孤立代理字符（非 UTF-8 文件名在 POSIX 上可能出现），
    这种时候宁可打出转义形式，也不要让整条命令崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # pythonw 之类：没有控制台，也没有输出可言
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            # 不是 TextIOWrapper（pytest 的 capsys、调用方替换过的流）：
            # 换不了就不换，别为了改编码把 CLI 弄崩。
            continue


def main(argv: list[str] | None = None) -> int:
    # 必须在解析参数之前：`--help` 本身也是中文。
    _force_utf8_streams()
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "run":
            return _cmd_run(args)
        if args.cmd == "task":
            return _cmd_task(args)
        if args.cmd == "mcp":
            return _cmd_mcp(args)
        if args.cmd == "worker":
            return _cmd_worker(args)
        if args.cmd == "daemon":
            return _cmd_daemon(args)
        if args.cmd == "host":
            return _cmd_host(args)
        if args.cmd == "recover":
            return _cmd_recover(args)
        if args.cmd == "undo":
            return _cmd_undo(args)
        if args.cmd in ("approvals", "approval"):
            return _cmd_approvals(args)
        if args.cmd in ("memory", "memories"):
            return _cmd_memory(args)
        if args.cmd == "replay":
            return _cmd_replay(args)
        return _cmd_evaluate(args)
    except Exception as exc:  # CLI 边界：打印可读错误，不抛栈
        # 脱敏：异常文案可能带着端点回显的 Authorization 或 URL 里的 userinfo。
        # 终端输出经常被贴进 issue/聊天记录，明文 key 就是这么泄露的。
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
