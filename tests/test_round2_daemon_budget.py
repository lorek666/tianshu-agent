"""审批与重启后的显式续跑也必须走同一份持久预算账本。"""

from __future__ import annotations

import shutil

import pytest
from test_daemon import (
    Harness,
    SlowModel,
    _connect,
    _prepare_overwrite_run,
    _short_data_root,
    _wait_run_state,
    _wait_terminal,
)

from tianshu.budget import BudgetLedger
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ToolCall


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("request_limit", [1, None])
def test_approval_resume_keeps_budget_accounting(tmp_path, restart, request_limit):
    root, inside_tmp = _short_data_root(tmp_path)
    root.mkdir(exist_ok=True)
    workspace = root / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    note.write_text("before", encoding="utf-8")
    model = SlowModel([
        ModelResponse(
            "tool_use",
            tool_calls=[ToolCall("c1", "modify_text", {"path": "note.md", "content": "after"})],
            usage={"input_tokens": 10, "output_tokens": 10},
        ),
        ModelResponse("end_turn", content="done", usage={"input_tokens": 10, "output_tokens": 10}),
    ])
    limits = Limits(8, 20_000, budget_limits=(
        None if request_limit is None else {"requests": request_limit}
    ))
    harness = Harness(root, model, limits=limits).start()
    client = None
    try:
        client = _connect(root)
        _task, run = _prepare_overwrite_run(client, workspace)
        run_id = run["run_id"]
        _wait_run_state(client, run_id, {"waiting_approval"})
        approval = client.call("approvals.list", {"run_id": run_id})["approvals"][0]
        if restart:
            client.close()
            client = None
            assert harness.stop_and_wait()
            assert not harness.errors
            harness = Harness(root, model, limits=limits).start()
            client = _connect(root)
            assert client.call("run.status", {"run_id": run_id})["run"]["state"] == (
                "waiting_approval"
            )
        client.call("approvals.decide", {
            "approval_id": approval["approval_id"], "decision": "approve",
        })
        final = _wait_terminal(client, run_id)
        assert note.read_text(encoding="utf-8") == "after"
        assert final["effect_state"] == "applied"
        expected_calls = 1 if request_limit == 1 else 2
        assert model.calls == expected_calls
        assert final["state"] == ("failed" if request_limit == 1 else "succeeded")
        client.close()
        client = None
        assert harness.stop_and_wait()
        assert not harness.errors
        log = EventLog(root / "events.db")
        try:
            usage = BudgetLedger(log).usage(f"run:{run_id}")
            assert usage["settled"]["requests"] == expected_calls
            assert usage["settled"]["tokens"] == expected_calls * 20
            events = log.read_run(run_id)
            assert sum(event.type == "model.intent" for event in events) == expected_calls
            assert sum(event.type == "receipt" for event in events) == 1
        finally:
            log.close()
    finally:
        if client is not None:
            client.close()
        assert harness.stop_and_wait()
        assert not harness.errors
        if not inside_tmp:
            shutil.rmtree(root)
