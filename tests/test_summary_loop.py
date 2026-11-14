"""语义摘要的生成路径与长任务验收（H3 的判据）。

PRD §7.1 第 1 / 4 / 5 条是这一组的立据：

> 检索、提取、摘要在纯编译函数外执行；摘要调用经过授权与共享预算，并在被任何
> 后续模型请求引用前持久化。

> 摘要失败、预算不足或证据缺失时，使用有界确定性裁剪或明确暂停；不偷偷增加一次
> 模型调用。

> 用长任务夹具比较正确性、关键信息保留、费用与延迟；关键约束被摘要遗漏必须能被
> 测试发现。验收包含断网回放、摘要生成后崩溃恢复，以及更换摘要模型不改变历史结果。

这里的"模型"是脚本替身：它按脚本吐响应，但**按真实上下文作答**（看到约束才说
约束在），这样"关键信息保留"才不是一句空话。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import engine_for, stack_for, write_exact
from test_model_contract import VENDORS, FakeTransport, _model, _payload

from tianshu.capability import CapabilityProfile
from tianshu.context import ContextBudget, compile_context
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.model import ModelIdentity, ModelResponse, ToolCall
from tianshu.replay import replay
from tianshu.summaries import SUMMARY_MAX_BYTES, SummaryStore

CONSTRAINT = "硬约束：报告只用中文。"
ROUNDS = 6
BUDGET_BYTES = 3000


def _ws(tmp_path: Path) -> Path:
    """两个文件：约束只在第一轮读的那个里，其余轮次读的正文没有约束。

    只用一个文件会让"丢失"测不出来——每轮都读同一份带约束的文件，后面几轮的
    工具输出里当然还有它（我第一次就是被这个夹具骗成假绿的）。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    write_exact(ws / "notes.md", CONSTRAINT + "\n" + ("笔记正文。" * 120) + "\n")
    write_exact(ws / "body.md", ("后续正文，不含硬约束。" * 60) + "\n")
    return ws


class ConstraintModel:
    """长任务替身：

    - 正常轮次：连续 `rounds` 次 `read_text`，然后一轮"根据上下文作答"；
    - 摘要轮次（`tools == []`）：只把材料里带约束的那一行吐回去。

    作答**真的看上下文**：约束还在（原文或摘要里）就说"约束已保留"，否则说
    "约束缺失"。「关键信息保留」这条判据靠的就是这个差别。
    """

    def __init__(self, *, rounds: int = ROUNDS, summary_keeps_constraint: bool = True) -> None:
        self.rounds = rounds
        self.summary_keeps_constraint = summary_keeps_constraint
        self.calls = 0
        self.summary_calls = 0
        self.round_calls = 0  # 只数正常轮次：摘要调用不许挤掉工具轮
        self.requests: list[int] = []  # 每次请求的字符数（费用对照用）
        self.answer = ""
        self.capability = CapabilityProfile(
            provider="fake",
            model="constraint-model",
            supports_tool_calling=True,
            declared_by="tests/test_summary_loop.py（长任务替身）",
        )

    def capability_profile(self) -> CapabilityProfile:
        return self.capability

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="fake", vendor="acme", model="constraint-model")

    def _material_has_constraint(self, messages: list[dict]) -> bool:
        return any(CONSTRAINT in (m.get("content") or "") for m in messages)

    def complete(self, messages, tools):  # noqa: ANN001, ARG002
        self.calls += 1
        self.requests.append(sum(len(m.get("content") or "") for m in messages))
        if not tools:
            self.summary_calls += 1
            text = CONSTRAINT if self.summary_keeps_constraint else "早期对话涉及若干笔记文件。"
            text = text[:SUMMARY_MAX_BYTES]
            return ModelResponse(
                "end_turn",
                content=text,
                usage={"input_tokens": 120, "output_tokens": 20},
            )
        self.round_calls += 1
        if self.round_calls <= self.rounds:
            path = "notes.md" if self.round_calls == 1 else "body.md"
            return ModelResponse(
                "tool_use",
                tool_calls=[ToolCall(f"c{self.round_calls}", "read_text", {"path": path})],
                usage={"input_tokens": 40, "output_tokens": 8},
            )
        kept = self._material_has_constraint(messages)
        self.answer = "约束已保留：只用中文" if kept else "约束缺失"
        return ModelResponse(
            "end_turn",
            content=self.answer,
            usage={"input_tokens": 30, "output_tokens": 6},
        )


def _run(tmp_path: Path, model, *, summary_mode: str = "off", limits_kwargs=None):
    ws = _ws(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path / "data")
    from tianshu.loop import Limits

    kwargs = {"max_turns": ROUNDS + 3, "context_budget_bytes": BUDGET_BYTES,
              "summary_mode": summary_mode}
    kwargs.update(limits_kwargs or {})
    engine = engine_for(stack, model, limits=Limits(**kwargs))
    task = stack.tasks.create_task("长任务：整理笔记并写报告", [str(ws)])
    result = engine.start(task.task_id, out_name="")
    return result, stack, log


def _store(log: EventLog) -> SummaryStore:
    return SummaryStore(log, create=False)


def _intent_payloads(log: EventLog, run_id: str) -> list[dict]:
    return [
        event.payload
        for event in log.read_run(run_id)
        if event.type == "model.intent"
    ]


# ---------------------------------------------------------------- 默认关


def test_default_off_makes_no_summary_calls(tmp_path):
    """默认 off：一次摘要调用都不发生，摘要表保持空，模型调用数与轮次一致。"""
    model = ConstraintModel()
    result, stack, log = _run(tmp_path, model)

    assert result.outcome in ("succeeded", "noop")
    assert result.summary_calls == 0
    assert _store(log).summary_count() == 0
    assert model.summary_calls == 0
    assert result.model_attempts == result.turns
    assert all(
        "summaries" not in (payload.get("context") or {})
        for payload in _intent_payloads(log, result.run_id)
    )
    log.close()


# ---------------------------------------------------------------- 生成与复用


def test_summary_calls_are_bounded_and_recorded(tmp_path):
    """开启后调用次数有界（`summary_max_calls`），且每次都有可核对的记录。

    为什么不是"恰好 1 次"：材料身份含事件 seq，丢弃集合随轮次变大就是新材料；
    严格一次只对"整条 Run 只丢一次"成立。有界 + 有记录才是可兑现的承诺。
    """
    model = ConstraintModel()
    result, stack, log = _run(tmp_path, model, summary_mode="model")

    assert result.outcome in ("succeeded", "noop")
    assert 1 <= result.summary_calls == model.summary_calls <= 3
    store = _store(log)
    assert store.summary_count() == model.summary_calls
    record = store.latest_ready(
        store._conn.execute("SELECT material_id FROM summary_versions").fetchone()[0]
    )
    assert record is not None and record.ready
    assert record.model == "fake:acme:constraint-model"
    assert record.prompt_version.startswith("summary-prompt-")
    assert record.summary_version.startswith("summary-")
    assert record.input_tokens == 120 and record.output_tokens == 20
    assert record.text == CONSTRAINT
    log.close()


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("known_field", ["input_tokens", "output_tokens"])
def test_partial_summary_usage_keeps_token_occupation(tmp_path, vendor, known_field):
    raw_field = {
        "input_tokens": "prompt_tokens", "output_tokens": "completion_tokens",
    }[known_field] if vendor == "openai" else known_field
    transport = FakeTransport(_payload(vendor, text=CONSTRAINT, usage={raw_field: 7}))
    adapter = _model(vendor, transport)

    class PartialSummaryModel(ConstraintModel):
        def complete(self, messages, tools):
            if not tools:
                self.calls += 1
                self.summary_calls += 1
                return adapter.complete(messages, tools)
            return super().complete(messages, tools)

    model = PartialSummaryModel()
    result, stack, log = _run(
        tmp_path, model, summary_mode="model",
        limits_kwargs={"summary_max_calls": 1,
                       "budget_limits": {"tokens": 100_000, "requests": 100}},
    )
    try:
        assert result.outcome in ("succeeded", "noop")
        assert result.summary_calls == model.summary_calls == len(transport.calls) == 1
        usage = stack.budget.usage(f"run:{result.run_id}")
        assert usage["reserved"]["tokens"] > 0, "摘要缺一半用量也不能释放 token 预占"
        assert result.unknown_usage_calls == 1
        # 普通轮次也没有可核验价格：每次金额都应未决，只有摘要缺 token。
        rows = stack.budget.reconcile(f"run:{result.run_id}")
        assert usage["unknown_reservations"] == len(rows) == model.calls
        assert all("micros" not in row["usage"] for row in rows)
        assert sum("tokens" not in row["usage"] for row in rows) == 1
        normal_tokens = ROUNDS * (40 + 8) + 30 + 6
        assert usage["settled"]["tokens"] == normal_tokens
        assert result.tokens == normal_tokens + 7
        assert usage["settled"]["requests"] == model.calls
    finally:
        log.close()


def test_summary_is_referenced_by_the_next_model_request(tmp_path):
    """摘要在被引用的那次请求里**已经落库**，并被写进 `context.summaries`。"""
    model = ConstraintModel()
    result, stack, log = _run(tmp_path, model, summary_mode="model")
    payloads = _intent_payloads(log, result.run_id)
    with_summary = [p for p in payloads if (p.get("context") or {}).get("summaries")]
    assert with_summary, "摘要没有被任何一次请求引用：等于白花钱"
    refs = with_summary[0]["context"]["summaries"]
    assert refs and refs[0]["source_seqs"]
    store = _store(log)
    record = store.read(refs[0]["summary_id"])
    assert record is not None, "被引用的摘要必须已经在库里（先持久化再引用）"
    assert record.status == "ready"
    assert record.text_digest == refs[0]["text_digest"]
    log.close()


def test_same_material_is_reused_not_recalled(tmp_path):
    """同一材料再次出现 ⇒ 复用已落库的摘要（0 次新调用）。

    判据：调用次数**显著少于轮次**。如果每轮都重新摘要，次数会跟轮次同量级。
    """
    model = ConstraintModel(rounds=ROUNDS + 4)
    result, stack, log = _run(
        tmp_path,
        model,
        summary_mode="model",
        limits_kwargs={"summary_max_calls": 99, "max_turns": 30},
    )
    assert result.outcome in ("succeeded", "noop")
    assert model.summary_calls == result.summary_calls
    dropped_turns = sum(
        1
        for payload in _intent_payloads(log, result.run_id)
        if (payload.get("context") or {}).get("dropped_seqs")
        or (payload.get("context") or {}).get("summaries")
    )
    assert model.summary_calls < dropped_turns, (
        f"摘要调用 {model.summary_calls} 次、需要压缩的轮次 {dropped_turns} 次："
        "看起来每轮都重新摘要了（没有复用）"
    )
    assert result.model_attempts == result.turns + model.summary_calls
    log.close()


# ---------------------------------------------------------------- 失败回落


def test_budget_refusal_falls_back_without_calling_model(tmp_path):
    """账本拒绝 ⇒ 不调用摘要模型、写 failed 记录、回落确定性裁剪，Run 继续。"""
    model = ConstraintModel()
    result, stack, log = _run(
        tmp_path,
        model,
        summary_mode="model",
        limits_kwargs={"budget_limits": {"requests": 0}},
    )
    # `requests: 0` 连主调用都拒绝 —— 先验证"摘要不绕过账本"这一点：
    assert result.outcome == "failed"
    assert model.summary_calls == 0
    store = _store(log)
    assert store.summary_count() == 0, "预占在调用与落库之前：被拒时不该留下调用记录"
    log.close()


def test_upper_bound_without_declaration_skips_summary(tmp_path):
    """硬上限模式拿不到输出上界 ⇒ 摘要不发（回落），而不是把 Run 弄死在这里。"""
    model = ConstraintModel()
    result, stack, log = _run(
        tmp_path, model, summary_mode="model", limits_kwargs={"budget_bound": "upper"}
    )
    # 主调用在 upper 模式下会因为没声明输出上界被拒绝（既有口径），但摘要不能
    # 抛 `BudgetConfigError`：它必须安静回落，让主调用的拒绝成为唯一原因。
    assert result.outcome == "failed"
    assert (result.error or "").startswith("预算配置拒绝"), result.error
    assert model.summary_calls == 0
    assert _store(log).summary_count() == 0
    log.close()


def test_summary_failure_falls_back_to_deterministic_view(tmp_path):
    """摘要正文不合格（超长）⇒ 记录 failed、回落确定性裁剪，Run 仍跑完。"""

    class LongSummaryModel(ConstraintModel):
        def complete(self, messages, tools):  # noqa: ANN001, ARG002
            if not tools:
                self.calls += 1
                self.summary_calls += 1
                return ModelResponse(
                    "end_turn",
                    content="x" * (SUMMARY_MAX_BYTES + 10),
                    usage={"input_tokens": 10, "output_tokens": 5},
                )
            return super().complete(messages, tools)

    model = LongSummaryModel()
    result, stack, log = _run(tmp_path, model, summary_mode="model")
    assert result.outcome in ("succeeded", "noop")
    store = _store(log)
    rows = [
        row
        for row in store._conn.execute("SELECT * FROM summary_versions ORDER BY created_at")
    ]
    assert rows and all(row["status"] == "failed" for row in rows)
    assert all("正文不合格" in (row["error"] or "") for row in rows)
    assert len(rows) == model.summary_calls == result.summary_calls
    report = replay(log, result.run_id)
    assert report.ok, report.problems
    log.close()


def test_upper_mode_summary_reservation_refuses_locally(tmp_path, monkeypatch):
    """upper 模式拿不到输出上界：摘要**在自己这里**拒绝并回落，不抛给 Run。

    为什么单独测这一层：正常路径上（模型没声明上界）主调用会先被拒绝，摘要
    根本轮不到，整轮只看到主调用的错。所以这里直接调 `_maybe_summarize`，把
    "摘要必须自己安静失败"钉死（NC7 拆掉那个 except 分支就会红）。
    """
    from types import SimpleNamespace

    from tianshu.errors import BudgetConfigError
    from tianshu.loop import Limits

    class UndeclaredModel(ConstraintModel):
        def capability_profile(self):  # noqa: ANN201
            return CapabilityProfile(
                provider="fake", model="no-bound", supports_tool_calling=True,
                declared_by="tests/test_summary_loop.py（不声明输出上界）",
            )

    ws = _ws(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path / "data")
    engine = engine_for(
        stack,
        UndeclaredModel(),
        limits=Limits(
            context_budget_bytes=BUDGET_BYTES, summary_mode="model", budget_bound="upper"
        ),
    )
    task = stack.tasks.create_task("长任务", [str(ws)])
    messages, seqs = _history_messages()
    budget = ContextBudget(max_bytes=BUDGET_BYTES)
    view = compile_context(messages, seqs, budget)
    assert view.dropped_seqs, "夹具没触发压缩"
    monkeypatch.setattr(
        engine, "_message_source_seqs", lambda *_a, **_k: list(seqs)
    )

    out = engine._maybe_summarize(
        "run:test-upper", task, SimpleNamespace(messages=messages), view, budget
    )
    assert out.summaries == (), "拿不到上界就不许摘"
    store = _store(log)
    rows = [row for row in store._conn.execute("SELECT * FROM summary_versions")]
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "输出上界" in (rows[0]["error"] or "")

    # 直接验证底层拒绝的原因，别让"安静回落"变成"什么都没发生"。
    from tianshu.summaries import MaterialBlock, build_material

    material = build_material(
        [MaterialBlock(seq=seqs[2], role="tool", content="材料")], scope="task:t"
    )
    with pytest.raises(BudgetConfigError, match="输出上界"):
        engine._reserve_summary(material, "attempt#1", "run:test-upper")
    log.close()


def _history_messages() -> tuple[list[dict], list[int]]:
    """与 `_ws` 同形的历史：6 段往返、每段一条 assistant + 一条长工具输出。"""
    messages: list[dict] = [
        {"role": "system", "content": "你是执行器。"},
        {"role": "user", "content": "长任务：整理笔记"},
    ]
    seqs = [0, 0]
    seq = 1
    for index in range(6):
        messages.append(
            {
                "role": "assistant",
                "content": "读文件" + "x" * 400,
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_text", "arguments": "{}"},
                    }
                ],
            }
        )
        seqs.append(seq)
        seq += 1
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "name": "read_text",
                "content": "输出" + "y" * 400,
            }
        )
        seqs.append(seq)
        seq += 1
    return messages, seqs


# ---------------------------------------------------------------- 长任务验收


def test_long_task_key_information_retained_only_with_summary(tmp_path):
    """长任务对照：不带摘要 = 约束丢了；带摘要 = 约束留下。"""
    off_model = ConstraintModel()
    off_result, _, off_log = _run(tmp_path / "off", off_model, summary_mode="off")
    assert off_result.outcome in ("succeeded", "noop")
    off_log.close()

    on_model = ConstraintModel()
    start = time.perf_counter()
    on_result, _, on_log = _run(tmp_path / "on", on_model, summary_mode="model")
    elapsed = time.perf_counter() - start
    assert on_result.outcome in ("succeeded", "noop")
    on_log.close()

    # 关键信息保留：确定性裁剪把第一段（带约束）丢了 ⇒ 模型说约束缺失；
    # 摘要把它留下了 ⇒ 模型说约束保留。这是"摘要确实进了上下文"的端到端证据。
    assert off_model.answer == "约束缺失"
    assert on_model.answer.startswith("约束已保留")

    # 费用对照（本机实测，只报告不断言 provider 账单）：摘要模式多 N 次调用
    # （N ≤ `summary_max_calls`），但每次请求更短。两个数字都写进 docs，不当 SLO。
    assert 1 <= on_result.summary_calls <= 3
    assert on_result.model_attempts == on_result.turns + on_result.summary_calls
    assert on_result.tokens > 0 and off_result.tokens > 0
    assert on_result.tokens > off_result.tokens  # 多一次调用，token 必然更多
    assert elapsed < 60  # 宽松上界：只挡住"跑成死循环"，不当延迟指标


def test_key_constraint_omission_is_detectable(tmp_path):
    """摘要漏掉约束必须能被**探测器**发现（不是靠人眼看）。"""

    def retained(summary_text: str, constraints: list[str]) -> list[str]:
        """返回摘要里**没有**保住的硬约束。空列表 = 全部保住。"""
        return [c for c in constraints if c not in summary_text]

    good = "早期材料：笔记文件若干。" + CONSTRAINT
    assert retained(good, [CONSTRAINT]) == []

    bad_model = ConstraintModel(summary_keeps_constraint=False)
    bad_result, _, log = _run(tmp_path, bad_model, summary_mode="model")
    assert bad_result.outcome in ("succeeded", "noop")
    store = _store(log)
    record = store.latest_ready(
        store._conn.execute("SELECT material_id FROM summary_versions").fetchone()[0]
    )
    assert record is not None and record.text is not None
    assert retained(record.text, [CONSTRAINT]) == [CONSTRAINT], "探测器必须报缺"
    # 摘要漏掉约束 ⇒ 模型按上下文如实作答"约束缺失"（端到端一致，不是假绿）。
    assert bad_model.answer == "约束缺失"
    log.close()


# ---------------------------------------------------------------- 回放与换模型


def test_offline_replay_reads_saved_summary(tmp_path):
    """断网回放：只用事件日志 + 同一个库里的摘要，不构造模型、不重新生成。"""
    model = ConstraintModel()
    result, stack, log = _run(tmp_path, model, summary_mode="model")
    assert result.summary_calls >= 1

    # 回放路径不接收模型参数——它根本不可能"重新生成"摘要。
    report = replay(log, result.run_id)
    assert report.ok, report.problems
    assert report.messages_verified >= 1
    log.close()


def test_replay_without_summary_records_reports_problem(tmp_path):
    """把摘要表丢掉（模拟"只带事件日志去回放"）⇒ problem，不假装通过。"""
    model = ConstraintModel()
    result, stack, log = _run(tmp_path, model, summary_mode="model")
    assert result.summary_calls >= 1
    store = _store(log)
    store._conn.execute("DROP TABLE summary_versions")
    log._conn.commit()

    report = replay(log, result.run_id)
    assert not report.ok
    assert any("摘要记录缺失" in problem for problem in report.problems), report.problems
    log.close()


def test_changing_summary_model_does_not_change_history(tmp_path):
    """换摘要模型：旧版本永久保留，历史运行继续引用它（回放结果不变）。"""
    model = ConstraintModel()
    result, stack, log = _run(tmp_path, model, summary_mode="model")
    store = _store(log)
    material_id = store._conn.execute(
        "SELECT material_id FROM summary_versions"
    ).fetchone()[0]
    v1 = store.latest_ready(material_id)
    assert v1 is not None

    # 显式"刷新"：**同一材料**换模型追加 v2。原始材料正文已经不在测试手里了，
    # 但材料身份只需要 `(scope, sources)`——用记录里的来源重建同一个身份即可。
    from tianshu.summaries import MaterialSource, SummaryMaterial

    ledger = store.material(material_id)
    assert ledger is not None
    same_material = SummaryMaterial(
        material_id=material_id,
        scope=ledger["scope"],
        task_id=ledger["task_id"],
        run_id=ledger["run_id"],
        sources=tuple(
            MaterialSource(kind=s["kind"], ref=s["ref"], digest=s["digest"])
            for s in ledger["sources"]
        ),
        text="（刷新用）",
    )
    v2 = store.complete(
        store.begin(same_material, model="other:vendor:model2"), "另一版摘要"
    )
    assert v2.version == 2 and v2.material_id == material_id
    assert store.latest_ready(material_id).summary_id == v2.summary_id

    # 历史运行的引用没变：它按 summary_id 指向 v1，回放仍然通过。
    assert store.read(v1.summary_id).text == v1.text
    report = replay(log, result.run_id)
    assert report.ok, report.problems
    log.close()


# ---------------------------------------------------------------- 崩溃恢复


_CHILD = """
import sys
sys.path.insert(0, {src!r})
from pathlib import Path
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ToolCall

db = Path({db!r})
ws = Path({ws!r})
log = EventLog(db, blob_dir=db.parent / "blobs")
stack = build_stack(log, data_root=db.parent / "data")


class M:
    def __init__(self):
        self.calls = 0

    def price_identity(self):
        from tianshu.model import ModelIdentity

        return ModelIdentity(adapter="fake", vendor="acme", model="m")

    def capability_profile(self):
        from tianshu.capability import CapabilityProfile

        return CapabilityProfile(
            provider="fake", model="m", supports_tool_calling=True,
            declared_by="child",
        )

    def complete(self, messages, tools):
        self.calls += 1
        if not tools:
            return ModelResponse("end_turn", content={constraint!r},
                                 usage={{"input_tokens": 5, "output_tokens": 2}})
        if self.calls <= {rounds}:
            return ModelResponse(
                "tool_use",
                tool_calls=[ToolCall(f"c{{self.calls}}", "read_text", {{"path": "notes.md"}})],
                usage={{"input_tokens": 10, "output_tokens": 4}},
            )
        return ModelResponse("end_turn", content="完成",
                             usage={{"input_tokens": 10, "output_tokens": 4}})


engine = AgentLoop(
    tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
    dispatcher=stack.dispatcher, approvals=stack.approvals,
    snapshots=stack.snapshots, model=M(),
    limits=Limits(max_turns={rounds} + 3, context_budget_bytes={budget},
                  summary_mode="model"),
    budget=stack.budget, contents=stack.contents, summaries=stack.summaries,
    memory=stack.memory,
)
task = stack.tasks.create_task("长任务", [{ws!r}])
engine.start(task.task_id, out_name="")
"""


def _spawn_crash(tmp_path: Path, ws: Path, rounds: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TIANSHU_FAILPOINT"] = "summary-after-call"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = _CHILD.format(
        src=src,
        db=str(tmp_path / "events.db"),
        ws=str(ws),
        rounds=rounds,
        budget=BUDGET_BYTES,
        constraint=CONSTRAINT,
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_crash_after_summary_call_leaves_pending_without_retry(tmp_path):
    """调用已发出、正文没落库就崩：重启只能看到 pending，且**不重试**。"""
    ws = _ws(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    proc = _spawn_crash(tmp_path, ws, ROUNDS)
    assert proc.returncode == 77, f"未在摘要注入点硬退：rc={proc.returncode}\n{proc.stderr}"
    assert "[failpoint] summary-after-call" in proc.stderr

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "data")
    store = _store(log)
    rows = store.versions(
        store._conn.execute("SELECT material_id FROM summary_versions").fetchone()[0]
    )
    assert [row.status for row in rows] == ["pending"], "崩溃点必须留下待结算记录"
    run_id = log.read_types(["run.start"])[0].run_id

    class ResumeModel(ConstraintModel):
        def __init__(self):
            super().__init__(rounds=0)
            self.calls = 99  # 直接进入"作答"分支

    resume_model = ResumeModel()
    engine = engine_for(
        stack,
        resume_model,
        limits=_limits(summary_mode="model"),
    )
    result = engine.resume_run(run_id)

    assert result.outcome in ("succeeded", "noop")
    # **关键断言**：没有第二次摘要调用 —— 不知道结果就不重试。
    assert resume_model.summary_calls == 0
    rows = store.versions(rows[0].material_id)
    assert [row.status for row in rows] == ["failed"]
    assert "中断" in (rows[0].error or "")
    assert result.summary_calls == 1  # 崩掉的那一次也如实计数
    report = replay(log, run_id)
    assert report.ok, report.problems
    log.close()


def _limits(*, summary_mode: str):
    from tianshu.loop import Limits

    return Limits(
        max_turns=ROUNDS + 3,
        context_budget_bytes=BUDGET_BYTES,
        summary_mode=summary_mode,
    )
