"""F13 成本可见：价目表查得到才算、未知绝不记 0、累计与展示分开报。

这一组测试钉的是**账目口径**，不是算术花活：

1. 已知价格算得准（输入/输出不同价、整数微美元、四舍五入可预测）；
2. 查不到价格 → `None`，并且**不污染**累计（未知的次数单独计）；
3. 查不到的原因分得清：厂商不明 / 模型不在表里 / usage 缺一半 / 命中未模拟的
   计价档位（长上下文高档、缓存倍率）——一律不算，而不是拿个近似值顶上；
4. `unit_price_ver` 真的出现在事件里，历史日志能回答"当时按哪版价格算的"；
5. 事件里只带身份与结果，不带整张价格表。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tianshu.events import EventLog
from tianshu.loop import Limits, _Progress, run
from tianshu.model import (
    AnthropicMessagesModel,
    ModelIdentity,
    ModelResponse,
    OpenAICompatibleModel,
    ScriptedModel,
    identity_of,
)
from tianshu.pricing import (
    PRICE_SOURCES,
    UNIT_PRICE_VER,
    cost_record,
    estimate_cost,
    format_cost,
    format_usd,
    price_for,
    table_size,
)
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RunCursor

# ---------------------------------------------------------------- 价目表


def test_known_rows_have_expected_rates():
    """抽查几行：微美元/MTok 必须与官方页面上的美元/MTok 对上（×1e6）。"""
    gpt4o_mini = price_for("openai", "gpt-4o-mini")
    assert gpt4o_mini is not None
    assert gpt4o_mini.input_micros_per_mtok == 150_000  # $0.15 / MTok
    assert gpt4o_mini.output_micros_per_mtok == 600_000  # $0.60 / MTok
    assert gpt4o_mini.currency == "USD"
    assert gpt4o_mini.max_input_tokens is None

    sonnet = price_for("anthropic", "claude-sonnet-4-5")
    assert sonnet is not None
    assert sonnet.input_micros_per_mtok == 3_000_000  # $3 / MTok
    assert sonnet.output_micros_per_mtok == 15_000_000  # $15 / MTok

    opus = price_for("anthropic", "claude-opus-5-5")
    assert opus is not None
    assert (opus.input_micros_per_mtok, opus.output_micros_per_mtok) == (
        4_000_000,
        20_000_000,
    )  # $4 / $20


def test_every_row_is_self_consistent():
    """表里每一行都能查到、键与 provider 一致、价格为正、货币是 USD。"""
    assert table_size() > 0
    for provider, model in (
        ("openai", "gpt-4o-mini"),
        ("openai", "gpt-6-luna"),
        ("anthropic", "claude-haiku-5-5"),
        ("anthropic", "claude-opus-5-5"),
    ):
        price = price_for(provider, model)
        assert price is not None
        assert price.provider == provider
        assert price.model == model
        assert price.currency == "USD"
        assert price.input_micros_per_mtok > 0 and price.output_micros_per_mtok > 0
        if price.max_input_tokens is not None:
            assert price.max_input_tokens > 0


def test_table_has_sources_for_every_provider():
    """价格表必须有出处：每个 provider 都登记了官方页面与抓取日期。"""
    assert set(PRICE_SOURCES) == {"openai", "anthropic"}
    for url in PRICE_SOURCES.values():
        assert url.startswith("https://")
    assert UNIT_PRICE_VER == "2026-11-09", "改任何一行数字都必须改这个版本号"


def test_price_for_unknown_is_none_not_zero():
    assert price_for("openai", "gpt-9-ultra") is None
    assert price_for("nope", "gpt-4o-mini") is None
    assert price_for(None, "gpt-4o-mini") is None
    assert price_for("openai", None) is None
    assert price_for("", "") is None


def test_price_lookup_does_not_guess_similar_names():
    """不做前缀匹配、不做"同系列应该差不多"：查不到就是查不到。"""
    assert price_for("anthropic", "claude-sonnet-4") is None, "4 与 4.5 不是一个价"
    assert price_for("openai", "gpt-4o-mini-typo") is None
    assert price_for("openai", "gpt-4o-2024") is None, "日期后缀必须完整才归一化"
    assert price_for("anthropic", "gpt-4o-mini") is None, "厂商与模型对不上也不查"


def test_snapshot_suffix_maps_to_base_model():
    """带日期快照的模型 id 与基础名同价（官方价目表就是这么组织的）。"""
    base = price_for("openai", "gpt-4o-mini")
    dated = price_for("openai", "gpt-4o-mini-2024-07-18")
    assert dated == base
    assert price_for("anthropic", "claude-sonnet-4-5-20250929") == price_for(
        "anthropic", "claude-sonnet-4-5"
    )


# ---------------------------------------------------------------- 费用计算


def test_known_cost_uses_separate_input_output_rates():
    """输入/输出不同价：1000 in + 500 out @ $0.15/$0.60 = $0.000450。"""
    cost = estimate_cost(
        {"input_tokens": 1000, "output_tokens": 500},
        price_for("openai", "gpt-4o-mini"),
    )
    assert cost is not None
    assert cost.micros == 450
    assert cost.input_micros == 150
    assert cost.output_micros == 300
    assert cost.currency == "USD"
    assert cost.unit_price_ver == UNIT_PRICE_VER
    assert isinstance(cost.micros, int), "金额必须是整数微美元，不许浮点"


def test_unknown_price_yields_none_cost():
    assert estimate_cost({"input_tokens": 1, "output_tokens": 1}, None) is None


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": None, "output_tokens": 10},
        {"input_tokens": 10, "output_tokens": None},
        {"input_tokens": 10},
        {"input_tokens": -1, "output_tokens": 10},
        {"input_tokens": True, "output_tokens": 10},
        None,
    ],
)
def test_usage_missing_a_side_is_unknown_not_partial(usage):
    """缺一半 token 数：不做"部分估计"，也不崩。半个成本不是成本。"""
    assert estimate_cost(usage, price_for("openai", "gpt-4o-mini")) is None


def test_rounding_is_half_up_and_deterministic():
    price = price_for("openai", "gpt-4o-mini")  # 150_000 微美元/MTok
    # 3 token → 0.45 微美元 → 0；4 token → 0.60 微美元 → 1
    assert estimate_cost({"input_tokens": 3, "output_tokens": 0}, price).micros == 0
    assert estimate_cost({"input_tokens": 4, "output_tokens": 0}, price).micros == 1
    # 同一输入算两次必须一样（整数运算，不受浮点影响）
    a = estimate_cost({"input_tokens": 12345, "output_tokens": 6789}, price)
    b = estimate_cost({"input_tokens": 12345, "output_tokens": 6789}, price)
    assert a == b


def test_long_context_tier_is_unknown_not_underpriced():
    """分档价的另一档没登记：超过阈值就报未知，不拿低档价少报。"""
    price = price_for("openai", "gpt-6-luna")
    assert price is not None and price.max_input_tokens == 272_000
    at_limit = estimate_cost(
        {"input_tokens": 272_000, "output_tokens": 10}, price
    )
    assert at_limit is not None
    assert estimate_cost({"input_tokens": 272_001, "output_tokens": 10}, price) is None

    haiku = price_for("anthropic", "claude-haiku-5-5")
    assert haiku is not None and haiku.max_input_tokens == 100_000
    assert estimate_cost({"input_tokens": 100_000, "output_tokens": 1}, haiku)
    assert estimate_cost({"input_tokens": 100_001, "output_tokens": 1}, haiku) is None


def test_cache_tokens_make_cost_unknown():
    """本表不模拟缓存倍率：只要真的出现缓存 token，这次就报未知。"""
    price = price_for("openai", "gpt-4o-mini")
    base = {"input_tokens": 100, "output_tokens": 10}
    assert estimate_cost(base, price) is not None
    assert estimate_cost({**base, "cache_read_input_tokens": 0}, price) is not None
    assert estimate_cost({**base, "cache_read_input_tokens": 50}, price) is None
    assert estimate_cost({**base, "cache_creation_input_tokens": 50}, price) is None


# ---------------------------------------------------------------- 成本记录


def test_record_shape_carries_identity_and_result_only():
    record = cost_record(
        provider="openai",
        model="gpt-4o-mini",
        usage={"input_tokens": 1000, "output_tokens": 500},
        adapter="openai-compatible",
    )
    payload = record.to_payload()

    assert record.known
    assert payload["provider"] == "openai"
    assert payload["adapter"] == "openai-compatible"
    assert payload["model"] == "gpt-4o-mini"
    assert payload["units"] == {"input_tokens": 1000, "output_tokens": 500}
    assert payload["unit_price_ver"] == UNIT_PRICE_VER
    assert payload["cost"] == 450
    assert payload["currency"] == "USD"
    assert set(payload) == {
        "provider",
        "adapter",
        "model",
        "units",
        "unit_price_ver",
        "cost",
        "currency",
    }


def test_record_unknown_price_keeps_what_it_knows():
    """厂商已知、模型没价：provider 照写，cost/单价版本留空。"""
    record = cost_record(
        provider="openai",
        model="gpt-9-ultra",
        usage={"input_tokens": 10, "output_tokens": 5},
        adapter="openai-compatible",
    )
    assert not record.known
    assert record.cost is None
    assert record.unit_price_ver is None
    assert record.currency is None
    assert record.provider == "openai"
    assert record.to_payload()["cost"] is None


def test_record_without_vendor_is_unknown():
    record = cost_record(provider=None, model="gpt-4o-mini", usage={})
    assert record.cost is None
    assert record.provider is None
    assert record.units == {"input_tokens": None, "output_tokens": None}


def test_record_payload_never_embeds_the_price_table():
    """payload 大小纪律：每轮事件只带身份与结果。"""
    record = cost_record(
        provider="anthropic",
        model="claude-sonnet-4-5",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    raw = json.dumps(record.to_payload(), ensure_ascii=False)
    assert "input_micros_per_mtok" not in raw
    assert "output_micros_per_mtok" not in raw
    assert len(raw.encode("utf-8")) < 320, "这条记录会进每一轮的 model.result"


# ---------------------------------------------------------------- 展示


def test_format_usd_is_exact():
    assert format_usd(0) == "$0.000000"
    assert format_usd(450) == "$0.000450"
    assert format_usd(1_500_000) == "$1.500000"
    assert format_usd(12_345_678) == "$12.345678"
    assert format_usd(-450) == "-$0.000450"


def test_format_cost_separates_known_from_unknown():
    # 没有调用：说"无"，不说 0
    assert format_cost(0, 0, 0) == "无（没有模型调用）"
    # 全部已知
    assert format_cost(450, 0, 1) == "$0.000450 USD（1 次调用）"
    assert format_cost(900, 0, 2) == "$0.000900 USD（2 次调用）"
    # 全部未知：绝不显示 0
    assert format_cost(0, 2, 2) == "未知（2 次调用查不到价格）"
    assert "0.000000" not in format_cost(0, 2, 2)
    # 一半一半
    mixed = format_cost(450, 1, 2)
    assert "$0.000450 USD（1 次调用）" in mixed
    assert "未知（1 次调用）" in mixed


# ---------------------------------------------------------------- 模型身份


def test_official_endpoint_is_priced():
    model = OpenAICompatibleModel("gpt-4o-mini", api_key="k")
    identity = identity_of(model)
    assert identity == ModelIdentity(
        adapter="openai-compatible", vendor="openai", model="gpt-4o-mini"
    )

    claude = AnthropicMessagesModel("claude-sonnet-4-5", api_key="k")
    assert identity_of(claude) == ModelIdentity(
        adapter="anthropic", vendor="anthropic", model="claude-sonnet-4-5"
    )


def test_custom_gateway_is_not_priced():
    """协议 ≠ 厂商：代理站/自建服务的价格由对方定，我们不认官价表。"""
    proxy = OpenAICompatibleModel(
        "gpt-4o-mini", base_url="https://proxy.example.com/v1", api_key="k"
    )
    identity = identity_of(proxy)
    assert identity is not None
    assert identity.vendor is None, "非官方端点不许套官方价"
    assert identity.adapter == "openai-compatible"
    assert identity.model == "gpt-4o-mini"


def test_http_and_lookalike_hosts_are_not_official():
    plain = OpenAICompatibleModel(
        "gpt-4o-mini", base_url="http://api.openai.com/v1", api_key="k"
    )
    assert identity_of(plain).vendor is None, "明文端点不算官方结算"

    lookalike = OpenAICompatibleModel(
        "gpt-4o-mini", base_url="https://api.openai.com.evil.example/v1", api_key="k"
    )
    assert identity_of(lookalike).vendor is None, "后缀陷阱不算官方"


def test_identity_of_object_without_capability_is_none():
    """没有身份能力的模型（测试替身、录制回放）→ 未知，不猜。"""
    assert identity_of(object()) is None
    assert identity_of(ScriptedModel([])) == ModelIdentity(
        adapter="scripted", vendor=None, model=""
    )


# ---------------------------------------------------------------- loop 接线


class _Transport:
    """按脚本返回响应。不联网。"""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    def post(self, url, *, body, headers, timeout):  # noqa: ANN001
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        return self._payloads.pop(0)


def _openai_payload(
    *, usage: dict[str, int] | None = None, tool: str | None = None, text: str = ""
) -> dict[str, Any]:
    message: dict[str, Any] = {"content": text}
    if tool is not None:
        message["tool_calls"] = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": tool, "arguments": '{"path": "note.md"}'},
            }
        ]
    return {
        "choices": [
            {
                "message": message,
                "finish_reason": "tool_calls" if tool else "stop",
            }
        ],
        "usage": usage if usage is not None else {"prompt_tokens": 1000, "completion_tokens": 500},
    }


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text("原始内容\n", encoding="utf-8")
    return ws


def _run(tmp_path: Path, ws: Path, model, *, limits: Limits | None = None):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    result = run(
        task="整理目录里的笔记",
        workspace_dir=ws,
        out_name="summary.md",
        model=model,
        log=log,
        limits=limits,
        snapshots=SnapshotStore(tmp_path / "artifacts"),
    )
    return result, log


def _cost_of(ev) -> dict[str, Any]:
    return ev.payload["cost"]


def test_scripted_run_reports_unknown_cost_not_zero(tmp_path):
    """没有价格的模型照样能跑：事件里 cost 是 null，汇总里是"未知次数"。"""
    ws = _workspace(tmp_path)
    result, log = _run(tmp_path, ws, ScriptedModel([ModelResponse("end_turn", content="完成")]))

    assert result.state in ("succeeded", "noop"), result.error
    assert result.tokens == 0 and result.unknown_usage_calls == 1
    assert result.cost_total_micros == 0
    assert result.unknown_cost_calls == 1, "费用未知的调用必须单独计数"

    results = [e for e in log.read_run(result.run_id) if e.type == "model.result"]
    assert len(results) == 1
    cost = _cost_of(results[0])
    assert cost["provider"] is None
    assert cost["adapter"] == "scripted"
    assert cost["cost"] is None, "查不到价格就是 null，不是 0"
    assert cost["unit_price_ver"] is None
    assert results[0].payload["usage"]["input_tokens"] is None, "scripted 不给 usage"

    # CLI 那一行也必须是"未知"，不是"$0.000000"
    shown = format_cost(result.cost_total_micros, result.unknown_cost_calls, result.turns)
    assert shown == "未知（1 次调用查不到价格）"
    log.close()


def test_priced_run_accumulates_known_cost(tmp_path):
    """官方端点 + 已知模型：每轮算得出钱，两轮累计正确。"""
    ws = _workspace(tmp_path)
    transport = _Transport(
        [
            _openai_payload(tool="read_text"),
            _openai_payload(text="完成"),
        ]
    )
    model = OpenAICompatibleModel(
        "gpt-4o-mini", base_url="https://api.openai.com/v1", api_key="k", transport=transport
    )
    result, log = _run(tmp_path, ws, model)

    assert result.state in ("succeeded", "noop"), result.error
    assert result.turns == 2
    # 每轮 1000 in + 500 out @ $0.15/$0.60 = 450 微美元
    assert result.cost_total_micros == 900
    assert result.unknown_cost_calls == 0
    assert result.tokens == 3000

    results = [e for e in log.read_run(result.run_id) if e.type == "model.result"]
    assert len(results) == 2
    for event in results:
        cost = _cost_of(event)
        assert cost["provider"] == "openai"
        assert cost["adapter"] == "openai-compatible"
        assert cost["model"] == "gpt-4o-mini"
        assert cost["unit_price_ver"] == UNIT_PRICE_VER, "事件里必须能查到价格版本"
        assert cost["cost"] == 450
        assert cost["units"] == {"input_tokens": 1000, "output_tokens": 500}
    assert format_cost(result.cost_total_micros, result.unknown_cost_calls, result.turns) == (
        "$0.000900 USD（2 次调用）"
    )
    log.close()


def test_run_mixes_known_and_unknown_calls(tmp_path):
    """同一 run 里既有可能计价、又可能不知道价格：两个数分开报。"""
    ws = _workspace(tmp_path)

    class _Unknown(OpenAICompatibleModel):
        """沿用官方端点身份，但模型名不在表里。"""

        def __init__(self, transport) -> None:  # noqa: ANN001
            super().__init__(
                "gpt-9-ultra",
                base_url="https://api.openai.com/v1",
                api_key="k",
                transport=transport,
            )

    transport = _Transport([_openai_payload(text="完成")])
    result, log = _run(tmp_path, ws, _Unknown(transport))

    assert result.cost_total_micros == 0
    assert result.unknown_cost_calls == 1
    cost = _cost_of([e for e in log.read_run(result.run_id) if e.type == "model.result"][0])
    assert cost["provider"] == "openai", "厂商知名，只是这个模型没价"
    assert cost["cost"] is None
    log.close()


def test_custom_gateway_run_is_unknown_cost(tmp_path):
    """把基址指向代理站：能跑、能算 token，但费用一律未知。"""
    ws = _workspace(tmp_path)
    transport = _Transport([_openai_payload(text="完成")])
    model = OpenAICompatibleModel(
        "gpt-4o-mini", base_url="https://proxy.example.com/v1", api_key="k", transport=transport
    )
    result, log = _run(tmp_path, ws, model)

    assert result.tokens == 1500, "token 照记"
    assert result.cost_total_micros == 0
    assert result.unknown_cost_calls == 1
    cost = _cost_of([e for e in log.read_run(result.run_id) if e.type == "model.result"][0])
    assert cost["provider"] is None
    assert cost["adapter"] == "openai-compatible"
    assert cost["cost"] is None
    log.close()


def test_missing_usage_marks_cost_unknown(tmp_path):
    """provider 没给 usage：token 未知 → 费用必然未知（而不是 0）。"""
    ws = _workspace(tmp_path)
    transport = _Transport(
        [{"choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}]}]
    )
    model = OpenAICompatibleModel(
        "gpt-4o-mini", base_url="https://api.openai.com/v1", api_key="k", transport=transport
    )
    result, log = _run(tmp_path, ws, model)

    assert result.unknown_usage_calls == 1
    assert result.cost_total_micros == 0
    assert result.unknown_cost_calls == 1
    cost = _cost_of([e for e in log.read_run(result.run_id) if e.type == "model.result"][0])
    assert cost["cost"] is None
    assert cost["provider"] == "openai", "身份仍然要记清楚"
    log.close()


def test_cost_survives_cursor_round_trip():
    """费用累计跟着游标走：resume 之后不会把暂停前花的钱丢掉。"""
    progress = _Progress(
        messages=[{"role": "user", "content": "x"}],
        turns=2,
        tokens=3000,
        cost_total_micros=900,
        unknown_cost_calls=1,
    )
    restored = _Progress.of(RunCursor.of(progress.to_cursor().to_dict()))

    assert restored.cost_total_micros == 900
    assert restored.unknown_cost_calls == 1
    # 旧游标（没有这两个键）→ 0 次未知、0 已知，不是崩
    legacy = _Progress.of(RunCursor.of({"messages": [], "budget_ledger": {"turns": 1}}))
    assert legacy.cost_total_micros == 0
    assert legacy.unknown_cost_calls == 0


# ---------------------------------------------------------------- CLI 展示


def _cli(*argv: str) -> tuple[int, str]:
    """直接调 CLI 入口：同一条解析 + 装配路径，不起子进程。"""
    import contextlib
    import io

    from tianshu.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue() + err.getvalue()


def _cli_run(tmp_path: Path) -> tuple[int, str, str]:
    """跑一次 scripted run，返回 (exit_code, 输出, run_id)。"""
    ws = tmp_path / "cli-ws"
    ws.mkdir()
    (ws / "note.md").write_text("原始内容\n", encoding="utf-8")
    db = str(tmp_path / "cli.db")
    code, out = _cli(
        "run",
        "--task",
        "整理目录里的笔记",
        "--workspace",
        str(ws),
        "--out",
        "summary.md",
        "--db",
        db,
        "--data-root",
        str(tmp_path / "cli-authority"),
        "--model",
        "scripted",
    )
    run_id = next(
        line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("run_id")
    )
    return code, out, run_id


def test_cli_run_prints_unknown_cost_not_zero(tmp_path):
    """scripted 模型没有价格：输出里是"费用未知（N 次）"，不许出现 $0.000000。"""
    code, out, _ = _cli_run(tmp_path)

    assert code == 0, out
    assert "turns  : 3" in out, out
    assert "费用   : 未知（3 次调用查不到价格）" in out, out
    assert "$0.000000" not in out, "未知不许显示成 0 元"


def test_cli_status_reports_cost_fields(tmp_path):
    """`run status` 也能看到已经烧了多少（账来自游标的预算账本）。"""
    _, _, run_id = _cli_run(tmp_path)
    code, out = _cli(
        "run",
        "status",
        "--run-id",
        run_id,
        "--db",
        str(tmp_path / "cli.db"),
        "--json",
    )
    assert code == 0, out
    payload = json.loads(out)

    assert payload["cost_total_micros"] == 0
    assert payload["unknown_cost_calls"] == 3
