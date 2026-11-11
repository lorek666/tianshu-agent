"""守护进程的集成测试：真的起一个 daemon、真的连 IPC、真的关客户端。

这里要证明的不是"函数能调用"，而是 M5 的退出条件本身：

1. **关掉界面任务继续跑**——客户端断开只是少了一个订阅者，Run 照旧推进到终态。
2. **重启后恢复**——启动扫描把"有 intent 无 result"的 Run 交给 `recover_run`；
   确认不了就落 `reconciling`，**不许当成功**；`waiting_approval` 绝不被自动续跑。
3. **审批落定后自动续跑**——人点完"允许"，不需要再点一次"继续"。
4. **单实例**——同一个 `data_root` 的第二个 daemon 被拒；陈旧锁（连不上端点）
   可以接管。
5. **推送游标**——`subscribe {since_seq}` 先补历史再推增量，断线重连不重不漏；
   daemon 重启后 `event_seq_head` 继续，不从零开始。
6. **shutdown**——5 秒内干净退出：端点/socket/锁都清掉，下一次能直接起。

跨平台注意：unix socket 路径上限 100 字节，`pytest` 的 `tmp_path` 在 macOS 上
通常会超。`root` 夹具优先用 `tmp_path`，超长时退到系统临时目录里的短路径
（Windows 是命名管道，没有这个限制，直接用 `tmp_path`）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from support import stack_for, write_exact

from tianshu import daemon, ipc
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.jobs import JobStore
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.tasks import RUN_RECONCILING, RUN_RUNNING, RUN_WAITING_APPROVAL

# --------------------------------------------------------------------------- 夹具


def _short_data_root(tmp_path: Path) -> tuple[Path, bool]:
    """返回 (data_root, 是否在 tmp_path 里)。"""
    candidate = tmp_path / "data"
    try:
        ipc.endpoint_path(candidate)
        return candidate, True
    except ipc.ProtocolError:
        # macOS 上测试路径太长：换一个短目录，并让调用方负责清理。
        return Path(tempfile.mkdtemp(prefix="tsd-", dir=tempfile.gettempdir())), False


@pytest.fixture()
def root(tmp_path):
    path, inside_tmp = _short_data_root(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    if not inside_tmp:
        shutil.rmtree(path, ignore_errors=True)


class SlowModel:
    """在 `ScriptedModel` 外面加一层延迟。

    用途是让"客户端断开之后还在跑"这件事**可观测**：如果 daemon 在
    `run.start` 返回前就把 Run 跑完了，断开客户端再断言终态就证明不了任何东西。
    """

    def __init__(self, responses: list[ModelResponse], *, delay: float = 0.0) -> None:
        self._inner = ScriptedModel(responses)
        self._delay = delay
        self.calls = 0

    def complete(self, messages, tools):  # noqa: ANN001, ANN201 - 与 Model 协议同形
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        return self._inner.complete(messages, tools)

    def capability_profile(self):  # noqa: ANN201 - 直接委托
        return self._inner.capability_profile()

    def price_identity(self):  # noqa: ANN201 - 直接委托
        return self._inner.price_identity()


class RouteModel:
    """按任务目标分发脚本（每个目标一份响应队列），用来让两条 Run 交替推进。

    单线程驱动意味着模型调用是串行的，但**控制面不该等它**：这里给"长任务"
    设一个明显的模型延迟，再在延迟窗口里从另一条连接审批/查询，用延迟上界
    证明主循环没有被驱动线程拖住。
    """

    def __init__(self, routes: dict[str, list[tuple[float, ModelResponse]]]) -> None:
        self._routes = {key: list(items) for key, items in routes.items()}
        self.calls = 0

    def complete(self, messages, tools):  # noqa: ANN001, ANN201
        self.calls += 1
        goal = ""
        for message in messages:
            if message.get("role") == "user":
                goal = str(message.get("content") or "")
                break
        for key, items in self._routes.items():
            if key in goal:
                if not items:
                    return ModelResponse(finish_reason="end_turn", content="")
                delay, response = items.pop(0)
                if delay:
                    time.sleep(delay)
                return response
        return ModelResponse(finish_reason="end_turn", content="")

    def capability_profile(self):  # noqa: ANN201
        return ScriptedModel([]).capability_profile()

    def price_identity(self):  # noqa: ANN201
        return ScriptedModel([]).price_identity()


class BlockingModel:
    """第一次 `complete()` 就阻塞，直到测试放行。

    用来证明"驱动线程卡住时 shutdown 仍能在 5 秒内返回，且不去关一条还可能
    被别的线程使用的连接（否则会得到一个没人看得到的 ProgrammingError）"。
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools):  # noqa: ANN001, ANN201
        self.entered.set()
        self.release.wait(30.0)
        return ModelResponse(finish_reason="end_turn", content="迟到的完成")

    def capability_profile(self):  # noqa: ANN201
        return ScriptedModel([]).capability_profile()

    def price_identity(self):  # noqa: ANN201
        return ScriptedModel([]).price_identity()


class Harness:
    """在子线程里跑 `serve()`，并等它就绪。"""

    def __init__(
        self,
        root: Path,
        model,
        *,
        limits: Limits | None = None,
        driver_interval_s: float = 0.02,
    ) -> None:
        self.root = root
        self.model = model
        self.limits = limits
        self.driver_interval_s = driver_interval_s
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run, name="test-tianshu-daemon", daemon=True
        )

    def _run(self) -> None:
        try:
            daemon.serve(
                self.root,
                model=self.model,
                limits=self.limits,
                driver_interval_s=self.driver_interval_s,
                on_ready=self.ready.set,
                stop_event=self.stop,
            )
        except BaseException as exc:  # noqa: BLE001 - 测试要把启动失败带回来
            self.errors.append(exc)
        finally:
            self.ready.set()

    def start(self, timeout: float = 15.0) -> Harness:
        self.thread.start()
        assert self.ready.wait(timeout), "daemon 没有在超时内就绪"
        if self.errors:
            raise self.errors[0]
        return self

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


@pytest.fixture()
def start_daemon(root):
    started: list[Harness] = []

    def _start(model, **kwargs) -> Harness:
        harness = Harness(root, model, **kwargs)
        harness.start()
        started.append(harness)
        return harness

    yield _start
    for harness in started:
        harness.stop_and_wait(timeout=5.0)


# --------------------------------------------------------------------------- 辅助


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


def _run_state(client: ipc.Client, run_id: str) -> str:
    return client.call("run.status", {"run_id": run_id})["run"]["state"]


def _wait_run_state(
    client: ipc.Client, run_id: str, states: set[str], *, timeout: float = 10.0
) -> dict:
    def probe():
        run = client.call("run.status", {"run_id": run_id})["run"]
        return run if run["state"] in states else None

    return _wait_until(probe, timeout=timeout)


def _wait_terminal(client: ipc.Client, run_id: str, *, timeout: float = 15.0) -> dict:
    return _wait_run_state(
        client, run_id, {"succeeded", "failed", "cancelled", "fenced"}, timeout=timeout
    )


def _collect_events(
    client: ipc.Client, *, expected: int, timeout: float = 10.0
) -> list[dict]:
    """把订阅连接上的推送收集到 `expected` 条（或超时）。"""
    collected: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(collected) < expected and time.monotonic() < deadline:
        message = client.next_event(timeout=0.2)
        if message is None:
            continue
        collected.append(message["event"])
    return collected


def _db_events(root: Path) -> list:
    log = EventLog(root / "events.db")
    try:
        return log.read_after(0)
    finally:
        log.close()


def _serve_thread(root: Path, model) -> tuple[threading.Thread, list[BaseException]]:
    """起一个不做就绪等待的 serve（用于"应该被拒"的场景）。"""
    errors: list[BaseException] = []
    ready = threading.Event()

    def target() -> None:
        try:
            daemon.serve(root, model=model, driver_interval_s=0.02, on_ready=ready.set)
        except BaseException as exc:  # noqa: BLE001 - 要把失败类型交给断言
            errors.append(exc)
        finally:
            ready.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    assert ready.wait(15.0), "第二个 daemon 既没就绪也没报错"
    thread.join(5.0)
    return thread, errors


def _fake_stale_lock(root: Path) -> Path:
    path = daemon.lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    started_at = int(time.time() * 1000) - 60_000
    payload = (
        f'{{"pid": 999999, "started_at": {started_at},'
        ' "instance_id": "stale_instance"}'
    )
    path.write_bytes(payload.encode("utf-8"))
    return path


# --------------------------------------------------------------------------- 端到端


def test_first_full_run_reaches_terminal(root, start_daemon) -> None:
    """**第一条测试就端到端**：真起 daemon → task.create → run.start → 终态。

    这条测试存在的理由：控制面的 27 条单测全绿也抓不到"驱动线程用了别的线程
    建的 SQLite 连接"这类 bug——它只在 `serve()` 的线程模型里出现。所以这里
    先证明"一个 Run 真能跑完"，再谈握手/推送/恢复。
    """
    workspace = root / "ws"
    workspace.mkdir()
    start_daemon(
        ScriptedModel([ModelResponse(finish_reason="end_turn", content="完成")])
    )
    client = _connect(root)
    try:
        task = client.call(
            "task.create",
            {
                "goal": "跑到底",
                "workspaces": [{"root_path": str(workspace), "mode": "writable"}],
            },
        )["task"]
        run = client.call(
            "run.start", {"task_id": task["task_id"], "out_name": "out.md"}
        )["run"]
        assert run["state"] not in ("succeeded", "failed", "cancelled")
        final = _wait_terminal(client, run["run_id"])
        assert final["state"] == "succeeded"
        assert final["outcome"] == "succeeded"
        assert final["ended_at"] is not None
        assert final["goal_revision"] == 1
        types = [
            event["type"]
            for event in client.call("events.run", {"run_id": run["run_id"]})["events"]
        ]
        assert "run.end" in types and "run.finish" in types
        assert "run.state" in types
        assert "model.result" in types
    finally:
        client.close()


# --------------------------------------------------------------------------- 握手


def test_hello_returns_welcome_and_requires_handshake_first(root, start_daemon) -> None:
    start_daemon(ScriptedModel([]))
    client = _connect(root)
    try:
        welcome = client.welcome
        assert set(welcome) >= {
            "protocol",
            "daemon_version",
            "instance_id",
            "event_seq_head",
            "data_root",
            "started_at",
        }
        assert welcome["protocol"] == ipc.PROTOCOL_MAX
        assert welcome["data_root"] == str(root)
        assert isinstance(welcome["event_seq_head"], int)
        assert isinstance(welcome["started_at"], int)
        assert welcome["instance_id"]
        # 握手后未知方法 → unknown_method（可以按 code 分支）。
        with pytest.raises(ipc.RemoteError) as excinfo:
            client.call("run.explode")
        assert excinfo.value.code == "unknown_method"
    finally:
        client.close()


def test_requests_before_hello_are_protocol_mismatch(root, start_daemon) -> None:
    """§3.3：未完成 hello 的连接只能调 hello，其它方法一律 protocol_mismatch。"""
    from multiprocessing.connection import Client as RawClient

    start_daemon(ScriptedModel([]))
    family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
    raw = RawClient(
        ipc.endpoint_path(root), family=family, authkey=ipc.load_authkey(root)
    )
    try:
        raw.send_bytes(ipc.encode(ipc.request("state", {}, request_id="c1")))
        reply = ipc.decode(raw.recv_bytes())
    finally:
        raw.close()
    assert reply["ok"] is False
    assert reply["error"]["code"] == "protocol_mismatch"


# --------------------------------------------------------------------------- 关 UI 继续跑


def test_run_continues_after_client_disconnects(root, start_daemon) -> None:
    """**M5 的判据**：关掉客户端（= 关 UI），任务继续推进到终态。"""
    workspace = root / "ws"
    workspace.mkdir()
    model = SlowModel([ModelResponse(finish_reason="end_turn", content="完成")],
                      delay=0.5)
    start_daemon(model)

    client = _connect(root)
    task = client.call(
        "task.create",
        {
            "goal": "写个总结",
            "workspaces": [{"root_path": str(workspace), "mode": "writable"}],
        },
    )["task"]
    run = client.call("run.start", {"task_id": task["task_id"], "out_name": "r.md"})[
        "run"
    ]
    assert run["state"] not in ("succeeded", "failed", "cancelled")
    # 模拟"关掉界面"：连接关掉，之后没有任何客户端在场。
    client.close()

    watcher = _connect(root)
    try:
        final = _wait_terminal(watcher, run["run_id"])
        assert final["state"] == "succeeded"
        assert final["outcome"] == "succeeded"
        assert final["ended_at"] is not None
        # 至少真的经过了一次模型调用（不是 run.start 顺手跑完的）。
        assert model.calls >= 1
    finally:
        watcher.close()


# --------------------------------------------------------------------------- 审批


def _prepare_overwrite_run(client: ipc.Client, workspace: Path):
    task = client.call(
        "task.create",
        {
            "goal": "覆盖笔记",
            "workspaces": [{"root_path": str(workspace), "mode": "writable"}],
        },
    )["task"]
    run = client.call("run.start", {"task_id": task["task_id"]})["run"]
    return task, run


def test_approve_resumes_run_to_terminal_without_second_resume(
    root, start_daemon
) -> None:
    """人点"允许"之后，驱动线程自动续跑到终态；不需要再点一次"继续"。"""
    workspace = root / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    write_exact(note, "旧内容\n")
    model = ScriptedModel(
        [
            ModelResponse(
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        "c1",
                        "modify_text",
                        {"path": "note.md", "content": "新内容\n"},
                    )
                ],
            ),
            ModelResponse(finish_reason="end_turn", content="改好了"),
        ]
    )
    start_daemon(model)

    # 连接 A 建任务；连 A 关掉后再由连接 B 审批（两个连接，模拟 RPC + UI 两条线）。
    first = _connect(root)
    try:
        task, run = _prepare_overwrite_run(first, workspace)
        _wait_run_state(first, run["run_id"], {"waiting_approval"})
        approvals = first.call("approvals.list", {"run_id": run["run_id"]})[
            "approvals"
        ]
        assert len(approvals) == 1
        approval_id = approvals[0]["approval_id"]
        assert approvals[0]["state"] == "pending"
    finally:
        first.close()

    second = _connect(root)
    try:
        decided = second.call(
            "approvals.decide", {"approval_id": approval_id, "decision": "approve"}
        )
        assert decided["approval"]["state"] == "approved"
        # resumed 只表示"已交给驱动线程"，不表示"已经跑起来了"。
        assert decided["resumed"] is True
        final = _wait_terminal(second, run["run_id"])
        assert final["state"] == "succeeded"
        assert final["effect_state"] == "applied"
        assert note.read_bytes() == "新内容\n".encode()
        # 审批被消费，不会在队列里留下"批了但没执行"的废单。
        assert second.call("approvals.list", {"run_id": run["run_id"]})[
            "approvals"
        ] == []
    finally:
        second.close()


def test_control_plane_stays_responsive_while_driver_runs_long_task(
    root, start_daemon
) -> None:
    """一个 Run 在跑长任务时，另一条连接仍能查询与审批（PRD §4.2 的并发口径）。

    驱动线程是单线程，但**控制面不排在它后面**：两者各持一条 SQLite 连接，
    只靠事务串行化。这里用"长任务 1 秒模型延迟 + 控制面响应 <0.5s"把这件事
    钉死——如果哪天有人在主循环里等驱动，延迟断言会立刻红。
    """
    workspace = root / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    write_exact(note, "旧内容\n")
    model = RouteModel(
        {
            "要审批": [
                (
                    0.0,
                    ModelResponse(
                        finish_reason="tool_use",
                        tool_calls=[
                            ToolCall(
                                "c1",
                                "modify_text",
                                {"path": "note.md", "content": "新内容\n"},
                            )
                        ],
                    ),
                ),
                (0.0, ModelResponse(finish_reason="end_turn", content="改好了")),
            ],
            "长任务": [
                (1.0, ModelResponse(finish_reason="end_turn", content="慢但完成了"))
            ],
        }
    )
    start_daemon(model)
    client = _connect(root)
    try:
        short = client.call(
            "task.create",
            {
                "goal": "要审批",
                "workspaces": [{"root_path": str(workspace), "mode": "writable"}],
            },
        )["task"]
        short_run = client.call("run.start", {"task_id": short["task_id"]})["run"]
        _wait_run_state(client, short_run["run_id"], {"waiting_approval"})
        approval_id = client.call(
            "approvals.list", {"run_id": short_run["run_id"]}
        )["approvals"][0]["approval_id"]

        # 长任务用另一个目录：两个 Run 抢同一个 workspace 会先撞租约，
        # 那样测到的是"排队重试"，不是"驱动线程阻塞在模型调用里"。
        long_workspace = root / "ws-long"
        long_workspace.mkdir()
        long = client.call(
            "task.create",
            {
                "goal": "长任务",
                "workspaces": [{"root_path": str(long_workspace), "mode": "writable"}],
            },
        )["task"]
        long_run = client.call("run.start", {"task_id": long["task_id"]})["run"]

        # 驱动线程此刻正阻塞在 1s 的模型调用里；控制面必须照常作答。
        started = time.monotonic()
        state = client.call("state", {})
        decided = client.call(
            "approvals.decide", {"approval_id": approval_id, "decision": "approve"}
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, f"控制面被驱动线程拖住了（{elapsed:.2f}s）"
        assert decided["resumed"] is True
        run_ids = {run["run_id"] for run in state["runs"]}
        assert {short_run["run_id"], long_run["run_id"]} <= run_ids

        # 两条 Run 都要跑到终态：审批不会被"另一条长任务"饿死。
        assert _wait_terminal(client, long_run["run_id"])["state"] == "succeeded"
        assert _wait_terminal(client, short_run["run_id"])["state"] == "succeeded"
        assert note.read_bytes() == "新内容\n".encode()
    finally:
        client.close()


# --------------------------------------------------------------------------- 订阅与游标


def test_subscribe_history_then_incremental_and_reconnect_without_gaps(
    root, start_daemon
) -> None:
    start_daemon(ScriptedModel([ModelResponse(finish_reason="end_turn", content="ok")]))
    subscriber = _connect(root)
    actor = _connect(root)
    try:
        assert subscriber.subscribe(0)["from_seq"] == 0
        task = actor.call("task.create", {"goal": "产生事件"})["task"]
        run = actor.call("run.start", {"task_id": task["task_id"]})["run"]
        _wait_terminal(actor, run["run_id"])

        expected = _db_events(root)
        history = _collect_events(subscriber, expected=len(expected))
        history_seqs = [event["seq"] for event in history]
        assert history_seqs == [event.seq for event in expected]
        assert history_seqs == sorted(set(history_seqs)), "历史补洞不许重复/乱序"
        last = history_seqs[-1]
        subscriber.close()

        # 断线重连带游标：只要 seq >= last+1 的，不许从头再来，也不许跳过。
        resumed = _connect(root)
        try:
            assert resumed.subscribe(last + 1)["from_seq"] == last + 1
            second_task = actor.call("task.create", {"goal": "重连后的事件"})["task"]
            new_events = _db_events(root)
            want = [event.seq for event in new_events if event.seq >= last + 1]
            got = _collect_events(resumed, expected=len(want))
            got_seqs = [event["seq"] for event in got]
            assert got_seqs == want
            assert got_seqs[0] == last + 1, "游标补齐的第一条必须正好是断点"
            assert any(
                event["type"] == "task.created" for event in got
            ), "重连后产生的新事件必须被推送到"
            assert second_task["task_id"]
        finally:
            resumed.close()
    finally:
        subscriber.close()
        actor.close()


def test_daemon_restart_keeps_event_seq_and_replays_history(root, start_daemon) -> None:
    workspace = root / "ws"
    workspace.mkdir()
    first = start_daemon(
        ScriptedModel([ModelResponse(finish_reason="end_turn", content="第一轮")])
    )
    client = _connect(root)
    try:
        task = client.call(
            "task.create",
            {
                "goal": "重启前",
                "workspaces": [{"root_path": str(workspace), "mode": "writable"}],
            },
        )["task"]
        run = client.call("run.start", {"task_id": task["task_id"]})["run"]
        _wait_terminal(client, run["run_id"])
    finally:
        client.close()
    head_before = _db_events(root)[-1].seq
    assert first.stop_and_wait(timeout=5.0)

    second = start_daemon(
        ScriptedModel([ModelResponse(finish_reason="end_turn", content="第二轮")])
    )
    client = _connect(root)
    try:
        assert client.welcome["event_seq_head"] == head_before
        client.subscribe(0)
        replay = _collect_events(client, expected=head_before)
        assert [event["seq"] for event in replay] == list(range(1, head_before + 1))
        assert client.call("state", {})["event_seq_head"] == head_before
    finally:
        client.close()
    assert second.stop_and_wait(timeout=5.0)


# --------------------------------------------------------------------------- 单实例


def test_second_daemon_on_same_data_root_is_refused(root, start_daemon) -> None:
    start_daemon(ScriptedModel([]))
    thread, errors = _serve_thread(root, ScriptedModel([]))
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ipc.IpcError)
    assert not isinstance(errors[0], ipc.ProtocolError)
    # 第一个实例必须仍然可用：拒绝第二个不等于自伤。
    client = _connect(root)
    try:
        assert client.call("state", {})["event_seq_head"] >= 0
    finally:
        client.close()


def test_stale_lock_is_taken_over(root, start_daemon) -> None:
    """残留锁（连不上端点）可以接管：机器重启/崩溃后不该永远起不来。"""
    stale = _fake_stale_lock(root)
    assert stale.exists()
    harness = start_daemon(ScriptedModel([]))
    payload = daemon._read_lock(stale)  # noqa: SLF001 - 测试要核对锁内容
    assert payload is not None
    assert payload["instance_id"] != "stale_instance"
    assert ipc.probe(root) is True
    client = _connect(root)
    try:
        assert client.welcome["instance_id"] == payload["instance_id"]
    finally:
        client.close()
    assert harness.stop_and_wait(timeout=5.0)
    assert not stale.exists(), "正常退出必须把锁一起释放"


def test_two_daemons_started_concurrently_only_one_wins(root) -> None:
    """§4.4 的竞态窗口：`O_CREAT|O_EXCL` 只让一个创建锁，另一个退让复用。"""
    gate = threading.Barrier(2)
    results: list[tuple[threading.Event, list[BaseException]]] = []
    stop_events: list[threading.Event] = []

    inner: list[threading.Thread] = []
    outer: list[threading.Thread] = []

    def contender() -> None:
        ready = threading.Event()
        errors: list[BaseException] = []
        stop = threading.Event()
        results.append((ready, errors))
        stop_events.append(stop)

        def target() -> None:
            try:
                gate.wait()
                daemon.serve(
                    root,
                    model=ScriptedModel([]),
                    driver_interval_s=0.02,
                    on_ready=ready.set,
                    stop_event=stop,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                ready.set()

        thread = threading.Thread(target=target, daemon=True)
        inner.append(thread)
        thread.start()

    for _ in range(2):
        thread = threading.Thread(target=contender, daemon=True)
        outer.append(thread)
        thread.start()
    for thread in outer:
        thread.join(5.0)
    for ready, _errors in results:
        assert ready.wait(15.0), "两个竞争者都没有得出结论"
    winners = [pair for pair in results if not pair[1]]
    losers = [pair for pair in results if pair[1]]
    assert len(winners) == 1, f"应当恰好一个赢，实际 winners={len(winners)}"
    assert len(losers) == 1
    assert isinstance(losers[0][1][0], ipc.IpcError)
    for stop in stop_events:
        stop.set()
    for thread in inner:
        thread.join(5.0)
    assert all(not thread.is_alive() for thread in inner), "赢家没有退出"
    assert not daemon.lock_path(root).exists()


# --------------------------------------------------------------------------- 恢复


def _make_run(log: EventLog, stack, workspace: Path, *, state: str) -> str:
    task = stack.tasks.create_task(
        "改文件", [{"root_path": str(workspace), "mode": "writable"}]
    )
    run = stack.tasks.start_run(
        task.task_id,
        payload={"task": "改文件", "out": "", "limits": {}, "tools": []},
    )
    stack.tasks.transition(run.run_id, state)
    return run.run_id


def test_unresolved_intent_becomes_reconciling_not_success(root, start_daemon) -> None:
    """有 intent 无 result、且磁盘状态无法核实 → reconciling，绝不判成功。"""
    workspace = root / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    write_exact(note, "原始内容\n")
    log = EventLog(root / "events.db")
    stack = stack_for(log, data_root=root)
    try:
        run_id = _make_run(log, stack, workspace, state=RUN_RUNNING)
        args = {"path": "note.md", "content": "agent 想写的内容\n"}
        # 写前证据存在（说明确实进入了写入阶段），但磁盘上是第三种内容。
        stack.snapshots.init_run(run_id)
        stack.snapshots.record_preimage(
            run_id,
            "c1",
            "note.md",
            before="原始内容\n".encode(),
            expected_after=sha256_hex("agent 想写的内容\n".encode()),
            root=workspace,
        )
        write_exact(note, "完全不相干的内容\n")
        log.append(
            run_id,
            "tool.intent",
            {
                "call_id": "c1",
                "tool_id": "modify_text",
                "args": args,
                "args_hash": sha256_hex(canonical_bytes(args)),
            },
            actor_kind="core",
            legacy_write=True,
        )
        # 崩溃留下的作业：启动恢复必须 park 它，否则驱动线程会把它捡起来继续写。
        stack.claims.enqueue(run_id)
    finally:
        log.close()

    start_daemon(ScriptedModel([]))
    client = _connect(root)
    try:
        run = client.call("run.status", {"run_id": run_id})["run"]
        assert run["state"] == RUN_RECONCILING
        assert run["outcome"] is None, "不确定不许写成任何结论"
        events = client.call("events.run", {"run_id": run_id})["events"]
        types = [event["type"] for event in events]
        assert "recovery.blocked" in types
        unknown = [
            event
            for event in events
            if event["type"] == "tool.result"
            and event["payload"].get("effect_state") == "unknown"
        ]
        assert unknown, "无法核实的调用必须留痕为 unknown"
        assert unknown[0]["payload"]["output"].get("recovered") is True
        # 作业被 park 成 waiting：没有驱动者会碰它。
        verify = EventLog(root / "events.db")
        try:
            job = JobStore(verify).job_for_run(run_id)
        finally:
            verify.close()
        assert job is not None and job.state == "waiting"
    finally:
        client.close()


def test_waiting_approval_is_never_resumed_by_recovery(root, start_daemon) -> None:
    """自动续跑 waiting_approval 的 Run 等于绕过审批（§5 明写）。"""
    workspace = root / "ws"
    workspace.mkdir()
    log = EventLog(root / "events.db")
    stack = stack_for(log, data_root=root)
    try:
        run_id = _make_run(log, stack, workspace, state=RUN_WAITING_APPROVAL)
        job = stack.claims.enqueue(run_id)
        claim = stack.claims.claim(job.job_id)
        stack.claims.park(claim, reason=RUN_WAITING_APPROVAL)
    finally:
        log.close()

    start_daemon(
        ScriptedModel([ModelResponse(finish_reason="end_turn", content="不该跑到这里")])
    )
    client = _connect(root)
    try:
        # 给驱动线程几个轮询周期：它若认领了 waiting 作业，状态就会变。
        time.sleep(0.4)
        run = client.call("run.status", {"run_id": run_id})["run"]
        assert run["state"] == RUN_WAITING_APPROVAL
        assert run["outcome"] is None
    finally:
        client.close()


# --------------------------------------------------------------------------- 关闭


def test_shutdown_finishes_quickly_and_leaves_clean_state(root, start_daemon) -> None:
    harness = start_daemon(ScriptedModel([]))
    client = _connect(root)
    try:
        welcome = client.welcome
        assert welcome["instance_id"]
    finally:
        client.close()
    endpoint = ipc.endpoint_path(root)
    started = time.monotonic()
    assert daemon.request_shutdown(root, reason="user", timeout=5.0) is True
    assert harness.thread.join(5.0) is None, "serve() 没有在 5 秒内返回"
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert not ipc.probe(root), "端点还活着"
    if os.name != "nt":
        assert not Path(endpoint).exists(), "socket 文件没有被清掉"
    assert not daemon.lock_path(root).exists(), "锁没释放，下次会起不来"
    assert harness.errors == []

    # 干净退出之后必须能立刻再起一个（否则"重启后恢复"就是空话）。
    again = start_daemon(ScriptedModel([]))
    assert again.stop_and_wait(timeout=5.0)


def test_request_shutdown_without_daemon_returns_false(root) -> None:
    assert daemon.request_shutdown(root, timeout=0.5) is False


def test_shutdown_returns_when_driver_thread_is_stuck(
    root, monkeypatch, capsys
) -> None:
    """驱动线程卡在模型调用里时：shutdown 仍要按时返回，且不跨线程关它的连接。

    `_shutdown` 的 `driver_stopped` 守卫就是为这条测试存在的：从 serve 线程关掉
    一条还可能被驱动线程使用的 SQLite 连接，会得到一个后台线程里没人看得到的
    ProgrammingError，还会截断一次写入。宁可泄漏连接等进程退出回收。
    """
    monkeypatch.setattr(daemon, "JOIN_TIMEOUT_S", 0.3)
    model = BlockingModel()
    harness = Harness(root, model)
    harness.start()
    client = _connect(root)
    try:
        task = client.call("task.create", {"goal": "会被卡住"})["task"]
        run = client.call("run.start", {"task_id": task["task_id"]})["run"]
        assert model.entered.wait(10.0), "模型调用没有开始，测试前提不成立"
    finally:
        client.close()

    started = time.monotonic()
    assert daemon.request_shutdown(root) is True
    assert harness.thread.join(5.0) is None, "serve() 被卡住的驱动线程拖住了"
    assert time.monotonic() - started < 5.0
    assert not daemon.lock_path(root).exists(), "卡住的驱动线程不该挡住锁释放"

    # 放行模型调用，让残留的驱动线程自然收尾；期间不许出现跨线程关连接的错误。
    model.release.set()
    time.sleep(0.5)
    err = capsys.readouterr().err
    assert "ProgrammingError" not in err
    assert run["run_id"]


@pytest.mark.skipif(
    os.name == "nt", reason="Windows 上 os.kill 的 SIGTERM 不是优雅退出路径"
)
def test_sigterm_exits_zero_and_releases_lock(root) -> None:
    """SIGTERM 是第三条入口：信号 → 停止标志 → 同一条清理路径。

    用子进程而不是在测试进程里 raise：信号处理器只能在主线程装，pytest 主线程
    不该被测试改写。这里验的是"进程收到 SIGTERM 后正常返回（退出码 0）"，
    不是"被信号打死（-15）"。
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {src!r})\n"
        "from pathlib import Path\n"
        "from tianshu import daemon\n"
        "from tianshu.model import ScriptedModel\n"
        f"daemon.serve(Path({str(root)!r}), model=ScriptedModel([]),"
        " on_ready=lambda: print('READY', flush=True))\n"
    )
    env = dict(os.environ)
    env["TIANSHU_SANDBOX_BACKEND"] = "local_fs"
    proc = subprocess.Popen(  # noqa: S603 - 测试自己拼的命令，没有外部输入
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert "READY" in line, f"子进程没有报就绪：{line!r}"
        assert ipc.probe(root) is True
        proc.terminate()  # SIGTERM
        assert proc.wait(timeout=5.0) == 0, "SIGTERM 应当走正常退出路径（退出码 0）"
    finally:
        if proc.poll() is None:  # pragma: no cover - 只在测试失败时走到
            proc.kill()
            proc.wait(timeout=5.0)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    assert not daemon.lock_path(root).exists(), "SIGTERM 路径也必须释放锁"


# --------------------------------------------------------------------------- 就绪顺序（#63）


def test_on_ready_waits_for_self_connect_proof(root, monkeypatch) -> None:
    """就绪标记必须晚于"自连探测通过"——issue #63 的顺序回归。

    注入：把 `ipc.self_connect` 换成一个"进来就卡住、等人放行"的探针。修复后
    daemon 必须停在就绪屏障里，`on_ready` 不许落盘；放行后才宣告就绪。
    负控：把 `_await_endpoint_ready()` 的回退顺序去掉（旧实现），`on_ready`
    会在自连探测之前就置位 → 本用例红。
    """
    entered = threading.Event()
    release = threading.Event()
    real_self_connect = ipc.self_connect

    def gated(data_root, *, timeout: float = 1.0) -> bool:
        entered.set()
        assert release.wait(20.0)
        return real_self_connect(data_root, timeout=timeout)

    monkeypatch.setattr(ipc, "self_connect", gated)
    harness = Harness(root, ScriptedModel([]))
    try:
        harness.thread.start()
        deadline = time.monotonic() + 10.0
        while (
            time.monotonic() < deadline
            and not entered.is_set()
            and not harness.ready.is_set()
        ):
            time.sleep(0.01)
        # 先断言"标记没有提前"，再断言"确实走到了自连探测"：负控（旧顺序）
        # 会停在第一条上，红的样子就是 issue #63 的根因——就绪标记比连接能力早。
        assert not harness.ready.is_set(), (
            "自连探测还没通过，on_ready 就已经落盘——就绪标记在撒谎"
        )
        assert entered.is_set(), "daemon 没有走到自连探测"
        release.set()
        assert harness.ready.wait(10.0), "放行自连探测后 daemon 没有就绪"
        assert harness.errors == []
        client = _connect(root)
        try:
            assert client.call("state", {})["event_seq_head"] >= 0
        finally:
            client.close()
    finally:
        release.set()
        harness.stop_and_wait(timeout=5.0)


def test_on_ready_is_not_called_when_endpoint_never_proves_connectable(
    root, monkeypatch
) -> None:
    """端点始终证明不了可用：宁可启动失败 + 清锁，也不许宣告就绪。"""
    monkeypatch.setattr(ipc, "self_connect", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "SELF_CONNECT_BUDGET_S", 0.3)
    monkeypatch.setattr(daemon, "SELF_CONNECT_TIMEOUT_S", 0.1)
    ready = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            daemon.serve(root, model=ScriptedModel([]), on_ready=ready.set)
        except BaseException as exc:  # noqa: BLE001 - 把启动失败带回断言
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(15.0)
    assert not thread.is_alive()
    assert not ready.is_set(), "端点没证明可用，on_ready 不能被调用"
    assert errors, "启动失败必须抛出来，不许当成正常退出"
    assert isinstance(errors[0], ipc.IpcError)
    assert "自连探测失败" in str(errors[0])
    assert not daemon.lock_path(root).exists(), "失败退出也要释放单实例锁"


def test_accept_thread_fatal_error_stops_daemon_and_reports_reason(
    root, start_daemon, monkeypatch
) -> None:
    """accept 线程异常退出必须**带着原因让 daemon 退出**，不许静默变僵尸。"""
    real_accept = ipc.Server.accept
    broken = threading.Event()

    def flaky(self):
        if broken.is_set():
            raise RuntimeError("listener fd 坏了")
        return real_accept(self)

    monkeypatch.setattr(ipc.Server, "accept", flaky)
    harness = start_daemon(ScriptedModel([]))
    # 先让 daemon 完整就绪（上面的探活/自连都走真实 accept），再打破监听。
    broken.set()
    assert ipc.probe(root) is True, "这一步只负责把阻塞中的 accept 叫醒"
    assert harness.thread.join(15.0) is None, "accept 线程坏了，daemon 还在跑"
    assert harness.errors, "accept 线程异常退出必须把原因交给调用方"
    message = str(harness.errors[0])
    assert "accept 线程不可用" in message
    assert "listener fd 坏了" in message
    assert not daemon.lock_path(root).exists(), "致命退出也要释放锁"
    assert not ipc.probe(root, timeout=0.2), "退出后端点不能还活着"


def test_handshake_failures_on_accept_are_retryable(
    root, start_daemon, monkeypatch
) -> None:
    """握手阶段失败（探活连接就是这样）是可重试的，不许升级成致命退出。"""
    real_accept = ipc.Server.accept
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ipc.ProtocolError("模拟握手失败（可重试）")
        return real_accept(self)

    monkeypatch.setattr(ipc.Server, "accept", flaky)
    start_daemon(ScriptedModel([]))
    client = _connect(root)
    try:
        assert client.call("state", {})["event_seq_head"] >= 0
    finally:
        client.close()
    assert calls["n"] >= 2, "应当先失败两次再恢复正常"


def test_stop_event_shuts_down_without_rpc(root) -> None:
    """`stop_event` 是第三条入口（Host/测试用），必须和信号/RPC 统一到同一路径。"""
    harness = Harness(root, ScriptedModel([]))
    harness.start()
    assert harness.stop_and_wait(timeout=5.0)
    assert not daemon.lock_path(root).exists()


def test_client_disconnect_does_not_spin_a_cpu_core(root) -> None:
    """客户端断开后，daemon 的读线程必须退出——不能空转烧掉一个核。

    这是实测到的真 Bug，不是假想：演示里 `client.close()` 之后 daemon 持续
    吃满一个核（`poll()` 立刻报可读 → `recv_bytes()` 立刻 EOF 的循环），
    直到进程被杀。根因是 `ipc.ServerConnection.recv()` 对"超时"和"断线"都
    返回值 None，读循环无法区分，只能永远 continue。

    这里用子进程跑真 daemon，再用它自己的 `time.process_time()` 记账：
    空转时 1.5 秒会吃掉约 1.5 秒 CPU；正常时只有驱动线程的轻微轮询开销。
    "读线程退出了"是修法，"CPU 没被烧"是用户能感觉到的后果，两个都验。
    """
    script = root / "quiet_daemon.py"
    write_exact(
        script,
        "\n".join(
            [
                "import json, sys, threading, time",
                "from pathlib import Path",
                "",
                "from tianshu import daemon, ipc",
                "from tianshu.capability import CapabilityProfile",
                "from tianshu.model import ModelIdentity, ModelResponse",
                "",
                "",
                "class Quiet:",
                "    def capability_profile(self):",
                "        return CapabilityProfile(provider='test', model='quiet',",
                "                                  supports_tool_calling=True,",
                "                                  declared_by='cpu spin test')",
                "    def price_identity(self):",
                "        return ModelIdentity(adapter='test', vendor=None, model='quiet')",
                "    def complete(self, messages, tools):",
                "        return ModelResponse(finish_reason='end_turn', content='')",
                "",
                "",
                "root = Path(sys.argv[1])",
                "stop = threading.Event()",
                "ready = threading.Event()",
                "",
                "",
                "def run():",
                "    try:",
                "        daemon.serve(root, model=Quiet(), on_ready=ready.set,",
                "                     stop_event=stop)",
                "    except BaseException as exc:  # noqa: BLE001",
                "        print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), flush=True)",
                "        ready.set()",
                "",
                "",
                "threading.Thread(target=run, daemon=True).start()",
                "assert ready.wait(20), 'daemon 没在 20s 内就绪'",
                "client = ipc.Client(root, timeout=5)",
                "client.connect()",
                "client.call('state')",
                "client.close()  # 就是这一步之后读线程原本会空转",
                "time.sleep(0.3)",
                "t0 = time.process_time()",
                "time.sleep(1.5)",
                "delta = time.process_time() - t0",
                "stop.set()",
                "time.sleep(0.1)",
                "print(json.dumps({'cpu_delta': delta}), flush=True)",
                "",
            ]
        ),
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src, "TIANSHU_SANDBOX_BACKEND": "local_fs"}
    proc = subprocess.run(  # noqa: S603 - 测试自己拼的命令，没有外部输入
        [sys.executable, str(script), str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=90,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 0, f"子进程失败：{proc.stderr[-2000:]}"
    assert lines, f"子进程没有输出：{proc.stderr[-2000:]}"
    report = json.loads(lines[-1])
    assert "error" not in report, report
    delta = float(report["cpu_delta"])
    assert delta < 0.5, (
        f"客户端断开后 daemon 在 1.5s 内吃了 {delta:.2f}s CPU——读线程在空转"
    )

