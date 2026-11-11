# G1 收口的施工规格（SPEC-G1-CLOSURE-2026-11-11）

- 立据：`docs/PRD-v1.md` §5.2.3（通用工具准入）、§5.6（Limits 与预算账本）、§11.1 G1；
  上一轮交付对账 [`PRD-REVIEW-DELIVERY-2026-11-11.md`](PRD-REVIEW-DELIVERY-2026-11-11.md) §4
  把 G1 剩下的五条逐条登记为**不成立**，本规格是把其中三条拆成可验收工作流的文件。
- 基线：`main @ 5297965`（PR #94 之后）。落笔时实测 **1601 passed / 7 skipped**、
  `ruff check .` 干净、运行时依赖仍为零（`pyproject.toml` 的 `dependencies = []`）。
- 判定口径（沿用 `docs/SPEC-PRD-REVIEW-2026-11-11.md` §0）：**有回归测试、且做过负控
  （把修复回退后测试会红）才算做完**。**要求写进 PRD ≠ 已实现**；判定不了 = unknown + 阻断。
- 范围：只做「不需要网络 / MCP / 子代理真实能力、不需要新依赖、不需要 Windows 实机」的部分：
  - **G1c**：共享预算账本**默认打开**（记账模式）+ 硬上限模式的**可验证上界**；
  - **G1d**：资源种类 `network` / `mcp` / `subtask` 的解析、规范化与外发许可门；
  - **G1e**：大结果的**有界摘要 + 内容引用 + 分页读取**（读取验证任务可见范围）。
- 本轮**不做**（逐条登记，见 §5）：真实网络 / MCP / 委派执行器（G2）、界面上的「已知费用 vs
  未知用量」展示（UI 轮）、§7.1 语义摘要、G3 平台实机、真实模型复跑。
- 交付状态：**G1c 由 PR #96 交付**；G1d、G1e 依次排队（合入后由对账 PR 把实测数字写回
  交付文档与 [KNOWLEDGE.md](KNOWLEDGE.md)）。

## 0 施工纪律

1. 只在自己的 worktree 里操作，禁止在主检出 `/Volumes/Date/天枢Agent` 跑 `git` 或改文件。
   本规格的 worktree 是 `/Volumes/Date/ts-wt-g1c`，分支 `feat/g1c-default-on`（后续
   `feat/g1d-resource-kinds`、`feat/g1e-content-refs`，各自从最新 `main` 切出）。
2. **不加第三方运行时依赖**；不抬 Python 版本；不改 `.github/workflows` 的必过检查。
3. 一提交一件事：`feat:` / `test:` / `docs:` 分开，提交必须有正文（改了什么、为什么、判据、负控摘要）。
4. **不许改判据让自己变绿**：既有断言只能因为「新要求改变了语义」而改，且必须在 PR body
   写明旧口径、新口径与理由。
5. 收工前：`PYTHONPATH=<worktree>/src /Volumes/Date/天枢Agent/.venv/bin/python -m pytest
   -o addopts="" -q` 全绿；`/Volumes/Date/天枢Agent/.venv/bin/ruff check .` 干净。
6. 干不完就如实报：完成了什么、卡在哪、哪些宣称仍然不成立。

## 1 G1c：账本默认打开 + 硬上限的可验证上界

### 1.1 语义变更（旧口径 → 新口径）

`Limits.budget_limits` 的两种取值含义变了，这是**唯一**需要写进 PR body 的既有语义改动：

| 取值 | 旧口径（PR #92 交付时） | 新口径（本轮） |
|---|---|---|
| `None`（默认） | 整个账本不启用，一次预占都不写 | **记账模式**：账户照建、每次 attempt 照预占、照结算，但**任何维度都不设闸**（不拒发） |
| 显式 dict | 该维度设闸 | 不变（该维度设闸；未列出的维度只记账不设闸） |

为什么把默认从「关闭」改成「记账」：

- PRD §5.6 第 4 条要求「尝试过但全失败不得显示没有模型调用」，第 7 条要求「恢复先核对未决
  预占」。账本关着时这两条**在默认路径上完全不成立**——恢复流程即便想看未决预占，库里也
  没有。只有记账常开，这两条才有事实可核。
- 记账模式**不改变**「发不发请求」的判定（没有维度设闸 ⇒ `reserve()` 永不因额度拒绝），
  所以它不是估算预占带来的行为变更；估算预占会让临界 run 提前被拒这件事，只在显式给出
  上限（或硬上限模式）时发生，那时用户已经显式要了一个上限。
- 明确不承诺：记账模式不产生「用户可见的费用页面」（UI 未做），也不让 `max_total_tokens`
  的事后计数消失。

### 1.2 硬上限模式与可验证上界

新增 `Limits.budget_bound: str = "estimate"`，取值封闭集 `{"estimate", "upper"}`：

- `estimate`（默认）：预占用 `budget.estimate_need()`（4 字符 ≈ 1 token + 25% 余量 + 输出
  预留）。**它是估算，不是上界**（PRD §5.6 第 3 条 / §5.7）；结算超过预占时如实记
  `overrun`，但**不**停机——估算本来就会偏，拿它当上界才是错的。
- `upper`：预占用 `budget.upper_need()`，两项都必须可验证：
  - **输入上界** = 实际请求体的 UTF-8 字节数（`json.dumps(messages, ensure_ascii=True)`）。
    依据：字节级 BPE 下每个 token 至少覆盖 1 个字节 ⇒ `tokens ≤ bytes`。ASCII 转义把非 ASCII
    字符展开成 `\uXXXX`（6 字节/字符），所以 CJK 的上界远大于估算值，方向是**保守**的。
  - **输出上界** = adapter 自己声明的输出上限（`ModelCapabilities.output_tokens` /
    `max_tokens`）。**拿不到就拒绝进入 `upper` 模式**（`BudgetConfigError`，在第一次请求前
    抛出并记 `model.error`），不用 `output_reserve_tokens` 的 256 默认值冒充上界。
  - 两者相加 = 一次 attempt 的可验证上界；`reserve()` 用它与上限比较。
- 越界（`upper` 模式）：结算值 > 该预占的 need ⇒ 账户标记 `overrun`，此后 `reserve()` 一律
  `BudgetRefused`（reason = 上界被突破），并在 `usage()` / `reconcile()` 里可见。
- 不承诺的话：provider 若在计费口径上加固定的「每消息开销」，字节上界不覆盖那几个 token；
  这一点写在 [budget-ledger.md](budget-ledger.md) 里，靠 `overrun` 暴露，不靠嘴保证。

### 1.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/tianshu/budget.py` | `upper_bound_tokens(text)`；`upper_need(...)`；`budget_accounts.overrun` 列（`_ensure_columns` 迁移）；`settle()` 记 overrun；`reserve()` 在 `overrun` 时拒绝；`usage()` / `reconcile()` 暴露 `overrun_count` |
| `src/tianshu/errors.py` | 新增 `BudgetConfigError`（配置层面的拒绝：进不了硬上限模式） |
| `src/tianshu/loop.py` | `Limits.budget_bound`；`_ensure_budget_account` 默认建账户（记账模式）；`_reserve_attempt` 按 `budget_bound` 选估算/上界；`upper` 模式下取 adapter 输出上限，拿不到 → `model.error` 且不调用模型 |
| `tests/test_budget_ledger.py` | 旧「账本关闭 = 不预占」用例改为「记账模式 = 预占但不拒发」；新增记账模式、上界模式、overrun、缺失输出上限的用例 |
| `docs/budget-ledger.md` | 默认记账与硬上限两节；`estimate ≠ 上界` 的边界；旧/新口径 |

### 1.4 负控（把修复回退后必须变红）

| # | 回退 | 期望 |
|---|---|---|
| NC1 | `budget_limits is None` 时仍然直接 return（回到「账本默认关闭」） | 记账模式用例红 |
| NC2 | `upper` 模式仍用 `estimate_need()` | CJK 上界用例红（估算远小于字节上界） |
| NC3 | adapter 无输出上限时继续用 256 默认值 | 「进不了 hard 模式」用例红 |
| NC4 | `settle()` 把超过预占的用量裁到 need | overrun 用例红 |
| NC5 | `upper` 模式 openrun 后不阻断后续 `reserve()` | 「上界被突破后拒发」用例红 |
| NC6 | 记账模式给维度偷偷设闸（例如默认 tokens 上限） | 「记账模式不拒发临界 run」用例红 |

## 2 G1d：资源种类与外发许可

### 2.1 三种新资源的规范化形状

`resources.py` 的 `RESOURCE_KINDS` 从 `{file, memory}` 扩到 `{file, memory, network, mcp, subtask}`，
判定顺序保持「绝对路径优先 ⇒ 认得的 scheme ⇒ 不认得的 scheme 拒绝 ⇒ 相对路径拒绝」：

| 种类 | 形状 | 规范化规则 |
|---|---|---|
| `network` | `network://<transport>/<host>[:port]/<path>[?<query>]` | `transport ∈ {http, https}`；host 小写 + IDNA 归一；默认端口（80/443）省略；path 必须以 `/` 开头、折叠 `.`/`..` 且不许逃出根；**拒绝** userinfo（`@`）、fragment（`#`）、百分号编码的 host；query 原样保留 |
| `mcp` | `mcp://<server>/<tool>` | 两段都要匹配 `[a-z0-9_.-]{1,64}`；多段/空段/尾斜杠一律拒 |
| `subtask` | `subtask://<task_id>` | `task_id` 走既有的 `_OPAQUE_ID` 形状 |

`ResourceRef.uri` 对这三种返回**规范化后**的字符串（回写进 `resources`，因此审批摘要是对规范
形状算的）。file 仍是裸绝对路径，memory 仍是 `memory://<scope>/<id>`，两者**逐字节不变**。

### 2.2 许可判定（`PolicyEngine.classify`）

先按种类分流，**一次请求只能有一种种类**（混合 ⇒ `DECISION_DENY` + `c4.mixed_resource_kinds`），
不再"含 `://` 就当记忆"：

| 种类 | 允许的动作 | 判定 |
|---|---|---|
| `file` | 既有 | 不变 |
| `memory` | 既有 | 不变 |
| `network` | 新增 `ACTION_FETCH = "fetch"` | **恒 `DECISION_APPROVE`**（`c4.outbound`）：外发没有自动路径，必须人工批准；动作不是 `fetch` ⇒ `DENY` + `c4.action_resource_mismatch` |
| `mcp` | 既有 `ACTION_EXEC` | `DECISION_APPROVE`（`c4.mcp_call`）：MCP 工具是不可信外部程序，与 `exec` 同级 |
| `subtask` | 新增 `ACTION_DELEGATE = "delegate"` | **恒 `DENY`**（`c4.subtask_unavailable`）：没有委派执行器，能力缺失要 fail-closed |

`SUPPORTED_ACTIONS` 增加 `fetch` / `delegate`；`AUTO_ACTIONS` 不加（外发与委派都不自动放行），
`APPROVAL_ACTIONS` 加 `fetch`（`delegate` 不进，因为它恒拒）。

### 2.3 执行侧

- `dispatcher` 只把 `kind == file` 的请求交给文件执行器、`kind == memory` 交给记忆执行器；
  其它种类（及其执行器未实现这件事）在**派发之前**拒绝 `dispatch.resource_kind_unsupported`，
  绝不回退成「当成文件路径试试」。
- 既有 `_write_target` / `_list_files` / `_read_text` 只接受 file：收到 `network://...` 报
  `ResourceError`（G1a 的 `reject_uri_path` 已经在做，本轮补上「解析出种类后按种类拒绝」）。
- 预算：`network` 的 `bytes` 维度留给 G2 的真实请求使用；本轮只登记维度已存在，不写「已计量」。

### 2.4 负控

| # | 回退 | 期望 |
|---|---|---|
| NC7 | `network` 资源走 AUTO（不加人工批准） | 「外发必须 APPROVE」用例红 |
| NC8 | 混合种类请求放行 | `c4.mixed_resource_kinds` 用例红 |
| NC9 | 文件执行器接受 `network://`（回退成路径） | 「网络资源不被文件执行器接收」用例红 |
| NC10 | 未实现执行器的种类按 file 兜底 | `dispatch.resource_kind_unsupported` 用例红 |
| NC11 | `network` 规范化不折叠 `..` / 不拒 userinfo | 两条规范化用例红 |
| NC12 | `subtask` 返回 AUTO 或 APPROVE | 「委派能力缺失必须拒」用例红 |

## 3 G1e：有界摘要 + 内容引用 + 分页读取

### 3.1 现状与缺口

`context.py` 已经把单条 > 8 KiB 的工具输出换成占位并把原文写进内容寻址 blob
（`loop._persist_spills`），但占位只告诉模型「已落盘」，**没有可用的引用**，也没有读取工具，
更没有「谁能读」的判定。于是「大结果有界」成立一半，「内容引用 + 分页读取并验证任务可见范围」
完全没做（PRD §2.3 也明确了这一点：工具自己截断的内容不会因 spills 被恢复）。

### 3.2 设计

新增 `src/tianshu/contents.py`（建在事件库同一 SQLite 文件）：

- `content_objects(digest PK, size, created_at)`：内容寻址对象清单；
- `content_grants(digest, task_id, run_id, created_at, PK(digest, task_id))`：**可见范围**，
  spill 发生时由主循环按当前 run 的 task 写入（同一内容被多个任务引用就多行，不串号）；
- `record(digest, data, *, task_id, run_id)`：幂等（同 digest 不重复写对象行，只加授权）；
- `read(ref, *, task_id, offset, length)`：形状校验 ⇒ 授权校验 ⇒ 读 blob ⇒ **sha256 重算比对**
  ⇒ 返回 `{data, offset, next_offset, total_size, digest}`。任一失败抛明确错误
  （`ContentNotVisible` / `ContentMissing` / `ContentDigestMismatch` / `ContentRefError`）。

占位文本改为**可操作**的：保留头尾预览 + `ref: content://sha256:<digest>` + `total_bytes`
+ 一句「用 read_content 分页读取」。模型据此可以真正取回原文。

新工具 `read_content`（`exposed=True`）：

```
{"ref": string(pattern ^content://sha256:[0-9a-f]{64}$),
 "offset": integer(minimum 0),
 "length": integer(minimum 1, maximum 65536)}
required: ["ref"]
```

- 在 `dispatcher` 里作为**资源种类 `content`** 走统一准入：`ActionRequest.resources =
  (content://sha256:<digest>,)`，`action = "read"`；派发层算出 `preconditions["content_visible"]`
  （当前 task 有授权）后由 `PolicyEngine` 判：可见 ⇒ `AUTO`（`c4.content_scope`），不可见 ⇒
  `DENY`（`c4.content_not_visible`）。工具执行时**再核一次**（双闸，不靠单点）。
- `RESOURCE_KINDS` 因此再增一种 `content`（G1d 之后），形状 `content://sha256:<64hex>`。
- 分页是有界的：`length` 上限 64 KiB，返回体不会因引用大而失控；`next_offset` 让模型自己续读。

### 3.3 不做 / 不承诺

- 不做「工具级 output 子契约」（C2 后续），也不改 `tool.result.payload` 的 schema：spill 是
  **上下文层**的行为，事件里仍保留工具返回的原文（> 256 KiB 落 ref 是既有规则）。
- `read_text` 自己截断的内容**仍然**取不回来（它根本没被落盘）——不加「透明恢复」的假象。
- 不做 blob 的保留期 / 垃圾回收（属备份与保留期轮，已登记）。

### 3.4 负控

| # | 回退 | 期望 |
|---|---|---|
| NC13 | 占位里不给 `ref`（回到「已落盘」文案） | 占位可用性用例红 |
| NC14 | `read()` 不校验授权（任何 digest 都能读） | 跨任务读取用例红 |
| NC15 | `read()` 不重算 sha256 | 篡改 blob 用例红 |
| NC16 | 策略层不看 `content_visible`（恒 AUTO） | 不可见拒绝用例红 |
| NC17 | 工具执行侧不再复核授权（单闸） | 双闸用例红 |
| NC18 | `read()` 不按 `offset/length` 切片 | 分页用例红 |

## 4 合并顺序与收工

1. G1c → PR → 合入 `main`（记账默认打开是行为变更，PR body 必须写旧/新口径与不承诺项）。
2. G1d → PR → 合入 `main`（`docs(contract):` 单独提交说明 `SUPPORTED_ACTIONS` 新增两个动作）。
3. G1e → PR → 合入 `main`（`docs(contract):` 单独提交说明 `read_content` 工具与 `content` 资源种类）。
4. 对账 PR：全量测试 + `ruff`，把实测数字、负控结果、Windows 实测写进
   [PRD-REVIEW-DELIVERY-2026-11-11.md](PRD-REVIEW-DELIVERY-2026-11-11.md)、
   [PRD-v1.md](PRD-v1.md) 相应章节的实现状态、[KNOWLEDGE.md](KNOWLEDGE.md) 与 README 计数。

每个 PR 合入后 `main` 的 required checks 会作废其它在途分支的 required checks（保护规则是
strict），所以三个 PR **串行**推进：前一个合入、拉最新 `main`、再开下一个。

## 5 本轮明确不做（不许写成完成）

- 真实网络请求 / MCP 客户端 / 子代理：G2。本轮只交付**准入面**（种类、规范化、外发许可、
  执行器拒绝兜底），因此「私有内容未经外发许可不发包」仍只能记为 **unknown**——没有外发面，
  无法证明也没有违反；**不许**写成「已通过」。
- 界面分别显示已知费用与未知用量：UI 轮。
- 「工具级 output 子契约」、`oneOf`/`anyOf`/`$ref`/`format` 支持、`run_command` 隔离门、
  §7.1 模型语义摘要、G3 平台实机、真实模型复跑：都仍未做。
- Windows job 仍是既有 5 failed 族（并非 required check）；本轮不声称修好。
