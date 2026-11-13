# 数据准入历史夹具

SQL由Git对象 `30b973dfe21ce71f40ccfd7e84412de33d23877a:src/tianshu/events.py` 的 `_SCHEMA` 原文提取；legacy-v2另用同对象migrate.py的两项历史SQL及固定审计行。状态JSON按该对象MigrationState.to_dict及_require_backup的真实format1字段固定保存。未调用新初始化器或迁移器。

v0包含一条没有单行event_versions绑定的合法旧run.start；legacy-v2保留相同事件，增加历史两步机制对象。JSON旧backup路径为公开合成值，不依赖真实文件存在，不构成续做证据。
