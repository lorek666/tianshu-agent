# 2026-11-21 · 本机 Webhook 接收器

为已有本机事件日程接收经 HMAC 验签的 HTTP 请求。接收器由用户单独启动，只绑定 `127.0.0.1`；桌面 Host 和 Daemon 的监听方式不变。Daemon 必须已运行，接收器和 Daemon 使用同一个数据根。先创建事件日程，再将随机密钥放入进程环境并启动：

```bash
tianshu schedule create --desktop --goal '执行已配置的工作' --on-event
tianshu schedule webhook-serve SCHEDULE_ID --secret-env TIANSHU_WEBHOOK_SECRET --port 8793
```

`SCHEDULE_ID` 使用上一条命令返回的 `sched_...`；Daemon 使用自定义数据根时，接收器加 `--data-root` 指向同一目录。`TIANSHU_WEBHOOK_SECRET` 由操作系统或进程管理器注入，取 32 到 4096 个 UTF-8 字节，建议每条日程各自生成随机密钥。重启接收器可更换密钥；密钥不通过命令行或事件传递。

发件方发送 `POST /v1/webhook`，`Content-Type: application/json`，正文是只包含 `event_id` 的 UTF-8 JSON 对象（最长 1024 字节），例如 `{"event_id":"import:2026-11-21"}`。事件 ID 首字符为 ASCII 字母或数字，后续允许字母、数字及 `_.:-`，最多 128 字符。发件方同时提交：

- `X-Tianshu-Timestamp`：Unix 秒，服务器允许最多 300 秒时钟偏差。
- `X-Tianshu-Signature`：`sha256=` 加小写十六进制 HMAC-SHA256。使用该接收器配置的密钥，对 ASCII `tianshu-webhook-v1\n<SCHEDULE_ID>\n<TIMESTAMP>\n`（`\n` 是换行字节）拼接原始正文计算。

正文长度用单个 `Content-Length` 传递；不接受 `Transfer-Encoding`、`Content-Encoding` 或 URL 查询串。请求头和正文共享一次最多 3 秒的读取时限，超时直接关闭连接。验签后通过本机认证 IPC 提交既有 `schedule.signal`，发件方不能更改任务目标、权限或工作区。有效请求返回 `202`，`{"accepted":true}` 表示本次入队，`{"accepted":false}` 表示同一事件已处理。停用或删除日程会拒绝新触发。遇到响应丢失、断连或 `503` 时，发件方可刷新签名时间并用**原事件 ID**重试；接收器不会自动重发。

需要公网来源时，在同一台机器上自行部署 HTTPS 反向代理，仅将对应入口的 `POST` 转发至绑定该日程的 `http://127.0.0.1:8793/v1/webhook`。代理须保持正文和两个签名头原样，先缓存并限制请求体大小，再以明确的 `Content-Length` 转发；还应限制客户端并发、请求速率和慢连接，避免占用串行接收器。关闭请求体和签名头的访问日志记录，不将 Daemon IPC、桌面 Host 或其他本机端口暴露到公网。

当前验证使用真实回环 HTTP、CLI 子进程和认证 Daemon，检查固定日程、拒绝过期与篡改、入队去重、停用以及超时后恢复接收。公网代理部署、证书和外部发送方未进行端到端验收。
