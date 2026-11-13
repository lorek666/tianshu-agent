#!/usr/bin/env python3
"""Collect actual Windows deny-sharing outcomes without synthesizing OS exceptions."""
import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path


def worker(args):
    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tianshu import host

    root = Path(args.root)
    try:
        if args.child == "read":
            value = host.read_host_info(root)
            returned = "None" if value is None else "HostInfo"
        elif args.child == "clear":
            returned = host.clear_host_info(root, pid=424242)
        else:
            returned = host.stop_host(root, timeout=0.5)
        result = {"error": None, "returned": returned}
    except BaseException as exc:
        cause = exc.__cause__
        result = {
            "error": type(exc).__name__, "message": str(exc),
            "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None),
            "cause_type": None if cause is None else type(cause).__name__,
            "cause_errno": getattr(cause, "errno", None),
            "cause_winerror": getattr(cause, "winerror", None),
        }
    result["path_exists"] = host.host_lock_path(root).exists()
    print(json.dumps(result), flush=True)


def run(args):
    if os.name != "nt":
        raise RuntimeError("This native probe must run on actual Windows")
    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tianshu import host

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    result = {
        "python": sys.version, "platform": platform.platform(),
        "repo": str(Path(args.repo).resolve()),
        "host_sha256": hashlib.sha256(
            (Path(args.repo) / "src/tianshu/host.py").read_bytes()
        ).hexdigest(),
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="verify-host-exclusive-") as temp:
        root = Path(temp)
        original_info = host.HostInfo(pid=424242, port=0, token="probe-only", started_at=1)
        host.write_host_info(root, original_info)
        path = host.host_lock_path(root)
        original = path.read_bytes()
        # GENERIC_READ, share=0, OPEN_EXISTING: a real deny-sharing native handle.
        handle = api.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for mode in ("read", "clear", "stop"):
                proc = subprocess.Popen(
                    [sys.executable, __file__, "--repo", args.repo, "--root",
                     str(root), "--child", mode],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                try:
                    out, err = proc.communicate(timeout=15)
                except BaseException:
                    proc.kill()
                    proc.communicate(timeout=5)
                    raise
                assert proc.returncode == 0, (proc.returncode, out, err)
                result["cases"].append({
                    "operation": mode, "native_share_mode": 0,
                    "observed": json.loads(out.splitlines()[-1]),
                    "stdout": out, "stderr": err,
                })
        finally:
            if not api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        result["original_bytes_preserved"] = path.read_bytes() == original
        restored = host.read_host_info(root)
        result["read_after_release_matches_original"] = restored == original_info
        result["read_after_release"] = (
            None if restored is None else {"pid": restored.pid, "started_at": restored.started_at}
        )
    target = Path(args.output)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(target), "result": result}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root")
    parser.add_argument("--child")
    parser.add_argument("--output", default="host-exclusive-read.json")
    arguments = parser.parse_args()
    if arguments.child:
        worker(arguments)
    else:
        run(arguments)
