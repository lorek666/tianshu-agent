from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

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
FORBIDDEN_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".log", ".key"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_IDENTITY_FIELDS = frozenset(
    {"schema", "product", "version", "commit", "target"}
)


class VerifyError(ValueError):
    """发行产物、清单或来源字节不一致。"""


def _read_release_identity(data: bytes, label: str) -> dict[str, Any]:
    try:
        identity = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VerifyError(f"{label} 不是合法 JSON：{exc}") from exc
    if not isinstance(identity, dict):
        raise VerifyError(f"{label} 根必须是 object")
    missing = RELEASE_IDENTITY_FIELDS - set(identity)
    unknown = set(identity) - RELEASE_IDENTITY_FIELDS
    if missing or unknown:
        raise VerifyError(
            f"{label} 字段必须封闭：缺少={sorted(missing)}，未知={sorted(unknown)}"
        )
    if identity.get("schema") != 1 or identity.get("product") != "tianshu-agent":
        raise VerifyError(f"{label} 的 schema 或 product 不合法")
    version = identity.get("version")
    if not isinstance(version, str) or re.fullmatch(r"0\.[0-9]+\.[0-9]+", version) is None:
        raise VerifyError(f"{label} 的 version 不合法")
    commit = identity.get("commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise VerifyError(f"{label} 的 commit 必须是 40 位小写十六进制 SHA")
    target = identity.get("target")
    if target not in TARGETS:
        raise VerifyError(f"{label} 的 target 不合法：{target!r}")
    return identity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_path(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    lowered = {part.lower() for part in path.parts}
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in member.name
        or lowered & FORBIDDEN_PARTS
        or path.suffix.lower() in FORBIDDEN_SUFFIXES
    ):
        raise VerifyError(f"归档包含不安全路径：{member.name!r}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute() or "\\" in member.linkname:
            raise VerifyError(f"归档包含逃逸链接：{member.name!r}")
        base = path.parent if member.issym() else PurePosixPath()
        resolved = PurePosixPath(posixpath.normpath(str(base / target)))
        if (
            not resolved.parts
            or ".." in resolved.parts
            or resolved.parts[0] != path.parts[0]
        ):
            raise VerifyError(f"归档包含逃逸链接：{member.name!r}")
    elif not (member.isfile() or member.isdir()):
        raise VerifyError(f"归档包含特殊文件：{member.name!r}")
    return path


def _read_member(archive: tarfile.TarFile, name: str) -> bytes:
    stream = archive.extractfile(name)
    if stream is None:
        raise VerifyError(f"归档缺少文件：{name}")
    return stream.read()


def _read_archive_path(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> bytes:
    seen: set[str] = set()
    current = name
    while True:
        if current in seen:
            raise VerifyError(f"归档链接循环：{name}")
        seen.add(current)
        member = members.get(current)
        if member is None:
            raise VerifyError(f"归档缺少文件：{current}")
        if member.isfile():
            return _read_member(archive, current)
        if member.issym():
            target = PurePosixPath(current).parent / member.linkname
            current = posixpath.normpath(target.as_posix())
            continue
        if member.islnk():
            current = member.linkname
            continue
        raise VerifyError(f"归档路径不是可读取文件：{current}")


def _expect_pe(data: bytes, label: str) -> int:
    if len(data) < 64 or data[:2] != b"MZ":
        raise VerifyError(f"{label} 不是 PE 文件")
    offset = struct.unpack("<I", data[0x3C:0x40])[0]
    if offset + 6 > len(data) or data[offset : offset + 4] != b"PE\0\0":
        raise VerifyError(f"{label} 缺少 PE header")
    return struct.unpack("<H", data[offset + 4 : offset + 6])[0]


def _expect_arch(data: bytes, target: str, label: str) -> None:
    if target.startswith("macos"):
        expected_magic = b"\xcf\xfa\xed\xfe"
        if len(data) < 8 or data[:4] != expected_magic:
            raise VerifyError(f"{label} 不是 64 位 Mach-O")
        cpu_type = struct.unpack("<I", data[4:8])[0]
        expected = 0x0100000C if target.endswith("arm64") else 0x01000007
        if cpu_type != expected:
            raise VerifyError(f"{label} CPU 架构与 target {target} 不匹配")
        return

    machine = _expect_pe(data, label)
    expected = 0xAA64 if target.endswith("arm64") else 0x8664
    if machine != expected:
        raise VerifyError(f"{label} PE 架构与 target {target} 不匹配")


def _verify_macos_archive(
    *,
    artifact: Path,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    bundle_dir: Path,
) -> None:
    with tarfile.open(artifact, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        for member in members.values():
            _member_path(member)

        roots = {PurePosixPath(name).parts[0] for name in members}
        if roots != {"Tianshu Agent.app"}:
            raise VerifyError("macOS 归档必须只包含 Tianshu Agent.app")
        prefix = "Tianshu Agent.app/Contents/"
        required = {
            f"{prefix}MacOS/tianshu-desktop",
            f"{prefix}Info.plist",
            f"{prefix}Resources/licenses/LICENSE",
            f"{prefix}Resources/release.json",
            f"{prefix}Resources/ui/dist/index.html",
        }
        missing = required - set(members)
        if missing:
            raise VerifyError(f"macOS 归档缺少应用文件：{sorted(missing)}")

        release_bytes = _read_archive_path(
            archive,
            members,
            f"{prefix}Resources/release.json",
        )
        _read_release_identity(release_bytes, "macOS 归档 release.json")
        if release_bytes != (bundle_dir / "release.json").read_bytes():
            raise VerifyError("归档 release.json 与已验证 bundle 字节不一致")
        archived_manifest = _read_archive_path(
            archive,
            members,
            f"{prefix}Resources/release-manifest.json",
        )
        if archived_manifest != manifest_bytes:
            raise VerifyError("归档 release-manifest.json 与已验证 bundle 字节不一致")
        ui_bytes = _read_archive_path(
            archive,
            members,
            f"{prefix}Resources/ui/dist/index.html",
        )
        if ui_bytes != (bundle_dir / "ui" / "dist" / "index.html").read_bytes():
            raise VerifyError("归档 UI 与已验证 bundle 字节不一致")

        for relative, expected in manifest["files"].items():
            member_name = f"{prefix}Resources/{relative}"
            actual = hashlib.sha256(
                _read_archive_path(archive, members, member_name)
            ).hexdigest()
            if actual != expected:
                raise VerifyError(f"归档文件 SHA256 不匹配：{relative}")

        resource_prefix = PurePosixPath(f"{prefix}Resources")
        actual_resources = {
            path.relative_to(resource_prefix).as_posix()
            for name in members
            if not members[name].isdir()
            and (path := PurePosixPath(name)).is_relative_to(resource_prefix)
        }
        expected_resources = set(manifest["files"]) | {
            "release-manifest.json",
            "icon.icns",
            "icon.ico",
        }
        unlisted = actual_resources - expected_resources
        missing_payload = set(manifest["files"]) - actual_resources
        if unlisted or missing_payload:
            raise VerifyError(
                "macOS 归档资源集合与 manifest 不一致："
                f"未列文件={sorted(unlisted)}，缺失文件={sorted(missing_payload)}"
            )

        contents_prefix = PurePosixPath(prefix.rstrip("/"))
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
        } | {f"Resources/{relative}" for relative in manifest["files"]}
        allowed_contents = expected_contents | {
            "Resources/icon.icns",
            "Resources/icon.ico",
        }
        unexpected_bundle_files = actual_contents - allowed_contents
        missing_bundle_files = expected_contents - actual_contents
        if unexpected_bundle_files or missing_bundle_files:
            raise VerifyError(
                "macOS 应用文件集合不完整："
                f"未列文件={sorted(unexpected_bundle_files)}，"
                f"缺失文件={sorted(missing_bundle_files)}"
            )

        desktop = _read_archive_path(archive, members, f"{prefix}MacOS/tianshu-desktop")
        _expect_arch(desktop, manifest["target"], "tianshu-desktop")


def _verify_windows_artifact(
    *,
    artifact: Path,
    bundle_dir: Path,
    target: str,
    extractor: Callable[[Path, Path], None] | None = None,
) -> None:
    installer = artifact.read_bytes()
    machine = _expect_pe(installer, artifact.name)
    if machine not in {0x014C, 0x8664, 0xAA64}:
        raise VerifyError(f"{artifact.name} 不是可识别的 NSIS PE 安装器")
    if b"NullsoftInst" not in installer:
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
        payload_root = release_paths[0].parent
        payload_release = release_paths[0].read_bytes()
        identity = _read_release_identity(payload_release, "NSIS payload release.json")
        if identity["target"] != target:
            raise VerifyError(
                f"NSIS payload target 不匹配：期望 {target!r}，实际 {identity['target']!r}"
            )
        if payload_release != (bundle_dir / "release.json").read_bytes():
            raise VerifyError("NSIS payload release.json 与已验证 bundle 字节不一致")

        payload_manifest_path = payload_root / "release-manifest.json"
        bundle_manifest_path = bundle_dir / "release-manifest.json"
        if not payload_manifest_path.is_file():
            raise VerifyError("NSIS payload 缺少 release-manifest.json")
        if payload_manifest_path.read_bytes() != bundle_manifest_path.read_bytes():
            raise VerifyError("NSIS payload release-manifest.json 与已验证 bundle 字节不一致")
        try:
            payload_manifest = read_manifest(payload_manifest_path, base_dir=payload_root)
        except ReleaseManifestError as exc:
            raise VerifyError(f"NSIS payload release-manifest 不合法：{exc}") from exc
        if (
            payload_manifest["target"] != identity["target"]
            or payload_manifest["version"] != identity["version"]
            or payload_manifest["commit"] != identity["commit"]
        ):
            raise VerifyError("NSIS payload release.json 与 release-manifest 身份不一致")

        required_files = set(payload_manifest["files"])
        allowed_files = required_files | {
            "release-manifest.json",
            "icon.ico",
            "icon.icns",
        }
        managed_prefixes = {"app", "python", "ui", "licenses"}
        actual_files = {
            path.relative_to(payload_root).as_posix()
            for path in payload_root.rglob("*")
            if path.is_file()
        }
        unlisted = {
            relative
            for relative in actual_files - allowed_files
            if relative.split("/", 1)[0] in managed_prefixes
        }
        missing = required_files - actual_files
        if unlisted or missing:
            raise VerifyError(
                "NSIS payload 文件集合与 manifest 不一致："
                f"未列文件={sorted(unlisted)}，缺失文件={sorted(missing)}"
            )
        python = payload_root / "python" / "python.exe"
        if not python.is_file():
            raise VerifyError("NSIS payload 缺少 python/python.exe")
        _expect_arch(python.read_bytes(), target, "NSIS payload python.exe")


def _validate_extracted_tree(root: Path) -> None:
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise VerifyError(f"NSIS 解包结果包含符号链接：{path}")
        if not (path.is_file() or path.is_dir()):
            raise VerifyError(f"NSIS 解包结果包含特殊文件：{path}")
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise VerifyError(f"NSIS 解包路径逃出临时目录：{path}") from exc


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


def _extract_nsis_payload(artifact: Path, destination: Path) -> None:
    command = [
        _seven_zip_command(),
        "x",
        "-y",
        f"-o{destination}",
        str(artifact),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise VerifyError(f"NSIS payload 解包失败：{detail}")


def _verify_windows_bundle(bundle_dir: Path, target: str) -> None:
    python = bundle_dir / "python" / "python.exe"
    if not python.is_file():
        raise VerifyError("Windows bundle 缺少 python.exe")
    _expect_arch(python.read_bytes(), target, "python.exe")


def host_target() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x64"
    if system == "windows":
        return "windows-arm64" if machine in {"arm64", "aarch64"} else "windows-x64"
    return None


def _runtime_smoke(bundle_dir: Path, target: str) -> dict[str, Any]:
    current = host_target()
    if current != target:
        raise VerifyError(
            f"runtime smoke 必须在原生 target {target} 运行，当前宿主为 {current!r}"
        )
    python = (
        bundle_dir / "python" / "python.exe"
        if target.startswith("windows")
        else bundle_dir / "python" / "bin" / "python3"
    )
    if not python.is_file():
        raise VerifyError(f"bundle 缺少可执行的 Python：{python}")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(bundle_dir / "app")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import tianshu, tianshu.host, tianshu.daemon; print(tianshu.__version__)",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise VerifyError(f"bundle Python 启动烟测失败：{detail}")
    return {
        "status": "pass",
        "host_target": current,
        "version": result.stdout.strip(),
    }


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
    artifact_dir = Path(artifact_dir).resolve()
    bundle_dir = Path(bundle_dir).resolve()
    manifest_path = bundle_dir / "release-manifest.json"
    try:
        manifest = read_manifest(manifest_path, base_dir=bundle_dir)
    except ReleaseManifestError as exc:
        raise VerifyError(str(exc)) from exc
    expected_identity = {
        "target": target,
        "version": version,
        "commit": commit,
    }
    for field, value in expected_identity.items():
        if manifest.get(field) != value:
            raise VerifyError(
                f"发布清单 {field} 不匹配：期望 {value!r}，实际 {manifest.get(field)!r}"
            )
    release_path = bundle_dir / "release.json"
    try:
        release_bytes = release_path.read_bytes()
    except OSError as exc:
        raise VerifyError(f"无法读取 bundle release.json：{exc}") from exc
    release_identity = _read_release_identity(release_bytes, "bundle release.json")
    for field, value in expected_identity.items():
        if release_identity.get(field) != value:
            raise VerifyError(
                f"release.json {field} 不匹配：期望 {value!r}，"
                f"实际 {release_identity.get(field)!r}"
            )
    manifest_bytes = manifest_path.read_bytes()
    copied_manifest = artifact_dir / "release-manifest.json"
    if not copied_manifest.is_file() or copied_manifest.read_bytes() != manifest_bytes:
        raise VerifyError("发行目录 release-manifest.json 与 bundle 字节不一致")

    stem = f"Tianshu-Agent-{version}-{target}"
    suffix = ".app.tar.gz" if target.startswith("macos") else "-setup.exe"
    artifact = artifact_dir / f"{stem}{suffix}"
    checksum_path = artifact_dir / f"{stem}.sha256"
    if not artifact.is_file() or not checksum_path.is_file():
        raise VerifyError("发行目录缺少 target 对应的产物或 SHA256 文件")
    checksum_fields = checksum_path.read_text(encoding="ascii").split()
    if (
        len(checksum_fields) != 2
        or not SHA256_RE.fullmatch(checksum_fields[0])
        or checksum_fields[1] != artifact.name
    ):
        raise VerifyError("SHA256 文件格式不合法")
    actual = _sha256(artifact)
    if actual != checksum_fields[0]:
        raise VerifyError(f"产物 SHA256 不匹配：期望 {checksum_fields[0]}，实际 {actual}")

    if target.startswith("macos"):
        _verify_macos_archive(
            artifact=artifact,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            bundle_dir=bundle_dir,
        )
    else:
        _verify_windows_artifact(
            artifact=artifact,
            bundle_dir=bundle_dir,
            target=target,
        )
        _verify_windows_bundle(bundle_dir, target)
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
        "sha256": actual,
        "size": artifact.stat().st_size,
        "manifest_sha256": _sha256(manifest_path),
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
    except VerifyError as exc:
        print(f"发行验证失败：{exc}", file=sys.stderr)
        return 1
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{args.report.name}.",
            dir=args.report.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
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
