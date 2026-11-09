"""控制面：把 IPC 方法表（`docs/M5-desktop.md` §3.4）映射到既有库函数。

## 为什么要有这一层，而不是让 daemon 直接调库

三件事需要集中在一处，而不是散在 socket 循环里：

1. **方法名/参数/结果形状是契约。** UI 直接消费 `TaskView` / `RunView`；把
   `dataclass` 换个字段名就悄悄改变契约。这里只投影契约列出的字段——尤其
   **不放 digest**：人和模型之间不该靠一串哈希沟通（§3.4）。
2. **错误要能按 `code` 分支。** 库里的异常是 Python 类型，传输层要的是
   `protocol_mismatch` / `bad_params` / `not_found` / `conflict` ... 的字符串。
   映射规则只有一份，CLI/Host/UI 才不会各自解释一套。
3. **可测性。** 这一层是纯逻辑：给一个 `Stack` 就能跑，不需要 socket、不需要
   起线程。`tests/test_control.py` 因此能直接核对"`run.start` 只入队"。

## 这一层不做什么

- **不写文件、不判定策略、不绕过审批。** 每个状态变更都调用 `TaskStore` /
  `ApprovalStore` / `Dispatcher` / `worker` 里的既有函数；撤销走 `undo.py`，
  它自己会经派发/审批路径落盘。
- **不驱动 Run。** `run.start` / `run.resume` / `approvals.decide` 只把 Run 放进
  `resume_requests`，真正执行在 daemon 的驱动线程里（`AgentLoop.resume_run`）。
  `TaskStore.start_run` 建的 Run 是 `created`，**不要**在前台线程把它
  `transition` 到 `running`——那是认领者的职责，前台替它做等于伪造"有人在跑"。
- **不拥有推送/连接。** `events.run` 只回答一次查询；`subscribe` 的补洞与增量
  推送由 daemon 的主循环负责。

## 线程模型

一个 `ControlPlane` 只属于**一个**线程（daemon 的主循环）。SQLite 连接不能跨
线程共享，所以驱动线程用自己的一套 `Stack`（daemon 里装配两次，各自持有自己的
`EventLog` 连接）。这一层只碰自己那份连接。
"""

from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import RunFenced, TianshuError, ToolArgumentError, WorkspaceViolation
from .execpolicy import ExecPolicy
from .loop import Limits, Stack
from .permissions import ApprovalError, HumanIdentity
from .tasks import (
    RUN_FENCED,
    RUN_PAUSED,
    RUN_RECONCILING,
    RUN_WAITING_APPROVAL,
    TERMINAL_STATES,
)
from .undo import UndoRefused, apply_undo, plan_undo

#: daemon 的版本号。加字段不升协议版本（§3.3），这个号是给人看的。
DAEMON_VERSION = "0.1.0"

#: `state` / `task.list` 一次最多返回多少任务。首屏不该把十年历史全塞过去。
DEFAULT_TASK_LIMIT = 50
MAX_TASK_LIMIT = 200

#: `state.runs` 里保留的终态 Run 数量（§3.4：非终态 + 最近 20 个终态）。
RECENT_TERMINAL_RUNS = 20

#: `events.run` 的分页默认值与上限。UI 的时间线一次拉一屏。
DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 1000

#: `task.create` 的 exec 策略默认值，与 `cli.py` 的 `--allow-exec-cmd` 同口径。
#: 不能 import cli（会把 argparse/模型构造拖进 Daemon 进程），所以这里显式重复
#: 两个常量；它们一旦分叉，`tests/test_control.py` 的 exec 用例会先红。
EXEC_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
EXEC_DEFAULT_TIMEOUT_MS = 30_000

#: `approvals.decide` 的机器枚举 → 库里的 `approved`/`rejected`。
_DECISION_MAP = {"approve": "approved", "reject": "rejected"}

#: 这些状态下 Run 已经停下、作业可能已 park，`run resume` 才有意义。
#: 其余状态（created/queued/running/cancelling）作业已经在队里或有人持有，
#: 再塞一条 resume 只是跟正在跑的 owner 抢 claim。
RUN_PENDING_RESUME_STATES = frozenset(
    {RUN_PAUSED, RUN_WAITING_APPROVAL, RUN_RECONCILING}
)


class ControlError(Exception):
    """控制面可预期的失败。`code` 是契约里的错误码（§3.2）。

    `details` 只放解释性数据（哪张审批、为什么不行），**不放 digest**——
    和视图同一条规矩。daemon 把它原样转成 `ipc.fail(...)`。
    """

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


# --------------------------------------------------------------------------- 视图
#
# 字段集合就是契约（§3.4 的 TaskView/RunView/ApprovalView/EventView）。多放一个
# 字段等于偷偷扩大契约；少放一个会让 UI 自己猜。`tests/test_control.py` 用精确
# 集合断言钉住它们。


def task_view(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "goal": task.goal,
        "goal_revision": int(task.goal_revision),
        "status": task.status,
        "active_run_id": task.active_run_id,
        "workspaces": [binding.to_dict() for binding in task.workspaces],
        "created_at": int(task.created_at),
        "updated_at": int(task.updated_at),
    }


def event_view(event: Any) -> dict[str, Any]:
    return {
        "seq": int(event.seq),
        "type": event.type,
        "run_id": event.run_id,
        "task_id": event.task_id,
        "ts": int(event.ts),
        "actor_kind": event.actor_kind,
        "payload": event.payload,
    }


def approval_view(approval: Any, *, rule_id: str = "") -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "run_id": approval.run_id,
        "task_id": approval.task_id,
        "action": approval.action,
        "resources": list(approval.resources),
        "reason": approval.reason,
        "rule_id": rule_id,
        "state": approval.state,
        "created_at": int(approval.created_at),
        "expires_at": int(approval.expires_at),
    }


class ControlPlane:
    """RPC 方法名 → 库函数的唯一映射。

    `model` / `limits` 只用于拼 `run.start` 的 payload（与 `AgentLoop.start`
    的字段逐字一致）：真正的模型调用发生在驱动线程，这一层不碰 model。
    """

    def __init__(
        self,
        stack: Stack,
        *,
        model: Any,
        limits: Limits | None = None,
        resume_requests: queue.Queue[str] | None = None,
    ) -> None:
        self._stack = stack
        self._model = model
        self._limits = limits or Limits()
        self._tasks = stack.tasks
        self._claims = stack.claims
        self._approvals = stack.approvals
        self._log = stack.log
        #: 驱动线程的唤醒队列。为 None 时 `run.resume`/`approvals.decide` 只记视图，
        #: 不承诺"已交给驱动"——单测（没有驱动线程）就是这样用的。
        self.resume_requests = resume_requests
        #: 同一个 Run 在驱动线程取走之前只入队一次。否则一次审批决定会把它塞
        #: 两遍，驱动线程的第二趟 resume 就会撞上"已终结/正被持有"。
        self._resume_inflight: set[str] = set()
        self._resume_lock = threading.Lock()
        #: daemon 装配后填进来的身份信息（`state.daemon` 用）。空串/0 表示
        #: 脱离 daemon 单独使用（测试）。
        self.instance_id = ""
        self.started_at = 0
        self._closed = False
        self._methods: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "state": self._state,
            "task.list": self._task_list,
            "task.create": self._task_create,
            "task.revise": self._task_revise,
            "run.start": self._run_start,
            "run.list": self._run_list,
            "run.status": self._run_status,
            "run.pause": self._run_pause,
            "run.resume": self._run_resume,
            "run.cancel": self._run_cancel,
            "approvals.list": self._approvals_list,
            "approvals.decide": self._approvals_decide,
            "events.run": self._events_run,
            "undo.plan": self._undo_plan,
            "undo.apply": self._undo_apply,
        }

    # ------------------------------------------------------------------ 生命周期

    @property
    def event_log(self):
        """本线程的事件日志连接（daemon 的推送循环用它补洞）。"""
        return self._log

    @property
    def stack(self) -> Stack:
        return self._stack

    def event_seq_head(self) -> int:
        return self._log.seq_head()

    def close(self) -> None:
        """关闭本控制面持有的那条 SQLite 连接。可重复调用。"""
        if self._closed:
            return
        self._closed = True
        self._log.close()

    # ------------------------------------------------------------------ 入口

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """执行一个 RPC 方法。所有失败都抛 `ControlError`（daemon 转成 fail 帧）。"""
        handler = self._methods.get(method)
        if handler is None:
            raise ControlError(
                "unknown_method",
                f"未知方法：{method}",
                {"method": method, "known": sorted(self._methods)},
            )
        if not isinstance(params, dict):
            raise ControlError("bad_params", f"params 必须是对象：{params!r}")
        try:
            return handler(params)
        except ControlError:
            raise
        except RunFenced as exc:
            # 状态不允许（已被 fence / 已终结 / 有别的活跃 Run）→ conflict。
            raise ControlError("conflict", str(exc)) from exc
        except ApprovalError as exc:
            raise self._approval_error(exc) from exc
        except (WorkspaceViolation, ToolArgumentError, ValueError, TypeError) as exc:
            raise ControlError("bad_params", str(exc)) from exc
        except KeyError as exc:
            raise ControlError("bad_params", f"缺少参数：{exc}") from exc
        except TianshuError as exc:
            raise self._generic_error(exc) from exc
        except OSError as exc:
            raise ControlError("internal", f"I/O 失败：{exc}") from exc

    # ------------------------------------------------------------------ state

    def _state(self, params: dict[str, Any]) -> dict[str, Any]:
        # 不用 params：首屏永远按默认条数返回。UI 要更多自己调 task.list。
        tasks = self._list_tasks(DEFAULT_TASK_LIMIT)
        runs = self._list_recent_runs()
        return {
            "tasks": [task_view(task) for task in tasks],
            "runs": [self._run_view(run) for run in runs],
            "approvals": self._list_approvals(None),
            "event_seq_head": self.event_seq_head(),
            "daemon": self._daemon_view(),
        }

    def _daemon_view(self) -> dict[str, Any]:
        """`state.daemon`：契约没有逐字段规定，这里只给排障必需的五个。

        不提供 `digest`、`token`、authkey——§7 的安全边界要求这些不进日志/视图。
        """
        data_root = self._stack.data_root
        return {
            "daemon_version": DAEMON_VERSION,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "data_root": "" if data_root is None else str(data_root),
            "started_at": int(self.started_at),
        }

    # ------------------------------------------------------------------ task

    def _list_tasks(self, limit: int) -> list[Any]:
        """按更新时间倒序取任务。

        `TaskStore` 没有 `list_tasks()`（本里程碑不允许改 `tasks.py`），所以这里
        只做一次**只读**投影查询拿 id，再走公有的 `get_task()` 反序列化——
        不自己拼 Task 对象，避免第二个"任务长什么样"的定义。
        """
        rows = self._tasks.conn.execute(
            "SELECT task_id FROM tasks ORDER BY updated_at DESC, created_at DESC"
            " LIMIT ?",
            (int(limit),),
        ).fetchall()
        tasks = []
        for row in rows:
            task = self._tasks.get_task(row["task_id"])
            if task is not None:
                tasks.append(task)
        return tasks

    def _task_list(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = _opt_int(params, "limit", DEFAULT_TASK_LIMIT, 1, MAX_TASK_LIMIT)
        return {"tasks": [task_view(task) for task in self._list_tasks(limit)]}

    def _task_create(self, params: dict[str, Any]) -> dict[str, Any]:
        goal = _require_str(params, "goal")
        workspaces = params.get("workspaces") or []
        if not isinstance(workspaces, list):
            raise ControlError("bad_params", "workspaces 必须是列表")
        exec_policy = self._exec_policy(params, workspaces)
        task = self._tasks.create_task(goal, workspaces, exec_policy=exec_policy)
        return {"task": task_view(task)}

    @staticmethod
    def _exec_policy(
        params: dict[str, Any], workspaces: list[Any]
    ) -> ExecPolicy | None:
        """复刻 `cli.py --allow-exec-cmd` 的策略形状（同一套默认值）。

        参数是**可执行文件绝对路径列表**（与 CLI 同形）；布尔值会被拒绝：
        没有允许清单的"开启 exec"等于让模型随便执行什么，这不是一个可授权的
        请求，而是缺参数。
        """
        raw = params.get("allow_exec_cmd")
        if raw is None or raw is False:
            return None
        if raw is True:
            raise ControlError(
                "bad_params",
                "allow_exec_cmd 需要可执行文件绝对路径列表（不能只给 true）",
            )
        if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
            raise ControlError(
                "bad_params", "allow_exec_cmd 必须是可执行文件绝对路径的字符串列表"
            )
        writable: list[str] = []
        for item in workspaces:
            binding = item if isinstance(item, dict) else None
            mode = (binding or {}).get("mode", "writable")
            root = (binding or {}).get("root_path")
            if mode == "writable" and isinstance(root, str):
                writable.append(root)
        return ExecPolicy(
            argv_allowlist=tuple(raw),
            cwd_roots=tuple(writable),
            env_allowlist=("PATH",),
            max_output_bytes=EXEC_DEFAULT_MAX_OUTPUT_BYTES,
            timeout_ms=EXEC_DEFAULT_TIMEOUT_MS,
        )

    def _task_revise(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_str(params, "task_id")
        goal = _require_str(params, "goal")
        if self._tasks.get_task(task_id) is None:
            raise ControlError("not_found", f"task 不存在：{task_id}")
        task = self._tasks.revise_task(task_id, goal=goal)
        return {"task": task_view(task)}

    # ------------------------------------------------------------------ run

    def _run_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """**只入队，不驱动。** 立即返回 RunView（state=created）。

        payload 的形状与 `AgentLoop.start` 逐字一致（`task` / `out` / `limits` /
        `tools`）：`run.start` 事件是回放的输入，少了这些字段，`replay` 就无法
        仅凭日志重建首轮提示消息。
        """
        task_id = _require_str(params, "task_id")
        out_name = params.get("out_name")
        if out_name is not None and not isinstance(out_name, str):
            raise ControlError("bad_params", "out_name 必须是字符串")
        task = self._tasks.get_task(task_id)
        if task is None:
            raise ControlError("not_found", f"task 不存在：{task_id}")
        payload = {
            "task": task.goal,
            "out": out_name or "",
            "limits": {
                "max_turns": self._limits.max_turns,
                "max_total_tokens": self._limits.max_total_tokens,
            },
            "tools": _exposed_tool_ids(),
        }
        run = self._tasks.start_run(task.task_id, payload=payload)
        # 作业进队列；认领者（daemon 驱动线程）负责 created → queued → running。
        if self._claims is None:
            raise ControlError("internal", "Stack 未配置 JobStore，无法入队")
        self._claims.enqueue(run.run_id)
        return {"run": self._run_view(run)}

    def _run_list(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_str(params, "task_id")
        if self._tasks.get_task(task_id) is None:
            raise ControlError("not_found", f"task 不存在：{task_id}")
        return {
            "runs": [
                self._run_view(run) for run in self._tasks.list_runs(task_id)
            ]
        }

    def _run_status(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(_require_str(params, "run_id"))
        return {"run": self._run_view(run)}

    def _run_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(_require_str(params, "run_id"))
        # request_* 只记录请求/就地生效；执行器在安全点确认（PRD §4.3）。
        return {"run": self._run_view(self._tasks.request_pause(run.run_id))}

    def _run_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        """只把 Run 放进驱动线程的队列；返回**当前** RunView。

        `resumed` 语义与 `approvals.decide` 一致：它只表示"已交给驱动线程"，
        不表示"已经跑起来了"。控制请求与生效状态本来就是两件事（PRD §4.3）。
        """
        run = self._require_run(_require_str(params, "run_id"))
        if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
            raise ControlError(
                "conflict",
                f"run 已终结（{run.state}），不能 resume；需要重试请 start 新 run",
            )
        if run.state in RUN_PENDING_RESUME_STATES:
            self._enqueue_resume(run.run_id)
        # `created` / `queued` / `running` / `cancelling`：作业已经在队里或正在跑，
        # 再塞一条 resume 只会跟正在跑的 owner 抢 claim。什么都不做。
        return {"run": self._run_view(run)}

    def _run_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(_require_str(params, "run_id"))
        return {"run": self._run_view(self._tasks.request_cancel(run.run_id))}

    def _require_run(self, run_id: str) -> Any:
        run = self._tasks.get_run(run_id)
        if run is None:
            raise ControlError("not_found", f"run 不存在：{run_id}")
        return run

    def _run_view(self, run: Any) -> dict[str, Any]:
        counts = _EffectCounts.of(self._log.read_run(run.run_id, verify=False))
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "state": run.state,
            "outcome": run.outcome,
            "effect_state": run.effect_state,
            "control_requested": run.control_requested,
            "goal_revision": int(run.goal_revision),
            "lease_epoch": int(run.lease_epoch),
            # `runs` 投影没有 started_at 列：用 run.start 事件的落库时间
            # （= created_at）当"开始时间"。它回答的是"这个 Run 是什么时候
            # 被要求开始的"，不声称"第一轮模型调用发生在那一刻"。
            "started_at": int(run.created_at),
            "ended_at": None if run.ended_at is None else int(run.ended_at),
            "n_applied": counts.n_applied,
            "n_unknown": counts.n_unknown,
            "undoable": bool(self._stack.snapshots.load(run.run_id)),
        }

    def _list_recent_runs(self) -> list[Any]:
        """非终态（按开始顺序）+ 最近 20 个终态/被 fence 的（新的在前）。"""
        rows = self._tasks.conn.execute(
            "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
        active: list[Any] = []
        recent: list[Any] = []
        for row in rows:
            run = self._tasks.get_run(row["run_id"])
            if run is None:
                continue
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                if len(recent) < RECENT_TERMINAL_RUNS:
                    recent.append(run)
            else:
                active.append(run)
        active.sort(key=lambda r: int(r.created_at))
        return active + recent

    # ------------------------------------------------------------------ approvals

    def _list_approvals(self, run_id: str | None) -> list[dict[str, Any]]:
        """过期的先清再列（批了也不会执行的不该留在队列里，§3.4）。"""
        self._approvals.expire_due()
        rule_ids = self._approval_rule_ids()
        return [
            approval_view(approval, rule_id=rule_ids.get(approval.approval_id, ""))
            for approval in self._approvals.pending(run_id=run_id)
        ]

    def _approval_rule_ids(self) -> dict[str, str]:
        """`Approval` 投影没有 `rule_id`，它只存在于 `approval.requested` 事件里。

        视图要按契约给出 rule_id（UI 拿去映射人话），所以从事件读一次；
        同一张审批只会被请求一次（`ApprovalStore.request` 幂等），字典不会歧义。
        """
        mapping: dict[str, str] = {}
        for event in self._log.read_types(["approval.requested"]):
            approval_id = event.payload.get("approval_id")
            rule_id = event.payload.get("rule_id")
            if isinstance(approval_id, str) and isinstance(rule_id, str):
                mapping.setdefault(approval_id, rule_id)
        return mapping

    def _approvals_list(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = params.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise ControlError("bad_params", "run_id 必须是字符串")
        if run_id:
            self._require_run(run_id)
        return {"approvals": self._list_approvals(run_id or None)}

    def _approvals_decide(self, params: dict[str, Any]) -> dict[str, Any]:
        """决定 + 把 Run 交给驱动线程续跑。

        `resumed` 只表示"决定已交给驱动线程"，**不表示已经跑起来**：真正的
        续跑发生在驱动线程的 `resume_run`，可能失败（被别的实例持有等），
        那由驱动线程记录后丢弃。把"入队"说成"已续跑"就是撒谎。
        """
        approval_id = _require_str(params, "approval_id")
        decision = _require_str(params, "decision")
        library_decision = _DECISION_MAP.get(decision)
        if library_decision is None:
            raise ControlError(
                "bad_params",
                f"未知决定：{decision}（可选 {sorted(_DECISION_MAP)}）",
            )
        current = self._approvals.get(approval_id)
        if current is None:
            raise ControlError("not_found", f"审批不存在：{approval_id}")
        # `reason` 目前不属于库里的审批记录（人话解释在请求上），显式忽略：
        # 不拿它去改 state，也不塞进视图。
        approval = self._approvals.decide(
            approval_id,
            decision=library_decision,
            actor=HumanIdentity.local_cli(),
        )
        resumed = self._enqueue_resume(approval.run_id)
        rule_ids = self._approval_rule_ids()
        return {
            "approval": approval_view(
                approval, rule_id=rule_ids.get(approval.approval_id, "")
            ),
            "resumed": resumed,
        }

    def _enqueue_resume(self, run_id: str) -> bool:
        """把一个 Run 交给驱动线程；同一个 Run 在取走前只入队一次。"""
        if self.resume_requests is None:
            return False
        with self._resume_lock:
            if run_id in self._resume_inflight:
                return False
            self._resume_inflight.add(run_id)
        self.resume_requests.put(run_id)
        return True

    def resume_drained(self, run_id: str) -> None:
        """驱动线程取走一个续跑请求后调用（内部接线，不是 RPC 方法）。

        不做这一步，同一个 Run 的第二次审批决定会以为"已经在队里"而不入队，
        于是人批了第二个动作，任务却停在原地。
        """
        with self._resume_lock:
            self._resume_inflight.discard(run_id)

    # ------------------------------------------------------------------ events

    def _events_run(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _require_str(params, "run_id")
        self._require_run(run_id)
        after_seq = _opt_int(params, "after_seq", 0, 0, 2**63 - 1)
        limit = _opt_int(params, "limit", DEFAULT_EVENT_LIMIT, 1, MAX_EVENT_LIMIT)
        events = [
            event
            for event in self._log.read_run(run_id)
            if int(event.seq) > after_seq
        ]
        has_more = len(events) > limit
        return {
            "events": [event_view(event) for event in events[:limit]],
            "seq_head": self.event_seq_head(),
            "has_more": has_more,
        }

    # ------------------------------------------------------------------ undo

    def _undo_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        """只读预演。没有可撤销的变更时返回 `{changes: []}`（不是错误）。

        `plan_undo` 在"没有变更"时抛 `UndoRefused(code=1)`，那是 CLI 的退出码
        语义（1 = 没有可撤销的东西）。对 UI 来说这是一个合法答案：卡片显示
        "这个 Run 没有可撤销的改动"，而不是一条红色错误。
        """
        run_id = _require_str(params, "run_id")
        try:
            plan = plan_undo(self._stack, run_id)
        except UndoRefused as refused:
            if refused.code == 1:
                return {"changes": []}
            raise self._refused(refused) from refused
        return {"changes": [_undo_change(item) for item in reversed(plan.items)]}

    def _undo_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行撤销。前置拒绝 → `refused` 错误码；执行中炸 → `undo_failed`。"""
        run_id = _require_str(params, "run_id")
        try:
            outcome = apply_undo(self._stack, run_id)
        except UndoRefused as refused:
            raise self._refused(refused) from refused
        except (TianshuError, OSError) as exc:
            # 不吞：撤销失败（部分文件已改）必须让 UI 显示"已停止，部分生效"，
            # 而不是一条假的 success。细节里带上异常类型，方便排障。
            raise ControlError(
                "undo_failed",
                f"撤销执行失败：{type(exc).__name__}: {exc}",
                {"error_type": type(exc).__name__},
            ) from exc
        result: dict[str, Any] = {"applied": bool(outcome.ok)}
        refused = [*outcome.conflicts, *outcome.problems]
        if refused:
            result["refused"] = refused
        return result

    @staticmethod
    def _refused(refused: UndoRefused) -> ControlError:
        return ControlError(
            "refused",
            str(refused),
            {"code": int(refused.code), "lines": list(refused.lines)},
        )

    # ------------------------------------------------------------------ 错误映射

    @staticmethod
    def _approval_error(exc: ApprovalError) -> ControlError:
        message = str(exc)
        if "不存在" in message:
            return ControlError("not_found", message)
        return ControlError("conflict", message)

    @staticmethod
    def _generic_error(exc: TianshuError) -> ControlError:
        """把无类型的 `TianshuError` 归到契约里的错误码。

        库里的错误没有 `code` 字段（除了 UndoRefused），只能按语义分类：
        "不存在" → not_found；参数不合法 → bad_params；状态不允许 → conflict；
        其余 → internal（那是我们的 bug，不该让 UI 当业务错误处理）。
        """
        message = str(exc)
        if "不存在" in message:
            return ControlError("not_found", message)
        if any(marker in message for marker in ("不能为空", "不合法", "非法", "必须")):
            return ControlError("bad_params", message)
        if any(
            marker in message
            for marker in ("已终结", "已关闭", "已有活跃", "已被 fence", "不能")
        ):
            return ControlError("conflict", message)
        return ControlError("internal", message)


# --------------------------------------------------------------------------- 内部工具


class _EffectCounts:
    """从事件数出"改了几个 / 几个不知道"。

    口径必须与 `AgentLoop._index_events` 完全一致：`applied` 计一处生效，
    `partial`/`unknown` 计一处不确定。分叉的后果是 UI 的数字和终态结算对不上。
    """

    __slots__ = ("n_applied", "n_unknown")

    def __init__(self, n_applied: int = 0, n_unknown: int = 0) -> None:
        self.n_applied = n_applied
        self.n_unknown = n_unknown

    @classmethod
    def of(cls, events: list[Any]) -> _EffectCounts:
        counts = cls()
        for event in events:
            if event.type != "tool.result":
                continue
            state = event.payload.get("effect_state")
            if state == "applied":
                counts.n_applied += 1
            elif state in ("partial", "unknown"):
                counts.n_unknown += 1
        return counts


def _undo_change(item: Any) -> dict[str, Any]:
    """一条撤销预演。`path` 给**绝对路径**：多 workspace 任务里只写相对路径，
    人看不出动的是哪个目录（`UndoPlan.describe` 同一条理由）。"""
    root = Path(item.root)
    target = root / item.path if item.root else Path(item.path)
    if item.before_state == "absent":
        return {
            "path": str(target),
            "kind": "delete",
            "reason": "原本不存在，撤销=删掉",
        }
    return {
        "path": str(target),
        "kind": "restore",
        "reason": f"还原到写前内容（{item.before_state[:12]}…）",
    }


def _exposed_tool_ids() -> list[str]:
    """模型可见的工具清单（与 `AgentLoop.start` 的 payload 一致）。"""
    from .tools import SPECS

    return sorted(SPECS)


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ControlError("bad_params", f"{key} 必须是非空字符串")
    return value


def _opt_int(
    params: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlError("bad_params", f"{key} 必须是整数")
    if value < minimum or value > maximum:
        raise ControlError(
            "bad_params", f"{key} 必须在 [{minimum}, {maximum}] 之间"
        )
    return int(value)


__all__ = [
    "DAEMON_VERSION",
    "ControlError",
    "ControlPlane",
    "approval_view",
    "event_view",
    "task_view",
]