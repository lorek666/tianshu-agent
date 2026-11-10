"""M5 端到端：Daemon ↔ IPC ↔ Host ↔ 真 UI 产物。

这一层回答的是 M5 的退出条件本身，而不是某个模块的内部正确性：

1. **关掉客户端，任务继续**：把 UI（客户端）断开，Run 仍然推进到终态。
   用一条"模型阻塞、直到测试放行"的假模型证明——如果 daemon 依赖客户端，
   它就会卡住。
2. **重启后恢复**：杀进程再起一个，任务/事件/待批审批都还在，且不重复副作用。
3. **审批真的能通过 IPC 落地**：Run 停在 `waiting_approval` → `approvals.decide`
   → 自动续跑 → 文件恰好被删一次。
4. **Host 用真 UI 产物把 RPC 转给真 Daemon**：不是假 daemon，也不是手写 HTML。

真崩溃（SIGKILL）用子进程来做——"重启后恢复"这句话如果只用线程 stop 模拟，
测的是优雅退出，不是崩溃恢复。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tianshu import daemon, host, ipc
from tianshu.capability import CapabilityProfile
from tianshu.model import ModelIdentity, ModelResponse, ToolCall

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_UI_DIST = REPO_ROOT / "ui" / "dist"
TERMINAL = {"succeeded", "failed", "cancelled"}


# --------------------------------------------------------------------------- 夹具


class FakeModel:
    """可脚本化、可阻塞的测试模型。

    `gate` 用来把"模型正在思考"变成一个可控的时间点：测试可以先把客户端全关掉，
    再放行，从而证明"没有客户端也能继续跑"。
    """

    __test__ = False  # 不是 pytest 测试类

    def __init__(self, script: list[ModelResponse], *, gate: threading.Event | None = None):
        self._script = list(script)
        self._gate = gate

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="test", vendor=None, model="test-model")

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="test",
            model="test-model",
            supports_tool_calling=True,
            supports_parallel_tool_calls=False,
            supports_streaming=False,
            declared_by="tests/test_integration_m5.py",
        )

    def complete(self, messages, tools):  # noqa: ARG002
        if self._gate is not None:
            gate, self._gate = self._gate, None
            gate.wait(20)
        if not self._script:
            return ModelResponse(finish_reason="end_turn", content="完成")
        return self._script.pop(0)


def _short_root(prefix: str) -> Path:
    base = None if os.name == "nt" else "/tmp"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _tail(path: Path, *, max_bytes: int = 4000) -> str:
    """读文件尾巴（诊断用）：子进程日志只留最后几千字节就够定位了。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return "（没有日志文件）"
    return raw[-max_bytes:].decode("utf-8", errors="replace")


class DaemonHarness:
    """在后台线程里跑一个真 daemon，并等到端点可连。"""

    def __init__(self, root: Path, model: Any):
        self.root = root
        self.model = model
        self.errors: list[BaseException] = []
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="m5-daemon", daemon=True)

    def _run(self) -> None:
        try:
            daemon.serve(
                self.root,
                model=self.model,
                on_ready=self.ready.set,
                stop_event=self.stop,
            )
        except BaseException as exc:  # noqa: BLE001 - 测试要把异常带出来
            self.errors.append(exc)
            self.ready.set()

    def __enter__(self) -> DaemonHarness:
        self.thread.start()
        assert self.ready.wait(20), "daemon 没有在 20s 内就绪"
        assert not self.errors, self.errors
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            daemon.request_shutdown(self.root, reason="test", timeout=5)
        except (ipc.IpcError, OSError):
            self.stop.set()
        self.thread.join(10)
        if self.thread.is_alive():
            self.stop.set()
            self.thread.join(5)


@pytest.fixture()
def root():
    path = _short_root("m5int-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _client(root: Path) -> ipc.Client:
    client = ipc.Client(root, timeout=10)
    client.connect()
    return client


def _wait_run(client: ipc.Client, run_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.call("run.status", {"run_id": run_id})["run"]
        if last.get("state") in TERMINAL:
            return last
        time.sleep(0.1)
    raise AssertionError(f"Run 没有在 {timeout}s 内到终态：{last}")


def _wait_state(
    client: ipc.Client, run_id: str, state: str, *, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.call("run.status", {"run_id": run_id})["run"]
        if last.get("state") == state:
            return last
        time.sleep(0.05)
    try:
        events = client.call("events.run", {"run_id": run_id})["events"]
    except Exception:  # noqa: BLE001
        events = []
    raise AssertionError(
        f"Run 没有进入 {state}：{last}；末尾事件="
        f"{[(e['type'], e['payload'].get('error')) for e in events][-8:]}"
    )


def _make_task(client: ipc.Client, ws: Path, goal: str = "整理目录并写摘要") -> str:
    result = client.call(
        "task.create",
        {"goal": goal, "workspaces": [{"root_path": str(ws), "mode": "writable"}]},
    )
    return result["task"]["task_id"]


def _start(client: ipc.Client, task_id: str, out_name: str = "summary.md") -> str:
    return client.call("run.start", {"task_id": task_id, "out_name": out_name})["run"][
        "run_id"
    ]


SUMMARY_SCRIPT = [
    ModelResponse(
        finish_reason="tool_use", tool_calls=[ToolCall("c1", "list_files", {"path": "."})]
    ),
    ModelResponse(
        finish_reason="tool_use",
        tool_calls=[
            ToolCall(
                "c2",
                "create_report",
                {"name": "summary.md", "content": "# 摘要\n\n（集成测试）\n"},
            )
        ],
    ),
]


# --------------------------------------------------------------------------- 1. 端到端


def test_daemon_runs_task_to_terminal_state(root: Path) -> None:
    ws = root / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes(b"hello")
    with DaemonHarness(root, FakeModel(SUMMARY_SCRIPT)):
        client = _client(root)
        try:
            task_id = _make_task(client, ws)
            run_id = _start(client, task_id)
            run = _wait_run(client, run_id)
            assert run["state"] == "succeeded", run
            assert run["outcome"] == "succeeded"
            assert (ws / "summary.md").read_bytes().decode("utf-8").startswith("# 摘要")
            events = client.call("events.run", {"run_id": run_id})["events"]
            types = [event["type"] for event in events]
            assert types[0] == "run.start"
            assert "model.intent" in types and "tool.result" in types
            assert "receipt" in types, types
            assert "run.end" in types
            assert types[-1] == "run.finish"
            state = client.call("state", {})
            assert any(task["task_id"] == task_id for task in state["tasks"])
        finally:
            client.close()


def test_approval_is_decided_through_ipc_and_run_continues(root: Path) -> None:
    ws = root / "ws"
    ws.mkdir()
    (ws / "old.txt").write_bytes("要删掉的旧账单".encode())
    script = [
        ModelResponse(
            finish_reason="tool_use",
            tool_calls=[ToolCall("c1", "delete_file", {"path": "old.txt"})],
        ),
    ]
    with DaemonHarness(root, FakeModel(script)):
        client = _client(root)
        try:
            task_id = _make_task(client, ws, goal="删掉旧账单")
            run_id = _start(client, task_id)
            run = _wait_state(client, run_id, "waiting_approval")
            assert run["state"] == "waiting_approval"
            approvals = client.call("approvals.list", {"run_id": run_id})["approvals"]
            assert len(approvals) == 1, approvals
            card = approvals[0]
            assert card["action"] == "delete"
            assert card["reason"] or card["rule_id"]
            assert (ws / "old.txt").exists(), "批准前文件必须一个字节都不动"

            decided = client.call(
                "approvals.decide", {"approval_id": card["approval_id"], "decision": "approve"}
            )
            assert decided["resumed"] is True, decided

            run = _wait_run(client, run_id)
            assert run["state"] == "succeeded", run
            assert not (ws / "old.txt").exists()
            types = [
                event["type"] for event in client.call("events.run", {"run_id": run_id})["events"]
            ]
            assert "approval.requested" in types
            assert "approval.decided" in types
            assert "approval.consumed" in types
            assert "receipt" in types
            assert client.call("approvals.list", {})["approvals"] == []
        finally:
            client.close()


def test_rejected_action_terminates_and_is_not_reasked(root: Path) -> None:
    """人拒绝之后，同一动作不会再回来问第二遍（issue #40 的端到端形态）。

    这是"审批 UI 有用"的反面：如果拒绝只是把 Run 停在 waiting_approval，
    用户点多少次拒绝都不会结束，UI 上表现为一张永远消不掉的卡片。
    """
    ws = root / "ws"
    ws.mkdir()
    (ws / "old.txt").write_bytes("旧账单".encode())
    with DaemonHarness(
        root,
        FakeModel(
            [
                ModelResponse(
                    finish_reason="tool_use",
                    tool_calls=[ToolCall("c1", "delete_file", {"path": "old.txt"})],
                )
            ]
        ),
    ):
        client = _client(root)
        try:
            task_id = _make_task(client, ws, goal="删掉旧账单")
            run_id = _start(client, task_id)
            _wait_state(client, run_id, "waiting_approval")
            approval_id = client.call("approvals.list", {"run_id": run_id})["approvals"][0][
                "approval_id"
            ]
            client.call(
                "approvals.decide", {"approval_id": approval_id, "decision": "reject"}
            )
            run = _wait_run(client, run_id, timeout=20)
            assert run["state"] == "failed", run
            assert run["effect_state"] == "none_applied"
            assert (ws / "old.txt").exists(), "被拒绝的动作不许动文件"
            events = client.call("events.run", {"run_id": run_id})["events"]
            requested = [e for e in events if e["type"] == "approval.requested"]
            assert len(requested) == 1, "否决后不许再发起同一动作的审批"
            rejected = [
                e
                for e in events
                if e["type"] == "tool.result" and e["payload"].get("status") == "rejected"
            ]
            assert rejected, events
            # rule_id 在 tool.result 的 output 里（result envelope 的既有形状），
            # dispatch.rejected 上也有一个同值的顶层 rule_id。
            assert any(
                (e["payload"].get("output") or {}).get("rule_id")
                == "c4.approval_rejected"
                for e in rejected
            ), rejected
            assert any(
                e["payload"].get("rule_id") == "c4.approval_rejected"
                for e in events
                if e["type"] == "dispatch.rejected"
            )
            assert client.call("approvals.list", {})["approvals"] == []
        finally:
            client.close()


# --------------------------------------------------------------------------- 2. 关 UI 任务继续


def test_run_keeps_going_after_every_client_disconnects(root: Path) -> None:
    """关掉界面 ≠ 停任务。这是 M5 的第一条退出条件。

    做法：让模型卡在第一次调用上（`gate`），确认 Run 在 running；把客户端**关掉**；
    再放行模型。如果 daemon 的推进依赖客户端连接，Run 会永远停住。
    """
    ws = root / "ws"
    ws.mkdir()
    gate = threading.Event()
    model = FakeModel(
        [
            ModelResponse(
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        "c2",
                        "create_report",
                        {"name": "summary.md", "content": "# 关掉界面之后写的\n"},
                    )
                ],
            ),
        ],
        gate=gate,
    )
    with DaemonHarness(root, model):
        client = _client(root)
        task_id = _make_task(client, ws)
        run_id = _start(client, task_id)
        _wait_state(client, run_id, "running")
        client.close()  # 模拟关掉 UI：此后没有任何客户端
        gate.set()
        time.sleep(0.5)
        reopened = _client(root)
        try:
            run = _wait_run(reopened, run_id)
            assert run["state"] == "succeeded", run
            assert (ws / "summary.md").exists()
        finally:
            reopened.close()


# --------------------------------------------------------------------------- 3. 重启后恢复


def test_pending_approval_survives_restart_and_stays_waiting(root: Path) -> None:
    """重启恢复不能"顺手把等待审批的 Run 续跑"——那等于绕过审批。"""
    ws = root / "ws"
    ws.mkdir()
    (ws / "old.txt").write_bytes("旧账单".encode())
    script = [
        ModelResponse(
            finish_reason="tool_use",
            tool_calls=[ToolCall("c1", "delete_file", {"path": "old.txt"})],
        ),
    ]
    with DaemonHarness(root, FakeModel(script)):
        client = _client(root)
        task_id = _make_task(client, ws)
        run_id = _start(client, task_id)
        _wait_state(client, run_id, "waiting_approval")
        approval_id = client.call("approvals.list", {"run_id": run_id})["approvals"][0][
            "approval_id"
        ]
        client.close()

    # 第二个 daemon（等价于重启）：状态还在，审批还挂着，且没有被自动放行。
    with DaemonHarness(root, FakeModel([])):
        client = _client(root)
        try:
            run = client.call("run.status", {"run_id": run_id})["run"]
            assert run["state"] == "waiting_approval", run
            assert (ws / "old.txt").exists()
            approvals = client.call("approvals.list", {})["approvals"]
            assert [a["approval_id"] for a in approvals] == [approval_id]
            client.call(
                "approvals.decide", {"approval_id": approval_id, "decision": "approve"}
            )
            run = _wait_run(client, run_id)
            assert run["state"] == "succeeded", run
            assert not (ws / "old.txt").exists()
        finally:
            client.close()


def test_second_daemon_on_same_root_is_refused(root: Path) -> None:
    with DaemonHarness(root, FakeModel([])):
        second_errors: list[BaseException] = []
        ready = threading.Event()

        def second() -> None:
            try:
                daemon.serve(root, model=FakeModel([]), on_ready=ready.set)
            except BaseException as exc:  # noqa: BLE001
                second_errors.append(exc)
            finally:
                ready.set()

        thread = threading.Thread(target=second, daemon=True)
        thread.start()
        assert ready.wait(10)
        thread.join(5)
        assert second_errors, "第二个 daemon 必须被拒绝"
        assert "占用" in str(second_errors[0]) or isinstance(second_errors[0], ipc.IpcError)


@pytest.mark.skipif(
    os.name == "nt", reason="SIGKILL / AF_UNIX 语义；Windows 用 taskkill，本测试不覆盖"
)
def test_crashed_daemon_recovers_run_without_duplicate_side_effects(root: Path) -> None:
    """真 `kill -9` 后再起一个 daemon：Run 继续、事件不重、文件不被写两遍。"""
    ws = root / "ws"
    ws.mkdir()
    script = root / "crash_daemon.py"
    script.write_text(
        "\n".join(
            [
                "import sys, time",
                "from pathlib import Path",
                "from tianshu.daemon import serve",
                "",
                "class M:",
                "    def capability_profile(self):",
                "        from tianshu.capability import CapabilityProfile",
                "        return CapabilityProfile(provider='test', model='blocking',",
                "                                  supports_tool_calling=True,",
                "                                  declared_by='crash test')",
                "    def price_identity(self):",
                "        from tianshu.model import ModelIdentity",
                "        return ModelIdentity(adapter='test', vendor=None, model='blocking')",
                "    def complete(self, messages, tools):",
                "        time.sleep(30)  # 卡在模型调用里，等测试来杀",
                "        from tianshu.model import ModelResponse",
                "        return ModelResponse(finish_reason='end_turn', content='never')",
                "",
                "root = Path(sys.argv[1])",
                "# on_ready 在端点**已经能连之后**调用（契约如此），所以落一个标记文件，",
                "# 让父进程不必靠『轮询 probe』去猜子进程走到哪一步——轮询只能得出",
                "# 『还没好』，出问题时不知道该怀疑 import、锁还是 bind。",
                "serve(root, model=M(),",
                "      on_ready=lambda: (root / 'daemon-ready').write_bytes(b'1'))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    # 日志写文件，不用 `stderr=subprocess.PIPE`：管道没人读，daemon 写满 64KB
    # 缓冲区就会阻塞在 write 上——表现是"端点永远不出现"，一个跟被测语义毫无关系
    # 的死锁（慢 runner 上更容易踩到）。写文件既能避免死锁，失败时还能把尾巴带进
    # 断言消息里，不用再靠猜。
    log_path = root / "crash_daemon.log"
    log_file = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(root)],
        env=env,
        stdout=log_file,
        stderr=log_file,
    )
    log_file.close()
    try:
        ready_mark = root / "daemon-ready"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ready_mark.exists():
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        assert ready_mark.exists(), (
            "崩溃测试用的 daemon 没走到 on_ready"
            f"（returncode={proc.poll()}）；日志尾巴：\n{_tail(log_path)}"
        )
        # on_ready 之后端点就已经能连了；这里留一点余量吸收调度抖动。
        # 等的时候盯着子进程：它若已经退出，就别再白等 10 秒——「活着但端点不见」
        # 与「已经死了」是两种完全不同的故障，混在一起只会让它变成一条没法查的
        # 偶发红（issue #63）。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ipc.probe(root, timeout=0.2):
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        endpoint = ipc.endpoint_path(root)
        assert ipc.probe(root), (
            f"on_ready 已落盘但端点连不上；returncode={proc.poll()}，"
            f"endpoint={endpoint}，exists={Path(endpoint).exists()}；"
            f"日志尾巴：\n{_tail(log_path)}"
        )
        client = _client(root)
        task_id = _make_task(client, ws)
        run_id = _start(client, task_id)
        _wait_state(client, run_id, "running")
        client.close()
        before = ipc.Client(root, timeout=5)
        before.connect()
        events_before = before.call("events.run", {"run_id": run_id})["events"]
        before.close()
        proc.kill()  # 真崩溃：不是 stop_event、不是 shutdown RPC
        proc.wait(10)
        assert not ipc.probe(root, timeout=0.2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)

    with DaemonHarness(root, FakeModel(SUMMARY_SCRIPT)):
        client = _client(root)
        try:
            run = _wait_run(client, run_id)
            events_after = client.call("events.run", {"run_id": run_id})["events"]
            assert run["state"] == "succeeded", {
                "run": run,
                "events": [(e["type"], e["payload"]) for e in events_after][-12:],
            }
            assert len(events_after) >= len(events_before)
            assert [e["seq"] for e in events_after] == list(
                range(events_after[0]["seq"], events_after[0]["seq"] + len(events_after))
            ), "事件 seq 出现缺口"
            # 副作用恰好一次：receipt 只允许出现一条（这次是新建文件）。
            receipts = [e for e in events_after if e["type"] == "receipt"]
            assert len(receipts) == 1, [e["payload"] for e in receipts]
            assert (ws / "summary.md").exists()
            ignored = signal.SIGTERM  # 仅为在 Windows 上也能 import 该模块
            assert ignored == signal.SIGTERM
        finally:
            client.close()


# --------------------------------------------------------------------- 4. Host 链路


@pytest.mark.skipif(
    not (REAL_UI_DIST / "index.html").is_file(),
    reason="仓库里没有 ui/dist（先 `pnpm --dir ui run build`）",
)
def test_host_bridges_real_daemon_with_real_ui(root: Path) -> None:
    import json
    import socket

    ws = root / "ws"
    ws.mkdir()
    with DaemonHarness(root, FakeModel(SUMMARY_SCRIPT)):
        token = "integration-token"
        config = host.BridgeConfig(data_root=root, ui_dir=REAL_UI_DIST, token=token, port=0)
        bridge = host.UiBridge(config)
        bridge.serve_background()
        try:
            def http(method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
                with socket.create_connection(("127.0.0.1", bridge.port), timeout=5) as sock:
                    lines = [
                        f"{method} {path} HTTP/1.1",
                        f"Host: 127.0.0.1:{bridge.port}",
                        f"X-Tianshu-Token: {token}",
                    ]
                    if body is not None:
                        lines.append(f"Content-Length: {len(body)}")
                    lines.append("Connection: close")
                    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (body or b""))
                    raw = b""
                    while True:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                head, _, payload = raw.partition(b"\r\n\r\n")
                return int(head.split(b"\r\n")[0].split(b" ")[1]), payload

            status, payload = http(
                "POST",
                "/rpc",
                json.dumps(
                    {
                        "method": "task.create",
                        "params": {
                            "goal": "通过 Host 建任务",
                            "workspaces": [{"root_path": str(ws), "mode": "writable"}],
                        },
                    }
                ).encode("utf-8"),
            )
            assert status == 200, payload
            task_id = json.loads(payload)["result"]["task"]["task_id"]

            status, page = http("GET", "/")
            assert status == 200
            assert f'content="{token}"'.encode() in page

            status, state_raw = http("GET", "/state")
            assert status == 200
            assert any(
                task["task_id"] == task_id for task in json.loads(state_raw)["result"]["tasks"]
            )
        finally:
            bridge.stop()


@pytest.mark.skipif(
    not (REAL_UI_DIST / "index.html").is_file(),
    reason="仓库里没有 ui/dist（先 `pnpm --dir ui run build`）",
)
def test_event_stream_ends_when_daemon_goes_away(root: Path) -> None:
    """daemon 断开订阅连接后，SSE 必须结束，不能变成"空转发心跳"的永动机。

    实测过的真 Bug：`Client.next_event()` 在断线时也返回 None，SSE 循环把
    None 当"这一刻没事件"，于是 `poll()→EOF` 的循环烧满一个核，而且浏览器
    永远等不到流结束（UI 会一直显示"事件流已连接"，实际早就没有事件了）。
    """
    ws = root / "ws"
    ws.mkdir()
    token = "stream-token"
    harness = DaemonHarness(root, FakeModel([]))
    with harness:
        config = host.BridgeConfig(data_root=root, ui_dir=REAL_UI_DIST, token=token, port=0)
        bridge = host.UiBridge(config)
        bridge.serve_background()
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            request = (
                f"GET /events?since_seq=0 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{bridge.port}\r\n"
                f"X-Tianshu-Token: {token}\r\n"
                "Accept: text/event-stream\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            raw = b""
            while b"\r\n\r\n" not in raw:
                raw += sock.recv(65536)
            assert b"200" in raw.split(b"\r\n")[0], raw[:200]

            # 让 daemon 正常退出：它会先关掉所有已接受的连接，再关 listener。
            assert daemon.request_shutdown(root, reason="test", timeout=5)

            sock.settimeout(5)
            deadline = time.monotonic() + 5
            ended = False
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:  # 5s 内一直有字节过来 = 在空转发心跳
                    break
                if not chunk:
                    ended = True
                    break
            assert ended, "daemon 断开后事件流没有结束（SSE 在空转发心跳）"
        finally:
            if sock is not None:
                sock.close()
            bridge.stop()


@pytest.mark.skipif(
    not (REAL_UI_DIST / "index.html").is_file(),
    reason="仓库里没有 ui/dist（先 `pnpm --dir ui run build`）",
)
def test_host_stop_stops_daemon_too(root: Path) -> None:
    """`tianshu host stop` 必须连 Daemon 一起收掉，不能留孤儿进程。

    回归（实测）：`run_host` 的退出回调先置 `stop_event`，主线程立刻结束进程，
    把"正在关 Daemon"的清理线程一起带走——`stop` 报"Host 已退出"，Daemon 却
    继续占着 socket、继续能接活。必须用**真子进程**才测得出来：在 pytest 进程里
    跑 run_host，主线程退出后进程还活着，清理线程不会死，Bug 就躲过去了。
    """
    ws = root / "ws"
    ws.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "TIANSHU_SANDBOX_BACKEND": "local_fs",
    }
    proc = subprocess.Popen(  # noqa: S603 - 测试自己拼的命令，没有外部输入
        [
            sys.executable,
            "-m",
            "tianshu",
            "host",
            "run",
            "--data-root",
            str(root),
            "--port",
            "0",
            "--ui-dir",
            str(REAL_UI_DIST),
            "--model",
            "scripted",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        deadline = time.monotonic() + 60
        info = None
        while time.monotonic() < deadline:
            info = host.read_host_info(root)
            if info is not None and host._http_alive(info):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert info is not None and host._http_alive(info), (
            f"Host 没能在 60s 内起来（returncode={proc.poll()}）；"
            f"daemon 日志尾巴：\n{_tail(root / 'logs' / 'daemon.log')}"
        )
        assert ipc.probe(root, timeout=1.0), "Host 起来了但 Daemon 没起来"

        assert host.stop_host(root, timeout=20) == 0, "stop_host 没能让 Host 退出"
        assert proc.wait(timeout=20) == 0, "Host 进程退出码不是 0"
        # 不留宽限：Host 退出这一步本身就必须发生在 Daemon 收掉之后，
        # "退出后再等 Daemon 慢慢死"是错的语义（孤儿窗口里它还能接活）。
        assert not ipc.probe(root, timeout=0.5), "Host 退出后 Daemon 还活着（孤儿进程）"
        assert host.read_host_info(root) is None, "Host 退出后 host.lock 没清掉"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        try:
            daemon.request_shutdown(root, reason="test", timeout=5)
        except (ipc.IpcError, OSError):
            pass
        host.clear_host_info(root)
