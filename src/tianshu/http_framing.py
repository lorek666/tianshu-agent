"""Bounded HTTP/1 response framing for transports that require a complete result."""

from __future__ import annotations

import http.client
import re
from collections.abc import Iterator

_CHUNK_SIZE = re.compile(rb"([0-9a-fA-F]{1,16})(?:[ \t]*;[\t -~]*)?\r\n")
_TRAILER = re.compile(rb"([!#$%&'*+.^_`|~0-9a-zA-Z-]+):[\t\x20-\x7e\x80-\xff]*\r\n")
_CHUNK_METADATA_BYTES = 65_536
_CHUNK_LINE_BYTES = 4096


class HttpFramingError(http.client.HTTPException):
    """The message is ambiguous, truncated, invalid or exceeds its limit."""


def response_length(response: http.client.HTTPResponse) -> int | None:
    lengths = response.headers.get_all("Content-Length", [])
    encodings = response.headers.get_all("Transfer-Encoding", [])
    if (response.headers.defects or len(lengths) > 1 or len(encodings) > 1
            or (encodings and (lengths or response.version != 11 or not response.chunked))
            or (lengths and (response.length is None
                             or not re.fullmatch(r"[0-9]+", lengths[0].strip(" \t"))))):
        raise HttpFramingError("HTTP 响应分帧不明确")
    return response.length if lengths else None


def read_chunks(
    response: http.client.HTTPResponse, max_bytes: int, *,
    max_metadata: int = _CHUNK_METADATA_BYTES,
) -> Iterator[bytes]:
    # CPython 3.12 discards the delimiter and accepts EOF in the trailers.
    stream = response.fp
    if stream is None:
        raise HttpFramingError("HTTP 响应流已关闭")
    remaining = max_bytes
    overhead = max_metadata

    def read_line() -> bytes:
        nonlocal overhead
        limit = min(overhead, _CHUNK_LINE_BYTES)
        line = stream.readline(limit + 1)
        if not line or len(line) > limit or not line.endswith(b"\r\n"):
            raise HttpFramingError("HTTP 分块响应头不完整或超限")
        overhead -= len(line)
        return line

    while True:
        chunk = _CHUNK_SIZE.fullmatch(read_line())
        if chunk is None:
            raise HttpFramingError("HTTP 响应的分块长度无效")
        size = int(chunk[1], 16)
        if size == 0:
            break
        if size > remaining or overhead < 2:
            raise HttpFramingError("HTTP 分块响应超出上限")
        block = stream.read(size + 2)
        if len(block) != size + 2 or block[-2:] != b"\r\n":
            raise HttpFramingError("HTTP 分块不完整")
        overhead -= 2
        remaining -= size
        yield block[:-2]

    while (line := read_line()) != b"\r\n":
        trailer = _TRAILER.fullmatch(line)
        if trailer is None or trailer[1].lower() in {b"content-length", b"transfer-encoding"}:
            raise HttpFramingError("HTTP 尾部字段无效")


def chunk_lines(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Reassemble lines only from verified chunks, retaining any partial final line."""
    prefix = bytearray()
    for chunk in chunks:
        start = 0
        while (end := chunk.find(b"\n", start)) != -1:
            if prefix:
                prefix.extend(chunk[start:end + 1])
                yield bytes(prefix)
                prefix.clear()
            else:
                yield chunk[start:end + 1]
            start = end + 1
        prefix.extend(chunk[start:])
    if prefix:
        yield bytes(prefix)
