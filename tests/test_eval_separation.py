"""M4 的分离验收：`evaluate` 调模型，`replay` 不调。

PRD §6 红线 10 的原话是"评测的 replay 与 evaluate 不得混称"。这句话在代码里
可以变成两个可检查的事实：

1. `evaluate` 真的调了模型（否则它只是换皮 replay）；
2. 对同一份事件库再跑 `replay`，**一条新事件都不会产生**——模型一次都没被叫醒。

第 2 条用事件总数不变来证明，比"我相信 replay 里没有 model 调用"可靠。
"""

from __future__ import annotations

import json

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
    out = tmp_path / "baseline.json"
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
            "--out",
            str(out),
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

    # 落盘的是报告本体，可以直接当下一次的 --baseline
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["schema"] == "tianshu.eval.v1"
    assert saved["verdict"] == "pass"


def test_cli_compare_accepts_previous_report_as_baseline(tmp_path, capsys):
    workdir_a = tmp_path / "eval-a"
    baseline = tmp_path / "a.json"
    assert (
        main(
            [
                "evaluate",
                "--case",
                "N1",
                "--repeat",
                "3",
                "--workdir",
                str(workdir_a),
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
            "--case",
            "N1",
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