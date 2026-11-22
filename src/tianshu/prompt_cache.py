"""版本化 provider wire 缓存策略；不改变内部消息、授权或重试语义。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any
from urllib.parse import urlsplit

ENV_POLICY = "TIANSHU_PROMPT_CACHE_POLICY"
POLICIES = ("auto", "off", "prefix-v1", "responses-explicit-v1")


def official_openai(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    return (
        parsed.scheme == "https" and parsed.hostname == "api.openai.com"
        and parsed.port in (None, 443) and parsed.username is None
        and parsed.password is None and not parsed.query and not parsed.fragment
    )


def resolve_policy(value: str | None, *, adapter: str, endpoint: str) -> str:
    """在 adapter 构造时固定配置；第三方端点仅在主动选择策略时启用。"""
    policy = os.environ.get(ENV_POLICY, "auto") if value is None else value
    if not isinstance(policy, str) or policy not in POLICIES:
        raise ValueError(f"未知缓存 cache_policy：{policy!r}；可选 {POLICIES}")
    if policy == "responses-explicit-v1" and adapter != "openai-responses":
        raise ValueError("responses-explicit-v1 只能用于支持显式断点的 Responses 端点")
    if policy == "auto":
        return "prefix-v1" if adapter in (
            "openai-compatible", "openai-responses",
        ) and official_openai(endpoint) else "off"
    return policy


def stable_tools(tools: list[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    """内置目录顺序 + 动态名称顺序；不排序 schema 的 required/enum 等数组。"""
    if policy == "off":
        return tools
    from .tools import SPECS

    order = {name: index for index, name in enumerate(SPECS)}
    ordered = sorted(tools, key=lambda tool: (
        order.get(tool["name"], len(order)), tool["name"],
    ))
    return json.loads(json.dumps(ordered, sort_keys=True))


def stable_system(messages: list[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    """只固定开头普通 system 的对象键，不重排正文、数组或 opaque 项。"""
    if policy == "off":
        return messages
    result = list(messages)
    for index, message in enumerate(messages):
        if message.get("role") != "system" or "type" in message:
            break
        result[index] = json.loads(json.dumps(message, sort_keys=True))
    return result


def routing_key(
    *, adapter: str, endpoint: str, model: str, policy: str,
    messages: list[dict[str, Any]], tools: list[dict[str, Any]],
) -> str:
    leading = []
    for message in messages:
        if message.get("role") != "system":
            break
        leading.append(message)
    raw = json.dumps(
        [policy, adapter, endpoint, model, leading, tools],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "ts-prefix-v1-" + hashlib.sha256(raw).hexdigest()[:40]


def anthropic_breakpoints(body: dict[str, Any]) -> None:
    """转换后的私有 body；工具/system/任务/历史，最后一条消息留作动态尾部。"""
    if body.get("tools"):
        body["tools"][-1]["cache_control"] = {"type": "ephemeral"}
    if body.get("system"):
        body["system"] = [{
            "type": "text", "text": body["system"], "cache_control": {"type": "ephemeral"},
        }]
    messages = body["messages"]
    if messages and messages[0]["role"] == "user" and messages[0]["content"]:
        messages[0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    if len(messages) > 1:
        blocks = messages[-2]["content"]
        if blocks and blocks[-1].get("type") in ("text", "tool_use", "tool_result"):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}


def responses_breakpoints(body: dict[str, Any]) -> None:
    """仅标记普通 input text；opaque output items 和工具往返原值透传。"""
    body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    items = body["input"]
    leading = 0
    while leading < len(items) and (
        items[leading].get("role") == "system" and "type" not in items[leading]
    ):
        leading += 1
    # 只有紧随初始 system（或位于起点）的 user 才可认作初始任务。
    # 压缩/opaque 输出之后的第一个 user 是动态补充，不能借此重新认定任务。
    task = leading if leading < len(items) and (
        items[leading].get("role") == "user" and "type" not in items[leading]
    ) else None
    selected = set()
    if leading:
        selected.add(leading - 1)
    if task is not None:
        selected.add(task)
    historical = [
        index for index, item in enumerate(items)
        if item.get("role") == "user" and "type" not in item
        and index < len(items) - 1 and index != task
    ]
    # 显式模式只回查来件带标记的位置；保留上一历史点与新增历史点，
    # 让连续追加时仍能查到上一轮写入，连同 system/任务最多四点。
    selected.update(historical[-2:])
    for index in sorted(selected):
        item = items[index]
        content = item["content"]
        if isinstance(content, str):
            item["content"] = [{"type": "input_text", "text": content}]
        else:
            item["content"] = copy.deepcopy(content)
        text_blocks = [block for block in item["content"] if block.get("type") == "input_text"]
        if text_blocks:
            text_blocks[-1]["prompt_cache_breakpoint"] = {"mode": "explicit"}
