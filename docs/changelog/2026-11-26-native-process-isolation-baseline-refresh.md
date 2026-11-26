# 原生进程隔离 PR 基线刷新（2026-11-26）

本次通过正常双父合并，把旧 #228 原生进程隔离实现刷新到 Agent Loop / Goal V2 栈的
`28dc2b9567131af5015e71ecd6248b055da0c076`，保留旧 #228 的授权、租约、事件、执行隔离
与 Host/Daemon 生命周期语义，同时接收 prompt cache、辅助摘要遥测、Mission / Goal 和
Windows 分片校准。未 rebase 已共享历史，未直推 main，未合并 PR，也未发布。

## 冲突决策与边界

- **Windows 权重取两侧并集较大值**：两侧原有键全部保留且无降低，merge 新增 cache /
  summary / evaluation 键。`tests/test_isolation.py` 从两侧共同的 40 秒保守上调到 47 秒，
  使两组原有平衡断言继续成立；没有降低权重、放宽阈值或删除校准测试。
- **严格分片 verifier 不变**：macOS 保持 count2，Windows 保持两台 runner × 每台两个真实
  worker / count4；完整 collection、partition、身份、attempt、pytest 退出码和上游 success
  校验全部保留。
- **CI workflow 原样保留**：`.github/workflows/ci.yml` 与旧 #228 blob 完全一致，未采用
  其他分支冲突的拓扑。
- **高危源码逐文件保全**：`daemon.py`、`exec_backends.py`、`exec_seatbelt.py`、
  `execution_admission.py`、`host.py`、`isolation.py`、`leases.py` 与旧 #228 blob 完全一致。
- **知识库合并而非覆盖**：同时保留缓存/遥测边界与 Mission / MCP / Agent Loop / Goal /
  本地 Skill / 进程隔离限制。

## 冻结对象

合并父提交：

- 旧 #228：`20e256a06f56ad70bf32159bf3548ca399523597`
- 刷新 #227：`28dc2b9567131af5015e71ecd6248b055da0c076`
- merge commit：`5a30a2e3109c91301bbc194cb8f378378b4efc62`
- Git tree：`84ea732c018d40af4d4daf064b4a65a9149a5e4e`

规范树覆盖 Git 跟踪的 `src/`、`tests/`、`scripts/` 实际文件；复审树另加入
`.github/workflows/ci.yml`。按仓库相对 UTF-8 路径全局排序，每文件写入
`path bytes + NUL + raw SHA256(file bytes) + NUL`：

- 规范树（392 文件）：`fd3bef6e232920a07014b82b6a472774408e0ddf83686b4f0f4b922a622e9aeb`
- 复审树（393 文件）：`b5148ecdf46eae73a921f0f946d69fb00486086fb74daa883ec03bd568428440`

## 独立复审与有效变异

独立第二读者 `pr228-refresh-reviewer` 对上述复审树完成 Tier 1 探针复审，结论为 **已修**，
未发现 P0/P1/P2。复审报告 SHA256 为
`5f5cf54f6cf52be501a3fe21dc53f58dc9cdaf65957d3a54e304bbba06a8beb2`，探针清单 SHA256 为
`80f60d35a8538ba3ea792f9dfc330fcb54fdc6aa767ed66266c309ba75e3905b`。

作者在 `/tmp` 私有副本重放：基线两项校准通过；把 `test_isolation.py` 权重从 47 改回 40
后，原 medium-file 与 complete-leaf 平衡断言均真实失败；恢复 47 后再次通过。变异未改动
作者工作树或产品断言，恢复树与复审树逐文件一致。原始证据见
`/tmp/pr228-parent-replay-evidence.json`。

## 本地验证

- 定向 CI/分片/隔离域回归 exit 0；独立复审 scoped green 为 `178 passed in 47.04s`。
- `uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 和
  `actionlint .github/workflows/ci.yml` 均 exit 0。
- 第一次隔离全量运行因系统盘可用空间低于产品 2 GiB 保护线而出现 7 个 M5 启动失败，
  全部为 `dispatch.disk_space_low`，不是断言、授权或隔离回归。该红色日志保留为
  `/tmp/pr228-full-pytest-disk-gate-attempt1.log`，SHA256
  `ee5b50d46156b5d908e0a04074a80c9301e9c95786bc24c216c72db78ba112d0`。
- 不降低磁盘门禁，使用同一冻结代码，把 `TMPDIR`、`TIANSHU_TEST_TMP_ROOT`、锁根、注册表
  与 authority 根全部隔离到数据卷 `/Volumes/Date/tianshu-pytest-pr228-rerun.lAdEmh` 后，
  无筛选 `uv run --frozen pytest -o addopts='' -q -rs` exit 0。原始日志
  `/tmp/pr228-full-pytest.log` SHA256
  `15c21dad9df0afcf460119671b575f2d1ff9a01c8afefb0b12630506f8f55449`；通过项、跳过原因
  与精确耗时以该日志为准，本文不硬编码静态总数。本机 macOS 结果不替代原生 Windows CI。

本文件为纯文档回填。回填后实测规范树仍为
`fd3bef6e232920a07014b82b6a472774408e0ddf83686b4f0f4b922a622e9aeb`，复审树仍为
`b5148ecdf46eae73a921f0f946d69fb00486086fb74daa883ec03bd568428440`；最终静态门禁日志
SHA256 为 `a68bc97206ac28871728fd5263bbe7b805df2b796e713bf20200b2fd0919d3ef`。新 head 的
hosted required CI 必须绑定推送后的精确 head；旧 #228、#225、#227 的绿色不继承为本轮证明。

## 未决边界

#224 的 Windows 完整进程树监管、原生 sandbox 与任意后代监督仍未交付。macOS leaf 后端的
`process_tree=enforced` 仅表示 deny-fork 加确认根进程退出，不是通用物理隔离。真实模型、
用户设备、签名、四目标发行和预览验收均不在本次范围内。
