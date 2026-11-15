from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install_macos.sh"
UNINSTALL = ROOT / "scripts" / "uninstall_macos.sh"
pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="macOS 安装脚本依赖 plutil/shasum；其他平台的 shell 行为不作为 macOS 安装证据",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive(
    base: Path,
    *,
    unsafe_name: str | None = None,
    python_exit: int = 0,
    verifier_exit: int = 0,
) -> tuple[Path, Path]:
    base.mkdir(parents=True, exist_ok=True)
    app = base / "payload" / "Tianshu Agent.app"
    resources = app / "Contents" / "Resources"
    python = resources / "python" / "bin" / "python3"
    ui = resources / "ui" / "dist" / "index.html"
    desktop = app / "Contents" / "MacOS" / "tianshu-desktop"
    python.parent.mkdir(parents=True)
    ui.parent.mkdir(parents=True)
    desktop.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexit {python_exit}\n", encoding="utf-8")
    python.chmod(0o755)
    desktop.write_text(
        "#!/bin/sh\n"
        'if [ "${1-}" = "--verify-bundle" ]; then\n'
        f"  exit {verifier_exit}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    desktop.chmod(0o755)
    ui.write_text("<!doctype html>", encoding="utf-8")
    (resources / "release.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "product": "tianshu-agent",
                "version": "0.1.0",
                "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
                "target": "macos-arm64",
            }
        ),
        encoding="utf-8",
    )
    archive = base / "Tianshu-Agent-0.1.0-macos-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(app, arcname="Tianshu Agent.app")
        if unsafe_name is not None:
            payload = b"escape"
            info = tarfile.TarInfo(unsafe_name)
            info.size = len(payload)
            import io

            stream.addfile(info, io.BytesIO(payload))
    checksum = base / "Tianshu-Agent-0.1.0-macos-arm64.sha256"
    checksum.write_text(f"{_sha(archive)}  {archive.name}\n", encoding="ascii")
    return archive, checksum


def _run(script: Path, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *(str(arg) for arg in args)],
        check=False,
        text=True,
        capture_output=True,
    )


def test_install_canonicalizes_symlink_prefix_and_does_not_touch_data(tmp_path):
    archive, checksum = _archive(tmp_path)
    real_prefix = tmp_path / "real applications"
    real_prefix.mkdir()
    linked_prefix = tmp_path / "linked applications"
    linked_prefix.symlink_to(real_prefix, target_is_directory=True)
    data_root = tmp_path / "data"
    data_root.mkdir()
    marker = data_root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    result = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        linked_prefix,
    )

    assert result.returncode == 0, result.stderr
    installed = real_prefix / "Tianshu Agent.app"
    assert installed.is_dir()
    assert linked_prefix.is_symlink()
    assert (linked_prefix / "Tianshu Agent.app").resolve() == installed.resolve()
    assert f"已安装：{installed}" in result.stdout
    assert marker.read_text(encoding="utf-8") == "keep"


def test_install_rejects_checksum_mismatch_before_touching_existing_app(tmp_path):
    archive, checksum = _archive(tmp_path)
    checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="ascii")
    prefix = tmp_path / "Applications"
    existing = prefix / "Tianshu Agent.app"
    existing.mkdir(parents=True)
    marker = existing / "keep"
    marker.write_text("old", encoding="utf-8")

    result = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
    )

    assert result.returncode == 2
    assert "SHA256 不匹配" in result.stderr
    assert marker.read_text(encoding="utf-8") == "old"


def test_install_rejects_archive_path_traversal(tmp_path):
    archive, checksum = _archive(tmp_path, unsafe_name="../../outside")
    prefix = tmp_path / "Applications"

    result = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
    )

    assert result.returncode == 2
    assert "逃逸路径" in result.stderr
    assert not (prefix / "Tianshu Agent.app").exists()


def test_install_runs_bundle_verifier_before_replacing_existing_app(tmp_path):
    archive, checksum = _archive(tmp_path, verifier_exit=1)
    prefix = tmp_path / "Applications"
    existing = prefix / "Tianshu Agent.app"
    existing.mkdir(parents=True)
    marker = existing / "keep"
    marker.write_text("old", encoding="utf-8")

    result = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
    )

    assert result.returncode == 2
    assert "资源清单校验失败" in result.stderr
    assert marker.read_text(encoding="utf-8") == "old"


def test_install_rejects_overlapping_program_and_data_roots(tmp_path):
    archive, checksum = _archive(tmp_path)
    prefix = tmp_path / "Applications"
    data_root = prefix / "AgentOS"

    result = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
        "--data-root",
        data_root,
    )

    assert result.returncode == 2
    assert "数据根不能位于安装目录内" in result.stderr
    assert not (prefix / "Tianshu Agent.app").exists()


def test_uninstall_removes_program_and_preserves_data_by_default(tmp_path):
    archive, checksum = _archive(tmp_path)
    prefix = tmp_path / "Applications"
    data_root = tmp_path / "AgentOS"
    data_root.mkdir()
    marker = data_root / "events.db"
    marker.write_bytes(b"data")
    installed = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
    )
    assert installed.returncode == 0, installed.stderr

    result = _run(
        UNINSTALL,
        "--prefix",
        prefix,
        "--data-root",
        data_root,
    )

    assert result.returncode == 0, result.stderr
    assert not (prefix / "Tianshu Agent.app").exists()
    assert marker.read_bytes() == b"data"
    assert "用户数据保留" in result.stdout


def test_uninstall_purges_only_explicit_temporary_data_root(tmp_path):
    archive, checksum = _archive(tmp_path)
    prefix = tmp_path / "Applications"
    data_root = tmp_path / "AgentOS"
    data_root.mkdir()
    (data_root / "events.db").write_bytes(b"data")
    installed = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
    )
    assert installed.returncode == 0, installed.stderr

    result = _run(
        UNINSTALL,
        "--prefix",
        prefix,
        "--data-root",
        data_root,
        "--purge-data",
        "--confirm-data-root",
        data_root,
    )

    assert result.returncode == 0, result.stderr
    assert not (prefix / "Tianshu Agent.app").exists()
    assert not data_root.exists()
    assert "已按显式请求删除用户数据" in result.stdout


def test_uninstall_purge_requires_exact_secondary_confirmation(tmp_path):
    archive, checksum = _archive(tmp_path)
    prefix = tmp_path / "Applications"
    data_root = tmp_path / "AgentOS"
    data_root.mkdir()
    marker = data_root / "events.db"
    marker.write_bytes(b"data")
    installed = _run(
        INSTALL,
        "--archive",
        archive,
        "--sha256",
        checksum,
        "--prefix",
        prefix,
    )
    assert installed.returncode == 0, installed.stderr

    result = _run(
        UNINSTALL,
        "--prefix",
        prefix,
        "--data-root",
        data_root,
        "--purge-data",
    )

    assert result.returncode == 2
    assert marker.read_bytes() == b"data"
    assert "confirm-data-root" in result.stderr
    assert (prefix / "Tianshu Agent.app").is_dir()


def test_reinstall_replaces_old_version_after_smoke(tmp_path):
    first_archive, first_checksum = _archive(tmp_path / "first")
    prefix = tmp_path / "Applications"
    first = _run(
        INSTALL,
        "--archive",
        first_archive,
        "--sha256",
        first_checksum,
        "--prefix",
        prefix,
    )
    assert first.returncode == 0, first.stderr
    old_marker = prefix / "Tianshu Agent.app/Contents/Resources/old-version"
    old_marker.write_text("old", encoding="utf-8")
    second_archive, second_checksum = _archive(tmp_path / "second")

    second = _run(
        INSTALL,
        "--archive",
        second_archive,
        "--sha256",
        second_checksum,
        "--prefix",
        prefix,
    )

    assert second.returncode == 0, second.stderr
    assert not old_marker.exists()
    assert not list(prefix.glob("Tianshu Agent.app.previous-*"))


def test_failed_reinstall_smoke_restores_previous_version(tmp_path):
    first_archive, first_checksum = _archive(tmp_path / "first")
    prefix = tmp_path / "Applications"
    first = _run(
        INSTALL,
        "--archive",
        first_archive,
        "--sha256",
        first_checksum,
        "--prefix",
        prefix,
    )
    assert first.returncode == 0, first.stderr
    old_marker = prefix / "Tianshu Agent.app/Contents/Resources/old-version"
    old_marker.write_text("old", encoding="utf-8")
    second_archive, second_checksum = _archive(
        tmp_path / "second",
        python_exit=1,
    )

    second = _run(
        INSTALL,
        "--archive",
        second_archive,
        "--sha256",
        second_checksum,
        "--prefix",
        prefix,
    )

    assert second.returncode == 1
    assert "恢复旧版本" in second.stderr
    assert old_marker.read_text(encoding="utf-8") == "old"
    assert not list(prefix.glob("Tianshu Agent.app.previous-*"))
