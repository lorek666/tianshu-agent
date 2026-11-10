"""#53 的两条静态疑点：否决缓存的作用域、以及「撤销已消费未执行」的三条路径。

来源：`docs/DEBUG-M0M5.md`「静态疑点（未复现，登记待查）」。复审只读码登记，
不是已确认漏洞。这一组测试把它们逐条变成**可复现或可反证的探针**，结论写进
`docs/cost-and-veto.md`。

## 疑点 1：否决缓存只按 `run_id`

否决缓存（`ApprovalStore.rejected_for`）在 SQL 里按 `run_id` 过滤。问题不是
"过滤条件写错了"，而是**同一 Task 换新 Run 会不会忘掉上一次的否决**。

答案取决于一个前提：**Run 只能由人/控制面发起**。模型只有工具调用能力
（`tool_manifest()` 里的 6 个工具），没有 `run.start` RPC、也没有"新建 Run"
这个工具。所以"换个 Run 再问一次"必然是人/控制面主动发起的新执行——那是
人的决定，不是模型绕过否决。

这一组测试钉住这个前提，分三层：

1. 模型可见工具清单 == `SPECS` 里 `exposed=True` 的投影，且每个工具的动作都在
   封闭动作集里（没有"启动 Run"这种动作类）；
2. `tools.py` 连 `TaskStore` 都拿不到（只从 `.tasks` 导入 `ExecutionPermit`
   票据），结构上不可能 `start_run`；
3. 活的探针：模型在 Run 里提议 `run_command`（最高危的可见工具）也不会触发第二
   次 `TaskStore.start_run`。

## 疑点 2：`revoke_consumed` 的判定依赖 dispatcher 的状态核验

三条路径逐条探针：

- A 撤销后同一个 approval 不能被再次派发消费（也不会有第二次 `tool.intent`）；
- B 越过 `execution.started` 之后撤销必须失败，不能假装回滚；
- C 撤销之后这个 Run 里重新发起同一个动作 → 新审批 → 能正常执行。

三条都成立时不改 `dispatcher.py`（该文件不属于本工作流）；测试只钉现状。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tianshu.dispatcher import (
    KIND_WAITING,
    Dispatcher,
    build_request,
)
from tianshu.errors import ToolArgumentError
from tianshu.events import EventLog
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import (
    ApprovalError,
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.tools import SPECS, ExecutionPermit, tool_manifest

REPO = Path(__file__).resolve().parents[1]
ORIGINAL = "原始内容\n"
#: 模型动作的封闭集合（PRD §5.2 / C4）。没有任何一项等于"启动 Run"。
MODEL_ACTION_SET = {"read", "create", "overwrite", "delete", "exec"}
#: 控制面/CLI 才有的动作名。模型工具里出现它们就是越界。
CONTROL_ONLY_NAMES = {
    "run_start",
    "run.start",
    "start_run",
    "run_resume",
    "task_create",
    "task_revise",
    "approvals_decide",
    "undo_apply",
    "recover_run",
}

write_exact = lambda path, text: path.write_bytes(text.encode("utf-8"))  # noqa: E731


# ---------------------------------------------------------------- 夹具


@dataclass
class Env:
    root: Path
    ws: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    def new_run(self):
        task = self.tasks.create_task("整理目录里的笔记", [{"root_path": str(self.ws)}])
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([self.ws], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim


def _env(tmp_path: Path) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", ORIGINAL)
    data = tmp_path / "authority"
    data.mkdir()
    log = EventLog(tmp_path / "events.db")
    leases = LeaseStore(log)
    claims = JobStore(log)
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=data)
    approvals = ApprovalStore(log)
    grants = GrantStore(log)
    snapshots = SnapshotStore(tmp_path / "artifacts")
    dispatcher = Dispatcher(
        log,
        tasks,
        leases,
        PolicyEngine(),
        approvals,
        grants=grants,
        claims=claims,
        snapshots=snapshots,
        data_root=data,
    )
    return Env(tmp_path, ws, log, leases, claims, tasks, approvals, grants,
               snapshots, dispatcher)


@pytest.fixture()
def env(tmp_path):
    e = _env(tmp_path)
    try:
        yield e
    finally:
        e.log.close()


def _request(env: Env, task, run, claim, *, call_id="c1", text="改后的内容\n"):
    args = {"path": "note.md", "content": text}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id="modify_text",
        target=env.ws / "note.md",
        args=args,
    )
    return request, args


def _admit_overwrite(env: Env, *, call_id="c1", text="改后的内容\n"):
    """走到「已批准、可派发」。"""
    task, run, claim = env.new_run()
    request, args = _request(env, task, run, claim, call_id=call_id, text=text)
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.kind == KIND_WAITING, decision
    env.approvals.decide(
        decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )
    return task, run, claim, request, args, decision


def _intents(env: Env, run_id: str) -> list[str]:
    return [
        event.payload["call_id"]
        for event in env.log.read_run(run_id)
        if event.type == "tool.intent"
    ]


def _types(env: Env, run_id: str) -> list[str]:
    return [event.type for event in env.log.read_run(run_id)]


# ================================================================ 疑点 1


def test_model_tool_surface_has_no_run_start():
    """模型可见工具 == exposed SPECS 投影，且没有「启动 Run」这类动作。

    模型能做的只有工具调用；`run.start` 是控制面（IPC）与 CLI 的方法，不在
    工具清单里。若将来有人加了一个能建 Run 的工具，这条会红——那时否决缓存的
    作用域假设（见 `permissions.ApprovalStore.rejected_for`）就失效了。
    """
    ids = {tool["name"] for tool in tool_manifest()}
    assert ids == {spec.tool_id for spec in SPECS.values() if spec.exposed}
    assert ids.isdisjoint(CONTROL_ONLY_NAMES), (
        f"模型工具清单里出现了控制面动作：{sorted(ids & CONTROL_ONLY_NAMES)}"
    )
    for spec in SPECS.values():
        assert spec.action in MODEL_ACTION_SET, (
            f"工具 {spec.tool_id} 的动作 {spec.action!r} 不在模型动作封闭集合里"
        )
    # 只读 / 写文件 / 执行命令 / 记忆读写——**没有任何一个**的动作语义是"新建 Run"。
    # 这份字面清单是**故意**的绊线：模型工具面每增加一个工具，都必须在这里显式
    # 过一次（F10 的记忆工具是 11 个里的 5 个）；不然"模型不能建 Run"这条前提
    # 就可能在无人察觉时被稀释。
    assert ids == {
        "list_files",
        "read_text",
        "create_report",
        "modify_text",
        "delete_file",
        "run_command",
        "memory.search",
        "memory.get",
        "memory.propose",
        "memory.confirm",
        "memory.forget",
    }


def test_tools_module_cannot_construct_runs():
    """结构钉子：`tools.py` 从 `.tasks` 只拿 `ExecutionPermit`，拿不到 TaskStore。

    这比"清单里现在没有 run_start"更强：就算有人往清单里塞一个新工具，只要
    工具实现拿不到 `TaskStore.start_run`，它也建不出 Run。
    """
    tree = ast.parse((REPO / "src" / "tianshu" / "tools.py").read_bytes())

    def _type_checking(test: ast.expr) -> bool:
        return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"

    runtime_imports: list[ast.ImportFrom] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _type_checking(node.test):
            # `if TYPE_CHECKING:` 块里的 import 运行时不存在（TaskStore 就在里面），
            # 不能当成"工具拿得到"；else 分支仍要走。
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.ImportFrom):
            runtime_imports.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    imported_from_tasks: list[str] = []
    imported_modules: set[str] = set()
    for node in runtime_imports:
        imported_modules.add(node.module or "")
        if node.module == "tasks":
            imported_from_tasks.extend(alias.name for alias in node.names)
    assert imported_from_tasks == [ExecutionPermit.__name__], (
        f"tools.py 从 tasks 导入了 {imported_from_tasks}；多拿一个 TaskStore 就"
        "等于工具面多了一条建 Run 的通道"
    )
    for forbidden in ("control", "cli", "daemon", "host"):
        assert f".{forbidden}" not in imported_modules and forbidden not in imported_modules, (
            f"tools.py 不应该依赖 {forbidden}（控制面/外壳不属工具能力）"
        )


def test_a_model_driven_run_never_starts_another_run(tmp_path, monkeypatch):
    """活的探针：模型提议最高危的可见工具也不会触发第二次 `start_run`。

    `run_command` 在任务未显式开启 exec 策略时被策略拒绝（`c4.exec_not_configured`）；
    无论它被拒还是被执行，它都只是工具调用，不是 Run 启动。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", ORIGINAL)
    data = tmp_path / "authority"
    data.mkdir()
    log = EventLog(tmp_path / "events.db")
    stack = build_stack(log, data_root=data)

    calls: list[str] = []
    real_start_run = stack.tasks.start_run

    def spy_start_run(*args, **kwargs):
        calls.append(str(kwargs.get("run_id") or (args[0] if args else "")))
        return real_start_run(*args, **kwargs)

    monkeypatch.setattr(stack.tasks, "start_run", spy_start_run)

    engine = AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        model=ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "run_command", {"argv": ["echo", "hi"]})],
                ),
                ModelResponse("end_turn", content="结束"),
            ]
        ),
        limits=Limits(4, 20_000),
    )
    task = stack.tasks.create_task("试试执行命令", [{"root_path": str(ws)}])
    result = engine.start(task.task_id)

    assert len(calls) == 1, f"一次 Run 只允许一次 start_run，实际：{calls}"
    rows = log.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    assert rows["n"] == 1, "模型提议的工具不得新建 Run"
    refused = [
        event.payload
        for event in log.read_run(result.run_id)
        if event.type == "tool.result"
    ]
    assert refused, "run_command 应留下 tool.result（拒绝也要留痕）"
    assert refused[0]["status"] == "rejected"
    assert refused[0]["output"]["rule_id"] == "c4.exec_not_configured"
    log.close()


# ================================================================ 疑点 2


def test_revoked_approval_cannot_be_consumed_again(env, monkeypatch):
    """路径 A：撤销之后，同一个 approval 不能再被派发消费。

    结论：**成立**。探针做三件事：
    - intent 已提交、文件还没打开时撤销（与执行抢同一把守卫，撤销先赢）；
    - 直接调 `ApprovalStore.consume` 必须抛 `ApprovalError`；
    - 再派发同一请求也不会有第二次 `tool.intent` / 第二次效果。
    """
    task, run, claim, request, args, decision = _admit_overwrite(env)

    original_begin = env.tasks.begin_execution

    def begin_after_revoke(data_root, run_id, resources):
        env.dispatcher.revoke(
            decision.approval_id,
            actor=HumanIdentity.local_cli(),
            reason="探针：intent 之后、打开文件之前撤销",
        )
        return original_begin(data_root, run_id, resources)

    monkeypatch.setattr(env.tasks, "begin_execution", begin_after_revoke)
    first = env.dispatcher.dispatch(request.request_id, claim)
    monkeypatch.setattr(env.tasks, "begin_execution", original_begin)

    assert first.status == "rejected", first
    assert first.effect_state == "none_applied"
    assert env.approvals.get(decision.approval_id).state == "revoked"
    assert (env.ws / "note.md").read_bytes() == ORIGINAL.encode()
    assert _intents(env, run.run_id) == ["c1"], "撤销之后不许再派发"

    # approval 层：条件 UPDATE 只认 approved → 已撤销的票不可消费
    with pytest.raises(ApprovalError) as consume_error:
        env.approvals.consume(
            decision.approval_id,
            action_digest=request.action_digest(),
            request_digest=request.digest(),
        )
    assert "不可消费" in str(consume_error.value)

    # 再派发同一条请求：仍然只有一次 intent、文件一个字节没动
    again = env.dispatcher.dispatch(request.request_id, claim)
    assert again.status == "rejected", again
    assert again.effect_state == "none_applied"
    assert _intents(env, run.run_id) == ["c1"]
    assert env.approvals.get(decision.approval_id).state == "revoked"
    assert (env.ws / "note.md").read_bytes() == ORIGINAL.encode()


def test_revoke_after_execution_started_is_refused(env, monkeypatch):
    """路径 B：越过 `execution.started` 之后撤销必须失败，不假装回滚。

    结论：**成立**。探针把派发记录推进到执行器在真正写之前会写的 `executing`，
    同时 `execution.started` 事件已经由真实的 `begin_execution` 写入；此时
    `revoke()` 看到 dispatch 状态不是 intent，明确报错（而不是把已执行的操作
    "撤销"成一句空话）。
    """
    task, run, claim, request, args, decision = _admit_overwrite(env)

    real_execute = env.dispatcher._executor.execute

    def execute_then_get_fenced(admitted, permit):
        # 真实执行器在写文件前会 `mark_dispatch(..., "executing")`；这里复制
        # 那一步，然后停在"已越过 execution.started、结论还没落库"的窗口。
        # 抛 ToolArgumentError（守卫内复核类错误）而不是裸 Exception：后者会被
        # 记成 unknown（"可能写了一半"），那不是这个探针要观察的状态。
        env.dispatcher.mark_dispatch(admitted.request.run_id, admitted.request.call_id,
                                     "executing")
        raise ToolArgumentError("探针：停在 execution.started 之后、写入之前")

    monkeypatch.setattr(env.dispatcher._executor, "execute", execute_then_get_fenced)
    result = env.dispatcher.dispatch(request.request_id, claim)
    monkeypatch.setattr(env.dispatcher._executor, "execute", real_execute)

    # dispatch 的守卫内复核把它记成"确定没写"（一个字节没动），但派发记录
    # 已经越过 intent —— 这是撤销必须拒绝的状态。
    assert result.effect_state == "none_applied"
    events = _types(env, run.run_id)
    assert "execution.started" in events, "探针的前提：确实越过 execution.started"
    row = env.dispatcher.dispatch_record(run.run_id, request.call_id)
    assert row.state == "executing", row.state

    with pytest.raises(ApprovalError) as error:
        env.dispatcher.revoke(decision.approval_id, actor=HumanIdentity.local_cli())
    assert "已经开始执行" in str(error.value)
    assert env.approvals.get(decision.approval_id).state == "consumed", (
        "撤销失败之后审批仍是已消费——不许把状态改成'已撤销'来假装成功"
    )
    assert (env.ws / "note.md").read_bytes() == ORIGINAL.encode()


def test_revocation_allows_a_fresh_approval_in_the_same_run(env, monkeypatch):
    """路径 C：撤销之后，同一个 Run 里重新发起这个动作 → 新审批 → 能执行。

    结论：**成立**。撤销作废的是"那一张票"，不是"这件事永久不能做"。
    """
    task, run, claim, request, args, decision = _admit_overwrite(env)

    original_begin = env.tasks.begin_execution

    def begin_after_revoke(data_root, run_id, resources):
        env.dispatcher.revoke(decision.approval_id, actor=HumanIdentity.local_cli())
        return original_begin(data_root, run_id, resources)

    monkeypatch.setattr(env.tasks, "begin_execution", begin_after_revoke)
    env.dispatcher.dispatch(request.request_id, claim)
    monkeypatch.setattr(env.tasks, "begin_execution", original_begin)
    assert env.approvals.get(decision.approval_id).state == "revoked"

    # 同 Run 重新发起（新 call_id → 新请求号；动作内容没变，但旧票已作废，
    # 会重新走审批，而不是复用被撤销的那张）
    request2, args2 = _request(env, task, run, claim, call_id="c2", text="第二次\n")
    second = env.dispatcher.prepare(request2, claim, args=args2)
    assert second.kind == KIND_WAITING, second
    assert second.approval_id != decision.approval_id
    env.approvals.decide(
        second.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )
    done = env.dispatcher.dispatch(request2.request_id, claim)

    assert done.status == "succeeded", done
    assert done.effect_state == "applied"
    assert (env.ws / "note.md").read_bytes() == "第二次\n".encode()
    assert env.approvals.get(second.approval_id).state == "consumed"