# M3 验收：可替换（第二个 provider + 第二个沙箱后端）

PRD §11 给 M3 的定义是 **"第二个 chat provider + 第二个沙箱后端"**，退出条件是
**"契约测试全绿，主循环 0 改动"**。

这份文档只回答三个问题：判据成立吗（§1）、每一项怎么证明的（§2–§4）、
**哪些没证明**（§6）。最后一条比前两条重要——M3 最容易自我欺骗的地方是
"我把接缝写出来了"，而不是"接缝真的被第二个实现顶过"。

## 1. 判据

| 判据 | 机械证据 | 结果 |
|---|---|---|
| 主循环 0 改动 | `git diff dab74cc -- src/tianshu/loop.py \| wc -l`（`dab74cc` = M2 合并点） | **0** |
| 契约测试全绿 | `uv run pytest -o addopts="" -q` | **514 passed, 1 skipped** |
| 两个线上协议跑同一套断言 | `tests/test_model_contract.py`（69 用例，参数化 vendor） | 全绿 |
| 两个沙箱后端跑同一套断言 | `tests/test_backend_contract.py`（83 用例，参数化 backend） | 全绿，1 skip 见 §6 |
| 环境不污染"全绿" | `TIANSHU_SANDBOX_BACKEND=memfs uv run pytest -o addopts="" -q` | **514 passed, 1 skipped**（与不带变量一致） |

`loop.py` 一行未改，所以后端选择只能是**进程级**（`TIANSHU_SANDBOX_BACKEND` /
`configure_backend()`）：`build_stack` 就在 `loop.py` 里，把后端塞进装配层会让
第一条判据失效。代价是一个进程不能同时用两个后端；per-run 灵活性留给 M5 host。

## 2. Task 1 · 第二个 provider（PR #15，合并 `55badf5`）

六项验收：

| # | 项 | 证明 |
|---|---|---|
| 1 | `finish_reason` 落在 PRD §5.5 的 13 值封闭集 | `ModelResponse.__post_init__` 强制校验；`test_out_of_set_finish_reason_fails_fast` 覆盖 5 个越界值 |
| 2 | 传输/HTTP/协议失败是 `ModelError`，不是伪 `finish_reason` | `test_http_status_is_classified`（401/403/429/500/503/400 各自的 kind 与 retryable）、`test_network_error_is_transport`、`test_timeout_is_timeout`、`test_non_json_body_is_protocol` |
| 3 | 未映射的线上语料**不兜底成成功** | `test_unknown_finish_reason_is_error_not_success`（含 `None`/`""`/`weird`）、`test_missing_finish_reason_is_error`；错误里带模型原文摘要（`test_unmapped_finish_reason_keeps_model_excerpt`） |
| 4 | `usage` 未知为 `null`，缓存 token 不并入输入 | `test_usage_missing_is_none_not_zero`、`test_anthropic_cache_tokens_not_folded` |
| 5 | 两个协议送出的**工具定义都真的带参数 schema** | `test_tools_carry_the_parameter_schema`（用真实 `tool_manifest()`；OpenAI 断言 `parameters`、Anthropic 断言 `input_schema`） |
| 6 | 第二个协议的历史约束不被留给线上 400 | `test_to_anthropic_messages_*`：`system` 提顶层、角色交替、多条 `tool` 结果合并、重复 `tool_use` id 去重、幽灵 `tool_result` 降级成文字、空 assistant 轮次跳过、assistant 开头/只有 system 明确报错 |

内部消息形状写进了 PRD §5.1（三张表 + 三条规则）：它进游标、进 `messages_hash`、
被 replay 用来重建，所以 adapter 只能翻译、不能反向改写。

### 真实端点证据

网关同时支持两种协议，两个 adapter 都跑了完整 Run（不是只有单测）：

| provider | run_id | 轮数 | tokens | 效果 | replay |
|---|---|---|---|---|---|
| openai-compatible | `run_c116133ffc57` | 4 | 4438 | applied=1 | 通过（39 事件 / 4 次消息校验） |
| anthropic | `run_7c2db1fc8d18` | 3 | 1190 | applied=1 | 通过（33 事件 / 3 次消息校验） |

## 3. Task 2 · 第二个沙箱后端（PR #18，合并 `49432a5`）

接缝划在**存储原语**（`sandbox.FileBackend`，8 个方法）：工具逻辑——截断、UTF-8
回退、拒绝覆盖、写前证据顺序、output 形状、异常映射——留在 core；
`Dispatcher` 仍是唯一策略判定点，`FileExecutor` 仍是唯一"实际打开文件"的入口。

| 实现 | 是什么 | 会碰宿主盘吗 |
|---|---|---|
| `LocalBackend` | 真实文件系统 | 会（只读元数据 + 读写文件），且只有它会 |
| `MemBackend` | 进程内虚拟 FS | 不写；只 lstat 一次"命名空间是否存在" |

关键项：

| # | 项 | 证明 |
|---|---|---|
| 1 | 契约在两个后端上各跑一遍 | `test_backend_contract.py` 的 `env` fixture 参数化 `local_fs`/`memfs` |
| 2 | **违约后端必须让契约变红**（C0 金丝雀） | 7 条元测试：`write` 静默丢弃、`read` 无视 limit、`exists` 恒真、`unlink` 空操作、`require_root` 空操作、`identity_key` 恒常量、`is_file` 恒真 / `exists` 无视目录 / 目录 size 乱填 / `size` 不拒目录 |
| 3 | 越界防护没变弱 | `Workspace.resolve` 本体一行未改；local 下 17 条越界实测全拒（`../`×3、绝对×2、symlink×5、工具层×5），写入路径绕过 loop 直接给 Dispatcher 时两后端都是 `dispatch.lease_lost`（租约是第二道独立门） |
| 4 | 看盘逻辑真的收敛 | 工作区数据路径全部走 `ctx.io`/`current_backend()`；剩余宿主盘访问只剩证据库（事件 blob / 快照 manifest / 锁文件）与后端自身 |
| 5 | 执行器不会半真半假地跑 | `FileExecutor` 构造时与执行前各校验一次"当前后端没被换掉"，不一致 → `ToolNotAllowed`（一个字节没动） |
| 6 | 副作用可见行为在两个后端一致 | 返回形状、拒绝覆盖、64 KiB 截断、UTF-8 回退、删除、list 排序与 NFC、写前证据顺序、`effect_state` 只用实际会产出的三个值 |
| 7 | 顺带修的真 bug：覆盖写入丢 mode | `fsutil` 覆盖时保留原 mode（`mkstemp` 0600 + `replace` 会把 0644 变 0600）；新建仍 0600 |

## 4. 独立复审记录

两轮都用了**独立 context** 的复审者，只看 diff 不看我的描述。

| Pass | 对象 | 结论 | 处理 |
|---|---|---|---|
| A | PR #15（provider） | **阻断 1** / 建议 11 / 已核查 10 | 阻断项（OpenAI 侧漏翻 `input_schema → parameters`，参数 schema 从未发给模型）本 PR 内修复；11 条建议里 7 条采纳、4 条记为已知问题 |
| B | PR #18（后端） | **阻断 0** / 建议 12 / 已核查 10 | 4 条在合并前补提交修掉（测试后端钉死、memfs 单例复位、目录契约三处洞、目录目标崩溃）；其余记账 |

报告全文：`docs/superpowers/notes/review-pr15.md`、`docs/superpowers/notes/review-pr18.md`。

### Pass A 的阻断项值得单独记

OpenAI adapter 发的是 `{"type":"function","function":{name,description,input_schema}}`，
OpenAI 协议要的是 `parameters`。于是**参数 schema 从来没到达模型**，模型只能猜参数名。
这条漏翻从 M0 就在，而它能活到 M3，是因为新写的契约测试用了**手写的工具定义**并只
断言工具名——测试一旦用自己的替身，就只证明了替身和它对得上。

修前修后的对照（同一任务、同一模型、同一条主循环）：

| | `list_files` | `read_text` | `create_report` |
|---|---|---|---|
| 修前 `run_52e0076b11e3` | `{}` | `{}` → 未派发（缺 path） | `{content, path}` → 未派发 |
| 修后 `run_c116133ffc57` | `{path: "."}` | `{path: "notes.md"}` | `{content, name}` |

我当时把症状归因为"工具参数命名不一致是产品缺陷"并开了 issue #16。**归因错了**：
模型不是猜错名字，是它压根没拿到 schema。#16 已按事实更正并关闭。

## 5. 已知限制（登记，不在 M3 修）

1. **`MemBackend` 不是沙箱**：没有权限位、owner、符号链接、磁盘配额。它证明的是
   "工具副作用经过一个可替换的边界"，不是"副作用被限制住了"。PRD §8.1 威胁表不变。
2. **崩溃恢复证据仍只由 local 覆盖**：memfs 的状态随进程消失，无法参与
   "进程被杀之后还能不能核对"。
3. **memfs 不承诺符号链接**：那条契约是 `pytest.skip`（`-rs` 可见），不是"已验证"。
4. **memfs 无法表示空目录**：目录只来自根或写文件的父链；契约用 `.keep` 变通，
   已在提案覆盖矩阵写明。
5. **目录 size 统一成 0**：这是对 local 的可见行为变化（原来给 `st_size`，本机 64）。
   目录字节数本来没意义，两个后端不一致会让模型看到的东西随后端跳变。
6. **读失败/列目录失败从 `unknown` 变成 `failed/none_applied`**：新行为更对
   （读失败什么都没改），但对 local 是静默的语义变化，已记录。
7. **（M3 之后已修，issue #17）快照证据键只有相对路径**：多 workspace 会撞键，
   `undo` 可能写到错误目录。现在证据键是 `(workspace 根, 相对路径)`，撤销按每条
   记录自己的根落盘；旧格式（v2 清单）记录被**拒绝**撤销，没有自动迁移。取舍见
   `docs/superpowers/notes/pr-g-evidence-keys.md`。
8. **（M3 之后已修，issue #17）凭据不可"独立核对"**：`action` 曾是 tool_id、
   `resource` 只有相对路径、`evidence` 是合成字符串、`verify_receipts` 不核对磁盘。
   现在 `action` 是真实动作、`resource` 是绝对路径并带 `root`/`intent_seq`/`blob_ref`，
   `evidence` 是逐条可解析的结构化引用，`verify_receipts` 会核对事件、快照 blob
   与磁盘现状；旧格式凭据被报成"无法独立核对"而不是默认通过。
9. **内部工具 `restore_file` 能被模型提案派发**：`_dispatchable` 不校验 `exposed`（issue #19）。
10. **打开前没有 realpath 复核**：`Workspace.resolve` 在解析时消解符号链接，但
    resolve 与 open 之间仍有 TOCTOU 窗口（与 M2 同，未变好也未变坏）。
11. **`retryable` 无调用方**：给上层策略留的判据，不是自动重试。
12. **`ContinuationRef` 不做往返**：Anthropic 的 `thinking` 块当前跳过。

## 6. 未验证（不许说成已验证）

| 项 | 为什么没验 |
|---|---|
| Windows x64 / arm64 | 没有该平台；`TIANSHU_*` 全路径逻辑、`os.chmod`、锁文件语义只在 macOS 上跑过 |
| macOS x64 | 只有 arm64 |
| 真实 Anthropic 账号 | 两个协议都只用中转网关验证过；网关实现了 Messages API，但不是官方端点 |
| memfs 在真实 Host 里跑 | memfs 目前只服务于测试与（将来的）确定性 replay |
| memfs 上的崩溃恢复 | 状态随进程消失，无法验证——这正是它不承诺的原因 |
| 真实断电 / fsync 语义 | 与 M2 相同，无硬件级证据 |
| 符号链接逃逸在 memfs 上 | 未覆盖（不是已验证）：实测顺带被 `Workspace.resolve` 的宿主 realpath 挡住 |

## 7. 留下的票据

- #16 工具参数命名（**已关闭**：归因错误，真因是 adapter 漏翻 schema）
- #17 快照/凭据证据键只有相对路径，多 workspace 撞键（**已修**：见 §5 第 7、8 条）
- #19 内部工具 `restore_file` 能被模型派发

下一步（M4 可评估）需要 20 条 case + replay/evaluate 分离；`MemBackend` 正好是
确定性 replay 的落点。
