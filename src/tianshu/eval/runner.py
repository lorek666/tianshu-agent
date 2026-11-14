"""评测运行器：每条 case、每次运行都从干净目录开始。

这里有三件事必须做对，否则分数没有意义：

1. **干净 fixture**：每次运行重新铺设工作区、新建事件库、新建快照根。
   复用目录会把上一条 case 的状态带进下一条，失败归因就永远查不清。
2. **真实路径**：跑的是 `loop.AgentLoop` 本身，不是"评测专用简化版"。
   崩溃 case 走子进程硬退 + `recover_run`，和真实断电后同一条恢复路径。
3. **人审替身**：审批边界 case 停住之后由本地人审替身做决定（批准/否决），
   `HumanIdentity.local_cli()` 是唯一允许构造的身份——评测器不能自带
   "模型也能批准"的后门。

坐标约定（子进程与父进程必须一致）：

    <case>/run<N>/ws/          workspace（fixture）
    <case>/run<N>/outside/     工作区外的靶子文件
    <case>/run<N>/events.db    事件日志（blob 在 events.db 同级的 blobs/）
    <case>/run<N>/data/        authority 数据根（快照清单在 data/artifacts/）
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..capability import CapabilityProfile
from ..contents import parse_ref
from ..errors import TianshuError
from ..events import EventLog
from ..loop import AgentLoop, Limits, build_stack
from ..model import Model, ModelError, ModelIdentity, ScriptedModel, identity_of
from ..permissions import HumanIdentity
from ..recovery import RecoveryReport, recover_run
from ..replay import ReplayReport, replay
from ..sandbox import current_backend
from ..tasks import RUN_WAITING_APPROVAL, SUBTASK_UNFINISHED_STATES
from ..worker import Worker
from .cases import CASE_BY_ID, SUITE_VERSION, Case
from .checks import FollowupObservation, Observation
from .webfixture import WebFixture, closed_origin

#: 子进程入口；父进程按同一目录约定拼路径。
CHILD_MODULE = "tianshu.eval.crash_child"
CHILD_TIMEOUT_S = 120
#: 审批往返最多几次。一个正常 case 最多停一次；留 6 次是为了让"批准后又停"
#: 这类 bug 表现为失败，而不是把评测器卡死。
MAX_APPROVAL_ROUNDS = 6
#: G2 委派 D3：父 Run 每 park 一次算一轮。上限防的是"父任务反复派子任务、
#: 永远收不了尾"——超过就把它写成评测失败，不是静默重试到天荒地老。
MAX_DELEGATION_ROUNDS = 8

ModelFactory = Callable[[Case], Model]


class _FailingModel:
    """子 Run 的"模型直接失败"替身（评测用，不是产品运行时依赖）。

    为什么需要它：D3-c3 要证明"子任务失败时父任务不编造"。让子 Run 模型抛
    不可重试的 `ModelError`，走的是真实的失败收尾路径；不用改产品代码造一个
    假终态。能力声明必须给全，否则子 Run 会在准入阶段就失败，测到的就不是
    "执行中失败"那条路径了。
    """

    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="scripted",
            model="failing-child",
            supports_tool_calling=True,
            declared_by="eval._FailingModel（子任务失败替身）",
        )

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="scripted", vendor=None, model="")

    def complete(self, messages, tools):  # noqa: ARG002
        self.calls += 1
        raise ModelError(self.message, kind="protocol", retryable=False)


def require_local_backend() -> None:
    """评测只能在 local 后端上跑；别的后端一律**开跑前拒绝**。

    为什么不是"跑跑看"：这套评测有三个硬前提，它们都建立在"宿主盘上的一份
    状态，跨进程可见"之上：

    1. fixture 由 `_prepare` 直接落盘（`Path.write_text`），判据（`file.equals`
       等）也从盘上读——都不经过后端；
    2. 崩溃 case 的子进程在**另一个进程**里读同一份 fixture、写同一个工作区；
    3. `recover_run` 要在父进程里看到子进程"崩溃前留下的"副作用。

    memfs 是进程内单例，状态随进程消失（见 `tianshu/sandbox.py` 的说明），
    所以上面三条一条都不成立。实测（2026-11-10，macOS arm64，
    `TIANSHU_SANDBOX_BACKEND=memfs` 跑整套）：得分 1/20、唯一的绿是唯一
    不写文件的 N6，**verdict=unsafe**——那不是"系统不安全"，是后端与评测器
    不匹配的假分数。假分数的危害比没有分数大：它会被人引用。

    所以按"判不了就拒绝"的既有口径 fail closed：宁可让调用方看到一条解释，
    也不要产出会误导人的报告。
    """
    backend = current_backend()
    if backend.name != "local_fs":
        raise TianshuError(
            f"评测只能在 local_fs 后端上跑，当前是 {backend.name!r}。"
            "理由：fixture 与判据直接读写宿主盘，崩溃 case 的子进程还要跨进程"
            "看到同一份状态；进程内虚拟文件系统（memfs）三者都不满足，"
            "跑出来的分数是后端不匹配的假分数（实测 1/20 且 verdict=unsafe），"
            "不是系统结论。要评测请去掉 TIANSHU_SANDBOX_BACKEND 或显式设成 "
            "local_fs。"
        )


@dataclass
class SuiteRun:
    """一次整套评测的原始材料（报告构建在 report.py）。"""

    observations: list[Observation]
    workdir: Path
    elapsed_ms: int
    identity: dict[str, str | None]
    suite_version: str
    repeat: int


def case_dir(workdir: Path, case: Case, run_index: int) -> Path:
    return Path(workdir) / case.case_id / f"run{run_index}"


def _prepare(case_dir: Path, case: Case) -> tuple[Path, Path]:
    ws = case_dir / "ws"
    outside = case_dir / "outside"
    for path in (ws, outside, case_dir / "data", case_dir / "blobs"):
        path.mkdir(parents=True, exist_ok=True)
    _write_files(ws, case.fixture)
    _write_files(outside, case.outside)
    return ws, outside


def _write_files(root: Path, files: dict[str, str]) -> None:
    """按字节写 fixture。

    不能用 `write_text`：Windows 上它把 `\n` 翻译成 `\r\n`，fixture 的字节
    就不再等于 `case.fixture` 里声明的内容，"干净 fixture" 与哈希判据全歪。
    """
    for rel, content in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


def _read_tree(root: Path) -> dict[str, bytes]:
    """递归读成一个 `{相对路径: 字节}` 快照。

    用 bytes 而不是解码后的 str：判据要能比对二进制等价，解码会把非法 UTF-8
    悄悄替换掉，两个不同的文件看起来就一样了。
    """
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            out[rel] = path.read_bytes()
    return out


def _latest_run_id(log: EventLog) -> str | None:
    row = log.conn.execute(
        "SELECT run_id FROM events WHERE type='run.start' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None


def _identity_of(factory: ModelFactory, case: Case) -> dict[str, str | None]:
    """取模型身份。取不到就写 unknown，绝不用 adapter 名冒充厂商。

    优先用**非崩溃 case** 取身份：崩溃 case 恒用离线脚本，拿它去问真实模型
    工厂会得到一个 scripted 身份——报告头会把"被评测的模型"写错。
    """
    try:
        model = factory(case)
    except Exception:  # noqa: BLE001 - 身份只影响报告头，不该拖垮整次评测
        return {"adapter": "unknown", "vendor": None, "model": ""}
    identity = identity_of(model)
    if identity is None:
        return {"adapter": "unknown", "vendor": None, "model": ""}
    return {
        "adapter": identity.adapter,
        "vendor": identity.vendor,
        "model": identity.model,
    }


# ---------------------------------------------------------------- 联网夹具（PRD §10.5）

#: 联网研究任务里 `{WEB_BASE}` 的替换值形状：`127.0.0.1:<port>`。
WEB_BASE_TOKEN = "{WEB_BASE}"


def _start_web(case: Case) -> tuple[WebFixture | None, object | None, str]:
    """按 case 声明启动本机来源（或准备一个"连不上"的地址）。

    返回 `(fixture, 保留的 socket, base)`：
    - `web_routes` 非空 → 起 loopback 服务器，base 是它的地址；
    - `web_closed` → 一个已 bind 未 listen 的端口（连接必被拒），base 是它；
    - 都没有 → base 为空串（不需要替换）。
    """
    if case.web_routes:
        fixture = WebFixture()
        for path, body in case.web_routes.items():
            fixture.route(path, body=body)
        fixture.start()
        return fixture, None, fixture.base
    if case.web_closed:
        base, sock = closed_origin()
        return None, sock, base
    return None, None, ""


def _materialize_case(case: Case, base: str) -> Case:
    """把 `{WEB_BASE}` 替换成真实地址（任务文本 / 白名单 / 脚本）。

    地址是内核分配的，事先不知道；所以脚本只能"起来之后再生成"。替换后的 case
    只在本进程内用于运行与判据，`case_id` / `version` 不变（分母身份由
    `fixture_digest` 里的声明决定，不随端口号变）。
    """
    if (
        not base
        and case.script_factory is None
        and WEB_BASE_TOKEN not in case.task
        and not any(WEB_BASE_TOKEN in item for item in case.outbound_allow)
    ):
        return case
    script = case.script
    if case.script_factory is not None:
        script = tuple(case.script_factory(base))
    allow = tuple(item.replace(WEB_BASE_TOKEN, base) for item in case.outbound_allow)
    return replace(
        case,
        task=case.task.replace(WEB_BASE_TOKEN, base),
        script=script,
        outbound_allow=allow,
    )


def _collect_offline_sources(
    stack: object, case: Case, events: list, log: EventLog
) -> list[dict[str, Any]]:
    """关掉来源服务器**之后**，从内容引用读回每一次成功抓取的字节。

    读的时候会重算 sha256（`ContentStore.read` 的既有保护）：引用与内容对不上
    就直接在这里炸，判据拿不到"看起来还行"的假来源。`head` 只留前 1 KiB——
    判据要核对的是"读得回来、内容对得上"，不是把整页塞进报告。
    """
    task_id = ""
    for event in events:
        if event.type == "run.start":
            task_id = str(event.payload.get("task_id") or "")
            break
    sources: list[dict[str, Any]] = []
    for event in events:
        if event.type != "tool.result":
            continue
        payload = event.payload
        if payload.get("tool_id") != "network.fetch":
            continue
        if payload.get("status") != "succeeded":
            continue
        output = payload.get("output") or {}
        ref = output.get("ref")
        url = str(output.get("url") or "")
        if not isinstance(ref, str) or not ref.startswith("content://sha256:"):
            # 成功结果却没有引用 = 记录不完整。不在这里判定（replay 会报错），
            # 但判据需要看到"这条来源不可核验"，所以放进列表且摘要留空。
            sources.append({"ref": ref, "digest": "", "bytes": 0, "head": "", "url": url})
            continue
        digest = parse_ref(ref)
        page = stack.contents.read(ref, task_id=task_id, length=1024)
        sources.append(
            {
                "ref": ref,
                "digest": digest,
                "bytes": page.total_size,
                "head": page.data,
                "url": url,
            }
        )
    return sources


# ---------------------------------------------------------------- 委派（G2 D3）


def _unfinished_subtasks(stack: Any, parent_run_id: str) -> list[Any]:
    return [
        row
        for row in stack.tasks.list_subtasks(parent_run_id)
        if row.state in SUBTASK_UNFINISHED_STATES
    ]


def _child_model(case: Case, subtask_id: str) -> Model:
    """按 `subtask_id` 取子 Run 的模型；没声明就**拒绝**，不拿父脚本冒充。"""
    plan = case.subtask_scripts.get(subtask_id)
    if plan is None:
        raise TianshuError(
            f"子任务 {subtask_id!r} 没有声明评测脚本（case.subtask_scripts 缺这个 "
            "slug）；评测器不会用父任务脚本子 Run，也不会猜一个空脚本"
        )
    if plan.fail_with is not None:
        return _FailingModel(plan.fail_with)
    return ScriptedModel(list(plan.script))


def _drive_child_runs(
    stack: Any,
    case: Case,
    parent_run_id: str,
    *,
    limits: Limits,
) -> str | None:
    """把父 Run 已派出的子任务驱动到终态。返回 None = 成功，否则是失败原因。

    **只驱动子 Run**：Worker 的 `engine_factory` 按 claim 的 Run 查父链，认领到
    非子 Run（例如父作业被意外唤醒）会直接报错，而不是拿占位模型跑父任务。
    调用前先确认每个未完成子任务的作业都是 `pending`，因此 `max_jobs` 恰好等于
    子任务数时，Worker 不会越过最后一个子任务去认领父作业。

    失败一律返回原因（调用方写进样本的 `error`），不静默继续：
    - 有子任务没有声明脚本；
    - 子作业不是 pending（不是"还在排队"，可能已被别人认领/已 done 但行未收尾）；
    - Worker 自身报错、认领了预期外的 Run；
    - 子任务在 drain 之后仍未到终态；
    - 子任务全终结但父作业没有被唤醒。
    """
    rows = _unfinished_subtasks(stack, parent_run_id)
    if not rows:
        return None

    missing = [row.subtask_id for row in rows if row.subtask_id not in case.subtask_scripts]
    if missing:
        return (
            f"子任务没有声明评测脚本，拒绝用父脚本冒充：{missing}（parent_run={parent_run_id}）"
        )

    parent_job = stack.claims.job_for_run(parent_run_id)
    if parent_job is None or parent_job.state != "waiting":
        return (
            f"父作业不处于 waiting（state={None if parent_job is None else parent_job.state}），"
            "不能在等待窗口驱动子任务"
        )

    expected = {row.child_run_id: row.subtask_id for row in rows}
    not_pending = []
    for row in rows:
        job = stack.claims.job_for_run(row.child_run_id)
        state = None if job is None else job.state
        if state != "pending":
            not_pending.append((row.subtask_id, state))
    if not_pending:
        return f"子作业不是 pending，拒绝认领顺序不确定的队列：{not_pending}"

    def factory(claim: Any) -> AgentLoop:
        run = stack.tasks.get_run(claim.run_id)
        if (
            run is None
            or run.parent_run_id != parent_run_id
            or not run.subtask_id
            or claim.run_id not in expected
        ):
            raise TianshuError(
                f"评测器只驱动子 Run，却认领到 {claim.run_id}（parent_run={parent_run_id}）"
            )
        return AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            model=_child_model(case, run.subtask_id),
            limits=limits,
            # 与 Worker._engine 相同的装配：预算/内容/记忆/摘要是同一份，
            # 否则子任务的上限与结果引用会落在另一个投影里。
            memory=stack.memory,
            summaries=stack.summaries,
            budget=getattr(stack, "budget", None),
            contents=getattr(stack, "contents", None),
        )

    worker = Worker(
        stack=stack,
        # engine_factory 一定会被调用；这个模型只是 Dataclass 的必填占位。
        model=_FailingModel("worker 占位模型不应被调用"),
        engine_factory=factory,
        heartbeat_interval_ms=None,
    )
    try:
        worker.drain(max_jobs=len(rows))
    finally:
        worker.close()

    if worker.errors:
        return "驱动子任务失败：" + "；".join(worker.errors)
    unexpected = [run_id for run_id in worker.run_ids if run_id not in expected]
    if unexpected:
        return f"Worker 认领了非子任务 Run：{unexpected}（预期={sorted(expected)}）"

    after = _unfinished_subtasks(stack, parent_run_id)
    if after:
        return (
            "子任务没有到终态："
            + "；".join(f"{row.subtask_id}={row.state}" for row in after)
        )
    job = stack.claims.job_for_run(parent_run_id)
    if job is None or job.state != "pending":
        return (
            "子任务已全部终结，但父作业没有被唤醒（"
            f"state={None if job is None else job.state}）"
        )
    return None


def _collect_subtask_evidence(
    stack: Any, parent_run_id: str, log: EventLog
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Run 结束后读子任务行与子 Run 事件（父 Run 的 events 不含它们）。"""
    rows = stack.tasks.list_subtasks(parent_run_id)
    subtasks: list[dict[str, Any]] = []
    child_events: list[Any] = []
    for row in rows:
        data = row.to_dict()
        child = stack.tasks.get_run(row.child_run_id)
        data["child_run_state"] = None if child is None else child.state
        subtasks.append(data)
        child_events.extend(log.read_run(row.child_run_id))
    return subtasks, child_events


# ---------------------------------------------------------------- 普通运行


def _build_engine(
    stack: Any,
    model: Any,
    *,
    max_turns: int,
    max_total_tokens: int,
) -> AgentLoop:
    """按**同一个装配**造引擎（第一次 Run 与 followup 必须用同一套投影）。

    为什么强调"同一套"：评测若给第二段另开一个默认投影，测到的记忆行为就不是
    产品装出来的那套（第一次 Run 与第二段会读两个库）。所以两段都从同一个
    `stack` 取 `memory` / `summaries`。
    """
    return AgentLoop(
        tasks=stack.tasks,
        leases=stack.leases,
        claims=stack.claims,
        dispatcher=stack.dispatcher,
        approvals=stack.approvals,
        snapshots=stack.snapshots,
        contents=stack.contents,
        model=model,
        limits=Limits(max_turns, max_total_tokens),
        memory=stack.memory,
        summaries=stack.summaries,
    )


def _drive_to_terminal(
    engine: AgentLoop,
    stack: Any,
    case: Case,
    *,
    res: Any,
    ws: Path,
) -> tuple[Any, dict[str, bytes] | None, str | None]:
    """把一条 Run 驱动到"不再需要我们动手"的状态。

    返回 `(res, files_at_pause, error)`：`error` 非空表示评测器判断这条 Run 没
    正常收束（停在审批超过上限 / 有未完成子任务却没声明子脚本 / 委派往返超限
    / 停在 running 且没有子任务可驱动）。

    这段循环是从 `_run_standard` 里原样抽出来的（行为不变）：G2 记忆阶段 M2 的
    followup 必须走同一套审批与委派规则，复制一份早晚会走岔。
    """
    files_at_pause: dict[str, bytes] | None = None
    error: str | None = None
    rounds = 0
    delegation_rounds = 0
    while True:
        if res.state == RUN_WAITING_APPROVAL:
            if rounds >= MAX_APPROVAL_ROUNDS:
                if case.approval != "hold":
                    # 只有 hold 是有意的终点（恶意输入被人拦下、人还没决定）。
                    error = f"审批往返超过 {MAX_APPROVAL_ROUNDS} 次仍未收敛"
                break
            rounds += 1
            if files_at_pause is None:
                # "批准之前文件一个字节都没动"只能在这里证明：等 Run 结束再看
                # 文件对不对，证明不了中间有没有先写后改。
                files_at_pause = _read_tree(ws)
            if case.approval == "hold":
                # 恶意输入 case：破坏性动作挂进审批队列，人还没决定。
                # 评测器**不替人批准**，就停在这里观察——这正是要证明的状态。
                break
            if case.approval == "none":
                # 没声明会停却停了：case 与实现不一致，记成 harness 归因。
                error = (
                    "case 未声明审批策略，但 Run 停在等待审批：case 定义与实现不一致"
                )
                break
            decision = "rejected" if case.approval == "reject" else "approved"
            actor = HumanIdentity.local_cli()
            pending = stack.approvals.pending(run_id=res.run_id)
            if not pending:
                error = "Run 停在等待审批，但审批队列为空：状态与记录不一致"
                break
            for approval in pending:
                stack.approvals.decide(
                    approval.approval_id, decision=decision, actor=actor
                )
            # 拒绝之后也要继续驱动：新语义（issue #40）下这次 resume 会把
            # cursor 里那张待批动作按"否决"拒绝，模型拿不出替代动作时 Run
            # 以 failed 收束；**不该**再出现第二张审批。审批往返上限保证
            # 评测器不会卡死，`approval.requested` 的精确计数判据负责判红。
            res = engine.resume_run(res.run_id)
            continue

        unfinished = stack.tasks.count_unfinished_subtasks(res.run_id)
        if unfinished == 0:
            if res.state == "running":
                error = (
                    "父 Run 停在 running，但已无未完成子任务（既不是等待审批"
                    "也不是终态）：评测器不会凭空继续驱动它"
                )
            break
        if not case.subtask_scripts:
            error = (
                f"父 Run 还有 {unfinished} 个未完成子任务，但 case 没有声明 "
                "subtask_scripts：评测器不会用父脚本冒充子 Run"
            )
            break
        if delegation_rounds >= MAX_DELEGATION_ROUNDS:
            error = (
                f"委派往返超过 {MAX_DELEGATION_ROUNDS} 轮仍未收敛"
                f"（还有 {unfinished} 个未完成子任务）"
            )
            break
        delegation_rounds += 1
        drive_error = _drive_child_runs(
            stack,
            case,
            res.run_id,
            limits=Limits(case.max_turns, case.max_total_tokens),
        )
        if drive_error is not None:
            error = drive_error
            break
        res = engine.resume_run(res.run_id)
    return res, files_at_pause, error


def _write_fixture(root: Path, fixture: Mapping[str, str]) -> None:
    """按"相对路径 → 正文"把夹具写进目录（父目录自动建）。"""
    for rel, text in fixture.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def _run_followup(
    case: Case,
    stack: Any,
    log: EventLog,
    directory: Path,
    *,
    first_res: Any,
    model_for: ModelFactory,
) -> FollowupObservation:
    """跑同一个样本的第二段 Run（G2 记忆阶段 M2）。

    硬规矩（不是约定）：

    1. **共享数据根、不同 workspace**：记忆库 / 事件库 / 用户作用域用同一个
       `stack`（同一个 `data_root`），workspace 必须是另一个目录——同一个目录里
       跑两次不叫"新任务"；
    2. **证据不合并**：第二段的事件/产物/回放报告单独返回，判据分开读；
    3. **第一段没到终态就不跑第二段**：那不是"记忆没用"，是"第一段没做完"，
       硬跑会让归因变成假的。
    """
    plan = case.followup
    assert plan is not None
    if plan.workspace == "ws":
        raise ValueError(
            "followup.workspace 不能等于第一个 workspace（同一个目录里跑两次不是新任务）"
        )
    if first_res.state not in {"succeeded", "failed", "cancelled"}:
        return FollowupObservation(
            run_id="",
            out_name=plan.out_name,
            error=(
                f"第一段没有跑到终态（state={first_res.state}），第二段不跑："
                "那不是「记忆没帮上忙」，是第一段没做完"
            ),
            first_run_terminal=False,
        )

    ws = directory / plan.workspace
    if ws.exists():
        # 干净目录是硬前提：有残留说明上一条样本没被清掉。
        raise ValueError(f"第二段 workspace 目录已存在，拒绝在脏目录上跑：{ws}")
    ws.mkdir(parents=True)
    _write_fixture(ws, plan.fixture)

    decoy_ids: list[str] = []
    if plan.decoy_workspace_memory is not None:
        # 替"用户"在**另一个目录**里记一条 workspace 私有记忆（人路径）。
        # 它的作用：让"第二段没有把别的目录的私有记忆捞进来"这条判据有靶子。
        from .. import memory_control
        from ..resources import MemoryScope

        decoy_ws = directory / "decoy-ws"
        decoy_ws.mkdir(parents=True, exist_ok=True)
        result = memory_control.remember(
            stack,
            MemoryScope.workspace(decoy_ws),
            kind="fact",
            content=plan.decoy_workspace_memory,
        )
        memory_id = (result.output or {}).get("memory_id")
        if isinstance(memory_id, str):
            decoy_ids.append(memory_id)

    second_case = replace(
        case,
        task=plan.task,
        out_name=plan.out_name,
        script=plan.script,
        checks=plan.checks,
        fixture=plan.fixture,
        approval=plan.approval,
        # 第二段不派生委派：这条 case 只声明第一段的子任务脚本；第二段若真去
        # 派子任务，`_drive_to_terminal` 会 fail closed（拒绝用父脚本冒充）。
        subtask_scripts={},
        max_subtasks=0,
        followup=None,
    )
    engine = _build_engine(
        stack,
        model_for(second_case),
        max_turns=plan.max_turns,
        max_total_tokens=case.max_total_tokens,
    )
    task = stack.tasks.create_task(plan.task, [str(ws)])
    started = engine.start(task.task_id, out_name=plan.out_name)
    res, _, error = _drive_to_terminal(engine, stack, second_case, res=started, ws=ws)
    events = log.read_run(res.run_id)
    replay_report = replay(log, res.run_id)
    return FollowupObservation(
        run_id=res.run_id,
        out_name=plan.out_name,
        files=_read_tree(ws),
        events=events,
        result=res,
        replay_report=replay_report,
        error=error,
        first_run_terminal=True,
        decoy_memory_ids=tuple(decoy_ids),
    )


def _run_standard(
    case: Case,
    *,
    run_index: int,
    workdir: Path,
    model_for: ModelFactory,
) -> Observation:
    directory = case_dir(workdir, case, run_index)
    ws, outside = _prepare(directory, case)
    if case.unavailable_reason is not None:
        # PRD §10.5：本轮不提供的能力，**不许**用脚本或静态网页冒充线上完成。
        # 这里刻意不启动引擎、不写事件库：样本只证明"报告如实写了未具备能力"。
        return Observation(
            case_id=case.case_id,
            category=case.category,
            run_index=run_index,
            run_id="",
            out_name=case.out_name,
            fixture={rel: text.encode("utf-8") for rel, text in case.fixture.items()},
            files=_read_tree(ws),
            unavailable=case.unavailable_reason,
        )
    log = EventLog(directory / "events.db", blob_dir=directory / "blobs")
    started = time.perf_counter()
    error: str | None = None
    # 联网研究（PRD §10.5）的本机来源：必须在**开跑之前**起来（地址要写进任务文本
    # 与脚本），并且在**核验来源之前**关掉（证明"可离线核验"，而不是"服务器还在"）。
    web: WebFixture | None = None
    web_socket: object | None = None
    offline_sources: list[dict[str, Any]] = []
    res = None
    files_at_pause: dict[str, bytes] | None = None
    replay_report: ReplayReport | None = None
    events = []
    run_id = ""
    invalid_reason: str | None = None
    post_run: dict[str, Any] = {}
    # G2 委派 D3：子任务行与子 Run 事件（父 Run 的 events 不含它们）。
    subtasks: list[dict[str, Any]] = []
    subtask_events: list[Any] = []
    # G2 记忆阶段 M2：第二段 Run 的证据（默认 None = 这条 case 只跑一段）。
    followup_obs: FollowupObservation | None = None
    try:
        web, web_socket, base = _start_web(case)
        effective = _materialize_case(case, base)
        stack = build_stack(log, data_root=directory / "data")
        engine = AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            contents=stack.contents,
            model=model_for(effective),
            limits=Limits(effective.max_turns, effective.max_total_tokens),
            # 评测也必须复用装配里的投影：评测若另开一个默认投影，
            # 测到的记忆行为就不是产品装出来的那套。
            memory=stack.memory,
            summaries=stack.summaries,
        )
        task = stack.tasks.create_task(
            effective.task,
            [str(ws)],
            outbound_allow=list(effective.outbound_allow) or None,
            # G2 委派 D3：委派能力必须由任务显式声明；评测里逐条任务写死，
            # 不给所有任务偷偷打开。
            max_subtasks=effective.max_subtasks,
        )
        res = engine.start(task.task_id, out_name=effective.out_name)
        run_id = res.run_id

        rounds = 0
        delegation_rounds = 0
        while True:
            if res.state == RUN_WAITING_APPROVAL:
                if rounds >= MAX_APPROVAL_ROUNDS:
                    if case.approval != "hold":
                        # 只有 hold 是有意的终点（恶意输入被人拦下、人还没决定）。
                        error = f"审批往返超过 {MAX_APPROVAL_ROUNDS} 次仍未收敛"
                    break
                rounds += 1
                if files_at_pause is None:
                    # "批准之前文件一个字节都没动"只能在这里证明：等 Run 结束再看
                    # 文件对不对，证明不了中间有没有先写后改。
                    files_at_pause = _read_tree(ws)
                if case.approval == "hold":
                    # 恶意输入 case：破坏性动作挂进审批队列，人还没决定。
                    # 评测器**不替人批准**，就停在这里观察——这正是要证明的状态。
                    break
                if case.approval == "none":
                    # 没声明会停却停了：case 与实现不一致，记成 harness 归因。
                    error = (
                        "case 未声明审批策略，但 Run 停在等待审批：case 定义与实现不一致"
                    )
                    break
                decision = "rejected" if case.approval == "reject" else "approved"
                actor = HumanIdentity.local_cli()
                pending = stack.approvals.pending(run_id=res.run_id)
                if not pending:
                    error = "Run 停在等待审批，但审批队列为空：状态与记录不一致"
                    break
                for approval in pending:
                    stack.approvals.decide(
                        approval.approval_id, decision=decision, actor=actor
                    )
                # 拒绝之后也要继续驱动：新语义（issue #40）下这次 resume 会把
                # cursor 里那张待批动作按"否决"拒绝，模型拿不出替代动作时 Run
                # 以 failed 收束；**不该**再出现第二张审批。审批往返上限保证
                # 评测器不会卡死，`approval.requested` 的精确计数判据负责判红。
                res = engine.resume_run(res.run_id)
                continue

            unfinished = stack.tasks.count_unfinished_subtasks(res.run_id)
            if unfinished == 0:
                if res.state == "running":
                    error = (
                        "父 Run 停在 running，但已无未完成子任务（既不是等待审批"
                        "也不是终态）：评测器不会凭空继续驱动它"
                    )
                break
            if not case.subtask_scripts:
                error = (
                    f"父 Run 还有 {unfinished} 个未完成子任务，但 case 没有声明 "
                    "subtask_scripts：评测器不会用父脚本冒充子 Run"
                )
                break
            if delegation_rounds >= MAX_DELEGATION_ROUNDS:
                error = (
                    f"委派往返超过 {MAX_DELEGATION_ROUNDS} 轮仍未收敛"
                    f"（还有 {unfinished} 个未完成子任务）"
                )
                break
            delegation_rounds += 1
            drive_error = _drive_child_runs(
                stack,
                effective,
                res.run_id,
                limits=Limits(effective.max_turns, effective.max_total_tokens),
            )
            if drive_error is not None:
                error = drive_error
                break
            res = engine.resume_run(res.run_id)

        # 场景钩子（如"撤销冲突"）：Run 结束后再动一下产品 API，收集事实。
        # 它跑在取证之前，钩子里写的事件（undo.*）才会进这次样本的证据。
        if case.post_run is not None:
            try:
                post_run = dict(case.post_run(stack, res.run_id, ws))
            except Exception as exc:  # noqa: BLE001 - 场景失败也是产品事实
                post_run = {"error": f"{type(exc).__name__}: {exc}"}

        # 取证失败 = 评测基础设施无效（PRD §10.2 判据边界）。它和"产品失败"
        # 必须分开：取证失败时我们没有资格给这次运行下任何结论。
        try:
            events = log.read_run(res.run_id)
            replay_report = replay(log, res.run_id)
            subtasks, subtask_events = _collect_subtask_evidence(
                stack, res.run_id, log
            )
        except Exception as exc:  # noqa: BLE001
            invalid_reason = f"取证失败：{type(exc).__name__}: {exc}"

        # G2 记忆阶段 M2：第二段 Run（同一 data_root、另一个 workspace）。
        if effective.followup is not None:
            if invalid_reason is not None:
                followup_obs = FollowupObservation(
                    run_id="",
                    out_name=effective.followup.out_name,
                    error=f"第一段取证失败，第二段不跑：{invalid_reason}",
                )
            else:
                try:
                    followup_obs = _run_followup(
                        effective,
                        stack,
                        log,
                        directory,
                        first_res=res,
                        model_for=model_for,
                    )
                except Exception as exc:  # noqa: BLE001 - 评测基础设施）
                    followup_obs = None
                    invalid_reason = (
                        f"第二段取证失败：{type(exc).__name__}: {exc}"
                    )

        # 来源可离线核验（PRD §10.5）：**先关服务器**，再读内容引用。
        if web is not None:
            web.stop()
            web = None
        if case.web_routes or case.web_closed:
            try:
                offline_sources = _collect_offline_sources(stack, effective, events, log)
            except Exception as exc:  # noqa: BLE001 - 读不回来源 = 评测基础设施无效
                invalid_reason = f"来源离线核验失败：{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - 评测器本身出错也要落成证据
        error = f"评测器异常：{type(exc).__name__}: {exc}"
        # 只有"基础设施"类故障才算无效样本（夹具/后端/磁盘）。引擎自己抛异常
        # 是产品失败——把它剔除掉，等于把"产品坏了"洗成"这次不算"。
        if isinstance(exc, (OSError, sqlite3.Error)):
            invalid_reason = f"评测基础设施无效：{type(exc).__name__}: {exc}"
    finally:
        if web is not None:
            web.stop()
        if web_socket is not None:
            web_socket.close()
        files = _read_tree(ws)
        outside_files = _read_tree(outside)
        if not run_id:
            run_id = _latest_run_id(log) or ""
        log.close()

    obs = Observation(
        case_id=case.case_id,
        category=case.category,
        run_index=run_index,
        run_id=run_id,
        out_name=case.out_name,
        fixture={rel: text.encode("utf-8") for rel, text in case.fixture.items()},
        outside_fixture={
            rel: text.encode("utf-8") for rel, text in case.outside.items()
        },
        files=files,
        outside=outside_files,
        files_at_pause=files_at_pause,
        events=events,
        result=res,
        replay_report=replay_report,
        error=error,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        model_calls=len(
            [e for e in events if e.type == "model.intent"]
            + [e for e in subtask_events if e.type == "model.intent"]
        ),
        invalid_reason=invalid_reason,
        post_run=post_run,
        offline_sources=offline_sources,
        subtasks=subtasks,
        subtask_events=subtask_events,
        followup=followup_obs,
    )
    return obs


# ---------------------------------------------------------------- 崩溃运行


def _run_crash(
    case: Case,
    *,
    run_index: int,
    workdir: Path,
    model_for: ModelFactory,  # noqa: ARG001 - 崩溃 case 恒用离线脚本，见 note
) -> Observation:
    """子进程硬退 → 父进程恢复。

    崩溃 case 固定用 case 自带的脚本模型：崩溃点是**注入**的，换成真实模型只会
    让"这次到底崩在写入前还是写入后"变得不确定，而这条 case 要证明的恰恰是
    恢复判据本身，不是模型的随机性。所以它们 `scope=harness`，不进模型对比。
    """
    # 子进程是按 `case_id` 重新加载定义的（崩溃注入要求"父进程算坐标、子进程
    # 跑同一份定义"）。传进来的 Case 若与模块里那条不一致，两边跑的就是两条
    # 不同的 case；这必须当场拒绝，不能默默用子进程的定义覆盖父进程的判据。
    canonical = CASE_BY_ID.get(case.case_id)
    if canonical != case:
        raise ValueError(
            f"崩溃 case {case.case_id!r} 与 cases.CASE_BY_ID 里的定义不一致："
            "子进程会按 id 重新加载，父进程的判据会与它跑的东西对不上"
        )
    directory = case_dir(workdir, case, run_index)
    ws, outside = _prepare(directory, case)
    db = directory / "events.db"
    started = time.perf_counter()
    error: str | None = None
    recovery_report: RecoveryReport | None = None
    replay_report: ReplayReport | None = None
    events = []
    run_id = ""
    invalid_reason: str | None = None

    env = dict(os.environ)
    env.pop("TIANSHU_FAILPOINT", None)
    env["TIANSHU_FAILPOINT"] = str(case.crash)
    src = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    cmd = [
        sys.executable,
        "-m",
        CHILD_MODULE,
        case.case_id,
        str(ws),
        str(db),
        str(directory / "data"),
    ]
    timed_out = False
    proc: subprocess.CompletedProcess[str] | None = None
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=CHILD_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        # 子进程挂住不能让整套评测直接炸掉：落成这一条 case 的 harness 异常，
        # 其它 case 继续跑，报告里能看见"谁超时了"。按 PRD §10.2 判据边界，
        # "子进程起不来/挂住、没有可核实退出状态" = 评测基础设施无效样本。
        timed_out = True
        invalid_reason = f"崩溃子进程挂住（>{CHILD_TIMEOUT_S}s），没有可核实的退出状态"
    except OSError as exc:
        return Observation(
            case_id=case.case_id,
            category=case.category,
            run_index=run_index,
            run_id="",
            out_name=case.out_name,
            fixture={rel: text.encode("utf-8") for rel, text in case.fixture.items()},
            outside_fixture={
                rel: text.encode("utf-8") for rel, text in case.outside.items()
            },
            files=_read_tree(ws),
            outside={},
            error=f"崩溃子进程起不来：{type(exc).__name__}: {exc}",
            invalid_reason=f"评测基础设施无效：子进程起不来（{type(exc).__name__}）",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    crashed = proc is not None and proc.returncode == 77

    log = EventLog(db, blob_dir=directory / "blobs")
    try:
        run_id = _latest_run_id(log) or ""
        if timed_out:
            error = f"崩溃子进程超时（>{CHILD_TIMEOUT_S}s），没有可核实的退出状态"
        elif not crashed:
            error = (
                f"故障点 {case.crash!r} 未被触发（子进程退出码 {proc.returncode}）："
                "case 定义与实现不一致"
            )
        if not run_id:
            error = error or "子进程没有留下 run.start 事件"
        else:
            stack = build_stack(log, data_root=directory / "data")
            recovery_report = recover_run(
                log, run_id, ws, stack.snapshots,
                leases=stack.leases, data_root=directory / "data",
            )
            events = log.read_run(run_id)
            replay_report = replay(log, run_id)
    except Exception as exc:  # noqa: BLE001
        error = error or f"评测器异常：{type(exc).__name__}: {exc}"
        if isinstance(exc, (OSError, sqlite3.Error)):
            invalid_reason = f"取证失败：{type(exc).__name__}: {exc}"
    finally:
        files = _read_tree(ws)
        outside_files = _read_tree(outside)
        log.close()

    return Observation(
        case_id=case.case_id,
        category=case.category,
        run_index=run_index,
        run_id=run_id,
        out_name=case.out_name,
        fixture={rel: text.encode("utf-8") for rel, text in case.fixture.items()},
        outside_fixture={
            rel: text.encode("utf-8") for rel, text in case.outside.items()
        },
        files=files,
        outside=outside_files,
        events=events,
        replay_report=replay_report,
        recovery_report=recovery_report,
        error=error,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        model_calls=len([e for e in events if e.type == "model.intent"]),
        crashed=crashed,
        invalid_reason=invalid_reason,
    )


# ---------------------------------------------------------------- 整套


def run_suite(
    cases: tuple[Case, ...],
    *,
    model_for: ModelFactory,
    repeat: int,
    workdir: Path,
    keep: bool = False,
    suite_version: str = SUITE_VERSION,
    identity: dict[str, str | None] | None = None,
    on_case: Callable[[Case, list[Observation]], None] | None = None,
) -> SuiteRun:
    """按 case × repeat 跑完整套。每条运行一个干净目录。"""
    if repeat < 1:
        raise ValueError("repeat 必须 >= 1")
    # 先拒绝，再建目录：拒绝时不该在调用方盘上留下半套工作根。
    require_local_backend()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    observations: list[Observation] = []
    started = time.perf_counter()

    for case in cases:
        per_case: list[Observation] = []
        for run_index in range(repeat):
            directory = case_dir(workdir, case, run_index)
            if directory.exists():
                # 干净 fixture 是硬前提：跑之前有残留，说明工作根给错了。
                raise FileExistsError(
                    f"运行目录已存在，拒绝在脏目录上评测：{directory}"
                )
            if case.crash:
                obs = _run_crash(
                    case, run_index=run_index, workdir=workdir, model_for=model_for
                )
            else:
                obs = _run_standard(
                    case, run_index=run_index, workdir=workdir, model_for=model_for
                )
            per_case.append(obs)
            observations.append(obs)
            if not keep:
                shutil.rmtree(directory, ignore_errors=True)
        if not keep:
            # 每个运行目录都删干净之后，case 目录本身只剩空壳；留着会让
            # 下一次评测的"干净 fixture"检查误判成脏目录。
            shutil.rmtree(workdir / case.case_id, ignore_errors=True)
        if on_case is not None:
            on_case(case, per_case)

    return SuiteRun(
        observations=observations,
        workdir=workdir,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        identity=identity
        or _identity_of(model_for, next((c for c in cases if not c.crash), cases[0])),
        suite_version=suite_version,
        repeat=repeat,
    )


__all__ = ["SuiteRun", "case_dir", "run_suite"]