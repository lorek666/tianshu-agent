"""M4 的分离验收：`evaluate` 调模型，`replay` 不调。

PRD §6 红线 10 的原话是"评测的 replay 与 evaluate 不得混称"。这句话在代码里
可以变成两个可检查的事实：

1. `evaluate` 真的调了模型（否则它只是换皮 replay）；
2. 对同一份事件库再跑 `replay`，**一条新事件都不会产生**——模型一次都没被叫醒。

第 2 条用事件总数不变来证明，比"我相信 replay 里没有 model 调用"可靠。
"""

from __future__ import annotations

import json
from pathlib import Path

from tianshu.cli import main
from tianshu.events import EventLog
from tianshu.replay import replay


def _event_counts(log: EventLog) -> tuple[int, int]:
    total = log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    model = log.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type IN ('model.intent','model.result')"
    ).fetchone()[0]
    return int(total), int(model)


def _latest_run_id(log: EventLog) -> str:
    row = log.conn.execute(
        "SELECT run_id FROM events WHERE type='run.start' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return row["run_id"]


def test_evaluate_calls_model_and_replay_does_not(tmp_path, capsys):
    workdir = tmp_path / "eval"
    code = main(
        [
            "evaluate",
            "--case",
            "N1",
            "--repeat",
            "3",
            "--workdir",
            str(workdir),
            "--keep",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    report = payload["report"]
    assert report["verdict"] == "pass"
    assert report["overall"]["runs"] == 3, "evaluate 必须逐次跑，不是跑一次乘三"
    assert payload["compare"] is None

    # evaluate 真的调用了模型：每次 Run 都有 model.intent
    db = workdir / "N1" / "run0" / "events.db"
    log = EventLog(db, blob_dir=db.parent / "blobs")
    try:
        run_id = _latest_run_id(log)
        total_before, model_before = _event_counts(log)
        assert model_before >= 1, "evaluate 没有调用模型，那它只是换皮 replay"
        replay_report = replay(log, run_id)
        total_after, model_after = _event_counts(log)
    finally:
        log.close()

    assert replay_report.ok, replay_report.problems
    assert total_after == total_before, "replay 往事件库里写了新事件：它不该调模型"
    assert model_after == model_before, "replay 调用了模型：审计与评测混了"

    # 更强的静态证据：replay 的 API 里根本没有模型入口，也不 import 模型模块。
    # （不能只搜源码里的 "model" 字样：docstring 里就写着 model.intent。）
    import ast as _ast
    import inspect as _inspect

    import tianshu.replay as replay_mod

    assert list(_inspect.signature(replay_mod.replay).parameters) == ["log", "run_id"]
    source = _inspect.getsource(replay_mod)
    imported: set[str] = set()
    for node in _ast.walk(_ast.parse(source)):
        if isinstance(node, _ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, _ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(module.endswith("model") for module in imported), imported


def test_cli_compare_accepts_previous_report_as_baseline(tmp_path, capsys):
    # 完整 20 条才有资格当"可比报告"；子集只用于冒烟（见下一条测试）。
    baseline = tmp_path / "a.json"
    assert (
        main(
            [
                "evaluate",
                "--repeat",
                "3",
                "--workdir",
                str(tmp_path / "eval-a"),
                "--out",
                str(baseline),
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "evaluate",
            "--repeat",
            "3",
            "--workdir",
            str(tmp_path / "eval-b"),
            "--baseline",
            str(baseline),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    comparison = json.loads(captured.out)["compare"]
    assert comparison["comparable"] is True
    assert comparison["summary"]["delta"] == 0.0
    saved = json.loads(baseline.read_text(encoding="utf-8"))
    assert saved["schema"] == "tianshu.eval.v1"
    assert saved["selection"]["complete"] is True


def test_cli_refuses_repeat_below_three(tmp_path, capsys):
    code = main(
        [
            "evaluate",
            "--case",
            "N1",
            "--repeat",
            "1",
            "--workdir",
            str(tmp_path / "eval"),
        ]
    )
    assert code == 2
    assert "≥3" in capsys.readouterr().err


def test_cli_refuses_dirty_workdir(tmp_path, capsys):
    workdir = tmp_path / "eval"
    workdir.mkdir()
    (workdir / "junk.txt").write_text("残留\n", encoding="utf-8")
    code = main(
        [
            "evaluate",
            "--case",
            "N1",
            "--repeat",
            "3",
            "--workdir",
            str(workdir),
        ]
    )
    assert code == 2
    assert "干净" in capsys.readouterr().err


def test_cli_refuses_unknown_case(tmp_path, capsys):
    code = main(
        [
            "evaluate",
            "--case",
            "ZZZ",
            "--repeat",
            "3",
            "--workdir",
            str(tmp_path / "eval"),
        ]
    )
    assert code == 2
    assert "未知的 case id" in capsys.readouterr().err

def test_cli_refuses_real_model_on_harness_only_selection(tmp_path, capsys):
    """崩溃 case 恒用离线脚本：拿它们给真实模型记分是假证据，必须在开跑前拒绝。"""
    code = main(
        [
            "evaluate",
            "--case",
            "R1",
            "--repeat",
            "3",
            "--model",
            "openai-compatible",
            "--model-name",
            "some-model",
            "--workdir",
            str(tmp_path / "eval"),
        ]
    )
    assert code == 2
    assert "harness" in capsys.readouterr().err


def test_cli_maps_unsafe_verdict_to_exit_code_four(tmp_path, capsys, monkeypatch):
    """安全门槛失败必须是独立的退出码（4），不能被读成普通失败（1）。"""
    from tianshu.eval import SUITE_VERSION
    from tianshu.eval.runner import SuiteRun

    fake_report = {
        "schema": "tianshu.eval.v1",
        "suite_version": SUITE_VERSION,
        "generated_at": "2026-11-09T00:00:00Z",
        "model": {"adapter": "scripted", "vendor": None, "model": ""},
        "repeat": 3,
        "elapsed_ms": 0,
        "workspace_retained": False,
        "safety_gate": {
            "passed": False,
            "rule": "x",
            "violations": [
                {
                    "case_id": "N1",
                    "run_index": 0,
                    "check_id": "policy.no_secret_in_trace",
                    "detail": "泄漏",
                }
            ],
        },
        "overall": {
            "cases": 1,
            "runs": 3,
            "run_passes": 3,
            "run_pass_rate": 1.0,
            "case_pass_rate": 1.0,
            "cases_all_green": 1,
        },
        "scope": {
            "model": {"cases": 1, "case_pass_rate": 1.0, "cases_all_green": 1},
            "harness": {"cases": 0, "case_pass_rate": 0.0, "cases_all_green": 0},
        },
        "categories": {
            "normal": {
                "cases": 1,
                "runs": 3,
                "passes": 3,
                "pass_rate": 1.0,
                "run_pass_rate": 1.0,
            }
        },
        "attribution": {},
        "verdict": "unsafe",
        "cases": [
            {
                "case_id": "N1",
                "category": "normal",
                "pass_rate": 1.0,
                "run_details": [{"run_index": 0, "passed": True, "failed_checks": []}],
            }
        ],
    }
    empty_suite = SuiteRun(
        observations=[],
        workdir=Path("."),
        elapsed_ms=0,
        identity={"adapter": "scripted", "vendor": None, "model": ""},
        suite_version=SUITE_VERSION,
        repeat=3,
    )
    monkeypatch.setattr("tianshu.eval.run_suite", lambda *a, **k: empty_suite)
    monkeypatch.setattr("tianshu.eval.build_report", lambda *a, **k: fake_report)

    code = main(
        [
            "evaluate",
            "--case",
            "N1",
            "--repeat",
            "3",
            "--workdir",
            str(tmp_path / "eval"),
        ]
    )
    assert code == 4


def test_cli_refuses_subset_report_as_formal_artifact(tmp_path, capsys):
    """子集报告的安全门槛只覆盖选中的几条：不能写出去当"可比分数"。"""
    for extra in (["--out", str(tmp_path / "x.json")], ["--baseline", "whatever.json"]):
        code = main(
            [
                "evaluate",
                "--case",
                "N1",
                "--repeat",
                "3",
                "--workdir",
                str(tmp_path / f"eval-{len(extra)}"),
                *extra,
            ]
        )
        assert code == 2
        assert "子集" in capsys.readouterr().err
