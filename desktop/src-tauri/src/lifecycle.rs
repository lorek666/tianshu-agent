use std::fmt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HostEndpoint {
    pub pid: u32,
    pub port: u16,
}

impl HostEndpoint {
    pub fn url(self) -> String {
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
struct RawHostInfo {
    pid: u32,
    port: u16,
    token: String,
}

pub fn parse_host_endpoint(raw: &[u8]) -> Result<HostEndpoint, HostInfoError> {
    let parsed: RawHostInfo =
        serde_json::from_slice(raw).map_err(|_| HostInfoError::InvalidEndpoint)?;
    if parsed.pid == 0 || parsed.token.is_empty() {
        return Err(HostInfoError::InvalidEndpoint);
    }
    if parsed.port == 0 {
        return Err(HostInfoError::NotReady);
    }
    Ok(HostEndpoint {
        pid: parsed.pid,
        port: parsed.port,
    })
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
        .env("PYTHONPATH", app_dir);
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
