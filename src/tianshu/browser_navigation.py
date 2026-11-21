"""Run-scoped navigation of public HTML; pages are parsed as untrusted text."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from .errors import ToolArgumentError
from .net import Fetched, NetError
from .resources import NetworkTarget, ResourceError

BROWSER_TOOL = "browser.navigate"
_MAX_LINKS = 24
_MAX_PREVIEW = 4096
_HIDDEN = frozenset({"script", "style", "template", "iframe", "form"})
_SAFE_PATH = re.compile(r"/[a-zA-Z0-9._~!$&'()*+,:;=@/-]*")


def browser_target(value: object) -> NetworkTarget:
    if (not isinstance(value, str) or not value.startswith("network://https/")
            or len(value) > 2048 or re.search(r"[^\x21-\x7e]|[\\#]", value)):
        raise ToolArgumentError("网页目标必须是规范 HTTPS 网络资源")
    try:
        target = NetworkTarget.parse(value)
    except ResourceError:
        raise ToolArgumentError("网页目标必须是规范 HTTPS 网络资源") from None
    path = value.removeprefix(target.origin_uri).partition("?")[0]
    if value != target.to_uri() or not _SAFE_PATH.fullmatch(path):
        raise ToolArgumentError("网页目标不是可发送的规范 HTTPS 路径")
    return target


def _link_target(current: NetworkTarget, href: str) -> str | None:
    if not href or href.startswith("#") or len(href) > 2048 or "\\" in href:
        return None
    try:
        original = current.origin + current.path + ("?" + current.query if current.query else "")
        absolute = urlsplit(urljoin(original, href))
        path = absolute.path or "/"
        if absolute.scheme != "https" or not absolute.netloc:
            return None
        uri = f"network://https/{absolute.netloc}{path}"
        if absolute.query:
            uri += "?" + absolute.query
        target = browser_target(uri)
        return uri if target.origin_uri == current.origin_uri else None
    except (ValueError, ToolArgumentError):
        return None


class _PageParser(HTMLParser):
    def __init__(self, target: NetworkTarget) -> None:
        super().__init__(convert_charrefs=True)
        self.target = target
        self.links: list[dict[str, str]] = []
        self.hidden: list[str] = []
        self.current_link: str | None = None
        self.label = ""
        self.text: list[str] = []
        self.remaining = _MAX_PREVIEW

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self.hidden.append(tag)
        if self.hidden or tag != "a" or len(self.links) >= _MAX_LINKS:
            return
        self.handle_endtag("a")
        href = dict(attrs).get("href")
        self.current_link = _link_target(self.target, href) if isinstance(href, str) else None

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
            return
        if tag == "a" and self.current_link is not None:
            if len(self.links) < _MAX_LINKS and not any(
                link["url"] == self.current_link for link in self.links
            ):
                self.links.append({"url": self.current_link, "label": self.label or "链接"})
            self.current_link = None
            self.label = ""

    def handle_data(self, data: str) -> None:
        if self.hidden or (self.remaining <= 0 and self.current_link is None):
            return
        text = " ".join(data.split())
        if self.remaining > 0 and text:
            fragment = text[:self.remaining]
            self.text.append(fragment)
            self.remaining -= len(fragment) + 1
        if self.current_link is not None and text:
            self.label = (f"{self.label} {text}".strip())[:100]


def page_snapshot(target: NetworkTarget, fetched: Fetched) -> dict[str, Any]:
    if (fetched.uri != target.to_uri() or fetched.redirects or fetched.truncated
            or fetched.status != 200
            or fetched.content_type.split(";", 1)[0].strip().lower() != "text/html"):
        raise NetError("网页导航需要原目标返回的完整 HTML", reason="net.browser_response")
    parser = _PageParser(target)
    try:
        parser.feed(fetched.body.decode("utf-8", errors="replace"))
        parser.close()
        parser.handle_endtag("a")
    except (ValueError, RecursionError):
        raise NetError("网页内容无法解析", reason="net.browser_response") from None
    return {"links": parser.links, "text": " ".join(parser.text)}


def verify_navigation(log, run_id: str, args: dict[str, Any], approved_uri: str) -> None:
    target = browser_target(args.get("url"))
    if target.to_uri() != approved_uri:
        raise ToolArgumentError("网页地址与审批目标不同")
    if not args.keys() & {"from_call", "link_index"}:
        return
    source = args.get("from_call")
    index = args.get("link_index")
    if not isinstance(source, str) or type(index) is not int or not 0 <= index < _MAX_LINKS:
        raise ToolArgumentError("跟随网页链接须同时提供来源调用和有效的链接序号")
    matches = [ev.payload for ev in log.read_run(run_id, event_types=("tool.result",))
               if ev.payload.get("call_id") == source]
    if len(matches) == 1:
        row = matches[0]
        if row.get("tool_id") == BROWSER_TOOL and row.get("status") == "succeeded":
            output = row["output"]
            links = (output.get("browser") or {}).get("links", [])
            if (browser_target(output.get("url")).origin_uri == target.origin_uri
                    and isinstance(links, list) and index < len(links)
                    and isinstance(links[index], dict) and links[index].get("url") == approved_uri):
                return
    raise ToolArgumentError("链接不属于本次运行中已保存的同源网页，或目标地址已变化")
