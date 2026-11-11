"""控制面记忆操作：**人签字的读与写**（CLI 与 Daemon RPC 共用这一份）。

为什么要有这个模块
------------------
本机人对记忆的控制面有两个入口：命令行（`tianshu memory ...`）和 UI（浏览器 →
Host `POST /rpc` → Daemon 的 `ControlPlane`）。它们**必须是同一个人、同一套语义、
同一条派发路径**：人敲命令 / 在 UI 上点按钮 = 批准者本人，写操作照样走
intent/result + 一次性审批 + 执行侧二次核验。

把这条路径收在一处，是因为"写记忆要经过 dispatcher"这条纪律一旦被抄成两份，
早晚会有一份图省事直接改投影——那样审批与回执就形同虚设。**这里不判断权限**：
作用域过滤交给 `MemoryProjection`（`allowed=`），审批交给 `PolicyEngine` /
`Dispatcher`。本模块只负责"用本地人身份把一次控制面操作做完并投影成视图"。

身份口径（**按事实读，别读成授权边界**）
-----------------------------------------
- 本模块只在**控制面**里被调用：CLI 进程，或 Daemon 的 `ControlPlane`（RPC）。
- 模型工具面与这条控制面是**两条路**：模型走 `AgentLoop` → `Dispatcher` →
  `MemoryExecutor`，按 `allowed` 装备过滤；`memory.list` 不是工具，其余同名工具
  名字相同但权限不同。
- **但端点挡不住模型**：v1 没有进程级沙箱，`run_command` 能以**同一个 uid** 跑子
  进程；同 uid 进程本来就能读 `run/authkey`、连 IPC、跑 `tianshu memory`，并自己
  以 `HumanIdentity.local_cli()` 落审批。所以「本机人 = 连得上 IPC」区分不了
  「人」和「模型触发的同 uid 子进程」——**这不是 N6 能解决的问题**，是 F2/exec 的
  边界（见 F10 §13 #30、KNOWLEDGE §7）。控制面交付的价值是：人与 CLI 共用一套实现、
  写操作永远走 dispatcher、审批一次性消费、执行侧二次核验。
- 端点权限只在**跨本地用户**这层有意义：POSIX 上是 `run/` 目录 0700 + socket 0600
  （`ipc._chmod_private`）；Windows 是命名管道（ACL 继承创建者、**未**额外加固、
  **未验证**）。所以别把"跨用户保护"写成"模型隔离"。
- 写操作用 `HumanIdentity.local_cli()` 落审批：与 CLI 同一个 actor。**不新造身份
  令牌**——令牌挡不住同 uid，只会多一个能被抄走的东西。

返回形状
--------
每条记忆的视图**必须**带这六项：`scope` / `origin` / `revision` / `status` /
`source_refs` / `proof_count`（另加 `memory_id`）。缺一项，人就无法判断"这条记忆
是谁的、凭什么、改过没有、还算不算数"——这正是 F10 §9 的出口判据。正文一律
**纯文本**：UI 负责转义与排版，后端不产出可执行标记。
"""

from __future__ import annotations

from typing import Any

from .dispatcher import build_request
from .errors import TianshuError
from .loop import Stack
from .memory import Memory, MemoryProjection
from .permissions import HumanIdentity
from .resources import MemoryScope
from .workspace import WorkspaceBinding

__all__ = [
    "MISSING",
    "memory_view",
    "list_memories",
    "read_memory",
    "remember",
    "edit",
    "transition",
    "promote",
    "erase",
]

#: `get_memory` 的"不在可见作用域内"哨兵。
#:
#: 不用 `None`：`None` 与"记忆不存在"分不开，而这两件事对调用方意味着不同的
#: 下一步（一个是换个作用域再看，一个是根本没这条）。
MISSING = object()


# --------------------------------------------------------------------------- 视图


def memory_view(
    memory: Memory,
    content: str | None = None,
    *,
    score: float | None = None,
    score_kind: str | None = None,
) -> dict[str, Any]:
    """一条记忆对人和 UI 的统一投影（六项判据 + 内容引用）。

    正文只在显式给出 `content` 时出现：列表页不该把整库正文塞进响应，
    `memory.get` 才是"我要看这一段"。
    """
    payload: dict[str, Any] = memory.to_dict()
    payload["content_ref"] = memory.content_ref
    payload["content"] = content
    if score is not None:
        payload["score"] = score
        payload["score_kind"] = score_kind
    return payload


def list_memories(
    projection: MemoryProjection,
    *,
    allowed: tuple[MemoryScope, ...],
    status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """按作用域列出记忆（不带正文）。

    没有正文是刻意的：列表可能一次返回几十条，正文要按需 `get`。
    `source_refs` / `proof_count` 留着——"凭什么"必须在列表页就能看。
    """
    # `limit=None` 表示"用投影的默认条数"：不要把 None 传下去——投影对 `limit`
    # 做整数校验（`_require_int`），None 会被判成非法参数，UI 的"不传 limit"就
    # 变成 400 了。
    kwargs = {} if limit is None else {"limit": limit}
    rows = projection.list_memories(allowed=allowed, status=status, **kwargs)
    return [memory_view(memory) for memory in rows]


def read_memory(
    projection: MemoryProjection,
    memory_id: str,
    *,
    allowed: tuple[MemoryScope, ...],
) -> dict[str, Any] | None:
    """读一条记忆 + 正文；不可见或不存在都返回 `None`（调用方再决定怎么报）。

    正文不可用（正文快照缺失）时 `content_state` 是 `unavailable`，**不**回落
    到当前记忆库的最新版本：历史证据不可用就如实说不可用（F10 §9）。
    """
    memory = projection.get(memory_id, allowed=allowed)
    if memory is None:
        return None
    content = projection.read_content(memory)
    view = memory_view(memory, content)
    # 正文可用性要显式给：`content=None` 既可能是"没读到"也可能是"历史快照缺失"，
    # UI 不能靠猜。读接口回答"这条记忆的正文现在在不在"；列表不带正文也就不带这栏。
    view["content_state"] = "available" if content is not None else "unavailable"
    return view


# --------------------------------------------------------------------------- 派发


def _dispatch_as_human(
    stack: Stack,
    *,
    tool_id: str,
    target: MemoryScope,
    tool_args: dict[str, object],
    workspaces: list[WorkspaceBinding],
    memory_scopes: list[MemoryScope],
    call_id: str,
):
    """用本地人身份把一次记忆写派发出去（控制面唯一出口）。

    控制 Run 只活这一次调用：`open_control_run` 写下 `control: true` 与声明作用域
    （事件事实，崩溃恢复后仍可判），派发完在 `finally` 里关掉——不管成功失败，
    不留一个"控制面还开着"的空壳。

    审批在这里是当场签掉而不是跳过：`prepare()` 若判 `waiting`，说明这条工具
    按策略需要人决定；敲这条命令的人就是决定者，所以落一张真审批再 `dispatch()`。
    一次性消费、执行侧二次核验、Receipt 全部照旧生效——**不是**"人已经批过了"
    的后门，而是"人正在批"。
    """
    control = stack.dispatcher.open_control_run(
        goal=f"{tool_id}:{call_id}", workspaces=workspaces, memory_scopes=memory_scopes
    )
    outcome, effect = "failed", "unknown"
    try:
        request = build_request(
            task=control.task,
            run=control.run,
            claim=control.claim,
            call_id=call_id,
            tool_id=tool_id,
            target=target.to_uri(),
            args=tool_args,
        )
        decision = stack.dispatcher.prepare(request, control.claim, args=tool_args)
        if decision.is_rejected:
            raise TianshuError(
                f"记忆操作被拒绝：{decision.reason}（{decision.rule_id}）"
            )
        if decision.is_waiting:
            stack.approvals.decide(
                decision.approval_id,
                decision="approved",
                actor=HumanIdentity.local_cli(),
            )
        result = stack.dispatcher.dispatch(decision.request_id, control.claim)
        outcome = "succeeded" if result.ok else "failed"
        effect = result.effect_state
        return result
    finally:
        stack.dispatcher.close_control_run(
            control, outcome=outcome, effect_state=effect
        )


def _scope_bindings(scope: MemoryScope) -> tuple[list[WorkspaceBinding], list[MemoryScope]]:
    """把目标作用域翻译成控制 Run 的绑定与声明。

    - `workspace` 记忆要绑**它自己所属的那个目录**（不是造一个假目录：那就是这条
      记忆真正归属的根），并且不额外声明记忆作用域——目录绑定本身就够。
    - `task` 记忆声明它的作用域、**不**绑目录：记忆授权与文件授权是两件事，控制面
      替旧 task 签字时不该顺手拿到那个任务的目录。
    - `user` 作用域两者都不需要。
    """
    if scope.scope == "workspace":
        return [WorkspaceBinding(scope.scope_id)], []
    if scope.scope == "task":
        return [], [scope]
    return [], []


# --------------------------------------------------------------------------- 写


def remember(
    stack: Stack,
    scope: MemoryScope,
    *,
    kind: str,
    content: str,
    expires_at: int | None = None,
):
    """人直接记住一条（`memory.remember`，控制面专属工具）。

    创建即 `active` + `origin=user_explicit`：人本人就是签发者，所以不需要审批；
    但依旧走 intent/result 与执行侧二次核验。模型清单里没有这个工具，模型不可能
    走这条路把自己写的内容说成"用户明示"。
    """
    tool_args: dict[str, object] = {"kind": kind, "content": content}
    if expires_at is not None:
        tool_args["expires_at"] = expires_at
    workspaces, declared = _scope_bindings(scope)
    return _dispatch_as_human(
        stack,
        tool_id="memory.remember",
        target=scope,
        tool_args=tool_args,
        workspaces=workspaces,
        memory_scopes=declared,
        call_id="memory_remember_1",
    )


def edit(stack: Stack, memory: Memory, *, content: str):
    """改一条记忆的正文：一次审批 = 旧行退役（可查）+ 新行 active。

    `revision` 不是"顺手带上的字段"，是审批绑定的一部分：人看到的是哪一版就写
    哪一版，执行侧从权威投影再核一次，过期 → `dispatch.memory_stale_revision`。
    """
    scope = MemoryScope(memory.scope, memory.scope_id)
    workspaces, declared = _scope_bindings(scope)
    return _dispatch_as_human(
        stack,
        tool_id="memory.edit",
        target=scope,
        tool_args={
            "memory_id": memory.memory_id,
            "revision": memory.revision,
            "content": content,
        },
        workspaces=workspaces,
        memory_scopes=declared,
        call_id=f"memory_edit_{memory.memory_id}",
    )


def transition(stack: Stack, memory: Memory, *, confirm: bool):
    """确认（进召回）/ 忘记（tombstone）一条记忆。

    `forget` 的前置状态是"当前这条是什么状态"，不是固定 `active`——候选与活动
    记忆都允许被忘记（tombstone 对两者都成立），所以 args 里要带 `status`。
    """
    tool_id = "memory.confirm" if confirm else "memory.forget"
    tool_args: dict[str, object] = {
        "memory_id": memory.memory_id,
        "revision": memory.revision,
    }
    if not confirm:
        tool_args["status"] = memory.status
    scope = MemoryScope(memory.scope, memory.scope_id)
    # workspace 作用域的授权要求 scope_id 精确等于控制 Run 的某个绑定根；task
    # 作用域声明它自己（见 `_scope_bindings`）。
    workspaces, declared = _scope_bindings(scope)
    return _dispatch_as_human(
        stack,
        tool_id=tool_id,
        target=scope,
        tool_args=tool_args,
        workspaces=workspaces,
        memory_scopes=declared,
        call_id=f"{tool_id.replace('.', '_')}_{memory.memory_id}",
    )


def promote(stack: Stack, memory: Memory, new_scope: MemoryScope):
    """把一条记忆**移动**到另一个作用域（共享是显式动作）。

    原行立即退役（原作用域不再召回，避免双份真相），目标作用域新行是 `candidate`
    （人再 `confirm` 才召回）；Receipt 针对**原行**。控制 Run 同时声明原作用域与
    目标作用域——否则"从 A 提升到 A 以外的 B"会被授权层拒掉。
    """
    old_scope = MemoryScope(memory.scope, memory.scope_id)
    tool_args: dict[str, object] = {
        "memory_id": memory.memory_id,
        "revision": memory.revision,
        "scope": new_scope.scope,
        "scope_id": new_scope.scope_id,
    }
    workspaces = (
        [WorkspaceBinding(old_scope.scope_id)] if old_scope.scope == "workspace" else []
    )
    declared = [
        scope for scope in (old_scope, new_scope) if scope.scope != "user"
    ]
    return _dispatch_as_human(
        stack,
        tool_id="memory.promote",
        target=old_scope,
        tool_args=tool_args,
        workspaces=workspaces,
        memory_scopes=declared,
        call_id=f"memory_promote_{memory.memory_id}",
    )

def erase(stack: Stack, memory: Memory):
    """物理擦除一条记忆的正文副本与索引（`memory.erase`，控制面专属）。

    `delete` 动作 + 一次性审批 + Receipt；引用计数为 0 才删文件（别的记忆还引用
    同一内容寻址 blob 就保留，回执里列 `retained_by`）。事件日志里的历史读快照
    删不掉——边界见 F10 §10，UI 不许写"已彻底删除"。
    """
    scope = MemoryScope(memory.scope, memory.scope_id)
    workspaces, declared = _scope_bindings(scope)
    return _dispatch_as_human(
        stack,
        tool_id="memory.erase",
        target=scope,
        tool_args={
            "memory_id": memory.memory_id,
            "revision": memory.revision,
        },
        workspaces=workspaces,
        memory_scopes=declared,
        call_id=f"memory_erase_{memory.memory_id}",
    )
