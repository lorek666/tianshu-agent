# 技术架构书（TA）— Windows / macOS 桌面 App

> 配套 `PRD-v1.md`。本文只回答"用什么技术、怎么落地、两端差在哪"。
> 目标平台：macOS 13+（arm64 / x64）、Windows 10 1809+ / 11（x64）。

---

## 1. 选型总表

| 层 | 选型 | 理由 | 被否方案 |
|---|---|---|---|
| UI 外壳 | **Tauri 2**（Rust + 系统 WebView） | 体积小；Rust 宿主天然承担"OS 权限拥有者"角色；本来就要 native FFI | Electron：+90MB，且要额外 Node 原生模块层才能做 AX/UIA |
| UI 前端 | React 19 + TypeScript + Vite | 生态成熟 | — |
| 宿主 | Rust 2024 edition | FFI、沙箱、签名链可控 | Go：FFI 与 macOS 签名链更麻烦 |
| Agent 运行时 | Python 3.12 | 生态最全；3.12 而非 3.14，wheel 覆盖最稳 | 3.13+：部分 C 扩展无轮子 |
| Python 分发 | **python-build-standalone**（Astral 构建）+ 预解析 venv，作为 resource 打包 | 可复现、可逐文件签名、无 PyInstaller bootloader 黑盒 | PyInstaller：启动慢、Windows 杀软误报高；Nuitka：构建慢、调试难 |
| IPC | **本地 socket + 长度前缀帧**（macOS: Unix domain socket；Windows: Named Pipe） | **不起 TCP 端口 = 不触发 Windows 防火墙弹窗**；无端口冲突 | localhost HTTP：Windows 每次弹防火墙 |
| 本地存储 | SQLite（WAL）+ 文件级 Event Store | 单用户场景足够 | 嵌入式 KV：查询能力不足 |
| 密钥 | macOS Keychain / Windows DPAPI | 系统托管，开源后不落明文 | config 文件：开源即泄密 |
| 遥测 | 本地 OTel 导出，无强制上报 | 桌面产品 + 开源 | SaaS 后端：违反"无闭源运行时依赖" |
| 更新 | Tauri updater + Ed25519 签名清单 | 内置、跨平台 | 自建更新服务：维护成本 |

### 1.1 Tauri 选型的附带条件（评审后加严）

Tauri 的真正代价不在体积，在 **Windows 10 的 WebView2 缺失**。20H2 之前的机器上引导安装经常失败，而这批机器恰恰属于最不愿意看报错的普通用户。90MB 是一次性成本，WebView2 引导失败的支持成本是持续的。

**接受 Tauri 的前提条件**（否则应改用 Electron）：

1. 安装器**内置 Evergreen Bootstrapper**，不依赖网络下载
2. 引导失败时给**可操作的降级路径**（明确提示 + 手动安装链接 + 自动重试），而不是一句"缺少组件"
3. 安装包体积按"Electron 上限 90MB + Python 70–100MB"做最坏估算，确认仍可接受
4. 验证清单必须覆盖**无 WebView2 的 Win10 干净机**（见 §10 第 5 条）

若第 1、2 条做不到，Tauri 省下的 90MB 会以更高的支持成本还回去。


---

## 2. 进程模型

```
AgentOS.app / AgentOS.exe          ← Tauri 宿主（Rust）
 ├─ WebView 进程（系统托管）
 ├─ Daemon 子进程（Python）
 └─ Worker 子进程（按需，受 Job Object / Seatbelt 约束）
```

### 2.1 所有权边界（与 PRD §1.2 一致）

| 进程 | 拥有 | 不拥有 |
|---|---|---|
| Host (Rust) | OS 权限、Keychain/DPAPI、文件监护、签名验证、进程树管理 | 任务状态、模型调用 |
| Daemon (Python) | 任务状态、事件日志、上下文、工具路由 | OS 权限、不直接弹系统授权框 |
| WebView | 交互 | 执行 |

**Host 是 Daemon 的父进程并负责监护**：Daemon 崩溃 → Host 检测到 → 指数退避重启（1s, 2s, 4s, 8s，上限 30s）→ Daemon 启动时执行崩溃恢复（扫"有 intent 无 result"）。

### 2.2 单实例

| 平台 | 机制 | 注意 |
|---|---|---|
| macOS | 锁文件 `flock` + `open` 事件转发（自定义 URL scheme `agentos://`） | 不要用 `LSMultipleInstancesProhibited`，它会让"打开第二个文件"变成静默失败 |
| Windows | 命名互斥体 `Local\AgentOS.Single` + 管道转发 argv | 用 `Local\` 不用 `Global\`：多用户会话互不影响；`Global\` 需 SeCreateGlobalPrivilege |

Daemon 单实例另用一层：`daemon.lock`（PID + 启动时间 monotonic）。判定"陈旧锁"必须同时满足：PID 不存在 **或** 该 PID 的启动时间不匹配（防 PID 复用）。这条直接对应 PRD F8 的租约语义。

### 2.3 生命周期

- 关闭窗口 ≠ 退出：最小化到托盘，Daemon 继续跑（PRD §1.2 第一原则）
- 显式退出：Host 先向 Daemon 发 `shutdown(reason=user)`，等 5s，超时杀进程树
- 开机自启：macOS `SMAppService`；Windows 注册表 `HKCU\...\Run` 或任务计划程序（延迟启动）

---

## 3. Python Daemon 的分发（本项目最大的工程风险）

### 3.1 做法

```
resources/
  python/
    bin/python3            # python-build-standalone
    lib/python3.12/...
  site-packages/           # 预解析、预编译的 wheel
  agentos_daemon/          # 我们的包
```

启动方式：`resources/python/bin/python3 -m agentos_daemon --socket <path>`，`PYTHONHOME`/`PYTHONPATH` 显式指定，**不读用户环境变量**（避免被系统 Python 污染）。

### 3.2 三条必须遵守的规则

1. **不装到用户系统 Python**。永远用内置解释器，`sys.executable` 只允许指向 resources 内路径。
2. **不联网装依赖**。所有 wheel 在打包时解析完（`uv pip compile` → `uv pip install --target`），运行时离线。
3. **不做 `pip install`**。用户机器上执行 `pip` 是供应链攻击面。
4. **锁定构建版本并校验 SHA256**。只写 `cpython-3.12.16` 不够——要锁定 `python-build-standalone` 的具体 release tag（如 `20261026`）与每个产物的 SHA256，并在 CI 里校验。

> 补充代价（评审提出）：python-build-standalone 由 Astral 构建，我们**不控制它的供应链**。禁止用户在机器上跑 pip 省下的攻击面，等于转移到了构建期。第 4 条就是为堵这个口子。


### 3.3 体积预算

| 组成 | macOS (arm64) | Windows (x64) |
|---|---|---|
| Python + stdlib | ~42 MB | ~38 MB |
| site-packages（含 numpy/pydantic 等） | ~60–90 MB | ~70–100 MB |
| Rust 宿主 + WebView 资源 | ~9 MB | ~11 MB |
| 合计（压缩后 dmg/exe） | ~55–75 MB | ~60–85 MB |

> 如果超 120MB，先看是不是把 `torch` 之类的巨物带进来了——v1 不需要。

---

## 4. IPC 协议

### 4.1 传输

| 平台 | 端点 |
|---|---|
| macOS | `~/Library/Application Support/AgentOS/run/agent.sock`（权限 `0700`，目录 `0700`） |
| Windows | `\\.\pipe\AgentOS-{用户SID}`（默认 ACL 仅当前用户） |

### 4.2 帧格式

```
[4 bytes BE length][payload]
payload = JSON（v1）/ msgpack（后续，需版本协商）
```

不选 HTTP/gRPC 的原因：Windows 上监听 TCP 会触发防火墙授权弹窗，对普通用户是灾难性体验；且端口冲突无解。

### 4.3 握手

```
→ hello {protocol: [1,2], client: "host/1.0.0", pid}
← welcome {protocol: 2, daemon_version, event_seq_head}
```
版本用**区间协商**（PRD §5 同款），不取单一版本号。

### 4.4 消息类别

`rpc`（请求-响应）、`event`（单向推送）、`stream`（流式分块）。

**关键约束**：事件日志是唯一事实源，IPC 只是传输。Host 掉线后重连，Daemon 用 `event_seq_head` 让 Host 补齐缺口——**UI 状态永远可以从事务日志重建**（PRD 红线 3）。

---

## 5. 平台能力矩阵

| 能力 | macOS | Windows | v1 |
|---|---|---|---|
| 进程创建 | `posix_spawn` | `CreateProcess`（需 `CREATE_SUSPENDED` + Job Object 关联） | ✅ |
| 终端 PTY | `forkpty` | **ConPTY**（1809+，`CreatePseudoConsole`） | ✅ |
| 信号 | POSIX 信号 | 无。用 `GenerateConsoleCtrlEvent` / Job Object 整体终止 | ✅ |
| 文件监视 | FSEvents | `ReadDirectoryChangesW` | ✅ |
| 沙箱 | Seatbelt profile + TCC | **AppContainer / restricted token + Job Object** | ✅（分级） |
| 屏幕截取 | ScreenCaptureKit（需 TCC 屏幕录制） | Windows.Graphics.Capture | macOS only |
| 输入注入 | CGEvent（需 TCC 辅助功能） | `SendInput`（**必须在交互会话**，不能在 Session 0） | macOS only |
| 无障碍树 | AXUIElement | UI Automation (UIA) | macOS only |
| 密钥 | Keychain Services | DPAPI（`CryptProtectData`，per-user） | ✅ |
| 托盘 | NSStatusItem | Shell_NotifyIcon | ✅ |
| 签名 | Developer ID + notarytool | Authenticode（EV / Azure Trusted Signing） | ✅ |

---

## 6. 沙箱：分级，不追求跨平台等价

**先说清楚一件事**：做不到两端等价的强隔离。macOS 的进程级沙箱（`sandbox-exec`）早已弃用，第三方 App 真正可用的只有 App Sandbox（需 App Store / 特定 entitlements）与 TCC。Windows 的 AppContainer 能力更强但改造量大。

所以采用**四层防御，逐层加码**：

| 层 | 机制 | 跨平台一致 | 作用 | v1 交付状态 |
|---|---|---|---|---|
| L1 | 文件代理：所有路径在 Host 内规范化后匹配 allowlist | ✅ 完全一致 | 防路径逃逸（`..`、符号链接） | ✅ 交付 |
| L2 | 进程树 Job Object / 进程组 | ✅ | 防孤儿进程、限制 CPU/内存/时长 | ✅ 交付 |
| L3a | Windows AppContainer / restricted token | ❌ | 防越界读 | ✅ 交付 |
| L3b | macOS Seatbelt / TCC | ❌ | 防越界读 | ⛔ **标 `unsupported`** |
| L4 | 容器后端（可选） | ✅ | 高风险任务 | ✅ 交付（可选启用） |

**L3b 的矛盾要明说。** 上一版把 Seatbelt 列进 v1 交付表，同时又承认 `sandbox-exec` 已弃用、第三方 App 只有 App Sandbox 与 TCC——这是自相矛盾的表述。定稿：

- macOS v1 的隔离**只承诺 L1 + L2**，L3 标 `unsupported`
- UI 上"隔离级别"如实显示为**「仅路径控制」**，不显示"四层防御"
- 这正好用上 F2 那条"后端执行不了必须返回 `unsupported`，禁止静默放宽"——与其声称有 L3 再靠免责声明补救，不如让后端明确说做不到
- 需要强隔离的任务 → 走 L4 容器（无凭据 + 副本工作区）

**诚实结论：macOS 用户看到的隔离级别确实比 Windows 低一档。** 这个差距本来就在，现在只是不再藏在措辞后面。

由此推出 PRD §8.1 的那条 UI 声明：macOS 上未使用容器的任意 shell 执行，必须标为**「受信任执行」**，不得宣称仍受目录 capability 限制。


---

## 7. 平台差异陷阱（每条都踩过才会写）

### 7.1 macOS 文件名是 NFD

APFS 存的名字是分解形式（`ü` = `u` + 组合符）。Python `os.listdir` 原样返回。后果：中文/带音标文件名比较失败、UI 显示重复项。

**处理**：所有路径进入系统时 `unicodedata.normalize('NFC')`；与磁盘交互时再转回，或统一用字节级比较。

### 7.2 Windows 路径

- 长路径 >260 需 manifest 开 `longPathAware` + `\\?\` 前缀
- 保留名：`CON, PRN, AUX, NUL, COM1-9, LPT1-9`（不能创建）
- 大小写不敏感但保留大小写；`C:\Foo` 与 `c:\foo` 同一文件
- **`\\?\` 前缀会绕过路径规范化**，与 L1 的 `..` 检查冲突——必须先用 `Path::canonicalize` 再去前缀

### 7.3 ConPTY 与信号（已定稿为固定两段，不允许"或"）

Windows 没有信号。上一版写成"强杀**或**先发 CTRL_BREAK 再给 5s"，这等于把优雅期变成可选项——而 PRD 的 INV-5（超时结果未知即不可重放）恰恰依赖这段优雅期：进程要有机会在被杀前把 result 写出来。**定稿为固定两段，没有第二种走法：**

| 步骤 | 动作 | 条件 |
|---|---|---|
| 1 | `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pgid)` | 进程有控制台时执行 |
| 2 | 等待 ≤ 5000 ms | — |
| 3 | `TerminateJobObject` 强杀整个 Job | 无论第 1 步是否执行 |

**无控制台的进程跳过第 1 步，但必须在 result 事件里标 `grace_skipped: true`。** 恢复流程看到该标记即按 `effect_state = unknown` 处理，转人工核查。

代价：无控制台进程永远走人工核查。但这比"静默强杀后误判为已完成"安全——后者会产生一份"成功"的假记录。


### 7.4 macOS 签名链

必须签：宿主二进制、**内嵌的每一个 dylib / .so / Python 可执行文件**、WebView 资源。漏签任何一个 → notarization 失败或 Gatekeeper 拒绝启动。

顺序必须是**由内向外**：`codesign` 内层 `.so` → 框架 → 宿主 → dmg。`--deep` 已不推荐。

### 7.5 Windows SmartScreen

新证书即使 EV 也要靠声誉积累，早期用户会看到"未知发布者"。缓解：EV 证书 + Microsoft Store 分发（可选）+ 首次安装说明页。**这是产品问题不是技术问题，必须提前告知用户。**

---

## 8. 构建与发布

### 8.1 四个构建目标（不是两个）

| target | Rust triple | Python 解释器 | 说明 |
|---|---|---|---|
| macOS arm64 | `aarch64-apple-darwin` | `cpython-3.12.16-aarch64-apple-darwin` | |
| macOS x64 | `x86_64-apple-darwin` | `cpython-3.12.16-x86_64-apple-darwin` | |
| Windows x64 | `x86_64-pc-windows-msvc` | `cpython-3.12.16-x86_64-pc-windows-msvc` | |
| Windows arm64 | `aarch64-pc-windows-msvc` | `cpython-3.12.16-aarch64-pc-windows-msvc` | **需先过 §8.3 依赖审计** |

**Python 无法跨架构 lipo**，四个 target 各自独立打包。python-build-standalone 已确认提供全部四种（含 `aarch64-pc-windows-msvc` 的 3.12.16）。

### 8.2 不签名分发（已定：不接受签名成本）

**决定：不做 Apple Developer Program（$99/年），不做 Authenticode 证书。** 产物未签名。

这不是"少一步"，而是把信任问题从"付钱"变成"用户操作"。逐平台说清代价：

#### macOS（真正的难点）

机制是 `com.apple.quarantine` 扩展属性 + Gatekeeper。未经公证的应用带隔离标记，启动被拒。

| 场景 | 结果 |
|---|---|
| 用户用浏览器下载 dmg → 双击 | **被拦截**，提示「无法打开，因为 Apple 无法验证」（文案随系统版本变化） |
| 用户想绕过 | macOS 15 起 Apple 收紧了「右键→打开」这条老路径，可靠做法是去「系统设置 → 隐私与安全性 → 仍要打开」，**不同小版本的具体路径需实测确认** |
| 用户在终端 `xattr -d com.apple.quarantine` | 可用，但面向普通用户不现实 |

**推荐的分发形态：安装脚本而非 dmg。**

```sh
# 关键点：curl 下载不携带 quarantine 属性
curl -fsSL https://…/install.sh | bash
```

理由：通过 `curl` 落盘的文件**默认没有 `com.apple.quarantine`**（只有设置 `LSFileQuarantineEnabled` 的应用才会打标记）。所以"脚本安装"天然绕开 Gatekeeper，不需要用户理解任何安全概念。代价是必须提供脚本审阅与 SHA256 校验，且不能让用户 `curl | bash` 盲跑——脚本内容必须短到能一眼读完。

**必须提前告知用户的三件事：**

1. **TCC 权限可能在每次更新后重置。** 未签名/临时签名的构建，每次更新二进制签名变化，macOS 可能把 app 当成"新应用"，重新索要辅助功能与屏幕录制权限。**这会让 CU 功能在每次更新后都要用户重新授权一次**——这是不签名最持续的体验成本。
2. 首次运行会提示网络/文件访问，全部需要用户在系统设置里手动允许。
3. 未经公证 = 出了问题 Apple 不背书；安全责任完全在项目方与用户。

#### Windows（明显好办）

| 场景 | 结果 |
|---|---|
| 运行未签名 exe | SmartScreen 警告「Windows 已保护你的电脑」 |
| 绕过 | 点「更多信息」→「仍要运行」，**两步，符合用户既有习惯** |
| 缓解 | 提供 SHA256 + 走 `winget`；包管理器安装的体验优于裸 exe |

Windows 侧未签名的代价可接受，不需要特别方案。

#### 仍然安全的两个部分

1. **自动更新仍可验证**：Tauri updater 用我们自己的 Ed25519 密钥签更新清单，与 Apple/Windows 代码签名无关。更新链路不因不签名而失守。
2. **依赖完整性仍可验证**：`uv.lock` 哈希锁定 + python-build-standalone 的 SHA256 校验（§3.2 第 4 条）。

#### 我的立场（供你复核）

$99/年 ≈ ¥700，是这个项目里最便宜的一笔基础设施支出，比它带来的支持工单量便宜得多。**但决定权在你，已按"不签名"落地。**

如果哪天要改主意，成本最低的顺序是：先上 Apple Developer Program（解决 macOS，这是唯一真正卡用户的地方）→ Windows 继续不签（SmartScreen 可接受）。

补充：不上架商店换来的好处是**可以合法地做"默认可写工作区"**（PRD D3），App Sandbox 的强制约束不存在了。这是净收益，与签名与否无关。

### 8.3 四目标二进制门禁（替代包名黑名单）

**先更正一处事实错误。** 上一版断言 `cryptography` "3.43 到 50.0 全无 win_arm64 轮子"，并据此立了"永久禁止引入"的规则。这是错的：

| 版本 | win_arm64 轮子 |
|---|---|
| 46.0.3 | **3 个**（含 `cp311-abi3-win_arm64`） |
| 47.0.0 / 48.0.0 / 50.0.2 | 0 |

实际情况是**该库对 Windows arm64 的轮子覆盖是间歇性的**——46.0.3 有，47 之后消失。据此立包名黑名单有两个后果：一是依据不成立；二是黑名单会永久冻结整个依赖图（`cryptography` 是大量包的传递依赖），杀伤力远大于多支持一个架构。

**替代规则：不按包名禁止，按目标构建结果判定。**

```
for target in [mac-arm64, mac-x64, win-x64, win-arm64]:
    uv pip install --target "build/$target" \
        --python-platform $target --only-binary :all: \
        -r requirements.lock          # 带哈希锁定
    # 在 natively 对应架构的 runner 上做 import/启动冒烟测试
```

四条约束：

1. **`--only-binary :all:`**：禁止任何目标尝试源码编译，缺失即失败
2. **哈希锁定**（`uv.lock` / `requirements.lock` with hashes）：构建可复现
3. **原生运行验证**：`--python-platform` 只影响解析，不证明能 import。四个目标要在对应架构的真机/CI runner 上各跑一次 `python -c "import agentos_daemon"`
4. **失败时的处理顺序**：先看是否有可用版本 → 再看是否可源码构建 → 最后才做功能降级。**降级必须记录在案，不能静默**

**当前已知风险项（v1 需要注意，但不是禁令）**：

| 包 | 现状 | 处理 |
|---|---|---|
| `cryptography` | 46.0.3 有 arm64，47+ 无 | 若必须用，锁 46.x 并做安全例外登记；否则 TLS 走 CPython 内置 `ssl`（自带 OpenSSL），JWT 用纯 Python `pyjwt` |
| `tiktoken` | 无 arm64 轮子 | 不引入。token 计数由 provider adapter 提供（PRD §5.7），harness 不自算 |

已确认**有** arm64 轮子的：`pydantic-core`、`numpy`、`pillow`、`httptools`、`msgspec`、`orjson`、`regex`、`watchfiles`；纯 Python 通用轮子：`httpx`、`pyjwt`、`uvicorn`。

**为什么不砍掉 arm64**：Windows x64 + arm64 是用户明确要求的范围。依赖问题是构建方案要解决的问题，不是砍架构的理由。若某依赖确实无解，处理方式是"该功能在 arm64 降级并如实告知"，而不是"不支持 arm64"。



---

## 9. 目录布局

| 平台 | 数据 | 缓存 | 日志 |
|---|---|---|---|
| macOS | `~/Library/Application Support/AgentOS/` | `~/Library/Caches/AgentOS/` | `~/Library/Logs/AgentOS/` |
| Windows | `%LOCALAPPDATA%\AgentOS\` | `%LOCALAPPDATA%\AgentOS\cache\` | `%LOCALAPPDATA%\AgentOS\logs\` |

> Windows **不用 `%APPDATA%`（Roaming）**：事件日志和快照会撑爆漫游配置同步。

内部结构：

```
AgentOS/
  events/            # 一事件一文件（OpenHands 式）
  artifacts/blobs/<sha256>
  snapshots/<run_id>/  # 可回滚快照
  memory/sqlite.db
  run/agent.sock|pipe
  daemon.lock
  logs/
```

---

## 10. 验证清单（M0 前必须过）

1. 两个平台各起一次完整 run，事件日志能从零重放出同样结果
2. Host 强杀 Daemon（`kill -9` / `TerminateProcess`），重启后恢复正确、无重复副作用
3. 同时启动两个实例：第二个只转发 argv，不创建第二个 Daemon
4. macOS：TCC 拒绝"辅助功能"时，CU 功能降级而不是崩溃
5. Windows：**无 WebView2 的 Win10 干净机**上，安装器内置 Bootstrapper 能自行装好；装不上时给出可操作降级路径（不是一句报错）
6. 两端各构建一次带签名的包，在干净机器上安装运行（不再依赖开发环境）
7. 文件名带中文/带重音符号，两端读写一致
8. **四个构建目标各自通过 import 冒烟测试**（不是只过 `--python-platform` 解析）
9. Windows 无控制台进程被取消时，result 带 `grace_skipped: true`，恢复走人工核查而不是判为成功
10. **未签名安装实测**：macOS 用脚本安装后能直接启动（无 quarantine 拦截）；用浏览器下载 dmg 时拦截图与文案记录在案，README 对应说明与实测一致
11. **TCC 重置实测**：安装 v1 并授权辅助功能 → 覆盖安装 v2 → 检查权限是否被重置。**这条决定更新流程要不要加权限引导**
12. Windows 未签名 exe 首次运行，SmartScreen 的「仍要运行」路径可用



---

## 11. 已定与待定

### 11.1 已定（2026-11-08）

| 项 | 决定 | 影响 |
|---|---|---|
| 分发渠道 | **不上架商店，只 GitHub 开源** | 摆脱 App Sandbox 强制约束，可做"默认可写"；但签名/公证成本落在自己身上 |
| macOS 最低版本 | **13.0** | ScreenCaptureKit（12.3+）、SMAppService（13+）都可用；TCC API 为现代版 |
| Windows 架构 | **x64 + arm64 双支持** | 四个构建目标；arm64 依赖审计成为 M0 门禁（见 §8.3） |
| 代码签名 | **不签名（不接受 $99–600/年）** | macOS 走安装脚本分发绕开 quarantine；Windows 容忍 SmartScreen；**代价是更新后 TCC 权限可能重置**（见 §8.2） |
| 诊断包 | **仅本地导出，永不上传** | 无崩溃自动上报通道；导出前默认脱敏，因为最终会贴到公开 issue |


### 11.2 仍需回答

| ID | 问题 | 为什么必须早定 |
|---|---|---|
| **T2** | Windows arm64 的"功能子集"边界在哪（CU 不支持？哪些工具缺失？） | 四 target 全功能等价的成本远高于三 target + 一个受限 target |
| **T3** | 更新渠道：Tauri 自建 updater 还是走 `winget` / Homebrew Cask？ | 自建 updater 要维护签名清单与差分包；包管理器则放弃推送能力 |

> T1（签名成本）已定：**不接受**，见 §8.2。随之而来的两个新问题：
> - macOS 安装脚本用 `curl | bash` 形态，**是否接受**这种形态对安全敏感用户的劝退效果？（替代：提供脚本源码 + 用户手动下载运行）
> - 不签名后，TCC 权限在更新后是否真的重置，**必须先实测**再决定更新流程是否加权限检查步骤。


### 11.3 arm64 的取舍（评审裁决后定稿）

评审中有意见提出"砍掉第 4 目标，arm64 降为实验性"，**已驳回**。理由：Windows x64 + arm64 是用户明确要求的范围；且该意见的前提（`cryptography` 无 arm64 轮子）经复核不成立。

**定稿口径：**

- 四个目标**全部进 CI**，任一失败即阻断发布——不设"实验性目标"
- 允许的降级粒度是**按功能**，不是按架构：某个依赖在 arm64 无解时，该功能在 arm64 上降级并在 UI/README 如实标注
- 禁止的降级是**静默**：不允许"构建过了但少个能力"而不声明
- M0 不为四条流水线扩容，四目标并行构建的工时上限按 3 天计

装机量小是事实，但它影响的是**优先级**，不是**要不要支持**。



## 12. 模型接缝：接上第二个线上协议之后的结论（M3）

M0–M2 只有一家 OpenAI-compatible 端点，所以"换模型 = 换 adapter"这句话一直
没被真正顶过。M3 接上 Anthropic Messages API 之后，有三条结论值得写在这里。

**12.1 内部消息形状是契约，不是实现细节**

主循环、游标、`messages_hash`、`replay` 共用同一份**规范化消息形状**
（`system` / `user` / `assistant`+`tool_calls` / `tool`）。它必须中立：
两个协议的差异（`system` 在顶层、工具结果要包 `tool_result`、`tools` 要不要
再包一层 `{"type":"function"}`、`max_tokens` 必填）全部由 adapter 消化。
adapter 一旦反向改写内部形状，replay 重建的消息序列与当时发出去的就不再一致，
`messages_hash` 会立刻对不上——这不是风格问题，是证据链断裂。

**12.2 错误是对象，不是结束原因**

`finish_reason` 回答"模型这一轮怎么结束的"，13 值封闭集。401/429/连接被掐断
压根不是模型的选择，压成 `finish_reason="error"` 的代价是：策略层想知道
"该重试还是该换 key"时无从判断，而且事件日志里多了一个契约不允许的值。
M3 起一律抛 `ModelError(kind, status_code, retryable)`；主循环原有的
`except Exception` 分支记 `model.error` 事件后终止——**这一处 0 改动**。
`retryable` 目前**没有调用方**：它的存在是为了让上层的重试策略将来能判断，
而不是现在就自动重试（自动重试会掩盖 429 与持续 5xx，还会把预算悄悄花掉）。

**12.3 判据是"同一套契约测试跑两个实现"，不是"我加了个 adapter"**

`tests/test_model_contract.py` 用注入的 `Transport` 对两个 adapter 各跑一遍
同一组断言（结尾、工具调用、半截 JSON、401/403/429/500、非 JSON 响应体、
未知 `stop_reason`、缺 usage、缓存 token）。任一 adapter 满足不了，
说明它不合格，不说明测试要放宽。

**12.4 还没做的部分（不要在别处说成已经做了）**

- 沙箱后端接缝（M3 另一半）见 `docs/superpowers/notes/m3-backend-seam-proposal.md`
- 流式（`StreamEvent`）留着契约，v1 不用
- `ContinuationRef` 的签名/thinking 原样往返：两个 provider 都没有 v1 必须
  往返的 opaque 字段，Anthropic 的 `thinking` 块当前**跳过**，不做往返
- 这不是 OS 级隔离：`Workspace` 是路径边界，不是安全边界
