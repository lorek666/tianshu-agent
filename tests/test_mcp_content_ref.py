"""MCP 入站正文 content-ref：真实子进程、泄露、水合、凭据与失败负控。"""

from __future__ import annotations

import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support import approve_pending, engine_for

from tianshu._tasks_types import RunCursor
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.loop import build_stack
from tianshu.mcp import (
    MCP_MAX_RESULT_BYTES,
    McpCallResult,
    McpClients,
    McpServerConfig,
)
from tianshu.mcp_results import (
    MAX_RESULT_BYTES,
    MAX_STRUCTURED_BYTES,
    SCHEMA,
    McpResultError,
    decode_mcp_result,
    encode_mcp_result,
    hydrate_mcp_messages,
    hydrate_mcp_output,
    is_ref_output,
    read_mcp_result,
    sanitize_cursor_payload,
)
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ACTION_EXEC
from tianshu.receipt import build_mcp_receipt, verify_receipts
from tianshu.replay import replay
from tianshu.resources import MemoryScope
from tianshu.tools import ToolResult

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
SENTINEL = "MCP-CONTENT-SENTINEL-9f31"


class RecordingModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return super().complete(messages, tools)


def _config(tmp_path: Path, *, mode: str = "content_ref") -> McpServerConfig:
    return McpServerConfig(
        name="fake",
        command=(
            sys.executable, str(FIXTURE), "--mode", mode,
            "--call-log", str(tmp_path / "calls.jsonl"),
        ),
        max_result_bytes=MCP_MAX_RESULT_BYTES if mode == "content_ref_oversize" else 65_536,
    )


def _start(
    tmp_path: Path, *, failpoint_name: str = "", transaction_failure: bool = False,
    config: McpServerConfig | None = None,
):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    clients = McpClients({"fake": config or _config(tmp_path)})
    stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
    original_failpoint = None
    if failpoint_name:
        import tianshu.dispatcher as dispatcher

        original_failpoint = dispatcher.failpoint

        def injected(name, **kwargs):
            if name == failpoint_name:
                raise RuntimeError("injected:" + name)
            return original_failpoint(name, **kwargs)

        dispatcher.failpoint = injected
    if transaction_failure:
        original = log.append_many

        def injected(batch):
            if any(item["type"] == "tool.result" for item in batch):
                raise RuntimeError("injected:result-transaction")
            return original(batch)

        log.append_many = injected
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        task = stack.tasks.create_task("MCP content ref", [str(ws)], mcp_servers=["fake"])
        model = RecordingModel([
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "mcp__fake__echo", {"message": "hello"}
            )]),
            ModelResponse("end_turn", content="done"),
        ])
        engine = engine_for(stack, model)
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)
    finally:
        if failpoint_name:
            dispatcher.failpoint = original_failpoint
    return SimpleNamespace(
        log=log, clients=clients, stack=stack, model=model, result=result,
        root=tmp_path, calls=tmp_path / "calls.jsonl",
    )


@pytest.fixture
def completed(tmp_path):
    env = _start(tmp_path)
    try:
        assert env.result.state == "succeeded", env.result
        yield env
    finally:
        env.clients.close_all()
        env.log.close()


def _result_payload(env):
    return next(
        e.payload for e in env.log.read_run(env.result.run_id, verify=False)
        if e.type == "tool.result"
    )


def _blob(env) -> Path:
    mcp = _result_payload(env)["output"]["mcp"]
    return Path(env.log.db_path).parent / mcp["blob_path"] / mcp["ref"].rsplit(":", 1)[1]


def _tool_messages(messages):
    return [m for m in messages if m.get("role") == "tool"]


def test_body_never_enters_events_wal_cursor_or_diagnostics(completed):
    env = completed
    payload = _result_payload(env)
    mcp = payload["output"]["mcp"]
    assert mcp["schema"] == SCHEMA
    assert mcp["ref"].startswith("content://sha256:")
    assert not {"content", "structured", "text_parts", "endpoint", "spawn"} & set(mcp)
    assert payload["effect_state"] == "applied"
    body = _blob(env).read_bytes()
    envelope = decode_mcp_result(body)
    assert envelope["content_blocks"][1]["data"] == SENTINEL + "\n中文\x00tail"
    assert envelope["structured"]["secret"] == SENTINEL + "\n中文\x00tail"
    assert mcp["size"] == len(body)
    assert mcp["sha256"] == sha256_hex(body)
    assert mcp["after_state"] == "mcp:" + sha256_hex(body)
    with sqlite3.connect(env.log.db_path) as conn:
        raw_cursor = conn.execute(
            "SELECT payload FROM run_cursors WHERE run_id = ?", (env.result.run_id,)
        ).fetchone()[0]
    event_bytes = canonical_bytes([e.payload for e in env.log.read_run(env.result.run_id)])
    wal = Path(str(env.log.db_path) + "-wal")
    surfaces = [raw_cursor.encode(), event_bytes, env.log.db_path.read_bytes()]
    if wal.exists():
        surfaces.append(wal.read_bytes())
    surfaces.append(repr(env.clients.states()).encode())
    assert all(SENTINEL.encode() not in surface for surface in surfaces)
    assert SENTINEL.encode() in body
    assert verify_receipts(env.log, env.result.run_id) == []
    assert replay(env.log, env.result.run_id).ok
    assert len(env.calls.read_text(encoding="utf-8").splitlines()) == 1


def test_live_resume_reopen_and_replay_hydrate_equivalent_messages(completed):
    env = completed
    live = _tool_messages(env.model.seen[-1])
    assert len(live) == 1 and SENTINEL in live[0]["content"]
    cursor, _ = env.stack.tasks.load_cursor(env.result.run_id)
    resumed = _tool_messages(cursor.messages)
    assert resumed == live
    persisted = sanitize_cursor_payload(cursor.to_dict())
    assert SENTINEL not in json.dumps(persisted)
    report = replay(env.log, env.result.run_id)
    assert report.ok, report.problems
    env.clients.close_all()
    env.log.close()
    reopened = EventLog(env.root / "events.db", blob_dir=env.root / "blobs")
    try:
        # 不依赖写盘过程的 _mcp_blob_dir 内存提示；新连接凭持久定位独立读取。
        assert verify_receipts(reopened, env.result.run_id) == []
        stack = build_stack(reopened, data_root=env.root / "data")
        loaded, _ = stack.tasks.load_cursor(env.result.run_id)
        assert _tool_messages(loaded.messages) == live
        assert replay(reopened, env.result.run_id).ok
    finally:
        reopened.close()


@pytest.mark.parametrize("mutation", [
    "missing", "tampered", "size", "hash", "schema", "metadata", "unknown_schema", "no_schema",
])
def test_blob_and_metadata_negative_controls_fail_closed(completed, mutation):
    env = completed
    output = deepcopy(_result_payload(env)["output"])
    path = _blob(env)
    original = path.read_bytes()
    if mutation == "missing":
        path.unlink()
    elif mutation == "tampered":
        path.write_bytes(original.replace(b"empty", b"other"))
    elif mutation == "size":
        output["mcp"]["size"] += 1
    elif mutation == "hash":
        output["mcp"]["sha256"] = "0" * 64
    elif mutation == "schema":
        output["mcp"]["schema"] = "tianshu.mcp-result.v2"
    elif mutation == "metadata":
        output["mcp"]["duration_ms"] += 1
    elif mutation == "unknown_schema":
        output["mcp"]["schema"] = "vendor.result.v9"
    else:
        del output["mcp"]["schema"]
    with pytest.raises(McpResultError):
        hydrate_mcp_output(output, log=env.log)
    if mutation in {"missing", "tampered"}:
        assert verify_receipts(env.log, env.result.run_id)


def test_cursor_and_receipt_reject_corrupt_blob_without_downgrade(completed):
    env = completed
    _blob(env).write_bytes(b"{}")
    with pytest.raises(McpResultError):
        env.stack.tasks.load_cursor(env.result.run_id)
    assert verify_receipts(env.log, env.result.run_id)
    problems = replay(env.log, env.result.run_id).problems
    assert any("MCP 结果正文无法核验" in problem for problem in problems)


@pytest.mark.parametrize("name", ["mcp_blob_written", "mcp_before_result_commit"])
def test_blob_and_commit_failpoints_converge_unknown_without_dangling_result(tmp_path, name):
    env = _start(tmp_path, failpoint_name=name)
    try:
        rows = env.log.read_run(env.result.run_id, verify=False)
        results = [e.payload for e in rows if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0]["effect_state"] == "unknown"
        assert not [e for e in rows if e.type == "receipt"]
        assert not any(
            e.type == "tool.result" and e.payload["effect_state"] == "applied" for e in rows
        )
        assert SENTINEL.encode() not in canonical_bytes([e.payload for e in rows])
        assert len(env.calls.read_text(encoding="utf-8").splitlines()) == 1
        if name == "mcp_blob_written":
            blobs = [path for path in env.stack.snapshots.blob_dir.iterdir() if path.is_file()]
            assert len(blobs) == 1
            assert SENTINEL.encode() in decode_mcp_result(
                blobs[0].read_bytes()
            )["text_parts"][0].encode()
    finally:
        env.clients.close_all()
        env.log.close()


def test_content_grant_rolls_back_with_result_transaction(tmp_path):
    env = _start(tmp_path, transaction_failure=True)
    try:
        results = [
            e.payload for e in env.log.read_run(env.result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert [item["effect_state"] for item in results] == ["unknown"]
        with sqlite3.connect(env.log.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM content_objects").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM content_grants").fetchone()[0] == 0
        assert len(env.calls.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        env.clients.close_all()
        env.log.close()


def test_memory_erase_retains_blob_referenced_by_mcp(completed):
    env = completed
    mcp = _result_payload(env)["output"]["mcp"]
    body = _blob(env).read_bytes()
    digest = mcp["sha256"]
    memory = env.stack.memory
    memory_blob = memory.blob_path(digest)
    assert memory_blob is not None
    memory_blob.parent.mkdir(parents=True, exist_ok=True)
    memory_blob.write_bytes(body)
    scope = MemoryScope.user()
    memory.apply_event("tool.result", {
        "tool_id": "memory.remember", "status": "succeeded", "output": {
            "memory_id": "mem_mcp_shared", "revision": 1,
            **scope.to_dict(), "kind": "fact", "content_ref": digest,
            "source_refs": [], "evidence": [], "proof_count": 0,
            "origin": "user_explicit", "status": "active", "created_at": 1,
            "deduped": False,
        },
    }, seq=999)
    admitted = SimpleNamespace(
        args={"memory_id": "mem_mcp_shared", "revision": 1},
        request=SimpleNamespace(
            run_id=env.result.run_id, call_id="erase-mcp-shared", action=ACTION_EXEC,
            preconditions={},
        ),
    )
    env.stack.dispatcher._memory_projection()
    result = env.stack.dispatcher._memory_executor._erase(admitted, scope)
    assert result.effect_state == "applied"
    assert result.output["erasure"]["deleted"] is False
    assert "mcp:append-only-result" in result.output["erasure"]["retained_by"]
    assert memory_blob.read_bytes() == body
    assert _blob(env).read_bytes() == body


def _called(text: str = "", structured=None):
    return McpCallResult(
        request_id=7, content=({"type": "text", "text": text},), is_error=False,
        structured=structured, duration_ms=3, bytes=len(text.encode()),
    )


def _metadata():
    return {"server": "fake", "tool": "echo", "protocol_version": "2025-06-18"}


@pytest.mark.parametrize(("text", "structured"), [
    ("", None), ("", {}), ("NUL\x00\n中文", {"b": 1, "a": {"x": True}}),
])
def test_canonical_encoding_is_stable(text, structured):
    first, output = encode_mcp_result(_called(text, structured), _metadata())
    reordered = {"a": {"x": True}, "b": 1} if structured else structured
    second, other = encode_mcp_result(_called(text, reordered), _metadata())
    assert first == second
    assert output == other
    assert decode_mcp_result(first)["structured"] == structured
    assert output["size"] == len(first)
    assert output["sha256"] == sha256_hex(first)


def test_structured_and_total_limits_fail_closed():
    with pytest.raises(McpResultError):
        encode_mcp_result(_called(structured={"x": "z" * MAX_STRUCTURED_BYTES}), _metadata())
    with pytest.raises(McpResultError):
        encode_mcp_result(_called("x" * MAX_RESULT_BYTES), _metadata())
    with pytest.raises(McpResultError):
        decode_mcp_result(b" " * (MAX_RESULT_BYTES + 1))


def test_dispatch_oversize_converges_unknown_without_retry_or_body_leak(tmp_path):
    env = _start(tmp_path, config=_config(tmp_path, mode="content_ref_oversize"))
    try:
        sentinel = b"MCP-OVERSIZE-SENTINEL-4c72"
        rows = env.log.read_run(env.result.run_id, verify=False)
        results = [e.payload for e in rows if e.type == "tool.result"]
        assert [item["effect_state"] for item in results] == ["unknown"]
        assert sentinel not in canonical_bytes([e.payload for e in rows])
        cursor, _ = env.stack.tasks.load_cursor(env.result.run_id)
        assert sentinel not in json.dumps(cursor.to_dict(), ensure_ascii=False).encode()
        assert len(env.calls.read_text(encoding="utf-8").splitlines()) == 1
        assert not any(
            sentinel in path.read_bytes()
            for path in env.stack.snapshots.blob_dir.iterdir() if path.is_file()
        )
    finally:
        env.clients.close_all()
        env.log.close()


def test_malformed_canonical_json_and_ref_markers_fail_closed(tmp_path):
    body, output = encode_mcp_result(_called("legacy"), _metadata())
    env = SimpleNamespace(db_path=tmp_path / "events.db", blob_dir=None)
    output = {"mcp": {**output, "blob_kind": "snapshot", "blob_path": "blobs"}}
    blob = tmp_path / "blobs" / output["mcp"]["sha256"]
    blob.parent.mkdir()
    blob.write_bytes(body)
    malformed = body.replace(b'"schema"', b'"schema":"tianshu.mcp-result.v1","schema"', 1)
    blob.write_bytes(malformed)
    with pytest.raises(McpResultError):
        read_mcp_result(output, log=env)
    for mcp in ({"schema": "vendor"}, {"ref": "content://sha256:" + "0" * 64}):
        assert is_ref_output({"mcp": mcp})
        with pytest.raises(McpResultError):
            hydrate_mcp_output({"mcp": mcp}, log=env)
    malformed_ref = deepcopy(output)
    malformed_ref["mcp"]["ref"] = "content://sha256:../outside"
    with pytest.raises(McpResultError):
        read_mcp_result(malformed_ref, log=env)
    absolute_path = deepcopy(output)
    absolute_path["mcp"]["blob_path"] = str(blob.parent)
    with pytest.raises(McpResultError):
        read_mcp_result(absolute_path, log=env)


def test_old_inline_event_cursor_and_receipt_remain_compatible(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        run_id = "run_legacy_mcp"
        call_id = "legacy-call"
        digest = sha256_hex(b"legacy inline result")
        old = {
            "server": "fake", "tool": "echo", "tool_version": "1",
            "request_id": 7, "is_error": False, "duration_ms": 3,
            "after_state": "mcp:" + digest, "content": ["legacy body"],
            "structured": {"old": True}, "unsupported_content": [],
        }
        intent = log.append(
            run_id=run_id, type="tool.intent",
            payload={
                "call_id": call_id, "tool_id": "mcp__fake__echo", "args_hash": "a" * 64,
                "args": {}, "request_id": "req_legacy", "request_digest": "b" * 64,
                "dispatch_id": "dispatch_legacy", "grant_id": "grant_legacy",
                "attempt_id": 1, "claim_epoch": 0,
            },
            actor_kind="model",
        )
        output = {"mcp": old}
        result = ToolResult("succeeded", "applied", output)
        log.append(
            run_id=run_id, type="tool.result", actor_kind="tool",
            payload={
                "call_id": call_id, "tool_id": "mcp__fake__echo", "status": "succeeded",
                "effect_state": "applied", "output": output,
            },
        )
        receipt = build_mcp_receipt(
            run_id=run_id, call_id=call_id, tool_id="mcp__fake__echo", args_hash="a" * 64,
            result=result, resource="mcp://fake/echo", action=ACTION_EXEC,
            intent_seq=intent,
        )
        log.append(
            run_id=run_id, type="receipt", actor_kind="core", payload=receipt.to_payload(),
        )
        assert hydrate_mcp_output(output) == output
        message = {
            "role": "tool", "tool_call_id": call_id, "name": "mcp__fake__echo",
            "content": json.dumps(output, ensure_ascii=False, sort_keys=True),
        }
        cursor = RunCursor(messages=[message])
        assert cursor.to_dict()["messages"] == [message]
        assert hydrate_mcp_messages([message]) == [message]
        assert verify_receipts(log, run_id) == []
    finally:
        log.close()


def _output_for_body(body: bytes, *, blob_path: str = "blobs") -> dict[str, Any]:
    _, output = encode_mcp_result(_called("legacy"), _metadata())
    digest = sha256_hex(body)
    output.update({
        "ref": f"content://sha256:{digest}",
        "size": len(body),
        "sha256": digest,
        "after_state": "mcp:" + digest,
        "blob_path": blob_path,
    })
    return {"mcp": output}


def _append_audited_ref_result(env, output: dict[str, Any], body: bytes, call_id: str) -> None:
    mcp = output["mcp"]
    path = Path(env.log.db_path).parent / mcp["blob_path"] / mcp["sha256"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    original = next(
        event for event in env.log.read_run(env.result.run_id, verify=False)
        if event.type == "tool.intent"
    )
    intent = deepcopy(original.payload)
    intent["call_id"] = call_id
    intent_seq = env.log.append(
        run_id=env.result.run_id, type="tool.intent", payload=intent, actor_kind="model",
    )
    result = ToolResult("succeeded", "applied", output)
    env.log.append(
        run_id=env.result.run_id, type="tool.result", actor_kind="tool",
        payload={
            "call_id": call_id, "tool_id": "mcp__fake__echo", "status": "succeeded",
            "effect_state": "applied", "output": output,
        },
    )
    receipt = build_mcp_receipt(
        run_id=env.result.run_id, call_id=call_id, tool_id="mcp__fake__echo",
        args_hash=intent["args_hash"], result=result, resource="mcp://fake/echo",
        action=ACTION_EXEC, intent_seq=intent_seq,
    )
    env.log.append(
        run_id=env.result.run_id, type="receipt", actor_kind="core", payload=receipt.to_payload(),
    )


def _assert_ref_audit_fails_closed(env, output: dict[str, Any]) -> None:
    with pytest.raises(McpResultError):
        read_mcp_result(output, log=env.log)
    receipt_problems = verify_receipts(env.log, env.result.run_id)
    assert any("MCP canonical blob 无法核验" in problem for problem in receipt_problems)
    report = replay(env.log, env.result.run_id)
    assert not report.ok
    assert any("MCP 结果正文无法核验" in problem for problem in report.problems)


@pytest.mark.parametrize("escape", ["locator", "symlink", "file_symlink", "fallback", "ref"])
def test_outside_ref_and_blob_path_fail_closed_in_all_audits(completed, monkeypatch, escape):
    if escape in {"symlink", "file_symlink"}:
        try:
            probe = completed.root / "symlink-probe"
            target = completed.root / "symlink-target"
            target.write_text("probe")
            probe.symlink_to(
                target if escape == "file_symlink" else completed.root,
                target_is_directory=escape == "symlink",
            )
            probe.unlink()
        except (OSError, NotImplementedError):
            pytest.skip("当前平台不支持 symlink")
    env = completed
    body, _ = encode_mcp_result(_called("legacy"), _metadata())
    outside = env.root / f"../{env.root.name}-outside"
    outside.mkdir()
    target = outside / sha256_hex(body)
    target.write_bytes(body)
    output = _output_for_body(body, blob_path=f"../{outside.name}")
    if escape == "fallback":
        output["mcp"]["blob_path"] = ""
        monkeypatch.setattr(env.log, "_blob_dir", outside)
    elif escape == "symlink":
        link = env.root / "escape-link"
        link.symlink_to(outside, target_is_directory=True)
        output["mcp"]["blob_path"] = link.name
    elif escape == "file_symlink":
        directory = env.root / "blobs"
        directory.mkdir(exist_ok=True)
        (directory / sha256_hex(body)).symlink_to(target)
        output["mcp"]["blob_path"] = directory.name
    elif escape == "ref":
        output["mcp"]["ref"] = "content://sha256:../outside"
    _append_audited_ref_result(env, output, body, f"escape-{escape}")
    _assert_ref_audit_fails_closed(env, output)
    assert target.read_bytes() == body


@pytest.mark.parametrize("location", ["content", "structured"])
def test_surrogate_blob_fails_closed_in_all_audits(completed, location):
    env = completed
    structured = {"x": "legacy"} if location == "structured" else None
    body, _ = encode_mcp_result(_called("legacy", structured), _metadata())
    damaged = body.replace(b"legacy", b"\\ud800")
    assert damaged != body and len(damaged) == len(body)
    output = _output_for_body(damaged)
    with pytest.raises(McpResultError):
        decode_mcp_result(damaged)
    _append_audited_ref_result(env, output, damaged, f"surrogate-{location}")
    _assert_ref_audit_fails_closed(env, output)


@pytest.mark.parametrize("location", ["content", "structured"])
def test_surrogate_encoding_is_normalized(location):
    structured = {"x": "\ud800"} if location == "structured" else None
    text = "\ud800" if location == "content" else "legacy"
    called = McpCallResult(
        request_id=7, content=({"type": "text", "text": text},), is_error=False,
        structured=structured, duration_ms=3, bytes=6,
    )
    with pytest.raises(McpResultError):
        encode_mcp_result(called, _metadata())


def test_legitimate_event_and_snapshot_blob_locators_remain_readable(tmp_path):
    body, _ = encode_mcp_result(_called("legacy"), _metadata())
    log = SimpleNamespace(db_path=tmp_path / "events.db", blob_dir=tmp_path / "event-blobs")
    for kind, directory in (("event", log.blob_dir), ("snapshot", tmp_path / "snapshot-blobs")):
        directory.mkdir()
        (directory / sha256_hex(body)).write_bytes(body)
        output = _output_for_body(body, blob_path=directory.name)
        output["mcp"]["blob_kind"] = kind
        envelope, actual = read_mcp_result(output, log=log)
        assert actual == body and envelope["text_parts"] == ["legacy"]
