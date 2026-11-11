"""内容引用：有界摘要 + 分页读取 + 任务可见范围（G1e 的存储层判据）。

PRD §5.2.3 的两句话是这一组的立据：

> 大结果以有界摘要 + 内容引用返回，分页读取引用同样验证任务可见范围。

> 私有内容未经外发许可不发包（与本组无关，但同属"内容"的边界）。

这一组只测**存储层**（`src/tianshu/contents.py`）：引用形状、授权、分页、
摘要复核、错误分类。工具与派发层接线在 `test_content_read.py`。
"""

from __future__ import annotations

import hashlib

import pytest

from tianshu.contents import (
    DEFAULT_PAGE,
    MAX_PAGE,
    REF_PREFIX,
    ContentDigestMismatch,
    ContentError,
    ContentMissing,
    ContentNotVisible,
    ContentStore,
    make_ref,
    parse_ref,
)
from tianshu.events import EventLog


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(exist_ok=True)
    try:
        yield log, ContentStore(log, blob_dir=blob_dir), blob_dir
    finally:
        log.close()


def _put(store: ContentStore, blob_dir, data: bytes, *, task_id: str, run_id="run-1"):
    digest = _digest(data)
    (blob_dir / digest).write_bytes(data)
    store.record(digest, data, task_id=task_id, run_id=run_id)
    return digest


# ---------------------------------------------------------------- 1 引用形状


def test_ref_shape_is_closed():
    digest = "a" * 64
    assert make_ref(digest) == f"{REF_PREFIX}{digest}"
    assert parse_ref(make_ref(digest)) == digest
    for bad in (
        "",
        None,
        digest,
        f"{REF_PREFIX}",
        f"{REF_PREFIX}{'A' * 64}",  # 大写不算同一个摘要
        f"{REF_PREFIX}{'a' * 63}",
        f"{REF_PREFIX}{'a' * 65}",
        "content://sha1:" + "a" * 40,
        f"file://{digest}",
    ):
        with pytest.raises(ContentError):
            parse_ref(bad)
    with pytest.raises(ContentError):
        make_ref("not-a-digest")


# ---------------------------------------------------------------- 2 登记与去重


def test_record_dedupes_content_but_binds_every_task(store):
    log, contents, blobs = store
    data = "同一份大结果".encode()
    digest = _put(contents, blobs, data, task_id="task-a")
    first = contents.object(digest)
    assert first is not None and first["size"] == len(data)

    # 同一份内容被另一个任务引用：对象只有一行，授权多一行
    again = contents.record(digest, data, task_id="task-b", run_id="run-2")
    assert again is False, "同 digest 不该重复登记对象"
    assert contents.created_count() == 1
    assert contents.visible(digest, "task-a")
    assert contents.visible(digest, "task-b")

    # 内容寻址去重不能变成"共享可见性"：授权是按任务写的
    assert not contents.visible(digest, "task-c")


def test_record_rejects_bad_input(store):
    _log, contents, blobs = store
    data = b"x"
    with pytest.raises(ContentError):
        contents.record("short", data, task_id="t")
    with pytest.raises(ContentError):
        contents.record(_digest(data), "not-bytes", task_id="t")  # type: ignore[arg-type]
    with pytest.raises(ContentError):
        contents.record(_digest(data), data, task_id="")


def test_store_uses_the_event_database(store):
    """建在事件库同一个 SQLite 里：不制造第二个事实源。"""
    log, contents, blobs = store
    _put(contents, blobs, b"payload", task_id="task-a")
    names = {
        row[0]
        for row in log.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"content_objects", "content_grants"} <= names


# ---------------------------------------------------------------- 3 可见范围


def test_visibility_is_enforced_per_task(store):
    _log, contents, blobs = store
    digest = _put(contents, blobs, b"private notes", task_id="owner")
    page = contents.read(make_ref(digest), task_id="owner")
    assert page.data == "private notes"
    with pytest.raises(ContentNotVisible):
        contents.read(make_ref(digest), task_id="intruder")


def test_invisible_is_reported_before_missing(store):
    """授权先判：不可见时不从错误信息里探出"它存不存在"。"""
    _log, contents, blobs = store
    digest = _put(contents, blobs, b"x", task_id="owner")
    (blobs / digest).unlink()  # 内容没了
    with pytest.raises(ContentNotVisible):
        contents.read(make_ref(digest), task_id="intruder")
    with pytest.raises(ContentMissing):
        contents.read(make_ref(digest), task_id="owner")


# ---------------------------------------------------------------- 4 分页


def test_paging_reconstructs_exact_bytes(store):
    _log, contents, blobs = store
    text = "".join(chr(0x4E00 + i) for i in range(300))  # 300 个 CJK 字符 = 900 字节
    digest = _put(contents, blobs, text.encode(), task_id="task-a")

    pages = []
    offset = 0
    while True:
        page = contents.read(make_ref(digest), task_id="task-a", offset=offset, length=256)
        pages.append(page)
        assert page.total_size == len(text.encode())
        if page.next_offset is None:
            break
        assert page.next_offset == offset + len(page.data.encode())
        offset = page.next_offset
        assert len(pages) < 20, "分页没有推进"

    assert "".join(item.data for item in pages) == text
    assert pages[0].to_payload()["ref"] == make_ref(digest)
    assert pages[-1].to_payload()["next_offset"] is None


def test_page_boundary_does_not_split_multibyte_chars(store):
    """分页按字节切片，但**不许**把多字节字符切一半交出去。"""
    _log, contents, blobs = store
    digest = _put(contents, blobs, ("中" * 5).encode(), task_id="task-a")
    ref = make_ref(digest)

    first = contents.read(ref, task_id="task-a", offset=0, length=4)
    assert first.data == "中", "4 字节只够一个整字（3 字节），第 4 字节是半截"
    assert first.next_offset == 3, "下次必须从字符边界开始"

    second = contents.read(ref, task_id="task-a", offset=3, length=4)
    assert second.data == "中"

    with pytest.raises(ContentError) as excinfo:
        contents.read(ref, task_id="task-a", offset=0, length=1)
    assert "长度" in str(excinfo.value)


def test_paging_bounds_are_closed(store):
    _log, contents, blobs = store
    digest = _put(contents, blobs, b"0123456789", task_id="task-a")
    ref = make_ref(digest)
    with pytest.raises(ContentError):
        contents.read(ref, task_id="task-a", length=0)
    with pytest.raises(ContentError):
        contents.read(ref, task_id="task-a", length=MAX_PAGE + 1)
    with pytest.raises(ContentError):
        contents.read(ref, task_id="task-a", offset=-1)
    with pytest.raises(ContentError):
        contents.read(ref, task_id="task-a", offset=11)
    with pytest.raises(ContentError):
        contents.read(ref, task_id="")
    assert DEFAULT_PAGE <= MAX_PAGE
    # 恰好读完时 next_offset 为 None（不是"="）
    page = contents.read(ref, task_id="task-a", offset=0, length=10)
    assert page.next_offset is None


# ---------------------------------------------------------------- 5 摘要复核


def test_read_recomputes_the_digest(store):
    _log, contents, blobs = store
    digest = _put(contents, blobs, b"original", task_id="task-a")
    (blobs / digest).write_bytes(b"tampered")  # 同名文件，内容换了
    with pytest.raises(ContentDigestMismatch):
        contents.read(make_ref(digest), task_id="task-a")


def test_non_utf8_content_is_refused_not_mangled(store):
    _log, contents, blobs = store
    data = b"\xff\xfe" * 4
    digest = _put(contents, blobs, data, task_id="task-a")
    with pytest.raises(ContentError):
        contents.read(make_ref(digest), task_id="task-a")


def test_read_refuses_without_a_blob_directory(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        contents = ContentStore(log)  # 没有 blob_dir
        with pytest.raises(Exception) as excinfo:
            contents.read(make_ref("a" * 64), task_id="task-a")
        assert "blob" in str(excinfo.value).lower() or "授权" in str(excinfo.value)
    finally:
        log.close()
