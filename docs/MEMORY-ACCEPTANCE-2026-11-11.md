# F10 记忆（契约 v1.2）：交付验收报告

> **状态时点**：2026-11-11 21:10 · `main @ c4b020a` · macOS arm64 · Python 3.12
> **本机实测**：`1730 passed / 7 skipped`（`uv run --python 3.12 --frozen pytest -o addopts="" -q`），
> `ruff check src tests` 干净。
> **怎么复现**：见 §7。本文每条都能指到**真实存在的测试名**或**代码函数**；写"未验证"的就是
> 真的没有证据，别把它读成"已验收"。

## 0. 一句话

F10 的 v1.2 契约增量（`layer` / `erased_at` / 6 个新工具 + 1 个升级 / 6 条规则编号 /
物理擦除 / 恢复分支 / 检索质量 / L2-L3 蒸馏 / 控制面端点）**逐条落地并有测试**；
**仍然不成立或未验证的项目在 §6 全部列出**（10 条里 2 条已在 G2 记忆阶段 M1 / PR #126 交付并标 ✅）——其中 3 项是能力缺口（真 embedding provider、
L2/L3 检索质量评测、LLM 蒸馏），4 项是平台/环境未验证（Windows 实机与命名管道 ACL、
Windows CI 的 6 条既有失败、控制面不是模型隔离边界、跨进程断电与真实模型驱动 UI），
1 项是产品面未做（冲突消解）。

## 1. 交付清单（按合并顺序）

| PR | 内容 | 关键文件 |
|---|---|---|
| #74 | 剩余记忆施工规格（文档，不改契约） | `docs/SPEC-RESIDUALS-MEMORY.md` |
| #75 | 记忆契约 v1.2（文档） | `docs/PRD-v1.md` §4.1 / §5.2.2 / §5.4.2，`docs/F10-memory-design.md` |
| #78 | N1 产生侧：控制面标记、`propose` 2.0 作用域、`memory.remember`、`origin` 升级 | `tasks.py` / `dispatcher.py` / `memory_exec.py` / `cli.py` |
| #83 | N1 后半：`memory.edit`（一次审批两行）、`memory.promote`（移动语义） | 同上 |
| #85 | N4 物理擦除：`memory.erase` + 引用计数 + `erased_at`（投影 schema v2） | `memory.py` / `memory_exec.py` |
| #89 | N5 崩溃恢复的记忆分支 + 派发审计表终态脱敏 | `recovery.py` / `dispatcher.py` |
| #93 | N2 检索质量：作用域优先级 + 向量索引/RRF（**受能力边界约束**） | `memory_index.py` / `memory.py` / `loop.py` |
| #98 | N3 确定性 L2/L3 蒸馏 + freshness（`memory.consolidate` / schema **v3**） | `memory_distill.py` / `memory.py` / `memory_exec.py` |
| #100 | N6 控制面 Host/IPC 端点（8 个 RPC）+ 共享控制面模块 + 作业原子性修复 | `memory_control.py` / `control.py` / `cli.py` / `jobs.py` |
| #126 | **G2 记忆阶段 M1**：`memory.search`（第 9 个 RPC）+ React 记忆面板（补上本报告第 7/8 条缺口） | `memory_control.py` / `control.py` / `ui/src/components/MemoryPanel.tsx` / `ui/src/api/memory.ts` |

合并后 `main` 与记忆相关的**数字**：`SPECS` 18 个（记忆 10 个），`tool_manifest()` 14 个
（记忆可见 7 个）、C3 事件类型 41 个（**未新增**）、C4 规则 56 个（拒绝 46 + 判定 8 +
保留未使用 2；记忆组 6 条，**N3/N6 没有新增编号**）、投影 `_SCHEMA_VERSION = 3`。

## 2. 契约面实际状态（先看这张，再看 §3 判据）

| 契约 | 状态 | 事实 |
|---|---|---|
| C2 工具 | ✅ | `memory.*` 共 10 个：7 个模型可见（`search` / `get` / `propose` **2.0** / `consolidate` / `confirm` / `forget` / `edit`）+ 3 个控制面专属 `exposed=False`（`remember` / `promote` / `erase`）。均无 `path_arg`。 |
| C3 事件 | ✅ **未新增事件族** | 记忆生命周期全由 `tool.intent` / `tool.result` + 审批事件承载；`event.json` 的 tool_id 封闭集与 `event_schema._TOOL_ID` 同序（有判据测试）。 |
| C4 权限 | ✅ | 记忆组 6 条规则；白名单/审批/作用域判定在派发层 + 执行侧**各一次**；`c4.memory_control_scope_binding` 是 v1.2 唯一新增编号。 |
| 投影 schema | ✅ v3 | `layer TEXT NOT NULL DEFAULT 'l1'`（v2→v3 就地 `ALTER`，旧行 l1）与 `erased_at`（v1→v2）都可重复执行；更高版本 fail-closed。 |
| 控制面 RPC | ✅ Host/IPC 一侧 | 8 个方法：`list` / `get` / `remember` / `confirm` / `edit` / `forget` / `promote` / `erase`；**不是授权边界**（见 §6-6）。 |

## 3. 逐条判据 ↔ 证据

> 判据来源：`docs/F10-memory-design.md` §14（出口验收）与 `docs/SPEC-RESIDUALS-MEMORY.md`
> 各工作流的"判据"节。下表**只列能被一条测试打红**的判据；"部分覆盖 / 不成立"的写在这里
> 或 §6，不混进 ✅。

### 3.1 资产与作用域（N1 / F10 §5–§6）

| 判据 | 证据（测试名） |
|---|---|
| 记住一条偏好后新任务能检索到 | `test_user_memory_is_recalled_by_a_later_task`、`test_zero_directory_task_can_recall_memory`、`test_propose_confirm_get_search_round_trip` |
| 不同 workspace 不串记忆、模型读不到别人的 | `test_cross_scope_isolation_and_loadout`、`test_observe_is_scope_filtered`、`test_workspace_scope_is_denied_cross_task_and_rule_is_registered`、`test_task_scope_is_only_for_its_own_task` |
| 候选未经确认不激活；模型不能自称 `user_explicit` | `test_agent_propose_then_confirm_through_dispatcher`（断言 origin=inferred）、`test_human_approval_upgrades_origin_to_user_confirmed` |
| `origin` 三档迁移规则（只升不降、promote 随行） | `test_human_approval_upgrades_origin_to_user_confirmed`、`test_promote_is_control_only_and_moves_memory`、`test_edit_with_approval_keeps_old_row_and_activates_new` |
| 编辑一次审批两行、旧版可查、不许改 kind | `test_edit_with_approval_keeps_old_row_and_activates_new`、`test_edit_requires_the_revision_the_human_saw`、`test_cli_edit_keeps_old_row_and_activates_new` |
| 提升是移动：原行退役、新行候选、Receipt 指原行、只控制面能做 | `test_promote_is_control_only_and_moves_memory`（含 receipt 断言）、`test_promote_to_workspace_canonicalizes_the_target_scope`、`test_cli_promote_moves_memory_into_task_scope` |
| 模型只能写本任务 task；workspace **无条件**拒（含控制任务复用） | `test_model_can_only_propose_into_its_own_task_scope`、`test_model_cannot_propose_into_workspace_scope`、`test_workspace_propose_is_refused_even_on_a_control_task` |
| 控制面标记与声明作用域落事件、rebuild 后仍在 | `test_control_marker_and_scope_declaration_survive_rebuild` |
| 工具↔资源种类不能互换；`memory://` 编解码可指回 | `test_tool_and_resource_kind_must_match`、`test_workspace_uri_percent_encodes_and_round_trips`、`test_workspace_scope_is_canonicalized` |
| 五个→七个模型可见工具真在 `tool_manifest()`；控制面专属不在 | `test_memory_tools_are_real_specs_without_path_args`、`test_memory_tool_sets_match_the_registry` |

### 3.2 擦除与遗忘（N4 / F10 §10）

| 判据 | 证据 |
|---|---|
| 只删唯一引用正文；共享 blob 保住别人的记忆；已擦除行不计引用 | `test_erase_deletes_unique_blob_and_marks_erased`、`test_erase_keeps_shared_blob_and_reports_retained_by` |
| 擦除的计数/删文件/提交在同一把跨进程执行守卫内 | `test_erase_commit_runs_inside_the_execution_guard` |
| 擦除只控制面能做；重复擦除确定拒绝；旧库补列；高版本 fail-closed | `test_erase_is_control_plane_only`、`test_erase_deletes_unique_blob_and_marks_erased`、`test_schema_v1_database_is_migrated_in_place`、`test_newer_schema_version_fails_closed` |
| 忘记后新任务/重建/重启都不再召回；不可按 id 复活 | `test_forget_is_not_recalled_and_rebuild_does_not_resurrect`、`test_forget_and_content_survive_process_restart`、`test_forgotten_memory_cannot_be_revived_by_id` |
| 回放用当时快照，不回落最新版本 | `test_replay_uses_the_snapshot_not_the_latest_memory`、`test_missing_blob_is_content_unavailable_and_never_falls_back` |

### 3.3 崩溃恢复与审计脱敏（N5 / F10 §10、#21 / #26）

| 判据 | 证据 |
|---|---|
| 有 intent 无 result ⇒ 投影层**一定**没生效；不阻断目录 | `test_recovery_marks_pending_memory_write_as_not_applied` |
| 正文 blob 层：孤儿副本按引用计数清、共享的不误删 | `test_content_write_crash_leaves_no_orphan_blob_after_recovery`、`test_recovery_cleanup_never_deletes_a_blob_another_memory_shares` |
| `memory.erase` 例外：文件已没 ⇒ unknown（不宣布"没生效"） | `test_erase_crash_after_deletion_is_not_reported_as_none_applied`、`test_erased_and_missing_rows_have_distinct_recovery_conclusions` |
| 快照清单坏掉也先结算记忆写 | `test_memory_write_is_settled_even_when_the_snapshot_manifest_is_corrupt` |
| 派发审计表终态脱敏（成功/拒绝/中途炸掉/waiting 后被否决） | `test_accepted_memory_write_args_are_redacted_at_terminal_state`、`test_rejected_memory_write_keeps_no_raw_content_in_the_request_table`、`test_memory_write_that_crashes_mid_projection_is_still_redacted`、`test_request_stored_before_approval_is_redacted_when_it_is_later_rejected` |
| 非记忆请求不被过度脱敏；`tool.intent` 只留正文哈希 | `test_file_write_args_are_not_redacted`、`test_intent_events_drop_content_for_every_content_memory_write` |
| 旧库终态行打开时补扫；迁移中断重试；同号重写保留完整参数 | `test_terminal_rows_from_an_older_library_are_redacted_when_opened`、`test_interrupted_backfill_is_retried_on_the_next_open`、`test_reissued_request_after_terminal_state_keeps_full_args` |

### 3.4 检索质量（N2 / F10 #20、#23、§8）

| 判据 | 证据 |
|---|---|
| 作用域优先级：task > workspace > user，且与 `allowed` 无关、可复现 | `tests/test_memory_rank.py` 7 条 |
| 语义候选与 FTS 取**并集**（FTS 空也跑）；能力边界自报 | `test_semantic_route_runs_even_when_fts_finds_nothing`、`test_semantic_only_candidates_are_merged_with_fts_candidates`、`test_fts_only_reports_semantic_false_with_reason` |
| 空索引/半套向量/换 provider 不报可用；重建逐字节确定 | `test_empty_index_is_not_reported_as_available`、`test_mixed_provider_vectors_are_detected_as_incomplete`、`test_provider_switch_makes_old_vectors_ineligible`、`test_rebuild_vectors_is_byte_identical_across_runs` |
| RRF 可手算；`search()` 不偷偷变 hybrid | `test_rrf_scores_are_hand_checkable`、`test_search_stays_fts_only_and_retrieve_is_hybrid` |
| 索引跟随生命周期（候选/确认/忘记/擦除）；语义路不越权 | `test_index_follows_the_memory_lifecycle`、`test_erase_drops_the_vector`、`test_semantic_route_never_pulls_in_other_scopes` |
| **量化**（固定样例 22 query / 25 记忆） | `docs/memory-retrieval-eval.md`：FTS MRR 0.864 → hybrid-hash **0.955**（lexical 0.688→1.000，mixed 1.000→0.875 是真实代价）；Recall@5 两者 1.000 = **饱和**，脚本 `scripts/eval_memory_retrieval.py` 可复跑 |

### 3.5 L2/L3 蒸馏与新鲜度（N3 / F10 #19、§8）

| 判据 | 证据 |
|---|---|
| 两次合成**逐字节相同**；改一条源只动它那一块 | `test_two_consolidations_are_byte_identical`、`test_changing_one_source_only_moves_its_own_block` |
| L2 是引用式拼装（不是自由摘要）；`proof_count == len(evidence)` | `test_l2_content_is_reference_assembly_not_free_form`、`test_proof_count_and_evidence_are_mechanically_consistent`、`test_consolidate_refuses_inconsistent_page_from_a_buggy_builder` |
| 坏页进不了投影（未知 layer / `l1` / 空来源）；重复应用幂等；正文回退 fail-closed | `test_consolidate_rejects_bad_pages`（参数化 3 例）、`test_reapplying_the_same_page_is_idempotent`、`test_reverting_to_an_old_page_body_fails_closed` |
| L3 只收 user 作用域里人给的偏好、4 KiB 整行预算、"预算已满" | `test_l3_page_is_budgeted_and_only_human_preferences`、`test_l3_only_for_user_scope` |
| 旧页被取代后仍可 `get`（含 evidence / `source_refs` 快照）；**active 页不因合成退役** | `test_old_page_is_superseded_and_still_gettable`、`test_confirmed_page_is_not_retired_by_a_later_consolidation` |
| replay 只认事件 output、不重算 | `test_consolidate_output_is_replayable` |
| freshness：三态（fresh/unknown/stale）、**三条检索路径都降权**、过期 L1 不算更新证据 | `test_freshness_marks_pages_and_demotes_them`、`test_retrieve_demotes_stale_pages_even_without_a_vector_index`、`test_unknown_freshness_stays_unknown_and_is_not_reported_as_fresh`、`test_freshness_rank_puts_unknown_between_fresh_and_stale`、`test_expired_l1_does_not_age_a_page` |
| consolidate 是真模型工具；workspace 无条件拒；task 只能自己；重复合成如实报状态；失败不写孤儿正文（单页/多页） | `test_consolidate_is_a_real_model_tool_and_writes_candidate_pages`、`test_consolidate_refuses_workspace_scope_at_dispatch`、`test_consolidate_task_scope_only_for_own_task`、`test_repeated_consolidate_reports_existing_status_not_candidate`、`test_failed_consolidate_writes_no_orphan_blob`、`test_multi_page_consolidate_prechecks_every_page_before_writing` |
| schema 迁移：v1→v3 与 v2→v3（`layer` 补列、旧行 l1、可重复执行） | `test_schema_v1_database_is_migrated_in_place`、`test_schema_v2_database_gains_the_layer_column_in_place` |

### 3.6 控制面端点（N6 / F10 §9、#30）

| 判据 | 证据 |
|---|---|
| 六项判据（`scope`/`origin`/`revision`/`status`/`source_refs`/`proof_count`）+ 纯文本正文 | `test_list_and_get_return_the_six_judgement_fields`、`test_default_scope_is_user_and_other_scopes_must_be_named` |
| 写操作全走 dispatcher（事件里有**该工具的** intent/result、审批一次性消费） | `test_edit_rpc_writes_intent_and_result_events`、`test_forget_moves_state_through_the_dispatcher`、`test_confirm_on_an_already_active_memory_is_rejected`、`test_promote_moves_the_memory_and_old_scope_stops_recalling`、`test_stale_revision_is_refused_before_dispatch` |
| task 作用域写后视图不为 null；workspace → user promote 可用 | `test_task_scope_edit_and_forget_return_the_updated_view`、`test_promote_from_workspace_to_user_uses_source_scope` |
| `memory.erase` 删副本 + `content_state=unavailable` | `test_erase_rpc_removes_the_body_copy_and_reports_it` |
| 8 个方法都注册；`memory.list` 不是模型工具（**只证明名字/路径**） | `test_all_eight_control_plane_methods_are_registered`、`test_memory_list_has_no_model_tool_counterpart` |
| 真 Host `/rpc` + token 代理；无 token 401 | `test_host_rpc_proxies_memory_endpoints_with_the_ui_token` |
| 控制 Run 的作业**入队/认领原子**（CI 抓到的竞态） | `test_enqueue_and_claim_never_exposes_a_pending_job`、`test_control_run_claims_its_job_atomically` |

## 4. 负控总表（"改坏什么 → 哪条红"，本轮都真跑过）

| # | 改坏什么 | 变红的判据 |
|---|---|---|
| 1 | 每块合成正文掺入全页面字节数（破增量 delta） | `test_changing_one_source_only_moves_its_own_block` |
| 2 | `_stale_map` 直接 `return {}` | `test_freshness_marks_pages_and_demotes_them` |
| 3 | 派发层去掉 consolidate 的 workspace 无条件拒 | `test_consolidate_refuses_workspace_scope_at_dispatch` |
| 4 | L3 预算因子 ×100 | `test_l3_page_is_budgeted_and_only_human_preferences` |
| 5 | L3 去掉 user 作用域限定 | `test_l3_only_for_user_scope` |
| 6 | `consolidate()` 就位校验改 `if False` | `test_consolidate_refuses_inconsistent_page_from_a_buggy_builder` |
| 7 | `retrieve()` 无索引分支去掉 stale 排序键 | `test_retrieve_demotes_stale_pages_even_without_a_vector_index`（复审抓到的真洞） |
| 8 | 三态里的 `unknown` 当成新鲜 | `test_freshness_rank_puts_unknown_between_fresh_and_stale` |
| 9 | 去掉既有页预检（先写正文再失败） | `test_failed_consolidate_writes_no_orphan_blob` |
| 10 | 预检前先写第一页正文 | `test_multi_page_consolidate_prechecks_every_page_before_writing` |
| 11 | consolidate 一律把 output 写成 candidate | `test_repeated_consolidate_reports_existing_status_not_candidate` |
| 12 | 旧页退役重新包含 active | `test_confirmed_page_is_not_retired_by_a_later_consolidation` |
| 13 | 已退役页 id 静默 return | `test_reverting_to_an_old_page_body_fails_closed` |
| 14 | v2→v3 迁移不补 `layer` 列 | `test_schema_v2_database_gains_the_layer_column_in_place` |
| 15 | 过期 L1 参与陈旧判定 | `test_expired_l1_does_not_age_a_page` |
| 16 | `memory.edit` 端点返回假成功、不派发 | `test_edit_rpc_writes_intent_and_result_events` |
| 17 | `enqueue_and_claim` 改回"先 enqueue 再 claim" | `test_enqueue_and_claim_never_exposes_a_pending_job` + `test_control_run_claims_its_job_atomically` |

更早几轮（N1/N2/N4/N5）的负控记在 `F10-memory-design.md` §13 的对应行与
`SPEC-RESIDUALS-MEMORY.md` 的落地记录里（例如：去掉 `origin` 只升不降、去掉 `erase` 的
跨进程守卫、去掉终态脱敏、语义路只重排 FTS 候选、`availability()` 永不降级……）。

## 5. 数字对账

| 项 | 值 |
|---|---|
| 全量测试 | **1730 passed / 7 skipped**（`main @ c4b020a`，macOS arm64） |
| 记忆测试（八个文件） | 272 条：`host_memory` 14 + `store` 67 + `dispatch` 67 + `loop` 26 + `distill` 22 + `resources` 43 + `rank` 7 + `vectors` 26 |
| `tool_manifest()` | 14 个（记忆可见 7 个；控制面专属 3 个中 2 个在记忆组外的 `restore_file` 不计） |
| C3 事件类型 | 41 个（**本轮未新增**；`memory.consolidate` 只进 tool_id 封闭集） |
| C4 规则 | 56 个 = 拒绝 46 + 判定 8 + 保留未使用 2（记忆组 6 条；N3/N6 **0 新增**） |
| 投影 schema | 3（v1→v2 加 `erased_at`；v2→v3 加 `layer`） |
| 检索评测 | `docs/memory-retrieval-eval.md`（22 query / 25 记忆；MRR 0.864 → 0.955；Recall@5 饱和） |

## 6. 未验证 / 不宣称（**这一节是这份报告最重要的部分**）

| # | 事项 | 现状 | 为什么 / 后续 |
|---|---|---|---|
| 1 | **真 embedding provider** | ❌ 没有。生产只接受 `TIANSHU_EMBEDDING_PROVIDER=hash`（确定性哈希，**无语义能力**）；外部 provider 需数据外发许可，未接线 | 评测里 hybrid 的收益只证明"两路名次融合"，不是语义能力。接真 provider 必须重跑 `memory-retrieval-eval.md` 并另记数字 |
| 2 | **L2/L3 检索质量评测** | ❌ 没有 | 没人证明"读页比直接读 L1 更好用"；文档与 README 都禁止这样宣称。L2/L3 立论只到"可重建、可回放、能与证据逐条对账" |
| 3 | **LLM 蒸馏** | ❌ 没做 | 生成式合成让"两次合成是否一致"不可机械核对。要做必须另立契约：正文进事件、replay 用快照、与确定性路径分两套代码 |
| 4 | **Windows 实机 + 命名管道 ACL** | ❌ 未验证 | 没有 Windows 机器；`ipc._chmod_private` 在 Windows 直接 return（只影响写位、无 ACL 语义），pipe security descriptor 未设置。Windows CI 只作为非必需检查跑（见 #5） |
| 5 | **Windows CI 全绿** | ❌ 6 条既有非记忆族失败（`test_authority_block` ×2 / `test_host` / `test_ipc` ×2 / `test_resources`）；**记忆相关用例在 Windows 上全过** | 这些族不是本轮引入；Windows job 不在 required checks。别写成"Windows 已验证" |
| 6 | **控制面不是模型隔离边界** | ⚠ 已知边界，**不是已解决** | v1 没有进程级沙箱：`run_command` 以当前 uid 跑子进程，可读 `run/authkey`、连 IPC、跑 CLI 并自己落审批。所以"审批需要人"只对**没有** `run_command` 的任务成立。要收口需要进程/文件系统沙箱或独立 OS 用户（F2/exec 后续工单） |
| 7 | **React 记忆面板** | ✅ 已接（#126） | 端点之外补了 UI：列表/检索/确认/忘记/擦除，六项判据 + `score`/`score_kind`，正文纯文本、不做乐观更新、冲突只刷新重读；UI 测试 10 文件 75 条。**端点仍不是授权边界**（见第 6 条） |
| 8 | **`memory.search` 的 RPC** | ✅ 已暴露（#126） | 第 9 个记忆方法；复用 `retrieve()`，所以返回带 `semantic`/`degraded`/`fused`；显式 `scope` 是**追加**不是替换（人永远还看得见 `user` 记忆）；无 `status` 参数（刻意收窄） |
| 9 | **跨进程断电测试 / 真实模型驱动 UI** | ❌ 未做 | 断电用 `SystemExit` 打在 `_commit_result` 覆盖"副作用已发生、提交未发生"的逻辑窗口，不是真跨进程死亡；UI 演示用 `scripted` 模型，不是真实模型驱动 |
| 10 | **冲突消解 / 跨作用域合成 / 对话抽取** | ❌ 不做 | 两条矛盾的 L1 在页里并列、不裁决；明确记在 SPEC §9 |

## 7. 复现

```bash
# 1) 全量测试（注意 pyproject 里 addopts=-q，再叠一个 -q 会吞掉汇总行）
uv run --python 3.12 --frozen pytest -o addopts="" -q          # 1730 passed, 7 skipped
uv run --python 3.12 --frozen ruff check src tests             # All checks passed!

# 2) 只跑记忆的八个文件
uv run --python 3.12 --frozen pytest -o addopts="" -q \
  tests/test_host_memory.py tests/test_memory_store.py tests/test_memory_dispatch.py \
  tests/test_memory_loop.py tests/test_memory_distill.py tests/test_resources.py \
  tests/test_memory_rank.py tests/test_memory_vectors.py

# 3) 检索评测（不需要真模型）
uv run --python 3.12 python scripts/eval_memory_retrieval.py

# 4) 控制面端点手验（真 daemon + 真 IPC）
uv run --python 3.12 -m tianshu --data-root /tmp/ts-demo daemon &
uv run --python 3.12 -m tianshu --data-root /tmp/ts-demo memory remember "报告用中文写"
uv run --python 3.12 -m tianshu --data-root /tmp/ts-demo memory list
uv run --python 3.12 -m tianshu --data-root /tmp/ts-demo memory confirm <memory_id> --revision 1
```

## 8. 后续工单建议（不在本轮范围）

1. **F2/exec 进程沙箱**（控制面/审批的真实隔离边界；与 `run_command` 一起收口）。
2. **Windows pipe security descriptor + 实机 ACL 测试**（或明确 N6 Windows unsupported）。
3. ~~**React 记忆面板**~~ **已交付**（#126：消费 9 个 RPC，正文纯文本）。
4. **真 embedding provider 接线 + 重跑评测**（含数据外发许可、密钥管理）。
5. ~~**`memory.search` RPC**~~ **已交付**（#126：复用 `retrieve()` 的能力标注）。
6. **LLM 蒸馏契约**（生成式合成的事件形状、replay 口径、评测）。