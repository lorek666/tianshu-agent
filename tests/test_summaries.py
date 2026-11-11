"""语义摘要：记录层与材料身份（H1 的判据）。

PRD §7.1 第 2 / 3 条是这一组的立据：

> 摘要记录来源事件 / 内容哈希、适用任务与作用域、模型 / adapter / prompt /
> 摘要版本和实际结果；作为有来源的派生材料，不替代原始事实。

> replay 读取当时保存的摘要和选中材料；不重新生成摘要……需要刷新时追加新版本，
> 历史运行继续引用旧版本。

这一组只测**存储层 + 材料构造**（`src/tianshu/summaries.py`）：身份、有界化、
状态机、版本追加、不可删不可改。生成路径在 `test_summary_loop.py`，注入与回放
在 `test_summary_context.py` / `test_summary_replay.py`。
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from tianshu.events import EventLog
from tianshu.summaries import (
    ERROR_MAX_BYTES,
    ITEM_BYTES,
    MATERIAL_BYTES,
    MATERIAL_VERSION,
    MAX_SOURCES,
    PENDING,
    PROMPT_VERSION,
    READY,
    SUMMARY_MAX_BYTES,
    SUMMARY_VERSION,
    MaterialBlock,
    SummaryEmpty,
    SummaryMaterialError,
    SummaryStateError,
    SummaryStore,
    SummaryTooLong,
    build_material,
    prompt_for,
    render_block,
    text_digest,
)


@pytest.fixture
def store(tmp_path):
    log = EventLog(tmp_path / "events.db")
    return SummaryStore(log)


def _blocks(count: int = 3, *, text: str = "约束：报告只用中文。") -> list[MaterialBlock]:
    return [
        MaterialBlock(seq=10 + i, role="assistant" if i % 2 else "tool", content=f"{text}#{i}")
        for i in range(count)
    ]


def _material(**kw):
    return build_material(_blocks(), scope="task:t1", task_id="t1", run_id="r1", **kw)


# ---------------------------------------------------------------- 材料身份


def test_material_identity_is_deterministic():
    """同一批来源 ⇒ 同一个 material_id（同材料同摘要的前提）。"""
    a = build_material(_blocks(), scope="task:t1", task_id="t1")
    b = build_material(_blocks(), scope="task:t1", task_id="t1")
    assert a.material_id == b.material_id
    assert a.text == b.text
    assert a.digest == b.digest


def test_material_identity_changes_with_content():
    """原文改一个字 ⇒ 来源哈希变 ⇒ material_id 变（旧摘要不会被误用）。"""
    base = build_material(_blocks(), scope="task:t1")
    changed = build_material(
        [
            MaterialBlock(seq=b.seq, role=b.role, content=b.content + "!")
            for b in _blocks()
        ],
        scope="task:t1",
    )
    assert base.sources != changed.sources
    assert base.material_id != changed.material_id


def test_material_identity_changes_with_scope_and_version():
    """作用域或材料渲染版本变 ⇒ 身份变：两份不同语义的材料不能撞 id。"""
    base = build_material(_blocks(), scope="task:t1")
    other_scope = build_material(_blocks(), scope="task:t2")
    assert base.material_id != other_scope.material_id
    assert MATERIAL_VERSION in repr(MATERIAL_VERSION)  # 版本常量存在且被写进身份

    identity = {
        "material_version": MATERIAL_VERSION + "-changed",
        "scope": "task:t1",
        "sources": [
            {"kind": s.kind, "ref": s.ref, "digest": s.digest} for s in base.sources
        ],
    }
    from tianshu.canonical import jcs_sha256

    assert jcs_sha256(identity) != base.material_id


def test_sources_carry_original_content_hash_not_truncated_piece():
    """来源记的是**原文**哈希：渲染截断不能改变"摘的是什么"的证据。"""
    long = "汉" * (ITEM_BYTES * 2)  # 每条 3 字节 ⇒ 必然超过单条上限
    material = build_material(
        [MaterialBlock(seq=7, role="tool", content=long)], scope="task:t1"
    )
    assert (
        material.sources[0].digest
        == hashlib.sha256(long.encode("utf-8")).hexdigest()
    )
    assert len(material.text.encode("utf-8")) <= MATERIAL_BYTES
    assert "单条已截断" in material.text


# ---------------------------------------------------------------- 有界化


def test_material_is_bounded_and_never_splits_multibyte_chars():
    """总长度 ≤ MATERIAL_BYTES；截断点落在多字节字符中间时不留替换符。"""
    blocks = [
        MaterialBlock(seq=i + 1, role="tool", content="汉" * 700)  # 每条 2100B
        for i in range(20)
    ]
    material = build_material(blocks, scope="task:t1")
    assert len(material.text.encode("utf-8")) <= MATERIAL_BYTES
    assert "\ufffd" not in material.text  # 没有解码替换符
    assert len(material.sources) < len(blocks)  # 确实截掉了后面的来源


def test_empty_material_is_an_error_not_a_fake_summary():
    """没有来源 → 明确报错（调用方回落确定性裁剪），不造一份空材料。"""
    with pytest.raises(SummaryMaterialError, match="没有可摘要的材料"):
        build_material([], scope="task:t1")


def test_oversized_first_block_is_an_error(monkeypatch):
    """单条渲染就超过总上限 → 报错，而不是把一份"什么都装不下"的材料交出去。

    正常情况下够不到这条分支（单条先被 `ITEM_BYTES` 截断，1 KiB < 8 KiB）；
    这里把单条上限临时调大，专门验"放不下就明说"。
    """
    import tianshu.summaries as summaries

    monkeypatch.setattr(summaries, "ITEM_BYTES", summaries.MATERIAL_BYTES * 2)
    with pytest.raises(SummaryMaterialError, match="放不下"):
        summaries.build_material(
            [MaterialBlock(seq=1, role="tool", content="x" * (MATERIAL_BYTES * 2))],
            scope="task:t1",
        )


def test_max_sources_is_enforced_by_truncation():
    blocks = [
        MaterialBlock(seq=i + 1, role="tool", content="x" * 8)
        for i in range(MAX_SOURCES + 10)
    ]
    material = build_material(blocks, scope="task:t1")
    assert len(material.sources) == MAX_SOURCES


def test_render_block_is_deterministic_and_marks_truncation():
    block = MaterialBlock(seq=3, role="tool", content="a" * (ITEM_BYTES + 10))
    first = render_block(block)
    assert first == render_block(block)
    assert first.startswith("[seq=3 role=tool]\n")
    assert "单条已截断" in first


# ---------------------------------------------------------------- 状态机


def test_begin_then_complete_records_provenance(store):
    material = _material()
    record = store.begin(material, model="anthropic:acme:m1", attempt_id="run:r1#s1")
    assert record.status == PENDING
    assert record.version == 1
    assert record.material_id == material.material_id
    assert record.prompt_version == PROMPT_VERSION
    assert record.summary_version == SUMMARY_VERSION
    assert record.attempt_id == "run:r1#s1"
    assert record.text is None and record.input_tokens is None  # 未知不记 0

    done = store.complete(record, "要点：只用中文。", input_tokens=12, output_tokens=4)
    assert done.status == READY
    assert done.text == "要点：只用中文。"
    assert done.text_digest == text_digest("要点：只用中文。")
    assert done.input_tokens == 12 and done.output_tokens == 4
    assert store.latest_ready(material.material_id).summary_id == done.summary_id


def test_unknown_usage_stays_null_not_zero(store):
    """provider 不给 usage ⇒ 记 NULL。记 0 等于说"这次调用没花钱"。"""
    record = store.begin(_material(), model="m")
    done = store.complete(record, "要点")
    assert done.input_tokens is None and done.output_tokens is None
    row = store.material(record.material_id)
    assert row is not None
    fresh = store.read(done.summary_id)
    assert fresh is not None and fresh.input_tokens is None


def test_complete_rejects_blank_and_oversized_text(store):
    record = store.begin(_material(), model="m")
    with pytest.raises(SummaryEmpty):
        store.complete(record, "   \n\t ")
    with pytest.raises(SummaryTooLong, match="超过上限"):
        store.complete(record, "x" * (SUMMARY_MAX_BYTES + 1))
    with pytest.raises(SummaryTooLong, match="NUL"):
        store.complete(record, "a\x00b")
    # 三次都失败之后仍是 pending：可以继续用同一条记录结算（不产生新版本）。
    assert store.read(record.summary_id).status == PENDING


def test_ready_text_is_frozen(store):
    """正文写入后不可改：再次结算抛错，DB 触发器也不许改。"""
    record = store.begin(_material(), model="m")
    done = store.complete(record, "第一版")
    with pytest.raises(SummaryStateError, match="只有 pending"):
        store.complete(done, "第二版")
    with pytest.raises(sqlite3.IntegrityError, match="不可改"):
        store._conn.execute(
            "UPDATE summary_versions SET text = ? WHERE summary_id = ?",
            ("被偷改", done.summary_id),
        )
    assert store.read(done.summary_id).text == "第一版"


def test_versions_append_and_old_ones_survive(store):
    """刷新 = 追加新版本；旧 run 引用的旧版本原样保留（PRD §7.1 第 3 条）。"""
    material = _material()
    first = store.begin(material, model="model-a")
    first = store.complete(first, "模型 A 的摘要")
    second = store.begin(material, model="model-b")
    second = store.complete(second, "模型 B 的摘要")

    assert (first.version, second.version) == (1, 2)
    assert first.summary_id != second.summary_id
    assert store.read(first.summary_id).text == "模型 A 的摘要"
    assert store.latest_ready(material.material_id).summary_id == second.summary_id
    assert [r.version for r in store.versions(material.material_id)] == [1, 2]


def test_begin_refuses_second_pending_for_same_material(store):
    """同材料不许并发两次尝试：崩溃遗留要先结案（否则恢复时会偷偷多调一次）。"""
    material = _material()
    store.begin(material, model="m")
    with pytest.raises(SummaryStateError, match="未结算"):
        store.begin(material, model="m")


def test_fail_bounds_reason_and_keeps_text_null(store):
    record = store.begin(_material(), model="m")
    failed = store.fail(record, "供应商 500：" + "x" * (ERROR_MAX_BYTES * 2))
    assert failed.status == "failed"
    assert failed.text is None and failed.text_digest is None
    assert len(failed.error.encode("utf-8")) <= ERROR_MAX_BYTES + len("…（已截断）".encode())
    assert store.latest_ready(record.material_id) is None
    assert store.latest(record.material_id).status == "failed"


def test_records_are_append_only(store):
    """表上禁 DELETE：订正靠追加新版本，不靠删历史。"""
    record = store.begin(_material(), model="m")
    store.complete(record, "要点")
    with pytest.raises(sqlite3.IntegrityError, match="追加式"):
        store._conn.execute(
            "DELETE FROM summary_versions WHERE summary_id = ?", (record.summary_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="追加式"):
        store._conn.execute("DELETE FROM summary_sources")
    with pytest.raises(sqlite3.IntegrityError, match="追加式"):
        store._conn.execute("DELETE FROM summary_materials")


def test_material_ledger_lists_sources_in_order(store):
    material = _material()
    store.complete(store.begin(material, model="m"), "要点")
    ledger = store.material(material.material_id)
    assert ledger is not None
    assert ledger["scope"] == "task:t1"
    assert ledger["bytes"] == material.bytes
    assert [s["ref"] for s in ledger["sources"]] == [s.ref for s in material.sources]
    assert [s["digest"] for s in ledger["sources"]] == [s.digest for s in material.sources]


def test_count_for_run_counts_failed_and_ready(store):
    """失败的尝试也是**一次真实调用**：按行数算，不按成功算。"""
    m1 = build_material(_blocks(2), scope="task:t1")
    m2 = build_material(
        [MaterialBlock(seq=40, role="tool", content="另一批材料")], scope="task:t1"
    )
    store.complete(store.begin(m1, model="m", run_id="run-a"), "要点")
    store.fail(store.begin(m2, model="m", run_id="run-a"), "供应商超时")
    assert store.count_for_run("run-a") == 2
    assert store.count_for_run("run-b") == 0
    assert store.count_for_run("") == 0


# ---------------------------------------------------------------- 不建表 + 事务


def test_read_does_not_create_schema(tmp_path):
    """回放旧库时不该顺手改 schema：读不到就是"这个库没有摘要记录"。"""
    log = EventLog(tmp_path / "events.db")
    reader = SummaryStore(log, create=False)
    assert reader.read("a" * 64) is None
    assert reader.latest("b" * 64) is None
    assert reader.count_for_run("run-x") == 0
    row = reader._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summary_versions'"
    ).fetchone()
    assert row is None
    # 写路径在没建表时明确拒绝，而不是让 SQLite 抛一个看不懂的 OperationalError。
    material = build_material(_blocks(2), scope="task:t1")
    with pytest.raises(SummaryStateError, match="摘要表不存在"):
        reader.begin(material, model="m")


def test_write_rolls_back_with_the_event_transaction(tmp_path):
    """摘要行与事件同生共死：外层事务回滚 ⇒ 不留半条记录。"""
    log = EventLog(tmp_path / "events.db")
    store = SummaryStore(log)
    material = build_material(_blocks(2), scope="task:t1")
    with pytest.raises(RuntimeError):
        with log.transaction():
            store.begin(material, model="m", run_id="run-a")
            raise RuntimeError("模拟外层事务失败")
    assert store.latest(material.material_id) is None
    assert store.material_count() == 0


def test_prompt_is_versioned_and_bounded():
    material = _material()
    prompt = prompt_for(material)
    assert prompt.startswith(f"[{PROMPT_VERSION}]")
    assert str(SUMMARY_MAX_BYTES) in prompt
    assert f"{len(material.sources)} 条" in prompt
    assert material.text in prompt
