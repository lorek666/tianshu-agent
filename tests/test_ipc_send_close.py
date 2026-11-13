"""发送半帧后关闭：真实 IPC / 子进程 / 短写屏障，不伪造关闭异常。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from tianshu import daemon, ipc


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX Connection._send 短写循环；Windows PipeConnection 使用不同的发送实现",
)
def test_abort_after_real_partial_write_exits_sender_cleanly(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="tssrace-", dir="/tmp") as directory:
        root = Path(directory)
        server = ipc.Server(root)
        server.start()
        with socket.socket() as control:
            control.bind(("127.0.0.1", 0))
            control.listen(1)
            control.settimeout(5)
            child_code = (
                "import socket,sys\n"
                "from multiprocessing.connection import Client\n"
                "from pathlib import Path\n"
                "from tianshu import ipc\n"
                "root=Path(sys.argv[1])\n"
                "conn=Client(ipc.endpoint_path(root),family='AF_UNIX',"
                "authkey=ipc.load_authkey(root))\n"
                "s=socket.create_connection(('127.0.0.1',int(sys.argv[2])),timeout=5)\n"
                "s.sendall(b'ready');s.recv(1);conn.close();s.close()\n"
            )
            accepted = threading.Event()
            connection = {}

            def accept():
                try:
                    connection["raw"] = server.accept()
                except BaseException as exc:
                    connection["error"] = exc
                finally:
                    accepted.set()

            accept_thread = threading.Thread(target=accept, daemon=True)
            accept_thread.start()
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(root), str(control.getsockname()[1])]
            )
            peer = None
            conn = None
            sent = threading.Event()
            release = threading.Event()
            try:
                assert accepted.wait(5), "IPC 认证未完成"
                assert "error" not in connection, connection
                raw = connection["raw"]
                peer, _ = control.accept()
                peer.settimeout(5)
                assert peer.recv(5) == b"ready"
                original_send = raw._conn._send

                def short_write(fd, buf):
                    if len(buf) > 16384 and not sent.is_set():
                        # 真正发送一段字节，随后在下一次 write 读取句柄前固定调度。
                        written = os.write(fd, buf[:4096])
                        sent.set()
                        assert release.wait(5), "关闭屏障未释放"
                        return written
                    return os.write(fd, buf)

                monkeypatch.setattr(
                    raw._conn, "_send", lambda buf: original_send(buf, write=short_write)
                )
                thread_errors = []
                original_hook = threading.excepthook

                def record_thread_error(args):
                    if args.thread.name == "tianshu-daemon-sender-send-race":
                        thread_errors.append(args.exc_value)
                    else:
                        original_hook(args)

                monkeypatch.setattr(threading, "excepthook", record_thread_error)
                send_errors = []
                original_raw_send = raw.send

                def record_closed(message):
                    try:
                        original_raw_send(message)
                    except ipc.ConnectionClosed as exc:
                        send_errors.append(exc)
                        raise

                monkeypatch.setattr(raw, "send", record_closed)
                conn = daemon._ClientConnection(raw, name="send-race")
                assert conn.send({"push": "event", "payload": "x" * 32768})
                assert sent.wait(5), "未完成真实短写"
                assert conn.drop("send_stall")
                assert raw.closed
                release.set()
                assert conn.join_sender(2), "关闭后发送线程未退出"
                assert not thread_errors, thread_errors
                assert len(send_errors) == 1
                assert isinstance(send_errors[0].__cause__, TypeError)
                assert conn.drop_reason == "send_stall"
                assert conn.sent_frames == 0
            finally:
                release.set()
                if conn is not None:
                    conn.close()
                    conn.join_sender(2)
                if peer is not None:
                    peer.sendall(b"x")
                    peer.close()
                if child.poll() is None:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                server.close()
                accept_thread.join(2)
            assert child.returncode == 0


def test_send_type_error_on_open_connection_is_not_hidden():
    error = TypeError("unexpected implementation error")

    class OpenConnection:
        closed = False

        def send_bytes(self, data):
            raise error

    raw = ipc.ServerConnection(OpenConnection())
    with pytest.raises(TypeError) as caught:
        raw.send({"ok": True})
    assert caught.value is error


def test_encode_type_error_is_not_hidden_even_if_connection_is_closed():
    class ClosedConnection:
        closed = True

        def send_bytes(self, data):
            raise AssertionError("编码失败不应进入发送")

    raw = ipc.ServerConnection(ClosedConnection())
    with pytest.raises(TypeError):
        raw.send({"not_json": object()})
