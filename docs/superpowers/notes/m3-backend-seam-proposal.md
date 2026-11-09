# M3 第二个执行后端：接缝划在哪，第二个后端选什么

状态：提案（未实现，待评审）
范围：PRD §11 M3 的 Task 2（PR F）。Task 1（C1 provider 接缝）不在本文内。
关联：`docs/superpowers/plans/2026-11-09-m3-swappable.md` §3、`docs/PRD-v1.md` §5.2 / §8.1 / §11
基线：当前工作树，`uv run pytest --collect-only` 收集 401 个用例。

---

## 0. 结论先行

1. 接缝划在**存储原语**这一层：新增一个 `FileBackend` 协议（8 个方法），工具逻辑（截断、UTF-8 回退、拒绝覆盖、写前证据顺序、output 形状、异常映射）留在 core 不动。**不**把"工具执行"整体下沉。
2. 第二个实现选**进程内虚拟文件系统（memfs）**：确定性、不需要 root/容器、零依赖，在 CI 里就是一个普通 pytest 用例。它是"可替换性证明"，不是产品功能，更不是隔离。
3. 后端选择只能是**进程级**（env / 显式 configure），不能 per-run。原因不是偏好，而是 M3 的验收命令要求 `git diff main -- src/tianshu/loop.py` 为空——最自然的注入点 `build_stack(backend=...)` 正好在 loop.py 里。
4. 这个方案会暴露 6 个必须先修的现有缺陷（看盘探针分散在 7 处、快照证据键会撞车、凭据不可独立核对、`Workspace` 把边界和存在性焊死、写盘丢 mode、effect_state 有死值）。修它们不是"接缝的成本"，是 M3 本来就该还的债。

---

## 1. 先把判据钉死

PRD §11：M3 的退出条件是"契约测试全绿，主循环 0 改动"。计划 §4 把它变成一条可执行命令：

```
git diff main -- src/tianshu/loop.py     # 必须为空
```

这句话有一个不显眼但很硬的结构性后果：

- `AgentLoop` 的三个工具接触点都在 loop.py：`_exec_read`（loop.py:910）、`_target_for`（:943）、`_advance` 里的 `build_request`（:828）。
- **`build_stack` 也在 loop.py 里**（:1371），而它是唯一装配 `SnapshotStore` 与 `Dispatcher` 的地方。
- 所以"给 `build_stack` 加一个 `backend=` 参数"这条最自然的注入路径，会让判据直接失败。

由此得到三条设计约束，后面所有候选都按它打分：

| 约束 | 含义 |
|---|---|
| C-a | 不得改 loop.py，**包括 `build_stack` 的签名** |
| C-b | 读路径与写路径必须各自能在不改调用方的前提下拿到后端 |
| C-c | 同一套 contract test 必须能在一个 pytest 进程里对两个实现各跑一遍 |

C-b 是关键：读路径根本不经过 Dispatcher，它直接 `execute(Workspace(root), tool_id, args, ctx)`（loop.py:932），而 `ToolContext` 只传了 snapshots / log / run_id / call_id（loop.py:928-930）。所以后端解析必须放在**两个调用点都会经过的地方**——工具层自己（`ToolContext` 的惰性默认值），而不是装配层。

> 这也解释了为什么"进程级单例"不是偷懒：在 C-a 之下它是唯一解。要 per-run，就得把后端塞进 `ToolContext` 的构造参数，而那行代码在 loop.py 里。

---

## 2. 现状：文件操作现在是什么

### 2.1 六个工具（五个对外 + 一个内部）

全部是 `tools.py` 的模块级函数，签名统一 `(ws: Workspace, args: dict, ctx: ToolContext) -> ToolResult`，注册在 `_HANDLERS`（tools.py:417），元数据在 `SPECS`（:426）。

| 工具 | action | side_effect | 读盘 | 写盘 | output 键（精确） |
|---|---|---|---|---|---|
| `list_files` | read | pure | `is_dir`、`iterdir`、逐项 `stat` | 无 | `entries: [{name,is_dir,size}]` |
| `read_text` | read | pure | `is_file`、`stat().st_size`、`open("rb").read(64KiB)` | 无 | `path, content, bytes, total_bytes, truncated` |
| `create_report` | create | non_idempotent / E3 | `exists` | `atomic_write_bytes` | `path, bytes, sha256, after_state, before_state` |
| `modify_text` | overwrite | non_idempotent / E3 | `exists`、`is_file`、`read_bytes` | 同上 | 同上 |
| `delete_file` | delete | non_idempotent / E3 | `exists`、`is_file`、`read_bytes` | `unlink` | `path, deleted, after_state, before_state` |
| `restore_file` | overwrite | 内部（`exposed=False`） | `exists`、`read_bytes` | `atomic_write_bytes` | `path, bytes, sha256, after_state, before_state, restored` |

`restore_file` 不给模型看，只由 `tianshu undo` 的 control run 派发（cli.py:656-700）。`_write_target`（tools.py:208）是写路径的统一入口：有派发票据时**只信任票据里的 target**，参数解析结果必须与之相等，否则 `ToolNotAllowed`。

### 2.2 对外可观察的行为面

**返回值形状**：`ToolResult(status, effect_state, output)`（tools.py:148-156）。`status ∈ {succeeded, failed, rejected, cancelled, unknown}`，`effect_state ∈ {none_applied, applied, partial, unknown}`。实际产生的组合只有五种：

| 场景 | status | effect_state | 谁产生 |
|---|---|---|---|
| 读成功 | succeeded | none_applied | 工具 |
| 读失败（不存在 / 不是文件 / 非 UTF-8） | failed | none_applied | 工具 |
| 写成功 | succeeded | applied | 工具 |
| 拒绝（越界 / 无票据 / 前置不成立 / 租约 / 审批） | rejected | none_applied | Dispatcher |
| 进入执行后异常 | failed | unknown | Dispatcher `_fail_unknown`（dispatcher.py:1218） |

`cancelled` 与 `partial` 是**死枚举**：`ToolResult.status="cancelled"` 没有任何产生者；`effect_state="partial"` 只在 control run 关闭时手工传入（cli.py:794/814），从不来自工具。contract test 不应该假装覆盖这两个值。

**异常面**（接缝最容易漏的一半）。工具不是"只返回 ToolResult"，它们会抛：

| 异常 | 出处 | 被谁吃掉 | 最终结果 |
|---|---|---|---|
| `ToolArgumentError` | 参数校验、坏 base64、哈希不符 | Dispatcher 阶段二元组（dispatcher.py:624）→ `_abort_dispatch`；loop `_exec_read`（:935） | rejected / none_applied |
| `ToolNotAllowed` | 票据目标不一致（tools.py:224）、`_verify` 八项核验 | 同上 | rejected / none_applied |
| `OverwriteRefused` | `create_report` 目标已存在（:295） | 同上 | rejected / none_applied |
| `WorkspaceViolation` | `Workspace.resolve` 越界（workspace.py:163-175） | 同上 | rejected / none_applied |
| `OSError` | 任何 `open/stat/unlink` 的真实错误 | Dispatcher 的 `except Exception`（:639） | failed / **unknown** + 阻断工作区 |
| `ValueError` | `build_receipt` 见 applied 但缺 path/after（receipt.py:87） | 没人接 | 主循环崩（刻意） |

注意 `OSError` 不在"打开文件之前"的异常元组里（dispatcher.py:624-636），所以任何写阶段的 OS 错误都被当成"可能写了一半"。这是正确的保守选择，但它意味着：**后端把错误映射成什么异常，直接决定 effect_state 的诚实程度**。契约必须规定"读失败 → 返回 failed，写失败 → 抛 OSError 子类"，不能让实现各自发挥。

**路径字符串形状**：`output["path"]` 是 `ws.relative(target)`（workspace.py:180），即 `str(Path.relative_to)`。Windows 上是反斜杠。它会流进 receipt.resource、`snapshot.Change.path`、事件 payload 与审计字段。跨平台回放/比对的前提是统一成 `/`，现在没有统一。

**快照写入时机**（顺序本身就是契约）：

```
mark_dispatch(..., "executing")            # tools.py:754，进入 in-flight
handler:
  ctx.prewrite(rel, before, after)         # :140 → SnapshotStore.record_preimage
      blob 先落盘（内容寻址，已存在则跳过）              snapshot.py:98-100
      整个 manifest 原子重写（删同 call_id 旧条目再追加） snapshot.py:103-110
  atomic_write_bytes(target, data)         # fsutil.py:17-32：同目录 temp → fsync → replace → fsync(dir)
failpoint("after_write")                   # tools.py:761：内容已改、结果未提交
回到 Dispatcher → 同事务写 tool.result + receipt → 派发记录 done（dispatcher.py:1274）
```

- `init_run` 在 run 启动时写空清单（loop.py:443）。"清单不存在"因此可以被解释成异常（被清理/损坏），而不是"没写过"。
- 清单在 `data_root/artifacts/snapshots/{run_id}.json`，blob 在 `data_root/artifacts/blobs/<sha256>`（loop.py:1396）。**证据与工作区物理分离**，这点必须保留。
- `prewrite` 是"写前证据"。任何"先写后证据"的实现都直接违约——恢复协议就是靠这条把 `none_applied` 和 `applied` 分开。

**凭据依赖**（receipt.py）：

- 只有 `effect_state == "applied"` 才产出（:78）。
- 字段全部取自 `result.output`：`resource = output["path"]`（:82）、`after = output["sha256"] or output["after_state"]`（:84）、`before = output["before_state"] or "absent"`（:100）。
- `action` 被写成 `f"{tool_id}"`（:96），**不是** C4 的 create / overwrite / delete。
- `backend` 是模块常量 `"local_fs"`（:26），不是每次调用的事实。
- `evidence` 只有一条合成字符串 `f"tool.result:{status}"`（:103），不引用事件 seq、不引用快照 blob、不引用 manifest。
- `verify_receipts`（:112）只做"凭据 ↔ tool.result"的交叉核对，**不核对磁盘**。

**事件面**：`tool.intent`（dispatcher.py:559-578，含完整 `args`）、`tool.result`（:1240 / :1281，5 个键）、`receipt`（:1302）、`dispatch.*`。payload > 256 KiB 落 blob（events.py:25/234），所以把文件内容放进 intent 不撑表，但它确实把"写什么"写进了事件日志——那是刻意的可审计性。

### 2.3 谁在读盘：不止 tools.py

这是接缝设计里最容易被低估的一点。现在的"看盘"逻辑分散在 **7 处**，各写了一遍：

| 位置 | 读什么 | 用途 |
|---|---|---|
| `tools.observe_state`（:55） | `exists` + 全量 `read_bytes` → sha256 | 审批绑定/复核写前状态 |
| `dispatcher.build_request`（:1353-1356） | 同上（`target.exists()` / `observe_state`） | 派发前当场取 preconditions |
| `dispatcher._resolve_target`（:897） | `target.parent.is_dir()` | create 的父目录存在性 |
| `dispatcher.prepare`（:404） | `target.path.exists()` | 选 rule_id |
| `recovery._observe`（:56） | `exists` + 全量读 → sha256 | 恢复判据 |
| `snapshot.undo`（:175-183） | 逐条 `exists` + `read_bytes` | 撤销预检 |
| `FileExecutor._verify`（:708-723） | `ResourceIdentity.of`（`os.stat`）+ `check_preconditions` | 执行侧复核 |

只要后端换了而其中任何一处仍看真盘，就会出现"审批看到真盘、写入进虚拟盘"的半真半假状态——这比不换后端更危险。所以接缝不是"把 tools.py 里的 os 调用抽掉"，而是**把"看盘"收敛成一个探针，再让这 7 处都用它**。

### 2.4 更正一个前提：不依赖 mtime

`observe_state` 用的是**全文件内容 sha256**（tools.py:63），不是 `st_mtime_ns`。两个结论：

- 好消息：没有 mtime 分辨率、时区、跨 FS 的问题，确定性反而好。
- 坏消息：审批路径上每次 `prepare` + 每次 `_verify` 各读一遍整个文件，大文件是 O(n) 双读；且 `read_text` 的截断判断用 `stat().st_size`（:261），与随后的 `read`（:262）是两次打开，存在 TOCTOU。

真正依赖 `stat` 的地方是：`list_files` 的 `st_size`（:250，对目录无意义且跨 FS 不同）、`read_text` 的 size（:261）、`ResourceIdentity.of` 的 `st_dev/st_ino`（workspace.py:63）。

---

## 3. 接缝候选

三个方案，按"下沉深度"排序。行数是量级估计（±30%），不是承诺。

### 候选 A：原语级 `FileBackend`

工具逻辑全部留在 core，只把"字节怎么进出"抽掉。

```python
class FileBackend(Protocol):
    name: str                                                       # 进 receipt.backend
    def exists(self, p: Path) -> bool: ...
    def is_file(self, p: Path) -> bool: ...
    def is_dir(self, p: Path) -> bool: ...
    def size(self, p: Path) -> int: ...                             # 仅普通文件有意义
    def list_dir(self, p: Path) -> list[tuple[str, bool, int]]: ...  # 名字 / 是否目录 / 字节数
    def read_bytes(self, p: Path, limit: int) -> tuple[bytes, int]: ...  # (前 limit 字节, 总大小)
    def write_bytes_atomic(self, p: Path, data: bytes) -> None: ...      # 必须原子替换
    def unlink(self, p: Path) -> None: ...
    def identity(self, p: Path) -> str: ...                          # 稳定键
```

| 项 | 评估 |
|---|---|
| 改动面 | 新增 `sandbox.py`（协议 + 工厂）80-120；`backends/local.py` 100-140；`tools.py` 10 个调用点 + `ToolContext` 字段 + `observe_state` 签名 60-90；`workspace.py` 拆职责 30；`dispatcher.py` 3 处探针 20-30；`recovery.py` 1 处 15；`snapshot.py` undo 预检 15-25；`receipt.py` backend 入参 10；`cli.py` 20-30。合计约 350-500 行（不含第二个后端与测试） |
| receipt | 需要改：`backend` 从常量变成 `build_receipt(..., backend=...)`，其余字段不动 |
| snapshot | 语义不动（`record_preimage` 只收 bytes）；undo 预检换 probe（15-25 行） |
| dispatcher | 装配不动（已接受 `executor` 参数）；只需 3 处直接读盘换 probe，并让默认 executor 由工厂解析后端 |
| loop 0 改动 | **能**：选择在工厂 + `ToolContext` 惰性默认值 |
| 同一 contract test 覆盖两实现 | **能**，断言对象正好是工具可见行为（output 形状、异常、证据顺序、凭据） |

风险：协议别定成"文件系统 API"。比如"原子写"在远程后端是两次 RPC；`identity` 的语义 local（dev:ino）和 memfs（路径）不同，必须限定"只用于租约与覆盖判断，不承诺跨后端可比"。

### 候选 B：执行级 `ToolBackend`

```python
class ToolBackend(Protocol):
    def execute(self, admitted: AdmittedCall, permit: ExecutionPermit) -> ToolResult: ...
```

`FileExecutor` 退化成 `LocalToolBackend`，Dispatcher 的 `executor` 参数类型从具体类变成协议。

| 项 | 评估 |
|---|---|
| 改动面 | 协议 + 类型 60-100；拆分 `FileExecutor` 80；第二实现必须**重新实现 5 个工具的编排与结果形状** 250-400；快照/凭据归属要重新定 |
| receipt / snapshot | **必须选边**：core 继续负责 → 后端只剩落盘，方案退化成 A；后端负责 → 两套实现各自解释"applied 是什么意思"，违反"契约只有一份实现"的精神 |
| dispatcher | 只改类型与默认构造约 10 行；但 `_verify` 的八项核验、`prewrite` 顺序、`failpoint` 位置都降级成"后端必须自己遵守"的口头约定 |
| loop 0 改动 | 能 |
| contract test | 能，但每条都要走完整派发链（审批 + grant + 租约 + intent），跑两遍成本约 2×；且第二后端要复制 ToolResult 语义，测试变成"两个实现互相对齐"，不是"契约约束实现" |

它不算错，只是**时机不对**：B 的收益在"工具要在别的进程/别的信任域里跑"（M5 的 sandbox worker），而现在没有任何一个工具需要。更关键的是 PRD §8.1 的结构是 `Dispatcher（判定）→ Host（强制执行）→ Worker（执行者）`：该下沉的是执行者，不是"判定 + 证据"。

### 候选 C：状态级虚拟 FS（core 只认 VFS）

core 从不碰 `Path`，所有状态在虚拟 FS 里，真实磁盘只是"物化/提交"的一层。

| 项 | 评估 |
|---|---|
| 改动面 | `workspace.py` 重写为纯路径代数、`snapshot`/`recovery`/`undo` 全部换基准、CLI 导出/物化、崩溃恢复新增"未提交"状态：800-1500 行 |
| receipt / snapshot | `applied` 的语义变成"已 staged"，与"写前快照 + 冲突安全撤销"的现有含义冲突；undo 要区分"回滚未提交"和"回滚已提交" |
| dispatcher | preconditions 的读取基准全变，`tool.intent` 里"写前状态"的含义也变 |
| loop 0 改动 | 表面能，实际是"类型没变、语义全变"——这种 0 改动没有意义 |
| contract test | 两个后端的契约不再同构（一个有 commit 阶段），同一套断言写不出来 |

结论：**M3 不做**。v2 真要接远程沙箱，"staged vs committed" 迟早要面对，但应该和 E2/作业模型一起设计，不该塞进 M3。

### 候选 0：只换 `Workspace(root)`

PRD 与计划都明确了：不同实现、同一接缝，甚至不构成候选。列在这里是为了防止有人把它当交付物。

### 对比

| 维度 | A 原语 | B 执行 | C 状态 |
|---|---|---|---|
| 能否绕开 loop.py | 能 | 能 | 名义上能 |
| 契约实现只有一份 | 是 | 否（或退化成 A） | 否 |
| 第二实现工作量 | 180-260 行 | 250-400 行 | 800+ 行 |
| 与 §8.1 结构一致 | 一致（执行者下沉） | 部分（判定也下沉） | 不一致 |
| 暴露现有缺陷的程度 | 高（正好逼出探针问题） | 中 | 高但代价过大 |
| 3 周内可完成 | 是 | 勉强 | 否 |

---

## 4. 推荐：A + 进程内虚拟 FS

### 4.1 为什么是 A

1. **证据与判定必须留在 core。** receipt 与 snapshot 是"我们对用户承诺了什么"的载体，不是后端实现细节。后端只回答"这个路径现在有没有东西、写进去了没有"。
2. **与 M5 方向一致。** 真正的沙箱后端最终是把**原语**执行在 worker 侧（Host 拿句柄，worker 只做受限的 read/write/unlink）。A 的协议形状天然能变成 RPC；B 会把工具编排搬进沙箱，反而让 Host 失去对副作用的控制点。
3. **改动可以被证明是"行为不变"的。** 第一步只做提取（local 后端 + 调用点替换），401 个用例应当原样全绿；如果红了，说明提取改变了行为，能立刻定位。B/C 没有这个中间态。
4. **契约测试的断言对象是工具可见行为**，正是 A 留在 core 的部分；B 把这部分交给后端实现，测试只能测"两个实现像不像"。
5. 不动 loop.py、不加依赖、不引入进程/超时/取消这些 M5 的复杂度。

### 4.2 选择点与注入路径（关键细节）

```
sandbox.py
  _current: FileBackend | None = None
  def configure_backend(backend): ...        # 显式注入（测试 / CLI）
  def current_backend() -> FileBackend: ...  # 惰性：env TIANSHU_SANDBOX_BACKEND，默认 local
```

- `ToolContext.backend: FileBackend | None = None`；工具通过 `ctx.io` 取，`io` 在第一次访问时回落到 `current_backend()`。**loop.py:928-930 那行构造原样不动。**
- `FileExecutor` 构造时显式持有后端，并在 `execute()` 里塞进 `ToolContext`（tools.py 内部 1 行）。
- `Dispatcher.__init__` 的默认 executor 由工厂创建（dispatcher.py:303-314，改 3-5 行）。
- `build_request` / `_resolve_target` / `prepare` 的探针改成 `current_backend()`；`recover_run`、`snapshot.undo` 同理。
- **一致性断言**：`FileExecutor.__init__` 里检查 `backend is current_backend()`（或后端名相同），不一致直接抛。否则"executor 用 memfs、precondition 读 local"会静默发生，比崩溃更难查。测试通过 `configure_backend()` 切换，而不是给各组件各塞一个。

### 4.3 顺带要拆的一件事：`Workspace` 的两种职责

现在 `Workspace.__init__` 在构造时要求 `root.is_dir()`（workspace.py:158-161），`resolve` 用 `Path.resolve()` 做 realpath（:169）。这其实是两种东西：

- **词法边界**（候选路径规范化后是否在 root 内）——纯路径运算，所有后端共用，必须是唯一实现（PRD §8.1「策略判定点唯一」）。
- **存在性与符号链接**——后端的事实。local 有 symlink，memfs 没有，远程后端有它自己的。

拆成：`Workspace` 只做词法包含（构造不再要求目录存在）；local 后端构造时 `is_dir()` 快速失败 + 打开前 realpath 复核（防符号链接逃逸，单点实现）；memfs 不做 realpath（无 symlink，明确不承诺）。

这是**唯一会碰到安全边界的一处改动**，必须有独立测试证明：local 后端下 `../`、绝对路径、指向根外的 symlink 三种越界仍然全部被拒（对应现有 `tests/test_dispatch.py` 的越界用例）。

### 4.4 后端标识进凭据

`build_receipt` 增加 `backend: str` 参数，`BACKEND` 常量删除（或保留为 local 的默认值）。历史 receipt 不动；新 receipt 的 `backend` 来自 `backend.name`。这样"这条凭据由哪个后端产出"第一次成为事实，而不是一个写着 `local_fs` 的谎言。

---

## 5. 这个方案会暴露的现有缺陷

分"必须先修"与"可以先记账"两类。每条都能指到行。

### 必须先修（不修则换后端必然出错）

1. **看盘逻辑分散在 7 处**（§2.3）。不是洁癖：只要一处还看真盘，审批绑定的 preconditions 和实际写入就基于两个不同的世界。修法：`probe` 协议（`exists/is_file/is_dir/size/read_bytes`），7 处全部改用。
2. **`Workspace` 把边界与存在性焊死**（workspace.py:158-161）。虚拟根不是目录 → `Workspace(root)` 直接抛 → 读路径（loop.py:932）先挂。修法见 §4.3，并补越界回归。
3. **`ResourceIdentity.of` 依赖 `os.stat`**（workspace.py:59-72）。memfs 上必然走 `path:` 退化分支。它本身不是错，但租约的 `assert_covers`（leases.py:541-562）同时混用 inode 键与路径前缀，必须规定：跨后端只承诺路径前缀覆盖，inode 仅用于 local 的"同一目录"判定。否则将来出现"inode 成立、路径不成立"的混合状态，没人解释得清。
4. **快照证据键只有 workspace 相对路径**（`Change.path`，snapshot.py:34；写入时传 `rel`，tools.py:302/333/367）。一个 Run 可以绑多个 workspace（`Task.workspaces` 是数组），两个 workspace 下的 `a.md` 会撞成同一条证据；undo 又只用一个 `ws` 去 resolve（cli.py:748）→ 撤销可能写到错误目录。修法：证据键加上 workspace 根（或 `ResourceIdentity` 的稳定键），manifest 里记录 `root`。**这条不修，换后端只是让它更容易复现。**
5. **写盘丢 mode / owner / xattr**（fsutil.py:19-28）。`mkstemp` 建的是 0600，`os.replace` 保留 temp 的 mode，所以覆盖一个 0644 文件之后它是 0600；硬链接也会被断开（replace 换 inode）。这是 local 后端的行为，必须写进契约测试**固定下来**（要么修成"保留原 mode"，要么明确声明"不保留"），不能让它处于"没人知道"的状态。
6. **`OSError` → `unknown` 的映射是隐式的**（dispatcher.py:624-643）。契约必须显式规定：读失败返回 `failed`，写失败抛 `OSError` 子类；"写失败但可能已部分生效"由 dispatcher 记 unknown 并阻断。memfs 也要守同一套映射，否则两个实现的 effect_state 不可比。

### 可以先记账（不阻塞，但要进 M3 验收的已知问题）

7. **凭据不能"独立核对"**。receipt.py 的模块注释承诺"不看对话、不看模型输出，只看它自己"，但实际：`action` 是 tool_id（:96）、`resource` 只有相对路径（:82）、`evidence` 是合成字符串（:103）、不引用 snapshot blob / manifest / 事件 seq、`verify_receipts` 不核对磁盘（:112-149）。它是一张"自洽的记录"，不是"可独立验证的凭据"。M3 至少把 `backend` 变成事实；完整的证据链（凭据 → 事件 seq → 快照 blob → 磁盘）建议单独排 PR。
8. **`create_report` 的存在性检查与写入之间有竞态**（:295 与 :303）。dispatcher 的 precondition（:550）也查一次，但两次都不原子。单进程单 Run 下靠租约挡住了并发，所以现在没暴露；跨后端时如果 memfs 的 `exists` 与 `write` 不共享同一把锁，这条会立刻变成 bug。
9. **`delete_file` 读与 unlink 之间同样有竞态**（:364 与 :368）：可能删掉一个刚被替换的新文件，却记下旧内容的哈希。
10. **`read_text` 的 stat/read 两次打开**（:261-262），`truncated` 可能基于过期 size；且"前缀是合法 UTF-8 就接受"（:267-276）意味着一个二进制文件若前 64 KiB 恰好合法，会返回一段"看起来正常"的文本。这是可接受的产品取舍，但契约测试要把它写成断言，而不是留给下一个人猜。
11. **`list_files`**：目录 size 无意义且跨 FS 不同（:250）；排序键是原始 `name` 而输出是 NFC（:241/:248）；`is_dir()` 跟随 symlink（:249）。memfs 必须给确定值（建议目录 size 恒 0），否则同一份断言在两个后端上不可能同时成立。
12. **`effect_state` 有死值**（`partial` 从不来自工具）、`status=cancelled` 无产生者（tools.py:150-151）。contract test 应为"实际产生的组合"写断言，并把死值登记为已知不一致。
13. **路径分隔符不统一**（`ws.relative` → `str(Path.relative_to)`）。进 output、receipt、snapshot 的路径在 Windows 上是 `\`。换后端是把这件事说清楚的最好时机：契约规定**证据里的路径一律 `/` 分隔**。

---

## 6. 第二个后端：选什么

### 6.1 推荐：进程内虚拟文件系统（memfs）

实现要点：

- 状态是 `dict[logical_root, dict[relative_path, bytes]]` + 目录集合；进程退出即消失。
- **逻辑根必须落在宿主不可能存在的命名空间**，例如 `/__tianshu_memfs__/{run_id}`；后端构造时若发现该路径在宿主上存在，直接 fail closed（宁可不跑，也不要和真盘混在一起）。不要选 `/tmp/...`：macOS 上会 realpath 成 `/private/tmp`，`WorkspaceBinding.__post_init__` 的 resolve（workspace.py:108）会改字符串。
- `write_bytes_atomic` 用"先建新条目、再删旧条目"模拟替换；`read_bytes(p, limit)` 返回 `(data[:limit], len(data))`，与 local 的 `(前 limit 字节, 总大小)` 语义一致。
- `list_dir` 返回 `(name, is_dir, size)`，目录 size 恒 0；排序交给工具层（工具已经排了）。
- `identity(p)` 返回 `"path:" + 规范化路径`。
- **不模拟**：权限位、owner、符号链接、磁盘满、rename 的 crash 语义、mtime。

为什么是它：

- 确定性：不依赖时钟、inode、平台目录语义、剩余空间。
- 无 root、无容器、无网络、无子进程 → CI 里就是普通 pytest 用例，还能直接在单测里造"写一半就抛错"的后端，用来验证"证据先于内容"。
- 零运行时依赖，符合项目约束。
- 它能**逼出**真实缺陷（§5 的 1/2/3/4/12 都是被它逼出来的），这正是一个好的第二实现该干的事。

### 6.2 被否掉的备选

| 备选 | 否决理由 |
|---|---|
| scratch 目录副本（拷贝工作区到临时目录再跑） | 仍写宿主真盘，只是换了 root；与 local 语义几乎相同，证明不了可替换，反而多一份清理/权限问题 |
| 只读包装后端（所有写抛拒绝） | 对"只读副本工作区"有产品价值，但按定义过不了"写成功"那半套契约，不构成替换性证明；可作为 A 之上的装饰器**另排** PR |
| 出进程 worker（RPC + 超时 + 取消） | 这是真正的第二个执行模型，但工作量约 2×，且超时/取消/僵尸进程属于 M5 的 Host 议题；塞进 M3 会挤掉 Task 1 |
| 依赖 `pyfakefs` / `pyfilesystem` | 违反零依赖，且它们有自己的语义，等于引入第三套契约 |

### 6.3 诚实边界（要写进 README 与 M3 验收）

- memfs **不是沙箱**，它比 local 更弱（没有权限概念）。它证明的是"工具副作用经过一个可替换的边界"，不是"副作用被限制住了"。
- M3 结束后，PRD §8.1 的威胁表**一个字都不变**：模型越权 / 提示词注入 / 失控 worker 仍然靠策略层 + 路径边界 + 审批，不靠后端。
- "跨平台等价隔离"依然不在 v1（TECH-ARCH §6 已说明）。
- memfs 无持久化 → **kill -9 那套证据（M1）仍然只由 local 覆盖**。这是覆盖矩阵里必须单列的一行，不能因为"两个后端都绿了"就假装崩溃恢复也被两个后端验证过。

---

## 7. Contract test 清单

文件：`tests/test_backend_contract.py`，仿 `tests/test_model_contract.py` 的写法：

```python
BACKENDS = ["local_fs", "memfs"]

@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path): ...   # local 用 tmp_path；memfs 用逻辑根

def test_create_over_existing_is_refused(backend, env): ...
```

`env` 复用 `tests/test_dispatch.py` 里的 `Env`（真 EventLog + 真 TaskStore / LeaseStore / GrantStore / ApprovalStore / SnapshotStore），只把后端换掉。断言必须落在"工具可见行为"上，不测实现细节。

**C0 负向元测试（先写这条）**
- 用一个故意违约的 `MisbehavingBackend`（跳过快照 / 写后不更新状态 / 把读错误抛成 `ToolNotAllowed`）跑同一套用例，**套件必须红**。否则"全绿"证明不了什么。

**C1 边界与解析**
1. `read_text("../../etc/passwd")` → `WorkspaceViolation`，且后端读调用计数为 0。
2. 绝对路径逃逸 → `WorkspaceViolation`。
3. local-only：根内 symlink 指向根外 → `WorkspaceViolation`。
4. 根不存在时：local 后端构造即失败（fail fast）；memfs 允许。
5. 同一路径两次 `identity()` 得到同一个键。

**C2 读语义**
6. `list_files` 两次调用 output 的 canonical JSON 完全相同；顺序按规范化后的名字。
7. `list_files` 的目录条目 `size == 0`；普通文件 `size == len(bytes)`。
8. `read_text` 65536 字节 ASCII → `bytes == 65536`、`total_bytes == 65536`、`truncated is False`。
9. `read_text` 65537 字节 → `truncated is True`，`content` 长度 65536。
10. 截断点落在多字节字符中间（`"a"*65535 + "中"`）→ `content.encode("utf-8")` 成功、`bytes <= 65536`、不抛。
11. 非 UTF-8 → `status="failed"`、`effect_state="none_applied"`、`output["error"]` 含 "UTF-8"，不抛异常。
12. 不存在 / 是目录 → `failed`、`none_applied`。
13. 所有只读调用 `effect_state == "none_applied"`。

**C3 写语义与顺序**
14. `create_report` 目标已存在 → 抛 `OverwriteRefused`（不是返回 rejected）。
15. `create_report` 成功 → output 键集合**精确等于** `{path, bytes, sha256, after_state, before_state}`；`before_state == "absent"`；`after_state == sha256(content)`；`path` 相对根且 `/` 分隔。
16. **证据先于内容**：注入"write 必抛"的后端 → 抛异常后 manifest 里已有该 `call_id` 条目，而内容未变；恢复结论为 `none_applied`。
17. **反向断言**：用"先写后证据"的违约后端跑同一用例 → 测试必须失败（与 C0 配套）。
18. `modify_text` 目标不存在 / 是目录 → `failed`，且无写入、无快照。
19. `modify_text` 成功 → `before_state == sha(旧)`、`after_state == sha(新)`。
20. `delete_file` 成功 → `deleted is True`、`after_state == "absent"`、`before_state == sha(旧)`；随后 `read_text` 返回 failed。
21. `delete_file` 目标是目录 → `failed`（不递归）。
22. `restore_file`：`sha256` 不符 → `ToolArgumentError`；`content_b64` 非法 → `ToolArgumentError`。
23. 参数多余键 / 缺必填 / 类型错 → `ToolArgumentError`，且无快照、无写入。
24. `AdmittedCall.target` 与 args 解析结果不一致 → `ToolNotAllowed`（票据优先）。

**C4 快照证据**
25. 每次 `applied` 恰好产生一条 `Change`，键为 `call_id`；同一 `call_id` 再写一次是**替换**而不是追加（`load()` 长度不变）。
26. 相同内容写两个路径 → `blobs/` 下只有一个 blob 文件。
27. `failed` / `rejected` 调用不产生 change；`has_manifest` 仍为 True（`init_run` 已执行）。
28. `before is None` → `before_state == ABSENT` 且 `blob_ref is None`。
29. 快照只写 `data_root`；workspace 根下不出现 `artifacts/`、`blobs/`、`snapshots/`（memfs 下更严格：宿主上根本没有这些路径）。

**C5 凭据**
30. `applied` → 恰好一条 receipt 事件；`failed` / `unknown` → 零条。
31. `receipt.after_state` 等于 `output["sha256"] or output["after_state"]`；`before_state` 与 output 一致。
32. `receipt.backend == backend.name`，两个实现的值不同。
33. `verify_receipts(log, run_id) == []`。
34. `effect_state="applied"` 但 output 缺 path → `ValueError`（宁可炸，不出半张凭据）。

**C6 派发与恢复**
35. 两后端下 `tool.intent` / `tool.result` 的 payload 键集合相同（只允许差一个 `backend` 字段）。
36. `tool.result.effect_state ∈ {none_applied, applied, unknown}`（断言"不会出现 partial"，而不是"必须出现"）。
37. 同一 run 内 create → modify → delete 后 `recover_run` 无 unresolved（memfs 下在同一进程内验证）。
38. `undo`：还原后内容等于 preimage；事后手工改文件 → `conflicts` 非空且**一个字节都没动**。
39. 目标不在持有租约的子树内 → `LeaseLost`（两个后端同一结论，验证 §5 第 3 条的探针一致性）。

**C7 只对 local 跑（单独文件 / 单独 marker）**
40. 四个 failpoint 的 kill -9 语义（`after_intent` / `after_write` / `after_result` / `after_receipt`）。
41. 权限拒绝（`chmod 000`）→ `OSError` → `unknown` + 阻断。
42. 覆盖已有文件后的 mode 行为（先固定现状，再决定是否修）。
43. 符号链接相关的逃逸与竞态。

C7 混进参数化套件会立刻产生"memfs 不满足"的假红；删掉它又会丢掉 M1 的崩溃证据。所以**套件必须显式分两层**，并在验收文档里写清哪一层覆盖哪个后端。

---

## 8. 风险与不做

**风险**

1. **进程级单例的串扰**：两个后端同时存在于一个进程（比如测试忘了 reset）会让"审批读 local、写入写 memfs"静默发生。缓解：`configure_backend` 只能显式调用；`FileExecutor` 构造时断言一致；测试用 autouse fixture 复位。
2. **契约测试假绿**：两个实现由同一人写、断言只覆盖已知行为。缓解：C0 违约后端；断言写成"输出键集合精确相等"而不是"包含某些键"；把本清单进 `docs/`。
3. **触碰安全边界**：`Workspace` 放宽构造条件是本提案唯一会动安全语义的地方。缓解：三类越界用例（`..`、绝对路径、symlink）单独一组，必须在 local 下全绿；memfs 只做前两类。
4. **覆盖矩阵出现结构性空白**：崩溃恢复、权限、磁盘满只在 local 上有证据。必须在 `docs/M3-verification.md` 里按后端分列，不能笼统写"契约测试全绿"。
5. **行数估算不准**：所有行数是量级估计；第一步（纯提取）落地后应重新估后续步骤。
6. **memfs 被误用**：一旦它进了产品默认路径（比如某分支在没有真 workspace 时"顺手"用 memfs），用户会以为文件写成功了。缓解：只允许显式 `--backend memfs` / env 选择，并让 `tool.result` 与 receipt 留下 `backend=memfs` 的痕迹；CLI 对 memfs 打一行醒目提示。

**不做**

- 不做 OS 级隔离、不做权限模型、不做 chroot/容器/Seatbelt。M3 的"沙箱后端"是可替换的**执行后端**，措辞上不许写成"隔离"。
- 不做 per-run 后端选择（要改 loop.py）。
- 不做远程 / RPC / 句柄能力模型（M5 的 Host/Worker）。
- 不做 shell / Python 执行工具：那是另一个接缝，且 PRD §8.1 要求"未使用有效 OS 沙箱的任意 shell 执行"必须标为受信任执行。
- 不做流式、不做 E2。
- 不改 `docs/PRD-v1.md`；如果评审认可本文，PRD 的补充单独提。
- 不给 memfs 做持久化/崩溃恢复：那会把它变成"另一个 local"，失去确定性测试替身的意义。

---

## 9. 落地顺序（PR F 切成 5 步）

| 步 | 内容 | 验收 |
|---|---|---|
| F1 | `sandbox.py` 协议 + `LocalFileBackend` + `tools.py` 调用点替换（行为不变） | 401 个用例全绿；本次 diff 不含 loop.py |
| F2 | 探针收敛：`workspace.py` 拆职责、7 处看盘改 probe、`receipt.backend` 入参、证据路径统一 `/` | 越界三连 + receipt/snapshot 测试全绿 |
| F3 | `MemfsBackend` + 逻辑根命名空间 + fail closed | memfs 单测；未显式选择时不会被选中 |
| F4 | `tests/test_backend_contract.py` 参数化 + `MisbehavingBackend` + C7 分层 | 同一套断言两实现全绿；违约后端必红 |
| F5 | CLI `--backend` / `TIANSHU_SANDBOX_BACKEND`、README / TECH-ARCH 的诚实边界、`docs/M3-verification.md` | 三条验收命令见下 |

验收命令：

```
uv run pytest -q
uv run ruff check .
git diff --stat main -- src/tianshu/loop.py     # 必须无输出
```

---

## 10. 需要评审拍板的三件事

1. **memfs 够不够"第二个后端"？** 我的判断是够——M3 的判据是"可替换"，不是"更安全"。如果评审认为它太像测试替身，次优选择是"出进程 worker"（方案 B，工作量约 2×，且要处理超时/取消/僵尸进程）。
2. **接受进程级后端选择吗？** 代价是一个进程内不能同时用两个后端；收益是 `loop.py` 0 改动能机械成立。若更看重 per-run 灵活性，就得放宽判据、允许 `build_stack(backend=...)`，并在验收里写明"loop.py 改了一个参数"。
3. **`receipt.resource` 要不要带 workspace 根？** 带了才能跨 workspace 唯一，代价是与历史 receipt 的字段语义不一致（旧数据保留相对路径，`verify_receipts` 不重算）。这与 §5 第 4 条的证据键问题绑在一起，建议同一个 PR 解决。

---

附录：文中行号基于当前工作树（2026-11-09）。实现时若行号漂移，以函数名为准。

---

## 附录 B：落地记录（PR F，2026-11-09）

评审拍板：memfs 够用；接受进程级后端选择；`receipt.resource` 带 workspace 根
推迟到 PR G（本 PR 只让 `backend` 成为事实）。

**已落地（对应提案 §9 的 F1–F4）**

| 内容 | 位置 |
|---|---|
| `FileBackend` 协议 + `LocalBackend` + `MemBackend` + 进程级选择器 | `src/tianshu/sandbox.py` |
| 路径键（`normalize_name` / `overlap_key` / 祖先判断）从 workspace 拆出，供后端复用 | `src/tianshu/pathkeys.py` |
| 看盘收敛：tools / dispatcher / snapshot / recovery 统一走 probe | 同名模块 |
| `Workspace` 拆成"词法边界"+"后端负责根可用性" | `src/tianshu/workspace.py` |
| `ResourceIdentity.of(backend=...)`；跨后端只承诺路径前缀 | 同上 |
| 覆盖已有文件保留原 mode（mkstemp 0600 的 bug） | `src/tianshu/fsutil.py` |
| 证据路径统一 `/`（`as_posix`） | `workspace.relative` |
| `receipt.backend` 记录实际后端 | `receipt.build_receipt(backend=...)` |
| 两个后端跑同一套工具契约 + C0 违约后端元测试 | `tests/test_backend_contract.py` |
| 后端原语（mode / 原子替换 / 虚拟命名空间 / 不碰宿主盘 / 选择器） | `tests/test_sandbox_backend.py` |

**与提案的差异（都要如实写在 PR 里）**

1. **CLI 的 `--backend` 没做。** `src/tianshu/cli.py` 在 PR #15（Task 1）的
   改动范围内，本 PR 不碰它；选择走 `TIANSHU_SANDBOX_BACKEND` 或显式
   `configure_backend()`。CLI 开关留给 PR G。
2. **`MemBackend` 的命名空间可注入**（默认 `/__tianshu_memfs__`）。测试需要
   一个不与其它用例共享的命名空间，否则 memfs 的进程内状态会串。默认值
   仍然对宿主同名路径 fail closed。
3. **`list_files` 的排序键改成了 NFC 规范化后的名字**（提案 §5 第 11 条）。
   这是对两个后端都生效的行为变化：macOS 上 NFD 文件名不再出现"显示顺序
   与排序顺序不一致"。
4. **`read_text` 改为后端一次打开返回 `(前缀, 总大小)`**：不再有 stat 与
   read 之间的窗口（提案 §5 第 10 条只要求记账，这里顺手消掉了竞态面）。

**本 PR 明确没修、已记账的（不要以为接缝把它们解决了）**

1. **快照证据键仍只有 workspace 相对路径**（提案 §5 第 4 条）→ PR G。
2. **凭据仍不可"独立核对"**（`action` 是 tool_id、`evidence` 是合成串、
   不引用事件 seq / blob）→ PR G。
3. **存在性与删除的竞态**（`create_report` 的 exists 与写入、`delete_file`
   的读与 unlink）→ 原样保留。
4. **目录目标会让 `build_request` 抛 `IsADirectoryError`/`FileNotFoundError`**
   （新发现）：`delete_file`/`modify_text` 指向目录时，`observe_state` 在
   生成 preconditions 阶段直接读目录失败，异常逃出 `prepare`，既不是
   `rejected` 也不是 `failed`。M2 起就是这样，接缝只是让它更明显。
   修复方式应该是让 `observe_state` 对目录返回一个可判定的状态（或让
   `build_request` 先做 `is_file` 判定并抛 `DispatchDenied`），单独开票。
5. **`effect_state=partial` / `status=cancelled` 仍是死枚举**：契约测试断言
   "不会出现 partial"，而不是假装它存在。

**覆盖矩阵（验收文档要照抄，别笼统写"契约测试全绿"）**

| 断言组 | local_fs | memfs |
|---|---|---|
| 工具可见行为（读/写/删/恢复/参数/证据顺序/凭据/租约） | ✅ | ✅ |
| 快照落在 data_root、工作区内无 artifacts | ✅ | ✅ |
| 越界：`../`、绝对路径 | ✅ | ✅ |
| 越界：符号链接 | ✅ | 不承诺（无 symlink） |
| 覆盖写入保留 mode、新建 0600 | ✅ | 不适用（无 mode） |
| kill -9 / failpoint / 权限拒绝 / ENOSPC | ✅（M1 证据） | ❌ 状态随进程消失 |
