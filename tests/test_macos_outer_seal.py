from __future__ import annotations

import importlib.util
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "package_app.py"


def _module():
    spec = importlib.util.spec_from_file_location("outer_seal_package", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(tmp_path):
    app = tmp_path / "Tianshu Agent.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/MacOS/tianshu-desktop").write_bytes(b"synthetic unsigned or linker input")
    (app / "Contents/Info.plist").write_bytes(b"unchanged info")
    (app / "Contents/Resources/data").write_bytes(b"unchanged resource")
    return app


def _fake_sign(app):
    signature = app / "Contents/_CodeSignature"
    signature.mkdir(mode=0o700)
    (signature / "CodeResources").write_bytes(b"synthetic envelope, not Apple validation")
    main = app / "Contents/MacOS/tianshu-desktop"
    main.write_bytes(main.read_bytes() + b" sealed")


@pytest.mark.parametrize("input_bytes", [b"synthetic unsigned x64", b"synthetic linker arm64"])
def test_outer_seal_exact_no_force_command_and_native_verify(tmp_path, monkeypatch, input_bytes):
    module = _module()
    app = _app(tmp_path)
    (app / "Contents/MacOS/tianshu-desktop").write_bytes(input_bytes)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs == {"check": False, "timeout": 300}
        if "--sign" in command:
            _fake_sign(app)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    module._seal_macos_app(app)
    assert commands == [
        ["/usr/bin/codesign", "--sign", "-", "--timestamp=none", str(app)],
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app)],
    ]
    assert (app / "Contents/Resources/data").read_bytes() == b"unchanged resource"
    assert (app / "Contents/Info.plist").read_bytes() == b"unchanged info"


@pytest.mark.parametrize("failure", ["sign", "verify", "missing", "timeout"])
def test_outer_seal_failure_stops_without_retry(tmp_path, monkeypatch, failure):
    module = _module()
    app = _app(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if failure == "missing":
            raise FileNotFoundError("codesign missing")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 300)
        if "--sign" in command and failure != "sign":
            _fake_sign(app)
        code = 1 if ("--sign" in command) == (failure == "sign") else 0
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.PackageError, match="codesign"):
        module._seal_macos_app(app)
    assert len(calls) == (2 if failure == "verify" else 1)
    assert all("--force" not in c and "-f" not in c for c in calls)


@pytest.mark.parametrize(
    "mutation",
    [
        "resource",
        "info",
        "extra",
        "missing-envelope",
        pytest.param(
            "mode", marks=pytest.mark.skipif(os.name != "posix", reason="POSIX executable mode")
        ),
    ],
)
def test_outer_seal_rejects_unexpected_native_mutations(tmp_path, monkeypatch, mutation):
    module = _module()
    app = _app(tmp_path)

    def run(command, **kwargs):
        if "--sign" in command:
            _fake_sign(app)
            if mutation == "resource":
                (app / "Contents/Resources/data").write_bytes(b"changed")
            elif mutation == "info":
                (app / "Contents/Info.plist").write_bytes(b"changed")
            elif mutation == "mode":
                main = app / "Contents/MacOS/tianshu-desktop"
                main.chmod(stat.S_IMODE(main.stat().st_mode) ^ 0o100)
            elif mutation == "extra":
                (app / "Contents/_CodeSignature/extra").write_bytes(b"bad")
            else:
                (app / "Contents/_CodeSignature/CodeResources").unlink()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.PackageError):
        module._seal_macos_app(app)


@pytest.mark.parametrize("lowercase", [False, True])
def test_outer_seal_refuses_existing_envelope_before_native_command(
    tmp_path, monkeypatch, lowercase
):
    module = _module()
    app = _app(tmp_path)
    _fake_sign(app)
    if lowercase:
        directory = app / "Contents/_CodeSignature"
        directory.rename(app / "Contents/_codesignature")
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(module.PackageError, match="已有"):
        module._seal_macos_app(app)
    assert calls == []


@pytest.mark.skipif(os.name != "posix", reason="macOS fd-relative roundtrip uses POSIX primitives")
def test_archive_roundtrip_compares_bytes_modes_and_verifies_restored_app(tmp_path, monkeypatch):
    module = _module()
    app = _app(tmp_path)
    _fake_sign(app)
    artifact = tmp_path / "structural.tar.gz"
    module._write_macos_archive(app, artifact)
    calls = []

    def verify(restored):
        assert restored != app
        assert (restored / "Contents/Resources/data").read_bytes() == b"unchanged resource"
        assert (restored / "Contents/_CodeSignature/CodeResources").read_bytes()
        assert stat.S_IMODE(restored.stat().st_mode) == 0o700
        calls.append(restored)

    monkeypatch.setattr(module, "_verify_macos_app", verify)
    module._verify_macos_roundtrip(app, artifact)
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "posix", reason="macOS fd-relative roundtrip uses POSIX primitives")
@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "mode",
        "duplicate",
        "casefold",
        "symlink",
        "hardlink",
        "special",
        "escape",
        "extra",
        "missing",
    ],
)
def test_archive_roundtrip_rejects_drift_before_native_verify(tmp_path, monkeypatch, mutation):
    module = _module()
    app = _app(tmp_path)
    _fake_sign(app)
    original = tmp_path / "original.tar.gz"
    artifact = tmp_path / "mutated.tar.gz"
    module._write_macos_archive(app, original)
    target = "Tianshu Agent.app/Contents/Resources/data"
    with tarfile.open(original, "r:gz") as source, tarfile.open(artifact, "w:gz") as dest:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isfile() else None
            if member.name == target:
                if mutation == "missing":
                    continue
                if mutation == "hash":
                    stream = io.BytesIO(b"x" * member.size)
                elif mutation == "mode":
                    member.mode ^= 0o100
                elif mutation in {"symlink", "hardlink"}:
                    member.type = tarfile.SYMTYPE if mutation == "symlink" else tarfile.LNKTYPE
                    member.linkname = (
                        "../Info.plist"
                        if mutation == "symlink"
                        else "Tianshu Agent.app/Contents/Info.plist"
                    )
                    member.size = 0
                    stream = None
                elif mutation == "special":
                    member.type = tarfile.FIFOTYPE
                    member.size = 0
                    stream = None
                elif mutation == "escape":
                    member.name = "Tianshu Agent.app/../escape"
                if mutation == "duplicate":
                    dest.addfile(member, io.BytesIO(b"unchanged resource"))
            dest.addfile(member, stream)
        if mutation in {"extra", "casefold"}:
            extra = tarfile.TarInfo(
                target.upper() if mutation == "casefold" else "Tianshu Agent.app/Contents/extra"
            )
            extra.size = 1
            dest.addfile(extra, io.BytesIO(b"x"))
    calls = []
    monkeypatch.setattr(module, "_verify_macos_app", lambda path: calls.append(path))
    with pytest.raises(module.PackageError):
        module._verify_macos_roundtrip(app, artifact)
    assert calls == []
    assert artifact.exists()  # failure evidence is not silently removed


def test_direct_release_checks_input_and_roundtrip_without_signing(tmp_path, monkeypatch):
    module = _module()
    app = _app(tmp_path)
    _fake_sign(app)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_bytes(b"{}")
    calls = []
    monkeypatch.setattr(module, "_verify_macos_app", lambda path: calls.append(("verify", path)))
    monkeypatch.setattr(
        module,
        "_verify_macos_roundtrip",
        lambda source, tar, expected=None: calls.append(("roundtrip", source)),
    )
    monkeypatch.setattr(
        module, "_seal_macos_app", lambda path: pytest.fail("direct input must not be re-signed")
    )
    result = module.release_artifact(
        bundled=app,
        bundle_dir=bundle,
        out_dir=tmp_path / "out",
        version="0.1.0",
        target="macos-arm64",
    )
    assert calls == [("verify", app), ("roundtrip", app)]
    assert result.is_file()


@pytest.mark.parametrize("stage", ["input", "roundtrip"])
def test_direct_release_verification_failure_does_not_publish_checksum(
    tmp_path, monkeypatch, stage
):
    module = _module()
    app = _app(tmp_path)
    _fake_sign(app)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_bytes(b"{}")

    def fail(*args):
        raise module.PackageError("codesign failure")

    monkeypatch.setattr(module, "_verify_macos_app", fail if stage == "input" else lambda p: None)
    monkeypatch.setattr(module, "_verify_macos_roundtrip", fail)
    with pytest.raises(module.PackageError):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle,
            out_dir=tmp_path / "out",
            version="0.1.0",
            target="macos-arm64",
        )
    assert not list((tmp_path / "out").glob("*.sha256"))
    assert not list((tmp_path / "out").glob("*.app.tar.gz"))


def test_windows_release_never_invokes_macos_tools(tmp_path, monkeypatch):
    module = _module()
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"synthetic NSIS")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: pytest.fail("no macOS tool on Windows path")
    )
    result = module.release_artifact(
        bundled=installer,
        bundle_dir=bundle,
        out_dir=tmp_path / "out",
        version="0.1.0",
        target="windows-x64",
    )
    assert result.read_bytes() == installer.read_bytes()


@pytest.mark.parametrize("publish", [False, True])
def test_package_seals_final_tauri_output_once_even_without_archive(tmp_path, monkeypatch, publish):
    module = _module()
    bundle = tmp_path / "bundle"
    for name in ["python", "app/tianshu", "licenses", "ui/dist"]:
        (bundle / name).mkdir(parents=True)
    (bundle / "licenses/LICENSE").write_bytes(b"license")
    (bundle / "ui/dist/index.html").write_bytes(b"ui")
    (bundle / "release.json").write_bytes(b"{}")
    (bundle / "release-manifest.json").write_bytes(b'{"version":"0.1.0"}')
    (tmp_path / "desktop").mkdir()
    app = _app(tmp_path)
    monkeypatch.setattr(module, "native_target", lambda: "macos-arm64")
    monkeypatch.setattr(module, "resolve_pnpm", lambda: "/tools/pnpm")
    monkeypatch.setattr(module, "bundle_path", lambda *a, **k: app)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--sign" in command:
            _fake_sign(app)
        return subprocess.CompletedProcess(command, 0)

    def release(**kwargs):
        assert (app / "Contents/_CodeSignature/CodeResources").is_file()
        calls.append(["release"])
        return tmp_path / "result.tar.gz"

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "release_artifact", release)
    result = module.package_app(
        repo_root=tmp_path,
        target="macos-arm64",
        bundle_dir=bundle,
        out_dir=tmp_path / "out" if publish else None,
    )
    assert result == (tmp_path / "result.tar.gz" if publish else app)
    assert len([c for c in calls if "--sign" in c]) == 1
    assert len([c for c in calls if "--verify" in c]) == 1
    assert (calls[-1] == ["release"]) == publish


def test_direct_release_rejects_unsealed_input_without_native_command(tmp_path, monkeypatch):
    module = _module()
    app = _app(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("no envelope"))
    with pytest.raises(module.PackageError, match="封套"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle,
            out_dir=tmp_path / "out",
            version="0.1.0",
            target="macos-arm64",
        )
    assert not list((tmp_path / "out").glob("*.sha256"))


def test_release_output_inside_app_is_rejected_before_writing(tmp_path, monkeypatch):
    module = _module()
    app = _app(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("unsafe output"))
    with pytest.raises(module.PackageError, match="App 内"):
        module.release_artifact(
            bundled=app,
            bundle_dir=bundle,
            out_dir=app / "output",
            version="0.1.0",
            target="macos-arm64",
        )
    assert not (app / "output").exists()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="real Apple compiler/codesign fixture; never execute fixture"
)
@pytest.mark.parametrize("signature", ["unsigned", "linker", "ordinary"])
def test_native_outer_seal_and_archive_roundtrip(tmp_path, monkeypatch, signature):
    """Actual host-native structural proof, not product/Gatekeeper/runtime proof."""
    module = _module()
    artifact_root = SCRIPT.parents[1] / ".omc" / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix=f"outer-seal-native-{signature}-", dir=artifact_root))
    calls = []
    real_run = subprocess.run
    (evidence / "source-identity.json").write_text(
        json.dumps(
            {
                "package_script_sha256": module._sha256(SCRIPT),
                "test_script_sha256": module._sha256(Path(__file__)),
                "python": sys.version,
                "platform": sys.platform,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def recorded_run(command, **kwargs):
        sequence = len(calls)
        record = {"argv": [str(arg) for arg in command], "kwargs": kwargs, "exit": None}
        calls.append(record)
        (evidence / "commands.json").write_text(
            json.dumps(calls, indent=2) + "\n", encoding="utf-8"
        )
        stdout = evidence / f"{sequence:02d}.stdout"
        stderr = evidence / f"{sequence:02d}.stderr"
        try:
            with stdout.open("wb") as out, stderr.open("wb") as err:
                result = real_run(command, stdout=out, stderr=err, **kwargs)
            record["exit"] = result.returncode
            record["stdout_sha256"] = module._sha256(stdout)
            record["stderr_sha256"] = module._sha256(stderr)
            return result
        except BaseException as exc:
            record["error"] = repr(exc)
            raise
        finally:
            (evidence / "commands.json").write_text(
                json.dumps(calls, indent=2) + "\n", encoding="utf-8"
            )

    monkeypatch.setattr(module.subprocess, "run", recorded_run)
    assert recorded_run(["/usr/bin/clang", "--version"], check=False, timeout=300).returncode == 0
    source = tmp_path / "fixture.c"
    source.write_text("int main(void) { return 0; }\n", encoding="ascii")
    standalone = tmp_path / "fixture-main"
    linker_flag = "-Wl,-no_adhoc_codesign" if signature == "unsigned" else "-Wl,-adhoc_codesign"
    compile_result = recorded_run(
        ["/usr/bin/clang", str(source), linker_flag, "-o", str(standalone)],
        check=False,
        timeout=300,
    )
    assert compile_result.returncode == 0, (
        f"compiler unsupported/refused; no alternate route: {evidence}"
    )
    if signature == "ordinary":
        # Sign truly standalone code before the App exists: signing a binary
        # already inside an App can cause codesign to seal its enclosing bundle.
        module._run_codesign(["--sign", "-", "--timestamp=none"], standalone)
    app = tmp_path / "Tianshu Agent.app"
    binary = app / "Contents/MacOS/tianshu-desktop"
    binary.parent.mkdir(parents=True)
    standalone.rename(binary)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/data").write_bytes(b"fixture resource, not product payload\n")
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "invalid.tianshu.structural-fixture",
                "CFBundleExecutable": "tianshu-desktop",
                "CFBundleName": "Structural Fixture",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
            }
        )
    )
    assert not (app / "Contents/_CodeSignature").exists()
    display = recorded_run(
        ["/usr/bin/codesign", "--display", "--verbose=4", str(binary)], check=False, timeout=300
    )
    diagnostic = (evidence / f"{len(calls) - 1:02d}.stderr").read_text(encoding="utf-8")
    if signature == "unsigned":
        assert display.returncode != 0 and "not signed at all" in diagnostic
    else:
        assert display.returncode == 0 and "Signature=adhoc" in diagnostic
        assert ("linker-signed" in diagnostic) == (signature == "linker")
    before = module._macos_app_state(app)
    (evidence / "before.json").write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    if signature == "ordinary":
        with pytest.raises(module.PackageError, match="codesign 失败"):
            module._seal_macos_app(app)
        assert module._macos_app_state(app) == before
        assert calls[-1]["exit"] != 0
    else:
        module._seal_macos_app(app)
        archive = tmp_path / "structural-fixture-not-release.tar.gz"
        module._write_macos_archive(app, archive)
        module._verify_macos_roundtrip(app, archive)
        (evidence / "archive.json").write_text(
            json.dumps(
                {
                    "path": str(archive),
                    "sha256": module._sha256(archive),
                    "size": archive.stat().st_size,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    after = module._macos_app_state(app)
    (evidence / "after.json").write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
    assert all("--force" not in c["argv"] and "-f" not in c["argv"] for c in calls)
    assert all("--deep" not in c["argv"] for c in calls if "--sign" in c["argv"])
    print(f"native structural fixture receipts: {evidence}")
