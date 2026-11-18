"""CLI 记忆控制面子命令（内部模块，对外入口见 `tianshu.cli`）。

只负责命令行参数解析与输出格式化；实际记忆读写与审批派发均委托给 `memory_control`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import memory_control
from ._cli_ops import _authority_stack
from .errors import TianshuError
from .memory import MEMORY_KINDS
from .memory_control import memory_view
from .resources import MEMORY_SCOPES, MemoryScope, build_memory_scope


def add_memory_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    mem = sub.add_parser(
        "memory",
        aliases=["memories"],
        help="记忆控制面：Agent 记住了什么、依据是什么；确认候选 / 忘记一条",
    )
    mem.add_argument(
        "action",
        choices=[
            "list",
            "search",
            "get",
            "confirm",
            "forget",
            "remember",
            "edit",
            "promote",
            "erase",
            "rebuild-vectors",
        ],
    )
    mem.add_argument(
        "value", nargs="?",
        help="search 的查询词；get/confirm/forget 的 memory_id",
    )
    mem.add_argument(
        "--scope", default=None, choices=sorted(MEMORY_SCOPES),
        help="把某个作用域加入可见集合（默认只装备 user 作用域）",
    )
    mem.add_argument(
        "--scope-id", default=None,
        help="与 --scope 一起使用；workspace 作用域必须给绝对路径",
    )
    mem.add_argument(
        "--workspace", type=Path, default=None, action="append",
        help="把该目录的 workspace 作用域加入可见集合（可重复）",
    )
    mem.add_argument(
        "--task-id", default=None, help="把该 task 作用域加入可见集合"
    )
    mem.add_argument("--status", default=None,
                     choices=["candidate", "active", "superseded", "forgotten"])
    mem.add_argument("--limit", type=int, default=50)
    mem.add_argument(
        "--kind", default="preference",
        choices=sorted(MEMORY_KINDS),
        help="remember 的记忆种类（默认 preference）",
    )
    mem.add_argument(
        "--expires-at", type=int, default=None,
        help="remember 的过期时间（毫秒时间戳，可选）",
    )
    mem.add_argument(
        "--content", default=None, help="edit 的新版正文"
    )
    mem.add_argument(
        "--to-scope", default=None, choices=sorted(MEMORY_SCOPES),
        help="promote 的目标作用域",
    )
    mem.add_argument(
        "--to-scope-id", default=None,
        help="promote 的目标 scope_id（workspace 必须绝对路径；task 默认用 --task-id）",
    )
    mem.add_argument("--data-root", type=Path, default=None)
    mem.add_argument("--db", type=Path, default=cli.DEFAULT_DB)
    mem.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                     help="使用桌面端默认数据目录")
    mem.add_argument("--json", action="store_true")


def _cli_memory_scopes(args: argparse.Namespace) -> tuple[MemoryScope, ...]:
    """命令行能看到的作用域。**

    默认只有 `user`：单用户产品的本地人当然是 owner，但"默认全库"会让一条
    命令的输出取决于库里恰好有什么。要看 workspace / task 记忆就把作用域
    显式加进来——这与 F10 §6「一个 Run 只装备它有权访问的作用域」是同一套
    口径，只是这里的主体是人，不是模型。
    """
    scopes: list[MemoryScope] = [MemoryScope.user()]
    for path in args.workspace or []:
        scopes.append(MemoryScope.workspace(path))
    if args.task_id:
        scopes.append(MemoryScope.task(args.task_id))
    if args.scope:
        scope_id = args.scope_id
        if scope_id is None:
            if args.scope == "user":
                scope_id = "local"
            elif args.scope == "task":
                if not args.task_id:
                    raise TianshuError("--scope task 需要 --task-id（或 --scope-id）")
                scope_id = args.task_id
            else:
                raise TianshuError("--scope workspace 需要 --scope-id（绝对路径）")
        scopes.append(MemoryScope(args.scope, scope_id))

    seen: set[MemoryScope] = set()
    deduped: list[MemoryScope] = []
    for scope in scopes:
        if scope not in seen:
            seen.add(scope)
            deduped.append(scope)
    return tuple(deduped)


def _memory_public(
    memory, content: str | None = None, *, score=None, score_kind=None
) -> dict:
    """一条记忆对人和 JSON 的统一投影（实现在 `memory_control.memory_view`）。

    六项判据（`scope` / `origin` / `revision` / `status` / `source_refs` /
    `proof_count`）由那个函数保证，CLI 与 UI 视图不许各写一份——写两份就会
    漂移，最后变成"命令行能看见、UI 看不见"。
    """
    return memory_view(memory, content, score=score, score_kind=score_kind)


def _print_memory(
    memory, content: str | None = None, *, score=None, score_kind=None
) -> None:
    payload = _memory_public(memory, content, score=score, score_kind=score_kind)
    print(f"memory_id : {payload['memory_id']}")
    print(f"revision  : {payload['revision']}")
    print(f"scope     : {payload['scope']}  {payload['scope_id']}")
    print(f"status    : {payload['status']}")
    print(f"kind      : {payload['kind']}   origin: {payload['origin']}")
    print(f"source_refs: {', '.join(payload.get('source_refs') or []) or '（无）'}")
    print(f"证据      : proof_count={payload.get('proof_count', 0)}")
    print(f"内容引用  : {payload.get('content_ref') or '（无）'}")
    if payload.get("created_at"):
        print(f"创建/过期 : {payload['created_at']} / {payload.get('expires_at') or '（无）'}")
    if payload.get("supersedes"):
        print(f"取代      : {payload['supersedes']}")
    if content is not None:
        print("内容      :")
        for line in content.splitlines() or [""]:
            print(f"  {line}")


def _cmd_memory(args: argparse.Namespace) -> int:
    stack = _authority_stack(args.db, args.data_root)
    try:
        projection = stack.memory
        allowed = _cli_memory_scopes(args)

        if args.action == "list":
            rows = projection.list_memories(
                allowed=allowed, status=args.status, limit=args.limit
            )
            if args.json:
                print(json.dumps(
                    [_memory_public(m) for m in rows],
                    ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("没有符合条件的记忆")
                return 0
            for index, memory in enumerate(rows):
                if index:
                    print("")
                _print_memory(memory)
            return 0

        if args.action == "remember":
            if not args.value:
                print("错误：memory remember 需要记忆内容", file=sys.stderr)
                return 2
            scope = MemoryScope.user()
            if args.task_id:
                scope = MemoryScope.task(args.task_id)
            elif args.scope and args.scope != "user":
                print(
                    "错误：remember 只支持 user / task 作用域；"
                    "共享到 workspace 由 memory promote 负责（N1 后续提交）",
                    file=sys.stderr,
                )
                return 2
            return _remember_memory(stack, scope, args)

        if args.action == "rebuild-vectors":
            # 语义召回的**构建入口**：只装配 provider 还不够，必须显式建一次
            # 索引；否则 availability() 报"尚未建立"，检索永远 FTS-only。
            if getattr(projection, "index", None) is None:
                print(
                    "错误：当前没有配置向量 provider（设置 "
                    "TIANSHU_EMBEDDING_PROVIDER=hash 或由装配方传入）；"
                    "没有索引可建",
                    file=sys.stderr,
                )
                return 2
            written = projection.rebuild_vectors()
            available, reason = projection.index.availability()
            if args.json:
                print(json.dumps(
                    {"rows": written, "available": available, "reason": reason},
                    ensure_ascii=False, indent=2))
                return 0
            print(f"已重建向量索引：{written} 条；可用={available}；原因={reason or '—'}")
            return 0

        if not args.value:
            print(f"错误：memory {args.action} 需要查询词 / memory_id",
                  file=sys.stderr)
            return 2

        if args.action == "search":
            from .memory import MAX_RECALL_BYTES, MAX_RECALL_ITEMS, RecallPriority

            # 控制面检索也带上"离谁更近"的上下文：给了 --task-id / --workspace
            # 就按 F10 §8 排序，否则退化成 origin → 时间（不猜调用方意图）。
            priority = RecallPriority(
                task_id=args.task_id,
                workspace_roots=tuple(scope.scope_id for scope in allowed
                                      if scope.scope == "workspace"),
            )
            outcome = projection.retrieve(
                args.value,
                allowed=allowed,
                limit=min(max(args.limit, 1), MAX_RECALL_ITEMS),
                max_bytes=MAX_RECALL_BYTES,
                priority=priority,
                # 控制面显式动作（人敲命令）：允许 embedding 外发（若配置了
                # 外部 provider）——与面板 RPC 同口径（W6 外发 gate）。
                allow_embedding_outbound=True,
            )
            recalled = outcome.items
            if args.json:
                # 检索结果就是模型会看到的那份（F10 §8：带出处，不是全库）。
                print(json.dumps(
                    {
                        "items": [
                            _memory_public(
                                r.memory,
                                r.content,
                                score=r.score,
                                score_kind=r.score_kind,
                            )
                            for r in recalled
                        ],
                        # 能力边界也进机器可读输出：`semantic=false` 不是"没找到"，
                        # 原因在 `degraded`。
                        "semantic": outcome.semantic,
                        "degraded": outcome.degraded,
                        "fused": outcome.fused,
                    },
                    ensure_ascii=False, indent=2, default=str))
                return 0
            if not recalled:
                print("没有检索到记忆")
                return 0
            print(f"检索到 {len(recalled)} 条（最多 {MAX_RECALL_ITEMS} 条 / "
                  f"{MAX_RECALL_BYTES}B）：")
            if not outcome.semantic:
                print(f"（本次只有词面召回：{outcome.degraded}）")
            for index, item in enumerate(recalled):
                if index:
                    print("")
                _print_memory(
                    item.memory,
                    item.content,
                    score=item.score,
                    score_kind=item.score_kind,
                )
            return 0

        memory = projection.get(args.value, allowed=allowed)
        if memory is None:
            print(
                f"错误：记忆不存在或不在当前作用域集合内：{args.value}\n"
                "（非 user 作用域请用 --workspace / --task-id / --scope 加上）",
                file=sys.stderr,
            )
            return 1

        if args.action == "get":
            content = projection.read_content(memory)
            if args.json:
                print(json.dumps(
                    _memory_public(memory, content),
                    ensure_ascii=False, indent=2, default=str))
            else:
                _print_memory(memory, content)
                if content is None:
                    print("警告：历史证据不可用：内容快照缺失"
                          "（不回落到当前记忆库）", file=sys.stderr)
            return 0 if content is not None else 1

        if args.action == "edit":
            if not args.content:
                print("错误：memory edit 需要 --content（新版正文）", file=sys.stderr)
                return 2
            return _dispatch_memory_write(
                stack,
                memory,
                "memory.edit",
                {
                    "memory_id": memory.memory_id,
                    "revision": memory.revision,
                    "content": args.content,
                },
                args,
            )

        if args.action == "promote":
            if not args.to_scope:
                print("错误：memory promote 需要 --to-scope / --to-scope-id",
                      file=sys.stderr)
                return 2
            return _dispatch_memory_promote(stack, memory, args)

        if args.action == "erase":
            return _dispatch_memory_write(
                stack,
                memory,
                "memory.erase",
                {"memory_id": memory.memory_id, "revision": memory.revision},
                args,
            )

        # confirm / forget 是**人**发起的记忆生命周期操作：人就是批准者本身，
        # 所以这里用本地人身份把那张审批当场签掉。它仍然走完整派发路径——
        # intent/result + Receipt + 一次性消费——不是控制面偷写存储。
        result = _dispatch_memory_decision(
            stack, memory, confirm=args.action == "confirm"
        )
        updated = projection.get(
            memory.memory_id,
            allowed=(MemoryScope(memory.scope, memory.scope_id),),
        )
        if args.json:
            print(json.dumps(
                {
                    "memory_id": memory.memory_id,
                    "action": args.action,
                    "status": result.status,
                    "effect_state": result.effect_state,
                    "output": result.output,
                    "memory": _memory_public(updated) if updated is not None else None,
                },
                ensure_ascii=False, indent=2, default=str))
        else:
            print(f"{memory.memory_id}: {args.action} -> {result.status}")
            if updated is not None:
                # 人做完决定后要看得到"现在是什么状态、依据还在不在"。
                print(f"  scope: {updated.scope} {updated.scope_id}")
                print(f"  revision: {updated.revision}")
                print(f"  status: {updated.status}")
                print(f"  source_refs: {', '.join(updated.source_refs) or '（无）'}")
            if not result.ok:
                print(f"  ! {result.output.get('error')}", file=sys.stderr)
        return 0 if result.ok else 1
    finally:
        stack.log.close()


def _dispatch_memory_decision(stack, memory, *, confirm: bool):
    """把「确认 / 忘记」变成一次经人签字的派发。

    与 `undo` 的 `--approve` 同一形状：用户明确敲了这条命令，就是批准；审批
    仍然落库、仍然一次性消费、执行侧仍然二次核验 digest。**不**给模型开一条
    "人已经批过了"的后门——这里根本没有模型参与。

    实现在 `memory_control.transition`：CLI 与 daemon 的 RPC 共用同一份，避免
    "写记忆要经 dispatcher"这条纪律被抄成两份（一份早晚会图省事直接改投影）。
    """
    return memory_control.transition(stack, memory, confirm=confirm)


def _dispatch_memory_write(stack, memory, tool_id: str, tool_args: dict, args) -> int:
    """把一次"人签字的记忆写"变成经控制 Run 的真实派发（人敲命令 = 批准）。

    `edit` / `erase` 的 args 形状不同，由 `memory_control` 各自的入口负责构造；
    这里只剩"派发 + 打印结果"——打印是 CLI 的职责，不是控制面语义的一部分。
    """
    if tool_id == "memory.edit":
        result = memory_control.edit(stack, memory, content=str(tool_args["content"]))
    elif tool_id == "memory.erase":
        result = memory_control.erase(stack, memory)
    else:  # pragma: no cover - 关集：只允许这两个别的工具没有控制面入口
        raise TianshuError(f"未知的控制面记忆写：{tool_id}")
    if args.json:
        print(json.dumps(
            {
                "action": tool_id,
                "memory_id": memory.memory_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{memory.memory_id}: {tool_id} -> {result.status}")
        if result.output.get("memory_id"):
            print(f"  new memory_id: {result.output['memory_id']}")
        if result.output.get("supersedes"):
            print(f"  supersedes   : {result.output['supersedes']}")
        erasure = result.output.get("erasure")
        if isinstance(erasure, dict):
            state = "已删除" if erasure.get("deleted") else "被其它记忆引用，保留"
            print(f"  正文副本     : {state}（{erasure.get('content_ref')}）")
            if erasure.get("retained_by"):
                print(f"  仍被引用     : {', '.join(erasure['retained_by'])}")
        if not result.ok and result.output.get("error"):
            print(f"  ! {result.output.get('error')}", file=sys.stderr)
    return 0 if result.ok else 1


def _dispatch_memory_promote(stack, memory, args) -> int:
    """把一条记忆移动（共享）到 --to-scope；原行退役、目标作用域新行是候选。"""
    new_scope_id = args.to_scope_id
    if new_scope_id is None:
        if args.to_scope == "user":
            new_scope_id = "local"
        elif args.to_scope == "task":
            if not args.task_id:
                raise TianshuError("--to-scope task 需要 --task-id 或 --to-scope-id")
            new_scope_id = args.task_id
        else:
            raise TianshuError("--to-scope workspace 需要 --to-scope-id（绝对路径）")
    try:
        new_scope = build_memory_scope(args.to_scope, new_scope_id)
    except TianshuError as exc:
        raise TianshuError(f"提升目标作用域不合法：{exc}") from exc

    result = memory_control.promote(stack, memory, new_scope)
    if args.json:
        print(json.dumps(
            {
                "action": "memory.promote",
                "memory_id": memory.memory_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{memory.memory_id}: promote -> {result.status}")
        print(f"  新行: {result.output.get('memory_id')} "
              f"({result.output.get('scope')} {result.output.get('scope_id')}, "
              f"{result.output.get('status')})")
        print("  原行已退役（保留可查）；新行是候选，需要再 confirm 才召回")
    return 0 if result.ok else 1


def _remember_memory(stack, scope, args) -> int:
    """`tianshu memory remember`：人直接记住一条（控制面专属工具）。

    与 `undo --approve` 同一形状：人敲了命令就是签发者，工具本身不需要审批；
    但仍走完整 intent/result + Receipt 与执行侧二次核验。模型清单里没有
    `memory.remember`，所以模型不可能走这条路自封"用户明示"。
    """
    result = memory_control.remember(
        stack,
        scope,
        kind=args.kind,
        content=args.value,
        expires_at=args.expires_at,
    )
    output = result.output or {}
    if args.json:
        print(json.dumps(
            {
                "action": "remember",
                "status": result.status,
                "effect_state": result.effect_state,
                "output": output,
            },
            ensure_ascii=False, indent=2, default=str))
    elif result.ok:
        print(f"{output.get('memory_id')}: remember -> active")
        print(f"  scope: {output.get('scope')} {output.get('scope_id')}")
        print(f"  kind : {output.get('kind')}   origin: {output.get('origin')}")
    else:
        print(f"remember -> {result.status}", file=sys.stderr)
        if output.get("error"):
            print(f"  ! {output.get('error')}", file=sys.stderr)
    return 0 if result.ok else 1
