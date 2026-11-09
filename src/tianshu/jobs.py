"""作业认领（M2 · PR A 的 Task 2 剩余部分）。

Job 回答的是另一个问题：**"这个 Run 现在归谁跑"**。

Task/Run 解决的是"要做的事"和"这次执行"，但它不回答"谁在跑"。
没有这层就会出两种事故：

1. 两个 worker 认领同一个 Run，各自写 cursor、各自提交 turn，最终状态被写乱，
   而且同一个模型调用被付了两次钱。
2. worker A 卡住了（进程活着但心跳停了），worker B 接管；随后 A 醒过来继续写，
   用**旧身份**覆盖 B 的成果。A 是"活着但已过期"，靠租约 TTL 拦不住它。

第二种是这层存在的真正理由。所以这里的主键不是 `job_id`，而是
**`(owner_instance_id, claim_epoch)`**：
- `claim_epoch` 每次认领 / 接管 +1；
- 心跳、收尾、写 cursor、提交模型结果，全部用它做条件更新的 `WHERE`，
  `rowcount == 0` 就说明"你已经不是 owner 了"，只能失败，不能重试覆盖。

没有 `claim_epoch` 时，B 接管后 A 的 `finish` 依然能匹配到 `job_id` 那行并写成功；
也就是说"旧 worker 不得改新 owner 的状态"这句计划里的要求，只靠 pid/心跳时间
是做不到的——pid 会被复用，时间也是可以被后来者超越的。

**默认 deadline 是 30 秒，所以跑循环的一方必须定期 `heartbeat`。**
心跳循环属于 Task 6 的 worker；在那之前，任何超过 30 秒的模型调用都会让作业
变成"可接管"，接管者拿到更高的 `claim_epoch`，旧 worker 的下一次提交就会
`JobLost`。这不是 bug，是刻意的取舍：宁可让慢 worker 失败并交还工作，也不能
让它无声占着资源。跑真实模型时要把 heartbeat 接上再压测。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .events import EventLog
from .leases import (
    DEFAULT_TTL_MS,
    LeaseStore,
    ProcessIdentity,
    current_process,
    pid_alive,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',
    owner_instance_id TEXT,
    owner_pid         INTEGER,
    claim_epoch       INTEGER NOT NULL DEFAULT 0,
    claimed_at        INTEGER,
    heartbeat_at      INTEGER,
    deadline_at       INTEGER,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    output_ref        TEXT,
    outcome           TEXT,
    created_at        INTEGER NOT NULL,
    ended_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);

-- 一个 Run 最多一个未完成作业。没有这条约束，同一个 Run 可以有两个作业，
-- 两个 worker 各自 claim 到不同的 job_id、epoch 都是 1，都能通过 claim 核验，
-- 于是同一份 cursor 被两个 owner 交替写——fencing 形同虚设。
-- `waiting` 也算未完成：它只是"没人该自动跑它"，不是结束。
DROP INDEX IF EXISTS idx_jobs_one_open_per_run;

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_open_v2
    ON jobs(run_id) WHERE state IN ('pending','claimed','waiting');
"""

JOB_PENDING = "pending"
JOB_CLAIMED = "claimed"
JOB_WAITING = "waiting"
JOB_DONE = "done"
JOB_CANCELLED = "cancelled"

TERMINAL_JOB_STATES = {JOB_DONE, JOB_CANCELLED}
JOB_OUTCOMES = {"succeeded", "failed", "cancelled"}

JOB_ENQUEUED = "job.enqueued"
JOB_CLAIMED_EVENT = "job.claimed"
JOB_REQUEUED = "job.requeued"
JOB_PARKED = "job.parked"
JOB_FINISHED = "job.finished"


class JobLost(TianshuError):
    """claim 已不是当前 owner / 已被接管 / 已终结——不得继续提交。"""


class JobUnavailable(TianshuError):
    """没有可认领的作业。"""


@dataclass(frozen=True)
class JobClaim:
    """一次认领的身份。所有后续提交都必须带它，不能只带 job_id。"""

    job_id: str
    run_id: str
    owner_instance_id: str
    owner_pid: int
    claim_epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "owner_instance_id": self.owner_instance_id,
            "owner_pid": self.owner_pid,
            "claim_epoch": self.claim_epoch,
        }


@dataclass
class Job:
    job_id: str
    run_id: str
    state: str
    owner_instance_id: str | None = None
    owner_pid: int | None = None
    claim_epoch: int = 0
    claimed_at: int | None = None
    heartbeat_at: int | None = None
    deadline_at: int | None = None
    cancel_requested: bool = False
    output_ref: str | None = None
    outcome: str | None = None
    created_at: int = 0
    ended_at: int | None = None


def _now() -> int:
    return int(time.time() * 1000)


def _claim_owned(claims: JobStore, claim: JobClaim) -> bool:
    """这个 claim 现在还归本实例吗（只读判断，给"失败后归类"用）。"""
    job = claims.get(claim.job_id)
    return (
        job is not None
        and job.state == JOB_CLAIMED
        and job.claim_epoch == claim.claim_epoch
        and job.owner_instance_id == claim.owner_instance_id
        and job.owner_pid == claim.owner_pid
    )


def renew_claim(
    leases: LeaseStore,
    claims: JobStore,
    claim: JobClaim,
    *,
    ttl_ms: int = DEFAULT_TTL_MS,
) -> bool:
    """续一次 claim：租约 + 作业 deadline。

    为什么值得单独抽出来：worker 的后台心跳线程用**另一条 SQLite 连接**
    （连接不能跨线程共享），但它续的东西必须和主循环 `heartbeat_once` 完全
    一致——两套实现迟早会有一套忘了续租约，而"作业还活着、目录却是别人的"
    正是最危险的半死状态。

    返回值是"还该继续心跳吗"：
    - `True`：续上了，接着跑；
    - `False`：作业已经收尾（`done` / `cancelled`）或租约已被本实例按正常流程
      释放，心跳没有意义，该静默停下。收尾顺序是"先 `claims.finish`，再释放
      租约"，所以晚到一步的心跳看到的正是这个状态——那是**正常结束**，不是
      "我丢了 claim"。把它记成失败会让一次成功的执行以退出码 1 收场。
    - 抛 `JobLost`：真的被接管了（epoch/owner 变了、作业消失）。

    顺序是"先读一次判身份 → 再续 → 续失败时再读一次归类"：身份变了立刻抛
    （那是真的被接管），续失败时若发现"作业已不是 claimed"就算正常结束。
    只在失败路径上多读一次，成功路径仍是两次写到底。
    """
    job = claims.get(claim.job_id)
    if job is None:
        return False
    # **先判身份，后判状态**：接管之后新 owner 可能立刻把作业交还(`pending`)
    # 或挂起(`waiting`)，这两种也必须报 `JobLost`——否则"我被接管了"这件事
    # 会被静默成"正常结束"。只有 epoch/owner 没变（本实例自己收尾/暂停/被取消）
    # 才是静默。
    if job.claim_epoch != claim.claim_epoch or (
        job.owner_instance_id != claim.owner_instance_id
    ):
        raise JobLost(
            f"claim 已被接管：job={claim.job_id} epoch={claim.claim_epoch}"
            f"（当前 {job.claim_epoch}/{job.owner_instance_id}）"
        )
    if job.state != JOB_CLAIMED:
        return False

    # **先核 claim 再续租约**：两条独立的事务，顺序反了就会出现"我已经不是
    # owner，却把工作区租约又续了 30 秒"——新 owner 之后来 acquire 会撞
    # LeaseConflict，而被续的那行租约要等 TTL 才失效。claim 的核验是权威判据，
    # 所以它必须先成功。
    try:
        claims.heartbeat(claim, ttl_ms=ttl_ms)
    except TianshuError:
        if not _claim_owned(claims, claim):
            return False
        raise

    paths = leases.live_paths(claim.run_id)
    if paths:
        try:
            leases.renew([Path(p) for p in paths], claim.run_id, ttl_ms=ttl_ms)
        except TianshuError:
            # 从"读到有租约"到"去续"之间，本实例可能刚好在收尾/暂停里把租约
            # 释放了（两个线程看不到彼此的中间状态）。没有租约可续不等于租约
            # 丢了：真正的判据是执行前的 `assert_covers`。
            if not _claim_owned(claims, claim):
                return False
            if leases.live_paths(claim.run_id):
                raise
            # 剩下一种情况无法与"本实例刚好暂停/收尾释放了租约"区分：租约在
            # `live_paths` 与 `renew` 之间被别人拿走。这里不猜，交给执行前的
            # `assert_covers` 兜底——它才是"这条路径归不归我写"的判据。
    return True


class JobStore:
    def __init__(
        self,
        log: EventLog,
        *,
        identity: ProcessIdentity | None = None,
        clock=_now,
    ) -> None:
        if log.in_transaction:
            raise TianshuError("JobStore 不能在已打开的事务中构造")
        self._log = log
        self._conn = log.conn
        self._conn.row_factory = sqlite3.Row
        self._identity = identity or current_process()
        self._clock = clock
        self._conn.executescript(SCHEMA)

    @property
    def identity(self) -> ProcessIdentity:
        return self._identity

    @property
    def transaction(self):
        return self._log.transaction()

    # ---- 入队 ----

    def enqueue(self, run_id: str) -> Job:
        now = self._clock()
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        with self._log.transaction() as tx:
            existing = self._conn.execute(
                "SELECT job_id FROM jobs WHERE run_id=?"
                " AND state IN ('pending','claimed','waiting')",
                (run_id,),
            ).fetchone()
            if existing is not None:
                raise TianshuError(
                    f"run 已有未完成作业：{existing['job_id']}（run={run_id}）"
                )
            self._conn.execute(
                "INSERT INTO jobs (job_id, run_id, state, claim_epoch,"
                " cancel_requested, created_at) VALUES (?,?,'pending',0,0,?)",
                (job_id, run_id, now),
            )
            tx.append(
                run_id,
                JOB_ENQUEUED,
                {"job_id": job_id, "run_id": run_id},
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._row_to_job(row) if row else None

    def job_for_run(self, run_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, *, state: str | None = None) -> list[Job]:
        if state is None:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created_at", (state,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    # ---- 认领 ----

    def claim_next(
        self, *, ttl_ms: int = DEFAULT_TTL_MS, max_attempts: int = 16
    ) -> JobClaim | None:
        """认领一个可跑的作业。

        可认领 = `pending`，或 `claimed` 但**持有者已经不在了**：
        - deadline 过了（卡住 / 进程被挂起）；
        - 或者进程已经死了。

        第二条不能省。只看 deadline 的话，一个被 kill 的 owner 要等 30 秒 TTL
        才允许接管，用户看到的是"resume 没反应"；而 `is_live` 已经把
        "进程不在了"判成不活，认领没有理由比它保守。

        `waiting` 永远不在这里：它是"等外面的事件"（人批准 / recover / resume），
        轮询认领它只会每轮涨一次 claim_epoch，把用户刚批的审批作废。
        """
        for _ in range(max_attempts):
            # 判定与认领必须在**同一个写事务**里：`BEGIN IMMEDIATE` 一拿到写锁，
            # 别的进程就没法在"我看见它是 pending"和"我把它改成 claimed"之间
            # 插进来。分开做的话会出一个很难复现的洞——B 在旧快照上看到 pending，
            # 去认领时却从 A 刚提交的 epoch 1 上接着 +1，于是两个进程同时持
            # 同一个 Run 的 claim，各自以为自己是 owner。
            with self._log.transaction():
                # 排序按"能不能跑"优先，而不是只按创建时间：否则队列前面
                # 堆着 32 个"活着但卡住"的 claimed 作业时，后建的 pending
                # 永远扫不到（`LIMIT` 把它们挡在外面），表现为"队列卡死"。
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE state IN ('pending','claimed')"
                    " ORDER BY CASE WHEN state='pending' THEN 0 ELSE 1 END,"
                    "          created_at LIMIT ?",
                    (max_attempts * 2,),
                ).fetchall()
                for row in rows:
                    job = self._row_to_job(row)
                    if job.state == JOB_PENDING or not self.is_live(job):
                        try:
                            return self.claim(job.job_id, ttl_ms=ttl_ms)
                        except (JobLost, JobUnavailable):
                            # 同一把锁里几乎不可能被抢；防御性保留：
                            # 换下一个候选，而不是把失败当成"没有作业"。
                            continue
                return None
        return None

    def claim(
        self,
        job_id: str,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        take_over_live: bool = False,
    ) -> JobClaim:
        """认领指定作业。条件 UPDATE 保证只有一个认领者赢。

        `take_over_live=False`（默认）时**拒绝**从"仍然活着的 owner"手里接管：
        `claim_epoch` 保护的是"接管之后旧 owner 写不进状态"，但它保护不了
        "另一个 owner 正在写的时候状态被接管"——那种情况下两边都会往同一份
        游标上写。所以"能不能接管"必须在**持有写锁的事务里**重新判一次，
        而不是由调用方在事务外先看一眼。
        """
        now = self._clock()
        deadline = now + ttl_ms
        with self._log.transaction() as tx:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobUnavailable(f"job 不存在：{job_id}")
            if row["state"] in TERMINAL_JOB_STATES:
                raise JobUnavailable(f"job 已终结：{job_id}（state={row['state']}）")
            if (
                not take_over_live
                and row["state"] == JOB_CLAIMED
                and self.is_live(self._row_to_job(row))
            ):
                raise JobLost(
                    f"job 仍由活着的 owner 持有，拒绝接管：{job_id}"
                    f"（owner_pid={row['owner_pid']} epoch={row['claim_epoch']}）"
                )

            epoch = row["claim_epoch"] + 1
            cur = self._conn.execute(
                "UPDATE jobs SET state='claimed',"
                " owner_instance_id=?, owner_pid=?, claim_epoch=?,"
                " claimed_at=?, heartbeat_at=?, deadline_at=?"
                " WHERE job_id=?"
                "   AND claim_epoch=?"
                "   AND state NOT IN ('done','cancelled')",
                (
                    self._identity.instance_id,
                    self._identity.pid,
                    epoch,
                    now,
                    now,
                    deadline,
                    job_id,
                    row["claim_epoch"],
                ),
            )
            if cur.rowcount == 0:
                raise JobLost(f"job 已被别人认领：{job_id}")

            claim = JobClaim(
                job_id=job_id,
                run_id=row["run_id"],
                owner_instance_id=self._identity.instance_id,
                owner_pid=self._identity.pid,
                claim_epoch=epoch,
            )
            tx.append(
                row["run_id"],
                JOB_CLAIMED_EVENT,
                {
                    **claim.to_dict(),
                    "previous_epoch": row["claim_epoch"],
                    "deadline_at": deadline,
                },
            )
        return claim

    # ---- 续期 / 收尾 ----

    def heartbeat(self, claim: JobClaim, *, ttl_ms: int = DEFAULT_TTL_MS) -> None:
        """续 deadline。只对当前 owner 生效，否则抛 JobLost。"""
        now = self._clock()
        deadline = now + ttl_ms
        with self._log.transaction():
            cur = self._conn.execute(
                "UPDATE jobs SET heartbeat_at=?, deadline_at=?"
                " WHERE job_id=? AND owner_instance_id=? AND claim_epoch=?"
                "   AND owner_pid=? AND state='claimed'",
                (
                    now,
                    deadline,
                    claim.job_id,
                    claim.owner_instance_id,
                    claim.claim_epoch,
                    claim.owner_pid,
                ),
            )
            if cur.rowcount == 0:
                raise JobLost(
                    f"心跳失败：claim 已失效（job={claim.job_id} "
                    f"epoch={claim.claim_epoch}）"
                )

    def assert_claim(self, claim: JobClaim) -> None:
        """只读校验：这个 claim 现在还有效吗。

        给别的存储（cursor / 模型结果）用：它们写的是**这个 Run 的状态**，
        必须确认写的人还是当前 owner，否则旧 worker 会覆盖新 owner 的成果。
        """
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (claim.job_id,)
        ).fetchone()
        if row is None:
            raise JobLost(f"job 不存在：{claim.job_id}")
        if row["state"] != JOB_CLAIMED:
            raise JobLost(f"job 不在认领状态：{claim.job_id}（state={row['state']}）")
        if row["claim_epoch"] != claim.claim_epoch:
            raise JobLost(
                f"claim 已被接管：job={claim.job_id} 期望 epoch={claim.claim_epoch}，"
                f"当前 epoch={row['claim_epoch']}"
            )
        if (
            row["owner_instance_id"] != claim.owner_instance_id
            or row["owner_pid"] != claim.owner_pid
        ):
            raise JobLost(f"claim 的 owner 不一致：job={claim.job_id}")
        # claim 是"持有凭证"，但凭证值本身不该跨实例通用：别的实例即便拿到了
        # 这个对象，也不代表它就是当前 owner。跟租约一样按进程实例判。
        if claim.owner_instance_id != self._identity.instance_id:
            raise JobLost(
                f"claim 不属于本实例：job={claim.job_id}"
                f"（claim={claim.owner_instance_id} 本实例={self._identity.instance_id}）"
            )

    def finish(self, claim: JobClaim, *, outcome: str) -> Job:
        """收尾。旧 claim 一律拒绝——"活着但已过期"的 worker 就是靠这里拦住的。"""
        if outcome not in JOB_OUTCOMES:
            raise TianshuError(f"未知 outcome：{outcome}（可选 {sorted(JOB_OUTCOMES)}）")
        now = self._clock()
        with self._log.transaction() as tx:
            cur = self._conn.execute(
                "UPDATE jobs SET state=?, outcome=?, ended_at=?"
                " WHERE job_id=? AND owner_instance_id=? AND claim_epoch=?"
                "   AND owner_pid=? AND state='claimed'",
                (
                    JOB_DONE,
                    outcome,
                    now,
                    claim.job_id,
                    claim.owner_instance_id,
                    claim.claim_epoch,
                    claim.owner_pid,
                ),
            )
            if cur.rowcount == 0:
                row = self._conn.execute(
                    "SELECT claim_epoch, state FROM jobs WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()
                raise JobLost(
                    f"收尾被拒绝：claim 已失效（job={claim.job_id} "
                    f"epoch={claim.claim_epoch} 当前={row['claim_epoch'] if row else '?'} "
                    f"state={row['state'] if row else '?'}）"
                )
            tx.append(
                claim.run_id,
                JOB_FINISHED,
                {**claim.to_dict(), "outcome": outcome},
            )
        return self.get(claim.job_id)  # type: ignore[return-value]

    def requeue(self, claim: JobClaim) -> Job:
        """主动交还（暂停/安全点）。不是失败，下一次认领会拿到更高的 epoch。"""
        now = self._clock()
        with self._log.transaction() as tx:
            cur = self._conn.execute(
                "UPDATE jobs SET state='pending', owner_instance_id=NULL,"
                " owner_pid=NULL, heartbeat_at=NULL, deadline_at=NULL,"
                " claimed_at=NULL, ended_at=?"
                " WHERE job_id=? AND owner_instance_id=? AND claim_epoch=?"
                "   AND owner_pid=? AND state='claimed'",
                (
                    now,
                    claim.job_id,
                    claim.owner_instance_id,
                    claim.claim_epoch,
                    claim.owner_pid,
                ),
            )
            if cur.rowcount == 0:
                raise JobLost(f"交还被拒绝：claim 已失效（job={claim.job_id}）")
            tx.append(claim.run_id, JOB_REQUEUED, claim.to_dict())
        return self.get(claim.job_id)  # type: ignore[return-value]

    def park(self, claim: JobClaim, *, reason: str) -> Job:
        """把作业挂起：还开着，但不该被自动跑，等外面的事件叫醒它。

        `reason` 是 `waiting_approval` / `paused` / `reconciling` 这类"Run 停下
        来了、但没结束"的状态。放回 `pending` 是错的：一个轮询 worker 会立刻
        再认领、再停在同一个地方，每一轮涨一次 `claim_epoch`——用户刚批下来的
        审批会被下一轮作废，于是"批准之后永远执行不了"。留在 `claimed` 也是
        错的：进程已经要把控制权交出来了，`claimed` 对外说的是"有人正在跑"。
        """
        with self._log.transaction() as tx:
            cur = self._conn.execute(
                # `ended_at` 保持 NULL：作业没结束，只是没人该自动跑它。
                "UPDATE jobs SET state='waiting', owner_instance_id=NULL,"
                " owner_pid=NULL, heartbeat_at=NULL, deadline_at=NULL,"
                " claimed_at=NULL"
                " WHERE job_id=? AND owner_instance_id=? AND claim_epoch=?"
                "   AND owner_pid=? AND state='claimed'",
                (
                    claim.job_id,
                    claim.owner_instance_id,
                    claim.claim_epoch,
                    claim.owner_pid,
                ),
            )
            if cur.rowcount == 0:
                raise JobLost(f"挂起被拒绝：claim 已失效（job={claim.job_id}）")
            tx.append(
                claim.run_id,
                JOB_PARKED,
                {**claim.to_dict(), "reason": reason},
            )
        return self.get(claim.job_id)  # type: ignore[return-value]

    def request_cancel(self, job_id: str) -> Job:
        """记下取消请求。执行侧在安全点自己确认，不能假装瞬时停止。"""
        with self._log.transaction():
            self._conn.execute(
                "UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,)
            )
        return self.get(job_id)  # type: ignore[return-value]

    def is_live(self, job: Job) -> bool:
        """这个作业现在是否由**一个活着的 owner** 持有。

        三件事都要成立：还在 claimed 状态、deadline 没过、owner 进程还在。
        只看前两条会把"进程已经崩了但 deadline 还没到"判成"别人正在跑"，
        于是崩溃后的 Run 永远 resume 不了——它停在 RUNNING，既没 owner 在跑，
        也没有人允许接管。
        """
        if job.state != JOB_CLAIMED:
            return False
        if job.deadline_at is not None and job.deadline_at <= self._clock():
            return False
        if not job.owner_pid:
            return False
        return pid_alive(job.owner_pid)

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> Job | None:
        """由控制端直接终结作业（不经 claim）。

        为什么允许不传 claim：取消是**人对 Run 的决定**，不是 owner 的自我收尾。
        人可能在一个 owner 正在跑的 Run 上按取消，此时人手里本来就没有 claim。
        这里不做"看心跳猜死活"，只做一条条件 UPDATE：谁来都行，但只有第一个
        把 pending/claimed 改成 cancelled 的生效；之后旧 owner 的心跳/收尾
        会因为 `state != 'claimed'` 一致地失败（`assert_claim` / `heartbeat` /
        `finish` 都要求 claimed），不会出现"取消之后 owner 又把它写回成功"。
        """
        with self._log.transaction() as tx:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            now = self._clock()
            cur = self._conn.execute(
                "UPDATE jobs SET state='cancelled', outcome='cancelled',"
                " cancel_requested=1, ended_at=?"
                " WHERE job_id=? AND state IN ('pending','claimed','waiting')",
                (now, job_id),
            )
            if cur.rowcount == 0:
                return self.get(job_id)
            tx.append(
                row["run_id"],
                JOB_FINISHED,
                {
                    "job_id": job_id,
                    "run_id": row["run_id"],
                    "claim_epoch": row["claim_epoch"],
                    "owner_instance_id": row["owner_instance_id"],
                    "owner_pid": row["owner_pid"],
                    "outcome": "cancelled",
                    "reason": reason,
                    "by": "control",
                },
            )
        return self.get(job_id)

    # ---- 行映射 ----

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            run_id=row["run_id"],
            state=row["state"],
            owner_instance_id=row["owner_instance_id"],
            owner_pid=row["owner_pid"],
            claim_epoch=row["claim_epoch"],
            claimed_at=row["claimed_at"],
            heartbeat_at=row["heartbeat_at"],
            deadline_at=row["deadline_at"],
            cancel_requested=bool(row["cancel_requested"]),
            output_ref=row["output_ref"],
            outcome=row["outcome"] if "outcome" in row.keys() else None,
            created_at=row["created_at"],
            ended_at=row["ended_at"],
        )


__all__ = [
    "JOB_CANCELLED",
    "JOB_WAITING",
    "renew_claim",
    "JOB_OUTCOMES",
    "JOB_CLAIMED",
    "JOB_DONE",
    "JOB_PENDING",
    "TERMINAL_JOB_STATES",
    "Job",
    "JobClaim",
    "JobLost",
    "JobStore",
    "JobUnavailable",
]