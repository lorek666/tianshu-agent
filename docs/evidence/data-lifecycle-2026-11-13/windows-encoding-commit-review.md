# Windows 编码修正 · 提交级独立绑定

日期：2026-11-13。独立第二读者 `/root/verify_lifecycle`。

**结论：提交 `fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45` 通过本地限定范围的独立复核，无待修阻断；修正后的原生Windows仍待新CI。** 唯一父提交为 `f509a53881c372d8d7f4b1e2718130edaff4ed06`。

- 已核32项受审对象：原有产品/测试、十脚本、既有报告与JSON均继承冻结字节；唯一测试对象更新为已跑新hash `a9af99927d18d3035a719f4d325ed22c3d06e1bfd93943681ba0788d33e7f6ad`。三份新编码复核原件与独立侧原件逐字节相同。
- 提交只有8条路径变化；唯一非docs路径是`tests/test_backup_materials.py`。已按Git原件核实，变化精确等于为第111行`read_text`增加UTF-8参数；断言和产品未变。五个产品src、其余测试、`.gitignore`及原探针保持前轮对象。
- 246个门禁冻结对象与本提交Git对象逐项一致，运行前后无漂移；三个门禁exit0。直接读取原始log并核其SHA与归档输出相同，pytest结尾为 **2273 passed / 7 skipped，374.91s**。这是主代理执行、第二读者核原日志及对象继承的全量证据，不冒充第二读者亲跑全量。
- 亲跑的ASCII环境红绿及144项证据有效继承。新文档明确写为非UTF-8默认编码复现，并明确新原生Windows CI仍待运行，没有把ASCII复现写成Windows通过。旧Windows失败记录也保留为失败。
- `git diff --check f509a53881c372d8d7f4b1e2718130edaff4ed06 fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45`退出0。本轮没有再次机械运行144项或全量，没有修改仓库。

精确对象哈希、原件匹配、全量日志hash和门禁归属见同目录`windows-encoding-commit-binding.json`。后续若同步main或实质改变产品、测试、探针，须重新核对受影响组合；本结论不预填远端required状态。
