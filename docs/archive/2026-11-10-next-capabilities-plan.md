> **历史提案归档：2026-11-10。** 下文保留当日草案原文，供追溯研究与实施拆解使用；它不是现行契约，也不是交付或验收证明。文中的“尚未实现”“最新协议”及候选依赖均属于当日提案口径，不应直接当作当前事实。现行依据与后续方案见[索引](../NEXT-CAPABILITIES-PLAN.md)。

---

# 网络 → MCP → 记忆 → 子代理：增量实施方案

日期：2026-11-10。状态：**供决策与实施拆解的提案，尚未实现，尚未变更现行契约**。

本方案承接现有 PRD，不重做产品定义。按四段能力交付；每段交付可运行功能、UI 入口、契约测试和验收证据。用户无需手工配置开发环境，由开发 Agent 按工单实施。本文不宣称现有测试、平台或线上服务已经验证通过。

## 0. 当前技术能力修订（2026-11-10）

用户补充要求“采用最新技术，考虑未来五年”。因此，本方案不再把下面的最小可运行范围当作阶段的最终能力：简单 fetch、FTS-only、只会 spawn 的子代理都是基线。事件、授权、预算、来源与恢复内核保留，能力层按以下目标增量交付；不承诺某个组件五年保持领先。

| 能力 | 修订目标 | 工程前置条件 |
|---|---|---|
| 网络 | 多搜索 provider 接缝 + 成熟正文提取 + HTTP 快速路径 + browser.read 渲染路径；支持有来源的迭代研究 | 浏览器子资源同样受出网边界约束；托管研究与本地 fetch 分开标注证据可见性 |
| 工具/MCP | 新旧 MCP 协议 adapter + 动态工具检索/按需加载 + 有界工具调用示例 | 按 SDK 实际支持版本测试；发现工具不等于获得调用授权 |
| 记忆 | SQLite 事实源 + FTS/BM25 与向量混合召回 + RRF 融合 + 可选模型 reranker | 索引可重建；embedding/reranker 版本和选中结果可回放；作用域过滤在检索前执行 |
| 子代理 | 持久化任务依赖图 + 按需并行 + 结构化证据结果；外部协作预留 A2A adapter | 本地依然复用 Job/Run；A2A 不是内部调度器，也不自动传递授权 |
| 批量调用 | 逐步加入程序化工具调用，代码执行批处理/筛选后只返回需要的结果 | 必须有经验证的执行隔离、无直接文件/网络权限、每次工具调用仍过 broker；当前无物理沙箱时禁用 |

### 最新不等于无条件装 latest

- 本轮直接读取官方页：MCP latest 指向 **2026-07-28**。该版移除 initialize/initialized 与协议级 HTTP session，改用逐请求版本/能力和 `server/discover`；旧版必须留独立兼容路径。Tasks 已移到官方扩展，不能当所有 server 的核心能力。
- 当前 Playwright 官方要求 **macOS 14+**，与项目既定 **macOS 13+** 有冲突。浏览器后端必须做 feature gate：较新系统可选当前 Playwright；macOS 13 的 browser.read 要另验一个仍有安全维护且兼容的后端或用户明确授权的远端后端。在验明前标 unsupported，普通 HTTP fetch 仍可用。不能为了“最新”静默改最低系统要求，也不能长期冻结有漏洞的旧浏览器。
- 建议把依赖 ADR 扩展为“核心少依赖、集成采用维护中的 SDK/库”：HTTPX/AnyIO 是网络 adapter 候选，成熟正文提取器、官方 MCP SDK/完整 schema 校验器、检索后端均按模块锁定。它们不会自动解决 SSRF、取消或权限；四目标和最低 OS 仍必须单独验收。Python 版本升级是另一张兼容性工单，不因追新顺手升级。
- LanceDB Embedded 可作为向量/混合检索候选，官方文档已提供向量 + FTS + RRF；本轮未验证其 macOS 13/Windows arm64 制品，不宣布已选定或支持。保存 `EmbeddingProvider / VectorIndex / Reranker` 三个接缝，候选不满足平台门禁时换实现，不删掉混合检索目标。
- 动态工具发现采用内部 `tools.search → tools.describe → call`，基础工具常驻，其余按任务载入固定版本。可用模型厂商原生能力加速，但通用模型也能走内部工具发现；不把 Claude 专有字段焊进 Core。
- 程序化调用先支持经过审定的有界批处理计划；允许模型任意生成可执行代码之前必须交付真正的隔离后端。不能靠 AST 黑名单或另一个 Python 进程声称已沙箱化。
- 不立即引入 GraphRAG 或外部多代理集群。知识图谱要以跨实体/跨时间问题评测证明增益，外部 A2A 在存在外部 Agent 对接需求时实现。升级验证指标包含任务成功率、来源准确性、Recall@k、费用、p95 延迟和安全回归，不用“模型觉得不错”替代。

### 修订交付范围

保留第 7 节的基线工单，但必须增加：PR04 后的浏览器/迭代研究工单（受平台门禁）；PR06–07 的新旧协议与工具按需发现；PR09 的混合检索和同义改写评测；PR11 的持久化依赖图。程序化执行与外部 A2A 分别是通过隔离门禁后/有外部对接需求后的后续工单，不与首批交付混称完成。

每季度以固定评测集比较搜索、embedding、reranker 和模型候选；协议更新由版本兼容矩阵和契约测试准入，安全更新及时处理。先离线/影子评测，再切默认，可回退旧 adapter；业务事件与原始材料不随索引重建而丢失。

## 1. 本轮核对的现状与接缝

读取基线：工作树位于 `fix/m0m5-audit`，读取时无未提交改动；未切分支、未运行产品测试。实施时从届时已验收的主线另开分支，勿在复审修复分支混入功能。

| 代码事实 | 实施影响 |
|---|---|
| `tools.py` 的 `ToolSpec` 与 `AdmittedCall` 包含文件路径、workspace 字段 | 网络、MCP、记忆不能伪装成一个文件路径；需要按资源种类解析和执行 |
| `dispatcher.py` 说明及 `loop.py::_exec_read` 明确让只读文件绕过派发；读取需要绑定目录 | 新工具都进入公共准入路径；无 workspace 的联网任务必须合法 |
| `tools.py::validate_args` 只校验少量顶层 schema 关键字 | 不能直接接受任意 MCP schema 并声称已验证；不支持的关键字必须拒绝准入 |
| `loop.py` 按 `_Progress` / cursor 记录单 Run usage、费用，重试以历史 usage 投影 | 已有记账不等于跨 Run 并发预算预占；接子代理前必须实现共享事务账本 |
| `context.py::compile_context` 是确定性纯函数，按字节预算编译工具往返 | 检索、网页抽取、模型摘要在编译前完成并持久化；纯编译函数不联网、不调用模型 |
| `jobs.py` 已有 claim_epoch、作业唯一认领、waiting 与接管语义 | 子代理复用 Job/Run；扩充父子关系与依赖，不再建一套调度器 |
| `pyproject.toml` 运行时依赖为空，AGENTS 明确零依赖 | 引入官方 MCP SDK / 完整 schema 校验器需要先修改依赖决策 |

不复述旧里程碑完成度，也不把知识库里的历史测试数字当成本轮测试结果。现行 PRD §5/§6、CONTRIBUTING 的独立复审与契约独立 PR 要求继续有效。

## 2. 一条公共工具路径

```text
Agent Loop / Child Run
  → TaskToolView（本任务可见、固定版本的工具清单）
  → ToolRegistry（工具定义 + adapter + 资源解析器）
  → Admission（参数、任务状态、权限、预算）
  → 已提交的 intent
  → Executor（file / network / mcp / memory / delegation）
  → result + usage + 适用的 Receipt
  → EventLog + ArtifactStore
```

实现只扩展现有 dispatcher，不并行建立第二套授权系统。现有文件后端、快照与凭据保持各自职责；所有工具统一准入不意味着每个读操作都要求人工批准或文件写租约。

### 契约增量

- `ResourceDescriptor`：带 `kind` 的联合类型，例如 file、web_origin、mcp_tool、memory_scope、child_job。各类有自己的规范化、冲突和阻断规则；URL 不能套 `Workspace.resolve`。
- `ToolBinding`：固定 `tool_id + version + schema_hash + descriptor_hash + adapter_id`。descriptor_hash 还覆盖描述、server 身份及本地风险策略；更改后失效旧审批，不热替换运行中的工具。
- `CallContext`：task/run/call/attempt、deadline、取消信号、policy_ref、budget_account_id、artifact_store。模型只能给业务参数，不能自填身份、授权票据或账本账户。
- 扩充工具元数据，分别表达本地/外部状态变化、数据外发、计费和重试策略；不要用单个 `pure` 标签同时表达四件事。
- `ArtifactRef`：来源、内容哈希、媒体类型、字节数、提取器版本、采集时间、截断状态。新增有界的 `artifact.read` 工具，按引用分页读取；服务端核验该引用属于任务可见范围。
- 外部观察附 `source_id` 和 `trust=external`；网页、MCP、记忆、子任务结果保留来源，不能自动提升为用户授权或系统指令。
- `replay` 只读已保存结果，禁止重新搜索、抓网页、查询当前 MCP 清单或重新检索最新记忆。`evaluate` 可以用固定夹具另开运行。

封闭集同步修改代码、schemas、PRD；有历史数据迁移和兼容测试。工具内部 ID 可带命名空间，provider adapter 负责映射为其支持的工具名，双向映射必须无碰撞、可回放。

### 共享额度

新增 `BudgetLedger`，账户挂根 Run/委派组，原子预占后才能发出计费请求。每次真实 attempt 独立登记，settle 按唯一 attempt ID 去重。

- 限额维度：模型 token、可计价费用、搜索次数、网络请求数、累计下载字节、截止时间、子任务数量；不同单位分账，不互相假装等价。
- 每个子任务另有局部上限，实际执行同时消耗根账户；汇总子任务报告时不再重复计算已经结算的子调用。
- 使用未知/计价未知不记零。断流且可能计费的请求保留未决预占，过了超时不能自动释放成可用余额。
- provider 未提供可验证成本/用量上界时，只承诺调用次数和输出限制等可强制边界；严格货币/总 token 模式拒绝无法保证上界的请求。
- 网络等待期间不持有 SQLite 写事务、全局执行锁或不相关目录的独占锁。复用 claim 心跳；取消后不再发新请求。

## 3. A：网络是一等工具

### 范围与选择

原生注册 `web.search` 与 `web.fetch`，与文件工具同级，不依赖 MCP server，也不调用 shell/curl 拼命令。

首版使用 Python 标准库实现受控 HTTP(S) 客户端和有界 HTMLParser 提取器；先覆盖普通 HTML、纯文本、JSON。首个搜索 provider 建议 Brave Search，通过 `SearchProvider` 接缝调用其官方 HTTP 接口，不依赖搜索 SDK。接入时固定接口与计价版本，用夹具覆盖；未配置搜索凭据时显示“搜索未配置”，fetch 仍可用，不能返回空结果冒充联网成功。提供 FixtureSearchProvider 做离线测试。搜索服务的注册或付费不在本计划内自动执行。

暂不做登录态页面、浏览器执行 JavaScript、无限抓站、附件/PDF/OCR、网页代操作。动态页面明确报告正文不可获得，不编造已阅读。

### 对模型的接口

```text
web.search(query, limit=5, domains?)
  → results[{source_id, title, url, snippet}], provider, fetched_at, usage

web.fetch(url, max_bytes?)
  → source_id, final_url, status, title, content_ref,
    excerpt, content_hash, fetched_at, extractor_version, truncated

artifact.read(ref, offset, limit)
  → text, next_offset, eof, provenance
```

模型参数只能收紧服务端限制。搜索摘要是发现线索；未 fetch 的条目在报告里标为“仅搜索摘要”，不能标成已核验正文。引用须能够定位到已保存内容及段落/文本区间，不能只返回一个可能以后变化的链接。

### 默认限制（提案值）

- 公网 HTTP/HTTPS 文档获取，GET；缺省只允许 80/443。私网、loopback、link-local、云元数据等特殊地址拒绝；本地测试通过测试注入端口运行，不开放生产策略后门。
- 每 Run 最多 5 次搜索、20 次抓取，单任务网络并发 2；所有重试占用请求额度。
- 连接 5 秒、总请求截止 20 秒、最多 3 次重定向；每跳重新授权。
- 单响应传输字节与解压后字节各限 2 MiB，Run 累计各限 20 MiB；工具内联正文最多 8 KiB，余下用 ArtifactRef。拒绝不支持的编码，限制解析深度与输出长度。
- 无凭据、无 Cookie 的公网请求作为默认网页模式。认证抓取需要独立 provider 配置及授权，不接受模型随意传 Authorization/Cookie。

### 必须实现的执行边界

1. 地址解析校验与实际连接绑定：解析所有候选 IP，筛查禁止范围，实际只连接本次核验的地址；HTTPS 继续用原主机名做 SNI 和证书验证。不能先检查 DNS、再让 HTTP 库自行解析一次。IPv6、IPv4 映射地址、混合地址和每次重连均覆盖。
2. 不隐式继承环境代理；受控代理模式要有独立策略，否则拒绝。跨 origin 重定向不带凭据，不自动连接未经核验的新地址。
3. 网络 GET 也会泄露查询数据、产生远端日志和费用，不能等同于本地纯读。Task 的联网许可应声明“发送给哪些服务、允许哪些来源数据外发”。首版保守规则：公共任务与公开网页可在许可范围内自动使用；一旦引入私有文件/私有记忆，任何后续联网动作均需预览实际 URL/query 并明确批准，或预设允许该数据域外发的策略。不能只靠秘密字符串扫描。
4. 外部文本中的“执行命令、读取密钥、把资料发到某网址”不构成授权；结构化来源与执行侧约束必须存在。UI 展示正文/摘要使用纯文本，不直接执行远端 HTML。
5. 断网、429、超时、正文过大分开呈现。只有允许重试的读取请求有界重试，受总 deadline 与账本约束；取消不声称撤销已送出的请求。
6. 新网络错误只影响相应请求/资源，不用文件 recovery 的默认逻辑阻断整个 workspace。

### 出口验收

- 无目录任务完成“搜索主题 → 读取两个结果 → 带正文来源答复”；输出报告需要另走文件授权。
- 公网重定向到内网、DNS 重绑定、特殊 IPv6、超大压缩响应、慢响应均被执行端拦截。
- 读取私有文件后尝试把内容拼进 query/URL，没有外发许可则不发包；抓取文本不能自己扩大权限。
- 断网后 replay 成功且测试明确断言零外部请求；原始结果及提取器版本可追溯。
- 已用完搜索额度后再次调用在请求发送前被拒绝；未配置 provider 与真正零搜索结果不混同。

## 4. B：MCP adapter

### 依赖决策与接入顺序

建议采纳“**核心零运行时依赖，MCP 集成可选依赖**”：使用官方 Python MCP SDK 和可维护的 JSON Schema 校验器，SDK 类型不得越过 adapter。通过独立 ADR/契约 PR 调整当前零依赖规则，锁定版本/哈希，验证 mac-arm64、mac-x64、win-x64、win-arm64；最终包预装选定依赖，用户机器不跑 pip。四目标构建下载测试与原生 import/启动测试是不同证据，不能互相代替。

该建议尚未修改现行规则。若官方 SDK 及依赖无法通过四目标门禁，该集成阻塞并报告原因，不静默降级架构支持、不实现一个缺安全语义的“全兼容”替身。

分两批：先 stdio，本地连接自建夹具与一个可信只读 server；再 Streamable HTTP。协议版本使用 SDK 实际支持的显式版本集合：2026-07-28 走逐请求元信息和 discovery 选择，旧版本才走 initialize 握手。记录确切选择及兼容路径，不把内部 C1–C5 的区间协商直接复制到 MCP。新协议变更独立验证，SDK 未支持时不能伪称已接通。

### 接入行为

- ServerConfig 绑定 server_id、transport、固定启动 argv/cwd 或 endpoint、最小 env、凭据引用、本地工具允许表、启动与调用期限。
- 新版：`server/discover（可选的预先版本选择）→ tools/list（含分页）→ 本地审核与注册 → tools/call`，每个请求携带版本/能力；旧版兼容路径使用 `initialize`。结果进入现有 ToolResult 和 ArtifactRef，`input_required` 不能当完成结果。流断开后的协议请求 ID 更新不等于业务操作可安全重试；写调用仍按执行结果核查规则处理。
- 内部身份为 `mcp.<server_id>.<tool_name>`，对模型使用稳定合法 alias。两台 server 的同名工具不冲突。
- 工具清单变化只形成新候选描述，重审后才启用；Run 使用清单快照。schema、描述、endpoint 或 server 身份变化使旧审批失效。
- 校验器在执行侧校验完整声明；不支持的 dialect/关键字显式拒绝。外部 `$ref` 不自动联网解析。
- 保留 JSON-RPC 错误、工具 `isError`、连接错误的区别；结构化结果按 outputSchema 校验。图片/音频及 resource link 首版仅保存元数据和引用，不自动打开 URL、读取本地文件或上传给模型。
- MCP 注解不是能力授权。首版只启用本地明确允许、信任来源及凭据范围已核验的读取型工具。MCP server 自称只读不能作为证明。
- 任意外部写工具默认禁用；接通前必须有专用权限映射、执行凭据与结果核查策略。无法确定效果的结果仍为 unknown，禁止盲重试；不能承诺任意 server 都满足可撤销/崩溃可核实红线。
- 不接 sampling、elicitation、prompts 自动注入及 roots 自动扩权；未支持能力不声明。首版无自动插件市场下载，不运行未审核的 `npx -y ...`。

### 进程与安全边界

stdio server 经用户明确配置后作为受信任本地程序启动。MCP 协议和 roots 不是 OS 沙箱；当前 LocalExecBackend 没有物理隔离，不能宣称第三方 server 的自行文件访问/出网受本产品 NetworkPolicy 约束。只给 server 最小凭据、最小 env；强隔离是额外后端能力，不在本版假装实现。

stdout 专供协议，stderr 有界收集并脱敏；确保持续消费，不能被写满管道卡住。调用期限、取消与进程树回收绑定 Daemon 生命周期；取消通知并不证明远端操作没发生。HTTP server 的鉴权 token 只绑定指定 origin，不透传给下游，也不跟随跨域重定向。HTTP 传输经受控网络层或经过同等验证的 connector；SDK 默认客户端若做不到，HTTP 阶段不得上线。

### 出口验收

真子进程夹具覆盖握手、分页、同名工具、畸形 schema、清单变化、断线、超时、取消及 stderr 大量输出。断线后的写调用不被自动重放；未知工具/工具描述变更无自动启用；server 退出不留下卡死的 Job。两个传输后端使用同一套 C2 契约断言。历史 replay 不需要启动 MCP server。

## 5. C：本地记忆

### 第一版选型与接口

SQLite 作为事实源，记忆正文通过受控内容存储引用。首个可运行 PR 做 FTS5 与显式记忆，随后同阶段加入向量召回 + RRF；可选 reranker 需用相关性/延迟/费用评测决定是否默认启用。FTS-only 是降级模式，不是本阶段的最终能力。知识图谱与自动全量对话抽取不进入本批次。

四目标启动探测 FTS5 支持；中文不能默认用空格分词即可：基线用确定性中文双字切分辅助索引、英文词/标识符索引，保存 tokenizer_version，用中文夹具验证召回。向量索引是可重建投影，保留正文、原始来源及 embedding 模型/维数；更换 embedding 时重建索引，不混查不同向量空间。默认不把私有记忆发往外部 embedding/reranker 服务；云端模式须有数据外发许可。

```text
Memory {
  memory_id, revision, scope: user|workspace|task, scope_id,
  kind: preference|fact|decision, content_ref,
  source_refs[], origin: user_explicit|user_confirmed|inferred,
  status: candidate|active|superseded|forgotten,
  created_at, expires_at?, supersedes?
}

memory.search(query, scope?, limit=5)
memory.get(memory_id)
memory.propose(content, scope, source_refs)
```

控制面提供新增/确认/编辑/忘记。模型的 propose 只生成候选；明确“记住这件事”的用户动作可直接确认，摘要或网页内容不能自动成为长期用户事实。scope 与 scope_id 由当前身份和任务绑定核验，模型不能读取另一个 workspace 的私有记忆。任务状态、grant、审批决定不存为可覆盖的普通记忆。

### 检索与上下文

先过滤 scope、有效期和状态，再排序和裁剪；优先当前用户明示、当前任务/工作区证据。冲突事实并列呈现并保留来源，不能由新抓取网页静默覆盖用户偏好。

每轮按需要检索一次，保存实际选中的 memory_id/revision、来源及文本快照引用；然后调用纯 `compile_context`。首版默认最多 5 条、合计 4 KiB，受总上下文预算限制。不把全库放进 system prompt；检索文本是带出处的上下文资料。共享/发送到远端模型前沿用该任务的数据外发许可。

### 忘记与可回放

“忘记”追加 tombstone 并在同事务从活动投影移除，失效搜索/向量/检索缓存；新请求和重建索引都不能复活它。事件只保存引用与审计元数据，避免把新记忆正文重复写进不可删除日志。

UI 区分“停止未来召回”与“物理擦除历史数据”。前者本阶段交付；后者需要追踪历史模型消息、来源事件、备份、共享 blob 引用及法定/用户保留策略，单开契约工单。不能因为删了 FTS 行就声称所有历史内容已擦除。历史 replay 使用当时的快照，若用户将来选择彻底清除导致快照缺失，应明确报历史证据不可用而不是重取最新记忆。

### 出口验收

记住“报告用中文、先列结论”后，新任务能检索到；不同 workspace 不串记忆；中文无空格文本和中英混合标识符的词面召回用固定样例量化；同义改写召回作为后续向量检索的比较基线，不声称 FTS 已具备语义理解；候选不能未经确认自动激活。编辑保留修订，忘记后新任务、重建索引、应用重启均不再召回。删除整个检索索引可重建，而正文及来源仍在。离线 replay 不读当前记忆数据库的最新版本。

## 6. D：有边界的子代理

子代理是独立的 Child Task/Run + Job，复用已有 loop、工具准入、审批、日志与作业认领，不采用自由群聊角色框架。它依赖公共调度/预算/权限，而非强依赖 MCP 或向量记忆；本方案按用户要求的功能交付顺序推进。

```text
agent.spawn(goal, input_refs, output_schema, requested_tools, limits)
  → child_task_id, child_run_id, job_id
agent.status(job_id)
agent.result(job_id) → status, summary, evidence_refs, usage, unresolved
agent.cancel(job_id)
```

spawn 立刻返回 Job 身份；完成通过已有事件推送父任务，不在普通工具调用里阻塞数分钟，不让父模型循环轮询消耗费用。

### 第一版边界

- 深度固定为 1，最多同时 2 个子任务、每根任务最多创建 4 个；每子任务默认 8 轮、总期限 300 秒（均为提案默认值，可配置且不得超过根限额）。子任务无再委派权限。
- 默认只读：允许授权范围内的文件读取、web、只读 MCP、受限记忆检索。结果保存在内部 ArtifactStore；只有父任务负责对用户文件落地。子任务不能激活长期记忆、改变审批或获得父任务的秘密。
- 权限 = 父任务可委派权限 ∩ 请求范围 ∩ 子任务角色允许集；截止时间与数据外发许可同样只能收窄。上游撤销或改目标立即 fence 后代。
- 输入是明确 goal、选定材料引用和输出 schema，不复制全量聊天、凭据、所有记忆或父审批票据。结果同样不作为授权。
- 保存 root_run_id、parent_run_id、delegation_id、parent_goal_revision；spawn 事件、子运行和 Job 入队同一事务。以父 run + call_id 去重，父进程恢复不能重复创建孩子。
- 根账户与孩子局部额度在一个预算事务核验；取消时仅释放确定未派发的预占，未知计费保持未决。子任务不被赋予独立满额预算。
- 父任务等待孩子时持久化 waiting/dependency 状态并让出 worker 槽位；不能占着唯一执行线程等孩子，也不能握着孩子所需的目录独占租约。第一版子任务只读，加快避免写租约依赖环；父恢复写入前重取租约并重新核验版本/文件状态。
- 子任务部分失败如实返回；父任务不能把 partial 当完整成功。父暂停阻止后代新派发，父取消向整棵树传播，已送出的网络/模型请求仍可能产生费用。
- UI 在父任务下列出子任务目标、状态、来源与费用，不暴露 claim_epoch 等内部字段。

### 出口验收

一个父任务派两个孩子分别调查两种方案，收齐来源后生成对比；单 worker 调度也能完成，不死锁。父进程在 spawn 前后、孩子完成前后崩溃恢复不重复派发；父改目标后旧孩子不能提交有效结果。真实多进程 + 屏障测试最后额度争抢，只能一个预占成功。子任务无写权限、无再委派权限；父取消后无新派发，子费用只计一次，未完成项显示明确。

## 7. 可执行 PR 队列

下表的编号是本扩展计划内部编号，不改写 M0–M5 的历史编号。契约 PR 与实现 PR 分开；阶段方案获确认后才执行。

| PR | 内容 | 依赖 | 合并判据 |
|---|---|---|---|
| 01 docs(contract) | 公共资源、外发、结果与预算契约；新增动作/错误/事件登记及迁移规则 | 已验收主线 | 决策可对应到验收项；不夹带代码 |
| 02 feat(tool-runtime) | Registry、任务工具清单、资源解析器、通用准入、ArtifactRef/read、基础共享账本 | 01 | 旧文件/恢复/审批/replay 不退化；无目录工具合法；并发预占探针 |
| 03 feat(web-fetch) | 受控网络层、fetch、正文抽取、来源与 UI | 02 | 地址/重定向/额度/外发/取消/回放夹具通过 |
| 04 feat(web-search) | SearchProvider、首个真实搜索 adapter、夹具及来源链接 | 03 | 配置缺失与零结果区分；两网页证据闭环；可选真实服务 smoke |
| 05 docs(contract) | MCP 依赖 ADR、server 身份、schema 固定、远端效果与恢复准入 | 04 | 核心零依赖边界与四目标门禁明确 |
| 06 feat(mcp-stdio) | 官方 SDK adapter、stdio 生命周期、工具清单与受信任配置 UI | 05 | 真进程夹具、schema 拒绝、无未审工具、无孤儿阻塞 |
| 07 feat(mcp-http) | Streamable HTTP、凭据隔离、受控 connector、两传输契约测试 | 06 | endpoint/redirect/取消/断线验证，不能绕过网络策略 |
| 08 docs(contract) | Memory schema、候选确认、作用域、forget 与历史保留语义 | 07 | 明确不承诺未经实现的物理擦除 |
| 09 feat(memory) | SQLite/FTS5、中文索引、工具、记忆管理 UI、上下文快照 | 08 | 作用域、来源、忘记不复活、索引重建、历史回放 |
| 10 docs(contract) | 父子 Task/Run、委派权限、共享额度、等待与取消传播 | 09 | 不复制授权、不产生租约/worker 依赖死锁 |
| 11 feat(delegation) | 子任务 spawn/聚合、调度、树形状态、预算联调 | 10 | 两子任务真实闭环与父子崩溃/取消/并发争抢验证 |

每个实现 PR 更新现行知识库与验证记录；跑锁文件、静态检查、项目测试和该阶段夹具。涉及授权、并发、事件、账本的改动须独立复审并给出可复现探针。云服务 smoke、四架构构建、原生平台运行分别标注证据范围；缺 key 时只记录未跑真实服务，不把 fixture 当线上结果。

## 8. 四次产品演示与总验收

1. 网络：无目录新任务搜索公开主题、读取两篇正文并列来源；未授权私有材料外发被拒。
2. MCP：连接一个可信 server，查看已启用工具并调用；切断连接有明确失败，历史仍可回放。
3. 记忆：用户明确记住格式偏好，新任务使用，用户忘记后不再使用；来源与作用域可见。
4. 子代理：两个调查子任务并行，父任务汇总成可核实对比；暂停/取消/预算用完都沿父子链生效。

本方案仅设计交付，不代表上述能力、依赖变更、测试或真实服务已经完成。第一张实际开发工单从 PR01/PR02 开始，随后优先交付网络闭环；无需为后续子代理一次性重写整个内核。

## 9. 参考与证据

- 当前源码：`src/tianshu/{tools,dispatcher,permissions,loop,context,jobs}.py`，`pyproject.toml`。
- 项目规范：`docs/PRD-v1.md` §5/§6/§7，`docs/KNOWLEDGE.md`，`AGENTS.md`，`CONTRIBUTING.md`。
- 官方 MCP SDK 目录：https://modelcontextprotocol.io/docs/2026-07-28/sdk （本轮读取，Python SDK 属官方选项；未选定或安装版本）。
- MCP 旧版 tools：https://modelcontextprotocol.io/specification/2025-11-25/server/tools （兼容路径参考）。
- MCP 当前变更：https://modelcontextprotocol.io/specification/2026-07-28/changelog.md 及 https://modelcontextprotocol.io/specification/2026-07-28/server/discover.md （新版无 initialize、逐请求协商、tasks 扩展等）。
- 动态工具发现与程序化调用：https://www.anthropic.com/research/advanced-tool-use （厂商功能与实验；本项目收益须独立评测，不照搬文章中的百分比）。
- 混合检索：https://docs.lancedb.com/search/hybrid-search.md （向量 + FTS + RRF；不构成本项目平台兼容证据）。
- 浏览器平台限制：https://playwright.dev/python/docs/intro （本轮官方要求 macOS 14+）。
- A2A 与 MCP 的范围：https://a2a-protocol.org/latest/topics/a2a-and-mcp/ （外部 Agent 协作与工具接入互补）。
- 托管联网研究能力：https://developers.openai.com/api/docs/guides/tools-web-search.md （可选 provider 路径；不能假定与本地逐次授权和原始内容可回放等价）。
