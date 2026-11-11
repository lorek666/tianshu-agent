"""PRD §10.6 有界浸泡测试：默认跳过，只有显式开启才跑。

为什么默认跳：CI 的时间预算要留给判据测试；浸泡是"发布候选前在本机跑一次"
的能力验证，不是每次提交都跑。开启方式：

    TIANSHU_SOAK=1 TIANSHU_SOAK_SECONDS=180 \\
      python -m pytest -o addopts="" -q tests/test_soak.py

覆盖：反复 建任务/跑完、订阅/断连/带游标重连、取消、重启 daemon；
结束时报告资源趋势（rss/cpu/disk 的 p50/p95/最大值）与残留检查（端点、锁、
daemon 线程、子进程）。默认 3 分钟，上限由环境变量控制——测试自己不会超时。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tianshu import daemon, ipc
from tianshu import metrics as metrics_mod
from tianshu.model import ModelIdentity, ModelResponse, ScriptedModel

pytestmark = pytest.mark.skipif(
    os.environ.get("TIANSHU_SOAK") != "1",
    reason="浸泡测试默认跳过：TIANSHU_SOAK=1 才跑（有界、按 TIANSHU_SOAK_SECONDS 秒）",
)

DEFAULT_SOAK_SECONDS = 180.0


def _soak_seconds() -> float:
    raw = os.environ.get("TIANSHU_SOAK_SECONDS")
    if not raw:
        return DEFAULT_SOAK_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SOAK_SECONDS
    return max(5.0, value)


def _short_data_root(tmp_path: Path) -> tuple[Path, bool]:
    candidate = tmp_path / "data"
    try:
        ipc.endpoint_path(candidate)
        return candidate, True
    except ipc.ProtocolError:
        return Path(tempfile.mkdtemp(prefix="tssoak-", dir=tempfile.gettempdir())), False


class StepModel:
    """按目标分派：`slow` 目标先睡一会儿（给取消留窗口），其余立刻结束。"""

    def __init__(self, *, slow_s: float = 0.4) -> None:
        self.slow_s = slow_s

    def complete(self, messages, tools):  # noqa: ANN001, ANN201
        goal = next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "user"),
            "",
        )
        if goal.startswith("slow"):
            time.sleep(self.slow_s)
        return ModelResponse(finish_reason="end_turn", content="ok")

    def capability_profile(self):  # noqa: ANN201
        return ScriptedModel([]).capability_profile()

    def price_identity(self):  # noqa: ANN201
        return ModelIdentity(adapter="test", vendor=None, model="step")


class Harness:
    def __init__(self, root: Path, model) -> None:
        self.root = root
        self.model = model
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run, name="test-soak-daemon", daemon=True
        )

    def _run(self) -> None:
        try:
            daemon.serve(
                self.root,
                model=self.model,
                driver_interval_s=0.02,
                on_ready=self.ready.set,
                stop_event=self.stop,
            )
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)
        finally:
            self.ready.set()

    def start(self, timeout: float = 20.0) -> Harness:
        self.thread.start()
        assert self.ready.wait(timeout), "daemon 没有就绪"
        if self.errors:
            raise self.errors[0]
        return self

    def stop_and_wait(self, timeout: float = 10.0) -> bool:
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _connect(root: Path, *, timeout: float = 10.0) -> ipc.Client:
    client = ipc.Client(root, timeout=timeout)
    client.connect()
    return client


def _wait(predicate, *, timeout: float = 20.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时")


def _wait_terminal(client: ipc.Client, run_id: str, *, timeout: float = 20.0) -> str:
    def done():
        state = client.call("run.status", {"run_id": run_id})["run"]["state"]
        return state if state in {"succeeded", "failed", "cancelled", "fenced"} else None

    return str(_wait(done, timeout=timeout))


def _one_cycle(root: Path, index: int) -> None:
    """一轮用户动作：跑一个任务、订阅断连、带游标重连、取消一个慢 Run。"""
    client = _connect(root)
    try:
        task = client.call("task.create", {"goal": f"soak-{index}"})["task"]
        run = client.call("run.start", {"task_id": task["task_id"]})["run"]
        assert _wait_terminal(client, run["run_id"]) == "succeeded"

        subscriber = _connect(root)
        try:
            subscriber.subscribe(0)
            for _ in range(3):
                if subscriber.next_event(timeout=0.1) is not None:
                    break
            cursor = subscriber.welcome["event_seq_head"]
        finally:
            subscriber.close()
        resumed = _connect(root)
        try:
            # 从游标补读：留几条重叠的窗口，断言"确实有事件从游标之后补来"。
            since = max(0, cursor - 3)
            assert resumed.subscribe(since)["from_seq"] == since
            _wait(
                lambda: resumed.next_event(timeout=0.05) is not None,
                timeout=5.0,
            )
        finally:
            resumed.close()

        slow_task = client.call("task.create", {"goal": f"slow-{index}"})["task"]
        slow_run = client.call("run.start", {"task_id": slow_task["task_id"]})["run"]
        _wait(
            lambda: client.call("run.status", {"run_id": slow_run["run_id"]})["run"][
                "state"
            ]
            in {"running", "waiting_approval"},
            timeout=10.0,
        )
        client.call("run.cancel", {"run_id": slow_run["run_id"]})
        _wait_terminal(client, slow_run["run_id"])
    finally:
        client.close()


def test_bounded_soak_reports_trend_and_leaves_no_residue(tmp_path) -> None:
    duration = _soak_seconds()
    started = time.monotonic()
    root, inside_tmp = _short_data_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    model = StepModel()
    harness = Harness(root, model).start()
    restarts = 0
    cycles = 0
    try:
        while time.monotonic() - started < duration:
            _one_cycle(root, cycles)
            cycles += 1
            elapsed = time.monotonic() - started
            # 浸泡中段重启一次 daemon（验证指标/事件在重启后继续，而不是重来）。
            if restarts == 0 and elapsed > duration / 2:
                assert harness.stop_and_wait()
                harness = Harness(root, model).start()
                restarts += 1
    finally:
        stopped = harness.stop_and_wait()

    records = metrics_mod.read_metrics(root)
    summary = metrics_mod.summarize(records)
    rss = metrics_mod.metric_values(records, metrics_mod.RSS_MIB)
    disk = metrics_mod.metric_values(records, metrics_mod.DISK_GROWTH)
    elapsed = time.monotonic() - started

    residue_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("tianshu-daemon-")
        or thread.name.startswith("tianshu-metrics")
    ]
    report = {
        "duration_s": round(elapsed, 2),
        "requested_s": duration,
        "cycles": cycles,
        "restarts": restarts,
        "stopped_cleanly": stopped,
        "metrics_records": len(records),
        "rss_mib": {
            "p50": summary.get(metrics_mod.RSS_MIB, {}).get("p50"),
            "p95": summary.get(metrics_mod.RSS_MIB, {}).get("p95"),
            "max": summary.get(metrics_mod.RSS_MIB, {}).get("max"),
            "last": rss[-1] if rss else None,
        },
        "cpu_pct_p95": summary.get(metrics_mod.CPU_PCT, {}).get("p95"),
        "disk_growth_bytes": {
            "min": min(disk) if disk else None,
            "max": max(disk) if disk else None,
            "last": disk[-1] if disk else None,
        },
        "event_seq_head_last": (
            metrics_mod.metric_values(records, metrics_mod.EVENT_SEQ_HEAD)[-1:]
            or [None]
        )[0],
        "slow_subscriber_dropped": len(
            [r for r in records if r.get("metric") == metrics_mod.SLOW_SUBSCRIBER_DROPPED]
        ),
        "residue_threads": residue_threads,
        "endpoint_alive_after_exit": ipc.probe(root, timeout=0.2),
        "lock_present_after_exit": daemon.lock_path(root).exists(),
        "errors": [f"{type(e).__name__}: {e}" for e in harness.errors],
    }
    print("[soak] " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)

    # 残留与泄漏的硬判据（有界，不追求"绝对不涨"）。
    assert stopped, "daemon 没有在超时内退出"
    assert not residue_threads, f"daemon 线程残留：{residue_threads}"
    assert not report["endpoint_alive_after_exit"], "退出后端点仍可连"
    assert not report["lock_present_after_exit"], "退出后锁文件仍在"
    assert cycles >= 1
    assert len(records) > 0, "浸泡期间没有任何指标落盘"
    if rss:
        assert max(rss) < 512.0, f"RSS 超过 512MiB，疑似泄漏：max={max(rss):.1f}MiB"
    if disk:
        assert max(disk) < 256 * 1024 * 1024, (
            f"数据根增长超过 256MiB，疑似泄漏：max={max(disk)}"
        )
    assert summary.get(metrics_mod.STARTUP_READY_MS, {}).get("count", 0) >= 2, (
        "重启后 startup_ready_ms 应至少两条（首次 + 重启）"
    )

    if not inside_tmp:
        shutil.rmtree(root, ignore_errors=True)