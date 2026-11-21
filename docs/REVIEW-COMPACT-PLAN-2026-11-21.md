# `/compact` 与计划模式独立 Tier 1 探针复审

日期：2026-11-21。第二读者：独立子会话 `/root/workflow_review`。
本文仅记录第二读者在独立工作树亲自运行、读回的证据，不将作者自查结果当作验收证据。

## 结论与绑定

**非文档代码树的独立探针复审 PASS**，绑定 `d161e060ceefe240a930bc5cbef8d775ca8eb4b6`。工作流原限定范围的已修证据保持有效；作者后续本机全量 exit 1 后，第二读者另确定性复现的 IPC fd 生命周期 P1，已通过同契约红转绿、恢复旧实现重新变红及受影响域回归。最终文档回填仍须另核对，作者新候选全量及 GitHub required CI 另由各自门禁负责；不把定向通过冒充全量或平台实测。

- 基础提交：`2c62128ff071ea1d348acaf422734fbf47ce4626`。
- 首轮冻结 tree：`c6193a7749cc3b036c8df010dbd5c34c2740e041`。
- 后端修复 tree：`23d2920ae082aa6c1fcf2498d837fb3b057e8602`。
- UI 修复 tree：`5b218a331dbad1e5c0d4813f2f545205a909dd4c`。
- 工作流冻结 tree：`bd5672298f5282bfe82b15418d76648fe7da4aaa`。
- IPC 修复后冻结 tree：`d161e060ceefe240a930bc5cbef8d775ca8eb4b6`；其中 `src` 为 `89f02d55c02911da08606b7988790dc6dddfb4b4`，`tests` 为 `5c5c6269d2573230ab5fd9797b836f985a7b9ba1`，`schemas` 为 `3773f5617a944bde97a9e42e0e766d096ff6dcc8`，`ui` 为 `918e8ad439a37d9841049c16bd07aad1801a655d`。
- 每次应用作者冻结增量后，均用独立索引 `git write-tree` 逐字核对。`23d2920…` → `5b218a…` 仅涉及 App、正式 UI 测试和 `ui/dist`，Python 源码、测试与 schema 零变化。
- `5b218a…` → `bd56722…` 仅修复 `tests/test_g2_delegation_contract.py` 的事件封闭集期望镜像并更新注释；产品源码、UI、schema 及其余测试零变化。原产品与 UI 验证仍绑定相同子树，新增测试增量另完成独立红绿验证。
- `bd56722…` → `d161e06…` 的非文档增量仅为 `src/tianshu/ipc.py` 和新增正式 `tests/test_ipc_fd_lifecycle.py`；schemas、UI 及工作流实现未变。本次完整应用两树差异后，独立索引 `git write-tree` 与冻结 tree 精确一致，已跟踪工作区对索引零差异。
- 后续纯文档回填不自动纳入此绑定，需另核对增量确为纯文档且非文档代码树未变。

## 隔离与真实性

独立工作树为 `/Users/ronfee/.codex/worktrees/compact-plan-review/天枢Agent`，分支 `codex/compact-plan-review`。通过原生 `create_worktree` 创建，在本树 `git apply --index` 应用候选。未在作者工作树测试或写入；未改产品代码，未提交、push、开 PR 或合并。

独立探针未跟踪、不进入候选索引：

- `tests/test__compact_plan_review_probe.py`
- `tests/test__ipc_fd_review_probe.py`
- `ui/src/__tests__/compact-plan-review.test.ts`
- `ui/src/__tests__/compact-plan-mode-review.test.tsx`

最后一个文件从候选 UI 测试复制后添加独立实际 RPC 场景，其中原有用例不冒称独立设计。临时探针、负控 runner 和日志未提交，保留在独立工作树；持续回归由正式 tests 承担。日志路径为独立工作树的 `.omc/artifacts/`，不是正式报告的 docs 目录。Python 使用 `uv run --frozen pytest` 和真实 `tests/conftest.py` 的临时授权根、维护锁根、注册表 Home、沙箱复位及子进程 startup 隔离。数据库、blob、工作区均在 `tmp_path` 或专用临时目录；不读生产凭据、不调用付费模型。

模型采用 ScriptedModel 或两家真实 adapter + FakeTransport；并发采用真 spawn 子进程与 Barrier；硬退出采用 `TIANSHU_FAILPOINT` → `os._exit(77)`。UI 异步用有界条件轮询，Demo 定时行为用 fake timers。

## 已修

### P1：合法撤销使旧 compact 来源失效，并破坏全局 state

首轮 `c6193a…` 红。真实链路为 execute 运行 `create_report` 成功 → compact 成功 → 原 Run `undo.apply` 成功。撤销向原 Run 合法追加 `undo.completed`，旧实现按来源最后事件的 seq 判断来源是否早于工作流，将合法追加误判为来源损坏。

独立探针实际捕获到压缩回放失败，且全局 state 返回 `('not_found', '工作流来源不存在、跨任务或不是已结束的历史')`，并非仅审计假红。

修复先读取并核验完整来源链，再以工作流创建前的历史前缀核验冻结材料。同一真实撤销探针在 `23d2920…` 与最终树均绿，最终日志为 `after_undo_replay_problems: []`、`after_undo_state_error: None`。

测试：`test_later_legitimate_undo_must_not_invalidate_saved_compact_source`。红证据见 `compact-plan-review-baseline.log`；绿证据见 `compact-plan-review-final-python.log`。

### P1：计划模式经过 compact 后，普通补充实际按 effects 执行

独立真实 App 探针在 `23d2920…` 红：已有成功 plan → 仅发送 `/compact` → 发送普通补充；用户未确认计划、未主动选择执行。实际 `run.start` 参数为 `mode: 'execute'`、`execution_policy: { format_version: 1, mode: 'effects' }`。

根因为 `App.selectRun()` 将 compact 视为 execute 并改写任务模式；问题影响实际请求，不只是 selector 显示。最终树使 compact 不覆盖任务模式，无内存模式时按最近非 compact Run 恢复。同一独立探针红转绿，实际请求 mode 为 plan；候选正式 App 重开页面场景也由第二读者执行通过。

测试：`compact-plan-mode-review.test.tsx` 中“已在计划模式时，压缩完成不应自动切为执行模式”。红日志为 `compact-plan-review-ui-mode-red.log`，绿日志为 `compact-plan-review-final-ui.log`。

### 阻断 P2：无效计划先成功，确认时才拒绝

边界由作者先提出；第二读者另写探针实际复现，不冒称发现来源完全独立。首轮树下，end_turn 同时夹工具调用，或正文为 131073 字节，均先 succeeded，确认入口才拒绝。

修复使用新响应与崩溃后已提交响应共用的可消费性校验。同一参数化探针在 `23d2920…` 与最终树均绿，均在完成阶段明确 failed，不承诺不可确认的成功计划。

测试：`test_successful_plan_must_have_confirmable_final_text`。红日志为 `compact-plan-review-baseline.log`，绿日志为 `compact-plan-review-final-python.log`。

### 计划启动失败重试保留模式

由作者发现并加入真实 App 正式红测。第二读者在修复树独立执行，核对首次计划启动失败后，详情重试仍发送同一 mode plan 与 request_id。证据见最终 UI 日志。未将作者发现冒称独立发现。

### Demo 明确能力边界

首轮真实 DemoHostClient 忽略 plan，曾产生模拟工具事件并最终 applied。这是 `?demo=1` 内存模拟的错误语义，非真实 Daemon 授权漏洞，首轮未作为阻断。最终树对 plan、compact、plan.execute 明确拒绝并提示连接真实守护进程。独立探针核对拒绝后 Demo Run 集合不变，见最终 UI 日志。

### 事件封闭集镜像补齐 run.workflow

作者全量门禁发现已有 G2 契约测试保留旧事件类型期望值。第二读者在 `5b218a…` 独立运行同一契约域，实际复现 `46 != 45`；随后应用最小测试增量，核对仅将期望值匹配已登记的 run.workflow 并更新注释，未删除或放宽断言。

逐项 payload、CURRENT_SCHEMAS、LEGACY_SCHEMAS、type enum 及 schema 序列化逐字一致断言原样保留。第二读者在 `bd56722…` 用同一命令红转绿：

```bash
uv run --frozen pytest -o addopts='' -q \
  tests/test_g2_delegation_contract.py tests/test_event_schema.py
```

独立红日志为 `compact-plan-review-schema-mirror-red.log`，独立绿日志为 `compact-plan-review-schema-mirror-green.log`，退出码分别为 1、0。作者全量结果另由作者门禁负责，不将此定向结果冒充全量。

### P1：旧 IPC reader 在 abort 后消费新认证连接的帧头

状态：`bd56722…` 红，`d161e06…` 已修。作者原完整 pytest 在 Suite 2 的 resumed.connect 中出现 answer_challenge 等待 WELCOME 时 EOF；同用例一次重跑绿不计修复或全量通过。

第二读者用真实 spawn 客户端、认证 Listener、_ClientConnection.drop/ServerConnection.abort 与调度屏障复现：旧 reader 在 poll 后取到 fd，尚未执行单次 read；abort 关闭 fd；新 accept 自然复用同一 fd；放行旧 read 后实际读走新 digest 帧头 `00000028`，服务端报 bad message length，新客户端在相同 stdlib answer_challenge 位置 EOF → ConnectionClosed。

正控仅改变调度为新 accept 前完成旧 read，同样自然复用 fd 后新连接认证及 hello 成功。统一断言新连接应成功的探针在原树真实 exit 1。临时探针为 `tests/test__ipc_fd_review_probe.py`，红证据为 `compact-plan-review-ipc-fd-red.log`。

修复把 poll 后的关闭检查、取 fd 与单次 OS read 放进 reader 现有状态锁，与 close 串行；不持锁等 poll，也未改整帧 deadline。若 read 已进入，close 在其单次 syscall 后才释放 fd；若 close 先完成，后续旧 read 被拒绝。独立执行原探针及正式生命周期测试，均以新连接认证与 hello 成功、无非空旧读取、无 accept 错误及线程结束作为产品断言，exit 0，日志为 `compact-plan-review-ipc-fd-green.log`。

探针使用 `_state_lock.locked()` 仅选择与真实实现匹配的调度顺序，不据该内部状态判定通过。**负控**仅在独立 pytest 进程将 `_read_socket` 替换为 `bd56722…` 原方法；同一独立探针及正式测试均在新连接必须成功断言处重新变红、进程真实 exit 1，正控仍通过。负控再次实读到 `00000028`、自然复用相同 fd，并复现 answer_challenge:962 EOF → ConnectionClosed，见 `compact-plan-review-ipc-fd-negative.log`。独立产品文件及候选索引未被负控修改。

临时探针曾将自己的 EOF `b""` 记录为 `[""]` 后过严判错；这是探针错误，现改为只拒绝非空读取，并保留新连接成功等外部断言，不登记为产品缺陷。

独立 IPC/daemon/SLO、半帧期限、慢订阅者与状态压力域回归 exit 0。半帧头、扩展头与正文仍能按期限返回并续读，原发送中断、关闭唤醒、daemon shutdown 与真实 SLO/soak 子进程场景通过；可选 `tests/test_soak.py` 的长时浸泡按默认环境跳过，未冒称执行。IPC 修复未改 Windows pipe 分支；本次为本机 macOS arm64 / CPython 3.12 证据，不外推 Windows 实测。

该机制不依赖创建工作流或运行 AgentLoop；修复前相关 IPC/daemon blob 与契约基线相同，不能据此排除本轮通用路径的调度变化影响触发概率。原全量现场的 Suite 2 目录和子进程 stderr 被 finally 清理，故不能证明那一次 EOF 确由此 reader 截字节造成，也不将其归为既有 issue 或负载。此原始现场归因边界在修复后继续保留。

## 部分修

无已复现项目。

## 未修

无已复现项目。

## 授权、持久化与恢复探针

最终 Python 联合命令执行并通过以下证据：

- 两家真实 adapter 参数化同套恶意副作用响应，plan/compact 均无 tool.intent，原文件字节不变，且 FakeTransport 确实被调用。
- plan 实际执行 read_text 调查并产计划；明确确认后另建 effects Run。新 Run 覆盖已有文件仍进入真实 waiting_approval，文件不变、无 tool.intent、存在真实待审批记录。
- plan/compact 与 workflow-after-call/workflow-after-result 交叉：真子进程 exit 77 后恢复不调用替代模型，外部调用标记仅一次，usage 完整前后一致；unknown 安全失败、已提交结果消费成功，重复 resume 拒绝终态。
- 压缩续接确实使用保存摘要，任务投影重建后可离线回放，原始来源事件逐字不变。
- 错误修订确认不留事件；enqueue 故障使 Run、workflow、active_run_id、队列原子回滚。
- 候选并发确认采用真实 spawn 子进程与 Barrier，独立重跑验证只入队一次。
- **负控**：仅在独立测试 monkeypatch 中把 compact 当 plan，使其真实执行不该执行的 read_text；零 tool.result 断言如预期变红，外层捕获 AssertionError。证明探针可检出模式门失效；未改产品文件，夹具结束自动恢复。

首轮探针开发曾误把 capability_profile 当方法、误用不存在的 read_all；修正真实 API 后才计入证据。独立 UI 探针的 rpc 属性可变性也仅在探针内修正。这些是探针错误，不登记为产品缺陷。

## 实际验证命令与日志

以下工作流命令在 `5b218a…` 已独立执行、读回并确认 **exit 0**；其产品源码、UI 与 schema 子树和工作流冻结 `bd56722…` 完全一致，新增测试镜像的独立红绿见前节。后续 `d161e06…` 仅增加 IPC 修复及其正式回归，追加验证列于后文；未把旧产品子树称为与新 IPC 子树相同。测试数量以实际命令日志为准，本文不维护静态总条数。

```bash
uv run --frozen pytest -o addopts='' -q -s \
  tests/test__compact_plan_review_probe.py \
  tests/test_workflow.py tests/test_workflow_cli.py tests/test_event_schema.py
pnpm --dir ui test
pnpm --dir ui run typecheck
pnpm --dir ui run build
uv lock --check
uv run --frozen ruff check .
git diff --exit-code -- ui/dist
```

对应日志位于独立工作树 `.omc/artifacts/`：`compact-plan-review-final-python.log`、`compact-plan-review-final-ui.log`、`compact-plan-review-final-typecheck.log`、`compact-plan-review-final-build.log`、`compact-plan-review-final-lock.log`、`compact-plan-review-final-ruff.log`。最终构建产物与冻结索引一致，没有 ui/dist 漂移。

另在首轮树完成以下受影响后端域回归，exit 0：

```bash
uv run --frozen pytest -o addopts='' -q \
  tests/test_tasks.py tests/test_control.py \
  tests/test_recovery.py tests/test_recovery_atomicity.py \
  tests/test_summary_context.py tests/test_summary_loop.py tests/test_summary_replay.py \
  tests/test_budget_ledger.py tests/test_budget_review.py \
  tests/test_low_disk_execution.py tests/test_low_disk_run_creation.py tests/test_events.py
```

最终后端修复通过 workflow/CLI/schema 与独立故障探针重跑覆盖；最终 UI 增量无后端变化，未把首轮域回归伪称最终树本机全量。第二读者未执行本机全量 pytest、远端 required CI；本机证据仅指 macOS arm64 / CPython 3.12 开发环境，不宣称 Windows、其他架构或用户桌面设备实测。

IPC 新冻结树 `d161e06…` 的独立追加命令：

```bash
uv run --frozen pytest -o addopts='' -q -s \
  tests/test__ipc_fd_review_probe.py tests/test_ipc_fd_lifecycle.py
uv run --frozen python .omc/artifacts/review-ipc-old-reader-negative.py
uv run --frozen pytest -o addopts='' -q -rs \
  tests/test_ipc.py tests/test_ipc_send_close.py tests/test_round2_inputs_ipc.py \
  tests/test_daemon.py tests/test_slow_subscriber.py tests/test_slo_and_soak.py \
  tests/test_slo_state_pressure.py tests/test_soak.py
uv lock --check
uv run --frozen ruff check .
git diff --cached --check
git diff --exit-code
git write-tree
```

负控命令预期且实际 exit 1，其余命令均 exit 0；`git write-tree` 为 `d161e060ceefe240a930bc5cbef8d775ca8eb4b6`。日志分别为 `compact-plan-review-ipc-fd-green.log`、`compact-plan-review-ipc-fd-negative.log`、`compact-plan-review-ipc-domain-green.log`、`compact-plan-review-ipc-lock.log`、`compact-plan-review-ipc-ruff.log`、`compact-plan-review-ipc-diff-check.log`，均保留在独立树 `.omc/artifacts/`。
