"""M2 · Task 2 验收：目录身份、跨进程租约、执行守卫。

要证明的是：**两个进程/两个任务不能同时改同一棵目录树**，
而且互斥依据是"真的是同一棵树"，不是路径字符串。

复审补充（PR #7 的测试是同进程两条连接 + sleep 猜时序，证不了跨进程）：
- 真正的子进程 + 文件/标准输入屏障，不用 sleep 排时序；
- 父子目录、符号链接别名必须冲突；
- owner 身份是进程实例，不是裸 pid；
- 进程硬退后 OS 锁会释放。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.leases import (
    ExecutionGuard,
    GuardTimeout,
    LeaseConflict,
    LeaseLost,
    LeaseStore,
    ProcessIdentity,
    execution_guard,
)
from tianshu.workspace import ResourceIdentity, Workspace, overlap_key

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture()
def env(tmp_path):
    log = EventLog(tmp_path / "db.sqlite")
    yield log, LeaseStore(log)
    log.close()


def _dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


# ---------------------------------------------------------------- 身份


def test_symlink_resolves_to_same_identity(tmp_path):
    real = _dir(tmp_path, "real")
    link = tmp_path / "link"
    os.symlink(real, link)
    assert Workspace(real).identity().key == Workspace(link).identity().key


def test_different_dirs_have_different_identity(tmp_path):
    assert Workspace(_dir(tmp_path, "a")).identity().key != Workspace(
        _dir(tmp_path, "b")
    ).identity().key


def test_identity_is_stable_across_calls(tmp_path):
    d = _dir(tmp_path, "x")
    assert ResourceIdentity.of(d) == ResourceIdentity.of(d)


def test_parent_and_child_overlap_but_are_not_identical(tmp_path):
    """inode 只回答"是不是同一个目录"，父子是两个 inode 却是同一棵树。"""
    parent = _dir(tmp_path, "ws")
    child = parent / "sub"
    child.mkdir()
    a, b = ResourceIdentity.of(parent), ResourceIdentity.of(child)
    assert a.key != b.key
    assert a.overlaps(b) and b.overlaps(a)
    assert overlap_key(parent) != overlap_key(child)


# ---------------------------------------------------------------- 互斥


def test_second_owner_cannot_take_held_lease(env, tmp_path):
    _, s = env
    d = _dir(tmp_path, "ws")
    s.acquire([d], "run_a")
    with pytest.raises(LeaseConflict):
        s.acquire([d], "run_b", steal_stale=False)


def test_parent_child_directories_conflict(env, tmp_path):
    """两个 Task 分别绑定 /ws 与 /ws/sub：同一棵树，必须互斥。"""
    _, s = env
    parent = _dir(tmp_path, "ws")
    child = parent / "sub"
    child.mkdir()

    s.acquire([parent], "run_parent")
    with pytest.raises(LeaseConflict) as ei:
        s.acquire([child], "run_child", steal_stale=False)
    assert "同一棵子树" in str(ei.value)


def test_child_then_parent_conflict(env, tmp_path):
    _, s = env
    parent = _dir(tmp_path, "ws")
    child = parent / "sub"
    child.mkdir()
    s.acquire([child], "run_child")
    with pytest.raises(LeaseConflict):
        s.acquire([parent], "run_parent", steal_stale=False)


def test_symlink_alias_conflicts(env, tmp_path):
    _, s = env
    real = _dir(tmp_path, "real")
    link = tmp_path / "alias"
    os.symlink(real, link)
    s.acquire([real], "run_a")
    with pytest.raises(LeaseConflict):
        s.acquire([link], "run_b", steal_stale=False)


def test_same_run_can_reacquire(env, tmp_path):
    _, s = env
    d = _dir(tmp_path, "ws")
    s.acquire([d], "run_a")
    s.acquire([d], "run_a")
    assert s.holder(d).run_id == "run_a"


def test_release_frees_the_dir(env, tmp_path):
    _, s = env
    d = _dir(tmp_path, "ws")
    s.acquire([d], "run_a")
    s.release([d], "run_a")
    s.acquire([d], "run_b", steal_stale=False)
    assert s.holder(d).run_id == "run_b"


def test_acquire_is_all_or_nothing(env, tmp_path):
    """两个目录里有一个被占，就不能只拿另一个。"""
    _, s = env
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    s.acquire([b], "run_other")
    with pytest.raises(LeaseConflict):
        s.acquire([a, b], "run_mine", steal_stale=False)
    assert s.holder(a) is None


def test_different_dirs_do_not_conflict(env, tmp_path):
    _, s = env
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    s.acquire([a], "run_a")
    s.acquire([b], "run_b", steal_stale=False)
    assert s.holder(a).run_id == "run_a"
    assert s.holder(b).run_id == "run_b"


# ---------------------------------------------------------------- 过期与接管


def test_expired_lease_can_be_taken_over_with_injected_clock(tmp_path):
    """用注入时钟推进 TTL，不靠 sleep 猜时间。"""
    now = [1_000_000]
    log = EventLog(tmp_path / "db.sqlite")
    try:
        s = LeaseStore(log, clock=lambda: now[0])
        d = _dir(tmp_path, "ws")
        s.acquire([d], "run_a", ttl_ms=100)
        now[0] += 50
        assert s.is_held_by(d, "run_a")
        now[0] += 100
        assert not s.is_held_by(d, "run_a")
        s.acquire([d], "run_b")
        assert s.holder(d).run_id == "run_b"
        with pytest.raises(LeaseLost):
            s.assert_held([d], "run_a")
    finally:
        log.close()


def test_renew_extends_expiry(env, tmp_path):
    _, s = env
    d = _dir(tmp_path, "ws")
    lease = s.acquire([d], "run_a", ttl_ms=50).leases[0]
    assert s.renew([d], "run_a", ttl_ms=10_000) == 1
    assert s.holder(d).expires_at > lease.expires_at


def test_renew_raises_when_not_owner(env, tmp_path):
    """心跳不能静默返回 0：续不到就等于失去租约。"""
    _, s = env
    d = _dir(tmp_path, "ws")
    s.acquire([d], "run_a")
    with pytest.raises(LeaseLost):
        s.renew([d], "run_b")


def test_failed_renew_does_not_extend_anything(env, tmp_path):
    """续租是全有全无：一个资源续不到，已续的也不能留下。"""
    _, s = env
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    first = s.acquire([a], "run_a", ttl_ms=1000).leases[0]
    s.acquire([b], "run_b", ttl_ms=1000)
    with pytest.raises(LeaseLost):
        s.renew([a, b], "run_a", ttl_ms=10_000)
    assert s.holder(a).expires_at == first.expires_at


def test_foreign_instance_cannot_pass_guard(env, tmp_path):
    """别的进程实例拿着同一个 run_id 也不等于持有者（PR #7 只比 run_id）。"""
    log, s = env
    d = _dir(tmp_path, "ws")
    s.acquire([d], "run_shared")
    foreign = LeaseStore(log, identity=ProcessIdentity.mint())
    with pytest.raises(LeaseLost):
        foreign.assert_held([d], "run_shared")
    with pytest.raises(LeaseLost):
        foreign.renew([d], "run_shared")
    assert foreign.release([d], "run_shared") == 0
    assert s.is_held_by(d, "run_shared"), "别人的 release 不能删掉持有者"


def test_release_run_drops_everything_for_that_run(env, tmp_path):
    _, s = env
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    s.acquire([a, b], "run_a")
    assert s.release_run("run_a") == 2
    assert s.list_for_run("run_a") == []


@pytest.mark.skipif(os.name != "posix", reason="PID 存活探测仅 POSIX 实现")
def test_dead_owner_lease_can_be_stolen(env, tmp_path):
    """持有者进程已消失 → 租约视为失效，可接管（不必等 TTL）。"""
    log, s = env
    d = _dir(tmp_path, "ws")
    s.acquire([d], "run_dead", ttl_ms=10 * 60_000)
    log.conn.execute(
        "UPDATE leases SET owner_pid = 999999 WHERE resource_key=?",
        (ResourceIdentity.of(d).key,),
    )
    s.acquire([d], "run_new", steal_stale=True)
    assert s.holder(d).run_id == "run_new"


# ---------------------------------------------------------------- 真·跨进程


_RACE_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, {src!r})
    from tianshu.events import EventLog
    from tianshu.leases import LeaseConflict, LeaseStore

    db, workspace, run_id, go = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    log = EventLog(Path(db))
    store = LeaseStore(log)
    print("ready", flush=True)
    while not Path(go).exists():   # 屏障：等父进程放行，不靠 sleep 猜先后
        pass
    try:
        store.acquire([Path(workspace)], run_id, steal_stale=False)
        print("win", flush=True)
    except LeaseConflict:
        print("conflict", flush=True)
    """
)


def test_two_processes_one_resource(tmp_path):
    """两个真进程抢同一目录：只允许一个拿到，另一个必须看到冲突。"""
    log = EventLog(tmp_path / "db.sqlite")
    log.close()
    ws = _dir(tmp_path, "ws")
    go = tmp_path / "go"

    children = [
        subprocess.Popen(
            [sys.executable, "-c", _RACE_CHILD.format(src=SRC),
             str(tmp_path / "db.sqlite"), str(ws), f"run_{i}", str(go)],
            stdout=subprocess.PIPE, text=True,
        )
        for i in range(2)
    ]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        go.write_text("go")
        results = sorted(child.stdout.readline().strip() for child in children)
    finally:
        for child in children:
            child.communicate(timeout=30)

    assert results == ["conflict", "win"], results


_HOLDER_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, {src!r})
    from tianshu.events import EventLog
    from tianshu.leases import LeaseConflict, LeaseStore

    db, workspace, run_id = sys.argv[1], sys.argv[2], sys.argv[3]
    log = EventLog(Path(db))
    store = LeaseStore(log)
    store.acquire([Path(workspace)], run_id)
    print("held", flush=True)
    sys.stdin.readline()          # 父进程关掉 stdin 才释放
    store.release([Path(workspace)], run_id)
    """
)


def test_live_process_blocks_another_process(env, tmp_path):
    """另一个活进程持有时，本进程必须冲突；它退出后才能拿到。"""
    _, s = env
    ws = _dir(tmp_path, "ws")
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_CHILD.format(src=SRC),
         str(tmp_path / "db.sqlite"), str(ws), "run_live"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(LeaseConflict):
            s.acquire([ws], "run_mine", steal_stale=False)
    finally:
        out, _ = child.communicate(timeout=30)
    assert child.returncode == 0, out

    s.acquire([ws], "run_mine", steal_stale=False)


# ---------------------------------------------------------------- 执行守卫


def test_execution_guard_excludes_across_processes(tmp_path):
    """守住"核验 + 开始执行"的窗口：另一个进程拿不到锁就只能等或超时。"""
    root = tmp_path / "data"
    child_src = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, {src!r})
        from tianshu.leases import GuardTimeout, execution_guard

        root = Path(sys.argv[1])
        try:
            with execution_guard(root, timeout_ms=200):
                print("got", flush=True)
        except GuardTimeout:
            print("timeout", flush=True)
        """
    )
    with execution_guard(root) as guard:
        assert isinstance(guard, ExecutionGuard)
        out = subprocess.run(
            [sys.executable, "-c", child_src.format(src=SRC), str(root)],
            capture_output=True, text=True, timeout=30,
        )
    assert out.stdout.strip() == "timeout", out.stderr

    with execution_guard(root, timeout_ms=1000):
        pass  # 父进程自己释放后能再拿到


def test_execution_guard_released_on_hard_exit(tmp_path):
    """进程硬退（os._exit）时内核仍会释放文件锁，不需要清理死锁。"""
    root = tmp_path / "data"
    child_src = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        sys.path.insert(0, {src!r})
        from tianshu.leases import execution_guard

        with execution_guard(Path(sys.argv[1])):
            print("locked", flush=True)
            os._exit(0)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_src.format(src=SRC), str(root)],
        stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "locked"
    child.wait(timeout=30)
    with execution_guard(root, timeout_ms=2000):
        pass


def test_guard_timeout_is_typed(tmp_path):
    root = tmp_path / "data"
    with execution_guard(root):
        with pytest.raises(GuardTimeout):
            with execution_guard(root, timeout_ms=50):
                pass


def test_dead_workspace_path_still_yields_identity(tmp_path):
    """目录还不存在时也不能崩：身份退化为规范路径键。"""
    missing = tmp_path / "not-yet"
    ident = ResourceIdentity.of(missing)
    assert ident.key.startswith("path:")
    assert ident.path.endswith("not-yet")