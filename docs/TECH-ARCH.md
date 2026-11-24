# 技术架构书（TA）— Windows / macOS 桌面 App

> 配套 [PRD-v1.md](PRD-v1.md)。本文包含目标架构与实际接线；设计要求不代表已交付。当前源码地图见 §13，验证状态见 [status.md](status.md)。
> 长期目标：macOS 13+（arm64 / x64）、Windows 10 1809+ / 11（x64 / arm64），不表示这些环境已验收。首发按 [PRD §11.1](PRD-v1.md) 仅准备 `macos-arm64` 限定预览，支持版本以实机验收为准。

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
| 本地存储 | SQLite（WAL）append-only 单提交点（事件表 + 同库投影） | 单用户场景足够；事务与回放简单（PRD §5.3 已修正早期「一事件一文件」草案，2026-11-14 对账） | 嵌入式 KV：查询能力不足 |
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

R0–R3 桌面候选在 M5 拓扑外增加 Tauri 薄壳：

```text
Tauri 桌面壳（Rust）
 ├─ 系统 WebView（加载 Host 提供的 React UI）
 └─ Python Host（单实例、HTTP/SSE 桥、监护 Daemon）
     └─ Python Daemon（IPC、任务与执行线程）
         └─ 受控命令子进程（默认 local；可选原生沙箱见 §6）
```

无 Tauri 时，系统浏览器也可连接 Python Host。现行职责见 [M5 桌面规格](M5-desktop.md) 与 [桌面壳说明](../desktop/README.md)。下文涉及自启和系统进程管理的设计项不代表已交付。

### 2.1 所有权边界（与 PRD §1.2 一致）

| 角色 | 已有职责 |
|---|---|
| Tauri 薄壳 | 窗口、托盘、单实例、启动或附着 Python Host |
| Python Host | 提供 UI、转发 IPC、启动和监护 Daemon |
| Python Daemon | 任务、回放恢复、事件日志、上下文、工具路由 |
| WebView / 系统浏览器 | 展示状态及收集用户操作；不负责工具执行 |

Daemon 启动时先扫描未收尾的执行，再对外服务。桌面壳和 Host 的监护分别对应各自管理的子进程。

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
  tianshu/                 # 我们的包（旧名 agentos_daemon 从未存在，2026-11-14 对账修正）
```

启动方式：`resources/python/bin/python3 -m tianshu daemon --data-root <path>`，`PYTHONHOME`/`PYTHONPATH` 显式指定，**不读用户环境变量**（避免被系统 Python 污染）。**现状注（2026-11-14）**：`--socket` 资源化打包形态是 §3 的打包目标设计、尚未交付；当前交付是 `python -m tianshu daemon`（`cli.py`）+ Host 自动拉起。

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

| 能力 | macOS | Windows | 现行范围 |
|---|---|---|---|
| 命令子进程 | `subprocess.Popen` | `subprocess.Popen` | 默认 `local`，不提供 OS 级隔离 |
| 终端 PTY | POSIX PTY | ConPTY 尚未接入 | Windows 请求 PTY 返回 `PolicyUnsupported` |
| 超时终止 | 尽力终止 POSIX 进程组 | 终止直接子进程 | Windows 不保证整树退出；Job Object 为后续设计 |
| 文件监视 | 目标原语 FSEvents | 目标原语 `ReadDirectoryChangesW` | 本表不承诺系统级文件监视已交付 |
| 沙箱 | Seatbelt（选用 `seatbelt` 后端且系统有可执行的 `sandbox-exec`） | 原生子进程沙箱后端待交付 | W14 同时提供符合内核条件的 Linux Landlock，实测范围见 §6 |
| 截屏 / 输入 / 无障碍 | 目标原语 ScreenCaptureKit / CGEvent / AXUIElement | 目标原语 Windows.Graphics.Capture / `SendInput` / UIA | 不作为当前桌面候选已交付能力 |
| 密钥 | W1 已实现 Keychain Services | 系统凭据后端待交付 | 请求当前平台未实现的 `keychain` 后端时返回 `CredentialUnsupported` |
| 托盘 | Tauri 托盘 | Tauri 托盘 | 源码已接线，用户设备体验待验 |
| 签名 | Developer ID + notarytool | Authenticode（EV / Azure Trusted Signing） | 当前分发方案不做 OS 签名，见 §8.2 |

---

## 6. 沙箱：分级，不追求跨平台等价

平台的系统原语和实测范围各不相同。后端声明用于路由，任务隔离门读取现场绑定的探针报告。历史 F2 探针记录见 [isolation-matrix.md](isolation-matrix.md)，现行 W14 实测见 [交付记录](changelog/2026-11-18-w14-dual-sandboxes.md)。

| 层 | 机制 | 现行交付范围 |
|---|---|---|
| L1 | 文件工具规范化路径后按 allowlist 核对 | 已交付，作用范围为内置文件工具 |
| L2 | 进程树清理 | POSIX 使用进程组。Windows 整树清理缺少 Job Object 保证 |
| L3a | Windows AppContainer / restricted token | 待交付。请求 `app_container` 返回 `PolicyUnsupported` |
| L3b | macOS Seatbelt | W14 可用后端。依赖当前系统提供 `sandbox-exec`，保护范围以探针结果为准 |
| L3 | Linux Landlock | W14 可用后端。内核可用时施加文件约束，TCP 规则需要 ABI 4 或以上 |
| L4 | 容器 | 待交付。请求 `container` 返回 `PolicyUnsupported` |

`exec_backends.py` 的默认后端是 `local`。显式选择 `seatbelt`、`landlock` 或 `auto` 时，平台与原语不可用会返回 `PolicyUnsupported`。`auto` 按当前平台选择可用的原生后端。审批卡片与结果记录使用实际隔离视图，`local` 标为「受信任执行」。

由此推出 PRD §8.1 的 UI 声明：未使用有效隔离的任意代码执行必须标为
**「受信任执行」**，不得宣称仍受目录 capability 限制。

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

### 7.3 ConPTY 与两段终止（目标设计，尚未完整接入）

现有 Windows 命令后端从 `exec.py` 终止直接子进程，不保证整树退出。下表为目标方案，不是现行行为。Windows 没有 POSIX 信号。上一版写成「强杀或先发 CTRL_BREAK 再给 5s」，会将优雅期变成可选项。PRD 的 INV-5（超时结果未知即不可重放）要求给进程留下记录 result 的机会，因此目标设计保留固定两段：

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

**决定：不做 Apple Developer Program（$99/年），不做 Authenticode 证书。** 不做认证发布者代码签名。
macOS 打包路径的免费 outer ad-hoc 资源封套只检查 bundle 结构完整性，不提供发布者身份、
公证或 Gatekeeper 放行；不改变本节 T1 的成本与信任政策，发行索引仍为 `signed: false`。
具体生成和归档往返检查见[桌面发行说明](../desktop/README.md#macos-资源封套与发布者身份不是同一项)。

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
3. **原生运行验证**：`--python-platform` 只影响解析，不证明能 import。四目标依赖准入规则保持不变。发行时，每个实际分发的目标都必须在对应架构上验证 native import 和启动 smoke；arm64 限定预览只要求其自身发行证据，四目标正式发行分别验收（[PRD §11.1](PRD-v1.md)）。CI runner 的 smoke 不是用户设备的安装证据。
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
  events.db          # SQLite WAL append-only 单提交点（2026-11-14 对账：修正早期「一事件一文件」草案，与 PRD §5.3 / events.py 一致）
  artifacts/blobs/<sha256>
  artifacts/snapshots/<run_id>/  # 可回滚快照
  memory_blobs/      # 记忆正文（记忆表与事件同库；正文落库旁目录）
  run/agent.sock|pipe
  run/daemon.lock
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

- 四个目标**全部进 CI**，任一失败即阻断发布（**现状注（2026-11-14）**：当前 required 为三平台 Python 矩阵 + UI 四项；win-arm64 / mac-x64 原生冒烟未进 CI，属 G3 发布前差距，不以目标口径冒充现状）——不设"实验性目标"
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

## 13. 源码模块地图

> 从 README 拆出（README 只保留架构要点与指向这里的链接）。
> 下面只按职责分组；**文件数量以当前目录实测为准**，不在此手写计数。
> 数据根布局见 §9，进程模型见 §2，IPC 协议见 §4。

**唯一事实源是事件日志**：SQLite WAL、append-only（触发器禁止 `UPDATE` / `DELETE`），Task / Run / Approval / Grant / Lease 都是同库事务更新的**可重建投影**。状态变更必须同事务写事件，否则回放无法解释「为什么这个 Run 被 fence 了」。

**M5 的三进程**：UI（浏览器里的 React）→ loopback HTTP + SSE → Host（Python：单实例、监护 Daemon、注入 token）→ IPC（macOS/Linux = AF_UNIX，Windows = 命名管道，`multiprocessing.connection` + `authkey`）→ Daemon（任务 / 事件 / 审批 / 驱动线程）。**UI 断开只减少订阅者**；Daemon 重启时先做恢复扫描（有 intent 无 result 的调用交给既有 `recover_run`、重新排队 `queued`/`parked`），再对外服务。

五道版本化契约与十一条红线约束全局。**C1** 模型 I/O，**C2** 工具，**C3** 事件（封闭注册表，写入侧拒未登记类型），**C4** 权限与审批（[封闭规则清单](PRD-v1.md#541-拒绝规则编号payloadrule_id)），**C5** 评测（`SUITE_VERSION = m4.6`）。

C3 的 `approval.requested.command` 是 exec 审批的只读展示投影：派发侧从已校验 args 复制完整 argv、实际 cwd 和后端名，控制面按 approval_id 回读。该对象不参与策略判定、args/action digest 或审批消费；执行侧仍以原请求与 digest 为准。字段只进入 current v1；冻结的 legacy v0 保持原形状并拒绝该字段。历史缺字段不得自动批准或猜测命令。

```
src/tianshu/          Python 源码 + eval/ 子包（下面只按职责分组；数量以当前目录实测为准）
├─ 事实源与契约 ─────────────────────────────────────────────
│  events.py        SQLite WAL append-only 事件日志 + 可组合事务
│  canonical.py     RFC 8785 (JCS) 规范化 + 独立标准向量
│  rules.py         C4 rule id 封闭注册表（代码 / schema / PRD 同步核对）
│  event_schema.py  C3 逐类型事件 payload 契约（v1 schema + 版本旁表）
│  toolschema.py    C2 工具参数封闭 JSON Schema 子集（编译期拒绝）
│  tool_registry.py 动态工具注册表（内置 SPECS + MCP 运行时准入）
│  errors.py        区分「模型想错了」与「执行侧跳过了校验」（后者是 bug）
├─ 路径与磁盘 ───────────────────────────────────────────────
│  workspace.py     路径规范化 / 资源身份（st_dev:st_ino）/ 0..n 目录绑定
│  pathkeys.py      路径键：大小写 / Unicode 规范化与祖先判断
│  fsutil.py        原子写入：临时文件 → fsync → os.replace → fsync 目录
│  sandbox.py       存储后端接缝：LocalBackend（真实盘）/ MemBackend（进程内，不是沙箱）
│  resources.py     资源种类：把「要操作什么」从文件路径解耦（memory:// 等）
│  authority.py     用户级「权威根」：跨数据根的阻断状态
│  platform_identity.py 从真实已打开的原生句柄读文件系统实体身份
├─ 授权与执行 ───────────────────────────────────────────────
│  permissions.py   C4 动作分类 / 审批 / 一次性 grant
│  dispatcher.py    唯一派发入口：消费审批 + 登记 intent + 守卫内核验后执行
│  execpolicy.py    F2 执行侧命令策略（声明式、默认全拒、deny 优先）
│  exec_backends.py F2 执行后端注册表（未实现/未知一律 unsupported，绝不回落 local）
│  isolation.py     F2 四边界实测探针 + 隔离门 + 受信任执行视图
│  exec.py          F1 执行原语：argv 模式（非 shell）、超时、截断、进程组
│  tools.py         受限工具 + FileExecutor（自己回库核验票据，不信调用方）
│  guard.py         工作区阻断检查（未知副作用不得继续写）
├─ 副作用可撤销 ─────────────────────────────────────────────
│  snapshot.py      写前快照与冲突安全撤销
│  receipt.py       操作凭据（能独立核对，不必信调用方）
│  undo.py          撤销：把一次 Run 的写入还原或删除（本身也走审批/租约/receipt）
│  undo_journal.py  撤销意图日志：跨多文件撤销的可续做边界
│  recovery.py      中断核查：applied / none_applied / unknown 三者不合并
│  crashpoint.py    故障注入点（os._exit 硬退，仅 TIANSHU_FAILPOINT 生效）
├─ 任务、租约、作业 ─────────────────────────────────────────
│  tasks.py         Task / Run / RunCursor 状态机，fencing，控制请求
│  leases.py        跨进程资源租约 + 本机执行守卫（POSIX / Windows）
│  jobs.py          作业认领与 claim_epoch（旧 worker 不得覆盖新 owner）
│  run_policy.py    Run 创建前的执行资格固定（不授予工具权限）
├─ 调度（W5）────────────────────────────────────────────────
│  scheduler.py     确定性最小调度器：定时任务的到点认领与驱动
│  schedule_cli.py  `tianshu schedule` 子命令：定时任务管理面
├─ 记忆（F10）────────────────────────────────────────────────
│  memory.py        事件日志的可重建投影：L0–L3、作用域、forget=tombstone
│  memory_index.py  向量索引第二路（FTS ∪ 语义 RRF；Hash 基线 provider）
│  memory_distill.py L2/L3 确定性合成（场景页 / 偏好页 + freshness）
│  memory_exec.py   受信任的记忆执行器
│  memory_control.py 控制面记忆操作（CLI 与 RPC 共用，写走 dispatcher）
├─ 数据维护与迁移（G3）──────────────────────────────────────
│  data_admission.py 真实事件库只读准入前缀（版本 / 进度校验）
│  data_digest.py   迁移一致点算法 v1
│  data_state.py    迁移进度的严格类型与纯校验
│  migrate.py       标准根离线迁移（一致点绑定、严格续做）
│  maintenance.py   数据库维护准入（协作式维护窗口）
│  backup.py        一致点备份 / 校验 / 材料布局与引用保护
│  data_cli.py      data 子命令：备份 / 校验 / 迁移 / 受管恢复 / 审计 / GC / 诊断导出
│  data_gc.py       W15 保守离线清理：只读预演、独占执行与效果记账
│  disk_admission.py Run 创建前的当前空间保护（只读真实位置，不预留容量）
│  restore_access.py 普通连接和恢复批次的组合准入
│  restore_contract.py 恢复 B/C 的值对象与校验
│  restore_registry.py 用户级受管恢复权威、只追加状态与链观察
│  restore_manager.py 受管导入、隔离复核、续做、中止与解除
├─ 模型层 ───────────────────────────────────────────────────
│  model.py         C1 接缝：adapter 只做双向翻译，不改写内部形状
│  capability.py    F9a 能力声明与准入（只用于路由，不用于授权）
│  credentials.py   F12 凭据分域查找 + 全链路脱敏（Keychain 后端 macOS 已落地）
│  keychain_macos.py macOS Keychain 直调后端（SecItem，仅 darwin 加载）
│  embedding_openai.py OpenAI 兼容 embedding 端点适配器（W6，需数据外发许可）
│  pricing.py       F13 成本账：整数微美元，未知价格记 None 并单独计数
│  budget.py        共享预算账本（PRD §5.6）：token / 金额预占与结算
│  context.py       F5 上下文编译：纯函数、按字节预算、丢弃只记账不摘要
├─ 驱动 ─────────────────────────────────────────────────────
│  loop.py          E1 主循环（持久游标、暂停 / 取消 / 恢复）+ 栈装配
│  worker.py        作业循环：认领排队中的 Run（一次性 drain）
│  replay.py        回放校验：seq / messages_hash / 产物哈希三方核对
│  metrics.py       本地运行质量指标（PRD §10.6）：进程内采样、写数据根
│  cli.py           命令行入口（含 daemon / host 两个常驻入口）
├─ 网络 / MCP / 摘要（G2）────────────────────────────────────
│  net.py           network.fetch 执行器（GET、同源重定向、1 MiB 上限）
│  mcp.py           MCP stdio 客户端（三道门、封闭方言准入）
│  summaries.py     模型语义摘要的记录与材料身份（PRD §7.1）
│  contents.py      内容引用：大结果的有界摘要 + 可分页读取的引用
├─ 桌面三进程（M5）─────────────────────────────────────────
│  ipc.py           本地 IPC：AF_UNIX / 命名管道、版本区间协商、推送游标、断线语义
│  daemon.py        常驻守护进程：锁、恢复扫描、请求线程、驱动线程、优雅退出
│  control.py       RPC 方法 → 既有库函数的映射（不自己写文件、不重判策略）
│  host.py          外壳：单实例、监护 Daemon、UI 的 HTTP/SSE 桥、token 注入
└─ eval/            M4 评测：20 条 case、干净 fixture、可比分数与失败归因
```

平台标准数据目录、单实例判据、断线语义与恢复顺序的完整规格见 [M5-desktop.md](M5-desktop.md)；
产品规格见 [PRD-v1.md](PRD-v1.md)（§5 契约、§6 红线）与 本文件 §1–§12。

UI 在 `ui/`：Vite + React + TS，构建产物 `ui/dist` 已提交（Host 直接加载，运行时不需要 Node）。
