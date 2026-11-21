# 公开网页搜索工具契约

状态：契约提案。浏览器自动化及远程 MCP 不属于此工具。

`network.search` 1.0 是 C2 的 E3 `fetch` 动作。封闭输入 `{origin, query}`：`origin` 是用户所选、任务白名单已声明的 `network://https/<host>[:port]` 源；不接受路径、query、fragment 或凭据。`query` 是 1–400 字符的非空单行查询。执行侧固定构造 `/search?q=<UTF-8 percent encoding>&format=json`，要求使用提供 SearXNG JSON 搜索的服务。没有默认服务；未配置服务或 JSON 被服务端禁用时，工具明确失败。

目标和查询都绑定到原有网络请求、人工审批、预算及审计。该工具不会自动访问搜索结果中的链接，不带代理、Cookie 或凭据，不跟随重定向。审批展示完整目标。执行由现有网络执行器完成，继续拒绝私网或非全局地址，限制 DNS、连接和读取的期限及体积；语义错误、非成功 HTTP 状态及截断 JSON 不算零条结果。

正常结果保留既有 `network.fetch` 证据字段和内容引用，同时增加 `results`，最多十项，条目为 `{title,url,snippet}`。仅展示 `http` 或 `https` 且不含 URL 凭据的链接。标题与摘要是外部数据，不构成命令。回放核对已保存结果和引用，不重发查询。
