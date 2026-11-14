"""MCP 派发链路（G2 MCP 阶段 PR D 的判据 c8–c13）。

对端是 `tests/fixtures/fake_mcp_server.py` 真子进程；模型是 `ScriptedModel`，
所以整条路是"脚本模型 → 主循环 → dispatcher → stdio server → 事件/凭据"，
中间没有替身。
"""

from __future__ import annotations

import sys
from pathlib import Path

from support import approve_pending, engine_for

from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import verify_receipts
from tianshu.replay import replay
from tianshu.tools import SPECS, tool_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


# ---------------------------------------------------------------- 夹具


def _config(name: str = "fake", *args: str, **kwargs) -> McpServerConfig:
    return McpServerConfig(
        name=name, command=(sys.executable, str(FIXTURE), *args), **kwargs
    )


def _stack(tmp_path, *, configs: dict[str, McpServerConfig] | None = None):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    clients = McpClients(configs or {"fake": _config()})
    stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
    return log, stack, clients


def _task(stack, *, servers=("fake",), ws=None, isolation=None):
    from tianshu.execpolicy import ExecPolicy

    bindings = [str(ws)] if ws is not None else []
    policy = None
    if isolation is not None:
        policy = ExecPolicy(isolation_requires=list(isolation))
    return stack.tasks.create_task(
        "用 MCP 工具", bindings, exec_policy=policy, mcp_servers=list(servers)
    )


def _script(*calls: tuple[str, dict], final: str = "写完了。") -> ScriptedModel:
    responses = [
        ModelResponse("tool_use", tool_calls=[ToolCall(f"c{i}", tool, args)])
        for i, (tool, args) in enumerate(calls, start=1)
    ]
    responses.append(ModelResponse("end_turn", content=final))
    return ScriptedModel(responses)


def _drive(stack, engine, task, *, rounds=6):
    result = engine.start(task.task_id, out_name="out.md")
    while result.state == "waiting_approval" and rounds > 0:
        rounds -= 1
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    return result


def _finish(stack, engine, result, *, rounds=6):
    """从当前状态继续批准/恢复到 Run 结束（不新建 Run）。"""
    while result.state == "waiting_approval" and rounds > 0:
        rounds -= 1
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    return result


def _results(log, run_id) -> dict[str, dict]:
    return {
        e.payload["call_id"]: e.payload
        for e in log.read_run(run_id, verify=False)
        if e.type == "tool.result"
    }


def _rejected(log, run_id) -> list[dict]:
    return [
        e.payload
        for e in log.read_run(run_id, verify=False)
        if e.type == "dispatch.rejected"
    ]


def _ws(tmp_path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


# ---------------------------------------------------------------- c8：任务白名单


def test_mcp_call_without_task_allowlist_is_rejected_before_approval(tmp_path):
    log, stack, clients = _stack(tmp_path)
    try:
        task = _task(stack, servers=())
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state != "waiting_approval", "没声明 server 不该弹批准"
        rejected = _rejected(log, result.run_id)
        assert [row["rule_id"] for row in rejected] == ["c4.mcp_not_allowed"]
        assert result.effect_state == "none_applied"
        # 拒绝还是要有 tool.result（"为什么不派发"不能只活在日志里），
        # 但**没有 intent、没有凭据、没有批准**。
        rows = _results(log, result.run_id)
        assert [row["status"] for row in rows.values()] == ["rejected"]
        types = [e.type for e in log.read_run(result.run_id, verify=False)]
        assert "tool.intent" not in types
        assert "receipt" not in types
        assert not stack.approvals.pending(run_id=result.run_id)
    finally:
        clients.close_all()


# ---------------------------------------------------------------- c9：批准 → 执行 → 事件 + 凭据


def test_approved_mcp_call_runs_once_and_produces_receipt(tmp_path):
    calls = tmp_path / "calls.jsonl"
    log, stack, clients = _stack(
        tmp_path, configs={"fake": _config("fake", "--call-log", str(calls))}
    )
    try:
        task = _task(stack, ws=_ws(tmp_path))
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval"
        pending = stack.approvals.pending(run_id=result.run_id)
        assert [list(row.resources) for row in pending] == [["mcp://fake/echo"]]
        assert pending[0].action == "exec"
        approval_events = [
            e
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "approval.requested"
        ]
        assert [e.payload["rule_id"] for e in approval_events] == ["c4.mcp_call"]
        # MCP 审批卡不冒充"后端隔离覆盖了它"：不带 isolation 视图（那是 exec 的）。
        assert "isolation" not in approval_events[0].payload
        result = _finish(stack, engine, result)
        assert result.state in ("succeeded", "failed")
        rows = _results(log, result.run_id)
        assert len(rows) == 1
        payload = next(iter(rows.values()))
        assert payload["status"] == "succeeded"
        assert payload["effect_state"] == "applied"
        mcp = payload["output"]["mcp"]
        assert mcp["server"] == "fake" and mcp["tool"] == "echo"
        assert mcp["content"] == ["echo:hi"]
        assert mcp["unsupported_content"] == ["image"]
        assert mcp["after_state"].startswith("mcp:")
        assert mcp["spawn"]["via_exec_backend"] is False
        # 调用确实只发生一次（真子进程的日志是外部可观察证据）
        assert len(calls.read_text(encoding="utf-8").splitlines()) == 1
        # 凭据：backend=mcp、证据含 mcp_call，且 verify_receipts 认为它可核对
        receipt_events = [
            e for e in log.read_run(result.run_id, verify=False) if e.type == "receipt"
        ]
        assert len(receipt_events) == 1
        receipt = receipt_events[0].payload
        assert receipt["backend"] == "mcp"
        assert receipt["before_state"] == "unknown"
        assert {ref["kind"] for ref in receipt["evidence"]} >= {"result", "intent", "mcp_call"}
        mcp_ref = next(ref for ref in receipt["evidence"] if ref["kind"] == "mcp_call")
        assert mcp_ref["server"] == "fake" and mcp_ref["tool"] == "echo"
        # 响应摘要必须进证据：只绑 server/tool/request_id 核对不了"结果是不是同一份"。
        assert len(mcp_ref["sha256"]) == 64
        assert receipt["after_state"] == "mcp:" + mcp_ref["sha256"]
        assert verify_receipts(log, result.run_id) == []
    finally:
        clients.close_all()


def test_is_error_maps_to_failed_but_applied(tmp_path):
    log, stack, clients = _stack(
        tmp_path, configs={"fake": _config("fake", "--mode", "is_error")}
    )
    try:
        task = _task(stack, ws=_ws(tmp_path))
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = _drive(stack, engine, task)
        payload = next(iter(_results(log, result.run_id).values()))
        assert payload["status"] == "failed"
        assert payload["effect_state"] == "applied"
        assert payload["output"]["mcp"]["is_error"] is True
    finally:
        clients.close_all()


# ---------------------------------------------------------------- c10：超时 = unknown


def test_call_timeout_is_unknown_not_none_applied(tmp_path):
    log, stack, clients = _stack(
        tmp_path,
        configs={
            "fake": _config("fake", "--mode", "hang_on_call", call_timeout_ms=200)
        },
    )
    try:
        task = _task(stack, ws=_ws(tmp_path))
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = _drive(stack, engine, task)
        payload = next(iter(_results(log, result.run_id).values()))
        assert payload["status"] == "failed"
        assert payload["effect_state"] == "unknown", "超时可能已经执行过，不能说没生效"
        assert payload["output"]["reason"] == "mcp.timeout"
        # unknown 不产凭据（effect_state != applied）
        assert not [e for e in log.read_run(result.run_id, verify=False) if e.type == "receipt"]
    finally:
        clients.close_all()


# ---------------------------------------------------------------- c14-c13：server 不可用 / 隔离


def test_unavailable_server_is_rejected_before_approval(tmp_path):
    log, stack, clients = _stack(tmp_path)
    try:
        task = _task(stack, ws=_ws(tmp_path))
        from tianshu.run_policy import EFFECTS

        stack.dispatcher.prepare_run(task.task_id, EFFECTS)
        clients.close_all()  # server 死了，但注册表里还有它的工具
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state != "waiting_approval"
        rules = [row["rule_id"] for row in _rejected(log, result.run_id)]
        assert rules == ["dispatch.mcp_server_unavailable"]
    finally:
        clients.close_all()


def test_isolation_requires_makes_mcp_unproven(tmp_path):
    log, stack, clients = _stack(tmp_path)
    try:
        task = _task(stack, ws=_ws(tmp_path), isolation=("files_authority",))
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state != "waiting_approval"
        rules = [row["rule_id"] for row in _rejected(log, result.run_id)]
        assert rules == ["c4.mcp_isolation_unproven"]
    finally:
        clients.close_all()


# ---------------------------------------------------------------- 取消（c11）


def test_cancelled_run_sends_no_mcp_frames(tmp_path):
    calls = tmp_path / "calls.jsonl"
    log, stack, clients = _stack(
        tmp_path, configs={"fake": _config("fake", "--call-log", str(calls))}
    )
    try:
        task = _task(stack, ws=_ws(tmp_path))
        stack.dispatcher._cancel_requested = lambda run_id: True
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = _drive(stack, engine, task)
        payload = next(iter(_results(log, result.run_id).values()))
        assert payload["status"] == "failed"
        assert payload["effect_state"] == "none_applied"
        assert payload["output"]["reason"] == "mcp.cancelled"
        assert not calls.exists() or calls.read_text(encoding="utf-8") == "", (
            "取消的 Run 一条 tools/call 都不该发出去"
        )
    finally:
        clients.close_all()


# ---------------------------------------------------------------- 工具面（c1）


def test_manifest_and_run_start_record_mcp_tools(tmp_path):
    _log, stack, clients = _stack(tmp_path)
    try:
        task = _task(stack, ws=_ws(tmp_path))
        from tianshu.run_policy import EFFECTS

        stack.dispatcher.prepare_run(task.task_id, EFFECTS)
        ids = {row["name"] for row in tool_manifest()}
        assert {"mcp__fake__echo", "mcp__fake__add"} <= ids
        engine = engine_for(stack, _script())
        result = engine.start(task.task_id, out_name="out.md")
        start = next(
            e for e in _log.read_run(result.run_id, verify=False) if e.type == "run.start"
        )
        assert "mcp__fake__echo" in start.payload["tools"]
        assert "read_text" in start.payload["tools"]
    finally:
        clients.close_all()


def test_without_mcp_config_the_surface_is_byte_identical(tmp_path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "data")
    try:
        assert tool_manifest() == [
            {
                "name": spec.tool_id,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in SPECS.values()
            if spec.exposed
        ]
        task = _task(stack, servers=())
        engine = engine_for(stack, _script())
        result = engine.start(task.task_id, out_name="out.md")
        start = next(
            e for e in log.read_run(result.run_id, verify=False) if e.type == "run.start"
        )
        assert start.payload["tools"] == sorted(SPECS)
    finally:
        log.close()


# ---------------------------------------------------------------- c15：回放只读


def test_replay_does_not_talk_to_the_server(tmp_path):
    calls = tmp_path / "calls.jsonl"
    log, stack, clients = _stack(
        tmp_path, configs={"fake": _config("fake", "--call-log", str(calls))}
    )
    try:
        task = _task(stack, ws=_ws(tmp_path))
        engine = engine_for(stack, _script(("mcp__fake__echo", {"message": "hi"})))
        result = _drive(stack, engine, task)
        before = calls.read_text(encoding="utf-8")
        clients.close_all()
        report = replay(log, result.run_id)
        assert report.ok, report.problems
        assert calls.read_text(encoding="utf-8") == before
    finally:
        clients.close_all()


# ---------------------------------------------------------------- 任务白名单存储


def test_task_records_mcp_servers_in_row_and_event(tmp_path):
    log, stack, clients = _stack(tmp_path)
    try:
        task = _task(stack, servers=("fake", "fake", "other"))
        assert task.mcp_servers == ("fake", "other"), "排序去重，声明可复现"
        event = next(e for e in log.read_types({"task.created"}))
        assert event.payload["mcp_servers"] == ["fake", "other"]
    finally:
        clients.close_all()


# ---------------------------------------------------------------- c15：装配探针（复审 v5）


class _RecordingClients:
    """只记录 `start_all()` 调用次数的装配探针（形态与 `McpClients` 对齐）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.configs: dict = {}

    def start_all(self) -> None:
        self.calls += 1

    def states(self):
        return ()

    def server(self, name):
        return None

    def close_all(self) -> None:
        pass


def test_replay_does_not_construct_or_start_clients(tmp_path):
    """回放不碰 MCP：装配探针只在新 effects Run 预检通过后启动一次。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    probe = _RecordingClients()
    stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=probe)
    try:
        assert probe.calls == 0, "只装配不能启动客户端"
        task = _task(stack, servers=())
        engine = engine_for(stack, _script())
        result = engine.start(task.task_id, out_name="out.md")
        assert probe.calls == 1, "新 effects Run 预检通过后应当启动一次"
        report = replay(log, result.run_id)
        assert report.ok, report.problems
        assert probe.calls == 1, "回放不该启动/构造 MCP 客户端"
    finally:
        log.close()
