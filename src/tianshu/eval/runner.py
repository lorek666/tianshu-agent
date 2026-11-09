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
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..events import EventLog
from ..loop import AgentLoop, Limits, build_stack
from ..model import Model, identity_of
from ..permissions import HumanIdentity
from ..recovery import RecoveryReport, recover_run
from ..replay import ReplayReport, replay
from ..tasks import RUN_WAITING_APPROVAL
from .cases import SUITE_VERSION, Case
from .checks import Observation

#: 一条 case 的运行目录长这样；子进程入口按同一约定拼路径。
CASE_DIRS = ("ws", "outside", "data", "blobs")
CHILD_MODULE = "tianshu.eval.crash_child"
CHILD_TIMEOUT_S = 120
#: 审批往返最多几次。一个正常 case 最多停一次；留 6 次是为了让"批准后又停"
#: 这类 bug 表现为失败，而不是把评测器卡死。
MAX_APPROVAL_ROUNDS = 6

ModelFactory = Callable[[Case], Model]


@dataclass
class SuiteRun:
    """一次整套评测的原始材料（报告构建在 report.py）。"""

    observations: list[Observation]
    workdir: Path
    elapsed_ms: int
    identity: dict[str, str | None]
    suite_version: str
    repeat: int
    #: 每条 case 的判据 id 清单（报告里要能看出"这次到底按什么判的"）。
    check_ids: dict[str, list[str]] = field(default_factory=dict)


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
    for rel, content in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


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


# ---------------------------------------------------------------- 普通运行


def _run_standard(
    case: Case,
    *,
    run_index: int,
    workdir: Path,
    model_for: ModelFactory,
) -> Observation:
    directory = case_dir(workdir, case, run_index)
    ws, outside = _prepare(directory, case)
    log = EventLog(directory / "events.db", blob_dir=directory / "blobs")
    started = time.perf_counter()
    error: str | None = None
    res = None
    files_at_pause: dict[str, bytes] | None = None
    replay_report: ReplayReport | None = None
    events = []
    run_id = ""
    try:
        stack = build_stack(log, data_root=directory / "data")
        engine = AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            model=model_for(case),
            limits=Limits(case.max_turns, case.max_total_tokens),
        )
        task = stack.tasks.create_task(case.task, [str(ws)])
        res = engine.start(task.task_id, out_name=case.out_name)
        run_id = res.run_id

        rounds = 0
        while res.state == RUN_WAITING_APPROVAL and rounds < MAX_APPROVAL_ROUNDS:
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
                error = "case 未声明审批策略，但 Run 停在等待审批：case 定义与实现不一致"
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
            res = engine.resume_run(res.run_id)

        if res.state == RUN_WAITING_APPROVAL:
            error = f"审批往返超过 {MAX_APPROVAL_ROUNDS} 次仍未收敛"

        events = log.read_run(res.run_id)
        replay_report = replay(log, res.run_id)
    except Exception as exc:  # noqa: BLE001 - 评测器本身出错也要落成证据
        error = f"评测器异常：{type(exc).__name__}: {exc}"
    finally:
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
        model_calls=len([e for e in events if e.type == "model.intent"]),
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
    directory = case_dir(workdir, case, run_index)
    ws, outside = _prepare(directory, case)
    db = directory / "events.db"
    started = time.perf_counter()
    error: str | None = None
    recovery_report: RecoveryReport | None = None
    replay_report: ReplayReport | None = None
    events = []
    run_id = ""

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
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=CHILD_TIMEOUT_S
    )
    crashed = proc.returncode == 77

    log = EventLog(db, blob_dir=directory / "blobs")
    try:
        run_id = _latest_run_id(log) or ""
        if not crashed:
            error = (
                f"故障点 {case.crash!r} 未被触发（子进程退出码 {proc.returncode}）："
                "case 定义与实现不一致"
            )
        if not run_id:
            error = error or "子进程没有留下 run.start 事件"
        else:
            stack = build_stack(log, data_root=directory / "data")
            recovery_report = recover_run(
                log, run_id, ws, stack.snapshots, leases=stack.leases
            )
            events = log.read_run(run_id)
            replay_report = replay(log, run_id)
    except Exception as exc:  # noqa: BLE001
        error = error or f"评测器异常：{type(exc).__name__}: {exc}"
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
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    observations: list[Observation] = []
    check_ids = {case.case_id: [c.check_id for c in case.checks] for case in cases}
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
        check_ids=check_ids,
    )


__all__ = ["CASE_DIRS", "SuiteRun", "case_dir", "run_suite"]