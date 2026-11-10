"""操作凭据（Receipt）。

回答第一个问题：**Agent 到底改了什么？**

一条 Receipt 必须能独立核对——不看对话、不看模型输出，只看它自己 + 磁盘。
字段对齐 PRD §3.5：
    receipt_id / run_id / call_id / action / args_hash / root + resource
    before_state / after_state / backend / evidence / intent_seq / blob_ref /
    issued_at / issuer

**为什么 resource 带 workspace 根**：只写相对路径时，"wsA/a.md" 与
"wsB/a.md" 是同一张凭据，跨 workspace 无法区分，`verify_receipts` 也没有
落点可查。所以 PR G 起 `resource` 是**绝对路径**，`root` 单独记 workspace 根，
并断言 resource 落在 root 内（这本身就是一条可核对的关系）。

**证据链**（`evidence` 是结构化引用，不是合成字符串）：
    result → 本次调用的 `tool.result` 事件（按 call_id 定位）
    intent → `tool.intent` 事件的 seq（args_hash 由它担保）
    blob   → 写前快照 blob（内容寻址，sha256 == before_state）
`verify_receipts` 逐条解析这些引用，再拿磁盘现状与 `after_state` 比。任何一条
对不上都算问题；旧格式凭据（没有 root）明确报"无法独立核对"，不装作没事。

硬约束：
- 只有 effect_state = applied 才产生 receipt
- 失败与结果未知不得伪造成功（对应「不能冒充」）
- 新建文件的 before_state 显式记为 "absent"，不是空字符串、不是 null
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .permissions import ACTION_CREATE, ACTION_DELETE, ACTION_EXEC, ACTION_OVERWRITE
from .sandbox import FileBackend, current_backend
from .snapshot import ABSENT
from .tools import ToolResult
from .workspace import Workspace

ISSUER = "tianshu/0.1"
#: 历史默认值。M3 起凭据记录**实际**后端（`build_receipt(backend=...)`），
#: 这个常量只留给"没传 backend"的旧调用点，保证旧事件语义不变。
BACKEND = "local_fs"

#: 有副作用的动作。凭据的 `action` 必须是其中之一——它不能是 tool_id，
#: 也不能是空串（那正是"看起来完整、其实核对不了"的来源）。
WRITE_ACTIONS = (ACTION_CREATE, ACTION_OVERWRITE, ACTION_DELETE)


@dataclass
class Receipt:
    receipt_id: str
    run_id: str
    call_id: str
    tool_id: str
    action: str
    args_hash: str
    root: str  # workspace 根（绝对路径），resource 必须落在它里面
    resource: str  # 目标文件的绝对路径
    effect_state: str
    before_state: str  # "absent" | sha256 | "unknown"
    after_state: str | None  # sha256 | "absent" | "exec:<digest>"
    backend: str
    intent_seq: int  # 产生这次写的 tool.intent 事件 seq（真实引用）
    evidence: list[dict[str, Any]] = field(default_factory=list)
    blob_ref: str | None = None  # 写前快照 blob；before_state=absent 时为 None
    issued_at: int = 0
    issuer: str = ISSUER

    def to_payload(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "action": self.action,
            "args_hash": self.args_hash,
            "root": self.root,
            "resource": self.resource,
            "effect_state": self.effect_state,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "backend": self.backend,
            "intent_seq": self.intent_seq,
            "evidence": self.evidence,
            "blob_ref": self.blob_ref,
            "issued_at": self.issued_at,
            "issuer": self.issuer,
        }


def _evidence_refs(
    *,
    call_id: str,
    intent_seq: int,
    blob_ref: str | None,
    before_state: str,
    memory_id: str | None = None,
) -> list[dict[str, Any]]:
    """把真实引用写成可逐条解析的证据列表。没有引用就不写假引用。

    记忆凭据多一条 `memory` 引用：它把凭据绑到具体 memory_id + scope URI，
    而不是让 `root/resource` 两个字符串自己"看起来像"。记忆没有写前 blob
    （blob 是文件 preimage 的概念），所以不写假 blob 引用。
    """
    refs: list[dict[str, Any]] = [
        {"kind": "result", "call_id": call_id},
        {"kind": "intent", "seq": intent_seq},
    ]
    if memory_id is not None:
        refs.append({"kind": "memory", "memory_id": memory_id})
        return refs
    if before_state == ABSENT:
        refs.append({"kind": "blob", "absent": True})
    elif blob_ref:
        refs.append({"kind": "blob", "path": str(blob_ref), "sha256": before_state})
    return refs


def build_receipt(
    *,
    run_id: str,
    call_id: str,
    tool_id: str,
    args_hash: str,
    result: ToolResult,
    root: str = "",
    resource: str = "",
    action: str = "",
    intent_seq: int = 0,
    backend: str = BACKEND,
    blob_ref: str | None = None,
) -> Receipt | None:
    """副作用已发生时产出一条凭据；否则返回 None。

    只认 applied。partial / unknown 也返回 None——它们的事实状态是「不确定」，
    不能被一张看起来完整的凭据粉饰过去，应当留在 tool.result 里等人工核查。

    `root` / `resource` / `action` / `intent_seq` 在**真的产出凭据**时都是
    必需的：缺了任何一个，这张凭据就退化成"自洽但不可核对"，而这正是本模块
    要修掉的毛病。它们带默认值只是为了 `effect_state != applied` 的调用点
    （拒绝、未派发）不必构造不存在的证据——那种情况不会返回凭据。
    """
    if result.effect_state != "applied":
        return None

    out = result.output
    rel = str(out.get("path", ""))
    # 写后状态：写入是内容哈希，删除是显式的 "absent"（不是空、不是 None）
    after = out.get("sha256") or out.get("after_state")
    if not rel or not after:
        # applied 但缺路径或写后状态：这是执行侧的 bug，宁可炸也不能产出半张凭据
        raise ValueError(
            f"effect_state=applied 但缺少 path/写后状态，无法产出凭据：{out!r}"
        )
    missing = [
        name for name, value in (
            ("root", root),
            ("resource", resource),
            ("action", action),
            ("intent_seq", intent_seq),
        )
        if not value
    ]
    if missing:
        # applied 却拿不到定位信息：宁可炸，也不能往日志里塞一张核对不了的凭据。
        raise ValueError(
            f"effect_state=applied 但缺少凭据定位信息 {missing}，无法产出可独立核对的凭据"
        )
    if action not in WRITE_ACTIONS:
        raise ValueError(
            f"凭据的 action 必须是 {WRITE_ACTIONS} 之一，收到 {action!r}"
            "（不许拿 tool_id 冒充动作）"
        )

    root_p = Path(root).resolve()
    target = Path(resource).resolve()
    if target != root_p and root_p not in target.parents:
        raise ValueError(
            f"凭据的 resource 不在记录的 workspace 根内：{target}（root={root_p}）"
        )

    before_state = str(out.get("before_state") or ABSENT)
    return Receipt(
        receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        call_id=call_id,
        tool_id=tool_id,
        action=action,
        args_hash=args_hash,
        root=str(root_p),
        resource=str(target),
        effect_state=result.effect_state,
        before_state=before_state,
        after_state=str(after),
        backend=backend,
        intent_seq=int(intent_seq),
        evidence=_evidence_refs(
            call_id=call_id,
            intent_seq=int(intent_seq),
            blob_ref=blob_ref,
            before_state=before_state,
        ),
        blob_ref=str(blob_ref) if blob_ref else None,
        issued_at=int(time.time() * 1000),
    )


def build_exec_receipt(
    *,
    run_id: str,
    call_id: str,
    tool_id: str,
    args_hash: str,
    result: ToolResult,
    root: str = "",
    resource: str = "",
    action: str = "",
    intent_seq: int = 0,
    backend: str = BACKEND,
) -> Receipt | None:
    """执行命令的凭据。

    命令没有"文件写后哈希"：`after_state` 是执行摘要（`exec:<sha256>`，由
    `run_command` 按 argv/退出码/信号/输出算出），`before_state` 显式记
    `"unknown"`——我们只知道它跑了，不知道它动了哪些文件。**磁盘核对不适用**：
    `verify_receipts` 对 exec 凭据只核对事件引用与结果字段，不比较磁盘。

    `resource` 是命令的 cwd（绝对路径），必须落在记录的 workspace 根内；
    `root` 是那个 workspace 根。这样"这条命令在哪棵树里跑的"仍可核对。
    """
    if result.effect_state != "applied":
        return None

    out = result.output
    after = out.get("after_state")
    if not after:
        raise ValueError(
            f"exec 结果 applied 但没有 after_state（执行摘要）：{out!r}"
        )
    missing = [
        name
        for name, value in (
            ("root", root),
            ("resource", resource),
            ("action", action),
            ("intent_seq", intent_seq),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"exec 结果 applied 但缺少凭据定位信息 {missing}，无法产出可核对的凭据"
        )
    if action != ACTION_EXEC:
        raise ValueError(f"exec 凭据的 action 必须是 {ACTION_EXEC}，收到 {action!r}")

    root_p = Path(root).resolve()
    target = Path(resource).resolve()
    if target != root_p and root_p not in target.parents:
        raise ValueError(
            f"exec 凭据的 cwd 不在记录的 workspace 根内：{target}（root={root_p}）"
        )

    return Receipt(
        receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        call_id=call_id,
        tool_id=tool_id,
        action=ACTION_EXEC,
        args_hash=args_hash,
        root=str(root_p),
        resource=str(target),
        effect_state=result.effect_state,
        before_state="unknown",
        after_state=str(after),
        backend=backend,
        intent_seq=int(intent_seq),
        evidence=_evidence_refs(
            call_id=call_id,
            intent_seq=int(intent_seq),
            blob_ref=None,
            before_state="unknown",
        ),
        blob_ref=None,
        issued_at=int(time.time() * 1000),
    )


def build_memory_receipt(
    *,
    run_id: str,
    call_id: str,
    tool_id: str,
    args_hash: str,
    result: ToolResult,
    root: str = "",
    resource: str = "",
    action: str = "",
    intent_seq: int = 0,
    backend: str = "memory",
) -> Receipt | None:
    """记忆激活 / 遗忘的凭据。

    记忆不是文件，所以 `root` 是 **scope URI**（`memory://...`），`resource`
    是 memory_id；两者不再做"路径谁包含谁"的比较，而是由证据引用
    `{"kind": "memory", "memory_id": ...}` 绑定。写前/写后状态使用稳定形状：

        memory:<memory_id>:<status>:rev<N>

    `propose` 不产凭据：候选写入只是投影草稿，不是用户长期可见行为的改变
    （F10 §7）。只有 confirm / forget 改变召回行为，才需要可核对凭据。
    """
    if result.effect_state != "applied":
        return None

    out = result.output
    after = out.get("after_state")
    before = out.get("before_state")
    if not after or not before:
        raise ValueError(
            f"记忆结果 applied 但缺少 before_state/after_state：{out!r}"
        )
    missing = [
        name
        for name, value in (
            ("root", root),
            ("resource", resource),
            ("action", action),
            ("intent_seq", intent_seq),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"记忆凭据缺少定位信息 {missing}，无法产出可独立核对的凭据"
        )
    if action not in WRITE_ACTIONS:
        raise ValueError(
            f"记忆凭据的 action 必须是 {WRITE_ACTIONS} 之一，收到 {action!r}"
        )

    return Receipt(
        receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        call_id=call_id,
        tool_id=tool_id,
        action=action,
        args_hash=args_hash,
        root=str(root),
        resource=str(resource),
        effect_state=result.effect_state,
        before_state=str(before),
        after_state=str(after),
        backend=backend,
        intent_seq=int(intent_seq),
        evidence=_evidence_refs(
            call_id=call_id,
            intent_seq=int(intent_seq),
            blob_ref=None,
            before_state=str(before),
            memory_id=str(resource),
        ),
        blob_ref=None,
        issued_at=int(time.time() * 1000),
    )


def receipts_of(log, run_id: str) -> list[dict[str, Any]]:
    return [e.payload for e in log.read_run(run_id) if e.type == "receipt"]


# ---------------------------------------------------------------- 核对


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _observe(target: Path, io: FileBackend) -> str:
    if not io.exists(target):
        return ABSENT
    return _sha(io.read_bytes(target)[0])


def _verify_evidence(
    payload: dict[str, Any],
    label: str,
    *,
    by_seq: dict[int, Any],
    intents: dict[str, Any],
    required: tuple[str, ...] = ("result", "intent", "blob"),
) -> list[str]:
    """逐条解析凭据自己带的证据引用。解析不了 = 核对不了 = 问题。"""
    refs = payload.get("evidence")
    if not isinstance(refs, list) or not refs:
        return [f"凭据 {label} 没有可解析的证据引用"]

    problems: list[str] = []
    kinds: set[str] = set()
    call_id = payload.get("call_id")
    for ref in refs:
        if not isinstance(ref, dict):
            # 旧格式：evidence 是 ["tool.result:succeeded"] 这类合成串，
            # 它引用不到任何东西，不能算证据。
            problems.append(
                f"凭据 {label} 的证据引用不是结构化引用：{ref!r}（合成串无法核对）"
            )
            continue
        kind = ref.get("kind")
        if kind == "result":
            if ref.get("call_id") != call_id:
                problems.append(
                    f"凭据 {label} 引用的 result 属于另一个调用：{ref.get('call_id')!r}"
                )
            kinds.add("result")
        elif kind == "intent":
            seq = ref.get("seq")
            intent = intents.get(call_id)
            event = by_seq.get(seq) if isinstance(seq, int) else None
            if event is None or event.type != "tool.intent":
                problems.append(
                    f"凭据 {label} 引用的 intent seq={seq} 不是本 run 的 tool.intent 事件"
                )
            elif intent is None or intent.seq != seq:
                problems.append(
                    f"凭据 {label} 引用的 intent seq={seq} 与本次调用的 intent 不一致"
                )
            elif intent.payload.get("args_hash") != payload.get("args_hash"):
                problems.append(
                    f"凭据 {label} 的 args_hash 与 intent 记录不一致"
                    "（参数可能被改写）"
                )
            kinds.add("intent")
        elif kind == "memory":
            memory_id = ref.get("memory_id")
            if not memory_id:
                problems.append(f"凭据 {label} 的 memory 引用没有 memory_id")
            elif memory_id != payload.get("resource"):
                problems.append(
                    f"凭据 {label} 的 memory 引用与 resource 不一致："
                    f"{memory_id!r} ≠ {payload.get('resource')!r}"
                )
            kinds.add("memory")
        elif kind == "blob":
            if ref.get("absent"):
                if payload.get("before_state") != ABSENT:
                    problems.append(
                        f"凭据 {label} 声称写前内容为 absent，但 before_state="
                        f"{payload.get('before_state')!r}"
                    )
            else:
                path = ref.get("path")
                if not path:
                    problems.append(f"凭据 {label} 的 blob 引用没有路径")
                elif not Path(path).is_file():
                    problems.append(f"凭据 {label} 的写前 blob 不存在：{path}")
                elif _sha(Path(path).read_bytes()) != payload.get("before_state"):
                    problems.append(
                        f"凭据 {label} 的写前 blob 内容与 before_state 不符：{path}"
                    )
            kinds.add("blob")
        else:
            problems.append(f"凭据 {label} 有无法识别的证据引用：{ref!r}")

    for required_kind in required:
        if required_kind not in kinds:
            problems.append(f"凭据 {label} 缺少 {required_kind} 证据引用")
    return problems


def _verify_disk(
    payload: dict[str, Any],
    label: str,
    io: FileBackend,
    undone: set[tuple[str, str]],
) -> list[str]:
    """凭据 + 磁盘现状。这是"改坏磁盘能被发现"的那一步。

    `undone` 是日志里 `undo.completed` 记下的 (root, path)。凭据写的是
    "当时磁盘是什么"，撤销之后磁盘本来就该变回去——没有这一条，**每一次
    合法撤销都会永久显示成"与凭据不一致"**，这个告警也就没人当回事了。
    注意只认"内容正好回到 before_state"，撤销到别的样子仍然是问题。
    """
    root = payload.get("root")
    resource = payload.get("resource")
    if not root or not resource:
        return [
            f"凭据 {label} 是旧格式（没有 workspace 根／绝对资源路径），"
            "无法独立核对磁盘：请以 recover 与快照清单的结论为准"
        ]

    recorded_backend = payload.get("backend")
    if recorded_backend and recorded_backend != io.name:
        return [
            f"凭据 {label} 记录的后端是 {recorded_backend}，当前后端是 {io.name}："
            "无法在当前后端核对磁盘"
        ]

    root_p = Path(str(root)).resolve()
    target = Path(str(resource)).resolve()
    if target != root_p and root_p not in target.parents:
        return [f"凭据 {label} 的资源路径不在记录的 workspace 根内：{target}"]

    try:
        Workspace(root_p)
    except TianshuError as exc:
        return [f"凭据 {label} 记录的 workspace 根现在不可用（{exc}）：无法核对磁盘"]

    expected = payload.get("after_state")
    current = _observe(target, io)
    if current == expected:
        return []

    rel = (target.relative_to(root_p) if target != root_p else Path(".")).as_posix()
    if current == payload.get("before_state") and (str(root_p), rel) in undone:
        # 已记录的撤销：内容回到了写前状态，这与凭据并不矛盾。
        return []
    return [
        f"凭据 {label} 与磁盘不一致：{target} 现在是 {current[:12]}…，"
        f"凭据写的是 {str(expected)[:12]}…（写入之后被改过）"
    ]


def verify_receipts(log, run_id: str) -> list[str]:
    """凭据与工具结果、事件、写前 blob、磁盘现状互相对账。

    返回问题列表，空表示整条证据链（盘上记录 → 事件 → 快照 → 磁盘）都对得上。
    字段缺失不会被当成"没问题"：旧格式凭据会被明确报成无法核对。
    """
    events = log.read_run(run_id)
    by_seq = {e.seq: e for e in events}
    results: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.type == "tool.result":
            results.setdefault(e.payload["call_id"], e.payload)
    intents = {e.payload["call_id"]: e for e in events if e.type == "tool.intent"}
    undone: set[tuple[str, str]] = set()
    for e in events:
        if e.type != "undo.completed":
            continue
        for item in e.payload.get("entries") or []:
            if isinstance(item, dict) and item.get("root") and item.get("path"):
                undone.add((str(item["root"]), str(item["path"])))
    io = current_backend()
    problems: list[str] = []

    for ev in events:
        if ev.type != "receipt":
            continue
        p = ev.payload
        label = p.get("receipt_id") or f"seq={ev.seq}"
        call_id = p.get("call_id")
        res = results.get(call_id)
        if res is None:
            problems.append(f"凭据 {label} 找不到对应的 tool.result")
            continue
        if res.get("effect_state") != "applied":
            problems.append(
                f"凭据 {label} 声称 applied，但工具结果是 {res.get('effect_state')}"
            )
        out = res.get("output") or {}
        actual_after = out.get("sha256") or out.get("after_state")
        if actual_after != p.get("after_state"):
            problems.append(
                f"凭据 {label} 的 after_state 与工具结果哈希不一致"
            )
        if p.get("tool_id") and res.get("tool_id") != p.get("tool_id"):
            problems.append(
                f"凭据 {label} 的 tool_id 与工具结果不一致："
                f"{p.get('tool_id')!r} ≠ {res.get('tool_id')!r}"
            )
        if str(p.get("tool_id") or "").startswith("memory."):
            # 记忆没有磁盘可核对：它的写后状态是投影里的
            # `memory:<id>:<status>:rev<N>`。核对三件事：结果字段、
            # 证据引用（result + intent + memory）、以及凭据的 memory_id
            # 确实等于 resource（在 `_verify_evidence` 里）。
            problems.extend(
                _verify_evidence(
                    p,
                    label,
                    by_seq=by_seq,
                    intents=intents,
                    required=("result", "intent", "memory"),
                )
            )
            if not str(p.get("after_state") or "").startswith("memory:"):
                problems.append(
                    f"凭据 {label} 的记忆写后状态形状不对："
                    f"{p.get('after_state')!r}"
                )
            if not str(p.get("before_state") or "").startswith("memory:"):
                problems.append(
                    f"凭据 {label} 的记忆写前状态形状不对："
                    f"{p.get('before_state')!r}"
                )
            if out.get("memory_id") != p.get("resource"):
                problems.append(
                    f"凭据 {label} 的 memory_id 与工具结果不一致："
                    f"{p.get('resource')!r} ≠ {out.get('memory_id')!r}"
                )
        elif p.get("action") == ACTION_EXEC:
            # 执行命令没有文件写后哈希，磁盘核对不适用；核对的是
            # "结果字段齐全" + "证据引用能逐条解析"。
            problems.extend(
                _verify_evidence(
                    p,
                    label,
                    by_seq=by_seq,
                    intents=intents,
                    required=("result", "intent"),
                )
            )
            for field in (
                "exit_code",
                "signal",
                "timed_out",
                "truncated",
                "stdout_ref",
                "stderr_ref",
                "stderr_merged",
                "duration_ms",
            ):
                if field not in out:
                    problems.append(
                        f"凭据 {label} 的 exec 结果缺少审计字段：{field}"
                    )
        else:
            if p.get("root") and p.get("resource"):
                # 新格式才谈得上"逐条解析证据"；旧格式凭据没有指向任何东西，
                # `_verify_disk` 会给出唯一一条明确的"无法核对"，不必刷三遍。
                problems.extend(
                    _verify_evidence(p, label, by_seq=by_seq, intents=intents)
                )
            problems.extend(_verify_disk(p, label, io, undone))

    # 反向：所有 applied 的结果都应当有凭据
    receipt_calls = {e.payload["call_id"] for e in events if e.type == "receipt"}
    for call_id, res in results.items():
        if res.get("effect_state") != "applied" or call_id in receipt_calls:
            continue
        if res.get("tool_id") == "memory.propose":
            # F10 §7：候选写入是投影草稿，不是用户长期可见行为的改变；
            # 它没有凭据（要求见 docs/F10-memory-design.md §7）。
            continue
        problems.append(f"副作用已发生但没有凭据：call_id={call_id}")

    return problems
