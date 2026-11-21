"""Signed HTTP deliveries use the real CLI, daemon and transactional Scheduler."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from http.server import HTTPServer
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_scheduler import _count_task_created, _Harness, _short_data_root

from tianshu import ipc, schedule_webhook

SECRET = "fixture-secret-0000000000000000000000000000"


def signature(schedule_id: str, stamp: str, body: bytes) -> str:
    material = f"tianshu-webhook-v1\n{schedule_id}\n{stamp}\n".encode("ascii") + body
    return "sha256=" + hmac.new(SECRET.encode(), material, hashlib.sha256).hexdigest()


def post(port, schedule_id, body, *, skew=0, signed_body=None, path="/v1/webhook"):
    stamp = str(int(time.time()) + skew)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        connection.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "X-Tianshu-Timestamp": stamp,
            "X-Tianshu-Signature": signature(
                schedule_id, stamp, body if signed_body is None else signed_body,
            ),
        })
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@contextmanager
def daemon_running(root):
    harness = _Harness(root).start()
    try:
        yield
    finally:
        assert harness.stop_and_wait()


def test_signed_webhook_is_bound_to_one_schedule_and_deduplicates(tmp_path, monkeypatch):
    root = _short_data_root(tmp_path)
    monkeypatch.setenv("TIANSHU_WEBHOOK_SECRET_FIXTURE", SECRET)
    with daemon_running(root):
        with ipc.Client(root) as client:
            schedule_id = client.call("schedule.create", {
                "goal": "固定 Webhook 任务", "trigger": {"kind": "event"},
            })["schedule"]["schedule_id"]
            another = client.call("schedule.create", {
                "goal": "不得由此入口触发", "trigger": {"kind": "event"},
            })["schedule"]["schedule_id"]
        with subprocess.Popen([
            sys.executable, "-m", "tianshu.cli", "schedule", "webhook-serve", schedule_id,
            "--data-root", str(root), "--port", "0",
            "--secret-env", "TIANSHU_WEBHOOK_SECRET_FIXTURE",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") as child:
            try:
                assert child.stdout is not None
                lines = Queue()
                Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True).start()
                ready = lines.get(timeout=10)
                found = re.fullmatch(r"Webhook: http://127\.0\.0\.1:(\d+)/v1/webhook\n", ready)
                assert found, f"webhook listener did not start: {ready!r}"
                port = int(found[1])
                body = b'{"event_id":"fixture:delivery-1"}'
                assert post(port, another, body)[0] == 401
                assert post(port, schedule_id, body, skew=-3600)[0] == 401
                assert post(port, schedule_id, body + b" ", signed_body=body)[0] == 401
                assert post(port, schedule_id, b'{"event_id":"a","event_id":"b"}')[0] == 400
                assert post(port, schedule_id, b'{"event_id":"a","goal":"bad"}')[0] == 400
                assert post(port, schedule_id, b" " * 1025)[0] == 413
                assert post(port, schedule_id, body, path="/v1/webhook?x=1")[0] == 404
                assert post(port, schedule_id, body, path="//v1/webhook")[0] == 404
                assert _count_task_created(root / "events.db", "固定 Webhook 任务") == 0
                assert post(port, schedule_id, body) == (202, {"accepted": True})
                assert post(port, schedule_id, body) == (202, {"accepted": False})
                with ipc.Client(root) as client:
                    client.call("schedule.set_enabled", {
                        "schedule_id": schedule_id, "enabled": False,
                    })
                assert post(port, schedule_id, b'{"event_id":"fixture:delivery-2"}')[0] == 409
                assert _count_task_created(root / "events.db", "固定 Webhook 任务") == 1
                assert _count_task_created(root / "events.db", "不得由此入口触发") == 0
            finally:
                child.terminate()
                _, stderr = child.communicate(timeout=10)
                assert SECRET not in stderr and "fixture:delivery" not in stderr


@pytest.mark.parametrize("stall", ["headers", "body"])
def test_request_read_deadline_covers_headers_and_body(tmp_path, monkeypatch, stall):
    root = _short_data_root(tmp_path)
    clock = [50.0]
    monkeypatch.setattr(schedule_webhook, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time,
    ))
    reached_stall = Event()
    release = Event()
    first_request = True
    goal = "Webhook 请求超时后不准入队"
    with daemon_running(root):
        with ipc.Client(root) as client:
            schedule_id = client.call("schedule.create", {
                "goal": goal, "trigger": {"kind": "event"},
            })["schedule"]["schedule_id"]
        actual_handler = schedule_webhook._handler(root, schedule_id, SECRET.encode())

        class PausingHandler(actual_handler):
            def _pause(self):
                nonlocal first_request
                if first_request:
                    first_request = False
                    reached_stall.set()
                    assert release.wait(timeout=5)

            def setup(self):
                super().setup()
                if stall == "headers":
                    self._pause()

            def parse_request(self):
                result = super().parse_request()
                if result and stall == "body":
                    self._pause()
                return result

        raw = b'{"event_id":"expired-reading"}'
        stamp = str(int(time.time()))
        head = (
            "POST /v1/webhook HTTP/1.0\r\n"
            f"Content-Length: {len(raw)}\r\nContent-Type: application/json\r\n"
            f"X-Tianshu-Timestamp: {stamp}\r\n"
            f"X-Tianshu-Signature: {signature(schedule_id, stamp, raw)}\r\n\r\n"
        ).encode("ascii")
        with HTTPServer(("127.0.0.1", 0), PausingHandler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with socket.create_connection(server.server_address, timeout=5) as sender:
                    prefix, remaining = (head, raw) if stall == "body" else (
                        head[:18], head[18:] + raw,
                    )
                    sender.sendall(prefix)
                    assert reached_stall.wait(timeout=5)
                    clock[0] += 4
                    sender.sendall(remaining)
                    release.set()
                    with (
                        http.client.HTTPResponse(sender) as response,
                        pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)),
                    ):
                        response.begin()
                assert post(server.server_port, schedule_id, b'{"event_id":"after-stall"}') == (
                    202, {"accepted": True},
                )
                assert _count_task_created(root / "events.db", goal) == 1
            finally:
                release.set()
                server.shutdown()
                worker.join(timeout=5)
