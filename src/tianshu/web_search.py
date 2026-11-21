"""Compile a bounded search request and normalize public search hits."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .errors import ToolArgumentError
from .net import Fetched, NetError
from .resources import NetworkTarget, ResourceError, parse_network_origin

SEARCH_TOOL = "network.search"


def search_target(args: dict[str, Any]) -> NetworkTarget:
    origin, query = args.get("origin"), args.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 400 or any(
        unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in query
    ):
        raise ToolArgumentError("网页搜索词必须是 1–400 个字符的单行文字")
    try:
        declared = parse_network_origin(origin)
        target = NetworkTarget.parse(declared)
    except ResourceError as exc:
        raise ToolArgumentError("网页搜索需要有效的 HTTPS 搜索源") from exc
    if target.transport != "https" or origin != target.origin_uri:
        raise ToolArgumentError("网页搜索源必须是规范 HTTPS 源，不含路径或请求参数")
    return replace(target, path="/search", query=urlencode(
        {"q": query, "format": "json"}, quote_via=quote,
    ))


def _safe_result_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(
        char.isspace() or char == "\\" or unicodedata.category(char).startswith("C")
        for char in value
    ):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port
        return bool(parts.scheme in ("http", "https") and parts.hostname
                    and parts.username is None and parts.password is None)
    except ValueError:
        return False


def search_hits(fetched: Fetched) -> list[dict[str, str]]:
    if (fetched.truncated or fetched.status != 200
            or fetched.content_type.split(";", 1)[0].strip().lower() != "application/json"):
        raise NetError("搜索服务未返回完整的 JSON 结果", reason="net.search_response")
    try:
        contents = json.loads(fetched.body.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise NetError("搜索服务返回了无效的 JSON", reason="net.search_response") from exc
    hits = contents.get("results") if isinstance(contents, dict) else None
    if not isinstance(hits, list):
        raise NetError("搜索结果缺少 results 数组", reason="net.search_response")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url, title, snippet = hit.get("url"), hit.get("title"), hit.get("content", "")
        if (not _safe_result_url(url) or not isinstance(title, str)
                or not isinstance(snippet, str) or url in seen):
            continue
        # Reject unpaired surrogates instead of failing the whole result transaction.
        title = title.encode("utf-8", "replace").decode("utf-8")
        snippet = snippet.encode("utf-8", "replace").decode("utf-8")
        result.append({"title": title[:300], "url": url, "snippet": snippet[:800]})
        seen.add(url)
        if len(result) == 10:
            break
    return result
