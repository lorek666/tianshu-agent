# G2 记忆阶段施工规格（2026-11-12）

> 目标：把 PRD §11.1「G2 完整用户体验」里**最后一项未做**——**记忆阶段**——做成
> 可验收的增量。网络（#110 / #112）、MCP（#116–#119）、子代理 D1/D2/D3（#120 / #121 / #123）
> 已交付。本规格只说**记忆这一段**；MCP / 子代理不再改。
>
> 本文件是**施工规格，不是完成记录**。下文的判据、数字与测试名在实现 PR 里落地，
> 实测数字回填到 §6 与交付对账文档。计划条数写"计划"，实测与计划不一致时按实测
> 改计划并说明原因（先例：网络阶段估 14/22/10、实测 18/23/11）。

---

## 0 这一段要回答的两个用户问题

G2 的验收口径是"同一任务集证明新增收益 + 可查看"。记忆阶段必须回答：

1. **可查看**：用户能不能看见"系统记住了我什么"、凭什么、还算不算数，并亲手
   确认 / 忘记 / 删除？（现在**不能**：`ui/` 里一条记忆相关代码都没有，
   控制面也没有检索端点。）
2. **可证明收益**：记忆到底让任务做得更好吗？（现在**没有证据**：真实用户任务集
   8 条里没有一条用到记忆；跨任务召回只有单元测试，不在任务集分母里。）

两问答不出来，就不算交付——只把工具挂上（2026-11-10 的 N1/N2/N3/N4 已做完）
不等于用户能看见、也不等于收益被证明。

---

## 1 现状（2026-11-12 按代码读出，不是按计划写）

**已经有的**（不要重复实现）：

| 能力 | 位置 | 状态 |
|---|---|---|
| 记忆投影（表 + FTS + 正文内容寻址 + rebuild） | `memory.py` `MemoryProjection` | ✅ |
| 7 个模型可见工具（`search`/`get`/`propose`/`confirm`/`forget`/`edit`/`consolidate`）+ 3 个控制面专属（`remember`/`promote`/`erase`） | `tools.py` `SPECS` 记忆段 | ✅ |
| 派发链路（`memory://` 作用域、审批、Receipt、执行侧二次核验） | `dispatcher.py` / `memory_exec.py` | ✅ |
| 主循环召回（零目录也能检索、`model.intent.payload.context.memories` 落身份） | `loop.py` `_exec_memory_read` / `context_payload` | ✅ |
| 控制面 CLI（`tianshu memory list\|search\|get\|confirm\|forget\|remember\|edit\|promote\|erase`） | `cli.py` / `memory_control.py` | ✅ |
| 控制面 RPC **8 个**：`memory.list` / `get` / `remember` / `confirm` / `edit` / `forget` / `promote` / `erase` | `control.py` `ControlPlane._methods` | ✅ |
| 擦除 / 崩溃恢复 / 引用计数 / 闸门（PII·密钥） | `memory.py` / `recovery.py` | ✅ |

**缺口**（本阶段要补的三件事）：

1. **控制面没有 `memory.search`**：`ControlPlane._methods` 里只有 `memory.list`
   （按作用域列、无查询）与 `memory.get`（按 id 取）。人的检索入口只有 CLI
   （`cli.py` 直接调投影），**UI 无法检索**，RPC 层也不能。
2. **UI 没有记忆面板**：`ui/src` 全文没有 `memory` / `记忆` 字样（2026-11-12
   `grep -rln` 实测 0 命中）。G2 的"可查看"在记忆上不成立。
3. **真实用户任务集不测记忆**：`REAL_USER_CASES` 8 条（本地 2 / 受控 3 / 联网 2 /
   委派 1），**没有一条召回记忆**；跨任务召回只在 `test_memory_loop.py` 的单元用例里。
   顺带，评测 runner 现在**一个 case 只跑一个 Run**（`_run_standard`），
   而"记住 → 新任务召回"天然是**两次 Run**（同一记忆库、不同 task/workspace）。

> **并行施工风险**：记忆的 N1–N6 轮由其他会话完成，目前相关 worktree
> （`ts-wt-memory` / `ts-wt-hostmem` / `ts-wt-counts` / `ts-wt-distill` / `ts-wt-retrieval`）
> 都停在已合入的提交上、无未提交改动、无 open PR（2026-11-12 `gh pr list` 实测）。
> 本阶段从 `main @ 5d4cb67` 起分支；若中途有人动 `memory*.py` / `ui/`，先 rebase 再继续。

---

## 2 增量 M1：可查看（`memory.search` RPC + UI 记忆面板）

### 2.1 控制面 RPC `memory.search`

**为什么必须走投影的 `retrieve()` 而不是 `search()`**：库里的 `search()` 明确是
FTS-only（注释写明"不要偷偷变 hybrid"），而能力标注（`semantic` / `degraded` /
`fused`）只在 `retrieve()` 上。人在 UI 上检索时**必须**看到"这次有没有语义路",
否则空结果会被误读成"没有这条记忆"。

**请求**

```json
{ "method": "memory.search",
  "params": { "query": "报告用中文",
              "scope": "user",            // 可选：user | task | workspace
              "scope_id": "…",            // 可选：task id / workspace 绝对路径
              "limit": 5 } }              // 可选：正整数，执行侧夹到 ≤ MAX_RECALL_ITEMS
```

> **没有 `status` 参数**（与模型侧 `memory.search` 一致）：检索只返回**可召回集合**
> （`active`、未过期、正文可读）。要看候选 / 已忘 / 已擦除用 `memory.list` 的
> `status` 过滤——"检索"与"盘点"是两个问题，混在一个参数里会让空结果的含义变模糊。

**响应**（`ok: true`）

```json
{ "items": [ <memory_view + score + score_kind + stale> ],
  "semantic": false, "degraded": "未配置向量 provider", "fused": false,
  "query": "报告用中文",
  "scopes": ["memory://user/local"] }
```

- 条目形状**复用** `memory_control.memory_view(...)`（六项判据 + `content_ref`）+
  `score` / `score_kind` / `stale`（与模型侧 `_memory_item` 同口径，避免"人和模型
  看到的排序解释不一样"）。
- 正文**带**：这是检索不是列表（列表才刻意不带正文）。带多少由投影的
  `MAX_RECALL_BYTES` 裁剪，`content` 可能短于投影正文——调用方看 `content_ref`
  就能知道原文在哪。
- `scopes` 回显**这次实际生效的可见作用域**：空结果时人才能分清"没有这条记忆"
  与"作用域没覆盖到"。
- `semantic` / `degraded` / `fused` 原样给出（`RecallOutcome` 的字段），**不许**
  写成一句 `found: false`。

**错误面**（`ControlError`，与既有 8 个方法同一套）

| 情况 | code |
|---|---|
| `query` 缺失 / 空串 / 非字符串 | `bad_params` |
| `limit` 不是正整数 / 超过 `MAX_RECALL_ITEMS` | `bad_params`（超出上限按上限夹，不报错——与模型侧一致） |
| 传了 `status`（不支持） | 忽略该键，不报错（与 `additionalProperties` 口径不同：RPC 不是模型工具，宽容读参不会放宽权限） |
| `scope` 未知 / `scope_id` 与 scope 不匹配 | `bad_params` |
| 未声明任何可见作用域 | `bad_params`（人这条路上不该发生；发生就是装配错） |

**权限口径（照抄 F10 §13 #30，不要新造）**：`memory.search` 是**人机界面入口**，
与模型工具面是两条路；它挡不住同 uid 进程，只挡跨本地用户。这一点在 UI 面板与
文档里都要写成事实，不写成"模型无法检索记忆"。

### 2.2 UI 记忆面板

`ui/src` 现在是三栏壳（任务列表 / 运行详情 / 时间线）+ 模态框。记忆面板加在
**顶部工具条的一个"记忆"按钮**打开的模态（与 `newTaskOpen` 同款），不改三栏结构：

- **列表**：`memory.list`（默认 user 作用域，可切 task / workspace 并手填 `scope_id`）。
  每行显示六项判据里人需要看的：`status`（候选/生效/已忘/已擦除）、`scope`、
  `origin`（谁说的）、`revision`、`source_refs` 条数、`proof_count`，以及正文摘要
  （截断，点击才 `memory.get`）。**纯文本**渲染（不 `dangerouslySetInnerHTML`）。
- **检索**：输入框 + `memory.search`；结果列表带 `score` / `score_kind` / `stale`
  的**人话标注**（"排序=来源权重" / "排序=融合分数" / "这页可能过时，看 L1 证据"）；
  顶部条显示 `semantic` / `degraded`（"这次只有词面召回：未配置向量 provider"）。
  空结果时同时显示 `scopes`——人能区分"没这条"与"没覆盖到"。
- **动作**：确认（`memory.confirm`，带当前 `revision`）、忘记（`memory.forget`）、
  编辑（`memory.edit`）、删除（`memory.erase`，二次确认弹窗写明"会删正文副本，
  事件日志里的读快照仍保留"）、记住一条（`memory.remember`，人自己写）。
  每个动作后**重新拉列表**，不做乐观更新（记忆是有 revision 的，猜状态会骗人）。
- **状态**：空态、加载态、`conflict`（revision 过期 → 提示"刷新后重试"，不自动重试）、
  `not_found`、daemon 不可达（复用既有 banner）。**降级**：`memory.search` 不可用时
  面板显示"检索不可用（原因）"，不静默退化成 `list`。

### 2.3 M1 判据与负控

判据（每条都能被一条测试打红）：

| # | 判据 | 打红它的测试 |
|---|---|---|
| M1-a | `memory.search` 真注册、真走 `retrieve()`（响应里 `semantic`/`degraded`/`fused` 与投影一致） | `test_memory_search_rpc_returns_capability_labels`（真 daemon + 真 IPC） |
| M1-b | 作用域过滤生效：未授权作用域的命中**不出现**，`scopes` 回显与实际一致 | `test_memory_search_rpc_is_scope_filtered` |
| M1-c | 空结果与"能力降级"不同形状（`items: []` + `degraded` 非空 ≠ 报错） | `test_memory_search_rpc_empty_is_not_an_error` |
| M1-d | 参数错误面（query 空、limit 非法、scope 未知）都是 `bad_params`，不 500 | `test_memory_search_rpc_param_errors` |
| M1-e | UI 面板能列、能检索、能确认/忘记，且**纯文本**渲染正文（XSS 载荷不进 DOM） | `ui` 里的组件测试（jsdom）+ 截图 |
| M1-f | `conflict`（revision 过期）明确提示、不自动重试 | UI 组件测试 |

负控（回退必红，实测记录进 PR body）：

| # | 回退 | 期望 |
|---|---|---|
| NC-M1 | `memory.search` 改成调 `search()`（FTS-only） | M1-a 红（`semantic`/`degraded` 变成空/False） |
| NC-M2 | 作用域过滤去掉（`allowed=()` 或全放） | M1-b 红 |
| NC-M3 | 条目改成只给 `{memory_id}` | M1-a/M1-e 红（六项判据缺失） |
| NC-M4 | UI 用 `dangerouslySetInnerHTML` 渲染正文 | M1-e 红（XSS 载荷进 DOM） |

---

## 3 增量 M2：可证明收益（真实用户任务 `RU-M1@1.0.0` 跨任务召回）

### 3.1 任务怎么设计（一个真实用户场景，不是"测 API"）

> 用户第一句话："以后写周报都用中文、先给结论。这次先记下来。"
> 第二件任务（新 Run、新 workspace、**同一用户**）："把这两份会议纪要汇总成周报。"

预期：第二个任务**不需要用户再重复偏好**——系统从 `user` 作用域召回"周报用中文、
先给结论"，把它用在产物上（`model.intent.payload.context.memories` 里有这条；
产物结构是"先结论后细节"）。

反例（必须能判红）：

- 召回被剪掉（记忆库空）→ 产物退化成没有偏好的默认格式；
- 作用域串了（另一个 workspace 的私有记忆被召回）→ 安全项红；
- `memory.forget` 之后的新 Run 仍召回 → 红；
- 越过授权作用域（例如召回 workspace 私有记忆）→ 红。

### 3.2 评测器要补的能力：一个 case 跑**两次 Run**（`Case.followup`）

现在 `_run_standard` = 一次 `engine.start` + 审批/park 循环 + 一次 `run.end`。
新增可选字段（默认 `None` = 行为完全不变）：

```python
@dataclass(frozen=True)
class FollowupRun:
    task: str                 # 第二个任务的文本
    out_name: str
    script: tuple[ModelResponse, ...]
    checks: tuple[Check, ...]
    workspace: str = "ws2"    # 与 Run 1 不同的目录（同 data root）
```

`Case.followup: FollowupRun | None`。执行规则（写死在 runner 里）：

1. 两次 Run **共享** `directory / "data"`（同一个事件库 / 记忆投影 / 用户作用域），
   workspace 必须**不同**（否则"新任务"是假的）；
2. Run 1 结束（含审批循环）后**不重建** stack，直接在同一 stack 上
   `create_task` + `start` 跑 Run 2；
3. 两次 Run 的 `Observation` **不合并**：Run 1 的产物/事件仍记在 `Observation`，
   Run 2 的结果另存 `Observation.followup: FollowupObservation | None`
   （含 `run_id` / `events` / `artifact_text` / `memories` / `checks` 输入）；
4. 任一 Run 出错 → 样本 `error` 写明是哪一段（"Run 1 未结束" / "Run 2 派发失败"），
   **不静默**、不把两段拼成一个通过；
5. 回放（`replay`）要能重放**两次** Run（Run 2 的 replay 校验它自己的产物与记忆快照）。

### 3.3 `RU-M1@1.0.0` 的夹具、脚本、判据

- **夹具（Run 1）**：无文件；用户说"以后写周报都用中文、先给结论"。
- **脚本（Run 1）**：模型 `memory.propose`（kind=preference，正文"周报用中文、
  先给结论"）→ 人批准 `memory.confirm`（脚本模型下由评测器的审批替身批准，与既有
  受控修改 case 同一套；`origin` 因此升为 `user_confirmed`）。
- **夹具（Run 2）**：新 workspace，`资料/会议纪要 A.md`、`资料/会议纪要 B.md`。
- **脚本（Run 2）**：`memory.search("周报 偏好")` → 读到偏好 → `read_text` 两份纪要 →
  `create_report` 写 `weekly.md`（第一行"结论：…"，正文中文）。
- **判据（Run 1）**：`memory.proposal_created`（`memory.propose` 真的成功、候选/生效
  状态正确）、`memory.confirmed_by_human`（`origin=user_confirmed`、审批一次性消费、
  Receipt 可核对）、`approval.requested(1, exact=True)`。
- **判据（Run 2，完成度）**：`memory.recalled_from_user_scope`（`memory.search` 成功
  且命中 Run 1 的 `memory_id`；`model.intent.context.memories` 里有该 id）、
  `artifact.starts_with_conclusion`（产物首段是结论）、`artifact_mentions("中文")`。
- **判据（Run 2，安全）**：`memory.scope_not_leaked`（Run 2 的召回结果里**没有**
  其它 workspace 的私有记忆）、`outputs.authorized_only("weekly.md")`、
  `memory.no_unapproved_write`（Run 2 没有产生未经批准的 `active` 记忆）。
- **判据（after-forget 变体，测试内任务）**：`RU-M1-F`（不进公开分母）——
  Run 1 记住 → 人 `memory.forget` → Run 2 召回为空、产物**不含**偏好；
  防"忘记之后还在用"。

### 3.4 M2 负控（回退必红）

| # | 回退 | 期望 |
|---|---|---|
| NC-M5 | Run 1 的记忆写入被跳过（`memory.propose` 不落库） | `memory.recalled_from_user_scope` 红 |
| NC-M6 | Run 2 不召回（`memory.search` 返回空 / 从脚本里去掉） | 产物偏好判据红 |
| NC-M7 | 召回不按作用域过滤（塞一条别的 workspace 私有记忆） | `memory.scope_not_leaked` 红 |
| NC-M8 | `followup` 复用 Run 1 的 workspace | "新任务"守卫红（runner 直接报错） |
| NC-M9 | forget 之后仍召回（把 tombstone 当 active） | `RU-M1-F` 红 |

---

## 4 口径变更（旧 → 新 → 理由）

| # | 旧口径 | 新口径 | 理由 / 影响 |
|---|---|---|---|
| 1 | 控制面 RPC **8 个**记忆方法 | **9 个**（+`memory.search`） | UI 要检索；模型侧 `memory.search` 早已存在，缺的只是人的入口 |
| 2 | UI 无记忆入口（三栏壳） | 顶部工具条 + 记忆模态（列表/检索/确认/忘记/编辑/删除/记住） | G2「可查看」在记忆上成立 |
| 3 | `REAL_USER_CASES` **8** 条 / 24 次样本 | **9** 条 / 27 次（+`RU-M1@1.0.0`） | G2 要求"同一任务集证明新增收益" |
| 4 | 一个 case 一次 Run | 可选 `followup` 第二次 Run（默认关闭） | "记住 → 新任务召回"天然两段；默认关闭所以既有 8 条 case 行为不变 |
| 5 | `fixture_digest` 只算单 Run 输入 | 非默认时把 `followup` 的 task/out_name/script/checks/workspace 也算进 digest | 改第二段脚本必须让这条 case 不可比（红线：输入变了不能"看起来还是同一条"） |
| 6 | `Observation` 只有一次 Run 的产物/事件 | 新增 `followup`（默认 `None`） | 两段证据不能混成一条链（与 D3 的 `subtask_events` 同一条纪律） |

`SUITE_VERSION` **不动**（`m4.6`）：任务版本独立于 suite 版本（先例：D3 加 `RU-D1@1.0.0` 时
`SUITE_VERSION` 未动）。改 `followup` 字段只会改 `RU-M1` 自己的 digest。

---

## 5 诚实边界（交付后仍然不成立 / 未验证）

1. **脚本模型，不是真实模型分数**：Run 1/Run 2 都是 `ScriptedModel`；"偏好被用上"
   是脚本写的，判据证明"召回 → 上下文 → 产物"这条链路，不证明真实模型会照偏好写。
2. **没有真实 embedding**：`memory.search` 的能力标注在本机永远是
   `semantic=false` + 明确 `degraded` 原因（`HashEmbeddingProvider` 无语义能力）。
   UI 上必须原样显示，**不许**写成"语义检索已启用"。
3. **不证明"召回质量"**：`docs/memory-retrieval-eval.md` 的离线指标（FTS MRR 0.864 →
   hybrid 0.955）是另一套 22 query 夹具，**不是**用户任务收益；本阶段的收益证据是
   "一个真实场景里偏好被用上"，不能外推成"记忆让所有任务更好"。
4. **UI 只在 Chromium + 本机 daemon 上验**：Windows 命名管道 ACL 未加固且未验证；
   UI 面板不覆盖多用户、不覆盖 token 轮换。
5. **控制面端点不是授权边界**：见 §2.1 权限口径；同 uid 的 `run_command` 子进程
   可达（F2/exec 边界，不因本阶段改变）。
6. **`followup` 只支持**"同一 data root、两次串行 Run"：不支持并发、不支持两次之间
   重启 daemon（记忆库跨进程仍在，但评测器不重启）、不支持三段以上。
7. **擦除/遗忘的物理边界不变**：`memory.erase` 删正文副本与索引；事件日志里的读快照
   保留（F10 §10）。UI 的删除确认文案必须写清这一点，别承诺"物理抹除"。
8. **多子任务 × 记忆**（子任务里读记忆）不在本阶段：子任务能力仍是 D1/D2 的只读边界，
   记忆读在子任务里**没有新增评测**。

---

## 6 交付清单（实现 PR 合入后回填实测）

| # | 内容 | PR | 状态 |
|---|---|---|---|
| 1 | 本施工规格 | 本 PR | 待合入 |
| 2 | M1：`memory.search` RPC + UI 记忆面板 + 判据/负控 | 待开 | 未开始 |
| 3 | M2：`Case.followup` + `RU-M1@1.0.0` + `RU-M1-F` + 判据/负控 | 待开 | 未开始 |
| 4 | M3：文档对账（`real-user-tasks` / `PRD-v1 §10.5+§11.1` / `KNOWLEDGE` / `README` / 交付对账）+ 干净 main 实测数字 | 待开 | 未开始 |

每步的验收命令：

```bash
uv run --frozen pytest -o addopts="" -q         # 全量
uv run --frozen pytest -o addopts="" -q tests/test_host_memory.py tests/test_memory_loop.py
uv run --frozen ruff check .
cd ui && pnpm test && pnpm build                # UI（若本机有 pnpm）
uv run --frozen python -m tianshu evaluate --repeat 3
```
