"""Validate local schedule triggers and observe time and directories."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import TianshuError
from .pathkeys import is_ancestor_or_self, overlap_key
from .sandbox import current_backend
from .workspace import WorkspaceBinding

FILE_POLL_MS = 5_000
_MAX_SCAN_ENTRIES = 2_048
_CLOCK_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_FIXED_ZONE = re.compile(r"UTC([+-])([01][0-9]|2[0-3]):([0-5][0-9])")


def _zone(name: Any) -> tzinfo:
    if not isinstance(name, str) or not name or len(name) > 100:
        raise TianshuError("timezone 必须是 UTC、UTC+HH:MM 或 IANA 时区名称")
    if name == "UTC":
        return UTC
    if match := _FIXED_ZONE.fullmatch(name):
        sign, hour, minute = match.groups()
        offset = timedelta(hours=int(hour), minutes=int(minute))
        return timezone(offset if sign == "+" else -offset)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TianshuError("此系统无法使用指定的 IANA 时区；可改用 UTC 或固定偏移") from exc


def validate_trigger(
    raw: Any, bindings: list[WorkspaceBinding]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the normalized trigger and optional directory baseline."""
    if not isinstance(raw, dict):
        raise TianshuError("trigger 必须是对象")
    if raw.get("kind") == "event" and set(raw) == {"kind"}:
        return {"kind": "event"}, None
    if raw.get("kind") == "calendar":
        clock, zone, weekdays = raw.get("time"), raw.get("timezone"), raw.get("weekdays")
        if set(raw) != {"kind", "time", "timezone", "weekdays"}:
            raise TianshuError("calendar 只接受 time、timezone 和 weekdays")
        if not isinstance(clock, str) or _CLOCK_TIME.fullmatch(clock) is None:
            raise TianshuError("time 必须是 24 小时制 HH:MM")
        _zone(zone)
        if (not isinstance(weekdays, list) or not 1 <= len(weekdays) <= 7
                or any(type(day) is not int or not 1 <= day <= 7 for day in weekdays)
                or len(set(weekdays)) != len(weekdays)):
            raise TianshuError("weekdays 必须是不重复的整数列表（周一 1，周日 7）")
        return {"kind": "calendar", "time": clock, "timezone": zone,
                "weekdays": sorted(weekdays)}, None
    if raw.get("kind") == "file_arrival" and set(raw) == {"kind", "directory"}:
        value = raw["directory"]
        if (not isinstance(value, str) or len(value) > 4096 or "\0" in value
                or not Path(value).is_absolute()):
            raise TianshuError("directory 必须是本地绝对路径")
        if current_backend().name != "local_fs":
            raise TianshuError("当前文件后端不支持目录触发")
        try:
            directory = Path(value).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise TianshuError("无法解析监听目录") from exc
        if not any(
            not binding.can_write and directory.is_relative_to(binding.root_path)
            for binding in bindings
        ):
            raise TianshuError("监听目录必须位于只读工作区内")
        directory_key = overlap_key(str(directory))
        for binding in bindings:
            if binding.can_write:
                binding_key = overlap_key(binding.root_path)
                if (is_ancestor_or_self(binding_key, directory_key)
                        or is_ancestor_or_self(directory_key, binding_key)):
                    raise TianshuError("可写工作区不能与监听目录重叠")
        return {"kind": "file_arrival", "directory": str(directory)}, scan_directory(directory)
    raise TianshuError("未知触发类型或字段；请使用 calendar、file_arrival 或 event")


def calendar_next(trigger: dict[str, Any], now: int) -> int:
    zone = _zone(trigger["timezone"])
    first = datetime.fromtimestamp(now / 1000, zone).date()
    wall_time = time.fromisoformat(trigger["time"])
    # Each selected weekday occurs at least twice in this horizon. Round trip
    # through UTC to skip a nonexistent wall time; on a fold select only the
    # first occurrence, including when now lies between both occurrences.
    for offset in range(15):
        day = first + timedelta(days=offset)
        if day.isoweekday() not in trigger["weekdays"]:
            continue
        wall = datetime.combine(day, wall_time)
        utc = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) != wall:
            continue
        next_ms = int(utc.timestamp()) * 1000
        if next_ms > now:
            return next_ms
    raise TianshuError("未来两周内找不到可用的日历触发时刻")


def _directory_identity(info: os.stat_result) -> list[int]:
    is_reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if not stat.S_ISDIR(info.st_mode) or is_reparse or not info.st_ino:
        raise TianshuError("监听目录不是可固定身份的本地目录")
    return [info.st_dev, info.st_ino]


def _fingerprints(entries: Any) -> list[str]:
    names = []
    for count, entry in enumerate(entries, 1):
        if count > _MAX_SCAN_ENTRIES:
            raise TianshuError(f"监听目录超过 {_MAX_SCAN_ENTRIES} 项；请使用较小的收件目录")
        # Never read bytes or follow an entry, including symlinks and junctions.
        if entry.is_file(follow_symlinks=False):
            if os.name == "nt" and (
                entry.stat(follow_symlinks=False).st_file_attributes
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                continue
            names.append(hashlib.sha256(entry.name.encode("utf-8", "surrogatepass")).hexdigest())
    return sorted(names)


def scan_directory(path: Path, *, expected: list[int] | None = None) -> dict[str, Any]:
    if current_backend().name != "local_fs":
        raise TianshuError("当前文件后端不支持目录触发")
    try:
        if path.resolve(strict=True) != path:
            raise TianshuError("监听目录已改变；请重新创建触发器")
        if os.scandir in os.supports_fd:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                identity = _directory_identity(os.fstat(fd))
                if expected is not None and identity != expected:
                    raise TianshuError("监听目录已改变；请重新创建触发器")
                with os.scandir(fd) as entries:
                    names = _fingerprints(entries)
            finally:
                os.close(fd)
        else:
            identity = _directory_identity(path.lstat())
            if expected is not None and identity != expected:
                raise TianshuError("监听目录已改变；请重新创建触发器")
            with os.scandir(path) as entries:
                names = _fingerprints(entries)
        if path.resolve(strict=True) != path or _directory_identity(path.lstat()) != identity:
            raise TianshuError("扫描期间监听目录已改变；请重新创建触发器")
    except (OSError, RuntimeError) as exc:
        raise TianshuError("无法扫描监听目录") from exc
    return {"identity": identity, "files": names}
