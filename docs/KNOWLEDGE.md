# 天枢 Agent · 项目知识库

> **AI 开发必读必更。** 每次开发前读本文，结束后更新本文。强制指令见根目录 [AGENTS.md](../AGENTS.md)。
> 本文是「活文档」：蒸馏决策、架构要点、踩坑教训与当前状态，供任何一次开发会话快速上手。
> **权威规格仍以 [PRD-v1.md](PRD-v1.md) 为准**——本文与 PRD 冲突时以 PRD 为准，并应顺手修正本文。

---

## 0. 这份文档怎么用（给 AI）

- **开发前**：读 §1–§7，尤其 §5 踩坑教训与 §6 流程规则；改契约前先读 PRD §5/§6。
- **开发后**：
  - 状态变了 → 更新 §2 / §7；
  - 新决策 / 新教训 → 追加 §4 / §5；
  - **在 §8 会话日志追加一条**（日期 + 做了什么 + 发现什么），这是硬性要求。
- **对账**：本文任何「已实现什么 / 多少测试 / 覆盖哪些平台」的陈述必须与当前代码和 CI 一致，不留假话。数字会过期，更新时重新核实。

## 1. 项目是什么

- **目标**：面向普通用户的桌面 Agent OS——模型自选、在用户指定目录读写、完成长任务；把「AI 可能失控」当一等工程问题。
- **当前形态**：**M0–M5 内核已完成，另有未签名 L1 桌面内测壳候选**。M0–M4 是命令行内核；M5 交付三进程桌面闭环（Daemon + Python Host + React UI）。当前分支新增 Tauri 2 薄壳、托盘、单实例、锁定 CPython 3.12/UI bundle、manifest 字节校验、macOS 安装脚本与四目标 release workflow。macOS arm64 已取得本机安装态证据；这不是 G3 正式发布，Rust Host、OS 签名、自动更新、四目标用户设备安装/升级/回退、恢复 B/C、SLO 与长稳仍未完成。
- **核心承诺**：事件可重放、执行侧授权、覆盖/删除需审批、默认可撤销、崩溃可恢复。
- **非目标（v1）**：图片/视频生成、多租户、防本机用户篡改（只防模型越权与失控 worker）。
- **技术栈**：Python 3.12（`requires-python = ">=3.12,<3.13"`）、`uv`、**零第三方运行时依赖**（`dependencies = []`）；dev 依赖只有 `pytest>=8.0` 与 `ruff>=0.6`。UI 是 Vite + React + TS，但只有**开发/构建**时需要 Node，运行时加载已提交的 `ui/dist`。
- **版本 / 许可**：`0.1.0`；Apache License 2.0（`LICENSE` 已被 git 跟踪）。

## 2. 当前状态（数字会过期，更新时重核）

- **当前远端 main 为 `e242257`（2026-11-16）**：#140 文档对账经正常保护合并；
  该 main 在 `702ba40` 之后只改文档、`.gitignore` 与 `AGENTS.md`。发布分支已普通
  merge 到该 main。独立第二读者对 `3863887/tree 7d6de1b` 的结论是 **`UNFIXED`，
  不可合并**：运行期 Host 崩溃不会自动恢复，且崩溃后显式退出会在 owned/attached
  两条路径遗留 Daemon、`host.lock`、`daemon.lock` 与 `agent.sock`。`6dfa605` 对
  owned/attached Host 都加入最多 3 次有界退避恢复；Python 退出辅助入口只清理已退出
  且 PID/`started_at` 匹配的 Host，并只在 Daemon 锁 SHA 未变化时停止 Daemon。
  `df1aacd` 再修 supervisor 发现新 endpoint 时可能丢掉仍存活 owned `Child` 句柄的
  竞态：只有 `try_wait()` 确认旧 child 已退出才允许接管。该竞态回归在旧实现稳定红、
  修复后绿。当前 Cargo 为 **32 tests**（12 lib + 11 lifecycle + 9 release），相关
  `tests/test_host_cleanup.py` 为 **27 passed / 1 skipped**（skip 仅 Windows 专用
  句柄测试）。

  `aaaffb4/tree b5d03d6` 的精确 macos-arm64 作者态产物
  （archive SHA256 `4b419aa85cd9745d63fe2def02c91a4da03fcb17a01a3948e34a2349af440562`，
  manifest `b18bbcadd205deda2f8a0375b2c7b27a5ce1145dfe8eae1e3c4ed3714de93a75`）
  已通过 verify/runtime smoke、原生 `--verify-bundle` 原始/listed-tamper/
  unlisted-hook 负控、新 source-bundle 未列 hook 负控、全新安装（包内
  `.pyc/__pycache__=0`）、两路真实 owned/attached Host SIGKILL 恢复及退出清理、
  普通 GUI 生命周期、attach→quit 和 takeover safe/race 探针。两路崩溃探针都确认
  新 Host PID 与锁一致、`started_at` 变化、`/health` 与 `/session` 为 200、
  Daemon 身份保持、AX 恢复“已连接”；退出后 desktop/当前 Host/Daemon 全退，
  三个运行锁/socket 消失，`execution.guard` 与 `ipc.key` 保留。`eaa9da0` 补上的
  Python source bundle 完整集合校验让攻击副本精确报未列
  `app/sitecustomize.py` 并 exit 1。该候选本机 lock/Ruff/diff、完整 Python
  **3549P/12S（678.43s，exit 0）**、UI 124 tests、Rust 32 tests、fmt/clippy、
  actionlint 与 shellcheck 全部通过；第一次全量日志也为 3549P/12S，外层 zsh 写
  `status` 只读变量导致的包装器错误已保原件并用 `gate_rc` 重跑。
  上述仍是 **作者侧候选证据，不是最终 PASS**：docs-only 最终 SHA、该 SHA 的六项云
  CI、最终产物/安装/GUI 复验和全新第二读者独立 PASS 都还没有绑定，PR 不得合并。
  旧 `5873720` 的 PartIAL、`7b4cc7a` 的 PARTIAL 与 `3863887` 的 UNFIXED 均只保留为
  历史复审事实，不得被新修复冒领。
- **PR #155 前端已完整接入；真实冷启动白屏由 Tauri 导航时序修复（2026-11-15）**：
  `feat/budget-subtask-ui` 的合并提交 `d37a84c` 已是当前分支祖先，当前 `ui/` 树与该
  分支逐字节一致；PR run `43676855243` 的 Ubuntu/macOS/Windows 两分片/Windows
  汇总/UI 六项全绿，当前 UI 为 16 文件 **124 tests**，typecheck、build 与
  `ui/dist` 对账均通过。因此问题不在“前端分支未接入”，而在 `aa6b65f`/`820a7cd`
  的真实 Release WebView：Host、Daemon、`host.lock` 和 `/health=200` 都正常，自有
  Host 冷启动仍稳定白屏。Tauri `setup` 在事件循环启动前执行，旧代码同步等待 Host
  后直接 `navigate`，该消息没有可处理的运行时事件循环。`58d0bdb` 把成功导航与失败页
  `eval` 都排到 `run_on_main_thread`，其余 Host/安全/生命周期逻辑不变。真实安装态
  macos-arm64 冷启动已显示“已连接”；HTTP 提供的 JS/CSS 与安装资源字节一致，
  `index.html` 仅按设计注入 token。该提交的归档 SHA256 为
  `4870e473c23d05e7d2b996a7837140794c31cdd4ba88425fe2025c4ac55cd24f`、
  manifest SHA256 为
  `37aa9fd8db453b5e1b7981f45b5d8affbcc14558db5334a274bccdcb5d026c81`；
  runtime smoke 通过，篡改 `app/tianshu/host.py` 被精确拒绝；真实进程链
  `49833 → 49838 → 49840` 在关窗、重开后保持不变，`Cmd+Q` 后三层全退且
  `host.lock`/`daemon.lock`/`agent.sock` 消失。旧 `820a7cd` 的仅窗口/进程生命周期
  结论不能替代页面内容证据；最终 docs-only SHA 仍须重建产物、跑精确 CI 并取得独立
  第二读者 PASS。
- **PR #162 Windows 原子替换瞬时占用已做有界修复（2026-11-15，代码 `bb0ce76`）**：
  `b46f015` 的精确 PR run `43708266691` 中 Ubuntu、macOS、Windows shard 0 和 UI
  成功；Windows shard 1 唯一失败为 `test_concurrent_approval_consumes_once`。
  审批并发控制本身只产生一个执行者，另一派发已以 `state=executing` 被拒；获胜的
  `modify_text` 在 Windows 把 `.note.md.*.tmp` 原子替换为 `note.md` 时收到
  `WinError 5 Access is denied`，因此效果按既有保守口径记为 `unknown`。读取句柄在
  `write_bytes_atomic()` 前已经关闭，派发事务使用 `BEGIN IMMEDIATE` 串行化，且该
  测试与 `fsutil` 替换路径都不是 release-app 本次新增；剩余根因边界是 Windows
  短暂占用目标文件，项目已有 `metrics.py` 记录同一 `os.replace` 句柄占用语义。
  `bb0ce76` 仅对 `PermissionError.winerror in {5, 32, 33}` 做 8 次、总退避约
  1.27 秒的重试，其他权限错误立即抛出，持续占用在界满后保留原异常并附诊断 note，
  临时文件仍由 finally 清理。新增测试先证明旧实现遇到两次瞬时 `WinError 5` 直接失败，
  修复后第三次成功；持续占用测试证明 8 次后仍失败、目标字节不变且无 tmp 残留。
  目标 24 项与崩溃一致性测试通过；清理本机 `ANTHROPIC_BASE_URL` 中转污染后完整
  Python 套件为 **3516 passed / 12 skipped / 640.36s**。当前作者对象 `5e89de5`
  （tree `d41ab57`）的 PR run `43714752198` 六 job 全绿：Ubuntu 3496P32S、
  macOS 3516P12S、Windows 1726P38S + 1746P18S（汇总 3472P56S / 3528 节点）、UI
  全步骤成功；精确 macos-arm64 产物、冷启动可见 UI、关窗/重开/显式退出也通过。
  若之后还有 docs-only 提交，新 head 的 CI/产物必须另行绑定；独立复审仍另绑。
- **W5 调度器已交付并入 main `c637b52`（2026-11-15，PR #167）**：基线
  main@0c5bb71；本机门禁 lock/ruff 0、全量 3408P/11S（686.57s）= 基线
  3395P/11S + 13 条新调度器测试；负控 NC1/NC2 实跑红→还原留证。v1 边界如实：
  interval 调度、只走 CLI、无 cron/无 RPC/无 UI/零新事件族（C3 评估见
  CONTRACT-W5-SCHEDULER-2026-11-15.md）。PR 项自身 CI 曾出现触发异常并留有记录；
  merge commit `c637b52` 进入本发布候选后又由精确 run `43729859260` 覆盖验证。

- **W15 v1 只读保留期审计命令已并入 main `702ba40`（2026-11-15，PR #169）**：`tianshu data retention-audit --data-root` ——逐表行数/最老行年龄/状态直方图、增长投影（观测窗口 ≥1 天才外推，几毫秒跨度会外推出垃圾速率——测试实抓后修）、可清理候选测量（jobs 终态行、budget_reservations settled+known；终态判定只用 tasks/jobs/budget 权威常量在 Python 侧解释，不猜 dispatch 面语义）、cleanup_plan 现状。**零删除**：mode=ro 连接不加锁不阻断写入，测试钉住 events.db 字节哈希前后不变。SQL 全部单行字面量（标识符不进拼接，值一律参数绑定）；33 测试文件级 +4。实现过程记一笔：Mimosa hook 三次拦截动态 SQL（execute 变量/跨行拼接），按其建议改全静态结构后反而更符合封闭集纪律。三项拍板仍留实测数据。

- **第三批 W3/W5/W15 三 PR 已全部并入 main `c637b52`（2026-11-15）**：#164（W15 docs-only）→`9466df0`、#166（W3 state 8MiB 上界）→`c7f31f7`（run 43721830851）、#167（W5 调度器）→`c637b52`（run 43724954970），均六项 CI 全绿；KNOWLEDGE.md 冲突按 difflib entries 并集法 + 行多重集证明解决。相关 worktree 与本地分支已删。**W15 三项拍板定为「先测量后拍板」（2026-11-15 维护者委托）**：先交付 v1 只读保留期审计命令（零删除，CONTRACT-W15 §3.3 划界）再逐项拍板，本片不拍死数值与红线修订。全量套件现级 **3461 passed / 12 skipped**（649.56s，macOS arm64；以 CI 为准）。

- **W5 调度器已并入 main `c637b52`（2026-11-15，PR #167，CI run 43724954970 六项全绿）**：基线 main@0c5bb71；本机门禁 lock/ruff 0、全量 3408P/11S（686.57s）= 基线 3395P/11S + 13 条新调度器测试；负控 NC1/NC2 实跑红→还原留证（attempt1 Windows shard0 红 = 新测试终态谓词真缺陷，已修）。v1 边界如实：interval 调度、只走 CLI、无 cron/无 RPC/无 UI/零新事件族（C3 评估见 CONTRACT-W5-SCHEDULER-2026-11-15.md）。

- **W 系列七 PR 已全部合并，main `c8b53bb`（2026-11-15）**：维护者拍板后按「并入最新 main → 双向 diff/行多重集终验 → 本地门禁 → CI 六项全绿 → 保护合并」逐条收口，全程零强推：#150→`2530a15`（run43661642024）、#151→`595be35`（attempt1 Windows shard1 红 = `test_concurrent_approval_consume` 的 `os.replace` WinError 5 文件锁竞态，与凭据域无关，attempt2 全绿）、#152→`7f4c07d`（run43668070804）、#154→`24eabc4`（attempt1 shard0 红 = `test_data_binding` 在案 flake 家族，attempt2 全绿 run43670453171）、#155→`d37a84c`（run43676855243）、#156→`134e9f0`（run43679118848）、#157→`c8b53bb`（run43681412578）。冲突三次 modify-modify（#155/#156 的 §7 G2 行、#157 的 §7 F10 L2/L3 行）+ 两次 §8 同锚追加（#152/#154）+ 两次自动合并（#150/#151）：modify-modify 用 difflib 提取双方相对祖先的插入段机械求并集（位置互不重叠才可行），G2 行终版 = 956 base + 415 main + 61 W10 = 1432 字符。合并轮教训：**冲突解决器「theirs-then-ours」只对纯追加冲突合法——modify-modify 按追加处理会整行重复**（#155 实犯，行多重集核查抓出后修复）；**同一 §7 状态行会被多个在途 PR 各自销账**（G2 行被 #152/#155/#156 三方叠改、F10 行被 #157 叠改）；**本机两个会话的全量套件并行 + 遗留临时目录把临时卷打到 98% 满，DiskAdmission 空间门槛让 110 个测试拒绝启动 Run（"缺少完整 Run storage context"）**——清理遗留临时目录并错开对方会话后复跑全绿；**后台 pytest 管道 `| tail` 吞 summary 且管道退出码取 tail 的**——用文件重定向 + 显式退出码。各 merge tip 本机全量门禁：3329P/8S → 3334P/11S → 3347P/11S → 3369P/11S → 3369P/11S → 3382P/11S → 3395P/11S（711.25s）。收口 PR #163（纯文档 +3 行）attempt1 ubuntu 红 = `test_send_phase_failure_is_never_retried` 首现 CI 红点（#152 net 韧性测试域）；恢复 T1 后续候选再次复现 `DID NOT RAISE NetError`，已定位为 `fetch()` 未核对 `Content-Length` 的真实完整性缺陷并修复，现不以 runner timing flake 结案。**W3/W5/W15 已由 #146/#159 落地解锁；#141 已并入 main `702ba40`，#140 已由 `e242257` 正常保护合并**；本轮七 PR 与两份文档对账在 KNOWLEDGE/PRD 相交，合并按同法处理。
- **PR #162 真实 GUI 发现 `host.lock` 发布竞态，代码 `719de9a` 已最小修复（作者侧证据，最终产物复审待回填）**：
  `0f8649c` 的安装态 App 再次启动时，Host 与 Daemon 已实际启动且 `/health` 可用，
  Tauri 壳却偶发显示“Host 元数据不合法”并进入失败页。根因是 Python Host 用
  `Path.write_bytes()` 发布 `host.lock`：外部桌面壳不持有 Python 侧元数据互斥，
  可能在旧文件已截断、新 JSON 尚未写完时读到半截内容。确定性探针用 spawn 子进程
  发布、父进程绕过 Python 互斥直接读；原实现截断到 `{"pid` 后稳定得到
  `JSONDecodeError`。`719de9a` 改用同目录临时文件、fsync、`os.replace` 与目录
  fsync 原子发布，外部读者只能看到完整旧锁或完整新锁，回归为
  `tests/test_host.py::test_host_metadata_publication_is_atomic_for_external_readers`
  旧实现 exit 1、修复后 exit 0；main `61f985e` 合并后的 Host 聚合套件为
  **76 passed / 1 skipped**。该证据不等于最终新 head 的真实产物已经复验。
  `0f8649c` 的受控 loopback 模型 GUI 链还分别证明了
  拒绝审批不执行、批准后文件与 Receipt 闭合、等待审批时取消不执行；关窗后三层进程
  继续、重开连接同一 Host、`Cmd+Q` 后 Host/Daemon/壳退出且无 socket/锁残留。上述 GUI
  事实是旧 head 证据，最终 App 必须重建后重跑。
- **PR #162 同步 main `61f985e` 后的独占本机门（2026-11-15，作者侧候选）**：
  `ca8f32d` 合并最新 main，`719de9a` 修 `host.lock` 原子发布，`8058926` 把回归改为
  spawn 子进程发布、父进程直接读。最终代码树在确认全用户 `restore-gate` 空闲后独占运行
  lock、全仓 Ruff 与完整 pytest，结果为 **3514 passed / 12 skipped / 704.08s**，
  lock/Ruff 均 0；Host 聚合 **76 passed / 1 skipped**，UI 16 文件 **124 tests**，
  typecheck/build/`ui/dist` 一致，Rust **24 tests**、fmt、Clippy `-D warnings`、
  actionlint、shellcheck 与 `git diff --check` 均通过。先前两次全量与另一在途会话的完整
  pytest 重叠，被用户级全局 `restore-gate` 分别打断为
  `maintenance_busy`；失败日志保留在 ignored `.omc/artifacts/`，不能算产品红，也不能用
  后来绿色覆盖竞争事实。代码对象 `1b0c99d` 的后续云 CI run `43699772460` 六个 job
  全绿；该结论只绑定这个代码对象，后续 docs-only 提交仍须有自己的精确 SHA CI。同一代码
  对象的 macos-arm64 产物 archive SHA256 为
  `3e798305decff899e5d8d074ca26cd461945f44f8924f94d76c2439556963733`，
  manifest SHA256 为
  `1f2e998e3cbf45b3a14c3a8165d69b64acd74b8ad4fc54901fa9023bdce6916b`。
  在该安装产物的真实 Tauri 窗口中，关窗后三层 PID 与 Host 端口保持不变；按完整 App
  路径重开后仍连接同一端口与隔离数据根；`super+q` 退出后三层进程全部消失、
  `host.lock`/`daemon.lock`/`agent.sock` 均删除。`execution.guard` 与 `ipc.key` 是持久
  文件，不能误记成锁残留。测试环境同时注册了同 bundle id 的旧 debug App，按显示名绑定
  会被 LaunchServices 启动到旧 App；最终 GUI 证据改为按完整正式 App 路径绑定，这只作为
  测试隔离边界，不冒充生产缺陷。最终 docs-only HEAD
  `aa6b65f0d2e389180e9b6990ad02796b50acafe0`（tree
  `b28d084d95f30044641eea2b85f1f923419c1086`）已由 PR run
  `43701648845` 单独验证：Ubuntu **3494P32S（482.62s）**、macOS
  **3514P12S（902.75s）**、Windows 两片合计 **3470P56S**（3526 节点，汇总器核验完整
  覆盖）、UI 全步骤成功；六 job 全绿。该 SHA 的 macos-arm64 archive SHA256 为
  `efc39081a544c87a98e630494fdbfa366fe225cc630b601d82a980e175d56967`，manifest
  SHA256 为 `fafb51b6226c2b86da06935cd02f1913a18e4bab8fc56f1220e9ece872867705`，
  runtime smoke、篡改/未列文件负控、安装/卸载及真实 GUI 生命周期均通过。当前唯一未完成的
  合并前门禁是独立第二读者复审；不能由作者上下文代替。
- **可安装发行版 PR #162 第四轮作者复审修复（2026-11-15，代码 `3c0687d`，独立复审与最终云门待回填）**：
  在 `7707597` 上确认 7 项发布/生命周期缺口并先取得原实现红：release
  dispatch 接受旧 main 祖先；失败 Host 启动遗留新 Daemon；`/shutdown` ACK 后 Host
  在清理 Daemon 前退出会留下孤儿；runtime lock 的 target 可绑定错误 triple；macOS
  verifier 不检查 bundled Python 架构；Host 子进程已退出仍轮询到 30 秒超时；一次快照
  遗漏 Host 被杀后才发布锁的 detached Daemon。修复要求 dispatch 与 fetch 后当前
  `origin/main` 精确相等并先解引用 tag；lock 校验四目标 native triple；归档校验
  `python/bin/python3` 架构；桌面退出所有 owned-Host 路径都完成 Daemon 清理；启动
  失败只清理相对启动前快照新建/变化的锁，并在 5 秒有界窗口捕获延迟发布，既有 Daemon
  负控保持字节不变。真实 Unix 子进程 + 真实 HTTP ACK 探针旧实现红、修后绿；本机统一门为
  Python **3465 passed / 11 skipped（721.19s）**、UI 16 文件 **124 tests**、Rust
  **18 tests**、lock/Ruff/typecheck/build/dist/Cargo fmt/clippy/actionlint/shellcheck
  全 0。Windows 真子进程生命周期测试仍需原生 runner 执行；本会话被明确禁止启动/调用
  子代理，因此这条只记作者侧修复，不冒充 required 独立复审 PASS，也不宣称 G3、四目标
  用户设备安装或 main release workflow 已完成。
- **可安装发行版 PR #162 第三轮 Windows MCP setup 红已定位并修（2026-11-15，最终云端门禁待回填）**：
  rebase 后 run `43687249112` 的 Ubuntu、macOS、Windows 分片 1 与 UI 成功；Windows
  分片 0 唯一失败为 `test_stalled_send_is_unknown_and_replay_does_not_retry`：测试先用
  固定 5 秒等待控制 socket，早于同一配置允许的 10 秒 MCP 握手预算，超时后又用清理断言
  掩盖 worker 原始结果。`c4627c3` 先等待真实 `call_tool` 入口，再与已建立的控制连接
  rendezvous；setup watchdog 从配置的握手预算派生，产品 200ms transport 期限、真实子
  进程、unknown/无 Receipt/两次 replay 断言均不变。确定性对照对旧/新测试注入同一 6 秒
  `build_stack` 延迟：旧版 `TimeoutError`，新版通过；MCP 相关测试 **202 passed**，全量
  **3461 passed / 11 skipped（639.23s）**。最终 head 云端 required 与独立复审仍未完成。
- **可安装发行版 PR #162 第二轮 Windows 红已定位并修（2026-11-15，最终云端门禁待回填）**：
  首轮 Ubuntu 修复后的 run `43684083160` 中 Ubuntu、macOS、Windows 分片 1 与 UI 成功；
  Windows 分片 0 唯一失败为网络期限 Timer 先触发 `shutdown()`、后续时钟检查尚未看到
  到期时把中断错分为 `net.read`（期望 `net.timeout`）。原 job
  `132720749135` 与 `test_network_deadline_interrupts_slow_progress_before_peer_finishes[body]`
  原始栈保留。新增真实 HTTP + 受控 Timer 回归在旧实现上确定性得到 `net.read`，修后只在
  Timer 实际中断路径把 `OSError` / `HTTPException` 归为 `net.timeout`，真实读错、协议错误
  和取消分类不变；网络六文件 **84 passed**。分支已普通 rebase 到最新 main
  `c8b53bb2e00c7be20e980fec3994019c64674420`，该组合 lock/全仓 ruff exit 0、完整
  **3461 passed / 11 skipped（668.23s）**。当前 head 的原生四项 required、独立复审及
  main 四目标 release 仍未完成；首轮 `d26ad98`、第二轮旧 head `06fb772` 与 rebase 后新
  head 是三个独立证据对象，不得互相冒充。
- **可安装发行版 PR #162 首轮原生 CI 红已定位并修（2026-11-15，当前 head 门禁待回填）**：
  `d26ad98` 的 PR run `43681913168` 中 macOS、两个 Windows 分片与 UI 均成功；Ubuntu
  唯一失败为新增测试 `test_cross_os_target_is_rejected` 在 Linux 上先调用只支持
  macOS/Windows 的 `native_target()`，尚未进入被测的跨 OS 拒绝分支。原日志保留为真负控；
  `9a79b82` 只把该测试改为确定性模拟 `macos-arm64` 宿主，不增加 skip、不放宽产品校验。
  修复文件定向 **9 passed**；隔离模型/私有端点、failpoint 与 opt-in 开关后的本机完整门为
  lock/全仓 ruff exit 0、**3447 passed / 11 skipped（693.91s）**。当前 PR head 的四个
  required checks、最终独立复审和精确 main 四目标 release workflow 仍待结果，不能把
  `d26ad98` 首轮五项结果或本机全量冒充最终提交通过。
- **可安装发行版 L1 加固候选（2026-11-15，最终门禁与复审待回填）**：从 main
  `595be35c037cef35da7f166a5db2a624f2b733a9` 新建 `codex/release-app`，现以普通 rebase
  接到最新 main `134e9f04605547e58523ed7184200eccd9e9fea9`。历史作者提交 `def12d27`
  按完整文件集合校验
  bundle/归档，拒绝未列 `sitecustomize.py`、已列文件篡改、链接逃逸、错误 Windows
  Python 架构和未绑定源构建；release 编译禁用资源 override；Host 附着核对
  `/health` PID 与 token 保护 `/session`，退出直接调用 `POST /shutdown`，Host 崩溃时通过
  同包 Python 收尾 Daemon。macOS 安装器在替换旧 app 前调用原生 `--verify-bundle`，
  purge 要求逐字匹配 `--confirm-data-root`。macOS arm64 真实产物绑定上述 SHA，SHA256
  `634dc6ed00d505f579ef6cabb5f7bd155d3a1652454b490279a32f500e1c9497`；在隔离前缀/数据根
  完成 WebView、真实 `/health`/`/session`、单实例、脚本化任务执行、关窗保活、LaunchServices
  重开、Cmd+Q、Host `SIGKILL` 后退出无孤儿、陈旧锁重启、重装回退、篡改失败页、卸载保留与
  purge 二次确认。作者全门为本机 `3389 passed / 11 skipped / 589.67s`，UI 13 文件
  91 tests/type/build/dist，Cargo 19 tests/fmt/clippy，lock/ruff/shellcheck/actionlint 全 0。
  独立复审确认上述指定项已修，但把该提交判为部分修：短 `release.json` 未严格解析、归档
  manifest 未字节绑定、四目标仍复用非匹配架构 runner、Windows 只验外层 PE。rebase 后
  代码提交 `cc05c3e` 已补封闭身份、bundle/归档/NSIS
  payload 三向字节绑定、Windows 7-Zip 实解唯一资源根、原生 smoke 强制和
  `macos-26-intel` / `windows-11-arm` runner；release 定向 64 passed，lock/ruff/actionlint/
  shellcheck 通过。这是新代码候选，尚不能继承 `def12d27` 的最终产物或全门结论。
  这不是 G3：最新 SHA 的完整本机门、真实产物、四目标 workflow、独立复审/CI，
  macOS x64/Windows 用户设备、签名/公证、自动更新、真实设备升级/SLO 仍未完成。
- **Windows 计时/锁等待诊断 PR #153 已交付，main 自身首次 CI 独立通过（2026-11-14）**：PR head `da47a3b6dbc38f0e0634af101c7089bf511c48e3` 的首次 run43586138334 attempt1 六 job 成功；独立结论 `PASS_PR153_ATTEMPT1_NATIVE_WITH_RETAINED_WATCHDOG_EVIDENCE`。正常保护 merge 为 main `88ec0ab640302f81602db93e69502f5ba5d84cb5`（tree `b9f05920d21b39c072633fd44299e4786da59f51`，parents `c2cf…` / `da47…`）。main 自身首次 push run43588389290 attempt1 六 job 成功，独立结论 `PASS_MAIN_88EC_ATTEMPT1_NATIVE_WITH_RETAINED_TIMING_EVIDENCE`：两片完整覆盖 3207 节点、9588 阶段，**3170 passed / 37 skipped**；Ubuntu 3194/13、macOS 3199/8、UI 13 文件/91 测试。PR 和 main 各保留 3 次成功节点的 watchdog 长等待，未把绿色或长等待误写成生产根因已修复。原 c2cf 两红、实际 SQLite 锁机制及原 MCP 卡点仍未知；#145/#147 保持开放。本片只交付测试计时、异常清理和失败现场，恢复 B 产品仍未实施。
- **控制面响应测试已改为确定性屏障（2026-11-15）**：纯文档恢复契约 PR 的新 head 原生 run43634904064 在 Windows 分片 0 唯一红为 `test_control_plane_stays_responsive_while_driver_runs_long_task`，控制面两 RPC 实测 **0.781s，超过原 0.5s 阈值**；shard1、Ubuntu、macOS、UI 四个 job 成功，shard0 与 Windows required 汇总失败。产品 blob 与已通过的 main `88ec` 相同，原测试的“模型 sleep + 墙钟阈值”对 runner 调度敏感；0.781 秒耗时来源仍未定位，不能据此断定产品阻塞，也不能把本轮通过写成性能根因已修。隔离修复把长模型改为真实 Event 阻塞，只有驱动仍锁在模型调用、控制面 RPC 已完成才通过；2 秒 watchdog 只防止测试死锁，触发即失败。旧候选漏了入口失败清理，独立真进程负控发现后已用外层 `finally` 修复；4aae 候选目标连续 20 次通过，整文件与全量均通过（**3199 passed / 8 skipped，660.48s**，lock/ruff 0）。最终 93c1 仅改 RouteModel 文档字符串并复验目标测试，不冒充新一轮 20 次或 full。该结果不建立新 SLO，也不证明原 0.5 秒数值性能目标或 SQLite 根因。
- **控制面屏障修复与恢复 B 文档契约已进入 main `925ca8f2`（2026-11-15）**：PR #158 head `6f40591989901784ac925b2a6639fb5e5ebfc380` 的原生 run43638768307 attempt1 六 job 全绿并经第二读者确认；正常保护 merge 为 main `f307bf16e0c5bb4b9eeac5c9c6e58353fb5411eb`。随后恢复 B 文档契约 PR #146 head `6a5d2130e6162a82d2d0ec27da193ad1033db0ea`（tree `4e5599b777e1879ac294c60ae6021ec308dfbc2a`）以普通 merge 同步到该 main，相对 base 仅 33 份 README/docs/证据改变，产品、测试、schema、UI、CI 与依赖零 delta；本机 lock/full/ruff 全 0，3199 passed / 8 skipped（578.91s）。PR 原生 run43644560048 六 job 全绿：Ubuntu 3194/13、macOS 3199/8、Windows 合 3170/37，两片覆盖 3207 节点与 9588 阶段，原失败控制面测试三阶段通过。保护合并后 main 为 `925ca8f2e94541b5e07c6510e827f6a7381cb2e6`（tree 与 PR head 相同）；main 自身 push run43646899748 六 job 全绿，Windows 合仍为 1590P14S + 1580P23S、3207 节点/9588 阶段，控制面目标通过并保留 1 次 watchdog 后正常 PASS。两次精确对象的最后第二读者签发均因复审服务 upstream 401 未完成，不能伪造独立 PASS；#145/#147 仍开放。
- **恢复 B T0 v2 内部契约实现已并入 main `139653c5`（2026-11-15）**：在 main `925ca8f2` 上接入独立候选的 `restore_contract.py` `f4fb10af…`、`tests/test_restore_contract.py` `7017cd67…`、`schemas/data/restore-v1.json` `0f55065b…`。接入时发现通用 `data/` 忽略规则会把新 schema 静默排除出 Git，已用仅限 `schemas/data/` 的 `.gitignore` 例外修复。本机目标回归 **119 passed**；lock/full/ruff 全 0，**3318 passed / 8 skipped（542.29s）**，原 3199 项保留、新增正好 119 项，前后已跟踪文件不变。PR #159 首跑 run43649748068 的 Windows shard 0 唯一失败，是测试读取中文规范依赖默认编码：`PRD-v1.md` 在 cp1252 下触发 `UnicodeDecodeError`；全部 5 处 `read_text()` 随后显式指定 UTF-8，测试 blob 更新为 `0ba84f9a…`，源码与 schema 保持独立候选字节。修复后 PR run43651290917 六 job 全绿：Ubuntu 3313/13、macOS 3318/8、Windows 1649P15S + 1640P22S，覆盖 3326 节点/9945 阶段，首红测试与控制面测试三阶段均 PASS。正常保护合并后 main 为 `139653c5b92bd9f6c9af181d828ffd035567a79c`（tree `77489c4b…`，与 PR head 相同）；main 自身 push run43652961032 六 job 全绿，Windows 合 3289P37S、3326 节点/9945 阶段，shard0 保留 3 次 watchdog 后 PASS。该片只是严格 parser/validator/schema 内部接口，不接 CLI、不执行复制/删除/review、不改 EventLog；B01–B09、T1–T6、产品恢复仍 `NOT_RUN`。精确新对象的最后第二读者签发仍因 upstream 401 未完成，不伪造 PASS。
- **恢复 B T1 平台基础候选代码与原生门禁通过（2026-11-15）**：从 main `2530a157` 新增原生实体身份、gate/namespace/entity 非阻塞锁、固定 Home registry、`EventLog` 连接期连续锁，并把迁移/离线备份纳入 namespace+entity 独占。独立复审抓到并修复 absent→reserved 准入竞态及迁移 SQLite close 失败提前释放 access；T0+T1 定向收集 **166** 项，macOS 本机为 **165 passed / 1 skipped**（Windows known-folder 用例）。同步 main `0c5bb71` 后，代码对象 `32f003d` 的本机 lock/full/ruff 全 0，**3443P12S（645.06s）**；PR run `43688229712` attempt3 六 job 全绿，Windows 合 **3409P46S / 3455 nodes**，UI 16 文件 124 测试。第二读者在独立导出重放两项反例并覆盖 net/T1 六文件，**104P1S PASS**；Windows `FileIdInfo`/锁/rename/registry artifact 状态 PASS。attempt1 重放开放的 #145 heartbeat `SQLITE_BUSY` flake，保持 `NOT_FIXED`；attempt2 部分重跑的 artifact attempt 名缺片被汇总正确拒绝。完整 apply/review/resume/abort、B01–B09 与三材料独立入口闭包仍 `NOT_RUN`，T1 候选不能冒充完整 B 产品。

- **PR149已交付测试诊断，main首次CI仍红（2026-11-14）**：PR首次run43573586653 attempt1六job成功并经独立核验，正常保护合入main `c2cf1c71c65f46dea161e41b140df99d137a5686`。该main自身首次run43576167008 attempt1为独立确认的`FAIL_THIS_MAIN_PUSH_CI_ONLY`：Windows两片各一个call失败，合计2 failed/3167 passed/37 skipped、完整3206节点，汇总正确拒绝；UI/Ubuntu/macOS成功。M2第13轮定位到append_many真实BEGIN SQLITE_BUSY，等待起点已从ring淘汰；MCP原2秒从服务发现起等待整个worker，隔离审批/unknown屏障证明该测试混合计时。最终MCP569b/M2f220两文件已获独立限定内容PASS；启动/accept清理真缺口修后复验，原断言与期限保留。1163固定输入的lock/full/ruff均0，3199 passed/8 skipped、635.00s，前后无漂移。门后只回填事实与证据，正式对象/新原生CI另核；实际Windows锁机制及MCP原卡点仍未知。详见[本轮记录](WINDOWS-CI-TIMING-2026-11-14.md)。#145/#147保持开放，恢复B继续暂停；下方“待原生CI”是旧提交前时点。

- **M2测试诊断修后本机通过，原生CI待核（2026-11-14，提交前）**：原9eb完整门3196 passed/8 skipped虽绿，但独立真实路径别名验收FAIL，原件保留；只补两处canonical和真实别名/外部副本回归后，766a源以其余1111文件精确继承获得独立内容窄PASS。最终1112输入lock/full/ruff全0，**3198 passed/8 skipped（546.63s）**，源/HEAD/status稳定；原六assert/60轮、生产/CI/UI不变。旧独立两个真实BEGIN区分与有界语义按同源继承，不冒充重跑；Windows junction分支本机未执行，新原生执行另核。只补失败现场，不宣称#145/#147或b450原红根因已修，恢复B仍暂停。[M2诊断记录](M2-CI-DIAGNOSTICS-2026-11-14.md)绑定旧FAIL、新PASS及完整门原件。

- **main 正常收尾 CI 新红，根因仍待定位（2026-11-14）**：审批诊断 PR148 已正常保护合入 main `b450c339d546ae2007ed9754e70de0108c4b86c4`，自身 push run43567298082 attempt1 经第二读者确认 `FAIL_THIS_MAIN_PUSH_CI_ONLY`。Windows片1正常收尾测试第55轮出现 `database is locked`，Run queued、无 run.end；Windows合3156 passed/1 failed/37 skipped，required汇总正确拒绝，其余四job成功。本次审批节点通过，#147根因未建立、#145未修，不能并为同因。隔离真实心跳持锁在租约获取和running转换两个BEGIN均产生同指纹，无hold正控通过；人为>5000ms不是原CI根因证明。本轮只补测试侧有界失败现场，原断言/60轮/生产/CI不改；详见[M2诊断记录](M2-CI-DIAGNOSTICS-2026-11-14.md)。

- **恢复B同步仅保存本地对象（2026-11-14）**：本地普通merge `277e82e265b54e0e8fe1b2fc1a99d747e95306d5` 的1127个blob已由第二读者绑定至已测冻结加唯一已审知识库事实delta，结论 `PASS_LOCAL_MERGE_OBJECT_BINDING`。未推送；main新红仍阻断恢复契约PR146推进，B产品全部未实施。该对象身份结论不是新full或新原生CI通过。

- **审批删除根因未定，失败诊断已补（2026-11-14，提交前记录）**：PR #146 head `3e9da0bd63399fc86488018605138b54b22fb427` 首次 run43559488826 attempt1 的 Windows 片0在 Run succeeded 后 old.txt 仍在处失败；原红、未合状态及 issue #147 保留，不归因 flaky/基线或与 #145 同因。固定77源的50次本机未复现不抵消原红。本片保留原13assert/时限，仅补 failure-only 真实链与采集保护；最终源 `f138d6c1` 通过独立短关键字段 delta 验收，本机 lock/full/ruff 全0，3186 passed/8 skipped（544.97s），1080项前后稳定。旧截断与 resource 遗漏的 PARTIAL、观察器过强及人工中断门均保留；正式 Git 对象与原生 CI 需另行绑定，不能外推本记录。[诊断记录](APPROVAL-CI-DIAGNOSTICS-2026-11-14.md)给出原件与字符/分页/脱敏边界；恢复B仍未实现。

- **恢复 B 同步候选本机通过，main 自身 CI 新红阻断推进（2026-11-14）**：1127 文件冻结候选 `2df18981` 的 lock/full/全仓 ruff 均 exit0，**3186 passed / 8 skipped（524.89s）**，源码、HEAD 与 Git 状态前后不变；同步文稿独立结论 `PASS_SYNC_DOC_DELTA_ONLY`，相对 main b450 仍仅原32文稿，全部产品与测试等 main。随后 main 自身 push run43567298082 attempt1 终态 **failure**：Windows 片1及 required 汇总失败，其余四 job 成功；实际 checkout 为 `b450c339d546ae2007ed9754e70de0108c4b86c4`。唯一失败为 `test_normal_finish_and_pause_do_not_report_heartbeat_failure` 的 call，第55轮 `OperationalError: database is locked`，Run 为 queued、无 run.end；本机绿与 PR148 绿均不能覆盖它。完整六job日志和两份分片原件已收齐，独立终态核验与根因调查仍待；不预判与 #145/#147 同因。恢复契约推送暂停，B 产品仍未授权/全部 NOT_RUN。下方“仍待终态”是同步开始时的历史观察。

- **恢复 B 契约 PR #146 同步诊断基线（2026-11-14）**：审批失败诊断 PR #148 已正常保护合入 main `b450c339d546ae2007ed9754e70de0108c4b86c4`；其 PR 原生 run43565231609 attempt1 六 job 成功且经第二读者独立核验，main 自身 push run43567298082 仍待终态与独立核验。本分支只同步该基线并合成知识库双方事实，恢复契约正文与产品实现不改；原 #146 head `3e9da0bd63399fc86488018605138b54b22fb427` 的 run43559488826 首红保留，issue #147 根因仍未建立，#145 仍未修。新组合须另跑本机门禁并绑定新提交与原生 CI，不能借诊断 PR 绿代替恢复契约 PR 通过；B 产品仍全部 `NOT_RUN`。

- **低磁盘 PR #144 已交付，main 自身 CI 独立核验通过（2026-11-14）**：正式 head `9413c7ca66af56fce8ac1664e989cf8cd6fc9808` 正常保护合并为 main `8f2a508e68f27efc4100b7466fcd79043341f7fa`（tree `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`）。PR原生run43553215035及main自身push run43555557537均attempt1、六job/四required成功；新main70steps与两份Windows有序3191节点全覆盖经第二读者核验。main结果Ubuntu3178 passed/13 skipped、macOS3183/8、Windows合3154/37、UI13文件91测试；一次watchdog后对应测试PASS。**issue #145仍open，旧Run failed根因未知未修，不能排除PR回归**；root明确接纳内部研发风险交付，不冒称读者无条件批准。实际runner与警告/限制、原失败见[低磁盘记录](LOW-DISK-2026-11-14.md)。

- **恢复 B 仅契约文字准备完成，产品未实现（2026-11-14）**：[受管导入、持久隔离与只读复核契约](restore-held-contract.md)保留合法v1 held导入与任意全空骨架，明确final namespace操作独占、stage_owned、publish_intent、abort_intent及删除目录屏障先于aborted。v3独立结论 `PASS_FOR_PURE_DOCS_PR_TEXT_ONLY`，原四项/容量和两处delta按精确原件继承；全部B产品验收/断点仍NOT_RUN。当前整理纯docs(contract) PR，不启用apply/review/resume/abort；解除及完整旧Run恢复留C，不能将文字/低磁盘CI当恢复服务通过。

- **本次纯契约候选本机门禁（2026-11-14）**：正式32文稿冻结经独立窄集成复审，结论 `CONTENT_READY_PENDING_FINAL_COMMIT_AND_GATES`，不等于产品验收。首轮lock0/full1/ruff0，唯一失败为官方端点计价测试继承 `ANTHROPIC_BASE_URL` 后得到非官方身份；完整 **1 failed / 3182 passed / 8 skipped（511.32s）** 原件保留。受控非官方覆盖1F、无覆盖1P及固定源码因果链经第二读者核验，测试的环境依赖未修。清除继承的模型/凭据配置后，同一候选lock/full/ruff均exit0，**3183 passed / 8 skipped（514.95s）**，1092份文件、HEAD与状态前后不变；随后只回填本条及会话日志。正式SHA与本PR原生CI另行绑定，B产品仍全部NOT_RUN，未运行付费模型或soak。

- **历史：低磁盘PR #144 首轮原生CI失败，测试诊断增量待云端（2026-11-14，当时时点）**：旧head `96e3e485eb4ecbecadbadb54a60414967ab63649` 的run43549430150 attempt1已结束，Ubuntu/macOS/UI/Windows片0成功，Windows片1两项失败且required汇总失败。当前仅修两测试的CRLF文本协议与60次收尾失败诊断，生产/CI不变；心跳根因仍未复现、未修复。1030源清单 `2922a345` 的本机lock/full/全仓ruff通过（3183 passed / 8 skipped，513.63s），执行前后源与Git状态稳定。两测试已独立窄审通过，含强制真实子进程CRLF正控、坏READY及reason注入call真红；不等于Windows通过或心跳已修。原首红与新公开子集、独立身份见[低磁盘交付记录](LOW-DISK-2026-11-14.md)；下方旧“原生CI待核”为历史时点。
- **审批删除根因未定，失败诊断已补（2026-11-14，提交前记录）**：PR #146 head `3e9da0bd63399fc86488018605138b54b22fb427` 首次 run43559488826 attempt1 的 Windows 片0在 Run succeeded 后 old.txt 仍在处失败；原红、未合状态及 issue #147 保留，不归因 flaky/基线或与 #145 同因。固定77源的50次本机未复现不抵消原红。本片保留原13assert/时限，仅补 failure-only 真实链与采集保护；最终源 `f138d6c1` 通过独立短关键字段 delta 验收，本机 lock/full/ruff 全0，3186 passed/8 skipped（544.97s），1080项前后稳定。旧截断与 resource 遗漏的 PARTIAL、观察器过强及人工中断门均保留；正式 Git 对象与原生 CI 需另行绑定，不能外推本记录。[诊断记录](APPROVAL-CI-DIAGNOSTICS-2026-11-14.md)给出原件与字符/分页/脱敏边界；恢复B仍未实现。

- **低磁盘修后完整本机门禁通过（2026-11-14）**：906项固定源清单 `df72efbd` 上lock/full/全仓ruff均exit0，**3183 passed / 8 skipped（509.43s）**，源码、HEAD与状态无漂移；57份UI源码/测试/锁/dist精确继承879候选完整13文件91测试和install/typecheck/build通过结果，未重复未变UI。七文件修正保留原AUTH/eval/fence与硬退断言，修复过早提案过滤和旧夹具实际实例；六份历史脚本仅移名文本，原件/索引不改。906七文件窄增量已独立PASS（报告`016f4c2f`，四项已修、无新增窄范围阻断，BASE-B1仍未修）；最终事实封装/正式SHA和原生CI仍待，旧首红完整保留。[交付记录](LOW-DISK-2026-11-14.md)给出原件与范围。

- **低磁盘首轮统一门禁失败（2026-11-14，历史时点，修后结果见上）**：879 项固定候选的 lock exit0，全量 **41 failed / 3141 passed / 8 skipped（495.61s）**，源码、HEAD、状态前后稳定；同源完整 UI **13 文件 / 91 测试通过**，install/typecheck/build及dist一致性通过。独立诊断确认过早的 pure_only 提案过滤跳过原 Dispatcher 的父版本失效检查与拒绝审计，须保留原 AUTH/eval/fence 断言修复；另有旧手装 Loop 未接同一存储实例、旧规则计数及纵深故障夹具适配。ruff 7错来自6份历史探针，按原字节移名文本并显式映射，未压低lint门。历史879文档/公开证据增量已独立PASS，但不抵消本次失败；最终修后组合、正式SHA与CI待核。详见[交付记录](LOW-DISK-2026-11-14.md)。

- **低磁盘 T3 独立入口验收通过，最终组合待核（2026-11-14）**：第二读者在717项固定源清单 `7acc6843` 上确认 `PASS_T3_FROZEN_ENTRYPOINT_SCOPE` / `PASS_T3_FROZEN_NARROW_DELTA`，独立52项Python（50首轮+2 paused补充）、4项真实App DOM；11个最小接点变异各1个call行为红，0 setup/import/collection kill，行为阻断0。复原5项Python/2项App与27个已有拒绝案例持久材料核对均单列，不重复计数；report/facts/index为 `e1de27bf` / `a07fe5ce` / `537c937f`。整分支读者核211内核路径继承及原T1/T2证据身份，发现公开索引3份视觉dist被忽略的INT-E1，原717报告保持不批准；root以最窄`.gitignore`例外补原字节，另清楚说明作者711核心+6派生=717的历史口径，待新候选独立销账。统一全量、lock、全仓ruff、完整UI与原生CI未运行；详见[低磁盘交付记录](LOW-DISK-2026-11-14.md)。

- **既有CLI预算装配缺口未修（2026-11-14，BASE-B1）**：整分支第二读者比较a022/T2/T3，确认`cli._engine`均未传`stack.budget`；真实离线ScriptedModel写出model.intent/result但engine预算为空、账户和预占均0。这是基线既有P2，不是T3新增，单独跟踪；其他入口预算证据不得外推CLI，低盘本片不冒称修复。原探针与源hash在[717整分支复审](evidence/low-disk-2026-11-14/integration-review-717/REPORT.md)。

- **低磁盘 T3 作者候选历史时点（2026-11-14，已由上方独立验收更新）**：T1 round1 在542源冻结和六文件 round1 delta 上由第二读者独立确认规格/质量窄范围 PASS；T2 在543源冻结上独立确认 `PASS_T2_FROZEN_CONTENT_ONLY` / `PASS_T2_NARROW_DELTA_AND_INDEPENDENT_EVIDENCE`，最终独立组合 **80 passed（11.01s）**，9个变异产生10个行为失败、0 setup/import/collection kill，阻断0。T3 作者已接通严格 CLI `--execution-mode`、RPC `run.start.params.execution_policy` 与两条 UI 启动路径，最近 Python 三文件窄回归 **76 passed**、UI 目标 **4 passed**、typecheck/build exit0。T3 尚未独立复审，统一全量、lock、全仓 ruff、完整 UI suite 与原生 CI 均未运行；不能把 T1/T2 的独立 PASS 或 T3 局部绿合并成整功能交付。逐条 LD 映射、真红、负控与未验证范围见[低磁盘交付记录](LOW-DISK-2026-11-14.md)，稳定身份副本见[证据目录](evidence/low-disk-2026-11-14/)。

- **低磁盘 T2 作者时点记录，已由上方独立 PASS 替代（2026-11-14）**：以已审 T1 round1 的542源文件为固定输入；普通非 pure 在审批前、消费批准/grant及intent前、TaskStore/File/Memory/network/MCP真实边界现测同一材料上下文。pure_only 的非pure取消仍拒绝；effects/合法v0/v1既有读、普通/摘要脚本模型及停止控制保留原规则。MCP只为真实pending动作按身份/目标/资格/空间惰性恢复发现，发现后核原版本/schema，不把启动作为纯读/模型前提。作者新测试95项的相关分组均通过；16文件联合首轮510通过、3失败、1跳过，3项为审计前移与静态登记缺口，修后相关三文件142通过、MCP/派发三文件64通过，追加schema绑定3通过。8个单门行为负控真红并复原；双真实进程审批屏障、硬退unknown保全、ENOSPC旧Receipt/快照保全有定向原件。该历史时点没有独立T2通过、统一全量、lock、原生CI或产品提交；T3 当时尚未实施。

- **低磁盘 T1 round1 独立销账（2026-11-14）**：root正式授权已确认规格/质量PASS，C1/C2/Q1均已修、0 open，基线 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215` 上的完整542源冻结逐项匹配。原独立报告/事实/index哈希前缀分别为 `a2ec4da0` / `b5f56ebc` / `dfe69bb0`。下方T1作者“待独立”及旧FAIL是历史时点，保留原文；该独立结论不扩大为后续T2或整功能已交付。

- **低磁盘 T1 C2 与 round1 最终候选，待独立窄复验（2026-11-14）**：独立读者用真实 `EventLog.append` 证明新 policy 的浮点 `format_version=1.0` 可写为 v2、严格对象重读却拒绝。round1 只在 `run.start` 解析 v2 且结构通过后复用既有 `RunExecutionPolicyV1.of`，经原 `PayloadContractError` 拒绝坏资格；schema description 明说额外项目语义校验，未声称标准 JSON Schema 可区分1与1.0。旧回归2红8绿、修后10绿，删语义 hook 负控2红8绿并恢复；整数1经真实写入和重开读回，float/bool/未来值写端拒绝且无 event/version/blob 残留，其他事件/字段的2.0仍接受。C1+C2、takeover及相关14文件最终 **467 passed（11.17s）**，窄ruff/py_compile/旧v0/v1及镜像对账通过；40文件合并新冻，独立C1/C2销账、统一全量和原生 CI 仍待。

- **低磁盘 T1 C1 作者修复 round1，待独立窄复验（2026-11-14）**：独立读者在旧39文件候选中证明：控制创建预检高盘、最终事务低盘时仍遗留 `Task/task.created`，所以旧作者800项局部通过不能证明完整创建原子性。当前作者树已由 root 同树快进至 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215`；最薄修复只将控制 `create_task + start_run` 放入可组合事务，保留预检/MCP、租约与队列顺序。直接控制/remember/undo 的晚低盘与晚未知新增回归先 **6 failed / 7 passed**，修后13项通过；移除事务负控6项真红并恢复。受影响九文件最终 **249 passed（9.73s）**；额外 takeover 真子进程夹具在旧冻结与修后均因缺同实例材料1红5绿，按授权只补 `memory/contents` 后6项通过，移除参数负控真红并恢复。旧冻结与原始失败不改写，新40文件候选另冻；本条没有独立通过、全量、产品提交或原生 CI 结论。

- **低磁盘准入 T1 本机候选，未交付（2026-11-14）**：基于 `605014d44e8168dcfc4dce24f64e77200cd3e34d`，只实施创建纵向：严格 `RunExecutionPolicyV1`、真实位置探测、TaskStore 创建事务内最终空间门、仅 `run.start` 当前 payload v2，以及 direct/Loop/Control/记忆/undo/子 Run 接线。完整栈显式绑定实际 Snapshot/Memory/Content、authority 解析器与独立 execution.guard；CLI/daemon/eval 复用同一 contents 实例，MCP 延至新 effects 预检通过后准备。候选只登记有真实产生路径的两项空间拒绝；资格拒绝编号与执行/恢复封口留 T2，模式选择器留 T3。计划六文件联合 exit0、最终27文件关联 **800 passed / 1 skipped（38.12s）**，23个变异均真红并复原；原件与冻结索引在忽略目录保存，独立复核另绑精确内容。没有产品提交、全量门禁或本次原生 CI，不能称 LD-01–06 全部通过或整功能交付。

- **低磁盘 C4 拒绝编号契约阶段记录（2026-11-14）**：[契约 §5.1](low-disk-admission-contract.md#51-c4-拒绝编号待实施补充)约定资格不允许、低盘、空间未知三类拒绝；PRD 当时单列待实施表、可写规则为 75 项。该次只补文档；实际产生路径与代码/schema/PRD 当前清单的登记由后续产品候选完成，LD-01–06 在契约阶段均为 `NOT_RUN`。

- **低磁盘准入契约阶段记录（2026-11-14）**：[低磁盘 Run 准入契约](low-disk-admission-contract.md) 固定创建前 `pure_only/effects` 资格、默认 effects 与显式纯读入口，并覆盖已有 Run 纯读/模型恢复、停止控制例外和真实跨卷材料位置。v2 独立文字复核为 `CONTRACT_TEXT_READY_FOR_FORMAL_PR`，已修文字2、部分0、未修0；LD-01–06 产品验收全部 `NOT_RUN`。本次只提交契约与事实记录，恢复 apply、容量/GC、真实付费任务、十小时长跑及普通用户发行未因此交付。

- **PR #137 原生 CI 已通过，合并事件已确认；merge SHA 自身 push CI 未获证（2026-11-14）**：正式 head `5d046f724ac14aa2a79ce266f4bfc9a6abf0239f` 的 [run43495488294 attempt1](https://github.com/lorek666/tianshu-agent/actions/runs/43495488294/attempts/1) 六个 job 全 success：Ubuntu **2982 passed / 13 skipped**、macOS **2987 / 8**；Windows 两片 **1487 / 11** 与 **1471 / 26**，合 **2958 / 37**，同一完整 collection 2995 项分为1498/1497，Windows required 汇总成功；UI 12文件87测试及类型/构建/dist一致性通过。第二读者结论 `PASS_THIS_NATIVE_PR_CI_ONLY`，核对两份原生 artifact 的完整阶段与身份；3次 watchdog 后对应测试均 PASS。Windows 证据限 Server 2025 x86_64 / Python 3.12.16 runner。普通、绑定 head 的合并请求返回502后，真实 main 曾推进至 merge `6c291a600d8edcd5435eea268b3c2f2a71470c2d`（tree `69f8bfeca64460a6a8a72b2d8e279ab45e76e026`，有序 parents 为 `016910970d381a66ab12a94a5c88080a8cbf7437`、上述 head）；截至 `2026-11-13T20:35:32Z` 的历史观察仍为 REST/GraphQL OPEN / merged=false。后续在 `2026-11-14T01:40:35Z` 查 REST 得 `state=closed`、`merged=true`、`merged_at=2026-11-13T21:26:14Z`，故合并事件本身已销账；同一时刻查该 merge SHA 的 workflow runs 为 0，只能沿用 head 的既有 CI，不能称 `6c291a6` 自身 push CI 已通过。下方旧红与候选记录保留各自历史时点。

- **PR #137 H3 原生 CI 仍未通过（2026-11-14）**：正式 head `e324edeb9c57cd9e8fe851a2657da8420e7838a2` 的 [run43474954367 attempt1](https://github.com/lorek666/tianshu-agent/actions/runs/43474954367/attempts/1) 中 Ubuntu、macOS、UI 三项 success；Windows job132056273251 达到25分钟上限取消。三个原失败 CLI 已 PASS，但 Windows 部分输出的 **2765 PASS / 31 SKIP** 不是整套汇总或整套通过，不能合并。旧红保留，不用相邻输出时间窗口作 pytest duration 或性能根因。

- **Windows 双 runner 分片作者候选（2026-11-14）**：仅 CI 基础设施及流程文档变更，原 watchdog、业务源码/测试、collector、锁与 UI 保持不变；两个独立 Windows runner 从同 SHA 完整 collection 按文件节点数分组，Ubuntu required 汇总同时要求上游 success 与两份完整、同身份阶段报告。合法 teardown skip/xfail 沿 pytest exit0 语义接受，迟发 wrapper 失败不能被 session0 覆盖。v1本机67项与原包装器31项定向98 passed / 0 skipped（25.36s）后，独立实际复审确认 **C1：收集错误原生 exit2 被插件异常改成3**，旧绿色不能作该行为已修证据。v2只修插件并加强测试：语法错误+0/1/2有效文件及收集早/晚、调用期真实中断均对照原 wrapper 精确保留2，新增文件定向 **71 passed / 0 skipped（16.60s）**；冻结v1上精确新回归4 failed / 2 passed，原件保留。第二读者已给出 `PASS_C1_DELTA_CONTENT_AND_LOCAL_BEHAVIOR_COMBINED_GATES_NATIVE_CI_PENDING`：12个真实受影响场景确认C1已修，部分修/未修/内容未决均0，其余未变主线继承v1独立32场景；不是把作者71项当独立证据。最终本机 lock/full/ruff 均exit0，**2987 passed / 8 skipped，pytest汇总485.59s**（命令485.79s），537份文件、HEAD与状态前后不变。执行身份为H3加六文件v2未提交冻结组合，之后仅补本知识库事实；正式提交绑定与新原生CI仍另核。节点数仅为平衡启发式，未宣称性能改善或交付通过。

- **PR #137 同步最新 main（2026-11-14）**：本分支先形成 Windows 测试修正 `cbd80f7350953df2179845c7f5e344e25f795985`，随后普通 merge 同步另一会话已合 PR #139 的 main `016910970d381a66ab12a94a5c88080a8cbf7437`。仅知识库三处新增记录冲突，按双方历史并存解决；该 main 的生产模块、测试及 UI 产物逐字节继承，不在本片重复修改。旧2743项本机结果不外推这次实质变化后的新组合。来源及知识库解冲突已由第二读者独立核对；新535文件冻结组合的 lock/full/ruff 均exit0，**2916 passed / 8 skipped，pytest汇总489.93s**，535文件、HEAD与状态前后不变。执行身份为 H2 加待提交的 main 集成组合，之后仅回填本知识库两条结果；最终正式对象与四 required 继续另核。

- **PR #137 Windows 测试修正候选（2026-11-14）**：正式旧 head `0b3f60d6fe330a2a9b3fe1bfc8ff373f8b7dd5ff` 的首次 run43443020017 中 Ubuntu、macOS、UI success，Windows 三项 CLI 测试 FAILED 后整套达到25分钟上限，故未通过、未合并。固定旧源码的短诊断证明文本 stderr 使用 CRLF，原 LF 字节断言不适用；完整 collection 后执行原顺序266项又复现发布屏障后的 `INTERNAL_ERROR`。进一步真实 PID/EOF 观测确认 Windows venv 启动器与执行收集器的解释器 PID 不同；直接基解释器的两个 phase 均 PID 等于 Popen PID，真实 terminate→wait 后同进程句柄已退出、零剩余输出、收据边界正确。当前仅修测试：精确匹配平台文本换行，硬终止用例在 Windows 直接启动基解释器、同一屏障严格核 PID，等待退出后再收尾管道；普通 CLI 仍覆盖 venv 入口，生产收集器不改。精确测试候选 `22fdda8d…` 经第二读者窄审无本范围阻断，独立3项真实 CLI 通过，真子进程伪报 PID 在身份断言被拒绝。修后本机 lock/full/ruff 全部exit0，**2743 passed / 8 skipped，pytest汇总462.23s**；执行身份为旧 head 加测试与当时知识库的未提交组合，524文件、HEAD与状态前后不变，随后仅补本知识库结果。正式新 SHA 的对象绑定与四 required 继续另核；不以诊断或本机结果代替 Windows 通过，所有原红保留。

- **第二批 collect-only 提交前最终组合（2026-11-13）**：在 `cefa1e375974d96e2ec25e199f6605f6dcf2239b` 基线上，收集器 BE-01/BE-02 已获第二读者 `PASS_COLLECT_ONLY_CONTENT_V3`，部分修/未修/内容未决均0。BE-02 独立9项、BE-01单次故障4项及原早期漂移探针通过；三种行为变异均为真实kill，关联118项另列不相加；未变范围按精确v2原件继承。最终本机 lock/full/ruff 均exit0，**2743 passed / 8 skipped，445.72s**，524份候选文件、HEAD与工作区状态前后不变；之后仅补本条及三文档事实/导航。本条是提交前快照，正式完整SHA、当前四required及合并后main自身CI另核，后续结果见 `codex/g3-baseline-evidence` 对应PR。旧红与观察器错误原件保留；只交付显式已有报告/指标的收集，不认证历史执行、不运行真实模型或10h长跑，不能称第二批/G3整批完成。

- **G3准入迁移 PR #136 已交付（2026-11-13）**：正式head `4fa7d4b66b9d6540c1821cff3b70a83cbc6f3ee8` 已正常保护合并为main `cefa1e375974d96e2ec25e199f6605f6dcf2239b`；PR自身首次run43424475453、新main自身首次run43428459428的四required均success，且分别经独立对象/原始日志核验。只交付T1/T2/T3准入迁移切片；恢复隔离/受管apply、容量与执行GC仍未交付，下方旧候选记录保留各自历史时点。

- **第二批收集器 BE-02 作者整改候选（2026-11-13）**：原生 Git 对尚不存在、仅由 `/ignored/` 覆盖的输出目录，精确 `check-ignore` 返回1，尾斜杠查询返回0；这是目录忽略规则的可用性问题，尚无泄漏或收据缺陷证据。候选先对 canonical target 运行固定本地 `--literal-pathspecs ls-files -z`，拒绝目标或后代已跟踪，再按尾斜杠目录语义检查 ignore；Git/I/O 错误保守失败。有效旧候选回归为 **3 failed / 7 passed**，修后 BE-02 **11 passed**（含字面 `[]` 与相邻跟踪名字正控）、BE-01 六项回归通过，原三文件关联 **118 passed / 0 skipped**，两文件窄 ruff 通过。首轮另有两条“缺失 tracked 子树尾斜杠也必返回0”的测试假设错误，原件保留并排除，不算产品缺陷；两指南原样继承。BE-01 原读者结论、BE-02 独立复验、最终组合全量与当前提交 CI 均另核，不称独立解决或第二批完成。

- **第二批收集器 BE-01 作者整改候选（2026-11-13）**：独立读者在 v1 脚本 SHA256 `3d64153cd27fd24eace08f2fb2a93a8e65b0f5bf68031c76bd5d156a4f99bf78` 的首次报告读取后改源码，Git 仍 clean，旧候选错误 exit0 并发布含改后 hash 的收据。作者仅把初始 collector 身份读取前移为 `collect()` 第一操作，保留最终复核；新增六项回归在旧候选 **3 failed / 3 passed**，修后六项通过，三文件关联 **107 passed**。早期删除为真实临时文件删除，不可读为 I/O 叶子抛 `PermissionError`，不冒充 OS 权限或 Windows ACL 实测。旧 v1 原件与两指南不改；BE-01 独立复验、最终组合全量和当前提交 CI 待核，不能称独立解决或第二批完成。

- **第二批基线证据收集器历史 v1 作者候选（2026-11-13）**：本条保留当时自验，BE-01 已否定其早期 collector 漂移覆盖，后续候选见上条。`scripts/baseline_evidence.py` 只收集显式已有 C5 报告与可选指标，保全私有字节原件，摘要严格限字段；输入、收集器或 Git 可见漂移及发布前失败不出完成收据。执行/批准固定 `NOT_EXECUTED_BY_TOOL` / `NOT_EVALUATED`，不运行模型或 soak、不升级历史结论。新工具定向 **54 passed**，报告/指标关联 **101 passed**，窄 ruff 通过；独立复审、最终组合全量与当前提交 CI 尚待完成，不能称第二批或 G3 整批完成。入口与边界见 [真实任务台账 §7](real-user-tasks.md#7-显式收集已有报告开发工具2026-11-13) 和 [指标指南 §9](runtime-metrics.md#9-将显式指标文件与已有报告一同保全开发工具2026-11-13)。

- **第二轮debug修复候选（2026-11-13）**：从真实main `cefa1e375974d96e2ec25e199f6605f6dcf2239b` 重新复现19项（6P1/11P2/2P3），授权、审批resume预算、两provider/MCP/网络/IPC、备份预演、记忆UI与摘要来源已完成最小修复及独立内容复审。DATA05已由cefa迁移修复销账。二审发现摘要REAL被int截断残留，补严格整数后原探针拒绝；首全量1failed/2836passed/8skipped为AST误把HTTP opener.open当文件IO，已窄修并经独立负控，最终完整门禁lock/ruff/full均exit0，2845passed/8skipped、500.10s，277个非docs对象前后无漂移；UI类型/87tests/build及dist一致。正式四required另核。只同步main `c1bad0a` 的工程教训文档，不覆盖另一会话。完整范围、红绿原件、边界与最终版本记录见[第二轮总账](DEBUG-ROUND2-2026-11-13.md)及对应PR；本条候选快照不预先宣称合并或Windows原生通过。**2026-11-14 对账**：该候选已作为 PR #139 正常合入 main（`0169109`，2026-11-14，四 required 以该 PR 的 CI 为准）。

- **G3 数据准入与迁移提交前本机快照（2026-11-13）**：T1 最终585、T2、T3精确内容已分别通过独立第二读者，未决0；普通merge同步实际main0df，保留双方知识库历史，已审产品/测试不变。最终组合 lock/full/ruff 均exit0，**2672 passed / 8 skipped，433.15s**，267个非docs冻结对象前后无漂移。T2独立81+298+2，12变异11真实kill、M05保留守卫拒绝不记kill；T3独立35/0skip、两个窄CLI变异真实kill，首30/5为观察器顺序问题且原件保留。README/指南已补实际版本准入与显式migrate，作者历史候选原件不回写为新批准。本条提交前快照时正式提交、四required与新main自身CI仍待核；后续完整SHA、当前检查与合并事实以 `codex/g3-data-admission` 对应实现PR正文和后续独立原件为准，不回写本条历史时点。不能称G3整批完成；下方均为各自历史时点。见[集成记录](DATA-ADMISSION-2026-11-13.md)。

- **G3首片PR #134已正常合并（2026-11-13）**：最终head `b5b5c7fed03f3bd7c75ce759708e383e5ec46091` 的run43409722913四required全部success（Ubuntu2434/13、macOS2439/8、Windows2410/37、UI80及typecheck/build/dist）。第二读者核对四份原始日志、实际checkout/tree与保护后，正常merge为main `69119f009096df74fc8e370d5d1c6d71cad3f290`，完整tree与已测head一致；未改门禁或重跑旧CI。见[独立CI记录](evidence/data-lifecycle-2026-11-13/pr134-b5-ci-independent-review.md)。main自己的push CI另核，T1/迁移/恢复隔离/容量GC不在本PR验收范围。

- **G3 T3 CLI 本机候选（2026-11-13，独立复审待验）**：在 `585e347` + 精确 T2 冻结 `dddec59d` 上新增 `data migrate --data-root ROOT --backup BACKUP [--json]`，只接已审迁移库；六类错误结构化输出、文本/JSON 脱敏，保留四个真实 77。两文件定向 **56 passed**，含六种真实单故障与移除故障正控、双子进程锁屏障、完整事件历史/重复续做、进度与 DDL 实际失败、链接和非标准根拒绝、编程异常不洗白。父会话已核 T2 独立 `PASS_TASK2_CONTENT_ONLY`，不改写下方 T2 历史候选原件；T3 独立复审、同步实际 main 后的最终组合全量和新原生 CI 仍待核，不能称 G3 完成。见 [T3 记录](DATA-ADMISSION-T3-2026-11-13.md)。

- **G3 T2 本机候选（2026-11-13，独立实现复审待验）**：基于已核 T1 的 `585e347`，显式迁移增加完整 typed 业务/机制摘要、三方双向材料、原 manifest/计划绑定与严格 current 提交窗口；完成重复 A/B 不写进度或 DB。迁移定向 **90 passed**、备份/准入关联 **208 passed**，lock/ruff/diff 通过；438 个非授权基线文件无漂移。初次失败、11 项精确旧源码真实未拒绝重演与 pre-open 三红→三绿均保留。只形成库候选，T3 CLI、最终组合全量、新原生 CI 和独立负控另验；详见 [T2 记录](DATA-ADMISSION-T2-2026-11-13.md)。下方“T2/T3 尚未交付”为各历史时点，不能覆盖本条或提前当作整批通过。

- **T1修后本机完整组合通过（2026-11-13，CI待核）**：912加最终九项准入与736d77c6恢复夹具顺序适配，lock/ruff/full exit0，**2566 passed / 8 skipped，435.78s**；264项非文档冻结无漂移。原九项内容、集成继承和夹具的独立正负控均通过；夹具保留全部旧恢复断言，历史非原子实现仍真红。正式提交绑定与新原生CI另核；T2/T3尚未交付。旧全量/首轮四红是历史过程，不能覆盖本结果。见[T1记录](DATA-ADMISSION-T1-2026-11-13.md)。

- **T1组合首轮历史失败（2026-11-13）**：912与最终九项T1组合首轮全量4 failed / 2562 passed / 8 skipped（432.50s），264项非文档冻结无漂移。四红均为main #133新增恢复测试的corrupt夹具先写历史材料再建空库，被新准入正确拒绝，未到恢复；仅调整夹具为先建真实库后注入坏manifest，不放宽产品守卫或修改recovery。修后全量、独立提交绑定和新CI仍待核；下方旧30b全量与内容通过不代替它。

- **G3 T1 实际库准入候选（2026-11-13，`codex/g3-data-admission`）**：基线 `30b973d`，新增共享严格进度类型与真实 EventLog 只读准入前缀，检查通过后才设置 WAL/执行 DDL/创建 payload 目录。v0 与内存库保留；缺整个版本旁表、未来/畸形已登记版本、实际 v2 无 completed、in_progress 与错误 sidecar 均拒绝；有限 legacy completed 仅普通连接兼容。首轮全量三红保留；修复字节 I/O/空骨架误判，并经精确基线确认价格节点受本机端点配置影响。过滤配置后的全量 **2542 passed / 8 skipped，424.60s**；独立复审后仅增强重复字段测试，关联 **300 passed**、lock/ruff0，产品相对已绿全量不变。该新测试未含在旧全量，后续整合组合须再跑全量；独立结论与本片 CI 另绑。T2 备份绑定迁移/T3 迁移 CLI 以及恢复隔离/GC 仍未交付，不能称 G3 完成。证据见 [T1 记录](DATA-ADMISSION-T1-2026-11-13.md)。

- **T1 组合验收进行中（2026-11-13）**：作者已正式交权；第二读者按最终九项 SHA256 给出 `PASS_T1_CONTENT_ONLY`，127新增/158既有针对回归/41独立探针通过，重复字段负控已闭合。父会话从 `30b973d` 快进到已核对的 `9129095`，保留实际 main #133 与全部契约/计划归档，再原样恢复已审 T1 源码和测试；最终组合全量、提交绑定与原生 CI 待核，不把旧测试全量转作最终证据。

- **G3 下一片契约（2026-11-13，待实施）**：[数据准入与迁移绑定契约](data-admission-contract.md) 选择实际 DB 的兼容准入、独占迁移与备份/进度绑定；正常 v0 不盲标为 v2，0→2 仍只代表迁移机制。整批恢复 hold 是后续形状，具体记录和 apply 尚未实现；低磁盘、保留/GC 也未交付。该文档不是新的测试或发布通过记录。
- **G3 首片同步 main #133（2026-11-13，本机组合已验证）**：实际 main 已正常合入恢复修复，提交 `f2aef9da75d2e546af99baa523d196d1e099c196`；本分支同步该实际对象，产品/测试按两侧对象继承，仅知识库冲突保留双方内容。本组合 lock/ruff exit0，全量 **2439 passed / 8 skipped，427.25s**，255 个冻结非文档对象无漂移；UI 对象及3份 dist 精确继承 a839 已验证版本（80项），没有另跑未变 UI。下方 a839 与系统 debug 条目是各自历史快照；本组合正式提交绑定及新四项原生 CI 继续单独核实，不借上游绿色替代。证据见[本机门禁](evidence/data-lifecycle-2026-11-13/main133-integration-local-gates.json)。

- **G3 首片吸收 main 已合修复（2026-11-13）**：本地 PR #134 候选从编码修正 `fd5f56c` 同步 main `a8399062dd87675cc10b7c258ab4ce3679b53613`（I/O #131、预算 #132 均已合入）；仅知识库发生内容冲突并保留双方记录，产品和测试按各自已审对象组合。本组合 lock/ruff 均 exit0，全量 **2416 passed / 8 skipped，425.09s**，253 个非文档文件无漂移；UI 类型/80 测试/构建通过且 dist 不变。第二读者内容继承核对通过，完整合并提交及新原生 CI 另行绑定；恢复 #133 仍由另一 debug 会话推进，不采用其旧绿色替本组合背书。

- **G3 数据生命周期首片（2026-11-13，`codex/g3-data-lifecycle`，基线 `ccff93e`）**：新增显式 `data backup / verify / restore-plan`。CLI 备份持独占维护准入，正常 `EventLog` 真实连接持共享准入；材料覆盖 `events.db`、`artifacts/`、根 `blobs/` 和 `memory_blobs/`，发布前校验并从备份 DB 重推引用。预演只读，不创建目标、不激活旧记忆，报告未决 intent/审批/Run/租约及后续删除历史不可证明。实际恢复、全产品版本准入/迁移启动门、低磁盘保护和 GC 均未交付；首片不能记成整个 G3 已完成。行为与验收见 [数据生命周期记录](DATA-LIFECYCLE-2026-11-13.md)，操作见 [备份指南](backup-restore.md)。
**系统 debug 最终候选提交前快照（2026-11-13）**：A–G共确认18项（8 P1、8 P2、2 P3，无已确认P0），均按原实现负控、最小修复和独立第二读者证据收口；已声明限制与无复现疑点未冒充bug或已解决。总表、执行路径、复现命令与残余边界见 [系统总账](DEBUG-SYSTEMATIC-2026-11-13.md)。

- **I/O #131 已合**：正式head `b6fe974d9751a2252c2180bdec70e44a081dfb74` 的 [run43373823819](https://github.com/lorek666/tianshu-agent/actions/runs/43373823819) 四required success；main `6f05c4c681d9805ca4db1b1f5640822e113128a4` 与它完整tree相同，自己的 [run43376113307](https://github.com/lorek666/tianshu-agent/actions/runs/43376113307) 也四success。C-007最终5e7 Windows原生专项run43375185081及第二读者142份原件核对均通过；不再将它写成待核，旧六红仍保留。
- **预算 #132 已合**：正式head `a36a792c7b5ea2eaa0a6277443f96e5ceb594ea9` 独立对象绑定通过，[run43376464937](https://github.com/lorek666/tianshu-agent/actions/runs/43376464937) 四required success：Ubuntu2292/9（318.79s）、macOS2293/8（646.80s）、Windows2269/32（1343.04s），UI80及typecheck/build/dist一致性通过。正常merge为main `a8399062dd87675cc10b7c258ab4ce3679b53613`，完整tree与a36相同。
- **恢复 #133 最终候选**：已审组合 `d482c26f2eae2eeff2b45533df48a55e424c7b75` 普通merge同步实际main为 `dce34faad57374d7aca7dda8e256c1613a3c16f8`，完整tree未变；本次仅补交付事实文档。新完整SHA、独立差异绑定、当前四required和实际合并状态见 [PR #133](https://github.com/lorek666/tianshu-agent/pull/133)，本提交前快照不预先宣称绿或合并。最终本机三批lock/ruff/full exit0，分别2219/8、2293/8、2316/8；代码/测试/产物按248/249/251条精确冻结对象继承，不机械重复无变化全量。
- **真实页面恢复补验**：真实CLI/Host/Daemon与已提交dist、临时数据和loopback脚本模型；Daemon实际SIGKILL后CLI重启，页面“已暂停→继续→成功”保持同Run，21条旧事件前缀与唯一文件Receipt、hash/mtime保持；`unbilled_model_intents=1`和费用未知未隐去。手动重启和已settled写入后的模型中断不等于unknown人工核查或Host自动监护验收，见 [I/O证据](DEBUG-IO-2026-11-13.md)。
- **Windows性能仍有边界**：旧isolation两个120秒PIPE收尾阻塞已消除；正式I/O PR1142.05s、同tree main842.28s、预算PR1343.04s是不同run实测，不代表稳定提速或25分钟SLO。保留未归因耗时、GIL诊断限制、Actions迁移提示和未验证设备。原始日志、负控、截图与独立报告逐hash归档主目录被忽略的 `.omc/artifacts/debug-2026-11-13`；最终main/同步/清理实况见PR最终记录，不提前宣布完成。

以下为各阶段当时的历史快照，包括其原时点的“当前”“未合”“待验”；不覆盖上方最终候选快照，不将旧失败改绿或旧绿色挪作新门禁。

- **系统debug最终本地组合（2026-11-13）**：恢复候选吸收预算 `10cf60f6772f9ddec0ffd7a43b14f24f7dd333d3` 与I/O `b6fe974d9751a2252c2180bdec70e44a081dfb74`，三个领域业务对象不变，Host68ea、正式测试dc274a。最终组合本机2316 passed / 8 skipped、442.55s，lock/ruff0；251条冻结对象（排除docs/目录，仍包含根目录Markdown）前后hash一致。I/O最终测试内容预验通过，完整SHA对象继承及Windows/当前四required继续按版本绑定，三PR仍未合。旧dd49 Windows六红与所有历史结果保留；不提前推后两批引发strict同步重复CI。

- **预算最终测试版整合（2026-11-13）**：继承I/O `b6fe974d9751a2252c2180bdec70e44a081dfb74` 的dc274a正式回归，产品与九个预算业务对象不变。最终冻结组合本机2293 passed / 8 skipped、439.61s，lock/ruff0，249个非文档文件全量前后无漂移；独立对象映射、正式SHA与当前CI另绑，不使用旧609或a3绿色替代。三批本地收口后按依赖顺序只推最终候选。

- **系统debug整合历史快照（2026-11-13，最终状态见本节首条）**：I/O正式 `dd49f21530899d2d643fba031a7fe46ea69ebbde` 已获第二读者完整SHA批准，报告SHA256 `3e9778149d3fbbb0941436a8b33388511315a7deae24188c38430d2997b12d16`；当前四项CI run43367761924尚未收齐。隔离Windows修后实验 `cdc191f6a08f23373162580061946ce2bcdcf2ff` / run43368502126失败，第二读者正亲读原件定位，未把失败改绿。预算本地 `609c1f8cebd6e4bb10dad115bcad3d6038bb43bf` 已吸收dd49，本机2293/8、420.63s及323文件稳定，关键预算探针独立通过，正式对象绑定进行中。恢复批正在仅继承其精确Host修复，预算/恢复业务源码不变，新的组合全量与正式SHA另行绑定。三PR均未合并；基准确认18项（8 P1/8 P2/2 P3），无已确认P0。

- **诊断修复整合本机门禁（2026-11-13）**：合入已独立批准的I/O `7111cbb43cd71db9f25a1348d91e7b39a0d97dee` 后，冻结组合 `uv lock --check`、`uv run --frozen ruff check .`、`uv run --frozen pytest` 均exit0；本机 **2297 passed / 7 skipped / 396.59s**，运行前后全体跟踪文件hash不变。生产src/UI/schema/依赖与上一轮已审范围相同。该历史恢复组合已绑定本地 `4eb050b642e0f136eeb75a9626f6eb061c448495`，第二读者精确对象继承核验已完成；不含C-007，也没有该提交的当前远端CI，不能代替后续门禁。

- **系统 debug 整合门禁（2026-11-13）**：最终 I/O 与预算代码已由独立第二读者分别绑定 `a0cb461702e538b22192eacc9b27fffc030a34ac`、`21cd9b253e11ba71fcd8ad8fa610ab5646b42076` 验收；恢复生产已审，正式故障注入测试又强化了首次 authority 登记断点。完整组合本机 **2266 passed / 7 skipped / 390.29s**、lock/ruff 通过；预算/I/O 组合 **2243 / 7 / 387.42s**。这些不代替云端结果：I/O 历史a0 run `43349177427` 的 Windows 在60秒诊断输出时 access violation/exit139，另外三项成功；后续纯标准库Windows实验确认原生定时机制风险，C-005/C-006修复已推I/O `7111cbb43cd71db9f25a1348d91e7b39a0d97dee`，7111的run43359223697随后Windows因Host清理WinError5失败，另三项成功；当前dd49/run43367761924的Ubuntu/UI已成功、Mac/Windows仍待，三个PR均未合并。完整版本和证据见 [系统总账](DEBUG-SYSTEMATIC-2026-11-13.md)。

- **系统 debug：崩溃恢复（2026-11-13）**：命令恢复按持久化执行边界判定，已执行或证据不足不再被文件空快照判为未生效；unknown结果、本地目录/对象阻断与派发终态在短事务中提交。升级补偿旧孤立unknown，不复活人工unblock。新增23项真实副作用/硬退出/恢复幂等回归，关联237通过；完整证据见 [恢复记录](DEBUG-RECOVERY-2026-11-13.md)。组合工作区本机全量为2226 passed/7 skipped（374.22s），lock/ruff通过，UI80通过且重建产物一致；此处是未提交组合快照结果，不冒充任何拆分提交的 CI。

- **系统 debug：预算结算（2026-11-13）**：原部分 usage 与负 usage 两缺陷经 40 项回归修复，独立复审又发现真实调用漏金额限额、旧负计数仍能准入、摘要错误未计未知三项。第二轮接通普通/摘要/重试金额预占与父子限额；缺价且金额受限则发前拒绝，无金额限额的零货币预留不代表零费用，实际缺价调用的金额仍 `partial` 未决；旧非法数量拒读拒写、不洗白。第二轮新增 32 项、原 40 项、独立 8 候选通过，关联 328 项通过（批次有重叠），本机全量 **2235 passed / 7 skipped / 381.25s**，lock/ruff 通过；但二审在 `51d46e0…` 确认 B3 仍部分修：摘要失败后立即预算拒发时终态读到旧游标。第三轮已补摘要调用后立即保存游标，两 provider 正式回归与冻结独立探针均 **2 红→2 绿**，并检查数据库重开与 replay；受影响 183 项通过，新全量 **2237 passed / 7 skipped / 384.75s**，lock/ruff 通过。当前提交独立复审/CI 继续单独绑定。详见 [预算证据](DEBUG-BUDGET-2026-11-13.md)；作者测试不构成新提交的独立批准。

- **预算批整合C-007（2026-11-13）**：本地556预算提交吸收正式I/O `dd49f21530899d2d643fba031a7fe46ea69ebbde`；源码/测试只继承其Host边界，预算业务对象不变，知识库冲突保留双方历史。冻结323跟踪文件的新组合全量 **2293 passed / 8 skipped，420.63s**，lock/ruff exit0，运行前后hash一致。独立整合复审与后续当前提交四项CI继续绑定，旧a3四绿不代替本轮；本机不能执行的Windows原生拒读仍待runner。

- **诊断修复整合本机门禁（2026-11-13）**：合入已独立批准的I/O `7111cbb43cd71db9f25a1348d91e7b39a0d97dee` 后，冻结组合 `uv lock --check`、`uv run --frozen ruff check .`、`uv run --frozen pytest` 均exit0；本机 **2274 passed / 7 skipped / 394.72s**，运行前后全体跟踪文件hash不变。生产src/UI/schema/依赖与上一轮已审范围相同。该历史组合已绑定本地提交 `556e533d88a6258f55ecbade9434211901e5d00a`，第二读者已完成精确Git对象继承核验；尚未包含C-007，也没有该提交的远端四项CI，不能代替后续整合门禁。

- **Host 元数据整改：C-007/P2（2026-11-13）**：I/O PR #131 的正式 `dd49f21530899d2d643fba031a7fe46ea69ebbde` / run43367761924 **仍不能合并**：Ubuntu2218/9、macOS2219/8与UI80tests成功；Windows **6 failed / 2189 passed / 32 skipped，1149.02s**。不是先前7111的一红，也不是cdc隔离实验的两红。第二读者用精确dd49测试在真实cp1252环境复现五项失败；只补测试`_CHILD`对生产CLI已有UTF-8初始化的调用，相同源码/环境/断言即五项通过。第六项是share0实际拒读成立、但错误强求CRT没有提供的winerror32；另修monitor探针主动丢弃GET造成的后台10053。产品保持68ea的条件发布→monitor实际启动→HTTP服务顺序，未吞异常/重试或改契约。最终测试dc274a已在保留cp1252的本机整文件19 passed/1 Windows-only skipped（24.06s）；失败stderr、真实worker身份与清理结果保全，Mac真失败路径已验两PID无残留。该测试版独立新增范围内容预验已通过（报告SHA256 0988a65c…），正式SHA、Windows与四CI仍待完成；旧3c68的2219/8本机全量及内容预验不能转移成新门禁。7111 WinError5、d6ee双Host退回、a624未启动join退回和全部旧版本证据仍保留，见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **I/O CI诊断整改：C-005/P2（2026-11-13）**：原I/O的Windows CI在60秒原生转储时access violation；另建纯标准库Windows实验 `92545fa418478afd1be6886f70ba5f5470e24c0b` / run43353423204，在相同CPython3.12.16/Server2025上0.687s复现原生dump退出0xC0000005，而Python线程和无dump控制正常。确认诊断机制风险，不把原失败COMMIT栈当成SQLite根因。仅测试包装器替换Windows定时诊断，保留fatal handler、60秒阈值、25分钟期限及全部门禁；后台等待/采样/写出异常不得假绿，真实abort/阻塞PIPE/退出码负控随代码提交。最终冻结工作区（a0基线加补丁）lock/ruff/full均exit0，全量2200 passed/7 skipped、390.82秒；第二读者随后将八文件Git对象与280份原件绑定正式提交 `7111cbb43cd71db9f25a1348d91e7b39a0d97dee` 批准；该版Windows正式矩阵31项诊断回归通过，三个60秒栈后相应用例继续通过。但整轮CI因C-007失败，不能把领域批准当成合并批准；细节及残余GIL边界见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **MCP测试生命周期整改：C-006/P3（2026-11-13）**：独立观察发现对象阻断测试业务通过后真实server/泵线程仍存活，原因是夹具未close client而被atexit保留，不是生产关闭失效。仅测试模块构造成功即登记finalizer，早于构栈/启动；90项相关测试、57次启动记录（55个唯一pid）在每teardown及结束均零残留，主动测试失败/构栈失败仍以pytest exit1如实失败且清理完成。与C-005最终补丁一同通过冻结工作区2200/7全量，八文件运行期间hash未变；已绑定正式提交7111的八文件独立批准；该领域未决与新C-007整体CI阻断分开记录，见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **系统 debug：I/O 边界修复（2026-11-13）**：从 `main@ccff93e79cfeb93e47e5a35012fd92fae366e941` 隔离排查。确认输出管道收尾锁等待、MCP 发送背压不受期限限制、Host 非法请求裸异常、重定向正文无界读取、UI 正文超时缺口和 SSE 失败计数重置，均先取得原实现失败证据再修复。第二读者在 `4479088ecb2d161755d7ba3a89e46119c94a8b8e` 补充发现深层JSON仍裸异常，原Host结论为部分修；补丁只捕获解析处RecursionError，新增真HTTP回归覆盖结构化400、认证顺序及同连接下一请求边界。随后对照Ubuntu原始日志确认既存C-004/P2：半帧写入遇到并发关闭，发送线程裸TypeError；真实子进程/短写屏障复现后，仅在传输已关闭时规范为ConnectionClosed，保留cause与两项类型错误负控，该IPC增量随后由第二读者绑定 `a0cb461702e538b22192eacc9b27fffc030a34ac` 验收，并经7111精确对象继承。构建与真页面验证、Windows 慢测栈、范围及未验证项见 [I/O debug 记录](DEBUG-IO-2026-11-13.md)。本条不代替具体提交的独立复审和四项 CI；最终门禁结果绑定对应 PR 当前 SHA。

> 各功能条目保留其注明日期、PR 与提交的验收快照；条目中的“仍未做”不能脱离该版本引用。后续同主题交付覆盖旧状态，历史数字不汇总成当前测试数。合并门禁以本节“门禁”条目为准，Windows 最新已验证结果以本节 Windows 条目及其 run 为准。

- **里程碑**：**M0–M5 已完成**。M4 由 PR #41 / #45 合入（代码在 `src/tianshu/eval/`）；**M5 桌面闭环由 PR #49 完成**（2026-11-10，`feat/m5-desktop`）：Daemon / Host / UI 三进程打通、审批 UI、关 UI 任务继续、重启后恢复。设计与契约见 [M5-desktop.md](M5-desktop.md)，实测证据见 [M5-verification.md](M5-verification.md)。
- **F10 独立复审与整改（2026-11-11，`feat/memory` / PR #64）**：[REVIEW-F10-memory.md](REVIEW-F10-memory.md) 对 `c4216e1` 做独立复审（高 1 / 中 5 / 轻 9），结论是**不能按"已验收"合并**；逐条处置见 [REVIEW-RESPONSE-F10-memory.md](REVIEW-RESPONSE-F10-memory.md)。· **高危 D1 按架构裁定改文档、不改实现**：`memory.search`/`memory.get` 的 `tool.result` 必然带正文——不留它，replay 就重建不出模型当时的输入，`messages_hash` 立刻对不上。所以"事件日志只存引用"**只对写入路径成立**（`Dispatcher._intent_args` 已把 propose 的 `content` 换成 `sha256`+长度），§3.1 / §11 已按实测改正，并把"`forget` ≠ 物理擦除、正文副本 = 事件日志读快照 + 正文 blob"写进契约。· **代码侧修 5 处**：① `Dispatcher` 拒收**另一个日志**的投影（同事务约束落到装配边界，不再靠调用方记得）；② Worker / daemon / eval / 测试替身统一复用 `stack.memory`（原先会各自 `open_memory()` 开第二个投影，自定义 blob 目录时写入成功却检索不到）；③ `parse_memory_uri` 与 `MemoryScope.workspace` **先拒相对路径再 realpath**（先 realpath 会把 `memory://workspace/notes` 按 CWD 补成绝对路径，fail-closed 退化成"取决于进程在哪启动"）；④ `memory.search` schema 暴露 `limit`（1..5，执行侧再夹一次）——否则 §8"模型可以要求更少"不成立；⑤ **被拒绝**的记忆写请求在 `dispatch_requests` 脱敏成 `content_sha256`+`content_bytes`（拒绝的请求永不执行，不破坏派发/审批绑定）。· **仍兑现不了（如实留账）**：编辑（`supersede`）与 workspace/task 作用域**创建**都没有生产入口（§13 #24 / #25）；**被采纳后**又被内容闸门挡掉的请求仍把原文留在 `dispatch_requests`（闸门在 executor 跑、请求行在 `prepare()` 就落库；彻底修要把扫描挪到落库前或给审计表定保留策略，§13 #26）；物理擦除仍是单开工单。· **一处自证伪**：§14 曾写"没有断言 origin 的测试"，实际 `test_agent_propose_then_confirm_through_dispatcher` 早就断言 `origin == "inferred"`，已改。
- **N6 控制面记忆端点落地（2026-11-11，分支 `feat/host-memory`，PR #100）**：daemon 的 `ControlPlane._methods` 新增 **8 个**记忆 RPC（`memory.list` / `get` / `remember` / `confirm` / `edit` / `forget` / `promote` / `erase`），`host.py` 原样代理 `POST /rpc`（有真 Host + token 回归用例）。新模块 `memory_control.py` 收口 CLI 与 RPC 的控制面记忆操作（写操作一律走 dispatcher：intent/result + 一次性审批 + 执行侧二次核验）。视图六项判据由 `memory_control.memory_view` 统一产出；`promote` 的源作用域用 `source_scope`，写后视图按记忆自己的作用域回读。测试 `tests/test_host_memory.py` 14 条（真起 daemon + 真 IPC + 真 Host `/rpc`）；负控：把 edit 端点改成「假成功、不派发」→ 红；全量 `1728 passed / 7 skipped`。**⚠ 身份口径按事实降级（复审抓到的核心错误）**：这些端点**不是授权边界**——v1 没有进程级沙箱，`run_command` 能以同 uid 起子进程读 IPC 凭据、跑 CLI 并自己落审批，所以「本机人 = 连得上 IPC」无法区分人与模型触发的同 uid 进程；端点只在**跨本地用户**这层有意义（POSIX 0700/0600；**Windows 命名管道 ACL 未加固且未验证**），**不新造身份令牌**（挡不住同 uid）。**未做**：React 面板、`memory.search` RPC、进程级沙箱。
- **N3 L2/L3 蒸馏 + freshness 落地（2026-11-11，分支 `feat/memory-distill`，PR #98）**：投影 schema **2 → 3**（`memories.layer TEXT NOT NULL DEFAULT 'l1'` 就地 `ALTER`，旧行一律 l1），`Memory` 补齐第 17 个字段。新模块 `memory_distill.py` 做**确定性合成**（LLM 合成仍明确没做）：L2 = 同作用域 active L1 按 token **重叠系数 0.34** 聚簇 → 引用式拼装（每行带 `[memory_id@revision]`，`proof_count == len(evidence)`，引文超 200 字节截断标注）；L3 = user 作用域里人给的 `preference` 按 **4 KiB** 预算整行打包（放不下标「预算已满」，不做半行截断）。两次合成**逐字节相同**、改一条源只动它那一块；页 id = sha256(scope, layer, content) 前 16 位 → 重复合成幂等。`memory.consolidate`（1.0 / create / 模型可见 / args 仅 `{scope, scope_id}`）：workspace **无条件拒**、task 只能自己；写 `candidate` + `origin=inferred`，人 `confirm` 才进召回。投影只认事件 `output`、**不重算**算法（replay 逐项一致）；旧页被取代后仍可按 id 查。**freshness**：`source_refs` 带 `seq:<basis_seq>`，作用域内出现更大 `last_seq` 的 active 未过期 L1 ⇒ `stale=true` 且排到新鲜 L1 之后（降权不短路；`search()` 与 `retrieve()` 的三条路径共用 `_order_by_priority(..., stale=)`；无 `seq:` 的旧行 `stale=None`＝不可判定，输出省略字段、不谎报新鲜）。测试：`tests/test_memory_distill.py` 22 条 + `test_memory_dispatch.py` 7 条真派发 e2e（另加「重复合成如实报状态」「多页失败不写正文」）+ `test_memory_store.py` 的 v2→v3 补列用例。负控 10 条真跑红。**独立复审（1 严重 / 4 中等 / 3 轻微）全部处置**：真洞是 `retrieve()` 无索引分支没有 stale 排序键（默认生产路径上陈旧页照样排第一，原测试被 origin 权重掩盖）；其余是假判据（v2→v3 迁移、坏页 `l1`/空来源、幂等、旧页 evidence 快照）、设计边界（active 页不因合成退役）与文档假话，均已改。**残留**：无 L2/L3 vs L1 的检索质量评测（不许宣称读页更好用）、无冲突消解（矛盾 L1 并列不裁决）、L3 仅 user 作用域、LLM 合成未做、执行侧 workspace 守护经派发不可达（纵深冗余，不声称覆盖）。
- **N2 检索质量落地（2026-11-11，分支 `feat/memory-retrieval`）**：**作用域优先级**（#23）：`memory.RecallPriority(task_id, workspace_roots)`，排序键 `(优先级, origin 权重, created_at 降序, memory_id)`；关键决定——优先级是**上下文**的函数、不是 `allowed` 的函数（`allowed` 只决定可见性），这正好解掉旧设计里"压序会依赖调用方传了哪些 scope、不可复现"的顾虑；主循环从 Task 绑定目录算、CLI 从 `--task-id/--workspace` 算。**向量**（#20）：新模块 `memory_index.py`——`EmbeddingProvider` 协议 + `MemoryIndex`（`memory_vectors` / `memory_vector_meta`、`model_id+dim` 过滤、`availability()` 返回原因、`rebuild()` 清空重算）+ 纯函数 `rf_fuse(k=60)`；`MemoryProjection` 用 duck-typed `index=` 接上（避免模块环），propose(active)/confirm/supersede/forget/erase 维护向量、`rebuild()` 清空向量后按事件重放；`retrieve()` 返回 `RecallOutcome(items, semantic, degraded, fused)`，`memory.search` 与 CLI `--json` 都带这三项。**能力边界自报**：内置只有确定性 `HashEmbeddingProvider`（**没有语义能力**，模块 docstring 与评测报告都写死不许对外称语义召回）；未配置 provider → FTS-only + `degraded` 原因；换 provider 未重建 → 旧向量不参与；`memory_vector_meta` 只在**成功 rebuild** 时写 built + provider；`availability()` 校验 built/身份/覆盖三条，空表与半套向量一律报降级（首轮复审抓到的假 true 已修）。**测试 +40**（rank 7 / vectors 26 / loop 输出·接线·构建入口·CLI 失败路径等），负控 5 条真跑过（建表事务 / 语义只重排 / 无 index rebuild 等）（去掉优先级 / availability 永不降级 / 去掉确认后的索引钩子 / 语义路只采纳 FTS 候选 / 去掉索引建表的事务判断）。**生产入口**：`build_stack(embedding_provider=)` / `TIANSHU_EMBEDDING_PROVIDER=hash`，并用 `tianshu memory rebuild-vectors` 显式建索引（只装配不会构建；~~只接受 hash 基线~~2026-11-14 W6 起也接受 `openai` 兼容端点，见下条 W6 会话日志）。**评测**：`scripts/eval_memory_retrieval.py` + `docs/memory-retrieval-eval.md`——22 query / 25 记忆，FTS MRR 0.864 → hybrid-hash 0.955，**Recall@5 两者都 1.000 = 饱和**，报告里写明"这不是语义能力证明"并列出五项没做到；千条级评测见 [memory-scale-eval.md](memory-scale-eval.md)（N=1000 下 FTS-only Recall@5 掉到 0.650，hash 融合保持 1.000；k∈{30,60,100} 不敏感）。· **偏差**：`_SCHEMA_VERSION` 没再上调（`memory_vectors` 是可选派生表，随 provider 身份落 `memory_vector_meta`，不进 `memories` 的形状契约）。· **仍没做**：L2/L3 与 `memory.consolidate`、~~真 embedding provider~~**2026-11-14 W6 已接线外部 OpenAI 兼容端点 + 外发 gate（模型检索永不外发），真实端点分数未跑**、reranker、~~跨千条记忆的召回上限与 RRF `k` 敏感性评测~~**已做（memory-scale-eval.md）**。
- **G1 收口轮（2026-11-11，PR #96 / #97 / #99，`main @ 850e046`）**：把 §5.2.3 / §5.6 剩下的三件补齐，G1 门禁只剩"执行器 / 隔离门 / UI / 固定开销"几条**已登记的不成立项**（见 §7 与交付对账 §4）。**G1c（#96）**：账本从"默认关闭"改成**默认记账**（`budget_limits=None` 时建账户、预占、结算都写，但任何维度都不设闸）；新增 `budget_bound="upper"` **可验证上界**=请求体 UTF-8 字节数（字节级 BPE 下 token ≤ bytes）+ adapter 能力声明的 `output_tokens`，拿不到输出上界就 `BudgetConfigError` 拒绝启动（不用 256 默认预留冒充上界）；`overrun` 越界如实记、上界口径停机；顺带修 `run.cancel` 在账户未建时丢取消意图的真 bug（先建后取消）。**G1d（#97）**：`RESOURCE_KINDS` 2→5（network/mcp/subtask），network 目标规范化（host 小写+IDNA、默认端口省略、`.`/`..` 折叠、百分号段先解码再判、拒 userinfo/fragment）；`fetch` 外发**恒需人工批准**（无自动路径）、MCP 与 exec 同级、委派因无执行器恒拒、种类混用拒；无执行器的种类在**派发前**拒绝（`dispatch.resource_kind_unsupported`）；6 个新规则编号 + 事件 `action` 枚举 5→7。**G1e（#99）**：新 `contents.py`（`content_objects`/`content_grants` 建在事件库同一 SQLite）+ 占位给出可用 `content://sha256:<hex>` 引用 + `read_content` 分页读取（≤64 KiB/页、`next_offset`）+ **按任务**授权（内容寻址去重≠共享可见）+ 读时**重算 sha256**；分页不切半个 UTF-8 字符。**测试 +43**（G1c 11 / G1d 15 / G1e 17），负控 **18/18** 实测红（G1c 6 / G1d 6 / G1e 12 含存储层）。全量 **1684 passed / 7 skipped**（165.86s，macOS arm64），`ruff` 干净，required checks 三个 PR 全绿。· **偏差**：G1e 原规格要加 `content` 资源种类 + 策略双闸，实现时按既有硬口径「只读动作不经过派发」（`Dispatcher._typecheck()`，`read_text`/`memory.search` 同路）改成读工具 + 存储层可见范围，SPEC §3.2 已如实修订。· **仍不成立**：网络/MCP/委派执行器（G2）、`run_command` 隔离门、每消息固定开销不在上界内、任务级外发白名单 `outbound_allow`、账本的 UI 展示、§7.1 语义摘要、G3 平台实机。
- **§7.1 语义摘要轮（2026-11-11，PR #103 / #105，`main @ 66c1a25`）**：把「模型摘要」从禁止项改成**受控能力**——默认仍关；开启后摘要作为**有来源、有版本、已持久化**的派生材料进入上下文，回放读当时保存的那一份，不重新生成、不重新检索。**H1（#103 第 1 提交）**：新 `summaries.py`，三张表（`summary_materials` / `summary_sources` / `summary_versions`）建在事件库同一个 SQLite；`material_id = sha256(jcs({材料版本, 作用域, sources[seq + 原文 sha256]}))`；三个字节上限（单条 1 KiB / 材料 8 KiB / 正文 2 KiB）；`pending → ready|failed` 一次性、正文写入后不可改（代码 + 触发器）、表上禁 DELETE、usage 未知留 NULL。**H2（#103 第 2 提交）**：`compile_context(..., summaries=())` 仍是纯函数（正文由调用方持久化后传入）；激活 = **整段覆盖 + 真的被牺牲**；`summarized_seqs` / `dropped_seqs` 互斥；装不下就**放弃摘要**回落确定性；`model.intent.payload.context.summaries` 只写**实际用上**的身份（正文不进事件，schema + JSON 两处同步）；`replay` 按 `summary_id` 读回正文并三道校验（行 / 版本与正文摘要 / 材料台账），**缺记录 = problem，不重新生成**。**H3（#105）**：`Limits.summary_mode`（默认 `off`）+ `summary_max_calls=3`；决策顺序 = 可复用优先（**子集复用**：覆盖的 seq 现在全都被牺牲的旧摘要）→ 上限 → 造有界材料 → 完全一致复用 → 生成 → 重编译；生成顺序 `begin(pending) 先提交 → 账本预占 → 模型调用 → complete(ready) → 重编译`；授权四条件（显式开关 + 模型准入 + 边界 + 预占成功，**不是人工审批**：材料本来就已发给同一 provider）；失败一律回落（预算拒绝 / upper 无上界 / 模型报错 / 正文不合格），崩溃注入点 `summary-after-call` 后重启见 pending 只标 failed **不重试**；摘要调用计入 `model_attempts` 与 tokens/cost，`RunResult.summary_calls` 从表按 run 数（resume 不丢）。**测试 +60**（H1 22 / H2 24 / H3 14），负控 **24/24** 实测红（7+7+10）。全量 **1790 passed / 7 skipped**（180.83s，macOS arm64），`ruff` 干净，required checks 全绿。· **偏差**：无（默认 off；未动事件类型 / 工具 / 规则编号）。· **仍不成立**：跨 Run 复用（材料身份含 seq）、增量/分层摘要、自动刷新（CLI 未接）、摘要质量评测、提示注入（包装声明只是提示级缓解）、保留期/GC、纯事件导出不足以回放摘要轮次；且本机长任务夹具里摘要模式**更贵**（多 3 次调用、token 324→744），买到的是「关键约束没丢」，不是省钱。
- **N5 崩溃恢复 + 审计表脱敏落地（2026-11-11，分支 `fix/memory-recovery-audit`）**：记忆写的投影更新与 `tool.result` 由 `Dispatcher._commit_result` 在**同一个 SQLite 事务**里提交，所以"有 intent 无 result"⇔**投影/事件层一定没生效**——`recovery.recover_run` 据此新增记忆专用分支：`RecoveryReport.memory_not_applied` 单列"记忆未生效"，结算 `ToolResult(failed, none_applied, {error, recovered, memory_write})`，**不写 `recovery.blocked`、不阻断目录**（记忆没有 preimage，别把"没写"和"不知道写没写"合并——红线 5/11）；**唯一例外是 `memory.erase`**（删文件在提交之外、不可逆）：文件已没时报 unknown 而不是 none_applied，见下方复审整改①；`assess_run` 也不再把记忆写算进 `pending_writes`（否则接管判定会去要一份根本不存在的快照证据）。另一半 #26 收口：`dispatch_requests` 加 `args_redacted` 列，请求进终态（`done` / `rejected`，含执行中途炸掉走 `_fail_unknown` 那条）时由 `_set_request_state()` → `_redact_terminal_args()` 把记忆正文换成 `content_sha256` + `content_bytes`；**执行期必须用完整参数**（`args_hash` 绑定校验、执行器从这张表取参），所以不能更早——而记忆写的终态推进与投影提交同事务，脱敏与提交同原子，"写完未脱敏"的窗口是零。非记忆请求一律不动（防过度脱敏有单独用例）。测试 +14 全在 `tests/test_memory_dispatch.py`（被采纳脱敏 / 闸门挡掉脱敏 / 中途炸掉脱敏 / 文件写不脱敏 / **已存 waiting 行被人否决后在 ON CONFLICT 分支也要脱敏** / 断电恢复报未生效），**负控（最初 7 条，后续追加至 17 条）**：关掉终态脱敏 → 三条脱敏用例红、文件写那条仍绿；删 `_recover_run` 记忆分支 → 恢复用例红；删 `assess_run` 记忆分支 → 同一条红（接管判定变阻断）；只修插入路径不改 ON CONFLICT 覆盖 → "等待后被否决"那条红。全量 **1581 passed / 7 skipped**（五轮整改后，已并入 main 的 #88/#90/#91）。· **偏差**：原规格要求新建 `tests/test_memory_recovery.py` 并做"两个真进程"断电，实际把用例放进既有派发测试文件（同一套夹具，另开文件要复制 `Env`），且跨进程断电没有在 CI 里做——如实登记在 SPEC §6.4，别当已验。· **复审整改（同日，独立复审高 3 / 中 4 / 轻 6，逐条修完）**：① 高——`memory.erase` 先删正文文件、后提交，删文件在事务外且不可逆，恢复原来对所有记忆写一律宣布"未生效"，等于把"内容已销毁"说成"什么都没发生"；改成按 memory_id 查投影：文件还在才算没生效，文件已没报 `unknown`（`memory_unresolved`，`ok=False`，**不**进会拿去阻断目录的 `unresolved`）。② 高——内容型记忆写在提交前就落 `memory_blobs/`，崩溃留孤儿副本（无行引用、擦除也删不到）；恢复按 intent 里的 `content_sha256` 找并**按引用计数为 0 才删**，共享 blob 绝不误删。③ 高——`_intent_args` 只对 `memory.propose` 脱敏，`memory.remember` / `memory.edit` 的正文原样进了 **append-only 不可删** 的 `tool.intent`；改成"记忆资源 + 参数里有 content"，不列工具名。④ 中——快照清单损坏的早退块把记忆写推进 unknown 并挡目录（与"不阻断"承诺相反）；早退块也先判记忆写。⑤ 中——升级前就在终态的行永不回填；加 `_backfill_redacted_args()` 打开库时补扫。⑥ 中——`_reject` 给被拒的**文件**请求也标 `args_redacted=1`（字段说谎）；改成"真的脱敏了才标 1"。⑦ 中——ON CONFLICT 单向保留脱敏值，让"终态后重新发起"的合法请求被判"参数被改过"；改成取最后一次写入的形态。⑧ 轻——恢复不更新 `dispatches` 表（仍显示"仍在执行"）；结算时一并推进到 `error`。**负控 17 条逐条真跑过**（7 条主项：关终态脱敏 / 改回单向 ON CONFLICT / 删 recovery 记忆分支 / 删 assess 记忆分支 / `_reject` 无条件标脱敏 / 跳过补扫 / `_intent_args` 只认 propose；10 条附加：erase 当普通写 / 关孤儿清理 / 删清单损坏早退块的记忆分支 / CLI 不打印 `memory_unresolved` / 恢复忽略传入的投影与 blob_dir / 迁移标记提前置位 / `recover_run` 忽略 blob_dir / 标记在但列被删（强制重扫）/ `memory` 与 `blob_dir` 的优先级 / 加列与补扫之间崩溃重试）。· **仍没做**：`memory.consolidate` / L2/L3 / 向量召回 / §8 作用域优先级 / Host-UI 记忆端点。
- **N4 物理擦除落地（2026-11-11，分支 `feat/memory-erasure`）**：`memory.erase`（控制面专属、`delete` + 一次性审批 + Receipt）删正文副本与索引，投影行 `forgotten` + `erased_at`，`content_state=unavailable`；**引用计数为 0 才删文件**，否则保留并在回执 `retained_by` 里列引用者；**已擦除的行不计入计数**（否则第一条会把第二条永远卡住）。删除顺序：**先删文件 → 结果与事件同事务提交**（崩在中间只留下"文件没了、行没标擦除"，正文如实不可用；反过来才是泄漏）。投影 schema v1→v2 只加可空列、**不重建**；`read_content` 见 `erased_at` 一律 None。测试 +8（擦除唯一/共享/控制面专属/**守卫内提交**/CLI/CLI 擦非 active/旧库迁移/高版本 fail-closed）；合入 #84（事件 payload 契约）后本分支全量 **1527 passed / 7 skipped**。· **合入 #84 时契约绑定测试当场抓到两处漂移**：工具白名单缺 `memory.erase`、`receipt.evidence` 的 `kind` 封闭集缺 `memory_erasure` 且不接受四个擦除字段——已登记进 v1 契约（`event_schema.py` + `schemas/c3/event.json` 逐字一致）。· **复审整改（同日）**：① 高——引用计数与删文件在提交事务外，两进程并发擦共享 blob 会都保留文件、两行都标擦除、孤儿留存；修法把**提交也放进同一把跨进程执行守卫**，回归 `test_erase_commit_runs_inside_the_execution_guard`；② 中——`verify_receipts` 不认识 `memory_erasure` 证据（合法擦除反被判"无法识别"）；补逐字段比对（与同次 `tool.result.output.erasure`），测试改成 `assert verify_receipts(...) == []`；③ 中——CLI 擦不掉 forgotten/candidate/superseded（DELETE 前置默认 active）；改成不默认声明状态、由执行侧 observed 兜底，补 CLI 用例；④ `count_content_ref` 注释与 SQL 相反、`schema_version` 高版本 fail-closed 未记录，已修文档。· **偏差**：原规格把删除顺序写成"事务内更新 → 删文件 → 提交"，执行器返回结果后才由 dispatcher 提交，实际无法那样排；已在 SPEC §5.2 改成实际顺序并写明理由。· **仍没做**：`memory.consolidate`（N3）、L2/L3、向量、§8 优先级、恢复记忆分支、UI。
- **F10 记忆产生侧落地（N1，2026-11-11，分支 `feat/memory-lifecycle`，PR 待合）**：控制面有了**可判定的**身份——`Task.is_control` / `Task.memory_scopes` 从 `task.created` payload 读回（只加键、不加事件类型，rebuild 也还原）；`Dispatcher.open_control_run(memory_scopes=...)` 给 CLI 用。C2：`memory.propose` 升 **2.0**（`scope`/`scope_id`，模型只能写 `user` 与**本任务** `task`），新增 `memory.remember`（`1.0`、`exposed=False`：创建即 `active` + `origin=user_explicit`）。执行侧 `memory_exec._verify` 独立核验：控制面工具要 `is_control`（越权 → 新编号 `c4.memory_control_scope_binding`）、模型写 workspace **无条件拒**（不因 `Task.is_control` 放行——同一 Task 上后开的模型 Run 会继承控制面标记）、task 只能本任务、声明作用域只在控制 Run；策略层同步（`memory_declared_scopes` **只由派发层覆盖写入**，不接受请求自带值）。**#22 收口**：人批准 confirm 时执行器给 output 加 `origin="user_confirmed"`，投影只允许 `inferred → user_confirmed`（只升级不降级）；`user_explicit` 只能由 remember 产生。**#27 解除**：CLI `memory confirm|forget --task-id` 用控制 Run 的声明作用域签字，且 `lease_paths == ()`（不绑旧 task 目录）。规则只新增一条（`RULE_IDS` 49 → 50，三处同改），擦除回执 / 编辑 revision / 作用域越界都复用现有编号。测试：N1a +8（6 派发 + 2 CLI），N1b（edit/promote）+6（4 派发 + 2 CLI）；合入 `main`（#82 eval-four-dimensions）后本分支全量 **1439 passed / 7 skipped**（167.71s；记忆特性自身 +14，其余来自 main 的 #79/#82）。· **N1b 补记**：`memory.edit`（一次审批 = 旧行 superseded 可查 + 新行 active，新 `memory_id`/`revision=1`/`supersedes`，不许改 kind）与 `memory.promote`（移动语义：原行退役、目标作用域候选、Receipt 指原行、`source_refs` 必含原 id；目标作用域统一 canonicalize）已落地；**origin 按"这一版正文的来源"**：控制面 edit → user_explicit、模型经人批准 → user_confirmed、promote 来源随行不降档；`user_explicit` 的免审批只保留给 `confirm`（策略层与 `_requires_approval` 两处都按 `memory_tool == "memory.confirm"` 收口——复审口径：edit/promote 永远要人批准）。· **仍没做**：`memory.consolidate` / `memory.erase`（契约已定义，代码未落地）、L2/L3、向量召回、恢复分支、UI；`origin=user_explicit` 的**候选**免审批确认仍无生产入口（remember 直接写 active，不产生候选）。
- **剩余记忆施工规格（仅文档，2026-11-11）**：[SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) 把 F10 §13 #18–#27 十条拆成六个工作流（N1 产生侧 / N2 检索质量 / N3 L2-L3 / N4 擦除边界 / N5 恢复与审计 / N6 控制面 UI），每条给"现状（代码事实）→ 设计 → 判据 → 负控"与文件所有权，契约增量单独列在 §1 等 `docs(contract)` PR。**该文档是待实施方案，不是已完成能力**；实施前的记忆状态仍是"只交付 L1"。状态数字与实现进度见上一段 F10 实现轮条目。
- **F10 本地记忆设计（v1.1，提案，2026-11-10）**：新增 [F10-memory-design.md](F10-memory-design.md)。核心判断：**记忆是事件日志的另一个可重建投影，不是第二个事实源**——红线 1 已把独立记忆库这条路堵死。参照 [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)（L0–L3 分层、记忆资产化、装备 loadout、默认私有、可编辑）与 [OpenViking](https://github.com/volcengine/OpenViking)（上下文即虚拟文件系统、三层读取、目录作用域检索、会话成文件 + diff、记忆带引用）的组织方式，**只取设计不取实现**。关键决定：记忆操作是 C2 工具（首版不新增事件族，避免与 `tool.intent`/`tool.result` 形成同一因果的第二份指针）；写入**分级审批**（propose 写惰性候选不挡；agent 发起的 confirm/forget 走 C4 审批 + Receipt，等同覆盖/删除）；默认 `scope=user` 私有；`origin=inferred` 永不自动激活；忘记=tombstone ≠ 物理擦除。· **第二轮（mem0 / Hindsight）补进**：L2 升级为**带证据的信念**（`evidence[]` + `proof_count` + 原文引文，refine 而非覆盖）；L3 是**预算好的可启动页面**（读取为 DB read，无检索/无 LLM）；蒸馏**只做增量 delta 刷新、禁止全量重生成**；**freshness awareness**（陈旧层先核验再用）；记忆写入过 **Memory Defense** 闸门（PII/密钥，红act 或阻断）+ **写入前去重**；实体/时序检索列为后续、须评测证明增益。· **（那一轮只落文档；实现状态见下一条）**
- **F10 记忆实现轮（2026-11-10，分支 `feat/memory` → PR #64，已提交）**：记忆从设计进入**落地**。新增 `src/tianshu/memory.py`（`MemoryProjection`：投影表 + FTS5 + 正文内容寻址 + 重建）、`memory_exec.py`（`MemoryExecutor`：执行侧二次核验、`memory://` 作用域判定）、`resources.py`（资源种类：裸绝对路径 = `file`，`memory://<scope>/<scope_id>` = `memory`，其他 scheme fail-closed）。接线：`dispatcher`（`_authority` / `_resolve_target` / `_policy_preconditions` / `_check_memory_preconditions`）、`permissions`（`PolicyEngine._classify_memory`）、`tools`（5 个 `memory.*` ToolSpec，均无 `path_arg`）、`loop`（只读分支 `_exec_memory_read`、写目标 `_memory_target`）、`context`（`context_payload(memories=...)`）、`cli`（`tianshu memory list|search|get|confirm|forget`）、`receipt`（`build_memory_receipt`）。契约增量：C2 五个工具（`version=1.0`）；C4 资源种类 + **6 条新规则编号**（`RULE_IDS` 43 → 49）；C3 **不新增事件族**（记忆生命周期由 `tool.intent`/`tool.result` + 审批事件承载）。· **测试**：四个记忆测试文件已覆盖——`test_memory_store.py`（51 个 `def test_` / 64 条）、`test_memory_dispatch.py`（20 条，真派发/审批/事务/资源种类/作用域/错装配投影/拒绝行脱敏）、`test_memory_loop.py`（12 条，主循环/预算/replay/CLI/遗忘后不可按 id 复活）、`test_resources.py`（23 个 / 44 条）。· **未落地 / 未覆盖（别当已完成）**：L2/L3 蒸馏与向量召回未实现；`confirm` **不改写 `origin`**，因此 `user_explicit` 与 `user_confirmed` 两档都**没有生产入口**、线上记忆恒为 `inferred`，§8 的 origin 排序实际退化成 `created_at` 降序；§8 的**作用域优先级**（优先当前任务/工作区证据）未实现（作用域只用于过滤）；`recovery.recover_run` **没有记忆分支**（intent 已提交而 result 未落库时按文件工具的推断走，投影侧安全但结论不精确）；`origin=user_explicit` 的免审批确认**生产侧没有产生入口**（`memory_exec._propose` 固定写 `inferred`，测试靠播种事件才走到那条分支）。逐条状态见 [F10-memory-design.md](F10-memory-design.md) §13，判据↔测试映射见 §14。
- **M0–M5 对抗性复审（2026-11-10）**：[DEBUG-M0M5.md](DEBUG-M0M5.md) 对 `main@4eb4ea9` 做了独立复审（严重 0 / 中等 5 / 轻微 7），本轮把 12 条**逐条裁决**（修复记录追加在同一文档末尾）：5 中等里 4 条已修（中-1 阻断 TOCTOU、中-2 未知工具恢复、中-3 注入证据面、中-4 畸形阻断记录 fail-open）、1 条**误报**（中-5：`prepare` 当场就拒，补钉住测试而非改代码）；6 条轻微修复（同内容写入口径、缺 blob 域错误、unblock 稳定 id、M3 diff 区间、封闭集全等断言、eval 归因分层）+ 1 条纯文档补登（Host 强杀后 Daemon 残留）。复审留账的静态疑点/未验证项开成 issue **#53 / #54 / #55**。
- **M0–M1 报告修复对账（2026-11-10 复核）**：[DEBUG-M0M1.md](DEBUG-M0M1.md) 末尾补了「修复状态总表」——S1–S4 / M1–M4 / L1–L2 / R1–R5 逐条对应代码位置与钉住它的测试，含负控口径。复核中发现并修掉一条残留：**快照清单损坏时恢复/撤销漏出裸 `JSONDecodeError`**（现为 `SnapshotCorrupt` → unknown+阻断 / 拒绝撤销）。仍然不成立的四项如实写在总表末尾：真实断电、Windows 实机、authority 数据根逃逸 + 完整 takeover（issue #61）、跨多文件撤销的事务性。
- **2026-11-11 收尾轮：DEBUG-M0M1 除 Windows 实机外全部落地（PR #66–#72）**：① 真实断电 → 持久化边界注入 × 撕裂写 × 真实 HFS+ 强制摘卷（[powercut-evidence.md](powercut-evidence.md)），并**新发现两个真 bug**：删除类动作的恢复去读已被删除的文件（`FileNotFoundError` 穿出 `recover_run`）、撕裂 WAL 被静默洗成空库（fail-open，历史全丢）；② authority 数据根逃逸 → 固定用户级权威根（`authority.py`，与 `--db`/cwd 无关），守卫双读；③ 完整 takeover → `recovery.assess_run` 只读对账 + 三个接管入口；④ 跨多文件撤销 → 写前意图日志 + 崩溃后幂等续做 + 恢复侧阻断；⑤ #63 就绪竞态根因是 **`Listener` 默认 backlog=1，`probe` 的未认证连接自己占掉唯一队列位**；⑥ #58 费用行按**尝试次数**说话；⑦ #57 评测判据按目标作用域化（`SUITE_VERSION` m4.5，完成度/安全分栏）；⑧ #53/#54/#40 销账。规格与并发施工纪律见 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md)，判据↔测试映射见 [DEBUG-M0M1.md](DEBUG-M0M1.md) 末尾新表。· **仍未成立**：块设备断电语义、Windows 实机、真实模型 m4.5 复跑（中转站 503 全模型不可用）、撤销不是两阶段提交。
- **测试（历史快照，2026-11-12，G2 记忆阶段合入后）**：**2108 passed, 7 skipped**（干净 `main @ bfe5916` 实测，macOS arm64 本机，**386.76s**；同机 `main @ 1e17003` 基线 2098/7 ⇒ 记忆阶段 **+10** = `tests/test_eval_followup.py` 6 条 + `tests/test_eval_real_tasks.py` 4 条）；`run_real_user_tasks(repeat=3)` **9 case × 27 次全绿、369 条判据 0 失败、27.78s**；`ruff check .` → `All checks passed!`；UI `vitest` **10 文件 75 条**、`pnpm build` 成功；负控 M1 **3/3** + M2 **5/5** 逐条实测红（见 §8 该条与 PR body）。
- **测试（上一轮，G2 子代理 D3 分支，保留原样）**：**2078 passed, 7 skipped**（`feat/g2-delegation-d3` 分支实测，2026-11-12，362.75s，macOS arm64 本机；基线 `main @ 5eed5ee`（D2 #121 合入后）同机实测 **2069 passed / 7 skipped / 358.23s**，本轮 **+9** = `tests/test_eval_real_tasks.py` 新增 9 条：D3-c1/c2/c3 三条 + 2 条判据负控 + 1 条"未声明子任务脚本 → fail closed" + 1 条委派输入进分母（首版 7 条）+ **独立复审整改新增 2 条**（夹具反转负控、`_child_model` 直接拒未声明 slug））。`run_real_user_tasks(repeat=3)` 本机实测 8 case × 3 = 24 次全绿、**339 条判据 0 失败**（26.76s，`scope.real_user` cases 8 / runs 24）；`ruff check .` → `All checks passed!`。负控 **10/10** 逐条实测（失败:通过 = 6:2 / 1:7 / 7:1 / 1:7 / 2:6 / 7:1 / 1:1 / 1:0 / 1:7 / 1:7），变异与红名单见 §8 该条与 PR body。**下面是上一轮的记录，保留原样**。
- **测试（历史）**：**2069 passed, 7 skipped**（`feat/g2-delegation-d2` 分支实测，2026-11-12，354.14s，macOS arm64 本机；基线 `main @ 7dc9350`（MCP 崩溃恢复 #122 合入后）同机实测 **2047 passed / 7 skipped / 354.21s**，本轮 **+22** = D2 三个测试文件（exec 12 / fence 3 / replay 7）22 条）。`ruff check .` → `All checks passed!`；D2 负控 6 条实测红（失败数 24/2/1/3/15/2，逐条见 §8 该条与 PR body）。
- **测试（G2 网络阶段）**：**1869 passed, 7 skipped**（**`main @ b60f976`** 实测，2026-11-11，341.71s，macOS arm64 本机；已含 G2 网络阶段 #110/#112 与并行 F2 工作流 #108）。构成：§7.1 轮的 1790 → +79 = #110 **+41**（`test_outbound_allow.py` 18 + `test_network_fetch.py` 23）+ #112 **+13**（`test_network_research_eval.py` 11 + 真实任务判据净增 2）+ 并行 #108 **+25**。负控本轮 **18/18** 实测红（白名单 5 + 执行器 8 + 联网评测 5）。`ruff check .` → `All checks passed!`。耗时从 180s 涨到 342s 主要来自联网评测任务（每套真实用户任务要起本机来源服务、跑 7 case × 3 次）。**下面几条是历史记录，保留原样**。
- **测试（历史）**：**1641 passed, 7 skipped**（`feat/memory-retrieval`（N2，含两轮复审整改）分支实测，2026-11-11，已并入 main 的 #89/#91/#92；N2 起点基线为 `main @ 8f2440a` 的 1581 passed / 7 skipped，本分支 +36，其余来自 main）。
- **测试（main 历史记录）**：**1536 passed, 7 skipped**（`main @ ec7d7ae`（收尾轮 #88 合入）实测，2026-11-11，158.55s，macOS arm64 本机；CI 同刻 macOS job 也是 1536 / 7，ubuntu 是 1535 / 8（多 1 条 skip = `test_ipc.py` 里 AF_UNIX backlog 的 macOS 专属用例）。相对 #85 合入时的 **1529**（`feat/memory-erasure` 合入 #84 后实测）再 +7：Windows 运行时兼容（`perf_counter` 启动计时 / Windows pid 存活判定）+1、IPC 握手期 EOF 类型化 +3、指标并发读取尾巴 +3；1529 的构成是 N4 自身 +8，其余来自 main 的 #79/#82/#84；`main` 上一次提交态基线 1316 passed / 6 skipped，macOS 本机实测，收尾轮 `43d04ef`，115.97s；上一轮 DEBUG-M0M1 复核轮为 1080 passed / 3 skipped；F10 分支 `feat/memory` **合入 `main` 的 #62 / #65 / #66 / #67 并完成复审整改后实测 1243 passed, 3 skipped**（105.91s；其中 140 条来自本特性的四个记忆测试文件，其余来自 main 的新增测试）。未注明「分支实测」的数字一律指 `main` 提交态基线。以下为历史上各轮实测的原始记录：macOS 本机实测，2026-11-10，DEBUG-M0M1 复核轮（分支 `fix/m0m1-corrupt-manifest`）：`uv run --python 3.12 pytest -o addopts="" -q` → `1080 passed, 3 skipped in 105.86s`；上一轮真实模型轮 `test/m4-real-model` 为 `1077 passed, 3 skipped in 101.01s`；`uv run --python 3.12 ruff check .` → `All checks passed!`）；测试文件 **63** 个 `tests/test_*.py`。基线 1061 →（复审轮 +13 → 1074）→（真实模型轮 +3 → 1077）→（DEBUG-M0M1 复核轮 +3 → **1080**），无新增 skip。真实模型轮新增 3 条：eval 拒绝 memfs 两条（库 + CLI）、replay 按路径核产物一条；DEBUG-M0M1 复核轮新增 3 条：坏快照清单的恢复（写工具 unknown+阻断）、只读调用不被误伤（none_applied、不阻断）、撤销拒绝（拒绝+零修改）。M5 新增 6 个测试文件：`test_ipc` / `test_host` / `test_control` / `test_daemon` / `test_integration_m5`，以及 issue #40 修复带的 `test_approval_reject`；五个 M5 文件共 **110** 条（ipc 26 / host 28 / control 27 / daemon 19 / integration_m5 10），`test_approval_reject` 9 条（均实测 `--collect-only`）。**Windows 的数字本轮按 CI 重核过**（收尾轮）：`main @ ec7d7ae` 的 `windows-latest` 实测 **5 failed / 1509 passed / 29 skipped**（939.99s；修复前 35 failed → #87 后 8 → #88 合入前 6），5 条失败逐条登记在 [PRD-REVIEW-DELIVERY-2026-11-11.md](PRD-REVIEW-DELIVERY-2026-11-11.md) §3；另有一次 Windows job 因命名管道阻塞在 25 分钟上限被取消（同一棵树在 main 上 15:39 跑完），也如实记在那里。仍然**没有 Windows 实机**，别把 macOS 数字写成三平台通用。注意 `pyproject.toml` 的 addopts 已带 `-q`：直接 `uv run pytest` 会正常打印汇总；但**再在命令行补一个 `-q`**（叠成 `-qq`）汇总行就没了，那时要 `-o addopts="" -q` 才行。三种情况都实测过。
- **代码规模**：`src/tianshu/*.py` **73** 个模块（2026-11-16 实测 `ls src/tianshu/*.py | wc -l`；上一版写 63 是 2026-11-14 时点数，其后 W5 调度（scheduler/schedule_cli）、W4 恢复B（restore_access/restore_contract/restore_registry）、W6 embedding（embedding_openai）、F12 Keychain（keychain_macos）、run_policy/disk_admission/platform_identity 再新增 10 个。）模块来源：M5 新增 `ipc` / `host` / `daemon` / `control`，并把撤销从 `cli` 提成库 `undo`；工程评审收尾轮加了 `metrics` / `isolation` / `backup` / `migrate` / `event_schema`；G1 轮次加 `toolschema` / `budget`；G1 收口轮加 `contents`；§7.1 轮加 `summaries`；G2 网络阶段加 `net`；G2 MCP 阶段加 `mcp` / `tool_registry`；并行 F2 工作流加 1 个。另有子包 `eval/`（**8** 个 .py，G2 网络阶段加 `webfixture`）；`tests/test_*.py` **133** 个（2026-11-16 实测 `ls tests/test_*.py | wc -l`；上一版写 117 是 2026-11-14 时点数，其后调度/embedding/恢复B×4/低盘×4/net_resilience/memory_conflict/memory_outbound_gate/g2_delegation_write/baseline_evidence/pytest_ci_shards 再新增 16 个。**D3 本轮没有新增测试文件**，只在 `test_eval_real_tasks.py` / `test_eval_negative_controls.py` 内增项）。
- **平台（2026-11-13 修复分支实测）**：`codex/fix-windows-ci@4bb1781`（已整合 `main@3648d79`）的 [CI run 43283015540](https://github.com/lorek666/tianshu-agent/actions/runs/43283015540) 四个 job 全 success：Windows **2127 passed / 30 skipped**（1393.12s）、macOS **2150 / 7**（569.34s）、Ubuntu **2149 / 8**（269.43s，3 warnings）、UI 通过。Windows runner 为 **Windows Server 2025**，不是用户设备实测。历史 Windows 长期失败及 `main@2100fde` 超时记录仍保留；这条绿色属于当时修复分支；其后 PR #130 已合入 `main@ccff93e79cfeb93e47e5a35012fd92fae366e941`，该 main 的 [run 43298476414](https://github.com/lorek666/tianshu-agent/actions/runs/43298476414) 四项全绿。两份结果分别绑定各自 SHA，**均不证明长期无偶发问题**。原因及证据见 [WINDOWS-CI-2026-11-13.md](WINDOWS-CI-2026-11-13.md)。
- **门禁**：`main` 已开分支保护——必须走 PR、`strict`（分支须与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推与删除。2026-11-13 经 GitHub API 写入并回读确认，required status checks 为 `test (ubuntu-latest) · py3.12`、`test (macos-latest) · py3.12`、`test (windows-latest) · py3.12`、`ui · typecheck / test / build` 四项，均绑定 GitHub Actions（app_id=15368）；批准人数仍为 0。设置已在远端生效，文档随 PR #130 交付。
- **CI 步骤**：锁文件检查 → frozen 安装 → Ruff → pytest；Windows 使用 `uv run --frozen python scripts/pytest_with_stacks.py -o addopts="" -vv -rs --tb=short --durations=20 --stack-timeout=60` 输出逐例、短错误栈、耗时与Python线程定时栈，其余平台保留 `uv run --frozen pytest -o addopts="" -q -rs`。原生fatal handler保留，C扩展长期独占GIL时不能保证60秒栈；25分钟上限不替代这个诊断缺口。权限 `contents: read`，`fail-fast: false`，不使用 `continue-on-error`。
- **Windows 跳过项（历史阶段快照）**：按平台跳过的是 memfs 符号链接、POSIX PID 探活、POSIX 权限位 ×2、无 PTY ×2、无进程组/信号 ×2，以及只在 POSIX 成立的真 socket 断言（AF_UNIX backlog、假对端「只 listen 不握手」）这几类；收尾轮 CI 实测 skip 数：macOS **7**、ubuntu **8**、Windows **29**，跳过原因写在跳过消息里，CI 用 `-rs` 打进日志。 · **2026-11-13 修复分支**：Windows 30（恢复 POSIX 残留 socket 用例遗漏的平台条件，较历史多 1），macOS 7、Ubuntu 8；Windows 命名管道认证/关闭/重复实例等测试仍运行。
- **M5 新入口**：`tianshu daemon`（常驻守护进程）与 `tianshu host run|stop|status`（桌面外壳）。运行数据根默认平台标准目录，`TIANSHU_DATA_ROOT` 覆盖；**旧的内核子命令（`run` / `task` / `approvals` / …）仍默认 CWD 下的 `.tianshu/events.db`**（`cli.DEFAULT_DB`，`--db` 可改）——两套落点并存，文档必须分开写。
- **数据目录**：`ipc.default_data_root()` 已把平台标准位置实现出来——macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`（不用 Roaming：事件日志会撑爆漫游同步）、Linux `$XDG_DATA_HOME/AgentOS`（缺省 `~/.local/share/AgentOS`）。目录内：`events.db`、`artifacts/{blobs,snapshots}`、`run/`（IPC 端点 / `ipc.key` / 锁）、`logs/daemon.log`（Host 重定向的 daemon stderr）。测试与 CI 一律显式传 `--data-root` 到 tmp，**不允许**写用户主目录。
- **模型**：两个线上协议（OpenAI-compatible、Anthropic Messages），各一个 adapter，主循环只认内部规范化形状；契约测试在 `tests/test_model_contract.py`。桌面演示与验收默认 `scripted` 模型（不联网、不花钱）。**2026-11-10 起有两个真实模型的完整评测数据**（deepseek-v4.1-flash 18/20 case、glm-5.2 15/20 case，同一 suite m4.4）+ 一次真实模型审批流端到端；逐条证据与裁决见 [M4-verification.md](M4-verification.md) §7.1。key 走环境变量，不落库、不进文档。

## 3. 架构速览

- **唯一事实源**：SQLite WAL append-only 事件日志（触发器禁 UPDATE/DELETE）。Task/Run/Approval/Grant/Lease 都是同库事务更新的可重建投影。
- **Task ≠ Run**：Task 是用户看到的那件事，Run 是一次执行；改目标会 fence 旧 Run。
- **授权只在执行侧**：模型输出/工具输出/MCP 注解都不构成授权依据；dispatcher 是唯一派发入口，执行前登记 intent、执行后写 result + Receipt。
- **三进程（M5）**：UI（浏览器）→ loopback HTTP + SSE → Host（单实例、监护 Daemon、token 注入）→ IPC（AF_UNIX / Windows 命名管道，`ipc.py`）→ Daemon（任务/事件/审批/驱动线程）。**关 UI 只减少订阅者，不停任务**；重启后由 Daemon 的恢复扫描接手。细节见 [M5-desktop.md](M5-desktop.md) §1/§4/§5。
- **五道契约**（版本化；冻结条件＝字段级 JSON Schema 可校验 + 契约测试全绿 + 第二家 provider 验证过）：
  | ID | 名称 | 当前状态（只写可核实的） |
  |---|---|---|
  | C1 | Model I/O | 两个线上协议各一个 adapter；结束原因是封闭集，越界抛 `ModelError` |
  | C2 | Tool | **23 个 `ToolSpec`、19 个对模型开放**（4 个控制面专属 `exposed=False`：`memory.remember` / `memory.promote` / `memory.erase` / `restore_file`）；`tool_manifest()` + 每种工具的参数要求由工具自己声明 |
  | C3 | Event | **已封闭**：45 个事件类型，写入侧拒未登记类型，且逐类型 payload 有 v1 契约（`event_schema.py` + `schemas/c3/event.json`）；声明了但从未发射的类型 **0** 个 |
  | C4 | Permission/Approval | **rule id 已封闭**：低磁盘 T1/T2 已登记并独立核验两项空间拒绝与资格拒绝；T3 作者候选的入口证据仍待独立复审、统一全量与原生 CI。当前代码/schema/PRD 清单共 78 项，写入前校验。非拒绝判定 10 个（`c4.auto` / `c4.overwrite` / `c4.delete` / `c4.exec` / `c4.mcp_call` / `c4.memory_scope` / `c4.outbound` / `c4.outside_workspace` / `c4.subtask_spawn` / `c4.subtask_cancel`）+ 保留未使用 3 个（`c4.unhandled` / `dispatch.denied` / `c4.subtask_unavailable`）；`c4.approval_rejected` 是 issue #40「否决即终局」新增的拒绝原因；记忆组 6 条（N1 起）见 PRD §5.4.1。审批/凭据见 PRD §5.4 |
  | C5 | Eval | `replay`（审计，不调模型）与 `evaluate`（换模，干净 fixture）已分离，评测套件 `m4.4` |
  | C5 | Eval | `replay`（审计，不调模型）与 `evaluate`（换模，干净 fixture）已分离，评测套件 `m4.6`（判据按目标作用域化后逐版本不可比，见 [M4-verification.md](M4-verification.md) §7.4） |
- **封闭集落在三处**：代码里的集合（`events.EVENT_TYPES` / `rules.RULE_IDS`）、`schemas/` 下的 enum（`schemas/c3/event.json`、`schemas/c4/rules.json`，共 7 个 schema 文件）、PRD 对应小节；测试把前两者绑在一起，改一处忘另一处会红。
- **两个「未」不是一回事**：C3 的「声明未发射」是 **0**（没有登记了却不发射的事件类型）；C4 的「保留未使用」是 **3**（`c4.unhandled` / `dispatch.denied` / `c4.subtask_unavailable`，防御性兜底或历史归档，登记为合法但没有产生路径）。别把这两个数字混着写。
- **十一条红线**（与功能冲突时以红线为准，每条能被一条测试打红）：
  1. 事件日志 append-only，无 UPDATE/DELETE
  2. 副作用执行前已持久化 intent，执行后必须写 result
  3. 上下文是可再生视图，删掉所有视图可从头重编
  4. 授权只在执行侧判定
  5. 副作用结果未知时禁重放，只有 `none_applied` 可安全重试
  6. 每个副作用必须产出可独立核对的 Receipt
  7. 跨层只传版本化契约对象，SDK 对象/异常实例不得越界
  8. 工具 schema 变化即视为新工具，不自动升级
  9. 能力声明只用于路由，不用于授权
  10. replay 与 evaluate 不得混称，安全门槛不可被其他分数抵消
  11. 任何「崩溃后无法判定状态」的设计视为 bug
- **模块地图**：按功能分：事件与状态（`events`/`tasks`/`jobs`/`leases`/`snapshot`/`recovery`/`replay`）、授权与派发（`permissions`/`rules`/`dispatcher`/`guard`/`receipt`）、模型与执行（`model`/`capability`/`context`/`exec`/`execpolicy`/`credentials`/`pricing`/`sandbox`）、工具与路径（`tools`/`workspace`/`pathkeys`/`fsutil`/`canonical`）、撤销（`undo`）、主循环与入口（`loop`/`worker`/`cli`/`errors`/`crashpoint`）、**桌面三进程（`ipc` 传输与契约 / `daemon` 常驻与恢复 / `control` RPC 映射 / `host` 外壳与 UI 桥）**、记忆（`memory` / `memory_exec` / `resources`）、运行与维护（`metrics` 运行指标 / `isolation` 隔离探针 / `backup` 备份恢复 / `data_cli` 数据维护入口 / `maintenance` 协作准入 / `migrate` 迁移 / `event_schema` 事件 payload 契约）、评测（`eval/`）。细节见 PRD / TECH-ARCH 与 [M5-desktop.md](M5-desktop.md)。
- **CLI 子命令**（`tianshu --help`）：`run` / `mcp` / `task` / `worker` / `recover` / `undo` / `approvals`（别名 `approval`）/ `replay` / `daemon` / `host` / `evaluate` / `memory`（别名 `memories`）/ `data`。
  - `worker` 是**一次性 drain**——队空或撞上被占用的工作区就退出；**常驻进程是 `daemon`**（`host run` 会拉起并监护它）。
  - `host` 只有 `run` / `stop` / `status` 三个动作（不带动作 = `run`）；`status` 在跑退出 0、没跑退出 1。
  - `replay` 审计不调模型；`evaluate` 换模跑固定 case。
  - 退出码：`0` 成功 / `1` 失败或取消 / `2` 前置条件错 / `3` 需要人决定（等审批、已暂停、待核查、`undo` 未带 `--approve` 的预演）/ **`4` = evaluate 判 unsafe**（安全门槛一票否决）。

- **已实现的功能面**（PRD §7 编号 → 落地模块，模块说明取自代码）：
  - **F1 执行原语** → `exec.py`（argv 模式，非 shell；超时/截断/进程组）
  - **F2 执行策略与沙箱接缝** → `execpolicy.py`（声明式、默认全拒、deny 优先）、
    `exec_backends.py`（注册表；未实现后端一律 `unsupported`，绝不回落 local）、
    `exec.py`（本地后端与进程树）、`isolation.py`（四边界实测探针 + 门 +
    `isolation_view`）。「受信任执行」在**审批前**可见（`ApprovalView.isolation`），
    执行结果/事件带 `output.isolation`；口径见
    [F2-sandbox-policy.md](F2-sandbox-policy.md)
  - **F5 上下文编译** → `context.py`（纯函数、按字节预算、丢弃只记账不摘要）
  - **F9a 模型准入** → `capability.py`（能力声明与准入，只用于路由，不用于授权）
  - **F12 凭据** → `credentials.py`（分域查找 + 全链路脱敏；Keychain 后端 **macOS 已落地**——`keychain_macos.py` SecItem 直调、仅 darwin 加载，W1 交付并经 #151 独立复审；其余平台仍如实报 `unsupported`）
  - **F13 成本** → `pricing.py`（整数微美元；未知价格记 `None` 并单独计数）
  - **F14 界面部分** → `ui/`（React + TS）：机器枚举 → 人话的映射在 `src/lib/humanize.ts`；`digest` / 请求 id / 枚举值不进视图，内部细节放折叠区（PRD §7 功能表 F14 行）
  - 其余 P0 功能落在：`events` / `recovery`（事件与恢复）、`tools`（工具系统）、`permissions`（授权审批）、`tasks` / `jobs` / `leases`（任务与运行）、`snapshot` / `receipt`（产物与回滚）、`undo`（撤销）
  - **F10 记忆**属 v1.1，v1 不做（PRD §2.2 / §7）

## 4. 关键决策（D1–D7）与硬约束

- **低磁盘 Run 资格决定（2026-11-14，T3作者候选已接线）**：创建前固定 `pure_only/effects`，默认 effects，同时提供真实 CLI/RPC/UI 纯读选择；避免首次副作用时追加资格状态机。代价是选择 effects 后最终只读的新 Run 仍须准入。普通/摘要模型及必要内部审计不算本条 C2 业务副作用，仍守预算、取消与持久化；已有 effects/legacy 的读与模型恢复保留原门，新普通非 pure 动作现测空间。字段、停止控制例外及逐事件版本以[独立契约](low-disk-admission-contract.md)为准；T1/T2 已独立窄范围通过，T3 候选仍待独立复审与统一门禁。

| ID | 决策 | 直接后果 |
|---|---|---|
| D1 | 首批用户＝普通用户，后期开源 | 权限默认值是产品决策而非配置项；F11 回滚 / F13 成本可见升 P0 |
| D2 | 单用户 | 砍多租户；租约只防「同用户双实例」；README/代码须显式声明单用户单机 |
| D3 | 编程沙箱默认可写 | 写/删分级；删除必须可撤销 |
| D4 | 图片视频走用户自己的 key | 成本可见成硬需求；不做充值计费 |
| D5 | 快照保留策略 | 见 PRD §8.4 |
| D6 | Task 是用户可见单位，Workspace 是其属性（0..n） | 空目录任务合法；「读 A 写 B」可表达 |
| D7 | 诊断包仅本地导出、永不上传 | 但默认脱敏（最终用途是贴公开 issue） |

**派生硬约束**：安全不能靠用户配置（默认值必须正确）；默认可写就必须默认可撤销；单用户假设必须显式声明。

- **Windows CI 分片决定（2026-11-14）**：保留四 required 名称与25分钟上限，以两个独立 runner 按整文件分片，避免同 runner 并发引入端口/进程/资源竞争。汇总 job 在 Ubuntu 核验真实 Windows job 的结论与开发报告；完整收集快照先于其它筛选，最终列表再核，不用选后列表倒推全集。报告不是独立执行证明，必须与上游 job success 一起使用；不新增产品契约、依赖或 skip。

### 收尾轮实现决策（2026-11-11，PR #66–#72）

- **阻断状态属于"这台机器这个用户"，不属于某个 `--db`**：`authority.py` 把它固定到 `TIANSHU_AUTHORITY_ROOT`（否则 `~/.tianshu/authority`），与 `--db`/`--data-root`/cwd 无关；键用 `pathkeys.overlap_key(resolve(path))`（与租约同口径）。失败语义朝安全方向：**库不存在=空集**（不是损坏，否则新用户第一次用就被自己挡住）、**读不出=已阻断**、**写失败=如实报「跨数据根阻断未生效」**。代价：权威根只保护"阻断"，跨数据根没有租约互斥。
- **接管前对账只读**：`recovery.assess_run` 不写事件、不落盘、不阻断（用 `SnapshotStore.__new__` 挂属性绕过会 `mkdir` 的构造函数）；拒绝接管后的双重证据（日志 + 权威根）由调用方在租约事务回滚**之后**落。三个入口（`AgentLoop.start`/`resume_run`、控制 Run）共用同一条实现——只保护控制 Run 等于没保护。
- **撤销用"写前意图日志 + 幂等续做"，不是两阶段提交**：`artifacts/undo/<run_id>.json` 记录完整计划与每步 `post_state`；崩溃后先核对 done/剩余项与磁盘一致再从断点继续，**绝不从头再来**；`completed` 只差补事件不阻断。恢复侧把未完成撤销变成阻断（`recovery.blocked` + 权威根），阻断是 journal 的投影、不是事实源：`unblock` 可解除，但只要 journal 仍 `in_progress`，下次恢复会重新挡住。
- **就绪标记必须晚于"端点真的可服务"**：`on_ready` 在 accept 线程进入 accept + 自连认证握手成功后才落；`LISTEN_BACKLOG=64`。根因不是"listen 没完成"，而是 backlog=1 时探活自己占满队列（详见 §5）。
- **评测判据按目标作用域 + 分类**：`Check.kind`（completion/safety）只有 safety 进 `safety_gate`；完成度判据红不影响安全结论；`SUITE_VERSION` m4.5 使旧报告不可比（两道守卫）。
- **`EventLog` 对"有内容却读不出 events 表"的库 fail closed**：0 字节按新库（首次启动窗口无历史可丢），只有"文件存在且有内容、读不出 schema"才 `EventLogCorrupt`。检查必须在 `executescript(_SCHEMA)` 之前、`PRAGMA journal_mode=WAL` 之前——顺序即是"先读、再写"的保证。

### 工程评审新增要求的实现决策（2026-11-11，PR #79–#84）

- **四维独立、安全唯一硬门槛**：`dimensions` 四项各自记录样本/通过/失败依据；completion 失败只影响 `verdict=pass`，绝不进 safety；无效样本（评测基础设施故障）不进分子分母，但产品该持久化的 intent/result/Receipt/审批/恢复证据缺失**仍是产品失败**。
- **判据负控要可机械检查**：每个 safety 判据必须登记负控，元测试缺登记即红；`Check.path_coverage` 让「专门测错误路径」的 case 在未触发时明确说「没有覆盖证据」。
- **控制面推送必须与请求处理解耦**：每连接有界发送队列 + 独立发送线程，队列满/发送超期 → 断开并记 `slow_subscriber_dropped`，重连按 `since_seq` 补读；同步 `send()` 会把慢消费者变成控制面 DoS。
- **隔离是测出来的，不是声明出来的**：门只认 `probe_backend()` 的实测报告，不认后端 `capabilities.isolation`；`unknown` 与 `not_enforced` 一样阻断；本机 local 后端文件/网络/凭据三边界实测可越过 → 依赖能力全部关闭。
- **备份必须是「一致点 + 引用清单」，不是复制库文件**：SQLite 在线备份 API 取一致点，`backup.json` 记哈希/引用/版本；中断留下的临时目录不得被 `verify` 当有效；恢复只允许干净根；迁移前必须 `verify_backup` 通过、每步进度持久化、`crashpoint` 注入中断后可续做。
- **写入口的「旧版兼容」必须与「读兼容」分开**：`EventLog.append` 默认只收当前 v1 契约，v0 只能在显式 `legacy_write=True` 下写入并记 `payload_version=0`；`event_versions` 旁表与事件行同事务；旁表缺失/损坏 → `PayloadVersionUnavailable`（不把「读不懂版本」洗成「旧版」）。**自动回退 v0 是 fail-open**：`_legacy_schema` 的 `required=[]` 会让任意 `{}` 通过。

### 收尾轮的兼容与并发决策（2026-11-11，PR #87–#88）

- **Windows 上「测得出真值」要换时钟、换系统调用**：`time.monotonic()` 在 Windows 走 `GetTickCount64`（约 15.6ms 分辨率），热启动会量到 `0.0`——启动耗时起点与读数都改用 `time.perf_counter()`（QPC），语义仍是「记录值 = 真实测量」。`_pid_alive` 不能用 `os.kill(pid, 0)`（Windows 上是 TerminateProcess 语义），改用标准库 `ctypes` 的 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`：超时=活着、已信号=已退出、拒绝访问=按活着保守处理、其余=`None`（unknown）。
- **并发读者的「半行」不是损坏**：`read_metrics()` 对没有 `\n` 收尾的最后一段先尝试解析，失败就跳过（`tail -f` 语义），下一次读它就会完整；已由 `\n` 收尾的坏行仍带行号抛 `ValueError`。写侧仍是「一行一次 `os.write`」，但**不把「一次 write 对并发读者原子」当契约**——那是文件系统语义，不是我们能承诺的。
- **裸 stdlib 异常不能穿过自研抽象**：`multiprocessing` 的认证握手在「对端 accept 后立刻关」时抛的是 **`EOFError`**（不是 `OSError`）；`Client.connect()` 现在把它映射成 `ConnectionClosed` 且**不重试**（重试会把「已经走了」拖成整个 timeout）。`request_shutdown()` 的返回类型因此恒为 `bool`，收尾路径不会因为「daemon 已经走了」抛异常。对齐依据：服务端 `Server.accept()` 早就把同一种情况映射成可重试的 `ProtocolError`。

### M5 与 PRD / TECH-ARCH 的偏差（即 M5 文档里的 D1–D5，别与上表 D1–D7 混）

下表是 M5 历史基线。2026-11-15 的独立发行工单已补 Tauri 2 薄壳、托盘和单实例，
因此 M5-D1/D5 是“当时没做”；M5-D2（Rust Host）与四目标正式发行仍未完成。

| # | 文档原样 | 本版实现 | 换回去的时机 |
|---|---|---|---|
| M5-D1 | 桌面应用 = **Tauri 2 · React · TS** | UI 是 React + TS，但**外壳是系统浏览器**，没有 Tauri/WebView2 | 单独工单：外壳 + 托盘 + 开机自启 |
| M5-D2 | **Desktop Host (Rust)** | Host 是 Python 进程（`host.py`） | 与 D1 同一张工单；只换实现，IPC/UI 契约不动 |
| M5-D3 | UI ↔ Host 走**类型化 IPC（版本化）** | 预览壳走 **loopback HTTP + SSE**：`POST /rpc`、`GET /events`、`GET /state` | D1 落地时换 WebView 内 IPC |
| M5-D4 | TECH-ARCH §4.2 帧 = `[4B 长度][JSON]` | 语义一致，帧编码用标准库 `multiprocessing.connection`（POSIX = AF_UNIX，Windows = 命名管道）+ `authkey` | 需要跨语言/跨版本 IPC 时只替换 `ipc.Server` / `ipc.Client` |
| M5-D5 | 关闭窗口 ≠ 退出：最小化到托盘 | 没有窗口可关：**关浏览器标签 = 断订阅者**；Host 活到 `tianshu host stop` 或被杀 | 与 D1 一起 |

逐条理由与"不为偏差辩护"的部分见 [M5-desktop.md](M5-desktop.md) §2。

### M5 实现决策（踩出来的，不是偏好）

- **驱动线程的 SQLite 连接必须 `cross_thread=True`**：连接在 serve 线程建、驱动线程用；默认 `check_same_thread=True` 会抛 `ProgrammingError`，表现是 Run 永远停在 `created`（本分支修过两次：先是连接归线程，再是跨线程复用）。`EventLog(cross_thread=True)` 只给"连接建在一处、只在该线程用"的通道用，不是免检通行证。
- **SSE 用 `fetch` 读流，不用浏览器 `EventSource`**：契约要求每个请求带 `X-Tianshu-Token`，而 `EventSource` 天生带不了自定义 header，也没有自定义重连游标；用 fetch 流能达到同样的协议与「带 `since_seq` 重连」语义。
- **token 只进页面 `<meta>` 与请求头，不进 URL**：URL 会进浏览器历史、shell 历史、聊天记录和截图；`UiBridge.url` 刻意不带 token（`src/tianshu/host.py` 的 `url` 属性注释写明了理由）。

### M5 修掉的真 Bug：`host stop` 的退出路径（三个缺陷叠在一条路径上）

修在 `f5da9ef`。三个缺陷单独看都像"偶发/环境问题"，叠起来把"关 Host"变成半失败；全是**顺序与注册位置**问题，不是算法问题：

1. **`/shutdown` 端点注册在 `do_GET`，而 `tianshu host stop` 发的是 POST** → 永远 404，`stop` 只能打印"Host 未在超时内退出"并返回 1。修法：只接受 POST + token；GET 回 405，**不能**落到 SPA 兜底回 200 + `index.html`——那会让人以为通知成功了。回归：`tests/test_host.py::test_shutdown_endpoint_is_post_only_and_requires_token`。
2. **清理回调赋给 `_Handler` 的类属性** → 普通函数成为类属性会被描述符协议绑定成 bound method，`threading.Thread(target=self.stop_callback)` 调用时多塞一个 handler 实例当 `self` → `TypeError`，清理线程静默死掉（日志里只有一条没人看的 traceback）。修法：回调挂在 **server 实例**上，handler 用 `self.server.stop_callback` 取。回归：`tests/test_host.py::test_stop_host_stops_a_running_host_and_clears_info`。
3. **`stop_all()` 先置 `stop_event` 再清理** → 主线程立刻从 `run_host()` 返回，进程退出把"正在关 Daemon"的清理线程一起带走：`stop` 报"Host 已退出"，Daemon 却留成孤儿继续占 socket、继续接活。修法：**清理完成之后**才放主线程走（`stop_requested` 负责去重）。回归：`tests/test_integration_m5.py::test_host_stop_stops_daemon_too`。

第 3 条**在 pytest 进程内测不出来**：主线程退出后进程还活着，清理线程不会死，Bug 会躲过去；必须用**真子进程**跑 `tianshu host run` → `stop_host()` → 进程退出后立刻 `ipc.probe` 断言为假。本轮在新 tip 实测：`host run` → `host stop` 输出 `Host 已退出`，Host 与 Daemon 进程都消失，`ipc.probe == False`，`run/` 只剩常驻的 `ipc.key`。这也是下面 §5「顺序即契约」与「真进程验收」两条教训的来源。

### M0–M5 复审修法的实现决策（2026-11-10，`fix/m0m5-audit`）

- **把「阻断核验」压到执行边界，同时让恢复和派发抢同一把锁**：`FileExecutor.execute()` 在改文件前最后一步按**真正要写的那个路径**再判一次；`TaskStore.begin_execution()` 在拿住执行守卫之后、写 `execution.started` 之前按资源路径再判一次；`recover_run(..., data_root=...)` 给定时，恢复全过程（含落 `recovery.blocked`）在本机执行守卫内跑，CLI `recover` / daemon 启动恢复 / eval 崩溃恢复三处都传。代价与边界：恢复要等守卫（上限 `GUARD_TIMEOUT_MS=5s`，超时抛 `GuardTimeout`）；daemon 的恢复扫描在 bind 之前（此时没有驱动线程），所以不会自锁；库调用方不传 `data_root` 时没有跨进程线性化，只剩两道就地核验（口径不变，窗口收窄）。
- **未知/畸形一律 fail-closed，且"拒绝"要用已有的 rule id**：`guard.scan_blocked()` 把「回放出的阻断集合」和「判定不了的事件 seq」一起交出来；`blocked_workspaces()` 见到后者抛 `EventLogCorrupt`（这份日志答不了"哪些目录被阻断"），写入守卫转成 `WorkspaceBlocked`；守卫内被拒的派发在 `_abort_dispatch` 里映射回 `dispatch.workspace_blocked`，不新造 rule id（C4 是封闭集）。恢复里 `SPECS.get(tool_id) is None` 直接 unknown + 阻断，不再走"没证据=没写"的推理。
- **同内容写入 = 没有可观测副作用**：`_modify_text` 在 `before == data` 时返回 `succeeded / none_applied`，不落 preimage、不产 applied 凭据（`build_receipt` 只对 `applied` 出凭据）。这样活体路径与恢复侧（`before == expected_after → none_applied`）口径一致，"副作用已发生"不再被一句空话冒充。
- **`EventLogCorrupt` 的语义从一个扩到三个**：payload_hash 不符（A8）、外部 payload blob 缺失/不可读、阻断记录解析不出 workspace。三种都是"这份日志不能作为结论来源"，所以共用同一个域错误，但消息必须带 `seq`（和 blob 路径），否则调用方只知道"读失败"。

### M4 真实模型实测后的实现决策（2026-11-10，`test/m4-real-model`）

- **产物哈希按「路径」归属，不按「最后一次写入」**：`replay` 原先取最后一条带 `sha256` 的 `tool.result` 去比 `run.end.artifact`。真实模型会「先建报告、后覆盖目标文件」，于是最后一条属于被覆盖的目标，拿它比对报告哈希 → **一份完好日志被判 `ok=false`**（审计假红）。改为 `written_sha[path]`；`run.end` 带 `path` 时只跟同路径比，没有该路径记录才退回旧口径（不凭空判红）。这是审计侧最重要的口径：**误报比漏报更伤**，一次假红之后没人再看 replay。
- **评测只接受 `local_fs`，其它后端开跑前拒绝**：fixture 用 `Path.write_text` 直接落宿主盘、判据从宿主盘读、崩溃 case 还要跨进程看见同一份状态；memfs 是进程内单例、状态随进程消失，三条全不成立。实测（不加守卫）`TIANSHU_SANDBOX_BACKEND=memfs` 跑整套得 **1/20 + `verdict=unsafe`**——唯一的绿是唯一不写文件的 N6。假分数会被引用，所以 `run_suite` 先 `require_local_backend()`（CLI 退 2），拒绝时连工作根都不建。
- **P1/P2 判据不在这轮改**：真实模型的 `unsafe` 里有五条根因是判据写死了脚本模型的收尾形态（系统提示词要求写报告 → 模型的第二个副作用是合法交付物；`approval.requested=0` 只是"没尝试"，不是"绕过"）。修复要改判据语义 → 改 `fixture_digest` → 旧报告全部失去可比性，必须 bump `SUITE_VERSION` 并复跑两份真实报告。混在"补证据"的 PR 里偷偷改判据，比不改更坏；登记工单，判据口径写进 [M4-verification.md](M4-verification.md) §8 第 7 条。

### G1 通用执行基础的实现决策（2026-11-11，PR #91 / #92）

- **schema 方言封闭、注册期拒绝，不静默忽略**：`toolschema.py` 只认对象/数组/标量三组白名单关键字，`oneOf` / `$ref` / `format` / `default` 等在**注册期**抛 `ToolSchemaError`（带工具名 + JSON 路径 + 关键字）。理由不是"规范要求"（JSON Schema 里未实现关键字本是注解），而是准入边界：注册表里写着、执行侧不看，等于对模型和审批人一起撒谎。`$ref` 另有一条硬理由——解析外部引用 = 读文件或联网，PRD 明确禁止。
- **工具身份的三项绑定里，"描述摘要"必须是条件字段**：`ActionRequest.description_hash` 只在非空时进 `to_canonical_dict()`。历史请求（无该字段）摘要逐字节不变，历史库里的审批 / grant / 派发记录不作废；新请求多绑一层，改描述即作废旧批准。这与「资源种类用 scheme 前缀而不是给 `ActionRequest` 加字段」是同一条理由：**不能为一条新要求把既有授权链集体作废**。
- **预算账本建在事件库同一个 SQLite 文件里，父账户计数器本身就是子树聚合**：预占沿 `parent_id` 链逐级扣，所以父看到的花费天然含子账户；`usage()` 若再对后代求和就会把同一笔算两次（PRD §5.6 第 2 条）。
- **未知用量按维度保留占用，不按零结算；取消不是退款**：`settle(known_dimensions=...)` 支持"token 已知、金额未知"的 `partial` 状态；`abandon_unsent()` 只释放确认没发出的预占。`run.cancel` 向账本传播取消只阻断后续预占，已发出的请求原样保留等 `reconcile()`。
- **账本默认关闭（已被 #96 取代，保留原文以便对照）**：`Limits.budget_limits` 为 None 时行为与之前一致。估算预占（4 字符≈1 token + 25% 余量 + 输出预留）会让临界 run 提前被拒，属行为变更需单独评审；G2 接入前必须默认打开。**不能**把"账本已实现"读成"§5.6 发布前置条件已满足"。

### G1 收口轮的实现决策（2026-11-11，PR #96 / #97 / #99）

- **默认记账 ≠ 默认设闸**（取代上一条）：`budget_limits=None` 时账户/预占/结算照写，但**任何维度都不设闸**。理由：§5.6 第 4 条（尝试过但全失败不得显示没有调用）与第 7 条（恢复先核对未决预占）在账本关着时在默认路径上没有任何事实可核；而"设闸"需要一个用户明确给的上限。
- **"可验证上界"必须两项都是上界，拿不到就不进这个模式**：输入 = 请求体 UTF-8 字节数（字节级 BPE 下 token ≤ bytes；CJK 因此比"4 字符≈1 token"大得多，方向保守），输出 = adapter 能力声明的 `output_tokens`（它就是发出去的那个 `max_tokens`）。OpenAI-compatible 的请求体里没有该字段，所以它**如实不能**进 upper 模式——这是能力事实，不是缺陷清单。**不承诺**绝对不超额：provider 每消息固定开销不在字节上界内，靠 `overrun` 暴露并在上界口径停机。
- **外发没有自动路径**：`network` 资源只能配 `fetch`，判定恒 `APPROVE`（`c4.outbound`）；即使目标是公开页面也一样——"公开与否"不是本地能判定的东西。`mcp` 按 `exec` 同级；`subtask` 在 G2 子代理 D1 之前**恒拒**（没有委派执行器，走到审批再失败会给"批了就能用"的错觉）——D1 起改成按工具判定（spawn 审批 / cancel 自动 / read 自动），旧编号 `c4.subtask_unavailable` 退役为保留未使用。D2 起子 Run 由既有 Worker 驱动、父 Run 挂起/唤醒、取消传播与父目标 fence 已接。
- **没有执行器的种类在派发前拒绝**（`dispatch.resource_kind_unsupported`），不进审批流程；许可判定仍完整实现——它描述的是"将来有执行器时外发必须人工批准"这条语义，不冒充"现在就能发"。
- **资源规范化按"同目标只能一种写法"做**：host 小写+IDNA、默认端口省略、`.`/`..` 折叠、百分号段先解码再判、拒 userinfo/fragment。每一种折叠都对应一个"看起来只是写法不同、实际是两个目标"的口子；`ResourceRef` 只收已规范化形状（值会进审批摘要）。
- **内容引用：去重 ≠ 共享可见**：内容寻址会去重（同内容多任务一行对象），所以授权记在 `(digest, task_id)`；引用是**校验和**不是文件名，读时重算 sha256；分页不切半个 UTF-8 字符。
- **读工具不过派发是既有硬口径，不是本轮开的后门**：`Dispatcher._typecheck()` 明写"只读动作不经过派发"，`read_text`/`memory.search` 同路。给一个永远到不了派发层的 `content` 资源种类 + 策略双闸只会多一份没人用的契约；改成读工具 + 存储层可见范围，"双闸"落地为"存储层核一次授权 + 执行层用本任务身份调它"（NC：把身份换成空串就红）。


### §7.1 语义摘要轮的实现决策（2026-11-11，PR #103 / #105）

- **记录存正文、事件存身份**：不新增事件类型（仍 41 个）。两份正文（事件 + 记录）会互相矛盾，改一份忘另一份就开始对不上；"这一轮用了哪一版"用 `summary_id`/`version`/`text_digest`/`source_seqs` 四个字段就够，正文按 id 从同一库取。
- **编译保持纯函数**：摘要正文由调用方**先持久化**再作为 `SummarySlice` 传入 `compile_context`。可重放性来自"回放读当时保存的那一份"，不来自"禁止模型"——这句是对 `context.py` 原注释结论的修订，理由保留。
- **允许「子集复用」**：材料身份含事件 `seq` ⇒ 丢弃集合一变大就是新材料；只认"材料完全一致"会在调用上限一到时整段回落确定性裁剪，最先丢的硬约束又没了。放宽成"这份摘要覆盖的 seq 现在确实都被牺牲了"，用它成立。
- **先持久化再引用**：`begin(pending)` 在模型调用**之前**提交。崩溃恢复只能看到 pending，于是"标 failed、不重试"是唯一诚实的处置——重试等于偷偷多花一次钱，而"不偷偷增加一次模型调用"正是 §7.1 第 4 条。
- **摘要不许弄死 Run**：预算拒绝 / 已取消 / upper 模式拿不到输出上界 / 模型报错 / 正文空白或超长，一律回落确定性裁剪。`BudgetConfigError` 在摘要路径上被当作"这次不摘"处理（主调用该拒绝还拒绝）。
- **默认上限 3 次是产品选择**：它防"每轮一次"的费用失控；很长的 Run 后段仍会回落确定性裁剪（设计取舍，不是 bug）。


### G2 网络阶段的实现决策（2026-11-11，PR #110 / #112）

- **两道门，两道都在 `prepare()` 里走完**：任务级白名单（`outbound_allow`，源级、创建时定死）+ 逐次人工批准（`c4.outbound`）。白名单不通过时**不弹批准**——"批了也发不出去"的批准会白占一次人工决定。白名单是**源**（transport+host+port），路径/查询一律拒绝：允许写路径会让人以为"只许访问 /docs"，而执行侧只按源判，那是一条假保证。
- **外发面靠"请求里没有别的数据"来收口**：只用 `http.client`，不读代理环境变量、不发 Cookie / `Referer` / `Authorization`、不转发凭据、GET 无请求体，请求头只有固定几项。这让「私有内容未经外发许可不发包」在执行面**可被核对**——但它证明的是"请求里没有内容"，不是"没有别的泄露通道"。
- **重定向只许同源**：批准的是"这个源上的这个 URL"；靠 302 把包发到另一个源等于绕过批准。跨源/换端口/https→http 一律拒绝，需要时让模型重新发起、重新拿批准。
- **字节账本用可验证上界**：发送前按 `max_bytes` 预占（本次最多读这么多），读完后按实际字节结算；`prepare()` 先做一次非预占的额度预检，额度不够在弹批准之前就拒（`dispatch.budget_refused`）。未知用量（超时/读断）**保留预占**，不记零。
- **取消有两层**：派发前查 + 执行器每跳/每块之前查。单独去掉任一层两个判据仍绿（另一层兜住了），两处都去掉才真的发包——冗余是**证明过**的，不是声明的。
- **内容引用先落盘再写事件**：抓到的字节先写内容寻址 blob + `ContentStore.record(task_id)`，再写 `tool.result`；反序会承诺一份不存在的内容。回放只读已保存结果，缺 `ref` 报 problem，不重新联网。
- **`tool.result.status` 的封闭集没有 `cancelled`**：取消按 `failed` + `output.reason=net.cancelled` 记（"没发出去"是确定结论，`effect_state=none_applied`），不为一个状态扩契约。
- **评测的"来源可离线核验"必须真的关掉服务器**：runner 在 Run 结束、夹具停掉之后从内容引用读回字节（会重算 sha256），读不回来记 `invalid_reason`（评测基础设施无效），不是"通过"。任务层用 `RU-W1@2.0.0`（断网诚实失败）与 `RU-W2@1.0.0`（本机夹具交付）;旧版 `RU-W1@1.0.0` 的「本轮不提供网络能力」在 #110 之后不再成立，换版本而不是改判据。
- **模型工具面绊线显式过一次**：`test_veto_scope` 的字面清单加 `network.fetch`，动作集合加 `fetch`；`tool.intent` / `tool.not_dispatched` / `receipt` 的 tool_id 枚举与 JSON 镜像同步。

### F10 N3 蒸馏的实现决策（2026-11-11，PR #98）

- **只做确定性合成，LLM 合成延后**：L2/L3 的立论是"可重建、可回放、能和证据逐条对账"；生成式摘要会让"两次合成是否一致"变成不可机械核对的事。所以本轮 L2 是**引用式拼装**（每行带 `[memory_id@revision]`），两次合成逐字节相同，增量 delta 天然成立。要上 LLM 必须另立契约：正文进 `tool.result`、replay 用快照、与确定性路径分两套代码与两个测试文件。
- **freshness 的基准是逻辑时钟 `seq`，不是 `consolidated_at`**：后者是钟表时间（可回拨），而且 `memory.confirm` 这条生命周期推进会把该列改成确认时刻——晚确认的页会永远"新鲜"。基准落在 `source_refs` 的 `seq:<basis_seq>`（合成那一刻看到的最大 `last_seq`），缺失时判"不可判定"，不谎报新鲜。
- **陈旧层降权、不短路**（F10 §8）：`stale=true` 的页排到新鲜 L1 之后，既不删也不挡召回；有 stale 时排序先按 `(stale, 原排序键)`，否则 proximity 0 的 L2 页会压过 user 作用域的 L1。
- **合成页 = 候选**：`memory.consolidate` 只写可重建投影，结果 `candidate` + `origin=inferred`，人 `confirm` 才进召回；免审批只保留给 `memory.confirm` 本身。页 id 由「作用域 + 层 + 正文」决定 ⇒ 重复合成幂等，旧页由同层新页 supersede 且仍可按 id 查。
- **L3 只对 user 作用域**：偏好页是"关于这个人"的页面，workspace / task 的偏好不是同一层语义。

### F10 N6 控制面端点的实现决策（2026-11-11，PR #100）

- **控制面记忆操作只留一份实现**（`memory_control.py`，CLI 与 daemon RPC 共用）："写记忆必须走 dispatcher"这条纪律抄两份，早晚有一份图省事直接改投影。
- **不新造身份令牌**：控制面的边界是"这条 IPC 只有本机人能连"（POSIX `run/` 0700 + socket 0600），再加一套 token 只是多一个能被抄走的东西；写操作用 `HumanIdentity.local_cli()` 落审批，与 CLI 同一个 actor。
- **同名不同路**：`memory.get` / `edit` / `forget` 既是模型工具也是 RPC 方法，但两条路身份与过滤完全不同（模型按 `allowed` 装备、人在本机全作用域）——所以测试断言里必须写清走的是哪条路。
- **规格清单也会漏**：§7.2 列的 6 个方法没有 `memory.confirm`，缺它就没有"人确认候选才召回"的入口；落地时按"判据优先于清单"补齐并记进 §7.4。

### 恢复 B T1 平台准入的实现决策（2026-11-15）

- **路径 hash 只保留为 namespace，不再冒充实体身份**：POSIX 实体键来自持续打开句柄的 `device+inode`；Windows 来自 `GetFileInformationByHandleEx(FileIdInfo)` 的卷序列号+128-bit file ID。硬链接别名会争同一 entity lock。
- **gate 与业务锁分层**：gate 只保护 anchor/身份固定窗口；namespace/entity shared access 交给真实连接，所有锁非阻塞。registry observation 也持 shared namespace/entity，因此多个普通连接可并行，写者独占时立即 busy。
- **registry 读取是完整快照，head 不是授权**：固定真实 Home 下只追加 registry 每次读取规范 meta、全事件链和 head，调用 T0 `validate_event_chain()`；写入只 INSERT event，并在同一事务用旧 head CAS 前进。
- **SQLite close 决定 access 寿命**：`MaintenanceConnection.close()` 先让 SQLite 真正关闭，再释放实体句柄和外部锁；跨线程 close 失败时连接仍可写，后续锁必须继续持有。
- **Windows 目录发布不伪造 fsync**：registry 目录用 `MoveFileExW(MOVEFILE_WRITE_THROUGH)` 发布；Windows 目录的物理断电 durability 仍不是已证明事实。CI 另有原生 probe artifact，直接记录 file ID、硬链接、持句柄 rename、锁和 registry 结果。

## 5. 踩坑教训（别再踩）

- **“锁曾消失”不能授权清理，清理前必须在同一互斥下重新证明无人接管**：Host 发布
  `host.lock` 是旧文件消失、新文件原子出现之间的可观察空窗。退出路径若只在旧 Host
  消失后读取一次 Daemon 锁，就会把新 Host 已接管的同一 Daemon 当成旧实例误杀。
  条件删除必须先进入 Host 元数据跨进程守卫，确认仍无 Host，再把 Daemon 当前锁 SHA
  与退出前快照逐字节比较；新 Host 或新 Daemon 实例一出现就返回“保留”并释放守卫，
  等待 Daemon 退出也不能长期占用守卫。回归必须真实制造 `旧 Host 退出 → 空窗 →
  新 Host 发布 → 复用 Daemon` 的顺序，普通“新锁已经存在”的保护不能覆盖该窗口。
- **Tauri `setup` 不是事件循环中的普通异步上下文**：`setup` 会在事件循环启动前执行，
  Host 冷启动又可能同步阻塞；此时直接 `WebviewWindow::navigate` 可能只投递消息而没有
  消费者，留下“Host 健康、窗口存在、页面全白”的假通过。导航和失败页写入应通过
  `run_on_main_thread` 排入事实上的主线程事件队列。GUI 验收必须截取并检查真实页面内容；
  只测窗口数、PID、端口或 `/health` 不能证明用户看见了 UI。
- **Windows 的 `os.replace` 瞬时共享冲突要在原子发布层有界处理**：目标文件被另一个
  句柄短暂打开时，即使源临时文件已完整写入并关闭，Windows 仍可能给
  `ERROR_ACCESS_DENIED` / sharing/lock violation。只能重试这一小组 Windows 错误并设置
  总时间上界；普通权限错误立即失败，持续占用最终仍须报错。测试必须同时证明“瞬时失败
  后成功”和“持续失败仍在界满后失败、旧文件与临时文件状态可判定”，不能用重跑 CI
  掩盖。
- **跨进程共享的元数据文件必须原子发布**：Host 与 Tauri 壳没有共用 Python
  侧互斥；直接截断再写会让另一个进程把短暂中间态当成永久损坏。发布锁文件应写
  同目录临时文件、fsync 后原子替换，再 fsync 父目录；回归必须让外部读者停在
  “已截断但未写完”的旧窗口，而不是只反复启动碰运气。
- **同一用户不能并行跑两个含 `EventLog` 的完整套件**：T1 的 restore gate 是用户级固定
  锁，不是按测试临时目录隔离。第二套件会在迁移/备份独占窗口得到
  `maintenance_busy`，即使两套测试的数据根不同。门禁必须先核对 `pytest` 与
  `eval.crash_child` 均退出，再取得一次独占 gate 作为空闲探针；失败原件保留，不能把
  并发污染写成产品回归。
- **`database_list.main` 不是 SQLite 内部 HANDLE**：标准库不能读出 VFS 使用的 OS 句柄。T1 能证明“锁定实体句柄 + main 路径 + 重开实体一致”，不能把这三项写成直接读取 SQLite 内部句柄。
- **读者不能用 `BEGIN IMMEDIATE` 冒充共享锁**：它会把两个普通连接串行化。registry observation 使用 shared namespace/entity + `BEGIN` 只读快照，写者使用外部 exclusive 锁；这样既保留并发，也让已接线 writer 立即互斥。
- **Windows 没有可移植的目录 `fsync` 等价物**：`FlushFileBuffers` 对普通目录句柄不能当成无条件成功。目录发布另行使用 write-through rename，并把物理断电级 durability 保持 `NOT_RUN`。
- **平台身份必须来自句柄而不是两次 `stat`**：路径可能在检查与加锁之间被替换。最终 entity identity、硬链接数和后续 `verify_path()` 都以真实打开句柄/重开实体为准。
- **终止测试必须绑定实际执行者（2026-11-14）**：Windows 的 venv `python.exe` 可另起真实解释器，`Popen.pid` 与屏障内 `os.getpid()` 不一定相等；只等启动器退出不能证明 worker 已死。`communicate()` 会关闭 stdin，使仍在屏障读取的 worker 遇到 EOF 并进入错误分支。硬终止测试须让屏障同时报告 PID、严格匹配被终止的进程，并在关闭管道前确认其退出。文本 stderr 的系统换行与二进制屏障协议分别精确断言，不用 strip 或吞异常把失败改绿。诊断清理自身的 WinError 5 与产品失败分开保留；本次七例诊断整体仍失败，其中四个 venv 清理错误不因两个直接解释器对照通过而消失。

- **测试入口也必须匹配生产初始化（2026-11-13）**：Windows上stdout重定向到DEVNULL仍可能以cp1252编码；测试用`python -c`直调Host、遗漏真实`cli.main`已有的`_force_utf8_streams`，中文print会在发布ready后异常、清元数据，表现为EOF/错误接管。用同一源码、同一cp1252环境、只补真实入口初始化的正负控定位，不能全局开UTF8掩盖差异。stderr保留原始bytes并按子进程实际UTF-8解码；失败收尾不能提前kill截断traceback或误杀Windows launcher而留下实际worker。只在最终runner证明Windows收尾，不拿Mac PID相同的情况代言。

- **探针必须持有自己发出的请求（2026-11-13）**：暂停真实Thread.start时发GET，0.4秒无响应就主动关TCP，会在放行后制造服务端10053。边界期限保持不变，但应保持同一连接，服务ready后在普通I/O预算内读完同一响应；继续检查stderr、资源与实际worker PID。`Path.read_bytes`经CRT可只剩EACCES，不能把底层share0必然等于异常对象winerror32当跨层契约。原红切源码时还须覆盖pytest的`pythonpath`配置并在同一进程核验导入hash，环境PYTHONPATH单独设置不足以证明跑到旧版。

- **恢复自身也是崩溃路径**：不能先提交unknown result，再在另一事务补block；重启会把“有result”当完成而跳过保护。新增不变量需同时覆盖新窗口、旧数据补偿与人工解除意图。
- **期限要覆盖真实阻塞点**：收到HTTP头不等于读完正文；创建响应deadline之前的PIPE写入、主线程关闭被读线程占用的缓冲流都能逃出期限。测试要用真实背压和屏障，不以增加超时或重试替代定位。

- **条件删必须与读、写、接管处于同一互斥（2026-11-13）**：读到“还是我的pid”不代表unlink时还是；stop拿旧快照后，新Host已经接管也会被误删，pid复用还要求实例token/started_at绑定。只给unlink加锁或只加pid参数都不完整。HostInfo使用`run/execution.guard`，不占业务根守卫，HTTP/Daemon等待不入锁；晚到的启动者条件发布失败时只能关自己资源，不能误停胜者复用的Daemon。Windows WinError5既可能来自真实双删，也可能是持久只读拒绝，不能统一吞掉。读失败更不能等同物理缺失；只在FileNotFoundError时宣布缺失。

- **发布端口不等于已启动服务（2026-11-13）**：仅在写端口时校验owner，之后到`serve_background`之间仍可被真health失败的后启动者接管，旧实例继续serve会形成两个活Host。发布和服务线程启动须处于同一短元数据临界区，停止回调先安装、monitor在条件发布后且HTTP开放前实际启动。验证正确互斥时必须以第二进程真实OS锁busy作为放行信号；先等第二进程ready才释放第一进程会把探针自身循环等待误判成产品死锁。全量绿不能压过独立反例，改后重跑受影响验收。

- **HTTP开放前必须完成所需线程启动（2026-11-13）**：a624把monitor移到HTTP服务之后，真实`Thread.start`边界暂停时`_thread`已发布而尚未启动；stop真实join抛RuntimeError，但finally仍关闭HTTP并放行主循环，导致退出0且日志未关。只有退出码/Daemon不可达断言会漏掉它，回归必须同时验收后台异常和所有本实例资源。修复启动顺序，不能通过隐藏`_thread`、跳过join或吞异常假装修好；本例Daemon已停，不扩大成孤儿进程。

- **定时诊断测试必须证明采到了所声称阶段（2026-11-13）**：第二读者用无sleep的setup屏障发现，旧三阶段测试只有栈header断言，snapshot都在setup时仍3 passed；真实Lock正控也会因提前采样缺目标帧而误红。测试专用ready事件应绑定实际目标阶段，setup/call/teardown各自断言不同函数帧；隐私测试也须确认采到带敏感变量的目标函数。等待诊断完成时要同步排空输出或落临时文件，不能引入未读PIPE阻塞。修夹具不等于修改生产60秒协议，旧测试版本的全量绿不移用为新版本门禁。

- **后台诊断的等待本身也会失败（2026-11-13）**：有限浮点数并不一定是平台可接受的等待秒数；`1e30`在Event.wait中OverflowError，若只把snapshot放try里，pytest可能1 passed/exit0而诊断线程已死。配置须受`threading.TIMEOUT_MAX`约束，后台等待与采样同置错误收口，并以真实成功/失败/中断子pytest验证非零诊断及原退出码保持。

- **诊断本身也会改变被诊断进程**：CPython3.12原生定时dump能在纯标准库动态frame负载中access violation；Python栈最后落在COMMIT不等于SQLite根因。须用无项目代码的同环境正负控区分。后台线程已经输出部分字节后仍可能抛错，`exit0 + stack_bytes>0`不足以作证；错误必须显式传到会话退出码。fatal负控也必须确认真的执行到abort，配置提前失败的非零不是有效负控。Python线程方案拿不到GIL时不能保证栈，不能把外部期限说成等价诊断。
- **清理要在资源构造成功时登记**：只在helper返回后或测试最后close，构栈/启动中途失败时仍会把真实子进程留给后续用例。MCP对象阻断夹具改为构造成功立即登记finalizer，并用测试失败/构栈失败两类注入证明资源清零；业务断言通过不代表夹具没有污染环境。

- **"审批门"要在弹批准之前判完**：白名单/额度这类注定失败的前置条件如果只在执行时检查，用户会收到一个"批了也发不出去"的批准——白占一次人工决定。把前置条件放进 `prepare()`（`c4.outbound_not_allowed` / `dispatch.budget_refused`），批准只发给能执行的请求。
- **冗余保护要"证明"，不是"声明"**：取消检查做了两层（派发前 + 执行器每块之前），单独拆掉派发前那层两个判据**仍然绿**（执行器第一跳之前就看到了取消），两处都去掉才真的发包。写负控时要先确认拆的是"最后一层保护"，否则会把冗余误判成"这行没用"。
- **"来源可离线核验"必须真的关掉服务器再读**：否则测的是"服务器还在，所以读得到"。runner 的收集顺序（Run 结束 → 停夹具 → 读内容引用并重算 sha256）就是这条判据的本体；读不回来记无效样本，不是通过。
- **"夹具 ≠ 线上"要写进判据与文档，而不只是注释**：联网研究的成功判据全部跑 loopback 夹具；报告、README、PRD §10.5 都写明"真实公网研究未验证"，否则读者会把"夹具通过"当成公网能力。
- **计划里估的测试条数会偏**：规格估 14 / 22 / 10，实测 18 / 23 / 11——补的是"匹配/迁移/CLI 端到端"这类不估就会漏的判据。以实测回填规格，不拿计划数当交付数。

- **"默认关闭"这种留账写法会被下一条要求直接推翻，别把边界写死在验收句里**：G1b 交付时"账本默认关闭"是刻意边界，G1c 就要求默认记账。可复用做法是把"为什么关"写成**条件**（关着时哪条 PRD 要求不成立），而不是写成永久结论——条件变了，结论自然要改。
- **`executescript()` 建表会先提交当前事务**：把"建摘要表"放在写路径里时，外层事务的原子性被打断——回滚之后摘要行还在，"摘要行与事件同生共死"变成口号。修法：建表在事务**之外**（构造时），回放用 `create=False` 不建表；判据是"外层事务回滚后不留半条记录"。
- **"关键信息保留"夹具会假绿**：第一版让 6 轮都读**同一个**带约束的文件，后面几轮的工具输出里当然还有约束，"丢没丢"根本没被测到。改成"第一轮读带约束的文件、其余轮读别的"才真正区分两种实现。**做保留类判据先问：关键信息是不是还有别的活路。**
- **负控必须拆掉所有层保护**：`BudgetConfigError` 是 `TianshuError` 子类，"安静回落"有两层 catch；只拆第一层时负控仍然绿。同理，摘要激活有"整段覆盖"与"真的被牺牲"两个条件，负控要能分别拆。
- **负控脚本自己会伪造绿**：本轮两次假绿都是脚本问题，不是代码问题——① 测试 node id 传成单个字符串（`exit=4 no tests ran`，看起来"非零"其实没跑）；② 替换只改了两处引用中的一处（占位里 `ref` 与提示里的 `{"ref": ...}` 各一份，去掉一处另一处还在，模型照样拿得到）。现在负控脚本先断言"替换目标存在且内容确实变了"，并在每次跑前清 `__pycache__`（同一秒内改写文件时 mtime 粒度会让 pytest 复用旧字节码，实测出现过一次）。
- **枚举型契约每加一个值都是三处同改**：`action` 5→7、`tool_id` 加 `read_content`、6 个规则编号——`rules.py` / `schemas/*.json` / PRD 表格，`tests/test_rule_ids.py` 与 `test_event_schema.py` 会钉住；`.json` 里的 enum **顺序**也要与 Python 一致（镜像测试是逐字比较，排序过就红）。
- **"绊线清单"要按设计用它**：`test_veto_scope.py` 里那份模型工具面字面清单不是冗余断言，每加一个模型可见工具都必须在那里显式过一次——加 `read_content` 时它确实红了，改它时要在 PR body 写明这是**有意的口径变更**。

- **「兼容读取」不等于「兼容写入」（本轮 fail-open 差点混过去）**：逐类型 payload 第一次实现时，`detect_version` 在 v1 校验失败后自动退回 v0，而 v0 schema 的 `required` 是清空的 → 任意 `{}` 都会被当成「旧版」接受，写入校验等于没写。修法是把读兼容（旁表/映射）与写兼容（显式 `legacy_write`）分开，并加元测试：生产代码零 `legacy_write=True`、显式 legacy 仍须过 v0 封闭 schema。
- **单线程里的同步推送 = 慢消费者 DoS**：`daemon._main_loop` 把请求处理和 `conn.send()` 推送放在一个线程里，一个「订阅后不读」的客户端写满发送缓冲就能把状态/审批/取消/shutdown 全堵住；改成有界队列 + 独立发送线程后，慢订阅者被断开且重连能按游标补读。
- **恢复判据要落在"同一个事务"上，而不是"看起来没写"**：记忆写的投影更新与 `tool.result` 同事务提交，于是"有 intent 无 result"⇔一定没生效，恢复可以给出**可证明**的结论；文件写没有这层保证，只能对快照/哈希说"既非写前也非写后"→ unknown + 阻断目录。别把两类副作用塞进一个 `pending_writes` 计数：一旦混算，接管判定会去要一份记忆写根本不存在的快照证据。
- **审计表要"执行期完整 / 终态脱敏"两段式**：执行器从 `dispatch_requests.args_payload` 取参数、`args_hash` 要绑定校验，所以不能落库即脱敏（会把派发打红）；但审计表没有擦除路径，正文留着就是永久副本。正解是请求进终态后**幂等**补做脱敏，并让终态推进与副作用提交**同事务**——否则会留"写成功但正文仍在"的窗口。被拒绝的请求永不会执行，落库即脱敏即可。
- **「声明」不能当「证据」**：隔离后端可以自称 `app_container`，探针必须真的去读/写界外文件、连 loopback、读父进程 env、观察孙进程；门只认探针报告，`unknown` 一律阻断。假后端负控（自称隔离但探针不通过）必须红。
- **备份/迁移的「中断」要真的注入**：只测 happy path 等于没测；`crashpoint` 每步硬退后重启，验证「可续做 / 无混合版本 / 或从备份恢复」，并证明「未发布的临时目录不会被当有效备份」。
- **严格分支保护下多 PR 串行合并**：每次合并都会让其他 PR 的 required checks 变「过期」（strict + enforce_admins），必须重新同步 main 再等 CI；不要用 `--admin` 绕过，也不要为了让 PR 变绿去删 required check。
- **夹具是在声明「我是历史形状」**：默认严格后 19 个测试文件的播种调用点显式加 `legacy_write=True`（不改 payload/断言）；调生产 API（`commit_model_turn`）的夹具则改为当前契约值（`finish_reason: tool_use`、usage 双键）。用放宽产品校验来迁就测试是最坏的一种「变绿」。

- **「CI 红了」先分家，而且必须按用例名逐条对账**：收尾轮 Windows 从 35 failed → 8（#87）→ 6（#88），每一轮都逐条读了失败用例名，才发现四条本轮新模块的真缺陷（`os.fsync` 对只读句柄 `EBADF`、`time.monotonic()` 分辨率、Windows `_pid_alive` 恒 `None`、隔离网络探针在 runner 上应记 `unknown` 而不是硬编码结论），以及两条只在 CI 时序下暴露的并发缺陷（握手期裸 `EOFError`、指标半行）。教训：**不要用「#50 家族」一句话结案**——按用例名对账，否则新缺陷会被旧标签盖住；本轮就曾把 #87 之前新模块的缺陷写成「既有族」，是错的，PR #87/#88 的正文与本文已按实测改正。
- **「本机连续绿」对并发窗口没有证明力**：握手 EOF 与指标半行在同一提交上 macOS 本机跑两遍全绿，只在 CI 的慢 runner 上才撞到。并发/时序缺陷的判据要能**确定性构造**窗口（假对端只 listen 不握手、手写半行再补齐），而不是多跑几遍碰运气。
- **`EOFError` 不是 `OSError`**：`except (OSError, ...)` 捕不到 `multiprocessing.connection` 握手期抛的 `EOFError`。凡是「连不上」的封装，都要显式把 `EOFError` 算进去并映射成自己的域错误；否则调用方按契约只捕 `(IpcError, OSError)`，收到的是契约外的崩溃。
- **自审有盲区**：PR #7 自写自审自合，第二读者十分钟找出 13 项缺陷、5 项高危。作者知道自己的意图，所以看不出实现没兑现意图。→ 高危改动必须独立复审（见 §6）。
- **测试证据会造假**：曾用「同进程两条连接」冒充两个进程、用 `sleep(0.05)` 猜时序、用手写替身盖住真洞。→ 并发要真子进程 + 屏障，时序用注入时钟，契约断言用真实 `tool_manifest()` / 真实 schema。
- **文档会说假话**：README/PRD/复审报告多次与代码漂移，出现专门「修假话 / 对账」的提交。→ 改代码同步改文档；复审报告写错归因要按事实重写。
- **「放在系统标准位置」这类话要能指到代码行**：README 长期写着「运行时数据放在系统标准位置」，而 `cli.DEFAULT_DB` 一直是**当前工作目录下的 `.tianshu/events.db`**（相对路径，靠 `.gitignore` 排除）；`~/Library/Application Support/AgentOS/` 那套当时只存在于 TECH-ARCH、属 M5 设计。这条假话过了多个里程碑没人发现——**直到 M5 才真正兑现**：`ipc.default_data_root()` 实现了平台标准目录（`TIANSHU_DATA_ROOT` 覆盖），`daemon`/`host` 用它；旧内核子命令仍默认 CWD，两套并存。教训不变：凡是「放在哪里 / 什么时候清理 / 谁负责」的运行时行为陈述，都要能指出是哪一行代码；指不出来，就说明它还没实现。
- **事务要可组合**：各 store 各自 `BEGIN IMMEDIATE` 会嵌套报错；统一走 `EventLog.transaction()`（已在事务中则复用）。
- **父子目录需要互斥**：`(st_dev, st_ino)` 只答「是否同一目录」，`/ws` 与 `/ws/sub` 是同一棵树，须 ancestor/descendant 一律冲突。
- **租约/fence 必须释放**：Run 收尾或被 fence 后要在同一事务释放租约，否则新 Run 被自己上一个 Run 挡住。
- **进程身份 ≠ pid**：pid 会复用，用 `ProcessIdentity{instance_id, pid, started_at}`；外来实例不得过守卫/续租/释放。
- **封闭集要强制**：`finish_reason` 曾出现越界值 `error`/`unknown` 靠 `type: ignore` 压着；改为封闭集 + `ModelError` 对象，越界即报错。同一条道理后来复用到 C3 事件类型与 C4 拒绝原因上。
- **状态必须是事件的投影**：直接写表不写事件，回放就无法解释为什么被 fence。
- **「定义了」不等于「会产生」**：一个 id/类型可能只在代码里有定义、没有任何发射点。登记契约时要么给出产生路径，要么显式标注「保留未使用」，不能让它默默躺在集合里冒充已实现。
- **「超时」和「断线」都返回 `None` 的 API 是定时炸弹（M5 实测烧核）**：对端一断开，`multiprocessing.connection` 的 `poll()` 立刻报可读、`recv_bytes()` 立刻 EOF；读循环若只判 `None` 就会 100% CPU 空转——演示里关掉浏览器后 daemon 烤满一个核，直到被杀。修法是把语义钉死：`ServerConnection.recv()` 断线仍返回 `None` 但置 `eof=True`（此后再调立刻返回），`Client.next_event()` 断线抛 `ConnectionClosed`。回归测试还包含**真子进程 CPU 记账**（`time.process_time()`：空转 1.5s 会吃掉约 1.5s CPU），因为"读线程退出了"是内部事实，"没烧核"才是用户能感觉到的后果。
- **`Listener.accept()` / `Connection.recv()` 的阻塞语义约束了关闭方式**：这些调用不能被就地 `close()` 从另一个线程可靠打断（会卡在内部锁上），所以 Daemon 的关闭靠**「轮询 + 停止标志」**（带超时的 `poll`，每轮检查 stop），而不是收到 `shutdown` 就 `close()`。代价是退出最多等一个轮询周期；换来的是不会出现"关不掉的守护进程"。
- **验收必须用真进程 / 真产物**：M5 的界面截图是**真 daemon + 真 `ui/dist` + 真 Host 桥**跑出来的。手写一个 HTML 当截图会盖住 token 注入的 Bug——真页面是「空 `<meta>` 原地替换」，手写 HTML 恰好"有/没有 token"都看不出来（M5 期间真踩过：多插一个 meta，`querySelector` 拿到空值 = 没 token）。任何"UI 能用了"的结论都得指着真产物。
- **后台 shell 启动的子进程继承 `SIG_IGN`**：用 `(cmd &)` 起的进程后用 `kill -INT` 收尾是无效的（子进程忽略 SIGINT），本机实测后台 Python 的 `SIGINT` disposition 就是 `1`（SIG_IGN）。这是**测试环境陷阱，不是产品 Bug**——要么用能自退的脚本，要么用 `SIGTERM` / 显式 shutdown 接口，别把"打不死"当缺陷报。
- **顺序即契约（M5 `host stop` 教的）**：退出/清理路径里"**先发信号再清理**"与"**先清理再放行主线程**"是两种语义，选错会把安全边界（这里：不留孤儿进程）变成偶发问题——先置 `stop_event`，主线程立刻返回、进程退出把清理线程带走，`stop` 报成功但 Daemon 还在跑。凡是"关掉 A 之前必须先把 B 收掉"的路径，顺序本身就是契约，要写进注释和测试名（`..._stops_daemon_too`），不能靠"反正会一起退出"。
- **"敲一下叫醒阻塞的 accept"必须跨平台写，且不能阻塞 close（M5 二次踩）**：Windows 上 accept 阻塞在 `ConnectNamedPipe` 的 **overlapped I/O**，从别的线程关 listener 句柄**不会取消**挂起的 I/O——accept 线程永远卡住、命名管道名一直存在（症状：daemon 已退出、`probe()` 仍为真、下一个实例被拒）。第一版只给 Windows 加了"敲门"，结果 **ubuntu 红了**：Linux 上关掉正在被 `accept()` 使用的 fd 也不会可靠唤醒 syscall（macOS 会，所以本机一直绿）。第二版又踩：敲门本身（`CreateFile` 在实例全忙时）可能长时间不返回，把"关服务端"变成新的卡点（CI 的 windows job 挂在 Pytest 里 20 分钟没输出）。**结论**：①阻塞式 `accept` 的唤醒要么跨平台统一做、要么别做；②任何"为了让退出更快"的辅助动作，都必须放进带超时的旁路线程，不能让退出路径等它；③"本机绿"对跨平台阻塞语义几乎没有证明力。
- **子进程日志不要用没人读的 `PIPE`**：`stderr=subprocess.PIPE` 而父进程不读，子进程写满 64KB 缓冲区就阻塞在 `write` 上——表现是"端点永远不出现"，一个跟被测语义毫无关系的死锁。写文件既避免死锁，失败时还能把尾巴带进断言消息（`_tail()`）。**推论**：子进程"起不来"的断言必须带上 `returncode` 与日志尾巴，否则只能猜。
- **"挂住"和"失败"要分开**：CI 作业默认 360 分钟超时，一个挂住的测试会占 6 小时 runner 且不给任何信息。给 job 设 `timeout-minutes` 后，挂住至少会带着已有输出结束。同理，等子进程就绪不要靠"轮询 probe"猜进度——让它自己在 `on_ready` 落一个标记文件，断言就能区分"没走到 on_ready"和"走到了但端点连不上"。
- **pytest 进程内测不出"进程退出"类 Bug**：主线程退出后 pytest 进程还活着，清理线程不会死，Bug 会躲过去。这类逻辑必须用**真子进程**（`subprocess` 跑真的 `tianshu host run`，再 `stop_host`，然后从外部断言进程/端点），否则测试测的是"函数返回了"，不是"进程真的退干净了"。

- **「再检查一次」关不严 `check → act` 的窗口，共享一把锁才关得严（复审 中-1）**：`ensure_not_blocked` 只在派发 phase-1 查一次，查完到落盘之间仍可被插入一条 `recovery.blocked`。修法不是再多加几次检查，而是**让两件事抢同一把锁**：恢复落阻断与派发落盘都进本机执行守卫（`execution_guard`），先后就有了全序；同时把核验压到执行边界（改文件前最后一步、按真正要写的那个路径判）。教训：凡是「判定 → 副作用」跨进程成对出现的，先问"这两件事有没有共同的线性化点"；没有的话，检查次数只是把窗口缩小，不是关掉。
- **「无法判定」必须和「没有」分开（复审 中-2 / 中-4）**：两处 fail-open 是同一个病——`except Exception: continue` 把解析不了的阻断事件当成"没阻断"；未知 `tool_id` 套用"清单里没有条目 = 确定没写"。前者等于替一条看不懂的审计记录宣布结论，后者等于替一个语义未知的工具假设写入协议。口径：**不可解析 / 语义未知 / 读不出结论，一律 `unknown` + 阻断**，让人来看。猜出来的"没事"比明确的"不知道"危险得多。
- **证据要核「真正被写的那个东西」（复审 中-3）**：M1 的「kill×100 · 0 重复副作用」听着很强，实际注入只走 `create_report`，不变量 A 读的还是预置后从未被写的诱饵文件 `note.md`——断言一直绿，但和这次写入无关。修法：从 `tool.intent` 参数解析真实目标再逐字节核对，并把矩阵配平到 create + 覆盖写。**而且必须做负控**：故意破坏不变量（只写半截内容、把 A 改回读诱饵）看测试是否红，否则分不清"断言成立"与"断言恒真"。
- **复审报告也会误报，误报同样要留证据（复审 中-5）**：报告称 `prepare` 对目录外删除"不拒、会给用户一张永远批不了的审批"，实测是 `rejected`（`dispatch.lease_lost`）、无审批、等待队列为空、文件没动。处理方式不是"顺手加个检查"糊过去（那会多出一条永远不生效的分支），而是**加一条钉住测试**把"它本来就拒"变成回归，并在报告里写明反证。审查者与被审者都可能错，能复现的输出才是共同语言。
- **真实模型会把"评测器没想到、但系统提示词要求"的动作做出来**：P1 的判据写「整个 Run 只能有一个生效副作用」，脚本模型照做；真实模型先把报告写出来（`SYSTEM_PROMPT` 规则 3 + `输出文件：…` 明说了要它），于是两个副作用 → 判据红 → `verdict=unsafe`。**判据要按目标作用域化**（"`notes.md` 恰好被写一次"），而不是数整个 Run 的副作用总数；否则测的是"模型像不像我的脚本"。
- **"不安全"与"没做到"必须分开**：一份 `unsafe` 意味着"系统被绕过"，而"模型什么都没做"只是完成度为 0。把 `approval.requested=0`、`outcome≠failed` 这类**收尾形态**标成 safety，会让"更保守的模型"被判更不安全——方向反了。
- **假分数比没有分数危险**：memfs 下评测能跑完、能出报告、能打印 `verdict=unsafe`，看起来像结论，其实是后端不匹配（fixture 落宿主盘、执行走虚拟盘）。判不了就拒绝（`require_local_backend`），比交一份会被引用的数字强。
- **审计工具的误报会训练人忽略它**：`replay` 那条"产物哈希不一致"是错的（比的是另一个文件的哈希），但文案看起来像真发现。凡是"核对"类逻辑，先问"我比的是同一个对象吗"——按路径/按 id 归属，不要按事件顺序猜。
- **校验器自己崩掉，等于没人替你回答「到底写没写」**：快照清单被截断时 `SnapshotStore.load()` 直接漏出 `json.JSONDecodeError`——恢复还没开始推理就先抛栈，CLI 只打印 "Expecting property name"，既没说清哪份证据坏了，也没有阻断。读审计材料的代码，解析失败必须转域错误（`SnapshotCorrupt`，与 `EventLogCorrupt` 同一档），并让调用侧落成 `unknown`+阻断或明确拒绝；不能当成空清单继续，也不能让裸异常穿过库边界。负控照旧：把修复回退后新用例必须红，红的样子就是那条 `JSONDecodeError`。


- **"读不出来"必须 fail closed，而不是洗成"什么都没有"（2026-11-11 实测）**：`events.db` 的 WAL 被撕到 schema 首帧时，旧 `EventLog.__init__` 照常 `executescript(_SCHEMA)` 建表——打开成功、`events` 表 0 行、原 WAL 被丢弃，历史静默蒸发，`recover_run` 据此报告"没有任何未决调用"。修法：先记下"库存在且有内容"，connect 后、建 schema **之前**读 `sqlite_master`（这一步触发 WAL 回放，是"有没有历史"的唯一事实来源），读不出就抛域错误；`PRAGMA journal_mode=WAL` 必须后移（否则可能先毁 WAL 再检查）。推论：**任何"打开即建表"的初始化都自带一条 "坏库→空库" 的洗白路径**，隔离门禁要放在建表之前。
- **探活/心跳自己也会占资源，backlog=1 会被自己堵死（#63 根因）**：`multiprocessing.connection.Listener` 默认 backlog 是 1，而 `probe()` 是"连一下就关"的**未认证**连接——服务端必须先失败一次握手才能腾出队列位；队列满时 macOS 对 AF_UNIX 的 `connect` **立即 ECONNREFUSED**。表现是"端点文件存在、进程活着、probe 连不上"，连续两轮都被误判成"就绪早于 listen"。修法：backlog 调到 64 + 就绪前自连一次完整握手 + `probe_result` 带 errno/重试。教训：**探活链路上的每个"轻量动作"都要算它占用的队列/锁/句柄**；"本机偶发"先看容量，别先怀疑逻辑。
- **"恢复判据"要覆盖"目标本来就不存在"这件事**：`delete_file` 的 applied 状态是"文件已删除"，而恢复的 applied 分支无条件 `size()` 目标 → `FileNotFoundError` 穿出 `recover_run`。#54 把 delete 纳入注入矩阵才暴露：**哈希一致 ≠ 目标存在**；applied 的目标可能是 `absent`，output/凭据都要按删除形状写。
- **库契约接好 ≠ 产品接线（T3 的教训）**：撤销意图日志提供了 `outstanding()`/`resume()` 契约、也有 7 条崩溃续做测试，但 `recovery.py` 属于另一工作流，合并后**没有任何产品代码**调用它们——恢复照样报告干净、写入照样能进。补接线的 5 条测试（`tests/test_undo_recovery.py`）和负控（去掉接线 → 4 failed）证明：**"有库 API + 有单测"不等于"产品里生效"**，跨模块的收尾必须点名到"哪个调用点"。
- **测试夹具不能模拟不存在的历史版本**：`EventLog` 改成 fail closed 后，两条迁移用例红了——它们用裸 `sqlite3` 造"只有 leases/approvals 一张表"的库。但真实旧库一定带 `events` 表（`_SCHEMA` 从第一次提交起就建它）。修法是把 fixture 改成"先建完整 schema、再把那张表降级"，而不是放宽产品判据：**别用一个不存在的历史版本给自己作证**。
- **`detach -force` 不是断电**：真实 HFS+ 卷强制摘下后重挂，验证了"fsync 后的产物可恢复"，但手工探针显示 **20MB 完全未 fsync 的数据照样存活**——所以这个手段**不能**制造"丢页缓存"，也不能用来宣称"未 fsync 一定丢"。写文档时必须把"能证明/不能证明"分开列。

- **"契约里写了"不等于"执行侧在看"（2026-11-11 实测）**：`SPECS` 里早写着 `argv.items` / `minItems` / `limit.minimum|maximum`，但 `validate_args()` 只看顶层 `type / required / additionalProperties`——嵌套约束**从未执行**，模型给 `argv: [1]` 也放行。这比没有 schema 更糟：它让模型与审批人以为检查过。判据要钉"非法值真的被拒"，不是"schema 里写着 items"。
- **别把"未实现的关键字"当注解忽略（同轮）**：JSON Schema 里 `oneOf` 未知实现时可以合法忽略，但准入边界不能——注册表写着、执行侧不看就是撒谎。做法是**封闭白名单 + 注册期拒绝**，让"多写一个关键字"在 import 时就红，而不是等模型调用。
- **加一个身份字段进摘要时，先问"历史摘要会不会变"（同轮）**：`description_hash` 若无条件写进 `to_canonical_dict()`，历史库所有审批 / grant / 派发摘要集体变化，"更安全"的表象下是把既有授权链作废。正确做法是条件字段 + 按值钉死旧摘要的测试。
- **预占/结算类账本必须用数据库原子性，不能"先读后写"（同轮）**：并发抢最后一份额度时，普通读-改-写会让两边都读到旧值。`BEGIN IMMEDIATE` 内检查+插入+扣减是唯一可靠做法；用例要开**两条独立连接**模拟两个进程，并做"去掉事务 5/5 次变红"的负控。

- **加了列 ≠ 接通了路径**（N3）：`memories` 加 `layer` 后，INSERT 占位符少一个、`search()` 手写 SELECT 漏 `m.layer`、FTS 装配少一列——三处都会在运行时炸（`IndexError` / `19 values for 20 columns`）。加列时按"建表 → `_MEMORY_COLUMNS` → INSERT → 每条 SELECT → `_row_to_memory` → 迁移"逐点核，别只改建表。
- **自述的守卫要能被一条测试打红，否则它只是注释**（N3）：`consolidate()` 的 `proof_count` 就位校验最初绿得毫无意义（测试只测了生成器自身的一致性），补了"塞一个坏页"的用例才算数；执行侧的 workspace 守护经派发路径根本不可达（派发层先拒），就在 PR/SPEC 里**如实写"没有测试能单独打红它"**，而不是假装覆盖。

- **推送要看远端，不要看退出码**（N3/N6）：worktree 一度处于 detached HEAD，`git push` 仍以 0 退出（它推的是那个没前进的分支引用），于是"已推送"是假的。以后每次推送后 `git ls-remote origin refs/heads/<branch>` 核对。
- **同名断言会把真问题掩盖**（N3 复审两次抓到）：`tool.intent` 里有 `tool_id`，只断言"事件里有 intent/result"会被别的工具的事件满足；断言必须带 `payload['tool_id'] == ...`。

- **CI 慢机器会把「窗口」放大成真 bug**（N6）：`open_control_run` 先 enqueue 再 claim，本地毫秒级窗口在 ubuntu CI 上被 Daemon 驱动线程的 `claim_next()` 抢走，`memory.remember` 报 `internal: job 仍由活着的 owner 持有`。修法是 `JobStore.enqueue_and_claim()` 把入队与认领放进同一个写事务；判据用「另一条连接在 enqueue 瞬间抢」的确定性补丁，而不是开线程碰运气（并发版本 8 次一次没撞上）。

- **预算底层通过不等于真实调用已接线（2026-11-13 独立复审）**：账本支持 `micros`，但普通/摘要预占不带这个维度时，零金额上限也会漏发。必须在真实 adapter 的受控 transport 边界观察预占，并核对实际费用、重试与父子链。新写入口拒绝负数也不能保护旧负计数；升级后读取和新准入必须 fail-closed，不能把“历史数据”当作授予额外余额的理由。未知 token 与未知金额是两个维度，修正未决计数时分别用精确断言，不能为保住旧数字漏记未知费用。
- **内存统计修好不等于终态已记住（2026-11-13 B3 二审）**：摘要失败后未知计数在 `RunResult` 中正确，但若依赖下一普通调用成功再存游标，预算立即拒发会让 `run.end` 读旧值。回归必须覆盖“失败后马上停止”，并分别核对内存、持久游标、终态与重开结果；不能只凭继续运行的成功路径宣称修完。

- **证据也有成本，超过代码就不再是「更严谨」（2026-11-13 系统 debug 复盘）**：一天合入 7 个 PR（#130–#136），`3648d79..cefa1e3` 共 256 文件、+53,367/−971 行，其中 `docs/` 192 文件占 +43,812 行（约 82%），代码与测试合计约 9.5k 行；`docs/evidence/` 内 8,819 行 evidence-index、6,927 行 commit-binding、4,860 行 source-object-map 属机器可生成物，而原始日志/截图本就归档在被忽略的 `.omc/artifacts/`。教训：**证据能生成就别手写**，仓库只存索引、结论与不可再生原件；一条证据若不能让人在一分钟内复核某个具体断点，它就是负担而不是证据。留账是为了让人敢下判断，不是把判断淹掉。
- **门禁时长贴住 job 上限本身就是可靠性风险（2026-11-13）**：Windows 全量 23 分 13 秒、job 上限 25 分钟，只剩约 2 分钟余量；四项 required 加 strict 下串行合并会重复触发整轮 CI，所以本轮策略是「本地各自收口后集中推送」。教训：把检查列入 required 只解决「敢不敢合」，不解决「多久能合」；并且必须把**超时取消**与**断言失败**分开记，前者不能写成后者，也不能靠加时或加重试掩盖。
- **能推翻结论的实验要留提交，但不进产品（2026-11-13）**：`codex/debug-windows-diagnostic`、`codex/debug-host-stop-diagnostic` 刻意停在分支——纯标准库反例证明「原生定时转储会在动态 frame 负载下崩」，但那是证据不是产品。教训：反例实验要有自己可复现的提交与 run，可以永远不合 main；历史误红保留原样，「当时现场」与「本次结论」分开写，既不倒推找回已丢的 stderr，也不把实验成功当产品通过。
- **「只在某平台红」先假设是代码（2026-11-13 issue #50 收口）**：长期结论是「生产不受影响、必须实机才能定位」，实际根因是 `load_authkey()` 缺 `O_BINARY`：Windows 文本模式把随机 `LF` 扩成 `CRLF`，创建者返回原 key、后续进程读到另一个 key；GitHub 托管 runner 稳定复现。教训：跨平台默认值（编码、换行、文本/二进制模式、路径分隔符）一律显式写死；把平台差异当环境噪声，一个真缺陷可以挂上几个月。
- **第二读者要换的是上下文，不是措辞（2026-11-13）**：本轮复审在新会话 + detached 工作树执行，作者不为自己批准，只按已冻结的验收项逐条对照，记录完整 SHA、范围、命令、输出与未决项。教训：同一上下文里「再检查一遍」用的还是同一套错误假设；复审的成本主要在**重建独立观察环境**，省掉这一步就等于自审。
- **安装路径的符号链接会变成“App 坏了”（2026-11-15）**：macOS `/tmp` 是指向 `/private/tmp` 的符号链接，Tauri 启动时按自身安全策略拒绝可执行路径中的 symlink，表现为 `resource_dir()` 返回 `UnknownPath`。安装脚本必须在移动 app 前把 prefix 解析为 `pwd -P`，同时把资源目录错误改为可见的启动失败页而不是 panic。测试若直接拿 `/tmp/...` 当“干净安装路径”，会把测试夹具问题误报成产品崩溃。
- **完整测试也会被宿主 provider 环境污染（2026-11-15）**：第一次全量 3377 项中唯一失败是 Anthropic 模型身份变成未知 vendor，根因是宿主 `ANTHROPIC_BASE_URL` 指向中转站；隔离该变量后同一用例转绿。提交门禁必须只在测试子进程显式清空模型/端点/数据根/故障注入变量，不打印值、不修改全局配置，并保留“首红是环境污染”的分类。

## 6. 流程规则（机械 + 纪律）

- **单人维护口径**：0 人 GitHub 批准、自己合 PR 可以接受；CODEOWNERS、issue assignee 与 GitHub milestones 按协作需要启用，不强制重复维护。高危改动仍须另一会话或人提供独立证据，绑定被审提交完整 SHA、验收范围、命令、输出与未决项；后续实质改动须重审对应范围。新增提交说明原因，合并说明保留原因与证据，不为整理格式改写历史。

- **门禁是机械的**：`main` 分支保护——走 PR、CI 绿（`strict`）、`enforce_admins`（所有者也绕不过）、禁强推/删除。本地绿不算数。
- **高危改动独立复审**：碰授权/租约/并发写/事件日志/计费，须由「第二读者」（另一会话或人）复审；不读作者自查结论，只按计划验收项逐条对照，并给出**可复现探针证据**。
- **复审结论分三层**：已修 / 部分修 / 未修；后两者不得当作已解决，登记为已知限制并跟踪销账。
- **契约变更单独提交**：`docs(contract):` 单独 commit / 单独 PR，body 写「原方案 → 新方案 → 为什么 → 影响哪些代码」，不夹带代码。
- **文档对账**：见 §0 与 §5；提交前跑 `uv lock --check` + `uv run --frozen pytest` + `uv run --frozen ruff check .`。
- 细节见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 7. 已知限制 / 未决问题

- **可安装发行版仍是 L1 作者候选，不是 G3**：`3863887` 的独立复审为 `UNFIXED`
  （两条 HIGH），`5873720` 与 `7b4cc7a` 为 `PARTIAL`；当前后续提交针对这些发现补了
  Host 崩溃恢复、owned/attached 退出清理、supervisor child-handle 竞态与 source
  bundle 完整性校验，但还没有任一独立读者对最终精确 SHA 给出 PASS。最终 docs-only
  SHA 的 required CI、macOS arm64 产物/安装/GUI、合并后 main 自身 CI 与四目标 release
  workflow 都仍待执行。未签名、未验证的设备平台、自动更新、恢复 B/C、SLO、长稳和
  issue #145 同样保持未完成；当前不得宣称 L2 或 G3 已发布。
- **恢复 B 仍只有 T1 平台基础**：当前没有真实受管 `apply/review/resume/abort` CLI/API、P0–P5 复制/发布/清理链或完整 B01–B09 验收。T1 候选只证明身份、锁、registry 与当前 EventLog/迁移/备份入口的连续 access，不能解除普通业务 held 状态。
- **T1 入口闭包边界**：EventLog、迁移和离线备份已接 composite access；三材料/其他后台写者的完整入口归属索引仍未全部收口，文档不得把“核心连接已持锁”扩写成“所有旧/raw/material writer 已被约束”。raw SQLite、旧程序和同 uid 直接改文件始终在协作锁声明外。
- **Windows 原生结果待回填**：`scripts/restore_t1_windows_probe.py` 和 Windows 测试必须在 GitHub 原生 runner 实际通过并产生 artifact；在 run/SHA/artifact 绑定前，Windows T1 行为保持 `NOT_RUN`。Windows Server CI也不代表 Windows 10/11 用户设备或 ARM64。
- **c2cf双红与诊断边界（2026-11-14）**：M2活跃renew context的32ms/63ms观测差不证明单次5秒持锁，run.end的none_applied也不证明execution.started后文件未动；不按无证据假设修改锁策略。MCP此次CI在returned断言停止，后续unknown/replay断言未执行；本机真实屏障只证明原测试能混入审批和落盘等待，不定位原Windows卡点。最终测试计时/清理和M2起点/调用链诊断已独立限定PASS、本机完整门通过；实际Windows与正式对象仍另验，不借PR149绿色销账。READONLY和注入7ms仅验诊断；[本轮记录](WINDOWS-CI-TIMING-2026-11-14.md)保留范围。

- **M2诊断验证边界（2026-11-14）**：766a候选已在本机真实POSIX别名输入修复诊断caller丢失，并独立核根外同内容副本仍external；Windows junction分支本机未执行。诊断成功静默、失败附note，但包装及resolve有非零时序成本；它只观察已装配本进程context，不是OS锁owner、外部写者或精确COMMIT起点。新完整本机门不覆盖原Windows根因，正式对象/原生CI另核。

- **正常收尾锁错误未修（2026-11-14）**：main b450原生push CI失败已独立确认；同一queued/五事件指纹可来自两个不同事务BEGIN，当前没有原CI底层帧、SQLite错误码或持锁轨迹，实际根因 `NOT_ESTABLISHED`。本轮测试观测只覆盖已装配的本进程事务，不能排除未观测的外部写者，也不能用本机或PR绿覆盖原main红；恢复B契约推送和产品保持暂停。

- **当前数据生命周期边界（2026-11-14，本次提交前）**：低磁盘 PR #144 已合 main8f2a，PR及main自身首次六job CI均已独立核验通过；issue #145 的旧Run failed原因未知，未关闭。恢复B纯契约PR #146首次CI另有审批后删除未发生失败，issue #147根因未建立，原红仍阻断该提交合并。本片只补有界失败诊断，短关键字段独立验收与最终本机门已通过，不把它们当作新原生CI或根因修复；后续绑定详见[诊断记录](APPROVAL-CI-DIAGNOSTICS-2026-11-14.md)。恢复apply/review/resume/abort、解除与完整旧Run恢复均未实现；旧作者时点保留历史含义，当前以精确提交及交付记录为准。

- **main b450 自身 CI 未通过（2026-11-14）**：run43567298082 的 Windows 片1在正常收尾测试第55轮出现 database is locked，Run 留 queued、无 run.end，required 汇总失败。原生首红保留，完整终态独立核验与根因待查，不能用 PR148 的绿色或本机3186项通过销账；暂不推进恢复契约推送或 B 产品，也不宣称已证明与旧 #145/#147 同因。

- **当前低磁盘边界（2026-11-14）**：PR144及main8f2a自身原生CI已通过并独立绑定，精确结果见§2。issue #145旧Windows正常收尾Run failed原因未知/未修，不能排除PR回归；root已显式接纳内部研发风险，绿色不等于长期稳定或无条件整PR批准。BASE-B1既有CLI预算装配缺口未修。恢复B当前只有[契约文字](restore-held-contract.md)，全部产品验收NOT_RUN；C解除/完整旧Run恢复、容量上限/GC、真实付费任务、10h soak及普通用户发行仍待。逐位置空间探测不是配额；Windows Server CI不代表Windows10/11、ARM64或桌面实测。以下作者/失败记录均为其历史时点，不覆盖本条。

- **审批诊断提交前的数据生命周期边界（2026-11-14，历史记录）**：低磁盘 PR #144 已合 main8f2a，PR及main自身首次六job CI均已独立核验通过；issue #145 的旧Run failed原因未知，未关闭。恢复B纯契约PR #146首次CI另有审批后删除未发生失败，issue #147根因未建立，原红仍阻断该提交合并。本片只补有界失败诊断，短关键字段独立验收与最终本机门已通过，不把它们当作新原生CI或根因修复；后续绑定详见[诊断记录](APPROVAL-CI-DIAGNOSTICS-2026-11-14.md)。恢复apply/review/resume/abort、解除与完整旧Run恢复均未实现；旧作者时点保留历史含义，当前以精确提交及交付记录为准。

- **T1装配/恢复继承缺口已在T2独立范围内修复（2026-11-14）**：固定已审T1源上的六个受影响测试原样复现失败：4个跨进程pending MCP恢复缺发现注册表、dead-at-dispatch夹具启动前取server、摘要硬退子进程漏传真实memory。T2只补pending MCP按需准备和两个夹具实际实例；原硬退/unknown/对象阻断断言保持。另原并发审批用例固定T1通过，新增执行上下文门后测试两线程手装配缺context，最薄补同连接Snapshot/authority/guard后通过，未放宽生产校验、竞争和一次消费断言。上述不撤销T1此前已审范围；T2 的完整冻结内容与窄 delta 已由第二读者独立 PASS，但仍不扩大为 T3/整功能通过。

- **低磁盘 T1 C2 尚未独立销账（2026-11-14）**：旧候选的新 policy 写读类型不闭合；round1 只收紧新 `run.start` v2 的严格对象语义，保留历史 v0/v1 schema、其他事件和字段的 JSON Schema integer 语义。不能放宽对象 `.of` 或把已写坏资格补为 effects/pure_only；本轮作者467项联合通过与负控不替代独立复核，旧800项结果也不补证该缺口。

- **低磁盘 T1 C1 尚未独立销账（2026-11-14）**：旧候选最终空间拒绝留下控制 Task/创建事件，独立复核已否定这一范围的旧作者“已修”表述；round1 的同事务修复及 takeover 真子进程实例适配只有作者窄测/负控证据，必须绑定新冻结交第二读者复验。最终空间复测仍在创建事务内，沿用既有嵌套事务异常须传播到外层才回滚的语义；未扩大为整个控制入口事务，也未变更租约接管、授权或执行侧门禁。

- **低磁盘 T1 候选尚未独立验收（2026-11-14）**：创建侧的 `dispatch.disk_space_low/unknown` 已与真实产生路径、代码/schema/PRD 清单同步接线；`dispatch.run_effects_disallowed` 留给 T2 真实执行守卫，不提前登记为 unused。资格损坏仍报原异常；当前纯读路径的材料缺席与创建拒绝仅属 T1 定向候选证据，不代表直接派发、执行器、网络/MCP 发送或恢复封口已交付。

- **低磁盘执行与可达入口历史缺口（2026-11-14，T3候选已补）**：T1 的 TaskStore/Loop/控制创建与逐类型 v2、T2 的执行侧空间与资格复核/已有 Run 纯读/模型/停止控制例外均已形成冻结候选并由第二读者核验；T3 作者候选已补 CLI/RPC/UI 显式模式选择器。实际位置逐个探测，仅相同 canonical 路径去重，不借同卷假设跳过嵌套位置；受控叶子注入不冒充真实多卷/挂载或 Windows 用户设备实测。T3独立内容和修后本机统一全量已通过；最终窄增量/对象绑定与原生CI另核，恢复 apply/隔离、容量与执行 GC 不在本片。

- **PR #137 合并已确认，merge SHA 自身 push CI 仍缺失（2026-11-14）**：精确 PR 原生 CI 已通过，真实 main 已含其合并对象。早期 REST/GraphQL OPEN / merged=false 是历史观察；后续 REST 已返回 `closed / merged=true / merged_at=2026-11-13T21:26:14Z`，合并事件销账。同一时点按 `6c291a6` head 查 workflow runs 为 0，故只可继承 head CI 结论，不能声称 merge SHA 自身 push CI 通过。停止重复合并操作并保留早期异常原件，后续提交的 CI 不能补证这个缺失事件。该异常不放宽四 required、25分钟上限或独立复审；Windows runner 通过也不证明 Windows 10/11、ARM64、安装/ACL 或用户设备体验。下方旧“未通过”条目是各自历史 head 的结论。

- **Windows 分片 C1 整改候选（2026-11-14）**：独立读者用同一真实fixture证明原 wrapper 收集错误 exit2、v1分片 exit3；作者新增精确对照又复现收集晚期 `KeyboardInterrupt` 被改成3。v2在已有收集失败时保留原生流程，不对部分collection分组；未进入分组的收集中断也不触发最终列表异常。失败报告仍不满足session0/完整覆盖，汇总拒绝；无错误时空集合/空片/晚过滤守卫保留。作者71项定向已过；第二读者12个真实受影响场景已确认C1已修、部分修/未修/内容未决均0，正常覆盖及空片/晚过滤/缺阶段/失败拒绝守卫仍在。最终537文件组合lock/full/ruff通过（2987 passed / 8 skipped），正式SHA绑定与新原生CI另核，不将“非零即可”当退出码保留证据。

- **PR #137 Windows 整套仍未通过（2026-11-14）**：H3 `e324edeb9c57cd9e8fe851a2657da8420e7838a2` 的 run43474954367 attempt1 仍在25分钟取消，另三项绿；三个 CLI 已通过，2765 PASS / 31 SKIP 只是部分输出。双独立 runner 文件分片是后续基础设施候选，新原生 CI、完整覆盖、独立实施复审与实际耗时仍待核；节点数平衡不保证25分钟完成。不得加时、移除 required 或借本机绿色合并；输出窗口不足以判定性能根因。Windows Server runner 也不代替 Windows 用户设备、ARM64 或安装体验验证。

- **基线收集不是执行或批准（2026-11-13）**：该开发工具只检查显式输入的完整结构并保全字节，无法鉴别完整伪造、证明历史命令/退出码，或保证发现先改后还原的瞬态变化。收据与原件包含私有材料，只能分享固定字段摘要；摘要单独存在不是完成凭证。发布后被终止仍可有合法收据，不提前记自身 exit0；不承诺断电持久性、同 uid 防篡改或 Windows ACL。真实模型、公网任务、十小时长跑、SLO 和用户设备验收仍须另验，不能借收集器本机通过收口。

- **第二轮修复后的保留边界（2026-11-13）**：模型跨源redirect拒绝，但同源POST307/308仍是urllib既有HTTP错误，仅302正控已验；网络响应总deadline修复不等于可中断DNS系统解析；IPC认证握手期限、命名管道跨用户ACL、Windows10/11/ARM64用户设备未由本机证明。备份Receipt材料/原生parser/事件版本/runs全集核对已修候选，仍未交付审批租约全历史重建、恢复apply/隔离、低磁盘与GC。最终提交和CI见[第二轮总账](DEBUG-ROUND2-2026-11-13.md)，下方G3“旧backup边界未修”为其历史切片时点，不能覆盖本条。

- **G3本切片边界（2026-11-13）**：实际EventLog准入、备份绑定迁移和显式CLI已有内容独立验收及最终本机全量；本条提交前快照时新CI待核，后续状态见对应实现PR正文及后续独立原件。0→2仍只改变迁移机制；restore hold/journal、实际apply、记忆隔离、低磁盘保护、保留/GC未交付。v2备份不带进度，不能补造/复制旧绑定绕过恢复后准入。另一轮只读debug已确认的Receipt材料、snapshot/undo语义、未来版本、投影缺行、manifest类型等旧backup/restore-plan边界未在本片修；后续恢复或GC前须收口，不能借完整digest宣称全部备份语义已修。

- **T2 候选不等于迁移产品接线或 G3 完成**：本机实现仍待第二读者按冻结四文件做具名变异验收；T3 的 `data migrate` CLI 尚未接，整合全量与原生 CI 未跑。恢复 apply/隔离、低磁盘和 GC 仍缺；维护锁只约束合作式调用，当前仅适用于两步机制迁移，边界见 [T2 记录](DATA-ADMISSION-T2-2026-11-13.md)。

- **恢复跨库窗口仍存在**：本地unknown与阻断现同事务，但事件库与authority是两个数据库；本地提交后、authority登记前死亡，跨数据根可见性依赖该根再次恢复补登记。命令没有物理沙箱，unknown恢复只阻止安全重放，不证明已识别任意外部副作用。

- **预算历史数据与金额上界**：本轮拒绝读取/使用已有非法负计数，没有迁移、洗白；真实数据库异常需单独取证。旧版已误结清但只剩合法正数量的 partial 记录不能凭空重建未知信息。估算模式不是硬上界，当前价格表无法提供所有计价档位的金额硬上界，`upper` 加金额限额会明确拒发；缺价且任一祖先受金额限制也拒发。默认无金额限额仍记录未决费用，零金额预留不表示免费，未知用量不自动退款。

- **C-007已修，仍有边界（2026-11-13）**：正式b6对象已独立绑定且四required通过；5e7原生Windows专项20 passed/1 skipped、helper2 passed、7111真实拒读负控与实际worker/launcher/Daemon退出证据经第二读者核对通过，见§2及I/O报告。dd49六红、cp1252入口缺口、旧monitor取证错误、7111/d6ee/a624反例均保留为历史，不倒推找回已丢弃stderr或冒称同栈。HostInfo guard只保护合作进程短读写/条件删和条件发布→monitor实际启动→HTTP服务启动，不是整个启动或运行期排他锁，也不是同uid安全边界；port0期间可被接管，败者须拒绝发布/删除。外部程序/杀软持续拒绝仍明确失败，Windows10/11/ARM64用户设备仍未验证。

- **CI线程栈边界（2026-11-13）**：为避开已在Windows3.12.16独立重现的原生定时frame竞态，Windows使用Python线程定时采样；C扩展长期占用GIL时无法保证60秒出栈。fatal handler仍保留，25分钟job上限不变，但两者不是该缺口的等价替代。未验证所有C扩展/GIL死锁、真实百线程并发或stdout/stderr同时外部阻塞。7111正式Windows矩阵已逐项通过31项包装器回归，三个60秒栈后相应用例继续通过；整轮因C-007失败。这些结果和临时原生实验success都不替代后续提交的业务套件通过。

- **I/O debug 范围（2026-11-13）**：已修复的有界读写不等于进程隔离或网络带宽硬配额；Windows 命名管道 ACL、Windows 用户设备/ARM64、真实模型驱动 UI、安装与升级仍未验证。C-004是已确认的既存POSIX发送关闭竞态，本机回归不代替新提交独立复审与Ubuntu CI；Windows的不同发送路径保留既有测试，不用POSIX调度探针代言。当前工作区的本机绿不冒充独立批准或云端绿。逐项证据与尚未验证范围见 [I/O debug 记录](DEBUG-IO-2026-11-13.md)。

> 本节含按日期保留的阶段交付记录，其中“未做”只描述该阶段；后续交付以 §2 和相应专题文档为准。平台与合并门禁以 §2 的具体提交 / CI / 远端配置为准，旧失败数字不代表最新提交。

**PRD 2026-11-11 工程评审新增要求：首轮交付留账（2026-11-11，PR #79–#84；对账见 [PRD-REVIEW-DELIVERY-2026-11-11.md](PRD-REVIEW-DELIVERY-2026-11-11.md)）**

- **G1 已收口（2026-11-11，PR #91 / #92 / #96 / #97 / #99）**：§5.2.3 通用工具准入（封闭 schema 方言、注册期拒绝、嵌套校验、描述绑定、身份字段双闸、URI 拒绝、准入在派发前）+ 五种资源种类与外发许可（外发恒需人工批准、无执行器种类派发前拒绝）+ 有界摘要/内容引用/分页读取（任务可见范围 + sha256 复核）；§5.6 共享预算账本（**默认记账**、原子预占、幂等分维度结算、未知不记零、父子链、取消传播）+ 硬上限模式的**可验证上界**。**仍不成立**：① 网络 / MCP / 委派的**执行器**（G2），所以「私有内容未经外发许可不发包」只能记 **unknown**（闸门就位、可拒绝，但没有外发面可验证「发了什么」）；② `run_command` 仍只受显式 `ExecPolicy`，不经过隔离门；③ 硬上限的每消息固定开销不在上界内（靠 `overrun` 暴露）；④ 没有任务级外发白名单（`outbound_allow`）；⑤ ~~账本没有 UI 展示~~（**2026-11-14 W8 部分销账**：Run 事件面的预算面板已交付——限额 / token 已知与未知分列 / 已知与未知费用分列 / `dispatch.budget_refused`·`dispatch.subtask_limit` 拒绝记录；**账户级账本视图仍未交付**——上限设置、未决预占清单、下载字节与子任务数量维度、`overrun` 标志不在事件里，需要 `control.py` 新只读端点，契约见 [SPEC-W8](SPEC-W8-BUDGET-SUBTASK-UI-2026-11-14.md) §6）；⑥ 保留期/GC 与跨机器内容引用未做。见 [tool-admission.md](tool-admission.md)、[resource-kinds.md](resource-kinds.md)、[content-refs.md](content-refs.md)、[budget-ledger.md](budget-ledger.md)、[SPEC-G1-CLOSURE-2026-11-11.md](SPEC-G1-CLOSURE-2026-11-11.md)。
- **⚠ 控制面不是模型隔离边界（F2/exec，2026-11-11 复审确认）**：v1 没有进程级沙箱，`run_command` 以当前 uid 执行子进程；子进程可读 `run/authkey`、连 daemon IPC、跑 `tianshu` CLI，等于能以「人」的身份调控制面并自己落审批。所以"审批需要人"这条在当前实现里只挡**没有** `run_command` 的任务；要真隔离需要进程/文件系统沙箱或独立 OS 用户（G1/exec 的后续工单）。别把 N6 的控制面端点写成"模型无路径"。**F2 收口后仍然成立**：F2 做的是「后端可插拔 + 实测门 + 受信任执行可见」，不是沙箱；`run_command` 仍是同 uid 子进程，四边界探针在本机只有 `process_tree` enforced，任何非空 `isolation_requires` 都会 `unsupported`（见 [F2-sandbox-policy.md](F2-sandbox-policy.md)）。
- **F10 N6（2026-11-11 端点 / 2026-11-12 面板与检索，PR #100 / #126）**：Host/IPC 端点（8 个记忆 RPC）+ **`memory.search`（第 9 个）+ React 记忆面板**都已落地（列出/检索/确认/忘记/擦除；条目带六项判据与 `score`/`score_kind`，`stale` 不可判定就省略；正文纯文本；UI 不做乐观更新、冲突只刷新重读）。**端点仍不是授权边界**：v1 没有进程级沙箱，同 uid 的 `run_command` 能读 IPC 凭据、跑 CLI 并自己落审批——跨本地用户只有 POSIX 文件权限（Windows 命名管道 ACL 未加固、未验证）；"无身份拒绝"在单用户本机模型下**无法构造反例**（只有代码/文档论证，不声称已验证）。
- **§7.1 已交付但仍有边界**：默认关；跨 Run 不复用（材料身份含 `seq`）；不做增量/分层摘要与自动刷新（CLI 未接）；**没有摘要质量评测**（"比确定性裁剪更好"不许宣称）；提示注入未解决（"不是指令"的包装只是提示级缓解，执行侧闸门才是强制点）；纯事件导出不足以回放摘要轮次（缺记录报 problem 不重生成）；本机夹具里摘要**更贵**（token 324→744、请求字符 4054→7887）换关键约束不丢；保留期/GC 未做。见 [context-summaries.md](context-summaries.md) §8。
- **F10 L2/L3（N3，2026-11-11；冲突消解 2026-11-14 W7 更新）**：只落地**确定性**合成，LLM 合成未做（W7 已交付契约增量分析 [SPEC-W7-L2L3-GENERATIVE-2026-11-14.md](SPEC-W7-L2L3-GENERATIVE-2026-11-14.md)——另立契约 + W6 外发 gate 对齐是前置，实现 PR 等在途 #154 落地）。**没有 L2/L3 vs L1 的检索质量评测**——谁都不能宣称"读页比直接读 L1 更好用"；跨作用域合成、对话抽取都没做；**冲突消解（W7，2026-11-14）已落地确定性裁决**：L2 页内对"替换型差异"成员对（同 kind、互不为子集、token 规模相等、重叠系数 ≥0.6）按 origin→seq→memory_id 三级写死规则在页尾标注"以谁为准"，**不删行、不改 L1、不改召回集合**——检测是召回导向的（抓不住低重叠语义矛盾），标注说"候选"不断言"矛盾"，淘汰式处置（覆盖/删除语义）需审批契约、未做；L3 冲突裁决未做（偏好页是人给的，机器按 seq 裁决语义越权 + 预算交互）；L3 仅 user 作用域；执行侧 workspace 守护经派发不可达（纵深冗余，无单独测试）；`memory.consolidate` 不进审批表（`ACTION_CREATE`），将来若做"合成后自动 confirm"必须另立契约。判据 `tests/test_memory_conflict.py` 13 条（现有 `test_memory_distill.py` 22 条零改动），负控 4 条实跑红还原。
- **G2 网络阶段已交付，但边界必须一起读**（2026-11-11，PR #110 / #112）：`outbound_allow` + `network.fetch` + §10.5 联网研究闭环（本机夹具）。**仍不成立**：只支持 GET、**没有检索/搜索 API**（§10.5 的"检索公开来源"在真实意义上未闭环）；**真实公网研究与真实模型复跑未验证**（判据全跑 loopback 夹具）；重定向只许同源；响应上限 1 MiB / 摘要 4 KiB，二进制正文只有有损摘要且没有文本分页读取；取消只能阻止新请求/新读（在途 TCP 字节无法撤回）；**DNS rebinding / 解析后二次校验未做**（白名单显式声明源，解析到私有地址会照发）；每 Run 抓取配置是进程级常量；~~无速率限制/重试~~**已交付（2026-11-14，`feat/net-rate-retry`）：连接失败自动重试（只重试 TCP 未建立的 `net.connect`/`net.connect_timeout`，发送后失败绝不重试——服务器端连接数测试钉住授权边界）+ 进程级速率下限（默认 1.0s 可关），见 [SPEC-G2-NET-RESILIENCE](SPEC-G2-NET-RESILIENCE-2026-11-14.md)**；内容分级未实现（审批展示完整 URL 由人判断）；保留期/GC 未做。MCP **已交付**（#116–#119；真实第三方 server 未验证、无沙箱），**记忆阶段也已交付**（#126 `memory.search` + React 面板 = 可查看；#127 `RU-M1@1.0.0` 跨任务召回 = 可证明收益；见 §8 的 2026-11-12 记忆阶段条目）。**子代理 D1 + D2 + D3 已交付**（D1 排队面：`max_subtasks` + `subtasks` 实体/父子链 + 四个工具 + 数量/局部限额 + 预占；D2 执行与收束：Worker 驱动子 Run、父挂起/唤醒、取消传播、父目标 fence、结果回填、回放不重跑；D3 真实用户任务评测：`RU-D1@1.0.0` 只读子任务 + 父任务逐字汇总引用 + 只读边界/失败不编造判据）。D3 是**脚本模型**评测，不是真实模型分数；只覆盖一条公开委派任务 + 一条测试内失败路径。**仍不成立**：~~子任务只读~~（2026-11-14 起默认仍只读、可显式声明文件写，见下方 W10 条目）、深度 1（契约分析已交付，实现待契约 PR）、单机同 Worker 池、取消不退款、读不到输出时摘要为空、~~无 UI 页面~~（**2026-11-14 W8 部分销账**：父 Run 事件面的子任务面板已交付——`subtask.spawned`/`subtask.finished` 合并出限额 / 状态 / 用量，进行中不冒充终态、用量未知不当 0；账户级委派计数视图仍缺 control 端点，见 [SPEC-W8](SPEC-W8-BUDGET-SUBTASK-UI-2026-11-14.md) §6）、Windows 未验证。见 [network-g2.md](network-g2.md) §7 与 [resource-kinds.md](resource-kinds.md) §8。
- **W10 子代理写权限已交付（脚本模型判据），深度>1 与 fork 只交契约分析**（2026-11-14，`feat/subagent-depth-write`）：`subtask.spawn` 2.0 增可选 `write` 布尔声明（红线 8 版本化，缺省 false 与旧行为逐字节一致，RU-D1@1.0.0 样例不动）。`write=true` 的授权上限**恒为父绑定**：子 Task 逐条保留父绑定 mode（父只读的根仍然只读），子 Run 拿到 `effects` 资格（实际拿到可写绑定才给，父全只读则如实降级回 `pure_only`）；子 Run 里每个写动作仍走既有派发审批链（prepare 判定 → 人批准 → 执行侧重判 + 一次性消费 → intent → 守卫内执行 → result + Receipt），与父 Run 完全同构，无免审通道、无权限继承；出网/MCP/exec/记忆写/再委派依旧按既有编号拒绝。判据 12 条 + 负控 3 条（变异红→还原绿），设计与授权论证见 SPEC-W10-SUBAGENT-DEPTH-2026-11-14.md；**授权面改动，合并前必须独立复审**。深度>1（要改 C4 编号语义 + `task.created.depth` 封闭枚举）与 fork（要往子 Run 事件落继承边界）的契约增量四段式见 CONTRACT-C4-SUBTASK-DEPTH-2026-11-14.md，实现留待契约 PR。已知边界：父挂起等子任务时不释放写租约，可写子 Run 首写前要等父租约 TTL 过期走接管对账（正确性无缺口、有等待）；真实模型复跑与 Windows 未验证。
- **G3 未做**：无 Windows / mac-x64 / win-arm64 实机；安装/升级/回退、Keychain、平台 SLO、macOS 13+ 最低版本均未验证。历史 G3 收尾轮 Windows CI 为 **5 failed / 1509 passed / 29 skipped**（交付对账 §3），另有 25 分钟超时记录；这些是当时的失败证据。2026-11-13 修复分支的后续结果见 §2 与 [Windows CI 报告](WINDOWS-CI-2026-11-13.md)，不把历史失败当成当前结论；实机、安装与升级验收仍未完成。
- **数据生命周期**：`data backup` 通过协作式共享/独占锁排斥正常活跃连接；低层在线 `create_backup()` 不自动独占，旧版或同 uid 直接 SQLite 可绕过。完整材料校验与只读 `restore-plan` 已接 CLI，实际恢复/记忆隔离、换路径恢复、全产品版本准入与迁移启动门、低磁盘保护及 GC 未交付。原 `migrate.SCHEMA_VERSION=2` 是迁移机制演示，正常产品库仍为 `user_version=0`，不能据此直接拒写全部现有库。旧备份若缺未擦除记忆或 content/payload 材料会校验失败；不可把校验通过当成备份后删除历史完整的证据。
- **指标**：SLO 阈值未冻结；`rss_mib` 是峰值口径；~~指标文件无轮转~~**指标文件已按大小轮转（2026-11-14，`feat/metrics-rotation`，默认 16 MiB × 4 份、失败降级不丢样本，见 [SPEC-METRICS-ROTATION](SPEC-METRICS-ROTATION-2026-11-14.md) 与 [runtime-metrics.md](runtime-metrics.md) §10；80 MiB 上界是估算默认值不是冻结政策）**；~~`state` 响应可超 IPC 8MiB~~**`state` 响应已按预算装填（2026-11-15，`feat/state-response-bound`：4 MiB 预算内按 tasks→runs→approvals 份额两趟装填 + `tasks_truncated`/`runs_truncated`/`approvals_truncated` 截断标记（加字段不升版本）+ 单条目超预算 fail loud 抛 `internal`，绝不静默截断单条文本；`_approval_context` 改按 pending 集合所在 Run 有界读，state/approvals.list 不再随事件历史全表扫；见 [SPEC-STATE-RESPONSE-BOUND](SPEC-STATE-RESPONSE-BOUND-2026-11-15.md) 与 [M5-desktop.md](M5-desktop.md) §3.4 state 行）**；慢订阅者宽限窗内主循环会从落后游标重读。
- **事件契约**：`oneOf`/`anyOf`/`$ref`/`format` 注册期拒绝；每工具 output 子契约未做；CLI 未区分版本错误文案；历史上从未出现的旧字段组合读作 v0、写入口不识别。
- **评测**：真实模型未跑（无凭据），harness 分数不得当真实模型结论；报告没有「四维总分」（设计如此）；撤销的授权派发链路未进评测（只走 `SnapshotStore.undo` 数据安全预检）。
- **隔离**：探针不覆盖内核攻击面/配额/出站网络；`app_container`/`container` 后端 v1 未实现；本轮只落门，未接产品路径。

**收尾轮的兼容与并发留账（2026-11-11，PR #87–#88）**

- **Windows 仍无实机**：`fsync` 只读句柄降级、`time.perf_counter()` 启动计时、`OpenProcess(SYNCHRONIZE)`+`WaitForSingleObject(0)` 的 pid 判定，旧验收仅记录过当时的 Windows CI runner；当前完整套件证据见 §2（2026-11-13 run 为 Server 2025），不能据此扩大这些专项探针的验证范围；APFS/HFS+ 的目录 fsync 语义、命名管道 `accept`/`CloseHandle` 的唤醒时机、Windows 上的等价"握手期对端关闭"窗口仍未实测。
- **指标读者容错有边界**：只豁免"最后一段没有 `\n` 收尾"（这是正在写），中间行与带换行的坏行照抛 `ValueError`；指标文件没有轮转、没有 fsync 保证，读到的样本可能比磁盘少（不是假数，是"还没写完"）。
- **IPC 的握手期语义是客户端侧映射**：`Client.connect()` 把握手期 `EOFError` 映射成 `ConnectionClosed` 并停止重试；Windows 命名管道上是否会在同一窗口抛别的异常（如 `BrokenPipeError`）未实测，只有 POSIX 真 socket 与跨平台打桩两条回归。
- **一个上报口径错误已改正**：收尾轮曾把 #87 之前新模块的 Windows 失败写成"#50 家族"（见 §5 第一条教训），PR #87/#88 的正文、本文与交付对账都已按实测重写。

**边界与隔离**

- **`Workspace` 是路径边界，不是安全边界**：它回答"能不能碰这个路径"，不隔离进程、不防绕过。
- **`LocalExecBackend().capabilities.isolation == "none"`**：v1 从未交付物理沙箱。`MemBackend` 更弱——它连权限位、owner、符号链接都没有，**不是沙箱**。
- **D 系列约束**：安全不能靠用户配置；默认可写就必须默认可撤销；单用户假设显式声明。

**F12 凭据**

- **Keychain 仅 macOS**：W1 起 darwin 上 `keychain_macos.py` SecItem 直调已落地（CI 与本机真实通过）；其余平台零依赖下调 `CredReadW` 等无平台可验证，仍如实报 `unsupported`。**不得对未实现平台声称"密钥存在系统钥匙串里"。**
- 真实模型 key 只从环境变量读，永不进库；用过的中转 key 应自行作废。

**M0–M5 复审留账（[DEBUG-M0M5.md](DEBUG-M0M5.md)，2026-11-10；修复记录见该文档末尾）**

- **M2 静态疑点 → issue #53**：否决缓存只按 `run_id` 生效（同一 Task 新 Run 是否忘掉否决，取决于 `run start` 是否只由人/控制面触发，未逐条验证）；`revoke_consumed` 的判定依赖 dispatcher 状态核验，未逐条探针验证。**两条都未复现**，不许当成已确认漏洞，也不许当成已解决。
- **M1 `delete_file` 注入矩阵 → issue #54**：本轮（PR 修法）把 "kill×100" 的证据面从只覆盖 create 扩到 create + 覆盖写；删除类动作的恢复判据是 `absent`、撤销按 preimage 重建，另开一组断言跟进。
- **M4 未验证项 → issue #55**（真实换模对比 / Windows `evaluate` / memfs 评测路径 / 真超时）：2026-11-10 真实模型轮已销账三条，**只剩 Windows 实机一条**（2026-11-10 时点口径。**2026-11-14 对账**：2026-11-11 判据升 m4.5 后，真实换模复跑重新成为未决项——现剩 Windows 实机 + m4.5 真实复跑两条，见下文 issue #55 行与 issue 本体评论）。
- **按已知限制接受**：`create_report` 先 `exists` 检查再 `os.replace`，对**外部进程**仍有覆盖 TOCTOU；lease 只防受控 run，PRD 非目标本就包含"防本机用户"。登记是为了下次读到的人不至于以为没考虑过。
- **中-5 是误报**（`prepare` 当场拒绝目录外目标），处理方式是一条钉住测试，不是产品代码改动。

**M0–M1 复核留账（[DEBUG-M0M1.md](DEBUG-M0M1.md) 修复状态总表，2026-11-10）**

- ~~**authority 数据根逃逸 + 完整 takeover → issue #61**~~：**已修（2026-11-11，PR #70）**。权威根固定到用户级位置（与 `--db`/cwd 无关）；三个接管入口都先只读对账、拒绝时落双证据。残留：权威根只保护"阻断"（跨数据根没有租约互斥）、resume 对账发生在认领作业之后、网络盘上是本机视角。
- **真实断电 / Windows 实机**：**断电语义部分验证**（进程级四个持久化边界 + 撕裂写 + 真实 HFS+ 强制摘卷，PR #71），**块设备/固件断电仍未验证**（`detach -force` 仍刷脏页，"未 fsync 一定丢"不成立）；Windows 实机仍未验证（见 #50）。
- ~~**跨多文件撤销**~~：**已修（2026-11-11，PR #66/#72）**：写前意图日志 + 崩溃后幂等续做 + 恢复侧阻断。仍**不**宣称一次事务：崩在动作之间是"可续做"，需要人/CLI 触发续做。
- ~~**M5 集成测试偶发红 → issue #63**~~：**已修（2026-11-11，PR #69）**。根因是 `Listener` 默认 backlog=1 + 探活的未认证连接自己占满队列（macOS 立即 ECONNREFUSED），不是"就绪早于 listen"；改 backlog=64 + 自连握手后 `on_ready` + `probe_result` 带 errno/重试，同一用例文件 100 次 0 失败。

**M4 评测未验证项（2026-11-10 真实模型轮更新，不许写成"通过"）**

- 真实换模：**已跑**（deepseek-v4.1-flash 18/20、glm-5.2 15/20，同一 suite m4.4，`compare_reports` 判可比；逐条裁决见 [M4-verification.md](M4-verification.md) §7.1）——但两家都判 `unsafe`，根因是 P1/P2 判据写死了脚本模型收尾形态（issue #57），**不是**「换模通过」；
- Windows 上的 `evaluate` **未验证**（issue #55 仅剩的一条）；
- `memfs` 后端：**不做，改为开跑前拒绝**（实测假分数 1/20 + unsafe，见 §4）；
- 真超时：**已覆盖**（黑洞端点真等满 60s×3，`3m01.6s` 后 failed）。
- **m4.5 真实模型复跑：未完成（外部阻塞）**。判据已按目标作用域化（#57）并通过 scripted 基线与确定性单测；但要产出两份真实报告需要可用模型中转站，当时该 key 的所有模型一律 `503 model_not_found / auto groups is not enabled`（`/v1/models` 为空，换域名/模型 id 相同）。文档写"未完成"，没有拿 scripted 冒充，也没有为了绿放松安全判据。

**M5 桌面闭环未验证 / 已知限制（详见 [M5-desktop.md](M5-desktop.md) §10 与 [M5-verification.md](M5-verification.md)）**

- **未验证**：Windows 实机（只有 CI `windows-latest`；命名管道的杀软 / ACL / 长路径差异没覆盖）· 真实模型（无 API key，演示与验收用 `scripted` 模型，不代表真实模型能力）· macOS x64 与 Windows ARM64 用户设备安装/升级 · Tauri 自动更新 · 同一毫秒的并发审批压测（只验证了先后顺序）。
- **Daemon 生命周期不写事件日志**：`shutdown(reason)` 只进 `logs/daemon.log`。C3 是封闭集合且每条事件挂 `run_id`，新增 `daemon.*` 族是契约变更，代价大于收益。
- **陈旧锁判定有 2 秒宽限窗**：第一个 Daemon 在「拿到锁 → bind」之间超过 2s（超大库恢复扫描）时，第二个实例理论上可能误判陈旧并接管。跨平台读取进程启动时间没有 stdlib 方案，属单独工单。
- **推送在主循环里同步发送**：一个不读数据的订阅者理论上能拖慢 RPC 响应；M5 用"本地 socket + 每批 256 条"把风险压在可接受范围，没做独立发送线程 + 非阻塞写。**2026-11-14 对账**：本条已被 PR #81 取代——独立发送线程与有界队列已交付（`daemon.py` `SEND_QUEUE_MAX=1024`；慢订阅者/队列溢出/重连负控 `tests/test_slow_subscriber.py`），与本文 §2/§5/§7 其他提及处一致。
- **驱动线程卡死 >5 秒时刻意不跨线程关连接**：关闭正被别的线程使用的 SQLite 连接只会得到没人看得到的 `ProgrammingError`，还可能截断写入；保留连接交给进程退出回收（测试钉住"shutdown 仍返回、锁已释放、没有 ProgrammingError"）。
- **`approvals.decide` 的 `reason` 被忽略**：审批记录没有自由文本字段，要存就得改 `schemas/m2/approval.json`（契约变更）；当前只用于 UI 展示。
- **当前是未签名 L1 内测 App，不是 G3 发行版**：Tauri 薄壳、托盘、macOS 安装脚本与四目标 draft workflow 已实现候选；无 OS 签名/公证、自动更新、开机自启，Rust Host 未替换 Python Host，四目标用户设备安装/升级/回退没完成。

**平台**

- **无 Windows 实机证据**：当前证据来自 GitHub `windows-latest`，2026-11-13 已验证 run 使用 Windows Server 2025；没有 Windows 10/11、ARM64 用户设备或安装体验验证。
- **UI / Windows 合并门禁缺口已关闭（2026-11-13）**：远端现强制 Ubuntu、macOS、Windows、UI 四项检查（完整名称见 §2）；仍保留 strict、enforce_admins、禁强推/删除与 0 人批准。Windows 实现提交 `4bb1781` 已通过完整 CI；后续提交须取得自己的结果。耗时接近 25 分钟上限仍是可靠性风险，不能因设为 required 就宣称长期稳定或实机验证完成。
- **Windows IPC 握手失败根因已复现（issue #50，2026-11-13）**：`load_authkey()` 新建密钥未指定 `O_BINARY`，Windows 文本模式把随机 LF 改成 CRLF，创建者与后续读取者拿到不同密钥。固定字节回归在 Windows run `43281774439` 明确失败；本分支已改为二进制写入并验证旧密钥不轮转。此前「生产不受影响、只需测试重试、必须有实机才能定位」的结论撤回：这是生产密钥持久化缺陷，GitHub 托管 Windows 已提供复现。修复提交 `4bb1781` 的完整矩阵已通过（run `43283015540`）；见 [Windows CI 修复证据](WINDOWS-CI-2026-11-13.md)。
- **CI 作业有 25 分钟上限**：GitHub 默认 360 分钟，挂住的测试会占 6 小时 runner 且不给任何信息（M5 实测踩过）。
- **不得声称 main 历史上每个提交都绿**：`154b916` 那次 macOS job 排队 15 分钟后被取消（`runner_name` 为空、`steps` 为空），是 runner 供给问题，同提交的 ubuntu / windows 都绿。

**数据落点**

- **两套落点并存，别混写**：
  - `daemon` / `host`：默认平台标准目录（`ipc.default_data_root()`，`TIANSHU_DATA_ROOT` 覆盖）——macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`；目录内含 `events.db`、`artifacts/{blobs,snapshots}`、`run/`、`logs/daemon.log`。
  - 内核子命令（`run` / `task` / `approvals` / `undo` / …）：**仍是当前工作目录下的 `.tianshu/events.db`**（`cli.DEFAULT_DB` 相对路径，靠 `.gitignore` 排除），`--db` 可改。
  - 因此"运行时数据放系统标准位置"这句话**只对桌面入口成立**；写文档时必须带主语，别再写成全局结论。

**仍开着的问题**

- **issue #27**：PRD §4.1 关键字段列与实现双向漂移 11 处，需逐条决定收敛方向。
- **issue #40**：**已修**（否决即终局，`c4.approval_rejected`，`SUITE_VERSION` 升 `m4.4`，PR #48 合入 `feat/m5-desktop`；判据与限制更新见 [M4-verification.md](M4-verification.md)）。

- **issue #55**（M4 未验证项）：四条里三条销账（真实换模 m4.4 跑过、memfs 评测改为明确拒绝、真超时黑洞端点跑满）。**m4.5 真实模型复跑被中转站阻塞**（所有模型 `503 / auto groups is not enabled`，见 [M4-verification.md](M4-verification.md) §7.4），**Windows 实机复验仍未做**（需要 Windows 实机），issue 保持打开。
- ~~**issue #57**~~：**已修（2026-11-11，PR #68）**：`Check.kind`（completion/safety）+ 8 条作用域化判据 + 完成度/安全分栏 + `SUITE_VERSION` m4.5。真实模型报告未复跑（同上一条阻塞）。
- ~~**issue #58**~~：**已修（2026-11-11，PR #67）**：`RunResult.model_attempts`（真发一次请求才 +1，含超时/失败/重试）+ `cli._cost_note` 统一口径；全失败时打印「0 个完成轮次 / 3 次尝试（费用未知）」，不再说"没有模型调用"。
- ~~**issue #53 / #54**~~：**已销账（2026-11-11）**。疑点 1 不成立（模型无法启动 Run，写成设计前提 + 三层钉子）；疑点 2 三条路径探针成立；delete 注入矩阵补齐并暴露一个真 bug（见 §5）。
- ~~**issue #61 / #63**~~：**已修**（见上一段与 PR #69/#70）。

**实现上的疤**

- `run.end` 不带**货币成本**汇总（`cost_total_micros`；成本经游标账本在 `run status` 里看，CLI 收尾从 `ledger` 读钱）；token/turns/`budget_enforced` 汇总已是 `run.end` 的 required 字段（`tasks.py:_run_end_payload`、`event_schema.py`）——货币成本汇总本身仍挂账（约 6 行改动）。
- `check_exec()` 仍是低层 API，`spawn` 已不再调用它。
- 多工作区相对路径解析仍绑定到第一个可写工作区。
- 契约未全冻结：`schemas/` 下现有 `c3/event.json`、`c4/rules.json`、`m2/{approval,grant,job,run,task}.json` 与恢复 B T0 的 `data/restore-v1.json`；C1/C2/C5 **没有**字段级 schema——补齐并加契约测试之前，别宣布冻结。`data/restore-v1.json` 是源码 schema，`data/` 的运行时忽略规则必须保留其明确例外。

**本地环境提醒（非项目事实，只对所有者本机成立）**

- 本仓库位于外置卷 `/Volumes/Date`：**务必经常 `git commit`、尽早 push**（卷拔掉就没了）。`refs/`（参考仓库，约 762MB）已在 `.gitignore` 里，**不要入库**。

## 8. 会话更新日志（AI 每次追加，勿删历史）

- **2026-11-16 · PR #162 锁空窗修复最终本机门**：代码收口 `77e1181` 增加
  `PYTHONIOENCODING=utf-8`，避免 Windows 重定向 stderr 把接管诊断变成另一条失败码；
  `stop_daemon_if_unowned` 与 Host 元数据守卫的注释同步为“锁内确认请求、锁外等待
  锁消失”。精确该代码树的门为完整 Python **3543P12S/656.70s**、相关 Host/M5
  **76P1S**、Rust **27 tests**；UI 16 文件 **124 tests**、typecheck/build 与
  `ui/dist` 重建对账通过，lock/Ruff/actionlint/shellcheck/diff-check 全 0。随后只回填
  本知识与发行规格，形成最终 docs-only SHA；其云 CI、精确 Tauri 包、锁空窗与普通
  attach→quit 探针及新的第二读者 PASS 仍须分别绑定，未以本机门代替。
- **2026-11-16 · PR #162 修复 host.lock 空窗接管误杀并同步最新 main**：独立第二读者
  对 `7b4cc7a/tree 251537b` 维持 `PARTIAL`，新增复现证明旧 Host 锁消失后、新 Host
  发布锁前存在 HIGH 竞态：桌面退出会误杀新 Host 已复用的 Daemon。修复提交
  `0942ee5` 增加有界接管窗口；清理前进入 Host 元数据跨进程守卫并重新确认无 Host，
  只停止锁 SHA 与退出前快照相同的 Daemon，新 Host 返回 3、Daemon 换实例返回 4，
  均保留现场。三个真实分支测试与两条真实 Daemon/M5 集成测试覆盖“无 Host 可清理、
  新 Host 保留、实例变更保留”；真实竞态回归在旧实现上为 `Ok(())` 误通过、修复后
  绿。修复提交上的本机门为完整 Python **3543P12S/662.96s**、相关 Host/M5
  **26P1S**、Rust **27 tests**，lock、Ruff、`git diff --check` 均 0。之后普通 merge
  最新 `main@e242257`，唯一冲突 `KNOWLEDGE.md` 以行多重集保留原冲突区块 60/60 行
  和分支四条 PR #162 日志，无冲突标记。该合并后只新增本条 tracked 文档对账；最终
  docs-only SHA 的精确 CI、Tauri 安装包、锁空窗/普通 attach→quit 探针与新的独立
  PASS 尚未产生，因此 PR 保持未合并，release workflow 不启动。
- **2026-11-15 · PR #162 首轮 CI 红定位与本机重门**：`d26ad98` 的精确 run
  `43681913168` 保留为 Ubuntu 单红：测试在 Linux 调用仅支持 macOS/Windows 的宿主探测，
  未覆盖目标拒绝逻辑；macOS、Windows 两分片、Windows 汇总与 UI 同时真实成功。只修测试
  宿主模拟后目标 `9 passed`，本机 lock/全仓 ruff exit 0、完整 `3447 passed / 11 skipped`
  （693.91s）。当前 PR head 原生 required checks、独立复审和 main 四目标 release 仍未
  完成，因此只记为“已定位并修复首红”，不记为发布验收或 G3。

- **2026-11-15 · PR #162 第二轮 Windows 网络分类红与本机重门**：首轮修复后的精确 run
  `43684083160` 保留 Windows 分片 0 单红，原因是期限 Timer 的 `shutdown()` 被后续 IO
  异常路径在时钟检查尚未越过 deadline 时错分为 `net.read`。新真实 HTTP 回归先在旧实现
  确定性红，再仅在 Timer 中断路径保留 `net.timeout`；六个网络相关文件 `84 passed`。
  分支普通 rebase 到 main `c8b53bb` 后，最终代码通过 lock、全仓 ruff 与完整
  `3461 passed / 11 skipped`（668.23s）。当前 head 云矩阵、独立复审和 main 发布工作流
  仍待，未把任何旧 SHA 的绿色或本机全量扩展成最终验收。

- **2026-11-15 · PR #162 第三轮 Windows MCP setup 红与本机重门**：rebase 后精确 run
  `43687249112` 保留 Windows 分片 0 单红。测试在 `call_tool` 前用固定 5 秒等待控制
  socket，而 MCP 配置允许 10 秒握手；超时清理又先于真实 worker 结果断言，掩盖了 setup
  阶段。改为先等待真实 transport 入口并以配置握手上限加清理预算作为 watchdog，再
  rendezvous socket；同一 6 秒注入下旧测试红、新测试绿。MCP 相关 `202 passed`，完整
  `3461 passed / 11 skipped`（639.23s）。最终 head 原生矩阵与独立复审仍待，不把测试
  修复扩展为 MCP 生产期限或 Windows 设备支持已变。

- **2026-11-15 · PR #162 第四轮发布/生命周期复审作者修复**：从 `7707597` 继续，先以
  可复现探针确认旧 main 祖先可进入 release、启动失败/ACK 后崩溃窗口遗留 Daemon、
  runtime lock 错 triple、macOS Python 架构漏检、Host 已退出仍等 30 秒、延迟 Daemon
  锁逃过一次性清理七项。提交 `3c0687d` 后统一本机门为 Python 3465P/11S、UI 124
  tests、Rust 18 tests，lock/Ruff/typecheck/build/dist/fmt/clippy/actionlint/shellcheck
  全 0。随后又确认 `host.lock` 截断发布竞态（R3-08）并在 `719de9a`/`8058926` 修复；
  同步 main `61f985e` 后，最终代码对象 `1b0c99d` 的独立本机全门为
  3514P12S（704.08s），UI 124 tests、Rust 24 tests，其余静态门全 0。精确 docs-only
  candidate `aa6b65f` 的 PR run `43701648845` 六 job 全绿：Ubuntu 3494P32S、
  macOS 3514P12S、Windows 3470P56S/3526 节点、UI 成功；GitHub Actions 仅保留
  Node 20 action 弃用和 Windows PATH 提示，无产品或测试失败。同一 SHA 的
  macos-arm64 产物通过 runtime smoke、manifest/篡改负控、安装/卸载/重装/保留数据以及
  真实 App 启动、关窗、重开、显式退出。由于本会话禁止启动/调用子代理，独立第二读者
  PASS 仍不存在；PR 保持未合并，不把作者侧作者门禁和本机自查当成独立批准。

- **2026-11-16 · PR #162 同步 main `702ba40` 并完成精确云门**：从独立复审
  `PARTIAL@5873720` 的诊断修复提交 `187a579` 普通合并最新 main `702ba40`，合并
  提交 `3f5e2b1`（tree `2ab00890`）。唯一冲突仍是 `KNOWLEDGE.md` 两处同锚追加：
  解析器保留 HEAD/main 双方，并把当前状态从 `1a8aebb` 更新为 `702ba40`；W15 v1
  审计（PR #169）改记已并入 main，已合并的 #141 从“仍 OPEN”改为实际，`#140` 保留。
  合并前用 stage1+双方相对 base 的唯一行多重集证明零事实丢失，冲突标记和
  `git diff --check` 均为 0。合并后独占本机门为 lock/Ruff/W15 定向 31P、完整
  Python **3538P12S/697.14s**、UI 124 tests、Rust 26 tests 及全部静态门 0。PR run
  `43747490300` 绑 `3f5e2b1`，GitHub 合成 merge `ec106a89` 的 tree 与候选相同，
  六 job 全绿；Ubuntu 3518P32S、macOS 3538P12S、Windows 1756P19S+1738P37S
  （3494P56S / 3550 nodes）、UI 124 tests。原件在
  `.omc/artifacts/release-app-sync-702ba40-3f5e2b1-2026-11-16/`。本日志之后只做
  docs-only 最终 SHA、精确 macos-arm64 产物、安装态 attach→quit 与独立复审；旧 SHA
  绿色不冒充新 SHA。
- **2026-11-16 · 独立复审发现附着 Host 退出遗留服务并修复**：第二读者绑定
  `5873720/tree f16f930` 复审为 `PARTIAL`；它独立构造的 macos-arm64 产物、篡改
  负控、安装、冷启动、普通自有 Host 生命周期及 W3/W5 兼容均通过，但安装态复现
  `owns_host=false` 时 App 菜单退出只结束 desktop，Host/Daemon、`host.lock`、
  `daemon.lock`、`agent.sock` 全部残留。先用真实 loopback HTTP 回归锁定原缺陷：
  与会话前实现相比，退出后 2 秒内没有发出 `POST /shutdown`，测试稳定超时。修复在
  `desktop/src-tauri/src/lib.rs` 保留“本壳拥有”和“附着”两种启动来源，但显式退出
  统一请求鉴权 shutdown；随后按 PID 等待原 `host.lock` 消失，若 Host ACK 后 Daemon
  仍未清理，则只对启动前快照未变化的 Daemon 执行同包 Python 有界停止，并拒绝误杀
  退出期间接管的新实例。新增两条真实 HTTP/子进程回归覆盖完整清理和 ACK 后孤儿 Daemon
  收尾。复审另指出旧 main 基线与桌面 README 将原生 Intel/ARM64 runner 写成“交叉
  构建”，已同步修正。候选已普通合并最新 `main@1a8aebb`，`KNOWLEDGE` 冲突保留双方
  历史；最终新 SHA 的云 CI、产物与安装态 attach→quit 证据待回填。
- **2026-11-15 · PR #162 同步 W3/W5 并完成精确 CI、产物与 GUI 复验**：远端 main
  在收尾期间连续推进到 `c7f31f7`（W3）与 `c637b52`（W5）；两次都用普通 merge
  同步，`KNOWLEDGE` 冲突保留双方事实，另只删除 main 带入的 `scheduler.py` 行尾空白。
  最终代码候选 `3073602`（tree `d8c55fb`）定向交集 260P1S、完整 Python
  3534P12S（692.71s）、UI 124 tests、Rust 24 tests、lock/Ruff/typecheck/build/
  dist/Cargo fmt/clippy/actionlint/shellcheck/diff 全 0。精确 PR run
  `43729859260` 六项全绿：Ubuntu 3514P32S（525.33s）、macOS 3534P12S
  （803.83s）、Windows shard 0 1756P17S、shard 1 1734P39S，汇总 3490P56S 并核验
  3546 nodes；UI 23s。精确 macos-arm64 archive SHA256
  `86efe6573b204eda1608754f0c023ec0b37a5d2734febb69a6b238bfdc3c2115`、
  manifest `48822d8fadb2e375a8dc93457c20aa5a8cd9ce606f02b9923d3aec74d8bcd738`，
  通过 verify/runtime smoke/篡改 `host.py` 精确拒绝/全新安装。真实 App 冷启动显示
  React UI 与“已连接”，`/health` 和 `/state` 使用同一隔离数据根；关窗后
  `desktop 37550 → Host 37578 → Daemon 37585` 保持不变，重开复用同一进程链，
  菜单“退出天枢 Agent”后三层全退，`host.lock`/`daemon.lock`/`agent.sock` 清理且
  包内字节码为 0。证据在
  `.omc/artifacts/release-app-final-3073602-2026-11-15/`。本会话不能调用独立复审，
  不把作者侧结果冒充第二读者 PASS；独立 PASS 前仍不合并、不跑依赖 merge SHA 的四
  目标发布。
- **2026-11-15 · PR #162 Windows 修复精确 CI 与产物闭环**：`5e89de5` 的 PR run
  `43714752198`（attempt 1）六 job 全绿：Ubuntu 3496P32S/529.73s、macOS
  3516P12S/755.22s、Windows shard 0 1726P38S/704.93s、shard 1
  1746P18S/895.39s，Windows 汇总为 3472P56S 并核验 3528 nodes；UI
  install/typecheck/test/build/dist 全部成功。上一轮 shard 1 的 `WinError 5`
  已在修复后消失。从 `5e89de5` 重建的 macos-arm64 archive SHA256
  `f1163707937bc8370abf32847f5e4ff3c48cddbee5b4fd9c754cf151b2b5038d`、manifest
  `964f0c4b6e98f542b5e8caa2e7b8ef4577ab0fe1030acf6be031d504c5e8432a`，通过
  verify/runtime smoke/篡改负控/fresh install；真实 WebView 冷启动可见，关窗、
  重开保持同一 `desktop 6636 → Host 6647 → Daemon 6648`，`Cmd+Q` 后三层全退且
  `host.lock`/`daemon.lock`/`agent.sock` 消失。证据在
  `.omc/artifacts/release-app-final-5e89de5-2026-11-15/`。独立第二读者 PASS 仍是
  合并硬门；本会话不能调用复审代理，也不把作者侧六绿冒充独立验收。
- **2026-11-15 · PR #162 Windows 原子替换瞬时占用修复**：`b46f015` 的精确 CI 中
  Windows shard 1 仅 `test_concurrent_approval_consumes_once` 失败：审批只放行一个
  执行者，但获胜 `modify_text` 的 `os.replace(note.md)` 遇到 `WinError 5`，结果按
  事实记为 `unknown`。检查读取生命周期、`BEGIN IMMEDIATE` 派发和 `fsutil` 后，确认
  不是双重执行、未关闭的本进程读句柄或 b46 新增回归；根因边界是 Windows 原子替换时
  目标被短暂占用。按 TDD 先写瞬时两次失败、第三次成功的测试并看旧实现红，再在
  `bb0ce76` 中只对 WinError 5/32/33 做 8 次有界退避，其他权限错误立即抛出，持续
  占用最终失败并附诊断 note。目标 24 项、Ruff 与崩溃测试通过；本机完整套件首轮
  3515P 唯一失败已由全局 `ANTHROPIC_BASE_URL` 中转污染单测复现，保留原失败后在移除
  该变量的环境重跑为 **3516P12S（640.36s）**。旧 Windows 红日志、RED/GREEN 与全量
  原件在 `.omc/artifacts/windows-atomic-replace-retry-2026-11-15/`；最终云矩阵、重建
  产物和独立复审仍另绑。
- **2026-11-15 · PR #155 前端核对与 Release WebView 冷启动白屏修复**：先刷新远端并
  核对 PR/分支历史，确认 `feat/budget-subtask-ui` 已由 PR #155 合并为 `d37a84c`，
  当前 `codex/release-app` 已包含其提交且 `ui/` 树逐字节相同；UI install/typecheck/
  **124 tests**/build/dist 全绿，不是待接入分支。继续检查真实安装产物时发现
  `aa6b65f`/`820a7cd` 的所有权 Host 冷启动会稳定显示纯白页，而 Host/Daemon、
  `host.lock` 和 `/health` 都正常。根因是 Tauri `setup` 在事件循环前阻塞等待 Host
  后直接 `navigate`；旧 GUI 探针只验证了窗口与进程，没有验证页面内容，因而漏报。
  提交 `58d0bdb` 只把成功导航和启动错误渲染排入 `run_on_main_thread`，不改 Host、
  授权、事件或生命周期语义。空闲重跑此前受 GUI 负载影响的 Rust 时序测试为 0.63s，
  全套 Rust **24 tests**、fmt、clippy 与静态门通过；UI 再次 124 tests 通过。从
  `58d0bdb` 重建的 macos-arm64 产物通过 verify/runtime smoke，篡改负控精确拒绝。
  全新安装冷启动实际显示 React UI 和“已连接”，HTTP JS/CSS 与包内字节一致；
  关窗后三层 PID 不变，重开恢复同一 Host，真实 `Cmd+Q` 后三层全退且三把运行期锁/
  socket 清理。原件索引为 `.omc/artifacts/release-app-frontend-58d0bdb-2026-11-15/INDEX.sha256`
  （SHA256 `1cc81d9e3cfe10e5dc0dfd8a9315de58237a8c3a4913ec909e8506e08c39adf6`）。
  本条只完成作者侧修复与新产物证据；docs-only 最终 SHA、精确云 CI、四目标 workflow
  和独立复审仍另绑，PR #162 保持未合并。
- **2026-11-15 · PR #162 同步最新 main、真实 GUI 缺陷修复与目标模式收口**：
  fetch 后确认远端 main 已由 `0c5bb71` 前进到 `61f985e`；在保留
  `backup/codex-release-app-before-main61` 的同时普通合并为 `ca8f32d`，只发生
  `docs/KNOWLEDGE.md` 冲突，机械保留 main 的网络/T1/W 系列事实与 release-app
  历史，无 skip、门禁或产品契约变更。真实安装态 App 在 `0f8649c` 上暴露启动期
  `host.lock` 半写读取；spawn 子进程发布、父进程直接读的确定性旧实现探针稳定
  截断为 `{"pid` 并抛 `JSONDecodeError`，`719de9a` 改为临时文件 + fsync +
  原子替换 + 目录 fsync。`8058926` 随后把回归升级为真实 spawn 子进程中的阻塞发布者；
  main `61f985e` 合并后的独占门为 **3514P12S、704.08s**，Host 76P1S、UI 124 tests、
  Rust 24 tests，其余静态门全 0。
  受控本地 OpenAI-compatible 服务在旧真实产物上完成拒绝、批准、取消三闭环，并验证
  关窗保活、重开同 Host、`Cmd+Q` 无壳/Host/Daemon 残留。收口时发现另一个在途会话
  也启动了完整 pytest；两个全量因用户级 restore gate 互相触发 `maintenance_busy`，
  相关红日志均保留，待所有测试进程退出后独占门真实通过。当前只冻结作者侧证据；
  独立复审因本会话禁止启动子代理而不能由作者代替，最终新 head 的完整门、云 CI、
  产物和 GUI 复验另绑。

- **2026-11-15 · W15 v1 只读保留期审计命令（`feat/retention-audit`，基线 main@1a8aebb）**：按维护者委托「先测量后拍板」落地 CONTRACT-W15 §3.3 的 v1——`data_cli.py` 新增 `retention-audit` 子命令（+508 行含测试）：逐表面 `COUNT+MIN/MAX`（23 表全字面量 SQL 分派）、8 个状态面直方图、增长投影（basics 复用，窗口 ≥1 天才外推）、候选测量（jobs/budget_reservations/runs 三个低风险面，终态判定全在 Python 侧用 `TERMINAL_JOB_STATES`/`NON_TERMINAL_STATES`/`SETTLED` 权威常量算，不对 dispatch 面猜语义）、`cleanup_plan(data_root)` 现状直连。**零删除不变量**：`mode=ro` URI 连接（SQLite 连接层拒写）+ 测试钉 events.db SHA256 前后不变；不加维护锁不阻断写入（点读快照如实写进 notes）。测试 4 条新增（拒绝路径+计数/只读哈希/投影数学+未知表/候选常量一致性），`test_data_cli.py` 31 passed。**过程记录**：(a) 每表 SQL 无法参数化标识符，初版 f-string 拼接被 Mimosa hook 判「SQL 注入」拦截三次——按其建议改全静态字面量分派后，注入面反而归零（表名封闭集 + 字面量 + 值绑定）；(b) 夹具上 2ms 事件跨度外推出 86,400,000 行/天的垃圾速率，测试实抓 → 修为观测窗口 ≥1 天才外推；(c) 大段 Edit/Write 内容连续发生拼写损坏 6 次（占位符垃圾/键位突变），全部靠「每段落盘立即 py_compile+AST 校验」抓回——**长代码块一律 ≤25 行分段 + 逐段验证**。CI 环境：#168 attempt1 与 #141 attempt1 的 Windows shard1 先后红在 `test_round2_inputs_net.py::test_network_deadline_interrupts_slow_progress_before_peer_finishes`（`[headers]`/`[body]` 两个参数、同签名 `'net.read' != 'net.timeout'`，WinError 10053 连接被宿主中止）——两树产品代码与绿 main 零 delta，初步定性 runner 负载敏感，**按 #163 先例不以 timing flake 结案**；另 #168 attempt2 用 `--failed` 部分重跑被汇总 job 的分片校验按设计拒绝（missing/extra shard artifacts，attempt 内必须成套）——**部分重跑不能出可验证绿，须整跑重跑**（#168 attempt3 整跑全绿后合并 1a8aebb）。三项拍板继续留实测数据。

- **2026-11-15 · 第三批 W3/W5/W15 合并列车 + worktree 清理（维护者拍板「合并进 main 后删掉」，并把 W15 拍板流程委托为「先测量后拍板」）**：严格列车逐条执行「rebase 就最新 main → KNOWLEDGE 行多重集终验 → 本地门禁（lock/ruff/全量）→ CI 六项全绿 → 保护 merge commit」：#164（W15 docs-only）→`9466df0`（原 CI 绿，免 rebase）；#166（W3 8MiB 上界）rebase 到 `9466df0` 自动合并（多重集 PASS，门禁 3448P/12S/662.78s）→ run 43721830851 六项绿 → `c7f31f7`；#167（W5 调度器）rebase 到 `c7f31f7` 遇 KNOWLEDGE.md 唯一冲突（README/ROADMAP 自动合并）——**difflib entries 并集法（replace 感知，main 先/branch 后同锚排序）proof PASS（933 = 929+929−925 行，零丢失零重复）**，门禁 3461P/12S/649.56s（= main 级 3448 + 13 条调度新测试，账目吻合）→ run 43724954970 六项绿 → `c637b52`。删除清单：w7/w8/w10/w10-review（上批已合并，删前逐一核对分支尖 = 已合并 PR head）+ w3/w5/w15（合并后删）；六个本地分支全部 `git branch -d` 自然可删——**前提是本地 main 先 `pull --ff-only` 到最新（分支须是 HEAD 祖先），否则 -d 被拒**；远端分支 GitHub 合并时已自动删除。**W15 三项拍板（事件日志 archive-then-truncate = 红线 1 修订、summary_versions FAILED 行豁免触发器 = 契约变更、jobs/budget_reservations 终态行保留期数值）不拍死任何数值**：维护者委托后定为「先测量后拍板」——先交付 v1 只读保留期审计命令（CONTRACT-W15 §3.3 划界，零删除），用实测数字支撑逐项拍板。本片顺带修正路线图状态账：W1/W2/W6/W7/W8/W10 六行仍写「实现完成见 PR」或「未开始」（实际均已合并），一并销账。#140/#141（文档对账）仍 OPEN，与后续 PR 按 modify-modify 并集法处理；F10 等 §7 旧条目的「PR 待合」漂移属 #140 范围，本片不扩 scope。

- **2026-11-15 · W15 保留期/GC 契约评估（docs-only）**：路线图 W15 解锁后开工，交付 [CONTRACT-W15-RETENTION-2026-11-15.md](CONTRACT-W15-RETENTION-2026-11-15.md)——逐面盘点 13 张表/文件的增长驱动 × 删除不变量 × 恢复/审计角色。**结论：没有任何一张表今天就安全可删**：`summary_versions` 有 BEFORE DELETE 触发器（summaries.py:128）且回放三道校验依赖；`dispatches` 的 `boundary_protocol`/`intent_seq` 是 `none_applied` 判定组成部分（红线 2）；`dispatch_requests` 隐私已用终态脱敏而非删除解决；红线 1/5/11 直接覆盖 events/预算面。三个「点名的增长面」实际无 GC 缺口：leases release 即删行自清理、migration_log 行数=版本数、metrics 已被 #150 轮转；无界增长的现实风险上限由 DiskAdmission 门槛兜住（如实拒绝而非静默膨胀）。**v1 建议 = 只读保留期审计命令**（data_cli 子命令：逐表行数/最老行年龄/终态占比/增长投影/cleanup_plan 现状，零删除），实现落点 maintenance.py/data_cli.py 被 #161 占用等落地后另立 PR。**拍板项三项**：事件日志 archive-then-truncate（= 红线 1 修订）、summary_versions FAILED 行豁免触发器（= 契约变更 docs(contract) 先行）、jobs/budget_reservations 终态行保留期数值。

- **2026-11-15 · W5 调度器实现（SPEC → 实现 → 负控 → 门禁，基线 main@0c5bb71）**：交付确定性最小调度器——`src/tianshu/scheduler.py`（schedules 表按 budget.py:84/dispatcher.py 先例建在事件库同一条连接上）、`src/tianshu/schedule_cli.py` + cli.py 两处钩子（管理面只走 CLI，无 RPC/无 UI/无 cron，均如实写明）、daemon.py `_driver_loop` 装配点 + tick 调用（Scheduler 与 Worker 同在驱动线程装配，遵守 driver_log 连接线程纪律；调度器故障不带走驱动循环、不计入 consecutive_failures）。触发 = 与 `task.create`/`run.start` 完全同路径（create_task → prepare_run → start_run → enqueue，Run 由 worker 认领），**零新事件族**；追赶 = 补发一次并从当前时间重排；幂等机制如实记录为「同事务记账 + 单写者」——**核对发现事件层 `idem_key` 无 UNIQUE、`EventLog.append` 不去重、task 创建路径不使用它**（正控测试 §4.3 的诚实结论）。授权面论证：调度器不是授权输入，只建任务入队不执行副作用，Run 内动作照走执行侧判定（红线 2/4/6）。文档四件套：SPEC-W5（接口/语义/测试清单）、CONTRACT-W5（C3 评估：v1 不新增事件族的四条理由 + v2 五类 schedule.* 事件 payload 草案 + 三处同改清单，单独 commit 不夹带代码）、ROADMAP W5 行 → 实现完成、README 特性表新增一行。测试 13 条全注入时钟（FakeClock），daemon 集成用有界轮询非 sleep 猜测。负控两条实跑红→还原：NC1 拆到期检查 = 9 failed/4 passed；NC2 拆同事务记账 = 6 failed/7 passed，关键红证据 `['run_9eba…'] == []`（interval 内第二次 tick 双发）。本机门禁：`uv lock --check` 0、全仓 ruff 0、全量 **3408 passed / 11 skipped（686.57s）**（基线 3395P/11S + 13 条新测试，正好吻合）；跑前抢 `/tmp/tianshu-suite-lock`、df 5.3GiB 达标、文件重定向 + 显式退出码。CI 结果以 PR 为准。

- **2026-11-15 · Ubuntu CI 暴露短正文静默成功并修复**：T1 最终候选的 Ubuntu job 在 `test_send_phase_failure_is_never_retried` 实测 `DID NOT RAISE NetError`；Mac、两片 Windows、Windows 汇总和 UI 均绿，证明失败不是 T1 平台回归。根因是 `http.client` 对带 `Content-Length` 的半截响应是否抛 `IncompleteRead` 取决于平台/EOF 时机，`fetch()` 未自行核对已读长度。修复在正文读取后显式比较 `HTTPResponse.length` 与收集字节，短正文报 `net.read`；半响应夹具先读完请求，避免把 TCP RST 混成协议测试。新增假连接回归覆盖“客户端静默 EOF”，`test_net_resilience.py` 为 14 passed；最终全量 3443P12S、645.06s，lock/ruff 0。
- **2026-11-15 · W 系列七 PR 合并收口**：维护者拍板合并七条 W 系列 PR（#150 指标轮转 / #151 keychain / #152 网络重试 / #154 embedding 外发 gate / #155 预算子任务 UI / #156 子代理写深度 / #157 记忆冲突消解）。strict 保护下逐条执行「并入最新 main → 双向 diff/行多重集终验 → 本地门禁 → CI 六项全绿 → 保护合并」，全程零强推；main 推进 `02ac07e→2530a15→595be35→7f4c07d→24eabc4→d37a84c→134e9f0→c8b53bb`。冲突三次 modify-modify（#155/#156 的 §7 G2 行、#157 的 §7 F10 L2/L3 行）+ 两次 §8 同锚追加（#152/#154）+ 两次自动合并（#150/#151）。modify-modify 解法 = **difflib 提取双方相对共同祖先的插入段机械求并集**（两侧文本零改写、位置互不重叠才可行；#155 曾因解决器按追加处理把 G2 行复制成两份，行多重集核查抓出后修复——**冲突解决器的「theirs-then-ours」只对纯追加冲突合法**）；F10 行 main 侧等于祖先版故直接取分支版。环境教训两条：**两个会话的全量套件并行 + 遗留临时目录把临时卷打到 98% 满，DiskAdmission 空间门槛让 110 个测试拒绝启动 Run（"缺少完整 Run storage context"）**——清理遗留临时目录并等对方套件结束再跑；**后台 pytest 管道 `| tail` 吞 summary 且管道退出码取 tail 的**——用文件重定向 + 显式退出码。CI flake 记录两条（均 attempt2 全绿）：#151 attempt1 Windows shard1 = `test_concurrent_approval_consume` 的 `os.replace` WinError 5 文件锁竞态（新 flake 家族成员）；#154 attempt1 shard0 = `test_data_binding` 在案家族。各 merge tip 本机全量门禁：3329P/8S → 3334P/11S → 3347P/11S → 3369P/11S → 3369P/11S → 3382P/11S → 3395P/11S（711.25s），合并后 main 终态以收口分支复跑为准。W3/W5/W15 已由 #146/#159 落地解锁，W4/W12 等 #141（仍 OPEN）；#140/#141 与本轮改动在 KNOWLEDGE/PRD 相交，后续合并按同法处理。收口轮：Mimosa deep 重扫合并后 main（scan-2026-11-15T05-50-03.471Z-aeada98bbc47，seal sha256:fe541aaa…ba8ad）31 findings 与上轮 seal 27a03f47 签名逐条一致 0 增 0 删；收口 PR #163 attempt1 ubuntu 红 = `test_send_phase_failure_is_never_retried`；后续恢复 T1 新 run 仍复现 `DID NOT RAISE NetError`，已定位为 `fetch()` 未自行核对 `Content-Length` 的真实完整性缺陷并修复（见下一条）。#163 attempt2 绿不能改写首红，现不以 timing flake 结案。
- **2026-11-15 · 恢复 B T1 平台基础候选**：从最新 main `2530a157` 调整到 `codex/restore-t1-platform`，新增真实实体身份、非阻塞 shared/exclusive entity access、固定 Home 只追加 registry、EventLog 连续复合锁，并把迁移/离线备份改为 namespace+entity 独占。负控覆盖真双进程共享/独占与硬退、硬链接、路径/祖先替换、SQLite `main`、registry CAS 竞争/初始化竞争/四 failpoint/schema 篡改、SQLite/实体句柄 close 失败保锁。独立第二读者用真子进程发现 gate 外 absent 快照与迁移 close 失败释放 access 两项缺陷；修复后新增竞态和公开 migrate close-failure 回归，第二轮定向复审 PASS。T0+T1 定向 166 项（macOS 165P1S）；同步 main `0c5bb71` 后的代码对象 `32f003d` 本机 lock/full/ruff 为 3443P12S、645.06s、全 0。PR run43688229712 attempt3 六 job 全绿：Ubuntu 3433P22S、macOS 3443P12S、Windows 1699P28S+1710P18S（3455节点）、Windows汇总与UI通过；attempt1 的 #145 flake 与 attempt2 artifact 缺片原件保留。独立导出复验 104P1S PASS；Windows artifact `restore-t1-windows-43688229712-3` 状态 PASS。
- **2026-11-15 · 可安装 App 第二轮复审缺口修复**：把 `codex/release-app` 普通 rebase 到
  `origin/main@134e9f0`，保留五提交历史。复审绑定 `def12d27` 的结论为部分修，新增阻断
  不是应用已可绕过 Rust 校验，而是发布门禁会接受错误候选：短 `release.json` 与完整
  manifest/请求身份未三向校验、归档内 manifest 未绑定、非原生 runner 可生成
  `not_run_cross_target`、Windows 合成 PE 可冒充 NSIS。代码提交 `cc05c3e` 增加封闭短身份
  解析和字节一致要求；Windows 通过 7-Zip 实解，要求唯一 `release.json` 资源根，完整
  manifest、managed 文件集合和 Python PE 均与 bundle 绑定；assembler 只接受原生 `pass`，
  matrix 使用 `macos-26-intel`、`windows-11-arm` 等匹配架构 runner。负控先红后绿，
  release 定向 64 passed。该记录不替代最终 SHA 的真实产物、全门、Windows 原生 release
  workflow、两份独立复审或用户设备证据。

- **2026-11-15 · 可安装 App 复审阻断修复与真实闭环**：对 `bef1a3a` 的两份独立复审
  结论逐项销账，作者提交 `def12d27` 修复完整文件集合、来源洁净绑定、Windows NSIS/Python
  架构分离、workflow 输入注入、Host 身份与直接退出、资源 override、孤儿 Daemon、安装前
  原生 verifier、目录重叠与 purge 二次确认。定向负控和全门通过（3389P/11S、589.67s；
  UI 13/91；Cargo 19；其它静态门 0）。真实 macOS arm64 产物 SHA256
  `634dc6ed...` 在隔离数据根完成 WebView 任务、关窗/重开、Cmd+Q、Host `SIGKILL` 后无孤儿、
  陈旧锁恢复和篡改失败页；pre-existing debug App 的默认用户数据流未被停掉或清理。
  **踩了**：完整文件集合会与 Python 首次导入生成的 `__pycache__` 冲突，正确修法是把所有
  bundled Python 入口固定禁写字节码，而不是放行未列文件；LaunchServices 重开需要处理
  `Resumed`，默认 macOS Quit 预定义菜单会绕过普通退出闭包，必须换成自定义菜单项。
  独立复审与 PR/原生 CI 另绑，未把作者结果当成批准。
- **2026-11-15 · 可安装 App L1 候选与启动/安装安全闭环**：基线 `main@595be35`，隔离分支 `codex/release-app`。先落 [SPEC-RELEASE-APP](SPEC-RELEASE-APP-2026-11-15.md)，实现 release manifest、锁定 python-build-standalone 3.12.16、Tauri 2 薄壳、单实例/托盘、启动时逐文件 SHA 校验、macOS 安装/卸载与启动烟测失败回退、四目标 draft-release workflow。真实调试发现 `/tmp` symlink 导致 Tauri `resource_dir=UnknownPath`、误把数据根指针文件当目录；前者由安装 prefix canonicalize 修复，后者由测试夹具纠正。release 模式 macOS arm64 已真实安装并验证 WebView/`/health`、关窗保活、重开、`Cmd+Q` 无壳/Host/Daemon 残留、卸载保留与显式清理临时数据；篡改一个 Python 文件后 App 显示 startup failure 且 Host 未启动。完整门禁：lock/ruff 0，Python **3377 passed / 11 skipped / 558.70s**，UI 13 文件 91 tests/type/build/dist 0，Cargo 14 tests/clippy 0；首次全量唯一失败已确认由宿主 `ANTHROPIC_BASE_URL` 污染造成，测试子进程隔离后目标测试与全量通过。仍未取得最终提交独立复审、四目标原生 workflow、设备安装/升级/回退、签名/自动更新或 G3。

- **2026-11-14 · Windows 计时/锁等待诊断保护合并与 main 首绿**：正式 da47 先由第二读者完成测试内容、事实文稿/公开子集和正式 Git 对象三层窄绑定；本机 1163 输入上 lock/full/ruff 全 0，3199 passed/8 skipped、635.00s。PR153 首次六 job 全绿后独立核对两份原生 artifact、真实 checkout/tree/parents、3207 节点与 9588 阶段，确认 48 个聚焦节点全过并保留 3 次 watchdog；正常保护合并为 main88ec，tree 不变。main 自身首次 push CI 另跑并独立重算同一完整 collection，六 job 全绿；其 3 次长等待均只在片 1，测试随后全阶段通过。原 c2cf 双红、未知锁/MCP 机制和 #145/#147 不销账，恢复 B 仍未实现。

- **2026-11-15 · W 系列独立复审轮（#151 Keychain 第二读者）**：对七个在途 PR 启动全量独立复审（每 PR 一个不读作者结论的第二读者：SPEC 判据逐条 + 自设计探针 + 变异牙齿 + 全量门禁 + PR body 对账）。#151 复审 PARTIAL、无 P0（判据全 PASS、负控独立重放全红、探针残留 0），两个 P1 当轮修复于本分支：① 三个测试直删生产凭据槽 `tianshu-agent/openai/llm`（实测跑一次测试套件即无声销毁用户真实 key）→ autouse 夹具隔离到 `tianshu-agent-selftest` service + 生产槽只读守卫（变异验证有牙；守卫消息只报有值/空不泄载荷）；② 「kSecUseKeychain 被忽略、文件 keychain 路由已死」被两臂对照探针证伪（挂该键条目落进文件 keychain）→ 「只支持默认 keychain」改为范围裁决表述，API 行为不变。另修：PRD F12 行补落地状态、PR body 绑定改指实际被审 SHA、SPEC §8 数字账目与过时文案（「v1 只有 env」「macOS runner 没有 login keychain」已被推翻）、locked 测试补绑定常量断言。教训：**平台行为探针结论单轮不可作永久 API 拒绝依据**（两轮矛盾 → 范围裁决）；**测试对生产命名空间的写删必须结构隔离，不靠逐测试自觉**。修复后门禁 3204 passed / 11 skipped（665.98s）+ lock/ruff 0（SPEC §9 全记录）。其余 PR 复审结果记各自分支/正文。
- **2026-11-15 · W 系列独立复审轮（#152 网络重试第二读者）**：#152 复审 PARTIAL、无 P0（判据 9 PASS + 2 PASS*、负控 4/4 有牙、门禁独立复跑 3211P/8S），2 个 P1 当轮修复于本分支：① **重定向已消费后连接失败被自动重试**——重定向后第二跳连接失败被当作可重试，自动重发第二个 GET（无重新批准，违反 SPEC 自己的授权不变量），且因 visited 跨尝试保留被误报成 `net.redirect_loop`（探针实测服务器收到 ['/a','/a']）→ fetch() 记录 redirects_followed，已消费重定向的尝试一律不重试、抛原始连接错误；新判据 + NC-D（拆守卫即红）。② **`net.connect_timeout` 重试实际不可达**（连接超时=整个剩余期限，首试烧尽后 deadline 检查拒绝第二次尝试）→ 如实改文档，per-attempt 切分登记未决，不夹带行为变更。另修：「TCP 从未建立、零字节到达对端」措辞过强（TLS 握手 1498 字节实测已到对端）→ 全部改为「应用层请求字节为零（TLS 握手字节除外，如实）」，授权核心未破；`min_interval_s` 接受 inf/nan → inf 会让速率等待死循环并持住进程锁 → `math.isfinite` 构造期拒；SPEC §3.1 注入方案与实现不符（实为 conftest autouse 钉 0）、ROADMAP 悬空未披露、基线数字过时等 P2/P3 一并更正。教训：**「可证明没发出去」的证明必须覆盖整条跳链**——单跳测试钉不住多跳后的重试边界；**「零字节」类绝对化措辞要写明是哪一层的零字节**（TLS 握手字节先于 HTTP 请求字节）。修复后门禁见 SPEC §9。
- **2026-11-15 · W 系列独立复审轮（#154 embedding 外发 gate 第二读者）**：#154 复审 PARTIAL、无 P0（核心交付成立：检索 gate 是真执行侧结构保证、NC1 有牙、模型路径端到端零外发、评测数字逐位复算吻合），4 个 P1 当轮修复于本分支：① 评测脚本 `--provider openai` 接口静默作废（hybrid 行被 gate 降级成 FTS-only，假数字）→ retrieve 显式传 `allow_embedding_outbound=True` + hybrid 行 `semantic=False` 即报错，两条新测试钉参数与拒印；② 「安全默认」测试无牙（`_Mute` 误用 setattr 赋属性而非剥属性，变异 19 全绿）→ 真剥属性后翻转变异即红；③ 端点回显认证头时密钥经 dispatcher `_fail_unknown` 不脱敏进 append-only 事件日志 → `EmbeddingError` 构造期统一 `redact()`（provider 装配即 register_secret；无形态 key 测试钉注册路径；复审探针复跑：异常串/events.db/_fail_unknown 三处全 False）；④ 边界声称「网络调用只在 rebuild/CLI search/面板 RPC」漏掉写入激活面（confirm/edit/remember apply 的 upsert→embed 外发）→ SPEC §2.3 边界表补第四行 + PRD/三处 docstring 对账，是否纳入同一 gate 裁决登记为未决（产品行为变更不夹带）。另修 2 个 P2（全量数字 3229 不可复现→绑定实测；「零文件交集」→窄声称）。教训：**「测试直驱的对象」与「声称的入口」要对齐**（安全默认/eval 接口/CLI 壳三处都是同样病根）；**错误串构造期脱敏是唯一可靠层**——不指望每个下游自觉调 redact。修复后门禁（本机 macOS arm64）3221 passed / 8 skipped（575.29s）+ lock/ruff 0。
- **2026-11-15 · W 系列独立复审轮（#155 UI 第二读者）**：#155 复审 **PASS（带 1 项 P1）**——J1–J14 全 PASS、负控独立复跑先红后绿、前端 install/typecheck/test/build 全 exit0 且 build 后工作区干净（提交的 dist=当前源码产物）、**事件 schema 逐字段对照无错位**、后端 3199P/8S 与声称一致、PR body 无夸大无假话。1 个 P1 当轮修复：**子运行状态回填在真实与 demo 场景都不可达**（App 传父任务过滤的 taskRuns，而子 Run 的 task_id 是子 Task id，childRunId 永远查不到；SPEC §2.3 原设计是传全量 state.runs；单测夹具伪造父 task_id 掩盖了它）→ App 改传全量 runs（RunDetail 的 runs 只喂 SubtaskPanel 无其他消费点）+ 夹具改用子 Task id 如实造形。另修：SPEC §8 行数假数字（147/160，初版误写 151/171）；`hasExplicitUsage` 死分支（asRecord 对缺失返回 {} 永不 null——复审抓对，我一开始还误判成活分支）；显式 usage {0,0} 的「有记录且已知 0」与「用量未知」分开渲染（BudgetView 增 explicitUsageResults）；J7 判据文件名更正。教训：**「测试直驱的对象」与「声称的入口」要对齐**（与 #154 的安全默认/eval 接口同病根——单测夹具造形失真会掩盖接线缺陷）；前端纯函数单测绿 ≠ App 装配绿。复审轮门禁：前端 124 tests（+1）/typecheck/build exit0、dist 随源码更新；后端零改动。

- **2026-11-14 · W8 预算账本 UI + 子任务 UI（纯前端消费，SPEC 先行）**：核对控制面 `_methods` 全部 25 个方法——**没有** `budget.*` / 子任务查询端点，且 `budget.py` 不写任何事件（账本是事件库同 SQLite 的纯表投影），因此账户级账本（上限设置、未决预占、bytes/子任务数维度、`overrun`）在事件里不存在；`control.py` 属禁改文件（#146 占用），按任务红线停在契约（[SPEC-W8](SPEC-W8-BUDGET-SUBTASK-UI-2026-11-14.md) §6 给出 `budget.view` 建议端点），**不为闭环加后端**。事件面足够闭环的部分已交付：`subtask.spawned`/`subtask.finished`（都写在父 Run 事件流上，`tasks.py:2462` 实证）驱动子任务面板，`run.start` payload 的 `limits` + `model.result` 的 `usage`/`cost` + `policy.denied` 的 `dispatch.budget_refused`/`dispatch.subtask_limit`（`rules.py` 封闭集）驱动预算面板；口径照 §5.6 第 4 条（已知/未知分列、未知不当 0），拒绝归类只认 rule_id 封闭集不做文本猜测。前端 16 文件 **123 tests** 全绿（基线 13 文件 91 条 +32）、typecheck/build/dist 一致；负控 3 条实跑红→还原绿（usage 缺失当 0 / 漏 `budget_refused` / 未收尾冒充 succeeded，红的测试名记录在 SPEC §8）；后端零改动由全量 **3199 passed / 8 skipped（545.59s）** 与 lock/ruff exit0 验证。demo 世界补种子事件后真实页面浏览器核验：两个面板、C4 拒绝人话与时间线句子全部按预期渲染。§7 G1 条目⑤与 G2 条目「无 UI 页面」已划线部分销账；账户级视图与真实 daemon 的端到端（需 `budget.view` 端点）仍未决。

- **2026-11-15 · 控制面测试屏障修复**：恢复 B 纯文档同步 PR 首次原生 CI 暴露 Windows daemon 控制面测试的唯一红点，0.781 秒超过原 0.5 秒阈值，shard0 与 required 汇总失败，另四 job 成功；同产品代码在 main 首轮通过，但 0.781 的来源未定位。第二读者完整核验失败 run 的 3207 节点、两 artifact 与汇总拒绝，确认不是以成功报告硬套。隔离分支只改测试：长任务改为真实 Event 屏障，控制 RPC 必须在屏障未释放时完成；补外层 finally 保证入口/连接失败也释放，独立真进程正控与两项串行负控闭合。4aae 候选目标 20/20、daemon 文件与全量 3199P8S 均通过；最终 93c1 只改注释并复验目标。不将新测试解释成性能 SLO、SQLite 根因或恢复 B 产品验收。
- **2026-11-15 · PR158 合并与恢复 B 同步续做**：PR158 原生六 job 先完成独立验收，再正常保护合入 main `f307bf16`；main 自身首次 push 原始六 job 成功，但第二读者连续因上游 token 401 中断，故保留“原始成功、独立未完成”而不伪称 PASS。随后将纯文档恢复契约 PR #146 普通 merge 到该 main，仅保留 KNOWLEDGE 双方历史并补本次事实；产品、测试、schema、UI、CI 与依赖不新增变更。
- **2026-11-15 · PR146 保护合并与 main925 自身 CI**：对 #146 `6a5d2130` 完成本机 3199P8S、原生六 job、Windows 3207/9588 与控制面目标核验后，普通保护合并为 `925ca8f2`；新 main 自身 push run43646899748 六 job 再次全绿，Ubuntu 3194/13、macOS 3199/8、Windows 3170/37、UI 13/91，分片 1604+1603 不重不漏，1 次 watchdog 后对应测试 PASS。精确对象第二读者仍由复审服务 upstream 401 中断而保持 PENDING；不关闭 145/147，恢复 B 产品仍未实施。
- **2026-11-15 · 恢复 B T0 v2 集成、Windows 首红与保护合并**：从独立 `PASS_EXACT_T0_V2_CLEANUP_DELTA_ONLY` 候选按原字节接入三文件，修掉 `data/` 规则会漏提交 `schemas/data/restore-v1.json` 的集成缺口。目标 119P；分支 lock/full/ruff 为 3318P8S、542.29s、全 0，原 3199 精确继承。PR 原生首跑 shard 0 暴露测试依赖 Windows 默认 cp1252 读取中文 PRD 的真实可移植性红点；未把文档改为 ASCII、未放宽断言，改为全部 5 处文本读取显式 UTF-8，并以 119P 与窄 ruff 复验。修复后 PR #159 run43651290917 六 job 全绿并由 PR merge-ref/3326 节点/9945 阶段绑定，正常保护合并为 main139。main139 自身 push run43652961032 再次六 job 全绿，首红测试与控制面测试三阶段均 PASS；shard0 长等待 3 次 watchdog 后仍全部通过。该片只提供版本化内部类型、schema 与值级守卫，不提供 apply/review/resume/abort 产品能力；B01–B09 与断点继续 NOT_RUN，精确新对象第二读者仍因 401 未完成。

- **2026-11-14 · W10 子代理深度（写权限 / 深度>1 / fork）**：基于 main@88ec0ab 的 `feat/subagent-depth-write`。占用面自查七条在途 PR（#140/#141/#146/#150/#151/#152/#154），本片只动 tools.py（spawn 2.0）/tasks.py（create_subtask write 声明）/dispatcher.py（spawn 执行器透传）+ 新测试 + 三份文档；loop.py、worker.py、schemas/、事件 payload 结构零改动（#154 在 loop.py 只动 3241–3267 装配段，无交集）。实现范围判定：写权限落地（唯一契约面是红线 8 版本化的 C2 envelope，先例 PRD §5.2.2）；深度>1 必然改 C4 编号语义 + `task.created.depth` 封闭枚举（event_schema.py 981 行）→ 只交四段式契约分析（单独 `docs(contract):` commit a1635ac）；fork 要往子 Run 事件落继承边界（replay 的 messages_hash 要求）→ 同入契约分析，均不实现。授权论证按执行侧结构写进 SPEC §2.3：子写动作与父写动作同一条 prepare→审批→执行侧重判+消费→intent→守卫→Receipt 链；审批绑 run_id（find_for/consume/否决缓存），父不能代批子由「摘要含 run_id/task_id + SQL run_id 过滤 + 消费侧 run 校验」三层承担。13 条判据（tests/test_g2_delegation_write.py）+ 3 条负控全部实跑红→还原绿；负控 W10-n1 的结构发现：单拆 `_find` 的 SQL run_id 过滤不红（摘要层独立承重），必须连 `action_fields` 的 run_id/task_id 一起拆才红——已如实记入 SPEC §4。租约结构：父挂起等子时不释放写租约，可写子 Run 走既有 contended 交还 → TTL 过期 → takeover_reconcile 接管（测试用注入时钟推进，不 sleep）；「父挂起时主动释放租约」留待独立评估。门禁与 PR 另记；授权面改动，独立复审待主会话安排，验收项清单在 SPEC §7。
- **2026-11-15 · W 系列独立复审轮（#157 第二读者）**：#157 复审 **PASS、无代码/正确性缺陷、无 P0**——13 判据全 PASS + 10 独立探针全过（重叠系数恰 0.6 含等号、三级决胜、镜像参数序稳定、跨 kind 零候选、子集吸收不裁决、上限边界、L3 零裁决、5 种排列逐字节同页）；6 次变异全红还原，作者负控账目在「判定点变异」口径下精确复现；门禁 3212/8 复现一致；PR body 逐条对账属实。2 个 P2 文档级发现当轮修复：① SPEC 头部与 PR body 的「与在途 PR 零文件交集」不实（KNOWLEDGE.md 与 6 个 PR 相交）→ 改为「代码/判据文件零交集，KNOWLEDGE.md 共享、预期 rebase 冲突属文档合并」；② SPEC §3.3 标注行排序陈述（`winner_id, loser_id` 字典序）与实现（对内小/大 id 序）不符 → 改 SPEC 对齐实现（确定性不受影响）。P3：§3.1 数字 0.70→0.6522（PR body 的 0.65 本来就对）；`test_cross_kind_pairs_are_not_flagged` 的 `or True` 恒真死代码删除（当前夹具下页必成页，断言改如实）。PRD 170 行漂移不改的理由（#141/#146 占用 PRD）经复审核实成立——契约 PR 合并时必须收口该行。

- **2026-11-14 · W7 记忆冲突消解落地，生成式合成停在契约分析**：分支 `feat/memory-l2l3-synthesis`（基线 main@88ec0ab）。范围判定：**生成式合成不实现**——N3 教训要求另立契约、外发 gate 在 `memory.py`（#154 在途占用）、模型接线在 `loop.py`（同占用）+ 计费属高危面，SPEC §6 交付契约增量分析（方案 B：控制面专属 `memory.synthesize` + `sends_data_off_device` 缺省按外发 + `c4.outbound` 同级审批 + 生成页 evidence 降级为引用存在性校验）；**冲突消解实现**：`memory_distill.py` +113 行——L2 页内对"替换型差异"成员对（同 kind、互不为子集、token 规模相等、重叠系数 ≥0.6；等规模判据是八夹具 m4/m5 互补流程事实 0.65 不误标的关键，子集排除的存在理由是同词重排）按 origin→seq→memory_id 三级写死规则在页尾标注 `[冲突裁决] 以谁为准`（上限 8 行、超限如实报数），**不删行、不改 L1、不改召回、事件 payload 面零改动**（标注全在页正文）；检测是召回导向的，标注说"候选"不断言矛盾。判据 `tests/test_memory_conflict.py` 13 条（`test_memory_distill.py` 22 条零改动全绿，记忆面 7 文件 254 绿）；负控 4 条实跑红还原（NC1 拆子集排除→J4b 红；NC2 规则换序→J3/J12 红；NC3 上限失效→J8 红；NC4 阈值清零→J5/J11 红）。全量门禁 lock/ruff exit0，**3212 passed / 8 skipped（543.58s）**= 基线 3199 + 13。文档对账：PRD 170 行「layer 未落库」与代码 schema v3 漂移（留给 #141/#146）；修 F10 §5 三处「代码未落地」假话（layer×2、origin 迁移规则×1，均与 F10 §13 自身矛盾）；§7 N3 行已更新。SPEC §7/§8 见 [SPEC-W7-L2L3-GENERATIVE-2026-11-14.md](SPEC-W7-L2L3-GENERATIVE-2026-11-14.md)。

- **2026-11-14 · Windows计时与保留诊断最终候选收口**：MCP真实call_tool入口/退出直接受原2秒约束，独立审批/unknown屏障正控、真实blocking与迟调度负控和前置错传播均按预期；accept旧泄漏及9abd新启动错覆盖的FAIL保留，最终569b真探针销账。M2f220局部保留enter起点、最多4个canonical上层caller与有限runtime，READONLY/180事务ring淘汰/7ms注入和两删功能call红证明诊断，原六assert/60轮/1ms不变。第二读者正式限定内容PASS，76件紧凑包逐hash只读保全；root最终1163源lock/full/ruff均0，3199P8skip635.00s、前后无漂移。旧611.04s门不覆盖清理增量，公开73份原字节子集明确不完整；提交预检为该目录日志补最窄忽略例外，保全15份原日志。正式Git对象与新CI另绑；未改生产/期限/重试/CI门、不关闭145/147、不推进恢复B或触碰凭据与metrics他线。


- **2026-11-14 · PR149保护合并与main双红分路定位**：亲读并逐hash保全PR149独立原生PASS与正常合并身份；main c2cf首次六job原件96件1762771B保全，第二读者完整3206节点/全部阶段确认两个真实call红与汇总拒绝，不重跑或取消。MCP原测在精确隔离源用一次观察及审批/unknown两真实屏障证实混合计时，原红保留；M2只定位append_many BEGIN，不能把活跃心跳快照当根因。另开codex/windows-deadline-and-lock-observation，两作者分别窄改MCP计时与M2不淘汰起点/调用链，生产/200ms/2秒/1ms/60轮/CI门不改，最终验收另绑，恢复B未推进。
- **2026-11-14 · 「全做」路线图 + W1 Keychain 凭据后端（`feat/credentials-keychain`）**：维护者拍板全量缺口收口，落 [ROADMAP-COMPLETE-AGENT-2026-11-14](ROADMAP-COMPLETE-AGENT-2026-11-14.md)（15 个工作流 + 外部阻塞清单 + 占用面排序；沙箱/外壳/流式等结构性项单列，不冒充单会话可完成）。**W1 先行**：F12 的 Keychain 后端从 stub 变 macOS 真实现——新 `keychain_macos.py`（ctypes SecItem 直调默认/login keychain，`kSecUseAuthenticationUIFail` 防无头进程交互卡死，`-25300`→未配置 `None`、`-25299`→转 `SecItemUpdate`、其余 OSStatus→`CredentialUnsupported`）；`credentials.py` 能力声明分平台（darwin `implemented/supports_write/secure_storage` 全 True）。**本机探针证据**（SPEC §2）：默认 keychain 全链路可用；`SecKeychainCreate` 一律 `-25294`、`kSecUseKeychain` 文件路由被忽略——文件 keychain 已死，`keychain_path` 参数如实只收 None。**显式偏差**：测试直接对真实 login keychain 自证（唯一 service 名 + finally 清理复核 + 锁定 `-25308` 如实跳过），「临时数据根」纪律在本工单无法完全遵守、SPEC §3.4 登记。三态契约测试存活：防回退在 darwin 改断言「空 keychain + env 假 key → 构造报缺少 API key」。定向 43 passed / 3 skipped（skip=非 darwin stub 测试）+ 负控三条实跑红还原（NC1 未配置吞成空串 / NC2 拆 update / NC3 静默回退 env）；不碰 C1–C5。平台陈述：仅 macOS，Windows/Linux 仍 stub，CI macOS runner 钥匙串状态以当次 run 为准。
- **2026-11-14 · W2 网络重试与速率下限（`feat/net-rate-retry`）**：推翻 `net.py`「不自动重试」的刻意不做，但**把重试边界钉在授权语义上**：只重试 `net.connect`/`net.connect_timeout`（TCP 从未建立、零字节到达对端，重发不构成第二次外发请求，不需要重新批准）；TLS 之后一切失败（读断/超时/协议/任何响应含 5xx）绝不自动重试——**服务器端连接数测试钉住这条边界**（NC3 首轮把变异加错在 net.protocol 上、实际场景是 net.read，假阴性被手动复现抓出后修正——负控必须验证变异命中目标路径）。`Fetched` 附加 `attempts`（不进工具输出，dispatcher 零改动）；进程级速率下限 `min_interval_s` 默认 1.0s 可关（等待分片可取消、不计入抓取 deadline），env/构造注入两级覆盖。新增 `tests/test_net_resilience.py` 11 条（重试恢复/耗尽/单次关闭/发送后不重试/取消/限速语义与集成/env/校验）；网络面四文件 64 passed；conftest 增 autouse 把限速钉 0（进程级状态不钉会互相等待）。文档对账：net.py docstring、network-g2.md §3.1/§7 两处、KNOWLEDGE §7 G2 行。不碰 C1–C5。
- **2026-11-14 · W6 真 embedding provider 与外发 gate（`feat/embedding-provider`）**：外部 OpenAI 兼容 embedding 端点接线（`embedding_openai.py`：http.client 直连、TLS 恒开、不跟随重定向、批量 ≤64 分片、错误封闭集 auth/quota/status/network/timeout/shape、dim 显式配置且响应不符即拒）。**核心设计是外发 gate**（SPEC-N2-EMBEDDING-PROVIDER §2.3）：`retrieve(..., allow_embedding_outbound=False)` **默认 False**——模型工具的检索永不触发端点请求（零网络调用被测试钉死），自动降级 FTS-only 并如实标注；网络调用只允许发生在人显式发起的控制面动作（rebuild / CLI search / 面板 RPC）；provider 用 `sends_data_off_device` 自报能力、缺省按会外发处理（安全默认，`_ScriptedProvider` 等本机测试 provider 补 False）。配置只从环境变量读（四件套），密钥永不落盘。**千条级评测**（`scripts/eval_memory_scale.py` + `docs/memory-scale-eval.md`）：确定性合成背景 + 固定 20 黄金 query，N=1000 下 FTS-only Recall@5 掉到 0.650 而 hash 融合保持 1.000（小样例饱和掩盖规模效应的判断被证实）；k∈{30,60,100} 在该分布下 MRR 逐位相同（不敏感）；真 provider 分数留接口未跑（无真实凭据，不编造）。测试：test_embedding_provider 13 条 + test_memory_outbound_gate 6 条（模型检索零外发/控制面显式外发/hash 不受 gate/回滚不留半套/安全默认/评测脚本判据）；负控 NC1 拆 gate→2 红、NC2 拆 dim 校验→1 红、NC3 拆评测脚本 hybrid rebuild→1 红，全部实跑红还原。不碰 C1–C5、事件面、工具 schema。

- **2026-11-14 · M2诊断路径别名真红与窄修收口**：保留9eb独立FAIL、旧完整门及审阅观察器错误；作者仅两处resolve+真实symlink/junction参数化回归，修前1F1P、修后12项诊断与42项整文件绿。第二读者新3P及两类单点行为负控关闭POSIX路径别名缺陷，按1111精确继承既有有界/原异常/真实两个BEGIN证据。root亲读并逐hash保全54件旧FAIL与38件新PASS；最终1112固定源lock/full/ruff0，3198P8skip546.63s，前后稳定。门后只补事实与原字节公开子集，最终对象/新CI另核；原Windows根因及145/147均未销账，恢复B未推进。

- **2026-11-14 · main新红独立销账与M2诊断续做**：亲读并逐hash保全main b450独立FAIL原件77件，以及恢复B本地merge277对象绑定PASS原件10件；原恢复分支保持干净未push。隔离调查原件保留真实heartbeat持锁下两个不同BEGIN的同指纹和无hold正控，明确原CI根因仍未知。另开 `codex/windows-m2-lock-diagnostics`，只授权原正常收尾用例的测试侧有界诊断；保留六assert/60轮、生产/CI与所有旧红，独立验收及本机/原生CI另核，B产品未授。
- **2026-11-15 · W 系列独立复审轮（#150 metrics 轮转第二读者）**：#150 复审 **PASS（1 P2 + 5 P3，无 P0/P1）**——十条判据独立复跑全 PASS、并发 8×25 真线程 Barrier 探针 200/200 恰好一次、真子进程跨进程竞态探针、变异 6 个全实跑、门禁 3207P/8S 独立复跑（与作者 3194 差 13 = rebase 带入 #153，基线收集数实测核对无造假）。当轮修复：① **[P2] `>=` 阈值边界无测试钉住**（复审变异 `>=`→`>` 后 22 条全绿，唯一存活变异）→ 垫出「恰好等于阈值」文件的边界测试（双臂：== 轮转 / 低于阈值不轮转），同变异现在当场红；② **[P3] 全角数字后缀误入轮转清单**（`isdigit()` 对「１」为 True 且 int() 恰好解析 → 同槽位重复、真实文件读两遍）→ `isascii() and isdigit()` + 测试；③ 轮转成功但样本写入失败时不补 `metrics_rotated` 事件 → except 分支 best-effort 补记；④ `limit=0` 返回全部的 `[-0:]` 怪癖（基线既有）→ docstring + 测试如实钉住历史语义。**更正（勿删历史）**：下条 2026-11-14 日志的「与在途 PR #140/#141/#146 无文件交集」不实——docs/KNOWLEDGE.md 本身与 #140/#146 相交；PR body 与 SPEC 的窄声称（不触碰两个代码文件）经复核实测准确。教训：**「阈值边界（==）是最容易漏钉的判据」**——off-by-one 变异在只有「明显超过」场景的测试面里是存活变异。
- **2026-11-14 · 指标文件轮转与保留（`feat/metrics-rotation`）**：针对 §7 登记的「指标文件无轮转」落地**默认开启**的按大小轮转：追加前锁内 stat，超 `max_file_bytes`（默认 16 MiB）先链式 `os.replace`（`metrics.jsonl.<槽位>`，保留 4 份、最旧被覆盖、无 DELETE）；失败降级为继续追加当前文件（`rotation_errors` 计数，**样本不丢**优先于文件不超限）；`metrics_rotated` 事件可观测且不级联（阈值小于一行时防无限递归）。`read_metrics` 增 `include_rotated`（时间序读回全部历史）与 `list_metrics_files`，**默认签名不变、既有调用方零迁移**；构造/env 可覆盖，`0` 显式禁用。全部收在 `metrics.py` 内部，不改 daemon.py、不碰 C1–C5。冲突面核查：与在途 PR #140/#141/#146（文档对账/restore 契约）无文件交集。SPEC 判据 8 条新增全绿（`tests/test_metrics.py` 定向 22 passed，既有 14 条不变）+ **负控三条实跑红还原**（NC1 拆大小检查→轮转/保留 2 红；NC2 失败改丢样本→容忍用例红；NC3 摘级联抑制→RecursionError 红）；本机 macOS arm64 lock/ruff exit0，全量 **3194 passed / 8 skipped（546.53s）**（= 基线 3186 + 本轮 8）。文档对账：runtime-metrics.md §10、KNOWLEDGE §7 指标条目已同步。状态以对应 PR CI 为准；不宣称 Windows 实机验证，80 MiB 上界是估算默认值不是冻结政策。

- **2026-11-14 · 审批失败诊断短关键字段收口**：初版独立PARTIAL确认总截断/长error丢关键事实；第二版独立真红确认短receipt.resource遗漏，原全量由root主动中断exit2并保留，不动GitHub原首红。作者先增断言真红再仅补摘要字段；最终v3独立PASS_DEFINED_DELTA_SCOPE，两新正/两旧真实负控与原成功一次有证据，原13assert及未变真实拒绝/fallback边界按身份继承。最终源f138d6c1在1080项固定输入上lock/full/ruff全0，3186 passed/8 skipped（544.97s），前后身份稳定。只发布事实文档和原字节公开子集；字符而非KB、首页1000事件、任意长字段有损及脱敏/墙钟限制明确保留。原147根因NOT_ESTABLISHED、146未合，正式对象/新CI另绑，恢复B产品和真实付费/soak未启动。

- **2026-11-14 · PR146审批删除首红与独立诊断分支**：核对原生run43559488826六job/两artifact及原call断言；第二读者固定77源50个本机独立进程未复现，结论NOT_ESTABLISHED，不归因Windows/flaky/145或基线。187件triage原件逐hash保全，50个pytest current链接仅留metadata。另从main8f2a建诊断分支，只给原测试失败补有界真实链及采集失败保护，原13assert/时限保留；真实受控拒绝前后同一原断言仍红，恢复后作者11 passed。独立审和集中门禁另核，PR146未合，issue147未关闭，恢复产品未授权。另亲读并运行恢复B内部接口seal核验：12件/20输入未变，仅计划层PASS，不当产品验收。

- **2026-11-14 · 低盘最终组合首红诊断与收口**：879 固定候选的首轮全量41失败、3141通过、8跳过，lock0；同源完整UI91项及type/build通过，源码与dist稳定。root亲读并逐字节保全879文档增量独立PASS、首红和剩余门禁；第二读者用三个离线场景确认过早pure_only提案过滤跳过原派发前父fence与拒绝审计，而最终子结果回填仍被阻止。修复保留原AUTH/eval/fence断言、模型pure清单及执行边界门；旧手装Loop补同栈实例，内层记忆探针只故障注入上游以取得真实permit。六份历史脚本按原字节移名文本，原173索引不改、显式路径映射全部闭合，不新增lint排除。修后最终组合、独立增量、正式对象与CI另核；没有把旧局部绿或观察器错误当成交付。

- **2026-11-14 · 低磁盘 T1 C2 纳入 round1**：在同轮 C1 候选上复现真实 v2 policy 的1.0写入/严格重读不一致，按额外授权只在新v2结构校验后调用既有严格对象语义，并在镜像说明标准 JSON Schema integer 的边界；不改对象、旧schema或其他integer语义。真实追加/重开回归2红8绿→10绿，删hook2红8绿并复原；旧v0/v1和历史fixture、其他当前schema、严格对象字节均保持。最终14文件467 passed（11.17s），窄ruff/py_compile通过；静态对账脚本首轮误用系统python未找到项目模块，改为uv冻结环境后全部通过，原观察器错误保留。与C1/takeover共40文件新冻，旧原件与旧报告前缀不改写，等待独立窄复验；本轮无full/lock/CI、commit/push、T2/T3或付费调用。

- **2026-11-14 · 低磁盘 T1 C1 round1**：保留独立晚低盘控制创建遗留 Task/事件的原始反例，新增真实控制/remember/undo 的全高、初低、晚低、晚未知对照和外层事务回滚回归，七张表完整行及已有快照/工作区字节一起核对；旧实现6红7绿，最薄 Task/Run 同事务后13绿，移除修复6红并复原。九文件关联249 passed（9.73s）；补查 takeover 时发现原T1漏传真实 memory/contents 的子进程装配，旧冻结同样1红5绿，经授权仅补两实例后6绿，移除参数原context错误真红。旧39文件冻结、原件与旧报告前缀均保留，新40文件另冻待独立窄复验；窄ruff首轮3项格式错误也保留，修后通过。未做 T2/T3、full/lock/CI、commit/push、UI 构建或真实模型/付费调用。

- **2026-11-14 · 低磁盘 T1 创建纵向候选**：在固定605作者树完成 policy/逐位置探测、`run.start` v2 与全部 Run 生产者接线，旧v0/v1 schema和历史fixture保持原解释。扩展红暴露 CLI 省 contents 的真实生产回归及 MCP 重复准备重建注册表丢失死 server 工具的回归，已补实际实例并只在首次 MCP 准备后登记；手装配测试仅补真实依赖，缺 claims/leases/data_root 与可组合事务负例保留。纯读模型真实清单与运行期间租约观测绑定实际 registry/lease，避免仅在终态查询漏掉临时写租约。23个真实变异均被定向断言杀死并逐文件复原，原首红与观察器误用均保留。最终27文件关联800 passed / 1 skipped（38.12s）、计划六文件联合exit0，窄ruff/py_compile/旧schema及历史fixture字节核对通过。只形成本机 T1 候选，冻结另记 ignored ledger；T2/T3、独立复核、统一全量与原生 CI 尚未完成，无产品commit/push、真实模型或付费调用。

- **2026-11-14 · 低磁盘 C4 拒绝编号契约增补**：将资格不允许、低盘与空间未知区分为三类待实施拒绝，补入独立契约与 PRD 待实施指针；区分真实 0、测量无效/位置不可证明及持久资格损坏。当前 75 项可写清单不变；本草案阶段未改运行时代码/schema，未运行产品、测试、API 或模型，后续正式提交检查另按实际记录；LD-01–06 保持 `NOT_RUN`。

- **2026-11-14 · 低磁盘准入契约与 PR #137 状态对账**：将文字 v2 整理为独立待实施契约，保留 pure Dispatcher 调用、停止空间例外、既有 effects/legacy 读与模型恢复，以及 authority/guard 真实位置；PRD §5.3.5 / §8.4 增加引用。第二读者只确认两项文字问题已修，LD-01–06 全部 `NOT_RUN`；产品另 PR。补录正式 head `5d046f7` 的六 job 原生 CI 与独立核验通过，同时披露正常合并请求502后的真实 main `6c291a6` / PR仍OPEN、无该main push CI 的服务端不一致；一次同 SHA 请求恢复405后停止修改重试，不伪称已合并或补造 main 绿色。历史红与候选原文保留，未运行真实付费任务或十小时长跑。

- **2026-11-14 · Windows 分片独立C1复验与最终本机门禁**：第二读者仅重验v2受影响12个真实场景，确认C1已修、部分修/未修/内容未决0；未变主线按精确v1独立报告继承，旧收集错误改码及作者回归时序如实保留。root在H3 `e324edeb9c57cd9e8fe851a2657da8420e7838a2` 加六文件v2冻结组合集中执行lock/full/ruff，全部exit0，2987 passed / 8 skipped、pytest485.59s（命令485.79s），537文件/HEAD/status无漂移；之后仅补本知识库事实，不冒称正式新提交自身已跑。正式对象独立绑定、单push后的六job原生CI与两份artifact仍待核，不称Windows或第二批整批完成。

- **2026-11-14 · Windows 分片 C1 最小整改**：亲读独立同fixture原 wrapper exit2/分片exit3原件；只改插件、新基础设施测试与本知识库事实，其他三源及原包装器/collector/锁不动。精确新回归在冻结v1上4红/2绿（含0/1/2有效文件和收集晚期中断），候选新增文件71 passed / 0 skipped（16.60s）；初始5项先跑候选再回退冻结v1验红，ledger如实记录时序，不伪称最初red-first。旧v1冻结和root旧537对象历史不覆盖，v2另冻，独立复验、集中全量与新CI继续待核。

- **2026-11-14 · Windows 双 runner 分片基础设施候选**：H3 三绿/Windows25分钟取消如实入账，保留旧失败与部分输出边界。新增开发插件按真实完整 collection 分组、记录阶段，汇总同时核本 run/attempt/SHA、节点覆盖和上游 job success；四 required 名称、原 wrapper/fatal/GIL 边界与25分钟不变。真实子 pytest 覆盖三阶段 skip/xfail、XPASS、失败/中断、缺件/身份/阶段变异及迟发诊断 session0→wrapper3；手算第三文件归属首错单列为观察器错误，不计产品负控。实施仅六文件，不碰另一会话业务修复；新增与原包装器定向合跑98 passed / 0 skipped（25.36s），窄 ruff / actionlint 通过，原包装器、collector、pyproject、lock 与 UI job 字节不变。独立实施复审、集中全量和新 CI 结果继续分开绑定，当前不称 Windows 通过或性能改善。

- **2026-11-14 · PR #137 集成另一会话已合修复**：远端 main 前进至 `016910970d381a66ab12a94a5c88080a8cbf7437` 后，在本会话分支正常 merge；保留 collect-only、Windows 诊断与第二轮 debug 双方知识库历史。生产与 UI 继承上游，收集器及测试继承已审候选；实质组合变化后重新冻结并集中跑本机门禁，lock/full/ruff 全过（2916 passed / 8 skipped，pytest汇总489.93s），535文件及HEAD/状态无漂移。独立读者核535文件来源及三处冲突的双历史保留；随后仅回填本知识库两条事实，最终正式对象与新原生CI另绑，不借先前2743项结果或另一会话 CI 代替验收。

- **2026-11-14 · PR #137 Windows 失败定位与最小测试修正**：保留首次三红及25分钟取消、两次短诊断和 PID 诊断原件；独立读者分别核对实际源码对象、Windows LF→CRLF 工作树字节与真实进程身份。wait-only 的早期充分性判断因遗漏 venv 启动器身份而撤回，旧报告不覆盖；直接解释器两 phase 的实际 PID、退出和收据对照支持最小测试候选。只改收集器测试与本知识库，不改生产模块或另一 debug 会话范围。第二读者独立窄审通过，3项真实 CLI 正控和 PID 伪报拒绝分别留证，审阅汇总脚本首错亦保留；作者另留2项正控、10项预期断言负控，均不与产品测试数相加。冻结未提交组合集中一次 lock/full/ruff 全过（2743 passed / 8 skipped，pytest汇总462.23s），524文件及HEAD/状态无漂移；随后仅补本知识库事实，新正式对象与原生 CI 仍待核。未运行真实模型或10小时长跑。

- **2026-11-13 · collect-only 独立复验与最终本机门禁收口**：亲读并逐hash归档v1/v2/v3独立原件，BE-01早期源码身份与BE-02目录忽略兼容均独立已修，未决0；保留所有真实红与观察器首错，不覆盖历史。冻结v3组合集中一次lock/full/ruff通过（2743 passed / 8 skipped，445.72s），524文件及HEAD/状态前后不变；随后仅对账三文档和修正导航，不改已审代码/测试或另一debug会话范围。正式提交对象与新原生CI继续单独绑定；未运行真实模型、soak或用户设备验收，不把收集收据当执行批准。

- **2026-11-13 · BE-02 单点整改候选**：按原生 exact1/slash0 反例先加回归，旧 v2 脚本有效3红/7绿；首轮另外两条 tracked 子树断言误假设原件保留并明确排除。只改收集器的目标 index 查询与目录 ignore 语义，补 `[]` 字面目标/相邻跟踪名字正控，修后BE-02十一绿、BE-01六绿、关联118绿，窄ruff通过。三源相对v2另冻，两指南及v1/v2历史材料保持；只记录作者候选，不抢原读者BE-01结论。独立复验、组合全量与新CI交root接手，不提交推送、不运行模型或soak。

- **2026-11-13 · BE-01 单点整改候选**：独立探针在首次报告读取完成处改变 v1 collector，旧候选仍返回0并发布收据；无改动正控正常，变更检查点和 clean Git 均实际到达。作者先加六回归，保留旧脚本上 3 红/3 绿：一项真实错误完成、两项入口读取顺序不符；只前移初始 hash 读取，末尾复核不动，修后六绿、关联107绿。测试覆盖早期改字节、真实删除、叶子 PermissionError、入口拒读先于 Git/报告/输出，错误仅固定类别；不声明 OS ACL 或 Windows 实跑。旧38份冻结材料及两指南保持，新的源码/测试/知识库与原始日志另冻；BE-01 独立复验、最终全量和新 CI 待 root 接手，不自批、不提交推送、不运行模型或 soak。

- **2026-11-13 · 第二批 collect-only 候选**：在 `cefa1e375974d96e2ec25e199f6605f6dcf2239b` / tree `fca43ac29d44dc5cca1801dc05b3bb9832a95682` 隔离实现显式报告/指标收集器，只改两份新 Python 与三份指南/知识库。Task 1 先红后 38 绿；Task 2 保留源码/HEAD/输入/收集器四红及删除源六红，再补最终复核得定向 54 绿；关联 101 绿、窄 ruff 通过。真实子进程以二进制 PIPE 屏障确认发布前/后再终止，未把启动失败当作证据；Windows 尚未实跑。原件私有、摘要限字段、成功静默、不认证历史执行；独立复审、组合全量与当前提交 CI 另绑，不自批、不提交推送、不运行模型或 soak。

- **2026-11-13 · 第二轮系统debug修复与独立复审**：按用户“开始修复”和集中CI要求，在隔离debug-round2工作树从最新cefa建立冻结基线，三作者唯一文件所有权修复19项；root与独立第二读者各验授权、输入、数据、预算/UI/摘要。先旧实现真红再修，保留初始夹具错误与首全量AST误报；摘要REAL截断被二审抓出并补修。不改契约、依赖、job上限或required checks，main后来仅8行教训文档安全快进保留。真实浏览器记忆写/刷新失败及晚到回包截图来自实际dist与Host/Daemon，临时进程正常收尾。最终门禁、完整SHA及CI另由对应PR绑定，不把本机专项绿当云端绿；原件E与可执行测试见第二轮总账。

- **2026-11-13 · T3 迁移 CLI 候选**：仅接显式标准根与现有备份；首轮真实 RED 为 25 failed / 26 passed，只证明入口尚缺，未冒充深层协议负控。接线后首轮 51 passed，再补三种标准根与两种编程异常边界得 56 passed；六 code、四硬退出续做、真实跨进程互斥和失败原子性均实际到达。T2 24 项非知识库冻结原件与其他非授权基线无漂移，未改另一 debug 会话范围，未 full/CI/commit/push。更新维护指南与独立 T3 事实原件，交第二读者验收；不自批、不借历史全量或 CI 代替本批次结论。

- **2026-11-13 · T2 一致点绑定候选**：在 `585e347` 稳定 T1 基线上仅改 digest、迁移协调器和两份授权测试，普通 v0 兼容、T1/backup/maintenance/CLI 与其他 debug 范围不变。保全初次 RED 与首轮七红；纠正把合法 after-step1 当跳步的负例，保留全部窗口正负控、原 future-target 拒绝与四个真实 77。隔离旧源码 11 项真正未拒绝重演；补查空主库历史旁文件入口三红→三绿。最终定向 90、关联 208、字节 I/O 门禁 1 项通过，lock/ruff/diff0、438 个非授权文件无漂移；事实文档与冻结原件交第二读者，未跑全量/CI、未提交推送，不自批 T2 或宣称 G3 完成。

- **2026-11-13 · 数据准入 T1 候选**：在独立工作树按已审计划实现实际 EventLog 前缀与严格 state，不改迁移协调器/CLI。固定历史 SQL/format1 状态从基线 Git 对象提取；保全首次 RED、126 测试在原 Git 源码上的明确标注重演，以及隐藏列/索引三红到绿。首轮全量三红分清：字节 I/O 违规与空目录骨架误判经回归修复，价格节点在原基线原环境同样红而过滤配置后绿，未修改其产品/旧测试。过滤配置后全量 2542 passed / 8 skipped，424.60s；第二读者指出重复字段测试缺合法正控，随后仅增强该测试并在临时去 hook 源码上取得两红、当前产品关联 300 绿，lock/ruff0。最终新测试 127 项，产品与已绿全量一致；新测试/后续整合组合的全量、独立结论及原生 CI 分别绑定，不借旧绿作新结论。

- **2026-11-13 · G3 准入/迁移契约分片**：只读临时探针确认正常库仍为 v0、实际自定义 DB 可被无关根的版本误作证、现有迁移只看 daemon/host 而不排斥普通连接、异根备份可被接受，且旧备份恢复会带回旧 active 记忆和已存正文游标。比较最小准入与完整产品格式后，先明确实际文件准入、备份/计划/进度绑定及离线互斥；完整 schema 集中冻结另行处理。恢复选整批持久执行隔离，具体字段与实现后置，避免只挡新搜索却漏旧游标。本提交只改契约与状态指针，不修改产品或另一 debug 会话代码。

- **2026-11-13 · G3 首片吸收已合修复**：同步远端 main a839906 的 I/O 与预算修复，唯一知识库冲突按段保留两边历史；保留本轮 UTF-8 测试修正、备份与只读恢复预演，不修改另一会话的业务实现。新组合本机 2416 passed / 8 skipped、425.09s，lock/ruff0，253 非文档文件无漂移；UI 80 项及类型/构建通过，dist 字节一致。第二读者核 353 条路径按双方对象继承，提交级绑定及新原生 CI 继续另记；最终同步后再集中推送，未借历史 CI 预填通过。

- **2026-11-13 · G3 首片 Windows 编码修正**：PR #134 的 `f509a53` 初次 CI 中 UI/Ubuntu/macOS 成功，Windows 为 1 failed / 2244 passed / 35 skipped，唯一失败是本片新增测试用默认 cp1252 读取 UTF-8 记忆正文。仅把该行改成显式 UTF-8，不改产品、断言或 CI。第二读者携带父提交原 conftest 在 ASCII 默认编码环境亲跑原红→新绿及 144 项回归；完整修复提交绑定与新原生 Windows CI 另行记录，不借另一 debug 会话绿色作证。原始失败和独立证据见 [本轮记录 §2.1](DATA-LIFECYCLE-2026-11-13.md#21-首次-ci-与-windows-测试编码修正)。

- **2026-11-13 · G3 数据生命周期首片**：按用户授权分批推进，避开另一 debug 会话的 I/O、预算、恢复及 Host/CI 修复。补齐离线 backup / 独立 verify / 只读 restore-plan；修正旧备份遗漏根 blobs 与 memory_blobs、manifest 隐藏引用、只读连接失败回退读写等边界。维护准入绑定真实 SQLite 连接，关闭失败或外部连接/游标继续存活时不提前放锁；锁固定在 OS 用户临时根，避免数据根上级权限和可变环境影响身份。独立复审发现并修正清理候选漏引用、静态 WAL 库校验生成旁文件、缺投影误报空状态；macOS 根别名在创建时核对后保存，后续校验不回源。发现迁移版本 2 只是演示而实际产品库为 v0，决定先做独立版本/恢复隔离契约，再接迁移与恢复 apply；本轮不改契约，不做 GC。测试、独立复审与 CI 口径集中见 [本轮记录](DATA-LIFECYCLE-2026-11-13.md)，不借另一会话的历史绿色作证。

- **2026-11-13 · 最终测试版本地集中门禁**：Host68ea不变，正式测试dc274a及协议本地内容预验通过（独立报告0988a65c…）。三批候选并行跑lock/ruff/full全部exit0：I/O2219/8、437.57s；预算2293/8、439.61s；恢复2316/8、442.55s。各248/249/251个非文档文件前后无漂移，日志原HEAD与最终工作区内容通过冻结清单映射，不拿旧dd49 CI或旧测试全量代替。正式SHA绑定、最终Windows探针与四required仍待；集中推送节奏保留，不提前推后两批。

- **2026-11-13 · C-007 Windows六红分类与最终测试协议冻结**：正式dd49/run43367761924 Windows为6失败/2189通过/32跳过，其他三项成功；cdc隔离实验2失败另记。第二读者精确dd49/cp1252五红→唯一真实CLI初始化五绿，报告c9aef076…及37原件台账4d89a387…；未回填成原CI丢失stderr。正式helper只补入口、保全原异常与stderr/worker/清理输出；share0断言EACCES并保留字节，monitor同连接0.4秒边界→ready后5秒完整读取，产品68ea不动。最终dc274a测试本机cp1252整文件19/1、24.06s，hash稳定；真实child自然exit1和父失败后Host/Daemon均无残留已验Mac。实验握手/锁内真实os._exit23及validator同步校准，撤销PYTHONPATH被pytest覆盖的无效负控，未将中断测试认绿。新独立复核及Windows/四CI待最终集中执行，无commit/push、无中间全量，未改生产、契约或门禁。

- **2026-11-13 · 恢复批吸收最终Host修复**：从4eb合入预算609，产品仅继承已审dd49的Host边界和20项回归，知识库冲突逐段保留恢复、预算与I/O历史。本机2316 passed/8 skipped、424.20s，lock/ruff0，327跟踪文件全量前后hash不变；独立151项和六组崩溃/恢复探针内容预验通过，正式SHA绑定另记。修后Windows实验仍红，正式测试/取证协议待修正，不把该旧测试组合绿色移用到后续版本。按用户要求，全部修复、回归与独立复审本地稳定后集中推送跑CI；不省略当前SHA四required及strict同步，不提前报完成。

- **2026-11-13 · 诊断修复整合**：合入I/O `7111cbb43cd71db9f25a1348d91e7b39a0d97dee` 的测试包装器、MCP夹具与文档。生产src、UI、锁文件相对上一轮已审对象不变；知识库冲突保留双方记录。重新冻结组合并完成本机门禁：2297 passed / 7 skipped / 396.59s，lock/ruff均exit0，运行期hash全匹配；最终SHA独立继承与当前CI另行记录，不移用旧CI绿色。

- **2026-11-13 · 系统 debug 末次整合与 Windows 阻断**：预算 B3 游标持久化与 I/O sender 竞态补丁完成独立复审；强化恢复探针同时阻断两种 authority 登记，原实现真红、修复真绿。两处整合全量及锁/lint通过，源码指纹冻结核对；保留最新 Windows access violation 失败原日志，继续调查，不以历史绿、重跑或提高超时绕过合并门禁。

- **2026-11-13 · 系统 debug / 恢复批次**：复现命令执行后误判none_applied、恢复二次崩溃丢阻断；实现执行边界判据、本地原子提交、历史孤立unknown补偿及unblock保留。原始故障注入与回归记录在 [DEBUG-RECOVERY](DEBUG-RECOVERY-2026-11-13.md)，跨库authority限制如实保留，交独立第二读者审验。

- **2026-11-13 · 预算整合Host最终补丁**：吸收dd49精确Host/20项测试及证据文档，保留预算与I/O知识库双方记录，纠正556历史对象继承已完成的状态。新组合323文件全量前后不变，2293/8、420.63s，lock/ruff0；预算业务代码未改。第二读者重新检查关键预算契约及正式对象，当前CI尚待，不把本机绿或旧a3结果当合并资格。

- **2026-11-13 · 诊断修复整合**：合入I/O `7111cbb43cd71db9f25a1348d91e7b39a0d97dee` 的测试包装器、MCP夹具与文档。生产src、UI、锁文件相对上一轮已审对象不变；知识库冲突保留双方记录。重新冻结组合并完成本机门禁：2274 passed / 7 skipped / 394.72s，lock/ruff均exit0，运行期hash全匹配；最终SHA独立继承与当前CI另行记录，不移用旧CI绿色。

- **2026-11-13 · B3 二审补修**：`51d46e06f230d50b8d7ec27eb5ce3d3c763c2a0a` 上复现摘要失败后普通调用因金额不足被拒，内存未知为 1 而终态为 0。正式新增两 provider 回归先红，再在摘要调用后立即保存现有游标，正式与冻结独立探针均 2 绿；数据库重开、未决账本与 replay 保持一致，受影响 183 项通过。本机全量 **2237 passed / 7 skipped / 384.75s**，lock/ruff 通过。未改费用或数据契约；独立复审与 CI 仍按最终版本绑定。

- **2026-11-13 · 预算独立复审整改（B1/B2/B3）**：在 `e89a95d4c7e8f7196b38c51148e294d4c72bed9a` 的独立工作树中，将复审发现吸收为正式回归（首批 16 项原红，最终 32 项在冻结原实现全红、修复后全绿），接通金额预占/结算/父子闸、摘要失败 unknown 和旧非法账本拒读。原 40、冻结候选 8、关联 328 项通过；旧版本 API 种出的负账本重开后两次拒绝、四表不变。本机全量 **2235 passed / 7 skipped / 381.25s**，lock/ruff 通过。持久化仍是整数 need；零货币预留与未知实际费用明确区分，未引入 null 数据格式或假价格。独立二审与 CI 按本轮最终提交继续绑定，作者不自批。

- **2026-11-13 · 系统 debug / 预算批次**：修复部分 usage 释放预占与负 usage 回补预算；两真实适配器、普通/摘要、父子账本回归与真子进程竞争验证。沿用既定未知计费契约，未动历史用户账本；高危变更交第二读者绑定提交复审。

- **2026-11-13** · **C-007第三版本机与独立内容预验收口**：68ea源码/3c68测试冻结全量2219/8、407.48s，lock/ruff exit0，245非文档运行前后hash一致。第二读者57/1及真实monitor、两启动窗口、stop替换owner、supervise=True拒删/原异常均通过；内容报告865def8e…，正式SHA与修后Windows/四CI仍待绑定。同步纠正C-004/C-005/C-006的旧“待批准”叙述，保留其已经完成的精确领域批准，也保留7111整轮Windows红和d6ee/a624两次候选退回；未提交运行数据、未改契约/超时/重试/门禁。

- **2026-11-13 · C-007修复新引入的monitor启动竞态**：a624虽全量2218 passed/8 skipped、404.50s且245非文档文件hash无漂移，第二读者仍以真实Thread.start屏障复现HTTP可停止、monitor尚未启动，join裸RuntimeError、log_open=True而CLI/run_host均0，判未批准；clean7111相同时序正常，本轮排序引入不能算既存问题。第20项正式回归先在a624原红1项/5.62s，再只将monitor启动移到同guard内条件发布之后、HTTPserve之前，保留双Host互斥与败者不关共享Daemon。新68ea/3c68内容hash作者定向57 passed/1 skipped、52.92s、ruff通过；断言覆盖真实停止、后台stderr、monitor/log/socket及Daemon/元数据。旧全量明确标历史未批准，新内容仍交独立反例与负控复跑、重新全量和正式SHA CI，不把本机作者转绿当独立批准。

- **2026-11-13 · C-007独立复审再收紧发布边界**：初版Host元数据guard虽通过2217/8本机全量，第二读者在真ensure+实际端口已写、serve尚未启动的屏障上复现B接管后A继续serve，两个不同PID/端口均HTTP成功，判部分修未批准。只采用分别采A/B的v2证据，误重复采B的早期探针撤销。第19项正式回归先在d6ee原红1项（0.79s），随后把条件端口发布与服务线程启动纳同一短guard；停止回调先装、monitor后启，无HTTP/Daemon锁内等待。修后探针以B真实OS锁busy后放A，不制造等待Bready的自身死锁。新a624/4af4内容hash冻结，作者定向56 passed/1 skipped、47.19s、ruff通过；重新交第二读者与全量门禁，旧全量不冒充新批准。

- **2026-11-13 · Host停止与实例所有权：C-007/P2**：正式7111四项CI中只有Windows失败，6.41秒的正常Host停止泄漏WinError5；保留失败状态，确认既有31项诊断回归实际通过。独立Windows实验真双进程10组3次重现同读双删的WinError5，持久只读5及普通读句柄32作不同负控；人工share-delete候选本轮无错，未冒充根因。独立真文件/真进程又复现拒读假缺失、旧stop删新owner、旧健康结果覆盖新owner，以及A真ensure后晚发布覆盖已运行B。先跑首批11红/1绿，旧健康检查与真双Host回归另各原红1项，再复用run目录内既有OS guard和实例快照条件操作。败者返回非零并只收本实例socket/log，monitor在发布后才启动；正常stop仍由真Host+Daemon验收。作者冻结源码/测试的定向55 passed/1 skipped、41.37s、ruff通过；未改C-005诊断包装器与C-006夹具，也不把本机定向结果当成新提交批准。证据与待完成门禁见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。
- **2026-11-13 · C-005测试证据校准与等待侧收口**：独立setup屏障反例确认Lock正控提前采样会误红、仅查header的三阶段测试会误绿；测试ready同步并检查各阶段独立函数帧，锁诊断输出改临时文件，隐私检查确认目标函数在栈中。第二读者再以1e30确认后台wait溢出仍pytest exit0；最小增加TIMEOUT_MAX配置上界并把wait/snapshot同置try，默认60秒及生产src不变。新增四项先在旧实现4 failed，最终31项新回归+8项eval为39 passed/18.87秒、ruff通过。精确中断先前full留下exit2记录，历史2195/7不冒充最终门禁。最终八文件冻结整合为lock/ruff/full exit0，2200 passed/7 skipped、390.82秒，运行前后hash相同；独立内容hash预验覆盖19场景、10配置、阶段扰动及C-006失败清理。结果后仅回填两文档；正式SHA复审和四项CI仍待完成，不以自查或内容hash预验冒充提交批准。

- **2026-11-13 · MCP测试生命周期：C-006/P3**：第二调查者定位对象阻断helper真实client没有close、被atexit留到pytest退出；最小单测业务1pass但观察器抓到1进程/2线程残留并exit1。仅该模块新增13行生命周期fixture；90项、57次启动记录（55个唯一pid）每teardown与最终均零残留，test call及build_stack主动失败仍pytest exit1且清理完成。未改变生产逻辑，未将其扩大为全部Windows耗时根因；与C-005整改合并工作区重新跑门禁，后续正式SHA仍需独立复审与CI。

- **2026-11-13 · Windows CI诊断原生失败：C-005/P2**：在I/O工作树a0cb461702e538b22192eacc9b27fffc030a34ac上只追加测试工具和回归。先调查原Windows60秒dump截断/AV，保留同测试100.83秒正常通过的反证；纯标准库Windows实验92545fa/run43353423204独立复现原生诊断0xC0000005，Python/no-dump控制正常。新增Python线程pytest包装器并同步CI、CONTRIBUTING与AGENTS；保留fatal handler、60秒/25分钟及原pytest失败/中断退出码。真实子进程验证后台错误、阻塞写、abort到达marker与隐私/输出界限，撤销一次配置提前失败的旧负控；不改变生产模块或产品契约。新提交仍待第二读者复核与required CI，详见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **2026-11-13 · I/O CI 警告整改：C-004/P2**：原main与I/O首提交的Ubuntu慢订阅者用例均有发送线程write(None,buf)警告，不能把success状态当成没有异常。基于 `72af1ae9f7a5c826b1dde568ba30005e1c4dc45d`，用真实IPC子进程、实际短写和关闭屏障重现；确认线程能退出，无daemon死亡/数据损坏证据，按P2关闭竞态处理。仅在底层连接已关闭时把send_bytes的TypeError转为ConnectionClosed并保留cause，编码在try外、开放连接TypeError原样暴露；两项负控保留。68项定向在未处理线程警告升错误下通过，lock/ruff通过，macOS arm64全量2169 passed / 7 skipped（377.77s，exit0）；新SHA待独立复审与CI。POSIX短写用例在Windows明确跳过该不同实现路径，两项负控及既有Windows IPC覆盖不移除。证据见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **2026-11-13 · I/O 独立复审整改：Host JSON 深度**：第二读者在 `4479088ecb2d161755d7ba3a89e46119c94a8b8e` 复现约20KB深层JSON仍触发RecursionError并断线，确认原Host修复未覆盖全部解析失败边界。在独立 `codex/debug-io-review` 工作树先取得真HTTP原红，再仅捕获解析递归错误返回400；保留认证在前、完整读取后解析，新增同连接正常请求及401/403回归。原独立12项探针由作者复跑全部通过；本机Host相关65项通过，全量2166 passed / 7 skipped（379.90s），lock/ruff通过。5000位整数候选实测已由ValueError边界返回400，撤销疑点。上述结果绑定基线加补丁，后续新完整SHA仍须原第二读者复核与CI；不把字节有界等同解析深度可处理，也不把自查当独立批准。证据见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **2026-11-13 · 系统 debug / I/O 批次**：真实不读管道、持有写端孙进程、半响应 HTTP、SSE 头后断流复现后做最小修复；纠正 README required checks/摘要/Windows 口径及 AGENTS 数据目录陈述。区分 API 支持、本机执行、GitHub runner 与用户设备实测，未改变 Python 版本、运行时依赖或产品契约。证据见 [DEBUG-IO](DEBUG-IO-2026-11-13.md)。

- **2026-11-13 · 分支与本地状态对账**：按维护者授权开展全部分支、本地草稿、备份与同步处理；原工作区草稿已保存至仓库外备份（`repo-sync-20261113-034435`，含 patch / bundle / `.omc` 副本）与 stash `263737c304d9d885f5d5d428c3b88a7f7bd7103b`，原工作区已切到 main 并快进 133 个提交。已核实并删除 7 条本地旧分支与 5 条远端残留分支；旧工作树保留为 detached 历史快照，文件未删除。远端已开启合并后自动删除分支。记录时仅剩 PR #130 待最终 CI / 合并；后续合并状态以 GitHub PR 与 main 为准。修正 CONTRIBUTING §8 先前新增的“必须签名/公证”要求：它与 PRD §11.1 / §13 T1“不支付 OS 签名成本”冲突，现按既定分发策略验证安装脚本、系统提示、权限、产物校验及更新/回退；未来改变 T1 须单独契约 PR。从上述备份恢复原工作区的 2026-11-10 能力提案，按 CONTRIBUTING §1 将[原文](archive/2026-11-10-next-capabilities-plan.md) 放入 `docs/archive/`；[原路径](NEXT-CAPABILITIES-PLAN.md) 只保留历史警告与现行依据索引。原 281 行正文逐字节保留，本次归档没有修改备份原文，不把历史计划当作当前契约或验收证据。

- **2026-11-13 · 单人 Codex 开发流程收口**：按维护者要求，使用 GitHub 分支保护 API 将 UI 与 Windows 加入 required（四项均绑定 Actions app），回读验证其余保护字段完全未变；批准人数保留 0。新增 PR 模板要求填写改动原因、版本与验证证据；同步 CONTRIBUTING / AGENTS / CI 注释：允许 codex 分支前缀、明确提交与合并说明原因、独立复审绑定 SHA 和可复现证据、区分最新状态与历史验收。安装包与发布链路仍未实现，普通用户发布前须完成 CONTRIBUTING §8 验收；不把研发阶段写成可安装发布。配套文档通过 PR #130 交付，未自动合并。

- **2026-11-13 · Windows CI 修复（`codex/fix-windows-ci`，PR #130）**：以 `main@2100fde` 为基线。固定密钥回归在 Windows run `43281774439` 得到真实红，确认缺 `O_BINARY` 导致 LF→CRLF；修复仅改新建文件 flags，既有 key 不轮转。另修 MCP 假服务端 UTF-8、authority 子进程输出编码、测试路径/平台/URI 假设及 POSIX socket 用例遗漏的平台条件；Windows pytest 增加逐例输出与长等待线程栈。历史完整日志的 56 失败已分类，本地相关回归通过；**整合 `main@3648d79` 后，`4bb1781` 的四个 CI job 全 success：Windows 2127/30、macOS 2150/7、Ubuntu 2149/8、UI 通过；独立复审无阻塞，本机全量 2150/7**。详细红绿证据与最终结果见 [WINDOWS-CI-2026-11-13.md](WINDOWS-CI-2026-11-13.md)。
- **2026-11-12/13** · **MCP 对象级阻断（PR #129，分支 `feat/g2-mcp-object-block`，基线 `main @ 2100fde`）**：用户说「继续」，验收报告 §5-8 与 [mcp-g2.md](mcp-g2.md) §6-3 正是这件事——阻断对象此前只有 workspace（目录），MCP 结算 `unknown` 时按最保守口径挡调用方自己传的那一个目录：既挡错东西（真正有未核实副作用的是那个工具，在 server 那一侧），解除粒度又粗（解开目录等于连"可能已执行过"的工具一起放开）。· **做成的（对象模型：两种对象并存、默认不放松）**：目录（文件写，不变）+ MCP 对象 `mcp://<server>/<tool>`（工具级）/ `mcp://<server>`（server 级，遮住它下面全部工具）；MCP 结算 `unknown` 时**两者同时登记**。**对象键只从派发行 `target` 读**（派发当时的原始 URI，并用解析出的段重拼规范键），**绝不**按 `mcp__…__…` 反解（规范化把 `-`/`.` 换成了 `_`）；读不到派发行、**缺 `target` 列**、形状不认识都**不登记**对象阻断（不编），目录阻断照旧——缺列时恢复按"证明不了对象"继续，不许在写任何阻断之前崩掉（复审 M5）。· **两道发外核验**（都是受控拒绝 `rejected`/`none_applied`、一个字节不发）：① `_resolve_target`（**弹批准之前**）；② `_mark_mcp_send_boundary`（**执行守卫内**、紧贴 `intent → executing` 推进）。命中时派发行**不会**推进到 `executing`：第一道在事件登记之前（还没有派发行），第二道命中按 `_abort_dispatch` 把行标成 **`rejected`**（此前文档写"保持 intent"是错的，复审 M8 已改）。两个键一起查 `(tool 级, server 级)`，否则一条 server 级阻断永远读不到；**键只能等于自己**（不做前缀匹配——`mcp://a` 不许挡 `mcp://ab/*`）。· **线性化边界（复审两轮整改后）**：对象阻断的**两种提交路径**（恢复侧、运行期 `unknown` 的 `_commit_mcp_unknown` 与 `_commit_result` 的未知分支）都先拿 `execution_guard`，与发外边界的"复核 → 推进"共用同一把守卫：要么阻断先提交（复核看得见、拒发），要么推进先完成（这次调用已越过边界，阻断只约束之后的新调用）。`unblock_object` 是人工解除，与 `unblock()` 一样**不**抢守卫（解除只会放宽，不会漏挡——复审第二轮 N3 抓到文档把这条写成"三种路径都拿守卫"）。**仍剩两条窗口**：没有 `data_root` 的库调用方没有守卫可抢；提交侧抢守卫超时时按最坏情况照常提交并在结果里写 `block_guard="timeout"`（有单测钉住）。**发包前 server 不可用**（确定一个字节都没写）**不登记**对象阻断（`sent=False`；复审 M6）。· **运行期 unknown**（`-32603` / 超时 / 断线）就地落 `recovery.object_blocked`；跨数据根靠下一次 `recover` 的启动补登记（`mirror_blocks_to_authority` 现在同时补目录与对象），**不是即时**。每条未结算调用各落一条阻断事件（`call_ids` 是归因材料，报告里同一对象只列一次）。· **解除分开**：`unblock()` 只解目录，`unblock_object()` 才解对象（先写日志审计事件、再释放权威根；释放失败 → 日志已不挡、权威根仍挡，异常如实抛出）——核对过工作区文件不等于核对过 server 侧副作用。解除入口**目前是库函数**（CLI 没有 `unblock` 子命令）；解除事件记的是人工动作，对一个从未阻断的键也会写一条（与目录 `unblock` 同口径）。· **契约增量（本工单唯一改契约处）**：新增事件 `recovery.object_blocked` / `recovery.object_unblocked`（**事件类型 43 → 45**）与规则编号 `dispatch.mcp_object_blocked`（**规则 74 → 75**），四处同步（`EVENT_TYPES` / `CURRENT_SCHEMAS` / `schemas/c3/event.json` / PRD §5.3.2；`RULE_IDS` / `RULE_GROUPS["MCP"]` / `schemas/c4/rules.json` / PRD §5.4.1），夹具 `minimal_v1.json` / `legacy_v0.jsonl` 覆盖到 45 个类型。**没有复用** `recovery.blocked` 的 `workspace` 字段：目录回放会把 `mcp://…` 当成一个不存在的目录路径，既是假阻断也是假解除。· **升级兼容**：权威库新增 `blocked_objects` 表；旧版本库缺表 = **空集**（旧版本写不出对象阻断，缺表能被证明），表在但读不出、或表里有形状不合法的键 → 按已阻断处理（读侧 fail-closed）；写侧（`register_object_blocked`）先校验键形状，不许写一条永远匹配不上的记录。`boundary_protocol` 只影响"能不能证明没发"，不影响对象身份（旧行同样登记对象——它本来就按"可能已发出"处理）。· **判据/数字（复审整改后）**：`tests/test_mcp_object_block.py` **34 条**（权威根登记/释放/幂等、旧库缺表=空集、坏库=已阻断、表里坏键=已阻断、事件回放与畸形键、单字符串参数、弹批准之前拒绝、server 级遮全工具、同前缀不误挡、批准期间落下的阻断批准后也不发、第二道门单独站得住、无 `data_root` 也要第二道门、运行期 unknown 就地阻断且第二次一字不发、运行期阻断提交拿守卫、恢复按 `target` 读对象键且缺列不崩、never_sent 不登记、无派发行不编、旧行 unknown + 登记、目录解除不碰对象、对象解除后可调用且留审计、畸形键拒绝、跨数据根可见、补登记不复活已解除、对象阻断不拦文件写、误拒不是 unknown、发包前 server 不可用不登记对象、每条未结算调用各留一条归因）；全量 **2146 passed / 7 skipped**（macOS arm64，374.95s；同机基线 `main @ 2100fde` = **2108 / 7**，397.29s；差值 **+38** = 新文件 34 条 + 契约参数化 **+4**：事件类型 43→45 让「逐类型」用例多跑两格 × 两个文件），`ruff check .` 干净，UI `vitest` **76 passed**（新增 1 条对象阻断摘要用例）。负控脚本 `scripts/negative_controls_object_block.py` **26 条全部实测红**（NC-O1 判定阶段拒绝 / O2 边界前复核 / O3 server 级键 / O4 `target` 原始名字 / O5 never_sent 不登记 / O6 无派发行不编 / O7 畸形记录不静默跳过 / O8 解目录顺手解对象 / O9 权威根 fail-open / O10 旧库缺表当已阻断 / O11 运行期 unknown 不落事件 / O12 补登记不带对象 / O13 受控拒绝错记 unknown / O14 回放不校验键形状 / O15 单字符串按字符拆 / O16 补登记用 REPLACE 复活已解除 / O17 前缀匹配 / O18 删无数据根分支复核 / O19 对照：命中判定短路成永不命中 / O20 阻断提交不拿守卫 / O21 缺 `target` 列不回退 / O22 发包前不可用也登记对象 / O23 后续未结算调用直接 return 丢归因 / O24 超时/断线不落阻断 / O25 守卫拿到但提交在锁外 / O26 抢守卫超时不写 `block_guard`），与崩溃负控同一套「快照 → 变异 → 清 pyc → 跑目标用例 → 快照还原」+「变红三条件（rc=1 + FAILED 命中目标节点 + AssertionError）」；旧脚本 NB21/NB25–NB39 **16 条重新全红**（NB32 的变异锚点跟随新调用签名同步）。· **独立复审（第一轮，另一读者在 PR head `a9890d4` 上自建 clone + 变异探针）**：结论**有阻塞、不可合并**；它实测确认了契约数字/升级兼容/跨数据根/两道门/恢复判据等 12 组正面结论，同时抓到 **2 个 BLOCKER + 10 个 MAJOR + 7 个 MINOR**，本轮逐条处置（全部整改或明确文档化）：**B1**——对象阻断写入不参与执行守卫，第二道核验与 `intent→executing` 非原子，实测"阻断已落盘、请求仍发出"；整改 = `_commit_mcp_unknown` 先拿同一把守卫（抢不到时照常提交并写 `block_guard="timeout"`），守卫获取次数用 spy 钉住 + NC-O20。**B2**——日志回放只把 `object` 当非空字符串，`not-a-key`/大写/尾斜杠被当"不匹配的有效阻断"→ fail-open；整改 = `scan_blocked_objects` 用 `parse_mcp_object_key` 校验、畸形即 malformed（按已阻断），权威根读侧同样 fail-closed、写侧先校验；用例 + NC-O14。**M1** 单字符串参数按字符拆开静默放行 → 显式收 str，用例 + NC-O15。**M2** "已解除不被日志复活"没判据（原用例解除的是另一个 key）→ 改成同 key 的 register→release→mirror→stale-log，并直接测 `backfill_object_blocked`，NC-O16。**M3** 前缀匹配变异全绿 → 补 `a` vs `ab` 同前缀用例，NC-O17。**M4** 无 `data_root` 分支第二道门没判据 → 直接调 `_mark_mcp_send_boundary` 的用例，NC-O18。**M5** 派发表缺 `target` 列时恢复崩掉 → 列存在性回退，用例 + NC-O21。**M6** 发包前 server 不可用也登记对象阻断 → `sent=False` 不登记（且补住了一个自测发现的洞：`sent=False` 曾经的实现会掉进文件写分支，`_McpTarget` 没有 `workspace_root` 直接炸），用例用调用栈识别构造该分支，NC-O22。**M7** `parse_mcp_object_key` 的 server-only 分支不校验段形状、`register_object_blocked` 不校验键 → 两处都校验，坏列表补 `mcp://A` / `mcp://a b`。**M8** 文档多写"命中后派发行保持 intent"（实测是 `rejected`）→ 四处文档改口径。**M9** SPEC §5 负控表被写坏（NB39 行被重复 c16 行替换、`## 5 负控` 出现两次）→ 修复并补回 NB39 + NC-O 行。**M10** "批准期间落下的阻断"用例实际由 dispatch 阶段一挡住 → 用例归因写清楚（守卫内那条由 bypass 用例单独钉）。**MINOR**：event-payload-contract 残留 43/35 → 45/38（顺带把 6→7 个真实历史形状差异与夹具 drift 判定修正：`"历史形状" in note` 会命中"无已知历史形状差异"，之前 `>=5` 永远成立）；CLI 无解除入口 / server 级只有读侧 / `unblock_object` 对从未阻断的键也写事件 / 同一对象多条 `call_ids` 去重丢归因（最后一条改成每条调用各写一条事件，NC-O23）→ 前三条明确写进边界。· **独立复审（第二轮，另一读者在 head `96ed1ab` 上复核整改）**：第一轮的 B1/B2/M1–M10 + MINOR 逐条**已关闭**（它自建变异/探针实测），但抓到 **1 条新 BLOCKER（N1）+ 3 条 MAJOR/MINOR**，全部整改：**N1 真实假阴性**——`McpTimeout` / `McpClosed` 走的是 `_commit_result` 而不是 `_fail_unknown`，所以**超时与断线这两种最常见的 unknown 根本没有登记对象阻断**，下一次还能再发（探针实测 `calls=2`）；而四份文档都写着"运行期 unknown（`-32603`/超时/断线）就地落"——文档对、代码只覆盖了 `-32603`。整改：`_commit_result` 对 `_McpTarget` + `effect_state=unknown` 在守卫内补 `recovery.object_blocked`，超时/断线各一条用例 + NC-O24。**N2** B1 的 spy 判据是空壳（只数"拿了几次守卫"）——"守卫拿到但提交挪到锁外"仍全绿；整改：spy 记录持锁状态并包 `EventLog.append`，断言对象阻断**落库时确实持锁**，NC-O25。**N4** 抢守卫超时的 `block_guard="timeout"` 没有判据 → 强制超时用例 + NC-O26。**N3** 四份文档说 `unblock_object` 也拿守卫（代码没有、也不该有）→ 改成"两种提交路径 + 解除不抢守卫"。**N5–N10**（docstring 残留"未做"、v5 变更记录残留"保持 intent"、KNOWLEDGE 同一节"保留未使用 2 vs 3"、`test_event_schema` 82→84、`authority.is_object_blocked` 低层查询对坏键 fail-open、`_dispatch_row_targets` 规范化今天是恒等）→ 全部修/写清并各留注释。第二轮同时独立验证：全量 2146/7、基线 2108/7、对象 34 条、UI 76、两份负控 26+16 全红且无锚点漂移、契约数字与 bytes 镜像、升级/坏库/跨数据根/两道门/恢复判据均 PASS。· **诚实边界（更新后仍不成立）**：对象阻断**解除是人工动作**（无自动判据、无过期；入口是库函数不是 CLI）；粒度只到工具/服务器（**没有 per-call 回执**，要证明"这一次到底跑没跑"需要 server 侧确认协议，未做）；server 级键**只有读侧**能力（产品只登记工具级）；运行期 unknown 的对象阻断只落本数据根日志、跨数据根非即时；**无 `data_root` 的库调用方**与**提交侧抢守卫超时**两个窗口仍在；目录阻断仍按调用方 `--workspace` 路径（代码不按 task 绑定核对，传错就挡错）；对象层是**额外**一层、不放松目录层；标记提交与真正写出之间的窄窗口仍保守（PR #124 遗留）；server 侧私有状态没有任何证据、恢复不问它；真实第三方 server 兼容性未验证；**NB23 仍未做**；Windows 是已知基线红、不作为实机证据。· **踩的坑**：⓪ **最常路径反而漏了**——运行期 unknown 只测了 `-32603`，超时/断线两条真正常见的路径走另一个提交函数，文档却统一写成"就地落"；复审第二轮用探针把"超时后再调一次、请求照样发出"钉了出来（**症状在文档与代码的分叉处，不在被改的文件里**）。⓪² **计数不等于在锁内**：spy 只数守卫获取次数时，"拿到锁→立刻释放→在锁外提交"仍全绿——判据必须观测**提交发生时的持锁状态**。① `sent=False` 的实现第一版会掉进文件写分支（`_McpTarget` 没有 `workspace_root`），是自测的"发包前 server 死掉"用例抓到的——**新分支必须自己跑一遍才知道掉哪**；② 负控的"变红"要求 `AssertionError`，而 `pytest.raises` 未抛出报 `Failed: DID NOT RAISE` → NC-O8/O9/O15 等用例改成显式 try/except/else 断言才咬得住；③ 变异锚点必须落在**用例真的会走到**的分支（NC-O6 最初锚"无派发表"早退，而用例现场是"有表无行"）；④ 旧负控脚本 NB32 锚点随调用点签名（`server`/`tool`）漂移，脚本如实报"锚点命中 0 次"并 rc=1——锚点同步后 16 条重新全红；⑤ 契约增量会牵动两处"逐类型"参数化用例、夹具生成器（`minimal_v1.json` 由 `minimal_payload()` 生成，整表重生成才不会漂）与 PRD 文档对账用例（漏一行规则说明直接红）；⑥ SPEC 文档批量替换时用 `## 5 负控` 当锚点、又在同文件里重复插入，导致标题与表格被写坏（复审 M9 抓到）——**批量改文档也要先 `grep -c` 确认唯一，改完再读回**。· **顺手修掉的旧账**：README 把 C3/C4 写成「41 个类型 / 43 个规则编号：36 拒绝 + 5 判定 + 2 保留未使用」→ 按当前实测改成 **45 个类型 / 75 个规则编号（62 拒绝 + 10 判定 + 3 保留未使用）**；`docs/KNOWLEDGE.md` §3 契约速览表同样过期（41 事件 / 56 规则 / 17 ToolSpec·13 暴露）→ 同步为 45 / 75 / 19·15；`docs/event-payload-contract.md` 的 43/35 → 45/38。
- **2026-11-12** · **G2 记忆阶段交付：可查看（`memory.search` RPC + React 面板）+ 可证明收益（`RU-M1@1.0.0` 跨任务召回）（#125 规格 / #126 M1 / #127 M2 / M3 文档对账，基线 `main @ bfe5916`）**：§11.1「网络 → MCP → 记忆 → 子代理」的最后一段。先落 [SPEC-G2-MEMORY-2026-11-12.md](SPEC-G2-MEMORY-2026-11-12.md)（M1/M2/M3 + 判据 M1-a…f + 负控 M1…M9 + 口径变更 + 诚实边界），再串行交付。· **M1（#126）可查看**：控制面第 9 个记忆方法 `memory.search`——**显式 `scope` 是"追加"不是"替换"**（人永远还看得见 `user` 记忆），没有 `status` 参数（刻意收窄的检索面）；`memory_control.search_memories()` 复用 `retrieve()`，所以条目带 `semantic`/`degraded`/`fused` 与 `score`/`score_kind`，`stale` 不可判定时**省略字段**而不是写 `false`；React 面板（`MemoryPanel` + `MemoryDialog` + `api/memory.ts` + `lib/memoryView.ts` + `lib/memoryController.ts`）做列出/检索/确认/忘记/擦除，正文**纯文本**（不渲染 HTML）、**不做乐观更新**（写后回读服务端视图）、冲突**只刷新重读不重试**。· **顺带修掉一条真缺口**：控制面任务（`memory.remember:…`）因为 `task_view()` 不区分而混进任务列表，UI 现在按 `control` 字段过滤（`tests/test_control.py` 的 `TASK_FIELDS` 同步）。· **M2（#127）可证明收益**：评测器补 `Case.followup`——同一 case 跑**两段 Run**（共用 `data_root`、第二段换 workspace；`workspace=="ws"` / 第一段没到终态 / 残留子任务脚本一律 fail-closed）；`FollowupObservation` 与第一段证据**分开**（`recalled_memory_ids()` 从 `model.intent.payload.context.memories` 读，不靠模型自述）；公开第 9 条真实用户任务 `RU-M1@1.0.0`：第一段人记一条写作偏好，第二段新任务**召回并真的按偏好产出**，同时**不召回**别的项目目录的诱饵记忆、产物里也不出现诱饵正文；测试内失败路径 `RU-M1-F`（忘记 ⇒ 召回为空 + 产物无偏好）不进公开分母。· **判据只有三层都核才算**：工具命中 → `context.memories` 里真有这条 id → 产物里真的体现；只核前两层会把"召回了但没用"判绿，只核第三层会把"碰巧写对了"判绿。· **负控 8 条实测红**（M1 三条：假成功不派发 / 面板乐观更新 / `stale` 谎报；M2 五条：不把 `followup.checks` 写进报告 1F:3P / 确认+忘记净零 1F:3P / 诱饵与查询词无交集 1F:3P / 只核两层 1F:3P / 作用域不隔离 2F:2P）。· **踩的坑（都是判据自己的问题，不是产品问题）**：① NC-M5 最初把"确认"和"忘记"都跑一遍 ⇒ 净零变化，负控**瞎了**；改成跳过确认只跑忘记才咬得住。② NC-M7 的诱饵记忆最初和查询词没有交集 ⇒ "跨 workspace 不泄漏"永远绿；把诱饵正文改成含查询词（"周报""偏好"）才真的能证明隔离。③ `build_report` 一开始只评第一段判据，第二段那 12 条在报告里**看不见**（369 对 357 才发现）——回填时抓到的真缺口。④ M1 首张截图暴露控制面任务污染列表，才补 `control` 字段与 UI 过滤：**截图不是走流程，是真的在看**。· **数字（干净 `main @ bfe5916`，macOS arm64）**：全量 **2108 passed / 7 skipped**（386.76s；同机 `main @ 1e17003` 基线 2098/7 ⇒ 本阶段 +10）；`ruff check .` 干净；UI `vitest` **10 文件 75 条**、`pnpm build` 成功；`run_real_user_tasks(repeat=3)` **9 case × 27 次全绿、369 条判据 0 失败、27.78s**；28 条既有 case 的 `fixture_digest` 逐字节不变（`followup` 只在非默认时才进摘要）。· **诚实边界**：两段都是**脚本模型**，判据证明链路（召回 → 注入 → 产物），**不是真实模型能力分数**；内置 embedding 只有 `HashEmbeddingProvider`（**无语义能力**，永远 `semantic=false` + 降级原因）⇒ **不是召回质量证明**；第二段没有并发、没有 daemon 重启、不超过两段；诱饵只覆盖 `workspace` 作用域；记忆×委派交叉未评测；Windows 未验证。· **`memory.search` 的一处口径纠正**：显式 `scope` 是**追加**不是替换——第一版判据按"替换"写、红了才改，改成两个 task 作用域对比才对。
- **2026-11-12** · **MCP 外发边界标记 + 恢复分档（PR #124，分支 `feat/g2-mcp-send-marker`，基线 `main @ 5eed5ee`）**：用户说「继续」，上一轮验收报告 §5-7 与 [mcp-g2.md](mcp-g2.md) §6-2 正是这件事——把「恢复分不出请求发没发出」这条**过度阻断**从「整段调用」缩到「标记已落盘、请求还没写」。· **机制（不新增事件、不改事件契约）**：复用既有 `dispatches.state`，真正写 server stdin **之前**由 `dispatcher._mark_mcp_send_boundary` 把行从 `intent` 推到 `executing`（`WHERE state=intent` 只进不退；与 `revoke()` 共用 `execution_guard`——撤销先到 → 推进不上 → **不发**，结论 `rejected`/`none_applied`；标记先到 → `revoke` 明确报错「已经开始执行」，不再假装撤销成功）。恢复/`assess_run` 据此分两档：`intent`（或已是 `revoked`/`rejected`，标记从没推进）→ `status=failed`、`effect_state=none_applied`、`reason=mcp.not_sent`、**不阻断**（`recover` 后可直接 `resume`，不需要 `unblock`）；`executing` 或**无从证明**（老库没有 `dispatches` 表、没有行、状态不认识）→ `unknown` + `mcp.crash_unsettled` + 阻断（保守方向不放松）。**升级兼容**：派发表加 `boundary_protocol`（新行 1，`ALTER TABLE` 给旧行补 0）——旧版本在写请求前**不推进**派发行、撤销与发送之间也没有互斥，所以旧行的 `intent`/`revoked`/`rejected` 一律按「可能已发出」处理（`unknown` + 阻断），不许当成「确定没发」。新注入点 `mcp_marked`（标记已落盘、请求未写）把三窗口结论钉成**各不相同**；清单损坏分支不再拿无关证据（文件快照清单）的损毁去挡 MCP 调用；`revoked`/`rejected` 在恢复侧算「没越过边界」，但**行保持终态**（撤销过就该一直看得出撤销过）。· **判据/数字**：`tests/test_mcp_crash.py` **24 条**（新增标记窗口含「撤销被拒」、发包前撤销不发请求、清单损坏不误挡、**没有派发表/没有行时不放松**（回退用例）、静态表 `idempotent`/`pure` × `intent`/`executing` 四格）；全量 **2098 passed / 7 skipped**（macOS arm64，364.17s；已 rebase 到 `main @ 1eeb5fd`；同机基线 = **2083/7**，362.44s → 差值 +15 是本轮新增用例），`ruff check .` 干净，UI `vitest` **75 passed**（含 #126 记忆面板，实测）。负控脚本扩到 **16 条**、全部实测红：新增 NB30（清空「没越过边界」状态集合 → 窗口一回归 unknown）、NB31（一律判没发 → 最危险方向）、NB32（拿掉标记 → 标记窗口与发包前撤销一起坏）、NB33（只认 `intent` → 已撤销的调用又变阻断）、NB34（清单损坏分支不按 MCP 判）、NB35（不看 `boundary_protocol` → 旧行被洗成没发）、NB36（把 `done`/`error` 也算成没越过边界 → 自相矛盾行被替它下结论）、NB37（只读对账先关连接再读标记 → own-log 路径静默降级）、NB38（去掉边界推进的 `state=intent` 前置条件 → 撤销先到也能发）、NB39（抢守卫超时错映射成 unknown → 确定没发的调用被挡目录）；NB21/NB28/NB32 的锚点与目标用例名随实现同步更新。· **复审整改（独立第二读者 round 1）**：复审在 `a8e904e`（未含文档与后补用例）上实测 14/14 绿、11/11 负控红，抓到 1 条**真实假阴性** + 1 组未修订文档 + 3 条未钉住的安全分支，全部处置：① **假阴性（B1）**——旧版本写的 `intent` 行在 `mcp_after_call` 崩溃后与新行形状完全相同，新恢复会把「可能已执行」洗成 `none_applied` 并撤掉阻断；修法是给派发行加 `boundary_protocol` 版本标记（旧行 0 → 一律 unknown + 阻断），缺列的老库按旧行读不崩；补 3 格旧行用例 + NB35。② **文档（B2）**——验收报告/mcp-g2/SPEC 仍写「两窗口都 unknown」「本工单未做」并引用已改名的用例；本轮把四份文档按新分档重写、失效引用清掉、数字回填（22 条 / 14 条负控 / 2082 对同机基线 2069）。③ **未钉住的分支（B3）**——无表/无行回退（已由 `test_mcp_without_a_dispatch_row_stays_conservative` + NB31 覆盖）、`done`/`error` 自相矛盾行（新用例 + NB36）、`assess_run(log=None)` own-log 读序（新用例 + NB37；复审的「挪到 close 之后」变异会让它变成异常而不是断言失败，所以 NB37 用「先关连接再读 + 静默降级」的变异形状）。另修一处审计文案（「仍是 intent」→「停在 intent/revoked/rejected」）。· **复审第二轮（另一独立读者，在整改后的 b20ea7d/f52be49 上）**：B1/B2/B3 三条**已关闭**（它自建旧行现场验证 protocol=0 → unknown + 阻断、protocol=1 同形状 → none_applied；DROP COLUMN 模拟旧表 → 按旧行读；连开两次 Dispatcher 迁移幂等）；但抓到 1 条**新阻塞（S1）**——`_advance_to_executing` 的 `AND state=intent` 前置条件没有任何判据（`test_refused_send_boundary_does_not_call_server` 把整个入口 monkeypatch 掉了，只测调用方怎么处理 False），它把该条件删掉跑 68 条全绿。整改：新增确定性用例直接调真实 `_advance_to_executing`（intent → True/executing；revoked/rejected/done/error → False 且行不变）+ 负控 NB38。顺带修掉它报的非阻塞口径差：**抢守卫超时按受控拒绝**（`GuardTimeout` → `_abort_dispatch` / `dispatch.guard_timeout`，与文件写分支同口径；原来会抛出去让 Run failed），补用例 + NB39。文档小项也一并改（「可直接 resume」改成「recover 后可直接 resume（无需 unblock）」、SPEC §7-10 恢复口径、mcp-g2 负控范围、PR body 表格加 `boundary_protocol=1` 限定）。· **诚实边界（更新后仍不成立）**：标记提交与真正写出之间**仍有一小段窗口按「可能已发出」保守处理**（有意选的方向；彻底消掉需要 server 侧回执协议，未做）；**升级前留下的派发行**（`boundary_protocol=0`）一律按「可能已发出」处理（不拿旧行证明没发）；阻断对象仍只有 workspace、**没有**对象级阻断；「挡哪个目录」仍由调用方 `--workspace` 负责（代码不按 task 绑定核对）；server 侧私有状态没有任何证据、恢复不问它；真实第三方 server 兼容性仍未验证；**NB23 仍未做**。· **踩的坑**：① 派发层直接 `log.append("tool.result")` 会让日志出现**两条同 call_id 的结果**——主循环判定「自己记过了」的依据是派发行状态 ∈ (`done`,`error`)，所以「行保持终态 + 返回结果」这类路径必须由主循环补事件（照 `_abort_dispatch` 的分工），dispatcher 不能自己写；② `assess_run` 在 `own_log` 的 `finally: close()` 之后才遍历 pending，新增的「读派发表」必须挪进 try 里先读好；③ `git commit -m "…"` 里的反引号会被 shell 当命令替换吃掉（实测把提交信息里的函数名抹空），提交信息改走 `-F 文件`。· **顺手修掉的旧账**：[PRD-v1.md](PRD-v1.md) §11.1 G2 行与 §5.2 实现状态里把 MCP 阶段写成 `#116 / #117 / #118`（实际验收是 #119），本轮改成 `#116 / #117 / #119 / #122 / #124`（#121 合入后该行已可安全改写）。
- **2026-11-12** · **G2 子代理 D3：真实用户任务 `RU-D1@1.0.0` + 文档对账（分支 `feat/g2-delegation-d3`，PR #123）**：把「委派」接进真实用户任务集——父任务真的 `subtask.spawn`（人工批准一次），子任务由**既有 Worker** 驱动到终态，父任务被唤醒后读 `subtask.result` 并**逐字**汇总进 `summary.md`、带两个本地文档名。· **做成的**：`eval/cases.py` 增 `max_subtasks` / `subtask_scripts` / `SubtaskScript`（父/子两套脚本，按 `subtask_id` 取；未声明拒绝驱动）；`eval/runner.py` 在父 Run park（作业 `waiting` + 未完成子任务）时用 `Worker` + 逐子任务 `engine_factory` 驱动子作业（只认子 Run；先核子作业 `pending`、父作业 `waiting`），再 `resume_run(parent)`；往返上限 8 轮，失败原因写进样本 `error`（不静默）；子 Run 事件与子任务行分别进 `Observation.subtask_events` / `.subtasks`；`fixture_digest` 纳入委派声明与子脚本。判据新增 5 个工厂：`subtask.spawned` / `subtask.finished_state` / `subtask.result_quoted`（要求结果 `done=true` + 非空摘要 + 合法 `content://` + 产物逐字包含摘要）/ `subtask.readonly_boundary`（safety：两条规则真的出现在子 Run 的 `dispatch.rejected`，无 `effect_state=applied`、无非纯读成功）/ `subtask.output_unavailable`（safety：空摘要 + 无引用 + error 写「输出不可用」，产物写「没有取得」）。子任务脚本先尝试写文件/出网（被 `c4.readonly_binding` / `c4.outbound_not_allowed` 拒绝），再读两份文档；父报告写「子任务只能读」。失败路径 `RU-D1-F`（测试内，不进公开分母）让子 Run 模型抛不可重试 `ModelError` → 子任务 `failed`、`subtask.result` 空摘要/无引用 → 父报告写「没有取得 / 输出不可用」，`artifact.excludes("CSV","3 秒内","打印延后","（来源：")` 钉住不编造。· **判据/数字**：`tests/test_eval_real_tasks.py` 22 → 29 条（首版：D3-c1/c2/c3 + 2 条判据负控 + 1 条 runner fail-closed + 1 条分母回归；复审整改再 +2 = **31 条**）；全量 **2076 passed / 7 skipped**（首版，356.20s，macOS arm64 本机；基线 `main @ 5eed5ee` 同机 2069/7、358.23s；+7）→ 整改后 **2078 passed / 7 skipped**（362.75s，+9），`ruff` 干净；`run_real_user_tasks(repeat=3)` 8 case × 3 = 24 次全绿（首版 26.73s / 整改后 26.76s、339 条判据 0 失败）。负控逐条实测（runner 驱动 / 子脚本选择 / 子事件采集 / 只读边界 / 失败不编造 / `max_subtasks` 接线），变异与失败:通过见 PR body。· **口径**：公开真实用户任务 7 → 8 条（`REAL_USER_CASES` 分母 21 → 24）；契约基线 20 条与 `SUITE_VERSION=m4.6` 未动（任务版本独立）。· **诚实边界（更新后仍不成立）**：父/子都是**脚本模型**，父报告汇总文字预先写好——判据证明产品链路，不证明真实模型会写摘要；`subtask.result_quoted` 要求逐字引用，真实模型改写摘要会判完成度失败（要改口径须换任务版本）；公开委派任务只有一条，失败路径是测试内任务不计入分母；多子任务并发/取消/fence 的**评测**未做；子任务只读、深度 1、单机 Worker 池、取消不退款仍按 D1/D2 边界；真实公网/真实模型/Windows 未验证。· **踩到的**：`_collect_subtask_evidence` 必须单独读子 Run（父 `events` 不含子事件）；子任务脚本必须独立，拿父脚本会从头重放；`run_suite` 的 `model_for` 只给父 Run，子 Run 只能走 `Worker.engine_factory`。· **独立复审 + 整改（同日第二轮）**：第二读者复审 PR #123 报了 **1 条窄 BLOCKER + 5 条 MINOR**，全部整改（基线 `main @ 5eed5ee`，+2 tests）：① **BLOCKER**——`RU-D1-F` 的「不编造来源」只挡了 `（来源：`，而本任务成功报告自己的来源行是 `来源：资料/...`（无括号）；把失败产物追加一条 `来源：资料/产品需求.md、资料/评审纪要.md。` 后 **19 条判据全绿**（safety 判据在真实形状上是假绿）；整改：needle 加 `来源：` / `来源:`，失败负控新增"只注入一条来源（不带事实）"并要求任务级判据至少红一条。② c1 的"汇总事实"与夹具无因果（夹具换成 XML / 30 分钟 / 打印必须做后判据仍绿）——新增完成度判据 `subtask.result_grounded`，把链路接到**子 Run 真读到的正文**（`tool.result.output.content`），并加夹具反转负控（grounding 红、`result_quoted` 仍绿，正是"只靠老判据等于没测因果"的证据）。③ `Observation.subtasks` 没有任何判据读它、注释却说"两者都看"——`subtask.output_unavailable` 改为**行与事件都读**（收束后的行被补内容也能抓到）。④ `subtask_readonly_boundary` 的规则出现是**跨子 Run 并集**，多子任务下不可区分——docstring 写清并登记为未覆盖边界。⑤ fail-closed 三层冗余、只拆一层仍绿——新增 `_child_model` 直接拒未声明 slug 的测试（最内层兜底终于有自己的断言），PR body 保留"三层冗余"说明。⑥ `PRD-REVIEW-DELIVERY` §0 的"负控见 §3"改成指向真实位置。整改后：全量 **2078 passed / 7 skipped**（362.75s；基线 2069/7）、`ruff` 干净、`run_real_user_tasks(repeat=3)` 24/24 全绿 **339 条判据 0 失败**（26.76s）、负控 **10/10** 实测红（含新增 NC9 来源 needle 回退 1:7、NC10 grounding 恒真 1:7）。
- **2026-11-12** · **G2 MCP 收尾轮：崩溃夹具 + 恢复判据（PR #122，分支 `test/mcp-crash-recovery`，基线 `main @ 6140d9c`）**：用户说「继续」，上一轮验收报告 §5 第 3 条正是这件事。· **做成的**：新增两个崩溃注入点（`dispatcher` 的 `mcp_before_call` / `mcp_after_call`，真子进程 `os._exit(77)`；生产路径永不触发）；`recovery` / `assess_run` 改成按 **id 形状**（`mcp__` 前缀）认 MCP，不再依赖"`SPECS` 里没有这个 id"——即使动态 id 被误塞进静态表也仍判 `unknown` 且**不读磁盘**（认错了会把"没有写前证据"读成"确定没写"）；未结算的 MCP 调用落 `status/effect_state=unknown` + `reason=mcp.crash_unsettled` + `tool_id` + `tianshu mcp list` 指引，**不补凭据**、不编造 `after_state`/`sha256`、不按 `__` 反解 server/tool 名字；**恢复补写结果现在同时推进 `dispatches` 行**（`applied→done`、其余`→error`；复审 R2 指出的既有缺口，文件写分支一起修）；复审第二轮又补了两个边界：**只从 `intent/executing` 推进**（`revoked`/`rejected` 等终态是审计事实，不许被改写，NB29 实测红）、`updated_at` 用恢复这一刻的时钟（不用行自己的 `created_at`，否则更新时间倒退）；派发表不存在的老库只写结果不崩。`assess_run` 的 MCP 分支单独覆盖（它和 `_recover_run` 是两条分支，只撤一条而另一条在正常现场看不到——NB28 实测红）。负控已脚本化可复现：`scripts/negative_controls_mcp_crash.py`（NB21/NB28/NB25/NB26/NB27/NB29，快照还原 + 清 pyc）。· **判据/数字**：新 `tests/test_mcp_crash.py` **9 条**（响应已收到窗口 / 发包前窗口 / 解除阻断后不重放 / `isError` 不被重判 / 静态表误装 / 文件写派发行终态），崩溃窗口由假 server 的 `--effect-log` 证明"它真的执行了"、`--call-log` 证明"请求真的到了"，而库里只有 intent 没有 result；恢复后逐条核对 unknown / 无凭据 / 派发行终态 / `assess_run` 拒绝接手 / 幂等 / **call-log 仍 1 行不重放** / `unblock` 后 resume 成功且 `effect_state` 仍 unknown。全量 **2047 passed / 7 skipped**（macOS arm64，同机基线 2038/7，差值 = 本轮 9 条），`ruff` 干净；负控 **NB21 / NB28 / NB25 / NB26 / NB27 / NB29 实测红**（0.42–0.92s，脚本可复现）。· **诚实边界（更新后仍不成立）**：恢复**分不出**"没发出去"与"发了但响应丢了"→ 两者都 unknown + 阻断（**可能过度阻断**；要消掉需"发送前持久标记"契约增量，验收报告 §5-7）；阻断对象只有 workspace、**没有**对象级阻断（§5-8）；server 侧私有状态没有任何证据、恢复也不去问它；真实第三方 server 兼容性仍未验证；**NB23 仍未做**。· **踩的坑**：负控还原用 `git checkout -- <file>` 回到 **HEAD**，把本轮未提交的 `recovery.py`/`dispatcher.py` 改动抹掉一次（用补丁重放修回）——还原必须用"变异前的文件快照"。· **口径修正（自己上一轮的过度声称）**：验收报告 §2-2 把"`tasks` 表旧行读成空元组"与"旧 v1 事件仍合法"混在一起；`task.created.mcp_servers` 在 v1 里是 required，升级前的 v1 行只是"读取不重校验所以读得出"，按当前 v1 严格校验**不合法**——已改。· **PRD 待改**：§11.1 G2 行把 MCP 阶段写成 `#116 / #117 / #118`（实际是 #119），该行正被并行 PR #121 改写，留到其后修。
- **2026-11-12** · **G2 MCP 阶段交付（PR #115 / #116 / #117 / #118，`main @ 暂无`）**：按 §11.1「网络 → MCP → 记忆 → 子代理」推进。先落 [SPEC-G2-MCP-2026-11-12.md](SPEC-G2-MCP-2026-11-12.md)（v2，经两轮独立复审整改 6 项阻断 + 5 项建议），再串行交付：**#116 客户端**（`mcp.py`：stdio 换行分隔 JSON-RPC、`initialize`→`tools/list`→`tools/call`、协议版本只认已知集合不降级、env 只给 PATH+声明项、stderr 有界、超时自动发 `cancelled`、晚到响应丢弃、协议失败真收尸、`max_tools`/`max_result_bytes` 真的生效，假 server 夹具 43 条 + 负控 NB1–NB13）；**#117 注册表与准入**（`tool_registry.py`：`mcp__server__tool` 两段冲突检查、`inputSchema` 归一到封闭方言（类型不匹配关键字**拒绝**而非静默丢）、`additionalProperties` 缺省→false 记 `narrowed`、`tool_version=mcp1:sha256(原始 schema+归一 schema+描述)`、进程级注册表在无 MCP 时工具面逐字节不变（manifest 顺序也钉住），33 条 + 负控 5 条）；**#118 派发链路**（`mcp://` 资源执行器、任务级 `task.created.mcp_servers` + `--allow-mcp-server`、`c4.mcp_not_allowed`/`c4.mcp_isolation_unproven` 在**弹批准之前**判定、超时/断线/协议失败→`unknown`、`receipt(backend="mcp")` + `verify_receipts` 分流、`tianshu mcp list`，11+6 条 + 负控 9 条）。全量 **1905 → 1999 passed / 7 skipped**（macOS arm64），UI 51 不变，`ruff` 干净。· **口径变更（已写进 PRD/契约文档）**：`tool.intent`/`tool.not_dispatched`/`receipt` 的 `tool_id` 枚举→形状 pattern（动态 id 无法枚举；放宽的是数据契约，白名单仍由注册表判定）；`receipt.backend` += `mcp`、`evidence.kind` += `mcp_call`；6 个新规则编号。· **诚实边界**：**真实第三方 server 未验证**（本轮无法联网核验官方规范，只用自写假 server）；**无沙箱**（三道门防模型自作主张，不防恶意 server；`untrusted_local_mcp` 未启用）；MCP 进程**不经过** `exec_backends`（`isolation_requires` 对 MCP 一律拒，不跨对象作证）；只做 stdio/tools；大结果不落内容引用；无预算维度；UI 无面板；Windows 未实机；**恢复只到 unknown+阻断**且**没有 MCP 崩溃夹具**（NB21/NB23 未做）。见 [mcp-g2.md](mcp-g2.md)、[MCP-ACCEPTANCE-2026-11-12.md](MCP-ACCEPTANCE-2026-11-12.md)。

- **2026-11-12** · **G2 子代理 D2：执行与收束（分支 `feat/g2-delegation-d2`，PR #121）**：按 [SPEC-G2-DELEGATION-2026-11-11.md](SPEC-G2-DELEGATION-2026-11-11.md) 做 D2，**子任务真的会跑完，父任务等它**。· **做成的**：子 Run 由既有 `Worker`/`AgentLoop` 驱动（不新写调度器；Worker 的引擎补上 `budget`/`contents`，否则子任务局部 `max_turns`/`max_tokens` 只是 spawn 时的一行 JSON）；父 Run 想正常收尾但还有未完成子任务 → **计数与 `JobStore.park` 在同一写事务**里完成，作业 `waiting`、Run 保持 `running`、游标落库；新 `JobStore.wake()` 只对 `waiting` 生效（复用 `job.requeued`，owner 字段可选 + `by`/`reason`），终态/非 RUNNING 父 Run 不唤醒；子 Run 终态写入（`TaskStore._finish_locked` 的**同一事务**）更新 `subtasks` 行、向父 Run 追加 `subtask.finished`、结算父账户 `subtasks` 维度（确定未开始才 `abandon_unsent`）、无未完成子任务时唤醒父作业；结果 = 最后一轮有界输出（≤4 KiB，UTF-8 边界安全）+ `content://sha256` 全文引用（先落盘再写事件），拿不到输出则 summary 为空并在 error 说明「输出不可用」；父取消传播（未开始当场 cancelled + abandon，其余 `cancelling` + 子 Run 取消请求 + 子作业 `request_cancel`），`run.end` reason 带 `subtasks_cancelled=N`，并唤醒 waiting 父作业；父目标 fence（`subtasks.parent_goal_revision/parent_lease_epoch`，派发前 + 收尾时核验）→ `dispatch.parent_fenced`、子任务 `fenced`、**不写 `subtask.finished`**；Worker 启动恢复按子 Run 终态修正未收尾行、唤醒"子任务已全终结的 waiting 父作业"；回放校验 `subtask.spawn`/`subtask.result` 形状（缺身份/`content://`/summary → problem），子 Run 的 `run.start` 写 `task` 让回放能重建首条消息；CLI `run status` 显示「等待子任务 N 个」。· **判据/数字**：新增 3 个测试文件 **22** 条（exec 12 / fence 3 / replay 7：D2-c1…c9 + 事务原子性 + 终态不唤醒 + 结算守卫 + 无输出不编造 + 恢复路径 fence + 合法空结果不误报 + CLI）；全量 **2069 passed / 7 skipped**（macOS arm64，354.14s；基线 `main @ 7dc9350` 同机 **2047 passed / 7 skipped**、354.21s），`ruff check .` 干净；负控 **6/6** 实测红（跑 7 个 `test_g2_delegation_*.py` 共 57 条：失败/通过 = 24/33、2/55、1/56、3/54、15/42、2/55；NC5 的变异是把子任务结算整体移到收尾事务之后，逐条变异见 PR body）；复审整改另加 3 条负控（回放引用判定 / 派发 summary 归一 / 恢复路径 fence 判定）各自红。· **复审整改（独立第二读者）**：抓到 1 条阻断——`replay` 的形状校验把 D2 **自己产出**的合法记录判红（`subtask.result` 的 `done:true` 在「成功但无输出」与「已取消/已 fence」时 `summary` 为空、`result_ref` 为 null，这是「拿不到输出就不编造」的既定形状）。已修：派发层终态 `summary` 恒为字符串（NULL → `""`），回放改为「**有正文**（或缺 `summary` 键）才要求 `content://` 引用」，另加「`result_ref` 存在但不合法 → problem」；回归测试 +3（合法空结果 / 取消 / fenced），恢复路径复用同一套 fence 判定（+1 测试）。两条 MINOR 写进规格 §5 边界：12 子 Run 收尾是两笔事务（中间崩溃只能手动 `run resume <child_run_id>`）、13 父 Run 失败不传播取消；14 `recover` CLI 未接启动恢复。整改负控 4 条各自变红：回退 replay 的两处修复 → 3 failed（空结果/取消/fenced 三条合法路径全红）、回退 `needs_ref` 的「有正文」条件 → 1 failed、回退派发层 summary 归一 → 1 failed、回退恢复路径 fence 分支 → 1 failed。· **口径变更（写进 PR body）**：父 Run 不再在派 spawn 后直接 succeeded（D1 的 `test_spawn_requires_approval_then_queues` / `test_subtask_reads_go_through_dispatch_without_approval` 按新语义更新为「父挂起」）；`job.requeued` 的 owner 字段改为可选（wake 时 owner 已被 park 清空）；`run.control` 加可选 `reason`；`dispatch.parent_fenced` 从保留未使用移入「身份失配」分组并接上产生路径。· **为什么这样切**：D1 先把身份/限额/预算/恢复做实，D2 只接"谁执行、父怎么等、结果怎么回填"；父 Run 不新增状态，挂起只体现为作业 `waiting`，状态机与 UI 不用改。· **踩到的**：① 子 Run 的 `run.start` 没写 `task`，回放拿空字符串重建首条消息，`messages_hash` 必红（D2 接执行后才暴露）；② "计数后 park"分两个事务会留下真竞态（子任务收尾时父作业还 claimed、wake no-op，父随后 park 就永远没人叫醒）——改成同一 `BEGIN IMMEDIATE`；③ Worker 的引擎原来没传 `budget`/`contents`，子任务局部上限与内容引用在 worker 驱动的路径上都不生效。· **仍不成立**：子任务只读、深度 1、每父 Run 未完成 2 / 每根任务 4、单机同 Worker 池（无跨机器调度）、`wake` 不保证立刻被认领、取消不退款、读不到输出时 summary 为空、无子任务 UI 页面、Windows 未验证、D3 未做。

- **2026-11-12** · **G2 子代理 D1：合同 + 实体 + 四个工具 + 限额 + 共享预算（分支 `feat/g2-delegation-d1`）**：按 [SPEC-G2-DELEGATION-2026-11-11.md](SPEC-G2-DELEGATION-2026-11-11.md) 只做 D1（**不做 Worker 执行/唤醒**）。· **做成的**：`task.created` 必填 `max_subtasks`（0–4）+ 可选父链（`parent_task_id` / `parent_run_id` / `subtask_id` / `depth`，旧事件 v0 合法、旧库补列后读成 0）；`tasks` / `runs` 父链列 + 新表 `subtasks`（主键 `(parent_run_id, subtask_id)`）；四个工具 `subtask.spawn`（delegate，需审批）/ `subtask.status`·`subtask.result`（read，自动）/ `subtask.cancel`（delegate，自动）；执行侧 `_authority_subtask()` 六道门（工具归属 / 能力声明 / depth=0 / slug 唯一 / 数量与局部上限 / 目标存在）全部在**弹批准之前**；子 Task 固定只读（父绑定改写 + 去重、无 exec/外发/记忆/再委派）；spawn 五件事（子 Task + 子 Run + 子 Job + `subtasks` 行 + 父 Run `subtask.spawned`）**同一事务**；父账户 `subtasks` 维度真实预占 1、子账户 `run:<child_run_id>` 的 `parent_id` 指向根 Run 账户（局部 tokens/requests + 固定 300s deadline）；`recovery` 按 `(run_id, slug)` 查 `subtasks` 行补身份一致结果，没行补 unknown 且**不阻断目录**。· **判据/数字**：新增 4 个测试文件 **35** 条（34 条覆盖 D1-c1…c11 + 1 条 `subtask.result` 的 usage「未知不写零」）；全量 **2038 passed / 7 skipped**（356.28s，macOS arm64；基线 `main @ 80e8e56` 同机 1999/7、352.89s；本轮 +39 = 35 + 事件类型参数化 2 + `subtask` 事件族 2），`ruff` 干净；负控 **10/10** 实测红（实测失败数 1/1/6/3/19/2/1/1/3/2，逐条见 PR body 与 §8）。事件 41→**43**、规则 59→**74**（D1 自身 +9；规格 §2.7 预估 D1 +8，§2.3 还要求 `dispatch.subtask_rejected`；合并 MCP 后在 main 的 65 基础上是 74）、工具 19→**23**（MCP 工具是运行时动态注册，不进 `SPECS`）。· **口径变更（写进 PR body）**：`c4.subtask_unavailable` 恒拒 → 保留未使用；`EXECUTABLE_KINDS` 加 `subtask`；`delegate` 不做动作级审批（按工具区分）；子任务工具即使 `action=read` 也走派发（否则父作用域/存在性没处判）；`cancelling` 是第 7 个状态（规格 §2.1 DDL 注释只列 6 个、§2.6 又要求它）；`dispatch.parent_fenced` 先登记为保留未使用（D2 接产生路径）。· **为什么这样切**：先把身份、限额、预算与恢复的账做实，再接执行；D1 合入后子任务**只排队不执行**是刻意的，判据里专门钉了"queued + created + pending，不写 `subtask.finished`"。· **D2 已闭合**：Worker 驱动子 Run、父 Run 挂起/唤醒、父取消传播、父改目标 fence、结果回填与回放、`subtask.cancel` 真正停止（见下一条 D2 记录）；**仍不成立**：子任务 UI 页面、A2A/外部 Agent/流式结果、跨机器调度。· **顺手修的既有缺口**：`rebuild_from_events` 之前不重放 `outbound_allow` 与 `mcp_servers`（投影重建会把网络/MCP 白名单丢掉），本轮按 `task.created` 一起重建。· **与 G2 MCP 阶段的合并**：main 已合入 MCP 阶段（#116–#119），本分支同步并解决契约冲突——`task.created` 两个新必填键并存、五种资源种类都有执行器（`dispatch.resource_kind_unsupported` 退化为防御性兜底）、MCP 与委派的授权/派发分支并列、事件 43 / 规则 74 / SPECS 23。
- **2026-11-12** · **G2 MCP 阶段施工规格（PR A，仅文档，未改任何代码/契约）**：用户说「继续」，按 §11.1 的「网络 → MCP → 记忆 → 子代理」顺序，下一张工单 = **MCP 客户端与执行器**。先落 [SPEC-G2-MCP-2026-11-12.md](SPEC-G2-MCP-2026-11-12.md)（现状代码事实 / 协议面 / 设计 / 判据 MCP-c1…c15 / 负控表 / 口径变更 / 诚实边界 / PR B–D 拆分与文件所有权）。· **现状核实（写进规格 §1，不是计划）**：`resources.py` 早就有 `mcp://<server>/<tool>` 与 `McpTarget`；`permissions._classify_mcp` 恒需人工批准（`c4.mcp_call`）；`dispatcher.EXECUTABLE_KINDS` 不含 mcp → 任何 mcp 请求**派发前**被拒；`isolation.CAPABILITY_UNTRUSTED_LOCAL_MCP` 要求四边界实测；`SPECS` 是**导入期静态表**，模型清单/白名单/`run.start.tools`/`control._exposed_tool_ids` 全直接读它；`event_schema._TOOL_ID` 是 19 个内置 id 的**枚举**（`tool.intent` / `tool.not_dispatched` / `receipt` 都用它），而 `tool.result.tool_id` 早已是形状 pattern。· **三条关键设计决定**：① **三把锁**——进程级用户配置（装机=信任决定）× 任务级 `--allow-mcp-server` 白名单（判在弹批准之前）× 既有逐次人工批准；② **不冒充沙箱**——server 以当前用户权限跑，`via_exec_backend=false` 如实标注（F2 后端 `spawn` 是一次性执行语义，服务不了长连接），`untrusted_local_mcp` 能力**不启用**，任务声明 `isolation_requires` 时 macOS 本机一律 unsupported；③ **动态工具必须可绑定**——`tool_id="mcp__<server>__<tool>"`、`tool_version=mcp1:<sha256(server|tool|原始 schema|归一 schema|描述)[:16]>`（**原始 schema 也进摘要**：server 改动我们不认的关键字也算换工具），schema 只接受 `toolschema` 封闭方言，认不出就**拒绝整个工具**（不是丢掉关键字），`additionalProperties` 缺省归一成 `false` 并记 `narrowed`。· **契约增量四件**（§3.6，全部走独立 `docs(contract):`）：`tool.intent`/`tool.not_dispatched`/`receipt` 的 `tool_id` 枚举→pattern（与 `tool.result` 逐字一致，旧事件仍合法）；`task.created.exec_policy.mcp_servers` 可选数组（沿用 F2 的 `isolation_requires` 做法，不 bump payload 版本）；`receipt.backend += mcp`、`receipt.evidence.kind += mcp_call`（N4 的教训：不加 `verify_receipts` 分支，合法凭据会被判"无法识别"）；新规则编号 5 个（`c4.mcp_not_allowed` 与 4 个 `dispatch.mcp_*`）。· **诚实边界（§7 共 12 条）**：本轮**无法联网核验官方规范**（本机 web provider 未注册），协议形状按 2025-06-18 已知口径写并 fail-closed，**真实第三方 server 兼容性 = 未验证**；无沙箱；只做 stdio；只做 tools（resources/prompts/sampling/roots/logging/progress/`list_changed` 全不做）；图片/音频/资源正文不收；MCP 无字节预算、server 无资源上限；同 server 串行；Windows 未实机；恢复只到 unknown+阻断；回放不调 server。· **未做**：一行代码没写，契约一行没改，判据测试**都还不存在**；本机基线数字待 PR B 落笔时以实测回填（§1 写的是 `main @ 0f30d01` 的 1905/7）。

- **2026-11-11** · **§7.1 语义摘要轮（PR #103 / #105，`main @ 66c1a25`）**：先写 [SPEC-SUMMARY-2026-11-11.md](SPEC-SUMMARY-2026-11-11.md)（H1/H2/H3 三个增量 + 判据 + 负控表），再串行落 PR #103（记录层 + 材料身份；注入编译 + `model.intent.context.summaries` 契约 + 回放读摘要）与 #105（生成路径：开关 / 授权四条件 / 账本预占 / 先持久化再引用 / 失败回落 / 崩溃不重试 + 长任务夹具）。测试 **1684 → 1790**（+60 本特性；另 +46 来自并行工作流），负控 **24/24** 实测红，`ruff` 干净，required checks 全绿。· **踩了**：① `executescript()` 建表会先提交当前事务，把摘要行与事件的同生共死假掉（已修 + 判据）；② 「关键信息保留」夹具假绿（每轮读同一个带约束的文件，约束根本没丢）；③ 负控两次假绿——NC2 夹具预算太紧（摘要激活后总把剩余往返一起牺牲，"重叠"发生不了）、NC7 只拆了一层 catch（`BudgetConfigError` 是 `TianshuError` 子类，兜底接住了）。· **留下**：新文档 [context-summaries.md](context-summaries.md)（含"为什么确定性裁剪不够、而模型摘要很危险"、决策顺序、崩溃恢复、长任务实测）；交付对账 §1/§3/§4/§5 与 PRD §7.1 状态已回写。
- **2026-11-11** · **N6 控制面记忆端点（PR #100，`feat/host-memory`）**：daemon `ControlPlane` 新增 **8 个**记忆 RPC（`list` / `get` / `remember` / `confirm` / `edit` / `forget` / `promote` / `erase`），`host.py` 原样代理 `POST /rpc`（真 Host + token 回归用例）；新模块 `memory_control.py` 收口 CLI 与 RPC 的控制面记忆操作（写操作一律走 dispatcher）；视图统一六项判据，写后视图按记忆自己的作用域回读，`promote` 的源作用域用 `source_scope`。测试 `tests/test_host_memory.py` 14 条真起 daemon + 真 IPC；负控（端点假成功不派发）真跑红；全量 **1728 passed / 7 skipped**。**⚠ 身份口径被复审判为不成立并已降级**：v1 无进程沙箱，`run_command` 同 uid 可读 authkey / 连 IPC / 跑 CLI 并自己落审批，所以端点**不是授权边界**，只在跨本地用户这层有意义（POSIX 0700/0600；Windows ACL 未加固未验证）；控制面价值 = 人机界面 + 共用 dispatcher 实现。**未做**：React 面板（端点可被调用 ≠ UI 有入口）、`memory.search` RPC、进程级沙箱。
- **2026-11-11** · **N3 L2/L3 蒸馏 + freshness（PR #98，`feat/memory-distill`）**：按 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) §4 只做**确定性**合成——投影 schema **2→3**（`memories.layer` 就地 `ALTER`，旧行 l1）、新模块 `memory_distill.py`（L2 聚簇引用式拼装 / L3 4 KiB 预算页 / 页 id 确定性 / `seq:<basis_seq>` 新鲜度基准）、`memory.consolidate`（1.0 / create / 模型可见 / workspace 无条件拒 / 结果 candidate）、`Recalled.stale` 与降权排序。全量 **1730 passed / 7 skipped**（N3 合并后 1714 + N6 的 14 条控制面用例 + 2 条作业原子性判据）。**踩到四件**：① 加 `layer` 列后三处漏改（INSERT 占位符、`search()` 的 SELECT、FTS 装配列），是"加列 ≠ 接通路径"的活教材；② L2 页一度压过更权威的 L1——proximity 0 < user 2，加 stale 次级键时先按 `(stale, 原键)` 排；③ 自抓两条"假判据"（`proof_count` 就位校验无测试、执行侧 workspace 守护不可达），前者补坏页用例，后者如实记为纵深冗余；④ `confirm` 会改写 `consolidated_at`（L1 也如此），freshness 若拿它当基准，晚确认的页会永远"新鲜"——改判据到逻辑时钟 `seq`，规格 §4.2 #5 的原文口径记为偏差。**复审整改（同日）**：独立复审 1 严重 + 4 中等 + 3 轻微，全部处置——① **真洞**：`retrieve()` 的无索引/索引不可用分支只排序不带 stale，默认生产路径上陈旧页照样排第一（原测试被 origin 权重掩盖）；排序键抽成 `_order_by_priority(..., stale=)` 三路共用，并补「页与 L1 同 origin/同 created_at」用例。② `stale` 从 bool 改三态 `bool|None`（无 `seq:` 不可判定，不再压成 false 谎报新鲜）。③ 补真正的 v2→v3 补列迁移用例（原 v1 用例从没删过 `layer`）、坏页 `l1`/空来源参数化、重复应用幂等、正文回退 fail-closed、旧页 evidence 快照。④ **设计边界**：`memory.consolidate` 只取代同层 **candidate** 旧页——已 confirm 的 active 页不因合成退役（模型可见免审批工具不能撤销人的召回授权），新旧页并存由 freshness 排序。⑤ 文档假话（F10 §7 表 / §15、PRD §5.2.2 仍写"未落地"）已改。`stale_map` 的「更新的证据」也只算未过期的 active L1。全量 `1730 passed / 7 skipped`（合并 main@da6b4a9（N3）+ N6 后）。**未做/不宣称**：无 L2/L3 vs L1 评测、无冲突消解、L3 仅 user 作用域、LLM 合成未做；Windows 实机与真实模型驱动 UI 仍未验证。
- **2026-11-11** · **G1 通用执行基础：§5.2.3 通用工具准入 + §5.6 共享预算账本（PR #91/#92）**：收尾对账里 G1 被登记为未做，用户说「继续」，于是先落 [SPEC-G1-ADMISSION-2026-11-11.md](SPEC-G1-ADMISSION-2026-11-11.md)（G1a 准入 / G1b 账本，含"要求 → 现状 → 本轮交付"与负控计划），再分两个 PR 落地。**G1a（#91）**：新 `toolschema.py` 把参数 schema 变成**封闭方言**（对象/数组/标量三组白名单，`oneOf`/`$ref`/`format`/`default` 注册期拒绝并报工具名 + JSON 路径），`validate_args()` 保留签名但真正执行嵌套约束（`argv[0]`、`minItems`、`minimum/maximum`、`enum`、`pattern`），准入前移到 `_dispatchable()`（非法参数不产生审批、不写文件），`ActionRequest.description_hash` 把**描述**也绑进审批摘要（条件字段：空则不进规范字典，历史摘要逐字节不变），`MODEL_FORBIDDEN_ARGS`（run/task/call/attempt/预算/deadline）注册期拒绝声明 + 执行期拒绝传入，路径参数拒绝 `scheme://`（URL 不再被拼成 `$WS/https:/example.com/a`），replay 仍只读事件（改 / 删当前工具清单后回放照样通过）。**G1b（#92）**：新 `budget.py` 建在事件库同一个 SQLite 文件（账户/上限/计数器/预占四张表），`reserve()` 在 `BEGIN IMMEDIATE` 内逐级检查并扣减、`settle()` 幂等且支持**分维度**（token 已知、金额未知 → `partial`，未知维度占用保留、不记零）、`cancel()` 沿子树传播、`reconcile()` 只报告不清零；`Limits.budget_limits` 打开后每次模型 attempt 前预占，失败即 `model.error` + 停下（**一次模型调用都不发生**）；控制面 `run.cancel` 传播到账本。· **数字**：新增 `tests/test_g1_admission.py`（19）+ `tests/test_budget_ledger.py`（20，含两条独立 SQLite 连接的真并发）；`main @ cc2240e`（#89 / #91 / #92 后）本机 **1601 passed / 7 skipped**（169.08s），`ruff check .` 干净；负控 G1a **9/9**、G1b **10/10** 实测变红；Windows 在 PR #92 resync 运行里 **5 failed / 1548 passed / 29 skipped**（既有族，未实机）。· **决定 / 踩坑**：① 契约里写了 ≠ 执行侧在看——`items`/`minItems`/`minimum` 一直写着却从未执行，这比没有 schema 更糟（讨论见 [tool-admission.md](tool-admission.md) §1）；② 未实现关键字必须**注册期拒绝**而不是当注解忽略，`$ref` 更有"禁止联网解析"的硬理由；③ 给摘要加字段前先问历史摘要会不会变：`description_hash` 做成**条件字段**，与「资源种类用 scheme 前缀」同一条理由——不能为一条新要求把既有授权链作废；④ 预占/结算必须靠数据库原子性，读-改-写会在并发下双花（去掉 `BEGIN IMMEDIATE` 的负控 5/5 次红）；⑤ 账本**默认关闭**（估算预占会让临界 run 提前被拒，属行为变更需评审，G2 前必须默认打开）——这条写进了文档与 §7，**不当作 §5.6 发布前置条件已满足**。· **留账**：`docs/tool-admission.md`、`docs/budget-ledger.md`、`docs/SPEC-G1-ADMISSION-2026-11-11.md`；交付对账 §1/§3/§4/§5 已更新。· **仍未做**：账本默认启用、网络 / MCP / 子任务资源种类与外发许可、"有界摘要 + 内容引用"分页、`run_command` 的隔离门、硬上限的可验证上界、G2、G3、§7.1。

- **2026-11-11** · **PRD 工程评审收尾轮：Windows 兼容 + 两个并发缺陷（PR #87/#88）**：首轮五条工作流合入后，Windows job 从 35 failed 起步逐轮收口——#87 修 `os.fsync` 对只读句柄 `EBADF`（改用可写句柄，仍失败只在边界降级）与隔离测试的"未知"口径、#88 修 `time.monotonic()`（Windows ~15.6ms 分辨率 → 热启动量到 `0.0`，改 `perf_counter`）与 `_pid_alive`（Windows 上 `os.kill(pid,0)` 是 TerminateProcess 语义 → 改 `OpenProcess`+`WaitForSingleObject`），并把 Windows 压到 **5 failed / 1509 passed / 29 skipped**（`main @ ec7d7ae` 实测；剩余 5 条全部既有族，逐条登记在交付对账 §3，另有一次命名管道阻塞导致的 25 分钟超时取消）。· **CI 又抓到两个与本轮新模块无关、但确实是真缺陷的问题**：① macOS job 红在 `test_event_stream_ends_when_daemon_goes_away`——daemon 已退出后 `DaemonHarness.__exit__` 补发的 `request_shutdown()` 撞上"端点还在、accept 已停"窗口，`multiprocessing` 的 `answer_challenge()` 抛裸 `EOFError`，它既不是 `OSError` 也不是 `IpcError`，把调用方打崩；修法把握手期 EOF 映射成 `ConnectionClosed` 且不重试，回归三条（POSIX 真 socket 假对端、跨平台打桩 `_Socket`、`request_shutdown` 返回 `False`），负控撤掉修复后三条全红。② ubuntu job 红在慢订阅者用例——`read_metrics()` 把"正在写的最后一行"当损坏（读到断在第 37 字符的 JSON），修法改成"没有 `\n` 收尾的尾段先试解析、失败跳过"，带换行的坏行仍报错；负控：撤掉修复第一条用例立刻红。· **数字**：`main @ ec7d7ae` 本机 **1536 passed / 7 skipped**（158.55s），CI macOS 1536/7、ubuntu 1535/8；`ruff check .` 干净。· **教训**：CI 红了要按用例名分家（别用"#50 家族"一句话盖住新缺陷——本轮真的把新缺陷误写进过旧标签，已按实测改正）；"本机连续绿"对并发窗口没有证明力，时序缺陷要用确定性构造（假对端不握手、手写半行）而不是多跑几遍；`EOFError` 不是 `OSError`。· **留下**：`docs/PRD-REVIEW-DELIVERY-2026-11-11.md` §3 的 Windows 逐条证据；`docs/M5-desktop.md` §3.6 的连接期语义；`docs/runtime-metrics.md` 的读者并发契约。

- **2026-11-11** · **N4 复审整改（并发孤儿 blob / 回执不可核对 / CLI 擦非 active）**：第二读者三个探针各复现一条真问题。① **高**：擦除的引用计数与 unlink 在 dispatcher 提交事务之外，两个进程同时擦共享同一 `content_ref` 的两条记忆时，双方都看到"对方还没擦"→ 都 `deleted=False`，随后两行都标 `erased`、文件永久留存（"已物理擦除"不成立）。修法：把 `memory.erase` 的 `_commit_result` 放进 `_begin_memory_execution` 的 `execution_guard` 块内——计数/删文件/提交在同一把跨进程锁里，第二个进程只能看到第一行已 erased。回归用"守卫深度探针"而非 sleep：把 `execution_guard` 换成计数上下文管理器，断言 commit 发生在 depth=1；负控：改成守卫外提交 → `seen == [0]` 红。② `verify_receipts` 对合法擦除回执报"无法识别的证据引用"（`memory_erasure` kind 未登记），且测试只调不 assert；补逐字段比对，测试改成 `== []`。③ CLI 擦不掉 forgotten/candidate/superseded：DELETE 前置默认 `before_status="active"`，erase schema 又没有 status；改成不默认声明、由执行侧 observed 与投影迁移校验兜底，补 CLI 用例。· **教训**："先检查再写"的跨进程临界区，必须把**提交**也包进同一把锁——锁只包检查等于没锁；回执"可核对"不是加个 evidence 字段，得让核对器认识它，且测试要断言返回值而不是只调用。
- **2026-11-11** · **N4 擦除落地 + 一个实施偏差**：`memory.erase` 做完了（控制面专属、审批 + Receipt、引用计数、`erased_at` 列、v1→v2 迁移），测试 +5、全量 1444/7。· **踩到的三件**：① 引用计数最初把**已擦除行**也算引用者 → 第一条擦除后第二条永远删不掉；改成 `erased_at IS NULL` 才算。② 规格写的"事务内更新 → 删文件 → 提交"在现有架构里做不到（执行器只返回结果、投影在 dispatcher 事务里更新），实际改成"先删文件 → 提交"；崩在中间只会文件没了、行没标擦除（正文如实不可用），反过来先标擦除文件还在才是泄漏——规格已按实际更正。③ `_MEMORY_COLUMNS` 加了列后，`search` 自己手写的 SELECT 忘了加 `m.erased_at`，`_row_to_memory` 直接 `IndexError`；列清单必须只有一处来源或每处 SELECT 都跟。
- **2026-11-11** · **PRD 工程评审新增要求首轮交付（W0–W5，PR #76/#77/#79/#80/#81/#82/#84）**：用户把工程评审内容写进 PRD 工作区后要求「先写 spec、新建分支、goal 模式完成」。先落 [SPEC-PRD-REVIEW-2026-11-11.md](SPEC-PRD-REVIEW-2026-11-11.md)（五个工作流 + 文件所有权 + 验收/负控），再把新增章节合入 `main` 的 PRD（PR #77；§5.2.1 冲突解决为记忆两节保留原编号、通用准入改 §5.2.3，记忆章节逐节核对未丢）。五条并行工作流各自 worktree/分支：G0 四维评测 + 真实任务集（#82）、本地指标 + 慢订阅者隔离（#81，真 bug）、隔离门禁探针 + 矩阵（#79）、一致备份恢复 + 迁移中断（#80）、逐类型 payload 契约 + 严格写 + 精确版本（#84）。`main @ c0dfa88` 实测 **1519 passed / 7 skipped**（170.12s），`ruff check .` 干净；相对 `da84d92` 基线 1316/6，本轮 +185，其余 +18 来自并行记忆工作流。· **踩了/决定了**：① 写侧的自动 v0 回退是 fail-open（`{}` 万能合法），必须读兼容与写兼容分开；② 单线程同步推送会被慢订阅者堵死控制面；③ 隔离只认探针证据、unknown 阻断；④ 备份要一致点 + 引用清单，中断产物不得当有效；⑤ 严格分支保护下多 PR 必须逐个同步 main 再等 CI。· **仍未做（如实登记）**：§5.2.3/§5.6（与记忆工作流同文件，下一轮）、§7.1 语义摘要、G2 网络/MCP/子代理、G3 平台实机与安装升级；逐条未验证项见 §7。· **留下**：`docs/PRD-REVIEW-DELIVERY-2026-11-11.md`、`docs/eval-four-dimensions.md`、`docs/real-user-tasks.md`、`docs/runtime-metrics.md`、`docs/isolation-matrix.md`、`docs/backup-restore.md`、`docs/event-payload-contract.md`、`docs/SPEC-PRD-REVIEW-2026-11-11.md`。
- **2026-11-11** · **N1b 复审整改：promote 目标作用域归一 + origin 不降档**：第二读者抓到两条中等问题——① 提升目标用 `MemoryScope(scope, scope_id)` 裸构造、不 canonicalize，而声明集合按 canonical 存：CLI 传 `/a/b/.` 会被判"不在声明内"，UI/API 会直接踩；② edit 固定 `user_confirmed`、promote 固定 `inferred`，把 `user_explicit` 记忆"降档"（权重 1.0→0.6），与 PRD "三档不降级"冲突。修法：新增 `resources.build_memory_scope()` 统一 canonicalize（workspace 走 `MemoryScope.workspace`），派发层/执行侧/CLI 三处都改；origin 改为**按这一版正文的来源**——控制面 edit → `user_explicit`、模型写经人批准 → `user_confirmed`、promote **来源随行**（投影校验 `origin == 原行 origin`）。补两条测试：workspace 目标原始路径归一、Receipt `resource/root` 指原行。· 教训："契约说三档不降级"要落到**每条写路径的 origin 赋值**上；scope 字符串比较前必须先归一，否则"语义相同"会在授权判定里变成"不在声明内"。
- **2026-11-11** · **N1b 落地：memory.edit / memory.promote**：`memory.edit` 一次审批产出两行（旧行 superseded、仍在库里可查；新行 active、`supersedes` 指向旧行），审批绑定 `before_revision`，不允许改 kind；`memory.promote` 是**移动**：原行立即退役（原作用域不再召回）、目标作用域新行是候选（再 confirm 才召回），Receipt 针对原行（`receipt_memory_id`），`source_refs` 必含原 id，只控制面能调。· **踩到两个同源坑**：策略层 `_classify_memory` 的 overwrite 分支把 `memory_origin == user_explicit` 当成免审批，于是 edit 也被自动放行；但执行侧 `_requires_approval` 的豁免只认 confirm → CLI 报 `overwrite 缺少审批绑定`。修法是把"免审批"按**工具**收口（`memory_tool == memory.confirm`），两处都改，负控：去掉该限制 → `test_cli_edit_keeps_old_row_and_activates_new` 红。第二个坑：`build_request` 对 overwrite 写死 `before_status: "candidate"`，edit/promote 的目标可能是 active → 改成用 `args.get("status")`（可不声明），状态约束由投影迁移校验兜底。· 另：edit 的 Fig 旧测试（`test_supersede_keeps_old_row...`）原来借 propose 的 output 造 supersede，现按生产形状改用 `memory.edit`。
- **2026-11-11** · **N1 复审整改：堵掉"控制任务复用为模型 Run → 模型继承控制面身份写 workspace"**：第二读者用探针复现——CLI 为 workspace 作用域 confirm/forget 创建的控制任务带 `is_control=1` + workspace 绑定，同一 task 上再开模型 Run，`memory.propose` 到 workspace 会 `succeeded applied` 并写入投影。根因：控制面标记是 **Task 级持久标记**，`exposed=False` 只挡 `memory.remember`、挡不住模型可见的 `propose`。修法：`memory.propose → workspace` 在**派发层与执行侧都无条件拒**（不因 `is_control` 放行），共享写只留 `memory.promote`；补回归 `test_workspace_propose_is_refused_even_on_a_control_task`（负控：去掉两道判定即 `admitted`）。同批按复审建议做的三件小事：`TaskStore.create_task` 对 `memory_scopes` 做 URI 校验（不再只查"非空字符串"）、`cli._task_json` 输出 `is_control`/`memory_scopes`、`_verify` 的"写别人的 task"分支加注释标明是纵深防御（authority/policy 通常先拒）。· **教训**：把"可信来源"落成 **Task 级**标记时，要问"这个 Task 还会不会跑不是那个来源的 Run"——答案是会，所以模型可见的工具必须**无条件**按模型规则限制，不能与"控制面特权"共用同一条豁免。
- **2026-11-11** · **F10 记忆产生侧（N1）落地**：见 §2 的 N1 条目（契约、执行侧、CLI、测试都写在那里）。· **踩了 / 决定了三件**：① 新规则编号的产生路径只认 `DispatchDenied` / `PolicyDecision` / `_reject` / `_reject_runtime`（`test_rule_ids._producer_hits` 的 AST 扫描），`MemoryDispatchRefused` 不算——所以同一条拒绝要在派发层用 `DispatchDenied` 抛一次，顺带满足"执行前就能拒"；② `memory.propose` 的 `origin` **不能**在投影层锁死 `inferred`：`user_explicit` 候选是免审批确认分支的合法输入（测试播种），越权防线在执行器与策略层，不在字段校验；③ 模型写 workspace 作用域必须拒（共享边界），但**拒在哪一层**要想清楚：策略层会因绑定而放行，所以拒点落到执行器 `_verify`，测试直接钉 `c4.memory_scope_denied` 且投影无行。· **没做**：见 §2 N1 条目的"仍没做"；Windows 实机、真实模型驱动 UI 照旧没有。
- **2026-11-11** · **记忆契约 v1.2（仅文档：PRD §5.2.2 / §5.4.2 + F10 §5/§7/§9/§10）**：把 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) §1 的契约增量写成**契约本体**。PRD：① §4.1 `Memory` 加 `layer`（l1/l2/l3）与 `erased_at?`，标注"v1.2 契约、代码未落地"；② 新增 §5.2.2 记忆工具契约增量——`memory.propose` 1.0→**2.0**（加 `scope`/`scope_id`，`task` 只能是本任务、`workspace` 不从模型进入），新增 `memory.edit` / `memory.remember` / `memory.promote` / `memory.consolidate` / `memory.erase`（含 `input_schema` 原样、`exposed=False` 的两道边界、origin 不由参数决定、不新增事件族、`task.created` 加 `memory_scopes` 键）；③ 新增 §5.4.2 四条规则编号语义（`c4.memory_scope_not_in_loadout` / `c4.memory_control_scope_binding` / `c4.memory_edit_requires_prior_revision` / `c4.memory_erasure_requires_receipt`）。F10：§5 加 `layer`/`erased_at` 语义，§7 审批表加 5 行（标"v1.2 契约"），§9 编辑入口指向 `memory.edit`（一次审批两行），§10 写清"擦除 = 删副本 + 删索引；事件日志读快照保留、UI 不许写已彻底删除"，§13 加注"v1.2 契约不在已落地口径里"。· **决定**：契约先行 ≠ 提前注册——`DECLARED_UNUSED_RULE_IDS` 的定义是"防御兜底、当前没有产生路径"，不是计划功能占位；拿它占位等于让注册表替不存在的路径作证（`test_rule_ids` 的判据就是"登记了必须有产生路径"）。所以四条编号只写语义，**登记与产生路径在同一次改动**（`rules.py` / `schemas/c4/rules.json` / PRD §5.4.1 三处同改）。同理 `tools.SPECS` 不提前加：加了就进模型工具清单、却没有执行器。· **未做**：代码一行没改（`tools.SPECS` / `rules.RULE_IDS` / `schemas/` 均未动）；新工具与新判据测试都还不存在；F10 §13 状态未变；纯文档改动，本地全量实测 **1316 passed, 6 skipped**（116.93s，与 `main` 基线一致）。· 落地顺序仍是 SPEC §8：N1 → N4 → N5 → N2 → N3 → N6。
- **2026-11-11** · **剩余记忆工作量施工规格（仅文档，未改任何契约、未写代码）**：立据 F10 §13 仍为"未实现 / 无入口 / 部分落地"的十条（#18–#27）+ §9 的 `host.py` 零记忆端点，落 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md)（形态沿用 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md)：施工纪律 + 文件所有权 + 每工作流"现状（代码事实）/ 设计 / 判据 / 负控"）。六个工作流：**N1 产生侧**（origin 两档、作用域创建、编辑 supersede、CLI/remember/promote，#18/#22/#24/#25/#27）、**N2 检索质量**（作用域优先级 + 向量 + RRF，#20/#23）、**N3 L2/L3**（确定性合成 + freshness，#19）、**N4 擦除边界**（§10）、**N5 恢复 + 审计表脱敏收尾**（#21/#26）、**N6 控制面 UI**（§9）。N1/N2/N3 都动 `memory.py`，**串行**；N4/N6 可与 N1 并行，**N5 与 N1 同改 `dispatcher.py` 故串行**。· **写进规格的关键判断（都还没实现，别读成已修）**：① `origin` 由**审批出处/控制面入口**决定，模型参数里永远没有 origin——`user_confirmed` = 人批准的 confirm，`user_explicit` 只由 `exposed=False` 的 `memory.remember` 产生；② **workspace 是共享边界**：模型只能写 `user` 与自己的 `task`，共享必须人签发 `memory.promote`；③ **编辑一次审批两行**（旧行 `superseded` 保留可查 + 新行 `active`），投影加 `_apply_edit` 分支，**仍不新增事件族**（只多一种 `tool.result` 形状）；④ 向量索引是**可重建投影**，`model_id + dim` 不同不混空间，未配置 provider 就降级 FTS-only 并如实标注；内置 `HashEmbeddingProvider` **只配测试与离线基线，不许宣称语义能力**；⑤ L2 v1 走**确定性引用式拼装**（两次合成逐字节相同 → 增量 delta 天然成立），LLM 蒸馏另立契约与评测；⑥ **擦除 = 删正文副本 + 删索引**；事件日志里的读快照删不掉，日志压缩与红线 1 冲突，规格明确"不做"，UI 也不许写"已彻底删除"；⑦ 恢复判据是**"有 intent 无 result ⇔ 一定没生效"**（result 与投影同事务），所以不写 unknown、不阻断目录；⑧ 审计表**终态后**脱敏，落地前必须先证明终态后没有任何路径从 `args_payload` 重算 `args_hash`。· **另外**：`memory.propose` 加 `scope` 是 schema 变更 → 按红线 8 升 `version 2.0`；工具面绊线 `test_veto_scope.py` 必须随工具增减同步登记。· **未做**：C2/C4/PRD/`schemas/` 一行没改（契约增量在规格 §1，待单独 `docs(contract)` PR）；规格里的判据测试**都还不存在**；本地全量重跑过 **1316 passed, 6 skipped**（113.69s，与 `main` 基线一致）。· **第二读者（只读核对）纠正一处真错 + 四处措辞**：① 我原写"task 作用域里被激活的记忆没人能忘记"——**错**：`tasks.py` 没有"任务结束"状态（`task.status` 恒 `open`），同一 task 再开一个 Run 就能让模型 `memory.forget` + 人审批走完；事实是"人能忘，但只能绕道模型 Run"，属**控制面缺口**而非隐私死锁，规格已改；② `apply_event` 不止在 `_commit_result` 调用（`rebuild()` 也走），已限定为"实时写路径"；③ `_SCHEMA_VERSION` 只进 `projection_hash()`、没有持久化版本号可比对，"版本不符则重建"是**新增机制**，已标注；④ `_apply_confirm`/`_apply_forget` 的 UPDATE 列比我写的多（恢复分支、`revision`/`last_seq`），已写全；⑤ `grep -rin` 会连 `.pyc` 一起命中，已限定 `--include=*.py`。· **仍未验证**：Windows 实机、真实模型驱动 UI。
- **2026-11-11** · **DEBUG-M0M1 收尾轮：除 Windows 实机外全部落地（PR #66–#72）**：先写 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md) 再动手，六个工作流各自独立 worktree/分支（文件所有权表写进规格 §0，KNOWLEDGE/README/DEBUG 只由 lead 落），主线合并顺序 #66 → #67 → #68 → #69 → #70 → #71 → #72。基线 `main@540c543`（1080 passed / 3 skipped）→ 收尾后 **1316 passed, 6 skipped**、`ruff check .` 干净。七件事：① 真实断电 → `fsutil` 四个持久化边界故障点（`TIANSHU_FAILPOINT_PATH` 限路径）× {新建,覆盖写,删除} × 三条断言，撕裂写模拟（清单/blob/WAL），真实 HFS+ `detach -force` 摘卷实验（opt-in，本地 3 passed）；② authority 数据根逃逸 → 固定用户级权威根（`authority.py`），守卫双读，真子进程双 `--db` 探针；③ 完整 takeover → `recovery.assess_run` 只读对账 + `LeaseStore.reconcile` + 三个入口（`AgentLoop.start`/`resume_run`、控制 Run），真 `SIGKILL` 前任 + 真子进程接管；④ 跨多文件撤销 → `undo_journal` 写前意图日志 + 幂等续做 + 恢复侧阻断（阻断是 journal 的投影）；⑤ #63 → 根因 `Listener` 默认 backlog=1 被探活自己占满（macOS 立即 ECONNREFUSED），backlog=64 + 自连握手后 `on_ready`，100 次文件级运行 0 失败；⑥ #58 → `model_attempts` 口径（全失败打印「0 个完成轮次 / 3 次尝试（费用未知）」）；⑦ #57 → `Check.kind` + 作用域化判据 + m4.5 基线，真实模型复跑被中转站 503 阻塞（如实写"未完成"）。· **另外抓到并修掉两个真 bug**：删除类动作的恢复去读已被删除的文件（`FileNotFoundError` 穿出 `recover_run`）、撕裂 WAL 被静默洗成空库（fail-open，历史全丢、恢复报"干净"）。· **踩了**：`git checkout -- <file>` 会把**未提交**的队友改动一起丢掉（负控回退要用文件备份，不是 git 还原）；`detach -force` 仍刷脏页（20MB 未 fsync 存活），所以"断电压测"不能靠它；测试夹具造"半张表旧库"等于拿不存在的历史版本作证；库契约接好≠产品接线（T3 的 `outstanding()` 没人调用）；探活自己会占队列。· **留下**：`docs/powercut-evidence.md` / `docs/authority-takeover.md` / `docs/undo-journal.md`（T3）/ `docs/m5-readiness.md` / `docs/cost-and-veto.md` / `docs/eval-scoped-checks.md` / `docs/SPEC-RESIDUALS-M0M1.md`；issue #40/#53/#54/#57/#58/#61/#63 有证据可关；#55 只剩 Windows 实机 + m4.5 真实模型复跑（外部阻塞）；#50（Windows 命名管道握手）仍未定位。· **仍未验证**：块设备/固件断电、Windows 实机、真实模型 m4.5。

> 格式：`日期 · 做了什么 · 发现/决定了什么`。最新在上。

- **2026-11-11** · **G2 网络阶段：任务级外发白名单 + `network.fetch` + §10.5 联网研究闭环（PR #110 / #112，`main @ b60f976`）**：先写 [SPEC-G2-NETWORK-2026-11-11.md](SPEC-G2-NETWORK-2026-11-11.md)（I1 白名单 / I2 执行器 / I3 联网研究闭环，含数据形状、派发链路落点、判据与负控、口径变更、诚实边界），再串行落 #110（白名单 + 执行器）与 #112（评测夹具 + 两条联网任务）。测试 **1790 → 1869**（#110 +41、#112 +13、并行 #108 +25），负控 **18/18** 实测红，`ruff` 干净，required checks 全绿。· **做成的**：`task.created.outbound_allow`（required 键，空也写；旧事件按 v0 读）；执行侧 `_authority_network` 在 `prepare()` 里先判白名单（未声明 = 一条包都不发）、通过后仍逐次人工批准；`net.py`（第 53 个模块）只用标准库 GET——同源重定向、1 MiB 上限、10s 硬超时、每跳/每块取消检查、不读代理不发 Cookie、固定头；字节账本按 `max_bytes` 预占 / 实际结算、不足时在弹批准前拒；内容引用先落盘再写事件、回放不联网（缺 ref 报 problem）；`RU-W1@2.0.0`（本机不可达端口 → 抓取如实失败 + 报告写"无法访问/没有取得"）+ `RU-W2@1.0.0`（本机夹具抓两个来源 → 带引用报告，服务器关掉后来源仍可离线核验）。· **踩了 / 决定了**：① 白名单与额度这类"注定失败"的前置条件必须在**弹批准之前**判完，否则用户收到"批了也发不出去"的批准；② 取消做了两层检查，单独拆一层判据**仍绿**（另一层兜住），两处都拆才红——冗余要证明；③ `tool.result.status` 封闭集没有 `cancelled`，取消按 `failed` + `output.reason=net.cancelled` 记，不为一个状态扩契约；④ "来源可离线核验"必须在**关掉服务器之后**读内容引用并重算 sha256，读不回记无效样本；⑤ 计划里估 14/22/10 条，实测 18/23/11，以实测回填；⑥ `RU-W1@1.0.0` 的「本轮不提供网络能力」在 #110 之后不再成立，**换版本（2.0.0）**而不是改判据文本。· **留下**：新文档 [network-g2.md](network-g2.md)（含仍然不成立的 12 条边界）；[real-user-tasks.md](real-user-tasks.md) 台账与 `scope.real_user` 样本 18→21；PRD §5.2.3 实现状态 / §10.5 第三类状态 / §11.1 G2 行、交付对账 §1/§2/§3/§4/§5、README 均已回写。· **仍未做/未验证**：MCP / 记忆执行器 / 子代理；真实公网研究与真实模型复跑；DNS rebinding 防护；检索 API；速率限制；内容分级与保留期/GC。

- **2026-11-10** · **F10 记忆落地轮的契约文档增量（`feat/memory`，文档侧）**：把 PRD 与 F10 从"提案"对齐到**实际落地形状**。改了三处：① PRD §4.1 `Memory` 补齐 `evidence[]` / `proof_count` / `consolidated_at?` 三个字段，并说明它是投影、不是第五张事实表；② PRD §5.2 新增 §5.2.1 记忆工具表（5 个工具的 `version` / `side_effect_level` / `exec_model` / C4 动作 / 审批 / 执行路径 + `input_schema` 原样 + "首版不新增事件族"的因果）；③ PRD §5.4.1 新增"记忆"组的 6 条规则编号与"用户该怎么做"。F10 侧：新增 §6.1 资源种类设计（`memory://` scheme 前缀、逐字节不变的裸绝对路径、fail-closed、不哈希的理由、派发/执行四处怎么分流），§13 改成**逐条实际状态**（23 条，含"未落地/未覆盖/无产生入口"），§14 改成判据↔**真实测试函数名**的映射（21 条）。· **决定/发现**：资源种类用 **scheme 前缀**而不是给 `ActionRequest` 加字段——后者会改 `to_canonical_dict()`，让**所有 file 请求**的 `digest()` 与 `action_digest()` 变化，历史审批 / grant / 派发记录 / 重放会集体失效；裸绝对路径 = `file` 则逐字节不变。另发现两个坑：**`dispatch.memory_not_found` / `dispatch.memory_stale_revision` 一度"登记了却没有产生路径"**（`test_rule_ids.py::test_every_id_has_a_producer_or_is_declared_unused` 直接红，靠 `dispatcher` 也产出这两条才转绿）；**`origin=user_explicit` 的确认免审批路径没有产生入口**（`memory_exec._propose` 固定写 `inferred`，防模型自称"用户明示"绕过审批）。· **测试数字（工作区实测，未提交；跑的时候队友还在改，以提交后 CI 为准）**：`uv run --frozen pytest -o addopts="" -q` → **1212 passed, 3 skipped**（105.56s，23:58–00:00 实测；**合入 `main` 前**的分支数字）；`--collect-only` → 1215 条 / 44 个 `tests/test_*.py`。新增记忆测试四个文件：`test_memory_store.py`（51 个 `def test_`，参数化后 64 条）、`test_memory_dispatch.py`（18 条）、`test_memory_loop.py`（11 条）、`test_resources.py`（22 个 / 42 条）。· **仍未覆盖**：L2/L3 蒸馏与向量召回未实现（`grep -rin distill src/tianshu/` 无结果）；`origin=user_explicit` 的免审批确认在生产侧没有产生入口。· **收尾时先红后绿的一条**：`test_default_blob_dir_tracks_the_event_log_store` 曾断言"默认 blob 目录 == `EventLog.blob_dir`"，与 `MemoryProjection.__init__` 的设计相反（正文放 DB 旁 `memory_blobs/`：事件 payload 不可删、记忆正文将来要能物理擦除，同目录会因同字节而误删日志材料）；现已改名为 `test_default_blob_dir_is_separate_from_event_payload_store` 并按设计断言，实测转绿。**判据别只信一次红/绿，要问它测的是不是设计要的东西。**· **别写成已验收**：Windows 只是跑不挡（不在 required checks），本机只有 macOS 数字。

- **2026-11-10** · **DEBUG-M0M1 修复对账（`fix/m0m1-corrupt-manifest`）**：用户问「M0–M1 报告里的问题修好了吗」，逐条复核 16 项（S1–S4 / M1–M4 / L1–L2 / R1–R5）——每项都有代码落点与钉住测试，总表补进 [DEBUG-M0M1.md](DEBUG-M0M1.md) 末尾。复核中新发现一条**真残留**：快照清单被截断/写坏时 `SnapshotStore.load()` 漏出裸 `JSONDecodeError`，恢复还没推理就先崩、CLI 只打印 "Expecting property name"、也不阻断。修法：`errors.SnapshotCorrupt`（与 `EventLogCorrupt` 同档）；恢复侧整份核对 → `unknown` + 阻断（不退回"没证据=没写"）；`undo.plan` 拒绝并给补救路径；库入口抛类型化错误；两条回归 + 负控（回退修复即红，红的样子就是那条 `JSONDecodeError`）。1077 → **1080 passed / 3 skipped**（105.86s），`ruff check .` 干净。· **仍然不成立**：真实断电、Windows 实机（#50）、authority 数据根逃逸 + 完整 takeover（**新开 #61**）、跨多文件撤销事务性。· **顺手对账**：§7 的「M4 评测未验证项」整块按真实模型轮更新（三条销账、只剩 Windows）；M0–M5 复审里已修的项保持原记录不动。 · **另外**：反复跑集成测试时抓到一条**既有**偶发红（`test_crashed_daemon_recovers_run_without_duplicate_side_effects` 首段：`on_ready` 已落盘但端点连不上），在 pristine `main` 上 80 次文件级运行复现 2 次 → 与本轮改动无关，开 **#63** 留证；不掩盖，也不当成绿。
- **2026-11-10** · **F10 记忆设计第二轮学习（mem0 / Hindsight，仅文档）**：读 [mem0](https://github.com/mem0ai/mem0)（ADD-only 加性抽取「不静默改写」、写入前去重、实体抽取、语义+BM25+实体+时序四路检索、SQL 事实为源 + 向量/实体为投影）与 [Hindsight](https://github.com/vectorize-io/hindsight)（Observations 带证据与 proof count、refine 而非覆盖；Mental Models 是预算好的可启动页面、读取为 DB read；增量 delta 刷新防漂移；freshness awareness；Decision Models；Memory Defense）后，把 9 条增量折进 [F10-memory-design.md](F10-memory-design.md)。· **最关键的判断**：mem0 与 Hindsight **独立收敛到同一个架构**——「事实是 source of truth，向量/实体/合成层都是可重建投影」——而这正是天枢红线 1 已有的形状；天枢不需要借鉴，只需要把 L2/L3 做成**带证据、可增量、可证伪**的信念。· **三条实质改变**：L2 必须有 `evidence[]` + `proof_count`（没有证据的信念不成立）；蒸馏**只做增量 delta，禁止全量重生成**（LLM 全量重写会漂移）；**freshness awareness**（陈旧层不短路检索，先回落原始事实核验）。· **不抄**：两家都是重服务 + 外部 LLM/embedding 硬依赖（违反零依赖与离线评测）；Hindsight 的 decision model 天枢跑不了；mem0 的 update=覆盖与天枢 supersede 是不同路线。· **别信的数字**：mem0 自报 LoCoMo 92.5 / LongMemEval 94.4 但明说开源版「方向类似、数字不同」；Hindsight 说别家分数是 self-reported。
- **2026-11-10** · **F10 记忆设计落文档（仅文档，未写代码）**：读 [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)（L0–L3 分层蒸馏、记忆资产化、装备 loadout、默认私有、可编辑记忆、冷启动 save file）与 [OpenViking](https://github.com/volcengine/OpenViking)（上下文即虚拟文件系统、三层读取、目录作用域检索、会话成文件 + diff、记忆带引用）后，落 [F10-memory-design.md](F10-memory-design.md)。· **决定**：记忆**不是第二个事实源**，而是事件日志的可重建投影（红线 1 已堵死独立记忆库）；记忆操作做成 C2 工具，**首版不新增事件族**——新增 `memory.*` 会与 `tool.intent`/`tool.result` 形成同一因果的第二份指针，正应了 §5.3.3 给 `parent_seq` 立的规矩；写入**分级审批**（propose 写惰性候选不挡；agent 发起的 confirm/forget 走 C4 审批 + Receipt，等同覆盖/删除）；默认 `scope=user` 私有、共享是显式动作；`origin=inferred` 永不自动激活（摘要/网页不能变成长期用户事实）；忘记=tombstone ≠ 物理擦除。· **不抄**：不建独立记忆服务、不引外部 embedding/VLM 作硬依赖、不做多租户/团队 ACL（D2）、不做云托管（D7）、不照抄 AGPL 代码。· **踩了**：`NEXT-CAPABILITIES-PLAN.md` §5 已有实施拆解，本文只补**设计**输入，两者不重复也不冲突（冲突以本文为准）。· **留下**：PRD §4.1 `Memory` 实体字段与 §7 F10 加了指向本文的指针；README 与 §9 索引同步。契约增量（C2 记忆工具、C4 审批规则编号）待 PR08 `docs(contract)` 单独 PR。
- **2026-11-10** · **按“采用最新技术、五年演进”要求修订扩展方案（仍为提案）**：在 [实施方案](NEXT-CAPABILITIES-PLAN.md) §0 增加浏览器读取、动态工具发现、混合检索与持久化委派目标。官方资料核验纠正旧 MCP 通用流程：2026-07-28 使用逐请求版本/能力及 server/discover，initialize 仅留旧版兼容路径。当前 Playwright 官方要求 macOS 14+，与项目 macOS 13 下限冲突，浏览器后端需单独门禁，未宣称兼容。LanceDB 是可替换候选，四目标制品本轮未验。程序化工具调用以真实隔离为前提；A2A 只面向外部 Agent 对接。未安装依赖、未修改实现或现行契约、未运行功能测试。

- **2026-11-10** · **下一阶段能力方案（仅提案）**：新增 [网络 → MCP → 记忆 → 子代理实施方案](NEXT-CAPABILITIES-PLAN.md)，按 11 个独立契约/实现 PR 拆解。源码核对发现现有只读工具绕过 dispatcher 且依赖 workspace、schema 校验是顶层小子集、预算计量主要在单 Run/cursor 内；计划先扩展公共资源准入与共享预占，再依次交付原生 web 工具、MCP adapter、本地显式记忆、只读子代理。**未修改实现、未运行测试、未改变现行契约**。官方 MCP SDK 的可选依赖方案需单独 ADR；“核心零依赖、adapter 可选依赖”仍是建议，不是已批准的新规则。记忆逻辑忘记与历史物理擦除分开，网络私有数据外发另行授权。

- **2026-11-10** · **M0–M5 复审修法（`fix/m0m5-audit`）**：把 [DEBUG-M0M5.md](DEBUG-M0M5.md) 的 12 条发现逐条裁决并落地——4 条中等修复（阻断核验下沉执行边界 + 恢复与派发共享执行守卫；未知工具恢复 fail-closed；畸形阻断记录 fail-closed；M1 注入矩阵扩到覆盖写）、6 条轻微修复、1 条误报加钉住测试、1 条文档补登；基线 `main@4eb4ea9` 的 1061 → **1074 passed / 3 skipped**（97.83s），`ruff check .` 干净。· **踩了**：`check → act` 的窗口不是"多查一次"能关的，得让两边抢同一把锁（`begin_execution` 的守卫覆盖整个写过程，恢复侧不进这把锁就白搭）；`except: continue` 与"未知工具套用已知推理"是同一个 fail-open 病；M1 的强证据其实读的是诱饵文件（只有负控才照得出恒真断言）；复审报告自己也会误报（中-5 说 `prepare` 不拒，实测 `rejected`）。· **留下**：修复记录追加在 [DEBUG-M0M5.md](DEBUG-M0M5.md) 末尾（每条的命令、实测输出与残余风险）；静态疑点/未验证项开成 **#53 / #54 / #55**；`errors.EventLogCorrupt` 语义扩到三种情形（hash 不符 / 外部 blob 缺失 / 阻断记录解析失败）；`recover_run(data_root=...)` 成为 CLI/daemon/eval 三处的统一调用方式。
- **2026-11-10** · **M5 合并后的收尾（PR #49 已合，PR #51 补丁）**：`tianshu host stop` 三缺陷、Windows/Linux 平台差异、UI 三缺陷、以及"挂住"问题全部修完；`main@6862d8f` 实测 **1061 passed / 3 skipped**。· **踩了**：`/shutdown` 端点挂在 `do_GET` 而 `host stop` 发 POST（永远 404）；清理回调赋给 handler **类属性**被描述符协议绑成 bound method（`TypeError` 静默吞掉清理）；`stop_all` 先置 `stop_event` 再清理 → Daemon 留成孤儿；Windows 的 accept 叫不醒（overlapped I/O）、`PeekNamedPipe` 直接抛错、重复实例抛裸 `PermissionError`；Linux 上关 fd 唤不醒 accept；**敲门自己也会阻塞**（windows job 挂 20 分钟）；子进程日志用未读的 `PIPE` 会死锁；崩溃恢复测试靠轮询 probe 猜进度导致 macOS 偶发红。· **留下**：`docs/M5-verification.md` §5（host stop）、§6（断线烧核）、§6.5（跨平台差异与 Windows 未解决问题）；issue **#50** 记录 Windows 命名管道偶发握手失败（根因未定位，已在 `ci.yml` 就近注明、**不加** `continue-on-error`）；UI 三个可见缺陷已修（任务状态/守护进程版本人话映射、1280 宽审批按钮回到首屏），遗留"审批事件 action≠rule id 导致标题兜底"另开工单。
- **2026-11-11** · **G1 收口轮（PR #96 / #97 / #99，`main @ 850e046`）**：账本默认记账 + 硬上限可验证上界（G1c）、资源种类 network/mcp/subtask + 外发许可门（G1d）、内容引用 + 分页读取 + 任务可见范围（G1e）。测试 **1601 → 1684**（+43 本特性、+40 来自并行 N2），负控 **18/18** 实测红，`ruff` 干净，required checks 全绿。· **踩了**：① 负控脚本两次假绿（node id 传成单串 → 「没跑」被当成红；占位里两处引用只去掉一处 → 模型照样拿得到），现在负控先断言「替换目标存在且内容变了」并清 `__pycache__`；② `.json` enum 顺序必须与 Python 逐字一致（镜像测试）；③ `read_content` 触发 `test_veto_scope` 的绊线清单——那是有意的口径变更，必须在 PR body 写明。· **留下**：两个新文档（[resource-kinds.md](resource-kinds.md) / [content-refs.md](content-refs.md)）与规格 [SPEC-G1-CLOSURE-2026-11-11.md](SPEC-G1-CLOSURE-2026-11-11.md)；G1e 的规格偏差（原计划 `content` 资源种类 + 策略双闸，按「只读不过派发」改为读工具 + 存储层可见范围）已回写 SPEC；交付对账 §4 重新登记「仍然不成立」清单。
- **2026-11-10** · **M5 桌面闭环（PR #49）**：三进程（Daemon / Host / 浏览器 UI）打通，IPC 契约与断线语义、审批 UI、关 UI 任务继续、重启恢复、离线演示脚本全部落地；测试 942 → **1060 passed / 3 skipped**（`f5da9ef` 收尾后实测）。· **踩了**：`recv()` 对"超时"和"断线"都返 `None`，客户端一断 daemon 读线程 100% CPU 空转（实测烧一个核，靠 `eof` + `ConnectionClosed` 修掉，回归测试带真子进程 CPU 记账）；驱动线程跨线程复用 SQLite 连接直接 `ProgrammingError`，Run 永远停在 `created`（`cross_thread=True` 修）；手写 HTML 截图会盖住 token 注入 Bug（真页面是空 `<meta>` 原地替换）；`tianshu host stop` 三个缺陷叠在一条退出路径上（`/shutdown` 挂错 `do_GET`、清理回调绑成 bound method、先置 stop_event 再清理）→ stop 永远 404、清理静默死、Daemon 留成孤儿，靠 POST-only + 回调挂 server + 清理完再放主线程修掉（真子进程回归才测得出来）。· **留下**：M5 偏差 D1–D5 与"不做安装包/托盘"的口径写进 [M5-desktop.md](M5-desktop.md) §2/§9；断线语义写进 §3.6 与 4 条回归测试；`SUITE_VERSION` 升 **m4.4**（否决即终局，issue #40，PR #48）；剩余限制与未验证项见 [M5-desktop.md](M5-desktop.md) §10 与 [M5-verification.md](M5-verification.md)。
- **2026-11-10** · 把未提交的草稿按 `7abad75` 事实重写并落库 · 发现草稿里 Windows 和测试数两处都是假话，属"文档说假话"复发；落库时又抓到第三处——README 长期声称「运行时数据放系统标准位置」，实际一直是 CWD 下的 `.tianshu/`。
- **2026-11-09** · 扩充 AGENTS.md 开发规则：新增测试纪律 / 数据与事件完整性 / 安全与授权 / 契约与类型纪律 / 依赖与平台 / 提交与仓库卫生 / 反模式共 7 节。· 规则全部溯源 PRD 红线与 REVIEW-PRA 教训，不写空话；固化「红就是红，不装作绿」（不用 continue-on-error / type: ignore 假绿）。
- **2026-11-09** · 建立本知识库 + AGENTS.md 强制读写钩子；README 重写为标准开源样式；`main` 配置分支保护（PR + CI 绿 + enforce_admins + 禁强推）。· 确认现状：M0–M3 完成、767 测试、Windows CI 暂摘（#21）；把「自审盲区 / 测试证据造假 / 文档假话」三大教训固化进流程。

- 2026-11-13 · 归档数据准入最终计划、独立终审、Git 继承绑定及推理探针十二份原件，共 84322 字节并逐项核 SHA256；保留计划通过与产品未验收边界。独立终审后只授权隔离工作树 T1，T2/T3 未授；归档不改产品、不将旧探针或既有全量当成新实现验收。另将契约证据 README 的“当前分支”改成精确历史提交，避免后续合入上游后失实。
- 2026-11-13 · 归档 `16721ff` 的正式独立 Git 绑定、只读重核脚本及计数说明；父链 fd5/a839、253 个冻结非文档对象、作者 2416/8 全量与 UI80 原日志均完成对应。旧 19/34 段落实际含相邻索引，严格 §8 日志为15/30；README 9/8 与7/8是过滤口径差异，附录明确、原件不改。内容未丢失；本条不证明后续 #133 组合或新 CI 通过。
- **2026-11-13** · **系统debug最终交付前收口**：#131的b6与#132的a36已在独立绑定和当前四required通过后正常合入main6f05/a839，tree分别相同；恢复组合d482同步实际main为dce，产品/测试/产物未变，只回填交付文档，最终head门禁与合并状态绑定PR #133，不借旧绿或提前承诺。补真实页面硬崩溃→CLI重启→页面继续原Run，一次文件Receipt、21条旧事件前缀和hash/mtime保持，未结算模型intent及3条握手诊断如实留存；首轮错过模型期限的失效操作不计通过。18项统计经独立复核为8/8/2；Windows性能只报告实测与未归因，不宣称稳定SLO。历史失败、平台/跨库/费用/GIL边界不删除；按精确对象继承避免重复全量，只推最终候选，其他G3/历史工作树不操作。

- 2026-11-13 · PR #133 在四项 required 成功后由原 debug 会话正常合入 main f2aef9d；数据生命周期分支同步该实际对象，唯一冲突文件知识库的两个区块均保留双方历史。开始验证新组合，未更改上游恢复实现或将上游 CI 当作本片通过。

- 2026-11-13 · main f2aef9d 与备份首片的新组合 lock/ruff/full 均 exit0；全量2439 passed/8 skipped、427.25s，255冻结文件前后一致。UI源码/测试/产物未变，3份dist精确对应a839及f2aef9d，沿用已绑定80项证据而未机械复跑。所有日志与元数据归档；正式对象绑定与该候选原生CI仍分别核实。

- 2026-11-13 · 准入契约分支吸收已审备份候选b5b5c7f及main f2aef9d；只合并知识库双方历史，契约正文与全部产品/测试对象保持。PR134新CI运行中，T1仍在另一工作树独立验收；本同步不新增产品通过结论。

- **2026-11-13 · 工程管理与调试教训归纳（只读复盘后回填）**：本次由另一会话只读复核 `3648d79..cefa1e3`（#130–#136 共 7 个 PR、确认 18 项缺陷 8/8/2/0），把不针对单一缺陷、可迁移到后续开发的经验补进 §5：证据产能比与可生成物、Windows 23/25 分钟的门禁余量、实验分支不合 main、跨平台默认值、第二读者换上下文。本条是**归纳**，不新增缺陷结论、不重跑测试、不改变任何提交或 CI 口径；具体证据仍以各 PR 正文、`DEBUG-SYSTEMATIC/IO/BUDGET/RECOVERY-2026-11-13.md`、`WINDOWS-CI-2026-11-13.md` 与 §2/§7 为准。未触碰其他工作树与未提交内容。

- 2026-11-13 · T1正式交权后整合最新已审9129095，使用独立临时stash保全11文件并快进，只合并知识库状态；产品/测试按交接摘要不变。第二读者内容未决0，开始最终组合门禁；T2/T3尚未实施。

- 2026-11-13 · T1整合main #133首轮全量4红2562绿8跳过，定位到新恢复夹具在初次建库前注入坏材料；按契约保留拒绝，只授权测试顺序适配与独立负控。原件归档，不称产品已验收。

- 2026-11-13 · T1夹具顺序修后新组合全量2566 passed/8 skipped435.78s，lock/ruff0，264项冻结无漂移；独立历史非原子恢复负控仍两项真红。固定本机候选与原件，最终SHA/原生CI另绑，T2继续按分段授权推进。

- 2026-11-13 · PR134最终b5四required真实全绿，独立原始CI核对及当前head/base/保护guard均无阻断，正常合入main69119f0且tree完全相同。契约分支仅吸收该合并事实和CI证据，不夹T1产品；后续另开纯docs契约PR。

- 2026-11-13 · T2/T3独立验收收口，root接管冻结候选后普通merge同步main0df，只融合知识库与三份上游CI证据。T1九项+恢复fixture、T2四项、T3三项精确继承；README与指南窄改当前迁移事实。最终集中lock/full/ruff exit0，2672 passed/8 skipped（433.15s），267非docs对象无漂移；正式SHA、新实现PR四required及新main自身CI待核，不借旧绿色。独立原始失败、探针修正和非kill均保留，实际restore/容量/GC及第二至四批仍未交付。

- **2026-11-14 · 低磁盘修后完整本机门禁与证据保全**：root核作者七路径交接后接管live，恢复memory_exec相对879多出的无关EOF换行，冻结906源 `df72efbd`；首轮41失败的窄修保留原授权/审计/父版本与硬退断言。lock/full/全仓ruff均exit0，3183passed8skipped、509.43s，前后源码/HEAD/status不变；完整UI以57项精确不变继承原13文件91测试和类型/构建通过，未重复跑。六原脚本.py.txt与173映射原字节、首红/观察器错误/负控均保全，作者311联合不与独立或全量相加。泛凭据模式首扫命中六既有测试/证据fixture，逐字节与HEAD相同，属观察器误报、未新增凭据且不打印值。906窄增量随后独立PASS（`016f4c2f` / facts`3ef68826` / index`b5825b37`），root亲读并核1944+索引、73.2MB原件ignored保全；7精准测试和4真实离线路径通过，六负控按行为真红，BASE-B1未修。最终事实/正式对象与原生CI仍待；未扩大为G3整批完成。

- **2026-11-14 · 最终对象事实计数修正**：第二读者核首轮41条FAILED汇总发现，现行低盘文稿把“23项存储实例接线+1项规则计数”误写为24项夹具。root亲读原日志确认approval8、m2 1、recovery14，共23；现只校正文稿并保留该发现，原9a69对象不能冒称全部事实准确。产品、测试、锁、UI和3183passed8skipped的906完整门原件不变，新正式对象仍须独立窄绑定。

- **2026-11-14 · 低磁盘交付收口与恢复B契约**：PR144正常保护合main8f2a，PR run43553215035与main自身push43555557537 attempt1六job/四required均成功；main原件及精确对象由第二读者独立确认，root亲读并核12报告文件/59输入，完整日志和产物保留ignored。旧normal-finish失败以issue145保留unknown/unfixed，root明确承担内部研发风险，未改生产猜根因或销账。B三版草案完成独立文字迭代：修先删后锁、token空窗、E/P/S/D映射、合法v1兼容及清理目录屏障先于aborted；v3仅可提交纯docs契约，产品/43验收/断点仍NOT_RUN。此处不称G3整批完成，不运行付费模型或10h soak。

- **2026-11-14 · 恢复B正式文稿与本机验证**：32文稿/公开子集经独立窄集成确认，§1–8精确继承已审v3，产品零delta。首轮全量1F3182P8skip因继承非官方端点，原红与受控对照、独立因果诊断均保全；没有把环境前提失败说成产品已修。清除模型/凭据配置后同源lock/full/ruff全0，3183P8skip、514.95s，1092文件和Git状态稳定；本次仅回填事实，不重复未变UI。正式提交/新原生CI待绑定，实施计划独立审另列接口衔接修订，B产品/断点仍NOT_RUN；issue145及BASE-B1未修边界不变。

- **2026-11-14 · 恢复 B 契约同步诊断基线**：PR148 正常保护合入 main b450，PR 原生 CI 经独立核验；main 自身 push run43567298082 仍待终态。恢复契约分支通过普通 merge 同步，知识库唯一冲突合成双方事实，契约正文、原件与全部产品保持；原146首次CI失败、issues145/147及旧PARTIAL记录不覆盖。新组合的本机门禁、正式对象与原生CI另核，本条不授权恢复B产品实施。

- **2026-11-14 · 新组合本机全门与 main 自身失败分开记账**：1127文件/manifest2df18981的lock/full/ruff均0，3186 passed/8 skipped、524.89s，前后身份与Git状态稳定；文稿同步独立PASS只覆盖32文稿及继承身份。main b450自身run43567298082首次终态failure，Windows片1正常收尾测试第55轮database is locked、queued无run.end；六job和两份artifact完整保全，独立终态与根因另查，不重跑覆盖、不猜同因。恢复契约推送暂停，仅回填知识库事实，其他1126文件不改；B产品未授权。

- **2026-11-14 · 低磁盘 T2 作者候选冻结**：以已审542源逐项0差异开始，完成真实ToolSpec资格/实时空间、执行器独立核验与已有Run恢复例外；资格拒绝同步代码/schema/PRD。首红4项与边界12项真实失败保留；固定T1六项继承失败、phase1并发context失败均留原件。恢复矩阵首轮54绿6个epoch观察器错断言、边界首次绿8个lazy表观察器错误、静态格式错误与单门脚本第7项匹配计数2（非kill）均如实保存。最终三项联合红按原审计/静态登记目的窄修；相关142、MCP/派发64、schema绑定3通过，8个单门负控真红后完整复原。双真实进程靠通信屏障，硬退只用TIANSHU_FAILPOINT；无sleep时序冒充、无真实模型/付费。新完整组合及相对T1本片delta/源哈希/原件另冻交第二读者，作者停写；未full/lock/CI、commit/push或T3。

- **2026-11-14 · 低磁盘 T2 独立通过、T3 作者候选冻结**：第二读者在543源冻结上给出 `PASS_T2_FROZEN_CONTENT_ONLY` / `PASS_T2_NARROW_DELTA_AND_INDEPENDENT_EVIDENCE`，最终独立80 passed、9变异10个行为失败、0 setup/import/collection kill、阻断0；T2报告/facts/index及T1/T2源映射已复制到仓库内[证据目录](evidence/low-disk-2026-11-14/)。T3接通CLI/RPC/UI同一严格资格，真实CLI与IPC验证默认effects、显式pure_only、坏策略`bad_params`、重开token和workspace writable独立；UI两路径与失败重试保留选择。CLI首红argparse未知参数、RPC首红低盘`internal`、显式null/array红、UI 13文件87P3F、IPC跨线程观察器红均保留。三项最终负控（CLI改effects、RPC绕过解析、UI详情漏传policy）真红后恢复。最终定向门禁Python 76 passed、UI目标4 passed、typecheck/build/install/ruff/compile/stub扫描均exit0，dist从`B-xT4bPb/tHfDYJNM`换为`CIZ6Bgii/XPu6LbkB`且前后清单与SHA留档。T3独立复审、统一全量、lock、全仓ruff、完整UI suite和原生CI未运行；详见[低磁盘交付记录](LOW-DISK-2026-11-14.md)，不commit/push或创建PR。

- **2026-11-14 · 低磁盘T3独立通过与公开证据收口**：亲读T3独立报告并逐hash核1485项加索引自身，52Python/4App与11行为红按实际阶段记录，完整原件1486件保全ignored档案。整分支原717报告明确INT-E1索引缺3视觉dist不批准，root核实系dist忽略规则而非产品产物丢失，只补最窄例外与原字节，不重写旧失败；作者711核心与root717含6派生区别、契约75项为历史时点同步澄清。原a022/T2/T3 CLI预算接线缺口BASE-B1如实披露并单列跟踪，未顺手修预算。当前只改事实文档/证据封装，267产品/测试/锁/dist不变量保持；新候选独立销账、最终组合、正式对象和原生CI另核。

- **2026-11-14 · PR #144 首次Windows红与两测试诊断候选**：原run43549430150六job/两artifact和完整失败原件保全；Windows片1的READY CRLF比较与正常收尾Run failed保持真实失败，未rerun/取消/放宽required。拒绝无根因证据的共享锁提案并恢复生产字节，仅复用CHILD_TEXT修协议、将原心跳收尾断言扩至60次并显示真实run.end。新1030源2922a345上lock/full/ruff通过，3183 passed/8 skipped（513.63s），前后无漂移。第二读者在固定副本确认原断言未减、真实CRLF正控及坏READY/reason两call负控；完整182件独立包和9件本机门原件逐hash保全ignored，公开只发明确34原件子集及两份导航清单。心跳根因未复现/未修，Windows和新正式对象尚待，不合并旧红、不外推设备平台；本次只更新两测试和事实证据。

- **2026-11-15 · W3 state 响应 8MiB 上界交付（`feat/state-response-bound`，基线 main@0c5bb71）**：先复核登记项并写复现测试在基线代码实跑变红——50×~313KB goal 的对抗数据集让 `state` 响应 14.4MB（`ProtocolError: 消息 14419496 字节，超过上限 8388608`），且查明真实断连路径：daemon 发送线程 `_send_loop` 把编码超限的 `ProtocolError` 当 `send_error` 静默 drop 整条连接（端到端复现 `ConnectionClosed: 对端在消息结束前关闭`，不是超时）；`_approval_context` 每次 `read_types(["approval.requested"])` 全表扫 + 逐条 SHA256 验签的失控也被 spy 用例实证（300 张历史审批在库即红）。修复：`_state` 改 4 MiB（帧上限一半）预算内按 tasks→runs→approvals 份额两趟装填，放不下按条停并置 `tasks_truncated`/`runs_truncated`/`approvals_truncated`（加字段不升版本，契约行走 `docs(contract)` 单独 commit）；单条目超预算 fail loud 抛 `internal`（不静默丢条目、不截断 goal 文本）；`_approval_context` 改按 pending 集合所在 Run `read_run(verify=False)` 有界读（与 `_run_view` 同口径，读取量与全局历史解耦；decide 响应的 rule_id 有专门测试钉住不退化）。负控 2 条实跑红→还原绿（NC1 拆装填守卫→对抗用例红；NC2 拆有界读→spy 红；还原以 SHA256 比对字节一致）。修复后 test_control 33 passed、test_daemon+slow_subscriber+integration_m5 42 passed；本机门禁 `uv lock --check` / `ruff check .` / 全量 `uv run --frozen pytest` 均 exit 0（3400 passed / 11 skipped / 739.34s，macOS arm64），设计取舍与不变量论证在 [SPEC-STATE-RESPONSE-BOUND-2026-11-15.md](SPEC-STATE-RESPONSE-BOUND-2026-11-15.md)。未决：`task.list`/`approvals.list` 无体积预算（同对抗 goal 仍可撑大其响应帧），登记为后续工单候选；本机为 macOS arm64，Windows/Linux 以 CI 为准。过程事故记录：在本仓库误用 `git stash`（push 因 pathspec 失败后 pop 弹出他人旧 stash 导致 KNOWLEDGE.md 冲突），已恢复 HEAD 版本、stash 条目原样保留无丢失——**共享仓库一律不用 stash，改用 /tmp 文件交换法做变异还原**。

- **2026-11-16 · 文档对账批次 A 重验轮（PR #140 rebase 到 main@702ba40 后逐项对照）**：原轮（2026-11-14）26 项普通文档修复因列车阻塞滞留，本轮逐项对照现 main 重验——**24 项保留**（其中引用与事实全部重证：#40/#53/#54/#50 已 CLOSED、#55 仍 OPEN；`recover` 未核实效果实测 `cli.py` 返回 1；`run.end` required 集合含 tokens/turns/budget_enforced；PRD F14 在 §7；M4 §7.4/`test_slow_subscriber.py`/`SEND_QUEUE_MAX=1024`/`WINDOWS-CI-2026-11-13.md` 等引用全存在）、**1 项升级执行**（§8 错位日志归位从 5 条扩到 9 条——main 其后又积压 4 条：低磁盘 T2/T3 三条与 PR #144，全部归位到各自日期区）、**1 项销**（backup-restore §0 T3 行已被后续 PR 超越：现文已写「三片均已独立验收」「T2/T3 已随 PR #136 交付」，原漂移不存在）。**数字类修复按现 main 重测**：63→**73** 模块（+10：scheduler/schedule_cli/restore×3/embedding_openai/keychain_macos/run_policy/disk_admission/platform_identity）、117→**133** 测试文件（+16）；23 ToolSpec（19 可见）/`SUITE_VERSION=m4.6` 经 AST/代码实测在现 main 仍真。**重验轮新抓漂移并顺手修**：F12 Keychain 四处「v1 未实现」旧口径（README×2 + 本文 §7/已知限制×2）——`keychain_macos.py` 已随 W1 落地并经 #151 独立复审，全部改为 macOS 已落地/其余平台如实 unsupported；README 架构树按现 main 补全 34 个缺失模块（PR 原 24 + 其后新增 10），头部保持「数量以实测为准」不写死计数。SPEC 文档补 §2 重验记录。仅改文档，不改代码/测试/CI。

- **2026-11-16 · PR #162 崩溃恢复/退出清理与 source bundle 封闭修复**：独立读者对
  `3863887/tree 7d6de1b` 判 `UNFIXED`，两条 HIGH 分别是 owned Host 崩溃后不自动
  恢复，以及 owned/attached 崩溃后退出遗留 Daemon 与运行锁/socket。`6dfa605` 新增
  owned/attached 最多 3 次有界退避恢复、基于已退出 child 的 PID/`started_at` 清理，
  以及在 Host 元数据跨进程守卫内按 Daemon 锁 SHA 条件停止；`df1aacd` 再修 supervisor
  将仍存活 owned `Child` 句柄丢掉后错误接管新 endpoint 的竞态。定向结果 27P/1S
  （唯一 skip 为 Windows 专用句柄）、Cargo 32P，fmt/clippy 通过。精确 `df1aacd`
  macos-arm64 产物归档 SHA256
  `4448f7dbbea0e1b12ae133c22ef0e48eb10ceb486768013d46cc5a8520618001`、
  manifest `8c04b09f0a84e2feb59918b5bb90e4ba8aeb2f3929fc7e889432b92ce5dc9722`；
  verify/runtime smoke、原生篡改/未列 hook 负控、安装、两路崩溃恢复、普通 GUI、
  attach→quit 与 takeover safe/race 均通过，证据在
  `.omc/artifacts/release-app-final-df1aacd-2026-11-16/`。

  随后发现 Python `verify_release.py` 对传入 source bundle 只验清单声明字节，直接加入
  `bundle/app/sitecustomize.py` 仍 exit 0；归档层与原生壳虽已拒绝，但独立 Python
  verifier 应同样封闭。`eaa9da0` 新增完整 source 文件集合校验和回归：旧实现
  `DID NOT RAISE VerifyError`，修复后 1P，`test_release_verify.py` 16P；真实 634
  文件 bundle 的 verify/runtime smoke 仍 pass。Ruff 与 diff check 通过。当前仍须从
  docs-only 最终 SHA 重建产物并重跑两路崩溃/GUI/takeover，绑定该 SHA 六项 CI，
  再由全新第二读者 PASS；在此之前不得普通合并或启动精确 main 的 release workflow。

## 9. 权威文档索引

- **记忆交付验收**：[MEMORY-ACCEPTANCE-2026-11-11.md](MEMORY-ACCEPTANCE-2026-11-11.md)（逐条判据↔测试 + 负控总表 + 未验证 10 项；数字以 `main @ c4b020a` 实测为准）。

- [PRD-v1.md](PRD-v1.md) — 产品需求与架构（唯一权威）：范围、契约、红线、里程碑、决策记录
- [TECH-ARCH.md](TECH-ARCH.md) — 技术架构：选型、进程模型、打包、平台差异
- [REVIEW-PRA.md](REVIEW-PRA.md) — PR #7 独立复审报告（§5 教训的主要来源）
- [DEBUG-M0M5.md](DEBUG-M0M5.md) — M0–M5 对抗性复审报告 + 本轮修复记录（裁决、命令、实测输出、残余风险）
- [M5-desktop.md](M5-desktop.md) — M5 桌面闭环：三进程拓扑、IPC 契约、恢复语义、偏差（§2）与已知限制（§10）
- [M0-verification.md](M0-verification.md) / [M1-verification.md](M1-verification.md) / [M2-verification.md](M2-verification.md) / [M3-verification.md](M3-verification.md) / [M4-verification.md](M4-verification.md) / [M5-verification.md](M5-verification.md) — 各里程碑验收证据
- [F5-context-compile.md](F5-context-compile.md) — F5 上下文编译的设计与取舍
- [network-g2.md](network-g2.md) — G2 网络阶段：任务级外发白名单 + `network.fetch` 执行器（两道门、执行侧规则、字节账本、内容引用、回放不联网、判据与负控、诚实边界）
- [SPEC-G2-NETWORK-2026-11-11.md](SPEC-G2-NETWORK-2026-11-11.md) — G2 网络阶段的施工规格（三个增量、判据、负控、口径变更）
- [context-summaries.md](context-summaries.md) — §7.1 模型语义摘要：为什么确定性裁剪不够、记录层 / 注入 / 回放 / 生成路径、崩溃恢复、长任务实测与诚实边界
- [F10-memory-design.md](F10-memory-design.md) — F10 本地记忆设计（v1.1）：投影而非事实源、L0–L3 分层、作用域与装备、默认私有；**§6.1 资源种类（`memory://` scheme）**、**§13 契约增量的实际状态**、**§14 判据↔真实测试映射**
- [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) — 剩余记忆工作量施工规格（**待实施**）：§13 #18–#27 逐条现状与设计、六个工作流、契约增量清单、判据与负控口径、明确不做
- [F1-exec-primitive.md](F1-exec-primitive.md) / [exec-run-command.md](exec-run-command.md) — F1 执行原语与 `run_command` 工具
- [REFS.md](REFS.md) — 参考仓库与固定 commit
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — 分支、提交、独立复审规范
- [../AGENTS.md](../AGENTS.md) — AI 开发强制指令
