"""本地运行质量指标（PRD §10.6）：进程内采样、写本机数据根、不联网。

## 这条模块回答什么

发布候选之前，需要能回答"这台机器上跑起来到底多久 / 卡在哪 / 哪个客户端会把
控制面拖住"，而不是只凭体感。指标只落**本机数据根**（`<data_root>/metrics.jsonl`），
每次追加一行 JSON；没有上报端点、没有后台 exporter、不读环境里的任何 URL。

## 为什么不写"日志"

日志是给人翻的，指标要能被程序读取并断言：每一行都能被 `read_metrics()` 解析、
被 `summarize()` 聚合。缺数据时写 `value: null`（unknown），**不写 0**——0 会被
下游读成"这件事花了 0 毫秒"，那是假数据。

## 为什么没有阈值

PRD §10.6 明确：数值冻结留给发布候选。本模块提供 `SLO_THRESHOLDS` 空表与
`slo_report()`，在阈值未冻结前一律返回 `unknown`，不生成"通过/不通过"的假 SLO。

## 并发与文件格式

- 采样来自守护进程的多个线程（主循环记取消计时、发送线程/看门狗记慢订阅者、
  采样线程记资源）。追加走一把进程内锁 + `O_APPEND` 单行写入；一行一个 JSON
  对象，崩溃最多留下半行，读取侧对空行/坏行有明确处理。
- 产品代码禁止文本模式文件 IO（仓库纪律），这里只用 `os.open/os.write` 写字节。

## 字段口径（`docs/runtime-metrics.md` 有实测值与未冻结说明）

| metric | 含义 |
|---|---|
| `startup_ready_ms` | 从 `serve()` 调用到"端点可服务"的毫秒数；`cold_start` 标记是否有旧数据根 |
| `cancel_ack_ms` | 收到 `run.cancel` RPC 到回出响应的毫秒数（控制面确认，不是执行器停下） |
| `cancel_stop_ms` | 从 `run.cancel` 到 Run 落到终态的毫秒数；超时记 `null` |
| `restart_recover_ms` | 启动恢复扫描耗时；`cold_start=false` 时才有实际恢复工作 |
| `rss_mib` / `cpu_pct` | 进程常驻内存（MiB）与 CPU 百分比（相对单核，相邻采样差） |
| `disk_growth` | 数据根字节数相对启动基线的增量（`disk_bytes` 给总量） |
| `event_seq_head` | 采样时刻事件日志最大 seq（补洞/积压的第一手信号） |
| `slow_subscriber_dropped` | 慢订阅者被断开的计数；`reason` 区分队列满 / 发送超期 / 发送错误 |
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

METRICS_FILENAME = "metrics.jsonl"

#: 环境变量：`TIANSHU_METRICS=0` 关闭采样（默认开启）。注意这是**本地开关**，
#: 不是遥测开关——本项目没有遥测（D7：诊断包只本地导出）。
ENV_METRICS = "TIANSHU_METRICS"

#: 环境变量：采样间隔（秒），默认 5s。测试把它调小以取得确定性样本。
ENV_METRICS_INTERVAL = "TIANSHU_METRICS_INTERVAL_S"

DEFAULT_SAMPLE_INTERVAL_S = 5.0

#: 数据根磁盘统计的扫描上限：超过就记 unknown，而不是把守护进程拖进一次
#: 百万文件的 `walk`。指标不该成为被测量的开销。
MAX_DISK_ENTRIES = 50_000

# ---- 指标名（字符串常量：读取侧与写入侧不能各写一套拼写） -------------------

STARTUP_READY_MS = "startup_ready_ms"
CANCEL_ACK_MS = "cancel_ack_ms"
CANCEL_STOP_MS = "cancel_stop_ms"
RESTART_RECOVER_MS = "restart_recover_ms"
RSS_MIB = "rss_mib"
CPU_PCT = "cpu_pct"
DISK_GROWTH = "disk_growth"
EVENT_SEQ_HEAD = "event_seq_head"
SLOW_SUBSCRIBER_DROPPED = "slow_subscriber_dropped"

#: 逗号分隔的"至少有数据"清单：`docs/runtime-metrics.md` 与验收用它核对
#: 一次真实运行是否覆盖了 PRD §10.6 要求的字段。
REQUIRED_METRICS: tuple[str, ...] = (
    STARTUP_READY_MS,
    CANCEL_ACK_MS,
    CANCEL_STOP_MS,
    RESTART_RECOVER_MS,
    RSS_MIB,
    CPU_PCT,
    DISK_GROWTH,
    EVENT_SEQ_HEAD,
    SLOW_SUBSCRIBER_DROPPED,
)

#: SLO 阈值表。**刻意留空**：PRD §10.6 把数值冻结留给发布候选；在此之前
#: `slo_report()` 必须返回 unknown，而不是拿一个拍脑袋的数字当验收线。
SLO_THRESHOLDS: dict[str, dict[str, Any]] = {}

_RESERVED_KEYS = frozenset({"ts", "metric", "value", "pid", "instance_id", "kind"})


def metrics_path(data_root: Path) -> Path:
    return Path(data_root) / METRICS_FILENAME


def _now_ms() -> int:
    return int(time.time() * 1000)


def percentile(values: Iterable[float], p: float) -> float | None:
    """纯 Python 的 nearest-rank 百分位（p∈[0,100]）。

    为什么不用插值：样本量小（本地运行几十次），插值只会制造"更精确"的错觉；
    nearest-rank 可以在文档里写成一句话、可以被下一个人手算复核。
    空序列返回 None——那是"没有数据"，不是 0。
    """
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if p <= 0:
        return data[0]
    if p >= 100:
        return data[-1]
    rank = max(1, math.ceil(p / 100.0 * len(data)))
    return data[min(rank - 1, len(data) - 1)]


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 metric 聚合：count / unknown / p50 / p95 / min / max。

    `unknown` 单独计数（value 为 null 的样本），**不混进** p50/p95 的分母。
    两者分开，才能回答"是慢"还是"根本没测到"。
    """
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        metric = record.get("metric")
        if not isinstance(metric, str):
            continue
        bucket = groups.setdefault(
            metric, {"values": [], "unknown": 0}
        )
        value = record.get("value")
        if value is None:
            bucket["unknown"] += 1
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            bucket["values"].append(float(value))
        else:  # pragma: no cover - 写入侧已拒绝非数值
            bucket["unknown"] += 1
    result: dict[str, dict[str, Any]] = {}
    for metric, bucket in sorted(groups.items()):
        values = bucket["values"]
        result[metric] = {
            "count": len(values),
            "unknown": bucket["unknown"],
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return result


def slo_report() -> dict[str, Any]:
    """SLO 状态：阈值未冻结时只能是 unknown（PRD §10.6）。"""
    return {
        "status": "unknown",
        "reason": "SLO 阈值未冻结：数值留给发布候选（PRD §10.6）",
        "thresholds": dict(SLO_THRESHOLDS),
    }


def read_metrics(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """读取 JSONL 指标。`path` 可以是数据根（目录）或文件本身。

    - 空行跳过；坏行抛 `ValueError`（带行号）：指标文件损坏不该被当成"没有数据"；
    - `limit` 只取**最后** N 条（诊断时看最近的）。
    """
    target = Path(path)
    if target.is_dir():
        target = metrics_path(target)
    if not target.exists():
        return []
    raw = target.read_bytes()
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.split(b"\n"), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"指标文件第 {lineno} 行无法解析：{exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"指标文件第 {lineno} 行不是 JSON 对象")
        records.append(record)
    if limit is not None and limit >= 0:
        records = records[-limit:]
    return records


def metric_values(
    records: Iterable[dict[str, Any]], metric: str
) -> list[float]:
    """取一个指标的所有**数值**样本（null 不算数值）。"""
    return [
        float(record["value"])
        for record in records
        if record.get("metric") == metric
        and isinstance(record.get("value"), (int, float))
        and not isinstance(record.get("value"), bool)
    ]


def _env_enabled() -> bool:
    raw = os.environ.get(ENV_METRICS)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _env_interval() -> float:
    raw = os.environ.get(ENV_METRICS_INTERVAL)
    if not raw:
        return DEFAULT_SAMPLE_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SAMPLE_INTERVAL_S
    return value if value > 0 else DEFAULT_SAMPLE_INTERVAL_S


def _rss_mib() -> float | None:
    """进程 RSS（MiB）。

    macOS 的 `ru_maxrss` 单位是字节，Linux 是 KiB——两种都实测过，用
    `sys.platform` 分支；拿不到就返回 None（unknown），不猜 0。
    """
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (ImportError, OSError, ValueError):  # pragma: no cover - 平台缺能力
        return None
    peak = float(usage.ru_maxrss)
    if peak <= 0:
        return None
    if sys.platform == "darwin":
        return peak / (1024.0 * 1024.0)
    return peak / 1024.0


def _disk_bytes(data_root: Path, *, max_entries: int = MAX_DISK_ENTRIES) -> int | None:
    """数据根目录的字节数；条目超过上限返回 None（unknown，不拖慢进程）。"""
    total = 0
    seen = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(data_root):
            for name in filenames:
                seen += 1
                if seen > max_entries:
                    return None
                try:
                    total += os.stat(
                        os.path.join(dirpath, name), follow_symlinks=False
                    ).st_size
                except OSError:
                    continue
    except OSError:  # pragma: no cover - 数据根不可读
        return None
    return total


class MetricsRecorder:
    """把采样追加到 `<data_root>/metrics.jsonl` 的进程内采样器。

    失败容忍：写失败只累加 `write_errors` 并在 stderr 提示一次，**不抛出**——
    指标是诊断能力，不能反过来把主业务弄崩。写失败时文件缺数据，读取侧会看到
    空/少样本（测试用负控覆盖这一点）。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        enabled: bool | None = None,
        interval_s: float | None = None,
        instance_id: str = "",
        pid: int | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.enabled = _env_enabled() if enabled is None else bool(enabled)
        self.interval_s = _env_interval() if interval_s is None else max(0.01, float(interval_s))
        self.instance_id = instance_id
        self.pid = os.getpid() if pid is None else int(pid)
        self.path = metrics_path(self.data_root)
        self._lock = threading.Lock()
        self._sampler: threading.Thread | None = None
        self._sampler_stop = threading.Event()
        self._write_errors = 0
        self._logged_error = False
        self._disk_baseline: int | None = None
        self._last_wall: float | None = None
        self._last_cpu: float | None = None
        if self.enabled:
            self.data_root.mkdir(parents=True, exist_ok=True)
            self._disk_baseline = _disk_bytes(self.data_root)

    # -- 写入 ---------------------------------------------------------------

    @property
    def write_errors(self) -> int:
        return self._write_errors

    def _append(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        raw = (
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with self._lock:
                fd = os.open(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                try:
                    offset = 0
                    while offset < len(raw):
                        written = os.write(fd, raw[offset:])
                        if written <= 0:  # pragma: no cover - 盘满/IO 异常
                            raise OSError("os.write 返回 0")
                        offset += written
                finally:
                    os.close(fd)
        except OSError as exc:
            self._write_errors += 1
            if not self._logged_error:
                self._logged_error = True
                print(
                    f"[tianshu-metrics] 指标写入失败（后续静默计数）：{exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def _record(
        self,
        metric: str,
        value: float | int | None,
        kind: str,
        fields: dict[str, Any],
    ) -> None:
        if not isinstance(metric, str) or not metric:
            raise TypeError("metric 必须是非空字符串")
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise TypeError(f"指标值必须是数值或 None（unknown）：{metric}={value!r}")
        extra = {k: v for k, v in fields.items() if k not in _RESERVED_KEYS}
        record: dict[str, Any] = {
            "ts": _now_ms(),
            "metric": metric,
            "value": value,
            "kind": kind,
            "pid": self.pid,
            "instance_id": self.instance_id,
            **extra,
        }
        self._append(record)

    def record(self, metric: str, value: float | int | None, **fields: Any) -> None:
        """记一个采样点。`value=None` 表示 unknown（缺测量，不是 0）。"""
        self._record(metric, value, "sample", fields)

    def record_duration_ms(self, metric: str, ms: float | None, **fields: Any) -> None:
        self._record(metric, None if ms is None else float(ms), "duration", fields)

    def counter(self, metric: str, increment: int = 1, **fields: Any) -> None:
        """记一次计数事件（每次记增量；聚合时用 sum，不是 p95）。"""
        self._record(metric, float(increment), "counter", fields)

    def note(self, metric: str, value: float | int | None, **fields: Any) -> None:
        """语义化记录（如 `event_seq_head`）；kind 用 event 便于读文件的人区分。"""
        self._record(metric, value, "event", fields)

    # -- 采样线程 -----------------------------------------------------------

    def start_sampler(
        self,
        *,
        seq_head: Callable[[], int] | None = None,
    ) -> None:
        """启动后台采样（幂等：重复调用不会开第二条线程）。"""
        if not self.enabled or self._sampler is not None:
            return
        self._last_wall = time.monotonic()
        try:
            start_cpu: float | None = time.process_time()
        except (OSError, ValueError):  # pragma: no cover - 平台缺能力
            start_cpu = None
        self._last_cpu = start_cpu
        # 基线重新取一次：构造到启动采样之间数据根可能已经建了几张表。
        self._disk_baseline = _disk_bytes(self.data_root)
        self._sampler_stop.clear()
        self._sampler = threading.Thread(
            target=self._sample_loop,
            args=(seq_head,),
            name="tianshu-metrics",
            daemon=True,
        )
        self._sampler.start()

    def _sample_loop(self, seq_head: Callable[[], int] | None) -> None:
        while not self._sampler_stop.is_set():
            self.sample_once(seq_head=seq_head)
            if self._sampler_stop.wait(self.interval_s):
                return

    def sample_once(
        self, *, seq_head: Callable[[], int] | None = None
    ) -> dict[str, float | int | None]:
        """取一次资源样本（测试直接调用它，不靠 sleep 猜采样点）。"""
        rss = _rss_mib()
        self._record(RSS_MIB, rss, "resource", {})

        now_wall = time.monotonic()
        try:
            now_cpu: float | None = time.process_time()
        except (OSError, ValueError):  # pragma: no cover - 平台缺能力
            now_cpu = None
        cpu_pct: float | None = None
        if now_cpu is not None and self._last_cpu is not None and self._last_wall:
            wall = now_wall - self._last_wall
            if wall > 0:
                cpu_pct = max(0.0, (now_cpu - self._last_cpu) / wall * 100.0)
        self._last_wall = now_wall
        self._last_cpu = now_cpu
        self._record(CPU_PCT, cpu_pct, "resource", {})

        disk = _disk_bytes(self.data_root)
        growth: int | None = None
        if disk is not None and self._disk_baseline is not None:
            growth = disk - self._disk_baseline
        self._record(DISK_GROWTH, growth, "resource", {"disk_bytes": disk})

        head: int | None = None
        if seq_head is not None:
            try:
                head = int(seq_head())
            except Exception:  # noqa: BLE001 - 采样失败只记 unknown，不拖垮主进程
                head = None
        self._record(EVENT_SEQ_HEAD, head, "event", {})

        return {
            "rss_mib": rss,
            "cpu_pct": cpu_pct,
            "disk_growth": growth,
            "event_seq_head": head,
        }

    def stop_sampler(self) -> None:
        self._sampler_stop.set()
        thread, self._sampler = self._sampler, None
        if thread is not None:
            thread.join(timeout=5.0)

    def close(self) -> None:
        self.stop_sampler()


__all__ = [
    "CANCEL_ACK_MS",
    "CPU_PCT",
    "DEFAULT_SAMPLE_INTERVAL_S",
    "DISK_GROWTH",
    "ENV_METRICS",
    "ENV_METRICS_INTERVAL",
    "EVENT_SEQ_HEAD",
    "METRICS_FILENAME",
    "MetricsRecorder",
    "REQUIRED_METRICS",
    "RESTART_RECOVER_MS",
    "RSS_MIB",
    "SLOW_SUBSCRIBER_DROPPED",
    "STARTUP_READY_MS",
    "metric_values",
    "metrics_path",
    "percentile",
    "read_metrics",
    "slo_report",
    "summarize",
]