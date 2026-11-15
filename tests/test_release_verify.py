from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
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


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_verify_fixture")
    bundle = tmp_path / "bundle"
    app = bundle / "app" / "tianshu"
    license_file = bundle / "licenses" / "LICENSE"
    python = bundle / "python" / "bin" / "python3.12"
    ui = bundle / "ui" / "dist"
    app.mkdir(parents=True)
    license_file.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    ui.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    license_file.write_text("Apache License 2.0\n", encoding="utf-8")
    python.write_bytes(_macho(0x0100000C))
    (ui / "index.html").write_text("<!doctype html>", encoding="utf-8")
    release = {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": COMMIT,
        "target": "macos-arm64",
    }
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
    packaged_python = resources / "python" / "bin" / "python3.12"
    packaged_license = resources / "licenses" / "LICENSE"
    packaged_ui = resources / "ui" / "dist"
    binary.parent.mkdir(parents=True)
    packaged_python.parent.mkdir(parents=True)
    packaged_license.parent.mkdir(parents=True)
    packaged_ui.mkdir(parents=True)
    binary.write_bytes(_macho(0x0100000C))
    packaged_python.write_bytes(_macho(0x0100000C))
    packaged_license.write_text("Apache License 2.0\n", encoding="utf-8")
    (resources / "release.json").write_bytes(release_bytes)
    (packaged_ui / "index.html").write_text("<!doctype html>", encoding="utf-8")
    package = _load(PACKAGE_SCRIPT, "package_app_verify_fixture")
    artifact = package.release_artifact(
        bundled=built,
        bundle_dir=bundle,
        out_dir=tmp_path / "out",
        version="0.1.0",
        target="macos-arm64",
    )
    return bundle, artifact.parent, artifact


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
