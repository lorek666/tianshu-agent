from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assemble_release.py"
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"
BUILD_RUN_URL = "https://github.com/example/tianshu-agent/actions/runs/1"


def _load():
    spec = importlib.util.spec_from_file_location("assemble_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifacts(root: Path) -> None:
    for target in _load().TARGETS:
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
