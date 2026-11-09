"""M2 · Task 1 验收：Task/Run 与目标版本（fencing）。

要证明的是：**用户中途改主意，旧目标是真失效，不只是"新目标覆盖了旧目标"。**
复审补充：状态必须是事件的投影（否则回放无法解释 fence），
Run 收尾必须交还租约（否则下一个 Run 被自己挡住）。
"""

from __future__ import annotations

import pytest

from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.leases import LeaseLost, LeaseStore
from tianshu.tasks import RunFenced, TaskStore
from tianshu.workspace import ResourceIdentity, WorkspaceBinding


@pytest.fixture()
def env(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    leases = LeaseStore(log)
    # data_root 必填：目标变更与"开始执行"必须抢同一把本机守卫
    store = TaskStore(log, leases=leases, data_root=tmp_path / "data")
    ws = tmp_path / "ws"
    ws.mkdir()
    yield log, store, leases, ws
    log.close()


def test_create_task_defaults(env):
    _, store, _, ws = env
    t = store.create_task("整理报告", [str(ws)])
    assert t.goal_revision == 1
    assert t.lease_epoch == 1
    assert t.active_run_id is None
    assert t.status == "open"
    assert len(t.workspaces) == 1
    assert t.workspaces[0].root_path == str(ws.resolve())
    assert t.workspaces[0].mode == "writable"


def test_zero_workspace_is_legal_and_implicit_nothing(env):
    """纯对话任务：0 个目录合法，且不能隐式变成 cwd。"""
    _, store, _, _ = env
    t = store.create_task("只是聊聊天")
    assert t.workspaces == []
    assert store.get_task(t.task_id).workspaces == []


def test_binding_mode_is_persisted(env):
    _, store, _, ws = env
    t = store.create_task("读 A 写 B", [
        WorkspaceBinding(str(ws), mode="readonly", snapshot_policy="none"),
    ])
    b = store.get_task(t.task_id).workspaces[0]
    assert b.mode == "readonly"
    assert b.snapshot_policy == "none"


def test_invalid_binding_is_rejected(env):
    _, store, _, ws = env
    with pytest.raises(TianshuError):
        store.create_task("x", [WorkspaceBinding(str(ws), mode="write")])


def test_empty_goal_is_rejected(env):
    _, store, _, _ = env
    with pytest.raises(TianshuError):
        store.create_task("   ")


def test_run_binds_current_revision(env):
    _, store, _, _ = env
    t = store.create_task("整理报告")
    r = store.start_run(t.task_id)
    assert r.goal_revision == 1
    assert r.lease_epoch == 1
    assert store.get_task(t.task_id).active_run_id == r.run_id


def test_finish_run_clears_active_and_releases_lease(env):
    _, store, leases, ws = env
    t = store.create_task("整理报告", [str(ws)])
    r = store.start_run(t.task_id)
    leases.acquire([ws], r.run_id)

    done = store.finish_run(r.run_id, outcome="succeeded", effect_state="applied")
    assert done.state == "succeeded"
    assert store.get_task(t.task_id).active_run_id is None
    assert leases.list_for_run(r.run_id) == [], "Run 收尾必须交还租约"


def test_revise_goal_fences_running_run_and_frees_workspace(env):
    """改目标后新 Run 必须能立刻接手目录，不能被旧 Run 的残留租约挡住。"""
    _, store, leases, ws = env
    t = store.create_task("整理 A 目录", [str(ws)])
    r = store.start_run(t.task_id)
    leases.acquire([ws], r.run_id)

    t2 = store.revise_goal(t.task_id, "只整理 B 目录")
    assert t2.goal_revision == 2
    assert t2.lease_epoch == 2
    assert store.get_run(r.run_id).state == "fenced"
    assert store.is_fenced(r.run_id)
    assert leases.list_for_run(r.run_id) == [], "被 fence 的 Run 必须交还租约"

    r2 = store.start_run(t.task_id)
    leases.acquire([ws], r2.run_id)  # 不抛 LeaseConflict


def test_fenced_run_cannot_report_success(env):
    """旧目标下未完成的 Run 不能提交成功——否则旧目标会"复活"。"""
    _, store, _, _ = env
    t = store.create_task("整理 A")
    r = store.start_run(t.task_id)
    store.revise_goal(t.task_id, "只整理 B")

    with pytest.raises(RunFenced):
        store.finish_run(r.run_id, outcome="succeeded", effect_state="applied")


def test_new_run_uses_new_revision(env):
    _, store, _, _ = env
    t = store.create_task("整理 A")
    store.start_run(t.task_id)
    store.revise_goal(t.task_id, "只整理 B")

    r2 = store.start_run(t.task_id)
    assert r2.goal_revision == 2
    assert r2.lease_epoch == 2
    assert not store.is_fenced(r2.run_id)


def test_history_is_preserved(env):
    """改目标不覆盖历史：先失败的 Run 与新 Run 都要在。"""
    _, store, _, _ = env
    t = store.create_task("整理 A")
    r1 = store.start_run(t.task_id)
    store.finish_run(r1.run_id, outcome="failed", effect_state="none_applied")

    r2 = store.start_run(t.task_id)
    store.finish_run(r2.run_id, outcome="succeeded", effect_state="applied")

    runs = store.list_runs(t.task_id)
    assert [r.run_id for r in runs] == [r1.run_id, r2.run_id]
    assert runs[0].outcome == "failed"
    assert runs[1].outcome == "succeeded"


def test_finish_after_fence_raises_even_if_state_cleared(env):
    """即使没人把 state 改成 fenced，版本号不符也必须拒绝。"""
    _, store, _, _ = env
    t = store.create_task("整理 A")
    r = store.start_run(t.task_id)

    store._conn.execute(
        "UPDATE tasks SET goal_revision = goal_revision + 1 WHERE task_id=?",
        (t.task_id,),
    )
    with pytest.raises(RunFenced):
        store.finish_run(r.run_id, outcome="succeeded", effect_state="applied")


def test_unknown_outcome_or_effect_state_is_rejected(env):
    """PR #7 的 finish_run 接受任意字符串并把 state 写成它，Run 就永远不是终态。"""
    _, store, _, _ = env
    t = store.create_task("整理 A")
    r = store.start_run(t.task_id)

    with pytest.raises(TianshuError):
        store.finish_run(r.run_id, outcome="noop", effect_state="applied")
    with pytest.raises(TianshuError):
        store.finish_run(r.run_id, outcome="succeeded", effect_state="whatever")
    assert store.get_run(r.run_id).state == "created"


def test_revise_unknown_task_raises(env):
    _, store, _, _ = env
    with pytest.raises(TianshuError):
        store.revise_goal("task_nope", "x")


def test_start_run_refuses_second_active_run(env):
    """同一 Task 不允许两个活跃 Run，否则会互相覆盖成果。"""
    _, store, _, _ = env
    t = store.create_task("x")
    store.start_run(t.task_id)
    with pytest.raises(TianshuError) as ei:
        store.start_run(t.task_id)
    assert "活跃" in str(ei.value)


def test_start_run_allowed_after_finish(env):
    _, store, _, _ = env
    t = store.create_task("x")
    r1 = store.start_run(t.task_id)
    store.finish_run(r1.run_id, outcome="failed", effect_state="none_applied")
    r2 = store.start_run(t.task_id)
    assert r2.run_id != r1.run_id


# ---------------------------------------------------------------- 执行侧校验


def test_assert_current_rejects_fenced_run(env):
    _, store, _, _ = env
    t = store.create_task("x")
    r = store.start_run(t.task_id)
    store.assert_current(r.run_id)
    store.revise_goal(t.task_id, "y")
    with pytest.raises(RunFenced):
        store.assert_current(r.run_id)


def test_assert_write_allowed_needs_both_run_and_lease(env):
    _, store, leases, ws = env
    t = store.create_task("x", [str(ws)])
    r = store.start_run(t.task_id)

    with pytest.raises(LeaseLost):
        store.assert_write_allowed(r.run_id, [ResourceIdentity.of(ws)])  # 还没租约

    leases.acquire([ws], r.run_id)
    store.assert_write_allowed(r.run_id, [ResourceIdentity.of(ws)])

    leases.release([ws], r.run_id)
    with pytest.raises(LeaseLost):
        store.assert_write_allowed(r.run_id, [ResourceIdentity.of(ws)])


# ---------------------------------------------------------------- 事件投影


def test_state_changes_are_recorded_as_events(env):
    log, store, _, _ = env
    t = store.create_task("整理")
    r1 = store.start_run(t.task_id)
    store.finish_run(r1.run_id, outcome="failed", effect_state="none_applied")
    store.revise_goal(t.task_id, "只整理 B")

    types = [ev.type for ev in log.read_types(
        ["task.created", "task.revised", "run.start", "run.finish", "run.fenced"]
    )]
    assert types == ["task.created", "run.start", "run.finish", "task.revised"]


def test_rebuild_from_events_restores_projection(env):
    """事件是事实源：删掉投影表也必须能一模一样地重建回来。"""
    log, store, _, _ = env
    t = store.create_task("整理", [str(env[3])])
    r1 = store.start_run(t.task_id)
    store.finish_run(r1.run_id, outcome="failed", effect_state="none_applied")
    store.revise_goal(t.task_id, "只整理 B")
    r2 = store.start_run(t.task_id)
    store.finish_run(r2.run_id, outcome="succeeded", effect_state="applied")

    before_tasks = [row for row in log.conn.execute(
        "SELECT task_id, goal, goal_revision, lease_epoch, active_run_id,"
        " status, workspaces FROM tasks ORDER BY task_id"
    )]
    before_runs = [row for row in log.conn.execute(
        "SELECT run_id, task_id, goal_revision, lease_epoch, state, outcome,"
        " effect_state FROM runs ORDER BY created_at"
    )]

    log.conn.execute("DELETE FROM tasks")
    log.conn.execute("DELETE FROM runs")
    store.rebuild_from_events()

    after_tasks = [row for row in log.conn.execute(
        "SELECT task_id, goal, goal_revision, lease_epoch, active_run_id,"
        " status, workspaces FROM tasks ORDER BY task_id"
    )]
    after_runs = [row for row in log.conn.execute(
        "SELECT run_id, task_id, goal_revision, lease_epoch, state, outcome,"
        " effect_state FROM runs ORDER BY created_at"
    )]
    assert [tuple(r) for r in before_tasks] == [tuple(r) for r in after_tasks]
    assert [tuple(r) for r in before_runs] == [tuple(r) for r in after_runs]


def test_rebuild_keeps_fenced_state(env):
    log, store, _, _ = env
    t = store.create_task("x")
    r = store.start_run(t.task_id)
    store.revise_goal(t.task_id, "y")
    log.conn.execute("DELETE FROM runs")
    store.rebuild_from_events()
    assert store.get_run(r.run_id).state == "fenced"

def test_dict_binding_is_canonicalized_like_a_string_path(tmp_path):
    """同一目录的两种写法必须得到同一个绑定，否则授权会前后矛盾。

    dict 输入（事件重建、CLI、手写测试）如果原样保留 `/tmp/x` 别名，而执行侧
    解析出 `/private/tmp/x`，`_covers` 就会把"目录内写入"判成"目录外"；
    反方向更糟：别名写法能绕过本应命中的绑定。
    """
    from tianshu.workspace import WorkspaceBinding, parse_bindings

    target = tmp_path / "ws"
    target.mkdir()
    alias = str(target)
    resolved = str(target.resolve())

    from_str = WorkspaceBinding.of(alias)
    from_dict = WorkspaceBinding.of({"root_path": alias, "mode": "readonly"})
    rebuilt = parse_bindings(
        [{"root_path": str(target.parent / "." / target.name),
          "mode": "readonly", "snapshot_policy": "on_write"}]
    )[0]

    assert from_str.root_path == resolved
    assert from_dict.root_path == resolved
    assert rebuilt.root_path == resolved
    assert from_dict.mode == "readonly"
