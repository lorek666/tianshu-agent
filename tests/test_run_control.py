"""M2 · PR C · Task 5：持久游标、用户控制与目标变更。

这一组测试要钉住的是"停下来之后还能不能算清楚账"：

1. 暂停只在安全点生效——正在执行的那一次副作用照常完成，不许假装冻结；
2. 批准 ≠ 执行：暂停中的 Run 即使拿到了批准也不许动文件，必须显式 resume；
3. resume 不重跑已完成的调用，也不重复付模型的钱（含"结果已落库、游标还没更新"的窗口）；
4. 改目标让旧 Run 的**待批请求和执行资格**同时失效，历史保留；
5. 活着的 owner 持续续租时不换 epoch，批准不会因为一轮 TTL 流逝就作废；
6. 进程死掉被别人接管后必须重新批一次，且只批一次就能跑完。

第 5、6 条用注入时钟推进时间：真实等 30 秒 TTL 会让测试又慢又脆，
而 TTL 逻辑本身与墙上时钟无关。注入必须贯穿所有 store——只换一个就等于
一半组件用假时钟、一半用真时钟，那样测出来的是幻觉。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tianshu.dispatcher import Dispatcher
from tianshu.errors import RunFenced, TianshuError
from tianshu.events import EventLog, canonical_bytes
from tianshu.jobs import JobClaim, JobLost
from tianshu.leases import ProcessIdentity
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import HumanIdentity
from tianshu.tasks import (
    RUN_PAUSED,
    RUN_RUNNING,
    RUN_WAITING_APPROVAL,
    RunCursor,
)

ORIGINAL = "原始内容\n"


def _types_of(log: EventLog, run_id: str) -> list[str]:
    return [e.type for e in log.read_run(run_id)]


# ---------------------------------------------------------------- 夹具


class Clock:
    """可推进的逻辑时钟。"""

    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


class PausingDispatcher(Dispatcher):
    """在"已获准、文件还没动"的那一刻插入一次用户操作。

    真实场景是用户在一次文件写入正在发生时按了暂停：那一次照常完成，之后不再
    开始新的。这里用同一个入口复现，不伪造状态、不冻结线程。
    """

    hook = None
    after_hook = None

    def dispatch(self, request_id, claim):
        stored = self.get_request(request_id)
        assert stored is not None
        if self.hook is not None:
            self.hook(stored.request.run_id, stored.request.call_id)
        result = super().dispatch(request_id, claim)
        if self.after_hook is not None:
            self.after_hook(stored.request.run_id, stored.request.call_id)
        return result


@dataclass
class Env:
    root: Path
    ws: Path
    log: EventLog
    clock: Clock
    stack: object
    dispatcher: PausingDispatcher

    def engine(self, responses=None, *, limits=None, model=None) -> AgentLoop:
        return AgentLoop(
            tasks=self.stack.tasks,
            leases=self.stack.leases,
            claims=self.stack.claims,
            dispatcher=self.dispatcher,
            approvals=self.stack.approvals,
            snapshots=self.stack.snapshots,
            model=model or ScriptedModel(responses or []),
            limits=limits or Limits(8, 20_000),
        )

    def task(self, goal="整理目录里的笔记", roots=None):
        roots = roots if roots is not None else [str(self.ws)]
        return self.stack.tasks.create_task(goal, roots)

    def events(self, run_id: str) -> list[str]:
        return [e.type for e in self.log.read_run(run_id)]

    def count(self, run_id: str, event_type: str) -> int:
        return sum(1 for e in self.log.read_run(run_id) if e.type == event_type)

    def intents(self, run_id: str) -> list[str]:
        return [
            e.payload["call_id"]
            for e in self.log.read_run(run_id)
            if e.type == "tool.intent"
        ]

    def claim_of(self, run_id: str) -> JobClaim:
        """从作业行重建当前 claim（用于"绕过主循环直接派发"的反例）。"""
        job = self.stack.claims.job_for_run(run_id)
        assert job is not None and job.owner_instance_id is not None
        return JobClaim(
            job_id=job.job_id,
            run_id=run_id,
            owner_instance_id=job.owner_instance_id,
            owner_pid=job.owner_pid,
            claim_epoch=job.claim_epoch,
        )


def _env(tmp_path: Path) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text(ORIGINAL, encoding="utf-8")
    data = tmp_path / "authority"
    data.mkdir()
    clock = Clock()
    log = EventLog(tmp_path / "events.db")
    stack = build_stack(
        log, data_root=data, clock=clock, dispatcher_cls=PausingDispatcher
    )
    return Env(
        root=tmp_path,
        ws=ws,
        log=log,
        clock=clock,
        stack=stack,
        dispatcher=stack.dispatcher,
    )


@pytest.fixture()
def env(tmp_path):
    e = _env(tmp_path)
    yield e
    e.log.close()


def _actor() -> HumanIdentity:
    return HumanIdentity.local_cli()


def _write_turn(calls: list[ToolCall]) -> ModelResponse:
    return ModelResponse("tool_use", tool_calls=calls)


def _end(text: str = "完成") -> ModelResponse:
    return ModelResponse("end_turn", content=text)


def _create(name: str, content: str, call_id: str) -> ToolCall:
    return ToolCall(call_id, "create_report", {"name": name, "content": content})


def _overwrite(path: str, content: str, call_id: str) -> ToolCall:
    return ToolCall(call_id, "modify_text", {"path": path, "content": content})


# ---------------------------------------------------------------- 1. 暂停


def test_pause_waits_for_current_effect(env):
    """暂停请求落在一次写入中间：那一次必须完成并被记账，之后才停。

    反例是"暂停 = 立即冻结"：把控制请求直接写成 PAUSED，就会对外宣称已经停稳，
    而文件可能刚被改到一半。
    """
    engine = env.engine([
        _write_turn([_create("a.md", "甲", "c1"), _create("b.md", "乙", "c2")]),
        _end(),
    ])
    task = env.task()

    def hook(run_id: str, call_id: str) -> None:
        # 第一次派发正在飞的时候用户按了暂停
        env.stack.tasks.request_pause(run_id)

    env.dispatcher.hook = hook
    result = engine.start(task.task_id)
    env.dispatcher.hook = None

    # 效果已经发生：a.md 写了，账上也认了
    assert (env.ws / "a.md").read_text(encoding="utf-8") == "甲"
    assert result.effect_state == "applied"
    assert result.n_applied == 1
    # 但 Run 停在暂停，而不是"成功"或"失败"
    assert result.state == RUN_PAUSED
    assert result.outcome == "", "暂停不是结论，不能记成 succeeded/failed"
    assert not (env.ws / "b.md").exists(), "暂停之后不得开始新的副作用"
    assert env.intents(result.run_id) == ["c1"]

    # 请求已被确认并清除：否则 resume 会立刻又停下
    assert env.stack.tasks.get_run(result.run_id).control_requested is None
    # 暂停交还工作区，别人可以用这个目录
    assert env.stack.leases.live_paths(result.run_id) == []

    resumed = env.engine([_end()]).resume_run(result.run_id)
    assert resumed.state == "succeeded", resumed.error
    assert (env.ws / "b.md").read_text(encoding="utf-8") == "乙"
    assert env.intents(result.run_id) == ["c1", "c2"]


def test_cancel_during_model_turn_bills_cost_and_drops_proposals(env):
    """模型流中取消：这笔钱已经花了，提案一个都不许派发。

    "取消"不等于"没发生过"——模型调用是真的发出去了，费用必须入账。
    """
    task = env.task()
    cancelled = {"done": False}

    class CancellingModel(ScriptedModel):
        def complete(self, messages, tools):
            active = env.stack.tasks.get_task(task.task_id).active_run_id
            if active and not cancelled["done"]:
                cancelled["done"] = True
                env.stack.tasks.request_cancel(active)
            return super().complete(messages, tools)

    engine = env.engine(
        model=CancellingModel([
            ModelResponse(
                "tool_use",
                tool_calls=[_create("a.md", "甲", "c1")],
                usage={"input_tokens": 100, "output_tokens": 20},
            ),
            _end(),
        ])
    )
    result = engine.start(task.task_id)

    assert result.state == "cancelled"
    assert result.tokens == 120, "已经发生的模型费用仍要入账"
    assert not (env.ws / "a.md").exists(), "取消后不得派发未提交的工具提议"
    assert env.intents(result.run_id) == []
    assert env.count(result.run_id, "model.result") == 1


# ---------------------------------------------------------------- 2. 批准 ≠ 执行


def test_approved_while_paused_does_not_execute(env):
    """拿到批准还不算执行；暂停中的 Run 必须由人显式 resume。"""
    engine = env.engine([_write_turn([_overwrite("note.md", "新内容", "c1")]), _end()])
    task = env.task()
    result = engine.start(task.task_id)

    assert result.state == RUN_WAITING_APPROVAL
    assert result.approval_ids, "覆盖类操作必须留下待批请求"
    approval_id = result.approval_ids[0]

    # 人先暂停，再批准
    env.stack.tasks.request_pause(result.run_id)
    assert env.stack.tasks.get_run(result.run_id).state == RUN_PAUSED
    env.stack.approvals.decide(approval_id, decision="approved", actor=_actor())
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL
    assert env.intents(result.run_id) == []

    # 就算有人绕过主循环直接派发，执行侧也必须拒绝——这不是"调用方自觉"
    approval = env.stack.approvals.get(approval_id)
    refused = env.dispatcher.dispatch(approval.request_id, env.claim_of(result.run_id))
    assert refused.status == "rejected"
    assert refused.output["rule_id"] == "dispatch.not_running"
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL

    resumed = env.engine([_end()]).resume_run(result.run_id)
    assert resumed.state == "succeeded", resumed.error
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "新内容"
    assert env.intents(result.run_id) == ["c1"], "批准一次只能执行一次"


# ---------------------------------------------------------------- 3. 不重跑


def test_resume_never_repeats_completed_calls(env):
    """resume 只补做没做完的调用，已完成的既不重派也不重新问模型。"""
    engine = env.engine([
        _write_turn([_create("a.md", "甲", "c1"), _create("b.md", "乙", "c2")]),
        _end(),
    ])
    task = env.task()

    def hook(run_id: str, call_id: str) -> None:
        if call_id == "c1":
            env.stack.tasks.request_pause(run_id)

    env.dispatcher.hook = hook
    result = engine.start(task.task_id)
    env.dispatcher.hook = None
    assert result.state == RUN_PAUSED

    resumed = env.engine([_end()]).resume_run(result.run_id)
    assert resumed.state == "succeeded", resumed.error

    intents = env.intents(result.run_id)
    assert intents.count("c1") == 1, "已完成的调用不得重派"
    assert intents.count("c2") == 1
    assert (env.ws / "a.md").read_text(encoding="utf-8") == "甲"
    assert (env.ws / "b.md").read_text(encoding="utf-8") == "乙"
    # 第一轮的提案从游标里恢复，没有再问一次模型
    assert env.count(result.run_id, "model.intent") == 2, "只应有第一轮与收尾那一轮"
    # 跨恢复重建的消息序列必须与原始执行逐字节一致，否则"能重放"就是假的
    from tianshu.replay import replay as replay_run

    report = replay_run(env.log, result.run_id)
    assert report.ok, report.problems


def test_resume_recovers_when_cursor_lags_behind_events(env):
    """结果已落库、游标还是旧的：resume 必须认出"这次调用做过了"。

    工具结果由派发层提交，游标由主循环提交，两者不在一个事务里，所以存在
    "结果写了、游标没写"的真实窗口。历史结果以事件为准，游标只用于知道
    "下一步该做什么"——把顺序搞反，就是崩溃之后重复执行一次写入。
    """
    engine = env.engine([
        _write_turn([_create("a.md", "甲", "c1"), _create("b.md", "乙", "c2")]),
        _end(),
    ])
    task = env.task()

    def hook(run_id: str, call_id: str) -> None:
        if call_id == "c1":
            env.stack.tasks.request_pause(run_id)

    env.dispatcher.hook = hook
    result = engine.start(task.task_id)
    env.dispatcher.hook = None
    assert result.state == RUN_PAUSED
    assert (env.ws / "a.md").exists()

    # 把游标退回到"c1 还没做完"的样子（模拟崩溃窗口里的旧快照）
    stored = env.stack.tasks.load_cursor(result.run_id)
    assert stored is not None
    stale = stored[0]
    stale.completed_call_ids = []
    stale.pending_proposals = [
        {
            "call_id": "c1",
            "tool_id": "create_report",
            "args": {"name": "a.md", "content": "甲"},
            "kind": "write",
            "request_id": stale.pending_proposals[0].get("request_id"),
            "approval_id": None,
            "attempt_id": 1,
        },
        *stale.pending_proposals,
    ]
    env.log.conn.execute(
        "UPDATE run_cursors SET payload=? WHERE run_id=?",
        (canonical_bytes(stale.to_dict()).decode("utf-8"), result.run_id),
    )

    resumed = env.engine([_end()]).resume_run(result.run_id)
    assert resumed.state == "succeeded", resumed.error
    assert env.intents(result.run_id).count("c1") == 1, "不得因为游标落后而重派"
    assert (env.ws / "a.md").read_text(encoding="utf-8") == "甲", "内容没有被写第二遍"
    assert (env.ws / "b.md").read_text(encoding="utf-8") == "乙"


# ---------------------------------------------------------------- 4. 改目标


def test_goal_change_fences_pending_and_prepared_calls(env):
    """用户改主意：待批请求、已批准但未派发的动作、整个 Run 一起失效。"""
    engine = env.engine([_write_turn([_overwrite("note.md", "旧目标的改法", "c1")])])
    task = env.task("把 note.md 改一版")
    result = engine.start(task.task_id)

    assert result.state == RUN_WAITING_APPROVAL
    approval_id = result.approval_ids[0]
    env.stack.approvals.decide(approval_id, decision="approved", actor=_actor())
    # 已批准但还没派发 —— 正是"prepared call"的窗口
    prepared_claim = env.claim_of(result.run_id)

    revised = env.stack.tasks.revise_task(task.task_id, goal="换个目标：只读总结")
    assert revised.goal_revision == task.goal_revision + 1
    assert revised.lease_epoch == task.lease_epoch + 1

    assert env.stack.tasks.get_run(result.run_id).state == "fenced"
    assert env.stack.approvals.get(approval_id).state == "superseded", (
        "旧目标的批准必须作废，不能暗中复用"
    )
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL

    # 旧 Run 不能再被 resume，也不能再派发
    with pytest.raises(RunFenced):
        env.engine([_end()]).resume_run(result.run_id)
    refused = env.dispatcher.dispatch(
        env.stack.approvals.get(approval_id).request_id, prepared_claim
    )
    assert refused.status == "rejected"
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL
    # 历史保留：旧 Run 与它的失败原因都还在
    assert RUN_FENCED_EVENT in env.events(result.run_id)


RUN_FENCED_EVENT = "run.fenced"


def test_goal_change_requires_new_run_and_preserves_history(env):
    """改目标之后不能"接着跑旧 Run"：新目标必须开新 Run，旧记录不改写。"""
    engine = env.engine([_write_turn([_create("a.md", "甲", "c1")]), _end()])
    task = env.task("写 a.md")
    first = engine.start(task.task_id)
    assert first.state == "succeeded"

    env.stack.tasks.revise_task(task.task_id, goal="写 b.md")
    old = env.stack.tasks.get_run(first.run_id)
    assert old.state == "succeeded", "旧 Run 的结论不该被改目标抹掉"

    second = env.engine([_write_turn([_create("b.md", "乙", "c9")]), _end()]).start(
        task.task_id
    )
    assert second.run_id != first.run_id
    assert (env.ws / "b.md").read_text(encoding="utf-8") == "乙"


def test_revise_bindings_does_not_widen_old_authorization(env):
    """改绑定只影响之后的新 Run，不扩张旧授权。"""
    other = env.root / "other"
    other.mkdir()
    (other / "note.md").write_text("另一个目录\n", encoding="utf-8")
    task = env.task("整理", [str(env.ws)])
    env.stack.tasks.revise_task(task.task_id, bindings=[str(other)])

    fresh = env.stack.tasks.get_task(task.task_id)
    assert [b.root_path for b in fresh.workspaces] == [str(other.resolve())]

    run = env.engine([_write_turn([_create("x.md", "内容", "c1")]), _end()]).start(
        task.task_id
    )
    assert run.state == "succeeded", run.error
    assert (other / "x.md").exists(), "新 Run 应当写在新的绑定目录里"
    assert not (env.ws / "x.md").exists()


# ---------------------------------------------------------------- 5/6. epoch


def test_live_approval_resume_keeps_epoch(env):
    """活的 owner 持续续租时 resume 不换 epoch，批准不会一轮 TTL 就作废。

    反例是"每次 resume 都重新认领"：那样每 30 秒就要重新批一次，
    用户看到的是"批了还在批"。
    """
    engine = env.engine([_write_turn([_overwrite("note.md", "新内容", "c1")]), _end()])
    task = env.task()
    result = engine.start(task.task_id)
    assert result.state == RUN_WAITING_APPROVAL

    epoch_before = env.stack.claims.job_for_run(result.run_id).claim_epoch
    claim = engine.current_claim(result.run_id)
    assert claim is not None, "等待审批期间 owner 必须还握着 claim"

    # 模拟 Task 6 的心跳循环：跨过 30 秒租约 TTL，但远不到 10 分钟审批 TTL
    for _ in range(3):
        env.clock.advance(20_000)
        engine.heartbeat_once(claim)
    env.stack.approvals.decide(
        result.approval_ids[0], decision="approved", actor=_actor()
    )

    resumed = engine.resume_run(result.run_id)
    assert resumed.state == "succeeded", resumed.error
    assert env.stack.claims.job_for_run(result.run_id).claim_epoch == epoch_before
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "新内容"
    assert env.intents(result.run_id) == ["c1"]


def test_reclaimed_approval_can_complete_after_one_reapproval(env):
    """进程死掉被别人接管：旧批准作废，重新批一次即可跑完，且只跑一次。"""
    engine = env.engine([_write_turn([_overwrite("note.md", "接管后写入", "c1")]), _end()])
    task = env.task()
    result = engine.start(task.task_id)
    assert result.state == RUN_WAITING_APPROVAL
    old_approval = result.approval_ids[0]
    env.stack.approvals.decide(old_approval, decision="approved", actor=_actor())
    old_epoch = env.stack.claims.job_for_run(result.run_id).claim_epoch

    # 老 owner 再没续过心跳：租约与作业 deadline 双双过期（>30s，仍 << 10min）
    env.clock.advance(60_000)

    takers = build_stack(
        env.log,
        data_root=env.root / "authority",
        clock=env.clock,
        identity=ProcessIdentity.mint(),
        dispatcher_cls=PausingDispatcher,
    )
    taker = AgentLoop(
        tasks=takers.tasks,
        leases=takers.leases,
        claims=takers.claims,
        dispatcher=takers.dispatcher,
        approvals=takers.approvals,
        snapshots=takers.snapshots,
        model=ScriptedModel([_end()]),
        limits=Limits(8, 20_000),
    )

    parked = taker.resume_run(result.run_id)
    assert parked.state == RUN_WAITING_APPROVAL
    assert takers.claims.job_for_run(result.run_id).claim_epoch > old_epoch, (
        "接管必须推进 claim_epoch"
    )
    # 旧批准被显式作废（不能静默换成"新 epoch 也能用"）
    assert env.stack.approvals.get(old_approval).state == "superseded"
    fresh = takers.approvals.pending(run_id=result.run_id)
    assert len(fresh) == 1, "一次接管只应留下一条重新审批"

    takers.approvals.decide(fresh[0].approval_id, decision="approved", actor=_actor())
    done = taker.resume_run(result.run_id)
    assert done.state == "succeeded", done.error
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "接管后写入"
    assert env.intents(result.run_id) == ["c1"], "接管不得导致重复执行"


def test_old_owner_heartbeat_fails_after_takeover(env):
    """旧 owner 的心跳在接管之后必须失败，不能继续续一个不属于它的作业。"""
    engine = env.engine([_write_turn([_overwrite("note.md", "x", "c1")])])
    task = env.task()
    result = engine.start(task.task_id)
    claim = engine.current_claim(result.run_id)
    assert claim is not None

    env.clock.advance(60_000)
    takers = build_stack(
        env.log,
        data_root=env.root / "authority",
        clock=env.clock,
        identity=ProcessIdentity.mint(),
    )
    takers.claims.claim(takers.claims.job_for_run(result.run_id).job_id)

    with pytest.raises(JobLost):
        engine.heartbeat_once(claim)


def test_cursor_round_trip_keeps_budget_and_completed_calls(env):
    """游标里必须留下预算与已完成调用：resume 不清空预算、不重算已完成。"""
    engine = env.engine([
        ModelResponse(
            "tool_use",
            tool_calls=[_create("a.md", "甲", "c1")],
            usage={"input_tokens": 30, "output_tokens": 10},
        ),
        _end(),
    ])
    task = env.task()
    result = engine.start(task.task_id)
    assert result.state == "succeeded"

    stored = env.stack.tasks.load_cursor(result.run_id)
    assert stored is not None
    cursor: RunCursor = stored[0]
    assert cursor.completed_call_ids == ["c1"]
    assert cursor.budget_ledger["tokens"] == 40
    assert cursor.budget_ledger["turns"] == 2
    assert stored[1]["goal_revision"] == result.goal_revision
    assert stored[1]["claim_epoch"] >= 1


def test_transition_rejects_terminal_outcome_states(env):
    """终态只能走 finish_run：那里还要写 outcome 与 effect_state 两个维度。"""
    task = env.task()
    run = env.stack.tasks.start_run(task.task_id)
    with pytest.raises(TianshuError):
        env.stack.tasks.transition(run.run_id, "succeeded")
    assert env.stack.tasks.transition(run.run_id, RUN_RUNNING).state == RUN_RUNNING
    # 执行中的 Run 只记录请求：真的停下要等执行器的安全点
    pending = env.stack.tasks.request_pause(run.run_id)
    assert pending.state == RUN_RUNNING
    assert pending.control_requested == "pause"

# ---------------------------------------------------------------- CLI 面


def _cli(*argv: str) -> tuple[int, str]:
    """直接调库入口跑 CLI：不起子进程，但走的是同一条解析 + 装配路径。"""
    import contextlib
    import io

    from tianshu.cli import main

    out, err = io.StringIO(), io.StringIO()
    code = 2
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue() + err.getvalue()


def test_cli_task_create_start_status_and_cancel(env, tmp_path):
    """CLI 的 task / run / approvals 三条控制路径能真正驱动同一个 Run。"""
    import json as _json

    db = str(env.root / "cli.db")

    code, out = _cli(
        "task", "create", "--goal", "整理目录", "--workspace", str(env.ws),
        "--db", db, "--json",
    )
    assert code == 0, out
    task_id = _json.loads(out)["task"]["task_id"]

    code, out = _cli(
        "run", "start", "--task-id", task_id, "--out", "a.md", "--db", db, "--json"
    )
    assert code == 0, out
    run_id = _json.loads(out)["run"]["run_id"]
    assert _json.loads(out)["run"]["state"] == "succeeded"

    code, out = _cli("run", "status", "--run-id", run_id, "--db", db, "--json")
    assert code == 0, out
    assert _json.loads(out)["state"] == "succeeded"

    code, out = _cli("task", "show", "--task-id", task_id, "--db", db, "--json")
    assert code == 0, out
    payload = _json.loads(out)
    assert payload["task"]["goal_revision"] == 1
    assert [r["run_id"] for r in payload["runs"]] == [run_id]

    code, out = _cli("approvals", "list", "--db", db, "--json")
    assert code == 0
    assert _json.loads(out) == []

    # 已结束的 Run 再取消：请求是幂等的，不会把终态改写
    code, out = _cli("run", "cancel", "--run-id", run_id, "--db", db)
    assert code == 0, out


def test_cli_revise_fences_old_run(env, tmp_path):
    """`task revise` 之后旧 Run 立刻不可用，历史仍可查。"""
    import json as _json

    db = str(env.root / "cli2.db")
    _, out = _cli("task", "create", "--goal", "旧目标", "--workspace", str(env.ws),
                  "--db", db, "--json")
    task_id = _json.loads(out)["task"]["task_id"]
    _, out = _cli("run", "start", "--task-id", task_id, "--out", "a.md",
                  "--db", db, "--json")
    run_id = _json.loads(out)["run"]["run_id"]

    code, out = _cli("task", "revise", "--task-id", task_id, "--goal", "新目标",
                     "--db", db, "--json")
    assert code == 0, out
    revised = _json.loads(out)["task"]
    assert revised["goal_revision"] == 2
    assert revised["lease_epoch"] == 2

    # 旧 Run 在新目标下不能 resume
    code, out = _cli("run", "resume", "--run-id", run_id, "--db", db)
    assert code != 0


# ---------------------------------------------------------------- 执行中改目标


def test_goal_change_before_dispatch_fences_the_call(env):
    """改目标抢在派发之前：那次写入被拒绝，文件一个字节都没动。"""
    engine = env.engine([_write_turn([_create("a.md", "甲", "c1")]), _end()])
    task = env.task("旧目标：写 a.md")

    def hook(run_id: str, call_id: str) -> None:
        env.stack.tasks.revise_task(task.task_id, goal="新目标：别写了")

    env.dispatcher.hook = hook
    result = engine.start(task.task_id)
    env.dispatcher.hook = None

    assert not (env.ws / "a.md").exists(), "已 fence 的 Run 不该写出文件"
    assert env.stack.tasks.get_run(result.run_id).state == "fenced"
    assert result.outcome == "", "被 fence 的 Run 不能报出结论"
    assert result.effect_state == "none_applied"


def test_goal_change_after_effect_keeps_effect_but_never_reports_success(env):
    """改目标发生在副作用之后：修改保留，但 Run 不得报成功。

    "取消/改目标不撤回已发生的修改"这句话必须两个方向都成立：既不假装没写，
    也不假装写成功了——后者会让用户以为旧目标已经完成。
    """
    engine = env.engine([_write_turn([_create("a.md", "甲", "c1")]), _end()])
    task = env.task("旧目标：写 a.md")

    def after(run_id: str, call_id: str) -> None:
        env.stack.tasks.revise_task(task.task_id, goal="新目标")

    env.dispatcher.after_hook = after
    result = engine.start(task.task_id)
    env.dispatcher.after_hook = None

    assert (env.ws / "a.md").read_text(encoding="utf-8") == "甲", "已发生的修改保留"
    assert env.stack.tasks.get_run(result.run_id).state == "fenced"
    assert result.outcome == "", "不许把被 fence 的 Run 记成 succeeded"
    assert result.effect_state == "applied", "效果要如实记账"


# ---------------------------------------------------------------- 其它不变量


def test_pause_and_wait_never_append_run_end(env):
    """没结束的 Run 不许有 run.end：那是"有结论"的记号。"""
    engine = env.engine([_write_turn([_overwrite("note.md", "新内容", "c1")]), _end()])
    task = env.task()
    waiting = engine.start(task.task_id)
    assert waiting.state == RUN_WAITING_APPROVAL
    assert "run.end" not in env.events(waiting.run_id), "等待审批不是结论"
    assert "run.finish" not in env.events(waiting.run_id)

    env.stack.tasks.request_pause(waiting.run_id)
    assert "run.end" not in env.events(waiting.run_id), "暂停不是结论"

    env.stack.approvals.decide(
        waiting.approval_ids[0], decision="approved", actor=_actor()
    )
    done = env.engine([_end()]).resume_run(waiting.run_id)
    assert done.state == "succeeded"
    assert "run.end" in env.events(waiting.run_id)
    assert env.events(waiting.run_id).count("run.end") == 1, "结论只能有一个"


def test_cancel_supersedes_pending_approvals(env):
    """取消之后待批队列必须清空，不能留下"批了也不会执行"的僵尸请求。"""
    engine = env.engine([_write_turn([_overwrite("note.md", "新内容", "c1")])])
    task = env.task()
    waiting = engine.start(task.task_id)
    approval_id = waiting.approval_ids[0]
    assert env.stack.approvals.pending(run_id=waiting.run_id)

    cancelled = env.stack.tasks.request_cancel(waiting.run_id)
    assert cancelled.state == "cancelled"
    assert cancelled.outcome == "cancelled"
    assert env.stack.approvals.get(approval_id).state == "superseded"
    assert env.stack.approvals.pending(run_id=waiting.run_id) == []
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL
    # 已终结的 Run 不能再 resume
    with pytest.raises(TianshuError):
        env.engine([_end()]).resume_run(waiting.run_id)


def test_chat_only_task_cannot_touch_files(env):
    """0 目录任务合法，但绝不能因此获得 cwd 权限。"""
    task = env.task("只聊天，不碰文件", roots=[])
    assert env.stack.tasks.get_task(task.task_id).workspaces == []
    engine = env.engine([_write_turn([_create("a.md", "甲", "c1")]), _end()])
    result = engine.start(task.task_id)

    assert result.state == "succeeded"
    assert result.effect_state == "none_applied"
    assert not (env.ws / "a.md").exists()
    assert env.intents(result.run_id) == []


# ---------------------------------------------------------------- 真崩溃 + 接管


def test_crashed_dispatch_becomes_reconciling_then_resume_after_recover(tmp_path):
    """进程在 intent 之后、写入之前被硬杀：接管方既不能重派，也不能当成功。

    这是"重启不丢已完成工具、不重复副作用"的核心场景，必须用**真子进程**
    硬退来造：进程内构造的状态无法证明"崩溃后留下的持久状态到底是什么样"。
    """
    import os
    import subprocess
    import sys

    from tianshu.events import EventLog
    from tianshu.leases import ProcessIdentity
    from tianshu.recovery import recover_run

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("内容\n", encoding="utf-8")
    db = tmp_path / "crash.db"

    src = str(Path(__file__).resolve().parents[1] / "src")
    child = f"""
import sys
sys.path.insert(0, {src!r})
from tianshu.cli import main
sys.exit(main(["run", "--workspace", {str(ws)!r}, "--task", "写报告",
               "--out", "r.md", "--model", "scripted",
               "--db", {str(db)!r}]))
"""
    env = dict(os.environ)
    env["TIANSHU_FAILPOINT"] = "after_intent"
    proc = subprocess.run(
        [sys.executable, "-c", child], env=env, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 77, f"应当在 after_intent 硬退，实际 {proc.returncode}"

    log = EventLog(db, blob_dir=db.parent / "blobs")
    try:
        row = log.conn.execute(
            "SELECT run_id FROM events WHERE type='run.start' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        run_id = row["run_id"]
        events = [e.type for e in log.read_run(run_id)]
        assert events.count("tool.intent") == 1, "写入的 intent 已提交"
        writes = [
            e for e in log.read_run(run_id)
            if e.type == "tool.result" and e.payload.get("tool_id") == "create_report"
        ]
        assert writes == [], "崩溃点在写入之前：不该有写入的结果"
        assert not (ws / "r.md").exists()

        stack = build_stack(
            log,
            data_root=db.parent,
            identity=ProcessIdentity.mint(),
        )
        engine = AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            model=ScriptedModel([_end()]),
            limits=Limits(8, 20_000),
        )
        # 死掉的 owner 必须能被接手：只按 Run 状态判断的话，崩溃的 Run 停在
        # RUNNING 就永远没人能接。
        first = engine.resume_run(run_id)
        assert first.state == "reconciling"
        assert "recover" in (first.error or "")
        assert _types_of(log, run_id).count("tool.intent") == 1, "不许重派"

        report = recover_run(log, run_id, ws, stack.snapshots)
        assert report.none_applied == ["c2"], "写前就崩了，应当判为未执行"

        second = engine.resume_run(run_id)
        assert second.state in ("succeeded", "failed"), second.error
        # 今天的选择是"核查为未执行 = 该调用已经有结果"，于是不自动重试，
        # 由模型看到失败结果后自己决定要不要换一个 call_id 再来一次。
        # 所以 intent 仍然只有一条；真要重试会带新的 call_id。
        assert _types_of(log, run_id).count("tool.intent") == 1
    finally:
        log.close()





def test_read_result_survives_cursor_lag_without_duplicate_message(env):
    """读操作的结果已落库、游标还没更新：恢复后不能补出第二条工具消息。

    补出两条的话，下一轮 `messages_hash` 就和 replay 重建的序列对不上——
    "能重放"这句话立刻变成假的。读操作不会有副作用，可以直接复用旧结果。
    """
    engine = env.engine([
        _write_turn([
            ToolCall("r1", "read_text", {"path": "note.md"}),
            _create("a.md", "甲", "c1"),
        ]),
        _end(),
    ])
    task = env.task()

    def hook(run_id: str, call_id: str) -> None:
        if call_id == "c1":
            env.stack.tasks.request_pause(run_id)

    env.dispatcher.hook = hook
    result = engine.start(task.task_id)
    env.dispatcher.hook = None
    assert result.state == RUN_PAUSED

    # 把游标退回到"刚提交完模型结果、两个调用都还没做"的样子：
    # 这正是"读操作的结果已落库、游标没跟上"的崩溃窗口。
    #
    # 只可能是这个形状：消息、已完成调用、待执行提案同一次提交，所以游标落后
    # 的那一步一定还在 pending 里，不会出现"结果没有、提案也没了"的中间态。
    stored = env.stack.tasks.load_cursor(result.run_id)
    stale = stored[0]
    assert [m.get("role") for m in stale.messages][:3] == [
        "system", "user", "assistant"
    ]
    stale.messages = stale.messages[:3]
    stale.completed_call_ids = []
    stale.pending_proposals = [
        {
            "call_id": "r1",
            "tool_id": "read_text",
            "args": {"path": "note.md"},
            "kind": "read",
            "request_id": None,
            "approval_id": None,
            "attempt_id": 1,
        },
        {
            "call_id": "c1",
            "tool_id": "create_report",
            "args": {"name": "a.md", "content": "甲"},
            "kind": "write",
            "request_id": None,
            "approval_id": None,
            "attempt_id": 1,
        },
    ]
    env.log.conn.execute(
        "UPDATE run_cursors SET payload=? WHERE run_id=?",
        (canonical_bytes(stale.to_dict()).decode("utf-8"), result.run_id),
    )

    resumed = env.engine([_end()]).resume_run(result.run_id)
    assert resumed.state == "succeeded", resumed.error

    results = [
        e.payload["call_id"]
        for e in env.log.read_run(result.run_id)
        if e.type == "tool.result"
    ]
    assert results.count("r1") == 1, "读操作不得被记两次结果"

    from tianshu.replay import replay as replay_run

    report = replay_run(env.log, result.run_id)
    assert report.ok, report.problems
    assert (env.ws / "a.md").read_text(encoding="utf-8") == "甲"


def test_dispatch_time_rejection_still_gets_a_result_event(env):
    """派发阶段被拒（写前状态变了）也必须留下 tool.result 事件。

    派发层只在真的写进 `tool.result` 的状态下才算"记过了"；拒绝/撤销/守卫内
    复核失败都没有事件。主循环如果只补消息、不补事件，下一轮的
    `messages_hash` 就和 replay 重建的序列对不上——日志看起来完整，其实缺一条。
    """
    engine = env.engine([_write_turn([_create("a.md", "甲", "c1")]), _end()])
    task = env.task()

    def hook(run_id: str, call_id: str) -> None:
        # 在"已获准、还没打开文件"的窗口里，别人抢先建了同名文件
        (env.ws / "a.md").write_text("别人写的\n", encoding="utf-8")

    env.dispatcher.hook = hook
    result = engine.start(task.task_id)
    env.dispatcher.hook = None

    assert result.effect_state == "none_applied"
    assert (env.ws / "a.md").read_text(encoding="utf-8") == "别人写的\n", "不能覆盖"
    results = [
        e.payload
        for e in env.log.read_run(result.run_id)
        if e.type == "tool.result"
    ]
    assert [r["call_id"] for r in results] == ["c1"]
    assert results[0]["status"] == "rejected"
    assert results[0]["output"]["rule_id"] in (
        "dispatch.stale_precondition",
        "dispatch.refused",
    )

    from tianshu.replay import replay as replay_run

    report = replay_run(env.log, result.run_id)
    assert report.ok, report.problems
