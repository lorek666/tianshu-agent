# G2 网络阶段的施工规格（SPEC-G2-NETWORK-2026-11-11）

- 立据：`docs/PRD-v1.md` §5.2.3（通用工具准入 / 资源种类 / 外发许可 / 内容引用）、
  §5.4.1（规则编号）、§5.6（共享预算账本）、§10.5（真实用户任务集·联网研究）、
  §11.1 **G2 完整用户体验**（「按网络 → MCP → 记忆 → 子代理推进；网络阶段先完成
  §10.5 的本地任务和联网研究闭环，可查看产物、来源、费用与恢复状态」）。
- 上游对账：[`PRD-REVIEW-DELIVERY-2026-11-11.md`](PRD-REVIEW-DELIVERY-2026-11-11.md)
  §1 把「G2 网络 / MCP / 子代理」登记为**未做**，§4 逐条写明「网络 / MCP / 委派的
  执行器没有（G2）」「没有任务级外发白名单（`outbound_allow`）」。本规格把其中
  **网络**一段拆成三个可验收增量；**MCP、记忆执行器、子代理本轮不做**（§5 登记）。
- 基线：`main @ 5248ad5`（PR #106 之后）。落笔时实测 **1790 passed / 7 skipped**、
  `ruff check .` 干净、运行时依赖仍为零（`pyproject.toml` 的 `dependencies = []`）。
- 判定口径（沿用 `SPEC-PRD-REVIEW-2026-11-11.md` §0）：**有回归测试、且做过负控
  （把这次加的保护回退后测试会红）才算做完**。要求写进 PRD ≠ 已实现；判定不了 =
  unknown + 阻断。**不许**用「能发一个请求」冒充「联网研究闭环」。
- 范围（本轮**只做**）：
  - **I1**：任务级外发白名单 `outbound_allow` —— `task.created` payload 契约增量、
    任务表、创建入口、执行侧许可门；
  - **I2**：`network.fetch` **执行器**与派发链路 —— GET 抓取、目标/重定向受执行侧
    规则约束、超时与响应上限、字节账本预占/结算、有界摘要 + `content://` 引用、
    断网诚实失败、取消或额度耗尽后无新请求、回放不联网；
  - **I3**：§10.5 **联网研究闭环**（本机夹具）：来源可离线核验、私有内容外发受控、
    报告可检查，以及**断网时的诚实失败**任务。
- 本轮**不做**（逐条登记，见 §5）：MCP 客户端、记忆 / 子代理执行器、搜索引擎
  （没有检索 API 与凭据）、代理 / Cookie / 凭据转发、真实公网研究复跑、内容分级、
  保留期与 GC、G3 平台实机。

## 0 施工纪律

1. 只在自己的 worktree（`/Volumes/Date/ts-wt-g2`，分支 `feat/g2-*`）里操作；禁止在
   主检出 `/Volumes/Date/天枢Agent` 跑 `git` 或改文件（那里有别的会话的未提交改动）。
2. **不加第三方运行时依赖**；抓取只用标准库（`http.client` / `ssl` / `socket`）。
3. 一提交一件事：`feat:` / `test:` / `docs:` 分开；提交正文写「改了什么、为什么、
   判据、负控摘要」。
4. **不许改判据让自己变绿**：既有断言只能因为「新要求改变了语义」而改，且必须在
   PR body 写明旧口径、新口径与理由。本轮已知会改到的旧口径见 §4。
5. 收工前：全量 `pytest` 绿、`ruff` 干净；数字以干净 `main` 上的实测为准，
   **不编 CI 数字**。
6. 干不完就如实报：完成了什么、卡在哪、哪些宣称仍然不成立。

## 1 三个增量与依赖

| 增量 | 交付物 | 依赖 | 合入顺序 |
|---|---|---|---|
| I1 | `outbound_allow`：任务声明 + 执行侧许可门 | 无 | 先 |
| I2 | `network.fetch` 执行器 + 派发链路 + 内容引用 + 字节账本 | I1（白名单是执行侧规则的一部分） | 次 |
| I3 | §10.5 联网研究闭环（本机夹具，正 / 反两个任务） | I1 + I2 | 末 |

I2 必须能在 **I1 未声明任何白名单**时安全失败（`c4.outbound_not_allowed`），这样
「没有声明的任务一条包都不发」是 I1 就成立的判据，而不是靠 I2 的执行器自觉。

## 2 施工口径

### 2.1 I1：任务级外发白名单 `outbound_allow`

**形状**：白名单的每一项是一个**源**（origin），不是完整 URL：

```
network://<transport>/<host>[:port]     例：network://https/example.com
                                            network://http/127.0.0.1:8080
```

- 用 `resources.NetworkTarget` 解析并**规范化**（host 小写 + IDNA、默认端口省略、
  拒绝 userinfo / fragment），再要求 `path == "/"`、`query == ""`：源不带路径，
  路径级别的取舍留给**逐次人工批准**；
- 创建时**去重 + 排序**后落库，保证同一个声明得到同一份摘要与同一条事件 payload；
- 传入非字符串 / 空串 / 带路径 / 带查询 / 带 fragment / `https://...` 直接拒绝
  （`TianshuError`，CLI 退出码 2）——**不静默丢弃**非法项。

**存储**：

- `tasks.outbound_allow TEXT NOT NULL DEFAULT '[]'`（`_ensure_column` 幂等补列）；
- `Task.outbound_allow: tuple[str, ...] = ()`；`_row_to_task` 读回；
- `TaskStore.create_task(..., outbound_allow: list[str] | None = None)`；
- **不可改**：`task.revise` 只改目标与绑定，白名单在创建时定死（与 `exec_policy`
  同一条理由：扩权必须是新任务，不能让旧 Run 的批准跟着悄悄扩）。这一点写进文档。

**事件契约**：

- `task.created` payload 增加 **required** 键 `outbound_allow: [str]`，新写入恒写
  （空列表也写）。旧事件缺这个键 → `detect_version()` 落到 v0（`LEGACY_SCHEMAS`
  由当前 schema 派生、`required=[]`），与 `control` / `memory_scopes` 的先例一致；
- `schemas/c3/event.json` 的 `$defs.payload` / `$defs.legacyPayload` 同步重生成
  （`tests/test_event_schema.py::test_schema_json_mirrors_python_contract` 逐字绑定）。

**执行侧许可门**（`dispatcher._authority` 的 scheme 分支）：

- 网络资源仍**只能**单个（沿用 `c4.multi_resource`）；
- 资源种类是 `network` 时：工具必须在 `NETWORK_TOOL_IDS`（否则
  `dispatch.network_resource_mismatch`）；目标的 `(transport, host, port)` 必须与
  白名单某一项**完全相等**，否则 `c4.outbound_not_allowed`（**新增规则编号**）；
- 白名单为空 / 未声明 → **一律拒绝**："这个任务没有声明任何允许外发的源"；
- 该门在 `prepare()` 里就生效（`_authority` 在审批创建之前被调用），所以**不会**
  给用户弹一个「批了也发不出去」的批准；
- 通过白名单之后**仍然**要逐次人工批准（`c4.outbound`，既有规则），两道门语义不同：
  任务是「准不准出网、能去哪几个源」，批准是「这一次这个 URL 发不发」。

**入口**：

- CLI：`run` 与 `task create` 各加 `--allow-outbound ORIGIN`（可重复）；
  `_outbound_allow(args)` 解析并规范化；`task show --json` 输出 `outbound_allow`，
  人话输出也打印；
- `control.py` 的控制面 Run、`dispatcher.create_control_run` **不传**白名单（默认空）
  ——控制面职责不是出网，默认拒绝是正确的默认；
- 评测入口：`Case.outbound_allow`（可含 `{WEB_BASE}` 模板，见 §2.3），
  `eval/runner.py` 传给 `create_task`。

**判据**（`tests/test_outbound_allow.py`，约 14 条）：

1. 创建时规范化：大小写 / IDNA / 默认端口 / 去重 / 排序；
2. 非法项拒绝（带路径 / 带查询 / fragment / userinfo / `https://` / 空串 / 非字符串）；
3. `task.created` payload 含规范化后的列表；空列表也写；
4. 旧事件（无该键）读回为 v0 且 `outbound_allow=()`（不补值）；
5. 白名单匹配：同 transport+host+port 放行到下一道门；换端口 / 换 transport /
   换 host（含子域）→ `c4.outbound_not_allowed`；
6. 未声明白名单 → `c4.outbound_not_allowed`，且**不产生** `approval.requested`；
7. `task.revise` 不会改白名单；
8. CLI `--allow-outbound` 端到端（`task create` + `task show --json`）；
9. 控制面任务默认空列表。

**负控**（回退必红，逐条实测）：

| 回退 | 变红的判据 |
|---|---|
| 删掉执行侧白名单门 | 未声明白名单的任务仍能出网路径（`c4.outbound_not_allowed` 那条红） |
| 匹配忽略端口 / 忽略 transport | 换端口 / 换 transport 的拒绝判据红 |
| 创建时不做规范化（原样存） | 规范化与「同一源两种写法」判据红 |
| payload 不写 `outbound_allow` / 镜像 JSON 不同步 | 契约与镜像判据红 |
| CLI 不接线 | 端到端判据红 |

### 2.2 I2：`network.fetch` 执行器

**工具注册**（`tools.py`）：

```
tool_id="network.fetch", version="1.0"
side_effect_level="non_idempotent"   # 有外发与成本：不走"纯读"路径，必须经派发
action=ACTION_FETCH
input_schema={"type":"object",
              "properties":{"url":{"type":"string","minLength":1,"maxLength":2048}},
              "required":["url"],"additionalProperties":False}
```

- `url` **只接受规范形** `network://<transport>/<host>[:port]/<path>[?query]`；
  传 `https://...` 报错并提示规范写法（一个目标一种写法，杜绝「同一目标两种摘要」）。
  这条写进工具描述（含例子）。
- 描述里同时写清：只支持 GET；外发需人工批准；任务必须先 `--allow-outbound`。

**执行器模块**（新 `src/tianshu/net.py`，第 53 个模块）：

```python
NET_TIMEOUT_MS = 10_000
NET_MAX_BYTES = 1_048_576          # 单次抓取最多读 1 MiB
NET_MAX_REDIRECTS = 3
NET_SUMMARY_BYTES = 4_096          # 返回给模型的有界摘要
NET_CHUNK = 65_536
NET_USER_AGENT = "tianshu-agent/1 net-fetch"

@dataclass(frozen=True)
class NetLimits:      # 测试注入点；默认值即上面的常量
    timeout_ms: int = NET_TIMEOUT_MS
    max_bytes: int = NET_MAX_BYTES
    max_redirects: int = NET_MAX_REDIRECTS
    summary_bytes: int = NET_SUMMARY_BYTES

@dataclass(frozen=True)
class Fetched:
    target: NetworkTarget      # 最终跳的目标
    status: int
    content_type: str
    body: bytes                # 已读到的字节（≤ max_bytes）
    truncated: bool
    redirects: tuple[str, ...] # 每一跳的 URI（不含第一跳）
    elapsed_ms: int

class NetError(TianshuError): ...           # 平台级失败（连不上 / TLS / 超时）
def canonical_target(raw: object) -> NetworkTarget   # 只接受 network:// 规范形
def fetch(target, *, limits, cancel) -> Fetched     # 阻塞式；每跳/每块检查 cancel
def summary_text(fetched, limit_bytes) -> str       # UTF-8 有损解码 + 截断
```

抓取规则（每条都有理由，写进 `docs/network-g2.md`）：

- **只用 `http.client`**，按 `NetworkTarget` 的 host/port 建连接：
  **不读 `HTTP_PROXY` 等环境变量**、不发 Cookie、不转发任何环境凭据、不带
  `Referer`/`Authorization`；请求头只有 `Host` / `User-Agent` / `Accept` /
  `Connection: close`。这样「私有内容未经许可不发包」在外发面上有一条可核对的事实：
  请求里除了目标 URL 与固定头，没有别的数据。
- **TLS 默认校验**（`ssl.create_default_context()`）；不提供关闭校验的开关。
- **重定向**：手动跟随，最多 `max_redirects` 跳；**只允许同源**（transport+host+port
  完全相同），跨源或 https→http 一律拒绝（`net.redirect_origin_denied`）——批准的是
  一个源上的一个 URL，不能靠 302 把包发到没批准的源；同源内换路径也要重新过
  `canonical_target` 的形状检查。
- **响应上限**：按块读，累计到 `max_bytes` 就停并标 `truncated=True`；**不**信任
  `Content-Length`（可以是假的），只数实际读到的字节。
- **超时**：`timeout_ms` 是整次抓取（连接 + 读）的硬上限；每次读之前按剩余时间设
  socket timeout，超时抛 `NetError("net.timeout")`。
- **取消**：`cancel()` 在**每次连接、每一跳、每一块读之前**被调用；已取消就不再发
  新请求/新读，返回 `cancelled`。在途 TCP 字节无法撤回（如实写进文档）。
- **失败分类**：DNS、连接被拒、TLS 失败、超时、协议错误都是 `failed`
  （`effect_state=none_applied`），输出里带 `reason` 与人类可读说明——**不**伪造成
  「没有内容」。

**派发链路**（`dispatcher.py` / `loop.py` / `loop.build_stack`）：

- `EXECUTABLE_KINDS` 加 `network`（**旧口径变更**，见 §4）；
- 新目标类型 `_NetworkTarget(uri, target)`；`_resolve_target` 在 memory 分支之后、
  文件分支之前处理 `network://`；
- `_authority` / `_resolve_target` / `_requires_approval`（`fetch` 已在
  `APPROVAL_ACTIONS`）/ `_insert_dispatch` / `_admitted` 都要认识网络目标：
  `target` 记规范 URI，`rel_path=""`，`workspace_root=""`，
  `resource_key=f"network:{origin}{path}"`；
- `dispatch()` 的网络分支**不**拿目录执行守卫（没有本地副作用，也没有租约），
  但仍在同一事务里：登记 intent → 预占字节 → 发送 → 落内容 → `tool.result`；
- **字节账本**：发送前 `BudgetLedger.reserve(need={"bytes": limits.max_bytes},
  attempt_id=f"net:{call_id}")`（这是**可验证上界**：最多读这么多）；读完后
  `settle(usage={"bytes": 实读字节}, known={"bytes"})`，预占多出的部分释放。
  `reserve` 抛 `BudgetRefused` / `BudgetCancelled` → 拒绝派发（`dispatch.budget_refused`，
  **新增规则编号**），**一个包都不发**；账本未装配时不预占（如实登记为未记账）；
- **内容引用**：读完把字节写进内容寻址 blob（与快照 / 事件同一套
  `atomic_write_bytes` + `ContentStore.record(task_id=...)`），返回
  `content://sha256:<hex>`；没有内容存储时**明确失败**，不把大 body 直接塞进事件；
- 返回给模型的 output：
  `{"url": <最终 URI>, "status": 200, "content_type": "...", "bytes": N,
    "truncated": bool, "redirects": [...], "ref": "content://sha256:...",
    "summary": "<≤4 KiB 有损解码文本>"}`；
- `loop._target_for` 增加网络分支（在「任务必须绑定目录」之前），**零目录任务**
  也能抓取；`build_request` 增加网络分支（`resources=(uri,)`，无文件 preconditions）；
- `build_stack` 把 `contents` 交给 `Dispatcher`（内容引用与事件同库同事务）。

**回放**：不加新事件类型（仍是 41 类）。`tool.intent` / `tool.result` 已冻结
「output 是对象」这一层；回放只读记录，**不**重新发请求。回放另加一条保护：
`tool.result` 里出现 `network.fetch` 却没有 `ref` 时，回放报告 problem
（「外发结果缺内容引用」），不静默当作普通文本。

**判据**（`tests/test_network_fetch.py`，约 22 条）：

1. 本机夹具 HTTP 服务器（`tests/httpserver.py`：路由 → 状态码/头/正文，记录命中数；
   `http.server` 实现，不引第三方）；
2. 抓取成功：output 的 status / content_type / bytes / summary / ref 正确；内容可
   `read_content` 读回（分页）且摘要复核通过；
3. 超上限：正文超过 `max_bytes` → `truncated=True`、`bytes==max_bytes`、ref 指向
   已读部分；
4. 重定向：同源 302 跟随并记录 `redirects`；跨源 302 → `failed`
   （`net.redirect_origin_denied`），**第二跳没有请求**（服务器命中数不变）；
5. https→http → 同上（本机不真起 TLS，用 `network://https/...` 指向本机 http 端口
   会 TLS 失败；该判据用「同源判定」单元测试而不是真握手）；
6. 失败分类：连接被拒 → `failed`；不存在的 host（DNS 失败）→ `failed`；
   读超时（服务器睡过 `timeout_ms`）→ `failed` 且耗时受控；
7. 未声明白名单 / 白名单不含该源 → `rejected` + `c4.outbound_not_allowed`，
   **服务器命中数为 0**；
8. 逐次审批：白名单内但未批准 → `waiting_approval`；批准后执行；拒绝后不重发；
9. 字节账本：`limits budget_limits={"bytes": N}` 下第 N+1 次抓取在**发送前**被拒
   （服务器命中数不增），`usage()["bytes"]` 与实际一致；
10. 取消：派发前取消 → 不再发请求；plan 里记录的 cancel 状态可见；
11. 零目录任务可抓取；文件目标的任务里发网络请求不会被当成路径解析；
12. 回放：跑完后停掉服务器再 `replay()` → 报告干净、服务器命中数不增；
13. 工具参数：`https://...` 被拒并提示规范写法；畸形 URI 被拒；
14. 工具描述 / 版本 / schema 摘要进审批（改描述作废旧批准——沿用既有机制，
    这里只验证网络工具也走这条路）。

**负控**（回退必红，逐条实测）：

| 回退 | 变红的判据 |
|---|---|
| 执行器不检查白名单（只在 prepare 检查，dispatch 绕过） | 白名单拒绝 / 命中数 0 |
| 重定向自动跟随（`http.client` 手工 302 或 `urllib` 默认） | 跨源 302 判据（第二跳命中数 >0） |
| 不设响应上限 | `truncated` 与 `bytes` 判据 |
| 不写内容引用（body 内联） | ref / 分页读取判据 |
| 不预占字节 / 不结算 | 额度耗尽判据 + `usage()["bytes"]` |
| 取消检查只看入口、不在每块/每跳检查 | 取消后命中数判据 |
| 回放时重新抓取 | 停服务器后 replay 判据 |
| `network.fetch` 结果缺 ref 时回放不报 problem | 回放 problem 判据 |

### 2.3 I3：§10.5 联网研究闭环（本机夹具）

**评测夹具**（`eval/runner.py` + `eval/tasks.py`）：

- `Case` 增加：`outbound_allow: tuple[str, ...]`（支持 `{WEB_BASE}` 模板）、
  `script_factory: Callable[[str], tuple[ModelResponse, ...]] | None`、
  `net_fixture: bool`；
- 运行环境：`net_fixture=True` 时按 case 起一个**本机** HTTP 服务（`127.0.0.1:0`
  临时端口），`WEB_BASE = 127.0.0.1:<port>`；`task` 文本、`outbound_allow` 与
  `script_factory` 的 `{WEB_BASE}` 都在运行时替换；
- **离线核验**：Run 结束后先**关掉**夹具服务器，再从内容存储读回每个
  `network.fetch` 结果的 `ref`（`ContentStore.read` 会重算 sha256），把
  `{ref, digest, bytes, head}` 记进 `Observation.offline_sources`；判据只读它，
  证明「来源可离线核验」，不是「服务器还在所以能读」；
- 断网任务：夹具用一个**已 bind 未 listen** 的 socket 的端口当目标（连接必被拒），
  证明诚实失败。

**任务**：

- `RU-W1@2.0.0`（改造）：联网研究·**断网**——目标不可达，工具如实失败，报告必须写明
  「无法访问 / 未取到来源」，不得出现未被抓取的「事实」或伪造引用；
- `RU-W2@1.0.0`（新增）：联网研究交付·本机夹具——抓两个夹具页面，写
  `research.md`，引用两条来源（含最终 URI 与引用摘要），并由 `offline_sources`
  证明来源可离线核验；审批只批这两次外发；写文件仍需批准（沿用既有 E3 链）；
- 两条都保留 `version` 与 `versioned_id`；`fixture_digest` 把版本、授权、脚本、
  判据源码摘要算进分母（沿用既有机制）。

**新增判据**（`eval/checks.py`）：

- `network_fetched(min_count)`：`tool.result` 里 `network.fetch` 成功且带 `ref` 的条数；
- `sources_offline(min_count, needles=...)`：读 `Observation.offline_sources`，
  条数、digest 形状、`head` 含期望片段；
- `network_failed_honestly()`：至少一次 `network.fetch` 失败（不是 succeeded），
  且报告里不出现「伪造的成功句式」的关键片段（case 自带 needle）；
- `no_approval_for_other_origins()`：`approval.requested` 里没有任何非白名单源。

**判据**（`tests/test_network_research_eval.py`，约 10 条）：两个任务各跑
`repeat>=3` 的报告结构、`scope=real_user` 计数、离线核验字段、断网任务的
诚实性判据、负控见下。

**负控**：

| 回退 | 变红的判据 |
|---|---|
| RU-W2 仍标 `unavailable`（不真跑） | 可跑性 / 事件非空判据 |
| 离线核验直接读活服务器（绕过内容存储） | 关掉服务器后的 `sources_offline` |
| 断网任务被写成「成功报告」 | `network_failed_honestly` + 伪造事实判据 |
| 外发前不弹审批（把 fetch 从 `APPROVAL_ACTIONS` 去掉） | `approval.requested` 判据 |

## 3 判据与负控总表（回填实测）

| 增量 | 新增测试文件 | 条数 | 负控 | 实测（2026-11-11，本机 macOS arm64） |
|---|---|---|---|---|
| I1 | `tests/test_outbound_allow.py` | **18**（原估 14，补了匹配/迁移/CLI 三条） | 5 | **5/5 红** |
| I2 | `tests/test_network_fetch.py`（+`tests/httpserver.py`） | **23**（原估 22） | 8 | **8/8 红** |
| I3 | `tests/test_network_research_eval.py` | 待回填 | 待回填 | 待回填 |

## 4 口径冲突与旧口径变更（必须写进 PR body）

1. **`EXECUTABLE_KINDS` 从 `{file, memory}` 变成 `{file, memory, network}`**：
   `tests/test_g1d_resource_kinds.py` 里「网络资源在派发前被拒
   （`dispatch.resource_kind_unsupported`）」这条判据的前提**变了**——网络现在有执行器，
   但**没有白名单仍然拒**。改法：该测试改成「MCP / 委派仍然无执行器」，并为网络补
   「无白名单 → `c4.outbound_not_allowed`」。旧口径（资源种类文档 §4/§7「执行器未实现」）
   同步更新，不删除历史结论，写清「G1d 交付时没有执行器，G2 网络阶段补上网络」。
2. **`resource-kinds.md` §7「没做」表**：删掉「真实网络请求」一项，保留 MCP / 委派 /
   子代理；新增「代理 / Cookie / 搜索引擎 / 内容分级」等仍然没做的项。
3. **PRD §5.2.3 实现状态**里「网络 / MCP / 委派的真实执行器（G2）」拆开：网络已交付，
   MCP / 委派仍未交付。
4. **`task.created` payload 新增 required 键**：旧事件按 v0 读取（`required=[]`），
   与 `control` / `memory_scopes` 的先例一致；不改事件类型数量（仍 41）。
5. **`RU-W1` 的 `unavailable_reason` 语义**：旧的「本轮不提供网络能力」在 I2 之后不再
   成立，改成「断网 / 无检索配置时如实失败」的**可运行**任务；「能力不具备」的诚实
   判据保留给 MCP / 子代理（另立任务，不在本轮）。
6. **规则编号新增**：`c4.outbound_not_allowed`、`dispatch.network_resource_mismatch`、
   `dispatch.budget_refused`；三处同改（`rules.py` / `schemas/c4/rules.json` /
   PRD §5.4.1），`tests/test_rule_ids.py` 绑定。

## 5 诚实边界（交付后仍然不成立 / 未验证）

- 只支持 **GET**，没有检索 / 搜索 API（没有搜索引擎与凭据）；没有 POST / 上传。
- **不读代理环境变量**、不发 Cookie、不转发凭据：这是「私有内容不经许可不发包」的
  执行面保证；但**不等于**「外发面已经证明没有别的泄露通道」——它证明的是请求里没有
  别的数据。
- 重定向**只允许同源**；需要跨源时由模型发起新的动作、再要一次人工批准。
- 响应上限 1 MiB、摘要 4 KiB：**不是**完整网页；二进制正文只给有损摘要，正文原样
  落在内容引用里。
- 取消只能阻止**新**请求/新读；在途 TCP 字节无法撤回。
- 白名单是「源」级别（host+port+transport），**没有**路径、方法、频率、时间窗粒度；
  没有速率限制、没有重试策略（一次失败就是失败）。
- SSRF 面：白名单显式声明源，loopback / 内网**只能**被显式声明；但没有 DNS rebinding
  防护、没有「解析后二次校验」（解析到私有地址会照发）——**如实登记**，不假装
  已经解决。
- 每 Run 的抓取配置（超时 / 上限）是**进程级常量 + 测试注入**，没有接到 CLI / 任务
  声明。
- §10.5 的「联网研究」只在**本机夹具**上闭环：没有真实公网站点、没有真实检索服务、
  没有真实模型复跑；「真实公网研究」仍记为 **未验证**。
- 内容分级（哪些材料不许外发）仍未实现：审批展示的是**完整 URL**，人按 URL 判断；
  没有「私有材料标记 / 外发表」。
- 保留期 / GC 仍未做：抓回来的内容只写不删。
- MCP / 记忆执行器 / 子代理**本轮不做**。

## 6 交付清单（合入后回填实测）

| 增量 | PR | 内容 | 判据 | 负控 |
|---|---|---|---|---|
| I1 | 待回填 | `outbound_allow` | 待回填 | 待回填 |
| I2 | 待回填 | `network.fetch` | 待回填 | 待回填 |
| I3 | 待回填 | 联网研究闭环 | 待回填 | 待回填 |
| 对账 | 待回填 | 交付文档 / PRD / KNOWLEDGE / README | — | — |
