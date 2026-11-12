# G2 MCP 阶段验收报告（2026-11-12）

- 施工规格：[`SPEC-G2-MCP-2026-11-12.md`](SPEC-G2-MCP-2026-11-12.md)（当前 v5；v2 经两轮独立复审整改，v3/v4/v5 分别是收尾轮、外发边界标记、对象级阻断的增量）。
- 设计说明与边界：[`mcp-g2.md`](mcp-g2.md)。
- 交付 PR：**#115**（规格）、**#116**（客户端，已合并 `41ee0ae`）、**#117**（注册表与准入，已合并 `55848ec`）、**#119**（派发链路 + 契约 + CLI + 文档）。
  （#118 是并行工作流「子代理阶段施工规格」，与本轮无关，登记在此避免编号混淆。）
- 基线：`main @ 0f30d01`（F2 收口），本机实测 **1905 passed / 7 skipped**；
  交付后本机实测 **1999 passed / 7 skipped**（macOS arm64），UI `vitest` **51 passed** 不变，
  `ruff check .` 干净，运行时依赖仍为零（`dependencies = []`）。
- 收尾轮（PR #122）实测：**2047 passed / 7 skipped**（macOS arm64；同机基线
  `main @ 6140d9c` = 2038/7，差值是本轮 9 条崩溃/恢复判据）。
- **外发边界标记（PR #124）**实测：**2098 passed / 7 skipped**（macOS arm64，364.17s；
  同机基线 `main @ 1eeb5fd` = **2083 / 7**，362.44s；差值 +15 = `test_mcp_crash.py`
  9 条 → 24 条）。复审两轮后补了升级兼容（`boundary_protocol`）、三条安全回退、
  「边界推进只从 intent」与「抢守卫超时按受控拒绝」的判据；UI `vitest` 随 #126
  变成 **75 passed**（本分支实测）。
- **对象级阻断（PR #129）**实测（复审整改后）：**2143 passed / 7 skipped**（macOS arm64，
  379.76s；同机基线 `main @ 2100fde` = **2108 / 7**，397.29s；差值 **+35** = 新文件
  `tests/test_mcp_object_block.py` 31 条 + 契约参数化用例 **+4**（事件类型 43→45，
  `test_event_schema.py` / `test_events.py` 各自的"逐类型"用例多跑两格）。负控
  `scripts/negative_controls_object_block.py` **23 条**全部实测红（第一轮复审后又补了
  O14–O23）；UI `vitest` **76 passed**（新增 1 条对象阻断事件摘要用例）；
  `ruff check .` 干净。
- **收尾轮（PR #122）**：补 MCP 崩溃夹具与恢复判据——c10 由「部分」转为**实测**、
  NB21 落地；顺带修掉复审指出的"恢复补写结果却不推进派发行"（R2）与"终态派发行
  被改写 / 更新时间戳倒退"（R2-2、R2-3）。
- **外发边界标记（PR #124）**：MCP 请求写进 server stdin **之前**先把派发行推进到
  `executing`；恢复/接管据此分档（标记前 → `none_applied` + 不阻断；标记后或无从证明 →
  `unknown` + 阻断），撤销与发送抢同一把执行守卫。把 §4-10 的过度阻断从"整段调用"缩到
  "标记已落盘、请求还没写"这一小段（该窗口仍然保守，见 §4-10）。
- 逐条对账口径：**有回归测试 + 负控实测红**才算做完；判据编号与规格 §4 一一对应。

## 1 判据逐条对账（c1–c16）

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
| MCP-c10 | 超时/崩溃/断线 → `failed` + `unknown`，可核查 | 派发侧：`test_call_timeout_is_unknown_not_none_applied`、`test_crash_during_call_raises_closed`、`test_runtime_protocol_failure_reaps_the_process`；**恢复侧**：`test_crash_after_server_executed_settles_unknown_and_blocks`、`test_crash_after_marker_conservatively_blocks_and_refuses_revoke`、`test_crash_before_send_settles_none_applied_and_does_not_block`、`test_blocked_workspace_can_be_unblocked_and_run_finishes_without_replay`、`test_ise_error_result_is_applied_and_recovery_leaves_it_alone`、`test_refused_send_boundary_does_not_call_server`、`test_corrupt_manifest_does_not_drag_never_sent_mcp_into_unknown`、`test_mcp_without_a_dispatch_row_stays_conservative`、`test_legacy_dispatch_rows_cannot_prove_the_request_was_not_sent`、`test_finished_dispatch_row_without_a_result_is_still_unknown`、`test_assess_run_reads_the_send_marker_before_closing_its_own_log`、`test_dispatch_table_from_an_older_version_does_not_break_recovery`、`test_send_boundary_only_advances_rows_that_are_still_intent`、`test_guard_timeout_before_send_is_a_controlled_rejection` | **✅ 通过（实测，收尾轮 + PR #124）**：三个崩溃窗口结论各不相同——`mcp_before_call`（外发边界标记**没**落盘）→ `none_applied` + `mcp.not_sent` + **不阻断**（`recover` 之后可直接 `resume`，不需要 `unblock`）；`mcp_marked`（标记已落盘、请求未写）与 `mcp_after_call`（server 的 `--effect-log` 证明它真的执行了）→ `unknown` + `mcp.crash_unsettled` + 阻断。恢复补结果时把派发行推到终态、**不补凭据**、**不重放**（call-log 仍 1 次）、幂等；`assess_run` 同口径（标记前允许接管、标记后拒绝）；标记推进不上（已撤销/作废）就不发请求；`isError` 已有 result 的调用不被重判。仍有一小段窗口保守阻断，见 §4-10 |
| MCP-c11 | 已取消的 Run 一条 `tools/call` 都不发，`none_applied` | `test_cancelled_run_sends_no_mcp_frames` | ✅ 通过（假 server 的调用日志为空） |
| MCP-c12 | 结果超上限 → 截断 + 字节记账；非文本只记类型不丢（**规格已改成不落内容引用**） | `test_max_result_bytes_truncates_and_reports_bytes`、`test_max_result_bytes_counts_utf8_bytes_not_characters`、`test_structured_content_counts_against_the_result_budget`、`test_call_tool_returns_text_and_keeps_unsupported_content` | ✅ 通过（与**改后的**判据一致：截断 + `bytes`/`dropped_bytes`/`dropped_blocks`，不落 `content://`；见 §4-5） |
| MCP-c13 | `isolation_requires` 非空 → 任何平台都 `c4.mcp_isolation_unproven` | `test_isolation_requires_makes_mcp_unproven` | ✅ 通过（不因后端自称而放行） |
| MCP-c14 | `tianshu mcp list --json` 如实列 server 状态 / 协议版本 / 准入与被拒工具与原因 / 截断 | `test_mcp_list_reports_servers_tools_and_protocol`、`test_mcp_list_surfaces_rejected_tools_with_reason`、`test_mcp_list_reports_unconfigured_without_starting_anything` | ✅ 通过 |
| MCP-c15 | 回放不碰 server：**装配探针**记录 `start_all()` 只在装配点发生 1 次，回放后仍为 1；调用日志前后一致 | `test_replay_does_not_construct_or_start_clients`、`test_replay_does_not_talk_to_the_server` | ✅ 通过 |
| MCP-c16 | **对象级阻断**：MCP 调用结算 `unknown` → 同时阻断该工具（`mcp://server/tool`，从派发行 `target` 读）+ 调用方传入的目录；发外前两道核验（判定阶段 / 外发边界前）都是 `rejected` + `none_applied` 且 server 收不到请求；server 级阻断遮住全部工具；解除分开（`unblock` 不碰对象） | `test_recovery_registers_object_from_dispatch_target_not_from_tool_id`、`test_recovery_never_sent_does_not_register_object_block`、`test_recovery_without_dispatch_row_blocks_dir_but_cannot_name_the_object`、`test_recovery_legacy_row_blocks_object_but_stays_conservative_on_the_row`、`test_blocked_object_is_refused_before_any_approval_is_requested`、`test_send_boundary_guard_recheck_holds_even_if_dispatch_phase_check_is_bypassed`、`test_block_registered_while_waiting_for_approval_is_refused_before_send`、`test_server_level_block_covers_every_tool_of_that_server`、`test_block_of_another_object_does_not_block_this_one`、`test_in_run_unknown_registers_object_block`、`test_unblock_workspace_does_not_release_the_object`、`test_unblock_object_allows_the_call_again_and_is_auditable`、`test_object_block_is_cross_data_root`、`test_old_authority_database_without_objects_table_is_empty_not_broken`、`test_corrupt_authority_makes_object_checks_block_not_pass`、`test_backfill_does_not_revive_a_released_object`、`test_object_keys_match_only_themselves_not_by_prefix`、`test_library_dispatch_without_data_root_still_checks_objects`、`test_log_replay_rejects_malformed_object_keys_fail_closed`、`test_authority_row_with_bad_key_fails_closed`、`test_ensure_object_not_blocked_accepts_a_single_string`、`test_unknown_commit_takes_the_execution_guard`、`test_server_dead_at_dispatch_is_unknown_without_object_block`、`test_recovery_survives_dispatch_table_without_target_column`、`test_each_unsettled_call_gets_its_own_block_event` | **✅ 通过（实测，PR #129）**：`tests/test_mcp_object_block.py` **31 条**；假 server 的 `--call-log` 证明被拒时**一个请求都没发**，`--effect-log` 证明运行期 unknown 那条路径确实执行过。负控 `scripts/negative_controls_object_block.py` **23 条**全部实测红 |

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
7. **恢复的派发行口径**（收尾轮修正，不属于事件契约，但审计看得到）：恢复补写
   `tool.result` 时，`dispatches` 行必须同时离开 `intent/executing`——
   `applied → done`、`none_applied`/`unknown` → `error`，并且**只从
   `intent/executing` 推进**（`revoked`/`rejected`/`done`/`error` 是审计事实，不许被
   恢复改写）；`updated_at` 用恢复这一刻的时钟（不是行自己的 `created_at`，否则更新
   时间倒退）。这条对**所有工具**生效，不只 MCP——文件写的恢复分支原来漏着（复审 R2）。
8. **MCP 外发边界标记**（PR #124，同样不新增事件、不改事件契约）：真正写 server stdin
   之前，派发行从 `intent` 推进到 `executing`（`_mark_mcp_send_boundary`，只从 `intent`
   推进、与 `revoke` 共用执行守卫）。恢复/接管据此把未完成的 MCP 调用分成两档：
   `intent` / `revoked` / `rejected`（没越过边界）→ `none_applied`；
   `executing` 或无从证明（老库没有派发表、没有行、状态不认识）→ `unknown` + 阻断。
   `revoked`/`rejected` 是审计终态：结果照写、行不改写。
   **升级兼容（复审 B1 整改）**：派发表加 `boundary_protocol` 列（新行写 1；`ALTER TABLE`
   给旧行补 0）。旧版本在写请求前**不推进**派发行、撤销与发送之间也没有互斥，所以
   **旧行的 `intent`/`revoked`/`rejected` 都不能证明"没发出去"**——一律按"可能已发出"
   处理（`unknown` + 阻断）。缺列的老库按旧行读（不炸、也不当成有标记）。
   **抢守卫超时（复审第二轮整改）**：标记在 try 内，`GuardTimeout` 走 `_abort_dispatch`
   → `rejected` / `none_applied` / `dispatch.guard_timeout`（与文件写分支同一口径）。
   拿不到守卫就等于没发出去，不能记成 `unknown` 去挡目录。

9. **对象级阻断（PR #129，本阶段唯一一次"事件类型 + 规则编号"增量）**：新增事件
   `recovery.object_blocked`（`object` / `call_ids` / `reason`）与
   `recovery.object_unblocked`（`object` / `reason`）——**事件类型 43 → 45**，
   `EVENT_TYPES` / `event_schema.CURRENT_SCHEMAS` / `schemas/c3/event.json` / PRD §5.3.2
   四处同步；夹具 `minimal_v1.json` / `legacy_v0.jsonl` 覆盖到 45 个类型。新增规则编号
   `dispatch.mcp_object_blocked`——**74 → 75**，`RULE_IDS` / `RULE_GROUPS["MCP"]` /
   `schemas/c4/rules.json` / PRD §5.4.1 四处同步。**为什么不复用** `recovery.blocked` 的
   `workspace` 字段：目录回放会把 `mcp://…` 当成一个不存在的目录路径，既是假阻断也是假
   解除——两种阻断对象是两种对象模型，键的"同一个"判定也不同（`overlap_key` vs URI 逐字节）。
   权威库新增 `blocked_objects` 表；旧版本库缺这张表 = 空集（能证明：旧版本写不出这类
   记录），表在但读不出 → 按已阻断处理。**不放松**：目录阻断照旧登记，对象阻断是额外
   一层；`unblock()` 解目录但**不**解对象，对象要走 `unblock_object()`。

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
| NB21 | 撤掉 `_recover_run` 里的 MCP 分支（`if False`）——静态表里本来就有那个 MCP id（用例自己 `monkeypatch.setitem`），即"恢复只靠静态表缺条目认出 MCP"的退化 | `test_mcp_tool_in_static_specs_is_still_not_treated_as_a_file_tool`（`idempotent`/`pure` × `intent`/`executing` 四格）、`test_crash_after_server_executed_settles_unknown_and_blocks` | **RED（PR #124 实测）**：3 failed / 0.97s ——空清单里没有本次条目，退化后会判 `none_applied`（把可能已执行的 MCP 调用洗成"确定没写"），崩后结算里也丢掉了 `reason=mcp.crash_unsettled`。**口径**：正确实现 + 同一份"静态表里有 MCP id"的世界是**绿**的（它按 id 形状判）；**红的是撤掉分支之后**，两者别混着写 |
| NB28 | 只撤 `assess_run` 的 MCP 分支 | 同上（它断言 `assess_run.unresolved`） | **RED（收尾轮实测）**：1 failed / 0.42s —— 正常崩溃用例里 MCP id 不在静态表（`spec is None` → unresolved），撤掉这条分支**测不出来**，所以必须单独覆盖只读对账 |
| NB29 | 去掉 `_mark_dispatch_settled` 的 `state IN ('intent','executing')` 保护 | `test_recovery_does_not_rewrite_terminal_dispatch_rows`（`revoked` + `rejected` 两格） | **RED（收尾轮实测）**：1 failed / 0.42s —— 已撤销/已拒绝的派发行会被改写成 `done`/`error`，审计里丢掉"被撤销过"这条事实 |
| NB22 | `isolation_requires` 放行 | c13 用例 | **RED**（0.20s） |
| NB23 | replay 装配里调用 `start_all()` | **未做**（c15 只证明了"回放不调用 server"，没有把装配改成反面） | **未验证** |
| NB24 | MCP 凭据按 exec 口径核对 | c9 的凭据核对 | **RED**（0.22s） |
| NB25 | 拿掉 `dispatcher` 里 `failpoint("mcp_after_call")`（崩溃窗口不复存在） | c10 的崩溃夹具 | **RED（收尾轮实测）**：1 failed / 0.68s —— 用例断言 rc=77 + stderr 注入标记，"没崩在响应之后"当场暴露 |
| NB26 | 恢复把派发行一律写 `done`（unknown 也写成功） | c10 崩溃夹具 + 文件写派发行终态用例 | **RED（收尾轮实测）**：2 failed / 0.71s |
| NB27 | 恢复只写 `tool.result`、不推进派发行（回到 R2 之前的口径） | 同上 | **RED（收尾轮实测）**：2 failed / 0.66s |
| NB30 | 把"没越过外发边界"的三个状态清空（`proven: set[str] = set()`） | `test_crash_before_send_settles_none_applied_and_does_not_block` | **RED（PR #124 实测）**：1 failed / 0.98s —— 恢复拿不到本机证据，窗口一又变回 `unknown` + 阻断（过度阻断回归） |
| NB31 | 把"证明不了没发就按可能已发"翻成"一律没发" | `test_crash_after_marker_conservatively_blocks_and_refuses_revoke`、`test_mcp_without_a_dispatch_row_stays_conservative` | **RED（PR #124 实测）**：2 failed / 0.98s —— 标记已落盘的窗口、以及没有派发表/没有行的老库都被洗成 `none_applied`，**最危险的方向**（把可能已发生的副作用说成没发生） |
| NB32 | 拿掉发包前的外发边界标记（`boundary_marked = True`：不落标记、直接当成已推进） | 标记窗口 + 发包前撤销 | **RED（PR #124 实测）**：2 failed / 0.97s —— 崩溃在"标记之后"仍是 `intent`，恢复会判没发；撤销与发送的分界也一起消失（server 照样被打） |
| NB33 | 只认 `intent`、不认 `revoked`/`rejected` | `test_recovery_does_not_rewrite_terminal_dispatch_rows`（`rejected` 格） | **RED（PR #124 实测）**：1 failed / 0.47s —— 已撤销的调用又变回 `unknown` + 阻断（拿不出证据却硬要挡） |
| NB34 | 清单损坏分支不按 MCP 判（`if False`） | `test_corrupt_manifest_does_not_drag_never_sent_mcp_into_unknown` | **RED（PR #124 实测）**：1 failed / 0.47s —— 一份无关证据（文件快照清单）的损毁会把"请求都没写出"的调用推成 `unknown` 并挡住目录 |
| NB35 | 不看 `boundary_protocol`（把"旧行"也当成"有标记"） | `test_legacy_dispatch_rows_cannot_prove_the_request_was_not_sent`（intent/revoked/rejected 三格） | **RED（复审整改实测）**：3 failed / 0.48s —— 升级前的旧行（写请求前不推进派发行）会把"可能已执行"洗成"确定没执行"（复审 B1 的假阴性方向） |
| NB36 | 把 `done`/`error` 也算成"没越过边界" | `test_finished_dispatch_row_without_a_result_is_still_unknown`（done/error 两格） | **RED（复审整改实测）**：2 failed / 0.47s —— 行说已结算、事件里没有结果的自相矛盾形状被恢复替它选了一个结论 |
| NB37 | 只读对账先关自己的连接、再读派发标记（并静默降级） | `test_assess_run_reads_the_send_marker_before_closing_its_own_log` | **RED（复审整改实测）**：1 failed / 0.46s —— own-log 路径的读序/读不到被安静吞掉，结论从 `none_applied` 变 `unresolved` |
| NB38 | 去掉外发边界推进的 `state=intent` 前置条件 | `test_send_boundary_only_advances_rows_that_are_still_intent` | **RED（复审第二轮 S1 整改实测）**：1 failed / 0.47s —— 已撤销/已拒绝的行会被改成 `executing`，「撤销先到就不发」的分界失效（此前这条前置条件没有判据：`test_refused_send_boundary...` 把整个入口 monkeypatch 掉了） |
| NB39 | 把「抢守卫超时 = 确定没发」错映射成 `unknown` | `test_guard_timeout_before_send_is_a_controlled_rejection` | **RED（复审第二轮实测）**：1 failed / 0.51s —— 一次根本没出去的调用被记成「效果未知」并阻断目录（过度阻断的旧口径） |
| NC-O1 | 撤掉判定阶段的对象阻断拒绝（`_resolve_target` 里那行换成 `pass`） | `test_blocked_object_is_refused_before_any_approval_is_requested` | **RED（PR #129 实测）**：被阻断的调用会弹批准、批准后真发出去 |
| NC-O2 | 外发边界前的复核拿到空键（`keys = ()`，等于不检查） | `test_send_boundary_guard_recheck_holds_even_if_dispatch_phase_check_is_bypassed` | **RED（PR #129 实测）**：批准期间落下的阻断被放过去，请求真写进 server 的 stdin |
| NC-O3 | `mcp_block_keys` 只给工具级、不给 server 级 | `test_server_level_block_covers_every_tool_of_that_server` | **RED（PR #129 实测）**：一条 `mcp://<server>` 的阻断记录永远读不到 |
| NC-O4 | 把派发行 `target` 里的 `-`/`.` 也折叠成 `_`（反解工具 id 的错口径） | `test_recovery_registers_object_from_dispatch_target_not_from_tool_id` | **RED（PR #129 实测）**：阻断对象变成 `srv_a/do_thing`，不是用户配的 `srv.a/do-thing` |
| NC-O5 | 没越过外发边界的调用也登记对象阻断 | `test_recovery_never_sent_does_not_register_object_block` | **RED（PR #129 实测）**：确定没发的调用被记成「可能已执行」 |
| NC-O6 | 没有派发行时编一个对象键出来 | `test_recovery_without_dispatch_row_blocks_dir_but_cannot_name_the_object` | **RED（PR #129 实测）**：证明不了对象却登记阻断（假精确） |
| NC-O7 | 畸形对象阻断记录静默跳过（`except: continue` 同类错误） | `test_log_replay_block_and_unblock` | **RED（PR #129 实测）**：无法判定的记录被当成「没阻断」放行 |
| NC-O8 | 解除 workspace 阻断时顺手把所有对象阻断也解了 | `test_unblock_workspace_does_not_release_the_object` | **RED（PR #129 实测）**：核对过工作区文件就等于核对过 server 侧（本轮要消掉的错误口径） |
| NC-O9 | 权威根读不出来时 fail-open（对象守卫返回「没阻断」） | `test_corrupt_authority_makes_object_checks_block_not_pass` | **RED（PR #129 实测）**：坏库/锁死会让对象阻断凭空消失 |
| NC-O10 | 旧版本库没有对象表 → 返回「已阻断」 | `test_old_authority_database_without_objects_table_is_empty_not_broken` | **RED（PR #129 实测）**：升级用户第一次用就被自己挡住（缺表能被证明是空集） |
| NC-O11 | 运行期 unknown 不落对象阻断事件 | `test_in_run_unknown_registers_object_block` | **RED（PR #129 实测）**：同一个可能已执行的工具下次还能再发一次 |
| NC-O12 | 启动补登记不带对象阻断 | `test_object_block_is_cross_data_root` | **RED（PR #129 实测）**：换个 `--db` 就看不见「这个工具不许再调」（#61.1 同类逃逸） |
| NC-O13 | 守卫内对象阻断被错记成 `unknown`（`_abort_dispatch` → `_fail_unknown`） | `test_send_boundary_guard_recheck_holds_even_if_dispatch_phase_check_is_bypassed` | **RED（PR #129 实测）**：确定没发的调用变成「效果未知」，结论不诚实且多挡目录 |
| NC-O14 | 回放不校验对象键形状（`not-a-key` / 大写 / 尾斜杠都算有效） | `test_log_replay_rejects_malformed_object_keys_fail_closed` | **RED（PR #129 实测）**：畸形键被当成「不匹配的有效阻断」，真实目标被放行（fail-open） |
| NC-O15 | 单字符串参数按字符拆开 | `test_ensure_object_not_blocked_accepts_a_single_string` | **RED（PR #129 实测）**：`ensure_object_not_blocked(log, "mcp://s/t")` 静默放行 |
| NC-O16 | 补登记用 `INSERT OR REPLACE` | `test_backfill_does_not_revive_a_released_object` | **RED（PR #129 实测）**：已人工解除的记录被日志里的旧阻断复活（过度阻断） |
| NC-O17 | 对象守卫改成前缀匹配 | `test_object_keys_match_only_themselves_not_by_prefix` | **RED（PR #129 实测）**：`mcp://a` 的阻断把 `mcp://ab/*` 一起挡住 |
| NC-O18 | 删掉无 `data_root` 分支的边界前复核 | `test_library_dispatch_without_data_root_still_checks_objects` | **RED（PR #129 实测）**：库调用方的第二道门消失 |
| NC-O19 | 对照：守卫命中判定直接短路成「永不命中」 | `test_blocked_object_is_refused_before_any_approval_is_requested` | **RED（PR #129 实测）**：最直接的 fail-open 变异 |
| NC-O20 | 对象阻断的提交不拿执行守卫 | `test_unknown_commit_takes_the_execution_guard` | **RED（复审第一轮实测，B1）**：阻断能在「复核通过 → 推进」之间插进去，那次调用照样发出去 |
| NC-O21 | 派发表缺 `target` 列不做回退 | `test_recovery_survives_dispatch_table_without_target_column` | **RED（复审第一轮实测，M5）**：`SELECT target` 抛错，恢复在写任何阻断之前崩掉 |
| NC-O22 | 发包前 server 不可用也当成「可能已执行」 | `test_server_dead_at_dispatch_is_unknown_without_object_block` | **RED（复审第一轮实测，M6）**：一个字节都没发，却登记对象阻断（过度阻断） |
| NC-O23 | 同一对象的后续未结算调用直接 return | `test_each_unsettled_call_gets_its_own_block_event` | **RED（复审第一轮实测，m4）**：`call_ids` 只剩第一条归因 |

> 负控脚本自身的坑（PR B 实测踩到）：同尺寸同 mtime 秒的改写会让 Python 复用旧
> `__pycache__` → **假绿**。脚本清 `__pycache__` + `PYTHONDONTWRITEBYTECODE=1`，
> 并逐条记录返回码与耗时。
>
> 收尾轮又踩到第二种坑：还原用 `git checkout -- <file>` 回到的是 **HEAD**，会把同一轮里
> **尚未提交**的改动一起抹掉（本轮把 `recovery.py` / `dispatcher.py` 的新代码抹过一次，靠
> 重放补丁修回来）。负控的还原必须是"变异前自己拍的文件快照"，不能借用 git 的 HEAD。
>
> 负控已脚本化、可复现（同一套"快照 → 变异 → 清 pyc + `PYTHONDONTWRITEBYTECODE=1` →
> 跑目标用例 → 快照还原"；全部按预期变红才返回 0，有一条没红就返回 1）：
>
> - `uv run python scripts/negative_controls_mcp_crash.py`：NB21 / NB28 / NB25–NB27 /
>   NB29 / NB30–NB39 共 **16 条**；
> - `uv run python scripts/negative_controls_object_block.py`：NC-O1–NC-O23 共 **23 条**
>   （对象级阻断，PR #129）。
>
> "变红"要同时满足 rc=1、FAILED 行命中声明的目标节点、且输出里有 AssertionError
> （只算 rc 会把 import/collection 炸当成红）。表中耗时与 failed 数是**各轮实测记录**
> （同一台机器；每次运行有几十毫秒到一百多毫秒的波动，失败条数稳定）。

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
10. **崩溃后的 MCP 结论分三档，仍有一档是保守的**（PR #124 起，真子进程夹具实测，见
    §1-c10）。外发边界标记（§2-8）让"没发出去"变成可证明：标记之前崩 → `none_applied` +
    **不阻断**；撤销/作废先到 → 同样 `none_applied`、行保持终态；标记之后（请求可能已写出）
    或无从证明（老库没有派发表、没有行、状态不认识）→ `unknown` + 阻断。仍然不成立的：
    - **标记提交与真正写出之间**有一小段窗口：崩在那里会被按"可能已发出"处理，也就是
      仍然可能过度阻断。这是**有意**选的方向（宁可多挡，不把可能已发生的副作用说成没
      发生）；彻底消掉需要 server 侧的确认协议（例如请求 id 回执），不在本轮范围。
    - 阻断只发生在**标记之后**这一类上；"没越过边界"的调用不再挡任何目录（PR #124 前
      这两种情况是一起挡的）。
    - **升级边界**：只有"由本版本写的派发行"才能证明没发（`boundary_protocol=1`）。
      升级前留下的行（包括 `intent`/`revoked`）一律 `unknown` + 阻断——旧版本在写请求前
      不推进派发行，拿旧行当"没发"就是假阴性。
    - **阻断对象现在有两种**（PR #129，见 §1-c16/§2-9）：目录（workspace，文件写用）与
      MCP 对象（`mcp://server/tool` / server 级 `mcp://server`）。MCP 结算 `unknown` 时
      **同时**登记两者——目录是保守层（server 以当前用户权限跑在本机，传入的工作区**可能**
      被它改过），对象是精确层。**"哪个目录"仍由调用方负责**：`tianshu recover` 的
      `--workspace` 是必填参数，代码**没有**拿 `task_id` 去核对它属于该 Task 的哪些绑定
      （零目录 Task 也合法，CLI 却强制给一个路径）。传错就挡错目录 —— 这是已知代价，不是
      代码保证过的事。对象键不靠调用方：从派发行的 `target` 读（派发当时写下的原始
      `mcp://server/tool`），**不按 `mcp__…__…` 反解**（规范化把 `-`/`.` 换成了 `_`，反解
      出来的名字可能不是用户配的那个）；读不到派发行就**不登记**对象阻断（不编）。
    - **线性化边界（复审第一轮整改后）**：对象阻断的三种提交路径——恢复侧、运行期
      `unknown`（`_commit_mcp_unknown`）、手工 `unblock_object`——现在都先拿
      `execution_guard`，与发外边界的"复核 → 推进"共用同一把守卫：要么阻断先提交
      （发送方复核看得见、拒发），要么发送方先完成推进（这次调用已越过边界，阻断只
      约束之后的新调用）。**仍剩两条窗口**：① **没有 `data_root` 的库调用方**没有守卫
      可抢（`_mark_mcp_send_boundary` 与 `_commit_mcp_unknown` 都退化成直查），窗口仍在；
      ② 提交侧抢守卫**超时**（`GuardTimeout`）时按最坏情况照常提交并在结果里写
      `block_guard="timeout"`——结果不能丢，但这一条确实没排在守卫里。文档此前写
      "撤销与阻断都排不进这道缝"是过度声称，已改成这个口径。
    - **对象阻断的解除是人工动作**：`unblock_object()` 单独解除，`unblock()`（目录）不碰
      它——核对过工作区文件不等于核对过 server 侧副作用（后者本地没法核对，只能人看
      server 的输出/日志）。没有"核对通过"的自动判据，也没有过期时间。**解除入口目前
      是库函数**（与 `unblock()` 一致，CLI 没有 `unblock` / `unblock-object` 子命令——
      `recover` 的输出会提示函数名，但那是 Python API，不是命令行）。解除事件记录的是
      **人工动作**，不代表当时确有有效阻断（对一个从未阻断的键也会写一条，与目录
      `unblock()` 同口径）。server 侧自己的状态（它动过哪些文件、发过什么请求、执行了几
      次）**没有任何证据**，恢复也不去问它（问了也不是本机证据）。
    - **对象粒度只到工具/服务器**：没有 per-call 回执。要证明"这一次到底跑没跑"需要
      server 侧确认协议（例如请求 id 回执），未做。运行期 `unknown`（超时/断线/内部错误）
      用的同一条对象阻断，但那条事件只落在**本数据根日志**（跨数据根要等下一次 `recover`
      的启动补登记，不是即时的）。**server 级阻断目前只有读侧能力**：产品代码只登记
      工具级（`mcp://server/tool`），server 级键由运维通过库 API 手工登记（没有 CLI），
      读侧（守卫、回放、权威根）都生效。
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
3. ~~MCP 崩溃夹具（intent 无 result）+ 恢复判据（补 NB21）~~ **已完成（收尾轮 #122）**：
   `tests/test_mcp_crash.py` 9 条，NB21/NB25/NB26/NB27/NB28/NB29 负控实测红。PR #124 把它
   扩到 **24 条**（三个崩溃窗口 + 标记窗口 + 发包前撤销 + 清单损坏 + 无派发行回退 +
   旧版本行升级兼容 + 自相矛盾行 + own-log 读序 + 边界推进前置条件 + 抢守卫超时 +
   静态表四格 + 终态派发行），负控 **16 条**。
4. 大结果内容引用与文本分页读取；MCP 预算维度（调用次数/结果字节）。
5. UI 的 MCP 面板（server 状态、准入/被拒工具、截断说明）。
6. Windows 实机验证与 CI 基线修复（既有工单）。
7. ~~**发送前持久标记**：让恢复能区分"未发出 → `none_applied` + 不阻断"与"已发出 →
   `unknown` + 阻断"~~ **已完成（PR #124）**：标记复用既有 `dispatches.state`
   （`intent` → `executing`），不新增事件、不改事件契约；恢复/接管按它分档，撤销与发送
   共用执行守卫。剩余窄窗口（标记提交 → 真正写出）仍按"可能已发出"保守处理，见 §4-10；
   要再往前推需要 server 侧回执协议（新的工单，**未做**）。
8. ~~**对象级阻断**~~ **已完成（PR #129）**：阻断对象模型 = 目录 + MCP 对象
   （`mcp://server/tool` 工具级 / `mcp://server` server 级）。MCP 结算 `unknown` 时两者
   同时登记（不放松目录层）；发外前两道核验（判定阶段、外发边界前），解除分开
   （`unblock` 只解目录，`unblock_object` 才解对象）。契约增量：事件
   `recovery.object_blocked` / `recovery.object_unblocked`（43 → 45）+ 规则编号
   `dispatch.mcp_object_blocked`（74 → 75）。**新剩下的**：对象阻断是人工解除、无自动
   判据/过期；粒度只到工具/服务器（没有 per-call 回执）；运行期 unknown 的对象阻断
   跨数据根靠下一次 `recover` 补登记，不是即时。