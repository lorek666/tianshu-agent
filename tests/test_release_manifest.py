from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_manifest.py"
SCHEMA = ROOT / "release" / "release.schema.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("release_manifest_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_manifest(base: Path) -> dict:
    runtime = base / "bundle" / "python" / "python.exe"
    ui = base / "bundle" / "ui" / "index.html"
    runtime.parent.mkdir(parents=True)
    ui.parent.mkdir(parents=True)
    runtime.write_bytes(b"synthetic python runtime")
    ui.write_text("<!doctype html><title>Tianshu</title>", encoding="utf-8")
    return {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
        "target": "windows-x64",
        "python": "3.12.16",
        "python_build_release": "20990101",
        "ui_dist_sha256": _sha(ui),
        "files": {
            "bundle/python/python.exe": _sha(runtime),
            "bundle/ui/index.html": _sha(ui),
        },
    }


def test_valid_manifest_is_checked_against_real_bundle_bytes(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)

    module.validate_manifest(data, base_dir=tmp_path)

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert set(data) == set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["product"]["const"] == "tianshu-agent"
    assert schema["properties"]["target"]["enum"] == [
        "macos-arm64",
        "macos-x64",
        "windows-x64",
        "windows-arm64",
    ]


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("schema", 2, "schema"),
        ("schema", True, "schema"),
        ("schema", 1.0, "schema"),
        ("product", "other-agent", "product"),
        ("version", "1.0.0", "version"),
        ("commit", "A" * 40, "commit"),
        ("target", "linux-x64", "target"),
        ("python", "3.13.1", "python"),
        ("python_build_release", "", "python_build_release"),
        ("ui_dist_sha256", "0" * 63, "ui_dist_sha256"),
    ],
)
def test_manifest_rejects_unknown_or_malformed_identity(tmp_path, field, bad_value, message):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    data[field] = bad_value

    with pytest.raises(module.ReleaseManifestError, match=message):
        module.validate_manifest(data, base_dir=tmp_path)


def test_manifest_rejects_unknown_fields(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    data["signed"] = True

    with pytest.raises(module.ReleaseManifestError, match="未知字段"):
        module.validate_manifest(data, base_dir=tmp_path)


def test_manifest_rejects_changed_bundle_byte(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    (tmp_path / "bundle" / "ui" / "index.html").write_text("tampered", encoding="utf-8")

    with pytest.raises(module.ReleaseManifestError, match="SHA256 不匹配"):
        module.validate_manifest(data, base_dir=tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "bundle\\windows",
        "tests/secret.txt",
        ".omc/evidence.log",
        ".env.production",
        "bundle/events.db",
    ],
)
def test_manifest_rejects_unsafe_or_forbidden_paths(tmp_path, path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.write_text("outside", encoding="utf-8")
    data["files"][path] = _sha(outside)

    with pytest.raises(module.ReleaseManifestError, match="路径|禁止|绝对|环境"):
        module.validate_manifest(data, base_dir=tmp_path)


def test_manifest_requires_ui_hash_to_match_frozen_dist(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    data["ui_dist_sha256"] = "0" * 64

    with pytest.raises(module.ReleaseManifestError, match="ui_dist_sha256"):
        module.validate_manifest(data, base_dir=tmp_path)


def test_read_and_write_manifest_round_trip(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    path = tmp_path / "release-manifest.json"

    module.write_manifest(path, data)

    assert module.read_manifest(path, base_dir=tmp_path) == data
