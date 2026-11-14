"""M2 · PR A 复审修复验收：可组合事务。

PR #7 的自查漏了一点：事件日志、Task/Run、租约各自 `BEGIN IMMEDIATE`，
组合起来必然撞上 SQLite 的「cannot start a transaction within a transaction」。
派发阶段（PR B）必须在一个事务里同时写审批消费、租约、事件和 intent，
所以事务入口必须先能组合，否则后面只能靠"写完再补"来假装原子。
"""

from __future__ import annotations

import sqlite3

import pytest
from support import effects_admission

from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.leases import LeaseStore
from tianshu.tasks import TaskStore


@pytest.fixture()
def env(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    leases = LeaseStore(log)
    tasks = TaskStore(
        log,
        leases=leases,
        data_root=tmp_path,
        disk_admission=effects_admission(
            log, snapshots=None, memory=None, contents=None, data_root=tmp_path
        ),
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    yield log, tasks, leases, ws
    log.close()


def test_events_and_projection_roll_back_together(env):
    """同一事务里写投影和事件，抛错后两者都不能留下记录。"""
    log, tasks, _, _ = env

    with pytest.raises(RuntimeError):
        with log.transaction() as tx:
            tasks.create_task("会被回滚的任务")
            tx.append("", "run.start", {"x": 1}, legacy_write=True)
            raise RuntimeError("boom")

    assert log.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_connection_usable_after_rollback(env):
    log, tasks, _, _ = env

    with pytest.raises(RuntimeError):
        with log.transaction():
            tasks.create_task("回滚")
            raise RuntimeError("boom")

    t = tasks.create_task("回滚之后还能写")
    assert tasks.get_task(t.task_id) is not None


def test_stores_are_composable_in_one_transaction(env):
    """租约 + Task + Run + 事件一次提交——这正是 PR B 派发需要的形状。"""
    log, tasks, leases, ws = env

    with log.transaction() as tx:
        task = tasks.create_task("组合事务")
        run = tasks.start_run(task.task_id)
        lease_set = leases.acquire([ws], run.run_id)
        tx.append(run.run_id, "model.intent", {"ok": True}, legacy_write=True)

    assert lease_set.resource_keys
    assert tasks.get_task(task.task_id).active_run_id == run.run_id
    assert log.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type='model.intent'"
    ).fetchone()[0] == 1


def test_nested_begin_is_never_issued(env):
    """显式嵌套必须复用外层事务，不能各自 BEGIN。"""
    log, tasks, leases, ws = env

    with log.transaction():
        task = tasks.create_task("嵌套")
        with log.transaction():
            run = tasks.start_run(task.task_id)
            with log.transaction():
                leases.acquire([ws], run.run_id)

    assert tasks.get_task(task.task_id).active_run_id == run.run_id
    assert leases.holder(ws) is not None


def test_failed_nested_store_rolls_back_outer(env):
    """内层抛错必须让外层整体回滚，不能留下半份状态。"""
    log, tasks, leases, ws = env

    with pytest.raises(TianshuError):
        with log.transaction():
            tasks.create_task("半份状态")
            tasks.revise_goal("task_不存在", "x")

    assert log.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert leases.list_all() == []


def test_store_cannot_be_built_inside_open_transaction(env):
    """executescript 会隐式 COMMIT：在事务里构造 store 等于把半份状态提交出去。"""
    log, _, _, _ = env
    from tianshu.leases import LeaseStore

    with log.transaction():
        with pytest.raises(TianshuError):
            TaskStore(log)
        with pytest.raises(TianshuError):
            LeaseStore(log)


def test_transaction_rejects_second_writer_without_guard(env, tmp_path):
    """事务是独占的：BEGIN IMMEDIATE 会在另一个连接上阻塞/失败，而不是静默并发。"""
    log, _, _, _ = env
    other = sqlite3.connect(tmp_path / "db.sqlite", isolation_level=None, timeout=0.05)
    try:
        with log.transaction():
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
                other.execute("COMMIT")
    finally:
        other.close()