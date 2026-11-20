# 2026-11-19 · 恢复 B07 后续备份顺序校验

`data restore-review --later-backup` 按受管恢复 B §6 校验后续输入。复核从 Binding 已封存的 `backup.json` 原字节读取恢复点，沿用备份完整性与源路径校验，并要求候选清单的 `created_at_ms` 严格递增、已校验的 `event_seq_head` 不倒退。较晚生成且事件序号相同的备份仍可参与比对。校验未通过时，CLI 退出 `2`，返回 `review_complete=false` 的部分复核及失败原因，`known_later_changes` 为空。[受管恢复 B 契约](../restore-held-contract.md)

通过校验的可选差异仍使用现有 `review_sha256` 规则，摘要绑定静态隔离根的脱敏复核。清单创建时间来自生成备份时的系统墙钟，规范化源路径和事件序号只用于复核输入筛选。后续遗忘和删除历史保留 `completeness_unproven`。

定向用例经真实备份、校验与 `published_hold` 恢复，覆盖旧时间、同时间、时间较新但事件序号倒退，以及事件序号递增和持平两种较新备份。原备份移出后仍可用已封存清单判定顺序。既有 CLI 用例继续核验遗忘差异、脱敏及 `restore-release --expect-review` 摘要兼容性。[B07 用例](../../tests/test_restore_b_execution.py) · [CLI 回归](../../tests/test_restore_bc_review_regressions.py)
