"""人触发的 Goal 事件派生视图；Run 成功与人工验收是不同事实。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError
from .run_policy import RunExecutionPolicyV1
from .tasks import RUN_FENCED, TERMINAL_STATES
from .workflow import final_text

GOAL_EVENTS = ("goal.created", "goal.round_started", "goal.closed")
GOAL_STATUSES = (
    "ready", "running", "awaiting_review", "exhausted", "blocked",
    "stale", "completed", "cancelled",
)
GOAL_OUTCOMES = ("completed", "cancelled")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def request_id(value: Any) -> str:
    if not isinstance(value, str) or REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id 必须为 1–128 位字母、数字、下划线或连字符")
    return value


def configuration(params: dict[str, Any]) -> dict[str, Any]:
    """RPC 与事件读取共用范围检查；不把布尔值当成整数。"""
    criteria = params.get("criteria")
    if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 12
            or any(not isinstance(c, str) or not c.strip()
                   or len(c.encode("utf-8")) > 1024 for c in criteria)):
        raise ValueError("验收条件需为 1–12 条非空文本，每条不超过 1024 UTF-8 字节")
    rounds = params.get("max_rounds", 3)
    if type(rounds) is not int or not 1 <= rounds <= 10:
        raise ValueError("max_rounds 必须为 1–10 的整数")
    revision = params.get("goal_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("goal_revision 必须为正整数")
    try:
        policy = RunExecutionPolicyV1.of(params["execution_policy"])
    except (TianshuError, KeyError) as exc:
        raise ValueError("execution_policy 必须为有效的 V1 执行资格") from exc
    return {
        "format_version": 1, "goal_revision": revision,
        "request_id": request_id(params.get("request_id")),
        "criteria": list(criteria), "max_rounds": rounds,
        "execution_policy": policy.to_dict(),
    }


@dataclass
class Goal:
    task_id: str
    config: dict[str, Any]
    rounds: list[Any] = field(default_factory=list)
    closing: Any = None

    @property
    def goal_id(self) -> str:
        return self.config["goal_id"]

    def successful_run(self, tasks, log):
        if not self.rounds:
            return None
        run = tasks.get_run(self.rounds[-1].run_id)
        if run is None:
            raise TianshuError("Goal Run 投影缺失，请重建后再试")
        if run.effect_state in ("partial", "unknown"):
            return None
        try:
            final_text(log, run)
        except TianshuError:
            return None
        return run

    def view(self, tasks, log) -> dict[str, Any]:
        task = tasks.get_task(self.task_id)
        if task is None:
            raise TianshuError("Goal Task 投影缺失，请重建后再试")
        if self.closing is not None:
            status = self.closing.payload["outcome"]
        elif task.goal_revision != self.config["goal_revision"]:
            status = "stale"
        elif not self.rounds:
            status = "ready"
        else:
            run = tasks.get_run(self.rounds[-1].run_id)
            if run is None:
                raise TianshuError("Goal Run 投影缺失，请重建后再试")
            if run.state not in TERMINAL_STATES and run.state != RUN_FENCED:
                status = "running"
            elif self.successful_run(tasks, log) is None:
                status = "blocked"
            else:
                status = ("exhausted" if len(self.rounds) >= self.config["max_rounds"]
                          else "awaiting_review")
        return {
            "format_version": 1, "goal_id": self.goal_id, "task_id": self.task_id,
            "goal_revision": self.config["goal_revision"],
            "criteria": self.config["criteria"], "max_rounds": self.config["max_rounds"],
            "execution_policy": self.config["execution_policy"],
            "rounds_started": len(self.rounds),
            "latest_run_id": self.rounds[-1].run_id if self.rounds else None,
            "status": status,
            "confirmed_criteria": (
                self.closing.payload["confirmed_criteria"] if self.closing else []
            ),
        }


def for_task(log, task_id: str) -> list[Goal]:
    """只读当前 Task 的已校验事件，任何派生状态都可重新计算。"""
    found: dict[str, Goal] = {}
    for event in log.read_types(GOAL_EVENTS, task_id=task_id):
        p = event.payload
        goal_id = p["goal_id"]
        if p["format_version"] != 1 or type(p["format_version"]) is not int:
            raise TianshuError("Goal 事件版本无效")
        if event.type == "goal.created":
            if goal_id in found:
                raise TianshuError("重复的 Goal 创建事件")
            configuration(p)
            found[goal_id] = Goal(task_id, p)
            continue
        goal = found.get(goal_id)
        if goal is None or goal.closing is not None:
            raise TianshuError("Goal 事件缺少创建记录或发生在关闭之后")
        if event.type == "goal.round_started":
            source = goal.rounds[-1].run_id if goal.rounds else None
            if (p["round"] != len(goal.rounds) + 1
                    or p["round"] > goal.config["max_rounds"]
                    or p["source_run_id"] != source):
                raise TianshuError("Goal 轮次或来源链损坏")
            goal.rounds.append(event)
        else:
            goal.closing = event
    return list(found.values())
