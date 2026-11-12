# G2 MCP 阶段验收报告（2026-11-12）

- 施工规格：[`SPEC-G2-MCP-2026-11-12.md`](SPEC-G2-MCP-2026-11-12.md)（v2，经两轮独立复审整改）。
- 设计说明与边界：[`mcp-g2.md`](mcp-g2.md)。
- 交付 PR：**#115**（规格）、**#116**（客户端，已合并 `41ee0ae`）、**#117**（注册表与准入，已合并 `55848ec`）、**#119**（派发链路 + 契约 + CLI + 文档）。
  （#118 是并行工作流「子代理阶段施工规格」，与本轮无关，登记在此避免编号混淆。）
- 基线：`main @ 0f30d01`（F2 收口），本机实测 **1905 passed / 7 skipped**；
  交付后本机实测 **1999 passed / 7 skipped**（macOS arm64），UI `vitest` **51 passed** 不变，
  `ruff check .` 干净，运行时依赖仍为零（`dependencies = []`）。
- 收尾轮（本 PR）实测：**2044 passed / 7 skipped**（macOS arm64，359.79s；同机基线
  `main @ 6140d9c` = 2038/7，差值是本轮 6 条崩溃/恢复判据）。
- **收尾轮（PR #122）**：补 MCP 崩溃夹具与恢复判据——c10 由「部分」转为**实测**、
  NB21 落地；顺带修掉复审 R2 指出的"恢复补写结果却不推进派发行"（见 §2-7）。
- 逐条对账口径：**有回归测试 + 负控实测红**才算做完；判据编号与规格 §4 一一对应。

## 1 判据逐条对账（c1–c15）

| 编号 | 判据 | 落点（测试） | 结论 |
|---|---|---|---|
| MCP-c1 | 无 MCP 配置时工具面逐字节不变（manifest 15 项 / `run.start.tools` 19 项 / veto 字面集合 15 项） | `test_registry_without_clients_equals_builtins`、`test_without_mcp_config_the_surface_is_byte_identical`、`test_model_tool_surface_has_no_run_start` | ✅ 通过（顺序也钉住：manifest 用 list 相等断言） |
| MCP-c2 | 配置非法（未知键 / 相对 `argv[0]` / 名字越界 / argv 空 / 相对配置文件路径）→ 启动期拒绝，一个进程都不 spawn | `test_config_validation_is_fail_closed`（7 参数）、`test_config_rejects_relative_command`、`test_relative_config_env_path_is_refused` | ✅ 通过 |
| MCP-c3 | 协议版本不在支持集合 → server 不可用、零准入，诊断原样记版本 | `test_unknown_protocol_version_is_refused_not_downgraded`、`test_clients_start_all_isolates_one_bad_server` | ✅ 通过 |
| MCP-c4 | 方言外 schema 关键字 → 只拒该工具，其它工具照常准入 | `test_normalize_rejects_unsupported_shapes`（8 参数）、`test_rejection_does_not_take_other_tools_down`、`test_mcp_list_surfaces_rejected_tools_with_reason` | ✅ 通过 |
| MCP-c5 | `additionalProperties` 缺省 → 显式 `false` 且记 `narrowed`；多传字段被拒 | `test_normalize_narrows_additional_properties`、`test_normalize_keeps_explicit_additional_properties_true_as_is` | ✅ 通过（收窄只发生在这一处，+`normalize_object_schema` 编译复检） |
| MCP-c6 | 工具 id 映射冲突（两段都查）→ 两者都不准入 | `test_tool_id_collision_rejects_both_sides`、`test_tool_collision_covers_tool_segment_too` | ✅ 通过 |
| MCP-c7 | server 改 schema/描述 → `tool_version` 变 → 旧批准失效 | `test_tool_version_binds_the_raw_schema`、`test_tool_version_changes_with_description` | ✅ 通过（版本 = 原始 schema + 归一 schema + 描述 的摘要在纯函数上钉住） |
| MCP-c8 | 没声明 `--allow-mcp-server` → 不弹批准，`dispatch.rejected` = `c4.mcp_not_allowed` | `test_mcp_call_without_task_allowlist_is_rejected_before_approval` | ✅ 通过 |
| MCP-c9 | 声明了 → 弹批准（`c4.mcp_call`）→ 执行一次 → intent + result + receipt(backend=mcp)，且凭据**可独立核对**（含响应摘要） | `test_approved_mcp_call_runs_once_and_produces_receipt` | ✅ 通过（真子进程调用日志证明只发一次；`mcp_call` 证据带 `sha256` 响应摘要并与执行摘要逐字核对；`verify_receipts == []`） |
| MCP-c10 | 超时/崩溃/断线 → `failed` + `unknown`，可核查 | 派发侧：`test_call_timeout_is_unknown_not_none_applied`、`test_crash_during_call_raises_closed`、`test_runtime_protocol_failure_reaps_the_process`；**恢复侧（收尾轮新增）**：`test_crash_after_server_executed_settles_unknown_and_blocks`、`test_crash_before_send_is_indistinguishable_and_still_blocks`、`test_blocked_workspace_can_be_unblocked_and_run_finishes_without_replay`、`test_ise_error_result_is_applied_and_recovery_leaves_it_alone` | **✅ 通过（实测，收尾轮）**：真子进程在「响应已收到、结果未落盘」处硬退（server 的 `--effect-log` 证明它真的执行了）→ 恢复补 `status/effect_state=unknown` + `reason=mcp.crash_unsettled` + 阻断该 workspace + **不补凭据** + **不重放**（call-log 仍 1 次）+ 幂等（再恢复一次无新事件）；派发行从 `intent` 推到 `error`；`assess_run` 只读拒绝接手；`isError` 已有 result 的调用不被重判。「请求没发出去」的窗口照样 unknown + 阻断（恢复没有证据区分——见 §4-10 的代价说明） |
| MCP-c11 | 已取消的 Run 一条 `tools/call` 都不发，`none_applied` | `test_cancelled_run_sends_no_mcp_frames` | ✅ 通过（假 server 的调用日志为空） |
| MCP-c12 | 结果超上限 → 截断 + 字节记账；非文本只记类型不丢（**规格已改成不落内容引用**） | `test_max_result_bytes_truncates_and_reports_bytes`、`test_max_result_bytes_counts_utf8_bytes_not_characters`、`test_structured_content_counts_against_the_result_budget`、`test_call_tool_returns_text_and_keeps_unsupported_content` | ✅ 通过（与**改后的**判据一致：截断 + `bytes`/`dropped_bytes`/`dropped_blocks`，不落 `content://`；见 §4-5） |
| MCP-c13 | `isolation_requires` 非空 → 任何平台都 `c4.mcp_isolation_unproven` | `test_isolation_requires_makes_mcp_unproven` | ✅ 通过（不因后端自称而放行） |
| MCP-c14 | `tianshu mcp list --json` 如实列 server 状态 / 协议版本 / 准入与被拒工具与原因 / 截断 | `test_mcp_list_reports_servers_tools_and_protocol`、`test_mcp_list_surfaces_rejected_tools_with_reason`、`test_mcp_list_reports_unconfigured_without_starting_anything` | ✅ 通过 |
| MCP-c15 | 回放不碰 server：**装配探针**记录 `start_all()` 只在装配点发生 1 次，回放后仍为 1；调用日志前后一致 | `test_replay_does_not_construct_or_start_clients`、`test_replay_does_not_talk_to_the_server` | ✅ 通过 |

## 2 契约与口径变更（逐条）

1. `tool.intent.tool_id` / `tool.not_dispatched.tool_id` / `receipt.tool_id`：**枚举 → 形状
   pattern**（`^[a-z][a-z0-9_.]*$`，与 `tool.result.tool_id` 逐字相同）。旧事件仍合法；
   放宽的是**数据契约**，白名单仍由注册表判定。`schemas/c3/event.json` 重新生成，
   `tool.intent.args` / `tool.result.output` 仍是开放对象不变。
2. `task.created.mcp_servers`：字符串数组、**新写恒写**（空也写），`tasks` 表加列
   （旧行缺键 → 读取侧按空元组，语义 = 一个都不许）。**两件事必须分开说**：
   `tasks` 表旧行补列后读成空元组（已测）；而 `task.created` **事件**的当前 v1 契约把
   `mcp_servers` 放在 `required` 里，升级前写入的那些 v1 行缺这个键——读取路径不重校验，
   所以**读得出来**，但按当前 v1 严格校验它们**不再是合法 payload**。这不是"向前兼容的
   合法升级"，而是"读取侧宽容 + 当前契约更严"；谁要是拿 `payload_errors(v1)` 去复核历史
   行，会如实地拿到失败。
3. `receipt.backend` += `"mcp"`；`receipt.evidence[].kind` += `"mcp_call"`
   （可选字段 `server` / `tool` / `request_id`，响应摘要复用既有 `sha256`）。
4. 新规则编号 6 个：`c4.mcp_not_allowed`、`c4.mcp_isolation_unproven`、
   `dispatch.mcp_resource_mismatch`、`dispatch.mcp_server_unavailable`、
   `dispatch.mcp_tool_unknown`、`dispatch.mcp_protocol_error`；`RULE_IDS` / `RULE_GROUPS` /
   `schemas/c4/rules.json` / PRD §5.4.1 四处同步。
5. `dispatcher.EXECUTABLE_KINDS` += `mcp`；`resource-kinds.md` §4/§7 与
   `tool-admission.md` §7/§8 的"没有执行器"口径改写。
6. 工具面判据（`test_veto_scope.py`）：基线保持 15 项字面集合；动态工具只在配置 MCP 时
   出现，故该判据的"没有新增可见工具"结论**在没有 MCP 配置时仍成立**。

## 3 负控（回退即红，逐条实测）

| 编号 | 回退（把保护拿掉） | 目标用例 | 实测 |
|---|---|---|---|
| NB1 | 子进程继承全量环境变量 | `test_child_env_only_carries_path_and_declared_keys` 等 | **RED**（2 failed） |
| NB2 | 允许相对 `argv[0]` | `test_config_rejects_relative_command` | **RED** |
| NB3 | 不认识的协议版本也照收 | `test_unknown_protocol_version_is_refused_not_downgraded` 等 | **RED** |
| NB4 | 去掉单条消息上限 | `test_oversized_single_message_is_refused` | **RED** |
| NB5 | 畸形 JSON 跳过 | `test_malformed_json_marks_server_failed` | **RED** |
| NB6 | JSON-RPC 错误当成功 | `test_protocol_rejection_error_code_is_marked` | **RED** |
| NB7 | EOF 不唤醒等待方 | `test_crash_during_call_raises_closed` | **RED**（5.05s） |
| NB8 | stderr 不设上限 | `test_stderr_is_captured_but_bounded` | **RED** |
| NB9 | 关闭不 terminate/kill | `test_close_is_idempotent_and_reaps_the_process` | **RED**（4.05s） |
| NB10 | 发现期 `max_tools` 不生效 | `test_max_tools_is_enforced_at_discovery` | **RED** |
| NB11 | `max_result_bytes` 不截断 | `test_max_result_bytes_truncates_and_reports_bytes` | **RED** |
| NB12 | 协议失败不收尸 | `test_runtime_protocol_failure_reaps_the_process` | **RED** |
| NB13 | 晚到响应不去重 | `test_late_response_after_timeout_does_not_poison_the_next_call` | **RED** |
| NB14 | `mcp` 从 `EXECUTABLE_KINDS` 拿掉 | c9 用例 | **RED**（0.22s） |
| NB15 | 去掉任务 server 白名单 | c8 用例 | **RED**（0.93s） |
| NB16 | `_classify_mcp` 改成 AUTO | c9 用例 | **RED**（0.30s） |
| NB17 | 超时按 `none_applied` 结算 | c10 用例 | **RED**（0.45s） |
| NB18 | 取消检查挪到发包之后 | c11 用例 | **RED**（0.25s） |
| NB19 | `task.created` 不写 `mcp_servers` | 任务记录用例 | **RED**（0.23s） |
| NB20 | `tool_id` 不放开（保持枚举） | c9 用例 | **RED**（0.24s） |
| NB21 | 撤掉 `_recover_run` 里的 MCP 分支（`if False`），同时允许 MCP 工具 id 进静态 `SPECS`——即"恢复只靠静态表缺条目认出 MCP"的退化 | `test_mcp_tool_in_static_specs_is_still_not_treated_as_a_file_tool`、`test_crash_after_server_executed_settles_unknown_and_blocks` | **RED（收尾轮实测）**：2 failed / 0.66s ——空清单里没有本次条目，退化后会判 `none_applied`（把可能已执行的 MCP 调用洗成"确定没写"），且崩后结算里丢掉了 `reason=mcp.crash_unsettled` 这条可诊断结论 |
| NB22 | `isolation_requires` 放行 | c13 用例 | **RED**（0.20s） |
| NB23 | replay 装配里调用 `start_all()` | **未做**（c15 只证明了"回放不调用 server"，没有把装配改成反面） | **未验证** |
| NB24 | MCP 凭据按 exec 口径核对 | c9 的凭据核对 | **RED**（0.22s） |
| NB25 | 拿掉 `dispatcher` 里 `failpoint("mcp_after_call")`（崩溃窗口不复存在） | c10 的崩溃夹具 | **RED（收尾轮实测）**：1 failed / 0.68s —— 用例断言 rc=77 + stderr 注入标记，"没崩在响应之后"当场暴露 |
| NB26 | 恢复把派发行一律写 `done`（unknown 也写成功） | c10 崩溃夹具 + 文件写派发行终态用例 | **RED（收尾轮实测）**：2 failed / 0.71s |
| NB27 | 恢复只写 `tool.result`、不推进派发行（回到 R2 之前的口径） | 同上 | **RED（收尾轮实测）**：2 failed / 0.66s |

> 负控脚本自身的坑（PR B 实测踩到）：同尺寸同 mtime 秒的改写会让 Python 复用旧
> `__pycache__` → **假绿**。脚本清 `__pycache__` + `PYTHONDONTWRITEBYTECODE=1`，
> 并逐条记录返回码与耗时。
>
> 收尾轮又踩到第二种坑：还原用 `git checkout -- <file>` 回到的是 **HEAD**，会把同一轮里
> **尚未提交**的改动一起抹掉（本轮把 `recovery.py` / `dispatcher.py` 的新代码抹过一次，靠
> 重放补丁修回来）。负控的还原必须是"变异前自己拍的文件快照"，不能借用 git 的 HEAD。

## 4 诚实边界（交付后仍然不成立 / 未验证）

1. **真实第三方 MCP server 兼容性未验证**。本轮**无法联网核验官方规范**（本机 web
   provider 未注册），实现按 MCP 2025-06-18 的已知形状写并与**自写假 server** 对齐；
   自测通过不是兼容性证据。协议版本集合是显式常量，认不出即拒绝。
2. **没有沙箱**：server 以当前用户权限运行，能读写本机文件、联网、继承 `PATH` 以便
   其子进程工作。三道门（用户配置 × 任务白名单 × 逐次批准）防的是"模型自作主张"，
   **不防恶意 server**。`CAPABILITY_UNTRUSTED_LOCAL_MCP` **未启用**（它的判据是四边界
   `enforced` 实测证据，而 v1 没有）。
3. **MCP 进程不经过 `exec_backends` 注册表**（长连接 vs 一次性 `spawn`），输出里
   `via_exec_backend=false` 如实标注。因此 `isolation_requires` 对 MCP **一律拒绝**，
   不拿 exec 后端的实测报告跨对象作证。
4. **只做 stdio + tools**：HTTP/SSE、OAuth、resources/prompts/sampling/roots/logging/
   progress、`list_changed` 动态刷新全部未做。同 server 调用串行。
5. **大结果不落内容引用**：客户端按 `max_result_bytes` 截断并报 `bytes` /
   `dropped_bytes`，**没有** `content://` 引用（与 `run_command`/网络的机制不同）；
   图片/音频/资源正文只记类型。
6. **没有 MCP 预算维度**：调用不预占账本，server 的 CPU/内存/磁盘占用无上限（只有
   结果大小、超时、`max_tools` 三个上限）。
7. **Windows 未实机验证**：stdio 管道与线程只在 `windows-latest` 上跑，而 Windows 本来
   就是已知基线红；不把 CI 的绿/红当实机证据。
8. **UI 没有 MCP 面板**：审批卡片能看到 `mcp://` 目标，`tianshu mcp list` 是命令行诊断。
9. **回放只读**：不重连 server、不重放调用；事件里只有结果摘要（`mcp:<sha256>`）与
   有界文本，**没有** server 原始响应全文。
10. **崩溃后的 MCP 结论只到 `unknown` + 阻断，"没发出去"也算在内**（收尾轮已用真子进程
    夹具实测，见 §1-c10）。仍然不成立的：
    - 恢复**分不出**"请求没写进 server stdin"与"写进去了、响应丢了"：本机没有任何持久化
      证据说明"这一帧发出去了"，所以两者都判 `unknown` + 阻断该 workspace。**代价**是可能
      过度阻断（一次根本没发出去的调用也要人工核对后 `unblock`）；**方向**是对的（宁可多
      挡，不把可能已发生的副作用说成没发生）。要消除它需要"发送前落一条持久标记"，那是
      契约增量（§5-7）。
    - 阻断对象只有 workspace：MCP server 以当前用户权限跑在本机，这个工作区**可能**被它
      改过，所以按最保守口径挡这个 Run 声明的工作区。server 侧自己的状态（它动过哪些
      文件、发过什么请求）**没有任何证据**，恢复也不去问它（问了也不是本机证据）。
    - 没有对象级阻断（以 `mcp://server/tool` / server 为阻断对象）：现有 guard 只有
      workspace 字段（§5-8）。
11. **等批准期间取消不立即生效**：`tools/call` 是同步阻塞调用，取消只在发包前检查
    一次；等响应期间取消要等这次调用结算（超时或返回）才生效。已发出的 RPC 无法
    撤回（同网络阶段的"在途 TCP 字节无法撤回"）。
12. **`max_tools` 两层都判**：客户端发现阶段截断（并报 `<truncated>` 拒绝行），准入层
    再判一次；截断发生在客户端时，被截掉的工具名我们根本收不到——拒绝行只能说明
    "还有更多，被截断了"，不能列出名字。
13. **Windows arm64 / 安装升级 / 签名**等 G3 事项与本轮无关，仍未做。
14. **跨进程注册表一致性**：`build_stack` 现在**总是**设进程级注册表（没有 MCP 时设成
    纯内置表），同进程先后装配不会互相继承动态工具；但多进程各有一份注册表，跨进程
    一致性靠"同配置同准入"，未做跨进程比对。
## 5 后续工单

1. 把长连接 spawn 收进 `exec_backends` 接缝（`spawn_session`）+ MCP 专属隔离探针，之后
   才谈 `isolation_requires` 对 MCP 成立。
2. 真实第三方 server 兼容性验证（≥2 个真实 server）+ 官方规范复核协议版本/`initialize`。
3. ~~MCP 崩溃夹具（intent 无 result）+ 恢复判据（补 NB21）~~ **已完成（收尾轮，本 PR）**：
   `tests/test_mcp_crash.py` 6 条（3 条真子进程崩溃用例 + `isError` 不重判 + 静态表误装 +
   文件写派发行终态），NB21/NB25/NB26/NB27 负控实测红。
4. 大结果内容引用与文本分页读取；MCP 预算维度（调用次数/结果字节）。
5. UI 的 MCP 面板（server 状态、准入/被拒工具、截断说明）。
6. Windows 实机验证与 CI 基线修复（既有工单）。
7. **发送前持久标记**：让恢复能区分"未发出 → `none_applied` + 不阻断"与"已发出 → `unknown`
   + 阻断"，消掉 §4-10 的过度阻断。需要事件契约增量（新事件或 `tool.intent` 扩展字段）
   与规格修订，不能只改恢复的推断。
8. **对象级阻断**：以 `mcp://server/tool` 或 server 作为阻断对象。现有 guard 只有 workspace
   字段，先决定阻断对象模型再谈"精准阻断"；不拿调用方传进来的路径冒充 MCP 的目录。