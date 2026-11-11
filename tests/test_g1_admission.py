"""G1a：§5.2.3 通用工具准入（施工规格 `docs/SPEC-G1-ADMISSION-2026-11-11.md` §2）。

钉住七件事，每件都对应 PRD 的一句原文：

1. **完整 schema**：嵌套对象 / 数组 / 约束在执行前被执行（旧代码只看顶层 type）。
2. **不支持的方言拒绝准入**：`oneOf` / `$ref` / `format` 等注册期报错，不静默忽略。
3. **工具身份绑定**：版本 + schema + 描述；改描述让旧批准失效，改回去又匹配。
4. **历史摘要不变**：没有 `description_hash` 的旧请求，规范字典逐字节不变。
5. **执行上下文由系统提供**：模型不能把 run/attempt/预算身份塞进参数。
6. **URL 不伪装成文件路径**：路径参数里出现 `scheme://` 直接拒绝，不拼进工作区。
7. **replay 只读已保存结果**：改掉 / 删掉当前工具清单，历史 run 仍然能回放。

负控（每条都要能打红）在 `docs/SPEC-G1-ADMISSION-2026-11-11.md` §4 列出；
本文件里的断言都刻意写成"回退修复就红"的形状，不做宽松匹配。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from support import stack_for

from tianshu import tools, toolschema
from tianshu.dispatcher import build_request
from tianshu.errors import ToolArgumentError, ToolSchemaError
from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ActionRequest, ApprovalStore, PolicyEngine
from tianshu.replay import replay
from tianshu.workspace import WorkspaceBinding


def _ws(tmp_path: Path, files: dict[str, str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in files.items():
        (ws / name).write_bytes(content.encode("utf-8"))
    return ws


def _events(log: EventLog, run_id: str) -> list:
    return log.read_run(run_id)


# ---------------------------------------------------------------- 1 完整 schema


def test_nested_array_items_are_validated_before_execution():
    """`run_command.argv` 的 `items: {type: string}` 以前从未被执行。"""
    schema = tools.SPECS["run_command"].input_schema
    with pytest.raises(ToolArgumentError) as caught:
        tools.validate_args({"argv": [1]}, schema)
    message = str(caught.value)
    assert "argv[0]" in message, message
    assert "期望 string" in message and "integer" in message


def test_schema_constraints_are_enforced():
    schema = tools.SPECS["run_command"].input_schema
    with pytest.raises(ToolArgumentError, match="至少需要 1 个元素"):
        tools.validate_args({"argv": []}, schema)

    memory = tools.SPECS["memory.search"].input_schema
    with pytest.raises(ToolArgumentError, match="不能大于 5"):
        tools.validate_args({"query": "x", "limit": 99}, memory)
    with pytest.raises(ToolArgumentError, match="不能小于 1"):
        tools.validate_args({"query": "x", "limit": 0}, memory)

    # 自定义 schema：enum / pattern / minLength 也必须真的执行（不是只写在注册表里）。
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "name": {"type": "string", "minLength": 2, "pattern": "^[a-z]+$"},
            "nested": {
                "type": "object",
                "properties": {"flag": {"type": "boolean"}},
                "required": ["flag"],
                "additionalProperties": False,
            },
        },
        "required": ["mode", "name", "nested"],
        "additionalProperties": False,
    }
    with pytest.raises(ToolArgumentError, match="不在允许的取值里"):
        tools.validate_args({"mode": "c", "name": "ok", "nested": {"flag": True}}, schema)
    with pytest.raises(ToolArgumentError, match="长度至少 2"):
        tools.validate_args({"mode": "a", "name": "x", "nested": {"flag": True}}, schema)
    with pytest.raises(ToolArgumentError, match="不匹配 pattern"):
        tools.validate_args({"mode": "a", "name": "AB", "nested": {"flag": True}}, schema)
    with pytest.raises(ToolArgumentError, match="nested"):
        tools.validate_args({"mode": "a", "name": "ok", "nested": {}}, schema)


def test_invalid_args_are_rejected_at_admission_not_inside_the_tool(tmp_path):
    """非法参数不该走到审批：用户不该被要求批准一个已经被判死形的调用。"""
    ws = _ws(tmp_path, {"a.md": "# 甲\n"})
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse(
            "tool_use",
            tool_calls=[ToolCall("c1", "create_report", {"name": "out.md", "content": 5})],
        ),
        ModelResponse("end_turn", content="参数不合法，我先停下。"),
    ])
    try:
        result = run(
            task="写一份报告", workspace_dir=ws, out_name="out.md",
            model=model, log=log, limits=Limits(4, 20_000),
        )
        events = _events(log, result.run_id)
        not_dispatched = [e for e in events if e.type == "tool.not_dispatched"]
        assert not_dispatched, [e.type for e in events]
        reason = not_dispatched[0].payload["reason"]
        assert "参数不满足工具契约" in reason, reason
        assert "content" in reason and "integer" in reason, reason
        # 关键：没有任何审批被创建，也没有文件被写。
        assert not (ws / "out.md").exists()
        assert not [e for e in events if e.type == "approval.requested"]
    finally:
        log.close()


# ---------------------------------------------------------------- 2 方言拒绝


@pytest.mark.parametrize(
    ("schema", "keyword"),
    [
        ({"type": "object", "oneOf": []}, "oneOf"),
        ({"type": "object", "properties": {"x": {"type": "string", "$ref": "#/a"}}}, "$ref"),
        ({"type": "object", "properties": {"x": {"type": "string", "format": "email"}}}, "format"),
        ({"type": "object", "properties": {"x": {"type": "object", "allOf": []}}}, "allOf"),
        ({"type": "object", "properties": {"x": {"type": "string", "default": "a"}}}, "default"),
    ],
)
def test_registry_rejects_unsupported_keywords(schema, keyword):
    with pytest.raises(ToolSchemaError) as caught:
        toolschema.compile_schema(schema, where="probe")
    assert keyword in str(caught.value), str(caught.value)


def test_registry_rejects_non_boolean_additional_properties():
    with pytest.raises(ToolSchemaError, match="additionalProperties"):
        toolschema.compile_schema(
            {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}},
            where="probe",
        )


def test_validate_registry_rejects_a_bad_spec_but_accepts_builtins():
    tools.validate_registry()  # 内置注册表必须过
    bad = dataclasses.replace(
        tools.SPECS["read_text"],
        input_schema={"type": "object", "properties": {"path": {"type": "string", "oneOf": []}}},
    )
    with pytest.raises(ToolSchemaError) as caught:
        tools.validate_registry({"read_text": bad})
    assert "read_text" in str(caught.value) and "oneOf" in str(caught.value)


# ---------------------------------------------------------------- 3 身份字段


def test_registry_rejects_identity_fields_declared_as_arguments():
    with pytest.raises(ToolSchemaError, match="run_id"):
        toolschema.compile_schema(
            {"type": "object", "properties": {"run_id": {"type": "string"}}},
            where="probe",
        )
    with pytest.raises(ToolSchemaError, match="attempt_id"):
        toolschema.compile_schema(
            {"type": "object", "properties": {"o": {"type": "object", "properties": {
                "attempt_id": {"type": "integer"}}}}},
            where="probe",
        )


def test_identity_fields_are_rejected_even_when_schema_allows_extra_args():
    """`additionalProperties` 省略 = 允许额外键；身份字段仍然必须被拒。"""
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    with pytest.raises(ToolArgumentError, match="身份字段"):
        tools.validate_args({"query": "x", "run_id": "run_1"}, schema)
    with pytest.raises(ToolArgumentError, match="身份字段"):
        tools.validate_args({"query": "x", "budget": {"tokens": 1}}, schema)


# ---------------------------------------------------------------- 4 身份摘要绑定


def _request(**overrides) -> ActionRequest:
    base = dict(
        request_id="req_1",
        task_id="task_1",
        run_id="run_1",
        job_id="job_1",
        claim_epoch=1,
        owner_instance_id="inst_1",
        goal_revision=1,
        lease_epoch=1,
        call_id="call_1",
        attempt_id=1,
        tool_id="create_report",
        tool_version="1.0",
        schema_hash="schema_abc",
        action="create",
        resources=("/ws/a.md",),
        args_hash="args_abc",
        description_hash=tools.SPECS["create_report"].description_digest(),
    )
    base.update(overrides)
    return ActionRequest(**base)


def test_description_change_invalidates_the_old_approval(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    try:
        approvals = ApprovalStore(log)
        policy = PolicyEngine()
        ws = tmp_path / "ws"
        ws.mkdir()
        bindings = [WorkspaceBinding(str(ws))]
        request = _request(resources=(str(ws / "a.md"),), action="overwrite")
        decision = policy.classify("overwrite", list(request.resources), bindings)
        assert decision.needs_approval, decision
        approvals.request(request, decision)
        assert approvals.find_for(request) is not None

        original = tools.SPECS["create_report"].description
        changed = dataclasses.replace(
            tools.SPECS["create_report"], description=original + "（改过的说明）"
        )
        moved = dataclasses.replace(request, description_hash=changed.description_digest())
        assert moved.action_digest() != request.action_digest()
        assert approvals.find_for(moved) is None, "改描述后旧批准必须失效"

        # 负控的另一半：把描述改回原文，旧批准又能匹配（证明原因就是描述，
        # 而不是别的东西顺带变了）。
        restored = dataclasses.replace(
            moved, description_hash=tools.SPECS["create_report"].description_digest()
        )
        assert approvals.find_for(restored) is not None
    finally:
        log.close()


def test_legacy_request_digest_is_byte_identical():
    """没有 `description_hash` 的历史请求：规范字典与摘要都不能变。

    这里钉的是**兼容性**：为了一条新要求把历史库里的审批 / grant / 派发记录
    集体作废，不是"更安全"，是把既有授权链扔掉。所以摘要值直接钉死。
    """
    legacy = _request(description_hash="")
    canonical = legacy.to_canonical_dict()
    assert "description_hash" not in canonical
    assert legacy.digest() == "f733806010cec199fed6f5d9e66ea6dc43e462bac4d17076c4dc84ab912a7f0d"
    assert dataclasses.replace(legacy, description_hash="x" * 64).digest() != legacy.digest()


def test_request_binds_version_schema_and_description(tmp_path):
    """PRD 原文「任务绑定固定工具版本、schema 与描述摘要」——三项都要进摘要。"""
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log)
    try:
        task = stack.tasks.create_task("写报告", [{"root_path": str(ws)}])
        run = stack.tasks.start_run(task.task_id)
        stack.leases.acquire([ws], run.run_id)
        claim = stack.claims.claim(stack.claims.enqueue(run.run_id).job_id)
        stack.tasks.transition(run.run_id, "running", claim=claim)
        spec = tools.SPECS["create_report"]
        request = build_request(
            task=task, run=run, claim=claim, call_id="c1",
            tool_id="create_report", target=ws / "out.md",
            args={"name": "out.md", "content": "内容"},
        )
        assert request.tool_version == spec.version
        assert request.schema_hash == tools.schema_digest(spec.input_schema)
        assert request.description_hash == spec.description_digest()
        base = request.action_digest()
        for changed in (
            dataclasses.replace(request, tool_version="1.1"),
            dataclasses.replace(request, schema_hash="0" * 64),
            dataclasses.replace(request, description_hash="1" * 64),
        ):
            assert changed.action_digest() != base, changed
    finally:
        log.close()


# ---------------------------------------------------------------- 5 URL ≠ 路径


def test_uri_like_path_argument_is_not_a_file_path():
    with pytest.raises(ToolArgumentError, match="不接受 URI"):
        tools.reject_uri_path("https://example.com/a", tool_id="read_text")
    # 不是 URI 的普通名字不许被误伤。
    tools.reject_uri_path("a.md", tool_id="read_text")
    tools.reject_uri_path("notes/2026/11.md", tool_id="read_text")


def test_uri_path_is_rejected_at_execution_without_touching_the_workspace(tmp_path):
    ws = _ws(tmp_path, {"a.md": "# 甲\n"})
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse(
            "tool_use",
            tool_calls=[ToolCall("c1", "read_text", {"path": "https://example.com/a"})],
        ),
        ModelResponse("end_turn", content="读不到，先停。"),
    ])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log, limits=Limits(4, 20_000),
        )
        events = _events(log, result.run_id)
        results = [e for e in events if e.type == "tool.result"]
        errors = [e.payload["output"].get("error", "") for e in results]
        assert any("不接受 URI" in err for err in errors), errors
        # 不能被拼成 `$WS/https:/example.com/a` 这种"看起来访问过网络"的目录。
        assert not (ws / "https:").exists()
        assert [p.name for p in ws.iterdir()] == ["a.md"]
    finally:
        log.close()


# ---------------------------------------------------------------- 6 replay


def test_replay_reads_saved_events_not_the_current_tool_registry(tmp_path):
    """回放只读已保存结果：工具清单换了，历史 run 的判定不能跟着变。"""
    ws = _ws(tmp_path, {"a.md": "# 甲\n\n事件日志是唯一事实源。\n"})
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel([
        ModelResponse("tool_use", tool_calls=[ToolCall("c1", "read_text", {"path": "a.md"})]),
        ModelResponse("end_turn", content="读完了。"),
    ])
    try:
        result = run(
            task="读一下", workspace_dir=ws, out_name="out.md",
            model=model, log=log, limits=Limits(4, 20_000),
        )
        assert replay(log, result.run_id).ok

        original = dict(tools.SPECS)
        try:
            tools.SPECS["read_text"] = dataclasses.replace(
                original["read_text"],
                description="换过的描述",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "minLength": 99}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
            del tools.SPECS["list_files"]
            assert replay(log, result.run_id).ok
        finally:
            tools.SPECS.clear()
            tools.SPECS.update(original)
    finally:
        log.close()


def test_build_request_binds_the_current_description(tmp_path):
    """派发层真的把描述摘要传下去了吗？（不是测试自己构造的假请求）"""
    ws = _ws(tmp_path, {})
    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log)
    try:
        task = stack.tasks.create_task("写报告", [{"root_path": str(ws)}])
        run = stack.tasks.start_run(task.task_id)
        stack.leases.acquire([ws], run.run_id)
        claim = stack.claims.claim(stack.claims.enqueue(run.run_id).job_id)
        stack.tasks.transition(run.run_id, "running", claim=claim)

        args = {"name": "out.md", "content": "内容"}
        request = build_request(
            task=task, run=run, claim=claim, call_id="c1",
            tool_id="create_report", target=ws / "out.md", args=args,
        )
        assert request.description_hash == tools.SPECS["create_report"].description_digest()
        assert request.description_hash, "派发请求必须绑住描述摘要"

        original = tools.SPECS["create_report"]
        try:
            tools.SPECS["create_report"] = dataclasses.replace(
                original, description=original.description + "（改过）"
            )
            again = build_request(
                task=task, run=run, claim=claim, call_id="c1",
                tool_id="create_report", target=ws / "out.md", args=args,
            )
            assert again.description_hash != request.description_hash
            assert again.action_digest() != request.action_digest()
        finally:
            tools.SPECS["create_report"] = original
    finally:
        log.close()
