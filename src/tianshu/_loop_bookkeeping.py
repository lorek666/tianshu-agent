"""主循环执行进度、消息归因、落盘 spill、事件对账与终态收尾（内部 Mixin，由 `AgentLoop` 继承）。

承载 `_Progress` 值对象、提案三元组判定、事件历史重建、审批重绑、未决派发对账与 Run 终态收尾，
零契约变更。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .contents import ContentStore
from .context import RUN_START_SEQ
from .dispatcher import (
    APPROVAL_REJECTED,
    DISPATCH_EXECUTING,
    DISPATCH_INTENT,
    Dispatcher,
)
from .errors import RunFenced, TianshuError, ToolArgumentError
from .events import Event, EventLog
from .fsutil import atomic_write_bytes
from .guard import scan_blocked
from .jobs import (
    JOB_CLAIMED,
    JOB_PENDING,
    JOB_WAITING,
    JobClaim,
    JobLost,
    JobStore,
    JobUnavailable,
)
from .model import ModelResponse, ToolCall
from .permissions import ApprovalStore
from .snapshot import SnapshotStore
from .summaries import SummaryStore
from .tasks import (
    RUN_FENCED,
    RUN_RECONCILING,
    TERMINAL_STATES,
    Run,
    RunCursor,
    Task,
    TaskStore,
)
from .tools import SUBTASK_TOOL_IDS, ToolResult, validate_args

if TYPE_CHECKING:  # pragma: no cover
    from .loop import RunResult

#: 预算账本里由 `_Progress` 显式读写的键。剩下的键（未来新增的账目）由
#: `_Progress.budget` 原样透传，不会被重复计算或丢掉。
_LEDGER_OWNED_KEYS = frozenset({
    "turns",
    "model_attempts",
    "model_attempts_known",
    "tokens",
    "unknown_usage_calls",
    "cost_total_micros",
    "unknown_cost_calls",
})


@dataclass
class _Progress:
    """一份可持久化的执行进度（= RunCursor 的运行时视图）。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    #: 与 `messages` 平行的 seq 归因：第 i 条消息是哪个事件写进来的。
    #: 头两条（system + 任务陈述）记 `RUN_START_SEQ`——它们由 run.start 派生，
    #: 没有属于自己的消息事件。这份列表只给上下文编译溯源用，不参与消息重建。
    message_seqs: list[int] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    model_turn: dict[str, Any] | None = None
    turns: int = 0
    #: 模型尝试次数（含失败/重试）。它进预算账本，resume 之后不丢：费用行按它
    #: 说"打出去几次"，而不是按完成轮次说（issue #58）。
    model_attempts: int = 0
    #: 上面这个数是否完整。旧游标（账本里没有该键）只能以完成轮次为下限，
    #: 此时 `False` —— 展示层必须说"不知道"，不能说一个精确的 0/N。
    model_attempts_known: bool = True
    tokens: int = 0
    unknown_usage_calls: int = 0
    #: 已知费用累计（微美元）与费用未知的调用数。跟着游标走，resume 之后不丢账。
    cost_total_micros: int = 0
    unknown_cost_calls: int = 0
    n_applied: int = 0
    n_unknown: int = 0
    budget: dict[str, Any] = field(default_factory=dict)

    @property
    def effect_state(self) -> str:
        if self.n_applied and self.n_unknown:
            return "partial"
        if self.n_applied:
            return "applied"
        if self.n_unknown:
            return "unknown"
        return "none_applied"

    def to_cursor(self) -> RunCursor:
        return RunCursor(
            messages=self.messages,
            message_seqs=self.message_seqs,
            pending_proposals=self.pending,
            completed_call_ids=self.completed,
            approval_ids=self.approvals,
            model_turn=self.model_turn,
            budget_ledger={
                "turns": self.turns,
                "model_attempts": self.model_attempts,
                "model_attempts_known": self.model_attempts_known,
                "tokens": self.tokens,
                "unknown_usage_calls": self.unknown_usage_calls,
                "cost_total_micros": self.cost_total_micros,
                "unknown_cost_calls": self.unknown_cost_calls,
                **self.budget,
            },
        )

    @classmethod
    def of(cls, cursor: RunCursor) -> _Progress:
        ledger = dict(cursor.budget_ledger or {})
        messages = list(cursor.messages)
        seqs = list(cursor.message_seqs)
        if len(seqs) != len(messages):
            # 旧游标（F5 之前写的）没有 seq 列表，或者游标在"结果已落库、游标还没
            # 跟上"的窗口里被读到。留空即可：`_message_source_seqs` 会从事件重建，
            # 重建不出来也不影响消息本身——溯源是记账材料，不是发送内容。
            seqs = []
        return cls(
            messages=messages,
            message_seqs=seqs,
            pending=[dict(p) for p in cursor.pending_proposals],
            completed=list(cursor.completed_call_ids),
            approvals=list(cursor.approval_ids),
            model_turn=cursor.model_turn,
            turns=int(ledger.get("turns", 0) or 0),
            # 旧游标没有尝试次数键：以完成轮次为**下限**（每次成功至少一次
            # 尝试），并标记 `model_attempts_known=False`——展示层据此说"不知道"，
            # 而不是把一个下限当成精确值。resume 后新增的尝试继续累加。
            model_attempts=int(
                ledger.get("model_attempts", ledger.get("turns", 0)) or 0
            ),
            model_attempts_known="model_attempts" in ledger,
            tokens=int(ledger.get("tokens", 0) or 0),
            unknown_usage_calls=int(ledger.get("unknown_usage_calls", 0) or 0),
            cost_total_micros=int(ledger.get("cost_total_micros", 0) or 0),
            unknown_cost_calls=int(ledger.get("unknown_cost_calls", 0) or 0),
            budget={k: v for k, v in ledger.items()
                    if k not in _LEDGER_OWNED_KEYS},
        )


def spec_is_write(tool_id: str) -> bool:
    """该工具是否有副作用。故障注入只打在这类工具上。"""
    from .tool_registry import current_registry

    spec = current_registry().spec(tool_id)
    return spec is not None and spec.side_effect_level != "pure"


def proposal_requires_dispatch(tool_id: str) -> bool:
    """这条调用是否必须经过 Dispatcher。

    **与"有没有副作用"是两件事**：`subtask.status` / `subtask.result` 是纯读，
    但父任务身份、父 Run 作用域与"子任务是否存在"都在
    `Dispatcher._authority_subtask()` 里判（D1-c7）。把它们当普通只读工具直接
    执行，等于把"不存在"伪装成空结果。所以委派工具一律走派发；它们的
    `side_effect_level` 仍然如实写 `pure`（不为了让它们进派发而谎报写文件）。
    """
    return spec_is_write(tool_id) or tool_id in SUBTASK_TOOL_IDS


def _dispatchable(resp: ModelResponse, call: ToolCall) -> tuple[bool, str]:
    """PRD §5.1 的三元组判定。参数是字符串 = 模型给了半截 JSON。"""
    if resp.finish_reason != "tool_use":
        return False, f"finish_reason={resp.finish_reason}"
    if not isinstance(call.arguments, dict):
        return False, "arguments 不是完整 JSON 对象（可能被截断）"
    from .tool_registry import current_registry

    spec = current_registry().spec(call.name)
    if spec is None:
        return False, f"工具不在白名单：{call.name}"
    if not spec.exposed:
        # 清单即能力边界：`tool_manifest()` 只投影 exposed=True 的工具，模型看不见
        # `restore_file`，却仍可能在响应里把它拼出来。不派发，但按 A7 留痕——模型
        # 需要收到一条 tool 消息才能继续，硬失败会把一次可恢复的乱猜变成整轮失败。
        return False, f"内部工具不对模型开放（exposed=False）：{call.name}"
    # 参数准入在**这里**做，不是在工具实现里做：嵌套对象、数组与约束都要在
    # 派发（更别提审批）之前被拒。否则用户会被要求批准一次"argv 里放了个整数"
    # 的调用，而执行侧到打开文件之前才发现参数不合法——那是把契约当装饰。
    try:
        validate_args(call.arguments, spec.input_schema)
    except ToolArgumentError as exc:
        return False, f"参数不满足工具契约：{exc}"
    return True, ""


def _tool_message(call_id: str, tool_id: str, result: ToolResult) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_id,
        "content": json.dumps(result.output, ensure_ascii=False, sort_keys=True),
    }


def _assistant_message(resp: ModelResponse) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
    if resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": c.call_id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(
                        c.arguments, ensure_ascii=False, sort_keys=True
                    ),
                },
            }
            for c in resp.tool_calls
        ]
    return msg



def message_source_seqs(events: list[Event]) -> list[int]:
    """按事件顺序给出「每条消息来自哪个事件」的 seq 列表。

    这份列表与 `replay` 重建消息序列的顺序是**同一套规则**（`replay.py` 直接用
    它来核对）：头两条来自 run.start，之后每条 `model.result` / `tool.result` /
    `tool.not_dispatched` 各产出一条消息。

    把它放在这里而不是各自实现两遍，是因为"循环写的归因"和"回放算的归因"一旦
    分叉，replay 报的红就是假的——那种红比不校验更糟。
    """
    seqs = [RUN_START_SEQ, RUN_START_SEQ]
    for event in events:
        if event.type in ("model.result", "tool.result", "tool.not_dispatched"):
            seqs.append(event.seq)
    return seqs



class _LoopBookkeepingMixin:
    """上下文归因、落盘、事件对账、审批重绑、终态收尾与恢复认领，供 `AgentLoop` 组合继承。"""

    _tasks: TaskStore
    _claims: JobStore
    _dispatcher: Dispatcher
    _approvals: ApprovalStore
    _snapshots: SnapshotStore | None
    _contents: ContentStore | None
    _summaries: SummaryStore | None
    _log: EventLog
    _results: dict[str, dict[str, Any]]
    _result_seqs: dict[str, int]
    _counted: set[str]
    _artifact: dict[str, Any] | None
    _vetoed_subjects: set[str]
    _veto_open: bool

    # ---------------------------------------------------------------- 上下文

    def _message_source_seqs(
        self, run_id: str, progress: _Progress
    ) -> list[int]:
        """与 `progress.messages` 平行的 seq 归因（给 `compile_context` 用）。

        优先用游标里存的那份（它是和消息同一个事务写的）。只有两种情况下它不可用：
        旧游标（F5 之前没有这个字段）和"结果已落库、游标还没跟上"的窗口。这时按
        事件重建——重建规则与 `replay` 完全同一份实现（`message_source_seqs`），
        所以循环记的归因与回放算的归因不会分叉。
        """
        if len(progress.message_seqs) == len(progress.messages):
            return list(progress.message_seqs)
        derived = message_source_seqs(self._log.read_run(run_id, verify=False))
        count = len(progress.messages)
        if len(derived) >= count:
            return derived[:count]
        # 事件比消息少（说明有消息没有对应事件）：能对上的部分照实给，
        # 剩下的标"来源不明"(RUN_START_SEQ)，不编 seq。
        return derived + [RUN_START_SEQ] * (count - len(derived))

    def _persist_spills(
        self, spills: list[tuple[str, bytes]], run_id: str
    ) -> None:
        """把待落盘的工具输出原文写进内容寻址 blob 目录。

        复用已有的 `artifacts/blobs/<sha256>`：`SnapshotStore` 的写前副本与事件
        payload 落盘用的是同一套目录与同一个原子写。同内容只存一份，重复 spill
        不会产生第二份文件。
        """
        if not spills:
            return
        blob_dir = self._snapshots.blob_dir if self._snapshots is not None else None
        if blob_dir is None:
            blob_dir = self._log.blob_dir
        if blob_dir is None:
            # 没有 blob 目录就没法兑现占位里"完整内容已落盘"那句话。
            # 与其发一条撒谎的上下文，不如明确失败。
            raise TianshuError(
                "工具输出需要落盘，但当前 Run 没有可用的 blob 目录"
                "（SnapshotStore 与 EventLog 都没配置 blob_dir）"
            )
        task_id = ""
        run = self._tasks.get_run(run_id)
        if run is not None:
            task_id = getattr(run, "task_id", "") or ""
        for digest, data in spills:
            blob = blob_dir / digest
            if not blob.exists():
                from .restore_access import ensure_material_write_allowed

                ensure_material_write_allowed(blob_dir, owner_log=self._log)
                atomic_write_bytes(blob, data)
            if self._contents is not None and task_id:
                # 授权在**落盘时**按当前 Run 的任务写入：内容寻址会去重，可见性
                # 不能记在对象上（否则一个任务能看到另一个任务的原文）。
                self._contents.record(digest, data, task_id=task_id, run_id=run_id)


    # ---------------------------------------------------------------- 记账

    def _index_events(self, run_id: str, progress: _Progress) -> None:
        """把"已经发生过什么"从事件里重建出来。

        为什么不只用游标：游标是最后一次成功提交的快照，可能比事件落后一步
        （写结果与写游标在两个提交里）。历史结果以事件为准，游标只用来知道
        "下一步该做什么"。
        """
        self._results = {}
        self._result_seqs = {}
        self._counted: set[str] = set()
        self._artifact = None
        # 否决索引每次驱动重建（见 `__init__`）：不只是"这次有没有否决"，而是
        # "否决之后有没有替代动作真的生效"。后者决定模型说"完了"时能不能记成
        # succeeded。
        self._vetoed_subjects = set()
        self._veto_open = False
        veto_seq: int | None = None
        applied_seq: int | None = None
        n_applied = n_unknown = 0
        for ev in self._log.read_run(run_id):
            if ev.type == "tool.result":
                payload = ev.payload
                call_id = payload.get("call_id", "")
                self._results[call_id] = payload
                self._result_seqs[call_id] = ev.seq
                self._counted.add(call_id)
                state = payload.get("effect_state")
                if state == "applied":
                    n_applied += 1
                    applied_seq = ev.seq
                elif state in ("partial", "unknown"):
                    n_unknown += 1
                out = payload.get("output") or {}
                if (
                    payload.get("tool_id") == "create_report"
                    and payload.get("status") == "succeeded"
                    and out.get("sha256")
                ):
                    self._artifact = out
                if out.get("rule_id") == APPROVAL_REJECTED:
                    veto_seq = ev.seq
                    # 记下"这件事已经被否决过、且模型被告知过"。同一条拒绝结果
                    # 只能来自一份被否决的动作；请求记录里有它的完整内容摘要，
                    # 于是模型换个 call_id 重发时也能认出来是同一件事。
                    stored = (
                        self._dispatcher.get_request(out["request_id"])
                        if out.get("request_id")
                        else None
                    )
                    if stored is not None:
                        self._vetoed_subjects.add(
                            stored.request.subject_digest()
                        )

        # "否决还没被替代"= 最近一次否决之后没有任何效果落地。模型只要找到别的
        # 路（换参数/换目标并被人批准执行），这就是一次正常的成功收尾。
        self._veto_open = veto_seq is not None and (
            applied_seq is None or applied_seq < veto_seq
        )

        progress.n_applied = n_applied
        progress.n_unknown = n_unknown

        # 这里刻意**不**从事件补齐游标里的消息。
        #
        # 消息、已完成调用、待执行提案是同一个事务写的，所以游标只可能整体落后
        # 一步，落后的那一步对应的调用必然还在 `pending` 里，由 `_advance` 按序
        # 补记。反过来"按事件把缺的消息补到末尾"看起来更周到，实际会在中间缺
        # 一条时把顺序排错——而消息顺序错了，`messages_hash` 立刻对不上，
        # replay 报的红比它想修的问题更严重。

    def _record_result(
        self,
        run_id: str,
        proposal: dict[str, Any],
        result: ToolResult,
        *,
        append: bool = True,
    ) -> None:
        """工具结果只记一次。

        派发成功的结果由 `Dispatcher` 在它自己的事务里连凭据一起写；走到这里
        只是"确认它写过了"。而拒绝、未派发这类没有进派发事务的结果由本循环补写。
        两份结果的日志会让 replay 无法重建唯一一条消息序列。
        """
        call_id = proposal["call_id"]
        if call_id in self._results:
            return
        if append:
            from .receipt import build_receipt

            batch = [
                {
                    "run_id": run_id,
                    "type": "tool.result",
                    "payload": {
                        "call_id": call_id,
                        "tool_id": proposal["tool_id"],
                        "status": result.status,
                        "effect_state": result.effect_state,
                        "output": result.output,
                    },
                    "actor_kind": "tool",
                    "idem_key": call_id,
                }
            ]
            receipt = build_receipt(
                run_id=run_id,
                call_id=call_id,
                tool_id=proposal["tool_id"],
                args_hash=proposal.get("args_hash", ""),
                result=result,
            )
            if receipt is not None:
                batch.append(
                    {
                        "run_id": run_id,
                        "type": "receipt",
                        "payload": receipt.to_payload(),
                        "actor_kind": "core",
                        "idem_key": receipt.receipt_id,
                    }
                )
            seqs = self._log.append_many(batch)
            # 批里第一条永远是 tool.result（凭据跟在后面），seq 归因取它。
            self._result_seqs[call_id] = seqs[0]
        self._results[call_id] = {
            "call_id": call_id,
            "tool_id": proposal["tool_id"],
            "status": result.status,
            "effect_state": result.effect_state,
            "output": result.output,
        }

    def _tool_result_seq(self, run_id: str, call_id: str) -> int:
        """这条调用的 `tool.result` 事件 seq。

        缓存来自两处：`_index_events`（本次驱动开始时扫一遍）与 `_record_result`
        （本循环自己写的）。剩下一种情形要现查：**派发层**在它自己的事务里写了
        `tool.result`，主循环手里只有结果、没有 seq。查不到就返回 RUN_START_SEQ
        （"来源不明"），不猜。
        """
        cached = self._result_seqs.get(call_id)
        if cached is not None:
            return cached
        seq = RUN_START_SEQ
        if run_id:
            for ev in self._log.read_run(run_id, verify=False):
                if ev.type == "tool.result" and ev.payload.get("call_id") == call_id:
                    seq = ev.seq
                    break
        self._result_seqs[call_id] = seq
        return seq

    def _absorb(
        self,
        progress: _Progress,
        call_id: str,
        recorded: dict[str, Any],
        *,
        run_id: str = "",
    ) -> None:
        """把一条已落库的结果并进进度：消息、计数、产物、seq 归因。"""
        if call_id in progress.completed:
            return
        result = ToolResult(
            status=recorded.get("status", "failed"),
            effect_state=recorded.get("effect_state", "unknown"),
            output=recorded.get("output") or {},
        )
        progress.messages.append(
            _tool_message(call_id, recorded.get("tool_id", ""), result)
        )
        # 与消息平行地补一条归因。查不到（例如结果没有对应事件）就记 0：
        # 溯源是记账材料，宁可标"来源不明"也不在这里编一个 seq。
        progress.message_seqs.append(self._tool_result_seq(run_id, call_id))
        # 计数按**调用**去重，不按"这次驱动里有没有处理过它"：`_index_events`
        # 已经按事件把它数过一遍了，游标落后时这里会把同一个调用再数一次，
        # 于是"改了几个文件"变成虚高的数字（effect_state 本身看不出来）。
        if call_id not in self._counted:
            self._counted.add(call_id)
            if result.effect_state == "applied":
                progress.n_applied += 1
            elif result.effect_state in ("partial", "unknown"):
                progress.n_unknown += 1
        if (
            recorded.get("tool_id") == "create_report"
            and result.ok
            and result.output.get("sha256")
        ):
            self._artifact = result.output
        progress.completed.append(call_id)

    def _reusable_request_id(self, proposal, probe) -> str | None:
        """同一个动作该用哪个请求号（决定旧批准是"继续有效"还是"作废"）。

        两个来源，优先级从高到低：
        1. 游标里记着的上一轮请求号；
        2. 本 Run 里**仍然待批/已批**的请求，其授权字段与这一次完全一致。

        第 2 条是必需的：崩在 `prepare` 与 `save_cursor` 之间时，游标里没有
        请求号，但库里已经有一张待批审批。只看游标就会给同一个动作再发一张，
        用户看到两条一模一样的待批项，批了其中一条另一条变废单。
        """
        left = probe.to_canonical_dict()
        left.pop("request_id", None)

        candidate = proposal.get("request_id")
        ids: list[str] = [candidate] if candidate else []
        ids.extend(self._dispatcher.request_ids_for_run(probe.run_id))
        for request_id in ids:
            stored = self._dispatcher.get_request(request_id)
            if stored is None:
                continue
            right = stored.request.to_canonical_dict()
            right.pop("request_id", None)
            if right != left:
                continue
            if request_id != candidate:
                approval_id = stored.approval_id
                if approval_id:
                    approval = self._approvals.get(approval_id)
                    if approval is None or approval.state not in ("pending", "approved"):
                        continue
            return request_id
        return None

    def _rebind_approval(
        self, proposal: dict[str, Any], request: Any
    ) -> None:
        """旧批准是否还对得上这次请求；对不上就作废（不能暗中重新绑定）。

        典型场景：进程死了、别人接管了 claim，`claim_epoch` 变了，摘要随之变化。
        旧批准必须显式 superseded，否则它会一直挂在待批列表里，看着像"还能用"。
        """
        approval_id = proposal.get("approval_id")
        if not approval_id:
            return
        current = self._approvals.get(approval_id)
        if current is None:
            return
        # 比的是**动作摘要**，不是完整请求摘要：进程重启换了 claim_epoch，
        # 请求摘要必变，但人批准的那件事一个字都没变，审批必须继续有效
        # （否则 `approvals approve` 之后 `run resume` 永远要重批一次）。
        # 参数、目标、revision、绑定变了，动作摘要才变——那才是该作废的时候。
        action = request.action_digest()
        same_action = (
            current.action_digest == action if current.action_digest
            else current.request_digest == request.digest()
        )
        if current.state in ("pending", "approved") and not same_action:
            self._approvals.supersede(approval_id, reason="request_rebound")
            proposal["approval_id"] = None

    # ---------------------------------------------------------------- 收尾

    def _end_outcome(self, message: str) -> tuple[str, str | None]:
        """模型说"完了"时的结论：先看这次 Run 有没有**未翻案的否决**。

        人否决了一个动作、模型又拿不出替代动作 → 这个 Run 没有完成它的目标，
        只能记 `failed`（`effect_state` 由游标如实给出，通常是 `none_applied`）。
        记成 succeeded/noop 等于替人的否决签字同意，也会让 issue #40 从
        "停在 waiting_approval" 变成"悄悄成功"——两种都是撒谎。
        """
        if not self._veto_open:
            return message, None
        return "failed", (
            f"审批被拒绝（{APPROVAL_REJECTED}）：本 Run 有被否决的动作，"
            "模型没有提出可执行的替代动作"
        )

    def _state_of(self, run_id: str) -> str:
        run = self._tasks.get_run(run_id)
        return run.state if run is not None else RUN_FENCED

    def _has_tool_result_event(self, run_id: str, call_id: str) -> bool:
        row = self._log.conn.execute(
            "SELECT 1 FROM events WHERE run_id=? AND type='tool.result'"
            " AND idem_key=? LIMIT 1",
            (run_id, call_id),
        ).fetchone()
        return row is not None

    def _sync_persisted_effects(
        self, events: list[Event], progress: _Progress
    ) -> str:
        """按已落库 tool.result 对齐 progress 计数，防止绕过 _absorb 误报 none_applied。"""
        n_applied = 0
        n_unknown = 0
        has_partial = False
        has_unknown = False
        for ev in events:
            if ev.type != "tool.result":
                continue
            state = ev.payload.get("effect_state")
            if state == "applied":
                n_applied += 1
            elif state == "partial":
                n_unknown += 1
                has_partial = True
            elif state == "unknown":
                n_unknown += 1
                has_unknown = True
        progress.n_applied = max(progress.n_applied, n_applied)
        progress.n_unknown = max(progress.n_unknown, n_unknown)
        if progress.n_applied == 0 and has_partial and not has_unknown:
            return "partial"
        return progress.effect_state

    def _has_unsettled_dispatch(
        self, run_id: str, events: list[Event] | None = None
    ) -> bool:
        """是否存在已落 intent 但尚未结算 tool.result 的副作用派发（#145）。"""
        run_events = (
            self._log.read_run(run_id, verify=False) if events is None else events
        )
        intents = {
            e.payload.get("call_id")
            for e in run_events
            if e.type == "tool.intent" and e.payload.get("call_id")
        }
        results = {
            e.payload.get("call_id")
            for e in run_events
            if e.type == "tool.result" and e.payload.get("call_id")
        }
        if intents - results:
            return True
        rows = self._log.conn.execute(
            "SELECT call_id FROM dispatches WHERE run_id=? AND state IN (?, ?)",
            (run_id, DISPATCH_INTENT, DISPATCH_EXECUTING),
        ).fetchall()
        return any(row["call_id"] not in results for row in rows)

    def _has_active_own_workspace_block(self, events: list[Event]) -> bool:
        """本 Run 是否发出了尚未解除的本地目录阻断（`recovery.blocked`）。"""
        own_workspaces = {
            ev.payload.get("workspace")
            for ev in events
            if ev.type == "recovery.blocked"
            and isinstance(ev.payload.get("workspace"), str)
            and ev.payload.get("workspace")
        }
        if not own_workspaces:
            return False
        scan = scan_blocked(self._log)
        if scan.malformed:
            return True
        return bool(own_workspaces & scan.blocked)

    def _finish(
        self,
        run_id: str,
        claim: JobClaim,
        progress: _Progress,
        *,
        outcome: str,
        error: str | None,
    ) -> RunResult:
        """终结：先把作业收掉（证明我们还是 owner），再写 Run 结论。

        顺序不能反：先写 Run 成功、再发现 claim 已被接管，就会留下一条
        "旧 owner 宣告成功"的记录，而这正是 fencing 要禁止的事。
        """
        events = self._log.read_run(run_id, verify=False)
        effect = self._sync_persisted_effects(events, progress)
        if self._has_unsettled_dispatch(
            run_id, events
        ) or self._has_active_own_workspace_block(events):
            # 存在未结算 intent，或 `_fail_unknown` 已落库本地目录阻断但尚未转入
            # reconciling 就被异常打断：绝不能写终态 run.end 或释放目录租约（红线
            # 2/5/11）。转入 reconciling 保留租约交给 recover/人工核查；若此处仍遇
            # OperationalError，则让异常上抛由 Worker 挂起作业。
            reconcile_error = (
                "存在未完成的派发（intent 已提交、结果未知），"
                "请先 recover 核查再 resume"
            )
            if error:
                reconcile_error = f"{reconcile_error}（{error}）"
            try:
                if effect != "none_applied":
                    with self._log.transaction():
                        self._tasks.note_effect(
                            run_id,
                            effect_state=effect,
                            n_applied=progress.n_applied,
                            n_unknown=progress.n_unknown,
                            claim=claim,
                        )
                        self._tasks.transition(
                            run_id,
                            RUN_RECONCILING,
                            claim=claim,
                            reason="inflight_dispatch",
                        )
                else:
                    self._tasks.transition(
                        run_id,
                        RUN_RECONCILING,
                        claim=claim,
                        reason="inflight_dispatch",
                    )
            except (JobLost, RunFenced) as exc:
                return self._result(
                    run_id, progress, state=self._state_of(run_id), error=str(exc)
                )
            return self._result(
                run_id, progress, state=RUN_RECONCILING, error=reconcile_error
            )
        run_outcome = "succeeded" if outcome == "noop" else outcome
        try:
            self._claims.finish(claim, outcome=run_outcome)
        except (JobLost, JobUnavailable, TianshuError) as exc:
            return self._result(
                run_id,
                progress,
                state=self._state_of(run_id),
                error=f"收尾被拒绝（claim 已失效）：{exc}",
            )

        try:
            with self._log.transaction() as tx:
                # `run.end` 由这里写（终态事件只有这一个写者，CLI 直接取消
                # 也走同一条路），结算字段全部从事件与游标派生。
                #
                # `report_outcome` 只在"什么都没改"时才是 noop：改过文件却
                # 报 noop，会让 run.end 与 run.finish 各说一套（前者 noop、
                # 后者 succeeded），而 effect_state 又写着 applied。判断口径
                # 与 `_result()` 保持一致，只在这里落地一次。
                self._tasks._finish_locked(  # noqa: SLF001 - 同包内部提交
                    tx,
                    run_id,
                    outcome=run_outcome,
                    effect_state=effect,
                    reason=error,
                    report_outcome=(
                        "noop" if outcome == "noop" and effect == "none_applied" else None
                    ),
                )
        except RunFenced as exc:
            return self._result(
                run_id, progress, state=self._state_of(run_id), error=str(exc)
            )
        return self._result(
            run_id, progress, state=self._state_of(run_id), error=error
        )

    def _receipt_ids(self, run_id: str) -> list[str]:
        """凭据清单以事件为准，不从内存拼。

        执行成功的结果是 `Dispatcher` 在它自己的事务里连凭据一起写的，主循环
        只"确认它写过"，手里没有凭据 id。从日志读回来既准又能在回放时复核。
        """
        return [
            ev.payload["receipt_id"]
            for ev in self._log.read_run(run_id)
            if ev.type == "receipt" and ev.payload.get("receipt_id")
        ]

    def _unbilled_intents(self, run_id: str) -> int:
        """有 intent、没 result 的模型调用次数。"""
        intents = {
            e.payload.get("request_id")
            for e in self._log.read_run(run_id)
            if e.type == "model.intent"
        }
        results = {
            e.payload.get("request_id")
            for e in self._log.read_run(run_id)
            if e.type == "model.result"
        }
        return len(intents - results)

    def _result(
        self,
        run_id: str,
        progress: _Progress,
        *,
        state: str,
        error: str | None = None,
    ) -> RunResult:
        run = self._tasks.get_run(run_id)
        outcome = ""
        if run is not None and run.state in TERMINAL_STATES:
            outcome = run.outcome or ""
            if outcome == "succeeded" and progress.effect_state == "none_applied":
                # M0 的 noop 语义：没东西可做也是成功，但不能记成"改了什么"
                outcome = "noop"
        content = ""
        for message in reversed(progress.messages):
            if message.get("role") == "assistant":
                content = message.get("content") or ""
                break
        from .loop import RunResult

        return RunResult(
            run_id=run_id,
            outcome=outcome,
            effect_state=progress.effect_state,
            content=content,
            artifact=self._artifact,
            turns=progress.turns,
            summary_calls=(
                self._summaries.count_for_run(run_id)
                if self._summaries is not None
                else 0
            ),
            model_attempts=progress.model_attempts,
            model_attempts_known=progress.model_attempts_known,
            tokens=progress.tokens,
            unknown_usage_calls=progress.unknown_usage_calls,
            cost_total_micros=progress.cost_total_micros,
            unknown_cost_calls=progress.unknown_cost_calls,
            receipts=self._receipt_ids(run_id),
            n_applied=progress.n_applied,
            n_unknown=progress.n_unknown,
            error=error,
            state=state if run is None else run.state,
            approval_ids=list(progress.approvals),
            task_id=run.task_id if run is not None else "",
            goal_revision=run.goal_revision if run is not None else 0,
        )

    def _task(self, task_id: str) -> Task:
        task = self._tasks.get_task(task_id)
        if task is None:
            raise TianshuError(f"task 不存在：{task_id}")
        return task

    # ---------------------------------------------------------------- 认领

    def _claim_for_resume(self, run: Run) -> JobClaim | None:
        """拿到驱动这个 Run 的身份。

        返回 None 只在"别人正持有有效 claim"时发生——调用方据此拒绝抢占。
        """
        job = self._claims.job_for_run(run.run_id)
        if job is None:
            job = self._claims.enqueue(run.run_id)
        if job.state == JOB_CLAIMED:
            mine = self.current_claim(run.run_id)
            if mine is not None:
                return mine
            if self._claims.is_live(job):
                # 别人正在跑：不抢。
                return None
            # 死掉的/过期的 owner：接管。这里必须真的判断"还活着吗"，
            # 不能只看 Run 的状态——崩溃的 Run 会永远停在 RUNNING，
            # 只按状态判断就等于"崩一次就再也不能接手"。
            return self._claims.claim(job.job_id)
        if job.state in (JOB_PENDING, JOB_WAITING):
            # `waiting` = 上一轮驱动已经停下来等人（批准 / recover / resume）。
            # 显式 resume 就是那个"人已经决定继续"的事件，所以这里直接认领，
            # 不经过 `claim_next`——轮询认领 waiting 作业会让审批被反复作废。
            return self._claims.claim(job.job_id)
        # 作业已经收尾（done/cancelled），但 Run 还没结束：新开一个作业
        return self._claims.claim(self._claims.enqueue(run.run_id).job_id)
