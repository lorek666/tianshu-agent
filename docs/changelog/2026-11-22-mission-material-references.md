# Mission 输入材料引用追踪（2026-11-22，本地候选）

独立复审发现：`run.mission` 和 `run.mission_pending` 已将首轮消息、模板正文写入
事件 blob 目录，但备份与 GC 只追踪事件外置 `payload_ref`，没有读取事件内部的
`messages_ref` 和 `mission.prompt.content_ref`。有效 Mission 输入因而被当作孤儿；
清理后恢复又会因材料缺失而拒绝。

本次由保存的真实事件 payload 推导两类引用，复用 Mission 的封闭 schema 和摘要
校验，并接入 GC 引用保护、备份 `mission_blobs` 引用组及独立备份校验。支持标准
`blobs` 与 `artifacts/blobs` 布局，引用文件继续使用原生实体与内容哈希核验。
材料缺失、篡改或 Mission payload 损坏均闭锁拒绝，不调整 GC 年龄或删除范围。
历史 `material_storage=inline` 仍只依赖事件内正文；旧备份没有 `mission_blobs` 组
时也从保存的数据库重推导，不回源补材料，不改写历史事件。

真实红绿日志保存在 ignored `.omc/artifacts/mission-mcp-integration/`：

- `red-mission-gc.log`：复现独立探针，两类事件的输入被列为孤儿。
- `red-mission-materials.log`：旧实现允许缺少 Mission 正文的备份通过，且无法识别
  被隐藏的损坏与非法 Mission 输入。
- `green-mission-materials-final.log`：真实预演及批准 GC 保留两类输入，同时回收
  真正孤儿；GC 后仍能读取 pending 输入或恢复 frozen Run。覆盖两种标准 blob
  布局、历史 blob/inline 语义、外置大事件、缺失/篡改材料及隐藏清单负控。
- `green-mission-gc-domain-final.log`：备份、GC、Mission 与相关恢复域回归。

本机验证不等于最终组合候选的完整门禁；本机全量、独立第二读者与 hosted required
CI 由整合方绑定最终代码树。Windows 含文件删除的批准 GC 继续按原策略拒绝；本次
没有将本机 POSIX 文件清理结果扩展为其他平台的文件删除证明。
