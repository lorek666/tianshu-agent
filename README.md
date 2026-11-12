<div align="center">

# 天枢 Agent · Tianshu Agent

**可回放、可审批、可撤销的桌面 Agent OS 内核**

让 AI 在你指定的目录里干活——但每一步副作用都留痕，越权要你点同意，
改坏了能撤销，崩溃了能判定「到底有没有生效」。

[![CI](https://github.com/lorek666/tianshu-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/lorek666/tianshu-agent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-0-success.svg)](pyproject.toml)

</div>

---

## 这是什么

面向**普通用户**的桌面 Agent OS：模型自选、在你指定的目录里读写文件、完成长任务。v1 不含图片与视频生成。

它把「AI 可能失控」当成一等工程问题来解，而不是靠 prompt 里写几句「请谨慎操作」：

- **事件溯源**：所有状态变更写进 append-only 事件日志，任何一次执行都能从零重放出相同结果。
- **执行侧授权**：授权只在执行侧判定，模型说什么都不算数；覆盖 / 删除 / 执行命令必须**人工审批**。
- **默认可撤销**：写前快照 + 可独立核对的凭据（Receipt），改坏了能撤销，撤销本身也走审批。
- **崩溃可恢复**：中断后能判定「已生效 / 未生效 / 未知」，**未知绝不盲目重放**。

> **当前形态：M5 桌面闭环已完成。** M0–M4 是命令行内核；M5 交付**三进程桌面闭环**——
> Daemon（常驻）、Host（桌面外壳）与浏览器里的 React 界面：关掉 UI 任务继续，重启后恢复。
> 但外壳是**系统浏览器**，不是安装包：没有 Tauri/WebView2、托盘、签名与自动更新
> （偏差逐条登记在 [docs/M5-desktop.md](docs/M5-desktop.md) §2）。
> 现阶段仍请把它当作**给开发者用的工具**——别把本文的愿景当成已交付的成品 App。

## 特性

| | 能力 | 怎么做到 |
|---|---|---|
| 🔁 | **可重放** | 事件日志三方核对（seq / `messages_hash` / 产物哈希）；`replay` 只读历史，不调模型 |
| 🔐 | **执行侧授权** | `dispatcher` 是唯一派发入口：消费审批 → 登记 intent → 守卫内核验 → 才执行 |
| ↩️ | **写前快照 + 冲突安全撤销** | 默认可写就默认可撤销；撤销前核对内容哈希，被外部改过就拒绝 |
| 🧯 | **崩溃恢复** | `applied` / `none_applied` / `unknown` 三态**不合并**，只有 `none_applied` 可安全重试 |
| 🧵 | **跨进程租约 + fencing** | 防同用户双实例重复执行；旧 worker 不得覆盖新 owner |
| 🧠 | **上下文编译** | 纯函数、按**字节**预算裁历史，丢弃只记账不摘要（摘要会毁掉可重放性） |
| 🛠️ | **执行工具** | `run_command` 走 argv 不经 shell、默认全拒、需审批、有超时与输出截断 |
| 🌐 | **受控出网** | `network.fetch` 只支持 GET；**任务级外发白名单**（未声明 = 一条包都不发）+ 逐次人工批准；同源重定向、1 MiB 响应上限、10s 硬超时、字节账本、有界摘要 + `content://` 引用；回放不联网 |
| 🧩 | **受控子代理** | 父任务声明 `--max-subtasks N` 后可用 `subtask.spawn` 派**只读**子任务（写文件/出网/exec/MCP/记忆写/再委派都是拒绝，不弹嵌套审批）；子 Run 由既有 Worker 驱动，父 Run 在等子任务时挂起作业、子任务收尾的同一事务里唤醒；父取消传播、父改目标 fence（`dispatch.parent_fenced`）；结果以 ≤4 KiB 摘要 + `content://` 引用回填，回放不重跑。**评测**：`RU-D1@1.0.0` 在脚本模型下真的派一个只读子任务 → 父任务逐字汇总并引用两个本地文档；写/外发被规则拒绝、子任务失败时父报告写「没有取得 / 输出不可用」。**没有跨机器调度、取消不退款、不是真实模型分数**（见下方诚实清单） |
| 🔌 | **受控 MCP** | MCP server 走 stdio 客户端；**用户配置（`TIANSHU_MCP_CONFIG`）× 任务级 server 白名单（`--allow-mcp-server`）× 逐次人工批准** 三道门；server 的 `inputSchema` 必须过封闭方言准入（认不出就拒该工具，不静默丢），工具版本绑定原始 schema + 归一 schema + 描述，改了就得重新批准；超时/断线按 `unknown` 结算；`tianshu mcp list` 如实列 server 状态、准入/被拒工具与原因。**未沙箱、未验证真实第三方 server**（见下方诚实清单） |
| 💰 | **成本可见** | 整数微美元记账；价格未知记 `None` 并单独计数，**不伪装成 0** |
| 🔌 | **模型可替换** | OpenAI-compatible 与 Anthropic 两套协议，换 provider 主循环零改动 |
| 🖥️ | **桌面闭环** | 关掉 UI / 刷新页面任务照跑；重启后 Daemon 恢复扫描并续跑（判据：关 UI 后任务继续、重启后恢复） |
| 🧭 | **审批 UI** | 覆盖 / 删除 / 执行命令在界面上点「允许 / 拒绝」，决定经 IPC 落库；**否决即终局**，不再重复问同一个动作 |
| 🪶 | **零第三方运行时依赖** | 纯 Python 标准库；dev 依赖只有 pytest + ruff；UI 构建产物已提交，运行时不需要 Node |

### 它不假装能做到的事

这份清单是刻意写在前面而不是埋在文末的——本仓库的规矩是**文档不许说假话**（见 [CONTRIBUTING.md](CONTRIBUTING.md)「文档改动怎么提交」）：

- **`Workspace` 是路径边界，不是安全边界。** `LocalExecBackend().capabilities.isolation == "none"`，**从未交付过物理沙箱**。它防的是模型越权与失控 worker，不防本机用户，也不防恶意代码。
- **密钥没有存进系统钥匙串。** v1 的 Keychain 后端**未实现**（零依赖下只能 ctypes 直调平台 C API，而我们没有平台能真验证），请求它会如实抛 `CredentialUnsupported`；已交付的是凭据接缝 + 全链路脱敏。
- **Windows 没有实机验证。** 三平台 CI 里 Windows 是真跑，但**不稳定**：命名管道上偶发握手失败（issue [#50](https://github.com/lorek666/tianshu-agent/issues/50)，根因未定位，有些不稳定的轮次会让作业撞上超时被取消）；跑在 `windows-latest`（Server 2022）上，并按平台跳过若干项（`memfs` 符号链接、标准库无 PTY、无进程组 / Unix 信号、无 POSIX 权限位语义、PID 存活探测仅 POSIX……），跳过原因逐条写在测试的跳过消息里，条数以 CI `-rs` 日志为准。
- **M5 交付的是进程闭环，不是可安装的 App。** 外壳是系统浏览器：没有 Tauri/WebView2、系统托盘、开机自启、安装包、签名、公证与自动更新。关掉浏览器标签只是**少一个订阅者**，Host 一直活到 `tianshu host stop`（或被杀）。
- **F10 记忆：L1 + 生命周期（N1）+ 检索质量（N2）+ 擦除（N4）+ 恢复/审计（N5）+ L2/L3 蒸馏（N3）已落地；只剩控制面 UI/Host 端点（N6）。** 已落地：事件日志上的可重建投影（`memory.py`；schema **v3**，`memories` 带 `layer` / `erased_at`）、10 个 `memory.*` 工具（**7 个模型可见**；`memory.propose` 升 2.0，可写 `user` / **本任务** `task` 作用域；`memory.remember` / `memory.promote` / `memory.erase` 是控制面专属、`exposed=False`，不进模型清单；`memory.edit` 一次审批保留旧版）、`memory://` 资源种类、C4 审批与 Receipt、CLI 控制面（`list|search|get|confirm|forget|remember|edit|promote|erase`，`--task-id` 可替旧 task 作用域签字且不绑它的目录）、`origin` 两档（`user_explicit` 由 `remember` 产生，`user_confirmed` 由人批准 `confirm` 升级）。**N2 检索质量**：作用域优先级（当前 task 0 / 本 Run 的 workspace 1 / user 2；优先级是**上下文**的函数、不是 `allowed` 的函数，同一上下文逐位可复现）；向量索引 `memory_index.py`（`EmbeddingProvider` 接缝 + `memory_vectors` 表）；`retrieve()` 取 **FTS 候选 ∪ 语义候选**（语义命中 FTS 没召回的行也并进来）并按 `bm25` 名次 + 语义名次做 RRF，输出带 `semantic` / `degraded` / `fused`，条目带 `score_kind`。**能力边界（自报）**：内置只有确定性 `HashEmbeddingProvider`（**无语义能力**，评测写明不是语义能力证明）；未配置 provider / 索引未完整构建 / 换 provider 未 `rebuild_vectors()` → 一律 FTS-only 并说明原因（`availability()` 校验 `built` 标记与向量覆盖，空索引不会装成可用）。生产入口：`build_stack(embedding_provider=...)` 或环境变量 `TIANSHU_EMBEDDING_PROVIDER=hash`，并用 `tianshu memory rebuild-vectors` 显式建一次索引（只装配 provider 不会构建向量；只接受 hash 基线，外部 provider 需数据外发许可，尚未接线）。评测见 [docs/memory-retrieval-eval.md](docs/memory-retrieval-eval.md)（22 query / 25 记忆：FTS MRR 0.864 → hybrid 0.955，Recall@5 两者饱和为 1.000）。**CLI breaking**：`memory search --json` 从裸数组改成 `{items, semantic, degraded, fused}`（为携带能力标注）。**N3 L2/L3 蒸馏（只做确定性合成）**：`memory.consolidate`（1.0 / create / 模型可见，args 仅 `{scope, scope_id}`）把同作用域 `active` 的 L1 聚成 **L2 场景页**（token 重叠系数 0.34 聚簇 → 引用式拼装，每行带 `[memory_id@revision]`，`evidence` 每条 `{memory_id, revision, quote}` 且 `proof_count == len(evidence)`）与 **L3 偏好页**（user 作用域里人给的 `preference` 按 4 KiB 预算整行打包，放不下标"预算已满"）；两次合成**逐字节相同**、改一条源只动它那一块；结果写 `candidate`，人 `confirm` 才进召回；新页只取代同层的**候选**旧页（旧正文与它当时的 evidence 仍可按 id 查到），**人确认过的页不因合成退役**——合成是模型可见、免审批的工具，不能撤销人的召回授权。**新鲜度**：`source_refs` 带 `seq:<basis_seq>`，作用域里出现更新的 L1 后旧页 `stale=true` 并排在新鲜 L1 之后（降权不短路；`search()` 与 `retrieve()` 的三条路径共用同一排序键）；没有 `seq:` 的旧行 `stale` 是 **`None`**（不可判定，输出里省略该字段），不谎报新鲜。workspace / 他人 task 的 consolidate **无条件拒**，task 只能写自己。**N6 控制面端点（Host/IPC 一侧）**：daemon 的 `ControlPlane` 有 8 个记忆 RPC——`memory.list` / `memory.get` / `memory.remember` / `memory.confirm` / `memory.edit` / `memory.forget` / `memory.promote` / `memory.erase`；`host.py` 把浏览器 `POST /rpc` 原样代理给 daemon（有真 Host + token 回归用例）。**⚠ 这些端点不是授权边界**：v1 没有进程级沙箱，`run_command` 能以同 uid 起子进程，同 uid 进程能读 IPC 凭据、跑 CLI 并自己落审批——所以它们交付的是"人机界面与 CLI 共用一套实现 + 写操作都走 dispatcher"，**不是**"模型不可能写记忆"；跨本地用户的保护只有 POSIX 的文件权限（Windows 命名管道 ACL 未加固、未验证）。**UI 目前没有记忆面板**，端点只是"可被调用"。CLI 与 RPC 共用同一份实现 `memory_control.py`（写操作永远走 dispatcher：intent/result + 一次性审批 + 执行侧二次核验），视图统一给六项判据（`scope` / `origin` / `revision` / `status` / `source_refs` / `proof_count`）；身份口径按事实写：**端点权限只在跨本地用户这层有意义**（POSIX 0700/0600；Windows ACL 未加固），**不新造身份令牌**；模型工具面与这条控制面是两条路（`memory.list` 不是工具，其余同名工具走主循环的 `allowed` 过滤），但**同 uid 的 `run_command` 仍可达**——见上一条。**未实现**：React 记忆面板（端点已具备，面板属 UI 工作流；别把"UI 能调"读成"UI 上已经能看见"）、`memory.search` 的 RPC、LLM 蒸馏（生成式合成无法机械核对，明确延后）、L2/L3 vs L1 的检索质量评测（**不做"比 L1 更好用"的宣称**）、冲突消解（矛盾的 L1 在页里并列、不裁决）、真 embedding provider、reranker。**崩溃恢复**：`recovery.recover_run` 有记忆专用分支，判据分三层——投影/事件层（与 `tool.result` 同事务，"有 intent 无 result"⇔这一层一定没生效）、正文 blob 层（提交前已落盘，恢复按引用计数清孤儿副本）、`memory.erase` 例外（先删文件、后提交，删文件不可逆：文件还在才算没生效，文件已没报 unknown，不冒充"没写"）。记忆写不阻断目录（没有 preimage），`dispatches` 行会一并推进到 `error`。**脱敏**：`tool.intent` 对带正文的记忆写（propose / remember / edit）只留 `content_sha256` + `content_bytes`；`dispatch_requests.args_payload` 在请求进入终态（成功 / 拒绝 / 执行中途未知）后脱敏，老库里的终态行打开时补扫，执行期仍保留完整参数（`args_hash` 绑定 + 执行器取参）。**残留**：停在 waiting/admitted 的请求仍带正文；payload 损坏的行不补扫（fail-open，反查口径是 `args_redacted=0`）；模型工具调用里的正文另存于 `model.result` / `run_turns`（replay 必需，物理擦除见 F10 §10）；擦除崩溃按文件是否还在分层：还在 ⇒ 确未生效，已没 ⇒ unknown 待人工核对；`assess_run` 对擦除一律保守判 unknown；正文目录不是 DB 旁默认值时，库调用方要传 `recover_run(memory=...)` 或 `blob_dir=...`（`memory` 优先；CLI/daemon 已传）；文件写请求的审计不受影响。`memory.erase` 会删正文副本与索引（引用计数为 0 才删，事件日志里的历史读快照保留）；workspace 的 propose 是**无条件**拒绝的（即便同一 Task 上后开的模型 Run 继承了控制面标记也一样），共享只能由人 `memory.promote` 进入。逐条状态见 [docs/F10-memory-design.md](docs/F10-memory-design.md) §13/§14；剩余施工规格见 [docs/SPEC-RESIDUALS-MEMORY.md](docs/SPEC-RESIDUALS-MEMORY.md)——那是**待实施**方案，不是已完成。
- **MCP 只做到「stdio + tools + 假 server」。** G2 MCP 阶段交付了 stdio 客户端、动态工具准入（server 的 `inputSchema` 必须过封闭方言，认不出就拒该工具）、派发链路（任务级 server 白名单 + 每次人工批准）、`receipt(backend="mcp")` 与 `tianshu mcp list` 诊断。**但**：真实第三方 server **没验证过**（本机 web provider 不可用，协议形状按 MCP 2025-06-18 的已知口径实现并与自写假 server 对齐——自测通过不是兼容性证据）；**没有沙箱**，server 以当前用户权限运行，三道门只防"模型自作主张"、不防恶意 server；MCP 进程**不经过** `exec_backends`（长连接 vs 一次性 `spawn`），所以任务的 `isolation_requires` 对 MCP **一律拒绝**（`c4.mcp_isolation_unproven`）；只做 stdio，HTTP/SSE / OAuth / resources / prompts / sampling 全没做；大结果**不落内容引用**（客户端按 `max_result_bytes` 截断并报字节）；没有 MCP 预算维度；UI 没有 MCP 面板。细节与逐条边界见 [docs/mcp-g2.md](docs/mcp-g2.md) 与 [docs/MCP-ACCEPTANCE-2026-11-12.md](docs/MCP-ACCEPTANCE-2026-11-12.md)。
- **子代理：D1 + D2 + D3，只读、单机、不退款。** `task.created.max_subtasks`（0–4）+ `subtasks` 实体与父子链 + `subtask.spawn/status/result/cancel` 四个工具 + 数量/局部限额 + `subtasks` 维度预算预占（D1）；子 Run 由**既有 Worker** 驱动、父 Run 在等子任务时挂起作业（Run 保持 running）、子任务收尾的同一事务里写行/写父 Run 事件/结算预算/唤醒父作业、父取消传播、父改目标 fence（`dispatch.parent_fenced`）、结果以 ≤4 KiB 摘要 + `content://` 引用回填、回放不重跑（D2）。**仍不成立**：子任务只能做纯读（写文件/出网/exec/MCP/记忆写/再委派都是拒绝，不弹嵌套审批）；深度固定 1、每父 Run 未完成上限 2、每根任务累计上限 4；子任务与父任务共用同一台机器/同一个 Worker 池（**没有跨机器调度**），`wake` 只保证可被重新认领、**不保证立刻被认领**；**取消不退款**（已发生费用照实计），读不到子 Run 最后一轮输出时 `summary` 为空并说明「输出不可用」，**不编造**；没有子任务 UI 页面（CLI `run status` 与事件里有）；**D3 评测只覆盖脚本模型**（父/子两套脚本，不是真实模型分数），公开委派任务只有 `RU-D1@1.0.0` 一条 + 测试内失败路径 `RU-D1-F`，不是所有委派场景；Windows 未验证。见 [resource-kinds.md](docs/resource-kinds.md) §8、[real-user-tasks.md](docs/real-user-tasks.md) 与 [SPEC-G2-DELEGATION-2026-11-11.md](docs/SPEC-G2-DELEGATION-2026-11-11.md)。
- **联网研究只在本机夹具上闭环。** 网络执行器（`network.fetch`）与任务级白名单已交付，§10.5 的联网研究任务现在真的跑——但来源是 loopback 夹具，不是公网站点：**没有检索 / 搜索 API**（没有搜索引擎与凭据）、**真实公网研究未验证**、DNS rebinding / 解析后二次校验未做、无速率限制、二进制正文没有文本分页读取、内容分级与保留期未做。别把"能发一个受控 GET"读成"公网研究能力已具备"（细节见 [docs/network-g2.md](docs/network-g2.md) §7）。
- **真实模型没有接过 UI。** 演示与验收用 `scripted` 模型（不联网、不花钱），它证明的是进程语义，**不代表真实模型能力**；真实模型路径见下文「接真实模型」。CLI 与评测已跑过真实模型（2026-11-10，两家模型完整 20 条 × 3 次，见 [docs/M4-verification.md](docs/M4-verification.md) §7.1），但**还没有真实模型驱动过 UI**。
- 已知缺口：issue [#40](https://github.com/lorek666/tianshu-agent/issues/40)（审批被否决后 resume 会重新请求审批）**已修**（PR #48）——否决即终局，`SUITE_VERSION` 升 `m4.4`，工单仍开着等确认；issue [#27](https://github.com/lorek666/tianshu-agent/issues/27)（PRD §4.1 字段漂移）已收敛关闭。

## 快速开始

### 环境要求

- Python **3.12**（`>=3.12,<3.13`，不支持 3.13+）
- [`uv`](https://docs.astral.sh/uv/)
- （只改前端才需要）Node + `pnpm`；**运行桌面界面不需要 Node**，`ui/dist` 已提交

### 安装

```bash
git clone https://github.com/lorek666/tianshu-agent.git
cd tianshu-agent
uv sync --python 3.12
```

### 跑测试与 lint

```bash
uv run pytest -o addopts="" -q   # macOS 本机实测：2078 passed, 7 skipped（G2 子代理 D3 分支含复审整改实测；CI 三平台为准）
uv run ruff check .              # 全绿（All checks passed!）
```

> 注意 `pyproject.toml` 里 `addopts = "-q"`，再叠一个 `-q` 会把汇总行吞掉，所以要 `-o addopts="" -q`。
> **Windows/CI 的数字以 CI 日志为准**：本机只有 macOS 的实测数字，Windows 只在 `windows-latest` runner 上跑，
> 没有实机验证（平台跳过项数也不要在本地猜）。

### 桌面闭环离线演示（M5，不调真实模型）

一条命令看完整闭环：建任务 → 停在审批 → **断开所有客户端（等价关掉 UI）** → 用新连接批准 → 任务自己跑完。

```bash
uv run python scripts/demo_m5.py --data-root /tmp/m5-demo --auto
```

它会打印界面地址、审批卡片（动作 / 目标 / 原因）与最终 JSON（`final_state`、`effect_state`、产物、事件序列），
结束时自己关掉 Host 与 Daemon。不加 `--auto` 则是可交互版本（Ctrl-C 退出）。

### 桌面三进程（M5）

```bash
# 1) 常驻守护进程：一般不用手动起——host 会自动拉起并监护它
uv run tianshu daemon --data-root /tmp/ts-data

# 2) 桌面外壳：单实例 + 监护 Daemon + 把 UI 的 HTTP/SSE 桥到本地 IPC
uv run tianshu host run --data-root /tmp/ts-data        # 打印 http://127.0.0.1:<随机端口>/
uv run tianshu host status --data-root /tmp/ts-data     # 在跑退出 0，没跑退出 1
uv run tianshu host stop --data-root /tmp/ts-data       # 让 Host 自己退出（它负责关 Daemon）
```

`host run` 的顺序是**先恢复、再对外服务**：拿 Host 单例锁 → 探测/拉起 Daemon（等端点就绪，上限 15s）→
Daemon 完成恢复扫描后才开 HTTP 服务 → 把访问 token 注入 `index.html` 的 `<meta name="tianshu-token">`。
页面与 `/rpc`、`/state`、`/events` 同源；数据端点都要求 `X-Tianshu-Token` 请求头
（**token 不进 URL**——URL 会进浏览器历史、shell 历史、聊天记录和截图）。`--open` 直接开浏览器，`--port 0`（默认）随机端口。

关浏览器标签 / 刷新页面**只是少一个订阅者**，任务继续跑；要停才用 `tianshu host stop`。
`stop` 是**先收掉 Daemon、再让 Host 退出**（收尾顺序本身是契约）：不会留下孤儿 Daemon 继续占着 socket、
继续接活。带 `--no-supervise` 可以不监护 Daemon 崩溃重启（调试用）。

### UI 的开发与构建（只改前端时需要）

`ui/dist` 已提交，`tianshu host` 直接加载它，运行时不需要 Node。改前端才需要：

```bash
pnpm --dir ui install
pnpm --dir ui run dev       # Vite 开发服务器（127.0.0.1:5199）
pnpm --dir ui run build     # 重建 ui/dist —— 构建产物要和源码一起提交
pnpm --dir ui run typecheck
pnpm --dir ui run test      # vitest
```

CI 的 `ui` job 跑 typecheck / test / build，并用 `git diff --exit-code -- ui/dist` 卡住「改了源码没提交产物」。
界面在没有后端时的静态预览（`?demo=1`）见 [ui/README.md](ui/README.md)。

### 命令行跑一次（不调真实模型）

用 scripted 模型跑通「阅读目录并生成摘要」的闭环：

```bash
uv run tianshu run \
  --workspace /tmp/ws --task "阅读并生成摘要" --out summary.md

# 回放校验这次执行（审计：不调模型）
uv run tianshu replay --run-id run_xxxx
```

### 接真实模型

两套线上协议，主循环一行都不用改（契约测试见 `tests/test_model_contract.py`）：

```bash
# 1) OpenAI-compatible：OpenAI / DeepSeek / Moonshot / vLLM / Ollama 都走这条
export TIANSHU_API_KEY=sk-...
export TIANSHU_BASE_URL=https://api.openai.com/v1   # 可选，支持任意兼容端点
uv run tianshu run --workspace /tmp/ws --task "..." \
  --out summary.md --model openai-compatible --model-name gpt-4o-mini

# 2) Anthropic Messages API：协议不同（system 顶层 / tool_use 内容块），所以是另一个 adapter
export ANTHROPIC_API_KEY=sk-ant-...
uv run tianshu run --workspace /tmp/ws --task "..." \
  --out summary.md --model openai-compatible --provider anthropic \
  --model-name claude-sonnet-4-5
```

`--provider` 不写时读 `TIANSHU_PROVIDER`，再默认 `openai`。
`--model-name` 对 anthropic **必须显式给**——不替你猜模型 id，猜错的表现是 404。

> 密钥只从环境变量读，永不落库、永不进事件日志。用过的 key 请自行作废轮换。

## 用法

**Task 是用户看到的那件事，Run 是一次执行**，两者生命周期分离。改目标会立刻 fence 掉旧 Run（待批请求作废，历史保留）。

| 子命令 | 作用 |
|---|---|
| `task` | 创建 / 查看 / 改目标或绑定 |
| `run` | 跑一次任务；或用 `start` / `status` / `pause` / `resume` / `cancel` 控制已有 Run |
| `approvals` | 审批队列：`list` / `approve` / `reject` / `revoke` |
| `undo` | 撤销某次 Run 的文件变更（不带 `--approve` 只预演） |
| `recover` | 核对并补全一次被打断的 Run |
| `worker` | 作业循环，**一次性 drain**：队空或撞上被占用的工作区就退出 |
| `daemon` | **常驻**守护进程：驱动任务、恢复扫描、IPC 控制面（不是 `worker`） |
| `host` | 桌面外壳：`run`（默认）/ `stop` / `status`，把 UI 桥到本地 IPC |
| `replay` | 从事件日志回放并校验（**审计，不调模型**） |
| `evaluate` | 在干净 fixture 上换模型跑固定 case（**换模，与 replay 是两件事**） |

```bash
# 建任务（目录可重复；--readonly 指定只读目录；不传目录 = 纯对话任务）
uv run tianshu task create --goal "整理季度笔记" --workspace /tmp/ws

# 开一次执行 / 看状态 / 暂停 / 恢复 / 取消
uv run tianshu run start  --task-id task_xxxx --out summary.md
uv run tianshu run status --run-id run_xxxx
uv run tianshu run pause  --run-id run_xxxx
uv run tianshu run resume --run-id run_xxxx
uv run tianshu run cancel --run-id run_xxxx

# 人工审批（覆盖、删除、执行命令都不会被自动执行）
uv run tianshu approvals list
uv run tianshu approvals approve appr_xxxx

# 撤销某次执行的文件变更（不带 --approve 时只预演）
uv run tianshu undo --run-id run_xxxx --workspace /tmp/ws

# 作业循环：认领排队中的 Run（崩溃、重启后接手；死掉的 owner 立刻可接管）
uv run tianshu worker --once    # 只跑一个作业
uv run tianshu worker           # 跑到队空 / 第一次撞上被占用的工作区为止

# 桌面：常驻守护进程 + 外壳（详见上方「桌面三进程」）
uv run tianshu daemon --data-root /tmp/ts-data
uv run tianshu host run --data-root /tmp/ts-data
```

批准的是「**那个动作**」，不是「那个进程」：换个进程 `run resume` 依然有效，
但执行仍要求当前 claim 与租约在手，且**改目标 / 改参数会让批准作废**。
**否决是终局**：同一 Run 内同一动作被拒绝后，后续派发直接以 `c4.approval_rejected` 拒绝，
不会反复弹同一张卡；模型改了参数或目标才是一次新的审批。

`replay` 与 `evaluate` 不是同一个东西：前者只读历史日志做审计，后者换模型重跑。
PRD §6 红线 10 明令不得混称——安全门槛是硬门槛，不能被其它分数抵消。

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 有结论且成功 |
| `1` | 失败 / 取消（`worker` 被接管或结算失败也是 1） |
| `2` | 用法或前置条件错误（含 `recover` 发现未核实效果） |
| `3` | 需要人决定（等审批 / 已暂停 / 待核查；`undo` 未带 `--approve` 的预演） |
| `4` | （仅 `evaluate`）安全门槛被击穿 = unsafe |

`host status` 在跑返回 `0`、没跑返回 `1`（便于脚本判断）；`host stop` 成功退出或本来没在跑都返回 `0`。

### 数据目录

**M5 起有两套落点，别混：**

| 用谁 | 默认数据根 | 覆盖方式 |
|---|---|---|
| `tianshu daemon` / `tianshu host` | 平台标准目录：macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`（缺省 `~/.local/share/AgentOS`） | `--data-root` 或环境变量 `TIANSHU_DATA_ROOT` |
| 内核子命令（`run` / `task` / `approvals` / `undo` / …） | 当前工作目录下的 `.tianshu/events.db`（已被 `.gitignore` 排除） | `--db` |

数据根里是：`events.db`（唯一事实源）、`artifacts/blobs`、`artifacts/snapshots`、
`run/`（IPC 端点、`ipc.key`、Host/Daemon 锁）与 `logs/daemon.log`（Host 重定向的 Daemon 日志）。
测试与 CI 一律显式指到临时目录，**不会写你的用户主目录**。

## 架构概览

**唯一事实源是事件日志**：SQLite WAL、append-only（触发器禁止 `UPDATE` / `DELETE`），Task / Run / Approval / Grant / Lease 都是同库事务更新的**可重建投影**。状态变更必须同事务写事件，否则回放无法解释「为什么这个 Run 被 fence 了」。

**M5 的三进程**：UI（浏览器里的 React）→ loopback HTTP + SSE → Host（Python：单实例、监护 Daemon、注入 token）→ IPC（macOS/Linux = AF_UNIX，Windows = 命名管道，`multiprocessing.connection` + `authkey`）→ Daemon（任务 / 事件 / 审批 / 驱动线程）。**UI 断开只减少订阅者**；Daemon 重启时先做恢复扫描（有 intent 无 result 的调用交给既有 `recover_run`、重新排队 `queued`/`parked`），再对外服务。

五道版本化契约 + 十一条红线约束全局：**C1** 模型 I/O、**C2** 工具、**C3** 事件（41 个类型的封闭注册表，写入侧拒未登记类型）、**C4** 权限/审批（43 个 rule id 的封闭注册表：36 条拒绝原因 + 5 条非拒绝判定 + 2 条保留未使用）、**C5** 评测（`SUITE_VERSION = m4.4`）。

```
src/tianshu/          49 个 .py + eval/ 子包（下面按职责分组，47 个实义模块，余两个是 __init__.py / __main__.py）
├─ 事实源与契约 ─────────────────────────────────────────────
│  events.py        SQLite WAL append-only 事件日志 + 可组合事务
│  canonical.py     RFC 8785 (JCS) 规范化 + 独立标准向量
│  rules.py         C4 rule id 封闭注册表（43 个：36 拒绝 + 5 判定 + 2 保留未使用）
│  errors.py        区分「模型想错了」与「执行侧跳过了校验」（后者是 bug）
├─ 路径与磁盘 ───────────────────────────────────────────────
│  workspace.py     路径规范化 / 资源身份（st_dev:st_ino）/ 0..n 目录绑定
│  pathkeys.py      路径键：大小写 / Unicode 规范化与祖先判断
│  fsutil.py        原子写入：临时文件 → fsync → os.replace → fsync 目录
│  sandbox.py       存储后端接缝：LocalBackend（真实盘）/ MemBackend（进程内，不是沙箱）
├─ 授权与执行 ───────────────────────────────────────────────
│  permissions.py   C4 动作分类 / 审批 / 一次性 grant
│  dispatcher.py    唯一派发入口：消费审批 + 登记 intent + 守卫内核验后执行
│  execpolicy.py    F2 执行侧命令策略（声明式、默认全拒、deny 优先）
│  exec_backends.py F2 执行后端注册表（未实现/未知一律 unsupported，绝不回落 local）
│  isolation.py     F2 四边界实测探针 + 隔离门 + 受信任执行视图
│  exec.py          F1 执行原语：argv 模式（非 shell）、超时、截断、进程组
│  tools.py         受限工具 + FileExecutor（自己回库核验票据，不信调用方）
│  guard.py         工作区阻断检查（未知副作用不得继续写）
├─ 副作用可撤销 ─────────────────────────────────────────────
│  snapshot.py      写前快照与冲突安全撤销
│  receipt.py       操作凭据（能独立核对，不必信调用方）
│  undo.py          撤销：把一次 Run 的写入还原或删除（本身也走审批/租约/receipt）
│  recovery.py      中断核查：applied / none_applied / unknown 三者不合并
│  crashpoint.py    故障注入点（os._exit 硬退，仅 TIANSHU_FAILPOINT 生效）
├─ 任务、租约、作业 ─────────────────────────────────────────
│  tasks.py         Task / Run / RunCursor 状态机，fencing，控制请求
│  leases.py        跨进程资源租约 + 本机执行守卫（POSIX / Windows）
│  jobs.py          作业认领与 claim_epoch（旧 worker 不得覆盖新 owner）
├─ 模型层 ───────────────────────────────────────────────────
│  model.py         C1 接缝：adapter 只做双向翻译，不改写内部形状
│  capability.py    F9a 能力声明与准入（只用于路由，不用于授权）
│  credentials.py   F12 凭据分域查找 + 全链路脱敏（Keychain 后端 v1 未实现）
│  pricing.py       F13 成本账：整数微美元，未知价格记 None 并单独计数
│  context.py       F5 上下文编译：纯函数、按字节预算、丢弃只记账不摘要
├─ 驱动 ─────────────────────────────────────────────────────
│  loop.py          E1 主循环（持久游标、暂停 / 取消 / 恢复）+ 栈装配
│  worker.py        作业循环：认领排队中的 Run（一次性 drain）
│  replay.py        回放校验：seq / messages_hash / 产物哈希三方核对
│  cli.py           命令行入口（含 daemon / host 两个常驻入口）
├─ 桌面三进程（M5）─────────────────────────────────────────
│  ipc.py           本地 IPC：AF_UNIX / 命名管道、版本区间协商、推送游标、断线语义
│  daemon.py        常驻守护进程：锁、恢复扫描、请求线程、驱动线程、优雅退出
│  control.py       RPC 方法 → 既有库函数的映射（不自己写文件、不重判策略）
│  host.py          外壳：单实例、监护 Daemon、UI 的 HTTP/SSE 桥、token 注入
└─ eval/            M4 评测：20 条 case、干净 fixture、可比分数与失败归因
```

平台标准数据目录、单实例判据、断线语义与恢复顺序的完整规格见 [docs/M5-desktop.md](docs/M5-desktop.md)；
产品规格见 [docs/PRD-v1.md](docs/PRD-v1.md)（§5 契约、§6 红线）与 [docs/TECH-ARCH.md](docs/TECH-ARCH.md)。

UI 在 `ui/`：Vite + React + TS，构建产物 `ui/dist` 已提交（Host 直接加载，运行时不需要 Node）。

## 项目状态与路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** 骨架 | EventLog + E1 闭环 + StopReason/Limits | ✅ 完成 |
| **M1** 副作用 | intent/result + Receipt + 写前快照 + 恢复 | ✅ 完成 |
| **M2** 权限与任务 | C4 审批状态机（写 / 删分级）+ Task/Run + 作业表租约 | ✅ 完成 |
| **M3** 可替换 | 第二个 chat provider + 第二个沙箱后端 | ✅ 完成 |
| **M4** 可评估 | 20 条 case + `replay`/`evaluate` 分离 | ✅ 完成 |
| **M5** 桌面闭环 | Daemon/Host/UI 三进程打通 + 权限审批 UI | ✅ 完成（PR #49） |

**M5 实测**：全量 **1316 passed, 6 skipped**（macOS 本机，2026-11-11 收尾轮 `43d04ef`）；三进程真进程链路跑通——
`tianshu host run` → `tianshu host stop` 后 Host 与 Daemon 都退出（`ipc.probe == False`，不留孤儿），
`scripts/demo_m5.py --auto` 真 daemon + 真 `ui/dist` + 真 Host 桥输出 `final_state=succeeded` / `effect_state=applied`
（中途断开所有客户端后用新连接批准）。完整判据、未验证项与真进程截图见 [docs/M5-verification.md](docs/M5-verification.md)。

**F2（沙箱与 policy）已收口（PR #108 / #111）**：交付的是「后端可插拔 + 实测门 +
受信任执行**审批前**可见」，**不是物理沙箱**。`TIANSHU_EXEC_BACKEND` 选不到的后端
（`app_container` / `container` / `seatbelt` / `landlock`）一律 `PolicyUnsupported`，
不会回落本地；策略里的 `isolation_requires` 只认 `isolation.py` 的真实子进程探针
（`unknown` 与 `not_enforced` 一样拒绝），报告绑定后端/平台/workspace/data_root/
probe 版本/timeout；未隔离执行在审批卡片、时间线、`tool.result.output.isolation`
里如实标「受信任执行」。**本机实测四边界只有 `process_tree` enforced**，
所以任何非空 `isolation_requires` 都会 `unsupported`；Windows L2（Job Object）
与 L3/L4 后端**未实现**。判据、负控与未验证项见
[docs/F2-ACCEPTANCE-2026-11-11.md](docs/F2-ACCEPTANCE-2026-11-11.md) 与
[docs/F2-sandbox-policy.md](docs/F2-sandbox-policy.md)。

**M5 明确未验证的**（写成「通过」就是假话）：**Windows 实机**（只有 CI `windows-latest`，命名管道的杀软/ACL/长路径差异未覆盖）·
**真实模型驱动 UI**（CLI / `evaluate` 已用真实模型跑过：deepseek-v4.1-flash 18/20、glm-5.2 15/20，见 [docs/M4-verification.md](docs/M4-verification.md) §7.1；UI 仍未接过真实模型）· Tauri/WebView2 外壳与托盘（M5 没做）· 同毫秒并发审批压测。
完整清单见 [docs/M5-verification.md](docs/M5-verification.md)。

**M4 实测**：`SUITE_VERSION = m4.4`，20 条 case —— 正常成功 8 / 工具错误 4 / 超时恢复 3 / 恶意输入 3 / 审批边界 2，
每类有下限守卫；安全门槛一票否决，不能被其它分数抵消。
M5 同批修掉了 issue #40（**否决即终局**）：`approval="reject"` 的判据改为 `approval.veto_terminates`，
并保留「无重复审批请求」的机械判据。

**M4 明确未验证的**（详见 [docs/M4-verification.md](docs/M4-verification.md)）：
真实换模（两个真实 provider 的分数对比）未跑 · Windows 上的 `evaluate` 未单独验证 · memfs 后端未覆盖 · 真超时未覆盖。

### CI 门禁

GitHub Actions。Python 矩阵三个平台 × Python 3.12：`ubuntu-latest` / `macos-latest` / `windows-latest`，检查名形如 `test (macos-latest) · py3.12`；
另有一个前端 job `ui · typecheck / test / build`（ubuntu）：typecheck → vitest → build → `git diff --exit-code -- ui/dist`（产物漂移门禁）。

步骤与本地 `CONTRIBUTING.md` §5「提交前检查」的命令逐字相同（本地绿而 CI 红只可能是环境差异），另加两条只在 CI 才可能的门禁：

- `uv lock --check` —— 锁文件漂移。`uv sync --frozen` **不是**漂移门禁（`pyproject.toml` 变了它照样 exit 0，实测如此）。
- `uv run --frozen` —— 防止顺手改写锁文件绕过上面那条。

`main` 已开分支保护：必须走 PR、`strict`（要求分支与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推 / 禁删除。

> ⚠️ **目前只有 `ubuntu-latest` 和 `macos-latest` 两个 Python 检查是「必过」。Windows 会跑、会报红，但没有设成必过状态**——
> 也就是说 Windows 红了不会自动挡住合并。而 `ci.yml` 里自己写着「Windows 是**主打平台**」。
> 这是已知的门禁缺口，建议在 Windows 稳定一段时间后把它也设为 required（见 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) 已知限制）。

## 开发

```bash
uv sync --python 3.12
uv run pytest -o addopts="" -q   # macOS 本机实测：2078 passed, 7 skipped（G2 子代理 D3 分支含复审整改实测；CI 三平台为准）
uv run ruff check .              # lint
```

约定（完整版见 [CONTRIBUTING.md](CONTRIBUTING.md)）：

- Python **3.12**，`uv`，**零第三方运行时依赖**；新增依赖须四目标可用（`mac-arm64` / `mac-x64` / `win-x64` / `win-arm64`，见 TECH-ARCH §8.3）。
- UI 用 Vite + React + TS；`ui/dist` 是**要提交的产物**（Host 直接加载），改了前端必须重建并一起提交，CI 有漂移门禁。
- 事件日志 = SQLite WAL 单提交点，事务统一走 `EventLog.transaction()`（可组合，不嵌套 `BEGIN`）。
- 授权只在执行侧判定。
- 契约变更单独提 `docs(contract):`，body 写「原方案 → 新方案 → 为什么 → 影响哪些代码」。
- 高危改动（授权 / 租约 / 并发写 / 事件日志 / 计费）**须独立复审并附可复现证据**。

> **AI 辅助开发**：每次开发前必读 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)（项目知识库），结束后更新它。
> 强制指令见 [AGENTS.md](AGENTS.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/PRD-v1.md](docs/PRD-v1.md) | **产品需求与架构**：范围、五道契约、十一条红线、里程碑（唯一权威版本） |
| [docs/TECH-ARCH.md](docs/TECH-ARCH.md) | **技术架构**：选型、进程模型、打包、平台差异 |
| [docs/M5-desktop.md](docs/M5-desktop.md) | **M5 桌面闭环**：三进程拓扑、IPC 契约、恢复语义、偏差登记（§2）、已知限制（§10） |
| [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) | **项目知识库**：当前状态、关键决策、踩坑教训、已知限制 |
| [docs/F10-memory-design.md](docs/F10-memory-design.md) | **F10 记忆设计（v1.1）**：记忆作为事件日志的可重建投影、L0–L3 分层、作用域与装备、写入分级审批；**§6.1 资源种类（`memory://`）**、**§13 契约增量的实际状态**、**§14 判据↔真实测试** |
| [docs/SPEC-RESIDUALS-MEMORY.md](docs/SPEC-RESIDUALS-MEMORY.md) | **剩余记忆工作量施工规格**：§13 #18–#27 逐条现状（代码事实）、N1–N6 六个工作流的判据与负控口径（N1/N2/N3/N4/N5 已落地，含 §2.3/§3.4/§4.4/§5.4/§6.4 落地记录；**只剩 N6**）、契约增量清单、明确不做 |
| [docs/F2-ACCEPTANCE-2026-11-11.md](docs/F2-ACCEPTANCE-2026-11-11.md) | **F2 沙箱与 policy 验收报告**：判据↔测试、负控 A1–A11/B1–B13、数字对账，以及**未验证 / 不宣称**的 12 项（无物理沙箱、Windows Job Object 未实现、L3/L4 未实现、探针覆盖有限、审批视图是装配时视图、Windows CI 基线红等） |
| [docs/MEMORY-ACCEPTANCE-2026-11-11.md](docs/MEMORY-ACCEPTANCE-2026-11-11.md) | **F10 记忆 v1.2 交付验收报告**：逐条判据↔测试、负控总表、数字对账，以及**未验证 / 不宣称**的 10 项（真 embedding provider、L2/L3 评测、LLM 蒸馏、Windows 实机与 ACL、控制面非隔离边界、React 面板、`memory.search` RPC、跨进程断电、真实模型 UI、冲突消解） |
| [AGENTS.md](AGENTS.md) | AI 编码助手的强制指令（读什么、更什么、哪些不许做） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 分支、提交规范、独立复审、什么该推什么不该推 |
| [docs/REVIEW-PRA.md](docs/REVIEW-PRA.md) | PR #7 独立复审报告（「自审盲区」教训的来源） |
| `docs/M*-verification.md` | M0–M5 各里程碑的判据与验收证据 |
| [docs/REFS.md](docs/REFS.md) | 参考仓库与固定 commit |
| [docs/archive/](docs/archive/) | 历史研究记录，**已被 PRD-v1 取代，勿作为实现依据** |

**改动任何契约前先读 PRD §5、§6。** 红线与功能需求冲突时，以红线为准。

## 贡献

欢迎 issue 与 PR。请先读 [CONTRIBUTING.md](CONTRIBUTING.md)：契约变更需单独 PR，高危改动需独立复审并附可复现证据，必过的 CI 检查（ubuntu + macOS）绿了才能合。

## 许可证

[Apache License 2.0](LICENSE)（含专利授权）。