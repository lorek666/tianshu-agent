"""Validate uploaded attachment bytes without opening a local path or a URL."""

from __future__ import annotations

import base64
import hashlib
import unicodedata
from typing import Any

MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 240 * 1024
_FORMAT_VERSION = 1
RUN_ATTACHMENTS = "run.attachments"
MEDIA_TYPES = ("text/plain", "image/png", "image/jpeg")
_UPLOAD_FIELDS = {"name", "media_type", "data_base64"}
_PERSISTED_FIELDS = _UPLOAD_FIELDS | {"format_version", "sha256"}


class AttachmentError(ValueError):
    """An attachment does not satisfy the closed input format."""


def _decode(attachment: dict[str, Any], *, max_bytes: int) -> bytes:
    name, media_type, data = (attachment[k] for k in ("name", "media_type", "data_base64"))
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or name != name.strip()
        or name in (".", "..")
        or any(c in "/\\:" or unicodedata.category(c).startswith("C") for c in name)
    ):
        raise AttachmentError("附件名称必须是单个文件名，最多 128 个字符")
    if media_type not in MEDIA_TYPES:
        raise AttachmentError("附件只支持 UTF-8 文本、PNG 或 JPEG")
    if not isinstance(data, str):
        raise AttachmentError("附件内容必须采用规范 Base64")
    if len(data) > ((max_bytes + 2) // 3) * 4:
        raise AttachmentError("附件总大小不能超过 240 KiB")
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise AttachmentError("附件内容必须采用规范 Base64") from exc
    if base64.b64encode(raw).decode("ascii") != data:
        raise AttachmentError("附件内容必须采用规范 Base64")
    if len(raw) > max_bytes:
        raise AttachmentError("附件总大小不能超过 240 KiB")
    if media_type == "text/plain":
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AttachmentError("文本附件必须采用 UTF-8 编码") from exc
    elif media_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AttachmentError("附件不符合声明的 PNG 格式")
    elif media_type == "image/jpeg" and not (
        raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9") and len(raw) >= 5
    ):
        raise AttachmentError("附件不符合声明的 JPEG 格式")
    return raw


def normalize_attachments(raw: object) -> list[dict[str, Any]]:
    """Return new records whose digest is computed by the host from validated bytes."""
    if not isinstance(raw, list) or len(raw) > MAX_ATTACHMENTS:
        raise AttachmentError("每次运行最多可上传 4 个附件")
    remaining = MAX_ATTACHMENT_BYTES
    records = []
    for attachment in raw:
        if not isinstance(attachment, dict) or attachment.keys() != _UPLOAD_FIELDS:
            raise AttachmentError("上传附件必须包含文件名、格式和内容")
        data = _decode(attachment, max_bytes=remaining)
        remaining -= len(data)
        records.append({
            **attachment,
            "format_version": _FORMAT_VERSION,
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return records


def decode_attachment(record: object) -> bytes:
    """Recheck stored format and digest before bytes leave the event boundary."""
    if (
        not isinstance(record, dict)
        or record.keys() != _PERSISTED_FIELDS
        or type(record["format_version"]) is not int
        or record["format_version"] != _FORMAT_VERSION
    ):
        raise AttachmentError("未识别的附件记录格式")
    raw = _decode(record, max_bytes=MAX_ATTACHMENT_BYTES)
    if record["sha256"] != hashlib.sha256(raw).hexdigest():
        raise AttachmentError("附件摘要与记录的内容不符")
    return raw


def validated_attachments(records: object) -> list[tuple[dict[str, Any], bytes]]:
    if not isinstance(records, list) or len(records) > MAX_ATTACHMENTS:
        raise AttachmentError("附件记录最多 4 条")
    pairs = [(record, decode_attachment(record)) for record in records]
    if sum(len(raw) for _, raw in pairs) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("附件总大小不能超过 240 KiB")
    return pairs


def attachments_fact(events) -> list[dict[str, Any]]:
    from .errors import TianshuError

    matches = [event.payload["attachments"] for event in events if event.type == RUN_ATTACHMENTS]
    if len(matches) > 1:
        raise TianshuError("同一 Run 的附件事件不能重复")
    return matches[0] if matches else []


def text_attachment(name: str, raw: bytes) -> str:
    # Quote the boundary independently of user-controlled file contents.
    import json

    return f"附件（用户提供的资料）：{json.dumps(name, ensure_ascii=False)}\n{raw.decode('utf-8')}"


def plain_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep historical message bytes; adapt only structured attachments."""
    result = []
    for message in messages:
        if "attachments" not in message:
            result.append(message)
            continue
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            raise AttachmentError("附件只能随用户消息发送")
        records = validated_attachments(message["attachments"])
        if any(record["media_type"] != "text/plain" for record, _ in records):
            raise AttachmentError("所选模型未开启图片附件能力")
        text = [message["content"]]
        text.extend(text_attachment(record["name"], raw) for record, raw in records)
        result.append({"role": "user", "content": "\n\n".join(text)})
    return result
