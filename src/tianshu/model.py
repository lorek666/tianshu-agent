"""模型接缝（C1）。

不绑定任何厂商：核心只认 Model 协议。换模型 = 换 adapter，主循环不动。
M0 提供三个实现：
  - ScriptedModel   离线确定性，验收测试用
  - RecordedModel   回放用，答案来自事件日志
  - OpenAICompatibleModel  真实调用（需要 key）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

# PRD §5.5：模型侧的结束原因，13 值封闭集合
FinishReason = str

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

    def to_payload(self) -> dict[str, Any]:
        return {
            "finish_reason": self.finish_reason,
            "content": self.content,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in self.tool_calls
            ],
            "usage": self.usage,
        }

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> ModelResponse:
        return cls(
            finish_reason=p["finish_reason"],
            content=p.get("content", ""),
            tool_calls=[
                ToolCall(c["call_id"], c["name"], c["arguments"])
                for c in p.get("tool_calls", [])
            ],
            usage=p.get("usage", {}),
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
            out[dst] = v
    return out


class Model(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


# ---------------------------------------------------------------- 离线实现


class ScriptedModel:
    """按脚本依次返回。脚本用完即视为 end_turn（避免无限循环掩盖 bug）。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        self.calls += 1
        if not self._responses:
            return ModelResponse(finish_reason="end_turn", content="")
        return self._responses.pop(0)


class RecordedModel:
    """回放用：按 request_id 顺序吐出事件里记录的响应。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def complete(self, messages, tools) -> ModelResponse:  # noqa: ARG002
        if not self._responses:
            raise RuntimeError("回放中断：事件日志里的模型响应少于实际调用次数")
        return self._responses.pop(0)


# ---------------------------------------------------------------- 真实实现


class OpenAICompatibleModel:
    """最小 OpenAI-compatible 客户端。零第三方依赖，用 stdlib urllib。

    覆盖 OpenAI / DeepSeek / Moonshot / 本地 vLLM / Ollama 的 /v1 接口。
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.base_url = (
            base_url or os.environ.get("TIANSHU_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("TIANSHU_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError(
                "缺少 API key。设置 TIANSHU_API_KEY（可选 TIANSHU_BASE_URL）。"
            )

    def complete(self, messages, tools) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            return ModelResponse(
                finish_reason="error",  # type: ignore[arg-type]
                content=f"HTTP {exc.code}: {detail}",
            )
        except urllib.error.URLError as exc:
            return ModelResponse(
                finish_reason="error",  # type: ignore[arg-type]
                content=f"网络错误：{exc.reason}",
            )

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish = self._map_finish(choice.get("finish_reason"))

        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                parsed = raw  # 半截 JSON：原样带出，交给执行侧拒绝
            calls.append(
                ToolCall(
                    call_id=tc.get("id") or f"call_{i}",
                    name=fn.get("name", ""),
                    arguments=parsed,
                )
            )

        return ModelResponse(
            finish_reason=finish,
            content=msg.get("content") or "",
            tool_calls=calls,
            usage=normalize_usage(data.get("usage")),
        )

    @staticmethod
    def _map_finish(raw: str | None) -> FinishReason:
        return {
            "stop": "end_turn",
            "length": "limit_output_tokens",
            "tool_calls": "tool_use",
            "content_filter": "content_filtered",
        }.get(raw or "", "end_turn")
