"""标准库 HTTP/SSE 实链路；仅回环地址，不使用真实模型或凭据。"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_responses_model import frames_for, message, response

from tianshu.model import ModelError
from tianshu.openai_responses import OpenAIResponsesModel


@contextmanager
def endpoint(stream, *, disconnected=False):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["Authorization"], body))
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if stream else "application/json",
            )
            self.end_headers()
            if stream:
                frames = frames_for([message("本地响应")])
                if disconnected:
                    frames = frames[:-1]
                for _, data in frames:
                    # SSE event 字段可省略，type 在 JSON 信封中；使用 CRLF。
                    self.wfile.write(f"data: {data}\r\n\r\n".encode())
                    self.wfile.flush()
            else:
                self.wfile.write(json.dumps(response([message("本地响应")])).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("stream", [False, True])
def test_real_http_and_sse_use_stateless_endpoint_and_final_measurement(stream):
    with endpoint(stream) as (url, received):
        model = OpenAIResponsesModel("test-model", api_key="test-only", base_url=url)
        preview = []
        result = (
            model.stream_complete([], [], preview.append) if stream else model.complete([], [])
        )
        assert result.content == "本地响应" and result.finish_reason == "end_turn"
        assert result.usage == {"input_tokens": 13, "output_tokens": 8}
        assert received[0][:2] == ("/v1/responses", "Bearer test-only")
        assert received[0][2]["store"] is False
        assert result.continuation.scope[1] == f"{url}/responses"
        if stream:
            assert "".join(d.text for d in preview) == "本地响应"


def test_real_sse_disconnect_never_returns_a_committable_model_response():
    with endpoint(True, disconnected=True) as (url, _):
        model = OpenAIResponsesModel("test-model", api_key="test-only", base_url=url)
        with pytest.raises(ModelError) as error:
            model.stream_complete([], [], lambda _: None)
        assert error.value.kind == "protocol" and not error.value.retryable
