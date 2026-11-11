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
from tianshu.memory import MemoryProjection, MemoryStoreError
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
    outcome = projection.retrieve("报告 中文", allowed=(scope,))
    assert outcome.semantic is True
    assert outcome.degraded is None
    assert outcome.fused is True
    assert outcome.items
    assert all(item.score > 0 for item in outcome.items)
    # 词面路第一与语义路第一都在候选里；融合后仍是这两条。
    ids = {item.memory.memory_id for item in outcome.items}
    assert ids <= {"m1", "m2"}
    assert index.availability() == (True, None)


def test_semantic_route_never_pulls_in_other_scopes(vector_env) -> None:
    """语义路同样先生效过滤条件：越权作用域不能被融合捞回来。"""
    log, projection, _index = vector_env
    user_scope = MemoryScope.user()
    ws_scope = MemoryScope.workspace("/tmp/ws_other")
    _seed(log, projection, "mem_user", "报告用中文", scope=user_scope)
    _seed(log, projection, "mem_ws", "报告用中文", scope=ws_scope)
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