# 资源种类与外发许可（PRD §5.2.3 的「带种类的资源」）

- 立据：`docs/PRD-v1.md` §5.2.3；施工规格
  [`SPEC-G1-CLOSURE-2026-11-11.md`](SPEC-G1-CLOSURE-2026-11-11.md) §2。
- 状态：**资源的解析/规范化与许可判定已实现**（`resources.py` / `permissions.py` /
  `dispatcher.py` / `rules.py`）；**执行器未实现**——网络、MCP、委派三种能力本身属 G2，
  今天它们在派发前被明确拒绝（`dispatch.resource_kind_unsupported`）。
- 判据：`tests/test_g1d_resource_kinds.py`（15 条）；负控 6 项实测变红（见 §6）。

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
| `network` | **只能** `fetch` | **恒需人工批准**（无自动路径） | `c4.outbound` |
| `mcp` | **只能** `exec` | 恒需人工批准（MCP 工具是不可信外部程序） | `c4.mcp_call` |
| `subtask` | **只能** `delegate` | **恒拒**（没有委派执行器） | `c4.subtask_unavailable` |

一次动作**只能有一种种类**（`c4.mixed_resource_kinds`）；种类与动作不匹配是
`c4.action_resource_mismatch`（例如文件资源配 `fetch`）。多目标同种类沿用既有
`c4.multi_resource`。

## 4 执行侧：没有执行器的种类不会被执行

`dispatcher.resolve_scheme_resource()` 是"资源种类"在执行侧的唯一落点：

- 有执行器的种类：`file`、`memory`；
- 其余种类在**派发前**拒绝：`dispatch.resource_kind_unsupported`，消息写明是哪种资源、
  指向哪个 URI。

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
| 没有执行器的种类按 file 兜底 | `test_kinds_without_an_executor_are_refused_before_dispatch` |
| 规范化不折叠 `..` / 不拒 userinfo | `test_network_target_rejects_confusing_forms` |
| `subtask` 判定改成 AUTO/APPROVE | `test_subtask_is_denied_because_the_capability_does_not_exist` |

## 7 没做 / 不成立的部分

| 缺口 | 现状 |
|---|---|
| 真实网络请求、MCP 客户端、子代理 | **未做**（G2）。所以「私有内容未经外发许可不发包」仍只能记为 **unknown**：闸门已就位、可拒绝，但没有外发面可验证 |
| 外发字节计量（`bytes` 维度） | 账本有该维度，但没有真实外发数据 |
| 任务级外发白名单（`outbound_allow`） | 未做：现在外发只有"逐次人工批准"这一条路，没有"这个任务允许访问这几个域名"的声明（那需要改 `task.created` payload 契约，属 G2） |
| 内容分级 / 哪些内容不许外发 | 未做：没有外发表，无法定义"私有内容"的边界 |
| 子任务执行与预算父子账户 | 未做：`subtasks` 维度与父子链在账本里，但没有委派执行器 |
