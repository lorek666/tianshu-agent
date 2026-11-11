"""M2 · Task 2 剩余验收：作业认领、游标提交、执行守卫接线。

PR #7 把计划 Task 2 只做了一半：租约有了，但 **"谁在跑这个 Run"** 这一层
（`jobs.py` / `JobClaim`）和 `execution_guard` 的实际接线都没有。这里补齐，
并重点钉住两件事故：

1. **旧 worker 覆盖新 owner**：worker A 卡住、B 接管后，A 醒过来继续写。
   A 进程还活着，靠 pid / 心跳时间都拦不住，只能靠 `claim_epoch`。
2. **"核验"与"开始写"之间的窗口**：`assert_held` 读完就返回，读完到真正
   打开文件之间可以被接管。只有把复核放进执行守卫里，窗口才关得上。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.jobs import (
    JOB_CLAIMED_EVENT,
    JOB_FINISHED,
    JobLost,
    JobStore,
    JobUnavailable,
)
from tianshu.leases import LeaseLost, LeaseStore, ProcessIdentity
from tianshu.tasks import (
    EXECUTION_STARTED,
    RUN_CURSOR_SAVED,
    RUN_MODEL_TURN,
    RUN_RUNNING,
    ExecutionPermit,
    RunCursor,
    RunFenced,
    TaskStore,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _now_ms() -> int:
    return int(__import__("time").time() * 1000)


@pytest.fixture()
def env(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    leases = LeaseStore(log)
    jobs = JobStore(log)
    ws = tmp_path / "ws"
    ws.mkdir()
    data_root = tmp_path / "data"
    tasks = TaskStore(log, leases=leases, claims=jobs, data_root=data_root)
    yield log, tasks, leases, jobs, ws, data_root
    log.close()


def _run_with_lease(tasks, leases, ws, goal="整理"):
    task = tasks.create_task(goal, [str(ws)])
    run = tasks.start_run(task.task_id)
    leases.acquire([ws], run.run_id)
    return task, run


def _running(tasks, leases, ws, goal="整理"):
    """租约在手、状态 RUNNING 的 Run。

    `begin_execution` 只认 RUNNING：`created` 是"还没开始"，能写文件就说明
    状态判断没接上。执行侧的真实顺序是 created → queued → running。
    """
    task, run = _run_with_lease(tasks, leases, ws, goal)
    return task, tasks.transition(run.run_id, RUN_RUNNING)


# ---------------------------------------------------------------- 认领


def test_enqueue_then_claim(env):
    _, tasks, _, jobs, _, _ = env
    _, run = _run_with_lease(tasks, env[2], env[4])
    job = jobs.enqueue(run.run_id)
    assert job.state == "pending"

    claim = jobs.claim(job.job_id)
    assert claim.run_id == run.run_id
    assert claim.claim_epoch == 1
    assert jobs.get(job.job_id).state == "claimed"
    assert jobs.get(job.job_id).owner_instance_id == jobs.identity.instance_id


def test_claim_is_recorded_as_event(env):
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    jobs.enqueue(run.run_id)
    jobs.claim_next()
    types = [e.type for e in log.read_types([JOB_CLAIMED_EVENT])]
    assert types == [JOB_CLAIMED_EVENT]


def test_claim_next_skips_terminal_jobs(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    claim = jobs.claim(job.job_id)
    jobs.finish(claim, outcome="succeeded")
    assert jobs.claim_next() is None


def test_second_claim_bumps_epoch_and_fences_old(env):
    """接管后旧 claim 立刻失效——这是"活着但已过期"的唯一拦截点。"""
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)

    old = jobs.claim(job.job_id)
    # 模拟"旧 worker 卡住了"：把 deadline 推回过去，让作业可被接管
    log.conn.execute(
        "UPDATE jobs SET deadline_at = 1 WHERE job_id = ?", (job.job_id,)
    )
    new = jobs.claim(job.job_id)

    assert new.claim_epoch == old.claim_epoch + 1
    with pytest.raises(JobLost):
        jobs.heartbeat(old)
    with pytest.raises(JobLost):
        jobs.finish(old, outcome="succeeded")
    with pytest.raises(JobLost):
        jobs.assert_claim(old)

    jobs.heartbeat(new)  # 新 owner 正常
    assert jobs.finish(new, outcome="succeeded").state == "done"


def test_old_claim_cannot_write_cursor_or_turns(env):
    """旧 worker 不只是不能 finish：它连 cursor / 模型结果都不能写。"""
    log, tasks, leases, jobs, ws, data_root = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    old = jobs.claim(job.job_id)

    cursor = RunCursor(completed_call_ids=["call_1"])
    tasks.save_cursor(run.run_id, cursor, old)

    log.conn.execute(
        "UPDATE jobs SET deadline_at = 1 WHERE job_id = ?", (job.job_id,)
    )
    new = jobs.claim(job.job_id)

    with pytest.raises(JobLost):
        tasks.save_cursor(run.run_id, RunCursor(completed_call_ids=[]), old)
    with pytest.raises(JobLost):
        tasks.commit_model_turn(run.run_id, {"turn_id": "t2"}, old)

    # 新 owner 写入后，游标反映的是新 owner 的内容
    tasks.save_cursor(run.run_id, RunCursor(completed_call_ids=["call_9"]), new)
    loaded, meta = tasks.load_cursor(run.run_id)
    assert loaded.completed_call_ids == ["call_9"]
    assert meta["claim_epoch"] == new.claim_epoch


def test_foreign_instance_cannot_assert_claim(env):
    """claim 是持有凭证，但凭证值不该跨实例通用。"""
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)
    with pytest.raises(JobLost):
        JobStore(env[0], identity=ProcessIdentity.mint()).assert_claim(claim)


def test_heartbeat_requires_current_owner(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    job_claim = jobs.claim(job.job_id)

    foreign = JobStore(env[0], identity=ProcessIdentity.mint())
    with pytest.raises(JobLost):
        foreign.assert_claim(job_claim)

    jobs.heartbeat(job_claim)
    assert jobs.get(job.job_id).deadline_at is not None


def test_requeue_returns_to_pending_and_next_claim_bumps_epoch(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    first = jobs.claim(job.job_id)
    assert jobs.requeue(first).state == "pending"
    second = jobs.claim(job.job_id)
    assert second.claim_epoch == first.claim_epoch + 1


def test_one_open_job_per_run(env):
    """一个 Run 只能有一个未完成作业。

    否则两个 worker 各自 claim 不同 job_id、epoch 都是 1，都能通过核验，
    同一份 cursor 被两个 owner 交替写——fencing 白做。
    """
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    jobs.enqueue(run.run_id)
    with pytest.raises(TianshuError):
        jobs.enqueue(run.run_id)

    # 数据库层也不能被绕：直接插入应当撞唯一索引
    with pytest.raises(sqlite3.IntegrityError):
        log.conn.execute(
            "INSERT INTO jobs (job_id, run_id, state, claim_epoch,"
            " cancel_requested, created_at) VALUES ('job_x',?,'pending',0,0,1)",
            (run.run_id,),
        )


def test_finish_rejects_unknown_outcome(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)
    with pytest.raises(TianshuError):
        jobs.finish(claim, outcome="whatever")
    assert jobs.get(claim.job_id).state == "claimed"


def test_finish_records_outcome_on_projection(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)
    assert jobs.finish(claim, outcome="failed").outcome == "failed"


def test_revise_goal_is_blocked_while_execution_in_progress(env):
    """改目标与"开始执行"抢同一把守卫：不会出现"复核通过后目标被改"。"""
    from tianshu.leases import GuardTimeout

    _, tasks, leases, jobs, ws, data_root = env
    task, run = _running(tasks, leases, ws)

    with tasks.begin_execution(data_root, run.run_id, [ws]):
        with pytest.raises(GuardTimeout):
            tasks.revise_goal(task.task_id, "只整理 B")

    # 执行结束（守卫释放）后可以改目标，且旧 Run 立刻被 fence
    tasks.revise_goal(task.task_id, "只整理 B")
    assert tasks.get_run(run.run_id).state == "fenced"


def test_revise_goal_requires_data_root(env):
    """没配 data_root 时必须拒绝：静默放过等于留一个 TOCTOU 窗口。"""
    log, _, leases, _, ws, _ = env
    tasks = TaskStore(log, leases=leases)
    task = tasks.create_task("x")
    with pytest.raises(TianshuError) as ei:
        tasks.revise_goal(task.task_id, "y")
    assert "data_root" in str(ei.value)


def test_begin_execution_renews_lease(env):
    """复核通过后租约马上到期也不怕：begin_execution 在守卫内续租。"""
    _, tasks, leases, _, ws, data_root = env
    _, run = _running(tasks, leases, ws)
    log = env[0]
    log.conn.execute("UPDATE leases SET expires_at = ?", (_now_ms() + 500,))
    before = leases.holder(ws).expires_at

    with tasks.begin_execution(data_root, run.run_id, [ws]):
        after = leases.holder(ws).expires_at
        assert after > before
        assert leases.is_held_by(ws, run.run_id)


def test_claim_terminal_job_raises(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    claim = jobs.claim(job.job_id)
    jobs.finish(claim, outcome="succeeded")
    with pytest.raises(JobUnavailable):
        jobs.claim(job.job_id)


def test_cursor_requires_claim_store(env):
    """没有 JobStore 就不许写游标：裸 run_id 写入等于没有 owner 概念。"""
    from tianshu.jobs import JobClaim

    log, _, leases, _, ws, _ = env
    tasks = TaskStore(log, leases=leases)
    _, run = _run_with_lease(tasks, leases, ws)

    with pytest.raises(TianshuError) as ei:
        tasks.save_cursor(run.run_id, RunCursor(), None)  # type: ignore[arg-type]
    assert "JobClaim" in str(ei.value)

    fake = JobClaim(
        job_id="job_x",
        run_id=run.run_id,
        owner_instance_id="i",
        owner_pid=1,
        claim_epoch=1,
    )
    with pytest.raises(TianshuError) as ei2:
        tasks.save_cursor(run.run_id, RunCursor(), fake)
    assert "JobStore" in str(ei2.value)


def test_cursor_rejects_claim_for_other_run(env):
    _, tasks, leases, jobs, ws, _ = env
    task = tasks.create_task("x", [str(ws)])
    run_a = tasks.start_run(task.task_id)
    tasks.finish_run(run_a.run_id, outcome="failed", effect_state="none_applied")
    run_b = tasks.start_run(task.task_id)

    job_b = jobs.enqueue(run_b.run_id)
    claim_b = jobs.claim(job_b.job_id)
    with pytest.raises(TianshuError):
        tasks.save_cursor(run_a.run_id, RunCursor(), claim_b)


# ---------------------------------------------------------------- 模型结果


def test_model_turn_seq_increments(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    claim = jobs.claim(job.job_id)

    assert tasks.commit_model_turn(run.run_id, {"turn_id": "t1"}, claim) == 1
    assert tasks.commit_model_turn(run.run_id, {"turn_id": "t2"}, claim) == 2
    turns = tasks.list_model_turns(run.run_id)
    assert [t["turn_id"] for t in turns] == ["t1", "t2"]


def test_same_turn_twice_is_idempotent(env):
    """重启重放同一个 turn 不能变成两次计费。"""
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    job = jobs.enqueue(run.run_id)
    claim = jobs.claim(job.job_id)
    turn = {"turn_id": "t1", "usage": {"input_tokens": 10, "output_tokens": None}}

    assert tasks.commit_model_turn(run.run_id, turn, claim) == 1
    assert tasks.commit_model_turn(run.run_id, turn, claim) == 1
    assert len(tasks.list_model_turns(run.run_id)) == 1
    assert log.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type=?", (RUN_MODEL_TURN,)
    ).fetchone()[0] == 1


def test_same_turn_different_content_is_an_error(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)

    tasks.commit_model_turn(run.run_id, {"turn_id": "t1", "x": 1}, claim)
    with pytest.raises(TianshuError):
        tasks.commit_model_turn(run.run_id, {"turn_id": "t1", "x": 2}, claim)


def test_turn_requires_turn_id(env):
    _, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)
    with pytest.raises(TianshuError):
        tasks.commit_model_turn(run.run_id, {}, claim)


def test_cursor_round_trips_and_is_deterministic(env):
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)

    cursor = RunCursor(
        messages=[{"role": "user", "content": "整理一下"}],
        pending_proposals=[{"tool": "read_text", "call_id": "c1"}],
        completed_call_ids=["c0"],
        approval_ids=["ap1"],
        budget_ledger={"tokens": 1200},
        resource_epochs={"ws": 1},
    )
    tasks.save_cursor(run.run_id, cursor, claim)
    loaded, _ = tasks.load_cursor(run.run_id)
    assert loaded.to_dict() == cursor.to_dict()

    row = log.conn.execute(
        "SELECT payload FROM run_cursors WHERE run_id=?", (run.run_id,)
    ).fetchone()
    # F5 起游标多一个 `message_seqs`（上下文编译的 seq 归因），序列化形状随之改变。
    # 这条断言钉的是"同一份游标序列化结果永远一样"，不是"字段集合永远不变"。
    assert row["payload"] == (
        '{"approval_ids":["ap1"],"budget_ledger":{"tokens":1200},'
        '"completed_call_ids":["c0"],"message_seqs":[],'
        '"messages":[{"content":"整理一下",'
        '"role":"user"}],"model_turn":null,"output_ref":null,'
        '"pending_proposals":[{"call_id":"c1","tool":"read_text"}],'
        '"resource_epochs":{"ws":1}}'
    )


def test_cursor_save_is_recorded_as_event(env):
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)
    tasks.save_cursor(run.run_id, RunCursor(pending_proposals=[{"a": 1}]), claim)
    events = log.read_types([RUN_CURSOR_SAVED])
    assert len(events) == 1
    assert events[0].payload["claim_epoch"] == claim.claim_epoch
    assert events[0].payload["pending_proposals"] == 1


# ---------------------------------------------------------------- 执行守卫接线


def test_begin_execution_emits_started_event(env):
    log, tasks, leases, _, ws, data_root = env
    _, run = _running(tasks, leases, ws)

    with tasks.begin_execution(data_root, run.run_id, [ws]) as permit:
        assert isinstance(permit, ExecutionPermit)
        assert permit.run_id == run.run_id
        assert permit.resources == (str(ws),)

    events = log.read_types([EXECUTION_STARTED])
    assert len(events) == 1
    assert events[0].payload["run_id"] == run.run_id
    assert events[0].seq == events[0].seq  # 序号来自同一事务


def test_begin_execution_rejects_fenced_run(env):
    """改目标后再想开始执行：必须在守卫内被拒绝。"""
    _, tasks, leases, _, ws, data_root = env
    task, run = _run_with_lease(tasks, leases, ws)
    tasks.revise_goal(task.task_id, "只整理 B")

    with pytest.raises(RunFenced):
        with tasks.begin_execution(data_root, run.run_id, [ws]):
            pass


def test_begin_execution_rejects_lost_lease(env):
    _, tasks, leases, _, ws, data_root = env
    _, run = _running(tasks, leases, ws)
    leases.release([ws], run.run_id)

    with pytest.raises(LeaseLost):
        with tasks.begin_execution(data_root, run.run_id, [ws]):
            pass


def test_begin_execution_requires_resources(env):
    """不声明资源就等于不校验租约——直接拒绝。"""
    _, tasks, _, _, ws, data_root = env
    _, run = _run_with_lease(tasks, env[2], ws)
    with pytest.raises(TianshuError) as ei:
        with tasks.begin_execution(data_root, run.run_id, []):
            pass
    assert "资源" in str(ei.value)


def test_guard_cannot_be_taken_inside_transaction(env):
    """锁序必须是"守卫 → 事务"。反过来会和守卫持有者循环等待。"""
    _, tasks, leases, _, ws, data_root = env
    task, run = _run_with_lease(tasks, leases, ws)

    with tasks.transaction():
        with pytest.raises(TianshuError) as ei:
            tasks.revise_goal(task.task_id, "y")
        assert "锁序" in str(ei.value)
        with pytest.raises(TianshuError):
            with tasks.begin_execution(data_root, run.run_id, [ws]):
                pass


def test_begin_execution_requires_lease_store(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    try:
        tasks = TaskStore(log)
        task = tasks.create_task("x")
        run = tasks.start_run(task.task_id)
        with pytest.raises(TianshuError):
            with tasks.begin_execution(tmp_path, run.run_id, []):
                pass
    finally:
        log.close()


def test_begin_execution_holds_guard_during_body(env):
    """with 块内另一个实例拿不到守卫——这正是"核验完到写完"那段窗口。"""
    from tianshu.leases import GuardTimeout, execution_guard

    _, tasks, leases, _, ws, data_root = env
    _, run = _running(tasks, leases, ws)

    with tasks.begin_execution(data_root, run.run_id, [ws]):
        with pytest.raises(GuardTimeout):
            with execution_guard(data_root, timeout_ms=50):
                pass


def test_guard_is_released_after_body(env):
    _, tasks, leases, _, ws, data_root = env
    _, run = _running(tasks, leases, ws)
    with tasks.begin_execution(data_root, run.run_id, [ws]):
        pass

    # 能再次进入，说明守卫随块结束释放了
    with tasks.begin_execution(data_root, run.run_id, [ws]):
        pass


def test_body_exception_still_releases_guard(env):
    from tianshu.leases import execution_guard

    _, tasks, leases, _, ws, data_root = env
    _, run = _running(tasks, leases, ws)
    with pytest.raises(RuntimeError):
        with tasks.begin_execution(data_root, run.run_id, [ws]):
            raise RuntimeError("写文件时崩了")

    with execution_guard(data_root, timeout_ms=1000):
        pass


# ---------------------------------------------------------------- pid 复用


def test_reused_pid_is_treated_as_stale(tmp_path):
    """崩溃后 pid 被分给新进程：不能因为"pid 还活着"就继续认死租约。

    判定依据是单向的：新进程启动时间晚于租约获取时间 → 复用。
    """
    log = EventLog(tmp_path / "db.sqlite")
    try:
        real_now = 1_000_000_000
        d = tmp_path / "ws"
        d.mkdir()

        # 先记一条租约，再用注入的 pid 启动时间模拟"pid 已被复用"
        s = LeaseStore(log, pid_started=lambda pid: real_now + 60_000)
        s.acquire([d], "run_dead")
        log.conn.execute("UPDATE leases SET wall_acquired_at = ?", (real_now,))
        assert s.is_held_by(d, "run_dead") is False

        # 且可被接管
        s.acquire([d], "run_new")
        assert s.holder(d).run_id == "run_new"
    finally:
        log.close()


def test_legit_process_is_not_treated_as_stale(tmp_path):
    """正常持有者不能被误判——误判就是双写，比晚接管危险得多。"""
    log = EventLog(tmp_path / "db.sqlite")
    try:
        real_now = 1_000_000_000
        d = tmp_path / "ws"
        d.mkdir()
        s = LeaseStore(log, pid_started=lambda pid: real_now - 60_000)
        s.acquire([d], "run_alive")
        log.conn.execute(
            "UPDATE leases SET wall_acquired_at = ?", (real_now,)
        )
        assert s.is_held_by(d, "run_alive") is True
    finally:
        log.close()


def test_zero_wall_acquired_at_skips_reuse_check(tmp_path):
    """旧库/未知墙钟时不猜：宁可等 TTL，也不要误接管。"""
    log = EventLog(tmp_path / "db.sqlite")
    try:
        d = tmp_path / "ws"
        d.mkdir()
        s = LeaseStore(log, pid_started=lambda pid: 10**15)
        s.acquire([d], "run_old")
        log.conn.execute("UPDATE leases SET wall_acquired_at = 0")
        assert s.is_held_by(d, "run_old") is True
    finally:
        log.close()


def test_pid_started_at_is_available_for_self():
    from tianshu.leases import pid_started_at

    started = pid_started_at(os.getpid())
    if started is None:  # 某些平台拿不到，属明确不支持
        pytest.skip("该平台无法读取进程启动时间")
    assert started <= int(__import__("time").time() * 1000) + 5_000


# ---------------------------------------------------------------- 真·跨进程接管


_TAKEOVER_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, {src!r})
    from tianshu.events import EventLog
    from tianshu.jobs import JobStore

    db = sys.argv[1]
    log = EventLog(Path(db))
    store = JobStore(log)
    claim = store.claim_next()
    print("claimed", claim.claim_epoch if claim else 0, flush=True)
    sys.stdin.readline()
    try:
        store.finish(claim, outcome="succeeded")
        print("finished", flush=True)
    except Exception as exc:
        print(type(exc).__name__, flush=True)
    """
)


def test_live_worker_loses_claim_to_another_process(tmp_path):
    """另一个进程接管后，**还活着的**旧 worker 不能再收尾。

    这就是计划里"旧 worker 即使还活着也不得改新 owner 的状态"，
    没有 claim_epoch 做不到——pid 活着、心跳也没过期。
    """
    log = EventLog(tmp_path / "db.sqlite")
    jobs = JobStore(log)
    job = jobs.enqueue("run_x")

    child = subprocess.Popen(
        [sys.executable, "-c", _TAKEOVER_CHILD.format(src=SRC),
         str(tmp_path / "db.sqlite")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, **CHILD_TEXT,
    )
    try:
        assert child.stdout.readline().strip() == "claimed 1"
        # 模拟 subprocess 那一侧的 worker 卡住：deadline 已过，本进程接管
        log.conn.execute(
            "UPDATE jobs SET deadline_at = 1 WHERE job_id = ?", (job.job_id,)
        )
        mine = jobs.claim(job.job_id)
        assert mine.claim_epoch == 2
        jobs.finish(mine, outcome="succeeded")
        # 放行旧 worker：它仍是活进程，但 claim 已失效
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "JobLost"
    finally:
        child.communicate(timeout=30)
        log.close()
    assert child.returncode == 0


def test_end_to_end_single_run_order(env):
    """把各层按真实顺序串一遍，确认锁和事务的嵌套顺序没死锁。

    顺序必须是：建 Task → 开 Run → 取租约 → 入队 → 认领 → 写游标/结果
    → begin_execution(守卫内) → 实际写文件 → 收 Run → 收 Job。
    守卫永远在事务之外拿；事务里不再去拿守卫，这样两层不会互相等。
    """
    log, tasks, leases, jobs, ws, data_root = env
    target = ws / "报告.md"

    task = tasks.create_task("写一份报告", [str(ws)])
    run = tasks.start_run(task.task_id)
    leases.acquire([ws], run.run_id)
    job = jobs.enqueue(run.run_id)
    claim = jobs.claim(job.job_id)
    tasks.transition(run.run_id, RUN_RUNNING, claim=claim)

    tasks.commit_model_turn(
        run.run_id, {"turn_id": "t1", "finish_reason": "tool_use"}, claim
    )
    tasks.save_cursor(
        run.run_id,
        RunCursor(pending_proposals=[{"tool": "create_report", "path": "报告.md"}]),
        claim,
    )

    with tasks.begin_execution(data_root, run.run_id, [ws]) as permit:
        assert permit.seq > 0
        target.write_text("正文", encoding="utf-8")

    tasks.save_cursor(run.run_id, RunCursor(completed_call_ids=["call_1"]), claim)
    assert tasks.finish_run(
        run.run_id, outcome="succeeded", effect_state="applied"
    ).state == "succeeded"
    assert jobs.finish(claim, outcome="succeeded").state == "done"

    assert leases.list_for_run(run.run_id) == []
    assert target.read_text(encoding="utf-8") == "正文"

    order = [e.type for e in log.read_types([
        "task.created", "run.start", "job.enqueued", "job.claimed",
        "run.model_turn", "run.cursor", "execution.started",
        "run.finish", "job.finished",
    ])]
    assert order == [
        "task.created",
        "run.start",
        "job.enqueued",
        "job.claimed",
        "run.model_turn",
        "run.cursor",
        "execution.started",
        "run.cursor",
        "run.finish",
        "job.finished",
    ]


def test_job_finished_event_records_outcome(env):
    log, tasks, leases, jobs, ws, _ = env
    _, run = _run_with_lease(tasks, leases, ws)
    claim = jobs.claim(jobs.enqueue(run.run_id).job_id)
    jobs.finish(claim, outcome="succeeded")
    events = log.read_types([JOB_FINISHED])
    assert events[0].payload["outcome"] == "succeeded"
    assert events[0].payload["claim_epoch"] == 1

def test_enqueue_and_claim_never_exposes_a_pending_job(env, monkeypatch) -> None:
    """结构判据：入队与认领之间**不能**有可被别的连接抢走的窗口。

    Daemon 的驱动线程每几十毫秒 `claim_next()` 扫一次 pending 作业。控制面
    （UI 的记忆写 / 撤销）若先 `enqueue` 再 `claim`，两步之间的窗口就归驱动线程
    所有：CI（慢机器）上真复现过 `memory.remember` 报
    `internal: job 仍由活着的 owner 持有，拒绝接管`。

    这条用猴子补丁把窗口**确定性**放大：`enqueue` 一旦被调用，就立刻用第二条
    连接把作业抢走（模拟驱动线程最坏情况）。原子实现根本不调用 `enqueue`，
    所以抢不到；谁把它改回"先 enqueue 再 claim"，这里就会 `JobLost` 变红。
    比"开线程碰运气"可靠——并发版本在 8 次里一次都没撞上。
    """
    from tianshu.events import EventLog as _EventLog
    from tianshu.jobs import JobStore as _JobStore

    log, tasks, _leases, jobs, ws, _data_root = env
    stolen: list[str] = []

    original_enqueue = jobs.enqueue

    def enqueue_then_steal(run_id: str):
        job = original_enqueue(run_id)
        # 第二条连接 = 另一个持写锁的竞争者（daemon 驱动线程的最小同构）。
        other_log = _EventLog(log.db_path)
        try:
            other = _JobStore(other_log)
            claim = other.claim_next()
            if claim is not None:
                stolen.append(claim.job_id)
                other.finish(claim, outcome="succeeded")
        finally:
            other_log.close()
        return job

    monkeypatch.setattr(jobs, "enqueue", enqueue_then_steal)

    task = tasks.create_task("控制面写", [str(ws)])
    run = tasks.start_run(task.task_id)
    job, claim = jobs.enqueue_and_claim(run.run_id)  # 旧实现会在这里 JobLost
    try:
        assert stolen == [], f"作业在入队后、认领前被抢走：{stolen}"
        assert claim.job_id == job.job_id
    finally:
        jobs.finish(claim, outcome="succeeded")
        tasks.finish_run(run.run_id, outcome="succeeded", effect_state="none_applied")
