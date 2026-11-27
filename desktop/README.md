# Tianshu Agent 桌面壳与发行候选

> 状态：R0–R3 四目标构建与安装基线已随 PR #175 / #186 合入；`e87a38c` 已实跑单目标预览并生成未公开的 `v0.1.1` prerelease 草稿。
> 主干此后已有新提交，该草稿仅对应 `e87a38c`；最终候选用户设备验收仍待完成。
> 本文不把源码、局部测试、预览 workflow 或 CI runner 结果写成用户设备已验证的正式发行版。

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

首发按 [PRD §11.1](../docs/PRD-v1.md) 仅准备 `macos-arm64` 限定预览。实现合入后，在
GitHub Actions 的 **Release candidate** 选择 `main`，填写未占用的 `version`（如 `0.1.0`）与
当前主干的完整 `commit`，勾选 `macos_arm64_preview`。只构建和汇总 Apple Silicon 产物，
创建标记为 Apple Silicon Preview 的 prerelease **草稿**；同名 Release（包括草稿）或 tag
已存在时拒绝。不开启该选项或用 `v0.x.y` annotated tag 触发时，仍走四个原生目标。

两条路径共用提交、CI 与原生校验：workflow 的提交和手动输入或 tag 解引用后的完整 SHA
都须等于当前 `origin/main`，该 SHA 最新一次 main push CI 的四项 required 检查与全部 job
必须成功。旧绿不替代较新的失败或运行中结果。每个所选目标都需通过发行物校验和匹配宿主的
runtime smoke。工作流不公开发布；真机条件见 [CONTRIBUTING §8](../CONTRIBUTING.md#8-发布)。

源码辅助脚本固定为 `scripts/install-macos.sh` 和 `scripts/uninstall-macos.sh`，下载到同一目录下
不带 `scripts/` 前缀。`release-index.json` 绑定版本、完整提交和 Actions 构建 run，并记录归档、
checksum、manifest 和两份脚本的文件名、SHA256 与大小。预览拒绝混合目标、额外输入及非空输出；
工作流只上传索引列出的资产与索引自身。

维护者从预览草稿下载资产前先完成 GitHub 登录。以下版本号替换为待验候选，下载到新的空目录：

```bash
download_dir=$(mktemp -d /private/tmp/tianshu-draft.XXXXXX)
gh release download v0.1.0 \
  --repo lorek666/tianshu-agent \
  --dir "$download_dir"
cd "$download_dir"
```

先将 `version`、`commit`、`build_run_url` 与候选主干 SHA 和该次 Actions run 对账。核对目标
集合只含 `macos-arm64`，验证索引列出的全部资产，以及归档独立 checksum：

```bash
jq '{version, commit, build_run_url, signed, artifacts}' release-index.json
jq -e '[.artifacts[].target] == ["macos-arm64"]' release-index.json
jq -r '.assets[] | "\(.sha256)  \(.name)"' release-index.json | shasum -a 256 -c -
shasum -a 256 -c Tianshu-Agent-0.1.0-macos-arm64.sha256
```

### macOS 资源封套与发布者身份不是同一项

`package_app.py` 在最终 Tauri App 校验后，仅对外层执行免费的 ad-hoc 资源封装：
`codesign --sign - --timestamp=none`，不使用 `--force` 或递归签名。它接受无签名或
linker-ad-hoc 主程序，拒绝已有资源封套；其他已有签名由原生工具的 no-force 语义拒绝，
失败不换参数重试。封装前后只允许主程序字节和精确 `_CodeSignature/CodeResources`
封套变化，原资源、Info.plist 与原有权限必须保持。

生成路径必须通过原生 `codesign --verify --deep --strict --verbose=4`，真实 tar
归档再在私有目录受限提取，核对完整文件/目录集合、字节和权限，并对恢复的 App 再做同一
原生验证，通过后才发布归档及 checksum。直接调用 `release_artifact` 只验证已封装输入，
不重复签名。失败保留私有 staging/roundtrip 现场并报错，不自动修复或清理 xattr。

静态 verifier 只兼容性地允许这一精确的可选目录及非空普通文件，仍可读取历史未封装
fixture；这不是 Apple 验签或信任判定。`release-index.json` 的 `signed: false`
保持表示没有认证发布者证书签名，不表示 Mach-O 中绝无 linker/ad-hoc 签名位。
没有 Developer ID、公证或 Authenticode，不保证 Gatekeeper/SmartScreen 放行，
也不替代原生启动、环境变量凭据读取和安装/升级/回退验收。
旧草稿和资产不得原地重签或覆盖，修复须走新提交和未占用版本的新候选。

macOS 安装脚本只接受本地 archive 与 checksum 文件路径，不执行 `curl | bash`：

```bash
sh ./install-macos.sh \
  --archive ./Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz \
  --sha256 ./Tianshu-Agent-0.1.0-macos-arm64.sha256 \
  --prefix "$HOME/Applications"
```

脚本先校验 SHA256/tar 路径与链接，再调用同包原生 verifier；安装与卸载都拒绝程序目录和
数据根互相包含。默认卸载只删当前程序，保留
`$HOME/Library/Application Support/AgentOS`。清理测试数据必须显式给出 canonical 后
逐字匹配的二次确认：

```bash
sh ./uninstall-macos.sh --prefix "$HOME/Applications"

sh ./uninstall-macos.sh \
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

### Windows 安装（后续四目标流程，不适用于预览）

从对应的四目标草稿下载目标架构的 NSIS setup 与对应 `.sha256`，在 PowerShell 中独立
重算并比对：

```powershell
gh release download v0.1.0 --repo lorek666/tianshu-agent `
  --pattern "*windows-arm64*" --dir "$env:TEMP\tianshu-draft"
$stem = "$env:TEMP\tianshu-draft\Tianshu-Agent-0.1.0-windows-arm64"
$actual = (Get-FileHash "$stem-setup.exe" -Algorithm SHA256).Hash.ToLower()
$expected = (Get-Content "$stem.sha256").Split()[0].ToLower()
if ($actual -ne $expected) { throw "SHA256 mismatch" }
```

摘要一致后运行 `Tianshu-Agent-0.1.0-windows-arm64-setup.exe`。安装器按当前用户安装，
用户数据位于 `%LOCALAPPDATA%\AgentOS`，默认卸载保留。产物未做认证发布者的
Developer ID / Authenticode 证书签名；macOS 免费资源封套不改变这一点。
Gatekeeper、SmartScreen 和真实设备安装、升级、回退及权限提示仍待验收。

`e87a38c` 已实际运行预览 workflow 并生成未公开草稿；原生 runner 的构建和 smoke 不替代真实
用户设备实测。该草稿不证明后续主干提交已通过发行验收；物理断电持久性也未验证，不作为限定预览前置。

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
