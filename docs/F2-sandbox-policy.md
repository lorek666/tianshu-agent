# F2 沙箱与 policy：交付说明（2026-11-11）

关联：PRD §7 F2 / §8.1 / §8.1.1；[TECH-ARCH.md](TECH-ARCH.md) §6；
[isolation-matrix.md](isolation-matrix.md)；[F1-exec-primitive.md](F1-exec-primitive.md)。
规格与判据：[SPEC-F2-2026-11-11.md](SPEC-F2-2026-11-11.md)。

**一句话**：F2 的 P0 不是"造沙箱"，而是把三件事做成**可执行、可测、不可静默
放宽**的机制：后端可选且做不到要 `unsupported`；隔离要求只认实测证据；未隔离
执行在**审批前**就被标成「受信任执行」。

---

## 1 三个机制

### 1.1 后端注册表（`src/tianshu/exec_backends.py`）

- `ExecBackend` 协议：`capabilities`（如实声明）+ `spawn()`（真正创建进程）。
- 进程级选择：`TIANSHU_EXEC_BACKEND`，默认 `local`；注册表键 `"local"` 稳定，
  平台差异在 `capabilities.name`（`local-posix` / `local-windows`）。
- **已知未实现的后端**：`app_container` / `container` / `seatbelt` / `landlock`
  → `PolicyUnsupported(feature="exec_backend")`，**绝不回落到 local**；拼错的名字
  同样拒绝并列出可用名字。
- 注册是**代码级**能力：`register_exec_backend()` 同名替换要显式 `replace=True`；
  注册未实现名字还要求 `capabilities.name` 自报同名、`capabilities.isolation`
  与名字相称——本地代理不能顶替 `container`。
- 装配层在**构造期**解析后端：配置错误启动就炸，不拖到第一条命令。
- 产品路径：`dispatcher._precheck_exec`（审批前）与 `tools._run_command`（执行）
  用**同一个后端对象**；`Dispatcher(executor=...)` 的自定义执行器也要核。
  调用方先 `check_spawn()`（+ 本次 `pty=True` 时的能力判定），后端内部再核一次
  ——防御纵深，不是把策略判定外包给后端。

### 1.2 隔离要求与实测门（`src/tianshu/isolation.py`）

- 策略字段 `ExecPolicy.isolation_requires`：PRD §8.1.1 的四条**边界名**
  （`files_authority` / `network` / `credentials` / `process_tree`），封闭枚举、
  去重保序；空 = 不声明隔离要求。
- 门 `require_isolation_requirements(policy, backend, workspace=..., data_root=...)`：
  - 要求非空 → 必须有**绑定本次现场**的 `IsolationReport`
    （`workspace` / `data_root` / `probe_version` 全部一致）；
  - 逐条 `require_isolation()`：`unknown` 与 `not_enforced` **一样拒绝**；
  - 失败发生在 `backend.spawn()` **之前**，没有"退回不隔离"的分支；
  - 执行路径没有 `data_root` → 不探测、直接拒绝（不拿临时目录假装测过）。
- **边界独立、等级累计**：门只核"请求了哪条边界"；`isolation_view.level`
  （`measured_level`）只是累计显示。因此可能出现"门放行 `process_tree`、
  `level=none`"——此时视图必须带 `enforced_boundaries`，UI 显示
  "受信任执行：未隔离（已实测的边界：进程树）"。
- 探针缓存 `probe_backend_cached` 的条目绑定
  `(后端对象 identity, PROBE_VERSION, platform, workspace, data_root, timeout)`：
  换数据根 / 换同名实现 / 探针逻辑升级都会重测。

### 1.3 「受信任执行」的审批前可见性

- `ApprovalStore.request(isolation=...)` 写入 `approval.requested`；
  `dispatch.requested`（exec 的审批路径）带同一视图；
- `control._approval_context()` 从事件回读 → `ApprovalView.isolation` 字段 →
  UI 审批卡片（`ApprovalPanel`）在用户点"允许"之前显示人话；
- 执行结果：`run_command` 输出恒带 `output.isolation`（有实测按报告；
  无实测如实 `unverified`）；rejected 分支同样带视图；
- 非 exec 的 auto 路径**不带** `isolation`（可选字段，不存在 ≠ null）。

UI 映射优先级（`ui/src/lib/humanize.ts::isolationSentence`，顺序反了就是撒谎）：

1. `contradicted`（自述与实测冲突）→ warn
2. `unverified` 且 `declared != "none"`（后端自称隔离但没测）→ warn
3. `level == "none"` 且有部分边界已实测 → warn，列出已实测边界
4. `level == "none"` → 「受信任执行：未隔离」
5. 其它等级 → 对应中文

---

## 2 本机实测（macOS arm64，2026-11-11）

`docs/isolation-matrix.md` §2 有原始输出。结论：

| 边界 | 本机 `local-posix` 实测 | 含义 |
|---|---|---|
| `files_authority` | `not_enforced` | 子进程能读写工作区外文件、能开权威库 |
| `network` | `not_enforced` | 子进程能连 loopback |
| `credentials` | `not_enforced` | 子进程能读到父进程环境里的哨兵 |
| `process_tree` | `enforced` | 超时后按进程组杀树，孙进程确认消失 |

`measured_level = none`（累计等级要求 files+tree，本机 files 已失守），
但 `enforced_boundaries = ["process_tree"]`。

**所以：** 声明任意非空 `isolation_requires` 在本机都会 `unsupported`
（包括只要求 `files_authority`）。这是诚实结果，不是配置错误。

---

## 3 与 PRD 判据的对应

| 判据 | 落地 |
|---|---|
| F2-c1 后端可插拔 | `exec_backends.py` 注册表 + 装配接线（PR #108） |
| F2-c2 L3 标 unsupported | 四个未实现后端名 → `PolicyUnsupported`；TECH-ARCH §6 表已改 |
| F2-c3 声明式/默认全拒/deny 优先 | `execpolicy.py`（既有，未改语义） |
| F2-c4 做不到必须 unsupported | 后端选择 + 能力判定 + 隔离门三处都不回落 |
| F2-c5 受信任执行必须可见 | 审批前 `ApprovalView.isolation` + 结果/事件 + UI（PR #109） |
| F2-c6 四边界实测证据 | `isolation.py` 探针 + 门接入产品路径 |
| F2-c7 进程树（L2） | POSIX 交付；Windows ⛔（见 §7 决策） |

---

## 4 怎么接一个新的执行后端

1. 实现 `capabilities: ExecBackendCapabilities` 与
   `spawn(argv, *, cwd, env, policy, blob_dir=None, pty=False)`；
2. 如实声明 `isolation`（`none` / `path_only` / `process_tree` /
   `app_container` / `container`）与各 `supports_*`；
3. `register_exec_backend(backend, name=...)`；
4. 用 `isolation.probe_backend()` 产出该后端 × 平台的实测报告，写进
   `docs/isolation-matrix.md`——**没有实测报告就不能声明任何 `enforced`**；
5. 把 `TIANSHU_EXEC_BACKEND` 指过去，并跑隔离相关测试。

**不能做的**：只把 `capabilities.isolation` 字符串改成更强——门只认探针报告。

---

## 5 明确不做什么

- 不实现 Seatbelt / Landlock / AppContainer / 容器（PRD F2 定调；`container`
  等名字只是"已知未实现"的注册项）。
- 不做 per-run 后端切换（进程级，与文件后端 `sandbox.py` 同一纪律）。
- 不做跨平台等价强隔离——四边界探针只覆盖列出的五类行为，不等于"安全"。

---

## 6 未验证（与验收报告一致）

- Windows **实机**：无；Job Object 未实现，进程树 `unknown`。
- macOS x64 / Linux x64 / arm64 无实机，矩阵保持 `unknown`。
- 探针不覆盖：内核攻击面（ptrace/注入）、DNS/出站、磁盘配额、CPU 上限、
  TCC 交互。
- 审批卡片显示的是**装配时**后端视图（可能 `unverified`）；执行结果里的视图才是
  本次执行的证据。
- UI 只到审批卡片 + 时间线文案；没有独立"隔离"面板。

---

## 7 决策记录：为什么 v1 不做 Windows Job Object

**技术上可行**（所以不是"做不到"）：

```
CreateProcess(CREATE_SUSPENDED) → 进程在暂停态
AssignProcessToJobObject(job, hProcess) → 先入 Job
NtResumeProcess / 枚举线程 ResumeThread → 恢复
超时 → TerminateJobObject(job, 1) → Job 内后代一起终止
```

零依赖 `ctypes` 可以走通，不必重写管道/信号。但本轮**选择不做**，理由：

1. 需要 `Popen._handle`（私有）或完整 `CreateProcess`，以及 `NtResumeProcess`
   这类未文档化接口；这是可维护性/兼容性风险，不是"顺手加个 ctypes"；
2. 本机**没有 Windows 实机**，唯一验证渠道是 CI runner；用私有接口写进程控制、
   且无法证明 assign 窗口零逃逸，属于"不可证明的隔离"；
3. 按本项目纪律（"可证明才能声明"），做不到就不声明：
   `supports_job_objects=False`、杀直接子进程、`descendants_may_survive=True`。

后续工单应包含：完整 `CreateProcess`（或私有 handle 路线）+ Job 事件观察 +
Windows 实机验收，而不是直接复用本轮代码。

---

## 8 W14 双系统进程级沙箱契约与实现（2026-11-18）

> 上文 §1–§7 保留 2026-11-11 F2 首轮交付时的历史事实与 `local` 基线证据。本节记录 W14 双系统进程级沙箱契约及代码接线（总览见 [CONTRACT-PHASE2-G3-2026-11-18.md](CONTRACT-PHASE2-G3-2026-11-18.md) §4，落地于 `exec_seatbelt.py` / `exec_landlock.py` / `exec_backends.py` / `isolation.py` / `dispatcher.py` / `tools.py`）。

### 8.1 默认值、配置入口与零静默回落规则

- **内核默认值不变**：`exec_backends.DEFAULT_EXEC_BACKEND = "local"` 保持不变（未设置或为空时仍走 `LOCAL_EXEC_BACKEND`，不破坏既有默认任务与单测语义）。
- **显式后端选择**：`TIANSHU_EXEC_BACKEND` 支持 `"local"` | `"seatbelt"` | `"landlock"` | `"auto"`：
  - `"seatbelt"`：仅在 macOS（Darwin）可用；非 Darwin 或系统 Seatbelt 能力不可用时一律抛 `PolicyUnsupported(backend="seatbelt", feature="exec_backend")`。
  - `"landlock"`：仅在 Linux `x86_64` / `aarch64` 且内核启用 Landlock（Linux >= 5.13，ABI >= 1）时可用；内核 `< 5.13`（`ENOSYS` / `EOPNOTSUPP`）、Landlock 未启用或非已验证架构一律抛 `PolicyUnsupported(backend="landlock", feature="exec_backend")`。
  - `"auto"`：显式自动原生沙箱模式——Darwin 解析为 `"seatbelt"`，Linux（`x86_64`/`aarch64`）解析为 `"landlock"`，Windows 或其他无已实现原生沙箱的平台直接抛 `PolicyUnsupported(backend="auto", feature="exec_backend")`。
  - **铁律**：任何非 `"local"` 配置在目标沙箱不可用时**严禁静默降级回 `local`**。

### 8.2 macOS `seatbelt` 与 Linux `landlock` 分级能力契约

| 后端注册名 | 平台 / 内核条件 | `capabilities.isolation` | 四边界强制机制 |
|---|---|---|---|
| `seatbelt` | macOS Darwin（SBPL） | `app_container` | ① `files_authority`：默认拒写，仅放行 canonical `policy.cwd_roots` 与每进程私有临时区；显式拒读写工作区外用户敏感路径、`data_root`（含 `events.db*`、`artifacts/`、`blobs/`、`memory_blobs/`、`run/ipc.key` 与 IPC socket）、`~/.tianshu/` 及维护锁目录；② `network`：`(deny network*)` 阻断全部 TCP/UDP/UNIX 网络（含 loopback）；③ `credentials`：后端环境清洗 + SBPL 阻断 `~/Library/Keychains/**`、`~/.ssh/**`、`~/.aws/**` 及 `com.apple.securityd` 等钥匙串 Mach 服务；④ `process_tree`：POSIX 新会话进程组杀树 |
| `landlock` | Linux `x86_64`/`aarch64`，ABI >= 4（Linux >= 6.7） | `app_container` | 子进程 `execve` 前 `prctl(PR_SET_NO_NEW_PRIVS, 1)` + Landlock ruleset：① `files_authority`：仅对系统运行库只读与 canonical `policy.cwd_roots`（加私有临时目录）赋 `LANDLOCK_PATH_BENEATH` 规则，天然隔绝工作区外目录、`data_root`、`~/.tianshu/` 与维护锁目录；② `network`：启用 `LANDLOCK_ACCESS_NET_BIND_TCP \| LANDLOCK_ACCESS_NET_CONNECT_TCP` 零放行规则阻断全部 TCP 连接与监听；③ `credentials`：后端环境清洗 + VFS 层隔绝 `~/.local/share/keyrings`、`~/.ssh`、`/run/user/<uid>` 等凭据套接字；④ `process_tree`：POSIX 进程组杀树 |
| `landlock` | Linux `x86_64`/`aarch64`，ABI 1..3（Linux 5.13..6.6） | `process_tree`（仅文件系统与进程树，无内核 TCP 隔离） | 启用上述 `files_authority`、`credentials` 与 `process_tree`，`supports_network_isolation = False`；`_EXPECTED_ISOLATION["landlock"]` 允许 `{"app_container", "process_tree"}` 以如实反映内核 ABI，请求 `network` 边界的任务由隔离门诚实拒绝（`IsolationRefused`） |
| `landlock` | Linux `< 5.13` 或 Landlock 禁用 / 其他架构 | — | 构造/解析期直接抛 `PolicyUnsupported` |

### 8.3 后端强制凭据清洗、`PROBE_VERSION = "v2"` 与审批前实测视图

1. **后端强制凭据环境清洗 + OS 凭据库封锁**：
   - 无论 `ExecPolicy.env_allowlist` 如何填写，`seatbelt` 与 `landlock` 在 `spawn()` 内部强制剥离宿主敏感变量（`TIANSHU_*` 内部变量与隔离探针哨兵、`*_API_KEY`、`*_SECRET*`、`*_TOKEN*`、`*_PASSWORD*`、`SSH_AUTH_SOCK`、`GPG_AGENT_INFO`、`DBUS_SESSION_BUS_ADDRESS`、`SECURITYSESSIONID` 等），并在各自 OS 沙箱规则中封锁系统钥匙串/密钥环目录与 IPC 服务。
2. **`PROBE_VERSION = "v2"`**：
   - `isolation.PROBE_VERSION` 从 `"v1"` 升至 `"v2"`，使探针缓存自动失效旧版报告，并同步核验权威数据根、宿主环境哨兵及 OS 凭据库封锁效果。
3. **非 `none` 后端的审批前实测 `ApprovalView.isolation`**：
   - 当 `backend.capabilities.isolation != "none"` 且存在 `data_root` 时，即使 `policy.isolation_requires` 为空，`dispatcher._precheck_exec`（审批创建前）与 `tools._run_command`（实际执行）也必须运行 `probe_backend_cached(backend, workspace=probe_workspace, data_root=data_root)`，将绑定本次现场的实测报告写入 `ApprovalView.isolation`（`unverified=False`，如实呈现 `measured`、`boundaries`、`enforced_boundaries`，并在自述与实测不符时立即标出 `contradicted=True`）。
   - 当 `backend.capabilities.isolation == "none"` 且 `policy.isolation_requires` 为空时（默认 `local` 路径），继续保持零探针子进程开销（`report=None`, `unverified=True`, `level="none"`）。
