from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import ntpath
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_release.py"
EXPECTED_RUNTIME = {
    "macos-arm64": {
        "triple": "aarch64-apple-darwin",
        "asset": "cpython-3.12.16+20261110-aarch64-apple-darwin-install_only.tar.gz",
        "url": (
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20261110/"
            "cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz"
        ),
        "sha256": "e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f",
    },
    "macos-x64": {
        "triple": "x86_64-apple-darwin",
        "asset": "cpython-3.12.16+20261110-x86_64-apple-darwin-install_only.tar.gz",
        "url": (
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20261110/"
            "cpython-3.12.16%2B20261110-x86_64-apple-darwin-install_only.tar.gz"
        ),
        "sha256": "83f2922437e6d546d0fcdb8c11f150a8e52787e8d9ea8651b1974e20908eb1bc",
    },
    "windows-x64": {
        "triple": "x86_64-pc-windows-msvc",
        "asset": "cpython-3.12.16+20261110-x86_64-pc-windows-msvc-install_only.tar.gz",
        "url": (
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20261110/"
            "cpython-3.12.16%2B20261110-x86_64-pc-windows-msvc-install_only.tar.gz"
        ),
        "sha256": "e23fe5b16926de1d81661b1a97352e89fb167549c55a6825796ccd0f32ec7f1b",
    },
    "windows-arm64": {
        "triple": "aarch64-pc-windows-msvc",
        "asset": "cpython-3.12.16+20261110-aarch64-pc-windows-msvc-install_only.tar.gz",
        "url": (
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20261110/"
            "cpython-3.12.16%2B20261110-aarch64-pc-windows-msvc-install_only.tar.gz"
        ),
        "sha256": "7122bf62a0cf601527ba62e19ca715d64db86b269765da23937e9ac7f968f39c",
    },
}


def _load_module():
    spec = importlib.util.spec_from_file_location("build_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_archive(path: Path, *, target: str, executable: bool = True) -> Path:
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
            if executable and name in {"bin/python3", "python.exe"}:
                info.mode = 0o755
            archive.addfile(info, io.BytesIO(body))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    assert not (output / "tests").exists()
    assert not list(output.rglob("__pycache__"))
    assert not list(output.rglob("*.pyc"))
    assert not (output / ".venv").exists()


@pytest.mark.parametrize("target", sorted(EXPECTED_RUNTIME))
def test_prepare_bundle_supports_each_pinned_target_entrypoint(tmp_path, target):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target=target)
    output = tmp_path / "bundle"

    manifest_path = module.prepare_bundle(
        repo_root=repo,
        target=target,
        version="0.1.0",
        commit="595be35c037cef35da7f166a5db2a624f2b733a9",
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        runtime_sha256=_sha256(archive),
        allow_unbound_source=True,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = "python/python.exe" if target.startswith("windows") else "python/bin/python3"
    assert manifest["target"] == target
    assert entry in manifest["files"]
    assert (output / entry).is_file()


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


def test_prepare_bundle_rejects_non_executable_posix_entry(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(
        tmp_path / "runtime.tar.gz",
        target="macos-arm64",
        executable=False,
    )

    with pytest.raises(module.BuildError, match="不可执行"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.1.0",
            commit="595be35c037cef35da7f166a5db2a624f2b733a9",
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            out_dir=tmp_path / "bundle",
            runtime_sha256=_sha256(archive),
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


def test_runtime_lock_binds_each_target_to_its_native_triple(tmp_path):
    module = _load_module()
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    native = {
        "macos-arm64": "aarch64-apple-darwin",
        "macos-x64": "x86_64-apple-darwin",
        "windows-x64": "x86_64-pc-windows-msvc",
        "windows-arm64": "aarch64-pc-windows-msvc",
    }

    for target, triple in native.items():
        wrong = next(value for value in native.values() if value != triple)
        lock["targets"][target]["triple"] = wrong
        broken = tmp_path / f"{target}.json"
        broken.write_text(json.dumps(lock), encoding="utf-8")
        try:
            with pytest.raises(module.BuildError, match=rf"{target}\.triple"):
                module.load_runtime_lock(broken)
        finally:
            lock["targets"][target]["triple"] = triple


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_fixture(repo: Path) -> str:
    _git(repo, "init")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return _git(repo, "rev-parse", "HEAD")


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


def test_runtime_lock_pins_full_download_urls_and_published_hashes():
    module = _load_module()

    lock = module.load_runtime_lock(ROOT / "release" / "python-runtime.lock.json")

    assert lock == {
        "schema": 1,
        "release": "20261110",
        "python": "3.12.16",
        "source": "https://github.com/astral-sh/python-build-standalone",
        "targets": EXPECTED_RUNTIME,
    }


@pytest.mark.parametrize(
    ("field", "bad_value", "match"),
    [
        ("asset", "cpython-3.12.16+20261110-x86_64-apple-darwin-install_only.tar.gz", "asset"),
        (
            "url",
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20261110/wrong.tar.gz",
            "url",
        ),
    ],
)
def test_runtime_lock_rejects_target_identity_drift(tmp_path, field, bad_value, match):
    module = _load_module()
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock["targets"]["macos-arm64"][field] = bad_value
    broken = tmp_path / "runtime-lock.json"
    broken.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(module.BuildError, match=match):
        module.load_runtime_lock(broken)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("schema", True),
        ("python", "3.12.17"),
        ("release", "20261111"),
        ("source", "https://example.invalid/python-build-standalone"),
    ],
)
def test_runtime_lock_rejects_top_level_identity_drift(tmp_path, field, bad_value):
    module = _load_module()
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock[field] = bad_value
    broken = tmp_path / "runtime-lock.json"
    broken.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(module.BuildError):
        module.load_runtime_lock(broken)


def test_download_runtime_uses_canonical_url_and_hash_from_lock(tmp_path, monkeypatch):
    module = _load_module()
    body = b"controlled runtime archive bytes\x00with binary content"
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock["targets"]["macos-arm64"]["sha256"] = hashlib.sha256(body).hexdigest()
    lock_path = tmp_path / "runtime-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    requested: list[str] = []

    def fake_urlopen(request, timeout):
        assert timeout == 120
        requested.append(request.full_url)
        return io.BytesIO(body)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    path = module.download_runtime(lock_path, "macos-arm64", tmp_path / "cache")

    assert requested == [EXPECTED_RUNTIME["macos-arm64"]["url"]]
    assert path.read_bytes() == body
    assert _sha256(path) == hashlib.sha256(body).hexdigest()


def test_download_runtime_discards_hash_mismatch(tmp_path, monkeypatch):
    module = _load_module()
    body = b"corrupt or truncated runtime"
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock["targets"]["macos-arm64"]["sha256"] = "0" * 64
    lock_path = tmp_path / "runtime-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(body),
    )
    cache = tmp_path / "cache"
    with pytest.raises(module.BuildError, match="SHA256"):
        module.download_runtime(lock_path, "macos-arm64", cache)

    assert not (cache / EXPECTED_RUNTIME["macos-arm64"]["asset"]).exists()
    assert not list(cache.glob(".download-*"))


def _single_member_archive(
    path: Path,
    *,
    name: str,
    kind: bytes = tarfile.REGTYPE,
    body: bytes = b"runtime",
    linkname: str = "",
) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        if kind == tarfile.REGTYPE:
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        else:
            info.size = 0
            archive.addfile(info)
    return path


@pytest.mark.parametrize(
    ("name", "kind", "linkname", "match"),
    [
        ("python/bin/python3", tarfile.SYMTYPE, "python3.12", "链接"),
        ("python/bin/python3", tarfile.LNKTYPE, "python/bin/python3.12", "链接"),
        ("python/bin/fifo", tarfile.FIFOTYPE, "", "特殊文件"),
        ("python\\..\\escape", tarfile.REGTYPE, "", "路径"),
        ("python/bin/file:stream", tarfile.REGTYPE, "", "路径"),
    ],
)
def test_archive_rejects_non_regular_members_and_platform_escapes(
    tmp_path, name, kind, linkname, match
):
    module = _load_module()
    archive = _single_member_archive(
        tmp_path / "bad.tar.gz", name=name, kind=kind, linkname=linkname
    )

    with pytest.raises(module.BuildError, match=match):
        module.safe_extract_tar(archive, tmp_path / "out")


def test_archive_requires_the_exact_python_runtime_root(tmp_path):
    module = _load_module()
    archive = tmp_path / "extra-root.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("python/bin/python3", "outside/private.txt"):
            body = b"runtime"
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))

    with pytest.raises(module.BuildError, match="python/|根"):
        module.safe_extract_tar(archive, tmp_path / "out")


def test_archive_rejects_casefold_collisions(tmp_path):
    module = _load_module()
    archive = tmp_path / "case-collision.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("python/bin/Tool", "python/bin/tool"):
            body = b"runtime"
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))

    with pytest.raises(module.BuildError, match="重复路径"):
        module.safe_extract_tar(archive, tmp_path / "out")


def test_runtime_hash_is_verified_before_archive_contents_are_read(tmp_path):
    module = _load_module()
    archive = _single_member_archive(
        tmp_path / "malicious.tar.gz",
        name="../../escape",
    )

    with pytest.raises(module.BuildError, match="SHA256"):
        module.safe_extract_tar(
            archive,
            tmp_path / "out",
            expected_sha256="0" * 64,
        )

    assert not (tmp_path / "out").exists()


def test_prepare_bundle_does_not_copy_ignored_worktree_data(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    (repo / ".gitignore").write_text("*.private\n", encoding="utf-8")
    commit = _commit_fixture(repo)
    private = repo / "src" / "tianshu" / "user.private"
    private.write_text("synthetic local user data", encoding="utf-8")
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")

    manifest_path = module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit=commit,
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=tmp_path / "bundle",
        runtime_sha256=_sha256(archive),
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "app/tianshu/user.private" not in manifest["files"]
    assert not (manifest_path.parent / "app/tianshu/user.private").exists()


def test_prepare_bundle_rejects_credential_store_in_source(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    (repo / ".gitignore").write_text("credentials.json\n", encoding="utf-8")
    commit = _commit_fixture(repo)
    credential = repo / "src" / "tianshu" / "credentials.json"
    credential.write_text('{"api_key": "synthetic"}', encoding="utf-8")
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")

    with pytest.raises(module.BuildError, match="凭据|credentials"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.1.0",
            commit=commit,
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            out_dir=tmp_path / "bundle",
            runtime_sha256=_sha256(archive),
        )


@pytest.mark.parametrize(
    "relative",
    [
        ".venv/private.bin",
        "tests/private.txt",
        "refs/private.git",
        ".omc/state.json",
        "logs/private.log",
        "events.db-wal",
        "secret.key",
    ],
)
def test_prepare_bundle_rejects_forbidden_source_material(tmp_path, relative):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    forbidden = repo / "src" / "tianshu" / relative
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_bytes(b"synthetic forbidden release input")
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")

    with pytest.raises(module.BuildError, match="禁止|凭据|用户数据"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.1.0",
            commit="595be35c037cef35da7f166a5db2a624f2b733a9",
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            out_dir=tmp_path / "bundle",
            runtime_sha256=_sha256(archive),
            allow_unbound_source=True,
        )


def test_failed_prepare_preserves_existing_output_directory(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    output = tmp_path / "bundle"
    valid_archive = _runtime_archive(tmp_path / "valid.tar.gz", target="macos-arm64")
    module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit="595be35c037cef35da7f166a5db2a624f2b733a9",
        runtime_archive=valid_archive,
        python_version="3.12.16",
        python_build_release="20261110",
        runtime_sha256=_sha256(valid_archive),
        out_dir=output,
        allow_unbound_source=True,
    )
    previous_release = (output / "release.json").read_bytes()
    previous_manifest = (output / "release-manifest.json").read_bytes()

    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as runtime:
        for name, mode in (("python/bin/python3", 0o755), ("../../escape", 0o644)):
            body = b"runtime"
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(body)
            runtime.addfile(info, io.BytesIO(body))

    with pytest.raises(module.BuildError, match="路径"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.1.0",
            commit="595be35c037cef35da7f166a5db2a624f2b733a9",
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            runtime_sha256=_sha256(archive),
            out_dir=output,
            allow_unbound_source=True,
        )

    assert (output / "release.json").read_bytes() == previous_release
    assert (output / "release-manifest.json").read_bytes() == previous_manifest


def test_prepare_refuses_to_replace_unrelated_existing_directory(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    output = tmp_path / "user-data"
    output.mkdir()
    note = output / "keep.txt"
    note.write_text("unrelated user data", encoding="utf-8")

    with pytest.raises(module.BuildError, match="manifest"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.1.0",
            commit="595be35c037cef35da7f166a5db2a624f2b733a9",
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            out_dir=output,
            runtime_sha256=_sha256(archive),
            allow_unbound_source=True,
        )

    assert note.read_text(encoding="utf-8") == "unrelated user data"


def test_prepare_replaces_existing_empty_output_directory(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    output = tmp_path / "bundle"
    output.mkdir()

    manifest_path = module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit="595be35c037cef35da7f166a5db2a624f2b733a9",
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        runtime_sha256=_sha256(archive),
        allow_unbound_source=True,
    )

    assert manifest_path.is_file()
    assert json.loads((output / "release.json").read_text(encoding="utf-8"))["version"] == "0.1.0"


def test_prepare_bundle_manifest_matches_exact_identity_and_real_file_bytes(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    output = tmp_path / "bundle"
    commit = "595be35c037cef35da7f166a5db2a624f2b733a9"

    manifest_path = module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit=commit,
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        runtime_sha256=_sha256(archive),
        allow_unbound_source=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = json.loads((output / "release.json").read_text(encoding="utf-8"))

    assert identity == {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": commit,
        "target": "macos-arm64",
    }
    assert {field: manifest[field] for field in identity} == identity
    actual_files = {
        path.relative_to(output).as_posix(): _sha256(path)
        for path in output.rglob("*")
        if path.is_file() and path.name != "release-manifest.json"
    }
    assert manifest["files"] == actual_files
    assert manifest["ui_dist_sha256"] == actual_files["ui/dist/index.html"]
    assert manifest["python"] == "3.12.16"
    assert manifest["python_build_release"] == "20261110"
    module.validate_manifest(manifest, base_dir=output)
    assert not any(path.is_symlink() for path in output.rglob("*"))


def _git_with_input(repo: Path, *args: str, input_bytes: bytes, env: dict[str, str] | None = None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        input=input_bytes,
        capture_output=True,
        env=env,
    ).stdout.strip()


def _commit_with_extra_paths(
    repo: Path,
    parent: str,
    paths: dict[str, bytes],
) -> str:
    index = repo / ".git" / "review-extra-index"
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(index)
    _git_with_input(repo, "read-tree", parent, input_bytes=b"", env=environment)
    index_lines = []
    for relative, body in paths.items():
        oid = _git_with_input(
            repo,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=body,
        ).decode("ascii")
        index_lines.append(f"100644 {oid}\t{relative}\n".encode())
    _git_with_input(
        repo,
        "-c",
        "core.protectNTFS=false",
        "update-index",
        "--index-info",
        input_bytes=b"".join(index_lines),
        env=environment,
    )
    tree = _git_with_input(
        repo,
        "-c",
        "core.protectNTFS=false",
        "write-tree",
        input_bytes=b"",
        env=environment,
    ).decode("ascii")
    return _git_with_input(
        repo,
        "commit-tree",
        tree,
        "-p",
        parent,
        input_bytes=b"windows unsafe source path fixture\n",
    ).decode("ascii")


def _runtime_lock_for_archive(archive: Path, *, target: str = "macos-arm64") -> dict:
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock["targets"][target]["sha256"] = _sha256(archive)
    return lock


def _write_fixed_runtime_lock(repo: Path, lock: dict) -> None:
    release_dir = repo / "release"
    release_dir.mkdir()
    (release_dir / "python-runtime.lock.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _cli_build_command(
    repo: Path,
    *,
    target: str,
    version: str,
    commit: str,
    archive: Path,
    output: Path,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(repo),
        "--target",
        target,
        "--version",
        version,
        "--commit",
        commit,
        "--runtime-archive",
        str(archive),
        "--out",
        str(output),
    ]


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_replacement_refs_cannot_change_bundled_source_bytes(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    commit = _commit_fixture(repo)
    original_oid = _git(repo, "rev-parse", f"{commit}:src/tianshu/__init__.py")
    replacement_oid = _git_with_input(
        repo,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=b"raise RuntimeError('replacement blob must never ship')\n",
    ).decode("ascii")
    _git(repo, "replace", original_oid, replacement_oid)
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""

    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    output = tmp_path / "bundle"
    module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit=commit,
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        runtime_sha256=_sha256(archive),
    )

    assert (output / "app" / "tianshu" / "__init__.py").read_bytes() == (b"__version__ = '0.1.0'\n")


def test_cli_rejects_arbitrary_runtime_lock_path(tmp_path):
    repo = _repo_fixture(tmp_path)
    commit = _commit_fixture(repo)
    archive = _runtime_archive(tmp_path / "fake-runtime.tar.gz", target="macos-arm64")
    arbitrary_lock = tmp_path / "arbitrary-runtime-lock.json"
    arbitrary_lock.write_text(
        json.dumps(_runtime_lock_for_archive(archive), ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "bundle"
    command = _cli_build_command(
        repo,
        target="macos-arm64",
        version="0.1.0",
        commit=commit,
        archive=archive,
        output=output,
    )
    command.extend(["--runtime-lock", str(arbitrary_lock)])

    result = subprocess.run(command, capture_output=True, text=True)

    assert result.returncode == 2
    assert "runtime-lock" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "failpoint",
    ["release.publish.old_moved", "release.publish.new_moved"],
)
def test_publish_crash_is_recovered_and_restart_is_idempotent(tmp_path, failpoint):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    _write_fixed_runtime_lock(repo, _runtime_lock_for_archive(archive))
    commit = _commit_fixture(repo)
    output = tmp_path / "bundle"
    module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit=commit,
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        runtime_sha256=_sha256(archive),
    )

    command = _cli_build_command(
        repo,
        target="macos-arm64",
        version="0.2.0",
        commit=commit,
        archive=archive,
        output=output,
    )
    environment = dict(os.environ)
    environment["TIANSHU_FAILPOINT"] = failpoint
    crashed = subprocess.run(command, capture_output=True, text=True, env=environment)

    assert crashed.returncode == 77
    marker = tmp_path / ".bundle.publish.json"
    assert marker.is_file()
    if failpoint == "release.publish.old_moved":
        assert not output.exists()
    else:
        assert output.is_dir()

    recovered = subprocess.run(command, capture_output=True, text=True)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads((output / "release.json").read_text(encoding="utf-8"))["version"] == "0.2.0"
    first_tree = _tree_hashes(output)
    assert not marker.exists()
    assert not list(tmp_path.glob(".bundle.backup-*"))
    assert not list(tmp_path.glob(".bundle.build-*"))

    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode == 0, repeated.stderr
    assert _tree_hashes(output) == first_tree
    assert not marker.exists()


def test_output_symlink_is_rejected_before_resolving_to_target(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    target = tmp_path / "bundle-target"
    module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit="595be35c037cef35da7f166a5db2a624f2b733a9",
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=target,
        runtime_sha256=_sha256(archive),
        allow_unbound_source=True,
    )
    alias = tmp_path / "bundle-alias"
    alias.symlink_to(target, target_is_directory=True)
    before = _tree_hashes(target)

    with pytest.raises(module.BuildError, match="链接|symbolic"):
        module.prepare_bundle(
            repo_root=repo,
            target="macos-arm64",
            version="0.2.0",
            commit="595be35c037cef35da7f166a5db2a624f2b733a9",
            runtime_archive=archive,
            python_version="3.12.16",
            python_build_release="20261110",
            out_dir=alias,
            runtime_sha256=_sha256(archive),
            allow_unbound_source=True,
        )

    assert _tree_hashes(target) == before


def test_download_runtime_rejects_symlink_cache_before_writing(tmp_path, monkeypatch):
    module = _load_module()
    body = b"synthetic runtime bytes"
    archive_name = EXPECTED_RUNTIME["macos-arm64"]["asset"]
    target = tmp_path / "cache-target"
    target.mkdir()
    sentinel = target / "keep.bin"
    sentinel.write_bytes(b"do not move this target")
    cache = tmp_path / "cache"
    cache.symlink_to(target, target_is_directory=True)
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock["targets"]["macos-arm64"]["sha256"] = hashlib.sha256(body).hexdigest()
    lock_path = tmp_path / "runtime-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(body),
    )

    with pytest.raises(module.BuildError, match="链接|symbolic"):
        module.download_runtime(lock_path, "macos-arm64", cache)

    assert sentinel.read_bytes() == b"do not move this target"
    assert not (target / archive_name).exists()
    assert sorted(path.name for path in target.iterdir()) == ["keep.bin"]


def test_git_tree_rejects_casefold_colliding_source_paths(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    commit = _commit_fixture(repo)
    ss_oid = _git_with_input(
        repo,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=b"print('ss')\n",
    ).decode("ascii")
    sharp_s_oid = _git_with_input(
        repo,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=b"print('sharp s')\n",
    ).decode("ascii")
    index = repo / ".git" / "casefold-index"
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(index)
    _git_with_input(repo, "read-tree", commit, input_bytes=b"", env=environment)
    _git_with_input(
        repo,
        "update-index",
        "--index-info",
        input_bytes=(
            f"100644 {ss_oid}\tsrc/tianshu/ss.py\n100644 {sharp_s_oid}\tsrc/tianshu/\u00df.py\n"
        ).encode(),
        env=environment,
    )
    casefold_tree = _git_with_input(repo, "write-tree", input_bytes=b"", env=environment).decode(
        "ascii"
    )
    casefold_commit = _git_with_input(
        repo,
        "commit-tree",
        casefold_tree,
        "-p",
        commit,
        input_bytes=b"casefold tree fixture\n",
    ).decode("ascii")

    with pytest.raises(module.BuildError, match="大小写|casefold|冲突"):
        module._commit_file_specs(repo, casefold_commit)


@pytest.mark.parametrize(
    ("relative", "case"),
    [
        ("src/tianshu/a\\..\\..\\..\\..\\review-escape.py", "backslash-traversal"),
        ("src/tianshu/file:stream", "alternate-data-stream"),
        ("src/tianshu/NUL", "reserved-device"),
        ("src/tianshu/NUL .txt", "reserved-device-space"),
        ("src/tianshu/CON .js", "reserved-device-space"),
        ("src/tianshu/COM1 .txt", "reserved-device-space"),
        ("src/tianshu/AUX  .x", "reserved-device-space"),
        ("src/tianshu/trailing.", "trailing-dot"),
        ("src/tianshu/trailing ", "trailing-space"),
        ("src/tianshu/control\x01name", "control-character"),
    ],
)
def test_git_tree_rejects_windows_unsafe_source_paths(tmp_path, relative, case):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    parent = _commit_fixture(repo)
    commit = _commit_with_extra_paths(repo, parent, {relative: b"unsafe source path\n"})
    destination = tmp_path / "stage" / "app"
    destination.mkdir(parents=True)

    if case == "backslash-traversal":
        windows_stage = ntpath.join("C:\\release-stage", "app", "tianshu")
        windows_target = ntpath.normpath(
            ntpath.join(
                windows_stage,
                *PurePosixPath(relative.removeprefix("src/tianshu/")).parts,
            )
        )
        assert ntpath.relpath(windows_target, windows_stage).startswith("..\\")

    with pytest.raises(module.BuildError, match="路径|Windows|反斜杠|保留|控制|尾"):
        module._copy_tracked_inputs(repo, commit, destination)

    assert not any(destination.iterdir())


def test_download_runtime_rejects_symlink_ancestor_without_external_write(tmp_path, monkeypatch):
    module = _load_module()
    body = b"synthetic runtime bytes"
    cache_root = tmp_path / "cache-root"
    cache_root.mkdir()
    target = tmp_path / "external-target"
    target.mkdir()
    sentinel = target / "keep.bin"
    sentinel.write_bytes(b"do not write through this link")
    alias = cache_root / "alias"
    alias.symlink_to(target, target_is_directory=True)
    cache = alias / "nested"
    lock = json.loads((ROOT / "release" / "python-runtime.lock.json").read_text(encoding="utf-8"))
    lock["targets"]["macos-arm64"]["sha256"] = hashlib.sha256(body).hexdigest()
    lock_path = tmp_path / "runtime-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(body),
    )

    with pytest.raises(module.BuildError, match="链接|祖先|普通目录"):
        module.download_runtime(lock_path, "macos-arm64", cache)

    assert sentinel.read_bytes() == b"do not write through this link"
    assert not (target / "nested").exists()


def test_prejournal_crash_staging_is_cleaned_without_touching_unknown_directory(tmp_path):
    module = _load_module()
    repo = _repo_fixture(tmp_path)
    archive = _runtime_archive(tmp_path / "runtime.tar.gz", target="macos-arm64")
    _write_fixed_runtime_lock(repo, _runtime_lock_for_archive(archive))
    commit = _commit_fixture(repo)
    output = tmp_path / "bundle"
    module.prepare_bundle(
        repo_root=repo,
        target="macos-arm64",
        version="0.1.0",
        commit=commit,
        runtime_archive=archive,
        python_version="3.12.16",
        python_build_release="20261110",
        out_dir=output,
        runtime_sha256=_sha256(archive),
    )
    command = _cli_build_command(
        repo,
        target="macos-arm64",
        version="0.2.0",
        commit=commit,
        archive=archive,
        output=output,
    )
    child_code = """
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("release_build_crash_before_journal", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._extract_verified_members = lambda *args, **kwargs: os._exit(77)
raise SystemExit(module.main(json.loads(sys.argv[2])))
"""
    crashed = subprocess.run(
        [sys.executable, "-c", child_code, str(SCRIPT), json.dumps(command[2:])],
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == 77
    stale_staging = {path for path in tmp_path.glob(".bundle.build-*") if path.is_dir()}
    assert len(stale_staging) == 1
    assert not (tmp_path / ".bundle.publish.json").exists()

    foreign = tmp_path / ".bundle.build-unowned"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("not owned by the release builder", encoding="utf-8")

    restarted = subprocess.run(command, capture_output=True, text=True)

    assert restarted.returncode == 0, restarted.stderr
    assert all(not path.exists() for path in stale_staging)
    assert sentinel.read_text(encoding="utf-8") == "not owned by the release builder"
    assert not list(tmp_path.glob(".bundle.staging-*.json"))
    assert json.loads((output / "release.json").read_text(encoding="utf-8"))["version"] == "0.2.0"
