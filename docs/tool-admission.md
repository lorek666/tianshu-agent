# 通用工具准入（PRD §5.2.3 的落地与边界）

- 立据：`docs/PRD-v1.md` §5.2.3；施工规格 `docs/SPEC-G1-ADMISSION-2026-11-11.md` §2。
- 状态：**已实现的部分**见 §2–§7；**未实现的部分**逐条登记在 §8，不写进「已完成」。
- 判据：`tests/test_g1_admission.py`（19 条），每条都在
  `docs/SPEC-G1-ADMISSION-2026-11-11.md` §4 有对应的负控（回退修复即红，实测 9/9）。

## 1 为什么要有这一层

在接入网络 / MCP / 子代理之前，工具调用必须先走**同一套**注册、参数校验、任务状态、
权限、预算与留痕路径。否则每接一种新执行器，就复制一份"只在自己那条路上成立"的
校验——而模型、审批人和用户看到的却是同一份工具清单。

这一层解决的是**契约诚实性**，不是新增能力：

| 旧状态（本轮之前） | 后果 |
|---|---|
| 执行侧只校验顶层 `type / required / additionalProperties` | `SPECS` 里写着的 `items` / `minItems` / `minimum` / `maximum` **从未被执行**：契约里写了，执行侧不看 |
| 注册期零校验 | schema 里写 `oneOf` / `$ref` / `format` 会被静默忽略，模型与审批人以为检查过 |
| 审批摘要只绑 `tool_version + schema_hash` | 改一句工具描述，旧批准照样匹配 |
| 路径参数不区分 URI | `https://example.com/a` 被拼成 `$WS/https:/example.com/a`，不报错也不联网 |
| 身份字段只是"碰巧"被 `additionalProperties: false` 挡住 | 哪个工具一旦放宽成 `true`，`run_id` / `budget` 就能从参数进来 |

## 2 封闭的 schema 方言（`src/tianshu/toolschema.py`）

方言 = 下表列出的关键字，**多一个都不认**：

| 位置 | 允许 |
|---|---|
| 根 / 对象 | `type: object`、`properties`、`required`、`additionalProperties`（只允许 `bool`）、`title`、`description` |
| 数组 | `type: array`、`items`（单个 schema）、`minItems`、`maxItems` |
| 标量 | `type: string\|integer\|number\|boolean`、`enum`、`minimum`、`maximum`、`minLength`、`maxLength`、`pattern`（`re` 编译失败即拒绝） |

**拒绝**（注册期，`ToolSchemaError`，报出工具名 + JSON 路径 + 关键字）：
`oneOf` / `anyOf` / `allOf` / `not` / `$ref` / `$defs` / `definitions` / `$schema` /
`format` / `multipleOf` / `uniqueItems` / `patternProperties` / `dependencies` /
`if` / `then` / `else` / `default` / `examples`。

两条理由写死在代码注释里：

1. `$ref` 要解析外部引用 = 读文件或联网，PRD 明确禁止自动联网解析；
2. 其余关键字要么是注解（按规范可以忽略），要么需要一套没实现的语义。**忽略**
   不违反 JSON Schema，但违反准入边界：注册表里写着、执行侧不看，等于对模型和
   审批人一起撒谎。

`minimum` / `maximum` 只接受整数——本项目所有授权参数都是整数，混进浮点会让
"摘要"与"比较"两套语义分叉（`canonical.py` 拒绝浮点是同一条理由）。

## 3 注册期 `validate_registry()`

`tools.py` 导入时对内置 `SPECS` 跑一遍（`validate_registry()`），所以：

- schema 写了方言外的关键字 → **import 就炸**，不是等模型调用；
- `required` 引用了未声明的属性、`minItems > maxItems`、`type` 缺失、
  `additionalProperties` 不是 bool、根不是 `object` → 同上；
- 工具键与 `ToolSpec.tool_id` 不一致 → 同上（注册表键就是身份，不能两处不同）。

测试对 `SPECS` 打补丁后必须显式 `validate_registry({...})`，不给"运行时偷偷
放行"留后门。

## 4 执行期校验

`tools.validate_args(args, schema)` 保留原签名与异常类型（`ToolArgumentError`），
内部改成"编译（带缓存）+ 逐层校验"，因此 `loop.py` / `memory_exec.py` 一行不用改：

- 嵌套对象、数组元素、`enum`、`pattern`、长度与数值边界都真的执行；
- 错误消息带路径：`参数.argv[0] 期望 string，实际 integer`、
  `参数.limit 不能大于 5，实际 99`；
- **准入在派发之前**：`loop._dispatchable()` 现在用完整 schema 判定，非法参数
  不会产生审批请求，模型收到一条 `tool.not_dispatched` 说明原因。用户不该被
  要求批准一个已经被判死形的调用。

## 5 工具身份绑定

PRD 原文：「任务绑定固定工具版本、schema 与描述摘要；动态发现不等于获得调用权限，
更新不得热替换既有批准。」

`ActionRequest` 现在同时绑三项：`tool_version`、`schema_hash`（参数 schema 的规范
摘要，已有）、`description_hash`（**本轮新增**）。派发侧在 `build_request()` 里从
当前注册表取，不信任调用方：

- 改版本 / 改 schema / 改描述 → `action_digest` 变 → 旧批准 `find_for()` 匹配不到
  → 必须重新审批；改回去又能匹配（负控钉住"原因就是这一项"）；
- `description_hash` 是**条件字段**：只有非空才进规范字典。历史请求（本轮之前
  写的）摘要**逐字节不变**，历史库里的审批 / grant / 派发记录不作废。这条取舍
  与 `resources.py` 用 scheme-prefix 而不是新增 `resource_kind` 字段是同一条理由，
  在 `tests/test_g1_admission.py::test_legacy_request_digest_is_byte_identical`
  里按摘要值钉死。

## 6 执行上下文由系统提供

`toolschema.MODEL_FORBIDDEN_ARGS` 是封闭集：
`run_id` / `task_id` / `call_id` / `attempt_id` / `job_id` / `dispatch_id` /
`claim_epoch` / `lease_epoch` / `goal_revision` / `owner_instance_id` /
`approval_id` / `grant_id` / `budget` / `budget_account` / `deadline`。

两道闸：

1. **注册期**：任何工具 schema 声明这些属性 → `ToolSchemaError`；
2. **执行期**：参数里出现这些键 → `ToolArgumentError`（即使该工具放宽了
   `additionalProperties: true`，封闭集优先）。

真正的原因不在这一层：`ToolContext` / `AdmittedCall` 已经由系统注入 run / call /
attempt，**没有任何代码从参数读身份**。这两道闸是让"schema 允许"和"执行侧接受"
口径一致——否则将来某次放宽会悄悄把身份字段带回来。

## 7 资源种类与 URL

- `resources.RESOURCE_KINDS = {file, memory, network, mcp, subtask}`（G1d 起五种，
  封闭集）；未知 `scheme://` 一律拒绝（既有）。
- 路径参数在解析前拒绝 `^[A-Za-z][A-Za-z0-9+.-]*://` 形状：`read_text` /
  `list_files` / 所有写入目标 / `run_command` 的 `cwd`。URL 不会被拼进工作区。
- `network://` 已在 G2 网络阶段接入（外发白名单 + 逐次批准 + 内容引用）；
  `mcp://` 已在 G2 MCP 阶段接入（stdio 客户端 + 动态工具准入 + 任务级 server 白名单
  + 逐次批准，见 [mcp-g2.md](mcp-g2.md)）；`subtask://` 已在 G2 子代理 D1 接入
  （`max_subtasks` 声明 + 人工批准 + 数量/局部限额 + `subtasks` 维度预占，见
  [resource-kinds.md](resource-kinds.md) §8）；**D1 只排队、不执行子 Run**（执行在 D2）。新种类**不得**复用 file 分支。

## 8 未实现的部分（不许当作已完成）

| 缺口 | 现状 | 何时能做 |
|---|---|---|
| 五种资源种类 | **全部已补**（G1d 解析与许可；执行器：网络在 G2 网络阶段、MCP 在 G2 MCP 阶段、委派在 G2 子代理 D1 接入派发分支——**D1 只排队、不执行子 Run**，执行在 D2） | — |
| MCP 工具的参数 schema | **已接入准入**：server 的 `inputSchema` 归一进本方言，认不出就拒该工具（`tool_registry.normalize`） | — |
| 私有内容外发许可 | **已接网络面**（G2 网络阶段）：外发恒需人工批准、未声明白名单不发包；**内容分级仍未做** | 内容分级单独立项 |
| 大结果「有界摘要 + 内容引用」、分页读取引用并验证任务可见范围 | 只有 `read_text` 截断 + `truncated` 标记与 `run_command` 的 blob 目录；没有 `ref` 内容引用与分页工具 | 单独立项（要同时改 context / blob / 工具契约） |
| 无 workspace 的研究任务 | 记忆只读、网络抓取、子任务派发都不需要绑定目录；**子任务本身不会执行**（D1 只排队，执行在 D2） | 已实现（子任务执行在 D2） |
| 取消 / 额度耗尽后无新请求 | 共享账本已落地（G1b），子任务在 spawn 时按 `subtasks` 维度预占、额度不足拒发；**子任务收尾结算与取消传播在 D2** | D2（`docs/SPEC-G2-DELEGATION-2026-11-11.md` §2.5–§2.6） |
| 隔离门禁 | 本轮不新增高自主执行能力，因此没有新增 §8.1.1 门禁项 | 新增任意代码执行能力时 |

## 9 加新工具 / 新资源种类时的检查表

新增工具：

1. `ToolSpec.input_schema` 只用 §2 的关键字；描述写清楚副作用与限制。
2. 有文件目标的工具必须给 `path_arg`；模型不能看见的工具 `exposed=False`。
3. 加进 `SPECS` 后 `validate_registry()` 必须过（导入即校验）。
4. 测试：非法嵌套参数、越界值、身份字段、URI 路径各一条。

新增资源种类：

1. 先改 `resources.RESOURCE_KINDS` + `parse_resource()` + PRD §5.4.1 说明；
2. 补执行侧外发 / 准入规则与 §8.1.1 隔离证据；
3. 不许把新种类伪装成绝对路径——`file` 分支只处理真实路径。
