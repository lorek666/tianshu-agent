# 资源种类与外发许可（PRD §5.2.3 的「带种类的资源」）

- 立据：`docs/PRD-v1.md` §5.2.3；施工规格
  [`SPEC-G1-CLOSURE-2026-11-11.md`](SPEC-G1-CLOSURE-2026-11-11.md) §2。
- 状态：**资源的解析/规范化与许可判定已实现**（`resources.py` / `permissions.py` /
  `dispatcher.py` / `rules.py`）。**执行器**：`file` / `memory` 早已有；**网络**在
  G2 网络阶段补上（`network.fetch`，见 [network-g2.md](network-g2.md)）；**委派**在
  G2 子代理 D1 补上（`subtask.spawn` / `subtask.status` / `subtask.result` /
  `subtask.cancel`，见本文 §8）。**MCP 仍然没有执行器**，在派发前拒绝
  （`dispatch.resource_kind_unsupported`）。
- 判据：`tests/test_g1d_resource_kinds.py`（15 条，其中"哪些种类有执行器"一条随 G2
  网络阶段更新）；负控 6 项实测变红（见 §6）。

## 1 为什么需要"种类"

`ActionRequest.resources` 原来是 `tuple[str, ...]`，派发层从头到尾按**绝对路径**理解它。
记忆上线时用 `memory://` 前缀解决了"没有路径的资源"，但显式种类只做到两种。PRD §5.2.3
要求把**文件、网络目标、MCP 工具、记忆作用域、子任务**都当作一等资源："各自规范化与
校验"，且"URL 不伪装成文件路径，无 workspace 的研究任务合法"。

## 2 五种资源的形状

`RESOURCE_KINDS = {file, memory, network, mcp, subtask}`，**封闭集**：不在里面的种类一律拒绝。

| 种类 | 形状 | 规范化 |
|---|---|---|
| `file` | 裸绝对路径（**没有** scheme） | 历史不变：`resources` 里每一条绝对路径逐字节照旧，`digest` / `action_digest` 不受影响 |
| `memory` | `memory://<scope>/<scope_id>` | 既有（见 `resources.py` 模块注释） |
| `network` | `network://<transport>/<host>[:port]/<path>[?query]` | host 小写 + IDNA(punycode)；默认端口（80/443）省略；path 折叠 `.`/`..` 且不许逃出根；百分号编码的段**先解码再判**；query 原样；拒绝 userinfo（`@`）与 fragment（`#`） |
| `mcp` | `mcp://<server>/<tool>` | 两段都限定 `[a-z0-9_.-]{1,64}`，不许多段/空段 |
| `subtask` | `subtask://<task_id>` | task_id 走既有的 `_OPAQUE_ID` 形状 |

`transport` 只有 `http` / `https` 两种：多一种传输就多一套规范化规则，而 v1 不需要。

**`https://x/y` 仍然是非法资源**：它既不是绝对路径、又不是本项目的任何 scheme，`parse_resource()`
照旧拒绝。网络资源用**自己的 scheme** 明确表达，而不是放宽 http(s) 的解析——放宽等于让同一个
目标有两种写法。

### 2.1 规范化为什么这么严

每一种折叠都对应一个"看起来只是写法不同、实际是两个目标"的口子：

| 规则 | 不这么做会怎样 |
|---|---|
| host 小写 + IDNA | `ExAmPle.COM` / `example.com` / `xn--…` 三种写法指向同一台机器，却会得到三张互不匹配的批准 |
| 默认端口省略 | 同一目标两种摘要 → 旧批准无故失效 |
| `.`/`..` 折叠 | `/a/../b` 与 `/b` 是同一路径 |
| 百分号编码先解码再判 | `%2e%2e` 解码后是 `..`，不解码就折叠不掉——等于给路径穿越留了个"看起来规范"的入口；解码后含 `/`、`\`、NUL 的段直接拒绝 |
| 拒绝 userinfo | 批准的是主机，不是"看起来像主机"的一整串凭据 |
| 拒绝 fragment | fragment 不发给服务器，却会进摘要，制造"同一请求两种摘要" |

`ResourceRef` 只接受**已规范化**的形状：传一个"合法但非规范"的值会抛 `ResourceError`
（非规范写法必须经过 `parse_resource()` 归一后再进 `resources`）。

## 3 许可判定（`PolicyEngine.classify`）

动作集合扩了两个：`fetch`（外发）与 `delegate`（委派）。**没有**把外发塞进 `read`：
PRD 明写"网络 GET / 搜索不是无成本、无外发的本地纯读"，塞进读就等于说"读一下没关系"。

| 资源种类 | 允许动作 | 判定 | rule_id |
|---|---|---|---|
| `file` | read/create/overwrite/delete/exec | 不变 | 不变 |
| `memory` | 既有 | 不变 | 不变 |
| `network` | **只能** `fetch` | 任务级白名单（`outbound_allow`）**先判**，然后**恒需人工批准**（无自动路径） | `c4.outbound_not_allowed` / `c4.outbound` |
| `mcp` | **只能** `exec` | 恒需人工批准（MCP 工具是不可信外部程序） | `c4.mcp_call` |
| `subtask` | `delegate`（spawn / cancel）与 `read`（status / result） | **按工具**：spawn 需人工批准、cancel 自动放行（只减少执行）、read 自动；父任务能力声明/深度/限额/预算在派发层判 | `c4.subtask_spawn` / `c4.subtask_cancel` / `c4.auto` |

> **D1 起 `c4.subtask_unavailable` 退役为"保留未使用"**：它回答的是 D1 之前
> "委派没有执行器"的拒绝。编号保留给历史日志，但不再有产生路径（`rules.DECLARED_UNUSED_RULE_IDS`）。

一次动作**只能有一种种类**（`c4.mixed_resource_kinds`）；种类与动作不匹配是
`c4.action_resource_mismatch`（例如文件资源配 `fetch`）。多目标同种类沿用既有
`c4.multi_resource`。

## 4 执行侧：没有执行器的种类不会被执行

`dispatcher.resolve_scheme_resource()` 是"资源种类"在执行侧的唯一落点：

- 有执行器的种类：`file`、`memory`、**`network`**（G2 网络阶段）、**`subtask`**
  （G2 子代理 D1）；
- 其余种类（`mcp`）在**派发前**拒绝：`dispatch.resource_kind_unsupported`，
  消息写明是哪种资源、指向哪个 URI。

网络的"有执行器"不等于"自动放行"：它多一道**任务级白名单**（`outbound_allow`）,
之后仍要逐次人工批准（见 [network-g2.md](network-g2.md) §1）。

为什么不是"走到审批再失败"：审批是给人做决定的，为一个**注定执行不了**的能力弹批准，
等于给用户一个"批了就能用"的错觉。反过来，许可判定（§3）仍然完整实现并有判据——它描述的
是"将来有执行器时，外发必须人工批准"这条**语义**，不冒充"现在就能发"。

## 5 摘要绑定

`ActionRequest.to_canonical_dict()` 里 `resources` 排序后参与摘要，所以：

- 同一个目标的非规范写法 → 归一后摘要**相同**（同一张批准可用）；
- 换 host / 换 path → 摘要**不同**（旧批准失效）。

`tests/test_g1d_resource_kinds.py` 用 `ApprovalStore.find_for()` 直接验证这两条。

## 6 负控（回退即红，实测 6/6）

| 回退 | 变红的测试 |
|---|---|
| `network` 判定改成 AUTO（自动放行） | `test_outbound_always_needs_approval` |
| 混合种类放行 | `test_mixed_resource_kinds_are_denied` |
| 文件资源配 `fetch` 落到 `c4.unhandled`/放行 | `test_network_action_must_be_fetch` |
| 没有执行器的种类按 file 兜底 | `test_kinds_without_an_executor_are_refused_before_dispatch`（现在只覆盖 mcp / subtask） |
| 规范化不折叠 `..` / 不拒 userinfo | `test_network_target_rejects_confusing_forms` |
| `subtask` 判定改成动作级 AUTO/APPROVE（不区分工具） | `test_subtask_decision_depends_on_tool_and_action` |

## 7 没做 / 不成立的部分

| 缺口 | 现状 |
|---|---|
| 真实网络请求 | **已实现**（G2 网络阶段）：`network.fetch` 抓取、任务级白名单、逐次批准、内容引用与字节账本。见 [network-g2.md](network-g2.md)；但**只支持 GET、没有检索 API、真实公网研究未验证** |
| MCP 客户端 | **未做**（G2 后续阶段）。仍在派发前拒绝 |
| 子代理 | **D1 已实现**（合同 + 实体 + 四个工具 + 限额 + 共享预算 + 取消请求）；**D2（Worker 执行、父挂起/唤醒、取消传播、fence、结果回填）未做**，所以子任务只排队不执行。见 §8 |
| 外发字节计量（`bytes` 维度） | 已接：发送前按上限预占、读完后按实际字节结算（判据 `test_bytes_budget_is_enforced_before_sending`） |
| 任务级外发白名单（`outbound_allow`） | **已实现**：`task.created` payload 契约增量 + 执行侧门（见 [network-g2.md](network-g2.md) §2） |
| 内容分级 / 哪些内容不许外发 | 未做：没有外发表，无法定义"私有内容"的边界 |
| 子任务执行与预算父子账户 | 账本侧 D1 已接：spawn 在父账户 `subtasks` 维度真实预占 1、子账户 `run:<child_run_id>` 的 `parent_id` 指向根 Run 账户（局部上限 `tokens`/`requests`、deadline 固定 300s）；**收尾结算在 D2**（D1 不结算、不退款） |

## 8 G2 子代理 D1：委派资源有了执行器（只排队，不执行）

- 判据与范围见 `docs/SPEC-G2-DELEGATION-2026-11-11.md` §2.1–§2.4 / §3 D1；
  测试在 `tests/test_g2_delegation_{contract,spawn,control,recovery}.py`。
- 资源形状不变：`subtask://<slug>`，slug 走既有 `_OPAQUE_ID`
  （`[A-Za-z0-9_.:-]{1,128}`）。**没有新增解析器**。
- 四个工具：`subtask.spawn`（`delegate`，需审批）、`subtask.status` /
  `subtask.result`（`read`，自动）、`subtask.cancel`（`delegate`，自动）。
- 委派**不是自动放行**：`_authority_subtask()` 在弹批准之前判六件事——工具归属、
  任务级 `max_subtasks > 0`、父任务 `depth == 0`、slug 在父 Run 内唯一、
  数量与局部上限、目标存在（status/result/cancel）。
- D1 的诚实边界（逐条见规格 §5）：子 Run 停在 `created`、子作业停在 `pending`、
  子任务停在 `queued`；`subtask.cancel` 只落 `cancelling`（真正停下在 D2）；
  子任务只能做纯读，且没有任何收尾事件（`subtask.finished` 由 D2 写）。
