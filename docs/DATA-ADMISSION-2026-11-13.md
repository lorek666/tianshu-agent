# 数据准入与备份绑定迁移 · 集成验收记录

日期：2026-11-13。本记录为提交前本机冻结快照，汇总第一批的 T1/T2/T3 切片；“待验/待核”仅指该时点。正式完整 SHA、后续四 required 与合并事实以 `codex/g3-data-admission` 对应实现 PR 正文及后续独立原件为准，本记录不回写这些后续状态。这是源码研发切片，恢复隔离、实际恢复、容量保护与 GC 仍未交付。下列 T1、T2、T3 作者记录保留各自冻结时点，本次冻结组合状态以本记录和知识库为准。

## 实现范围

- T1：实际 EventLog 打开前检查真实数据库、版本旁表和关联进度；通过后才设置 WAL、执行 DDL 和创建 payload 目录。正常 v0 保持兼容，未知版本、损坏进度和迁移中间态拒绝。见 [T1 记录](DATA-ADMISSION-T1-2026-11-13.md)。
- T2：显式迁移取得合作式独占维护准入，使用同一条连接，绑定完整 typed 逻辑内容、FTS shadow、双向材料 inventory、manifest 原始字节、原迁移计划及机制起点。每步 DDL、版本与审计同事务；按真实提交窗口严格续做，完成后的 A/B 路径不改写进度或数据库。见 [T2 作者冻结记录](DATA-ADMISSION-T2-2026-11-13.md)。
- T3：新增 `tianshu data migrate --data-root ROOT --backup BACKUP [--json]`，原始 Path 交给已审迁移库。保留六类结构化错误、输出脱敏、编程异常边界和四个真实退出码 77。见 [T3 作者冻结记录](DATA-ADMISSION-T3-2026-11-13.md)及[维护指南](backup-restore.md)。

契约由独立 PR #135 合入，本文不改变契约。[数据准入契约](data-admission-contract.md) SHA256 为 `c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`；[最终计划](evidence/data-admission-plan-2026-11-13/admission-implementation-plan.md) SHA256 为 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875`。

## 独立第二读者的结论

T1 内容、恢复测试 fixture 顺序适配及正式 `585e3474b7f9f03bb118308470fac1f500c3a02e` 的[对象绑定](evidence/data-admission-2026-11-13/source-bindings/t1-final-585-commit-binding.md)已分别通过，原行为证据在 [T1 目录](evidence/data-admission-t1-2026-11-13/README.md)。历史全量首轮的四项恢复夹具失败及仅修夹具顺序后的结果均保留；未放宽产品拒绝条件。

T2 结论为 `PASS_TASK2_CONTENT_ONLY`，部分修、未修、本片未决均为 0：独立 81 项、关联 298 项、额外两项固定时钟/双旁文件 no-op 通过。12 个隔离变异中 11 个被真实行为或不变性断言检出；M05 仍被保留的机制摘要/起始版本守卫拒绝，明确不记作 kill。见 [正式报告](evidence/data-admission-t2-2026-11-13/independent/independent-review.md)。

T3 结论为 `PASS_TASK3_CONTENT_ONLY`，部分修、未修、本片未决均为 0：独立 35 passed、0 skipped（19.03s），两项窄 CLI 变异各被一项真实行为断言检出。作者 56 项原始输出逐字节核对；不是额外执行的独立 56 项。首轮独立探针 30 passed / 5 failed 来自观察器生成空 WAL/SHM 的顺序，修正探针观察顺序后通过，主候选未变；首红和修正 patch 保留，不记为产品修复。见 [正式报告](evidence/data-admission-t3-2026-11-13/independent/independent-review.md)。

这些结论只覆盖各自精确内容；不能代替下述最终组合门禁、正式提交或原生 CI。

## 最终组合与门禁

集成起点为 Git `585e3474b7f9f03bb118308470fac1f500c3a02e`，加 T2/T3 冻结 50 路径，再普通 merge 同步实际 main `0df7019aeed4f9116262e38f86561d5eff06d0e4`。上游相对共同祖先 `9129095` 仅四份文档变化；知识库冲突按双方原文合并。T1 九项和恢复 fixture、T2 四项、T3 三项的已审对象保持精确继承。父会话另修 README 过期的“迁移启动门未交付”说明并补新命令。

最终组合在 macOS arm64/Python 3.12.13 的 `uv lock --check`、`uv run --frozen pytest`、`uv run --frozen ruff check .` 均 exit 0；全量 **2672 passed / 8 skipped，433.15s**。运行前后 267 个非 `docs/` 对象逐哈希一致。仅对子进程局部移除本机 `TIANSHU_*` 与以 `BASE_URL`/`API_KEY` 结尾的配置，不打印值或修改用户环境；变量名、命令、原始输出及冻结清单见 [本机结果](evidence/data-admission-2026-11-13/final-local/results.json)与[归档索引](evidence/data-admission-2026-11-13/final-local/archive-manifest.json)。这是 HEAD585 加已冻结补丁和 main0df 的未提交组合结果，正式 SHA 按对象另绑，未宣称提交后重跑。新实现 PR 的原生 CI 仍待验收。未改 UI 对象，既有 80 项 UI 证据按精确对象继承，本机没有机械重跑 UI。

上游 main0df 自身首次 push CI 的 [独立报告](evidence/data-admission-2026-11-13/upstream-main-0df/main-0df-ci-independent-review.md)仅作为上游历史：run `43416712660` 四 required 成功，Ubuntu 2434/13、macOS 2439/8、Windows 2410/37，UI 80；不将它用于替代本组合检查。

## 仍未交付和证据边界

0→2 只代表现有两步迁移机制，不代表所有业务表、authority 或凭据格式已冻结。维护锁约束合作程序，不抵抗旧版程序、raw SQLite 或同 uid 恶意写入。普通连接保留既定 legacy 兼容范围；显式迁移只接受标准根布局。

真实 restore apply、恢复 hold/journal、记忆隔离、低磁盘保护、容量保留与 GC 均不在本切片。v2 备份未保存迁移进度，不能复制或补造旧进度绕过恢复后的准入。另一轮只读 debug 已确认的 Receipt 回滚材料、snapshot/undo 语义、未来 payload/DB 版本、投影缺行及 manifest 类型等备份/预演边界须在后续恢复或 GC 前收口；本片没有修改 backup/旧 restore-plan，也没有据此宣称全部备份语义已修。

本机是 macOS arm64/Python 3.12。GitHub Windows runner、未验证的 Windows 用户设备和 ARM64 安装体验分别记录；本机与 runner 的结果都不扩写为 Windows 10/11 设备支持。第二至第四批仍按原顺序待实施。

## 原件保存

T2/T3 独立报告、索引、原始探针/日志/变异 patch 均按原字节保存于各自 `independent/`，压缩包由 `public-archive-index.json` 逐成员绑定。合成 case 数据库与材料只在被忽略的 `.omc/artifacts/` 保全，公开索引保留其哈希；不提交运行时用户数据。首轮失败、夹具修正、未检出的 M05 和平台 skip 边界均保留。

来源准备的原四项（含 `prepare-source-binding.py`）以原文件名封装在 [preparation-originals.tar.gz](evidence/data-admission-2026-11-13/source-bindings/preparation-originals.tar.gz)，逐成员摘要及原索引映射见 [preparation-archive-mapping.json](evidence/data-admission-2026-11-13/source-bindings/preparation-archive-mapping.json)。原准备报告与索引保持原字节，脚本作为证据保存，未执行。
