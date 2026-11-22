"""主循环共享预算预占/结算、语义摘要与模型有界重试（内部 Mixin，由 `AgentLoop` 继承）。

所有预算预占、摘要生命周期及模型调用重试均保持与 `AgentLoop` 同一运行上下文与事件日志连接，
零契约变更。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .budget import (
    BOUND_ESTIMATE,
    BOUND_UPPER,
    BudgetCancelled,
    BudgetLedger,
    BudgetRefused,
    estimate_need,
    upper_need,
)
from .capability import declared_output_tokens, retry_delay_ms
from .context import (
    RUN_START_SEQ,
    ContextBudget,
    ContextCompileError,
    ContextOverflow,
    ContextView,
    SummarySlice,
    compile_context,
    context_payload,
)
from .crashpoint import failpoint
from .credentials import redact
from .errors import BudgetConfigError, TianshuError
from .events import EventLog
from .model import Model, ModelError, ModelResponse, identity_of
from .pricing import cost_record
from .summaries import (
    MATERIAL_BYTES,
    MaterialBlock,
    SummaryError,
    SummaryMaterialError,
    SummaryStore,
    build_material,
    prompt_for,
    render_block,
)
from .summaries import (
    PENDING as SUMMARY_PENDING,
)
from .tasks import Task, TaskStore

if TYPE_CHECKING:  # pragma: no cover
    from ._loop_bookkeeping import _Progress
    from .loop import Limits

SUMMARY_MODE_MODEL = "model"


def summary_blocks(
    messages: list[dict[str, Any]],
    seqs: list[int],
    dropped_seqs: list[int],
) -> list[Any]:
    """把"被牺牲的往返"还原成摘要材料块。**只取整段，绝不拆段**。

    为什么按"整段"取：编译侧要求摘要**整段覆盖**往返（半段会让"这份摘要代表
    哪几条"说不清）。材料如果在这里被字节上限切在一段中间，编译会直接拒绝，
    等于白花一次模型调用——所以宁可少摘一段，也不把一段拆成两半。

    `dropped_seqs` 一定是"最旧的那些往返"（牺牲顺序），所以遇到第一段没被丢的
    就可以停：后面的都还活着。
    """
    dropped = set(dropped_seqs)
    if not dropped:
        return []
    blocks: list[Any] = []
    total = 0
    index = 0
    count = len(messages)
    if len(seqs) != count:
        return []  # 归因不平行：宁可不摘，也不按猜的边界切
    while index < count and seqs[index] == RUN_START_SEQ:
        index += 1
    while index < count:
        start = index
        index += 1
        while index < count and messages[index].get("role") != "assistant":
            index += 1
        group_seqs = seqs[start:index]
        if not group_seqs or not set(group_seqs) <= dropped:
            break  # 之后的往返都还活着
        group: list[Any] = []
        for offset in range(start, index):
            message = messages[offset]
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str):
                return []
            group.append(
                MaterialBlock(
                    seq=seqs[offset],
                    role=role,
                    content=content if isinstance(content, str) else "",
                )
            )
        size = sum(len(render_block(item).encode("utf-8")) for item in group)
        if total + size > MATERIAL_BYTES:
            break  # 这一段放不下：停在往返边界上
        blocks.extend(group)
        total += size
    return blocks


def material_seqs(material: Any) -> tuple[int, ...]:
    """材料 → 它覆盖的事件 seq（`seq:<N>` 引用解析）。形状不对就抛 `TianshuError`。"""
    out: list[int] = []
    for source in material.sources:
        ref = source.ref
        if not isinstance(ref, str) or not ref.startswith("seq:"):
            raise TianshuError(f"摘要材料来源形状不合法：{ref!r}")
        try:
            out.append(int(ref[4:]))
        except ValueError as exc:
            raise TianshuError(f"摘要材料来源不是整数 seq：{ref!r}") from exc
    return tuple(out)


def _message_chars(messages: list[dict[str, Any]]) -> int:
    """上下文里实际要发给模型的字符数（预占估算的输入）。

    只数 `content`，不数 JSON 结构本身——结构化字段的 token 开销远小于正文，
    而预占还额外加了 25% 余量与输出预留。这里刻意保守：宁可多估一点。
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"])
    return total


def _message_bytes(messages: list[dict[str, Any]]) -> int:
    """实际请求体的 UTF-8 字节数——硬上限模式下**输入**的那一半上界。

    与 `_message_chars()` 的区别不只是单位：这里序列化成真正要发出去的那份
    JSON（`ensure_ascii=True`，非 ASCII 展开成 `\\uXXXX`），所以结构字段、角色
    标记、转义后膨胀的部分都算进去了。字节级 BPE 下 `tokens ≤ bytes`，于是它是
    一个方向正确的上界；CJK 因此比"4 字符≈1 token"的估算大得多，那正是应该的。
    """
    return len(json.dumps(messages, ensure_ascii=True).encode("utf-8"))



class _LoopSummaryMixin:
    """共享预算账本预占/结算、F5/§7.1 语义摘要与模型有界重试，供 `AgentLoop` 组合继承。"""

    _tasks: TaskStore
    _model: Model | None
    _on_model_delta: (
        Callable[[str, str, int, dict[str, Any] | None, bool], None] | None
    )
    _budget: BudgetLedger | None
    _summaries: SummaryStore | None
    _limits: Limits
    _sleep: Callable[[float], None]
    _last_usage: dict[str, int]
    _log: EventLog

    # ---------------------------------------------------------------- 预算账本

    def _budget_account_id(self, run_id: str) -> str:
        return f"run:{run_id}"

    def _ensure_budget_account(self, run_id: str) -> None:
        """建本 Run 的预算账户（幂等）。**记账默认打开**。

        没有 `budget_limits` 时建的是"不设闸的账户"：预占与结算照记，但任何维度
        都不拒发。这不是"账本关着"——关着的话 §5.6 第 4 / 7 条（尝试过但全失败
        不许显示没有调用、恢复先核对未决预占）在默认路径上没有任何事实可核。
        """
        from .goal_v2 import ensure_phase_budget
        if ensure_phase_budget(
            self._log, self._budget, run_id, self._limits.budget_limits,
            self._limits.budget_bound, runtime=self._leases.identity.instance_id,
        ):
            return
        if self._budget is None:
            return
        self._budget.create_account(
            self._budget_account_id(run_id),
            limits=dict(self._limits.budget_limits or {}),
            run_id=run_id,
            label="run",
            bound=self._limits.budget_bound,
        )

    def _reserve_attempt(self, run_id: str, request_id: str, attempt: int, view):
        with self._log.transaction():
            return self._reserve_attempt_locked(run_id, request_id, attempt, view)

    def _reserve_attempt_locked(self, run_id: str, request_id: str, attempt: int, view):
        """发送前预占。返回预占凭据；没有账本时返回 None。

        `estimate` 与 `upper` 两种口径的差别不是"保守程度"，是**性质**：前者是
        估算（可以偏、偏了照实记），后者是两个上界相加（偏了就是保证失效，停机）。
        """
        if self._budget is None:
            return None
        self._ensure_budget_account(run_id)
        if self._limits.budget_bound == BOUND_UPPER:
            output_bound = declared_output_tokens(self._model)
            if output_bound is None:
                model_cls = getattr(
                    self._model, "adapter_name", lambda: type(self._model).__name__
                )()
                raise BudgetConfigError(
                    "硬上限模式（budget_bound='upper'）需要 adapter 声明输出上界，"
                    f"而 {model_cls} 没有声明 `output_tokens`；"
                    "拿默认输出预留冒充上界等于把估算写成保证，所以拒绝进入该模式"
                )
            need = upper_need(
                input_bytes=_message_bytes(view.messages),
                output_bound=output_bound,
                requests=1,
            )
        else:
            need = estimate_need(
                input_chars=_message_chars(view.messages),
                output_reserve=self._limits.output_reserve_tokens,
                requests=1,
            )
        need, unpriced = self._with_money_need(need)
        return self._budget.reserve(
            self._budget_account_id(run_id),
            attempt_id=f"{request_id}#{attempt}",
            run_id=run_id,
            call_id=request_id,
            need=need,
            unpriced=unpriced,
        )

    def _with_money_need(self, need: dict[str, int]) -> tuple[dict[str, int], bool]:
        """金额与 token 同次预占；缺价不发明名义价格。

        现有价格表只承诺标准档估价，不是所有计费档位的可验证上界。
        upper 口径因此不生成金额上界；若账户链设金额限额，账本会拒发。
        无金额限额时零金额预留只表示没有预留额度；缺价的费用仍为 None，
        micros 维度未决（partial）；仅缺金额上界但有实际价格时正常结算。
        """
        micros = None
        if self._limits.budget_bound == BOUND_ESTIMATE:
            output = max(0, self._limits.output_reserve_tokens)
            identity = identity_of(self._model)
            cost = cost_record(
                provider=identity.vendor if identity is not None else None,
                model=identity.model if identity is not None else None,
                usage={"input_tokens": max(0, need["tokens"] - output),
                       "output_tokens": output},
                adapter=identity.adapter if identity is not None else "",
            )
            micros = cost.cost
        return {**need, "micros": 0 if micros is None else micros}, micros is None

    def _settle_attempt(self, reservation, resp: ModelResponse | None) -> None:
        """按 provider 的最终 usage 结算；没有的维度**保留占用**，不记零。

        - 请求发出去过 → `requests` 维度已知（1 次）；
        - provider 给全输入和输出 usage → `tokens` 维度已知；
        - 查得到价格 → `micros` 维度已知；
        其余维度留在预占里，由 `reconcile()` 报告（PRD §5.6 第 4 / 7 条）。
        """
        if self._budget is None or reservation is None:
            return
        usage: dict[str, int] = {"requests": 1}
        known = {"requests"}
        if resp is not None:
            inp = resp.usage.get("input_tokens")
            outp = resp.usage.get("output_tokens")
            if inp is not None and outp is not None:
                usage["tokens"] = inp + outp
                known.add("tokens")
            identity = identity_of(self._model)
            cost = cost_record(
                provider=identity.vendor if identity is not None else None,
                model=identity.model if identity is not None else None,
                usage=resp.usage,
                adapter=identity.adapter if identity is not None else "",
            )
            if cost.cost is not None:
                usage["micros"] = cost.cost
                known.add("micros")
        self._budget.settle(
            reservation.reservation_id,
            usage=usage,
            known_dimensions=known & set(reservation.need),
        )

    # ---------------------------------------------------------------- 语义摘要

    def _summary_model_id(self) -> str:
        """摘要记录里的模型身份：`adapter:vendor:model`（缺项留空）。"""
        identity = identity_of(self._model)
        if identity is None:
            return getattr(
                self._model, "adapter_name", lambda: type(self._model).__name__
            )()
        return ":".join(
            part
            for part in (identity.adapter, identity.vendor or "", identity.model or "")
        )

    def _maybe_summarize(
        self,
        run_id: str,
        task: Task,
        progress: _Progress,
        view: ContextView,
        budget: ContextBudget,
    ) -> ContextView:
        """PRD §7.1：把"本来就要丢的历史"换成一份**已持久化**的模型摘要。

        顺序写死（`docs/context-summaries.md` §4）：

            材料 → 记录(pending) → 账本预占 → 模型调用 → 正文提交 → 再编译

        任何一步不成立都**回落确定性裁剪**：摘要是优化，不是任务的必要条件。
        默认 `summary_mode="off"`，这里直接返回——默认路径一行不变。
        """
        if self._limits.summary_mode != SUMMARY_MODE_MODEL:
            return view
        # 没有记录或没有账本就不摘：不发"没人记得、没人记账"的模型调用。
        if self._summaries is None or self._budget is None or self._model is None:
            return view
        if not view.dropped_seqs:
            return view
        messages = progress.messages
        seqs = self._message_source_seqs(run_id, progress)
        dropped = set(view.dropped_seqs)
        try:
            # 已经落库、且覆盖的 seq 现在**全都**被牺牲了的摘要：可以拿来复用。
            # 允许"子集复用"是必要的——调用上限一到，只用"材料完全一致"就会整段
            # 回落确定性裁剪，最先丢的硬约束又没了。
            reusable = [
                (record, covered)
                for record, covered in self._summaries.ready_with_sources(run_id)
                if set(covered) <= dropped
            ]
            calls = self._summaries.count_for_run(run_id)
        except TianshuError:
            return view
        if calls >= max(0, int(self._limits.summary_max_calls)):
            # 调用上限到了：不再生成新摘要，但已有的摘要还能盖住"当时就丢掉的
            # 那一段"，用它。
            return self._reuse_or_fallback(view, reusable, messages, seqs, budget)

        try:
            blocks = summary_blocks(messages, seqs, view.dropped_seqs)
            if not blocks:
                return self._reuse_or_fallback(view, reusable, messages, seqs, budget)
            material = build_material(
                blocks,
                scope=f"task:{task.task_id}",
                task_id=task.task_id,
                run_id=run_id,
            )
            latest = self._summaries.latest(material.material_id)
        except SummaryMaterialError:
            return self._reuse_or_fallback(view, reusable, messages, seqs, budget)
        except TianshuError:
            return view

        if latest is not None and latest.ready:
            # 材料完全一致：直接复用（0 次调用）。
            return self._recompile_with_summary(
                view, latest, tuple(material_seqs(material)), messages, seqs, budget
            )
        if latest is not None and latest.status == SUMMARY_PENDING:
            # 崩溃遗留：这次调用发生过没有，只有账本知道；重试就等于偷偷再来
            # 一次（PRD §7.1 第 4 条禁止的正是这个）。标成"结果未知"。
            self._fail_summary(latest, "中断：上一次摘要调用结果未知（不重试）")
            return self._reuse_or_fallback(view, reusable, messages, seqs, budget)

        calls = self._summaries.count_for_run(run_id)
        if calls >= max(0, int(self._limits.summary_max_calls)):
            # 调用上限到了：已有的摘要还能盖住"当时就丢掉的那一段"，用它。
            return self._reuse_or_fallback(view, reusable, messages, seqs, budget)

        record = self._generate_summary(run_id, task, progress, material)
        if record is not None:
            recompiled = self._recompile_with_summary(
                view, record, tuple(material_seqs(material)), messages, seqs, budget
            )
            if recompiled.summaries:
                return recompiled
        return self._reuse_or_fallback(view, reusable, messages, seqs, budget)

    def _reuse_or_fallback(
        self,
        view: ContextView,
        reusable: list[Any],
        messages: list[dict[str, Any]],
        seqs: list[int],
        budget: ContextBudget,
    ) -> ContextView:
        """用已有的（覆盖最大的）ready 摘要再编译；一条都没有就原样返回。"""
        for record, covered in reusable:
            recompiled = self._recompile_with_summary(
                view, record, covered, messages, seqs, budget
            )
            if recompiled.summaries:
                return recompiled
        return view

    def _generate_summary(
        self,
        run_id: str,
        task: Task,
        progress: _Progress,
        material: Any,
    ) -> Any:
        """走完一次摘要调用。失败返回 None（调用方回落），从不抛给 Run。"""
        assert self._summaries is not None and self._model is not None
        attempt_id = f"summary#{material.material_id}#1"
        try:
            record = self._summaries.begin(
                material,
                model=self._summary_model_id(),
                attempt_id=attempt_id,
                run_id=run_id,
                task_id=task.task_id,
            )
        except TianshuError:
            return None
        try:
            reservation = self._reserve_summary(material, attempt_id, run_id)
        except (BudgetRefused, BudgetCancelled, BudgetConfigError) as exc:
            from .goal_v2 import record_admission_refused
            record_admission_refused(self._log, run_id, exc, source="summary")
            self._fail_summary(record, f"预算拒绝：{exc}")
            return None
        except TianshuError as exc:
            self._fail_summary(record, f"预算配置错误：{exc}")
            return None

        # 一次摘要调用就是一次真实模型调用：attempts 与 tokens 都要如实加，
        # 否则"这个 Run 到底打出去几次模型"会少算（PRD §5.6 第 4 条）。
        progress.model_attempts += 1
        try:
            resp = self._model.complete(
                [{"role": "user", "content": prompt_for(material)}], []
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败一律回落
            progress.unknown_usage_calls += 1
            progress.unknown_cost_calls += 1
            self._settle_summary(reservation, None)
            self._fail_summary(record, f"模型调用失败：{exc!r}")
            return None
        # 崩溃注入点：**调用已发出、正文还没落库**——恢复时只能看到 pending。
        failpoint("summary-after-call")
        self._absorb_summary_usage(progress, resp)
        self._settle_summary(reservation, resp)
        cost = self._summary_cost(resp)
        try:
            return self._summaries.complete(
                record,
                resp.content,
                input_tokens=resp.usage.get("input_tokens"),
                output_tokens=resp.usage.get("output_tokens"),
                micros=cost.cost,
            )
        except SummaryError as exc:
            self._fail_summary(record, f"摘要正文不合格：{exc}")
            return None

    def _reserve_summary(self, material: Any, attempt_id: str, run_id: str) -> Any:
        with self._log.transaction():
            return self._reserve_summary_locked(material, attempt_id, run_id)

    def _reserve_summary_locked(self, material: Any, attempt_id: str, run_id: str) -> Any:
        """摘要调用的账本预占：口径与主调用一致（estimate / upper 两种）。"""
        if self._budget is None:
            return None
        self._ensure_budget_account(run_id)
        prompt = prompt_for(material)
        if self._limits.budget_bound == BOUND_UPPER:
            output_bound = declared_output_tokens(self._model)
            if output_bound is None:
                raise BudgetConfigError(
                    "硬上限模式（budget_bound='upper'）需要 adapter 声明输出上界；"
                    "摘要调用拿不到上界就不发（回落确定性裁剪），"
                    "不拿默认预留冒充上界"
                )
            need = upper_need(
                input_bytes=len(prompt.encode("utf-8")),
                output_bound=output_bound,
                requests=1,
            )
        else:
            need = estimate_need(
                input_chars=len(prompt),
                output_reserve=self._limits.output_reserve_tokens,
                requests=1,
            )
        need, unpriced = self._with_money_need(need)
        return self._budget.reserve(
            self._budget_account_id(run_id),
            attempt_id=attempt_id,
            run_id=run_id,
            call_id=attempt_id,
            need=need,
            unpriced=unpriced,
        )

    def _summary_cost(self, resp: ModelResponse) -> Any:
        identity = identity_of(self._model)
        return cost_record(
            provider=identity.vendor if identity is not None else None,
            model=identity.model if identity is not None else None,
            usage=resp.usage,
            adapter=identity.adapter if identity is not None else "",
        )

    def _absorb_summary_usage(self, progress: _Progress, resp: ModelResponse) -> None:
        """把摘要调用的用量记进**同一个** Run 账本（未知不记零）。"""
        inp = resp.usage.get("input_tokens")
        outp = resp.usage.get("output_tokens")
        if inp is None or outp is None:
            progress.unknown_usage_calls += 1
        # 已知部分照实累计为下界；缺项仍由未知计数和账本预占表达。
        progress.tokens += (inp or 0) + (outp or 0)
        cost = self._summary_cost(resp)
        if cost.cost is None:
            progress.unknown_cost_calls += 1
        else:
            progress.cost_total_micros += cost.cost

    def _settle_summary(self, reservation: Any, resp: ModelResponse | None) -> None:
        """摘要调用的结算：口径与主调用同一份（未知不记零，保留占用）。"""
        if self._budget is None or reservation is None:
            return
        usage: dict[str, int] = {"requests": 1}
        known = {"requests"}
        if resp is not None:
            inp = resp.usage.get("input_tokens")
            outp = resp.usage.get("output_tokens")
            if inp is not None and outp is not None:
                usage["tokens"] = inp + outp
                known.add("tokens")
            cost = self._summary_cost(resp)
            if cost.cost is not None:
                usage["micros"] = cost.cost
                known.add("micros")
        self._budget.settle(
            reservation.reservation_id,
            usage=usage,
            known_dimensions=known & set(reservation.need),
        )

    def _fail_summary(self, record: Any, reason: str) -> None:
        """把一次摘要尝试结案成 failed。**结案失败不抛**：摘要不许弄死 Run。"""
        assert self._summaries is not None
        try:
            self._summaries.fail(record, reason)
        except TianshuError:
            return

    def _recompile_with_summary(
        self,
        view: ContextView,
        record: Any,
        source_seqs: tuple[int, ...],
        messages: list[dict[str, Any]],
        seqs: list[int],
        budget: ContextBudget,
    ) -> ContextView:
        """用**已提交**的摘要再编译一次。放不下/编译不了 ⇒ 保留确定性结果。"""
        if not record.ready or record.text is None or record.text_digest is None:
            return view
        item = SummarySlice(
            summary_id=record.summary_id,
            version=record.version,
            text=record.text,
            text_digest=record.text_digest,
            source_seqs=source_seqs,
            summary_version=record.summary_version,
        )
        try:
            again = compile_context(messages, seqs, budget, summaries=(item,))
        except (ContextCompileError, ContextOverflow, TianshuError):
            return view
        return again if again.summaries else view

    def _complete_with_retry(
        self,
        run_id: str,
        progress: _Progress,
        request_id: str,
        view: ContextView,
        budget: ContextBudget,
    ) -> tuple[ModelResponse | None, str]:
        """有界重试（PRD §7 F9a：默认 3 次 attempt）。

        - **每次 attempt 都写一条 `model.intent`**，`attempt` 是真实序号。
          三次尝试全写成 1 的日志等于没有审计：事后看不出"同一件事试了三遍"。
        - 只有 `ModelError.retryable` 为真才重试；不可重试的错误一次就停。
        - 每次重试前重新核对 token 预算：重试不能成为绕过 `max_total_tokens`
          的后门。
        - 重试**只包住 `complete()`**：模型调用没有副作用；派发与工具执行另有
          幂等键与恢复流程，套用这里的重试会让一次副作用执行两遍。
        """
        attempts = max(1, int(self._limits.max_model_attempts))
        last: ModelError | None = None

        # 崩溃不清空已发送身份，也不复用 reserve 后 / intent 前遗留的预占。
        first = 1 + max((
            ev.payload.get("attempt", 0)
            for ev in self._log.read_run(run_id)
            if ev.type == "model.intent" and ev.payload.get("request_id") == request_id
        ), default=0)
        if self._budget is not None:
            while self._budget.reservation_for(
                self._budget_account_id(run_id), f"{request_id}#{first}"
            ) is not None:
                first += 1
        for attempt in range(first, attempts + 1):
            if attempt > 1:
                # 预算守卫：重试不能成为绕过 `max_total_tokens` 的后门。
                #
                # 只看账本是不够的：**失败的调用不产生用量**，所以"再试一次会不会
                # 超"在账本上永远看不出来。这里用上一次成功调用的用量做投影——
                # 同一轮请求的规模不会差太多，宁可少试一次，也不让重试把预算
                # 悄悄花超。投影是估计，但方向是保守的（见 PR 说明）。
                projected = progress.tokens + self._last_usage.get(run_id, 0)
                if projected > self._limits.max_total_tokens:
                    reason = (
                        f"token 预算不足，不再重试：已用 {progress.tokens}，"
                        f"预计再试一次约 +{self._last_usage.get(run_id, 0)}，"
                        f"上限 {self._limits.max_total_tokens}；"
                        f"已尝试 {attempt - 1} 次，最后一次失败原因：{last!r}"
                    )
                    self._log.append(
                        run_id,
                        "model.error",
                        {
                            "request_id": request_id,
                            "attempt": attempt - 1,
                            "retryable": True,
                            "error": reason,
                        },
                        actor_kind="core",
                    )
                    return None, reason
                delay_ms = retry_delay_ms(attempt - 1, self._limits.retry_backoff_ms)
                if delay_ms > 0:
                    self._sleep(delay_ms / 1000.0)

            # PRD §5.6 第 1 条：请求发送前在共享账本里**原子预占**。
            # 预占失败就不发请求——这里的失败不是"重试一下就好"，重试就是绕过闸门。
            try:
                reservation = self._reserve_attempt(
                    run_id, request_id, attempt, view
                )
            except BudgetConfigError as exc:
                from .goal_v2 import record_admission_refused
                record_admission_refused(
                    self._log, run_id, exc, source="model", request_id=request_id)
                # 配置立不住 ≠ 这次额度不够：不发请求，也不重试（重试还是同一个配置）。
                reason = f"预算配置拒绝，不再调用模型：{exc}"
                self._log.append(
                    run_id,
                    "model.error",
                    {
                        "request_id": request_id,
                        "attempt": attempt,
                        "retryable": False,
                        "error": reason,
                    },
                    actor_kind="core",
                )
                return None, reason
            except (BudgetRefused, BudgetCancelled) as exc:
                from .goal_v2 import record_admission_refused
                record_admission_refused(
                    self._log, run_id, exc, source="model", request_id=request_id)
                reason = f"预算账本拒绝，不再调用模型：{exc}"
                self._log.append(
                    run_id,
                    "model.error",
                    {
                        "request_id": request_id,
                        "attempt": attempt,
                        "retryable": False,
                        "error": reason,
                    },
                    actor_kind="core",
                )
                return None, reason

            self._log.append(
                run_id,
                "model.intent",
                {
                    "request_id": request_id,
                    "attempt": attempt,
                    # messages_hash 记的是**发出去的那份**的摘要（F5 起 = 编译后的
                    # digest）。预算内恒等时它与"全量历史的摘要"完全相同。
                    "messages_hash": view.digest,
                    "context": context_payload(
                        budget, view, memories=self._memory_selections(view)
                    ),
                },
                actor_kind="core",
                # idem 键带上 attempt：每次尝试的 payload 不同，同键异参在这里
                # 不成立（PRD 附录 B 第 5 条）。
                idem_key=f"{request_id}#{attempt}",
            )

            # 尝试次数在**真正发请求的这一刻**加：预算守卫提前返回、准入拒绝、
            # 上下文编译失败都不算"打过一次"。加在这里，超时/传输失败/重试
            # 都会被如实计入（issue #58：失败也是一种发生过的事实）。
            progress.model_attempts += 1
            run_row = self._tasks.get_run(run_id)
            task_id_for_stream = run_row.task_id if run_row is not None else ""
            turn_idx = progress.turns + 1
            streamer = (
                getattr(self._model, "stream_complete", None)
                if self._on_model_delta is not None
                else None
            )

            def _emit_delta(
                delta_obj: Any,
                _tid: str = task_id_for_stream,
                _tidx: int = turn_idx,
            ) -> None:
                if self._on_model_delta is None:
                    return
                payload = (
                    delta_obj.to_payload()
                    if hasattr(delta_obj, "to_payload")
                    else dict(delta_obj)
                )
                self._on_model_delta(run_id, _tid, _tidx, payload, False)

            def _emit_done(
                _tid: str = task_id_for_stream,
                _tidx: int = turn_idx,
            ) -> None:
                if self._on_model_delta is not None:
                    self._on_model_delta(run_id, _tid, _tidx, None, True)

            try:
                manifest = self._run_tool_manifest(run_id)
                if callable(streamer):
                    resp = streamer(view.messages, manifest, _emit_delta)
                else:
                    resp = self._model.complete(view.messages, manifest)
            except ModelError as exc:
                _emit_done()
                # 请求已经发出去了：`requests` 维度已知，token / 金额未知。
                # 未知维度**不释放占用**——"可能已经计费"不能按零结算。
                self._settle_attempt(reservation, None)
                last = exc
                self._log.append(
                    run_id,
                    "model.error",
                    {
                        "request_id": request_id,
                        "attempt": attempt,
                        "retryable": exc.retryable,
                        "error": repr(exc),
                    },
                    actor_kind="core",
                )
                if not exc.retryable:
                    return None, (
                        f"模型调用异常（attempt {attempt}/{attempts}，不可重试）：{exc}"
                    )
            except Exception as exc:  # noqa: BLE001 - 非 ModelError 不重试
                _emit_done()
                self._settle_attempt(reservation, None)
                safe_repr = redact(repr(exc))
                safe_msg = redact(str(exc))
                self._log.append(
                    run_id,
                    "model.error",
                    {
                        "request_id": request_id,
                        "attempt": attempt,
                        "error": safe_repr,
                    },
                    actor_kind="core",
                )
                return None, f"模型调用异常（attempt {attempt}/{attempts}）：{safe_msg}"
            else:
                _emit_done()
                # 成功拿到回执：按 provider 的最终 usage 结算（PRD §5.7 第二阶段）。
                self._settle_attempt(reservation, resp)
                return resp, ""

        detail = repr(last) if last is not None else "（没有可用的错误信息）"
        return None, f"模型调用 {attempts} 次都失败：{detail}"
