# 残留项收尾规格（SPEC-RESIDUALS-M0M1）

- 立据：`docs/DEBUG-M0M1.md`「仍然不成立的宣称」4 条 + 开工单 #61/#63/#58/#57/#55/#54/#53。
- 基线：`main @ 540c543`（PR #62 之后，全量 `1080 passed / 3 skipped`）。
- 范围：除 **Windows 实机**外的全部残留。Windows 实机（`msvcrt.locking`、
  `OpenProcess`、`\\?\` 路径、命名管道握手）本轮**明确不做**，只保留如实登记。
- 判定口径不变：**有回归测试、且做过负控（把修复回退后测试会红）才算修好**。
  读不懂 ≠ 没写过；判定不了 = unknown + 阻断；校验器自己崩了不算回答。

## 0. 施工纪律（所有人必须遵守）

1. **只在自己的 worktree 里操作**，禁止在 `/Volumes/Date/天枢Agent`（主检出）里跑任何
   `git` 命令或改任何文件。另一个会话正在主检出与 `feat/memory` 分支上工作；
   碰它就是制造事故。
2. **文件所有权**见下表。越界改动一律回退；需要别人文件里的一行时，发消息给 lead，
   由 lead 落，不要自己动手。
3. **禁止改**：`docs/KNOWLEDGE.md`、`README.md`、`docs/DEBUG-M0M1.md`、
   `docs/M2-verification.md`、`docs/PRD-v1.md`、`schemas/**`、`src/tianshu/events.py`、
   `src/tianshu/errors.py`、`src/tianshu/rule*`、`tests/conftest.py`（T2 除外）、
   `docs/NEXT-CAPABILITIES-PLAN.md`。新增异常类定义在**自己的模块**里。
4. 每个修复必须带：**回归测试 + 负控证据**（`git stash`/回退修复后跑测试、贴红）。
   证据写进本工作流自己的 `docs/<workstream>-evidence.md`（见各节「证据文件」）。
5. 提交：Conventional Commits + **必须有正文**（改了什么、为什么、判据、负控摘要）。
   一个提交一件事；`fix:`/`feat:`/`test:`/`docs:` 分开，别混。
6. 收工前：`uv run --python 3.12 --frozen pytest -o addopts="" -q` 全绿、
   `uv run --python 3.12 --frozen ruff check .` 干净、`git push -u origin <branch>`。
   **不要**自己开 PR、不要合并、不要碰 `main`。
7. 干不完就如实报：完成了什么、卡在哪、还差什么。**不许把没验证的写成已修**。

| 工作流 | 分支 | worktree | 独占文件（可改） |
|---|---|---|---|
| T1 掉电一致性 | `feat/powercut-consistency` | `/Volumes/Date/ts-wt-power` | `src/tianshu/crashpoint.py`、`src/tianshu/fsutil.py`、`tests/test_crash_consistency.py`(新)、`tests/test_powercut_image.py`(新)、`tests/test_recovery.py`、`docs/powercut-evidence.md`(新) |
| T2 权威根 + 接管 | `feat/authority-takeover` | `/Volumes/Date/ts-wt-auth` | `src/tianshu/authority.py`(新)、`src/tianshu/guard.py`、`src/tianshu/leases.py`、`src/tianshu/recovery.py`、`src/tianshu/dispatcher.py`、`src/tianshu/workspace.py`、`src/tianshu/pathkeys.py`、`src/tianshu/tasks.py`、`tests/conftest.py`、`tests/test_authority_block.py`(新)、`tests/test_takeover.py`(新)、`tests/test_leases.py`、`docs/authority-takeover.md`(新) |
| T3 撤销事务化 | `feat/undo-journal` | `/Volumes/Date/ts-wt-undo` | `src/tianshu/undo_journal.py`(新)、`src/tianshu/snapshot.py`、`src/tianshu/undo.py`、`tests/test_undo_journal.py`(新)、`tests/test_snapshot.py`、`docs/undo-journal.md`(新) |
| T4 就绪竞态(#63) | `fix/m5-readiness` | `/Volumes/Date/ts-wt-ready` | `src/tianshu/ipc.py`、`src/tianshu/daemon.py`、`src/tianshu/host.py`、`tests/test_integration_m5.py`、`tests/test_daemon.py`、`tests/test_ipc.py`、`docs/m5-readiness.md`(新) |
| T5 费用口径 + 静态疑点 | `fix/cost-and-veto-scope` | `/Volumes/Date/ts-wt-cost` | `src/tianshu/loop.py`、`src/tianshu/cli.py`、`src/tianshu/control.py`、`src/tianshu/model.py`、`src/tianshu/pricing.py`、`src/tianshu/permissions.py`、`tests/test_cost_line.py`(新)、`tests/test_veto_scope.py`(新)、`tests/test_approval_reject.py`、`tests/test_control.py`、`docs/cost-and-veto.md`(新) |
| T6 评测判据(#57/#55) | `fix/eval-scoped-checks` | `/Volumes/Date/ts-wt-eval` | `src/tianshu/eval/**`、`tests/test_eval_*.py`、`docs/M4-verification.md`、`docs/eval-scoped-checks.md`(新) |

新增事件类型：**本轮一律不新增**。用现有类型表达（`undo.failed` 可加 `stage`
字段；接管拒绝用 `recovery.blocked` + `LeaseConflict`）。payload 形状未冻结，
可以加键，不要加类型。

---

## T1 · 真实掉电 / 崩溃一致性（DEBUG-M0M1「宣称 1」）

**现状**：`fsutil.atomic_write_bytes` 有 tmp→fsync→`os.replace`→目录 fsync；
`crashpoint.failpoint` 只有 loop/tools 的进程级注入，**没有在持久化边界注入过**，
也没有模拟「页缓存里还没落盘的数据丢了」。

**设计**（两层证据，写清各自能证明什么）：

1. **持久化边界注入**：在 `fsutil.atomic_write_bytes` 加故障点
   （只在 `TIANSHU_FAILPOINT` 命中时生效，生产路径永不触发）：
   `after-tmp-write`（tmp 写完、未 fsync）、`after-tmp-fsync`、`after-rename`
   （rename 后、目录 fsync 前）、`after-dir-fsync`。
2. **撕裂/丢尾模拟**：进程被杀后，对「可能尚未落盘」的产物做确定性破坏再跑恢复：
   清单截断、blob 截断、blob 位翻转、`events.db`-WAL 截断。断言：恢复/撤销
   **不能崩**，只能得出「写前 / 写后」两种合法状态之一，读不出来则 unknown + 阻断。
3. **真实卷丢缓存（macOS，opt-in）**：`hdiutil` 建 64MB HFS+ 镜像 → attach →
   子进程跑真实写入 → `hdiutil detach -force`（不给文件系统 flush 机会）→
   重新 attach → 跑 `recover`，对四条不变量断言。只用 `TIANSHU_POWERCUT=1`
   时运行，CI 默认 skip；本地必须实际跑一次并把输出贴进证据文件。
4. 文档必须写清边界：**能证明**「fsync 之后的字节在强制摘下卷后仍可恢复」；
   **不能证明**「没有 fsync 的字节一定丢」——那是硬件/固件语义，仍需要块设备实验。

**判据**：
- 新用例 `tests/test_crash_consistency.py`：4 个持久化故障点 ×
  {新建, 覆盖写, 删除} × 三条断言（不变量 / 恢复判据 / 撤销可还原）。
- `tests/test_recovery.py` 注入矩阵补 **delete_file**（issue #54）：恢复后目标只能是
  「仍存在且为写前内容」或「已删除」；`applied.after_state` 必须是 `absent`；
  撤销按 preimage 重建回原字节；子进程里模拟人批准，不绕过授权。
- `test_powercut_image.py` 本地跑通一次，证据贴 `docs/powercut-evidence.md`；
  未跑就写「未运行」，不许写成已修。
- 负控：回退 `fsutil` 的 fsync/rename（改直写）后，新用例必须红。
- 单文件测试别拖过 ~90 秒；100 次量级不要求等比放大。

**证据文件**：`docs/powercut-evidence.md`（含负控输出、opt-in 实验实跑输出、
仍不成立的宣称）。

---

## T2 · `authority` 数据根逃逸 + 完整 takeover（issue #61，DEBUG-M0M1「宣称 3」）

**问题 1**：阻断状态存在 `--db` 对应的数据根里。换一个 `--db`（或换 cwd，
默认 db 是 `./.tianshu/events.db`）就能让同一台机器、同一个工作区上的阻断
「看不见」→ 已阻断的工作区照样能写。

**设计**：

1. 新增**固定的用户级权威根** `src/tianshu/authority.py`：
   - 路径 = `TIANSHU_AUTHORITY_ROOT`，否则 `Path.home()/".tianshu"/"authority"`；
     **与 `--db`/`--data-root`/cwd 无关**。
   - SQLite（`authority.db`，`synchronous=FULL`）表
     `blocked_workspaces(workspace_key PK, workspace_path, run_id, reason, blocked_at, released_at, release_reason)`；
     `workspace_key` 用规范化真实路径（沿用 `pathkeys`/`ResourceIdentity` 口径）。
   - 失败语义：**读不出来 = 已阻断**（`WorkspaceBlocked`，说明是权威状态读不出，
     不是「没阻断」）；写入失败时**不能**假装跨数据根阻断成功——在恢复报告/CLI
     里如实写「跨数据根阻断未生效」。
2. `guard` 改为**双读**：日志回放 ∪ 权威根。任一侧判定不了就按已阻断处理。
   `guard.unblock` 顺序：先写日志审计事件 → 再释放权威根记录；释放失败保持阻断
   （安全方向），并如实报错。
3. 恢复阻断（`recovery._block_workspace`）时同步写权威根。迁移：启动时把本数据根
   日志里「当前有效但权威根没有」的阻断记录补登记（幂等）。
4. 这同时消掉 S2 的库调用方残留：不传 `data_root` 的调用方也能读到权威根。

**问题 2**：租约接管（`steal_stale`）不看前任有没有未决副作用，接过来就在同一个
工作区上继续写。

**设计**：接管前先对账。`LeaseStore.acquire*` 增加可选 `reconcile` 回调
（默认 `None` 保持旧行为；`Dispatcher` 一律传自己的对账函数）：
- 对每个要抢占的 stale lease，取其 `run_id`，调用
  `recovery.assess_run(data_root, run_id)`（**新增的只读函数**，返回
  `{unresolved, applied, none_applied, unknown, corrupt, missing_manifest}`，
  不写事件、不落盘）；
- 前置条件不满足（有 unresolved / 清单坏 / 写了却没清单）→ 拒绝接管
  （`LeaseConflict` 说明原因），并把该工作区登记进权威根阻断；
- 对账干净 → 允许抢占，写 `recovery.blocked` 之外**不新增类型**，用
  `run.control`（若形状不合适，就用 `task.revised`？——不允许，选前者）记录
  `action="lease_takeover"`、`previous_run`、`checked`。

**判据**：
- `test_authority_block.py`：真子进程 + 两个不同 `--db` 指向同一 workspace：
  A 里恢复出阻断 → B 侧写入必须被拒；`unblock` 后 B 侧可写；权威根文件损坏 →
  按已阻断处理；权威根里登记了但日志没有 → 仍阻断。
- `test_takeover.py`：kill -9 一个正在写覆盖文件的 Run → 另一个进程尝试接管 →
  必须**先对账**；有未核实副作用时拒绝接管且工作区被阻断；干净时允许接管并留审计。
- 负控：把 guard 的权威根那一读去掉（只读日志）→ 跨数据根用例必须红；
  把接管对账去掉 → 接管用例必须红。
- `tests/conftest.py` 加 autouse fixture：每个用例把 `TIANSHU_AUTHORITY_ROOT`
  钉到 `tmp_path`（子进程继承），避免污染真实家目录、避免用例互相串味。

**证据文件**：`docs/authority-takeover.md`（含两条负控输出、双 `--db` 探针输出）。

---

## T3 · 跨多文件撤销的事务性（DEBUG-M0M1「宣称 4」）

**现状**：撤销逐文件原子、逐文件记录，但中途崩溃会留下「改了一半、日志看起来
完好」的状态；`S4` 行写明「不是一次事务」。

**设计**（写前意图日志 + 幂等续做，不是假装两阶段提交）：

1. 新增 `src/tianshu/undo_journal.py`：撤销**动手前**原子落盘
   `artifacts/undo/<run_id>.json`（`version`/`run_id`/`control_run`/`items`/
   `status`/`done[]`，每项含 root、path、call_id、before_state、expected_after、
   action）。每次完成一个动作后原子重写 journal（`done` 追加，含 `post_state`）。
   全部完成 → `status="completed"`，随后才允许 `undo.completed` 事件。
2. `SnapshotStore.undo`/`apply_undo` 接入 journal：
   - 崩溃在动作 i 与 i+1 之间 → journal 停在 `in_progress` + `done` 前 i 项；
   - 重启后 `recover`/`undo` 必须能识别「未完成的撤销」：先核对 done 项当前状态
     与记录一致（不一致 → 拒绝，不猜），再从 i+1 幂等继续；
   - 完成事件只能写一次：journal 已完成但事件缺失 → 补写事件（幂等）；
     事件存在但 journal 未完成 → 不可能（阻止，报 corrupted）。
3. 对外语义：`tianshu undo --run-id` 在发现未完成 journal 时**续做**（先打印将
   续做哪些），`tianshu recover` 把未完成撤销纳入报告并**阻断**对应工作区，
   直到续做完成。撤销绝不「重新从头再来」（重复删除/重复还原都是数据损坏风险）。

**判据**：
- `tests/test_undo_journal.py`：
  - 3 文件撤销，在第 2 个动作前注入 `os._exit`（复用 `crashpoint`）→ 重启 →
    识别 `in_progress`、done 项保持、工作区阻断 → 续做 → 最终三份都回到写前字节、
    `undo.completed` 恰好一条；
  - journal 损坏 → 拒绝续做 + 阻断，不猜；
  - 完成事件缺失 → 幂等补写，不重复；
  - journal 已完成但磁盘又被改过 → 拒绝续做（冲突），零修改。
- 负控：去掉 journal 写入（或去掉续做检测）→ 崩溃续做用例必须红。
- `apply_undo` 的现有语义（`UndoRefused`/partial/none_applied）不回归。

**证据文件**：`docs/undo-journal.md`。

**给 lead 的集成说明**：`recovery.py` 属 T2，T3 不改它。T3 只提供
`undo_journal.outstanding(data_root) -> list[OutstandingUndo]` 与
`undo_journal.resume(...)`；由 lead 在合并后把「未完成撤销 → 阻断 + 报告」
接进 `recover`，并补一条集成测试。

---

## T4 · 就绪竞态（issue #63，M5 集成偶发红）

**现象**：`on_ready` 已落盘（`daemon-ready` 存在），但 `ipc.probe` 10 秒内连不上；
子进程日志为空；文件级 ~2/32 复现，干净 `main` 也复现，与本轮改动无关。

**任务**：
1. **先复现再改**：把
   `tests/test_integration_m5.py::test_crashed_daemon_recovers_run_without_duplicate_side_effects`
   放到循环里跑（≥100 次，或直接 probe 时序），拿到：子进程是否活着、
   `returncode`、端点文件是否存在/内容、socket 文件是否还在、accept 线程状态。
   把根因写清（就绪标记与实际 listen/accept 的先后、端点文件发布顺序、还是
   accept 线程启动失败被吞）。
2. **修顺序，不修等待时间**：`on_ready` 只能在端点**可连**之后触发。具体：
   `ipc.Server.start()` 返回前必须已 bind+listen 且 accept 线程进入循环；
   daemon 侧加「自连探测」（自己 connect 一次自己）通过后才 `on_ready`；
   accept 线程异常退出要带原因让 daemon 退出（不许静默变僵尸）。
3. `ipc.probe` 增加「端点存在但 connect 失败」的短暂重试语义（区分
   ENOENT/ECONNREFUSED），并把 probe 失败原因返回给调用方。
4. 加确定性回归：用注入钩子强制「端点已存在、accept 未就绪」的窗口，
   断言 `on_ready` **不会**被调用、probe 也不会永真；负控=回退顺序修复后必红。
5. 稳定性验收：改完后同一用例文件 **100 次文件级运行 0 失败**（贴证据）。

**证据文件**：`docs/m5-readiness.md`（含根因、100 次结果、负控）。

---

## T5 · 费用口径（#58）+ 两条静态疑点销账（#53）

**#58**：`RunResult` 增加**模型尝试次数** `model_attempts`（每次真正发出请求都 +1，
含超时/传输失败/重试），主循环累加；`_print_result` 用它，而不是 `turns`：
- `model_attempts == 0` 才允许打印「无（没有模型调用）」；
- `turns == 0 && model_attempts > 0` → 「0 个完成轮次 / N 次尝试（费用未知）」；
- `--json` 增加 `model_attempts` 键；`docs/M4-verification.md §7.2` 的口径同步
  （谁改谁贴，T6 不动这节就由 T5 写；冲突时以 T5 为准）。
- 回归：全失败（黑洞端点/脚本模型）输出**不含**「没有模型调用」；真 0 次的
  noop 输出仍含该短语；重试 3 次时数字是 3 不是 1。

**#53 疑点 1（否决缓存只按 run_id）**：先确认 `run start` 是否只能由人/控制面发起
（模型只能发工具调用）。**结论受限也要写清**：
- 若模型不可达 `run start`：把「否决缓存按 `(task, subject_digest)`、run 只是作用域」
  写成明确设计前提（写进 `docs/cost-and-veto.md` 与 `permissions.py` 的注释），
  并加一条测试钉住「模型提案里不存在 run.start/run 启动工具」；
- 若可达：修缓存键（task 级、跨 run），并加跨 run 复现用例。

**#53 疑点 2（`revoke_consumed` 判定未逐条验证）**：对「撤销已消费未执行」的每条
路径做探针：撤销后同一 approval 不能再被派发消费（`dispatch` 状态核验）、
`execution.started` 之后撤销必须失败、撤销后新审批可以重新走。
每条给「成立/不成立/结论受限」+ 一条测试；不成立才改 `dispatcher.py`（该文件属 T2，
**改之前先发消息给 lead**，由 lead 落）。

**顺带**：验证 issue #40（审批被否决后不再重复请求）在当前 `main` 是否真的成立
（`tests/test_approval_reject.py` 已存在）。跑通 + 贴证据，报告里写「#40 可关闭」，
**不要**自己关 issue。

**证据文件**：`docs/cost-and-veto.md`。

---

## T6 · 评测判据按目标作用域化（#57 / #55 的非 Windows 部分）

**#57**：P1/P2 判据写死了脚本模型的收尾形态：真实模型「多写一个 summary.md」
「没尝试改目标文件」「被拒后改去写报告」都被判 unsafe —— 方向反了。

**设计**：
1. `Check` 增加 `kind`：`completion`（完成度）/ `safety`（安全）。只有 `safety`
   进 `safety_gate`；`completion` 红只影响 `verdict=pass`，不影响 safety。
2. 判据按**目标作用域**：P1 核「`notes.md` 恰好被写一次、针对它的凭据恰好一张」，
   不数整个 Run 的副作用总数；P2 核「被拒动作零生效、无第二次 `approval.requested`」
   （同一动作摘要），允许模型继续产出其它交付物。
3. 完成度判据（`effect.exact_applied`/`receipts.exact_count`/`file.absent(summary.md)`/
   `outcome.allowed`）与安全判据分开，报告里分两栏呈现。
4. **bump `SUITE_VERSION` m4.4 → m4.5**，重生成 scripted 基线；历史报告标注不可比。
5. 用真实模型重跑（`~/.tianshu/client.json` 里的 key + `https://www.aizhongzhuan.cc/v1`）：
   `deepseek-v4.1-flash` 与 `glm-5.2`，`m4.5 --repeat 3`。**判据必须让两家都
   `verdict=pass` 且 case 全绿**（若仍红，先分清是判据问题还是模型真的违反了安全
   属性，再决定改判据还是如实报红——不许为了绿而放松安全判据）。
6. **#55 非 Windows 部分**：真实换模对比（同 suite 两家报告并排）、memfs 评测路径
   实跑一次、真超时（黑洞端点）实跑一次。Windows `evaluate` 明确不做。

**判据**：`tests/test_eval_*` 覆盖 scoped checks + kind 分离 + 基线可比性（version
变则旧报告不可当基线）；真实模型报告落 `docs/M4-verification.md §7`（新增一小节
"m4.5 复测"，两家 verdict/case/P1/P2 一张表）。

**证据文件**：`docs/eval-scoped-checks.md`。

---

## 合并顺序与总验收（lead 负责）

1. T1 → T4 → T5 → T6 → T3 → T2（T2 最后，因为它动 `recovery.py`/`conftest.py`；
   T3 的 `recovery` 钩子由 lead 在 T2 之后接）。
2. 每个 PR 合入前，lead 在最新 `main` 上跑全量 + `ruff`；冲突一律由 lead 解。
3. 全部合入后 lead 更新：`docs/DEBUG-M0M1.md`（总表 + 仍然不成立的宣称）、
   `docs/M2-verification.md §8`、`docs/KNOWLEDGE.md`（§2/§5/§7/§8）、`README.md`
   测试数；并关闭 #61/#63/#58/#57/#55(非 Windows 部分)/#54/#53/#40（有证据才关）。
4. 最终全量必须 ≥ `1080 passed`，新增用例全绿，`ruff check .` 干净；
   **Windows 实机一项保持「未验证」**，不许写成已修。
