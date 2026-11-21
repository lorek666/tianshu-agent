"""真实认证与 fd 自然复用：旧 reader 不得消费新连接的认证帧头。"""

from __future__ import annotations

import gc
import multiprocessing
import multiprocessing.connection as mp_connection
import os
import tempfile
import threading
import traceback
from pathlib import Path

import pytest

from tianshu import daemon, ipc


def _connecting_child(root: str, go, result) -> None:
    client = ipc.Client(Path(root), timeout=5.0)
    result.send({"ready": True})
    if not go.wait(10):
        result.send({"error": "go barrier timed out"})
        return
    try:
        welcome = client.connect()
        result.send({"connected": True, "welcome": welcome})
    except BaseException as exc:
        result.send({
            "connected": False,
            "error": type(exc).__name__,
            "cause": type(exc.__cause__).__name__,
            "traceback": traceback.format_exc(),
        })
    finally:
        client.close()
        result.close()


@pytest.mark.skipif(os.name == "nt", reason="限定复核 POSIX fd 复用窗口")
@pytest.mark.parametrize("reuse_before_old_read", [False, True])
def test_abort_old_reader_and_new_authenticated_connection(monkeypatch, reuse_before_old_read):
    ctx = multiprocessing.get_context("spawn")
    go = ctx.Event()
    parent_result, child_result = ctx.Pipe(duplex=False)
    reader_entered = threading.Event()
    release_read = threading.Event()
    read_done = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    reader_result = []
    accepted = []
    old_read_bytes = []
    new_server_fds = []
    accept_errors = []
    filler_fds = []
    with tempfile.TemporaryDirectory(prefix="tsfd-life-", dir="/tmp") as temp:
        root = Path(temp)
        server = ipc.Server(root)
        server.start()

        def first_accept():
            accepted.append(server.accept())

        first = threading.Thread(target=first_accept, daemon=True)
        first.start()
        raw_old_client = mp_connection.Client(
            server.endpoint, family="AF_UNIX", authkey=server.authkey,
        )
        first.join(5)
        assert accepted
        old = accepted[0]
        old_fd = old._conn.fileno()
        original_read = old._conn._read
        original_close = old.close
        managed = daemon._ClientConnection(old, name="lifecycle-old")

        def held_read(fd, size):
            assert fd == old_fd
            reader_entered.set()
            assert release_read.wait(10), "未放行旧 read"
            try:
                data = original_read(fd, size)
                old_read_bytes.append(data.hex())
                return data
            finally:
                read_done.set()

        def close_after_fd_was_taken():
            # abort 的真实 shutdown 先叫醒 conn.poll；固定调度为先取 fd，再 close。
            assert reader_entered.wait(5), "shutdown 未叫醒真实旧 reader"
            close_started.set()
            original_close()
            close_finished.set()

        def old_receive():
            try:
                reader_result.append(old.recv(timeout=5.0))
            except BaseException as exc:
                reader_result.append(type(exc).__name__)

        monkeypatch.setattr(old._conn, "_read", held_read)
        monkeypatch.setattr(old, "close", close_after_fd_was_taken)
        reader = threading.Thread(target=old_receive, daemon=True)
        child = ctx.Process(target=_connecting_child, args=(str(root), go, child_result))
        child.start()
        child_result.close()
        assert parent_result.poll(10), "子进程未就绪"
        assert parent_result.recv() == {"ready": True}
        # spawn 结束父端管道初始化后，填满旧 fd 以下的空洞；下一次 accept 自然复用旧 fd。
        while True:
            filler = os.open(os.devnull, os.O_RDONLY)
            filler_fds.append(filler)
            if filler > old_fd:
                break
        reader.start()

        original_deliver = mp_connection.deliver_challenge

        class PausedAuthentication:
            def __init__(self, conn):
                self.conn = conn

            def send_bytes(self, value):
                return self.conn.send_bytes(value)

            def recv_bytes(self, maxlength):
                fd = self.conn.fileno()
                new_server_fds.append(fd)
                release_read.set()
                assert read_done.wait(5), "旧 read 未完成"
                assert close_finished.wait(5), "旧 close 未完成"
                return self.conn.recv_bytes(maxlength)

        def deliver_with_scheduler(conn, authkey):
            if reuse_before_old_read:
                return original_deliver(PausedAuthentication(conn), authkey)
            return original_deliver(conn, authkey)

        monkeypatch.setattr(mp_connection, "deliver_challenge", deliver_with_scheduler)
        drop_result = []
        dropper = threading.Thread(
            target=lambda: drop_result.append(managed.drop("send_stall")), daemon=True,
        )
        try:
            dropper.start()
            assert reader_entered.wait(5)
            assert close_started.wait(5)
            if not reuse_before_old_read:
                release_read.set()
                assert read_done.wait(5)
                assert close_finished.wait(5)
            elif not old._reader._state_lock.locked():
                # 旧实现未锁住单次 read，确保 close 已真实完成再接受新连接。
                # 若 read 已在同一状态锁内，close 必须等旧 read，不能释放其 fd。
                assert close_finished.wait(5)

            def final_accept():
                try:
                    resumed = server.accept()
                    new_server_fds.append(resumed._conn.fileno())
                    hello = resumed.recv(timeout=5.0)
                    assert hello is not None and hello["method"] == "hello"
                    resumed.send(ipc.ok(hello["id"], {"protocol": 1}))
                except BaseException as exc:
                    accept_errors.append(f"{type(exc).__name__}: {exc}")
                finally:
                    # Listener.accept 认证失败的 Connection 随异常帧释放后须实际关闭。
                    gc.collect()

            accept_thread = threading.Thread(target=final_accept, daemon=True)
            accept_thread.start()
            go.set()
            assert parent_result.poll(10), "新连接既未完成，也未报错"
            outcome = parent_result.recv()
            accept_thread.join(5)
            reader.join(5)
            print({
                "reuse_before_old_read": reuse_before_old_read,
                "old_fd": old_fd,
                "new_server_fds": new_server_fds,
                "old_read_bytes": old_read_bytes,
                "reader_result": reader_result,
                "accept_errors": accept_errors,
                "outcome": outcome,
            })
            if reuse_before_old_read and not outcome["connected"]:
                assert outcome["error"] == "ConnectionClosed"
                assert outcome["cause"] == "EOFError"
                assert len(old_read_bytes) == 1 and len(old_read_bytes[0]) == 8
                assert new_server_fds == [old_fd]
                assert "answer_challenge" in outcome["traceback"]
            assert outcome["connected"] is True, "旧 reader 截走新认证连接的帧头"
            assert not any(old_read_bytes), "旧 reader 只能读到自己的 EOF"
            assert not accept_errors
            assert not reader.is_alive()
            assert not accept_thread.is_alive()
            dropper.join(5)
            assert drop_result == [True]
        finally:
            release_read.set()
            go.set()
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
            reader.join(5)
            dropper.join(5)
            raw_old_client.close()
            managed.close()
            managed.join_sender(1)
            server.close()
            for filler in filler_fds:
                os.close(filler)
            parent_result.close()
            assert child.exitcode == 0
