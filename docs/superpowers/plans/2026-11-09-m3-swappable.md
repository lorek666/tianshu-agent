# M3 · 可替换（第二个 provider + 第二个沙箱后端）

PRD §11 对 M3 的定义只有一句：**"第二个 chat provider + 第二个沙箱后端"**，
退出条件是**"契约测试全绿，主循环 0 改动"**。

这句话是整份架构里最容易被自己骗过去的地方。加一个"另一个 base_url 的
OpenAI 兼容端点"不算第二个 provider；把 `Workspace(root)` 换一个 root 也不
算第二个沙箱。真正的判据是：**换掉实现时，主循环一行都不用改，而且两个实现
跑的是同一套契约测试。** 这一阶段就是去把这条判据变成可执行的东西。

## 1. 现状盘点：三个真问题

M2 结束时 `model.py` 只有 240 行，三个 adapter 里两个是测试替身，所以从没被
第二个真实协议顶过。真做时会撞上三件事：

**(1) `finish_reason` 有越界值。** PRD §5.5 给的是 13 值封闭集合。
`OpenAICompatibleModel` 却在 HTTP 失败时返回 `finish_reason="error"`、在
`_map_finish` 兜底时返回 `"unknown"`——两个都不在集合里，靠
`# type: ignore[arg-type]` 压着。主循环拿到 `"error"` 会走"模型调用未正常
结束"分支（行为正确），但**契约早就破了**：`model.result` 事件里存着一个
不允许出现的原因值，`schemas/m1/model.result.json` 挡不住（没枚举），换
provider 时谁也不知道要不要接着支持这两个值。

**(2) C1 里"错误"是对象，不是字符串。** PRD §5.1 的核心对象列表里写着
`ModelError`，而代码里根本没有这个类型。传输失败、401、429、被掐断，全被
压成一个伪 `finish_reason`，于是"重试"和"降级"这类策略层信息在接缝处就丢了。

**(3) 主循环说的不是"中立格式"，是 OpenAI 的消息格式。** `Model.complete()`
收的是 `[{"role": "tool", "tool_call_id": ...}]` 这种 OpenAI 形状的 dict，
而且这份 dict 会被 `save_cursor` **持久化进游标**、被 `messages_hash`
盖进事件、被 `replay` 用来重建。也就是说：所谓"换 model 不用改主循环"，
前提是新 adapter 愿意吃 OpenAI 格式。这不是错误——**但它必须被写下来**，
否则下一个人会以为内部格式是"随便什么"，然后在 adapter 里偷偷改主循环传的
消息，把 replay 打烂。

结论：M3 第一件事不是写第二个 adapter，而是**把接缝的定义写成契约，再用
第二个 adapter 去证明它**。

## 2. Task 1 · C1 接缝做成真的（PR E）

- [x] **1.1 `ModelError` 落地。** `kind ∈ {transport, timeout, http_status,
      auth, rate_limit, protocol}` + `status_code` + `retryable`（建议，不是保证）。
      `from_http()` 做状态码分类（401/403→auth、429→rate_limit、408→timeout、
      5xx 与其余→http_status），`__repr__` 带类别与状态码——因为主循环把
      `repr(exc)` 写进 `model.error` 事件，事后要能判断"该重试还是该换 key"。
      **主循环 0 改动**：它本来就有 `except Exception` 分支。
- [x] **1.2 封闭集强制执行 + usage 归一化。** `ModelResponse.__post_init__`
      校验 13 值、把 `usage` 归一化成两个规范键（缺失 `None`）。
      顺带清掉了旧测试里的 `"error"` / `"length"` 两个字面量。
      **执行记录**：`test_out_of_set_finish_reason_fails_fast` 覆盖
      `error/unknown/length/max_output_tokens/""` 五个越界值。
- [x] **1.3 内部消息形状写进 PRD §5.1**（三张表 + 三条规则），
      并写明"事件里的 `arguments` 是解析后对象、解析失败保留原串、adapter 不许修好它"。
- [x] **1.4 `AnthropicMessagesModel`。** `system` 提顶层；`tool_use` / `tool_result`
      内容块；连续多条 `tool` 消息合并进同一条 `user`（Anthropic 要求交替）；
      工具定义直接吃 C2 投影（不需要 `{"type":"function"}` 包装）；`max_tokens`
      必填；`stop_reason` 映射 6 个已知值，`pause_turn` 等未映射值**报错**而不是猜。
- [x] **1.5 契约测试 `tests/test_model_contract.py`（59 个用例）。**
      两个 adapter 参数化跑同一组断言，全部走注入的 `Transport`（零网络）；
      HTTP 分类那几条**替换 `urlopen` 跑真的 `UrllibTransport`**，不造假。
- [x] **1.6 CLI `--provider {openai,anthropic}`**、`TIANSHU_PROVIDER`、
      `ANTHROPIC_API_KEY`（回落到 `TIANSHU_API_KEY`）。anthropic 缺 `--model-name`
      直接报错，不替用户猜模型 id。

**执行记录（真实端点，两个协议各跑一次完整 Run）**

网关 `https://www.aizhongzhuan.cc/v1` 同时支持两种协议，于是两个 adapter 都做了
**真实的**端到端验证，而不是只有单测：

| provider | run_id | 轮数 | tokens | 效果 | replay |
|---|---|---|---|---|---|
| openai-compatible | `run_4472b08f5cd6` | 5 | 5032 | applied=1 | 通过（44 事件 / 5 次消息校验） |
| anthropic | `run_fd0f09d5c71c` | 4 | 1743 | applied=1 | 通过（39 事件 / 4 次消息校验） |

两次都真的写出了 `summary.md` 并留下了 `applied` 凭据。这条证据的意义是：
**同一份主循环、同一套工具、同一个事件日志，跑在两个线上协议上。**

**真实模型暴露的缺陷（不在本 PR 修，已记录）**

第一次跑（`--max-turns 4`）连续两轮被 `tool.not_dispatched` 拦下：模型给
`read_text` 漏了 `path`、给 `create_report` 用了 `path` 而不是 `name`。
两个事实：
1. 拦得对、反馈得对——下一轮模型自己改对了（说明 A7 那条"未派发也要留痕"的
   设计在真实模型上有效，不是只对 ScriptedModel 有效）；
2. 但**工具参数命名不一致**（`list_files`/`read_text` 用 `path`，
   `create_report` 用 `name`）会稳定地浪费真实模型的轮数。这是产品缺陷，
   不是模型笨。改法涉及 C2 schema 与 `args_hash`/动作摘要，必须单独一张票，
   不能顺手改。

## 3. Task 2 · 沙箱后端接缝（PR F）

- [ ] **2.1 接缝设计**（见 `docs/superpowers/notes/m3-backend-seam-proposal.md`）。
- [ ] **2.2 第二个后端落地**，并让**同一套** contract test 覆盖两个实现。
- [ ] **2.3 诚实边界**：这不是 OS 级隔离。真正的进程/文件系统隔离是 M5 的
      Host 的事，此处只能证明"工具副作用经过一个可替换的边界"。

## 4. Task 3 · 验收与文档（PR G）

- [ ] `docs/M3-verification.md`：逐条验收矩阵 + 实测输出 + 独立复审记录。
- [ ] **"主循环 0 改动"的证据**：`git diff main -- src/tianshu/loop.py` 为空，
      并把这条命令的输出贴进验收文档（不接受"我印象里没改"）。
- [ ] 更新 `README.md` / `docs/TECH-ARCH.md` 的 provider 与后端小节。

## 5. 明确不做

- 不做第三个 provider（两个已足以证明接缝；第三个只增加维护面）。
- 不做流式（`StreamEvent` 契约留着，v1 不用）。
- 不做 `ContinuationRef` 的签名/thinking 往返（PRD §5.1 要求"原样往返"，
  但 v1 的两个 provider 都没有需要往返的 opaque 字段；写进未验证清单）。
- 不声称任何物理隔离。`Workspace` 是路径边界，不是安全边界。
