# Tianshu Agent R1 桌面壳候选

> 状态：**R1 源码候选，尚未独立复审或合入。** 这里只有 Tauri 2 薄壳、Python Host
> 生命周期与资源指纹校验。R2 的锁定 Python runtime/构建脚本、R3 的安装器和 release
> workflow 不在 R1，文档不能把它们写成已交付能力。

壳负责窗口、托盘、单实例和 Python Host/Daemon 生命周期。任务、审批、事件、工具执行、
授权和恢复仍由现有 Python Host/Daemon 实现；Rust 没有替换 Host，也没有形成新的授权
边界。

窗口关闭只隐藏，托盘菜单的显式退出才请求 Host 停止服务。自有 Host 与附着 Host
使用不同的退出协调路径；停止 Daemon 前会复核 Host 元数据和退出前 Daemon 锁摘要，
新 Host 或新 Daemon 实例已经接管时保留现场。Host 意外退出时有有界恢复，不会无限重试。

## R1 资源契约

R1 启动器读取以下资源根：

```text
<resource-root>/
  python/bin/python3              macOS 下的 Python 3.12 路径
  python/python.exe               Windows 下的 Python 3.12 路径
  app/tianshu/                    Python 包目录
  ui/dist/index.html              已构建 UI 入口
  release.json                    封闭的版本/提交/目标身份
  release-manifest.json           完整文件集合与 SHA256
```

`release.json` 必须是 R0 schema v1 的 `tianshu-agent` 身份；`release-manifest.json`
必须绑定相同的 version/commit/target，列出每个资源文件，且 UI 哈希必须对应
`ui/dist/index.html`。原生启动器还会：

- 拒绝清单外的文件、缺失文件和字节不符；
- 拒绝 canonicalize 后逃出资源根的符号链接；
- 拒绝原始路径或 canonical 路径中的 `.git`、`.omc`、`.tianshu`、`.venv`、
  `logs`、`refs`、`tests`、`__pycache__`；
- 拒绝 `.db/-wal/-shm/-journal`、`.sqlite*`、`.log`、`.key` 与 `.env*` 文件。

Host 入口以 argv 启动，使用 `python -P -m tianshu host run ...`。`-P` 防止调用者
当前目录里的同名 `tianshu/` 包遮蔽资源目录；三个 Python 子入口都设置了
`PYTHONDONTWRITEBYTECODE=1`，并固定 `PYTHONIOENCODING=utf-8`。token 只从 Host
元数据和鉴权 HTTP 路径读取，不写入 argv、URL、日志或持久配置。

仅 debug build 可在显式设置 `TIANSHU_DESKTOP_ALLOW_RESOURCE_OVERRIDE=1` 后，用
`TIANSHU_DESKTOP_RESOURCE_DIR`、`TIANSHU_DESKTOP_RELEASE_FILE`、
`TIANSHU_DESKTOP_PYTHON`、`TIANSHU_DESKTOP_APP_DIR`、
`TIANSHU_DESKTOP_UI_DIR` 指向受控夹具。资源根仍须通过完整的
`release.json` / manifest 校验；release 文件、Python、包目录和 UI 目录的独立
覆盖只用于开发测试。release build 会忽略全部覆盖环境变量，只能使用 Tauri
发布的规范资源目录。

## R1 明确不包含

- 下载、锁定或校验 CPython 3.12 runtime；
- `build_release.py` / `package_app.py` / `verify_release.py`；
- macOS 安装/卸载/回退脚本与 Windows NSIS；
- 四目标 GitHub Release workflow、资产汇总与 provenance；
- OS 代码签名、公证、自动更新和真实用户设备升级/回退。

这些属于 R2/R3，必须在各自 PR、当前 SHA 的独立复审和 CI 通过后再更新状态。

## 本机检查

```bash
cd desktop/src-tauri
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
```

macOS 当前测试会额外覆盖真实 Unix symlink 指向资源根内禁止文件的负控；
现有 required checks 在 macOS job 内运行 Rust fmt/test/clippy，并通过原 Windows
required 汇总 job 绑定一个原生 Windows Rust fmt/test/clippy job，因此四个 required
check 名称不变而桌面壳不再是“只在本机测”。Windows runner 覆盖大小写不敏感的禁止
路径、argv、HTTP 鉴权和资源身份测试，但不能替代 Windows 10/11 或 ARM64 用户设备实测。

## 待绑定同一 SHA 的手工证据

以下项目在 R1 正式候选上仍是 `NOT_RUN`，不得沿用前驱提交的截图或结论：

1. 从 R1 资源夹具启动真实 WebView，确认页面来自 Host，显示“已连接”。
2. 关闭窗口后确认壳、Host、Daemon 继续运行，再重新打开同一窗口。
3. 分别验证自有 Host、预先启动的附着 Host、显式退出和 Host 意外退出恢复。
4. 在旧 Host 退出到新 Host 发布锁之间复跑 takeover 竞态，确认不误杀新实例。
5. 触发系统退出组件超时，确认只清理已验证为自有且已退出的 Host；真实整机注销/
   关机必须另记设备证据。

这些证据只有在绑定最终 R1 候选提交时才算数。GitHub runner 绿不等于桌面用户设备实测。
