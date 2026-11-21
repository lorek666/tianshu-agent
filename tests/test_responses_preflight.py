"""设计预审确认的真实调用身份、热切换与恢复计费负控。"""

import os
import subprocess
import sys
from contextlib import closing

import pytest
from support import engine_for, stack_for

from tianshu.credentials import EnvCredentialStore
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel, UrllibTransport
from tianshu.pricing import cost_record
from tianshu.settings_control import RuntimeModelSlot

_CRASH = """
import os
from pathlib import Path
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.crashpoint import failpoint
root = Path(os.environ["RESPONSES_CASE"])
log = EventLog(root / "events.db", blob_dir=root / "blobs")
stack = build_stack(log, data_root=root)
task = stack.tasks.create_task("hello", [])
def crash(*args, **kwargs):
    failpoint("review-model-result-before-commit")
stack.tasks.commit_model_turn = crash
AgentLoop(tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
    dispatcher=stack.dispatcher, approvals=stack.approvals, snapshots=stack.snapshots,
    contents=stack.contents, budget=stack.budget, memory=stack.memory,
    summaries=stack.summaries, limits=Limits(budget_limits={"requests": int(
        os.environ["RESPONSES_REQUEST_LIMIT"])}),
    model=ScriptedModel([ModelResponse("end_turn", usage={
        "input_tokens": 1, "output_tokens": 1})])).start(task.task_id, out_name="")
"""


@pytest.mark.parametrize("limit", [1, 2])
def test_retry_after_crash_uses_new_attempt_and_cannot_reuse_spent_request(tmp_path, limit):
    env = {
        **os.environ,
        "RESPONSES_CASE": str(tmp_path),
        "RESPONSES_REQUEST_LIMIT": str(limit),
        "TIANSHU_FAILPOINT": "review-model-result-before-commit",
    }
    child = subprocess.run(
        [sys.executable, "-c", _CRASH], env=env, capture_output=True, timeout=45
    )
    assert child.returncode == 77, child.stderr.decode("utf-8", "replace")
    assert b"[failpoint] review-model-result-before-commit" in child.stderr
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        assert not [e for e in log.read_run(run_id) if e.type == "model.result"]
        model = ScriptedModel([
            ModelResponse("end_turn", usage={"input_tokens": 1, "output_tokens": 1})
        ])
        result = engine_for(
            stack, model, limits=Limits(budget_limits={"requests": limit})
        ).resume_run(run_id)
        attempts = [
            e.payload["attempt"] for e in log.read_run(run_id) if e.type == "model.intent"
        ]
        assert attempts == ([1] if limit == 1 else [1, 2])
        assert model.calls == limit - 1
        assert result.outcome == ("failed" if limit == 1 else "noop")
        rows = log.conn.execute(
            "SELECT attempt_id FROM budget_reservations WHERE run_id=?", (run_id,)
        ).fetchall()
        assert len(rows) == len({r["attempt_id"] for r in rows}) == limit
        settled = log.conn.execute(
            "SELECT settled FROM budget_counters WHERE account_id=? AND dimension='requests'",
            (f"run:{run_id}",),
        ).fetchone()[0]
        assert settled == limit


@pytest.mark.parametrize("mode", ["openai", "openai-responses"])
def test_actual_model_step_freezes_request_and_cost_identity_during_hot_switch(
    tmp_path, monkeypatch, mode
):
    slot = RuntimeModelSlot(
        None, stores={"env": EnvCredentialStore({"TIANSHU_API_KEY": "test-only"})}
    )
    cfg = {"mode": mode, "model_name": "gpt-4o-mini", "credential_backend": "env"}
    slot.configure(cfg)
    sent = []

    def post(self, url, *, body, **kwargs):
        sent.append(body["model"])
        # 请求已发出、结果尚未返回时发生配置变更：下一步生效，本步账本身份应固定。
        slot.configure({**cfg, "model_name": "gpt-4o"})
        usage = {"input_tokens": 10, "output_tokens": 10}
        if mode == "openai":
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                "usage": usage,
            }
        return {
            "id": "resp_1", "status": "completed", "usage": usage, "output": [{
                "type": "message", "id": "msg_1", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": "done", "annotations": [],
                }],
            }],
        }

    monkeypatch.setattr(UrllibTransport, "post", post)
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("hello", [])
        result = engine_for(stack, slot).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        assert sent == ["gpt-4o-mini"]
        saved = next(e.payload["cost"] for e in log.read_run(result.run_id)
                     if e.type == "model.result")
        assert saved["model"] == "gpt-4o-mini"
        expected = cost_record(
            provider="openai", model="gpt-4o-mini",
            usage={"input_tokens": 10, "output_tokens": 10},
            adapter="openai-compatible" if mode == "openai" else mode,
        )
        assert saved == expected.to_payload()
        settled = log.conn.execute(
            "SELECT settled FROM budget_counters WHERE account_id=? AND dimension='micros'",
            (f"run:{result.run_id}",),
        ).fetchone()[0]
        assert settled == expected.cost
