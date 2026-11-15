# 天枢 Agent 可安装发行版施工规格

> 状态：**L1 本机实现候选；尚未完成四目标与独立验收，不得据此宣称 G3 正式发布。**
>
> 起草基线：`main @ 595be35c037cef35da7f166a5db2a624f2b733a9`。候选现已普通合并当前
> `main @ e242257984d2ebd1443e60c81e7d2c97b287122a`；任何基线或旧候选 CI 都不是新
> 发行候选提交的绿色证据。
>
> 前端 PR #155 已合并为 `d37a84c490b7e6588b4e122e5591ccb989530b8e`，当前分支的
> `ui/` 树与该分支逐字节一致；桌面壳加载的是同一份 `ui/dist`，不存在待手工接入的
> 第二套前端。旧候选 `aa6b65f` / `820a7cd` 的真实自有 Host 冷启动暴露了一个此前
> 生命周期探针未覆盖的真实缺陷：Host、Daemon、`host.lock` 与 `/health` 均正常，但
> Release WebView 稳定为白屏。产品修复提交为
> `58d0bdb0c9648a870368c1aa7723c4fc0c450f8b`，把 `navigate` 和失败页 `eval` 排入
> Tauri 事件循环。该提交的 macos-arm64 产物已重新通过校验、运行时烟测、篡改负控、
> 全新安装、真实冷启动显示“已连接”、关窗保活、重开同一 Host 和 `Cmd+Q` 三层进程
> 全退。其后的 `b46f015` 精确 CI 又在 Windows shard 1 抓到既有原子写入路径的
> `WinError 5`：审批并发控制本身通过，获胜写入在 `os.replace` 发布 `note.md` 时遇到
> 瞬时的目标占用。代码修复 `bb0ce76` 只对 Windows access/sharing/lock violation
> 做 8 次、约 1.27 秒总退避；其他权限错误立即失败，持续占用在界满后仍报错。第二读者
> 对 `5873720/tree f16f930` 的独立复审为 `PARTIAL`：独立构建、验签、安装、普通自有
> Host 生命周期和 W3/W5 兼容通过，但附着既有 Host 后显式退出只关闭桌面壳，遗留
> Host/Daemon。后续修复统一 owned/attached 退出的鉴权 shutdown 与有界清理路径；第二
> 读者对 `7b4cc7a/tree 251537b` 的复审仍为 **`PARTIAL`**，并新增一条 HIGH：
> `host.lock` 的“旧锁消失、新锁尚未发布”空窗中，退出路径会误杀新 Host 已接管的
> Daemon。修复提交 `0942ee5` 在有界接管窗口内持续观察，清理前进入 Host 元数据跨进程
> 守卫、重新确认无 Host，并只停止锁 SHA 与退出前快照完全一致的 Daemon；新 Host 或
> 新 Daemon 实例出现时返回保留。真实竞态回归旧实现红、修复后绿；代码收口 `77e1181`
> 固定 Windows stderr 为 UTF-8。精确该代码树的完整 Python 本机门为 3543P/12S
> （656.70s），Rust 27 tests，UI 124 tests，lock、Ruff、actionlint、shellcheck 与
> diff check 通过。最终
> docs-only SHA 仍须重新绑定云 CI；精确 macos-arm64 产物、锁空窗与普通
> attach→quit 探针、第二读者 PASS 另绑，不得继承 `5873720`、`7b4cc7a` 或任何更早
> 对象的绿色。状态仍是 L1 候选，不得合并为已验收发行版。

## 0. 问题、权威输入与判定口径

规格起点是真实的 React UI、Python Host/Daemon 与系统浏览器，没有 Tauri 外壳、安装产物、
升级/回退闭环或 tag 发布流程。当前 `codex/release-app` 候选已补入未签名 Tauri 薄壳、
锁定 CPython/UI bundle、manifest/架构校验、macOS 安装/卸载、升级烟测回退和四目标
release workflow；macOS arm64 已做本机真实安装生命周期。它们仍是实现证据，不是 G3 签发：

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
| Host | Python 单实例、监督 Daemon、回环 HTTP/SSE、token | Tauri 启动/附着同一 Host，并管理退出；清理前在元数据守卫内复核 Host 与 Daemon 实例 | Rust Host 已替换 Python Host |
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
`~/Library/Application Support/AgentOS/`；`--purge-data` 必须同时给出逐字匹配的
`--confirm-data-root DIR`，并且拒绝根目录、家目录、系统公共目录及与程序目录重叠的数据根。

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
  app/exe 可启动的最小 smoke，不允许用 `|| true` 吞失败。

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
| 升级卡在半写 | 原子安装、旧版本保留、启动 smoke 后才切默认入口 |
| 用户数据被卸装 | uninstall 默认不删数据；purge 需要显式参数、逐字匹配的 `--confirm-data-root` 与路径重叠检查 |
| 路径含空格/Unicode | 全 argv 传递，不拼 shell；macOS/Windows 路径测试 |
| 平台未测 | `release-manifest.json` 状态必须 `verified`/`runner-only`/`unknown` |

## 9. 测试与负控

### 9.1 Python 契约测试

- `tests/test_release_manifest.py`：schema、未来版本、负数/布尔数字、未知 target、SHA 不符、
  缺文件、额外禁止文件、commit 非 40 位。
- `tests/test_release_build.py`：锁定 Python 版本目标、解包路径穿越、错误 SHA、复制 UI
  dist、排除 `.venv`/数据库/凭据。
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
9. manifest 未列出 `sitecustomize.py` → App 启动和安装前必须红。
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

本轮候选当前只可宣称：

- macOS arm64：真实 release-mode 构建、安装、WebView 连接、关窗保活、重开、显式退出、
  卸载保留数据与显式清理临时数据；篡改 bundle 字节后被启动校验拒绝。`0942ee5`
  已提供锁空窗竞态的作者侧红绿回归；最终安装产品的锁空窗与普通 attach→quit 仍需
  在精确 docs-only SHA 上复验。
- 前端 PR #155 已完整进入当前发行分支；本机 16 文件 124 tests、typecheck/build 与
  `ui/dist` 对账通过。`58d0bdb` 的安装态冷启动实际显示 Host 提供的同一前端，不再把
  “进程起来但页面白屏”记成 GUI 通过。
- Windows 原子替换对 `ERROR_ACCESS_DENIED` / `ERROR_SHARING_VIOLATION` /
  `ERROR_LOCK_VIOLATION` 使用有界重试；瞬时占用恢复后写入成功，永久占用仍非零失败且
  不残留临时文件。CI runner 结果仍不等于 Windows 10/11 用户设备验证。
- macOS x64、Windows x64、Windows arm64：代码与 workflow 已具备对应 target 构建入口，
  但用户设备、安装、升级、回退与系统警告仍为 `unknown`；后续原生 workflow 通过也只能
  记为 runner 证据。

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

- [ ] 对最终提交重跑 release manifest/build/install/verify 定向测试；未列文件、已列
  篡改、摘要、架构、tar 逃逸、短身份/归档 manifest 漂移和伪 NSIS payload 负控均绑定
  最终 SHA。`def12d27` 的上一轮结果只保留为其历史证据。
- [x] 对 `58d0bdb` 的 macos-arm64 安装产物验证 Tauri 壳真实启动 bundled Python 与
  Host/Daemon；冷启动 WebView 显示“已连接”，关窗后三层进程继续，LaunchServices
  重开保持同一 Host，`Cmd+Q` 无孤儿。完整脚本化审批链仍须在最终 docs-only SHA 重建后
  复验，不能把旧产物结果冒充新 SHA。
- [ ] 对最终产物验证 macOS arm64 安装、Host 崩溃后退出、重装回退与卸载；数据保留
      和 purge 二次确认明确。
- [ ] macOS x64、Windows x64、Windows arm64 的每项证据标为 `verified` / `runner-only` /
      `unknown`，不混写。
- [x] 产物只含允许文件；无 `.venv`、tests、数据库、日志、密钥、refs、`.omc`。
- [ ] `release.json` 与 `release-manifest.json` 绑定最终合并 SHA；当前本机产物只绑定
  基线 SHA，不能把未提交工作树的字节冒充该 SHA。
- [x] 安装器烟测失败时恢复旧程序；数据恢复尚未交付，安装/升级不调用低层 restore。
- [ ] 最终提交的 `uv lock --check`、ruff、完整 pytest、UI
      typecheck/test/build/dist、Cargo 测试和 release verifier 全绿；负控原件保留。
- [ ] 独立复审读取正式 SHA、原始日志、安装/升级探针，而非作者自述。
- [x] 文档明确“L1/L2 内测”与“G3 正式发布”的差异，不把本规格的实现说成所有 G3 已完成。

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
