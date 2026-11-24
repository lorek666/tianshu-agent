"""G3-B10 开发测量；不运行正式 SLO 门禁。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from threading import Lock
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import measure_slo as slo  # noqa: E402

STATE_PRESSURE_REPORT = "slo-state-pressure.json"


def _write_state_pressure_report(report: dict[str, Any], tmp_path: Path) -> str:
    """Persist the complete redacted report before any diagnostic predicate can fail."""
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    runner_temp = os.environ.get("RUNNER_TEMP")
    output_dir = Path(runner_temp) if runner_temp else tmp_path
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / STATE_PRESSURE_REPORT
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(rendered + "\n", encoding="utf-8")
    temporary.replace(output)
    return rendered


def test_state_diagnostic_uses_live_pressure_in_real_children(tmp_path: Path) -> None:
    report, exit_code = slo.run_state_pressure_diagnostic(repeats=2)
    rendered = _write_state_pressure_report(report, tmp_path)
    assert str(Path.home()) not in rendered, rendered
    assert exit_code == 0, rendered
    assert report["schema_version"] == "slo-state-pressure-dev-v1", rendered
    assert report["scope"] == "diagnostic_pre_threshold", rendered
    assert "verdict" not in report, rendered
    assert "state_rpc_ms" not in slo.REQUIRED_SLO_METRICS, rendered
    assert report["orphan_process_count"] == 0, rendered
    assert report["slow_subscriber"]["cursor_reconnect_verified"], rendered
    trials = report["trials"]
    assert {trial["trial"] for trial in trials} == {"sm0", "st0", "ov0"}, rendered
    summary = report["state_rpc_ms"]
    assert summary["attempted_count"] == summary["qualified_count"] == len(trials), rendered
    assert summary["invalid_count"] == 0, rendered
    assert 0 < summary["p50_ms"] <= summary["p95_ms"] <= summary["max_ms"], rendered
    for trial in trials:
        assert trial["invalid_reason"] is None, rendered
        assert trial["latency_ms"] > 0, rendered
        assert 0 <= trial["dispatch_offset_ms"] <= trial["latency_ms"], rendered
        pressure = trial["pressure"]
        assert pressure["drop_reason"] == "send_stall", rendered
        assert pressure["same_live_send_at_dispatch"] is True, rendered
        assert pressure["sent_frames_at_drop"] == pressure["sent_frames_at_dispatch"] >= 2, rendered
        assert pressure["ready_offset_ms"] <= 0, rendered
        assert pressure["drop_offset_ms"] > trial["dispatch_offset_ms"], rendered
        assert pressure["send_age_at_ready_ms"] >= 10, rendered
        assert pressure["send_age_at_drop_ms"] > slo.SUITE2_SEND_STALL_S * 1000, rendered
    print(rendered)  # `pytest -s` 可直接留存脱敏的开发证据。


def test_state_after_same_sender_watchdog_drop_is_not_a_sample(monkeypatch) -> None:
    measure = slo._measure_state_rpc

    def state_after_drop(control, pressure_dir: Path, tag: str) -> tuple[int, int | None]:
        if tag == "sm0":
            # 真子进程、正常认证、同一非读取客户端；等真正断开后才发同一 state 请求。
            slo._wait_condition((pressure_dir / f"{tag}.json").is_file, timeout=5.0)
        return measure(control, pressure_dir, tag)

    monkeypatch.setattr(slo, "_measure_state_rpc", state_after_drop)
    report, exit_code = slo.run_state_pressure_diagnostic(repeats=2)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    assert str(Path.home()) not in rendered, rendered
    assert exit_code == 1, rendered
    assert report["orphan_process_count"] == 0, rendered
    delayed = next(trial for trial in report["trials"] if trial["trial"] == "sm0")
    pressure = delayed["pressure"]
    assert delayed["latency_ms"] > 0, rendered  # 不是由于 state 本身调用失败才排除样本。
    assert pressure["drop_reason"] == "send_stall", rendered
    assert pressure["drop_offset_ms"] < 0, rendered
    assert delayed["invalid_reason"] == "no_live_pressure_at_dispatch", rendered
    summary = report["state_rpc_ms"]
    assert summary["invalid_reasons"] == {"no_live_pressure_at_dispatch": 1}, rendered
    qualified = [t["latency_ms"] for t in report["trials"] if t["trial"] != "sm0"]
    assert summary["qualified_count"] == len(qualified), rendered
    assert summary["p50_ms"] == min(qualified), rendered
    assert summary["p95_ms"] == summary["max_ms"] == max(qualified), rendered


@pytest.mark.parametrize("runner_temp_set", [False, True])
def test_synthetic_failure_survives_cancellation_with_complete_json(
    runner_temp_set: bool, tmp_path: Path
) -> None:
    """Hard-exit after the first failed predicate; no pytest log is needed to recover evidence."""
    runner_temp = tmp_path / "runner-temp"
    child_env = dict(os.environ)
    if runner_temp_set:
        child_env["RUNNER_TEMP"] = str(runner_temp)
    else:
        child_env.pop("RUNNER_TEMP", None)
    child = dedent(
        """
        import importlib.util
        import json
        import os
        import sys
        from pathlib import Path

        spec = importlib.util.spec_from_file_location("state_pressure_test", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = {
            "schema_version": "slo-state-pressure-dev-v1",
            "scope": "diagnostic_pre_threshold",
            "environment": {"redaction": "d7_local_redacted"},
            "slow_subscriber": {"cursor_reconnect_verified": False},
            "state_rpc_ms": {"qualified_count": 0, "invalid_count": 1},
            "trials": [{"trial": "sm0", "invalid_reason": "synthetic_failure"}],
            "orphan_process_count": 0,
        }
        module.slo.run_state_pressure_diagnostic = lambda **kwargs: (report, 1)
        try:
            module.test_state_diagnostic_uses_live_pressure_in_real_children(Path(sys.argv[2]))
        except AssertionError as error:
            assert json.loads(str(error)) == report
            sys.stdout.write("truncated before pytest can render the assertion")
            sys.stdout.flush()
            os._exit(73)
        os._exit(74)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", child, str(Path(__file__).resolve()), str(tmp_path)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    assert result.stdout == "truncated before pytest can render the assertion"
    output_dir = runner_temp if runner_temp_set else tmp_path
    output = output_dir / STATE_PRESSURE_REPORT
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "slo-state-pressure-dev-v1"
    assert persisted["state_rpc_ms"] == {"qualified_count": 0, "invalid_count": 1}
    assert persisted["trials"] == [{"trial": "sm0", "invalid_reason": "synthetic_failure"}]
    assert persisted["slow_subscriber"]["cursor_reconnect_verified"] is False
    assert persisted["orphan_process_count"] == 0


@pytest.mark.parametrize("lock_root_set", [False, True])
def test_state_diagnostic_refuses_without_effective_test_lock(lock_root_set, monkeypatch, tmp_path):
    if lock_root_set:
        monkeypatch.setenv("TIANSHU_TEST_LOCK_ROOT", str(tmp_path / "not-the-actual-lock-root"))
    else:
        monkeypatch.delenv("TIANSHU_TEST_LOCK_ROOT")
    with pytest.raises(slo.SloConfigError, match="pytest|测试锁根未生效"):
        slo.run_state_pressure_diagnostic()


_RPC_TIMING = (20_000_100, 20_000_300)


def test_state_rpc_timing_distinguishes_events_within_one_native_tick(monkeypatch, tmp_path):
    class Control:
        def call(self, method, params):
            assert (method, params) == ("state", {})
            return {"event_seq_head": 1}

    # 模拟 Windows 3.12：一个粗粒度 tick 内可完成 RPC，不同的 QPC 读数仍能区分先后。
    fine_ticks = iter(_RPC_TIMING)
    monkeypatch.setattr(slo, "time", SimpleNamespace(
        monotonic=lambda: 100.0,
        monotonic_ns=lambda: 100_000_000_000,
        perf_counter_ns=lambda: next(fine_ticks),
    ))
    (tmp_path / "sm0.ready").touch()

    assert slo._measure_state_rpc(Control(), tmp_path, "sm0") == _RPC_TIMING


def test_state_pressure_send_age_stays_in_watchdog_clock_domain(monkeypatch):
    monkeypatch.setattr(slo, "time", SimpleNamespace(
        monotonic=lambda: 100.125,
        monotonic_ns=lambda: 100_125_000_000,
        perf_counter_ns=lambda: 4_000_000_000,
    ))
    conn = SimpleNamespace(
        _lock=Lock(), _sending_since=100.0, subscribed=True, closed=False, sent_frames=2,
    )
    sending = slo._active_send(conn)
    assert sending is not None
    assert sending.observed_ns == 4_000_000_000
    assert sending.age_ns == 125_000_000


@pytest.mark.parametrize(
    ("observation_delta", "timing", "reason"),
    [
        (None, _RPC_TIMING, "missing_pressure_observation"),
        ({}, (None, None), "missing_rpc_timing"),
        ({"dispatch_ns": None}, _RPC_TIMING, "missing_state_dispatch"),
        ({"dispatch_ns": 20_000_099}, _RPC_TIMING, "dispatch_outside_rpc"),
        ({"trial": "sm1"}, _RPC_TIMING, "wrong_pressure_trial"),
        ({"same_live_send_at_dispatch": False}, _RPC_TIMING, "no_live_pressure_at_dispatch"),
        ({"drop_ns": 20_000_150}, _RPC_TIMING, "no_live_pressure_at_dispatch"),
        ({"drop_ns": _RPC_TIMING[0]}, _RPC_TIMING, "no_live_pressure_at_dispatch"),
        ({"send_age_at_ready_ns": None}, _RPC_TIMING, "no_live_pressure_at_dispatch"),
        ({"send_age_at_ready_ns": 9_999_999}, _RPC_TIMING, "no_live_pressure_at_dispatch"),
        ({"drop_reason": "closed"}, _RPC_TIMING, "no_same_sender_watchdog_drop"),
        ({"send_age_at_drop_ns": None}, _RPC_TIMING, "no_same_sender_watchdog_drop"),
        ({"send_age_at_drop_ns": 350_000_000}, _RPC_TIMING, "no_same_sender_watchdog_drop"),
        ({"sent_frames_at_drop": 3}, _RPC_TIMING, "no_same_sender_watchdog_drop"),
        ({"drop_ns": None}, _RPC_TIMING, "no_same_sender_watchdog_drop"),
    ],
)
def test_state_trial_rejects_unqualified_measurements(
    observation_delta: dict[str, Any] | None, timing: tuple[int | None, int | None], reason: str
) -> None:
    observation = {
        "trial": "sm0",
        "ready_ns": 15_000_000,
        "send_age_at_ready_ns": 15_000_000,
        "send_age_at_drop_ns": 360_000_000,
        "dispatch_ns": 20_000_200,
        "drop_ns": 360_000_000,
        "same_live_send_at_dispatch": True,
        "sent_frames_at_dispatch": 2,
        "sent_frames_at_drop": 2,
        "drop_reason": "send_stall",
    }
    valid = slo._state_trial("sm0", *_RPC_TIMING, observation)
    assert valid["invalid_reason"] is None, valid
    modified = None if observation_delta is None else {**observation, **observation_delta}
    trial = slo._state_trial("sm0", *timing, modified)
    assert trial["invalid_reason"] == reason
    report = slo._state_sample_summary([trial])
    assert report["qualified_count"] == 0
    assert report["invalid_count"] == 1
    assert report["p50_ms"] is report["p95_ms"] is report["max_ms"] is None
