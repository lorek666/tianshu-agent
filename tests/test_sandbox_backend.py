"""后端原语：local 的真实行为与 memfs 的边界。

这里测的是**后端自己**承诺的东西（原子替换、mode、虚拟命名空间、不碰宿主盘），
工具可见的行为在 `test_backend_contract.py` 里对两个实现各跑一遍。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tianshu import sandbox
from tianshu.errors import TianshuError, WorkspaceViolation
from tianshu.sandbox import ENV_VAR, LocalBackend, MemBackend

# ---------------------------------------------------------------- local


def test_local_require_root_rejects_missing(tmp_path):
    with pytest.raises(WorkspaceViolation):
        LocalBackend().require_root(tmp_path / "nope")


def test_local_read_bytes_returns_prefix_and_total(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 100)
    data, total = LocalBackend().read_bytes(p, 10)
    assert data == b"x" * 10
    assert total == 100


def test_local_read_bytes_without_limit_reads_all(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 100)
    data, total = LocalBackend().read_bytes(p)
    assert data == b"x" * 100 and total == 100


def test_local_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalBackend().read_bytes(tmp_path / "nope")


def test_local_overwrite_preserves_mode(tmp_path):
    """M2 的 bug：mkstemp 0600 + replace 会把 0644 的用户文件悄悄变成 0600。"""
    p = tmp_path / "f.md"
    p.write_bytes(b"old")
    os.chmod(p, 0o644)

    LocalBackend().write_bytes_atomic(p, b"new")

    assert p.read_bytes() == b"new"
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_local_new_file_is_private(tmp_path):
    """新建文件维持 0600：不显式说明的话，下一个人会以为是 umask。"""
    p = tmp_path / "new.md"
    LocalBackend().write_bytes_atomic(p, b"x")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_local_atomic_replace_leaves_no_temp_files(tmp_path):
    p = tmp_path / "f.md"
    p.write_bytes(b"old")
    LocalBackend().write_bytes_atomic(p, b"new")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["f.md"]


def test_local_identity_prefers_inode(tmp_path):
    key = LocalBackend().identity_key(tmp_path)
    assert key == LocalBackend().identity_key(tmp_path)
    if os.stat(tmp_path).st_ino:
        assert not key.startswith("path:")


def test_local_unlink_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalBackend().unlink(tmp_path / "nope")


# ---------------------------------------------------------------- memfs


def _mem(tmp_path: Path, name: str = "ns") -> MemBackend:
    return MemBackend(namespace=tmp_path / name)


def test_mem_accepts_only_logical_roots(tmp_path):
    backend = _mem(tmp_path)
    with pytest.raises(WorkspaceViolation):
        backend.require_root(tmp_path / "ws")


def test_mem_refuses_when_namespace_exists_on_host(tmp_path):
    """命名空间在宿主上真实存在时 fail closed：说明环境已经不可信了。"""
    ns = tmp_path / "ns"
    ns.mkdir()
    backend = MemBackend(namespace=ns)
    with pytest.raises(WorkspaceViolation):
        backend.require_root(ns / "ws")


def test_mem_never_creates_host_paths(tmp_path):
    backend = _mem(tmp_path)
    root = tmp_path / "ns" / "ws"
    backend.require_root(root)

    backend.write_bytes_atomic(root / "a.md", b"hi")

    assert backend.read_bytes(root / "a.md")[0] == b"hi"
    # 宿主盘上什么都没发生：连命名空间目录都不该被创建
    assert not (tmp_path / "ns").exists()


def test_mem_roundtrip_and_listing(tmp_path):
    backend = _mem(tmp_path)
    root = tmp_path / "ns" / "ws"
    backend.require_root(root)
    backend.write_bytes_atomic(root / "b.md", b"bb")
    backend.write_bytes_atomic(root / "a.md", b"a")
    backend.write_bytes_atomic(root / "sub" / "c.md", b"ccc")

    entries = sorted(backend.list_dir(root))
    assert entries == [("a.md", False, 1), ("b.md", False, 2), ("sub", True, 0)]
    assert backend.is_dir(root / "sub")
    assert backend.size(root / "a.md") == 1


def test_mem_read_respects_limit(tmp_path):
    backend = _mem(tmp_path)
    root = tmp_path / "ns" / "ws"
    backend.require_root(root)
    backend.write_bytes_atomic(root / "big.md", b"x" * 50)

    data, total = backend.read_bytes(root / "big.md", 10)
    assert data == b"x" * 10 and total == 50


def test_mem_missing_raises_filenotfound(tmp_path):
    backend = _mem(tmp_path)
    root = tmp_path / "ns" / "ws"
    backend.require_root(root)
    with pytest.raises(FileNotFoundError):
        backend.read_bytes(root / "nope")
    with pytest.raises(FileNotFoundError):
        backend.unlink(root / "nope")


def test_mem_identity_is_path_based(tmp_path):
    backend = _mem(tmp_path)
    root = tmp_path / "ns" / "ws"
    backend.require_root(root)
    assert backend.identity_key(root / "a.md").startswith("path:")


# ---------------------------------------------------------------- 选择器


def test_default_backend_is_local(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    sandbox.reset_backend()
    assert sandbox.current_backend().name == sandbox.DEFAULT_BACKEND


def test_env_selects_memfs(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "memfs")
    sandbox.reset_backend()
    assert isinstance(sandbox.current_backend(), MemBackend)


def test_unknown_backend_name_is_a_clear_error():
    with pytest.raises(TianshuError) as excinfo:
        sandbox.configure_backend("nope")
    assert "未知后端" in str(excinfo.value)


def test_configure_backend_accepts_an_instance(tmp_path):
    backend = MemBackend(namespace=tmp_path / "ns")
    assert sandbox.configure_backend(backend) is backend
    assert sandbox.current_backend() is backend

def test_reset_backend_clears_memfs_state(monkeypatch):
    """reset 必须把 memfs 单例的虚拟状态一起清掉。

    只置 `_current=None` 的话，下一个用例会看见上一个用例的文件
    （典型症状：OverwriteRefused，或 list_files 多出条目）。
    """
    monkeypatch.setenv(ENV_VAR, "memfs")
    backend = sandbox.configure_backend("memfs")
    root = sandbox.MEM_NAMESPACE / "reset-case"
    backend.require_root(root)
    backend.write_bytes_atomic(root / "a.md", b"x")
    assert backend.exists(root / "a.md")

    sandbox.reset_backend()

    fresh = sandbox.configure_backend("memfs")
    assert fresh is backend, "memfs 是进程级单例"
    assert not fresh.exists(root / "a.md"), "reset 必须清掉虚拟状态"
