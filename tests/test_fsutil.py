from __future__ import annotations

import time
from pathlib import Path

import pytest

import tianshu.fsutil as fsutil


def test_atomic_write_retries_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    real_replace = fsutil.os.replace
    attempts = 0

    def transient_share_violation(source, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError(13, "target temporarily open", source)
            error.winerror = 5
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(fsutil.os, "replace", transient_share_violation)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)

    fsutil.atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert attempts == 3
    assert list(tmp_path.glob(".note.md.*.tmp")) == []


def test_atomic_write_raises_after_bounded_windows_sharing_retries(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    attempts = 0

    def persistent_share_violation(source, destination) -> None:
        nonlocal attempts
        attempts += 1
        error = PermissionError(13, "target remains open", source)
        error.winerror = 5
        raise error

    monkeypatch.setattr(fsutil.os, "replace", persistent_share_violation)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)

    with pytest.raises(PermissionError, match="target remains open"):
        fsutil.atomic_write_bytes(target, b"new")

    assert attempts == len(fsutil._WINDOWS_REPLACE_RETRY_DELAYS)
    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".note.md.*.tmp")) == []
