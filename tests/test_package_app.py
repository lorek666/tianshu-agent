from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "package_app.py"


def _load_module(*, stub_pnpm: bool = True):
    spec = importlib.util.spec_from_file_location("package_app_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if stub_pnpm:
        module.resolve_pnpm = lambda platform_name=None: (
            f"/tools/{module.pnpm_launcher(platform_name)}"
        )
    return module


def _package_bundle(tmp_path: Path) -> Path:
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "python").mkdir(parents=True)
    (bundle_dir / "app" / "tianshu").mkdir(parents=True)
    (bundle_dir / "licenses").mkdir()
    (bundle_dir / "ui" / "dist").mkdir(parents=True)
    (bundle_dir / "licenses" / "LICENSE").write_text("license", encoding="utf-8")
    (bundle_dir / "ui" / "dist" / "index.html").write_text(
        "<!doctype html>", encoding="utf-8"
    )
    (bundle_dir / "release.json").write_text("{}", encoding="utf-8")
    (bundle_dir / "release-manifest.json").write_text(
        json.dumps({"version": "0.1.0"}),
        encoding="utf-8",
    )
    return bundle_dir


def _expected_macos_app(tmp_path: Path) -> Path:
    triple = module_target_triple()
    app = (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / triple
        / "release"
        / "bundle"
        / "macos"
        / "Tianshu Agent.app"
    )
    binary = app / "Contents" / "MacOS" / "tianshu-desktop"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    return app


def module_target_triple() -> str:
    return "aarch64-apple-darwin"


def _mac_release_inputs(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "built" / "Tianshu Agent.app"
    binary = app / "Contents" / "MacOS" / "tianshu-desktop"
    resources = app / "Contents" / "Resources"
    binary.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    binary.write_bytes(b"binary")
    (resources / "release.json").write_text("{}", encoding="utf-8")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "release-manifest.json").write_text(
        json.dumps({"version": "0.1.0"}),
        encoding="utf-8",
    )
    return app, bundle_dir


def test_resource_config_maps_only_verified_bundle_inputs(tmp_path):
    module = _load_module()

    config = module.resource_config(tmp_path)

    assert config == {
        "bundle": {
            "resources": {
                str(tmp_path / "python"): "python",
                str(tmp_path / "app"): "app",
                str(tmp_path / "licenses"): "licenses",
                str(tmp_path / "ui" / "dist"): "ui/dist",
                str(tmp_path / "release.json"): "release.json",
                str(tmp_path / "release-manifest.json"): "release-manifest.json",
            }
        }
    }


def test_tauri_config_overrides_bundle_version(tmp_path):
    module = _load_module()

    config = module.tauri_config(tmp_path, "0.2.3")

    assert config["version"] == "0.2.3"
    assert config["bundle"] == module.resource_config(tmp_path)["bundle"]


def test_tauri_config_rejects_future_major_version(tmp_path):
    module = _load_module()

    with pytest.raises(module.PackageError, match="0.x.y"):
        module.tauri_config(tmp_path, "1.0.0")


def test_macos_bundle_path_is_target_specific(tmp_path):
    module = _load_module()

    assert module.bundle_path(tmp_path, "macos-arm64", debug=False) == (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "macos"
        / "Tianshu Agent.app"
    )


def test_windows_bundle_path_requires_setup_executable(tmp_path):
    module = _load_module()

    path = module.bundle_path(tmp_path, "windows-x64", debug=True)

    assert path == (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / "debug"
        / "bundle"
        / "nsis"
        / "Tianshu Agent_0.1.0_x64-setup.exe"
    )


def test_windows_arm64_bundle_path_is_target_specific(tmp_path):
    module = _load_module()

    path = module.bundle_path(
        tmp_path,
        "windows-arm64",
        debug=False,
        target_triple="aarch64-pc-windows-msvc",
        version="0.2.3",
    )

    assert path == (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / "aarch64-pc-windows-msvc"
        / "release"
        / "bundle"
        / "nsis"
        / "Tianshu Agent_0.2.3_arm64-setup.exe"
    )


def test_cross_arch_bundle_uses_explicit_rust_target_path(tmp_path):
    module = _load_module()

    path = module.bundle_path(
        tmp_path,
        "macos-x64",
        debug=False,
        target_triple="x86_64-apple-darwin",
    )

    assert path == (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / "x86_64-apple-darwin"
        / "release"
        / "bundle"
        / "macos"
        / "Tianshu Agent.app"
    )


def test_macos_release_artifact_is_checksum_bound_and_contains_app(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)

    artifact = module.release_artifact(
        bundled=app,
        bundle_dir=bundle_dir,
        out_dir=tmp_path / "out",
        version="0.1.0",
        target="macos-arm64",
    )

    assert artifact.name == "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    checksum = artifact.with_name("Tianshu-Agent-0.1.0-macos-arm64.sha256")
    digest, filename = checksum.read_text(encoding="ascii").split()
    assert filename == artifact.name
    assert digest == hashlib.sha256(artifact.read_bytes()).hexdigest()
    with tarfile.open(artifact, "r:gz") as archive:
        names = set(archive.getnames())
    assert "Tianshu Agent.app/Contents/MacOS/tianshu-desktop" in names
    assert "Tianshu Agent.app/Contents/Resources/release.json" in names


def test_release_checksum_uses_lf_bytes_when_text_mode_would_add_crlf(
    tmp_path, monkeypatch
):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    original_write_text = Path.write_text

    def windows_text_mode(path, data, *args, **kwargs):
        kwargs["newline"] = ""
        return original_write_text(
            path,
            data.replace("\n", "\r\n"),
            *args,
            **kwargs,
        )

    monkeypatch.setattr(Path, "write_text", windows_text_mode)

    artifact = module.release_artifact(
        bundled=app,
        bundle_dir=bundle_dir,
        out_dir=tmp_path / "out",
        version="0.1.0",
        target="macos-arm64",
    )

    checksum = artifact.with_name("Tianshu-Agent-0.1.0-macos-arm64.sha256")
    payload = checksum.read_bytes()
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert b"\r" not in payload
    digest, filename = payload.decode("ascii").rstrip("\n").split("  ")
    assert digest == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert filename == artifact.name


def test_release_artifact_rejects_version_path_traversal(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)

    with pytest.raises(module.PackageError, match="version"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle_dir,
            out_dir=tmp_path / "out",
            version="../../escape",
            target="macos-arm64",
        )

    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_release_artifact_rejects_symlinked_output_ancestor(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    ancestor = tmp_path / "alias"
    ancestor.symlink_to(external, target_is_directory=True)
    nested_out = ancestor / "nested"

    with pytest.raises(module.PackageError, match="输出目录路径.*链接"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle_dir,
            out_dir=nested_out,
            version="0.1.0",
            target="macos-arm64",
        )

    assert not (external / "nested").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_release_artifact_rejects_symlinked_output_directory(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    out_link = tmp_path / "out"
    out_link.symlink_to(external, target_is_directory=True)

    with pytest.raises(module.PackageError, match="输出目录不能是符号链接"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle_dir,
            out_dir=out_link,
            version="0.1.0",
            target="macos-arm64",
        )

    assert not any(external.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_release_artifact_rejects_symlinked_destination_without_writing(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    destination = out_dir / "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    external = tmp_path / "external.tar.gz"
    external.write_bytes(b"keep")
    destination.symlink_to(external)

    with pytest.raises(module.PackageError, match="输出路径不能是符号链接"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle_dir,
            out_dir=out_dir,
            version="0.1.0",
            target="macos-arm64",
        )

    assert external.read_bytes() == b"keep"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 硬链接负控")
def test_release_artifact_rejects_hardlinked_output_file(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    external = tmp_path / "outside-sentinel"
    external.write_bytes(b"sentinel")
    destination = out_dir / "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    os.link(external, destination)

    with pytest.raises(module.PackageError, match="硬链接"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle_dir,
            out_dir=out_dir,
            version="0.1.0",
            target="macos-arm64",
        )

    assert external.read_bytes() == b"sentinel"
    assert destination.read_bytes() == b"sentinel"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_release_artifact_rejects_symlinked_tauri_bundle(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    app_link = tmp_path / "Tianshu Agent.app.link"
    app_link.symlink_to(app, target_is_directory=True)

    with pytest.raises(module.PackageError, match="发行物不能是符号链接"):
        module.release_artifact(
            bundled=app_link,
            bundle_dir=bundle_dir,
            out_dir=tmp_path / "out",
            version="0.1.0",
            target="macos-arm64",
        )

    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_release_artifact_rejects_symlink_inside_macos_app(tmp_path):
    module = _load_module()
    app, bundle_dir = _mac_release_inputs(tmp_path)
    nested = app / "Contents" / "Resources" / "escape.txt"
    external = tmp_path / "outside-secret"
    external.write_text("must not be archived", encoding="utf-8")
    nested.symlink_to(external)

    with pytest.raises(module.PackageError, match="符号链接"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle_dir,
            out_dir=tmp_path / "out",
            version="0.1.0",
            target="macos-arm64",
        )

    assert not (tmp_path / "out").exists()


def test_cross_os_target_is_rejected(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")
    foreign = "windows-x64"

    with pytest.raises(module.PackageError, match="操作系统不一致"):
        module.package_app(
            repo_root=tmp_path,
            target=foreign,
            bundle_dir=tmp_path / "bundle",
            target_triple=module.TARGET_TRIPLE[foreign],
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_package_app_rejects_symlinked_bundle_root(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    real_bundle = _package_bundle(tmp_path)
    _expected_macos_app(tmp_path)
    bundle_link = tmp_path / "bundle-link"
    bundle_link.symlink_to(real_bundle, target_is_directory=True)

    with pytest.raises(module.PackageError, match="bundle.*符号链接|路径.*链接"):
        module.package_app(
            repo_root=tmp_path,
            target="macos-arm64",
            bundle_dir=bundle_link,
            target_triple=module_target_triple(),
            out_dir=tmp_path / "out",
        )

    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_package_app_rejects_symlink_inside_required_source_tree(
    tmp_path, monkeypatch
):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")
    calls: list[object] = []

    def unexpected_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(module.subprocess, "run", unexpected_run)
    bundle_dir = _package_bundle(tmp_path)
    _expected_macos_app(tmp_path)
    external = tmp_path / "outside-secret"
    external.write_text("must not be packaged", encoding="utf-8")
    (bundle_dir / "app" / "tianshu" / "escape.py").symlink_to(external)

    with pytest.raises(module.PackageError, match="符号链接|链接"):
        module.package_app(
            repo_root=tmp_path,
            target="macos-arm64",
            bundle_dir=bundle_dir,
            target_triple=module_target_triple(),
            out_dir=tmp_path / "out",
        )

    assert calls == []
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO 负控")
def test_package_app_rejects_nonregular_required_source(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    bundle_dir = _package_bundle(tmp_path)
    _expected_macos_app(tmp_path)
    license_path = bundle_dir / "licenses" / "LICENSE"
    license_path.unlink()
    os.mkfifo(license_path)

    with pytest.raises(module.PackageError, match="普通文件|特殊文件"):
        module.package_app(
            repo_root=tmp_path,
            target="macos-arm64",
            bundle_dir=bundle_dir,
            target_triple=module_target_triple(),
            out_dir=tmp_path / "out",
        )

    assert not (tmp_path / "out").exists()


def test_cross_arch_target_is_rejected_even_on_matching_os(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")

    with pytest.raises(module.PackageError, match="原生 target"):
        module.package_app(
            repo_root=tmp_path,
            target="macos-x64",
            bundle_dir=tmp_path / "bundle",
            target_triple="x86_64-apple-darwin",
        )


@pytest.mark.parametrize(
    ("host_os", "expected_launcher"),
    [("posix", "pnpm"), ("nt", "pnpm.cmd")],
)
def test_package_app_uses_the_platform_pnpm_launcher(
    tmp_path, monkeypatch, host_os, expected_launcher
):
    module = _load_module(stub_pnpm=False)
    commands = []
    resolved = str(tmp_path / expected_launcher)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda launcher: resolved if launcher == expected_launcher else None,
    )
    assert module.resolve_pnpm(host_os) == resolved
    monkeypatch.setattr(module, "native_target", lambda: "windows-x64")
    monkeypatch.setattr(module, "resolve_pnpm", lambda: resolved)
    (tmp_path / "desktop").mkdir()

    def failed_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(module.subprocess, "run", failed_run)
    bundle_dir = _package_bundle(tmp_path)

    with pytest.raises(module.PackageError, match="Tauri build 失败"):
        module.package_app(
            repo_root=tmp_path,
            target="windows-x64",
            bundle_dir=bundle_dir,
            target_triple=module.TARGET_TRIPLE["windows-x64"],
        )

    assert commands[0][0] == resolved


def test_resolve_pnpm_reports_a_missing_launcher(monkeypatch):
    module = _load_module(stub_pnpm=False)
    monkeypatch.setattr(module.shutil, "which", lambda _launcher: None)

    with pytest.raises(module.PackageError, match=r"pnpm 启动器：pnpm\.cmd"):
        module.resolve_pnpm("nt")


def test_resolve_pnpm_returns_an_absolute_executable_for_relative_path(
    tmp_path, monkeypatch
):
    module = _load_module(stub_pnpm=False)
    tools = tmp_path / "tools"
    tools.mkdir()
    launcher = tools / module.pnpm_launcher()
    if os.name == "nt":
        launcher.write_text("@echo off\r\n", encoding="ascii")
    else:
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        launcher.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"tools{os.pathsep}{os.environ.get('PATH', '')}")

    resolved = module.resolve_pnpm()

    assert Path(resolved).is_absolute()
    assert Path(resolved).resolve() == launcher.resolve()


def test_package_app_rejects_a_missing_desktop_before_launcher(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "windows-x64")
    bundle_dir = _package_bundle(tmp_path)

    with pytest.raises(module.PackageError, match="desktop 发布目录"):
        module.package_app(
            repo_root=tmp_path,
            target="windows-x64",
            bundle_dir=bundle_dir,
            target_triple=module.TARGET_TRIPLE["windows-x64"],
        )


@pytest.mark.parametrize("missing_path", ["/other/pnpm.cmd", None])
def test_package_app_does_not_hide_unrelated_file_not_found(
    tmp_path, monkeypatch, missing_path
):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "windows-x64")
    monkeypatch.setattr(module, "resolve_pnpm", lambda: "/tools/pnpm.cmd")
    (tmp_path / "desktop").mkdir()

    def unrelated_run(command, **_kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified", missing_path)

    monkeypatch.setattr(module.subprocess, "run", unrelated_run)
    bundle_dir = _package_bundle(tmp_path)

    with pytest.raises(FileNotFoundError):
        module.package_app(
            repo_root=tmp_path,
            target="windows-x64",
            bundle_dir=bundle_dir,
            target_triple=module.TARGET_TRIPLE["windows-x64"],
        )


def test_macos_package_accepts_real_app_directory_and_writes_archive(
    tmp_path, monkeypatch
):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )

    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "python").mkdir(parents=True)
    (bundle_dir / "app" / "tianshu").mkdir(parents=True)
    (bundle_dir / "licenses").mkdir()
    (bundle_dir / "ui" / "dist").mkdir(parents=True)
    (bundle_dir / "licenses" / "LICENSE").write_text("license", encoding="utf-8")
    (bundle_dir / "ui" / "dist" / "index.html").write_text(
        "<!doctype html>", encoding="utf-8"
    )
    (bundle_dir / "release.json").write_text("{}", encoding="utf-8")
    (bundle_dir / "release-manifest.json").write_text(
        json.dumps({"version": "0.1.0"}),
        encoding="utf-8",
    )

    triple = module.TARGET_TRIPLE["macos-arm64"]
    app = (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / triple
        / "release"
        / "bundle"
        / "macos"
        / "Tianshu Agent.app"
    )
    binary = app / "Contents" / "MacOS" / "tianshu-desktop"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")

    artifact = module.package_app(
        repo_root=tmp_path,
        target="macos-arm64",
        bundle_dir=bundle_dir,
        target_triple=triple,
        out_dir=tmp_path / "out",
    )

    assert artifact.name == "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    with tarfile.open(artifact, "r:gz") as archive:
        assert "Tianshu Agent.app/Contents/MacOS/tianshu-desktop" in archive.getnames()


def test_windows_package_rejects_renamed_nsis_even_when_build_succeeds(
    tmp_path, monkeypatch
):
    module = _load_module()
    monkeypatch.setattr(module, "native_target", lambda: "windows-x64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )

    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "python").mkdir(parents=True)
    (bundle_dir / "app" / "tianshu").mkdir(parents=True)
    (bundle_dir / "licenses").mkdir()
    (bundle_dir / "ui" / "dist").mkdir(parents=True)
    (bundle_dir / "licenses" / "LICENSE").write_text("license", encoding="utf-8")
    (bundle_dir / "ui" / "dist" / "index.html").write_text(
        "<!doctype html>", encoding="utf-8"
    )
    (bundle_dir / "release.json").write_text("{}", encoding="utf-8")
    (bundle_dir / "release-manifest.json").write_text(
        json.dumps({"version": "0.1.0"}),
        encoding="utf-8",
    )

    triple = module.TARGET_TRIPLE["windows-x64"]
    nsis_dir = (
        tmp_path
        / "desktop"
        / "src-tauri"
        / "target"
        / triple
        / "release"
        / "bundle"
        / "nsis"
    )
    nsis_dir.mkdir(parents=True)
    wrong = nsis_dir / "Unexpected_0.1.0_x64-setup.exe"
    wrong.write_bytes(b"installer")

    with pytest.raises(module.PackageError, match="唯一发行物"):
        module.package_app(
            repo_root=tmp_path,
            target="windows-x64",
            bundle_dir=bundle_dir,
            target_triple=triple,
            out_dir=tmp_path / "out",
        )

    assert not (tmp_path / "out").exists()
