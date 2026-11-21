"""A lost or redirecting reply does not cause a second form submission."""

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tianshu import net
from tianshu.resources import NetworkTarget


@pytest.mark.parametrize("mode", [
    "accept", "redirect", "drop", "truncated", "unframed", "invalid-headers", "no-content",
    "cancel-before-send", "too-large",
])
def test_form_transport_posts_once_without_credentials(monkeypatch, mode):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, dict(self.headers), raw))
            if mode == "drop":
                self.close_connection = True
                return
            body = b"" if mode == "no-content" else b"confirmed"
            self.send_response({"no-content": 204, "redirect": 307}.get(mode, 201))
            self.send_header("Location", "/different")
            if mode not in ("unframed", "no-content"):
                self.send_header("Content-Length", str(len(body) + (mode == "truncated")))
            if mode == "invalid-headers":
                self._headers_buffer.append(b"X-Missing-Colon\r\n")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    target = NetworkTarget.parse("network://https/fixture.example/submit")
    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def connect(endpoint, timeout):
            assert endpoint == target
            return http.client.HTTPConnection(*server.server_address, timeout=timeout)

        monkeypatch.setattr(net, "_connect", connect)
        body = b"message=a+b%26c"
        limits = net.NetLimits(min_interval_s=0, max_bytes=(1 if mode == "too-large" else 1024))
        try:
            if mode == "cancel-before-send":
                with pytest.raises(net.FormSubmitError) as exc:
                    net.submit_form(target, body, limits=limits, cancel=lambda: True)
                assert exc.value.reason == "net.cancelled" and not exc.value.sent
                assert received == []
                return
            if mode in ("accept", "no-content"):
                result = net.submit_form(target, body, limits=limits)
                expected = (201, b"confirmed") if mode == "accept" else (204, b"")
                assert (result.status, result.body) == expected
            else:
                with pytest.raises(net.FormSubmitError) as exc:
                    net.submit_form(target, body, limits=limits)
                assert exc.value.sent is True
            [(path, headers, sent)] = received
            assert (path, sent) == ("/submit", body)
            assert headers["Content-Type"] == "application/x-www-form-urlencoded"
            assert not {"Cookie", "Authorization", "Referer"} & headers.keys()
        finally:
            server.shutdown()
            thread.join(timeout=5)
