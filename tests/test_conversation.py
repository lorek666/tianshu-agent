from contextlib import closing
from copy import deepcopy

import pytest
from support import engine_for, stack_for

from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.replay import replay


class RecordingModel(ScriptedModel):
    def __init__(self):
        super().__init__([ModelResponse("end_turn", "上一轮原话：第二点是蓝色。"),
                          ModelResponse("end_turn", "扩写完成。"),
                          ModelResponse("end_turn", "第三轮完成。")])
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return super().complete(messages, tools)


def test_followup_sends_prior_answer_under_assistant_role_and_replays(tmp_path):
    model = RecordingModel()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=model)
        task = plane.handle("task.create", {"goal": "只回复两条文字，不读取文件。"})["task"]
        engine = engine_for(stack, model)
        rid = plane.handle("run.start", {"task_id": task["task_id"]})["run"]["run_id"]
        assert engine.resume_run(rid).terminal
        assert "Markdown 与纯文本" not in model.requests[0][0]["content"]
        result = plane.handle("run.follow_up", {"task_id": task["task_id"],
                                                "source_run_id": rid, "message": "扩写第二点。"})
        run = result["run"]
        assert run["run_id"] != rid
        assert engine.resume_run(run["run_id"]).terminal
        request = model.requests[1]
        assert request[-2:] == [
            {"role": "assistant", "content": "上一轮原话：第二点是蓝色。"},
            {"role": "user", "content": "扩写第二点。"},
        ]
        assert stack.tasks.get_task(task["task_id"]).goal == task["goal"]
        assert replay(log, run["run_id"]).ok
        assert replay(log, rid).ok


def test_followup_cannot_use_run_from_different_task(tmp_path):
    model = RecordingModel()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=model)
        task = stack.tasks.create_task("源")
        other = stack.tasks.create_task("另一任务")
        source = engine_for(stack, model).start(task.task_id)
        with pytest.raises(ControlError) as caught:
            plane.handle("run.follow_up", {"task_id": other.task_id,
                                           "source_run_id": source.run_id, "message": "继续"})
        assert caught.value.code == "bad_params"
        assert not stack.tasks.list_runs(other.task_id)


def test_followup_replays_all_committed_answers_on_selected_branch(tmp_path):
    model = RecordingModel()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=model)
        task = stack.tasks.create_task("描述颜色")
        engine = engine_for(stack, model)
        source = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
        assert engine.resume_run(source).terminal
        for text in ("扩写。", "对比两轮。"):
            source = plane.handle("run.follow_up", {
                "task_id": task.task_id, "source_run_id": source, "message": text,
            })["run"]["run_id"]
            assert engine.resume_run(source).terminal
            assert replay(log, source).ok
        assert model.requests[-1][1:] == [
            {"role": "user", "content": "描述颜色"},
            {"role": "assistant", "content": "上一轮原话：第二点是蓝色。"},
            {"role": "user", "content": "扩写。"},
            {"role": "assistant", "content": "扩写完成。"},
            {"role": "user", "content": "对比两轮。"},
        ]


def test_followup_rechecks_goal_revision_in_start_transaction(tmp_path, monkeypatch):
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        model = RecordingModel()
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=model)
        task = stack.tasks.create_task("旧目标")
        run = engine_for(stack, model).start(task.task_id)
        original_start = plane._run_start

        def race(*args, **kwargs):
            stack.tasks.revise_task(task.task_id, goal="新目标")
            return original_start(*args, **kwargs)

        monkeypatch.setattr(plane, "_run_start", race)
        with pytest.raises(ControlError) as caught:
            plane.handle("run.follow_up", {
                "task_id": task.task_id, "source_run_id": run.run_id, "message": "继续",
            })
        assert caught.value.code == "conflict"
        assert [item.run_id for item in stack.tasks.list_runs(task.task_id)] == [run.run_id]


@pytest.mark.parametrize("attachment_size", [32, 200 * 1024])
def test_attachment_bytes_follow_the_run_and_stay_out_of_event_view(tmp_path, attachment_size):
    import base64

    model = RecordingModel()
    original = "附件里的资料：蓝色。".encode().ljust(attachment_size, b"x")
    encoded = base64.b64encode(original).decode()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=model)
        task = stack.tasks.create_task("读附件")
        run_id = plane.handle("run.start", {"task_id": task.task_id, "attachments": [{
            "name": "notes.txt", "media_type": "text/plain", "data_base64": encoded,
        }]})["run"]["run_id"]
        if attachment_size == 200 * 1024:
            recorded, = [e for e in log.read_run(run_id) if e.type == "run.attachments"]
            assert (tmp_path / "blobs" / recorded.payload_hash).is_file()
        assert engine_for(stack, model).resume_run(run_id).terminal
        [item] = model.requests[-1][-1]["attachments"]
        assert item["data_base64"] == encoded
        assert [e.type for e in log.read_run(run_id)][:3] == [
            "run.start", "run.conversation", "run.attachments",
        ]
        view = plane.handle("events.run", {"run_id": run_id})["events"]
        [file_event] = [e for e in view if e["type"] == "run.attachments"]
        assert file_event["payload"]["attachments"] == [
            {"name": "notes.txt", "media_type": "text/plain"},
        ]
        assert encoded not in str(view)
        assert replay(log, run_id).ok
        followup = plane.handle("run.follow_up", {
            "task_id": task.task_id, "source_run_id": run_id, "message": "继续",
        })["run"]["run_id"]
        assert engine_for(stack, model).resume_run(followup).terminal
        assert all("attachments" not in item for item in model.requests[-1])


def test_invalid_attachment_never_creates_a_run(tmp_path):
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=RecordingModel())
        task = stack.tasks.create_task("读附件")
        head = log.seq_head()
        with pytest.raises(ControlError) as caught:
            plane.handle("run.start", {"task_id": task.task_id, "attachments": [{
                "name": "../notes.txt", "media_type": "text/plain", "data_base64": "YQ==",
            }]})
        assert caught.value.code == "bad_params"
        assert log.seq_head() == head
        assert stack.tasks.list_runs(task.task_id) == []


def test_guidance_received_during_generation_is_replayed_in_the_next_turn(tmp_path):
    class SteeredModel(RecordingModel):
        guide = None

        def complete(self, messages, tools):
            if not self.requests:
                self.guide()
            return super().complete(messages, tools)

    model = SteeredModel()
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=model)
        task = stack.tasks.create_task("描述颜色")
        run = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
        model.guide = lambda: plane.handle("run.guide", {"run_id": run, "text": "再解释原因。"})
        assert engine_for(stack, model).resume_run(run).terminal
        assert all(m["content"] != "再解释原因。" for m in model.requests[0])
        assert model.requests[1][-1] == {"role": "user", "content": "再解释原因。"}
        events = log.read_run(run)
        [request] = [e for e in events if e.type == "run.guidance_requested"]
        [applied] = [e for e in events if e.type == "run.guidance_applied"]
        assert applied.payload["request_seq"] == request.seq
        assert replay(log, run).ok
        with pytest.raises(ControlError) as caught:
            plane.handle("run.guide", {"run_id": run, "text": "已结束。"})
        assert caught.value.code == "conflict"


def test_replay_refuses_conversation_fact_with_cross_task_source(tmp_path):
    with closing(EventLog(tmp_path / 'events.db', blob_dir=tmp_path / 'blobs')) as log:
        stack = stack_for(log, data_root=tmp_path)
        model = RecordingModel()
        source = stack.tasks.create_task('来源任务')
        target = stack.tasks.create_task('无关任务')
        first = engine_for(stack, model).start(source.task_id)
        [answer] = [e for e in log.read_run(first.run_id) if e.type == 'model.result']
        wrong = stack.tasks.start_run(target.task_id, payload={'task': target.goal})
        log.append(wrong.run_id, 'run.conversation', {'turns': [{
            'source_run_id': first.run_id, 'source_seq': answer.seq,
            'answer': answer.payload['content'], 'message': '继续',
        }]}, task_id=target.task_id, actor_kind='human')
        report = replay(log, wrong.run_id)
        assert not report.ok
        assert any('来源' in p for p in report.problems)


def test_guidance_while_waiting_does_not_rewrite_approved_proposal(tmp_path):
    from support import approve_pending

    from tianshu.model import ToolCall

    class GuidedModel(RecordingModel):
        def __init__(self):
            ScriptedModel.__init__(self, [
                ModelResponse("tool_use", tool_calls=[ToolCall("write1", "modify_text", {
                    "path": "note.txt", "content": "原提案",
                })]),
                ModelResponse("end_turn", "已收到补充"),
            ])
            self.requests = []

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.txt").write_text("原文件", encoding="utf-8")
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        model = GuidedModel()
        plane = ControlPlane(stack, model=model)
        engine = engine_for(stack, model)
        task = stack.tasks.create_task("修改一个笔记", [str(ws)])
        waiting = engine.start(task.task_id)
        assert waiting.state == "waiting_approval"
        [before] = stack.approvals.pending(run_id=waiting.run_id)
        plane.handle("run.guide", {"run_id": waiting.run_id, "text": "下次改成新内容"})
        [after] = stack.approvals.pending(run_id=waiting.run_id)
        assert after.approval_id == before.approval_id
        assert after.action_digest == before.action_digest
        assert (ws / "note.txt").read_text(encoding="utf-8") == "原文件"
        assert approve_pending(stack, waiting.run_id)
        assert engine.resume_run(waiting.run_id).terminal
        assert (ws / "note.txt").read_text(encoding="utf-8") == "原提案"
        events = log.read_run(waiting.run_id)
        headers = [e.type for e in events]
        assert headers.index("tool.result") < headers.index("run.guidance_applied")
        assert model.requests[-1][-1] == {"role": "user", "content": "下次改成新内容"}
        assert replay(log, waiting.run_id).ok
