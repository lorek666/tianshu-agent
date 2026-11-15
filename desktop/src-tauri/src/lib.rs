pub mod lifecycle;
pub mod release;

#[cfg(debug_assertions)]
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuBuilder, MenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent, WindowEvent};

use crate::lifecycle::{
    DefaultDataRootInput, HostEndpoint, HostInfoError, build_host_command,
    build_orphan_daemon_shutdown_command, default_data_root, parse_host_endpoint,
    request_host_shutdown, verify_host, wait_for_host,
};
use crate::release::{ReleaseIdentity, current_target, verify_resource_manifest};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const STARTUP_POLL: Duration = Duration::from_millis(100);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
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
}

impl RuntimeState {
    fn finish_owned_host_exit(&mut self) -> Result<(), String> {
        self.child = None;
        let result = stop_orphan_daemon(&self.python, &self.app_dir, &self.data_root);
        if result.is_ok() {
            self.owns_host = false;
        }
        result
    }

    fn stop_host(&mut self) -> Result<(), String> {
        if !self.owns_host {
            return Ok(());
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
}

struct StartupPaths {
    python: PathBuf,
    app_dir: PathBuf,
    ui_dir: PathBuf,
    data_root: PathBuf,
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

fn stop_orphan_daemon(python: &Path, app_dir: &Path, data_root: &Path) -> Result<(), String> {
    let daemon_lock = daemon_lock_path(data_root);
    if !daemon_lock.exists() {
        return Ok(());
    }
    let status = build_orphan_daemon_shutdown_command(python, app_dir, data_root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("请求孤儿 Daemon 退出失败：{error}"))?;
    if !status.success() {
        return Err(format!("孤儿 Daemon 退出请求返回失败：{status}"));
    }
    let started = Instant::now();
    while started.elapsed() < SHUTDOWN_TIMEOUT {
        if !daemon_lock.exists() {
            return Ok(());
        }
        thread::sleep(STARTUP_POLL);
    }
    Err("Daemon 未在超时内退出，拒绝留下孤儿进程".to_string())
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
            return stop_orphan_daemon(&paths.python, &paths.app_dir, &paths.data_root);
        }
        if started.elapsed() >= FAILED_HOST_DAEMON_SETTLE_TIMEOUT {
            return Ok(());
        }
        thread::sleep(STARTUP_POLL);
    }
}

fn ensure_host(paths: &StartupPaths) -> Result<(HostEndpoint, Option<Child>), String> {
    if let Ok(endpoint) = read_host_endpoint(&paths.data_root)
        && verify_host(&endpoint).is_ok()
    {
        return Ok((endpoint, None));
    }

    let daemon_before = read_daemon_lock_snapshot(&paths.data_root)?;
    let mut child = build_host_command(
        &paths.python,
        &paths.app_dir,
        &paths.ui_dir,
        &paths.data_root,
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
            if verify_host(&endpoint).is_err() {
                return Err(HostInfoError::NotReady);
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
            app.manage(Mutex::new(RuntimeState {
                python: paths.python,
                app_dir: paths.app_dir,
                data_root: paths.data_root,
                endpoint,
                child,
                owns_host,
            }));
            window.navigate(url)?;
        }
        Err(error) => {
            eprintln!("天枢桌面启动失败：{error}");
            window.set_title("天枢 Agent · 启动失败")?;
            let detail = serde_json::to_string(&error)?;
            window.eval(format!(
                "document.getElementById('detail').textContent = {detail};"
            ))?;
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
            if let Some(state) = app.try_state::<Mutex<RuntimeState>>()
                && let Ok(mut state) = state.lock()
            {
                let _ = state.stop_host();
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::process::{Command, Stdio};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::*;

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
            "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then\n  rm -f \"$3/run/daemon.lock\"\n  exit 0\nfi\nexit 2\n",
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
            },
            child: Some(child),
            owns_host: true,
        };

        let result = state.stop_host();
        acknowledged.recv_timeout(Duration::from_secs(2)).unwrap();
        server.join().unwrap();

        assert!(result.is_ok(), "{result:?}");
        assert!(!daemon_lock.exists(), "ACK 后 Host 退出时遗留了 Daemon 锁");
        assert!(!state.owns_host);
        assert!(state.child.is_none());
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
                "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nmkdir -p {}\nprintf '%s' '{{\"pid\":41}}' > {}\nprintf '%s' 'invalid' > {}\nexec sleep 30\n",
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
                "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then\n  touch {}\n  exit 0\nfi\nmkdir -p {}\nprintf '%s' 'invalid' > {}\nexec sleep 30\n",
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
                "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nmkdir -p {}\nprintf '%s' '{{\"pid\":41}}' > {}\nexit 1\n",
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
                "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then\n  rm -f {}\n  exit 0\nfi\nmkdir -p {}\n( sleep 0.5; printf '%s' '{{\"pid\":41}}' > {}; touch {} ) &\nprintf '%s' 'invalid' > {}\nexec sleep 30\n",
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
