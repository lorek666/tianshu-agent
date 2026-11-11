"""PRD §10.6 本地运行指标：采样、读取、聚合与"不许写假数"。

这组测试只关心一件事：**指标是可被程序读取和断言的数据，不是一行行日志**。
因此每个采样点都要能读回来，缺数据必须是 `null`（unknown），而不是 0；
冷/热启动、取消的两个时间点、重启恢复、资源采样都必须来自真实运行。

负控（PRD 要求"把采样写空 / 谎填 0 必须红"）对应两条锚点：
- `test_unknown_is_null_not_zero`：unknown 被写成 0 就红；
- `test_startup_ready_ms_is_positive`：真实启动耗时被写 0 就红。
"""

from __future__ import annotations

import ast
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

# --------------------------------------------------------------------------- 夹具


def _short_data_root(tmp_path: Path) -> tuple[Path, bool]:
    """unix socket 路径上限 100 字节，macOS 上 pytest tmp_path 通常会超。"""
    candidate = tmp_path / "data"
    try:
        ipc.endpoint_path(candidate)
        return candidate, True
    except ipc.ProtocolError:
        return Path(tempfile.mkdtemp(prefix="tsm-", dir=tempfile.gettempdir())), False


@pytest.fixture()
def root(tmp_path):
    path, inside_tmp = _short_data_root(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    if not inside_tmp:
        shutil.rmtree(path, ignore_errors=True)


class Harness:
    """在子线程里跑真 daemon。指标测试需要真实启动/取消/恢复，不是裸函数。"""

    def __init__(self, root: Path, model) -> None:
        self.root = root
        self.model = model
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run, name="test-metrics-daemon", daemon=True
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
        except BaseException as exc:  # noqa: BLE001 - 启动失败要带回断言
            self.errors.append(exc)
        finally:
            self.ready.set()

    def start(self, timeout: float = 15.0) -> Harness:
        self.thread.start()
        assert self.ready.wait(timeout), "daemon 没有就绪"
        if self.errors:
            raise self.errors[0]
        return self

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


class GatedModel:
    """第一次 complete() 阻塞到放行：让"取消发生在模型调用期间"可控。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools):  # noqa: ANN001, ANN201
        self.entered.set()
        self.release.wait(30.0)
        return ModelResponse(finish_reason="end_turn", content="放行后完成")

    def capability_profile(self):  # noqa: ANN201
        return ScriptedModel([]).capability_profile()

    def price_identity(self):  # noqa: ANN201
        return ModelIdentity(adapter="test", vendor=None, model="gated")


def _connect(root: Path, *, timeout: float = 5.0) -> ipc.Client:
    client = ipc.Client(root, timeout=timeout)
    client.connect()
    return client


def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时")


def _records_with(root: Path, metric: str) -> list[dict]:
    return [
        record
        for record in metrics_mod.read_metrics(root)
        if record.get("metric") == metric
    ]


# --------------------------------------------------------------------------- 读取 / 聚合


def test_record_read_roundtrip_and_fields(root: Path) -> None:
    recorder = metrics_mod.MetricsRecorder(root, instance_id="inst-1")
    recorder.record_duration_ms(metrics_mod.STARTUP_READY_MS, 12.5, cold_start=True)
    recorder.counter(metrics_mod.SLOW_SUBSCRIBER_DROPPED, reason="queue_full")
    recorder.note(metrics_mod.EVENT_SEQ_HEAD, 42)
    recorder.close()

    records = metrics_mod.read_metrics(root)
    assert [record["metric"] for record in records] == [
        metrics_mod.STARTUP_READY_MS,
        metrics_mod.SLOW_SUBSCRIBER_DROPPED,
        metrics_mod.EVENT_SEQ_HEAD,
    ]
    first = records[0]
    assert first["value"] == 12.5
    assert first["cold_start"] is True
    assert first["kind"] == "duration"
    assert first["pid"] == os.getpid()
    assert first["instance_id"] == "inst-1"
    assert isinstance(first["ts"], int) and first["ts"] > 0
    assert records[1]["reason"] == "queue_full"
    assert metrics_mod.metric_values(records, metrics_mod.EVENT_SEQ_HEAD) == [42.0]


def test_unknown_is_null_not_zero(root: Path) -> None:
    """缺测量必须写 `null`；写成 0 会被下游读成"花了 0 毫秒"。"""
    recorder = metrics_mod.MetricsRecorder(root)
    recorder.record_duration_ms(metrics_mod.CANCEL_STOP_MS, None, reason="timeout")
    recorder.close()

    records = metrics_mod.read_metrics(root)
    assert records[0]["value"] is None
    summary = metrics_mod.summarize(records)
    bucket = summary[metrics_mod.CANCEL_STOP_MS]
    assert bucket["count"] == 0
    assert bucket["unknown"] == 1
    assert bucket["p50"] is None and bucket["p95"] is None
    assert metrics_mod.slo_report()["status"] == "unknown"


def test_percentile_is_nearest_rank() -> None:
    assert metrics_mod.percentile([], 50) is None
    assert metrics_mod.percentile([7], 95) == 7.0
    assert metrics_mod.percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert metrics_mod.percentile([1, 2, 3, 4, 5], 95) == 5.0
    assert metrics_mod.percentile([5, 1, 3], 0) == 1.0
    assert metrics_mod.percentile([5, 1, 3], 100) == 5.0


def test_summary_separates_values_and_unknown(root: Path) -> None:
    recorder = metrics_mod.MetricsRecorder(root)
    for value in (10.0, 20.0, 30.0, 40.0):
        recorder.record_duration_ms(metrics_mod.CANCEL_ACK_MS, value)
    recorder.record_duration_ms(metrics_mod.CANCEL_ACK_MS, None, reason="timeout")
    recorder.close()
    bucket = metrics_mod.summarize(metrics_mod.read_metrics(root))[
        metrics_mod.CANCEL_ACK_MS
    ]
    assert bucket["count"] == 4
    assert bucket["unknown"] == 1
    assert bucket["p95"] == 40.0
    assert bucket["min"] == 10.0 and bucket["max"] == 40.0


def test_disabled_recorder_writes_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(metrics_mod.ENV_METRICS, "0")
    recorder = metrics_mod.MetricsRecorder(root)
    assert recorder.enabled is False
    recorder.record(metrics_mod.RSS_MIB, 12.0)
    recorder.close()
    assert metrics_mod.read_metrics(root) == []


def test_sampler_once_returns_real_process_numbers(root: Path) -> None:
    recorder = metrics_mod.MetricsRecorder(root)
    sample = recorder.sample_once(seq_head=lambda: 7)
    recorder.close()
    assert sample["rss_mib"] is None or sample["rss_mib"] > 0
    assert sample["event_seq_head"] == 7
    assert sample["disk_growth"] is not None
    records = metrics_mod.read_metrics(root)
    by_metric = {record["metric"]: record["value"] for record in records}
    assert set(by_metric) >= {
        metrics_mod.RSS_MIB,
        metrics_mod.CPU_PCT,
        metrics_mod.DISK_GROWTH,
        metrics_mod.EVENT_SEQ_HEAD,
    }


def test_metric_file_stays_inside_data_root_and_module_has_no_network(
    root: Path, tmp_path: Path
) -> None:
    """D7：诊断只在本地。指标模块不 import 网络栈，文件只落数据根。"""
    recorder = metrics_mod.MetricsRecorder(root)
    recorder.record(metrics_mod.RSS_MIB, 1.0)
    recorder.close()
    entries = sorted(path.name for path in root.iterdir())
    assert entries == [metrics_mod.METRICS_FILENAME]
    if root.parent == tmp_path:
        parent_entries = sorted(path.name for path in tmp_path.iterdir())
        assert parent_entries == ["data"], (
            f"数据根之外不该有别的落点：{parent_entries}"
        )

    source = Path(metrics_mod.__file__).read_bytes().decode("utf-8")
    tree = ast.parse(source)
    network_modules = {"socket", "ssl", "urllib", "http", "requests", "httpx"}
    import_targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_targets.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            import_targets.add(node.module.split(".")[0])
    assert not (import_targets & network_modules), (
        f"指标模块不该 import 网络栈：{import_targets & network_modules}"
    )
    assert "http://" not in source and "https://" not in source


def test_rejects_non_numeric_value(root: Path) -> None:
    recorder = metrics_mod.MetricsRecorder(root)
    with pytest.raises(TypeError):
        recorder.record(metrics_mod.RSS_MIB, "12.0")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        recorder.record("", 1.0)
    recorder.close()


# --------------------------------------------------------------------------- 真实 daemon 采样


def test_cold_then_warm_startup_metrics_are_real(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷/热启动都要有 `startup_ready_ms`，且是真实正数（不是伪造的 0）。"""
    monkeypatch.setenv(metrics_mod.ENV_METRICS_INTERVAL, "0.02")

    cold = Harness(root, ScriptedModel([]))
    cold.start()
    assert cold.stop_and_wait()
    cold_records = _records_with(root, metrics_mod.STARTUP_READY_MS)
    assert len(cold_records) == 1
    cold_record = cold_records[0]
    assert cold_record["cold_start"] is True
    assert cold_record["value"] > 0, "真实启动耗时不可能为 0（负控锚点）"
    assert cold_record["value"] < 60_000

    warm = Harness(root, ScriptedModel([]))
    warm.start()
    assert warm.stop_and_wait()
    warm_records = _records_with(root, metrics_mod.STARTUP_READY_MS)
    assert len(warm_records) == 2
    warm_record = warm_records[-1]
    assert warm_record["cold_start"] is False
    assert warm_record["value"] > 0

    # 采样线程必须写出事件头与资源样本（"有数据"不是"有日志"）。
    _wait_until(
        lambda: _records_with(root, metrics_mod.EVENT_SEQ_HEAD),
        timeout=5.0,
    )
    heads = metrics_mod.metric_values(
        metrics_mod.read_metrics(root), metrics_mod.EVENT_SEQ_HEAD
    )
    assert heads and heads[-1] >= 0
    rss_records = _wait_until(
        lambda: _records_with(root, metrics_mod.RSS_MIB), timeout=5.0
    )
    rss = metrics_mod.metric_values(
        metrics_mod.read_metrics(root), metrics_mod.RSS_MIB
    )
    if rss:
        assert rss[-1] > 0
    else:
        # 平台没有 `resource`（Windows）：必须留下 unknown 记录，而不是什么都没有。
        # "测不到"与"不存在"在这里同样是可断言的事实。
        assert any(record["value"] is None for record in rss_records)


def test_restart_recover_metric_records_recovery_scan(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(metrics_mod.ENV_METRICS_INTERVAL, "0.05")
    first = Harness(root, ScriptedModel([]))
    first.start()
    first.stop_and_wait()

    second = Harness(root, ScriptedModel([]))
    second.start()
    client = _connect(root)
    try:  # 让热启动时有真实数据可扫描
        task = client.call("task.create", {"goal": "热启动前的任务"})["task"]
        client.call("run.start", {"task_id": task["task_id"]})
    finally:
        client.close()
    second.stop_and_wait()

    records = _records_with(root, metrics_mod.RESTART_RECOVER_MS)
    assert len(records) == 2
    warm = records[-1]
    assert warm["cold_start"] is False
    assert isinstance(warm["runs_scanned"], int) and warm["runs_scanned"] >= 0
    assert warm["value"] >= 0


def test_cancel_ack_and_stop_are_measured_separately(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消的两个时间点：控制面 ack（立即）与执行器停下（安全点之后）。

    用"模型调用期间取消"把两者拆开：ack 不等模型，stop 必须等模型返回后
    安全点确认。谎填 0/把两者混成一个数，都会让本用例的数值关系红。
    """
    monkeypatch.setenv(metrics_mod.ENV_METRICS_INTERVAL, "0.05")
    model = GatedModel()
    harness = Harness(root, model)
    harness.start()
    client = _connect(root)
    try:
        task = client.call("task.create", {"goal": "会被取消的 Run"})["task"]
        run = client.call("run.start", {"task_id": task["task_id"]})["run"]
        assert model.entered.wait(10.0), "模型调用没有开始，测试前提不成立"

        started = time.monotonic()
        client.call("run.cancel", {"run_id": run["run_id"]})
        ack = _wait_until(
            lambda: _records_with(root, metrics_mod.CANCEL_ACK_MS), timeout=5.0
        )[0]
        assert ack["value"] is not None and ack["value"] >= 0
        assert ack["run_id"] == run["run_id"]
        assert time.monotonic() - started < 2.0, "ack 不该等执行器停下"

        # 让 Run 在安全点确认取消；stop 指标必须 > 0，且晚于 ack。
        time.sleep(0.15)
        model.release.set()
        _wait_until(
            lambda: _records_with(root, metrics_mod.CANCEL_STOP_MS), timeout=10.0
        )
        stop = _records_with(root, metrics_mod.CANCEL_STOP_MS)[0]
        assert stop["run_id"] == run["run_id"]
        assert stop["state"] == "cancelled"
        assert stop["value"] is not None
        assert stop["value"] >= 100.0, (
            "stop 计时必须覆盖到安全点（负控：谎填 0 会在这里红）"
        )
        assert stop["value"] >= ack["value"] - 1.0
    finally:
        model.release.set()
        client.close()
        harness.stop_and_wait(timeout=5.0)