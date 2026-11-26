# 能力边界清单（不假装能做到的事）

> 本文件从 README 拆出。README 门口只留几句真话与指向这里的链接；
> 完整的边界、残留、评测口径与未验证项集中在这里，便于逐条核对。
>
> 本仓库的规矩是**文档不许说假话**（见 [CONTRIBUTING.md](../CONTRIBUTING.md) §4「文档改动怎么提交」）。
> 这份清单刻意写在前面而不是埋在文末——它是产品承诺的一部分，不是免责声明。
>
> 逐条状态以代码与 CI 为准；本文件不做静态计数。

## 1. 总清单

这份清单是刻意写在前面而不是埋在文末的——本仓库的规矩是**文档不许说假话**（见 [CONTRIBUTING.md](../CONTRIBUTING.md)「文档改动怎么提交」）：

- **`Workspace` 只约束内置文件工具的路径。** 默认 `local` 命令后端的隔离级别为 `none`。启用 macOS Seatbelt 或 Linux Landlock 后，子进程的约束以实际选中的后端与审批前的实测报告为准。
- **macOS 之外密钥不进系统钥匙串。** Keychain 后端已按 W1 落地（`keychain_macos.py` SecItem 直调，仅 darwin 加载，`TIANSHU_CREDENTIAL_BACKEND=keychain`；CI 与本机真实通过，独立复审见 #151）；其余平台无后端、请求时如实抛 `CredentialUnsupported`；凭据接缝 + 全链路脱敏照旧交付。
- **Windows 没有用户设备实测。** Windows 已是四项 required checks 之一；基准 `ccff93e79cfeb93e47e5a35012fd92fae366e941` 的 [main CI](https://github.com/lorek666/tianshu-agent/actions/runs/43298476414) 四项通过。该次 runner 为 Windows Server 2025；不能把 `windows-latest` 永久等同于某一系统版本，也不代表 Windows 10/11、ARM64 或安装体验。历史认证问题已定位为密钥文本模式写入并修复；本轮另定位了输出管道收尾阻塞，修复与实际耗时见 [系统 debug 记录](DEBUG-IO-2026-11-13.md)。平台跳过原因以该次 CI 的 `-rs` 日志为准。
- **旧材料空树复核不是原子快照。** `7b4d80b` 增加最终逐目录真实内容复核，堵住已下降目录内迟到材料被延迟 stat 隐藏的缺口；保留身份/子项闭包及错误 fail-closed，观察成本有界。它仍不能排除某目录最后一次观察后的同 uid 修改，不是物理隔离，也未修复自定义路径的保留名祖先误识别。原 CI 红、新本地全量和仍待最终托管 CI 的范围见[修复证据](changelog/2026-11-20-integration-ci-repair.md)。
- **非标准材料路径的保留名祖先可能被误拒，尚未修复。** 非标准数据库或异根 data_root 所用的私有 materials 布局若处于 `artifacts`、`blobs`、`memory_blobs` 祖先下，可能被标准材料域识别误判，按所有者不匹配 fail-closed 拒写。默认 `events.db` / AgentOS 布局不受该已复现组合影响，不阻断首次 Apple Silicon 限定预览；这不表示任意 `--db` / materials 路径均受支持，搬动测试临时根也不是产品修复。跟踪 [#204](https://github.com/lorek666/tianshu-agent/issues/204)。
- **受管恢复写入依赖目录 `fsync`。** Windows 可生成只读计划并复核符合绑定的隔离批次。有效写入会返回 `durability_unavailable`。Windows 的离线 GC 遇到任一文件候选时拒绝整批。当前仅支持受管恢复到备份记录的原路径，物理掉电和用户设备验收仍待补齐。详见[备份与恢复指南](backup-restore.md)和 [#200](https://github.com/lorek666/tianshu-agent/issues/200)。
- **发行草稿入口已接入，首发验收未完成。** Tauri 薄壳管理窗口、托盘、单实例和 Python Host 生命周期；锁定 Python runtime、打包校验、macOS 安装/卸载脚本和四目标 `release.yml` 已随 PR #175 / #186 合入。workflow 的 `macos_arm64_preview` 只生成单目标草稿；按 [PRD §11.1](PRD-v1.md) 首发仅准备 `macos-arm64`，主干工作流实跑、下载字节校验与 Mac 真机安装/升级/回退待验。没有 OS 签名、公证、自动更新；未验证的 macOS 版本和其他平台不在首发支持范围。
- **长时稳定性未验证。** 首次 `macos-arm64` 限定预览按 [PRD §10.6](PRD-v1.md) 取消强制 ≥10h soak，不设替代时长；既有 soak 工具、测试与阈值仅作可选、非阻断诊断，不表示长跑已完成。候选 SLO、资源采样与其他适用首发门禁不变；四目标正式发行原长稳要求不变。
- **F10 记忆：L1 + 生命周期（N1）+ 检索质量（N2）+ 擦除（N4）+ 恢复/审计（N5）+ L2/L3 蒸馏（N3）已落地；控制面 UI/Host 端点（N6）也已交付——**React 记忆面板**（列出/检索/确认/忘记/擦除）+ 第 9 个记忆 RPC `memory.search`。** 已落地：事件日志上的可重建投影（`memory.py`；schema **v3**，`memories` 带 `layer` / `erased_at`）、10 个 `memory.*` 工具（**7 个模型可见**；`memory.propose` 升 2.0，可写 `user` / **本任务** `task` 作用域；`memory.remember` / `memory.promote` / `memory.erase` 是控制面专属、`exposed=False`，不进模型清单；`memory.edit` 一次审批保留旧版）、`memory://` 资源种类、C4 审批与 Receipt、CLI 控制面（`list|search|get|confirm|forget|remember|edit|promote|erase`，`--task-id` 可替旧 task 作用域签字且不绑它的目录）、`origin` 两档（`user_explicit` 由 `remember` 产生，`user_confirmed` 由人批准 `confirm` 升级）。**N2 检索质量**：作用域优先级（当前 task 0 / 本 Run 的 workspace 1 / user 2；优先级是**上下文**的函数、不是 `allowed` 的函数，同一上下文逐位可复现）；向量索引 `memory_index.py`（`EmbeddingProvider` 接缝 + `memory_vectors` 表）；`retrieve()` 取 **FTS 候选 ∪ 语义候选**（语义命中 FTS 没召回的行也并进来）并按 `bm25` 名次 + 语义名次做 RRF，输出带 `semantic` / `degraded` / `fused`，条目带 `score_kind`。**能力边界（自报）**：内置只有确定性 `HashEmbeddingProvider`（**无语义能力**，评测写明不是语义能力证明）；未配置 provider / 索引未完整构建 / 换 provider 未 `rebuild_vectors()` → 一律 FTS-only 并说明原因（`availability()` 校验 `built` 标记与向量覆盖，空索引不会装成可用）。生产入口：`build_stack(embedding_provider=...)` 或环境变量 `TIANSHU_EMBEDDING_PROVIDER=hash`，并用 `tianshu memory rebuild-vectors` 显式建一次索引（只装配 provider 不会构建向量；默认内置确定性 hash 基线；W6 已在 `embedding_openai.py` 接线受外发白名单门控的 OpenAI 兼容 embedding 端点，但真实外部端点检索分数未跑）。评测见 [memory-retrieval-eval.md](memory-retrieval-eval.md)（22 query / 25 记忆：FTS MRR 0.864 → hybrid 0.955，Recall@5 两者饱和为 1.000）。**CLI breaking**：`memory search --json` 从裸数组改成 `{items, semantic, degraded, fused}`（为携带能力标注）。**N3 L2/L3 蒸馏（只做确定性合成）**：`memory.consolidate`（1.0 / create / 模型可见，args 仅 `{scope, scope_id}`）把同作用域 `active` 的 L1 聚成 **L2 场景页**（token 重叠系数 0.34 聚簇 → 引用式拼装，每行带 `[memory_id@revision]`，`evidence` 每条 `{memory_id, revision, quote}` 且 `proof_count == len(evidence)`）与 **L3 偏好页**（user 作用域里人给的 `preference` 按 4 KiB 预算整行打包，放不下标"预算已满"）；两次合成**逐字节相同**、改一条源只动它那一块；结果写 `candidate`，人 `confirm` 才进召回；新页只取代同层的**候选**旧页（旧正文与它当时的 evidence 仍可按 id 查到），**人确认过的页不因合成退役**——合成是模型可见、免审批的工具，不能撤销人的召回授权。**新鲜度**：`source_refs` 带 `seq:<basis_seq>`，作用域里出现更新的 L1 后旧页 `stale=true` 并排在新鲜 L1 之后（降权不短路；`search()` 与 `retrieve()` 的三条路径共用同一排序键）；没有 `seq:` 的旧行 `stale` 是 **`None`**（不可判定，输出里省略该字段），不谎报新鲜。workspace / 他人 task 的 consolidate **无条件拒**，task 只能写自己。**N6 控制面端点（Host/IPC 一侧）**：daemon 的 `ControlPlane` 有 8 个记忆 RPC——`memory.list` / `memory.get` / `memory.remember` / `memory.confirm` / `memory.edit` / `memory.forget` / `memory.promote` / `memory.erase`；`host.py` 把浏览器 `POST /rpc` 原样代理给 daemon（有真 Host + token 回归用例）。**⚠ 这些端点不构成进程隔离。** 未隔离的 `local` 后端以同一用户身份执行命令，控制面仍与 CLI 共用 dispatcher。选用 W14 沙箱时，见 §2 的实测边界。跨本地用户的保护依赖系统权限，Windows 命名管道 ACL 待加固和验证。**记忆面板已交付**（`ui/src/components/MemoryPanel.tsx`）：列表/检索/确认/忘记/擦除，正文纯文本、不做乐观更新、冲突只刷新重读；端点这层仍然**不是**授权边界（见上两句）。CLI 与 RPC 共用同一份实现 `memory_control.py`（写操作永远走 dispatcher：intent/result + 一次性审批 + 执行侧二次核验），视图统一给六项判据（`scope` / `origin` / `revision` / `status` / `source_refs` / `proof_count`）；身份口径按事实写：**端点权限只在跨本地用户这层有意义**（POSIX 0700/0600；Windows ACL 未加固），**不新造身份令牌**；模型工具面与这条控制面是两条路（`memory.list` 不是工具，其余同名工具走主循环的 `allowed` 过滤），但**同 uid 的 `run_command` 仍可达**——见上一条。**评测**：真实用户任务 `RU-M1@1.0.0`——第一段人记一条写作偏好，第二段新任务召回并真的按偏好产出，同时不召回别的项目目录的诱饵记忆、产物里也不出现诱饵正文；忘记之后召回为空（失败路径 `RU-M1-F` 不进公开分母）。**两段都是脚本模型**，不是真实模型分数、也不是召回质量证明。**未实现**：LLM 蒸馏（生成式合成无法机械核对，明确延后）、L2/L3 vs L1 的检索质量评测（**不做"比 L1 更好用"的宣称**）、真实外部 embedding 端点的收益评测（接线已在 `embedding_openai.py`，W7 已落地 L2 页内确定性冲突裁决，生成式合成仍停在契约分析）、reranker。**崩溃恢复**：`recovery.recover_run` 有记忆专用分支，判据分三层——投影/事件层（与 `tool.result` 同事务，"有 intent 无 result"⇔这一层一定没生效）、正文 blob 层（提交前已落盘，恢复按引用计数清孤儿副本）、`memory.erase` 例外（先删文件、后提交，删文件不可逆：文件还在才算没生效，文件已没报 unknown，不冒充"没写"）。记忆写不阻断目录（没有 preimage），`dispatches` 行会一并推进到 `error`。**脱敏**：`tool.intent` 对带正文的记忆写（propose / remember / edit）只留 `content_sha256` + `content_bytes`；`dispatch_requests.args_payload` 在请求进入终态（成功 / 拒绝 / 执行中途未知）后脱敏，老库里的终态行打开时补扫，执行期仍保留完整参数（`args_hash` 绑定 + 执行器取参）。**残留**：停在 waiting/admitted 的请求仍带正文；payload 损坏的行不补扫（fail-open，反查口径是 `args_redacted=0`）；模型工具调用里的正文另存于 `model.result` / `run_turns`（replay 必需，物理擦除见 F10 §10）；擦除崩溃按文件是否还在分层：还在 ⇒ 确未生效，已没 ⇒ unknown 待人工核对；`assess_run` 对擦除一律保守判 unknown；正文目录不是 DB 旁默认值时，库调用方要传 `recover_run(memory=...)` 或 `blob_dir=...`（`memory` 优先；CLI/daemon 已传）；文件写请求的审计不受影响。`memory.erase` 会删正文副本与索引（引用计数为 0 才删，事件日志里的历史读快照保留）；workspace 的 propose 是**无条件**拒绝的（即便同一 Task 上后开的模型 Run 继承了控制面标记也一样），共享只能由人 `memory.promote` 进入。逐条状态见 [F10-memory-design.md](F10-memory-design.md) §13/§14；剩余施工规格见 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md)——那是**待实施**方案，不是已完成。
- **MCP 已有 stdio 与受限 HTTPS 客户端，真实第三方 server 仍未验证。**
  工具必须通过封闭 schema 准入，调用继续经过任务白名单、逐动作授权、
  固定版本和 Receipt。HTTPS 不等于任意 HTTP/SSE/OAuth/resources/prompts/sampling
  支持；以 `mcp_http.py` 的实际传输范围为准。本地主循环候选新增 Task/Run
  专属 Session、启动前 subject 准入、claim 后事务外发现及暂停/取消/收尾清理，
  不再准确地描述为“生命周期没有准入”。但准入接缝不等于平台隔离已完整：
  无隔离要求的人工受信任路径仍可能以当前用户权限运行；自动路径要求完整四边界，
  证据不足时在启动/发现前拒绝。远程 MCP 没有本机进程隔离证明，
  要求对应传输准入时也明确拒绝。结果按 `max_result_bytes` 截断记账，
  不宣称完整大结果内容引用或 MCP 独立预算维度。设置中的 MCP 面板仍为只读状态，
  不提供任意 server 编辑/安装。历史协议验收见 [MCP 记录](MCP-ACCEPTANCE-2026-11-12.md)，
  本候选接缝与未验范围见[主循环实施记录](changelog/2026-11-23-agent-loop-implementation.md)。
- **Agent 主循环已形成本地实现，P0-E 平台隔离仍未完成。** Mission、任务图、
  独立验收、Goal V2、有限授权和启动准入已集成；完全访问继续禁用。
  Windows 原生隔离及完整进程树监管由[工单 #224](https://github.com/lorek666/tianshu-agent/issues/224)跟踪。
  有限授权只能由人显式签发，绑定单个 Run，不继承到其他轮次或子任务。
  V1 授权投影全丢后可恢复余额，但原授权保守过期。最终门禁、PR/CI、
  真实付费模型 UI 与用户设备验收须分别记录，不能因源码已实现就称已发布。
- **子代理默认只读，委派深度限一层。** W10 的 `subtask.spawn` 2.0 可以显式声明 `write=true`。它只在父任务可写的绑定内授予文件写能力，每次写仍要人工批准。子任务的出网、MCP、执行命令、记忆写入与再委派保持禁用。子 Run 由同机 Worker 驱动，父 Run 等待结果时挂起作业。取消向下传播，已发生的费用照常结算。D3 与 W10 使用脚本模型验收，真实模型测试及 Windows 用户设备验证待补。见 [资源规格](resource-kinds.md) §8 与 [W10 规格](SPEC-W10-SUBAGENT-DEPTH-2026-11-14.md)。
- **联网研究只在本机夹具上闭环。** `network.fetch` 按任务白名单和逐次批准发出 GET。W9 校验每跳解析结果并连接已核验的地址，`network.fetch` 与 `read_content` 支持分页和二进制读取。搜索 API、真实站点研究和内容分级与保留期仍待验证或交付。见 [网络规格](network-g2.md) 与 [W9 交付记录](changelog/2026-11-18-w9-network-depth.md)。
- **真实模型没有接过 UI。** 演示与验收用 `scripted` 模型（不联网、不花钱），它证明的是进程语义，**不代表真实模型能力**；真实模型接入路径见 [cli.md](cli.md)「接真实模型」。CLI 与评测已跑过真实模型（2026-11-10，两家模型完整 20 条 × 3 次，见 [M4-verification.md](M4-verification.md) §7.1），但**还没有真实模型驱动过 UI**。
- 已知缺口：issue [#40](https://github.com/lorek666/tianshu-agent/issues/40)（审批被否决后 resume 会重新请求审批）**已修**（PR #48）——否决即终局，`SUITE_VERSION` 升 `m4.4`，工单已关闭（2026-11-14 对账：#53/#54/#40 均已销账）；issue [#27](https://github.com/lorek666/tianshu-agent/issues/27)（PRD §4.1 字段漂移）已收敛关闭。

## 2. F2 沙箱与执行策略：交付的是什么，不是什么

F2 提供可插拔后端、运行现场的实测门和审批前隔离视图。W14 新增 macOS Seatbelt 与适用 Linux 内核上的 Landlock，默认后端为 `local`。`TIANSHU_EXEC_BACKEND=auto` 选择当前平台可用的原生后端。后端不可用时返回 `PolicyUnsupported`。

`isolation_requires` 只接受现场探针确认已实施的边界。报告绑定后端、平台、
工作区、数据根、探针版本及超时；主循环候选还绑定具体程序、环境、配置和生命周期。
历史 macOS Seatbelt 探针不覆盖脱离进程组的后代；新的实际 subject 负控已证明
`process_tree=not_enforced`。自动命令与本地 MCP 要求完整四边界，因此该组合
在启动前被拒绝，不能以 killpg/leader 消失声称整树退出。Linux Landlock 的
TCP 规则需要 ABI 4 或以上；本候选仍须按实际对象取得新证据。
Windows 原生子进程沙箱与 Job Object 整树清理待交付。

各平台的判据、实测范围和已知限制见 [F2 规格](F2-sandbox-policy.md) 与 [W14 交付记录](changelog/2026-11-18-w14-dual-sandboxes.md)。

## 3. 当前仍未验证的（写成「通过」就是假话）

**当前仍未验证的**（写成「通过」就是假话）：**Windows 实机**（只有 CI `windows-latest`，命名管道的杀软/ACL/长路径差异与安装体验未覆盖）·
**真实模型驱动 UI**（CLI / `evaluate` 已用真实模型跑过：deepseek-v4.1-flash 18/20、glm-5.2 15/20，见 [M4-verification.md](M4-verification.md) §7.1；UI 仍未接过真实模型）· macOS x64 与 Windows ARM64 用户设备安装/升级 · Tauri 自动更新 · 同毫秒并发审批压测。
完整清单见 [M5-verification.md](M5-verification.md)。

## 4. M4 评测的未验证项

Windows 用户设备上的 `evaluate` 仍待复验。历史 macOS 实测中，`evaluate` 在建立工作根前拒绝 memfs；这是拒绝路径的证据，不是 memfs 可运行整套评测的证据。另用实际不响应的测试端点验证了 CLI `run` 的模型超时和回放；`evaluate` 的崩溃用例仍用注入硬退。详见 [M4 实测 §7](M4-verification.md) 与 [#55](https://github.com/lorek666/tianshu-agent/issues/55)。

| 套件 | 真实换模状态 | 证据范围 |
|---|---|---|
| `m4.4` | 已实跑两家（2026-11-10） | 两份报告都判为 `unsafe`；复核发现 P1/P2 判据把脚本模型的收尾形态误当作安全属性。完整结果与归因见 M4 实测 §7.1。 |
| `m4.5` | 复跑未完成 | 判据改为按目标作用域核对，旧报告不可比。该轮中转服务返回 `503 model_not_found / auto groups is not enabled`，见 M4 实测 §7.4。 |
| `m4.6` | 历史版本，真实模型对比待验 | 没有对应的真实模型报告；不能与后续判据直接混比。 |
| `m4.7` | 本地候选当前版本，真实模型对比待验 | `RU-D1@1.1.0` 增加严格绑定的只读 Mission 路由拒绝证据；脚本模型与负控不代表真实模型能力，见 M4 实测 §7.5。 |

## 5. 能力总表（逐条含边界与评测口径）

> README 只保留核心承诺。具体机制、限制与评测口径以本表和上文为准。

| | 能力 | 怎么做到 |
|---|---|---|
| 🔁 | **可重放** | 事件日志三方核对（seq / `messages_hash` / 产物哈希）；`replay` 只读历史，不调模型 |
| 🔐 | **执行侧授权** | `dispatcher` 是唯一派发入口：消费审批 → 登记 intent → 守卫内核验 → 才执行 |
| ↩️ | **写前快照 + 冲突安全撤销** | 默认可写就默认可撤销；撤销前核对内容哈希，被外部改过就拒绝 |
| 🧯 | **崩溃恢复** | `applied` / `none_applied` / `unknown` 三态**不合并**，只有 `none_applied` 可安全重试 |
| 🧵 | **跨进程租约 + fencing** | 防同用户双实例重复执行；旧 worker 不得覆盖新 owner |
| 🧠 | **上下文编译** | 纯函数、按**字节**预算裁历史；默认确定性裁剪，可选摘要先保存来源与版本，回放读取当时材料 |
| 🛠️ | **执行工具** | `run_command` 走 argv 不经 shell、默认全拒、需审批、有超时与输出截断 |
| 🌐 | **受控出网** | `network.fetch` 只支持 GET；任务级外发白名单和逐次人工批准。同源重定向，每跳校验解析结果并连接已核验的地址。响应上限 1 MiB，默认超时预算 10s（系统 DNS 解析不能异步打断）。返回有界摘要和支持分页的 `content://` 引用；回放不联网。 |
| 🧩 | **受控子代理** | `--max-subtasks N` 允许父任务申请 `subtask.spawn`，派发需人工批准。默认只读；W10 的 `write=true` 只保留父绑定允许的文件写入，实际写动作逐次审批。子任务不能出网、执行命令、调用 MCP、写记忆或再次委派。父 Run 等待时挂起，子任务收尾后唤醒；父取消或改目标会向下传递。结果以有界摘要和 `content://` 引用返回。本地候选 `RU-D1@1.1.0` 验证的是脚本模型下的默认只读委派，不是真实模型或跨机器调度。 |
| ⏰ | **定时任务（v1 最小调度器）** | `tianshu schedule create/list/show/enable/disable/remove`（interval 调度，**无 cron**）+ 控制面 `schedule.list/create/set_enabled/remove` + 桌面「设置 → 定时任务」面板 + daemon 到期自动触发：走与 `task.create`/`run.start` 完全相同的存储路径创建任务并入队 Run（事件流只出现既有事件族，**无新增 schedule 生命周期事件**），由既有 Worker 认领驱动；停机期间落下的到期**补发一次并从当前时间重排**；触发记账与建任务同事务，失败回滚并记 `last_error`。Run 内副作用照走执行侧授权（调度器不是授权输入）；平台以 CI 矩阵为准 |
| 🔌 | **受控 MCP** | stdio 与受限 HTTPS 客户端，用户配置、任务白名单、逐动作授权、固定 schema/描述及 Receipt。主循环候选新增 Run 专属生命周期与启动前准入；未证明完整隔离的自动路径拒绝，真实第三方 server 仍未验证，见 §1。 |
| 🧾 | **记忆看得见** | 控制面 `memory.search` + React 记忆面板：列出 / 检索 / 确认 / 忘记 / 擦除；条目带六项判据 + 语义能力标注（`semantic` / `degraded` / `fused`）+ `score`；正文纯文本、不做乐观更新、冲突只刷新重读。**评测**：`RU-M1@1.0.0` 跨任务召回（两段 Run；脚本模型） |
| 💰 | **成本可见** | 整数微美元记账；价格未知记 `None` 并单独计数，**不伪装成 0** |
| 🔌 | **模型可替换** | OpenAI-compatible 与 Anthropic 两套协议，换 provider 主循环零改动 |
| 🖥️ | **桌面闭环** | 关掉 UI / 刷新页面任务照跑；R1 源码候选的 Tauri 薄壳设计为关闭窗口只隐藏、托盘退出才停服务；重启后 Daemon 恢复扫描并续跑 |
| 🧭 | **审批 UI** | 覆盖 / 删除 / 执行命令在界面上点「允许 / 拒绝」，决定经 IPC 落库；**否决即终局**，不再重复问同一个动作 |
| 🪶 | **零第三方运行时依赖** | 纯 Python 标准库；dev 依赖只有 pytest + ruff；UI 构建产物已提交，运行时不需要 Node |

## 相关文档

- [PRD-v1.md](PRD-v1.md) §5 五道契约、§6 十一条红线（权威规格）
- [F2-ACCEPTANCE-2026-11-11.md](F2-ACCEPTANCE-2026-11-11.md) · [F2-sandbox-policy.md](F2-sandbox-policy.md)
- [M4-verification.md](M4-verification.md) · [M5-verification.md](M5-verification.md)
- [mcp-g2.md](mcp-g2.md) · [network-g2.md](network-g2.md) · [F10-memory-design.md](F10-memory-design.md)
- [KNOWLEDGE.md](KNOWLEDGE.md) §7 已知限制
