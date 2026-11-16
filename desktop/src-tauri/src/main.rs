#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    if arguments.next().as_deref() == Some(std::ffi::OsStr::new("--verify-bundle")) {
        let Some(resource_dir) = arguments.next().map(std::path::PathBuf::from) else {
            eprintln!("--verify-bundle 需要资源目录");
            std::process::exit(2);
        };
        if arguments.next().is_some() {
            eprintln!("--verify-bundle 不接受额外参数");
            std::process::exit(2);
        }
        if let Err(error) = tianshu_desktop::verify_bundle(&resource_dir) {
            eprintln!("桌面资源清单校验失败：{error}");
            std::process::exit(1);
        }
        return;
    }
    tianshu_desktop::run();
}
