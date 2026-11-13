#!/usr/bin/env python3
"""Independent real-process Host metadata ordering probes with actual OS lock outcomes."""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(value):
    print(json.dumps(value), flush=True)


def load(repo):
    sys.path.insert(0, str(Path(repo) / "src"))
    from tianshu import host, ipc, leases

    return host, ipc, leases


def worker(args):
    host, ipc, leases = load(args.repo)
    root = Path(args.root)
    target = host.host_lock_path(root)
    busy_reported = False
    original_try = leases._try_lock

    def observe_lock(fd):
        nonlocal busy_reported
        try:
            original_try(fd)
        except OSError as exc:
            if not busy_reported:
                busy_reported = True
                emit(
                    {
                        "phase": "LOCK_BUSY",
                        "pid": os.getpid(),
                        "errno": exc.errno,
                        "winerror": getattr(exc, "winerror", None),
                    }
                )
                assert sys.stdin.readline().strip() == "RETRY"
            raise

    if args.role.endswith("_observe"):
        leases._try_lock = observe_lock
    if args.role == "clear_hold_read":
        original_read = Path.read_bytes
        held = False

        def read_at_boundary(self):
            nonlocal held
            value = original_read(self)
            if self == target and not held:
                held = True
                emit({"phase": "READ_HELD", "pid": os.getpid()})
                assert sys.stdin.readline().strip() == "RELEASE"
            return value

        Path.read_bytes = read_at_boundary
    if args.role == "write_hold_partial":
        original_write = Path.write_bytes

        def write_at_boundary(self, data):
            if self != target:
                return original_write(self, data)
            split = max(1, len(data) // 2)
            with self.open("wb") as stream:
                stream.write(data[:split])
                stream.flush()
                emit({"phase": "WRITE_HALF_HELD", "pid": os.getpid()})
                assert sys.stdin.readline().strip() == "RELEASE"
                stream.write(data[split:])
                stream.flush()
            return len(data)

        Path.write_bytes = write_at_boundary
    started = time.monotonic()
    try:
        if args.role in ("hold_meta", "hold_business"):
            directory = ipc.run_dir(root) if args.role == "hold_meta" else root
            with leases.execution_guard(directory):
                emit({"phase": "GUARD_HELD", "pid": os.getpid(), "scope": args.role})
                assert sys.stdin.readline().strip() == "RELEASE"
            value = None
        elif args.role.startswith("clear"):
            value = host.clear_host_info(root, pid=424242)
        elif args.role.startswith("write"):
            value = host.write_host_info(
                root, host.HostInfo(pid=424243, port=0, token="new-probe", started_at=2)
            )
        else:
            current = host.read_host_info(root)
            value = (
                None if current is None else {"pid": current.pid, "started_at": current.started_at}
            )
        emit(
            {
                "phase": "RESULT",
                "value": value,
                "error": None,
                "elapsed_s": time.monotonic() - started,
            }
        )
    except BaseException as exc:
        emit(
            {
                "phase": "RESULT",
                "error": type(exc).__name__,
                "message": str(exc),
                "elapsed_s": time.monotonic() - started,
            }
        )


def run(args):
    host, ipc, _leases = load(args.repo)
    safety = (
        Path(args.helper)
        if args.helper
        else Path(__file__).with_name("verify-host-cleanup-boundaries.py")
    )
    spec = importlib.util.spec_from_file_location("probe_safety", safety)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    results = {
        "repo": str(Path(args.repo).resolve()),
        "python": sys.version,
        "platform": sys.platform,
        "head": subprocess.run(
            ["git", "-C", args.repo, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "host_sha256": hashlib.sha256(
            (Path(args.repo) / "src/tianshu/host.py").read_bytes()
        ).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": [],
    }
    with helper.probe_root() as base:

        def spawn(root, role):
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
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            helper.CHILDREN.append(proc)
            return proc

        def seed(name):
            root = base / name
            host.write_host_info(
                root, host.HostInfo(pid=424242, port=0, token="old-probe", started_at=1)
            )
            return root

        def complete(proc):
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == 0, (out, err, proc.returncode)
            assert not out.strip(), ("unexpected remaining protocol", out)

        for name, first_role, second_role in (
            ("clear_then_writer", "clear_hold_read", "write_observe"),
            ("clear_then_clear", "clear_hold_read", "clear_observe"),
            ("partial_writer_then_reader", "write_hold_partial", "read_observe"),
        ):
            root = seed(name)
            first = spawn(root, first_role)
            first_event = helper.line(first)
            assert first_event["phase"] in ("READ_HELD", "WRITE_HALF_HELD"), first_event
            second = spawn(root, second_role)
            second_event = helper.line(second)
            assert second_event["phase"] in ("LOCK_BUSY", "RESULT"), second_event
            helper.signal(first, "RELEASE")
            first_result = helper.line(first)
            assert first_result["phase"] == "RESULT", first_result
            complete(first)
            if second_event["phase"] == "LOCK_BUSY":
                helper.signal(second, "RETRY")
                second_result = helper.line(second)
            else:
                second_result = second_event
            complete(second)
            current = host.read_host_info(root)
            results["cases"].append(
                {
                    "name": name,
                    "first_event": first_event,
                    "second_event": second_event,
                    "first_result": first_result,
                    "second_result": second_result,
                    "current_owner": None if current is None else current.pid,
                }
            )

        for name, held_scope, hard_exit in (
            ("metadata_guard_normal_release", "hold_meta", False),
            ("metadata_guard_hard_exit", "hold_meta", True),
            ("business_guard_is_separate", "hold_business", False),
        ):
            root = seed(name)
            holder = spawn(root, held_scope)
            held = helper.line(holder)
            assert held["phase"] == "GUARD_HELD"
            reader = spawn(root, "read_observe")
            event = helper.line(reader)
            if hard_exit:
                holder.kill()
                holder.communicate(timeout=5)
            else:
                helper.signal(holder, "RELEASE")
                assert helper.line(holder)["phase"] == "RESULT"
                complete(holder)
            if event["phase"] == "LOCK_BUSY":
                helper.signal(reader, "RETRY")
                outcome = helper.line(reader)
            else:
                outcome = event
            complete(reader)
            results["cases"].append(
                {
                    "name": name,
                    "first_reader_event": event,
                    "reader_result": outcome,
                    "holder_returncode": holder.returncode,
                }
            )

        root = seed("metadata_guard_timeout")
        holder = spawn(root, "hold_meta")
        assert helper.line(holder)["phase"] == "GUARD_HELD"
        reader = spawn(root, "read_plain")
        outcome = helper.line(reader)
        assert outcome["phase"] == "RESULT"
        complete(reader)
        helper.signal(holder, "RELEASE")
        assert helper.line(holder)["phase"] == "RESULT"
        complete(holder)
        results["cases"].append(
            {
                "name": "metadata_guard_timeout",
                "result": outcome,
                "metadata_exists": host.host_lock_path(root).exists(),
                "metadata_guard_exists": (ipc.run_dir(root) / "execution.guard").exists(),
            }
        )
    output = Path(args.output)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    emit(
        {
            "output": str(output),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "cases": results["cases"],
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root")
    parser.add_argument("--role")
    parser.add_argument("--output", required=False)
    parser.add_argument("--helper", required=False)
    args = parser.parse_args()
    if args.role:
        worker(args)
    else:
        run(args)
