"""Host 元数据清理的真实文件/进程边界，不用伪造 PermissionError。"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tianshu import daemon, host, ipc

_CHILD = r"""
import json
import os
import socket
import sys
import threading
from pathlib import Path
from tianshu import host, ipc, leases
from tianshu.cli import _force_utf8_streams

# 与真实CLI入口一致；Windows文本流即使重定向也可能仍是cp1252。
_force_utf8_streams()

root = Path(sys.argv[1])
mode = sys.argv[2]
sock = socket.create_connection(('127.0.0.1', int(sys.argv[3])), timeout=15)
sock.settimeout(15)
stream = sock.makefile('rwb', buffering=0)

def send(value):
    stream.write(json.dumps(value).encode() + b'\n')

def receive():
    return json.loads(stream.readline())

send({'pid': os.getpid()})
if mode.startswith('host:'):
    original_ensure = host.DaemonSupervisor.ensure
    original_serve = host.UiBridge.serve_background
    original_init = host.UiBridge.__init__
    bridges = []
    supervisors = []
    def ensure(self):
        supervisors.append(self)
        original_ensure(self)
        if mode == 'host:first':
            send({'ensured': True})
            assert receive() == 'publish'
    def bridge_init(self, config):
        original_init(self, config)
        bridges.append(self)
    def serve(self):
        if mode == 'host:first-after-publish':
            send({'before_serve': True, 'port': self.port})
            assert receive() == 'serve'
        original_serve(self)
        send({'ready': True})
    if mode == 'host:monitor-held':
        original_start = threading.Thread.start
        def start(self):
            if self.name == 'daemon-supervisor':
                # 暂停真实Thread.start，不伪造Thread或join异常。
                send({
                    'monitor_start_held': True,
                    'port': bridges[0].port,
                    'token': bridges[0].config.token,
                    'thread_published': supervisors[0]._thread is self,
                })
                assert receive() == 'start'
            return original_start(self)
        threading.Thread.start = start
    if mode == 'host:second-after-publish':
        original_lock = leases._try_lock
        lock_notified = False
        def observe_lock(fd):
            global lock_notified
            try:
                original_lock(fd)
            except OSError:
                if not lock_notified:
                    send({'blocked': True})
                    lock_notified = True
                raise
        leases._try_lock = observe_lock
    host.DaemonSupervisor.ensure = ensure
    host.UiBridge.__init__ = bridge_init
    host.UiBridge.serve_background = serve
    code = host.run_host(
        root, ui_dir=root / 'ui',
        daemon_command=[sys.executable, '-m', 'tianshu', 'daemon', '--model', 'scripted'],
    )
    send({
        'exit': code,
        'bridge_closed': all(x.socket.fileno() == -1 for x in bridges),
        'monitor_alive': any(x._thread is not None and x._thread.is_alive() for x in supervisors),
        'log_open': any(x._log_file is not None and not x._log_file.closed for x in supervisors),
    })
    sys.exit(code)
elif mode == 'replace':
    assert receive() == 'go'
    info = host.acquire_host_info(root, ui_dir=root / 'ui')
    send({'written': True, 'pid': info.pid})
    assert receive() == 'exit'
elif mode in ('stop', 'acquire'):
    original = host._http_alive
    def alive(info, **kwargs):
        send({'snapshot': info.pid})
        assert receive() == 'go'
        return original(info, **kwargs)
    host._http_alive = alive
    if mode == 'stop':
        send({'result': host.stop_host(root, timeout=1)})
    else:
        try:
            host.acquire_host_info(root, ui_dir=root / 'ui')
        except host.HostError:
            send({'refused': True})
        else:
            send({'refused': False})
elif mode == 'guard':
    with leases.execution_guard(ipc.run_dir(root)):
        send({'locked': True})
        command = receive()
        if command == 'crash':
            os._exit(23)
        assert command == 'exit'
elif mode == 'deny-read':
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(host.host_lock_path(root)), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        send({'read_denied': True})
        assert receive() == 'exit'
    finally:
        assert api.CloseHandle(handle)
elif mode.startswith('operation:'):
    # 只观察真实内核锁的拒绝，不伪造锁错误或跳过实际文件操作。
    original = leases._try_lock
    notified = False
    def try_lock(fd):
        global notified
        try:
            original(fd)
        except OSError:
            if not notified:
                send({'blocked': True})
                notified = True
            raise
    leases._try_lock = try_lock
    name = mode.split(':', 1)[1]
    if name == 'read':
        value = host.read_host_info(root)
        assert value is not None
    elif name == 'write':
        host.write_host_info(root, host.HostInfo(os.getpid(), 0, 'new', 2))
    elif name == 'clear':
        host.clear_host_info(root)
    elif name == 'acquire':
        host.acquire_host_info(root, ui_dir=root / 'ui')
    elif name == 'stop':
        send({'result': host.stop_host(root)})
        sys.exit(0)
    else:
        raise AssertionError(name)
    send({'done': True})
else:
    raise AssertionError(mode)
stream.close()
sock.close()
"""


class _Peer:
    def __init__(self, proc: subprocess.Popen[bytes], conn: socket.socket) -> None:
        self.proc = proc
        self.conn = conn
        self.stream = conn.makefile("rwb", buffering=0)
        self.pid = self.receive()["pid"]

    def receive(self) -> dict:
        raw = self.stream.readline()
        assert raw, f"子进程未返回消息（exit={self.proc.poll()}）"
        return json.loads(raw)

    def send(self, value: str) -> None:
        self.stream.write(json.dumps(value).encode() + b"\n")


@contextlib.contextmanager
def _peer(root: Path, mode: str):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(15)
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("TIANSHU_", "OPENAI_", "ANTHROPIC_"))
        }
        env["PYTHONPATH"] = str(Path(host.__file__).resolve().parents[1])
        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(root), mode, str(listener.getsockname()[1])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        peer = None
        conn = None
        failure = None
        try:
            conn, _ = listener.accept()
            conn.settimeout(15)
            peer = _Peer(proc, conn)
            yield peer
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # 先让屏障中的真实worker收到EOF，并等它写完原始traceback。
            # 直接kill可能只杀Windows venv launcher，还会截断首个异常。
            if conn is not None:
                with contextlib.suppress(OSError):
                    conn.shutdown(socket.SHUT_RDWR)
                if peer is not None:
                    peer.stream.close()
                conn.close()
            cleanup = None
            stderr = b""
            stderr_complete = False
            try:
                try:
                    _, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired as timeout_error:
                    stderr = timeout_error.stderr or b""
                    if os.name == "nt":
                        worker_pid = peer.pid if peer is not None else proc.pid
                        cleanup = subprocess.run(
                            ["taskkill", "/PID", str(worker_pid), "/T", "/F"],
                            capture_output=True, timeout=10,
                        )
                    else:
                        proc.kill()
                    _, stderr = proc.communicate(timeout=10)
                stderr_complete = True
                if cleanup is not None and cleanup.returncode != 0:
                    raise AssertionError(
                        f"taskkill exit={cleanup.returncode}，"
                        f"stdout={cleanup.stdout!r}，stderr={cleanup.stderr!r}"
                    )
            except BaseException as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"子进程清理异常：{cleanup_error!r}")
            if failure is not None:
                failure.add_note(
                    f"子进程mode={mode}，launcher_pid={proc.pid}，"
                    f"worker_pid={peer.pid if peer is not None else None}，"
                    f"exit={proc.returncode}，stderr_complete={stderr_complete}；"
                    f"stderr原始bytes={stderr!r}\n"
                    f"stderr UTF-8:\n{stderr.decode('utf-8', errors='backslashreplace')}"
                )
                if cleanup is not None:
                    failure.add_note(
                        f"taskkill exit={cleanup.returncode}，"
                        f"stdout={cleanup.stdout!r}，stderr={cleanup.stderr!r}"
                    )


def _seed(root: Path, *, pid: int = 424242) -> host.HostInfo:
    info = host.HostInfo(pid=pid, port=0, token="old", started_at=1)
    host.write_host_info(root, info)
    return info


@pytest.mark.parametrize("same_pid", [False, True], ids=["new-pid", "reused-pid"])
def test_stale_stop_preserves_replacement_process_info(tmp_path: Path, same_pid: bool) -> None:
    """stop 旧快照不能清掉另一真实进程的新 token，包括 PID 被复用的形状。"""
    with _peer(tmp_path, "replace") as owner:
        _seed(tmp_path, pid=owner.pid if same_pid else 424242)
        with _peer(tmp_path, "stop") as stopper:
            snapshot = stopper.receive()
            assert snapshot["snapshot"] == (owner.pid if same_pid else 424242)
            owner.send("go")
            assert owner.receive() == {"written": True, "pid": owner.pid}
            replacement = host.read_host_info(tmp_path)
            assert replacement is not None and replacement.token != "old"
            stopper.send("go")
            assert stopper.receive() == {"result": 0}
            assert stopper.proc.wait(timeout=10) == 0
            assert owner.proc.poll() is None
            assert host.read_host_info(tmp_path) == replacement
        owner.send("exit")
        assert owner.proc.wait(timeout=10) == 0


def test_stale_acquire_health_result_cannot_overwrite_new_process(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _peer(tmp_path, "acquire") as contender:
        assert contender.receive() == {"snapshot": 424242}
        with _peer(tmp_path, "replace") as owner:
            owner.send("go")
            assert owner.receive() == {"written": True, "pid": owner.pid}
            replacement = host.read_host_info(tmp_path)
            assert replacement is not None and replacement.pid == owner.pid
            contender.send("go")
            assert contender.receive() == {"refused": True}
            assert contender.proc.wait(timeout=10) == 0
            assert host.read_host_info(tmp_path) == replacement
            owner.send("exit")
            assert owner.proc.wait(timeout=10) == 0


def test_old_port_publisher_cannot_overwrite_replacement(tmp_path: Path) -> None:
    old = _seed(tmp_path)
    replacement = host.HostInfo(old.pid, 0, "replacement", old.started_at + 1)
    host.write_host_info(tmp_path, replacement)
    old.port = 12345
    with pytest.raises(host.HostError):
        host.write_host_info(tmp_path, old, expected=old)
    assert host.read_host_info(tmp_path) == replacement


def test_same_instance_can_publish_its_new_port(tmp_path: Path) -> None:
    info = _seed(tmp_path)
    info.port = 12345
    host.write_host_info(tmp_path, info, expected=info)
    assert host.read_host_info(tmp_path) == info


def test_late_real_host_cannot_replace_the_running_owner_or_stop_its_daemon() -> None:
    """A 真 ensure 后暂停，B 真启动；A 必须非零退出，B 与共享 Daemon 仍可用。"""
    with tempfile.TemporaryDirectory(prefix="hstop-", dir=None if os.name == "nt" else "/tmp") as d:
        root = Path(d)
        try:
            with _peer(root, "host:first") as first:
                assert first.receive() == {"ensured": True}
                assert ipc.probe(root, timeout=1)
                initial = host.read_host_info(root)
                assert initial is not None and initial.pid == first.pid and initial.port == 0
                with _peer(root, "host:second") as second:
                    assert second.receive() == {"ready": True}
                    winner = host.read_host_info(root)
                    assert winner is not None and winner.pid == second.pid
                    assert host._http_alive(winner)
                    first.send("publish")
                    assert first.receive() == {
                        "exit": 1, "bridge_closed": True, "monitor_alive": False, "log_open": False
                    }
                    assert first.proc.wait(timeout=10) == 1
                    assert host.read_host_info(root) == winner
                    assert host._http_alive(winner)
                    assert ipc.probe(root, timeout=1)
                    assert host.stop_host(root, timeout=20) == 0
                    assert second.receive() == {
                        "exit": 0, "bridge_closed": True, "monitor_alive": False, "log_open": False
                    }
                    assert second.proc.wait(timeout=10) == 0
                    assert not ipc.probe(root, timeout=0.5)
                    assert host.read_host_info(root) is None
        finally:
            daemon.request_shutdown(root, reason="test_cleanup", timeout=5)


def test_real_host_publication_and_service_start_cannot_be_taken_over() -> None:
    """端口已写、serve尚未开始时，第二真实Host不能接管出两个活桥。"""
    with tempfile.TemporaryDirectory(prefix="hpub-", dir=None if os.name == "nt" else "/tmp") as d:
        root = Path(d)
        try:
            with _peer(root, "host:first-after-publish") as first:
                before_serve = first.receive()
                assert before_serve["before_serve"] is True and before_serve["port"] > 0
                assert ipc.probe(root, timeout=1)
                with _peer(root, "host:second-after-publish") as second:
                    # 只在真实内核锁返回busy后放行A；不等待本来就该被挡住的B ready。
                    assert second.receive() == {"blocked": True}
                    first.send("serve")
                    assert first.receive() == {"ready": True}
                    assert second.receive() == {
                        "exit": 1, "bridge_closed": True, "monitor_alive": False, "log_open": False
                    }
                    assert second.proc.wait(timeout=10) == 1
                    winner = host.read_host_info(root)
                    assert winner is not None and winner.pid == first.pid
                    assert winner.port == before_serve["port"]
                    assert host._http_alive(winner)
                    assert ipc.probe(root, timeout=1)
                    assert host.stop_host(root, timeout=20) == 0
                    assert first.receive() == {
                        "exit": 0, "bridge_closed": True, "monitor_alive": False, "log_open": False
                    }
                    assert first.proc.wait(timeout=10) == 0
                    assert not ipc.probe(root, timeout=0.5)
                    assert host.read_host_info(root) is None
        finally:
            daemon.request_shutdown(root, reason="test_cleanup", timeout=5)


def test_real_stop_cannot_join_monitor_before_its_thread_has_started() -> None:
    """在真实Thread.start处暂停，HTTP可达就真停；验收异常与资源收尾。"""
    with tempfile.TemporaryDirectory(prefix="hmon-", dir=None if os.name == "nt" else "/tmp") as d:
        root = Path(d)
        try:
            with _peer(root, "host:monitor-held") as running:
                boundary = running.receive()
                if boundary == {"ready": True}:
                    boundary = running.receive()
                assert boundary["monitor_start_held"] is True
                assert boundary["thread_published"] is True
                released = False
                try:
                    # 观测期限到达不代表放弃这条真实请求。若服务尚未启动，保持
                    # 同一TCP连接到放行后读完，避免探针主动断线污染后台stderr。
                    with socket.create_connection(
                        ("127.0.0.1", boundary["port"]), timeout=0.4
                    ) as client:
                        client.sendall(
                            f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{boundary['port']}\r\n"
                            "Connection: close\r\n\r\n".encode("ascii")
                        )
                        response = b""
                        http_alive_before_start = True
                        try:
                            while chunk := client.recv(65536):
                                response += chunk
                        except TimeoutError:
                            assert response == b"", ("partial boundary response", response)
                            http_alive_before_start = False
                        if not http_alive_before_start:
                            assert client.fileno() >= 0
                            running.send("start")
                            released = True
                            assert running.receive() == {"ready": True}
                            # 边界观测仍为0.4秒；服务确认启动后只读完同一请求。
                            client.settimeout(5)
                            while chunk := client.recv(65536):
                                response += chunk
                        headers, separator, body = response.partition(b"\r\n\r\n")
                        assert separator and headers.split(b"\r\n")[0].split()[1] == b"200"
                        health = json.loads(body)
                        assert health == {"ok": True, "pid": running.pid}
                    env = {
                        k: v for k, v in os.environ.items()
                        if not k.startswith(("TIANSHU_", "OPENAI_", "ANTHROPIC_"))
                    }
                    env["PYTHONPATH"] = str(Path(host.__file__).resolve().parents[1])
                    stopper = subprocess.run(
                        [sys.executable, "-m", "tianshu", "host", "stop", "--data-root", str(root)],
                        capture_output=True, env=env, timeout=20,
                    )
                finally:
                    if not released and running.proc.poll() is None:
                        running.send("start")
                outcome = running.receive()
                _, stderr = running.proc.communicate(timeout=10)
                assert stopper.returncode == 0, stopper.stderr.decode()
                assert running.proc.returncode == 0, stderr.decode()
                assert not ipc.probe(root, timeout=0.5)
                assert host.read_host_info(root) is None
                assert outcome == {
                    "exit": 0, "bridge_closed": True, "monitor_alive": False, "log_open": False
                }, stderr.decode()
                assert stderr == b""
                assert not http_alive_before_start
        finally:
            daemon.request_shutdown(root, reason="test_cleanup", timeout=5)


@pytest.mark.parametrize("kind", ["invalid-json", "directory"])
def test_unreadable_host_info_is_not_reported_as_absent(tmp_path: Path, kind: str) -> None:
    path = host.host_lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    if kind == "invalid-json":
        path.write_bytes(b"{")
    else:
        path.mkdir()
    with pytest.raises(host.HostError):
        host.read_host_info(tmp_path)
    assert host.stop_host(tmp_path) != 0
    assert path.exists()


@pytest.mark.skipif(os.name == "nt", reason="chmod 000 读权限语义只适用于 POSIX")
def test_real_read_permission_denial_cannot_become_success(tmp_path: Path) -> None:
    _seed(tmp_path)
    path = host.host_lock_path(tmp_path)
    path.chmod(0)
    try:
        with pytest.raises(host.HostError):
            host.read_host_info(tmp_path)
        assert host.stop_host(tmp_path) != 0
        assert path.exists()
    finally:
        path.chmod(0o600)


@pytest.mark.skipif(os.name != "nt", reason="真实无 FILE_SHARE_READ 句柄只适用于 Windows")
def test_windows_real_read_denial_cannot_become_success(tmp_path: Path) -> None:
    original_info = _seed(tmp_path)
    path = host.host_lock_path(tmp_path)
    original_bytes = path.read_bytes()
    with _peer(tmp_path, "deny-read") as holder:
        assert holder.receive() == {"read_denied": True}
        try:
            with pytest.raises(host.HostError) as caught:
                host.read_host_info(tmp_path)
            # Path.read_bytes经CRT得到EACCES；不要求已经丢失的原生winerror32。
            assert isinstance(caught.value.__cause__, PermissionError)
            assert caught.value.__cause__.errno == errno.EACCES
            assert host.stop_host(tmp_path) == 1
        finally:
            holder.send("exit")
            assert holder.proc.wait(timeout=10) == 0
    assert path.read_bytes() == original_bytes
    assert host.read_host_info(tmp_path) == original_info


def test_real_delete_permission_denial_is_explicit_stop_failure(tmp_path: Path) -> None:
    _seed(tmp_path)
    path = host.host_lock_path(tmp_path)
    target = path if os.name == "nt" else path.parent
    target.chmod(stat.S_IREAD if os.name == "nt" else 0o500)
    try:
        assert host.stop_host(tmp_path) != 0
        assert path.is_file()
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o700)


@pytest.mark.parametrize("operation", ["read", "write", "clear", "acquire"])
def test_metadata_operations_share_a_process_guard(tmp_path: Path, operation: str) -> None:
    """真实第二进程持锁期间，读/写/条件删/接管都不能穿过同一互斥。"""
    _seed(tmp_path)
    with _peer(tmp_path, "guard") as holder:
        assert holder.receive() == {"locked": True}
        with _peer(tmp_path, f"operation:{operation}") as actor:
            assert actor.receive() == {"blocked": True}
            holder.send("exit")
            assert holder.proc.wait(timeout=10) == 0
            assert actor.receive() == {"done": True}
            assert actor.proc.wait(timeout=10) == 0


def test_metadata_guard_is_released_by_real_process_crash(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _peer(tmp_path, "guard") as holder:
        assert holder.receive() == {"locked": True}
        with _peer(tmp_path, "operation:clear") as cleaner:
            assert cleaner.receive() == {"blocked": True}
            holder.send("crash")
            assert holder.proc.wait(timeout=10) == 23
            assert cleaner.receive() == {"done": True}
            assert cleaner.proc.wait(timeout=10) == 0
    assert not host.host_lock_path(tmp_path).exists()


def test_guard_timeout_is_an_explicit_stop_failure(tmp_path: Path) -> None:
    original = _seed(tmp_path)
    with _peer(tmp_path, "guard") as holder:
        assert holder.receive() == {"locked": True}
        with _peer(tmp_path, "operation:stop") as stopper:
            assert stopper.receive() == {"blocked": True}
            # 整个既有 5 秒锁预算内都由真实子进程持有，不用 sleep 猜窗口。
            assert stopper.receive() == {"result": 1}
            assert stopper.proc.wait(timeout=10) == 0
        holder.send("exit")
        assert holder.proc.wait(timeout=10) == 0
    assert host.read_host_info(tmp_path) == original


def test_missing_host_info_cleanup_remains_idempotent(tmp_path: Path) -> None:
    assert host.read_host_info(tmp_path) is None
    host.clear_host_info(tmp_path)
    host.clear_host_info(tmp_path)
    assert host.stop_host(tmp_path) == 0
    assert not host.host_lock_path(tmp_path).exists()
