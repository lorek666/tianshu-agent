use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use tianshu_desktop::lifecycle::{
    DefaultDataRootInput, HostEndpoint, HostInfoError, build_host_command,
    build_host_exit_daemon_shutdown_command, build_orphan_daemon_shutdown_command,
    default_data_root, parse_host_endpoint, request_host_shutdown, verify_host, wait_for_host,
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
    let raw =
        br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":""}"#;

    let endpoint = parse_host_endpoint(raw).unwrap();

    assert_eq!(
        endpoint,
        HostEndpoint {
            pid: 42,
            port: 59123,
            token: "secret_secret_secret".to_string(),
            started_at: 1,
        }
    );
    assert_eq!(endpoint.url(), "http://127.0.0.1:59123/");
    assert!(!format!("{endpoint:?}").contains("secret_secret_secret"));
}

#[test]
fn host_endpoint_rejects_bad_port_and_missing_token() {
    let not_ready =
        br#"{"pid":42,"port":0,"token":"secret_secret_secret","started_at":1,"ui_dir":""}"#;
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
fn host_endpoint_rejects_header_injection_and_extra_fields() {
    let injected =
        br#"{"pid":42,"port":59123,"token":"secret\r\nX-Evil: yes","started_at":1,"ui_dir":""}"#;
    let extra = br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":"","debug":true}"#;

    assert_eq!(
        parse_host_endpoint(injected).unwrap_err(),
        HostInfoError::InvalidEndpoint
    );
    assert_eq!(
        parse_host_endpoint(extra).unwrap_err(),
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
            "-P",
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
    assert!(command.get_envs().any(|(name, value)| {
        name == "PYTHONDONTWRITEBYTECODE" && value == Some(std::ffi::OsStr::new("1"))
    }));
    assert!(command.get_envs().any(|(name, value)| {
        name == "PYTHONIOENCODING" && value == Some(std::ffi::OsStr::new("utf-8"))
    }));
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
                    token: "secret_secret_secret".to_string(),
                    started_at: 1,
                })
            }
        },
        |_| {},
    )
    .unwrap();

    assert_eq!(reads, 3);
    assert_eq!(endpoint.port, 59123);
}

fn response(body: &str) -> String {
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

#[test]
fn host_identity_and_shutdown_use_authenticated_local_http() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let (sender, receiver) = mpsc::channel();
    let server = thread::spawn(move || {
        for body in [
            r#"{"ok":true,"pid":42}"#,
            r#"{"ok":true,"pid":42}"#,
            r#"{"ok":true,"shutting_down":true}"#,
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
            sender
                .send(String::from_utf8(raw).unwrap())
                .expect("request");
            stream.write_all(response(body).as_bytes()).unwrap();
        }
    });
    let endpoint = HostEndpoint {
        pid: 42,
        port,
        token: "secret_secret_secret".to_string(),
        started_at: 1,
    };

    verify_host(&endpoint).unwrap();
    request_host_shutdown(&endpoint).unwrap();
    server.join().unwrap();

    let requests: Vec<_> = receiver.try_iter().collect();
    assert_eq!(requests.len(), 3);
    assert!(requests[0].starts_with("GET /health HTTP/1.1\r\n"));
    assert!(requests[1].starts_with("GET /session HTTP/1.1\r\n"));
    assert!(requests[1].contains("X-Tianshu-Token: secret_secret_secret\r\n"));
    assert!(requests[2].starts_with("POST /shutdown HTTP/1.1\r\n"));
}

#[test]
fn orphan_daemon_shutdown_command_is_constant_and_argv_bound() {
    let python = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/python/bin/python3");
    let app = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/app");
    let data = Path::new("/Users/alice/Library/Application Support/AgentOS");
    let expected_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    let command = build_orphan_daemon_shutdown_command(python, app, data, expected_sha256);

    assert_eq!(command.get_program(), python);
    let args: Vec<_> = command.get_args().collect();
    assert_eq!(args[0], "-P");
    assert_eq!(args[1], "-c");
    assert!(args[2].to_string_lossy().contains("stop_daemon_if_unowned"));
    assert_eq!(Path::new(args[3]), data);
    assert_eq!(args[4], expected_sha256);
    assert!(
        command
            .get_envs()
            .any(|(name, value)| { name == "PYTHONPATH" && value == Some(app.as_os_str()) })
    );
    assert!(command.get_envs().any(|(name, value)| {
        name == "PYTHONDONTWRITEBYTECODE" && value == Some(std::ffi::OsStr::new("1"))
    }));
    assert!(command.get_envs().any(|(name, value)| {
        name == "PYTHONIOENCODING" && value == Some(std::ffi::OsStr::new("utf-8"))
    }));
}

#[test]
fn host_exit_daemon_shutdown_command_binds_stale_host_without_token() {
    let python = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/python/bin/python3");
    let app = Path::new("/Applications/Tianshu Agent.app/Contents/Resources/app");
    let data = Path::new("/Users/alice/Library/Application Support/AgentOS");
    let expected_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    let command =
        build_host_exit_daemon_shutdown_command(python, app, data, Some(expected_sha256), 42, 7);

    assert_eq!(command.get_program(), python);
    let args: Vec<_> = command.get_args().collect();
    assert_eq!(args[0], "-P");
    assert_eq!(args[1], "-c");
    assert!(
        args[2]
            .to_string_lossy()
            .contains("stop_daemon_after_host_exit")
    );
    assert_eq!(Path::new(args[3]), data);
    assert_eq!(args[4], expected_sha256);
    assert_eq!(args[5], "42");
    assert_eq!(args[6], "7");
    assert!(
        !args
            .iter()
            .any(|arg| arg.to_string_lossy().contains("secret"))
    );
    assert!(command.get_envs().any(|(name, value)| {
        name == "PYTHONIOENCODING" && value == Some(std::ffi::OsStr::new("utf-8"))
    }));
}
