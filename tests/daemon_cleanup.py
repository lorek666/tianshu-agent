from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from tianshu import daemon
from tianshu.execution_admission import process_birth_identity
from tianshu.leases import pid_alive


def _observed_process_alive(pid: int, birth: str | None) -> bool:
    if not pid_alive(pid):
        return False
    if birth is None:
        return True
    return process_birth_identity(pid) in (None, birth)


def shutdown_test_daemon(root: Path, *, timeout: float = 5.0) -> None:
    lock_path = daemon.lock_path(root)
    try:
        snapshot = lock_path.read_bytes()
    except FileNotFoundError:
        return
    identity = json.loads(snapshot)
    pid = identity["pid"]
    if type(pid) is not int or pid <= 0:
        raise AssertionError("test Daemon lock has an invalid PID")
    birth = process_birth_identity(pid)
    deadline = time.monotonic() + timeout
    daemon.request_shutdown(
        root,
        expected_instance_id=identity["instance_id"],
        timeout=max(0.1, deadline - time.monotonic()),
    )
    while True:
        if not _observed_process_alive(pid, birth):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                "test Daemon process did not exit after graceful shutdown; "
                "refusing to terminate an unowned PID"
            )
        threading.Event().wait(min(0.02, remaining))
