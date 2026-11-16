from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows 当前进程无创建符号链接权限；不冒充已验证 symlink")
        raise


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
        "bundle/.ENV.production",
        "bundle/events.db",
        "bundle/events.db-wal",
        "bundle/events.db-shm",
        "bundle/events.db-journal",
        "bundle/events.sqlite-wal",
        "bundle/events.sqlite3-shm",
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


@pytest.mark.parametrize("destination", [".tianshu/events.db", ".env"])
def test_manifest_rejects_internal_symlink_to_forbidden_file(tmp_path, destination):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    secret = tmp_path / destination
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"synthetic private bytes")
    link = tmp_path / "bundle/resource.bin"
    _symlink_or_skip(link, secret)
    data["files"]["bundle/resource.bin"] = _sha(secret)

    with pytest.raises(module.ReleaseManifestError, match="禁止|环境"):
        module.validate_manifest(data, base_dir=tmp_path)


def test_manifest_rejects_symlink_outside_release_root(tmp_path):
    module = _load_module()
    root = tmp_path / "release"
    data = _valid_manifest(root)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside release root")
    _symlink_or_skip(root / "bundle/resource.bin", outside)
    data["files"]["bundle/resource.bin"] = _sha(outside)

    with pytest.raises(module.ReleaseManifestError, match="逃出发布根"):
        module.validate_manifest(data, base_dir=root)


def test_manifest_allows_internal_symlink_to_permitted_file(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    asset = tmp_path / "bundle/asset.bin"
    asset.write_bytes(b"allowed resource")
    _symlink_or_skip(tmp_path / "bundle/resource.bin", asset)
    data["files"]["bundle/resource.bin"] = _sha(asset)

    module.validate_manifest(data, base_dir=tmp_path)


@pytest.mark.parametrize(
    "fake_ui", ["bundle/notui/index.html", "bundle/notui/dist/index.html"]
)
def test_manifest_does_not_bind_ui_from_partial_component(tmp_path, fake_ui):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    data["files"].pop("bundle/ui/index.html")
    (tmp_path / "bundle/ui/index.html").unlink()
    fake = tmp_path / fake_ui
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"not a UI resource")
    data["files"][fake_ui] = _sha(fake)
    data["ui_dist_sha256"] = _sha(fake)

    with pytest.raises(module.ReleaseManifestError, match="ui_dist_sha256"):
        module.validate_manifest(data, base_dir=tmp_path)


@pytest.mark.parametrize(
    "ui_path", ["ui/index.html", "ui/dist/index.html", "bundle/ui/dist/index.html"]
)
def test_manifest_accepts_explicit_ui_path_components(tmp_path, ui_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    data["files"].pop("bundle/ui/index.html")
    (tmp_path / "bundle/ui/index.html").unlink()
    ui = tmp_path / ui_path
    ui.parent.mkdir(parents=True, exist_ok=True)
    ui.write_bytes(b"actual UI resource")
    data["files"][ui_path] = _sha(ui)
    data["ui_dist_sha256"] = _sha(ui)

    module.validate_manifest(data, base_dir=tmp_path)


@pytest.mark.parametrize(
    ("field", "constraint", "replacement"),
    [
        ("version", "pattern", r"^9\.9\.9$"),
        ("commit", "pattern", r"^a{40}$"),
        ("python", "pattern", r"^3\.12\.99$"),
        ("python_build_release", "minLength", 20),
        ("ui_dist_sha256", "pattern", r"^0{64}$"),
        ("files", "minProperties", 3),
        ("files", "additionalProperties", {"type": "string", "pattern": r"^0{64}$"}),
    ],
)
def test_manifest_applies_constraints_from_real_schema(
    tmp_path, field, constraint, replacement
):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    schema["properties"][field][constraint] = replacement
    changed_schema = tmp_path / "changed-release.schema.json"
    changed_schema.write_text(json.dumps(schema), encoding="utf-8")
    module.SCHEMA_PATH = changed_schema

    message = field if field != "files" else "files|SHA256"
    with pytest.raises(module.ReleaseManifestError, match=message):
        module.validate_manifest(data, base_dir=tmp_path)


def test_read_and_write_manifest_round_trip(tmp_path):
    module = _load_module()
    data = _valid_manifest(tmp_path)
    path = tmp_path / "release-manifest.json"

    module.write_manifest(path, data)

    assert module.read_manifest(path, base_dir=tmp_path) == data
