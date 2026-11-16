# Tianshu Agent 桌面壳与发行候选

> 状态：**R1+R3 源码候选，尚未通过最终组合候选的完整门禁、真实 release workflow
> dispatch 或独立复审。** R2 的锁定 Python runtime、build/verify 入口仍由 R2 提供；
> 本文不把现有源码、局部测试或 CI runner 结果写成用户设备已验证的正式发行版。

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

## R1 本身明确不包含

- 下载、锁定或校验 CPython 3.12 runtime；
- `build_release.py` / `package_app.py` / `verify_release.py`；
- macOS 安装/卸载/回退脚本与 Windows NSIS；
- 四目标 GitHub Release workflow、资产汇总与 provenance；
- OS 代码签名、公证、自动更新和真实用户设备升级/回退。

这些属于 R2/R3，必须在各自 PR、当前 SHA 的独立复审和 CI 通过后再更新状态。

## R3 发行与安装

`.github/workflows/release.yml` 只在手动触发或 `v*` tag 时生成 draft GitHub Release。
它要求手动输入或 annotated tag 解引用后的完整 commit 与 fetch 后的当前 `origin/main`
精确相同，并要求这个精确 SHA 最新一次 main push CI 的全部 job 为 `success`；随后在
四个原生产物 runner 上构建和 runtime smoke，汇总后仍只创建 draft。任何一层失败都
停止，不使用 `continue-on-error`、重试或跳过步骤。preflight 先按 run id 取该 SHA
字面最新对象，再依次要求它已 `completed` 且 `success`；最新 run 失败或仍在运行都不会
回退到更老的成功 run。

macOS 辅助脚本的规范文件名是 `scripts/install-macos.sh` 和
`scripts/uninstall-macos.sh`。`release-index.json` 绑定四目标产物，也绑定这两份脚本的
`name`、`sha256` 与 `size`；汇总时保留脚本 `0755` 权限。不要使用下划线变体，也不提供
双别名。assembler 在创建输出前严格校验 `0.x.y` version、40 位小写 commit 与
GitHub Actions HTTPS run URL，并拒绝符号链接或非普通文件的 artifacts 根、target 目录
及四目标必需输入。

维护者从 draft release 下载资产时，先完成 GitHub 登录：

```bash
gh release list --repo lorek666/tianshu-agent
gh release download v0.1.0 \
  --repo lorek666/tianshu-agent \
  --dir /private/tmp/tianshu-draft
cd /private/tmp/tianshu-draft
```

检查 release index 的源码身份、构建 run、签名状态和辅助脚本摘要：

```bash
jq '{version, commit, build_run_url, signed, helper_scripts}' release-index.json
jq -r '.helper_scripts[] | "\(.sha256)  \(.name)"' release-index.json \
  | shasum -a 256 -c -
```

再独立核对 macOS 归档本身的 SHA256。该 `.sha256` 文件来自 draft；本地重算必须匹配：

```bash
shasum -a 256 -c Tianshu-Agent-0.1.0-macos-arm64.sha256
```

macOS 安装脚本只接受本地 archive 与 checksum 文件路径，不执行 `curl | bash`：

```bash
sh scripts/install-macos.sh \
  --archive ./Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz \
  --sha256 ./Tianshu-Agent-0.1.0-macos-arm64.sha256 \
  --prefix "$HOME/Applications"
```

脚本先校验 SHA256/tar 路径与链接，再调用同包原生 verifier；安装与卸载都拒绝程序目录和
数据根互相包含。默认卸载只删当前程序，保留
`$HOME/Library/Application Support/AgentOS`。清理测试数据必须显式给出 canonical 后
逐字匹配的二次确认：

```bash
sh scripts/uninstall-macos.sh --prefix "$HOME/Applications"

sh scripts/uninstall-macos.sh \
  --prefix "$HOME/Applications" \
  --data-root /private/tmp/tianshu-test-data \
  --purge-data \
  --confirm-data-root /private/tmp/tianshu-test-data
```

每次成功重装都会把一个旧 App 保留为
`$HOME/Applications/Tianshu Agent.app.previous-*`；再次成功安装会轮换并只保留一个旧
版本。默认卸载 **不会** 删除这个备份。需要回收磁盘时，先列出目录，再只删除确认过的
完整路径，不要删除仍要回退的那一份：

```bash
find "$HOME/Applications" -maxdepth 1 -type d \
  -name 'Tianshu Agent.app.previous-*' -print
rm -rf "/absolute/path/printed/above"
```

Windows 从同一 draft 下载目标架构的 NSIS setup 与对应 `.sha256`，在 PowerShell 中独立
重算并比对：

```powershell
gh release download v0.1.0 --repo lorek666/tianshu-agent `
  --pattern "*windows-arm64*" --dir "$env:TEMP\tianshu-draft"
$asset = "$env:TEMP\tianshu-draft\Tianshu-Agent-0.1.0-windows-arm64-setup.exe"
$actual = (Get-FileHash $asset -Algorithm SHA256).Hash.ToLower()
$expected = (Get-Content "$asset.sha256").Split()[0].ToLower()
if ($actual -ne $expected) { throw "SHA256 mismatch" }
```

摘要一致后运行 `Tianshu-Agent-0.1.0-windows-arm64-setup.exe`。安装器按当前用户安装，
用户数据位于 `%LOCALAPPDATA%\AgentOS`，默认卸载保留。产物未做 Apple/Microsoft 代码
签名；Gatekeeper、SmartScreen、Windows 10/11/ARM64 与 macOS x64 的真实设备安装、
升级、回退和权限提示都仍是 `UNKNOWN`。

release workflow 目前没有真实 dispatch 结果。即使未来 workflow 在四个原生 runner 上
全绿，也只得到 `runner-only` 构建证据，不能替代用户设备实测；物理断电下的持久性同样
保持 `NOT_RUN`。

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
