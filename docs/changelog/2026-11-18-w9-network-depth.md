# 2026-11-18 · G3-B08 / W9：连接期单次 DNS 解析与 Socket IP 钉死 + C2 `v2.0` 网络分页与二进制编码

## 背景与契约依据

依据已冻结契约 [`docs/CONTRACT-PHASE2-G3-2026-11-18.md`](../CONTRACT-PHASE2-G3-2026-11-18.md) §3、[`docs/network-g2.md`](../network-g2.md) §4.1/§7.1 与 [`docs/PRD-v1.md`](../PRD-v1.md) §5.2.3（及红线 8），落地 W9 网络深度两项核心能力，关闭域名白名单下的 DNS Rebinding TOCTOU / 内网与云元数据 SSRF 面，并打通大响应与二进制内容引用的无损分页读取闭环。

## 关键实现与架构决策

1. **零 TOCTOU 连接期单次 DNS 解析 + 全 RRset Fail-Closed 校验 + Socket 数值 IP 钉死（`src/tianshu/net.py` + `src/tianshu/dispatcher.py`）**
   - **区分字面量 IP 与域名**：
     - URL 主机为规范字面量 IP（如测试夹具显式白名单并逐次批准的 `127.0.0.1:<port>`）时不触发任何 DNS 解析，同时对未指定（`0.0.0.0`/`::`）、多播、广播及链路本地云元数据段（`169.254.0.0/16`、`fe80::/10` 及其 `ipv4_mapped`/`sixtofour`/`teredo` 内嵌映射）恒定拦截并抛 `NetError(reason="net.private_address_blocked")`。非标准十进制/十六进制/八进制数字主机名不被视作字面量豁免，统一进入域名解析门禁拦截。
     - URL 主机为域名时，每跳仅调用一次可注入 `_resolve_host(host, port)`；对返回的完整 RRset 逐条解包内嵌 IPv4 并核验全局单播性，只要任一候选记录命中非全局/私网/回环/CGNAT/链路本地/ULA/多播/保留/未指定地址，在创建任何 `socket` 之前整体 fail-closed 抛 `NetError(reason="net.private_address_blocked")`。
   - **Socket 直连钉死与主机身份保持**：
     - `_PinnedHTTPConnection` 与 `_PinnedHTTPSConnection` 保留原始域名 `self.host`（确保 HTTP `Host: <host>[:port]` 请求头逐字不变），重写 `connect()` 直接向已校验数值 `sockaddr` 建立 TCP 连接，绝不触发 `http.client` 二次 `getaddrinfo()`。
     - `_PinnedHTTPSConnection.connect()` 在已连上的底层套接字上以 `CERT_REQUIRED` + `check_hostname=True` 调用 `context.wrap_socket(raw_sock, server_hostname=self.host)`，完整保留原域名的 TLS SNI 与 X.509 主机名核验，握手异常时立即关闭底层套接字防止句柄泄漏。
     - 每一跳同源 301/302/303/307/308 重定向均重新执行「单次解析 → 全 RRset 校验 → 数值 IP 钉死」，拦截次跳重绑到内网/回环；`net.private_address_blocked` 不入 `_RETRYABLE_REASONS`，并在 `dispatcher._dispatch_network` 的 `known_zero` 中按已知 `0` 字节结清预占。

2. **红线 8 工具升版（`1.0` → `2.0`）与分页/多编码无损读取（`src/tianshu/tools.py` + `src/tianshu/contents.py` + `src/tianshu/loop.py` + `src/tianshu/dispatcher.py`）**
   - **`network.fetch` `2.0`**：新增可选参数 `offset`（`>= 0`）、`length`（`1..65536`，默认 `limits.summary_bytes`）与 `max_bytes`（`1..1048576`，有效上界 `min(max_bytes, limits.max_bytes)` 同步收紧 `prepare()` 额度预检、`dispatch()` 字节账本预占与抓取读取上限）；输出在保留全部原有字段基础上新增 `offset`、`page_bytes`（UTF-8 多字节字符边界安全回退）与 `next_offset`（未读完时直接交由 `read_content` 离线翻页，避免重复外发请求）。
   - **`read_content` `2.0`**：新增封闭编码枚举 `encoding ∈ {"utf-8", "utf-8-replace", "latin-1", "base64"}`（默认 `"utf-8"` 与 `1.0` 严格一致）；所有模式先统一执行 `(digest, task_id)` 任务可见范围校验与整文件 `sha256` 复核，输出新增 `encoding` 字段并保证 `bytes` 恒为本页消费的底层原始字节数（`next_offset != null` 时恒满足 `offset + bytes == next_offset`）。
   - **红线 8 跨版本隔离**：旧 `1.0` `ActionRequest` 被当前派发器以 `dispatch.not_admitted` 拒绝，既有 `1.0` 审批记录不会匹配 `2.0` 请求摘要。

## 诚实边界

- 搜索/检索 API 选型与真实公网站点/付费模型端到端复跑仍属外部待拍板/待验项，未在本分支宣称闭环。
- 操作系统底层 `socket.getaddrinfo` 系统调用本身仍无法被 Python 线程级定时器在阻塞中途异步打断。

## 验证命令

```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen pytest tests/test_w9_network_depth.py tests/test_network_fetch.py tests/test_net_resilience.py tests/test_outbound_allow.py tests/test_round2_inputs_net.py tests/test_round2_auth_execution.py tests/test_network_research_eval.py tests/test_eval_real_tasks.py tests/test_c1_c2_c5_schemas.py tests/test_schemas.py tests/test_g1_admission.py tests/test_g1d_resource_kinds.py tests/test_content_refs.py tests/test_content_read.py tests/test_dispatch.py tests/test_rule_ids.py tests/test_veto_scope.py tests/test_windows_compat.py -q
```
