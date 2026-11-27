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

内部模块拆分说明（对外符号与行为零变更）：
- `_cli_ops.py`：`mcp`/`worker`/`recover`/`undo`/`approvals`/`daemon`/`host` 与 `_authority_stack`
  （`_cmd_recover` 在本模块显式重导出，兼容外部测试直接引用 `cli._cmd_recover`）
- `_cli_memory.py`：`memory`（`memories`）控制面
- `_cli_eval.py`：`replay` 与 `evaluate`
- 本模块保留 `DEFAULT_DB`、`MODEL_PROVIDERS`、`_scripted_model`/`_provider_of`/`_model_from`、
  `run`/`task` 核心控制以及 `main()`，保证测试对 `cli._scripted_model` 与 `cli._cmd_run`
  的 monkeypatch 契约不变。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ._cli_eval import (
    _cmd_evaluate,
    _cmd_replay,
    add_evaluate_parser,
    add_replay_parser,
)
from ._cli_memory import _cmd_memory, add_memory_parser
from ._cli_ops import (
    _authority_stack,
    _cmd_approvals,
    _cmd_daemon,
    _cmd_host,
    _cmd_mcp,
    _cmd_recover,
    _cmd_undo,
    _cmd_worker,
    add_approvals_parser,
    add_daemon_parser,
    add_host_parser,
    add_mcp_parser,
    add_recover_parser,
    add_undo_parser,
    add_worker_parser,
)
from .credentials import redact
from .errors import TianshuError
from .execpolicy import ISOLATION_BOUNDARIES, ExecPolicy
from .loop import AgentLoop, Limits
from .model import AnthropicMessagesModel, Model, OpenAICompatibleModel, ScriptedModel
from .openai_responses import OpenAIResponsesModel
from .pricing import format_cost
from .replay import replay
from .tasks import (
    RUN_FENCED,
    RUN_RECONCILING,
    RUN_WAITING_APPROVAL,
    TERMINAL_STATES,
)

DEFAULT_DB = Path(".tianshu/events.db")

# 线上协议名。加第三个协议时只改这里 + `_model_from`，不动主循环。
MODEL_PROVIDERS = ("openai", "anthropic", "openai-responses")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tianshu", description="天枢 Agent")
    p.add_argument(
        "--desktop",
        action="store_true",
        default=False,
        help="指向桌面端默认数据目录（ipc.default_data_root()/events.db），"
        "替代当前目录下的 .tianshu/events.db",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "run",
        help="执行、计划、压缩上下文，或查询和控制已有 Run",
    )
    r.add_argument(
        "action", nargs="?", default=None,
        choices=["start", "status", "pause", "resume", "cancel", "plan", "compact", "execute-plan"],
        help="省略时按兼容入口处理：--task/--workspace 建 Task 后立刻 start",
    )
    r.add_argument("--task-id", default=None,
                   help="对已存在的 Task 开一次执行（start）")
    r.add_argument("--run-id", default=None, help="目标 Run；compact 来源或 execute-plan 的计划")
    r.add_argument("--request-id", default=None, help="工作流请求幂等标识；重试同一次请求时复用")
    r.add_argument("--goal-revision", type=int, default=None,
                   help="execute-plan 确认时绑定的目标修订")
    r.add_argument("--workspace", type=Path, default=None, action="append",
                   help="可重复；兼容入口与 task create 用")
    r.add_argument("--readonly", type=Path, default=None, action="append",
                   help="只读绑定目录（不能写入）")
    r.add_argument("--task", default=None, help="兼容入口的目标文本")
    r.add_argument("--out", default=None, help="兼容入口的输出文件名")
    from .mission import TASK_KINDS

    r.add_argument("--task-kind", choices=TASK_KINDS, default="general",
                   help="新 Mission 的任务类型；只决定工作方法与工具候选，不授予权限")
    r.add_argument(
        "--execution-mode",
        choices=("effects", "pure_only"),
        default=None,
        help="新 Run 的执行资格：effects（默认）或 pure_only；"
        "只适用于 run start 与兼容入口",
    )
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
    r.add_argument("--compact-threshold", type=int, default=None,
                   help="Responses 压缩阈值（令牌）；默认关闭")
    r.add_argument("--max-turns", type=int, default=8)
    r.add_argument("--max-total-tokens", type=int, default=20_000)
    r.add_argument("--data-root", type=Path, default=None,
                   help="authority 数据根（默认与 --db 同目录）")
    r.add_argument("--db", type=Path, default=DEFAULT_DB)
    r.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                   help="使用桌面端默认数据目录")
    r.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")

    add_mcp_parser(sub)

    t = sub.add_parser("task", help="任务：创建 / 查看 / 改目标或绑定")
    t.add_argument("action", choices=["create", "show", "revise"])
    t.add_argument("--task-id", default=None)
    t.add_argument("--goal", default=None)
    t.add_argument("--workspace", type=Path, default=None, action="append")
    t.add_argument("--readonly", type=Path, default=None, action="append")
    t.add_argument("--data-root", type=Path, default=None)
    t.add_argument("--db", type=Path, default=DEFAULT_DB)
    t.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                   help="使用桌面端默认数据目录")
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

    add_worker_parser(sub)
    add_recover_parser(sub)
    add_undo_parser(sub)
    add_approvals_parser(sub)
    add_memory_parser(sub)
    add_replay_parser(sub)
    add_daemon_parser(sub)
    add_host_parser(sub)
    add_evaluate_parser(sub)

    from .data_cli import add_parser

    add_parser(sub)

    from .schedule_cli import add_parser as add_schedule_parser

    add_schedule_parser(sub)
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
    if provider == "openai-responses":
        if not name or not name.strip():
            raise TianshuError("openai-responses provider 需要显式 --model-name")
        return OpenAIResponsesModel(name, base_url=args.base_url,
                                    compact_threshold=getattr(args, "compact_threshold", None))
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


def _run_execution_policy(args: argparse.Namespace):
    from .run_policy import EFFECTS, PURE_ONLY

    return PURE_ONLY if args.execution_mode == "pure_only" else EFFECTS


def _engine(stack, args: argparse.Namespace) -> AgentLoop:
    return AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        contents=stack.contents,
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
    if args.execution_mode is not None and args.action not in (None, "start"):
        print(
            "错误：--execution-mode 只适用于新建 Run（run start 或兼容入口）",
            file=sys.stderr,
        )
        return 2
    needs_mcp = args.action in (None, "start", "resume", "plan", "execute-plan")
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
        if action in ("plan", "compact", "execute-plan"):
            from .control import ControlPlane

            model = _model_from(args)
            plane = ControlPlane(
                stack, model=model, limits=Limits(args.max_turns, args.max_total_tokens),
            )
            params = {"task_kind": args.task_kind}
            if args.request_id is not None:
                params["request_id"] = args.request_id
            if action == "execute-plan":
                method = "plan.execute"
                params.update(plan_run_id=args.run_id or "", goal_revision=args.goal_revision)
            else:
                method = "run.compact" if action == "compact" else "run.start"
                task_id = args.task_id
                if action == "plan" and not task_id and args.task:
                    task_id = stack.tasks.create_task(args.task, _bindings(args)).task_id
                params["task_id"] = task_id or ""
                if action == "plan":
                    params["mode"] = "plan"
                elif args.run_id:
                    params["source_run_id"] = args.run_id
            envelope = plane.handle(method, params)
            run = envelope["run"]
            if run["state"] in TERMINAL_STATES:
                return _run_status(
                    stack, argparse.Namespace(**{**vars(args), "run_id": run["run_id"]}),
                )
            result = _engine(stack, args).resume_run(run["run_id"])
            return _report(stack, result, args)
        if action == "start":
            result = _engine(stack, args).start(
                args.task_id or "",
                out_name=args.out,
                execution_policy=_run_execution_policy(args),
                task_kind=args.task_kind,
            )
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
    result = _engine(stack, args).start(
        task.task_id,
        out_name=args.out,
        execution_policy=_run_execution_policy(args),
        task_kind=args.task_kind,
    )
    return _report(stack, result, args)


def _report(stack, result, args: argparse.Namespace) -> int:
    from .workflow import final_text, workflow_for

    workflow = workflow_for(stack.log, result.run_id)
    text = ""
    if (workflow is not None and workflow.mode in ("plan", "compact")
            and result.state == "succeeded"):
        text = final_text(stack.log, stack.tasks.get_run(result.run_id))
    if args.json:
        report = replay(stack.log, result.run_id)
        workflow_result = {"workflow": {"mode": workflow.mode, "text": text}} if workflow else {}
        print(json.dumps(
            {"run": result.__dict__, "replay": report.__dict__, **workflow_result},
            ensure_ascii=False, indent=2, default=str))
        return _exit_code(result) if report.ok else 1
    _print_result(result, as_json=False)
    if workflow is not None and workflow.mode in ("plan", "compact") and text:
        print(f"\n{'实施计划' if workflow.mode == 'plan' else '压缩摘要'}：\n{text}\n")
        if workflow.mode == "plan":
            revision = stack.tasks.get_run(result.run_id).goal_revision
            print("确认并执行：tianshu run execute-plan --run-id "
                  f"{result.run_id} --goal-revision {revision}"
                  "（沿用相同 --db / --data-root 与模型参数）")
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
    if cursor is not None and cursor[0].has_exact_model_attempts():
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
                "unfinished_subtasks": stack.tasks.count_unfinished_subtasks(run_id),
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
    unfinished = stack.tasks.count_unfinished_subtasks(run_id)
    if unfinished:
        # 父 Run 等子任务时会停在 RUNNING + 作业 waiting。只说"running"会让人
        # 以为它还在干活；这里明确"在等谁"和"谁会叫醒它"。
        print(f"等待子任务 : {unfinished} 个未完成"
              + ("（作业 waiting，子任务收尾后自动唤醒）"
                 if job is not None and job.state == "waiting" else ""))
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


def _apply_desktop_db_bridge(
    args: argparse.Namespace, raw_argv: Sequence[str]
) -> None:
    """打通内核 CLI 与桌面端默认数据根，且不改变既有 DEFAULT_DB 契约。"""
    from . import ipc

    explicit_db = any(
        tok == "--db" or tok.startswith("--db=") for tok in raw_argv
    )
    explicit_data_root = any(
        tok == "--data-root" or tok.startswith("--data-root=")
        for tok in raw_argv
    ) or (getattr(args, "data_root", None) is not None)
    use_desktop = bool(getattr(args, "desktop", False)) or (
        os.environ.get("TIANSHU_USE_DESKTOP_DB") == "1"
    )
    has_db = hasattr(args, "db")
    current_db = getattr(args, "db", None)
    if use_desktop and (has_db or getattr(args, "cmd", None) == "data"):
        if explicit_db or explicit_data_root or (
            current_db is not None and current_db != DEFAULT_DB
        ):
            raise TianshuError(
                "--desktop（或 TIANSHU_USE_DESKTOP_DB=1）不能与显式 --db 或 --data-root 同时指定"
            )
        desktop_root = ipc.default_data_root()
        if has_db:
            args.db = desktop_root / "events.db"
        if hasattr(args, "data_root"):
            args.data_root = desktop_root
        return

    # 交互式终端提示：当前目录尚无 .tianshu/events.db，而桌面端数据根已有库时提示 --desktop
    interactive = (
        getattr(sys.stderr, "isatty", lambda: False)()
        or os.environ.get("TIANSHU_FORCE_CLI_HINT") == "1"
    )
    if (
        interactive
        and has_db
        and not explicit_db
        and not explicit_data_root
        and not getattr(args, "json", False)
        and (current_db is None or current_db == DEFAULT_DB)
        and not DEFAULT_DB.exists()
    ):
        desktop_db = ipc.default_data_root() / "events.db"
        if desktop_db.is_file():
            print(
                f"提示：当前目录未找到 {DEFAULT_DB}；"
                f"若要查看/操作桌面端数据，请加 --desktop（{desktop_db}）",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    # 必须在解析参数之前：`--help` 本身也是中文。
    _force_utf8_streams()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(effective_argv)
    try:
        _apply_desktop_db_bridge(args, effective_argv)
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
        if args.cmd == "data":
            from .data_cli import run

            return run(args)
        if args.cmd == "schedule":
            from .schedule_cli import run

            return run(args)
        return _cmd_evaluate(args)
    except Exception as exc:  # CLI 边界：打印可读错误，不抛栈
        # 脱敏：异常文案可能带着端点回显的 Authorization 或 URL 里的 userinfo。
        # 终端输出经常被贴进 issue/聊天记录，明文 key 就是这么泄露的。
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
