# T1 实际数据库准入证据

本目录保全作者交接和独立第二读者的原件；各文件字节摘要见
`archive-manifest.json`。原件中 `/tmp/t1-admission/` 路径是当时执行位置，
本目录以相同文件名保存对应内容；原 `.log` 和探针 `.py` 文件仅添加 `.txt` 扩展名，字节不变。负控中的失败是有意移除守卫后的真实结果，
不能计入候选的成功测试数。

`independent-review.md` 与 `independent-evidence.json` 给出
`PASS_T1_CONTENT_ONLY`，绑定最终九项源码、测试和夹具摘要。
第二读者亲跑最终新增127项、既有针对回归158项和41项独立合成检查；
四种产品守卫移除及最终重复字段守卫移除均产生真实失败。
早期重复字段测试在移除守卫后仍通过的原件另存，未覆盖或当作成功证据。

`full-final-clean.log.txt` 是作者在旧基线30b上、最终单测试增强前的
2542 passed / 8 skipped；`full-final-clean-boundary.json` 记录该版本边界。
该全量不覆盖最终测试，也不覆盖后来继承的main #133。
`full-first.log.txt` 与 `full-failure-controls.log.txt` 保留首轮失败和配置隔离对照。
子进程仅去除本机模型端点配置，不输出配置值；未改变产品或旧价格测试。

`handoff-manifest.json` 是正式交权时的11文件快照。
后续整合只更新事实文档；最终九项已审输入保持相同。
`admission-contract-main133-inheritance.json` 是912分支的有限继承核对，
不是T1产品或CI验收。

最终新组合全量和提交绑定另行记录。这里的有限内容结论不覆盖T2/T3、
迁移CLI、恢复隔离、容量/GC，也不代表第一批或G3交付完成。


部分原始pytest失败日志自带行尾空格。为同时保留原始字节与正常diff检查，
这些文件以确定性gzip（mtime=0）保存为 `.txt.gz`，不修剪输出。
`gzip -dc FILE.txt.gz` 或Python标准库 `gzip.decompress` 可恢复完整原件；
manifest同时记录压缩文件摘要和解压后的原件摘要。其余原件不变。
