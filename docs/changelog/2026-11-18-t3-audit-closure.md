# 2026-11-18 · G3-B03 数据准入 T3 与低磁盘准入 T3 独立复审收口

## 背景

由独立第二读者会话（`dc086121`）对 `main`（`e5f3bfc`）上的 **数据准入 T3（`data migrate` / 备份验证绑定 / 维护锁协作准入）** 与 **低磁盘准入 T3（CLI / RPC / UI 入口与 W10 子代理交汇边界）** 执行独立探针审计：

- **Data-Admission T3**：全部验收判据独立探针裁决 **`PASS`**（零代码缺陷）。
- **Low-Disk Admission T3**：识别出两项跨特性交汇与 F14 展示缺陷（`B-F1`、`B-F2`）及一项既有挂账核对（`BASE-B1`），在本分支集中闭环修复。

## 根因与修复内容

1. **修复 `B-F1`：`TaskStore.create_subtask(..., write=True)` 绕过创建前磁盘准入（`src/tianshu/tasks.py`、`src/tianshu/dispatcher.py`）**
   - **根因**：低磁盘契约在 W10 之前假设子任务恒为 `pure_only`，W10 引入 `write=True` 后当父任务存在可写绑定时子 Run 升级为 `EFFECTS`，但 `create_subtask` 直接进入写事务调 `_insert_run_locked`，未先走 `self.require_new_run(...)`，导致直接调用或在派发守卫晚期低盘窗口下仍可创建 `EFFECTS` 子 Run。
   - **修复**：在 `TaskStore.create_subtask` 开启 SQLite 写事务之前，按子任务实际改写后的可写根调用 `self.require_new_run(child_policy, writable_roots=...)`（`write=False` 及父全只读如实降级的 `PURE_ONLY` 路径仍无 2 GiB 阈值门、不在裸 `TaskStore` 误拒）；同步在 `Dispatcher.dispatch` 的 `_SubtaskTarget` 守卫异常捕获中纳入 `DiskAdmissionError`，确保守卫内晚期空间跌落时完整回滚子行/预算预占/`execution.started` 并记录 `dispatch.rejected`（`effect_state="none_applied"`）。
   - **测试**：在 `tests/test_low_disk_run_creation.py` 补齐裸 `TaskStore` 上下文缺失拒绝、全盘与仅子工作区低盘创建前事务外拒绝、只读降级正控及 `Dispatcher` 守卫晚期低盘回滚正负控。

2. **修复 `B-F2`：控制面低盘错误分类防污染与 UI F14 人话映射（`src/tianshu/control.py`、`ui/src/api/errors.ts`、`ui/src/api/host.ts`、`ui/src/lib/humanize.ts`）**
   - **根因**：`ControlPlane.handle()` 原先让 `DiskAdmissionError` 落入 `_generic_error(exc)` 子串匹配，工作区路径或底层异常若含「不能」「必须」「不存在」「非法」即被误判为 `conflict` / `bad_params` / `not_found`；前端 `humanizeHostError` 直接截断透出原始 `dispatch.disk_space_low:` 机器枚举前缀，且 `humanize.ts` 的 `ACTION_LABELS` 漏登低盘三项 C4 `rule_id`。
   - **修复**：
     - `ControlPlane.handle()` 在 `_generic_error` 之前显式捕获 `(DiskAdmissionError, RunEffectsDisallowed)`，固定返回 `ControlError("internal", ..., {"rule_id": exc.rule_id})`；
     - `errorFromEnvelope` 与 `RealHostClient` 保留信封中的 `details`；`humanizeHostError` 识别 `dispatch.disk_space_low` / `dispatch.disk_space_unknown` / `dispatch.run_effects_disallowed`（含路径含「不能」及 `undo_failed` 包装场景），输出清晰中文提示且不向主界面泄露原始 `rule_id`；
     - `ui/src/lib/humanize.ts` 为三项 C4 规则补齐中文标签并在 `dispatch.rejected` / `policy.denied` / `tool.result` 拒绝分支统一输出人话；同步重编提交 `ui/dist`。

3. **核对保留项与独立契约分支说明**
   - **`BASE-B1`（`src/tianshu/cli.py` `_engine` 未传 `budget=stack.budget`）**：已核实既有缺口现状；因并行 PR #193 正在修改 `src/tianshu/cli.py`，本分支刻意不触碰 `cli.py` 以保持零冲突，留待 #193 合入后单独收口。
   - **契约文字同步**：`docs/low-disk-admission-contract.md` 关于 W10 `write=True` 子任务资格的契约澄清按仓库规范由独立 `docs(contract):` 分支单独提交，不与本代码修复混装。
