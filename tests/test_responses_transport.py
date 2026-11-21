"""Verify Responses HTTP framing and retry policy through committed Run events."""

import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from support import stack_for
from test_responses_model import frames_for, message, response

from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits
from tianshu.model import ModelError, UrllibTransport
from tianshu.openai_responses import OpenAIResponsesModel


@pytest.mark.parametrize("streaming", [False, True])
def test_incomplete_rate_limit_body_is_a_protocol_error(streaming):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(429)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        transport = UrllibTransport(allow_redirects=False, strict_framing=True)
        url = f"http://127.0.0.1:{server.server_port}/v1/responses"
        try:
            with pytest.raises(ModelError) as error:
                if streaming:
                    list(transport.post_stream(url, body={}, headers={}, timeout=2))
                else:
                    transport.post(url, body={}, headers={}, timeout=2)
            assert error.value.kind == "protocol" and error.value.status_code is None
        finally:
            thread.join(timeout=5)


@pytest.mark.parametrize(("streaming", "first_response"), [
    (streaming, case)
    for streaming in (False, True)
    for case in [
        429, 503, 302, "truncated", "short_headers", "valid_chunked", "bad_chunk_delimiter",
        "missing_chunk_trailer", "conflicting_length", "close_delimited",
    ]
] + [(True, "unterminated_sse")])
def test_responses_requires_complete_framing_and_retries_only_rate_limit(
    tmp_path, streaming, first_response,
):
    bodies = []
    redirects = []
    if streaming:
        frames = frames_for([message()])
        terminal = "".join(f"event: {name}\ndata: {data}\n\n" for name, data in frames).encode()
    else:
        terminal = json.dumps(response()).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.close_connection = True
            body = self.rfile.read(int(self.headers["Content-Length"]))
            bodies.append((self.path, body))
            payload, count, status = terminal, len(terminal), 200
            chunked = False
            if len(bodies) == 1:
                if first_response == "truncated":
                    # The entire terminal object can be parsed before the missing bytes are noticed.
                    count += 20
                elif first_response == "unterminated_sse":
                    payload = terminal[:-1]
                    count = len(payload)
                elif first_response == "short_headers":
                    self.wfile.write(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\n")
                    return
                elif isinstance(first_response, int):
                    payload, count, status = b"{}", 2, first_response
                else:
                    chunked = first_response != "close_delimited"
            self.send_response(status)
            content_type = "text/event-stream" if streaming else "application/json"
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            if status == 302:
                self.send_header("Location", "/redirected")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            if (not chunked and first_response != "close_delimited"
                    or first_response == "conflicting_length"):
                self.send_header("Content-Length", str(count))
            self.end_headers()
            if chunked:
                delimiter = b"XX" if first_response == "bad_chunk_delimiter" else b"\r\n"
                trailer = (b"0\r\n" if first_response == "missing_chunk_trailer"
                           else b"0\r\nX-Test: valid\r\n\r\n")
                # Split the first SSE line and a JSON token across chunk boundaries.
                for block in (payload[:4], payload[4:]):
                    self.wfile.write(f"{len(block):x}\r\n".encode() + block + delimiter)
                self.wfile.write(trailer)
            else:
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
                errors = [e for e in events if e.type == "model.error"]
                results = [e for e in events if e.type == "model.result"]
                if first_response == 429:
                    assert bodies[0] == bodies[1]
                    assert [e.payload["retryable"] for e in errors] == [True]
                    assert run.state == "succeeded" and len(results) == 1
                elif first_response in {"valid_chunked", "close_delimited"}:
                    assert not errors
                    assert run.state == "succeeded" and len(results) == 1
                else:
                    assert [e.payload["retryable"] for e in errors] == [False]
                    assert run.state == "failed" and not results
                    usage = stack.budget.usage(f"run:{run.run_id}")
                    assert usage["settled"]["requests"] == 1
                    assert usage["reserved"]["tokens"] > 0 and usage["unknown_reservations"] == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)
