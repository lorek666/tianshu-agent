from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from release_manifest import (  # noqa: E402
    ReleaseManifestError,
    validate_manifest,
    write_manifest,
)

TARGET_TRIPLES = {
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x64": "x86_64-apple-darwin",
    "windows-x64": "x86_64-pc-windows-msvc",
    "windows-arm64": "aarch64-pc-windows-msvc",
}
TARGETS = frozenset(TARGET_TRIPLES)


class BuildError(ValueError):
    """发行构建输入或中间产物不满足约束。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source_identity(repo_root: Path, commit: str) -> None:
    repo_root = Path(repo_root).resolve()
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"无法确认发行源身份：{exc}") from exc
    if head != commit:
        raise BuildError(f"源 HEAD {head} 与发行 commit {commit} 不一致")
    if status:
        raise BuildError("发行构建要求干净工作树")


def load_runtime_lock(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"无法读取 Python runtime lock：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise BuildError("不支持的 Python runtime lock schema")
    if data.get("python") != "3.12.16":
        raise BuildError("Python runtime lock 必须固定为 3.12.16")
    targets = data.get("targets")
    if not isinstance(targets, dict) or set(targets) != TARGETS:
        raise BuildError("Python runtime lock 必须精确覆盖四个桌面 target")
    for target, entry in targets.items():
        if not isinstance(entry, dict):
            raise BuildError(f"{target} runtime entry 必须是 object")
        for field in ("triple", "asset", "sha256"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise BuildError(f"{target}.{field} 必须是非空字符串")
        expected_triple = TARGET_TRIPLES[target]
        if entry["triple"] != expected_triple:
            raise BuildError(
                f"{target}.triple 必须是 {expected_triple}，实际为 {entry['triple']}"
            )
        digest = entry["sha256"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise BuildError(f"{target}.sha256 不合法")
    return data


def _validate_archive_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise BuildError(f"运行时归档路径逃出目标目录：{member.name!r}")
    if member.isdev() or member.isfifo():
        raise BuildError(f"运行时归档包含特殊文件：{member.name!r}")
    if member.issym() or member.islnk():
        link = PurePosixPath(member.linkname)
        if link.is_absolute() or ".." in link.parts:
            raise BuildError(f"运行时归档链接逃出目标目录：{member.name!r}")


def safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                _validate_archive_member(member)
                target = (root / member.name).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise BuildError(
                        f"运行时归档路径逃出目标目录：{member.name!r}"
                    ) from exc
            tar.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, tarfile.FilterError) as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"无法安全解包运行时归档：{exc}") from exc


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise BuildError(f"构建输入目录不存在：{source}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def _remove_python_caches(root: Path) -> None:
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)
    for path in root.rglob("*.pyc"):
        path.unlink(missing_ok=True)


def _prune_runtime(root: Path) -> None:
    runtime = root / "python"
    for relative in (
        "include",
        "libs",
        "share",
        "lib/python3.12/ensurepip",
        "lib/python3.12/idlelib",
        "lib/python3.12/lib2to3",
        "lib/python3.12/site-packages",
        "lib/python3.12/test",
        "lib/python3.12/tkinter",
        "lib/python3.12/turtledemo",
        "Lib/ensurepip",
        "Lib/idlelib",
        "Lib/lib2to3",
        "Lib/site-packages",
        "Lib/test",
        "Lib/tkinter",
        "Lib/turtledemo",
    ):
        path = runtime / relative
        if path.is_dir():
            shutil.rmtree(path)
    for pattern in ("lib/*tcl*", "lib/*tk*"):
        for path in runtime.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
    for pattern in (
        "bin/2to3*",
        "bin/idle*",
        "bin/pip*",
        "bin/pydoc*",
        "bin/*-config",
        "Scripts/*",
    ):
        for path in runtime.glob(pattern):
            path.unlink(missing_ok=True)


def _python_entry(root: Path, target: str) -> Path:
    if target.startswith("windows"):
        return root / "python" / "python.exe"
    return root / "python" / "bin" / "python3"


def _collect_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "release-manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256(path)
    return files


def prepare_bundle(
    *,
    repo_root: Path,
    target: str,
    version: str,
    commit: str,
    runtime_archive: Path,
    python_version: str,
    python_build_release: str,
    out_dir: Path,
    runtime_sha256: str | None = None,
    allow_unbound_source: bool = False,
) -> Path:
    if target not in TARGETS:
        raise BuildError(f"未知 target：{target}")
    runtime_archive = Path(runtime_archive)
    expected = runtime_sha256 or _sha256(runtime_archive)
    actual = _sha256(runtime_archive)
    if actual != expected:
        raise BuildError(f"Python runtime SHA256 不匹配：期望 {expected}，实际 {actual}")
    if not allow_unbound_source:
        verify_source_identity(Path(repo_root), commit)

    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    safe_extract_tar(runtime_archive, out_dir)
    _prune_runtime(out_dir)
    _remove_python_caches(out_dir)
    if not _python_entry(out_dir, target).is_file():
        raise BuildError("Python runtime 归档缺少目标解释器")

    _copy_tree(Path(repo_root) / "src" / "tianshu", out_dir / "app" / "tianshu")
    _copy_tree(Path(repo_root) / "ui" / "dist", out_dir / "ui" / "dist")
    license_path = Path(repo_root) / "LICENSE"
    if not license_path.is_file():
        raise BuildError("构建输入缺少 LICENSE")
    (out_dir / "licenses").mkdir()
    shutil.copy2(license_path, out_dir / "licenses" / "LICENSE")

    identity = {
        "schema": 1,
        "product": "tianshu-agent",
        "version": version,
        "commit": commit,
        "target": target,
    }
    (out_dir / "release.json").write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    files = _collect_files(out_dir)
    ui_relative = "ui/dist/index.html"
    if ui_relative not in files:
        raise BuildError("UI bundle 缺少 ui/dist/index.html")
    manifest = {
        **identity,
        "python": python_version,
        "python_build_release": python_build_release,
        "ui_dist_sha256": files[ui_relative],
        "files": files,
    }
    manifest_path = out_dir / "release-manifest.json"
    write_manifest(manifest_path, manifest)
    try:
        validate_manifest(manifest, base_dir=out_dir)
    except ReleaseManifestError as exc:
        raise BuildError(str(exc)) from exc
    return manifest_path


def download_runtime(lock_path: Path, target: str, cache_dir: Path) -> Path:
    lock = load_runtime_lock(lock_path)
    entry = lock["targets"][target]
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / entry["asset"]
    if destination.is_file() and _sha256(destination) == entry["sha256"]:
        return destination
    url = str(lock["source"]).rstrip("/") + "/" + entry["asset"]
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache_dir)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, Path(temporary).open(
            "wb"
        ) as output:
            shutil.copyfileobj(response, output)
        if _sha256(Path(temporary)) != entry["sha256"]:
            raise BuildError("下载的 Python runtime SHA256 不匹配")
        Path(temporary).replace(destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument(
        "--runtime-lock",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "release" / "python-runtime.lock.json",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tianshu")
    args = parser.parse_args(argv)
    lock = load_runtime_lock(args.runtime_lock)
    verify_source_identity(args.repo_root, args.commit)
    entry = lock["targets"][args.target]
    runtime = args.runtime_archive or download_runtime(
        args.runtime_lock, args.target, args.cache_dir
    )
    manifest = prepare_bundle(
        repo_root=args.repo_root,
        target=args.target,
        version=args.version,
        commit=args.commit,
        runtime_archive=runtime,
        python_version=lock["python"],
        python_build_release=lock["release"],
        out_dir=args.out,
        runtime_sha256=entry["sha256"],
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
