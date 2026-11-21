"""派发记录、审计脱敏迁移、拒绝/中止落账与结果凭据单事务提交（内部 Mixin，由 `Dispatcher` 继承）。

保持 `_reject` / `_reject_runtime` / `_fail_unknown` 位置参数签名与全部对象阻断负控锚点逐字不变，
零契约变更。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ._dispatcher_types import (
    _BACKFILL_MARKER,
    DISPATCH_ERROR,
    DISPATCH_EXECUTING,
    DISPATCH_FINISHED,
    DISPATCH_INTENT,
    DISPATCH_REJECTED,
    DISPATCH_ROW_REJECTED,
    KIND_REJECTED,
    STATE_DONE,
    STATE_REJECTED,
    DispatchDecision,
    DispatchDenied,
    _audit_args,
    _Authority,
    _McpTarget,
    _MemoryTarget,
    _NetworkTarget,
    _SubtaskTarget,
    _Target,
)
from .errors import (
    McpObjectBlocked,
    RunFenced,
    TianshuError,
    ToolNotAllowed,
    WorkspaceBlocked,
)
from .events import EventLog, canonical_bytes, sha256_hex
from .guard import ensure_object_not_blocked
from .jobs import JobClaim, JobLost
from .leases import GuardTimeout, LeaseLost
from .permissions import ACTION_EXEC, ActionRequest, ApprovalError
from .receipt import (
    build_browser_form_receipt,
    build_exec_receipt,
    build_mcp_receipt,
    build_memory_receipt,
    build_receipt,
)
from .resources import is_memory_resource, mcp_block_keys
from .rules import check_rule_id
from .snapshot import SnapshotStore
from .tools import AdmittedCall, FileExecutor, ToolResult


class _DispatcherLedgerMixin:
    """派发表读写、外发边界推进、终态参数脱敏、拒绝与结果/凭据原子提交。"""

    _log: EventLog
    _conn: sqlite3.Connection
    _snapshots: SnapshotStore | None
    _data_root: Path | None
    _clock: Any
    _guard_timeout_ms: int
    _executor: FileExecutor

    def _insert_dispatch(
        self,
        request: ActionRequest,
        auth: _Authority,
        target: _Target,
        seq: int,
        approval_id: str | None,
        grant_id: str,
    ) -> None:
        now = self._clock()
        if isinstance(target, _MemoryTarget):
            # 记忆没有目录。`target` 记 scope URI，`workspace_root` 记 scope_id
            # （人类可读），`resource_key` 用稳定前缀避免与文件路径混淆。
            target_value = target.uri
            rel_path = ""
            workspace_root = target.scope.scope_id
            resource_key = f"memory:{target.scope.scope}:{target.scope.scope_id}"
        elif isinstance(target, _NetworkTarget):
            # 网络既没有目录也没有作用域：目标记规范 URI，资源键用源 + 路径，
            # 事后能直接回答"这次到底发了什么"。
            target_value = target.uri
            rel_path = ""
            workspace_root = ""
            resource_key = (
                f"network:{target.target.origin_uri}{target.target.path}"
            )
        elif isinstance(target, _McpTarget):
            # MCP 同样没有目录：目标记 mcp:// URI，资源键用 server/tool。
            target_value = target.uri
            rel_path = ""
            workspace_root = ""
            resource_key = f"mcp:{target.server}/{target.tool}"
        elif isinstance(target, _SubtaskTarget):
            # 委派既没有目录也没有作用域：目标记规范 URI，资源键按父 Run 作用域
            # 里的 slug 记（事后能直接回答"派的到底是谁"）。
            target_value = target.uri
            rel_path = ""
            workspace_root = ""
            resource_key = f"subtask:{target.subtask_id}"
        else:
            target_value = str(target.path)
            rel_path = target.rel_path
            workspace_root = target.workspace_root
            resource_key = target.identity.key
        self._conn.execute(
            "INSERT INTO dispatches (dispatch_id, run_id, task_id, call_id,"
            " request_id, request_digest, tool_id, action, resources, target,"
            " rel_path, workspace_root, resource_key, approval_id, grant_id,"
            " claim_epoch, owner_instance_id, attempt_id, intent_seq,"
            " boundary_protocol, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'intent',?,?)",
            (
                self._dispatch_id(request),
                request.run_id,
                request.task_id,
                request.call_id,
                request.request_id,
                request.digest(),
                request.tool_id,
                request.action,
                json.dumps(list(request.resources), ensure_ascii=False),
                target_value,
                rel_path,
                workspace_root,
                resource_key,
                approval_id,
                grant_id,
                request.claim_epoch,
                request.owner_instance_id,
                request.attempt_id,
                seq,
                now,
                now,
            ),
        )

    @staticmethod
    def _dispatch_id(request: ActionRequest) -> str:
        """派发记录的稳定身份。

        不能用裸 call_id：provider 给的短 ID（"c1"、"call_1"）在不同 Run 之间
        会重名，拿它当主键会让"另一个 run 的同名调用"互相覆盖。这里把 run 和
        call 一起哈希，既稳定（重算得出同一个值）又不会跨 run 误碰。
        """
        digest = sha256_hex(f"{request.run_id}:{request.call_id}".encode())
        return f"disp_{digest[:16]}"

    def mark_dispatch(self, run_id: str, call_id: str, state: str) -> None:
        """执行器把派发记录推进到 executing / done。"""
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=?"
                " WHERE run_id=? AND call_id=?",
                (state, self._clock(), run_id, call_id),
            )

    def _refuse_blocked_mcp_object(self, server: str, tool: str) -> None:
        """对象被阻断 → `DispatchDenied`（带 C4 规则编号，用户可见）。

        判定不了的两种情况（日志里有畸形对象阻断记录、权威根读不出来）同样拒绝：
        `ensure_object_not_blocked` 把它们转成 `McpObjectBlocked`，这里再统一
        转成一次"拒绝"而不是让派发崩掉。
        """
        keys = mcp_block_keys(server, tool)
        try:
            ensure_object_not_blocked(self._log, keys)
        except McpObjectBlocked as exc:
            raise DispatchDenied(str(exc), "dispatch.mcp_object_blocked") from exc

    def _mark_mcp_send_boundary(
        self, dispatch_id: str, *, server: str, tool: str,
        admitted: AdmittedCall | None = None,
    ) -> bool:
        """把这次 MCP 派发从 `intent` 推到 `executing` —— 外发边界。

        为什么需要它：`tools/call` 写进 server 的 stdin 之后，"发没发出去"在本机
        就不可证明了。此前恢复只能把"没有结果事件"一律读成 `unknown` + 阻断，
        连"请求连发都没发"的崩溃也一起挡（验收报告 §4-10 承认的过度阻断）。有了
        这条在发包**之前**提交的标记，恢复才有第二条路：

        - 行仍停在外发边界之前（`intent` / 已被撤销 / 已作废）→ 请求没写出 →
          `none_applied`，不阻断；
        - 行是 `executing` → 可能已经发出 → `unknown` + 阻断（保守方向）。

        标记本身也有代价：在"标记提交"与"真正写出"之间崩掉，恢复会按已发出处理。
        这是**有意**选的保守方向——宁可多挡一个目录，也不把可能已经发生的副作用
        说成没发生。

        只从 `intent` 推进（`where state=intent`）：撤销/作废是审计事实，不能让一
        个后到的执行把它改回"正在执行"。与 `revoke` 共用执行守卫，谁先拿到锁谁
        说话：撤销先到就不再发；标记先到，撤销会明确报错而不是假装成功。

        返回 `False` 表示这条派发已经不能再发了（被撤销/作废，或没有派发行）。

        **对象阻断复核也在这里**（与 `_advance_to_executing` 同一把锁内）：
        `_resolve_target` 已经判过一次，但"判定通过 → 真正写 stdin"之间可能隔着
        等批准、等租约；这期间另一个 Run 可能把同一个对象标成阻断（例如它刚超
        时、还没人核对）。检查放在守卫内、紧贴状态推进：**撤销**与**恢复侧**的
        阻断提交（恢复侧与运行期 unknown 的 `_commit_mcp_unknown`）共用这把守卫，
        排不进这道缝；`unblock_object` 是人工解除，与 `unblock()` 一样不抢守卫
         （解除只会放宽，不会漏挡）。本入口缺少实际数据根时拒绝外发；提交侧
        抢守卫超时时照常提交并写 `block_guard="timeout"`
        （见 docs/SPEC §3.4 线性化边界）。命中就抛 `McpObjectBlocked`，由
        `_dispatch_mcp` 按受控拒绝处理（一个字节都没发）。
        """
        keys = mcp_block_keys(server, tool)
        # 对象阻断仍先核，缺数据根不再能绕过执行资格/空间与真实票据。
        ensure_object_not_blocked(self._log, keys)
        execution_guard = sys.modules["tianshu.dispatcher"].execution_guard
        with execution_guard(self._require_data_root(), timeout_ms=self._guard_timeout_ms):
            with self._log.transaction():
                ensure_object_not_blocked(self._log, keys)
                row = self._conn.execute(
                    "SELECT state FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
                ).fetchone()
                if row is None or row["state"] != DISPATCH_INTENT:
                    return False
                if admitted is None or admitted.dispatch_id != dispatch_id:
                    raise DispatchDenied("MCP 外发缺少绑定票据", "dispatch.not_admitted")
                auth = self._verify_resource_execution(admitted)
                target = self._resolve_target(admitted.request, auth)
                if (not isinstance(target, _McpTarget)
                        or (target.server, target.tool) != (server, tool)):
                    raise DispatchDenied("MCP 外发目标与真实请求不符", "dispatch.not_admitted")
                return self._advance_to_executing(dispatch_id)

    def _advance_to_executing(self, dispatch_id: str) -> bool:
        with self._log.transaction():
            cursor = self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=?"
                " WHERE dispatch_id=? AND state=?",
                (DISPATCH_EXECUTING, self._clock(), dispatch_id, DISPATCH_INTENT),
            )
            return cursor.rowcount > 0

    def _admitted(
        self,
        request: ActionRequest,
        claim: JobClaim,
        target: _Target,
        seq: int,
        approval_id: str | None,
        grant_id: str,
    ) -> AdmittedCall:
        stored = self.get_request(request.request_id)
        if isinstance(target, _MemoryTarget):
            target_value = target.uri
            rel_path = ""
            workspace_root = target.scope.scope_id
            resource_key = f"memory:{target.scope.scope}:{target.scope.scope_id}"
        elif isinstance(target, _NetworkTarget):
            target_value = target.uri
            rel_path = ""
            workspace_root = ""
            resource_key = (
                f"network:{target.target.origin_uri}{target.target.path}"
            )
        elif isinstance(target, _McpTarget):
            target_value = target.uri
            rel_path = ""
            workspace_root = ""
            resource_key = f"mcp:{target.server}/{target.tool}"
        elif isinstance(target, _SubtaskTarget):
            target_value = target.uri
            rel_path = ""
            workspace_root = ""
            resource_key = f"subtask:{target.subtask_id}"
        else:
            target_value = str(target.path)
            rel_path = target.rel_path
            workspace_root = target.workspace_root
            resource_key = target.identity.key
        return AdmittedCall(
            dispatch_id=self._dispatch_id(request),
            request=request,
            request_digest=request.digest(),
            claim=claim,
            tool_id=request.tool_id,
            action=request.action,
            args=stored.args if stored is not None else {},
            target=target_value,
            rel_path=rel_path,
            workspace_root=workspace_root,
            resource_key=resource_key,
            resources=tuple(request.resources),
            intent_seq=seq,
            attempt_id=request.attempt_id,
            approval_id=approval_id,
            grant_id=grant_id,
        )

    def _store_request(
        self,
        request: ActionRequest,
        args: dict[str, Any],
        state: str,
        reason: str,
        rule_id: str,
        *,
        approval_id: str | None = None,
        args_redacted: bool = False,
    ) -> None:
        payload = canonical_bytes(request.to_canonical_dict()).decode("utf-8")
        args_payload = canonical_bytes(args).decode("utf-8")
        now = self._clock()
        with self._log.transaction():
            self._conn.execute(
                "INSERT INTO dispatch_requests (request_id, task_id, run_id,"
                " request_digest, payload, args_payload, args_redacted, state,"
                " reason, rule_id, approval_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(request_id) DO UPDATE SET"
                " state=excluded.state, reason=excluded.reason,"
                " rule_id=excluded.rule_id, approval_id=excluded.approval_id,"
                # 同一 `request_id` 会被写多次：`prepare()` 先存完整参数，之后
                # 被拒绝改成脱敏形态；反过来，终态之后同一请求被**重新发起**时
                # 又要拿回完整参数（执行期要用它重算 `args_hash`）。所以这里取
                # "最后一次写入"的形态，不做单向合并——单向保留会让重新发起
                # 的合法请求永远判成"参数被改过"。
                " args_payload=excluded.args_payload,"
                " args_redacted=excluded.args_redacted,"
                " updated_at=excluded.updated_at",
                (
                    request.request_id,
                    request.task_id,
                    request.run_id,
                    request.digest(),
                    payload,
                    args_payload,
                    1 if args_redacted else 0,
                    state,
                    reason,
                    rule_id,
                    approval_id,
                    now,
                    now,
                ),
            )

    def _set_request_state(
        self, request_id: str, state: str, *, approval_id: str | None = None
    ) -> None:
        with self._log.transaction():
            self._conn.execute(
                "UPDATE dispatch_requests SET state=?, approval_id=COALESCE(?,"
                " approval_id), updated_at=? WHERE request_id=?",
                (state, approval_id, self._clock(), request_id),
            )
            if state in (STATE_DONE, STATE_REJECTED):
                # 终态之后不会再有人从这张表取参数去执行，正文可以脱敏了。
                self._redact_terminal_args(request_id)

    def _migrate_redacted_args(self) -> None:
        """一次性迁移：给升级前的终态行补脱敏，成功后才落持久标记。

        标记在**同一个事务**里写入，所以中途失败/被 kill 时标记不会留下，
        下次打开会重试；只有标记存在才算"这件事真的做过"。
        """
        row = self._conn.execute(
            "SELECT value FROM dispatch_meta WHERE key = ?",
            (_BACKFILL_MARKER,),
        ).fetchone()
        if row is not None:
            return
        with self._log.transaction():
            self._backfill_redacted_args()
            self._conn.execute(
                "INSERT INTO dispatch_meta(key, value) VALUES (?, '1')"
                " ON CONFLICT(key) DO NOTHING",
                (_BACKFILL_MARKER,),
            )

    def _backfill_redacted_args(self) -> None:
        """升级前就已处于终态的行：补做脱敏。**必须在调用方的事务里**。

        `args_redacted` 列是 N5 才加的，老库里那些 `done` / `rejected` 行不会
        自己回到终态，所以迁移时必须主动扫一遍。只处理 `args_redacted = 0` 的
        终态行；解析失败的行**保持原样**——宁可让审计看到"这一行没脱敏"，也不
        要把读不懂的行直接抹成不可审计。非记忆行 `_audit_args` 原样返回，不动。
        """
        rows = self._conn.execute(
            "SELECT request_id, payload, args_payload FROM dispatch_requests"
            " WHERE args_redacted = 0 AND state IN (?, ?)",
            (STATE_DONE, STATE_REJECTED),
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            try:
                request = ActionRequest.of(json.loads(row["payload"]))
                args = json.loads(row["args_payload"])
            except (TypeError, ValueError):  # 老库/外力改坏：跳过并保留原样
                continue
            if not isinstance(args, dict):
                continue
            redacted = _audit_args(request, args)
            if redacted is args:
                continue
            updates.append(
                (canonical_bytes(redacted).decode("utf-8"), row["request_id"])
            )
        for payload, request_id in updates:
            self._conn.execute(
                "UPDATE dispatch_requests SET args_payload = ?,"
                " args_redacted = 1 WHERE request_id = ? AND args_redacted = 0"
                " AND state IN (?, ?)",
                (payload, request_id, STATE_DONE, STATE_REJECTED),
            )

    def _redact_terminal_args(self, request_id: str) -> None:
        """请求进终态后，把 `args_payload` 里的记忆正文换成摘要。

        执行期的绑定校验与执行器取参数都发生在终态之前；此后这张表只剩审计
        用途。已经脱敏过的行直接跳过（幂等）。非记忆请求不动。
        """
        row = self._conn.execute(
            "SELECT payload, args_payload, args_redacted FROM dispatch_requests"
            " WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None or int(row["args_redacted"]):
            return
        try:
            request = ActionRequest.of(json.loads(row["payload"]))
            args = json.loads(row["args_payload"])
        except (TypeError, ValueError):  # pragma: no cover - 库被外力改坏
            return
        if not isinstance(args, dict):
            return
        redacted = _audit_args(request, args)
        if redacted is args:
            return
        self._conn.execute(
            "UPDATE dispatch_requests SET args_payload = ?, args_redacted = 1"
            " WHERE request_id = ?",
            (canonical_bytes(redacted).decode("utf-8"), request_id),
        )

    @staticmethod
    def _intent_args(request: ActionRequest, args: dict[str, Any]) -> dict[str, Any]:
        """写进 `tool.intent` 的参数（事件日志**不存记忆正文**）。

        F10 §3.1 要求事件日志"只存引用与审计元数据，不重复写记忆正文"。
        `memory.confirm` / `memory.forget` / `memory.erase` 的参数只有
        memory_id / revision / status，本身不是正文；**凡带 `content` 的记忆写**
        （`memory.propose` / `memory.remember` / `memory.edit`）都必须在写事件前
        拿掉正文，只留下可核对的长度与哈希。判断口径是"记忆资源 + 参数里有
        content"，不是罗列工具名——新增一个带正文的记忆写不会因为忘了登记就把
        正文漏进 append-only 的事件日志。完整参数仍在派发请求表里（受一次审批/
        派发绑定的操作记录），执行器也从那里取，不依赖事件里的副本。`args_hash`
        仍然绑完整参数，不能被这段脱敏改写。
        """
        if "content" not in args:
            return args
        if not any(is_memory_resource(resource) for resource in request.resources):
            return args
        safe = dict(args)
        content = safe.pop("content", None)
        if isinstance(content, str):
            raw = content.encode("utf-8")
            safe["content_omitted"] = True
            safe["content_bytes"] = len(raw)
            safe["content_sha256"] = sha256_hex(raw)
        return safe

    def _append(self, request: ActionRequest, event: str, payload: dict[str, Any]) -> int:
        return self._log.append(
            request.run_id,
            event,
            payload,
            task_id=request.task_id,
            actor_kind="core",
        )

    def _reject(
        self,
        request: ActionRequest,
        args: dict[str, Any],
        reason: str,
        rule_id: str,
        *,
        event: str,
    ) -> DispatchDecision:
        # 校验在**任何写入之前**：未登记的编号是代码 bug，不能先落一条日志再让人猜。
        rule_id = check_rule_id(rule_id, where=f"_reject:{event}")
        # 拒绝的行**永远不会执行**，所以这里可以脱敏而不破坏任何
        # 派发/审批绑定：被拒的尝试不该把记忆正文原文永久留在审计表里。
        redacted_args = _audit_args(request, args)
        self._store_request(
            request,
            redacted_args,
            STATE_REJECTED,
            reason,
            rule_id,
            # `args_redacted` 是审计事实字段：非记忆请求（或没有正文的记忆
            # 请求）本来就不该被标成"已脱敏"。
            args_redacted=redacted_args is not args,
        )
        self._append(
            request,
            event,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "action": request.action,
                "resources": list(request.resources),
                "reason": reason,
                "rule_id": rule_id,
            },
        )
        return DispatchDecision(
            KIND_REJECTED, request.request_id, request.digest(), reason=reason,
            rule_id=rule_id,
        )

    def _reject_runtime(
        self, request: ActionRequest, exc: BaseException, rule_id: str
    ) -> ToolResult:
        rule_id = check_rule_id(rule_id, where="_reject_runtime")
        reason = f"{type(exc).__name__}: {exc}"
        self._set_request_state(request.request_id, STATE_REJECTED)
        self._append(
            request,
            DISPATCH_REJECTED,
            {
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "reason": reason,
                "rule_id": rule_id,
            },
        )
        return ToolResult(
            "rejected",
            "none_applied",
            {"error": reason, "rule_id": rule_id, "request_id": request.request_id},
        )

    def _abort_dispatch(
        self, request: ActionRequest, dispatch_id: str, exc: BaseException
    ) -> ToolResult:
        """守卫内复核没过：一个字节都没写，明确记成 rejected。"""
        reason = f"{type(exc).__name__}: {exc}"
        rule_id = getattr(exc, "rule_id", None) or {
            ApprovalError: "dispatch.approval_revoked",
            RunFenced: "dispatch.run_fenced",
            ToolNotAllowed: "dispatch.not_admitted",
            GuardTimeout: "dispatch.guard_timeout",
            JobLost: "dispatch.claim_lost",
            LeaseLost: "dispatch.lease_lost",
            # 守卫内二次阻断核验（复审 中-1）被拒：和 phase-1 那条用同一个
            # rule id，用户看到的仍然只是"这个目录有未核实的副作用"。
            WorkspaceBlocked: "dispatch.workspace_blocked",
            # 守卫内的对象阻断复核（MCP §5-8）：与目录阻断分开的规则编号。
            McpObjectBlocked: "dispatch.mcp_object_blocked",
        }.get(type(exc), "dispatch.refused")
        rule_id = check_rule_id(rule_id, where="_abort_dispatch")

        with self._log.transaction():
            # 只在"还没人把它作废"时推进：撤销已经把它标成 revoked 的，
            # 不能被后到的拒绝改写成另一个结论。
            self._conn.execute(
                "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                " WHERE dispatch_id=? AND state=?",
                (
                    DISPATCH_ROW_REJECTED,
                    reason,
                    self._clock(),
                    dispatch_id,
                    DISPATCH_INTENT,
                ),
            )
            self._log.append(
                request.run_id,
                DISPATCH_REJECTED,
                {
                    "dispatch_id": dispatch_id,
                    "call_id": request.call_id,
                    "request_id": request.request_id,
                    "reason": reason,
                    "rule_id": rule_id,
                    "phase": "guard",
                },
                task_id=request.task_id,
                actor_kind="core",
            )
        # 审批/许可已经消费掉了（一次性），所以这次请求必须重新发起
        self._set_request_state(request.request_id, STATE_REJECTED)
        return ToolResult(
            "rejected",
            "none_applied",
            {"error": reason, "rule_id": rule_id, "request_id": request.request_id},
        )

    def _fail_unknown(
        self,
        request: ActionRequest,
        dispatch_id: str,
        target: _Target,
        exc: BaseException,
        rule_id: str = "",
        *,
        sent: bool = True,
    ) -> ToolResult:
        """执行中途炸了：结论只能是 unknown，并且立刻阻断后续写入。

        "没写成功"和"不知道写了没有"是两件事。后者必须挡住这个目录的后续
        写入，直到有人用 recover 核对过——否则下一个人是在一个状态不明的
        目录上继续改。
        """
        reason = f"{type(exc).__name__}: {exc}"
        output: dict[str, Any] = {"error": reason, "request_id": request.request_id}
        if rule_id:
            # 用户看到的失败原因必须是登记过的编号之一（契约里能查到的说法），
            # 不能只是一句自由文本。
            output["rule_id"] = check_rule_id(rule_id, where="_fail_unknown")
        result = ToolResult("failed", "unknown", output)
        result_event = {
            "run_id": request.run_id,
            "type": "tool.result",
            "payload": {
                "call_id": request.call_id,
                "tool_id": request.tool_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            "task_id": request.task_id,
            "actor_kind": "tool",
            "idem_key": request.call_id,
        }
        if isinstance(target, _McpTarget) and not sent:
            # 发包**之前**就确定没发出去（server 已经不可用）：不落对象阻断，但结果
            # 仍要如实落库（`unknown` 是这一支的旧口径，本工单不改结论）。这里**不**
            # 能掉进下面文件写的分支——`_McpTarget` 没有 `workspace_root`。
            with self._log.transaction():
                self._log.append(**result_event)
                self._conn.execute(
                    "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                    " WHERE dispatch_id=?",
                    (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
                )
                self._set_request_state(request.request_id, STATE_REJECTED)
            return result

        if isinstance(target, _McpTarget):
            # 到这里说明 `sent`：请求已越过外发边界，server 侧可能已经执行。
            # MCP 没有本地目录可阻断，但它有**对象**：走到 `_fail_unknown` 且
            # `sent` 说明请求已经越过外发边界（超时 / 断线 / server 内部错误），
            # server 侧可能已经执行。这里就地落一条对象阻断事件（与文件写分支"就地落
            # `recovery.blocked`、不在派发层写权威根"同一分工；跨数据根由下一次
            # `recover` 的启动补登记带上）。
            #
            # 只挡这个工具（+ 它属于的 server 由读侧 `mcp_block_keys` 一起查），
            # 不挡目录：目录阻断是恢复侧"目录状态可能被改过"的判断，这里没有
            # 任何证据支持它。
            #
            # **绝不**在这里调用 apply_event：结果未知就是"投影是否更新不知道"。
            object_event = {
                "run_id": request.run_id,
                "type": "recovery.object_blocked",
                "payload": {
                    "object": target.uri,
                    "call_ids": [request.call_id],
                    "reason": f"执行中断且效果未知：{reason}",
                },
                "task_id": request.task_id,
                "actor_kind": "system",
            }
            self._commit_mcp_unknown(request, dispatch_id, result_event, object_event, reason)
            return result

        if isinstance(
            target, (_MemoryTarget, _NetworkTarget, _SubtaskTarget)
        ):
            # 记忆没有可阻断的目录；网络 / 委派同样没有本地目录可阻断。
            # 而且**绝不**在这里调用 apply_event：
            # 结果未知就是"投影是否更新不知道"，补写一条投影会让未知变成
            # 一个编出来的确定结论。事件与派发状态仍同事务落库。
            with self._log.transaction():
                self._log.append(**result_event)
                self._conn.execute(
                    "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                    " WHERE dispatch_id=?",
                    (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
                )
                self._set_request_state(request.request_id, STATE_REJECTED)
            return result

        batch = [
            result_event,
            {
                "run_id": request.run_id,
                "type": "recovery.blocked",
                "payload": {
                    "workspace": target.workspace_root,
                    "call_ids": [request.call_id],
                    "reason": f"执行中断且效果未知：{reason}",
                },
                "task_id": request.task_id,
                "actor_kind": "system",
            },
        ]
        with self._log.transaction():
            self._log.append_many(batch)
            self._conn.execute(
                "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                " WHERE dispatch_id=?",
                (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
            )
            self._set_request_state(request.request_id, STATE_REJECTED)
        return result

    def _guarded_write(self, write, result_event: dict[str, Any]) -> None:
        """有数据根就在执行守卫内提交，抢不到守卫也**必须**提交（结果不能丢）。

        守卫把"发外边界复核 → 推进"与"阻断提交"排进同一个全序（复审 B1）。
        抢不到守卫（`GuardTimeout`，说明有别的写者正在持锁）时按最坏情况照常
        提交，并在结果里写 `block_guard="timeout"`——如实说明这一条没排在守卫里，
        而不是为了"排进守卫"把结果与阻断丢掉。
        """
        root = self._data_root
        if root is None:
            write()
            return
        execution_guard = sys.modules["tianshu.dispatcher"].execution_guard
        try:
            with execution_guard(Path(root), timeout_ms=self._guard_timeout_ms):
                write()
        except GuardTimeout:
            result_event["payload"]["output"]["block_guard"] = "timeout"
            write()

    def _commit_mcp_unknown(
        self,
        request: ActionRequest,
        dispatch_id: str,
        result_event: dict[str, Any],
        object_event: dict[str, Any],
        reason: str,
    ) -> None:
        """MCP 效果未知时的提交：结果 + 对象阻断 + 派发行终态，同一事务。

        为什么这里要抢**执行守卫**（复审第一轮 B1）：发外边界上"复核对象阻断 →
        把派发行推进到 `executing`"是在同一把守卫里完成的。如果对象阻断的提交不
        走守卫，它就能插在别人的"复核通过之后、推进之前"——那次调用照样发出去，
        "阻断已记录 → 之后不会再发"这句话就不成立。两边共用一把守卫之后顺序是
        确定的：要么阻断先提交（对方的复核看得见），要么对方的推进先完成（这次
        调用早已越过外发边界，阻断只约束它之后的新调用）。

        没有 `data_root`（库调用方）时没有撤销入口、也没有这把锁可抢：保持原行为，
        那一小段窗口仍在（文档如实写着，不假装已消掉）。

        守卫抢不到（`GuardTimeout`）时**不能**丢掉结果与阻断：照常提交，并在结果
        里写 `block_guard="timeout"`——这条阻断没有排在守卫里，读的人要知道。
        """

        def _write() -> None:
            with self._log.transaction():
                self._log.append(**result_event)
                self._log.append(**object_event)
                self._conn.execute(
                    "UPDATE dispatches SET state=?, reason=?, updated_at=?"
                    " WHERE dispatch_id=?",
                    (DISPATCH_ERROR, reason, self._clock(), dispatch_id),
                )
                self._set_request_state(request.request_id, STATE_REJECTED)

        self._guarded_write(_write, result_event)

    def _commit_result(
        self,
        request: ActionRequest,
        dispatch_id: str,
        result: ToolResult,
        target: _Target,
        intent_seq: int,
    ) -> None:
        """结果与凭据同事务落库，然后把派发记录推进到 done。

        记忆分支多一件事：`MemoryProjection.apply_event("tool.result", ...)`
        必须和 `tool.result`、凭据、派发状态在**同一个 SQLite 事务**里。投影
        更新放在事务外（或另开事务）会出现两种无法裁决的状态："事件在、投影
        不在"或"投影在、事件不在"。这里用同一个连接、同一个 `transaction()`，
        谁失败就一起回滚。
        """
        is_memory = isinstance(target, _MemoryTarget)
        is_network = isinstance(target, _NetworkTarget)
        is_subtask = isinstance(target, _SubtaskTarget)
        is_mcp = isinstance(target, _McpTarget)
        projection = self._memory_projection() if is_memory else None

        result_event = {
            "run_id": request.run_id,
            "type": "tool.result",
            "payload": {
                "call_id": request.call_id,
                "tool_id": request.tool_id,
                "status": result.status,
                "effect_state": result.effect_state,
                "output": result.output,
            },
            "task_id": request.task_id,
            "actor_kind": "tool",
            "idem_key": request.call_id,
        }
        batch: list[dict[str, Any]] = [result_event]

        if is_mcp and result.effect_state == "unknown":
            # 运行期 unknown 的**其余**出口（超时 `McpTimeout`、断线 `McpClosed`）
            # 走的是 `_commit_result`，不是 `_fail_unknown`：必须在这里补一条对象
            # 阻断，否则最常见的两种 unknown 之后同一个工具还能再发一次（复审 N1：
            # 文档声称"超时/断线就地落"，代码只覆盖了 -32603）。
            # 与结果同事务落库；跨数据根由下一次 `recover` 的启动补登记带上。
            # 提交同样拿执行守卫（与发外边界共用），抢不到时 `_guarded_write`
            # 照常提交并写 `block_guard="timeout"`。
            object_event = {
                "run_id": request.run_id,
                "type": "recovery.object_blocked",
                "payload": {
                    "object": target.uri,
                    "call_ids": [request.call_id],
                    "reason": (
                        "执行中断且效果未知："
                        f"{str(result.output.get('reason') or result.output.get('error') or '')}"
                    ),
                },
                "task_id": request.task_id,
                "actor_kind": "system",
            }
            batch.append(object_event)

            def _write_mcp_unknown() -> None:
                with self._log.transaction():
                    self._log.append_many(batch)
                    self._conn.execute(
                        "UPDATE dispatches SET state=?, updated_at=?"
                        " WHERE dispatch_id=?",
                        (DISPATCH_ERROR, self._clock(), dispatch_id),
                    )
                    self._set_request_state(request.request_id, STATE_DONE)

            self._guarded_write(_write_mcp_unknown, result_event)
            return

        if is_memory:
            if request.tool_id in (
                "memory.confirm",
                "memory.forget",
                "memory.remember",
                "memory.edit",
                "memory.promote",
                "memory.erase",
            ):
                receipt = build_memory_receipt(
                    run_id=request.run_id,
                    call_id=request.call_id,
                    tool_id=request.tool_id,
                    args_hash=request.args_hash,
                    result=result,
                    root=target.uri,
                    # promote 的凭据针对**原行**（移动的是它）；其余工具就是本行。
                    resource=str(
                        result.output.get("receipt_memory_id")
                        or result.output.get("memory_id")
                        or ""
                    ),
                    action=request.action,
                    intent_seq=intent_seq,
                    backend="memory",
                    extra_evidence=(
                        [dict(result.output["erasure"])]
                        if isinstance(result.output.get("erasure"), dict)
                        else None
                    ),
                )
            else:
                # propose 是候选草稿，不产凭据（F10 §7）。
                receipt = None
        elif is_network and request.tool_id == "browser.submit":
            receipt = build_browser_form_receipt(
                run_id=request.run_id, call_id=request.call_id, args_hash=request.args_hash,
                result=result, resource=target.uri, intent_seq=intent_seq,
            )
        elif is_network or is_subtask:
            # 网络与委派都没有本地文件副作用、也没有可撤销内容：不产凭据。
            # （凭据回答"改了什么文件"；"发了什么 / 派了什么"在 tool.result 里。）
            receipt = None
        elif is_mcp:
            # MCP 的副作用在 server 那边，但我们知道"它跑了"：凭据把
            # server/tool/请求 id 与执行摘要绑在一起（after_state=mcp:<sha256>），
            # `verify_receipts()` 对 `backend=="mcp"` 只核事件引用与结果字段，
            # **不比较磁盘**（与 exec 同一口径，见 receipt.build_mcp_receipt）。
            receipt = build_mcp_receipt(
                run_id=request.run_id,
                call_id=request.call_id,
                tool_id=request.tool_id,
                args_hash=request.args_hash,
                result=result,
                resource=target.uri,
                action=request.action,
                intent_seq=intent_seq,
            )
        elif request.action == ACTION_EXEC:
            # 命令的"写后状态"不是文件哈希：凭据记的是执行摘要，磁盘核对
            # 由 verify_receipts 明确跳过（见 receipt.build_exec_receipt）。
            receipt = build_exec_receipt(
                run_id=request.run_id,
                call_id=request.call_id,
                tool_id=request.tool_id,
                args_hash=request.args_hash,
                result=result,
                backend=self._executor.backend_name,
                root=target.workspace_root,
                resource=str(target.path),
                action=request.action,
                intent_seq=intent_seq,
            )
        else:
            receipt = build_receipt(
                run_id=request.run_id,
                call_id=request.call_id,
                tool_id=request.tool_id,
                args_hash=request.args_hash,
                result=result,
                backend=self._executor.backend_name,
                # 证据键是 (workspace 根, 相对路径)：根从**这次派发**解析出来的
                # 目标里取，而不是从某个默认目录取。多 workspace 任务里只有这里
                # 知道文件真正落在哪个目录。
                root=target.workspace_root,
                resource=str(target.path),
                action=request.action,
                intent_seq=intent_seq,
                blob_ref=self._snapshot_blob(request.run_id, request.call_id),
            )

        if receipt is not None:
            batch.append(
                {
                    "run_id": request.run_id,
                    "type": "receipt",
                    "payload": receipt.to_payload(),
                    "task_id": request.task_id,
                    "actor_kind": "core",
                    "idem_key": receipt.receipt_id,
                }
            )

        if is_memory:
            assert projection is not None
            with self._log.transaction():
                self._log.append_many(batch)
                if result.status == "succeeded" and result.effect_state == "applied":
                    # seq 用 intent_seq：契约要求投影能指回这次写入的意图；
                    # 传 None 或另取一个号都会制造第二份因果。
                    projection.apply_event(
                        "tool.result", result_event["payload"], seq=intent_seq
                    )
                self._conn.execute(
                    "UPDATE dispatches SET state=?, updated_at=? WHERE dispatch_id=?",
                    (DISPATCH_FINISHED, self._clock(), dispatch_id),
                )
                self._set_request_state(request.request_id, STATE_DONE)
            return

        # 结果、凭据与派发状态同一事务落库：杜绝"事件已写、派发行仍停在 intent"的半提交窗口。
        with self._log.transaction():
            self._log.append_many(batch)
            self._conn.execute(
                "UPDATE dispatches SET state=?, updated_at=? WHERE dispatch_id=?",
                (DISPATCH_FINISHED, self._clock(), dispatch_id),
            )
            self._set_request_state(request.request_id, STATE_DONE)

    def _snapshot_blob(self, run_id: str, call_id: str) -> str | None:
        """这次调用的写前快照 blob。

        凭据必须指向**同一条**证据：快照记录写前内容在哪里，凭据不能只有
        "我之前是什么"却指不回任何东西。
        """
        if self._snapshots is None:
            return None
        change = self._snapshots.find(run_id, call_id)
        return change.blob_ref if change is not None else None

    def _require_data_root(self) -> Path:
        if self._data_root is None:
            raise TianshuError(
                "未配置 data_root：派发与撤销必须共用同一把执行守卫"
            )
        return Path(self._data_root)
