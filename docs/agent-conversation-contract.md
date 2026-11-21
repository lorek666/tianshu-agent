# 桌面会话输入契约

状态：契约提案。合入契约不表示客户端、适配器或运行时已交付。

## C1：用户消息

- `user.content` 仍是字符串。由桌面用户随本轮提交的附件可通过可选 `attachments` 进入该轮的最后一条起始 user 消息。其值是与 `run.attachments` 相同的记录数组；没有附件就不写这个键。附件不会隐式复制到追问、子任务或定时任务。
- 每次最多 4 个附件，解码总量不超过 240 KiB。每条是封闭对象 `{format_version:1, name, media_type, data_base64, sha256}`，版本必须是整数。`name` 是不超过 128 字符的非空文件名，不接受目录、控制字符或路径分隔符。`media_type` 为 `text/plain`、`image/png`、`image/jpeg`；分别要求合法 UTF-8、匹配图片格式签名。`data_base64` 使用标准、有填充的规范 Base64，SHA-256 来源是解码后的原始字节。适配及回放均核验内容绑定。
- 文本附件由适配器与文件名一起转换为用户资料。Responses 适配器将文本转成 `input_text`，支持图片输入的配置将 PNG/JPEG 转成 `input_image`。未明确开启图片能力的配置在模型派发前拒绝图片，不自动更换端点，也不静默丢弃附件。上传授权仅代表作为本轮模型输入，不授权工具写入、出网、转发到其他工具或访问原文件。

## C3：会话事实

现有 `run.start` 和历史消息保持原形。以下均为封闭对象，只承认 payload v1：

- `run.conversation`：`{turns:[{source_run_id, source_seq, answer, message}]}`，`source_seq` 是源 Run 的最终成功 `model.result` seq。空数组选择通用的桌面任务提示。每次追问保留选定分支的已提交文本问答，按原顺序放在新 Run 的任务陈述之后。总量超过 64 KiB 时显式拒绝。源必须是同任务、同目标修订的成功 Run；不得转移工具结果、授权、续接密文或模型身份。
- `run.attachments`：`{attachments}`，记录上面定义的附件，每个 Run 最多一条，仅用户提交非空附件时产生。会话起点和附件事实与 Run 创建及入队同事务提交。快照和回放以已提交事件为源，不再打开原始文件，也不从 UI 导出的视图补内容。桌面事件视图只公开附件名称、格式等元数据，不公开编码字节。
- `run.guidance_requested`：`{text}`，单条 UTF-8 最多 4 KiB，每个 Run 最多 16 条；用户请求按事件 seq 排序。`run.guidance_applied`：`{request_seq}`，引用同 Run 的请求并至多消费一次，与引入该指令的游标同事务提交。回放按 applied 事件的位置插入用户消息，待消费请求不是模型输入。
- 指导在既有工具调用组完成后、下一次模型生成前生效；不会改写已提出或待批的调用，不会自动续跑暂停或待批的 Run，不构成工具授权。Run 终止、达到预算或失去认领权时仍可能存在未消费的指导，界面应区分提交和生效。


## C3：模型路由绑定

显式选档的 Run 增加一条 `run.model_route` v1，与开始和入队同事务提交。载荷为 `{format_version:1, requested, selected, config}`；`requested` 是 1–4 个互异的有序档案名，`selected` 必须属于该列表。`config` 是封闭非密钥配置，要求 `mode`、`model_name`、`base_url`、`credential_backend`，可选仅用于 Responses 的 `allow_image_attachments`。使用线上模式时保存实际冻结的端点，禁止端点含凭据、查询或 fragment。凭据本体不进事件，驱动时从既有凭据接口读取。

仅在任何请求派发前按候选顺序进行本地配置与凭据就绪检查。若有图片附件，候选还必须明确支持图片。选定后，本 Run 以及它的恢复固定该绑定，不因配置被改、凭据失效或模型故障而换路。追问另开 Run，由用户决定是否沿用档案。没有路由事件的历史运行保持旧的模型解析行为。

## 远程 MCP（C2/C4）

- `TIANSHU_MCP_CONFIG` 的每个 server 只能选其中一种：既有绝对路径 `command`，或固定的 HTTPS `url` 和可选 `bearer_token_env`。URL 不接受 userinfo、查询或 fragment；令牌只从指定环境变量读取。传输不走代理/重定向，连接使用网络工具现有的公网 IP 钉住及 TLS 验证。普通状态页和只读计划不主动连接远程端点；有副作用的 Run 只发现任务显式声明的远程 MCP。
- 远程发现只发送 `initialize`、`notifications/initialized`、`tools/list` 所需字段，不带任务资料。固定 Streamable HTTP 的有界 POST 响应，允许 JSON 与承载匹配 JSON-RPC 响应的 SSE；不实现 OAuth、GET 通知流、SSE 恢复、服务端发起请求或自动重连。会话标识只在客户端内存使用。
- `tools/call` 仍以既有 `mcp://server/tool` 资源、任务声明、逐次审批及执行前持久化边界执行。远程工具版本额外绑定 HTTPS 端点、凭据环境变量槽及本次握手独有标识；不持久化密钥或会话 ID。远程会话失效或换代时，已有请求和审批不能迁移至新会话。已越过发送边界后的 HTTP 拒绝、超时、协议错误均按结果未知结算，不向新端点转发、不自动重试。继续使用既有 Receipt 及对象阻断机制。

## Responses 同轮压缩（C1/C3）

- OpenAI Responses 模型可显式配置 `compact_threshold`（令牌，整数 1024–2000000）。默认关闭。开启时在同一个 `POST /responses` 请求中启用 `context_management`；仍 `store=false`、不使用 `previous_response_id`，不另发计费调用。模型档案若被显式选用，阈值随 Run 路由冻结。
- 已完成的输出回合可在前缀位置携带 `compaction` 加密项。无压缩项时沿用 `ContinuationRef` 1.0 / `model.result` v2；有压缩项时使用 `ContinuationRef` 1.1 / `model.result` v3。仅接受有唯一 ID、非空密文的单一压缩项，位置在本回合任何文本或工具提案之前；未知项或矛盾的流帧拒绝整组。新记录与旧记录按各自精确版本读取。
- `ctx-responses-compact-v1` 只在已有已提交压缩项时，用最后一个压缩边界替换旧输入前缀，保留系统指令、该回合压缩项及后续本轮输出和实际工具结果。完整事件/游标不删除；回放以相同策略核对消息摘要及来源。没有新边界且超过本地预算时如实停止。密文继续只允许原 adapter、规范端点、模型使用，不进入用户可见文本、明文摘要或其他 Run。
