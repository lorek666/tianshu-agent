"""公开 Responses 文本/函数协议；store:false，加密 reasoning 原样续接。"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from typing import Any

from .continuation import (
    COMPACTION_REF_VERSION,
    ContinuationRef,
    project_output,
    require_scope,
    responses_endpoint,
    validate_compact_threshold,
)
from .model import (
    ModelError,
    ModelResponse,
    ModelStreamDelta,
    ToolCall,
    Transport,
    UrllibTransport,
    _credential_for,
    _HttpModel,
    _native_cache_usage,
)
from .prompt_cache import responses_breakpoints, routing_key, stable_system, stable_tools
from .task_attachments import AttachmentError, text_attachment, validated_attachments


def _protocol() -> ModelError:
    # 不把不可信 response、SSE 或回显的加密输入放进异常 / 事件。
    return ModelError("Responses 响应不满足公开文本/函数续接协议", kind="protocol")


def _request_error(exc: ModelError) -> ModelError:
    # Stateless conversation transport does not make a POST safe to replay. urllib
    # cannot prove an ambiguous transport error occurred before the request was sent.
    return ModelError(
        "Responses 调用失败（响应细节已隐藏）", kind=exc.kind,
        status_code=exc.status_code,
        retryable=exc.retryable and exc.kind == "rate_limit" and exc.status_code == 429,
    )


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _protocol()
    return value


def _usage(raw: Any) -> dict[str, int | None]:
    if raw is None:
        return {"input_tokens": None, "output_tokens": None}
    if not isinstance(raw, dict):
        raise _protocol()
    usage = {k: _count(raw.get(k)) for k in ("input_tokens", "output_tokens")}
    details = raw.get("input_tokens_details")
    if details is not None:
        try:
            _native_cache_usage(usage, details, require_total=True)
        except ModelError:
            raise _protocol() from None
    # reasoning_tokens 已含在 output_tokens 中，不做第二次相加。
    return usage


class OpenAIResponsesModel(_HttpModel):
    ADAPTER = "openai-responses"
    VENDOR = "openai"
    OFFICIAL_HOST = "api.openai.com"
    SUPPORTS_THINKING = True

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: Transport | None = None,
        allow_image_attachments: bool = False,
        compact_threshold: int | None = None,
        cache_policy: str | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Responses 必须显式指定 model")
        endpoint = responses_endpoint(
            base_url or os.environ.get("TIANSHU_BASE_URL") or "https://api.openai.com/v1"
        )
        super().__init__(
            model, base_url=endpoint.removesuffix("/responses"),
            api_key=api_key if api_key is not None else _credential_for("openai"),
            env_key="TIANSHU_API_KEY", timeout=timeout,
            transport=(
                transport if transport is not None
                else UrllibTransport(allow_redirects=False, strict_framing=True)
            ),
            cache_policy=cache_policy,
        )
        self._endpoint = endpoint
        self.compact_threshold = validate_compact_threshold(compact_threshold, responses=True)
        self.supports_image_attachments = allow_image_attachments is True

    def continuation_scope(self) -> tuple[str, str, str]:
        return self.ADAPTER, self._endpoint, self.model

    def _request(self, messages, tools):
        tools = stable_tools(tools, self.cache_policy)
        try:
            require_scope(messages, self.continuation_scope())
            items: list[dict[str, Any]] = []
            for msg in messages:
                if "continuation" in msg:
                    ref = ContinuationRef.from_payload(msg["continuation"])
                    # 双重表示必须一致，不能用一组提案展示、另一组 bytes 回传。
                    from .model_messages import assistant_message_from_payload

                    raw = ref.to_payload()["output_items"]
                    if ref.has_compaction and self.compact_threshold is None:
                        raise ValueError
                    text, calls, *_ = project_output(raw, compaction=ref.has_compaction)
                    projected = assistant_message_from_payload({
                        "content": text, "tool_calls": calls, "continuation": ref.to_payload(),
                    })
                    if any(msg.get(k) != projected.get(k) for k in ("content", "tool_calls")):
                        raise ValueError
                    items.extend(raw)
                elif "attachments" in msg:
                    if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
                        raise AttachmentError("附件只能随用户消息发送")
                    parts = [{"type": "input_text", "text": msg["content"]}]
                    for attachment, raw in validated_attachments(msg["attachments"]):
                        mime = attachment["media_type"]
                        if mime == "text/plain":
                            parts.append({"type": "input_text",
                                          "text": text_attachment(attachment["name"], raw)})
                        elif self.supports_image_attachments:
                            encoded = attachment["data_base64"]
                            parts.append({"type": "input_image", "detail": "auto",
                                          "image_url": f"data:{mime};base64,{encoded}"})
                        else:
                            raise AttachmentError("所选模型未开启图片附件能力")
                    items.append({"role": "user", "content": parts})
                elif msg.get("role") == "tool":
                    items.append({
                        "type": "function_call_output", "call_id": msg["tool_call_id"],
                        "output": msg["content"],
                    })
                elif msg.get("role") in ("user", "system", "assistant"):
                    if msg.get("tool_calls") or not isinstance(msg.get("content"), str):
                        # 本模式不把别的协议的 assistant 函数历史伪造成 Responses。
                        raise ValueError
                    items.append(copy.deepcopy(msg))
                else:
                    raise ValueError
        except AttachmentError as exc:
            raise ModelError(str(exc), kind="protocol", retryable=False) from None
        except (ValueError, TypeError, KeyError):
            raise ModelError("模型续接作用域或消息不兼容，请创建新 Run", kind="protocol") from None
        body = {
            "model": self.model,
            "input": stable_system(items, self.cache_policy),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "truncation": "disabled",
            "service_tier": "default",
            "tools": [
                {"type": "function", "name": t["name"], "description": t["description"],
                 "parameters": copy.deepcopy(t["input_schema"]), "strict": False}
                for t in tools
            ],
        }
        if self.compact_threshold is not None:
            body["context_management"] = [
                {"type": "compaction", "compact_threshold": self.compact_threshold}
            ]
        if self.cache_policy != "off":
            body["prompt_cache_key"] = routing_key(
                adapter=self.ADAPTER, endpoint=self._endpoint, model=self.model,
                policy=self.cache_policy, messages=messages, tools=tools,
            )
        if self.cache_policy == "responses-explicit-v1":
            responses_breakpoints(body)
        return self._endpoint, body, {"Authorization": f"Bearer {self.api_key}"}

    def _parse(self, data):
        if (
            not isinstance(data, dict) or not isinstance(data.get("id"), str)
            or not data["id"] or data.get("status") not in ("completed", "incomplete")
            or data.get("error") is not None or data.get("store") is True
            or data.get("object", "response") != "response"
        ):
            raise _protocol()
        try:
            output = data.get("output")
            has_compaction = isinstance(output, list) and any(
                isinstance(item, dict) and item.get("type") == "compaction" for item in output
            )
            if has_compaction and (self.compact_threshold is None or data["status"] != "completed"):
                raise _protocol()
            content, calls, refusal, complete, visible = project_output(output,
                                                                         compaction=has_compaction)
            if data["status"] == "incomplete":
                reason = (data.get("incomplete_details") or {}).get("reason")
                finish = {
                    "max_output_tokens": "limit_output_tokens",
                    "content_filter": "content_filtered",
                }.get(reason)
                if finish is None:
                    raise _protocol()
            else:
                if not complete or not visible or data.get("incomplete_details") is not None:
                    raise _protocol()
                finish = "content_filtered" if refusal else "tool_use" if calls else "end_turn"
            ref = ContinuationRef({
                "schema_version": COMPACTION_REF_VERSION if has_compaction else "1.0",
                "adapter": self.ADAPTER,
                "endpoint": self._endpoint, "model": self.model,
                "output_items": data["output"],
            })
            return ModelResponse(
                finish, content, [ToolCall(**c) for c in calls], _usage(data.get("usage")), ref,
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise _protocol() from None

    def complete(self, messages, tools) -> ModelResponse:
        url, body, headers = self._request(messages, tools)
        try:
            data = self._transport.post(url, body=body, headers=headers, timeout=self.timeout)
            return self._parse(data)
        except ModelError as exc:
            raise _request_error(exc) from None
        except Exception:  # noqa: BLE001 - 不反射不可信传输对象
            raise _protocol() from None

    def stream_complete(
        self, messages, tools, on_delta: Callable[[ModelStreamDelta], None],
    ) -> ModelResponse:
        if not hasattr(self._transport, "post_stream"):
            return self.complete(messages, tools)
        url, body, headers = self._request(messages, tools)
        stream = _ResponseStream(on_delta)
        try:
            for name, text in self._transport.post_stream(
                url, body={**body, "stream": True}, headers=headers, timeout=self.timeout,
            ):
                stream.feed(name, text)
            if stream.terminal is None:
                raise _protocol()
            return self._parse(stream.terminal)
        except ModelError as exc:
            raise _request_error(exc) from None
        except Exception:  # noqa: BLE001 - 包括畸形 JSON，禁止密文进入错误事件
            raise _protocol() from None


class _ResponseStream:
    """有序生命周期核对；done 快照与已观察增量一致后才接受最终 output。"""

    def __init__(self, emit: Callable[[ModelStreamDelta], None]) -> None:
        self.emit = emit
        self.sequence = -1
        self.response_id: str | None = None
        self.started = False
        self.items: list[dict[str, Any]] = []
        self.done: dict[int, dict[str, Any]] = {}
        self.parts: dict[tuple[int, str, int], dict[str, Any]] = {}
        self.terminal: dict[str, Any] | None = None

    def _item(self, ev: dict[str, Any], expected: str | None = None) -> tuple[int, dict]:
        index = ev.get("output_index")
        if (
            type(index) is not int or index < 0 or index >= len(self.items)
            or index in self.done
        ):
            raise _protocol()
        item = self.items[index]
        if ev.get("item_id") != item["id"] or (
            expected is not None and item["type"] != expected
        ):
            raise _protocol()
        return index, item

    def _part(
        self, ev: dict[str, Any], family: str, index_key: str, expected: str,
    ) -> dict[str, Any]:
        index, _ = self._item(ev, expected)
        position = ev.get(index_key, 0) if family == "arguments" else ev.get(index_key)
        if type(position) is not int or position < 0:
            raise _protocol()
        key = index, family, position
        return self.parts.setdefault(key, {"text": "", "seen": False})

    def feed(self, name: str, text: str) -> None:
        ev = json.loads(text)
        if not isinstance(ev, dict) or self.terminal is not None:
            raise _protocol()
        kind = ev.get("type")
        sequence = ev.get("sequence_number")
        if (
            not isinstance(kind, str) or name not in ("message", kind)
            or type(sequence) is not int or sequence != self.sequence + 1
        ):
            raise _protocol()
        self.sequence = sequence
        if "response_id" in ev and ev["response_id"] != self.response_id:
            raise _protocol()
        if kind in ("response.created", "response.in_progress"):
            resp = ev.get("response")
            if not isinstance(resp, dict) or resp.get("status") != "in_progress":
                raise _protocol()
            rid = resp.get("id")
            if not isinstance(rid, str) or not rid or (
                self.response_id is not None and self.response_id != rid
            ):
                raise _protocol()
            if kind == "response.created":
                if self.started:
                    raise _protocol()
                self.started = True
            elif not self.started:
                raise _protocol()
            self.response_id = rid
            return
        if not self.started:
            raise _protocol()
        if kind in ("response.completed", "response.incomplete"):
            resp = ev.get("response")
            if (
                not isinstance(resp, dict) or resp.get("id") != self.response_id
                or kind != f"response.{resp.get('status')}"
                or len(self.done) != len(self.items)
                or resp.get("output") != [self.done[n] for n in range(len(self.items))]
            ):
                raise _protocol()
            self.terminal = resp
            return
        if kind == "response.output_item.added":
            index, item = ev.get("output_index"), ev.get("item")
            if (
                type(index) is not int or index != len(self.items)
                or not isinstance(item, dict)
                or item.get("type") not in ("reasoning", "message", "function_call", "compaction")
                or not isinstance(item.get("id"), str) or not item["id"]
                or any(i["id"] == item["id"] for i in self.items)
                or ("status" in item and item["status"] not in (
                    "in_progress", "completed", "incomplete",
                ))
                or ("encrypted_content" in item and item["encrypted_content"] is not None
                    and not isinstance(item["encrypted_content"], str))
            ):
                raise _protocol()
            self.items.append(item)
            self._seed_item(index, item)
            if item["type"] == "function_call":
                if not isinstance(item.get("name"), str) or not item["name"]:
                    raise _protocol()
                self.emit(ModelStreamDelta(kind="tool_call_start", tool_name=item["name"]))
            return
        if kind == "response.output_item.done":
            item = ev.get("item")
            if not isinstance(item, dict):
                raise _protocol()
            index, start = self._item({**ev, "item_id": item.get("id")})
            if item.get("type") != start["type"]:
                raise _protocol()
            for field in ("id", "type", "role", "phase", "call_id", "name"):
                if field in start and start[field] != item.get(field):
                    raise _protocol()
            if start.get("encrypted_content") and (
                start["encrypted_content"] != item.get("encrypted_content")
            ):
                raise _protocol()
            if start.get("status") in ("completed", "incomplete") and (
                start["status"] != item.get("status")
            ):
                raise _protocol()
            self._verify_parts(index, item)
            self.done[index] = item
            return
        # 三种 text 生命周期共用同一个核对规则，reasoning 从不推给预览。
        families = {
            "response.output_text": ("text", "content_index", "message", "text", True),
            "response.refusal": ("refusal", "content_index", "message", "refusal", True),
            "response.function_call_arguments": (
                "arguments", "_argument_index", "function_call", "arguments", False,
            ),
            "response.reasoning_summary_text": (
                "summary", "summary_index", "reasoning", "text", False,
            ),
            "response.reasoning_text": ("reasoning", "content_index", "reasoning", "text", False),
        }
        prefix, _, suffix = kind.rpartition(".")
        if prefix in families and suffix in ("delta", "done"):
            family, index_key, expected, value_key, preview = families[prefix]
            if expected == "function_call":
                _, item = self._item(ev, expected)
                if any(
                    field in ev and ev[field] != item.get(field)
                    for field in ("name", "call_id")
                ):
                    raise _protocol()
            part = self._part(ev, family, index_key, expected)
            if part.get("closed") or part.get("text_done"):
                raise _protocol()
            value = ev.get("delta" if suffix == "delta" else value_key)
            if not isinstance(value, str):
                raise _protocol()
            if suffix == "delta":
                part.update(text=part["text"] + value, seen=True)
                if preview and value:
                    self.emit(ModelStreamDelta(kind="text", text=value))
            else:
                if part["seen"] and value != part["text"]:
                    raise _protocol()
                part.update(text=value, seen=True, text_done=True)
            return
        if prefix in ("response.content_part", "response.reasoning_summary_part") and suffix in (
            "added", "done",
        ):
            part = ev.get("part")
            if not isinstance(part, dict):
                raise _protocol()
            summary = prefix == "response.reasoning_summary_part"
            part_type = part.get("type")
            mapping = {"output_text": ("text", "message"), "refusal": ("refusal", "message"),
                       "reasoning_text": ("reasoning", "reasoning")}
            if summary:
                if part_type != "summary_text":
                    raise _protocol()
                family, expected = "summary", "reasoning"
            elif part_type in mapping:
                family, expected = mapping[part_type]
            else:
                raise _protocol()
            value_key = "refusal" if family == "refusal" else "text"
            value = part.get(value_key)
            state = self._part(
                ev, family, "summary_index" if summary else "content_index", expected,
            )
            if not isinstance(value, str) or state.get("closed"):
                raise _protocol()
            if suffix == "added":
                if state.get("added") or state["seen"]:
                    raise _protocol()
                state.update(added=part, text=value, seen=bool(value))
            else:
                if state["seen"] and state["text"] != value:
                    raise _protocol()
                state.update(final=part, text=value, seen=True, closed=True)
            return
        # 包括 failed/error、未知工具生命周期；不能由后续 completed 洗白。
        raise _protocol()

    def _seed_item(self, index: int, item: dict[str, Any]) -> None:
        """added 里已有的正文同样是已观察前缀，不能被 done 静默替换。"""
        if item["type"] == "compaction":
            return
        if item["type"] == "function_call":
            fields = [("arguments", "_argument_index", [{"arguments": item.get("arguments")}])]
        else:
            fields = [("content", "content_index", item.get("content", []))]
            if item["type"] == "reasoning":
                fields.append(("summary", "summary_index", item.get("summary", [])))
        for field, index_key, parts in fields:
            if not isinstance(parts, list):
                raise _protocol()
            for position, part in enumerate(parts):
                if not isinstance(part, dict):
                    raise _protocol()
                family = (
                    "arguments" if field == "arguments" else
                    "summary" if field == "summary" else
                    {"output_text": "text", "refusal": "refusal",
                     "reasoning_text": "reasoning"}.get(part.get("type"))
                )
                if family is None:
                    raise _protocol()
                key = "arguments" if family == "arguments" else (
                    "refusal" if family == "refusal" else "text"
                )
                value = part.get(key)
                if not isinstance(value, str):
                    raise _protocol()
                state = self._part(
                    {"output_index": index, "item_id": item["id"], index_key: position},
                    family, index_key, item["type"],
                )
                state.update(text=value, seen=bool(value))

    def _verify_parts(self, index: int, item: dict[str, Any]) -> None:
        for (output_index, family, position), state in self.parts.items():
            if output_index != index:
                continue
            if state.get("added") is not None and not state.get("closed"):
                raise _protocol()
            if family == "arguments":
                value, part = item.get("arguments"), None
            else:
                field = "summary" if family == "summary" else "content"
                parts = item.get(field)
                if not isinstance(parts, list) or position >= len(parts):
                    raise _protocol()
                part = parts[position]
                expected = {"summary": "summary_text", "reasoning": "reasoning_text",
                            "text": "output_text", "refusal": "refusal"}[family]
                if not isinstance(part, dict) or part.get("type") != expected:
                    raise _protocol()
                value = part.get("refusal" if family == "refusal" else "text")
            if state["seen"] and value != state["text"]:
                raise _protocol()
            if "final" in state and state["final"] != part:
                raise _protocol()
