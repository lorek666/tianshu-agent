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