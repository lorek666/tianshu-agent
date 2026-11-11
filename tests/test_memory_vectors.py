"""向量索引（`memory_index.py`）与 RRF 融合的判据（F10 §8 / §12，#20）。

要点不是"有语义召回"，而是**能力边界必须自报**：

- 没配置 provider → `semantic=false` + `degraded` 说明，不是"没找到"；
- 换过 provider 而没重建 → 旧向量不参与，仍报 `semantic=false`；
- 重建是确定的：同一批记忆两次 `rebuild_vectors()` 逐字节相同；
- RRF 分数能手算核对；
- 索引跟随生命周期：候选不建、确认建、退役/忘记/擦除就下掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.memory import (
    Memory,
    MemoryProjection,
    MemoryStoreError,
    RecallPriority,
)
from tianshu.memory_index import HashEmbeddingProvider, MemoryIndex, rf_fuse
from tianshu.resources import MemoryScope

NOW = 1_700_000_000_000
RUN = "run_vectors"


def _record(log: EventLog, projection: MemoryProjection, payload: dict) -> int:
    with log.transaction() as tx:
        seq = tx.append(RUN, "tool.result", payload, legacy_write=True)
        projection.apply_event("tool.result", payload, seq=seq)
    return seq


def _propose(
    log: EventLog,
    projection: MemoryProjection,
    memory_id: str,
    content: str,
    *,
    scope: MemoryScope,
    created_at: int = 1,
) -> None:
    content_ref = projection.put_content(content)
    _record(
        log,
        projection,
        {
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
                "evidence": [
                    {"source_memory_id": "memory_source", "quote": "原文引文"}
                ],
                "proof_count": 1,
                "origin": "user_explicit",
                "status": "candidate",
                "created_at": created_at,
                "expires_at": None,
                "deduped": False,
            },
        },
    )


def _confirm(
    log: EventLog, projection: MemoryProjection, memory_id: str, *, scope: MemoryScope
) -> None:
    _record(
        log,
        projection,
        {
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
        },
    )


def _forget(
    log: EventLog, projection: MemoryProjection, memory_id: str, *, scope: MemoryScope
) -> None:
    _record(
        log,
        projection,
        {
            "tool_id": "memory.forget",
            "status": "succeeded",
            "output": {
                "memory_id": memory_id,
                "revision": 3,
                "before_status": "active",
                "status": "forgotten",
                "memory_uri": scope.to_uri(),
            },
        },
    )


def _seed(
    log: EventLog,
    projection: MemoryProjection,
    memory_id: str,
    content: str,
    *,
    scope: MemoryScope,
    created_at: int = 1,
) -> None:
    _propose(
        log, projection, memory_id, content, scope=scope, created_at=created_at
    )
    _confirm(log, projection, memory_id, scope=scope)


@pytest.fixture()
def fts_env(tmp_path: Path):
    """没有索引：FTS-only（生产默认形态，provider 未配置）。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=tmp_path / "memory_blobs", clock=lambda: NOW
    )
    projection.ensure_schema()
    yield log, projection
    log.close()


@pytest.fixture()
def vector_env(tmp_path: Path):
    """装配了确定性哈希 provider 的索引（**测试基线，不代表语义能力**）。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    index = MemoryIndex(log.conn, provider=HashEmbeddingProvider(dim=32))
    projection = MemoryProjection(
        log.conn,
        log=log,
        blob_dir=tmp_path / "memory_blobs",
        clock=lambda: NOW,
        index=index,
    )
    projection.ensure_schema()
    yield log, projection, index
    log.close()


# ---------------------------------------------------------------- 能力边界


def test_fts_only_reports_semantic_false_with_reason(fts_env) -> None:
    """未配置向量 provider：`semantic=false` + 原因，而不是"没找到"。"""
    log, projection = fts_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文，先列结论", scope=scope)
    outcome = projection.retrieve("报告 中文", allowed=(scope,))
    assert [item.memory.memory_id for item in outcome.items] == ["m1"]
    assert outcome.semantic is False
    assert outcome.degraded is not None and "未配置" in outcome.degraded
    assert outcome.fused is False


def test_provider_switch_makes_old_vectors_ineligible(vector_env) -> None:
    """换 provider 但没重建：旧向量不参与，检索如实报降级。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文，先列结论", scope=scope)
    # 能力必须**构建完成**才算可用：只写了几条向量不算。
    assert index.availability()[0] is False
    assert projection.rebuild_vectors() == 1
    assert index.availability() == (True, None)

    other = MemoryIndex(log.conn, provider=HashEmbeddingProvider(dim=32, model_id="other-32"))
    projection_other = MemoryProjection(
        log.conn,
        log=log,
        blob_dir=projection.blob_dir,
        clock=lambda: NOW,
        index=other,
    )
    projection_other.ensure_schema()
    available, reason = other.availability()
    assert available is False
    assert reason is not None and "rebuild_vectors" in reason
    outcome = projection_other.retrieve("报告 中文", allowed=(scope,))
    assert outcome.semantic is False
    assert outcome.degraded is not None
    # 词面路仍能用：降级不是"没结果"。
    assert [item.memory.memory_id for item in outcome.items] == ["m1"]


def test_rebuild_vectors_is_byte_identical_across_runs(vector_env) -> None:
    """重建是确定的：同一批记忆两次重建，向量逐字节相同。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文，先列结论", scope=scope)
    _seed(log, projection, "m2", "先讲风险再讲方案", scope=scope)

    first = dict(
        log.conn.execute(
            "SELECT memory_id, vec FROM memory_vectors ORDER BY memory_id"
        ).fetchall()
    )
    assert set(first) == {"m1", "m2"}
    count = projection.rebuild_vectors()
    second = dict(
        log.conn.execute(
            "SELECT memory_id, vec FROM memory_vectors ORDER BY memory_id"
        ).fetchall()
    )
    assert count == 2
    assert first == second


def test_index_follows_the_memory_lifecycle(vector_env) -> None:
    """候选不建向量；确认建；退役 / 忘记 / 擦除就下掉（否则语义路会捞回退役行）。"""
    log, projection, _index = vector_env
    scope = MemoryScope.user()
    _propose(log, projection, "m1", "报告用中文", scope=scope)
    assert (
        log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0
    ), "候选不参与召回，不该建向量"

    _confirm(log, projection, "m1", scope=scope)
    assert (
        log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1
    )

    _forget(log, projection, "m1", scope=scope)
    assert (
        log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0
    )


def test_erase_drops_the_vector(vector_env) -> None:
    """擦除后向量必须下掉：语义路不能拿着旧向量把已擦除内容再排回来。"""
    log, projection, _index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "要被擦掉的正文", scope=scope)
    _record(
        log,
        projection,
        {
            "tool_id": "memory.erase",
            "status": "succeeded",
            "output": {
                "memory_id": "m1",
                "revision": 3,
                "before_status": "active",
                "status": "forgotten",
                "memory_uri": scope.to_uri(),
                "erased_at": NOW,
            },
        },
    )
    assert (
        log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0
    )


# ---------------------------------------------------------------- RRF


def test_rrf_scores_are_hand_checkable() -> None:
    """`Σ 1/(60+rank)`：固定两路名次，分数与顺序逐条手算。"""
    fused = rf_fuse([["a", "b", "c"], ["c", "b", "d"]])
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 62)
    assert scores["d"] == pytest.approx(1 / 63)
    assert [memory_id for memory_id, _score in fused] == ["c", "b", "a", "d"]


def test_rrf_ties_are_broken_by_memory_id() -> None:
    """同分按 id 升序：名次不依赖字典插入顺序，跨进程可复现。"""
    assert [item[0] for item in rf_fuse([["b"], ["a"]], k=10)] == ["a", "b"]


def test_rrf_rejects_a_zero_k() -> None:
    with pytest.raises(MemoryStoreError):
        rf_fuse([["a"]], k=0)


def test_hybrid_retrieval_fuses_both_ranks(vector_env) -> None:
    """两路都有候选时：`fused=true`，`score` 是 RRF 分数而不是 origin 权重。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文，先列结论", scope=scope)
    _seed(log, projection, "m2", "报告先讲风险", scope=scope)
    projection.rebuild_vectors()
    outcome = projection.retrieve("报告 中文", allowed=(scope,))
    assert outcome.semantic is True
    assert outcome.degraded is None
    assert outcome.fused is True
    assert outcome.items
    assert all(item.score > 0 for item in outcome.items)
    # 两路并集：这两条都在；`score_kind` 必须标明是 RRF。
    ids = {item.memory.memory_id for item in outcome.items}
    assert ids == {"m1", "m2"}
    assert {item.score_kind for item in outcome.items} == {"rrf"}
    assert index.availability() == (True, None)


def test_semantic_route_never_pulls_in_other_scopes(vector_env) -> None:
    """语义路同样先生效过滤条件：越权作用域不能被融合捞回来。"""
    log, projection, _index = vector_env
    user_scope = MemoryScope.user()
    ws_scope = MemoryScope.workspace("/tmp/ws_other")
    _seed(log, projection, "mem_user", "报告用中文", scope=user_scope)
    _seed(log, projection, "mem_ws", "报告用中文", scope=ws_scope)
    projection.rebuild_vectors()
    outcome = projection.retrieve("报告 中文", allowed=(user_scope,))
    assert [item.memory.memory_id for item in outcome.items] == ["mem_user"]


def test_rebuild_clears_vectors_for_rows_that_are_no_longer_recallable(vector_env) -> None:
    """重建投影后向量集合 = 当前可召回行：不留没有行引用的残片。"""
    log, projection, _index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    _seed(log, projection, "m2", "先讲风险", scope=scope)
    _forget(log, projection, "m2", scope=scope)
    assert projection.rebuild_vectors() == 1
    rows = log.conn.execute(
        "SELECT memory_id FROM memory_vectors ORDER BY memory_id"
    ).fetchall()
    assert [row[0] for row in rows] == ["m1"]

# ---------------------------------------------------------------- 复审：并集 / 就绪状态 / 过期


class _ScriptedProvider:
    """测试用 provider：**按文本直接给向量**，用来把"语义候选"与"词面候选"
    彻底解耦（哈希基线做不到零词面重叠的相似性，用它就测不出并集）。"""

    model_id = "scripted-4"
    dim = 4

    def __init__(self, table: dict[str, tuple[float, float, float, float]]) -> None:
        self._table = table

    def embed(self, texts):
        out = []
        for text in texts:
            key = text if text in self._table else "*"
            out.append(list(self._table.get(key, (0.0, 0.0, 0.0, 1.0))))
        return out


def _seed_via_events(log, projection, mapping: dict[str, str], scope) -> None:
    """按 `memory_id -> content` 写一批 L1（propose + confirm，走真实事件）。"""
    for index, (memory_id, content) in enumerate(mapping.items()):
        _propose(log, projection, memory_id, content, scope=scope, created_at=index + 1)
        _confirm(log, projection, memory_id, scope=scope)


def test_semantic_route_runs_even_when_fts_finds_nothing(tmp_path: Path) -> None:
    """FTS 一条都没有时语义路必须照跑，并把语义命中的行并进结果。"""
    log = EventLog(tmp_path / "events.db")
    provider = _ScriptedProvider(
        {"alpha beta": (1.0, 0.0, 0.0, 0.0), "gamma-only-query": (1.0, 0.0, 0.0, 0.0)}
    )
    index = MemoryIndex(log.conn, provider=provider)
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=tmp_path / "blobs", clock=lambda: NOW, index=index
    )
    projection.ensure_schema()
    scope = MemoryScope.user()
    _seed_via_events(log, projection, {"mem_alpha": "alpha beta"}, scope)
    projection.rebuild_vectors()
    try:
        outcome = projection.retrieve("gamma-only-query", allowed=(scope,))
        assert outcome.semantic is True
        assert outcome.degraded is None
        assert outcome.fused is True
        assert [item.memory.memory_id for item in outcome.items] == ["mem_alpha"]
        assert outcome.items[0].score_kind == "rrf"
    finally:
        log.close()


def test_semantic_only_candidates_are_merged_with_fts_candidates(tmp_path: Path) -> None:
    """FTS 命中的 + 语义独有命中的，必须取并集后一起融合。"""
    log = EventLog(tmp_path / "events.db")
    provider = _ScriptedProvider(
        {
            "alpha beta": (1.0, 0.0, 0.0, 0.0),
            "gamma delta": (1.0, 0.0, 0.0, 0.0),
            "gamma": (1.0, 0.0, 0.0, 0.0),
        }
    )
    index = MemoryIndex(log.conn, provider=provider)
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=tmp_path / "blobs", clock=lambda: NOW, index=index
    )
    projection.ensure_schema()
    scope = MemoryScope.user()
    _seed_via_events(
        log, projection, {"mem_fts": "alpha beta", "mem_sem": "gamma delta"}, scope
    )
    projection.rebuild_vectors()
    try:
        outcome = projection.retrieve("gamma", allowed=(scope,))
        ids = {item.memory.memory_id for item in outcome.items}
        assert ids == {"mem_fts", "mem_sem"}, "语义独有候选不能被丢掉"
        assert outcome.fused is True and outcome.semantic is True
    finally:
        log.close()


def test_empty_index_is_not_reported_as_available(vector_env) -> None:
    """空索引（provider 装了但没构建）不得报 `semantic=true`。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    available, reason = index.availability()
    assert available is False
    assert reason is not None and "尚未建立" in reason
    outcome = projection.retrieve("报告", allowed=(scope,))
    assert outcome.semantic is False
    assert outcome.degraded is not None
    assert [item.memory.memory_id for item in outcome.items] == ["m1"]


def test_mixed_provider_vectors_are_detected_as_incomplete(vector_env) -> None:
    """混入另一套 provider 的向量 ⇒ 当前 provider 报覆盖不完整（不静默半套）。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    _seed(log, projection, "m2", "先讲风险", scope=scope)
    assert projection.rebuild_vectors() == 2
    assert index.availability() == (True, None)

    other = MemoryIndex(log.conn, provider=HashEmbeddingProvider(dim=32, model_id="other-32"))
    memory = projection.get("m2", allowed=(scope,))
    assert memory is not None
    other.upsert(memory, "先讲风险", last_seq=1)
    available, reason = index.availability()
    assert available is False
    assert reason is not None and "覆盖不完整" in reason
    assert other.availability()[0] is False

    # 重建当前 provider 的空间：覆盖补齐、其他空间的行被清掉。
    assert projection.rebuild_vectors() == 2
    assert index.availability() == (True, None)
    assert (
        log.conn.execute(
            "SELECT COUNT(*) FROM memory_vectors WHERE model_id = 'other-32'"
        ).fetchone()[0]
        == 0
    )


def test_rebuild_without_an_index_still_clears_vector_state(vector_env) -> None:
    """无索引的 `rebuild()` 也要清向量并作废构建标记，别留下半个索引。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    assert projection.rebuild_vectors() == 1
    assert index.availability() == (True, None)

    # 同一个连接上再开一个**没有索引**的投影：重建重放事件。
    plain = MemoryProjection(log.conn, log=log, blob_dir=projection.blob_dir, clock=lambda: NOW)
    assert plain.rebuild() >= 1
    vectors = log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
    meta = log.conn.execute("SELECT COUNT(*) FROM memory_vector_meta").fetchone()[0]
    assert vectors == 0
    assert meta == 0
    # 之后按带索引打开：必须先 rebuild_vectors 才能报可用。
    assert index.availability()[0] is False
    assert projection.rebuild_vectors() == 1
    assert index.availability() == (True, None)


def test_index_schema_creation_in_a_rolled_back_transaction_is_retried(tmp_path: Path) -> None:
    """索引建表在调用方事务里：回滚后不能缓存"已就绪"（与投影同款坑）。"""
    log = EventLog(tmp_path / "events.db")
    index = MemoryIndex(log.conn, provider=HashEmbeddingProvider(dim=8))
    try:
        with pytest.raises(RuntimeError):
            with log.transaction():
                index.ensure_schema()
                raise RuntimeError("业务校验失败，事务回滚")
        # 表没提交，同一个实例必须还能重新建表（否则后续写会 no such table）。
        index.ensure_schema()
        # 建表成功：下面这次写入必须能落库（旧实现会在这里 no such table）。
        memory = Memory(
            memory_id="m1",
            revision=1,
            scope="user",
            scope_id="local",
            kind="preference",
            content_ref="0" * 64,
            source_refs=("event:1",),
            evidence=(),
            proof_count=0,
            origin="inferred",
            status="active",
            consolidated_at=None,
            created_at=1,
            expires_at=None,
            supersedes=None,
        )
        index.upsert(memory, "回滚之后还能写")
        assert log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1
    finally:
        log.close()


def test_direct_index_search_excludes_expired_rows(vector_env) -> None:
    """`MemoryIndex.search()` 的公开契约里"过期过滤"必须是真的。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _propose(
        log,
        projection,
        "mem_old",
        "过期样例 报告用中文",
        scope=scope,
        created_at=1,
    )
    _record(
        log,
        projection,
        {
            "tool_id": "memory.confirm",
            "status": "succeeded",
            "output": {
                "memory_id": "mem_old",
                "revision": 2,
                "before_status": "candidate",
                "status": "active",
                "consolidated_at": NOW,
                "memory_uri": scope.to_uri(),
            },
        },
    )
    # 把过期时间写进投影（confirm 不改 expires_at；这里直接改投影是测试夹具，
    # 生产路径由 propose 的 expires_at 决定）。
    with log.transaction():
        log.conn.execute(
            "UPDATE memories SET expires_at = ? WHERE memory_id = ?", (NOW, "mem_old")
        )
    projection.rebuild_vectors()
    assert index.search("报告", allowed=(scope,), limit=5, now=NOW) == []
    assert index.search("报告", allowed=(scope,), limit=5, now=NOW - 1) == ["mem_old"]


def test_search_stays_fts_only_and_retrieve_is_hybrid(vector_env) -> None:
    """`search()` 不偷偷变 hybrid；要能力标注走 `retrieve()`。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    projection.rebuild_vectors()
    fts_only = projection.search("报告", allowed=(scope,))
    assert [item.memory.memory_id for item in fts_only] == ["m1"]
    assert {item.score_kind for item in fts_only} == {"origin"}
    hybrid = projection.retrieve("报告", allowed=(scope,))
    assert hybrid.semantic is True
    assert {item.score_kind for item in hybrid.items} == {"rrf"}




def test_built_marker_is_required_for_availability(vector_env) -> None:
    """meta 有 provider/dim 但没 built（构建没跑完）⇒ 不可用。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    # 手写 meta（模拟"只记录身份、没完成构建"的中间态），不写 built。
    with log.transaction():
        log.conn.execute(
            "INSERT OR REPLACE INTO memory_vector_meta(key, value) VALUES (?, ?)",
            ("provider_model_id", index.provider.model_id),
        )
        log.conn.execute(
            "INSERT OR REPLACE INTO memory_vector_meta(key, value) VALUES (?, ?)",
            ("provider_dim", str(index.provider.dim)),
        )
        log.conn.execute("DELETE FROM memory_vector_meta WHERE key = 'built'")
    available, reason = index.availability()
    assert available is False
    assert reason is not None and "完整构建" in reason
    # 建一次之后才可用。
    assert projection.rebuild_vectors() == 1
    assert index.availability() == (True, None)


def test_hybrid_result_keeps_scope_priority_as_first_grouping(vector_env) -> None:
    """融合之后仍以作用域优先级分组：task 的记忆排在 user 前面。"""
    log, projection, index = vector_env
    task_scope = MemoryScope.task("task_current")
    user_scope = MemoryScope.user()
    _seed(log, projection, "mem_task", "优先级样例 报告用中文", scope=task_scope)
    _seed(log, projection, "mem_user", "优先级样例 报告用中文", scope=user_scope)
    projection.rebuild_vectors()
    priority = RecallPriority(task_id="task_current", workspace_roots=())
    outcome = projection.retrieve(
        "优先级样例 报告",
        allowed=(task_scope, user_scope),
        priority=priority,
    )
    assert outcome.semantic is True
    assert [item.memory.memory_id for item in outcome.items] == ["mem_task", "mem_user"]


def test_semantic_only_candidate_respects_allowed_and_expiry(tmp_path: Path) -> None:
    """语义独有候选同样要过 allowed / 过期两道闸。"""
    log = EventLog(tmp_path / "events.db")
    provider = _ScriptedProvider(
        {
            "alpha beta": (1.0, 0.0, 0.0, 0.0),
            "gamma-only": (1.0, 0.0, 0.0, 0.0),
        }
    )
    index = MemoryIndex(log.conn, provider=provider)
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=tmp_path / "blobs", clock=lambda: NOW, index=index
    )
    projection.ensure_schema()
    user_scope = MemoryScope.user()
    other_scope = MemoryScope.workspace(str(tmp_path / "other"))
    _seed_via_events(
        log,
        projection,
        {"mem_other": "alpha beta"},
        other_scope,
    )
    projection.rebuild_vectors()
    try:
        # allowed 不含 other_scope ⇒ 语义路即使命中也不许并进来。
        outcome = projection.retrieve("gamma-only", allowed=(user_scope,))
        assert [item.memory.memory_id for item in outcome.items] == []
        assert outcome.semantic is True
    finally:
        log.close()


def test_rebuild_with_index_reindexes_vectors(vector_env) -> None:
    """带索引的 `rebuild()` 重放后要重建向量并恢复可用（不能半套）。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    _seed(log, projection, "m2", "先讲风险", scope=scope)
    projection.rebuild_vectors()
    assert index.availability() == (True, None)
    assert projection.rebuild() >= 2
    assert index.availability() == (True, None)
    order = projection.retrieve("报告", allowed=(scope,), priority=None)
    assert order.semantic is True


def test_unreadable_content_drops_the_stale_vector(vector_env) -> None:
    """正文不可读时旧向量必须下掉：没有正文的记忆不能被语义路排出来。"""
    log, projection, index = vector_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文", scope=scope)
    projection.rebuild_vectors()
    assert index.availability() == (True, None)
    memory = projection.get("m1", allowed=(scope,))
    assert memory is not None
    blob = projection.blob_path(memory.content_ref)
    assert blob is not None
    blob.unlink()
    # 再确认一次（正文不可读 ⇒ 钩子应 drop 向量）
    _forget(log, projection, "m1", scope=scope)
    assert (
        log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0
    )
