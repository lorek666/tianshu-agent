# 数据生命周期首片 · 独立复现材料

独立第二读者为 `/root/verify_lifecycle`。这里保存其实际运行的十份脚本（环境包装器加九个探针）、五份独立运行 JSON 及三份提交级绑定/继承/README 复跑 JSON，逐字节复制后核对 SHA256；没有保存临时数据库、用户资料或诊断日志。主代理的全量证据另记于 `local-gates.json`。范围与边界见 [本轮验收记录](../../DATA-LIFECYCLE-2026-11-13.md)。

从仓库根执行，例如：

```sh
python3 docs/evidence/data-lifecycle-2026-11-13/run_clean.py \
  uv run --frozen python \
  docs/evidence/data-lifecycle-2026-11-13/probe_locks.py
```

将最后的脚本名依次替换为下列名称，可复跑全部九个独立探针：

- `probe_locks.py`：真子进程共享共存、独占拒绝、硬退释放、构造失败、真实连接存活与根替换。
- `probe_cleanup_fixed.py`：仍登记的 content 与未擦除 memory 受保护，坏引用拒绝。
- `probe_readonly_edges_fixed.py`：静态 WAL 库校验不落旁文件；缺 runs 投影的预演明确失败。
- `probe_materials_cli.py`：真实 CLI 全材料备份、删源后只读预演、篡改清单不能隐藏缺材料、崩溃中断。
- `probe_live_wal.py`：在线备份包含尚未 checkpoint 的已提交事务。
- `probe_wal_sidecars.py`：保存备份拒绝 WAL/SHM/journal 旁文件，即使已列入清单。
- `probe_lock_permissions_fixed.py`：数据库目录可写而上一级只读时仍能使用；与固定基线对照。
- `probe_lock_identity_env.py`：变更 HOME/TMPDIR/TIANSHU_DATA_ROOT 不改变身份，独占期间真子进程均被拒绝。
- `probe_source_alias_fixed.py`：同根别名、材料后缀、删源后不回读、旧格式兼容与反例。

脚本仅在系统临时目录创建合成数据。`run_clean.py` 仅为测试子进程移除 `TIANSHU_*`、以 `BASE_URL` / `API_KEY` 结尾的变量，不打印变量值，不修改全局环境；产品与探针实际通过 `uv run --frozen` 使用项目 Python 3.12。它在证据中的外层解释器版本不代表产品运行版本。

本包记录的运行平台是 macOS arm64。权限探针需要 POSIX 权限语义；符号链接探针需要宿主允许创建链接；不要将其结果说成 Windows 原生验证。权限探针固定读取基线提交 `ccff93e79cfeb93e47e5a35012fd92fae366e941` 的 `events.py`，浅克隆缺少该对象时须先获取该提交，不能把取不到基线当成产品失败或通过。

`final-bundle-manifest.json` 绑定基线、被审产品/测试与脚本 SHA256。其余运行 JSON 分别保存九探针命令与关键输出、格式检查、144 项定向测试和最终被审文件哈希。`candidate-commit-binding.json` / `candidate-freeze-inheritance.json` / `archived-readme-smoke.json` 记录产品提交的 Git 对象、全量后仅文档/JSON 差异和第二读者对归档说明的亲跑。输出里的 `/tmp/...` 与系统临时路径是当次现场，不要求复跑生成相同临时名。完整提交结论见 [独立复审报告](../../REVIEW-DATA-LIFECYCLE-2026-11-13.md)；该报告与全量、CI 记录各自区分，不能相互替代。

`windows-encoding-review.md`、`windows-encoding-evidence.json` 和
`windows-encoding-scope.json` 是首次 CI 唯一 Windows 失败的独立复核原件。
它们记录原始 cp1252 异常，以及携带父提交原 conftest 的 ASCII 环境红绿和 144 项回归；
这三件文件逐字节归档，不改写既有产品复审。复核对象仅为测试读取显式指定 UTF-8 的一行，
不宣称新 Windows CI 已通过。后续完整提交与组合验证另行绑定。

`windows-encoding-commit-review.md` / `windows-encoding-commit-binding.json` 将一行修正及
原件绑定到 `fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45`。
`main132-integration-independent-review.md` / JSON 记录与 main a839906 的内容继承；
`main132-integration-local-gates.json` 保存该组合 2416/8 的本机输出、253 个冻结对象及
UI 80 项/构建产物一致证据。内容继承、提交对象、作者全量与新原生 CI 分别解释。

`main132-integration-commit-review.md` / `main132-integration-commit-binding.json` 是第二读者
对 `16721ff14ad08456e8f6a2522c3300b2652b0918` 的正式提交绑定；只读重核脚本另以
`main132-integration-commit-verify.py.txt` 保全。旧候选报告的 19/34 是包含相邻索引的段落数，
真正 §8 日志为 15/30 条，均保留。README 新增行的 9/8 与正式 JSON 的 7/8 使用不同过滤，
详见 `main132-readme-count-clarification.md`；它们不代表内容丢失，旧原件保持原字节。
本轮未重跑产品测试，不为后续 #133 组合或新原生 CI 提供结论。
