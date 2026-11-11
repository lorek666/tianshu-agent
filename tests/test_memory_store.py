"""F10 记忆投影验收（`src/tianshu/memory.py`）。

这些用例刻意只走真实路径：真 `EventLog`、真 sqlite（含 FTS5）、真内容寻址
目录，没有 mock。每个断言都对着 F10 §14 的出口判据：

- 记住「报告用中文、先列结论」后能检索到（中文双字切分，量化到 token）；
- 不同 workspace 不串记忆；
- 候选未确认不召回；
- 忘记后新请求、**重建索引**都不复活；
- 删除索引可重建，正文与来源仍在；
- PII / 密钥写入闸门（阻断的候选不入库）；
- 写入前去重（第二次不增长 FTS 行）；
- 4 KiB 裁剪（不是"超了就返回空"）。

陷阱提醒（写给自己）：最容易写成"看起来对了"的两条是：

1. forget 只删 FTS 行 —— 那在 rebuild 时必然复活。这里显式 `rebuild()` 后再查；
2. 字节裁剪只断言条数 —— 这里直接断言 `sum(len(content.encode())) <= 4096`，
   并且先放一条 8KB 的，证明"超了截断，不是返回空"。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tianshu.errors import TianshuError
from tianshu.events import EventLog, sha256_hex
from tianshu.memory import (
    MAX_RECALL_BYTES,
    MAX_RECALL_ITEMS,
    MEMORY_KINDS,
    MEMORY_ORIGINS,
    MEMORY_STATUSES,
    TOKENIZER_VERSION,
    Memory,
    MemoryProjection,
    MemoryStoreError,
    MemoryWriteBlocked,
    Recalled,
)
from tianshu.resources import MemoryScope

NOW = 1_700_000_000_000
RUN = "run_memory"


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def env(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    projection = MemoryProjection(
        log.conn,
        log=log,
        blob_dir=tmp_path / "memory_blobs",
        clock=lambda: NOW,
    )
    projection.ensure_schema()
    yield log, projection
    log.close()


def _record(log: EventLog, projection: MemoryProjection, payload: dict) -> int:
    """模拟 dispatcher：事件与投影在同一事务里提交。"""
    with log.transaction() as tx:
        seq = tx.append(RUN, "tool.result", payload)
        projection.apply_event("tool.result", payload, seq=seq)
    return seq


def _propose_payload(
    projection: MemoryProjection,
    memory_id: str,
    content: str,
    *,
    scope: MemoryScope | None = None,
    kind: str = "preference",
    origin: str = "user_explicit",
    created_at: int = 1,
    expires_at: int | None = None,
    source_refs: tuple[str, ...] = ("event:1",),
    evidence: tuple[dict, ...] | None = None,
    deduped: bool = False,
    content_ref: str | None = None,
    **overrides: object,
) -> dict:
    scope = MemoryScope.user() if scope is None else scope
    if evidence is None:
        evidence = ({"source_memory_id": "memory_source", "quote": "原文引文"},)
    if content_ref is None:
        content_ref = projection.put_content(content)
    output: dict[str, object] = {
        "memory_id": memory_id,
        "revision": 1,
        "scope": scope.scope,
        "scope_id": scope.scope_id,
        "kind": kind,
        "content_ref": content_ref,
        "source_refs": list(source_refs),
        "evidence": [dict(item) for item in evidence],
        "proof_count": len(evidence),
        "origin": origin,
        "status": "candidate",
        "created_at": created_at,
        "expires_at": expires_at,
        "deduped": deduped,
    }
    output.update(overrides)
    return {"tool_id": "memory.propose", "status": "succeeded", "output": output}


def _propose(
    projection: MemoryProjection,
    log: EventLog,
    memory_id: str,
    content: str,
    **kwargs: object,
) -> int:
    return _record(log, projection, _propose_payload(projection, memory_id, content, **kwargs))


def _confirm(
    projection: MemoryProjection,
    log: EventLog,
    memory_id: str,
    *,
    scope: MemoryScope | None = None,
    revision: int = 2,
    before_status: str = "candidate",
    consolidated_at: int | None = NOW,
) -> int:
    scope = MemoryScope.user() if scope is None else scope
    output = {
        "memory_id": memory_id,
        "revision": revision,
        "before_status": before_status,
        "status": "active",
        "consolidated_at": consolidated_at,
        "memory_uri": scope.to_uri(),
    }
    return _record(
        log,
        projection,
        {"tool_id": "memory.confirm", "status": "succeeded", "output": output},
    )


def _forget(
    projection: MemoryProjection,
    log: EventLog,
    memory_id: str,
    *,
    scope: MemoryScope | None = None,
    revision: int = 3,
    before_status: str = "active",
) -> int:
    scope = MemoryScope.user() if scope is None else scope
    output = {
        "memory_id": memory_id,
        "revision": revision,
        "before_status": before_status,
        "status": "forgotten",
        "memory_uri": scope.to_uri(),
    }
    return _record(
        log,
        projection,
        {"tool_id": "memory.forget", "status": "succeeded", "output": output},
    )


def _count(log: EventLog, table: str) -> int:
    return int(log.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------- 常量 / schema


def test_public_sets_and_limits_are_closed() -> None:
    assert MEMORY_KINDS == frozenset({"preference", "fact", "decision", "procedure"})
    assert MEMORY_STATUSES == frozenset({"candidate", "active", "superseded", "forgotten"})
    assert MEMORY_ORIGINS == frozenset({"user_explicit", "user_confirmed", "inferred"})
    assert MAX_RECALL_ITEMS == 5
    assert MAX_RECALL_BYTES == 4 * 1024
    assert TOKENIZER_VERSION == "cjk-bigram-v1"


def test_schema_is_idempotent_and_records_tokenizer(env) -> None:
    log, projection = env
    projection.ensure_schema()
    projection.ensure_schema()

    names = {
        row[0]
        for row in log.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    assert {"memories", "memory_meta", "memory_fts"} <= names
    version = log.conn.execute(
        "SELECT value FROM memory_meta WHERE key = 'tokenizer_version'"
    ).fetchone()[0]
    assert version == TOKENIZER_VERSION


def test_projection_refuses_a_second_connection(tmp_path: Path) -> None:
    """F10 §7：投影与事件必须同事务；第二连接直接拒绝，不给"先跑通"的后路。"""
    log = EventLog(tmp_path / "events.db")
    other = sqlite3.connect(tmp_path / "other.db", isolation_level=None)
    try:
        with pytest.raises(MemoryStoreError):
            MemoryProjection(other, log=log)
    finally:
        other.close()
        log.close()


# ---------------------------------------------------------------- 往返


def test_propose_confirm_get_search_round_trip(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "报告用中文、先列结论", created_at=100)

    candidate = projection.get("m1", allowed=[scope])
    assert candidate is not None
    assert candidate.status == "candidate"
    assert candidate.revision == 1
    assert candidate.content_ref == sha256_hex("报告用中文、先列结论".encode())
    # 候选未确认不召回（F10 §7：只生成惰性草稿）
    assert projection.search("中文结论", allowed=[scope]) == []

    _confirm(projection, log, "m1")

    memory = projection.get("m1", allowed=[scope])
    assert memory is not None
    assert memory.status == "active"
    assert memory.revision == 2
    assert memory.consolidated_at == NOW
    assert memory.source_refs == ("event:1",)
    assert memory.evidence == ({"source_memory_id": "memory_source", "quote": "原文引文"},)
    assert memory.proof_count == 1
    assert projection.read_content(memory) == "报告用中文、先列结论"

    hits = projection.search("中文结论", allowed=[scope])
    assert [hit.memory.memory_id for hit in hits] == ["m1"]
    assert hits[0].content == "报告用中文、先列结论"
    assert hits[0].score > 0


def test_apply_event_does_not_write_events(env) -> None:
    log, projection = env
    head = log.seq_head()
    _propose(projection, log, "m1", "投影不写事件")
    assert log.seq_head() == head + 1  # 只多了测试自己写的那条 tool.result


def test_non_memory_events_are_ignored(env) -> None:
    log, projection = env
    projection.apply_event("run.start", {"run_id": "x"}, seq=1)
    projection.apply_event(
        "tool.result",
        {"tool_id": "read_file", "status": "succeeded", "output": {"path": "/tmp/x"}},
        seq=2,
    )
    projection.apply_event(
        "tool.result",
        {
            "tool_id": "memory.search",
            "status": "succeeded",
            "output": {"results": []},
        },
        seq=3,
    )
    # 失败/被拒的记忆写结果也不是投影输入
    projection.apply_event(
        "tool.result",
        {"tool_id": "memory.confirm", "status": "rejected", "output": {"error": "denied"}},
        seq=4,
    )
    assert _count(log, "memories") == 0
    assert _count(log, "memory_fts") == 0


def test_event_and_projection_roll_back_together(env) -> None:
    """同事务：投影拒绝 → 事件也不留；这是"投影跑在事件日志连接上"的直接证据。"""
    log, projection = env
    payload = _propose_payload(projection, "m1", "坏种类", kind="opinion")

    with pytest.raises(MemoryStoreError):
        with log.transaction() as tx:
            seq = tx.append(RUN, "tool.result", payload)
            projection.apply_event("tool.result", payload, seq=seq)

    assert _count(log, "events") == 0
    assert _count(log, "memories") == 0
    # 连接回滚之后仍然可用
    projection.ensure_schema()
    assert projection.get("m1", allowed=[MemoryScope.user()]) is None


# ---------------------------------------------------------------- 重建


def test_rebuild_reproduces_projection_hash_and_search(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "报告用中文、先列结论", created_at=1)
    _confirm(projection, log, "m1")
    _propose(projection, log, "m2", "先写测试再写实现", kind="procedure", created_at=2)
    _confirm(projection, log, "m2")
    _propose(projection, log, "m3", "临时候选", created_at=3)

    live_hash = projection.projection_hash()
    live_hits = [hit.memory.memory_id for hit in projection.search("中文", allowed=[scope])]
    assert live_hits == ["m1"]

    applied = projection.rebuild()
    assert applied == 5  # 3 条 propose + 2 条 confirm
    assert projection.projection_hash() == live_hash
    assert [hit.memory.memory_id for hit in projection.search("中文", allowed=[scope])] == live_hits
    assert projection.get("m3", allowed=[scope]) is not None

    # 重建是幂等的：再跑一次还是同一份投影
    projection.rebuild()
    assert projection.projection_hash() == live_hash


def test_rebuild_reads_events_not_the_stale_projection(env) -> None:
    """事件是事实源：投影没跟上（例如进程在事务边界外被杀）时，rebuild 补上。"""
    log, projection = env
    payload = _propose_payload(projection, "m1", "只在事件里的记忆")
    with log.transaction() as tx:
        tx.append(RUN, "tool.result", payload)
    assert projection.get("m1", allowed=[MemoryScope.user()]) is None

    assert projection.rebuild() == 1
    memory = projection.get("m1", allowed=[MemoryScope.user()])
    assert memory is not None
    assert projection.search("记忆", allowed=[MemoryScope.user()]) == []  # 仍是候选


def test_rebuild_reproduces_dispatcher_intent_seq(env) -> None:
    """dispatcher 传 intent_seq 作来源；rebuild 必须还原同一个 seq，否则 hash 漂。"""
    log, projection = env
    scope = MemoryScope.user()
    payload = _propose_payload(
        projection, "m1", "来源指向意图事件", source_refs=()
    )
    with log.transaction() as tx:
        intent_seq = tx.append(
            RUN, "tool.intent", {"call_id": "call_x", "tool_id": "memory.propose"}
        )
        tx.append(
            RUN,
            "dispatch.admitted",
            {"call_id": "call_x", "intent_seq": intent_seq},
        )
        tx.append(
            RUN,
            "tool.result",
            {"call_id": "call_x", **payload},
        )
        projection.apply_event("tool.result", payload, seq=intent_seq)

    memory = projection.get("m1", allowed=[scope])
    assert memory is not None
    assert memory.source_refs == (f"event:{intent_seq}",)
    live_hash = projection.projection_hash()
    projection.rebuild()
    assert projection.projection_hash() == live_hash
    rebuilt = projection.get("m1", allowed=[scope])
    assert rebuilt is not None
    assert rebuilt.source_refs == (f"event:{intent_seq}",)


def test_projection_hash_covers_the_fts_index(env) -> None:
    log, projection = env
    _propose(projection, log, "m1", "索引也是投影的一部分")
    _confirm(projection, log, "m1")
    baseline = projection.projection_hash()

    log.conn.execute("DELETE FROM memory_fts")
    assert projection.projection_hash() != baseline

    projection.rebuild()
    assert projection.projection_hash() == baseline


# ---------------------------------------------------------------- 忘记


def test_forget_is_not_recalled_and_rebuild_does_not_resurrect(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "报告用中文、先列结论")
    _confirm(projection, log, "m1")
    assert [hit.memory.memory_id for hit in projection.search("中文结论", allowed=[scope])] == [
        "m1"
    ]
    assert _count(log, "memory_fts") == 1

    _forget(projection, log, "m1")

    assert projection.search("中文结论", allowed=[scope]) == []
    assert _count(log, "memory_fts") == 0
    forgotten = projection.get("m1", allowed=[scope])
    assert forgotten is not None
    assert forgotten.status == "forgotten"
    assert forgotten.revision == 3
    # 行还在：tombstone 不是"删一行就完事"
    assert _count(log, "memories") == 1
    assert projection.read_content(forgotten) == "报告用中文、先列结论"

    hash_after_forget = projection.projection_hash()
    assert projection.rebuild() == 3  # propose + confirm + forget
    assert projection.projection_hash() == hash_after_forget
    # 关键：重建之后仍然不召回、仍然看得见 tombstone
    assert projection.search("中文结论", allowed=[scope]) == []
    assert _count(log, "memory_fts") == 0
    rebuilt = projection.get("m1", allowed=[scope])
    assert rebuilt is not None
    assert rebuilt.status == "forgotten"
    assert projection.observe("m1", allowed=[scope]) == ("forgotten", 3)


def test_candidate_can_be_forgotten(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "还没激活就被删除的候选")
    _forget(projection, log, "m1", revision=2, before_status="candidate")
    assert projection.get("m1", allowed=[scope]).status == "forgotten"
    assert projection.search("候选", allowed=[scope]) == []
    projection.rebuild()
    assert projection.get("m1", allowed=[scope]).status == "forgotten"
    assert projection.search("候选", allowed=[scope]) == []


# ---------------------------------------------------------------- 去重


def test_find_duplicate_and_deduped_event_do_not_grow_the_index(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "报告用中文、先列结论")
    _confirm(projection, log, "m1")
    first = projection.get("m1", allowed=[scope])
    assert first is not None
    assert _count(log, "memory_fts") == 1

    duplicate = projection.find_duplicate(
        scope=scope, kind="preference", content="报告用中文、先列结论"
    )
    assert duplicate is not None
    assert duplicate.memory_id == "m1"

    payload = _propose_payload(
        projection,
        "m1",
        "报告用中文、先列结论",
        deduped=True,
        revision=duplicate.revision,
        content_ref=first.content_ref,
        source_refs=("event:9",),
    )
    _record(log, projection, payload)

    assert _count(log, "memories") == 1
    assert _count(log, "memory_fts") == 1
    assert projection.find_duplicate(
        scope=scope, kind="decision", content="报告用中文、先列结论"
    ) is None

    with pytest.raises(MemoryStoreError):
        projection.find_duplicate(
            scope=scope, kind="preference", content="sk-" + "a1B2c3D4e5F6g7H8i9J0"
        )


def test_duplicate_propose_without_deduped_is_rejected_atomically(env) -> None:
    log, projection = env
    _propose(projection, log, "m1", "去重之前必须已经查过")
    _confirm(projection, log, "m1")
    events_before = _count(log, "events")
    fts_before = _count(log, "memory_fts")

    with pytest.raises(MemoryStoreError):
        _propose(projection, log, "m2", "去重之前必须已经查过")

    # 事件与投影一起回滚：没有"succeeded 但没进投影"的假成功
    assert _count(log, "events") == events_before
    assert _count(log, "memories") == 1
    assert _count(log, "memory_fts") == fts_before


def test_deduped_event_cannot_point_at_a_forgotten_row(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "已经被忘记的重复目标")
    _forget(projection, log, "m1", revision=2, before_status="candidate")
    memory = projection.get("m1", allowed=[scope])
    assert memory is not None
    payload = _propose_payload(
        projection,
        "m1",
        "已经被忘记的重复目标",
        deduped=True,
        revision=memory.revision,
        content_ref=memory.content_ref,
    )
    with pytest.raises(MemoryStoreError):
        projection.apply_event("tool.result", payload, seq=99)


def test_projection_rejects_events_that_replay_order_cannot_explain(env) -> None:
    """投影必须只能是"按 seq 重放"的结果；早于 last_seq 的事件直接拒绝。"""
    log, projection = env
    scope = MemoryScope.user()
    payload = _propose_payload(projection, "m1", "顺序不能倒")
    projection.apply_event("tool.result", payload, seq=10)
    confirm = {
        "tool_id": "memory.confirm",
        "status": "succeeded",
        "output": {
            "memory_id": "m1",
            "revision": 2,
            "before_status": "candidate",
            "status": "active",
            "consolidated_at": NOW,
            "memory_uri": scope.to_uri(),
        },
    }
    with pytest.raises(MemoryStoreError):
        projection.apply_event("tool.result", confirm, seq=5)
    assert projection.get("m1", allowed=[scope]).status == "candidate"


# ---------------------------------------------------------------- 中文 / 混合标识符


def test_cjk_bigram_recall_is_quantified(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "报告用中文、先列结论")
    _confirm(projection, log, "m1")

    stored = log.conn.execute(
        "SELECT tokens FROM memory_fts WHERE memory_id = 'm1'"
    ).fetchone()[0]
    # 固定夹具的固定 token：跨标点不组双字
    assert stored.split() == ["报告", "告用", "用中", "中文", "先列", "列结", "结论"]

    # 查询按同一套双字切分；OR 召回（"文结"不在正文里，不能要求全中）
    assert [hit.memory.memory_id for hit in projection.search("中文结论", allowed=[scope])] == [
        "m1"
    ]
    assert [hit.memory.memory_id for hit in projection.search("先列", allowed=[scope])] == ["m1"]
    assert [hit.memory.memory_id for hit in projection.search("结论", allowed=[scope])] == ["m1"]
    assert projection.search("英文", allowed=[scope]) == []
    assert projection.search("", allowed=[scope]) == []
    assert projection.search("   ", allowed=[scope]) == []


def test_mixed_cjk_and_identifier_recall(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    content = "调用 memory.search 之前先看 ToolRegistry 的说明"
    _propose(projection, log, "m1", content, kind="procedure")
    _confirm(projection, log, "m1")

    tokens = log.conn.execute(
        "SELECT tokens FROM memory_fts WHERE memory_id = 'm1'"
    ).fetchone()[0].split()
    assert "memory" in tokens
    assert "search" in tokens
    assert "toolregistry" in tokens

    for query in ("memory.search", "ToolRegistry", "toolregistry", "Search"):
        hits = projection.search(query, allowed=[scope])
        assert [hit.memory.memory_id for hit in hits] == ["m1"], query
    assert projection.search("absent_token", allowed=[scope]) == []


# ---------------------------------------------------------------- 排序 / 过期


def test_origin_order_then_created_at_descending(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(
        projection, log, "inferred", "排序样例 丙", origin="inferred", created_at=999
    )
    _confirm(projection, log, "inferred")
    _propose(
        projection, log, "confirmed", "排序样例 乙", origin="user_confirmed", created_at=1
    )
    _confirm(projection, log, "confirmed")
    _propose(
        projection, log, "explicit_old", "排序样例 甲", origin="user_explicit", created_at=10
    )
    _confirm(projection, log, "explicit_old")
    _propose(
        projection, log, "explicit_new", "排序样例 丁", origin="user_explicit", created_at=20
    )
    _confirm(projection, log, "explicit_new")

    hits = projection.search("排序样例", allowed=[scope])
    assert [hit.memory.memory_id for hit in hits] == [
        "explicit_new",
        "explicit_old",
        "confirmed",
        "inferred",
    ]
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_expired_memory_is_not_recalled_but_get_still_works(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "expired", "到期样例 旧", created_at=1, expires_at=NOW)
    _confirm(projection, log, "expired")
    _propose(projection, log, "future", "到期样例 新", created_at=2, expires_at=NOW + 1)
    _confirm(projection, log, "future")

    hits = projection.search("到期样例", allowed=[scope])
    assert [hit.memory.memory_id for hit in hits] == ["future"]
    # 明确按 id 取仍然看得到（审计/编辑需要），只是不参与召回
    assert projection.get("expired", allowed=[scope]) is not None
    assert projection.observe("expired", allowed=[scope]) == ("active", 2)


# ---------------------------------------------------------------- 字节裁剪


def test_recall_byte_budget_truncates_instead_of_returning_empty(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    huge = "预算" + "记" * 3000  # 9000 字节，远超 4 KiB
    _propose(projection, log, "huge", huge, created_at=2)
    _confirm(projection, log, "huge")
    _propose(projection, log, "small", "预算 小条目", created_at=1)
    _confirm(projection, log, "small")

    hits = projection.search("预算", allowed=[scope], max_bytes=MAX_RECALL_BYTES)
    assert len(hits) == 1, "第一条被截断后不应再塞后面的条目"
    assert hits[0].memory.memory_id == "huge"
    assert 0 < len(hits[0].content.encode("utf-8")) <= MAX_RECALL_BYTES
    assert hits[0].content != huge
    assert sum(len(hit.content.encode("utf-8")) for hit in hits) <= MAX_RECALL_BYTES

    # 预算够时两条都在，且总字节仍不超
    roomy = projection.search("预算", allowed=[scope], max_bytes=MAX_RECALL_BYTES * 4)
    assert {hit.memory.memory_id for hit in roomy} == {"huge", "small"}
    assert sum(len(hit.content.encode("utf-8")) for hit in roomy) <= MAX_RECALL_BYTES * 4


def test_recall_limit_counts_and_bytes_together(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    for index in range(7):
        _propose(
            projection,
            log,
            f"m{index}",
            f"限量样例 {index}",
            created_at=index + 1,
        )
        _confirm(projection, log, f"m{index}")

    hits = projection.search("限量样例", allowed=[scope])
    assert len(hits) == MAX_RECALL_ITEMS
    assert [hit.memory.memory_id for hit in hits] == [
        "m6",
        "m5",
        "m4",
        "m3",
        "m2",
    ]
    assert projection.search("限量样例", allowed=[scope], limit=0) == []
    assert len(projection.search("限量样例", allowed=[scope], limit=2)) == 2


# ---------------------------------------------------------------- 作用域


def test_cross_scope_isolation_and_loadout(env, tmp_path: Path) -> None:
    log, projection = env
    root_a = MemoryScope.workspace(tmp_path / "workspace_a")
    root_b = MemoryScope.workspace(tmp_path / "workspace_b")
    task = MemoryScope.task("task_alpha")
    user = MemoryScope.user()

    _propose(projection, log, "wa", "工作区甲的私有记忆", scope=root_a, created_at=1)
    _confirm(projection, log, "wa", scope=root_a)
    _propose(projection, log, "wb", "工作区乙的私有记忆", scope=root_b, created_at=2)
    _confirm(projection, log, "wb", scope=root_b)
    _propose(projection, log, "u", "用户全局记忆", scope=user, created_at=3)
    _confirm(projection, log, "u", scope=user)
    _propose(projection, log, "t", "任务记忆", scope=task, created_at=4)
    _confirm(projection, log, "t", scope=task)

    only_b = projection.search("私有记忆", allowed=[root_b])
    assert [hit.memory.memory_id for hit in only_b] == ["wb"]
    assert projection.get("wa", allowed=[root_b]) is None
    assert projection.get("wb", allowed=[root_a]) is None
    assert projection.list_memories(allowed=[root_b])[0].memory_id == "wb"
    assert projection.search("私有记忆", allowed=[]) == []

    loadout = projection.loadout(
        task_id="task_alpha",
        workspace_roots=[
            str(tmp_path / "workspace_a"),
            str(tmp_path / "workspace_a" / "."),
            str(tmp_path / "workspace_b"),
            str(tmp_path / "workspace_a"),
        ],
    )
    # 去重、保持首次出现顺序；user 永远在、task 最后
    assert loadout == (user, root_a, root_b, task)
    visible = set()
    for scope in loadout:
        visible.update(memory.memory_id for memory in projection.list_memories(allowed=[scope]))
    assert visible == {"wa", "wb", "u", "t"}


def test_user_memory_is_recalled_by_a_later_task(env, tmp_path: Path) -> None:
    """F10 §14 第一条：记住偏好之后，**新任务**要能检索到（不是同一个投影里自问自答）。"""
    log, projection = env
    user = MemoryScope.user()
    _propose(projection, log, "pref", "报告用中文、先列结论", scope=user)
    _confirm(projection, log, "pref", scope=user)

    later = projection.loadout(
        task_id="task_later",
        workspace_roots=[str(tmp_path / "later_ws")],
    )
    assert user in later
    hits = projection.search("中文结论", allowed=later)
    assert [hit.memory.memory_id for hit in hits] == ["pref"]

    # 另一个 workspace 的私有记忆不能借"新任务"的装备混进来
    other_ws = MemoryScope.workspace(tmp_path / "other_ws")
    _propose(projection, log, "private", "别的工作区私有记忆", scope=other_ws)
    _confirm(projection, log, "private", scope=other_ws)
    assert projection.search("私有记忆", allowed=later) == []
    assert projection.get("private", allowed=later) is None


def test_workspace_uri_scope_mismatch_is_rejected(env, tmp_path: Path) -> None:
    log, projection = env
    workspace = MemoryScope.workspace(tmp_path / "ws_a")
    other = MemoryScope.workspace(tmp_path / "ws_b")
    _propose(projection, log, "m1", "作用域必须对得上", scope=workspace)

    payload = {
        "tool_id": "memory.confirm",
        "status": "succeeded",
        "output": {
            "memory_id": "m1",
            "revision": 2,
            "before_status": "candidate",
            "status": "active",
            "consolidated_at": NOW,
            "memory_uri": other.to_uri(),
        },
    }
    with pytest.raises(MemoryStoreError):
        _record(log, projection, payload)
    assert projection.get("m1", allowed=[workspace]).status == "candidate"


# ---------------------------------------------------------------- 写入闸门


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "a1B2c3D4e5F6g7H8i9J0",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_" + "a" * 36,
        "github_pat_" + "a" * 30,
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0",
    ],
)
def test_secret_patterns_are_blocked_before_storage(env, secret: str) -> None:
    _, projection = env
    safe, blocked = projection.redact_or_block(f"这里是内容 {secret} 结束")
    assert blocked is True
    assert secret not in safe
    with pytest.raises(MemoryWriteBlocked):
        projection.put_content(f"这里是内容 {secret} 结束")
    assert not projection.blob_dir.exists() or not any(projection.blob_dir.iterdir())


def test_pii_is_redacted_before_storage(env) -> None:
    _, projection = env
    cases = {
        "联系 alice@example.com 获取": ("alice@example.com", "[REDACTED:email]"),
        "电话 13800138000 备用": ("13800138000", "[REDACTED:phone]"),
        "身份证 11010519491231002X 复印件": ("11010519491231002X", "[REDACTED:id_card]"),
    }
    for raw, (needle, placeholder) in cases.items():
        safe, blocked = projection.redact_or_block(raw)
        assert blocked is False
        assert needle not in safe
        assert placeholder in safe
        ref = projection.put_content(raw)
        stored = (projection.blob_dir / ref).read_bytes().decode("utf-8")
        assert needle not in stored
        assert placeholder in stored


def test_blocked_candidate_does_not_enter_projection(env) -> None:
    """绕过 put_content() 手工塞一个密钥 blob：apply_event 也必须拒绝。"""
    log, projection = env
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    projection.blob_dir.mkdir(parents=True, exist_ok=True)
    ref = sha256_hex(secret.encode("utf-8"))
    (projection.blob_dir / ref).write_bytes(secret.encode("utf-8"))

    payload = _propose_payload(
        projection, "m1", secret, content_ref=ref, deduped=False
    )
    with pytest.raises(MemoryWriteBlocked):
        _record(log, projection, payload)

    assert _count(log, "memories") == 0
    assert _count(log, "memory_fts") == 0
    assert _count(log, "events") == 0


def test_pii_bypassing_put_content_is_rejected(env) -> None:
    log, projection = env
    raw = "联系 alice@example.com"
    projection.blob_dir.mkdir(parents=True, exist_ok=True)
    ref = sha256_hex(raw.encode("utf-8"))
    (projection.blob_dir / ref).write_bytes(raw.encode("utf-8"))

    payload = _propose_payload(projection, "m1", raw, content_ref=ref)
    with pytest.raises(MemoryWriteBlocked):
        _record(log, projection, payload)
    assert _count(log, "memories") == 0


# ---------------------------------------------------------------- 正文缺失


def test_missing_blob_is_content_unavailable_and_never_falls_back(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "first unicorn payload", created_at=1)
    _confirm(projection, log, "m1")
    _propose(projection, log, "m2", "second phoenix payload", created_at=2)
    _confirm(projection, log, "m2")
    m1 = projection.get("m1", allowed=[scope])
    m2 = projection.get("m2", allowed=[scope])
    assert m1 is not None and m2 is not None

    (projection.blob_dir / m1.content_ref).unlink()
    assert projection.read_content(m1) is None
    assert projection.read_content(m2) == "second phoenix payload"
    # 不能拿"最新记忆"顶上
    assert projection.search("unicorn", allowed=[scope]) == []

    projection.rebuild()
    rebuilt = projection.get("m1", allowed=[scope])
    assert rebuilt is not None
    assert rebuilt.status == "active"
    assert projection.read_content(rebuilt) is None
    assert projection.search("unicorn", allowed=[scope]) == []

    # blob 恢复后 rebuild 能按同一 content_ref 重新索引（不是"最新版本"）
    projection.blob_dir.joinpath(m1.content_ref).write_bytes(b"first unicorn payload")
    projection.rebuild()
    restored = projection.get("m1", allowed=[scope])
    assert restored is not None
    assert projection.read_content(restored) == "first unicorn payload"
    assert [hit.memory.memory_id for hit in projection.search("unicorn", allowed=[scope])] == [
        "m1"
    ]


def test_missing_blob_at_propose_marks_unavailable(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    ref = projection.put_content("先写正文再删掉")
    (projection.blob_dir / ref).unlink()

    _propose(projection, log, "m1", "先写正文再删掉", content_ref=ref)
    memory = projection.get("m1", allowed=[scope])
    assert memory is not None
    assert projection.read_content(memory) is None
    assert _count(log, "memory_fts") == 0
    _confirm(projection, log, "m1")
    assert projection.search("正文", allowed=[scope]) == []


def test_blob_restored_after_propose_still_passes_the_gate(env) -> None:
    """确认时补写索引也必须过闸门：之前没读到不是跳过扫描的理由。"""
    log, projection = env
    scope = MemoryScope.user()
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    ref = sha256_hex(secret.encode())
    payload = _propose_payload(projection, "m1", "占位", content_ref=ref)
    _record(log, projection, payload)
    assert projection.get("m1", allowed=[scope]).status == "candidate"
    assert _count(log, "memory_fts") == 0

    projection.blob_dir.mkdir(parents=True, exist_ok=True)
    (projection.blob_dir / ref).write_bytes(secret.encode())
    with pytest.raises(MemoryWriteBlocked):
        _confirm(projection, log, "m1")
    # 确认事件和"补写索引"一起回滚：候选没有变成 active
    assert projection.get("m1", allowed=[scope]).status == "candidate"
    assert _count(log, "memory_fts") == 0
    assert _count(log, "events") == 1


def test_read_content_rejects_non_digest_reference(env) -> None:
    """content_ref 会拼进文件路径；非 sha256 形状的引用不能拿来越界读文件。"""
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "正文引用形状")
    memory = projection.get("m1", allowed=[scope])
    assert memory is not None
    for bad in ("../events.db", "/etc/passwd", "0" * 63 + "z"):
        with pytest.raises(MemoryStoreError):
            projection.read_content(replace(memory, content_ref=bad))


# ---------------------------------------------------------------- 封闭集 / 状态机


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "opinion"},
        {"scope": "team", "scope_id": "x"},
        {"scope": "user", "scope_id": "root"},
        {"origin": "guessed"},
        {"status": "active"},
        {"revision": 2},
        {"proof_count": 99},
        {"deduped": "yes"},
    ],
)
def test_unknown_or_invalid_propose_values_raise(env, overrides: dict) -> None:
    log, projection = env
    before = _count(log, "events")
    payload = _propose_payload(projection, "m1", "非法输入")
    payload["output"].update(overrides)
    with pytest.raises(MemoryStoreError):
        _record(log, projection, payload)
    assert _count(log, "events") == before
    assert _count(log, "memories") == 0


def test_empty_source_refs_are_anchored_to_the_writing_event(env) -> None:
    """source_refs 在工具 schema 里可选；空列表不能变成"无来源记忆"。"""
    log, projection = env
    scope = MemoryScope.user()
    seq = _propose(projection, log, "m1", "来源由写入事件补足", source_refs=())
    memory = projection.get("m1", allowed=[scope])
    assert memory is not None
    assert memory.source_refs == (f"event:{seq}",)
    projection.rebuild()
    rebuilt = projection.get("m1", allowed=[scope])
    assert rebuilt is not None
    assert rebuilt.source_refs == (f"event:{seq}",)


def test_bad_state_transitions_raise(env) -> None:
    log, projection = env
    _propose(projection, log, "m1", "状态机不能乱跳")

    bad = [
        {"revision": 3},  # 必须 1 → 2
        {"before_status": "active"},
        {"status": "superseded"},
        {"memory_uri": "memory://task/task_other"},  # 作用域不符
    ]
    for override in bad:
        output = {
            "memory_id": "m1",
            "revision": 2,
            "before_status": "candidate",
            "status": "active",
            "consolidated_at": NOW,
            "memory_uri": MemoryScope.user().to_uri(),
        }
        output.update(override)
        with pytest.raises(MemoryStoreError):
            _record(
                log,
                projection,
                {"tool_id": "memory.confirm", "status": "succeeded", "output": output},
            )
    with pytest.raises(MemoryStoreError):
        _confirm(projection, log, "absent", revision=2)
    assert projection.get("m1", allowed=[MemoryScope.user()]).status == "candidate"


def test_corrupt_projection_row_is_not_laundered(env) -> None:
    """历史非法值读取时抛错，不洗白（AGENTS §七）。"""
    log, projection = env
    _propose(projection, log, "m1", "会被手工改坏")
    log.conn.execute("UPDATE memories SET kind = 'opinion' WHERE memory_id = 'm1'")
    with pytest.raises(MemoryStoreError):
        projection.get("m1", allowed=[MemoryScope.user()])
    with pytest.raises(MemoryStoreError):
        projection.list_memories(allowed=[MemoryScope.user()])


def test_list_memories_status_filter_and_limit(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "c1", "候选一", created_at=1)
    _propose(projection, log, "c2", "候选二", created_at=2)
    _propose(projection, log, "a1", "活动一", created_at=3)
    _confirm(projection, log, "a1")

    assert [m.memory_id for m in projection.list_memories(allowed=[scope], status="candidate")] == [
        "c2",
        "c1",
    ]
    assert [m.memory_id for m in projection.list_memories(allowed=[scope], status="active")] == [
        "a1"
    ]
    assert [m.memory_id for m in projection.list_memories(allowed=[scope], limit=2)] == [
        "a1",
        "c2",
    ]
    with pytest.raises(MemoryStoreError):
        projection.list_memories(allowed=[scope], status="unknown")


def test_tokenizer_version_mismatch_requires_rebuild(env) -> None:
    log, projection = env
    _propose(projection, log, "m1", "版本要落库")
    log.conn.execute("UPDATE memory_meta SET value = 'old-tokenizer'")
    with pytest.raises(MemoryStoreError):
        projection.ensure_schema()
    with pytest.raises(MemoryStoreError):
        projection.search("版本", allowed=[MemoryScope.user()])

    projection.rebuild()
    assert log.conn.execute(
        "SELECT value FROM memory_meta WHERE key = 'tokenizer_version'"
    ).fetchone()[0] == TOKENIZER_VERSION
    projection.ensure_schema()


# ---------------------------------------------------------------- supersede / observe


def test_supersede_keeps_old_row_but_removes_it_from_recall(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _propose(projection, log, "old", "旧偏好 alpha token", created_at=1)
    _confirm(projection, log, "old")
    # 生产形状是 `memory.edit`：新行直接 active + user_confirmed（一次审批），
    # 旧行 superseded。不再借 propose 的 output 造 supersede。
    payload = _propose_payload(
        projection,
        "new",
        "新偏好 beta token",
        created_at=2,
        origin="user_confirmed",
        status="active",
        supersedes="old",
    )
    payload["tool_id"] = "memory.edit"
    _record(log, projection, payload)

    old = projection.get("old", allowed=[scope])
    assert old is not None
    assert old.status == "superseded"
    assert projection.search("alpha", allowed=[scope]) == []
    assert [hit.memory.memory_id for hit in projection.search("beta", allowed=[scope])] == [
        "new"
    ]
    before = projection.projection_hash()
    projection.rebuild()
    assert projection.projection_hash() == before
    assert projection.get("old", allowed=[scope]).status == "superseded"


def test_observe_is_scope_filtered(env, tmp_path: Path) -> None:
    log, projection = env
    other = MemoryScope.workspace(tmp_path / "elsewhere")
    _propose(projection, log, "m1", "观察接口")
    _confirm(projection, log, "m1")
    assert projection.observe("m1", allowed=[MemoryScope.user()]) == ("active", 2)
    assert projection.observe("m1", allowed=[other]) is None
    assert projection.observe("absent", allowed=[MemoryScope.user()]) is None


def test_put_content_is_content_addressed(env) -> None:
    _, projection = env
    ref_one = projection.put_content("同样的正文")
    ref_two = projection.put_content("同样的正文")
    assert ref_one == ref_two == sha256_hex("同样的正文".encode())
    assert (projection.blob_dir / ref_one).read_bytes() == "同样的正文".encode()


# ---------------------------------------------------------------- 类型形状


def test_memory_to_dict_is_json_ready(env) -> None:
    log, projection = env
    _propose(projection, log, "m1", "字典形状")
    memory = projection.get("m1", allowed=[MemoryScope.user()])
    assert isinstance(memory, Memory)
    payload = memory.to_dict()
    assert payload["source_refs"] == ["event:1"]
    assert payload["evidence"] == [{"source_memory_id": "memory_source", "quote": "原文引文"}]
    assert json.loads(json.dumps(payload, ensure_ascii=False))["memory_id"] == "m1"
    hit = Recalled(memory=memory, content="字典形状", score=1.0)
    assert hit.memory is memory


def test_non_json_evidence_is_rejected(env) -> None:
    """非 JSON 的 evidence 连事件都写不进去；这里直接压投影的校验。

    真实 dispatcher 的 `append()` 会先抛 TypeError，事件根本不会产生——所以
    投影永远看不到它。这条用例证明投影自己的校验也是 fail-closed 的。
    """
    log, projection = env
    payload = _propose_payload(
        projection,
        "m1",
        "证据必须是 JSON",
        evidence=({"bad": object()},),
    )
    with pytest.raises(MemoryStoreError):
        projection.apply_event("tool.result", payload, seq=1)
    assert _count(log, "memories") == 0


def test_resource_errors_are_tianshu_errors(env) -> None:
    _, projection = env
    with pytest.raises(TianshuError):
        projection.loadout(task_id="bad task id", workspace_roots=[])


# ---------------------------------------------------------------- 真实 dispatcher 端到端


def test_real_dispatcher_propose_confirm_forget_round_trip(tmp_path: Path) -> None:
    """不 mock 派发链：真 Dispatcher + 真审批 + 真投影，最后 rebuild 仍然不复活。

    这条用例专门盯集成形状（单测盖不到）：
    - dispatcher 传给 apply_event 的 seq 是 `tool.intent` 的 seq，不是 result 的；
    - 投影与事件在同一个事务里；记忆正文的 blob 目录与读入口一致；
    - confirm/forget 真的走审批，而不是测试里手写一个 output。
    """
    from tianshu.dispatcher import KIND_WAITING, build_request
    from tianshu.events import EventLog
    from tianshu.loop import build_stack
    from tianshu.permissions import HumanIdentity
    from tianshu.tasks import RUN_RUNNING

    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "authority")
    try:
        task = stack.tasks.create_task("记忆端到端", [{"root_path": str(ws)}])
        run = stack.tasks.start_run(task.task_id)
        stack.leases.acquire([ws], run.run_id)
        job = stack.claims.enqueue(run.run_id)
        claim = stack.claims.claim(job.job_id)
        stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)

        def dispatch(tool_id: str, args: dict, call_id: str) -> dict:
            request = build_request(
                task=task,
                run=run,
                claim=claim,
                call_id=call_id,
                tool_id=tool_id,
                target=MemoryScope.user().to_uri(),
                args=args,
            )
            decision = stack.dispatcher.prepare(request, claim, args=args)
            if decision.kind == KIND_WAITING:
                stack.approvals.decide(
                    decision.approval_id,
                    decision="approved",
                    actor=HumanIdentity.local_cli(),
                )
            result = stack.dispatcher.dispatch(request.request_id, claim)
            assert result.ok, result.output
            assert result.status == "succeeded"
            return result.output

        scope = MemoryScope.user()
        proposed = dispatch(
            "memory.propose",
            {"kind": "preference", "content": "报告用中文、先列结论"},
            "call_propose",
        )
        memory_id = proposed["memory_id"]
        candidate = stack.memory.get(memory_id, allowed=[scope])
        assert candidate is not None
        assert candidate.status == "candidate"
        # dispatcher 用 tool.intent 的 seq，来源指向那次意图而不是 result
        assert candidate.source_refs[0].startswith("event:")

        dispatch("memory.confirm", {"memory_id": memory_id, "revision": 1}, "call_confirm")
        hits = stack.memory.search("中文结论", allowed=[scope])
        assert [hit.memory.memory_id for hit in hits] == [memory_id]

        dispatch("memory.forget", {"memory_id": memory_id, "revision": 2}, "call_forget")
        assert stack.memory.search("中文结论", allowed=[scope]) == []
        assert stack.memory.get(memory_id, allowed=[scope]).status == "forgotten"

        live_hash = stack.memory.projection_hash()
        assert stack.memory.rebuild() == 3
        assert stack.memory.projection_hash() == live_hash
        assert stack.memory.search("中文结论", allowed=[scope]) == []
        assert stack.memory.get(memory_id, allowed=[scope]).status == "forgotten"
    finally:
        log.close()


# ---------------------------------------------------------------- 进程重启 / 目录口径


def test_forget_and_content_survive_process_restart(tmp_path: Path) -> None:
    """重启后仍然不召回 forgotten，且正文仍按 content_ref 读得到（不是重取最新）。"""
    db = tmp_path / "events.db"
    blobs = tmp_path / "blobs"

    log = EventLog(db, blob_dir=blobs)
    projection = MemoryProjection(log.conn, log=log, blob_dir=blobs, clock=lambda: NOW)
    projection.ensure_schema()
    scope = MemoryScope.user()
    _propose(projection, log, "m1", "重启之后也不能复活")
    _confirm(projection, log, "m1")
    content_ref = projection.get("m1", allowed=[scope]).content_ref
    _forget(projection, log, "m1")
    log.close()

    reopened = EventLog(db, blob_dir=blobs)
    projection = MemoryProjection(
        reopened.conn, log=reopened, blob_dir=blobs, clock=lambda: NOW
    )
    projection.ensure_schema()
    try:
        assert projection.search("复活", allowed=[scope]) == []
        memory = projection.get("m1", allowed=[scope])
        assert memory is not None
        assert memory.status == "forgotten"
        assert memory.content_ref == content_ref
        assert projection.read_content(memory) == "重启之后也不能复活"
        projection.rebuild()
        assert projection.search("复活", allowed=[scope]) == []
        assert projection.get("m1", allowed=[scope]).status == "forgotten"
    finally:
        reopened.close()


def test_default_blob_dir_is_separate_from_event_payload_store(tmp_path: Path) -> None:
    """正文目录**不**跟随事件 payload 的 `log.blob_dir`，固定落 DB 旁 `memory_blobs/`。

    两者保留策略不同：事件 payload 是日志材料，必须留存供 replay / 审计；记忆
    正文是投影产物，F10 §10 要给"物理擦除"留口子，将来会按策略删。共用同一个
    content-addressed 目录还有一个具体误伤通道——某条记忆正文若与某个事件
    payload 是同一段字节，它们就是同一个文件，按策略删记忆会把日志材料一起删掉。

    `EventLog` **配置了** blob_dir 时也不能跟着走，这条断言就是钉这个。
    """
    with_blobs = EventLog(tmp_path / "with_blobs.db", blob_dir=tmp_path / "blobs")
    assert MemoryProjection(with_blobs.conn, log=with_blobs).blob_dir == (
        tmp_path / "memory_blobs"
    )
    with_blobs.close()

    plain = EventLog(tmp_path / "plain.db")
    assert MemoryProjection(plain.conn, log=plain).blob_dir == (tmp_path / "memory_blobs")
    plain.close()


def test_explicit_blob_dir_wins(tmp_path: Path) -> None:
    """显式传入仍然优先（测试与将来的迁移入口需要）。"""
    log = EventLog(tmp_path / "x.db", blob_dir=tmp_path / "blobs")
    explicit = tmp_path / "custom_memory_blobs"
    assert MemoryProjection(log.conn, log=log, blob_dir=explicit).blob_dir == explicit
    log.close()


def test_in_memory_database_marks_content_unavailable() -> None:
    """纯内存库没有"DB 旁边"：不编路径，正文如实标成不可用。"""
    log = EventLog(Path(":memory:"))
    projection = MemoryProjection(log.conn, log=log, clock=lambda: NOW)
    projection.ensure_schema()
    try:
        assert projection.blob_dir is None
        _propose(projection, log, "m1", "内存库没有正文目录")
        memory = projection.get("m1", allowed=[MemoryScope.user()])
        assert memory is not None
        assert projection.read_content(memory) is None
        assert _count(log, "memory_fts") == 0
        _confirm(projection, log, "m1")
        assert projection.search("内存库", allowed=[MemoryScope.user()]) == []
    finally:
        log.close()


def test_schema_v1_database_is_migrated_in_place(env) -> None:
    """v1 的库没有 erased_at 列、也没有 schema_version：打开时补列、不重建。"""
    log, projection = env
    _propose(projection, log, "m_old", "旧库里的记忆")
    projection.rebuild()
    # 模拟 v1：删掉新列与版本记录（SQLite >= 3.35 支持 DROP COLUMN）。
    log.conn.execute("ALTER TABLE memories DROP COLUMN erased_at")
    log.conn.execute("DELETE FROM memory_meta WHERE key = 'schema_version'")
    projection._schema_ready = False
    projection.ensure_schema()
    memory = projection.get("m_old", allowed=[MemoryScope.user()])
    assert memory is not None
    assert memory.erased_at is None
    assert projection.read_content(memory) == "旧库里的记忆"
    row = log.conn.execute(
        "SELECT value FROM memory_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None and int(row[0]) == 2
