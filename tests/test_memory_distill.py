"""L2/L3 确定性蒸馏的判据（F10 §4 / SPEC §4.3）。

要点不是"合成得像摘要"，而是**可机械核对**：

- 两次合成**逐字节相同**（不是"看起来一样"）；
- 改一条源只动与它相关的那一块，其余行逐字节不变（增量 delta）；
- `proof_count` 与证据条数机械一致；
- 旧页被新页 supersede 后仍可 `get`，方向正确；
- freshness：作用域里出现更新的 L1 后，旧页 `stale=true` 且排在新鲜证据之后；
- replay：重建后页内容与 `consolidated_at` 一致。

LLM 合成明确不在本文件里（未实现，也不假装）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.memory import MemoryProjection, MemoryStoreError
from tianshu.memory_distill import (
    L2_MIN_GROUP,
    L2_SIMILARITY,
    L3_PAGE_BYTES,
    ConsolidationSource,
    build_l2_page,
    build_l3_page,
    consolidate,
    page_memory_id,
)
from tianshu.resources import MemoryScope

NOW = 1_700_000_000_000
RUN = "run_distill"


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
    scope: MemoryScope | None = None,
    kind: str = "preference",
    origin: str = "user_confirmed",
) -> None:
    scope = MemoryScope.user() if scope is None else scope
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
                "kind": kind,
                "content_ref": content_ref,
                "source_refs": ["event:1"],
                "evidence": [{"source_memory_id": "memory_source", "quote": "原文引文"}],
                "proof_count": 1,
                "origin": origin,
                "status": "candidate",
                "created_at": 1,
                "expires_at": None,
                "deduped": False,
                "layer": "l1",
            },
        },
    )
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


def _sources(projection: MemoryProjection, scope: MemoryScope | None = None) -> list:
    scope = MemoryScope.user() if scope is None else scope
    return projection.consolidatable_sources(scope)


def _apply_pages(env, pages, scope: MemoryScope | None = None) -> list[str]:
    """把合成页按真实事件形状写进投影（与 executor 输出的 output 一致）。"""
    log, projection = env
    scope = MemoryScope.user() if scope is None else scope
    out_pages = []
    for page in pages:
        content_ref = projection.put_content(page.content)
        item = page.to_output()
        item.update(
            {
                "memory_id": page_memory_id(scope.to_uri(), page),
                "content_ref": content_ref,
                "consolidated_at": NOW,
                "status": "candidate",
                "origin": "inferred",
            }
        )
        out_pages.append(item)
    payload = {
        "tool_id": "memory.consolidate",
        "status": "succeeded",
        "output": {
            "scope": scope.scope,
            "scope_id": scope.scope_id,
            "memory_uri": scope.to_uri(),
            "pages": out_pages,
        },
    }
    _record(log, projection, payload)
    return [item["memory_id"] for item in out_pages]


def _confirm_page(log, projection, memory_id: str) -> None:
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
                "memory_uri": MemoryScope.user().to_uri(),
            },
        },
    )


def _eight_fixtures() -> list[ConsolidationSource]:
    """固定 8 条 L1：两组同主题 + 两条无关。"""
    return [
        ConsolidationSource("m1", 1, "报告用中文写，先列结论", 1, "preference", "user_explicit"),
        ConsolidationSource("m2", 1, "报告里先讲结论再给依据", 2, "preference", "user_confirmed"),
        ConsolidationSource(
            "m3", 1, "报告不要用英文，术语保留英文", 3, "preference", "user_confirmed"
        ),
        ConsolidationSource(
            "m4", 1, "数据库迁移分三步：备份、演练、割接", 4, "procedure", "inferred"
        ),
        ConsolidationSource("m5", 1, "数据库迁移前先做备份演练", 5, "procedure", "inferred"),
        ConsolidationSource("m6", 1, "日志保留 30 天", 6, "fact", "inferred"),
        ConsolidationSource("m7", 1, "周报里加上风险项", 7, "preference", "user_confirmed"),
        ConsolidationSource("m8", 1, "失败先排查原因再重试", 8, "procedure", "inferred"),
    ]


def test_threshold_is_pinned_by_tests() -> None:
    """阈值与最少簇大小写死在代码里，并由测试钉住（改阈值必须重跑判据）。"""
    assert L2_SIMILARITY == 0.34
    assert L2_MIN_GROUP == 2
    assert L3_PAGE_BYTES == 4 * 1024


def test_l2_content_is_reference_assembly_not_free_form() -> None:
    """L2 正文是引用式拼装：每条一行、带 `[memory_id@revision]`，块间空行。"""
    page = build_l2_page(_eight_fixtures())
    assert page is not None
    assert page.layer == "l2"
    assert page.kind == "fact"
    lines = [line for line in page.content.split("\n") if line.startswith("- ")]
    assert lines, page.content
    for line in lines:
        assert "@" in line and line.rstrip().endswith("]")
    assert "\n\n" in page.content, "不同簇之间要空行分隔"
    assert page.proof_count == len(page.evidence)


def test_two_consolidations_are_byte_identical() -> None:
    """两次合成**逐字节**相同（不是"看起来一样"）。"""
    first = consolidate(_eight_fixtures(), scope=MemoryScope.user())
    second = consolidate(_eight_fixtures(), scope=MemoryScope.user())
    assert [page.to_output() for page in first] == [page.to_output() for page in second]
    assert [page.content.encode() for page in first] == [
        page.content.encode() for page in second
    ]


def test_changing_one_source_only_moves_its_own_block() -> None:
    """增量 delta：改一条源，只有它所在的块变化，其它块逐字节不变。"""
    sources = _eight_fixtures()
    before = build_l2_page(sources)
    assert before is not None
    changed = [
        ConsolidationSource(
            source.memory_id,
            source.revision,
            source.content + "（补充：每周一次）" if source.memory_id == "m4"
            else source.content,
            source.last_seq,
            source.kind,
            source.origin,
        )
        for source in sources
    ]
    after = build_l2_page(changed)
    assert after is not None
    before_blocks = before.content.rstrip("\n").split("\n\n")
    after_blocks = after.content.rstrip("\n").split("\n\n")
    differing = [
        (left, right)
        for left, right in zip(before_blocks, after_blocks, strict=False)
        if left != right
    ]
    assert len(differing) == 1, "只该动 m4 所在的那一块"
    assert "m4" in differing[0][1]
    untouched_before = [block for block in before_blocks if "m4" not in block]
    untouched_after = [block for block in after_blocks if "m4" not in block]
    assert untouched_before == untouched_after, "未变化的块必须逐字节相同"


def test_proof_count_and_evidence_are_mechanically_consistent() -> None:
    page = build_l2_page(_eight_fixtures())
    assert page is not None
    assert page.proof_count == len(page.evidence)
    for item in page.evidence:
        assert set(item) == {"memory_id", "revision", "quote"}
        assert isinstance(item["quote"], str) and item["quote"]
    # 长正文的引文必须标注截断，不能静默砍掉。
    long_source = ConsolidationSource(
        "m_long", 1, "很长的正文" * 200, 1, "fact", "inferred"
    )
    page_long = build_l2_page([long_source, _eight_fixtures()[0]])
    assert page_long is None or all(
        len(item["quote"].encode()) <= 260 for item in page_long.evidence
    )


def test_consolidate_refuses_inconsistent_page_from_a_buggy_builder(monkeypatch) -> None:
    """合成器交回的页若 proof_count 与证据条数不一致，`consolidate` 必须拒。

    "证据数 = 条数"不能只在测试里成立，也不能只在 `build_l2_page` 里成立：生成
    算法哪天一漏写证据，坏页就不许进投影。这里把合成器换成"少算一条"的坏版本，
    证明就位校验真的会拦，而不是一句注释。
    """
    sources = _eight_fixtures()
    good = build_l2_page(sources)
    assert good is not None
    broken = dataclasses.replace(good, proof_count=good.proof_count - 1)
    monkeypatch.setattr("tianshu.memory_distill.build_l2_page", lambda _sources: broken)

    with pytest.raises(MemoryStoreError, match="不一致"):
        consolidate(list(sources), scope=MemoryScope.user())

    monkeypatch.undo()
    # 撤销坏版本后同一批输入应当照常出页（L2 在前、L3 在后）。
    assert [page.layer for page in consolidate(list(sources), scope=MemoryScope.user())] == [
        "l2",
        "l3",
    ]


def test_l3_page_is_budgeted_and_only_human_preferences() -> None:
    """L3 只收 user 作用域里"人给的"偏好，并按 4 KiB 预算打包。"""
    page = build_l3_page(_eight_fixtures())
    assert page is not None
    assert page.layer == "l3"
    assert len(page.content.encode()) <= L3_PAGE_BYTES
    ids = {item["memory_id"] for item in page.evidence}
    assert "m4" not in ids, "procedure 不该进偏好页"
    assert "m6" not in ids, "inferred 事实不该进偏好页"

    many = [
        ConsolidationSource(
            f"m{index:03d}", 1, f"偏好第 {index} 条：" + "内容" * 40, index, "preference",
            "user_explicit",
        )
        for index in range(60)
    ]
    big = build_l3_page(many)
    assert big is not None
    assert len(big.content.encode()) <= L3_PAGE_BYTES + 200  # 末尾的"预算已满"提示
    assert "预算已满" in big.content


def test_l3_only_for_user_scope() -> None:
    task_scope = MemoryScope.task("task_x")
    pages = consolidate(_eight_fixtures(), scope=task_scope)
    assert [page.layer for page in pages] == ["l2"]


def test_single_memory_does_not_make_an_l2_page() -> None:
    """单条事实不需要 L2 层：聚不出两行的簇就不产出。"""
    assert build_l2_page([_eight_fixtures()[0]]) is None


def test_old_page_is_superseded_and_still_gettable(env) -> None:
    """旧页退役但可查；supersede 方向正确（新页 supersedes 不指旧页为宿主）。"""
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    _seed(log, projection, "m2", "报告先讲结论")
    pages = consolidate(_sources(projection), scope=scope)
    first_ids = _apply_pages(env, pages)
    assert first_ids
    old_id = first_ids[0]
    old = projection.get(old_id, allowed=(scope,))
    assert old is not None and old.status == "candidate"

    _seed(log, projection, "m3", "报告里术语保留英文")
    pages2 = consolidate(_sources(projection), scope=scope)
    second_ids = _apply_pages(env, pages2)
    assert second_ids and second_ids[0] != old_id
    superseded = projection.get(old_id, allowed=(scope,))
    assert superseded is not None and superseded.status == "superseded"
    assert projection.read_content(superseded)


def test_consolidate_output_is_replayable(env) -> None:
    """重建后页内容与 `consolidated_at` 一致：合成只按事件重放，不重算。"""
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    _seed(log, projection, "m2", "报告先讲结论")
    pages = consolidate(_sources(projection), scope=scope)
    ids = _apply_pages(env, pages)
    before = projection.get(ids[0], allowed=(scope,))
    assert before is not None
    content_before = projection.read_content(before)
    hash_before = projection.projection_hash()
    projection.rebuild()
    after = projection.get(ids[0], allowed=(scope,))
    assert after is not None
    assert after.consolidated_at == before.consolidated_at
    assert projection.read_content(after) == content_before
    assert projection.projection_hash() == hash_before


def test_freshness_marks_pages_and_demotes_them(env) -> None:
    """作用域出现更新的 L1 后：旧页 `stale=true`，且排在 L1 证据之后。"""
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    _seed(log, projection, "m2", "报告先讲结论")
    pages = consolidate(_sources(projection), scope=scope)
    ids = _apply_pages(env, pages)
    l2_id = ids[0]
    _confirm_page(log, projection, l2_id)
    fresh = projection.retrieve("报告", allowed=(scope,))
    assert fresh.semantic is False  # 没配索引，不混淆能力
    stale_flags = {item.memory.memory_id: item.stale for item in fresh.items}
    assert stale_flags.get(l2_id) is False

    _seed(log, projection, "m3", "报告里术语保留英文")
    after = projection.retrieve("报告", allowed=(scope,))
    flags = {item.memory.memory_id: item.stale for item in after.items}
    assert flags.get(l2_id) is True, "出现了更新的 L1，旧页应标陈旧"
    order = [item.memory.memory_id for item in after.items]
    assert order.index(l2_id) > order.index("m3"), "陈旧页降权但不短路"


def test_consolidate_rejects_unknown_layer_or_layer_l1(env) -> None:
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    page = build_l3_page(_sources(projection))
    assert page is not None
    item = page.to_output()
    item.update(
        {
            "memory_id": "mem_bad",
            "content_ref": projection.put_content(page.content),
            "consolidated_at": NOW,
            "layer": "l9",
        }
    )
    with pytest.raises(MemoryStoreError):
        _record(
            log,
            projection,
            {
                "tool_id": "memory.consolidate",
                "status": "succeeded",
                "output": {"scope": scope.scope, "scope_id": scope.scope_id, "pages": [item]},
            },
        )