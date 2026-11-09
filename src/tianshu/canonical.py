"""规范 JSON（RFC 8785 / JCS）。

为什么不能继续用 `canonical_bytes()`（`sort_keys=True` 的那个）来算授权摘要：

- 它输出 `{"a": 1}` 这种带空格的 JSON，不是 JCS 的紧凑形式；
- 它按 **Unicode 码点**排序，JCS 要求按 **UTF-16 码元**排序。两者在星平面字符
  与 BMP 高区混排时结果不同（例如 U+FFFF 与 U+10000：JCS 里 U+10000 在前）；
- 它没有定义数字的规范形式。

`canonical_bytes` 继续服务旧事件日志的 payload_hash——改它会让历史 replay 全废。
这里另起一份，只给"授权摘要"用，两者互不影响。

**关于数字**：本项目所有授权字段都是整数（revision / epoch / 序号）、字符串
（id、哈希、路径）或布尔，没有浮点。所以这里接受整数并**拒绝浮点**，而不是
自己手写一份 ECMAScript 数字序列化再声称符合 RFC 8785——那种实现错了比没有更危险：
摘要会悄悄算出不同的值。真的需要浮点（成本、温度等）时再补，并且必须用 RFC 的
数字向量验收。
"""

from __future__ import annotations

import hashlib
from typing import Any

# JSON 里能安全交给 IEEE double 的整数上界。超过它的整数在不同实现里
# 会被舍入成不同的字符串，摘要就不再跨语言一致了。
MAX_SAFE_INT = 2**53 - 1

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class JcsError(ValueError):
    """输入无法按 RFC 8785 规范化。"""


def jcs_bytes(value: Any) -> bytes:
    """规范化后取 UTF-8 字节。摘要就是对它求 SHA-256。"""
    return jcs_dumps(value).encode("utf-8")


def jcs_sha256(value: Any) -> str:
    return hashlib.sha256(jcs_bytes(value)).hexdigest()


def jcs_dumps(value: Any) -> str:
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, int):
        out.append(_integer(value))
    elif isinstance(value, float):
        raise JcsError(
            f"不支持的浮点值：{value!r}。授权摘要里只允许整数；"
            "浮点需要 ECMAScript 数字序列化，本实现刻意不做"
        )
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for index, key in enumerate(_sorted_keys(value)):
            if index:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise JcsError(f"不支持的类型：{type(value).__name__}")


def _sorted_keys(mapping: dict[str, Any]) -> list[str]:
    for key in mapping:
        if not isinstance(key, str):
            raise JcsError(f"对象的键必须是字符串：{key!r}")
        _check_string(key)
    # 按 UTF-16 码元排序：用大端 UTF-16 编码后的字节序比较，
    # 字节序等于码元序（每码元两字节，高位在前）。直接用 Python 的 str
    # 比较是按码点排的，遇到星平面字符会排错。
    return sorted(mapping, key=lambda k: k.encode("utf-16-be", "surrogatepass"))


def _integer(value: int) -> str:
    if abs(value) > MAX_SAFE_INT:
        raise JcsError(
            f"整数超出安全范围：{value}。超过 2^53-1 后不同 JSON 实现"
            "序列化结果不再一致"
        )
    return str(value)


def _check_string(text: str) -> None:
    # 孤立代理项不是合法 Unicode，JCS 要求输入是合法 UTF-8 文本
    for index, char in enumerate(text):
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise JcsError(f"字符串含孤立代理项（位置 {index}）")


def _string(text: str) -> str:
    _check_string(text)
    out = ['"']
    for char in text:
        escaped = _ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif ord(char) < 0x20:
            # 其余控制字符用 \u00XX，小写十六进制（RFC 8785 §3.2.2.2）
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


__all__ = ["JcsError", "MAX_SAFE_INT", "jcs_bytes", "jcs_dumps", "jcs_sha256"]