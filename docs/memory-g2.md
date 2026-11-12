# G2 记忆阶段：交付与边界（2026-11-12）

> 施工规格：[SPEC-G2-MEMORY-2026-11-12.md](SPEC-G2-MEMORY-2026-11-12.md)。
> 本文只写**已经做成什么、怎么验的、还差什么**，不重复规格里的设计论证。
> 数字以本机 macOS arm64 实测为准；CI 三平台为准，Windows 已知红且非 required。

## 1 这一段要回答的两个问题

| 问题 | 本阶段的答案 | 证据 |
|---|---|---|
| 用户能不能看见"系统记住了我什么" | **能**：右上角「记忆」→ 面板（列表 / 检索 / 确认 / 忘记 / 删除 / 亲手记住） | `docs/m1-memory-panel.png`（真 Daemon + 真 `ui/dist` + 真 Host，1280×900） |
| 记忆到底让任务更好吗 | **M2 尚未交付**：真实用户任务集 8 条里还没有记忆任务 | 见 §4 未做清单 |

## 2 M1 已交付（可查看）

### 2.1 控制面 `memory.search` RPC（第 9 个记忆方法）

- `src/tianshu/memory_control.py` `search_memories()`：走投影的 **`retrieve()`** 而不是
  `search()`。理由：`search()` 是 FTS-only，而 `semantic` / `degraded` / `fused`
  只在 `retrieve()` 的 `RecallOutcome` 上；人在 UI 上检索时，空结果必须能分清
  "没有这条记忆"与"这次只有词面召回"。
- `src/tianshu/control.py` `_memory_search`：参数 `query` / 可选 `scope` + `scope_id` /
  可选 `limit`；**没有 `status`**（盘点用 `memory.list`）；响应带 `items` /
  `semantic` / `degraded` / `fused` / `query` / `limit` / `scopes`（回显实际生效的
  可见作用域）。条目形状与模型侧 `loop._memory_item` 同口径：六项判据 +
  `content_ref` + `score` / `score_kind`，`stale` 三态里 `None`（判不了）省略该键。
- 作用域口径按既有语义、不新造：默认只有 `user`；显式 `scope` 是**追加**一个
  可见作用域（人永远看得见自己的 user 记忆），不是替换。

### 2.2 UI 记忆面板

- 入口：`AppHeader` 右上角「记忆」按钮 → `MemoryDialog`（宽模态）。
- 组成：`components/MemoryPanel.tsx`（纯展示 `MemoryPanelView` + `MemoryRow`）、
  `components/MemoryDialog.tsx`（取数/两个本地交互：看正文、删除前二次确认）、
  `lib/memoryController.ts`（状态机）、`lib/memoryView.ts`（人话层）、
  `api/memory.ts`（RPC 面 + 归一）。
- 三条渲染纪律：正文**纯文本**（React 默认转义，XSS 载荷测试钉住）；`stale` 三态
  不压成两态（省略 = 判不了，不显示"新鲜"）；能力标注照实显示（`semantic=false` +
  `degraded` 原因），空结果时一并给可见作用域。
- 写操作**不乐观更新**：改完重新拉列表（记忆有 revision，猜状态会骗人）；`conflict`
  （revision 过期）刷新 + 提示，**不自动重试**——自动重放会悄悄替人确认一个没看过的版本。
- 「忘记」与「删除」分开：`memory.forget` = 停召回（tombstone）；`memory.erase` =
  删正文副本与索引，**二次确认**并写明"事件日志里的读快照会保留"（不许写"物理抹除"）。

### 2.3 顺带修掉的一条：控制面任务污染任务列表

真机截图第一版暴露的问题：人一点「记住一条」，控制面就开一个
`memory.remember:...` 的任务，**任务列表里凭空多出"工作量"**，还会被首屏选中。
修法：`task_view` 新增 `control` 字段（加键不加类型），UI 在 `normalizeState` 里
把 `control: true` 的任务摘掉（旧后端没有这个键时按 false，不误藏用户任务）。
截图第二版里任务列表是"还没有任务"，面板里能看到两条 user 记忆——这是修好之后的样子。

### 2.4 判据与负控（M1）

| # | 判据 | 测试 |
|---|---|---|
| M1-a | `memory.search` 走 `retrieve()`，`semantic`/`degraded`/`fused` 与投影一致 | `tests/test_host_memory.py::test_memory_search_rpc_returns_capability_labels` |
| M1-b | 作用域过滤：默认不串 task；指名 task A 后 task B 仍不可见 | `...::test_memory_search_rpc_is_scope_filtered` |
| M1-c | 空结果与能力降级是不同形状，不是错误 | `...::test_memory_search_rpc_empty_is_not_an_error` |
| M1-d | 参数错误面统一 `bad_params`；`limit` 超上限按上限夹 | `...::test_memory_search_rpc_param_errors` |
| M1-e | 正文纯文本渲染（XSS 不落 DOM） | `ui/src/components/__tests__/memory-panel.test.tsx` |
| M1-f | `conflict` 刷新 + 提示、不自动重试（写操作只发生一次） | `ui/src/lib/__tests__/memoryController.test.ts` |
| M1-g | 控制面任务不进任务列表（用户任务不被误藏） | `ui/src/api/__tests__/normalize.test.ts` + `tests/test_control.py::test_control_plane_tasks_are_marked_control` |

负控（真跑，变异 → 失败:通过 → 复原）：

| # | 变异 | 结果 |
|---|---|---|
| NC-M1 | `search_memories` 改用投影 `search()`（FTS-only） | **2 failed / 16 passed**（能力标注、空结果不是错误） |
| NC-M2 | 控制面忽略显式作用域（永远只看 user） | **2 failed / 16 passed**（作用域过滤、参数错误面） |
| NC-M3 | 条目只给 `memory_id`（丢掉六项判据） | **1 failed / 17 passed** |

UI 侧负控由测试自带：把正文换成 `dangerouslySetInnerHTML` 或删掉 `conflict` 分支，
对应 vitest 会红（见上表 M1-e / M1-f 的用例本体）。

### 2.5 实测数字（本机，macOS arm64，2026-11-12）

- 全量：**2083 passed / 7 skipped**，**363.94s**（`uv run --frozen pytest -o addopts="" -q`）。
  基线 `main @ c87ab09`（记忆阶段规格合入后）同机 **2078 passed / 7 skipped**；
  本阶段 **+5** = `tests/test_host_memory.py` 4 条（M1-a…d）+ `tests/test_control.py`
  1 条（控制面任务标记）。
- UI：`pnpm test` → **10 个文件 / 75 条**（本阶段 +24：面板渲染 8 / 控制器 8 /
  人话层 6 / 归一 2）；`pnpm build`（`tsc --noEmit` + vite）干净，`ui/dist` 已重建提交。
- `ruff check .` → **All checks passed!**
- 负控：见 §2.4 表（2F/16P、2F/16P、1F/17P，每条复原后 `git status --short` 为空）。
- 没有跑 CI 数字；Windows job 非 required 且既有基线红，**不写成绿**。

### 2.6 怎么复现截图

```bash
uv run python scripts/demo_memory_panel.py --data-root /tmp/m1-memory   # 打印界面地址
# 打开后用真 Chrome（headless 亦可）点右上角「记忆」，1280×900 截图
uv run python -c "from pathlib import Path; from tianshu import daemon; \
  print(daemon.request_shutdown(Path('/tmp/m1-memory'), reason='done', timeout=5))"
```

截图来自**真进程**：真 Daemon（空脚本模型，不联网、不花钱）+ 真 `ui/dist` +
真 Host 桥；三条种子记忆经由控制面人路径（`memory_control.remember` → dispatcher →
intent/result + 一次性审批 + 执行侧二次核验）写入，不是直接改投影。

## 3 仍不成立 / 未验证（M1 之后）

1. **没有真实 embedding**：本机 `memory.search` 永远 `semantic=false` + 明确
   `degraded` 原因（`HashEmbeddingProvider` 无语义能力）。面板照实显示"这次只有词面召回"，
   **不**声称"语义检索已启用"。
2. **UI 只在 Chromium + 本机 daemon 上验过**：Windows 命名管道 ACL 未加固且未验证；
   面板不覆盖多用户、不覆盖 token 轮换。
3. **控制面端点不是授权边界**（F10 §13 #30）：同 uid 的 `run_command` 子进程可达；
   端点权限只在**跨本地用户**这一层有意义。
4. **列表不带正文**：面板列表显示"（点开才加载正文）"，正文要 `memory.get`。
   这是刻意的（列表可能几十条），但意味着人不能一眼扫完全部正文。
5. **擦除的物理边界不变**：事件日志里的读快照保留；面板文案照此写，不承诺抹除。

## 4 未做（本阶段剩余）

- **M2 可证明收益**：`RU-M1@1.0.0` 跨任务召回真实用户任务 + `Case.followup` +
  判据/负控（规格 §3）。
- **M3 文档对账**：`real-user-tasks` / `PRD-v1 §10.5+§11.1` / `KNOWLEDGE` / `README` /
  交付对账 + 干净 main 实测数字。
- 「记住一条」成功后表单不清空、面板关闭后不保留草稿——M1 未做，登记于此。
