"""R2 INPUT-05/06: actual child processes hold incomplete frames behind barriers."""
from __future__ import annotations

import json
import multiprocessing
import os
import socket
import struct
import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from tianshu import ipc

MESSAGE = {"push": "event", "event": {"synthetic": True}}


def _receiver(connection, control, mode, resume):
    client = ipc.Client(Path("."), timeout=0.1)
    client._conn = connection
    server = ipc.ServerConnection(connection)
    control.send({"ready": True, "pid": os.getpid()})
    assert control.recv() == "go"
    began = time.monotonic()
    try:
        if mode == "rpc":
            result = client._receive(time.monotonic() + 0.1)
        elif mode == "event":
            result = client.next_event(timeout=0.1)
        else:
            result = server.recv(timeout=0.1)
        outcome = {"result": result, "eof": server.eof if mode == "server" else None}
    except Exception as exc:
        outcome = {"error": type(exc).__name__, "closed": connection.closed}
    outcome["elapsed"] = time.monotonic() - began
    control.send(outcome)
    if resume:
        again = (server.recv(timeout=0.1) if mode == "server"
                 else client.next_event(timeout=0.1))
        control.send({"second_timeout": again})
        assert control.recv() == "resume"
        try:
            result = (server.recv(timeout=2) if mode == "server"
                      else client.next_event(timeout=2))
            following = (server.recv(timeout=2) if mode == "server"
                         else client.next_event(timeout=2))
            control.send({"resumed": result, "following": following})
        except Exception as exc:
            control.send({"resume_error": type(exc).__name__})
    server.close()
    client.close()
    control.close()


@pytest.mark.parametrize("mode", ("rpc", "event", "server"))
@pytest.mark.parametrize("split", ("header", "extended_header", "body"))
def test_ipc_incomplete_frame_respects_deadline_and_preserves_subscription(mode, split):
    context = multiprocessing.get_context("spawn")
    control, child_control = context.Pipe()
    left, right = socket.socketpair()
    connection = Connection(left.detach())
    body = json.dumps(MESSAGE).encode()
    frame = struct.pack("!i", len(body)) + body
    cut = 2 if split == "header" else 5
    if split == "extended_header":
        frame = struct.pack("!iQ", -1, len(body)) + body
        cut = 6
    resume = mode != "rpc"
    child = context.Process(target=_receiver, args=(connection, child_control, mode, resume))
    child.start()
    connection.close()
    child_control.close()
    try:
        assert control.poll(5), "receiver did not reach barrier"
        ready = control.recv()
        assert ready["pid"] != os.getpid()
        right.sendall(frame[:cut])
        control.send("go")
        timely = control.poll(0.5)
        if not timely:
            # Release old code naturally so a failure cannot hide behind a kill.
            right.sendall(frame[cut:])
            assert control.poll(2)
        first = control.recv()
        if resume:
            assert control.poll(2)
            assert control.recv() == {"second_timeout": None}
            if timely:
                right.sendall(frame[cut:])
            next_body = json.dumps({"push": "event", "event": {"seq": 2}}).encode()
            right.sendall(struct.pack("!i", len(next_body)) + next_body)
            control.send("resume")
            assert control.poll(3)
            second = control.recv()
        child.join(3)
        assert child.exitcode == 0
        assert timely, f"half-frame read exceeded 100ms deadline: {first}"
        assert first["elapsed"] < 0.5
        if mode == "rpc":
            assert first["error"] == "ConnectionClosed"
            assert first["closed"] is True
        else:
            assert first["result"] is None
            if mode == "server":
                assert first["eof"] is False
            assert second == {
                "resumed": MESSAGE, "following": {"push": "event", "event": {"seq": 2}},
            }
    finally:
        right.close()
        control.close()
        if child.is_alive():
            child.terminate()
            child.join(3)


@pytest.mark.parametrize("mode", ("rpc", "event", "server"))
@pytest.mark.parametrize("extended", (False, True))
def test_ipc_rejects_oversize_header_without_waiting_for_body(mode, extended):
    context = multiprocessing.get_context("spawn")
    control, child_control = context.Pipe()
    left, right = socket.socketpair()
    connection = Connection(left.detach())
    child = context.Process(target=_receiver, args=(connection, child_control, mode, False))
    child.start()
    connection.close()
    child_control.close()
    try:
        assert control.poll(5)
        control.recv()
        length = ipc.MAX_MESSAGE_BYTES + 1
        header = struct.pack("!iQ", -1, length) if extended else struct.pack("!i", length)
        right.sendall(header)
        control.send("go")
        timely = control.poll(0.5)
        if not timely:
            right.shutdown(socket.SHUT_WR)
            assert control.poll(2)
        outcome = control.recv()
        child.join(3)
        assert child.exitcode == 0
        assert timely, "receiver waited for oversize body instead of rejecting its length"
        assert outcome.get("error") == "ProtocolError", outcome
        assert outcome.get("closed") is True
    finally:
        right.close()
        control.close()
        if child.is_alive():
            child.terminate()
            child.join(3)


def _native_sender(connection, control):
    control.send({"ready": True, "pid": os.getpid()})
    assert control.recv() == "go"
    try:
        connection.send_bytes(json.dumps(MESSAGE).encode())
        control.send("sent")
        assert control.recv() == "oversize"
        # Must be valid JSON: otherwise decode alone could fake size rejection.
        oversized = {"push": "event", "payload": "x" * (ipc.MAX_MESSAGE_BYTES + 1)}
        connection.send_bytes(json.dumps(oversized).encode())
        control.send("oversize_sent")
    except (BrokenPipeError, OSError):
        control.send("closed_by_reader")
    finally:
        connection.close()
        control.close()


@pytest.mark.parametrize("mode", ("event", "server"))
def test_native_ipc_pipe_timeout_reuse_and_bounded_message(mode):
    """Uses Windows AF_PIPE on that runner, and POSIX native Pipe locally."""
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe()
    control, child_control = context.Pipe()
    child = context.Process(target=_native_sender, args=(writer, child_control))
    child.start()
    writer.close()
    child_control.close()
    client = ipc.Client(Path("."))
    client._conn = reader
    server = ipc.ServerConnection(reader)
    receive = client.next_event if mode == "event" else server.recv
    try:
        assert control.poll(5)
        ready = control.recv()
        assert ready["pid"] != os.getpid()
        assert receive(timeout=0.05) is None
        assert receive(timeout=0.05) is None
        assert not reader.closed
        control.send("go")
        assert receive(timeout=2) == MESSAGE
        assert control.poll(2)
        assert control.recv() == "sent"
        control.send("oversize")
        with pytest.raises(ipc.ProtocolError):
            receive(timeout=2)
        assert reader.closed
        if mode == "server":
            assert receive(timeout=0.01) is None
            assert server.eof
        else:
            with pytest.raises(ipc.ConnectionClosed):
                receive(timeout=0.01)
        assert control.poll(3)
        assert control.recv() in {"closed_by_reader", "oversize_sent"}
        child.join(3)
        assert child.exitcode == 0
    finally:
        server.close()
        client.close()
        control.close()
        if child.is_alive():
            child.terminate()
            child.join(3)


def _wait_for_native_close(connection, control):
    control.send("ready")
    try:
        connection.recv_bytes()
        control.send("unexpected_message")
    except (EOFError, OSError):
        control.send("closed")
    finally:
        connection.close()
        control.close()


@pytest.mark.parametrize("mode", ("event", "server"))
def test_native_pipe_close_cancels_pending_read_and_releases_peer(mode):
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe()
    control, child_control = context.Pipe()
    child = context.Process(target=_wait_for_native_close, args=(writer, child_control))
    child.start()
    writer.close()
    child_control.close()
    client = ipc.Client(Path("."))
    client._conn = reader
    server = ipc.ServerConnection(reader)
    receiver = client if mode == "event" else server
    receive = client.next_event if mode == "event" else server.recv
    try:
        assert control.poll(5)
        assert control.recv() == "ready"
        # On AF_PIPE, this leaves the overlapped native ReadFile pending.
        assert receive(timeout=0.05) is None
        began = time.monotonic()
        receiver.close()
        assert time.monotonic() - began < 0.5
        assert reader.closed
        assert control.poll(2), "pending read retained a live pipe handle"
        assert control.recv() == "closed"
        child.join(3)
        assert child.exitcode == 0
    finally:
        receiver.close()
        control.close()
        if child.is_alive():
            child.terminate()
            child.join(3)
