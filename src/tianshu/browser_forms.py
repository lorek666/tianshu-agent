"""Describe a deliberately small, static subset of public HTML forms."""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import TypedDict
from urllib.parse import urlencode

from .errors import ToolArgumentError
from .net import FORM_POST_MAX_BYTES
from .resources import NetworkTarget

SUBMIT_TOOL = "browser.submit"
FORM_LIMIT = 8
FIELD_LIMIT = 8
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_INERT = frozenset({"template", "svg", "math", "noscript", "iframe", "xmp", "textarea"})
_WRAPPERS = frozenset({"div", "span", "p", "label", "br"})


class StaticForm(TypedDict):
    url: str
    fields: list[str]


def encode_fields(form: StaticForm, fields: object) -> bytes:
    if not isinstance(fields, list) or len(fields) != len(form["fields"]):
        raise ToolArgumentError("表单必须按网页顺序提供全部字段")
    pairs: list[tuple[str, str]] = []
    for name, field in zip(form["fields"], fields, strict=True):
        if not isinstance(field, dict) or set(field) != {"name", "value"}:
            raise ToolArgumentError("表单字段必须有且仅有 name 和 value")
        value = field["value"]
        if (field["name"] != name or not isinstance(value, str) or len(value) > 512
                or (value and not value.isprintable())):
            raise ToolArgumentError("表单字段与网页不符，或含无法在审批中显示的字符")
        pairs.append((name, value))
    body = urlencode(pairs).replace("~", "%7E").replace("%2A", "*").encode("ascii")
    if len(body) > FORM_POST_MAX_BYTES:
        raise ToolArgumentError("表单编码后的正文超过发送上限")
    return body


class _FormParser(HTMLParser):
    def __init__(self, resolve: Callable[[str], str | None]) -> None:
        super().__init__(convert_charrefs=True)
        self._resolve = resolve
        self._inert: list[str] = []
        self._depth = 0
        self._current: StaticForm | None = None
        self.forms: list[StaticForm] = []
        self.ambiguous = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self._inert or tag in _INERT:
            if tag in _INERT:
                self._inert.append(tag)
            self._current = None
            return
        if tag in ("base", "plaintext") or "form" in values:
            self.ambiguous = True
        if tag == "form":
            self._depth += 1
            self._current = None
            if self._depth != 1 or len(values) != len(attrs):
                return
            if (set(values) - {"method", "action", "enctype"}
                    or (values.get("method") or "").lower() != "post"
                    or values.get("enctype", "application/x-www-form-urlencoded")
                    != "application/x-www-form-urlencoded"):
                return
            action = values.get("action")
            url = self._resolve(action) if action and not re.search(r"[?#]", action) else None
            if url is not None:
                self._current = {"url": url, "fields": []}
            return
        if self._current is None:
            return
        if tag == "input" and len(values) == len(attrs) and not values.keys() - {"type", "name"}:
            key = values.get("name")
            fields = self._current["fields"]
            if ((values.get("type", "text") or "").lower() == "text"
                    and key and _NAME.fullmatch(key) and key not in fields
                    and len(fields) < FIELD_LIMIT):
                fields.append(key)
                return
        if (tag in _WRAPPERS and not attrs) or (tag == "button" and attrs == [("type", "submit")]):
            return
        # Reject the entire form instead of silently omitting a control or override.
        self._current = None

    def handle_endtag(self, tag: str) -> None:
        if self._inert:
            if self._inert[-1] == tag:
                self._inert.pop()
            return
        if tag == "form" and self._depth:
            if (self._depth == 1 and self._current is not None
                    and self._current["fields"] and len(self.forms) < FORM_LIMIT):
                self.forms.append(self._current)
            self._depth -= 1
            self._current = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("input", "br"):
            self.handle_starttag(tag, attrs)
        else:
            # HTML does not treat the slash as closing a normal form element.
            self.handle_starttag(tag, attrs)
            self._current = None


def extract_forms(
    target: NetworkTarget, html: str,
    *, resolve: Callable[[NetworkTarget, str], str | None],
) -> list[StaticForm]:
    parser = _FormParser(lambda href: resolve(target, href))
    parser.feed(html)
    parser.close()
    return [] if parser.ambiguous or parser._depth or parser._inert else parser.forms
