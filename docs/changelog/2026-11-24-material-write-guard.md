# 2026-11-24 · 材料写入 target 守卫（#204）

## 背景与根因

Tier 1 修复由真实负控驱动，共发现并关闭三类独立问题：

1. 私有 `.<db>.materials` / `.memory.materials` 是命名边界，不应被祖先中的 `artifacts`、`blobs`、`memory_blobs` 误判为标准材料域。此前异根 `data_root` 或自定义 `--db` 的上述组合会 fail-closed 拒写。
2. raw 路径显示为标准域、canonical 解析后却落在另一标准域时，不能继续沿用 raw 同根 `events.db` 的归属。标准材料现在强制 raw/canonical 同根同域；私有 target 则强制位于其 canonical 容器内。
3. 只校验目录 parent 会漏掉无需竞态的静态子目录链接；只校验“尚不存在”的 blob 会漏掉预置 leaf symlink。快照、撤销 journal、事件/内容/记忆 blob、执行/循环/子任务 spill 现在均守卫具体 target，并先守卫再判断是否已存在。

blob 仍先于事件事务或 manifest 引用持久化；拆分 lease 至多留下孤儿 blob，不提交虚假事件或材料引用。

## 验证

- `uv run --frozen pytest -o addopts="" -q -rs tests/test_restore_eventlog_access.py`
- 受影响回归：restore / backup / recovery / undo / events / content / memory / snapshot / dispatch / tasks / summary loop。
- `uv run --frozen pytest`
- `uv lock --check`
- `uv run --frozen ruff check .`
- `git diff --check`

最终本机全量 `pytest` 退出码为 0；托管 required CI 结果以关联 PR 的 Actions 日志为准，不用本机通过替代。

独立第二读者只读审阅完整 `restore_access.py`、全部生产调用点与完整回归文件，结论为 **PASS**；复审绑定 code/test tree 的 `git diff --binary` SHA256：

```text
00d58ddd286a295387e9e62bf05a7f5c8a7f4f50b25b4f5208bde546af7d7a98
```

新增的 snapshot preimage 负控预置真实逃逸文件，而不是悬空链接：在隔离工作树仅回退“先验 target 再判断 exists”这一处后，用例确定性失败于 `DID NOT RAISE RestoreScopeBusy`；最终候选同一用例通过，并断言保留区文件字节未变、manifest 未提交。

## PR #231 Windows shard0 跟进（2026-11-24）

- 真实失败不是放宽隔离要求的理由，而是测试夹具与 Windows 链接种类不一致：`_symlink_or_skip` 对所有链接固定 `target_is_directory=True`，snapshot preimage 负控因此创建了“目录种类、指向现有普通文件”的 leaf symlink。CPython 3.12 `ntpath.realpath(strict=False)` 的非严格错误白名单不含 `WinError 267`，在 readlink 回退前抛出 `NotADirectoryError`；原守卫只从 `resolve()` 结果看保留范围，随后被材料守卫包装为“无法解析目标”。
- 具体 target 先于 `exists()` 守卫的顺序保持不变。`ensure_path_not_held` 现在独立遍历 target/祖先的显式 `readlink` 目标（带循环保护），不再把可能失败的 `realpath` 当作唯一范围线索；若 canonicalize 仍不可用，材料写入继续 fail-closed。私有 `.<db>.materials` / `.memory.materials` 容器和标准域 raw/canonical 同根同域规则未放宽。
- 夹具按真实 target 种类创建链接。新增确定性 `WinError 267` 注入负控、现有文件 leaf symlink 的同根正控，以及 foreign-root / nonstandard 逃逸负控；本机 mac-arm64 定向 8 passed、完整 access 文件 71 passed、受影响 restore/snapshot/event/material 回归 770 passed / 1 skipped。未在本机运行 Windows，不能称 Windows 已验证；须由主代理在新 code/test tree 上重跑托管 Windows shard0 与全量门禁。
- Windows 跟进的独立第二读者结论为 **已修**，复核 code/test tree 的路径+实际字节 SHA256 为 `b8b434b7e9d8f581b2f1ceecd62b2b4ba93ea26c35a4b4e7ee1d8b79f362a279`。探针覆盖相对/绝对/链式/祖先链接、循环终止、`WinError 267` 与 readlink 失败拒绝、target 先于 `exists()`，以及同根正控和 foreign/nonstandard 负控。1200 层链接触发 `RecursionError` 的非阻断观察仍拒绝写入，未发现可达绕过；托管 Windows shard0 仍是独立平台门禁。
- Windows 跟进后的新 code/test tree 已在本机 mac-arm64 重跑完整 `uv run --frozen pytest`，退出码为 0；锁文件、Ruff、diff 与文档相对链接门禁通过。该结果不替代托管 Windows shard0。

## 残留边界

材料守卫核验的是 lease 获取时的具体 target。同 uid 仍可在检查后替换目录链接；本轮没有实现 fd-relative/openat 物理封闭，也不把 canonical containment 或本机测试通过表述为物理隔离。Windows 用户设备与最终托管 CI 状态须以对应 Actions 日志为准。
