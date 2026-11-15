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

struct RuntimeState {
    python: PathBuf,
    app_dir: PathBuf,
    data_root: PathBuf,
    endpoint: HostEndpoint,
    child: Option<Child>,
    owns_host: bool,
}

impl RuntimeState {
    fn stop_host(&mut self) -> Result<(), String> {
        if !self.owns_host {
            return Ok(());
        }
        if let Some(child) = self.child.as_mut()
            && let Ok(Some(_)) = child.try_wait()
        {
            self.child = None;
            self.owns_host = false;
            return stop_orphan_daemon(&self.python, &self.app_dir, &self.data_root);
        }
        if let Err(error) = request_host_shutdown(&self.endpoint) {
            if let Some(child) = self.child.as_mut()
                && let Ok(Some(_)) = child.try_wait()
            {
                self.child = None;
                self.owns_host = false;
                return stop_orphan_daemon(&self.python, &self.app_dir, &self.data_root);
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

fn stop_orphan_daemon(python: &Path, app_dir: &Path, data_root: &Path) -> Result<(), String> {
    let daemon_lock = data_root.join("run").join("daemon.lock");
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

fn ensure_host(paths: &StartupPaths) -> Result<(HostEndpoint, Option<Child>), String> {
    if let Ok(endpoint) = read_host_endpoint(&paths.data_root)
        && verify_host(&endpoint).is_ok()
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
