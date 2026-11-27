# 2026-11-27 · MCP 入站结果整合复验

[#235](https://github.com/lorek666/tianshu-agent/pull/235) 整合候选保留已合入的受管 MCP Session、启动准入、代次核对和结束清理。事件和游标只留结果引用及固定元数据；受管调用通过 `execution.admitted` 的 call ID、request ID 和生命周期核对。旧 inline 记录仍可读。

- 损坏 blob 按上限读取，核对摘要、字节数以及元数据的值和类型。独立 `data_root` 时，空的 snapshot 定位只解析到可信运行配置里的默认目录；记录提供的显式定位不能逃出 DB 根，相对定位与同 UID TOCTOU 仍有边界。
- JSON-RPC 明确拒绝保留 `none_applied`；未能确认的对端失败保留 `unknown` 与对象阻断。事件保存本地分类，不写对端错误正文。结果未知时不自动重发。
- 保留 `main` 的 SLO 优先顺序、Windows 四片与 macOS 两片、缺件即红和原有时间上限；历史 Windows 两片验收记录仅适用于[原候选](2026-11-24-mcp-result-content-ref.md)。

整合已复现上述问题并通过定向红绿；最终本机门禁、独立复审和托管 CI 以当前候选的回执为准。真实第三方 MCP、用户设备与后代进程监管均不在此验证范围。
