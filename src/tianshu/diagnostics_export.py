"""本地默认脱敏诊断包导出与内容清单预览（PRD §13.1 D7 / §13.3 第 4 条 / §7 F14）。

核心安全不变量：
1. **只在本地导出，绝不上传（D7）**：本模块不导入任何网络客户端（`socket` / `urllib` / `http`），
   只向用户显式指定的本地目录（禁止落入 `data_root` 内部，且 `O_EXCL` 拒绝静默覆盖）写出 `.zip`。
2. **默认脱敏**：
   - 所有文本先经 `credentials.redact()` 打码已知凭据与疑似密钥模式；
   - 本机敏感根路径（`data_root`、`Path.home()` 及其 `resolve()` 别名）连同其后完整子路径
     （含空格目录名与文件名）整体替换为 `<PATH_REDACTED>`，再扫描通用 POSIX / Windows 绝对路径；
   - 敏感自由文本（任务 `goal`、消息正文、工具原始参数与输出正文）不进入默认诊断包，
     仅保留事件结构、时序、状态、规则码与错误分类码；
3. **先预览、可逐项开关**：`preview_diagnostics()` 在落盘前给出每一节的说明、条数与脱敏规则，
   用户可通过 `sections` 逐项勾选导出内容。
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sqlite3
import sys
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .credentials import redact
from .errors import TianshuError

DIAGNOSTIC_SECTIONS: tuple[str, ...] = (
    "system",
    "runs_summary",
    "events_timeline",
    "schedules",
    "metrics",
    "daemon_log",
)

SECTION_DESCRIPTIONS: dict[str, str] = {
    "system": "Python / OS 平台架构与数据根存在性摘要（不含环境变量与用户名路径）",
    "runs_summary": "最近 100 条 Run 的状态、结局、副作用计数与时间戳（不含任务 goal 文本）",
    "events_timeline": "最近 500 条事件的结构化时序、事件类型与错误/规则码（已剥离消息正文与路径）",
    "schedules": "定时任务启用状态、间隔秒数与最近错误码（已打码 goal 与工作目录路径）",
    "metrics": "本地性能与慢订阅者采样指标（已对所有字符串字段做路径与凭据脱敏）",
    "daemon_log": "最近 200 行守护进程日志（已对凭据与本机绝对路径打码）",
}

# 捕获 POSIX 敏感根路径（支持含空格的多段子路径，直至引号/冒号/分号/括号/换行边界）
_POSIX_PATH_RE = re.compile(
    r"(?:/Users|/Volumes|/home|/private/var|/private/tmp"
    r"|/var/folders|/tmp|/Library/Application Support)"
    r"(?:/[^\r\n/\"',:;)\]]+)+"
)
# 捕获 Windows 盘符绝对路径（含空格子路径，以及 JSON/repr 双反斜杠转义形式 C:\\Users\\...）
_WIN_PATH_RE = re.compile(
    r"\b[A-Za-z]:\\{1,2}(?:[^\r\n\\\"',:;)\]]+(?:\\{1,2})?)+"
)


def _expand_extra_roots(extra_paths: Sequence[str]) -> list[str]:
    roots: set[str] = set()
    for raw in extra_paths:
        cleaned = str(raw or "").strip().rstrip("/\\")
        if len(cleaned) > 1:
            roots.add(cleaned)
            if "\\" in cleaned:
                roots.add(cleaned.replace("\\", "\\\\"))
            try:
                resolved = str(Path(cleaned).resolve()).rstrip("/\\")
                if len(resolved) > 1:
                    roots.add(resolved)
                    if "\\" in resolved:
                        roots.add(resolved.replace("\\", "\\\\"))
            except OSError:
                pass
    return sorted(roots, key=len, reverse=True)


def redact_diagnostic_text(text: str, *, extra_paths: Sequence[str] = ()) -> str:
    """对凭据与本机路径同时打码（先连同子路径整体消除 extra_paths，再扫通用绝对路径）。"""
    out = redact(text)
    for root_prefix in _expand_extra_roots(extra_paths):
        # 连同该根路径之后的全部子路径（含空格文件名及 JSON 双反斜杠连接符）一并打码
        subpath_pat = re.compile(
            re.escape(root_prefix) + r"(?:[/\\]+[^\r\n\"',:;)\]]*)?"
        )
        out = subpath_pat.sub("<PATH_REDACTED>", out)
    out = _WIN_PATH_RE.sub("<PATH_REDACTED>", out)
    out = _POSIX_PATH_RE.sub("<PATH_REDACTED>", out)
    return out


def _normalize_sections(sections: Sequence[str] | None) -> tuple[str, ...]:
    if sections is None:
        return DIAGNOSTIC_SECTIONS
    chosen: list[str] = []
    for item in sections:
        name = str(item).strip()
        if not name:
            continue
        if name not in DIAGNOSTIC_SECTIONS:
            raise TianshuError(
                f"未知诊断包分节：{name!r}（可选：{list(DIAGNOSTIC_SECTIONS)}）"
            )
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise TianshuError("至少选择一项要导出的诊断内容")
    return tuple(chosen)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _sanitize_event_payload(
    payload: dict[str, Any], *, redact_paths: Sequence[str]
) -> dict[str, Any]:
    """只保留结构性诊断键（状态、原因码、规则码、工具名、耗时），剥离自由文本与路径。"""
    safe: dict[str, Any] = {}
    allowed_scalar_keys = {
        "status",
        "outcome",
        "effect_state",
        "reason",
        "rule_id",
        "tool_id",
        "action",
        "finish_reason",
        "kind",
        "elapsed_ms",
        "attempts",
        "attempt",
        "turns",
        "tokens",
        "tokens_unknown_calls",
        "unbilled_model_intents",
        "budget_enforced",
        "n_applied",
        "n_unknown",
        "from",
        "to",
        "decision",
    }
    for key in allowed_scalar_keys:
        if key not in payload:
            continue
        val = payload[key]
        if isinstance(val, (int, float, bool)) or val is None:
            safe[key] = val
        elif isinstance(val, str):
            safe[key] = redact_diagnostic_text(val, extra_paths=redact_paths)
    if "error" in payload and payload["error"] is not None:
        safe["error"] = redact_diagnostic_text(
            str(payload["error"]), extra_paths=redact_paths
        )
    return safe


def _collect_sections(
    data_root: Path,
    selected: tuple[str, ...],
    *,
    redact_sensitive: bool,
) -> dict[str, Any]:
    root = Path(data_root)
    db_path = root / "events.db"
    home = str(Path.home())
    extra = (str(root), home)
    collected: dict[str, Any] = {}

    if "system" in selected:
        collected["system"] = {
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "machine": platform.machine(),
            "os_release": platform.release(),
            "data_root": "<DATA_ROOT>" if redact_sensitive else str(root),
            "db_exists": db_path.is_file(),
            "db_size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
            "redacted": redact_sensitive,
        }

    if db_path.is_file() and (
        {"runs_summary", "events_timeline", "schedules"} & set(selected)
    ):
        conn = _connect_ro(db_path)
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "runs_summary" in selected:
                if "runs" in tables:
                    rows = conn.execute(
                        "SELECT run_id, task_id, state, outcome, effect_state,"
                        " control_requested, goal_revision, lease_epoch,"
                        " created_at, ended_at"
                        " FROM runs ORDER BY created_at DESC LIMIT 100"
                    ).fetchall()
                    collected["runs_summary"] = [dict(r) for r in rows]
                else:
                    collected["runs_summary"] = []

            if "events_timeline" in selected:
                if "events" in tables:
                    rows = conn.execute(
                        "SELECT seq, run_id, task_id, type, ts, actor_kind, payload"
                        " FROM events ORDER BY seq DESC LIMIT 500"
                    ).fetchall()
                    events_out = []
                    for row in reversed(rows):
                        raw_payload = row["payload"]
                        parsed: dict[str, Any] = {}
                        if isinstance(raw_payload, str) and raw_payload:
                            try:
                                loaded = json.loads(raw_payload)
                                if isinstance(loaded, dict):
                                    parsed = loaded
                            except json.JSONDecodeError:
                                parsed = {}
                        events_out.append(
                            {
                                "seq": row["seq"],
                                "run_id": row["run_id"],
                                "task_id": row["task_id"],
                                "type": row["type"],
                                "ts": row["ts"],
                                "actor_kind": row["actor_kind"],
                                "summary": (
                                    _sanitize_event_payload(parsed, redact_paths=extra)
                                    if redact_sensitive
                                    else parsed
                                ),
                            }
                        )
                    collected["events_timeline"] = events_out
                else:
                    collected["events_timeline"] = []

            if "schedules" in selected:
                if "schedules" in tables:
                    rows = conn.execute(
                        "SELECT schedule_id, goal, workspaces_json, interval_seconds,"
                        " enabled, next_fire_at, last_fired_at, last_error, created_at"
                        " FROM schedules ORDER BY created_at ASC"
                    ).fetchall()
                    sched_out = []
                    for row in rows:
                        d = dict(row)
                        ws_raw = d.pop("workspaces_json", "[]")
                        try:
                            ws_list = json.loads(ws_raw) if ws_raw else []
                        except json.JSONDecodeError:
                            ws_list = []
                        if redact_sensitive:
                            d["goal"] = "<GOAL_REDACTED>"
                            d["workspace_count"] = (
                                len(ws_list) if isinstance(ws_list, list) else 0
                            )
                            if d.get("last_error"):
                                d["last_error"] = redact_diagnostic_text(
                                    str(d["last_error"]), extra_paths=extra
                                )
                        else:
                            d["workspaces"] = ws_list
                        sched_out.append(d)
                    collected["schedules"] = sched_out
                else:
                    collected["schedules"] = []
        finally:
            conn.close()
    else:
        for empty_sec in ("runs_summary", "events_timeline", "schedules"):
            if empty_sec in selected:
                collected[empty_sec] = []

    if "metrics" in selected:
        m_path = root / "metrics.jsonl"
        if m_path.is_file():
            raw_metrics = m_path.read_bytes().decode("utf-8", errors="replace")
            lines = raw_metrics.splitlines()[-200:]
            collected["metrics"] = [
                redact_diagnostic_text(line, extra_paths=extra)
                if redact_sensitive
                else line
                for line in lines
            ]
        else:
            collected["metrics"] = []

    if "daemon_log" in selected:
        log_path = root / "logs" / "daemon.log"
        if log_path.is_file():
            raw_log = log_path.read_bytes().decode("utf-8", errors="replace")
            lines = raw_log.splitlines()[-200:]
            collected["daemon_log"] = [
                redact_diagnostic_text(line, extra_paths=extra)
                if redact_sensitive
                else line
                for line in lines
            ]
        else:
            collected["daemon_log"] = []

    return collected


def _build_manifest(
    collected: dict[str, Any],
    selected: tuple[str, ...],
    *,
    redact_sensitive: bool,
) -> dict[str, Any]:
    items = []
    for name in DIAGNOSTIC_SECTIONS:
        included = name in selected
        payload = collected.get(name) if included else None
        count = (
            len(payload)
            if isinstance(payload, list)
            else (1 if isinstance(payload, dict) else 0)
        )
        encoded_bytes = (
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            if included
            else 0
        )
        items.append(
            {
                "section": name,
                "included": included,
                "description": SECTION_DESCRIPTIONS[name],
                "record_count": count,
                "estimated_bytes": encoded_bytes,
            }
        )
    return {
        "format_version": 1,
        "generated_at": int(time.time() * 1000),
        "redacted": redact_sensitive,
        "local_only": True,
        "sections": items,
    }


def preview_diagnostics(
    data_root: Path,
    *,
    sections: Sequence[str] | None = None,
    redact_sensitive: bool = True,
) -> dict[str, Any]:
    """只读生成诊断包内容预览清单，不写任何磁盘文件。"""
    selected = _normalize_sections(sections)
    collected = _collect_sections(
        data_root, selected, redact_sensitive=redact_sensitive
    )
    return _build_manifest(collected, selected, redact_sensitive=redact_sensitive)


def _write_archive_exclusive(dest_dir: Path, base_stem: str, data: bytes) -> Path:
    """以二进制 O_CREAT | O_EXCL 独占写出归档，防止 Windows 文本模式损坏 zip 且不覆盖旧包。"""
    open_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    )
    for idx in range(1000):
        suffix = "" if idx == 0 else f"-{idx}"
        candidate = dest_dir / f"{base_stem}{suffix}.zip"
        try:
            fd = os.open(candidate, open_flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        return candidate
    raise TianshuError("导出目录同名诊断包过多，拒绝覆盖既有归档")


def export_diagnostics(
    data_root: Path,
    out_dir: Path,
    *,
    sections: Sequence[str] | None = None,
    redact_sensitive: bool = True,
) -> dict[str, Any]:
    """将默认脱敏的诊断包写成 `<out_dir>/tianshu-diagnostics-<ts>.zip`（纯本地，绝不上传）。"""
    selected = _normalize_sections(sections)
    root_resolved = Path(data_root).expanduser().resolve()
    dest_dir = Path(out_dir).expanduser().resolve()
    if dest_dir == root_resolved or root_resolved in dest_dir.parents:
        raise TianshuError("诊断包导出目录不得位于 data_root 内部")
    if dest_dir.exists() and not dest_dir.is_dir():
        raise TianshuError("导出目标路径已存在且不是目录")
    dest_dir.mkdir(parents=True, exist_ok=True)

    collected = _collect_sections(
        data_root, selected, redact_sensitive=redact_sensitive
    )
    manifest = _build_manifest(collected, selected, redact_sensitive=redact_sensitive)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        for sec in selected:
            zf.writestr(
                f"{sec}.json",
                json.dumps(collected[sec], ensure_ascii=False, indent=2) + "\n",
            )

    now_ms = int(time.time() * 1000)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ms / 1000))
    ms_rem = now_ms % 1000
    stem = f"tianshu-diagnostics-{ts}-{ms_rem:03d}-{os.getpid()}"
    archive_path = _write_archive_exclusive(dest_dir, stem, buf.getvalue())
    return {
        "ok": True,
        "archive_path": str(archive_path),
        "manifest": manifest,
    }
