# 2026-11-21 · 本机任务触发

桌面设置和 CLI 可以建立固定间隔、日历、目录新增文件和本机事件四种触发。触发后按预设目标和工作区创建普通任务，Run 仍走现有执行侧授权和审批。旧间隔配置保持原有表和请求格式；新类型单独存储，旧程序不会把它们当成间隔任务运行。

```bash
tianshu schedule create --desktop --goal '工作日汇总' \
  --at 09:00 --timezone UTC+08:00 --weekdays 1,2,3,4,5
tianshu schedule create --desktop --goal '整理新到的文件' \
  --watch-directory /absolute/inbox --readonly /absolute/inbox --workspace /absolute/output
tianshu schedule create --desktop --goal '执行已配置的工作' --on-event
tianshu schedule signal SCHEDULE_ID import:2026-11-21
```

- 日历使用指定时区的墙上时间。固定 UTC 偏移不需要系统时区数据库；IANA 名称需系统提供数据。夏令时跳过不存在的时间，重复时刻只取第一次。停机期间错过多个时刻会追赶一次。
- 监听目录必须被只读工作区覆盖，且不能和任何可写工作区重叠。每五秒检查一次直属条目，超过 2048 条暂停本次检查并记录异常。第一次扫描仅建立基线；随后一批新增普通文件创建一次 Run，文件名不会改写任务目标。目录身份变化需重新创建触发器；不观察子目录、只改内容的文件，以及两次扫描间出现又消失的文件。
- 事件通过已认证本机控制面接入。外部程序只传任务 ID 和事件 ID，不能用信号传目标、路径或授权；没有公网 webhook。每个启用任务里的重复事件返回原 Run。事件 ID 摘要、触发记账和入队同事务保存；删除调度定义会一并删除它的去重记录。
- 默认脱敏的诊断包只展示触发类别，不导出监听路径、文件基线或事件标识。
