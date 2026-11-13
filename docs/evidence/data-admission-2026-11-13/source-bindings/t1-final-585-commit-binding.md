# T1 最终正式提交：有限继承绑定

日期：2026-11-13。结论：`PASS_FINAL_585_COMMIT_BINDING_ONLY`。本绑定范围未决 0。

## 精确提交

已亲核线性链：

- 基础：`91290950099b1202beeb9b34cd2d71af7e945118`。
- T1 内容提交：`c145953e733533b7c9773d900af76a1dde2c7b37`，父为上述基础。
- 最终提交：`585e3474b7f9f03bb118308470fac1f500c3a02e`，父为上述 T1 内容提交。

开始与结束的 `git status --porcelain=v1 --untracked-files=all` 均为空；当前分支为 `codex/g3-data-admission`。本读者未写作者树。

## 已批准内容的继承

原内容批准 `independent-review.md` / `independent-evidence.json` 的 SHA256 仍为 `c28a14d1b6ed4aab7b5999bcc973c61c72f5095587c743280f753d6fce4c2594` / `35ca1e539818d598cb883484d4f5964248d7602c96fe890d3adec63d404be744`；正式归档与 `/tmp` 原件逐字节相同。

其批准引用的 `freeze-source-final.json` 摘要 `d6115cac7ba6152abe094b68f94c13a24e04f3da8d6092b56be273a8a9ca13b0` 精确保留。九项 T1 源码、测试与 fixture 在 c145 和 585 均逐 SHA256 命中该冻结清单。

恢复 fixture 的独立批准 `recovery-fixture-independent-review.md` / `recovery-fixture-independent-evidence.json` 原件及正式归档仍分别为 `9d053c14681070cc41bc2c432782be03c09713133823d8633c1c7b796fdd3b19` / `74cdd41e594a103a2f8559fa6fa57bb9de1b449181452b4992ebc1b293c38689`。585 中 `tests/test_recovery_atomicity.py` 为已审 `736d77c60f8f1d655867dab8bb7b0a7e85623613c182e8ca8d0402d6b46e7573`，与 c145 相同。

相对 912，非 docs 变更恰为上述九项加恢复 fixture，共十路径，无额外变更。其外路径的 Git blob 与执行位继承 912；c145 与 585 的全部非 docs Git blob 与执行位完全相同。产品 `src/tianshu/recovery.py` 仍为 912 原对象。

## 修后本机门禁映射

亲读并重算父既有 `/tmp/tianshu-g3-20261113/t1-main133-fixed-gates.json` 与三个日志的摘要；正式归档逐字节相同。门禁 JSON 摘要为 `8cfae6546db2708c9060bd2e3bf7c5b61edc5b88d94a8b3aacc55e892bdd14f5`。

- `uv lock --check`：exit 0。
- `uv run --frozen ruff check .`：exit 0。
- `uv run --frozen pytest`：exit 0；原日志结尾为 `2566 passed, 8 skipped in 435.78s (0:07:15)`，包装器耗时 436.08s。

264 项冻结 `before` / `after` 完全相同，`drift=[]`。逐一读取 585 的正式 Git 归档字节后，264 项 SHA256 全命中；它们覆盖该提交全部非 docs 受控文件，没有漏出的源码、测试、依赖、CI 或 UI 变更。详细的「路径 → SHA256 → commit blob / mode」映射保存在机器 JSON。这把父本机门禁绑定到 585 的确切非 docs 内容；本读者没有重跑 full 或既有探针。

## gzip 包装提交

585 相对 c145 恰有 20 个路径差异：九原件删除、九 `.gz` 包装新增、README 与 manifest 修改，全部位于本片证据目录。每个 gzip 的 mtime 均为 0；九份解压字节与 c145 中各自原始 blob 完全相同。manifest 的压缩 SHA256/bytes 和解压 `original_sha256`/`original_bytes` 均亲核匹配；将九条包装记录还原为原件记录后，整个 manifest 与 c145 版本相同。其余原件未改，36 条 manifest 记录覆盖目录全部原件；README 只追加 gzip 说明。

`git diff 91290950099b1202beeb9b34cd2d71af7e945118..585e3474b7f9f03bb118308470fac1f500c3a02e --check` 实际 exit 0，无输出。

## 文档事实与权限边界

亲读当前 `docs/DATA-ADMISSION-T1-2026-11-13.md`、`docs/KNOWLEDGE.md` 的 T1 变更和证据 README。当前本机 2566/8、435.78s、264 项无漂移与原件相符；旧 2542 全量、最终重复字段增强、main133 首轮四红和独立夹具正负控分别保留为历史，未用旧绿覆盖新组合。过程章节仍保留当时「待核/已启动」的阶段性句子；当前状态及独立的修后全量章节明确本机通过、原生 CI 待核。

未发现把本机或有限内容通过说成新原生 CI、Windows/ARM64/安装实测、T2/T3、恢复隔离、容量/GC、G3 或第一批全部完成的陈述。正式提交现已由本报告作精确继承绑定；此前文档保留的「最终 SHA 另绑」是本报告补足的证据边界。

本结论仅覆盖最终 `585e3474b7f9f03bb118308470fac1f500c3a02e` 的有限继承绑定，不授 T2 代码写权，不代替 root 亲读后的分段授权，也不授原生 CI 或整批验收。后续实质内容变化须重审受影响范围。

机器证据：`/tmp/t1-admission/final-585-commit-binding.json`。本次只运行静态 Git / hash / gzip / 既有日志检查，没有测试或产品代码执行。
