from contextlib import closing

from support import approve_pending, engine_for, stack_for

from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.net import Fetched
from tianshu.receipt import verify_receipts

ORIGIN = "network://https/site.example"
START = ORIGIN + "/start"
NEXT = ORIGIN + "/next"


def test_browser_follows_only_exact_link_from_current_run(tmp_path, monkeypatch):
    sent = []

    def fetch(target, *, limits, cancel):
        sent.append(target.to_uri())
        assert limits.max_redirects == 0 and not cancel()
        html = (b'<title>Manual</title><script>not page text</script><a href="/next">Next</a>'
                b'<img src="/tracking"><form action="/post">No submit</form>'
                b'<a href="https://other.example/page">Other</a>')
        return Fetched(target.to_uri(), 200, "text/html", html, False, (), 5)

    monkeypatch.setattr("tianshu.dispatcher.net_fetch", fetch)
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("浏览帮助页", [], outbound_allow=[ORIGIN])

        def proposal(call_id, args):
            return ModelResponse(
                "tool_use", tool_calls=[ToolCall(call_id, "browser.navigate", args)]
            )

        engine = engine_for(stack, ScriptedModel([
            proposal("page", {"url": START}),
            proposal("forged", {"url": ORIGIN + "/other", "from_call": "page", "link_index": 0}),
            proposal("link", {"url": NEXT, "from_call": "page", "link_index": 0}),
            ModelResponse("end_turn", content="完成"),
        ]))
        waiting = engine.start(task.task_id)
        run_id = waiting.run_id
        assert waiting.state == "waiting_approval" and sent == []
        approve_pending(stack, run_id)
        waiting = engine.resume_run(run_id)
        assert waiting.state == "waiting_approval" and sent == [START]
        pending = stack.approvals.pending(run_id=run_id)
        assert [list(row.resources) for row in pending] == [[NEXT]]
        approve_pending(stack, run_id)
        assert engine.resume_run(run_id).terminal and sent == [START, NEXT]
        rows = {e.payload["call_id"]: e.payload for e in log.read_run(run_id)
                if e.type == "tool.result"}
        assert rows["page"]["output"]["browser"]["links"] == [{"label": "Next", "url": NEXT}]
        assert "not page text" not in rows["page"]["output"]["summary"]
        assert rows["forged"]["status"] == "rejected"
        assert verify_receipts(log, run_id) == []

        other = engine_for(stack, ScriptedModel([
            proposal("from-other", {"url": NEXT, "from_call": "page", "link_index": 0}),
            ModelResponse("end_turn", content="结束"),
        ])).start(task.task_id)
        assert other.terminal and sent == [START, NEXT]
