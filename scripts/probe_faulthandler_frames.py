"""Bounded diagnostic experiment only; no project imports or real data.

The child stresses live frame/code turnover while either CPython's native
dump_traceback_later thread or a Python thread captures stacks. A separate
parent process enforces every time limit. A lock deadlock and a disabled-dump
negative control verify that the Python-thread variant still diagnoses hangs.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path


def hold_lock_forever() -> None:
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()


def emit_python_stacks(fd: int) -> None:
    # Strong Python references are acquired while this thread holds the GIL.
    # No locals or environment values are included. Bound the diagnostic size.
    frames = sys._current_frames()
    try:
        chunks = ["PYTHON-WATCHDOG\n"]
        for ident, frame in list(frames.items())[:100]:
            chunks.append(f"Thread {ident}:\n")
            chunks.extend(traceback.format_stack(frame, limit=100))
        data = "".join(chunks).encode("utf-8", errors="backslashreplace")[:262144]
    finally:
        del frames
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])


def python_watchdog(stop: threading.Event, fd: int, interval: float) -> None:
    while not stop.wait(interval):
        emit_python_stacks(fd)


def churn_once() -> int:
    namespace: dict[str, object] = {}
    source = (
        "def outer():\n"
        "    def middle():\n"
        "        def inner():\n"
        "            return sum(range(17))\n"
        "        return inner()\n"
        "    return middle()\n"
    )
    exec(compile(source, "<ephemeral-frame-churn>", "exec"), namespace)
    value = namespace["outer"]()
    namespace.clear()
    return value


def child(mode: str, workload: str, seconds: float, path: Path) -> int:
    stop = threading.Event()
    watcher = None
    with path.open("wb", buffering=0) as diagnostic:
        faulthandler.enable(file=diagnostic)
        print("PROBE_READY", flush=True)
        interval = 0.0001 if workload == "churn" else 0.05
        if mode == "native":
            faulthandler.dump_traceback_later(interval, repeat=True, file=diagnostic)
        elif mode == "python":
            watcher = threading.Thread(
                target=python_watchdog,
                args=(stop, diagnostic.fileno(), interval),
                name="diagnostic-watchdog",
                daemon=True,
            )
            watcher.start()
        try:
            if workload == "lock":
                hold_lock_forever()
            elif workload == "quick":
                pass
            else:
                end = time.monotonic() + seconds
                count = 0
                while time.monotonic() < end:
                    assert churn_once() == 136
                    count += 1
                print(json.dumps({"iterations": count}), flush=True)
        finally:
            if mode == "native":
                faulthandler.cancel_dump_traceback_later()
            stop.set()
            if watcher is not None:
                watcher.join(timeout=2)
                if watcher.is_alive():
                    raise RuntimeError("watchdog thread failed to stop")
            faulthandler.disable()
    return 0


def read_ready(proc, first_line, ready) -> None:
    first_line.append(proc.stdout.readline())
    ready.set()


def parent(directory: Path, seconds: float) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    scenarios = [
        ("native", "churn"),
        ("python", "churn"),
        ("none", "churn"),
        ("python", "lock"),
        ("none", "lock"),
        ("python", "quick"),
    ]
    for mode, workload in scenarios:
        diagnostic = directory / f"{mode}-{workload}.stacks.log"
        command = [
            sys.executable,
            "-X",
            "dev",
            __file__,
            "--child",
            "--mode",
            mode,
            "--workload",
            workload,
            "--seconds",
            str(seconds),
            "--directory",
            str(diagnostic),
        ]
        env = dict(os.environ)
        env["PYTHONMALLOC"] = "debug"
        start = time.monotonic()
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        ready = threading.Event()
        first_line: list[bytes] = []

        reader = threading.Thread(
            target=read_ready,
            args=(proc, first_line, ready),
            name="probe-ready-reader",
            daemon=True,
        )
        reader.start()
        killed = False
        startup_ok = ready.wait(5)
        if not startup_ok:
            proc.kill()
            reader.join(timeout=3)
            out, err = proc.communicate(timeout=3)
            killed = True
        else:
            reader.join(timeout=1)
            try:
                out, err = proc.communicate(timeout=0.4 if workload == "lock" else seconds + 5)
            except subprocess.TimeoutExpired:
                killed = True
                proc.kill()
                out, err = proc.communicate(timeout=3)
        assert not reader.is_alive(), "readiness reader leaked"
        content = diagnostic.read_text(errors="replace") if diagnostic.exists() else ""
        result = {
            "mode": mode,
            "workload": workload,
            "returncode": proc.returncode,
            "external_deadline_killed": killed,
            "ready": startup_ok
            and len(first_line) == 1
            and first_line[0].rstrip(b"\r\n") == b"PROBE_READY",
            "outcome": "external_deadline"
            if killed
            else "completed"
            if proc.returncode == 0
            else "process_failure",
            "elapsed_s": round(time.monotonic() - start, 4),
            "stack_bytes": diagnostic.stat().st_size if diagnostic.exists() else 0,
            "reports_lock_frame": "in hold_lock_forever" in content,
            "native_crash_report": "Fatal Python error" in content
            or "Windows fatal exception" in content,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace")[:4000],
        }
        results.append(result)
        print(json.dumps(result), flush=True)
    by_key = {(r["mode"], r["workload"]): r for r in results}
    evidence = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "results": results,
        "native_path_ok": by_key["native", "churn"]["returncode"] == 0,
        "note": (
            "Diagnostic experiment, not project regression or Windows proof; "
            "native outcome is recorded even when the experiment completes"
        ),
    }
    (directory / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert all(result["ready"] for result in results)
    # A thread may fail after its first successful dump without changing child exit.
    assert all(not result["stderr"] for result in results if result["mode"] == "python")
    assert by_key["python", "churn"]["returncode"] == 0
    assert by_key["python", "churn"]["stack_bytes"] > 0
    assert by_key["none", "churn"]["returncode"] == 0
    assert by_key["python", "lock"]["external_deadline_killed"]
    assert by_key["python", "lock"]["reports_lock_frame"]
    assert by_key["none", "lock"]["external_deadline_killed"]
    assert not by_key["none", "lock"]["reports_lock_frame"]
    assert by_key["python", "quick"]["returncode"] == 0
    assert by_key["python", "quick"]["stack_bytes"] == 0
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--mode", choices=["native", "python", "none"])
    parser.add_argument("--workload", choices=["churn", "lock", "quick"])
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--directory", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.child:
        raise SystemExit(
            child(arguments.mode, arguments.workload, arguments.seconds, arguments.directory)
        )
    raise SystemExit(parent(arguments.directory, arguments.seconds))
