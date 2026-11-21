"""Exercise approvals, event ordering and form idempotency at the real executor boundary."""

import hashlib
import http.client
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu import net
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import verify_receipts
from tianshu.recovery import assess_run, recover_run
from tianshu.replay import replay
from tianshu.snapshot import SnapshotStore
from tianshu.tools import tool_manifest, validate_args

ORIGIN = "network://https/site.example"
PAGE = ORIGIN + "/form"
DEST = ORIGIN + "/submit"
HTML = b'<form method="post" action="/submit"><input name="message"></form>'


@pytest.mark.parametrize("mode", [
    "accepted", "uncertain", "truncated-204", "truncated-zero-length",
    "changed-source", "no-budget",
])
def test_scoped_form_submits_only_after_approval_and_never_repeats(tmp_path, monkeypatch, mode):
    received = []
    database = tmp_path / "events.db"
    blobs = tmp_path / "blobs"

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(HTML)))
            self.end_headers()
            self.wfile.write(HTML)

        def do_POST(self):
            with closing(sqlite3.connect(database)) as conn:
                state = conn.execute(
                    "SELECT state FROM dispatches WHERE call_id = 'submit'"
                ).fetchone()
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body, state))
            if mode == "uncertain":
                self.close_connection = True
                return
            if mode == "truncated-204":
                self.wfile.write(b"HTTP/1.1 204 No Content\r\n")
                self.close_connection = True
                return
            if mode == "truncated-zero-length":
                self.wfile.write(b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n")
                self.close_connection = True
                return
            self.send_response(201)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    args = {"url": DEST, "from_call": "page", "form_index": 0,
            "fields": [{"name": "message", "value": "中文 & action"}]}
    [manifest] = [entry for entry in tool_manifest() if entry["name"] == "browser.submit"]
    assert set(manifest["input_schema"]["required"]) == set(args)
    validate_args(args, manifest["input_schema"])

    def proposal(cid, tool, data):
        return ModelResponse("tool_use", tool_calls=[ToolCall(cid, tool, data)])

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(net, "_connect", lambda target, timeout: http.client.HTTPConnection(
            *server.server_address, timeout=timeout,
        ))
        try:
            with closing(EventLog(database, blob_dir=blobs)) as log:
                stack = stack_for(log, data_root=tmp_path)
                task = stack.tasks.create_task("填写测试表单", [], outbound_allow=[ORIGIN])
                limits = Limits(budget_limits={"bytes": net.NetLimits().max_bytes})
                engine = engine_for(stack, ScriptedModel([
                    proposal("page", "browser.navigate", {"url": PAGE}),
                    proposal("cross", "browser.submit", {**args, "url": ORIGIN + "/other"}),
                    proposal("wrong-field", "browser.submit", {
                        **args, "fields": [{"name": "extra", "value": "unexpected"}],
                    }),
                    proposal("submit", "browser.submit", args),
                    proposal("new-call", "browser.submit", args),
                    ModelResponse("end_turn", "完成"),
                ]), limits=limits if mode == "no-budget" else None)
                run = engine.start(task.task_id)
                assert run.state == "waiting_approval"
                assert approve_pending(stack, run.run_id)
                run = engine.resume_run(run.run_id)
                if mode == "no-budget":
                    assert run.terminal and not received
                    outputs = {ev.payload["call_id"]: ev.payload for ev in
                               log.read_run(run.run_id, event_types=("tool.result",))}
                    assert outputs["page"]["status"] == "succeeded"
                    assert outputs["submit"]["status"] == "rejected"
                    assert outputs["submit"]["output"]["rule_id"] == "dispatch.budget_refused"
                    assert stack.budget.usage(f"run:{run.run_id}")["settled"]["bytes"] == len(HTML)
                    return
                assert run.state == "waiting_approval" and not received
                [approval] = stack.approvals.pending(run_id=run.run_id)
                assert "POST" in approval.reason and "中文 & action" in approval.reason
                assert list(approval.resources) == [DEST]
                if mode == "changed-source":
                    source = stack.contents.blob_dir / hashlib.sha256(HTML).hexdigest()
                    assert source.read_bytes() == HTML
                    original_verify = stack.dispatcher._verify_resource_execution

                    def change_page_before_verify(ticket):
                        assert ticket.request.tool_id == "browser.submit"
                        source.write_bytes(b"not the approved page")
                        return original_verify(ticket)

                    monkeypatch.setattr(
                        stack.dispatcher, "_verify_resource_execution", change_page_before_verify,
                    )
                assert approve_pending(stack, run.run_id)
                finished = engine.resume_run(run.run_id)
                events = log.read_run(run.run_id)
                outputs = {event.payload["call_id"]: event.payload for event in events
                           if event.type == "tool.result"}
                assert outputs["cross"]["status"] == "rejected"
                assert outputs["wrong-field"]["status"] == "rejected"
                if mode == "changed-source":
                    assert finished.terminal
                    assert outputs["submit"]["status"] == "rejected" and received == []
                else:
                    assert finished.terminal
                    assert outputs["new-call"]["status"] == "rejected"
                    assert received == [(
                        "/submit", b"message=%E4%B8%AD%E6%96%87+%26+action", ("executing",),
                    )]
                    effect = "applied" if mode == "accepted" else "unknown"
                    receipts = [event.payload for event in events if event.type == "receipt"]
                    assert (outputs["submit"]["effect_state"], len(receipts)) == (
                        effect, 1 if mode == "accepted" else 0,
                    )
                    assert verify_receipts(log, run.run_id) == []
                    assert replay(log, run.run_id).ok
                    assert len(received) == 1
                    if mode == "accepted":
                        blob = stack.contents.blob_dir / hashlib.sha256(b"ok").hexdigest()
                        blob.write_bytes(b"no")
                        assert any("正文摘要" in p for p in verify_receipts(log, run.run_id))
                        another = stack.tasks.create_task(
                            "不能使用另一个运行的网页", [], outbound_allow=[ORIGIN],
                        )
                        reject_other_run = engine_for(stack, ScriptedModel([
                            proposal("foreign", "browser.submit", args),
                            ModelResponse("end_turn", "本次运行没有来源表单"),
                        ])).start(another.task_id)
                        assert reject_other_run.terminal
                        [foreign] = log.read_run(
                            reject_other_run.run_id, event_types=("tool.result",),
                        )
                        assert foreign.payload["status"] == "rejected" and len(received) == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)


_CRASH_CHILD = """
import http.client
import os
import sys
from pathlib import Path

from support import approve_pending, engine_for, stack_for
from tianshu import net
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall

root = Path(sys.argv[1])
origin = 'network://https/site.example'
net._connect = lambda _t, timeout: http.client.HTTPConnection(
    '127.0.0.1', int(sys.argv[2]), timeout=timeout)
log = EventLog(root / 'events.db', data_root=root)
stack = stack_for(log, data_root=root)
task = stack.tasks.create_task('form crash', [], outbound_allow=[origin])
engine = engine_for(stack, ScriptedModel([
    ModelResponse('tool_use', tool_calls=[
        ToolCall('page', 'browser.navigate', {'url': origin + '/form'}),
    ]),
    ModelResponse('tool_use', tool_calls=[ToolCall('submit', 'browser.submit', {
        'url': origin + '/submit', 'from_call': 'page', 'form_index': 0,
        'fields': [{'name': 'message', 'value': 'crash fixture'}],
    })]),
]))
run = engine.start(task.task_id)
assert run.state == 'waiting_approval' and approve_pending(stack, run.run_id)
run = engine.resume_run(run.run_id)
assert run.state == 'waiting_approval' and approve_pending(stack, run.run_id)
(root / 'run-id').write_text(run.run_id, encoding='ascii')
os.environ['TIANSHU_FAILPOINT'] = sys.argv[3]
engine.resume_run(run.run_id)
raise AssertionError('form crashpoint did not run')
"""


@pytest.mark.parametrize("point", [
    "after_intent", "browser_form.before_send", "browser_form.after_ack",
])
@pytest.mark.parametrize("corrupt", [False, True])
def test_form_recovery_uses_send_boundary_instead_of_file_snapshot(tmp_path, point, corrupt):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply(200, HTML)

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self._reply(201, b"ok")

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [
                str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH"),
            ])), TIANSHU_NET_MIN_INTERVAL_S="0")
            env.pop("TIANSHU_FAILPOINT", None)
            child = subprocess.run(
                [sys.executable, "-c", _CRASH_CHILD, str(tmp_path), str(server.server_port), point],
                env=env, capture_output=True, timeout=30,
            )
            assert child.returncode == 77, child.stderr.decode("utf-8", errors="replace")
            run_id = (tmp_path / "run-id").read_text(encoding="ascii")
            snapshots = SnapshotStore(tmp_path / "artifacts")
            if corrupt:
                (snapshots.manifest_dir / f"{run_id}.json").write_bytes(b"not JSON")
            boundary_crossed = point != "after_intent"
            expected = [b"message=crash+fixture"] if point == "browser_form.after_ack" else []
            assert received == expected
            with closing(EventLog(tmp_path / "events.db", data_root=tmp_path)) as log:
                assessment = assess_run(tmp_path, run_id, log=log)
                assert assessment.blocks_takeover is boundary_crossed
                report = recover_run(log, run_id, tmp_path, snapshots, data_root=tmp_path)
                assert report.unresolved == (["submit"] if boundary_crossed else [])
                assert report.none_applied == ([] if boundary_crossed else ["submit"])
                events = log.read_run(run_id)
                [result] = [e.payload for e in events if e.type == "tool.result"
                            and e.payload["call_id"] == "submit"]
                assert result["effect_state"] == ("unknown" if boundary_crossed else "none_applied")
                assert not [e for e in events if e.type in {"receipt", "recovery.blocked"}]
                head = log.seq_head()
                recover_run(log, run_id, tmp_path, snapshots, data_root=tmp_path)
                assert log.seq_head() == head
                assert received == expected
        finally:
            server.shutdown()
            thread.join(timeout=5)
