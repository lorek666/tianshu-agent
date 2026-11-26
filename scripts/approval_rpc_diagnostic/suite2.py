"""Apply local fact-finding probes to unchanged suite 2, not an acceptance run."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import measure_slo as s
from sitecustomize import APPROVAL, TRACE, record

out = Path(sys.argv[1]).resolve()
out.mkdir()
co = s.REPO_ROOT
expected_head = "281dd22261105d32910ae5838a7873f0a1c693f1"
expected_tree = "148030d683c961f423f11846dd776b2894c1a550"
assert subprocess.check_output(
    ["git", "show", "-s", "--format=%H %T"], cwd=co, text=True
).split() == [expected_head, expected_tree]
assert not subprocess.check_output(["git", "status", "--porcelain=v1", "-uno"], cwd=co)
assert os.environ.get("TIANSHU_CREDENTIAL_BACKEND") == "env"
os.environ[TRACE] = str(out)
saved = s._http_request


def timed(port, method, path, token, body=None, **kwargs):
    if not isinstance(body, dict) or body.get("method") != APPROVAL:
        return saved(port, method, path, token, body, **kwargs)
    with record("client", "http"):
        return saved(port, method, path, token, body, **kwargs)


s._http_request = timed
repeats = 3
thresholds, _, _ = s.load_and_validate_thresholds()
base = s._short_temp_dir("tsapproval-")
authority = base / "auth"
locks = base / "locks"
authority.mkdir()
locks.mkdir()
pids = s.TrackedPids()
try:
    _, _, samples, backpressure, inflight = s.run_suite2_cancel_and_approval(
        base,
        repeats=repeats,
        event_bytes_min=int(thresholds["dataset_profile"]["slow_subscriber_event_bytes_min"]),
        authority_root=authority,
        lock_root=locks,
        tracked_pids=pids,
    )
    orphans = s._find_orphan_pids(pids, base)
finally:
    import shutil

    shutil.rmtree(base)


def rows(role):
    (file,) = out.glob(role + "-*.ndjson")
    return [json.loads(line) for line in file.read_text().splitlines()]


def ms(delta):
    return round(delta / 1e6, 3)


client, host, daemon = map(rows, ("client", "host", "daemon"))
assert len(client) == len(host) == len(daemon) == len(samples) == 3
assert not orphans and backpressure["cursor_reconnect_verified"]
result = []
for i, (c, h, d, wall) in enumerate(zip(client, host, daemon, samples, strict=True)):
    c0, c1 = c["http"]
    h0, h1 = h["proxy"]
    d0, d1 = d["handle"]
    q0 = d["received_ns"]
    ((connect0, connect1),) = h["ipc_connect"]
    hello, (call0, call1) = h["ipc_call"]
    ((http0, http1),) = h["http_reply"]
    ((control0, control1),) = d["control"]
    ((write0, write1),) = d["db_decision"]
    ((read0, read1),) = d["read_context"]
    ((resume0, resume1),) = d["queue_resume"]
    assert (
        c0
        <= h0
        <= connect0
        <= hello[0]
        <= hello[1]
        <= connect1
        <= call0
        <= q0
        <= d0
        <= control0
        <= write0
        <= write1
        <= resume0
        <= resume1
        <= read0
        <= read1
        <= control1
        <= call1
        <= http0
        <= http1
        <= h1
        <= c1
    )
    assert d1 >= control1 and wall + 0.001 >= ms(c1 - c0)
    result.append(
        dict(
            index=i,
            label=["st0", "ov0", "ov1"][i],
            measured_ms=round(wall, 3),
            http_ms=ms(c1 - c0),
            before_proxy_ms=ms(h0 - c0),
            ipc_connect_ms=ms(connect1 - connect0),
            ipc_hello_ms=ms(hello[1] - hello[0]),
            after_client_send_start_until_daemon_receive_ms=ms(q0 - call0),
            daemon_queue_ms=ms(d0 - q0),
            daemon_handle_ms=ms(d1 - d0),
            decision_ms=ms(write1 - write0),
            resume_enqueue_ms=ms(resume1 - resume0),
            read_context_ms=ms(read1 - read0),
            ipc_after_control_ms=ms(call1 - control1),
            http_reply_ms=ms(http1 - http0),
            after_proxy_ms=ms(c1 - h1),
        )
    )
report = dict(
    schema="private-approval-rpc-timing-v1",
    head=expected_head,
    tree=expected_tree,
    repeats=repeats,
    platform=sys.platform,
    python=sys.version,
    notes="Suite 2 only, timing wrappers enabled. Not a CI gate or baseline performance result.",
    rows=result,
    orphans=orphans,
    backpressure=backpressure,
    inflight=inflight,
    artifacts={
        f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.glob("*.ndjson"))
    },
)
(out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2), flush=True)
