# Tianshu Agent 桌面壳

这是一个 Tauri 2 薄壳。它管理窗口、托盘和单实例，并在用户显式退出时协调当前
数据根的 Python Host/Daemon；无论 Host 由本壳启动还是由第二实例附着，退出都走
同一套鉴权停止路径。任务、审批、事件、工具执行与恢复仍在现有 Python
Host/Daemon 中。Rust 没有替换 Host，也没有形成新的授权边界。

## 资源布局

发布构建先生成以下 bundle，再由 Tauri 打包：

```text
bundle/
  app/tianshu/                 Python 包
  licenses/LICENSE             Apache 2.0
  python/                      锁定的 CPython 3.12
  ui/dist/                     与 Host 使用的 UI 完全相同的字节
  release.json                 版本、完整 commit、target
  release-manifest.json        每个内部文件的 SHA256
```

`scripts/package_app.py` 把同一 bundle 映射到 app 的
`Contents/Resources/{python,app,ui,release.json,release-manifest.json}`。启动和安装前
壳校验 target、版本、commit、每个文件 SHA 与完整文件集合；未列 Python 文件同样拒绝。
随后以 argv 启动：

```text
python -m tianshu host run --port 0 --ui-dir ... --data-root ...
```

token 由 Host 注入真实 UI，壳不把 token 写入 URL、命令行或持久配置。
bundled Python 的安装烟测、Host、Daemon 与孤儿收尾入口都固定
`PYTHONDONTWRITEBYTECODE=1`；否则首次导入生成的 `__pycache__` 会成为下一次启动时的未列文件。

## 本机检查

```bash
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
```

Rust 测试覆盖 Host 元数据解析、平台数据根、argv 构造和发行身份；真实安装态
生命周期还必须在打包后手工验收：

1. 从临时前缀启动 `.app`，确认真实 WebView 显示“已连接”且 Host `/health`
   返回 200。
2. 关闭窗口，确认壳、Host、Daemon 都继续运行。
3. 重新激活 app，确认同一窗口恢复并仍连接原 Host。
4. 托盘“退出并停止服务”或自定义 `Cmd+Q` 菜单，确认壳、Host、Daemon 全部退出。
5. 杀掉 Host 后再从托盘退出，确认 Daemon 被有界关闭且无孤儿。
6. 卸载默认保留数据根；只有 `--purge-data --confirm-data-root DIR` 才删除测试数据。

## 构建与安装

macOS arm64 本机构建示例：

```bash
uv run --frozen python scripts/build_release.py \
  --target macos-arm64 \
  --version 0.1.0 \
  --commit "$(git rev-parse HEAD)" \
  --out /private/tmp/tianshu-bundle

uv run --frozen python scripts/package_app.py \
  --target macos-arm64 \
  --bundle-dir /private/tmp/tianshu-bundle \
  --out-dir /private/tmp/tianshu-release

uv run --frozen python scripts/verify_release.py \
  --artifact-dir /private/tmp/tianshu-release \
  --bundle-dir /private/tmp/tianshu-bundle \
  --target macos-arm64 \
  --version 0.1.0 \
  --commit "$(git rev-parse HEAD)"
```

安装脚本要求同时提供 archive 与 SHA256 文件：

```bash
sh scripts/install_macos.sh \
  --archive /private/tmp/tianshu-release/Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz \
  --sha256 /private/tmp/tianshu-release/Tianshu-Agent-0.1.0-macos-arm64.sha256 \
  --prefix /private/tmp/tianshu-applications
```

脚本先校验 SHA256、tar 路径与链接，再调用同包原生 verifier 校验完整 manifest，并把
安装前缀解析成真实路径。macOS 上的
Tauri 启动路径检查会拒绝经过符号链接的可执行文件；解析前缀是为了让
`/tmp/...` 这类系统别名也能得到可启动的 app。

卸载：

```bash
sh scripts/uninstall_macos.sh --prefix /private/tmp/tianshu-applications
```

默认只删除程序。测试临时数据时，才可额外传逐字匹配的二次确认：

```bash
sh scripts/uninstall_macos.sh \
  --prefix /private/tmp/tianshu-applications \
  --data-root /private/tmp/tianshu-test-data \
  --purge-data \
  --confirm-data-root /private/tmp/tianshu-test-data
```

## 发行工作流

`.github/workflows/release.yml` 只在手动触发或 `v*` tag 时运行。它要求输入
commit 与 fetch 后的当前 `origin/main` 完全相同（annotated tag 先解引用），
并核验该精确 commit 的现有 CI 四个 required job 全绿。四个 target 在匹配
OS/架构的 runner 上构建；结果作为 draft GitHub Release 上传，必须人工检查后发布。

当前证据边界：

| target | 本轮可以证明 | 仍不能证明 |
|---|---|---|
| macOS arm64 | 本机构建、安装、真实窗口、关闭/重开/退出、卸载 | 其他 macOS 版本与权限状态 |
| macOS x64 | 原生 Intel runner 构建、bundle manifest/字节/Mach-O 架构校验与 runtime smoke | Intel 用户设备安装与升级 |
| Windows x64 | 原生 x64 runner 构建、PE/manifest 校验与 locked Python import smoke | Windows 10/11 用户设备、SmartScreen、安装升级 |
| Windows arm64 | 原生 ARM64 runner 构建、PE/manifest 校验与 locked Python import smoke | Windows ARM64 用户设备 |

CI runner 结果不是用户设备实测。当前产物未签名，也没有 Tauri 自动更新；
G3 的恢复 B/C、SLO、长稳、Keychain 实机与本表设备证据仍保持未完成。
