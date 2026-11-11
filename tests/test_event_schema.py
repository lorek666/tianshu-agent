"""C3 逐类型 payload 契约与精确版本的验收（PRD §5.3.5 第 1–2 条）。

它证明四件事，每件都有"看起来差不多、其实已经破了"的反例：

1. **契约完整且封闭**：41 个 `EVENT_TYPES` 每个都有一份 payload schema；
   不支持的关键字/dialect 在注册时就被拒绝（不是运行时忽略）。
2. **写入前校验**：非法 payload 抛带类型与字段路径的域错误，且发生在任何
   副作用之前（不留事件、不留 blob、不留版本行）。
3. **精确版本**：事件行有 `payload_version`，旁表 `event_versions` 与事件
   同一事务；历史行无版本记录 → 按 v0 读；旁表缺失/损坏 → 明确 unknown；
   未来版本 → 明确 unknown（读不懂不猜）。
4. **旧版夹具逐个可读**：`tests/fixtures/events/legacy_v0.jsonl` 对 41 个类型
   逐一验证"旧形状按本来就没有读取、不按新版补值"；当前形状的无版本记录行
   同样按 v0 读出（迁移场景）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tianshu import event_schema
from tianshu.errors import TianshuError
from tianshu.events import (
    EVENT_TYPES,
    MAX_INLINE_BYTES,
    EventLog,
    canonical_bytes,
    sha256_hex,
)
from tianshu.execpolicy import ExecPolicy
from tianshu.jobs import JOB_OUTCOMES
from tianshu.loop import Limits, run
from tianshu.model import FINISH_REASONS, ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EXEC,
    ACTION_OVERWRITE,
    ACTION_READ,
    APPROVAL_STATES,
)
from tianshu.replay import replay
from tianshu.sandbox import LocalBackend, MemBackend
from tianshu.tasks import (
    CONTROL_REQUESTS,
    EFFECT_STATES,
    RUN_OUTCOMES,
    RUN_STATES,
)
from tianshu.tools import SPECS
from tianshu.workspace import WorkspaceBinding

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "c3" / "event.json"
FIXTURES = ROOT / "tests" / "fixtures" / "events"
ALL_ACTIONS = {
    ACTION_READ,
    ACTION_CREATE,
    ACTION_OVERWRITE,
    ACTION_DELETE,
    ACTION_EXEC,
}


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")


def _schema_doc() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def _prop(type_name: str, *path: str) -> dict:
    node = event_schema.schema_for(type_name, event_schema.CURRENT_PAYLOAD_VERSION)
    for key in path:
        node = node["properties"][key]
    return node


def _enum_of(type_name: str, *path: str) -> set:
    return set(_prop(type_name, *path)["enum"]) - {None}


# ---------------------------------------------------------------- 契约完整性


def test_schema_json_mirrors_python_contract():
    """schemas/c3/event.json 与 event_schema.py 逐字一致（两处同时改才绿）。"""
    doc = _schema_doc()
    event_schema.require_supported_dialect(doc["$schema"])
    payload = doc["$defs"]["payload"]
    legacy = doc["$defs"]["legacyPayload"]
    assert set(payload) == set(EVENT_TYPES)
    assert payload == event_schema.CURRENT_SCHEMAS
    assert legacy == event_schema.LEGACY_SCHEMAS


def test_registry_is_complete_and_bidirectional():
    event_schema.ensure_registry(EVENT_TYPES)  # 不抛
    with pytest.raises(event_schema.UnsupportedSchemaKeyword, match="缺少 schema"):
        event_schema.ensure_registry(EVENT_TYPES - {"run.start"})
    with pytest.raises(event_schema.UnsupportedSchemaKeyword, match="多出 schema"):
        event_schema.ensure_registry(EVENT_TYPES | {"bogus.event"})


@pytest.mark.parametrize(
    "keyword_schema",
    [
        {"type": "object", "properties": {"x": {"type": "string", "minLength": 1}}},
        {"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}]}}},
        {"type": "object", "additionalProperties": False, "patternProperties": {}},
        {"type": "object", "properties": {"x": {"type": "string", "format": "date"}}},
    ],
)
def test_unsupported_keyword_is_rejected_at_registration(keyword_schema):
    """未实现的关键字必须在**注册时**拒绝：写进契约却不生效 = 契约在撒谎。"""
    with pytest.raises(event_schema.UnsupportedSchemaKeyword):
        event_schema.validate_schema_definition(keyword_schema, where="probe")


def test_unsupported_dialect_is_rejected():
    with pytest.raises(event_schema.UnsupportedSchemaKeyword, match="dialect"):
        event_schema.require_supported_dialect(
            "http://json-schema.org/draft-07/schema#"
        )


def test_every_type_root_is_closed_object():
    for type_name in EVENT_TYPES:
        schema = event_schema.schema_for(type_name, event_schema.CURRENT_PAYLOAD_VERSION)
        assert schema["type"] == "object", type_name
        assert schema["additionalProperties"] is False, type_name
        assert schema["required"], f"{type_name} 没有任何必填字段，等于没有契约"


def test_code_constants_bound_to_schema_enums():
    """封闭集三处同步：schema 的 enum 与代码常量不一致时这里红。"""
    assert _enum_of("run.state", "from") == RUN_STATES
    assert _enum_of("run.state", "to") == RUN_STATES
    assert _enum_of("run.finish", "outcome") == JOB_OUTCOMES
    assert _enum_of("job.finished", "outcome") == JOB_OUTCOMES
    # run.end 多一个 noop（loop.py 的 M0 语义），文档里写明两条口径不同
    assert _enum_of("run.end", "outcome") == set(RUN_OUTCOMES) | {"noop"}
    for type_name in ("run.effect", "run.end", "run.finish", "tool.result"):
        assert _enum_of(type_name, "effect_state") == EFFECT_STATES
    assert _enum_of("receipt", "effect_state") == EFFECT_STATES
    assert _enum_of("model.result", "finish_reason") == FINISH_REASONS
    assert _enum_of("run.model_turn", "finish_reason") == FINISH_REASONS
    assert _enum_of("tool.result", "status") == {
        "succeeded", "failed", "rejected", "unknown"
    }
    assert _enum_of("run.control", "action") == set(CONTROL_REQUESTS) | {
        "lease_takeover"
    }
    assert _enum_of("approval.decided", "decision") == {"approved", "rejected"}
    assert _enum_of("approval.revoked", "stage") == APPROVAL_STATES
    for type_name, field in (
        ("approval.requested", "action"),
        ("dispatch.requested", "action"),
        ("dispatch.rejected", "action"),
        ("grant.issued", "action"),
        ("policy.denied", "action"),
        ("receipt", "action"),
    ):
        assert _enum_of(type_name, field) == ALL_ACTIONS, type_name
    tool_ids = set(SPECS)
    assert _enum_of("tool.intent", "tool_id") == tool_ids
    assert _enum_of("tool.not_dispatched", "tool_id") == tool_ids
    assert _enum_of("receipt", "tool_id") == tool_ids
    # 恢复历史日志时 tool.result 允许未知（旧版）工具 id：形状封闭、白名单放开
    tool_result_id = _prop("tool.result", "tool_id")
    assert "enum" not in tool_result_id
    assert re.search(tool_result_id["pattern"], "rename_everything") is not None
    assert re.search(tool_result_id["pattern"], "Bad Tool Id!") is None
    assert _enum_of("receipt", "backend") == {"local_fs", "memfs", "memory"}
    exec_policy_fields = set(ExecPolicy.__dataclass_fields__)
    assert set(_prop("task.created", "exec_policy")["properties"]) == exec_policy_fields
    binding = _prop("task.created", "workspaces")["items"]
    assert set(binding["properties"]["mode"]["enum"]) == set(WorkspaceBinding.MODES)
    assert set(binding["properties"]["snapshot_policy"]["enum"]) == set(
        WorkspaceBinding.SNAPSHOT_POLICIES
    )
    # 本地后端名字必须被收进 receipt.backend：新后端若没登记，这里会红
    backend_names = {LocalBackend().name, MemBackend(namespace=Path("/tmp/ns")).name}
    assert backend_names <= _enum_of("receipt", "backend")
    assert {"local_fs", "memfs"} <= backend_names


# ---------------------------------------------------------------- 写入前校验


@pytest.mark.parametrize("type_name", sorted(EVENT_TYPES))
def test_every_type_has_valid_minimal_payload_and_invalid_extra_key(tmp_path, type_name):
    """每个类型至少一正一反例：最小合法 payload 通过；多一个陌生字段被拒。"""
    payload = event_schema.minimal_payload(type_name)
    event_schema.validate_payload(type_name, payload)

    log = _log(tmp_path)
    try:
        seq = log.append("run_x", type_name, payload)
        (event,) = log.read_run("run_x")
        assert event.seq == seq
        assert event.payload == payload
        assert event.payload_version == event_schema.CURRENT_PAYLOAD_VERSION
    finally:
        log.close()

    bad = dict(payload)
    bad["__nope__"] = 1
    with pytest.raises(event_schema.PayloadContractError) as ei:
        event_schema.validate_payload(type_name, bad)
    assert ei.value.event_type == type_name
    assert ei.value.path == "$.__nope__", ei.value.path
    assert "__nope__" in str(ei.value)


def test_invalid_payload_is_rejected_by_append_before_any_side_effect(tmp_path):
    """非法 payload 必须在写事件/落 blob 之前被拒。"""
    log = _log(tmp_path)
    try:
        bad = event_schema.minimal_payload("tool.result")
        bad["status"] = "completed"  # 不在封闭集里
        with pytest.raises(event_schema.PayloadContractError) as ei:
            log.append("run_x", "tool.result", bad)
        assert ei.value.event_type == "tool.result"
        assert "$.status" == ei.value.path
        assert log.read_run("run_x") == []
        assert log.seq_head() == 0
        assert log.conn.execute("SELECT count(*) FROM event_versions").fetchone()[0] == 0
    finally:
        log.close()


def test_invalid_payload_with_large_size_leaves_no_orphan_blob(tmp_path):
    """payload 超长时本来会先落 blob；契约校验必须在落盘之前。"""
    log = _log(tmp_path)
    try:
        bad = event_schema.minimal_payload("tool.result")
        bad["output"] = {"baits": "x" * (MAX_INLINE_BYTES + 1024)}
        bad["__nope__"] = True
        with pytest.raises(event_schema.PayloadContractError):
            log.append("run_x", "tool.result", bad)
        assert list((tmp_path / "blobs").iterdir()) == []
        assert log.seq_head() == 0
    finally:
        log.close()


def test_unknown_type_is_still_rejected_before_payload_validation(tmp_path):
    log = _log(tmp_path)
    try:
        with pytest.raises(TianshuError, match="EVENT_TYPES"):
            log.append("run_x", "dispatch.lease_lost", {"x": 1})
        assert log.read_run("run_x") == []
    finally:
        log.close()


def test_invalid_payload_rolls_back_the_whole_transaction(tmp_path):
    log = _log(tmp_path)
    try:
        valid = event_schema.minimal_payload("run.start")
        with pytest.raises(event_schema.PayloadContractError):
            with log.transaction() as tx:
                tx.append("run_x", "run.start", valid)
                # 两端都不合法（v1 缺必填 + v0 也不认这个陌生字段）
                tx.append("run_x", "run.end", {"bogus_field": 1})
        assert log.read_run("run_x") == []
        assert log.conn.execute("SELECT count(*) FROM event_versions").fetchone()[0] == 0
        assert log.in_transaction is False
    finally:
        log.close()


@pytest.mark.parametrize(
    "type_name, field, bad_value, path",
    [
        ("tool.result", "status", "completed", "$.status"),
        ("model.result", "finish_reason", "length", "$.finish_reason"),
        ("run.state", "to", "bogus", "$.to"),
        ("run.finish", "outcome", "noop", "$.outcome"),
        ("job.finished", "outcome", "noop", "$.outcome"),
        ("run.control", "action", "start", "$.action"),
        ("receipt", "effect_state", "done", "$.effect_state"),
        ("task.created", "workspaces", [{"root_path": "/ws", "mode": "rw",
                                        "snapshot_policy": "none"}],
         "$.workspaces[0].mode"),
    ],
)
def test_targeted_invalid_values_are_rejected_with_path(
    type_name, field, bad_value, path
):
    payload = event_schema.minimal_payload(type_name)
    payload[field] = bad_value
    errors = event_schema.payload_errors(type_name, payload)
    assert errors, f"{type_name}.{field}={bad_value!r} 应当非法"
    assert any(e.startswith(path + "：") for e in errors), errors


def test_nested_required_field_missing_is_rejected_with_path():
    payload = event_schema.minimal_payload("model.intent")
    payload["context"]["memories"] = [
        {"memory_id": "m", "revision": 1, "content_ref": "c", "scope": "user",
         "scope_id": "local"}
    ]
    errors = event_schema.payload_errors("model.intent", payload)
    assert any("$.context.memories[0]" in e and "tool_call_id" in e for e in errors)


def test_missing_required_field_is_rejected_with_root_path():
    payload = event_schema.minimal_payload("receipt")
    del payload["root"]
    errors = event_schema.payload_errors("receipt", payload)
    assert any(e.startswith("$：") and "root" in e for e in errors)


# ---------------------------------------------------------------- 版本与兼容


def test_current_writes_record_version_in_side_table(tmp_path):
    log = _log(tmp_path)
    try:
        payload = event_schema.minimal_payload("run.start")
        seq = log.append("run_x", "run.start", payload)
        (event,) = log.read_run("run_x")
        assert event.payload_version == event_schema.CURRENT_PAYLOAD_VERSION
        row = log.conn.execute(
            "SELECT type, version FROM event_versions WHERE seq=?", (seq,)
        ).fetchone()
        assert (row["type"], row["version"]) == ("run.start", 1)
    finally:
        log.close()


def test_legacy_shaped_write_is_stamped_version_zero(tmp_path):
    """写入口识别成旧版形状 → 记 version=0（不是悄悄当新版）。"""
    log = _log(tmp_path)
    try:
        payload = {"call_id": "c_old", "tool_id": "rename_everything",
                   "args": {}, "args_hash": "x"}
        assert event_schema.detect_version("tool.intent", payload) == 0
        log.append("run_x", "tool.intent", payload)
        (event,) = log.read_run("run_x")
        assert event.payload_version == 0
        assert event.payload == payload
    finally:
        log.close()


def test_historical_row_without_version_record_reads_as_v0(tmp_path):
    """旧库（升级前写入）没有版本行：读取按 v0，且不按新版默认补值。"""
    log = _log(tmp_path)
    try:
        payload = event_schema.minimal_payload("task.created")
        seq = log.append("run_x", "task.created", payload)
        # 模拟"这条记录是加版本旁表之前写的"：删掉版本记录（旁表不是事件表）
        log.conn.execute("DELETE FROM event_versions WHERE seq=?", (seq,))
        (event,) = log.read_run("run_x")
        assert event.payload_version == 0
        assert event.payload == payload, "读取不得做字段补值"
    finally:
        log.close()


def test_missing_version_side_table_is_explicit_unknown(tmp_path):
    """旁表整个缺失 → 不能把一切当 v0（那是把"读不懂"洗成"旧版"）。"""
    log = _log(tmp_path)
    try:
        log.append("run_x", "run.start", event_schema.minimal_payload("run.start"))
        log.conn.execute("DROP TABLE event_versions")
        with pytest.raises(event_schema.PayloadVersionUnavailable):
            log.read_run("run_x")
    finally:
        log.close()


def test_corrupt_version_row_is_explicit_unknown(tmp_path):
    """未来程序写的版本（99）→ 明确 unknown，不按当前 schema 猜。"""
    log = _log(tmp_path)
    try:
        seq = log.append("run_x", "run.start", event_schema.minimal_payload("run.start"))
        log.conn.execute(
            "UPDATE event_versions SET version=99 WHERE seq=?", (seq,)
        )
        with pytest.raises(event_schema.UnknownPayloadVersion):
            log.read_run("run_x")
    finally:
        log.close()


def test_unknown_event_type_in_history_is_not_interpreted(tmp_path):
    """历史库里出现未登记类型（早于封闭集/损坏）：读取明确报错，不按任何 schema 解释。"""
    log = _log(tmp_path)
    try:
        raw = canonical_bytes({"x": 1})
        cur = log.conn.execute(
            "INSERT INTO events (run_id, type, ts, actor_kind, actor_id, payload,"
            " payload_hash) VALUES ('run_x','bogus.event',0,'core','',?,?)",
            (raw.decode("utf-8"), sha256_hex(raw)),
        )
        log.conn.execute(
            "INSERT INTO event_versions (seq, type, version) VALUES (?,?,0)",
            (int(cur.lastrowid), "bogus.event"),
        )
        with pytest.raises(event_schema.UnknownPayloadVersion):
            log.read_run("run_x")
    finally:
        log.close()


def test_read_after_and_read_types_carry_versions(tmp_path):
    log = _log(tmp_path)
    try:
        first = log.append("run_x", "run.start", event_schema.minimal_payload("run.start"))
        legacy = {"call_id": "c_old", "tool_id": "rename_everything", "args": {},
                  "args_hash": "x"}
        second = log.append("run_x", "tool.intent", legacy)
        after = log.read_after(first - 1)
        assert [e.payload_version for e in after] == [1, 0]
        types = log.read_types(["tool.intent"])
        assert [e.payload_version for e in types] == [0]
        assert second == types[0].seq
    finally:
        log.close()


# ---------------------------------------------------------------- 夹具


def _fixture_lines() -> list[dict]:
    rows = []
    for line in (FIXTURES / "legacy_v0.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def test_legacy_fixture_covers_every_type_and_is_readable(tmp_path):
    """41 个类型逐个可读：旧形状按"本来就没有"读取，不按新版补值。"""
    rows = _fixture_lines()
    assert {r["type"] for r in rows} == set(EVENT_TYPES)
    assert len(rows) == len(EVENT_TYPES)
    drift = [r["type"] for r in rows if "历史形状" in r["note"]]
    assert len(drift) >= 5, f"旧形状覆盖太少，夹具失去区分度：{drift}"

    log = _log(tmp_path)
    try:
        for row in rows:
            type_name, payload = row["type"], row["payload"]
            assert event_schema.payload_errors(type_name, payload, 0) == [], type_name
            seq = log.append(f"run_{type_name}", type_name, payload)
            if event_schema.payload_errors(type_name, payload, 1):
                # 真有历史形状差异 → 写入口按 v0 记
                assert event_schema.detect_version(type_name, payload) == 0
            else:
                # 形状与当前一致、但"旁表无记录"的历史行同样按 v0 读
                log.conn.execute("DELETE FROM event_versions WHERE seq=?", (seq,))
            (event,) = log.read_run(f"run_{row['type']}")
            assert event.payload_version == 0, type_name
            assert event.payload == payload, f"{type_name} 读取不得补值"
    finally:
        log.close()


def test_minimal_v1_fixture_is_valid_and_round_trips(tmp_path):
    doc = json.loads((FIXTURES / "minimal_v1.json").read_text(encoding="utf-8"))
    assert doc["dataset"] == "minimal-v1"
    assert set(doc["events"]) == set(EVENT_TYPES)
    log = _log(tmp_path)
    try:
        for type_name, payload in sorted(doc["events"].items()):
            event_schema.validate_payload(type_name, payload)
            assert payload == event_schema.minimal_payload(type_name)
            log.append("run_x", type_name, payload)
        written = log.read_run("run_x")
    finally:
        log.close()
    assert len(written) == len(EVENT_TYPES)
    assert {e.type for e in written} == set(EVENT_TYPES)
    assert all(e.payload_version == 1 for e in written)


def test_replay_still_passes_after_contract_upgrade(tmp_path):
    """写入校验升级后 replay 仍可跑通（不能因为校验把历史/新事件读坏了）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    log = _log(tmp_path)
    try:
        result = run(
            task="写一份报告",
            workspace_dir=ws,
            out_name="report.md",
            model=ScriptedModel([
                ModelResponse("tool_use", tool_calls=[
                    ToolCall("c1", "create_report",
                             {"name": "report.md", "content": "产物"})
                ]),
                ModelResponse("end_turn", content="完成"),
            ]),
            log=log,
            limits=Limits(4, 1000),
        )
        report = replay(log, result.run_id)
        assert report.ok, report.problems
        assert report.problems == []
        versions = {e.payload_version for e in log.read_run(result.run_id)}
        assert versions == {1}, "新写入必须全部是当前版本"
    finally:
        log.close()


def test_blob_payload_still_round_trips_with_version(tmp_path):
    """大 payload（落 blob）也要带版本并能读回。"""
    log = _log(tmp_path)
    try:
        payload = event_schema.minimal_payload("model.result")
        payload["content"] = "x" * (MAX_INLINE_BYTES + 64)
        seq = log.append("run_x", "model.result", payload)
        (event,) = log.read_run("run_x")
        assert event.seq == seq
        assert event.payload == payload
        assert event.payload_version == 1
        assert event.payload["content"].startswith("x" * 16)
    finally:
        log.close()


# ---------------------------------------------------------------- 旁表事务性


def test_version_row_is_written_in_same_transaction(tmp_path):
    """版本旁表写失败时事件行也必须回滚（不能留下无版本的新事件）。"""
    log = _log(tmp_path)
    try:
        # 用触发器让"写版本行"必然失败：这是真实的库级失败，不是桩。
        log.conn.execute(
            "CREATE TRIGGER fail_version_insert BEFORE INSERT ON event_versions"
            " BEGIN SELECT RAISE(ABORT, 'injected-version-failure'); END;"
        )
        with pytest.raises(Exception, match="injected-version-failure"):
            log.append("run_x", "run.start", event_schema.minimal_payload("run.start"))
        log.conn.execute("DROP TRIGGER fail_version_insert")
        assert log.read_run("run_x") == []
        assert log.seq_head() == 0
        assert log.in_transaction is False
    finally:
        log.close()
