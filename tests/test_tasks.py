"""M2 · Task 1 验收：Task/Run 与目标版本（fencing）。

要证明的是：**用户中途改主意，旧目标是真失效，不只是"新目标覆盖了旧目标"。**
"""

from __future__ import annotations

import sqlite3

import pytest

from tianshu.errors import TianshuError
from tianshu.tasks import RunFenced, TaskStore


@pytest.fixture()
def store(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite", isolation_level=None)
    conn.row_factory = sqlite3.Row
    s = TaskStore(conn)
    yield s
    conn.close()


def test_create_task_defaults(store):
    t = store.create_task("整理报告", ["/tmp/reports"])
    assert t.goal_revision == 1
    assert t.lease_epoch == 1
    assert t.active_run_id is None
    assert t.workspaces == ["/tmp/reports"]
    assert t.status == "open"


def test_run_binds_current_revision(store):
    t = store.create_task("整理报告")
    r = store.start_run(t.task_id)
    assert r.goal_revision == 1
    assert r.lease_epoch == 1
    assert store.get_task(t.task_id).active_run_id == r.run_id


def test_finish_run_clears_active(store):
    t = store.create_task("整理报告")
    r = store.start_run(t.task_id)
    done = store.finish_run(r.run_id, outcome="succeeded", effect_state="applied")
    assert done.state == "succeeded"
    assert store.get_task(t.task_id).active_run_id is None


def test_revise_goal_fences_running_run(store):
    t = store.create_task("整理 A 目录")
    r = store.start_run(t.task_id)

    t2 = store.revise_goal(t.task_id, "只整理 B 目录")
    assert t2.goal_revision == 2
    assert t2.lease_epoch == 2
    assert store.get_run(r.run_id).state == "fenced"
    assert store.is_fenced(r.run_id), "旧 Run 必须被 fence"


def test_fenced_run_cannot_report_success(store):
    """旧目标下未完成的 Run 不能提交成功——否则旧目标会"复活"。"""
    t = store.create_task("整理 A")
    r = store.start_run(t.task_id)
    store.revise_goal(t.task_id, "只整理 B")

    with pytest.raises(RunFenced):
        store.finish_run(r.run_id, outcome="succeeded", effect_state="applied")


def test_new_run_uses_new_revision(store):
    t = store.create_task("整理 A")
    store.start_run(t.task_id)
    store.revise_goal(t.task_id, "只整理 B")

    r2 = store.start_run(t.task_id)
    assert r2.goal_revision == 2
    assert r2.lease_epoch == 2
    assert not store.is_fenced(r2.run_id)


def test_history_is_preserved(store):
    """改目标不覆盖历史：先失败的 Run 与新 Run 都要在。"""
    t = store.create_task("整理 A")
    r1 = store.start_run(t.task_id)
    store.finish_run(r1.run_id, outcome="failed", effect_state="none_applied")

    r2 = store.start_run(t.task_id)
    store.finish_run(r2.run_id, outcome="succeeded", effect_state="applied")

    runs = store.list_runs(t.task_id)
    assert [r.run_id for r in runs] == [r1.run_id, r2.run_id]
    assert runs[0].outcome == "failed"
    assert runs[1].outcome == "succeeded"


def test_finish_after_fence_raises_even_if_state_cleared(store):
    """即使没人把 state 改成 fenced，版本号不符也必须拒绝。"""
    t = store.create_task("整理 A")
    r = store.start_run(t.task_id)

    # 手工只改版本号，不动 run.state，模拟并发写入
    store._conn.execute(
        "UPDATE tasks SET goal_revision = goal_revision + 1 WHERE task_id=?",
        (t.task_id,),
    )
    with pytest.raises(RunFenced):
        store.finish_run(r.run_id, outcome="succeeded", effect_state="applied")


def test_revise_unknown_task_raises(store):
    with pytest.raises(TianshuError):
        store.revise_goal("task_nope", "x")
