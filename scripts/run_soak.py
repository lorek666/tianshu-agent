#!/usr/bin/env python3
"""PRD §10.6 常驻浸泡诊断（G3-B11）。

独立运行须使用专用测试 OS 账户或隔离测试机；临时 data root 不隔离用户级维护锁。
    uv run --frozen python scripts/run_soak.py \
        --duration-hours 10 --output-dir .omc/artifacts/soak-10h
    uv run --frozen python scripts/run_soak.py \
        --verify-only --output-dir .omc/artifacts/soak-10h

运行目录必须是新建叶子。脚本模型驱动真实 Host/Daemon，交替记录空闲和执行采样，
并覆盖审批/撤销、流式输出、记忆、订阅续读、暂停/取消、调度与计划重启。
`status.json` 完成标记晚于三份最终材料；v1 PASS 或离线校验成功均不是发布裁决。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import datetime as dt
import io
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(SRC_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import measure_slo as slo_mod  # noqa: E402
import soak_observation as obs  # noqa: E402

from tianshu import daemon, host, ipc  # noqa: E402
from tianshu import metrics as metrics_mod  # noqa: E402
from tianshu.capability import CapabilityProfile  # noqa: E402
from tianshu.fsutil import atomic_write_bytes  # noqa: E402
from tianshu.model import (  # noqa: E402
    ModelIdentity,
    ModelResponse,
    ModelStreamDelta,
    ToolCall,
)

DEFAULT_DURATION_HOURS = 10.0
DEFAULT_SAMPLE_INTERVAL_S = 30.0
MIN_SLOPE_WINDOW_S = 300.0  # 至少 5 分钟跨度才计算每小时线性回归斜率，短跑改用净增量门禁


# ---------------------------------------------------------------- 跨平台进程资源采样


def _parse_ps_cputime(raw: str) -> float | None:
    """解析 POSIX `ps -o cputime=` 输出（`[DD-]HH:MM:SS` 或 `MM:SS.xx`）为秒。"""
    text = raw.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        elif len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds = float(parts[1])
        else:
            return None
    except ValueError:
        return None
    return days * 86400.0 + hours * 3600.0 + minutes * 60.0 + seconds


def _self_open_fds() -> int | None:
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(fd_dir):
            try:
                # 减 1 排除 listdir 自身临时打开的目录 fd
                return max(0, len(os.listdir(fd_dir)) - 1)
            except OSError:
                continue
    if os.name == "nt":
        return _win32_process_stats(os.getpid())[2]
    return None


def _win32_process_stats(pid: int) -> tuple[float | None, float | None, int | None]:
    """Windows 标准库 ctypes 读取指定 PID 的 (rss_mib, cpu_time_s, handle_count)。"""
    if os.name != "nt" or pid <= 0:
        return None, None, None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    process_query_limited = 0x1000
    handle = kernel32.OpenProcess(process_query_limited, False, int(pid))
    if not handle:
        return None, None, None
    try:
        # 1. Handle count
        handle_count = ctypes.c_ulong(0)
        fds: int | None = None
        if kernel32.GetProcessHandleCount(handle, ctypes.byref(handle_count)):
            fds = int(handle_count.value)

        # 2. CPU times (100ns units)
        creation = ctypes.c_ulonglong(0)
        exit_t = ctypes.c_ulonglong(0)
        kernel_t = ctypes.c_ulonglong(0)
        user_t = ctypes.c_ulonglong(0)
        cpu_s: float | None = None
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            cpu_s = (int(kernel_t.value) + int(user_t.value)) / 10_000_000.0

        # 3. WorkingSetSize via K32GetProcessMemoryInfo / psapi
        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        rss_mib: float | None = None
        mem_fn = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if mem_fn is None:
            psapi = getattr(ctypes.windll, "psapi", None)  # type: ignore[attr-defined]
            mem_fn = getattr(psapi, "GetProcessMemoryInfo", None) if psapi else None
        if mem_fn is not None and mem_fn(
            handle, ctypes.byref(counters), counters.cb
        ):
            rss_mib = round(float(counters.WorkingSetSize) / (1024.0 * 1024.0), 3)
        return rss_mib, cpu_s, fds
    finally:
        kernel32.CloseHandle(handle)


def _pid_open_fds(pid: int | None) -> int | None:
    if pid is None or pid <= 0:
        return None
    if pid == os.getpid():
        return _self_open_fds()
    if os.name == "nt":
        return _win32_process_stats(pid)[2]
    proc_fd = f"/proc/{pid}/fd"
    if os.path.isdir(proc_fd):
        try:
            return len(os.listdir(proc_fd))
        except OSError:
            pass
    try:
        out = subprocess.run(
            ["lsof", "-n", "-P", "-a", "-p", str(pid), "-d", "0-99999"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
            return max(0, len(lines) - 1)
    except OSError:
        pass
    return None


class ProcessResourceTracker:
    """跟踪守护进程与 Host 进程的当前 RSS、区间 CPU% 与打开 FD 数。"""

    def __init__(self) -> None:
        self._cpu_history: dict[int, tuple[float, float]] = {}

    def inspect_pid(
        self, pid: int | None
    ) -> tuple[float | None, float | None, int | None]:
        if pid is None or pid <= 0:
            return None, None, None
        now_wall = time.monotonic()

        if os.name == "nt":
            rss_mib, cpu_s, fds = _win32_process_stats(pid)
            cpu_pct = self._compute_cpu_pct(pid, now_wall, cpu_s, fallback_pct=None)
            return rss_mib, cpu_pct, fds

        rss_mib: float | None = None
        cpu_s: float | None = None
        ps_pct: float | None = None

        if sys.platform.startswith("linux") and os.path.isdir(f"/proc/{pid}"):
            try:
                status_txt = (
                    Path(f"/proc/{pid}/status")
                    .read_bytes()
                    .decode("utf-8", errors="replace")
                )
                for line in status_txt.splitlines():
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            rss_mib = round(int(parts[1]) / 1024.0, 3)
                        break
                stat_txt = (
                    Path(f"/proc/{pid}/stat")
                    .read_bytes()
                    .decode("utf-8", errors="replace")
                )
                rparen = stat_txt.rfind(")")
                if rparen > 0:
                    fields = stat_txt[rparen + 2 :].split()
                    # utime=fields[11], stime=fields[12]
                    clk = os.sysconf("SC_CLK_TCK")
                    if len(fields) > 12 and clk > 0:
                        cpu_s = (int(fields[11]) + int(fields[12])) / float(clk)
            except (OSError, ValueError):
                pass

        if rss_mib is None or cpu_s is None:
            try:
                out = subprocess.run(
                    ["ps", "-o", "rss=,pcpu=,cputime=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if out.returncode == 0 and out.stdout.strip():
                    parts = out.stdout.strip().split()
                    if len(parts) >= 3:
                        rss_mib = round(int(parts[0]) / 1024.0, 3)
                        ps_pct = round(float(parts[1]), 2)
                        cpu_s = _parse_ps_cputime(parts[2])
            except (OSError, ValueError):
                pass

        cpu_pct = self._compute_cpu_pct(pid, now_wall, cpu_s, fallback_pct=ps_pct)
        fds = _pid_open_fds(pid)
        return rss_mib, cpu_pct, fds

    def _compute_cpu_pct(
        self,
        pid: int,
        now_wall: float,
        cpu_s: float | None,
        *,
        fallback_pct: float | None,
    ) -> float | None:
        if cpu_s is None:
            return fallback_pct
        prev = self._cpu_history.get(pid)
        self._cpu_history[pid] = (now_wall, cpu_s)
        if prev is None:
            return fallback_pct if fallback_pct is not None else 0.0
        prev_wall, prev_cpu = prev
        dt_wall = now_wall - prev_wall
        if dt_wall <= 0.001:
            return fallback_pct
        return round(max(0.0, (cpu_s - prev_cpu) / dt_wall * 100.0), 2)


def compute_rss_slope_mib_per_hour(
    samples: list[dict[str, Any]],
    *,
    min_span_s: float = MIN_SLOPE_WINDOW_S,
) -> float | None:
    """对稳定期（跳过前 20% 冷热身样本）的 total_rss_mib 计算最小二乘斜率（MiB/h）。"""
    pts: list[tuple[float, float]] = [
        (float(s["elapsed_s"]), float(s["total_rss_mib"]))
        for s in samples
        if isinstance(s.get("elapsed_s"), (int, float))
        and isinstance(s.get("total_rss_mib"), (int, float))
        and not isinstance(s.get("total_rss_mib"), bool)
    ]
    if len(pts) < 2:
        return None
    total_span = pts[-1][0] - pts[0][0]
    if total_span < min_span_s:
        return None
    warmup_cutoff = pts[0][0] + total_span * 0.2
    steady = [p for p in pts if p[0] >= warmup_cutoff]
    if len(steady) < 2:
        steady = pts
    span = steady[-1][0] - steady[0][0]
    if span <= 0:
        return None
    n = float(len(steady))
    mean_x = sum(x for x, _ in steady) / n
    mean_y = sum(y for _, y in steady) / n
    denom = sum((x - mean_x) ** 2 for x, _ in steady)
    if denom <= 1e-12:
        return 0.0
    slope_per_sec = sum((x - mean_x) * (y - mean_y) for x, y in steady) / denom
    return round(slope_per_sec * 3600.0, 4)


# --------------------------------------------------------------------------- Soak 子进程模型与宿主


class _SoakModel:
    """浸泡工作负载的确定性本地模型（支持 W4 流式增量、文件写/改、只读检索与受控慢轮次）。"""

    def __init__(self, data_root: Path) -> None:
        self._ctl_dir = data_root / "soak_ctl"
        self._ctl_dir.mkdir(parents=True, exist_ok=True)
        self._turns: dict[str, int] = {}

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="soak-local",
            model="soak-scripted-v1",
            supports_tool_calling=True,
            supports_parallel_tool_calls=True,
            supports_streaming=True,
            declared_by="scripts/run_soak.py",
        )

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="openai", vendor="openai", model="gpt-4o-mini")

    @staticmethod
    def _goal(messages: list[dict[str, Any]]) -> str:
        for msg in messages:
            if msg.get("role") == "user":
                return str(msg.get("content") or "")
        return ""

    def stream_complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_delta: Any
    ) -> ModelResponse:
        goal = self._goal(messages)
        if goal.startswith(("effects:", "sched:")):
            on_delta(ModelStreamDelta(kind="text", text=f"[stream:{goal[:24]}] "))
        return self.complete(messages, tools)

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]  # noqa: ARG002
    ) -> ModelResponse:
        goal = self._goal(messages)
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        turn = self._turns.get(goal, 0) + 1
        self._turns[goal] = turn

        if goal.startswith("effects:"):
            # 格式：effects:<cycle>
            cyc = goal.split(":", 1)[1].strip()
            if not has_tool_result:
                return ModelResponse(
                    finish_reason="tool_use",
                    content="写入周期工作文件并附带记忆提案",
                    tool_calls=[
                        ToolCall(
                            f"eff_create_{cyc}",
                            "create_report",
                            {
                                "name": "soak_note.md",
                                "content": f"# Soak Cycle {cyc}\nDeterministic local payload.\n",
                            },
                        ),
                        ToolCall(
                            f"eff_mem_{cyc}",
                            "memory.propose",
                            {
                                "kind": "fact",
                                "content": f"浸泡周期 {cyc} 偏好保持简洁格式",
                                "scope": "user",
                            },
                        ),
                    ],
                    usage={"input_tokens": 36, "output_tokens": 18},
                )
            return ModelResponse(
                finish_reason="end_turn",
                content=f"周期 {cyc} 写入完成",
                usage={"input_tokens": 16, "output_tokens": 6},
            )

        if goal.startswith("pure:"):
            cyc = goal.split(":", 1)[1].strip()
            if not has_tool_result:
                return ModelResponse(
                    finish_reason="tool_use",
                    content="只读枚举工作区并检索记忆",
                    tool_calls=[
                        ToolCall(f"pure_ls_{cyc}", "list_files", {"path": "."}),
                        ToolCall(
                            f"pure_ms_{cyc}",
                            "memory.search",
                            {"query": "浸泡周期"},
                        ),
                    ],
                    usage={"input_tokens": 24, "output_tokens": 12},
                )
            return ModelResponse(
                finish_reason="end_turn",
                content="纯读核验完成",
                usage={"input_tokens": 14, "output_tokens": 5},
            )

        if goal.startswith("gated:"):
            # 格式：gated:<tag> —— 第 1 轮在标记放行前挂起，之后进第 2 轮供 pause/cancel 测试
            tag = goal.split(":", 1)[1].strip()
            entered = self._ctl_dir / f"{tag}.entered"
            release = self._ctl_dir / f"{tag}.release"
            if turn == 1:
                slo_mod._write_bytes_exact(entered, b"1")  # noqa: SLF001
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline and not release.exists():
                    time.sleep(0.005)
                return ModelResponse(
                    finish_reason="tool_use",
                    content="第一轮放行，进入安全点检查",
                    tool_calls=[
                        ToolCall(f"gate_ls_{tag}", "list_files", {"path": "."})
                    ],
                    usage={"input_tokens": 20, "output_tokens": 8},
                )
            if turn == 2:
                entered2 = self._ctl_dir / f"{tag}.turn2_entered"
                release2 = self._ctl_dir / f"{tag}.turn2_release"
                slo_mod._write_bytes_exact(entered2, b"1")  # noqa: SLF001
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline and not release2.exists():
                    time.sleep(0.005)
            return ModelResponse(
                finish_reason="end_turn",
                content="受控慢任务结束",
                usage={"input_tokens": 12, "output_tokens": 4},
            )

        # sched:* 或普通任务直接快速收尾
        return ModelResponse(
            finish_reason="end_turn",
            content="scheduled-ok",
            usage={"input_tokens": 10, "output_tokens": 4},
        )


def _run_child_role(args: argparse.Namespace) -> int:
    role = args._child_role
    data_root = Path(args.data_root)
    if role == "soak-daemon":
        daemon.SEND_STALL_S = 0.25
        daemon.SUBSCRIBER_BACKLOG_STALL_S = 0.25
        os.environ[metrics_mod.ENV_METRICS_INTERVAL] = "0.5"
        daemon.serve(
            data_root,
            model=_SoakModel(data_root),
            driver_interval_s=0.02,
        )
        return 0
    if role == "soak-host":
        ui_dir = Path(args._ui_dir)
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_child-role",
            "soak-daemon",
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
    raise ValueError(f"未知角色：{role}")


# --------------------------------------------------------------------------- Soak 编排器与门禁


class _HostCluster:
    """管理真子进程 Host + Daemon 的启动、优雅停止与 PID 记录。"""

    def __init__(
        self,
        data_root: Path,
        ui_dir: Path,
        env: dict[str, str],
        tracked_pids: slo_mod.TrackedPids,
    ) -> None:
        self.data_root = data_root
        self.ui_dir = ui_dir
        self.env = env
        self.tracked_pids = tracked_pids
        self.proc: subprocess.Popen[bytes] | None = None
        self.info: host.HostInfo | None = None
        self.generation = 0

    def start(self) -> host.HostInfo:
        self.generation += 1
        # The parent cannot leave an unread PIPE attached during a ten-hour run.
        # This private transient log is not one of the D7 export materials.
        stderr_path = self.data_root.parent / "host-stderr.log"
        command, env = slo_mod._native_python_child(  # noqa: SLF001
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_child-role",
                "soak-host",
                "--data-root",
                str(self.data_root),
                "--_ui-dir",
                str(self.ui_dir),
            ],
            env=self.env,
        )
        with stderr_path.open("wb") as err_log:
            proc = subprocess.Popen(  # noqa: S603
                command,
                stdout=subprocess.DEVNULL,
                stderr=err_log,
                env=env,
            )
        self.proc = proc
        slo_mod._record_pid(proc.pid, self.tracked_pids)  # noqa: SLF001

        with stderr_path.open("rb") as log:
            self.info = slo_mod._wait_host_ready(  # noqa: SLF001
                proc, self.data_root, log, timeout=25.0,
            )
        slo_mod._record_lock_pids(  # noqa: SLF001
            self.data_root, self.tracked_pids, phase=f"soak_ready:{self.generation}",
        )
        return self.info

    def current_pids(self) -> tuple[int | None, int | None]:
        slo_mod._record_lock_pids(  # noqa: SLF001
            self.data_root, self.tracked_pids, phase=f"soak_sample:{self.generation}",
        )
        h_pid: int | None = None
        d_pid: int | None = None
        try:
            h_info = host.read_host_info(self.data_root)
            if h_info is not None:
                h_pid = h_info.pid
        except host.HostError:
            pass
        d_lock = daemon.lock_path(self.data_root)
        try:
            d_raw = json.loads(d_lock.read_bytes().decode("utf-8"))
            if isinstance(d_raw.get("pid"), int):
                d_pid = int(d_raw["pid"])
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        return d_pid, h_pid

    def stop(self) -> list[str]:
        failures = []
        slo_mod._record_lock_pids(  # noqa: SLF001
            self.data_root, self.tracked_pids, phase=f"soak_before_stop:{self.generation}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            rc = host.stop_host(self.data_root, timeout=15.0)
        if rc != 0:
            failures.append(f"host_stop_exit:{rc}")
        if self.proc is not None:
            try:
                self.proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                failures.append("host_stop_forced_kill")
                self.proc.kill()
                self.proc.wait(timeout=5.0)
            if self.proc.returncode != 0:
                failures.append(f"host_process_exit:{self.proc.returncode}")
            with (self.data_root.parent / "host-stderr.log").open("rb") as stderr:
                slo_mod._capture_host_stop(  # noqa: SLF001
                    self.proc, self.tracked_pids, stderr,
                    phase=f"soak_after_wait:{self.generation}",
                )
            self.proc = None
        self.info = None
        return failures


def evaluate_soak_gate(
    raw_report: dict[str, Any],
    thresholds: dict[str, Any],
    *,
    schema_defs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    defs = slo_mod.load_slo_schema_defs() if schema_defs is None else schema_defs
    t_errors = slo_mod.validate_against_def(defs, "thresholdsConfig", thresholds)
    if t_errors:
        raise slo_mod.SloConfigError("阈值配置不合法：" + "；".join(t_errors))
    relax_errors = slo_mod.check_no_unversioned_relaxation(thresholds)
    if relax_errors:
        raise slo_mod.SloConfigError("阈值防放宽检查失败：" + "；".join(relax_errors))

    report = copy.deepcopy(raw_report)
    failures: list[str] = list(report.get("failures") or [])
    limits = thresholds["soak_limits"]
    res = report["resources"]
    leaks = report["leak_checks"]

    idle_max = res.get("idle_rss_max_mib")
    active_max = res.get("active_rss_max_mib")
    rss_ok = True
    if idle_max is not None and float(idle_max) > float(limits["rss_idle_max_mib"]):
        rss_ok = False
        failures.append(
            f"idle_rss_exceeded: {idle_max} MiB > {limits['rss_idle_max_mib']} MiB"
        )
    if active_max is not None and float(active_max) > float(limits["rss_active_max_mib"]):
        rss_ok = False
        failures.append(
            f"active_rss_exceeded: {active_max} MiB > {limits['rss_active_max_mib']} MiB"
        )
    slope = leaks.get("rss_slope_mib_per_hour")
    if slope is not None and float(slope) > float(limits["rss_slope_max_mib_per_hour"]):
        rss_ok = False
        failures.append(
            f"rss_slope_leak: {slope} MiB/h > {limits['rss_slope_max_mib_per_hour']} MiB/h"
        )
    net_growth = leaks.get("rss_net_growth_mib")
    if net_growth is not None and float(net_growth) > float(
        limits["rss_net_growth_max_mib"]
    ):
        rss_ok = False
        failures.append(
            f"rss_net_growth_exceeded: {net_growth} MiB > {limits['rss_net_growth_max_mib']} MiB"
        )
    leaks["rss_bounded"] = rss_ok

    fds_ok = True
    for fd_key in ("daemon_open_fds_max", "host_open_fds_max", "runner_open_fds_max"):
        fd_val = res.get(fd_key)
        if fd_val is not None and int(fd_val) > int(limits["open_fds_max"]):
            fds_ok = False
            failures.append(f"{fd_key}_exceeded: {fd_val} > {limits['open_fds_max']}")
    fd_delta = leaks.get("fd_growth_delta")
    if fd_delta is not None and int(fd_delta) > int(limits["open_fds_growth_max"]):
        fds_ok = False
        failures.append(
            f"fd_growth_leak: delta={fd_delta} > {limits['open_fds_growth_max']}"
        )
    leaks["fds_bounded"] = fds_ok

    disk_ok = float(res["disk_growth_max_mib"]) <= float(limits["disk_growth_max_mib"])
    if not disk_ok:
        failures.append(
            f"disk_growth_exceeded: {res['disk_growth_max_mib']} MiB > "
            f"{limits['disk_growth_max_mib']} MiB"
        )
    leaks["disk_bounded"] = disk_ok

    if not leaks.get("endpoint_closed_after_stop"):
        failures.append("endpoint_still_alive: 停止后 IPC 端点仍可连接")
    if not leaks.get("locks_cleared_after_stop"):
        failures.append("locks_still_present: 停止后 host.lock 或 daemon.lock 未清理")

    orphans = report.get("orphan_pids") or []
    if len(orphans) > int(limits["max_orphan_processes"]):
        failures.append(f"orphan_processes: 检测到残留孤儿进程 {orphans}")

    report["failures"] = failures
    report["verdict"] = "PASS" if not failures else "FAIL"

    r_errors = slo_mod.validate_against_def(defs, "soakReport", report)
    if r_errors:
        raise slo_mod.SloConfigError("浸泡报告违反 slo-v1.json：" + "；".join(r_errors))

    return report, (0 if not failures else 1)


def render_soak_markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    wk = report["workloads"]
    res = report["resources"]
    lk = report["leak_checks"]
    lines = [
        "# 天枢 Agent · PRD §10.6 常驻浸泡运行报告",
        "",
        f"- **v1 诊断门禁（非发布验收）**：**`{report['verdict']}`**",
        "- **G3 发布验收**：`NOT_EVALUATED`；实际观测窗与材料摘要见 `soak-observation.json`。",
        f"- **策略版本**：`slo-thresholds-v{report['policy_version']}`",
        f"- **起止时间（UTC）**：`{report['started_at']}` → `{report['finished_at']}`",
        f"- **运行器时长（含启动、计划重启和退出）**：`{report['actual_duration_s']} s`"
        f"（目标 `{report['requested_duration_s']} s`，"
        f"采样间隔 `{report['sample_interval_s']} s`，"
        f"共 `{report['samples_count']}` 条样本 / `{report['cycles_completed']}` 轮周期）",
        f"- **运行环境**：`{env['os']}` · `{env['arch']}` · `{env['cpu_model']}` · "
        f"Python `{env['python_version']}` · 脱敏 `{env['redaction']}`",
        "",
        "## 1. 混合工作负载完成度",
        "",
        "| 负载项 | 累计完成次数 |",
        "|---|---:|",
    ]
    for k, v in sorted(wk.items()):
        lines.append(f"| `{k}` | {v} |")
    lines.extend(
        [
            "",
            "## 2. 资源水位与泄漏核验",
            "",
            f"- **空闲总常驻内存（Host + Daemon RSS）**：`p50 = {res['idle_rss_p50_mib']} MiB`，"
            f"`p95 = {res['idle_rss_p95_mib']} MiB`，`max = {res['idle_rss_max_mib']} MiB`",
            f"- **活跃总常驻内存（Host + Daemon RSS）**：`p50 = {res['active_rss_p50_mib']} MiB`，"
            f"`p95 = {res['active_rss_p95_mib']} MiB`，`max = {res['active_rss_max_mib']} MiB`",
            f"- **Daemon CPU 占用**：空闲 `p50 = {res['daemon_cpu_idle_p50_pct']}%`，"
            f"活跃 `p95 = {res['daemon_cpu_active_p95_pct']}%`",
            f"- **最大打开文件描述符/句柄数**：Daemon `{res['daemon_open_fds_max']}` · "
            f"Host `{res['host_open_fds_max']}` · Runner `{res['runner_open_fds_max']}`"
            f"（首末增量 `{lk['fd_growth_delta']}`）",
            f"- **磁盘占用**：SQLite `{res['sqlite_final_mib']} MiB` · "
            f"Blobs/Artifacts `{res['blobs_final_mib']} MiB` · "
            f"峰值净增长 `{res['disk_growth_max_mib']} MiB`",
            f"- **内存趋势与泄漏判定**：净增长 `{lk['rss_net_growth_mib']} MiB` · "
            f"回归斜率 `{lk['rss_slope_mib_per_hour']} MiB/h` · "
            f"`rss_bounded={lk['rss_bounded']}`",
            f"- **退出洁净度**：`endpoint_closed={lk['endpoint_closed_after_stop']}` · "
            f"`locks_cleared={lk['locks_cleared_after_stop']}` · "
            f"**残留孤儿进程 `{report['orphan_pids']}`**",
        ]
    )
    if report["failures"]:
        lines.extend(["", "## 3. 失败项明细", ""])
        for item in report["failures"]:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _sqlite_bytes(data_root: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = data_root / f"events.db{suffix}"
        if p.exists():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def run_soak(
    *,
    duration_s: float,
    sample_interval_s: float,
    output_dir: Path,
    thresholds_path: Path = slo_mod.DEFAULT_THRESHOLDS_PATH,
    schema_path: Path = slo_mod.DEFAULT_SCHEMA_PATH,
) -> tuple[dict[str, Any], int]:
    thresholds, thresholds_sha, defs = slo_mod.load_and_validate_thresholds(
        thresholds_path, schema_path=schema_path
    )
    if not 0 < duration_s <= threading.TIMEOUT_MAX:
        raise slo_mod.SloConfigError("浸泡时长须为平台可表示的正秒数")
    if not 0 < sample_interval_s <= threading.TIMEOUT_MAX:
        raise slo_mod.SloConfigError("采样间隔须为平台可表示的正秒数")
    requested_ns = math.ceil(duration_s * obs.NANOSECONDS)
    source_start = obs.capture_source(REPO_ROOT)

    # A fresh leaf is the publication boundary; never truncate a prior run.
    output_dir = Path(output_dir).absolute()
    try:
        output_dir.mkdir(mode=0o700, parents=True)
    except FileExistsError as exc:
        raise slo_mod.SloConfigError("浸泡输出目录必须尚不存在") from exc
    output_dir = output_dir.resolve()
    samples_path = output_dir / "samples.jsonl"
    status_path = output_dir / "status.json"

    temp_base = slo_mod._short_temp_dir("tssoak-")  # noqa: SLF001
    data_root = temp_base / "d"
    ws_dir = temp_base / "ws"
    auth_root = temp_base / "auth"
    lock_root = temp_base / "locks"
    for d in (data_root, ws_dir, auth_root, lock_root):
        d.mkdir(parents=True, exist_ok=True)
    ui_dir = (
        slo_mod.REAL_UI_DIST
        if (slo_mod.REAL_UI_DIST / "index.html").is_file()
        else temp_base / "empty_ui"
    )
    ui_dir.mkdir(parents=True, exist_ok=True)

    env = slo_mod._isolated_env(  # noqa: SLF001
        data_root=data_root,
        authority_root=auth_root,
        lock_root=lock_root,
    )
    tracked_pids = slo_mod.TrackedPids()
    tracker = ProcessResourceTracker()
    samples: list[dict[str, Any]] = []
    sample_lock = threading.Lock()
    phase_box = {"phase": "idle", "cycle": 0}
    workloads: dict[str, int] = {
        "idle_windows": 0,
        "effects_runs": 0,
        "pure_only_runs": 0,
        "file_undos": 0,
        "streamed_turns": 0,
        "memory_ops": 0,
        "subscriber_disconnects": 0,
        "slow_subscriber_drops": 0,
        "cursor_reconnects": 0,
        "pause_resume_cycles": 0,
        "mid_run_cancels": 0,
        "scheduled_runs_fired": 0,
        "graceful_restarts": 0,
        "recovery_scans_verified": 0,
    }
    runtime_failures: list[str] = []

    started_utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_start_ns = time.monotonic_ns()
    workload_start_ns = 0
    excluded_ns = 0
    restart_intervals_ns: list[list[int]] = []
    disk_baseline_bytes: int | None = None

    cluster = _HostCluster(data_root, ui_dir, env, tracked_pids)

    def _elapsed_ns() -> int:
        return time.monotonic_ns() - t_start_ns

    def _workload_ns(now_ns: int) -> int:
        return now_ns - workload_start_ns - excluded_ns

    def _take_sample(explicit_phase: str | None = None, *, first: bool = False) -> int:
        nonlocal disk_baseline_bytes, workload_start_ns
        with sample_lock:
            now_ns = _elapsed_ns()
            if first:
                workload_start_ns = now_ns
            elapsed = round(now_ns / obs.NANOSECONDS, 3)
            cur_phase = explicit_phase or str(phase_box["phase"])
            cur_cycle = int(phase_box["cycle"])

            d_pid, h_pid = cluster.current_pids()
            d_rss, d_cpu, d_fds = tracker.inspect_pid(d_pid)
            h_rss, h_cpu, h_fds = tracker.inspect_pid(h_pid)
            r_fds = _self_open_fds()

            total_rss: float | None = None
            if d_rss is not None and h_rss is not None:
                total_rss = round(d_rss + h_rss, 3)
            elif d_rss is not None:
                total_rss = d_rss
            elif h_rss is not None:
                total_rss = h_rss

            sql_b = _sqlite_bytes(data_root)
            blobs_b = slo_mod._dir_bytes(data_root / "artifacts")  # noqa: SLF001
            root_b = slo_mod._dir_bytes(data_root)  # noqa: SLF001
            if disk_baseline_bytes is None:
                disk_baseline_bytes = root_b
            growth_b = root_b - disk_baseline_bytes

            seq_head: int | None = None
            try:
                with ipc.Client(data_root, timeout=1.0) as c:
                    head = c.welcome.get("event_seq_head")
                    if type(head) is int and head >= 0:
                        seq_head = head
            except (ipc.IpcError, OSError):
                seq_head = None

            sample = {
                "ts_ms": int(time.time() * 1000),
                "elapsed_s": elapsed,
                "phase": cur_phase,
                "cycle": cur_cycle,
                "daemon_pid": d_pid,
                "host_pid": h_pid,
                "daemon_rss_mib": d_rss,
                "host_rss_mib": h_rss,
                "total_rss_mib": total_rss,
                "daemon_cpu_pct": d_cpu,
                "host_cpu_pct": h_cpu,
                "daemon_open_fds": d_fds,
                "host_open_fds": h_fds,
                "runner_open_fds": r_fds,
                "sqlite_mib": round(sql_b / (1024.0 * 1024.0), 4),
                "blobs_mib": round(blobs_b / (1024.0 * 1024.0), 4),
                "data_root_mib": round(root_b / (1024.0 * 1024.0), 4),
                "disk_growth_mib": round(growth_b / (1024.0 * 1024.0), 4),
                "event_seq_head": seq_head,
            }
            s_errs = slo_mod.validate_against_def(defs, "soakSample", sample)
            if s_errs:
                runtime_failures.append("invalid_soak_sample: " + "; ".join(s_errs))
            line = (
                json.dumps(sample, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            fd = os.open(
                str(samples_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                view = memoryview(line)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("sample_write_incomplete")
                    view = view[written:]
            finally:
                os.close(fd)
            samples.append(sample)

            workload_ns = _workload_ns(now_ns)
            live_status = {
                "state": "running",
                "verdict_scope": obs.VERDICT_SCOPE,
                "release_acceptance": obs.RELEASE_ACCEPTANCE,
                "started_at": started_utc,
                "updated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "elapsed_s": elapsed,
                "workload_duration_ns": workload_ns,
                "requested_duration_s": duration_s,
                "progress_pct": round(min(100.0, workload_ns / requested_ns * 100.0), 2),
                "cycle": cur_cycle,
                "samples_count": len(samples),
                "workloads": dict(workloads),
                "latest_sample": sample,
                "failures_so_far": len(runtime_failures),
            }
            atomic_write_bytes(status_path, obs.json_bytes(live_status))
            return now_ns

    sampler_stop = threading.Event()

    def _sampler_loop() -> None:
        while not sampler_stop.wait(sample_interval_s):
            try:
                _take_sample()
            except Exception as exc:  # noqa: BLE001
                runtime_failures.append(f"sampler_exception: {type(exc).__name__}: {exc}")

    def _start_ready() -> None:
        cluster.start()  # Includes a real Host health response.
        with ipc.Client(data_root, timeout=5.0) as client:
            client.call("state", {})  # Authenticated Daemon RPC, not just a lock file.

    sampler_thread: threading.Thread | None = None
    endpoint_closed = False
    locks_cleared = False

    try:
        _start_ready()
        _take_sample("idle", first=True)
        sampler = threading.Thread(
            target=_sampler_loop, name="soak-resource-sampler", daemon=True
        )
        sampler.start()
        sampler_thread = sampler

        # 重启节奏：长跑每 30 分钟优雅重启一次，短跑在中点至少触发一次优雅重启
        restart_interval_s = min(1800.0, max(1.5, duration_s * 0.45))
        restart_interval_ns = math.ceil(restart_interval_s * obs.NANOSECONDS)
        next_restart_at = workload_start_ns + restart_interval_ns
        idle_sleep_s = min(0.15, sample_interval_s * 0.25) if duration_s < 60.0 else 1.5

        cycle = 0
        while _workload_ns(_elapsed_ns()) < requested_ns or cycle == 0:
            phase_box["cycle"] = cycle

            # --- A. Idle 采样窗口 ---
            phase_box["phase"] = "idle"
            time.sleep(idle_sleep_s)
            _take_sample("idle")
            workloads["idle_windows"] += 1

            # --- B. Active 混合工作负载 ---
            phase_box["phase"] = "active"
            info = cluster.info
            assert info is not None

            with ipc.Client(data_root, timeout=10.0) as client:
                # B1: 在线订阅者（接收 W4 流式增量与事件）+ 正常断连
                head_seq = int(client.call("state", {})["event_seq_head"])
                online_stop = threading.Event()
                online_ready = threading.Event()
                stream_seen = threading.Event()
                online_errors: list[str] = []

                def consume_online(cursor, ready, stopped, seen, errors) -> None:
                    # 连接由读线程独占，不能等 effects 完成后才读；Windows
                    # 有界管道可能早在此之前因发送超期而正常断开慢订阅者。
                    try:
                        with ipc.Client(data_root, timeout=5.0) as sub:
                            sub.subscribe(cursor + 1, include_model_stream=True)
                            ready.set()
                            while not stopped.is_set():
                                message = sub.next_event(timeout=0.05)
                                if message is not None and message.get("push") == "model_stream":
                                    seen.set()
                    except Exception as exc:  # noqa: BLE001 - 必须使诊断失败
                        errors.append(f"{type(exc).__name__}: {exc}")
                    finally:
                        ready.set()

                online_reader = threading.Thread(
                    target=consume_online, name="soak-online-subscriber", daemon=True,
                    args=(head_seq, online_ready, online_stop, stream_seen, online_errors),
                )
                online_reader.start()
                try:
                    if not online_ready.wait(5) or online_errors:
                        raise RuntimeError(f"online_subscriber_not_ready:{online_errors}")

                    # B2: Effects 任务（文件写入 + 记忆提案 + W4 流式）+ F11 Undo
                    eff_task = client.call(
                        "task.create",
                        {
                            "goal": f"effects:{cycle}",
                            "workspaces": [{"root_path": str(ws_dir), "mode": "writable"}],
                        },
                    )["task"]
                    eff_run = client.call("run.start", {"task_id": eff_task["task_id"]})["run"]
                    eff_rid = eff_run["run_id"]

                    def _poll_eff(_rid: str = eff_rid) -> str | None:
                        r = client.call("run.status", {"run_id": _rid})["run"]
                        if r["state"] == "waiting_approval":
                            apps = client.call(
                                "approvals.list", {"run_id": _rid}
                            )["approvals"]
                            for ap in apps:
                                client.call(
                                    "approvals.decide",
                                    {"approval_id": ap["approval_id"], "decision": "approve"},
                                )
                            return None
                        return (
                            r["state"]
                            if r["state"] in ("succeeded", "failed", "cancelled")
                            else None
                        )

                    eff_state = slo_mod._wait_condition(_poll_eff, timeout=20.0, interval=0.01)  # noqa: SLF001
                    if eff_state != "succeeded":
                        runtime_failures.append(f"cycle_{cycle}_effects_failed:{eff_state}")
                    else:
                        workloads["effects_runs"] += 1

                    if stream_seen.wait(0.25):
                        workloads["streamed_turns"] += 1
                    else:
                        runtime_failures.append(f"cycle_{cycle}_online_subscriber_missing_stream")
                finally:
                    online_stop.set()
                    online_reader.join(5)
                    if online_reader.is_alive():
                        raise RuntimeError(f"cycle_{cycle}_online_subscriber_not_stopped")
                    runtime_failures.extend(
                        f"cycle_{cycle}_online_subscriber_failed:{error}"
                        for error in online_errors
                    )
                    workloads["subscriber_disconnects"] += 1

                # 执行 F11 撤销，保持工作区干净并验证快照链闭环
                plan = client.call("undo.plan", {"run_id": eff_run["run_id"]})["changes"]
                if plan:
                    undo_res = client.call("undo.apply", {"run_id": eff_run["run_id"]})
                    if undo_res.get("applied"):
                        workloads["file_undos"] += 1

                # B3: F10 记忆显式写入/检索 + pure_only 任务
                if cycle == 0:
                    client.call(
                        "memory.remember",
                        {"kind": "preference", "content": "浸泡周期默认使用精简中文总结"},
                    )
                    workloads["memory_ops"] += 1
                mem_res = client.call("memory.search", {"query": "浸泡周期"})
                if isinstance(mem_res.get("items"), list):
                    workloads["memory_ops"] += 1

                pure_task = client.call(
                    "task.create",
                    {
                        "goal": f"pure:{cycle}",
                        "workspaces": [{"root_path": str(ws_dir), "mode": "readonly"}],
                    },
                )["task"]
                pure_run = client.call(
                    "run.start",
                    {
                        "task_id": pure_task["task_id"],
                        "execution_policy": {"format_version": 1, "mode": "pure_only"},
                    },
                )["run"]
                pure_rid = pure_run["run_id"]
                pure_state = slo_mod._wait_condition(  # noqa: SLF001
                    lambda _rid=pure_rid: (
                        st
                        if (
                            st := client.call("run.status", {"run_id": _rid})["run"]["state"]
                        )
                        in ("succeeded", "failed", "cancelled")
                        else None
                    ),
                    timeout=15.0,
                    interval=0.01,
                )
                if pure_state == "succeeded":
                    workloads["pure_only_runs"] += 1
                else:
                    runtime_failures.append(f"cycle_{cycle}_pure_only_failed:{pure_state}")

                remaining_budget = (requested_ns - _workload_ns(_elapsed_ns())) / obs.NANOSECONDS

                # B4: 慢订阅者断连 + since_seq 游标重连（第 0 轮必做，之后每 3 轮执行）
                if cycle == 0 or (cycle % 3 == 0 and remaining_budget > 1.2):
                    cursor_seq = int(client.call("state", {})["event_seq_head"])
                    drops_before = len(
                        [
                            r
                            for r in metrics_mod.read_metrics(data_root, include_rotated=True)
                            if r.get("metric") == metrics_mod.SLOW_SUBSCRIBER_DROPPED
                        ]
                    )
                    slow_sub = ipc.Client(data_root, timeout=5.0)
                    slow_sub.connect()
                    try:
                        slow_sub.subscribe(0)
                        big_payload = "S" * slo_mod.BIG_EVENT_CHARS
                        for k in range(4):
                            client.call("task.create", {"goal": f"{big_payload}#c{cycle}_{k}"})
                        slo_mod._wait_condition(  # noqa: SLF001
                            lambda _db=drops_before: len(
                                [
                                    r
                                    for r in metrics_mod.read_metrics(
                                        data_root, include_rotated=True
                                    )
                                    if r.get("metric") == metrics_mod.SLOW_SUBSCRIBER_DROPPED
                                ]
                            )
                            > _db,
                            timeout=6.0,
                            interval=0.02,
                        )
                        workloads["slow_subscriber_drops"] += 1
                    finally:
                        slow_sub.close()

                    reconnect_sub = ipc.Client(data_root, timeout=5.0)
                    reconnect_sub.connect()
                    try:
                        head_now = int(client.call("state", {})["event_seq_head"])
                        since_seq = cursor_seq + 1
                        reconnect_sub.subscribe(since_seq)
                        got_seqs: list[int] = []
                        dl = time.monotonic() + 8.0
                        while time.monotonic() < dl and (not got_seqs or got_seqs[-1] < head_now):
                            ev = reconnect_sub.next_event(timeout=0.1)
                            if ev is not None and "event" in ev:
                                got_seqs.append(int(ev["event"]["seq"]))
                        if got_seqs == list(range(since_seq, head_now + 1)):
                            workloads["cursor_reconnects"] += 1
                        else:
                            exp_cnt = head_now - cursor_seq
                            runtime_failures.append(
                                f"cycle_{cycle}_cursor_gap:got={len(got_seqs)},expected={exp_cnt}"
                            )
                    finally:
                        reconnect_sub.close()

                # B5: 运行中途用户 pause -> resume -> cancel（第 0 轮必做，后续每 2 轮执行）
                remaining_budget = (requested_ns - _workload_ns(_elapsed_ns())) / obs.NANOSECONDS
                if cycle == 0 or (cycle % 2 == 0 and remaining_budget > 0.6):
                    tag = f"g{cycle}"
                    ctl_dir = data_root / "soak_ctl"
                    entered1 = ctl_dir / f"{tag}.entered"
                    release1 = ctl_dir / f"{tag}.release"
                    entered2 = ctl_dir / f"{tag}.turn2_entered"
                    release2 = ctl_dir / f"{tag}.turn2_release"

                    g_task = client.call(
                        "task.create",
                        {
                            "goal": f"gated:{tag}",
                            "workspaces": [{"root_path": str(ws_dir), "mode": "readonly"}],
                        },
                    )["task"]
                    g_run = client.call("run.start", {"task_id": g_task["task_id"]})["run"]
                    g_rid = g_run["run_id"]
                    slo_mod._wait_condition(entered1.exists, timeout=10.0, interval=0.01)  # noqa: SLF001

                    # 请求暂停 → 放行第 1 轮 → 确认进入 paused → 请求恢复
                    client.call("run.pause", {"run_id": g_rid})
                    slo_mod._write_bytes_exact(release1, b"1")  # noqa: SLF001
                    slo_mod._wait_condition(  # noqa: SLF001
                        lambda _rid=g_rid: (
                            client.call("run.status", {"run_id": _rid})["run"]["state"]
                            == "paused"
                        ),
                        timeout=10.0,
                        interval=0.01,
                    )
                    client.call("run.resume", {"run_id": g_rid})
                    workloads["pause_resume_cycles"] += 1

                    # 等进入第 2 轮后发起中途取消 → 放行第 2 轮 → 确认进入 cancelled
                    slo_mod._wait_condition(entered2.exists, timeout=10.0, interval=0.01)  # noqa: SLF001
                    client.call("run.cancel", {"run_id": g_rid})
                    slo_mod._write_bytes_exact(release2, b"1")  # noqa: SLF001
                    slo_mod._wait_condition(  # noqa: SLF001
                        lambda _rid=g_rid: (
                            client.call("run.status", {"run_id": _rid})["run"]["state"]
                            == "cancelled"
                        ),
                        timeout=10.0,
                        interval=0.01,
                    )
                    workloads["mid_run_cancels"] += 1

                # B6: W5 调度器定时触发（第 0 轮必做，后续每 4 轮且剩余 >1.5s 时执行）
                remaining_budget = (requested_ns - _workload_ns(_elapsed_ns())) / obs.NANOSECONDS
                if cycle == 0 or (cycle % 4 == 0 and remaining_budget > 1.5):
                    sched = client.call(
                        "schedule.create",
                        {
                            "goal": f"sched:cycle-{cycle}",
                            "interval_seconds": 1,
                            "workspaces": [],
                        },
                    )["schedule"]
                    sched_id = sched["schedule_id"]
                    try:
                        slo_mod._wait_condition(  # noqa: SLF001
                            lambda _sid=sched_id: any(
                                item["schedule_id"] == _sid
                                and item.get("last_fired_at") is not None
                                for item in client.call("schedule.list", {})["schedules"]
                            ),
                            timeout=6.0,
                            interval=0.05,
                        )
                        workloads["scheduled_runs_fired"] += 1
                    finally:
                        client.call("schedule.remove", {"schedule_id": sched_id})

            _take_sample("active")

            # --- C. 周期性优雅重启 Host + Daemon 并核验热启动恢复扫描 ---
            if _elapsed_ns() >= next_restart_at or (
                workloads["graceful_restarts"] == 0 and cycle == 0 and duration_s <= 20.0
            ):
                rec_before = len(
                    [
                        r
                        for r in metrics_mod.read_metrics(data_root, include_rotated=True)
                        if r.get("metric") == metrics_mod.RESTART_RECOVER_MS
                        and r.get("cold_start") is False
                    ]
                )
                # No sampler can straddle the planned stop/start boundary.
                with sample_lock:
                    phase_box["phase"] = "idle"
                    restart_started_ns = _elapsed_ns()
                    runtime_failures.extend(cluster.stop())
                    if ipc.probe(data_root, timeout=0.2):
                        runtime_failures.append(f"cycle_{cycle}_daemon_alive_after_host_stop")
                    _start_ready()
                    restart_finished_ns = _elapsed_ns()
                    restart_intervals_ns.append([restart_started_ns, restart_finished_ns])
                    excluded_ns += restart_finished_ns - restart_started_ns
                    workloads["graceful_restarts"] += 1
                rec_after = len(
                    [
                        r
                        for r in metrics_mod.read_metrics(data_root, include_rotated=True)
                        if r.get("metric") == metrics_mod.RESTART_RECOVER_MS
                        and r.get("cold_start") is False
                    ]
                )
                if rec_after > rec_before:
                    workloads["recovery_scans_verified"] += 1
                else:
                    runtime_failures.append(f"cycle_{cycle}_missing_warm_recovery_scan")
                next_restart_at = _elapsed_ns() + restart_interval_ns

            cycle += 1

        # The journal has exactly one final boundary, taken before shutdown.
        sampler_stop.set()
        sampler.join(timeout=5.0)
        if sampler.is_alive():
            raise RuntimeError("soak_sampler_did_not_stop")
        workload_end_ns = _take_sample("idle")
    finally:
        if not sampler_stop.is_set():
            sampler_stop.set()
            if sampler_thread is not None:
                sampler_thread.join(timeout=5.0)
        runtime_failures.extend(cluster.stop())
        endpoint_closed = not ipc.probe(data_root, timeout=0.2)
        locks_cleared = (
            not host.host_lock_path(data_root).exists()
            and not daemon.lock_path(data_root).exists()
        )
        orphans = slo_mod._find_orphan_pids(tracked_pids, temp_base)  # noqa: SLF001
        if (
            not endpoint_closed or not locks_cleared or orphans
            or (sampler_thread is not None and sampler_thread.is_alive())
        ):
            runtime_failures.append("temporary_soak_root_retained")
        else:
            try:
                shutil.rmtree(temp_base)
            except OSError:
                runtime_failures.append("temporary_soak_root_cleanup_failed")

    actual_duration_s = round(_elapsed_ns() / obs.NANOSECONDS, 3)
    finished_utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    idle_rss = [
        float(s["total_rss_mib"])
        for s in samples
        if s["phase"] == "idle" and isinstance(s.get("total_rss_mib"), (int, float))
    ]
    active_rss = [
        float(s["total_rss_mib"])
        for s in samples
        if s["phase"] == "active" and isinstance(s.get("total_rss_mib"), (int, float))
    ]
    all_rss = [
        float(s["total_rss_mib"])
        for s in samples
        if isinstance(s.get("total_rss_mib"), (int, float))
    ]
    idle_cpu = [
        float(s["daemon_cpu_pct"])
        for s in samples
        if s["phase"] == "idle" and isinstance(s.get("daemon_cpu_pct"), (int, float))
    ]
    active_cpu = [
        float(s["daemon_cpu_pct"])
        for s in samples
        if s["phase"] == "active" and isinstance(s.get("daemon_cpu_pct"), (int, float))
    ]
    d_fds_list = [
        int(s["daemon_open_fds"])
        for s in samples
        if isinstance(s.get("daemon_open_fds"), int)
    ]
    h_fds_list = [
        int(s["host_open_fds"])
        for s in samples
        if isinstance(s.get("host_open_fds"), int)
    ]
    r_fds_list = [
        int(s["runner_open_fds"])
        for s in samples
        if isinstance(s.get("runner_open_fds"), int)
    ]
    disk_growths = [float(s["disk_growth_mib"]) for s in samples]
    seq_heads = [
        int(s["event_seq_head"])
        for s in samples
        if isinstance(s.get("event_seq_head"), int)
    ]

    slope_mib_h = compute_rss_slope_mib_per_hour(samples, min_span_s=MIN_SLOPE_WINDOW_S)
    net_rss_growth = (
        round(all_rss[-1] - all_rss[0], 3) if len(all_rss) >= 2 else None
    )
    fd_delta: int | None = None
    if len(d_fds_list) >= 2:
        fd_delta = d_fds_list[-1] - d_fds_list[0]

    def _r3(val: float | None) -> float | None:
        return None if val is None else round(val, 3)

    raw_report: dict[str, Any] = {
        "schema_version": "soak-report-v1",
        "verdict": "PASS",
        "policy_version": int(thresholds["policy_version"]),
        "started_at": started_utc,
        "finished_at": finished_utc,
        "requested_duration_s": round(requested_ns / obs.NANOSECONDS, 3),
        "actual_duration_s": actual_duration_s,
        "sample_interval_s": round(sample_interval_s, 3),
        "samples_count": len(samples),
        "cycles_completed": max(1, int(phase_box["cycle"]) + 1),
        "environment": slo_mod.collect_hardware_metadata(),
        "workloads": workloads,
        "resources": {
            "idle_rss_p50_mib": _r3(metrics_mod.percentile(idle_rss, 50)),
            "idle_rss_p95_mib": _r3(metrics_mod.percentile(idle_rss, 95)),
            "idle_rss_max_mib": _r3(max(idle_rss) if idle_rss else None),
            "active_rss_p50_mib": _r3(metrics_mod.percentile(active_rss, 50)),
            "active_rss_p95_mib": _r3(metrics_mod.percentile(active_rss, 95)),
            "active_rss_max_mib": _r3(max(active_rss) if active_rss else None),
            "daemon_cpu_idle_p50_pct": _r3(metrics_mod.percentile(idle_cpu, 50)),
            "daemon_cpu_active_p95_pct": _r3(metrics_mod.percentile(active_cpu, 95)),
            "daemon_open_fds_max": max(d_fds_list) if d_fds_list else None,
            "host_open_fds_max": max(h_fds_list) if h_fds_list else None,
            "runner_open_fds_max": max(r_fds_list) if r_fds_list else None,
            "sqlite_final_mib": samples[-1]["sqlite_mib"] if samples else 0.0,
            "blobs_final_mib": samples[-1]["blobs_mib"] if samples else 0.0,
            "disk_growth_final_mib": samples[-1]["disk_growth_mib"] if samples else 0.0,
            "disk_growth_max_mib": round(max(disk_growths) if disk_growths else 0.0, 4),
            "event_seq_head_final": seq_heads[-1] if seq_heads else 0,
        },
        "leak_checks": {
            "rss_slope_mib_per_hour": slope_mib_h,
            "rss_net_growth_mib": net_rss_growth,
            "rss_bounded": True,
            "fd_growth_delta": fd_delta,
            "fds_bounded": True,
            "disk_bounded": True,
            "endpoint_closed_after_stop": endpoint_closed,
            "locks_cleared_after_stop": locks_cleared,
        },
        "orphan_pids": orphans,
        "failures": runtime_failures,
    }

    extra_redact = [str(temp_base), str(output_dir), str(REPO_ROOT), str(Path.home())]
    redacted_report = slo_mod._redact_obj(raw_report, extra_paths=extra_redact)  # noqa: SLF001
    final_report, exit_code = evaluate_soak_gate(
        redacted_report, thresholds, schema_defs=defs
    )

    _publish_completed(
        output_dir,
        final_report,
        window={
            "start_elapsed_ns": workload_start_ns,
            "end_elapsed_ns": workload_end_ns,
            "requested_ns": requested_ns,
        },
        restart_intervals_ns=restart_intervals_ns,
        source_start=source_start,
        source_end=obs.capture_source(REPO_ROOT),
        thresholds_sha256=thresholds_sha,
        schema_defs=defs,
    )
    return final_report, exit_code


def _publish_completed(
    output_dir: Path,
    report: dict[str, Any],
    *,
    window: dict[str, int],
    restart_intervals_ns: list[list[int]],
    source_start: dict[str, Any] | None,
    source_end: dict[str, Any] | None,
    thresholds_sha256: str,
    schema_defs: dict[str, Any],
) -> None:
    report_bytes = obs.json_bytes(report)
    markdown_bytes = render_soak_markdown(report).encode("utf-8")
    observation = obs.build_observation(
        window=window,
        restart_intervals_ns=restart_intervals_ns,
        source_start=source_start,
        source_end=source_end,
        thresholds_sha256=thresholds_sha256,
        policy_version=report["policy_version"],
        report_bytes=report_bytes,
        markdown_bytes=markdown_bytes,
        samples_bytes=(output_dir / "samples.jsonl").read_bytes(),
        schema_defs=schema_defs,
    )
    raw_observation = obs.json_bytes(observation)
    final_status = {
        **obs.completion_fields(report),
        "observation_sha256": obs.sha256_bytes(raw_observation),
    }
    # A live reader may see some artifacts before completion. The marker is last.
    atomic_write_bytes(output_dir / "soak-report.json", report_bytes)
    atomic_write_bytes(output_dir / "soak-report.md", markdown_bytes)
    atomic_write_bytes(output_dir / obs.OBSERVATION_NAME, raw_observation)
    atomic_write_bytes(output_dir / "status.json", obs.json_bytes(final_status))


def _resolve_duration_seconds(args: argparse.Namespace) -> float:
    if args.duration_seconds is not None:
        return float(args.duration_seconds)
    if args.duration_hours is not None:
        return float(args.duration_hours) * 3600.0
    env_sec = os.environ.get("TIANSHU_SOAK_SECONDS")
    if env_sec:
        return float(env_sec)
    env_hrs = os.environ.get("TIANSHU_SOAK_HOURS")
    if env_hrs:
        return float(env_hrs) * 3600.0
    # 默认 10.0 小时（含 TIANSHU_SOAK=1 触发的正式浸泡场景）
    return DEFAULT_DURATION_HOURS * 3600.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 PRD §10.6 常驻浸泡测试（支持 10h 过夜运行与实时 JSONL 检查点）。"
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=None,
        help="浸泡时长（小时；未指定 --duration-seconds 时默认 10.0 小时）",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=None,
        help="浸泡时长（秒；优先级高于 --duration-hours，供快速回归使用）",
    )
    parser.add_argument(
        "--sample-interval-seconds",
        type=float,
        default=DEFAULT_SAMPLE_INTERVAL_S,
        help="资源采样与检查点落盘间隔（秒，默认 30）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / ".omc" / "artifacts" / "soak-latest",
        help="实时检查点与最终报告输出目录（新运行要求目录尚不存在）",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="仅离线核对 --output-dir 的已完成诊断材料，不启动任何服务",
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=slo_mod.DEFAULT_THRESHOLDS_PATH,
        help="冻结阈值配置文件路径",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=slo_mod.DEFAULT_SCHEMA_PATH,
        help="SLO & Soak JSON Schema 路径",
    )
    parser.add_argument("--_child-role", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--data-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_ui-dir", default=None, help=argparse.SUPPRESS)
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

    if args.verify_only:
        try:
            thresholds, digest, defs = slo_mod.load_and_validate_thresholds(
                args.thresholds, schema_path=args.schema
            )
            observation = obs.verify_observation(
                args.output_dir, thresholds_sha256=digest,
                policy_version=thresholds["policy_version"], schema_defs=defs,
            )
        except (OSError, ValueError, OverflowError, slo_mod.SloConfigError) as exc:
            print(f"[soak-verify] 诊断材料核对失败：{exc}", file=sys.stderr)
            return 1
        print(obs.json_bytes(observation).decode("utf-8"), end="", flush=True)
        return 0

    try:
        duration_s = _resolve_duration_seconds(args)
        report, exit_code = run_soak(
            duration_s=duration_s,
            sample_interval_s=args.sample_interval_seconds,
            output_dir=args.output_dir,
            thresholds_path=args.thresholds,
            schema_path=args.schema,
        )
    except slo_mod.SloConfigError as exc:
        print(f"[soak-gate] 配置或契约错误：{exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "verdict_scope": obs.VERDICT_SCOPE,
                "release_acceptance": obs.RELEASE_ACCEPTANCE,
                "actual_duration_s": report["actual_duration_s"],
                "cycles_completed": report["cycles_completed"],
                "samples_count": report["samples_count"],
                "workloads": report["workloads"],
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
