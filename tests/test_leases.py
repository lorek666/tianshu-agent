"""M2 · Task 2 验收：目录身份与跨进程租约。

要证明的是：**两个进程/两个任务不能同时改同一个目录**，
而且互斥依据是"真的是同一个目录"，不是路径字符串。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from tianshu.leases import LeaseConflict, LeaseStore
from tianshu.workspace import ResourceIdentity, Workspace


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db), isolation_level=None, timeout=5)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "db.sqlite"


# ---------------------------------------------------------------- 身份


def test_symlink_resolves_to_same_identity(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)

    assert Workspace(real).identity().key == Workspace(link).identity().key


def test_different_dirs_have_different_identity(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert Workspace(tmp_path / "a").identity().key != Workspace(
        tmp_path / "b"
    ).identity().key


def test_identity_is_stable_across_calls(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    assert ResourceIdentity.of(d) == ResourceIdentity.of(d)


# ---------------------------------------------------------------- 租约


def test_second_process_cannot_take_held_lease(db, tmp_path):
    """两个独立连接 = 两个进程，必须互斥。"""
    d = tmp_path / "ws"
    d.mkdir()
    key = Workspace(d).identity().key

    c1, c2 = _conn(db), _conn(db)
    try:
        LeaseStore(c1).acquire_many([key], "run_a")
        with pytest.raises(LeaseConflict):
            LeaseStore(c2).acquire_many([key], "run_b", steal_stale=False)
    finally:
        c1.close()
        c2.close()


def test_same_run_can_reacquire(db, tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    key = Workspace(d).identity().key
    c = _conn(db)
    try:
        s = LeaseStore(c)
        s.acquire_many([key], "run_a")
        s.acquire_many([key], "run_a")  # 自己重复获取不冲突（续租语义）
        assert s.holder(key).run_id == "run_a"
    finally:
        c.close()


def test_release_frees_the_dir(db, tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    key = Workspace(d).identity().key
    c1, c2 = _conn(db), _conn(db)
    try:
        LeaseStore(c1).acquire_many([key], "run_a")
        LeaseStore(c1).release([key], "run_a")
        LeaseStore(c2).acquire_many([key], "run_b", steal_stale=False)
        assert LeaseStore(c2).holder(key).run_id == "run_b"
    finally:
        c1.close()
        c2.close()


def test_expired_lease_can_be_taken_over(db, tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    key = Workspace(d).identity().key
    c = _conn(db)
    try:
        s = LeaseStore(c)
        s.acquire_many([key], "run_a", ttl_ms=1)  # 立刻过期
        time.sleep(0.05)
        s.acquire_many([key], "run_b")  # 默认允许接管过期租约
        assert s.holder(key).run_id == "run_b"
    finally:
        c.close()


def test_acquire_many_is_all_or_nothing(db, tmp_path):
    """两个目录里有一个被占，就不能只拿另一个。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ka, kb = Workspace(a).identity().key, Workspace(b).identity().key

    c1, c2 = _conn(db), _conn(db)
    try:
        LeaseStore(c1).acquire_many([kb], "run_other")
        with pytest.raises(LeaseConflict):
            LeaseStore(c2).acquire_many([ka, kb], "run_mine", steal_stale=False)
        # 失败的批次不能留下 ka 的占用
        assert LeaseStore(c2).holder(ka) is None
    finally:
        c1.close()
        c2.close()


def test_different_dirs_do_not_conflict(db, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ka, kb = Workspace(a).identity().key, Workspace(b).identity().key
    c = _conn(db)
    try:
        s = LeaseStore(c)
        s.acquire_many([ka], "run_a")
        s.acquire_many([kb], "run_b", steal_stale=False)
        assert s.holder(ka).run_id == "run_a"
        assert s.holder(kb).run_id == "run_b"
    finally:
        c.close()


def test_renew_extends_expiry(db, tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    key = Workspace(d).identity().key
    c = _conn(db)
    try:
        s = LeaseStore(c)
        lease = s.acquire_many([key], "run_a", ttl_ms=50)[0]
        assert s.renew([key], "run_a", ttl_ms=10_000) == 1
        assert s.holder(key).expires_at > lease.expires_at
    finally:
        c.close()


@pytest.mark.skipif(os.name != "posix", reason="PID 存活探测仅 POSIX 实现")
def test_dead_owner_lease_can_be_stolen(db, tmp_path):
    """持有者进程已消失 → 租约视为失效，可接管（不必等 TTL）。"""
    d = tmp_path / "ws"
    d.mkdir()
    key = Workspace(d).identity().key
    c = _conn(db)
    try:
        s = LeaseStore(c)
        s.acquire_many([key], "run_dead", ttl_ms=10 * 60_000)
        # 伪造一个不存在的 pid
        c.execute("UPDATE leases SET owner_pid = 999999 WHERE resource_key=?", (key,))
        s.acquire_many([key], "run_new", steal_stale=True)
        assert s.holder(key).run_id == "run_new"
    finally:
        c.close()
