#!/usr/bin/env python3
"""Independent real-process cleanup probes; no product-code writes or fake OS errors."""

import argparse
import ctypes
import hashlib
import io
import json
import os
import platform
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

OWNER_PID = 424242
CHILDREN = []


def emit(value):
    print(json.dumps(value, ensure_ascii=False), file=sys.__stdout__, flush=True)


def load_host(repo):
    sys.path.insert(0, str(Path(repo) / "src"))
    from tianshu import host

    return host


def child(args):
    host = load_host(args.repo)
    root = Path(args.root)
    if args.child == "hold_read":
        with host.host_lock_path(root).open("rb") as handle:
            handle.read()
            emit({"ready": True, "pid": os.getpid()})
            assert sys.stdin.readline().strip() == "CLOSE"
        emit({"closed": True})
        return
    if args.child == "new_owner":
        info = host.acquire_host_info(root, ui_dir=root / "ui")
        emit({"ready": True, "pid": info.pid, "started_at": info.started_at})
        assert sys.stdin.readline().strip() == "CLOSE"
        return
    ready_sent = False
    original_read = host.read_host_info

    def gated_read(data_root):
        nonlocal ready_sent
        value = original_read(data_root)
        if not ready_sent:
            ready_sent = True
            emit(
                {
                    "ready": True,
                    "pid": os.getpid(),
                    "read_owner": None if value is None else value.pid,
                }
            )
            assert sys.stdin.readline().strip() == "GO"
        return value

    if args.child == "clear_after_read":
        host.read_host_info = gated_read
    elif args.child == "stop_before_health":
        original_alive = host._http_alive

        def gated_alive(info, **kwargs):
            nonlocal ready_sent
            if not ready_sent:
                ready_sent = True
                emit({"ready": True, "pid": os.getpid(), "read_owner": info.pid})
                assert sys.stdin.readline().strip() == "GO"
            return original_alive(info, **kwargs)

        host._http_alive = gated_alive
    before = time.perf_counter()
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            if args.child == "stop_before_health":
                value = host.stop_host(root, timeout=0.5)
            else:
                value = host.clear_host_info(root, pid=OWNER_PID if args.expected_owner else None)
        result = {"returned": value, "error": None}
    except BaseException as exc:
        result = {
            "returned": None,
            "error": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None),
            "message": str(exc),
        }
    result.update(
        {"elapsed_s": time.perf_counter() - before, "stdout": output.getvalue(), "pid": os.getpid()}
    )
    emit(result)


def spawn(args, root, mode, *, expected=True):
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--repo",
        args.repo,
        "--root",
        str(root),
        "--child",
        mode,
    ]
    if expected:
        cmd.append("--expected-owner")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    CHILDREN.append(process)
    return process


def line(proc):
    # Pipe handshakes deliberately gate the real operation; no timing sleeps.
    result = queue.Queue(maxsize=1)

    def receive():
        try:
            result.put(("line", proc.stdout.readline()))
        except BaseException as exc:
            result.put(("error", repr(exc)))

    reader = threading.Thread(target=receive, name=f"probe-handshake-{proc.pid}", daemon=True)
    reader.start()
    try:
        kind, raw = result.get(timeout=15)
    except queue.Empty as exc:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=5)
        raise TimeoutError(f"probe handshake exceeded 15s, child pid={proc.pid}") from exc
    reader.join(timeout=1)
    if reader.is_alive():
        raise RuntimeError(f"probe handshake reader did not finish, child pid={proc.pid}")
    if kind == "error":
        raise RuntimeError(f"probe handshake read failed, child pid={proc.pid}: {raw}")
    if not raw:
        raise RuntimeError(
            f"child closed stdout before handshake: pid={proc.pid}, returncode={proc.poll()}"
        )
    return json.loads(raw)


def reap_children():
    errors = []
    for proc in CHILDREN:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        except BaseException as exc:
            errors.append(f"pid={proc.pid}: {type(exc).__name__}: {exc}")
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
    if errors:
        raise RuntimeError("probe child cleanup failed: " + "; ".join(errors))


@contextmanager
def probe_root():
    with tempfile.TemporaryDirectory(prefix="verify-host-cleanup-") as temp:
        try:
            yield Path(temp)
        finally:
            # Reap every child before Windows TemporaryDirectory removes files.
            reap_children()


def signal(proc, message):
    proc.stdin.write(message + "\n")
    proc.stdin.flush()


def finish(proc, message=None):
    out, err = proc.communicate(None if message is None else message + "\n", timeout=15)
    assert proc.returncode == 0, (proc.returncode, out, err)
    items = [json.loads(x) for x in out.splitlines() if x]
    assert len(items) == 1, (items, err)
    return items[0]


def seed(host, root):
    root.mkdir(parents=True, exist_ok=True)
    host.write_host_info(
        root, host.HostInfo(pid=OWNER_PID, port=0, token="probe-only", started_at=1)
    )


def windows_shared_delete_handle(path):
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return api, handle


def run(args):
    host = load_host(args.repo)
    results = {
        "platform": platform.platform(),
        "python": sys.version,
        "repo": str(Path(args.repo).resolve()),
        "head": subprocess.run(
            ["git", "-C", args.repo, "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip(),
        "host_sha256": hashlib.sha256(
            (Path(args.repo) / "src/tianshu/host.py").read_bytes()
        ).hexdigest(),
        "cases": [],
    }
    with probe_root() as base:
        root = base / "absent"
        a = spawn(args, root, "clear", expected=True)
        results["cases"].append({"name": "missing_is_idempotent", "result": finish(a)})

        for iteration in range(args.repetitions):
            root = base / f"double-{iteration}"
            seed(host, root)
            children = [spawn(args, root, "clear_after_read") for _ in range(2)]
            ready = [line(p) for p in children]
            for p in children:
                signal(p, "GO")
            outcomes = [finish(p) for p in children]
            results["cases"].append(
                {
                    "name": "two_clear_after_same_read",
                    "iteration": iteration,
                    "ready": ready,
                    "results": outcomes,
                    "exists_after": host.host_lock_path(root).exists(),
                }
            )

        root = base / "reader"
        seed(host, root)
        holder = spawn(args, root, "hold_read")
        ready = line(holder)
        cleaner = spawn(args, root, "clear")
        outcome = finish(cleaner)
        after = host.host_lock_path(root).exists()
        released = finish(holder, "CLOSE")
        results["cases"].append(
            {
                "name": "real_open_reader_blocks_unlink",
                "holder": ready,
                "result": outcome,
                "exists_while_reader": after,
                "released": released,
            }
        )

        root = base / "different-owner"
        seed(host, root)
        host.write_host_info(
            root, host.HostInfo(pid=OWNER_PID + 1, port=0, token="new-probe", started_at=2)
        )
        cleaner = spawn(args, root, "clear")
        outcome = finish(cleaner)
        current = host.read_host_info(root)
        results["cases"].append(
            {
                "name": "preexisting_different_owner_is_preserved",
                "result": outcome,
                "preserved": current is not None and current.pid == OWNER_PID + 1,
            }
        )

        root = base / "new-owner-before-stop-cleanup"
        seed(host, root)
        stopper = spawn(args, root, "stop_before_health", expected=False)
        ready = line(stopper)
        new_owner = spawn(args, root, "new_owner")
        owner = line(new_owner)
        signal(stopper, "GO")
        outcome = finish(stopper)
        current = host.read_host_info(root)
        results["cases"].append(
            {
                "name": "stop_snapshot_does_not_own_replacement",
                "old": ready,
                "new": owner,
                "result": outcome,
                "new_owner_process_alive": new_owner.poll() is None,
                "preserved": current is not None and current.pid == owner["pid"],
            }
        )
        signal(new_owner, "CLOSE")
        new_owner.communicate(timeout=15)
        assert new_owner.returncode == 0

        if os.name == "nt":
            root = base / "delete-pending"
            seed(host, root)
            path = host.host_lock_path(root)
            api, handle = windows_shared_delete_handle(path)
            try:
                cleaner = spawn(args, root, "clear_after_read")
                ready = line(cleaner)
                # Real DeleteFile succeeds; the share-delete reader keeps it pending.
                path.unlink()
                signal(cleaner, "GO")
                outcome = finish(cleaner)
            finally:
                assert api.CloseHandle(handle)
            results["cases"].append(
                {
                    "name": "real_delete_pending_second_unlink",
                    "ready": ready,
                    "result": outcome,
                    "exists_after_handle_close": path.exists(),
                }
            )

            root = base / "readonly"
            seed(host, root)
            path = host.host_lock_path(root)
            path.chmod(stat.S_IREAD)
            try:
                cleaner = spawn(args, root, "clear")
                outcome = finish(cleaner)
                exists = path.exists()
            finally:
                path.chmod(stat.S_IWRITE)
            results["cases"].append(
                {
                    "name": "permanent_readonly_access_denied_must_not_be_swallowed",
                    "result": outcome,
                    "exists_after": exists,
                }
            )
        else:
            results["windows_only_cases"] = (
                "not run: native delete-pending and read-only denial require actual Windows"
            )
    destination = Path(args.output)
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    emit(
        {
            "output": str(destination),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "cases": results["cases"],
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root")
    parser.add_argument("--output", default="host-cleanup-probe.json")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--child")
    parser.add_argument("--expected-owner", action="store_true")
    arguments = parser.parse_args()
    if arguments.child:
        child(arguments)
    else:
        run(arguments)
