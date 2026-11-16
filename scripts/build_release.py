from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from release_manifest import (  # noqa: E402
    ReleaseManifestError,
    read_manifest,
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
EXPECTED_PYTHON = "3.12.16"
EXPECTED_RELEASE = "20261110"
RUNTIME_SOURCE = "https://github.com/astral-sh/python-build-standalone"
RUNTIME_LOCK_PATH = PurePosixPath("release/python-runtime.lock.json")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^0\.[0-9]+\.[0-9]+$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?$",
    re.IGNORECASE,
)
_FORBIDDEN_INPUT_PARTS = frozenset(
    {
        ".git",
        ".omc",
        ".tianshu",
        ".venv",
        "__pycache__",
        "logs",
        "refs",
        "tests",
    }
)
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".db-journal",
        ".db-shm",
        ".db-wal",
        ".key",
        ".log",
        ".p12",
        ".pem",
        ".pfx",
        ".sqlite",
        ".sqlite-journal",
        ".sqlite-shm",
        ".sqlite-wal",
        ".sqlite3",
        ".sqlite3-journal",
        ".sqlite3-shm",
        ".sqlite3-wal",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)


class BuildError(ValueError):
    """发行构建输入或中间产物不满足约束。"""


def _is_link(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or (junction_check is not None and junction_check())


def _validate_directory_chain(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    relative_parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in relative_parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise BuildError(f"无法检查{label}路径祖先：{current}：{exc}") from exc
        if _is_link(current):
            raise BuildError(f"{label}路径不得经过链接：{current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise BuildError(f"{label}路径祖先必须是普通目录：{current}")
    return absolute


def _validated_staging_destination(root: Path, relative: str) -> Path:
    _validate_input_path(relative)
    resolved_root = Path(root).resolve()
    target = resolved_root.joinpath(*PurePosixPath(relative).parts)
    try:
        target.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise BuildError(f"构建输入映射后逃出 staging：{relative!r}") from exc
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git_bytes(repo_root: Path, *args: str) -> bytes:
    environment = dict(os.environ)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            env=environment,
        )
    except OSError as exc:
        raise BuildError(f"无法运行 Git 校验发行源：{exc}") from exc
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        raise BuildError(f"Git 校验失败：{diagnostic or '未知错误'}")
    return result.stdout


def _run_git_text(repo_root: Path, *args: str) -> str:
    try:
        return _run_git_bytes(repo_root, *args).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BuildError("Git 输出不是 UTF-8，无法绑定发行源身份") from exc


def verify_source_identity(repo_root: Path, commit: str) -> None:
    repo_root = Path(repo_root).resolve()
    if _COMMIT_RE.fullmatch(commit) is None:
        raise BuildError("发行 commit 必须是 40 位小写十六进制 SHA")
    top_level = Path(_run_git_text(repo_root, "rev-parse", "--show-toplevel")).resolve()
    if top_level != repo_root:
        raise BuildError(f"repo_root 不是 Git 根目录：{repo_root}")
    head = _run_git_text(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    if head != commit:
        raise BuildError(f"源 HEAD {head} 与发行 commit {commit} 不一致")
    status = _run_git_text(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if status:
        raise BuildError("发行构建要求干净工作树")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复字段：{key}")
        result[key] = value
    return result


def _parse_runtime_lock(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        data = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, ValueError) as exc:
        raise BuildError(f"无法解析 Python runtime lock {source}：{exc}") from exc
    if not isinstance(data, dict):
        raise BuildError("Python runtime lock 根必须是 object")
    if set(data) != {"schema", "release", "python", "source", "targets"}:
        raise BuildError("Python runtime lock 字段必须封闭")
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise BuildError("不支持的 Python runtime lock schema")
    if data["python"] != EXPECTED_PYTHON:
        raise BuildError(f"Python runtime lock 必须固定为 {EXPECTED_PYTHON}")
    if data["release"] != EXPECTED_RELEASE:
        raise BuildError(f"Python runtime release tag 必须固定为 {EXPECTED_RELEASE}")
    if data["source"] != RUNTIME_SOURCE:
        raise BuildError("Python runtime source 必须固定为官方仓库")

    targets = data["targets"]
    if not isinstance(targets, dict) or set(targets) != TARGETS:
        raise BuildError("Python runtime lock 必须精确覆盖四个桌面 target")
    for target in sorted(TARGETS):
        entry = targets[target]
        if not isinstance(entry, dict):
            raise BuildError(f"{target} runtime entry 必须是 object")
        if set(entry) != {"triple", "asset", "url", "sha256"}:
            raise BuildError(f"{target} runtime entry 字段必须封闭")
        for field in ("triple", "asset", "url", "sha256"):
            if not isinstance(entry[field], str) or not entry[field]:
                raise BuildError(f"{target}.{field} 必须是非空字符串")

        triple = TARGET_TRIPLES[target]
        if entry["triple"] != triple:
            raise BuildError(f"{target}.triple 必须是 {triple}，实际为 {entry['triple']}")
        expected_asset = (
            f"cpython-{EXPECTED_PYTHON}+{EXPECTED_RELEASE}-{triple}-install_only.tar.gz"
        )
        if entry["asset"] != expected_asset:
            raise BuildError(
                f"{target}.asset 必须绑定 target/version/release，期望 {expected_asset}"
            )
        encoded_asset = urllib.parse.quote(expected_asset, safe="-._~")
        expected_url = f"{RUNTIME_SOURCE}/releases/download/{EXPECTED_RELEASE}/{encoded_asset}"
        if entry["url"] != expected_url:
            raise BuildError(f"{target}.url 必须固定为官方下载地址")
        if _SHA256_RE.fullmatch(entry["sha256"]) is None:
            raise BuildError(f"{target}.sha256 不合法")
    return data


def load_runtime_lock(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"无法读取 Python runtime lock：{exc}") from exc
    return _parse_runtime_lock(raw, source=str(path))


def load_committed_runtime_lock(repo_root: Path, commit: str) -> dict[str, Any]:
    path = RUNTIME_LOCK_PATH.as_posix()
    raw = _run_git_bytes(
        repo_root,
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        path,
    )
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise BuildError(f"发行 commit 必须精确包含唯一 {path}")
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode_text, kind, object_id = metadata.split(b" ", 2)
        listed_path = raw_path.decode("utf-8", errors="strict")
        mode = int(mode_text, 8)
        oid = object_id.decode("ascii", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise BuildError(f"{path} Git 清单格式不合法") from exc
    if listed_path != path or mode != 0o100644 or kind != b"blob":
        raise BuildError(f"{path} 必须是发行 commit 内唯一的普通 Git blob")
    lock_bytes = _run_git_bytes(repo_root, "cat-file", "blob", oid)
    return _parse_runtime_lock(lock_bytes, source=f"{commit}:{path}")


def _validate_member_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise BuildError(f"运行时归档路径不合法：{name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "python"
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise BuildError(f"运行时归档路径必须位于精确的 python/ 根：{name!r}")
    return path


def _member_type_error(member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        raise BuildError(f"运行时归档链接必须拒绝：{member.name!r}")
    if not member.isdir() and not member.isreg():
        raise BuildError(f"运行时归档包含特殊文件：{member.name!r}")


def _validate_internal_link(linkname: str) -> PurePosixPath:
    if not isinstance(linkname, str) or not linkname or "\\" in linkname:
        raise BuildError(f"运行时归档链接不合法：{linkname!r}")
    path = PurePosixPath(linkname)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BuildError(f"运行时归档链接逃出目标目录：{linkname!r}")
    return path


def _validated_destination(root: Path, name: str) -> Path:
    relative = _validate_member_name(name)
    destination = root.joinpath(*relative.parts)
    resolved_root = root.resolve()
    resolved_parent = destination.parent.resolve()
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise BuildError(f"运行时归档路径逃出目标目录：{name!r}") from exc
    return destination


def _extract_verified_members(
    archive: Path,
    destination: Path,
    *,
    materialize_internal_links: bool,
    expected_sha256: str | None,
) -> None:
    with Path(archive).open("rb") as raw:
        if expected_sha256 is not None:
            digest = hashlib.sha256()
            for chunk in iter(lambda: raw.read(1024 * 1024), b""):
                digest.update(chunk)
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise BuildError(
                    f"Python runtime SHA256 不匹配：期望 {expected_sha256}，实际 {actual}"
                )
            raw.seek(0)

        aliases: list[tuple[str, str]] = []
        seen: set[str] = set()
        with tarfile.open(fileobj=raw, mode="r:gz") as runtime:
            members = runtime.getmembers()
            for member in members:
                _validate_member_name(member.name)
                key = member.name.casefold()
                if key in seen:
                    raise BuildError(f"运行时归档包含重复路径：{member.name!r}")
                seen.add(key)
                if member.issym() and materialize_internal_links:
                    link = _validate_internal_link(member.linkname)
                    target = (PurePosixPath(member.name).parent / link).as_posix()
                    _validate_member_name(target)
                    aliases.append((member.name, target))
                    continue
                _member_type_error(member)
            if not seen:
                raise BuildError("运行时归档为空")
            alias_names = {name for name, _ in aliases}

            for member in members:
                if member.name in alias_names:
                    continue
                target = _validated_destination(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, mode=0o755, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
                source = runtime.extractfile(member)
                if source is None:
                    raise BuildError(f"无法读取运行时归档成员：{member.name!r}")
                try:
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                except OSError as exc:
                    raise BuildError(f"无法安全解包运行时归档：{exc}") from exc
                target.chmod(0o755 if member.mode & 0o111 else 0o644)

        for name, linkname in aliases:
            alias = _validated_destination(destination, name)
            source = _validated_destination(destination, linkname)
            if _is_link(source) or not source.is_file():
                raise BuildError(f"运行时归档链接目标必须是内部普通文件：{name!r} -> {linkname!r}")
            if alias.exists() or _is_link(alias):
                raise BuildError(f"运行时归档链接与文件冲突：{name!r}")
            alias.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            shutil.copyfile(source, alias)
            alias.chmod(stat.S_IMODE(source.stat().st_mode))


def safe_extract_tar(
    archive: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    archive = Path(archive)
    destination = Path(destination)
    if _is_link(archive) or not archive.is_file():
        raise BuildError(f"Python runtime 归档必须是普通文件：{archive}")
    if destination.exists() or _is_link(destination):
        raise BuildError(f"运行时解包目标必须不存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.extract-", dir=destination.parent)
    )
    try:
        _extract_verified_members(
            archive,
            temporary,
            materialize_internal_links=False,
            expected_sha256=expected_sha256,
        )
        temporary.replace(destination)
    except (OSError, tarfile.TarError) as exc:
        raise BuildError(f"无法安全解包运行时归档：{exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _validate_input_path(relative: str) -> None:
    if "\\" in relative:
        raise BuildError(f"构建输入路径不得包含 Windows 反斜杠：{relative!r}")
    if ":" in relative:
        raise BuildError(f"构建输入路径不得包含 Windows 盘符或 ADS 冒号：{relative!r}")
    if _CONTROL_CHARACTER_RE.search(relative):
        raise BuildError(f"构建输入路径不得包含控制字符：{relative!r}")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BuildError(f"构建输入路径不合法：{relative!r}")
    for part in path.parts:
        if part.endswith((".", " ")):
            raise BuildError(f"构建输入路径不得以点或空格结尾：{relative!r}")
        if (
            PureWindowsPath(part).is_reserved()
            or _WINDOWS_RESERVED_NAME_RE.fullmatch(part)
        ):
            raise BuildError(f"构建输入路径不得使用 Windows 保留设备名：{relative!r}")
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & _FORBIDDEN_INPUT_PARTS:
        raise BuildError(f"构建输入包含禁止目录：{relative!r}")
    lowered_name = path.name.lower()
    if (
        lowered_name.startswith(".env")
        or lowered_name in _SENSITIVE_NAMES
        or any(lowered_name.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES)
    ):
        raise BuildError(f"构建输入包含凭据或用户数据：{relative!r}")


def _is_ignored_cache_entry(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix.lower() == ".pyc" or path.name == ".DS_Store"


def _reject_unsafe_worktree_entries(root: Path) -> None:
    if _is_link(root) or not root.is_dir():
        raise BuildError(f"构建输入目录不存在或是链接：{root}")
    for path in sorted(root.rglob("*")):
        if _is_ignored_cache_entry(path):
            continue
        relative = path.relative_to(root).as_posix()
        if _is_link(path):
            raise BuildError(f"构建输入包含链接：{relative!r}")
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise BuildError(f"构建输入包含特殊文件：{relative!r}")
        _validate_input_path(relative)


def _commit_file_specs(
    repo_root: Path,
    commit: str,
) -> list[tuple[str, str, int]]:
    raw = _run_git_bytes(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        "--",
        "src/tianshu",
        "ui/dist",
        "LICENSE",
    )
    specs: list[tuple[str, str, int]] = []
    seen_casefold: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode_text, kind, object_id = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            mode = int(mode_text, 8)
            oid = object_id.decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise BuildError("Git 文件清单格式不合法") from exc
        folded = path.casefold()
        previous = seen_casefold.get(folded)
        if previous is not None:
            raise BuildError(f"Git 文件清单包含大小写冲突路径：{previous!r} 与 {path!r}")
        seen_casefold[folded] = path
        if mode not in {0o100644, 0o100755} or kind != b"blob":
            raise BuildError(f"构建输入必须是普通 Git blob：{path}")
        if not (
            path == "LICENSE" or path.startswith("src/tianshu/") or path.startswith("ui/dist/")
        ):
            raise BuildError(f"构建输入越过允许目录：{path}")
        _validate_input_path(path)
        specs.append((path, oid, mode))
    if not any(path.startswith("src/tianshu/") for path, _, _ in specs):
        raise BuildError("Git commit 缺少 src/tianshu")
    if not any(path.startswith("ui/dist/") for path, _, _ in specs):
        raise BuildError("Git commit 缺少 ui/dist")
    if not any(path == "LICENSE" for path, _, _ in specs):
        raise BuildError("Git commit 缺少 LICENSE")
    return specs


def _copy_tracked_inputs(repo_root: Path, commit: str, destination: Path) -> None:
    _reject_unsafe_worktree_entries(repo_root / "src" / "tianshu")
    _reject_unsafe_worktree_entries(repo_root / "ui" / "dist")
    for relative, oid, mode in _commit_file_specs(repo_root, commit):
        body = _run_git_bytes(repo_root, "cat-file", "blob", oid)
        if relative == "LICENSE":
            output_relative = "licenses/LICENSE"
        elif relative.startswith("src/tianshu/"):
            output_relative = "app/tianshu/" + relative.removeprefix("src/tianshu/")
        else:
            output_relative = relative
        target = _validated_staging_destination(destination, output_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(body)
        except OSError as exc:
            raise BuildError(f"无法安全复制构建输入：{relative}") from exc
        target.chmod(0o755 if mode == 0o100755 else 0o644)


def _copy_unbound_input(source: Path, destination: Path) -> None:
    _reject_unsafe_worktree_entries(source)
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if _is_ignored_cache_entry(path):
            continue
        relative = path.relative_to(source).as_posix()
        target = _validated_staging_destination(destination, relative)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("rb") as incoming, target.open("xb") as output:
                shutil.copyfileobj(incoming, output, length=1024 * 1024)
        except OSError as exc:
            raise BuildError(f"无法安全复制构建输入：{relative}") from exc
        target.chmod(0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644)


def _remove_python_caches(root: Path) -> None:
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)
    for path in root.rglob("*.pyc"):
        path.unlink(missing_ok=True)


def _prune_runtime(root: Path, target: str) -> None:
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
        if _is_link(path):
            raise BuildError(f"待裁剪运行时路径是链接：{relative}")
        if path.is_dir():
            shutil.rmtree(path)
    if target == "windows-arm64":
        # The pinned upstream archive carries this x64 DLL, but no retained PE
        # imports it. Keep the native vcruntime140.dll and remove the unneeded
        # wrong-architecture file rather than weakening the verifier.
        mismatched = runtime / "vcruntime140_1.dll"
        if _is_link(mismatched):
            raise BuildError(
                f"待裁剪运行时路径是链接：{mismatched.relative_to(runtime)}"
            )
        if mismatched.is_dir():
            raise BuildError(
                f"待裁剪运行时路径必须是普通文件："
                f"{mismatched.relative_to(runtime)}"
            )
        mismatched.unlink(missing_ok=True)
    for pattern in (
        "bin/2to3*",
        "bin/idle*",
        "bin/pip*",
        "bin/pydoc*",
        "bin/*-config",
        "Scripts/*",
    ):
        for path in runtime.glob(pattern):
            if _is_link(path):
                raise BuildError(f"待裁剪运行时路径是链接：{path.relative_to(runtime)}")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)


def _python_entry(root: Path, target: str) -> Path:
    if target.startswith("windows"):
        return root / "python" / "python.exe"
    return root / "python" / "bin" / "python3"


def _archive_python_entry_is_executable(archive: Path, target: str) -> bool:
    entry_name = "python/python.exe" if target.startswith("windows") else "python/bin/python3"
    with tarfile.open(archive, "r:gz") as runtime:
        members = {member.name: member for member in runtime.getmembers()}
    entry = members.get(entry_name)
    if entry is None:
        raise BuildError("Python runtime 归档缺少目标解释器")
    if target.startswith("windows"):
        return True
    if entry.issym():
        resolved = (PurePosixPath(entry_name).parent / entry.linkname).as_posix()
        target_member = members.get(resolved)
        if target_member is None or not target_member.isreg():
            return False
        return bool(target_member.mode & 0o111)
    return entry.isreg() and bool(entry.mode & 0o111)


def _assert_regular_tree(root: Path) -> None:
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if _is_link(path):
            raise BuildError(f"发行物包含链接：{relative}")
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise BuildError(f"发行物包含特殊文件：{relative}")
        _validate_input_path(relative)


def _collect_files(root: Path) -> dict[str, str]:
    _assert_regular_tree(root)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "release-manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256(path)
    return files


def _publish_marker_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.publish.json")


def _publish_lock_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.publish.lock")


def _staging_owner_path(output: Path, token: str) -> Path:
    return output.with_name(f".{output.name}.staging-{token}.json")


@contextmanager
def _publish_lock(output: Path):
    lock_path = _publish_lock_path(output)
    if _is_link(lock_path):
        raise BuildError(f"发布目录锁不得是链接：{lock_path}")
    flags = os.O_CREAT | os.O_RDWR
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise BuildError(f"无法打开发布目录锁：{exc}") from exc
    locked = False
    try:
        if os.name == "nt":
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BuildError(f"另一个发布进程正在使用输出目录：{output}") from exc
        else:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BuildError(f"另一个发布进程正在使用输出目录：{output}") from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_publish_marker(
    marker: Path,
    *,
    output: Path,
    staging: Path,
    backup: Path,
) -> None:
    payload = {
        "schema": 1,
        "output": output.name,
        "staging": staging.name,
        "backup": backup.name,
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.publish-intent-",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(marker)
    except OSError as exc:
        raise BuildError(f"无法持久化发布恢复记录：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_publish_marker(marker: Path, output: Path) -> tuple[Path, Path]:
    if _is_link(marker) or not marker.is_file():
        raise BuildError(f"发布恢复记录必须是普通文件：{marker}")
    try:
        data = json.loads(
            marker.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, ValueError) as exc:
        raise BuildError(f"发布恢复记录无效：{exc}") from exc
    if not isinstance(data, dict) or set(data) != {"schema", "output", "staging", "backup"}:
        raise BuildError("发布恢复记录字段必须封闭")
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise BuildError("不支持的发布恢复记录 schema")
    if data["output"] != output.name:
        raise BuildError("发布恢复记录与输出目录不匹配")
    for field, prefix in (
        ("staging", f".{output.name}.build-"),
        ("backup", f".{output.name}.backup-"),
    ):
        name = data[field]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith(prefix)
            or len(name) == len(prefix)
        ):
            raise BuildError(f"发布恢复记录的 {field} 路径不合法")
    staging = output.parent / data["staging"]
    backup = output.parent / data["backup"]
    if staging == backup:
        raise BuildError("发布恢复记录的 staging/backup 不得相同")
    return staging, backup


def _require_plain_directory(path: Path, label: str) -> None:
    if _is_link(path) or not path.is_dir():
        raise BuildError(f"发布恢复的 {label} 必须是普通目录：{path}")


def _validate_existing_release_directory(output: Path) -> None:
    manifest_path = output / "release-manifest.json"
    if _is_link(manifest_path) or not manifest_path.is_file():
        raise BuildError("已有输出目录必须包含普通 release-manifest.json")
    _assert_regular_tree(output)
    try:
        previous = read_manifest(manifest_path, base_dir=output)
    except ReleaseManifestError as exc:
        raise BuildError(f"已有输出 manifest 无效：{exc}") from exc
    actual_files = {
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    }
    expected_files = set(previous["files"]) | {"release-manifest.json"}
    if actual_files != expected_files:
        raise BuildError("已有输出文件集合与 manifest 不一致")


def _remove_publish_temporary(path: Path, label: str) -> None:
    if _is_link(path):
        raise BuildError(f"发布恢复的 {label} 不得是链接：{path}")
    if path.exists():
        if not path.is_dir():
            raise BuildError(f"发布恢复的 {label} 必须是普通目录：{path}")
        shutil.rmtree(path)


def _remove_publish_marker(marker: Path) -> None:
    if _is_link(marker):
        raise BuildError(f"发布恢复记录不得是链接：{marker}")
    marker.unlink(missing_ok=True)


def _write_staging_owner(output: Path, staging: Path, token: str) -> Path:
    marker = _staging_owner_path(output, token)
    payload = {
        "schema": 1,
        "output": output.name,
        "staging": staging.name,
        "token": token,
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.staging-intent-",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(marker)
    except OSError as exc:
        raise BuildError(f"无法持久化 staging 所有权记录：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return marker


def _read_staging_owner(marker: Path, output: Path) -> Path:
    if _is_link(marker) or not marker.is_file():
        raise BuildError(f"staging 所有权记录必须是普通文件：{marker}")
    try:
        data = json.loads(
            marker.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, ValueError) as exc:
        raise BuildError(f"staging 所有权记录无效：{exc}") from exc
    if not isinstance(data, dict) or set(data) != {"schema", "output", "staging", "token"}:
        raise BuildError("staging 所有权记录字段必须封闭")
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise BuildError("不支持的 staging 所有权记录 schema")
    if data["output"] != output.name:
        raise BuildError("staging 所有权记录与输出目录不匹配")
    token = data["token"]
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise BuildError("staging 所有权 token 不合法")
    expected_marker = _staging_owner_path(output, token).name
    expected_staging = f".{output.name}.build-{token}"
    if marker.name != expected_marker or data["staging"] != expected_staging:
        raise BuildError("staging 所有权记录路径不合法")
    return output.parent / expected_staging


def _remove_staging_owner(marker: Path) -> None:
    if _is_link(marker):
        raise BuildError(f"staging 所有权记录不得是链接：{marker}")
    marker.unlink(missing_ok=True)


def _recover_owned_staging(output: Path) -> None:
    prefix = f".{output.name}.staging-"
    try:
        candidates = sorted(
            path for path in output.parent.iterdir() if path.name.startswith(prefix)
        )
    except OSError as exc:
        raise BuildError(f"无法扫描 staging 所有权记录：{exc}") from exc
    for marker in candidates:
        staging = _read_staging_owner(marker, output)
        if _is_link(staging):
            raise BuildError(f"staging 所有权目标不得是链接：{staging}")
        if staging.exists() and not staging.is_dir():
            raise BuildError(f"staging 所有权目标必须是普通目录：{staging}")
        if staging.exists():
            shutil.rmtree(staging)
        _remove_staging_owner(marker)


def _create_owned_staging(output: Path) -> tuple[Path, Path]:
    token = uuid.uuid4().hex
    staging = output.with_name(f".{output.name}.build-{token}")
    marker = _write_staging_owner(output, staging, token)
    try:
        staging.mkdir(mode=0o700)
    except OSError as exc:
        try:
            _remove_staging_owner(marker)
        except BuildError as cleanup_exc:
            raise BuildError(f"无法创建 staging 且所有权记录清理失败：{cleanup_exc}") from exc
        raise BuildError(f"无法创建 staging：{exc}") from exc
    return staging, marker


def _recover_publish_transaction_locked(output: Path, marker: Path) -> None:
    staging, backup = _read_publish_marker(marker, output)
    output_exists = output.exists() or _is_link(output)
    staging_exists = staging.exists() or _is_link(staging)
    backup_exists = backup.exists() or _is_link(backup)
    if output_exists:
        _require_plain_directory(output, "output")
    if staging_exists:
        _require_plain_directory(staging, "staging")
    if backup_exists:
        _require_plain_directory(backup, "backup")

    if output_exists:
        if staging_exists and backup_exists:
            raise BuildError("发布恢复现场同时存在 output/staging/backup，拒绝猜测")
        if staging_exists:
            _remove_publish_temporary(staging, "staging")
        else:
            _validate_existing_release_directory(output)
        _remove_publish_temporary(backup, "backup")
        _remove_publish_marker(marker)
        return

    if backup_exists:
        if staging_exists:
            staging.replace(output)
        else:
            backup.replace(output)
        _validate_existing_release_directory(output)
        _remove_publish_temporary(backup, "backup")
        _remove_publish_marker(marker)
        return

    _remove_publish_temporary(staging, "staging")
    _remove_publish_marker(marker)


def _recover_publish_transaction(output: Path) -> None:
    marker = _publish_marker_path(output)
    if not marker.exists() and not _is_link(marker):
        return
    with _publish_lock(output):
        if marker.exists() or _is_link(marker):
            _recover_publish_transaction_locked(output, marker)


def _release_failpoint(name: str) -> None:
    if os.environ.get("TIANSHU_FAILPOINT") == name:
        os._exit(77)


def _validated_output(repo_root: Path, out_dir: Path) -> Path:
    repo_root = repo_root.resolve()
    requested = Path(out_dir).expanduser()
    if _is_link(requested):
        raise BuildError(f"显式输出目录不得是链接：{requested}")
    output = requested.resolve()
    if output == repo_root or repo_root.is_relative_to(output):
        raise BuildError("输出目录不得等于或包含发布源仓库")
    for protected in (repo_root / "src" / "tianshu", repo_root / "ui" / "dist"):
        if output == protected.resolve() or output.is_relative_to(protected.resolve()):
            raise BuildError(f"输出目录不得覆盖构建输入：{protected}")
    _recover_publish_transaction(output)
    _validate_output_directory_for_build(output)
    return output


def _validate_output_directory_for_build(output: Path) -> None:
    if output.exists() or _is_link(output):
        _require_plain_directory(output, "output")
        if any(output.iterdir()):
            _validate_existing_release_directory(output)


def _publish_staged_directory_locked(staging: Path, output: Path) -> None:
    _require_plain_directory(staging, "staging")
    marker = _publish_marker_path(output)
    backup = output.with_name(f".{output.name}.backup-{uuid.uuid4().hex}")
    if marker.exists() or _is_link(marker):
        _recover_publish_transaction_locked(output, marker)
    _validate_output_directory_for_build(output)
    _write_publish_marker(
        marker,
        output=output,
        staging=staging,
        backup=backup,
    )
    try:
        if output.exists():
            output.replace(backup)
        _release_failpoint("release.publish.old_moved")
        staging.replace(output)
        _release_failpoint("release.publish.new_moved")
        _validate_existing_release_directory(output)
        _remove_publish_temporary(backup, "backup")
        _remove_publish_marker(marker)
    except BaseException as exc:
        try:
            _recover_publish_transaction_locked(output, marker)
        except BaseException as recovery_exc:
            exc.add_note(f"发布事务恢复失败：{recovery_exc}")
        raise


def _publish_staged_directory(staging: Path, output: Path) -> None:
    with _publish_lock(output):
        _publish_staged_directory_locked(staging, output)


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
    if _VERSION_RE.fullmatch(version) is None:
        raise BuildError("version 必须匹配 0.x.y")
    if _COMMIT_RE.fullmatch(commit) is None:
        raise BuildError("commit 必须是 40 位小写十六进制 SHA")
    if python_version != EXPECTED_PYTHON:
        raise BuildError(f"Python runtime 必须固定为 {EXPECTED_PYTHON}")
    if python_build_release != EXPECTED_RELEASE:
        raise BuildError(f"Python build release 必须固定为 {EXPECTED_RELEASE}")

    repo_root = Path(repo_root).resolve()
    runtime_archive = Path(runtime_archive)
    if _is_link(runtime_archive) or not runtime_archive.is_file():
        raise BuildError(f"Python runtime 归档必须是普通文件：{runtime_archive}")
    try:
        actual_sha256 = _sha256(runtime_archive)
    except OSError as exc:
        raise BuildError(f"无法读取 Python runtime 归档：{exc}") from exc
    expected_sha256 = runtime_sha256 or actual_sha256
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise BuildError("Python runtime SHA256 格式不合法")
    if actual_sha256 != expected_sha256:
        raise BuildError(
            f"Python runtime SHA256 不匹配：期望 {expected_sha256}，实际 {actual_sha256}"
        )
    try:
        if not _archive_python_entry_is_executable(runtime_archive, target):
            raise BuildError("Python runtime 目标解释器不可执行")
    except (OSError, tarfile.TarError) as exc:
        raise BuildError(f"无法检查 Python runtime 目标解释器：{exc}") from exc
    if not allow_unbound_source:
        verify_source_identity(repo_root, commit)
        _reject_unsafe_worktree_entries(repo_root / "src" / "tianshu")
        _reject_unsafe_worktree_entries(repo_root / "ui" / "dist")
    elif not repo_root.is_dir():
        raise BuildError(f"构建输入目录不存在：{repo_root}")

    output = _validated_output(repo_root, out_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    with _publish_lock(output):
        _recover_owned_staging(output)
        _validate_output_directory_for_build(output)
        staging, staging_owner = _create_owned_staging(output)
        try:
            _extract_verified_members(
                runtime_archive,
                staging,
                materialize_internal_links=True,
                expected_sha256=expected_sha256,
            )
            _prune_runtime(staging, target)
            _remove_python_caches(staging)
            entry = _python_entry(staging, target)
            if _is_link(entry) or not entry.is_file():
                raise BuildError("Python runtime 归档缺少目标解释器普通文件")

            if allow_unbound_source:
                _copy_unbound_input(repo_root / "src" / "tianshu", staging / "app" / "tianshu")
                _copy_unbound_input(repo_root / "ui" / "dist", staging / "ui" / "dist")
                license_path = repo_root / "LICENSE"
                if _is_link(license_path) or not license_path.is_file():
                    raise BuildError("构建输入缺少普通 LICENSE 文件")
                licenses = staging / "licenses"
                licenses.mkdir()
                shutil.copyfile(license_path, licenses / "LICENSE")
            else:
                _copy_tracked_inputs(repo_root, commit, staging)

            identity = {
                "schema": 1,
                "product": "tianshu-agent",
                "version": version,
                "commit": commit,
                "target": target,
            }
            release_path = staging / "release.json"
            release_path.write_text(
                json.dumps(identity, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            release_path.chmod(0o644)
            files = _collect_files(staging)
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
            manifest_path = staging / "release-manifest.json"
            write_manifest(manifest_path, manifest)
            try:
                validate_manifest(manifest, base_dir=staging)
            except ReleaseManifestError as exc:
                raise BuildError(str(exc)) from exc
            _publish_staged_directory_locked(staging, output)
            return output / "release-manifest.json"
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            _remove_staging_owner(staging_owner)


def _download_runtime_from_lock(lock: dict[str, Any], target: str, cache_dir: Path) -> Path:
    if target not in TARGETS:
        raise BuildError(f"未知 target：{target}")
    entry = lock["targets"][target]
    cache_dir = _validate_directory_chain(cache_dir, "runtime 缓存")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BuildError(f"无法创建 runtime 缓存目录：{exc}") from exc
    cache_dir = _validate_directory_chain(cache_dir, "runtime 缓存")
    if _is_link(cache_dir) or not cache_dir.is_dir():
        raise BuildError(f"runtime 缓存目录必须是普通目录：{cache_dir}")
    destination = cache_dir / entry["asset"]
    if _is_link(destination):
        raise BuildError(f"runtime 缓存不得是链接：{destination}")
    if destination.is_file():
        try:
            if _sha256(destination) == entry["sha256"]:
                return destination
        except OSError as exc:
            raise BuildError(f"无法读取 runtime 缓存：{exc}") from exc

    descriptor, temporary_name = tempfile.mkstemp(prefix=".download-", dir=cache_dir)
    temporary = Path(temporary_name)
    os.close(descriptor)
    try:
        request = urllib.request.Request(
            entry["url"],
            headers={"User-Agent": "tianshu-release-builder/1"},
        )
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output, length=1024 * 1024)
        actual = _sha256(temporary)
        if actual != entry["sha256"]:
            raise BuildError(
                f"下载的 Python runtime SHA256 不匹配：期望 {entry['sha256']}，实际 {actual}"
            )
        temporary.replace(destination)
    except (OSError, urllib.error.URLError) as exc:
        raise BuildError(f"无法下载 Python runtime：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def download_runtime(lock_path: Path, target: str, cache_dir: Path) -> Path:
    return _download_runtime_from_lock(load_runtime_lock(lock_path), target, cache_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tianshu")
    args = parser.parse_args(argv)
    verify_source_identity(args.repo_root, args.commit)
    lock = load_committed_runtime_lock(args.repo_root, args.commit)
    entry = lock["targets"][args.target]
    runtime = args.runtime_archive or _download_runtime_from_lock(
        lock,
        args.target,
        args.cache_dir,
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
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"发行构建失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
