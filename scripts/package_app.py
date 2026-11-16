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


def _lstat(path: Path, label: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PackageError(f"{label} 无法检查：{exc}") from exc


def _require_regular_file(path: Path, label: str) -> None:
    metadata = _lstat(path, label)
    if metadata is None:
        raise PackageError(f"{label} 不存在：{path}")
    if stat.S_ISLNK(metadata.st_mode):
        raise PackageError(f"{label} 不能是符号链接：{path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PackageError(f"{label} 必须是普通文件：{path}")


def _prepare_output_directory(path: Path) -> Path:
    requested = Path(path).expanduser()
    metadata = _lstat(requested, "发行输出目录")
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            raise PackageError(f"发行输出目录不能是符号链接：{requested}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PackageError(f"发行输出目录必须是普通目录：{requested}")
    requested.mkdir(parents=True, exist_ok=True)
    metadata = _lstat(requested, "发行输出目录")
    assert metadata is not None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PackageError(f"发行输出目录必须是普通目录：{requested}")
    return requested.resolve(strict=True)


def _require_safe_output_path(path: Path) -> None:
    metadata = _lstat(path, "发行输出路径")
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise PackageError(f"发行输出路径不能是符号链接：{path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PackageError(f"发行输出路径必须是普通文件：{path}")


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
    bundle_metadata = _lstat(Path(bundled), "Tauri 发行物")
    if bundle_metadata is None:
        raise PackageError(f"Tauri 发行物不存在：{bundled}")
    if stat.S_ISLNK(bundle_metadata.st_mode):
        raise PackageError(f"Tauri 发行物不能是符号链接：{bundled}")
    if target.startswith("macos") and not stat.S_ISDIR(bundle_metadata.st_mode):
        raise PackageError(f"Tauri macOS 发行物不存在或不是目录：{bundled}")
    if not target.startswith("macos") and not stat.S_ISREG(bundle_metadata.st_mode):
        raise PackageError(f"Tauri Windows 发行物不存在或不是文件：{bundled}")
    out_dir = _prepare_output_directory(Path(out_dir))
    if target.startswith("macos"):
        destination = out_dir / f"Tianshu-Agent-{version}-{target}.app.tar.gz"
    else:
        destination = out_dir / f"Tianshu-Agent-{version}-{target}-setup.exe"
    checksum = out_dir / f"Tianshu-Agent-{version}-{target}.sha256"
    manifest_output = out_dir / "release-manifest.json"
    source_manifest = Path(bundle_dir) / "release-manifest.json"
    _require_regular_file(source_manifest, "bundle release-manifest.json")
    for output in (destination, checksum, manifest_output):
        _require_safe_output_path(output)
    if target.startswith("macos"):
        _write_macos_archive(bundled, destination)
    else:
        shutil.copyfile(bundled, destination)
    checksum.write_text(f"{_sha256(destination)}  {destination.name}\n", encoding="ascii")
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
    repo_root = Path(repo_root).resolve()
    bundle_dir = Path(bundle_dir).resolve()
    manifest = bundle_dir / "release-manifest.json"
    if not manifest.is_file():
        raise PackageError(f"发布清单不存在：{manifest}")
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    version = manifest_data.get("version")
    if not isinstance(version, str):
        raise PackageError("发布清单缺少 version")
    for required in (
        bundle_dir / "python",
        bundle_dir / "app" / "tianshu",
        bundle_dir / "licenses" / "LICENSE",
        bundle_dir / "ui" / "dist" / "index.html",
        bundle_dir / "release.json",
    ):
        if not required.exists():
            raise PackageError(f"Tauri 资源输入不存在：{required}")

    bundles = "app" if target.startswith("macos") else "nsis"
    command = [
        "pnpm",
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
    result = subprocess.run(command, cwd=repo_root / "desktop")
    if result.returncode != 0:
        raise PackageError(f"Tauri build 失败，exit={result.returncode}")

    expected = bundle_path(
        repo_root,
        target,
        debug=debug,
        target_triple=target_triple,
        version=version,
    )
    expected_exists = (
        expected.is_dir() if target.startswith("macos") else expected.is_file()
    )
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
