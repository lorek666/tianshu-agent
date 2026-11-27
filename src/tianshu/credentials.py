"""凭据接缝（F12）：从环境变量按 provider / capability 分域读取。

非敏感设置持久化为 JSON 文本，密钥只读启动环境，不使用系统钥匙串。
环境变量不是安全存储，可能被同用户进程、调试器或崩溃报告看到；执行子进程
仍使用最小环境。本模块提供 Secret 表示与日志脱敏，不能防止直接输出明文值。

未配置返回 None，不支持的域或退役后端抛 CredentialUnsupported；写入和删除
明确拒绝，不能把修改进程环境冒充持久化配置。见 docs/credential-env-contract.md。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import CredentialUnsupported, CredentialWriteRefused

__all__ = [
    "CAPABILITY_CLASSES",
    "ENV_VARS",
    "MASK",
    "CredentialBackendCapabilities",
    "CredentialRef",
    "CredentialStore",
    "EnvCredentialStore",
    "Secret",
    "UnsupportedCredentialStore",
    "default_store",
    "redact",
    "register_secret",
]

#: 打码后的占位。写成常量是为了测试能直接断言它，而不是各自抄字面量。
MASK = "***"

#: v1 实际用到的能力域。**不是封闭枚举**：provider 的可选能力随协议变，
#: 硬编码一个全集等于替未来撒谎。这里只登记"现在真的有调用方"的域。
CAPABILITY_CLASSES = ("llm",)

#: 域 → 环境变量名。所有适配器通过同一接缝取值。
ENV_VARS: dict[tuple[str, str], str] = {
    ("openai", "llm"): "TIANSHU_API_KEY",
    ("anthropic", "llm"): "ANTHROPIC_API_KEY",
}

#: 短于这个长度的值不进替换表：把 `abc` 这种子串全局替换会把正常日志打烂。
_MIN_REDACT_LEN = 8


# ---------------------------------------------------------------- 脱敏


class Secret(str):
    """密钥本体，但 `repr()` 永远打码。

    为什么继承 `str` 而不是包一层：请求头是 `f"Bearer {key}"` 拼出来的，
    包一层就要在每个调用点 `reveal()`，漏一处就是 401，而 401 的表现是
    "模型不工作"，很难查。继承 `str` 让所有既有拼接继续正确工作。

    为什么**只**覆盖 `__repr__`、不覆盖 `__str__` / `__format__`：
    `f"{secret}"` 走 `str.__format__`，一旦覆盖成掩码，真正发出去的
    `Authorization` 头就会变成 `Bearer ***`。安全的对象表示与正确的取值
    必须分开——打码只发生在"给人看"的路径（repr / 日志），取值路径保持原样。

    这不是"防住一切"：`str(secret)`、`secret == "..."`、切片都能拿到明文。
    它防的是**顺手把对象塞进日志**这一类最常见的事故。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Secret({MASK})"


#: 进程内已登记的密钥明文。`redact()` 用它做精确替换。
_REGISTERED: set[str] = set()

#: 形态兜底：即使某个值没被登记（例如代理站把 key 回显在响应体里），
#: 这几类形状也要打码。宁可多打一点，也不要把可能有效的密钥写进日志。
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token>
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-+/=]{8,}"),
    # x-api-key: <token> / api_key=<token>
    re.compile(r"(?i)\b(x-api-key\s*[:=]\s*)[^\s\"',]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key\s*[:=]\s*)[^\s\"',]{8,}"),
    # OpenAI 风格的字面量
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    # URL 里的 userinfo 密码：https://user:pass@host
    re.compile(r"(://[^/\s:@]+:)[^@\s]+@"),
)


def register_secret(value: str | None) -> None:
    """把已知的密钥明文登记进替换表。

    在**取值时**登记（`CredentialStore.get()` / adapter 构造），这样之后
    任何 `redact()` 都能把它抹掉——包括我们没预料到的日志点。
    """
    if value and len(value) >= _MIN_REDACT_LEN:
        _REGISTERED.add(value)


def redact(text: str) -> str:
    """把已知凭据与「看起来像凭据」的片段打码。

    幂等：对已打码的文本再跑一次不会变化（掩码本身不匹配任何模式）。
    """
    if not text:
        return text
    out = text
    # 先按具体值替换：长的先换，避免短值是长值的前缀时留下残段。
    for value in sorted(_REGISTERED, key=len, reverse=True):
        if value in out:
            out = out.replace(value, MASK)
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + MASK, out)
    return out


# ---------------------------------------------------------------- 分域键


@dataclass(frozen=True, order=True)
class CredentialRef:
    """凭据的分域键：`(provider, capability_class)`。

    为什么分域而不是"一个 key 走天下"：同一个 provider 的不同能力可能用
    不同的凭据（一个 key 只能做 chat、另一个才能做 batch），把它们混成
    一个变量名就没法表达"这个 key 不该用于那个能力"。

    构造时就校验形状（小写、无空白）：`OpenAI` 与 `openai` 若被当成两个域，
    表现是"明明配了却读不到"——这种 bug 只能靠日志猜。宁可构造时炸。
    """

    provider: str
    capability_class: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "capability_class"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"CredentialRef.{field_name} 必须是非空字符串，收到 {value!r}"
                )
            if value != value.strip().lower():
                raise ValueError(
                    f"CredentialRef.{field_name} 必须是小写且不含空白，收到 {value!r}"
                    "（`OpenAI` 和 `openai` 若算两个域，表现是『配了却读不到』）"
                )

    def __str__(self) -> str:
        return f"{self.provider}/{self.capability_class}"


# ---------------------------------------------------------------- 后端能力


@dataclass(frozen=True)
class CredentialBackendCapabilities:
    """后端能力声明。照 F2 的口径：**做不到要说出来，不许降级**。"""

    name: str
    #: 是不是"安全存储"（OS 托管、不落明文）。环境变量不是。
    secure_storage: bool
    #: `set()` / `delete()` 能不能真的落地。
    supports_write: bool
    #: 这个后端在本版本里是否真的实现了。
    implemented: bool


ENV_CAPABILITIES = CredentialBackendCapabilities(
    name="env",
    secure_storage=False,
    supports_write=False,
    implemented=True,
)

# ---------------------------------------------------------------- 协议


@runtime_checkable
class CredentialStore(Protocol):
    """凭据后端。三个方法的返回值语义是契约的一部分。

    - `get()`：有 → `Secret`；**没有 → `None`**；做不到 → `CredentialUnsupported`。
      绝不返回空串——空串会被 `if not key` 判成未配置，但也会被
      `f"Bearer {key}"` 拼成一个合法的坏请求，两种表现完全不同。
    - `set()` / `delete()`：落地不了就 `CredentialWriteRefused`，不假装成功。
    """

    capabilities: CredentialBackendCapabilities

    def get(self, ref: CredentialRef) -> Secret | None: ...

    def set(self, ref: CredentialRef, secret: str) -> None: ...

    def delete(self, ref: CredentialRef) -> None: ...


# ---------------------------------------------------------------- 后端实现


class EnvCredentialStore:
    """只读凭据后端：从环境变量读。**不是安全存储**（威胁模型见模块 docstring）。

    只读：`set()` / `delete()` 一律拒绝。理由不是懒——环境变量是**进程启动时
    就定的镜像**，写 `os.environ` 不会持久化、也不影响已经启动的其他进程，
    假装成功会让"我明明 set 成功过"变成一句谎话。
    """

    capabilities = ENV_CAPABILITIES

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def get(self, ref: CredentialRef) -> Secret | None:
        name = ENV_VARS.get((ref.provider, ref.capability_class))
        if name is None:
            # 没有约定的变量名 ≠ 没配置：这是我们没为这个域定义过后端，
            # 属于 unsupported。返回 None 会让用户去翻一个不存在的变量。
            raise CredentialUnsupported(
                f"环境变量后端没有为 {ref} 定义变量名。"
                f"已知域：{'、'.join(f'{p}/{c}' for p, c in ENV_VARS)}。"
                "要支持新 provider，在 credentials.ENV_VARS 里登记，"
                "而不是散落各处 os.environ.get。"
            )
        raw = self._env.get(name)
        if not raw:
            # 空串按"未配置"处理：`KEY=` 是一个很容易写出来的空值，
            # 把它当成凭据会让请求带着 `Bearer ` 发出去。
            return None
        register_secret(raw)
        return Secret(raw)

    def set(self, ref: CredentialRef, secret: str) -> None:
        raise CredentialWriteRefused(
            f"环境变量后端是只读的：无法写入 {ref}。"
            "它不是安全存储，也不该被当成可写凭据库——"
            "请设置对应的环境变量，并注意它对子进程可见（见 credentials 模块文档）。"
        )

    def delete(self, ref: CredentialRef) -> None:
        raise CredentialWriteRefused(
            f"环境变量后端是只读的：无法删除 {ref}。请从进程环境里移除该变量。"
        )


class UnsupportedCredentialStore:
    """任意"这个平台做不到"的后端占位。

    存在的理由：将来接 Windows Credential Manager / Linux Secret Service 时，
    失败原因各不相同。把原因做成参数，就不会退化成一个大而全的分支。
    """

    def __init__(self, backend: str, reason: str) -> None:
        self.capabilities = CredentialBackendCapabilities(
            name=backend, secure_storage=True, supports_write=False, implemented=False
        )
        self._reason = reason

    def get(self, ref: CredentialRef) -> Secret | None:
        raise CredentialUnsupported(f"无法读取 {ref}（{self.capabilities.name}）：{self._reason}")

    def set(self, ref: CredentialRef, secret: str) -> None:
        raise CredentialUnsupported(f"无法写入 {ref}（{self.capabilities.name}）：{self._reason}")

    def delete(self, ref: CredentialRef) -> None:
        raise CredentialUnsupported(f"无法删除 {ref}（{self.capabilities.name}）：{self._reason}")


def default_store() -> CredentialStore:
    """仅支持 env；显式选择退役 Keychain 或未知后端必须报错。"""
    name = (os.environ.get("TIANSHU_CREDENTIAL_BACKEND") or "env").strip().lower()
    if name == "env":
        return EnvCredentialStore()
    if name == "keychain":
        raise CredentialUnsupported(
            "Keychain 凭据后端已停用，请移除 TIANSHU_CREDENTIAL_BACKEND=keychain，"
            "并设置 TIANSHU_API_KEY / ANTHROPIC_API_KEY 后重新启动。"
        )
    raise CredentialUnsupported("未知的凭据后端（仅支持 env）")
