"""Run attachment input validates bytes before they can enter a persisted fact."""

from __future__ import annotations

import base64
import hashlib

import pytest

from tianshu.task_attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    decode_attachment,
    normalize_attachments,
)


def _upload(data: bytes, *, name: str = "笔记.txt", media_type: str = "text/plain"):
    return {"name": name, "media_type": media_type, "data_base64": base64.b64encode(data).decode()}


def test_attachment_roundtrips_original_bytes_and_rejects_forged_digest():
    original = "这一行保留换行\r\n".encode()
    attached, = normalize_attachments([_upload(original)])
    assert attached["name"] == "笔记.txt"
    assert attached["format_version"] == 1
    assert attached["sha256"] == hashlib.sha256(original).hexdigest()
    assert decode_attachment(attached) == original
    with pytest.raises(AttachmentError):
        decode_attachment({**attached, "sha256": "a" * 64})
    with pytest.raises(AttachmentError):
        decode_attachment({**attached, "format_version": True})


@pytest.mark.parametrize("upload", [
    _upload(b"hello", name="../note.txt"),
    _upload(b"hello", name="C:\\note.txt"),
    _upload(b"hello", name="bad\nlabel.txt"),
    _upload(b"hello", media_type="application/octet-stream"),
    _upload(b"hello", media_type="image/png"),
    _upload(b"\xff"),
    {**_upload(b"f"), "data_base64": "Zh=="},  # nonzero unused padding bits
    {**_upload(b"f"), "sha256": "untrusted"},
])
def test_attachment_rejects_untrusted_fields_types_and_encodings(upload):
    with pytest.raises(AttachmentError):
        normalize_attachments([upload])


def test_attachment_limit_is_aggregate_and_enforced_before_event_creation():
    part = _upload(b"x" * (MAX_ATTACHMENT_BYTES // 2))
    assert len(normalize_attachments([part, part])) == 2
    with pytest.raises(AttachmentError):
        normalize_attachments([part, part, _upload(b"extra")])
    with pytest.raises(AttachmentError):
        normalize_attachments([_upload(b"x")] * 5)
