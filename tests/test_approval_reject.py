"""审批被否决 = 这个动作被否决（issue #40）。

这一组测试盯的是「拒绝之后会发生什么」——在 issue #40 之前，答案是"再问一次"：

- `approvals reject` 之后 `run resume`，因为找不到可用批准，会**再建一张新审批**；
- Run 又回到 `waiting_approval`，用户只能反复点拒绝，Run 永远收不了尾；
- 拒绝也没有明确的工具结果语义，看起来像"那次调用被中断了"。

现在的契约（docs/PRD-v1.md §5.4 / §5.4.1）：

1. 同一 Run 内、同一**动作内容**（`subject_digest`，不含 call_id / owner /
   claim_epoch）的已拒绝审批 = 这个动作被否决：后续派发直接拒绝（
   `c4.approval_rejected`），**不再发起新审批**；
2. 被否决的动作落 `tool.result(status=rejected, effect_state=none_applied)`，
   payload 带 `rule_id`；
3. 模型拿不出替代动作 → Run 以 `failed` 收束，`run.end.reason` 写明审批被拒绝，
   而不是停在 `waiting_approval`；
4. 改参数/改目标 → 摘要变 → 允许重新发起审批；
5. 换个 call_id 重发同一个动作**不能**绕过否决（这是 issue #40 的真实复现路径）。

第 5 条是刻意的：否决缓存键必须排除调用身份，否则模型只要重新生成一个
`call_id`，人的否决就变成一张新的待批门票。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tianshu.dispatcher import (
    APPROVAL_REJECTED,
    build_request,
)
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, Stack, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ActionRequest, Approval, HumanIdentity
from tianshu.tasks import RUN_RUNNING, RUN_WAITING_APPROVAL, Task

ORIGINAL = "原始内容\n"
ORIGINAL_BYTES = ORIGINAL.encode("utf-8")


# ---------------------------------------------------------------- 夹具


@dataclass
class Env:
    root: Path
    ws: Path
    log: EventLog
    stack: Stack

    # ---- 驱动 ----

    def engine(self, responses, *, limits: Limits | None = None) -> AgentLoop:
        return AgentLoop(
            tasks=self.stack.tasks,
            leases=self.stack.leases,
            claims=self.stack.claims,
            dispatcher=self.stack.dispatcher,
            approvals=self.stack.approvals,
            snapshots=self.stack.snapshots,
            model=ScriptedModel(list(responses)),
            limits=limits or Limits(8, 20_000),
            memory=self.stack.memory,
            contents=self.stack.contents,
        )

    def task(self, goal: str = "整理目录里的笔记") -> Task:
        return self.stack.tasks.create_task(goal, [{"root_path": str(self.ws)}])

    def new_manual_run(self):
        """手工开一个 RUNNING 的 Run（给"只测派发层"的测试用，不经过主循环）。"""
        task = self.task()
        run = self.stack.tasks.start_run(task.task_id)
        self.stack.leases.acquire([self.ws], run.run_id)
        job = self.stack.claims.enqueue(run.run_id)
        claim = self.stack.claims.claim(job.job_id)
        self.stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        return task, run, claim

    # ---- 观察 ----

    def events(self, run_id: str):
        return self.log.read_run(run_id)

    def events_of(self, run_id: str, event_type: str):
        return [e for e in self.log.read_run(run_id) if e.type == event_type]

    def count(self, run_id: str, event_type: str) -> int:
        return len(self.events_of(run_id, event_type))

    def approvals_for(self, run_id: str) -> list[Approval]:
        ids = [
            row["approval_id"]
            for row in self.log.conn.execute(
                "SELECT approval_id FROM approvals WHERE run_id=?",
                (run_id,),
            ).fetchall()
        ]
        return [a for a in (self.stack.approvals.get(i) for i in ids) if a is not None]

    def veto_results(self, run_id: str) -> list[dict]:
        return [
            e.payload
            for e in self.events_of(run_id, "tool.result")
            if (e.payload.get("output") or {}).get("rule_id") == APPROVAL_REJECTED
        ]


def _env(tmp_path: Path) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_bytes(ORIGINAL_BYTES)
    data = tmp_path / "authority"
    data.mkdir()
    log = EventLog(tmp_path / "events.db")
    stack = build_stack(log, data_root=data)
    return Env(root=tmp_path, ws=ws, log=log, stack=stack)


@pytest.fixture()
def env(tmp_path):
    e = _env(tmp_path)
    try:
        yield e
    finally:
        e.log.close()


def _actor() -> HumanIdentity:
    return HumanIdentity.local_cli()


def _write_turn(calls: list[ToolCall]) -> ModelResponse:
    return ModelResponse("tool_use", tool_calls=calls)


def _end(text: str = "完成") -> ModelResponse:
    return ModelResponse("end_turn", content=text)


def _overwrite(path: str, content: str, call_id: str) -> ToolCall:
    return ToolCall(call_id, "modify_text", {"path": path, "content": content})


def _reject(env: Env, waiting) -> str:
    """人否决这次待批动作，返回审批编号。"""
    assert waiting.state == RUN_WAITING_APPROVAL
    assert len(waiting.approval_ids) == 1
    approval_id = waiting.approval_ids[0]
    env.stack.approvals.decide(approval_id, decision="rejected", actor=_actor())
    assert env.stack.approvals.get(approval_id).state == "rejected"
    return approval_id


# ---------------------------------------------------------------- 1. 否决 → 终止


def test_reject_then_resume_fails_without_a_second_approval(env):
    """拒绝 → resume：Run 以 failed 收束，且**没有第二个 approval.requested**。

    这是 issue #40 的直接回归：修之前 resume 会再建一张审批，Run 回到
    `waiting_approval`，用户只能反复点拒绝。
    """
    engine = env.engine(
        [_write_turn([_overwrite("note.md", "新内容\n", "c1")]), _end()]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)

    resumed = engine.resume_run(waiting.run_id)

    # ---- Run 的结论 ----
    assert resumed.state == "failed", resumed.error
    assert resumed.outcome == "failed"
    assert resumed.effect_state == "none_applied"
    assert resumed.error is not None and APPROVAL_REJECTED in resumed.error

    # ---- 不许再问一次 ----
    assert env.count(waiting.run_id, "approval.requested") == 1, (
        "拒绝之后又发起了一张新审批：这正是 issue #40 的循环"
    )
    assert env.approvals_for(waiting.run_id) != []
    assert len(env.approvals_for(waiting.run_id)) == 1
    assert env.stack.approvals.pending(run_id=waiting.run_id) == []

    # ---- 拒绝必须落成可审计的工具结果，而不是"中断" ----
    vetoes = env.veto_results(waiting.run_id)
    assert len(vetoes) == 1
    assert vetoes[0]["status"] == "rejected"
    assert vetoes[0]["effect_state"] == "none_applied"
    assert vetoes[0]["output"]["rule_id"] == APPROVAL_REJECTED

    # dispatch.rejected 也带着同一个编号（用户可见的"为什么被拒"）
    rejected_events = env.events_of(waiting.run_id, "dispatch.rejected")
    assert rejected_events
    assert rejected_events[-1].payload["rule_id"] == APPROVAL_REJECTED

    # ---- 没有任何副作用，也没有假装的凭据 ----
    assert (env.ws / "note.md").read_bytes() == ORIGINAL_BYTES
    assert env.count(waiting.run_id, "tool.intent") == 0, "被否决的动作不许派发"
    assert env.count(waiting.run_id, "receipt") == 0
    assert sorted(p.name for p in env.ws.iterdir()) == ["note.md"]


def test_rejection_keeps_the_run_out_of_waiting_approval(env):
    """Run 不许死在 `waiting_approval` 上：它必须是终态，且只有一条 run.end。"""
    engine = env.engine(
        [_write_turn([_overwrite("note.md", "新内容\n", "c1")]), _end()]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)
    resumed = engine.resume_run(waiting.run_id)

    assert resumed.terminal, resumed.state
    assert resumed.state not in (RUN_WAITING_APPROVAL, "paused", "reconciling")
    assert env.count(waiting.run_id, "run.end") == 1
    end = env.events_of(waiting.run_id, "run.end")[-1].payload
    # 事后读事件就能看出"为什么这次 Run 是失败的"
    assert end["outcome"] == "failed"
    assert end["effect_state"] == "none_applied"
    assert APPROVAL_REJECTED in (end.get("error") or "")


def test_veto_is_recorded_as_none_applied_not_unknown(env):
    """拒绝 ≠ 中断：`unknown/partial` 会让工作区被阻断，拒绝不配这个结论。"""
    engine = env.engine(
        [_write_turn([_overwrite("note.md", "新内容\n", "c1")]), _end()]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)
    engine.resume_run(waiting.run_id)

    assert env.count(waiting.run_id, "tool.result") == 1
    payload = env.events_of(waiting.run_id, "tool.result")[0].payload
    assert payload["status"] == "rejected", "拒绝不是 failed：失败意味着尝试过"
    assert payload["effect_state"] == "none_applied"
    # 也绝不能是 unknown/partial：那会要求 recovery 对账，并阻断工作区写入
    assert payload["effect_state"] not in ("unknown", "partial", "applied")


# ---------------------------------------------------------------- 2. 改参数 → 可再批


def test_changed_arguments_may_request_a_new_approval(env):
    """模型改了参数：动作内容变了，人可以改主意重新批准，Run 正常做完。

    脚本：覆盖 note.md 写 A（被拒）→ 改写 B（新审批，批准）→ 结束。
    """
    engine = env.engine(
        [
            _write_turn([_overwrite("note.md", "第一版\n", "c1")]),
            # 模型看到 c4.approval_rejected 之后改了内容再发起
            _write_turn([_overwrite("note.md", "第二版\n", "c2")]),
            _end(),
        ]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)

    second = engine.resume_run(waiting.run_id)
    assert second.state == RUN_WAITING_APPROVAL, second.error
    assert env.count(waiting.run_id, "approval.requested") == 2, (
        "改了参数应当能重新发起审批（否决只绑定同一份动作内容）"
    )
    pending = env.stack.approvals.pending(run_id=waiting.run_id)
    assert len(pending) == 1
    assert pending[0].approval_id not in waiting.approval_ids
    assert pending[0].request_digest != _rejected_request_digest(env, waiting.run_id)

    # 人批准第二版 → resume 之后它只写一次，Run 正常成功
    env.stack.approvals.decide(
        pending[0].approval_id, decision="approved", actor=_actor()
    )
    done = engine.resume_run(waiting.run_id)

    assert done.state == "succeeded", done.error
    assert done.effect_state == "applied"
    assert (env.ws / "note.md").read_bytes() == "第二版\n".encode()
    assert env.count(waiting.run_id, "approval.consumed") == 1


# ---------------------------------------------------------------- 3. 改目标 → 旧否决失效


def _rejected_request_digest(env: Env, run_id: str) -> str:
    rejected = [a for a in env.approvals_for(run_id) if a.state == "rejected"]
    assert len(rejected) == 1
    return rejected[0].request_digest


def _first_request(env: Env, run_id: str) -> ActionRequest:
    request_ids = env.stack.dispatcher.request_ids_for_run(run_id)
    assert request_ids, "Run 里应当已经登记过请求"
    stored = env.stack.dispatcher.get_request(request_ids[0])
    assert stored is not None
    return stored.request


def test_goal_revision_change_invalidates_the_old_rejection(env):
    """改目标（goal_revision/lease_epoch +1）→ 旧否决失效。

    否决缓存键是动作内容；owner/revision 一旦变化就是另一件事，人可以重新决定，
    不能拿旧否决把新目标下的动作也一并否掉。
    """
    engine = env.engine(
        [_write_turn([_overwrite("note.md", "新内容\n", "c1")]), _end()]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)

    request = _first_request(env, waiting.run_id)
    assert env.stack.approvals.rejected_for(request) is not None

    new_goal = replace(
        request,
        goal_revision=request.goal_revision + 1,
        lease_epoch=request.lease_epoch + 1,
    )
    assert env.stack.approvals.rejected_for(new_goal) is None, (
        "改目标之后旧否决仍然命中：人无法为新目标重新决定"
    )

    # 改参数/换目标同理；只有"换调用编号 / 换执行身份"不解除否决。
    assert env.stack.approvals.rejected_for(
        replace(request, args_hash="0" * 64)
    ) is None
    assert env.stack.approvals.rejected_for(
        replace(request, resources=(str(env.ws / "other.md"),))
    ) is None


def test_new_goal_revision_can_request_a_fresh_approval(env):
    """端到端证明"改目标 → 旧否决失效"：新目标的 Run 可以为同一动作重新发起审批。

    改目标会让 `goal_revision/lease_epoch` +1 并 fence 旧 Run（PRD §4.3）；新
    Run 下"同一个动作"是另一件事，不能被旧否决一并否掉。
    """
    task = env.task()
    engine = env.engine(
        [_write_turn([_overwrite("note.md", "第一版\n", "c1")]), _end()]
    )
    first = engine.start(task.task_id)
    _reject(env, first)

    revised = env.stack.tasks.revise_task(task.task_id, goal="换一个目标再改一次")
    assert revised.goal_revision == 2
    assert revised.lease_epoch == 2
    assert env.stack.tasks.get_run(first.run_id).state == "fenced"

    second_engine = env.engine(
        [_write_turn([_overwrite("note.md", "第二版\n", "c1")]), _end()]
    )
    second = second_engine.start(task.task_id)
    assert second.state == RUN_WAITING_APPROVAL, second.error
    assert second.run_id != first.run_id
    assert env.count(second.run_id, "approval.requested") == 1, (
        "新目标下同一个动作被旧否决挡住了：人无法为新目标重新决定"
    )
    assert env.stack.approvals.pending(run_id=second.run_id) != []


def test_changing_only_call_identity_does_not_bypass_the_veto(env):
    """换 call_id / attempt_id / owner / claim_epoch 不算"换了动作"。

    这些是调用身份：模型重发同一个动作时 call_id 会变，进程接管时 claim_epoch
    会变。若把它们算进否决键，模型只要重新生成一个 call_id 就能把人的否决变回
    "待批"——issue #40 的真实复现路径。
    """
    engine = env.engine(
        [_write_turn([_overwrite("note.md", "新内容\n", "c1")]), _end()]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)

    request = _first_request(env, waiting.run_id)
    for renamed in (
        replace(request, call_id="c_replayed", request_id="req_replayed"),
        replace(request, attempt_id=request.attempt_id + 1),
        replace(
            request,
            job_id="job_other",
            claim_epoch=request.claim_epoch + 1,
            owner_instance_id="other-instance",
        ),
    ):
        assert env.stack.approvals.rejected_for(renamed) is not None, (
            "换调用/执行身份就绕过了否决：这等于否决只生效一次"
        )


# ---------------------------------------------------------------- 4. 重发同一个动作 = 绕过点


def test_repeating_the_vetoed_action_with_a_new_call_id_terminates_the_run(env):
    """模型换个 call_id 原样重发同一个动作：仍然命中否决，Run 直接 failed。

    这是真实 provider 的行为（每轮从 `call_0` 重新编号）：如果否决缓存键包含
    call_id，这里会再建一张审批、Run 回到 waiting——issue #40 原样复现。
    """
    engine = env.engine(
        [
            _write_turn([_overwrite("note.md", "新内容\n", "c1")]),
            # 同一个动作、同一份参数，只换了调用编号
            _write_turn([_overwrite("note.md", "新内容\n", "c9")]),
            _end(),
        ]
    )
    waiting = engine.start(env.task().task_id)
    _reject(env, waiting)

    resumed = engine.resume_run(waiting.run_id)

    assert resumed.state == "failed", resumed.error
    assert resumed.effect_state == "none_applied"
    assert APPROVAL_REJECTED in (resumed.error or "")
    # 两次都是"否决式拒绝"：第一次是 cursor 里那张待批被否决，第二次是重发
    assert len(env.veto_results(waiting.run_id)) == 2
    assert env.count(waiting.run_id, "approval.requested") == 1, (
        "重发同一个动作竟然又开了一张审批：否决可以被绕过"
    )
    assert len(env.approvals_for(waiting.run_id)) == 1
    assert (env.ws / "note.md").read_bytes() == ORIGINAL_BYTES


def test_prepare_hits_the_veto_cache_without_creating_an_approval(env):
    """派发层直测：同一动作第二次 prepare 必须命中缓存，库里只有一张审批。"""
    task, run, claim = env.new_manual_run()
    env.stack.snapshots.init_run(run.run_id)
    args = {"path": "note.md", "content": "新内容\n"}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id="c1",
        tool_id="modify_text",
        target=env.ws / "note.md",
        args=args,
    )

    first = env.stack.dispatcher.prepare(request, claim, args=args)
    assert first.is_waiting
    env.stack.approvals.decide(
        first.approval_id, decision="rejected", actor=_actor()
    )

    second = env.stack.dispatcher.prepare(request, claim, args=args)
    assert second.is_rejected
    assert second.rule_id == APPROVAL_REJECTED
    assert second.approval_id is None, "被否决的动作不许再挂一张审批"
    assert env.count(run.run_id, "approval.requested") == 1
    assert len(env.approvals_for(run.run_id)) == 1

    stored = env.stack.dispatcher.get_request(second.request_id)
    assert stored is not None
    assert stored.state == "rejected"
    assert stored.rule_id == APPROVAL_REJECTED

    # 连 dispatch 这条入口也只能给出 rejected/none_applied，绝不动文件
    result = env.stack.dispatcher.dispatch(second.request_id, claim)
    assert result.status == "rejected"
    assert result.effect_state == "none_applied"
    assert result.output["rule_id"] == APPROVAL_REJECTED
    assert (env.ws / "note.md").read_bytes() == ORIGINAL_BYTES