"""Trace serial approval RPCs without writing request or response content."""

import json
import os
import runpy
import sys
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import local
from time import perf_counter_ns

TRACE = "TIANSHU_PRIVATE_APPROVAL_TIMING_OUT"
APPROVAL = "approvals.decide"
state = local()


def emit(row):
    file = Path(os.environ[TRACE]) / f"{row['role']}-{os.getpid()}.ndjson"
    with file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")


@contextmanager
def record(role, name, **fields):
    outer = getattr(state, "row", None)
    assert outer is None
    row = {"role": role, name: [perf_counter_ns(), None], **fields}
    state.row = row
    try:
        yield
    finally:
        row[name][1] = perf_counter_ns()
        state.row = None
        emit(row)


def span(name, fn, *args, **kwargs):
    row = getattr(state, "row", None)
    if row is None:
        return fn(*args, **kwargs)
    t = perf_counter_ns()
    try:
        return fn(*args, **kwargs)
    finally:
        row.setdefault(name, []).append([t, perf_counter_ns()])


def patch(cls, name, label):
    original = getattr(cls, name)

    @wraps(original)
    def traced(*args, **kwargs):
        return span(label, original, *args, **kwargs)

    setattr(cls, name, traced)


def install_host():
    from tianshu import host, ipc

    saved = host._Handler._proxy

    @wraps(saved)
    def proxy(self, method, params):
        if method != APPROVAL:
            return saved(self, method, params)
        with record("host", "proxy"):
            return saved(self, method, params)

    host._Handler._proxy = proxy
    for cls, method, label in (
        (ipc.Client, "connect", "ipc_connect"),
        (ipc.Client, "call", "ipc_call"),
        (host._Handler, "_json", "http_reply"),
    ):
        patch(cls, method, label)


def install_daemon():
    from tianshu import daemon, ipc
    from tianshu.control import ControlPlane
    from tianshu.permissions import ApprovalStore

    received = {}
    recv_orig = ipc.ServerConnection.recv
    handle_orig = daemon._Daemon._handle

    @wraps(recv_orig)
    def recv(self, *args, **kwargs):
        message = recv_orig(self, *args, **kwargs)
        if isinstance(message, dict) and message.get("method") == APPROVAL:
            received[id(message)] = perf_counter_ns()
        return message

    @wraps(handle_orig)
    def handle(self, conn, message):
        if message.get("method") != APPROVAL:
            return handle_orig(self, conn, message)
        received_ns = received.pop(id(message))
        with record("daemon", "handle", received_ns=received_ns):
            return handle_orig(self, conn, message)

    ipc.ServerConnection.recv = recv
    daemon._Daemon._handle = handle
    for cls, method, label in (
        (ApprovalStore, "decide", "db_decision"),
        (ControlPlane, "_approvals_decide", "control"),
        (ControlPlane, "_enqueue_resume", "queue_resume"),
        (ControlPlane, "_approval_context", "read_context"),
    ):
        patch(cls, method, label)


def install_measurement(script):
    # After the CLI defines _http_request, wrap it before main first measures.
    # Remove tracing before the measured workload to avoid profiling call overhead.
    assert sys.gettrace() is None

    def on_entry(frame, event, _arg):
        if (
            event == "call"
            and frame.f_code.co_name == "main"
            and Path(frame.f_code.co_filename).resolve() == script
        ):
            sys.settrace(None)
            saved = frame.f_globals["_http_request"]

            @wraps(saved)
            def http(port, method, path, token, body=None, **kwargs):
                if isinstance(body, dict) and body.get("method") == APPROVAL:
                    with record("client", "http"):
                        return saved(port, method, path, token, body, **kwargs)
                return saved(port, method, path, token, body, **kwargs)

            frame.f_globals["_http_request"] = http
        return None

    sys.settrace(on_entry)


if os.environ.get(TRACE):
    isolation_hook = os.environ.get("TIANSHU_PRIVATE_APPROVAL_ISOLATION_HOOK")
    if isolation_hook:
        runpy.run_path(isolation_hook)
    script = Path(sys.argv[0]).resolve()
    if script.name == "measure_slo.py":
        if len(sys.argv) >= 3 and sys.argv[1] == "--_child-role":
            if sys.argv[2] == "slo-host":
                install_host()
            elif sys.argv[2] == "slo-daemon":
                install_daemon()
        elif isolation_hook:
            install_measurement(script)
