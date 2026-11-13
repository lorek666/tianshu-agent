"""R2 INPUT-03/07: whole-request deadline and relative redirect targets."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest
from test_round2_inputs_model_mcp import _server

from tianshu import net
from tianshu.resources import NetworkTarget


@pytest.mark.parametrize("phase", ("headers", "body", "silent"))
def test_network_deadline_interrupts_slow_progress_before_peer_finishes(phase):
    stop = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    sent = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            started.set()
            try:
                if phase == "headers":
                    self.wfile.write(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                elif phase == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", "32")
                    self.end_headers()
                else:
                    stop.wait(2)
                    return
                # A controlled byte schedule, not a sleep guessing business state.
                for i in range(32):
                    if stop.wait(0.04):
                        break
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    sent.append(i)
                if phase == "headers":
                    self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                finished.set()

    with _server(Peer) as server:
        target = NetworkTarget.parse(f"network://http/127.0.0.1:{server.server_port}/slow")
        began = time.monotonic()
        try:
            with pytest.raises(net.NetError) as exc:
                net.fetch(target, limits=net.NetLimits(timeout_ms=100, max_bytes=100))
            elapsed = time.monotonic() - began
            assert exc.value.reason == "net.timeout"
            assert started.is_set()
            assert elapsed < 0.5, f"100ms deadline blocked for {elapsed:.3f}s"
            assert len(sent) < 16, "waited for peer progress instead of deadline"
        finally:
            stop.set()
            assert finished.wait(2)


@pytest.mark.parametrize("location,expected_path", [
    ("?second=2", "/dir/page?second=2"),
    ("next?second=2", "/dir/next?second=2"),
    ("/next?second=2", "/next?second=2"),
])
def test_relative_redirect_hits_exact_resource(location, expected_path):
    hits = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            hits.append(self.path)
            if len(hits) == 1:
                self.send_response(302)
                self.send_header("Location", location)
                body = b""
            else:
                self.send_response(200 if self.path == expected_path else 404)
                body = b"CORRECT-PAGE" if self.path == expected_path else b"WRONG-PAGE"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with _server(Peer) as server:
        uri = f"network://http/127.0.0.1:{server.server_port}"
        result = net.fetch(NetworkTarget.parse(uri + "/dir/page?first=1"))
    assert hits == ["/dir/page?first=1", expected_path]
    assert result.status == 200
    assert result.body == b"CORRECT-PAGE"
    assert result.uri == uri + expected_path


def test_network_path_redirect_to_second_origin_never_sends_request():
    second_hits = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            second_hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with _server(Sink) as sink:
        class Origin(Sink):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"//127.0.0.1:{sink.server_port}/target")
                self.send_header("Content-Length", "0")
                self.end_headers()

        with _server(Origin) as origin:
            target = NetworkTarget.parse(f"network://http/127.0.0.1:{origin.server_port}/dir/page")
            with pytest.raises(net.NetError) as exc:
                net.fetch(target)
            assert exc.value.reason == "net.redirect_origin_denied"
    assert not second_hits


def test_fragment_only_redirect_detects_loop_without_new_request():
    hits = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "#section")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with _server(Peer) as server:
        target = NetworkTarget.parse(f"network://http/127.0.0.1:{server.server_port}/dir/page?first=1")
        with pytest.raises(net.NetError) as exc:
            net.fetch(target)
        assert exc.value.reason == "net.redirect_loop"
    assert hits == ["/dir/page?first=1"]


def test_fast_fetch_cleans_timer_before_following_request():
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    observed_extra = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    with _server(Peer) as server:
        uri = f"network://http/127.0.0.1:{server.server_port}/fast"
        for _ in range(4):
            result = net.fetch(NetworkTarget.parse(uri), limits=net.NetLimits(timeout_ms=100))
            assert result.body == b"ok"
            observed_extra.extend(thread for thread in threading.enumerate()
                                  if isinstance(thread, threading.Timer)
                                  and thread.ident not in baseline_threads)
    assert observed_extra == [], "fetch returned while its deadline timer was still live"
