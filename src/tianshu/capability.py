"""F9a 模型准入：先问"这个模型能不能干这件事"，再问"它答了什么"。

PRD §7 F9a 在文档里出现了三次（CapabilityProfile 登记、必需能力缺失即拒绝、
有界重试默认 3 次 attempt），代码里此前一次都没有。这个模块只做**判定**：
不建连接、不调模型、不认识任何 provider 的线上格式（那是 `model.py` 的事）。

## 口径一：未声明 = 不支持（fail closed）

能力字段的缺省值是 `None`（未声明），**不是** `False`，也不是 `True`：

- `True`：声明具备。只有它算"支持"。
- `False`：声明不具备。
- `None`：没声明——我们不知道。

判定只认 `True`。所以"没声明"与"声明为不支持"在**结论**上一样（都拒绝），
但在**错误信息**上必须分开：一个是"这份声明没写"，一个是"写了，做不到"。
把两者混成一句话，运维就只能猜是模型不行还是配置漏了。

这不是悲观，是 F2 已经定下的同一种口径（`execpolicy.ExecBackendCapabilities`：
"未声明 = 不支持"，绝不降级）。理由在那边也成立：**乐观默认的代价是不可见的**。
一个能力声明漏写的模型被放行后，失败会以"模型答得不对"的形式出现在几十轮之后，
而不是以"准入被拒绝"的形式出现在第 0 轮。后者一行日志就能定位，前者要复盘整条链。

## 口径二：两种"不知道"要区别对待

1. **必需能力没声明** → 拒绝（见上）。`REQUIRED_FOR_RUN` 里的每一项都是"没有它
   就根本跑不对"的能力，不是优化项。
2. **上下文窗口没声明** → 不做窗口核验，也**不假装核验过**。窗口与预算单位不同
   （窗口是 token，F5 的预算是字节），换算需要 tokenizer，本项目零依赖、不猜。
   所以窗口只在**声明了**的时候才核验；没声明就是"没核验"，不是"核验通过"。
   发送量仍然由 F5 的字节预算限制，不会因为这里不核验就无限膨胀。

## 有界重试

重试的判定条件只有一个：`ModelError.retryable`（这个字段此前没有任何消费方）。
非可重试的错误立刻抛出，不做"再试一次看看"。次数上限在 `Limits.max_model_attempts`
（默认 3），退避在 `Limits.retry_backoff_ms`（指数：基数、2×基数、4×基数…）。

重试**只包住模型调用本身**。模型调用没有副作用；把同一套重试套到派发/工具执行上
就是另一件事了（那里要的是幂等键 + 恢复，不是重试），所以这里的重试范围刻意写得
很窄：`loop._model_step` 里只包 `complete()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import TianshuError

#: 能力名 → `CapabilityProfile` 字段。与 F2 的 `_FEATURE_ATTRS` 同一种写法：
#: 判定只认特征名，不认字段名，改字段名不会悄悄改语义。
_FEATURE_ATTRS: dict[str, str] = {
    "tool_calling": "supports_tool_calling",
    "parallel_tool_calls": "supports_parallel_tool_calls",
    "streaming": "supports_streaming",
    "thinking": "supports_thinking",
}

#: 一次 run 最少需要的能力。
#:
#: 为什么只有这一项：这个循环的**全部**作用就是"模型提案工具调用，执行侧判定并发起"
#: （PRD §5.1）。一个不支持工具调用的模型在这里不是"能力弱一点"，是"跑的不是这个
#: 流程"——它只会聊，不会做，Run 最终必然以 noop/失败收场，白烧钱。
#:
#: 其余能力**刻意不在这里**：并行工具调用是性能项（顺序执行也能跑完）；流式与
#: thinking 是 v1 没有消费方的可选项。把它们塞进必需集合会让一批本来能跑的模型
#: 被拒——fail closed 针对的是"跑不对"，不是"跑得不漂亮"。
REQUIRED_FOR_RUN: frozenset[str] = frozenset({"tool_calling"})


class CapabilityMissing(TianshuError):
    """模型不具备本次运行必需的能力（或干脆没声明能力）。

    与"模型这一轮答得不对"分开：这是**准入**问题，发生在花第一分钱之前。
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        missing: tuple[MissingCapability, ...] = (),
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.missing = missing


@dataclass(frozen=True)
class MissingCapability:
    """一项没满足的必需能力，以及它是"没写"还是"写了做不到"。"""

    feature: str
    #: None = 未声明；False = 声明为不支持。
    declared: bool | None

    def describe(self) -> str:
        if self.declared is None:
            return f"{self.feature}（未声明）"
        return f"{self.feature}（声明为不支持）"


@dataclass(frozen=True)
class CapabilityProfile:
    """一个 provider+model 的**如实**能力声明。

    声明的是"这套 adapter + 这个模型"合起来能做什么——运行时能用的就是这两者的
    交集，把它们拆成两份声明只会制造"各自都支持、合起来不行"的假象。

    字段默认 `None`（未声明），见模块注释的口径一。
    """

    provider: str
    model: str
    supports_tool_calling: bool | None = None
    supports_parallel_tool_calls: bool | None = None
    supports_streaming: bool | None = None
    supports_thinking: bool | None = None
    #: 上下文窗口上限，单位**字节**。刻意不用 token：窗口在供应商文档里是 token，
    #: 而本项目没有 tokenizer（零依赖），换算出来的字节数是编的。F5 的预算也是
    #: 字节，两边同一单位才能比。
    context_window_bytes: int | None = None
    #: 这份声明从哪来（adapter 名 / 人工登记 / 测试替身）。审计用，不参与判定。
    declared_by: str = ""

    def __post_init__(self) -> None:
        for attr in ("provider", "model"):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value:
                raise ValueError(f"CapabilityProfile.{attr} 必须是非空字符串：{value!r}")
        for attr in _FEATURE_ATTRS.values():
            value = getattr(self, attr)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"CapabilityProfile.{attr} 必须是 bool 或 None：{value!r}")
        if self.context_window_bytes is not None:
            if isinstance(self.context_window_bytes, bool) or not isinstance(
                self.context_window_bytes, int
            ):
                raise ValueError(
                    f"context_window_bytes 必须是整数或 None：{self.context_window_bytes!r}"
                )
            if self.context_window_bytes <= 0:
                raise ValueError(
                    f"context_window_bytes 必须为正：{self.context_window_bytes!r}"
                )

    # ---- 查询 ----

    def declaration(self, feature: str) -> bool | None:
        """原始声明：True / False / None（未声明）。"""
        return getattr(self, _attr_of(feature))

    def supports(self, feature: str) -> bool:
        """**只有明确声明 True 才算支持。** 未声明一律按不支持处理。"""
        return self.declaration(feature) is True

    def undeclared(self) -> list[str]:
        """所有没声明的能力名。给"这份 profile 还剩多少未知"这类体检用。"""
        return [name for name in _FEATURE_ATTRS if self.declaration(name) is None]

    def to_payload(self) -> dict[str, Any]:
        """事件/结果里的形状。未声明写成 `null`，不写成 false。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "declared_by": self.declared_by,
            "context_window_bytes": self.context_window_bytes,
            **{
                name: self.declaration(name)
                for name in _FEATURE_ATTRS
            },
        }


# ---------------------------------------------------------------- 判定


def missing_capabilities(
    profile: CapabilityProfile, required: frozenset[str] = REQUIRED_FOR_RUN
) -> tuple[MissingCapability, ...]:
    """必需集合里没被满足的项，按特征名排序（结果稳定，可断言）。"""
    for feature in required:
        _attr_of(feature)  # 未知特征名立刻炸，不静默当"已满足"
    return tuple(
        MissingCapability(feature=name, declared=profile.declaration(name))
        for name in sorted(required)
        if not profile.supports(name)
    )


def check_capabilities(
    profile: CapabilityProfile, required: frozenset[str] = REQUIRED_FOR_RUN
) -> None:
    """必需能力缺失 → `CapabilityMissing`。缺的越多，报的越多。"""
    missing = missing_capabilities(profile, required)
    if not missing:
        return
    detail = "、".join(item.describe() for item in missing)
    raise CapabilityMissing(
        f"本次运行需要 {detail}，而 provider={profile.provider} "
        f"model={profile.model} 没有满足；未声明不等于支持（fail closed）",
        provider=profile.provider,
        model=profile.model,
        missing=missing,
    )


def check_context_budget(
    profile: CapabilityProfile, required_bytes: int
) -> None:
    """声明了窗口就核验预算放得下；没声明就**不核验**（见模块口径二）。"""
    if profile.context_window_bytes is None:
        return
    if required_bytes <= profile.context_window_bytes:
        return
    raise CapabilityMissing(
        f"本次运行的上下文预算 {required_bytes}B 超出 model={profile.model} "
        f"声明的窗口 {profile.context_window_bytes}B；"
        "要么调小 Limits.context_budget_bytes，要么换窗口更大的模型",
        provider=profile.provider,
        model=profile.model,
    )


def capability_profile_of(model: object) -> CapabilityProfile | None:
    """取模型的能力声明。没有这条能力 → `None`（未登记），不猜。

    声明取不到（方法抛错）或返回了别的东西，一律按"未声明"处理：准入的结论都是
    拒绝，但 `admit_model` 会把原因写清楚，而不是让一个 AttributeError 漂到
    主循环的错误信息里。

    与 F13 的 `model.identity_of` 同一种可选能力写法：不把方法加进 `Model` 协议
    再逼所有实现补一遍——判定方自己决定"没有声明"怎么处理，比让实现方伪造声明好。
    """
    getter = getattr(model, "capability_profile", None)
    if not callable(getter):
        return None
    try:
        profile = getter()
    except Exception:  # noqa: BLE001 - 声明坏了 = 未声明，结论仍是拒绝
        return None
    return profile if isinstance(profile, CapabilityProfile) else None


def admit_model(
    model: object,
    *,
    required: frozenset[str] = REQUIRED_FOR_RUN,
    required_context_bytes: int | None = None,
) -> CapabilityProfile:
    """准入判定：能不能用这个模型跑这次 run。过不了就 `CapabilityMissing`。

    没有能力声明的对象**不放行**：它不是"能力未知的模型"，是"没进入准入系统的
    实现"。放它过去等于准入对第三方实现完全无效——而第三方实现恰恰最需要被核。
    """
    profile = capability_profile_of(model)
    if profile is None:
        name = type(model).__name__
        missing = tuple(
            MissingCapability(feature=feature, declared=None)
            for feature in sorted(required)
        )
        detail = "、".join(item.describe() for item in missing) or "（空）"
        raise CapabilityMissing(
            f"{name} 没有声明能力（未实现 capability_profile），"
            f"本次运行需要 {detail}；未声明不等于支持（fail closed）",
            provider=name,
            model="",
            missing=missing,
        )
    check_capabilities(profile, required)
    if required_context_bytes is not None:
        check_context_budget(profile, required_context_bytes)
    return profile


def retry_delay_ms(attempt: int, backoff_ms: int) -> int:
    """第 `attempt` 次失败之后该等多久（指数退避，`attempt` 从 1 开始）。

    纯函数：不读时钟。真正的等待由调用方注入的 sleeper 执行，测试才能不睡觉地
    验证"退避序列是 500/1000/2000"。`backoff_ms <= 0` 表示不退避（测试用得上）。
    """
    if attempt < 1:
        raise ValueError(f"attempt 从 1 开始：{attempt!r}")
    if backoff_ms <= 0:
        return 0
    return backoff_ms * (2 ** (attempt - 1))


def _attr_of(feature: str) -> str:
    try:
        return _FEATURE_ATTRS[feature]
    except KeyError:
        raise ValueError(
            f"未知能力：{feature!r}（可选：{sorted(_FEATURE_ATTRS)}）"
        ) from None


__all__ = [
    "REQUIRED_FOR_RUN",
    "CapabilityMissing",
    "CapabilityProfile",
    "MissingCapability",
    "admit_model",
    "capability_profile_of",
    "check_capabilities",
    "check_context_budget",
    "missing_capabilities",
    "retry_delay_ms",
]
