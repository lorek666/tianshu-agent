# 天枢 Agent 可安装发行版施工规格

> 状态：**R0 规格与清单基础候选。R1–R3 尚未按切片合入；不宣称已交付安装版或 G3。**
>
> 起草基线 `595be35`；本次拆分基线 `e242257984d2ebd1443e60c81e7d2c97b287122a`。
> 原 PR #162 的 `7d121ad564d07375c8777f51ad3b7330dd632125 / tree 922a259db4453ec9e78f5e32910e499ad300c2be`
> 独立复审为 REQUEST_CHANGES。其旧门禁、产物和生命周期结果只属于历史候选，不能作为本轮切片验收。

## 0. 问题、权威输入与判定口径

当前 main 具有 React UI、Python Host/Daemon 与系统浏览器。本轮 R0 只交付规格、
发布清单 schema、标准库校验器和真实字节测试。Tauri、锁定运行时、安装器和发布 workflow
分别留给 R1–R3；原大分支实现保留供逐片迁入，不能以文件已存在代替独立验收。

- 权威要求：`docs/PRD-v1.md` §11.1 G3、§13 T1、§10.6；`docs/TECH-ARCH.md` §8；
  `CONTRIBUTING.md` §8；现有偏差见 `docs/M5-desktop.md` §2。
- 固定决策：Python `>=3.12,<3.13`；Python 运行时依赖仍为零；单用户单机；四目标为
  macOS arm64/x64、Windows x64/arm64；不购买 Apple/Windows 代码签名；用户数据与程序
  安装目录分离；未实测的平台和设备不得宣称支持。
- 完成判据：每项能力必须有可重复测试或原始平台证据；“代码写了”“CI runner 跑过”
  “本机双击成功”都不能自动扩大为正式发布完成。
- 本规格自身不改变 PRD 五道契约、十一条红线、T1 或授权/数据格式。任何改变都需要单独的
  `docs(contract):` PR。

本规格区分三个层级，任何文档和 PR 必须使用同样口径：

| 层级 | 含义 | 何时可以宣称 |
|---|---|---|
| L0 当前状态 | Python Host + 系统浏览器 + `uv` | 已成立；仍称内部研发工具 |
| L1 可安装内测壳 | 未签名的 Tauri 壳携带锁定 Python/UI，可从干净用户目录启动 | 仅当产物、安装、启动、停止与卸载证据绑定同一提交 |
| L2 可追溯发行 | 四目标产物、SHA256/清单、升级与回退、源码/版本追溯、平台证据齐全 | 仅当 release workflow 和独立复审绑定最终 main |
| G3 正式发布 | PRD §11.1 全部发布条件，包括恢复、凭据、SLO、长稳、安装升级和遗留阻断裁决 | 只能由单独 G3 发布验收宣布，本规格不得代签 |

## 1. 目标与非目标

### 1.1 目标

1. 用 Tauri 2 提供原生窗口、托盘和单实例交互；窗口关闭后 Host/Daemon 继续工作，可从托盘
   重新打开，显式“退出”才停止服务。
2. 将锁定的 CPython 3.12、`src/tianshu`、已提交 `ui/dist` 和必要静态资源装进发行物，
   普通用户无需安装 Python、Node、uv、pip 或 pnpm。
3. 保持当前 IPC、Host HTTP/SSE、daemon、事件日志、审批和执行语义不变；壳只承担进程与
   窗口生命周期，不复制业务状态。
4. 提供可复现的分平台构建、版本化清单、SHA256、许可证与已知限制；发布物必须能追溯到
   精确 main 提交。
5. macOS 使用可审阅的安装脚本，Windows 使用未签名安装器并如实展示 SmartScreen 操作；
   不引入签名成本。
6. 安装覆盖升级和回退必须先备份用户数据，失败时不删除旧程序、不启动半升级服务。
7. 建立 mac arm64 本机证据、GitHub runner 构建证据和四目标用户设备证据三个独立层级。

### 1.2 非目标

- 不把 workspace/同 uid 守卫升级为物理沙箱。
- 不增加多用户、多租户、跨机器调度或商店分发。
- 不把 GitHub runner 当作 Windows 10/11、Windows arm64 或 macOS x64 用户设备。
- 不在本规格中补恢复 apply、低磁盘、GC、十小时 soak、真实模型效果或所有 G3 数据能力；
  这些是正式发布的前置依赖，缺失时 G3 保持 `NOT_RUN`。
- 不把 Tauri updater 自签名密钥说成 Apple/Microsoft 代码签名。
- 不提交用户数据、真实凭据、证书私钥、模型费用材料或测试数据库。

## 2. 现状态与缺口

| 能力 | 当前事实 | 本规格完成后 | 仍不能证明 |
|---|---|---|---|
| UI | React/Vite SPA；PR #155 的预算/子任务前端已合并且与当前 `ui/` 树一致；Host 在回环端口提供 `ui/dist` | 同一 `ui/dist` 被 Tauri WebView 加载，冷启动等待事件循环后再导航 | 所有 GPU/WebView 版本一致 |
| Host | Python 单实例、监督 Daemon、回环 HTTP/SSE、token | R1 将启动/附着同一 Host；清理前须在元数据守卫内复核 Host 与 Daemon 实例 | Rust Host 已替换 Python Host |
| 安装 | 当前需 clone + `uv sync` | 未签名安装脚本/安装器携带运行时 | 所有用户机器无 SmartScreen/Gatekeeper 差异 |
| 更新 | 无 tag workflow、无更新清单 | 版本清单、覆盖升级、失败回退 | Tauri 自动更新已交付 |
| 平台 | macOS arm64 本机；三平台 CI | 四目标产物及明确证据矩阵 | 未取得的用户设备实测 |
| 数据 | 默认根与程序目录分离；备份/预演已有 | 安装/升级不碰数据根，升级前备份 | 恢复 B/C、GC 和低磁盘已完成 |
| 发布 | 文档明确“尚未具备” | 可生成具名 release candidate | G3 正式发布已通过 |

## 3. 组件和进程边界

```text
Tianshu Agent.app / TianshuAgent.exe
  Tauri 2 shell (Rust)
  ├─ WebView: packaged ui/dist
  ├─ lifecycle: single instance / tray / child supervision
  └─ bundle/
       ├─ python/           pinned CPython 3.12 standalone
       ├─ app/              tianshu package
       ├─ ui/dist/          exact committed bundle
       ├─ release.json      short identity: version, commit, target
       └─ release-manifest.json
                            internal file set + SHA256
              │
              └─ spawn: bundled python -m tianshu host run
                         │
                         ├─ Python Host: loopback HTTP/SSE + token + supervisor
                         └─ Daemon: existing IPC/event/tool semantics
```

### 3.1 明确保留的接缝

- **Host 仍是 Python**。本切片把 Host 作为受 Tauri 管理的 sidecar，避免同时重写 Host、
  IPC 和服务生命周期。TECH-ARCH 的 Rust Host 目标仍保留为后续独立工单；发布文档不得把
  薄壳说成 Rust Host 已完成。
- Tauri 壳不直接读写事件库、不解析事件、不做审批、不调用模型、不持有业务缓存。
- UI 到 Host 的现有 `POST /rpc`、`GET /state`、`GET /events` 契约保持不变。Tauri 不复制
  token 到 URL、命令行参数、Tauri event 日志或持久配置。
- `ui/dist` 仍由 Host 注入 token；Tauri 只加载 Host 地址，不直接以 `file://` 打开页面，
  从而保留同源 CSP、Host 校验和 SSE 断线语义。

### 3.2 启动协议

新增 Rust 侧模块 `desktop/src-tauri/src/lifecycle.rs`，只依赖已启动的 Host 元数据和
现有 CLI：

1. 解析资源目录，校验同目录 `release.json` 的 `commit`、`target` 和自身版本；再按
   `release-manifest.json` 校验每个文件、完整文件集合和禁止的未列文件。
2. 选择用户覆盖的 `TIANSHU_DATA_ROOT`，否则使用 Python `ipc.default_data_root()` 同一
   平台规则；程序资源目录永不作为数据根。
3. 以无 shell 的 argv 启动 bundled Python：
   `python -m tianshu host run --port 0`。不得把密钥、prompt 或业务参数写进 shell
   字符串；默认监护 Daemon。
4. 在有界期限内等待 Host `/health` 和 `host.lock` 同时可读；健康响应 PID、锁文件 PID
   和 token 保护的 `/session` 响应必须一致。只读取已有 Host 元数据，不在日志中打印 token。
5. Tauri WebView 导航到 `host.lock` 中的 `url`。`setup` 在事件循环启动前执行，导航必须
   排入运行时的主线程消息队列；不能在同步等待 Host 的 `setup` 内直接调用
   `navigate`。加载失败时同样排队显示结构化启动错误，不允许空白页冒充可用。
6. 已有同数据根的可兼容 Host 时，壳只打开窗口附着，不启动第二个 Daemon；版本不兼容时
   明确拒绝并给出退出旧实例的说明。

### 3.3 关闭与退出语义

- 点击窗口关闭：隐藏窗口，Host/Daemon 继续；托盘菜单可“打开”或“退出”。
- 托盘或 macOS `Cmd+Q`：读取已核验的 Host 元数据，直接向 token 保护的 `POST /shutdown`
  发出请求，有界等待 Host/Daemon 收尾；失败保留进程和日志，报出 PID/数据根，不把窗口
  关闭当作进程已退出。若 Host 已崩溃但 Daemon 仍存活，退出门必须通过同包 Python 请求
  Daemon 关闭，并等锁消失后才允许壳退出。旧 Host 锁消失后须覆盖新 Host 发布新锁的
  接管窗口；停止 Daemon 前进入 Host 元数据跨进程守卫，确认仍无 Host，并且 Daemon 锁
  SHA 与退出前快照完全相同。新 Host 已发布或 Daemon 已换实例时必须保留，不得因一次
  旧锁缺失就清理。
- 系统注销/关机：壳先请求优雅停止；超出期限只能终止自身拥有的子进程，并写明终止不等于
  业务副作用已回滚。
- 壳崩溃：不删除 `host.lock`、不发送伪造结果；下次启动按现有 Host/Daemon 存活探测接管或
  附着。
- 单实例：同一用户双击多次只开一个 Tauri 壳；已有窗口收到聚焦/open 事件。

## 4. 发行物格式

版本初始为 `v0.1.0`，后续遵循 `v0.x.y`。所有资源名使用 ASCII，产品显示名可为“天枢
Agent”。

### 4.1 macOS

- `Tianshu-Agent-<version>-macos-<arch>.app.tar.gz`
- `Tianshu-Agent-<version>-macos-<arch>.sha256`
- `install-macos.sh` / `uninstall-macos.sh`
- `release-manifest.json`

安装脚本必须支持 `--archive PATH --sha256 PATH --prefix DIR [--data-root DIR]`，默认安装到
`~/Applications/Tianshu Agent.app`。脚本不接受未校验的远端 URL，不在一个未被审阅的
`curl | bash` 管道中执行。移动任何现有 app 之前，脚本调用同包原生
`tianshu-desktop --verify-bundle` 校验完整 manifest，并拒绝程序目录与数据根互相包含。
文档分别给出下载、校验、安装命令；安装完成前不改用户数据。

卸载默认只删程序，保留
`~/Library/Application Support/AgentOS/`；`--purge-data` 必须同时给出与 canonical 数据根逐字匹配的
`--confirm-data-root DIR`（例如 macOS 的 `/tmp` 必须确认其解析后的 `/private/tmp` 路径），并且拒绝根目录、家目录、系统公共目录及与程序目录重叠的数据根。

### 4.2 Windows

- `Tianshu-Agent-<version>-windows-<arch>-setup.exe`
- `Tianshu-Agent-<version>-windows-<arch>.sha256`
- `release-manifest.json`

使用 Tauri 的 NSIS 安装器；用户数据在 `%LOCALAPPDATA%\AgentOS\`，卸载默认保留。安装器
不要求管理员权限，不写 HKLM，不覆盖用户数据。未签名说明必须包含 SmartScreen
“更多信息 → 仍要运行”的真实截图或同版本实测记录；未实测前只标记 `unknown`。

## 5. Python 与前端捆绑

1. Python 使用 `python-build-standalone` 的 CPython 3.12，版本、release tag、目标 triple、
   下载 URL 和 SHA256 固定在工作树内 `release/python-runtime.lock.json`。
2. 下载后先校验 SHA256，再解包；禁止跟随工件内容自动更新版本。目标不匹配或哈希不符时
   构建失败。
3. 安装包只包含 `src/tianshu` 和运行所需资源；不包含 `.venv`、tests、refs、logs、
   `.omc`、用户数据库或真实凭据。
4. UI 只使用已经通过 typecheck/test/build 的 `ui/dist`；构建发布物后再次执行
   `git diff --exit-code -- ui/dist`。
5. 包内 `release.json` 只保留短身份，必须包含：

```json
{
  "schema": 1,
  "product": "tianshu-agent",
  "version": "0.1.0",
  "commit": "<40-hex-main-sha>",
  "target": "macos-arm64"
}
```

6. 包内 `release-manifest.json` 绑定身份和每个内部文件的 SHA256：

```json
{
  "schema": 1,
  "product": "tianshu-agent",
  "version": "0.1.0",
  "commit": "<40-hex-main-sha>",
  "target": "macos-arm64",
  "python": "3.12.16",
  "python_build_release": "<pinned>",
  "ui_dist_sha256": "<sha256>",
  "files": {"<relative-path>": "<sha256>"}
}
```

7. Python release verifier 必须严格解析封闭五字段 `release.json`，要求它与请求身份及
   `release-manifest.json` 的 version/commit/target 一致；macOS 归档和 Windows NSIS
   解包资源中的短清单、完整 manifest 必须分别与已验证 bundle 字节相同。Windows 还必须
   由 7-Zip 实际解出唯一资源根，不得以“外层 PE 合法”代替 payload 证据。
8. 汇总层 `release-index.json` 记录每个最终资产的 SHA256、大小、构建运行 URL、源提交、
   target、`runtime_smoke`、`device_verification` 和 `signed=false`。三份文件职责不得混写；
   未知字段拒绝、未知 target 拒绝、未知 schema 拒绝。

## 6. 可复现构建与发布流程

新增两个入口，逻辑共享，不在 CI YAML 中复制业务：

- `scripts/build_release.py --target <target> --version <version> --out <dir>`：本机/CI
  构建单个目标，只允许 clean worktree 或显式已验证 artifact source。
- `scripts/verify_release.py <dir>`：验证清单、SHA、目标布局、禁止文件、Python/UI 身份、
  真实 bundled sidecar 与 Host 可启动的最小 smoke，不允许用 `|| true` 吞失败。
  smoke 使用独立临时数据根，校验 Host `/health` 与鉴权 `/session` 的 PID 身份，等待
  Daemon IPC 健康；成功或失败后都须清理本次拥有的子进程。import 成功不能单独得到 pass。

新增 `.github/workflows/release.yml`，只在 `workflow_dispatch` 与严格 `v*` tag 触发：

1. 手动输入的完整 commit 必须等于 fetch 后的当前 `origin/main`；tag 先解引用到
   其 commit，再执行同一精确相等检查。旧 main 祖先不能作为本次候选。
2. 查询该 SHA 的四项 required 和 main push CI；不绿即停止。
3. 四目标各自由匹配 OS/arch 的原生 runner 构建并执行 import/runtime smoke；assembler
   只接受 `runtime_smoke.status=pass` 且 `host_target` 与 target 完全一致，跨目标结果
   不得进入候选。
4. 运行 release verifier，生成 SHA256 与 manifest，上传 GitHub Release draft。
5. 人工检查 draft 后才发布；工作流不在未审材料下自动 public release。
6. 任何 required check、版本、commit、哈希、许可或广告来源失败均不得发布。

当前 CI required checks 名称和分支保护不在本规格中修改。新增 release workflow 先为非
required 的发布门禁；若未来把它设为 required，必须单独同步远端保护配置与 CONTRIBUTING。

## 7. 升级、回退与数据保持

### 7.1 兼容规则

- 新版本启动前运行数据版本准入；不支持的历史版本拒绝写入并提示升级路径。
- 首次跨 schema 版本启动前，先通过现有备份入口生成验证通过的离线备份。
- 旧 `.app`/安装目录在升级成功前保留；新版本失败时恢复旧程序。数据恢复只能在已有
  restore B/C 产品入口和独立验收后启用，不能调用未验收的低层复制函数冒充回退。
- 程序安装目录与用户数据目录不得互相包含；升级/卸载脚本拒绝配置到源目录或数据根。

### 7.2 更新清单

后续 Tauri updater 切片才增加自动更新。自动更新使用独立 Ed25519 清单签名，公钥嵌入
release manifest；私钥只存 GitHub protected environment secret。它只证明清单/资产完整性，
不是 OS 代码签名。没有匹配公钥或目标的更新一律拒绝。

本规格的 L2 可以交付“下载新版本 → 校验 → 覆盖安装 → 保留数据 → 失败恢复旧程序”的手动
升级闭环；不得写成 Tauri 自动更新已完成。

## 8. 安全与失败闭包

| 风险 | 必须行为 |
|---|---|
| 资源目录被替换 | 启动和安装前按 manifest 校验完整文件集合；未列 `sitecustomize.py`、被改字节或逃逸链接均拒绝 |
| 运行时篡改自身资源 | bundled Python 的所有入口固定 `PYTHONDONTWRITEBYTECODE=1`；Host 环境由 Daemon 继承，`__pycache__` 不得使下一次启动绕过或破坏完整文件集合 |
| 伪造更新包 | 校验版本、target、commit、SHA256；未来 updater 另验 Ed25519 |
| 日志泄露 token | Tauri/Host/日志均不打印 token；测试扫描 stdout/stderr |
| 壳与 CLI 启双重 Daemon | 复用 Host 单实例和 daemon 锁；双进程屏障测试 |
| 关闭窗口误杀任务 | 关闭只隐藏；显式退出才 stop；崩溃恢复走既有语义 |
| 升级卡在半写 | 原子安装、旧版本保留、启动 smoke 后才切默认入口；SIGKILL 前后正式入口都必须存在或能确定性恢复 |
| 用户数据被卸装 | uninstall 默认不删数据；purge 需要显式参数、与 canonical 数据根逐字匹配的 `--confirm-data-root` 与路径重叠检查 |
| 路径含空格/Unicode | 全 argv 传递，不拼 shell；macOS/Windows 路径测试 |
| 平台未测 | `release-manifest.json` 状态必须 `verified`/`runner-only`/`unknown` |

## 9. 测试与负控

### 9.1 Python 契约测试

- `tests/test_release_manifest.py`：schema、未来版本、负数/布尔数字、未知 target、SHA 不符、
  缺文件、额外禁止文件、commit 非 40 位。
- `tests/test_release_build.py`：锁定 Python 版本目标、解包路径穿越、错误 SHA、复制 UI
  dist、排除 `.venv`/数据库/凭据。
- `tests/test_release_verify.py`：短身份/版本/target 漂移、清单与 bundle 字节、归档
  source 集合、source bundle 完整文件集合、平台架构、归档 manifest 漂移、伪 NSIS
  payload 与逃逸链接；未列 `sitecustomize.py` 在 source 和 archive 两层都必须拒绝。
- `tests/test_release_install.py`：macOS canonical 前缀、原生 verifier 前置顺序、合法/错误
  SHA、tar 逃逸拒绝、程序/数据根重叠、安装烟测、失败恢复旧程序、卸载保留数据与
  `--purge-data` 二次确认。升级回退先由这里的安装器烟测覆盖；跨 schema 数据升级仍须等
  恢复 B/C，不能由该测试冒充。

### 9.2 Rust/Tauri 测试

- `desktop/src-tauri` 使用 `cargo test` 覆盖目标解析、路径规范化、完整资源集合、Host
  `/health` + `/session` 身份、token 保护的 shutdown、孤儿 Daemon argv、子进程 argv 和
  启动失败结构体。
- 用真实 bundled Python fixture 与临时数据根跑子进程；不能用 mock child 声称跨进程。
- 壳启动的 Host 崩溃时 UI 显示错误，自动重启次数有界；停止后无孤儿进程。
- owned/attached Host 最多 3 次有界退避恢复；辅助清理只接受已退出且 PID/
  `started_at` 匹配的 Host，并在元数据守卫内按 Daemon 锁 SHA 条件停止。supervisor
  发现新 endpoint 时必须先用 `try_wait()` 确认旧 owned child 已退出，不能因新锁出现
  而丢掉仍存活的子句柄。
- `旧 Host 退出 → host.lock 空窗 → 新 Host 发布 → 复用 Daemon` 的真实进程回归：
  退出探针不得停止新 Host 或其 Daemon；同时覆盖 Host 已发布、Daemon 实例已变更两个
  保留分支。普通“新锁已经存在”的探针不能替代这个窗口。
- 退出辅助 Python 子进程的 stderr 必须显式 UTF-8，接管/失败诊断不能因 Windows
  locale code page 变成另一退出码。
- Tauri `setup` 冷启动回归必须同时证明 Host 身份可读且 WebView 实际渲染目标 UI；
  只有窗口存在、进程存活或 `/health=200` 不能证明用户看得到页面。
- token 不出现在 argv、URL、Tauri log、release manifest。

### 9.3 负控

每项安全/发布测试必须先证明旧/坏路径失败：

1. 改资产一个字节但保留旧 SHA → 拒绝启动/升级。
2. 用 Windows target 资产冒充 macOS → 拒绝。
3. 让安装脚本试图越过 prefix 或写数据根 → 拒绝。
4. 在 Host 未就绪时宣称窗口可用 → 测试必须红。
5. 关闭窗口后擅自停止 Daemon → 测试必须红。
6. 删除旧版本后新版本启动失败 → 回退测试必须红。
7. 让 token 进入日志 → 脱敏/安全测试必须红。
8. UI dist 被手改但未重建 → release build 必须红。
9. manifest 未列出 `sitecustomize.py` → source bundle、归档、App 启动和安装前验证
   都必须红；不能只让原生壳拒绝而 Python verifier 放行。
10. Host 附着端口开放但 PID/token 身份不符 → 必须拒绝附着。
11. Host 崩溃后退出壳 → Daemon 必须被请求关闭并等锁消失，不能留孤儿。
12. 短 `release.json` commit/version/target 漂移，即使重新计算 manifest/archive SHA →
    release verifier 必须红。
13. 归档或 NSIS payload 的完整 manifest 与 bundle 字节不一致 → verifier 必须红。
14. Windows 只给合成 PE header 或合法 PE 外壳但没有可解析 NSIS payload → verifier 必须红。
15. 四目标 runtime smoke 非 `pass` 或 `host_target` 不匹配 → assembler 必须红。
16. 在 Tauri `setup` 内等待 Host 后直接 `navigate` → 真实 Release WebView 冷启动白屏；
    修复后必须显示加载页并进入“已连接”，不能只用窗口/进程断言。
17. 在 `host.lock` 空窗后让新 Host 发布并复用旧 Daemon → 旧退出路径若误杀新实例必须
    红；修复后必须以元数据守卫内的二次确认和 Daemon 锁 SHA 保留新实例。
18. Host 已发布新 endpoint、但 supervisor 旧的 owned `Child` 仍存活时 → 旧实现不得
    丢掉 child 并重走 spawn/attach；修复后必须保留子句柄直到 `try_wait()` 证明退出。

19. bundle、manifest、archive 和 checksum 全部自洽，但 Host 入口必然崩溃而 import 正常 → runtime smoke 必须红。
20. 系统退出超时 → 只终止本壳确实拥有的 child；普通退出和 attached/新实例不得误杀。
21. macOS 换装在原子切换前后以真进程 SIGKILL → 正式入口和旧版本字节可恢复；trap 回滚不能替代此证据。

### 9.4 平台证据矩阵

| target | 构建证据 | 启动/安装证据 | 升级/回退证据 | 当前可宣称 |
|---|---|---|---|---|
| macOS arm64 | GitHub/本机构建 | 本机干净用户目录 | 本机临时数据根 | 本规格内可做 |
| macOS x64 | GitHub/兼容构建 | 需要 x64 用户设备或 runner | 需要用户设备 | 未取得前 `unknown` |
| Windows x64 | Windows runner | 需要 Windows 10/11 x64 设备 | 需要设备 | runner 通过也只是 `runner-only` |
| Windows arm64 | 原生 ARM64 runner | 需要 Windows arm64 设备 | 需要设备 | 未取得前 `unknown` |

“CI 绿”“文件存在”“跨编译成功”都不能填成用户设备已验证。

## 10. 交付切片与合并顺序

每一片单独 PR；后一片从前一片合入的 main 派生，不把未审文件串成一个大 PR。

| 切片 | 交付 | 退出条件 |
|---|---|---|
| R0 | 本 spec、release manifest schema、版本/target 常量 | 真实 schema 测试、负控、文档边界审查通过 |
| R1 | Tauri 壳、托盘、单实例、Python sidecar、Host 附着 | 本机真实窗口 + 真实 Host/Daemon；关闭/退出/崩溃探针 |
| R2 | 锁定 Python runtime、四目标 build/verify 脚本 | 本机 macOS arm64 产物可安装启动；打包不夹用户数据 |
| R3 | mac 安装脚本、Windows NSIS、可追溯 manifest、release workflow | 产物哈希/源码/版本绑定；安装/卸载证据 |
| R4 | 手动升级与回退、平台证据矩阵 | 旧/新版本切换、数据保留、失败回退 |
| R5 | 自动更新（可选，需独立 spec） | Ed25519 清单签名、断网/篡改/回滚负控 |

正式 G3 只有在以下项目全部关闭后才可另行宣布：恢复 B/C、Keychain 实机、SLO 阈值与
测量、十小时 soak、四目标安装/升级/回退、遗留阻断裁决。当前这些项目不能因 R0–R4 的
完成自动变成已完成。

本轮按以下边界推进，后续片从前一片保护合入后的 main 派生：

- R0：仅 spec/schema、`release_manifest.py` 与真实文件字节校验测试；不包含桌面或安装产品。
- R1：桌面壳、Host/Daemon 生命周期、启动与原子 metadata 发布所需的最小 IO 修复；独立记录真实窗口、关闭/重开、owned/attached、takeover 和系统退出组件探针。
- R2：锁定运行时、构建/打包/验证入口；runtime smoke 必须真实启动 sidecar 与 Host。自洽入口崩溃负控不可省。
- R3：macOS 安装/卸载、NSIS、assembler 和四目标 draft workflow；原 PR #162 保留作此片。必须验证崩溃原子换装，再重建精确候选产物。

旧 `7d121ad` 把四片合并且有三项产品阻断、PR 正文过期。拆分不重写其历史，保留旧失败；各片的独立复审、全量门禁和云 CI 均另绑新 SHA。四目标 release 只能在最终 main 自身 CI 通过后运行。Issue #145 保持独立 OPEN，runner 结果不代表 Windows 用户设备支持。

## 11. 接口与文件布局

建议布局，实际 PR 可按既有目录风格微调，但不得把业务逻辑塞进 Tauri/CI YAML：

```text
desktop/
  src-tauri/
    Cargo.toml
    Cargo.lock
    tauri.conf.json
    src/main.rs
    src/lifecycle.rs
    src/release.rs
  README.md
release/
  release.schema.json
  python-runtime.lock.json
  update-key.pub
scripts/
  build_release.py
  verify_release.py
  release_manifest.py
.github/workflows/release.yml
tests/
  test_release_manifest.py
  test_release_build.py
  test_release_install.py
  test_release_upgrade.py
```

接口固定如下，后续实现不得改名后各自造一套：

```python
# scripts/release_manifest.py
def read_manifest(path: Path) -> dict: ...
def validate_manifest(data: object, *, base_dir: Path) -> None: ...
def write_manifest(path: Path, data: dict) -> None: ...

# scripts/build_release.py
def prepare_bundle(*, repo_root: Path, target: str, version: str, commit: str,
                   runtime_archive: Path, out_dir: Path, ...) -> Path: ...

# scripts/verify_release.py
def verify_release(*, artifact_dir: Path, bundle_dir: Path, target: str,
                   version: str, commit: str, run_smoke: bool = True) -> dict: ...
```

Rust 侧只暴露：

```rust
pub struct ReleaseIdentity {
    pub version: String,
    pub commit: String,
    pub target: String,
}

pub fn verify_resource_manifest(
    resource_dir: &Path,
    identity: &ReleaseIdentity,
) -> Result<(), ReleaseManifestError>;
```

## 12. 验收清单

- [ ] R0 真实 schema、身份封闭集、真实字节/路径/SHA 负控及独立复审通过。
- [ ] R1 真实窗口、owned/attached Host/Daemon、关闭/重开、崩溃恢复和 takeover；系统退出组件超时只终止 owned child。真实整机注销/关机与组件探针分别记账。
- [ ] R2 锁定运行时与四目标 build/verify；真实 sidecar/Host/Daemon 健康烟测、退出清理和自洽入口崩溃负控通过。
- [ ] R3 安装/卸载/保留数据/purge canonical 确认、硬退出原子换装、NSIS 和 workflow 通过。
- [ ] 每片的 lock、Ruff、全量 pytest、适用 UI/Cargo/脚本门禁绑定新提交；负控原件保留。
- [ ] 最终产物重建并绑定当前提交、target、version、完整 manifest 与 archive SHA；真实 GUI 和 takeover 使用该产物。
- [ ] 新独立第二读者亲跑验收，结论绑定完整 SHA/tree/基线，旧 REQUEST_CHANGES/PARTIAL/UNFIXED 不继承为 PASS。
- [ ] 每片当前 required checks 全绿才保护合并；main 自身 CI 和最终四目标 release workflow 分别核验。
- [ ] macOS x64、Windows x64/arm64 的构建/设备证据逐项标为 verified / runner-only / unknown。
- [ ] L1/L2 内测与 G3 正式发布区分；恢复、SLO、十小时 soak 和用户设备未完成时不代签 G3。

## 13. 未决与阻塞依赖

以下项目没有被本规格吸收，缺失时应保持公开状态 `NOT_RUN`，不能被 release workflow 的
产物生成掩盖：

1. 恢复 B/C 的产品实现与四目标证据。
2. Tauri updater 的密钥托管、轮换与泄漏处置（R5；未实现前只做手动更新）。
3. macOS TCC 在真实用户设备上的首次授权、覆盖安装后是否重置，以及对应引导文案。
4. Windows 10/11 x64 与 arm64、macOS x64 的干净机器安装和 SmartScreen/Gatekeeper 实测。
5. PRD §10.6 的发布候选 SLO、十小时 soak、资源趋势和遗留阻断裁决。
6. 许可证/依赖清单扫描与第三方 notices；Rust/Tauri 构建依赖必须在 release manifest 和
   CI 文档中显式列出。

本规格回答“如何从浏览器工具走到可安装发行物”，不回答“天枢 Agent 已经达到正式发布
质量”。
