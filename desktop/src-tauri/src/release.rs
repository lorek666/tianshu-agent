use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::PathBuf;
use std::path::{Component, Path};

use serde::Deserialize;
use sha2::{Digest, Sha256};

const FORBIDDEN_PATH_PARTS: &[&str] = &[
    ".git",
    ".omc",
    ".tianshu",
    ".venv",
    "__pycache__",
    "logs",
    "refs",
    "tests",
];
const FORBIDDEN_PATH_SUFFIXES: &[&str] = &[
    ".log",
    ".key",
    ".db",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseIdentity {
    pub version: String,
    pub commit: String,
    pub target: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReleaseIdentityError {
    Invalid,
    TargetMismatch,
}

pub fn current_target() -> Result<&'static str, ReleaseIdentityError> {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        return Ok("macos-arm64");
    }
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    {
        return Ok("macos-x64");
    }
    #[cfg(all(target_os = "windows", target_arch = "x86_64"))]
    {
        return Ok("windows-x64");
    }
    #[cfg(all(target_os = "windows", target_arch = "aarch64"))]
    {
        return Ok("windows-arm64");
    }
    #[allow(unreachable_code)]
    Err(ReleaseIdentityError::Invalid)
}

impl fmt::Display for ReleaseIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid => formatter.write_str("发行身份无效"),
            Self::TargetMismatch => formatter.write_str("发行目标与当前平台不匹配"),
        }
    }
}

impl std::error::Error for ReleaseIdentityError {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawReleaseIdentity {
    schema: u32,
    product: String,
    version: String,
    commit: String,
    target: String,
}

fn valid_version(value: &str) -> bool {
    let parts: Vec<_> = value.split('.').collect();
    parts.len() == 3
        && parts[0] == "0"
        && parts[1..]
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

fn valid_sha(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

impl ReleaseIdentity {
    pub fn parse(raw: &[u8], current_target: &str) -> Result<Self, ReleaseIdentityError> {
        let parsed: RawReleaseIdentity =
            serde_json::from_slice(raw).map_err(|_| ReleaseIdentityError::Invalid)?;
        if parsed.schema != 1
            || parsed.product != "tianshu-agent"
            || !valid_version(&parsed.version)
            || !valid_sha(&parsed.commit)
        {
            return Err(ReleaseIdentityError::Invalid);
        }
        if parsed.target != current_target {
            return Err(ReleaseIdentityError::TargetMismatch);
        }
        Ok(Self {
            version: parsed.version,
            commit: parsed.commit,
            target: parsed.target,
        })
    }

    pub fn read(path: &Path, current_target: &str) -> Result<Self, ReleaseIdentityError> {
        let raw = std::fs::read(path).map_err(|_| ReleaseIdentityError::Invalid)?;
        Self::parse(&raw, current_target)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseManifestError(String);

impl fmt::Display for ReleaseManifestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ReleaseManifestError {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawReleaseManifest {
    schema: u32,
    product: String,
    version: String,
    commit: String,
    target: String,
    python: String,
    python_build_release: String,
    ui_dist_sha256: String,
    files: BTreeMap<String, String>,
}

fn safe_relative_path(value: &str) -> bool {
    !value.is_empty()
        && !value.contains('\\')
        && Path::new(value)
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

fn forbidden_path_reason(relative: &Path) -> Option<&'static str> {
    for component in relative.components() {
        let Component::Normal(part) = component else {
            continue;
        };
        let lowered = part.to_string_lossy().to_ascii_lowercase();
        if FORBIDDEN_PATH_PARTS.contains(&lowered.as_str()) {
            return Some("禁止目录");
        }
    }
    let name = relative
        .file_name()
        .map(|value| value.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default();
    if name.starts_with(".env") {
        return Some("环境文件");
    }
    if FORBIDDEN_PATH_SUFFIXES
        .iter()
        .any(|suffix| name.ends_with(suffix))
    {
        return Some("禁止文件");
    }
    None
}

fn verify_release_path(relative: &Path, display: &str) -> Result<(), ReleaseManifestError> {
    if let Some(reason) = forbidden_path_reason(relative) {
        return Err(ReleaseManifestError(format!(
            "发行物包含{reason}：{display:?}"
        )));
    }
    Ok(())
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_python_version(value: &str) -> bool {
    value
        .strip_prefix("3.12.")
        .is_some_and(|patch| !patch.is_empty() && patch.bytes().all(|byte| byte.is_ascii_digit()))
}

fn verify_file(root: &Path, relative: &str, expected: &str) -> Result<(), ReleaseManifestError> {
    if !safe_relative_path(relative) {
        return Err(ReleaseManifestError(format!(
            "release-manifest 包含不安全路径：{relative:?}"
        )));
    }
    verify_release_path(Path::new(relative), relative)?;
    if !valid_sha256(expected) {
        return Err(ReleaseManifestError(format!(
            "release-manifest 中 {relative} 的 SHA256 不合法"
        )));
    }
    let candidate = root.join(relative);
    let canonical = candidate.canonicalize().map_err(|error| {
        ReleaseManifestError(format!(
            "release-manifest 引用的文件无法读取：{relative}: {error}"
        ))
    })?;
    if !canonical.starts_with(root) || !canonical.is_file() {
        return Err(ReleaseManifestError(format!(
            "release-manifest 引用路径逃出资源目录：{relative}"
        )));
    }
    let canonical_relative = canonical.strip_prefix(root).map_err(|_| {
        ReleaseManifestError(format!("release-manifest 引用路径逃出资源目录：{relative}"))
    })?;
    verify_release_path(canonical_relative, &canonical_relative.to_string_lossy())?;
    let bytes = std::fs::read(&canonical).map_err(|error| {
        ReleaseManifestError(format!(
            "无法读取 release-manifest 文件 {relative}：{error}"
        ))
    })?;
    let actual = format!("{:x}", Sha256::digest(bytes));
    if actual != expected {
        return Err(ReleaseManifestError(format!(
            "资源字节与 release-manifest 不匹配：{relative}"
        )));
    }
    Ok(())
}

fn collect_resource_files(
    root: &Path,
    current: &Path,
    files: &mut BTreeMap<String, PathBuf>,
) -> Result<(), ReleaseManifestError> {
    let directory = root.join(current);
    let entries = fs::read_dir(&directory).map_err(|error| {
        ReleaseManifestError(format!("无法读取资源目录 {}：{error}", directory.display()))
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            ReleaseManifestError(format!("无法读取资源目录 {}：{error}", directory.display()))
        })?;
        let relative = current.join(entry.file_name());
        verify_release_path(&relative, &relative.to_string_lossy())?;
        let file_type = entry.file_type().map_err(|error| {
            ReleaseManifestError(format!(
                "无法取得资源类型 {}：{error}",
                entry.path().display()
            ))
        })?;
        if file_type.is_symlink() {
            let canonical = entry.path().canonicalize().map_err(|error| {
                ReleaseManifestError(format!(
                    "无法解析资源链接 {}：{error}",
                    entry.path().display()
                ))
            })?;
            if !canonical.starts_with(root) || !canonical.is_file() {
                return Err(ReleaseManifestError(format!(
                    "资源链接逃出资源目录或指向非文件：{}",
                    entry.path().display()
                )));
            }
            let canonical_relative = canonical.strip_prefix(root).map_err(|_| {
                ReleaseManifestError(format!("资源链接逃出资源目录：{}", entry.path().display()))
            })?;
            verify_release_path(canonical_relative, &canonical_relative.to_string_lossy())?;
            let relative = relative.to_string_lossy().replace('\\', "/");
            files.insert(relative, canonical);
        } else if file_type.is_dir() {
            collect_resource_files(root, &relative, files)?;
        } else if file_type.is_file() {
            let relative = relative.to_string_lossy().replace('\\', "/");
            files.insert(relative, entry.path());
        } else {
            return Err(ReleaseManifestError(format!(
                "资源目录包含特殊文件：{}",
                entry.path().display()
            )));
        }
    }
    Ok(())
}

pub fn verify_resource_manifest(
    resource_dir: &Path,
    identity: &ReleaseIdentity,
) -> Result<(), ReleaseManifestError> {
    let manifest_path = resource_dir.join("release-manifest.json");
    let raw = std::fs::read(&manifest_path).map_err(|error| {
        ReleaseManifestError(format!("无法读取 {}：{error}", manifest_path.display()))
    })?;
    let manifest: RawReleaseManifest = serde_json::from_slice(&raw)
        .map_err(|error| ReleaseManifestError(format!("release-manifest 不合法：{error}")))?;
    if manifest.schema != 1
        || manifest.product != "tianshu-agent"
        || manifest.version != identity.version
        || manifest.commit != identity.commit
        || manifest.target != identity.target
        || !valid_python_version(&manifest.python)
        || manifest.python_build_release.is_empty()
        || !valid_sha256(&manifest.ui_dist_sha256)
        || manifest.files.is_empty()
    {
        return Err(ReleaseManifestError(
            "release-manifest 身份或必填字段不合法".to_string(),
        ));
    }
    let ui_hash = manifest
        .files
        .get("ui/dist/index.html")
        .ok_or_else(|| ReleaseManifestError("release-manifest 缺少 UI index.html".to_string()))?;
    if ui_hash != &manifest.ui_dist_sha256 {
        return Err(ReleaseManifestError(
            "release-manifest 的 ui_dist_sha256 与 files 不一致".to_string(),
        ));
    }
    let root = resource_dir.canonicalize().map_err(|error| {
        ReleaseManifestError(format!(
            "无法解析资源目录 {}：{error}",
            resource_dir.display()
        ))
    })?;
    for (relative, digest) in &manifest.files {
        verify_file(&root, relative, digest)?;
    }

    let mut actual = BTreeMap::new();
    collect_resource_files(&root, Path::new(""), &mut actual)?;
    let expected: std::collections::BTreeSet<_> = manifest.files.keys().cloned().collect();
    // Tauri may add its generated window/tray icon and this manifest beside the
    // explicitly bundled resources. These metadata files cannot carry Python
    // import hooks or executable payloads of their own.
    let mut allowed = expected.clone();
    allowed.extend(
        ["release-manifest.json", "icon.icns", "icon.ico"]
            .into_iter()
            .map(str::to_owned),
    );
    let actual_paths: std::collections::BTreeSet<_> = actual.keys().cloned().collect();
    let unlisted: Vec<_> = actual_paths.difference(&allowed).cloned().collect();
    let missing: Vec<_> = expected.difference(&actual_paths).cloned().collect();
    if !unlisted.is_empty() || !missing.is_empty() {
        return Err(ReleaseManifestError(format!(
            "资源文件集合与 release-manifest 不一致；未列文件={unlisted:?}，缺失文件={missing:?}"
        )));
    }
    Ok(())
}
