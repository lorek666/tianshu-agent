from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_release import BuildError, load_runtime_lock  # noqa: E402
from release_manifest import ReleaseManifestError, read_manifest  # noqa: E402

TARGETS = frozenset({"macos-arm64", "macos-x64", "windows-x64", "windows-arm64"})
FORBIDDEN_PARTS = frozenset(
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
FORBIDDEN_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".log", ".key", ".pyc"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_IDENTITY_FIELDS = frozenset(
    {"schema", "product", "version", "commit", "target"}
)
VERSION_RE = re.compile(r"^0\.[0-9]+\.[0-9]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MACOS_ROOT = "Tianshu Agent.app"
MACOS_CONTENTS = f"{MACOS_ROOT}/Contents"
MACOS_RESOURCES = f"{MACOS_CONTENTS}/Resources"
ALLOWED_RESOURCE_ICONS = frozenset({"icon.icns", "icon.ico"})
NATIVE_SUFFIXES = frozenset({".dll", ".dylib", ".exe", ".pyd", ".so"})
MACOS_PYTHON_EXECUTABLES = frozenset(
    {"python/bin/python", "python/bin/python3", "python/bin/python3.12"}
)
# Tauri 2.12.4 ships NSIS 3.13 x86 stubs for every Windows payload architecture.
WINDOWS_NSIS_BOOTSTRAP_MACHINE = 0x014C
WINDOWS_NSIS_APP_EXECUTABLE = "tianshu-desktop.exe"
WINDOWS_NSIS_X86_SUPPORT_FILES = frozenset(
    {
        "$PLUGINSDIR/NSISdl.dll",
        "$PLUGINSDIR/StartMenu.dll",
        "$PLUGINSDIR/System.dll",
        "$PLUGINSDIR/nsDialogs.dll",
        "$PLUGINSDIR/nsis_tauri_utils.dll",
        "uninstall.exe",
    }
)
WINDOWS_NSIS_SUPPORT_FILES = WINDOWS_NSIS_X86_SUPPORT_FILES | {
    WINDOWS_NSIS_APP_EXECUTABLE,
    "$PLUGINSDIR/modern-wizard.bmp",
}
RUNTIME_SMOKE_TIMEOUT_S = 120.0
RUNTIME_SMOKE_TERM_TIMEOUT_S = 5.0
RUNTIME_SMOKE_TASKKILL_TIMEOUT_S = 10.0
RUNTIME_LOCK_PATH = SCRIPTS_DIR.parent / "release" / "python-runtime.lock.json"


class VerifyError(ValueError):
    """发行产物、清单或来源字节不一致。"""


def _failure(condition: bool, message: str) -> None:
    if not condition:
        raise VerifyError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_sha256(stream: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _validate_relative_path(raw: str) -> PurePosixPath:
    _failure(isinstance(raw, str) and raw != "", "files 的相对路径必须是非空字符串")
    _failure("\\" not in raw, f"发行物路径必须使用 /：{raw!r}")
    _failure("\0" not in raw, f"发行物路径含 NUL：{raw!r}")
    path = PurePosixPath(raw)
    _failure(not path.is_absolute(), f"发行物路径不得是绝对路径：{raw!r}")
    _failure(".." not in path.parts, f"发行物路径不得包含 ..：{raw!r}")
    _failure(
        path.parts and path.parts[0] not in {"", "."},
        f"发行物路径不合法：{raw!r}",
    )
    lowered = {part.lower() for part in path.parts}
    _failure(
        not (lowered & FORBIDDEN_PARTS),
        f"发行物包含禁止目录：{raw!r}",
    )
    _failure(
        path.suffix.lower() not in FORBIDDEN_SUFFIXES,
        f"发行物包含禁止文件：{raw!r}",
    )
    _failure(
        not path.name.lower().startswith(".env"),
        f"发行物包含环境文件：{raw!r}",
    )
    return path


def _validate_casefold_paths(paths: Iterable[str], label: str) -> None:
    casefolded: dict[str, str] = {}
    for path in paths:
        folded = path.casefold()
        previous = casefolded.setdefault(folded, path)
        _failure(
            previous == path,
            f"{label}包含大小写重复路径：{previous!r}/{path!r}",
        )


def _read_release_identity(data: bytes, label: str) -> dict[str, Any]:
    try:
        identity = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VerifyError(f"{label} 不是合法 JSON：{exc}") from exc
    _failure(isinstance(identity, dict), f"{label} 根必须是 object")
    assert isinstance(identity, dict)
    missing = RELEASE_IDENTITY_FIELDS - set(identity)
    unknown = set(identity) - RELEASE_IDENTITY_FIELDS
    if missing or unknown:
        raise VerifyError(
            f"{label} 字段必须封闭：缺少={sorted(missing)}，未知={sorted(unknown)}"
        )
    _failure(
        type(identity["schema"]) is int and identity["schema"] == 1,
        f"{label} 的 schema 不合法",
    )
    _failure(
        identity["product"] == "tianshu-agent",
        f"{label} 的 product 不合法",
    )
    _failure(
        isinstance(identity["version"], str)
        and VERSION_RE.fullmatch(identity["version"]) is not None,
        f"{label} 的 version 不合法",
    )
    _failure(
        isinstance(identity["commit"], str)
        and COMMIT_RE.fullmatch(identity["commit"]) is not None,
        f"{label} 的 commit 必须是 40 位小写十六进制 SHA",
    )
    _failure(
        isinstance(identity["target"], str) and identity["target"] in TARGETS,
        f"{label} 的 target 不合法：{identity['target']!r}",
    )
    return identity


def _identity_matches(
    actual: dict[str, Any],
    *,
    version: str,
    commit: str,
    target: str,
    label: str,
) -> None:
    for field, expected in (
        ("version", version),
        ("commit", commit),
        ("target", target),
    ):
        if actual.get(field) != expected:
            raise VerifyError(
                f"{label} {field} 不匹配：期望 {expected!r}，"
                f"实际 {actual.get(field)!r}"
            )


def _scan_bundle_paths(bundle_dir: Path) -> set[str]:
    root = bundle_dir.resolve()
    actual: set[str] = set()
    casefolded: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        _validate_relative_path(relative)
        folded = relative.casefold()
        previous = casefolded.setdefault(folded, relative)
        _failure(previous == relative, f"bundle 包含大小写重复路径：{previous!r}/{relative!r}")
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise VerifyError(f"bundle 包含损坏链接：{relative}") from exc
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise VerifyError(f"bundle 链接逃出发布根：{relative}") from exc
            actual.add(relative)
            continue
        if path.is_file():
            actual.add(relative)
            continue
        _failure(path.is_dir(), f"bundle 包含特殊文件：{relative}")
    return actual


def _verify_required_layout(root: Path, target: str, files: dict[str, str]) -> None:
    python = "python/python.exe" if target.startswith("windows") else "python/bin/python3"
    required = {
        "app/tianshu/__init__.py",
        "app/tianshu/__main__.py",
        "licenses/LICENSE",
        "release.json",
        "ui/dist/index.html",
        python,
    }
    missing = required - set(files)
    if missing:
        raise VerifyError(f"发布 bundle 缺少必需文件：{sorted(missing)}")


def _native_header(path: Path, limit: int = 65536) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _expect_macho(data: bytes, label: str, target: str) -> None:
    _failure(
        len(data) >= 8 and data[:4] == b"\xcf\xfa\xed\xfe",
        f"{label} 不是 64 位 thin Mach-O",
    )
    cpu_type = struct.unpack("<I", data[4:8])[0]
    expected = 0x0100000C if target.endswith("arm64") else 0x01000007
    _failure(
        cpu_type == expected,
        f"{label} CPU 架构与 target {target} 不匹配",
    )


def _expect_pe(data: bytes, label: str) -> int:
    _failure(len(data) >= 64 and data[:2] == b"MZ", f"{label} 不是 PE 文件")
    offset = struct.unpack("<I", data[0x3C:0x40])[0]
    _failure(
        offset + 6 <= len(data) and data[offset : offset + 4] == b"PE\0\0",
        f"{label} 缺少 PE header",
    )
    return struct.unpack("<H", data[offset + 4 : offset + 6])[0]


def _expect_arch(data: bytes, target: str, label: str) -> None:
    if target.startswith("macos"):
        _expect_macho(data, label, target)
        return
    machine = _expect_pe(data, label)
    expected = 0xAA64 if target.endswith("arm64") else 0x8664
    _failure(
        machine == expected,
        f"{label} PE 架构与 target {target} 不匹配",
    )


def _expect_windows_nsis_bootstrap(data: bytes, label: str) -> None:
    machine = _expect_pe(data, label)
    _failure(
        machine == WINDOWS_NSIS_BOOTSTRAP_MACHINE,
        f"{label} 外层 PE 不是 Tauri NSIS x86 引导器：0x{machine:04X}",
    )


def _path_has_native_payload(relative: str, target: str) -> bool:
    path = PurePosixPath(relative)
    if path.suffix.lower() in NATIVE_SUFFIXES:
        return True
    return (
        target.startswith("windows") and relative == "python/python.exe"
    ) or (
        target.startswith("macos") and relative in MACOS_PYTHON_EXECUTABLES
    )


def _verify_source_native_files(
    bundle_dir: Path,
    files: dict[str, str],
    target: str,
) -> None:
    python = "python/python.exe" if target.startswith("windows") else "python/bin/python3"
    _expect_arch(
        _native_header(bundle_dir / python),
        target,
        PurePosixPath(python).name,
    )
    for relative in sorted(files):
        if not _path_has_native_payload(relative, target):
            continue
        path = bundle_dir / relative
        if not path.is_file():
            raise VerifyError(f"原生文件不存在或不是普通文件：{relative}")
        _expect_arch(_native_header(path), target, relative)


def _verify_source_bundle(
    bundle_dir: Path,
    *,
    target: str,
    version: str,
    commit: str,
) -> tuple[dict[str, Any], bytes]:
    release_path = bundle_dir / "release.json"
    if not release_path.is_file():
        raise VerifyError("bundle 缺少 release.json")
    release_bytes = release_path.read_bytes()
    release_identity = _read_release_identity(
        release_bytes,
        "bundle release.json",
    )
    _identity_matches(
        release_identity,
        version=version,
        commit=commit,
        target=target,
        label="release.json",
    )
    manifest_path = bundle_dir / "release-manifest.json"
    try:
        manifest = read_manifest(manifest_path, base_dir=bundle_dir)
    except ReleaseManifestError as exc:
        raise VerifyError(str(exc)) from exc
    files = manifest["files"]
    assert isinstance(files, dict)
    actual = _scan_bundle_paths(bundle_dir)
    expected = set(files) | {"release-manifest.json"}
    unlisted = actual - expected
    missing = expected - actual
    if unlisted or missing:
        raise VerifyError(
            "bundle 文件集合与 manifest 不一致："
            f"未列文件={sorted(unlisted)}，缺失文件={sorted(missing)}"
        )
    _verify_required_layout(bundle_dir, target, files)
    ui_relative = "ui/dist/index.html"
    ui_digest = manifest.get("ui_dist_sha256")
    ui_bindings = [
        relative for relative, digest in files.items() if digest == ui_digest
    ]
    _failure(
        files.get(ui_relative) == ui_digest and ui_bindings == [ui_relative],
        "ui_dist_sha256 必须唯一精确绑定 bundle/ui/dist/index.html",
    )
    _identity_matches(
        manifest,
        version=version,
        commit=commit,
        target=target,
        label="发布清单",
    )
    _verify_source_native_files(bundle_dir, files, target)
    return manifest, manifest_path.read_bytes()


def _member_path(member: tarfile.TarInfo) -> PurePosixPath:
    raw = member.name.rstrip("/")
    path = PurePosixPath(raw)
    _failure(path.as_posix() == raw, f"归档路径未规范化：{member.name!r}")
    _validate_relative_path(raw)
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        _failure(
            not target.is_absolute() and "\\" not in member.linkname,
            f"归档包含逃逸链接：{member.name!r}",
        )
        base = path.parent if member.issym() else PurePosixPath()
        resolved = PurePosixPath(posixpath.normpath(str(base / target)))
        if not resolved.parts or ".." in resolved.parts or resolved.parts[0] != path.parts[0]:
            raise VerifyError(f"归档包含逃逸链接：{member.name!r}")
    else:
        _failure(
            member.isfile() or member.isdir(),
            f"归档包含特殊文件：{member.name!r}",
        )
    return path


def _tar_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    casefolded: dict[str, str] = {}
    for member in archive.getmembers():
        path = _member_path(member)
        name = path.as_posix()
        _failure(name not in members, f"归档包含重复成员：{name}")
        folded = name.casefold()
        previous = casefolded.setdefault(folded, name)
        _failure(previous == name, f"归档包含大小写重复成员：{previous!r}/{name!r}")
        members[name] = member
    for name, member in members.items():
        if not (member.issym() or member.islnk()):
            continue
        target = PurePosixPath(name).parent / member.linkname if member.issym() else PurePosixPath(
            member.linkname
        )
        resolved = posixpath.normpath(target.as_posix())
        _failure(resolved in members, f"归档链接目标不存在：{name} -> {resolved}")
    return members


def _resolve_archive_path(
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> tarfile.TarInfo:
    seen: set[str] = set()
    current = name
    while True:
        _failure(current not in seen, f"归档链接循环：{name}")
        seen.add(current)
        member = members.get(current)
        _failure(member is not None, f"归档缺少文件：{current}")
        assert member is not None
        if member.isfile():
            return member
        if member.issym():
            current = posixpath.normpath(
                str(PurePosixPath(current).parent / member.linkname)
            )
            continue
        if member.islnk():
            current = member.linkname
            continue
        raise VerifyError(f"归档路径不是可读取文件：{current}")


def _read_archive_bytes(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> bytes:
    member = _resolve_archive_path(members, name)
    stream = archive.extractfile(member)
    if stream is None:
        raise VerifyError(f"归档无法读取文件：{name}")
    return stream.read()


def _archive_sha256(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> str:
    member = _resolve_archive_path(members, name)
    stream = archive.extractfile(member)
    if stream is None:
        raise VerifyError(f"归档无法读取文件：{name}")
    return _stream_sha256(stream)


def _verify_macos_archive(
    *,
    artifact: Path,
    bundle_dir: Path,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    target: str,
) -> None:
    with tarfile.open(artifact, "r:gz") as archive:
        members = _tar_members(archive)
        roots = {PurePosixPath(name).parts[0] for name in members}
        if roots != {MACOS_ROOT}:
            raise VerifyError("macOS 归档必须只包含 Tianshu Agent.app")

        required = {
            f"{MACOS_CONTENTS}/Info.plist",
            f"{MACOS_CONTENTS}/MacOS/tianshu-desktop",
            f"{MACOS_RESOURCES}/release.json",
            f"{MACOS_RESOURCES}/release-manifest.json",
            f"{MACOS_RESOURCES}/ui/dist/index.html",
            f"{MACOS_RESOURCES}/python/bin/python3",
        }
        missing = required - set(members)
        if missing:
            raise VerifyError(f"macOS 归档缺少应用文件：{sorted(missing)}")

        release_bytes = _read_archive_bytes(
            archive,
            members,
            f"{MACOS_RESOURCES}/release.json",
        )
        release_identity = _read_release_identity(
            release_bytes,
            "macOS 归档 release.json",
        )
        _identity_matches(
            release_identity,
            version=manifest["version"],
            commit=manifest["commit"],
            target=target,
            label="macOS 归档 release.json",
        )
        if release_bytes != (bundle_dir / "release.json").read_bytes():
            raise VerifyError("归档 release.json 与已验证 bundle 字节不一致")
        if (
            _read_archive_bytes(
                archive,
                members,
                f"{MACOS_RESOURCES}/release-manifest.json",
            )
            != manifest_bytes
        ):
            raise VerifyError("归档 release-manifest.json 与已验证 bundle 字节不一致")
        if (
            _read_archive_bytes(
                archive,
                members,
                f"{MACOS_RESOURCES}/ui/dist/index.html",
            )
            != (bundle_dir / "ui" / "dist" / "index.html").read_bytes()
        ):
            raise VerifyError("归档 UI 与已验证 bundle 字节不一致")

        files = manifest["files"]
        assert isinstance(files, dict)
        for relative, expected in files.items():
            actual = _archive_sha256(
                archive,
                members,
                f"{MACOS_RESOURCES}/{relative}",
            )
            if actual != expected:
                raise VerifyError(
                    f"归档文件 SHA256 不匹配：{relative}；"
                    f"期望 {expected}，实际 {actual}"
                )

        resource_prefix = PurePosixPath(MACOS_RESOURCES)
        actual_resources = {
            path.relative_to(resource_prefix).as_posix()
            for name in members
            if not members[name].isdir()
            and (path := PurePosixPath(name)).is_relative_to(resource_prefix)
        }
        expected_resources = set(files) | {
            "release-manifest.json",
            *ALLOWED_RESOURCE_ICONS,
        }
        unlisted = actual_resources - expected_resources
        missing_payload = set(files) - actual_resources
        if unlisted or missing_payload:
            raise VerifyError(
                "macOS 归档资源集合与 manifest 不一致："
                f"未列文件={sorted(unlisted)}，缺失文件={sorted(missing_payload)}"
            )

        app_prefix = PurePosixPath(MACOS_ROOT)
        actual_app = {
            path.relative_to(app_prefix).as_posix()
            for name in members
            if (path := PurePosixPath(name)).is_relative_to(app_prefix)
        }
        # Historical unsigned/synthetic archives remain statically readable.
        # New generation requires native codesign checks in package_app.py;
        # this optional closed pair is not publisher or Apple trust validation.
        signature_directory = f"{MACOS_CONTENTS}/_CodeSignature"
        signature_file = f"{signature_directory}/CodeResources"
        has_envelope = signature_directory in members or signature_file in members
        if has_envelope:
            directory = members.get(signature_directory)
            envelope = members.get(signature_file)
            _failure(
                directory is not None and directory.isdir()
                and envelope is not None and envelope.isfile() and envelope.size > 0,
                "macOS 资源封套必须是精确目录及非空普通 CodeResources 文件",
            )

        expected_app_files = {
            "Contents/Info.plist",
            "Contents/MacOS/tianshu-desktop",
            "Contents/Resources/release-manifest.json",
        } | {f"Contents/Resources/{relative}" for relative in files}
        if has_envelope:
            expected_app_files.add("Contents/_CodeSignature/CodeResources")
        allowed_app_files = expected_app_files | {
            f"Contents/Resources/{name}" for name in ALLOWED_RESOURCE_ICONS
        }
        expected_app_directories: set[str] = set()
        for relative in expected_app_files:
            expected_app_directories.update(
                parent.as_posix()
                for parent in PurePosixPath(relative).parents
                if parent.as_posix() != "."
            )
        allowed_app_directories: set[str] = set()
        for relative in allowed_app_files:
            allowed_app_directories.update(
                parent.as_posix()
                for parent in PurePosixPath(relative).parents
                if parent.as_posix() != "."
            )
        expected_app = expected_app_files | expected_app_directories
        allowed_app = allowed_app_files | allowed_app_directories
        unexpected = actual_app - allowed_app
        absent = expected_app - actual_app
        if unexpected or absent:
            raise VerifyError(
                "macOS 应用文件集合不完整："
                f"未列条目={sorted(unexpected)}，缺失条目={sorted(absent)}"
            )

        contents_prefix = PurePosixPath(MACOS_CONTENTS)
        actual_contents = {
            path.relative_to(contents_prefix).as_posix()
            for name in members
            if not members[name].isdir()
            and (path := PurePosixPath(name)).is_relative_to(contents_prefix)
        }
        expected_contents = {
            "Info.plist",
            "MacOS/tianshu-desktop",
            "Resources/release-manifest.json",
        } | {f"Resources/{relative}" for relative in files}
        if has_envelope:
            expected_contents.add("_CodeSignature/CodeResources")
        allowed_contents = expected_contents | {
            f"Resources/{name}" for name in ALLOWED_RESOURCE_ICONS
        }
        unexpected = actual_contents - allowed_contents
        absent = expected_contents - actual_contents
        if unexpected or absent:
            raise VerifyError(
                "macOS 应用文件集合不完整："
                f"未列文件={sorted(unexpected)}，缺失文件={sorted(absent)}"
            )

        desktop = _read_archive_bytes(
            archive,
            members,
            f"{MACOS_CONTENTS}/MacOS/tianshu-desktop",
        )
        _expect_arch(desktop, target, "tianshu-desktop")
        for relative in sorted(files):
            if not _path_has_native_payload(relative, target):
                continue
            data = _read_archive_bytes(
                archive,
                members,
                f"{MACOS_RESOURCES}/{relative}",
            )
            _expect_arch(data, target, relative)


def _extract_nsis_payload(artifact: Path, destination: Path) -> None:
    command = [
        _seven_zip_command(),
        "x",
        "-y",
        f"-o{destination}",
        str(artifact),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifyError("NSIS payload 解包超时") from exc
    except OSError as exc:
        raise VerifyError(
            f"NSIS payload 解包失败：{type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        stdout = (result.stdout or b"").decode("utf-8", errors="replace")
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        detail = (stderr or stdout).strip()[-2000:]
        raise VerifyError(f"NSIS payload 解包失败：{detail}")


def _seven_zip_command() -> str:
    candidates = (
        shutil.which("7z"),
        shutil.which("7zz"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise VerifyError("Windows NSIS payload 验证需要 7-Zip（7z/7zz）")


def _validate_extracted_tree(root: Path) -> None:
    resolved_root = root.resolve()
    paths = list(root.rglob("*"))
    _validate_casefold_paths(
        [path.relative_to(root).as_posix() for path in paths],
        "NSIS 解包结果",
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        _validate_relative_path(relative)
        _failure(not path.is_symlink(), f"NSIS 解包结果包含符号链接：{relative}")
        _failure(path.is_file() or path.is_dir(), f"NSIS 解包结果包含特殊文件：{relative}")
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise VerifyError(f"NSIS 解包路径逃出临时目录：{relative}") from exc


def _verify_windows_bundle(bundle_dir: Path, target: str) -> None:
    python = bundle_dir / "python" / "python.exe"
    if not python.is_file():
        raise VerifyError("Windows bundle 缺少 python.exe")
    try:
        manifest = read_manifest(
            bundle_dir / "release-manifest.json",
            base_dir=bundle_dir,
        )
    except ReleaseManifestError as exc:
        raise VerifyError(str(exc)) from exc
    _verify_source_native_files(bundle_dir, manifest["files"], target)


def _contains_nsis_marker(path: Path) -> bool:
    marker = b"NullsoftInst"
    match = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            data = match + chunk
            if marker in data:
                return True
            match = data[-len(marker) + 1 :]
    return False


def _verify_windows_artifact(
    *,
    artifact: Path,
    bundle_dir: Path,
    target: str,
    extractor: Callable[[Path, Path], None] | None = None,
) -> None:
    header = _native_header(artifact)
    _expect_windows_nsis_bootstrap(header, artifact.name)
    if not _contains_nsis_marker(artifact):
        raise VerifyError(f"{artifact.name} 缺少 NSIS 安装器标识")

    with tempfile.TemporaryDirectory(prefix="tianshu-nsis-payload.") as temporary:
        destination = Path(temporary)
        (extractor or _extract_nsis_payload)(artifact, destination)
        _validate_extracted_tree(destination)
        release_paths = sorted(
            path for path in destination.rglob("release.json") if path.is_file()
        )
        if len(release_paths) != 1:
            raise VerifyError(
                "NSIS 安装器必须包含唯一 release.json 资源根："
                f"{[str(path.relative_to(destination)) for path in release_paths]}"
            )
        payload_release = release_paths[0].read_bytes()
        identity = _read_release_identity(payload_release, "NSIS payload release.json")
        _identity_matches(
            identity,
            version=json.loads((bundle_dir / "release.json").read_text())["version"],
            commit=json.loads((bundle_dir / "release.json").read_text())["commit"],
            target=target,
            label="NSIS payload release.json",
        )
        if payload_release != (bundle_dir / "release.json").read_bytes():
            raise VerifyError("NSIS payload release.json 与已验证 bundle 字节不一致")

        payload_root = release_paths[0].parent
        payload_manifest_path = payload_root / "release-manifest.json"
        if not payload_manifest_path.is_file():
            raise VerifyError("NSIS payload 缺少 release-manifest.json")
        if payload_manifest_path.read_bytes() != (
            bundle_dir / "release-manifest.json"
        ).read_bytes():
            raise VerifyError(
                "NSIS payload release-manifest.json 与已验证 bundle 字节不一致"
            )
        try:
            payload_manifest = read_manifest(
                payload_manifest_path,
                base_dir=payload_root,
            )
        except ReleaseManifestError as exc:
            raise VerifyError(f"NSIS payload release-manifest 不合法：{exc}") from exc
        _identity_matches(
            payload_manifest,
            version=identity["version"],
            commit=identity["commit"],
            target=identity["target"],
            label="NSIS payload release-manifest",
        )

        files = payload_manifest["files"]
        _verify_required_layout(payload_root, target, files)
        required_files = set(files) | {"release-manifest.json"}
        allowed_files = required_files | ALLOWED_RESOURCE_ICONS
        actual_files = {
            path.relative_to(payload_root).as_posix()
            for path in payload_root.rglob("*")
            if path.is_file()
        }
        if payload_root == destination:
            actual_files.difference_update(WINDOWS_NSIS_SUPPORT_FILES)
        unlisted = actual_files - allowed_files
        missing = set(files) - actual_files
        if unlisted or missing:
            raise VerifyError(
                "NSIS payload 文件集合与 manifest 不一致："
                f"未列文件={sorted(unlisted)}，缺失文件={sorted(missing)}"
            )

        payload_prefix = PurePosixPath(payload_root.relative_to(destination).as_posix())
        required_extracted_files = {
            (payload_prefix / PurePosixPath(relative)).as_posix()
            for relative in required_files
        } | WINDOWS_NSIS_SUPPORT_FILES
        allowed_extracted_files = {
            (payload_prefix / PurePosixPath(relative)).as_posix()
            for relative in allowed_files
        } | WINDOWS_NSIS_SUPPORT_FILES
        actual_extracted_files = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        }
        unlisted = actual_extracted_files - allowed_extracted_files
        missing = required_extracted_files - actual_extracted_files
        if unlisted or missing:
            raise VerifyError(
                "NSIS 解包文件集合与 Tauri 固定布局不一致："
                f"未列文件={sorted(unlisted)}，缺失文件={sorted(missing)}"
            )

        _expect_arch(
            _native_header(destination / WINDOWS_NSIS_APP_EXECUTABLE),
            target,
            f"NSIS payload {WINDOWS_NSIS_APP_EXECUTABLE}",
        )
        for relative in sorted(WINDOWS_NSIS_X86_SUPPORT_FILES):
            machine = _expect_pe(
                _native_header(destination / relative),
                f"NSIS installer {relative}",
            )
            _failure(
                machine == WINDOWS_NSIS_BOOTSTRAP_MACHINE,
                f"NSIS installer {relative} 不是固定的 x86 组件：0x{machine:04X}",
            )

        _expect_arch(
            _native_header(payload_root / "python" / "python.exe"),
            target,
            "NSIS payload python.exe",
        )
        _verify_source_native_files(payload_root, files, target)


def host_target() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system not in {"darwin", "windows"}:
        return None
    if machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    elif machine in {"x86_64", "amd64"}:
        architecture = "x64"
    else:
        return None
    platform_name = "macos" if system == "darwin" else "windows"
    return f"{platform_name}-{architecture}"


def _bundle_python(bundle_dir: Path, target: str) -> Path:
    if target.startswith("windows"):
        return bundle_dir / "python" / "python.exe"
    return bundle_dir / "python" / "bin" / "python3"


def _posix_process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_posix_process_group(
    process: subprocess.Popen[bytes],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        if process.returncode is not None and not _posix_process_group_exists(process.pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _terminate_runtime_smoke_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=RUNTIME_SMOKE_TASKKILL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerifyError(
                f"bundle Python 超时后无法回收 Windows 进程树："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        process.wait(timeout=RUNTIME_SMOKE_TERM_TIMEOUT_S)
        if result.returncode != 0:
            raise VerifyError(
                f"bundle Python 超时后 taskkill 返回 {result.returncode}："
                f"{(result.stderr or result.stdout or b'').decode(errors='replace')[-1000:]}"
            )
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=RUNTIME_SMOKE_TERM_TIMEOUT_S)
        return
    if not _wait_posix_process_group(process, RUNTIME_SMOKE_TERM_TIMEOUT_S):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not _wait_posix_process_group(process, RUNTIME_SMOKE_TERM_TIMEOUT_S):
            raise VerifyError("bundle Python 超时后进程组在 SIGKILL 后仍未消失")


def _load_trusted_runtime_lock() -> dict[str, Any]:
    try:
        return load_runtime_lock(RUNTIME_LOCK_PATH)
    except BuildError as exc:
        raise VerifyError(f"可信 Python runtime lock 不合法：{exc}") from exc


def _runtime_smoke(bundle_dir: Path, target: str) -> dict[str, Any]:
    current = host_target()
    if current != target:
        raise VerifyError(
            f"runtime smoke 必须在原生 target {target} 运行，当前宿主为 {current!r}"
        )
    python = _bundle_python(bundle_dir, target)
    if not python.is_file():
        raise VerifyError(f"bundle 缺少随包 Python：{python}")
    try:
        identity = _read_release_identity(
            (bundle_dir / "release.json").read_bytes(),
            "runtime smoke release.json",
        )
    except OSError as exc:
        raise VerifyError(f"无法读取 runtime smoke release.json：{exc}") from exc

    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONSTARTUP", "TIANSHU_FAILPOINT"):
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(bundle_dir / "app")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    try:
        manifest = read_manifest(
            bundle_dir / "release-manifest.json",
            base_dir=bundle_dir,
        )
    except ReleaseManifestError as exc:
        raise VerifyError(f"runtime smoke 发布清单不合法：{exc}") from exc
    runtime_lock = _load_trusted_runtime_lock()
    if manifest["python"] != runtime_lock["python"]:
        raise VerifyError(
            "runtime smoke Python 版本未绑定可信 runtime lock："
            f"清单={manifest['python']}，lock={runtime_lock['python']}"
        )
    if manifest["python_build_release"] != runtime_lock["release"]:
        raise VerifyError(
            "runtime smoke Python build release 未绑定可信 runtime lock："
            f"清单={manifest['python_build_release']}，lock={runtime_lock['release']}"
        )
    if target not in runtime_lock["targets"]:
        raise VerifyError(f"可信 runtime lock 缺少 target：{target}")
    command = [
        str(python),
        "-P",
        "-m",
        "tianshu.release_smoke",
        "--ui-dir",
        str(bundle_dir / "ui" / "dist"),
        "--expected-version",
        identity["version"],
        "--expected-commit",
        identity["commit"],
        "--expected-target",
        identity["target"],
        "--expected-python",
        manifest["python"],
    ]
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            cwd=bundle_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            **popen_options,
        )
    except OSError as exc:
        raise VerifyError(
            f"bundle Python 启动烟测失败：{type(exc).__name__}: {exc}"
        ) from exc
    try:
        stdout_bytes, stderr_bytes = process.communicate(
            timeout=RUNTIME_SMOKE_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        try:
            _terminate_runtime_smoke_tree(process)
            process.communicate(timeout=RUNTIME_SMOKE_TERM_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired, VerifyError) as cleanup_exc:
            process.kill()
            process.communicate(timeout=RUNTIME_SMOKE_TERM_TIMEOUT_S)
            raise VerifyError(
                f"bundle Python 启动烟测超时，且进程树回收失败："
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            ) from exc
        raise VerifyError(
            f"bundle Python 启动烟测超时：{RUNTIME_SMOKE_TIMEOUT_S:.3f}s；"
            "已回收进程树"
        ) from exc
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        detail = (stderr or stdout).strip()[-4000:]
        raise VerifyError(f"bundle Python 启动烟测失败：{detail}")
    try:
        smoke = json.loads(stdout)
    except ValueError as exc:
        raise VerifyError("bundle Python 启动烟测未返回 JSON 结果") from exc
    if not isinstance(smoke, dict) or smoke.get("status") != "pass":
        raise VerifyError("bundle Python 启动烟测未返回 pass")
    if smoke.get("host_target") != target:
        raise VerifyError(
            f"runtime smoke host_target 不匹配：期望 {target!r}，"
            f"实际 {smoke.get('host_target')!r}"
        )
    if smoke.get("version") != identity["version"]:
        raise VerifyError("runtime smoke 模块版本与发行身份不一致")
    if smoke.get("release") != identity:
        raise VerifyError("runtime smoke Host 发行身份与 release.json 不一致")
    python_runtime = smoke.get("python_runtime")
    if (
        not isinstance(python_runtime, dict)
        or python_runtime.get("implementation") != "CPython"
        or python_runtime.get("version") != manifest["python"]
    ):
        raise VerifyError("runtime smoke Python 身份与发布清单不一致")
    if (
        type(smoke.get("host_pid")) is not int
        or type(smoke.get("daemon_pid")) is not int
        or smoke["host_pid"] == smoke["daemon_pid"]
        or not isinstance(smoke.get("daemon_instance_id"), str)
    ):
        raise VerifyError("runtime smoke PID/instance 身份不完整")
    required_checks = {
        "import",
        "daemon_ipc",
        "daemon_identity",
        "release_identity",
        "python_runtime",
        "health",
        "session_token_required",
        "session",
        "state",
        "stop",
    }
    if set(smoke.get("checks") or ()) != required_checks:
        raise VerifyError("runtime smoke 检查集合不完整")
    return smoke


def _verify_checksum(artifact: Path, checksum_path: Path) -> str:
    try:
        payload = checksum_path.read_bytes()
    except OSError as exc:
        raise VerifyError(f"无法读取 SHA256 文件：{exc}") from exc
    _failure(
        payload.endswith(b"\n") and payload.count(b"\n") == 1,
        "SHA256 文件必须恰好包含一行",
    )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise VerifyError("SHA256 文件必须是 ASCII") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\r\n]+)\n", text)
    _failure(match is not None, "SHA256 文件格式不合法")
    assert match is not None
    _failure(match.group(2) == artifact.name, "SHA256 文件绑定了另一个产物")
    actual = _sha256(artifact)
    if actual != match.group(1):
        raise VerifyError(
            f"产物 SHA256 不匹配：期望 {match.group(1)}，实际 {actual}"
        )
    return actual


def _validate_artifact_directory(
    artifact_dir: Path,
    expected: set[str],
) -> None:
    entries = list(artifact_dir.iterdir())
    _validate_casefold_paths(
        [entry.name for entry in entries],
        "发行目录",
    )
    actual = {entry.name for entry in entries}
    if actual != expected:
        raise VerifyError(
            "发行目录文件集合必须封闭："
            f"未列文件={sorted(actual - expected)}，"
            f"缺失文件={sorted(expected - actual)}"
        )
    for entry in entries:
        _validate_relative_path(entry.name)
        if entry.is_symlink():
            raise VerifyError(f"发行目录包含符号链接：{entry.name}")
        if entry.is_dir():
            raise VerifyError(f"发行目录包含子目录：{entry.name}")
        _failure(entry.is_file(), f"发行目录包含特殊文件：{entry.name}")


def verify_release(
    *,
    artifact_dir: Path,
    bundle_dir: Path,
    target: str,
    version: str,
    commit: str,
    run_smoke: bool = True,
) -> dict[str, Any]:
    if target not in TARGETS:
        raise VerifyError(f"未知 target：{target}")
    if VERSION_RE.fullmatch(version) is None:
        raise VerifyError("version 必须匹配 0.x.y")
    if COMMIT_RE.fullmatch(commit) is None:
        raise VerifyError("commit 必须是 40 位小写十六进制 SHA")
    artifact_dir = Path(artifact_dir).resolve()
    bundle_dir = Path(bundle_dir).resolve()
    if not bundle_dir.is_dir():
        raise VerifyError(f"bundle 目录不存在：{bundle_dir}")
    if not artifact_dir.is_dir():
        raise VerifyError(f"artifact 目录不存在：{artifact_dir}")

    manifest, manifest_bytes = _verify_source_bundle(
        bundle_dir,
        target=target,
        version=version,
        commit=commit,
    )
    stem = f"Tianshu-Agent-{version}-{target}"
    artifact_suffix = ".app.tar.gz" if target.startswith("macos") else "-setup.exe"
    artifact = artifact_dir / f"{stem}{artifact_suffix}"
    checksum = artifact_dir / f"{stem}.sha256"
    _validate_artifact_directory(
        artifact_dir,
        {"release-manifest.json", artifact.name, checksum.name},
    )
    copied_manifest = artifact_dir / "release-manifest.json"
    if copied_manifest.read_bytes() != manifest_bytes:
        raise VerifyError("发行目录 release-manifest.json 与 bundle 字节不一致")

    if not artifact.is_file():
        raise VerifyError("发行目录缺少 target 对应的产物")
    if not checksum.is_file():
        raise VerifyError("发行目录缺少 target 对应的 SHA256 文件")
    actual_sha = _verify_checksum(artifact, checksum)

    if target.startswith("macos"):
        _verify_macos_archive(
            artifact=artifact,
            bundle_dir=bundle_dir,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            target=target,
        )
    else:
        _verify_windows_bundle(bundle_dir, target)
        _verify_windows_artifact(
            artifact=artifact,
            bundle_dir=bundle_dir,
            target=target,
        )

    smoke = (
        _runtime_smoke(bundle_dir, target)
        if run_smoke
        else {"status": "not_run_by_caller"}
    )
    return {
        "target": target,
        "version": version,
        "commit": commit,
        "artifact": artifact.name,
        "sha256": actual_sha,
        "size": artifact.stat().st_size,
        "manifest_sha256": _sha256(bundle_dir / "release-manifest.json"),
        "runtime_smoke": smoke,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_release(
            artifact_dir=args.artifact_dir,
            bundle_dir=args.bundle_dir,
            target=args.target,
            version=args.version,
            commit=args.commit,
        )
    except (OSError, VerifyError) as exc:
        print(f"发行验证失败：{exc}", file=sys.stderr)
        return 1
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{args.report.name}.",
            dir=args.report.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, args.report)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
