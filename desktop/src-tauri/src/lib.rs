pub mod lifecycle;
pub mod release;

#[cfg(debug_assertions)]
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use tauri::menu::{Menu, MenuItem};
#[cfg(target_os = "macos")]
use tauri::menu::{MenuBuilder, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent, WindowEvent};

use crate::lifecycle::{
    DefaultDataRootInput, HostEndpoint, HostInfoError, HostVerificationErrorKind,
    build_host_command, build_host_exit_daemon_shutdown_command,
    build_orphan_daemon_shutdown_command, default_data_root, parse_host_endpoint,
    request_host_shutdown, verify_host, verify_host_detailed, wait_for_host,
};
use crate::release::{ReleaseIdentity, current_target, verify_resource_manifest};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const STARTUP_POLL: Duration = Duration::from_millis(100);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
const HOST_WATCH_POLL: Duration = Duration::from_millis(500);
const HOST_DOWN_CONFIRMATIONS: u8 = 2;
const MAX_HOST_RESTARTS: u8 = 3;
const HOST_RESTART_BACKOFF: [Duration; 3] = [
    Duration::from_millis(500),
    Duration::from_secs(1),
    Duration::from_secs(2),
];
const HOST_STABLE_RESET: Duration = Duration::from_secs(60);
// A new Host publishes port 0 before it finishes binding and then replaces that
// record with the listening endpoint. Keep a bounded handoff window after the
// old metadata disappears so Daemon fallback cleanup cannot race that takeover.
const ATTACHED_HOST_HANDOFF_SETTLE: Duration = Duration::from_secs(2);
// Detached Daemon startup can outlive the Host process that requested it. The
// Python lock has a 2 s stale-lock grace window; allow process import/startup
// beyond that without introducing an unbounded retry.
const FAILED_HOST_DAEMON_SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

struct RuntimeState {
    python: PathBuf,
    app_dir: PathBuf,
    data_root: PathBuf,
    endpoint: HostEndpoint,
    child: Option<Child>,
    owns_host: bool,
    stopped: bool,
    release_identity: ReleaseIdentity,
}

impl RuntimeState {
    fn finish_owned_host_exit(&mut self) -> Result<(), String> {
        self.child = None;
        let daemon = read_daemon_lock_snapshot(&self.data_root)?;
        let result = stop_daemon_after_host_exit(
            &self.python,
            &self.app_dir,
            &self.data_root,
            daemon.as_deref(),
            self.endpoint.pid,
            self.endpoint.started_at,
        );
        if result.is_ok() {
            self.stopped = true;
        }
        result
    }

    fn stop_attached_host(&mut self) -> Result<(), String> {
        match verify_host_detailed(&self.endpoint, &self.release_identity, &self.data_root) {
            Ok(()) => {}
            Err(error) if error.kind == HostVerificationErrorKind::IdentityMismatch => {
                return Err(format!("退出前 Host 身份校验失败：{error}"));
            }
            Err(_) => {}
        }
        let daemon_before = read_daemon_lock_snapshot(&self.data_root)?;
        if let Err(request_error) = request_host_shutdown(&self.endpoint) {
            if verify_host(&self.endpoint, &self.release_identity, &self.data_root).is_ok() {
                return Err(format!(
                    "直接请求 Host 退出失败，但 Host 仍可连接：{request_error}"
                ));
            }
            thread::sleep(ATTACHED_HOST_HANDOFF_SETTLE);
            match read_host_endpoint(&self.data_root) {
                Ok(current) if current != self.endpoint => {
                    return Err("Host 退出期间实例已变更，拒绝停止新实例的服务".to_string());
                }
                Err(HostInfoError::NotReady) => {}
                Ok(_) => {}
                Err(error) => return Err(format!("确认附着 Host 退出失败：{error}")),
            }
            stop_daemon_after_host_exit(
                &self.python,
                &self.app_dir,
                &self.data_root,
                daemon_before.as_deref(),
                self.endpoint.pid,
                self.endpoint.started_at,
            )?;
            self.stopped = true;
            return Ok(());
        }
        let started = Instant::now();
        loop {
            match read_host_endpoint(&self.data_root) {
                Err(HostInfoError::NotReady) => {
                    let absent_since = Instant::now();
                    while absent_since.elapsed() < ATTACHED_HOST_HANDOFF_SETTLE {
                        thread::sleep(STARTUP_POLL);
                        match read_host_endpoint(&self.data_root) {
                            Err(HostInfoError::NotReady) => {}
                            Ok(_) => {
                                return Err(
                                    "Host 退出期间实例已变更，拒绝停止新实例的服务".to_string()
                                );
                            }
                            Err(error) => {
                                return Err(format!("等待附着 Host 退出失败：{error}"));
                            }
                        }
                    }
                    break;
                }
                Ok(endpoint) if endpoint.pid == self.endpoint.pid => {
                    if started.elapsed() >= SHUTDOWN_TIMEOUT {
                        return Err(format!(
                            "Host 未在 {} 秒内退出，服务仍保留",
                            SHUTDOWN_TIMEOUT.as_secs()
                        ));
                    }
                    thread::sleep(STARTUP_POLL);
                }
                Ok(_) => {
                    return Err("Host 退出期间实例已变更，拒绝停止新实例的服务".to_string());
                }
                Err(error) => {
                    return Err(format!("等待附着 Host 退出失败：{error}"));
                }
            }
        }

        let daemon_after = read_daemon_lock_snapshot(&self.data_root)?;
        if let Some(current) = daemon_after {
            if daemon_before.as_ref() != Some(&current) {
                return Err("Host 退出期间 Daemon 实例已变更，拒绝停止新实例".to_string());
            }
            match read_host_endpoint(&self.data_root) {
                Err(HostInfoError::NotReady) => {}
                Ok(_) => {
                    return Err("Host 退出期间实例已变更，拒绝停止新实例的服务".to_string());
                }
                Err(error) => return Err(format!("确认附着 Host 退出失败：{error}")),
            }
            stop_orphan_daemon(&self.python, &self.app_dir, &self.data_root, Some(&current))?;
        }
        self.stopped = true;
        Ok(())
    }

    fn stop_host(&mut self) -> Result<(), String> {
        if self.stopped {
            return Ok(());
        }
        if !self.owns_host {
            return self.stop_attached_host();
        }
        if self.child.is_none() {
            return self.finish_owned_host_exit();
        }
        if let Some(child) = self.child.as_mut()
            && let Ok(Some(_)) = child.try_wait()
        {
            return self.finish_owned_host_exit();
        }
        if let Err(error) = request_host_shutdown(&self.endpoint) {
            if let Some(child) = self.child.as_mut()
                && let Ok(Some(_)) = child.try_wait()
            {
                return self.finish_owned_host_exit();
            }
            return Err(format!("直接请求 Host 退出失败：{error}"));
        }
        let child = self
            .child
            .as_mut()
            .ok_or_else(|| "Host 子进程状态缺失".to_string())?;
        let started = Instant::now();
        while started.elapsed() < SHUTDOWN_TIMEOUT {
            match child.try_wait() {
                Ok(Some(_)) => {
                    return self.finish_owned_host_exit();
                }
                Ok(None) => thread::sleep(STARTUP_POLL),
                Err(error) => return Err(format!("等待 Host 退出失败：{error}")),
            }
        }
        Err(format!(
            "Host 未在 {} 秒内退出，进程仍保留",
            SHUTDOWN_TIMEOUT.as_secs()
        ))
    }

    fn stop_host_on_exit(&mut self) -> Result<(), String> {
        let started = Instant::now();
        if self.owns_host
            && let Some(child) = self.child.as_ref()
            && child.id() != self.endpoint.pid
        {
            return Err("系统退出时 Host 子句柄与已确认身份不符，拒绝终止".to_string());
        }
        let graceful = self.stop_host();
        if graceful.is_ok() || !self.owns_host {
            return graceful;
        }
        let Some(child) = self.child.as_mut() else {
            return graceful;
        };
        // The Child handle is the authority for forced termination. Never turn
        // an attached endpoint or a newly published host.lock PID into a kill.
        // A rejected/unreachable shutdown request can fail before the normal
        // grace period. Still give this child the same bounded time to exit.
        while started.elapsed() < SHUTDOWN_TIMEOUT {
            match child.try_wait() {
                Ok(Some(_)) => return self.finish_owned_host_exit(),
                Ok(None) => thread::sleep(STARTUP_POLL),
                Err(error) => return Err(format!("系统退出时查询 Host 状态失败：{error}")),
            }
        }
        if child
            .try_wait()
            .map_err(|error| format!("系统退出时查询 Host 状态失败：{error}"))?
            .is_none()
        {
            child
                .kill()
                .map_err(|error| format!("系统退出超时后终止自有 Host 失败：{error}"))?;
            eprintln!("系统退出超时，已终止本壳拥有的 Host 子进程；终止不等于业务副作用已回滚");
        }
        child
            .wait()
            .map_err(|error| format!("系统退出时回收自有 Host 失败：{error}"))?;
        self.finish_owned_host_exit()
    }
}

#[derive(Clone)]
struct StartupPaths {
    python: PathBuf,
    app_dir: PathBuf,
    ui_dir: PathBuf,
    data_root: PathBuf,
    release_identity: ReleaseIdentity,
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

#[cfg(debug_assertions)]
fn resource_env_path(name: &str) -> Option<PathBuf> {
    if std::env::var_os("TIANSHU_DESKTOP_ALLOW_RESOURCE_OVERRIDE").as_deref()
        != Some(OsStr::new("1"))
    {
        return None;
    }
    env_path(name)
}

#[cfg(not(debug_assertions))]
fn resource_env_path(_name: &str) -> Option<PathBuf> {
    // Production bundles must use Tauri's canonical resource directory. An
    // environment switch here would let an unverified tree bypass manifest
    // validation while still presenting itself as the signed application.
    None
}

fn platform_data_root() -> Result<PathBuf, String> {
    if let Some(root) = env_path("TIANSHU_DATA_ROOT") {
        if !root.is_absolute() {
            return Err("TIANSHU_DATA_ROOT 必须是绝对路径".to_string());
        }
        return Ok(root);
    }
    let home = env_path("HOME")
        .or_else(|| env_path("USERPROFILE"))
        .ok_or_else(|| "无法定位用户主目录".to_string())?;
    let platform = if cfg!(target_os = "windows") {
        "windows"
    } else {
        "macos"
    };
    default_data_root(
        platform,
        &DefaultDataRootInput {
            home,
            local_app_data: env_path("LOCALAPPDATA"),
        },
    )
    .map_err(|error| error.to_string())
}

fn startup_paths(resource_dir: &Path, package_version: &str) -> Result<StartupPaths, String> {
    let target = current_target().map_err(|error| error.to_string())?;
    let release_path = resource_env_path("TIANSHU_DESKTOP_RELEASE_FILE")
        .unwrap_or_else(|| resource_dir.join("release.json"));
    let identity =
        ReleaseIdentity::read(&release_path, target).map_err(|error| error.to_string())?;
    if identity.version != package_version {
        return Err(format!(
            "发行版本 {} 与桌面壳版本 {} 不匹配",
            identity.version, package_version
        ));
    }
    verify_resource_manifest(resource_dir, &identity).map_err(|error| error.to_string())?;

    let python = resource_env_path("TIANSHU_DESKTOP_PYTHON").unwrap_or_else(|| {
        if cfg!(target_os = "windows") {
            resource_dir.join("python").join("python.exe")
        } else {
            resource_dir.join("python").join("bin").join("python3")
        }
    });
    let app_dir =
        resource_env_path("TIANSHU_DESKTOP_APP_DIR").unwrap_or_else(|| resource_dir.join("app"));
    let ui_dir = resource_env_path("TIANSHU_DESKTOP_UI_DIR")
        .unwrap_or_else(|| resource_dir.join("ui").join("dist"));

    if !python.is_file() {
        return Err(format!("找不到锁定 Python 解释器：{}", python.display()));
    }
    if !app_dir.join("tianshu").is_dir() {
        return Err(format!("找不到打包的 tianshu 包：{}", app_dir.display()));
    }
    if !ui_dir.join("index.html").is_file() {
        return Err(format!("找不到打包的 UI：{}", ui_dir.display()));
    }

    Ok(StartupPaths {
        python,
        app_dir,
        ui_dir,
        data_root: platform_data_root()?,
        release_identity: identity,
    })
}

pub fn verify_bundle(resource_dir: &Path) -> Result<(), String> {
    let target = current_target().map_err(|error| error.to_string())?;
    let identity = ReleaseIdentity::read(&resource_dir.join("release.json"), target)
        .map_err(|error| error.to_string())?;
    verify_resource_manifest(resource_dir, &identity).map_err(|error| error.to_string())
}

fn host_info_path(data_root: &Path) -> PathBuf {
    data_root.join("run").join("host.lock")
}

fn read_host_endpoint(data_root: &Path) -> Result<HostEndpoint, HostInfoError> {
    let raw = std::fs::read(host_info_path(data_root)).map_err(|_| HostInfoError::NotReady)?;
    parse_host_endpoint(&raw)
}

fn daemon_lock_path(data_root: &Path) -> PathBuf {
    data_root.join("run").join("daemon.lock")
}

fn read_daemon_lock_snapshot(data_root: &Path) -> Result<Option<Vec<u8>>, String> {
    match std::fs::read(daemon_lock_path(data_root)) {
        Ok(raw) => Ok(Some(raw)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("读取 Daemon 锁快照失败：{error}")),
    }
}

fn stop_orphan_daemon(
    python: &Path,
    app_dir: &Path,
    data_root: &Path,
    expected_snapshot: Option<&[u8]>,
) -> Result<(), String> {
    let daemon_lock = daemon_lock_path(data_root);
    if !daemon_lock.exists() {
        return Ok(());
    }
    let expected = expected_snapshot
        .ok_or_else(|| "缺少退出前 Daemon 锁快照，拒绝停止未知实例".to_string())?;
    let expected_sha256 = format!("{:x}", Sha256::digest(expected));
    let output = build_orphan_daemon_shutdown_command(python, app_dir, data_root, &expected_sha256)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("请求孤儿 Daemon 退出失败：{error}"))?;
    let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
    match output.status.code() {
        Some(0) => Ok(()),
        Some(3) => Err("Host 在 Daemon 清理期间已接管，保留新实例服务".to_string()),
        Some(4) => Err("Daemon 实例已变更，保留新实例服务".to_string()),
        _ if detail.is_empty() => Err(format!("孤儿 Daemon 退出请求返回失败：{}", output.status)),
        _ => Err(format!(
            "孤儿 Daemon 退出请求返回失败：{}；{detail}",
            output.status
        )),
    }
}

fn stop_daemon_after_host_exit(
    python: &Path,
    app_dir: &Path,
    data_root: &Path,
    expected_snapshot: Option<&[u8]>,
    expected_host_pid: u32,
    expected_host_started_at: u64,
) -> Result<(), String> {
    let expected_sha256 =
        expected_snapshot.map(|snapshot| format!("{:x}", Sha256::digest(snapshot)));
    let output = build_host_exit_daemon_shutdown_command(
        python,
        app_dir,
        data_root,
        expected_sha256.as_deref(),
        expected_host_pid,
        expected_host_started_at,
    )
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::piped())
    .output()
    .map_err(|error| format!("清理已退出 Host 的服务失败：{error}"))?;
    let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
    match output.status.code() {
        Some(0) => Ok(()),
        Some(3) => Err("Host 在 Daemon 清理期间已接管，保留新实例服务".to_string()),
        Some(4) => Err("Daemon 实例已变更，保留新实例服务".to_string()),
        _ if detail.is_empty() => Err(format!("已退出 Host 的服务清理返回失败：{}", output.status)),
        _ => Err(format!(
            "已退出 Host 的服务清理返回失败：{}；{detail}",
            output.status
        )),
    }
}

fn stop_daemon_started_by_failed_host(
    paths: &StartupPaths,
    before: &Option<Vec<u8>>,
) -> Result<(), String> {
    let started = Instant::now();
    loop {
        let after = read_daemon_lock_snapshot(&paths.data_root)?;
        if let Some(current) = after
            && before.as_ref() != Some(&current)
        {
            return stop_orphan_daemon(
                &paths.python,
                &paths.app_dir,
                &paths.data_root,
                Some(&current),
            );
        }
        if started.elapsed() >= FAILED_HOST_DAEMON_SETTLE_TIMEOUT {
            return Ok(());
        }
        thread::sleep(STARTUP_POLL);
    }
}

fn ensure_host(paths: &StartupPaths) -> Result<(HostEndpoint, Option<Child>), String> {
    if let Ok(endpoint) = read_host_endpoint(&paths.data_root) {
        match verify_host_detailed(&endpoint, &paths.release_identity, &paths.data_root) {
            Ok(()) => return Ok((endpoint, None)),
            Err(error) if error.kind == HostVerificationErrorKind::IdentityMismatch => {
                return Err(error.to_string());
            }
            Err(_) => {}
        }
    }

    let daemon_before = read_daemon_lock_snapshot(&paths.data_root)?;
    let mut child = build_host_command(
        &paths.python,
        &paths.app_dir,
        &paths.ui_dir,
        &paths.data_root,
        &paths.release_identity,
    )
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null())
    .spawn()
    .map_err(|error| format!("启动 Host 失败：{error}"))?;

    let endpoint = wait_for_host(
        STARTUP_TIMEOUT,
        STARTUP_POLL,
        || {
            if let Ok(Some(status)) = child.try_wait() {
                eprintln!("Host 在就绪前退出：{status}");
                return Err(HostInfoError::ChildExited);
            }
            let endpoint = read_host_endpoint(&paths.data_root)?;
            match verify_host_detailed(&endpoint, &paths.release_identity, &paths.data_root) {
                Ok(()) => {}
                Err(error) if error.kind == HostVerificationErrorKind::IdentityMismatch => {
                    return Err(HostInfoError::Rejected(error.to_string()));
                }
                Err(_) => return Err(HostInfoError::NotReady),
            }
            Ok(endpoint)
        },
        thread::sleep,
    )
    .map_err(|error| format!("等待 Host 就绪失败：{error}"));

    match endpoint {
        Ok(endpoint) => Ok((endpoint, Some(child))),
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            match stop_daemon_started_by_failed_host(paths, &daemon_before) {
                Ok(()) => Err(error),
                Err(cleanup) => Err(format!("{error}；清理本次启动的 Daemon 失败：{cleanup}")),
            }
        }
    }
}

#[derive(Debug, Default)]
struct HostRestartPolicy {
    attempts: u8,
    healthy_since: Option<Instant>,
}

impl HostRestartPolicy {
    fn note_healthy(&mut self, now: Instant) {
        match self.healthy_since {
            None => self.healthy_since = Some(now),
            Some(started) if now.duration_since(started) >= HOST_STABLE_RESET => {
                self.attempts = 0;
            }
            Some(_) => {}
        }
    }

    fn note_unhealthy(&mut self) {
        self.healthy_since = None;
    }

    fn take_attempt(&mut self) -> bool {
        if self.attempts >= MAX_HOST_RESTARTS {
            return false;
        }
        self.attempts += 1;
        true
    }
}

fn navigate_to_recovered_host(app: &tauri::AppHandle, endpoint: &HostEndpoint) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let Ok(url) = tauri::Url::parse(&endpoint.url()) else {
        return;
    };
    let navigation_window = window.clone();
    let _ = window.run_on_main_thread(move || {
        if let Err(error) = navigation_window.set_title("天枢 Agent") {
            eprintln!("恢复后更新窗口标题失败：{error}");
        }
        if let Err(error) = navigation_window.navigate(url) {
            eprintln!("恢复后页面导航失败：{error}");
        }
    });
}

fn show_recovery_failure(app: &tauri::AppHandle, attempts: u8, error: &str) {
    eprintln!("Host 自动恢复第 {attempts}/{MAX_HOST_RESTARTS} 次失败：{error}");
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let title_window = window.clone();
    let _ = window.run_on_main_thread(move || {
        let _ = title_window.set_title("天枢 Agent · Host 恢复失败");
    });
}

fn adopt_discovered_host(
    state: &mut RuntimeState,
    discovered: HostEndpoint,
    identity: &ReleaseIdentity,
    data_root: &Path,
) -> bool {
    if let Some(child) = state.child.as_mut() {
        match child.try_wait() {
            Ok(Some(_)) => {}
            Ok(None) => return false,
            Err(error) => {
                eprintln!("检查旧 Host 子进程状态失败：{error}");
                return false;
            }
        }
    }
    if verify_host(&discovered, identity, data_root).is_err() {
        return false;
    }
    state.endpoint = discovered;
    state.child = None;
    state.owns_host = false;
    true
}

fn spawn_host_supervisor(app: tauri::AppHandle, paths: StartupPaths) -> std::io::Result<()> {
    thread::Builder::new()
        .name("host-supervisor".to_string())
        .spawn(move || {
            let mut policy = HostRestartPolicy::default();
            let mut unhealthy_observations = 0_u8;
            loop {
                thread::sleep(HOST_WATCH_POLL);
                let Some(state) = app.try_state::<Mutex<RuntimeState>>() else {
                    return;
                };
                let Ok(mut state) = state.lock() else {
                    eprintln!("Host supervisor 无法取得运行时状态锁");
                    return;
                };
                if state.stopped {
                    return;
                }

                let mut healthy =
                    verify_host(&state.endpoint, &state.release_identity, &state.data_root).is_ok();
                let data_root = state.data_root.clone();
                if let Ok(discovered) = read_host_endpoint(&state.data_root)
                    && discovered != state.endpoint
                    && adopt_discovered_host(
                        &mut state,
                        discovered,
                        &paths.release_identity,
                        &data_root,
                    )
                {
                    healthy = true;
                }
                if let Some(child) = state.child.as_mut() {
                    match child.try_wait() {
                        Ok(Some(_)) => {
                            state.child = None;
                            healthy = false;
                        }
                        Ok(None) => {}
                        Err(error) => {
                            eprintln!("检查 Host 子进程状态失败：{error}");
                            healthy = false;
                        }
                    }
                }

                if healthy {
                    unhealthy_observations = 0;
                    policy.note_healthy(Instant::now());
                    continue;
                }

                policy.note_unhealthy();
                // A live owned child that merely fails a health probe is not a
                // crash. Keep observing it instead of starting a second Host.
                if state.child.is_some() {
                    continue;
                }
                unhealthy_observations = unhealthy_observations.saturating_add(1);
                if unhealthy_observations < HOST_DOWN_CONFIRMATIONS || !policy.take_attempt() {
                    continue;
                }

                let backoff_index = usize::from(policy.attempts.saturating_sub(1));
                thread::sleep(HOST_RESTART_BACKOFF[backoff_index]);
                match ensure_host(&paths) {
                    Ok((endpoint, child)) => {
                        state.endpoint = endpoint.clone();
                        state.owns_host = child.is_some();
                        state.child = child;
                        state.stopped = false;
                        unhealthy_observations = 0;
                        policy.note_healthy(Instant::now());
                        navigate_to_recovered_host(&app, &endpoint);
                    }
                    Err(error) => show_recovery_failure(&app, policy.attempts, &error),
                }
            }
        })
        .map(|_| ())
}

fn show_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn show_quit_error(app: &tauri::AppHandle, error: &str) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_title("天枢 Agent · 服务退出失败");
        let _ = window.show();
        let _ = window.set_focus();
    }
    eprintln!("天枢桌面服务退出失败，保留进程：{error}");
}

fn request_quit(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Mutex<RuntimeState>>() {
        match state.lock() {
            Ok(mut state) => {
                if let Err(error) = state.stop_host() {
                    show_quit_error(app, &error);
                    return;
                }
            }
            Err(error) => {
                show_quit_error(app, &format!("运行时状态锁损坏：{error}"));
                return;
            }
        }
    }
    app.exit(0);
}

#[cfg(target_os = "macos")]
fn install_application_menu(app: &tauri::App) -> tauri::Result<()> {
    let quit = MenuItem::with_id(app, "app-quit", "退出天枢 Agent", true, Some("CmdOrCtrl+Q"))?;
    let application = SubmenuBuilder::new(app, "天枢 Agent")
        .about(None)
        .separator()
        .item(&quit)
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .build()?;
    let edit = SubmenuBuilder::new(app, "编辑")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;
    let menu = MenuBuilder::new(app)
        .item(&application)
        .item(&edit)
        .build()?;
    app.set_menu(menu)?;
    Ok(())
}

fn install_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "打开天枢 Agent", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出并停止服务", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let mut tray = TrayIconBuilder::with_id("tianshu-tray")
        .tooltip("天枢 Agent")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_window(app),
            "quit" => request_quit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn initialise(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let window = app
        .get_webview_window("main")
        .ok_or("Tauri main window missing")?;
    let package_version = app.package_info().version.to_string();
    #[cfg(target_os = "macos")]
    install_application_menu(app)?;
    let resource_dir = match resource_env_path("TIANSHU_DESKTOP_RESOURCE_DIR")
        .or_else(|| app.path().resource_dir().ok())
    {
        Some(resource_dir) => resource_dir,
        None => {
            let detail = serde_json::to_string("无法定位 Tauri 资源目录")?;
            window.set_title("天枢 Agent · 启动失败")?;
            window.eval(format!(
                "document.getElementById('detail').textContent = {detail};"
            ))?;
            install_tray(app.handle())?;
            return Ok(());
        }
    };

    match startup_paths(&resource_dir, &package_version).and_then(|paths| {
        let (endpoint, child) = ensure_host(&paths)?;
        Ok((paths, endpoint, child))
    }) {
        Ok((paths, endpoint, child)) => {
            let url = tauri::Url::parse(&endpoint.url())?;
            let owns_host = child.is_some();
            let supervisor_paths = paths.clone();
            app.manage(Mutex::new(RuntimeState {
                python: paths.python,
                app_dir: paths.app_dir,
                data_root: paths.data_root,
                endpoint,
                child,
                owns_host,
                stopped: false,
                release_identity: paths.release_identity,
            }));
            // `setup` runs before Tauri starts the event loop. Host startup can
            // block here while the child process becomes healthy, so queue the
            // navigation until the runtime can process it. Calling `navigate`
            // directly from this blocking setup leaves the release WebView blank
            // on a cold start even though the Host is healthy.
            let navigation_window = window.clone();
            window.run_on_main_thread(move || {
                if let Err(error) = navigation_window.navigate(url) {
                    eprintln!("天枢桌面页面导航失败：{error}");
                }
            })?;
            spawn_host_supervisor(app.handle().clone(), supervisor_paths)?;
        }
        Err(error) => {
            eprintln!("天枢桌面启动失败：{error}");
            window.set_title("天枢 Agent · 启动失败")?;
            let detail = serde_json::to_string(&error)?;
            let error_window = window.clone();
            window.run_on_main_thread(move || {
                if let Err(error) = error_window.eval(format!(
                    "document.getElementById('detail').textContent = {detail};"
                )) {
                    eprintln!("天枢桌面启动失败页更新失败：{error}");
                }
            })?;
        }
    }
    install_tray(app.handle())?;
    Ok(())
}

pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_window(app);
        }))
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "app-quit" {
                request_quit(app);
            }
        })
        .setup(initialise)
        .on_window_event(|window, event| {
            if window.label() == "main"
                && let WindowEvent::CloseRequested { api, .. } = event
            {
                api.prevent_close();
                let _ = window.hide();
            }
        });
    #[cfg(target_os = "macos")]
    let builder = builder.enable_macos_default_menu(false);
    let app = builder
        .build(tauri::generate_context!())
        .expect("failed to build Tianshu desktop app");

    app.run(|app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_window(app),
        RunEvent::Resumed => show_window(app),
        #[cfg(target_os = "macos")]
        RunEvent::Opened { .. } => show_window(app),
        RunEvent::ExitRequested { api, .. } => {
            let Some(state) = app.try_state::<Mutex<RuntimeState>>() else {
                return;
            };
            match state.lock() {
                Ok(mut state) => {
                    if let Err(error) = state.stop_host() {
                        api.prevent_exit();
                        show_quit_error(app, &error);
                    }
                }
                Err(error) => {
                    api.prevent_exit();
                    show_quit_error(app, &format!("运行时状态锁损坏：{error}"));
                }
            }
        }
        RunEvent::Exit => {
            // tao maps macOS applicationWillTerminate and Windows confirmed
            // WM_ENDSESSION to LoopDestroyed, which Wry exposes as Exit.
            // ExitRequested has no system-session reason; its code only
            // distinguishes user interaction from AppHandle::exit/restart.
            // Keep menu/ExitRequested shutdown graceful and cancellable, but
            // use the owned-child deadline once the OS/event loop is exiting.
            if let Some(state) = app.try_state::<Mutex<RuntimeState>>()
                && let Ok(mut state) = state.lock()
                && let Err(error) = state.stop_host_on_exit()
            {
                eprintln!("系统退出时服务清理未完成：{error}");
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    #[cfg(unix)]
    use std::fs;
    #[cfg(unix)]
    use std::io::{Read, Write};
    #[cfg(unix)]
    use std::net::TcpListener;
    #[cfg(unix)]
    use std::process::{Command, Stdio};
    #[cfg(unix)]
    use std::thread;
    use std::time::{Duration, Instant};

    use super::*;

    fn test_release_identity() -> ReleaseIdentity {
        ReleaseIdentity {
            version: "0.1.0".to_string(),
            commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
            target: "macos-arm64".to_string(),
        }
    }

    fn test_release_payload() -> String {
        let release = test_release_identity();
        format!(
            r#"{{"schema":1,"product":"tianshu-agent","version":"{}","commit":"{}","target":"{}"}}"#,
            release.version, release.commit, release.target
        )
    }

    fn test_host_release_identity() -> crate::lifecycle::HostReleaseIdentity {
        let release = test_release_identity();
        crate::lifecycle::HostReleaseIdentity {
            version: release.version,
            commit: release.commit,
            target: release.target,
        }
    }

    #[test]
    fn host_restart_policy_is_bounded_and_resets_after_stability() {
        let started = Instant::now();
        let mut policy = HostRestartPolicy::default();
        policy.note_healthy(started);
        policy.note_unhealthy();

        assert!(policy.take_attempt());
        assert!(policy.take_attempt());
        assert!(policy.take_attempt());
        assert!(!policy.take_attempt());
        assert_eq!(policy.attempts, MAX_HOST_RESTARTS);

        policy.note_healthy(started + HOST_STABLE_RESET);
        policy
            .note_healthy(started + HOST_STABLE_RESET + HOST_STABLE_RESET + Duration::from_secs(1));
        assert_eq!(policy.attempts, 0);
        assert!(policy.take_attempt());
    }

    #[cfg(unix)]
    #[test]
    fn discovered_host_does_not_drop_a_live_owned_child() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let child = Command::new("sleep").arg("2").spawn().unwrap();
        let child_pid = child.id();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let discovered_port = listener.local_addr().unwrap().port();
        let discovered_pid = child_pid.saturating_add(1);
        let release = test_release_identity();
        let release_body = format!(
            r#"{{"schema":1,"product":"tianshu-agent","version":"{}","commit":"{}","target":"{}"}}"#,
            release.version, release.commit, release.target
        );
        let server = thread::spawn(move || {
            for body in [
                format!(r#"{{"ok":true,"pid":{discovered_pid}}}"#),
                format!(r#"{{"ok":true,"pid":{discovered_pid},"release":{release_body}}}"#),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut raw = Vec::new();
                loop {
                    let mut chunk = [0_u8; 1024];
                    let count = stream.read(&mut chunk).unwrap();
                    raw.extend_from_slice(&chunk[..count]);
                    if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        let original = HostEndpoint {
            pid: child_pid,
            port: 59123,
            token: "secret_secret_secret".to_string(),
            started_at: 1,
            release: Some(crate::lifecycle::HostReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            }),
        };
        let discovered = HostEndpoint {
            pid: discovered_pid,
            port: discovered_port,
            token: "takeover_token_123".to_string(),
            started_at: 2,
            release: Some(crate::lifecycle::HostReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            }),
        };
        let mut state = RuntimeState {
            python: root.join("fake-python.sh"),
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: original.clone(),
            child: Some(child),
            owns_host: true,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let identity = test_release_identity();
        let first_adopted = adopt_discovered_host(&mut state, discovered.clone(), &identity, root);
        let first_endpoint = state.endpoint.clone();
        let first_owns_host = state.owns_host;
        let first_child_present = state.child.is_some();

        if let Some(mut child) = state.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        } else {
            let _ = Command::new("kill").arg(child_pid.to_string()).status();
        }
        let mut exited_child = Command::new("sh").args(["-c", "exit 0"]).spawn().unwrap();
        exited_child.wait().unwrap();
        state.child = Some(exited_child);
        state.endpoint = original.clone();
        state.owns_host = true;
        let second_adopted = adopt_discovered_host(&mut state, discovered.clone(), &identity, root);
        server.join().unwrap();

        assert!(!first_adopted, "活着的 owned child 被新 endpoint 顶掉");
        assert_eq!(first_endpoint, original);
        assert!(first_owns_host);
        assert!(first_child_present, "新 endpoint 让 App 丢失了子进程句柄");
        assert!(second_adopted, "旧 child 已退出后仍未接管新 Host");
        assert_eq!(state.endpoint, discovered);
        assert!(state.child.is_none());
        assert!(!state.owns_host);
    }

    #[cfg(unix)]
    #[test]
    fn discovered_host_with_session_identity_drift_is_not_adopted() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let discovered_pid = std::process::id();
        let server = thread::spawn(move || {
            for body in [
                format!(r#"{{"ok":true,"pid":{discovered_pid}}}"#),
                format!(
                    r#"{{"ok":true,"pid":{discovered_pid},"release":{{"schema":1,"product":"tianshu-agent","version":"0.1.0","commit":"1123456789abcdef0123456789abcdef01234567","target":"macos-arm64"}}}}"#
                ),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut raw = Vec::new();
                loop {
                    let mut chunk = [0_u8; 1024];
                    let count = stream.read(&mut chunk).unwrap();
                    raw.extend_from_slice(&chunk[..count]);
                    if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        let original = HostEndpoint {
            pid: discovered_pid.saturating_sub(1),
            port: 59_123,
            token: "original_token_1234".to_string(),
            started_at: 1,
            release: Some(test_host_release_identity()),
        };
        let discovered = HostEndpoint {
            pid: discovered_pid,
            port,
            token: "takeover_token_1234".to_string(),
            started_at: 2,
            release: Some(test_host_release_identity()),
        };
        let mut state = RuntimeState {
            python: root.join("fake-python.sh"),
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: original.clone(),
            child: None,
            owns_host: true,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let adopted = adopt_discovered_host(&mut state, discovered, &test_release_identity(), root);
        server.join().unwrap();

        assert!(!adopted);
        assert_eq!(state.endpoint, original);
        assert!(state.owns_host);
        assert!(state.child.is_none());
    }

    #[cfg(unix)]
    fn write_executable(path: &Path, body: &str) {
        use std::os::unix::fs::PermissionsExt;

        fs::write(path, body).unwrap();
        let mut permissions = fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(path, permissions).unwrap();
    }

    #[cfg(unix)]
    fn shell_quote(path: &Path) -> String {
        format!("'{}'", path.to_string_lossy().replace('\'', "'\\''"))
    }

    #[cfg(unix)]
    fn shutdown_server(
        release: PathBuf,
    ) -> (u16, std::sync::mpsc::Receiver<()>, thread::JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let (ready, receiver) = std::sync::mpsc::channel();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut raw = Vec::new();
            loop {
                let mut chunk = [0_u8; 1024];
                let count = stream.read(&mut chunk).unwrap();
                raw.extend_from_slice(&chunk[..count]);
                if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let body = r#"{"ok":true,"shutting_down":true}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
            fs::write(release, b"exit").unwrap();
            ready.send(()).unwrap();
        });
        (port, receiver, server)
    }

    #[cfg(unix)]
    fn shutdown_server_removing(
        paths: Vec<PathBuf>,
    ) -> (
        u16,
        std::sync::mpsc::Receiver<String>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let (sender, receiver) = std::sync::mpsc::channel();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut raw = Vec::new();
            loop {
                let mut chunk = [0_u8; 1024];
                let count = stream.read(&mut chunk).unwrap();
                raw.extend_from_slice(&chunk[..count]);
                if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let body = r#"{"ok":true,"shutting_down":true}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
            for path in paths {
                match fs::remove_file(path) {
                    Ok(()) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => panic!("cleanup failed: {error}"),
                }
            }
            sender
                .send(String::from_utf8(raw).unwrap())
                .expect("request");
        });
        (port, receiver, server)
    }

    #[cfg(unix)]
    fn verified_shutdown_server_removing(
        paths: Vec<PathBuf>,
        pid: u32,
    ) -> (
        u16,
        std::sync::mpsc::Receiver<String>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let (sender, receiver) = std::sync::mpsc::channel();
        let server = thread::spawn(move || {
            for index in 0..3 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut raw = Vec::new();
                loop {
                    let mut chunk = [0_u8; 1024];
                    let count = stream.read(&mut chunk).unwrap();
                    raw.extend_from_slice(&chunk[..count]);
                    if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                let body = match index {
                    0 => format!(r#"{{"ok":true,"pid":{pid}}}"#),
                    1 => format!(
                        r#"{{"ok":true,"pid":{pid},"release":{}}}"#,
                        test_release_payload()
                    ),
                    _ => r#"{"ok":true,"shutting_down":true}"#.to_string(),
                };
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).unwrap();
                sender
                    .send(String::from_utf8(raw).unwrap())
                    .expect("request");
                if index == 2 {
                    for path in &paths {
                        match fs::remove_file(path) {
                            Ok(()) => {}
                            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                            Err(error) => panic!("cleanup failed: {error}"),
                        }
                    }
                }
            }
        });
        (port, receiver, server)
    }

    #[cfg(unix)]
    fn stubborn_host_state(
        root: &Path,
    ) -> (
        RuntimeState,
        std::sync::mpsc::Receiver<String>,
        thread::JoinHandle<()>,
    ) {
        let child = Command::new("/bin/cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let (port, request, server) = shutdown_server_removing(Vec::new());
        let python = root.join("fake-python.sh");
        write_executable(&python, "#!/bin/sh\nexit 0\n");
        let state = RuntimeState {
            python,
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: child.id(),
                port,
                token: "secret_secret_secret".to_string(),
                started_at: 1,
                release: None,
            },
            child: Some(child),
            owns_host: true,
            stopped: false,
            release_identity: test_release_identity(),
        };
        (state, request, server)
    }

    #[cfg(unix)]
    #[test]
    fn system_exit_timeout_terminates_only_the_owned_child() {
        let directory = tempfile::tempdir().unwrap();
        let (mut state, request, server) = stubborn_host_state(directory.path());

        let started = Instant::now();
        let result = state.stop_host_on_exit();
        request.recv_timeout(Duration::from_secs(2)).unwrap();
        server.join().unwrap();
        let child_reaped = state.child.is_none();
        let stopped = state.stopped;
        if let Some(mut remaining) = state.child.take() {
            let _ = remaining.kill();
            let _ = remaining.wait();
        }
        println!("SYSTEM_EXIT_RESULT={result:?}");
        println!("SYSTEM_EXIT_ELAPSED_MS={}", started.elapsed().as_millis());
        println!("OWNED_CHILD_REAPED={child_reaped}");
        assert!(child_reaped, "系统退出超时后 owned child 仍存活");
        assert!(result.is_ok(), "{result:?}");
        assert!(stopped);
    }

    #[cfg(unix)]
    #[test]
    fn ordinary_quit_timeout_keeps_the_owned_child_alive() {
        let directory = tempfile::tempdir().unwrap();
        let (mut state, request, server) = stubborn_host_state(directory.path());

        let result = state.stop_host();
        request.recv_timeout(Duration::from_secs(2)).unwrap();
        server.join().unwrap();
        let mut child = state.child.take().unwrap();
        let child_alive = child.try_wait().unwrap().is_none();
        let _ = child.kill();
        let _ = child.wait();

        assert!(result.is_err_and(|error| error.contains("进程仍保留")));
        assert!(child_alive, "普通退出超时错误地终止了仍活着的 child");
        assert!(!state.stopped);
    }

    #[cfg(unix)]
    #[test]
    fn system_exit_timeout_preserves_an_attached_process() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let mut attached = Command::new("/bin/cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let (port, request, server) = verified_shutdown_server_removing(Vec::new(), attached.id());
        let metadata = format!(
            r#"{{"pid":{},"port":{port},"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{}}}"#,
            attached.id(),
            test_release_payload()
        );
        fs::write(&host_lock, metadata.as_bytes()).unwrap();
        let mut state = RuntimeState {
            python: root.join("fake-python.sh"),
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: attached.id(),
                port,
                token: "secret_secret_secret".to_string(),
                started_at: 1,
                release: Some(test_host_release_identity()),
            },
            child: None,
            owns_host: false,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host_on_exit();
        for _ in 0..3 {
            request.recv_timeout(Duration::from_secs(2)).unwrap();
        }
        server.join().unwrap();
        let child_alive = attached.try_wait().unwrap().is_none();
        let _ = attached.kill();
        let _ = attached.wait();

        assert!(result.is_err_and(|error| error.contains("服务仍保留")));
        assert!(child_alive, "系统退出超时误杀了 attached 进程");
        assert_eq!(fs::read(&host_lock).unwrap(), metadata.as_bytes());
        assert!(!state.stopped);
    }

    #[cfg(unix)]
    #[test]
    fn system_exit_refuses_a_child_endpoint_identity_mismatch() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let child = Command::new("/bin/cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let mut state = RuntimeState {
            python: root.join("unused-python"),
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: child.id().saturating_add(1),
                port: 1,
                token: "secret_secret_secret".to_string(),
                started_at: 1,
                release: None,
            },
            child: Some(child),
            owns_host: true,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host_on_exit();
        let mut child = state.child.take().unwrap();
        let child_alive = child.try_wait().unwrap().is_none();
        let _ = child.kill();
        let _ = child.wait();

        assert!(result.is_err_and(|error| error.contains("身份不符")));
        assert!(child_alive);
        assert!(!state.stopped);
    }

    #[cfg(unix)]
    #[test]
    fn system_exit_terminates_old_child_but_preserves_takeover() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let (mut state, request, server) = stubborn_host_state(root);
        let mut replacement = Command::new("/bin/cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let metadata = format!(
            r#"{{"pid":{},"port":59124,"token":"replacement_token","started_at":2,"ui_dir":""}}"#,
            replacement.id(),
        );
        fs::write(&host_lock, metadata.as_bytes()).unwrap();
        // The Python identity guard reports that a replacement Host exists.
        // Keep that guarded result instead of escalating to a PID-based kill.
        write_executable(&state.python, "#!/bin/sh\nexit 3\n");

        let result = state.stop_host_on_exit();
        request.recv_timeout(Duration::from_secs(2)).unwrap();
        server.join().unwrap();
        let old_child_reaped = state.child.is_none();
        let replacement_alive = replacement.try_wait().unwrap().is_none();
        if let Some(mut remaining) = state.child.take() {
            let _ = remaining.kill();
            let _ = remaining.wait();
        }
        let _ = replacement.kill();
        let _ = replacement.wait();

        assert!(old_child_reaped);
        assert!(result.is_err_and(|error| error.contains("已接管")));
        assert!(replacement_alive, "系统退出误杀了新 Host 实例");
        assert_eq!(fs::read(&host_lock).unwrap(), metadata.as_bytes());
    }

    #[cfg(unix)]
    #[test]
    fn attached_host_exit_requests_shutdown_and_clears_service_files() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let daemon_lock = run_dir.join("daemon.lock");
        let daemon_snapshot = br#"{"pid":42,"started_at":1,"instance_id":"daemon_attached"}"#;
        fs::write(&daemon_lock, daemon_snapshot).unwrap();
        let (port, request, server) =
            verified_shutdown_server_removing(vec![host_lock.clone(), daemon_lock.clone()], 41);
        let endpoint = HostEndpoint {
            pid: 41,
            port,
            token: "secret_secret_secret".to_string(),
            started_at: 1,
            release: Some(test_host_release_identity()),
        };
        fs::write(
            &host_lock,
            format!(
                r#"{{"pid":41,"port":{port},"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{}}}"#,
                test_release_payload()
            ),
        )
        .unwrap();
        let mut state = RuntimeState {
            python: root.join("fake-python.sh"),
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint,
            child: None,
            owns_host: false,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host();
        let requests: Vec<_> = (0..3)
            .map(|_| request.recv_timeout(Duration::from_secs(2)).unwrap())
            .collect();
        server.join().unwrap();

        assert!(result.is_ok(), "{result:?}");
        assert!(requests[0].starts_with("GET /health HTTP/1.1\r\n"));
        assert!(requests[1].starts_with("GET /session HTTP/1.1\r\n"));
        assert!(requests[2].starts_with("POST /shutdown HTTP/1.1\r\n"));
        assert!(requests[2].contains("X-Tianshu-Token: secret_secret_secret\r\n"));
        assert!(!host_lock.exists(), "附着 Host 退出后遗留了 host.lock");
        assert!(!daemon_lock.exists(), "附着 Host 退出后遗留了 daemon.lock");
    }

    #[cfg(unix)]
    #[test]
    fn attached_host_exit_bounds_daemon_cleanup_after_host_ack() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let daemon_lock = run_dir.join("daemon.lock");
        fs::write(
            &daemon_lock,
            br#"{"pid":42,"started_at":1,"instance_id":"daemon_attached"}"#,
        )
        .unwrap();
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nexit 2\n",
                shell_quote(&daemon_lock),
            ),
        );
        let (port, request, server) =
            verified_shutdown_server_removing(vec![host_lock.clone()], 41);
        fs::write(
            &host_lock,
            format!(
                r#"{{"pid":41,"port":{port},"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{}}}"#,
                test_release_payload()
            ),
        )
        .unwrap();
        let mut state = RuntimeState {
            python,
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: 41,
                port,
                token: "secret_secret_secret".to_string(),
                started_at: 1,
                release: Some(test_host_release_identity()),
            },
            child: None,
            owns_host: false,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host();
        for _ in 0..3 {
            request.recv_timeout(Duration::from_secs(2)).unwrap();
        }
        server.join().unwrap();

        assert!(result.is_ok(), "{result:?}");
        assert!(!host_lock.exists());
        assert!(!daemon_lock.exists(), "ACK 后 Host 退出时遗留了 Daemon");
        assert!(state.stopped);
    }

    #[cfg(unix)]
    #[test]
    fn attached_host_crash_then_quit_uses_dead_host_fallback() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let daemon_lock = run_dir.join("daemon.lock");
        fs::write(
            &daemon_lock,
            br#"{"pid":42,"started_at":1,"instance_id":"daemon_attached"}"#,
        )
        .unwrap();
        fs::write(
            &host_lock,
            r#"{"pid":999999,"port":59123,"token":"secret_secret_secret","started_at":7,"ui_dir":""}"#,
        )
        .unwrap();
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ] && [ \"$6\" = \"999999\" ] && [ \"$7\" = \"7\" ]; then\n  rm -f {} {}\n  exit 0\nfi\nexit 7\n",
                shell_quote(&host_lock),
                shell_quote(&daemon_lock),
            ),
        );
        let mut state = RuntimeState {
            python,
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: 999_999,
                port: 59_123,
                token: "secret_secret_secret".to_string(),
                started_at: 7,
                release: None,
            },
            child: None,
            owns_host: false,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host();

        assert!(result.is_ok(), "{result:?}");
        assert!(state.stopped);
        assert!(!host_lock.exists());
        assert!(!daemon_lock.exists());
    }

    #[cfg(unix)]
    #[test]
    fn attached_host_exit_preserves_takeover_published_after_lock_gap() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let daemon_lock = run_dir.join("daemon.lock");
        let daemon_snapshot = br#"{"pid":42,"started_at":1,"instance_id":"daemon_attached"}"#;
        fs::write(&daemon_lock, daemon_snapshot).unwrap();
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nexit 2\n",
                shell_quote(&daemon_lock),
            ),
        );

        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let host_lock_for_server = host_lock.clone();
        let server = thread::spawn(move || {
            for index in 0..3 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut raw = Vec::new();
                loop {
                    let mut chunk = [0_u8; 1024];
                    let count = stream.read(&mut chunk).unwrap();
                    raw.extend_from_slice(&chunk[..count]);
                    if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                let body = match index {
                    0 => r#"{"ok":true,"pid":41}"#.to_string(),
                    1 => format!(
                        r#"{{"ok":true,"pid":41,"release":{}}}"#,
                        test_release_payload()
                    ),
                    _ => r#"{"ok":true,"shutting_down":true}"#.to_string(),
                };
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).unwrap();
                if index == 2 {
                    drop(stream);
                    fs::remove_file(&host_lock_for_server).unwrap();
                    thread::sleep(Duration::from_millis(60));
                    fs::write(
                        &host_lock_for_server,
                        format!(
                            r#"{{"pid":43,"port":{port},"token":"takeover_token_123","started_at":2,"ui_dir":""}}"#
                        ),
                    )
                    .unwrap();
                }
            }
        });
        fs::write(
            &host_lock,
            format!(
                r#"{{"pid":41,"port":{port},"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{}}}"#,
                test_release_payload()
            ),
        )
        .unwrap();
        let mut state = RuntimeState {
            python,
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: 41,
                port,
                token: "secret_secret_secret".to_string(),
                started_at: 1,
                release: Some(test_host_release_identity()),
            },
            child: None,
            owns_host: false,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host();
        server.join().unwrap();

        assert!(
            result
                .as_ref()
                .is_err_and(|error| error.contains("实例已变更")),
            "{result:?}"
        );
        assert_eq!(fs::read(&daemon_lock).unwrap(), daemon_snapshot);
        let replacement = fs::read(&host_lock).unwrap();
        assert!(replacement.windows(3).any(|window| window == b"43,"));
        assert!(!state.stopped);
    }

    #[cfg(unix)]
    #[test]
    fn owned_host_exit_after_ack_still_stops_its_daemon() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let daemon_lock = run_dir.join("daemon.lock");
        fs::write(&daemon_lock, br#"{"pid":41}"#).unwrap();
        let release = root.join("host-exit");
        let host = root.join("fake-host.sh");
        write_executable(
            &host,
            &format!(
                "#!/bin/sh\nwhile [ ! -e {} ]; do sleep 0.01; done\nprintf '%s' '{{\"pid\":41}}' > {}\nexit 0\n",
                shell_quote(&release),
                shell_quote(&daemon_lock),
            ),
        );
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  rm -f \"$4/run/daemon.lock\"\n  exit 0\nfi\nexit 2\n",
        );
        let child = Command::new(&host).stdout(Stdio::null()).spawn().unwrap();
        let (port, acknowledged, server) = shutdown_server(release.clone());
        let mut state = RuntimeState {
            python,
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: child.id(),
                port,
                token: "secret_secret_secret".to_string(),
                started_at: 1,
                release: None,
            },
            child: Some(child),
            owns_host: true,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host();
        acknowledged.recv_timeout(Duration::from_secs(2)).unwrap();
        server.join().unwrap();

        assert!(result.is_ok(), "{result:?}");
        assert!(!daemon_lock.exists(), "ACK 后 Host 退出时遗留了 Daemon 锁");
        assert!(state.stopped);
        assert!(state.child.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn owned_host_crash_cleanup_binds_exact_stale_instance() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let host_lock = run_dir.join("host.lock");
        let daemon_lock = run_dir.join("daemon.lock");
        fs::write(
            &daemon_lock,
            br#"{"pid":42,"started_at":1,"instance_id":"daemon_owned"}"#,
        )
        .unwrap();
        let mut child = Command::new("/bin/sh")
            .args(["-c", "exit 0"])
            .spawn()
            .unwrap();
        let host_pid = child.id();
        let exited = Instant::now();
        while child.try_wait().unwrap().is_none() {
            assert!(
                exited.elapsed() < Duration::from_secs(2),
                "fake Host did not exit"
            );
            thread::sleep(Duration::from_millis(10));
        }
        fs::write(
            &host_lock,
            format!(
                r#"{{"pid":{host_pid},"port":59123,"token":"secret_secret_secret","started_at":7,"ui_dir":""}}"#
            ),
        )
        .unwrap();
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ] && [ \"$6\" = \"{host_pid}\" ] && [ \"$7\" = \"7\" ]; then\n  rm -f {} {}\n  exit 0\nfi\nexit 7\n",
                shell_quote(&host_lock),
                shell_quote(&daemon_lock),
            ),
        );
        let mut state = RuntimeState {
            python,
            app_dir: root.join("app"),
            data_root: root.to_path_buf(),
            endpoint: HostEndpoint {
                pid: host_pid,
                port: 59123,
                token: "secret_secret_secret".to_string(),
                started_at: 7,
                release: None,
            },
            child: Some(child),
            owns_host: true,
            stopped: false,
            release_identity: test_release_identity(),
        };

        let result = state.stop_host();

        assert!(result.is_ok(), "{result:?}");
        assert!(state.stopped);
        assert!(!host_lock.exists());
        assert!(!daemon_lock.exists());
    }

    #[cfg(unix)]
    #[test]
    fn live_host_without_release_identity_is_rejected_before_spawn() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let host_pid = std::process::id();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut raw = Vec::new();
            loop {
                let mut chunk = [0_u8; 1024];
                let count = stream.read(&mut chunk).unwrap();
                raw.extend_from_slice(&chunk[..count]);
                if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let body = format!(r#"{{"ok":true,"pid":{host_pid}}}"#);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        fs::write(
            run_dir.join("host.lock"),
            format!(
                r#"{{"pid":{host_pid},"port":{port},"token":"secret_secret_secret","started_at":1,"ui_dir":""}}"#
            ),
        )
        .unwrap();
        let spawned = root.join("spawned");
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!("#!/bin/sh\ntouch {}\nexit 1\n", shell_quote(&spawned)),
        );
        let paths = StartupPaths {
            python,
            app_dir: root.join("app"),
            ui_dir: root.join("ui"),
            data_root: root.to_path_buf(),
            release_identity: ReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            },
        };

        let error = ensure_host(&paths).unwrap_err();
        server.join().unwrap();

        assert!(error.contains("缺少发行身份"), "{error}");
        assert!(error.contains("退出旧实例"), "{error}");
        assert!(error.contains(&format!("PID={host_pid}")), "{error}");
        assert!(error.contains(&root.display().to_string()), "{error}");
        assert!(!error.contains("secret_secret_secret"), "{error}");
        assert!(
            !spawned.exists(),
            "已确认的旧 Host 被附着后仍启动了第二个 Host"
        );
    }

    #[cfg(unix)]
    #[test]
    fn live_session_without_release_identity_is_rejected_before_spawn() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let host_pid = std::process::id();
        let server = thread::spawn(move || {
            for body in [
                format!(r#"{{"ok":true,"pid":{host_pid}}}"#),
                format!(r#"{{"ok":true,"pid":{host_pid}}}"#),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut raw = Vec::new();
                loop {
                    let mut chunk = [0_u8; 1024];
                    let count = stream.read(&mut chunk).unwrap();
                    raw.extend_from_slice(&chunk[..count]);
                    if raw.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        fs::write(
            run_dir.join("host.lock"),
            format!(
                r#"{{"pid":{host_pid},"port":{port},"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{}}}"#,
                test_release_payload()
            ),
        )
        .unwrap();
        let spawned = root.join("spawned");
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!("#!/bin/sh\ntouch {}\nexit 1\n", shell_quote(&spawned)),
        );
        let paths = StartupPaths {
            python,
            app_dir: root.join("app"),
            ui_dir: root.join("ui"),
            data_root: root.to_path_buf(),
            release_identity: test_release_identity(),
        };

        let error = ensure_host(&paths).unwrap_err();
        server.join().unwrap();

        assert!(error.contains("/session 缺少发行身份"), "{error}");
        assert!(error.contains(&format!("PID={host_pid}")), "{error}");
        assert!(error.contains(&root.display().to_string()), "{error}");
        assert!(!error.contains("secret_secret_secret"), "{error}");
        assert!(
            !spawned.exists(),
            "已确认的旧 Host session 缺身份时仍启动了第二个 Host"
        );
    }

    #[cfg(unix)]
    #[test]
    fn startup_failure_cleans_daemon_created_by_the_new_host() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        let daemon_lock = run_dir.join("daemon.lock");
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nmkdir -p {}\nprintf '%s' '{{\"pid\":41}}' > {}\nprintf '%s' 'invalid' > {}\nexec sleep 30\n",
                shell_quote(&daemon_lock),
                shell_quote(&run_dir),
                shell_quote(&daemon_lock),
                shell_quote(&run_dir.join("host.lock")),
            ),
        );
        let paths = StartupPaths {
            python,
            app_dir: root.join("app"),
            ui_dir: root.join("ui"),
            data_root: root.to_path_buf(),
            release_identity: ReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            },
        };

        assert!(ensure_host(&paths).is_err());
        assert!(
            !daemon_lock.exists(),
            "启动失败后新 Host 拉起的 Daemon 未被清理"
        );
    }

    #[cfg(unix)]
    #[test]
    fn startup_failure_preserves_preexisting_daemon_lock() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        fs::create_dir_all(&run_dir).unwrap();
        let daemon_lock = run_dir.join("daemon.lock");
        let original = br#"{"pid":77,"instance_id":"daemon_existing"}"#;
        fs::write(&daemon_lock, original).unwrap();
        let cleaned = root.join("cleanup-called");
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  touch {}\n  exit 0\nfi\nmkdir -p {}\nprintf '%s' 'invalid' > {}\nexec sleep 30\n",
                shell_quote(&cleaned),
                shell_quote(&run_dir),
                shell_quote(&run_dir.join("host.lock")),
            ),
        );
        let paths = StartupPaths {
            python,
            app_dir: root.join("app"),
            ui_dir: root.join("ui"),
            data_root: root.to_path_buf(),
            release_identity: ReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            },
        };

        assert!(ensure_host(&paths).is_err());
        assert_eq!(fs::read(&daemon_lock).unwrap(), original);
        assert!(!cleaned.exists(), "既有 Daemon 不应被本次启动失败误杀");
    }

    #[cfg(unix)]
    #[test]
    fn exited_startup_child_fails_without_waiting_for_readiness_timeout() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        let daemon_lock = run_dir.join("daemon.lock");
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nmkdir -p {}\nprintf '%s' '{{\"pid\":41}}' > {}\nexit 1\n",
                shell_quote(&daemon_lock),
                shell_quote(&run_dir),
                shell_quote(&daemon_lock),
            ),
        );
        let paths = StartupPaths {
            python,
            app_dir: root.join("app"),
            ui_dir: root.join("ui"),
            data_root: root.to_path_buf(),
            release_identity: ReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            },
        };
        let started = Instant::now();

        assert!(ensure_host(&paths).is_err());
        let elapsed = started.elapsed();

        assert!(
            elapsed < Duration::from_secs(2),
            "子 Host 已退出仍阻塞到就绪超时：{elapsed:?}"
        );
        assert!(!daemon_lock.exists());
    }

    #[cfg(unix)]
    #[test]
    fn startup_failure_cleans_a_daemon_that_publishes_its_lock_late() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let run_dir = root.join("run");
        let daemon_lock = run_dir.join("daemon.lock");
        let delayed = root.join("daemon-wrote-lock");
        let python = root.join("fake-python.sh");
        write_executable(
            &python,
            &format!(
                "#!/bin/sh\nif [ \"$2\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nmkdir -p {}\n( sleep 0.5; printf '%s' '{{\"pid\":41}}' > {}; touch {} ) &\nprintf '%s' 'invalid' > {}\nexec sleep 30\n",
                shell_quote(&daemon_lock),
                shell_quote(&run_dir),
                shell_quote(&daemon_lock),
                shell_quote(&delayed),
                shell_quote(&run_dir.join("host.lock")),
            ),
        );
        let paths = StartupPaths {
            python,
            app_dir: root.join("app"),
            ui_dir: root.join("ui"),
            data_root: root.to_path_buf(),
            release_identity: ReleaseIdentity {
                version: "0.1.0".to_string(),
                commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
                target: "macos-arm64".to_string(),
            },
        };

        assert!(ensure_host(&paths).is_err());
        let deadline = Instant::now() + Duration::from_secs(2);
        while !delayed.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(delayed.exists(), "延迟 Daemon fixture 没有发布锁");
        assert!(
            !daemon_lock.exists(),
            "启动失败时漏掉了稍后才发布锁的新 Daemon"
        );
    }
}
