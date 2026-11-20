# 2026-11-19 · 恢复 B03 标准事件库的历史证据准入

恢复 B/C 的材料路径已经按 `overlap_key` 识别标准库名。空主库准入和旧版迁移旁证发现仍使用字面比较，导致在大小写不敏感的测试卷上用 `EVENTS.DB` 打开时遗漏历史证据。缺失与零字节主库都会初始化出空事件表。已有 v2 库使用该拼写时也会漏读有效的旧完成记录。

标准材料布局、空库证据检查和旧版旁证发现共用标准库名判据。已有库使用别名时，旧完成记录须通过普通文件的实体一致性核对。记录发现依赖路径比较，完成记录的归属依赖实际文件。这项只读核对运行在现有 `EventLog` 准入流程内，v2 旁证继续使用实际路径严格绑定。详情见 [data-admission-contract.md §2](../data-admission-contract.md#2-正常连接的准入顺序) 与 [test_data_admission.py](../../tests/test_data_admission.py)。

在大小写不敏感的 macOS `/Volumes/Date` 卷上，通过 `samefile` 核验了测试夹具的别名。实际 `EventLog` 红绿覆盖缺失及零字节主库、`artifacts` 与 `migration.json`、有效的旧完成记录。附加判据覆盖词法比较相同但实体不同的真实文件，保留 `events.db`、自定义库及严格 v2 进度校验的正负控。两个真实大小写拼写可并存的场景由大小写敏感的测试卷执行。

运行 `uv run --frozen pytest tests/test_data_admission.py`、受影响的数据准入、迁移、恢复 B/C、W15 测试、`uv lock --check` 与 `uv run --frozen ruff check .`。最终集成提交的全量结果、第二读者复审和跨平台状态以各自绑定的日志为准。
