"""网络抓取的连接重试与进程级速率下限（W2，SPEC-G2-NET-RESILIENCE 判据）。

核心语义（被本文件钉住，拆掉就红）：

1. **重试边界 = 「可证明零字节到达对端」**：只有连接阶段失败（`net.connect` /
   `net.connect_timeout`）可自动重试——TCP 从未建立，重发不构成第二次外发
   请求，因此不需要重新审批。服务器见过请求之后的一切失败**绝不重试**
   （用服务器端连接数证明，NC3 的锚点）。
2. **进程级速率下限**：相邻两次 fetch 开始至少隔 `min_interval_s`，等待分片
   且随时可取消，等待不计入抓取 deadline。

负控锚点：
- NC1 拆掉重试循环 → `test_retry_recovers_on_connect_failure` / `test_retry_exhausts...` 红；
- NC2 拆掉等待 → `test_rate_limit_*` 红；
- NC3 把 `net.read`/`net.protocol` 加进可重试集 → `test_send_phase_failure_is_never_retried` 红
  （服务器连接数从 1 变 2——这条钉住的是授权边界本体）。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from httpserver import NetFixture, closed_port

import tianshu.net as net_mod
from tianshu.net import (
    ENV_NET_CONNECT_ATTEMPTS,
    ENV_NET_MIN_INTERVAL_S,
    NET_CONNECT_ATTEMPTS,
    NET_MIN_INTERVAL_S,
    NetCancelled,
    NetError,
    NetLimits,
    _wait_for_rate_slot,
    canonical_target,
    fetch,
)

# ---------------------------------------------------------------- 夹具


class _FakeClock:
    """注入时钟：sleep 直接推进时钟，等待判定全程确定。"""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _half_response_server() -> tuple[socket.socket, int, list[int], threading.Thread]:
    """accept 后发半截响应（Content-Length 撒谎）再断：请求已完成、读取失败。

    服务器端记录每次 accept——「读失败绝不重试」只能靠这个数证明。
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepts: list[int] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:  # noqa: PERF203 - 关闭时退出
                return
            accepts.append(1)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nhalf")
                conn.close()
            except OSError:  # pragma: no cover - 客户端先行断开
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server, port, accepts, thread


@pytest.fixture()
def rate_limit_off(monkeypatch: pytest.MonkeyPatch):
    """单测显式关闭间隔（conftest 已默认关，这里防止用例自身需要确定状态）。"""
    monkeypatch.setenv(ENV_NET_MIN_INTERVAL_S, "0")


# ---------------------------------------------------------------- 重试


def test_retry_recovers_on_connect_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """首次连接失败（TCP 未建立）→ 自动重试 → 成功，attempts == 2。"""
    fixture = NetFixture().route("/a", body="好的").start()
    try:
        target = canonical_target(f"network://http/127.0.0.1:{fixture.port}/a")
        calls = {"n": 0}
        real_connect = net_mod._connect

        def flaky(target_obj, timeout_s):  # noqa: ANN001 - 与 _connect 同签名
            calls["n"] += 1
            if calls["n"] == 1:
                # 直接以「连接阶段失败」的稳定分类抛出：OSError→net.connect 的
                # 映射路径由 closed_port 用例覆盖，这里测的是重试循环本身。
                raise NetError("模拟连接被拒（TCP 未建立）", reason="net.connect")
            return real_connect(target_obj, timeout_s)

        monkeypatch.setattr(net_mod, "_connect", flaky)
        fetched = fetch(target, limits=NetLimits(min_interval_s=0))
        assert calls["n"] == 2
        assert fetched.attempts == 2
        assert fetched.status == 200
    finally:
        fixture.stop()


def test_retry_exhausts_and_reports(tmp_path) -> None:
    """closed_port：两次尝试都拒 → net.connect，且连接恰好发起 2 次。"""
    port, sock = closed_port()
    try:
        target = canonical_target(f"network://http/127.0.0.1:{port}/a")

        calls = {"n": 0}
        real_connect = net_mod._connect

        def counting(target_obj, timeout_s):  # noqa: ANN001 - 与 _connect 同签名
            calls["n"] += 1
            return real_connect(target_obj, timeout_s)

        net_mod._connect = counting
        try:
            with pytest.raises(NetError) as excinfo:
                fetch(target, limits=NetLimits(min_interval_s=0))
            assert excinfo.value.reason in {"net.connect", "net.connect_timeout"}
            assert calls["n"] == NET_CONNECT_ATTEMPTS == 2
        finally:
            net_mod._connect = real_connect
    finally:
        sock.close()


def test_connect_attempts_one_means_no_retry(tmp_path) -> None:
    """connect_attempts=1 回到旧行为：恰 1 次连接，失败即失败。"""
    port, sock = closed_port()
    try:
        target = canonical_target(f"network://http/127.0.0.1:{port}/a")

        calls = {"n": 0}
        real_connect = net_mod._connect

        def counting(target_obj, timeout_s):  # noqa: ANN001 - 与 _connect 同签名
            calls["n"] += 1
            return real_connect(target_obj, timeout_s)

        net_mod._connect = counting
        try:
            with pytest.raises(NetError) as excinfo:
                fetch(target, limits=NetLimits(connect_attempts=1, min_interval_s=0))
            assert excinfo.value.reason in {"net.connect", "net.connect_timeout"}
            assert calls["n"] == 1
        finally:
            net_mod._connect = real_connect
    finally:
        sock.close()


def test_send_phase_failure_is_never_retried(rate_limit_off) -> None:
    """NC3 锚点：请求已发出（服务器 accept 过）之后的失败绝不自动重试。

    服务器发半截响应再断 → 客户端读失败；**服务器端连接数必须停在 1**。
    把读失败加进可重试集，这里就会变 2——授权边界（第二次 GET 需要重新
    批准）就是这条测试的本体。
    """
    server, port, accepts, _thread = _half_response_server()
    try:
        target = canonical_target(f"network://http/127.0.0.1:{port}/x")
        with pytest.raises(NetError) as excinfo:
            fetch(target, limits=NetLimits(connect_attempts=3))
        assert excinfo.value.reason in {"net.protocol", "net.read"}, excinfo.value
        assert len(accepts) == 1, (
            f"发送阶段之后的失败被重试了（服务器连接数 {len(accepts)}）——"
            "这等于绕过逐次批准"
        )
    finally:
        server.close()


def test_retry_respects_cancel(tmp_path) -> None:
    """首次连接失败后 Run 被取消 → NetCancelled，第二次尝试不发生。"""
    port, sock = closed_port()
    try:
        target = canonical_target(f"network://http/127.0.0.1:{port}/a")

        calls = {"n": 0}
        real_connect = net_mod._connect

        def counting(target_obj, timeout_s):  # noqa: ANN001 - 与 _connect 同签名
            calls["n"] += 1
            return real_connect(target_obj, timeout_s)

        def cancelled() -> bool:
            return calls["n"] >= 1

        net_mod._connect = counting
        try:
            with pytest.raises(NetCancelled):
                fetch(target, limits=NetLimits(min_interval_s=0), cancel=cancelled)
            assert calls["n"] == 1
        finally:
            net_mod._connect = real_connect
    finally:
        sock.close()


# ---------------------------------------------------------------- 速率下限


def test_rate_limit_wait_semantics() -> None:
    """注入时钟的等待语义：首次不等待；紧随其后等满间隔；间隔已过则不等待。"""
    clock = _FakeClock(start=100.0)
    # 首次抓取（last=None）：不等待。
    _wait_for_rate_slot(clock, None, 1.0, None)
    assert clock.sleeps == []

    # 间隔已过：不等待。
    _wait_for_rate_slot(clock, 98.5, 1.0, None)
    assert clock.sleeps == []

    # 紧随其后：分片等待到间隔满足为止，总量恰为剩余间隔。
    clock.now = 100.0
    _wait_for_rate_slot(clock, 100.0, 1.0, None, sleep=clock.sleep)
    assert clock.sleeps and sum(clock.sleeps) == pytest.approx(1.0)
    assert all(s <= 0.05 for s in clock.sleeps), "分片上限 50ms（取消响应性）"


def test_rate_limit_wait_zero_interval_or_disabled() -> None:
    """min_interval_s <= 0 显式关闭：任何时刻都不等待。"""
    clock = _FakeClock(start=0.0)
    _wait_for_rate_slot(clock, 0.0, 0.0, None, sleep=clock.sleep)
    assert clock.sleeps == []
    _wait_for_rate_slot(clock, 0.0, -0.5, None, sleep=clock.sleep)
    assert clock.sleeps == []


def test_rate_limit_wait_respects_cancel() -> None:
    """等待中取消：立即 NetCancelled，不再睡下一片。"""
    clock = _FakeClock(start=0.0)
    cancelled = {"flag": False}

    def cancel() -> bool:
        return cancelled["flag"]

    def flip_then_sleep(seconds: float) -> None:
        cancelled["flag"] = True
        clock.sleep(seconds)

    with pytest.raises(NetCancelled):
        _wait_for_rate_slot(clock, 0.0, 1.0, cancel, sleep=flip_then_sleep)
    assert clock.sleeps == [pytest.approx(0.05)]


def test_rate_limit_integration_spacing() -> None:
    """loopback 集成：进程级两次 fetch 的墙钟间隔 ≥ min_interval_s；显式 0 关闭。"""
    from httpserver import NetFixture

    fixture = NetFixture().route("/a", body="好的").start()
    try:
        target = canonical_target(f"network://http/127.0.0.1:{fixture.port}/a")
        limits_on = NetLimits(min_interval_s=0.3)
        t0 = time.monotonic()
        fetch(target, limits=limits_on)
        fetch(target, limits=limits_on)
        spacing = time.monotonic() - t0
        assert spacing >= 0.25, f"两次抓取间隔 {spacing:.3f}s < 0.3s 下限"

        # 显式 min_interval_s=0（构造参数优先于 env）：紧随其后不再等待。
        t1 = time.monotonic()
        fetch(target, limits=NetLimits(min_interval_s=0))
        assert time.monotonic() - t1 < 0.25, "显式 0 关闭后不应再等待"
    finally:
        fixture.stop()


# ---------------------------------------------------------------- 默认值与校验


def test_env_overrides_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 合法值生效；非法/越界值回落默认（不做静默禁用）。"""
    monkeypatch.setenv(ENV_NET_CONNECT_ATTEMPTS, "3")
    monkeypatch.setenv(ENV_NET_MIN_INTERVAL_S, "0.5")
    limits = NetLimits()
    assert limits.connect_attempts == 3
    assert limits.min_interval_s == 0.5

    monkeypatch.setenv(ENV_NET_CONNECT_ATTEMPTS, "not-a-number")
    monkeypatch.setenv(ENV_NET_MIN_INTERVAL_S, "-5")
    tolerant = NetLimits()
    assert tolerant.connect_attempts == NET_CONNECT_ATTEMPTS
    assert tolerant.min_interval_s == NET_MIN_INTERVAL_S


def test_limits_validation() -> None:
    """connect_attempts < 1 / min_interval_s < 0 → NetError(net.limits)。"""
    with pytest.raises(NetError) as excinfo:
        NetLimits(connect_attempts=0)
    assert excinfo.value.reason == "net.limits"
    with pytest.raises(NetError) as excinfo:
        NetLimits(min_interval_s=-0.1)
    assert excinfo.value.reason == "net.limits"


# ---------------------------------------------------------------- deadline 烧尽


class _SlowConn:
    """假连接：connect() 烧满 socket 超时（= 整次 deadline）再抛 TimeoutError。"""

    def __init__(self, budget: float) -> None:
        self._budget = budget

    def connect(self) -> None:
        time.sleep(self._budget + 0.1)
        raise TimeoutError("injected connect timeout")

    def close(self) -> None:  # pragma: no cover - _fetch_once 的 finally 会调
        return None


def test_deadline_exhausted_keeps_connect_classification(monkeypatch) -> None:
    """第一次连接尝试烧满整次 deadline → 重试不得把分类顶成 net.timeout。

    修复前：第二次尝试进 `_fetch_once` 后由 `_remaining(deadline)` 抛
    net.timeout——「连接阶段失败、0 字节已知」被顶成「连接建立后的失败、
    用量未知」，失败分类与字节账本口径一起错档；且与本机时钟毫秒级竞争
    （本机侥幸绿、CI 随机红）。修复后：deadline 尽即尝试用尽，最终失败 =
    最后一次真实尝试的原始分类。sleep(+0.1s) 让 deadline **确定性**先到，
    不是竞态。
    """
    port, sock = closed_port()
    try:
        calls: list[int] = []

        def slow_connect(target_obj, timeout_s):  # noqa: ANN001 - 与 _connect 同签名
            calls.append(1)
            return _SlowConn(timeout_s)

        monkeypatch.setattr(net_mod, "_connect", slow_connect)
        target = net_mod.canonical_target(f"network://http/127.0.0.1:{port}/x")
        with pytest.raises(NetError) as err:
            net_mod.fetch(
                target,
                limits=NetLimits(timeout_ms=200, connect_attempts=2, min_interval_s=0.0),
            )
        assert err.value.reason == "net.connect_timeout"
        assert len(calls) == 1, "deadline 已尽，第二次尝试不应发生"
    finally:
        sock.close()
