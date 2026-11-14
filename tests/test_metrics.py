"""PRD §10.6 本地运行指标：采样、读取、聚合与"不许写假数"。

这组测试只关心一件事：**指标是可被程序读取和断言的数据，不是一行行日志**。
因此每个采样点都要能读回来，缺数据必须是 `null`（unknown），而不是 0；
冷/热启动、取消的两个时间点、重启恢复、资源采样都必须来自真实运行。

负控（PRD 要求"把采样写空 / 谎填 0 必须红"）对应两条锚点：
- `test_unknown_is_null_not_zero`：unknown 被写成 0 就红；
- `test_startup_ready_ms_is_positive`：真实启动耗时被写 0 就红。

轮转（2026-11-14，SPEC-METRICS-ROTATION）另有三条负控锚点：
- `test_rotation_splits_file_at_threshold`：拆掉"超阈值先轮转"就红（NC1）；
- `test_rotation_failure_keeps_the_sample`：轮转失败丢弃样本就红（NC2）；
- `test_rotation_emits_observable_event_without_recursion`：摘掉事件级联抑制就红（NC3）。
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

# --------------------------------------------------------------------------- 读取语义


def test_read_metrics_ignores_half_written_tail_line(root: Path) -> None:
    """并发追加时读到的"半行"必须被忽略，而不是把整条诊断打成异常。

    实测（CI ubuntu，PR #88）：`test_slow_subscriber_does_not_block_control_plane`
    一边让 daemon 采样、一边轮询 `read_metrics()`，读到的最后一行断在
    `{"instance_id":"daemon_...",`（第 37 个字符）——`JSONDecodeError` 直接把
    测试打红。JSONL 的"一次追加在读者眼里是否原子"没有保证，所以"没有 `\\n`
    收尾的最后一段"是可能还在写的记录：跳过它，等下一次读，**不当损坏**。
    """
    path = metrics_mod.metrics_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"metric": "a"}\n{"metric": "b", "va')

    assert metrics_mod.read_metrics(path) == [{"metric": "a"}]

    # 下一次读时后半行到齐了：必须完整出现（跳过是"等一下"，不是"永久丢弃"）。
    with path.open("ab") as handle:
        handle.write(b'lue": 1}\n')
    assert metrics_mod.read_metrics(path) == [{"metric": "a"}, {"metric": "b", "value": 1}]


def test_read_metrics_accepts_complete_tail_without_newline(root: Path) -> None:
    """不会补换行的写入方（手工造数据、别的工具）不该白丢最后一条。"""
    path = metrics_mod.metrics_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"metric": "a"}\n{"metric": "b"}')
    assert metrics_mod.read_metrics(path) == [{"metric": "a"}, {"metric": "b"}]


def test_read_metrics_still_rejects_corrupt_complete_line(root: Path) -> None:
    """负控：容忍"正在写的尾巴"不能变成容忍损坏。

    - 中间行坏了（前后都完整）→ 必须带行号抛 `ValueError`；
    - 尾巴带 `\\n` 却解析不了 → 也是损坏，照抛（只有"没换行"才享有等待语义）。
    """
    path = metrics_mod.metrics_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(b'{"metric": "a"}\n{"metric": bad}\n{"metric": "c"}\n')
    with pytest.raises(ValueError, match="第 2 行"):
        metrics_mod.read_metrics(path)

    path.write_bytes(b'{"metric": "a"}\nnot-json\n')
    with pytest.raises(ValueError, match="第 2 行"):
        metrics_mod.read_metrics(path)


# --------------------------------------------------------------------------- 轮转与保留


def test_rotation_splits_file_at_threshold(root: Path) -> None:
    """超阈值先轮转再追加：当前文件重新开始，旧内容进 `.1`，无丢失、按时间序。"""
    recorder = metrics_mod.MetricsRecorder(
        root, max_file_bytes=600, max_rotated_files=50
    )
    for seq in range(12):
        recorder.record("m", 1.0, seq=seq)
    recorder.close()

    # 12 行 ≈ 1KB > 600B：必然至少一次轮转；保留份数足够大，不该有样本被覆盖丢掉。
    slots = [slot for slot, _ in metrics_mod.list_metrics_files(root)]
    rotated = [slot for slot in slots if slot is not None]
    assert rotated, "超过阈值没有轮转（负控锚点：拆掉大小检查这条就红）"
    assert rotated == list(range(len(rotated), 0, -1)), (
        f"轮转槽位应连续且最新在前：{rotated}"
    )
    records = metrics_mod.read_metrics(root, include_rotated=True)
    samples = [r for r in records if r["metric"] == "m"]
    assert [r["seq"] for r in samples] == list(range(12)), "轮转不得丢失或重排样本"
    notes = [r for r in records if r["metric"] == metrics_mod.METRICS_ROTATED]
    assert notes, "轮转必须在指标流里可观测"
    for note in notes:
        assert note["kind"] == "event"
        assert note["max_file_bytes"] == 600
        assert note["value"] is not None and note["value"] > 0
    # 轮转后的数据根条目仍全部以 metrics.jsonl 为前缀（D7 本地边界不因轮转放宽）
    entries = [entry.name for entry in root.iterdir()]
    assert all(
        name.startswith(metrics_mod.METRICS_FILENAME) for name in entries
    ), f"数据根出现了非指标文件：{entries}"


def test_retention_overwrites_oldest_slot(root: Path) -> None:
    """保留上界：槽位不超过 max_rotated_files，最旧样本被链式覆盖而不是无限堆积。"""
    recorder = metrics_mod.MetricsRecorder(
        root, max_file_bytes=600, max_rotated_files=2
    )
    for seq in range(30):
        recorder.record("m", 1.0, seq=seq)
    recorder.close()

    slots = [slot for slot, _ in metrics_mod.list_metrics_files(root) if slot is not None]
    assert slots and max(slots) <= 2, f"槽位超出保留上界：{slots}"
    records = metrics_mod.read_metrics(root, include_rotated=True)
    seqs = [r["seq"] for r in records if r["metric"] == "m"]
    # 30 行 ≥ 3 次轮转：最早一份（槽位 2 被覆盖）一定被换出。
    assert seqs and seqs == sorted(seqs), f"保留内容必须仍按时间序：{seqs}"
    assert seqs[0] >= 1, f"最旧样本未被覆盖，保留策略没生效：{seqs[:5]}"
    assert len(seqs) < 30, "保留策略没有丢弃任何旧数据"


def test_rotation_failure_keeps_the_sample(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮转失败降级为继续追加当前文件：**样本不丢**优先于**文件不超限**。

    Windows 上读者持句柄会让 `os.replace` 拿到 PermissionError——注入同一失败
    （确定性替身），断言样本照常落盘、失败计数可见、恢复后下一轮继续轮转。
    """
    recorder = metrics_mod.MetricsRecorder(
        root, max_file_bytes=60, max_rotated_files=4
    )

    def _boom(src, dst):  # noqa: ANN001 - 签名与 os.replace 一致
        raise PermissionError(13, "模拟读者持句柄")

    monkeypatch.setattr(metrics_mod.os, "replace", _boom)
    for seq in range(3):
        recorder.record("m", 1.0, seq=seq)
    assert recorder.rotation_errors >= 1, "轮转失败没有被计数"

    monkeypatch.undo()
    recorder.record("m", 1.0, seq=3)
    recorder.close()

    records = metrics_mod.read_metrics(root, include_rotated=True)
    seqs = [r["seq"] for r in records if r["metric"] == "m"]
    assert seqs == [0, 1, 2, 3], f"轮转失败期间样本被丢弃了：{seqs}"
    slots = [slot for slot, _ in metrics_mod.list_metrics_files(root) if slot is not None]
    assert slots, "失败恢复后没有继续轮转"


def test_rotation_emits_observable_event_without_recursion(root: Path) -> None:
    """阈值小于一行：每次追加都轮转。事件不得级联事件，否则无限递归。

    轮转事件的追加自身也会触发轮转，但不得再产生新事件——这是 `_append` 里
    `record.get("metric") != METRICS_ROTATED` 抑制点的负控锚点（NC3）。
    """
    recorder = metrics_mod.MetricsRecorder(
        root, max_file_bytes=10, max_rotated_files=50
    )
    for seq in range(3):
        recorder.record("m", 1.0, seq=seq)
    recorder.close()

    records = metrics_mod.read_metrics(root, include_rotated=True)
    samples = [r for r in records if r["metric"] == "m"]
    assert [r["seq"] for r in samples] == [0, 1, 2]
    notes = [r for r in records if r["metric"] == metrics_mod.METRICS_ROTATED]
    assert notes, "轮转事件完全不可观测"
    assert len(notes) <= len(samples), (
        f"轮转事件发生了级联（{len(notes)} 条事件 > {len(samples)} 次普通追加）"
    )


def test_include_rotated_reads_chronologically(root: Path) -> None:
    """`include_rotated=True` 按时间序（最旧在前）拼接：`.2 → .1 → base`。"""
    base = metrics_mod.metrics_path(root)
    base.parent.mkdir(parents=True, exist_ok=True)
    metrics_mod.rotated_path(base, 1).write_bytes(
        b'{"metric":"m","seq":10}\n{"metric":"m","seq":11}\n'
    )
    metrics_mod.rotated_path(base, 2).write_bytes(b'{"metric":"m","seq":0}\n')
    base.write_bytes(b'{"metric":"m","seq":12}\n')

    records = metrics_mod.read_metrics(root, include_rotated=True)
    assert [r["seq"] for r in records] == [0, 10, 11, 12]
    assert [r["seq"] for r in metrics_mod.read_metrics(root)] == [12], (
        "默认行为必须是只读当前文件（既有调用方零迁移）"
    )
    limited = metrics_mod.read_metrics(root, include_rotated=True, limit=2)
    assert [r["seq"] for r in limited] == [11, 12], "limit 应作用于拼接后的全序列"


def test_list_metrics_files_orders_and_ignores_foreign_suffix(root: Path) -> None:
    """清单只收纯数字槽位；非数字后缀与目录不进清单；空洞槽位按存在序。"""
    base = metrics_mod.metrics_path(root)
    base.parent.mkdir(parents=True, exist_ok=True)
    metrics_mod.rotated_path(base, 1).write_bytes(b'{"metric":"a"}\n')
    metrics_mod.rotated_path(base, 3).write_bytes(b'{"metric":"c"}\n')
    (base.parent / "metrics.jsonl.tmp").write_bytes(b"{}\n")
    (base.parent / "metrics.jsonl.backup").write_bytes(b"{}\n")
    (base.parent / "metrics.jsonl.5").mkdir()

    # 当前文件还不存在：清单里没有槽位 None 的条目，轮转件照常列出（异常但可用）。
    assert [(slot, path.name) for slot, path in metrics_mod.list_metrics_files(root)] == [
        (3, "metrics.jsonl.3"),
        (1, "metrics.jsonl.1"),
    ]

    base.write_bytes(b'{"metric":"b"}\n')
    files = metrics_mod.list_metrics_files(root)
    assert [(slot, path.name) for slot, path in files] == [
        (3, "metrics.jsonl.3"),
        (1, "metrics.jsonl.1"),
        (None, "metrics.jsonl"),
    ]


def test_rotation_params_validation(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """构造侧 fail fast（负值 ValueError）；env 侧沿用模块容错：非法/负值回落默认。"""
    with pytest.raises(ValueError):
        metrics_mod.MetricsRecorder(root, max_file_bytes=-1)
    with pytest.raises(ValueError):
        metrics_mod.MetricsRecorder(root, max_rotated_files=0)

    recorder = metrics_mod.MetricsRecorder(root)
    assert recorder.max_file_bytes == metrics_mod.DEFAULT_MAX_FILE_BYTES
    assert recorder.max_rotated_files == metrics_mod.DEFAULT_MAX_ROTATED_FILES
    recorder.close()

    monkeypatch.setenv(metrics_mod.ENV_METRICS_MAX_FILE_BYTES, "-5")
    monkeypatch.setenv(metrics_mod.ENV_METRICS_MAX_ROTATED_FILES, "not-a-number")
    tolerant = metrics_mod.MetricsRecorder(root)
    assert tolerant.max_file_bytes == metrics_mod.DEFAULT_MAX_FILE_BYTES
    assert tolerant.max_rotated_files == metrics_mod.DEFAULT_MAX_ROTATED_FILES
    tolerant.close()

    monkeypatch.setenv(metrics_mod.ENV_METRICS_MAX_FILE_BYTES, "4096")
    monkeypatch.setenv(metrics_mod.ENV_METRICS_MAX_ROTATED_FILES, "2")
    configured = metrics_mod.MetricsRecorder(root)
    assert configured.max_file_bytes == 4096
    assert configured.max_rotated_files == 2
    configured.close()


def test_zero_disables_rotation(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """逃生门：`max_file_bytes=0`（构造或 env）显式禁用轮转，行为可断言。"""
    manual_root = root / "manual"
    manual = metrics_mod.MetricsRecorder(manual_root, max_file_bytes=0)
    assert manual.max_file_bytes == 0
    for seq in range(8):
        manual.record("m", 1.0, seq=seq)
    manual.close()

    monkeypatch.setenv(metrics_mod.ENV_METRICS_MAX_FILE_BYTES, "0")
    via_env_root = root / "via-env"
    via_env = metrics_mod.MetricsRecorder(via_env_root)
    assert via_env.max_file_bytes == 0
    for seq in range(8):
        via_env.record("m", 1.0, seq=seq)
    via_env.close()

    for data_root in (manual_root, via_env_root):
        slots = [
            slot
            for slot, _ in metrics_mod.list_metrics_files(data_root)
            if slot is not None
        ]
        assert slots == [], f"禁用轮转后仍产生了轮转件：{data_root}"
        seqs = [
            r["seq"]
            for r in metrics_mod.read_metrics(data_root)
            if r["metric"] == "m"
        ]
        assert seqs == list(range(8)), f"禁用轮转后样本丢失：{data_root}"
