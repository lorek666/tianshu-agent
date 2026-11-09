"""F13 成本可见：把 token 用量换算成钱，并且**只报能核对的钱**。

PRD §7 F13 的理由只有一句：「用户自己出钱，必须让他知道烧了多少」。所以这个模块
的失败模式不是"算不出来"，而是"算出来一个看起来合理的错数"。三条硬规则：

1. **未知价格是 `None`，绝不是 `0`。**
   `0` 是一个断言（"这次没花钱"），`None` 是一个事实（"我不知道"）。两者在界面上
   长得像，在账上完全不同。所以：厂商不明、模型不在表里、usage 缺一半、命中本表
   不模拟的计价档位——一律 `None`，并且由调用方单独累计"费用未知的调用次数"。
2. **不猜价格。** 表里查不到就报未知，不做前缀匹配、不做"同系列应该差不多"的推断、
   不拿低档价冒充高档价。价格是别人定的，猜出来的数字比没有更糟：它会被当真。
3. **每一行都要有出处。** 见下面「价目表」注释：来源页面、抓取日期、原始写法。
   表里没有的数字，宁可留空。

## 计价模型（以及它不覆盖什么）

- 单位是**每百万 token 的微美元**（`micro-USD / MTok`，1 微美元 = 1e-6 USD）。
  用整数是为了可复现：金额用浮点存进事件，换台机器就可能差一位。所有官方价目都能
  写成 4 位小数以内的 USD/MTok，乘以 1e6 就是整数。
- 输入与输出分开计价（两家供应商都这么算），相加得到一次调用的费用。
- 一次调用**必须两边 token 数都已知**才能算出金额。只有一边时不做"部分估计"：
  半个成本不是成本。
- **不模拟**：缓存读写的倍率（OpenAI cached input / Anthropic cache read+write）、
  Batch/Flex/Fast 等折扣档、长上下文分档（OpenAI >272K、Haiku 5.5 >100K 的更高
  档）、图片/音频/工具调用等非 token 计费。命中这些情况时返回 `None`（未知），
  而不是拿标准价硬算——见 `estimate_cost` 的分档守卫。
- 只对**官方端点**认价：`openai-compatible` / `anthropic` 协议可以指向任何网关
  （代理站、自建 vLLM、Bedrock 转发），那些地方的价格由对方定，本表不适用。
  厂商身份由 `model.price_identity()` 给出；端点不是官方主机时它返回
  `vendor=None`，本模块据此报未知。

## 价目表怎么更新

- `UNIT_PRICE_VER` 是**整张表的版本**，格式 `YYYY-MM-DD` = 这一版数字的核对日期。
  它出现在每条 `model.result` 的 `cost` 记录里，因此历史日志永远能说清"当时按哪一
  版价格算的"。**任何一行数字变动都必须改这个日期**，不允许悄悄改数字。
- 更新流程：打开下面 `PRICE_SOURCES` 里的官方页面 → 对照每行注释里的原始写法 →
  改数字 → 改 `UNIT_PRICE_VER` → 跑 `tests/test_pricing.py`。
- 责任人：维护这张表的是**发布者本人**（本仓库没有自动抓取，也不许联网取价——
  PRD §7.3 零依赖）。发现价格页改了而表没改，就应当把对应行删掉（未知优于错数），
  而不是继续用一个过期的数字。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 价目表版本 = 这一版数字的核对日期。改数字必须改它（见模块注释）。
UNIT_PRICE_VER = "2026-11-09"

#: 每一行价格的出处。表里没有第二手来源。
PRICE_SOURCES: dict[str, str] = {
    "openai": (
        "https://developers.openai.com/api/docs/pricing"
        "（Standard 档，per 1M tokens；抓取于 2026-11-09）"
    ),
    "anthropic": (
        "https://platform.claude.com/docs/en/about-claude/pricing"
        "（Model pricing，per MTok；抓取于 2026-11-09）"
    ),
}

#: 1 美元 = 1_000_000 微美元。
MICROS_PER_USD = 1_000_000
#: 价目表的计价规模：每百万 token。
TOKENS_PER_MTok = 1_000_000


@dataclass(frozen=True)
class UnitPrice:
    """一行价格：区分输入/输出单价，并记住它只在哪一档输入规模内成立。"""

    provider: str
    model: str
    #: 每百万输入 token 的微美元。
    input_micros_per_mtok: int
    #: 每百万输出 token 的微美元。
    output_micros_per_mtok: int
    currency: str = "USD"
    #: 该行只在这个输入 token 数以内成立（官方按上下文分档时的低档）。
    #: 超过它就返回未知——拿低档价算高档请求是**少报**，比不报危险。
    max_input_tokens: int | None = None


@dataclass(frozen=True)
class Cost:
    """一次调用的费用。金额是整数微美元，可精确复现。"""

    currency: str
    micros: int
    input_micros: int
    output_micros: int
    unit_price_ver: str = UNIT_PRICE_VER


@dataclass(frozen=True)
class CostRecord:
    """写进 `model.result` 的那份成本记录（PRD §7 F13 的五个键 + 两个补充）。

    `cost is None` 表示"这次调用的费用未知"，不是"免费"。调用方必须按
    `known` 分流累计，不能把 `None` 当 0 加进去。
    """

    provider: str | None
    adapter: str
    model: str | None
    units: dict[str, int | None] = field(default_factory=dict)
    unit_price_ver: str | None = None
    cost: int | None = None
    currency: str | None = None

    @property
    def known(self) -> bool:
        return self.cost is not None

    def to_payload(self) -> dict[str, Any]:
        """事件 payload 里的形状。**只带身份与结果，不带整张价格表。**"""
        return {
            "provider": self.provider,
            "adapter": self.adapter,
            "model": self.model,
            "units": {
                "input_tokens": self.units.get("input_tokens"),
                "output_tokens": self.units.get("output_tokens"),
            },
            "unit_price_ver": self.unit_price_ver,
            "cost": self.cost,
            "currency": self.currency,
        }


# ---------------------------------------------------------------- 价目表


def _row(model: str, input_micros: int, output_micros: int, **kwargs: Any) -> UnitPrice:
    return UnitPrice(
        provider="openai",
        model=model,
        input_micros_per_mtok=input_micros,
        output_micros_per_mtok=output_micros,
        **kwargs,
    )


def _claude(model: str, input_micros: int, output_micros: int, **kwargs: Any) -> UnitPrice:
    return UnitPrice(
        provider="anthropic",
        model=model,
        input_micros_per_mtok=input_micros,
        output_micros_per_mtok=output_micros,
        **kwargs,
    )


#: (provider, model) → UnitPrice。**精确匹配**，不做前缀/模糊匹配。
#:
#: 每行末尾注释是官方页面上的原始写法，方便人工复核。美元 → 微美元/MTok 乘 1e6
#: （如 $0.15/MTok = 150_000）。
_TABLE: dict[tuple[str, str], UnitPrice] = {
    # ---- OpenAI（Standard 档，per 1M tokens；来源见 PRICE_SOURCES["openai"]）----
    ("openai", "gpt-4o-mini"): _row("gpt-4o-mini", 150_000, 600_000),  # $0.15 / $0.60
    ("openai", "gpt-4o"): _row("gpt-4o", 2_500_000, 10_000_000),  # $2.50 / $10.00
    ("openai", "gpt-4.1"): _row("gpt-4.1", 2_000_000, 8_000_000),  # $2.00 / $8.00
    ("openai", "gpt-4.1-mini"): _row("gpt-4.1-mini", 400_000, 1_600_000),  # $0.40 / $1.60
    ("openai", "gpt-5"): _row("gpt-5", 1_250_000, 10_000_000),  # $1.25 / $10.00
    ("openai", "gpt-5-mini"): _row("gpt-5-mini", 250_000, 2_000_000),  # $0.25 / $2.00
    ("openai", "gpt-5-nano"): _row("gpt-5-nano", 50_000, 400_000),  # $0.05 / $0.40
    ("openai", "gpt-5.1"): _row("gpt-5.1", 1_250_000, 10_000_000),  # $1.25 / $10.00
    ("openai", "gpt-5.2"): _row("gpt-5.2", 1_750_000, 14_000_000),  # $1.75 / $14.00
    ("openai", "gpt-5.3-codex"): _row("gpt-5.3-codex", 1_750_000, 14_000_000),
    ("openai", "gpt-5.4-mini"): _row("gpt-5.4-mini", 750_000, 4_500_000),  # $0.75 / $4.50
    ("openai", "gpt-5.4-nano"): _row("gpt-5.4-nano", 200_000, 1_250_000),  # $0.20 / $1.25
    # 下面四行官方标的是「Short context（≤272K input tokens）」低档价。超过 272K
    # 另有长上下文价（页面同时给出），本表没登记 → 守卫报未知。
    ("openai", "gpt-6-luna"): _row(
        "gpt-6-luna", 100_000, 500_000, max_input_tokens=272_000
    ),  # $0.10 / $0.50
    ("openai", "gpt-6.1-terra"): _row(
        "gpt-6.1-terra", 1_000_000, 5_000_000, max_input_tokens=272_000
    ),  # $1.00 / $5.00
    ("openai", "gpt-6.1-sol"): _row(
        "gpt-6.1-sol", 2_000_000, 10_000_000, max_input_tokens=272_000
    ),  # $2.00 / $10.00
    ("openai", "gpt-6-astra"): _row(
        "gpt-6-astra", 10_000_000, 50_000_000, max_input_tokens=272_000
    ),  # $10.00 / $50.00
    # ---- Anthropic（per MTok；来源见 PRICE_SOURCES["anthropic"]）----
    # 表键是**请求里实际传的模型 id**，与官方页面上的产品名一一对应（注释里给名字）。
    ("anthropic", "claude-sonnet-4-5"): _claude(
        "claude-sonnet-4-5", 3_000_000, 15_000_000
    ),  # Claude Sonnet 4.5：$3 / $15
    ("anthropic", "claude-sonnet-4-6"): _claude(
        "claude-sonnet-4-6", 3_000_000, 15_000_000
    ),  # Claude Sonnet 4.6：$3 / $15
    ("anthropic", "claude-sonnet-5"): _claude(
        "claude-sonnet-5", 2_000_000, 10_000_000
    ),  # Claude Sonnet 5：$2 / $10
    ("anthropic", "claude-haiku-4-5"): _claude(
        "claude-haiku-4-5", 1_000_000, 5_000_000
    ),  # Claude Haiku 4.5：$1 / $5
    # Haiku 5.5 官方按 prompt 长度分档：≤100K 是 $0.10/$0.50，>100K 是 $0.50/$2.50。
    # 只登记低档 + 守卫，超过 100K 报未知（不拿低档价冒充）。
    ("anthropic", "claude-haiku-5-5"): _claude(
        "claude-haiku-5-5", 100_000, 500_000, max_input_tokens=100_000
    ),  # Claude Haiku 5.5（≤100K）：$0.10 / $0.50
    ("anthropic", "claude-opus-4-5"): _claude(
        "claude-opus-4-5", 5_000_000, 25_000_000
    ),  # Claude Opus 4.5：$5 / $25
    ("anthropic", "claude-opus-4-6"): _claude(
        "claude-opus-4-6", 5_000_000, 25_000_000
    ),  # Claude Opus 4.6：$5 / $25
    ("anthropic", "claude-opus-4-7"): _claude(
        "claude-opus-4-7", 5_000_000, 25_000_000
    ),  # Claude Opus 4.7：$5 / $25
    ("anthropic", "claude-opus-4-8"): _claude(
        "claude-opus-4-8", 5_000_000, 25_000_000
    ),  # Claude Opus 4.8：$5 / $25
    ("anthropic", "claude-opus-5"): _claude(
        "claude-opus-5", 5_000_000, 25_000_000
    ),  # Claude Opus 5：$5 / $25
    ("anthropic", "claude-opus-5-5"): _claude(
        "claude-opus-5-5", 4_000_000, 20_000_000
    ),  # Claude Opus 5.5：$4 / $20
}

#: 快照后缀：`-20250929` 或 `-2024-08-06`。同一个模型的日期快照与基础名同价，
#: 这是官方价目表的组织方式（快照不单独定价），不是我们"猜同一系列差不多"。
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def price_for(provider: str | None, model: str | None) -> UnitPrice | None:
    """查一行价格。查不到就是 `None`——不猜、不兜底、不用别的模型顶替。"""
    if not provider or not model:
        return None
    key = (provider.strip().lower(), model.strip())
    price = _TABLE.get(key)
    if price is not None:
        return price
    # 带日期快照后缀时去掉后缀再查一次；只在原样查不到时才做，且只去一次。
    canonical = _SNAPSHOT_SUFFIX.sub("", key[1])
    if canonical != key[1]:
        return _TABLE.get((key[0], canonical))
    return None


def estimate_cost(
    usage: Mapping[str, Any] | None, price: UnitPrice | None
) -> Cost | None:
    """一次调用的费用；算不出来就 `None`。

    "算不出来"包括：没查到价格、token 数缺一半、负数、命中本表不模拟的计价档位
    （长上下文高档、缓存读写）。**任何一种都不返回部分金额或 0。**
    """
    if price is None or not isinstance(usage, Mapping):
        return None

    input_tokens = _token_count(usage.get("input_tokens"))
    output_tokens = _token_count(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        # 只有一半 token 数时不做"部分估计"：半个成本不是成本。
        return None
    if price.max_input_tokens is not None and input_tokens > price.max_input_tokens:
        # 官方对这一档另有价目，本表没有 → 未知。拿低档价算是少报。
        return None
    # 缓存读写有各自的倍率（OpenAI cached input / Anthropic cache read+write），
    # 本表不模拟；只要真的出现了缓存 token，这次调用就报未知。
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        cached = _token_count(usage.get(key))
        if cached:
            return None

    input_micros = _scale(input_tokens, price.input_micros_per_mtok)
    output_micros = _scale(output_tokens, price.output_micros_per_mtok)
    return Cost(
        currency=price.currency,
        micros=input_micros + output_micros,
        input_micros=input_micros,
        output_micros=output_micros,
        unit_price_ver=UNIT_PRICE_VER,
    )


def cost_record(
    *,
    provider: str | None,
    model: str | None,
    usage: Mapping[str, Any] | None,
    adapter: str = "",
) -> CostRecord:
    """把"谁是厂商、哪个模型、用了多少"变成一条可落库的成本记录。

    `provider` / `model` 是**这次请求的身份**，不是查表结果：厂商知名、模型不知价时
    记录里照样写厂商（读者能看出"知道是谁，但没这个价"）。
    """
    price = price_for(provider, model)
    cost = estimate_cost(usage, price)
    units = {
        "input_tokens": _token_count((usage or {}).get("input_tokens"))
        if isinstance(usage, Mapping)
        else None,
        "output_tokens": _token_count((usage or {}).get("output_tokens"))
        if isinstance(usage, Mapping)
        else None,
    }
    return CostRecord(
        provider=provider or None,
        adapter=adapter,
        model=model or None,
        units=units,
        unit_price_ver=cost.unit_price_ver if cost is not None else None,
        cost=None if cost is None else cost.micros,
        currency=cost.currency if cost is not None else None,
    )


# ---------------------------------------------------------------- 展示


def format_usd(micros: int) -> str:
    """微美元 → `$1.234567`。整数除余，不经过浮点，六个小数位就是精度上限。"""
    sign = "-" if micros < 0 else ""
    whole, frac = divmod(abs(int(micros)), MICROS_PER_USD)
    return f"{sign}${whole}.{frac:06d}"


def format_cost(cost_total_micros: int, unknown_cost_calls: int, calls: int) -> str:
    """给人看的一行费用。**"花了 0 元"和"不知道花了多少"永远长得不一样。**"""
    if calls <= 0:
        return "无（没有模型调用）"
    known_calls = max(0, calls - unknown_cost_calls)
    if unknown_cost_calls <= 0:
        return f"{format_usd(cost_total_micros)} USD（{known_calls} 次调用）"
    if known_calls <= 0:
        return f"未知（{unknown_cost_calls} 次调用查不到价格）"
    return (
        f"{format_usd(cost_total_micros)} USD（{known_calls} 次调用）"
        f" + 未知（{unknown_cost_calls} 次调用）"
    )


# ---------------------------------------------------------------- 内部


def _token_count(value: Any) -> int | None:
    """非负整数才算有效 token 数；`True` 不算 1，负数是坏数据。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _scale(tokens: int, micros_per_mtok: int) -> int:
    """`tokens × 单价 / 1e6`，四舍五入到整数微美元。

    全程整数运算：同样的输入在任何机器上得到同一个数（浮点在这里会引入
    "同一笔账算两次不一样"的风险，而账最不该有这种性质）。
    """
    numerator = tokens * micros_per_mtok
    return (numerator + TOKENS_PER_MTok // 2) // TOKENS_PER_MTok


def table_size() -> int:
    """价目表行数。给"这张表有多大"这类体检用，不暴露整张表。"""
    return len(_TABLE)


__all__ = [
    "MICROS_PER_USD",
    "PRICE_SOURCES",
    "TOKENS_PER_MTok",
    "UNIT_PRICE_VER",
    "Cost",
    "CostRecord",
    "UnitPrice",
    "cost_record",
    "estimate_cost",
    "format_cost",
    "format_usd",
    "price_for",
    "table_size",
]
