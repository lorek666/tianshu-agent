# Phase 2 / G3 契约冻结总览：恢复 C 解除、W15 安全 GC、W9 v2.0 分页防重绑与 W14 双沙箱（2026-11-18）

> 本文件遵循 [AGENTS.md](../AGENTS.md) §三契约变更流程：先以独立 `docs(contract)` PR 冻结「原方案 → 新方案 → 为什么改 → 影响哪些代码模块」及增补机器契约（`schemas/data/restore-v2.json`、`schemas/data/restore-registry-v2.sql`），不夹带任何 Python / Rust / TS 实现代码；紧随的实现 PR 据此接线并通过双向绑定测试与独立复审。

---

## 1. Restore C · 受管恢复解除与服务恢复契约（[restore-release-contract.md](restore-release-contract.md) + `schemas/data/restore-v2.json` + `schemas/data/restore-registry-v2.sql`）

### 原方案
- 恢复 B（[restore-held-contract.md](restore-held-contract.md)、`schemas/data/restore-v1.json`、`schemas/data/restore-registry-v1.sql`）仅定义导入到持久隔离态 `published_hold`、只读 `restore-review`、同批 `restore-resume` 与未发布前 `restore-abort`；`RestoreRegistry` `USER_VERSION = 1` 仅含 7 个阶段（`reserved` → `stage_owned` → `sealed_hold` → `publish_intent` → `published_hold` 及 `abort_intent` → `aborted`），没有解除隔离事件或服务恢复 API。
- 普通准入（`data_admission.check_connection`）对 `PRAGMA user_version = 2` 的事件库强制要求同目录存在合法 `completed` 的 `<db>.migration.json`，而完整备份（`backup.py`）按设计排除 `.migration.json` 进度文件，导致合法 `v2` 备份即使解除隔离也无法通过普通 `EventLog` 准入。

### 新方案
1. **权威注册表无损升版（`RestoreRegistry` v2，`USER_VERSION = 2`）**：
   - 新增 [schemas/data/restore-registry-v2.sql](../schemas/data/restore-registry-v2.sql) 与 [schemas/data/restore-v2.json](../schemas/data/restore-v2.json)（保留 `restore-v1.json` 与 `restore-registry-v1.sql` 原字节不变，供既有 T0/T1 契约测试固定核验）。
   - `restore_events.kind` 封闭集在原 7 值基础上增补 `'release_intent'`（`ReleaseIntentV1`）与 `'released'`（`ReleasedV1`）；完整合法恢复链扩展为 `reserved → stage_owned → sealed_hold → publish_intent → published_hold → release_intent → released`。
   - 只读 `observe()` 同时兼容验证 `USER_VERSION = 1` 与 `USER_VERSION = 2`；首次写入 `v2` 事件时在独占事务内将既有 `v1` 注册表无损原地重建升为 `USER_VERSION = 2`（`registry_meta`、既有事件字节/哈希与 `registry_head` 100% 保持不变）。
   - 数据根内 `.restore-origin.json`（`OriginV1`）保持**一次写入、磁盘永不改写**；是否解除隔离唯以固定用户 Home 下 `RestoreRegistry` 全链校验通过且尾事件为匹配 `(S, D)` 实体身份的 `ReleasedV1` 为准。
2. **显式解除命令与前置核验**：
   - CLI / 控制入口：`tianshu data restore-release --data-root ROOT [--batch ID] --expect-review REVIEW_SHA256 --acknowledge-unproven-later-history [--json]`。
   - 在批次操作独占（`restore_gate` → `namespace` exclusive → `(S, D)` `entity` exclusive）下：核验无活跃 `daemon`/`host`/`worker`，重核稳定父目录 `P` 与最终根 `S`、主库 `D` 的原生实体身份及 `.restore-origin.json`，重算完整 `BindingV1` 与只读复核摘要，硬性要求 `review_complete == True`、`review_sha256 == expect_review` 且显式传入 `--acknowledge-unproven-later-history`。
3. **两阶段持久化解除与 append-only 确定性对账收口**：
   - 先持久化 `ReleaseIntentV1`（固化确定性 `ReconcilePlanV1` 及时间戳；故障点 `FP-RELEASE-INTENT`）。
   - 在同一独占锁内对主库 `D` 执行单事务 append-only 对账收口（不新增 C3 事件类型、不改写历史事件、不改 `PRAGMA user_version`）：将导入的所有非终态 `runs` 追加 `run.fenced` 置为 `fenced`；将所有 `pending`/`approved` 审批追加 `approval.expired` 置为 `expired` 并作废未消费 `grants`/`waiting` 请求；清空陈旧 `leases` 投影行；将非终态 `jobs`/`subtasks` 置为 `cancelled`/`fenced`；对未决 `tool.intent`/`dispatches` 严格区分「未越过发送边界可证 `none_applied`」与「其余一律保留 `unknown` 绝不重放（必要时补 `recovery.blocked` / `recovery.object_blocked` 及 `authority` 阻断）」；执行 `PRAGMA wal_checkpoint(TRUNCATE)`（故障点 `FP-RELEASE-RECONCILE`）。
   - 对账核验收敛后持久化 `ReleasedV1`（故障点 `FP-RELEASED`）；中断重入按 `ReleaseIntentV1.reconcile_plan` 幂等收敛，绝不重复追加第二轮对账事件。
4. **解除后数据库准入规则（Post-Release Database Admission Rule）**：
   - `PRAGMA user_version == 2` 的事件库：只要固定 Home `RestoreRegistry` 存在完整有效链且尾事件 `ReleasedV1` 绑定当前实时打开句柄的 `(S, D)` 实体身份（且 `_versions` 与 `_mechanism(conn, 2)` 审计表/索引核验通过），`acquire_restore_database_access` 与 `data_admission.check_connection` **直接放行普通连接，不再要求存在备份原本就不携带的 `<db>.migration.json` 文件**（也不伪造 `.migration.json`）；无有效 `ReleasedV1` 实体证明且无 `.migration.json` 的野生 `v2` 库继续拒绝。
   - `PRAGMA user_version == 0` 的事件库：`released` 后按既有 `v0` 规则直接放行普通 `EventLog` 连接。
   - `PRAGMA user_version == 1` 的事件库：`released` 只解除恢复隔离（允许离线执行 `tianshu data backup` 与 `tianshu data migrate`），普通 `EventLog` 打开**继续以 `migration_incomplete` 拒绝**，必须先对解除后的数据根生成新一致备份并显式执行 `tianshu data migrate` 升至 `v2` 后方可提供普通服务。

### 为什么改
- 闭环 PRD §8.5「先隔离复核、核清未决状态与后续删除不可证边界后再恢复服务」的完整契约链，同时在不伪造 `.migration.json`、不破坏 `.restore-origin.json` 不可变性的前提下，打通受管 `v2` 恢复库的正常启动准入并守住 `v1` 中间态禁入红线。

### 影响哪些代码模块
- `src/tianshu/restore_contract.py` / `src/tianshu/restore_registry.py`（v2 类型、摘要域 `review`/`reconcile`、`USER_VERSION` 1→2 兼容读取与原子升版）
- `src/tianshu/restore.py`（或受管恢复执行模块：`apply` / `review` / `resume` / `abort` / `release` 及 `FP-RELEASE-*` 故障点）
- `src/tianshu/restore_access.py` / `src/tianshu/data_admission.py`（`ReleasedV1` 范围放行与 `v2` 实体绑定准入、`v1` 持续要求显式 `migrate`）
- `src/tianshu/data_cli.py`（`restore-release` 等子命令接线）
- `tests/test_restore_*.py`（全链路正负控、双进程锁竞争与 `os._exit(77)` 恢复幂等测试）

---

## 2. W15 v2 · 保守离线安全 GC 契约（[CONTRACT-W15-RETENTION-2026-11-15.md](CONTRACT-W15-RETENTION-2026-11-15.md) §6）

### 原方案
- W15 v1 仅交付只读测量命令 `tianshu data retention-audit` 与只出候选清单不删文件的 `backup.cleanup_plan()`；孤儿 blob、超期终态 `jobs` 与已结清已知的 `budget_reservations` 没有受管离线清理入口，低磁盘（`< 2 GiB`）用户无法通过受控工具安全回收数据根空间。

### 新方案
1. **CLI 契约与退出码**：
   - `tianshu data gc --data-root ROOT [--older-than-days 90] [--dry-run | --approve] [--json]`（`--older-than-days` 默认 `90`，正整数 `>= 1`；`--dry-run` 与 `--approve` 互斥）。
   - 默认（未带 `--approve`，或显式 `--dry-run`）为**只读预演**，零写零删，退出码 **`3`**（需要人决定，与 `undo` 预演同口径）；显式 `--approve` 执行离线清理，成功退出 **`0`**，前置条件不满足/锁忙/引用未知退出 **`2`**。
2. **离线独占、锁内二次引用核验与低磁盘豁免**：
   - `--approve` 必须先取得 `acquire_restore_database_access(root / "events.db", exclusive=True)` 独占准入（自动排斥活跃 `EventLog` 连接与未解除 `held` 恢复范围），并在锁内核实无存活 `daemon`/`host`/`worker`。
   - **锁内重跑 `collect_references(root)`**（消除预演到执行间的 TOCTOU）：一旦抛 `ReferenceUnknown`、存在 `in_progress` 的 `undo_journals` 或准入核验失败，立即 **fail-closed（0 删除，退出 `2`）**。
   - **豁免 2 GiB 新 Run 空间门槛**：`data gc` 是空间回收维护操作，不创建业务 `effects` Run，不适用 `DiskAdmission` 2 GiB 下限，确保低磁盘用户可用它回收空间。
3. **绝对禁删面（0 删除硬红线）**：
   - `events` / `event_versions`（红线 1）；
   - `summary_versions` / `summary_materials` / `summary_sources`（DELETE 触发器 + 回放三道校验）；
   - `dispatches` / `dispatch_requests` / `dispatch_meta`（红线 2/6 崩溃恢复与脱敏审计）；
   - 非空快照清单 `artifacts/snapshots/<run_id>.json`（`changes != []`）、任何 `artifacts/undo/*.json`、`memories` / `memory_blobs`（记忆擦除只走 `memory.erase` + Receipt）、`content_objects` / `content_grants`；
   - `ReferenceIndex.protected_blobs` 中的全部受保护文件；
   - `budget_accounts` / `budget_limits` / `budget_counters` 聚合账本，以及任何 `state != 'settled'` 或 `known == 0` 的 `budget_reservations`（红线 5/11）。
4. **仅允许清理的四类安全目标 + WAL 截断**：
   - **孤儿 blob**：`artifacts/blobs/*` 与根 `blobs/*` 下不在 `protected_blobs` 内的普通无链接文件；
   - **超期空快照清单**：`artifacts/snapshots/<run_id>.json` 满足 `changes == []`、所属 `Run` 已处终态且 `created_at < cutoff_ms`、且该 `run_id` 无文件类 `receipt` 引用；
   - **超期终态 `jobs` 行**：`state IN ('done', 'cancelled')`、所属 `Run` 为终态、该 `Run` 下 `0` 条活跃 `dispatches`（`intent`/`executing`）、且 `COALESCE(updated_at, created_at) < cutoff_ms`；
   - **超期已结清且用量已知的 `budget_reservations` 行**：`state == 'settled' AND known == 1`、所属 `Run` 为终态、且 `COALESCE(settled_at, created_at) < cutoff_ms`（保留 `budget_counters` 累计事实不变）；
   - 表行删除单事务提交、文件删除及父目录同步完成后，执行 `PRAGMA wal_checkpoint(TRUNCATE)` 原地回收 `-wal` 体积（不做需要双倍临时空间的 `VACUUM`）。

### 为什么改
- 在不触碰红线 1/2/5/6/11、不放宽 `summary_versions` 触发器、不破坏任何历史回放与未知计费对账的前提下，落地维护者拍板的 90 天默认保留期与孤儿文件离线回收闭环。

### 影响哪些代码模块
- `src/tianshu/backup.py`（扩展孤儿扫描覆盖根 `blobs/*` 并保持 `ReferenceUnknown` fail-closed）
- `src/tianshu/data_cli.py`（新增 `gc` 子命令预演/批准执行与 JSON/人话报告）
- `tests/test_data_cli.py` / `tests/test_backup.py`（禁删面零改动不变量、并发锁忙、`ReferenceUnknown` 零删负控与低磁盘下正常回收测试）

---

## 3. W9 v2.0 · 网络分页读取与连接期 DNS 防重绑契约（PRD §5.2.3 + [network-g2.md](network-g2.md) §3/§4/§7）

### 原方案
- `network.fetch`（`1.0`）输入仅有 `{"url"}`，单次上限与摘要窗口写死在进程级 `NetLimits`，模型无法按需缩小单次预占字节或翻页预览，输出也不含分页游标。
- `read_content`（`1.0`）仅支持严格 UTF-8 文本解码，抓回来的二进制或非 UTF-8 内容（PDF、图片、Latin-1 报文）虽已落 `content://sha256:<hex>`，但模型没有任何无损分页读取手段。
- `net.py` 直接把 `target.host` 交给 `http.client.HTTP(S)Connection`，既未拦截域名解析到私有/回环/链路本地云元数据地址（SSRF），也存在检查与连接二次解析的 DNS rebinding TOCTOU 窗口。

### 新方案
1. **按红线 8 升版 C2 工具契约（`1.0` → `2.0`，旧审批摘要不跨版本复用）**：
   - **`network.fetch` `2.0`**：
     - `input_schema` 新增三个可选整数参数：`offset`（`minimum: 0`，默认 `0`）、`length`（`1..65536`，默认 `limits.summary_bytes` 即 `4096`）、`max_bytes`（`1..1048576`，有效上界 `effective_max_bytes = min(max_bytes, limits.max_bytes)`，同步用于 `prepare()` 额度预检与 `dispatch()` 字节预占）。
     - 完整抓取字节（`<= effective_max_bytes`）仍先原子落盘为 `content://sha256:<hex>` 再写 `tool.result`；返回 `output` 在既有字段基础上新增 `offset`、`page_bytes`、`next_offset`（`int | null`，UTF-8 字符边界安全切片，未读完时可直接传给 `read_content` 离线翻页而无需二次联网）。
   - **`read_content` `2.0`**：
     - `input_schema` 新增可选参数 `encoding`：`enum: ["utf-8", "utf-8-replace", "latin-1", "base64"]`（默认 `"utf-8"`，与 `1.0` 严格兼容）。
     - `"utf-8-replace"` 支持含损坏字节文本的容错分页；`"latin-1"` 支持单字节无损映射；`"base64"` 对 `[offset : offset + length]` 原始字节（单页上限 `65536` 原始字节）返回标准 RFC 4648 Base64 文本，补齐二进制内容引用的无损分页读取能力；所有编码模式统一先核 `(digest, task_id)` 可见范围与整文件 `sha256`，返回新增 `encoding` 字段并保证 `bytes` 为本次消费的原始字节数（未到末尾时 `offset + bytes == next_offset`）。
2. **连接期单次 DNS 解析校验 + Socket IP 钉死（防 DNS Rebinding & SSRF）**：
   - 在初始请求及**每一跳同源重定向**建立 TCP 连接前：
     - 若 `target.host` 为域名（非字面量 IP），仅调用一次可注入解析器（默认 `socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)`）；对返回的全部候选地址先解包 IPv4-mapped IPv6（`::ffff:x.x.x.x`），只要存在任意非全局单播地址（`not ip.is_global`，含 loopback `127.0.0.0/8`/`::1`、RFC 1918 私网、RFC 6598 CGNAT `100.64.0.0/10`、链路本地/云元数据 `169.254.0.0/16`/`fe80::/10`、ULA `fc00::/7`、多播、未指定 `0.0.0.0`/`::` 及保留段），立即在 `connect()` 之前拒绝并抛 `NetError(..., reason="net.private_address_blocked")`（字节账本按已知 `0` 字节结算，零数据包发出）。
     - 若 `target.host` 本身已是任务 `outbound_allow` 显式声明并经人工批准的字面量 IP（如本机测试夹具 `127.0.0.1:<port>`），不走 DNS 解析（天然无重绑窗口），但始终拒绝未指定地址（`0.0.0.0`/`::`）、多播与链路本地云元数据段（`169.254.0.0/16`/`fe80::/10`），同样报 `net.private_address_blocked`。
   - **Socket 直连钉死与主机身份保持**：TCP 套接字直接 `connect()` 已通过校验的 `(family, socktype, proto, sockaddr)` 字面量 IP，**绝不触发第二次 `getaddrinfo()`**；HTTP `Host` 请求头仍保留原规范 `target.host[:port]`；HTTPS 在已连上的套接字上执行 `ssl_context.wrap_socket(sock, server_hostname=target.host)`，完整保留每一跳的 TLS SNI `server_hostname` 与标准 X.509 证书链及主机名核验。

### 为什么改
- 同时解决大网页/二进制引用的分页读取缺口（无需重复外发请求）与域名白名单下的 DNS Rebinding / 内网元数据探测漏洞，且严格遵守红线 8 版本化与既有本机字面量夹具兼容。

### 影响哪些代码模块
- `src/tianshu/tools.py` / `schemas/c2/tool.json`（`network.fetch` 与 `read_content` 升 `2.0`）
- `src/tianshu/contents.py` / `src/tianshu/loop.py`（`read_content` 四种 `encoding` 分页实现）
- `src/tianshu/net.py` / `src/tianshu/dispatcher.py`（连接期单次解析、私有地址拦截 `net.private_address_blocked` 记 `0` 字节、IP 钉死连接器与分页摘要切片）
- `tests/test_network_fetch.py` / `tests/test_content_refs.py` / `tests/test_content_read.py` / `tests/test_c1_c2_c5_schemas.py`

---

## 4. W14 · 双系统进程级沙箱契约（[F2-sandbox-policy.md](F2-sandbox-policy.md) + PRD §7 F2）

### 原方案
- `exec_backends.py` 仅实现 `local`（`capabilities.isolation == "none"`）；`seatbelt` 与 `landlock` 均列在 `UNIMPLEMENTED_EXEC_BACKENDS`，本机任何非空 `ExecPolicy.isolation_requires` 一律因 `files_authority` / `network` / `credentials` 未强制而报 `PolicyUnsupported`。
- `isolation.PROBE_VERSION = "v1"`，且当任务未显式填写 `isolation_requires` 时，审批前 `ApprovalView.isolation` 不运行实测探针（仅回传 `unverified=True`）。

### 新方案
1. **内核默认不变 + 显式后端选择零静默回落**：
   - 内核默认常量保持 `DEFAULT_EXEC_BACKEND = "local"` 不变；`TIANSHU_EXEC_BACKEND` 支持 `"local"` | `"seatbelt"` | `"landlock"` | `"auto"`。
   - `"auto"` 在 macOS（Darwin）解析为 `"seatbelt"`，在 Linux（`x86_64`/`aarch64`）解析为 `"landlock"`，在 Windows 或其他无对应原生沙箱的平台直接抛 `PolicyUnsupported(backend="auto", feature="exec_backend")`；**任何配置在目标沙箱不可用时一律抛 `PolicyUnsupported`，绝不静默回落到 `local`**。
2. **macOS `seatbelt` 后端（Darwin SBPL，`capabilities.name = "seatbelt"`，`isolation = "app_container"`）**：
   - 基于 Darwin Seatbelt SBPL 配置启动受限进程组（保留 `argv` 数组直传、禁 shell 拼接、POSIX 进程组杀树 `process_tree`）：
     - `files_authority`：默认禁止写入，仅放行规范化后的 `policy.cwd_roots` 与每进程私有临时区；显式禁止读写工作区外用户目录、`data_root`（含 `events.db*`、`artifacts/`、`blobs/`、`memory_blobs/`、`run/ipc.key` 及 IPC socket）、`~/.tianshu/`（`authority`、`restore-registry-v1`）及维护锁目录；
     - `network`：`(deny network*)` 全面阻断出站/入站 TCP/UDP/UNIX 套接字（含本机 loopback）；
     - `credentials`：后端强制环境清洗 + SBPL 阻断 `~/Library/Keychains/**`、`~/.ssh/**`、`~/.aws/**` 及 `com.apple.securityd` 等系统钥匙串服务访问。
3. **Linux `landlock` 后端（Linux `x86_64` / `aarch64` 纯标准库 `ctypes` 系统调用，`capabilities.name = "landlock"`）**：
   - 子进程 `execve` 前强制 `prctl(PR_SET_NO_NEW_PRIVS, 1)` + `landlock_create_ruleset` / `landlock_add_rule` / `landlock_restrict_self`：
     - **Landlock ABI >= 4（Linux >= 6.7）**：启用文件系统白名单 + TCP 网络隔离（`LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP` 零放行规则），如实声明 `capabilities.isolation = "app_container"`、`supports_network_isolation = True`；
     - **Landlock ABI 1..3（Linux 5.13..6.6）**：内核不支持 TCP 规则，如实声明 `capabilities.isolation = "process_tree"`、`supports_filesystem_isolation = True`、`supports_network_isolation = False`（请求 `network` 边界的任务由隔离门诚实拒绝，绝不谎报 `app_container`）；
     - **Linux `< 5.13`、Landlock 未启用或非 `x86_64`/`aarch64` 架构**：构造/解析期直接抛 `PolicyUnsupported(backend="landlock", feature="exec_backend")`。
4. **后端强制凭据清洗 + OS 凭据库封锁 + `PROBE_VERSION = "v2"` + 审批前实测视图**：
   - `seatbelt` 与 `landlock` 在 `spawn()` 内部（独立于 `ExecPolicy.env_allowlist`）强制剥离宿主敏感环境变量（`TIANSHU_*` 含探针哨兵、`*_API_KEY`、`*_SECRET*`、`*_TOKEN*`、`*_PASSWORD*`、`SSH_AUTH_SOCK`、`GPG_AGENT_INFO`、`DBUS_SESSION_BUS_ADDRESS` 等），并封锁 OS 级凭据存储路径/服务。
   - `isolation.PROBE_VERSION` 升为 `"v2"`（缓存键自动失效旧证据，增强 `files_authority` / `credentials` 对权威根与凭据面的实测覆盖）。
   - **审批前实测规则**：当 `backend.capabilities.isolation != "none"` 且存在 `data_root` 时，即使 `policy.isolation_requires` 为空，`dispatcher._precheck_exec` 与执行侧也必须调用 `probe_backend_cached` 获取绑定当前 `(workspace, data_root, PROBE_VERSION="v2")` 的实测 `IsolationReport`，使审批前 `ApprovalView.isolation` 直接展示实测 `measured` 等级与 `enforced_boundaries`（`unverified=False`），并在后端声明与实测矛盾时立即标出 `contradicted=True`；`local`（`isolation == "none"` 且 `isolation_requires` 为空）保持零探针开销原路径不变。

### 为什么改
- 在不引入任何第三方运行时依赖、不违背「能力必须由真实子进程探针证明、做不到绝不回落 `local`」原则的前提下，为 macOS 与现代 Linux 提供可被四边界探针独立核实的进程级沙箱后端。

### 影响哪些代码模块
- `src/tianshu/exec_seatbelt.py` / `src/tianshu/exec_landlock.py`（新增纯标准库平台沙箱后端与凭据清洗/OS 凭据封锁）
- `src/tianshu/exec_backends.py`（注册 `seatbelt` / `landlock`、支持 `auto` 解析、放宽 `_EXPECTED_ISOLATION["landlock"]` 为 `{"app_container", "process_tree"}` 以如实表达 ABI 1..3）
- `src/tianshu/isolation.py` / `src/tianshu/dispatcher.py` / `src/tianshu/tools.py`（`PROBE_VERSION = "v2"` 与 `backend.capabilities.isolation != "none"` 时的审批前实测探测接线）
- `tests/test_exec_backends.py` / `tests/test_isolation.py` / `tests/test_isolation_gate.py` 及新增平台沙箱正负控测试
