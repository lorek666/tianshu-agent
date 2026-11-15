from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assemble_release.py"
COMMIT = "595be35c037cef35da7f166a5db2a624f2b733a9"


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
        build_run_url="https://github.com/example/actions/runs/1",
    )

    index = json.loads(path.read_text(encoding="utf-8"))
    assert index["signed"] is False
    assert [item["target"] for item in index["artifacts"]] == list(module.TARGETS)
    assert all(item["device_verification"] == "unknown" for item in index["artifacts"])
    assert (path.parent / "install_macos.sh").is_file()
    for item in index["artifacts"]:
        assert item["sha256"] == hashlib.sha256(
            (path.parent / item["artifact"]).read_bytes()
        ).hexdigest()


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
            build_run_url="https://github.com/example/actions/runs/1",
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
            build_run_url="https://github.com/example/actions/runs/1",
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
            build_run_url="https://github.com/example/actions/runs/1",
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
            build_run_url="https://github.com/example/actions/runs/1",
        )
