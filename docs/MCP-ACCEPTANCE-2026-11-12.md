# G2 MCP 阶段验收报告（2026-11-12）

- 施工规格：[`SPEC-G2-MCP-2026-11-12.md`](SPEC-G2-MCP-2026-11-12.md)（v2，经两轮独立复审整改）。
- 设计说明与边界：[`mcp-g2.md`](mcp-g2.md)。
- 交付 PR：**#115**（规格）、**#116**（客户端）、**#117**（注册表与准入）、**#118**（派发链路 + 契约 + CLI + 文档）。
- 基线：`main @ 0f30d01`（F2 收口），本机实测 **1905 passed / 7 skipped**；
  交付后本机实测 **1998 passed / 7 skipped**（macOS arm64），UI `vitest` **51 passed** 不变，
  `ruff check .` 干净，运行时依赖仍为零（`dependencies = []`）。
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
| MCP-c9 | 声明了 → 弹批准（`c4.mcp_call`）→ 执行一次 → intent + result + receipt(backend=mcp) | `test_approved_mcp_call_runs_once_and_produces_receipt` | ✅ 通过（真子进程调用日志证明只发一次；`verify_receipts == []`） |
| MCP-c10 | 超时/崩溃/断线 → `failed` + `unknown`，可核查 | `test_call_timeout_is_unknown_not_none_applied`、`test_crash_during_call_raises_closed`、`test_runtime_protocol_failure_reaps_the_process` | ✅ 通过；**恢复链路靠 recovery 的未知工具分支**（见 §4-10） |
| MCP-c11 | 已取消的 Run 一条 `tools/call` 都不发，`none_applied` | `test_cancelled_run_sends_no_mcp_frames` | ✅ 通过（假 server 的调用日志为空） |
| MCP-c12 | 结果超上限 → 截断 + 字节记账；非文本只记类型不丢 | `test_max_result_bytes_truncates_and_reports_bytes`、`test_max_result_bytes_counts_utf8_bytes_not_characters`、`test_structured_content_counts_against_the_result_budget`、`test_call_tool_returns_text_and_keeps_unsupported_content` | ✅ 通过（**不落内容引用**，见 §4-5） |
| MCP-c13 | `isolation_requires` 非空 → 任何平台都 `c4.mcp_isolation_unproven` | `test_isolation_requires_makes_mcp_unproven` | ✅ 通过（不因后端自称而放行） |
| MCP-c14 | `tianshu mcp list --json` 如实列 server 状态 / 协议版本 / 准入与被拒工具与原因 / 截断 | `test_mcp_list_reports_servers_tools_and_protocol`、`test_mcp_list_surfaces_rejected_tools_with_reason`、`test_mcp_list_reports_unconfigured_without_starting_anything` | ✅ 通过 |
| MCP-c15 | 回放不碰 server：调用日志在回放前后完全一致 | `test_replay_does_not_talk_to_the_server` | ✅ 通过（读的事件只读） |

## 2 契约与口径变更（逐条）

1. `tool.intent.tool_id` / `tool.not_dispatched.tool_id` / `receipt.tool_id`：**枚举 → 形状
   pattern**（`^[a-z][a-z0-9_.]*$`，与 `tool.result.tool_id` 逐字相同）。旧事件仍合法；
   放宽的是**数据契约**，白名单仍由注册表判定。`schemas/c3/event.json` 重新生成，
   `tool.intent.args` / `tool.result.output` 仍是开放对象不变。
2. `task.created.mcp_servers`：字符串数组、**新写恒写**（空也写），`tasks` 表加列
   （旧行缺键 → 读取侧按空元组，语义 = 一个都不许）。
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
| NB21 | MCP 工具塞进 `SPECS`（让 recovery 认为"已知"） | **未做**（缺一条"intent 无 result 的 MCP 崩溃"夹具） | **未验证** |
| NB22 | `isolation_requires` 放行 | c13 用例 | **RED**（0.20s） |
| NB23 | replay 装配里调用 `start_all()` | **未做**（c15 只证明了"回放不调用 server"，没有把装配改成反面） | **未验证** |
| NB24 | MCP 凭据按 exec 口径核对 | c9 的凭据核对 | **RED**（0.22s） |

> 负控脚本自身的坑（PR B 实测踩到）：同尺寸同 mtime 秒的改写会让 Python 复用旧
> `__pycache__` → **假绿**。脚本清 `__pycache__` + `PYTHONDONTWRITEBYTECODE=1`，
> 并逐条记录返回码与耗时。

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
10. **恢复链路靠既有保守分支**：MCP 工具**故意不进** `SPECS`，`recovery.py` 的"未知
    工具"分支给出 `unknown` + 阻断待核查。**没有**针对 MCP 的崩溃夹具（NB21 未做），
    所以"崩溃后判定"这条只在既有 recovery 语义上成立，**未在本轮实测**。
11. **`max_tools` 两层都判**：客户端发现阶段截断（并报 `<truncated>` 拒绝行），准入层
    再判一次；截断发生在客户端时，被截掉的工具名我们根本收不到——拒绝行只能说明
    "还有更多，被截断了"，不能列出名字。
12. **Windows arm64 / 安装升级 / 签名**等 G3 事项与本轮无关，仍未做。

## 5 后续工单

1. 把长连接 spawn 收进 `exec_backends` 接缝（`spawn_session`）+ MCP 专属隔离探针，之后
   才谈 `isolation_requires` 对 MCP 成立。
2. 真实第三方 server 兼容性验证（≥2 个真实 server）+ 官方规范复核协议版本/`initialize`。
3. MCP 崩溃夹具（intent 无 result）+ 恢复判据（补 NB21）。
4. 大结果内容引用与文本分页读取；MCP 预算维度（调用次数/结果字节）。
5. UI 的 MCP 面板（server 状态、准入/被拒工具、截断说明）。
6. Windows 实机验证与 CI 基线修复（既有工单）。