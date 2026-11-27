# Windows Job 命令审批绑定契约

**原方案**：命令审批绑定动作与参数摘要；审批卡片另外展示后端和隔离报告。仅凭路径和参数无法证明批准后仍会使用同一个后端或同一份程序内容。

**新方案**：显式 `windows_job_object` 的批准同时绑定准入后的 `ExecutionSubject`。兑现时与业务启动前分别复核后端和摘要，缺记录或有变化时拒绝。原有人工决定、审批消费和 Grant 仍然必需。

**为什么**：防止用普通命令后端兑现一个只批准使用 Job 的请求，也防止同路径的脚本替换后沿用旧批准。

**影响**：后续实现会扩展 `approval.requested` 当前 v1 的可选字段，同步 Python 与 JSON schema，保留旧 v0。执行侧涉及审批存储、派发与 `PreparedExecution`，不改变其他后端的既有授权。验证覆盖后端和脚本变更、缺证据、旧记录可读及拒绝路径零业务启动。仅核对现场可见内容，不扩展为防同用户篡改或完整沙箱承诺。

本提交只定义契约。实现由 [#258](https://github.com/lorek666/tianshu-agent/pull/258) 跟踪；完整隔离仍由 [#224](https://github.com/lorek666/tianshu-agent/issues/224) 跟踪。
