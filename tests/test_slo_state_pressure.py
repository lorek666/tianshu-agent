"""G3-B10 开发测量；不运行正式 SLO 门禁。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import measure_slo as slo  # noqa: E402


def test_state_diagnostic_uses_live_pressure_in_real_children() -> None:
    report, exit_code = slo.run_state_pressure_diagnostic(repeats=2)
    assert exit_code == 0, report
    assert report["schema_version"] == "slo-state-pressure-dev-v1"
    assert report["scope"] == "diagnostic_pre_threshold"
    assert "verdict" not in report
    assert "state_rpc_ms" not in slo.REQUIRED_SLO_METRICS
    assert report["orphan_process_count"] == 0
    assert report["slow_subscriber"]["cursor_reconnect_verified"]
    trials = report["trials"]
    assert {trial["trial"] for trial in trials} == {"sm0", "st0", "ov0"}
    summary = report["state_rpc_ms"]
    assert summary["attempted_count"] == summary["qualified_count"] == len(trials)
    assert summary["invalid_count"] == 0
    assert 0 < summary["p50_ms"] <= summary["p95_ms"] <= summary["max_ms"]
    for trial in trials:
        assert trial["invalid_reason"] is None, trial
        assert trial["latency_ms"] > 0
        assert 0 <= trial["dispatch_offset_ms"] <= trial["latency_ms"]
        pressure = trial["pressure"]
        assert pressure["drop_reason"] == "send_stall"
        assert pressure["same_live_send_at_dispatch"] is True
        assert pressure["sent_frames_at_drop"] == pressure["sent_frames_at_dispatch"] >= 2
        assert pressure["ready_offset_ms"] <= 0
        assert pressure["drop_offset_ms"] > trial["dispatch_offset_ms"]
        assert pressure["send_age_at_ready_ms"] >= 10
        assert pressure["send_age_at_drop_ms"] > slo.SUITE2_SEND_STALL_S * 1000
    rendered = json.dumps(report, sort_keys=True, ensure_ascii=False)
    assert str(Path.home()) not in rendered
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
    assert exit_code == 1, report
    assert report["orphan_process_count"] == 0
    delayed = next(trial for trial in report["trials"] if trial["trial"] == "sm0")
    pressure = delayed["pressure"]
    assert delayed["latency_ms"] > 0  # 不是由于 state 本身调用失败才排除样本。
    assert pressure["drop_reason"] == "send_stall"
    assert pressure["drop_offset_ms"] < 0
    assert delayed["invalid_reason"] == "no_live_pressure_at_dispatch"
    summary = report["state_rpc_ms"]
    assert summary["invalid_reasons"] == {"no_live_pressure_at_dispatch": 1}
    qualified = [t["latency_ms"] for t in report["trials"] if t["trial"] != "sm0"]
    assert summary["qualified_count"] == len(qualified)
    assert summary["p50_ms"] == min(qualified)
    assert summary["p95_ms"] == summary["max_ms"] == max(qualified)


def test_state_measurement_uses_existing_backlog_before_another_large_write(monkeypatch) -> None:
    call = slo._LabelledSuite2Client.call

    def delayed_write(client, method, params=None, **kwargs):
        goal = (params or {}).get("goal", "")
        if method == "task.create" and goal.startswith("P" * slo.BIG_EVENT_CHARS):
            # The subscription already replays large history. A separately
            # delayed write must not sit between that pressure and its state RPC.
            tag = goal.rsplit(":", 1)[-1]
            pressure_dir = client.data_root / slo._STATE_PRESSURE_DIR
            slo._wait_condition((pressure_dir / f"{tag}.json").is_file, timeout=5.0)
        return call(client, method, params, **kwargs)

    monkeypatch.setattr(slo._LabelledSuite2Client, "call", delayed_write)
    report, code = slo.run_state_pressure_diagnostic(repeats=2)
    assert code == 0, report
    assert {trial["trial"] for trial in report["trials"]} == {"sm0", "st0", "ov0"}
    assert all(trial["invalid_reason"] is None for trial in report["trials"])


def test_state_guard_accepts_filesystem_aliases_of_the_effective_lock_root(
    monkeypatch, tmp_path,
):
    lock_root = tmp_path / "locks"
    alias = tmp_path / "windows-path-alias"
    lock_root.mkdir()
    alias.mkdir()
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_state_guard_alias")
    monkeypatch.setenv("TIANSHU_TEST_LOCK_ROOT", str(alias))
    monkeypatch.setattr(slo.maintenance, "_lock_root", lambda: lock_root)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "samefile", lambda self, other: True)
        slo._require_state_pressure_test_isolation(lock_root)


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
