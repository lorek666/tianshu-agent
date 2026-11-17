from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from release_manifest import (  # noqa: E402
    ReleaseManifestError,
    validate_manifest_document,
)

TARGETS = ("macos-arm64", "macos-x64", "windows-x64", "windows-arm64")
HELPER_SCRIPTS = ("install-macos.sh", "uninstall-macos.sh")
VERSION_PATTERN = re.compile(r"0\.[0-9]+\.[0-9]+")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
BUILD_RUN_PATH_PATTERN = re.compile(
    r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*"
)


class AssembleError(ValueError):
    """四目标产物不完整或彼此身份不一致。"""


def _is_link(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or (junction_check is not None and junction_check())


def _lstat(path: Path, label: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AssembleError(f"{label} 无法检查：{exc}") from exc


def _validate_path_chain(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    relative_parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in relative_parts:
        current /= part
        metadata = _lstat(current, label)
        if metadata is None:
            break
        if _is_link(current):
            raise AssembleError(f"{label}路径不得经过符号链接：{current}")
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise AssembleError(f"{label}路径祖先必须是普通目录：{current}")
    return absolute


def _require_input_directory(path: Path, label: str) -> Path:
    absolute = _validate_path_chain(path, label)
    metadata = _lstat(absolute, label)
    if metadata is None:
        raise AssembleError(f"{label} 目录缺失")
    if _is_link(absolute):
        raise AssembleError(f"{label} 不能是符号链接")
    if not stat.S_ISDIR(metadata.st_mode):
        raise AssembleError(f"{label} 必须是普通目录")
    return absolute.resolve(strict=True)


def _require_regular_input(path: Path, label: str) -> None:
    absolute = _validate_path_chain(path, label)
    metadata = _lstat(absolute, label)
    if metadata is None:
        raise AssembleError(f"{label} 缺失")
    if _is_link(absolute):
        raise AssembleError(f"{label} 不能是符号链接")
    if not stat.S_ISREG(metadata.st_mode):
        raise AssembleError(f"{label} 必须是普通文件")


def _prepare_output_directory(path: Path) -> Path:
    requested = _validate_path_chain(path, "out_dir")
    metadata = _lstat(requested, "out_dir")
    if metadata is not None:
        if _is_link(requested):
            raise AssembleError("out_dir 不能是符号链接")
        if not stat.S_ISDIR(metadata.st_mode):
            raise AssembleError("out_dir 必须是普通目录")
    requested.mkdir(parents=True, exist_ok=True)
    requested = _validate_path_chain(requested, "out_dir")
    metadata = _lstat(requested, "out_dir")
    assert metadata is not None
    if _is_link(requested) or not stat.S_ISDIR(metadata.st_mode):
        raise AssembleError("out_dir 必须是普通目录")
    return requested.resolve(strict=True)


def _require_safe_output_path(path: Path) -> None:
    absolute = _validate_path_chain(path, "输出路径")
    metadata = _lstat(absolute, "输出路径")
    if metadata is None:
        return
    if _is_link(absolute):
        raise AssembleError(f"输出路径不能是符号链接：{absolute}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AssembleError(f"输出路径必须是普通文件：{absolute}")
    if metadata.st_nlink > 1:
        raise AssembleError(f"输出路径不能是硬链接：{absolute}")


def _validate_identity(version: str, commit: str, build_run_url: str) -> None:
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise AssembleError("version 必须匹配 0.x.y 且不含路径字符")
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise AssembleError("commit 必须是 40 位小写十六进制")
    if not isinstance(build_run_url, str):
        raise AssembleError("build_run_url 必须是 GitHub Actions HTTPS 构建运行 URL")
    try:
        parsed = urlsplit(build_run_url)
        port = parsed.port
    except ValueError as exc:
        raise AssembleError("build_run_url 不是合法 HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or BUILD_RUN_PATH_PATTERN.fullmatch(parsed.path) is None
    ):
        raise AssembleError(
            "build_run_url 必须是 https://HOST/OWNER/REPO/actions/runs/ID"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_index(path: Path, data: dict[str, Any]) -> None:
    _require_safe_output_path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def assemble_release(
    *,
    artifacts_root: Path,
    out_dir: Path,
    version: str,
    commit: str,
    build_run_url: str,
) -> Path:
    _validate_identity(version, commit, build_run_url)
    artifacts_root = _require_input_directory(Path(artifacts_root), "artifacts_root")
    out_dir = _prepare_output_directory(Path(out_dir))
    records: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for target in TARGETS:
        target_dir = _require_input_directory(artifacts_root / target, target)
        suffix = ".app.tar.gz" if target.startswith("macos") else "-setup.exe"
        stem = f"Tianshu-Agent-{version}-{target}"
        artifact = target_dir / f"{stem}{suffix}"
        checksum = target_dir / f"{stem}.sha256"
        manifest_path = target_dir / "release-manifest.json"
        verification_path = target_dir / "release-verification.json"
        _require_regular_input(artifact, f"{target} 产物")
        _require_regular_input(checksum, f"{target} SHA256")
        _require_regular_input(manifest_path, f"{target} 发布清单")
        _require_regular_input(verification_path, f"{target} 验证报告")
        try:
            fields = checksum.read_text(encoding="ascii").split()
        except (OSError, UnicodeError) as exc:
            raise AssembleError(f"{target} 的 SHA256 文件不可读：{exc}") from exc
        actual = _sha256(artifact)
        if len(fields) != 2 or fields[0] != actual or fields[1] != artifact.name:
            raise AssembleError(f"{target} 的 SHA256 与产物不一致")
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = validate_manifest_document(
                json.loads(manifest_bytes.decode("utf-8"))
            )
        except (UnicodeDecodeError, ValueError, ReleaseManifestError) as exc:
            raise AssembleError(f"{target} 发布清单不合法：{exc}") from exc
        expected = {"target": target, "version": version, "commit": commit}
        for field, value in expected.items():
            if manifest.get(field) != value:
                raise AssembleError(
                    f"{target} 发布清单 {field} 不匹配："
                    f"期望 {value!r}，实际 {manifest.get(field)!r}"
                )
        try:
            verification = json.loads(
                verification_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise AssembleError(
                f"{target} 的验证报告不可读或不合法：{exc}"
            ) from exc
        if not isinstance(verification, dict):
            raise AssembleError(f"{target} 的验证报告根必须是 object")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if (
            verification.get("target") != target
            or verification.get("version") != version
            or verification.get("commit") != commit
            or verification.get("sha256") != actual
            or verification.get("artifact") != artifact.name
            or verification.get("manifest_sha256") != manifest_sha256
        ):
            raise AssembleError(f"{target} 的验证报告与产物身份不一致")
        runtime_smoke = verification.get("runtime_smoke")
        if (
            not isinstance(runtime_smoke, dict)
            or runtime_smoke.get("status") != "pass"
            or runtime_smoke.get("host_target") != target
        ):
            raise AssembleError(f"{target} 的 runtime smoke 未在原生 target 通过")
        artifact_output = out_dir / artifact.name
        checksum_output = out_dir / checksum.name
        manifest_output = out_dir / f"release-manifest-{target}.json"
        _require_safe_output_path(artifact_output)
        _require_safe_output_path(checksum_output)
        _require_safe_output_path(manifest_output)
        shutil.copyfile(artifact, artifact_output)
        shutil.copyfile(checksum, checksum_output)
        shutil.copyfile(
            manifest_path,
            manifest_output,
        )
        artifact_asset = {
            "kind": "artifact",
            "target": target,
            "name": artifact_output.name,
            "sha256": actual,
            "size": artifact_output.stat().st_size,
        }
        checksum_asset = {
            "kind": "checksum",
            "target": target,
            "name": checksum_output.name,
            "sha256": _sha256(checksum_output),
            "size": checksum_output.stat().st_size,
        }
        manifest_asset = {
            "kind": "manifest",
            "target": target,
            "name": manifest_output.name,
            "sha256": _sha256(manifest_output),
            "size": manifest_output.stat().st_size,
        }
        assets.extend((artifact_asset, checksum_asset, manifest_asset))
        records.append(
            {
                "target": target,
                "artifact": artifact.name,
                "sha256": actual,
                "size": artifact.stat().st_size,
                "checksum": checksum_asset,
                "manifest": manifest_asset,
                "device_verification": "unknown",
                "runtime_smoke": runtime_smoke,
            }
        )

    helper_scripts: list[dict[str, Any]] = []
    for script in HELPER_SCRIPTS:
        source = Path(__file__).resolve().parent / script
        _require_regular_input(source, f"辅助脚本 {script}")
        if os.name == "posix" and source.stat().st_mode & 0o111 == 0:
            raise AssembleError(f"辅助脚本缺失或不可执行：{source}")
        copied = out_dir / script
        _require_safe_output_path(copied)
        shutil.copy2(source, copied)
        copied.chmod(source.stat().st_mode & 0o777 if os.name == "posix" else 0o755)
        helper_record = {
            "kind": "helper-script",
            "name": script,
            "sha256": _sha256(copied),
            "size": copied.stat().st_size,
        }
        helper_scripts.append(helper_record)
        assets.append(helper_record)
    index = {
        "schema": 1,
        "product": "tianshu-agent",
        "version": version,
        "commit": commit,
        "build_run_url": build_run_url,
        "signed": False,
        "artifacts": records,
        "helper_scripts": helper_scripts,
        "assets": assets,
    }
    path = out_dir / "release-index.json"
    _write_index(path, index)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-run-url", required=True)
    args = parser.parse_args(argv)
    try:
        path = assemble_release(
            artifacts_root=args.artifacts_root,
            out_dir=args.out_dir,
            version=args.version,
            commit=args.commit,
            build_run_url=args.build_run_url,
        )
    except (AssembleError, OSError) as exc:
        print(f"发行汇总失败：{exc}")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
