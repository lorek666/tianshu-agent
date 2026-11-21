"""Verify Responses retry policy through real HTTP and committed Run events."""

import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from support import stack_for
from test_responses_model import frames_for, message, response

from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits
from tianshu.openai_responses import OpenAIResponsesModel


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("first_response", [429, 503, 302, "truncated"])
def test_responses_retries_only_explicit_rate_limit(tmp_path, streaming, first_response):
    bodies = []
    redirects = []
    if streaming:
        frames = frames_for([message()])
        terminal = "".join(f"event: {name}\ndata: {data}\n\n" for name, data in frames).encode()
    else:
        terminal = json.dumps(response()).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            bodies.append((self.path, body))
            payload, count, status = terminal, len(terminal), 200
            if len(bodies) == 1:
                if first_response == "truncated":
                    # The entire terminal object can be parsed before the missing bytes are noticed.
                    count += 20
                else:
                    payload, count, status = b"{}", 2, first_response
            self.send_response(status)
            content_type = "text/event-stream" if streaming else "application/json"
            self.send_header("Content-Type", content_type)
            if status == 302:
                self.send_header("Location", "/redirected")
            self.send_header("Content-Length", str(count))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            redirects.append(self.path)
            self.send_error(429)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
                stack = stack_for(log, data_root=tmp_path)
                task = stack.tasks.create_task("return a local test answer")
                model = OpenAIResponsesModel(
                    "test-model", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key="test-only", timeout=2,
                )
                loop = AgentLoop(
                    tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
                    dispatcher=stack.dispatcher, approvals=stack.approvals,
                    snapshots=stack.snapshots,
                    model=model, budget=stack.budget, memory=stack.memory,
                    contents=stack.contents, summaries=stack.summaries,
                    limits=Limits(max_model_attempts=2, retry_backoff_ms=0,
                                  budget_limits={"requests": 2, "tokens": 1_000_000}),
                    on_model_delta=(lambda *_: None) if streaming else None,
                )
                run = loop.start(task.task_id, out_name="")
                expected_attempts = 2 if first_response == 429 else 1
                assert redirects == []
                assert len(bodies) == expected_attempts
                assert {path for path, _ in bodies} == {"/v1/responses"}
                assert (json.loads(bodies[0][1]).get("stream") is True) == streaming
                events = log.read_run(run.run_id)
                assert sum(e.type == "model.intent" for e in events) == expected_attempts
                [error] = [e for e in events if e.type == "model.error"]
                results = [e for e in events if e.type == "model.result"]
                if first_response == 429:
                    assert bodies[0] == bodies[1]
                    assert error.payload["retryable"] is True
                    assert run.state == "succeeded" and len(results) == 1
                else:
                    assert error.payload["retryable"] is False
                    assert run.state == "failed" and not results
                    usage = stack.budget.usage(f"run:{run.run_id}")
                    assert usage["settled"]["requests"] == 1
                    assert usage["reserved"]["tokens"] > 0 and usage["unknown_reservations"] == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)
