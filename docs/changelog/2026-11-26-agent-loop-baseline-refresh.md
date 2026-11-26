# Agent Loop 实现栈基线刷新（2026-11-26）

本次把 PR #225 从旧契约基线正常合并到刷新后的 PR #223 契约
`dfed838c37c5a6b4220039017459a99e7bbe1c6a`，保留 Agent Loop / Goal V2 实现，同时接收
主干已合入的 prompt cache、辅助摘要遥测与 Windows 分片校准。未改授权、租约、事件日志、
计费或恢复语义，未直接推送 main，也未合并 PR。

## 冲突决策

- **Windows 权重不能二选一**：按文件取原契约基线与实现分支测得权重的较大值，保留 Goal /
  online-soak 与 prompt-cache / summary evaluation 的全部键和两组平衡测试；严格收集与分片
  验证逻辑不变。现有 `scripts/pytest_ci_shards.py` 头部的实测运行身份继续保留。
- **RU-D1 统一为 `1.1.0`**：保留纯读 Mission 的实际模型提案与 `tool.not_dispatched` 路由
  拒绝证据，同时继承 `1.0.1` 的固定子任务 usage 和真实父子预算结算。历史 `1.0.1` 记录仍是
  历史事实，不倒改成新版本。
- **摘要硬退测试修正测试夹具路径**：Goal V2 的子进程把事件库写入临时 `data/events.db`，
  旧测试却打开临时根下的 `events.db`，因此读到新建空库。只修正测试读取路径，不删除断言，
  也不改摘要遥测产品或崩溃注入语义。
- **知识库合并而非覆盖**：同时保留 prompt-cache / 遥测边界和 Mission / MCP / Agent Loop /
  Goal G1 / 本地 Skill / 进程隔离限制，没有用一侧文档覆盖另一侧已知限制。

## 冻结对象与验证

合并父提交：

- 原实现头：`ce89a7a9b2865abd3f2a47db10c1f46f98e8b7db`
- 刷新契约头：`dfed838c37c5a6b4220039017459a99e7bbe1c6a`

非文档代码/测试树覆盖 `src/`、`tests/`、`scripts/`、`schemas/` 中全部受 Git 跟踪的实际
文件。按仓库相对 UTF-8 路径全局排序，对每个文件依次写入
`UTF8(path) + NUL + 实际文件字节 + NUL`，SHA256 为：

`cdc04ecba99dda2cb5a7cc6bc46b4ef494c6e4a05b948185db551951d0304119`

独立第二读者 `pr225-final-merge-reviewer` 对该树完成探针复审，结论为 **已修**，无未解决
发现。复审覆盖摘要遥测的发送前身份、用量回执、预算结算、异常回滚和路由证据绑定；未发现
嵌套事务、预算绕过或事件缺口。作者随后在 `/tmp` 私有副本中从冻结字节复现两项有效变异：
移除 Mission 路由拒绝事实后，`tests/test_eval_real_tasks.py:713` 的真实断言变红；恢复旧的
事件库路径后，`tests/test_summary_cache_telemetry.py:130` 的真实断言变红。两项恢复后目标
回归 exit 0，私有副本与复审树的逐文件字节比较及全局排序哈希均一致。变异只发生在私有副本，
没有改动作者工作树或测试断言。

定向回归、`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 均 exit 0。
作者工作树的隔离全量 `uv run --frozen pytest -o addopts='' -q -rs` exit 0；通过项、跳过原因与
精确耗时以 `/tmp/pr225-full-pytest.log` 原始日志为准，不在本文硬编码静态总数。该全量日志
SHA256 为 `19b70b513ad8992cc46322d691a0d6bc43a2abb819cbda6318270171ce1dc14b`。

原始定向、变异和恢复日志保存在 `/tmp/pr225-parent-replay-*.log`；这些本地证据不是发行
资产。新提交的 hosted required CI 必须绑定推送后的精确 head，不能继承本地全量、旧 #225
绿色或刷新契约 PR 的 CI 结论。原生 App、真实模型和发行验收仍不在本次范围内。
