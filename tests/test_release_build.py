from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_release.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_archive(path: Path, *, target: str) -> Path:
    root = "python"
    members = {
        "bin/python3": b"synthetic-runtime",
        "lib/python3.12/os.py": b"# standard library",
        "lib/python3.12/__pycache__/os.cpython-312.pyc": b"cache",
    }
    if target.startswith("windows"):
        members = {
            "python.exe": b"synthetic-runtime",
            "Lib/os.py": b"# standard library",
        }
    with tarfile.open(path, "w:gz") as archive:
        for name, body in members.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return path


def _repo_fixture(base: Path) -> Path:
    repo = base / "repo"
    (repo / "src" / "tianshu").mkdir(parents=True)
    (repo / "src" / "tianshu" / "__init__.py").write_text(
        "__version__ = '0.1.0'\n", encoding="utf-8"
    )
    (repo / "src" / "tianshu" / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8"
    )
    (repo / "ui" / "dist" / "assets").mkdir(parents=True)
    (repo / "ui" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (repo / "ui" / "dist" / "assets" / "app.js").write_text("console.log('ok')\n")
    (repo / "LICENSE").write_text("Apache License 2.0\n", encoding="utf-8")
    return repo


def test_prepare_bundle_contains_only_app_runtime_and_ui(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    output = tmp_path / "bundle"

    manifest_path = module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit="595be35c037cef35da7f166a5db2a624f2b733a9",
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        allow_unbound_source=True,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {
        "app/tianshu/__init__.py",
        "app/tianshu/cli.py",
        "licenses/LICENSE",
        "python/bin/python3",
        "python/lib/python3.12/os.py",
        "release.json",
        "ui/dist/assets/app.js",
        "ui/dist/index.html",
    }
    assert not (output / "release-manifest.json").read_bytes() == b""
    assert not (output / "tests").exists()
    assert not list(output.rglob("__pycache__"))
    assert not list(output.rglob("*.pyc"))
    assert not (output / ".venv").exists()
    assert not (output / "release.json").read_bytes() == b""


def test_prepare_bundle_rejects_archive_hash_mismatch(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")

    with pytest.raises(module.BuildError, match="SHA256"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.1.0",
            commit="595be35c037cef35da7f166a5db2a624f2b733a9",
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            runtime_sha256="0" * 64,
            out_dir=tmp_path / "bundle",
            allow_unbound_source=True,
        )


def test_runtime_archive_rejects_path_traversal(tmp_path):
    module = _load_module()
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        body = b"escape"
        info = tarfile.TarInfo("../../escape")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))

    with pytest.raises(module.BuildError, match="路径"):
        module.safe_extract_tar(archive, tmp_path / "out")

    assert not (tmp_path.parent / "escape").exists()


def test_runtime_lock_has_all_four_desktop_targets():
    module = _load_module()

    lock = module.load_runtime_lock(ROOT / "release" / "python-runtime.lock.json")

    assert lock["python"] == "3.12.16"
    assert set(lock["targets"]) == {
        "macos-arm64",
        "macos-x64",
        "windows-x64",
        "windows-arm64",
    }
    for target in lock["targets"].values():
        assert hashlib.sha256(b"not-the-asset").hexdigest() != target["sha256"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_source_identity_requires_exact_clean_head(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    _git(repo, "init")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    head = _git(repo, "rev-parse", "HEAD")

    module.verify_source_identity(repo, head)

    with pytest.raises(module.BuildError, match="不一致"):
        module.verify_source_identity(repo, "0" * 40)
    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(module.BuildError, match="干净工作树"):
        module.verify_source_identity(repo, head)
