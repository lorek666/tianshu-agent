"""File delivery is scoped to committed writes from the requested run."""

from contextlib import closing

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall


def test_preview_returns_exact_run_output_after_the_file_changes(tmp_path):
    from tianshu.task_files import ArtifactNotFound, list_artifacts, preview_artifact

    original = "第一版报告\n"
    ws = tmp_path / "work"
    ws.mkdir()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("保存报告", [str(ws)])
        engine = engine_for(stack, ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "create_report", {"name": "report.md", "content": original}),
                ToolCall("c2", "create_report", {"name": "extra.md", "content": "附件"}),
            ]),
            ModelResponse("end_turn"),
        ]))
        run = engine.start(task.task_id, out_name="")
        assert (ws / "report.md").read_bytes() == original.encode()
        artifacts = list_artifacts(log, run.run_id)
        assert [item["path"] for item in artifacts] == ["report.md", "extra.md"]
        first = artifacts[0]
        assert first["bytes"] == 16
        plane = ControlPlane(stack, model=ScriptedModel([]))
        assert plane.handle("artifact.list", {"run_id": run.run_id}) == {
            "artifacts": artifacts, "truncated": False,
        }

        # The product previews the verified run output, never a replacement path.
        (ws / "report.md").write_bytes(b"This is no longer the run's output")
        assert preview_artifact(log, run.run_id, first["artifact_id"]) == {
            **first, "text": original, "truncated": False,
        }
        assert plane.handle("artifact.preview", {
            "run_id": run.run_id, "artifact_id": first["artifact_id"],
        }) == {"artifact": {**first, "text": original, "truncated": False}}
        other = engine.start(task.task_id, out_name="")
        assert list_artifacts(log, other.run_id) == []
        with pytest.raises(ArtifactNotFound):
            preview_artifact(log, other.run_id, first["artifact_id"])
        with pytest.raises(ArtifactNotFound):
            preview_artifact(log, run.run_id, str(ws / "report.md"))
        with pytest.raises(ControlError) as refused:
            plane.handle("artifact.preview", {
                "run_id": other.run_id, "artifact_id": first["artifact_id"],
            })
        assert refused.value.code == "not_found"


def test_preview_limits_bytes_without_splitting_utf8(tmp_path):
    from tianshu.task_files import list_artifacts, preview_artifact

    output = "x" * (65536 - 1) + "汉字"
    ws = tmp_path / "work"
    ws.mkdir()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("保存报告", [str(ws)])
        run = engine_for(stack, ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "create_report", {"name": "report.md", "content": output}),
            ]),
            ModelResponse("end_turn"),
        ])).start(task.task_id, out_name="")
        first, = list_artifacts(log, run.run_id)
        assert preview_artifact(log, run.run_id, first["artifact_id"])["text"] == "x" * 65535
        assert preview_artifact(log, run.run_id, first["artifact_id"])["truncated"] is True


def test_only_applied_writes_become_file_artifacts(tmp_path):
    from tianshu.task_files import list_artifacts, preview_artifact

    ws = tmp_path / "work"
    ws.mkdir()
    report = ws / "report.md"
    report.write_bytes(b"before")
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("修订报告", [str(ws)])
        engine = engine_for(stack, ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("c1", "create_report", {"name": "report.md", "content": "rejected"}),
                ToolCall("c2", "modify_text", {"path": "report.md", "content": "before"}),
                ToolCall("c3", "modify_text", {"path": "report.md", "content": "after"}),
            ]),
            ModelResponse("end_turn"),
        ]))
        run = engine.start(task.task_id, out_name="")
        assert list_artifacts(log, run.run_id) == []
        while approve_pending(stack, run.run_id):
            engine.resume_run(run.run_id)

        results = [
            e.payload["effect_state"] for e in log.read_run(run.run_id) if e.type == "tool.result"
        ]
        assert results == ["none_applied", "none_applied", "applied"]
        assert report.read_bytes() == b"after"
        artifact, = list_artifacts(log, run.run_id)
        assert artifact["tool_id"] == "modify_text"
        assert preview_artifact(log, run.run_id, artifact["artifact_id"])["text"] == "after"
