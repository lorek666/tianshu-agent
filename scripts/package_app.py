from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

TAURI_CLI = "@tauri-apps/cli@2.12.4"
TARGET_TRIPLE = {
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x64": "x86_64-apple-darwin",
    "windows-x64": "x86_64-pc-windows-msvc",
    "windows-arm64": "aarch64-pc-windows-msvc",
}


class PackageError(RuntimeError):
    """Tauri 发行壳未按声明构建。"""


def _is_link(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or (junction_check is not None and junction_check())


def _lstat(path: Path, label: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PackageError(f"{label} 无法检查：{exc}") from exc


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
            if current == absolute:
                raise PackageError(f"{label}不能是符号链接：{current}")
            raise PackageError(f"{label}路径不得经过符号链接：{current}")
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise PackageError(f"{label}路径祖先必须是普通目录：{current}")
    return absolute


def _validated_directory(path: Path, label: str) -> Path:
    absolute = _validate_path_chain(path, label)
    metadata = _lstat(absolute, label)
    if metadata is None:
        raise PackageError(f"{label} 不存在：{absolute}")
    if _is_link(absolute) or not stat.S_ISDIR(metadata.st_mode):
        raise PackageError(f"{label} 必须是普通目录：{absolute}")
    return absolute.resolve(strict=True)


def _require_regular_file(path: Path, label: str) -> None:
    absolute = _validate_path_chain(path, label)
    metadata = _lstat(absolute, label)
    if metadata is None:
        raise PackageError(f"{label} 不存在：{absolute}")
    if _is_link(absolute):
        raise PackageError(f"{label} 不能是符号链接：{absolute}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PackageError(f"{label} 必须是普通文件：{absolute}")


def _validate_resource_tree(root: Path, label: str) -> None:
    root = _validated_directory(root, label)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = _lstat(path, label)
        assert metadata is not None
        if _is_link(path):
            raise PackageError(f"{label} 包含符号链接：{relative}")
        if not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise PackageError(f"{label} 包含特殊文件：{relative}")


def _prepare_output_directory(path: Path) -> Path:
    requested = _validate_path_chain(path, "发行输出目录")
    metadata = _lstat(requested, "发行输出目录")
    if metadata is not None:
        if _is_link(requested):
            raise PackageError(f"发行输出目录不能是符号链接：{requested}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PackageError(f"发行输出目录必须是普通目录：{requested}")
    requested.mkdir(parents=True, exist_ok=True)
    requested = _validate_path_chain(requested, "发行输出目录")
    metadata = _lstat(requested, "发行输出目录")
    assert metadata is not None
    if _is_link(requested) or not stat.S_ISDIR(metadata.st_mode):
        raise PackageError(f"发行输出目录必须是普通目录：{requested}")
    return requested.resolve(strict=True)


def _require_safe_output_path(path: Path) -> None:
    absolute = _validate_path_chain(path, "发行输出路径")
    metadata = _lstat(absolute, "发行输出路径")
    if metadata is None:
        return
    if _is_link(absolute):
        raise PackageError(f"发行输出路径不能是符号链接：{absolute}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PackageError(f"发行输出路径必须是普通文件：{absolute}")
    if metadata.st_nlink > 1:
        raise PackageError(f"发行输出路径不能是硬链接：{absolute}")


def resource_config(bundle_dir: Path) -> dict:
    root = Path(bundle_dir).resolve()
    return {
        "bundle": {
            "resources": {
                str(root / "python"): "python",
                str(root / "app"): "app",
                str(root / "licenses"): "licenses",
                str(root / "ui" / "dist"): "ui/dist",
                str(root / "release.json"): "release.json",
                str(root / "release-manifest.json"): "release-manifest.json",
            }
        }
    }


def tauri_config(bundle_dir: Path, version: str) -> dict:
    if re.fullmatch(r"0\.[0-9]+\.[0-9]+", version) is None:
        raise PackageError("version 必须匹配 0.x.y")
    return {
        "version": version,
        "bundle": resource_config(bundle_dir)["bundle"],
    }


def target_mode(root: Path, debug: bool, target_triple: str | None = None) -> Path:
    mode = root / "desktop" / "src-tauri" / "target"
    if target_triple:
        mode /= target_triple
    return mode / ("debug" if debug else "release")


def native_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x64"
    if system == "windows":
        return "windows-arm64" if machine in {"arm64", "aarch64"} else "windows-x64"
    raise PackageError(f"不支持的宿主平台：{sys.platform}/{machine}")


def pnpm_launcher(platform_name: str | None = None) -> str:
    """Return the executable name used by pnpm on the target host."""
    host = os.name if platform_name is None else platform_name
    return "pnpm.cmd" if host == "nt" else "pnpm"


def resolve_pnpm(platform_name: str | None = None) -> str:
    launcher = pnpm_launcher(platform_name)
    resolved = shutil.which(launcher)
    if resolved is None:
        raise PackageError(f"找不到 pnpm 启动器：{launcher}")
    return os.path.abspath(resolved)


def native_triple() -> str:
    target = native_target()
    return TARGET_TRIPLE[target]


def bundle_path(
    root: Path,
    target: str,
    *,
    debug: bool,
    target_triple: str | None = None,
    version: str = "0.1.0",
) -> Path:
    mode = target_mode(Path(root), debug, target_triple) / "bundle"
    if target.startswith("macos"):
        return mode / "macos" / "Tianshu Agent.app"
    arch = "arm64" if target.endswith("arm64") else "x64"
    return mode / "nsis" / f"Tianshu Agent_{version}_{arch}-setup.exe"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_macos_archive(app: Path, destination: Path) -> None:
    app = _validated_directory(app, "Tauri macOS 发行物")
    _validate_resource_tree(app, "Tauri macOS 发行物")
    with tarfile.open(destination, "w:gz", compresslevel=9) as archive:
        for path in sorted(app.rglob("*")):
            arcname = Path("Tianshu Agent.app") / path.relative_to(app)
            info = archive.gettarinfo(str(path), arcname.as_posix())
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if info.isfile():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)


def release_artifact(
    *,
    bundled: Path,
    bundle_dir: Path,
    out_dir: Path,
    version: str,
    target: str,
) -> Path:
    if target not in TARGET_TRIPLE:
        raise PackageError(f"未知 target：{target}")
    if re.fullmatch(r"0\.[0-9]+\.[0-9]+", version) is None:
        raise PackageError("version 必须匹配 0.x.y")
    bundled = _validate_path_chain(bundled, "Tauri 发行物")
    bundle_metadata = _lstat(bundled, "Tauri 发行物")
    if bundle_metadata is None:
        raise PackageError(f"Tauri 发行物不存在：{bundled}")
    if _is_link(bundled):
        raise PackageError(f"Tauri 发行物不能是符号链接：{bundled}")
    if target.startswith("macos") and not stat.S_ISDIR(bundle_metadata.st_mode):
        raise PackageError(f"Tauri macOS 发行物不存在或不是目录：{bundled}")
    if not target.startswith("macos") and not stat.S_ISREG(bundle_metadata.st_mode):
        raise PackageError(f"Tauri Windows 发行物不存在或不是文件：{bundled}")
    if target.startswith("macos"):
        _validate_resource_tree(bundled, "Tauri macOS 发行物")
    out_dir = _prepare_output_directory(Path(out_dir))
    if target.startswith("macos"):
        destination = out_dir / f"Tianshu-Agent-{version}-{target}.app.tar.gz"
    else:
        destination = out_dir / f"Tianshu-Agent-{version}-{target}-setup.exe"
    checksum = out_dir / f"Tianshu-Agent-{version}-{target}.sha256"
    manifest_output = out_dir / "release-manifest.json"
    bundle_dir = _validated_directory(bundle_dir, "发布 bundle")
    source_manifest = bundle_dir / "release-manifest.json"
    _require_regular_file(source_manifest, "bundle release-manifest.json")
    for output in (destination, checksum, manifest_output):
        _require_safe_output_path(output)
    if target.startswith("macos"):
        _write_macos_archive(bundled, destination)
    else:
        shutil.copyfile(bundled, destination)
    checksum.write_bytes(
        f"{_sha256(destination)}  {destination.name}\n".encode("ascii")
    )
    shutil.copyfile(source_manifest, manifest_output)
    return destination


def package_app(
    *,
    repo_root: Path,
    target: str,
    bundle_dir: Path,
    debug: bool = False,
    target_triple: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    if target not in TARGET_TRIPLE:
        raise PackageError(f"未知 target：{target}")
    native = native_target()
    if target.split("-", 1)[0] != native.split("-", 1)[0]:
        raise PackageError(
            f"target {target} 与宿主 {native} 操作系统不一致；"
            "四目标发行必须在匹配 OS 的 runner 构建"
        )
    if target != native:
        raise PackageError(
            f"target {target} 与原生 target {native} 不一致；"
            "发行构建和 runtime smoke 必须在匹配架构的原生 runner 完成"
        )
    if target_triple is not None and target_triple != TARGET_TRIPLE[target]:
        raise PackageError(f"target {target} 的 Rust triple 必须是 {TARGET_TRIPLE[target]}")
    repo_root = _validated_directory(repo_root, "发布源仓库")
    bundle_dir = _validated_directory(bundle_dir, "发布 bundle")
    manifest = bundle_dir / "release-manifest.json"
    _require_regular_file(manifest, "发布清单")
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    version = manifest_data.get("version")
    if not isinstance(version, str):
        raise PackageError("发布清单缺少 version")
    for directory, label in (
        (bundle_dir / "python", "Tauri Python runtime 资源"),
        (bundle_dir / "app" / "tianshu", "Tauri app 资源"),
        (bundle_dir / "licenses", "Tauri license 资源"),
        (bundle_dir / "ui" / "dist", "Tauri UI 资源"),
    ):
        _validate_resource_tree(directory, label)
    _require_regular_file(bundle_dir / "licenses" / "LICENSE", "Tauri LICENSE")
    _require_regular_file(
        bundle_dir / "ui" / "dist" / "index.html",
        "Tauri UI index",
    )
    _require_regular_file(bundle_dir / "release.json", "Tauri release.json")

    desktop = _validated_directory(repo_root / "desktop", "desktop 发布目录")
    bundles = "app" if target.startswith("macos") else "nsis"
    command = [
        resolve_pnpm(),
        "dlx",
        TAURI_CLI,
        "build",
        "--bundles",
        bundles,
        "--config",
        json.dumps(tauri_config(bundle_dir, version), separators=(",", ":")),
    ]
    if target_triple is not None:
        command.extend(["--target", target_triple])
    if debug:
        command.append("--debug")
    result = subprocess.run(command, cwd=desktop)
    if result.returncode != 0:
        raise PackageError(f"Tauri build 失败，exit={result.returncode}")

    expected = bundle_path(
        repo_root,
        target,
        debug=debug,
        target_triple=target_triple,
        version=version,
    )
    if target.startswith("macos"):
        try:
            expected = _validated_directory(expected, "Tauri macOS 发行物")
            _validate_resource_tree(expected, "Tauri macOS 发行物")
            expected_exists = True
        except PackageError:
            expected_exists = False
    else:
        try:
            _require_regular_file(expected, "Tauri Windows 发行物")
            expected_exists = True
        except PackageError:
            expected_exists = False
    if not expected_exists:
        pattern = "*.app" if target.startswith("macos") else "*-setup.exe"
        present = sorted(expected.parent.glob(pattern))
        raise PackageError(
            f"未找到 Tauri 声明的唯一发行物：{expected}；"
            f"候选：{present}"
        )
    bundled = expected
    if out_dir is not None:
        return release_artifact(
            bundled=bundled,
            bundle_dir=bundle_dir,
            out_dir=out_dir,
            version=version,
            target=target,
        )
    return bundled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target", required=True, choices=sorted(TARGET_TRIPLE))
    parser.add_argument("--target-triple", choices=sorted(TARGET_TRIPLE.values()))
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    print(
        package_app(
            repo_root=args.repo_root,
            target=args.target,
            bundle_dir=args.bundle_dir,
            debug=args.debug,
            target_triple=args.target_triple,
            out_dir=args.out_dir,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
