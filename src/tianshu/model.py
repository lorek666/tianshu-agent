"""模型接缝（C1）。

不绑定任何厂商：核心只认 Model 协议。换模型 = 换 adapter，主循环不动。

**这条接缝有个容易被忽略的前提，M3 把它写了下来**：主循环吃的 `messages`
是一个**规范化内部形状**（OpenAI 风格的 role/content/tool_calls），而它不是
"随便什么"，它是被**持久化进游标、被 `messages_hash` 盖进事件、被 replay 用来
重建**的东西。所以 adapter 的职责是两个方向的翻译：

    内部规范化形状  ──adapter──▶  各家线上协议
    内部规范化形状  ◀──adapter──  各家线上协议

adapter 只能翻译，**不得反向改写内部形状**——一改，replay 重建出来的消息
序列就和当时发出去的不一样，`messages_hash` 对不上，整条证据链作废。

实现：
  - ScriptedModel            离线确定性，验收测试用
  - RecordedModel            回放用，答案来自事件日志
  - OpenAICompatibleModel    第一个线上协议（OpenAI / DeepSeek / vLLM / Ollama）
  - AnthropicMessagesModel   第二个线上协议（Messages API）

错误语义（PRD §5.1 的 `ModelError`）：传输失败、HTTP 状态、响应体不合协议，
一律**抛 `ModelError`**，绝不伪造 `finish_reason`。`finish_reason` 是 13 值
封闭集合（PRD §5.5），塞一个 `"error"` / `"unknown"` 进去等于在事件日志里留下
一个契约不允许的值，换 provider 时没人知道该不该继续兼容它。
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from .capability import CapabilityProfile
from .continuation import ContinuationRef, require_scope
from .credentials import (
    MASK,
    CredentialRef,
    Secret,
    default_store,
    redact,
    register_secret,
)
from .errors import TianshuError
from .http_framing import HttpFramingError, chunk_lines, read_chunks, response_length
from .prompt_cache import (
    anthropic_breakpoints,
    resolve_policy,
    routing_key,
    stable_system,
    stable_tools,
)
from .task_attachments import AttachmentError, plain_messages

# PRD §5.5：模型侧的结束原因，13 值封闭集合。
FinishReason = str

# 注意 `max_tokens` 与 `limit_output_tokens` 在 PRD 里是同义并存的两个值：
# 前者是各家 SDK 的历史写法，后者是本项目的规范名。两个都接受，但我们
# 自己**只产出** `limit_output_tokens`（谁产出的值决定语义，别两边都产）。
FINISH_REASONS = frozenset(
    {
        "cancelled",
        "checkpoint",
        "content_filtered",
        "end_turn",
        "guardrail_intervened",
        "interrupt",
        "limit_output_tokens",
        "limit_structured_output_attempts",
        "limit_total_tokens",
        "limit_turns",
        "max_tokens",
        "stop_sequence",
        "tool_use",
    }
)


class ModelError(TianshuError):
    """模型侧的错误对象（PRD §5.1 的核心对象之一）。

    为什么要类型而不是字符串：`finish_reason` 表达的是"模型这一轮怎么结束的"，
    而 401、429、连接被掐断压根不是模型的选择。混在一起，重试/降级这些策略就
    在接缝处丢了——上层只能看见一个 `"error"` 字符串。

    `retryable` 是**建议**，不是保证：429 的 Retry-After、5xx 的抖动都可能
    让"可重试"重复失败，调用方仍必须有自己的上限。
    """

    KINDS = ("auth", "http_status", "protocol", "rate_limit", "timeout", "transport")

    def __init__(
        self,
        message: str,
        *,
        kind: str = "transport",
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        # **所有** ModelError 的文案在这里过一遍脱敏。理由：主循环把
        # `repr(exc)` 写进 `model.error` 事件，而 detail 来自 HTTP 响应体
        # （代理站可能把 Authorization 回显出来）。事件日志是永久证据，
        # 一旦写进去就擦不掉了——所以在构造点打码，而不是指望每个写入点记得打。
        super().__init__(redact(message))
        if kind not in self.KINDS:
            raise ValueError(f"未知 ModelError.kind：{kind}（可选 {self.KINDS}）")
        self.kind = kind
        self.status_code = status_code
        if retryable is None:
            retryable = kind in ("transport", "timeout", "rate_limit") or (
                status_code is not None and status_code >= 500
            )
        self.retryable = retryable

    @classmethod
    def from_http(cls, status_code: int, detail: str) -> ModelError:
        """把 HTTP 状态码翻译成错误类别。

        分类只影响"要不要重试/要不要提示用户检查 key"，不改变"这一轮失败了"
        这个结论——任何非 2xx 都不允许变成 `end_turn`。
        """
        if status_code in (401, 403):
            kind = "auth"
        elif status_code == 429:
            kind = "rate_limit"
        elif status_code == 408:
            kind = "timeout"
        else:
            kind = "http_status"
        return cls(
            f"HTTP {status_code}: {detail}" if detail else f"HTTP {status_code}",
            kind=kind,
            status_code=status_code,
        )

    def __repr__(self) -> str:
        # 主循环把 `repr(exc)` 写进 `model.error` 事件，所以这里要自带类别与状态码，
        # 否则事后只能看到一句人话，判断不了"该重试还是该换 key"。
        status = "" if self.status_code is None else f" status={self.status_code}"
        return f"ModelError(kind={self.kind}{status}, retryable={self.retryable}): {self}"


@dataclass(frozen=True)
class ModelStreamDelta:
    """C1 流式增量契约对象（W4，`schemas/c1/model.json` `$defs.modelStreamDelta`）。

    流式增量只用于控制面/在线 UI 的瞬态预览，不逐片写入 SQLite `events` 表；
    唯一持久化事实源仍是流正常收束后的 `ModelResponse` / `model.result`。
    """

    kind: str
    text: str = ""
    tool_name: str = ""
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError(f"未知 ModelStreamDelta.schema_version：{self.schema_version!r}")
        if self.kind not in ("text", "tool_call_start"):
            raise ValueError(f"未知 ModelStreamDelta.kind：{self.kind!r}")

    def to_payload(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "text": self.text,
            "tool_name": self.tool_name,
        }


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | str  # str 表示模型给出的半截/非法 JSON


@dataclass
class ModelResponse:
    finish_reason: FinishReason
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int | None] = field(default_factory=dict)
    continuation: ContinuationRef | None = None

    def __post_init__(self) -> None:
        # 越界值在这里就炸，而不是等它沉进事件日志、几个月后有人在
        # `run_turns` 投影里看见一个语料里不存在的结束原因。
        if self.finish_reason not in FINISH_REASONS:
            raise ValueError(
                f"finish_reason 越界：{self.finish_reason!r} 不在 PRD §5.5 的 13 值集合内"
                "（传输/HTTP 类错误请抛 ModelError，不要塞进结束原因）"
            )
        self.usage = normalize_usage(self.usage)
        if self.continuation is not None:
            self.to_payload()

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "finish_reason": self.finish_reason,
            "content": self.content,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in self.tool_calls
            ],
            "usage": self.usage,
        }
        if self.continuation is not None:
            self.continuation.validate_projection(payload)
            payload["continuation"] = self.continuation.to_payload()
        return payload

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> ModelResponse:
        """事件 payload → 响应对象。

        M3 之前 adapter 会在失败时写 `finish_reason="error"` / `"unknown"`，
        这两个值不在封闭集里，所以读**那种**历史事件会在这里抛 ValueError。
        这是有意的：我们不把非法值悄悄洗白成一个合法值（那会让"当时到底怎么
        结束的"永远查不清）。当前主循环与 replay 都不走这条路径，它只服务于
        手工排查与测试替身。
        """
        return cls(
            finish_reason=p["finish_reason"],
            content=p.get("content", ""),
            tool_calls=[
                ToolCall(c["call_id"], c["name"], c["arguments"])
                for c in p.get("tool_calls", [])
            ],
            usage=p.get("usage", {}),
            continuation=ContinuationRef.from_payload(p["continuation"])
            if "continuation" in p else None,
        )


# 各家 usage 字段名不同，归一化成这两个键；缺失一律 None，绝不用 0 冒充。
_USAGE_KEYS: dict[str, str] = {
    # OpenAI / DeepSeek / Moonshot / vLLM
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    # Anthropic
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    # Gemini
    "promptTokenCount": "input_tokens",
    "candidatesTokenCount": "output_tokens",
}

# 只在 provider 真的报了才出现的键。**不并入** input_tokens：Anthropic 的
# `input_tokens` 本身不含缓存读写，硬加进去会让同一批 token 被计两次费用。
_USAGE_OPTIONAL: dict[str, str] = {
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
}


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int | None]:
    """把任意 provider 的 usage 归一化。

    关键约束（PRD §5.1）：未知为 None，不是 0。
    把未知当 0 会让预算统计看起来正常，实际已经失控——比报错更危险。
    """
    out: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
    if not isinstance(raw, dict):
        return out
    for src, dst in _USAGE_KEYS.items():
        v = raw.get(src)
        if isinstance(v, int) and not isinstance(v, bool):
            if v < 0:
                raise ModelError(f"usage.{src} 必须是非负整数", kind="protocol")
            out[dst] = v
    for src, dst in _USAGE_OPTIONAL.items():
        if src not in raw:
            continue
        value = raw[src]
        if value is not None and (type(value) is not int or value < 0):
            raise ModelError(f"usage.{src} 必须是非负整数或 null", kind="protocol")
        out[dst] = value
    return out


def _chat_usage(
    raw: dict[str, Any] | None, *, inclusive_input: bool = False,
) -> dict[str, int | None]:
    """只有已知官方 Chat 才断言包含语义；始终校验数值与别名一致性。"""
    usage = normalize_usage(raw)
    if isinstance(raw, dict) and raw.get("prompt_tokens_details") is not None:
        return _native_cache_usage(
            usage, raw["prompt_tokens_details"], inclusive_input=inclusive_input,
        )
    return _native_cache_usage(usage, {}) if inclusive_input else usage


def _native_cache_usage(
    usage: dict[str, int | None], details: Any, *, require_total: bool = False,
    inclusive_input: bool = True,
) -> dict[str, int | None]:
    """只投影实际字段；按已知输入语义校验总量，不猜兼容端点的分母。"""
    if not isinstance(details, dict):
        raise ModelError("usage token details 必须是对象", kind="protocol")
    total = usage["input_tokens"]
    for source, target in (
        ("cached_tokens", "cache_read_input_tokens"),
        ("cache_write_tokens", "cache_creation_input_tokens"),
    ):
        if source not in details:
            continue
        value = details[source]
        if value is not None and (type(value) is not int or value < 0):
            raise ModelError(f"usage.{source} 必须是非负整数或 null", kind="protocol")
        if value is not None and (
            (require_total and total is None)
            or (inclusive_input and total is not None and value > total)
        ):
            raise ModelError(f"usage.{source} 超过已知输入", kind="protocol")
        if target in usage and usage[target] != value:
            raise ModelError("usage 缓存字段冲突", kind="protocol")
        usage[target] = value
    known_cache = [usage.get(key) for key in (
        "cache_read_input_tokens", "cache_creation_input_tokens",
    )]
    if inclusive_input and total is not None and sum(value or 0 for value in known_cache) > total:
        raise ModelError("usage 缓存读写之和超过输入", kind="protocol")
    return usage


class Model(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class ModelIdentity:
    """一次模型调用"用谁、花了什么"的身份，给 F13 成本核算用。

    三个字段各有各的问题，不能互相顶替：

    - `adapter`：实际用的协议适配器（`openai-compatible` / `anthropic` / ...）。
      它总是确定的，也是排查"为什么这条价格查不到"的第一条线索。
    - `vendor`：**价目表认的厂商**。协议 ≠ 厂商：`openai-compatible` 可以指向
      代理站、自建 vLLM、Bedrock 转发，那里的价格由对方定。所以只有端点确实是
      官方主机时这里才有值，否则 `None`（= 价格未知，不是免费）。
    - `model`：请求里真正发出去的模型 id（不做归一化）。
    """

    adapter: str
    vendor: str | None
    model: str


def identity_of(model: object) -> ModelIdentity | None:
    """取模型的可选计价身份。没有这条能力就返回 `None`（未知），不靠猜。

    用可选能力而不是往 `Model` 协议里加必需方法：协议是"能补全"的最小契约，
    测试替身、录制回放替身都没有真实厂商身份，逼它们实现只是在鼓励伪造。
    """
    getter = getattr(model, "price_identity", None)
    if not callable(getter):
        return None
    try:
        identity = getter()
    except Exception:  # noqa: BLE001 - 身份取不到只影响费用展示，不该拖垮调用
        return None
    return identity if isinstance(identity, ModelIdentity) else None


# ---------------------------------------------------------------- 传输层


class Transport(Protocol):
    """一次 JSON POST。抽出来的唯一目的是让契约测试能跑真实的解析代码而不联网。"""

    def post(
        self, url: str, *, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> dict[str, Any]: ...


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """模型凭据绑定配置的源；响应不能选择另一个凭据接收方。"""

    def __init__(self, *, allow_redirects: bool) -> None:
        self._allow_redirects = allow_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._allow_redirects:
            fp.close()
            raise ModelError("模型端点重定向已拒绝", kind="protocol")

        def origin(url):
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("重定向目标不能包含 userinfo")
            port = parsed.port
            if port is None:
                port = 443 if parsed.scheme.lower() == "https" else 80
            return (
                parsed.scheme.lower(), parsed.hostname, port,
            )

        try:
            allowed = origin(req.full_url) == origin(newurl)
        except ValueError:
            allowed = False
        if not allowed:
            fp.close()
            raise ModelError("模型端点跨源重定向已拒绝", kind="protocol")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _CompleteResponseHead:
    def __init__(self, stream):
        self._stream = stream

    def readline(self, size: int) -> bytes:
        line = self._stream.readline(size)
        if not line.endswith(b"\r\n"):
            raise HttpFramingError("模型响应状态行或头部不完整")
        return line

    def close(self) -> None:
        self._stream.close()


class _CompleteResponse(http.client.HTTPResponse):
    def begin(self) -> None:
        stream = self.fp
        assert stream is not None
        self.fp = _CompleteResponseHead(stream)
        try:
            super().begin()
            response_length(self)
        finally:
            self.fp = stream


class _CompleteHTTPConnection(http.client.HTTPConnection):
    response_class = _CompleteResponse


class _CompleteHTTPSConnection(http.client.HTTPSConnection):
    response_class = _CompleteResponse


class _CompleteHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CompleteHTTPConnection, req)


class _CompleteHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_CompleteHTTPSConnection, req, context=self._context)


_MAX_MODEL_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_MODEL_CHUNK_METADATA = 1024 * 1024


def _complete_chunks(response: http.client.HTTPResponse) -> Iterator[bytes]:
    return read_chunks(
        response, _MAX_MODEL_RESPONSE_BYTES, max_metadata=_MAX_MODEL_CHUNK_METADATA,
    )


def _read_model_body(response: http.client.HTTPResponse) -> bytes:
    if response.chunked:
        return b"".join(_complete_chunks(response))
    limit = _MAX_MODEL_RESPONSE_BYTES
    if response.length is not None and response.length > limit:
        raise HttpFramingError("模型响应超出字节上限")
    raw = response.read(limit + 1)
    if len(raw) > limit or response.length:
        raise HttpFramingError("模型响应超出上限或正文截断")
    return raw


def _complete_sse_lines(response: http.client.HTTPResponse) -> Iterator[bytes]:
    if response.chunked:
        yield from chunk_lines(_complete_chunks(response))
        return
    remaining = _MAX_MODEL_RESPONSE_BYTES
    while line := response.readline(remaining + 1):
        remaining -= len(line)
        if remaining < 0:
            raise HttpFramingError("模型 SSE 响应超出字节上限")
        yield line
    if response.length:
        raise HttpFramingError("模型 SSE 响应正文截断")


class UrllibTransport:
    """stdlib 实现。零第三方依赖是这个项目的一条硬线（PRD §7.3）。"""

    def __init__(self, *, allow_redirects: bool = True, strict_framing: bool = False) -> None:
        self._allow_redirects = allow_redirects
        self._strict_framing = strict_framing

    @contextmanager
    def _open(
        self, req: urllib.request.Request, timeout: float,
    ) -> Iterator[http.client.HTTPResponse]:
        handlers = [_SameOriginRedirect(allow_redirects=self._allow_redirects)]
        if self._strict_framing:
            handlers.extend((_CompleteHTTPHandler(), _CompleteHTTPSHandler()))
        try:
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=timeout) as response:
                yield response
        except urllib.error.HTTPError as exc:
            with exc:
                raw = _read_model_body(exc.fp) if self._strict_framing else exc.read()
            raise ModelError.from_http(exc.code, raw.decode("utf-8", "replace")[:400]) from exc

    def post(
        self, url: str, *, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with self._open(req, timeout) as resp:
                raw = _read_model_body(resp) if self._strict_framing else resp.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ModelError(f"请求超时：{reason}", kind="timeout") from exc
            if isinstance(reason, (OSError, http.client.IncompleteRead)):
                raise ModelError(f"网络错误：{reason}", kind="transport") from exc
            if isinstance(reason, http.client.HTTPException):
                raise ModelError(
                    f"HTTP 协议异常：{type(reason).__name__}: {reason}", kind="protocol"
                ) from exc
            raise ModelError(f"网络错误：{reason}", kind="transport") from exc
        except TimeoutError as exc:  # socket.timeout 在 3.10+ 就是 TimeoutError
            raise ModelError(f"请求超时：{exc}", kind="timeout") from exc
        except (OSError, http.client.IncompleteRead) as exc:
            raise ModelError(f"网络错误：{exc}", kind="transport") from exc
        except http.client.HTTPException as exc:
            raise ModelError(
                f"HTTP 协议异常：{type(exc).__name__}: {exc}", kind="protocol"
            ) from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelError(
                f"响应不是合法 JSON：{type(exc).__name__}: {exc}", kind="protocol"
            ) from exc
        if not isinstance(data, dict):
            raise ModelError(
                f"响应顶层不是对象：{type(data).__name__}", kind="protocol"
            )
        return data

    def post_stream(
        self,
        url: str,
        *,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> Iterator[tuple[str, str]]:
        """发起 SSE POST 流请求，逐帧产出 `(event_name, data_str)`。"""
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **headers,
            },
            method="POST",
        )
        try:
            with self._open(req, timeout) as resp:
                lines = _complete_sse_lines(resp) if self._strict_framing else resp
                yield from _iter_sse_frames(lines, strict=self._strict_framing)
                # HTTPResponse.readline() can reach EOF without checking Content-Length.
                if not self._strict_framing and resp.length:
                    raise ModelError("SSE 响应正文截断", kind="transport")
        except ModelError:
            raise
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ModelError(f"请求超时：{reason}", kind="timeout") from exc
            if isinstance(reason, (OSError, http.client.IncompleteRead)):
                raise ModelError(f"SSE 网络错误：{reason}", kind="transport") from exc
            if isinstance(reason, http.client.HTTPException):
                raise ModelError(
                    f"SSE HTTP 协议异常：{type(reason).__name__}: {reason}", kind="protocol"
                ) from exc
            raise ModelError(f"网络错误：{reason}", kind="transport") from exc
        except TimeoutError as exc:
            raise ModelError(f"请求超时：{exc}", kind="timeout") from exc
        except (OSError, http.client.IncompleteRead) as exc:
            raise ModelError(f"SSE 网络错误：{exc}", kind="transport") from exc
        except http.client.HTTPException as exc:
            raise ModelError(
                f"SSE HTTP 协议异常：{type(exc).__name__}: {exc}", kind="protocol"
            ) from exc


def _iter_sse_frames(byte_lines: Any, *, strict: bool = False) -> Iterator[tuple[str, str]]:
    """将字节行迭代器解析为标准 SSE `(event, data)` 帧。"""
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in byte_lines:
        try:
            line = (
                raw_line.decode("utf-8")
                if isinstance(raw_line, (bytes, bytearray))
                else str(raw_line)
            )
        except UnicodeDecodeError as exc:
            raise ModelError(f"SSE 流包含非法 UTF-8 字节：{exc}", kind="protocol") from exc
        if strict and not line.endswith("\n"):
            raise ModelError("SSE 响应行未结束", kind="protocol")
        line = line.rstrip("\r\n")
        if line == "":
            if data_lines:
                yield (event_name, "\n".join(data_lines))
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        colon = line.find(":")
        field_name = line if colon == -1 else line[:colon]
        value = "" if colon == -1 else line[colon + 1 :]
        if value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            event_name = value
        elif field_name == "data":
            data_lines.append(value)
    if data_lines:
        if strict:
            raise ModelError("SSE 响应事件未以空行结束", kind="protocol")
        yield (event_name, "\n".join(data_lines))


# ---------------------------------------------------------------- 离线实现


class ScriptedModel:
    """按脚本依次返回。脚本用完即视为 end_turn（避免无限循环掩盖 bug）。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def price_identity(self) -> ModelIdentity:
        """脚本模型没有真实厂商：适配器名照实给，厂商与价格留未知。

        这一点很关键——它让"没有价格"的路径在本地就能跑到：CLI 的
        `--model scripted` 走的就是这条，输出里看到的是"费用未知"而不是 `0`。
        """
        return ModelIdentity(adapter="scripted", vendor=None, model="")

    def capability_profile(self) -> CapabilityProfile:
        """脚本模型能做什么是**它自己实现出来的**，所以可以如实声明。

        它按脚本逐条吐 `ModelResponse`，因此"能返回工具调用"和"一轮多个调用"
        都是真的；窗口与 thinking 没有概念，留未声明（不是 false）。
        """
        return CapabilityProfile(
            provider="scripted",
            model="scripted",
            supports_tool_calling=True,
            supports_parallel_tool_calls=True,
            supports_streaming=True,
            declared_by="ScriptedModel（脚本替身）",
        )

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        self.calls += 1
        if not self._responses:
            return ModelResponse(finish_reason="end_turn", content="")
        return self._responses.pop(0)

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[ModelStreamDelta], None],
    ) -> ModelResponse:
        resp = self.complete(messages, tools)
        for tc in resp.tool_calls:
            on_delta(ModelStreamDelta(kind="tool_call_start", tool_name=tc.name))
        if resp.content:
            mid = max(1, len(resp.content) // 2)
            for part in (resp.content[:mid], resp.content[mid:]):
                if part:
                    on_delta(ModelStreamDelta(kind="text", text=part))
        return resp


class RecordedModel:
    """把一串记下来的响应按顺序吐出来（手工排查 / 测试替身用）。

    注意：`replay` **不用**它——回放是从事件重建消息序列并比对 `messages_hash`，
    不重新构造模型响应。别把这个类当成"回放能力"本身。
    """

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def capability_profile(self) -> CapabilityProfile:
        """录制的响应可能含工具调用（它只是把当时的内容再吐一遍），所以声明
        `tool_calling`；流式/thinking/窗口都不声明——它压根没有这些概念。"""
        return CapabilityProfile(
            provider="recorded",
            model="recorded",
            supports_tool_calling=True,
            declared_by="RecordedModel（录制回放替身）",
        )

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        if not self._responses:
            raise RuntimeError("回放中断：事件日志里的模型响应少于实际调用次数")
        return self._responses.pop(0)


# ---------------------------------------------------------------- 线上实现


def _reject_continuation(messages: list[dict[str, Any]]) -> None:
    try:
        require_scope(messages, None)
    except ValueError as exc:
        raise ModelError(str(exc), kind="protocol", retryable=False) from None


def _provider_text_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return plain_messages(messages)
    except AttachmentError as exc:
        raise ModelError(str(exc), kind="protocol", retryable=False) from None


def _credential_for(provider: str, capability_class: str = "llm") -> str:
    """经凭据接缝取密钥；未配置返回空串（由 `_HttpModel` 统一报「缺少 API key」）。

    为什么不让各 adapter 自己 `os.environ.get`：接缝后面将来是 OS Keychain，
    散落的 `os.environ.get` 会让"换后端"变成一次全局搜索，而且**每个漏改的
    点都会静默退回环境变量**——那就等于假装 Keychain 生效了。

    未配置（`get() -> None`）与做不到（`CredentialUnsupported`）在这里分开：
    前者变成空串、走既有的 auth 错误提示（告诉用户设哪个变量）；
    后者**直接抛出去**，绝不退回环境变量。
    """
    found = default_store().get(CredentialRef(provider, capability_class))
    return "" if found is None else str(found)


class _HttpModel:
    """两个线上 adapter 的公共骨架：装配请求 / 解析响应 / 错误分类。"""

    #: 协议适配器名（写进事件，永远确定）。
    ADAPTER = ""
    #: 价目表厂商名；只有端点是官方主机时才认。
    VENDOR = ""
    #: 官方端点主机名。价格是厂商定的，换主机就换个价目表——我们只认官方的。
    OFFICIAL_HOST = ""

    # ---- 能力声明（F9a）----
    #: 两个协议都支持 tools，而且这两个 adapter 确实在发 tools。
    SUPPORTS_TOOL_CALLING = True
    #: 一轮里返回多个工具调用：两边协议都允许，主循环也会逐个派发。
    SUPPORTS_PARALLEL_TOOL_CALLS = True
    #: W4：两个线上 adapter 均支持 SSE `stream_complete()`（无 `post_stream` 替身报 False）。
    SUPPORTS_STREAMING = True
    #: `thinking` / 扩展思考：不同 provider、不同模型差异太大，一律不声明。
    SUPPORTS_THINKING: bool | None = None
    #: 上下文窗口：窗口在供应商文档里是 token，没有 tokenizer 不换算成字节。
    CONTEXT_WINDOW_BYTES: int | None = None

    def capability_profile(self) -> CapabilityProfile:
        """这条 adapter + 这个模型的实际能力（F9a 准入用）。

        声明的是**适配器与模型合起来**的能力：运行时能用的就是两者的交集。
        """
        can_stream = bool(
            self.SUPPORTS_STREAMING and hasattr(self._transport, "post_stream")
        )
        return CapabilityProfile(
            provider=self.ADAPTER or type(self).__name__,
            model=self.model,
            supports_tool_calling=self.SUPPORTS_TOOL_CALLING,
            supports_parallel_tool_calls=self.SUPPORTS_PARALLEL_TOOL_CALLS,
            supports_streaming=can_stream,
            supports_thinking=self.SUPPORTS_THINKING,
            context_window_bytes=self.CONTEXT_WINDOW_BYTES,
            # 输出上界只报**实际会发给 provider 的那个数**：AnthropicAdapter 把
            # `max_tokens` 写进请求体（Messages API 还是必填），所以它是我们控制
            # 的、可验证的值。OpenAI-compatible 的请求体里没有 `max_tokens`
            # （输出由 provider 默认值决定）→ 这里如实是 None，硬上限模式会被拒，
            # 不拿别处的默认值冒充上界（PRD §5.6 第 3 条 / §5.7）。
            output_tokens=getattr(self, "max_tokens", None),
            declared_by=f"{type(self).__name__}（adapter 声明）",
        )

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        env_key: str,
        timeout: float,
        transport: Transport | None,
        cache_policy: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        # 存成 `Secret`：`f"Bearer {self.api_key}"` 照常工作（继承 str），
        # 但 repr / 日志里是掩码。同时登记进脱敏表，让事后任何 `redact()`
        # 都能抹掉它——包括我们没预料到的日志点。
        self.api_key = Secret(api_key)
        register_secret(api_key)
        self.timeout = timeout
        self._transport = transport or UrllibTransport()
        self.cache_policy = resolve_policy(
            cache_policy, adapter=self.ADAPTER, endpoint=self.base_url,
        )
        if not self.api_key:
            # 缺 key 是配置问题，不是"模型这一轮怎么结束的"，所以不走
            # finish_reason；抛错让 CLI 直接说清楚要设哪个环境变量。
            raise ModelError(
                f"缺少 API key。设置 {env_key}（可选 TIANSHU_BASE_URL 覆盖端点）。",
                kind="auth",
                retryable=False,
            )

    def __repr__(self) -> str:
        """配置对象的 repr：密钥永远打码。

        默认的对象 repr 只给地址，看着安全，但只要有人 `vars(model)`、
        把属性拼进日志、或者把它塞进更外层 dataclass 的 repr，明文就出去了。
        这里把"给人看"的那条路径直接钉成掩码。
        """
        key = MASK if self.api_key else None
        return (
            f"{type(self).__name__}(model={self.model!r}, "
            f"base_url={self.base_url!r}, api_key={key!r})"
        )

    def complete(self, messages, tools) -> ModelResponse:
        url, body, headers = self._request(messages, tools)
        data = self._transport.post(
            url, body=body, headers=headers, timeout=self.timeout
        )
        return self._parse(data)

    def price_identity(self) -> ModelIdentity:
        """见 `ModelIdentity`：端点不是官方主机时厂商记为未知。"""
        official = _is_official_endpoint(self.base_url, self.OFFICIAL_HOST)
        return ModelIdentity(
            adapter=self.ADAPTER or type(self).__name__,
            vendor=self.VENDOR if official else None,
            model=self.model,
        )

    def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[str, dict[str, Any], dict[str, str]]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def _parse(self, data: dict[str, Any]) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatibleModel(_HttpModel):
    """第一个线上协议：OpenAI-compatible 的 /chat/completions。

    覆盖 OpenAI / DeepSeek / Moonshot / 本地 vLLM / Ollama 的 /v1 接口。

    这也是"协议 ≠ 厂商"最典型的地方：同一个 adapter 指向代理站或自建服务时，
    价格不由 OpenAI 定。所以只有 `api.openai.com` 才认 OpenAI 的价目表。
    """

    ADAPTER = "openai-compatible"
    VENDOR = "openai"
    OFFICIAL_HOST = "api.openai.com"

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: Transport | None = None,
        cache_policy: str | None = None,
    ) -> None:
        super().__init__(
            model,
            base_url=base_url or os.environ.get("TIANSHU_BASE_URL")
            or "https://api.openai.com/v1",
            api_key=api_key if api_key is not None else _credential_for("openai"),
            env_key="TIANSHU_API_KEY",
            timeout=timeout,
            transport=transport,
            cache_policy=cache_policy,
        )

    def _request(self, messages, tools):
        _reject_continuation(messages)
        tools = stable_tools(tools, self.cache_policy)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": stable_system(_provider_text_messages(messages), self.cache_policy),
            "temperature": 0,
        }
        if tools:
            # C2 投影的键是 `input_schema`，OpenAI 要的是 `parameters`。
            # 只套一层 {"type": "function"} 是不够的：严格的端点直接 400
            # （Unrecognized request argument），宽松的网关会把整个参数 schema
            # 静默丢掉——模型于是只看得见工具名和描述，参数全靠猜。
            # 这条漏翻从 M0 就在，直到接第二个协议做对照才被发现。
            body["tools"] = [_openai_tool(t) for t in tools]
        if self.cache_policy != "off":
            body["prompt_cache_key"] = routing_key(
                adapter=self.ADAPTER, endpoint=self.base_url, model=self.model,
                policy=self.cache_policy, messages=messages, tools=tools,
            )
        return (
            f"{self.base_url}/chat/completions",
            body,
            {"Authorization": f"Bearer {self.api_key}"},
        )

    def _parse(self, data):
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("响应里没有 choices", kind="protocol")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ModelError("choices 里有非对象元素", kind="protocol")
        msg = choice.get("message")
        if not isinstance(msg, dict):
            raise ModelError("响应里 message 不是对象", kind="protocol")
        content = msg.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ModelError("响应里 content 不是文本", kind="protocol")
        finish = _map_finish(
            choice.get("finish_reason"), vendor="openai", excerpt=content
        )

        calls: list[ToolCall] = []
        tool_calls = msg.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            raise ModelError("响应里 tool_calls 不是数组", kind="protocol")
        for tc in tool_calls:
            if not isinstance(tc, dict):
                raise ModelError("tool_calls 里有非对象元素", kind="protocol")
            if tc.get("type") != "function":
                raise ModelError("未知 tool_calls 类型", kind="protocol")
            fn = tc.get("function")
            if not isinstance(fn, dict):
                raise ModelError("tool_calls function 不是对象", kind="protocol")
            call_id, name = tc.get("id"), fn.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise ModelError("tool_calls id 不是非空字符串", kind="protocol")
            if not isinstance(name, str) or not name:
                raise ModelError("tool_calls name 不是非空字符串", kind="protocol")
            raw = fn.get("arguments")
            if not isinstance(raw, str):
                raise ModelError("tool_calls arguments 不是 JSON 字符串", kind="protocol")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw  # 半截 JSON：原样带出，交给执行侧拒绝
            if not isinstance(parsed, dict):
                parsed = raw  # 非对象 JSON 同样不可作为完整工具参数派发
            calls.append(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=parsed,
                )
            )

        return ModelResponse(
            finish_reason=finish,
            content=content,
            tool_calls=calls,
            usage=_chat_usage(
                data.get("usage"), inclusive_input=self.price_identity().vendor == "openai",
            ),
        )

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[ModelStreamDelta], None],
    ) -> ModelResponse:
        if not hasattr(self._transport, "post_stream"):
            return self.complete(messages, tools)
        url, body, headers = self._request(messages, tools)
        body = {**body, "stream": True}
        if self.cache_policy != "off":
            body["stream_options"] = {"include_usage": True}
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        notified_tools: set[int] = set()
        raw_finish: Any = None
        raw_usage: dict[str, Any] | None = None

        for _ev, data_str in self._transport.post_stream(
            url, body=body, headers=headers, timeout=self.timeout
        ):
            stripped = data_str.strip()
            if not stripped:
                continue
            if stripped == "[DONE]":
                break
            try:
                chunk = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ModelError(f"流式 chunk 不是合法 JSON：{exc}", kind="protocol") from exc
            if not isinstance(chunk, dict):
                raise ModelError("流式 chunk 顶层不是对象", kind="protocol")
            if "error" in chunk and chunk["error"] is not None:
                raise ModelError(f"流式返回错误：{chunk['error']}", kind="protocol")
            usage_val = chunk.get("usage")
            if usage_val is not None:
                if not isinstance(usage_val, dict):
                    raise ModelError("流式 usage 不是对象", kind="protocol")
                raw_usage = usage_val
            choices = chunk.get("choices")
            if choices is None:
                continue
            if not isinstance(choices, list):
                raise ModelError("流式 choices 不是数组", kind="protocol")
            if not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ModelError("流式 choices 元素不是对象", kind="protocol")
            delta = choice.get("delta")
            if delta is not None and not isinstance(delta, dict):
                raise ModelError("流式 delta 不是对象", kind="protocol")
            if isinstance(delta, dict):
                piece = delta.get("content")
                if piece is not None and not isinstance(piece, str):
                    raise ModelError("流式 delta.content 不是文本", kind="protocol")
                if piece:
                    content_parts.append(piece)
                    on_delta(ModelStreamDelta(kind="text", text=piece))
                tcs = delta.get("tool_calls")
                if tcs is not None and not isinstance(tcs, list):
                    raise ModelError("流式 delta.tool_calls 不是数组", kind="protocol")
                if isinstance(tcs, list):
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            raise ModelError("流式 tool_calls 元素不是对象", kind="protocol")
                        idx = _valid_stream_index(tc.get("index"), where="OpenAI tool_calls")
                        slot = tool_acc.setdefault(
                            idx, {"id": "", "type": "", "name": "", "arguments": ""}
                        )
                        tc_id = tc.get("id")
                        if tc_id is not None:
                            if not isinstance(tc_id, str) or not tc_id:
                                raise ModelError(
                                    "流式 tool_calls.id 不是非空字符串", kind="protocol"
                                )
                            if slot["id"] and slot["id"] != tc_id:
                                raise ModelError("流式 tool_calls.id 前后冲突", kind="protocol")
                            slot["id"] = tc_id
                        tc_type = tc.get("type")
                        if tc_type is not None:
                            if not isinstance(tc_type, str) or not tc_type:
                                raise ModelError(
                                    "流式 tool_calls.type 不是非空字符串", kind="protocol"
                                )
                            slot["type"] = tc_type
                        fn = tc.get("function")
                        if fn is not None and not isinstance(fn, dict):
                            raise ModelError("流式 tool_calls.function 不是对象", kind="protocol")
                        if isinstance(fn, dict):
                            fn_name = fn.get("name")
                            if fn_name is not None:
                                if not isinstance(fn_name, str):
                                    raise ModelError(
                                        "流式 tool_calls.function.name 不是字符串",
                                        kind="protocol",
                                    )
                                if fn_name:
                                    slot["name"] += fn_name
                                    if idx not in notified_tools:
                                        notified_tools.add(idx)
                                        on_delta(
                                            ModelStreamDelta(
                                                kind="tool_call_start", tool_name=slot["name"]
                                            )
                                        )
                            fn_args = fn.get("arguments")
                            if fn_args is not None:
                                if not isinstance(fn_args, str):
                                    raise ModelError(
                                        "流式 tool_calls.function.arguments 不是字符串",
                                        kind="protocol",
                                    )
                                slot["arguments"] += fn_args
            if choice.get("finish_reason") is not None:
                raw_finish = choice.get("finish_reason")

        if raw_finish is None:
            raise ModelError(
                "OpenAI 流式响应提前结束：缺少 finish_reason（拒绝伪造 end_turn）",
                kind="protocol",
            )
        assembled_tools = [
            {
                "id": slot["id"],
                "type": slot["type"],
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            }
            for _, slot in sorted(tool_acc.items())
        ]
        synthesized: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "content": "".join(content_parts),
                        "tool_calls": assembled_tools,
                    },
                    "finish_reason": raw_finish,
                }
            ],
            "usage": raw_usage,
        }
        return self._parse(synthesized)


class AnthropicMessagesModel(_HttpModel):
    """第二个线上协议：Anthropic Messages API。

    和 OpenAI 的差别不是"换个 base_url"能糊过去的，正好用来验证接缝：

    - `system` 是**顶层参数**，不是 messages 里的一条
    - assistant 的工具调用是 `tool_use` 内容块，工具结果是**下一条 user 消息**
      里的 `tool_result` 块（于是"连续多条 tool 消息"必须合并进同一条 user）
    - 工具定义是 `{name, description, input_schema}`——恰好等于本项目的
      `tool_manifest()` 形状，不需要再包一层 `function`
    - `stop_reason` 的语料与 OpenAI 不同（`end_turn` / `max_tokens` /
      `stop_sequence` / `tool_use` / `refusal` / `model_context_window_exceeded`）；
      线上还可能给 `pause_turn`（服务端把长轮次挂起），v1 没有续接它的能力，
      所以**不映射**、直接报错，而不是猜成 `end_turn`
    """

    ADAPTER = "anthropic"
    VENDOR = "anthropic"
    OFFICIAL_HOST = "api.anthropic.com"

    API_VERSION = "2023-06-01"

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
        transport: Transport | None = None,
        cache_policy: str | None = None,
    ) -> None:
        super().__init__(
            model,
            # 只认 ANTHROPIC_*：`TIANSHU_BASE_URL` 通常是给 OpenAI 兼容端点配的，
            # 拿它去拼 /messages 会把请求打到 `.../v1/messages` 上然后 404，
            # 而 `TIANSHU_API_KEY` 更糟——那是把一家的 key 发给另一家。
            base_url=base_url
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com/v1",
            api_key=api_key
            if api_key is not None
            else _credential_for("anthropic"),
            env_key="ANTHROPIC_API_KEY",
            timeout=timeout,
            transport=transport,
            cache_policy=cache_policy,
        )
        # Messages API 里 max_tokens 是**必填**的（OpenAI 那边是可选）。
        self.max_tokens = max_tokens

    def _request(self, messages, tools):
        _reject_continuation(messages)
        tools = stable_tools(tools, self.cache_policy)
        system, converted = to_anthropic_messages(_provider_text_messages(messages))
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": converted,
            "temperature": 0,
        }
        if system:
            body["system"] = system
        if tools:
            # 本项目的 C2 投影已经是 {name, description, input_schema}
            body["tools"] = [dict(t) for t in tools]
        if self.cache_policy != "off":
            anthropic_breakpoints(body)
        return (
            f"{self.base_url}/messages",
            body,
            {"x-api-key": self.api_key, "anthropic-version": self.API_VERSION},
        )

    def _parse(self, data):
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ModelError("响应里没有 content 数组", kind="protocol")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise ModelError("content 里有非对象元素", kind="protocol")
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ModelError("text 内容块不是文本", kind="protocol")
                text_parts.append(text)
            elif kind == "tool_use":
                call_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(call_id, str) or not call_id:
                    raise ModelError("tool_use id 不是非空字符串", kind="protocol")
                if not isinstance(name, str) or not name:
                    raise ModelError("tool_use name 不是非空字符串", kind="protocol")
                if not isinstance(arguments, dict):
                    raise ModelError("tool_use input 不是对象", kind="protocol")
                calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
            elif kind in ("thinking", "redacted_thinking"):
                # v1 不做 ContinuationRef 往返（PRD §5.1 要求原样往返，但 v1 的
                # 两个 provider 都没有必须往返的 opaque 字段）。跳过并留痕在
                # 文档的未验证清单里，而不是假装看懂了。
                continue
            else:
                raise ModelError(f"未知内容块类型：{kind!r}", kind="protocol")

        content = "".join(text_parts)
        return ModelResponse(
            finish_reason=_map_finish(
                data.get("stop_reason"), vendor="anthropic", excerpt=content
            ),
            content=content,
            tool_calls=calls,
            usage=normalize_usage(data.get("usage")),
        )

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[ModelStreamDelta], None],
    ) -> ModelResponse:
        if not hasattr(self._transport, "post_stream"):
            return self.complete(messages, tools)
        url, body, headers = self._request(messages, tools)
        body = {**body, "stream": True}
        blocks_by_idx: dict[int, dict[str, Any]] = {}
        stop_reason: Any = None
        merged_usage: dict[str, Any] = {}
        saw_stop = False

        for ev_name, data_str in self._transport.post_stream(
            url, body=body, headers=headers, timeout=self.timeout
        ):
            stripped = data_str.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ModelError(f"Anthropic 流式帧不是合法 JSON：{exc}", kind="protocol") from exc
            if not isinstance(event, dict):
                raise ModelError("Anthropic 流式帧顶层不是对象", kind="protocol")
            etype = event.get("type", ev_name)
            if not isinstance(etype, str) or not etype:
                raise ModelError("Anthropic 流式帧缺少合法 type", kind="protocol")
            if etype == "error":
                raise ModelError(f"Anthropic 流式错误：{event.get('error')}", kind="protocol")
            if etype == "ping":
                continue
            if etype == "message_start":
                msg = event.get("message")
                if not isinstance(msg, dict):
                    raise ModelError("Anthropic message_start.message 不是对象", kind="protocol")
                usage_val = msg.get("usage")
                if usage_val is not None:
                    if not isinstance(usage_val, dict):
                        raise ModelError("Anthropic message_start.usage 不是对象", kind="protocol")
                    merged_usage.update(usage_val)
            elif etype == "content_block_start":
                idx = _valid_stream_index(
                    event.get("index"), where="Anthropic content_block_start"
                )
                if idx in blocks_by_idx:
                    raise ModelError(
                        f"Anthropic 重复的 content_block_start：index={idx}", kind="protocol"
                    )
                block = event.get("content_block")
                if not isinstance(block, dict):
                    raise ModelError(
                        "Anthropic content_block_start.content_block 不是对象", kind="protocol"
                    )
                btype = block.get("type")
                if btype == "text":
                    initial = block.get("text")
                    if not isinstance(initial, str):
                        raise ModelError("text 内容块不是文本", kind="protocol")
                    blocks_by_idx[idx] = {"type": "text", "text": initial, "stopped": False}
                    if initial:
                        on_delta(ModelStreamDelta(kind="text", text=initial))
                elif btype == "tool_use":
                    cid = block.get("id")
                    tname = block.get("name")
                    init_in = block.get("input")
                    if not isinstance(cid, str) or not cid:
                        raise ModelError("tool_use id 不是非空字符串", kind="protocol")
                    if not isinstance(tname, str) or not tname:
                        raise ModelError("tool_use name 不是非空字符串", kind="protocol")
                    if not isinstance(init_in, dict):
                        raise ModelError("tool_use input 不是对象", kind="protocol")
                    blocks_by_idx[idx] = {
                        "type": "tool_use",
                        "id": cid,
                        "name": tname,
                        "initial_input": dict(init_in),
                        "partial_json": "",
                        "saw_json_delta": False,
                        "stopped": False,
                    }
                    on_delta(ModelStreamDelta(kind="tool_call_start", tool_name=tname))
                elif btype in ("thinking", "redacted_thinking"):
                    blocks_by_idx[idx] = {"type": btype, "stopped": False}
                else:
                    raise ModelError(f"未知内容块类型：{btype!r}", kind="protocol")
            elif etype == "content_block_delta":
                idx = _valid_stream_index(
                    event.get("index"), where="Anthropic content_block_delta"
                )
                if idx not in blocks_by_idx:
                    raise ModelError(
                        f"Anthropic content_block_delta 引用未打开的块：index={idx}",
                        kind="protocol",
                    )
                target = blocks_by_idx[idx]
                if target.get("stopped"):
                    raise ModelError(
                        f"Anthropic content_block_delta 晚于 content_block_stop：index={idx}",
                        kind="protocol",
                    )
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    raise ModelError(
                        "Anthropic content_block_delta.delta 不是对象", kind="protocol"
                    )
                dtype = delta.get("type")
                if dtype == "text_delta":
                    if target.get("type") != "text":
                        raise ModelError("text_delta 目标内容块不是 text", kind="protocol")
                    piece = delta.get("text")
                    if not isinstance(piece, str):
                        raise ModelError("text_delta.text 不是字符串", kind="protocol")
                    if piece:
                        target["text"] = target["text"] + piece
                        on_delta(ModelStreamDelta(kind="text", text=piece))
                elif dtype == "input_json_delta":
                    if target.get("type") != "tool_use":
                        raise ModelError(
                            "input_json_delta 目标内容块不是 tool_use", kind="protocol"
                        )
                    pjson = delta.get("partial_json")
                    if not isinstance(pjson, str):
                        raise ModelError(
                            "input_json_delta.partial_json 不是字符串", kind="protocol"
                        )
                    target["saw_json_delta"] = True
                    target["partial_json"] = target["partial_json"] + pjson
                elif dtype in ("thinking_delta", "signature_delta"):
                    if target.get("type") not in ("thinking", "redacted_thinking"):
                        raise ModelError(
                            f"{dtype} 目标内容块不是 thinking", kind="protocol"
                        )
                else:
                    raise ModelError(f"未知 Anthropic delta 类型：{dtype!r}", kind="protocol")
            elif etype == "content_block_stop":
                idx = _valid_stream_index(
                    event.get("index"), where="Anthropic content_block_stop"
                )
                if idx not in blocks_by_idx:
                    raise ModelError(
                        f"Anthropic content_block_stop 引用未打开的块：index={idx}",
                        kind="protocol",
                    )
                if blocks_by_idx[idx].get("stopped"):
                    raise ModelError(
                        f"Anthropic 重复的 content_block_stop：index={idx}", kind="protocol"
                    )
                blocks_by_idx[idx]["stopped"] = True
            elif etype == "message_delta":
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    raise ModelError("Anthropic message_delta.delta 不是对象", kind="protocol")
                if delta.get("stop_reason") is not None:
                    stop_reason = delta.get("stop_reason")
                usage_val = event.get("usage")
                if usage_val is not None:
                    if not isinstance(usage_val, dict):
                        raise ModelError("Anthropic message_delta.usage 不是对象", kind="protocol")
                    merged_usage.update(usage_val)
            elif etype == "message_stop":
                saw_stop = True
                break
            else:
                raise ModelError(f"未知 Anthropic 流式事件类型：{etype!r}", kind="protocol")

        if not saw_stop or stop_reason is None:
            raise ModelError(
                "Anthropic 流式响应提前结束：缺少 message_stop 或 stop_reason（拒绝伪造 end_turn）",
                kind="protocol",
            )
        unstopped = [
            idx for idx, blk in sorted(blocks_by_idx.items()) if not blk.get("stopped")
        ]
        if unstopped:
            raise ModelError(
                f"Anthropic 流式内容块缺少 content_block_stop：index={unstopped}",
                kind="protocol",
            )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for _, blk in sorted(blocks_by_idx.items()):
            btype = blk["type"]
            if btype == "text":
                text_parts.append(blk["text"])
            elif btype == "tool_use":
                cid = blk["id"]
                tname = blk["name"]
                init_in = blk["initial_input"]
                if blk["saw_json_delta"]:
                    if init_in:
                        raise ModelError(
                            "Anthropic tool_use 同时携带非空初始 input 与 input_json_delta",
                            kind="protocol",
                        )
                    raw_json = blk["partial_json"]
                    try:
                        parsed_in: Any = json.loads(raw_json)
                    except json.JSONDecodeError:
                        parsed_in = raw_json
                    if not isinstance(parsed_in, dict):
                        parsed_in = raw_json
                else:
                    parsed_in = dict(init_in)
                calls.append(ToolCall(call_id=cid, name=tname, arguments=parsed_in))
        full_content = "".join(text_parts)
        return ModelResponse(
            finish_reason=_map_finish(stop_reason, vendor="anthropic", excerpt=full_content),
            content=full_content,
            tool_calls=calls,
            usage=normalize_usage(merged_usage),
        )


def _valid_stream_index(val: Any, *, where: str) -> int:
    if not isinstance(val, int) or isinstance(val, bool) or val < 0:
        raise ModelError(f"{where} index 必须是非负整数", kind="protocol")
    return val


def _openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """C2 投影 → OpenAI 的 function 定义。

    `input_schema` → `parameters` 是这里唯一的实质翻译。漏了它，请求体里
    就没有参数 schema：严格端点 400，宽松网关静默丢弃，模型只能靠描述猜参数名。
    """
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        },
    }


def _map_finish(raw: Any, *, vendor: str, excerpt: str = "") -> FinishReason:
    """线上语料 → 13 值。**不认识的必须报错，不许兜底成成功。**

    上一版的兜底 `"unknown"` 有两个问题：它不在封闭集里（契约破了），而且
    主循环会因为"不是 end_turn"而把 Run 记成失败——碰巧行为是对的，但代价是
    事件日志里多了一个永远不该出现的原因值。
    """
    table = {
        "openai": {
            "stop": "end_turn",
            "length": "limit_output_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "content_filtered",
        },
        "anthropic": {
            "end_turn": "end_turn",
            "max_tokens": "limit_output_tokens",
            "stop_sequence": "stop_sequence",
            "tool_use": "tool_use",
            "refusal": "content_filtered",
            "model_context_window_exceeded": "limit_total_tokens",
        },
    }[vendor]
    if isinstance(raw, str) and raw in table:
        return table[raw]
    # `pause_turn`（Anthropic 服务端把长轮次挂起）故意不映射：v1 没有续接服务端
    # 轮次的能力，猜成 end_turn 等于把"没跑完"记成"跑完了"。
    note = ""
    if excerpt:
        # 这一轮的模型原文与 usage 会随异常一起进 `model.error` 事件。
        # 不带摘要的话，事后只知道"结束原因看不懂"，不知道模型当时说了什么。
        note = f"；该轮模型原文前 200 字：{excerpt[:200]!r}"
    raise ModelError(
        f"未映射的结束原因（{vendor}）：{raw!r}——拒绝猜成 end_turn，"
        f"因为那会把一次异常收尾记成成功{note}",
        kind="protocol",
    )


def to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """内部规范化形状 → Messages API 的 (system, messages)。

    三条 Anthropic 的硬约束，都必须在这里消化掉，不能留给线上报 400：

    1. `system` 要提到顶层，且只能一条（多条用空行拼）
    2. 角色必须交替；同角色相邻消息要合并
    3. 一次 assistant 的多个 `tool_use`，其结果必须全部出现在**紧随其后的
       那条 user 消息**里
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    # 上一条 assistant 里发出去的 tool_use id。工具结果只有落在这一组里才能
    # 用 `tool_result` 表达；否则 Anthropic 会因为"找不到对应的 tool_use"拒整轮。
    pending_ids: set[str] = set()

    for m in messages:
        role = m.get("role")
        if role == "system":
            text = str(m.get("content") or "")
            if text:
                system_parts.append(text)
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            emitted: set[str] = set()
            pending_ids = set()
            text = m.get("content")
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                call_id = tc.get("id") or ""
                if not call_id or call_id in emitted:
                    # 同一个 id 在一次 assistant 里出现两次：主循环那边这条调用
                    # 一定被判成"未派发"并留了一条说明性的 tool 消息。这里绝不能
                    # 发两个同 id 的 tool_use（Anthropic 会 400），只发第一个；
                    # 第二条的结果在下面按"无配对的工具结果"降级成文字。
                    continue
                emitted.add(call_id)
                pending_ids.add(call_id)
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    # 半截 JSON：当时就是"未派发 + 一条说明性 tool 消息"，
                    # 这里给空对象即可——不伪造参数，也不让历史把整轮打挂。
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
            if not blocks:
                # 既没文字也没工具调用的轮次不携带信息。发空 text 块会被拒。
                continue
            push("assistant", blocks)
            continue

        if role == "tool":
            call_id = m.get("tool_call_id") or ""
            content = m.get("content") or ""
            if call_id in pending_ids:
                pending_ids.discard(call_id)
                push(
                    "user",
                    [{"type": "tool_result", "tool_use_id": call_id, "content": content}],
                )
            else:
                # 没有配对的 tool_use（重复 id 的第二条、历史被裁剪过、或旧数据）。
                # 硬发 tool_result 会让整轮 400，降级成一条文字把信息留住。
                push("user", [{"type": "text", "text": f"（工具结果 {call_id}：{content}）"}])
            continue

        push("user", [{"type": "text", "text": str(m.get("content") or "")}])

    if not out:
        raise ModelError(
            "对话里没有可发送的消息（只有 system？）——Messages API 至少要一条 user",
            kind="protocol",
        )
    if out[0]["role"] != "user":
        raise ModelError(
            "对话以 assistant 开头；Messages API 要求首条消息是 user",
            kind="protocol",
        )

    return "\n\n".join(system_parts), out


def _is_official_endpoint(base_url: str, host: str) -> bool:
    """端点是不是厂商官方主机。

    只认 https + 精确主机名。`http://api.openai.com`（明文）与
    `api.openai.com.evil.example`（后缀陷阱）都不算——价格表绑的是"你确实在跟
    这家厂商结算"，认错一次就会给出一个错的金额。
    """
    if not host:
        return False
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == host.lower()


__all__ = [
    "FINISH_REASONS",
    "AnthropicMessagesModel",
    "Model",
    "ModelError",
    "ModelIdentity",
    "ModelResponse",
    "ModelStreamDelta",
    "OpenAICompatibleModel",
    "RecordedModel",
    "ScriptedModel",
    "ToolCall",
    "Transport",
    "UrllibTransport",
    "identity_of",
    "normalize_usage",
    "to_anthropic_messages",
]
