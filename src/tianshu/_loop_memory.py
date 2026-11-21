"""主循环本地记忆装备/只读检索、内容引用读取与工具目标解析（内部 Mixin，由 `AgentLoop` 继承）。

所有记忆投影读取、内容分页读取及工具目标规范化均与 `AgentLoop` 保持同一连接与装备边界，
零契约变更。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contents import DEFAULT_PAGE, ContentStore
from .context import ContextView
from .errors import (
    OverwriteRefused,
    TianshuError,
    ToolArgumentError,
    WorkspaceBlocked,
    WorkspaceViolation,
)
from .events import EventLog
from .resources import (
    KIND_MEMORY,
    SCOPE_TASK,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    USER_SCOPE_ID,
    MemoryScope,
    ResourceError,
    parse_resource,
)
from .snapshot import SnapshotStore
from .tasks import Task
from .tools import (
    NETWORK_TOOL_IDS,
    SPECS,
    SUBTASK_TOOL_IDS,
    ToolContext,
    ToolResult,
    execute,
    target_arg,
    validate_args,
)
from .workspace import Workspace

#: 记忆工具（F10 §3.2）是 C2 工具，但不是文件工具。封闭集放在这里而不是
#: 前缀判断：`memory.*` 里出现一个没实现的工具时，应该走"未知工具不派发"，
#: 而不是猜它属于哪条路径。
MEMORY_READ_TOOLS: frozenset[str] = frozenset({"memory.search", "memory.get"})
MEMORY_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "memory.propose",
        "memory.consolidate",
        "memory.remember",
        "memory.confirm",
        "memory.forget",
        "memory.edit",
        "memory.promote",
        "memory.erase",
    }
)


def open_memory(
    log: EventLog,
    *,
    blob_dir: Path | None = None,
    data_root: Path | None = None,
    clock: Callable[[], int] | None = None,
    embedding_provider: object | None = None,
):
    """打开记忆投影（事件日志的另一个可重建投影，F10 §3.1）。

    投影和事件日志**共用同一个连接**：记忆状态与它对应的 `tool.intent` /
    `tool.result` 必须在同一个事务里落库，否则"事件说改了、投影没改"会变成
    一个要人工裁决的分叉。`log` 也一起传进去，投影要按事件重放（`rebuild`）。
    """
    from .memory import MemoryProjection
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if embedding_provider is not None:
        from .memory_index import MemoryIndex

        kwargs["index"] = MemoryIndex(log.conn, provider=embedding_provider)
    return MemoryProjection(
        log.conn, log=log, blob_dir=blob_dir, data_root=data_root, **kwargs
    )


def _canonical_root(binding: object) -> str:
    """workspace 绑定的规范化根。

    作用域按 canonical 路径算（`resources.MemoryScope.workspace` 也会再
    `realpath` 一次）：`/a/b`、`/a/./b` 必须是同一个作用域，否则用户会在
    三个"不同"的 workspace 记忆里找不到十分钟前存的东西。
    """
    return os.path.realpath(str(getattr(binding, "root_path", binding)))



class _LoopMemoryMixin:
    """记忆装备、只读检索、内容引用读取与写工具目标解析，供 `AgentLoop` 组合继承。"""

    _memory: Any
    _contents: ContentStore | None
    _snapshots: SnapshotStore | None
    _log: EventLog

    # ---------------------------------------------------------------- 记忆

    def _memory_projection(self):
        """本 Loop 的记忆投影。第一次使用时才打开。

        "懒"不是省事：不碰记忆的 Run 不需要记忆表，也不该因为投影打不开就
        整体跑不了。但**记忆工具一旦被调用**，缺投影就是明确失败（见
        `_exec_memory_read`），不会静默当成"这个任务没有记忆"。
        """
        if self._memory is None:
            self._memory = open_memory(self._log, data_root=self._tasks.data_root)
        return self._memory

    def _memory_allowed(self, task: Task) -> tuple[MemoryScope, ...]:
        """本 Run 的装备（F10 §6）：只装它有权访问的作用域。

        `loadout()` 是唯一的装备判定点：user + 本任务每个绑定目录的
        canonical 根 + 本任务自身。**不注入全库**。模型不能自己传 allowed——
        那等于让它给自己发权限。
        """
        roots = [_canonical_root(b) for b in task.workspaces]
        loaded = self._memory_projection().loadout(
            task_id=task.task_id, workspace_roots=roots
        )
        return tuple(loaded)

    def _memory_priority(self, task: Task):
        """这次检索"离谁更近"：当前任务 + 本 Run 绑定的 workspace（F10 §8）。"""
        from .memory import RecallPriority

        roots = tuple(_canonical_root(b) for b in task.workspaces)
        return RecallPriority(task_id=task.task_id, workspace_roots=roots)

    def _exec_memory_read(
        self,
        run_id: str,
        task: Task,
        call_id: str,
        tool_id: str,
        args: dict[str, Any],
    ) -> ToolResult:
        """记忆只读工具：不需要 workspace 绑定，但仍要留痕。

        `memory.search` / `memory.get` 是**读**：不改变任何用户可见状态，所以
        结果一律记 `none_applied`（与 `_exec_read` 同一口径）。零目录任务也能
        用——记忆不是文件，硬要它先绑一个目录，等于把"没有目录"误判成"没有
        记忆"，这正是 F10 明确拒绝的形状。
        """
        try:
            projection = self._memory_projection()
            spec = SPECS.get(tool_id)
            if spec is None:
                return ToolResult(
                    "rejected", "none_applied", {"error": f"工具不在白名单：{tool_id}"}
                )
            args = validate_args(args, spec.input_schema)
            allowed = self._memory_allowed(task)
            if tool_id == "memory.search":
                output = self._memory_search_output(
                    projection, args, allowed, self._memory_priority(task)
                )
            else:
                output = self._memory_get_output(projection, args, allowed)
            return ToolResult("succeeded", "none_applied", output)
        except (ToolArgumentError, ResourceError) as exc:
            return ToolResult(
                "rejected", "none_applied", {"error": f"{type(exc).__name__}: {exc}"}
            )
        except Exception as exc:  # noqa: BLE001 - 记忆投影自身炸了：如实失败
            return ToolResult("failed", "none_applied", {"error": repr(exc)})

    @staticmethod
    def _memory_search_output(projection, args, allowed, priority=None) -> dict[str, Any]:
        from .memory import MAX_RECALL_BYTES, MAX_RECALL_ITEMS

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentError("记忆检索的 query 必须是非空字符串")
        limit = args.get("limit", MAX_RECALL_ITEMS)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ToolArgumentError(f"记忆检索的 limit 必须是正整数：{limit!r}")
        # 首版硬上限（F10 §8）：模型可以要求更少，不能要求更多；
        # 字节上限不由模型控制——预算是执行侧的事。
        limit = min(limit, MAX_RECALL_ITEMS)
        outcome = projection.retrieve(
            query,
            allowed=allowed,
            limit=limit,
            max_bytes=MAX_RECALL_BYTES,
            priority=priority,
        )
        items = [
            _LoopMemoryMixin._memory_item(
                item.memory,
                item.content,
                score=item.score,
                # 同一字段两种口径，必须让模型能分辨：origin 权重 ≠ RRF 分数。
                score_kind=item.score_kind,
                # 合成层的新鲜度也要给模型看：陈旧页要回落 L1 证据核对。
                stale=item.stale,
            )
            for item in outcome.items
        ]
        return {
            "query": query,
            "items": items,
            "count": len(items),
            "limit": limit,
            "max_bytes": MAX_RECALL_BYTES,
            "allowed": [scope.to_dict() for scope in allowed],
            # 如实报告这一路有没有语义能力：`semantic=false` 不等于"没找到"，
            # 原因在 `degraded` 里（未配置 provider / 索引与 provider 不匹配）。
            "semantic": outcome.semantic,
            "degraded": outcome.degraded,
            "fused": outcome.fused,
        }

    @staticmethod
    def _memory_get_output(projection, args, allowed) -> dict[str, Any]:
        memory_id = args.get("memory_id") or args.get("id")
        if not isinstance(memory_id, str) or not memory_id:
            raise ToolArgumentError("memory.get 需要 memory_id")
        memory = projection.get(memory_id, allowed=allowed)
        if memory is None:
            # 不在装备内与不存在给同一个回答：区分它们等于让模型用"存在性"
            # 探测另一个 workspace 的私有记忆。
            return {
                "found": False,
                "memory_id": memory_id,
                "error": "记忆不存在或不在本次 Run 的装备内",
            }
        # 已遗忘 / 已擦除的记忆**不读正文**。`forget` 只改状态、不动正文
        # blob（擦除才删），所以忘记后正文其实还在盘上；照旧读出来就等于
        # `memory.get` 成了绕开 `forget` 的旁路——检索忘了，按 id 仍能捞回
        # 原话，"忘记"就只是对 search 生效的假动作。
        if memory.status == "forgotten" or memory.erased_at is not None:
            content = None
        else:
            content = projection.read_content(memory)
        if content is None:
            # F10 §10：历史内容快照缺失时明确报"不可用"，**不回落**取当前
            # 记忆库的最新版本。回落会把"当时看到了什么"换成"现在是什么"，
            # 回放立刻变成假证据。
            return {
                "found": True,
                "memory_id": memory.memory_id,
                "revision": memory.revision,
                "content_available": False,
                "error": (
                    "历史证据不可用："
                    + (
                        "记忆正文已擦除（正文副本与索引已删；事件日志里的"
                        "历史读快照仍在，不在这里提供）"
                        if memory.erased_at is not None
                        else (
                            "记忆已遗忘，正文不再提供"
                            if memory.status == "forgotten"
                            else "内容快照缺失"
                        )
                    )
                    + "（不回落到当前记忆库的正文）"
                ),
                "memory": memory.to_dict(),
            }
        return {
            "found": True,
            "content_available": True,
            "content": content,
            "memory": _LoopMemoryMixin._memory_item(memory, content),
        }

    @staticmethod
    def _memory_selections(view: ContextView) -> list[dict[str, Any]]:
        """这一轮**真正进入视图**的记忆身份（F10 §8）：id + revision + 快照引用。

        只从编译后的 `view.messages` 里取，不从完整历史取：被预算压掉、或被
        落盘占位替换掉的记忆没有进入模型看到的那份上下文，就不该记成"选中"。
        这不是第二份事实：记忆正文与出处已经在 `tool.result` 事件里，这里记的
        是"这一轮发给模型的视图里还活着哪几条"，供回放核对。
        """
        selections: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for message in view.messages:
            if message.get("role") != "tool":
                continue
            if message.get("name") not in MEMORY_READ_TOOLS:
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                # 内容已被预算替换成占位（落盘/预览）：原文不在这一轮视图里，
                # 也就不能算"进入上下文的记忆"。
                continue
            if not isinstance(payload, dict):
                continue
            candidates: list[dict[str, Any]] = []
            single = payload.get("memory")
            if isinstance(single, dict):
                candidates.append(single)
            items = payload.get("items")
            if isinstance(items, list):
                candidates.extend(item for item in items if isinstance(item, dict))
            for item in candidates:
                memory_id = item.get("memory_id")
                revision = item.get("revision")
                if not isinstance(memory_id, str) or not memory_id:
                    continue
                if isinstance(revision, bool) or not isinstance(revision, int):
                    continue
                key = (memory_id, revision)
                if key in seen:
                    continue
                seen.add(key)
                selections.append(
                    {
                        "memory_id": memory_id,
                        "revision": revision,
                        "content_ref": item.get("content_ref"),
                        "scope": item.get("scope"),
                        "scope_id": item.get("scope_id"),
                        "tool_call_id": message.get("tool_call_id"),
                    }
                )
        return selections

    @staticmethod
    def _memory_item(
        memory,
        content: str | None,
        *,
        score: float | None = None,
        score_kind: str | None = None,
        stale: bool | None = None,
    ):
        """一条记忆的出处的投影：正文 + 身份 + 来源引用。

        `source_refs` / `scope` / `revision` 必须一起给模型看：没有出处的
        "记忆"只是一段无法核对的断言，F10 要求每条记忆能指回来源。
        """
        item = memory.to_dict()
        item["content_ref"] = memory.content_ref
        item["content"] = content
        if score is not None:
            item["score"] = score
        if score_kind is not None:
            item["score_kind"] = score_kind
        if stale is not None:
            item["stale"] = stale
        return item

    def _exec_content_read(
        self, task: Task, call_id: str, args: dict[str, Any]
    ) -> ToolResult:
        """读一份内容引用的分页（`read_content` 工具的执行体）。

        为什么读工具不进派发：`Dispatcher._typecheck()` 的既有口径是"只读动作
        不经过派发（没有可授权的副作用）"，`read_text` / `memory.search` 都在
        这条路上。内容引用沿用同一口径，**不是**开了一个后门——它和它们一样
        要过白名单、schema 与参数校验，结果同样落 `tool.result` 留痕；差别只在
        "可见范围"这个判据由内容存储执行，因为除了它没有人知道 `(digest, task)`
        的授权行。

        `call_id` 只用于错误信息定位，不参与授权：授权看的是**任务**，不是
        某一次调用。
        """
        spec = SPECS.get("read_content")
        if spec is None:  # pragma: no cover - 注册表是模块常量
            return ToolResult(
                "rejected", "none_applied", {"error": "read_content 未注册"}
            )
        if self._contents is None:
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": "内容存储未配置：本次运行没有可用的内容引用"
                    "（EventLog 与 SnapshotStore 都没有 blob 目录）"
                },
            )
        try:
            checked = validate_args(args, spec.input_schema)
            page = self._contents.read(
                checked.get("ref"),
                task_id=task.task_id,
                offset=int(checked.get("offset", 0)),
                length=int(checked.get("length", DEFAULT_PAGE)),
                encoding=str(checked.get("encoding", "utf-8")),
            )
            return ToolResult("succeeded", "none_applied", page.to_payload())
        except (ToolArgumentError, TianshuError) as exc:
            # 形状/越权/缺失/摘要不符都是**确定的**拒绝：读没有副作用，所以
            # 一律 none_applied（记成 unknown 会把"读越权了"变成"可能写过"）。
            return ToolResult(
                "rejected", "none_applied", {"error": f"{type(exc).__name__}: {exc}"}
            )
        except Exception as exc:  # noqa: BLE001 - 工具自身炸了
            return ToolResult("failed", "none_applied", {"error": repr(exc)})

    def _exec_read(
        self,
        run_id: str,
        task: Task,
        call_id: str,
        tool_id: str,
        args: dict[str, Any],
    ) -> ToolResult:
        """只读工具：不经派发，但同样要留痕。

        纯读取不可能改变磁盘，所以失败一律记 `none_applied`——把它记成
        "unknown 效果"会让一个只是读越界了的 Run 挡住整个目录的写入。

        记忆只读工具在**任何目录解析之前**分流：它们没有文件目标，零目录任务
        也必须能检索。这不是"给记忆编一个假路径"，而是承认它是另一种资源。
        """
        if tool_id in MEMORY_READ_TOOLS:
            return self._exec_memory_read(run_id, task, call_id, tool_id, args)
        if tool_id == "read_content":
            # 内容引用不是文件：没有 workspace 也能读（零目录任务同样合法），
            # 可见范围按**任务**授权，由内容存储自己核（PRD §5.2.3）。
            return self._exec_content_read(task, call_id, args)
        root = self._primary_root(task)
        if root is None:
            return ToolResult(
                "rejected", "none_applied", {"error": "任务没有绑定任何目录"}
            )
        ctx = ToolContext(
            snapshots=self._snapshots, log=self._log, run_id=run_id, call_id=call_id
        )
        try:
            result = execute(Workspace(root), tool_id, args, ctx)
        except WorkspaceBlocked:
            raise
        except (WorkspaceViolation, OverwriteRefused, ToolArgumentError) as exc:
            return ToolResult(
                "rejected", "none_applied", {"error": f"{type(exc).__name__}: {exc}"}
            )
        except Exception as exc:  # noqa: BLE001 - 工具自身炸了
            return ToolResult("failed", "none_applied", {"error": repr(exc)})
        return ToolResult(result.status, "none_applied", result.output)

    def _target_for(
        self, task: Task, tool_id: str, args: dict[str, Any]
    ) -> Path | str:
        """把工具的目标解析成绝对路径，或记忆作用域 URI。

        多目录任务里"相对路径解析到哪个目录"是 M2 未解决的选择问题：这里用
        **第一个可写绑定**（没有可写绑定时用第一个绑定），并在策略层再判一次
        目标是否被某个绑定覆盖。绝对路径则直接按绑定覆盖关系判。

        **记忆不是文件**：它没有路径参数（`path_arg`），也不许被编一个假路径
        去骗过路径解析——那会让"删掉投影"变成"删掉用户文件"。所以按工具种类
        分流，目标是 `memory://<scope>/<scope_id>`，由 `resources` 原样携带。
        """
        from .tool_registry import current_registry

        mcp_target = current_registry().mcp_binding(tool_id)
        if mcp_target is not None:
            # MCP 工具的目标不是文件、也不是记忆作用域：一个 `mcp://server/tool`
            # 资源。参数里的内容（server/tool）由注册表决定，模型参数不得改写它。
            return mcp_target.uri
        if tool_id == "network.search":
            from .web_search import search_target

            return search_target(args).to_uri()
        if tool_id in NETWORK_TOOL_IDS:
            # 网络目标不是文件：零目录任务也必须能抓取，所以这一支必须在
            # "任务必须绑定目录"之前。参数只接受规范写法，`https://` 会被
            # 明确拒绝并给出改法（见 net.canonical_target）。
            from .net import NetError, canonical_target

            raw = args.get("url")
            if not isinstance(raw, str) or not raw:
                raise ToolArgumentError("网络工具的 url 必须是非空字符串")
            try:
                target = canonical_target(raw)
            except NetError as exc:
                raise ToolArgumentError(str(exc)) from exc
            return target.to_uri()
        if tool_id in SUBTASK_TOOL_IDS:
            # 子任务是父 Run 作用域里的一个 slug，不是文件：零目录任务同样合法，
            # 所以这一支也在"任务必须绑定目录"之前。形状校验走 resources 的
            # 既有解析器（不新写一份规则）。
            from .resources import ResourceError, parse_resource

            raw = args.get("subtask_id")
            if not isinstance(raw, str) or not raw:
                raise ToolArgumentError("子任务工具的 subtask_id 必须是非空字符串")
            try:
                ref = parse_resource(f"subtask://{raw}")
            except ResourceError as exc:
                raise ToolArgumentError(str(exc)) from exc
            return ref.uri
        if tool_id in MEMORY_WRITE_TOOLS:
            return self._memory_target(task, tool_id, args)
        key = target_arg(tool_id)
        raw = args.get(key)
        spec = SPECS.get(tool_id)
        if not isinstance(raw, str) or not raw:
            # run_command 的 cwd 是可选参数：缺省时在 workspace 根执行。
            # 其他工具没有这个默认值——文件目标必须由模型显式给出。
            raw = spec.path_default if spec is not None else ""
        if not isinstance(raw, str) or not raw:
            raise ToolArgumentError(f"目标路径必须是非空字符串：{key}")
        root = self._primary_root(task)
        if root is None:
            raise ToolArgumentError("任务没有绑定任何目录，无法解析写入目标")
        return Workspace(root).resolve(raw)

    def _memory_target(self, task: Task, tool_id: str, args: dict[str, Any]) -> str:
        """写记忆工具的目标：一个**已装备作用域**的 `memory://` URI。

        目标不是文件路径，也从磁盘读不到任何东西。`confirm` / `forget` 的
        目标由要操作的那条记忆自己决定（先按装备过滤查出来）；`propose`
        首版只有一个作用域：F10 §3.3 的默认私有 user/local（共享是另一个
        显式动作，本单不做）。任何解析结果都必须落在本 Run 的装备内——
        模型不能凭参数越作用域写入。
        """
        projection = self._memory_projection()
        allowed = self._memory_allowed(task)

        # confirm / forget 操作的是某条已存在的记忆：它属于哪个作用域由记忆
        # 自己说了算，不是模型说了算。查不到就是"不在装备内"，fail-closed。
        memory_id = args.get("memory_id") or args.get("id")
        if (
            tool_id in (
                "memory.confirm",
                "memory.forget",
                "memory.edit",
                "memory.promote",
                "memory.erase",
            )
            and memory_id
        ):
            if not isinstance(memory_id, str):
                raise ToolArgumentError(f"memory_id 必须是字符串：{memory_id!r}")
            memory = projection.get(memory_id, allowed=allowed)
            if memory is None:
                raise ToolArgumentError(
                    f"记忆不存在或不在本次 Run 的装备内：{memory_id}"
                )
            scope = MemoryScope(memory.scope, memory.scope_id)
            self._assert_scope_allowed(scope, allowed)
            return scope.to_uri()

        scope = self._memory_scope_from_args(task, args)
        self._assert_scope_allowed(scope, allowed)
        return scope.to_uri()

    def _memory_scope_from_args(
        self, task: Task, args: dict[str, Any]
    ) -> MemoryScope:
        """从工具参数解出作用域。显式 `memory://` URI 优先，其次 scope 参数。"""
        raw_target = args.get("target")
        if isinstance(raw_target, str) and raw_target:
            ref = parse_resource(raw_target)
            if ref.kind != KIND_MEMORY:
                raise ToolArgumentError(
                    f"记忆工具的目标必须是 memory:// 引用：{raw_target!r}"
                )
            return ref.memory_scope

        raw_scope = args.get("scope")
        if raw_scope is None:
            # F10 §3.3：新记忆默认私有（user/local），共享是显式动作。
            return MemoryScope.user()
        if not isinstance(raw_scope, str):
            raise ToolArgumentError(f"scope 必须是字符串：{raw_scope!r}")

        raw_scope_id = args.get("scope_id")
        if raw_scope_id is not None and not isinstance(raw_scope_id, str):
            raise ToolArgumentError(f"scope_id 必须是字符串：{raw_scope_id!r}")

        if raw_scope == SCOPE_USER:
            return MemoryScope(SCOPE_USER, raw_scope_id or USER_SCOPE_ID)
        if raw_scope == SCOPE_TASK:
            return MemoryScope.task(raw_scope_id or task.task_id)
        if raw_scope == SCOPE_WORKSPACE:
            root = raw_scope_id
            if root is None:
                primary = self._primary_root(task)
                if primary is None:
                    raise ToolArgumentError(
                        "workspace 作用域需要显式 scope_id：本任务没有绑定目录"
                    )
                root = str(primary)
            return MemoryScope.workspace(root)
        # `MemoryScope` 的构造会拒绝未知作用域；走到这里说明上面漏了一种，
        # 也按 fail-closed 处理，不猜。
        raise ToolArgumentError(
            f"未知记忆作用域：{raw_scope!r}（合法值：user / workspace / task）"
        )

    @staticmethod
    def _assert_scope_allowed(
        scope: MemoryScope, allowed: tuple[MemoryScope, ...]
    ) -> None:
        if scope not in allowed:
            raise ToolArgumentError(
                "作用域不在本次 Run 的装备内："
                f"{scope.scope}/{scope.scope_id}（已装备："
                f"{[s.to_dict() for s in allowed]}）"
            )

    @staticmethod
    def _primary_root(task: Task) -> Path | None:
        writable = [b for b in task.workspaces if b.can_write]
        pool = writable or list(task.workspaces)
        if not pool:
            return None
        return Path(pool[0].root_path)
