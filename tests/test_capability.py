"""F9a 模型准入 + 有界重试：先判"能不能跑"，再判"跑了几次"。

这一组测试钉四件事，都是"事后复盘成本极高"的那类：

1. **未声明 ≠ 支持**（fail closed）：能力没声明的模型被拒绝，不是"先试试看"。
   同时缺能力时**一分钱都不花**：模型一次都没被调用，日志里也没有 intent。
2. **两种"不知道"分开**：必需能力没声明 → 拒绝；上下文窗口没声明 → 不核验
   （也不假装核验过）。把两者混成一句，运维就只能猜是模型不行还是配置漏了。
3. **每次 attempt 都可审计**：`model.intent` 的 `attempt` 必须是 1,2,3。
   三次尝试全写成 1 的日志等于没有审计——看不出"同一件事试了三遍"。
4. **重试受预算约束**：失败调用不产生 usage，所以"再试一次会不会超预算"必须
   用上一次成功调用的用量去投影；不能只盯着账本（账本上看不出来）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tianshu.capability import (
    REQUIRED_FOR_RUN,
    CapabilityMissing,
    CapabilityProfile,
    MissingCapability,
    admit_model,
    capability_profile_of,
    check_capabilities,
    check_context_budget,
    missing_capabilities,
    retry_delay_ms,
)
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import (
    AnthropicMessagesModel,
    ModelError,
    ModelResponse,
    OpenAICompatibleModel,
    RecordedModel,
    ScriptedModel,
    ToolCall,
)
from tianshu.snapshot import SnapshotStore

# ---------------------------------------------------------------- 构件


def _profile(**overrides: Any) -> CapabilityProfile:
    base: dict[str, Any] = {
        "provider": "test",
        "model": "probe",
        "supports_tool_calling": True,
    }
    base.update(overrides)
    return CapabilityProfile(**base)


def _tool_turn(path: str = "note.md", call_id: str = "c1") -> ModelResponse:
    return ModelResponse(
        "tool_use", tool_calls=[ToolCall(call_id, "read_text", {"path": path})]
    )


def _end(text: str = "完成") -> ModelResponse:
    return ModelResponse("end_turn", content=text)


def _retryable() -> ModelError:
    return ModelError.from_http(503, "upstream 挂了")


def _non_retryable() -> ModelError:
    return ModelError("缺少 API key", kind="auth", retryable=False)


class ProbeModel:
    """按脚本返回/抛错的替身，记录"真的被调用了几次"和"能力被问了几次"。"""

    def __init__(
        self, profile: CapabilityProfile | None, script: list[Any] | None = None
    ) -> None:
        self._profile = profile
        self._script = list(script or [])
        self.calls = 0
        self.profile_calls = 0

    def capability_profile(self) -> CapabilityProfile:
        self.profile_calls += 1
        assert self._profile is not None
        return self._profile

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        self.calls += 1
        item = self._script.pop(0) if self._script else _end()
        if isinstance(item, BaseException):
            raise item
        return item


class UnprofiledModel:
    """只会补全、不声明任何能力：准入必须拒绝它。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        self.calls += 1
        return _end()


class Sleeper:
    """记录"本来要睡多久"，一秒都不真睡。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text("原始内容\n", encoding="utf-8")
    return ws


def _run(
    tmp_path: Path,
    model: object,
    *,
    limits: Limits | None = None,
    sleep: Sleeper | None = None,
):
    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    result = run(
        task="整理目录里的笔记",
        workspace_dir=_workspace(tmp_path),
        out_name="summary.md",
        model=model,  # type: ignore[arg-type]
        log=log,
        limits=limits,
        snapshots=SnapshotStore(tmp_path / "artifacts"),
        sleep=sleep,
    )
    return result, log


def _intents(log: EventLog, run_id: str) -> list[Any]:
    return [e for e in log.read_run(run_id) if e.type == "model.intent"]


def _errors(log: EventLog, run_id: str) -> list[Any]:
    return [e for e in log.read_run(run_id) if e.type == "model.error"]


# ---------------------------------------------------------------- 1. 口径


def test_required_for_run_is_deliberately_minimal():
    """必需集合只有 tool_calling：其余是性能/可选项，不能拿 fail closed 大棒拒。"""
    assert REQUIRED_FOR_RUN == frozenset({"tool_calling"})


def test_supports_only_counts_explicit_true():
    """未声明（None）在结论上与 False 一样：都不支持。"""
    assert _profile().supports("tool_calling") is True
    assert _profile(supports_tool_calling=False).supports("tool_calling") is False
    assert _profile(supports_tool_calling=None).supports("tool_calling") is False

    # 但"没写"和"写了做不到"必须能分开读
    assert _profile(supports_tool_calling=None).declaration("tool_calling") is None
    assert _profile(supports_tool_calling=False).declaration("tool_calling") is False


def test_undeclared_rules_out_the_rest():
    profile = _profile(supports_parallel_tool_calls=False)
    assert profile.undeclared() == ["streaming", "thinking"]


def test_unknown_feature_name_is_an_error_not_a_pass():
    """写错特征名不能悄悄算"已满足"——那是 fail open。"""
    with pytest.raises(ValueError):
        _profile().supports("tool_callng")
    with pytest.raises(ValueError):
        missing_capabilities(_profile(), frozenset({"nope"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": ""},
        {"model": ""},
        {"supports_tool_calling": "yes"},
        {"context_window_bytes": 0},
        {"context_window_bytes": True},
    ],
)
def test_profile_validates_its_fields(overrides):
    with pytest.raises(ValueError):
        _profile(**overrides)


def test_missing_capability_says_which_kind_of_missing():
    undeclared = missing_capabilities(_profile(supports_tool_calling=None))
    assert undeclared == (MissingCapability("tool_calling", None),)
    assert "未声明" in undeclared[0].describe()

    declared_false = missing_capabilities(_profile(supports_tool_calling=False))
    assert declared_false == (MissingCapability("tool_calling", False),)
    assert "声明为不支持" in declared_false[0].describe()


def test_profile_payload_keeps_unknown_as_null():
    payload = _profile().to_payload()
    assert payload["tool_calling"] is True
    assert payload["thinking"] is None, "没声明就是 null，不是 false"
    assert payload["context_window_bytes"] is None


# ---------------------------------------------------------------- 2. 准入


def test_admit_rejects_model_without_profile():
    with pytest.raises(CapabilityMissing) as excinfo:
        admit_model(UnprofiledModel())
    message = str(excinfo.value)
    assert "没有声明能力" in message
    assert "未声明" in message
    assert isinstance(excinfo.value, TianshuError)
    assert excinfo.value.missing == (MissingCapability("tool_calling", None),)


def test_admit_rejects_undeclared_tool_calling():
    model = ProbeModel(_profile(supports_tool_calling=None))
    with pytest.raises(CapabilityMissing) as excinfo:
        admit_model(model)
    assert "未声明" in str(excinfo.value)
    assert excinfo.value.provider == "test"


def test_admit_passes_declared_tool_calling():
    profile = _profile()
    assert admit_model(ProbeModel(profile)) is profile


def test_admit_honours_an_explicit_required_set():
    only_stream = frozenset({"streaming"})
    assert admit_model(
        ProbeModel(_profile(supports_streaming=True)), required=only_stream
    )
    with pytest.raises(CapabilityMissing):
        admit_model(ProbeModel(_profile(supports_streaming=None)), required=only_stream)


def test_context_window_is_checked_only_when_declared():
    """声明了窗口就核验预算；没声明就是"没核验"，不是"核验通过"。"""
    declared = _profile(context_window_bytes=1024)
    with pytest.raises(CapabilityMissing) as excinfo:
        check_context_budget(declared, 2048)
    assert "超出" in str(excinfo.value) and "窗口" in str(excinfo.value)
    check_context_budget(declared, 1024)  # 恰好放得下

    undeclared = _profile()
    check_context_budget(undeclared, 10**9)  # 没声明 → 不核验，也不炸

    assert admit_model(ProbeModel(declared), required_context_bytes=1024) is declared


def test_capability_profile_of_ignores_broken_declarations():
    class Broken:
        def capability_profile(self):  # noqa: ANN202
            return {"tool_calling": True}  # 不是 CapabilityProfile

    assert capability_profile_of(Broken()) is None, "声明坏了按未声明确处理"
    assert capability_profile_of(UnprofiledModel()) is None


def test_shipped_models_declare_tool_calling():
    """仓库里每一个模型实现都要能过准入——包括两个线上 adapter 与三个替身。"""
    for model in (
        ScriptedModel([]),
        RecordedModel([]),
        OpenAICompatibleModel("gpt-4o-mini", api_key="k"),
        AnthropicMessagesModel("claude-sonnet-4-5", api_key="k"),
    ):
        profile = admit_model(model)
        assert profile.supports("tool_calling") is True
        assert profile.supports_thinking is None, "thinking 一律不声明，不猜"

    # 两个线上 adapter 只实现非流式；替身没有流式概念，留未声明
    for adapter in (
        OpenAICompatibleModel("gpt-4o-mini", api_key="k"),
        AnthropicMessagesModel("claude-sonnet-4-5", api_key="k"),
    ):
        assert admit_model(adapter).supports("streaming") is False


def test_check_capabilities_reports_every_missing_item():
    profile = _profile(supports_tool_calling=False)
    with pytest.raises(CapabilityMissing) as excinfo:
        check_capabilities(profile, frozenset({"tool_calling", "streaming"}))
    assert len(excinfo.value.missing) == 2
    assert "tool_calling（声明为不支持）" in str(excinfo.value)
    assert "streaming（未声明）" in str(excinfo.value)


# ---------------------------------------------------------------- 3. loop 准入


def test_missing_capability_costs_nothing(tmp_path):
    """缺必需能力：**模型一次都没被调用**，日志里没有 intent，只有一个 error。"""
    model = ProbeModel(_profile(supports_tool_calling=False), [_end()])
    result, log = _run(tmp_path, model)

    assert result.outcome == "failed"
    assert "模型准入被拒绝" in (result.error or "")
    assert "tool_calling" in (result.error or "")
    assert model.calls == 0, "准入失败时不许调用模型"
    assert result.tokens == 0, "一分钱都没花"
    assert result.turns == 0

    intents = _intents(log, result.run_id)
    assert intents == [], "一次请求都没发出，不该有 model.intent"
    errors = _errors(log, result.run_id)
    assert len(errors) == 1
    assert errors[0].payload["attempt"] == 0, "0 = 还没有任何一次 attempt"
    assert "模型准入被拒绝" in errors[0].payload["error"]
    log.close()


def test_undeclared_capability_also_costs_nothing(tmp_path):
    model = ProbeModel(_profile(supports_tool_calling=None), [_end()])
    result, log = _run(tmp_path, model)

    assert result.outcome == "failed"
    assert "未声明" in (result.error or "")
    assert model.calls == 0
    log.close()


def test_unprofiled_model_costs_nothing(tmp_path):
    model = UnprofiledModel()
    result, log = _run(tmp_path, model)

    assert result.outcome == "failed"
    assert "没有声明能力" in (result.error or "")
    assert model.calls == 0
    log.close()


def test_admission_is_decided_once_per_run(tmp_path):
    """能力判定一次就够：两轮调用只问一次 capability_profile()。"""
    model = ProbeModel(_profile(), [_tool_turn(), _end()])
    result, log = _run(tmp_path, model)

    assert result.state in ("succeeded", "noop"), result.error
    assert model.calls == 2
    assert model.profile_calls == 1, "不是每轮重复判定"
    log.close()


def test_undeclared_window_does_not_block(tmp_path):
    """窗口没声明 → 不核验（旧行为），不是拒绝。与"必需能力"区别对待。"""
    model = ProbeModel(_profile(supports_tool_calling=True), [_end()])
    result, _log = _run(tmp_path, model)
    assert result.state in ("succeeded", "noop"), result.error
    _log.close()


def test_declared_window_smaller_than_budget_is_refused(tmp_path):
    """预算比模型窗口还大 = 配置错误，准入阶段就该挡住。"""
    model = ProbeModel(_profile(context_window_bytes=1024), [_end()])
    result, log = _run(tmp_path, model)
    assert result.outcome == "failed"
    assert "窗口" in (result.error or "")
    assert model.calls == 0
    log.close()


# ---------------------------------------------------------------- 4. 有界重试


def _fast_retry(**overrides: Any) -> Limits:
    base: dict[str, Any] = {"max_turns": 8, "max_total_tokens": 20_000, "retry_backoff_ms": 0}
    base.update(overrides)
    return Limits(**base)


def test_retryable_error_retries_then_succeeds(tmp_path):
    model = ProbeModel(_profile(), [_retryable(), _end()])
    result, log = _run(tmp_path, model, limits=_fast_retry())

    assert result.state in ("succeeded", "noop"), result.error
    assert model.calls == 2, "第一次失败、第二次成功"
    assert [e.payload["attempt"] for e in _intents(log, result.run_id)] == [1, 2]
    assert [e.payload["attempt"] for e in _errors(log, result.run_id)] == [1]
    assert result.turns == 1, "两次 attempt 仍然是一个 turn"

    # 诚实记账：失败那次是"可能花了钱但没记账"的调用，run.end 必须如实标出，
    # 于是预算被标成"不可信"。这不是 bug，是重试的代价被写进了账本。
    end = [e.payload for e in log.read_run(result.run_id) if e.type == "run.end"][0]
    assert end["unbilled_model_intents"] == 1
    assert end["budget_enforced"] is False
    log.close()


def test_non_retryable_error_stops_after_one_attempt(tmp_path):
    model = ProbeModel(_profile(), [_non_retryable(), _end()])
    result, log = _run(tmp_path, model, limits=_fast_retry())

    assert result.outcome == "failed"
    assert "不可重试" in (result.error or "")
    assert "attempt 1/3" in (result.error or "")
    assert model.calls == 1, "不可重试的错误一次就停"
    assert [e.payload["attempt"] for e in _intents(log, result.run_id)] == [1]
    log.close()


def test_three_attempts_then_failure_names_the_attempts(tmp_path):
    model = ProbeModel(_profile(), [_retryable(), _retryable(), _retryable()])
    result, log = _run(tmp_path, model, limits=_fast_retry())

    assert result.outcome == "failed"
    assert model.calls == 3
    error = result.error or ""
    assert "3 次都失败" in error, "重试耗尽必须说清尝试次数"
    assert "HTTP 503" in error, "还要带出最后一次失败的原因"
    log.close()


def test_attempt_numbers_are_auditable(tmp_path):
    """审计的关键：三次尝试在日志里必须是 1,2,3，而不是三个 1。"""
    model = ProbeModel(_profile(), [_retryable(), _retryable(), _retryable()])
    result, log = _run(tmp_path, model, limits=_fast_retry())

    intents = _intents(log, result.run_id)
    assert [e.payload["attempt"] for e in intents] == [1, 2, 3]
    # 同一次逻辑调用的三次尝试共用 request_id，但幂等键逐次不同
    # （每次 attempt 的 payload 不同，同键异参在这里不成立）
    assert len({e.payload["request_id"] for e in intents}) == 1
    assert len({e.idem_key for e in intents}) == 3
    log.close()


def test_retry_backoff_is_exponential_and_injectable(tmp_path):
    sleeper = Sleeper()
    model = ProbeModel(_profile(), [_retryable(), _retryable(), _end()])
    result, _log = _run(
        tmp_path, model, limits=_fast_retry(retry_backoff_ms=500), sleep=sleeper
    )

    assert result.state in ("succeeded", "noop"), result.error
    assert model.calls == 3
    assert sleeper.delays == [0.5, 1.0], "指数退避：500ms、1s（第二次失败后不再等）"
    _log.close()


def test_retry_stops_when_projection_exceeds_budget(tmp_path):
    """失败调用不产生 usage，所以只用账本看不出"再试一次会超预算"。

    这里：第 1 轮花掉 1000 token（上限 1500），第 2 轮的可重试失败若再试一次
    大概率又是 1000 → 投影 2000 > 1500，**不许重试**。
    """
    model = ProbeModel(
        _profile(),
        [
            ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("c1", "read_text", {"path": "note.md"})],
                usage={"input_tokens": 900, "output_tokens": 100},
            ),
            _retryable(),
            _end(),
        ],
    )
    result, log = _run(tmp_path, model, limits=_fast_retry(max_total_tokens=1500))

    assert result.outcome == "failed"
    assert model.calls == 2, "第 1 轮成功 + 第 2 轮只试一次"
    assert "token 预算不足" in (result.error or "")
    assert "不再重试" in (result.error or "")
    assert result.tokens == 1000
    # 失败那一次没有 result：按项目口径它就是"可能花了钱但没记账"的调用
    end = [e.payload for e in log.read_run(result.run_id) if e.type == "run.end"][0]
    assert end["unbilled_model_intents"] == 1
    assert end["budget_enforced"] is False, "有未记账的调用时预算不可信，如实标 False"
    log.close()


def test_retry_keeps_room_when_projection_fits(tmp_path):
    """预算放得下时照常重试：守卫不能因噎废食。"""
    model = ProbeModel(
        _profile(),
        [
            ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("c1", "read_text", {"path": "note.md"})],
                usage={"input_tokens": 100, "output_tokens": 20},
            ),
            _retryable(),
            _end(),
        ],
    )
    result, _log = _run(tmp_path, model, limits=_fast_retry(max_total_tokens=10_000))

    assert result.state in ("succeeded", "noop"), result.error
    assert model.calls == 3, "第 1 轮 + 第 2 轮两次 attempt"
    _log.close()


def test_non_model_error_is_not_retried(tmp_path):
    model = ProbeModel(_profile(), [RuntimeError("不该重试这个"), _end()])
    result, log = _run(tmp_path, model, limits=_fast_retry())

    assert result.outcome == "failed"
    assert "模型调用异常" in (result.error or "")
    assert model.calls == 1
    assert [e.payload["attempt"] for e in _intents(log, result.run_id)] == [1]
    log.close()


def test_retry_delay_is_a_pure_function():
    assert retry_delay_ms(1, 500) == 500
    assert retry_delay_ms(2, 500) == 1000
    assert retry_delay_ms(3, 500) == 2000
    assert retry_delay_ms(1, 0) == 0
    with pytest.raises(ValueError):
        retry_delay_ms(0, 500)
