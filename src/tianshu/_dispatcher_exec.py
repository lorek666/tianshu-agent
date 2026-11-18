"""派发层网络抓取、MCP 调用、子任务委派与控制 Run 生命周期（内部 Mixin，由 `Dispatcher` 继承）。

保持 `_dispatch_mcp` 方法名与全部崩溃注入点/负控锚点逐字不变，并通过
`sys.modules["tianshu.dispatcher"]` 绑定外发与守卫入口，零契约变更。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from ._dispatcher_types import (
    STATE_REJECTED,
    ControlRun,
    DispatchDenied,
    DispatchError,
    _McpTarget,
    _NetworkTarget,
    _SubtaskTarget,
)
from .contents import make_ref
from .errors import (
    BudgetCancelled,
    BudgetRefused,
    McpObjectBlocked,
    RunFenced,
    TianshuError,
)
from .events import EventLog, canonical_bytes, sha256_hex
from .fsutil import atomic_write_bytes
from .jobs import JobClaim, JobLost, JobStore
from .leases import GuardTimeout, LeaseConflict, LeaseStore, ReconcileFn
from .mcp import (
    McpCallError,
    McpClients,
    McpClosed,
    McpError,
    McpProtocolError,
    McpTimeout,
)
from .net import NetCancelled, NetError, NetLimits
from .net import summary_page as net_summary_page
from .permissions import ActionRequest, ApprovalError
from .resources import MemoryScope, parse_memory_uri
from .run_policy import EFFECTS, PURE_ONLY, RunExecutionPolicyV1
from .snapshot import SnapshotStore
from .tasks import (
    RUN_RUNNING,
    SUBTASK_TERMINAL_STATES,
    TaskStore,
    clean_subtask_limits,
    subtask_budget_attempt,
)
from .tool_registry import ToolRegistry
from .tools import AdmittedCall, ToolResult
from .workspace import WorkspaceBinding


class _DispatcherExecMixin:
    """网络抓取、MCP 外发、子任务委派以及租约接管对账/ControlRun，供 `Dispatcher` 组合继承。"""

    _log: EventLog
    _tasks: TaskStore
    _leases: LeaseStore
    _claims: JobStore
    _snapshots: SnapshotStore | None
    _data_root: Path | None
    _clock: Any
    _guard_timeout_ms: int
    _memory: Any
    _contents: Any
    _net_limits: NetLimits
    _mcp_clients: McpClients | None
    _mcp_prepared: bool
    _registry: ToolRegistry | None
    _budget: Any
    registry: ToolRegistry

    def _effective_net_max_bytes(
        self, args: dict[str, Any] | None = None, *, request_id: str = ""
    ) -> int:
        effective_args = args
        if effective_args is None and request_id:
            stored = self.get_request(request_id)
            effective_args = None if stored is None else stored.args
        raw_max = (
            None
            if not isinstance(effective_args, dict)
            else effective_args.get("max_bytes")
        )
        if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0:
            return min(raw_max, self._net_limits.max_bytes)
        return self._net_limits.max_bytes

    def _precheck_network_budget(
        self, request: ActionRequest, *, args: dict[str, Any] | None = None
    ) -> None:
        """不预占，只回答"这一次抓取的字节上界还放得下吗"。

        账本不可用 / 账户不存在（直接调派发的单元测试）时放行：真正的预占在
        `_dispatch_network` 里做，那里失败同样不会发包。
        """
        if self._budget is None:
            return
        account_id = f"run:{request.run_id}"
        if getattr(self._budget, "account", lambda _a: None)(account_id) is None:
            return
        usage = self._budget.usage(account_id)
        limit = (usage.get("limits") or {}).get("bytes")
        if limit is None:
            return
        used = int((usage.get("reserved") or {}).get("bytes", 0)) + int(
            (usage.get("settled") or {}).get("bytes", 0)
        )
        need = self._effective_net_max_bytes(args, request_id=request.request_id)
        if used + need > limit:
            raise DispatchDenied(
                f"预算不足：bytes 已用 {used}，本次抓取最多读 {need}，"
                f"上限 {limit}。请求没有发出。",
                "dispatch.budget_refused",
            )

    def _reserve_network_bytes(
        self, request: ActionRequest, *, args: dict[str, Any] | None = None
    ) -> object | None:
        """抓取前按 `bytes` 维度预占。额度不足 / 已取消 / 过 deadline 一律拒发。

        预占的是**本次最多读多少**（可验证上界），不是估算；所以一次抓取最多
        读 `effective_max_bytes`，而账本上限决定能发几次。账户不存在（例如直接调
        派发的单元测试）时不预占，也不伪造一个账户——如实登记为未记账。
        """
        if self._budget is None:
            return None
        account_id = f"run:{request.run_id}"
        account = getattr(self._budget, "account", lambda _a: None)(account_id)
        if account is None:
            return None
        need = self._effective_net_max_bytes(args, request_id=request.request_id)
        return self._budget.reserve(
            account_id,
            attempt_id=f"net:{request.call_id}",
            run_id=request.run_id,
            call_id=request.call_id,
            need={"bytes": need},
        )

    def _settle_network_bytes(
        self, reservation: object | None, actual: int | None
    ) -> None:
        """结算字节。`actual is None` = **不知道读了多少**：保留预占，不记零。

        "不知道"与"没花钱"是两件事：把失败的抓取按 0 结算，会让账本看起来
        比事实便宜；保留占用则交给 `reconcile()` 报告，由人核对执行证据。
        """
        if self._budget is None or reservation is None:
            return
        if actual is None:
            self._budget.settle(
                reservation.reservation_id,
                usage={},
                known_dimensions=set(),
            )
            return
        self._budget.settle(
            reservation.reservation_id,
            usage={"bytes": actual},
            known_dimensions={"bytes"},
        )

    def _write_content(self, request: ActionRequest, body: bytes) -> str:
        """把抓到的字节写成内容引用。

        **先落盘、后写事件**：先写 `tool.result` 再落盘的话，崩溃会让日志承诺
        一份不存在的内容。blob 写成功但事件没写成的窗口只会留下一个孤儿文件
        （没有事件指向它），不会让回放读到幻觉。
        """
        if self._contents is None:
            raise TianshuError(
                "没有内容存储：网络结果必须有界落盘并给引用，"
                "不会把大 body 直接塞进事件"
            )
        blob_dir = getattr(self._snapshots, "blob_dir", None) or self._log.blob_dir
        if blob_dir is None:
            raise TianshuError(
                "没有 blob 目录：网络结果无法落盘（SnapshotStore 与 EventLog "
                "都没有 blob_dir）"
            )
        digest = sha256_hex(body)
        blob = Path(blob_dir) / digest
        if not blob.exists():
            atomic_write_bytes(blob, body)
        self._contents.record(
            digest, body, task_id=request.task_id, run_id=request.run_id
        )
        return make_ref(digest)

    def _dispatch_mcp(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _McpTarget,
        intent_seq: int,
        admitted: AdmittedCall,
    ) -> ToolResult:
        """MCP 分支：已取消不发包；超时/断线/协议失败按 `unknown` 结算。

        为什么超时是 `unknown` 而不是 `none_applied`：`tools/call` 可能已经在
        server 那边执行完，只是响应没回来。红线 11 要求"不知道"就如实说不知道，
        恢复流程会因此阻断该目录等待人工核对，而不是自动重放。
        """
        failpoint = sys.modules["tianshu.dispatcher"].failpoint
        if self._cancel_requested(request.run_id):
            result = ToolResult(
                "failed",
                "none_applied",
                {
                    "error": "Run 已取消：没有发出 MCP 调用",
                    "reason": "mcp.cancelled",
                    "mcp": {"server": target.server, "tool": target.tool},
                },
            )
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result

        clients = self._mcp_clients
        server = None if clients is None else clients.server(target.server)
        if server is None or not server.is_alive:  # pragma: no cover - authority 已判
            # 这一支在**写 stdin 之前**：进程已经死了/拿不到，一个字节都写不进去。
            # `unknown` 的结算沿用旧口径（不在本工单改结论），但**不能**落对象
            # 阻断：对象阻断的语义是"server 侧可能已经执行过"，而这里确定没发出，
            # 记一条就是假记录（`sent=False`）。
            return self._fail_unknown(
                request,
                dispatch_id,
                target,
                DispatchDenied(
                    f"MCP server 已不可用：{target.server!r}",
                    "dispatch.mcp_server_unavailable",
                ),
                sent=False,
            )
        binding = self.registry.mcp_binding(target.tool_id)
        spawn = {
            "via_exec_backend": False,
            "pid": server.pid,
            "argv0": server.config.command[0] if server.config.command else "",
            "cwd": server.config.cwd,
            "env_keys": sorted(server.config.env),
        }
        base_output: dict[str, Any] = {
            "mcp": {
                "server": target.server,
                "tool": target.tool,
                "tool_version": "" if binding is None else binding.tool_version,
                "protocol_version": server.protocol_version,
                "spawn": spawn,
            }
        }
        # 外发边界：请求一旦写进 server 的 stdin，"这次调用到底发没发出去"在本机
        # 就再也没法证明了。所以写之前先把派发行从 `intent` 推到 `executing`——
        # 这一行同时是**撤销与发送的分界线**：
        #   - 撤销先到（行已 revoked/rejected）→ 推进不上，这次不发；
        #   - 推进先到 → 撤销会明确报错"已经开始执行"，而不是假装撤销成功。
        # 两者抢同一把跨进程执行守卫（`_mark_mcp_send_boundary`），否则"看一眼
        # 行状态、再改"之间还能插进另一个进程的撤销。
        #
        # 崩溃窗口注入（只给测试用；生产路径永远不设 `TIANSHU_FAILPOINT`）：
        #   mcp_before_call —— intent/dispatches 已提交、外发边界标记还没落盘。
        #                      请求一定没写出，恢复可以判 `none_applied` 且不阻断；
        #   mcp_marked     —— 标记已落盘、请求还没写进 stdin。恢复**无法**证明没
        #                      发出去，只能保守判 `unknown` + 阻断（这一小段窗口
        #                      是有意付出的代价，不是漏洞）；
        #   mcp_after_call —— 响应已经收到（server 确实执行过），而结果/凭据/终态
        #                      还没落盘。与上一个同口径：`unknown` + 阻断，不重放。
        failpoint("mcp_before_call")
        try:
            boundary_marked = self._mark_mcp_send_boundary(
                dispatch_id, server=target.server, tool=target.tool, admitted=admitted
            )
        except (GuardTimeout, DispatchDenied, ApprovalError, JobLost, RunFenced) as exc:
            # 拿不到执行守卫 = 没抢到"撤销与发送"的分界线：一个字节都没发。
            # 与文件写分支同一口径（GuardTimeout → 受控拒绝），不能让它变成一次
            # "效果未知"的崩溃——这次调用确实没出去。
            return self._abort_dispatch(request, dispatch_id, exc)
        except McpObjectBlocked as exc:
            # 等批准/等租约期间这个对象被别的 Run 标成阻断：同样一个字节都没发。
            # 按受控拒绝结算（`dispatch.mcp_object_blocked`），不能记成 unknown ——
            # "确定没发"被写成"可能发了"会凭空多挡一个目录。
            return self._abort_dispatch(request, dispatch_id, exc)
        if not boundary_marked:
            # 行已经被撤销/作废还照发，就等于让"撤销成功"这句话变假。这里不发，
            # 结论是确定的 `none_applied`。
            #
            # 与 `_abort_dispatch` 同一分工：派发行停在自己的终态（撤销/作废是
            # 审计事实，不被结果改写），`tool.result` 由**主循环**按返回值补写
            # ——派发层只在"自己真的写进结果"的行状态下才写事件，否则主循环会
            # 再补一条，日志里就出现两条同 call_id 的结果。
            self._set_request_state(request.request_id, STATE_REJECTED)
            return ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": "这次 MCP 调用在发出前已被撤销或作废；请求没有写出",
                    "reason": "mcp.revoked_before_send",
                    "mcp": dict(base_output["mcp"]),
                },
            )
        failpoint("mcp_marked")
        try:
            called = server.call_tool(
                target.tool, self._mcp_arguments(request), call_id=request.call_id
            )
        except McpTimeout as exc:
            # 客户端已经在超时时尽力发了 `notifications/cancelled`；这里只负责
            # 如实结算：**请求可能已经被执行**，所以是 unknown。
            output = dict(base_output)
            output["mcp"] = {**output["mcp"], "is_error": None, "timeout": True}
            output["reason"] = "mcp.timeout"
            output["error"] = str(exc)
            result = ToolResult("failed", "unknown", output)
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result
        except McpClosed as exc:
            output = dict(base_output)
            output["mcp"] = {**output["mcp"], "is_error": None, "closed": True}
            output["reason"] = "mcp.closed"
            output["error"] = str(exc)
            result = ToolResult("failed", "unknown", output)
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result
        except McpCallError as exc:
            # 协议层拒绝（解析/方法/参数）说明请求没有被当成工具调用执行，
            # 所以是确定结论 `none_applied`；其余错误码（含 -32603 内部错误）
            # 可能已经在执行后失败 → unknown + 已登记的规则编号。
            if exc.protocol_rejection:
                output = dict(base_output)
                output["mcp"] = {**output["mcp"], "is_error": None, "code": exc.code}
                output["reason"] = "mcp.rejected"
                output["error"] = str(exc)
                result = ToolResult("failed", "none_applied", output)
                self._commit_result(request, dispatch_id, result, target, intent_seq)
                return result
            return self._fail_unknown(
                request, dispatch_id, target, exc, "dispatch.mcp_protocol_error"
            )
        except (McpProtocolError, McpError) as exc:
            return self._fail_unknown(
                request, dispatch_id, target, exc, "dispatch.mcp_protocol_error"
            )
        except Exception as exc:  # noqa: BLE001 - 执行器自己炸了：效果未知
            return self._fail_unknown(request, dispatch_id, target, exc)

        failpoint("mcp_after_call")
        content_text = list(called.text_parts)
        kept_text = "\n".join(content_text)
        mcp_output = {
            **base_output["mcp"],
            "is_error": called.is_error,
            "content": content_text,
            "unsupported_content": list(called.unsupported_types),
            "structured": called.structured,
            "bytes": called.bytes,
            "truncated": called.truncated,
            "dropped_bytes": called.dropped_bytes,
            "dropped_blocks": called.dropped_blocks,
            "duration_ms": called.duration_ms,
            "request_id": called.request_id,
            # 执行摘要进凭据的 after_state（mcp:<sha256>）；没有磁盘可比对的
            # after_state 时凭据无从"独立核对"，所以这里必须给。
            "after_state": "mcp:" + sha256_hex(
                "\x00".join(
                    [
                        target.server,
                        target.tool,
                        str(called.request_id),
                        kept_text,
                        str(called.is_error),
                    ]
                ).encode("utf-8")
            ),
        }
        output: dict[str, Any] = {"mcp": mcp_output}
        status = "failed" if called.is_error else "succeeded"
        # 与 `run_command` 同一口径：完成落地的调用记 applied（它确实执行过），
        # isError 只影响 status；"是否动过、动了什么"我们无法核对，已登记。
        result = ToolResult(status, "applied", output)
        try:
            self._commit_result(request, dispatch_id, result, target, intent_seq)
        except Exception as exc:  # noqa: BLE001 - 外部调用已完成而结果事务失败：效果未知
            return self._fail_unknown(request, dispatch_id, target, exc)
        failpoint("after_result")
        return result

    def _mcp_arguments(self, request: ActionRequest) -> dict[str, Any]:
        """MCP 工具参数：从**权威库**取本次派发的参数（不信任调用方给的副本）。"""
        stored = self.get_request(request.request_id)
        raw = {} if stored is None else dict(stored.args)
        return raw

    def _dispatch_network(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _NetworkTarget,
        intent_seq: int,
        admitted: AdmittedCall,
    ) -> ToolResult:
        """网络分支：先查取消、再预占字节、最后才发包。

        顺序是刻意的：任何一步在前两步失败都不会产生一个请求——"取消或额度
        耗尽后无新请求"是这样成立的，而不是靠执行器自觉。
        """
        _pub = sys.modules["tianshu.dispatcher"]
        execution_guard = _pub.execution_guard
        net_fetch = _pub.net_fetch
        failpoint = _pub.failpoint
        # 1) 已取消 / 已暂停：一个包都不发。
        if self._cancel_requested(request.run_id):
            # 注意：`tool.result.status` 的封闭集没有 "cancelled"（契约只认
            # succeeded/failed/rejected/unknown）。取消的语义放进
            # `output.reason = "net.cancelled"`，状态按 failed 记——"没发出去"
            # 是确定结论，`effect_state=none_applied`。
            result = ToolResult(
                "failed",
                "none_applied",
                {
                    "error": "Run 已取消：没有发出网络请求",
                    "reason": "net.cancelled",
                    "url": target.uri,
                },
            )
            self._abort_dispatch(
                request, dispatch_id,
                DispatchDenied("Run 已取消或暂停", "dispatch.not_running"),
            )
            return result

        stored_req = self.get_request(request.request_id)
        net_args = {} if stored_req is None else dict(stored_req.args)
        effective_max_bytes = self._effective_net_max_bytes(net_args)
        call_limits = (
            self._net_limits
            if effective_max_bytes == self._net_limits.max_bytes
            else replace(self._net_limits, max_bytes=effective_max_bytes)
        )

        # 与撤销/改目标共用守卫；取消/暂停与此处共用同库写事务。
        # 所有资格和预算在执行开始标记前复核，提交后才允许请求越界。
        try:
            with execution_guard(
                self._require_data_root(), timeout_ms=self._guard_timeout_ms
            ):
                with self._log.transaction():
                    self._verify_resource_execution(admitted)
                    reservation = self._reserve_network_bytes(request, args=net_args)
                    self._start_resource_execution(request, dispatch_id)
        except (DispatchDenied, GuardTimeout, ApprovalError, JobLost, RunFenced) as exc:
            return self._abort_dispatch(request, dispatch_id, exc)
        except (BudgetRefused, BudgetCancelled) as exc:
            # 预占被拒 = 确定没发包。走既有的"复审拒绝"路径，事件/派发状态
            # 与其它守卫内拒绝保持同一形状。
            return self._abort_dispatch(
                request,
                dispatch_id,
                DispatchDenied(str(exc), "dispatch.budget_refused"),
            )

        # 3) 发包。
        try:
            fetched = net_fetch(
                target.target,
                limits=call_limits,
                cancel=lambda: self._cancel_requested(request.run_id),
            )
        except NetCancelled as exc:
            result = ToolResult(
                "failed",
                "none_applied",
                {
                    "error": str(exc),
                    "reason": "net.cancelled",
                    "url": target.uri,
                },
            )
            # 取消可能发生在读到一半之后：字节用量未知，保留预占。
            self._settle_network_bytes(reservation, None)
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result
        except NetError as exc:
            result = ToolResult(
                "failed",
                "none_applied",
                {"error": str(exc), "reason": exc.reason, "url": target.uri},
            )
            # 连不上 / 连接前私有地址阻断 = 确定一个字节都没读；其余失败
            # （超时 / 读到一半断了）用量未知，保留预占。
            known_zero = exc.reason in (
                "net.connect",
                "net.connect_timeout",
                "net.tls",
                "net.private_address_blocked",
                "net.arg_not_canonical",
                "net.arg_invalid",
                "net.limits",
            )
            self._settle_network_bytes(reservation, 0 if known_zero else None)
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result
        except Exception as exc:  # noqa: BLE001 - 执行器自己炸了：效果未知
            self._settle_network_bytes(reservation, None)
            return self._fail_unknown(request, dispatch_id, target, exc)

        # 4) 先核验内联分页窗口，再落内容引用、最后写结果事件。
        raw_offset = net_args.get("offset", 0)
        page_offset = (
            raw_offset
            if isinstance(raw_offset, int)
            and not isinstance(raw_offset, bool)
            and raw_offset >= 0
            else 0
        )
        raw_length = net_args.get("length", self._net_limits.summary_bytes)
        page_length = (
            raw_length
            if isinstance(raw_length, int)
            and not isinstance(raw_length, bool)
            and raw_length > 0
            else self._net_limits.summary_bytes
        )
        try:
            summary, page_bytes, next_offset, clipped = net_summary_page(
                fetched, offset=page_offset, length=page_length
            )
        except NetError as exc:
            self._settle_network_bytes(reservation, len(fetched.body))
            result = ToolResult(
                "failed",
                "none_applied",
                {"error": str(exc), "reason": exc.reason, "url": fetched.uri},
            )
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result

        try:
            ref = self._write_content(request, fetched.body)
        except Exception as exc:  # noqa: BLE001 - 落盘失败：确定没有事件承诺
            self._settle_network_bytes(reservation, len(fetched.body))
            result = ToolResult(
                "failed",
                "none_applied",
                {
                    "error": f"网络结果无法落盘：{type(exc).__name__}: {exc}",
                    "url": fetched.uri,
                    "reason": "net.content_unavailable",
                },
            )
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result

        output: dict[str, Any] = {
            "url": fetched.uri,
            "status": fetched.status,
            "content_type": fetched.content_type,
            "bytes": len(fetched.body),
            "truncated": fetched.truncated or clipped,
            "redirects": list(fetched.redirects),
            "elapsed_ms": fetched.elapsed_ms,
            "ref": ref,
            "summary": summary,
            "offset": page_offset,
            "page_bytes": page_bytes,
            "next_offset": next_offset,
        }
        if not fetched.is_text:
            output["binary"] = True
        result = ToolResult("succeeded", "none_applied", output)
        self._settle_network_bytes(reservation, len(fetched.body))
        try:
            self._commit_result(request, dispatch_id, result, target, intent_seq)
        except Exception as exc:  # noqa: BLE001 - 结果事务失败：按未知收束
            return self._fail_unknown(request, dispatch_id, target, exc)
        failpoint("after_result")
        return result

    # ---- 委派执行 ----

    def _subtask_limits(self, args: dict[str, Any]) -> dict[str, int]:
        """spawn 的局部上限：默认值 + 固定 deadline。

        两处调用（`_authority_subtask` 的限额检查与这里真正落库）必须得到同一个
        结论，所以只有这一个函数负责"补默认值"；deadline 在**spawn 那一刻**
        钉成 `now + 300s`（相对时间进账本，绝对值进 `subtasks.limits`）。
        """
        return clean_subtask_limits(
            {
                key: args[key]
                for key in ("max_turns", "max_tokens")
                if key in args
            }
        )

    def _reserve_subtask(self, request: ActionRequest, subtask_id: str) -> object | None:
        """在父账户的 `subtasks` 维度预占 1。

        账户不存在（直接调派发的单元测试 / 没有账本的装配）时不预占，也不
        伪造一个账户——如实登记为"未记账"，而不是让账本看起来有一笔占用。
        """
        if self._budget is None:
            return None
        account_id = f"run:{request.run_id}"
        if getattr(self._budget, "account", lambda _a: None)(account_id) is None:
            return None
        return self._budget.reserve(
            account_id,
            attempt_id=subtask_budget_attempt(request.run_id, subtask_id),
            run_id=request.run_id,
            call_id=request.call_id,
            need={"subtasks": 1},
        )

    def _ensure_child_budget_account(
        self,
        request: ActionRequest,
        child_run_id: str,
        limits: dict[str, int],
        *,
        created_at: int,
    ) -> None:
        """子 Run 的账户：`run:<child_run_id>`，父链指向父（=根）Run 账户。

        子账户**只声明局部上限**，不继承父任务剩余余量当自己的上限：父链负责
        扣减，局部上限负责"这个子任务最多花多少"。父账户不存在时不建子账户
        （建了会因 `parent_id` 不存在而抛错，而伪造一个父又会让账本说假话）。
        """
        if self._budget is None:
            return
        parent_id = f"run:{request.run_id}"
        if getattr(self._budget, "account", lambda _a: None)(parent_id) is None:
            return
        self._budget.create_account(
            f"run:{child_run_id}",
            limits={
                "tokens": int(limits.get("max_tokens", 0)),
                "requests": int(limits.get("max_turns", 0)),
            },
            parent_id=parent_id,
            run_id=child_run_id,
            label="subtask",
            # deadline 从 `subtasks.created_at` 起算，**不**再读一次墙钟：
            # 两次读钟在 CI 上可能相差 ±1ms（NTP 回拨），会让"deadline 正好是
            # 300s"这条可复现的不变量变成偶发失败。创建时间就是同一个事务里
            # 已经落库的那个时刻，用它才既可复现又与账本行一致。
            deadline_ms=created_at + int(limits.get("deadline_ms", 0)),
        )

    def _dispatch_subtask(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _SubtaskTarget,
        intent_seq: int,
        args: dict[str, Any],
        admitted: AdmittedCall,
    ) -> ToolResult:
        """委派分支：spawn / cancel / status / result 四个工具一个入口。

        为什么纯读也走这里：父任务身份、父 Run 作用域与"子任务是否存在"都要在
        这里判（`_authority_subtask()` 门 6）。绕过派发就等于把"不存在"伪装成
        空结果——那是 D1 明确拒绝的形状。
        """
        if request.tool_id == "subtask.spawn":
            return self._dispatch_subtask_spawn(
                request, dispatch_id, target, intent_seq, args, admitted
            )
        with self._log.transaction():
            self._verify_resource_execution(admitted)
            self._start_resource_execution(request, dispatch_id)
        if request.tool_id == "subtask.cancel":
            return self._dispatch_subtask_cancel(
                request, dispatch_id, target, intent_seq
            )
        if request.tool_id in ("subtask.status", "subtask.result"):
            return self._dispatch_subtask_read(
                request, dispatch_id, target, intent_seq
            )
        # 四个工具以外的 id 在 `_authority_subtask()` 就被拒了，这里再显式
        # 炸一次：宁可报"未知子任务工具"，也不要让新工具被当成 result 执行。
        raise TianshuError(f"未知的子任务工具：{request.tool_id}")

    def _dispatch_subtask_spawn(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _SubtaskTarget,
        intent_seq: int,
        args: dict[str, Any],
        admitted: AdmittedCall,
    ) -> ToolResult:
        """spawn：子 Task + 子 Run + 子 Job + `subtasks` 行 + 父 Run 事件，同一事务。

        预算预占也在这一个事务里：预占被拒时整笔回滚，父 Run 的日志里不会出现
        "派了但没派成"的半条记录。
        """
        failpoint = sys.modules["tianshu.dispatcher"].failpoint
        subtask_id = target.subtask_id
        goal = str(args.get("goal") or "").strip()
        limits = self._subtask_limits(args)
        # W10（subtask.spawn 2.0）：显式写声明。缺省 False 与旧版逐字节一致；
        # 非布尔值进不了这里（`validate_args` 按工具 schema 先拒），这里的
        # isinstance 是防御性双保险——布尔语义绝不允许被真值 truethy 混过。
        write = args.get("write", False)
        if not isinstance(write, bool):
            raise DispatchDenied(
                f"write 必须是布尔值：{write!r}", "dispatch.subtask_rejected"
            )
        try:
            with self._log.transaction() as tx:
                # 资格、数量上限、父版本和创建同事务，不能先读新版 Task 再
                # 搭配旧 Run 建孩子；外层执行守卫同时排除了撤销与目标变更。
                auth = self._verify_resource_execution(admitted)
                self._reserve_subtask(request, subtask_id)
                self._start_resource_execution(request, dispatch_id)
                subtask = self._tasks.create_subtask(
                    auth.task,
                    auth.run,
                    subtask_id=subtask_id,
                    goal=goal,
                    limits=limits,
                    write=write,
                )
                self._ensure_child_budget_account(
                    request,
                    subtask.child_run_id,
                    limits,
                    created_at=subtask.created_at,
                )
                tx.append(
                    request.run_id,
                    "subtask.spawned",
                    {
                        "subtask_id": subtask.subtask_id,
                        "child_task_id": subtask.child_task_id,
                        "child_run_id": subtask.child_run_id,
                        "job_id": subtask.job_id,
                        # 目标正文只留在子 Task 里；父 Run 只记摘要。
                        "goal_digest": sha256_hex(goal.encode("utf-8")),
                        "limits": dict(subtask.limits),
                    },
                    task_id=request.task_id,
                    actor_kind="core",
                )
        except (BudgetRefused, BudgetCancelled) as exc:
            # 预占被拒 = 确定什么都没建。走既有的"守卫内拒绝"路径，事件与派发
            # 状态与其它守卫内拒绝保持同一形状（effect_state=none_applied）。
            return self._abort_dispatch(
                request,
                dispatch_id,
                DispatchDenied(str(exc), "dispatch.budget_refused"),
            )

        result = ToolResult(
            "succeeded",
            "none_applied",
            {
                "subtask_id": subtask.subtask_id,
                "child_task_id": subtask.child_task_id,
                "child_run_id": subtask.child_run_id,
                "job_id": subtask.job_id,
                # 排队就是排队：D1 没有执行器，不假装已经跑起来。
                "state": subtask.state,
                "limits": dict(subtask.limits),
            },
        )
        self._commit_result(request, dispatch_id, result, target, intent_seq)
        failpoint("after_result")
        return result

    def _dispatch_subtask_cancel(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _SubtaskTarget,
        intent_seq: int,
    ) -> ToolResult:
        """cancel：只记录取消请求（`cancelling`），**不**写 `cancelled`。

        真正停下由执行侧在安全点确认（D2）。已终结的子任务幂等返回当前状态，
        不报错，也不假装撤销。
        """
        subtask = self._tasks.request_subtask_cancel(
            request.run_id, target.subtask_id
        )
        if subtask is None:  # pragma: no cover - 授权阶段已确认存在
            result = ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": f"子任务不存在：{target.subtask_id}",
                    "rule_id": "dispatch.subtask_not_found",
                    "subtask_id": target.subtask_id,
                },
            )
        else:
            finished = subtask.state in SUBTASK_TERMINAL_STATES
            result = ToolResult(
                "succeeded",
                "none_applied",
                {
                    "subtask_id": subtask.subtask_id,
                    "state": subtask.state,
                    "cancel_requested": (
                        not finished and subtask.state == "cancelling"
                    ),
                    # 已经结束的子任务：如实说"没有可撤销的执行"，不是"撤销成功"。
                    "already_finished": finished,
                    "child_run_id": subtask.child_run_id,
                    "job_id": subtask.job_id,
                },
            )
        self._commit_result(request, dispatch_id, result, target, intent_seq)
        return result

    def _dispatch_subtask_read(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _SubtaskTarget,
        intent_seq: int,
    ) -> ToolResult:
        """status / result：读本地状态与已落库结果，**不**触发执行。"""
        subtask = self._tasks.get_subtask(request.run_id, target.subtask_id)
        if subtask is None:  # pragma: no cover - 授权阶段已确认存在
            result = ToolResult(
                "rejected",
                "none_applied",
                {
                    "error": f"子任务不存在：{target.subtask_id}",
                    "rule_id": "dispatch.subtask_not_found",
                    "subtask_id": target.subtask_id,
                },
            )
            self._commit_result(request, dispatch_id, result, target, intent_seq)
            return result
        now = self._clock()
        if request.tool_id == "subtask.status":
            child = self._tasks.get_run(subtask.child_run_id)
            ended = subtask.ended_at
            output: dict[str, Any] = {
                "subtask_id": subtask.subtask_id,
                "state": subtask.state,
                "child_task_id": subtask.child_task_id,
                "child_run_id": subtask.child_run_id,
                # 子 Run 的状态与子任务状态是两个维度：都给出，不互相冒充。
                "child_run_state": child.state if child is not None else None,
                "job_id": subtask.job_id,
                "limits": dict(subtask.limits),
                "created_at": subtask.created_at,
                "elapsed_ms": max(0, (ended if ended is not None else now)
                                  - subtask.created_at),
                "error": subtask.error,
            }
        elif subtask.state not in SUBTASK_TERMINAL_STATES:
            # 未完成不是失败、也不是空结果：done=false 只说"还没有"。
            output = {
                "subtask_id": subtask.subtask_id,
                "state": subtask.state,
                "done": False,
            }
        else:
            output = {
                "subtask_id": subtask.subtask_id,
                "state": subtask.state,
                "done": True,
                # 终态的 `summary` 恒为字符串：行里的 NULL 只代表"没有正文"
                # （fence / 取消不会写摘要），不是"未知"。把 None 直接抛给
                # 调用方，会让回放的形状校验把一条**合法**结果判成
                # "summary 不是字符串"——审计工具在自己的产出上误报。
                "summary": subtask.summary or "",
                "result_ref": subtask.result_ref,
                "error": subtask.error,
            }
            # 用量从子 Run 的游标账本读。口径是"**未知不写、零照写**"：
            #
            # - 没有游标行 / 账本里没有这个键 → 这个维度的用量**未知**，键不出现在
            #   output 里（`int(None or 0)` 会把"没记账"洗成"花了 0"，正是预算口径
            #   `未知不是零` 要禁止的事）；
            # - 账本里显式写着 `0` → 这是已发生的事实（例如模型回合已记账、确实没
            #   花 token），照写 0。
            #
            # 两个维度各自判定：只知道 turns 时就只给 turns，不拿 0 补齐另一个。
            stored = self._tasks.load_cursor(subtask.child_run_id)
            if stored is not None:
                ledger = stored[0].budget_ledger or {}
                usage = {
                    key: int(ledger[key])
                    for key in ("turns", "tokens")
                    if isinstance(ledger.get(key), int)
                    and not isinstance(ledger.get(key), bool)
                }
                if usage:
                    output["usage"] = usage
        result = ToolResult("succeeded", "none_applied", output)
        self._commit_result(request, dispatch_id, result, target, intent_seq)
        return result


    # ---- 控制 Run（undo / recover 的写入口） ----

    def _takeover_refusal_reason(self, previous_run: str, assessment: Any) -> str:
        """接管被拒时给人看的说明：要说清"前任留下了什么没核实"。"""
        bits: list[str] = []
        if assessment.unresolved:
            bits.append(f"无法核实的调用 {list(assessment.unresolved)}")
        if assessment.unknown:
            bits.append(f"磁盘状态既非写前也非写后 {list(assessment.unknown)}")
        if assessment.corrupt:
            bits.append("快照清单损坏")
        if assessment.missing_manifest and assessment.pending_writes:
            bits.append("有写入意图却没有写前清单")
        detail = "；".join(bits) or "对账未通过"
        return (
            f"拒绝接管：前任 run {previous_run} 有未核实副作用（{detail}）。"
            "请先用 tianshu recover 核对并解除阻断，再重试；"
            "在核实之前该工作区不允许被新 owner 继续写"
        )

    def _make_takeover_reconciler(
        self, *, run_id: str, task_id: str, refusals: list[dict[str, Any]]
    ):
        """构造租约接管的对账回调（issue #61.2）。

        回调本身**只读**：调 `recovery.assess_run` 取前任状态。干净才在
        `acquire` 的事务里写 `run.control` 审计（与租约同一个提交）；不干净
        只把拒绝信息记进 `refusals` 并抛 `LeaseConflict`。拒绝的持久证据
        （`recovery.blocked` + 权威根）由 `open_control_run` 在事务回滚**之后**
        落——在回调里写事件会跟着 `acquire` 的事务一起回滚，日志会少一条。
        """

        def reconcile(previous_run_id: str, resource_path: str) -> None:
            # 局部 import：dispatcher / recovery 有共同的底层依赖，模块级互相
            # import 没必要，也容易在别人的重构里变成环。
            from .recovery import assess_run

            assessment = assess_run(self._data_root, previous_run_id, log=self._log)
            if assessment.blocks_takeover:
                reason = self._takeover_refusal_reason(previous_run_id, assessment)
                refusals.append(
                    {
                        "previous_run": previous_run_id,
                        "resource": resource_path,
                        "assessment": assessment.to_payload(),
                        "reason": reason,
                        "by_run": run_id,
                    }
                )
                raise LeaseConflict(reason)
            # 干净：留审计。用现有 run.control（不新增事件类型）；
            # `checked` 记录这次对账看到的状态，事后能回答"凭什么允许接管"。
            self._log.append(
                run_id,
                "run.control",
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "action": "lease_takeover",
                    "previous_run": previous_run_id,
                    "checked": assessment.to_payload(),
                },
                task_id=task_id,
                actor_kind="system",
            )

        return reconcile

    def _record_takeover_refusal(self, refusal: dict[str, Any]) -> None:
        """把"拒绝接管"落成可审计的阻断：日志事件 + 用户级权威根。

        必须在租约 acquire 的事务回滚之后调用（事件写在同一个事务里会被回滚，
        日志就丢了这条拒绝）。两步都做：只写日志的话换个数据根还是能绕过去，
        只写权威根的话本数据根回放不出"为什么被挡"。
        """
        previous_run = refusal["previous_run"]
        resource = str(Path(refusal["resource"]).resolve())
        checked = refusal["assessment"]
        call_ids = [*checked.get("unresolved", ()), *checked.get("unknown", ())]
        reason = (
            "接管被拒：前任 run 存在未核实副作用"
            f"（previous_run={previous_run}），阻断后续写入"
        )
        self._log.append(
            previous_run,
            "recovery.blocked",
            {
                "workspace": resource,
                "call_ids": call_ids,
                "reason": reason,
                "takeover_refused_by": refusal["by_run"],
            },
            actor_kind="system",
        )
        from . import authority
        from .authority import AuthorityUnavailable, AuthorityWriteFailed

        try:
            authority.register_blocked(resource, run_id=previous_run, reason=reason)
        except (AuthorityWriteFailed, AuthorityUnavailable) as exc:
            raise AuthorityWriteFailed(
                f"接管已拒绝，但跨数据根阻断未生效：{resource}（{exc}）"
            ) from exc

    def takeover_reconcile(
        self, *, run_id: str, task_id: str, refusals: list[dict[str, Any]]
    ) -> ReconcileFn:
        """租约接管的对账回调。

        主循环（`AgentLoop.start` / `resume_run`）与撤销/恢复的控制 Run 共用这
        一条实现：**所有**会抢 stale 租约的路径都走同一次对账，否则"完整 takeover"
        只保护控制 Run，真正的写入路径照样能被接管。
        """
        return self._make_takeover_reconciler(
            run_id=run_id, task_id=task_id, refusals=refusals
        )

    def record_takeover_refusals(self, refusals: list[dict[str, Any]]) -> None:
        """把被拒的接管落成阻断证据（必须在租约事务回滚**之后**调用）。

        只回答"证据登记是否完整"：全部成功就返回；有一条失败就抛
        `AuthorityWriteFailed`，调用方保留原 `LeaseConflict` 并附上这条失败——
        拒绝已经发生，不能因为写不进阻断证据就把"拒绝"改写成"允许"。
        """
        from .authority import AuthorityWriteFailed

        problems: list[str] = []
        for refusal in refusals:
            try:
                self._record_takeover_refusal(refusal)
            except TianshuError as exc:
                problems.append(str(exc))
        if problems:
            raise AuthorityWriteFailed("；".join(problems))

    def prepare_run(self, task_id: str, execution_policy: RunExecutionPolicyV1) -> None:
        task = self._tasks.get_task(task_id)
        if task is None:
            raise DispatchError(f"task 不存在：{task_id}")
        self._prepare_new_run(execution_policy, tuple(
            Path(b.root_path) for b in task.workspaces if b.can_write
        ))

    def _prepare_new_run(self, execution_policy: RunExecutionPolicyV1,
                         roots: tuple[Path, ...]) -> None:
        if execution_policy != PURE_ONLY:
            self._tasks.disk_admission.assert_stores(
                self._log, snapshots=self._snapshots, memory=self._memory,
                contents=self._contents, data_root=self._data_root,
            )
        self._tasks.require_new_run(execution_policy, writable_roots=roots)
        if execution_policy != PURE_ONLY and self._mcp_clients is not None:
            from .tool_registry import build_registry, set_registry

            if not self._mcp_prepared:
                self._mcp_clients.start_all()
                self._mcp_prepared = True
                self._registry = build_registry(self._mcp_clients)
                set_registry(self._registry)

    def prepare_pending_mcp(
        self, run_id: str, claim: JobClaim, request_id: str | None,
        tool_id: str, args: dict[str, Any],
    ) -> None:
        """只为已有的真实待派发 MCP 请求恢复发现；读/模型不以此为前提。"""
        if not tool_id.startswith("mcp__"):
            return
        stored = self.get_request(request_id) if request_id else None
        if stored is None and self._mcp_prepared:
            return  # 当前模型刚提出的新调用，随后完整 prepare 判定。
        if (stored is None or stored.request.run_id != run_id
                or stored.request.tool_id != tool_id
                or stored.request.args_hash != sha256_hex(canonical_bytes(args))
                or stored.args != args):
            raise DispatchDenied("缺少绑定的待恢复 MCP 请求", "dispatch.not_admitted")
        # 接管可以更新 owner，但不能改原动作、目标、schema 或资格。
        request = replace(stored.request, job_id=claim.job_id,
                          claim_epoch=claim.claim_epoch, owner_instance_id=claim.owner_instance_id)
        if self._mcp_prepared:
            self._check_tool_identity(request)
            return  # 已有客户端按原 prepare 路径记录授权/对象阻断拒绝。
        auth = self._authority(request, claim, args=args, mcp_discovery=True)
        if self._cancel_requested(run_id):
            raise DispatchDenied("Run 已取消或暂停", "dispatch.not_running")
        self._tasks.disk_admission.assert_stores(
            self._log, snapshots=self._snapshots, memory=self._memory,
            contents=self._contents, data_root=self._data_root,
        )
        self._tasks.require_effect_space(run_id, targets=(), data_root=self._data_root)
        if self._mcp_clients is None:
            raise DispatchDenied("MCP 未配置", "dispatch.mcp_server_unavailable")
        from .tool_registry import build_registry, set_registry

        self._mcp_clients.start_all()
        self._mcp_prepared = True
        self._registry = build_registry(self._mcp_clients)
        set_registry(self._registry)
        target = self._resolve_target(request, auth)
        # 新发现不能静默升级原 ToolSpec，随后仍走原审批/派发核验。
        self._check_run_execution(request, target)

    def open_control_run(
        self,
        *,
        goal: str,
        workspaces: list[WorkspaceBinding | str | dict[str, Any]],
        memory_scopes: list[MemoryScope | str] = (),
    ) -> ControlRun:
        """为"用户直接发起的写"建立自己的 Task/Run/JobClaim。

        不借用历史 Run 的身份：那个 Run 早就结束了，它的 revision/epoch/claim
        都不再有效。撤销操作用它自己的身份，并把"撤销的是谁"写进目标里。

        抢 stale 租约前**必须对账**（issue #61.2）：前任 run 有未核实副作用时
        拒绝接管、登记阻断，而不是在同一棵目录树上接着写。

        `memory_scopes`（契约 v1.2）：控制面显式声明的记忆作用域，落进
        `task.created` payload 与 `Task.is_control`。记忆动作据此授权；文件动作
        仍然只认目录绑定 + 租约，不因这组声明多拿一个字节。
        """
        goal = (goal or "").strip()
        if not goal:
            raise DispatchError("控制 Run 的目标不能为空")
        declared: list[str] = []
        for item in memory_scopes:
            scope = item if isinstance(item, MemoryScope) else parse_memory_uri(item)
            declared.append(scope.to_uri())
        bindings = [WorkspaceBinding.of(b) for b in workspaces]
        self._prepare_new_run(EFFECTS, tuple(Path(b.root_path) for b in bindings if b.can_write))
        # start_run 的最终空间复测拒绝时，控制 Task 与 task.created 也一起回滚。
        with self._log.transaction():
            task = self._tasks.create_task(
                goal, bindings, control=True, memory_scopes=declared
            )
            run = self._tasks.start_run(task.task_id)
        writable = [b.identity() for b in task.workspaces if b.can_write]
        # **零绑定控制 Run 是合法的**：人直接发起的记忆操作（CLI 的
        # `memory confirm` / `memory forget`，scope=user 时没有任何目录可绑）
        # 走的就是这条路。以前这里强制要求至少一个可写绑定，等于让"人的记忆
        # 决定"无法被记录成一个带 intent/result + 审批事件的副作用。
        #
        # 这不是在放宽文件写入权限：没有可写绑定就没有租约，而文件动作在派发
        # 层还必须过两道**独立**判定——
        #   1. `_authority()`：`_leases.held_paths()` 为空 →
        #      `dispatch.lease_lost`（"本实例没有持有任何租约"）；
        #   2. `_resolve_target()`：目标不被任何绑定覆盖 →
        #      `c4.outside_workspace_unsupported`。
        # 记忆动作则由 `memory_scope_allowed()` 单独核验作用域，它本来就不
        # 依赖目录租约（记忆没有目录）。
        #
        # 有绑定时仍然**先对账再接管**（issue #61.2）：主循环与撤销/恢复共用
        # 同一条对账实现，不因为"控制 Run 可以不绑目录"就跳过。
        refusals: list[dict[str, Any]] = []
        if writable:
            try:
                self._leases.acquire(
                    writable,
                    run.run_id,
                    reconcile=self.takeover_reconcile(
                        run_id=run.run_id, task_id=task.task_id, refusals=refusals
                    ),
                )
            except LeaseConflict as exc:
                try:
                    self.record_takeover_refusals(refusals)
                except TianshuError as record_exc:
                    raise LeaseConflict(
                        f"{exc}；且拒绝接管的阻断证据登记不完整：{record_exc}"
                    ) from exc
                raise
        # 入队与认领**必须原子**：Daemon 的驱动线程随时在 `claim_next()` 扫
        # pending 作业，先 enqueue 再 claim 会让它在窗口里抢走这条作业，控制面
        # 随即吃 `JobLost`（"仍由活着的 owner 持有"）。CLI 单进程时窗口是微秒级，
        # 但 UI/daemon 里的记忆写端点真在 CI 上撞到过（`internal` 报错）。
        _job, claim = self._claims.enqueue_and_claim(run.run_id)
        run = self._tasks.transition(run.run_id, RUN_RUNNING, claim=claim,
                                     reason="control_run")
        return ControlRun(
            task=task,
            run=run,
            claim=claim,
            lease_paths=tuple(str(b.root_path) for b in task.workspaces if b.can_write),
            memory_scopes=tuple(task.memory_scopes),
        )

    def close_control_run(self, control: ControlRun, *, outcome: str = "succeeded",
                          effect_state: str = "none_applied") -> None:
        self._leases.release_run(control.run.run_id)
        self._tasks.finish_run(
            control.run.run_id, outcome=outcome, effect_state=effect_state
        )
        try:
            self._claims.finish(control.claim, outcome=outcome)
        except (JobLost, TianshuError):
            # 作业已经被别人收尾：控制 Run 的结论仍以 run.finish 为准
            pass
