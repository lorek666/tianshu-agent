# 2026-11-19 · 恢复 B/C 持久化与 Windows 写入边界

恢复 B 在暂存、发布和清理后同步相关目录。重入时，匹配哈希的已有文件仍须完成文件 `fsync`，已出现的目标祖先须补做父目录屏障。发布改名后及清理已移除暂存根后，完成稳定父目录屏障才能登记终态。屏障失败时报告实际保留路径并由同批次管理命令续做。权威注册表在 POSIX 使用回滚日志及 `synchronous=EXTRA`，首次发布同步临时权威目录与发布父目录。

恢复 C 在 R2 提交后核验对账见证，完成主库回到自包含日志模式及持久化。R3 登记 `ReleasedV1` 前同步主库所在目录。失败保留隔离，重跑同批次复核与解除时恢复屏障并核对实际见证。见 [R2 重入校验](2026-11-19-restore-c05-reentry.md)。

Windows 原生实体标识、锁和注册表发布仍可使用。受管计划报告 `apply_supported=false`，绑定成立的既有批次仍可只读复核。有效的写入路径在缺少目录持久化原语时返回 `durability_unavailable`。W15 在同一平台限制下拒绝包含待删文件的整批清理。目录能力与设备验证由 [#200](https://github.com/lorek666/tianshu-agent/issues/200) 跟踪。

定向验证覆盖真实 CLI 进程硬退、稳定父目录同步失败及同批次重入、文件和祖先重入补同步、错误现场与终态注册表对账。证据入口为 [B 用例](../../tests/test_restore_b_execution.py)、[平台拒绝用例](../../tests/test_restore_durability.py) 与 [PR #198](https://github.com/lorek666/tianshu-agent/pull/198)。进程硬退与设备物理掉电分别验收。
