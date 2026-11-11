"""F10 记忆接线验收：主循环 / 上下文预算 / CLI / replay。

这一组测试只钉**接线**，不重复 `tests/test_memory_store.py` 已经覆盖的投影
语义（分词、重建、写入闸门、去重……）。每条断言都对应 F10 §6 / §8 / §10 或
本单交付清单里的一句硬要求：

- 零目录任务能检索记忆（旧代码在这里报"任务没有绑定任何目录"）；
- 一个 Run 只装备它有权访问的作用域（断言**传进 search 的 allowed 集合**）；
- 不同 workspace 不串记忆；
- 检索文本真的经过 `compile_context` 的字节预算，超预算时有明确行为；
- 忘记之后新任务检索不到；
- CLI `memory list|search|get|confirm|forget` 的人操作闭环；
- replay 用当时快照，不读当前记忆库最新版本；快照缺失时明确报"历史证据不可用"。

测试里种记忆走的是**真实投影写入入口**（`tool.result` 事件 + `apply_event`，
与 dispatcher 提交的同一形状），不是 mock：这样 CLI / 主循环拿到的就是真实
的 SQLite + FTS5 + 内容寻址正文。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from support import tool_capable_profile

from tianshu.context import ContextBudget, compile_context
from tianshu.events import EventLog, canonical_bytes
from tianshu.loop import AgentLoop, Limits, build_stack, initial_messages
from tianshu.memory import MAX_RECALL_BYTES, MAX_RECALL_ITEMS
from tianshu.model import ModelResponse, ToolCall
from tianshu.permissions import HumanIdentity
from tianshu.replay import replay
from tianshu.resources import MemoryScope


class RecordingModel:
    """按脚本返回，同时记下模型真正收到的 messages（深拷贝）。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.seen: list[list[dict]] = []

    def capability_profile(self):
        return tool_capable_profile("test-double", "RecordingModel")

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        self.seen.append(copy.deepcopy(messages))
        if not self._responses:
            return ModelResponse(finish_reason="end_turn", content="")
        return self._responses.pop(0)


@dataclass
class Env:
    root: Path
    log: EventLog
    stack: object
    seed_run: str = "run_memory_seed"
    _seed_counter: int = field(default=0, init=False)

    # ---- 装配 ----

    def task(self, goal: str, workspaces: list[Path] | None = None):
        bindings = [str(path) for path in (workspaces or [])]
        return self.stack.tasks.create_task(goal, bindings)

    def engine(
        self,
        responses: list[ModelResponse],
        *,
        limits: Limits | None = None,
    ) -> tuple[AgentLoop, RecordingModel]:
        model = RecordingModel(list(responses))
        engine = AgentLoop(
            tasks=self.stack.tasks,
            leases=self.stack.leases,
            claims=self.stack.claims,
            dispatcher=self.stack.dispatcher,
            approvals=self.stack.approvals,
            snapshots=self.stack.snapshots,
            model=model,
            limits=limits or Limits(max_turns=8),
            memory=self.stack.memory,
        )
        return engine, model

    def run_to_end(self, task_id: str, responses, *, limits=None):
        engine, model = self.engine(list(responses), limits=limits)
        result = engine.start(task_id)
        return result, model, engine

    def resume(self, run_id: str, responses):
        engine, model = self.engine(list(responses))
        result = engine.resume_run(run_id)
        return result, model, engine

    # ---- 种记忆（真实投影写入路径） ----

    def seed(
        self,
        content: str,
        *,
        memory_id: str | None = None,
        scope: MemoryScope | None = None,
        kind: str = "preference",
        origin: str = "user_explicit",
        status: str = "active",
        source_refs: tuple[str, ...] = ("event:1",),
    ) -> str:
        projection = self.stack.memory
        scope = scope or MemoryScope.user()
        self._seed_counter += 1
        memory_id = memory_id or f"mem_seed_{self._seed_counter:04d}"

        propose = {
            "tool_id": "memory.propose",
            "status": "succeeded",
            "output": {
                "memory_id": memory_id,
                "revision": 1,
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "kind": kind,
                "content_ref": projection.put_content(content),
                "source_refs": list(source_refs),
                "evidence": [
                    {"source_memory_id": "mem_source", "quote": "原文引文"}
                ],
                "proof_count": 1,
                "origin": origin,
                "status": "candidate",
                "created_at": 1,
                "expires_at": None,
                "deduped": False,
            },
        }
        with self.log.transaction() as tx:
            seq = tx.append(self.seed_run, "tool.result", propose, legacy_write=True)
            projection.apply_event("tool.result", propose, seq=seq)

        if status == "active":
            confirm = {
                "tool_id": "memory.confirm",
                "status": "succeeded",
                "output": {
                    "memory_id": memory_id,
                    "revision": 2,
                    "before_status": "candidate",
                    "status": "active",
                    "consolidated_at": 2,
                    "memory_uri": scope.to_uri(),
                },
            }
            with self.log.transaction() as tx:
                seq = tx.append(self.seed_run, "tool.result", confirm, legacy_write=True)
                projection.apply_event("tool.result", confirm, seq=seq)
        return memory_id

    def allowed(self, task) -> tuple[MemoryScope, ...]:
        return self.stack.memory.loadout(
            task_id=task.task_id,
            workspace_roots=[str(Path(b.root_path).resolve()) for b in task.workspaces],
        )


@pytest.fixture()
def env(tmp_path: Path):
    data_root = tmp_path / "authority"
    data_root.mkdir()
    # CLI 的 `_authority_stack()` 与本 fixture 用**同一个数据库路径**：记忆
    # 正文默认落在 DB 旁的 `memory_blobs/`，所以两个各自 new EventLog 的进程
    # 看到的是同一批正文，不需要额外对齐 blob 配置。
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=data_root)
    yield Env(root=tmp_path, log=log, stack=stack)
    log.close()


# ---------------------------------------------------------------- 小工具


def _search(query: str = "报告 中文") -> ModelResponse:
    return ModelResponse(
        "tool_use", tool_calls=[ToolCall("c_search", "memory.search", {"query": query})]
    )


def _get(memory_id: str) -> ModelResponse:
    return ModelResponse(
        "tool_use",
        tool_calls=[ToolCall("c_get", "memory.get", {"memory_id": memory_id})],
    )


def _propose(content: str, **args) -> ModelResponse:
    body = {"kind": "preference", "content": content}
    body.update(args)
    return ModelResponse(
        "tool_use", tool_calls=[ToolCall("c_propose", "memory.propose", body)]
    )


def _confirm(memory_id: str, revision: int) -> ModelResponse:
    return ModelResponse(
        "tool_use",
        tool_calls=[
            ToolCall(
                "c_confirm",
                "memory.confirm",
                {"memory_id": memory_id, "revision": revision},
            )
        ],
    )


def _forget(memory_id: str, revision: int) -> ModelResponse:
    return ModelResponse(
        "tool_use",
        tool_calls=[
            ToolCall(
                "c_forget",
                "memory.forget",
                {"memory_id": memory_id, "revision": revision},
            )
        ],
    )


def _end(content: str = "完成") -> ModelResponse:
    return ModelResponse("end_turn", content=content)


def _events_by_type(env: Env, run_id: str, event_type: str) -> list:
    return [e for e in env.log.read_run(run_id) if e.type == event_type]


def _tool_output(env: Env, run_id: str, tool_id: str) -> dict:
    results = [
        e.payload
        for e in _events_by_type(env, run_id, "tool.result")
        if e.payload.get("tool_id") == tool_id
    ]
    assert results, f"run {run_id} 里没有 {tool_id} 的 tool.result"
    return results[-1]["output"]


def _intents(env: Env, run_id: str) -> list:
    return _events_by_type(env, run_id, "model.intent")


def _cli(*argv: str) -> tuple[int, str]:
    import contextlib
    import io

    from tianshu.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue() + err.getvalue()


def _approve_all(env: Env, approval_ids: list[str]) -> None:
    actor = HumanIdentity.local_cli()
    for approval_id in approval_ids:
        env.stack.approvals.decide(approval_id, decision="approved", actor=actor)


# ---------------------------------------------------------------- 0. 封闭集


def test_memory_tool_sets_match_the_registry():
    """主循环的读/写封闭集必须与工具注册表一致——改一处忘另一处要红。"""
    from tianshu.loop import MEMORY_READ_TOOLS, MEMORY_WRITE_TOOLS
    from tianshu.tools import SPECS

    registry = {tool_id for tool_id in SPECS if tool_id.startswith("memory.")}
    assert registry == MEMORY_READ_TOOLS | MEMORY_WRITE_TOOLS
    assert not (MEMORY_READ_TOOLS & MEMORY_WRITE_TOOLS)
    # 记忆工具没有文件目标参数：它们不该被拿去 Path.resolve()。
    for tool_id in registry:
        assert SPECS[tool_id].path_arg == "", f"{tool_id} 不该有 path_arg"


# ---------------------------------------------------------------- 1. 零目录


def test_zero_directory_task_can_recall_memory(env: Env):
    """记忆检索不需要 workspace 绑定——旧代码在这里直接拒绝。"""
    memory_id = env.seed("报告用中文，先列结论", memory_id="mem_zero")
    task = env.task("回忆我的写作偏好")  # 刻意零绑定

    result, _, _ = env.run_to_end(task.task_id, [_search(), _end()])

    assert result.state == "succeeded", result.error
    output = _tool_output(env, result.run_id, "memory.search")
    assert output["count"] == 1
    item = output["items"][0]
    assert item["memory_id"] == memory_id
    assert item["revision"] == 2
    assert item["status"] == "active"
    assert item["content"] == "报告用中文，先列结论"
    assert item["source_refs"] == ["event:1"]

    # 读也要留痕，且读不会改变任何用户可见状态。
    read_event = [
        e
        for e in _events_by_type(env, result.run_id, "tool.result")
        if e.payload.get("tool_id") == "memory.search"
    ][0]
    assert read_event.payload["effect_state"] == "none_applied"
    assert "任务没有绑定任何目录" not in json.dumps(output, ensure_ascii=False)

    # 装备：user + 本任务 task；没有 workspace（这个任务一个都没绑）。
    assert output["allowed"] == [
        {"scope": "user", "scope_id": "local"},
        {"scope": "task", "scope_id": task.task_id},
    ]
    # 实际进入视图的记忆身份必须落进 model.intent，供回放核对。
    memories = _intents(env, result.run_id)[-1].payload["context"]["memories"]
    assert [m["memory_id"] for m in memories] == [memory_id]
    assert memories[0]["revision"] == 2
    assert memories[0]["content_ref"] == item["content_ref"]


def test_agent_propose_then_confirm_through_dispatcher(env: Env):
    """写记忆走既有派发：propose 自动放行，confirm 等审批，批准后进入召回。"""
    task = env.task("记住我的写作偏好")  # 零目录任务同样能写记忆

    result, _, _ = env.run_to_end(
        task.task_id,
        [
            _propose("报告用中文，先列结论", source_refs=["event:7"]),
            _end(),
        ],
    )
    # propose 的 memory_id 在下一轮才会进上下文，所以先确认它落库，再用真实
    # id 走确认——这不是分两段凑数，而是模型真实的观察顺序。
    assert result.state == "succeeded", result.error
    proposed = env.stack.memory.list_memories(allowed=env.allowed(task))
    assert len(proposed) == 1
    memory = proposed[0]
    assert memory.status == "candidate"
    assert memory.revision == 1
    assert memory.origin == "inferred", "模型不能自称 user_explicit 绕过审批"

    # 目标只能是 memory:// 作用域：不许给记忆编一个假文件路径去骗过路径解析。
    requested = [
        e.payload
        for e in env.log.read_types(("dispatch.requested",))
        if (e.payload.get("resources") or [""])[0].startswith("memory://")
    ]
    assert requested, "记忆写入没有走既有派发路径"
    for payload in requested:
        assert all(r.startswith("memory://") for r in payload["resources"]), payload
        assert all(not r.startswith("/") for r in payload["resources"]), payload

    confirm_run, _, _ = env.run_to_end(
        task.task_id, [_confirm(memory.memory_id, memory.revision), _end()]
    )
    assert confirm_run.state == "waiting_approval", confirm_run.error
    _approve_all(env, confirm_run.approval_ids)
    done, _, _ = env.resume(confirm_run.run_id, [_end()])
    assert done.state == "succeeded", done.error

    active = env.stack.memory.get(memory.memory_id, allowed=env.allowed(task))
    assert active is not None
    assert active.status == "active"
    assert active.revision == 2

    recalled, _, _ = env.run_to_end(task.task_id, [_search(), _end()])
    output = _tool_output(env, recalled.run_id, "memory.search")
    assert [item["memory_id"] for item in output["items"]] == [memory.memory_id]


# ---------------------------------------------------------------- 2. 装备


def test_loadout_equips_only_authorized_scopes(env: Env, monkeypatch):
    """断言**实际传给 search 的 allowed 集合**，而不是"能查到"。"""
    ws_a = env.root / "ws_a"
    ws_b = env.root / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    env.seed(
        "报告用中文（A 的私有偏好）",
        scope=MemoryScope.workspace(ws_a),
        memory_id="mem_a",
    )

    projection = env.stack.memory
    real_retrieve = projection.retrieve
    seen: list[tuple[MemoryScope, ...]] = []

    def spy(query, *, allowed, limit=5, max_bytes=4096, priority=None):
        seen.append(tuple(allowed))
        return real_retrieve(
            query,
            allowed=allowed,
            limit=limit,
            max_bytes=max_bytes,
            priority=priority,
        )

    # 主循环走 `retrieve()`（要拿语义/降级标注），所以绊线钉在这一层：
    # 断言的仍是"实际传给检索的 allowed 集合"，不是"能查到"。
    monkeypatch.setattr(projection, "retrieve", spy)

    task_a = env.task("A 的任务", [ws_a])
    result_a, _, _ = env.run_to_end(task_a.task_id, [_search(), _end()])
    assert result_a.state == "succeeded", result_a.error

    expected_a = env.allowed(task_a)
    assert seen[0] == expected_a
    keys = {(scope.scope, scope.scope_id) for scope in seen[0]}
    assert ("user", "local") in keys
    assert ("task", task_a.task_id) in keys
    assert ("workspace", str(ws_a.resolve())) in keys
    assert ("workspace", str(ws_b.resolve())) not in keys, "没绑定的目录不该被装备"
    assert _tool_output(env, result_a.run_id, "memory.search")["count"] == 1

    # B 任务拥有另一个 workspace 作用域：A 的记忆一条都看不到。
    task_b = env.task("B 的任务", [ws_b])
    result_b, _, _ = env.run_to_end(task_b.task_id, [_search(), _end()])
    assert result_b.state == "succeeded", result_b.error
    assert seen[1] == env.allowed(task_b)
    keys_b = {(scope.scope, scope.scope_id) for scope in seen[1]}
    assert ("workspace", str(ws_b.resolve())) in keys_b
    assert ("workspace", str(ws_a.resolve())) not in keys_b
    assert _tool_output(env, result_b.run_id, "memory.search")["count"] == 0


def test_recall_is_capped_at_five_items_and_4kib(env: Env):
    """首版预算：最多 5 条 / 合计 4 KiB（F10 §8）。"""
    for index in range(6):
        env.seed(
            f"报告用中文 第{index}条",
            memory_id=f"mem_cap_{index}",
            kind="preference",
        )
    task = env.task("列出写作偏好")
    result, _, _ = env.run_to_end(task.task_id, [_search("报告 中文"), _end()])
    assert result.state == "succeeded", result.error

    output = _tool_output(env, result.run_id, "memory.search")
    assert output["limit"] == MAX_RECALL_ITEMS
    assert output["max_bytes"] == MAX_RECALL_BYTES
    assert len(output["items"]) <= MAX_RECALL_ITEMS
    total = sum(len(item["content"].encode("utf-8")) for item in output["items"])
    assert total <= MAX_RECALL_BYTES


# ---------------------------------------------------------------- 3. 上下文预算


def test_recall_text_goes_through_compile_context_budget(env: Env):
    """检索文本走 F5 的字节预算；超预算时是**明确**的预览/丢弃，不是静默塞入。"""
    huge = "报告用中文。" + ("报" * 4000)
    env.seed(huge, memory_id="mem_huge")
    task = env.task("读记忆并总结")

    pinned = len(canonical_bytes(initial_messages(task.goal, None)))
    # 只比不可让前缀多 1.2 KB：完整工具结果（≈4 KB 正文 + JSON）放不下，
    # 但确定性预览放得下——这样能精确验证"预算真的生效了"。
    limits = Limits(max_turns=4, context_budget_bytes=pinned + 1200)

    result, model, _ = env.run_to_end(
        task.task_id, [_search(), _end()], limits=limits
    )
    assert result.state == "succeeded", result.error

    second_view = model.seen[1]
    dumped = json.dumps(second_view, ensure_ascii=False)
    assert "报" * 200 not in dumped, "超预算的检索正文不该原样进入上下文"
    assert (
        "[tool-output-previewed" in dumped or "[context-compacted]" in dumped
    ), "压缩必须是明确占位/说明，不能静默消失"

    # 这一轮模型没有看到任何一条完整记忆，所以 intent 不能声称选用了记忆。
    context = _intents(env, result.run_id)[-1].payload["context"]
    assert context.get("memories") in (None, [])

    # 事件里的完整结果仍然在（审计与回放不跟着预算一起缩水）。
    output = _tool_output(env, result.run_id, "memory.search")
    assert output["items"][0]["content"]


def test_default_budget_keeps_recall_verbatim_and_pure(env: Env):
    """默认预算下 compile_context 仍是纯函数：同输入同 digest，记忆原文不落盘。"""
    env.seed("报告用中文", memory_id="mem_pure")
    task = env.task("读记忆")
    result, model, _ = env.run_to_end(task.task_id, [_search(), _end()])
    assert result.state == "succeeded", result.error

    intent = _intents(env, result.run_id)[-1]
    context = intent.payload["context"]
    budget = ContextBudget(
        max_bytes=context["max_bytes"],
        spill_threshold=context["spill_threshold"],
    )
    view = compile_context(
        model.seen[-1], context["source_seqs"], budget
    )
    assert view.digest == intent.payload["messages_hash"]
    assert view.spills == []
    assert "报告用中文" in json.dumps(model.seen[-1], ensure_ascii=False)


# ---------------------------------------------------------------- 4. 忘记


def test_agent_forget_then_new_task_cannot_recall(env: Env):
    """确认 → 检索命中 → 忘记（审批）→ 新任务检索不到。"""
    memory_id = env.seed("报告用中文", memory_id="mem_forget")
    task = env.task("写作偏好")

    # 先证明检索得到（否则"忘掉后查不到"是假的）。
    before, _, _ = env.run_to_end(task.task_id, [_search(), _end()])
    assert _tool_output(env, before.run_id, "memory.search")["count"] == 1

    forget_run, _, _ = env.run_to_end(
        task.task_id, [_forget(memory_id, 2), _end()]
    )
    assert forget_run.state == "waiting_approval", forget_run.error
    _approve_all(env, forget_run.approval_ids)
    done, _, _ = env.resume(forget_run.run_id, [_end()])
    assert done.state == "succeeded", done.error

    memory = env.stack.memory.get(memory_id, allowed=env.allowed(task))
    assert memory is not None
    assert memory.status == "forgotten"
    assert memory.revision == 3

    after, _, _ = env.run_to_end(task.task_id, [_search(), _end()])
    assert after.state == "succeeded", after.error
    assert _tool_output(env, after.run_id, "memory.search")["count"] == 0


# ---------------------------------------------------------------- 5. CLI


def test_cli_memory_list_search_get_confirm_forget(env: Env):
    """CLI 是控制面：人能看、能检索、能确认、能忘记；人是批准者本身。"""
    db = str(env.log.db_path)
    memory_id = env.seed(
        "报告用中文，先列结论",
        memory_id="mem_cli",
        status="candidate",
        source_refs=("event:42", "artifact:summary.md"),
    )

    code, out = _cli("memory", "list", "--db", db)
    assert code == 0, out
    for label in ("memory_id", "revision", "scope", "status", "source_refs"):
        assert label in out, f"CLI list 输出缺少 {label}：{out}"
    assert memory_id in out
    assert "candidate" in out
    assert "event:42" in out

    code, out = _cli("memory", "search", "报告 中文", "--db", db)
    assert code == 0, out
    # 候选未激活不召回（F10 §7）。
    assert "没有检索到记忆" in out

    code, out = _cli("memory", "confirm", memory_id, "--db", db)
    assert code == 0, out
    confirmed = env.stack.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert confirmed is not None
    assert confirmed.status == "active"
    assert confirmed.revision == 2

    code, out = _cli("memory", "search", "报告 中文", "--db", db)
    assert code == 0, out
    assert memory_id in out
    assert "active" in out

    code, out = _cli("memory", "get", memory_id, "--db", db)
    assert code == 0, out
    assert "报告用中文，先列结论" in out
    assert "revision" in out and "scope" in out and "status" in out

    code, out = _cli("memory", "forget", memory_id, "--db", db)
    assert code == 0, out
    forgotten = env.stack.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert forgotten is not None
    assert forgotten.status == "forgotten"
    assert forgotten.revision == 3

    # 人发起 ≠ 绕过审计：仍然留下 intent/result + 审批 + 一次性凭据。
    receipts = env.log.read_types(("receipt",))
    assert {"memory.confirm", "memory.forget"} <= {
        e.payload.get("tool_id") for e in receipts
    }
    types = [e.type for e in env.log.read_types(("approval.decided", "approval.consumed"))]
    assert "approval.decided" in types

    # 忘记之后新任务检索不到（不是"CLI 说忘了，主循环还能查到"）。
    task = env.task("新任务")
    result, _, _ = env.run_to_end(task.task_id, [_search(), _end()])
    assert result.state == "succeeded", result.error
    assert _tool_output(env, result.run_id, "memory.search")["count"] == 0


def test_cli_workspace_scope_needs_the_root_and_then_works(env: Env):
    """workspace 记忆的控制面操作：必须显式带上那个根，带上了就能确认。"""
    db = str(env.log.db_path)
    ws = env.root / "cli_ws"
    ws.mkdir()
    memory_id = env.seed(
        "报告用中文",
        scope=MemoryScope.workspace(ws),
        memory_id="mem_ws_cli",
        status="candidate",
    )

    # 不带 root：看不见 workspace 作用域的记忆（默认只装备 user）。
    code, out = _cli("memory", "confirm", memory_id, "--db", db)
    assert code == 1, out
    assert "不存在或不在当前作用域集合内" in out

    code, out = _cli(
        "memory", "confirm", memory_id, "--workspace", str(ws), "--db", db
    )
    assert code == 0, out
    memory = env.stack.memory.get(
        memory_id, allowed=(MemoryScope.workspace(ws),)
    )
    assert memory is not None
    assert memory.status == "active"
    assert memory.scope == "workspace"


# ---------------------------------------------------------------- 6. replay


def test_replay_uses_the_snapshot_not_the_latest_memory(env: Env):
    """忘记 + 删正文之后，旧 Run 的 replay 仍通过——它读事件里的快照。"""
    memory_id = env.seed("版本一：报告用中文", memory_id="mem_replay")
    task = env.task("读记忆")
    result, _, _ = env.run_to_end(task.task_id, [_search(), _end()])
    assert result.state == "succeeded", result.error

    recorded = _tool_output(env, result.run_id, "memory.search")
    assert recorded["items"][0]["content"] == "版本一：报告用中文"
    content_ref = recorded["items"][0]["content_ref"]

    report_before = replay(env.log, result.run_id)
    assert report_before.ok, report_before.problems

    # 让"最新版本"变掉：忘记旧记忆，再新增一条同主题但内容不同的活动记忆。
    forget = {
        "tool_id": "memory.forget",
        "status": "succeeded",
        "output": {
            "memory_id": memory_id,
            "revision": 3,
            "before_status": "active",
            "status": "forgotten",
            "memory_uri": MemoryScope.user().to_uri(),
        },
    }
    with env.log.transaction() as tx:
        seq = tx.append(env.seed_run, "tool.result", forget, legacy_write=True)
        env.stack.memory.apply_event("tool.result", forget, seq=seq)
    env.seed("版本二：报告用英文", memory_id="mem_replay_new")

    # 再删掉旧正文 blob：模拟"历史快照缺失"。
    (env.stack.memory.blob_dir / content_ref).unlink()

    # 离线 replay 不读记忆库：最新版本换了、旧 blob 没了，它仍然通过。
    report_after = replay(env.log, result.run_id)
    assert report_after.ok, report_after.problems
    assert report_after.messages_verified == report_before.messages_verified

    # 但真正去读旧记忆时，必须明确报"历史证据不可用"，不许回落读新版本。
    get_run, _, _ = env.run_to_end(task.task_id, [_get(memory_id), _end()])
    output = _tool_output(env, get_run.run_id, "memory.get")
    assert output["found"] is True
    assert output["content_available"] is False
    assert "历史证据不可用" in output["error"]
    assert "版本二" not in json.dumps(output, ensure_ascii=False)


def test_memory_search_schema_exposes_limit_capped_at_five() -> None:
    """F10 §8「模型可以要求更少、不能要求更多」必须在 schema 上真的存在。

    执行侧 `_memory_search_output` 早就会读并夹取 `limit`，但 schema 不暴露它，
    模型就**永远拿满 5 条**——契约写着的能力在工具面上不存在，等于没有。
    """
    from tianshu.memory import MAX_RECALL_ITEMS
    from tianshu.tools import SPECS

    limit = SPECS["memory.search"].input_schema["properties"]["limit"]
    assert limit["type"] == "integer"
    assert limit["minimum"] == 1
    assert limit["maximum"] == MAX_RECALL_ITEMS


def test_forgotten_memory_cannot_be_revived_by_id(env) -> None:
    """忘记之后，按 id 也读不到正文。

    `forget` 只改状态、不删正文 blob（物理擦除是 F10 §10 单开的工单），正文
    其实还在盘上。若 `memory.get` 照旧把它读出来，`get` 就是绕开 `forget`
    的旁路——"忘记"只对 search 生效。存在性仍如实报告（found=True），正文
    明确不可用，且不回落到最新版本。
    """
    memory_id = env.seed("忘记后不该还能按 id 读到正文")
    forget = {
        "tool_id": "memory.forget",
        "status": "succeeded",
        "output": {
            "memory_id": memory_id,
            # seed 之后这条记忆已经是 revision 2（propose=1 → confirm=2）。
            "revision": 3,
            "before_status": "active",
            "status": "forgotten",
            "memory_uri": MemoryScope.user().to_uri(),
        },
    }
    with env.log.transaction() as tx:
        seq = tx.append(env.seed_run, "tool.result", forget, legacy_write=True)
        env.stack.memory.apply_event("tool.result", forget, seq=seq)

    task = env.stack.tasks.create_task("忘记后读取", [])
    run, _, _ = env.run_to_end(task.task_id, [_get(memory_id), _end()])
    output = _tool_output(env, run.run_id, "memory.get")
    assert output["found"] is True
    assert output["content_available"] is False
    assert "已遗忘" in output["error"]
    assert "忘记后不该还能按 id 读到正文" not in json.dumps(output, ensure_ascii=False)

def test_cli_remember_creates_active_user_explicit_memory(env: Env):
    """`tianshu memory remember`：人直接记住 → active + user_explicit。"""
    db = str(env.log.db_path)
    code, out = _cli(
        "memory", "remember", "以后用中文回答", "--kind", "preference", "--db", db
    )
    assert code == 0, out
    assert "remember -> active" in out
    assert "origin: user_explicit" in out
    recalled = env.stack.memory.search("中文", allowed=(MemoryScope.user(),))
    assert [item.memory.origin for item in recalled] == ["user_explicit"]


def test_cli_can_forget_task_scope_memory_with_task_id(env: Env):
    """契约 v1.2：控制面能替旧 task 作用域的记忆签字，且不绑它的目录。"""
    db = str(env.log.db_path)
    scope = MemoryScope.task("task_cli_scope")
    memory_id = env.seed("任务内约定", scope=scope, status="active")
    code, out = _cli(
        "memory", "forget", memory_id,
        "--task-id", "task_cli_scope", "--db", db,
    )
    assert code == 0, out
    memory = env.stack.memory.get(memory_id, allowed=(scope,))
    assert memory is not None
    assert memory.status == "forgotten"


def test_cli_edit_keeps_old_row_and_activates_new(env: Env):
    """`tianshu memory edit`：人签字后旧版退役、新版立即生效。"""
    db = str(env.log.db_path)
    memory_id = env.seed("报告用中文", memory_id="mem_cli_edit", status="active")
    code, out = _cli(
        "memory", "edit", memory_id,
        "--content", "报告用英文", "--db", db,
    )
    assert code == 0, out
    assert "memory.edit -> succeeded" in out
    old = env.stack.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert old is not None and old.status == "superseded"
    actives = [
        m
        for m in env.stack.memory.list_memories(allowed=(MemoryScope.user(),), status="active")
        if m.supersedes == memory_id
    ]
    assert len(actives) == 1
    # 控制面 edit = 人自己写的新正文 → user_explicit（不是"降档"）
    assert actives[0].origin == "user_explicit"


def test_cli_promote_moves_memory_into_task_scope(env: Env):
    """`tianshu memory promote`：原行退役、目标作用域新行是候选。"""
    db = str(env.log.db_path)
    memory_id = env.seed("这条以后给项目用", memory_id="mem_cli_promote", status="active")
    code, out = _cli(
        "memory", "promote", memory_id,
        "--to-scope", "task", "--task-id", "task_cli_target",
        "--db", db,
    )
    assert code == 0, out
    assert "promote -> succeeded" in out
    target = MemoryScope.task("task_cli_target")
    old = env.stack.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert old is not None and old.status == "superseded"
    promoted = [
        m
        for m in env.stack.memory.list_memories(allowed=(target,), status="candidate")
        if m.supersedes == memory_id
    ]
    assert len(promoted) == 1
    assert memory_id in promoted[0].source_refs
    # 搬的是同一段正文：来源随行，不降档
    assert promoted[0].origin == "user_explicit"


def test_cli_erase_deletes_content_and_marks_erased(env: Env):
    """`tianshu memory erase`：人签字 → 正文副本删掉、行标 erased_at。"""
    db = str(env.log.db_path)
    memory_id = env.seed("这条要被擦掉", memory_id="mem_cli_erase", status="active")
    row = env.stack.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert row is not None
    blob = env.stack.memory.blob_path(row.content_ref)
    assert blob is not None and blob.is_file()

    code, out = _cli("memory", "erase", memory_id, "--db", db)
    assert code == 0, out
    assert "正文副本" in out
    erased = env.stack.memory.get(memory_id, allowed=(MemoryScope.user(),))
    assert erased is not None
    assert erased.erased_at is not None
    assert erased.status == "forgotten"
    assert env.stack.memory.read_content(erased) is None
    assert blob is not None and not blob.exists()


def test_cli_can_erase_forgotten_and_candidate_memories(env: Env):
    """擦除不要求 active：忘记后（forgotten）与候选（candidate）都能擦。"""
    db = str(env.log.db_path)
    forgotten_id = env.seed("先忘记再擦除", memory_id="mem_cli_forget_erase", status="active")
    code, out = _cli("memory", "forget", forgotten_id, "--db", db)
    assert code == 0, out
    row = env.stack.memory.get(forgotten_id, allowed=(MemoryScope.user(),))
    assert row is not None and row.status == "forgotten"
    code, out = _cli("memory", "erase", forgotten_id, "--db", db)
    assert code == 0, out
    erased = env.stack.memory.get(forgotten_id, allowed=(MemoryScope.user(),))
    assert erased is not None and erased.erased_at is not None

    candidate_id = env.seed(
        "还没确认的候选", memory_id="mem_cli_candidate_erase", status="candidate"
    )
    code, out = _cli("memory", "erase", candidate_id, "--db", db)
    assert code == 0, out
    erased_candidate = env.stack.memory.get(
        candidate_id, allowed=(MemoryScope.user(),)
    )
    assert erased_candidate is not None and erased_candidate.erased_at is not None


# ---------------------------------------------------------------- 复审：输出口径与接线


def test_search_tool_output_reports_capability_and_score_kind(env: Env):
    """`memory.search` 的 output 必须带 `semantic` / `degraded` / `fused`，
    条目带 `score_kind`：不给 `fused` 模型无法判断分数口径。"""
    env.seed("报告用中文", scope=MemoryScope.user(), memory_id="mem_out")
    result, _, _ = env.run_to_end(
        env.task("输出口径", []).task_id, [_search(), _end()]
    )
    assert result.state == "succeeded", result.error
    output = _tool_output(env, result.run_id, "memory.search")
    assert output["semantic"] is False
    assert output["degraded"] is not None
    assert output["fused"] is False
    assert output["items"]
    assert {item["score_kind"] for item in output["items"]} == {"origin"}


def test_build_stack_wires_the_embedding_provider_from_env(monkeypatch, tmp_path):
    """生产装配入口：`TIANSHU_EMBEDDING_PROVIDER=hash` 时 stack.memory 带索引。"""
    from tianshu.loop import build_stack

    monkeypatch.setenv("TIANSHU_EMBEDDING_PROVIDER", "hash")
    log = EventLog(tmp_path / "events.db")
    try:
        stack = build_stack(log)
        assert getattr(stack.memory, "index", None) is not None
        available, _reason = stack.memory.index.availability()
        assert available is False, "空索引不得报可用"
    finally:
        log.close()


def test_unknown_embedding_provider_is_rejected(monkeypatch, tmp_path):
    """未知 provider 名字直接报错，不静默当成"没有 provider"。"""
    from tianshu.errors import TianshuError
    from tianshu.loop import build_stack

    monkeypatch.setenv("TIANSHU_EMBEDDING_PROVIDER", "magic-embeddings")
    log = EventLog(tmp_path / "events.db")
    try:
        with pytest.raises(TianshuError):
            build_stack(log)
    finally:
        log.close()


def test_cli_search_json_shape_is_documented_and_stable(env: Env, capsys, tmp_path):
    """CLI `memory search --json` 的形状：`{items, semantic, degraded, fused}`。

    旧形状是裸数组；这是**有意的 breaking**（要携带能力标注），用测试钉住新
    形状，避免以后又偷偷变回数组。
    """
    import json as _json

    from tianshu.cli import main

    rc = main(
        [
            "memory",
            "search",
            "报告",
            "--json",
            "--db",
            str(env.root / "events.db"),
            "--data-root",
            str(env.root / "authority"),
        ]
    )
    assert rc in (0, 1)
    payload = _json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert set(payload) == {"items", "semantic", "degraded", "fused"}
    assert payload["semantic"] is False


def test_env_provider_to_built_index_is_an_end_to_end_path(monkeypatch, tmp_path):
    """生产路径全链路：配 provider → CLI `memory rebuild-vectors` → 语义可用。

    这是首轮复审指出的缺口：只装配 provider 不会构建索引，用户配了也永远
    FTS-only。构建入口就是 CLI 的这个动作。
    """
    from tianshu.cli import main

    monkeypatch.setenv("TIANSHU_EMBEDDING_PROVIDER", "hash")
    data_root = tmp_path / "authority"
    data_root.mkdir()
    db = tmp_path / "events.db"
    log = EventLog(db, blob_dir=tmp_path / "blobs")
    try:
        stack = build_stack(log, data_root=data_root)
        projection = stack.memory
        scope = MemoryScope.user()
        content_ref = projection.put_content("报告用中文，先列结论")
        propose = {
            "tool_id": "memory.propose",
            "status": "succeeded",
            "output": {
                "memory_id": "mem_cli",
                "revision": 1,
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "kind": "preference",
                "content_ref": content_ref,
                "source_refs": ["event:1"],
                "evidence": [{"source_memory_id": "s", "quote": "q"}],
                "proof_count": 1,
                "origin": "user_explicit",
                "status": "candidate",
                "created_at": 1,
                "expires_at": None,
                "deduped": False,
            },
        }
        with log.transaction() as tx:
            seq = tx.append("run_cli", "tool.result", propose, legacy_write=True)
            projection.apply_event("tool.result", propose, seq=seq)
        confirm = {
            "tool_id": "memory.confirm",
            "status": "succeeded",
            "output": {
                "memory_id": "mem_cli",
                "revision": 2,
                "before_status": "candidate",
                "status": "active",
                "consolidated_at": 2,
                "memory_uri": scope.to_uri(),
            },
        }
        with log.transaction() as tx:
            seq = tx.append("run_cli", "tool.result", confirm, legacy_write=True)
            projection.apply_event("tool.result", confirm, seq=seq)
        assert projection.retrieve("报告", allowed=(scope,)).semantic is False
        rc = main(["memory", "rebuild-vectors", "--db", str(db), "--data-root", str(data_root)])
        assert rc == 0
        available, reason = projection.index.availability()
        assert available is True, reason
        built = projection.retrieve("报告", allowed=(scope,))
        assert built.semantic is True
    finally:
        log.close()
