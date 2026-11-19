# Restore C · R2 重入校验

旧版 `restore-release` 重入时只比较部分尾事件字段和租约状态。临时夹具在 R2 已提交后改变一项授权剩余次数、预算预占计数或历史／尾部事件版本，旧实现仍会写入 `ReleasedV1`。

R2 从匹配 `BindingV1` 字节与事件头的主库开始，比较历史事件和版本行的前缀摘要，并核验新增事件的每个字段和版本。在同一 SQLite 事务内写入绑定到本批 `release_intent` 事实的单行私有见证。摘要覆盖主库持久表的 SQLite 存储类型和值、行标识及结构定义，包括 R2 所有投影、事件版本和 FTS 影子表。[见证实现](../../src/tianshu/_restore_release_proof.py)

重入先核验见证和当前快照，再执行检查点与登记阻断。已偏离原绑定的主库如缺少当前 R1 的有效见证，仍保持隔离。R3 在注册表 CAS 前重读主库见证并核对检查点后持久化的字节摘要。`ReleasedV1` 注册表事实仍承担恢复后准入证明。合法备份可携带旧见证，新的 R2 仅在匹配新批次 `BindingV1` 时替换它。[解除流程](../../src/tianshu/restore_manager.py)

定向回归用真实 CLI 子进程覆盖 SQLite 事务提交前、提交后、检查点后和注册表提交窗口。第三状态用例核对普通打开仍被拒、尾事件不重复，并比较拒绝前后的主库、注册表和权威阻断文件。兼容用例覆盖已解除根的正常写入、再备份、独立 Home 原路径恢复、v1 显式迁移、v2 准入与 GC。[Restore C 回归](../../tests/test_restore_c_release.py) · [释放后 GC](../../tests/test_restore_bc_review_regressions.py)

本地验证使用 `uv lock --check`、`uv run --frozen ruff check .` 及 `uv run --frozen pytest -o addopts= -q -rs` 定向运行 Restore B/C、备份、迁移与 GC 模块。验证覆盖 SQLite 逻辑状态和进程硬退。独立复审、最终集成树全量测试及 required CI 结论随最终候选记录。
