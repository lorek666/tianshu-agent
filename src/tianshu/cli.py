"""命令行入口。

    python -m tianshu run    --workspace DIR --task TEXT --out FILE   # 兼容入口
    python -m tianshu task   create --goal TEXT --workspace DIR
    python -m tianshu run    start --task-id ID --out FILE
    python -m tianshu run    status|pause|resume|cancel --run-id ID
    python -m tianshu approvals list|approve|reject|revoke
    python -m tianshu undo   --run-id ID --workspace DIR [--approve]
    python -m tianshu recover --run-id ID --workspace DIR
    python -m tianshu replay --run-id ID

设计原则：CLI 只做参数解析与输出格式化，逻辑全在库函数里——这样测试可以直接调库，
不必 spawn 子进程。**不要在解析层做任何权限判断**：判断只有一处（Dispatcher），
多一处在 CLI 就多一条"绕过 CLI 就绕过判断"的路。

退出码：0 有结论且成功 / 1 失败或取消 / 2 前置条件错误 / 3 需要人决定
（等审批、已暂停、待核查）。脚本可以据此区分"没事了"和"等你"。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from .dispatcher import build_request
from .errors import TianshuError, ToolNotAllowed
from .events import EventLog
from .guard import ensure_not_blocked
from .loop import AgentLoop, Limits, build_stack
from .model import AnthropicMessagesModel, Model, OpenAICompatibleModel, ScriptedModel
from .permissions import HumanIdentity
from .recovery import recover_run
from .replay import replay
from .tasks import (
    RUN_FENCED,
    RUN_RECONCILING,
    RUN_WAITING_APPROVAL,
    TERMINAL_STATES,
)
from .workspace import Workspace, WorkspaceBinding

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

    t = sub.add_parser("task", help="任务：创建 / 查看 / 改目标或绑定")
    t.add_argument("action", choices=["create", "show", "revise"])
    t.add_argument("--task-id", default=None)
    t.add_argument("--goal", default=None)
    t.add_argument("--workspace", type=Path, default=None, action="append")
    t.add_argument("--readonly", type=Path, default=None, action="append")
    t.add_argument("--data-root", type=Path, default=None)
    t.add_argument("--db", type=Path, default=DEFAULT_DB)
    t.add_argument("--json", action="store_true")

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
    u.add_argument("--workspace", required=True, type=Path)
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

    v = sub.add_parser("replay", help="从事件日志回放并校验")
    v.add_argument("--run-id", required=True)
    v.add_argument("--db", type=Path, default=DEFAULT_DB)
    v.add_argument("--json", action="store_true")

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
    )


def _exit_code(result) -> int:
    """0 = 有结论且成功；1 = 失败/取消；3 = 需要人决定（还没结束）。"""
    if result.state in (RUN_WAITING_APPROVAL, RUN_RECONCILING, "paused"):
        return 3
    return 0 if result.outcome in ("succeeded", "noop") else 1


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
    if result.artifact:
        print(f"report : {result.artifact['path']}  ({result.artifact['bytes']}B)")
    if result.approval_ids and not result.terminal:
        # 终态 Run 的审批在收尾时已被 supersede，再提示"去批"就是让人批废单
        print("待批请求：")
        for approval_id in result.approval_ids:
            print(f"  - {approval_id}   （tianshu approvals approve {approval_id}）")


def _cmd_run(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db, args.data_root)
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
    task = stack.tasks.create_task(args.task, bindings)
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
    if pending:
        print(f"待批      : {len(pending)} 条")
        for approval in pending:
            print(f"  - {approval.approval_id}  {approval.action}  "
                  f"{','.join(approval.resources)}")
    return _status_exit_code(record)


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


def _cmd_task(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db, args.data_root)
    try:
        if args.action == "create":
            if not args.goal:
                print("错误：task create 需要 --goal", file=sys.stderr)
                return 2
            task = stack.tasks.create_task(args.goal, _bindings(args))
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
    if stack is not None:
        runs = stack.tasks.list_runs(task.task_id)
        for run in runs:
            print(f"  run {run.run_id}  {run.state}"
                  f"  {run.outcome or ''}  {run.effect_state or ''}")
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    """作业循环入口。

    与 `run start/resume` 走**同一条**执行路径（`Worker` → `AgentLoop.resume_run`），
    区别只在于"谁来认领"：前台是命令行自己建 Run 再跑，worker 是从队里领活。
    它不会自己写文件，也不重新判定策略——那些都在派发层。
    """
    from .worker import Worker

    stack = _authority_stack(args.db, args.data_root)
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
        report = recover_run(log, args.run_id, args.workspace, snapshots, leases=leases)
    finally:
        log.close()

    if args.json:
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    else:
        print(f"run_id : {report.run_id}")
        print(f"已确认执行 : {report.applied or '（无）'}")
        print(f"确认未执行 : {report.none_applied or '（无）'}")
        print(f"无法核实   : {report.unresolved or '（无）'}")
        for p in report.problems:
            print(f"  ! {p}")
    return 0 if report.ok else 1


def _authority_stack(db: Path, data_root: Path | None = None):
    """把 M2 的权威存储装配起来。

    CLI 只做装配与格式化，判定全在库里——也包括"用了哪些 store"这件事：
    测试和 CLI 走同一个 `build_stack`，否则"CLI 能用但库函数不行"这类
    绕过点会一直存在。
    """
    log = EventLog(db, blob_dir=db.parent / "blobs")
    return build_stack(log, data_root=data_root if data_root is not None else db.parent)


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


def _undo_apply(stack, control, ws: Workspace):
    """把每个"还原/删除"都变成一次经审批的派发。"""

    def apply(change, preimage: bytes | None) -> None:
        target = ws.resolve(change.path)
        if preimage is None:
            tool_id = "delete_file"
            call_id = f"undo_del_{change.call_id}"
            tool_args = {"path": change.path}
        else:
            tool_id = "restore_file"
            call_id = f"undo_res_{change.call_id}"
            tool_args = {
                "path": change.path,
                "content_b64": base64.b64encode(preimage).decode("ascii"),
                "sha256": change.before_state,
            }

        request = build_request(
            task=control.task,
            run=control.run,
            claim=control.claim,
            call_id=call_id,
            tool_id=tool_id,
            target=target,
            args=tool_args,
        )
        decision = stack.dispatcher.prepare(request, control.claim, args=tool_args)
        if decision.is_rejected:
            raise ToolNotAllowed(f"撤销动作被拒绝：{decision.reason}（{decision.rule_id}）")
        if decision.is_waiting:
            # 本地人工在这条命令上加 --approve 就是批准：显式、可审计、
            # 且仍然走完整的"批准一次只能执行一次"记录。
            stack.approvals.decide(
                decision.approval_id, decision="approved",
                actor=HumanIdentity.local_cli(),
            )
        result = stack.dispatcher.dispatch(decision.request_id, control.claim)
        if not result.ok:
            raise ToolNotAllowed(
                f"撤销动作未执行：{result.output.get('error')}"
            )

    return apply


def _undo_plan(stack, run_id: str, ws: Workspace) -> list[tuple[str, str]]:
    """只读预演：不写文件、不登记审批。

    用户先看清要动什么，再决定加不加 --approve。撤销方向是**调用逆序**，
    同一路径被写过多次时正序会自相冲突，所以这里也按逆序排。
    """
    plan: list[tuple[str, str]] = []
    for change in reversed(stack.snapshots.load(run_id)):
        if change.before_state == "absent":
            plan.append(("delete ", f"{change.path}（原本不存在，撤销=删掉）"))
        else:
            plan.append(
                (
                    "restore",
                    f"{change.path}（还原到 {change.before_state[:12]}…）",
                )
            )
    return plan


def _cmd_undo(args: argparse.Namespace) -> int:
    stack = None
    try:
        stack = _authority_stack(args.db, args.data_root)
        ws = Workspace(args.workspace)
        run_id = args.run_id
        changes = stack.snapshots.load(run_id)
        if not changes:
            print(f"run {run_id} 没有可撤销的变更")
            return 1

        plan = _undo_plan(stack, run_id, ws)
        if not args.approve:
            print("撤销会做这些事（尚未执行）：")
            for action, desc in plan:
                print(f"  - {action}: {desc}")
            print("")
            print("这些都属于覆盖/删除类操作，需要人工批准。")
            print("确认无误后加 --approve 重跑同一条命令。")
            return 3

        control = stack.dispatcher.open_control_run(
            goal=f"undo:{run_id}", workspaces=[WorkspaceBinding(str(ws.root))]
        )
        stack.snapshots.init_run(control.run.run_id)
        try:
            report = stack.snapshots.undo(
                run_id,
                ws,
                guard=lambda: ensure_not_blocked(stack.log, ws.root),
                apply=_undo_apply(stack, control, ws),
            )
        except Exception as exc:
            # 撤销是多文件操作：中途失败必须如实记成 partial，不能悄悄停在
            # 一半还让日志看起来完好。已经完成的那些文件是真的改了。
            stack.log.append(
                run_id,
                "undo.failed",
                {
                    "run_id": run_id,
                    "control_run": control.run.run_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                actor_kind="system",
            )
            stack.dispatcher.close_control_run(
                control, outcome="failed", effect_state="partial"
            )
            print(f"撤销中断（可能已部分生效，请用 recover 核查）：{exc}", file=sys.stderr)
            return 1

        if report.ok:
            stack.log.append(
                run_id,
                "undo.completed",
                {
                    "run_id": run_id,
                    "restored": report.restored,
                    "deleted": report.deleted,
                    "control_run": control.run.run_id,
                },
                actor_kind="human",
            )
        stack.dispatcher.close_control_run(
            control,
            outcome="succeeded" if report.ok else "failed",
            effect_state="applied" if report.ok else "partial",
        )

        if args.json:
            print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
        else:
            print(f"结论   : {'已撤销' if report.ok else '未撤销'}")
            for p in report.restored:
                print(f"  还原 : {p}")
            for p in report.deleted:
                print(f"  删除 : {p}")
            for c in report.conflicts:
                print(f"  冲突 : {c}")
            for p in report.problems:
                print(f"  ! {p}")
        return 0 if report.ok else 1
    finally:
        if stack is not None:
            stack.log.close()


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
        if args.cmd == "task":
            return _cmd_task(args)
        if args.cmd == "worker":
            return _cmd_worker(args)
        if args.cmd == "recover":
            return _cmd_recover(args)
        if args.cmd == "undo":
            return _cmd_undo(args)
        if args.cmd in ("approvals", "approval"):
            return _cmd_approvals(args)
        return _cmd_replay(args)
    except Exception as exc:  # CLI 边界：打印可读错误，不抛栈
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
