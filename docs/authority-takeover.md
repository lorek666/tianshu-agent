# 权威根与完整接管（#61）证据

- 分支：`feat/authority-takeover`；基线 `main @ 540c543`（1080 passed / 3 skipped）。
- 规格：[`SPEC-RESIDUALS-M0M1.md` §T2](../ts-wt-lead/docs/SPEC-RESIDUALS-M0M1.md)；
  对应 `docs/DEBUG-M0M1.md` 修复状态总表 M4 行的「仍未覆盖」与 `M2-verification.md` §8 的两条「未实现」。
- 收工全量：**1097 passed, 3 skipped in 103.67s**；`ruff check .` → `All checks passed!`。

## 0. 两条问题与结论

| 问题 | 结论 | 钉住它的测试 |
|---|---|---|
| 换 `--db` / 换 cwd 就能让同一工作区的阻断「看不见」（authority 数据根逃逸） | **已修**：阻断状态固定登记到用户级权威根，与 `--db`/`--data-root`/cwd 无关 | `tests/test_authority_block.py`（7 条，含真子进程双 `--db` 探针） |
| 租约可以被过期/死主接管，但接管不看前任有没有未决副作用 | **已修**：所有会抢 stale 租约的路径（主循环 `start`/`resume`、控制 Run）都先只读对账；不干净就拒绝 + 阻断 + 留审计 | `tests/test_takeover.py`（6 条，含真 `SIGKILL` 前任 + 真子进程接管） |

## 1. 权威根（`src/tianshu/authority.py`，新增）

- 路径：`TIANSHU_AUTHORITY_ROOT`，否则 `Path.home()/".tianshu"/"authority"`；
  **与 `--db` / `--data-root` / cwd 无关**。默认值就是"这台机器上这台用户"，
  换项目目录、换事件库都改不了它。
- 存储：SQLite（`authority.db`，`synchronous=FULL`）两张表，**两种阻断对象**：
  - `blocked_workspaces(workspace_key PK, workspace_path, run_id, reason, blocked_at, released_at, release_reason)`
    —— 目录（文件写用）；
  - `blocked_objects(object_key PK, run_id, reason, blocked_at, released_at, release_reason)`
    —— 外部对象（MCP 后续工单；键 = `mcp://server/tool` 或 server 级 `mcp://server`）。
    旧版本库**没有**这张表 = 从来没有对象阻断 = **空集**（能证明，不是损坏）；
    表在但读不出来 → 按已阻断处理。
  两张表不合并：目录键要过 `overlap_key`（大小写/Unicode 折叠、resolved 路径），
  URI 键是逐字节相等的另一种"同一个"。解除都不删行，谁因为什么解除是审计材料。
- 键：`workspace_key = pathkeys.overlap_key(resolve(path))`（Unicode NFC + 大小写折叠），
  与 `ResourceIdentity`/租约同一口径；换一种路径写法绕不过去。原始绝对路径另存一列排障。

失败语义（全部朝安全方向）：

| 情形 | 行为 | 理由 |
|---|---|---|
| 库/目录不存在 | **空集** | 新用户/新环境本来就没有阻断记录；当损坏会"第一次用就被自己挡住" |
| 库在但**对象表**不存在（旧版本库） | **空集**（只对对象查询） | 旧版本写不出对象阻断，缺表能被证明就是空集；当成损坏会让升级用户被全量挡住 |
| 库存在但读不出来（坏库/锁死/权限） | `AuthorityUnavailable` → 守卫按**已阻断** | 读不出来 ≠ 没阻断 |
| 写失败（锁忙 / 损坏） | `AuthorityWriteFailed` → 恢复报告/CLI 如实写「跨数据根阻断未生效」 | 不许假装跨数据根阻断成功 |
| 读路径 | **不** `mkdir`、不建库（SQLite 自己的 `-wal/-shm` 除外） | 只读对账不能改别人的数据根 |

## 2. 守卫双读（`src/tianshu/guard.py`）

- `blocked_workspaces(log)` = 本日志回放 ∪ 权威根有效记录（信息查询口径，读不出来就抛，
  不替调用方下"没阻断"的结论）。
- `_guard_roots(log)`（写入侧）= 同上；日志有畸形阻断事件或权威根读不出来 → **按已阻断**
  抛 `WorkspaceBlocked`。这是安全方向，不是"没阻断"。
- `ensure_not_blocked` 用 `pathkeys.overlap_key` 比目录身份：同一目录换个大小写写法不能绕过。
- `unblock()` 顺序：**先写日志审计事件 → 再释放权威根**。释放失败 → 日志已不挡、权威根仍挡
  （最多"多挡一会儿"），并抛 `AuthorityWriteFailed`，绝不假装解除成功。
- `blocked_objects(log)` / `ensure_object_not_blocked(log, keys)`：对象阻断的读侧与发外守卫。
  调用方一次给 `(工具级, server 级)` 两个键（server 级阻断必须能遮住它下面全部工具）；
  日志有畸形对象阻断记录或权威根读不出来 → 按**已阻断**抛 `McpObjectBlocked`。
- `unblock_object(log, key, reason=...)`：只解除对象阻断，顺序与 `unblock()` 相同
  （先写日志审计事件、再释放权威根）。**`unblock()` 不碰对象阻断**——核对过工作区文件
  不等于核对过 server 侧副作用。
- `mirror_blocks_to_authority(log)`：迁移入口。每次 `recover_run` 前把本数据根日志里
  **当前有效**的目录阻断与对象阻断都幂等补登记进权威根（旧版本的库、换过数据根的情况）；
  已解除的不会复活。补登记失败只写进恢复报告的问题列表，不炸恢复流程。

**双 `--db` 探针**（`test_cross_data_root_block_is_visible_to_another_db`，真子进程）：

1. 数据根 A 上跑 `recover_run` 判出 `unknown` → 日志写 `recovery.blocked` + 权威根登记；
2. 换数据根 B（另一个 `--db`）起 stack，`ensure_not_blocked(B.log, ws)` → **必须**抛
   `WorkspaceBlocked`；
3. `guard.unblock` 之后 B 侧可写；
4. 权威根文件损坏 → B 侧写入按已阻断（`test_corrupt_authority_root_fails_closed`）；
5. 权威根有记录但本数据根日志没有 → 仍阻断
   （`test_authority_record_without_log_event_still_blocks`）。

## 3. 完整 takeover

### 3.1 只读对账 `recovery.assess_run(data_root, run_id, *, log=None)`

`RecoveryReport` 的只读前身：**不写事件、不落盘、不阻断、不补凭据**，只回答
"这条被打断的 run 现在能不能被安全接手"。返回 `RunAssessment`：

| 字段 | 含义 |
|---|---|
| `applied` / `none_applied` | 能确定的效果（磁盘 = 写后 / 写前，证据自洽） |
| `unknown` | 磁盘既非写前也非写后（写了一半 / 被第三方改过） |
| `unresolved` | 无法完成对账（未知工具、证据根不可用、旧格式无根、清单损坏/缺失） |
| `corrupt` / `missing_manifest` | 清单坏了 / 有写入 intent 却没有清单 |
| `pending_writes` | 本次覆盖的待决写调用数 |

`blocks_takeover = 有 unresolved/unknown，或清单坏，或「写了却没清单」`。
清单损坏/缺失**返回状态而不是抛异常**（接管方要据此拒绝，不是崩溃）；事件库自身读不出来
返回 `corrupt=True`——校验器自己崩了不算"干净"。

只读是硬约束：`_read_only_snapshot_store` 用 `SnapshotStore.__new__` 挂属性，绕过会
`mkdir` 的构造函数（`test_assess_run_is_read_only` 断言：对账后事件条数、租约、文件、
目录清单全不变）。

### 3.2 接管点：三个入口，同一条对账

`LeaseStore.acquire*/acquire_many` 增加可选 `reconcile` 回调；`Dispatcher` 与 `AgentLoop`
一律传自己的：

1. `AgentLoop.start()` —— 新 Run 抢前任 stale 租约（**最常见的接管入口**）；
2. `AgentLoop.resume_run()` —— 崩溃 Run 被新实例续跑前，缺的租约重新获取；
3. `Dispatcher.open_control_run()` —— 撤销/恢复的控制 Run。

回调在 `acquire` 的同一事务里、**任何写之前**调用（`_acquire_locked` 先把"要抢的 stale
记录"收集起来，对账通过后才删行）。`reconcile` 抛 `LeaseConflict` → 整批拒绝、一行都不写；
事务回滚**之后**由调用方登记持久证据：

- `recovery.blocked`（本数据根日志，带 `takeover_refused_by` + 前任 `call_ids`）；
- 权威根 `blocked_workspaces`（换数据根也看得到）。

干净接管则用现有 `run.control` 记 `action="lease_takeover"` 审计（`previous_run` +
`checked`），不新增事件类型。`tasks.py` 的状态投影只认真正的控制请求
（`pause`/`cancel`），不会把审计动作写成非法的 `control_requested`。

### 3.3 真 `SIGKILL` 前任 + 真子进程接管

`tests/test_takeover.py` 的"前任"是真子进程：建 run + 租约 + 写前证据 → 写半截 →
`SIGKILL` 自己（不是 `return`、不是 `SystemExit`，没有任何清理机会）。接管也是另一个
真子进程——同进程里调两次 `LeaseStore` 证不了"两个实例"这件事。

| 用例 | 断言 |
|---|---|
| `test_takeover_refused_when_unresolved_side_effects` | 拒绝（rc=2）+ 原因含"未核实"；权威根阻断（换数据根仍被挡）；前任租约没被抢；日志有 `recovery.blocked`；目标文件零改动 |
| `test_clean_takeover_is_allowed_and_audited` | 干净前任（写前证据在、磁盘仍是写前）→ 允许接管；`run.control` 审计里有 `previous_run`/`checked`；**不**登记阻断 |
| `test_main_loop_refuses_to_steal_unresolved_lease` | **主循环入口**同样拒绝：`AgentLoop.start` 抢 stale 租约前对账，拒绝后权威根阻断、租约不变、文件零改动 |
| `test_assess_run_is_read_only` / `..._missing_manifest...` / `..._corrupt_manifest...` | 对账不写；清单缺失/损坏返回状态而不是抛异常 |

## 4. 负控（回退修复后必须红，原样输出）

### 4.1 去掉守卫里的权威根那一读

```
$ pytest -q tests/test_authority_block.py
FAILED tests/test_authority_block.py::test_cross_data_root_block_is_visible_to_another_db
FAILED tests/test_authority_block.py::test_corrupt_authority_root_fails_closed
FAILED tests/test_authority_block.py::test_authority_record_without_log_event_still_blocks
3 failed, 4 passed in 0.25s
```

### 4.2 主循环不再对账（`_acquire_leases` 退回裸 `acquire`）

```
$ pytest -q tests/test_takeover.py tests/test_authority_block.py
E       assert 0 == 2
E        +  where 0 = CompletedProcess(args=[...], returncode=0, stdout='ALLOWED\n', stderr='')
FAILED tests/test_takeover.py::test_main_loop_refuses_to_steal_unresolved_lease
1 failed, 12 passed in 0.74s
```

`ALLOWED` 就是漏洞本身：没有这一步，新 Run 会在"有没有写、写到哪一步都没核实"的目录上
直接开写。控制 Run 的接管用例仍然绿——这正是"只保护控制 Run"的旧状态。

## 5. 实现陷阱（都踩过并写进代码注释）

1. **权威根不存在 ≠ 损坏**：不存在 = 空集；0 字节/缺失目录不能在读路径上创建。
2. **并发**：WAL + `busy_timeout(5s)` + 极短事务（单条 upsert），事务不跨派发/执行。
3. **键规范化**：`resolve` + `pathkeys.overlap_key`，否则换写法就绕过。
4. **幂等补登记**：只补"日志当前有效"的目录，已解除的不复活；失败进报告不炸启动。
5. **import 环**：`authority.py` 只依赖 stdlib + `pathkeys`；dispatcher 里延迟 import
   `recovery.assess_run`，避免模块级环。
6. **性能**：守卫读权威根用单行 `SELECT 1 ... LIMIT 1`，不做全表；全量 103.67s（基线 103–108s）。
7. **测试隔离**：`tests/conftest.py` autouse 把 `TIANSHU_AUTHORITY_ROOT` 钉到 `tmp_path`，
   子进程继承同一根；真实家目录不会被测试写。
8. **assess_run 不许写**：只读契约由 `test_assess_run_is_read_only` 钉住。
9. **拒绝要能看懂**：`LeaseConflict` 文案写清"前任 run 有未核实副作用（哪几条调用）"。
10. **拒绝必须同时登记两处**：日志（为什么挡）+ 权威根（换数据根也挡），缺一不可。

## 6. 仍未覆盖 / 边界

1. **Windows 实机**：`msvcrt.locking`、命名管道、`\\?\` 路径仍只有实现、没有实机证据（本轮明确不做）。
2. **接管对账假定"前任的副作用都能在磁盘上判"**：本地文件系统成立；如果目标在别的机器/网络盘，
   对账读到的是本机视角。
3. **resume 路径**：对账发生在认领作业之后、租约获取时；拒绝时作业已被本实例认领（runs 状态
   未改），下一次 resume 会重新认领。没有数据风险，但会留一条 claimed 记录，等 TTL/下次认领。
4. **权威根只保护"阻断"这一件事**：租约状态仍只在本数据根；换数据根的两个 daemon 之间没有
   租约互斥（单机单实例靠 DaemonLock，跨数据根不承诺）。
5. **并发撤销/接管**：同一权威根多进程并发写由 SQLite 事务串行化；没有跨数据根的分布式锁。
