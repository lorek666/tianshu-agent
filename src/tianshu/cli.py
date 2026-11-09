"""命令行入口。

    python -m tianshu run    --workspace DIR --task TEXT --out FILE
    python -m tianshu replay --run-id ID

设计原则：CLI 只做参数解析与输出格式化，逻辑全在库函数里——这样测试可以直接调库，
不必 spawn 子进程。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from .dispatcher import Dispatcher, build_request
from .errors import ToolNotAllowed
from .events import EventLog
from .guard import ensure_not_blocked
from .jobs import JobStore
from .leases import LeaseStore
from .loop import Limits, run
from .model import OpenAICompatibleModel, ScriptedModel
from .permissions import (
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from .recovery import recover_run
from .replay import replay
from .snapshot import SnapshotStore
from .tasks import TaskStore
from .workspace import Workspace, WorkspaceBinding

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

    rc = sub.add_parser("recover", help="核对并补全一次被打断的 run")
    rc.add_argument("--run-id", required=True)
    rc.add_argument("--workspace", required=True, type=Path)
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

    ap = sub.add_parser("approvals", help="审批队列：列出/批准/否决/撤销")
    ap.add_argument("action", choices=["list", "approve", "reject", "revoke"])
    ap.add_argument("approval_id", nargs="?")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")

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

    snapshots = SnapshotStore(args.db.parent / "artifacts")

    result = run(
        task=args.task,
        workspace_dir=args.workspace,
        out_name=args.out,
        model=model,
        log=log,
        limits=Limits(args.max_turns, args.max_total_tokens),
        snapshots=snapshots,
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


def _cmd_recover(args: argparse.Namespace) -> int:
    log = EventLog(args.db, blob_dir=args.db.parent / "blobs")
    snapshots = SnapshotStore(args.db.parent / "artifacts")
    leases = LeaseStore(log)
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
    """把 M2 的权威存储装配起来。CLI 只做装配与格式化，判定全在库里。"""
    from dataclasses import dataclass

    log = EventLog(db, blob_dir=db.parent / "blobs")
    root = Path(data_root) if data_root is not None else db.parent
    leases = LeaseStore(log)
    claims = JobStore(log)
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=root)
    approvals = ApprovalStore(log)
    grants = GrantStore(log)
    snapshots = SnapshotStore(root / "artifacts")
    dispatcher = Dispatcher(
        log,
        tasks,
        leases,
        PolicyEngine(),
        approvals,
        grants=grants,
        claims=claims,
        snapshots=snapshots,
        data_root=root,
    )

    @dataclass
    class Stack:
        log: EventLog
        leases: LeaseStore
        claims: JobStore
        tasks: TaskStore
        approvals: ApprovalStore
        dispatcher: Dispatcher
        snapshots: SnapshotStore

    return Stack(log, leases, claims, tasks, approvals, dispatcher, snapshots)


def _cmd_approvals(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db)
    try:
        if args.action == "list":
            rows = [
                a for a in stack.approvals.pending()
            ]
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
    stack = _authority_stack(args.db, args.data_root)
    ws = Workspace(args.workspace)
    run_id = args.run_id
    try:
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
        if args.cmd == "recover":
            return _cmd_recover(args)
        if args.cmd == "undo":
            return _cmd_undo(args)
        if args.cmd == "approvals":
            return _cmd_approvals(args)
        return _cmd_replay(args)
    except Exception as exc:  # CLI 边界：打印可读错误，不抛栈
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
