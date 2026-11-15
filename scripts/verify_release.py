from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import posixpath
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
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


class VerifyError(ValueError):
    """发行产物、清单或来源字节不一致。"""


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
        if release_bytes != (bundle_dir / "release.json").read_bytes():
            raise VerifyError("归档 release.json 与已验证 bundle 字节不一致")
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
    manifest: dict[str, Any],
) -> None:
    machine = _expect_pe(artifact.read_bytes(), artifact.name)
    if machine not in {0x014C, 0x8664, 0xAA64}:
        raise VerifyError(f"{artifact.name} 不是可识别的 NSIS PE 安装器")


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
        return {
            "status": "not_run_cross_target",
            "host_target": current,
        }
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
    copied_manifest = artifact_dir / "release-manifest.json"
    if not copied_manifest.is_file() or copied_manifest.read_bytes() != manifest_path.read_bytes():
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
            bundle_dir=bundle_dir,
        )
    else:
        _verify_windows_artifact(artifact=artifact, manifest=manifest)
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
