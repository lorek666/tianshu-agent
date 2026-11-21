import json
from contextlib import closing
from urllib.parse import parse_qs

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.net import Fetched
from tianshu.replay import replay

ORIGIN = "network://https/search.example.com"
QUERY = "中文 & count=100"


def _engine(stack):
    return engine_for(stack, ScriptedModel([
        ModelResponse("tool_use", tool_calls=[ToolCall("c1", "network.search", {
            "origin": ORIGIN, "query": QUERY,
        })]),
        ModelResponse("end_turn", "完成"),
    ]))


def test_search_binds_query_to_approvals_and_does_not_open_results(tmp_path, monkeypatch):
    requests = []
    response = json.dumps({"results": [
        {"url": "https://example.com/source", "title": "来源", "content": "摘要"},
        {"url": "https://secret:password@example.com/", "title": "不应显示"},
        {"url": "javascript:ignored", "title": "不应显示"},
    ]}, ensure_ascii=False).encode()

    def fetch(target, *, limits, cancel):
        requests.append(target)
        assert limits.max_redirects == 0
        assert not cancel()
        return Fetched(target.to_uri(), 200, "application/json", response, False, (), 5)

    monkeypatch.setattr("tianshu.dispatcher.net_fetch", fetch)
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        denied = stack.tasks.create_task("搜索", [])
        assert _engine(stack).start(denied.task_id).terminal
        assert requests == []
        task = stack.tasks.create_task("搜索", [], outbound_allow=[ORIGIN])
        engine = _engine(stack)
        waiting = engine.start(task.task_id, out_name="")
        assert waiting.state == "waiting_approval"
        assert requests == []
        approve_pending(stack, waiting.run_id)
        assert engine.resume_run(waiting.run_id).terminal
        [target] = requests
        assert target.origin_uri == ORIGIN
        assert target.path == "/search"
        assert parse_qs(target.query) == {"q": [QUERY], "format": ["json"]}
        records = log.read_run(waiting.run_id)
        [intent] = [e for e in records if e.type == "tool.intent"]
        assert intent.payload["args"] == {"origin": ORIGIN, "query": QUERY}
        [result] = [e.payload for e in records if e.type == "tool.result"]
        assert result["output"]["results"] == [
            {"title": "来源", "url": "https://example.com/source", "snippet": "摘要"},
        ]
        assert result["output"]["ref"].startswith("content://sha256:")
        assert replay(log, waiting.run_id).ok
        assert len(requests) == 1


@pytest.mark.parametrize(('body', 'truncated'), [
    (b'{invalid', False), (b'{"results": []}', True),
])
def test_search_rejects_malformed_or_incomplete_response(tmp_path, monkeypatch, body, truncated):
    requests = []

    def fetch(target, *, limits, cancel):
        requests.append(target)
        return Fetched(target.to_uri(), 200, 'application/json', body, truncated, (), 5)

    monkeypatch.setattr('tianshu.dispatcher.net_fetch', fetch)
    with closing(EventLog(tmp_path / 'events.db', blob_dir=tmp_path / 'blobs')) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task('搜索', [], outbound_allow=[ORIGIN])
        engine = _engine(stack)
        run = engine.start(task.task_id)
        approve_pending(stack, run.run_id)
        assert engine.resume_run(run.run_id).terminal
        [result] = [e.payload for e in log.read_run(run.run_id) if e.type == 'tool.result']
        assert result['status'] == 'failed'
        assert result['output']['reason'] == 'net.search_response'
        assert 'ref' not in result['output']
        assert len(requests) == 1
        assert replay(log, run.run_id).ok
