# 受限 HTML 表单提交契约（待实现）

`browser.navigate` 2.0 在只读、逐次批准的公开 HTTPS 导航结果中增加可提交表单的描述。`browser.submit` 1.0 只接受同一 Run 成功导航的 `from_call`、从零起的 `form_index`、原样的目标 `url`，以及该表单全部控件的 `fields`。执行侧从当时保存、可见、重新计算摘要的 HTML 中重建表单，并核对目标与字段。网页内容不授予任何权限。

支持同源、无查询的显式 `POST` 和 UTF-8 `application/x-www-form-urlencoded`。只解析完整且不超过 64 KiB 的来源 HTML；仅支持具备唯一名字的普通文本输入，并要求人看到完整参数。拒绝 hidden、password、file、未识别的控件、默认值、目标覆盖属性、`base`、外部关联控件和其他表单编码。只按显式字段提交，不发送 Cookie、Authorization、Referer 或自定义头。不执行脚本、不持有登录态。

复用 C4 的 `fetch` 外发动作与 `c4.outbound`，按工具身份、完整参数、请求目标和本次 Run 把 SHA 绑定到一次批准。批准信息显示 POST、目标、字段和值。校验、审批消费与已提交 intent 在 socket 前完成；在执行守卫内重新核验来源、参数，检查同一来源表单没有已开始的提交，并记外发边界。预算继续记录网络工具收到的字节；表单正文另设固定上限。只建立固定到公开地址的 TLS 连接，只发一个 POST，不重定向、不自动重试。

已发出的 POST 收到无正文的 204，或收到带单一明确 `Content-Length` 的完整有界 2xx 时，`applied` 仅表示接收端确认这次请求。结果、内容引用与 `backend="network"` 的 Receipt 同事务落库；Receipt 引用 intent 和 result，并绑定目标、参数摘要、HTTP 状态与响应正文摘要，可离线核验。没有越过外发边界的拒绝记 `none_applied`；发送后断线、超时、非 2xx、无确定分帧或效果无法核对记 `unknown`，恢复只对账。历史只读导航保持可回放，既有审批保留原绑定。
