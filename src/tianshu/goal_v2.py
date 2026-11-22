"""事件驱动的 Goal V2 协调器；阶段 Run 复用持久 Job 与执行侧门禁。"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import replace
from pathlib import Path

from .budget import BOUND_UPPER
from .canonical import jcs_sha256
from .errors import BudgetCancelled, BudgetConfigError, BudgetRefused, TianshuError
from .execpolicy import ISOLATION_BOUNDARIES
from .goal_contracts import GOAL_EVENTS, RESOLUTIONS, GoalConfigV2, initial_state
from .goals import request_id as checked_request_id
from .plans import PlanStore, put_material, read_material
from .run_policy import EFFECTS, PURE_ONLY, RunExecutionPolicyV1
from .tasks import RUN_FENCED, TERMINAL_STATES
from .verification import step_verified

PHASE_EVENT = "goal.v2_phase_started"
STOPPED = ("completed", "cancelled", "stale")
HARD_BLOCKS = ("round_limit", "deadline_exceeded", "budget_exhausted")


class GoalConflict(TianshuError):
    """当前持久事实不允许所请求的迁移。"""


class GoalAdmissionRefused(BudgetConfigError):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


def phase_for(log, run_id):
    events = log.read_run(run_id, event_types=(PHASE_EVENT,))
    if len(events) > 1:
        raise GoalConflict("Goal 阶段绑定重复")
    return events[0].payload if events else None


def inherited_phase(log, run_id):
    """子任务沿真实 Run 父链继承 Goal 拒绝约束，不授予额外权限。"""
    seen = set()
    while run_id and run_id not in seen:
        seen.add(run_id)
        phase = phase_for(log, run_id)
        if phase:
            return phase, run_id
        row = log.conn.execute(
            "SELECT parent_run_id FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        run_id = row["parent_run_id"] if row else None
    return None, None


def _configuration(log, goal_id):
    events = [e for e in log.read_types(("goal.v2_created",))
              if e.payload["config"]["goal_id"] == goal_id]
    if len(events) != 1:
        raise GoalConflict("Goal 不存在或创建记录重复")
    return events[0]


def _check_account(budget, account_id, *, parent_id, run_id, label, limits,
                   deadline_ms, bound):
    expected = {"account_id": account_id, "parent_id": parent_id, "run_id": run_id,
                "label": label, "deadline_ms": deadline_ms, "bound": bound}
    account = budget.account(account_id)
    if (account is None or any(account[k] != v for k, v in expected.items())
            or budget.usage(account_id)["limits"] != limits):
        raise BudgetConfigError("Goal 预算账户配置或父链不符，不能重新挂接已有 Run")


def _latest_state(log, goal_id):
    created = _configuration(log, goal_id)
    events = [e for e in log.read_types(GOAL_EVENTS, task_id=created.task_id)
              if e.payload.get("goal_id") == goal_id and "state" in e.payload]
    return (events[-1].payload["state"] if events else created.payload["state"])


def phase_admission(log, run_id, *, clock=None, monotonic=None, runtime=None):
    phase, root_run = inherited_phase(log, run_id)
    if not phase:
        return None
    config = _configuration(log, phase["goal_id"]).payload["config"]
    state = _latest_state(log, phase["goal_id"])
    wall = (clock or (lambda: int(time.time() * 1000)))()
    mono = (monotonic or (lambda: int(time.monotonic() * 1000)))()
    if wall >= config["deadline_ms"] or mono >= state["deadline_monotonic_ms"]:
        return "deadline_exceeded"
    if (mono < state["clock_monotonic_ms"]
            or (runtime and runtime != state["runtime_session_id"]
                and wall < state["clock_wall_ms"])):
        return "recovery_required"
    task = log.conn.execute(
        "SELECT goal_revision FROM tasks WHERE task_id=?", (config["task_id"],),
    ).fetchone()
    if (not task or task["goal_revision"] != config["goal_revision"]
            or state["status"] not in ("active", "awaiting_input")
            or state["phase_run_id"] != root_run):
        return "recovery_required"
    return None


def record_admission_refused(log, run_id, exc, *, source, request_id=""):
    phase, _ = inherited_phase(log, run_id)
    if phase is None:
        return
    if isinstance(exc, GoalAdmissionRefused):
        category = exc.category
    elif isinstance(exc, BudgetRefused):
        category = "deadline_exceeded" if exc.dimension == "deadline" else "budget_exhausted"
    elif isinstance(exc, BudgetCancelled):
        category = "budget_exhausted"
    else:
        category = "protocol_error"
    log.append(run_id, "goal.v2_admission_refused", {
        "format_version": 2, "goal_id": phase["goal_id"], "category": category,
        "source": source, "request_id": request_id,
    }, actor_kind="core")


def ensure_phase_budget(log, budget, run_id, limits, bound, *, admission=True, runtime=None):
    """所有模型调用（包括摘要和重试）进入原账本前核对父链。"""
    phase, root_run = inherited_phase(log, run_id)
    if phase is None:
        return False
    config = _configuration(log, phase["goal_id"]).payload["config"]
    if budget is None:
        raise BudgetConfigError("Goal 阶段不能在缺失累计预算账本时调用模型")
    _check_account(
        budget, "goal:" + config["goal_id"], parent_id=None, run_id="", label="goal",
        limits=config["budget_limits"], deadline_ms=config["deadline_ms"],
        bound=config["budget_bound"],
    )
    if root_run == run_id:
        _check_account(
            budget, "run:" + run_id, parent_id="goal:" + config["goal_id"], run_id=run_id,
            label="run", limits=dict(limits or {}), deadline_ms=config["deadline_ms"],
            bound=bound,
        )
    elif "goal:" + config["goal_id"] not in budget._chain("run:" + run_id):
        raise BudgetConfigError("子任务预算没有连接原 Goal 父链")
    if bound != config["budget_bound"]:
        raise BudgetConfigError("阶段 Run 与 Goal 的预占口径不符")
    if admission:
        category = phase_admission(log, run_id, runtime=runtime)
        if category:
            raise GoalAdmissionRefused(category)
    return True


def limits_for_phase(log, run_id, limits):
    phase, _ = inherited_phase(log, run_id)
    if phase is None:
        return limits
    config = _configuration(log, phase["goal_id"]).payload["config"]
    account_limits = log.conn.execute(
        "SELECT dimension,limit_value FROM budget_limits WHERE account_id=?",
        ("run:" + run_id,),
    ).fetchall()
    starts = log.read_run(run_id, event_types=("run.start",))
    frozen = starts[0].payload.get("limits", {}) if starts else {}
    return replace(limits, budget_bound=config["budget_bound"],
                   budget_limits={r["dimension"]: r["limit_value"] for r in account_limits},
                   max_turns=min(limits.max_turns, frozen.get("max_turns", limits.max_turns)),
                   max_total_tokens=min(
                       limits.max_total_tokens,
                       frozen.get("max_total_tokens", limits.max_total_tokens)))


def denied_content_digest(log, request):
    """剔除可轮换的运行身份，只保留人否决的具体内容和 Goal/策略绑定。"""
    phase, _ = inherited_phase(log, request.run_id)
    if phase is None:
        return None
    config = _configuration(log, phase["goal_id"]).payload["config"]
    fields = request.subject_fields()
    for key in ("run_id", "lease_epoch", "task_id"):
        fields.pop(key, None)
    tasks = log.read_types(("task.created",), task_id=config["task_id"])
    policy = tasks[0].payload if tasks else None
    return jcs_sha256({
        "goal_id": config["goal_id"], "goal_revision": config["goal_revision"],
        "mission_digest": config["mission_digest"], "action": fields,
        "execution_policy": config["execution_policy"],
        "task_policy": policy,
    })


def action_denied(log, request):
    """执行侧拒绝约束；不提供任何授权，也不依赖模型解释。"""
    phase, _ = inherited_phase(log, request.run_id)
    if phase is None:
        return False
    digest = denied_content_digest(log, request)
    state = _latest_state(log, phase["goal_id"])
    if digest in state["recovered_denial_digests"]:
        return False
    if digest in state["denied_content_digests"]:
        return True
    # 人工否决提交后立即约束新动作，不等待下一次 coordinator tick。
    from .permissions import ActionRequest
    for event in log.read_types(("approval.decided",)):
        previous, _ = inherited_phase(log, event.run_id)
        if (not previous or previous["goal_id"] != phase["goal_id"]
                or event.payload["decision"] != "rejected"):
            continue
        row = log.conn.execute(
            "SELECT d.payload FROM dispatch_requests d JOIN approvals a"
            " ON d.request_id=a.request_id WHERE a.approval_id=?",
            (event.payload["approval_id"],),
        ).fetchone()
        if (row and denied_content_digest(
                log, ActionRequest.of(json.loads(row["payload"]))) == digest):
            return True
    return False


class GoalCoordinator:
    def __init__(self, stack, *, model=None, limits=None, clock=None, monotonic=None,
                 runtime_session_id=None):
        from .loop import Limits
        self.stack, self.log, self.tasks = stack, stack.log, stack.tasks
        self.model, self.limits = model, limits or Limits()
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.monotonic = monotonic or (lambda: int(time.monotonic() * 1000))
        self.runtime = runtime_session_id or stack.leases.identity.instance_id
        if stack.budget is None:
            raise BudgetConfigError("Goal V2 必须装配共享预算账本")

    def _events(self, goal_id):
        created = _configuration(self.log, goal_id)
        return [e for e in self.log.read_types(GOAL_EVENTS, task_id=created.task_id)
                if e.payload.get("goal_id", e.payload.get("config", {}).get("goal_id")) == goal_id]

    def get(self, goal_id):
        events = self._events(goal_id)
        created = events[0]
        config = GoalConfigV2.of(created.payload["config"]).to_dict()
        state = copy.deepcopy(created.payload["state"])
        revision = created.seq
        for event in events[1:]:
            p = event.payload
            if "state" not in p:
                continue
            if (p["expected_state_revision"] != revision
                    or p["orchestration_epoch"] != state["orchestration_epoch"]):
                raise GoalConflict("Goal 状态修订或协调代际链损坏")
            state, revision = copy.deepcopy(p["state"]), event.seq
        task = self.tasks.get_task(config["task_id"])
        if task is None:
            raise GoalConflict("Goal Task 缺失")
        if task.goal_revision != config["goal_revision"] and state["status"] not in STOPPED:
            state["status"] = "stale"
        return {
            "format_version": 2, "goal_id": goal_id, "task_id": config["task_id"],
            "config": config, **state, "rounds_started": state["round"],
            "state_revision": revision,
            "assessments": [e.payload for e in events if e.type == "goal.v2_assessed"],
            "pause_pending": bool(state["phase_run_id"] and (
                self.tasks.get_run(state["phase_run_id"]).control_requested == "pause")),
        }

    def mission(self, goal_id):
        created = _configuration(self.log, goal_id)
        mission = read_material(self.stack, created.task_id, created.payload["mission_ref"])
        if jcs_sha256(mission) != created.payload["config"]["mission_digest"]:
            raise GoalConflict("Goal Mission 摘要损坏")
        return mission

    @staticmethod
    def _state(view):
        from .goal_contracts import STATE_SCHEMA
        return {k: copy.deepcopy(view[k]) for k in STATE_SCHEMA["properties"]}

    def _transition(self, view, state, *, cause, request_id="", event="goal.v2_transitioned",
                    extra=None, human=False):
        current = self.get(view["goal_id"])
        if (current["state_revision"] != view["state_revision"]
                or current["orchestration_epoch"] != view["orchestration_epoch"]):
            raise GoalConflict("Goal 迁移 CAS 冲突，请刷新")
        payload = {
            "format_version": 2, "goal_id": view["goal_id"],
            "expected_state_revision": view["state_revision"],
            "orchestration_epoch": view["orchestration_epoch"], "request_id": request_id,
            "cause": cause, "state": state, **(extra or {}),
        }
        self.log.append(state["phase_run_id"] or "", event, payload, task_id=view["task_id"],
                        actor_kind="human" if human else "core")
        return self.get(view["goal_id"])

    def create(self, params):
        from .mission import prepare_mission
        request = checked_request_id(params["request_id"])
        if type(params["goal_revision"]) is not int:
            raise ValueError("goal_revision 必须为整数")
        task = self.tasks.get_task(params["task_id"])
        if task is None or task.goal_revision != params["goal_revision"]:
            raise GoalConflict("任务不存在或目标修订已改变")
        defaults = {
            "mode": "manual", "completion_policy": "human", "replan_policy": "confirm",
            "max_rounds": 3, "deadline_ms": self.clock() + 30 * 60_000,
            "budget_limits": {"requests": 32}, "budget_bound": "estimate",
            "max_no_progress_rounds": 2, "isolation_requirements": [],
            "predecessor_goal_id": None,
        }
        normalized = {**defaults, **params}
        digest = jcs_sha256(params)
        with self.log.transaction() as tx:
            task = self.tasks.get_task(params["task_id"])
            if task is None or task.goal_revision != params["goal_revision"]:
                raise GoalConflict("任务不存在或目标修订已改变")
            for previous in self.log.read_types(("goal.v2_created",), task_id=task.task_id):
                if previous.payload["request_id"] == request:
                    if previous.payload["request_digest"] != digest:
                        raise GoalConflict("request_id 已用于不同的 Goal 配置")
                    return self.get(previous.payload["config"]["goal_id"])
            if task.active_run_id:
                raise GoalConflict("创建 Goal 前必须结束活跃 Run")
            for previous in self.log.read_types(("goal.v2_created",), task_id=task.task_id):
                if self.get(previous.payload["config"]["goal_id"])["status"] not in STOPPED:
                    raise GoalConflict("当前任务已经有未关闭的 Goal V2")
            shared = prepare_mission(
                self.tasks, task.task_id, registry=self.stack.dispatcher.registry,
                criteria=params["criteria"],
            )
            mission = shared.to_dict()
            if not 1 <= len(mission["criteria"]) <= 64:
                raise ValueError("Goal 必须具有 1–64 项可验收条件")
            goal_id = "goal_v2_" + jcs_sha256({"task": task.task_id, "request": request})[:24]
            config = {
                k: normalized[k] for k in defaults
            } | {
                "format_version": 2, "goal_id": goal_id, "task_id": task.task_id,
                "goal_revision": task.goal_revision, "mission_id": mission["mission_id"],
                "mission_digest": jcs_sha256(mission),
                "execution_policy": params["execution_policy"],
            }
            config = GoalConfigV2.of(config).to_dict()
            now = self.clock()
            if config["deadline_ms"] <= now:
                raise ValueError("deadline_ms 必须是未来的绝对截止时间")
            if set(config["isolation_requirements"]) - set(ISOLATION_BOUNDARIES):
                raise ValueError("未知隔离边界")
            if config["predecessor_goal_id"] is not None:
                old = self.get(config["predecessor_goal_id"])
                if old["status"] not in STOPPED:
                    raise GoalConflict("前序 Goal 尚未结束")
            self._isolation(config, task)
            if config["budget_bound"] == BOUND_UPPER:
                from .capability import declared_output_tokens
                if "micros" in config["budget_limits"]:
                    raise BudgetConfigError("当前价格表不能证明金额上界")
                if declared_output_tokens(self.model) is None:
                    raise BudgetConfigError("upper Goal 必须具有可验证的模型输出上界")
            account = "goal:" + goal_id
            self.stack.budget.create_account(
                account, limits=config["budget_limits"], label="goal",
                deadline_ms=config["deadline_ms"], bound=config["budget_bound"],
            )
            _check_account(
                self.stack.budget, account, parent_id=None, run_id="", label="goal",
                limits=config["budget_limits"], deadline_ms=config["deadline_ms"],
                bound=config["budget_bound"],
            )
            tx.append("", "goal.v2_created", {
                "format_version": 2, "config": config,
                "mission_ref": put_material(self.stack, task.task_id, mission),
                "request_id": request, "request_digest": digest,
                "state": initial_state(now, config["deadline_ms"], self.runtime, self.monotonic()),
            }, task_id=task.task_id, actor_kind="human")
        return self.get(goal_id)

    def _isolation(self, config, task):
        if config["mode"] != "bounded_auto":
            return
        # Goal 从不向任务添加命令权限，也不把四边界“声明”当作实际准入。
        policy = task.exec_policy
        requires_commands = bool(policy and policy.argv_allowlist)
        if task.mcp_servers:
            # 启动/发现/调用的生命周期准入由专门入口保证；本阶段不能推测远端安全。
            raise GoalConflict("自动 Goal 尚不能证明所选 MCP 生命周期隔离")
        if requires_commands:
            if (set(policy.isolation_requires) != set(ISOLATION_BOUNDARIES)
                    or set(config["isolation_requirements"]) != set(ISOLATION_BOUNDARIES)):
                raise GoalConflict("自动模式命令策略必须预先要求全部四项隔离边界")

    def _budget_block(self, view, *, new_request=True):
        config = view["config"]
        budget = self.stack.budget
        _check_account(
            budget, "goal:" + view["goal_id"], parent_id=None, run_id="", label="goal",
            limits=config["budget_limits"], deadline_ms=config["deadline_ms"],
            bound=config["budget_bound"],
        )
        usage = budget.usage("goal:" + view["goal_id"])
        for row in budget.reconcile():
            if "goal:" + view["goal_id"] not in budget._chain(row["account_id"]):
                continue
            if (row["state"] == "pending"
                    or any(d not in row["usage"] for d in config["budget_limits"]
                           if d in row["need"])):
                return "usage_unknown"
        if usage["cancelled"] or (usage["overrun"] and config["budget_bound"] == BOUND_UPPER):
            return "budget_exhausted"
        if new_request:
            for dim, limit in config["budget_limits"].items():
                if usage["settled"].get(dim, 0) + usage["reserved"].get(dim, 0) >= limit:
                    return "budget_exhausted"
        return None

    def _clock_state(self, view, state):
        wall, mono = self.clock(), self.monotonic()
        if (wall < state["clock_wall_ms"] and state["runtime_session_id"] != self.runtime
                or mono < state["clock_monotonic_ms"]):
            return "recovery_required"
        state["deadline_remaining_ms"] = max(0, min(
            state["deadline_remaining_ms"], view["config"]["deadline_ms"] - wall,
            state["deadline_monotonic_ms"] - mono,
        ))
        state["clock_wall_ms"] = max(wall, state["clock_wall_ms"])
        state["clock_monotonic_ms"], state["runtime_session_id"] = mono, self.runtime
        return "deadline_exceeded" if state["deadline_remaining_ms"] <= 0 else None

    def _block(self, view, category, *, source=0, safe=False, digest=None, request_id=""):
        state = self._state(view)
        state["status"] = "blocked"
        state["blocker"] = {
            "category": category, "source_event_seq": source,
            "safe_retry": safe, "available_actions": ["cancel"] + (
                ["recover"] if category not in HARD_BLOCKS else []),
            "denied_content_digest": digest,
        }
        if digest and digest not in state["denied_content_digests"]:
            state["denied_content_digests"].append(digest)
        if state["phase_run_id"]:
            run = self.tasks.get_run(state["phase_run_id"])
            if run and run.state not in (*TERMINAL_STATES, RUN_FENCED):
                self.tasks.request_pause(run.run_id)
        if state["round"] > state["last_assessed_round"]:
            plan = PlanStore(self.stack).get(state["plan_id"]) if state["plan_id"] else None
            self._assess(view, state, plan)
        return self._transition(view, state, cause=category, request_id=request_id)

    def _phase_events(self, view):
        return self.log.read_types((PHASE_EVENT,), task_id=view["task_id"])

    def _resource_runs(self, view):
        phases = {e.run_id: e.seq for e in self._phase_events(view)
                  if e.payload["goal_id"] == view["goal_id"]}
        # 单层委派是当前 Task 契约；也核对同 Task 先前阶段遗留的子任务。
        for row in self.log.conn.execute(
            "SELECT child_run_id FROM subtasks WHERE parent_task_id=?", (view["task_id"],),
        ).fetchall():
            phases.setdefault(row["child_run_id"], 0)
        return phases

    def _effects_block(self, view):
        for run_id, seq in self._resource_runs(view).items():
            run = self.tasks.get_run(run_id)
            if run and (run.effect_state in ("partial", "unknown") or run.state == "reconciling"):
                return "effect_unknown", seq
            events = self.log.read_run(run_id, event_types=("tool.intent", "tool.result"))
            intents = {e.payload.get("dispatch_id") for e in events if e.type == "tool.intent"}
            results = {e.payload.get("dispatch_id") for e in events if e.type == "tool.result"}
            if intents - results:
                return "effect_unknown", seq
        return None

    def _lifecycle_block(self, view):
        from .events import EVENT_TYPES
        if "execution.admitted" not in EVENT_TYPES:
            return None
        task = self.tasks.get_task(view["task_id"])
        roots = [Path(b.root_path).resolve() for b in task.workspaces]
        own_runs, sessions = self._resource_runs(view), {}
        for event in self.log.read_types(("execution.admitted", "execution.ended")):
            payload = event.payload
            if event.type == "execution.admitted":
                admission = payload["admission"]
                other = Path(admission["workspace"]).resolve()
                if event.run_id not in own_runs and not any(
                    root.is_relative_to(other) or other.is_relative_to(root) for root in roots
                ):
                    continue
                identity = admission["lifecycle_id"]
                row = sessions.setdefault(identity, {"pending": set(), "ended": False})
                row["seq"] = event.seq
                if payload["stage"] == "call_pending":
                    row["pending"].add(payload.get("request_id", 0))
                elif payload["stage"] == "call_completed":
                    row["pending"].discard(payload.get("request_id", 0))
            elif payload["lifecycle_id"] in sessions:
                sessions[payload["lifecycle_id"]]["ended"] = payload["process_tree"] == "exited"
        for row in sessions.values():
            if row["pending"]:
                return "effect_unknown", row["seq"]
            if not row["ended"]:
                return "recovery_required", row["seq"]
        return None

    def _reconcile_lifecycles(self, goal_id):
        """OS 生命周期核查在协调事务之外；事务内只消费已持久化证据。"""
        from .events import EVENT_TYPES
        if "execution.admitted" not in EVENT_TYPES:
            return
        from .execpolicy import PolicyUnsupported
        from .execution_admission import assert_no_unresolved_executions
        view = self.get(goal_id)
        if view["status"] in STOPPED:
            return
        run = self.tasks.get_run(view["phase_run_id"]) if view["phase_run_id"] else None
        if run and run.state not in (*TERMINAL_STATES, RUN_FENCED):
            return
        task = self.tasks.get_task(view["task_id"])
        for binding in task.workspaces:
            try:
                assert_no_unresolved_executions(self.log, workspace=binding.root_path)
            except PolicyUnsupported:
                # 后续只读门从原 admitted/ended 事实取得来源和封闭 blocker。
                return

    def _denial(self, view):
        from .permissions import ActionRequest
        phase_ids = {e.run_id for e in self._phase_events(view)
                     if e.payload["goal_id"] == view["goal_id"]}
        for event in self.log.read_types(("approval.decided",), task_id=view["task_id"]):
            if event.run_id not in phase_ids or event.payload["decision"] != "rejected":
                continue
            row = self.log.conn.execute(
                "SELECT d.payload FROM dispatch_requests d JOIN approvals a"
                " ON d.request_id=a.request_id WHERE a.approval_id=?",
                (event.payload["approval_id"],),
            ).fetchone()
            if row is None:
                raise GoalConflict("否决事件缺少具体动作绑定")
            digest = denied_content_digest(self.log, ActionRequest.of(json.loads(row["payload"])))
            if digest not in view["recovered_denial_digests"]:
                return event.seq, digest
        return None

    def _start(self, view, state, phase, *, plan=None, step=None, request_id,
               conversation=None, continuing=False):
        from .control import _exposed_tool_ids
        from .mission import MissionSpecV1, freeze_mission
        from .verification_driver import bind_verification
        task = self.tasks.get_task(view["task_id"])
        self._isolation(view["config"], task)
        if state["phase_run_id"]:
            raise GoalConflict("阶段 Run 尚未消费")
        new_round = not continuing and phase in ("plan", "replan", "execute") and (
            state["round"] == 0 or state["last_assessed_round"] == state["round"])
        if new_round:
            if state["round"] >= view["config"]["max_rounds"]:
                return self._block(view, "round_limit", request_id=request_id)
            state["round"] += 1
            state["execution_run_id"] = state["verification_run_id"] = None
        policy = PURE_ONLY if phase in ("plan", "replan") else RunExecutionPolicyV1.of(
            view["config"]["execution_policy"])
        tools = _exposed_tool_ids(policy)
        mission = self.mission(view["goal_id"])
        if phase in ("plan", "replan"):
            prompt = (
                "为以下固定 Mission 生成结构化任务图。仅输出一个封闭 JSON 对象，"
                "顶层字段精确为 plan 与 verification_manifest，不要 Markdown 围栏。"
                "遵循 PlanDocumentV1 和 VerificationCheckV1；所有条件必须覆盖，"
                "检查不能由模型声明通过。\nMission:\n" + json.dumps(mission, ensure_ascii=False)
            )
            from .goal_planning import planning_contract
            prompt += "\nSchema:\n" + json.dumps(planning_contract(), ensure_ascii=False)
            prompt += "\nMission digest: " + view["config"]["mission_digest"]
            if plan:
                prompt += "\nPrevious Plan:\n" + json.dumps(plan["document"], ensure_ascii=False)
                prompt += "\nPrevious checks:\n" + json.dumps(plan["manifest"], ensure_ascii=False)
        elif phase == "execute":
            prompt = (
                "仅完成已确认计划的当前一步，然后结束本 Run，随后由独立验证阶段核查。"
                "模型结束说明不代表目标完成。\n" + json.dumps(step, ensure_ascii=False)
            )
        else:
            checks = [c for c in plan["manifest"]["checks"]
                      if c["check_id"] in step["verification_ids"]]
            command = any(c["kind"] == "command_exit" for c in checks)
            policy, tools = (EFFECTS, ["run_command"]) if command else (PURE_ONLY, [])
            prompt = "确定性独立验证阶段；不调用模型。"
        # 所有会发现 MCP/执行/联网的操作都留给 claim 驱动；创建事务只建原生事实。
        self.stack.dispatcher.prepare_run(
            task.task_id, policy, discover_mcp=False, discover_remote=False,
        )
        run_id = "run_gv2_" + jcs_sha256({
            "goal_id": view["goal_id"], "round": state["round"], "phase": phase,
            "epoch": state["orchestration_epoch"],
        })[:24]
        self.tasks.start_run(task.task_id, run_id=run_id, execution_policy=policy, payload={
            "task": prompt, "out": "", "tools": tools,
            "limits": {"max_turns": self.limits.max_turns,
                       "max_total_tokens": self.limits.max_total_tokens},
        })
        self.log.append(run_id, "run.conversation", conversation or {"turns": []},
                        task_id=task.task_id, actor_kind="human")
        freeze_mission(self.log, run_id, mission=MissionSpecV1.of(mission),
                       registry=self.stack.dispatcher.registry)
        if phase == "execute":
            PlanStore(self.stack).change_step(
                plan["plan_id"], step["step_id"], "running", run_id=run_id,
                request_id=run_id + "_running")
            state["execution_run_id"] = run_id
        elif phase == "verify":
            bind_verification(self.stack, run_id, plan, step["step_id"], request_id=run_id)
            state["verification_run_id"] = run_id
        parent = "goal:" + view["goal_id"]
        self.stack.budget.create_account(
            "run:" + run_id, limits=dict(self.limits.budget_limits or {}), parent_id=parent,
            run_id=run_id, label="run", deadline_ms=view["config"]["deadline_ms"],
            bound=view["config"]["budget_bound"],
        )
        ensure_phase = {
            "format_version": 2, "goal_id": view["goal_id"],
            "expected_state_revision": view["state_revision"],
            "orchestration_epoch": state["orchestration_epoch"], "phase": phase,
            "round": state["round"], "mission_digest": view["config"]["mission_digest"],
            "plan_id": plan["plan_id"] if plan else None,
            "plan_digest": plan["document_digest"] if plan else None,
            "step_id": step["step_id"] if step else None, "request_id": request_id,
            "budget_account_id": parent,
        }
        self.log.append(run_id, PHASE_EVENT, ensure_phase, task_id=task.task_id, actor_kind="core")
        ensure_phase_budget(self.log, self.stack.budget, run_id,
                            self.limits.budget_limits, view["config"]["budget_bound"],
                            admission=False)
        self.stack.claims.enqueue(run_id)
        from .crashpoint import failpoint
        failpoint("goal-v2-before-phase-commit")
        state.update(status="active", phase=phase, phase_run_id=run_id,
                     step_id=step["step_id"] if step else None)
        return self._transition(view, state, cause="phase_started", request_id=request_id)

    def _assess(self, view, state, plan=None):
        if state["round"] == state["last_assessed_round"]:
            return
        satisfied, completed, evidence = set(), set(), set()
        if plan:
            for step in plan["steps"]:
                if (step["state"] == "satisfied"
                        and step_verified(self.stack, plan, step["step_id"])):
                    satisfied.update(step["criterion_ids"])
                    if step["step_id"] in state["baseline_step_ids"]:
                        completed.add(step["step_id"])
            for event in self.log.read_types(("verification.result",), task_id=view["task_id"]):
                if (event.payload["plan_id"] == plan["plan_id"]
                        and event.payload["revision"] == plan["revision"]
                        and event.payload["result"] == "passed"):
                    evidence.update(event.payload["evidence_refs"])
        old = set(state["satisfied_criterion_ids"]), set(state["completed_step_ids"])
        progress = bool(satisfied - old[0] or completed - old[1])
        # 历史机械进展只增不减；失效证据仍不能通过最终完成门。
        state["satisfied_criterion_ids"] = sorted(satisfied | old[0])
        state["completed_step_ids"] = sorted(completed | old[1])
        state["no_progress_streak"] = 0 if progress else state["no_progress_streak"] + 1
        if (state["blocker"] and state["blocker"]["safe_retry"]
                and state["no_progress_streak"] >= view["config"]["max_no_progress_rounds"]
                and state["blocker"]["category"] in ("verification_failed", "execution_failed")):
            state["blocker"]["category"] = "no_progress"
        state["progress_fingerprint"] = jcs_sha256([
            state["satisfied_criterion_ids"], state["completed_step_ids"]])
        state["last_assessed_round"] = state["round"]
        target = next((s["criterion_ids"] for s in (plan or {}).get("steps", [])
                       if s["step_id"] == state["step_id"]), [])
        self.log.append(state["verification_run_id"] or state["execution_run_id"] or "",
                        "goal.v2_assessed", {
            "format_version": 1, "goal_id": view["goal_id"], "round": state["round"],
            "mission_digest": view["config"]["mission_digest"],
            "plan_digest": state["plan_digest"], "target_criterion_ids": target,
            "execution_run_id": state["execution_run_id"],
            "verification_run_id": state["verification_run_id"],
            "satisfied_criterion_ids": sorted(satisfied), "completed_step_ids": sorted(completed),
            "remaining_step_ids": [s["step_id"] for s in (plan or {}).get("steps", [])
                                   if s["step_id"] not in completed],
            "evidence_refs": sorted(evidence), "blocker": state["blocker"],
            "progress_fingerprint": state["progress_fingerprint"],
            "no_progress_streak": state["no_progress_streak"],
        }, task_id=view["task_id"], actor_kind="core")

    def tick(self, goal_id, *, requested=False, request_id="", expected_state_revision=None,
             orchestration_epoch=None):
        self._reconcile_lifecycles(goal_id)
        result = self._tick(
            goal_id, requested=requested, request_id=request_id,
            expected_state_revision=expected_state_revision,
            orchestration_epoch=orchestration_epoch)
        from .crashpoint import failpoint
        failpoint("goal-v2-after-tick")
        return result

    def _tick(self, goal_id, *, requested=False, request_id="", expected_state_revision=None,
              orchestration_epoch=None):
        if request_id:
            checked_request_id(request_id)
        with self.log.transaction():
            view = self.get(goal_id)
            request_digest = jcs_sha256({
                "requested": requested, "expected_state_revision": expected_state_revision,
                "orchestration_epoch": orchestration_epoch,
            })
            if request_id:
                for e in self._events(goal_id):
                    if e.payload.get("request_id") == request_id:
                        if (e.type != "goal.v2_advance_requested"
                                or e.payload["request_digest"] != request_digest):
                            raise GoalConflict("request_id 已用于不同的 Goal 操作")
                        return view
            if (expected_state_revision is not None
                    and expected_state_revision != view["state_revision"]
                    or orchestration_epoch is not None
                    and orchestration_epoch != view["orchestration_epoch"]):
                raise GoalConflict("Goal tick CAS 冲突")
            if view["status"] in (*STOPPED, "paused", "blocked"):
                if requested:
                    raise GoalConflict("Goal 当前状态不允许推进")
                return view
            if request_id:
                self.log.append("", "goal.v2_advance_requested", {
                    "format_version": 2, "goal_id": goal_id, "request_id": request_id,
                    "request_digest": request_digest,
                }, task_id=view["task_id"], actor_kind="human" if requested else "core")
            state = self._state(view)
            clock_block = self._clock_state(view, state)
            if clock_block:
                return self._block(view, clock_block, request_id=request_id)
            denial = self._denial(view)
            if denial:
                return self._block(view, "approval_denied", source=denial[0],
                                   digest=denial[1], request_id=request_id)
            effects = self._effects_block(view)
            if effects:
                # 正在执行的正常 intent 不是故障，只有阶段停下/结束仍未结才核查。
                run = self.tasks.get_run(state["phase_run_id"]) if state["phase_run_id"] else None
                if run is None or run.state in (*TERMINAL_STATES, RUN_FENCED, "reconciling"):
                    return self._block(view, effects[0], source=effects[1], request_id=request_id)
            active = self.tasks.get_run(state["phase_run_id"]) if state["phase_run_id"] else None
            if active is None or active.state in (*TERMINAL_STATES, RUN_FENCED):
                lifecycle = self._lifecycle_block(view)
                if lifecycle:
                    return self._block(view, lifecycle[0], source=lifecycle[1],
                                       request_id=request_id)
            plan = PlanStore(self.stack).get(state["plan_id"]) if state["plan_id"] else None
            if state["phase"] == "ask":
                return view
            if state["phase_run_id"]:
                run = self.tasks.get_run(state["phase_run_id"])
                if run is None:
                    return self._block(view, "recovery_required", request_id=request_id)
                if run.state not in (*TERMINAL_STATES, RUN_FENCED):
                    status = "awaiting_input" if run.state == "waiting_approval" else "active"
                    if status != state["status"]:
                        state["status"] = status
                        return self._transition(view, state, cause="phase_waiting",
                                                request_id=request_id)
                    return view
                if self.tasks.list_subtasks(run.run_id) and any(
                    s.state not in ("succeeded", "failed", "cancelled", "fenced")
                    for s in self.tasks.list_subtasks(run.run_id)
                ):
                    return self._block(view, "recovery_required", request_id=request_id)
                if run.state != "succeeded":
                    refused = self.log.read_run(
                        run.run_id, event_types=("goal.v2_admission_refused",))
                    category = refused[-1].payload["category"] if refused else "execution_failed"
                    return self._block(
                        view, category, source=refused[-1].seq if refused else 0,
                        safe=run.effect_state == "none_applied", request_id=request_id)
                from .conversation import clarification_question
                if clarification_question(self.log.read_run(run.run_id)):
                    state.update(phase="ask", status="awaiting_input")
                    return self._transition(view, state, cause="clarification_required",
                                            request_id=request_id)
                if state["phase"] == "ask":
                    return view
                if state["phase"] in ("plan", "replan"):
                    try:
                        from .goal_planning import consume_candidate
                        plan = consume_candidate(self, view, run.run_id)
                    except (ValueError, TianshuError, KeyError, TypeError):
                        return self._block(view, "protocol_error", request_id=request_id)
                    state.update(plan_id=plan["plan_id"], plan_digest=plan["document_digest"],
                                 phase_run_id=None, status="awaiting_review", phase="execute")
                    return self._transition(view, state, cause="plan_candidate",
                                            request_id=request_id)
                if state["phase"] == "execute":
                    plan = PlanStore(self.stack).change_step(
                        state["plan_id"], state["step_id"], "verifying",
                        request_id=run.run_id + "_verifying")
                    state.update(phase="verify", phase_run_id=None, status="ready")
                    view = self._transition(view, state, cause="execution_finished",
                                            request_id=request_id)
                    state = self._state(view)
                elif state["phase"] == "verify":
                    state["phase_run_id"] = None
                    step = next(s for s in plan["steps"] if s["step_id"] == state["step_id"])
                    if step["state"] == "verifying":
                        state.update(status="awaiting_review")
                        return self._transition(view, state, cause="human_verification",
                                                request_id=request_id)
                    if step["state"] != "satisfied":
                        return self._block(view, "verification_failed", safe=True,
                                           request_id=request_id)
                    self._assess(view, state, plan)
                    state.update(phase="finish", status="ready")
                    view = self._transition(view, state, cause="verification_finished",
                                            request_id=request_id)
                    state = self._state(view)
            if plan and not plan["confirmed"]:
                return view
            if plan and any(
                e.actor_kind == "human" and e.payload["plan_id"] == plan["plan_id"]
                and e.payload["revision"] == plan["revision"]
                for e in self.log.read_types(("plan.confirmed",), task_id=view["task_id"])
            ):
                state["baseline_step_ids"] = sorted(
                    set(state["baseline_step_ids"]) | {s["step_id"] for s in plan["steps"]})
            if state["phase"] == "verify" and not state["phase_run_id"]:
                step = next(s for s in plan["steps"] if s["step_id"] == state["step_id"])
                if step["state"] == "verifying" and state["verification_run_id"]:
                    return view
                if step["state"] == "satisfied":
                    self._assess(view, state, plan)
                    state.update(phase="finish", status="ready")
                    view = self._transition(view, state, cause="human_verified")
                    state = self._state(view)
            if plan and all(s["state"] == "satisfied" for s in plan["steps"]):
                budget_block = self._budget_block(view, new_request=False)
                if budget_block:
                    return self._block(view, budget_block, request_id=request_id)
                if self._complete(view, plan):
                    if view["config"]["completion_policy"] == "verified":
                        if all(c["mode"] == "machine" for c in self.mission(goal_id)["criteria"]):
                            state.update(status="completed", phase="finish")
                            return self._transition(view, state, cause="verified_complete",
                                                    event="goal.v2_closed", request_id=request_id)
                    state.update(status="awaiting_review", phase="finish")
                    if state != self._state(view):
                        return self._transition(view, state, cause="awaiting_finish",
                                                request_id=request_id)
                    return view
                return self._block(view, "verification_failed", request_id=request_id)
            if state["no_progress_streak"] >= view["config"]["max_no_progress_rounds"]:
                return self._block(view, "no_progress", request_id=request_id)
            if plan and any(s["state"] == "invalidated" for s in plan["steps"]):
                return self._block(view, "verification_failed", safe=True, request_id=request_id)
            budget_block = self._budget_block(view, new_request=state["phase"] != "verify")
            if budget_block:
                return self._block(view, budget_block, request_id=request_id)
            allowed = requested or (
                view["config"]["mode"] == "bounded_auto" and state["round"] > 0)
            if not allowed:
                return view
            if state["phase"] in ("plan", "replan"):
                return self._start(view, state, state["phase"], plan=plan, request_id=request_id)
            if plan:
                phase = "verify" if state["phase"] == "verify" else "execute"
                step = (next(s for s in plan["steps"] if s["step_id"] == state["step_id"])
                        if phase == "verify" else next(
                            (s for s in plan["steps"] if s["state"] == "ready"), None))
                if step:
                    return self._start(view, state, phase, plan=plan, step=step,
                                       request_id=request_id)
            return view

    def _complete(self, view, plan):
        if (not plan["confirmed"] or self._effects_block(view) or self._lifecycle_block(view)
                or any(not step_verified(self.stack, plan, s["step_id"]) for s in plan["steps"])
                or self.tasks.get_task(view["task_id"]).active_run_id):
            return False
        if self.log.conn.execute(
            "SELECT 1 FROM subtasks WHERE parent_task_id=?"
            " AND state NOT IN ('succeeded','failed','cancelled','fenced') LIMIT 1",
            (view["task_id"],),
        ).fetchone():
            return False
        for run_id in self._resource_runs(view):
            run = self.tasks.get_run(run_id)
            if (not run or run.state not in (*TERMINAL_STATES, RUN_FENCED)
                    or self.stack.leases.list_for_run(run_id)):
                return False
        return True

    def tick_all(self):
        changed = False
        for event in self.log.read_types(("goal.v2_created",)):
            goal_id = event.payload["config"]["goal_id"]
            previous = self.get(goal_id)
            # _tick 在 BEGIN IMMEDIATE 后读取当前 CAS；并发扫描不携带过期的内存修订。
            current = self.tick(goal_id)
            changed |= current["state_revision"] != previous["state_revision"]
        return changed

    def answer(self, source_run_id, message, request_id):
        from .conversation import build_follow_up, clarification_question
        request_id = checked_request_id(request_id)
        phase = phase_for(self.log, source_run_id)
        if phase is None:
            raise GoalConflict("澄清来源不是 Goal 阶段")
        with self.log.transaction():
            view = self.get(phase["goal_id"])
            for event in self._events(view["goal_id"]):
                if event.type == PHASE_EVENT and event.payload["request_id"] == request_id:
                    fact = self.log.read_run(event.run_id, event_types=("run.conversation",))
                    if not fact or fact[0].payload["turns"][-1]["message"] != message:
                        raise GoalConflict("request_id 已用于不同的澄清答复")
                    return event.run_id
            if (view["status"] != "awaiting_input" or view["phase"] != "ask"
                    or view["phase_run_id"] != source_run_id):
                raise GoalConflict("Goal 不再等待该澄清")
            events = self.log.read_run(source_run_id)
            if not clarification_question(events):
                raise GoalConflict("来源不是没有副作用的已完成澄清")
            state = self._state(view)
            category = self._clock_state(view, state) or self._budget_block(view)
            if category:
                self._block(view, category, request_id=request_id)
                return None
            plan = PlanStore(self.stack).get(state["plan_id"]) if state["plan_id"] else None
            step = next((s for s in (plan or {}).get("steps", [])
                         if s["step_id"] == state["step_id"]), None)
            if step:
                for target in ("blocked", "ready"):
                    plan = PlanStore(self.stack).change_step(
                        plan["plan_id"], step["step_id"], target,
                        reason="clarification_answer", request_id=request_id + "_" + target)
                step = next(s for s in plan["steps"] if s["step_id"] == step["step_id"])
            state["orchestration_epoch"] += 1
            state["phase_run_id"] = None
            view = self._transition(view, state, cause="clarification_answer",
                                    request_id=request_id + "_accepted", human=True)
            result = self._start(
                view, self._state(view), phase["phase"], plan=plan, step=step,
                request_id=request_id, conversation=build_follow_up(source_run_id, events, message),
                continuing=True)
            return result["phase_run_id"]

    def control(self, goal_id, action, params):
        request = checked_request_id(params["request_id"])
        if action == "recover" or action == "close" and params.get("outcome") == "completed":
            self._reconcile_lifecycles(goal_id)
        with self.log.transaction():
            view = self.get(goal_id)
            request_digest = jcs_sha256({"action": action, "params": params})
            for event in self._events(goal_id):
                if event.payload.get("request_id") == request:
                    if event.payload.get("cause") != "human:" + action + ":" + request_digest:
                        raise GoalConflict("request_id 已用于不同的 Goal 操作")
                    return view
            expected = params.get("expected_state_revision", view["state_revision"])
            if expected != view["state_revision"]:
                raise GoalConflict("Goal 状态修订已经改变")
            state = self._state(view)
            state["orchestration_epoch"] += 1
            extra, event = None, "goal.v2_transitioned"
            if action == "pause":
                if view["status"] in (*STOPPED, "blocked", "paused"):
                    raise GoalConflict("当前 Goal 不允许暂停")
                state["status"] = "paused"
                if state["phase_run_id"]:
                    run = self.tasks.get_run(state["phase_run_id"])
                    if run.state not in TERMINAL_STATES:
                        self.tasks.request_pause(run.run_id)
            elif action == "resume":
                if view["status"] != "paused":
                    raise GoalConflict("resume 只恢复普通暂停")
                state["status"] = "active" if state["phase_run_id"] else "ready"
                if state["phase_run_id"]:
                    run = self.tasks.get_run(state["phase_run_id"])
                    if run.control_requested == "pause":
                        raise GoalConflict("活跃 Run 尚未确认暂停，请等待安全点")
                    if run.state == "paused":
                        self.tasks.transition(run.run_id, "queued", reason="goal_human_resume")
                    job = self.stack.claims.job_for_run(run.run_id)
                    if job and job.state == "waiting":
                        self.stack.claims.wake(job.job_id, reason="goal_human_resume")
                    elif not job or job.state not in ("pending", "claimed"):
                        self.stack.claims.enqueue(run.run_id)
            elif action == "recover":
                block = state["blocker"]
                if (view["status"] != "blocked" or params["expected_block_event_seq"] !=
                        view["state_revision"]):
                    raise GoalConflict("阻断事实已改变或页面过期")
                if block["category"] in HARD_BLOCKS:
                    raise GoalConflict("恢复不能重置轮次、预算或期限上限")
                if self._effects_block(view):
                    raise GoalConflict("未知副作用尚无原工具核对证据，不能恢复")
                if self._lifecycle_block(view):
                    raise GoalConflict("执行生命周期尚无已证明的整树退出证据，不能恢复")
                if block["category"] == "usage_unknown" and self._budget_block(view):
                    raise GoalConflict("未知费用或用量尚未核对")
                if self._clock_state(view, state) or self._budget_block(view):
                    raise GoalConflict("剩余期限或预算不足以恢复")
                resolution = params["resolution"]
                if resolution not in RESOLUTIONS:
                    raise ValueError("未知恢复决定")
                refs = params["evidence_refs"]
                if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
                    raise ValueError("evidence_refs 必须为引用列表")
                for ref in refs:
                    read_material(self.stack, view["task_id"], ref)
                digest = params.get("denied_content_digest")
                if block["category"] == "approval_denied":
                    if resolution != "change_decision" or digest != block["denied_content_digest"]:
                        raise GoalConflict("改变否决必须明确绑定原决定及精确内容摘要")
                    state["recovered_denial_digests"].append(digest)
                elif resolution == "change_decision":
                    raise GoalConflict("当前阻断不是人工否决")
                if (block["category"] == "effect_unknown"
                        and (resolution != "reconciled" or not refs)):
                    raise GoalConflict("副作用恢复必须引用工具的核对证据")
                if block["category"] == "no_progress":
                    if resolution != "new_observation":
                        raise GoalConflict("无进展只能由人建立新观察段")
                    # 保留历史 assessment；此字段只表示新观察段连续值。
                    state["no_progress_streak"] = 0
                if state["phase_run_id"]:
                    run = self.tasks.get_run(state["phase_run_id"])
                    if run and run.state not in (*TERMINAL_STATES, RUN_FENCED):
                        self.tasks.request_cancel(run.run_id)
                        run = self.tasks.get_run(run.run_id)
                        if run.state not in (*TERMINAL_STATES, RUN_FENCED):
                            raise GoalConflict("原阶段尚未安全终结，不能建立新的阶段")
                state.update(status="ready", blocker=None, phase_run_id=None)
                if state["plan_id"]:
                    plan = PlanStore(self.stack).get(state["plan_id"])
                    step = next(
                        (s for s in plan["steps"] if s["step_id"] == state["step_id"]), None)
                    if step and step["state"] == "invalidated" and resolution != "replan":
                        raise GoalConflict("验收输入已经改变，必须重新规划并验证")
                    if step and step["state"] in ("running", "verifying"):
                        plan = PlanStore(self.stack).change_step(
                            plan["plan_id"], step["step_id"], "failed",
                            reason="goal.human_recovery", request_id=request + "_failed")
                        step = next(s for s in plan["steps"] if s["step_id"] == state["step_id"])
                    if resolution == "replan":
                        state["phase"] = "replan"
                    else:
                        state["phase"] = "execute"
                        if step and step["state"] in ("failed", "blocked"):
                            PlanStore(self.stack).change_step(
                                plan["plan_id"], step["step_id"], "ready",
                                request_id=request + "_ready")
                else:
                    state["phase"] = "plan"
                extra = {
                    "expected_block_event_seq": params["expected_block_event_seq"],
                    "resolution": resolution, "evidence_refs": refs,
                    "denied_content_digest": digest,
                }
                event = "goal.v2_recovered"
            elif action == "close":
                outcome = params["outcome"]
                if outcome not in ("completed", "cancelled"):
                    raise ValueError("outcome 必须为 completed 或 cancelled")
                if view["status"] in STOPPED:
                    raise GoalConflict("Goal 已关闭")
                if outcome == "completed":
                    if self._clock_state(view, state):
                        raise GoalConflict("完成确认已经超过原截止时间")
                    if self._budget_block(view) == "usage_unknown":
                        raise GoalConflict("存在尚未核对的费用或用量")
                    plan = PlanStore(self.stack).get(state["plan_id"]) if state["plan_id"] else None
                    if (view["status"] == "blocked" or not plan
                            or not all(s["state"] == "satisfied" for s in plan["steps"])
                            or not self._complete(view, plan)):
                        raise GoalConflict("尚未通过全部新鲜验收和未决副作用完成门")
                else:
                    for phase in self._phase_events(view):
                        if phase.payload["goal_id"] == goal_id:
                            self.tasks.request_cancel(phase.run_id)
                    self.stack.budget.cancel("goal:" + goal_id)
                state.update(status=outcome, phase="finish")
                event = "goal.v2_closed"
            else:
                raise ValueError("未知 Goal 控制动作")
            return self._transition(
                view, state, cause="human:" + action + ":" + request_digest,
                request_id=request, event=event, extra=extra, human=True)
