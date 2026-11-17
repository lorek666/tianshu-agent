use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use tianshu_desktop::lifecycle::{
    DefaultDataRootInput, HostEndpoint, HostInfoError, HostReleaseIdentity, build_host_command,
    build_host_exit_daemon_shutdown_command, build_orphan_daemon_shutdown_command,
    default_data_root, parse_host_endpoint, request_host_shutdown, verify_host,
    verify_host_release, wait_for_host,
};
use tianshu_desktop::release::ReleaseIdentity;

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
            release: None,
        }
    );
    assert_eq!(endpoint.url(), "http://127.0.0.1:59123/");
    assert!(!format!("{endpoint:?}").contains("secret_secret_secret"));
}

#[test]
fn host_release_identity_is_required_and_must_match_exactly() {
    let expected = ReleaseIdentity {
        version: "0.1.0".to_string(),
        commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
        target: "macos-arm64".to_string(),
    };
    let raw = br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{"schema":1,"product":"tianshu-agent","version":"0.1.0","commit":"0123456789abcdef0123456789abcdef01234567","target":"macos-arm64"}}"#;
    let mut endpoint = parse_host_endpoint(raw).unwrap();
    assert_eq!(
        endpoint.release,
        Some(HostReleaseIdentity {
            version: "0.1.0".to_string(),
            commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
            target: "macos-arm64".to_string(),
        })
    );
    verify_host_release(&endpoint, &expected).unwrap();

    endpoint.release = None;
    let missing = verify_host_release(&endpoint, &expected).unwrap_err();
    assert!(missing.contains("缺少发行身份"), "{missing}");

    endpoint.release = Some(HostReleaseIdentity {
        version: "0.1.0".to_string(),
        commit: "1123456789abcdef0123456789abcdef01234567".to_string(),
        target: "macos-arm64".to_string(),
    });
    let mismatch = verify_host_release(&endpoint, &expected).unwrap_err();
    assert!(mismatch.contains("不兼容"), "{mismatch}");
    assert!(mismatch.contains("退出旧实例"), "{mismatch}");

    for (label, actual) in [
        (
            "version",
            HostReleaseIdentity {
                version: "0.2.0".to_string(),
                commit: expected.commit.clone(),
                target: expected.target.clone(),
            },
        ),
        (
            "target",
            HostReleaseIdentity {
                version: expected.version.clone(),
                commit: expected.commit.clone(),
                target: "macos-x64".to_string(),
            },
        ),
    ] {
        endpoint.release = Some(actual);
        let mismatch = verify_host_release(&endpoint, &expected).unwrap_err();
        assert!(mismatch.contains("不兼容"), "{label}: {mismatch}");
    }
}

#[test]
fn host_endpoint_rejects_malformed_release_identity() {
    let missing_product = br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{"schema":1,"version":"0.1.0","commit":"0123456789abcdef0123456789abcdef01234567","target":"macos-arm64"}}"#;
    let wrong_schema = br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{"schema":2,"product":"tianshu-agent","version":"0.1.0","commit":"0123456789abcdef0123456789abcdef01234567","target":"macos-arm64"}}"#;
    let wrong_product = br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{"schema":1,"product":"other-agent","version":"0.1.0","commit":"0123456789abcdef0123456789abcdef01234567","target":"macos-arm64"}}"#;
    let extra = br#"{"pid":42,"port":59123,"token":"secret_secret_secret","started_at":1,"ui_dir":"","release":{"schema":1,"product":"tianshu-agent","version":"0.1.0","commit":"0123456789abcdef0123456789abcdef01234567","target":"macos-arm64","debug":true}}"#;

    assert_eq!(
        parse_host_endpoint(missing_product).unwrap_err(),
        HostInfoError::InvalidEndpoint
    );
    assert_eq!(
        parse_host_endpoint(extra).unwrap_err(),
        HostInfoError::InvalidEndpoint
    );
    assert_eq!(
        parse_host_endpoint(wrong_schema).unwrap_err(),
        HostInfoError::InvalidEndpoint
    );
    assert_eq!(
        parse_host_endpoint(wrong_product).unwrap_err(),
        HostInfoError::InvalidEndpoint
    );
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

    let release = ReleaseIdentity {
        version: "0.1.0".to_string(),
        commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
        target: "macos-arm64".to_string(),
    };
    let command = build_host_command(python, app, ui, data, &release);

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
            "--release-version",
            "0.1.0",
            "--release-commit",
            "0123456789abcdef0123456789abcdef01234567",
            "--release-target",
            "macos-arm64",
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
                    release: None,
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

fn expected_release() -> ReleaseIdentity {
    ReleaseIdentity {
        version: "0.1.0".to_string(),
        commit: "0123456789abcdef0123456789abcdef01234567".to_string(),
        target: "macos-arm64".to_string(),
    }
}

fn expected_host_release() -> HostReleaseIdentity {
    let expected = expected_release();
    HostReleaseIdentity {
        version: expected.version,
        commit: expected.commit,
        target: expected.target,
    }
}

fn release_json(release: &HostReleaseIdentity) -> String {
    format!(
        r#"{{"schema":1,"product":"tianshu-agent","version":"{}","commit":"{}","target":"{}"}}"#,
        release.version, release.commit, release.target
    )
}

fn serve_host_identity(
    session_release: Option<&HostReleaseIdentity>,
) -> (u16, thread::JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let health = r#"{"ok":true,"pid":42}"#.to_string();
    let session = match session_release {
        Some(release) => format!(
            r#"{{"ok":true,"pid":42,"release":{}}}"#,
            release_json(release)
        ),
        None => r#"{"ok":true,"pid":42}"#.to_string(),
    };
    let server = thread::spawn(move || {
        for body in [health, session] {
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
            stream.write_all(response(&body).as_bytes()).unwrap();
        }
    });
    (port, server)
}

#[test]
fn host_identity_and_shutdown_use_authenticated_local_http() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let (sender, receiver) = mpsc::channel();
    let release = expected_host_release();
    let release_body = release_json(&release);
    let server = thread::spawn(move || {
        let session_body = format!(r#"{{"ok":true,"pid":42,"release":{release_body}}}"#);
        for body in [
            r#"{"ok":true,"pid":42}"#.to_string(),
            session_body,
            r#"{"ok":true,"shutting_down":true}"#.to_string(),
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
            stream.write_all(response(&body).as_bytes()).unwrap();
        }
    });
    let endpoint = HostEndpoint {
        pid: 42,
        port,
        token: "secret_secret_secret".to_string(),
        started_at: 1,
        release: Some(release),
    };

    verify_host(
        &endpoint,
        &expected_release(),
        Path::new("/Users/alice/Library/Application Support/AgentOS"),
    )
    .unwrap();
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
fn host_verification_rejects_a_session_without_release_identity() {
    let release = expected_host_release();
    let (port, server) = serve_host_identity(None);
    let data_root = tempfile::tempdir().unwrap();
    let endpoint = HostEndpoint {
        pid: 42,
        port,
        token: "secret_secret_secret".to_string(),
        started_at: 1,
        release: Some(release),
    };

    let error = verify_host(&endpoint, &expected_release(), data_root.path()).unwrap_err();
    server.join().unwrap();

    assert!(error.contains("/session"), "{error}");
    assert!(error.contains("发行身份"), "{error}");
    assert!(error.contains("PID=42"), "{error}");
    assert!(
        error.contains(data_root.path().to_str().unwrap()),
        "{error}"
    );
    assert!(!error.contains("secret_secret_secret"), "{error}");
}

#[test]
fn host_verification_rejects_session_and_lock_identity_drift() {
    let lock = expected_host_release();
    let mut session = lock.clone();
    session.commit = "1123456789abcdef0123456789abcdef01234567".to_string();
    let (port, server) = serve_host_identity(Some(&session));
    let data_root = tempfile::tempdir().unwrap();
    let endpoint = HostEndpoint {
        pid: 42,
        port,
        token: "secret_secret_secret".to_string(),
        started_at: 1,
        release: Some(lock),
    };

    let error = verify_host(&endpoint, &expected_release(), data_root.path()).unwrap_err();
    server.join().unwrap();

    assert!(error.contains("/session"), "{error}");
    assert!(error.contains("不一致"), "{error}");
    assert!(!error.contains("secret_secret_secret"), "{error}");
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
