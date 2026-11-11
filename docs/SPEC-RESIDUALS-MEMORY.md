# 剩余记忆工作量施工规格（SPEC-RESIDUALS-MEMORY）

- 立据：[F10-memory-design.md](F10-memory-design.md) §13 的第 **#18–#27** 条（实际状态为
  "策略已测 / 生产无入口""未实现""未落地（入口缺口）""部分落地"的十条），加上 §9
  「可检查、可编辑」在控制面的兑现缺口（`host.py` 无任何记忆端点）。
- 基线：`main @ 915be6a`（PR #73 之后，`uv run --python 3.12 --frozen pytest
  -o addopts="" -q` 实测 **1316 passed / 6 skipped**；数字只对这次实测负责）。
- 范围：让 L1 从"模型能用"补齐到**人能管、编辑得动、忘得掉、恢复说得清**，并把
  L2/L3 与向量召回从设计落成可回放投影。**明确不做**的写在 §9。
- 判定口径（沿用 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md) §0）：
  **有回归测试、且做过负控（把修复回退后测试会红）才算修好**。读不懂 ≠ 没写过；
  判定不了 = unknown + 阻断。投影层能消费某个字段 ≠ 有生产入口。

## 0. 施工纪律（所有人必须遵守）

1. **只在自己的 worktree 里操作**。禁止在 `/Volumes/Date/天枢Agent`（主检出）跑
   `git` 或改文件——那是另一个会话的工作区。本规格的 worktree 是
   `/Volumes/Date/ts-wt-memory` 派生出的各分支。
2. **契约先行**：本文 §1 列出的契约增量必须单独合入 `docs(contract):` PR，body 写
   「原方案 → 新方案 → 为什么 → 影响哪些代码」，**不夹带行为实现**。契约 PR 不合，实现
   PR 不开工。注意「不夹带代码」的边界：**工具与规则编号的注册**（`tools.SPECS` /
   `rules.RULE_IDS` / `schemas/c4/rules.json`）必须随**产生路径**在同一次改动落地——
   `DECLARED_UNUSED_RULE_IDS` 只给防御兜底，不拿它给计划功能占位。契约 PR 只写文档：
   PRD §4.1 / §5.2.2 / §5.4.2 与 F10 §5 / §7 / §9 / §10。
3. **禁止改**：`docs/KNOWLEDGE.md`、`README.md`、`docs/PRD-v1.md`、`docs/F10-memory-design.md`、
   `src/tianshu/events.py`、`src/tianshu/errors.py`、`tests/conftest.py`。这些由 lead 在契约
   PR / 收尾轮统一落；工作流需要改一行就发消息给 lead。**例外**：`schemas/c4/rules.json`
   允许**追加**本工作流新登记的规则编号（`rule_id.enum` / `x-rule-descriptions` /
   `x-rule-groups` 三处同一编号），不许改既有条目——契约要求"编号与产生路径同一次改动"，
   锁死它等于让工作流无法完成三处同改。
4. **工具面绊线不许删**：`tests/test_veto_scope.py::test_model_tool_surface_has_no_run_start`
   是**字面判据**。新增/下线工具必须同步登记，删护栏的 PR 直接拒。
5. **一提交一件事**：`feat:` / `fix:` / `test:` / `docs:` 分开；提交必须有正文
   （改了什么、为什么、判据、负控摘要）。
6. **收工前**：`uv run --python 3.12 --frozen pytest -o addopts="" -q` 全绿、
   `uv run --python 3.12 --frozen ruff check .` 干净、`uv lock --check` 干净。
   **不要**自己合并、不要碰 `main`。
7. 干不完就如实报：完成了什么、卡在哪、还差什么。**不许把没验证的写成已修**。

| 工作流 | §13 条目 | 分支 | 起点 |
|---|---|---|---|
| **N1 产生侧** | #18 #22 #24 #25 #27 | `feat/memory-lifecycle` | 契约 PR 合入后的 `main` |
| **N2 检索质量** | #20 #23 | `feat/memory-retrieval` | N1 合入后（同改 `memory.py`） |
| **N3 L2/L3** | #19 | `feat/memory-distill` | N2 合入后（投影 schema v2 与向量表同一套迁移） |
| **N4 擦除边界** | §10（#26 相邻） | `feat/memory-erasure` | 契约 PR 合入后（可与 N1 并行） |
| **N5 恢复 + 审计** | #21 #26 | `fix/memory-recovery-audit` | 契约 PR 合入后（与 N1 并行） |
| **N6 控制面 UI** | §9 | `feat/memory-host-ui` | N1 合入后（复用 N1 的工具与 CLI 路径） |

N1 / N2 / N3 都动 `memory.py`、`memory_exec.py`，**串行**；N4 / N6 动不相交文件，可与 N1
并行；**N5 也动 `dispatcher.py`**（`_reject` / 终态脱敏），与 N1 的 `open_control_run`
同文件，排在 N1 之后。文件所有权冲突时以 N1 为先，后续工作流 rebase 后重跑自己的判据。

---

## 1. 契约增量（先合 PR，不夹带代码）

> 这一节的每条都必须能在合入后写成"代码里哪个函数实现了它"。写不清的不要合。

**C2 工具**（红线 8：schema 变化 = 新工具版本，旧审批自动失效）

1. `memory.propose` **升到 `version="2.0"`**：schema 增 `scope`（`user|task`）与
   `scope_id`。**workspace 作用域不从模型进入**——workspace 是共享边界，共享由人
   发起（见第 3 条），模型只能写 `user`（默认）与**当前 Run 自己的 task**。
2. 新增 `memory.edit`（`version="1.0"`，`exposed=True`）：`memory_id` + `revision` +
   `content` + `kind?`。语义 = 一次审批完成"旧版退役 + 新版激活"，见 N1 §2.3。
3. 新增三个 **`exposed=False`**（不进模型工具清单）的控制面工具。这不是新机制，
   已有先例 `tools.SPECS["restore_file"].exposed == False`（控制面撤销用）；模型侧
   边界由"`tool_manifest()` 只投影 exposed" + "未知工具走 `tool.not_dispatched`" 两道
   共同保证，实施时必须把这两道各钉一条测试：
   - `memory.remember`：人直接"记住这条"，创建即 `origin=user_explicit`，免审批
     （人本人就是批准者），仍走完整 intent/result + Receipt。
   - `memory.promote`：把一条 `user` 作用域记忆提升到 `workspace` / `task` 作用域
     （共享是显式动作，F10 §3.3），人签发一次审批。
   - `memory.erase`：删正文副本与检索索引（N4 的契约入口），`ACTION_DELETE` + 一次性
     审批 + Receipt；"事件日志读快照保留"的边界见 §5。
4. 新增 `memory.consolidate`（`exposed=True`，`version="1.0"`，`action=ACTION_CREATE`）：
   触发 L2/L3 合成（N3）。合成只写投影、可重建，**不挡审批**；但合成结果默认
   `candidate`，要人 `confirm` 才进召回。

**C4 权限**

5. 动作映射：`memory.edit` / `memory.promote` → `ACTION_OVERWRITE`（等同覆盖，需审批）；
   `memory.remember` → `ACTION_CREATE`，免审批但只允许控制面 Run；`memory.erase`
   （N4）→ `ACTION_DELETE` + Receipt。
6. **只新增一条规则编号** `c4.memory_control_scope_binding`（控制面专属能力被非控制面使用；
   契约 PR 已写进 PRD §5.4.2）。另外两种拒绝**复用现有编号**，不造同义新号：作用域越界 →
   `c4.memory_scope_denied`；编辑 revision 过期 → `dispatch.memory_stale_revision`。擦除回执
   对不上**不设编号**（Receipt 在结果侧产生，结论是 `effect_state=unknown`，不是"被拒"）。
   登记（`rules.RULE_IDS` + `schemas/c4/rules.json` 三处 + PRD §5.4.1 表格）与**产生路径**在
   同一次改动三处同改。理由：`DECLARED_UNUSED_RULE_IDS` 是防御兜底，不是计划功能占位；
   `tests/test_rule_ids.py` 的判据是"登记了必须有产生路径"。
7. `Dispatcher.open_control_run` 增可选 `memory_scopes`，并在 `task.created` payload 写
   `control: true`；`Task` 增加 `is_control`（从事件投影读回）。**执行侧判据**：
   `memory.remember` / `memory.promote` / `memory.erase` 与 `memory_scopes` 只在
   `is_control` 的 Run 可用；越权 → `c4.memory_control_scope_binding`。用途：让 CLI 能给
   旧 task 作用域的记忆做确认 / 遗忘（#27），且**不**因此获得该 task 的 workspace 绑定。
   payload 形状未冻结，**加键不加事件类型**；`control` 必须落进 `task.created` payload，
   不许只放在进程内对象里——否则崩溃恢复后就分不清谁是控制面。

**记忆实体（PRD §4.1）**

8. 加 `layer: l1 | l2 | l3`（默认 `l1`）；加 `erased_at?`（N4）；`status` 封闭集
   **不变**（擦除复用 `forgotten` + `erased_at`，不新增第五个状态——新增状态要牵动
   三处封闭集，收益不足）。
9. **`origin` 的迁移规则写死**：`inferred` 只能在"人批准了 confirm"时升为
   `user_confirmed`（出处=审批记录的 actor），只能由控制面 `memory.remember` 创建为
   `user_explicit`；模型参数里**永远没有** origin。三档之间不允许降级或互相改写。

**投影与审计**

10. 投影 `_SCHEMA_VERSION` 1 → 2（`memories` 加 `layer` / `erased_at` 列，新建
    `memory_vectors`，N3 再动）。**版本比对是新增机制**：当前 `_SCHEMA_VERSION` 只进
    `projection_hash()`，`memory_meta` 只存 `tokenizer_version`，没有"已存版本号"可
    比对。本次要把版本号写进 `memory_meta`，不符时**按投影语义重建**（投影可重建，
    允许），并把"重建耗时 / 条数"写进日志。
11. 派发审计表：请求进入**终态**（done / rejected / failed）后，`args_payload` 里的
    记忆正文替换为 `content_sha256` + `content_bytes`；请求表加 `args_redacted` 标志。
    理由与边界见 N5 §5.2。

---

## 2. N1 · 产生侧：origin 两档 / 作用域 / 编辑 / CLI

### 2.1 现状（按代码读出来的）

- `memory_exec.MemoryExecutor._propose` 固定写 `origin="inferred"`、"status=candidate"，
  写进 output 的字面量在 `_propose` 的返回字典里；注释写明"模型不能自称 user_explicit"。
- `memory._apply_confirm` 的主体 `UPDATE` 是 `revision / status / consolidated_at /
  last_seq`，**不改 origin**；另在"正文不可用→恢复"分支补写 `content_state` /
  `fts_rowid`（与 origin 无关）。`_parse_transition` 不接受 output 里的 origin。
- `tools.SPECS["memory.propose"].input_schema` 是 `additionalProperties: false` 且**没有**
  `scope` / `scope_id` / `target`；`loop.AgentLoop._memory_scope_from_args` 其实已经能解析
  `target` 与 `scope`，只是 schema 把模型的参数挡在门外 → 生产上 propose 恒为 `user/local`。
- `supersedes` 只出现在 `memory.py`（`Memory.supersedes` / `_apply_propose` / `_supersede`）
  与 `cli.py` 的只读打印；`grep -rn supersedes src/tianshu` 无工具/执行器写入它。
- `cli._dispatch_memory_decision` 遇到 `scope == "task"` 直接抛错（"必须在所属任务的 Run
  内完成"）。**别把它写成"没人能忘记"**——第二读者核对后纠正过我这一处：`tasks.py` 里
  没有"任务结束"这个状态（`task.status` 恒为 `open`），同一条 task 上可以再开一个 Run，
  由模型发 `memory.forget` + 人 `approvals approve` 走完。所以事实是**人能忘，但只能
  绕道模型 Run**；控制面没有直接入口。这是控制面缺口，不是隐私死锁。
- `host.py` 里 `grep -rin memory` 零命中：UI 没有记忆入口（F10 §9 的"能打开直接编辑"
  目前只有 CLI）。

### 2.2 设计

1. **origin 由审批出处决定，不由参数决定**。执行器构造 confirm 的 output 时读审批记录的
   actor：`HumanIdentity` → `origin="user_confirmed"`；否则（模型请求、无审批）拒绝。
   `_propose` 只在 `admitted.tool_id == "memory.remember"` 且该工具不在模型可见清单里时
   接受 `origin="user_explicit"`。这两条都要在执行侧独立复核，**不信任 prepare 的结论**。
2. **作用域创建**：`memory.propose` v2.0 的 `scope ∈ {user, task}`，`task` 的 `scope_id`
   必须等于本 Run 的 `task_id`（执行器从 `TaskStore` 读，不取模型给的 id）；
   `workspace` 一律走 `memory.promote`（人签发），模型写 `c4.memory_scope_not_in_loadout`。
   理由：task 作用域天然按任务隔离，写它不产生跨任务泄漏；workspace 是共享边界，
   共享必须是显式动作（F10 §3.3）。
3. **编辑**：`memory.edit` 一次审批完成"旧版退役 + 新版激活"：
   - 投影新增 `_apply_edit` 分支（仍是 `tool.result`，**不新增事件族**）：新行是**新
     `memory_id`**（不是同 id 推进 `revision`）、`revision=1`、`status=active`、
     `supersedes=<旧 id>`；旧行走 `_supersede`（`revision` 不变、保留行、移出召回，仍可按
     id 查到）。**不允许改 `kind`**——改 kind 等于换一条记忆。
   - 执行器必须绑定"人看到的那一版"：请求 preconditions 带 `before_revision`，执行侧从
     权威投影再读一次核对（沿用 `check_request_preconditions`，失败 →
     `dispatch.memory_stale_revision`）。
   - 新正文必须过写入闸门 + 去重（与 propose 同一条路径，不复制实现）。
4. **promote**：`memory.promote(memory_id, revision, scope, scope_id)`；**移动语义**：原行
   立即 `superseded`（原作用域不再召回），目标作用域新行 `candidate`（人 `confirm` 后才
   召回）。`source_refs` 必须包含原 `memory_id`（执行侧机械校验；`created_at` 是整数，
   不是引用，不要往里塞）。Receipt 针对**原行**（`root`=原 scope URI、`resource`=原
   `memory_id`、写前/写后用稳定状态串）。目标作用域必须落在控制 Run 声明的
   `memory_scopes` 或绑定目录内。
5. **CLI**：`tianshu memory remember <content> --kind ...`（`memory.remember`）、
   `tianshu memory edit <id> --revision N`（走审批 + `memory.edit`）、
   `tianshu memory confirm|forget --task-id <id>`（控制 Run 带 `memory_scopes=[task]`，
   只授予该作用域，不授予 workspace 权限）。目的是让人**不必先起一个模型 Run** 才能决定
   自己记忆的去留；**不是**"否则做不到"（同一 task 再开 Run 也能走，只是绕）。
6. **`memory.edit` 的正文回显**：审批界面/CLI 打印新正文时截断到 500 字节并标注
   "已截断"，全文只落 blob（审批记录不存第二份正文）。

### 2.3 判据

- `tests/test_memory_lifecycle.py`（新）：propose（`scope=task`）落在本任务；`scope_id`
  与模型给的 id 不一致 → 拒绝；propose 到未绑定 workspace → `c4.memory_scope_not_in_loadout`；
  `memory.remember` → `user_explicit` 且无 approval 记录；人批准 confirm → `user_confirmed`
  且 `revision+1`；`memory.edit` → 旧行 `superseded`、旧行仍可 `get`、新行召回、一次
  approval 只消费一次；`memory.promote` → 新行 scope 正确且 `source_refs` 含原 id。
- `tests/test_memory_dispatch.py` 扩：在 fixture 里把 `origin` 塞进 `memory.propose` 参数 →
  schema 拒绝（`additionalProperties: false`）；把 `memory.remember` 从模型清单调 → 拒绝。
- `tests/test_memory_loop.py` 扩：CLI `--task-id` 对已结束任务的 task 记忆执行忘记 →
  成功，且该控制 Run **不持有任何目录租约**（探针：`leases.held_paths()` 为空）。
- **负控**（必须贴红）：把 origin 判定改回"从参数读" → "模型自助提权"用例必须红；
  把 `_apply_edit` 的 supersede 去掉 → "旧版仍在召回"用例必须红。
- `replay`：编辑/提升/记住各跑一次 `replay`，`messages_hash` 校验通过、重建后两行
  revision 链一致。

**证据**：提交正文里贴负控输出；lead 汇总到 `docs/memory-residuals-evidence.md`。

---

## 3. N2 · 检索质量：作用域优先级 + 向量召回（RRF）

### 3.1 现状

- `MemoryProjection.search` 的排序键是 `(_ORIGIN_WEIGHTS[origin], -created_at, memory_id)`；
  **作用域只用于过滤**（`allowed` + loadout），不参与打分 → #23 未实现。
- `grep -rin --include=*.py 'embedding\|vector' src/tianshu` 只命中 `memory.py:74` 的注释
  "不做向量召回，不引入 embedding 依赖"（按字面带 `-r` 会连 `.pyc` 一起命中二进制，
  那不叫代码）；没有向量表、没有接缝 → #20 未实现。
- 检索信号只有 FTS5 + CJK 双字切分（`_tokenize`），无几何信号。

### 3.2 设计

1. **作用域优先级**：排序键改为
   `(scope_proximity, origin_weight, -created_at, memory_id)`，其中
   `scope_proximity` = 当前 task 0 / 本 Run 绑定的 workspace 1 / user 2。**纯函数**、
   同分同序、可单测（不依赖库内容之外的随机性）。
2. **向量是可重建投影，不是第二份事实**：新模块 `memory_index.py`
   - 表 `memory_vectors(memory_id PK, revision, model_id, dim, vec BLOB, last_seq)`；
   - `EmbeddingProvider` 协议：`model_id` / `dim` / `embed(list[str]) -> list[list[float]]`；
   - 内置 `HashEmbeddingProvider`（确定性 token 哈希，**只用于测试与离线基线，禁止
     对外声称语义能力**）；
   - 外部 provider 只有在用户显式配置 + 该任务的数据外发许可下才启用；未配置 →
     **降级 FTS-only 并如实标注**，不静默假装有语义。
3. **不混空间**：检索/融合前先按 `model_id + dim` 过滤；provider 换了 → 该批向量不
   参与（重新 `rebuild_vectors()` 后才恢复）。索引重建是幂等的，向量写入与
   `tool.result` 同事务。
4. **RRF 融合**：两路（BM25 / 向量）在**同一过滤集**上各自排序，`score = Σ 1/(60+rank)`，
   同分按 `memory_id` 稳定；候选先过滤（scope / status / expires）再排序再裁剪
   （F10 §8 的顺序不变）。
5. **reranker**：只留接口 + 评测入口，**默认关闭**；评测证明增益后再谈默认开。

### 3.3 判据

- `tests/test_memory_rank.py`（新）：作用域优先级顺序逐条断言（同一 query 下 task >
  workspace > user）；同分稳定；`expires_at` 过期项不参与。
- `tests/test_memory_vectors.py`（新）：同一批记忆两次 `rebuild_vectors()` 结果逐字节
  相同；`model_id` 变化后旧向量不参与且检索报告 `semantic=false`；provider 缺失 →
  FTS-only 且输出里 `degraded` 如实标注；RRF 分数可手算核对（固定 5 条夹具）。
- **负控**：把融合改成"只按 FTS 排" → 语义样例（同义改写）用例必须红；把过滤挪到
  排序之后 → 越权样例必须红。
- 评测：固定中文 + 中英混合样例（≥20 条 query），`Recall@5` / `MRR` 对 FTS-only 与
  hybrid 各跑一次，报告落 `docs/memory-retrieval-eval.md`。**只报实测数字**；同义改写
  样例若 hybrid 没赢，就如实写没赢。

### 3.4 落地记录（N2 实际，2026-11-11，分支 `feat/memory-retrieval`）

- **作用域优先级（#23）**：`memory.RecallPriority(task_id, workspace_roots)` +
  `proximity_pair()`；排序键 `(优先级, origin 权重, created_at 降序, memory_id)`。
  关键决定：优先级是**上下文**的函数，不是 `allowed` 的函数——这正是旧设计里
  "压序会依赖调用方传了哪些 scope"的解法（`allowed` 只决定可见性）。主循环
  `loop._memory_priority(task)` 从 Task 的绑定目录算，CLI 从 `--task-id/--workspace` 算。
  测试 `tests/test_memory_rank.py`（7 条）；负控：去掉优先级 → 顺序用例红。
- **向量（#20）**：新模块 `src/tianshu/memory_index.py`：
  - `EmbeddingProvider` 协议（`model_id` / `dim` / `embed`）；
  - `HashEmbeddingProvider`：确定性哈希基线，**无语义能力**（模块 docstring 写死"不许
    对外声称语义召回"，评测报告也按这个口径写）；
  - `MemoryIndex`：表 `memory_vectors` / `memory_vector_meta`；`model_id+dim` 过滤；
    `availability()` 返回 `(可用, 原因)`——**要求 built 标记 + provider 身份 + 覆盖校验**
    （空表/半套向量/换 provider 未重建一律不可用）；`rebuild()` 清空重算并置 built；
    `rf_fuse()` 纯函数（`k=60`）；建表在回滚事务后可重试（与投影同款缓存修正）；
    `search()` 的 `now` **必填**（过期判定不接受"默认当前时间"）。
  - `MemoryProjection` 用 duck-typed `index=` 接上（不 import `memory_index`，避免环），
    在 `_apply_propose`（仅 active）/`_apply_confirm`/`_supersede`/`_apply_forget`/
    `_apply_erase` 里维护向量；`rebuild()` 清空向量后按事件重放重建。
  - `MemoryProjection.retrieve()` 返回 `RecallOutcome(items, semantic, degraded, fused)`；
    **候选是 FTS ∪ 语义的并集**（语义路即使 FTS 为空也跑；语义独有候选按同一套过滤
    并入），RRF 融合 bm25 与语义两路名次后再按作用域优先级分组。`search()` 保持
    **明确的 FTS-only**（不偷偷变 hybrid），`Recalled.score_kind` 标明 `origin`/`rrf`。
    主循环 `memory.search` 输出新增 `semantic` / `degraded` / `fused`，条目带
    `score_kind`；CLI `--json` 同样三项（形状由数组改对象，README 标注 breaking）。
  - **生产接线**：`build_stack(embedding_provider=...)` / `TIANSHU_EMBEDDING_PROVIDER=hash`
    （CLI / daemon / worker 走同一装配路径）；只接受 hash 基线，外部 provider 需数据
    外发许可、尚未接线（代码里报错而不是静默忽略）。**构建入口**是
    `tianshu memory rebuild-vectors`：只装配 provider 不会构建向量，必须显式建一次
    索引（否则 `availability()` 报"尚未建立"、检索永远 FTS-only）。
  - **能力边界**：未配置 provider → FTS-only + `degraded` 原因；换 provider 未重建 →
    旧向量不参与且仍报降级。`memory_vector_meta` 只在**成功 `rebuild()`** 时写 built/provider（建表不写），
    后接的 provider 不能改写历史（否则 availability 会误报 True）；只有成功 `rebuild()` 才写 built/provider。
- **测试**：`tests/test_memory_vectors.py`（26 条）+ `tests/test_memory_rank.py`（7 条）
  + `tests/test_memory_loop.py` 的 3 条输出/接线用例。负控 5 条都真跑过：去掉优先级 /
  availability 永不降级 / 去掉确认后的索引钩子 / 语义路只采纳 FTS 候选 / 去掉索引建表
  的事务判断。
- **评测**：`scripts/eval_memory_retrieval.py` + `docs/memory-retrieval-eval.md`
  （22 query / 25 记忆；FTS MRR 0.864 → hybrid-hash 0.955（mixed 0.875）；**Recall@5 两者都
  1.000 = 饱和**，报告里写清"不是语义能力证明"）。
- **偏差 / 没做到**：
  1. `_SCHEMA_VERSION` **没有**从 2 再往上加：`memory_vectors` 是**可选派生表**，随
     provider 身份落在 `memory_vector_meta`，不进 `memories` 的形状契约；规格 §1 #10 原写
     "新建 memory_vectors 属于 v2"，N4 已把 v2 用掉，这里按"派生表不参与版本"处理。
  2. 没有真 embedding provider（只有哈希基线）；语义能力要等用户显式配置的 provider +
     数据外发许可（F10 §10）。
  3. `assess_run` 之类的恢复路径不消费向量（向量可重建，不参与副作用结算）。
  4. reranker 只有接缝设想，未实现；评测不含重排效果。
  5. 跨千条记忆的召回上限、RRF `k` 的敏感性、bm25 与优先级的加权变体都没评测。
  6. **没有真 embedding provider**；生产只接受 `hash` 基线（评测结论仅"两路名次融合"，
     不等于语义能力）。外部 provider 的配置项、数据外发许可、密钥管理都未接线。
  7. 首轮复审抓到的**两个真洞**已修：语义路原来只是"FTS 候选的重排器"（FTS 空就不跑、
     语义独有候选被丢），以及 `availability()` 在空索引/混合 provider 下报假 true。

---

## 4. N3 · L2/L3 蒸馏（增量、带证据、可回放）+ freshness

### 4.1 现状

- `grep -rin distill src/tianshu` 零命中；`Memory` 无 `layer` 字段（`memory.Memory`
  的 15 个字段里没有）；`consolidated_at` 只在 confirm 时写一次。
- L2 要求"带证据的信念"（`evidence[]` + `proof_count` + 原文引文）与 L3"预算好的可
  启动页面"在数据上没有落点。

### 4.2 设计（先确定性，后 LLM）

1. **数据**：`layer ∈ {l1, l2, l3}`（契约 §1.8），投影列随 schema v2。
2. **L2 v1 确定性合成（本轮只做这个）**：同作用域内 `active` 的 L1 记忆按稳定 token
   交叠聚类（阈值写死并进测试），合成正文 = 引用式拼装，**每条一行**
   `- <原文一行摘要> [<memory_id>@<revision>]` + 空行分隔；`evidence` = 每条的
   `{memory_id, revision, quote}`（quote 截断到 200 字节并标注截断），`proof_count` =
   实际条数。因为不是"生成"，两次合成**逐字节相同**，增量 delta 天然成立。
3. **触发与状态**：`memory.consolidate` 写 `candidate` 的 L2/L3；人 `confirm` 后进入
   召回。合成只动投影，不改 L1 行；旧 L2 通过 `supersedes` 保留（N1 已提供入口），
   **旧版内容与它当时站的 evidence 一起保留**。
4. **L3 = 可启动页面（DB read）**：`user` 作用域、`kind=preference`、`origin ∈
   {user_explicit, user_confirmed}` 的 active 记忆，按字节预算（≤4 KiB）打包；读取
   只查投影，不检索、不调模型。
5. **freshness**：召回 L2/L3 时比较作用域内 L1 的最大 `last_seq` 与
   `consolidated_at`；有更新 → 结果标 `stale=true`、**降权但不短路**，同时回落 L1
   证据（F10 §8 的"陈旧层先核验再用"）。
6. **LLM 合成明确延后**：将来若做，生成正文必须作为 `tool.result` 落事件（replay 用
   快照，不重算），并与确定性合成分两条代码路径 + 两个测试文件。

### 4.3 判据

- `tests/test_memory_distill.py`（新）：固定 8 条 L1 夹具 → 两次 `consolidate` 输出
  字节相同；改 1 条源 → 只有相关行变化，其余行**逐字节不变**（`bytes` 比较，不比较
  "看起来一样"）；`proof_count` 与 evidence 条数机械核对；旧版 L2 仍可 `get` 且
  supersede 链方向正确。
- freshness：新增一条 L1 后，原 L2 结果 `stale=true` 且不再排在 L1 证据前面。
- replay：重建后 L2 内容与 `consolidated_at` 一致；`messages_hash` 通过。
- **负控**：把增量合成改成"全量重排重生成" → 逐字节不变用例必须红；去掉 freshness
  判据 → stale 用例必须红。
- 边界如实写：确定性合成**不等于**语义摘要；不承诺"比 L1 更好用"，除非评测拿出来。

### 4.4 落地记录（N3 实际，2026-11-11，分支 `feat/memory-distill`）

- **数据层**：`memory.Memory` 第 17 个字段 `layer`（默认 `"l1"`），封闭集
  `MEMORY_LAYERS = {l1, l2, l3}`。投影 `_SCHEMA_VERSION` **2 → 3**：`memories` 加
  `layer TEXT NOT NULL DEFAULT 'l1'`，**in-place `ALTER`**（旧行一律 l1，不重建、不丢
  正文）；更高版本仍 fail-closed。`_MEMORY_COLUMNS` / `_row_to_memory` / INSERT / FTS
  的 SELECT 都带 `layer`——N2 之后 FTS 是召回装配主路径，漏一列就是 `IndexError`（实现
  时真踩过）。
- **确定性合成器 `src/tianshu/memory_distill.py`（新）**：
  - 常量写死并进测试：`L2_SIMILARITY = 0.34`（**重叠系数** `|A∩B| / min(|A|,|B|)`，不是
    Jaccard：中文短句 bigram 多，Jaccard 被长度差压垮）、`L2_MIN_GROUP = 2`、
    `L3_PAGE_BYTES = 4 * 1024`、`QUOTE_LIMIT_BYTES = 200`、`BASIS_SEQ_PREFIX = "seq:"`。
  - token 集合 = ASCII 词 + 汉字单字 + 相邻汉字双字组（与 FTS 分词**分开实现**；只收
    双字时短句交叠被长度差压垮，只收单字则被"的/用"这类高频字主导）。
  - L2 = 簇内引用式拼装：`- <原文压成一行> [<memory_id>@<revision>]`，块间空行；
    `evidence` 每条 `{memory_id, revision, quote}`，`proof_count == len(evidence)`；
    引文超 200 字节截断并标 `…[截断]`（不静默砍）。
  - L3 = user 作用域 + `kind=preference` + `origin ∈ {user_explicit, user_confirmed}` +
    `layer='l1'` + active，按 4 KiB 预算**整行**装入；放不下就停并追加
    `[预算已满：…]`（不做半行截断）。**只做 user 作用域**：偏好页是"关于这个人"的页面。
  - `source_refs = ("seq:<basis_seq>", *sorted(memory_id))`，`basis_seq = max(last_seq)`。
    `page_memory_id = mem_<sha256(scope_uri \0 layer \0 content)[:16]>`：同作用域 + 同层 +
    同正文 ⇒ 同 id，重复 `consolidate` 幂等。
  - `consolidate()` 就位校验：合成层非法（`l1` / 未知名）或 `proof_count != len(evidence)`
    一律拒——不给"生成算法漏一条证据"留进门缝。
- **投影应用** `_apply_consolidate` / `_apply_consolidate_page`：只认**事件里的 output**
  （正文引用 / 证据 / 时间 / 来源），**不重算**合成算法（重算会让 replay 变成"再猜
  一次"，事件说 A、投影写 B）；重复页幂等；同作用域同层的 candidate/active 旧页
  `_supersede(seq)` 退役（行保留，旧正文与它当时站的 evidence 仍可 `get`）；正文过
  `redact_or_block` 闸门（命中就拒，坏页不入库）。**重复页幂等**（同 id + 同 layer +
  同 `content_ref` 且状态是 candidate/active 直接返回）；**已 confirm 的 active 页不因合成
  退役**（`memory.consolidate` 是模型可见、免审批的工具，能退役人确认过的页面就等于模型
  可以撤销人的召回授权；人确认的页只有人能退，新旧页并存由 freshness 决定次序——复审提出
  的设计边界）；**正文回退**撞回已退役的页 id → fail-closed 报错（不静默停在 superseded，
  也不悄悄复活）。
- **工具面**：`memory.consolidate`（`1.0` / `ACTION_CREATE` / `exposed=True` / args 仅
  `{scope, scope_id}`，`additionalProperties=False`）→ `memory_exec._consolidate` →
  `consolidatable_sources(scope)`（只取 `layer='l1'`、active、未擦除、未过期、正文在盘
  上；读不到正文的行跳过——不可用的证据不进页）。**workspace 无条件拒**（与 propose 同
  款：派发层 `DispatchDenied` + 执行侧同名纵深防御，复用 `c4.memory_scope_denied`，
  **不新造规则号**）；task 作用域只能给自己；create 分支在
  `permissions._classify_memory` 里跳过 kind 检查（合成页的 kind 不是模型给的）。
  `test_veto_scope.py` 的模型可见工具封闭集同步加一名（那条绊线只许改，不许删）。
  结果 `candidate` + `origin=inferred`，人 `confirm` 后才进召回（免审批只管
  `memory.confirm`，合成不白送召回权）。
- **新鲜度与排序**：`_stale_map` 按 `(scope, scope_id)` 取
  `MAX(last_seq) WHERE layer='l1' AND status='active' AND erased_at IS NULL
  AND (expires_at IS NULL OR expires_at > now)`（过期 L1 不会被合成 / 召回使用，拿它判
  陈旧是误报），`> basis_seq` ⇒ 陈旧。**`stale` 是三态**：`True` 陈旧 / `False` 已确认
  新鲜 / `None` 不可判定（旧行没有 `seq:`；主循环输出里**省略**该字段——把"判不了"写成
  `false` 就是谎报新鲜）。排序键唯一实现在 `_order_by_priority(..., stale=)`，被
  `search()` 与 `retrieve()` 的**三条路径**（词面 / 无索引 / 索引不可用 / 融合）共用：
  同一作用域优先级组里陈旧页排到新鲜记忆之后（降权，不删、不短路，符合 F10 §8）。
  **复审修复**：无索引分支原先只排序、不带 stale，默认生产路径上陈旧页照样排第一——
  现在三处同键；`test_retrieve_demotes_stale_pages_even_without_a_vector_index` 用
  "页与 L1 同 origin / 同 created_at"的夹具把 origin 权重的掩盖去掉。
- **replay**：`test_consolidate_output_is_replayable` 从事件重建，页正文 / 证据 /
  `consolidated_at` 与实时写入逐项一致。
- **测试**：`tests/test_memory_distill.py`（新，**21 条**：阈值钉住、引用拼装、两次逐
  字节相同、改一条只动自己那块、proof/evidence 机械一致 + 坏页被就位校验拦、坏页应用
  参数化（未知 layer / `l1` / 空 `source_refs`）、重复应用幂等、正文回退 fail-closed、
  active 页不被合成退役、L3 预算 / 只收人给的偏好 / "预算已满"标记、L3 只 user、单条不出
  L2、旧页被 supersede 仍可 get（含 evidence / `source_refs` 快照）、replay 一致、
  freshness stale + 降序、**无索引路径也降权**、**无 `seq:` 时 `stale=None`**、**过期 L1 不算更新证据**）；
  `tests/test_memory_dispatch.py` **+4 条真派发 e2e**（consolidate 真在
  `tool_manifest()` 并写出候选页、workspace 拒、task 只能本任务、确认后的页进召回并在
  新 L1 之后变 stale，改走 `retrieve()` 并同时校验 `search()` 的相对顺序）；
  `tests/test_memory_store.py`：迁移断言改 `schema_version == 3`、更高版本 fail-closed 用
  `'4'`，并**补了真正的 v2→v3 用例**（`test_schema_v2_database_gains_the_layer_column_in_place`：
  删 `layer`、版本置 2、重开 → 旧行一律 l1、可重复执行；原 v1 用例从没删过 `layer`，
  证明不了这条）。
- **负控（都真跑过，逐条按"改坏什么 → 哪条红"记）**：① 每块掺入全页面字节数（破增量
  delta）→ 逐字节用例红；② `_stale_map` 直接 `return {}` → stale 用例红；③ 派发层去掉
  consolidate 的 workspace 无条件拒 → e2e 红；④ L3 预算因子 ×100 → 预算用例红；⑤ L3
  去掉 user 限定 → 只 user 用例红；⑥ 就位校验改 `if False` → 坏页用例红；⑦ 去掉
  `retrieve()` 无索引分支的 stale 排序键 → 无索引降权用例红（复审抓到的真洞）。**如实记**：
  执行侧 workspace 守护**经派发路径不可达**（派发层先拒），与 `memory.propose` 同款
  纵深防御，**没有测试能单独打红它**——不声称有覆盖。
- **偏差 / 没做到**：
  1. `_SCHEMA_VERSION` **这次**从 2 升到 3（与 N2 的"派生表不参与版本"不同）：`layer`
     是 `memories` 的形状契约，旧库必须能识别；迁移只加 NOT NULL DEFAULT 列。
  2. 规格 §4.2 #5 原写"比较 `consolidated_at` 与 L1 最大 `last_seq`"，实现改成把
     `seq:<basis_seq>` 写进 `source_refs` 再比：`consolidated_at` 是钟表时间，系统时间
     回拨会把"陈旧"判成"新鲜"，**而且 `memory.confirm` 这条生命周期推进会把该列改成
     确认时刻**（L1 行也如此）——晚确认的页就会永远"新鲜"；投影里的 `last_seq` 是单调
     逻辑时钟。旧行没有 `seq:` 时 `stale=None`（**不可判定**，主循环输出省略该字段），
     不标陈旧、也不写成"新鲜"。
  3. LLM 合成未做（本轮只有确定性合成）；**没有 L2/L3 vs L1 的检索质量评测**——没人
     证明"读页比直接读 L1 更好用"，README / 评测里不许那么写。
  4. L3 只有 user 作用域；workspace / task 的偏好页按设计不做。
  5. 跨作用域合成、对话抽取、**冲突消解**都没做：两条矛盾的 L1 在页里并列，不裁决。
  6. `consolidate` 不进审批表（`ACTION_CREATE`）：将来若要做"合成后自动 confirm"必须
     另立契约，否则等于绕开"人确认才召回"。
  7. **退役语义**：新页只把同层的 **candidate** 旧页置 superseded；active 页不动（复审
     指出的设计边界，见上）。代价是"页会越攒越多"（每个不同的正文各一页），人用
     forget / erase 收；没有自动回收。
  8. **页 id 回退冲突**：正文回退到旧版会撞回已退役的 id，此时 fail-closed 报错而不是
     自动复活——要多一条"人工决定"的路径。

---

---

## 5. N4 · 忘记 vs 物理擦除（契约先行，边界写死）

### 5.1 现状

- `memory._apply_forget` 做的是 `_drop_fts` + `UPDATE revision / status / fts_rowid=NULL /
  last_seq`；**正文 blob 不删**（`memory.py` 里没有任何 `unlink` / `os.remove` / `rmtree`）。
- 事件日志里的**读**事件保留正文快照（F10 §3.1 的实测结论），append-only 不可删。
- UI/CLI 上的"忘记"与"擦除"目前没有区分。

### 5.2 设计

1. 新增 `memory.erase`（控制面 only，`ACTION_DELETE` + Receipt）：
   - **引用计数必须先做**：`content_ref` 是 `sha256` 内容寻址，不同记忆（不同作用域 /
     kind）可能共享同一个 blob。删除前统计**所有投影行**（含 `forgotten` / `superseded`）
     对该 `content_ref` 的引用；只有计数为 0 才物理删文件，否则保留文件、只把本行标
     `erased_at` + `content_state=unavailable`。不做计数 = 擦一条让另一条正文凭空消失，
     那是数据损坏，不是擦除。
   - 删除该记忆的正文 blob（`blob_dir` 下按 `content_ref` 定位）。**实际顺序（N4 落地
     后修正，原规格写反了）**：**先删文件 → 再让结果与事件同事务提交**。崩在两者之间
     只会留下"文件没了、行还没标擦除"——重建时正文如实不可用；反过来（先标擦除、
     文件还在）才是隐私泄漏。删文件失败则整次擦除如实失败（不写 `erased_at`）。
     记忆没有目录租约。**并发保护（N4 复审后修正）**：引用计数 + 删文件必须与**提交**在同一把
     跨进程执行守卫（`_begin_memory_execution` 的 `execution_guard`）内——只靠 revision 前置与
     SQLite 写事务**不够**：两条共享同一 blob 的记忆被两个进程同时擦时，双方都会看到"对方
     还没擦"→ 都保留文件 → 两行都标擦除、孤儿文件永久留存。回归：
     `test_erase_commit_runs_inside_the_execution_guard`。
   - 投影行置 `forgotten` + `erased_at`，`content_state=unavailable`；
   - **重建后不得复活**：`apply_event` 见到 `erased_at` 的记忆 → 跳过 blob 读取、
     标 `content_state=unavailable`；blob 不存在不视为错误。
   - Receipt 列出"删了哪些 `content_ref` / 字节数 / 哪些被谁引用而未删"，供独立核对。
2. **契约必须写清的一句话**："物理擦除 = 删除正文副本与检索索引；事件日志中的读快照
   保留。"彻底清日志需要日志压缩/重写，与红线 1 冲突，单开契约工单，本轮**不做**，
   不许在 UI 上写"已彻底删除"。
3. UI/CLI 文案分级："忘记（不再召回，正文仍在本机，可恢复）" vs "擦除（删除正文副本与
   索引；历史回放会显示证据不可用）"，两句话都必须在确认页出现。

### 5.3 判据

- **测试落点（N4 实际）**：没有单开 `test_memory_erasure.py`，而是追加在既有两个记忆测试文件里
  （复用它们的 env 夹具，避免复制装配代码）：`test_memory_dispatch.py` 的
  `test_erase_deletes_unique_blob_and_marks_erased` / `test_erase_keeps_shared_blob_and_reports_retained_by` /
  `test_erase_is_control_plane_only`，`test_memory_loop.py` 的
  `test_cli_erase_deletes_content_and_marks_erased`，以及 `test_memory_store.py` 的
  `test_schema_v1_database_is_migrated_in_place`。判据：erase 后 blob 不存在、`search` 不召回、
  `get` 返回 `content_available: False`（不是 `content_unavailable`，字段名以代码为准）、
  `rebuild` 后仍不可召回。负控实际做了两条：无视引用计数 → 共享与删除用例红；
  去掉 control-only 判定 → 只控制面用例红。另加守卫回归（提交必须在守卫内）。**未做**：
  "只改 status 不删 blob"的负控、"另一进程正 get 时 erase"的并发用例（用守卫回归替代）。
- 负控：只改 status 不删 blob → rebuild 后能读 → 用例必须红。
- 跨进程：另一进程正 `get` 时 erase → 要么成功要么明确失败，**不允许**读到半截正文。
- 不做：备份/快照里的副本、跨库引用追踪。**库内引用计数必须做**（见 §5.2 第 1 条）——
  "不做共享 blob 引用追踪"曾经写在这里，是错的：同一 `content_ref` 真的可能被多条投影行
  共享（`put_content` 按 sha256 写单文件），不计数就会擦坏别的记忆。

---

## 6. N5 · 崩溃恢复的记忆分支 + 审计表脱敏收尾

### 6.1 现状

- `recovery.recover_run` 没有记忆分支（`grep -rin memory src/tianshu/recovery.py` 零命中），
  恢复按文件写工具的推断走（多数 `none_applied`，快照缺失时 `unknown` 并可能阻断目录）。
- 投影侧本身安全：**实时写路径**上 `apply_event` 只在 `_commit_result` 的事务里被调用
  （`rebuild()` 是重建路径，走同一入口但用途不同，不能混为一谈）；已有测试
  `test_mid_write_failure_is_unknown_and_leaves_no_half_projection` 钉住前者。
- `Dispatcher._audit_args` 只在 `_reject` 里用；**被采纳后**又被闸门挡掉的请求仍把正文
  留在 `dispatch_requests.args_payload`（F10 §13 #26 的"未修的一半"）。

### 6.2 设计

1. **恢复判据（可证明，不猜）**：记忆写入的投影更新与 `tool.result` **同事务**。所以
   "有 intent 无 result" ⇔ **投影/事件层一定没生效**。恢复时对
   `state ∈ {intent, executing}` 且 tool ∈ `MEMORY_TOOL_IDS` 的行：标 `rejected`、
   reason=`recovery.memory_not_applied`，**不写 unknown、不阻断目录、不进 `applied`
   快照语义**（记忆没有 preimage），恢复报告单列一段"记忆写入未生效 N 条"。
   **⚠ 这条早期结论只覆盖投影/事件层**：`memory.erase` 的删文件在提交之外且不可逆、
   内容型写在提交之前就落 blob——两类都要单独判（见 §6.4 的三层判据；两处冲突以
   §6.4 与代码为准）。
2. **审计表**：请求进入**终态**后，把 `args_payload` 的记忆正文替换为
   `content_sha256` + `content_bytes`，置 `args_redacted=1`；`args_hash` 保持原值。
   执行期"参数哈希核对"只发生在终态之前；实现时必须先用探针证明终态后**没有任何
   代码路径**从 payload 重算 hash（对账 / 报表 / 二次派发各查一遍），再落这条。
   正文的可回溯性由 blob + `tool.result` 保证，不依赖审计表。
3. 恢复报告里，"记忆未生效"与"文件写 unknown"分栏呈现，不合并三态（红线 5/11）。

### 6.3 判据

- `tests/test_memory_recovery.py`（新）：crashpoint 在 `memory.propose` 的 intent→result
  之间 `os._exit` → 重启 → `recover` 报告"未生效"、库中无该记忆行、目录**未被阻断**、
  重新 propose 成功；crashpoint 在 result 之后 → 恢复不重复写、不重复消费审批。
- 审计：三种终态（成功 / 闸门拒绝 / 超时失败）各一条，断言审计表里 `args_payload`
  不含正文原文（`assert "密钥片段" not in payload`），且 blob 仍能按 `content_ref` 读出
  成功那一条。
- 负控：把恢复判据改回 `unknown` → "目录不被阻断"用例必须红；把终态脱敏去掉 → 审计
  用例必须红。
- 两个真进程：一个进程 propose 中崩溃、另一个进程恢复 → 只出现一条"未生效"，无半条投影。

### 6.4 落地记录（N5 实际，2026-11-11；含独立复审整改）

- 分支 `fix/memory-recovery-audit`（起点 `main @ 42d786b` = N4 合入后），合入后 #21 / #26 在
  F10 §13 里都改成 ✅（复审后又改了一轮，见下）。
- **恢复分支（三层判据，不能一句话概括）**：
  - **投影/事件层**：`recover_run` 在"纯工具 / 文件写"分流**之前**先判
    `tool_id in MEMORY_TOOL_IDS`，结算 `failed/none_applied` + `memory_not_applied`；
    `assess_run` 同样归 `none_applied` 且**不** `pending_writes += 1`。快照清单损坏的早退
    路径也先判记忆写（复审 中-A：原来的早退块把记忆写当写工具 → unknown + 阻断目录）。
  - **正文 blob 层**：内容型记忆写在提交**之前**就把正文写进 `memory_blobs/`，崩溃会留下没有
    行引用的孤儿副本（复审 高-A）。恢复按 `tool.intent.args.content_sha256` 找 blob，
    **引用计数为 0 才删**，删掉的进 `RecoveryReport.memory_orphan_blobs`；共享 blob 绝不误删。
    残留：PII 红act 过的正文引用与原文哈希不同，定位不到 → 不删（`blob_dir` 只留一份，且内容
    已红act），如实记在这里。
  - **`memory.erase` 例外（复审 高-B）**：先删文件、后提交，删文件在事务外且不可逆。恢复按
    memory_id 查投影：**文件还在** ⇒ 确实没生效（`none_applied`）；**文件已没** ⇒ 只能
    `unknown` + `memory_unresolved`（不进 `report.unresolved`——那个列表末尾会被拿去阻断目录，
    记忆没有目录可挡），`ok=False`。`_settle` 同时把 `dispatches` 行推进到 `error`
    （复审 轻-A），不让审计继续显示"仍在执行"。
- **审计表**：`dispatch_requests` 加 `args_redacted` 列（`_ensure_column` 原地补列）；终态
  （`done` / `rejected`，含 `_fail_unknown` 那条）由 `_set_request_state()` →
  `_redact_terminal_args()` 脱敏；**升级前就在终态的行**由 `_backfill_redacted_args()` 在每次
  打开库时补扫（复审 中-C）。`ON CONFLICT` 取"最后一次写入"的形态（复审 中-D：单向保留会让
  "终态后重新发起"的合法请求被判"参数被改"）；`_reject` 的 `args_redacted` 改成"真的脱敏了
  才标 1"（复审 中-E：被拒的文件请求曾被标成已脱敏）。
- **事件日志（复审 高-C，属于 #26 的同一目标）**：`_intent_args` 原来只对
  `memory.propose` 拿掉 `content`，N1 新增的 `memory.remember` / `memory.edit` 把正文原样写进了
  **append-only、不可删** 的 `tool.intent`。改成"记忆资源 + 参数里有 content ⇒ 换成
  `content_sha256` / `content_bytes` / `content_omitted`"，不列工具名。
- **测试落点（与 §6.3 的偏差，如实记）**：没有新建 `tests/test_memory_recovery.py`——判据本来
  就在 `tests/test_memory_dispatch.py`（同一套 `Env` 夹具），单开文件要复制夹具。实际新增
  **26 条**，都在该文件：三条终态脱敏（成功 / 闸门 / 中途炸）、`waiting` 行被否决后（ON
  CONFLICT）、终态后重新发起、旧库终态行补扫、被拒文件请求不标脱敏、intent 不存正文
  （remember + edit）、断电恢复、孤儿 blob、共享 blob 不误删、擦除崩溃窗口、清单损坏仍结算、
  文件写不脱敏、CLI `recover` 打印两个新栏位、恢复使用装配方给的投影/blob_dir、补扫中断重试、标记与列脱钩重扫、`memory` 与 `blob_dir` 的优先级。
- **负控（17 条逐条真跑过、都变红）**：① 关终态脱敏；② ON CONFLICT 改回单向 CASE；③ 删
  `_recover_run` 记忆分支；④ 删 `assess_run` 记忆分支；⑤ `_reject` 无条件标脱敏；⑥ 跳过补扫；
  ⑦ `_intent_args` 只认 propose；⑧ 把 erase 当普通记忆写；⑨ 关掉孤儿清理；⑩ 删清单损坏早退块
  里的记忆分支；⑪ CLI 不打印 `memory_unresolved`；⑫ 恢复忽略传入的投影与 `blob_dir`。
  另 5 条：标记提前置位 / 标记在但列被删强制重扫 / 加列与补扫之间崩溃重试 /`memory` 与 `blob_dir` 优先级 /
  `recover_run` 忽略 `blob_dir`。独立复审另复跑了 ③⑧⑨ 与标记/列边界，结论一致。
- **残留（别读成"任何时刻都不存原文 / 一定没生效"）**：
  1. 停在 `waiting` / `admitted` 的行仍是完整参数——`dispatch()` 正是从这张表取参数执行，脱敏会
     把派发打红。要收口得先定义"等待行过期 / 取消即作废"，单开工单。
  2. 升级前终态行若 `payload` 已不可解析，**不补扫**（保留可审计性，`args_redacted` 保持 0）。
     `_redact_terminal_args` 解析失败也是静默返回——已知 fail-open，人工核查前不得声称"全部脱敏"。
  3. PII 红act 的孤儿 blob 定位不到（见上）。
  4. §6.3 要求的"两个真进程断电"没做：现有恢复测试都是同进程构造状态（`monkeypatch` 掉
     `_commit_result` 制造"副作用已发生、提交前硬退"），真跨进程断电不在 CI 可复现范围。
  5. `blob_dir` 里的正文副本不随审计表脱敏——那是设计（正文本来就存 blob，审计表只收重复副本）。
  6. 模型给出的工具调用参数（含模型自己提议的 propose / edit 正文）仍在 `model.result` 与
     `run_turns` 里：replay 要它重建模型输入，物理擦除范围见 F10 §10。
  7. 自定义 `blob_dir` 已支持（`recover_run(memory=...)`，CLI/daemon 都传）；只读库调用方若不给
     `memory` 也不给 `blob_dir`，恢复仍按 DB 旁默认目录找 blob——这是 `_open_memory_projection`
     注释里写明的契约限制。
  8. 补扫用 `dispatch_meta` 的**持久标记**：标记与补扫同一事务，中途失败/被 kill 时下次打开会
     重试（`test_interrupted_backfill_is_retried_on_the_next_open`）；**成功之后**不再回扫，旧库里
     payload 修好后不会自动补。
  9. `assess_run` 对擦除一律报 unknown / 拒接管（只读对账里无法安全判“文件删了没有”），擦除
     崩溃后要先 `recover` 再接管。

## 7. N6 · 控制面 UI / Host：看得见、改得动（F10 §9）

### 7.1 现状

`host.py` 零记忆端点；`cli._cmd_memory` 是唯一控制面。UI 只能看任务与审批，看不到
"Agent 记住了什么"。

### 7.2 设计

1. IPC 新增 `memory.list` / `memory.get` / `memory.remember` / `memory.edit` /
   `memory.forget` / `memory.promote`，全部要求**本地人身份**（与 CLI 同一条
   `HumanIdentity.local_cli()` 路径），**不经模型**。
2. 每条记忆显示的六项不可省：`scope / origin / revision / status / source_refs /
   evidence.proof_count`——缺一项，人就无法判断"是谁的、凭什么、改过没有、还算不算数"。
3. 正文以**纯文本**渲染（不执行远端 HTML，不解析 markdown 里的链接为可点击外链）。
4. 所有写操作走 dispatcher（intent/result + 一次性审批 + 执行侧二次核验），UI 里
   不新开"直接改投影"的捷径。

### 7.3 判据

- `tests/test_host_memory.py`（新）：真 IPC 连接 + 本地人身份 → list/get 正常；无身份
  → 拒绝；edit/forget 走 dispatcher（断言事件里有 intent/result）；越权作用域 →
  `c4.memory_scope_denied`。
- **负控**：把 UI 端点改成直接调 `projection` 改状态（不经 dispatcher）→ 用例必须红。
- 明确不测：真实模型驱动 UI（沿用现状"未接过真实模型"的写法，不许写成已接）。

---

## 8. 合并顺序与总验收（lead 负责）

1. **契约 PR**（文档：PRD §4.1 / §5.2.2 / §5.4.2 + F10 §5 / §7 / §9 / §10）→ 合入 `main`。
   **代码注册不在这里**：`tools.SPECS` / `rules.RULE_IDS` / `schemas/` 随各实现 PR 与产生
   路径同落地（见 §0.2）。契约 PR 不合则后续 PR 不开工。
2. 实现顺序：**N1 → N4 → N5 → N2 → N3 → N6**。**已按序落地**：N1（#83/#84）、N4（#85）、
   N5（#89）、N2（#93）、**N3（#98）**；**只剩 N6（控制面 UI / Host 记忆端点）**。
3. 每个 PR 合入前：lead 在最新 `main` 上跑全量 + `ruff` + `uv lock --check`；冲突一律
   lead 解。工具面绊线（`test_veto_scope.py`）随工具变化同步登记。
4. 全部合入后 lead 收尾：
   - `docs/F10-memory-design.md` §13 十条逐条改成实际状态（有测试函数名才写 ✅）、
     §14 判据加新测试名；
   - `docs/memory-residuals-evidence.md`（新）：每条的正控/负控输出摘要；
   - `README.md`（记忆段落与测试数）、`docs/KNOWLEDGE.md`（§2 状态 / §4 决策 / §5 教训 /
     §7 限制 / §8 会话日志）；
   - 数字以收尾轮实测为准，**Windows 实机保持"未验证"**。
5. 总验收（人可复现的最小演示）：
   - 记住一条偏好 → 新任务召回；改一次 → 旧版可查、新版召回；
   - 忘记 → 不再召回；擦除 → 正文副本消失、回放显示证据不可用；
   - 提升到 workspace → 另一任务在本 workspace 内能召回、在别的 workspace 不能；
   - 逐条能对上"哪个测试、哪次负控"。

---

## 9. 明确不做（这一轮）

- **日志压缩/重写式"彻底擦除"**：与红线 1 冲突，单开契约工单（N4 只做到"删副本 + 删索引"）。
- **LLM 蒸馏 / 跨作用域合成 / 全量对话抽取**：N3 只做确定性合成；LLM 版本要另立契约与
  评测。
- **实体检索、时序检索、reranker 默认启用**：F10 §12 已列"须评测证明增益"，不在本轮。
- **多用户 / 多租户 / ACL**（D2）、**云记忆托管**（D7）、**外部 embedding 默认开启**。
- **Windows 实机**：无机器，不碰、不写成已验证；只有 CI `windows-latest` 的结论。
- 真实模型驱动 UI（现状未接，本轮不接）。

---

## 10. 本文不宣称的事

- 本文**没有**变更任何现行契约：C2/C4/PRD/`schemas/` 的实际改动在 §1 的契约 PR 里。
- §2–§7 的判据**已全部落地并由测试钉住**（N1/N2/N3/N4/N5）；逐条对账在
  `docs/F10-memory-design.md` §14 与各工作流的"落地记录"（§2.3 / §3.4 / §4.4 / §5.4 / §6.4）。
  **N6 仍未实施**，它的判据（§7.3）目前一条测试都没有——别把本文的设计读成已实现。
- §2–§7 的设计是**待实施**方案，不是"已完成"；实施后每条都要能指到测试函数名与负控
  输出，否则按未修处理。