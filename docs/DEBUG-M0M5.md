# M0–M5 独立 Debug 报告（对抗性复审）

> 审查对象：`main @ 4eb4ea9`（`docs(m5): 知识库补记合并后的收尾`）。
> 方法：固定 commit → 读实现与测试 → 在 `/tmp/tianshu-audit-m{0m1,2,3,4,5}/` 用**真子进程 / crashpoint / 注入 barrier**做对抗复现 → 按 [DEBUG-M0M1.md](DEBUG-M0M1.md) 的「严重/中等/轻微」分级出结论。**本轮只写报告，未改任何实现。**
> 基线（已实测）：`uv lock --check` 通过 · `ruff check .` All checks passed · `pytest -o addopts="" -q` → **1061 passed, 3 skipped in 99s**。
> 复审者：三个并行子代理各覆盖 1–2 个里程碑，主代理（第二读者）对每条**严重/中等**发现亲自复现后才采信；子代理报「未发现」的项，主代理另行跑了退出判据的承重断言防「假绿」。
> 与上一轮关系：[DEBUG-M0M1.md](DEBUG-M0M1.md) 的 S1–S4 / R1–R5 严重项在后续里程碑已修；**本轮 M0–M5 全域没有新的严重级问题**。

## 总体结论

| 里程碑 | 退出判据（PRD §11） | 裁决 | 严重 | 中等 | 轻微 |
|---|---|---|---|---|---|
| M0 骨架 | 一条 run 的事件日志可重放出相同结果 | **成立** | 0 | 0 | 0 |
| M1 副作用 | kill -9 ×100 0 重复副作用；可撤销且冲突检测生效 | **部分成立** | 0 | 3 | 2 |
| M2 权限与任务 | 越权注入被拦；删除必经审批；租约跨实例不重复 | **大部分成立** | 0 | 2 | 1 |
| M3 可替换 | 契约测试全绿，主循环 0 改动 | **成立** | 0 | 0 | 2 |
| M4 可评估 | 换模前后有可比分数与失败归因 | **成立（scripted 基线）** | 0 | 0 | 1 |
| M5 桌面闭环 | 关 UI 任务继续，重启后恢复 | **成立** | 0 | 0 | 1 |

**合计：严重 0 · 中等 5 · 轻微 7。** 另有 2 处「文档与代码对账」问题（M3、M5）与两批「如实登记的未验证项」（M4 §7、M5 §8）。核心承诺（事件溯源 append-only、重放确定、执行侧授权、崩溃恢复幂等、桌面闭环）都被真探针站住；缺口集中在**边界时序与 fail-open 语义**，不是算法错误。

---

## 中等（5）

### 中-1 · 阻断检查存在 TOCTOU，phase-1 之后不再复查（M1）
- **现象**：`ensure_not_blocked` 只在派发 phase-1 的 `_authority` 里查一次，进入执行守卫后到真正落盘之间不复查；并发的 `recovery.blocked` 在这一窗口插入后，本条写入仍会落盘。
- **位置**：[dispatcher.py:968](../src/tianshu/dispatcher.py#L968)（phase-1 查阻断）vs [tasks.py:655](../src/tianshu/tasks.py#L655) `begin_execution` 守卫、[tools.py](../src/tianshu/tools.py) 写执行均无二次核验。
- **复现（已复现）**：`uv run --frozen python /tmp/tianshu-audit-m0m1/probe2_toctou.py` —— 真子进程在 phase-1 通过后停在 barrier，父进程 append `recovery.blocked`（`PARENT: workspace blocked = True`），子进程继续写 → `CHILD outcome: succeeded effect: applied`、`file_exists_after_block: True`。
- **影响**：违反 [DEBUG-M0M1.md](DEBUG-M0M1.md) S2 修法「在写入事务中校验阻断、避免 check-then-act」与红线 4/11。窗口窄（单条工具 phase-1→写盘之间）、单用户威胁模型下不易触发，故列中等。
- **建议修法**：把阻断核验下沉进 `begin_execution` 守卫内（或让 recover 阻断与执行争同一把写锁），在不可绕过的写边界再判一次。

### 中-2 · 未知 tool_id 的恢复不 fail-closed（M1）
- **现象**：`tool_id` 不在 `SPECS`（未知工具/新版本）时，恢复走「清单存在但无本次条目 → 确定未写」分支，判 `none_applied` 且不阻断；对已知写工具这是对的，对**语义未知的工具**等于替它假设「没写前证据＝没写」。
- **位置**：[recovery.py:172-179](../src/tianshu/recovery.py#L172)。
- **复现（已复现）**：`uv run --frozen python /tmp/tianshu-audit-m0m1/probe1.py` → `UNKNOWN tool recovery: applied= [] none_applied= ['c1'] unresolved= [] blocked= set()`。
- **影响**：违反红线 8（schema 变即新工具）/红线 11（无法判定即 bug）。**但**当前 dispatcher 只发白名单 intent，未知 `tool_id` 进不了活体 run，属**历史日志 / 前向兼容**缺口，非在野 exploit——故中等而非严重。
- **建议修法**：`spec is None` 一律 `unknown` + 阻断，禁止用通用 path/content 猜测未知工具语义。

### 中-3 · 「kill×100 0 重复副作用」证据只覆盖 create（M1）
- **现象**：故障注入脚本模型只发 `list_files` + `create_report`（新建文件），从不 `modify_text`/覆盖写；且不变量 A 读的是 `note.md`（预置后从未被写），真正目标 `report.md` 没被核「写前或写后」。
- **位置**：[test_recovery.py:195](../tests/test_recovery.py#L195)（种 note.md）、[test_recovery.py:208](../tests/test_recovery.py#L208)（A 读 note.md）、[cli.py:246-267](../src/tianshu/cli.py#L246)（scripted 只 create）。
- **复现（静态确认，读码即明）**：M1 退出判据声称的「0 重复副作用」证据面窄于声称——create 路径成立，覆盖写路径无 100 次注入背书。
- **建议修法**：注入矩阵补 `modify×各故障点`；不变量 A 改核真实写入目标。属测试证据问题，不是功能回归。

### 中-4 · 阻断判定对畸形 payload fail-open（M2）
- **现象**：`blocked_workspaces()` 回放 `recovery.blocked/unblocked` 用裸 SQL + `except Exception: continue`——payload 解析不出 `workspace` 就**当成没阻断**继续放行。一个无法判定的阻断事件被静默丢弃。
- **位置**：[guard.py:28-35](../src/tianshu/guard.py#L28)。
- **复现（已复现）**：`uv run --frozen python /tmp/tianshu-audit-m2/guard_failopen2.py` → 某 workspace 只有一条缺 `workspace` 键的阻断事件时 `is_blocked = False`（fail-closed 应为 True）。
- **影响**：违反红线 5/11「无法判定即不得写」。正常 writer 永远带 `workspace` 键，触发需要数据损坏或遗留写入器，故为**防御性 fail-open**、中等。
- **建议修法**：解析失败视为「无法判定的阻断」，抛 `WorkspaceBlocked`，不 `continue` 跳过。

### 中-5 · 目录外目标不在 prepare 阶段拒，给用户一张永远批不了的审批（M2）
- **现象**：`prepare` 对 workspace 外的删除目标不拒（探针 `P4a 未拦`），只在执行侧 [dispatcher.py:996-999](../src/tianshu/dispatcher.py#L996) 报 `c4.outside_workspace_unsupported`。执行侧挡住了（红线 4 成立），但审批阶段先向用户展示一张越界、注定无法执行的审批。
- **复现（子代理已复现）**：`prepare(build_request(tool="delete_file", target=<workspace 外>))` 不抛错；执行侧拒。
- **影响**：不违反「授权只在执行侧」，但破坏审批语义（审批应绑一个可执行的合法动作）。中等。
- **建议修法**：`prepare` 即对目录外目标 fail-fast，别把不可执行的动作送进审批队列。

---

## 轻微（7）

- **轻-1（M1）**：同内容 `modify` 的活体路径判 `applied` 并产 receipt（[tools.py:409](../src/tianshu/tools.py#L409)，探针 `probe3.py`：`effect_state=applied, receipts:1`），而恢复对同一状态判 `none_applied`（[recovery.py:211](../src/tianshu/recovery.py#L211)）。与 PR#6 已接受口径「同内容→none_applied、不产 applied receipt」相悖。建议 before==after 时 `effect_state=none_applied`。
- **轻-2（M1）**：外部 blob 缺失时读取抛**裸 `FileNotFoundError`**（[events.py:464](../src/tianshu/events.py#L464)，探针 `probe3.py`），不是显式域错误。R5 要求「缺失 ref 显式报 unknown」。建议捕获后抛 `EventLogCorrupt`/记 unknown。
- **轻-3（M2）**：`unblock()` 用 `abs(hash(ws)) % 10**10` 造合成 run_id（[guard.py:55](../src/tianshu/guard.py#L55)）。解释器哈希随机化 + 可碰撞，事件分区不可复现。建议用路径 digest 或 uuid 稳定派生。
- **轻-4（M3·文档对账）**：[M3-verification.md §1](M3-verification.md) 用 `git diff dab74cc -- src/tianshu/loop.py | wc -l = 0` 作为「主循环 0 改动」证据，但该命令今天实测输出 **856**（dab74cc 之后 6 个 M4/M5 合法提交动了 loop）。区间式 `git diff dab74cc 49432a5 -- src/tianshu/loop.py | wc -l` = **0**，判据本身成立。建议把 §1 改成区间式并钉死两个 commit。
- **轻-5（M3·测试强度，静态）**：`test_model_contract.py` 的封闭集断言只校 `len == 13`（[test_model_contract.py:397 附近](../tests/test_model_contract.py)），集合内容可被静默换血仍绿；且 `schemas/` 无 C1 enum 可与代码互绑（PRD §5.1 已明示 C1 未冻结，属登记状态）。建议断言与字面 13 值全集相等。
- **轻-6（M4·归因，静态）**：`_attribute` 把 `tool.*` 前缀失败归到 **policy** 层（[report.py:235](../src/tianshu/eval/report.py#L235)），而 `tool.read_observed`/`tool.attempted` 是工具证据类判据，分层归因计数偏向 policy。不违反红线（attribution 自称启发式），语义有偏差。建议归 `tool` 层或注释说明。
- **轻-7（M5·文档登记）**：`kill -9 Host` 会让 Daemon 作为独立进程继续跑并残留 `run/host.lock`（实测 `probe=True`、进程存活）。这本是 [M5-desktop.md:229](M5-desktop.md) 的**明文设计**（「Host 被杀 → Daemon 跑到下次显式 shutdown」，正是「关 UI 任务继续」），不是违约；但 daemon 侧无父进程看门狗，该差异只在设计表出现、未进 [M5-verification.md §8](M5-verification.md)。建议补登，避免「Host 被强杀后谁回收 Daemon」被当新问题反复报。

---

## 已复现 / 已验证 OK 的核心承诺（防「假绿」承重断言）

- **M0**：append-only 触发器真禁 UPDATE/DELETE；`finish_reason` 越界（error/unknown/length/refusal/None）全 `ValueError` fail-fast；scripted run → replay **逐字确定、不重新协商**（`ok=True/2 turns/msgs_verified=2`）；usage 全未知记 `unknown_usage_calls=3`、不谎报预算。
- **M1 恢复**：S1 反例（同内容/目标消失/缺 manifest）正确判 `none_applied` 或 `unknown`+阻断；R2 undo 绕过已修——`apply_undo` 在阻断 workspace 抛 `WorkspaceBlocked`、文件不动（探针 `probe4_undo.py`）；S4/R3 缺 blob 预检零修改、同路径逆序撤销回链最早 preimage；result+receipt 同事务、大 payload 先 fsync 原子发布；四故障点×恢复幂等绿。
- **M2 授权**：未审批删除被执行侧拒（`status=rejected rule=c4.approval_state`）；审批绑动作+参数且一次性（改参/二次消费 `ApprovalError`）；`..`/符号链接逃逸被拒且外部文件仍在；无字符串黑名单；父子目录 `LeaseConflict`；**外来实例**（[dispatcher.py:890](../src/tianshu/dispatcher.py#L890) `dispatch.foreign_instance`）与**非运行态 Run**（[:909](../src/tianshu/dispatcher.py#L909) `dispatch.not_running`）均在派发边界拒——此两条子代理未活体复现，主代理读码确认 + 测试覆盖（`test_dispatch/test_run_control/test_job_claims` 等 25 处引用）；`claim_next` 判定+写入同一 `BEGIN IMMEDIATE`（[jobs.py:325-330](../src/tianshu/jobs.py#L325)）；C4 rule id 43 个三处同步（`test_rule_ids.py` 14 passed）；fence 同事务释放（[tasks.py:503-541](../src/tianshu/tasks.py#L503)）；真子进程并发 `two_workers_claim_once` 等 5 passed。
- **M3 可替换**：两 adapter 参数化跑同一套 **69 条**契约测试（FakeTransport 零网络，HTTP 分类用真 `UrllibTransport` 只替 urlopen）；两 PR 区间对 loop.py diff = 0；未映射 stop_reason 两 adapter 全抛 `ModelError(kind=protocol)`；`ModelError` 是真对象且 `KINDS` 越界抛错（[model.py:85-101](../src/tianshu/model.py#L85)）；`LocalExecBackend.isolation="none"`、MemBackend 明确非沙箱（[exec.py:77](../src/tianshu/exec.py#L77)）。
- **M4 评测**：replay/evaluate 严格分离（`test_eval_separation` 8 passed，红线 10）；unsafe 一票否决 → 真实跑 `evaluate --case N1 --repeat 3` 得 **EXIT 4**、N1 其它判据全绿仍 unsafe（[report.py:432](../src/tianshu/eval/report.py#L432)/[cli.py:1077](../src/tianshu/cli.py#L1077)）；20 条 / 五类 / `SUITE_VERSION="m4.4"`；崩溃 case 用真 `subprocess`+`TIANSHU_FAILPOINT`，退出码非 77 即判「故障点未触发」。
- **M5 桌面**：`host run → host stop` 真子进程无孤儿（`probe=False`、两进程消失、`run/` 只剩 `ipc.key`）；断线不烧核（5s CPU 增量 0.03s，含 `kill -9` 硬断线）；关全部订阅者后 Run 继续并落盘、重启 daemon 恢复一致；token 不进 URL、SSE 用 fetch；五个 M5 测试文件 **110 passed**。

## 静态疑点（未复现，登记待查）
- **M2**：否决缓存只按 `run_id` 生效（[permissions.py:798](../src/tianshu/permissions.py#L798)），同一 Task 新 Run 会忘掉否决——`run start` 是否可被模型侧触发未验证；`revoke_consumed`（[permissions.py:720](../src/tianshu/permissions.py#L720)）依赖 [dispatcher.py:741](../src/tianshu/dispatcher.py#L741) 的 dispatch 状态核验，未逐条探针验证。
- **M1**：`create_report` 先 exists 检查再 `os.replace`（[tools.py:362-370](../src/tianshu/tools.py#L362)），对**外部**进程仍有覆盖 TOCTOU；但 lease 只防受控 run，PRD 非目标含「防本机用户」，影响有限。

## 未验证项（照抄各 verification §7/§8，不许写成已通过）
- **M4**：真实换模两 provider 分数对比未跑（无 key，全 scripted）· Windows 上 `tianshu evaluate` 未单独实机验证 · `memfs` 后端未覆盖（`grep memfs eval/` 零命中）· 真超时未覆盖（超时 case 全是注入崩溃）。
- **M5**：Windows 实机（仅 CI `windows-latest`；命名管道偶发握手失败 issue #50 根因未定位，刻意**不加** `continue-on-error`）· 真实模型未验证 · Tauri/托盘/安装包未做 · 陈旧锁 `LOCK_GRACE_MS=2_000` 宽限窗（[daemon.py:87](../src/tianshu/daemon.py#L87)）· 推送在主循环同步发送。

## 建议处理顺序
1. **先封 fail-open 语义**（中-1 TOCTOU、中-2 未知工具、中-4 guard）：不确定/无法判定一律 `unknown`+阻断，把阻断核验移到不可绕过的写边界。
2. **再补 M1 证据**（中-3）：注入矩阵加 modify/覆盖写，把「kill×100」扩到全部写类工具，别在窄证据上宣称完成。
3. **修审批语义**（中-5）：目录外目标在 prepare 阶段即拒。
4. **轻微按需**：轻-1/2 口径与域错误、轻-3 稳定 id、轻-6 归因分层；**轻-4 / 轻-7 是纯文档对账，可顺手修**（AGENTS §5 要求不留假话与悬空引用）。
5. 静态疑点（M2 否决缓存 / revoke 判定）与两批未验证项，各自开工单跟踪销账，不当已解决。

---
*探针脚本保留在 `/tmp/tianshu-audit-m0m1`、`/tmp/tianshu-audit-m2`（未入库，可删）。本轮仓库零改动，未提交任何文件。*

---

# 修复记录（2026-11-10）

> 与正文的关系：正文**逐字保留**为当时的审查记录，不改写；裁决、修法与证据追加在这里。
> 修复分支 `fix/m0m5-audit`（PR #56），基线 `main @ 4eb4ea9`。
> 验收口径沿用本仓库惯例：能机械复现的给命令与实测输出；跑不了的写明原因，不写成结论。

## 裁决总表

| # | 发现 | 裁决 |
|---|---|---|
| 中-1 | 阻断检查 TOCTOU，phase-1 之后不再复查 | **已修**：执行边界二次核验 + 恢复写阻断与派发共享同一把执行守卫 |
| 中-2 | 未知 `tool_id` 的恢复不 fail-closed | **已修**：`spec is None` 一律 unknown + 阻断 |
| 中-3 | 「kill×100 0 重复副作用」证据只覆盖 create | **已补强**：矩阵扩到 create + 覆盖写（48/52），不变量改核真实写入目标 |
| 中-4 | 阻断判定对畸形 payload fail-open | **已修**：解析不了 = 无法判定 = 拒绝写入 |
| 中-5 | 目录外目标给用户一张永远批不了的审批 | **误报**：`prepare` 当场就拒（`dispatch.lease_lost`），没有生成审批；未改产品代码，改加钉住测试 |
| 轻-1 | 同内容 `modify` 活体判 applied 并产凭据 | **已修**：与恢复同口径判 `none_applied`，不产凭据 |
| 轻-2 | 缺 blob 抛裸 `FileNotFoundError` | **已修**：抛 `EventLogCorrupt`，消息带 seq 与 blob 路径 |
| 轻-3 | `unblock()` 用 `hash()` 造 run_id | **已修**：改内容摘要，跨进程稳定 |
| 轻-4 | M3 §1 的 `git diff` 是半点不是区间 | **已修（文档）**：区间式 + 钉死两端 commit，并解释为什么半点会读出 856 |
| 轻-5 | 封闭集只校 `len == 13` | **已修（测试）**：与 13 个字面值全等 |
| 轻-6 | `tool.*` 失败归因到 policy 层 | **已修**：归 `tool` 层 |
| 轻-7 | Host 强杀后 Daemon 残留未登记 | **已补登（文档）**：`M5-verification.md` §8 |

## 中-1 · 阻断检查的 TOCTOU

**修法**三道，缺一道都还能被插进来：

1. 执行边界二次核验：`FileExecutor.execute()` 在**改文件前最后一步**按"这次真正要写的那个路径"再判一次（`guard.assert_paths_not_blocked`）。
2. 守卫内二次核验：`TaskStore.begin_execution()` 在拿住本机执行守卫之后、写 `execution.started` 之前，按资源路径再判一次。库调用方绕过派发时这条在。
3. 让两件事抢同一把锁：`recover_run(..., data_root=...)` 给定时，恢复整个过程在本机执行守卫内进行（CLI `recover`、daemon 启动恢复、eval 崩溃恢复三处都传了；daemon 那条在 bind 之前，此时没有驱动线程，不会自锁）。于是「落阻断」与「落盘」有了全序：要么阻断先落库（这次写入被拒），要么写入先完成（阻断只影响之后的写入）。

**证据**：

```
# 复审原探针（在守卫内暂停、父进程插入 recovery.blocked，再让它继续写）
$ uv run --python 3.12 python /tmp/tianshu-audit-m0m1/probe2_toctou.py
PARENT: workspace blocked = True
CHILD outcome: noop effect: none_applied file_exists: False
RESULT file_exists_after_block: False          # 修之前是 True
```

新增回归：`tests/test_failclosed.py::test_block_inserted_inside_execution_guard_still_blocks_the_write`（用**另一条 SQLite 连接**在守卫内落阻断，等价于另一个进程；断言 `rule_id=dispatch.workspace_blocked`、文件没被创建、没有凭据）与 `::test_recovery_blocking_write_contends_for_the_execution_guard`（守卫被占时恢复必须等着，释放之后才落阻断）。

**残余风险**（如实记）：库调用方调 `recover_run()` 不传 `data_root` 时没有跨进程线性化，只剩第 1、2 道（口径不变，只是窗口收窄到"检查→os.replace"之间）；`unblock()` 不抢守卫——它是人工解除阻断，语义上属于"恢复可写"，不参与写序。

## 中-2 · 未知工具的恢复

**修法**：`SPECS.get(tool_id)` 为 `None` 时立刻判 `unknown` 并计入 unresolved（进而阻断该目录），不再落到「清单里没有条目 → 确定没写」那条推理上——那条推理的前提是"我知道这个工具的写入协议"，未知工具没有这个前提。

**证据**：

```
$ uv run --python 3.12 python /tmp/tianshu-audit-m0m1/probe1.py | grep 'UNKNOWN tool'
UNKNOWN tool recovery: applied= [] none_applied= [] unresolved= ['c1'] blocked= {'…/ws3'}
# 修之前：applied= [] none_applied= ['c1'] unresolved= [] blocked= set()
```

回归：`tests/test_failclosed.py::test_unknown_tool_recovery_is_unknown_and_blocks`。

## 中-3 · M1 的注入证据面

**修法**（`tests/test_recovery.py` 重写 + `docs/M1-verification.md` §3/§5 对账）：

- 子进程不再走 `cli --model scripted`（那个脚本模型只发 list_files + create_report），改用公开 API 装配同一条 run 路径（`build_stack` + `AgentLoop` + `ScriptedModel`），命令它发 `modify_text`；`src/tianshu/cli.py` 未改。
- 矩阵从「4 故障点 × create」变成「4 故障点 × (create, overwrite)」：100 次 = 48 create + 52 overwrite，每个组合 ≥12 次，随机只用来打乱顺序（种子固定）——覆盖面由配平保证，不交给抽样运气。
- 不变量 A 改为从 `tool.intent` 参数解析**真实写入目标**（`report.md`）再逐字节核对，预置后从未被写的 `note.md` 反向断言没被动过；B 改为「每个写入 intent 恰好一条 result」；C 增加 `before_state` 与磁盘 side 的核对；新增 D「applied 恰好一张凭据、未执行不产凭据」。覆盖写必经审批：子进程按 `approvals.decide` + `resume_run` 的同一公开入口模拟人批准，测试里断言 `approval.requested/decided/consumed` 三段都在（授权没被绕过）。
- 负控：临时副本里让子进程只写半截内容 → 8 个用例红；把 A 改回读 `note.md` → 目标用例红。说明新断言不是空转。

**证据**：`tests/test_recovery.py` → **11 passed in 10.87s**（100 次注入用例 9.78s）。

**没做的**：`delete_file` 仍不在矩阵里（删除类的恢复判据是 `absent`、撤销是按 preimage 重建，另开一组断言）→ issue #54。注意删除**不是**因为"要审批"才不覆盖：覆盖写同样要审批，已经在矩阵里了。

## 中-4 · 畸形阻断记录的 fail-open

**修法**：`guard.scan_blocked()` 不再 `except: continue`，把「判定不了的事件 seq」和「阻断集合」一起交出来；`blocked_workspaces()` 遇到它就抛 `EventLogCorrupt`（这份日志不能回答"哪些目录被阻断"），写入守卫（`ensure_not_blocked` / `assert_paths_not_blocked`）转成 `WorkspaceBlocked`——**无法判定即不得写**，而不是静默放行。

**证据**：

```
$ uv run --python 3.12 python /tmp/tianshu-audit-m2/guard_failopen2.py
tianshu.errors.EventLogCorrupt: 阻断记录无法判定（seq=1 解析不出 workspace），无法回答哪些目录被阻断
# 修之前：is_blocked: False
```

回归：`tests/test_failclosed.py::test_malformed_block_event_fails_closed`（连派发都拒：`rule_id=dispatch.workspace_blocked`、文件没被创建）。

## 中-5 · 目录外目标 → 误报

**复核结论：不成立。** `prepare` 在 `_authority` 的租约覆盖判定就拒了（早于 `ApprovalStore.request()`），不会生成审批。实测：

```
decision.kind = rejected
decision.rule_id = dispatch.lease_lost
approvals.find_for(request) = None
approval.requested 事件 = []
dispatcher.waiting() = []
目标文件仍在 = True
```

原报告的依据是探针"没有抛异常"——但 `prepare` 的契约是**返回**一个 rejected 决定并落 `dispatch.rejected`，不是抛。为防以后有人真把"先建审批、执行侧再拒"改成产品行为，加了钉住测试 `tests/test_dispatch.py::test_zhong5_outside_workspace_delete_never_reaches_approval`（四个断言 + 文件不动）。产品代码零改动。

## 轻-1 ~ 轻-7

- **轻-1**：`_modify_text` 在 `before == data` 时直接返回 `succeeded / none_applied`（不调 `prewrite`、不产凭据），与恢复侧 `before==expected_after → none_applied` 同口径。回归 `tests/test_failclosed.py::test_same_content_overwrite_is_none_applied`。
- **轻-2**：`_row_to_event` 把 blob 读取失败收敛成 `EventLogCorrupt`（消息带 `seq=` 与 blob 路径，`from exc` 保留底层异常）。`errors.py` 的 docstring 同步扩到三种情形（hash 不符 / blob 缺失 / 阻断记录解析失败）。回归 `tests/test_debug_regressions.py::test_light2_missing_payload_blob_is_event_log_corrupt`。注意：`tests/test_sandbox_backend.py` 里 4 处 `FileNotFoundError` 是后端原语契约（工具层靠它区分 failed 与 unknown），**不能**改。
- **轻-3**：`unblock()` 的合成 run_id 改为 `unblock_<sha256(规范化路径)[:10]>`。回归 `tests/test_failclosed.py::test_unblock_run_id_is_stable_across_processes`（两个不同 `PYTHONHASHSEED` 的子进程必须得到同一个 id）。
- **轻-4**：`docs/M3-verification.md` §1 判据改成 `git diff dab74cc 49432a5 -- src/tianshu/loop.py | wc -l`，并写明为什么半点会读出 856（M4/M5 合法改过 `loop.py`）。
- **轻-5**：`test_finish_reason_closed_set_holds_all_13` 改为与 13 个字面值集合**全等**（等长换血也会红）。
- **轻-6**：`eval/report.py::_attribute` 的 `tool` 前缀失败归 `tool` 层。回归 `tests/test_eval_report.py::test_attribution_failed_tool_check_goes_to_tool_not_policy`（断言 `== ["tool"]`，同时证明没把 policy 带上）。
- **轻-7**：`docs/M5-verification.md` §8 补登「Host 被强杀后 Daemon 残留 + `run/host.lock` 不回收」——这是 `M5-desktop.md` §4 的明文设计，不是违约，但不登记就会被当成新问题反复报。

## 静态疑点与未验证项的去向

- M2 静态疑点（否决缓存只按 `run_id`、`revoke_consumed` 未逐条验证）→ **issue #53**。
- M1 `delete_file` 注入矩阵 → **issue #54**。
- M4 四条未验证项（真实换模对比 / Windows `evaluate` / memfs 评测路径 / 真超时）→ **issue #55**。
- M1 `create_report` 的 `exists → os.replace` 对外部进程仍有覆盖 TOCTOU：PRD 的非目标包含"防本机用户"，**按已知限制接受**，不单独开工单；这里留一句是为了下次有人读到时不至于以为没考虑过。
- M5 的未验证项（Windows 实机、Tauri 外壳、真实模型、同毫秒并发审批）仍以 `docs/M5-verification.md` §8 为准，本轮没有新增结论。

## 本轮的绿/红声明

```
uv run --python 3.12 pytest -o addopts="" -q   → 1074 passed, 3 skipped in 97.83s
uv run --python 3.12 ruff check .              → All checks passed!
```

基线是 1061 passed / 3 skipped，净增 13 条（`test_failclosed.py` 6 + `test_recovery.py` 4 + 三条单点回归 3）。**没有**新增 skip、没有 `continue-on-error`、没有把红灯改成跳过；`中-5` 是误报，所以它对应的改动是一条测试而不是一段产品代码。
