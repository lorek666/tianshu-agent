use std::fmt;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde::Deserialize;

#[derive(Clone, PartialEq, Eq)]
pub struct HostEndpoint {
    pub pid: u32,
    pub port: u16,
    pub token: String,
}

impl fmt::Debug for HostEndpoint {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("HostEndpoint")
            .field("pid", &self.pid)
            .field("port", &self.port)
            .field("token", &"<redacted>")
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
    Timeout,
}

impl fmt::Display for HostInfoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingLocalAppData => formatter.write_str("LOCALAPPDATA 未设置"),
            Self::InvalidEndpoint => formatter.write_str("Host 元数据不合法"),
            Self::NotReady => formatter.write_str("Host 尚未就绪"),
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
}

fn valid_token(value: &str) -> bool {
    (16..=256).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
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
    Ok(HostEndpoint {
        pid: parsed.pid,
        port: parsed.port,
        token: parsed.token,
    })
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
}

struct HttpResponse {
    status: u16,
    body: Vec<u8>,
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

pub fn verify_host(endpoint: &HostEndpoint) -> Result<(), String> {
    let health = http_request(endpoint, "GET", "/health", None)?;
    if health.status != 200 {
        return Err(format!("Host /health 返回 HTTP {}", health.status));
    }
    let health: HealthResponse = serde_json::from_slice(&health.body)
        .map_err(|error| format!("Host /health 响应不合法：{error}"))?;
    if !health.ok || health.pid != endpoint.pid {
        return Err("Host /health PID 与锁文件不一致".to_string());
    }

    let session = http_request(endpoint, "GET", "/session", Some(endpoint.token.as_str()))?;
    if session.status != 200 {
        return Err(format!("Host /session 返回 HTTP {}", session.status));
    }
    let session: SessionResponse = serde_json::from_slice(&session.body)
        .map_err(|error| format!("Host /session 响应不合法：{error}"))?;
    if !session.ok || session.pid != endpoint.pid {
        return Err("Host token 或 PID 身份核验失败".to_string());
    }
    Ok(())
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
) -> Command {
    let mut command = Command::new(python);
    command
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
        .env("PYTHONPATH", app_dir)
        .env("PYTHONDONTWRITEBYTECODE", "1");
    command
}

pub fn build_orphan_daemon_shutdown_command(
    python: &Path,
    app_dir: &Path,
    data_root: &Path,
) -> Command {
    let mut command = Command::new(python);
    command
        .arg("-c")
        .arg(
            "import sys; from pathlib import Path; \
             from tianshu.daemon import request_shutdown; \
             raise SystemExit(0 if request_shutdown(Path(sys.argv[1]), timeout=5) else 1)",
        )
        .arg(data_root)
        .env("PYTHONPATH", app_dir)
        .env("PYTHONDONTWRITEBYTECODE", "1");
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
