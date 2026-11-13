# PR #134 同步 main #131/#132 · 独立提交绑定

日期：2026-11-13。第二读者 `/root/verify_lifecycle`。仓库只读，报告写入 `/tmp`。

**提交对象、父内容继承、原件归档与作者本机门禁绑定通过。本结论不批准合并，不表示新原生 Windows/四项 required CI 已通过，也不覆盖后续 main #133 组合。**

核对提交：`16721ff14ad08456e8f6a2522c3300b2652b0918`。父提交按顺序为 `fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45` 与 `a8399062dd87675cc10b7c258ab4ce3679b53613`；共同基线为 `ccff93e79cfeb93e47e5a35012fd92fae366e941`。当前工作树已在契约分支，本次只读该完整 SHA 的 Git 对象，没有把当前分支内容当作受审组合。

除根 README/知识库两份组合文档外，候选清单 353 条路径中，351 条精确匹配原候选及三方归属所选的父对象；其中 1 条为旧 UI JS 资产删除。另 2 条仅为 `docs/DATA-LIFECYCLE-2026-11-13.md` 与证据 README 增补门禁记录，原正文按行保持。5 个产品 src、4 个目标测试与 `.gitignore` 共 10 项精确继承 fd5；上游 src/scripts/tests/UI/CI 按 a839 继承，原生命周期报告、探针及 JSON 共 24 项精确继承 fd5。没有第三种产品/测试内容。

根 README 与原候选 SHA256 相同。知识库保留两父新增内容及全部历史，澄清标题“G3 首片吸收 main 已合修复”已存在于实际提交。旧候选报告把 §8 后的格式提示及 §9 索引等相邻材料计入“19/34 历史段落”；该旧统计口径不准确，实际 §8 日志条目为 15/30 条。本次重新核实这些条目以及旧口径的全部段落均逐字保留，内容继承结论不变；四份原件不改写。

作者 `/root` 执行的本机门禁：lock/ruff/full pytest 均退出 0；原日志为 **2416 passed / 8 skipped，425.09s**。第二读者独立读取原始日志与元数据，确认归档输出与 SHA256 全相同，freeze/stable 的 253 个非文档文件与提交对象逐项一致，前后 drift=[]。该全量不是第二读者亲跑，不能写成独立全量复跑通过。

作者 UI install/typecheck/test/build 元数据均为退出 0；第二读者亲读原日志，其中测试为 **80 passed / 11 files**，typecheck/build 输出成功。3 个 dist 文件运行前后 hash 相同，并精确对应目标提交与 a839 父对象；没有独立复跑 UI。

四份归档原件与 `/tmp` 原件逐字节相同，SHA256 为：

- `main132-integration-independent-review.json`：`e6ad126592b5b22605b89e348ed7206bf826d4d11afa06c609830527a9dfc99f`。
- `main132-integration-independent-review.md`：`00d9f272230bc46353f5dec906691882d669bf61559b40d3a563768a0bae4a7f`。
- `windows-encoding-commit-binding.json`：`f969fcafeab258c0b4a9ad86a6102aa85da9a1d0a83f06968f236416b63770b9`。
- `windows-encoding-commit-review.md`：`40cf78c1b1f96554273382c59e91fd4f11f98882300a9a8eb08754c41b6fe56d`。

第二读者本轮亲执行了两父到目标的 `git diff --check`（均退出 0）、逐项 Git blob/mode/SHA256 比较、原始日志/元数据对账、文档保留和冲突标记检查。没有重跑此前生命周期探针/144 项、全量或原生 Windows；既有独立探针保持原先提交与平台归属。

边界继续成立：维护锁是协作锁；低层 backup 仍保留在线语义；restore apply、后续删除历史完整性保证与实际设备/Windows 10/11/ARM64 体验不在本片已交付范围。任一后续实质变动须绑定新对象并复核受影响范围；本组合的新原生 Windows 与全部 required CI 仍待另行核验。

逐项证据：`/tmp/main132-integration-commit-binding.json`，SHA256 `c8cb36aadb413891af28db368e84182c78da719f93e5c485ff36445292e5811a`。只读重核脚本：`/tmp/main132-integration-commit-verify.py`。
