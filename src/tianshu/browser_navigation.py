"""Run-scoped navigation of public HTML; pages are parsed as untrusted text."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from .browser_forms import FORM_LIMIT, SUBMIT_TOOL, encode_fields, extract_forms
from .contents import MAX_PAGE, ContentError, ContentStore
from .errors import TianshuError, ToolArgumentError
from .events import EventLog
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
        html = fetched.body.decode("utf-8", errors="replace")
        parser.feed(html)
        forms = (extract_forms(target, html, resolve=_link_target)
                 if len(fetched.body) <= MAX_PAGE else [])
        parser.close()
        parser.handle_endtag("a")
    except (ValueError, RecursionError):
        raise NetError("网页内容无法解析", reason="net.browser_response") from None
    return {"links": parser.links, "text": " ".join(parser.text), "forms": forms}


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


def verify_submission(
    log: EventLog, contents: ContentStore | None, run_id: str, task_id: str,
    args: dict[str, Any], approved_uri: str,
) -> bytes:
    target = browser_target(args.get("url"))
    source = args.get("from_call")
    index = args.get("form_index")
    if (target.to_uri() != approved_uri or target.query or contents is None
            or not isinstance(source, str) or type(index) is not int
            or not 0 <= index < FORM_LIMIT):
        raise ToolArgumentError("表单必须绑定本次运行内已保存的同源网页和有效序号")
    results = {
        event.payload["call_id"]: event.payload
        for event in log.read_run(run_id, event_types=("tool.result",))
    }
    row = results.get(source)
    if row is None or row["tool_id"] != BROWSER_TOOL or row["status"] != "succeeded":
        raise ToolArgumentError("没有找到本次运行内成功导航的来源网页")
    output = row["output"]
    source_target = browser_target(output.get("url"))
    if (source_target.origin_uri != target.origin_uri or type(output.get("bytes")) is not int
            or not 0 < output["bytes"] <= MAX_PAGE):
        raise ToolArgumentError("来源网页与表单目标不同源，或其内容超过表单解析上限")
    try:
        page = contents.read(output.get("ref"), task_id=task_id, length=MAX_PAGE,
                             encoding="utf-8-replace")
        if page.next_offset is not None or page.total_size != output["bytes"]:
            raise ContentError("保存的网页内容不完整")
        forms = extract_forms(source_target, page.data, resolve=_link_target)
    except (TianshuError, OSError, ValueError, RecursionError):
        raise ToolArgumentError("来源网页无法读取或摘要不符") from None
    if (forms != (output.get("browser") or {}).get("forms")
            or index >= len(forms) or forms[index]["url"] != approved_uri):
        raise ToolArgumentError("表单目标或字段与已保存的网页不一致")
    body = encode_fields(forms[index], args.get("fields"))

    # The same page/slot cannot send again in this Run even after another navigation.
    # This check runs again in the write transaction that advances the send boundary.
    for event in log.read_run(run_id, event_types=("tool.intent",)):
        intent = event.payload
        if intent["tool_id"] != SUBMIT_TOOL:
            continue
        prior = intent["args"]
        if prior.get("form_index") != index or prior.get("url") != approved_uri:
            continue
        prior_page = results.get(prior.get("from_call"))
        if prior_page is not None and prior_page["output"].get("url") != source_target.to_uri():
            continue
        state = log.conn.execute(
            "SELECT state, boundary_protocol FROM dispatches WHERE run_id=? AND dispatch_id=?",
            (run_id, intent["dispatch_id"]),
        ).fetchone()
        if (state is None or state["boundary_protocol"] != 1
                or state["state"] not in {"intent", "revoked", "rejected"}):
            raise ToolArgumentError("本次运行已经开始提交该表单，不能重复发送")
    return body
