"""F10 §8 的检索顺序：**作用域优先级**参与排序（#23）。

旧口径把排序钉死为 `origin → created_at`，作用域只用于过滤，理由是"让可见的
作用域互相压序会让同一权重内的顺序依赖调用方传了哪些 scope，不可复现"。
现在的处理把这两件事分开：

- `allowed` 仍然只决定**看不看得见**；
- 优先级只由"这条记忆自己的 scope / scope_id"与"当前 Run 的 task / workspace"
  决定（`RecallPriority`），因此同一份库内容 + 同一组 `allowed` + 同一个上下文
  必然排出逐位相同的名次。

这些用例都走真实投影与真实 FTS，不 mock 排序。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.memory import (
    MAX_RECALL_BYTES,
    MAX_RECALL_ITEMS,
    MemoryProjection,
    RecallPriority,
)
from tianshu.resources import MemoryScope

NOW = 1_700_000_000_000
RUN = "run_rank"


@pytest.fixture()
def env(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=tmp_path / "memory_blobs", clock=lambda: NOW
    )
    projection.ensure_schema()
    yield log, projection
    log.close()


def _record(log: EventLog, projection: MemoryProjection, payload: dict) -> int:
    with log.transaction() as tx:
        seq = tx.append(RUN, "tool.result", payload, legacy_write=True)
        projection.apply_event("tool.result", payload, seq=seq)
    return seq


def _seed(
    log: EventLog,
    projection: MemoryProjection,
    memory_id: str,
    content: str,
    *,
    scope: MemoryScope,
    created_at: int,
    origin: str = "user_explicit",
    expires_at: int | None = None,
) -> None:
    """propose + confirm：写入一条**可召回**的记忆（走真实事件路径）。"""
    content_ref = projection.put_content(content)
    propose = {
        "tool_id": "memory.propose",
        "status": "succeeded",
        "output": {
            "memory_id": memory_id,
            "revision": 1,
            "scope": scope.scope,
            "scope_id": scope.scope_id,
            "kind": "preference",
            "content_ref": content_ref,
            "source_refs": ["event:1"],
            "evidence": [{"source_memory_id": "memory_source", "quote": "原文引文"}],
            "proof_count": 1,
            "origin": origin,
            "status": "candidate",
            "created_at": created_at,
            "expires_at": expires_at,
            "deduped": False,
        },
    }
    _record(log, projection, propose)
    confirm = {
        "tool_id": "memory.confirm",
        "status": "succeeded",
        "output": {
            "memory_id": memory_id,
            "revision": 2,
            "before_status": "candidate",
            "status": "active",
            "consolidated_at": NOW,
            "memory_uri": scope.to_uri(),
        },
    }
    _record(log, projection, confirm)


def _ids(items) -> list[str]:
    return [item.memory.memory_id for item in items]


def test_scope_priority_orders_task_then_workspace_then_user(env) -> None:
    """同一 query 下：当前任务 > 本 Run 的 workspace > user（F10 §8 原话）。"""
    log, projection = env
    task_scope = MemoryScope.task("task_current")
    ws_scope = MemoryScope.workspace("/tmp/ws_current")
    user_scope = MemoryScope.user()
    # created_at 反向：让 user 那条第最新，证明"新"不能压过"近"。
    _seed(log, projection, "mem_user", "排序样例 用户的偏好", scope=user_scope, created_at=30)
    _seed(log, projection, "mem_ws", "排序样例 工作区的偏好", scope=ws_scope, created_at=20)
    _seed(log, projection, "mem_task", "排序样例 本任务的偏好", scope=task_scope, created_at=10)

    priority = RecallPriority(
        task_id="task_current", workspace_roots=(ws_scope.scope_id,)
    )
    hits = projection.search(
        "排序样例",
        allowed=(user_scope, ws_scope, task_scope),
        priority=priority,
    )
    assert _ids(hits) == ["mem_task", "mem_ws", "mem_user"]


def test_without_priority_the_old_order_is_kept(env) -> None:
    """不传上下文时退化成旧的 `origin → created_at`（向后兼容，不是"又一套顺序"）。"""
    log, projection = env
    scope = MemoryScope.user()
    _seed(
        log, projection, "mem_inferred", "排序样例 推测的",
        scope=scope, created_at=20, origin="inferred",
    )
    _seed(
        log, projection, "mem_confirmed", "排序样例 人确认过的",
        scope=scope, created_at=10, origin="user_confirmed",
    )
    _seed(
        log, projection, "mem_explicit", "排序样例 人明说的",
        scope=scope, created_at=1, origin="user_explicit",
    )
    hits = projection.search("排序样例", allowed=(scope,))
    assert _ids(hits) == ["mem_explicit", "mem_confirmed", "mem_inferred"]


def test_priority_is_reproducible_for_the_same_context(env) -> None:
    """同一份库内容 + 同一组 allowed + 同一上下文 ⇒ 逐位相同的名次。"""
    log, projection = env
    task_scope = MemoryScope.task("task_current")
    user_scope = MemoryScope.user()
    for index in range(4):
        _seed(
            log,
            projection,
            f"mem_{index}",
            f"排序样例 第{index}条",
            scope=user_scope,
            created_at=index,
        )
    _seed(log, projection, "mem_task", "排序样例 任务里的", scope=task_scope, created_at=99)
    priority = RecallPriority(task_id="task_current", workspace_roots=())
    first = _ids(
        projection.search(
            "排序样例", allowed=(user_scope, task_scope), priority=priority
        )
    )
    second = _ids(
        projection.search(
            "排序样例", allowed=(user_scope, task_scope), priority=priority
        )
    )
    assert first == second
    assert first[0] == "mem_task"


def test_priority_never_pulls_in_a_scope_that_is_not_allowed(env) -> None:
    """优先级只排序**已过滤**的候选：不许把越权作用域捞进来（过滤在排序之前）。"""
    log, projection = env
    task_scope = MemoryScope.task("task_current")
    user_scope = MemoryScope.user()
    _seed(log, projection, "mem_task", "排序样例 任务里的", scope=task_scope, created_at=2)
    _seed(log, projection, "mem_user", "排序样例 用户的", scope=user_scope, created_at=1)
    priority = RecallPriority(task_id="task_current", workspace_roots=())
    hits = projection.search("排序样例", allowed=(user_scope,), priority=priority)
    assert _ids(hits) == ["mem_user"]


def test_expired_memory_is_excluded_even_at_the_highest_priority(env) -> None:
    """过期项不参与：优先级不能把"已经不算数"的证据排到前面。"""
    log, projection = env
    task_scope = MemoryScope.task("task_current")
    user_scope = MemoryScope.user()
    _seed(
        log,
        projection,
        "mem_expired",
        "过期样例 任务里的",
        scope=task_scope,
        created_at=1,
        expires_at=NOW,
    )
    _seed(log, projection, "mem_user", "过期样例 用户的", scope=user_scope, created_at=2)
    priority = RecallPriority(task_id="task_current", workspace_roots=())
    hits = projection.search(
        "过期样例", allowed=(user_scope, task_scope), priority=priority
    )
    assert _ids(hits) == ["mem_user"]


def test_byte_budget_crop_still_happens_after_priority(env) -> None:
    """裁剪仍在排序之后：4 KiB 预算不能因为"优先级更高"就超。"""
    log, projection = env
    task_scope = MemoryScope.task("task_current")
    user_scope = MemoryScope.user()
    long_text = "预算样例 " + ("正文" * 3000)
    _seed(log, projection, "mem_big", long_text, scope=task_scope, created_at=2)
    _seed(log, projection, "mem_small", "预算样例 短的", scope=user_scope, created_at=1)
    priority = RecallPriority(task_id="task_current", workspace_roots=())
    hits = projection.search(
        "预算样例",
        allowed=(user_scope, task_scope),
        priority=priority,
        max_bytes=MAX_RECALL_BYTES,
        limit=MAX_RECALL_ITEMS,
    )
    assert sum(len(hit.content.encode("utf-8")) for hit in hits) <= MAX_RECALL_BYTES
    # 第一条被截断后不再塞第二条（避免"截一半还继续"的假队满）。
    assert len(hits) == 1

def test_priority_without_a_current_task_does_not_promote_task_scope(env) -> None:
    """`RecallPriority(task_id=None)`：缺上下文时 task 作用域不排在 user 前面。"""
    log, projection = env
    task_scope = MemoryScope.task("task_current")
    user_scope = MemoryScope.user()
    _seed(log, projection, "mem_task", "上下文样例 任务里的", scope=task_scope, created_at=2)
    _seed(log, projection, "mem_user", "上下文样例 用户的", scope=user_scope, created_at=1)
    priority = RecallPriority(task_id=None, workspace_roots=())
    hits = projection.search(
        "上下文样例", allowed=(user_scope, task_scope), priority=priority
    )
    assert [item.memory.memory_id for item in hits] == ["mem_user", "mem_task"]
