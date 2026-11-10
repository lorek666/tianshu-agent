"""测试用的"本地人审"替身。

M2 之后覆盖/删除类写入必须有批准才能执行（PRD §2.3）。测试要证明的往往是
"批准之后到底做了什么"，而不是"没批准会怎样"——后者各有用例单独覆盖。
这个模块把"人在这条命令上敲 approve"这一步自动化，其余路径全走真实服务层：

    看到待批 → approvals.decide(approved) → resume_run

它不是库的一部分（所以不在 `src/` 里）：在生产代码里放一个自动批准函数，
迟早会有人把它当默认路径用。
"""

from __future__ import annotations

from pathlib import Path

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


__all__ = [
    "CHILD_TEXT",
    "approve_pending",
    "engine_for",
    "pending_approvals",
    "stack_for",
    "tool_capable_profile",
    "write_exact",
]