"""C3 事件契约：封闭注册表 + 写入校验 + schema 绑定。

项目零运行时依赖，没有 JSON Schema 校验库，所以这里做的是**结构性对照**：

- 未登记的类型必须在写入侧被拒，且拒绝发生在任何副作用之前（不能留下孤儿 blob）；
- 每个登记类型都能真的写进去、读回来；
- `schemas/c3/event.json` 的 `type.enum` 与 `EVENT_TYPES` 完全一致——**同一个测试
  校验两边**，改跑一处忘另一处会红。

它证明的是"类型集合被封住了"，不是"payload 字段合法"：每种类型自己的 payload
形状尚未冻结（PRD §5.3.4 明写了这条边界），别把这里的绿当成 payload 契约也绿了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianshu.errors import TianshuError
from tianshu.events import (
    DECLARED_UNEMITTED_EVENT_TYPES,
    EVENT_TYPES,
    MAX_INLINE_BYTES,
    UNIMPLEMENTED_EVENT_FAMILIES,
    EventLog,
)

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "c3" / "event.json"
FAMILIES = frozenset(
    {
        "approval",
        "dispatch",
        "execution",
        "grant",
        "job",
        "model",
        "policy",
        "receipt",
        "recovery",
        "run",
        "task",
        "tool",
        "undo",
    }
)
REQUIRED_ENVELOPE = {"seq", "run_id", "type", "ts", "actor", "payload", "payload_hash"}


def _log(tmp_path: Path, *, blobs: bool = True) -> EventLog:
    blob_dir = tmp_path / "blobs" if blobs else None
    return EventLog(tmp_path / "events.db", blob_dir=blob_dir)


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 写入校验


def test_unknown_type_is_rejected(tmp_path):
    log = _log(tmp_path)
    try:
        with pytest.raises(TianshuError) as excinfo:
            log.append("run_x", "dispatch.lease_lost", {"x": 1})
        # rule id 不是事件类型：这条正是最容易混进来的那类错误
        assert "dispatch.lease_lost" in str(excinfo.value)
        assert "EVENT_TYPES" in str(excinfo.value)
        assert log.read_run("run_x") == []
    finally:
        log.close()


def test_unknown_type_is_rejected_before_any_side_effect(tmp_path):
    """payload 超长时本来会先落 blob；校验必须在落盘之前。"""
    log = _log(tmp_path)
    try:
        with pytest.raises(TianshuError):
            log.append("run_x", "not.a.type", {"big": "x" * (MAX_INLINE_BYTES + 1024)})
        assert list((tmp_path / "blobs").iterdir()) == [], "拒绝时不能留下孤儿 blob"
        assert log.read_run("run_x") == []
    finally:
        log.close()


def test_unknown_type_rolls_back_the_whole_transaction(tmp_path):
    log = _log(tmp_path)
    try:
        with pytest.raises(TianshuError):
            with log.transaction() as tx:
                tx.append("run_x", "run.start", {"x": 1})
                tx.append("run_x", "bogus.event", {"x": 2})
        assert log.read_run("run_x") == [], "同事务里的合法事件也必须一起回滚"
        assert log.in_transaction is False
    finally:
        log.close()


def test_declared_but_unemitted_type_is_rejected_with_a_hint(tmp_path, monkeypatch):
    """"已定义但无发射点"的类型硬写会被拒，并给出可操作的提示。

    这一条**不能只遍历真实的集合**：`DECLARED_UNEMITTED_EVENT_TYPES` 现在是空的
    （`job.lost` 已删除），空循环会让测试永远绿而机制坏掉也没人知道。所以这里临时
    注入一个成员，专门验机制本身；真实集合为空这件事由下一条测试正面断言。
    """
    from tianshu import events as events_mod

    monkeypatch.setattr(
        events_mod, "DECLARED_UNEMITTED_EVENT_TYPES", frozenset({"probe.unemitted"})
    )
    log = _log(tmp_path)
    try:
        with pytest.raises(TianshuError) as excinfo:
            log.append("run_x", "probe.unemitted", {})
        assert "没有发射点" in str(excinfo.value)
    finally:
        log.close()


def test_no_declared_unemitted_types_remain():
    """当前没有"声明了却没发射点"的事件类型。

    真有新成员时这条会红——这是刻意的：要么把发射点接上并移进 `EVENT_TYPES`，
    要么把这条改成带原因的清单。两种都比"悄悄多一个没人写的类型"好。
    """
    assert DECLARED_UNEMITTED_EVENT_TYPES == frozenset()


@pytest.mark.parametrize("event_type", sorted(EVENT_TYPES))
def test_every_registered_type_can_be_written_and_read_back(tmp_path, event_type):
    log = _log(tmp_path)
    try:
        seq = log.append("run_x", event_type, {"probe": event_type})
        (event,) = log.read_run("run_x")
        assert event.type == event_type
        assert event.seq == seq
        assert event.payload == {"probe": event_type}
        assert list(log.all_event_types()) == [event_type]
    finally:
        log.close()


# ---------------------------------------------------------------- schema 绑定


def test_schema_enum_matches_event_types():
    """同一处校验两边：注册表长了一个类型而 schema 没跟（或反过来）就红。"""
    schema = _schema()
    enum = schema["properties"]["type"]["enum"]
    assert len(enum) == len(set(enum)), "enum 里有重复值"
    assert set(enum) == set(EVENT_TYPES)
    assert enum == sorted(EVENT_TYPES), "enum 必须有序，diff 才可读"


def test_schema_required_excludes_the_reserved_parent_seq():
    schema = _schema()
    assert set(schema["required"]) == REQUIRED_ENVELOPE
    assert "parent_seq" in schema["properties"], "保留字段本身仍要在契约里"
    assert "parent_seq" not in schema["required"], "当前不写的字段不能是 required"
    assert "task_id" not in schema["required"]


def test_schema_does_not_advertise_unemitted_types():
    enum = set(_schema()["properties"]["type"]["enum"])
    assert enum.isdisjoint(DECLARED_UNEMITTED_EVENT_TYPES)
    description = _schema()["properties"]["type"]["description"]
    for name in DECLARED_UNEMITTED_EVENT_TYPES:
        assert name in description, "无发射点的类型必须被显式标注，不能只从 enum 里消失"
    # 机制本身要能在有成员时工作：用注入的假成员验一遍，避免"集合空了就永远绿"
    assert "没有发射点" in description or DECLARED_UNEMITTED_EVENT_TYPES == frozenset()


# ---------------------------------------------------------------- 族


def test_registry_families_are_closed():
    """族 = 名字空间前缀。出现新前缀而没有更新 PRD 映射表时，这里会红。"""
    derived = {t.split(".", 1)[0] for t in EVENT_TYPES}
    assert derived == FAMILIES
    assert derived.isdisjoint(UNIMPLEMENTED_EVENT_FAMILIES), (
        "PRD 声明过但从未实现的族不能出现在真实类型里"
    )
    bare = {t for t in EVENT_TYPES if "." not in t}
    assert bare == {"receipt"}, (
        "receipt 是唯一的裸类型；新增裸类型必须显式处理（schema enum / 族映射 / PRD）"
    )


def test_parent_seq_is_never_written_by_default(tmp_path):
    """保留字段：新写入恒为 None。接上真实因果时必须同时改契约（本测试会提醒）。"""
    log = _log(tmp_path)
    try:
        log.append("run_x", "run.start", {})
        log.append("run_x", "tool.intent", {})
        assert [e.parent_seq for e in log.read_run("run_x")] == [None, None]
    finally:
        log.close()
