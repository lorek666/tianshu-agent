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
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .errors import TianshuError
from .events import EventLog
from .leases import DEFAULT_TTL_MS, ProcessIdentity, current_process

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_open_per_run
    ON jobs(run_id) WHERE state IN ('pending','claimed');
"""

JOB_PENDING = "pending"
JOB_CLAIMED = "claimed"
JOB_DONE = "done"
JOB_CANCELLED = "cancelled"

TERMINAL_JOB_STATES = {JOB_DONE, JOB_CANCELLED}
JOB_OUTCOMES = {"succeeded", "failed", "cancelled"}

JOB_ENQUEUED = "job.enqueued"
JOB_CLAIMED_EVENT = "job.claimed"
JOB_REQUEUED = "job.requeued"
JOB_FINISHED = "job.finished"
JOB_LOST = "job.lost"


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
                " AND state IN ('pending','claimed')",
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

        可认领 = `pending`，或 `claimed` 但已过 deadline（持有者可能死了，
        也可能只是卡住——两种情况都由 claim_epoch 兜住：接管后旧 claim 失效）。
        """
        for _ in range(max_attempts):
            now = self._clock()
            row = self._conn.execute(
                "SELECT * FROM jobs"
                " WHERE state='pending'"
                "    OR (state='claimed' AND (deadline_at IS NULL OR deadline_at <= ?))"
                " ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            try:
                return self.claim(row["job_id"], ttl_ms=ttl_ms)
            except JobLost:
                # 竞态：别人先认领了。继续找下一个，而不是直接把失败当成"没有作业"。
                continue
        return None

    def claim(self, job_id: str, *, ttl_ms: int = DEFAULT_TTL_MS) -> JobClaim:
        """认领指定作业。条件 UPDATE 保证只有一个认领者赢。"""
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

    def request_cancel(self, job_id: str) -> Job:
        """记下取消请求。执行侧在安全点自己确认，不能假装瞬时停止。"""
        with self._log.transaction():
            self._conn.execute(
                "UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,)
            )
        return self.get(job_id)  # type: ignore[return-value]

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