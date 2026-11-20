# 2026-11-19 · 已发布 restore registry 的 SQLite 日志格式校验

SQLite 将 WAL 主库干净关闭后，目录可以只剩 `registry.db`，其文件头仍为 `02/02`。旧实现会接受该主库，SQLite 的 `mode=ro` 连接可创建 `registry.db-wal` 和 `registry.db-shm`。这违反固定权威闭锁与零写观察要求。见 [B 契约 §§3、6](../restore-held-contract.md) 与 [C 契约 §2.2](../restore-release-contract.md)。

- `observe()`、`revalidate()` 与 `append_event()` 持有 namespace/entity 锁后检查已发布权威的目录和 SQLite 文件头。目录须仅含 `registry.db`，头部读写格式版本须为 `01/01`。SQLite 连接继续执行现有 schema、meta 和权威验证。协作写者持有独占锁时优先报告 Busy。
- POSIX 通过主库句柄池读取文件头，SQLite 连接关完才释放额外描述符，避免影响同进程活跃读者的字节锁。首次建库使用私有空锚点初始化为 DELETE 日志模式，发布后走同一检查。

## 验证

- 定向测试用真实 SQLite 将原注册库设为 WAL 并关闭，旧候选的观察、重核与授权追加均错误接受。修复后在 `sqlite3.connect()` 前拒绝，主库字节和目录内容保持原样。
- 子进程持有实写事务与维护独占锁，观察在活跃回滚日志存在时报告 Busy。另一个本机 POSIX 探针验证同进程重核结束后，活跃观察的 SQLite 锁仍使子进程得到 `SQLITE_BUSY`。
- macOS 本机检查使用 `uv lock --check`、`uv run --frozen ruff check .`、`uv run --frozen pytest tests/test_restore*.py tests/test_data_admission.py tests/test_backup.py tests/test_backup_materials.py`。完整本机回归和独立复审的结论以 PR 所绑定的代码树为准，原生平台门禁以该 PR 的 required CI 日志为准。
