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
                connection_type = type(raw._conn)
                original_send = connection_type._send

                def short_write(fd, buf):
                    if len(buf) > 16384 and not sent.is_set():
                        # 真正发送一段字节，随后在下一次 write 读取句柄前固定调度。
                        written = os.write(fd, buf[:4096])
                        sent.set()
                        assert release.wait(5), "关闭屏障未释放"
                        return written
                    return os.write(fd, buf)

                monkeypatch.setattr(
                    connection_type, "_send",
                    lambda transport, buf: original_send(transport, buf, write=short_write),
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
                    except (OSError, ipc.ConnectionClosed) as exc:
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
                assert isinstance(send_errors[0], (OSError, ipc.ConnectionClosed))
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX socket 文件描述符复用")
@pytest.mark.parametrize("close_method", ["abort", "close"])
def test_abort_during_socket_read_cannot_consume_reused_file(tmp_path, monkeypatch, close_method):
    """关闭后的四字节帧头读取不能偷走另一个文件的前缀。"""
    from multiprocessing.connection import Connection

    local, remote = socket.socketpair()
    raw = ipc.ServerConnection(Connection(local.detach()))
    original_fd = raw._conn.fileno()
    prefix = b'{"cold_start":true,"metric":"restart_recover_ms","value":1}\n'
    target = tmp_path / "metrics.jsonl"
    target.write_bytes(prefix)
    entered, release = threading.Event(), threading.Event()
    original_read = Connection._read
    errors = []

    def gated_read(fd, size):
        entered.set()
        if not release.wait(5):
            raise AssertionError("读取屏障未释放")
        return original_read(fd, size)

    monkeypatch.setattr(Connection, "_read", staticmethod(gated_read))
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os,struct,sys\n"
         "fd=int(sys.argv[1]);body=b'{}'\n"
         "os.write(fd,struct.pack('!i',len(body))+body)\n"
         "sys.stdin.buffer.read(1)\n"
         "os.close(fd)\n", str(remote.fileno())],
        pass_fds=(remote.fileno(),), stdin=subprocess.PIPE,
    )
    remote.close()

    def receive():
        try:
            assert raw.recv(timeout=5) is None
        except BaseException as exc:  # noqa: BLE001 - 交还测试线程
            errors.append(exc)

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    file_fd = None
    try:
        assert entered.wait(5), "真实帧头读取没有开始"
        getattr(raw, close_method)()
        file_fd = os.open(target, os.O_RDONLY)
        assert file_fd == original_fd, "未真正复用刚关闭的传输描述符"
        release.set()
        receiver.join(5)
        assert not receiver.is_alive()
        assert not errors, errors
        assert os.read(file_fd, len(prefix)) == prefix, "IPC 读取偷走了文件前四字节"
    finally:
        release.set()
        raw.close()
        receiver.join(5)
        if file_fd is not None:
            os.close(file_fd)
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=5)
    assert child.returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX AF_UNIX socket fd reuse")
def test_aborted_reader_cannot_consume_new_connection_auth_frame(monkeypatch):
    """Let a new authenticated connection take the old fd before the old read runs."""
    from multiprocessing import connection as mp

    child_code = """
import sys
from pathlib import Path
from tianshu import ipc
assert sys.stdin.buffer.read(1) == b'!'
with ipc.Client(Path(sys.argv[1]), timeout=5) as client:
    assert client.welcome == {'protocol': 1}
print('authenticated')
"""
    entered, release, read_done = (threading.Event() for _ in range(3))
    old_bytes, reader_results, new_fds, errors, fillers = [], [], [], [], []
    with tempfile.TemporaryDirectory(prefix="tsh-auth-fd-", dir="/tmp") as directory:
        root = Path(directory)
        server = ipc.Server(root)
        server.start()
        accepted = []
        initial = threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True)
        initial.start()
        peer = mp.Client(server.endpoint, family="AF_UNIX", authkey=server.authkey)
        initial.join(5)
        assert len(accepted) == 1
        old = accepted[0]
        old_fd = old._conn.fileno()
        original_read = mp.Connection._read

        def held_read(fd, size):
            if threading.current_thread() is not reader:
                return original_read(fd, size)
            entered.set()
            try:
                assert release.wait(5), "old read was not released"
                data = original_read(fd, size)
                old_bytes.append(data)
                return data
            finally:
                read_done.set()

        def receive_old():
            try:
                reader_results.append(old.recv(timeout=5))
            except Exception as exc:  # noqa: BLE001 - pass back the actual thread error
                errors.append(exc)

        reader = threading.Thread(target=receive_old, daemon=True)
        monkeypatch.setattr(mp.Connection, "_read", staticmethod(held_read))
        original_deliver = mp.deliver_challenge

        class AuthBarrier:
            def __init__(self, conn):
                self.conn = conn

            def send_bytes(self, value):
                self.conn.send_bytes(value)

            def recv_bytes(self, maxlength):
                new_fds.append(self.conn.fileno())
                release.set()
                assert read_done.wait(5), "old read did not complete during new authentication"
                return self.conn.recv_bytes(maxlength)

        def deliver_with_barrier(conn, authkey):
            try:
                return original_deliver(AuthBarrier(conn), authkey)
            except BaseException:
                # On bad framing the client must see EOF even if the test
                # retains the server traceback for the failure diagnostic.
                conn.close()
                raise

        def accept_new():
            try:
                connection = server.accept()
                hello = connection.recv(timeout=5)
                assert hello is not None and hello["method"] == "hello"
                connection.send(ipc.ok(hello["id"], {"protocol": 1}))
            except Exception as exc:  # noqa: BLE001 - pass back the actual thread error
                errors.append(exc)

        monkeypatch.setattr(mp, "deliver_challenge", deliver_with_barrier)
        replacement = threading.Thread(target=accept_new, daemon=True)
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            while not fillers or fillers[-1] < old_fd:
                fillers.append(os.open(os.devnull, os.O_RDONLY))
            peer.send_bytes(b"{}")
            reader.start()
            assert entered.wait(5), "old socket did not reach the framed read"
            old.abort()
            replacement.start()
            stdout, stderr = child.communicate(input=b"!", timeout=10)
            replacement.join(5)
            reader.join(5)
            assert new_fds == [old_fd], "new authenticated socket did not really reuse the fd"
            assert old_bytes in [[b""], [b"\x00\x00\x00\x02"]], (
                "old socket consumed bytes from the replacement authentication frame",
                old_bytes, errors,
            )
            assert child.returncode == 0, (stderr, old_bytes, errors)
            assert stdout.strip() == b"authenticated"
            assert not replacement.is_alive() and not reader.is_alive()
            assert not errors
            assert reader_results == [None]
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
            peer.close()
            server.close()
            for worker in (reader, replacement):
                if worker.ident is not None:
                    worker.join(5)
            for fd in fillers:
                os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX socket 文件描述符复用")
@pytest.mark.parametrize("close_method", ["abort", "close"])
def test_abort_during_socket_send_cannot_overwrite_reused_file(tmp_path, monkeypatch, close_method):
    from multiprocessing.connection import Connection

    local, remote = socket.socketpair()
    raw = ipc.ServerConnection(Connection(local.detach()))
    original_fd = raw._conn.fileno()
    target = tmp_path / "metrics.jsonl"
    prefix = b'{"metric":"untouched","value":1}\n'
    target.write_bytes(prefix)
    entered, release = threading.Event(), threading.Event()
    original_send = Connection._send
    errors = []

    def gated_write(fd, data):
        entered.set()
        if not release.wait(5):
            raise AssertionError("发送屏障未释放")
        return os.write(fd, data)

    monkeypatch.setattr(
        Connection, "_send", lambda conn, data: original_send(conn, data, write=gated_write)
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdin.buffer.read(1)"],
        pass_fds=(remote.fileno(),), stdin=subprocess.PIPE,
    )
    remote.close()

    def send():
        try:
            raw.send({"ok": True})
        except (OSError, ipc.ConnectionClosed) as exc:
            errors.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    file_fd = None
    try:
        assert entered.wait(5), "真实发送没有开始"
        getattr(raw, close_method)()
        file_fd = os.open(target, os.O_WRONLY)
        assert file_fd == original_fd, "未真正复用刚关闭的传输描述符"
        release.set()
        sender.join(5)
        assert not sender.is_alive()
        assert target.read_bytes() == prefix, "IPC 发送覆盖了另一文件"
        assert len(errors) == 1
    finally:
        release.set()
        raw.close()
        sender.join(5)
        if file_fd is not None:
            os.close(file_fd)
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=5)
    assert child.returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX socket 关闭唤醒")
def test_close_wakes_socket_read_without_deadline(monkeypatch):
    from multiprocessing.connection import Connection

    local, remote = socket.socketpair()
    raw = ipc.ServerConnection(Connection(local.detach()))
    entered = threading.Event()
    original_poll = Connection.poll
    errors = []

    def poll(conn, timeout=0):
        entered.set()
        return original_poll(conn, timeout)

    monkeypatch.setattr(Connection, "poll", poll)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdin.buffer.read(1)"],
        pass_fds=(remote.fileno(),), stdin=subprocess.PIPE,
    )
    remote.close()

    def receive():
        try:
            assert raw.recv() is None
        except BaseException as exc:  # noqa: BLE001 - 交还测试线程
            errors.append(exc)

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    try:
        assert entered.wait(5)
        raw.close()
        reader.join(5)
        assert not reader.is_alive(), "close 未唤醒持有传输副本的无限期 poll"
        assert not errors, errors
    finally:
        raw.close()
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=5)
        reader.join(5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX socket 生命周期")
@pytest.mark.parametrize("already_closed", [False, True])
def test_socket_close_finishes_when_handle_is_closed_or_dup_unavailable(
    monkeypatch, already_closed,
):
    import errno
    from multiprocessing.connection import Connection

    local, remote = socket.socketpair()
    raw = ipc.ServerConnection(Connection(local.detach()))
    try:
        if already_closed:
            raw._conn.close()
        else:
            def no_descriptors(fd):
                raise OSError(errno.EMFILE, "injected descriptor exhaustion")
            monkeypatch.setattr(os, "dup", no_descriptors)
        raw.close()
        raw.close()
        assert raw.closed
        remote.settimeout(1)
        assert remote.recv(1) == b""
    finally:
        raw._conn.close()
        remote.close()
