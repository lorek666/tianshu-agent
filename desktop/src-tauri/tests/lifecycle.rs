use std::path::{Path, PathBuf};
use std::time::Duration;

use tianshu_desktop::lifecycle::{
    DefaultDataRootInput, HostEndpoint, HostInfoError, build_host_command, default_data_root,
    parse_host_endpoint, wait_for_host,
};

fn windows_env() -> DefaultDataRootInput {
    DefaultDataRootInput {
        home: PathBuf::from(r"C:\Users\alice"),
        local_app_data: Some(PathBuf::from(r"C:\Users\alice\AppData\Local")),
    }
}

fn mac_env() -> DefaultDataRootInput {
    DefaultDataRootInput {
        home: PathBuf::from("/Users/alice"),
        local_app_data: None,
    }
}

#[test]
fn data_root_matches_python_host_on_macos() {
    assert_eq!(
        default_data_root("macos", &mac_env()).unwrap(),
        PathBuf::from("/Users/alice/Library/Application Support/AgentOS")
    );
}

#[test]
fn data_root_matches_python_host_on_windows() {
    let local_app_data = windows_env().local_app_data.unwrap();
    let root = default_data_root("windows", &windows_env()).unwrap();
    assert!(root.starts_with(local_app_data));
    assert_eq!(root.file_name(), Some(std::ffi::OsStr::new("AgentOS")));
}

#[test]
fn windows_requires_local_app_data() {
    let mut env = windows_env();
    env.local_app_data = None;
    assert_eq!(
        default_data_root("windows", &env).unwrap_err(),
        HostInfoError::MissingLocalAppData
    );
}

#[test]
fn host_endpoint_accepts_real_metadata_without_exposing_token() {
    let raw = br#"{"pid":42,"port":59123,"token":"secret","started_at":1,"ui_dir":""}"#;

    let endpoint = parse_host_endpoint(raw).unwrap();

    assert_eq!(
        endpoint,
        HostEndpoint {
            pid: 42,
            port: 59123
        }
    );
    assert_eq!(endpoint.url(), "http://127.0.0.1:59123/");
}

#[test]
fn host_endpoint_rejects_bad_port_and_missing_token() {
    let not_ready = br#"{"pid":42,"port":0,"token":"secret"}"#;
    let missing_token = br#"{"pid":42,"port":59123}"#;

    assert_eq!(
        parse_host_endpoint(not_ready).unwrap_err(),
        HostInfoError::NotReady
    );
    assert_eq!(
        parse_host_endpoint(missing_token).unwrap_err(),
        HostInfoError::InvalidEndpoint
    );
}

#[test]
fn host_command_passes_paths_as_argv_and_keeps_token_out() {
    let python = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/python/bin/python3");
    let app = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/app");
    let ui = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/ui/dist");
    let data = Path::new("/Users/alice/Library/Application Support/AgentOS");

    let command = build_host_command(python, app, ui, data);

    assert_eq!(command.get_program(), python);
    let args: Vec<_> = command.get_args().collect();
    assert_eq!(
        args,
        [
            "-m",
            "tianshu",
            "host",
            "run",
            "--port",
            "0",
            "--ui-dir",
            ui.to_str().unwrap(),
            "--data-root",
            data.to_str().unwrap(),
        ]
    );
    assert!(
        command
            .get_envs()
            .any(|(name, value)| { name == "PYTHONPATH" && value == Some(app.as_os_str()) })
    );
    let rendered = format!("{command:?}");
    assert!(!rendered.contains("secret"));
    assert!(!rendered.contains("X-Tianshu-Token"));
}

#[test]
fn readiness_wait_does_not_treat_existing_file_as_ready() {
    let mut reads = 0;
    let endpoint = wait_for_host(
        Duration::from_millis(50),
        Duration::from_millis(10),
        || {
            reads += 1;
            if reads < 3 {
                Err(HostInfoError::NotReady)
            } else {
                Ok(HostEndpoint {
                    pid: 42,
                    port: 59123,
                })
            }
        },
        |_| {},
    )
    .unwrap();

    assert_eq!(reads, 3);
    assert_eq!(endpoint.port, 59123);
}
