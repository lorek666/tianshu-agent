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
