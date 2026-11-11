# F2（沙箱与 policy）交付验收报告（2026-11-11）

关联：[PRD-v1.md](PRD-v1.md) §7 F2 / §8.1 / §8.1.1；[TECH-ARCH.md](TECH-ARCH.md) §6；
[isolation-matrix.md](isolation-matrix.md)；[F2-sandbox-policy.md](F2-sandbox-policy.md)；
[SPEC-F2-2026-11-11.md](SPEC-F2-2026-11-11.md)。

**先说结论**：F2 的 P0 交付的是「**后端可插拔 + 实测门 + 受信任执行可见**」，
**不是物理沙箱**。macOS 上任意 shell 执行仍然是受信任执行；本机四边界实测只有
`process_tree` 一条 `enforced`，任何非空 `isolation_requires` 都会返回 `unsupported`。
L3/L4（Seatbelt / AppContainer / 容器）在 v1 未实现，Windows 进程树（Job Object）
也未实现——这些都在 §5 逐条写明，不写成"已完成"。

## 0 交付清单

| # | 内容 | PR / 位置 | 状态 |
|---|---|---|---|
| 1 | F2 收口规格（判据、增量、负控、未验证） | [#107](https://github.com/lorek666/tianshu-agent/pull/107) `docs/SPEC-F2-2026-11-11.md` | 已合入（v5） |
| 2 | 执行后端注册表（未实现/未知 → `unsupported`，绝不回落 local） | [#108](https://github.com/lorek666/tianshu-agent/pull/108) | 已合并 `b96107a` |
| 3 | 隔离要求 + 实测门 + 审批前「受信任执行」 | [#111](https://github.com/lorek666/tianshu-agent/pull/111) | 已合并 `0c490aa` |
| 4 | 文档对账（TECH-ARCH §6 / PRD / matrix / F1 / KNOWLEDGE / event contract） | 本 PR | 本 PR |

数字口径：本机 macOS arm64、`PYTHONPATH=$PWD/src`、`pytest -o addopts="" -q`：
**1905 passed / 7 skipped**（main + #108 + #111 + G2 网络阶段）；UI `vitest` **51 passed**、
`tsc --noEmit` 干净。三平台 CI：ubuntu / macOS / `ui` 绿；Windows 仍是**已知基线红**
（见 §5 第 10 条），本轮不声称 Windows 绿。

## 1 判据 ↔ 测试（逐条可对账）

| 判据 | 落点 | 钉住它的测试 |
|---|---|---|
| F2-c1 后端可插拔 | `src/tianshu/exec_backends.py`；`dispatcher._precheck_exec` / `tools._run_command` 经注册表 | `test_exec_backends.py::test_default_backend_is_the_local_backend`、`test_env_selects_a_registered_backend`、`test_run_command_uses_registered_backend`、`test_precheck_uses_configured_backend_capabilities` |
| F2-c2 L3/L4 标 `unsupported` | 四个已知未实现名字 + 拼错名字 → `PolicyUnsupported`；`TECH-ARCH.md` §6 表已改 | `test_unimplemented_backend_is_unsupported_not_local`（4 参数）、`test_unimplemented_backend_selected_by_env_is_also_refused`、`test_unknown_backend_is_refused_with_available_names` |
| F2-c3 声明式/默认全拒/deny 优先 | `execpolicy.py`（语义未改） | 既有 `test_execpolicy.py` 全套 |
| F2-c4 做不到必须 `unsupported`、禁静默放宽 | 后端选择 + 能力判定 + 隔离门三层；`isolation.require_isolation_requirements` | `test_bad_backend_config_fails_at_startup_not_first_command`、`test_unproven_boundary_is_refused`、`test_requirement_blocks_before_spawn`、`test_backend_failure_is_not_retried_against_local` |
| F2-c5 受信任执行必须**审批前**可见 | `dispatch.requested` / `approval.requested` / `ApprovalView.isolation` / `ToolResult.output.isolation`；UI 审批卡片与时间线 | `test_approval_before_sees_isolation_view`、`test_approval_view_shows_measured_report_when_requirement_passes`、`test_control.py::test_approvals_list_has_exact_shape_and_rule_id`、`approval-card.test.tsx`、`humanize.test.ts`「隔离视图」用例 |
| F2-c5a 执行结果可对账 | `run_command` 输出恒带 `isolation`（含 rejected 分支） | `test_run_command_output_carries_isolation_view`、`test_requirement_blocks_before_spawn` |
| F2-c6 四边界实测证据、报告绑定现场 | `IsolationReport` 绑定 backend/platform/workspace/data_root/probe_version/timeout；缓存绑定后端对象 identity | `test_probe_cache_reuses_same_site`、`test_probe_cache_separates_data_roots`、`test_probe_cache_is_invalidated_when_backend_object_is_replaced`、`test_report_from_another_backend_is_refused`、`test_report_from_another_platform_is_refused`、`test_report_from_another_site_is_refused`、`test_old_probe_version_is_refused` |
| F2-c6a 边界独立、等级累计 | `measured_level` 只用于显示；门逐边界判定 | `test_partial_boundary_view_is_not_just_trusted`、`test_unverified_view_keeps_unknown_boundaries`、`test_contradicted_flag_is_report_wins`、`test_precheck_and_execution_probe_the_same_workspace` |
| F2-c7 进程树（TECH-ARCH L2） | POSIX 进程组杀树（既有 `exec.py`）；Windows **未交付**，矩阵写 ⛔ | 既有 `test_exec.py` Windows/POSIX 用例 + `isolation-matrix.md` §2 |
| PRD §8.1.1 契约 | `_EXEC_POLICY.isolation_requires` 封闭枚举；`_ISOLATION_VIEW` 8 字段（measured 可 null、四边界 required、级别/已证明边界 enum）；`schemas/c3/event.json` 镜像 | `test_task_created_accepts_both_policy_shapes`、`test_illegal_boundary_is_rejected_before_write`、`test_isolation_view_contract_is_strict`、`test_event_schema.py::test_schema_json_mirrors_python_contract` |
| 预检路径错误可判拒绝 | `_precheck_exec` cwd 解析失败 → `DispatchDenied(c4.outside_workspace)` | `test_cwd_outside_workspace_is_a_rejection_not_a_run_failure` |
| 能力来源封闭 | `ExecBackendCapabilities.isolation ∈ ISOLATION_LEVELS`（与输出 enum 同源） | `test_bogus_backend_isolation_level_is_rejected_at_construction` |
| 装配同一世界 | 自定义 executor 的 exec_backend 与 data_root 都被核 | `test_dispatcher_refuses_executor_with_another_exec_backend`、`test_dispatcher_refuses_executor_with_another_data_root`、`test_executor_refuses_after_default_exec_backend_changes` |
| 无数据根合法 | `FileExecutor.data_root=None` 构造 + 无要求执行成功 | `test_file_executor_without_data_root_constructs_and_runs_command` |

## 2 负控（实跑；每条都能让实现回退后变红）

**PR #108（后端注册表）**

| 负控 | 改法 | 实测 |
|---|---|---|
| A1 | 未实现分支 `return LOCAL_EXEC_BACKEND` | **6 failed**（当时 23 例测试集） |
| A2 | 去掉未实现名字的 `replace=True` 门槛 | **1 failed**, 22 passed |
| A3 | `_run_command` 删掉调用方 `check_spawn` | **2 failed**, 21 passed |
| A4 | 恢复无条件 `check_capabilities`（真实回归） | **1 failed**, 22 passed |
| A5 | pty 判定挪到 argv 之前（规则 id 漂移） | **1 failed**, 22 passed |
| A6 | 删掉 FileExecutor 执行期后端核验 | **1 failed**, 22 passed |
| A7 | 删掉 Dispatcher 自定义执行器核验 | **1 failed**, 22 passed |
| A8 | 失败后先跑一次 local 再抛原异常 | **1 failed**, 22 passed |
| A9 | 不查 `blob_dir`/`pty` 形参 | **1 failed**, 24 passed |
| A10 | 未知必填参数不扫 positional-only | **1 failed**, 24 passed |
| A11 | 未实现名字不要求隔离级别相称 | **1 failed**, 24 passed |

**PR #111（隔离门与可见性）**

| 负控 | 改法 | 实测 |
|---|---|---|
| B1 | 门不按实测拒绝 | **4 failed**, 20 passed |
| B2 | 要求未满足时直接放行 | **7 failed**, 17 passed |
| B3 | `run_command` 输出删 `isolation` | **1 failed**, 23 passed |
| B4 | `dispatch.requested` 删 `isolation` | **1 failed**, 23 passed |
| B5 | `approval.requested` 删 `isolation` | **2 failed**, 22 passed |
| B6 | 缓存键去掉 `data_root` | **1 failed**, 23 passed |
| B7 | UI 把 `none` 写成"已隔离" | **vitest 1 failed** |
| B8 | JSON 镜像删新增字段 | **1 failed** |
| B9 | 契约不要求 `measured` | **1 failed**, 30 passed |
| B10 | 门不核 `report.backend`/`platform` | **2 failed**, 29 passed |
| B11 | 契约级 level 不进封闭集 | **1 failed**, 33 passed |
| B12 | cwd 解析失败不转 `DispatchDenied` | **1 failed**, 33 passed |
| B13 | 不核自定义 executor 的 `data_root` | **1 failed**, 33 passed |

每次负控后都 `git checkout -- .` 复原；随后 targeted/full 套件复跑通过。

## 3 文档对账

- `TECH-ARCH.md` §6：L3a/L4 由「✅ 交付」改为 **v1 未实现**；L1 明确「只约束内置
  文件工具，不约束子进程」；L2 改为「POSIX ✅ / Windows ⛔」；删掉"macOS 比
  Windows 低一档"这种在 v1 没有可交付物支撑的表述。
- `isolation-matrix.md` §7：声明/实测/已证明边界三口径分开；四种视图组合与 UI
  优先级；探测现场（workspace/data_root/backend/platform/version/timeout）与
  缓存成本；"不能由矩阵推出"的清单。
- `PRD-v1.md`：F2 行补落地位置；§8.1 两条 UI 声明标注落地点。
- `F1-exec-primitive.md`：补 Windows L2 未交付的如实降级指向。
- `event-payload-contract.md`：新增 §6.1——可选字段、旧事件合法、**旧 v1 二进制
  读新库不支持**（v1 未发布；发布后加字段必须 bump 版本）。
- `M5-desktop.md`：`ApprovalView` 字段加 `isolation` 并说明审批前语义。
- `KNOWLEDGE.md`：F2 条目扩成"策略 + 注册表 + 实测门 + 审批前可见"；
  "控制面不是模型隔离边界"保留结论并写明 F2 收口后仍然成立。

## 4 数字对账

| 项 | 值 |
|---|---|
| 本机全量（macOS arm64，main + #108 + #111 + G2 网络阶段） | **1905 passed / 7 skipped** |
| 本轮新增 Python 测试 | 25（`test_exec_backends.py`）+ 36（`test_isolation_gate.py`）= 61 |
| 本轮更新契约测试 | `test_control.py` 的 `ApprovalView` 精确集合 +1 字段 |
| UI 测试 | `vitest` 51 passed（隔离优先级 1 例 + ApprovalCard 渲染 3 例 + normalize/humanize 畸形/超时 2 例） |
| 事件类型 | 41（**未新增族**） |
| 规则 id | 56（**未新增**；`PolicyUnsupported`/`IsolationRefused` 走 `feature` 字段） |
| 依赖 | 0（`pyproject.toml` 未改） |

## 5 未验证 / 不宣称（按事实登记）

1. **macOS 上没有物理沙箱**：`local-posix` 的 `files_authority` / `network` /
   `credentials` 三边界实测 `not_enforced`，只有 `process_tree` 为 `enforced`；
   `measured_level = none`。任意非空 `isolation_requires` 在本机都会得到
   `unsupported`——这是诚实结果，不是配置错误。
2. **Windows 进程树（L2）未实现**：无 Job Object，`supports_job_objects=False`，
   超时只能杀直接子进程、`descendants_may_survive=True`；进程树边界 `unknown`。
   选择不做的理由见 `F2-sandbox-policy.md` §7（技术上可行，但依赖私有接口 +
   无 Windows 实机，无法证明 assign 窗口零逃逸）。
3. **L3 / L4 后端不存在**：`app_container` / `container` / `seatbelt` / `landlock`
   只是"已知未实现"的注册项，请求即 `unsupported`。
4. **Windows 实机**：没有；只有 CI runner 的经验结果（且本轮 Windows 基线红，
   见第 10 条）。macOS x64 / Linux x64 / arm64 同样无实机，矩阵保持 `unknown`。
5. **探针覆盖有限**：不覆盖内核攻击面（ptrace/注入）、DNS/出站、磁盘配额、
   CPU 上限、TCC 交互；四边界探针不是"安全评分"。
6. **审批视图是装配时视图**：`ApprovalView.isolation` 来自预检时的声明/报告
   （可能是 `unverified`）；本次执行的证据在 `tool.result.output.isolation`。
7. **报告缓存只在本进程内**：跨进程不共享；换后端对象/数据根/版本会重测，
   但"进程重启后重新探测"的成本没有优化。
8. **`isolation_requires` 的语义是边界集合**：请求 `process_tree` 只核杀树，
   不隐含文件边界；`level` 是累计显示。可能出现"门放行、level=none"——UI 文案
   已固定为「整体未隔离；仅 X 边界有实测证据」，但"部分边界有证据"仍可能被
   用户读强，需要后续用户测试。
9. **UI 只有审批卡片 + 时间线文案**：没有独立"隔离"面板；`ApprovalCard` 渲染
   用例只验证静态 HTML，不覆盖交互（点允许/拒绝）。
10. **Windows CI 基线红**：纯文档 PR #107（只改 docs）在 `windows-latest` 上
    8 failed；因此"Windows 绿"在本轮不是可交付项，F2 也没有 Windows 实机证据。
11. **旧 v1 二进制读新库不支持**：新增可选字段 + 既有 `additionalProperties:false`；
    v1 未发布，不做跨二进制前向兼容（`event-payload-contract.md` §6.1）。
12. **被拒绝的请求不带隔离视图**：`dispatch.rejected` / `policy.denied` 没有
    `isolation`（没有审批、没有执行）；PRD 不要求，未扩大契约。

## 6 复现

```bash
cd /Volumes/Date/ts-wt-f2
PYTHONPATH=$PWD/src /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q
PYTHONPATH=$PWD/src /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q \
  tests/test_exec_backends.py tests/test_isolation_gate.py tests/test_control.py \
  tests/test_event_schema.py tests/test_run_command.py tests/test_dispatch.py
cd ui && pnpm install --frozen-lockfile && pnpm run typecheck && pnpm test && pnpm run build
# 隔离矩阵实测（真实子进程，约 3 秒）
PYTHONPATH=$PWD/src /Volumes/Date/天枢Agent/.venv/bin/python -c \
  "from tianshu import isolation; from tianshu.exec import LOCAL_EXEC_BACKEND as B; \
   print(isolation.probe_backend(B, workspace='/tmp/f2-ws', data_root='/tmp/f2-data'))"
```

## 7 后续工单（按优先级）

1. **Windows 进程树**：完整 `CreateProcess`（或私有 handle 路线）+ Job 事件观察 +
   Windows 实机验收；在此之前矩阵保持 ⛔。
2. **真隔离后端**：容器（无凭据 + 副本工作区）或平台沙箱；先用四边界探针产出
   `enforced` 证据，再登记矩阵，再看依赖隔离的能力是否能开。
3. **UI 独立隔离面板**：把 `ApprovalView.isolation` 与 `tool.result.output.isolation`
   做成可展开的"这次执行到底受什么限制"视图；补交互测试。
4. **Windows CI 基线**：把 8 个既有失败查清并修复（与本轮无关，但挡发布门禁）。
5. **用户测试**：`受信任执行` / `整体未隔离；仅 X 边界有实测证据` 两种文案是否
   被正确理解（第 8 条）。