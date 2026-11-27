"""PRD §10.6 本地运行质量 SLO 门禁与常驻浸泡框架验收测试（G3-B10 / G3-B11）。

覆盖：
1. `schemas/slo/slo-v1.json` 与 `slo-thresholds-v1.json` 字段级契约与代码侧冻结值双向绑定；
2. 严格门禁负控（缺指标、样本不足、未知样本、`p95`/`max` 超限、未升版放宽阈值、安全不变量违背、
   浸泡内存斜率/FD/磁盘/孤儿进程泄漏均必须 fail-closed 非零拒绝）；
3. 真实子进程快速端到端闭环：`scripts/measure_slo.py --repeats 3 --smoke` +
   `scripts/run_soak.py --duration-seconds 6 --sample-interval-seconds 1`，断言 PASS、
   `orphan_pids == []`、D7 本机路径脱敏生效且在预算时间内完成。
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import threading
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import measure_slo as slo_mod  # noqa: E402
import run_soak as soak_mod  # noqa: E402
import soak_observation as obs  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "schemas" / "slo" / "slo-v1.json"
THRESHOLDS_PATH = REPO_ROOT / "schemas" / "slo" / "slo-thresholds-v1.json"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 测试进程描述符噪声")
def test_online_subscriber_probe_isolates_runner_fds(tmp_path, monkeypatch):
    import resource

    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 2048:
        pytest.skip("需要容纳测试进程与诊断器各自的描述符")
    with ExitStack() as handles:
        for _ in range(520):
            handles.callback(os.close, os.open(os.devnull, os.O_RDONLY))
        test_soak_online_subscriber_consumes_before_run_completion(tmp_path, monkeypatch, None)


@pytest.mark.parametrize("fault", [None, "disconnect", "missing_stream"])
def test_soak_online_subscriber_consumes_before_run_completion(tmp_path, monkeypatch, fault):
    """真实 Host/Daemon 的模型流必须在 effects 轮次完成之前被消费。"""
    # runner 资源门禁必须测独立诊断进程，不能把此前 pytest 用例的资源算进去。
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "import json,sys\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "sys.path.insert(0,sys.argv[1])\n"
            "from test_slo_and_soak import _assert_online_subscriber_progress\n"
            "with pytest.MonkeyPatch.context() as patch:\n"
            "    _assert_online_subscriber_progress(\n"
            "        Path(sys.argv[2]),patch,json.loads(sys.argv[3]))\n",
            str(REPO_ROOT / "tests"), str(tmp_path), json.dumps(fault),
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        close_fds=True, timeout=90,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr


def _assert_online_subscriber_progress(tmp_path, monkeypatch, fault):
    first_stream = threading.Event()
    original_call = soak_mod.ipc.Client.call
    original_next_event = soak_mod.ipc.Client.next_event
    first_run = True

    def call(client, method, params=None, **kwargs):
        nonlocal first_run
        response = original_call(client, method, params, **kwargs)
        if method == "run.start" and first_run:
            first_run = False
            assert first_stream.wait(5), "在线订阅者在等待 Run 完成时没有消费真实模型流"
        return response

    def next_event(client, **kwargs):
        message = original_next_event(client, **kwargs)
        if message is not None and message.get("push") == "model_stream":
            first_stream.set()
            if fault == "disconnect":
                client.close()
                return original_next_event(client, **kwargs)
            if fault == "missing_stream":
                return None
        return message

    monkeypatch.setattr(soak_mod.ipc.Client, "call", call)
    monkeypatch.setattr(soak_mod.ipc.Client, "next_event", next_event)
    report, code = soak_mod.run_soak(
        duration_s=0.01, sample_interval_s=1.0, output_dir=tmp_path / "observation",
    )
    assert first_stream.is_set()
    assert report["orphan_pids"] == []
    if fault is None:
        assert code == 0 and report["failures"] == [], report["failures"]
        assert report["workloads"]["streamed_turns"] > 0
        assert report["workloads"]["cursor_reconnects"] > 0
    else:
        assert code == 1 and report["verdict"] == "FAIL"
        assert any("online_subscriber" in failure for failure in report["failures"])


def _dummy_valid_slo_report(thresholds: dict, sha: str) -> dict:
    metrics = {}
    for name in slo_mod.REQUIRED_SLO_METRICS:
        cfg = thresholds["metrics"][name]
        metrics[name] = {
            "unit": "ms",
            "repeats": 10,
            "unknown_count": 0,
            "min_ms": 5.0,
            "p50_ms": 10.0,
            "p95_ms": round(float(cfg["p95_max"]) * 0.5, 3),
            "max_ms": round(float(cfg["p95_max"]) * 0.6, 3),
            "threshold_p95_ms": float(cfg["p95_max"]),
            "threshold_max_ms": float(cfg["max_ceiling"]),
            "min_repeats_required": 10,
            "passed": True,
        }
    return {
        "schema_version": "slo-report-v1",
        "verdict": "PASS",
        "gate_mode": "release",
        "policy_version": int(thresholds["policy_version"]),
        "thresholds_sha256": sha,
        "measured_at": "2026-11-18T00:00:00Z",
        "environment": {
            "os": "Darwin 25.6.0",
            "arch": "arm64",
            "cpu_model": "Apple M4",
            "memory_gib": 16.0,
            "python_version": "3.12.13",
            "redaction": "d7_local_redacted",
        },
        "dataset": {
            "cold_tasks": 0,
            "cold_events": 0,
            "warm_tasks": 50,
            "warm_runs": 50,
            "warm_events": 550,
            "warm_db_bytes": 102400,
            "warm_artifacts_bytes": 20480,
            "daemon_internal_cold_p50_ms": 5.2,
            "daemon_internal_warm_p50_ms": 2.4,
        },
        "metrics": metrics,
        "slow_subscriber": {
            "ipc_non_reading_attached": True,
            "sse_non_reading_attached": True,
            "dropped_count": 1,
            "cursor_reconnect_verified": True,
            "reconnected_events_count": 25,
        },
        "in_flight_effects": {
            "slow_model_runs": 5,
            "slow_tool_runs": 5,
            "in_flight_hold_target_ms": 40.0,
            "in_flight_applied_effects": 5,
            "in_flight_receipts_verified": 5,
            "post_cancel_dispatched_count": 0,
            "ack_before_inflight_release_all": True,
            "post_release_halt_p50_ms": 8.0,
            "post_release_halt_p95_ms": 15.0,
        },
        "recovery_audit": {
            "scenarios_tested": list(slo_mod.CRASH_SCENARIOS),
            "total_crash_runs": 10,
            "resumed_to_succeeded": 8,
            "held_in_reconciling": 2,
            "blind_retries": 0,
            "lost_receipts": 0,
            "leaked_budget_reservations": 0,
            "preserved_unsettled_reservations": 2,
            "daemon_internal_recover_p95_ms": 1.2,
        },
        "orphan_pids": [],
        "failures": [],
    }


@pytest.mark.parametrize("slow_startup", [False, True])
@pytest.mark.parametrize("broken_stderr", [False, True], ids=["stderr-ok", "stderr-pipe-closed"])
def test_slo_startup_diagnostic_write_failure_preserves_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    slow_startup: bool, broken_stderr: bool,
) -> None:
    thresholds, sha, _ = slo_mod.load_and_validate_thresholds()
    fixture = _dummy_valid_slo_report(thresholds, sha)
    cold_ms = thresholds["metrics"]["startup_cold_ms"]["max_ceiling"] + 1 if slow_startup else 5.0
    monkeypatch.setattr(slo_mod, "collect_hardware_metadata", lambda: fixture["environment"])
    monkeypatch.setattr(
        slo_mod, "run_suite1_startup",
        lambda *args, **kwargs: ([cold_ms], [5.0], fixture["dataset"]),
    )
    monkeypatch.setattr(
        slo_mod, "run_suite2_cancel_and_approval",
        lambda *args, **kwargs: (
            [5.0], [5.0], [5.0], fixture["slow_subscriber"], fixture["in_flight_effects"],
        ),
    )
    monkeypatch.setattr(
        slo_mod, "run_suite3_crash_recovery",
        lambda *args, **kwargs: ([5.0], fixture["recovery_audit"]),
    )
    # A real file where the writer requires a directory fails on all platforms.
    collision = tmp_path / "not-a-directory"
    collision.write_bytes(b"keep")
    samples = tmp_path / "samples.json"
    gate_json = tmp_path / "gate.json"
    gate_md = tmp_path / "gate.md"

    with ExitStack() as stack:
        if broken_stderr:
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            # Unbuffered writes surface the real pipe error at the diagnostic call.
            pipe = stack.enter_context(io.TextIOWrapper(
                os.fdopen(write_fd, "wb", buffering=0), write_through=True,
            ))
            stack.enter_context(monkeypatch.context()).setattr(sys, "stderr", pipe)
        code = slo_mod.main([
            "--smoke", "--repeats", "1", "--output-startup-phases-json",
            str(collision / "phases.json"), "--output-samples-json", str(samples),
            "--output-json", str(gate_json), "--output-md", str(gate_md),
        ])

    captured = capsys.readouterr()
    assert code == 1
    if not broken_stderr:
        assert "startup_phases_write_failed" in captured.err
    assert str(tmp_path) not in captured.err
    assert collision.read_bytes() == b"keep"
    report = json.loads(gate_json.read_text(encoding="utf-8"))
    verdict = "FAIL" if slow_startup else "PASS"
    assert report["verdict"] == json.loads(captured.out)["verdict"] == verdict
    assert gate_md.read_text(encoding="utf-8") == slo_mod.render_markdown_report(report)
    raw = json.loads(samples.read_text(encoding="utf-8"))["samples"]
    assert set(raw) == set(slo_mod.REQUIRED_SLO_METRICS)
    assert raw["startup_cold_ms"] == [cold_ms]
    assert raw["approval_rpc_ms"] == raw["restart_recovery_ms"] == [5.0]
    assert bool(report["failures"]) is slow_startup


def test_slo_schema_and_frozen_thresholds_binding() -> None:
    thresholds, sha, defs = slo_mod.load_and_validate_thresholds(
        THRESHOLDS_PATH, schema_path=SCHEMA_PATH
    )
    assert len(sha) == 64
    assert set(defs) == {"thresholdsConfig", "sloReport", "soakSample", "soakReport"}
    assert set(thresholds["metrics"]) == set(slo_mod.REQUIRED_SLO_METRICS)

    # 双向绑定：JSON 文件中的冻结数值必须与代码侧 FROZEN_V1_* 精确一致，改一处忘另一处必须红
    assert thresholds["policy_version"] == slo_mod.FROZEN_POLICY_VERSION
    assert thresholds["min_repeats"] == slo_mod.FROZEN_MIN_REPEATS
    for name, code_limits in slo_mod.FROZEN_V1_METRIC_LIMITS.items():
        json_limits = thresholds["metrics"][name]
        assert float(json_limits["p95_max"]) == float(code_limits["p95_max"]), name
        assert float(json_limits["max_ceiling"]) == float(code_limits["max_ceiling"]), name
        assert int(json_limits["min_repeats"]) == int(code_limits["min_repeats"]), name

    for key, expected_val in slo_mod.FROZEN_V1_SOAK_LIMITS.items():
        assert float(thresholds["soak_limits"][key]) == float(expected_val), key


def test_slo_gate_negative_controls_reject_missing_exceeded_and_relaxed(
    tmp_path: Path,
) -> None:
    thresholds, sha, defs = slo_mod.load_and_validate_thresholds()
    baseline = _dummy_valid_slo_report(thresholds, sha)
    ok_report, ok_code = slo_mod.evaluate_slo_gate(baseline, thresholds, schema_defs=defs)
    assert ok_code == 0 and ok_report["verdict"] == "PASS"

    # 负控 1：缺失任意必需指标必须失败
    missing_rep = copy.deepcopy(baseline)
    del missing_rep["metrics"]["cancel_ack_ms"]
    evaluated, code = slo_mod.evaluate_slo_gate(missing_rep, thresholds, schema_defs=defs)
    assert code == 1 and evaluated["verdict"] == "FAIL"
    assert any("missing_metric" in f for f in evaluated["failures"])

    # 负控 2：正式发布模式下样本数 < min_repeats 或存在 unknown 样本必须失败
    few_rep = copy.deepcopy(baseline)
    few_rep["metrics"]["startup_cold_ms"]["repeats"] = 3
    evaluated, code = slo_mod.evaluate_slo_gate(few_rep, thresholds, smoke=False, schema_defs=defs)
    assert code == 1 and evaluated["verdict"] == "FAIL"
    assert any("insufficient_samples" in f for f in evaluated["failures"])

    unk_rep = copy.deepcopy(baseline)
    unk_rep["metrics"]["approval_rpc_ms"]["unknown_count"] = 1
    evaluated, code = slo_mod.evaluate_slo_gate(unk_rep, thresholds, schema_defs=defs)
    assert code == 1 and evaluated["verdict"] == "FAIL"
    assert any("unknown_samples" in f for f in evaluated["failures"])

    # 负控 3：p95 超出冻结阈值必须失败
    slow_rep = copy.deepcopy(baseline)
    slow_rep["metrics"]["cancel_stop_dispatch_ms"]["p95_ms"] = (
        float(thresholds["metrics"]["cancel_stop_dispatch_ms"]["p95_max"]) + 0.1
    )
    evaluated, code = slo_mod.evaluate_slo_gate(slow_rep, thresholds, schema_defs=defs)
    assert code == 1 and evaluated["verdict"] == "FAIL"
    assert any("p95_exceeded" in f for f in evaluated["failures"])

    # 负控 4：未升版擅自放宽阈值或减少 min_repeats 必须抛 SloConfigError (exit 2)
    relaxed = copy.deepcopy(thresholds)
    relaxed["metrics"]["startup_warm_ms"]["p95_max"] = (
        float(thresholds["metrics"]["startup_warm_ms"]["p95_max"]) + 10.0
    )
    with pytest.raises(slo_mod.SloConfigError, match="threshold_relaxed_without_version_bump"):
        slo_mod.evaluate_slo_gate(baseline, relaxed, schema_defs=defs)

    relaxed_repeats = copy.deepcopy(thresholds)
    relaxed_repeats["min_repeats"] = 5
    with pytest.raises(slo_mod.SloConfigError, match="threshold_relaxed_without_version_bump"):
        slo_mod.evaluate_slo_gate(baseline, relaxed_repeats, schema_defs=defs)

    # CLI 层面对放宽阈值文件必须返回退出码 2
    bad_thresholds_file = tmp_path / "relaxed-thresholds.json"
    bad_thresholds_file.write_text(json.dumps(relaxed), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "measure_slo.py"),
            "--repeats",
            "1",
            "--smoke",
            "--thresholds",
            str(bad_thresholds_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15.0,
        check=False,
    )
    assert proc.returncode == 2
    assert "threshold_relaxed_without_version_bump" in proc.stderr

    # 负控 5：升了 policy_version 但未同步升 schema_version 必须拒绝
    mismatched_ver = copy.deepcopy(thresholds)
    mismatched_ver["policy_version"] = 2
    with pytest.raises(slo_mod.SloConfigError, match="schema_version"):
        slo_mod.evaluate_slo_gate(baseline, mismatched_ver, schema_defs=defs)

    # 负控 6：盲目重试 / 丢失回执 / 预算泄漏 / 取消后新派发 / 孤儿进程任一非零必须失败
    for field_path, bad_val, tag in [
        (("recovery_audit", "blind_retries"), 1, "blind_retries"),
        (("recovery_audit", "lost_receipts"), 1, "lost_receipts"),
        (("recovery_audit", "leaked_budget_reservations"), 1, "leaked_budget_reservations"),
        (("in_flight_effects", "post_cancel_dispatched_count"), 1, "post_cancel_dispatch"),
        (("orphan_pids",), [424242], "orphan_processes"),
    ]:
        broken = copy.deepcopy(baseline)
        if len(field_path) == 1:
            broken[field_path[0]] = bad_val
        else:
            broken[field_path[0]][field_path[1]] = bad_val
        evaluated, code = slo_mod.evaluate_slo_gate(broken, thresholds, schema_defs=defs)
        assert code == 1 and evaluated["verdict"] == "FAIL", tag
        assert any(tag in f for f in evaluated["failures"]), tag


def test_soak_slope_and_leak_gate_negative_controls() -> None:
    thresholds, _, defs = slo_mod.load_and_validate_thresholds()
    # 10 小时线性泄漏序列（每小时涨 50 MiB > 32 MiB/h 上限）
    leaking_samples = [
        {"elapsed_s": h * 3600.0, "total_rss_mib": 40.0 + h * 50.0}
        for h in range(11)
    ]
    slope = soak_mod.compute_rss_slope_mib_per_hour(leaking_samples)
    assert slope is not None and abs(slope - 50.0) < 0.1

    soak_rep = {
        "schema_version": "soak-report-v1",
        "verdict": "PASS",
        "policy_version": 1,
        "started_at": "2026-11-18T00:00:00Z",
        "finished_at": "2026-11-18T10:00:00Z",
        "requested_duration_s": 36000.0,
        "actual_duration_s": 36000.0,
        "sample_interval_s": 30.0,
        "samples_count": 1200,
        "cycles_completed": 500,
        "environment": {
            "os": "Darwin",
            "arch": "arm64",
            "cpu_model": "Apple M4",
            "memory_gib": 16.0,
            "python_version": "3.12.13",
            "redaction": "d7_local_redacted",
        },
        "workloads": {
            "idle_windows": 500,
            "effects_runs": 500,
            "pure_only_runs": 500,
            "file_undos": 500,
            "streamed_turns": 500,
            "memory_ops": 500,
            "subscriber_disconnects": 500,
            "slow_subscriber_drops": 160,
            "cursor_reconnects": 160,
            "pause_resume_cycles": 250,
            "mid_run_cancels": 250,
            "scheduled_runs_fired": 125,
            "graceful_restarts": 20,
            "recovery_scans_verified": 20,
        },
        "resources": {
            "idle_rss_p50_mib": 42.0,
            "idle_rss_p95_mib": 45.0,
            "idle_rss_max_mib": 48.0,
            "active_rss_p50_mib": 60.0,
            "active_rss_p95_mib": 90.0,
            "active_rss_max_mib": 110.0,
            "daemon_cpu_idle_p50_pct": 1.0,
            "daemon_cpu_active_p95_pct": 35.0,
            "daemon_open_fds_max": 32,
            "host_open_fds_max": 24,
            "runner_open_fds_max": 16,
            "sqlite_final_mib": 12.5,
            "blobs_final_mib": 4.2,
            "disk_growth_final_mib": 16.7,
            "disk_growth_max_mib": 18.0,
            "event_seq_head_final": 15000,
        },
        "leak_checks": {
            "rss_slope_mib_per_hour": slope,
            "rss_net_growth_mib": 500.0,
            "rss_bounded": True,
            "fd_growth_delta": 80,
            "fds_bounded": True,
            "disk_bounded": True,
            "endpoint_closed_after_stop": True,
            "locks_cleared_after_stop": True,
        },
        "orphan_pids": [],
        "failures": [],
    }
    evaluated, code = soak_mod.evaluate_soak_gate(soak_rep, thresholds, schema_defs=defs)
    assert code == 1 and evaluated["verdict"] == "FAIL"
    assert evaluated["leak_checks"]["rss_bounded"] is False
    assert evaluated["leak_checks"]["fds_bounded"] is False
    assert any("rss_slope_leak" in f for f in evaluated["failures"])
    assert any("fd_growth_leak" in f for f in evaluated["failures"])


def test_soak_workload_clock_rejects_rounded_up_ten_hours() -> None:
    # Arithmetic fixture only: both spans round to 36000.000 s on disk.
    ten_hours_ns = 36_000_000_000_000
    start = 902_000_000
    stopped, ready = start + 500_000_000, start + 1_250_000_000
    window = {
        "start_elapsed_ns": start,
        "end_elapsed_ns": start + ten_hours_ns + ready - stopped,
        "requested_ns": ten_hours_ns,
    }
    assert obs.measure_workload_window(window, [[stopped, ready]]) == (ten_hours_ns, True)
    short = {**window, "end_elapsed_ns": window["end_elapsed_ns"] - 1}
    assert obs.measure_workload_window(short, [[stopped, ready]]) == (ten_hours_ns - 1, False)
    longer_request = {**window, "requested_ns": ten_hours_ns + 1}
    assert obs.measure_workload_window(longer_request, [[stopped, ready]]) == (ten_hours_ns, False)
    assert obs.measure_workload_window(window, []) == (ten_hours_ns + ready - stopped, True)
    for intervals in ([[stopped, ready], [ready - 1, ready + 1]], [[start - 1, ready]],
                      [[stopped, stopped]], [[stopped, window["end_elapsed_ns"] + 1]]):
        with pytest.raises(ValueError, match="invalid_restart_intervals"):
            obs.measure_workload_window(window, intervals)
    with pytest.raises(ValueError, match="invalid_workload_window"):
        obs.measure_workload_window({**window, "requested_ns": True}, [])


def _run_child(
    command: list[str],
    env: dict[str, str],
    *,
    timeout: float,
    label: str,
    timeouts: dict[str, bool],
) -> subprocess.CompletedProcess[str]:
    timeouts[label] = False
    try:
        return subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )

    except subprocess.TimeoutExpired as exc:
        timeouts[label] = True

        def _text(value: str | bytes | None) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value or ""

        # 124 preserves a distinct timeout outcome for assertions and artifacts.
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
        )


def _publish_slo_diagnostics(
    root: Path | None,
    *,
    files: tuple[Path, ...],
    processes: dict[str, subprocess.CompletedProcess[str]],
    context: dict[str, object],
) -> None:
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    for source in files:
        if source.is_file():
            shutil.copyfile(source, root / source.name)
    for label, process in processes.items():
        for stream in ("stdout", "stderr"):
            text = slo_mod.redact_diagnostic_text(getattr(process, stream) or "")
            (root / f"{label}-{stream}.txt").write_text(text, encoding="utf-8")
    payload = {
        **context,
        "returncodes": {label: process.returncode for label, process in processes.items()},
    }
    (root / "run-context.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_measure_slo_smoke_and_run_soak_real_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    defs = slo_mod.load_slo_schema_defs()
    thresholds, _, _ = slo_mod.load_and_validate_thresholds()
    slo_json = tmp_path / "slo-smoke.json"
    slo_md = tmp_path / "slo-smoke.md"
    slo_samples = tmp_path / "slo-samples.json"
    startup_phases = tmp_path / "slo-startup-phases.json"
    soak_dir = tmp_path / "soak-out"
    diagnostics_value = os.environ.get("TIANSHU_SLO_DIAGNOSTICS_DIR")
    diagnostics = Path(diagnostics_value) if diagnostics_value else None
    started_at = dt.datetime.now(dt.UTC).isoformat()
    timeouts: dict[str, bool] = {}

    def _run_slo() -> subprocess.CompletedProcess[str]:
        command, env = slo_mod._native_python_child(  # noqa: SLF001
            [
                sys.executable,
                str(SCRIPTS_DIR / "measure_slo.py"),
                "--repeats",
                "3",
                "--smoke",
                "--output-json",
                str(slo_json),
                "--output-md",
                str(slo_md),
                "--output-samples-json",
                str(slo_samples),
                "--output-startup-phases-json",
                str(startup_phases),
            ],
            env=dict(os.environ),
        )
        return _run_child(
            command,
            env,
            timeout=120.0,
            label="slo",
            timeouts=timeouts,
        )

    def _run_soak() -> subprocess.CompletedProcess[str]:
        command, env = slo_mod._native_python_child(  # noqa: SLF001
            [
                sys.executable,
                str(SCRIPTS_DIR / "run_soak.py"),
                "--duration-seconds",
                "6",
                "--sample-interval-seconds",
                "1",
                "--output-dir",
                str(soak_dir),
            ],
            env=dict(os.environ),
        )
        return _run_child(
            command,
            env,
            timeout=120.0,
            label="soak",
            timeouts=timeouts,
        )

    # 顺序执行两个真实子进程套件，避免 2-vCPU CI 浸泡多进程负载干扰 SLO 毫秒级启动计时
    slo_proc = _run_slo()
    soak_proc = _run_soak()
    _publish_slo_diagnostics(
        diagnostics,
        files=(
            slo_json,
            slo_md,
            slo_samples,
            startup_phases,
            soak_dir / "soak-report.json",
            soak_dir / "samples.jsonl",
            soak_dir / "status.json",
            soak_dir / "soak-report.md",
            soak_dir / "soak-observation.json",
        ),
        processes={"slo": slo_proc, "soak": soak_proc},
        context={
            "started_at_utc": started_at,
            "finished_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "cpu_count": os.cpu_count(),
            "platform": slo_mod.platform.platform(),
            "pytest_worker": os.environ.get("PYTEST_XDIST_WORKER"),
            "ci_shard_index": os.environ.get("CI_SHARD_INDEX"),
            "timeouts": timeouts,
        },
    )

    assert slo_proc.returncode == 0, f"measure_slo 失败：{slo_proc.stdout}\n{slo_proc.stderr}"
    slo_report = json.loads(slo_json.read_text(encoding="utf-8"))
    assert slo_mod.validate_against_def(defs, "sloReport", slo_report) == []
    assert slo_report["verdict"] == "PASS"
    assert slo_report["orphan_pids"] == []
    assert slo_report["failures"] == []
    for name in slo_mod.REQUIRED_SLO_METRICS:
        m = slo_report["metrics"][name]
        assert m["repeats"] >= 3
        assert m["p50_ms"] > 0 and m["p95_ms"] > 0
        assert m["passed"] is True
    samples_report = json.loads(slo_samples.read_text(encoding="utf-8"))
    assert samples_report["schema_version"] == "slo-samples-v1"
    assert set(samples_report["samples"]) == set(slo_mod.REQUIRED_SLO_METRICS)
    phases = json.loads(startup_phases.read_text(encoding="utf-8"))
    assert set(phases) == {"schema_version", "cold", "warm"}
    assert phases["schema_version"] == "slo-startup-phases-v1"
    boundaries = ("process_spawned_ms", "host_ready_ms", "state_ready_ms", "task_created_ms")
    for kind in ("cold", "warm"):
        totals = samples_report["samples"][f"startup_{kind}_ms"]
        assert len(phases[kind]) == len(totals)
        for sample, total in zip(phases[kind], totals, strict=True):
            assert set(sample) == {*boundaries, "daemon_internal_ready_ms"}
            offsets = [sample[name] for name in boundaries]
            assert all(type(value) in (int, float) and math.isfinite(value) for value in offsets)
            assert 0 <= offsets[0] <= offsets[1] <= offsets[2] <= offsets[3] == total
            internal = sample["daemon_internal_ready_ms"]
            assert internal is None or (type(internal) in (int, float) and math.isfinite(internal)
                                        and internal >= 0)
    if diagnostics is not None:
        assert (diagnostics / startup_phases.name).read_bytes() == startup_phases.read_bytes()
        context = json.loads((diagnostics / "run-context.json").read_text(encoding="utf-8"))
        assert context["ci_shard_index"] == os.environ.get("CI_SHARD_INDEX")
    for name, samples in samples_report["samples"].items():
        metric = slo_report["metrics"][name]
        config = thresholds["metrics"][name]
        assert len(samples) == metric["repeats"] + metric["unknown_count"]
        assert slo_mod._summarize_samples(  # noqa: SLF001
            samples,
            p95_max=float(config["p95_max"]),
            max_ceiling=float(config["max_ceiling"]),
            min_repeats_required=3,
        ) == metric
    assert set(slo_report["recovery_audit"]["scenarios_tested"]) == set(
        slo_mod.CRASH_SCENARIOS
    )
    assert slo_report["recovery_audit"]["blind_retries"] == 0
    assert slo_report["recovery_audit"]["lost_receipts"] == 0
    assert slo_report["recovery_audit"]["leaked_budget_reservations"] == 0
    md_text = slo_md.read_text(encoding="utf-8")
    assert str(Path.home()) not in slo_json.read_text(encoding="utf-8")
    assert str(Path.home()) not in md_text

    assert soak_proc.returncode == 0, f"run_soak 失败：{soak_proc.stdout}\n{soak_proc.stderr}"

    soak_report_path = soak_dir / "soak-report.json"
    samples_path = soak_dir / "samples.jsonl"
    status_path = soak_dir / "status.json"
    soak_md_path = soak_dir / "soak-report.md"
    for p in (soak_report_path, samples_path, status_path, soak_md_path):
        assert p.is_file(), f"缺少浸泡产物：{p}"

    soak_report = json.loads(soak_report_path.read_text(encoding="utf-8"))
    assert slo_mod.validate_against_def(defs, "soakReport", soak_report) == []
    assert soak_report["verdict"] == "PASS"
    assert soak_report["orphan_pids"] == []
    assert soak_report["failures"] == []

    sample_lines = [
        json.loads(line)
        for line in samples_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(sample_lines) >= 3
    for s in sample_lines:
        assert slo_mod.validate_against_def(defs, "soakSample", s) == []

    wk = soak_report["workloads"]
    for key in (
        "idle_windows",
        "effects_runs",
        "pure_only_runs",
        "file_undos",
        "streamed_turns",
        "memory_ops",
        "subscriber_disconnects",
        "slow_subscriber_drops",
        "cursor_reconnects",
        "pause_resume_cycles",
        "mid_run_cancels",
        "scheduled_runs_fired",
        "graceful_restarts",
        "recovery_scans_verified",
    ):
        assert wk[key] >= 1, f"浸泡工作负载未覆盖 {key}: {wk}"

    status_doc = json.loads(status_path.read_bytes())
    console_doc = json.loads(soak_proc.stdout)
    assert status_doc["state"] == "completed"
    for report in (status_doc, console_doc):
        assert report["verdict"] == "PASS"
        assert report["verdict_scope"] == "soak_v1_diagnostic"
        assert report["release_acceptance"] == "NOT_EVALUATED"
    assert "`NOT_EVALUATED`" in soak_md_path.read_text(encoding="utf-8")
    thresholds, thresholds_sha, _ = slo_mod.load_and_validate_thresholds()
    observation_path = soak_dir / "soak-observation.json"
    observation = json.loads(observation_path.read_bytes())
    window = observation["window"]
    start, end = window["start_elapsed_ns"], window["end_elapsed_ns"]
    restarts = observation["restart_intervals_ns"]
    assert len(restarts) == wk["graceful_restarts"] >= 1
    assert all(start <= down < up <= end for down, up in restarts)
    assert all(prev_up <= next_down for (_, prev_up), (next_down, _) in pairwise(restarts))
    observed_ns = end - start - sum(up - down for down, up in restarts)
    assert observed_ns == observation["workload_duration_ns"]
    assert window["requested_ns"] == 6_000_000_000
    assert 6_000_000_000 <= observed_ns < end - start < 36_000_000_000_000
    assert start > 0
    assert sample_lines[0]["elapsed_s"] == round(start / 1e9, 3)
    assert sample_lines[-1]["elapsed_s"] == round(end / 1e9, 3)
    assert soak_report["actual_duration_s"] >= sample_lines[-1]["elapsed_s"]
    assert observation["duration_requirement_met"] is False
    assert observation["release_acceptance"] == "NOT_EVALUATED"
    assert "minimum_workload_duration_unmet" in observation["issues"]
    assert observation["thresholds_sha256"] == thresholds_sha
    assert observation["samples"]["count"] == len(sample_lines) == soak_report["samples_count"]
    for phase in ("idle", "active"):
        phase_rows = [row for row in sample_lines if row["phase"] == phase]
        assert observation["samples"][f"{phase}_rows"] == len(phase_rows) > 0
        assert observation["samples"][f"ready_{phase}_rows"] == sum(
            row["host_pid"] is not None and row["daemon_pid"] is not None
            and row["event_seq_head"] is not None for row in phase_rows
        ) > 0
    observation_digest = hashlib.sha256(observation_path.read_bytes()).hexdigest()
    assert status_doc["observation_sha256"] == observation_digest

    runner = [sys.executable, str(SCRIPTS_DIR / "run_soak.py"), "--output-dir", str(soak_dir)]
    originals = {p: p.read_bytes() for p in soak_dir.iterdir()}
    verification = subprocess.run(
        [*runner, "--verify-only"], capture_output=True, text=True, encoding="utf-8", timeout=15.0,
    )
    assert verification.returncode == 0, verification.stderr
    assert json.loads(verification.stdout) == observation
    # Real CLI refusal cannot truncate a prior completed bundle.
    duplicate = subprocess.run(
        [*runner, "--duration-seconds", "6"],
        capture_output=True, text=True, encoding="utf-8", timeout=30.0,
    )
    assert duplicate.returncode == 2 and "输出目录必须尚不存在" in duplicate.stderr
    assert {p: p.read_bytes() for p in soak_dir.iterdir()} == originals

    def verify(directory: Path) -> dict:
        return obs.verify_observation(
            directory, thresholds_sha256=thresholds_sha,
            policy_version=thresholds["policy_version"], schema_defs=defs,
        )

    sample_rows = originals[samples_path].splitlines(keepends=True)
    nonfinite_row = json.dumps({**sample_lines[0], "elapsed_s": float("nan")}).encode() + b"\n"
    for path, changed, error in (
        (status_path, obs.json_bytes({**status_doc, "release_acceptance": "PASS"}),
         "observation_status_mismatch"),
        (status_path, obs.json_bytes({**status_doc, "progress_pct": 99.0}),
         "observation_status_mismatch"),
        (status_path, obs.json_bytes({**status_doc, "state": "running"}),
         "observation_not_completed"),
        (observation_path, originals[observation_path] + b"\n", "observation_checksum_mismatch"),
        (soak_report_path, originals[soak_report_path] + b"\n", "observation_material_mismatch"),
        (soak_md_path, originals[soak_md_path] + b"\n", "observation_material_mismatch"),
        (samples_path, b" " + originals[samples_path], "observation_material_mismatch"),
        (samples_path, originals[samples_path][:-1], "incomplete_sample_journal"),
        (samples_path, b"".join(sample_rows[:-1]), "sample_count_mismatch"),
        (samples_path, b"{}\n" + b"".join(sample_rows[1:]), "invalid_soak_sample"),
        (samples_path, nonfinite_row + b"".join(sample_rows[1:]), "invalid_soak_sample"),
        (samples_path, b"{" + b"".join(sample_rows), "Expecting property name"),
        (soak_report_path, obs.json_bytes({**soak_report, "verdict": "FAIL"}),
         "observation_verdict_mismatch"),
    ):
        path.write_bytes(changed)
        try:
            with pytest.raises(ValueError, match=error):
                verify(soak_dir)
        finally:
            path.write_bytes(originals[path])
    tighter_thresholds = tmp_path / "tighter-thresholds.json"
    thresholds["soak_limits"]["open_fds_max"] -= 1
    tighter_thresholds.write_bytes(obs.json_bytes(thresholds))
    mismatch = subprocess.run(
        [*runner, "--verify-only", "--thresholds", str(tighter_thresholds)],
        capture_output=True, text=True, encoding="utf-8", timeout=15.0,
    )
    assert mismatch.returncode == 1 and "observation_thresholds_mismatch" in mismatch.stderr

    # Inject a real publication I/O error on the observation write. The other
    # final material writes happen, but readiness for offline verification does not.
    partial = tmp_path / "partial-publication"
    partial.mkdir()
    (partial / samples_path.name).write_bytes(originals[samples_path])
    (partial / status_path.name).write_bytes(b'{"state":"running"}\n')
    real_write = soak_mod.atomic_write_bytes

    def fail_observation_write(path: Path, data: bytes) -> None:
        if path.name == observation_path.name:
            raise OSError("injected observation write failure")
        real_write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(soak_mod, "atomic_write_bytes", fail_observation_write)
        with pytest.raises(OSError, match="injected observation write failure"):
            soak_mod._publish_completed(
                partial, soak_report, window=window, restart_intervals_ns=restarts,
                source_start=observation["source_start"], source_end=observation["source_end"],
                thresholds_sha256=thresholds_sha, schema_defs=defs,
            )
    assert (partial / soak_report_path.name).read_bytes() == originals[soak_report_path]
    assert (partial / soak_md_path.name).read_bytes() == originals[soak_md_path]
    assert not (partial / observation_path.name).exists()
    assert json.loads((partial / status_path.name).read_bytes())["state"] == "running"
    with pytest.raises(ValueError, match="observation_not_completed"):
        verify(partial)
    assert verify(soak_dir) == observation

    for dropped in (observation_path, samples_path, soak_report_path, soak_md_path):
        dropped.unlink()
        try:
            with pytest.raises(FileNotFoundError):
                verify(soak_dir)
        finally:
            dropped.write_bytes(originals[dropped])
    for contents in originals.values():
        assert str(Path.home()).encode() not in contents


def test_slo_diagnostics_preserve_raw_samples_and_redact_streams(tmp_path):
    source = tmp_path / "slo-samples.json"
    payload = b'{"samples":{"startup_cold_ms":[1.0,2.0]}}\n'
    source.write_bytes(payload)
    diagnostics = tmp_path / "diagnostics"
    process = subprocess.CompletedProcess(
        args=[], returncode=7,
        stdout=f"failed under {Path.home()}/private", stderr="slo stderr",
    )

    _publish_slo_diagnostics(
        diagnostics,
        files=(source, tmp_path / "missing.json"),
        processes={"slo": process},
        context={"ci_shard_index": "3"},
    )

    assert (diagnostics / source.name).read_bytes() == payload
    assert not (diagnostics / "missing.json").exists()
    stdout = (diagnostics / "slo-stdout.txt").read_text(encoding="utf-8")
    assert str(Path.home()) not in stdout
    assert "PATH_REDACTED" in stdout
    assert (diagnostics / "slo-stderr.txt").read_text(encoding="utf-8") == "slo stderr"
    context = json.loads((diagnostics / "run-context.json").read_text(encoding="utf-8"))
    assert context == {"ci_shard_index": "3", "returncodes": {"slo": 7}}


def test_slo_timeout_is_published_as_failure_diagnostics(tmp_path):
    timeouts: dict[str, bool] = {}
    process = _run_child(
        [
            sys.executable,
            "-c",
            "import sys, threading; "
            "print('before timeout', flush=True); "
            "print('timeout stderr', file=sys.stderr, flush=True); "
            "threading.Event().wait()",
        ],
        env=dict(os.environ),
        timeout=2.0,
        label="slo",
        timeouts=timeouts,
    )
    assert process.returncode == 124
    assert timeouts == {"slo": True}

    diagnostics = tmp_path / "diagnostics"
    _publish_slo_diagnostics(
        diagnostics,
        files=(),
        processes={"slo": process},
        context={"timeouts": timeouts},
    )
    context = json.loads((diagnostics / "run-context.json").read_text(encoding="utf-8"))
    assert context == {
        "timeouts": {"slo": True},
        "returncodes": {"slo": 124},
    }
    assert (diagnostics / "slo-stdout.txt").is_file()
    assert (diagnostics / "slo-stderr.txt").is_file()
