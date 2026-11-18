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

## 3 抓取执行器（`network.fetch` `2.0`）

必填目标参数 `url` **只接受规范形** `network://<transport>/<host>[:port]/<path>[?query]`
（可选分页与上界参数 `offset` / `length` / `max_bytes` 见 §4.1）。
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
| 响应读取与重定向共享整次 `timeout_ms`（默认10s）期限 | 慢滴响应不能反复重置单次socket等待；DNS/连接系统调用仍有下述边界 |
| 每一跳、每一块读之前检查取消 | 取消之后不再发新请求 / 新读；已经写进内核发送缓冲的字节无法撤回（如实登记） |
| TLS 默认校验，不提供关闭开关 | 关掉校验等于把"和谁说话"交给网络 |
| 只支持 GET；连接失败自动重试（2026-11-14 更新；2026-11-15 复审收紧） | ~~重试策略会改变"外发了几次"的事实~~：只重试 `net.connect` / `net.connect_timeout`——**应用层请求字节为零**（TLS 握手字节除外，如实）**且该次尝试未跟随任何重定向**（已消费重定向 = 服务器见过请求，重发是第二个 GET）；TLS 之后的一切失败（读断/超时/协议/任何响应含 5xx）仍**绝不自动重试**——重发必须重新批准。边界被服务器端连接数测试钉住，见 [SPEC-G2-NET-RESILIENCE](SPEC-G2-NET-RESILIENCE-2026-11-14.md) |

### 3.3 失败分类（都是 `failed` + `effect_state=none_applied`，不是"没有内容"）

| `output.reason` | 含义 | 字节账本 |
|---|---|---|
| `net.connect` / `net.connect_timeout` / `net.tls` | 连接阶段失败 | **0 字节（已知）** |
| `net.private_address_blocked` | 域名解析命中非全局/私有地址或危险字面量 IP，连接前拦截 | **0 字节（已知）** |
| `net.timeout` / `net.read` / `net.protocol` / `net.redirect_*` | 连接建立后的失败 | **未知**：预占保留、不记零 |
| `net.arg_not_canonical` / `net.arg_invalid` / `net.limits` | 参数/上限不合法（若已读取正文后内联摘要参数越界，按实际读取字节结算） | 未预占或已知实际字节 |
| `net.cancelled` | 取消（可能已发出） | 未知（保留预占） |
| `net.content_unavailable` | 内容存储不可用（落不了引用） | 已知实际字节 |

`tool.result.status` 的封闭集没有 `cancelled`（契约只认 succeeded/failed/rejected/
unknown），所以取消按 `failed` 记，语义放在 `output.reason`。

## 4 内容引用与字节账本

- 抓到的字节先写内容寻址 blob（与快照/事件同一套 `atomic_write_bytes`），再
  `ContentStore.record(task_id=...)` 授权给本任务，返回 `content://sha256:<hex>`；
  然后才写 `tool.result`。顺序反了会承诺一份不存在的内容；
- 返回给模型的是**有界摘要**（默认 ≤ 4 KiB，可由 `offset`/`length` 选择预览窗口，UTF-8 字符边界安全）+ 元数据 + 引用，不是
  整页正文；`read_content`（`2.0`）可以按 `utf-8` / `utf-8-replace` / `latin-1` / `base64` 分页无损读回原文与二进制内容（见 §4.1）；
- 字节账本：发送前按 `bytes = effective_max_bytes`（`min(max_bytes, limits.max_bytes)`）预占（**可验证上界**：本次最多读这么多），
  读完后按实际字节结算、释放差额。额度不够时在 `prepare()` 就拒绝
  （`dispatch.budget_refused`），**一个请求都不发**。

### 4.1 W9 v2.0 网络分页与内容编码契约（2026-11-18 已落地）

> 原方案 → 新方案 → 为什么改 → 影响模块见 [CONTRACT-PHASE2-G3-2026-11-18.md](CONTRACT-PHASE2-G3-2026-11-18.md) §3；代码落地见 `src/tianshu/{tools,contents,loop,net,dispatcher}.py` 与 `tests/test_w9_network_depth.py`。

按红线 8（工具 schema 变化即视为新工具，旧版本批准摘要不自动跨版本复用），两项工具同步升版至 **`2.0`**：

1. **`network.fetch`（`version: "1.0"` → `"2.0"`）**：
   - `input_schema`（`additionalProperties: false`，`required: ["url"]`）在 `url` 之外新增三个可选整数参数：
     - `offset`: `integer`（`minimum: 0`，缺省 `0`）——本次返回内联预览页在已抓取响应体内的起始字节偏移；
     - `length`: `integer`（`minimum: 1, maximum: 65536`，缺省 `limits.summary_bytes` 即 `4096`）——本次内联预览页的最大字节长度；
     - `max_bytes`: `integer`（`minimum: 1, maximum: 1048576`）——本次抓取最多读取的响应体字节上限；执行侧取 `effective_max_bytes = min(max_bytes, limits.max_bytes)`（未传时为 `limits.max_bytes`），并在 `prepare()` 额度预检与 `dispatch()` 字节账本预占中统一使用该有效可验证上界。
   - 完整已读响应字节（`<= effective_max_bytes`）依旧先原子落盘并登记本任务 `content://sha256:<hex>` 授权，再写 `tool.result`。
   - 成功返回 `output` 在保留原有字段（`url`、`status`、`content_type`、`bytes`、`truncated`、`redirects`、`elapsed_ms`、`ref`、`summary`、可选 `binary`）基础上，新增三个分页字段：
     - `offset`: `integer >= 0`（本页起始字节偏移）；
     - `page_bytes`: `integer >= 0`（本页 `summary` 实际覆盖的原始字节数；文本按 UTF-8 字符边界安全回退，绝不切半个多字节字符）；
     - `next_offset`: `integer | null`（当 `offset + page_bytes < bytes` 时为下一页起始字节偏移，模型可直接传给 `read_content(ref=..., offset=next_offset)` 离线续读而无需重复发包；已达响应末尾时为 `null`）。
2. **`read_content`（`version: "1.0"` → `"2.0"`）**：
   - `input_schema`（`additionalProperties: false`，`required: ["ref"]`）新增可选参数：
     - `encoding`: `string`，封闭枚举 `["utf-8", "utf-8-replace", "latin-1", "base64"]`（缺省 `"utf-8"`，与 `1.0` 行为完全一致）。
   - 语义与校验：
     - 所有模式一律先执行 `(digest, task_id)` 任务可见范围校验与整文件 `sha256` 复核（`ContentNotVisible` / `ContentMissing` / `ContentDigestMismatch` 口径不变）；
     - `"utf-8"`：严格 UTF-8 解码，分页不切半个多字节字符，遇到非法 UTF-8 字节明确报错；
     - `"utf-8-replace"`：在字符边界回退不完整尾部前导序列后，将其余非法字节替换为 `U+FFFD`；
     - `"latin-1"`：对 `[offset : offset + length]` 原始字节做 ISO-8859-1 单字节无损文本映射；
     - `"base64"`：对 `[offset : offset + length]` 原始字节（单页最多 `65536` 原始字节）输出标准 RFC 4648 带 padding Base64 字符串，供二进制内容引用无损分页读取；
     - 返回 `output` 新增 `encoding` 字段回显本次生效编码，且 `bytes` 恒表示本页消费的底层原始字节长度（当 `next_offset != null` 时满足 `offset + bytes == next_offset`）。

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
| 频率 / 速率限制 | **已交付进程级下限（2026-11-14）**：相邻两次 fetch 开始至少隔 `min_interval_s`（默认 1.0s，`0` 显式关闭，env 可覆盖）；等待分片且随时可取消、不计入抓取 deadline。per-target 限速 / 令牌桶 / 并发上限仍不做 |
| DNS rebinding / 解析后二次校验 | **已交付连接期单次解析与 IP 钉死（2026-11-18 W9 `v2.0`，见 §7.1）**：每跳仅解析一次、全 RRset 拦截非全局/私有/特殊保留地址（`net.private_address_blocked`）并直连数值 `sockaddr`，保留原 `Host` 头与 TLS SNI / X.509 主机名核验；操作系统底层 `getaddrinfo` 阻塞本身仍无法被 Python 线程异步打断 |
| 每 Run 的抓取配置 | 超时 / 跳数是进程级常量 + 测试注入；**2026-11-18 W9 `v2.0` 已交付按请求 `max_bytes` 上界收紧参数与 `offset`/`length` 内联窗口（见 §4.1）** |
| 二进制正文 | 内联 `summary` 为 UTF-8 预览；**2026-11-18 W9 `v2.0` 已交付 `read_content` `2.0` 的 `base64`/`latin-1`/`utf-8-replace` 编码无损分页读取（见 §4.1）** |
| 内容分级 | 未做：没有"私有材料"标记与外发表，审批展示完整 URL 由人判断 |
| 保留期 / GC | 未做：抓回来的内容只写不删 |
| MCP / 记忆执行器 / 子代理 | **本轮不做**（G2 后续阶段） |

### 7.1 W9 v2.0 连接期单次 DNS 解析与 Socket IP 钉死契约（2026-11-18 已落地）

为关闭「域名白名单放行后 DNS 解析到回环/私网/云元数据 IP 或二次解析重绑（DNS Rebinding TOCTOU）」风险，抓取执行器在初始请求与**每一跳重定向**建立连接前强制执行以下协议：

1. **单次解析与非全局地址拦截（`net.private_address_blocked`）**：
   - 当 `target.host` 为域名（非字面量 IP）时，仅调用一次可注入解析器（默认 `socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)`）。对返回的全部候选地址先解包 IPv4-mapped IPv6（`::ffff:x.x.x.x`），只要存在任意非全局单播地址（`not ip.is_global`，含回环 `127.0.0.0/8`/`::1`、RFC 1918 私网 `10/8`·`172.16/12`·`192.168/16`、RFC 6598 CGNAT `100.64.0.0/10`、链路本地及云元数据 `169.254.0.0/16`/`fe80::/10`、唯一本地 `fc00::/7`、多播、未指定 `0.0.0.0`/`::` 与保留地址），在发起任何 `socket.connect()` 之前立即抛 `NetError(..., reason="net.private_address_blocked")`。
   - 当 `target.host` 本身是任务 `outbound_allow` 显式声明并经人工批准的规范字面量 IP（例如本机测试夹具 `127.0.0.1:<port>`）时，不触发 DNS 解析（无重绑窗口），但对未指定（`0.0.0.0`/`::`）、多播与链路本地云元数据段（`169.254.0.0/16`/`fe80::/10`）仍恒定拦截并报 `net.private_address_blocked`。
   - 派发层将 `net.private_address_blocked` 记为 `status="failed"`、`effect_state="none_applied"`、`output.reason="net.private_address_blocked"`，字节账本按**已知 `0` 字节**结算并释放预占（确证未建立连接、未发送字节）。
2. **连接期 Socket IP 钉死 + HTTP `Host` / TLS SNI / X.509 身份保持**：
   - TCP 套接字必须直接向上述单次解析校验通过的 `(family, socktype, proto, sockaddr)` 字面量 IP 元组发起 `connect()`，**严禁将 `target.host` 再次交给 `http.client` 默认 `connect()` 触发第二次 `getaddrinfo()`**。
   - HTTP 请求头 `Host` 仍携带原规范 `target.host`（非默认端口含 `:port`），不得被替换成钉死的字面量 IP。
   - HTTPS 连接在已连上的套接字上通过默认安全上下文执行 `ssl_context.wrap_socket(sock, server_hostname=target.host)`，完整保留原域名的 TLS SNI `server_hostname` 与标准 X.509 证书链及主机名校验。
   - 因每跳使用独立连接，**每一次同源 301/302/303/307/308 重定向跳**都必须重新执行上述「单次解析 → 非全局 IP 拦截 → Socket IP 钉死 → `Host` 头与 TLS SNI / X.509 主机名核验」全流程，阻断「首跳公网 IP、次跳 302 同源重绑到内网/回环」攻击。

## 第二轮debug补充（2026-11-13）

网络派发在实际发包前的同一执行守卫内重核审批/claim/Run/grant；先短事务提交executing，再释放SQLite事务和守卫等待网络。
撤销先赢则不发包；执行开始先赢时撤销明确拒绝。query-only redirect用urljoin保留路径再检查同源。
响应慢头/体由绝对deadline中断实际socket，finally cancel+join定时器，跨跳不重置；系统DNS解析仍不能保证被该机制中断。
模型HTTP适配器的跨源redirect另行拒绝，不与network.fetch的审批混为一谈。可执行反例与独立复审见[第二轮总账](DEBUG-ROUND2-2026-11-13.md)。
