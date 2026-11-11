# 网络外发：任务级白名单 + 抓取执行器（PRD §5.2.3 / §11.1 G2 网络阶段）

- 立据：`docs/PRD-v1.md` §5.2.3（通用工具准入、资源种类、外发许可、内容引用）、
  §8.1（默认禁止未授权出网、允许目标/重定向受执行侧规则约束）、§11.1 **G2**；
  施工规格 [`SPEC-G2-NETWORK-2026-11-11.md`](SPEC-G2-NETWORK-2026-11-11.md)。
- 状态：**已实现**（`src/tianshu/net.py`、`dispatcher.py` 的网络分支、
  `resources.py` 的源规范化、`tasks.py` 的 `outbound_allow`）。
- 判据：`tests/test_outbound_allow.py`（18）、`tests/test_network_fetch.py`（23）；
  负控 5 + 8 实测变红（见 §6）。
- **仍然不成立的部分**逐条写在 §7，不许把"能发一个 GET"当成"联网研究已闭环"。

## 1 两道门，不是一道

| 门 | 回答的问题 | 由谁声明 | 代码位置 |
|---|---|---|---|
| 任务级白名单 `outbound_allow` | 这个任务**准不准出网、能去哪几个源** | 人（`task create --allow-outbound`），创建时定死 | `dispatcher._authority_network` |
| 逐次人工批准 `c4.outbound` | **这一次**这个 URL 发不发 | 人（`approvals approve`），每次都要 | `permissions._classify_network` |

两道门都在 `prepare()` 里走完，所以白名单不通过的任务**不会**被弹一个"批了也发
不出去"的批准。批准摘要绑的是**这次请求的动作**（目标 URI + 参数），不是"这个源"：
同源换一个路径仍然要再批一次（判据 `test_each_url_needs_its_own_approval`）。

## 2 任务级白名单的形状

白名单的每一项是一个**源**（origin），不是 URL：

```
network://<transport>/<host>[:port]     例：network://https/example.com
                                            network://http/127.0.0.1:8080
```

- 规范化：host 小写 + IDNA(punycode)、默认端口省略（80/443）、拒绝 userinfo /
  fragment / 百分号编码主机；同一个源的两种写法归一成**同一项**；
- **路径 / 查询一律拒绝**：白名单是"能不能跟这台服务器说话"的声明。允许写路径
  会让人以为"只许访问 `/docs`"，而执行侧只按 transport+host+port 判——那是一条
  假的保证（`parse_network_origin` 直接报错）；
- 空声明（`[]`）与"没有这个键"在语义上等价：**一条包都不发**。新事件恒写这个键
  （空也写），旧事件缺键按 payload v0 读取、不按新版补值；
- **创建后不可改**：`task revise` 只改目标与绑定。扩权必须是新任务——和白名单
  同理，`exec_policy` 也是创建时定死的。

## 3 抓取执行器（`network.fetch`）

工具参数只有一个 `url`，**只接受规范形** `network://<transport>/<host>[:port]/<path>[?query]`。
传 `https://...` 会被拒绝并给出改法：一个目标只有一种写法，否则同一目标会因为两种
输入得到两个 `args_hash` 和两份审批摘要。

### 3.1 请求面上有什么（这是"私有内容不发包"的执行面证据）

请求头只有固定的四项：`Host`（`http.client` 按目标自动填）、`User-Agent`
（常量 `tianshu-agent/1 net-fetch`）、`Accept: */*`、`Accept-Encoding: identity`、
`Connection: close`。**没有** Cookie、`Referer`、`Authorization`，**不读**
`HTTP_PROXY` / `HTTPS_PROXY` 等代理环境变量，不从环境或文件读任何凭据。

所以「私有内容未经外发许可不发包」在外发面上有一条可核对的事实：请求里除了目标
URL 与固定头，没有别的数据。**但这不是"已证明没有别的泄露通道"**——它证明的是
请求体与头里没有内容。

### 3.2 执行侧规则（每一条都有"不这么做会怎样"）

| 规则 | 为什么 |
|---|---|
| 重定向**只许同源**（transport + host + port 全同），最多 3 跳 | 批准的是"这个源上的这个 URL"；靠 302 把包发到另一个源等于绕过批准。跨源/换端口/https→http 一律拒绝（`net.redirect_origin_denied`） |
| 响应按 `max_bytes`（默认 1 MiB）封顶，超出标 `truncated` | 「大结果有界」；不信任 `Content-Length`（可以是假的），只数实际读到的字节 |
| 整次抓取 `timeout_ms`（默认 10s）硬上限 | 网络等待不许无限期挂住 Run |
| 每一跳、每一块读之前检查取消 | 取消之后不再发新请求 / 新读；已经写进内核发送缓冲的字节无法撤回（如实登记） |
| TLS 默认校验，不提供关闭开关 | 关掉校验等于把"和谁说话"交给网络 |
| 只支持 GET，不自动重试 | 重试策略会改变"外发了几次"的事实；v1 的网络阶段不做 |

### 3.3 失败分类（都是 `failed` + `effect_state=none_applied`，不是"没有内容"）

| `output.reason` | 含义 | 字节账本 |
|---|---|---|
| `net.connect` / `net.connect_timeout` / `net.tls` | 连接阶段失败 | **0 字节（已知）** |
| `net.timeout` / `net.read` / `net.protocol` / `net.redirect_*` | 连接建立后的失败 | **未知**：预占保留、不记零 |
| `net.arg_not_canonical` / `net.arg_invalid` / `net.limits` | 参数/上限不合法 | 未预占 |
| `net.cancelled` | 取消（可能已发出） | 未知（保留预占） |
| `net.content_unavailable` | 内容存储不可用（落不了引用） | 已知实际字节 |

`tool.result.status` 的封闭集没有 `cancelled`（契约只认 succeeded/failed/rejected/
unknown），所以取消按 `failed` 记，语义放在 `output.reason`。

## 4 内容引用与字节账本

- 抓到的字节先写内容寻址 blob（与快照/事件同一套 `atomic_write_bytes`），再
  `ContentStore.record(task_id=...)` 授权给本任务，返回 `content://sha256:<hex>`；
  然后才写 `tool.result`。顺序反了会承诺一份不存在的内容；
- 返回给模型的是**有界摘要**（≤ 4 KiB，UTF-8 有损解码）+ 元数据 + 引用，不是
  整页正文；`read_content` 可以按页读回原文（二进制正文没有文本分页读取工具，
  见 §7）；
- 字节账本：发送前按 `bytes = max_bytes` 预占（**可验证上界**：本次最多读这么多），
  读完后按实际字节结算、释放差额。额度不够时在 `prepare()` 就拒绝
  （`dispatch.budget_refused`），**一个请求都不发**。

## 5 回放不联网

`tool.result` 里已经有 status / url / bytes / ref / summary，回放只读这些记录，
不重新发请求、不读当前网络。跑完停掉服务器再 `replay()` 必须仍然干净
（判据 `test_replay_after_the_server_is_gone_does_not_refetch`）。反向保护：外发
结果缺 `ref` 时回放报 problem（「回放不重新联网，读不到当时抓到的内容」），
不静默当普通工具输出。

## 6 判据与负控（回退即红，实测）

| 增量 | 判据文件 | 条数 | 负控 | 实测（2026-11-11，本机 macOS arm64） |
|---|---|---|---|---|
| 白名单 | `tests/test_outbound_allow.py` | 18 | 5 | **5/5 实测红** |
| 执行器 | `tests/test_network_fetch.py`（+`tests/httpserver.py`） | 23 | 8 | **8/8 实测红** |

负控清单（每条都实测过"回退后对应判据变红"）：

| 回退 | 变红的判据 |
|---|---|
| 删掉执行侧白名单门 | `test_no_allowlist_means_no_approval_and_no_packet` / `test_dispatch_denies_origin_not_in_allowlist` |
| 白名单匹配忽略端口或 transport | `test_allowed_matching_is_exact_by_transport_host_port` |
| 创建时不做规范化 | `test_origin_normalization_collapses_writing_variants` |
| payload 不写 `outbound_allow` | `test_empty_allowlist_is_written_as_an_empty_list` / 契约镜像 |
| 跨源重定向自动跟随 | `test_cross_origin_redirect_is_refused_and_second_hop_never_happens`（第二跳命中数 > 0）+ `test_https_to_http_redirect_is_refused_by_the_same_rule` |
| 不设响应上限 | `test_response_is_capped_by_the_executor_limit` |
| 不写内容引用（正文内联） | `test_fetch_succeeds_and_returns_a_usable_reference` |
| 不预占 / 不结算字节 | `test_bytes_budget_is_enforced_before_sending` |
| 字节结算记零（预占释放、用量不记） | `test_bytes_budget_is_enforced_before_sending`（`usage.settled.bytes` 与实际不符、第二次抓取被放行） |
| 取消检查失效（执行器每跳/每块之前） | `test_in_flight_cancel_stops_further_reads_but_cannot_unsend` |
| 缺 ref 时回放不报 problem | `test_replay_reports_a_network_result_without_reference` |
| `https://` 参数被当成合法输入 | `test_plain_url_argument_is_rejected_with_the_canonical_form` |

**取消有两层检查（派发前 + 执行器每跳/每块之前）**：单独去掉派发前那层，两个取消
判据**仍然绿**（执行器第一跳之前就看到了取消）；只有两处都去掉，才会真的发包
（实测两个判据同时红）。这条冗余是刻意的：把"取消后不再发新请求"绑在一处检查上，
将来一次重构就没了。

## 7 没做 / 不成立的部分

| 缺口 | 现状 |
|---|---|
| 搜索引擎 / 检索 API | **没有**：只支持对给定 URL 的 GET。§10.5 的"检索公开来源"在真实意义上仍未闭环 |
| 真实公网研究 | **未验证**：判据全部跑本机 loopback 夹具；没有真实站点、没有真实检索服务、没有真实模型复跑 |
| POST / 上传 / 表单 | 未做（也就没有"把本地文件传出去"的面） |
| 代理 / Cookie / 凭据转发 | 未做，且**不读代理环境变量**（见 §3.1） |
| 跨源重定向 | 恒拒；需要时由模型重新发起并重新批准 |
| 频率 / 速率限制 | 未做 |
| DNS rebinding / 解析后二次校验 | **未做**：白名单显式声明源，但解析到私有地址会照发（SSRF 面未完全解决） |
| 每 Run 的抓取配置 | 超时 / 上限 / 跳数是进程级常量 + 测试注入；没接 CLI / 任务声明 |
| 二进制正文 | 原始字节在引用里，但没有文本分页读取工具；摘要是有损解码 |
| 内容分级 | 未做：没有"私有材料"标记与外发表，审批展示完整 URL 由人判断 |
| 保留期 / GC | 未做：抓回来的内容只写不删 |
| MCP / 记忆执行器 / 子代理 | **本轮不做**（G2 后续阶段） |
