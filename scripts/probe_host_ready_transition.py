#!/usr/bin/env python3
"""Real Host publication/serving boundary with a lock-aware contender handshake."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path


def emit(value):
    print(json.dumps(value), file=sys.__stdout__, flush=True)


def load(repo):
    sys.path.insert(0, str(Path(repo) / "src"))
    from tianshu import daemon, host, ipc, leases

    return daemon, host, ipc, leases


def worker(args):
    _daemon, host, _ipc, leases = load(args.repo)
    emit({"phase": "WORKER_READY", "role": args.role, "pid": os.getpid()})
    original_serve = host.UiBridge.serve_background
    original_lock = leases._try_lock
    notified = False

    def try_lock(fd):
        nonlocal notified
        try:
            original_lock(fd)
        except OSError as exc:
            if args.role == "second" and not notified:
                notified = True
                emit(
                    {
                        "phase": "LOCK_BUSY",
                        "role": args.role,
                        "pid": os.getpid(),
                        "errno": exc.errno,
                        "winerror": getattr(exc, "winerror", None),
                    }
                )
                assert sys.stdin.readline().strip() == "RETRY"
            raise

    def serve(self):
        # The ephemeral test token travels only through the private protocol;
        # the parent strips it from every persisted result.
        identity = {
            "role": args.role, "pid": os.getpid(), "port": self.port, "token": self.config.token
        }
        if args.role == "first":
            emit({"phase": "SERVE_HELD", **identity})
            assert sys.stdin.readline().strip() == "SERVE"
        original_serve(self)
        emit({"phase": "SERVER_STARTED", **identity})

    leases._try_lock = try_lock
    host.UiBridge.serve_background = serve
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            returned = host.run_host(
                Path(args.root),
                ui_dir=Path(args.repo) / "ui/dist",
                daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
                supervise=False,
            )
        emit({
            "phase": "EXIT", "role": args.role, "pid": os.getpid(),
            "returned": returned, "stdout": output.getvalue(),
        })
        return returned
    except Exception as exc:
        emit({
            "phase": "EXIT", "role": args.role, "pid": os.getpid(),
            "error": type(exc).__name__, "message": str(exc),
        })
        return 2


def run(args):
    daemon, host, ipc, _leases = load(args.repo)
    safety = (
        Path(args.helper)
        if args.helper
        else Path(__file__).with_name("verify-host-cleanup-boundaries.py")
    )
    spec = importlib.util.spec_from_file_location("probe_safety", safety)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TIANSHU_", "ANTHROPIC_", "OPENAI_"))
    }
    env.update({"PYTHONPATH": str(Path(args.repo) / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    result = {
        "repo": str(Path(args.repo).resolve()),
        "host_sha256": hashlib.sha256(
            (Path(args.repo) / "src/tianshu/host.py").read_bytes()
        ).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "events": [],
        "workers": {},
    }
    snapshots = []
    children = {}
    exit_events = {}

    def record(event):
        result["events"].append({k: v for k, v in event.items() if k != "token"})
        if event["phase"] == "SERVER_STARTED":
            snapshots.append(host.HostInfo(event["pid"], event["port"], event["token"], 0))
        elif event["phase"] == "EXIT":
            exit_events[event["role"]] = event

    with tempfile.TemporaryDirectory(
        prefix="c007-ready-", dir=None if os.name == "nt" else "/tmp"
    ) as temp:
        root = Path(temp)

        def spawn(role):
            proc = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "--repo",
                    args.repo,
                    "--root",
                    str(root),
                    "--role",
                    role,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            helper.CHILDREN.append(proc)
            children[role] = proc
            ready = helper.line(proc)
            assert ready["phase"] == "WORKER_READY" and ready["role"] == role, ready
            assert isinstance(ready["pid"], int) and ready["pid"] > 0, ready
            result["workers"][role] = {
                "worker_pid": ready["pid"], "launcher_pid": proc.pid, "handshake": ready
            }
            return proc

        try:
            first = spawn("first")
            first_held = helper.line(first)
            record(first_held)
            assert first_held["phase"] == "SERVE_HELD", first_held
            assert ipc.probe(root, timeout=1), "real Daemon was not ready"
            second = spawn("second")
            second_event = helper.line(second)
            record(second_event)
            assert second_event["phase"] in ("LOCK_BUSY", "SERVER_STARTED", "EXIT"), second_event
            helper.signal(first, "SERVE")
            first_event = helper.line(first)
            record(first_event)
            assert first_event["phase"] in ("SERVER_STARTED", "EXIT"), first_event
            if second_event["phase"] == "LOCK_BUSY":
                helper.signal(second, "RETRY")
                second_event = helper.line(second)
                record(second_event)
            current = host.read_host_info(root)
            result["current_owner"] = (
                None if current is None else {"pid": current.pid, "port": current.port}
            )
            result["snapshot_health"] = [
                {"pid": item.pid, "port": item.port, "alive": host._http_alive(item)}
                for item in snapshots
            ]
            result["daemon_alive_before_cleanup"] = ipc.probe(root, timeout=1)
            result["two_distinct_hosts_alive"] = (
                len({item["pid"] for item in result["snapshot_health"] if item["alive"]}) > 1
            )
        finally:
            for info in snapshots:
                if host._http_alive(info):
                    try:
                        host._http_post(info.port, "/shutdown", info.token, timeout=5)
                    except OSError as exc:
                        result.setdefault("cleanup_http_errors", []).append(type(exc).__name__)
            for role, proc in children.items():
                try:
                    out, err = proc.communicate(timeout=20)
                    for line in out.splitlines():
                        event = json.loads(line)
                        assert event["phase"] == "EXIT" and event["role"] == role, event
                        assert role not in exit_events, (role, exit_events)
                        exit_events[role] = event
                    result.setdefault("process_exits", []).append(
                        {
                            "role": role,
                            "worker_pid": result["workers"][role]["worker_pid"],
                            "launcher_pid": proc.pid,
                            "exit": proc.returncode,
                            "event": exit_events.get(role),
                            "stdout": out,
                            "stderr": err,
                        }
                    )
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=5)
                    result.setdefault("forced_cleanup", []).append(proc.pid)
            if ipc.probe(root, timeout=0.5):
                daemon.request_shutdown(root, reason="independent_probe_cleanup", timeout=5)
            deadline = time.monotonic() + 5
            while ipc.probe(root, timeout=0.1) and time.monotonic() < deadline:
                time.sleep(0.05)  # Bounded cleanup, never a scheduling assertion.
            result["daemon_alive_after_cleanup"] = ipc.probe(root, timeout=0.5)
            helper.reap_children()
            assert not result["daemon_alive_after_cleanup"]
    target = Path(args.output)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    emit(
        {
            "output": str(target),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "result": result,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root")
    parser.add_argument("--role")
    parser.add_argument("--output")
    parser.add_argument("--helper")
    args = parser.parse_args()
    if args.role:
        raise SystemExit(worker(args))
    run(args)
