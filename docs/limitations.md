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
- **旧材料空树复核不是原子快照。** `7b4d80b` 增加最终逐目录真实内容复核，堵住已下降目录内迟到材料被延迟 stat 隐藏的缺口；保留身份/子项闭包及错误 fail-closed，观察成本有界。它仍不能排除某目录最后一次观察后的同 uid 修改，不是物理隔离。自定义材料路径的 #204 修复与残留边界见下一条；原 CI 红、新本地全量和仍待最终托管 CI 的范围见[修复证据](changelog/2026-11-20-integration-ci-repair.md)。
- **材料路径守卫已修复 #204 的已复现缺口，但不是物理隔离。** 私有 `.<db>.materials` / `.memory.materials` 不再被祖先中的 `artifacts`、`blobs`、`memory_blobs` 误判为标准域；标准域要求 raw/canonical 同根同域，私有 target 不得逃逸 canonical 容器。快照、撤销 journal、事件/内容/记忆 blob 与各 spill 路径均校验具体文件 target，既有文件或 leaf symlink 不再跳过守卫。检查发生在 lease 获取时；同 uid 随后替换目录链接的 TOCTOU 仍是已披露的协作式边界，需 fd-relative/openat 才能物理封闭。默认 `events.db` / AgentOS 与自定义 `--db` 的上述组合均已覆盖，但不因此宣称任意同 uid 文件竞争可防。跟踪 [#204](https://github.com/lorek666/tianshu-agent/issues/204)，验证见[材料守卫修复](changelog/2026-11-24-material-write-guard.md)。
- **受管恢复写入依赖目录 `fsync`。** Windows 可生成只读计划并复核符合绑定的隔离批次。有效写入会返回 `durability_unavailable`。Windows 的离线 GC 遇到任一文件候选时拒绝整批。当前仅支持受管恢复到备份记录的原路径，物理掉电和用户设备验收仍待补齐。详见[备份与恢复指南](backup-restore.md)和 [#200](https://github.com/lorek666/tianshu-agent/issues/200)。
- **发行草稿入口已接入，首发验收未完成。** Tauri 薄壳管理窗口、托盘、单实例和 Python Host 生命周期；锁定 Python runtime、打包校验、macOS 安装/卸载脚本和四目标 `release.yml` 已随 PR #175 / #186 合入。workflow 的 `macos_arm64_preview` 只生成单目标草稿；按 [PRD §11.1](PRD-v1.md) 首发仅准备 `macos-arm64`，主干工作流实跑、下载字节校验与 Mac 真机安装/升级/回退待验。没有 OS 签名、公证、自动更新；未验证的 macOS 版本和其他平台不在首发支持范围。
- **长时稳定性未验证。** 首次 `macos-arm64` 限定预览按 [PRD §10.6](PRD-v1.md) 取消强制 ≥10h soak，不设替代时长；既有 soak 工具、测试与阈值仅作可选、非阻断诊断，不表示长跑已完成。候选 SLO、资源采样与其他适用首发门禁不变；四目标正式发行原长稳要求不变。
- **F10 记忆：L1 + 生命周期（N1）+ 检索质量（N2）+ 擦除（N4）+ 恢复/审计（N5）+ L2/L3 蒸馏（N3）已落地；控制面 UI/Host 端点（N6）也已交付——**React 记忆面板**（列出/检索/确认/忘记/擦除）+ 第 9 个记忆 RPC `memory.search`。** 已落地：事件日志上的可重建投影（`memory.py`；schema **v3**，`memories` 带 `layer` / `erased_at`）、10 个 `memory.*` 工具（**7 个模型可见**；`memory.propose` 升 2.0，可写 `user` / **本任务** `task` 作用域；`memory.remember` / `memory.promote` / `memory.erase` 是控制面专属、`exposed=False`，不进模型清单；`memory.edit` 一次审批保留旧版）、`memory://` 资源种类、C4 审批与 Receipt、CLI 控制面（`list|search|get|confirm|forget|remember|edit|promote|erase`，`--task-id` 可替旧 task 作用域签字且不绑它的目录）、`origin` 两档（`user_explicit` 由 `remember` 产生，`user_confirmed` 由人批准 `confirm` 升级）。**N2 检索质量**：作用域优先级（当前 task 0 / 本 Run 的 workspace 1 / user 2；优先级是**上下文**的函数、不是 `allowed` 的函数，同一上下文逐位可复现）；向量索引 `memory_index.py`（`EmbeddingProvider` 接缝 + `memory_vectors` 表）；`retrieve()` 取 **FTS 候选 ∪ 语义候选**（语义命中 FTS 没召回的行也并进来）并按 `bm25` 名次 + 语义名次做 RRF，输出带 `semantic` / `degraded` / `fused`，条目带 `score_kind`。**能力边界（自报）**：内置只有确定性 `HashEmbeddingProvider`（**无语义能力**，评测写明不是语义能力证明）；未配置 provider / 索引未完整构建 / 换 provider 未 `rebuild_vectors()` → 一律 FTS-only 并说明原因（`availability()` 校验 `built` 标记与向量覆盖，空索引不会装成可用）。生产入口：`build_stack(embedding_provider=...)` 或环境变量 `TIANSHU_EMBEDDING_PROVIDER=hash`，并用 `tianshu memory rebuild-vectors` 显式建一次索引（只装配 provider 不会构建向量；默认内置确定性 hash 基线；W6 已在 `embedding_openai.py` 接线受外发白名单门控的 OpenAI 兼容 embedding 端点，但真实外部端点检索分数未跑）。评测见 [memory-retrieval-eval.md](memory-retrieval-eval.md)（22 query / 25 记忆：FTS MRR 0.864 → hybrid 0.955，Recall@5 两者饱和为 1.000）。**CLI breaking**：`memory search --json` 从裸数组改成 `{items, semantic, degraded, fused}`（为携带能力标注）。**N3 L2/L3 蒸馏（只做确定性合成）**：`memory.consolidate`（1.0 / create / 模型可见，args 仅 `{scope, scope_id}`）把同作用域 `active` 的 L1 聚成 **L2 场景页**（token 重叠系数 0.34 聚簇 → 引用式拼装，每行带 `[memory_id@revision]`，`evidence` 每条 `{memory_id, revision, quote}` 且 `proof_count == len(evidence)`）与 **L3 偏好页**（user 作用域里人给的 `preference` 按 4 KiB 预算整行打包，放不下标"预算已满"）；两次合成**逐字节相同**、改一条源只动它那一块；结果写 `candidate`，人 `confirm` 才进召回；新页只取代同层的**候选**旧页（旧正文与它当时的 evidence 仍可按 id 查到），**人确认过的页不因合成退役**——合成是模型可见、免审批的工具，不能撤销人的召回授权。**新鲜度**：`source_refs` 带 `seq:<basis_seq>`，作用域里出现更新的 L1 后旧页 `stale=true` 并排在新鲜 L1 之后（降权不短路；`search()` 与 `retrieve()` 的三条路径共用同一排序键）；没有 `seq:` 的旧行 `stale` 是 **`None`**（不可判定，输出里省略该字段），不谎报新鲜。workspace / 他人 task 的 consolidate **无条件拒**，task 只能写自己。**N6 控制面端点（Host/IPC 一侧）**：daemon 的 `ControlPlane` 有 8 个记忆 RPC——`memory.list` / `memory.get` / `memory.remember` / `memory.confirm` / `memory.edit` / `memory.forget` / `memory.promote` / `memory.erase`；`host.py` 把浏览器 `POST /rpc` 原样代理给 daemon（有真 Host + token 回归用例）。**⚠ 这些端点不构成进程隔离。** 未隔离的 `local` 后端以同一用户身份执行命令，控制面仍与 CLI 共用 dispatcher。选用 W14 沙箱时，见 §2 的实测边界。跨本地用户的保护依赖系统权限，Windows 命名管道 ACL 待加固和验证。**记忆面板已交付**（`ui/src/components/MemoryPanel.tsx`）：列表/检索/确认/忘记/擦除，正文纯文本、不做乐观更新、冲突只刷新重读；端点这层仍然**不是**授权边界（见上两句）。CLI 与 RPC 共用同一份实现 `memory_control.py`（写操作永远走 dispatcher：intent/result + 一次性审批 + 执行侧二次核验），视图统一给六项判据（`scope` / `origin` / `revision` / `status` / `source_refs` / `proof_count`）；身份口径按事实写：**端点权限只在跨本地用户这层有意义**（POSIX 0700/0600；Windows ACL 未加固），**不新造身份令牌**；模型工具面与这条控制面是两条路（`memory.list` 不是工具，其余同名工具走主循环的 `allowed` 过滤），但**同 uid 的 `run_command` 仍可达**——见上一条。**评测**：真实用户任务 `RU-M1@1.0.0`——第一段人记一条写作偏好，第二段新任务召回并真的按偏好产出，同时不召回别的项目目录的诱饵记忆、产物里也不出现诱饵正文；忘记之后召回为空（失败路径 `RU-M1-F` 不进公开分母）。**两段都是脚本模型**，不是真实模型分数、也不是召回质量证明。**未实现**：LLM 蒸馏（生成式合成无法机械核对，明确延后）、L2/L3 vs L1 的检索质量评测（**不做"比 L1 更好用"的宣称**）、真实外部 embedding 端点的收益评测（接线已在 `embedding_openai.py`，W7 已落地 L2 页内确定性冲突裁决，生成式合成仍停在契约分析）、reranker。**崩溃恢复**：`recovery.recover_run` 有记忆专用分支，判据分三层——投影/事件层（与 `tool.result` 同事务，"有 intent 无 result"⇔这一层一定没生效）、正文 blob 层（提交前已落盘，恢复按引用计数清孤儿副本）、`memory.erase` 例外（先删文件、后提交，删文件不可逆：文件还在才算没生效，文件已没报 unknown，不冒充"没写"）。记忆写不阻断目录（没有 preimage），`dispatches` 行会一并推进到 `error`。**脱敏**：`tool.intent` 对带正文的记忆写（propose / remember / edit）只留 `content_sha256` + `content_bytes`；`dispatch_requests.args_payload` 在请求进入终态（成功 / 拒绝 / 执行中途未知）后脱敏，老库里的终态行打开时补扫，执行期仍保留完整参数（`args_hash` 绑定 + 执行器取参）。**残留**：停在 waiting/admitted 的请求仍带正文；payload 损坏的行不补扫（fail-open，反查口径是 `args_redacted=0`）；模型工具调用里的正文另存于 `model.result` / `run_turns`（replay 必需，物理擦除见 F10 §10）；擦除崩溃按文件是否还在分层：还在 ⇒ 确未生效，已没 ⇒ unknown 待人工核对；`assess_run` 对擦除一律保守判 unknown；正文目录不是 DB 旁默认值时，库调用方要传 `recover_run(memory=...)` 或 `blob_dir=...`（`memory` 优先；CLI/daemon 已传）；文件写请求的审计不受影响。`memory.erase` 会删正文副本与索引（引用计数为 0 才删，事件日志里的历史读快照保留）；workspace 的 propose 是**无条件**拒绝的（即便同一 Task 上后开的模型 Run 继承了控制面标记也一样），共享只能由人 `memory.promote` 进入。逐条状态见 [F10-memory-design.md](F10-memory-design.md) §13/§14；剩余施工规格见 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md)——那是**待实施**方案，不是已完成。
- **MCP 只做到「stdio + tools + 假 server」。** G2 MCP 阶段交付了 stdio 客户端、动态工具准入（server 的 `inputSchema` 必须过封闭方言，认不出就拒该工具）、派发链路（任务级 server 白名单 + 每次人工批准）、`receipt(backend="mcp")` 与 `tianshu mcp list` 诊断。**但**：真实第三方 server **没验证过**（本机 web provider 不可用，协议形状按 MCP 2025-06-18 的已知口径实现并与自写假 server 对齐——自测通过不是兼容性证据）；**没有沙箱**，server 以当前用户权限运行，三道门只防"模型自作主张"、不防恶意 server；MCP 进程**不经过** `exec_backends`（长连接 vs 一次性 `spawn`），所以任务的 `isolation_requires` 对 MCP **一律拒绝**（`c4.mcp_isolation_unproven`）；只做 stdio，HTTP/SSE / OAuth / resources / prompts / sampling 全没做；大结果**不落内容引用**（客户端按 `max_result_bytes` 截断并报字节）；没有 MCP 预算维度；UI 仅提供只读 MCP 服务与工具准入状态查看（设置 → MCP 服务，`mcp.list`），不支持在界面里编辑或启动未配置的 server。细节与逐条边界见 [mcp-g2.md](mcp-g2.md) 与 [MCP-ACCEPTANCE-2026-11-12.md](MCP-ACCEPTANCE-2026-11-12.md)。
- **子代理默认只读，委派深度限一层。** W10 的 `subtask.spawn` 2.0 可以显式声明 `write=true`。它只在父任务可写的绑定内授予文件写能力，每次写仍要人工批准。子任务的出网、MCP、执行命令、记忆写入与再委派保持禁用。子 Run 由同机 Worker 驱动，父 Run 等待结果时挂起作业。取消向下传播，已发生的费用照常结算。D3 与 W10 使用脚本模型验收，真实模型测试及 Windows 用户设备验证待补。见 [资源规格](resource-kinds.md) §8 与 [W10 规格](SPEC-W10-SUBAGENT-DEPTH-2026-11-14.md)。
- **联网研究只在本机夹具上闭环。** `network.fetch` 按任务白名单和逐次批准发出 GET。W9 校验每跳解析结果并连接已核验的地址，`network.fetch` 与 `read_content` 支持分页和二进制读取。搜索 API、真实站点研究和内容分级与保留期仍待验证或交付。见 [网络规格](network-g2.md) 与 [W9 交付记录](changelog/2026-11-18-w9-network-depth.md)。
- **真实模型没有接过 UI。** 演示与验收用 `scripted` 模型（不联网、不花钱），它证明的是进程语义，**不代表真实模型能力**；真实模型接入路径见 [cli.md](cli.md)「接真实模型」。CLI 与评测已跑过真实模型（2026-11-10，两家模型完整 20 条 × 3 次，见 [M4-verification.md](M4-verification.md) §7.1），但**还没有真实模型驱动过 UI**。
- 已知缺口：issue [#40](https://github.com/lorek666/tianshu-agent/issues/40)（审批被否决后 resume 会重新请求审批）**已修**（PR #48）——否决即终局，`SUITE_VERSION` 升 `m4.4`，工单已关闭（2026-11-14 对账：#53/#54/#40 均已销账）；issue [#27](https://github.com/lorek666/tianshu-agent/issues/27)（PRD §4.1 字段漂移）已收敛关闭。

## 2. F2 沙箱与执行策略：交付的是什么，不是什么

F2 提供可插拔后端、运行现场的实测门和审批前隔离视图。W14 新增 macOS Seatbelt 与适用 Linux 内核上的 Landlock，默认后端为 `local`。`TIANSHU_EXEC_BACKEND=auto` 选择当前平台可用的原生后端。后端不可用时返回 `PolicyUnsupported`。

`isolation_requires` 只接受现场探针确认已实施的边界。报告绑定后端、平台、工作区、数据根、探针版本及超时。macOS Seatbelt 的实测包含文件、网络、凭据和进程树。Linux Landlock 的 TCP 规则需要 ABI 4 或以上。Windows 原生子进程沙箱与 Job Object 整树清理待交付。

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
| `m4.6` | 当前套件，真实模型对比待验 | 没有对应的真实模型报告。脚本模型基线不代表真实模型能力。 |

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
| 🧩 | **受控子代理** | `--max-subtasks N` 允许父任务申请 `subtask.spawn`，派发需人工批准。默认只读；W10 的 `write=true` 只保留父绑定允许的文件写入，实际写动作逐次审批。子任务不能出网、执行命令、调用 MCP、写记忆或再次委派。父 Run 等待时挂起，子任务收尾后唤醒；父取消或改目标会向下传递。结果以有界摘要和 `content://` 引用返回。`RU-D1@1.0.0` 验证的是脚本模型下的默认只读委派，不是真实模型或跨机器调度。 |
| ⏰ | **定时任务（v1 最小调度器）** | `tianshu schedule create/list/show/enable/disable/remove`（interval 调度，**无 cron**）+ 控制面 `schedule.list/create/set_enabled/remove` + 桌面「设置 → 定时任务」面板 + daemon 到期自动触发：走与 `task.create`/`run.start` 完全相同的存储路径创建任务并入队 Run（事件流只出现既有事件族，**无新增 schedule 生命周期事件**），由既有 Worker 认领驱动；停机期间落下的到期**补发一次并从当前时间重排**；触发记账与建任务同事务，失败回滚并记 `last_error`。Run 内副作用照走执行侧授权（调度器不是授权输入）；平台以 CI 矩阵为准 |
| 🔌 | **受控 MCP** | MCP server 走 stdio 客户端；**用户配置（`TIANSHU_MCP_CONFIG`）× 任务级 server 白名单（`--allow-mcp-server`）× 逐次人工批准** 三道门；server 的 `inputSchema` 必须过封闭方言准入（认不出就拒该工具，不静默丢），工具版本绑定原始 schema + 归一 schema + 描述，改了就得重新批准；超时/断线按 `unknown` 结算；`tianshu mcp list` 如实列 server 状态、准入/被拒工具与原因。**未沙箱、未验证真实第三方 server**（见下方诚实清单） |
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
