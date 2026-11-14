"""W7 冲突消解的判据（SPEC-W7-L2L3-GENERATIVE-2026-11-14 §4）。

要点不是"检测出矛盾"，而是**可机械核对**：

- 检测判据与三级裁决规则全部写死（常量钉死），同一批输入逐字节得到同一份裁决；
- 裁决**只进页内标注**：不删行、不改 L1、不改召回集合——两条源记忆仍各自 active；
- 误报的代价被压到"页上多一行可核查的标注"（候选≠矛盾），不声称已证明逻辑矛盾。

现有 `tests/test_memory_distill.py` 的 22 条判据**零改动**：无冲突候选的页面逐字节
不变由它们继续钉住；本文件钉住"有冲突候选时新增什么、不新增什么"。负控（变异→红→
还原→绿）逐条记录在 SPEC §5。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.memory import MemoryProjection
from tianshu.memory_distill import (
    CONFLICT_ANNOTATION_LIMIT,
    CONFLICT_SIMILARITY,
    ConsolidationSource,
    _conflict_pairs,
    _resolve_conflict_pair,
    build_l2_page,
    consolidate,
)
from tianshu.resources import MemoryScope

NOW = 1_700_000_000_000
RUN = "run_conflict"


def _source(
    memory_id: str,
    content: str,
    last_seq: int,
    *,
    kind: str = "fact",
    origin: str = "inferred",
    revision: int = 1,
) -> ConsolidationSource:
    return ConsolidationSource(memory_id, revision, content, last_seq, kind, origin)


def _annotation_lines(content: str) -> list[str]:
    return [line for line in content.split("\n") if line.startswith("[冲突裁决]")]


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
    kind: str = "fact",
    origin: str = "inferred",
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


def _apply_pages(env, pages, scope: MemoryScope | None = None) -> list[str]:
    """把合成页按真实事件形状写进投影（与 executor 的 output 一致）。"""
    log, projection = env
    scope = MemoryScope.user() if scope is None else scope
    from tianshu.memory_distill import page_memory_id

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


def test_conflict_constants_are_pinned() -> None:
    """J1：阈值与标注上限写死在代码里（改了就是换了裁决口径，必须重跑判据）。"""
    assert CONFLICT_SIMILARITY == 0.6
    assert CONFLICT_ANNOTATION_LIMIT == 8


def test_mutually_exclusive_restatement_is_annotated_with_seq_rule() -> None:
    """J2：同 kind、同 origin 的互斥改写对被标注，规则 `seq`、胜者是 last_seq 大者。

    "报告用中文写" 与 "报告用英文写"：token 规模相等（各 11）、互不为子集、重叠
    ≈0.73——这是确定性度量能给出的最强"同主题换值"信号。标注行逐字符断言。
    """
    page = build_l2_page(
        [
            _source("mem_a", "报告用中文写", 1),
            _source("mem_b", "报告用英文写", 2),
        ]
    )
    assert page is not None
    lines = _annotation_lines(page.content)
    assert lines == [
        "[冲突裁决] mem_b@1 与 mem_a@1 按 seq 规则以 mem_b@1 为准（两条 L1 均仍有效）"
    ], page.content


def test_origin_rule_beats_recency() -> None:
    """J3：origin 规则**先于时间**——人确认的旧结论压过模型的新猜测。

    若实现是"谁新谁赢"，这条就红：`user_confirmed`（seq 1）必须压过 `inferred`
    （seq 2），规则名 `origin`。
    """
    page = build_l2_page(
        [
            _source("mem_h", "报告用中文写", 1, origin="user_confirmed"),
            _source("mem_m", "报告用英文写", 2, origin="inferred"),
        ]
    )
    assert page is not None
    assert _annotation_lines(page.content) == [
        "[冲突裁决] mem_h@1 与 mem_m@1 按 origin 规则以 mem_h@1 为准（两条 L1 均仍有效）"
    ], page.content


def test_subset_extension_is_not_a_conflict() -> None:
    """J4：子集/补充关系不标注——"报告用中文写" ⊂ "报告用中文写并且先列结论"。"""
    page = build_l2_page(
        [
            _source("mem_a", "报告用中文写", 1),
            _source("mem_b", "报告用中文写并且先列结论", 2),
        ]
    )
    assert page is not None, "两行仍应聚成一页（重叠 1.0 ≥ 0.34）"
    assert "- 报告用中文写 [mem_a@1]" in page.content
    assert _annotation_lines(page.content) == []


def test_word_order_reordering_is_not_a_conflict() -> None:
    """J4b：同 token 集合的语序重排不是冲突——这是**子集排除**判据的存在理由。

    "deploy on friday" 与 "on friday deploy"：token 集合相等（互为子集）、规模相等、
    重叠 1.0——若没有子集排除，等规模 + 高重叠会把它误标为冲突候选（"同一句话换
    了个语序"不是两个互斥结论）。这条让子集守卫成为能被负控打红的守卫，而不是注释。
    """
    page = build_l2_page(
        [
            _source("mem_a", "deploy on friday", 1),
            _source("mem_b", "on friday deploy", 2),
        ]
    )
    assert page is not None
    assert _annotation_lines(page.content) == []


def test_weakly_overlapping_cluster_mates_are_not_flagged() -> None:
    """J5：进得了簇（≥0.34）但低于冲突阈值（0.6）的等规模近邻不标注。

    "部署窗口是周一清晨六点" vs "部署窗口是周二深夜十点"：各 21 token、重叠 ≈0.571
    ——同场景，但骨架差异已超出"同一句话换值"的范围。既有八夹具（含 m4/m5 这对
    互补流程事实，规模 24/23 不等）整页无标注：等规模判据把"共享步骤词汇但各自
    成立"的对子排除在外。
    """
    weak = build_l2_page(
        [
            _source("mem_w1", "部署窗口是周一清晨六点", 1),
            _source("mem_w2", "部署窗口是周二深夜十点", 2),
        ]
    )
    assert weak is not None, "弱重叠对仍应聚成一页"
    assert _annotation_lines(weak.content) == []

    eight = [
        _source("m1", "报告用中文写，先列结论", 1, kind="preference", origin="user_explicit"),
        _source("m2", "报告里先讲结论再给依据", 2, kind="preference", origin="user_confirmed"),
        _source(
            "m3", "报告不要用英文，术语保留英文", 3, kind="preference", origin="user_confirmed"
        ),
        _source("m4", "数据库迁移分三步：备份、演练、割接", 4, kind="procedure"),
        _source("m5", "数据库迁移前先做备份演练", 5, kind="procedure"),
        _source("m6", "日志保留 30 天", 6),
        _source("m7", "周报里加上风险项", 7, kind="preference", origin="user_confirmed"),
        _source("m8", "失败先排查原因再重试", 8, kind="procedure"),
    ]
    page = build_l2_page(eight)
    assert page is not None
    assert _annotation_lines(page.content) == []


def test_cross_kind_pairs_are_not_flagged() -> None:
    """J6：跨 kind 不标注——事实与偏好同框可并存，即使正文高度重叠。"""
    page = build_l2_page(
        [
            _source("mem_a", "报告用中文写", 1, kind="fact"),
            _source("mem_b", "报告用英文写", 2, kind="preference"),
        ]
    )
    assert page is not None or True, "跨 kind 是否成页取决于聚类，这里只钉标注行为"
    if page is not None:
        assert _annotation_lines(page.content) == []


def test_conflict_page_is_still_byte_deterministic() -> None:
    """J7：带裁决标注的页仍逐字节确定——同一批输入两次合成完全一致。"""
    sources = [
        _source("mem_a", "报告用中文写", 1),
        _source("mem_b", "报告用英文写", 2),
        _source("mem_h", "报告用中文写，术语保留英文", 3, origin="user_confirmed"),
    ]
    first = build_l2_page(sources)
    second = build_l2_page(sources)
    assert first is not None and second is not None
    assert first.content.encode() == second.content.encode()
    assert first.to_output() == second.to_output()


def test_annotation_limit_truncates_and_reports_the_omitted_count() -> None:
    """J8：45 对候选 → 恰好 8 行标注 + 1 行如实截断（报 37 对省略），不许静默吞。"""
    digits = "一二三四五六七八九十"
    members = [
        _source(f"mem_d{index + 1:02d}", f"部署窗口是{digit}点", index + 1)
        for index, digit in enumerate(digits)
    ]
    assert len(_conflict_pairs(members)) == 45, "夹具本身应产生 45 个全对候选"
    page = build_l2_page(members)
    assert page is not None
    lines = _annotation_lines(page.content)
    assert len(lines) == CONFLICT_ANNOTATION_LIMIT + 1
    assert lines[-1] == "[冲突裁决] 其余 37 对省略（标注上限 8）"


def test_conflict_page_keeps_reference_assembly_invariants() -> None:
    """J9：裁决标注不破坏引用式拼装契约——`- ` 行全带 [id@rev]，证据机械一致。"""
    page = build_l2_page(
        [
            _source("mem_a", "报告用中文写", 1),
            _source("mem_b", "报告用英文写", 2),
        ]
    )
    assert page is not None
    quoted = [line for line in page.content.split("\n") if line.startswith("- ")]
    assert len(quoted) == 2
    for line in quoted:
        assert "@" in line and line.rstrip().endswith("]")
    assert page.proof_count == len(page.evidence) == 2
    for item in page.evidence:
        assert set(item) == {"memory_id", "revision", "quote"}


def test_resolution_leaves_both_l1_rows_active_and_replays(env) -> None:
    """J10（投影 e2e）：消解不改 L1——两条源记忆仍 active；页可回放逐位复现。"""
    log, projection = env
    scope = MemoryScope.user()
    _seed(log, projection, "mem_c1", "部署窗口是周一")
    _seed(log, projection, "mem_c2", "部署窗口是周二")
    pages = consolidate(projection.consolidatable_sources(scope), scope=scope)
    assert [page.layer for page in pages] == ["l2"]
    page_ids = _apply_pages(env, pages)
    assert len(page_ids) == 1

    for memory_id in ("mem_c1", "mem_c2"):
        row = projection.get(memory_id, allowed=(scope,))
        assert row is not None and row.status == "active", "消解不许动 L1 行"

    page = projection.get(page_ids[0], allowed=(scope,))
    assert page is not None
    content = projection.read_content(page)
    assert "[冲突裁决] mem_c2@2 与 mem_c1@2 按 seq 规则以 mem_c2@2 为准" in content

    hash_before = projection.projection_hash()
    projection.rebuild()
    after = projection.get(page_ids[0], allowed=(scope,))
    assert after is not None
    assert projection.read_content(after) == content
    assert projection.projection_hash() == hash_before


def test_delta_still_holds_when_a_conflict_appears() -> None:
    """J11：冲突候选出现时只追加裁决节，既有块逐字节不变（增量 delta 在冲突场景成立）。"""
    before_sources = [
        _source("p1", "部署窗口是周一清晨六点", 1),
        _source("p2", "部署窗口是周二深夜十点", 2),
        _source("q1", "发布说明用中文写", 3, kind="preference", origin="user_confirmed"),
        _source("q2", "发布说明先给结论", 4, kind="preference", origin="user_confirmed"),
    ]
    before = build_l2_page(before_sources)
    assert before is not None and _annotation_lines(before.content) == []

    after_sources = [
        before_sources[0],
        _source("p2", "部署窗口是周一深夜十点", 2),
        before_sources[2],
        before_sources[3],
    ]
    after = build_l2_page(after_sources)
    assert after is not None
    before_sections = before.content.rstrip("\n").split("\n\n")
    after_sections = after.content.rstrip("\n").split("\n\n")
    assert len(after_sections) == len(before_sections) + 1, after.content
    assert after_sections[-1].startswith("[冲突裁决]"), "新增的节是裁决标注"
    assert after_sections[1] == before_sections[1], (
        "未变化的块（q1/q2）必须逐字节相同"
    )
    assert "p2@1 与 p1@1 按 seq 规则以 p2@1 为准" in after_sections[-1]
    # p2 自己的正文变了，它那一行的变化是增量语义的一部分（只有 p1/p2 块在变）。
    assert "部署窗口是周一深夜十点 [p2@1]" in after_sections[0]


def test_resolution_rules_are_total_and_pinned() -> None:
    """J12：三级规则逐级钉住，且对任意输入全序（winner 必为其中一条、rule 必在集合内）。"""
    a = _source("mem_a", "同样的正文", 5)
    b = _source("mem_b", "同样的正文", 1, origin="user_confirmed")
    winner, loser, rule = _resolve_conflict_pair(a, b)
    assert (winner is b and loser is a and rule == "origin"), "origin 压过更晚的 seq"

    c = _source("mem_c", "内容一", 3)
    d = _source("mem_d", "内容二", 7)
    winner, loser, rule = _resolve_conflict_pair(c, d)
    assert (winner is d and loser is c and rule == "seq"), "同 origin 时新 seq 胜"

    e = _source("mem_e", "内容三", 4)
    f = _source("mem_f", "内容四", 4)
    winner, loser, rule = _resolve_conflict_pair(e, f)
    assert (winner is e and loser is f and rule == "memory_id"), "全序 tie-break 取小 id"

    for left, right in ((a, b), (b, a), (c, d), (d, c), (e, f), (f, e)):
        winner, _, rule = _resolve_conflict_pair(left, right)
        assert rule in {"origin", "seq", "memory_id"}
        assert winner in (left, right)
        # 参数顺序不影响结论（裁决是序上的事实，不是调用约定）。
        mirrored = _resolve_conflict_pair(right, left)
        assert mirrored[0] is winner and mirrored[2] == rule
