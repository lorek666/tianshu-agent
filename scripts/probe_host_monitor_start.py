#!/usr/bin/env python3
"""Observe real shutdown at the real supervisor Thread.start boundary."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path


def emit(value):
    print(json.dumps(value), file=sys.__stdout__, flush=True)


def load(repo):
    sys.path.insert(0, str(Path(repo) / "src"))
    from tianshu import daemon, host, ipc

    return daemon, host, ipc


def worker(args):
    _daemon, host, _ipc = load(args.repo)
    bridges = []
    supervisors = []
    original_bridge = host.UiBridge.__init__
    original_supervisor = host.DaemonSupervisor.__init__
    original_serve = host.UiBridge.serve_background
    original_start = threading.Thread.start

    def bridge_init(self, config):
        original_bridge(self, config)
        bridges.append(self)

    def supervisor_init(self, *values, **kwargs):
        original_supervisor(self, *values, **kwargs)
        supervisors.append(self)

    def serve(self):
        original_serve(self)
        emit({"phase": "SERVER_READY", "port": self.port})

    def thread_start(self):
        if self.name == "daemon-supervisor":
            # Only delay a real operation; never fake a Thread or an exception.
            emit(
                {
                    "phase": "MONITOR_START_HELD",
                    "pid": os.getpid(),
                    "port": bridges[0].port if bridges else 0,
                    "token": bridges[0].config.token if bridges else "",
                    "thread_published": supervisors[0]._thread is self,
                }
            )
            assert sys.stdin.readline().strip() == "START"
        return original_start(self)

    host.UiBridge.__init__ = bridge_init
    host.DaemonSupervisor.__init__ = supervisor_init
    host.UiBridge.serve_background = serve
    threading.Thread.start = thread_start
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            code = host.run_host(
                Path(args.root),
                ui_dir=Path(args.repo) / "ui/dist",
                daemon_command=[sys.executable, "-m", "tianshu", "daemon", "--model", "scripted"],
            )
        emit(
            {
                "phase": "EXIT",
                "returned": code,
                "stdout": output.getvalue(),
                "log_open": any(
                    s._log_file is not None and not s._log_file.closed for s in supervisors
                ),
                "monitor_alive": any(
                    s._thread is not None and s._thread.is_alive() for s in supervisors
                ),
                "bridge_closed": all(b.socket.fileno() == -1 for b in bridges),
            }
        )
        return code
    except Exception as exc:
        emit({"phase": "EXIT", "error": type(exc).__name__, "message": str(exc)})
        return 2


def run(args):
    daemon, host, ipc = load(args.repo)
    helper_path = (
        Path(args.helper)
        if args.helper
        else Path(__file__).with_name("verify-host-cleanup-boundaries.py")
    )
    spec = importlib.util.spec_from_file_location("probe_safety", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    result = {
        "host_sha256": hashlib.sha256(
            (Path(args.repo) / "src/tianshu/host.py").read_bytes()
        ).hexdigest(),
        "events": [],
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TIANSHU_", "OPENAI_", "ANTHROPIC_"))
    }
    env.update({"PYTHONPATH": str(Path(args.repo) / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    with tempfile.TemporaryDirectory(
        prefix="c007-mon-", dir=None if os.name == "nt" else "/tmp"
    ) as temp:
        root = Path(temp)
        proc = subprocess.Popen(
            [sys.executable, __file__, "--repo", args.repo, "--root", str(root), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        helper.CHILDREN.append(proc)
        released = False
        try:
            while True:
                event = helper.line(proc)
                result["events"].append({k: v for k, v in event.items() if k != "token"})
                if event["phase"] == "MONITOR_START_HELD":
                    break
                assert event["phase"] == "SERVER_READY", event
            info = host.HostInfo(event["pid"], event["port"], event["token"], 0)
            result["http_alive_at_thread_start_boundary"] = host._http_alive(info)
            if not result["http_alive_at_thread_start_boundary"]:
                helper.signal(proc, "START")
                released = True
                ready = helper.line(proc)
                result["events"].append(ready)
                assert ready["phase"] == "SERVER_READY", ready
            stopper = subprocess.run(
                [sys.executable, "-m", "tianshu", "host", "stop", "--data-root", str(root)],
                capture_output=True,
                text=True,
                env=env,
                timeout=20,
            )
            result["stopper"] = {
                "exit": stopper.returncode,
                "stdout": stopper.stdout,
                "stderr": stopper.stderr,
            }
            result["daemon_alive_after_stop"] = ipc.probe(root, timeout=0.5)
            if not released:
                helper.signal(proc, "START")
                released = True
            outcome = helper.line(proc)
            result["events"].append(outcome)
            assert outcome["phase"] == "EXIT", outcome
            out, err = proc.communicate(timeout=20)
            result["process_exit"] = proc.returncode
            result["remaining_stdout"] = out
            result["stderr"] = err
            result["metadata_absent_after_exit"] = host.read_host_info(root) is None
            result["daemon_alive_after_exit"] = ipc.probe(root, timeout=0.5)
        finally:
            if not released and proc.poll() is None:
                helper.signal(proc, "START")
            helper.reap_children()
            if ipc.probe(root, timeout=0.5):
                daemon.request_shutdown(root, reason="independent_probe_cleanup", timeout=5)
            deadline = time.monotonic() + 5
            while ipc.probe(root, timeout=0.1) and time.monotonic() < deadline:
                time.sleep(0.05)  # Only bounded probe cleanup, never a scheduling assertion.
            assert not ipc.probe(root, timeout=0.5), "probe cleanup left a live daemon"
    out = Path(args.output)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit(
        {"path": str(out), "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "result": result}
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--helper")
    args = parser.parse_args()
    if args.worker:
        raise SystemExit(worker(args))
    run(args)
