"""工作流 CLI 复用真实控制面；模型仅替换在线传输。"""

import json

import pytest

from tianshu import cli
from tianshu.model import ModelResponse, ScriptedModel


@pytest.mark.parametrize("action", ["plan", "compact", "execute-plan"])
def test_parser_exposes_workflow_actions(action):
    args = cli._build_parser().parse_args(["run", action, "--task-id", "t", "--run-id", "r"])
    assert args.action == action


def test_cli_plan_compact_and_confirm(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    base = ["--db", str(db), "--data-root", str(tmp_path), "--json"]
    monkeypatch.setattr(cli, "_model_from", lambda args: ScriptedModel([
        ModelResponse("end_turn", content="计划：先分析，再修改，最后验证。"),
    ]))
    assert cli.main(["task", "create", "--goal", "整理笔记", *base]) == 0
    task = json.loads(capsys.readouterr().out)["task"]["task_id"]
    assert cli.main(["run", "plan", "--task-id", task, *base]) == 0
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["run"]["run_id"]
    assert cli.main(["run", "execute-plan", "--run-id", run_id, "--goal-revision", "1", *base]) == 0
    capsys.readouterr()
    assert cli.main(["run", "compact", "--task-id", task, *base]) == 0
    capsys.readouterr()
