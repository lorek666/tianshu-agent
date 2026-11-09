"""作业循环（M2 · PR C 的 Task 6）。

`jobs.py` 回答"这个 Run 现在归谁跑"，本模块回答**"谁去把它跑起来"**：

    claim → resume/drive → settle

三件事都必须做对，否则"排队中的任务"只是数据库里的一行：

1. **认领**要能接手**死掉的** owner，而不是等 30 秒 TTL 走完。用户 kill 掉
   进程再 `resume`，不该先卡半分钟。
2. **驱动**必须与前台 `run start` / `run resume` 走**同一条**执行路径
   （`AgentLoop.resume_run` → 派发 → 审批 → 租约）。worker 只负责调度，
   不许自己写文件——不然"策略判定点唯一"这句话就不成立了。
3. **结算**要区分四种结果：
   - Run 已终结 → 用**自己的 claim** 收掉作业（那是"我还活着且还是 owner"的证据）；
   - Run 停下等人（等审批 / 已暂停 / 待核查）→ 挂起作业（`park`）；
   - 抢不到工作区租约 → 交还作业（`requeue`）：那是**排队**，不是"等人决定"；
   - 收尾被拒（已被接管）→ 什么都不做，不许替新 owner 写。
   被 fence 的 Run 由 `_fence_run` 自己收作业，worker 连认领的机会都没有。
   
   挂起而不是"放回 pending"：轮询循环会立刻再认领、再停在同一个地方，
   每轮涨一次 `claim_epoch`，把用户刚批的审批作废。

**心跳为什么需要一个线程。** 模型调用是阻塞的（真实端点几秒到几分钟）。
宿主线程一旦阻塞就没法自证"我还活着"，而 jobs 的默认 deadline 是 30 秒：
不续期，任何一个慢一点的模型请求都会让作业变成"可接管"，下一个进程一认领，
两边就开始抢同一份游标。所以阻塞期间要有**另一个线程**续租约与 deadline。
SQLite 连接不能跨线程共享，因此心跳线程用自己的一条连接（`_SideChannel`），
续的东西与主循环 `heartbeat_once` 完全一致（同一个 `renew_claim`）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import TianshuError
from .events import EventLog
from .jobs import (
    JOB_CLAIMED,
    JOB_OUTCOMES,
    JobClaim,
    JobLost,
    JobStore,
    JobUnavailable,
    renew_claim,
)
from .leases import (
    DEFAULT_TTL_MS,
    HEARTBEAT_MS,
    LeaseConflict,
    LeaseStore,
    ProcessIdentity,
)
from .loop import AgentLoop, Limits, RunResult, Stack
from .model import Model
from .tasks import (
    RUN_FENCED,
    RUN_PAUSED,
    RUN_WAITING_APPROVAL,
    TERMINAL_STATES,
)

#: 一次 `drain` 最多跑多少个作业。防的是"队列里有一万个作业，进程再也不回来"。
MAX_JOBS_PER_DRAIN = 64

#: 撞上别人的工作区租约时，最多等几轮再放弃。
#:
#: 交还作业本身是对的（那是"排队"，不是故障），但**交还完立刻放手**会留下
#: 一个没人捡的作业：持有者最后一次扫描时看到的还是"这个作业被活人占着"，
#: 于是它判定队空退出；之后交还才落到 `pending`，而这时已经没有活着的
#: worker 了（issue #31 的红就是这么来的）。所以这里等一小会儿再看。
MAX_CONTENTION_WAITS = 20

#: 每次等多久。这是**排队**不是忙等：持有者正在写盘，等的时候无事可做。
CONTENTION_WAIT_MS = 25


class HeartbeatLoop:
    """在宿主线程阻塞期间续 claim。

    失败即停止：心跳失败的语义是"我已经不是 owner 了"，继续每 40ms 刷一条
    注定失败的心跳只会把日志和数据库拖慢，问题也不会因此变轻。
    错误留给调用方（`error`），由它决定这次驱动还能不能算成功。
    """

    def __init__(
        self,
        beat: Callable[[], bool | None],
        *,
        interval_ms: int = HEARTBEAT_MS,
        name: str = "tianshu-heartbeat",
        join_timeout_ms: int = 12_000,
    ) -> None:
        if interval_ms is not None and interval_ms <= 0:
            raise TianshuError("心跳间隔必须为正数")
        self._beat = beat
        self._interval_s = max(1, int(interval_ms)) / 1000
        self._name = name
        self._join_timeout_s = join_timeout_ms / 1000
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._count_lock = threading.Lock()
        self.beats = 0
        self.error: BaseException | None = None
        self.stopped_promptly = True

    # ---- 生命周期 ----

    def start(self) -> HeartbeatLoop:
        if self._thread is not None:
            raise TianshuError("心跳循环已经启动过")
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """请求停止并**等到线程真的退出**。

        为什么必须等：调用方下一步往往会关掉这条连接。线程还活着时关连接，
        线程里会抛 `ProgrammingError: Cannot operate on a closed database`，
        而那个异常出现在后台线程里，没人会看到它——只会表现成"心跳偶尔没生效"。
        单次 beat 最长会被 `busy_timeout`（5s）挡住，所以默认给 12s。
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(self._join_timeout_s)
        self.stopped_promptly = not thread.is_alive()

    def __enter__(self) -> HeartbeatLoop:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---- 线程主体 ----

    def _run(self) -> None:
        busy_retries = 0
        # 先打一次再等：宿主线程紧接着就要进阻塞调用，不能先等一个 interval。
        while not self._stop.is_set():
            try:
                keep_going = self._beat()
            except sqlite3.OperationalError as exc:
                # "database is locked/busy" 不是"我丢了 claim"，只是主线程正在
                # 提交一个大事务。把它当成心跳失败会让一次正常的慢写入变成
                # 一次误判的接管；重试几次（每次最多 busy_timeout）再放弃。
                if "lock" not in str(exc).lower() and "busy" not in str(exc).lower():
                    if self.error is None:
                        self.error = exc
                    return
                busy_retries += 1
                if busy_retries > 3:
                    if self.error is None:
                        self.error = exc
                    return
                if self._stop.wait(self._interval_s):
                    return
                continue
            except BaseException as exc:  # noqa: BLE001 - 心跳失败必须留痕，不能吞
                if self.error is None:
                    self.error = exc
                return
            if keep_going is False:
                # 作业已经收尾：心跳没有意义，静默退出（不是失败）
                return
            busy_retries = 0
            with self._count_lock:
                self.beats += 1
            if self._stop.wait(self._interval_s):
                return


class _SideChannel:
    """心跳线程专用的第二条数据库连接。

    SQLite 连接默认 `check_same_thread=True`，跨线程用同一个连接会直接抛
    `ProgrammingError`；而心跳**必须**在别的线程里发生。所以这里开一条独立
    连接，但用**同一个** `ProcessIdentity`——续租与续 claim 都按实例身份比对，
    换一个身份就等于替别人续命。
    """

    def __init__(
        self,
        db_path,
        identity: ProcessIdentity,
        *,
        blob_dir=None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        # 这条连接**只**给心跳线程用；主线程永远不碰它（各自一条连接）。
        self._log = EventLog(db_path, blob_dir=blob_dir, cross_thread=True)
        kwargs = {} if clock is None else {"clock": clock}
        self._leases = LeaseStore(self._log, identity=identity, **kwargs)
        self._claims = JobStore(self._log, identity=identity, **kwargs)

    def beat(self, claim: JobClaim, *, ttl_ms: int = DEFAULT_TTL_MS) -> bool:
        return renew_claim(self._leases, self._claims, claim, ttl_ms=ttl_ms)

    def close(self) -> None:
        self._log.close()


@dataclass
class Worker:
    """一个作业循环。没有状态：所有状态都在 SQLite 里。"""

    stack: Stack
    model: Model
    limits: Limits | None = None
    ttl_ms: int = DEFAULT_TTL_MS
    heartbeat_interval_ms: int | None = HEARTBEAT_MS
    claims: JobStore | None = None
    engine_factory: Callable[[JobClaim], AgentLoop] | None = None
    beats: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    #: 上一次认领是否因为"工作区被别的 Run 占着"而没能跑（暂时性，不是故障）。
    contended: bool = False
    _side_stuck: bool = False
    #: 上一趟 drain 是否因为"没活干"结束（而不是撞上上限或被占用打断）
    exhausted: bool = False

    def __post_init__(self) -> None:
        self._claims = self.claims or self.stack.claims
        self._tasks = self.stack.tasks
        self._side: _SideChannel | None = None

    # ---- 对外 ----

    def run_once(self) -> bool:
        """认领一个作业并推进它。返回"这次有没有活干"。"""
        claim = self._claims.claim_next(ttl_ms=self.ttl_ms)
        if claim is None:
            return False
        self.run_ids.append(claim.run_id)
        self.contended = False
        requeue = False
        try:
            self._drive(claim)
        except LeaseConflict as exc:
            # 工作区被别的 Run 占着。这是**暂时**跑不了，不是"等人决定"：
            # 挂起会让这条作业永远等人来 resume；而它其实只是排在别人后面。
            # M2 的设计就是「同一棵树同一时刻只有一个写者」，所以正确做法是
            # 交还作业，让持有者跑完再轮到自己。
            requeue = True
            self.contended = True
            self.notes.append(f"工作区被占用，已交还作业：{exc}")
        except (JobLost, JobUnavailable):
            # 被接管：本实例已经不是 owner，交出去即可，不写任何状态
            self.errors.append("作业已被接管，本实例停止提交")
        except Exception as exc:  # noqa: BLE001 - 单个作业失败不该带走整个循环
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            self._settle(claim, requeue=requeue)
        return True

    def drain(self, *, max_jobs: int = MAX_JOBS_PER_DRAIN) -> list[str]:
        """把当前能跑的作业跑完一轮。返回真正驱动过的 Run。

        `exhausted` 记录"这一趟是不是因为没活干了才停"：只有它成立才敢对外
        说 `idle`——否则 `--max-jobs 1` 跑满上限时也会自称空闲，而队里还有活。

        **这不等于"队列已经清空"的保证。** 撞上别人的工作区租约会交还作业
        （见模块注释第 3 条）：这种情况下这里会等一小会儿再试（最多
        `MAX_CONTENTION_WAITS` 轮）；等不到就带着 `exhausted=False` 返回，
        语义是"我还有活没干完，你隔一会儿再来"。

        调用方必须按 `exhausted`（CLI 里就是 `idle`）决定要不要再调一次，
        **不能把一次 `drain()` 当成"队列已空"**。issue #31 就是这么红的：
        测试只调一次 drain、随后断言 5 个作业全都写完，而"一次 drain 清空
        队列"从来不是这里提供的性质。
        """
        done: list[str] = []
        self.exhausted = False
        budget = max(1, max_jobs)
        waits = 0
        # 等待不该吃掉作业预算，但也不能无限转：上限 = 预算 + 允许的等待轮数。
        for _ in range(budget + MAX_CONTENTION_WAITS + 1):
            if len(done) >= budget:
                return done
            if not self.run_once():
                self.exhausted = True
                return done
            # 交还过的 Run 会在 run_ids 里反复出现（认领→交还→再认领），
            # 报给调用方时去掉重复：同一个 Run 列三遍会让人以为跑了三次。
            if self.run_ids[-1] not in done:
                done.append(self.run_ids[-1])
            if not self.contended:
                waits = 0
                continue
            if waits >= MAX_CONTENTION_WAITS:
                return done
            waits += 1
            time.sleep(CONTENTION_WAIT_MS / 1000)
        return done

    def close(self) -> None:
        if self._side is not None and self._side_stuck:
            # 有线程可能还在用它：留着，让进程退出时由解释器回收
            self._side = None
            return
        if self._side is not None:
            self._side.close()
            self._side = None

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- 内部 ----

    def _engine(self, claim: JobClaim) -> AgentLoop:
        if self.engine_factory is not None:
            return self.engine_factory(claim)
        return AgentLoop(
            tasks=self.stack.tasks,
            leases=self.stack.leases,
            claims=self._claims,
            dispatcher=self.stack.dispatcher,
            approvals=self.stack.approvals,
            snapshots=self.stack.snapshots,
            model=self.model,
            limits=self.limits or Limits(),
            ttl_ms=self.ttl_ms,
        )

    def _side_channel(self) -> _SideChannel:
        if self._side is None:
            self._side = _SideChannel(
                self.stack.log.db_path,
                self._claims.identity,
                blob_dir=self.stack.log.blob_dir,
            )
        return self._side

    def _drive(self, claim: JobClaim) -> RunResult | None:
        run = self._tasks.get_run(claim.run_id)
        if run is None:
            raise TianshuError(f"作业指向的 Run 不存在：{claim.run_id}")
        if run.state in TERMINAL_STATES or run.state == RUN_FENCED:
            # 已经结束（或被 fence）的 Run 不该再有作业；收掉它，绝不"恢复"
            return None
        if run.state == RUN_WAITING_APPROVAL:
            # 等审批的 Run 由**人**决定要不要继续（`run resume`）。worker 若替人
            # 继续，"批准 ≠ 执行"就变成了"批准后某次轮询执行"，行为还会随
            # "作业有没有被哪个 worker 先碰过"变化——同一件事有两种答案。
            return None
        if run.state == RUN_PAUSED:
            # 用户按过暂停：worker 是**自动**认领者，它替用户取消暂停等于
            # 让"暂停"这个动作无效。要继续只能由 `run resume` 明确说一次。
            return None

        interval = self.heartbeat_interval_ms
        if not interval:
            return self._engine(claim).resume_run(claim.run_id)

        channel = self._side_channel()
        with HeartbeatLoop(
            # 心跳必须用**认领时那个 ttl**：认领说 200ms 过期、心跳却按 30s 续命，
            # 谁都不知道到底多久算活着，测试里"抢不走"也就成了必然成立的空断言。
            lambda: channel.beat(claim, ttl_ms=self.ttl_ms),
            interval_ms=interval,
        ) as loop:
            result = self._engine(claim).resume_run(claim.run_id)
        self.beats += loop.beats
        if not loop.stopped_promptly:
            # 线程没能在超时内退出：说明它卡在某次数据库写入里。此时**不能**
            # 关掉它用的那条连接（关掉会让线程里抛 ProgrammingError，而那个
            # 异常在后台线程里没人看得到）。宁可泄漏一条连接，也不制造一个
            # 看不见的写失败。
            self._side_stuck = True
            self.errors.append("心跳线程未如期退出，保留连接以免写入被截断")
        if loop.error is not None:
            # 心跳线说自己已经不是 owner 了：这次驱动的结论不可信，
            # 但也不要抛——主循环写状态时一样会被 claim 核验拦住。
            self.errors.append(f"心跳失效：{loop.error}")
        return result

    def _settle(self, claim: JobClaim, *, requeue: bool = False) -> None:
        """把作业状态与 Run 状态对齐。**只在还持有 claim 时才动。**"""
        job = self._claims.get(claim.job_id)
        if job is None or job.state != JOB_CLAIMED:
            # 主循环已经收掉了（终态），或者被别人接管了：都不是我们的活
            return
        run = self._tasks.get_run(claim.run_id)
        state = run.state if run is not None else RUN_FENCED
        try:
            if state in TERMINAL_STATES:
                outcome = (run.outcome if run is not None else None) or "failed"
                self._claims.finish(
                    claim, outcome=outcome if outcome in JOB_OUTCOMES else "failed"
                )
            elif state == RUN_FENCED:
                self._claims.finish(claim, outcome="failed")
            elif requeue:
                self._claims.requeue(claim)
            else:
                self._claims.park(claim, reason=state)
        except TianshuError as exc:
            # 被接管 / 作业已被别处收掉：不是我们的活，也不该让整个循环崩掉
            self.errors.append(f"结算未生效：{exc}")


def run_worker_once(
    store: JobStore,
    owner: ProcessIdentity,
    *,
    stack: Stack,
    model: Model,
    limits: Limits | None = None,
    heartbeat_interval_ms: int | None = HEARTBEAT_MS,
    ttl_ms: int = DEFAULT_TTL_MS,
) -> bool:
    """认领一个作业 → 推进 → 结算。返回"有没有活干"。

    `store` 允许传一条独立连接上的 `JobStore`（多进程测试就是这样用的）；
    `owner` 只是把身份写清楚，实际比对仍以 `store.identity` 为准。
    """
    if owner.instance_id != store.identity.instance_id:
        # claim 的 owner 就是 store 的身份。两个不一致时，"我以谁的名义续命"
        # 这个问题没有第二个答案，直接拒绝比默默按 store 走更安全。
        raise TianshuError(
            "owner 与 JobStore 的进程身份不一致："
            f"owner={owner.instance_id} store={store.identity.instance_id}"
        )
    worker = Worker(
        stack,
        model=model,
        limits=limits,
        ttl_ms=ttl_ms,
        heartbeat_interval_ms=heartbeat_interval_ms,
        claims=store,
    )
    try:
        return worker.run_once()
    finally:
        worker.close()


__all__ = [
    "MAX_JOBS_PER_DRAIN",
    "HeartbeatLoop",
    "Worker",
    "run_worker_once",
]