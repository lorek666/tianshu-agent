#!/usr/bin/env python3
"""PRD §10.6 本地运行质量 SLO 基准测量与门禁评估器（G3-B10）。

用法：
    uv run --frozen python scripts/measure_slo.py \
        --output-json /tmp/slo-report.json \
        --output-md docs/SLO-BASELINE-2026-11-18.md

特性与硬约束：
1. 纯 Python 3.12 标准库，零外部依赖，零外网连接；
2. 全部测量运行在隔离的临时数据根（独立 `TIANSHU_DATA_ROOT`、`TIANSHU_AUTHORITY_ROOT`、
   `TIANSHU_TEST_LOCK_ROOT`），绝不触碰宿主机真实用户目录、钥匙串或全局锁；
3. 三大实测套件：
   - Suite 1（启动可用性）：空库冷启动 (`startup_cold_ms`) vs 50 任务 / ≥500 事件热库启动
     (`startup_warm_ms`)，实测拉起真实 `host` + `daemon` 子进程直至 HTTP `/state` 与
     `task.create` 可用；
   - Suite 2（真实慢订阅者背压下的取消与审批响应）：同时挂载真实不读数据的 IPC 订阅者与
     Host SSE 订阅者填满发送通道，分别在慢模型与慢工具下测量 `cancel_ack_ms`、
     `cancel_stop_dispatch_ms` 与 `approval_rpc_ms`，单独列账在途副作用并核验游标重连；
   - Suite 3（崩溃重启恢复）：跨 4 类真实 `os._exit(77)` 硬退故障点（已批准写 `after_intent`、
     已批准写 `after_write`、撕裂未知副作用 `reconciling/blocked`、未决摘要模型与预算预占）
     重启真实 `daemon` 子进程测量 `restart_recovery_ms`，并断言 **0 盲目重试、0 丢失回执、
     0 泄漏预算预占**；
4. 严格门禁与防事后放宽（Anti-Relaxation）：缺指标、样本不足、超 `p95_max`/`max_ceiling`、
   安全不变量违背或未升版放宽阈值一律非零退出；所有输出按 D7 默认脱敏本机路径。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns
from typing import Any, BinaryIO

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tianshu import authority, daemon, event_schema, host, ipc, maintenance  # noqa: E402
from tianshu import metrics as metrics_mod  # noqa: E402
from tianshu.budget import BudgetLedger  # noqa: E402
from tianshu.capability import CapabilityProfile  # noqa: E402
from tianshu.diagnostics_export import redact_diagnostic_text  # noqa: E402
from tianshu.events import EventLog  # noqa: E402
from tianshu.execution_admission import process_birth_identity  # noqa: E402
from tianshu.leases import pid_alive  # noqa: E402
from tianshu.loop import AgentLoop, Limits, build_stack  # noqa: E402
from tianshu.model import (  # noqa: E402
    ModelIdentity,
    ModelResponse,
    ModelStreamDelta,
    ScriptedModel,
    ToolCall,
)
from tianshu.permissions import HumanIdentity  # noqa: E402
from tianshu.receipt import receipts_of  # noqa: E402
from tianshu.replay import replay  # noqa: E402
from tianshu.restore_access import default_material_paths  # noqa: E402
from tianshu.summaries import SUMMARY_MAX_BYTES, SummaryStore  # noqa: E402

DEFAULT_SCHEMA_PATH = REPO_ROOT / "schemas" / "slo" / "slo-v1.json"
DEFAULT_THRESHOLDS_PATH = REPO_ROOT / "schemas" / "slo" / "slo-thresholds-v1.json"
REAL_UI_DIST = REPO_ROOT / "ui" / "dist"

REQUIRED_SLO_METRICS: tuple[str, ...] = (
    "startup_cold_ms",
    "startup_warm_ms",
    "cancel_ack_ms",
    "cancel_stop_dispatch_ms",
    "approval_rpc_ms",
    "restart_recovery_ms",
)

#: 代码侧冻结的 v1 阈值上限（与 `schemas/slo/slo-thresholds-v1.json` 双向绑定）。
#: 若在不升级 `policy_version` + `schema_version` 的前提下放宽任何阈值或减少重复数，
#: `check_no_unversioned_relaxation()` 将立即以退出码 2 拒绝（PRD §10.6）。
FROZEN_POLICY_VERSION = 1
FROZEN_MIN_REPEATS = 10
FROZEN_V1_METRIC_LIMITS: dict[str, dict[str, float | int]] = {
    "startup_cold_ms": {"p95_max": 2200.0, "max_ceiling": 4500.0, "min_repeats": 10},
    "startup_warm_ms": {"p95_max": 2500.0, "max_ceiling": 5000.0, "min_repeats": 10},
    "cancel_ack_ms": {"p95_max": 500.0, "max_ceiling": 1200.0, "min_repeats": 10},
    "cancel_stop_dispatch_ms": {
        "p95_max": 1000.0,
        "max_ceiling": 2500.0,
        "min_repeats": 10,
    },
    "approval_rpc_ms": {"p95_max": 500.0, "max_ceiling": 1200.0, "min_repeats": 10},
    "restart_recovery_ms": {"p95_max": 2000.0, "max_ceiling": 4500.0, "min_repeats": 10},
}
FROZEN_V1_SOAK_LIMITS: dict[str, float | int] = {
    "rss_idle_max_mib": 256.0,
    "rss_active_max_mib": 512.0,
    "rss_slope_max_mib_per_hour": 32.0,
    "rss_net_growth_max_mib": 128.0,
    "open_fds_max": 512,
    "open_fds_growth_max": 64,
    "disk_growth_max_mib": 512.0,
    "max_orphan_processes": 0,
}

BIG_EVENT_CHARS = 180_000
SUITE2_SEND_STALL_S = 0.35
SUITE2_DRIVER_INTERVAL_S = 0.015
_STATE_PRESSURE_DIR = "state-pressure-dev"
_STATE_CONTROL_LABEL = "slo-state-pressure/control"
_STATE_SUBSCRIBER_PREFIX = "slo-state-pressure/subscriber/"
_STATE_READY_SEND_AGE_NS = 10_000_000
_STATE_OBSERVATION_WAIT_S = 2.0
IN_FLIGHT_HOLD_MS = 40.0
SUMMARY_CONSTRAINT = "硬约束：报告只用中文。"
SUMMARY_BUDGET_BYTES = 3000
SUMMARY_ROUNDS = 6


class TrackedPids(dict[int, set[str | None]]):
    """Native observations, with one binding per logical lock owner."""

    def __init__(self) -> None:
        super().__init__()
        self.lock_owners: set[str] = set()
        self.sources: dict[int, set[str]] = {}
        self.bindings: dict[tuple[int, str], dict[str, Any]] = {}
        self.host_stops: list[dict[str, Any]] = []


class SloConfigError(Exception):
    """SLO schema / 阈值配置非法或存在未升版放宽（退出码 2）。"""


# --------------------------------------------------------------------------- Schema 校验与防放宽


def load_slo_schema_defs(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    try:
        doc = json.loads(Path(schema_path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SloConfigError(f"无法读取 SLO schema {schema_path}：{exc}") from exc
    if not isinstance(doc, dict) or "$schema" not in doc or "$defs" not in doc:
        raise SloConfigError(f"SLO schema 缺少 $schema 或 $defs：{schema_path}")
    event_schema.require_supported_dialect(doc["$schema"])
    defs = doc["$defs"]
    if not isinstance(defs, dict):
        raise SloConfigError("SLO schema 的 $defs 必须是对象")
    for name, node in defs.items():
        event_schema.validate_schema_definition(node, where=f"slo-v1#{name}")
    return defs


def _check_numeric_bounds(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(float(value)) or math.isinf(float(value)):
            errors.append(f"{path}：数值不能是 NaN 或 Inf")
            return
        if "minimum" in schema and float(value) < float(schema["minimum"]):
            errors.append(f"{path}：{value} < minimum {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, sub in value.items():
            child = f"{path}.{key}"
            if key in props:
                _check_numeric_bounds(props[key], sub, child, errors)
            elif isinstance(additional, dict):
                _check_numeric_bounds(additional, sub, child, errors)
    elif isinstance(value, list) and "items" in schema:
        for idx, item in enumerate(value):
            _check_numeric_bounds(schema["items"], item, f"{path}[{idx}]", errors)


def validate_against_def(
    defs: dict[str, Any], def_name: str, payload: Any
) -> list[str]:
    if def_name not in defs:
        return [f"$：未知 schema 定义 {def_name!r}"]
    schema = defs[def_name]
    errors: list[str] = []
    event_schema._check_value(schema, payload, "$", errors, def_name)
    if not errors:
        _check_numeric_bounds(schema, payload, "$", errors)
    return errors


def check_no_unversioned_relaxation(
    thresholds: dict[str, Any],
    *,
    reference_version: int = FROZEN_POLICY_VERSION,
    reference_min_repeats: int = FROZEN_MIN_REPEATS,
    reference_metrics: dict[str, dict[str, float | int]] | None = None,
    reference_soak: dict[str, float | int] | None = None,
) -> list[str]:
    """核对阈值没有在未升级 policy_version + schema_version 的情况下被放宽。"""
    ref_metrics = FROZEN_V1_METRIC_LIMITS if reference_metrics is None else reference_metrics
    ref_soak = FROZEN_V1_SOAK_LIMITS if reference_soak is None else reference_soak
    violations: list[str] = []

    policy_version = thresholds.get("policy_version")
    schema_version = thresholds.get("schema_version")
    expected_schema_tag = f"slo-thresholds-v{policy_version}"
    if schema_version != expected_schema_tag:
        violations.append(
            f"schema_version ({schema_version!r}) 必须与 policy_version ({policy_version}) "
            f"绑定为 {expected_schema_tag!r}"
        )
    if not isinstance(policy_version, int) or isinstance(policy_version, bool):
        violations.append("policy_version 必须是正整数")
        return violations

    metrics_cfg = thresholds.get("metrics")
    if not isinstance(metrics_cfg, dict):
        violations.append("metrics 必须是对象")
        return violations

    missing = sorted(set(REQUIRED_SLO_METRICS) - set(metrics_cfg))
    if missing:
        violations.append(f"阈值配置缺失必需指标：{missing}")

    for metric_name, mcfg in metrics_cfg.items():
        if not isinstance(mcfg, dict):
            continue
        if float(mcfg.get("max_ceiling", -1)) < float(mcfg.get("p95_max", 0)):
            violations.append(
                f"{metric_name}: max_ceiling ({mcfg.get('max_ceiling')}) 不能小于 "
                f"p95_max ({mcfg.get('p95_max')})"
            )

    if policy_version < reference_version:
        violations.append(
            f"policy_version ({policy_version}) 不能回退到已冻结版本 ({reference_version}) 之前"
        )
        return violations

    if policy_version == reference_version:
        if int(thresholds.get("min_repeats", 0)) < int(reference_min_repeats):
            violations.append(
                "threshold_relaxed_without_version_bump: "
                f"min_repeats 从 {reference_min_repeats} 放宽为 {thresholds.get('min_repeats')}"
            )
        for metric_name, frozen in ref_metrics.items():
            cur = metrics_cfg.get(metric_name)
            if not isinstance(cur, dict):
                continue
            if float(cur["p95_max"]) > float(frozen["p95_max"]):
                violations.append(
                    f"threshold_relaxed_without_version_bump: {metric_name}.p95_max "
                    f"从 {frozen['p95_max']} 放宽为 {cur['p95_max']}"
                )
            if float(cur["max_ceiling"]) > float(frozen["max_ceiling"]):
                violations.append(
                    f"threshold_relaxed_without_version_bump: {metric_name}.max_ceiling "
                    f"从 {frozen['max_ceiling']} 放宽为 {cur['max_ceiling']}"
                )
            if int(cur["min_repeats"]) < int(frozen["min_repeats"]):
                violations.append(
                    f"threshold_relaxed_without_version_bump: {metric_name}.min_repeats "
                    f"从 {frozen['min_repeats']} 放宽为 {cur['min_repeats']}"
                )
        soak_cfg = thresholds.get("soak_limits")
        if isinstance(soak_cfg, dict):
            for key, frozen_val in ref_soak.items():
                if key not in soak_cfg:
                    continue
                if float(soak_cfg[key]) > float(frozen_val):
                    violations.append(
                        f"threshold_relaxed_without_version_bump: soak_limits.{key} "
                        f"从 {frozen_val} 放宽为 {soak_cfg[key]}"
                    )
    return violations


def load_and_validate_thresholds(
    thresholds_path: Path = DEFAULT_THRESHOLDS_PATH,
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    defs = load_slo_schema_defs(schema_path)
    try:
        raw_bytes = Path(thresholds_path).read_bytes()
        thresholds = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SloConfigError(f"无法读取阈值配置 {thresholds_path}：{exc}") from exc

    schema_errors = validate_against_def(defs, "thresholdsConfig", thresholds)
    if schema_errors:
        raise SloConfigError("阈值配置违反 slo-v1.json：" + "；".join(schema_errors))

    relax_errors = check_no_unversioned_relaxation(thresholds)
    if relax_errors:
        raise SloConfigError("阈值防放宽门禁拒绝：" + "；".join(relax_errors))

    canonical = json.dumps(thresholds, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return thresholds, digest, defs


# --------------------------------------------------------------------------- 环境与辅助工具


def _short_temp_dir(prefix: str) -> Path:
    base = None if os.name == "nt" else os.environ.get("TIANSHU_TEST_TMP_ROOT", "/tmp")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _write_bytes_exact(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _isolated_env(
    *,
    data_root: Path,
    authority_root: Path,
    lock_root: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    for secret_key in (
        "TIANSHU_API_KEY",
        "ANTHROPIC_API_KEY",
        "TIANSHU_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "TIANSHU_EMBEDDING_API_KEY",
        "TIANSHU_MCP_CONFIG",
        "TIANSHU_FAILPOINT",
        "TIANSHU_FAILPOINT_PATH",
    ):
        env.pop(secret_key, None)
    py_path = str(SRC_DIR)
    if env.get("PYTHONPATH"):
        py_path = py_path + os.pathsep + env["PYTHONPATH"]
    env.update(
        {
            "PYTHONPATH": py_path,
            "PYTHONIOENCODING": "utf-8",
            "TIANSHU_DATA_ROOT": str(data_root),
            "TIANSHU_AUTHORITY_ROOT": str(authority_root),
            "TIANSHU_TEST_LOCK_ROOT": str(lock_root),
            "TIANSHU_SANDBOX_BACKEND": "local_fs",
            "TIANSHU_NET_MIN_INTERVAL_S": "0",
            "TIANSHU_CREDENTIAL_BACKEND": "env",
            "TIANSHU_HOST_PROCESS_DIAGNOSTICS": "1",
        }
    )
    if extra:
        env.update(extra)
    return env


def collect_hardware_metadata() -> dict[str, Any]:
    cpu_model = platform.processor() or platform.machine() or "unknown"
    mem_gib: float | None = None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                cpu_model = out.stdout.strip()
        except OSError:
            pass
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                mem_gib = float(round(int(out.stdout.strip()) / (1024**3), 2))
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            meminfo = Path("/proc/meminfo").read_bytes().decode("utf-8", errors="replace")
            for line in meminfo.splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        mem_gib = float(round(int(parts[1]) / (1024**2), 2))
                    break
        except OSError:
            pass
        try:
            cpuinfo = Path("/proc/cpuinfo").read_bytes().decode("utf-8", errors="replace")
            for line in cpuinfo.splitlines():
                if "model name" in line and ":" in line:
                    cpu_model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    elif os.name == "nt":
        cpu_model = os.environ.get("PROCESSOR_IDENTIFIER") or cpu_model
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine() or "unknown",
        "cpu_model": cpu_model,
        "memory_gib": mem_gib,
        "python_version": platform.python_version(),
        "redaction": "d7_local_redacted",
    }


def _redact_obj(value: Any, *, extra_paths: list[str]) -> Any:
    if isinstance(value, str):
        return redact_diagnostic_text(value, extra_paths=extra_paths)
    if isinstance(value, list):
        return [_redact_obj(item, extra_paths=extra_paths) for item in value]
    if isinstance(value, dict):
        return {
            str(k): _redact_obj(v, extra_paths=extra_paths)
            for k, v in value.items()
        }
    return value


def _http_request(
    port: int,
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    raw_body = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = [
        f"{method} {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        f"X-Tianshu-Token: {token}",
        "Connection: close",
    ]
    if body is not None:
        headers.append("Content-Type: application/json; charset=utf-8")
        headers.append(f"Content-Length: {len(raw_body)}")
    wire = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + raw_body
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(wire)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = status_line.split(" ")
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    parsed = json.loads(payload.decode("utf-8")) if payload.strip() else {}
    return status, parsed


def _record_pid(
    pid: int, tracked_pids: dict[int, set[str | None]], *, source: str = "managed_root",
    phase: str = "started",
) -> None:
    # Keep every observed instance if the OS recycles a PID during this run.
    birth = process_birth_identity(pid)
    tracked_pids.setdefault(pid, set()).add(birth)
    if isinstance(tracked_pids, TrackedPids):
        tracked_pids.sources.setdefault(pid, set()).add(source)
        _observe_binding(pid, source, birth, tracked_pids, phase=phase, bind=True)


def _observe_binding(
    pid: int, source: str, birth: str | None, tracked_pids: TrackedPids, *,
    phase: str, bind: bool,
) -> None:
    now = monotonic_ns()
    row = tracked_pids.bindings.setdefault((pid, source), {
        "pid": pid, "source": source, "first_birth": birth,
        "first_phase": phase, "first_monotonic_ns": now, "recorded_births": [],
    })
    if bind and birth not in row["recorded_births"]:
        row["recorded_births"].append(birth)
    row.update(last_birth=birth, last_phase=phase, last_monotonic_ns=now)


def _record_lock_pids(
    data_root: Path, tracked_pids: TrackedPids, *, phase: str = "sample",
) -> None:
    for lock_name in ("host.lock", "daemon.lock"):
        p = ipc.run_dir(data_root) / lock_name
        try:
            obj = json.loads(p.read_bytes().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        pid = obj.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        instance = obj.get("token" if lock_name == "host.lock" else "instance_id")
        started = obj.get("started_at")
        owner = None
        if isinstance(instance, str) and instance and type(started) is int and started > 0:
            key = (str(p.resolve()), pid, started, instance)
            owner = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
        source = f"{lock_name}:{owner or 'unbound'}"
        if owner is not None and owner in tracked_pids.lock_owners:
            # Observe a reread for diagnosis, without assigning its current PID
            # occupant to the old owner or erasing an unreadable initial birth.
            _observe_binding(
                pid, source, process_birth_identity(pid), tracked_pids,
                phase=phase, bind=False,
            )
            continue
        # Re-reading an unchanged lock cannot make a later PID occupant belong
        # to that owner. An unreadable first identity remains conservative.
        _record_pid(pid, tracked_pids, source=source, phase=phase)
        if owner is not None:
            tracked_pids.lock_owners.add(owner)


def _capture_host_stop(
    proc: subprocess.Popen[bytes], tracked_pids: TrackedPids, stderr: BinaryIO, *,
    phase: str,
) -> None:
    """Keep actual owned-handle facts; never infer that a launcher covers descendants."""
    tracked_pids.host_stops.append({
        "source": "owned_host_root", "observer_pid": os.getpid(), "pid": proc.pid,
        "phase": phase, "monotonic_ns": monotonic_ns(), "returncode": proc.returncode,
    })
    stderr.seek(0, os.SEEK_END)
    stderr.seek(max(0, stderr.tell() - 32768))
    for line in stderr.read(32768).splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        row = obj.get("host_supervisor_process") if isinstance(obj, dict) else None
        if (
            not isinstance(row, dict) or not isinstance(row.get("action"), str)
            or row["action"] not in {
                "wait_exited", "wait_timeout", "terminate_requested",
                "final_wait_exited", "final_wait_timeout", "wait_error", "final_wait_error",
            }
        ):
            continue
        if any(type(row.get(key)) is not int for key in ("pid", "observer_pid", "monotonic_ns")):
            continue
        if row.get("returncode") is not None and type(row["returncode"]) is not int:
            continue
        facts = {key: row[key] for key in ("pid", "observer_pid", "monotonic_ns", "action")}
        facts.update(source="owned_daemon_root", phase=phase, returncode=row.get("returncode"))
        if type(row.get("errno")) is int:
            facts["errno"] = row["errno"]
        result = row.get("result")
        if (
            isinstance(result, dict) and isinstance(result.get("outcome"), str)
            and result["outcome"] in {
                "command_exited", "command_timeout", "signal_sent", "os_error",
            }
        ):
            facts["terminate_outcome"] = result["outcome"]
            for key in ("errno", "returncode"):
                if type(result.get(key)) is int:
                    facts[f"terminate_{key}"] = result[key]
        tracked_pids.host_stops.append(facts)


def _native_python_command(command: list[str]) -> list[str]:
    """Own the native interpreter process, not a Windows venv/uv launcher."""
    if command and command[0] == sys.executable:
        return [sys._base_executable, *command[1:]]
    return command


def _stderr_tail(stderr: BinaryIO, limit: int = 600) -> str:
    stderr.seek(0, os.SEEK_END)
    stderr.seek(max(0, stderr.tell() - limit))
    return stderr.read(limit).decode("utf-8", errors="replace")


def _wait_host_ready(
    proc: subprocess.Popen[bytes], data_root: Path, stderr: BinaryIO, *, timeout: float,
) -> host.HostInfo:
    """Bind readiness to the spawned Host PID and its OS birth identity."""
    birth = process_birth_identity(proc.pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Host 启动进程提前退出 rc={proc.returncode}: {_stderr_tail(stderr)}"
            )
        # An occupied endpoint or a readable lock is not proof that it belongs
        # to this cluster. Unreadable/reused identity must never be accepted.
        if birth is None or process_birth_identity(proc.pid) != birth:
            time.sleep(0.005)
            continue
        try:
            candidate = host.read_host_info(data_root)
        except host.HostError:
            candidate = None
        if (
            candidate is not None and candidate.pid == proc.pid
            and candidate.port > 0 and host._http_alive(candidate)  # noqa: SLF001
        ):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Host 启动进程提前退出 rc={proc.returncode}: {_stderr_tail(stderr)}"
                )
            try:
                confirmed = host.read_host_info(data_root)
            except host.HostError:
                confirmed = None
            if (
                proc.poll() is None and birth is not None
                and process_birth_identity(proc.pid) == birth
                and confirmed == candidate
            ):
                return candidate
        time.sleep(0.005)
    raise RuntimeError(f"Host 在 {timeout}s 内未就绪，或就绪实例不属于本次启动的进程")


def _native_python_child(
    command: list[str], *, env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    if os.name != "nt":
        return command, env
    native = _native_python_command(command)
    if native == command:
        return command, env
    return native, {**env, "PYTHONPATH": os.pathsep.join(sys.path)}


def _launch_host(
    command: list[str], *, env: dict[str, str],
) -> tuple[subprocess.Popen[bytes], BinaryIO]:
    command, env = _native_python_child(command, env=env)
    # A native file cannot block shutdown on an unread stderr pipe.
    stderr = tempfile.TemporaryFile()
    try:
        return subprocess.Popen(  # noqa: S603 - caller constructs a fixed Host argv
            _native_python_command(command), stdout=subprocess.DEVNULL, stderr=stderr, env=env,
        ), stderr
    except BaseException:
        stderr.close()
        raise


def _launch_recovery_daemon(
    command: list[str], *, env: dict[str, str],
) -> tuple[subprocess.Popen[bytes], BinaryIO]:
    command, env = _native_python_child(command, env=env)
    stderr = tempfile.TemporaryFile()
    try:
        return subprocess.Popen(  # noqa: S603 - caller constructs a fixed Daemon argv
            command, stdout=subprocess.DEVNULL, stderr=stderr, env=env,
        ), stderr
    except BaseException:
        stderr.close()
        raise


def _find_orphan_pids(
    tracked_pids: dict[int, set[str | None]], marker_root: Path,
) -> list[int]:
    me = os.getpid()
    identities = {pid: set(births) for pid, births in tracked_pids.items()}
    observations: dict[int, dict[str, Any]] = {}

    def still_alive(pid: int) -> bool:
        running = pid_alive(pid)
        current = process_birth_identity(pid) if running else None
        retained = running and (
            current is None or None in identities[pid] or current in identities[pid]
        )
        observations[pid] = {
            "pid": pid, "recorded_births": sorted(identities[pid], key=str),
            "pid_alive": running, "current_birth": current,
            "liveness_source": "leases.pid_alive", "native_queries_atomic": False,
            "monotonic_ns": monotonic_ns(),
            "reason": (
                "not_alive" if not running else
                "unreadable_current_identity" if current is None else
                "unreadable_recorded_identity" if None in identities[pid] else
                "recorded_instance_running" if retained else "different_instance"
            ),
        }
        # An unreadable identity cannot prove the recorded process has exited.
        return retained

    alive: set[int] = set()
    for pid in sorted(tracked_pids):
        if pid <= 0 or pid == me:
            continue
        if still_alive(pid):
            alive.add(pid)
    if os.name != "nt":
        marker = str(marker_root)
        try:
            proc = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    stripped = line.strip()
                    if not stripped or marker not in stripped:
                        continue
                    pid_part = stripped.split(None, 1)[0]
                    if pid_part.isdigit():
                        found_pid = int(pid_part)
                        if found_pid != me and pid_alive(found_pid) is True:
                            _record_pid(
                                found_pid, tracked_pids, source="command_line_marker",
                                phase="orphan_scan",
                            )
                            identities[found_pid] = set(tracked_pids[found_pid])
                            if still_alive(found_pid):
                                alive.add(found_pid)
        except OSError:
            pass
    if not alive:
        return []
    # 给刚退出的子进程至多 250ms 内核回收窗口（避免僵尸进程尚未 reap 的瞬态假阳）
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline and alive:
        alive = {pid for pid in alive if still_alive(pid)}
        if alive:
            time.sleep(0.03)
    if alive:
        evidence: dict[str, Any] = {
            "orphan_process_observations": [observations[pid] for pid in sorted(alive)],
        }
        if isinstance(tracked_pids, TrackedPids):
            evidence["process_sources"] = {
                str(pid): sorted(tracked_pids.sources.get(pid, set())) for pid in sorted(alive)
            }
            evidence["process_bindings"] = [
                row for (pid, _), row in tracked_pids.bindings.items() if pid in alive
            ]
            evidence["host_stop_observations"] = tracked_pids.host_stops
        print(json.dumps(evidence, sort_keys=True), file=sys.stderr, flush=True)
    return sorted(alive)


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(dirpath, name), follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _summarize_samples(
    samples: list[float | None],
    *,
    p95_max: float,
    max_ceiling: float,
    min_repeats_required: int,
) -> dict[str, Any]:
    numeric = [float(v) for v in samples if v is not None]
    unknown_count = sum(1 for v in samples if v is None)
    p50 = metrics_mod.percentile(numeric, 50)
    p95 = metrics_mod.percentile(numeric, 95)
    min_v = min(numeric) if numeric else None
    max_v = max(numeric) if numeric else None
    passed = (
        unknown_count == 0
        and len(numeric) >= min_repeats_required
        and p95 is not None
        and max_v is not None
        and p95 <= p95_max
        and max_v <= max_ceiling
    )
    return {
        "unit": "ms",
        "repeats": len(numeric),
        "unknown_count": unknown_count,
        "min_ms": None if min_v is None else round(min_v, 3),
        "p50_ms": None if p50 is None else round(p50, 3),
        "p95_ms": None if p95 is None else round(p95, 3),
        "max_ms": None if max_v is None else round(max_v, 3),
        "threshold_p95_ms": float(p95_max),
        "threshold_max_ms": float(max_ceiling),
        "min_repeats_required": int(min_repeats_required),
        "passed": bool(passed),
    }


# --------------------------------------------------------------------------- Suite 2 开发观测


def _same_lock_root(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _require_state_pressure_test_isolation(expected_lock_root: Path | None = None) -> None:
    configured = os.environ.get("TIANSHU_TEST_LOCK_ROOT")
    if not (os.environ.get("PYTEST_CURRENT_TEST") and configured):
        raise SloConfigError("state 背压诊断仅能在仓库隔离的 pytest 夹具内运行")
    lock_root = Path(configured)
    if not _same_lock_root(lock_root, maintenance._lock_root()) or (  # noqa: SLF001
        expected_lock_root is not None
        and not _same_lock_root(lock_root, expected_lock_root)
    ):
        raise SloConfigError("state 背压诊断的测试锁根未生效；不启动子进程")


class _LabelledSuite2Client(ipc.Client):
    """仅设置已有 hello.client 观察标签；连接和请求仍走正常认证、协议及调度。"""

    def __init__(self, root: Path, *, timeout: float, label: str) -> None:
        super().__init__(root, timeout=timeout)
        self.label = label

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if method == "hello" and params is not None:
            params = {**params, "client": self.label}
        return super().call(method, params, timeout=timeout)


@dataclass(frozen=True, slots=True)
class _ActiveSend:
    sending_since: float
    observed_ns: int
    age_ns: int
    sent_frames: int


def _send_age_ns(sending_since: float) -> int:
    # _sending_since 属于 Daemon watchdog 的 monotonic 时钟域，不能与 perf_counter 相减。
    return round((time.monotonic() - sending_since) * 1_000_000_000)


def _active_send(conn: daemon._ClientConnection) -> _ActiveSend | None:
    with conn._lock:  # noqa: SLF001 - 与真实发送线程同步读取，绝不修改 sender
        started = conn._sending_since  # noqa: SLF001
        # 排除 hello/subscribe 回执正写出，观察的必须是订阅之后的推送。
        if not conn.subscribed or conn.closed or conn.sent_frames < 2 or started is None:
            return None
        return _ActiveSend(started, time.perf_counter_ns(), _send_age_ns(started), conn.sent_frames)


@dataclass(slots=True)
class _PressureObservation:
    conn: daemon._ClientConnection
    ready: _ActiveSend | None = None
    dispatch: _ActiveSend | None = None
    dispatch_ns: int | None = None


@contextlib.contextmanager
def _observe_state_pressure(out_dir: Path) -> Iterator[None]:
    """只在隔离测试的 Daemon 子进程中观测；原派发、看门狗及断连逻辑照常执行。"""
    from unittest.mock import patch

    target = daemon._Daemon  # noqa: SLF001
    handle = target._handle  # noqa: SLF001
    watchdog = target._check_slow_subscribers  # noqa: SLF001
    record_drop = target._record_drop  # noqa: SLF001
    trials: dict[str, _PressureObservation] = {}
    ambiguous: set[str] = set()
    active_tag: str | None = None
    control_conn: daemon._ClientConnection | None = None
    ambiguous_control = False

    def publish(tag: str, payload: dict[str, Any]) -> None:
        path = out_dir / f"{tag}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"trial": tag, **payload}), encoding="utf-8")
        temp.replace(path)

    def subscriber_tag(conn: daemon._ClientConnection) -> str | None:
        label = conn.raw.client.get("client", "")
        if not label.startswith(_STATE_SUBSCRIBER_PREFIX):
            return None
        tag = label.removeprefix(_STATE_SUBSCRIBER_PREFIX)
        suffix = tag[2:]
        if tag[:2] in {"sm", "st", "ov"} and suffix.isascii() and suffix.isdigit():
            return tag
        return None

    def observed_handle(
        instance: daemon._Daemon, conn: daemon._ClientConnection, message: dict[str, Any]
    ) -> None:
        nonlocal active_tag, control_conn, ambiguous_control
        method = message.get("method")
        if conn is control_conn and method == "state" and active_tag is not None:
            trial = trials[active_tag]
            # 试次串行；只在这条专用连接的首条 state 派发时观测，不做文件 I/O。
            if active_tag not in ambiguous and trial.dispatch_ns is None:
                trial.dispatch = _active_send(trial.conn)
                trial.dispatch_ns = (
                    trial.dispatch.observed_ns if trial.dispatch else time.perf_counter_ns()
                )
        handle(instance, conn, message)
        if method == "hello" and conn.hello:
            if conn.raw.client.get("client") == _STATE_CONTROL_LABEL:
                if control_conn is None:
                    control_conn = conn
                elif conn is not control_conn:
                    ambiguous_control = True
        if method == "subscribe" and conn.hello and conn.subscribed:
            tag = subscriber_tag(conn)
            if tag is not None:
                active_tag = tag
                if tag not in trials:
                    trials[tag] = _PressureObservation(conn)
                elif trials[tag].conn is not conn:
                    ambiguous.add(tag)
                    publish(tag, {"observation_error": "ambiguous_pressure_subscriber"})

    def observed_watchdog(instance: daemon._Daemon) -> None:
        watchdog(instance)
        if active_tag is None or active_tag in ambiguous:
            return
        trial = trials[active_tag]
        if trial.ready is None:
            sending = _active_send(trial.conn)
            if sending is not None and sending.age_ns >= _STATE_READY_SEND_AGE_NS:
                trial.ready = sending
                (out_dir / f"{active_tag}.ready").touch()

    def observed_record_drop(instance: daemon._Daemon, conn: daemon._ClientConnection) -> None:
        record_drop(instance, conn)
        tag = subscriber_tag(conn)
        if tag is None or tag in ambiguous:
            return
        trial = trials.get(tag)
        if trial is None or trial.conn is not conn:
            return
        if ambiguous_control:
            publish(tag, {"observation_error": "ambiguous_state_client"})
            return
        ready = trial.ready
        dispatch = trial.dispatch
        drop_ns = time.perf_counter_ns()
        publish(tag, {
            "ready_ns": ready.observed_ns if ready else None,
            "dispatch_ns": trial.dispatch_ns,
            "send_age_at_ready_ns": ready.age_ns if ready else None,
            "send_age_at_drop_ns": _send_age_ns(dispatch.sending_since) if dispatch else None,
            # 同一服务端连接、同一 send；不是另一客户端／先前一轮掉线计数。
            "same_live_send_at_dispatch": bool(
                ready and dispatch and ready.sending_since == dispatch.sending_since
                and ready.sent_frames == dispatch.sent_frames
            ),
            "sent_frames_at_dispatch": dispatch.sent_frames if dispatch else None,
            "drop_reason": conn.drop_reason,
            "drop_ns": drop_ns,
            "sent_frames_at_drop": conn.sent_frames,
        })

    with (
        patch.object(target, "_handle", observed_handle),
        patch.object(target, "_check_slow_subscribers", observed_watchdog),
        patch.object(target, "_record_drop", observed_record_drop),
    ):
        yield


# --------------------------------------------------------------------------- 子进程受控角色


class _SloSuite2Model:
    """Suite 2 专用确定性模型：按任务 goal 前缀驱动慢模型、慢工具与覆盖审批。"""

    def __init__(self, data_root: Path) -> None:
        self._ctl_dir = data_root / "ctl"
        self._ctl_dir.mkdir(parents=True, exist_ok=True)
        self._seen_turns: dict[str, int] = {}

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="slo-harness",
            model="slo-controllable",
            supports_tool_calling=True,
            supports_parallel_tool_calls=True,
            supports_streaming=True,
            declared_by="scripts/measure_slo.py",
        )

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="test", vendor=None, model="slo-controllable")

    @staticmethod
    def _user_goal(messages: list[dict[str, Any]]) -> str:
        for msg in messages:
            if msg.get("role") == "user":
                return str(msg.get("content") or "")
        return ""

    def stream_complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_delta: Any
    ) -> ModelResponse:
        on_delta(ModelStreamDelta(kind="text", text="streaming-slo-chunk-" + ("z" * 512)))
        return self.complete(messages, tools)

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]  # noqa: ARG002
    ) -> ModelResponse:
        goal = self._user_goal(messages)
        turn = self._seen_turns.get(goal, 0) + 1
        self._seen_turns[goal] = turn

        if goal.startswith("slow-model:"):
            tag = goal.split(":", 1)[1].strip()
            if turn == 1:
                entered = self._ctl_dir / f"{tag}.model_entered"
                release = self._ctl_dir / f"{tag}.model_release"
                _write_bytes_exact(entered, b"1")
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline and not release.exists():
                    time.sleep(0.005)
                return ModelResponse(
                    finish_reason="tool_use",
                    content="准备创建报告（若已取消则不应派发）",
                    tool_calls=[
                        ToolCall(
                            f"call_sm_{tag}",
                            "create_report",
                            {"name": f"post_cancel_{tag}.md", "content": "never\n"},
                        )
                    ],
                    usage={"input_tokens": 24, "output_tokens": 12},
                )
            return ModelResponse(
                finish_reason="end_turn",
                content="完成",
                usage={"input_tokens": 10, "output_tokens": 4},
            )

        if goal.startswith("slow-tool:"):
            # 格式：slow-tool:<tag>:<ws_path>
            parts = goal.split(":", 2)
            tag = parts[1]
            ws_str = parts[2]
            if turn == 1:
                entered = self._ctl_dir / f"{tag}.tool_entered"
                release = self._ctl_dir / f"{tag}.tool_release"
                return ModelResponse(
                    finish_reason="tool_use",
                    content="先跑慢命令，再写第二个文件",
                    tool_calls=[
                        ToolCall(
                            f"call_st1_{tag}",
                            "run_command",
                            {
                                "argv": [
                                    sys.executable,
                                    str(Path(__file__).resolve()),
                                    "--_child-role",
                                    "slow-tool",
                                    "--_entered-file",
                                    str(entered),
                                    "--_release-file",
                                    str(release),
                                ],
                                "cwd": ws_str,
                            },
                        ),
                        ToolCall(
                            f"call_st2_{tag}",
                            "create_report",
                            {
                                "name": f"post_cancel_tool_{tag}.md",
                                "content": "must_not_dispatch\n",
                            },
                        ),
                    ],
                    usage={"input_tokens": 32, "output_tokens": 16},
                )
            return ModelResponse(
                finish_reason="end_turn",
                content="完成",
                usage={"input_tokens": 10, "output_tokens": 4},
            )

        if goal.startswith("approve-overwrite:"):
            tag = goal.split(":", 1)[1].strip()
            if turn == 1:
                return ModelResponse(
                    finish_reason="tool_use",
                    content="覆盖已有文件以触发审批",
                    tool_calls=[
                        ToolCall(
                            f"call_ov_{tag}",
                            "modify_text",
                            {"path": "target.md", "content": f"approved-{tag}\n"},
                        )
                    ],
                    usage={"input_tokens": 20, "output_tokens": 10},
                )
            return ModelResponse(
                finish_reason="end_turn",
                content="已覆盖",
                usage={"input_tokens": 10, "output_tokens": 4},
            )

        return ModelResponse(
            finish_reason="end_turn",
            content="ok",
            usage={"input_tokens": 8, "output_tokens": 4},
        )


class _RecoveryDaemonModel:
    """Suite 3 恢复 Daemon 模型：记录工具提议与摘要调用计数，严禁盲目重试。"""

    def __init__(self, stats_path: Path, scenario: str) -> None:
        self._stats_path = stats_path
        self._scenario = scenario
        self._calls = 0
        self._summary_calls = 0
        self._tool_proposals = 0

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="slo-recovery",
            model="recovery-model",
            supports_tool_calling=True,
            supports_parallel_tool_calls=False,
            supports_streaming=False,
            declared_by="scripts/measure_slo.py",
        )

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="openai", vendor="openai", model="gpt-4o-mini")

    def _flush_stats(self) -> None:
        _write_bytes_exact(
            self._stats_path,
            json.dumps(
                {
                    "calls": self._calls,
                    "summary_calls": self._summary_calls,
                    "tool_proposals": self._tool_proposals,
                }
            ).encode("utf-8"),
        )

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        if not tools:
            self._summary_calls += 1
            self._flush_stats()
            return ModelResponse(
                finish_reason="end_turn",
                content=SUMMARY_CONSTRAINT[:SUMMARY_MAX_BYTES],
                usage={"input_tokens": 80, "output_tokens": 16},
            )
        self._calls += 1
        # 对 after_intent 场景：写前中断已被恢复标为 failed/none_applied，此时主循环回到下一轮模型，
        # 模型无需重做未知动作，直接确认恢复后的状态或按需要单次收尾；对 after_write 场景：
        # 恢复已补齐 applied + receipt，模型看到成功结果后直接 end_turn。
        has_tool_msg = any(m.get("role") == "tool" for m in messages)
        if self._scenario == "approved_tool_after_intent" and not has_tool_msg:
            self._tool_proposals += 1
        self._flush_stats()
        return ModelResponse(
            finish_reason="end_turn",
            content="恢复核对完毕",
            usage={"input_tokens": 24, "output_tokens": 8},
        )


class _SummaryCrashModel:
    """触发语义摘要并在 `summary-after-call` 注入点硬退的确定性模型。"""

    def __init__(self, rounds: int = SUMMARY_ROUNDS) -> None:
        self.rounds = rounds
        self.round_calls = 0

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="slo-summary",
            model="gpt-4o-mini",
            supports_tool_calling=True,
            declared_by="scripts/measure_slo.py",
        )

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="openai", vendor="openai", model="gpt-4o-mini")

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]  # noqa: ARG002
    ) -> ModelResponse:
        if not tools:
            return ModelResponse(
                finish_reason="end_turn",
                content=SUMMARY_CONSTRAINT[:SUMMARY_MAX_BYTES],
                usage={"input_tokens": 120, "output_tokens": 20},
            )
        self.round_calls += 1
        if self.round_calls <= self.rounds:
            path = "notes.md" if self.round_calls == 1 else "body.md"
            return ModelResponse(
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(f"sum_c{self.round_calls}", "read_text", {"path": path})
                ],
                usage={"input_tokens": 40, "output_tokens": 8},
            )
        return ModelResponse(
            finish_reason="end_turn",
            content="摘要轮完成",
            usage={"input_tokens": 30, "output_tokens": 6},
        )


def _run_child_role(args: argparse.Namespace) -> int:
    role = args._child_role
    if role == "slow-tool":
        entered = Path(args._entered_file)
        release = Path(args._release_file)
        _write_bytes_exact(entered, b"1")
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not release.exists():
            time.sleep(0.005)
        return 0

    if role == "slo-daemon":
        data_root = Path(args.data_root)
        pressure_dir = data_root / _STATE_PRESSURE_DIR
        observing_pressure = pressure_dir.is_dir()
        if observing_pressure:
            _require_state_pressure_test_isolation(data_root.parent / "locks")
        # 缩短慢订阅者超时阈值以便在基准窗口内确定性观测断连（背压与队列机制完全不变）
        daemon.SEND_STALL_S = SUITE2_SEND_STALL_S
        daemon.SUBSCRIBER_BACKLOG_STALL_S = SUITE2_SEND_STALL_S
        os.environ[metrics_mod.ENV_METRICS_INTERVAL] = "0.1"
        observe = (
            _observe_state_pressure(pressure_dir)
            if observing_pressure
            else contextlib.nullcontext()
        )
        with observe:
            daemon.serve(
                data_root,
                model=_SloSuite2Model(data_root),
                driver_interval_s=SUITE2_DRIVER_INTERVAL_S,
            )
        return 0

    if role == "slo-host":
        data_root = Path(args.data_root)
        if (data_root / _STATE_PRESSURE_DIR).is_dir():
            _require_state_pressure_test_isolation(data_root.parent / "locks")
        ui_dir = Path(args._ui_dir)
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_child-role",
            "slo-daemon",
            "--data-root",
            str(data_root),
        ]
        return host.run_host(
            data_root,
            ui_dir=ui_dir,
            daemon_command=cmd,
            port=0,
            open_browser=False,
            supervise=True,
        )

    if role == "crash-setup":
        data_root = Path(args.data_root)
        ws = Path(args._ws_dir)
        scenario = args._scenario
        ws.mkdir(parents=True, exist_ok=True)
        log = EventLog(
            data_root / "events.db",
            blob_dir=default_material_paths(data_root / "events.db", data_root)[1],
        )
        stack = build_stack(log, data_root=data_root)
        if scenario in (
            "approved_tool_after_intent",
            "approved_tool_after_write",
            "unknown_effect_reconciling_blocked",
        ):
            _write_bytes_exact(ws / "note.md", b"initial-content\n")
            model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "crash_call_1",
                                "modify_text",
                                {"path": "note.md", "content": "updated-after-approve\n"},
                            )
                        ],
                        usage={"input_tokens": 20, "output_tokens": 10},
                    ),
                    ModelResponse(
                        "end_turn",
                        content="完成",
                        usage={"input_tokens": 12, "output_tokens": 6},
                    ),
                ]
            )
            limits = Limits(max_turns=4, max_total_tokens=10_000)
            engine = AgentLoop(
                tasks=stack.tasks,
                leases=stack.leases,
                claims=stack.claims,
                dispatcher=stack.dispatcher,
                approvals=stack.approvals,
                snapshots=stack.snapshots,
                contents=stack.contents,
                model=model,
                limits=limits,
                memory=stack.memory,
                summaries=stack.summaries,
                budget=stack.budget,
            )
            task = stack.tasks.create_task(
                f"crash-{scenario}",
                [{"root_path": str(ws), "mode": "writable"}],
            )
            first = engine.start(task.task_id)
            assert first.state == "waiting_approval", f"预期等待审批，实际 {first.state}"
            pending = stack.approvals.pending(run_id=first.run_id)
            assert len(pending) == 1
            stack.approvals.decide(
                pending[0].approval_id,
                decision="approved",
                actor=HumanIdentity.local_cli(),
            )
            _write_bytes_exact(
                data_root / "crash_meta.json",
                json.dumps({"run_id": first.run_id, "task_id": task.task_id}).encode("utf-8"),
            )
            fp = (
                "after_intent"
                if scenario == "approved_tool_after_intent"
                else "after_write"
            )
            os.environ["TIANSHU_FAILPOINT"] = fp
            # engine.start() 已持有该进程内的作业 claim，直接 resume_run 即可在 failpoint 处硬退
            engine.resume_run(first.run_id)
            return 99  # 不应到达：failpoint 会 os._exit(77)

        if scenario == "unsettled_model_and_budget":
            _write_bytes_exact(
                ws / "notes.md",
                (SUMMARY_CONSTRAINT + "\n" + ("笔记正文。" * 120) + "\n").encode("utf-8"),
            )
            _write_bytes_exact(
                ws / "body.md",
                (("后续正文，不含硬约束。" * 60) + "\n").encode("utf-8"),
            )
            limits = Limits(
                max_turns=SUMMARY_ROUNDS + 3,
                max_total_tokens=50_000,
                context_budget_bytes=SUMMARY_BUDGET_BYTES,
                summary_mode="model",
            )
            engine = AgentLoop(
                tasks=stack.tasks,
                leases=stack.leases,
                claims=stack.claims,
                dispatcher=stack.dispatcher,
                approvals=stack.approvals,
                snapshots=stack.snapshots,
                contents=stack.contents,
                model=_SummaryCrashModel(rounds=SUMMARY_ROUNDS),
                limits=limits,
                memory=stack.memory,
                summaries=stack.summaries,
                budget=stack.budget,
            )
            task = stack.tasks.create_task(
                "crash-summary-budget",
                [{"root_path": str(ws), "mode": "writable"}],
            )
            run = stack.tasks.start_run(
                task.task_id,
                payload={
                    "task": task.goal,
                    "out": "",
                    "limits": {
                        "max_turns": limits.max_turns,
                        "max_total_tokens": limits.max_total_tokens,
                    },
                    "tools": ["read_text", "list_files"],
                },
            )
            stack.claims.enqueue(run.run_id)
            _write_bytes_exact(
                data_root / "crash_meta.json",
                json.dumps({"run_id": run.run_id, "task_id": task.task_id}).encode("utf-8"),
            )
            os.environ["TIANSHU_FAILPOINT"] = "summary-after-call"
            engine.resume_run(run.run_id)
            return 99

        raise ValueError(f"未知崩溃场景：{scenario}")

    if role == "recovery-daemon":
        data_root = Path(args.data_root)
        scenario = args._scenario
        stats_path = data_root / "recovery_model_stats.json"
        limits = (
            Limits(
                max_turns=SUMMARY_ROUNDS + 3,
                max_total_tokens=50_000,
                context_budget_bytes=SUMMARY_BUDGET_BYTES,
                summary_mode="model",
            )
            if scenario == "unsettled_model_and_budget"
            else Limits(max_turns=6, max_total_tokens=20_000)
        )
        os.environ[metrics_mod.ENV_METRICS_INTERVAL] = "0.1"
        daemon.serve(
            data_root,
            model=_RecoveryDaemonModel(stats_path, scenario),
            limits=limits,
            driver_interval_s=0.01,
        )
        return 0

    raise ValueError(f"未知子进程角色：{role}")


# --------------------------------------------------------------------------- Suite 1: 启动可用性


def _seed_warm_template(
    seed_root: Path,
    *,
    warm_tasks: int,
    warm_events_min: int,
) -> dict[str, int]:
    ws = seed_root / "seed_ws"
    ws.mkdir(parents=True, exist_ok=True)
    _write_bytes_exact(ws / "readme.md", b"# Seed Workspace\n")
    log = EventLog(
        seed_root / "events.db",
        blob_dir=default_material_paths(seed_root / "events.db", seed_root)[1],
    )
    stack = build_stack(log, data_root=seed_root)
    try:
        for idx in range(warm_tasks):
            script = [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("c_read", "read_text", {"path": "readme.md"}),
                        ToolCall(
                            "c_rep",
                            "create_report",
                            {
                                "name": f"report_{idx:03d}.md",
                                "content": f"# Report {idx}\nSeeded warm history.\n",
                            },
                        ),
                    ],
                    usage={"input_tokens": 30, "output_tokens": 15},
                ),
                ModelResponse(
                    "end_turn",
                    content=f"已生成报告 {idx}",
                    usage={"input_tokens": 20, "output_tokens": 8},
                ),
            ]
            engine = AgentLoop(
                tasks=stack.tasks,
                leases=stack.leases,
                claims=stack.claims,
                dispatcher=stack.dispatcher,
                approvals=stack.approvals,
                snapshots=stack.snapshots,
                contents=stack.contents,
                model=ScriptedModel(script),
                limits=Limits(max_turns=4, max_total_tokens=10_000),
                memory=stack.memory,
                summaries=stack.summaries,
                budget=stack.budget,
            )
            task = stack.tasks.create_task(
                f"历史任务 #{idx:03d}",
                [{"root_path": str(ws), "mode": "writable"}],
            )
            res = engine.start(task.task_id, out_name=f"report_{idx:03d}.md")
            assert res.outcome == "succeeded", f"播种任务 #{idx} 失败：{res.error}"

        seq_head = log.seq_head()
        while seq_head < warm_events_min:
            extra_task = stack.tasks.create_task(f"补充事件任务 #{seq_head}", [])
            assert extra_task.task_id
            seq_head = log.seq_head()

        log.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        log.close()

    return {
        "warm_tasks": warm_tasks,
        "warm_runs": warm_tasks,
        "warm_events": seq_head,
        "warm_db_bytes": _dir_bytes(seed_root / "events.db"),
        "warm_artifacts_bytes": _dir_bytes(seed_root / "artifacts"),
    }


def _measure_one_host_startup(
    data_root: Path,
    *,
    ui_dir: Path,
    authority_root: Path,
    lock_root: Path,
    expect_warm_events: int,
    tracked_pids: TrackedPids,
) -> tuple[float, float | None]:
    env = _isolated_env(
        data_root=data_root,
        authority_root=authority_root,
        lock_root=lock_root,
    )
    t0 = time.perf_counter()
    startup_deadline = time.monotonic() + 30.0
    proc, stderr_log = _launch_host(
        [
            sys.executable,
            "-m",
            "tianshu",
            "host",
            "run",
            "--data-root",
            str(data_root),
            "--port",
            "0",
            "--ui-dir",
            str(ui_dir),
            "--model",
            "scripted",
        ],
        env=env,
    )
    _record_pid(proc.pid, tracked_pids)
    try:
        remaining = startup_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Host 在 30s 内未进入就绪状态")
        info = _wait_host_ready(
            proc, data_root, stderr_log, timeout=remaining,
        )
        if time.monotonic() >= startup_deadline:
            raise RuntimeError("Host 在 30s 内未进入就绪状态")
        while True:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Host 在 30s 内未进入就绪状态")
            try:
                st, state_resp = _http_request(
                    info.port, "GET", "/state", info.token, timeout=min(2.0, remaining),
                )
                if st == 200 and state_resp.get("ok") is True:
                    if expect_warm_events > 0:
                        assert (
                            int(state_resp["result"]["event_seq_head"])
                            >= expect_warm_events
                        )
                    break
            except (OSError, ValueError):
                pass
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Host 启动进程提前退出 rc={proc.returncode}: {_stderr_tail(stderr_log)}"
                )
            if time.monotonic() >= startup_deadline:
                raise RuntimeError("Host 在 30s 内未进入就绪状态")
            time.sleep(0.005)

        st_create, create_resp = _http_request(
            info.port,
            "POST",
            "/rpc",
            info.token,
            {"method": "task.create", "params": {"goal": "startup-availability-probe"}},
            timeout=5.0,
        )
        if st_create != 200 or not create_resp.get("ok"):
            raise RuntimeError(f"启动探针 task.create 失败：{st_create} {create_resp}")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _record_lock_pids(data_root, tracked_pids, phase="startup_ready")

        records = metrics_mod.read_metrics(data_root)
        ready_vals = metrics_mod.metric_values(records, metrics_mod.STARTUP_READY_MS)
        internal_ms = ready_vals[-1] if ready_vals else None
        return elapsed_ms, internal_ms
    finally:
        _record_lock_pids(data_root, tracked_pids, phase="startup_before_stop")
        with contextlib.redirect_stdout(io.StringIO()):
            host.stop_host(data_root, timeout=15.0)
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        try:
            _capture_host_stop(proc, tracked_pids, stderr_log, phase="startup_after_wait")
        finally:
            stderr_log.close()


def run_suite1_startup(
    base_dir: Path,
    *,
    repeats: int,
    warm_tasks: int,
    warm_events_min: int,
    authority_root: Path,
    lock_root: Path,
    tracked_pids: TrackedPids,
) -> tuple[list[float], list[float], dict[str, Any]]:
    ui_dir = REAL_UI_DIST if (REAL_UI_DIST / "index.html").is_file() else base_dir / "empty_ui"
    ui_dir.mkdir(parents=True, exist_ok=True)

    # 先执行一次不计时的独立空目录 Host+Daemon 预热并销毁，消除首次进程创建时
    # OS 文件缓存与 Windows Defender 首次扫描毛刺（后续每个 c{idx} 仍为全新空目录与新进程）
    warmup_root = base_dir / "warmup_cold"
    warmup_root.mkdir(parents=True, exist_ok=True)
    _measure_one_host_startup(
        warmup_root,
        ui_dir=ui_dir,
        authority_root=authority_root,
        lock_root=lock_root,
        expect_warm_events=0,
        tracked_pids=tracked_pids,
    )
    shutil.rmtree(warmup_root, ignore_errors=True)

    cold_samples: list[float] = []
    cold_internal: list[float] = []
    for idx in range(repeats):
        c_root = base_dir / f"c{idx}"
        c_root.mkdir(parents=True, exist_ok=True)
        ms, internal = _measure_one_host_startup(
            c_root,
            ui_dir=ui_dir,
            authority_root=authority_root,
            lock_root=lock_root,
            expect_warm_events=0,
            tracked_pids=tracked_pids,
        )
        cold_samples.append(ms)
        if internal is not None:
            cold_internal.append(internal)
        shutil.rmtree(c_root, ignore_errors=True)

    seed_root = base_dir / "seed"
    seed_root.mkdir(parents=True, exist_ok=True)
    seed_stats = _seed_warm_template(
        seed_root,
        warm_tasks=warm_tasks,
        warm_events_min=warm_events_min,
    )

    warm_samples: list[float] = []
    warm_internal: list[float] = []
    for idx in range(repeats):
        w_root = base_dir / f"w{idx}"
        w_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(seed_root / "events.db", w_root / "events.db")
        if (seed_root / "artifacts").exists():
            shutil.copytree(seed_root / "artifacts", w_root / "artifacts")
        ms, internal = _measure_one_host_startup(
            w_root,
            ui_dir=ui_dir,
            authority_root=authority_root,
            lock_root=lock_root,
            expect_warm_events=seed_stats["warm_events"],
            tracked_pids=tracked_pids,
        )
        warm_samples.append(ms)
        if internal is not None:
            warm_internal.append(internal)
        shutil.rmtree(w_root, ignore_errors=True)

    shutil.rmtree(seed_root, ignore_errors=True)
    cold_p50 = metrics_mod.percentile(cold_internal, 50)
    warm_p50 = metrics_mod.percentile(warm_internal, 50)
    dataset = {
        "cold_tasks": 0,
        "cold_events": 0,
        **seed_stats,
        "daemon_internal_cold_p50_ms": None if cold_p50 is None else round(cold_p50, 3),
        "daemon_internal_warm_p50_ms": None if warm_p50 is None else round(warm_p50, 3),
    }
    return cold_samples, warm_samples, dataset


# ---------------------------------------------------------------- Suite 2: 慢订阅者取消与审批


def _open_non_reading_sse(port: int, token: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    req = (
        f"GET /events?since_seq=0 HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"X-Tianshu-Token: {token}\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    sock.sendall(req)
    buf = b""
    sock.settimeout(5.0)
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    # 故意不再读取任何后续 SSE 帧，制造真实对端不读背压
    return sock


def _wait_condition(predicate: Any, *, timeout: float = 15.0, interval: float = 0.005) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    raise TimeoutError("等待条件超时")


def _measure_state_rpc(
    control: ipc.Client, pressure_dir: Path, tag: str
) -> tuple[int, int | None]:
    # 等真实发送信号，不靠固定 sleep 猜背压。即便未就绪/已断开，也留一条可判无效的试次。
    try:
        _wait_condition(
            lambda: (pressure_dir / f"{tag}.ready").is_file()
            or (pressure_dir / f"{tag}.json").is_file(),
            timeout=_STATE_OBSERVATION_WAIT_S,
        )
    except TimeoutError:
        pass
    # 与 Daemon 观测共享高分辨率时钟；Windows 3.12 的 monotonic 可能合并相邻事件。
    started_ns = time.perf_counter_ns()
    result = control.call("state", {})
    ended_ns = time.perf_counter_ns()
    return started_ns, ended_ns if type(result.get("event_seq_head")) is int else None


def _read_state_observation(pressure_dir: Path, tag: str) -> dict[str, Any] | None:
    try:
        value = json.loads((pressure_dir / f"{tag}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        return {"trial": tag, "observation_error": "invalid_pressure_observation"}
    return value


def _state_trial(
    tag: str,
    started_ns: int | None,
    ended_ns: int | None,
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    evidence = observation or {}
    dispatch_ns = evidence.get("dispatch_ns")
    ready_ns = evidence.get("ready_ns")
    ready_age_ns = evidence.get("send_age_at_ready_ns")
    drop_age_ns = evidence.get("send_age_at_drop_ns")
    drop_ns = evidence.get("drop_ns")
    reason = evidence.get("observation_error")
    same_send = evidence.get("same_live_send_at_dispatch") is True
    frames = evidence.get("sent_frames_at_dispatch")
    drop_frames = evidence.get("sent_frames_at_drop")
    drop_reason = evidence.get("drop_reason")

    if started_ns is None or ended_ns is None or ended_ns <= started_ns:
        reason = "missing_rpc_timing"
    elif observation is None:
        reason = "missing_pressure_observation"
    elif evidence.get("trial") != tag:
        reason = "wrong_pressure_trial"
    elif reason in (
        "ambiguous_pressure_subscriber", "ambiguous_state_client", "invalid_pressure_observation"
    ):
        pass
    elif type(drop_ns) is int and drop_ns <= started_ns:
        reason = "no_live_pressure_at_dispatch"
    elif type(dispatch_ns) is not int:
        reason = "missing_state_dispatch"
    elif not started_ns <= dispatch_ns <= ended_ns:
        reason = "dispatch_outside_rpc"
    elif (
        not same_send
        or type(ready_ns) is not int
        or type(ready_age_ns) is not int
        or ready_age_ns < _STATE_READY_SEND_AGE_NS
        or not ready_ns <= started_ns < dispatch_ns
        or (type(drop_ns) is int and drop_ns <= dispatch_ns)
    ):
        reason = "no_live_pressure_at_dispatch"
    elif (
        type(drop_ns) is not int
        or drop_reason != "send_stall"
        or type(drop_age_ns) is not int
        or drop_age_ns <= SUITE2_SEND_STALL_S * 1_000_000_000
        or type(frames) is not int
        or type(drop_frames) is not int
        or frames < 2
        or frames != drop_frames
    ):
        reason = "no_same_sender_watchdog_drop"
    else:
        reason = None

    def duration_ms(value: Any) -> float | None:
        return round(value / 1_000_000, 6) if type(value) is int else None

    def offset(value: Any) -> float | None:
        if type(value) is not int or started_ns is None:
            return None
        return duration_ms(value - started_ns)

    return {
        "trial": tag,
        "latency_ms": None if reason == "missing_rpc_timing" else offset(ended_ns),
        "dispatch_offset_ms": offset(dispatch_ns),
        "invalid_reason": reason,
        "pressure": {
            "same_live_send_at_dispatch": same_send,
            "ready_offset_ms": offset(ready_ns),
            "drop_offset_ms": offset(drop_ns),
            "send_age_at_ready_ms": duration_ms(ready_age_ns),
            "send_age_at_drop_ms": duration_ms(drop_age_ns),
            "sent_frames_at_dispatch": frames,
            "sent_frames_at_drop": drop_frames,
            "drop_reason": drop_reason,
        },
    }


def _state_sample_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    qualified = [trial["latency_ms"] for trial in trials if trial["invalid_reason"] is None]
    invalid = Counter(trial["invalid_reason"] for trial in trials if trial["invalid_reason"])
    return {
        "attempted_count": len(trials),
        "qualified_count": len(qualified),
        "invalid_count": sum(invalid.values()),
        "invalid_reasons": dict(invalid),
        "p50_ms": metrics_mod.percentile(qualified, 50),
        "p95_ms": metrics_mod.percentile(qualified, 95),
        "max_ms": max(qualified, default=None),
    }


def run_suite2_cancel_and_approval(
    base_dir: Path,
    *,
    repeats: int,
    event_bytes_min: int,
    authority_root: Path,
    lock_root: Path,
    tracked_pids: TrackedPids,
    state_trials: list[dict[str, Any]] | None = None,
) -> tuple[list[float], list[float], list[float], dict[str, Any], dict[str, Any]]:
    s2_root = base_dir / "s2"
    s2_root.mkdir(parents=True, exist_ok=True)
    pressure_dir = s2_root / _STATE_PRESSURE_DIR
    state_timings: dict[str, tuple[int, int | None]] = {}
    if state_trials is not None:
        _require_state_pressure_test_isolation()
        pressure_dir.mkdir()
    ui_dir = REAL_UI_DIST if (REAL_UI_DIST / "index.html").is_file() else base_dir / "empty_ui"
    ui_dir.mkdir(parents=True, exist_ok=True)

    env = _isolated_env(
        data_root=s2_root,
        authority_root=authority_root,
        lock_root=lock_root,
    )
    proc, stderr_log = _launch_host(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_child-role",
            "slo-host",
            "--data-root",
            str(s2_root),
            "--_ui-dir",
            str(ui_dir),
        ],
        env=env,
    )
    _record_pid(proc.pid, tracked_pids)

    slow_ipc_clients: list[ipc.Client] = []
    slow_sse_socks: list[socket.socket] = []
    ctrl_ipc: ipc.Client | None = None

    cancel_ack_samples: list[float] = []
    cancel_stop_samples: list[float] = []
    approval_rpc_samples: list[float] = []
    post_release_halts: list[float] = []
    ack_before_release_all = True
    in_flight_applied = 0
    in_flight_receipts = 0
    post_cancel_dispatched = 0

    try:
        info = _wait_host_ready(proc, s2_root, stderr_log, timeout=25.0)
        _record_lock_pids(s2_root, tracked_pids, phase="suite2_ready")

        ctrl_ipc = (
            _LabelledSuite2Client(s2_root, timeout=10.0, label=_STATE_CONTROL_LABEL)
            if state_trials is not None
            else ipc.Client(s2_root, timeout=10.0)
        )
        ctrl_ipc.connect()

        # 1) 挂载真实不读数据的 IPC 订阅者 + SSE 订阅者，并塞满内核发送缓冲触发慢订阅者断连
        initial_slow_ipc = ipc.Client(s2_root, timeout=5.0)
        initial_slow_ipc.connect()
        initial_slow_ipc.subscribe(0, include_model_stream=True)
        slow_ipc_clients.append(initial_slow_ipc)

        sse_sock = _open_non_reading_sse(info.port, info.token)
        slow_sse_socks.append(sse_sock)

        big_goal = "B" * max(BIG_EVENT_CHARS, int(event_bytes_min))
        for b_idx in range(5):
            ctrl_ipc.call("task.create", {"goal": f"{big_goal}#{b_idx}"}, timeout=10.0)

        _wait_condition(
            lambda: [
                r
                for r in metrics_mod.read_metrics(s2_root)
                if r.get("metric") == metrics_mod.SLOW_SUBSCRIBER_DROPPED
            ],
            timeout=10.0,
            interval=0.02,
        )
        cursor_before_workload = int(ctrl_ipc.call("state", {})["event_seq_head"])

        def _ensure_live_backpressure(tag: str) -> None:
            sub = (
                _LabelledSuite2Client(s2_root, timeout=5.0, label=_STATE_SUBSCRIBER_PREFIX + tag)
                if state_trials is not None
                else ipc.Client(s2_root, timeout=5.0)
            )
            sub.connect()
            sub.subscribe(0, include_model_stream=True)
            slow_ipc_clients.append(sub)
            if state_trials is not None:
                # subscribe(0) already replays the seeded large history. Measure
                # that actual send before another large write consumes its window.
                state_timings[tag] = _measure_state_rpc(ctrl_ipc, pressure_dir, tag)
            else:
                ctrl_ipc.call(
                    "task.create",
                    {"goal": ("P" * BIG_EVENT_CHARS) + f":{tag}"},
                    timeout=10.0,
                )

        slow_model_count = max(1, (repeats + 1) // 2)
        slow_tool_count = max(1, repeats - slow_model_count)
        # 确保总样本 >= repeats（当 repeats=1 时各跑 1 条即 2 >= 1）
        if slow_model_count + slow_tool_count < repeats:
            slow_tool_count = repeats - slow_model_count

        # 2A) 慢模型 + 慢订阅者背压下取消
        for idx in range(slow_model_count):
            tag = f"sm{idx}"
            _ensure_live_backpressure(tag)
            ws = s2_root / f"ws_{tag}"
            ws.mkdir(parents=True, exist_ok=True)
            _, created = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {
                    "method": "task.create",
                    "params": {
                        "goal": f"slow-model:{tag}",
                        "workspaces": [{"root_path": str(ws), "mode": "writable"}],
                    },
                },
            )
            task_id = created["result"]["task"]["task_id"]
            _, started = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {"method": "run.start", "params": {"task_id": task_id}},
            )
            run_id = started["result"]["run"]["run_id"]

            entered_path = s2_root / "ctl" / f"{tag}.model_entered"
            release_path = s2_root / "ctl" / f"{tag}.model_release"
            _wait_condition(entered_path.exists, timeout=15.0)

            t_req = time.perf_counter()
            st_cancel, cancel_resp = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {"method": "run.cancel", "params": {"run_id": run_id}},
            )
            t_ack = time.perf_counter()
            assert st_cancel == 200 and cancel_resp.get("ok"), cancel_resp
            if release_path.exists():
                ack_before_release_all = False
            cancel_ack_samples.append((t_ack - t_req) * 1000.0)

            remaining_hold = (IN_FLIGHT_HOLD_MS / 1000.0) - (t_ack - t_req)
            if remaining_hold > 0:
                time.sleep(remaining_hold)
            t_release = time.perf_counter()
            _write_bytes_exact(release_path, b"1")

            _wait_condition(
                lambda _rid=run_id: (
                    ctrl_ipc.call("run.status", {"run_id": _rid})["run"]["state"]
                    == "cancelled"
                ),
                timeout=15.0,
                interval=0.004,
            )
            t_stopped = time.perf_counter()
            cancel_stop_samples.append((t_stopped - t_req) * 1000.0)
            post_release_halts.append((t_stopped - t_release) * 1000.0)

            run_events = ctrl_ipc.call("events.run", {"run_id": run_id})["events"]
            intents = [e for e in run_events if e["type"] == "tool.intent"]
            if intents or (ws / f"post_cancel_{tag}.md").exists():
                post_cancel_dispatched += len(intents) + 1

        # 2B) 慢工具（已在途效果单独列账）+ 审批 + 慢订阅者背压下取消
        for idx in range(slow_tool_count):
            tag = f"st{idx}"
            _ensure_live_backpressure(tag)
            ws = (s2_root / f"ws_{tag}").resolve()
            ws.mkdir(parents=True, exist_ok=True)
            _, created = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {
                    "method": "task.create",
                    "params": {
                        "goal": f"slow-tool:{tag}:{ws}",
                        "workspaces": [{"root_path": str(ws), "mode": "writable"}],
                        # Supervised Python roles run natively on Windows; the
                        # allowlist must name that exact controlled interpreter.
                        "allow_exec_cmd": [sys._base_executable],
                    },
                },
            )
            task_id = created["result"]["task"]["task_id"]
            _, started = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {"method": "run.start", "params": {"task_id": task_id}},
            )
            run_id = started["result"]["run"]["run_id"]

            approvals = _wait_condition(
                lambda _rid=run_id: ctrl_ipc.call(
                    "approvals.list", {"run_id": _rid}
                )["approvals"],
                timeout=15.0,
                interval=0.005,
            )
            approval_id = approvals[0]["approval_id"]

            t_app0 = time.perf_counter()
            st_app, app_resp = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {
                    "method": "approvals.decide",
                    "params": {"approval_id": approval_id, "decision": "approve"},
                },
            )
            t_app1 = time.perf_counter()
            assert st_app == 200 and app_resp.get("ok"), app_resp
            approval_rpc_samples.append((t_app1 - t_app0) * 1000.0)

            entered_path = s2_root / "ctl" / f"{tag}.tool_entered"
            release_path = s2_root / "ctl" / f"{tag}.tool_release"
            _wait_condition(entered_path.exists, timeout=15.0)

            t_req = time.perf_counter()
            st_cancel, cancel_resp = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {"method": "run.cancel", "params": {"run_id": run_id}},
            )
            t_ack = time.perf_counter()
            assert st_cancel == 200 and cancel_resp.get("ok"), cancel_resp
            if release_path.exists():
                ack_before_release_all = False
            cancel_ack_samples.append((t_ack - t_req) * 1000.0)

            remaining_hold = (IN_FLIGHT_HOLD_MS / 1000.0) - (t_ack - t_req)
            if remaining_hold > 0:
                time.sleep(remaining_hold)
            t_release = time.perf_counter()
            _write_bytes_exact(release_path, b"1")

            _wait_condition(
                lambda _rid=run_id: (
                    ctrl_ipc.call("run.status", {"run_id": _rid})["run"]["state"]
                    == "cancelled"
                ),
                timeout=15.0,
                interval=0.004,
            )
            t_stopped = time.perf_counter()
            cancel_stop_samples.append((t_stopped - t_req) * 1000.0)
            post_release_halts.append((t_stopped - t_release) * 1000.0)

            run_events = ctrl_ipc.call("events.run", {"run_id": run_id})["events"]
            intents = [e for e in run_events if e["type"] == "tool.intent"]
            results = [e for e in run_events if e["type"] == "tool.result"]
            receipts = [e for e in run_events if e["type"] == "receipt"]
            # 第 1 个工具（run_command）在取消前已在途执行：必须单独列账且具备回执
            if (
                len(results) == 1
                and results[0]["payload"].get("effect_state") == "applied"
            ):
                in_flight_applied += 1
            if len(receipts) == 1:
                in_flight_receipts += 1
            # 第 2 个工具（create_report）必须在安全点被阻断：0 次新派发
            second_intents = [
                e for e in intents if e["payload"].get("tool_id") == "create_report"
            ]
            if second_intents or (ws / f"post_cancel_tool_{tag}.md").exists():
                post_cancel_dispatched += len(second_intents) + 1

        # 2C) 补足 approval_rpc_ms 样本至 >= repeats（覆盖文件写审批场景）
        extra_approvals_needed = max(0, repeats - len(approval_rpc_samples))
        for idx in range(extra_approvals_needed):
            tag = f"ov{idx}"
            _ensure_live_backpressure(tag)
            ws = (s2_root / f"ws_{tag}").resolve()
            ws.mkdir(parents=True, exist_ok=True)
            _write_bytes_exact(ws / "target.md", b"before\n")
            _, created = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {
                    "method": "task.create",
                    "params": {
                        "goal": f"approve-overwrite:{tag}",
                        "workspaces": [{"root_path": str(ws), "mode": "writable"}],
                    },
                },
            )
            task_id = created["result"]["task"]["task_id"]
            _, started = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {"method": "run.start", "params": {"task_id": task_id}},
            )
            run_id = started["result"]["run"]["run_id"]
            approvals = _wait_condition(
                lambda _rid=run_id: ctrl_ipc.call(
                    "approvals.list", {"run_id": _rid}
                )["approvals"],
                timeout=15.0,
                interval=0.005,
            )
            t_app0 = time.perf_counter()
            st_app, app_resp = _http_request(
                info.port,
                "POST",
                "/rpc",
                info.token,
                {
                    "method": "approvals.decide",
                    "params": {
                        "approval_id": approvals[0]["approval_id"],
                        "decision": "approve",
                    },
                },
            )
            t_app1 = time.perf_counter()
            assert st_app == 200 and app_resp.get("ok"), app_resp
            approval_rpc_samples.append((t_app1 - t_app0) * 1000.0)
            _wait_condition(
                lambda _rid=run_id: (
                    ctrl_ipc.call("run.status", {"run_id": _rid})["run"]["state"]
                    == "succeeded"
                ),
                timeout=15.0,
                interval=0.005,
            )

        # 3) 核验落后订阅者断连后按游标 `since_seq` 补读完整连续、零丢事件
        resumed = ipc.Client(s2_root, timeout=10.0)
        resumed.connect()
        try:
            head = int(ctrl_ipc.call("state", {})["event_seq_head"])
            since = cursor_before_workload + 1
            assert head >= since
            resumed.subscribe(since)
            collected: list[int] = []
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and (
                not collected or collected[-1] < head
            ):
                msg = resumed.next_event(timeout=0.2)
                if msg is not None and "event" in msg:
                    collected.append(int(msg["event"]["seq"]))
            expected_seqs = list(range(since, head + 1))
            cursor_verified = collected == expected_seqs
        finally:
            resumed.close()

        drops = [
            r
            for r in metrics_mod.read_metrics(s2_root, include_rotated=True)
            if r.get("metric") == metrics_mod.SLOW_SUBSCRIBER_DROPPED
        ]
        slow_sub_summary = {
            "ipc_non_reading_attached": True,
            "sse_non_reading_attached": True,
            "dropped_count": len(drops),
            "cursor_reconnect_verified": bool(cursor_verified),
            "reconnected_events_count": len(collected),
        }
        halt_p50 = metrics_mod.percentile(post_release_halts, 50)
        halt_p95 = metrics_mod.percentile(post_release_halts, 95)
        inflight_summary = {
            "slow_model_runs": slow_model_count,
            "slow_tool_runs": slow_tool_count,
            "in_flight_hold_target_ms": IN_FLIGHT_HOLD_MS,
            "in_flight_applied_effects": in_flight_applied,
            "in_flight_receipts_verified": in_flight_receipts,
            "post_cancel_dispatched_count": post_cancel_dispatched,
            "ack_before_inflight_release_all": ack_before_release_all,
            "post_release_halt_p50_ms": None if halt_p50 is None else round(halt_p50, 3),
            "post_release_halt_p95_ms": None if halt_p95 is None else round(halt_p95, 3),
        }
        if state_trials is not None:
            # 原有场景先照常跑；在计时窗口之外等这批同源断连证据。
            try:
                _wait_condition(
                    lambda: all((pressure_dir / f"{tag}.json").is_file() for tag in state_timings),
                    timeout=_STATE_OBSERVATION_WAIT_S,
                )
            except TimeoutError:
                pass
            state_trials.extend(
                _state_trial(tag, *timing, _read_state_observation(pressure_dir, tag))
                for tag, timing in state_timings.items()
            )
        return (
            cancel_ack_samples,
            cancel_stop_samples,
            approval_rpc_samples,
            slow_sub_summary,
            inflight_summary,
        )
    finally:
        if ctrl_ipc is not None:
            ctrl_ipc.close()
        for c in slow_ipc_clients:
            c.close()
        for s in slow_sse_socks:
            try:
                s.close()
            except OSError:
                pass
        _record_lock_pids(s2_root, tracked_pids, phase="suite2_before_stop")
        with contextlib.redirect_stdout(io.StringIO()):
            host.stop_host(s2_root, timeout=15.0)
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        try:
            _capture_host_stop(proc, tracked_pids, stderr_log, phase="suite2_after_wait")
        finally:
            stderr_log.close()
        shutil.rmtree(s2_root, ignore_errors=True)


def run_state_pressure_diagnostic(*, repeats: int = 2) -> tuple[dict[str, Any], int]:
    """隔离 pytest 专用：测量有同源背压证据的 state，不执行、不改变发布门禁。"""
    _require_state_pressure_test_isolation()
    if repeats < 1:
        raise SloConfigError("repeats 必须 >= 1")

    temp_base = _short_temp_dir("tsst-")
    authority_root = temp_base / "auth"
    lock_root = temp_base / "locks"
    tracked_pids = TrackedPids()
    trials: list[dict[str, Any]] = []
    try:
        authority_root.mkdir()
        lock_root.mkdir()
        _, _, _, slow_subscriber, _ = run_suite2_cancel_and_approval(
            temp_base,
            repeats=repeats,
            event_bytes_min=BIG_EVENT_CHARS,
            authority_root=authority_root,
            lock_root=lock_root,
            tracked_pids=tracked_pids,
            state_trials=trials,
        )
        orphans = _find_orphan_pids(tracked_pids, temp_base)
    finally:
        shutil.rmtree(temp_base, ignore_errors=True)

    samples = _state_sample_summary(trials)
    report = {
        "schema_version": "slo-state-pressure-dev-v1",
        "scope": "diagnostic_pre_threshold",
        "measured_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": collect_hardware_metadata(),
        "workload": {
            "suite": "suite2",
            "transport": "authenticated_daemon_ipc",
            "method": "state",
            "suite2_repeats": repeats,
            "seed_goal_chars": BIG_EVENT_CHARS,
            "fixture_send_stall_ms": SUITE2_SEND_STALL_S * 1000,
            "fixture_driver_interval_ms": SUITE2_DRIVER_INTERVAL_S * 1000,
            "ready_send_age_ms": _STATE_READY_SEND_AGE_NS / 1_000_000,
        },
        "slow_subscriber": slow_subscriber,
        "state_rpc_ms": samples,
        "trials": trials,
        "orphan_process_count": len(orphans),
    }
    usable = (
        samples["qualified_count"] > 0
        and samples["invalid_count"] == 0
        and not orphans
        and slow_subscriber["cursor_reconnect_verified"]
    )
    return _redact_obj(report, extra_paths=[str(temp_base), str(REPO_ROOT), str(Path.home())]), (
        0 if usable else 1
    )


# --------------------------------------------------------------------------- Suite 3: 崩溃重启恢复


CRASH_SCENARIOS: tuple[str, ...] = (
    "approved_tool_after_intent",
    "approved_tool_after_write",
    "unknown_effect_reconciling_blocked",
    "unsettled_model_and_budget",
)


def run_suite3_crash_recovery(
    base_dir: Path,
    *,
    repeats: int,
    authority_root: Path,
    lock_root: Path,
    tracked_pids: TrackedPids,
) -> tuple[list[float], dict[str, Any]]:
    total_runs = max(repeats, len(CRASH_SCENARIOS))
    recovery_samples: list[float] = []
    daemon_recover_internal: list[float] = []

    resumed_succeeded = 0
    held_reconciling = 0
    blind_retries = 0
    lost_receipts = 0
    leaked_budget = 0
    preserved_unsettled = 0
    scenarios_seen: list[str] = []

    for idx in range(total_runs):
        scenario = CRASH_SCENARIOS[idx % len(CRASH_SCENARIOS)]
        if scenario not in scenarios_seen:
            scenarios_seen.append(scenario)

        r_root = base_dir / f"r{idx}"
        r_auth = base_dir / f"ra{idx}"
        ws = r_root / "ws"
        r_root.mkdir(parents=True, exist_ok=True)
        r_auth.mkdir(parents=True, exist_ok=True)

        env = _isolated_env(
            data_root=r_root,
            authority_root=r_auth,
            lock_root=lock_root,
        )
        # Step 1: 真子进程执行至 failpoint 触发 os._exit(77) 硬退
        crash_command, crash_env = _native_python_child(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_child-role",
                "crash-setup",
                "--data-root",
                str(r_root),
                "--_ws-dir",
                str(ws),
                "--_scenario",
                scenario,
            ],
            env=env,
        )
        crash_proc = subprocess.run(  # noqa: S603
            crash_command,
            env=crash_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
        )
        if crash_proc.returncode != 77 or "[failpoint]" not in crash_proc.stderr:
            raise RuntimeError(
                f"崩溃子进程未在预期 failpoint 硬退 ({scenario}): "
                f"rc={crash_proc.returncode} stderr={crash_proc.stderr[-500:]}"
            )

        meta = json.loads((r_root / "crash_meta.json").read_bytes().decode("utf-8"))
        run_id = str(meta["run_id"])

        if scenario == "unknown_effect_reconciling_blocked":
            # 模拟写中撕裂/外部污染：磁盘内容既非 before_state 亦非 expected_after
            _write_bytes_exact(ws / "note.md", b"torn-partial-unknown-state\n")

        # Step 2: 计时拉起真实 daemon 子进程执行启动恢复扫描与续跑/挂起
        t0 = time.perf_counter()
        rec_proc, rec_stderr = _launch_recovery_daemon(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_child-role",
                "recovery-daemon",
                "--data-root",
                str(r_root),
                "--_scenario",
                scenario,
            ],
            env=env,
        )
        _record_pid(rec_proc.pid, tracked_pids)
        target_states = (
            {"reconciling"}
            if scenario == "unknown_effect_reconciling_blocked"
            else {"succeeded"}
        )
        try:
            _wait_condition(
                lambda _rt=r_root: ipc.probe(_rt, timeout=0.1),
                timeout=20.0,
                interval=0.005,
            )
            client = ipc.Client(r_root, timeout=5.0)
            client.connect()
            try:
                final_run = _wait_condition(
                    lambda _c=client, _rid=run_id, _ts=target_states: (
                        r
                        if (
                            r := _c.call("run.status", {"run_id": _rid})["run"]
                        )["state"]
                        in _ts
                        else None
                    ),
                    timeout=20.0,
                    interval=0.005,
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                recovery_samples.append(elapsed_ms)
            finally:
                client.close()
            _record_lock_pids(r_root, tracked_pids)
        finally:
            daemon.request_shutdown(r_root, reason="slo-bench", timeout=10.0)
            try:
                rec_proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                rec_proc.kill()
                rec_proc.wait(timeout=5.0)
            rec_stderr.close()

        # Step 3: 离线打开事件库与预算账本逐项核对不变量
        rec_metrics = metrics_mod.read_metrics(r_root)
        internal_vals = [
            float(r["value"])
            for r in rec_metrics
            if r.get("metric") == metrics_mod.RESTART_RECOVER_MS
            and r.get("cold_start") is False
            and isinstance(r.get("value"), (int, float))
        ]
        if internal_vals:
            daemon_recover_internal.append(internal_vals[-1])

        model_stats: dict[str, int] = {"calls": 0, "summary_calls": 0, "tool_proposals": 0}
        stats_file = r_root / "recovery_model_stats.json"
        if stats_file.exists():
            model_stats = json.loads(stats_file.read_bytes().decode("utf-8"))

        log = EventLog(
            r_root / "events.db",
            blob_dir=default_material_paths(r_root / "events.db", r_root)[1],
        )
        try:
            events = log.read_run(run_id)
            ledger = BudgetLedger(log)
            account_id = f"run:{run_id}"
            all_resv_rows = log.conn.execute(
                "SELECT account_id, attempt_id, state FROM budget_reservations WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            # 检查是否存在重复 attempt_id 预占行
            seen_attempts = [(r["account_id"], r["attempt_id"]) for r in all_resv_rows]
            if len(seen_attempts) != len(set(seen_attempts)):
                leaked_budget += len(seen_attempts) - len(set(seen_attempts))

            if scenario == "approved_tool_after_intent":
                resumed_succeeded += 1
                tool_results = [e for e in events if e.type == "tool.result"]
                if not any(
                    e.payload.get("effect_state") == "none_applied" for e in tool_results
                ):
                    blind_retries += 1
                if (ws / "note.md").read_bytes() != b"initial-content\n":
                    blind_retries += 1
                if not replay(log, run_id).ok:
                    lost_receipts += 1
                if ledger.pending(account_id):
                    leaked_budget += len(ledger.pending(account_id))

            elif scenario == "approved_tool_after_write":
                resumed_succeeded += 1
                # 已写盘的调用只能有 1 条 intent、1 条 applied 结果与 1 张可核对 Receipt，禁止重跑
                intents = [e for e in events if e.type == "tool.intent"]
                if len(intents) != 1 or model_stats.get("tool_proposals", 0) != 0:
                    blind_retries += 1
                recs = receipts_of(log, run_id)
                expected_sha = hashlib.sha256(b"updated-after-approve\n").hexdigest()
                if len(recs) != 1 or recs[0].get("after_state") != expected_sha:
                    lost_receipts += 1
                if not replay(log, run_id).ok:
                    lost_receipts += 1
                if ledger.pending(account_id):
                    leaked_budget += len(ledger.pending(account_id))

            elif scenario == "unknown_effect_reconciling_blocked":
                assert final_run["state"] == "reconciling"
                held_reconciling += 1
                # 必须发射 recovery.blocked、登记权威阻断，且绝不触发恢复模型或再次派发
                blocked_ev = [e for e in events if e.type == "recovery.blocked"]
                intents = [e for e in events if e.type == "tool.intent"]
                prev_auth_env = os.environ.get("TIANSHU_AUTHORITY_ROOT")
                os.environ["TIANSHU_AUTHORITY_ROOT"] = str(r_auth)
                try:
                    ws_is_blocked = authority.is_blocked(ws)
                finally:
                    if prev_auth_env is None:
                        os.environ.pop("TIANSHU_AUTHORITY_ROOT", None)
                    else:
                        os.environ["TIANSHU_AUTHORITY_ROOT"] = prev_auth_env
                if (
                    not blocked_ev
                    or not ws_is_blocked
                    or len(intents) != 1
                    or model_stats.get("calls", 0) != 0
                ):
                    blind_retries += 1

            elif scenario == "unsettled_model_and_budget":
                resumed_succeeded += 1
                # 摘要调用崩溃后严禁盲目重调摘要模型，且未决预占须保留在 reconcile() 中不可静默清零
                if model_stats.get("summary_calls", 0) != 0:
                    blind_retries += 1
                sum_store = SummaryStore(log)
                mat_row = log.conn.execute(
                    "SELECT material_id FROM summary_versions LIMIT 1"
                ).fetchone()
                if mat_row is None:
                    leaked_budget += 1
                else:
                    versions = sum_store.versions(mat_row["material_id"])
                    if [v.status for v in versions] != ["failed"]:
                        blind_retries += 1
                unsettled = ledger.reconcile(account_id)
                summary_unsettled = [
                    u for u in unsettled if str(u.get("attempt_id", "")).startswith("summary#")
                ]
                main_pending = [
                    u
                    for u in ledger.pending(account_id)
                    if not str(u.attempt_id).startswith("summary#")
                ]
                if len(summary_unsettled) == 1:
                    preserved_unsettled += 1
                else:
                    # 被偷偷清零或重复创建都算预算账本泄漏/失真
                    leaked_budget += 1
                if main_pending:
                    leaked_budget += len(main_pending)
                if not replay(log, run_id).ok:
                    lost_receipts += 1
        finally:
            log.close()
            shutil.rmtree(r_root, ignore_errors=True)
            shutil.rmtree(r_auth, ignore_errors=True)

    rec_p95 = metrics_mod.percentile(daemon_recover_internal, 95)
    audit = {
        "scenarios_tested": scenarios_seen,
        "total_crash_runs": total_runs,
        "resumed_to_succeeded": resumed_succeeded,
        "held_in_reconciling": held_reconciling,
        "blind_retries": blind_retries,
        "lost_receipts": lost_receipts,
        "leaked_budget_reservations": leaked_budget,
        "preserved_unsettled_reservations": preserved_unsettled,
        "daemon_internal_recover_p95_ms": None if rec_p95 is None else round(rec_p95, 3),
    }
    return recovery_samples, audit


# --------------------------------------------------------------------------- 门禁评估与报告生成


def evaluate_slo_gate(
    raw_report: dict[str, Any],
    thresholds: dict[str, Any],
    *,
    smoke: bool = False,
    smoke_repeats: int = 3,
    schema_defs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """对测量报告执行严格 SLO 门禁裁决。

    返回 `(evaluated_report, exit_code)`：
    - `0`：PASS
    - `1`：测量指标超限 / 缺样本 / 安全或资源不变量失败
    - `2`：schema 或阈值配置违反防放宽契约
    """
    defs = load_slo_schema_defs() if schema_defs is None else schema_defs
    t_errors = validate_against_def(defs, "thresholdsConfig", thresholds)
    if t_errors:
        raise SloConfigError("阈值配置不合法：" + "；".join(t_errors))
    relax_errors = check_no_unversioned_relaxation(thresholds)
    if relax_errors:
        raise SloConfigError("阈值防放宽检查失败：" + "；".join(relax_errors))

    report = copy.deepcopy(raw_report)
    failures: list[str] = list(report.get("failures") or [])
    metrics_out = report.get("metrics")
    if not isinstance(metrics_out, dict):
        raise SloConfigError("报告缺少 metrics 对象")

    metrics_cfg = thresholds["metrics"]
    global_min_repeats = int(thresholds["min_repeats"])

    for metric_name in REQUIRED_SLO_METRICS:
        if metric_name not in metrics_out:
            failures.append(f"missing_metric: 缺少必需指标 {metric_name}")
            continue
        m_entry = metrics_out[metric_name]
        m_cfg = metrics_cfg[metric_name]
        req_repeats = (
            max(1, int(smoke_repeats))
            if smoke
            else max(global_min_repeats, int(m_cfg["min_repeats"]))
        )
        m_entry["threshold_p95_ms"] = float(m_cfg["p95_max"])
        m_entry["threshold_max_ms"] = float(m_cfg["max_ceiling"])
        m_entry["min_repeats_required"] = req_repeats

        metric_ok = True
        if int(m_entry.get("unknown_count", 0)) > 0:
            metric_ok = False
            failures.append(
                f"unknown_samples: {metric_name} 含 {m_entry['unknown_count']} 个未知样本"
            )
        if int(m_entry.get("repeats", 0)) < req_repeats:
            metric_ok = False
            failures.append(
                f"insufficient_samples: {metric_name} 样本数 "
                f"{m_entry.get('repeats')} < {req_repeats}"
            )
        p95 = m_entry.get("p95_ms")
        max_v = m_entry.get("max_ms")
        if p95 is None or float(p95) > float(m_cfg["p95_max"]):
            metric_ok = False
            failures.append(
                f"p95_exceeded: {metric_name} p95={p95}ms > 冻结阈值 {m_cfg['p95_max']}ms"
            )
        if max_v is None or float(max_v) > float(m_cfg["max_ceiling"]):
            metric_ok = False
            failures.append(
                f"max_exceeded: {metric_name} max={max_v}ms > 上限 {m_cfg['max_ceiling']}ms"
            )
        m_entry["passed"] = metric_ok

    slow_sub = report.get("slow_subscriber") or {}
    if not slow_sub.get("ipc_non_reading_attached") or not slow_sub.get(
        "sse_non_reading_attached"
    ):
        failures.append("slow_subscriber_missing: 未同时挂载 IPC 与 SSE 不读慢订阅者")
    if int(slow_sub.get("dropped_count", 0)) < 1:
        failures.append("slow_subscriber_not_dropped: 未观测到慢订阅者背压断连计数")
    if not slow_sub.get("cursor_reconnect_verified"):
        failures.append("cursor_reconnect_failed: 慢订阅者断连后 since_seq 补读存在缺口")

    inflight = report.get("in_flight_effects") or {}
    if int(inflight.get("post_cancel_dispatched_count", 0)) != 0:
        cnt = inflight.get("post_cancel_dispatched_count")
        failures.append(f"post_cancel_dispatch: 取消后仍发生了 {cnt} 次新工具派发")
    if not inflight.get("ack_before_inflight_release_all"):
        failures.append("cancel_ack_blocked_by_inflight: cancel_ack 被在途慢操作阻塞")
    if int(inflight.get("in_flight_receipts_verified", 0)) < int(
        inflight.get("slow_tool_runs", 0)
    ):
        failures.append("in_flight_receipt_missing: 在途慢工具副作用缺少已核验 Receipt")

    audit = report.get("recovery_audit") or {}
    if int(audit.get("blind_retries", 0)) != 0:
        failures.append(f"blind_retries: 检测到 {audit.get('blind_retries')} 次盲目重试")
    if int(audit.get("lost_receipts", 0)) != 0:
        failures.append(
            f"lost_receipts: 检测到 {audit.get('lost_receipts')} 次回执丢失或回放不符"
        )
    if int(audit.get("leaked_budget_reservations", 0)) != 0:
        leaked_n = audit.get("leaked_budget_reservations")
        failures.append(
            f"leaked_budget_reservations: 检测到 {leaked_n} 处预算预占泄漏/清零"
        )

    orphans = report.get("orphan_pids") or []
    if orphans:
        failures.append(f"orphan_processes: 存在未退出的残留子进程 PID {orphans}")

    report["gate_mode"] = "smoke" if smoke else "release"
    report["failures"] = failures
    report["verdict"] = "PASS" if not failures else "FAIL"

    r_errors = validate_against_def(defs, "sloReport", report)
    if r_errors:
        raise SloConfigError("生成的 SLO 报告违反 slo-v1.json：" + "；".join(r_errors))

    return report, (0 if not failures else 1)


def render_markdown_report(report: dict[str, Any]) -> str:
    env = report["environment"]
    ds = report["dataset"]
    slow = report["slow_subscriber"]
    inf = report["in_flight_effects"]
    aud = report["recovery_audit"]
    sha_short = report["thresholds_sha256"][:16]

    lines = [
        "# 天枢 Agent · PRD §10.6 本地运行质量 SLO 实测报告",
        "",
        f"- **裁决结论（Verdict）**：**`{report['verdict']}`**（模式：`{report['gate_mode']}`）",
        f"- **策略版本**：`slo-thresholds-v{report['policy_version']}`（SHA256 `{sha_short}…`）",
        f"- **测量时间（UTC）**：`{report['measured_at']}`",
        f"- **运行环境**：`{env['os']}` · `{env['arch']}` · `{env['cpu_model']}` · "
        f"内存 `{env['memory_gib']} GiB` · Python `{env['python_version']}` · "
        f"脱敏 `{env['redaction']}`",
        f"- **热启动数据规模**：`{ds['warm_tasks']}` 任务 / `{ds['warm_runs']}` 运行 / "
        f"`{ds['warm_events']}` 事件 / `events.db` `{ds['warm_db_bytes']}` B / "
        f"`artifacts` `{ds['warm_artifacts_bytes']}` B",
        "",
        "## 1. 核心 SLO 指标分布与冻结阈值核验",
        "",
        "| 指标 (`metric`) | 样本数 | `min` (ms) | `p50` (ms) | `p95` (ms) | "
        "`max` (ms) | 冻结 `p95` 上限 (ms) | 单次峰值上限 (ms) | 结果 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in REQUIRED_SLO_METRICS:
        m = report["metrics"].get(name)
        if not m:
            continue
        badge = "PASS" if m["passed"] else "FAIL"
        lines.append(
            f"| `{name}` | {m['repeats']} | {m['min_ms']} | **{m['p50_ms']}** | "
            f"**{m['p95_ms']}** | {m['max_ms']} | {m['threshold_p95_ms']} | "
            f"{m['threshold_max_ms']} | `{badge}` |"
        )

    lines.extend(
        [
            "",
            f"> 附注：`daemon` 进程内自身就绪中位数（不含父进程 spawn）："
            f"冷启动 `{ds['daemon_internal_cold_p50_ms']} ms`，"
            f"热启动 `{ds['daemon_internal_warm_p50_ms']} ms`。",
            "",
            "## 2. 慢订阅者背压与在途副作用分账（PRD §10.6）",
            "",
            f"- **真实不读客户端挂载**：IPC 订阅者 `{slow['ipc_non_reading_attached']}` + "
            f"Host SSE 订阅者 `{slow['sse_non_reading_attached']}`",
            f"- **慢订阅者背压断连计数（`slow_subscriber_dropped`）**："
            f"`{slow['dropped_count']}` 次",
            f"- **断连后 `since_seq` 游标重连连续性核验**：`{slow['cursor_reconnect_verified']}`"
            f"（补读核验 `{slow['reconnected_events_count']}` 条连续事件，0 缺口）",
            f"- **在途慢操作与新派发隔离**：慢模型 `{inf['slow_model_runs']}` 轮 + "
            f"慢工具 `{inf['slow_tool_runs']}` 轮"
            f"（受控在途窗口 `{inf['in_flight_hold_target_ms']} ms`）",
            f"  - 控制面 `cancel_ack` 全部早于在途操作放行："
            f"`{inf['ack_before_inflight_release_all']}`",
            f"  - 已在途工具生效并核验 Receipt 数："
            f"`{inf['in_flight_applied_effects']}` / `{inf['in_flight_receipts_verified']}`",
            f"  - **取消后新派发工具数（必须为 0）**：`{inf['post_cancel_dispatched_count']}`",
            f"  - 在途操作返回后安全点停机纯耗时："
            f"`p50 = {inf['post_release_halt_p50_ms']} ms`，"
            f"`p95 = {inf['post_release_halt_p95_ms']} ms`",
            "",
            "## 3. 崩溃重启恢复不变量审计（`os._exit` 故障注入）",
            "",
            f"- **覆盖故障场景**：`{', '.join(aud['scenarios_tested'])}`"
            f"（共 `{aud['total_crash_runs']}` 轮）",
            f"- **恢复结局分流**：可确定状态自动续跑完结 `{aud['resumed_to_succeeded']}` 轮 · "
            f"未知副作用明确落 `reconciling` 并阻断目录 `{aud['held_in_reconciling']}` 轮",
            f"- **盲目重试次数（必须为 0）**：`{aud['blind_retries']}`",
            f"- **丢失或失配 Receipt 数（必须为 0）**：`{aud['lost_receipts']}`",
            f"- **预算预占泄漏或静默清零数（必须为 0）**：`{aud['leaked_budget_reservations']}`"
            f"（保留待对账未决摘要预占 `{aud['preserved_unsettled_reservations']}` 笔）",
            f"- **`daemon` 启动扫描内部 `p95`**：`{aud['daemon_internal_recover_p95_ms']} ms`",
            f"- **退出后残留孤儿进程 PID**：`{report['orphan_pids']}`",
        ]
    )
    if report["failures"]:
        lines.extend(["", "## 4. 失败项明细", ""])
        for item in report["failures"]:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def run_all_benchmarks(
    *,
    repeats: int = 10,
    smoke: bool = False,
    thresholds_path: Path = DEFAULT_THRESHOLDS_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> tuple[dict[str, Any], int]:
    thresholds, thresholds_sha, defs = load_and_validate_thresholds(
        thresholds_path, schema_path=schema_path
    )
    if repeats < 1:
        raise SloConfigError("repeats 必须 >= 1")

    temp_base = _short_temp_dir("tsslo-")
    authority_root = temp_base / "auth"
    lock_root = temp_base / "locks"
    authority_root.mkdir(parents=True, exist_ok=True)
    lock_root.mkdir(parents=True, exist_ok=True)
    tracked_pids = TrackedPids()

    ds_profile = thresholds["dataset_profile"]
    warm_tasks = (
        int(ds_profile["warm_tasks"])
        if not smoke
        else min(15, int(ds_profile["warm_tasks"]))
    )
    warm_events_min = (
        int(ds_profile["warm_events_min"])
        if not smoke
        else min(150, int(ds_profile["warm_events_min"]))
    )
    event_bytes_min = int(ds_profile["slow_subscriber_event_bytes_min"])

    try:
        cold_samples, warm_samples, dataset_info = run_suite1_startup(
            temp_base,
            repeats=repeats,
            warm_tasks=warm_tasks,
            warm_events_min=warm_events_min,
            authority_root=authority_root,
            lock_root=lock_root,
            tracked_pids=tracked_pids,
        )
        (
            cancel_ack_samples,
            cancel_stop_samples,
            approval_samples,
            slow_sub_info,
            inflight_info,
        ) = run_suite2_cancel_and_approval(
            temp_base,
            repeats=repeats,
            event_bytes_min=event_bytes_min,
            authority_root=authority_root,
            lock_root=lock_root,
            tracked_pids=tracked_pids,
        )
        recovery_samples, recovery_audit = run_suite3_crash_recovery(
            temp_base,
            repeats=repeats,
            authority_root=authority_root,
            lock_root=lock_root,
            tracked_pids=tracked_pids,
        )
        orphans = _find_orphan_pids(tracked_pids, temp_base)
    finally:
        shutil.rmtree(temp_base, ignore_errors=True)

    m_cfg = thresholds["metrics"]
    req_rep = max(1, repeats) if smoke else int(thresholds["min_repeats"])
    raw_samples_map: dict[str, list[float | None]] = {
        "startup_cold_ms": list(cold_samples),
        "startup_warm_ms": list(warm_samples),
        "cancel_ack_ms": list(cancel_ack_samples),
        "cancel_stop_dispatch_ms": list(cancel_stop_samples),
        "approval_rpc_ms": list(approval_samples),
        "restart_recovery_ms": list(recovery_samples),
    }
    metrics_summary = {
        name: _summarize_samples(
            vals,
            p95_max=float(m_cfg[name]["p95_max"]),
            max_ceiling=float(m_cfg[name]["max_ceiling"]),
            min_repeats_required=req_rep,
        )
        for name, vals in raw_samples_map.items()
    }

    raw_report: dict[str, Any] = {
        "schema_version": "slo-report-v1",
        "verdict": "PASS",
        "gate_mode": "smoke" if smoke else "release",
        "policy_version": int(thresholds["policy_version"]),
        "thresholds_sha256": thresholds_sha,
        "measured_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": collect_hardware_metadata(),
        "dataset": dataset_info,
        "metrics": metrics_summary,
        "slow_subscriber": slow_sub_info,
        "in_flight_effects": inflight_info,
        "recovery_audit": recovery_audit,
        "orphan_pids": orphans,
        "failures": [],
    }
    extra_redact = [str(temp_base), str(REPO_ROOT), str(Path.home())]
    redacted_report = _redact_obj(raw_report, extra_paths=extra_redact)
    return evaluate_slo_gate(
        redacted_report,
        thresholds,
        smoke=smoke,
        smoke_repeats=repeats,
        schema_defs=defs,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 PRD §10.6 本地运行质量 SLO 基准测试并执行冻结阈值门禁。"
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=10,
        help="每项 SLO 指标的重复测量次数（发布门禁默认 10）",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="显式启用快速冒烟模式（允许较少重复数，仍强制全部 p95 阈值与安全不变量）",
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=DEFAULT_THRESHOLDS_PATH,
        help="版本化冻结 SLO 阈值配置文件路径",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help="SLO JSON Schema 路径",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="写出脱敏后的结构化 JSON 报告路径",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="写出脱敏后的结构化 Markdown 报告路径",
    )
    # 内部受控子进程参数（--data-root 与 host.run_host 的子进程约定保持一致）
    parser.add_argument("--_child-role", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--data-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_ui-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_ws-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scenario", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_entered-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_release-file", default=None, help=argparse.SUPPRESS)
    return parser


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    args = _build_parser().parse_args(argv)
    if args._child_role:
        return _run_child_role(args)

    try:
        report, exit_code = run_all_benchmarks(
            repeats=args.repeats,
            smoke=args.smoke,
            thresholds_path=args.thresholds,
            schema_path=args.schema,
        )
    except SloConfigError as exc:
        print(f"[slo-gate] 配置或契约错误：{exc}", file=sys.stderr)
        return 2

    json_bytes = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if args.output_json is not None:
        _write_bytes_exact(args.output_json, json_bytes)
    if args.output_md is not None:
        md_bytes = render_markdown_report(report).encode("utf-8")
        _write_bytes_exact(args.output_md, md_bytes)

    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "gate_mode": report["gate_mode"],
                "metrics": {
                    k: {"p50_ms": v["p50_ms"], "p95_ms": v["p95_ms"], "max_ms": v["max_ms"]}
                    for k, v in report["metrics"].items()
                },
                "orphan_pids": report["orphan_pids"],
                "failures": report["failures"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
