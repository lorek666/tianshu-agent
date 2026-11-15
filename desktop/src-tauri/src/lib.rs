pub mod lifecycle;
pub mod release;

use std::ffi::OsStr;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent, WindowEvent};

use crate::lifecycle::{
    DefaultDataRootInput, HostEndpoint, HostInfoError, build_host_command, default_data_root,
    parse_host_endpoint, wait_for_host,
};
use crate::release::{ReleaseIdentity, current_target, verify_resource_manifest};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const STARTUP_POLL: Duration = Duration::from_millis(100);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);

struct RuntimeState {
    python: PathBuf,
    app_dir: PathBuf,
    data_root: PathBuf,
    child: Option<Child>,
    owns_host: bool,
}

impl RuntimeState {
    fn stop_host(&mut self) -> Result<(), String> {
        if !self.owns_host {
            return Ok(());
        }
        let status = stop_host_command(&self.python, &self.app_dir, &self.data_root)
            .status()
            .map_err(|error| format!("调用 host stop 失败：{error}"))?;
        if !status.success() {
            return Err(format!("host stop 返回失败：{status}"));
        }
        let child = self
            .child
            .as_mut()
            .ok_or_else(|| "Host 子进程状态缺失".to_string())?;
        let started = Instant::now();
        while started.elapsed() < SHUTDOWN_TIMEOUT {
            match child.try_wait() {
                Ok(Some(_)) => {
                    self.child = None;
                    self.owns_host = false;
                    return Ok(());
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

fn resource_env_path(name: &str) -> Option<PathBuf> {
    if std::env::var_os("TIANSHU_DESKTOP_ALLOW_RESOURCE_OVERRIDE").as_deref()
        != Some(OsStr::new("1"))
    {
        return None;
    }
    env_path(name)
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

fn host_info_path(data_root: &Path) -> PathBuf {
    data_root.join("run").join("host.lock")
}

fn read_host_endpoint(data_root: &Path) -> Result<HostEndpoint, HostInfoError> {
    let raw = std::fs::read(host_info_path(data_root)).map_err(|_| HostInfoError::NotReady)?;
    parse_host_endpoint(&raw)
}

fn host_alive(endpoint: HostEndpoint) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], endpoint.port));
    TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok()
}

fn stop_host_command(python: &Path, app_dir: &Path, data_root: &Path) -> Command {
    let mut command = Command::new(python);
    command
        .arg("-m")
        .arg("tianshu")
        .arg("host")
        .arg("stop")
        .arg("--data-root")
        .arg(data_root)
        .env("PYTHONPATH", app_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}

fn ensure_host(paths: &StartupPaths) -> Result<(HostEndpoint, Option<Child>), String> {
    if let Ok(endpoint) = read_host_endpoint(&paths.data_root)
        && host_alive(endpoint)
    {
        return Ok((endpoint, None));
    }

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
                return Err(HostInfoError::NotReady);
            }
            let endpoint = read_host_endpoint(&paths.data_root)?;
            if !host_alive(endpoint) {
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
            Err(error)
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
            "quit" => {
                if let Some(state) = app.try_state::<Mutex<RuntimeState>>()
                    && let Ok(mut state) = state.lock()
                    && let Err(error) = state.stop_host()
                {
                    show_quit_error(app, &error);
                    return;
                }
                app.exit(0);
            }
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
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_window(app);
        }))
        .setup(initialise)
        .on_window_event(|window, event| {
            if window.label() == "main"
                && let WindowEvent::CloseRequested { api, .. } = event
            {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build Tianshu desktop app");

    app.run(|app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_window(app),
        RunEvent::ExitRequested { api, .. } => {
            if let Some(state) = app.try_state::<Mutex<RuntimeState>>() {
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
