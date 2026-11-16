use std::fmt;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::release::ReleaseIdentity;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostReleaseIdentity {
    pub version: String,
    pub commit: String,
    pub target: String,
}

#[derive(Clone, PartialEq, Eq)]
pub struct HostEndpoint {
    pub pid: u32,
    pub port: u16,
    pub token: String,
    pub started_at: u64,
    pub release: Option<HostReleaseIdentity>,
}

impl fmt::Debug for HostEndpoint {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("HostEndpoint")
            .field("pid", &self.pid)
            .field("port", &self.port)
            .field("token", &"<redacted>")
            .field("started_at", &self.started_at)
            .field("release", &self.release)
            .finish()
    }
}

impl HostEndpoint {
    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.port)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostInfoError {
    MissingLocalAppData,
    InvalidEndpoint,
    NotReady,
    ChildExited,
    Rejected(String),
    Timeout,
}

impl fmt::Display for HostInfoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingLocalAppData => formatter.write_str("LOCALAPPDATA 未设置"),
            Self::InvalidEndpoint => formatter.write_str("Host 元数据不合法"),
            Self::NotReady => formatter.write_str("Host 尚未就绪"),
            Self::ChildExited => formatter.write_str("Host 子进程在就绪前退出"),
            Self::Rejected(detail) => formatter.write_str(detail),
            Self::Timeout => formatter.write_str("等待 Host 就绪超时"),
        }
    }
}

impl std::error::Error for HostInfoError {}

#[derive(Debug, Clone)]
pub struct DefaultDataRootInput {
    pub home: PathBuf,
    pub local_app_data: Option<PathBuf>,
}

pub fn default_data_root(
    platform: &str,
    input: &DefaultDataRootInput,
) -> Result<PathBuf, HostInfoError> {
    match platform {
        "macos" => Ok(input.home.join("Library/Application Support/AgentOS")),
        "windows" => input
            .local_app_data
            .as_ref()
            .map(|root| root.join("AgentOS"))
            .ok_or(HostInfoError::MissingLocalAppData),
        _ => Err(HostInfoError::MissingLocalAppData),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawHostInfo {
    pid: u32,
    port: u16,
    token: String,
    started_at: u64,
    #[serde(rename = "ui_dir")]
    _ui_dir: String,
    release: Option<RawHostRelease>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawHostRelease {
    schema: u32,
    product: String,
    version: String,
    commit: String,
    target: String,
}

fn valid_token(value: &str) -> bool {
    (16..=256).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn valid_release_version(value: &str) -> bool {
    let parts: Vec<_> = value.split('.').collect();
    parts.len() == 3
        && parts[0] == "0"
        && parts[1..]
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

fn valid_release_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_release_target(value: &str) -> bool {
    matches!(
        value,
        "macos-arm64" | "macos-x64" | "windows-x64" | "windows-arm64"
    )
}

fn parse_host_release(release: RawHostRelease) -> Result<HostReleaseIdentity, HostInfoError> {
    if release.schema != 1
        || release.product != "tianshu-agent"
        || !valid_release_version(&release.version)
        || !valid_release_commit(&release.commit)
        || !valid_release_target(&release.target)
    {
        return Err(HostInfoError::InvalidEndpoint);
    }
    Ok(HostReleaseIdentity {
        version: release.version,
        commit: release.commit,
        target: release.target,
    })
}

pub fn parse_host_endpoint(raw: &[u8]) -> Result<HostEndpoint, HostInfoError> {
    let parsed: RawHostInfo =
        serde_json::from_slice(raw).map_err(|_| HostInfoError::InvalidEndpoint)?;
    if parsed.pid == 0 || parsed.started_at == 0 || !valid_token(&parsed.token) {
        return Err(HostInfoError::InvalidEndpoint);
    }
    if parsed.port == 0 {
        return Err(HostInfoError::NotReady);
    }
    let release = parsed.release.map(parse_host_release).transpose()?;
    Ok(HostEndpoint {
        pid: parsed.pid,
        port: parsed.port,
        token: parsed.token,
        started_at: parsed.started_at,
        release,
    })
}

pub fn verify_host_release(
    endpoint: &HostEndpoint,
    expected: &ReleaseIdentity,
) -> Result<(), String> {
    let Some(actual) = endpoint.release.as_ref() else {
        return Err(
            "已有 Host 缺少发行身份，无法确认它与当前发行物兼容；请退出旧实例后重试".to_string(),
        );
    };
    if actual.version != expected.version
        || actual.commit != expected.commit
        || actual.target != expected.target
    {
        return Err(format!(
            "已有 Host 发行身份 {}@{}/{} 与当前桌面壳 {}@{}/{} 不兼容；请退出旧实例后重试",
            actual.version,
            actual.commit,
            actual.target,
            expected.version,
            expected.commit,
            expected.target
        ));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HealthResponse {
    ok: bool,
    pid: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SessionResponse {
    ok: bool,
    pid: u32,
    release: Option<RawHostRelease>,
}

struct HttpResponse {
    status: u16,
    body: Vec<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum HostVerificationErrorKind {
    Unavailable,
    IdentityMismatch,
}

#[derive(Debug)]
pub(crate) struct HostVerificationError {
    pub kind: HostVerificationErrorKind,
    detail: String,
}

impl fmt::Display for HostVerificationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

fn host_verification_error(
    endpoint: &HostEndpoint,
    data_root: &Path,
    kind: HostVerificationErrorKind,
    detail: impl Into<String>,
) -> HostVerificationError {
    let guidance = match kind {
        HostVerificationErrorKind::Unavailable => "",
        HostVerificationErrorKind::IdentityMismatch => "；请退出旧实例后重试",
    };
    HostVerificationError {
        kind,
        detail: format!(
            "Host 身份校验失败：PID={}，data_root={}；{}{}",
            endpoint.pid,
            data_root.display(),
            detail.into(),
            guidance
        ),
    }
}

fn http_request(
    endpoint: &HostEndpoint,
    method: &str,
    path: &str,
    token: Option<&str>,
) -> Result<HttpResponse, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], endpoint.port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(500))
        .map_err(|error| format!("连接 Host 失败：{error}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|error| format!("设置 Host 读取超时失败：{error}"))?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|error| format!("设置 Host 写入超时失败：{error}"))?;
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n",
        endpoint.port
    );
    if let Some(token) = token {
        request.push_str(&format!("X-Tianshu-Token: {token}\r\n"));
    }
    request.push_str("Content-Length: 0\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("写入 Host 请求失败：{error}"))?;
    let mut raw = Vec::new();
    stream
        .take(1024 * 1024)
        .read_to_end(&mut raw)
        .map_err(|error| format!("读取 Host 响应失败：{error}"))?;
    let separator = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| "Host 响应缺少 HTTP 头".to_string())?;
    let head =
        std::str::from_utf8(&raw[..separator]).map_err(|_| "Host HTTP 头不是 UTF-8".to_string())?;
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| "Host HTTP 状态行不合法".to_string())?;
    Ok(HttpResponse {
        status,
        body: raw[separator + 4..].to_vec(),
    })
}

pub(crate) fn verify_host_detailed(
    endpoint: &HostEndpoint,
    expected: &ReleaseIdentity,
    data_root: &Path,
) -> Result<(), HostVerificationError> {
    let health = http_request(endpoint, "GET", "/health", None).map_err(|detail| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::Unavailable,
            detail,
        )
    })?;
    if health.status != 200 {
        return Err(host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::Unavailable,
            format!("Host /health 返回 HTTP {}", health.status),
        ));
    }
    let health: HealthResponse = serde_json::from_slice(&health.body).map_err(|error| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::Unavailable,
            format!("Host /health 响应不合法：{error}"),
        )
    })?;
    if !health.ok || health.pid != endpoint.pid {
        return Err(host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::Unavailable,
            "Host /health PID 与锁文件不一致",
        ));
    }

    verify_host_release(endpoint, expected).map_err(|detail| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            detail,
        )
    })?;

    let session = http_request(endpoint, "GET", "/session", Some(endpoint.token.as_str()))
        .map_err(|detail| {
            host_verification_error(
                endpoint,
                data_root,
                HostVerificationErrorKind::Unavailable,
                detail,
            )
        })?;
    if session.status != 200 {
        return Err(host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            format!("Host /session 返回 HTTP {}", session.status),
        ));
    }
    let session: SessionResponse = serde_json::from_slice(&session.body).map_err(|error| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            format!("Host /session 响应不合法：{error}"),
        )
    })?;
    if !session.ok || session.pid != endpoint.pid {
        return Err(host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            "Host token 或 PID 身份核验失败",
        ));
    }
    let session_release = session.release.ok_or_else(|| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            "Host /session 缺少发行身份",
        )
    })?;
    let session_release = parse_host_release(session_release).map_err(|_| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            "Host /session 发行身份不合法",
        )
    })?;
    let lock_release = endpoint.release.as_ref().ok_or_else(|| {
        host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            "host.lock 缺少发行身份",
        )
    })?;
    if &session_release != lock_release {
        return Err(host_verification_error(
            endpoint,
            data_root,
            HostVerificationErrorKind::IdentityMismatch,
            format!(
                "host.lock 发行身份 {}@{}/{} 与已认证 /session 身份 {}@{}/{} 不一致",
                lock_release.version,
                lock_release.commit,
                lock_release.target,
                session_release.version,
                session_release.commit,
                session_release.target
            ),
        ));
    }
    Ok(())
}

pub fn verify_host(
    endpoint: &HostEndpoint,
    expected: &ReleaseIdentity,
    data_root: &Path,
) -> Result<(), String> {
    verify_host_detailed(endpoint, expected, data_root).map_err(|error| error.to_string())
}

pub fn request_host_shutdown(endpoint: &HostEndpoint) -> Result<(), String> {
    let response = http_request(endpoint, "POST", "/shutdown", Some(endpoint.token.as_str()))?;
    if response.status != 200 {
        return Err(format!("Host /shutdown 返回 HTTP {}", response.status));
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ShutdownResponse {
        ok: bool,
        shutting_down: bool,
    }
    let response: ShutdownResponse = serde_json::from_slice(&response.body)
        .map_err(|error| format!("Host /shutdown 响应不合法：{error}"))?;
    if !response.ok || !response.shutting_down {
        return Err("Host 未接受退出请求".to_string());
    }
    Ok(())
}

pub fn build_host_command(
    python: &Path,
    app_dir: &Path,
    ui_dir: &Path,
    data_root: &Path,
    release: &ReleaseIdentity,
) -> Command {
    let mut command = Command::new(python);
    command
        .arg("-P")
        .arg("-m")
        .arg("tianshu")
        .arg("host")
        .arg("run")
        .arg("--port")
        .arg("0")
        .arg("--ui-dir")
        .arg(ui_dir)
        .arg("--data-root")
        .arg(data_root)
        .arg("--release-version")
        .arg(&release.version)
        .arg("--release-commit")
        .arg(&release.commit)
        .arg("--release-target")
        .arg(&release.target)
        .env("PYTHONPATH", app_dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONIOENCODING", "utf-8");
    command
}

pub fn build_orphan_daemon_shutdown_command(
    python: &Path,
    app_dir: &Path,
    data_root: &Path,
    expected_sha256: &str,
) -> Command {
    let mut command = Command::new(python);
    command
        .arg("-P")
        .arg("-c")
        .arg(
            "import sys; from pathlib import Path; \
             from tianshu.host import stop_daemon_if_unowned; \
             raise SystemExit(stop_daemon_if_unowned(Path(sys.argv[1]), sys.argv[2], timeout=4))",
        )
        .arg(data_root)
        .arg(expected_sha256)
        .env("PYTHONPATH", app_dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        // stderr is a pipe here. Pin UTF-8 instead of allowing the Windows
        // locale code page to turn diagnostic text into a different failure.
        .env("PYTHONIOENCODING", "utf-8");
    command
}

pub fn build_host_exit_daemon_shutdown_command(
    python: &Path,
    app_dir: &Path,
    data_root: &Path,
    expected_sha256: Option<&str>,
    expected_host_pid: u32,
    expected_host_started_at: u64,
) -> Command {
    let mut command = Command::new(python);
    command
        .arg("-P")
        .arg("-c")
        .arg(
            "import sys; from pathlib import Path; \
             from tianshu.host import stop_daemon_after_host_exit; \
             raise SystemExit(stop_daemon_after_host_exit(\
             Path(sys.argv[1]), None if not sys.argv[2] else sys.argv[2], \
             int(sys.argv[3]), int(sys.argv[4]), timeout=4))",
        )
        .arg(data_root)
        .arg(expected_sha256.unwrap_or(""))
        .arg(expected_host_pid.to_string())
        .arg(expected_host_started_at.to_string())
        .env("PYTHONPATH", app_dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONIOENCODING", "utf-8");
    command
}

pub fn wait_for_host<F, S>(
    timeout: Duration,
    interval: Duration,
    mut read: F,
    mut sleep: S,
) -> Result<HostEndpoint, HostInfoError>
where
    F: FnMut() -> Result<HostEndpoint, HostInfoError>,
    S: FnMut(Duration),
{
    let started = Instant::now();
    loop {
        match read() {
            Ok(endpoint) => return Ok(endpoint),
            Err(HostInfoError::NotReady) if started.elapsed() < timeout => sleep(interval),
            Err(HostInfoError::NotReady) => return Err(HostInfoError::Timeout),
            Err(error) => return Err(error),
        }
    }
}
