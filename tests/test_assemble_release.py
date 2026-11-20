from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assemble_release.py"
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"
BUILD_RUN_URL = "https://github.com/example/tianshu-agent/actions/runs/1"
PREVIEW_TARGET = "macos-arm64"
PREVIEW_STEM = f"Tianshu-Agent-0.1.0-{PREVIEW_TARGET}"
PREVIEW_ARTIFACT = f"{PREVIEW_STEM}.app.tar.gz"
PREVIEW_CHECKSUM = f"{PREVIEW_STEM}.sha256"
MANIFEST = "release-manifest.json"
REPORT = "release-verification.json"
INDEX = "release-index.json"


def _load():
    spec = importlib.util.spec_from_file_location("assemble_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifacts(root: Path, targets: tuple[str, ...] | None = None) -> None:
    for target in _load().TARGETS if targets is None else targets:
        target_dir = root / target
        target_dir.mkdir(parents=True)
        suffix = ".app.tar.gz" if target.startswith("macos") else "-setup.exe"
        stem = f"Tianshu-Agent-0.1.0-{target}"
        artifact = target_dir / f"{stem}{suffix}"
        artifact.write_bytes(target.encode())
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (target_dir / f"{stem}.sha256").write_text(
            f"{checksum}  {artifact.name}\n",
            encoding="ascii",
        )
        manifest = {
            "schema": 1,
            "product": "tianshu-agent",
            "version": "0.1.0",
            "commit": COMMIT,
            "target": target,
            "python": "3.12.16",
            "python_build_release": "20261110",
            "ui_dist_sha256": "1" * 64,
            "files": {"ui/dist/index.html": "1" * 64},
        }
        manifest_path = target_dir / "release-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (target_dir / "release-verification.json").write_text(
            json.dumps(
                {
                    "target": target,
                    "version": "0.1.0",
                    "commit": COMMIT,
                    "sha256": checksum,
                    "artifact": artifact.name,
                    "manifest_sha256": hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest(),
                    "runtime_smoke": {
                        "status": "pass",
                        "host_target": target,
                        "version": "0.1.0",
                    },
                }
            ),
            encoding="utf-8",
        )


def test_assemble_release_indexes_exact_four_targets(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)

    path = module.assemble_release(
        artifacts_root=artifacts,
        out_dir=tmp_path / "release",
        version="0.1.0",
        commit=COMMIT,
        build_run_url=BUILD_RUN_URL,
    )

    index = json.loads(path.read_text(encoding="utf-8"))
    assert index["signed"] is False
    assert [item["target"] for item in index["artifacts"]] == list(module.TARGETS)
    assert all(item["device_verification"] == "unknown" for item in index["artifacts"])
    assert (path.parent / "install-macos.sh").is_file()
    for item in index["artifacts"]:
        assert item["sha256"] == hashlib.sha256(
            (path.parent / item["artifact"]).read_bytes()
        ).hexdigest()
    assert sorted(
        path.name for path in (ROOT / "scripts").glob("*macos.sh")
    ) == ["install-macos.sh", "uninstall-macos.sh"]


def test_assemble_release_index_binds_every_final_asset(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)

    path = module.assemble_release(
        artifacts_root=artifacts,
        out_dir=tmp_path / "release",
        version="0.1.0",
        commit=COMMIT,
        build_run_url=BUILD_RUN_URL,
    )

    index = json.loads(path.read_text(encoding="utf-8"))
    records = {record["name"]: record for record in index["assets"]}
    final_names = {
        item.name for item in path.parent.iterdir() if item.name != path.name
    }
    assert set(records) == final_names
    assert len(records) == 14
    for name, record in records.items():
        asset = path.parent / name
        assert record["sha256"] == hashlib.sha256(asset.read_bytes()).hexdigest()
        assert record["size"] == asset.stat().st_size
        assert record["kind"] in {
            "artifact",
            "checksum",
            "manifest",
            "helper-script",
        }


def test_assemble_release_preserves_executable_script_mode(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)

    path = module.assemble_release(
        artifacts_root=artifacts,
        out_dir=tmp_path / "release",
        version="0.1.0",
        commit=COMMIT,
        build_run_url=BUILD_RUN_URL,
    )

    for script in module.HELPER_SCRIPTS:
        copied = path.parent / script
        assert copied.read_bytes() == (ROOT / "scripts" / script).read_bytes()
        if os.name == "posix":
            assert stat.S_IMODE(copied.stat().st_mode) == 0o755


def test_assemble_release_index_binds_helper_script_bytes(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)

    path = module.assemble_release(
        artifacts_root=artifacts,
        out_dir=tmp_path / "release",
        version="0.1.0",
        commit=COMMIT,
        build_run_url=BUILD_RUN_URL,
    )

    index = json.loads(path.read_text(encoding="utf-8"))
    by_name = {entry["name"]: entry for entry in index["helper_scripts"]}
    for script in module.HELPER_SCRIPTS:
        copied = path.parent / script
        assert by_name[script] == {
            "kind": "helper-script",
            "name": script,
            "sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
            "size": copied.stat().st_size,
        }


def test_assemble_release_rejects_tampered_checksum(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    checksum = artifacts / "macos-arm64" / "Tianshu-Agent-0.1.0-macos-arm64.sha256"
    checksum.write_text(f"{'0' * 64}  artifact\n", encoding="ascii")

    with pytest.raises(module.AssembleError, match="SHA256"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


def test_assemble_release_rejects_partial_or_unbound_manifest(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    manifest = artifacts / "macos-arm64" / "release-manifest.json"
    manifest.write_text(
        json.dumps({"target": "macos-arm64"}),
        encoding="utf-8",
    )

    with pytest.raises(module.AssembleError, match="发布清单"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


def test_assemble_release_rejects_report_not_bound_to_manifest_bytes(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    report = artifacts / "macos-arm64" / "release-verification.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    data["manifest_sha256"] = "0" * 64
    report.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(module.AssembleError, match="验证报告"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


def test_assemble_release_rejects_cross_target_runtime_smoke(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    report = artifacts / "macos-x64" / "release-verification.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    data["runtime_smoke"] = {
        "status": "not_run_cross_target",
        "host_target": "macos-arm64",
    }
    report.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(module.AssembleError, match="runtime smoke"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


def test_assemble_release_rejects_malformed_verification_json(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    report = artifacts / "windows-x64" / "release-verification.json"
    report.write_text("{", encoding="utf-8")

    with pytest.raises(module.AssembleError, match="验证报告"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


def test_assemble_release_rejects_non_https_build_run_url(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)

    with pytest.raises(module.AssembleError, match="build_run_url"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url="file:///tmp/run",
        )


def test_assemble_release_rejects_invalid_version_before_creating_output(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="version"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=out_dir,
            version="../../escaped",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )

    assert not out_dir.exists()


def test_assemble_release_rejects_invalid_commit_before_creating_output(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="commit"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=out_dir,
            version="0.1.0",
            commit="MAIN",
            build_run_url=BUILD_RUN_URL,
        )

    assert not out_dir.exists()


@pytest.mark.parametrize(
    "build_run_url",
    (
        "https:///owner/repo/actions/runs/1",
        "https://github.com/owner/repo/not-a-run",
        "https://github.com/owner/repo/actions/runs/1?token=secret",
    ),
)
def test_assemble_release_requires_exact_https_actions_run_url(
    tmp_path, build_run_url
):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)

    with pytest.raises(module.AssembleError, match="build_run_url"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=build_run_url,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_assemble_release_rejects_symlinked_artifacts_root(tmp_path):
    module = _load()
    real_artifacts = tmp_path / "real-artifacts"
    _artifacts(real_artifacts)
    artifacts = tmp_path / "artifacts"
    artifacts.symlink_to(real_artifacts, target_is_directory=True)

    with pytest.raises(module.AssembleError, match="artifacts_root"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_assemble_release_rejects_symlinked_artifacts_ancestor(tmp_path):
    module = _load()
    real_parent = tmp_path / "real-parent"
    real_artifacts = real_parent / "artifacts"
    _artifacts(real_artifacts)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="artifacts_root.*链接"):
        module.assemble_release(
            artifacts_root=alias / "artifacts",
            out_dir=out_dir,
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )

    assert not out_dir.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_assemble_release_rejects_symlinked_target_dir(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    target_dir = artifacts / "macos-arm64"
    real_target_dir = tmp_path / "real-macos-arm64"
    target_dir.rename(real_target_dir)
    target_dir.symlink_to(real_target_dir, target_is_directory=True)

    with pytest.raises(module.AssembleError, match="macos-arm64"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_assemble_release_rejects_symlinked_required_input(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    artifact = (
        artifacts
        / "macos-arm64"
        / "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    )
    external = tmp_path / "external-artifact"
    external.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(external)

    with pytest.raises(module.AssembleError, match="符号链接"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO 负控")
def test_assemble_release_rejects_special_required_input(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    artifact = (
        artifacts
        / "macos-arm64"
        / "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    )
    artifact.unlink()
    os.mkfifo(artifact)

    with pytest.raises(module.AssembleError, match="普通文件"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=tmp_path / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_assemble_release_rejects_symlinked_out_dir(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    external = tmp_path / "external-release"
    external.mkdir()
    out_dir = tmp_path / "release"
    out_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(module.AssembleError, match="out_dir"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=out_dir,
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )

    assert not any(external.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
def test_assemble_release_rejects_symlinked_out_ancestor(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    external = tmp_path / "external-release"
    external.mkdir()
    alias = tmp_path / "release-alias"
    alias.symlink_to(external, target_is_directory=True)

    with pytest.raises(module.AssembleError, match="out_dir.*链接"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=alias / "release",
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )

    assert not (external / "release").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 硬链接负控")
def test_assemble_release_rejects_hardlinked_output_file(tmp_path):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    out_dir = tmp_path / "release"
    out_dir.mkdir()
    external = tmp_path / "outside-sentinel"
    external.write_bytes(b"sentinel")
    destination = out_dir / "Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz"
    os.link(external, destination)

    with pytest.raises(module.AssembleError, match="硬链接"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=out_dir,
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
        )

    assert external.read_bytes() == b"sentinel"
    assert destination.read_bytes() == b"sentinel"
    assert set(out_dir.iterdir()) == {destination}


@pytest.fixture(params=(False, True), ids=("full", "preview"))
def release_mode(request):
    return request.param


def _assemble(module, artifacts: Path, out_dir: Path, *, preview: bool, **kwargs):
    return module.assemble_release(
        artifacts_root=artifacts,
        out_dir=out_dir,
        version="0.1.0",
        commit=COMMIT,
        build_run_url=BUILD_RUN_URL,
        macos_arm64_preview=preview,
        **kwargs,
    )


def _mode_artifacts(root: Path, preview: bool) -> None:
    _artifacts(root, (PREVIEW_TARGET,) if preview else None)


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _cli(artifacts: Path, out_dir: Path, *options: str):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifacts-root",
            str(artifacts),
            "--out-dir",
            str(out_dir),
            "--version",
            "0.1.0",
            "--commit",
            COMMIT,
            "--build-run-url",
            BUILD_RUN_URL,
            *options,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("existing_empty_output", (False, True))
def test_preview_indexes_only_arm64_and_helpers_with_unchanged_schema(
    tmp_path, existing_empty_output
):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    out_dir = tmp_path / "release"
    if existing_empty_output:
        out_dir.mkdir()
    before = _file_snapshot(artifacts)

    path = _assemble(module, artifacts, out_dir, preview=True)

    assert path == out_dir / INDEX
    index = json.loads(path.read_text(encoding="utf-8"))
    assert set(index) == {
        "schema",
        "product",
        "version",
        "commit",
        "build_run_url",
        "signed",
        "artifacts",
        "helper_scripts",
        "assets",
    }
    assert index["schema"] == 1
    assert index["product"] == "tianshu-agent"
    assert index["version"] == "0.1.0"
    assert index["commit"] == COMMIT
    assert index["build_run_url"] == BUILD_RUN_URL
    assert index["signed"] is False
    assert len(index["artifacts"]) == 1
    record = index["artifacts"][0]
    assert set(record) == {
        "target",
        "artifact",
        "sha256",
        "size",
        "checksum",
        "manifest",
        "device_verification",
        "runtime_smoke",
    }
    assert record["target"] == PREVIEW_TARGET
    assert record["device_verification"] == "unknown"
    assert record["runtime_smoke"] == {
        "status": "pass",
        "host_target": PREVIEW_TARGET,
        "version": "0.1.0",
    }
    assert record["artifact"] == PREVIEW_ARTIFACT
    assert record["sha256"] == hashlib.sha256((out_dir / PREVIEW_ARTIFACT).read_bytes()).hexdigest()
    assert record["size"] == (out_dir / PREVIEW_ARTIFACT).stat().st_size
    assets = {item["name"]: item for item in index["assets"]}
    expected = {
        PREVIEW_ARTIFACT,
        PREVIEW_CHECKSUM,
        f"release-manifest-{PREVIEW_TARGET}.json",
        *module.HELPER_SCRIPTS,
    }
    assert set(assets) == expected
    assert {item.name for item in out_dir.iterdir()} == expected | {INDEX}
    for name, item in assets.items():
        assert item["sha256"] == hashlib.sha256((out_dir / name).read_bytes()).hexdigest()
        assert item["size"] == (out_dir / name).stat().st_size
    assert record["checksum"] == assets[PREVIEW_CHECKSUM]
    assert record["manifest"] == assets[f"release-manifest-{PREVIEW_TARGET}.json"]
    assert index["helper_scripts"] == [assets[name] for name in module.HELPER_SCRIPTS]
    for script in module.HELPER_SCRIPTS:
        copied = out_dir / script
        assert copied.read_bytes() == (ROOT / "scripts" / script).read_bytes()
        if os.name == "posix":
            assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert _file_snapshot(artifacts) == before


@pytest.mark.parametrize("missing", (PREVIEW_ARTIFACT, PREVIEW_CHECKSUM, MANIFEST, REPORT))
def test_both_modes_reject_missing_required_inputs(tmp_path, release_mode, missing):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _mode_artifacts(artifacts, release_mode)
    (artifacts / PREVIEW_TARGET / missing).unlink()
    out_dir = tmp_path / "release"
    before = _file_snapshot(artifacts)

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=release_mode)

    if release_mode:
        assert not out_dir.exists()
    assert _file_snapshot(artifacts) == before


@pytest.mark.parametrize("targets", ((), ("macos-x64",), ("windows-x64",)))
def test_preview_rejects_missing_or_foreign_target_before_output(tmp_path, targets):
    module = _load()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _artifacts(artifacts, targets)
    out_dir = tmp_path / "release"
    before = _file_snapshot(artifacts)

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=True)

    assert not out_dir.exists()
    assert _file_snapshot(artifacts) == before


@pytest.mark.parametrize(
    ("location", "entry", "directory"),
    (
        ("root", "macos-x64", True),
        ("root", "windows-x64", True),
        ("root", "windows-arm64", True),
        ("root", "linux-arm64", True),
        ("root", ".DS_Store", False),
        ("target", "old.app.tar.gz", False),
        ("target", "release-manifest-macos-x64.json", False),
        ("target", "bundle", True),
        ("target", ".DS_Store", False),
    ),
)
def test_preview_rejects_every_unexpected_input_before_output(tmp_path, location, entry, directory):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    parent = artifacts if location == "root" else artifacts / PREVIEW_TARGET
    extra = parent / entry
    if directory:
        extra.mkdir()
    else:
        extra.write_bytes(b"stale or unrelated")
    before = _file_snapshot(artifacts)
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=True)

    assert not out_dir.exists()
    assert extra.is_dir() if directory else extra.read_bytes() == b"stale or unrelated"
    assert _file_snapshot(artifacts) == before


@pytest.mark.parametrize(
    "entry",
    (INDEX, PREVIEW_ARTIFACT, PREVIEW_CHECKSUM, "install-macos.sh", "stale.txt", "empty-dir"),
)
def test_preview_rejects_any_existing_output_without_writes(tmp_path, entry):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    out_dir = tmp_path / "release"
    out_dir.mkdir()
    existing = out_dir / entry
    if entry == "empty-dir":
        existing.mkdir()
    else:
        existing.write_bytes(b"old release")
    before = _file_snapshot(out_dir)

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=True)

    assert _file_snapshot(out_dir) == before
    assert {path.name for path in out_dir.iterdir()} == {entry}


@pytest.mark.parametrize(
    ("filename", "field", "value"),
    (
        (MANIFEST, "target", "macos-x64"),
        (MANIFEST, "version", "0.9.9"),
        (MANIFEST, "commit", "1" * 40),
        (MANIFEST, "files", {}),
        (REPORT, "target", "macos-x64"),
        (REPORT, "version", "0.9.9"),
        (REPORT, "commit", "1" * 40),
        (REPORT, "artifact", "other.app.tar.gz"),
        (REPORT, "sha256", "0" * 64),
        (REPORT, "manifest_sha256", "0" * 64),
        (REPORT, "runtime_smoke", None),
        (REPORT, "runtime_smoke", {"status": "fail", "host_target": PREVIEW_TARGET}),
        (REPORT, "runtime_smoke", {"status": "pass", "host_target": "macos-x64"}),
        (
            REPORT,
            "runtime_smoke",
            {"status": "not_run_cross_target", "host_target": PREVIEW_TARGET},
        ),
    ),
)
def test_both_modes_apply_same_identity_manifest_report_and_native_smoke_checks(
    tmp_path, release_mode, filename, field, value
):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _mode_artifacts(artifacts, release_mode)
    source = artifacts / PREVIEW_TARGET / filename
    data = json.loads(source.read_text(encoding="utf-8"))
    data[field] = value
    source.write_text(json.dumps(data), encoding="utf-8")
    out_dir = tmp_path / "release"
    before = _file_snapshot(artifacts)

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=release_mode)

    if release_mode:
        assert not out_dir.exists()
    assert _file_snapshot(artifacts) == before


@pytest.mark.parametrize("filename", (MANIFEST, REPORT))
@pytest.mark.parametrize("payload", (b"{", b"[]", b"\xff"))
def test_both_modes_reject_malformed_json(tmp_path, release_mode, filename, payload):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _mode_artifacts(artifacts, release_mode)
    (artifacts / PREVIEW_TARGET / filename).write_bytes(payload)
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=release_mode)

    if release_mode:
        assert not out_dir.exists()


@pytest.mark.parametrize("tamper", ("artifact", "checksum-hash", "checksum-name", "manifest-bytes"))
def test_both_modes_reject_tampered_input_bytes(tmp_path, release_mode, tamper):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _mode_artifacts(artifacts, release_mode)
    target = artifacts / PREVIEW_TARGET
    checksum = target / PREVIEW_CHECKSUM
    if tamper == "artifact":
        (target / PREVIEW_ARTIFACT).write_bytes(b"tampered")
    elif tamper == "checksum-hash":
        checksum.write_text(f"{'0' * 64}  {PREVIEW_ARTIFACT}\n", encoding="ascii")
    elif tamper == "checksum-name":
        checksum.write_text(checksum.read_text().replace(PREVIEW_ARTIFACT, "other"))
    else:
        source = target / MANIFEST
        source.write_bytes(source.read_bytes() + b"\n")
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=release_mode)

    if release_mode:
        assert not out_dir.exists()


@pytest.mark.parametrize("invalid", (None, 0, 1, "full", "true", [], {}, 0.0))
def test_preview_option_requires_strict_bool_before_writes(tmp_path, invalid):
    module = _load()
    artifacts = tmp_path / "missing-artifacts"
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="macos_arm64_preview"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=out_dir,
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
            macos_arm64_preview=invalid,
        )

    assert not artifacts.exists()
    assert not out_dir.exists()


@pytest.mark.parametrize("explicit_full", (False, True))
def test_legacy_full_still_requires_all_four_targets(tmp_path, explicit_full):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    options = {"macos_arm64_preview": False} if explicit_full else {}
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="macos-x64"):
        module.assemble_release(
            artifacts_root=artifacts,
            out_dir=out_dir,
            version="0.1.0",
            commit=COMMIT,
            build_run_url=BUILD_RUN_URL,
            **options,
        )

    # Full retains the legacy partial-copy behavior; it must never mint an index.
    assert (out_dir / PREVIEW_ARTIFACT).is_file()
    assert not (out_dir / INDEX).exists()


@pytest.mark.parametrize("explicit_full", (False, True))
def test_legacy_full_keeps_existing_output_and_input_tolerance(tmp_path, explicit_full):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts)
    (artifacts / "extra-input.txt").write_bytes(b"legacy input")
    (artifacts / PREVIEW_TARGET / "extra-target-input.txt").write_bytes(b"legacy input")
    out_dir = tmp_path / "release"
    out_dir.mkdir()
    (out_dir / INDEX).write_bytes(b"old index")
    (out_dir / PREVIEW_ARTIFACT).write_bytes(b"old archive")
    stale = out_dir / "stale.txt"
    stale.write_bytes(b"legacy output")
    options = {"macos_arm64_preview": False} if explicit_full else {}

    path = module.assemble_release(
        artifacts_root=artifacts,
        out_dir=out_dir,
        version="0.1.0",
        commit=COMMIT,
        build_run_url=BUILD_RUN_URL,
        **options,
    )

    index = json.loads(path.read_text(encoding="utf-8"))
    assert [item["target"] for item in index["artifacts"]] == list(module.TARGETS)
    assert stale.read_bytes() == b"legacy output"
    assert (out_dir / PREVIEW_ARTIFACT).read_bytes() == PREVIEW_TARGET.encode()
    assert "stale.txt" not in {asset["name"] for asset in index["assets"]}


@pytest.mark.parametrize("preview", (False, True))
def test_cli_preview_flag_and_full_default(tmp_path, preview):
    artifacts = tmp_path / "artifacts"
    _mode_artifacts(artifacts, preview)
    out_dir = tmp_path / "release"
    options = ("--macos-arm64-preview",) if preview else ()

    result = _cli(artifacts, out_dir, *options)

    assert result.returncode == 0, (result.stdout, result.stderr)
    index = json.loads((out_dir / INDEX).read_text(encoding="utf-8"))
    expected = [PREVIEW_TARGET] if preview else list(_load().TARGETS)
    assert [record["target"] for record in index["artifacts"]] == expected


def test_cli_default_full_rejects_arm64_only(tmp_path):
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    out_dir = tmp_path / "release"

    result = _cli(artifacts, out_dir)

    assert result.returncode == 1
    assert not (out_dir / INDEX).exists()


@pytest.mark.parametrize(
    "options",
    (("--macos-arm64-preview=true",), ("--macos-arm64-preview", "false"), ("--mode", "preview")),
)
def test_cli_rejects_invalid_preview_values_and_unknown_mode(tmp_path, options):
    artifacts = tmp_path / "artifacts"
    out_dir = tmp_path / "release"

    result = _cli(artifacts, out_dir, *options)

    assert result.returncode == 2
    assert not artifacts.exists()
    assert not out_dir.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 符号链接负控")
@pytest.mark.parametrize(
    "location",
    (
        "root",
        "root-ancestor",
        "target",
        PREVIEW_ARTIFACT,
        PREVIEW_CHECKSUM,
        MANIFEST,
        REPORT,
        "output",
        "output-ancestor",
    ),
)
def test_both_modes_preserve_link_guards_and_external_bytes(tmp_path, release_mode, location):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _mode_artifacts(artifacts, release_mode)
    out_dir = tmp_path / "release"
    external = tmp_path / "external"
    if location in ("root", "target"):
        linked = artifacts if location == "root" else artifacts / PREVIEW_TARGET
        linked.rename(external)
        linked.symlink_to(external, target_is_directory=True)
    elif location in ("root-ancestor", "output-ancestor"):
        external.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(external, target_is_directory=True)
        if location == "root-ancestor":
            artifacts.rename(external / "artifacts")
            artifacts = alias / "artifacts"
        else:
            out_dir = alias / "release"
    elif location == "output":
        external.mkdir()
        out_dir.symlink_to(external, target_is_directory=True)
    else:
        linked = artifacts / PREVIEW_TARGET / location
        linked.rename(external)
        linked.symlink_to(external)
    before = _file_snapshot(external) if external.is_dir() else external.read_bytes()

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=release_mode)

    after = _file_snapshot(external) if external.is_dir() else external.read_bytes()
    assert after == before
    if release_mode and location not in ("output", "output-ancestor"):
        assert not out_dir.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 硬链接负控")
@pytest.mark.parametrize(
    "filename",
    (
        PREVIEW_ARTIFACT,
        PREVIEW_CHECKSUM,
        f"release-manifest-{PREVIEW_TARGET}.json",
        "install-macos.sh",
        INDEX,
    ),
)
def test_preview_rejects_existing_hardlinked_output_without_modifying_target(tmp_path, filename):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    out_dir = tmp_path / "release"
    out_dir.mkdir()
    external = tmp_path / "external"
    external.write_bytes(b"external sentinel")
    destination = out_dir / filename
    os.link(external, destination)

    with pytest.raises(module.AssembleError):
        _assemble(module, artifacts, out_dir, preview=True)

    assert destination.read_bytes() == external.read_bytes() == b"external sentinel"
    assert {path.name for path in out_dir.iterdir()} == {filename}


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO 负控")
@pytest.mark.parametrize("filename", (PREVIEW_ARTIFACT, PREVIEW_CHECKSUM, MANIFEST, REPORT))
def test_preview_rejects_special_required_input_before_output(tmp_path, filename):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    source = artifacts / PREVIEW_TARGET / filename
    source.unlink()
    os.mkfifo(source)
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="普通文件"):
        _assemble(module, artifacts, out_dir, preview=True)

    assert not out_dir.exists()
    assert stat.S_ISFIFO(source.lstat().st_mode)


@pytest.mark.parametrize("script", ("install-macos.sh", "uninstall-macos.sh"))
def test_preview_validates_helpers_before_output(tmp_path, monkeypatch, script):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    for name in module.HELPER_SCRIPTS:
        if name != script:
            source = scripts_dir / name
            source.write_bytes((ROOT / "scripts" / name).read_bytes())
            source.chmod(0o755)
    monkeypatch.setattr(module, "__file__", str(scripts_dir / SCRIPT.name))
    out_dir = tmp_path / "release"

    with pytest.raises(module.AssembleError, match="辅助脚本"):
        _assemble(module, artifacts, out_dir, preview=True)

    assert not out_dir.exists()


@pytest.mark.parametrize("relative_output", ("new-output", "macos-arm64/new-output"))
def test_preview_cannot_create_output_inside_closed_input_tree(tmp_path, relative_output):
    module = _load()
    artifacts = tmp_path / "artifacts"
    _artifacts(artifacts, (PREVIEW_TARGET,))
    before = _file_snapshot(artifacts)
    out_dir = artifacts / relative_output

    with pytest.raises(module.AssembleError, match="out_dir"):
        _assemble(module, artifacts, out_dir, preview=True)

    assert not out_dir.exists()
    assert _file_snapshot(artifacts) == before
