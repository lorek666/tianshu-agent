"""Task / Run / 游标（M2 · PR A Task 1 + PR C Task 5）。

一件用户的事 = 一个 Task；一次执行 = 一个 Run。一个 Task 可以有多个 Run，
失败重试就是新开一个 Run，历史不覆盖。

核心是 **fencing**：用户中途改主意时，旧 Run 必须立刻失效，不能拿着旧目标
继续改文件。
- `goal_revision`：目标改一次加一
- `lease_epoch`：目标或工作区绑定变更加一

Run 绑定创建时的这两个值；任一与 Task 当前值不符，即为被 fence。

Run 还有第二个维度：**状态**。非终态（`queued` / `running` /
`waiting_approval` / `reconciling` / `paused` / `cancelling`）都要落库，
否则"停下来了可以继续"和"正在执行"在数据里长得一样，UI 与 CLI 只能瞎猜。
用户的暂停/取消请求单独存在 `control_requested`：执行中的 Run 只记录请求，
执行器在**安全点**确认成状态，确认与清空请求是同一次提交。

**事实源是事件日志，不是这几张表。** tasks/runs 只是投影：每次状态变更都和
对应事件（`task.created` / `task.revised` / `run.start` / `run.state` /
`run.control` / `run.effect` / `run.cursor` / `run.finish` / `run.fenced`）
在同一个 SQLite 事务里提交，投影被删掉也能按事件重建。否则"用户改过目标"
这件事只存在于一张可被覆写的表里，回放无法解释某个 Run 为什么被 fence。

**锁序（全局唯一顺序）：执行守卫 → SQLite 事务。**

守卫是跨进程文件锁，SQLite 事务是跨进程写锁，两者都只有一把。如果某条路径
先拿事务再等守卫，就会和"先拿守卫再等事务"的另一条路径互相卡死。
所以凡是需要守卫的方法（`begin_execution` / `revise_task`）都显式拒绝在
已打开的事务里被调用，而不是靠调用方自觉。

## 内部模块拆分（零契约变更）

- `_tasks_types.py`：封闭常量集、建表 Schema、值对象与行映射
- `_tasks_subtasks.py`：`_SubtaskStoreMixin`（子任务委派、取消传播、唤醒与恢复）
- `_tasks_cursor_rebuild.py`：`_TaskCursorRebuildMixin`（游标、模型回合、终态结算与投影重建）
- 本模块：保留 `TaskStore` 核心入口，并重导出全部公开及测试依赖符号。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._tasks_cursor_rebuild import _TaskCursorRebuildMixin
from ._tasks_subtasks import _SubtaskStoreMixin
from ._tasks_types import (
    _MCP_SERVER_RE,
    CANCEL_REASON,
    CONTROL_CANCEL,
    CONTROL_PAUSE,
    CONTROL_REQUESTS,
    EFFECT_STATES,
    EXECUTION_STARTED,
    NON_TERMINAL_STATES,
    RUN_CANCELLING,
    RUN_CONTROL,
    RUN_CREATED,
    RUN_CURSOR_SAVED,
    RUN_EFFECT,
    RUN_END,
    RUN_FENCED,
    RUN_FENCED_EVENT,
    RUN_FINISH,
    RUN_MODEL_TURN,
    RUN_OUTCOMES,
    RUN_PAUSED,
    RUN_QUEUED,
    RUN_RECONCILING,
    RUN_RUNNING,
    RUN_START,
    RUN_STATE,
    RUN_STATES,
    RUN_WAITING_APPROVAL,
    SCHEMA,
    STOPPED_STATES,
    SUBTASK_CANCELLED,
    SUBTASK_CANCELLING,
    SUBTASK_DEADLINE_MS,
    SUBTASK_DEFAULT_MAX_TOKENS,
    SUBTASK_DEFAULT_MAX_TURNS,
    SUBTASK_FAILED,
    SUBTASK_FENCED,
    SUBTASK_GOAL_MAX_BYTES,
    SUBTASK_MAX_DEPTH,
    SUBTASK_MAX_PER_ROOT_TASK,
    SUBTASK_MAX_UNFINISHED_PER_RUN,
    SUBTASK_QUEUED,
    SUBTASK_RUNNING,
    SUBTASK_STATES,
    SUBTASK_SUCCEEDED,
    SUBTASK_SUMMARY_MAX_BYTES,
    SUBTASK_TERMINAL_STATES,
    SUBTASK_TOKENS_RANGE,
    SUBTASK_TURNS_RANGE,
    SUBTASK_UNFINISHED_STATES,
    TASK_CREATED,
    TASK_REVISED,
    TASK_SCOPE,
    TERMINAL_STATES,
    ExecutionPermit,
    Run,
    RunCursor,
    Subtask,
    Task,
    _ensure_column,
    _now,
    clean_subtask_limits,
    row_to_run,
    row_to_subtask,
    row_to_task,
    subtask_budget_attempt,
    truncate_utf8,
)
from .disk_admission import DiskAdmission, DiskAdmissionError
from .errors import RunFenced, TianshuError
from .events import EventLog
from .execpolicy import ExecPolicy
from .guard import assert_paths_not_blocked
from .jobs import JOB_WAITING, JobClaim, JobStore
from .leases import GUARD_TIMEOUT_MS, LeaseStore, execution_guard
from .run_policy import EFFECTS, PURE_ONLY, RunEffectsDisallowed, RunExecutionPolicyV1
from .workspace import ResourceIdentity, WorkspaceBinding, parse_bindings

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行期不导入
    from .permissions import ApprovalStore


class TaskStore(_SubtaskStoreMixin, _TaskCursorRebuildMixin):
    def __init__(
        self,
        log: EventLog,
        *,
        leases: LeaseStore | None = None,
        claims: JobStore | None = None,
        approvals: ApprovalStore | None = None,
        data_root: Path | None = None,
        guard_timeout_ms: int = GUARD_TIMEOUT_MS,
        budget: Any | None = None,
        contents: Any | None = None,
        blob_dir: Path | None = None,
        disk_admission: DiskAdmission | None = None,
    ) -> None:
        if log.in_transaction:
            # executescript 会先隐式 COMMIT 掉外层事务，构造 store 就等于把半份
            # 状态提交出去。只能在事务外构造。
            raise TianshuError("TaskStore 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        _ensure_column(self._conn, "runs", "control_requested", "TEXT")
        _ensure_column(self._conn, "tasks", "exec_policy", "TEXT NOT NULL DEFAULT ''")
        # `is_control` / `memory_scopes`：控制面 Run 的持久标记（契约 v1.2）。
        # 必须落在表里与 `task.created` payload 里——只放在内存对象里，崩溃恢复后
        # 就分不清哪个 Run 是人直接发起的了。
        _ensure_column(
            self._conn, "tasks", "is_control", "INTEGER NOT NULL DEFAULT 0"
        )
        _ensure_column(
            self._conn, "tasks", "memory_scopes", "TEXT NOT NULL DEFAULT '[]'"
        )
        _ensure_column(
            self._conn, "tasks", "outbound_allow", "TEXT NOT NULL DEFAULT '[]'"
        )
        _ensure_column(
            self._conn, "tasks", "mcp_servers", "TEXT NOT NULL DEFAULT '[]'"
        )
        # G2 委派 D1：父链与能力声明。旧库补列后旧行读成「根任务、不能委派」，
        # 这是唯一正确的默认值——未声明即无委派能力，不能因为补了个列就多出一种能力。
        _ensure_column(self._conn, "tasks", "parent_task_id", "TEXT")
        _ensure_column(self._conn, "tasks", "parent_run_id", "TEXT")
        _ensure_column(self._conn, "tasks", "subtask_id", "TEXT")
        _ensure_column(
            self._conn, "tasks", "depth", "INTEGER NOT NULL DEFAULT 0"
        )
        _ensure_column(
            self._conn, "tasks", "max_subtasks", "INTEGER NOT NULL DEFAULT 0"
        )
        _ensure_column(self._conn, "runs", "parent_run_id", "TEXT")
        _ensure_column(self._conn, "runs", "subtask_id", "TEXT")
        # G2 委派 D2：父版本核对值。旧库补列后旧行读成 0——不做"按当前值
        # 回填"，因为那会让一个旧库里的子任务凭空通过 fence 核对。0 与任何
        # 真实的 revision/epoch（≥1）都不等，旧行会被明确判成 fenced。
        _ensure_column(
            self._conn, "subtasks", "parent_goal_revision",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            self._conn, "subtasks", "parent_lease_epoch",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._leases = leases
        self._claims = claims
        # 共享预算与内容引用是可选的：没有账本时收尾不结算（不伪造一笔），
        # 没有内容存储时结果只有有界摘要、没有 result_ref（如实说缺什么）。
        self._budget = budget
        self._contents = contents
        self._blob_dir = Path(blob_dir) if blob_dir is not None else None
        # 审批存储是可选的：fence 时要作废旧审批，但 TaskStore 不该因此依赖
        # 整个权限层。传进来就作废，没传就只做 fencing（授权那边仍然会自动
        # 失败——旧 epoch 的请求过不了 _authority）。
        self._approvals = approvals
        self._disk_admission = disk_admission or DiskAdmission(None)
        self._data_root = data_root
        self._guard_timeout_ms = guard_timeout_ms

    @property
    def disk_admission(self) -> DiskAdmission:
        return self._disk_admission

    def require_new_run(
        self, policy: RunExecutionPolicyV1, *, writable_roots: tuple[Path, ...]
    ) -> None:
        context = self._disk_admission.context
        if policy != PURE_ONLY and context is not None and (
            context.log is not self._log or context.data_root != self._data_root
            or context.contents is not self._contents
        ):
            raise DiskAdmissionError(
                "dispatch.disk_space_unknown", "TaskStore 与 context 的实际材料不一致"
            )
        self._disk_admission.require_new_run(policy, writable_roots=writable_roots)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def leases(self) -> LeaseStore | None:
        return self._leases

    @property
    def claims(self) -> JobStore | None:
        return self._claims

    @property
    def data_root(self) -> Path | None:
        """authority 数据根：执行守卫文件放在这里，与工作区无关。"""
        return self._data_root

    @property
    def log(self) -> EventLog:
        return self._log

    def transaction(self):
        """与事件日志共用的可组合事务；已在事务中时复用外层事务。"""
        return self._log.transaction()

    def _guard(self, data_root: Path | None = None):
        """本机执行守卫。所有"会改变执行资格"的写操作都要走这里。

        必须成对：`begin_execution` 拿守卫确认"可以开始写"，`revise_goal` 拿
        同一把守卫改 revision/epoch。只让其中一方拿锁，就等于没锁——复核通过的
        下一刻目标被人改掉，写出去的仍然是旧目标的成果。
        """
        root = data_root or self._data_root
        if root is None:
            raise TianshuError(
                "未配置 data_root：无法串行化目标变更与执行开始（会留下 TOCTOU 窗口）"
            )
        if self._log.in_transaction:
            # 锁序必须是"守卫 → 事务"。已经在事务里再去等守卫，会和
            # "持有守卫、等事务"的另一条路径形成循环等待。
            raise TianshuError(
                "不能在已打开的事务中获取执行守卫（锁序必须是：守卫 → 事务）"
            )
        return execution_guard(root, timeout_ms=self._guard_timeout_ms)

    def _must_task(self, task_id: str) -> Task:
        loaded = self.get_task(task_id)
        if loaded is None:  # pragma: no cover - 同一连接刚写入或更新的行
            raise TianshuError(f"task 投影读取失败：{task_id}")
        return loaded

    def _must_run(self, run_id: str) -> Run:
        loaded = self.get_run(run_id)
        if loaded is None:  # pragma: no cover - 同一连接刚写入或更新的行
            raise TianshuError(f"run 投影读取失败：{run_id}")
        return loaded

    # ---- Task ----

    def create_task(
        self,
        goal: str,
        bindings: list[WorkspaceBinding | str | dict[str, Any]] | None = None,
        *,
        exec_policy: ExecPolicy | None = None,
        control: bool = False,
        memory_scopes: list[str] | None = None,
        outbound_allow: list[str] | tuple[str, ...] | None = None,
        mcp_servers: list[str] | tuple[str, ...] | None = None,
        max_subtasks: int = 0,
        parent_task_id: str | None = None,
        parent_run_id: str | None = None,
        subtask_id: str | None = None,
        depth: int = 0,
    ) -> Task:
        goal = (goal or "").strip()
        if not goal:
            raise TianshuError("goal 不能为空")
        self._check_parent_chain(
            parent_task_id=parent_task_id,
            parent_run_id=parent_run_id,
            subtask_id=subtask_id,
            depth=depth,
        )
        if not isinstance(max_subtasks, int) or isinstance(max_subtasks, bool):
            raise TianshuError(f"max_subtasks 必须是整数：{max_subtasks!r}")
        if not 0 <= max_subtasks <= SUBTASK_MAX_PER_ROOT_TASK:
            raise TianshuError(
                f"max_subtasks 必须在 0–{SUBTASK_MAX_PER_ROOT_TASK} 之间："
                f"{max_subtasks}（0 = 不允许委派；这是能力声明，不是可选项）"
            )
        scopes = list(memory_scopes or [])
        # 声明的作用域是安全相关输入：存进来的每个元素都必须是合法的
        # `memory://` URI。只在 Dispatcher 里校验不够——直接调 TaskStore 的
        # 调用方（恢复、测试、未来的 API）也走这个入口。
        from .resources import parse_memory_uri

        for item in scopes:
            if not isinstance(item, str) or not item:
                raise TianshuError(
                    "memory_scopes 必须是非空字符串数组（memory:// URI）"
                )
            try:
                parse_memory_uri(item)
            except TianshuError as exc:
                raise TianshuError(f"memory_scopes 不是合法的记忆作用域：{exc}") from exc
        # 外发白名单是安全相关输入：每个元素都必须是合法的**源**。
        # 解析/规范化在 TaskStore 入口再做一次（CLI 之外还有恢复、评测、测试
        # 直接调它），非法项**拒绝**而不是静默丢弃。
        from .resources import ResourceError, normalize_network_origins

        try:
            outbound = normalize_network_origins(outbound_allow)
        except ResourceError as exc:
            raise TianshuError(f"outbound_allow 不合法：{exc}") from exc
        # MCP server 白名单也是安全相关输入：每个名字都必须是资源段形状
        # （`mcp://<server>/<tool>` 与工具 id 都依赖它）。排序去重，让"同一份
        # 声明"在任务行与事件里逐字节一致。
        servers: list[str] = []
        for name in mcp_servers or []:
            if not isinstance(name, str) or not _MCP_SERVER_RE.match(name):
                raise TianshuError(
                    f"mcp_servers 里的 server 名不合法：{name!r}"
                    f"（必须是 {_MCP_SERVER_RE.pattern}）"
                )
            servers.append(name)
        servers = sorted(set(servers))
        parsed = parse_bindings(
            [b.to_dict() if isinstance(b, WorkspaceBinding) else b
             for b in (bindings or [])]
        )
        with self._log.transaction() as tx:
            task_id = self._create_task_locked(
                tx,
                goal=goal,
                bindings=parsed,
                exec_policy=exec_policy,
                control=control,
                memory_scopes=scopes,
                outbound_allow=outbound,
                mcp_servers=servers,
                max_subtasks=max_subtasks,
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                subtask_id=subtask_id,
                depth=depth,
                actor_kind="human",
            )
        return self._must_task(task_id)

    @staticmethod
    def _check_parent_chain(
        *,
        parent_task_id: str | None,
        parent_run_id: str | None,
        subtask_id: str | None,
        depth: int,
    ) -> None:
        """父链三项 + depth 必须自洽：要么全都是空（根任务），要么全都齐（子任务）。

        允许"有父任务但没有父 Run"会让"谁派的"这个问题有两个答案；而 depth
        与父链不一致就等于给出两种深度。
        """
        if depth not in (0, SUBTASK_MAX_DEPTH):
            raise TianshuError(
                f"depth 只能是 0 或 {SUBTASK_MAX_DEPTH}：{depth}"
            )
        chain = (parent_task_id, parent_run_id, subtask_id)
        if depth == 0:
            if any(item is not None for item in chain):
                raise TianshuError("根任务不能带父链字段（depth=0 时三项必须为空）")
            return
        if any(not item for item in chain):
            raise TianshuError(
                "子任务必须同时有 parent_task_id / parent_run_id / subtask_id"
            )

    def _create_task_locked(
        self,
        tx,
        *,
        goal: str,
        bindings: list[WorkspaceBinding],
        exec_policy: ExecPolicy | None,
        control: bool,
        memory_scopes: list[str],
        outbound_allow: tuple[str, ...],
        mcp_servers: list[str],
        max_subtasks: int,
        parent_task_id: str | None,
        parent_run_id: str | None,
        subtask_id: str | None,
        depth: int,
        actor_kind: str,
    ) -> str:
        """建任务行 + `task.created`（调用方负责事务）。

        子任务与根任务共用这一处，是为了让两份 `task.created` 永远同形状：
        一次兴之所至的复制粘贴，就会让"子任务的父链只存在于表里"。
        """
        now = _now()
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO tasks (task_id, goal, goal_revision, lease_epoch,"
            " active_run_id, status, workspaces, exec_policy, is_control,"
            " memory_scopes, outbound_allow, mcp_servers, parent_task_id,"
            " parent_run_id, subtask_id, depth, max_subtasks, created_at,"
            " updated_at)"
            " VALUES (?,?,1,1,NULL,'open',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                goal,
                json.dumps([b.to_dict() for b in bindings], ensure_ascii=False),
                json.dumps(exec_policy.to_dict(), ensure_ascii=False)
                if exec_policy is not None
                else "",
                1 if control else 0,
                json.dumps(list(memory_scopes), ensure_ascii=False),
                json.dumps(list(outbound_allow), ensure_ascii=False),
                json.dumps(list(mcp_servers), ensure_ascii=False),
                parent_task_id,
                parent_run_id,
                subtask_id,
                depth,
                max_subtasks,
                now,
                now,
            ),
        )
        payload: dict[str, Any] = {
            "task_id": task_id,
            "goal": goal,
            "goal_revision": 1,
            "lease_epoch": 1,
            "workspaces": [b.to_dict() for b in bindings],
            "exec_policy": exec_policy.to_dict() if exec_policy else None,
            # 契约 v1.2：控制面标记与作用域声明必须落进事件，不许只存在
            # 进程内对象里（崩溃恢复后就分不清谁是控制面）。
            "control": bool(control),
            "memory_scopes": list(memory_scopes),
            # G2：外发白名单必须落进事件（崩溃恢复后仍能回答
            # "这个任务当时准不准出网、准去哪儿"）。
            "outbound_allow": list(outbound_allow),
            # G2 MCP 阶段：任务级 server 白名单也进事件（崩溃恢复后仍能回答
            # "这个任务当时准用哪些外部 server"）。
            "mcp_servers": list(mcp_servers),
            # G2 委派 D1：能力声明**恒写**（0 也写）。旧事件缺这个键 → v0；
            # 不按新版补值，也就不存在"补列顺便补出一种能力"。
            "max_subtasks": max_subtasks,
        }
        # 父链只在**确实是子任务**时写。根写 None 会把"没有父"与"父未知"
        # 混成同一个形状，而这两件事在事后审计里完全不同。
        if depth:
            payload["parent_task_id"] = parent_task_id
            payload["parent_run_id"] = parent_run_id
            payload["subtask_id"] = subtask_id
            payload["depth"] = depth
        tx.append(
            TASK_SCOPE,
            TASK_CREATED,
            payload,
            task_id=task_id,
            actor_kind=actor_kind,
        )
        return task_id

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def revise_goal(self, task_id: str, new_goal: str) -> Task:
        """用户改主意。旧目标下的 Run 立即失效，历史保留。"""
        return self.revise_task(task_id, goal=new_goal)

    def revise_task(
        self,
        task_id: str,
        *,
        goal: str | None = None,
        bindings: list[WorkspaceBinding | str | dict[str, Any]] | None = None,
    ) -> Task:
        """改目标，或改工作区绑定，或两者一起。

        两件事都在**同一把执行守卫**里做，因为它们在同一个意义上改变"谁能写
        什么"：改目标让旧 Run 的 revision 过期，改绑定让旧 Run 的授权目录过期，
        任一先落地而另一件还在路上，就会出现"用旧绑定执行新目标"。
        绑定变更是**收窄/替换**，不是扩张：新绑定只对之后的新 Run 生效，
        旧 Run 已经失败在 fence 上，不存在"改个目录顺便续命"的通道。

        `keep_epoch` 之类的余地故意不留：改任何一项都让 `lease_epoch` +1，
        否则旧 Run 的租约和审批还能在同一个 epoch 下继续用。
        """
        if goal is None and bindings is None:
            raise TianshuError("revise_task 至少要改目标或改绑定之一")
        if goal is not None:
            goal = goal.strip()
            if not goal:
                raise TianshuError("goal 不能为空")
        parsed = None
        if bindings is not None:
            parsed = parse_bindings(
                [
                    b.to_dict() if isinstance(b, WorkspaceBinding) else b
                    for b in bindings
                ]
            )
        # 与 begin_execution 抢同一把守卫：要么改目标先完成（旧 Run 立刻 fence），
        # 要么执行已经开始（改目标排队到这次执行结束），不会出现"复核通过后目标被改"。
        with self._guard():
            with self._log.transaction() as tx:
                task = self.get_task(task_id)
                if task is None:
                    raise TianshuError(f"task 不存在：{task_id}")
                if task.status != "open":
                    raise TianshuError(f"task 已关闭，不能改目标：{task.status}")
                self._revise_locked(tx, task, goal, parsed)
        return self._must_task(task_id)

    def _revise_locked(
        self,
        tx,
        task: Task,
        new_goal: str | None,
        bindings: list[WorkspaceBinding] | None = None,
    ) -> None:
        now = _now()
        revision = task.goal_revision + 1
        epoch = task.lease_epoch + 1
        goal = new_goal if new_goal is not None else task.goal
        workspaces = bindings if bindings is not None else task.workspaces
        self._conn.execute(
            "UPDATE tasks SET goal=?, goal_revision=?,"
            " lease_epoch=?, active_run_id=NULL, workspaces=?, updated_at=?"
            " WHERE task_id=?",
            (
                goal,
                revision,
                epoch,
                json.dumps([b.to_dict() for b in workspaces], ensure_ascii=False),
                now,
                task.task_id,
            ),
        )
        tx.append(
            TASK_SCOPE,
            TASK_REVISED,
            {
                "task_id": task.task_id,
                "goal": goal,
                "goal_revision": revision,
                "lease_epoch": epoch,
                "workspaces": [b.to_dict() for b in workspaces],
            },
            task_id=task.task_id,
            actor_kind="human",
        )
        # 旧的活跃 Run 标记为 fenced（已终结的保持原状）
        if task.active_run_id:
            self._fence_run(tx, task.active_run_id, now, reason="goal_revised")

    def _fence_run(self, tx, run_id: str, now: int, *, reason: str) -> None:
        cur = self._conn.execute(
            "UPDATE runs SET state='fenced', control_requested=NULL, ended_at=?"
            " WHERE run_id=? AND state NOT IN ('succeeded','failed','cancelled','fenced')",
            (now, run_id),
        )
        if cur.rowcount == 0:
            return
        run = self.get_run(run_id)
        tx.append(
            run_id,
            RUN_FENCED_EVENT,
            {
                "run_id": run_id,
                "task_id": run.task_id if run else None,
                "reason": reason,
            },
            task_id=run.task_id if run else None,
        )
        # fence 触发时旧 Run 的审批立即作废：不能让一张旧 epoch 的批准在
        # 新目标下"暗中重新绑定"——人批的是旧目标的动作，不是新目标的。
        if self._approvals is not None:
            self._approvals.supersede_for_run(run_id, reason=f"run_fenced:{reason}")
        # fence 之后旧 Run 不再拥有工作区，租约必须交出来；
        # 否则同一 Task 的新 Run 会被上一个 Run 的残留租约挡住，直到 TTL 到期。
        if self._leases is not None:
            self._leases.release_run(run_id)
        # 作业也必须一起收掉。被 fence 的 Run 已经没有结论，不会有任何执行器
        # 再去推进它：`worker` 认领后立刻早退、`claim_next` 不认领 `waiting`、
        # `_finish_locked` 只在 outcome=cancelled 时收作业。留着这条作业的后果
        # 是它永远 `pending`/`waiting`——队列里一条永远轮不到的僵尸。
        if self._claims is not None:
            job = self._claims.job_for_run(run_id)
            if job is not None and job.state in ("pending", "claimed", "waiting"):
                self._claims.cancel(job.job_id, reason=f"run_fenced:{reason}")

    # ---- Run ----

    def start_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        execution_policy: RunExecutionPolicyV1 = EFFECTS,
        workflow: Any = None,
    ) -> Run:
        """开一次执行。

        `payload` 是 `run.start` 事件的附加字段（目标文本、输出文件名、预算）。
        它必须进事件而不是只留在内存里：`replay` 要能仅凭日志重建当时的
        提示消息序列，否则"消息哈希对得上"这条验证就没有意义。
        """
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        if workflow is not None:
            from .workflow import WorkflowV1

            if not isinstance(workflow, WorkflowV1):
                raise TianshuError("workflow 必须是 WorkflowV1")
            WorkflowV1.of(workflow.to_dict())
            if workflow.mode in ("plan", "compact") and execution_policy != PURE_ONLY:
                raise TianshuError("计划与压缩 Run 必须采用 pure_only 执行资格")
        with self._log.transaction() as tx:
            task = self.get_task(task_id)
            if task is None:
                raise TianshuError(f"task 不存在：{task_id}")
            if task.status != "open":
                raise TianshuError(f"task 已关闭：{task.status}")

            # 同一个 Task 同时只能有一个活跃 Run，否则两个 Run 会互相覆盖成果
            if task.active_run_id:
                active = self.get_run(task.active_run_id)
                if active is not None and active.state not in TERMINAL_STATES:
                    raise TianshuError(
                        f"task {task_id} 已有活跃 run {active.run_id}"
                        f"（state={active.state}），请先收尾或取消"
                    )

            # 在创建事务内使用本次读取的绑定，不拿预检时的旧 workspace 作证。
            self.require_new_run(execution_policy, writable_roots=tuple(
                Path(b.root_path) for b in task.workspaces if b.can_write
            ))
            now = _now()
            self._insert_run_locked(
                tx,
                task=task,
                run_id=run_id,
                now=now,
                payload=payload,
                execution_policy=execution_policy,
            )
            if workflow is not None:
                tx.append(
                    run_id, "run.workflow", workflow.to_dict(),
                    task_id=task.task_id, actor_kind="human",
                )
        return self._must_run(run_id)

    def run_execution_policy(self, run_id: str) -> RunExecutionPolicyV1 | None:
        from .event_schema import validate_payload

        starts = [e for e in self._log.read_run(run_id) if e.type == RUN_START]
        if len(starts) != 1:
            raise TianshuError(f"run {run_id} 的 run.start 必须唯一")
        event = starts[0]
        validate_payload(RUN_START, event.payload, event.payload_version)
        if event.payload_version in (0, 1):
            return None
        return RunExecutionPolicyV1.of(event.payload["execution_policy"])

    def require_effects_allowed(self, run_id: str) -> None:
        if self.run_execution_policy(run_id) == PURE_ONLY:
            raise RunEffectsDisallowed("此 Run 仅允许 pure 工具；请显式新开 effects Run")

    def require_effect_space(
        self, run_id: str, *, targets: tuple[Path, ...], data_root: Path | None
    ) -> None:
        self.require_effects_allowed(run_id)
        context = self._disk_admission.context
        actual_root = data_root if data_root is not None else self._data_root
        if context is None or (
            context.log is not self._log or context.contents is not self._contents
            or context.data_root is None or actual_root is None
            or self._data_root is None
            or context.data_root.resolve() != Path(actual_root).resolve()
            or context.data_root.resolve() != Path(self._data_root).resolve()
        ):
            raise DiskAdmissionError(
                "dispatch.disk_space_unknown", "执行实例与 Run context 的实际材料不一致"
            )
        run = self.assert_current(run_id)
        task = self.get_task(run.task_id)
        if task is None:
            raise TianshuError(f"task 不存在：{run.task_id}")
        self._disk_admission.require_effect_space(
            writable_roots=tuple(Path(binding.root_path) for binding in task.workspaces
                                 if binding.can_write),
            targets=targets,
        )

    def _insert_run_locked(
        self,
        tx,
        *,
        task: Task,
        run_id: str,
        now: int,
        execution_policy: RunExecutionPolicyV1,
        payload: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        subtask_id: str | None = None,
    ) -> str:
        """建 Run 行 + `run.start`（调用方负责事务与活跃 Run 的前置判定）。

        父链字段只在**子 Run** 上非空：它们是子任务身份在 Run 这一侧的镜像，
        重建投影时从 `run.start` 读回来（投影可丢，事件不可丢）。
        """
        self._conn.execute(
            "INSERT INTO runs (run_id, task_id, goal_revision, lease_epoch,"
            " state, parent_run_id, subtask_id, created_at)"
            " VALUES (?,?,?,?,'created',?,?,?)",
            (
                run_id,
                task.task_id,
                task.goal_revision,
                task.lease_epoch,
                parent_run_id,
                subtask_id,
                now,
            ),
        )
        self._conn.execute(
            "UPDATE tasks SET active_run_id=?, updated_at=? WHERE task_id=?",
            (run_id, now, task.task_id),
        )
        start: dict[str, Any] = {
            **(payload or {}),
            "run_id": run_id,
            "task_id": task.task_id,
            "goal_revision": task.goal_revision,
            "lease_epoch": task.lease_epoch,
            "execution_policy": execution_policy.to_dict(),
        }
        if subtask_id is not None:
            start["parent_run_id"] = parent_run_id
            start["subtask_id"] = subtask_id
        tx.append(
            run_id,
            RUN_START,
            start,
            task_id=task.task_id,
        )
        return run_id

    def run_payload(self, run_id: str) -> dict[str, Any]:
        """`run.start` 的原始字段（目标、输出文件名、预算）。"""
        row = self._conn.execute(
            "SELECT payload FROM events WHERE run_id=? AND type=? ORDER BY seq LIMIT 1",
            (run_id, RUN_START),
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else {}

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_run(row) if row else None

    def is_fenced(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        if run is None:
            return True
        task = self.get_task(run.task_id)
        if task is None:
            return True
        return (
            run.goal_revision != task.goal_revision
            or run.lease_epoch != task.lease_epoch
        )

    def assert_current(self, run_id: str) -> Run:
        """这个 Run 现在还有资格写入吗（只读判断，不留证据）。

        单用不够：读完到真正写之间目标可能被改。真正要写文件时必须改用
        `begin_execution`——它把同样的核验放进执行守卫，并留下
        `execution.started` 作为"已在执行"的线性化证据。
        """
        run = self.get_run(run_id)
        if run is None:
            raise RunFenced(f"run 不存在：{run_id}")
        if run.state in TERMINAL_STATES:
            raise RunFenced(f"run 已终结：{run_id}（state={run.state}）")
        if run.state == "fenced" or self.is_fenced(run_id):
            raise RunFenced(f"run 已被 fence：{run_id}")
        return run

    def assert_write_allowed(
        self, run_id: str, resources: list[ResourceIdentity] | None = None
    ) -> Run:
        """执行侧统一入口：Run 仍有效 + 资源租约仍在手。"""
        run = self.assert_current(run_id)
        if resources:
            if self._leases is None:
                raise TianshuError(
                    "未配置 LeaseStore：有写入资源的操作必须同时校验租约"
                )
            self._leases.assert_held(resources, run_id)
        return run

    @contextlib.contextmanager
    def begin_execution(
        self,
        data_root: Path | None,
        run_id: str,
        resources: list[ResourceIdentity | Path | str] | None = None,
        *,
        before_start: Callable[[], None] | None = None,
    ) -> Iterator[ExecutionPermit]:
        """进入实际文件操作前必须经过这里。

        为什么不能只用 `assert_write_allowed`：那是"读一下再返回"，读完到真正
        打开文件之间，另一个进程可以改目标（fence 这个 Run）或接管租约。
        把"复核 + 登记 execution.started"放进**同一把执行守卫**，这个窗口才关得上。

        with 块内的代码才是在"已获准执行"状态下跑的；块一结束守卫就释放，
        所以不要把模型调用、人工等待塞进来（那会让所有实例跟着等）。

        只接受 `RUNNING`：`PAUSED` / `WAITING_APPROVAL` 的 Run 手里可能还拿着
        有效的租约和 claim（等待审批期间必须续租，否则每轮 TTL 都要重新审批），
        但它们**不该执行**。把这条判断放在执行入口，而不是指望调用方自觉。

        尚未包含审批/grant 核验——那是 PR-B 的派发层。这里覆盖的是
        revision/epoch/租约/状态以及固定 Run 资格与当前必需位置空间，缺一项就拒绝。
        """
        resources = list(resources or [])
        if not resources:
            # 有副作用的执行必须绑定到具体资源上，否则"没传资源"就等于
            # 绕过租约校验。纯对话任务不会走到这里。
            raise TianshuError("begin_execution 必须声明要写的资源（不能为空）")
        if self._leases is None:
            raise TianshuError("未配置 LeaseStore：begin_execution 需要校验租约")
        with self._guard(data_root):
            run = self.assert_current(run_id)
            if run.state != RUN_RUNNING:
                raise RunFenced(
                    f"run 不在执行状态，拒绝开始写入：{run_id}（state={run.state}）"
                )
            self._leases.assert_held(resources, run_id)
            paths = tuple(Path(r.path if isinstance(r, ResourceIdentity) else r)
                          for r in resources)
            self.require_effect_space(
                run_id, targets=paths + tuple(p.parent for p in paths), data_root=data_root,
            )
            if resources:
                # 守卫内续一次租：接下来的写入不该因为"复核花了几秒"而掉出 TTL
                self._leases.renew(resources, run_id)
            # 复审 中-1：派发 phase-1 查过阻断，但查完到真正落盘之间还可能有人
            # 追加 `recovery.blocked`。守卫在整个 with 块期间一直持有，所以在这里
            # 再判一次、并让 recovery 的阻断写入也去抢同一把守卫（见
            # `recovery.recover_run(data_root=...)`）之后，两件事的先后就有了
            # 全序：要么阻断先落库（这次写入被拒），要么写入先完成（阻断只影响
            # 之后的写入）。两者都不会出现"已阻断却照写"。
            assert_paths_not_blocked(
                self._log,
                [r.path if isinstance(r, ResourceIdentity) else r for r in resources],
            )
            started_at = _now()
            with self._log.transaction() as tx:
                if before_start is not None:
                    before_start()
                seq = tx.append(
                    run_id,
                    EXECUTION_STARTED,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "goal_revision": run.goal_revision,
                        "lease_epoch": run.lease_epoch,
                        "resources": [str(r) for r in resources],
                    },
                    task_id=run.task_id,
                )
            yield ExecutionPermit(
                run_id=run_id,
                task_id=run.task_id,
                goal_revision=run.goal_revision,
                lease_epoch=run.lease_epoch,
                resources=tuple(str(r) for r in resources),
                seq=seq,
                started_at=started_at,
            )

    # ---- 状态机与非终态落库（M2 · Task 5） ----

    def transition(
        self,
        run_id: str,
        to_state: str,
        *,
        claim: JobClaim | None = None,
        reason: str = "",
    ) -> Run:
        """把一个还没结束的 Run 推进到另一个**非终态**。

        终态必须走 `finish_run`：那里还要写 outcome/effect_state 两个维度，
        而且拒绝在被 fence 的 Run 上报成功。这里只挪状态，不许冒充结论。

        `claim` 传了就顺带核验写的人是不是当前 owner；状态机是 Run 的事实，
        旧 worker 醒过来改状态同样是事故。
        """
        if to_state not in NON_TERMINAL_STATES:
            raise TianshuError(
                f"transition 只接受非终态：{to_state}"
                f"（终态请用 finish_run；可选 {sorted(NON_TERMINAL_STATES)}）"
            )
        with self._log.transaction() as tx:
            if claim is not None:
                self._assert_writer(run_id, claim)
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                raise RunFenced(
                    f"run 已终结，不能改状态：{run_id}（state={run.state}）"
                )
            if run.state == to_state:
                return run
            cur = self._conn.execute(
                "UPDATE runs SET state=?"
                " WHERE run_id=? AND state=?"
                "   AND state NOT IN ('succeeded','failed','cancelled','fenced')",
                (to_state, run_id, run.state),
            )
            if cur.rowcount == 0:
                raise RunFenced(f"run 状态已被别的进程改掉：{run_id}")
            tx.append(
                run_id,
                RUN_STATE,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "from": run.state,
                    "to": to_state,
                    "reason": reason,
                    **({"claim_epoch": claim.claim_epoch} if claim else {}),
                },
                task_id=run.task_id,
            )
        return self._must_run(run_id)

    def note_effect(
        self,
        run_id: str,
        *,
        effect_state: str,
        n_applied: int = 0,
        n_unknown: int = 0,
        claim: JobClaim | None = None,
    ) -> Run:
        """把"到目前为止到底改了没有"落库，中途也能查。

        没有这一步，一个停下来的 Run 只能回答"还没结束"，回答不了"改了什么"，
        而 PRD 要求 UI 必须能显示"已停止，但部分修改已生效"。
        这只是投影：真正的证据仍是每条 tool.result / receipt。
        """
        if effect_state not in EFFECT_STATES:
            raise TianshuError(f"未知 effect_state：{effect_state}")
        with self._log.transaction() as tx:
            if claim is not None:
                self._assert_writer(run_id, claim)
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            self._conn.execute(
                "UPDATE runs SET effect_state=? WHERE run_id=?",
                (effect_state, run_id),
            )
            tx.append(
                run_id,
                RUN_EFFECT,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "effect_state": effect_state,
                    "n_applied": n_applied,
                    "n_unknown": n_unknown,
                },
                task_id=run.task_id,
            )
        return self._must_run(run_id)

    # ---- 用户控制：暂停 / 取消 ----

    # 这些状态里"没有副作用在飞"是结构性事实，不是猜测：
    # - created / queued：还没开始执行
    # - waiting_approval：正等人批准，派发还没发生
    # - paused：安全点已经过去，影响它的那次执行已经收尾
    # - reconciling：执行器已经退出、等人工核查，**没有驱动者在跑**
    # 因此请求可以就地生效。RUNNING / CANCELLING 里可能有有限副作用在执行，
    # 只记录请求，由执行器在下一个安全点确认。
    #
    # RECONCILING 必须在这里：它是主循环主动返回、然后就不再看这个 Run 的状态。
    # 不就地生效的话，用户按取消之后会一直等一个永远不会来的安全点。
    SAFE_CONTROL_STATES = (
        RUN_CREATED,
        RUN_QUEUED,
        RUN_WAITING_APPROVAL,
        RUN_RECONCILING,
        RUN_PAUSED,
    )

    def request_pause(self, run_id: str) -> Run:
        """请求暂停。返回的是"现在"的 Run，不承诺已经停下。

        调用方要读 `control_requested` 才知道请求是否还在排队：
        它是非空就说明执行器还没走到安全点。
        """
        safe = tuple(self.SAFE_CONTROL_STATES)
        marks = ",".join("?" * len(safe))
        with self._log.transaction() as tx:
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                raise RunFenced(
                    f"run 已终结，不能暂停：{run_id}（state={run.state}）"
                )
            cur = self._conn.execute(
                f"UPDATE runs SET state=?, control_requested=NULL"
                f" WHERE run_id=? AND state IN ({marks})",
                (RUN_PAUSED, run_id, *safe),
            )
            immediate = bool(cur.rowcount)
            queued = False
            if not immediate:
                # 排队中的取消不会被后来的暂停顶掉：取消是更强的决定。
                # 无条件写 'pause' 会让"取消"凭空消失——用户按了取消，
                # 看到的却是"已暂停"，然后 Run 永远停在那里。
                queued = bool(
                    self._conn.execute(
                        "UPDATE runs SET control_requested=?"
                        " WHERE run_id=?"
                        "   AND (control_requested IS NULL OR control_requested=?)",
                        (CONTROL_PAUSE, run_id, CONTROL_PAUSE),
                    ).rowcount
                )
            tx.append(
                run_id,
                RUN_CONTROL,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "action": CONTROL_PAUSE,
                    "applied_immediately": immediate,
                    # 已经有更强的请求在排队时，这次暂停不生效——
                    # 必须记下来，否则日志里会出现"用户暂停了但没停"且无法解释。
                    "request_effective": immediate or queued,
                    "stronger_request": None
                    if immediate or queued
                    else self._control_now(run_id),
                },
                task_id=run.task_id,
                actor_kind="human",
            )
            if immediate:
                tx.append(
                    run_id,
                    RUN_STATE,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "from": run.state,
                        "to": RUN_PAUSED,
                        "reason": "pause_requested",
                        "control_cleared": True,
                    },
                    task_id=run.task_id,
                )
                # 暂停就交还工作区：不然别人会以为这个目录还被占着。
                # resume 必须重新获取租约（拿不到就说明目录被别人用了）。
                if self._leases is not None:
                    self._leases.release_run(run_id)
        return self._must_run(run_id)

    def _control_now(self, run_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT control_requested FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["control_requested"] if row is not None else None

    def request_cancel(self, run_id: str) -> Run:
        """请求取消。安全状态下就地终结，否则留给执行器在安全点确认。

        "取消"只保证不再派发新的副作用，不撤销已经发生的那一次：
        已经 applied 的 effect_state 会如实留在 Run 上。
        """
        safe = self.SAFE_CONTROL_STATES
        with self._log.transaction() as tx:
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                return run
            effect_state = run.effect_state or "none_applied"
            job = self._claims.job_for_run(run_id) if self._claims is not None else None
            parked = job is not None and job.state == JOB_WAITING
            if run.state in safe or parked:
                effect_state = self._cancel_effect_state(
                    run_id, run, self._log.read_run(run_id),
                )
                if parked and run.state not in safe:
                    # 作业已挂起，没有驱动者会确认请求；在同一事务内核对作业和副作用。
                    safe = (*safe, run.state)
            immediate = run.state in safe
            marks = ",".join("?" * len(safe))
            tx.append(
                run_id,
                RUN_CONTROL,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "action": CONTROL_CANCEL,
                    "applied_immediately": immediate,
                    "effect_state": effect_state,
                },
                task_id=run.task_id,
                actor_kind="human",
            )
            cur = self._conn.execute(
                f"UPDATE runs SET state=?, control_requested=NULL"
                f" WHERE run_id=? AND state IN ({marks})",
                (RUN_CANCELLING, run_id, *safe),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "UPDATE runs SET control_requested=?"
                    " WHERE run_id=?",
                    (CONTROL_CANCEL, run_id),
                )
                # G2 D2：父 Run 可能正停在 `waiting`（等子任务）。取消请求
                # 只改了请求位，没有执行者会去读它——把父作业叫醒，让 worker
                # 在安全点确认取消并收尾。子任务本身在下面传播。
                self._propagate_cancel_locked(tx, run_id)
                if self._claims is not None:
                    job = self._claims.job_for_run(run_id)
                    if job is not None and job.state == JOB_WAITING:
                        self._claims.wake(job.job_id, reason="parent_cancelled")
                return self._must_run(run_id)
            tx.append(
                run_id,
                RUN_STATE,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "from": run.state,
                    "to": RUN_CANCELLING,
                    "reason": "cancel_requested",
                },
                task_id=run.task_id,
            )
            # 先传播、再收尾：子任务列表在收尾事务里就是"取消时受到影响的
            # 那些子任务"，`run.end` 的 `subtasks_cancelled=N` 从同一份事实算。
            self._propagate_cancel_locked(tx, run_id)
            self._finish_locked(
                tx,
                run_id,
                outcome="cancelled",
                effect_state=effect_state,
                reason=CANCEL_REASON,
            )
        return self._must_run(run_id)

    def confirm_control(self, run_id: str, *, claim: JobClaim) -> str | None:
        """安全点：把排队中的控制请求变成状态并清掉请求。

        确认与清除必须是同一次提交。分两步的话，进程在中间被杀，恢复后会把
        同一条暂停请求再执行一遍——刚 resume 又立刻停下，用户看到的是
        "点了继续没反应"。

        返回 `pause` / `cancel` / `None`。`cancel` 已经就地收尾。
        """
        with self._log.transaction() as tx:
            self._assert_writer(run_id, claim)
            run = self.get_run(run_id)
            if run is None:
                raise TianshuError(f"run 不存在：{run_id}")
            action = run.control_requested
            if action is None or action not in CONTROL_REQUESTS:
                return None
            if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                # 请求还没确认，Run 就已经被别处结束了。清掉请求，别再执行它。
                self._conn.execute(
                    "UPDATE runs SET control_requested=NULL WHERE run_id=?", (run_id,)
                )
                return None
            if action == CONTROL_PAUSE:
                cur = self._conn.execute(
                    "UPDATE runs SET state=?, control_requested=NULL"
                    " WHERE run_id=? AND state=? AND control_requested=?",
                    (RUN_PAUSED, run_id, run.state, CONTROL_PAUSE),
                )
                if cur.rowcount == 0:
                    return None
                tx.append(
                    run_id,
                    RUN_STATE,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "from": run.state,
                        "to": RUN_PAUSED,
                        "reason": "pause_confirmed",
                        "claim_epoch": claim.claim_epoch,
                    "control_cleared": True,
                    },
                    task_id=run.task_id,
                )
                return CONTROL_PAUSE
            effect_state = run.effect_state or "none_applied"
            if action == CONTROL_CANCEL:
                effect_state = self._cancel_effect_state(
                    run_id, run, self._log.read_run(run_id),
                )
            cur = self._conn.execute(
                "UPDATE runs SET state=?, control_requested=NULL"
                " WHERE run_id=? AND state=? AND control_requested=?",
                (RUN_CANCELLING, run_id, run.state, CONTROL_CANCEL),
            )
            if cur.rowcount == 0:
                return None
            tx.append(
                run_id,
                RUN_STATE,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "from": run.state,
                    "to": RUN_CANCELLING,
                    "reason": "cancel_confirmed",
                    "claim_epoch": claim.claim_epoch,
                    "control_cleared": True,
                },
                task_id=run.task_id,
            )
            # G2 D2：父 Run 的安全点取消 → 先把取消传给未完成子任务，再收尾。
            # 顺序不能反：收尾之后父 Run 已是终态，`subtask.finished` 再回填
            # 就没有任何读者了，而"取消到底影响了谁"也失去唯一的事实来源。
            self._propagate_cancel_locked(tx, run_id)
            self._finish_locked(
                tx,
                run_id,
                outcome="cancelled",
                effect_state=effect_state,
                reason=CANCEL_REASON,
            )
            return CONTROL_CANCEL


    # ---- 行映射 ----

    _row_to_task = staticmethod(row_to_task)
    _row_to_run = staticmethod(row_to_run)
    _row_to_subtask = staticmethod(row_to_subtask)


# 跨模块/测试重导出兼容符号（保持与拆分前 `tianshu.tasks` 命名空间完全一致）。
_COMPAT_EXPORTS = (
    RUN_CURSOR_SAVED,
    RUN_END,
    RUN_FINISH,
    RUN_MODEL_TURN,
    RUN_QUEUED,
    STOPPED_STATES,
    SUBTASK_CANCELLED,
    SUBTASK_CANCELLING,
    SUBTASK_FAILED,
    SUBTASK_FENCED,
    SUBTASK_QUEUED,
    SUBTASK_RUNNING,
    SUBTASK_SUCCEEDED,
    SUBTASK_SUMMARY_MAX_BYTES,
    RunCursor,
    truncate_utf8,
)

__all__: list[Any] = [
    "TaskStore",
    "Task",
    "Run",
    "Subtask",
    "RunFenced",
    "TERMINAL_STATES",
    "NON_TERMINAL_STATES",
    "RUN_STATES",
    "RUN_OUTCOMES",
    "EFFECT_STATES",
    "SUBTASK_STATES",
    "SUBTASK_UNFINISHED_STATES",
    "SUBTASK_TERMINAL_STATES",
    "SUBTASK_MAX_UNFINISHED_PER_RUN",
    "SUBTASK_MAX_PER_ROOT_TASK",
    "SUBTASK_MAX_DEPTH",
    "SUBTASK_TURNS_RANGE",
    "SUBTASK_TOKENS_RANGE",
    "SUBTASK_DEFAULT_MAX_TURNS",
    "SUBTASK_DEFAULT_MAX_TOKENS",
    "SUBTASK_DEADLINE_MS",
    "SUBTASK_GOAL_MAX_BYTES",
    "clean_subtask_limits",
    "subtask_budget_attempt",
]
