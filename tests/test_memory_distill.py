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
    expires_at: int | None = None,
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
                "expires_at": expires_at,
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
    # "保留上一版 + 它当时的证据"是这条判据的另一半：只留住正文不算留住结论。
    assert [item["memory_id"] for item in superseded.evidence] == [
        item["memory_id"] for item in old.evidence
    ]
    assert tuple(superseded.source_refs) == tuple(old.source_refs)


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


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("layer", "l9", "未知合成层"),
        ("layer", "l1", "合成只产出 l2/l3，l1 会与原子事实混淆"),
        ("source_refs", [], "没有来源的合成页无法核对，必须拒"),
    ],
)
def test_consolidate_rejects_bad_pages(env, field: str, value: object, why: str) -> None:
    """投影应用层的坏页校验：未知 layer / l1 / 空来源都必须拒。

    就位校验（`consolidate()`）只防得住**生成器**的 bug；这一段防的是"事件里
    塞进来的坏页"——两者不是同一个入口，得分别有用例（复审指出原名只测了
    未知 layer，`l1` 与空 `source_refs` 两个分支没有回归）。
    """
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
        }
    )
    item[field] = value
    with pytest.raises(MemoryStoreError, match="|".join(["layer", "来源", "不能是"])):
        _record(
            log,
            projection,
            {
                "tool_id": "memory.consolidate",
                "status": "succeeded",
                "output": {"scope": scope.scope, "scope_id": scope.scope_id, "pages": [item]},
            },
        )
    assert projection.get("mem_bad", allowed=(scope,)) is None, "坏页一个字节都不该进投影"

def test_retrieve_demotes_stale_pages_even_without_a_vector_index(env) -> None:
    """默认检索路径（没有向量索引）也必须把陈旧页排在新鲜 L1 之后。

    复审复现过的真洞：`retrieve()` 的"无索引 / 索引不可用"分支只调
    `_order_by_priority(...)`，而陈旧标记只在融合路的分支里进了排序键——于是
    生产默认路径上陈旧 L2 页照样排第一。这里让页与 L1 **同为 `inferred`、
    同一 created_at**，把 origin 权重的掩盖去掉：只有 stale 这一档能把页排到
    后面。
    """
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "数据库迁移分三步：备份、演练、割接", kind="fact",
          origin="inferred")
    _seed(log, projection, "m2", "数据库迁移前先做备份演练", kind="fact",
          origin="inferred")
    pages = consolidate(_sources(projection), scope=scope)
    ids = _apply_pages(env, pages)
    l2_id = ids[0]
    _confirm_page(log, projection, l2_id)
    # 页写完之后又出现一条更新的 L1：页从此陈旧。
    _seed(log, projection, "m3", "数据库迁移要写回滚方案", kind="fact", origin="inferred")

    outcome = projection.retrieve("数据库迁移", allowed=(scope,))
    assert outcome.semantic is False, "没配索引，这次只有词面路"
    flags = {item.memory.memory_id: item.stale for item in outcome.items}
    assert flags.get(l2_id) is True
    order = [item.memory.memory_id for item in outcome.items]
    assert l2_id in order and "m3" in order
    assert order.index(l2_id) > order.index("m3"), (
        f"陈旧页必须排在新鲜 L1 之后（实际顺序：{order}）"
    )


def test_unknown_freshness_stays_unknown_and_is_not_reported_as_fresh(env) -> None:
    """旧页没有 `seq:` 标记 ⇒ `stale` 是 `None`（不可判定），不是 `False`。

    把"判不了"压成"新鲜"就是谎报：模型/UI 会以为这页跟得上最新事实。三态里
    `None` 必须能透出去（`Recalled.stale`），并在主循环的输出里**省略**这个字段
    而不是写 `false`。
    """
    log, projection = env
    scope = MemoryScope.user()
    # 用能聚成 L2 的一对（"报告…" 那对的重叠系数只有 0.33，聚不出来）。
    _seed(log, projection, "m1", "数据库迁移分三步：备份、演练、割接", kind="fact",
          origin="inferred")
    _seed(log, projection, "m2", "数据库迁移前先做备份演练", kind="fact", origin="inferred")
    pages = consolidate(_sources(projection), scope=scope)
    l2 = [page for page in pages if page.layer == "l2"][0]
    # 模拟 N3 之前的旧页：正文/证据都在，但没有 `seq:` 基准。
    legacy = dataclasses.replace(
        l2, source_refs=tuple(ref for ref in l2.source_refs if not ref.startswith("seq:"))
    )
    ids = _apply_pages(env, [legacy])
    _confirm_page(log, projection, ids[0])

    outcome = projection.retrieve("数据库迁移", allowed=(scope,))
    flags = {item.memory.memory_id: item.stale for item in outcome.items}
    assert flags.get(ids[0]) is None, "没有 seq: 的旧页必须报不可判定"


def test_reapplying_the_same_page_is_idempotent(env) -> None:
    """同一页事件应用两次：不新增行、状态与 revision 都不动。

    "页 id 由 (scope, layer, content) 决定"的设计要靠这条兜住——否则重复
    `consolidate` 会不断长行，或者把候选页的状态改来改去。
    """
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    _seed(log, projection, "m2", "报告先讲结论")
    pages = consolidate(_sources(projection), scope=scope)
    first = _apply_pages(env, pages)
    before = projection.get(first[0], allowed=(scope,))
    assert before is not None
    rows_before = len(projection.list_memories(allowed=(scope,), limit=100))
    same = _apply_pages(env, pages)
    assert same == first
    after = projection.get(first[0], allowed=(scope,))
    assert after is not None
    assert after.status == before.status == "candidate"
    assert (after.revision, after.content_ref) == (before.revision, before.content_ref)
    assert len(projection.list_memories(allowed=(scope,), limit=100)) == rows_before


def test_confirmed_page_is_not_retired_by_a_later_consolidation(env) -> None:
    """人 confirm 过的页不因新合成退役：模型可见的 consolidate 不能撤销人的召回授权。

    `memory.consolidate` 是模型可见、免审批的工具；若它能顺手把 active 页置成
    superseded，模型就成了"能删掉人确认过的页面"的角色（复审提出的设计边界）。
    现在只有 candidate 页会被新页取代，active 页与新页并存、由 freshness 决定
    谁排前面；人确认的页只有人能退（forget / erase）。
    """
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    _seed(log, projection, "m2", "报告先讲结论")
    pages = consolidate(_sources(projection), scope=scope)
    confirmed_id = _apply_pages(env, pages)[0]
    _confirm_page(log, projection, confirmed_id)

    _seed(log, projection, "m3", "报告里术语保留英文")
    newer = consolidate(_sources(projection), scope=scope)
    newer_ids = _apply_pages(env, newer)
    assert newer_ids and newer_ids[0] != confirmed_id

    still = projection.get(confirmed_id, allowed=(scope,))
    assert still is not None and still.status == "active", "人确认的页不许被合成退役"
    candidate = projection.get(newer_ids[0], allowed=(scope,))
    assert candidate is not None and candidate.status == "candidate"


def test_reverting_to_an_old_page_body_fails_closed(env) -> None:
    """正文回退到旧版会撞回同一个页 id：已退役的页 id 不许静默"再当候选"。

    页 id 由内容决定，所以"删掉 m3 再合成"会重新算出第一次那个 id。若此时旧行
    已是 superseded，静默 `return` 会让事件说"写了候选页"、投影却停在 superseded
    （人 confirm 直接失败）。悄悄复活又会改写已确认的结论——两条都不诚实，所以
    明确报错，由人决定是 forget 旧行还是换作用域重来。
    """
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "报告用中文写")
    _seed(log, projection, "m2", "报告先讲结论")
    first_pages = consolidate(_sources(projection), scope=scope)
    first_id = _apply_pages(env, first_pages)[0]
    # 旧页被新页取代（新页正文不同 ⇒ 新 id），旧行变 superseded。
    _seed(log, projection, "m3", "报告里术语保留英文")
    _apply_pages(env, consolidate(_sources(projection), scope=scope))
    old = projection.get(first_id, allowed=(scope,))
    assert old is not None and old.status == "superseded"

    with pytest.raises(MemoryStoreError, match="同一段正文|状态"):
        _apply_pages(
            env, [dataclasses.replace(page, content=page.content) for page in first_pages]
        )


def test_expired_l1_does_not_age_a_page(env) -> None:
    """过期 L1 不算"更新的证据"：它不会被合成采纳、也不会被召回。

    `_stale_map` 的"作用域内最大 L1 `last_seq`"若把已过期的行算进去，一条早就
    失效的临时事实会把页误标陈旧（复审的轻微项）。这里造一条 `expires_at <= now`
    且 `last_seq` 更新的 L1：页必须仍是新鲜。
    """
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "数据库迁移分三步：备份、演练、割接", kind="fact",
          origin="inferred")
    _seed(log, projection, "m2", "数据库迁移前先做备份演练", kind="fact", origin="inferred")
    pages = consolidate(_sources(projection), scope=scope)
    page_id = _apply_pages(env, pages)[0]
    _confirm_page(log, projection, page_id)
    # 页写完之后来一条**已过期**的更新 L1（NOW + 1 已过）。
    _seed(
        log,
        projection,
        "m_exp",
        "数据库迁移明天改到凌晨",
        kind="fact",
        origin="inferred",
        expires_at=NOW - 1,
    )
    outcome = projection.retrieve("数据库迁移", allowed=(scope,))
    flags = {item.memory.memory_id: item.stale for item in outcome.items}
    assert flags.get(page_id) is False, "过期 L1 不该把页判陈旧"
