use std::fs;

use serde_json::json;
use sha2::{Digest, Sha256};
use tianshu_desktop::release::{ReleaseIdentity, ReleaseIdentityError, verify_resource_manifest};

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn release_identity_requires_exact_target_and_main_sha() {
    let data = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64"
    }"#;

    let identity = ReleaseIdentity::parse(data, "macos-arm64").unwrap();

    assert_eq!(identity.version, "0.1.0");
    assert_eq!(identity.target, "macos-arm64");
    assert_eq!(identity.commit, "595be35c037cef35da7f166a5db2a624f2b733a9");
}

#[test]
fn release_identity_rejects_target_mismatch() {
    let data = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "windows-x64"
    }"#;

    assert_eq!(
        ReleaseIdentity::parse(data, "macos-arm64").unwrap_err(),
        ReleaseIdentityError::TargetMismatch
    );
}

#[test]
fn release_identity_rejects_extra_fields() {
    let data = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64",
      "debug": true
    }"#;

    assert_eq!(
        ReleaseIdentity::parse(data, "macos-arm64").unwrap_err(),
        ReleaseIdentityError::Invalid
    );
}

#[test]
fn release_identity_rejects_future_schema_and_bad_commit() {
    let future = br#"{
      "schema": 2,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64"
    }"#;
    let bad_commit = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "not-a-sha",
      "target": "macos-arm64"
    }"#;

    assert_eq!(
        ReleaseIdentity::parse(future, "macos-arm64").unwrap_err(),
        ReleaseIdentityError::Invalid
    );
    assert_eq!(
        ReleaseIdentity::parse(bad_commit, "macos-arm64").unwrap_err(),
        ReleaseIdentityError::Invalid
    );
}

#[test]
fn release_identity_reads_only_the_requested_file() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("release.json");
    fs::write(
        &path,
        br#"{
          "schema": 1,
          "product": "tianshu-agent",
          "version": "0.1.0",
          "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
          "target": "macos-arm64"
        }"#,
    )
    .unwrap();

    let identity = ReleaseIdentity::read(&path, "macos-arm64").unwrap();

    assert_eq!(identity.version, "0.1.0");
}

#[test]
fn resource_manifest_binds_real_file_bytes() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    let ui = root.join("ui/dist/index.html");
    fs::create_dir_all(ui.parent().unwrap()).unwrap();
    fs::write(&ui, b"<!doctype html>").unwrap();
    let identity_bytes = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64"
    }"#;
    fs::write(root.join("release.json"), identity_bytes).unwrap();
    let identity = ReleaseIdentity::parse(identity_bytes, "macos-arm64").unwrap();
    let manifest = json!({
      "schema": 1,
      "product": "tianshu-agent",
      "version": identity.version.clone(),
      "commit": identity.commit.clone(),
      "target": identity.target.clone(),
      "python": "3.12.16",
      "python_build_release": "20261110",
      "ui_dist_sha256": sha256(b"<!doctype html>"),
      "files": {
        "release.json": sha256(identity_bytes),
        "ui/dist/index.html": sha256(b"<!doctype html>")
      }
    });
    fs::write(
        root.join("release-manifest.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();

    verify_resource_manifest(root, &identity).unwrap();

    fs::write(&ui, b"tampered").unwrap();
    let error = verify_resource_manifest(root, &identity).unwrap_err();
    assert!(error.to_string().contains("不匹配"));
}

#[test]
fn resource_manifest_rejects_identity_mismatch_and_path_escape() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    let identity_bytes = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64"
    }"#;
    let identity = ReleaseIdentity::parse(identity_bytes, "macos-arm64").unwrap();
    let ui = root.join("ui/dist/index.html");
    fs::create_dir_all(ui.parent().unwrap()).unwrap();
    fs::write(&ui, b"x").unwrap();
    let file_hash = sha256(b"x");
    let manifest = json!({
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64",
      "python": "3.12.16",
      "python_build_release": "20261110",
      "ui_dist_sha256": file_hash,
      "files": {
        "../outside": file_hash,
        "ui/dist/index.html": file_hash
      }
    });
    fs::write(
        root.join("release-manifest.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();

    let error = verify_resource_manifest(root, &identity).unwrap_err();
    assert!(error.to_string().contains("不安全路径"));
}

#[test]
fn resource_manifest_rejects_unlisted_python_import_hook() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    let identity_bytes = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64"
    }"#;
    fs::write(root.join("release.json"), identity_bytes).unwrap();
    let ui = root.join("ui/dist/index.html");
    fs::create_dir_all(ui.parent().unwrap()).unwrap();
    fs::write(&ui, b"<!doctype html>").unwrap();
    let identity = ReleaseIdentity::parse(identity_bytes, "macos-arm64").unwrap();
    let manifest = json!({
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64",
      "python": "3.12.16",
      "python_build_release": "20261110",
      "ui_dist_sha256": sha256(b"<!doctype html>"),
      "files": {
        "release.json": sha256(identity_bytes),
        "ui/dist/index.html": sha256(b"<!doctype html>")
      }
    });
    fs::write(
        root.join("release-manifest.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();
    fs::write(root.join("sitecustomize.py"), b"raise SystemExit(9)").unwrap();

    let error = verify_resource_manifest(root, &identity).unwrap_err();

    assert!(error.to_string().contains("未列文件"));
    assert!(error.to_string().contains("sitecustomize.py"));
}

#[test]
fn resource_manifest_allows_only_tauri_generated_metadata() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    let identity_bytes = br#"{
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64"
    }"#;
    fs::write(root.join("release.json"), identity_bytes).unwrap();
    let ui = root.join("ui/dist/index.html");
    fs::create_dir_all(ui.parent().unwrap()).unwrap();
    fs::write(&ui, b"<!doctype html>").unwrap();
    fs::write(root.join("icon.icns"), b"generated").unwrap();
    let identity = ReleaseIdentity::parse(identity_bytes, "macos-arm64").unwrap();
    let manifest = json!({
      "schema": 1,
      "product": "tianshu-agent",
      "version": "0.1.0",
      "commit": "595be35c037cef35da7f166a5db2a624f2b733a9",
      "target": "macos-arm64",
      "python": "3.12.16",
      "python_build_release": "20261110",
      "ui_dist_sha256": sha256(b"<!doctype html>"),
      "files": {
        "release.json": sha256(identity_bytes),
        "ui/dist/index.html": sha256(b"<!doctype html>")
      }
    });
    fs::write(
        root.join("release-manifest.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();

    verify_resource_manifest(root, &identity).unwrap();
}
