from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import struct
import tarfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "scripts" / "package_app.py"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_release.py"
RELEASE_SCRIPT = ROOT / "scripts" / "release_manifest.py"
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _macho(cpu_type: int) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu_type) + b"\0" * 64


def _pe(machine: int) -> bytes:
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, machine)
    return bytes(data)


def _release_identity(target: str = "macos-arm64") -> dict:
    return {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": COMMIT,
        "target": target,
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_verify_fixture")
    bundle = tmp_path / "bundle"
    app = bundle / "app" / "tianshu"
    license_file = bundle / "licenses" / "LICENSE"
    python = bundle / "python" / "bin" / "python3"
    ui = bundle / "ui" / "dist"
    app.mkdir(parents=True)
    license_file.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    ui.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "host.py").write_text("VERSION = 'verified'\n", encoding="utf-8")
    license_file.write_text("Apache License 2.0\n", encoding="utf-8")
    python.write_bytes(_macho(0x0100000C))
    (python.parent / "python3.12").write_bytes(_macho(0x0100000C))
    (ui / "index.html").write_text("<!doctype html>", encoding="utf-8")
    release = _release_identity()
    release_bytes = (json.dumps(release, indent=2) + "\n").encode()
    (bundle / "release.json").write_bytes(release_bytes)
    files = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            relative = path.relative_to(bundle).as_posix()
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        **release,
        "python": "3.12.16",
        "python_build_release": "20261110",
        "ui_dist_sha256": files["ui/dist/index.html"],
        "files": files,
    }
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)

    built = tmp_path / "Tianshu Agent.app"
    binary = built / "Contents" / "MacOS" / "tianshu-desktop"
    resources = built / "Contents" / "Resources"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(_macho(0x0100000C))
    (built / "Contents" / "Info.plist").write_text(
        "<?xml version=\"1.0\"?><plist><dict/></plist>",
        encoding="utf-8",
    )
    shutil.copytree(bundle, resources)
    (resources / "icon.icns").write_bytes(b"tauri-generated-icon")
    package = _load(PACKAGE_SCRIPT, "package_app_verify_fixture")
    artifact = package.release_artifact(
        bundled=built,
        bundle_dir=bundle,
        out_dir=tmp_path / "out",
        version="0.1.0",
        target="macos-arm64",
    )
    return bundle, artifact.parent, artifact


def _windows_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_windows_fixture")
    bundle = tmp_path / "bundle"
    app = bundle / "app" / "tianshu"
    python = bundle / "python" / "python.exe"
    ui = bundle / "ui" / "dist"
    licenses = bundle / "licenses"
    app.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    ui.mkdir(parents=True)
    licenses.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "host.py").write_text("VERSION = 'verified'\n", encoding="utf-8")
    python.write_bytes(_pe(0x8664))
    (ui / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (licenses / "LICENSE").write_text("Apache License 2.0\n", encoding="utf-8")
    release = _release_identity("windows-x64")
    (bundle / "release.json").write_text(
        json.dumps(release, indent=2) + "\n",
        encoding="utf-8",
    )
    files = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            relative = path.relative_to(bundle).as_posix()
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        **release,
        "python": "3.12.16",
        "python_build_release": "20261110",
        "ui_dist_sha256": files["ui/dist/index.html"],
        "files": files,
    }
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")
    return bundle, installer, manifest


def _rewrite_archive(
    artifact_dir: Path,
    artifact: Path,
    mutate,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with tarfile.open(artifact, "r:gz") as archive:
            archive.extractall(root, filter="data")
        mutate(root / "Tianshu Agent.app")
        artifact.unlink()
        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(root / "Tianshu Agent.app", arcname="Tianshu Agent.app")
    checksum = artifact.with_name(
        artifact.name.removesuffix(".app.tar.gz") + ".sha256"
    )
    checksum.write_text(
        f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n",
        encoding="ascii",
    )


def test_verify_release_checks_manifest_checksum_arch_and_bound_ui(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_under_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)

    result = verify.verify_release(
        artifact_dir=artifact_dir,
        bundle_dir=bundle,
        target="macos-arm64",
        version="0.1.0",
        commit=COMMIT,
        run_smoke=False,
    )

    assert result["artifact"] == artifact.name
    assert result["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert result["manifest_sha256"] == hashlib.sha256(
        (bundle / "release-manifest.json").read_bytes()
    ).hexdigest()


def test_verify_release_rejects_tampered_artifact(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_tamper_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    artifact.write_bytes(artifact.read_bytes() + b"tamper")

    with pytest.raises(verify.VerifyError, match="SHA256 不匹配"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_missing_listed_app_payload(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_missing_payload_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)

    def mutate(app: Path) -> None:
        (app / "Contents" / "Resources" / "app" / "tianshu" / "host.py").unlink()

    _rewrite_archive(artifact_dir, artifact, mutate)

    with pytest.raises(verify.VerifyError, match="缺少文件|引用的文件"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_tampered_listed_file_after_archive_sha_rebound(
    tmp_path,
):
    verify = _load(VERIFY_SCRIPT, "verify_release_listed_tamper_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)

    def mutate(app: Path) -> None:
        (app / "Contents" / "Resources" / "app" / "tianshu" / "host.py").write_text(
            "VERSION = 'tampered'\n",
            encoding="utf-8",
        )

    _rewrite_archive(artifact_dir, artifact, mutate)

    with pytest.raises(verify.VerifyError, match="SHA256 不匹配"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_unlisted_archive_import_hook(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_unlisted_archive_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)

    def mutate(app: Path) -> None:
        (app / "Contents" / "Resources" / "app" / "sitecustomize.py").write_text(
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )

    _rewrite_archive(artifact_dir, artifact, mutate)

    with pytest.raises(verify.VerifyError, match="未列文件"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_release_identity_drift_from_manifest(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_identity_test")
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_identity_drift")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    release = _release_identity()
    release["commit"] = "0" * 40
    (bundle / "release.json").write_text(
        json.dumps(release, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads(
        (bundle / "release-manifest.json").read_text(encoding="utf-8")
    )
    manifest["files"]["release.json"] = hashlib.sha256(
        (bundle / "release.json").read_bytes()
    ).hexdigest()
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)
    (artifact_dir / "release-manifest.json").write_bytes(
        (bundle / "release-manifest.json").read_bytes()
    )

    with pytest.raises(verify.VerifyError, match="release.json"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_non_closed_release_identity(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_identity_fields_test")
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_identity_fields")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    release = {**_release_identity(), "source": "unbound"}
    (bundle / "release.json").write_text(
        json.dumps(release, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads(
        (bundle / "release-manifest.json").read_text(encoding="utf-8")
    )
    manifest["files"]["release.json"] = hashlib.sha256(
        (bundle / "release.json").read_bytes()
    ).hexdigest()
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)
    (artifact_dir / "release-manifest.json").write_bytes(
        (bundle / "release-manifest.json").read_bytes()
    )

    with pytest.raises(verify.VerifyError, match="字段必须封闭"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_archived_manifest_drift(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_manifest_drift_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)

    def mutate(app: Path) -> None:
        manifest_path = app / "Contents" / "Resources" / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.1"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    _rewrite_archive(artifact_dir, artifact, mutate)

    with pytest.raises(verify.VerifyError, match="release-manifest.*不一致"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_macos_python_arch_mismatch(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_python_arch_test")
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_python_arch")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    bundled_python = bundle / "python" / "bin" / "python3"
    bundled_python.write_bytes(_macho(0x01000007))
    manifest_path = bundle / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["python/bin/python3"] = hashlib.sha256(
        bundled_python.read_bytes()
    ).hexdigest()
    release_manifest.write_manifest(manifest_path, manifest)
    (artifact_dir / "release-manifest.json").write_bytes(manifest_path.read_bytes())

    def mutate(app: Path) -> None:
        (app / "Contents" / "Resources" / "python" / "bin" / "python3").write_bytes(
            bundled_python.read_bytes()
        )
        archived_manifest = app / "Contents" / "Resources" / "release-manifest.json"
        archived_manifest.write_bytes(manifest_path.read_bytes())

    _rewrite_archive(artifact_dir, artifact, mutate)

    with pytest.raises(verify.VerifyError, match="python3.*CPU 架构"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target="macos-arm64",
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_runtime_smoke_rejects_non_native_target(tmp_path, monkeypatch):
    verify = _load(VERIFY_SCRIPT, "verify_release_runtime_native_test")
    monkeypatch.setattr(verify, "host_target", lambda: "macos-arm64")

    with pytest.raises(verify.VerifyError, match="原生 target"):
        verify._runtime_smoke(tmp_path, "macos-x64")


def test_windows_verifier_extracts_and_binds_exact_payload(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_test")
    bundle, installer, _manifest = _windows_fixture(tmp_path)

    def extract(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)

    verify._verify_windows_artifact(
        artifact=installer,
        bundle_dir=bundle,
        target="windows-x64",
        extractor=extract,
    )
    verify._verify_windows_bundle(bundle, "windows-x64")

    python = bundle / "python" / "python.exe"
    python.write_bytes(_pe(0x8664))
    with pytest.raises(verify.VerifyError, match="PE 架构"):
        bad_bundle = tmp_path / "bad-bundle"
        shutil.copytree(bundle, bad_bundle)
        (bad_bundle / "python" / "python.exe").write_bytes(_pe(0xAA64))
        verify._verify_windows_bundle(bad_bundle, "windows-x64")


def test_windows_verifier_rejects_pe_without_nsis_payload(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_synthetic_test")
    _bundle, installer, _manifest = _windows_fixture(tmp_path)
    installer.write_bytes(_pe(0x014C))

    def extract(_artifact: Path, _destination: Path) -> None:
        raise AssertionError("缺少 NSIS 标识时不得调用解包器")

    with pytest.raises(verify.VerifyError, match="NSIS"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=tmp_path / "bundle",
            target="windows-x64",
            extractor=extract,
        )


def test_windows_verifier_rejects_payload_manifest_drift(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_manifest_drift")
    bundle, installer, _manifest = _windows_fixture(tmp_path)

    def extract(artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        manifest_path = destination / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(verify.VerifyError, match="release-manifest.*不一致"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extract,
        )


def test_windows_verifier_rejects_unlisted_python_payload(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_extra_payload")
    bundle, installer, _manifest = _windows_fixture(tmp_path)

    def extract(artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        (destination / "app" / "sitecustomize.py").write_text(
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )

    with pytest.raises(verify.VerifyError, match="未列文件"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extract,
        )


def test_archive_link_targets_are_confined_to_archive():
    verify = _load(VERIFY_SCRIPT, "verify_release_links_test")
    internal = tarfile.TarInfo("Tianshu Agent.app/Contents/Resources/app/link.py")
    internal.type = tarfile.SYMTYPE
    internal.linkname = "../ui/dist/index.html"
    verify._member_path(internal)

    escaped = tarfile.TarInfo("Tianshu Agent.app/Contents/Resources/app/link.py")
    escaped.type = tarfile.SYMTYPE
    escaped.linkname = "../../../../../../etc/passwd"
    with pytest.raises(verify.VerifyError, match="逃逸链接"):
        verify._member_path(escaped)
