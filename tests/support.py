"""测试用的"本地人审"替身。

M2 之后覆盖/删除类写入必须有批准才能执行（PRD §2.3）。测试要证明的往往是
"批准之后到底做了什么"，而不是"没批准会怎样"——后者各有用例单独覆盖。
这个模块把"人在这条命令上敲 approve"这一步自动化，其余路径全走真实服务层：

    看到待批 → approvals.decide(approved) → resume_run

它不是库的一部分（所以不在 `src/` 里）：在生产代码里放一个自动批准函数，
迟早会有人把它当默认路径用。
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

from tianshu import ipc
from tianshu.capability import CapabilityProfile
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, Stack, build_stack
from tianshu.model import Model
from tianshu.permissions import HumanIdentity
from tianshu.snapshot import SnapshotStore

#: 所有测试子进程的 stdout/stderr 解码参数。
#:
#: 产品侧 CLI 已经把输出钉成 UTF-8（`cli._force_utf8_streams`），父进程若按
#: locale 编码解就会得到 mojibake，甚至在 cp1252 的未定义字节上抛
#: `UnicodeDecodeError`。`errors="replace"` 是给"子进程在 import 阶段就崩"
#: 这种情形留的后路：那时 traceback 用的是 locale 编码，替换字符也总比看不到
#: 真实失败信息好。
CHILD_TEXT: dict[str, object] = {
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}


def write_exact(path: Path, text: str) -> None:
    """按 UTF-8 **字节**落盘，不经过换行翻译。

    证据链（`observe_state` / 快照 preimage / 凭据 before|after_state）算的是
    `sha256(文件原始字节)`。测试夹具若用 `Path.write_text()`，Windows 会在文本
    模式里把 `\n` 翻成 `\r\n`，于是"期望哈希"和磁盘上的字节对不上——那是
    夹具在骗自己，不是产品坏了。所有参与哈希的夹具一律走这里。
    """
    path.write_bytes(text.encode("utf-8"))


def stack_for(
    log: EventLog,
    *,
    snapshots: SnapshotStore | None = None,
    data_root: Path | None = None,
) -> Stack:
    return build_stack(log, snapshots=snapshots, data_root=data_root)


def engine_for(
    stack: Stack, model: Model, *, limits: Limits | None = None
) -> AgentLoop:
    return AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        model=model,
        limits=limits,
        budget=getattr(stack, "budget", None),
        # 与生产装配一致：测试替身也复用同一个记忆投影，避免"测试里
        # 默认目录碰巧对上、自定义目录才炸"的假通过。
        memory=stack.memory,
    )


def tool_capable_profile(provider: str, model: str = "test-double") -> CapabilityProfile:
    """测试替身的能力声明（F9a：每个模型都要如实声明自己会什么）。

    这些替身在用例里就是要返回工具调用的，所以声明 `tool_calling`；其余能力
    留**未声明**——替身不假装自己没有的东西，未声明 ≠ 支持这条对它同样成立。
    """
    return CapabilityProfile(
        provider=provider,
        model=model,
        supports_tool_calling=True,
        declared_by="tests/support.py（测试替身）",
    )


def pending_approvals(stack: Stack, run_id: str) -> list[str]:
    return [a.approval_id for a in stack.approvals.pending(run_id=run_id)]


def approve_pending(stack: Stack, run_id: str) -> list[str]:
    """批准这个 Run 当前所有待批请求。返回被批准的 id。"""
    ids = pending_approvals(stack, run_id)
    actor = HumanIdentity.local_cli()
    for approval_id in ids:
        stack.approvals.decide(approval_id, decision="approved", actor=actor)
    return ids


class HangupPeer:
    """POSIX 假对端：只 listen、**不做认证握手**，accept 到连接就立刻关闭。

    复现的是真实观测到的时序窗口（CI macOS 实测）：daemon 正在退出时，端点
    （socket 文件）还在、内核 backlog 还接得住 connect，但已经没有进程会完成
    `multiprocessing` 的 auth challenge。客户端 `_Socket()` 于是在
    `answer_challenge()` 里读到 EOF —— 抛的是裸 `EOFError`。

    这不是理论构造：`test_event_stream_ends_when_daemon_goes_away` 自己关掉
    daemon 后，`DaemonHarness.__exit__` 又补一次 `request_shutdown()`，正好撞上
    这个窗口；`EOFError` 既不是 `OSError` 也不是 `ipc.IpcError`，穿过
    `Client.connect()` 的重试分支把整条测试打崩（`1 failed, 1529 passed`）。

    Windows 命名管道没有等价的"裸句柄"（`Listener.accept()` 必然先握手），所以
    这个替身只在 POSIX 上用；Windows 侧的映射由 `tests/test_ipc.py` 里打桩
    `ipc._Socket` 的可移植用例覆盖，本类在 Windows 上直接拒绝构造。
    """

    def __init__(self, data_root: Path) -> None:
        if os.name == "nt":
            raise RuntimeError("HangupPeer 只在 POSIX 可用（Windows 用打桩用例覆盖）")
        self.data_root = Path(data_root)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> HangupPeer:
        ipc.run_dir(self.data_root).mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(ipc.endpoint_path(self.data_root))
            sock.listen(ipc.LISTEN_BACKLOG)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, name="hangup-peer", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            # 关键：**不**做 auth challenge，直接关。客户端会在握手读里得到 EOF。
            conn.close()

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()  # 唤醒阻塞中的 accept()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        if self._stop.is_set():
            try:
                Path(ipc.endpoint_path(self.data_root)).unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> HangupPeer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = [
    "CHILD_TEXT",
    "HangupPeer",
    "approve_pending",
    "engine_for",
    "pending_approvals",
    "stack_for",
    "tool_capable_profile",
    "write_exact",
]