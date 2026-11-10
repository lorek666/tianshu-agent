"""费用行的口径：按**尝试次数**说话，不按完成轮次（issue #58）。

现象：黑洞端点（accept 后不回包）下模型调用按 `max_model_attempts=3` 重试满
3 次后失败，CLI 却打印「费用 : 无（没有模型调用）」——把"打了 3 次都没回"
读成了"什么都没发生"。

根因：`cli._print_result` 把 `RunResult.turns`（**完成的轮次**）传给
`format_cost(..., calls=)`，而 `format_cost` 在 `calls <= 0` 时固定说"没有模型
调用"。`turns == 0` 恰好是"全部尝试失败"的形状，于是最需要如实报告的情况
被印成了最不关己的一句话。

修法：`RunResult.model_attempts` 由主循环在**真正发出请求的那一刻**累加
（含超时/传输失败/重试），进游标的预算账本（resume 不丢），CLI 按它分四种
情况说话（`_cost_note`）：

- `attempts == 0` → 「无（没有模型调用）」——只有真的没发过才允许；
- `attempts is None`（旧游标没有这个键）→ 不知道就不编数；
- `turns == 0 && attempts > 0` → 「0 个完成轮次 / N 次尝试（费用未知）」；
- `turns > 0` → 已完成轮次的账照旧报，未完成的尝试单独标未知。

这一组测试同时钉住**负控**要红的那三件事：全失败文案、真 0 次文案、重试数字。
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

from tianshu.capability import CapabilityProfile
from tianshu.cli import _print_result, main
from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelError, ModelResponse, ScriptedModel, ToolCall
from tianshu.snapshot import SnapshotStore

TIMEOUT = "请求超时：timed out"


class _FlakyModel:
    """前 N 次 `complete()` 抛可重试 timeout，然后按脚本回答。

    模拟黑洞端点：请求确实发出去了、对端收了连接但一个字节都不回，最后超时。
    它**不是** scripted 模型的"没答案"——两者在账上完全不同。
    """

    def __init__(
        self,
        failures: int,
        then: ModelResponse | None = None,
        *,
        kind: str = "timeout",
        retryable: bool = True,
    ) -> None:
        self._remaining = failures
        self._then = then
        self._kind = kind
        self._retryable = retryable
        self.calls = 0

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="tests/test_cost_line.py（超时替身）",
            model="blackhole",
            supports_tool_calling=True,
            declared_by="tests/test_cost_line.py",
        )

    def complete(self, _messages, _tools) -> ModelResponse:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise ModelError(TIMEOUT, kind=self._kind, retryable=self._retryable)
        assert self._then is not None, "脚本用完了：这个用例只该跑 N+1 次"
        return self._then


def _limits(*, max_turns: int = 8, attempts: int = 3) -> Limits:
    return Limits(
        max_turns=max_turns,
        max_total_tokens=20_000,
        max_model_attempts=attempts,
        retry_backoff_ms=0,
    )


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes("待总结的内容\n".encode())
    return ws


def _run(tmp_path: Path, model, *, limits: Limits | None = None):
    ws = _workspace(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    result = run(
        task="把 a.txt 的内容总结成 summary.md",
        workspace_dir=ws,
        out_name="summary.md",
        model=model,
        log=log,
        limits=limits,
        snapshots=SnapshotStore(tmp_path / "artifacts"),
        sleep=lambda _seconds: None,  # 退避不真睡：测的是计数，不是等待
    )
    return result, log


def _render(result) -> str:
    """走同一条 `_print_result` 打印路径（CLI 只是它的调用方）。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        _print_result(result, as_json=False)
    return out.getvalue()


def _render_json(result) -> dict[str, Any]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        _print_result(result, as_json=True)
    return json.loads(out.getvalue())


def _intents(log: EventLog, run_id: str) -> int:
    return sum(1 for event in log.read_run(run_id) if event.type == "model.intent")


# ---------------------------------------------------------------- 1. 全失败：不许说"没有模型调用"


def test_all_attempts_failed_counts_every_attempt(tmp_path):
    """黑洞端点：3 次尝试全超时 → turns=0 但 attempts=3，费用行必须说出来。

    负控目标：把 `_print_result` 改回 `result.turns` 后本用例红（文案回到
    「无（没有模型调用）」）。
    """
    model = _FlakyModel(failures=3)
    result, log = _run(tmp_path, model, limits=_limits(attempts=3))

    assert result.state == "failed", result.error
    assert "3 次都失败" in (result.error or ""), result.error
    assert result.turns == 0
    assert result.model_attempts == 3, "每次真正发出请求都要 +1"
    # 尝试次数不是从"完成的轮次"倒推出来的：事件里确实有 3 条 model.intent
    assert _intents(log, result.run_id) == 3
    assert model.calls == 3

    text = _render(result)
    assert "没有模型调用" not in text, f"打了 3 次却说没调用：{text}"
    assert "0 个完成轮次 / 3 次尝试（费用未知）" in text, text
    assert "$0.000000" not in text, "未知不许显示成 0 元"

    payload = _render_json(result)
    assert payload["model_attempts"] == 3
    assert payload["model_attempts_known"] is True
    assert payload["turns"] == 0
    assert payload["unknown_cost_calls"] == 0


def test_attempts_counter_survives_non_retryable_failure(tmp_path):
    """不可重试的错误只试一次：数字是 1，不是 0。"""
    model = _FlakyModel(failures=1, kind="auth", retryable=False)
    result, _ = _run(tmp_path, model, limits=_limits(attempts=3))

    assert result.turns == 0
    assert result.model_attempts == 1
    text = _render(result)
    assert "0 个完成轮次 / 1 次尝试（费用未知）" in text, text
    assert "没有模型调用" not in text


# ---------------------------------------------------------------- 2. 真 0 次：短语必须保留


def test_true_zero_calls_still_says_no_model_call(tmp_path):
    """一次请求都没发出（max_turns=0 在调用之前就停）→ 才允许说"没有模型调用"。"""
    result, log = _run(
        tmp_path, ScriptedModel([ModelResponse("end_turn", content="不该被调用")]),
        limits=_limits(max_turns=0),
    )

    assert result.state == "failed", result.error
    assert "最大轮数 0" in (result.error or ""), result.error
    assert result.turns == 0
    assert result.model_attempts == 0
    assert _intents(log, result.run_id) == 0, "真的没有发出过请求"

    text = _render(result)
    assert "无（没有模型调用）" in text, f"真 0 次反而不敢说：{text}"


# ---------------------------------------------------------------- 3. 重试后成功：数字是 3 不是 1


def test_retry_then_success_reports_three_attempts(tmp_path):
    """两次超时后第三次成功：完成轮次是 1，尝试次数是 3，费用行两者都要说。"""
    model = _FlakyModel(failures=2, then=ModelResponse("end_turn", content="完成"))
    result, log = _run(tmp_path, model, limits=_limits(attempts=3))

    assert result.state in ("succeeded", "noop"), result.error
    assert result.turns == 1
    assert result.model_attempts == 3
    assert _intents(log, result.run_id) == 3

    text = _render(result)
    assert "没有模型调用" not in text
    assert "共 3 次尝试" in text, f"重试 3 次却没写 3：{text}"
    assert "2 次尝试未完成" in text, text


# ---------------------------------------------------------------- 4. 账本：resume 不丢尝试次数


def _tool_call() -> ModelResponse:
    return ModelResponse(
        "tool_use",
        tool_calls=[
            ToolCall("c1", "modify_text", {"path": "a.txt", "content": "改后\n"})
        ],
    )


def test_attempts_persist_across_resume(tmp_path):
    """等待审批前已试 3 次；resume 后再问一次 → 4，不是从 0 重数。

    尝试次数进的是游标的预算账本（和 tokens / 成本同源），所以 resume 读得到。
    """
    from tianshu.loop import AgentLoop, build_stack
    from tianshu.permissions import HumanIdentity

    ws = _workspace(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "authority")
    task = stack.tasks.create_task("改 a.txt", [{"root_path": str(ws)}])

    first_engine = AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        model=_FlakyModel(failures=2, then=_tool_call()),
        limits=_limits(attempts=3),
        sleep=lambda _seconds: None,
    )
    waiting = first_engine.start(task.task_id)
    assert waiting.state == "waiting_approval", waiting.error
    assert waiting.turns == 1
    assert waiting.model_attempts == 3

    # 工具结果与游标在同一个提交里：waiting 时账本已经落库
    cursor = stack.tasks.load_cursor(waiting.run_id)
    assert cursor is not None
    assert cursor[0].budget_ledger["model_attempts"] == 3

    stack.approvals.decide(
        waiting.approval_ids[0], decision="approved", actor=HumanIdentity.local_cli()
    )
    second_engine = AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        model=ScriptedModel([ModelResponse("end_turn", content="完成")]),
        limits=_limits(attempts=3),
    )
    done = second_engine.resume_run(waiting.run_id)
    log.close()

    assert done.state == "succeeded", done.error
    assert done.turns == 2, "resume 之后又完成了一轮"
    assert done.model_attempts == 4, "3 次已记录 + resume 后 1 次，不能从 0 重数"


# ---------------------------------------------------------------- 5. CLI 的 --json / status


def _cli(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue() + err.getvalue()


def _cli_run(tmp_path: Path) -> tuple[int, str, str]:
    ws = _workspace(tmp_path)
    code, out = _cli(
        "run",
        "--task",
        "整理目录里的笔记",
        "--workspace",
        str(ws),
        "--out",
        "summary.md",
        "--db",
        str(tmp_path / "cli.db"),
        "--data-root",
        str(tmp_path / "cli-authority"),
        "--model",
        "scripted",
    )
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("run_id")
    )
    return code, out, run_id


def test_cli_json_reports_model_attempts(tmp_path):
    code, out, _run_id = _cli_run(tmp_path)
    assert code == 0, out
    # 正常的 scripted run：3 轮都成功 → turns == attempts == 3
    assert "turns  : 3" in out
    assert "费用" in out and "没有模型调用" not in out

    code, out = _cli(
        "run",
        "status",
        "--run-id",
        _run_id,
        "--db",
        str(tmp_path / "cli.db"),
        "--json",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["model_attempts"] == 3, payload
    assert payload["unknown_cost_calls"] == 3


def test_run_json_has_model_attempts_key(tmp_path):
    """`run --json` 的 run 对象里必须带 model_attempts（issue #58 第 3 条）。"""
    ws = _workspace(tmp_path)
    code, out = _cli(
        "run",
        "--task",
        "整理目录里的笔记",
        "--workspace",
        str(ws),
        "--out",
        "summary.md",
        "--db",
        str(tmp_path / "cli.db"),
        "--data-root",
        str(tmp_path / "cli-authority"),
        "--model",
        "scripted",
        "--json",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["run"]["model_attempts"] == 3, payload["run"]
    assert payload["run"]["turns"] == 3


def test_status_falls_back_honestly_for_legacy_cursor(tmp_path):
    """旧游标没有 model_attempts 键 → JSON 里是 null（不知道），不是 0。"""
    code, out, run_id = _cli_run(tmp_path)
    assert code == 0, out

    # 模拟升级前写下的游标：把账本里的新键删掉
    db = tmp_path / "cli.db"
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT payload FROM run_cursors WHERE run_id=?", (run_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["budget_ledger"].pop("model_attempts", None)
        conn.execute(
            "UPDATE run_cursors SET payload=? WHERE run_id=?",
            (json.dumps(payload), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    code, out = _cli(
        "run", "status", "--run-id", run_id, "--db", str(db), "--json"
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["model_attempts"] is None, (
        "旧游标没有尝试次数：必须说「不知道」，不能说 0 次"
    )
    code, text = _cli(
        "run", "status", "--run-id", run_id, "--db", str(db)
    )
    assert code == 0, text
    # 文本费用行同样不许把下限说成精确值：旧游标只按完成轮次报账
    assert "没有模型调用" not in text, text


def test_print_result_does_not_treat_a_legacy_lower_bound_as_exact():
    """`RunResult.model_attempts_known=False` 时，费用行不许提"尝试"。

    旧游标只能给出下限；下限不是精确值，硬写「共 N 次尝试」就是在编数。
    """
    from tianshu.loop import RunResult

    result = RunResult(
        run_id="run_legacy",
        outcome="failed",
        effect_state="none_applied",
        turns=2,
        model_attempts=2,
        model_attempts_known=False,
        cost_total_micros=900,
        unknown_cost_calls=0,
    )
    text = _render(result)
    cost_line = next(line for line in text.splitlines() if line.startswith("费用"))
    assert "尝试" not in cost_line, cost_line
    assert "没有模型调用" not in cost_line, cost_line
    assert "$0.000900 USD（2 次调用）" in cost_line, cost_line


def test_legacy_cursor_attempts_stay_unknown_after_resume(tmp_path):
    """旧游标标记 `model_attempts_known=False`：即使之后又记了数，也只算下限。

    `_Progress.of` 从完成轮次给一个下限，并把"不完整"写进账本；`run status`
    必须据此报 `null`，而不是把下限当成精确值展示。
    """
    from tianshu.loop import _Progress
    from tianshu.tasks import RunCursor

    legacy = _Progress.of(
        RunCursor.of({"messages": [], "budget_ledger": {"turns": 2}})
    )
    assert legacy.model_attempts == 2, "下限：两次成功至少两次尝试"
    assert legacy.model_attempts_known is False, "旧游标不知道真实尝试次数"
    assert legacy.to_cursor().budget_ledger["model_attempts_known"] is False

    modern = _Progress.of(
        RunCursor.of(
            {"messages": [], "budget_ledger": {"turns": 2, "model_attempts": 5}}
        )
    )
    assert modern.model_attempts == 5
    assert modern.model_attempts_known is True

    # 模拟"升级前写下的游标，后来被 resume 又写回了账本"：键在，但 known=False
    code, out, run_id = _cli_run(tmp_path)
    assert code == 0, out
    db = tmp_path / "cli.db"
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT payload FROM run_cursors WHERE run_id=?", (run_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["budget_ledger"]["model_attempts"] = 2
        payload["budget_ledger"]["model_attempts_known"] = False
        conn.execute(
            "UPDATE run_cursors SET payload=? WHERE run_id=?",
            (json.dumps(payload), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    code, out = _cli("run", "status", "--run-id", run_id, "--db", str(db), "--json")
    assert code == 0, out
    assert json.loads(out)["model_attempts"] is None, (
        "known=False 的下限不是精确值：JSON 必须报 null"
    )