"""Versioned, credential-free model selection fixed before a Run is enqueued."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .continuation import validate_compact_threshold

RUN_MODEL_ROUTE = "run.model_route"
MODEL_PROFILE_LIMIT = 16
MODEL_ROUTE_LIMIT = 4
_PROFILE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_CONFIG_KEYS = frozenset({"mode", "model_name", "base_url", "credential_backend"})


def model_profile_name(value: object) -> str:
    if not isinstance(value, str) or _PROFILE_NAME.fullmatch(value) is None:
        raise ValueError("模型档案名必须以字母开头，至多 32 个字母、数字、下划线或连字符")
    return value


def model_route_names(names: object) -> tuple[str, ...]:
    if not isinstance(names, (tuple, list)) or not 1 <= len(names) <= MODEL_ROUTE_LIMIT:
        raise ValueError(f"至少选择一个模型档案，候选最多 {MODEL_ROUTE_LIMIT} 个")
    result = tuple(map(model_profile_name, names))
    if len(set(result)) != len(result):
        raise ValueError("模型候选不能重复")
    return result


def validate_profile_config(raw: object) -> dict[str, Any]:
    if (not isinstance(raw, Mapping)
            or set(raw) - (_CONFIG_KEYS | {"allow_image_attachments", "compact_threshold"})
            or any(not isinstance(raw[key], str) for key in _CONFIG_KEYS & raw.keys())
            or ("allow_image_attachments" in raw
                and (type(raw["allow_image_attachments"]) is not bool
                     or raw.get("mode") != "openai-responses"))):
        raise ValueError("模型档案只能包含非敏感字符串配置")
    validate_compact_threshold(raw.get("compact_threshold"),
                               responses=raw.get("mode") == "openai-responses")
    result = dict(raw)
    url = result.get("base_url", "").strip()
    if url:
        try:
            parsed = urlsplit(url)
            _ = parsed.port
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or "?" in url or "#" in url or "\\" in url
                    or any(char.isspace() or ord(char) < 32 for char in url)):
                raise ValueError
        except ValueError:
            raise ValueError("模型档案端点必须是无凭据、查询和 fragment 的 HTTP(S) URL") from None
    return result


@dataclass(frozen=True)
class ModelRouteV1:
    requested: tuple[str, ...]
    selected: str
    mode: str
    model_name: str
    base_url: str
    credential_backend: str
    allow_image_attachments: bool = False
    compact_threshold: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "format_version": 1,
            "requested": list(self.requested),
            "selected": self.selected,
            "config": {
                "mode": self.mode,
                "model_name": self.model_name,
                "base_url": self.base_url,
                "credential_backend": self.credential_backend,
                **({"allow_image_attachments": True} if self.allow_image_attachments else {}),
                **({"compact_threshold": self.compact_threshold}
                   if self.compact_threshold is not None else {}),
            },
        }

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> ModelRouteV1:
        if (not isinstance(raw, Mapping)
                or set(raw) != {"format_version", "requested", "selected", "config"}
                or type(raw["format_version"]) is not int or raw["format_version"] != 1
                or not isinstance(raw["requested"], list)):
            raise ValueError("Run 模型路由格式非法")
        requested = model_route_names(raw["requested"])
        selected = model_profile_name(raw["selected"])
        if selected not in requested:
            raise ValueError("所选模型不在 Run 候选中")
        config = validate_profile_config(raw["config"])
        if not _CONFIG_KEYS <= set(config):
            raise ValueError("Run 模型配置字段不完整")
        return cls(requested, selected, **config)


def model_route_fact(events) -> dict[str, Any] | None:
    matches = [event.payload for event in events if event.type == RUN_MODEL_ROUTE]
    if len(matches) > 1:
        raise ValueError("同一 Run 的模型路由不能重复")
    return matches[0] if matches else None
