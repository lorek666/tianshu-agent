from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import platform
import shutil
import struct
import sys
import tarfile
import time
from pathlib import Path

import pytest
from _startup.release_smoke_test_launcher import SmokeTestIsolation

from tianshu import __version__, release_smoke

ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release_manifest.py"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_release.py"
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"
TARGET = "macos-arm64"
APP_ROOT = "Tianshu Agent.app"
RESOURCE_PREFIX = f"{APP_ROOT}/Contents/Resources"
NATIVE_TARGET: str | None
_system = platform.system().lower()
_machine = platform.machine().lower()
if _system == "darwin" and _machine in {"arm64", "aarch64"}:
    NATIVE_TARGET = "macos-arm64"
elif _system == "darwin" and _machine in {"x86_64", "amd64"}:
    NATIVE_TARGET = "macos-x64"
elif _system == "windows" and _machine in {"arm64", "aarch64"}:
    NATIVE_TARGET = "windows-arm64"
elif _system == "windows" and _machine in {"x86_64", "amd64"}:
    NATIVE_TARGET = "windows-x64"
else:
    NATIVE_TARGET = None
NATIVE_RUNTIME_REASON = (
    "发行 bundle 的 runtime smoke 只适用于受支持的 macOS/Windows 原生宿主"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_macos_archive(app: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(app.rglob("*")):
            archive.add(
                path,
                arcname=(Path(APP_ROOT) / path.relative_to(app)).as_posix(),
                recursive=False,
            )


def _macho(cpu_type: int = 0x0100000C) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu_type) + b"\0" * 64


def _pe(machine: int) -> bytes:
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, machine)
    return bytes(data)


def _release_identity(
    *,
    version: str = "0.1.0",
    commit: str = COMMIT,
    target: str = TARGET,
) -> dict:
    return {
        "schema": 1,
        "product": "tianshu-agent",
        "version": version,
        "commit": commit,
        "target": target,
    }


def _collect_files(bundle: Path) -> dict[str, str]:
    return {
        path.relative_to(bundle).as_posix(): _sha(path.read_bytes())
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name != "release-manifest.json"
    }


def _write_manifest(bundle: Path) -> dict:
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_fixture_verify")
    identity = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    files = _collect_files(bundle)
    ui_path = "ui/dist/index.html"
    assert ui_path in files
    manifest = {
        **identity,
        "python": "3.12.16",
        "python_build_release": "20261110",
        "ui_dist_sha256": files[ui_path],
        "files": files,
    }
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)
    return manifest


def _update_manifest(bundle: Path, **updates) -> dict:
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_fixture_verify_update")
    manifest = json.loads(
        (bundle / "release-manifest.json").read_text(encoding="utf-8")
    )
    manifest.update(updates)
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)
    return manifest


def _make_bundle(tmp_path: Path, *, target: str = TARGET) -> Path:
    bundle = tmp_path / "bundle"
    package = bundle / "app" / "tianshu"
    python_bin = bundle / "python" / "bin"
    ui_dist = bundle / "ui" / "dist"
    licenses = bundle / "licenses"
    package.mkdir(parents=True)
    python_bin.mkdir(parents=True)
    (ui_dist / "assets").mkdir(parents=True)
    licenses.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (package / "__main__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    cpu = 0x0100000C if target.endswith("arm64") else 0x01000007
    runtime = _macho(cpu)
    (python_bin / "python3").write_bytes(runtime)
    (python_bin / "python").write_bytes(runtime)
    (python_bin / "python3.12").write_bytes(runtime)
    (ui_dist / "index.html").write_text("<!doctype html><title>Tianshu</title>\n", encoding="utf-8")
    (ui_dist / "assets" / "app.js").write_text("export const ready = true;\n", encoding="utf-8")
    (licenses / "LICENSE").write_text("Apache License 2.0\n", encoding="utf-8")
    (bundle / "release.json").write_text(
        json.dumps(_release_identity(target=target), indent=2) + "\n",
        encoding="utf-8",
    )
    _write_manifest(bundle)
    return bundle


def _build_artifact(tmp_path: Path, bundle: Path, target: str = TARGET) -> tuple[Path, Path]:
    built = tmp_path / "built" / f"{APP_ROOT}"
    binary = built / "Contents" / "MacOS" / "tianshu-desktop"
    resources = built / "Contents" / "Resources"
    binary.parent.mkdir(parents=True)
    resources.parent.mkdir(parents=True, exist_ok=True)
    cpu = 0x0100000C if target.endswith("arm64") else 0x01000007
    binary.write_bytes(_macho(cpu))
    (built / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0"?><plist><dict/></plist>\n',
        encoding="utf-8",
    )
    shutil.copytree(bundle, resources)
    (resources / "icon.icns").write_bytes(b"tauri-generated-icon")
    artifact_dir = tmp_path / "out"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / f"Tianshu-Agent-0.1.0-{target}.app.tar.gz"
    _write_macos_archive(built, artifact)
    (artifact_dir / "release-manifest.json").write_bytes(
        (bundle / "release-manifest.json").read_bytes()
    )
    (artifact_dir / f"Tianshu-Agent-0.1.0-{target}.sha256").write_bytes(
        f"{_sha(artifact.read_bytes())}  {artifact.name}\n".encode("ascii"),
    )
    return artifact.parent, artifact


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    bundle = _make_bundle(tmp_path)
    artifact_dir, artifact = _build_artifact(tmp_path, bundle)
    return bundle, artifact_dir, artifact


def _rewrite_checksum(artifact: Path) -> None:
    checksum = artifact.with_name(
        artifact.name.removesuffix(".app.tar.gz") + ".sha256"
    )
    checksum.write_bytes(
        f"{_sha(artifact.read_bytes())}  {artifact.name}\n".encode("ascii"),
    )


def _replace_tar_member(artifact: Path, name: str, data: bytes) -> None:
    temporary = artifact.with_suffix(".tmp")
    with tarfile.open(artifact, "r:gz") as source, tarfile.open(
        temporary, "w:gz"
    ) as destination:
        replaced = False
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isfile() else None
            if member.name == name:
                member.size = len(data)
                stream = io.BytesIO(data)
                replaced = True
            destination.addfile(member, stream)
        assert replaced
    temporary.replace(artifact)
    _rewrite_checksum(artifact)


def _append_tar_member(artifact: Path, member: tarfile.TarInfo, data: bytes | None = None) -> None:
    temporary = artifact.with_suffix(".tmp")
    with tarfile.open(artifact, "r:gz") as source, tarfile.open(
        temporary, "w:gz"
    ) as destination:
        for existing in source.getmembers():
            stream = source.extractfile(existing) if existing.isfile() else None
            destination.addfile(existing, stream)
        if data is not None:
            member.size = len(data)
        destination.addfile(member, io.BytesIO(data) if data is not None else None)
    temporary.replace(artifact)
    _rewrite_checksum(artifact)


def _windows_bundle(tmp_path: Path, *, target: str = "windows-x64") -> Path:
    bundle = tmp_path / "windows-bundle"
    package = bundle / "app" / "tianshu"
    python_dir = bundle / "python"
    ui_dist = bundle / "ui" / "dist"
    licenses = bundle / "licenses"
    package.mkdir(parents=True)
    python_dir.mkdir()
    ui_dist.mkdir(parents=True)
    licenses.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    machine = 0xAA64 if target.endswith("arm64") else 0x8664
    (python_dir / "python.exe").write_bytes(_pe(machine))
    (ui_dist / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (licenses / "LICENSE").write_text("Apache License 2.0\n", encoding="utf-8")
    (bundle / "release.json").write_text(
        json.dumps(_release_identity(target=target), indent=2) + "\n",
        encoding="utf-8",
    )
    _write_manifest(bundle)
    return bundle


def _add_nsis_support_files(destination: Path, *, target: str) -> None:
    machine = 0xAA64 if target.endswith("arm64") else 0x8664
    (destination / "tianshu-desktop.exe").write_bytes(_pe(machine))
    plugins = destination / "$PLUGINSDIR"
    plugins.mkdir(parents=True, exist_ok=True)
    for name in (
        "NSISdl.dll",
        "StartMenu.dll",
        "System.dll",
        "nsDialogs.dll",
        "nsis_tauri_utils.dll",
    ):
        (plugins / name).write_bytes(_pe(0x014C))
    (plugins / "modern-wizard.bmp").write_bytes(b"fixed wizard bitmap")
    (destination / "uninstall.exe").write_bytes(_pe(0x014C))


def test_verify_release_accepts_exact_manifest_bytes_and_archive(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_under_test")
    bundle, artifact_dir, artifact = _fixture(tmp_path)

    result = verify.verify_release(
        artifact_dir=artifact_dir,
        bundle_dir=bundle,
        target=TARGET,
        version="0.1.0",
        commit=COMMIT,
        run_smoke=False,
    )

    assert result["artifact"] == artifact.name
    assert result["sha256"] == _sha(artifact.read_bytes())
    assert result["runtime_smoke"] == {"status": "not_run_by_caller"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", 2, "schema"),
        ("product", "other", "product"),
        ("version", "1.0.0", "version"),
        ("commit", "A" * 40, "commit"),
        ("target", "linux-x64", "target"),
    ],
)
def test_verify_release_rejects_non_closed_short_identity(
    tmp_path, field, value, message
):
    verify = _load(VERIFY_SCRIPT, f"verify_release_short_{field}")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    release = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    release[field] = value
    (bundle / "release.json").write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(verify.VerifyError, match=message):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_short_identity_drift_even_when_all_hashes_rebind(
    tmp_path,
):
    verify = _load(VERIFY_SCRIPT, "verify_release_self_consistent_drift")
    bundle = _make_bundle(tmp_path)
    release = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    release["commit"] = "0" * 40
    (bundle / "release.json").write_text(json.dumps(release), encoding="utf-8")
    _write_manifest(bundle)
    artifact_dir, artifact = _build_artifact(tmp_path / "rebuilt", bundle)

    with pytest.raises(verify.VerifyError, match="commit.*期望"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )
    assert artifact.is_file()


def test_verify_release_rejects_unknown_short_identity_field(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_extra_identity")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    release = {**_release_identity(), "source": "unbound"}
    (bundle / "release.json").write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(verify.VerifyError, match="字段必须封闭"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_manifest_file_not_byte_identical_to_bundle(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_manifest_bytes")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    copied = artifact_dir / "release-manifest.json"
    copied.write_bytes(copied.read_bytes() + b" ")

    with pytest.raises(verify.VerifyError, match="release-manifest.json.*字节不一致"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_tampered_checksum(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_checksum")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    checksum = artifact_dir / f"Tianshu-Agent-0.1.0-{TARGET}.sha256"
    checksum.write_bytes(f"{'0' * 64}  {artifact.name}\n".encode("ascii"))

    with pytest.raises(verify.VerifyError, match="SHA256 不匹配"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_unlisted_artifact_directory_file(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_artifact_extra")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    (artifact_dir / "events.db").write_bytes(b"user data")

    with pytest.raises(verify.VerifyError, match="发行目录文件集合"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_artifact_directory_subdirectory(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_artifact_subdirectory")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    (artifact_dir / "logs").mkdir()

    with pytest.raises(verify.VerifyError, match="发行目录文件集合"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_source_bundle_rejects_unlisted_sitecustomize(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_source_sitecustomize")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    (bundle / "app" / "sitecustomize.py").write_text(
        "raise SystemExit(91)\n", encoding="utf-8"
    )

    with pytest.raises(verify.VerifyError, match="bundle 文件集合.*未列文件"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_archive_rejects_unlisted_sitecustomize(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_archive_sitecustomize")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    member = tarfile.TarInfo(f"{RESOURCE_PREFIX}/app/sitecustomize.py")
    _append_tar_member(artifact, member, b"raise SystemExit(91)\n")

    with pytest.raises(verify.VerifyError, match="资源集合.*未列文件"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.production",
        ".tianshu/events.db",
        "logs/release.log",
        "tests/test_secret.py",
        ".omc/state.json",
        "__pycache__/host.cpython-312.pyc",
    ],
)
def test_source_bundle_rejects_forbidden_files_even_if_relisted(tmp_path, relative):
    verify = _load(VERIFY_SCRIPT, f"verify_release_forbidden_{relative}")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    path = bundle / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"forbidden")
    _write_manifest(bundle)
    (artifact_dir / "release-manifest.json").write_bytes(
        (bundle / "release-manifest.json").read_bytes()
    )

    with pytest.raises(verify.VerifyError, match="禁止|环境"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_archive_manifest_drift_even_with_new_archive_sha(
    tmp_path,
):
    verify = _load(VERIFY_SCRIPT, "verify_release_archive_manifest_drift")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    manifest_path = f"{RESOURCE_PREFIX}/release-manifest.json"
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "0.1.1"
    _replace_tar_member(
        artifact,
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )

    with pytest.raises(verify.VerifyError, match="release-manifest.json.*字节不一致"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_archive_rejects_escaping_symlink(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_archive_escape_link")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    link = tarfile.TarInfo(f"{RESOURCE_PREFIX}/app/escape.py")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../../../../../etc/passwd"
    _append_tar_member(artifact, link)

    with pytest.raises(verify.VerifyError, match="逃逸链接"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_archive_rejects_duplicate_member_names(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_duplicate_member")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    duplicate = tarfile.TarInfo(f"{RESOURCE_PREFIX}/ui/dist/index.html")
    _append_tar_member(artifact, duplicate, b"second index bytes")

    with pytest.raises(verify.VerifyError, match="重复"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_archive_rejects_file_at_macos_app_root(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_macos_root_extra")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    root_extra = tarfile.TarInfo(f"{APP_ROOT}/root-extra.txt")
    _append_tar_member(artifact, root_extra, b"unlisted app-root file")

    with pytest.raises(verify.VerifyError, match="应用文件集合"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_archive_rejects_unexpected_empty_directory_at_macos_app_root(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_macos_root_empty_dir")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    root_extra = tarfile.TarInfo(f"{APP_ROOT}/root-empty")
    root_extra.type = tarfile.DIRTYPE
    root_extra.mode = 0o755
    _append_tar_member(artifact, root_extra)

    with pytest.raises(verify.VerifyError, match="应用.*集合"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


@pytest.mark.parametrize(
    "relative",
    [
        "python/bin/python",
        "python/bin/python3",
        "python/bin/python3.12",
    ],
)
def test_verify_release_rejects_wrong_runtime_architecture(tmp_path, relative):
    verify = _load(VERIFY_SCRIPT, "verify_release_runtime_arch")
    bundle, artifact_dir, _artifact = _fixture(tmp_path)
    runtime = bundle / relative
    runtime.write_bytes(_macho(0x01000007))
    _write_manifest(bundle)
    (artifact_dir / "release-manifest.json").write_bytes(
        (bundle / "release-manifest.json").read_bytes()
    )

    name = Path(relative).name
    with pytest.raises(verify.VerifyError, match=f"{name}.*架构"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_rejects_wrong_desktop_architecture_in_archive(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_desktop_arch")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    _replace_tar_member(
        artifact,
        f"{APP_ROOT}/Contents/MacOS/tianshu-desktop",
        _macho(0x01000007),
    )

    with pytest.raises(verify.VerifyError, match="tianshu-desktop.*架构"):
        verify.verify_release(
            artifact_dir=artifact_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )


def test_verify_release_requires_exact_ui_dist_index_identity(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_ui_identity")
    release_manifest = _load(RELEASE_SCRIPT, "release_manifest_ui_identity")
    bundle, _artifact_dir, _artifact = _fixture(tmp_path)
    copied = bundle / "ui" / "copied.html"
    copied.write_bytes((bundle / "ui" / "dist" / "index.html").read_bytes())
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    manifest["ui_dist_sha256"] = _sha(copied.read_bytes())
    manifest["files"]["ui/copied.html"] = _sha(copied.read_bytes())
    release_manifest.write_manifest(bundle / "release-manifest.json", manifest)
    rebuilt_dir, rebuilt = _build_artifact(tmp_path / "ui-rebuilt", bundle)

    with pytest.raises(verify.VerifyError, match="ui/dist/index.html"):
        verify.verify_release(
            artifact_dir=rebuilt_dir,
            bundle_dir=bundle,
            target=TARGET,
            version="0.1.0",
            commit=COMMIT,
            run_smoke=False,
        )
    assert rebuilt.is_file()


def test_runtime_smoke_rejects_non_native_target(tmp_path, monkeypatch):
    verify = _load(VERIFY_SCRIPT, "verify_release_non_native")
    monkeypatch.setattr(verify, "host_target", lambda: "macos-arm64")

    with pytest.raises(verify.VerifyError, match="原生 target"):
        verify._runtime_smoke(tmp_path, "macos-x64")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("python", "3.12.99", "Python 版本"),
        ("python_build_release", "19000101", "build release"),
    ],
)
def test_runtime_smoke_binds_manifest_to_trusted_runtime_lock(
    tmp_path,
    monkeypatch,
    field,
    value,
    message,
):
    verify = _load(VERIFY_SCRIPT, f"verify_release_runtime_lock_{field}")
    bundle = _make_bundle(tmp_path)
    _update_manifest(bundle, **{field: value})
    monkeypatch.setattr(verify, "host_target", lambda: TARGET)
    monkeypatch.setattr(
        verify,
        "_bundle_python",
        lambda _bundle, _target: Path(sys.executable),
    )

    with pytest.raises(verify.VerifyError, match=message):
        verify._runtime_smoke(bundle, TARGET)


def _runtime_fixture(
    tmp_path: Path,
    monkeypatch,
    verify,
    *,
    broken_entrypoint: str | None = None,
) -> tuple[Path, Path, SmokeTestIsolation]:
    assert NATIVE_TARGET is not None
    bundle = _make_bundle(tmp_path, target=NATIVE_TARGET)
    shutil.rmtree(bundle / "app" / "tianshu")
    shutil.copytree(
        ROOT / "src" / "tianshu",
        bundle / "app" / "tianshu",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    daemon_pid_file = tmp_path / "daemon-entered.pid"
    daemon_module = bundle / "app" / "tianshu" / "daemon.py"
    with daemon_module.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n\n_smoke_original_serve = serve\n"
            "def serve(*args, **kwargs):\n"
            "    from pathlib import Path\n"
            f"    Path({str(daemon_pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "    return _smoke_original_serve(*args, **kwargs)\n"
        )
    if broken_entrypoint == "daemon":
        with daemon_module.open("a", encoding="utf-8") as stream:
            stream.write(
                "\n\ndef serve(*args, **kwargs):\n"
                "    raise RuntimeError('daemon startup intentionally broken')\n"
            )
    elif broken_entrypoint == "host":
        host_module = bundle / "app" / "tianshu" / "host.py"
        with host_module.open("a", encoding="utf-8") as stream:
            stream.write(
                "\n\ndef run_host(*args, **kwargs):\n"
                "    raise RuntimeError('host startup intentionally broken')\n"
            )
    # This fixture copies the live package, unlike synthetic historical bundles.
    (bundle / "release.json").write_text(
        json.dumps(_release_identity(version=__version__, target=NATIVE_TARGET), indent=2) + "\n",
        encoding="utf-8",
    )
    _write_manifest(bundle)
    _update_manifest(
        bundle,
        python=platform.python_version(),
        python_build_release="test-runtime-release",
    )
    monkeypatch.setattr(verify, "host_target", lambda: NATIVE_TARGET)
    monkeypatch.setattr(
        verify,
        "_load_trusted_runtime_lock",
        lambda: {
            "python": platform.python_version(),
            "release": "test-runtime-release",
            "targets": {NATIVE_TARGET: {}},
        },
    )
    # uv Windows venv executables can be launcher processes. Keep the outer
    # test process PID bound to the same real interpreter as the inner smoke.
    runtime_python = release_smoke._runtime_python_executable()
    monkeypatch.setattr(
        verify,
        "_bundle_python",
        lambda _bundle, _target: Path(runtime_python),
    )
    # This source-copy fixture is not a native bundle acceptance probe. Guard
    # both verifier -> smoke and smoke -> real CLI launches with private roots.
    isolation = SmokeTestIsolation(
        tmp_path / "smoke-isolation", app_dir=bundle / "app", ui_dir=bundle / "ui" / "dist",
        release=json.loads((bundle / "release.json").read_text(encoding="utf-8")),
        python_version=platform.python_version(),
        runtime_python=runtime_python,
    )
    isolation.bind(monkeypatch)
    monkeypatch.setattr(verify, "subprocess", isolation.proxy(runtime=True))
    return bundle, daemon_pid_file, isolation


@pytest.mark.parametrize("launcher_drift", [False, True])
def test_smoke_test_runtime_fixture_selects_base_not_venv_launcher(
    tmp_path, monkeypatch, launcher_drift,
):
    runtime_python = release_smoke._runtime_python_executable()
    launcher = tmp_path / "venv-launcher"
    launcher.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "_base_executable", runtime_python)
    # This no-CLI probe is platform-independent; target metadata is not run.
    monkeypatch.setitem(globals(), "NATIVE_TARGET", TARGET)
    verify = _load(VERIFY_SCRIPT, "verify_release_runtime_launcher_guard")
    bundle, _, isolation = _runtime_fixture(tmp_path, monkeypatch, verify)
    assert verify._bundle_python(bundle, TARGET) == Path(runtime_python)
    assert isolation.runtime_python == runtime_python
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        raise RuntimeError("test stopped before any real process")

    isolation.native_popen = record
    if launcher_drift:
        monkeypatch.setattr(verify, "_bundle_python", lambda *_args: launcher)
    expected = "original verifier argv mismatch" if launcher_drift else "test stopped before"
    with pytest.raises(RuntimeError, match=expected):
        verify._runtime_smoke(bundle, TARGET)
    if launcher_drift:
        assert calls == []
    else:
        assert len(calls) == 1
        command, options = calls[0]
        assert command[:3] == [runtime_python, "-P", "-m"]
        assert command[0] != str(launcher)
        assert options["cwd"] == bundle
    assert not (isolation.root / "runtime.json").exists()


@pytest.mark.skipif(NATIVE_TARGET is None, reason=NATIVE_RUNTIME_REASON)
def test_verify_runtime_smoke_launches_real_host_daemon_and_checks_token(tmp_path, monkeypatch):
    from tianshu.leases import pid_alive

    verify = _load(VERIFY_SCRIPT, "verify_release_runtime_positive")
    bundle, daemon_pid_file, isolation = _runtime_fixture(tmp_path, monkeypatch, verify)

    smoke = verify._runtime_smoke(bundle, NATIVE_TARGET)
    isolation.assert_children(smoke, runtime=True)

    assert smoke["status"] == "pass"
    assert smoke["host_target"] == NATIVE_TARGET
    assert smoke["host_pid"] != smoke["daemon_pid"]
    assert "session_token_required" in smoke["checks"]
    daemon_pid = int(daemon_pid_file.read_text(encoding="ascii"))
    assert daemon_pid == smoke["daemon_pid"]
    assert not pid_alive(smoke["host_pid"])
    assert not pid_alive(smoke["daemon_pid"])


@pytest.mark.skipif(NATIVE_TARGET is None, reason=NATIVE_RUNTIME_REASON)
@pytest.mark.parametrize("entrypoint", ["host", "daemon"])
def test_self_consistent_crashing_entrypoint_still_fails_and_reaps_owned_children(
    tmp_path, monkeypatch, entrypoint
):
    from tianshu.leases import pid_alive

    verify = _load(VERIFY_SCRIPT, f"verify_release_runtime_crash_{entrypoint}")
    bundle, daemon_pid_file, isolation = _runtime_fixture(
        tmp_path, monkeypatch, verify, broken_entrypoint=entrypoint
    )

    with pytest.raises(verify.VerifyError, match="启动烟测失败") as caught:
        verify._runtime_smoke(bundle, NATIVE_TARGET)
    assert f"{entrypoint} startup intentionally broken" in str(caught.value)
    isolation.assert_receipt("runtime", isolation.runtime_pid)
    roles = ("daemon", "host") if entrypoint == "host" else ("daemon",)
    for role in roles:
        launched_pid = int((isolation.root / f"launched-{role}.pid").read_text(encoding="ascii"))
        isolation.assert_receipt(role, launched_pid)
        assert not pid_alive(launched_pid)
    assert not pid_alive(isolation.runtime_pid)

    if daemon_pid_file.exists():
        assert not pid_alive(int(daemon_pid_file.read_text(encoding="ascii")))


@pytest.mark.skipif(NATIVE_TARGET is None, reason=NATIVE_RUNTIME_REASON)
def test_verifier_runtime_ignores_caller_cwd_tianshu_shadow(tmp_path, monkeypatch):
    verify = _load(VERIFY_SCRIPT, "verify_release_runtime_cwd_shadow")
    bundle, _daemon_pid_file, isolation = _runtime_fixture(tmp_path, monkeypatch, verify)
    caller = tmp_path / "caller"
    shadow = caller / "tianshu"
    shadow.mkdir(parents=True)
    marker = caller / "shadow-executed"
    (shadow / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (shadow / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('shadow', encoding='ascii')\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(caller)

    result = verify._runtime_smoke(bundle, NATIVE_TARGET)
    isolation.assert_children(result, runtime=True)

    assert result["status"] == "pass"
    assert not marker.exists()


@pytest.mark.skipif(
    NATIVE_TARGET is None or os.name == "nt",
    reason="POSIX process-group SIGKILL fallback probe",
)
def test_runtime_smoke_timeout_reaps_sigterm_ignoring_grandchild(
    tmp_path,
    monkeypatch,
):
    from tianshu.leases import pid_alive

    verify = _load(VERIFY_SCRIPT, "verify_release_runtime_timeout_tree")
    bundle = _make_bundle(tmp_path, target=NATIVE_TARGET)
    package = bundle / "app" / "tianshu"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    grandchild_ready_file = tmp_path / "runtime-smoke-grandchild.ready"
    child_pid_file = tmp_path / "runtime-smoke-child.pid"
    (package / "release_smoke.py").write_text(
        f"""import os
import subprocess
import sys
import time
from pathlib import Path

ready = Path({str(grandchild_ready_file)!r})
ready.unlink(missing_ok=True)
code = '''import signal
import time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(grandchild_ready_file)!r}).write_text("ready", encoding="ascii")
time.sleep(120)
'''
child = subprocess.Popen([sys.executable, "-c", code])
deadline = time.monotonic() + 10
while not ready.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not ready.exists():
    raise RuntimeError("grandchild did not install SIGTERM handler")
with open({str(child_pid_file)!r}, "w", encoding="ascii") as stream:
    stream.write(str(child.pid))
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(120)
""",
        encoding="utf-8",
    )
    _write_manifest(bundle)
    _update_manifest(
        bundle,
        python=platform.python_version(),
        python_build_release="test-runtime-release",
    )
    monkeypatch.setattr(verify, "host_target", lambda: NATIVE_TARGET)
    monkeypatch.setattr(
        verify,
        "_load_trusted_runtime_lock",
        lambda: {
            "python": platform.python_version(),
            "release": "test-runtime-release",
            "targets": {NATIVE_TARGET: {}},
        },
    )
    monkeypatch.setattr(verify, "_bundle_python", lambda _bundle, _target: Path(sys.executable))
    monkeypatch.setattr(verify, "RUNTIME_SMOKE_TIMEOUT_S", 2.0)

    with pytest.raises(verify.VerifyError, match="超时.*回收进程树"):
        verify._runtime_smoke(bundle, NATIVE_TARGET)

    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5.0
    while pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pid_alive(child_pid)


def test_windows_bundle_rejects_wrong_pe_architecture(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_arch")
    bundle = _windows_bundle(tmp_path)
    verify._verify_windows_bundle(bundle, "windows-x64")
    (bundle / "python" / "python.exe").write_bytes(_pe(0xAA64))
    _write_manifest(bundle)

    with pytest.raises(verify.VerifyError, match="PE 架构"):
        verify._verify_windows_bundle(bundle, "windows-x64")


def test_synthetic_pe_without_nsis_payload_is_rejected_before_extraction(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_synthetic")
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C))

    def extractor(_artifact: Path, _destination: Path) -> None:
        raise AssertionError("没有 NSIS 标识时不得调用解包器")

    with pytest.raises(verify.VerifyError, match="NSIS"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=_windows_bundle(tmp_path),
            target="windows-x64",
            extractor=extractor,
        )


@pytest.mark.parametrize("target", ["windows-x64", "windows-arm64"])
def test_windows_nsis_accepts_x86_bootstrap_with_exact_payload(tmp_path, target):
    verify = _load(VERIFY_SCRIPT, f"verify_release_windows_bootstrap_{target}")
    bundle = _windows_bundle(tmp_path, target=target)
    installer = tmp_path / f"Tianshu-Agent-0.1.0-{target}-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination / "$PLUGINSDIR" / "app")
        _add_nsis_support_files(destination, target=target)

    verify._verify_windows_artifact(
        artifact=installer,
        bundle_dir=bundle,
        target=target,
        extractor=extractor,
    )


@pytest.mark.parametrize("machine", [0x0200, 0x8664, 0xAA64])
def test_windows_nsis_rejects_non_tauri_outer_bootstrap_architecture(tmp_path, machine):
    verify = _load(
        VERIFY_SCRIPT,
        f"verify_release_windows_outer_non_tauri_{machine:04x}",
    )
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(machine) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, _destination: Path) -> None:
        raise AssertionError("非 Tauri NSIS 外层 PE 必须在解包前被拒绝")

    with pytest.raises(verify.VerifyError, match="x86 引导器"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_x86_bootstrap_does_not_replace_payload_architecture_check(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_outer_payload_negative")
    bundle = _windows_bundle(tmp_path, target="windows-x64")
    (bundle / "python" / "python.exe").write_bytes(_pe(0xAA64))
    _write_manifest(bundle)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination / "$PLUGINSDIR" / "app")
        _add_nsis_support_files(destination, target="windows-x64")

    with pytest.raises(verify.VerifyError, match="python.exe PE 架构"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_nsis_payload_must_have_unique_exact_resource_root(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_payload")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        extra = destination / "nested"
        extra.mkdir()
        (extra / "release.json").write_bytes((bundle / "release.json").read_bytes())

    with pytest.raises(verify.VerifyError, match="唯一 release.json"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_nsis_payload_rejects_marker_without_resources(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_empty_payload")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        (destination / "$PLUGINSDIR").mkdir()
        (destination / "$PLUGINSDIR" / "plugin.dll").write_bytes(b"not a payload")

    with pytest.raises(verify.VerifyError, match="唯一 release.json"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_nsis_payload_accepts_exact_nested_resource_root(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_payload_positive")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        payload = destination / "$PLUGINSDIR" / "app"
        shutil.copytree(bundle, payload)
        _add_nsis_support_files(destination, target="windows-x64")

    verify._verify_windows_artifact(
        artifact=installer,
        bundle_dir=bundle,
        target="windows-x64",
        extractor=extractor,
    )


def test_windows_nsis_accepts_root_payload_with_fixed_installer_files(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_root_payload_positive")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        _add_nsis_support_files(destination, target="windows-x64")

    verify._verify_windows_artifact(
        artifact=installer,
        bundle_dir=bundle,
        target="windows-x64",
        extractor=extractor,
    )


def test_windows_nsis_rejects_unexpected_file_outside_payload_root(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_outer_extra")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        payload = destination / "$PLUGINSDIR" / "app"
        shutil.copytree(bundle, payload)
        _add_nsis_support_files(destination, target="windows-x64")
        (destination / "$PLUGINSDIR" / "unexpected.dll").write_bytes(_pe(0x014C))

    with pytest.raises(verify.VerifyError, match="固定布局.*unexpected"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_nsis_rejects_missing_fixed_installer_file(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_outer_missing")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        _add_nsis_support_files(destination, target="windows-x64")
        (destination / "uninstall.exe").unlink()

    with pytest.raises(verify.VerifyError, match="固定布局.*uninstall.exe"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_nsis_rejects_wrong_desktop_executable_architecture(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_desktop_arch")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        _add_nsis_support_files(destination, target="windows-x64")
        (destination / "tianshu-desktop.exe").write_bytes(_pe(0xAA64))

    with pytest.raises(verify.VerifyError, match="tianshu-desktop.exe PE 架构"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


def test_windows_nsis_extracted_tree_rejects_casefold_duplicates(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_casefold")

    with pytest.raises(verify.VerifyError, match="大小写重复"):
        verify._validate_casefold_paths(
            ["python/bin/tool", "python/bin/Tool"],
            "NSIS 解包结果",
        )


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Windows", "AMD64", "windows-x64"),
        ("Windows", "aarch64", "windows-arm64"),
        ("Linux", "x86_64", None),
        ("Windows", "i386", None),
    ],
)
def test_host_target_accepts_only_declared_platform_architectures(
    monkeypatch,
    system,
    machine,
    expected,
):
    verify = _load(
        VERIFY_SCRIPT,
        f"verify_release_host_target_{system}_{machine}".lower(),
    )
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)

    assert verify.host_target() == expected


def test_windows_nsis_payload_rejects_root_sitecustomize(tmp_path):
    verify = _load(VERIFY_SCRIPT, "verify_release_windows_sitecustomize")
    bundle = _windows_bundle(tmp_path)
    installer = tmp_path / "Tianshu-Agent-0.1.0-windows-x64-setup.exe"
    installer.write_bytes(_pe(0x014C) + b"\0NullsoftInst\0")

    def extractor(_artifact: Path, destination: Path) -> None:
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        (destination / "sitecustomize.py").write_text(
            "raise SystemExit(91)\n", encoding="utf-8"
        )

    with pytest.raises(verify.VerifyError, match="未列文件"):
        verify._verify_windows_artifact(
            artifact=installer,
            bundle_dir=bundle,
            target="windows-x64",
            extractor=extractor,
        )


@pytest.mark.parametrize("envelope", [False, True])
def test_static_macos_verifier_allows_only_optional_exact_envelope(tmp_path, envelope):
    verify = _load(VERIFY_SCRIPT, "verify_optional_envelope")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    if envelope:
        directory = tarfile.TarInfo(f"{APP_ROOT}/Contents/_CodeSignature")
        directory.type = tarfile.DIRTYPE
        _append_tar_member(artifact, directory)
        _append_tar_member(
            artifact,
            tarfile.TarInfo(f"{APP_ROOT}/Contents/_CodeSignature/CodeResources"),
            b"opaque nonempty resource envelope; static check is not Apple validation",
        )
    result = verify.verify_release(
        artifact_dir=artifact_dir, bundle_dir=bundle, target=TARGET,
        version="0.1.0", commit=COMMIT, run_smoke=False,
    )
    assert result["runtime_smoke"] == {"status": "not_run_by_caller"}
    assert set(result) == {
        "target", "version", "commit", "artifact", "sha256", "size",
        "manifest_sha256", "runtime_smoke",
    }


@pytest.mark.parametrize("defect", [
    "empty", "missing-file", "missing-dir", "dir-as-file", "file-as-dir",
    "extra", "extra-dir", "escape", "symlink", "hardlink", "special", "casefold",
])
def test_static_macos_verifier_rejects_malformed_envelope(tmp_path, defect):
    verify = _load(VERIFY_SCRIPT, "verify_malformed_envelope")
    bundle, artifact_dir, artifact = _fixture(tmp_path)
    signature = f"{APP_ROOT}/Contents/_CodeSignature"
    if defect != "missing-dir":
        directory = tarfile.TarInfo(signature)
        directory.type = tarfile.REGTYPE if defect == "dir-as-file" else tarfile.DIRTYPE
        _append_tar_member(artifact, directory, b"bad" if defect == "dir-as-file" else None)
    if defect != "missing-file":
        member = tarfile.TarInfo(f"{signature}/CodeResources")
        data = b"envelope"
        if defect in {"symlink", "hardlink"}:
            member.type = tarfile.SYMTYPE if defect == "symlink" else tarfile.LNKTYPE
            member.linkname = (
                "../Info.plist" if defect == "symlink"
                else f"{APP_ROOT}/Contents/Info.plist"
            )
            data = None
        elif defect in {"special", "file-as-dir"}:
            member.type = tarfile.FIFOTYPE if defect == "special" else tarfile.DIRTYPE
            data = None
        elif defect == "empty":
            data = b""
        _append_tar_member(artifact, member, data)
    if defect in {"extra", "casefold", "escape"}:
        name = {
            "extra": "extra", "casefold": "coderesources", "escape": "../escape",
        }[defect]
        _append_tar_member(artifact, tarfile.TarInfo(f"{signature}/{name}"), b"extra")
    if defect == "extra-dir":
        extra_directory = tarfile.TarInfo(f"{signature}/extra")
        extra_directory.type = tarfile.DIRTYPE
        _append_tar_member(artifact, extra_directory)
    with pytest.raises(verify.VerifyError):
        verify.verify_release(
            artifact_dir=artifact_dir, bundle_dir=bundle, target=TARGET,
            version="0.1.0", commit=COMMIT, run_smoke=False,
        )
