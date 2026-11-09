"""控制面的行为测试（不启 socket、不启线程）。

`control.py` 的职责只有一条：**把 RPC 方法逐字映射到既有库函数**，并只投影契约
里的字段（`docs/M5-desktop.md` §3.4）。所以这里不测"策略对不对"——那是派发层的
测试该管的事；这里钉三件容易被"顺手重构"破坏的性质：

1. **`run.start` 只入队。** 它返回时 Run 不在 `running`，作业已经在队列里；
   如果哪天有人图省事在前台 `transition(RUNNING)`，所有"谁在跑"的语义都会失真。
2. **视图字段是精确集合。** 多一个字段就是偷偷扩大契约，少一个让 UI 去猜；
   尤其 `digest` 不许出现在任何视图里（§3.4）。
3. **审批决定只把 Run 交给驱动线程一次。** `resumed` 回答的是"已入队"，不是
   "已经跑起来了"——把队列说成执行就是撒谎。
"""

from __future__ import annotations

import hashlib
import queue

import pytest
from support import stack_for

from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ScriptedModel
from tianshu.permissions import ActionRequest, PolicyDecision
from tianshu.tasks import RUN_RUNNING

#: 契约里逐字列出的字段集合。改视图必须同时改契约文档与这里。
TASK_FIELDS = {
    "task_id",
    "goal",
    "goal_revision",
    "status",
    "active_run_id",
    "workspaces",
    "created_at",
    "updated_at",
}
RUN_FIELDS = {
    "run_id",
    "task_id",
    "state",
    "outcome",
    "effect_state",
    "control_requested",
    "goal_revision",
    "lease_epoch",
    "started_at",
    "ended_at",
    "n_applied",
    "n_unknown",
    "undoable",
}
APPROVAL_FIELDS = {
    "approval_id",
    "run_id",
    "task_id",
    "action",
    "resources",
    "reason",
    "rule_id",
    "state",
    "created_at",
    "expires_at",
}
EVENT_FIELDS = {"seq", "type", "run_id", "task_id", "ts", "actor_kind", "payload"}
STATE_FIELDS = {"tasks", "runs", "approvals", "event_seq_head", "daemon"}


class Env:
    """一套控制面 + 一个临时 workspace。"""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.ws = tmp_path / "ws"
        self.ws.mkdir()
        self.log = EventLog(tmp_path / "events.db")
        self.stack = stack_for(self.log, data_root=tmp_path)
        self.resume: queue.Queue[str] = queue.Queue()
        self.plane = ControlPlane(
            self.stack,
            model=ScriptedModel([]),
            limits=Limits(max_turns=3),
            resume_requests=self.resume,
        )

    def create_task(self, goal: str = "改报告") -> dict:
        return self.plane.handle(
            "task.create",
            {
                "goal": goal,
                "workspaces": [{"root_path": str(self.ws), "mode": "writable"}],
            },
        )["task"]

    def start_run(self, task_id: str, out_name: str = "report.md") -> dict:
        return self.plane.handle(
            "run.start", {"task_id": task_id, "out_name": out_name}
        )["run"]

    def approval(
        self,
        run_id: str,
        *,
        call_id: str = "call_1",
        args_hash: str | None = None,
        rule_id: str = "c4.overwrite",
    ) -> str:
        run = self.stack.tasks.get_run(run_id)
        assert run is not None
        request = ActionRequest(
            request_id=f"req_{call_id}",
            task_id=run.task_id,
            run_id=run_id,
            job_id="job_manual",
            claim_epoch=1,
            owner_instance_id="inst_manual",
            goal_revision=run.goal_revision,
            lease_epoch=run.lease_epoch,
            call_id=call_id,
            attempt_id=1,
            tool_id="modify_text",
            tool_version="1.0",
            schema_hash="schema_manual",
            action="overwrite",
            resources=(str(self.ws / "note.md"),),
            args_hash=args_hash or hashlib.sha256(call_id.encode()).hexdigest(),
            preconditions={"target": "present"},
        )
        approval = self.stack.approvals.request(
            request, PolicyDecision("approve", "覆盖已有文件需要确认", rule_id)
        )
        return approval.approval_id

    def close(self) -> None:
        self.plane.close()


@pytest.fixture()
def env(tmp_path):
    instance = Env(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------------------- task


def test_task_create_returns_exact_contract_view(env: Env) -> None:
    task = env.create_task()
    assert set(task) == TASK_FIELDS
    assert task["goal"] == "改报告"
    assert task["goal_revision"] == 1
    assert task["active_run_id"] is None
    assert task["status"] == "open"
    assert task["workspaces"] == [
        {
            "root_path": str(env.ws.resolve()),
            "mode": "writable",
            "snapshot_policy": "on_write",
        }
    ]
    assert isinstance(task["created_at"], int) and task["created_at"] > 0
    assert task["updated_at"] >= task["created_at"]


def test_task_list_is_newest_first(env: Env) -> None:
    first = env.create_task("先建的")
    second = env.create_task("后建的")
    tasks = env.plane.handle("task.list", {"limit": 10})["tasks"]
    assert {item["task_id"] for item in tasks} == {first["task_id"], second["task_id"]}
    stamps = [item["updated_at"] for item in tasks]
    assert stamps == sorted(stamps, reverse=True)
    assert all(set(item) == TASK_FIELDS for item in tasks)


def test_task_revise_bumps_revision_and_fences_old_run(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    revised = env.plane.handle(
        "task.revise", {"task_id": task["task_id"], "goal": "换个目标"}
    )["task"]
    assert revised["goal"] == "换个目标"
    assert revised["goal_revision"] == 2
    # 旧 Run 被 fence：state 直接落终态之外的 fenced，不是"还能继续跑"。
    status = env.plane.handle("run.status", {"run_id": run["run_id"]})["run"]
    assert status["state"] == "fenced"


# --------------------------------------------------------------------------- run


def test_run_start_only_enqueues(env: Env) -> None:
    """`run.start` 返回时：Run 不是 running、作业已在队列、没有前台驱动。"""
    task = env.create_task()
    run = env.start_run(task["task_id"])
    assert set(run) == RUN_FIELDS
    assert run["state"] not in ("running", "succeeded", "failed", "cancelled")
    stored = env.stack.tasks.get_run(run["run_id"])
    assert stored is not None and stored.state == "created"
    job = env.stack.claims.job_for_run(run["run_id"])
    assert job is not None and job.state == "pending"
    # 作业入队了，但**没有**任何东西被叫醒：resume 队列为空是"不驱动"的证据。
    assert env.resume.empty()
    # 回放需要的 payload 与 `AgentLoop.start` 逐字一致。
    payload = env.stack.tasks.run_payload(run["run_id"])
    assert set(payload) >= {"task", "out", "limits", "tools"}
    assert payload["task"] == "改报告" and payload["out"] == "report.md"


def test_run_start_refuses_second_active_run(env: Env) -> None:
    task = env.create_task()
    env.start_run(task["task_id"])
    with pytest.raises(ControlError) as excinfo:
        env.start_run(task["task_id"])
    assert excinfo.value.code == "conflict"


def test_run_start_unknown_task_is_not_found(env: Env) -> None:
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("run.start", {"task_id": "task_missing"})
    assert excinfo.value.code == "not_found"


def test_run_list_and_status_use_contract_views(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    runs = env.plane.handle("run.list", {"task_id": task["task_id"]})["runs"]
    assert [item["run_id"] for item in runs] == [run["run_id"]]
    assert all(set(item) == RUN_FIELDS for item in runs)
    status = env.plane.handle("run.status", {"run_id": run["run_id"]})["run"]
    assert status == run


def test_run_pause_is_immediate_in_safe_state(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    paused = env.plane.handle("run.pause", {"run_id": run["run_id"]})["run"]
    assert paused["state"] == "paused"
    assert paused["control_requested"] is None


def test_run_cancel_finishes_terminal(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    cancelled = env.plane.handle("run.cancel", {"run_id": run["run_id"]})["run"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["outcome"] == "cancelled"
    assert cancelled["ended_at"] is not None


def test_run_resume_enqueues_stopped_run_only(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    env.plane.handle("run.pause", {"run_id": run["run_id"]})
    resumed = env.plane.handle("run.resume", {"run_id": run["run_id"]})["run"]
    # 只入队：返回的仍是 paused，生效状态由驱动线程改。
    assert resumed["state"] == "paused"
    assert env.resume.get_nowait() == run["run_id"]

    # running 的 Run 已经在队里/在跑，再塞 resume 只会跟 owner 抢 claim。
    env.plane.resume_drained(run["run_id"])
    env.stack.tasks.transition(run["run_id"], RUN_RUNNING)
    env.plane.handle("run.resume", {"run_id": run["run_id"]})
    assert env.resume.empty()


def test_run_resume_terminal_is_conflict(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    env.plane.handle("run.cancel", {"run_id": run["run_id"]})
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("run.resume", {"run_id": run["run_id"]})
    assert excinfo.value.code == "conflict"


# --------------------------------------------------------------------------- state


def test_state_has_exact_shape_and_millisecond_timestamps(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    state = env.plane.handle("state", {})
    assert set(state) == STATE_FIELDS
    assert state["event_seq_head"] == env.log.seq_head()
    assert isinstance(state["event_seq_head"], int)
    assert set(state["daemon"]) >= {"daemon_version", "instance_id", "started_at"}
    assert [item["task_id"] for item in state["tasks"]] == [task["task_id"]]
    assert [item["run_id"] for item in state["runs"]] == [run["run_id"]]
    assert all(set(item) == TASK_FIELDS for item in state["tasks"])
    assert all(set(item) == RUN_FIELDS for item in state["runs"])
    for key in ("created_at", "updated_at"):
        assert isinstance(state["tasks"][0][key], int)
    for key in ("started_at",):
        assert isinstance(state["runs"][0][key], int)
    # 视图里不许出现 digest（§3.4：人和模型之间不该靠哈希沟通）。
    assert "digest" not in repr(state).lower()


def test_state_runs_are_non_terminal_plus_recent_terminal(env: Env) -> None:
    task = env.create_task()
    first = env.start_run(task["task_id"])
    env.plane.handle("run.cancel", {"run_id": first["run_id"]})
    second = env.start_run(task["task_id"])
    state = env.plane.handle("state", {})
    ids = [item["run_id"] for item in state["runs"]]
    # 非终态在前（按开始顺序），终态在后。
    assert ids == [second["run_id"], first["run_id"]]


# --------------------------------------------------------------------------- events


def test_events_run_returns_contract_view_and_pages(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    page = env.plane.handle("events.run", {"run_id": run["run_id"], "limit": 1})
    assert set(page) == {"events", "seq_head", "has_more"}
    assert page["seq_head"] == env.log.seq_head()
    assert page["has_more"] is True
    assert len(page["events"]) == 1
    event = page["events"][0]
    assert set(event) == EVENT_FIELDS
    assert event["type"] == "run.start"
    assert isinstance(event["seq"], int) and isinstance(event["ts"], int)

    rest = env.plane.handle(
        "events.run", {"run_id": run["run_id"], "after_seq": event["seq"]}
    )
    seqs = [item["seq"] for item in rest["events"]]
    assert seqs == sorted(seqs)
    assert all(seq > event["seq"] for seq in seqs)
    assert rest["has_more"] is False


def test_events_run_unknown_run_is_not_found(env: Env) -> None:
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("events.run", {"run_id": "run_missing"})
    assert excinfo.value.code == "not_found"


def test_events_run_limit_is_bounded(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("events.run", {"run_id": run["run_id"], "limit": 0})
    assert excinfo.value.code == "bad_params"


# --------------------------------------------------------------------------- approvals


def test_approvals_list_has_exact_shape_and_rule_id(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    approval_id = env.approval(run["run_id"])
    approvals = env.plane.handle("approvals.list", {"run_id": run["run_id"]})[
        "approvals"
    ]
    assert len(approvals) == 1
    view = approvals[0]
    assert set(view) == APPROVAL_FIELDS
    assert view["approval_id"] == approval_id
    assert view["state"] == "pending"
    # `Approval` 投影没有 rule_id，它必须从 approval.requested 事件补出来。
    assert view["rule_id"] == "c4.overwrite"
    assert "digest" not in repr(view).lower()


def test_approvals_decide_enqueues_once_per_run(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    first = env.approval(run["run_id"], call_id="call_1")
    second = env.approval(run["run_id"], call_id="call_2")

    decided = env.plane.handle(
        "approvals.decide", {"approval_id": first, "decision": "approve"}
    )
    assert set(decided) == {"approval", "resumed"}
    assert decided["approval"]["state"] == "approved"
    assert decided["resumed"] is True
    assert env.resume.get_nowait() == run["run_id"]

    # 同一 Run 的第二个决定：驱动线程还没取走（inflight 仍在），不重复入队
    # （resumed=False 是"已经交出去了"，不是"失败了"）。
    again = env.plane.handle(
        "approvals.decide", {"approval_id": second, "decision": "approve"}
    )
    assert again["approval"]["state"] == "approved"
    assert again["resumed"] is False
    assert env.resume.empty()

    # 驱动线程取走后，同一个 Run 的下一次决定应该能再次入队。
    env.plane.resume_drained(run["run_id"])
    third = env.approval(run["run_id"], call_id="call_3")
    decided_again = env.plane.handle(
        "approvals.decide", {"approval_id": third, "decision": "reject"}
    )
    assert decided_again["approval"]["state"] == "rejected"
    assert decided_again["resumed"] is True
    assert env.resume.get_nowait() == run["run_id"]


def test_approvals_decide_unknown_is_not_found(env: Env) -> None:
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle(
            "approvals.decide", {"approval_id": "appr_x", "decision": "approve"}
        )
    assert excinfo.value.code == "not_found"


def test_approvals_decide_bad_decision_is_bad_params(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    approval_id = env.approval(run["run_id"])
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle(
            "approvals.decide", {"approval_id": approval_id, "decision": "maybe"}
        )
    assert excinfo.value.code == "bad_params"


def test_approvals_decide_rejected_not_resumed_when_no_driver(env: Env) -> None:
    """没有驱动队列时 `resumed` 必须是 False：不撒谎说"已交给执行器"。"""
    task = env.create_task()
    run = env.start_run(task["task_id"])
    approval_id = env.approval(run["run_id"])
    plane = ControlPlane(
        env.stack, model=ScriptedModel([]), resume_requests=None
    )
    decided = plane.handle(
        "approvals.decide", {"approval_id": approval_id, "decision": "approve"}
    )
    assert decided["resumed"] is False


# --------------------------------------------------------------------------- undo


def test_undo_plan_empty_still_returns_changes_list(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    plan = env.plane.handle("undo.plan", {"run_id": run["run_id"]})
    assert plan == {"changes": []}


def test_undo_plan_maps_to_absolute_paths_and_kinds(env: Env) -> None:
    task = env.create_task()
    run = env.start_run(task["task_id"])
    env.stack.snapshots.init_run(run["run_id"])
    note = env.ws / "note.md"
    (note).write_bytes(b"old\n")
    env.stack.snapshots.record_preimage(
        run["run_id"],
        "call_1",
        "note.md",
        before=b"old\n",
        expected_after=hashlib.sha256(b"new\n").hexdigest(),
        root=env.ws,
    )
    env.stack.snapshots.record_preimage(
        run["run_id"],
        "call_2",
        "created.md",
        before=None,
        expected_after=hashlib.sha256(b"x\n").hexdigest(),
        root=env.ws,
    )
    plan = env.plane.handle("undo.plan", {"run_id": run["run_id"]})
    assert [item["kind"] for item in plan["changes"]] == ["delete", "restore"]
    assert [item["path"] for item in plan["changes"]] == [
        str((env.ws / "created.md").resolve()),
        str((env.ws / "note.md").resolve()),
    ]
    assert all(set(item) == {"path", "kind", "reason"} for item in plan["changes"])


# --------------------------------------------------------------------------- 错误


def test_unknown_method_reports_unknown_method(env: Env) -> None:
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("run.explode", {})
    assert excinfo.value.code == "unknown_method"
    assert "run.explode" in excinfo.value.message


def test_missing_params_is_bad_params(env: Env) -> None:
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("run.start", {})
    assert excinfo.value.code == "bad_params"


def test_non_object_params_is_bad_params(env: Env) -> None:
    with pytest.raises(ControlError) as excinfo:
        env.plane.handle("state", ["not", "an", "object"])
    assert excinfo.value.code == "bad_params"


def test_close_is_idempotent(env: Env) -> None:
    env.plane.close()
    env.plane.close()  # 第二次不许抛：装配失败路径与正常路径都会走到它