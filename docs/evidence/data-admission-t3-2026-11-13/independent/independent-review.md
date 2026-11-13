# T3 独立实现验收

结论：`PASS_TASK3_CONTENT_ONLY`。本片产品缺陷、部分修、未修与未决均为 0；此结论只覆盖下述精确冻结内容，不代替正式提交、最终组合全量或原生 CI。

## 精确候选

- Git 基线：`585e3474b7f9f03bb118308470fac1f500c3a02e`。
- 作者最终 T3 交权原件 SHA256：`79f5287dcf3719816649ffd49386e6087e04afb7f3e3810735d84164b7c828ee`，26 项全部匹配。
- 独立候选：`/tmp/t3-admission-independent/candidate-frozen`；完整 485 文件绑定为 `candidate-binding.json`，SHA256 `8ee219c4ec1304d48fab4d2dd24f8c7d7abb14dfa7d6ac345092a3a2e1f25fdd`。
- 从真实 Git archive 的 441 个基线文件重建，再叠加 T2 25 项和 T3 26 项（知识库由 T3 覆盖），相对基线准确 50 路径、7 非文档路径。435 个未覆盖基线文件及 T2 非知识库 24 项逐摘要继承；T2 的两个产品与两份测试精确不变。
- T3 唯一产品文件 `src/tianshu/data_cli.py` SHA256 `4f3ce8b6b35f484354878a91186298196023c970bd1a26275f544a993d167734`。测试摘要与完整逐文件表见 JSON 报告及候选绑定。

## 独立行为证据

修正探针观察顺序后，同一精确候选 **35 passed / 0 failed / 0 skipped，19.03s**；实际环境为 Darwin 25.6.0、arm64、Python 3.12.13。命令、逐例输出、真实子命令 stdout/stderr 与 JUnit 位于 `final-frozen-run/`。没有重跑 T1/T2/full/CI。

1. 六 code 分别来自真实未来版本、真实合法 v2 缺进度、坏 JSON、备份后真实追加、另一个进程的 EventLog 共享锁、原子进度发布失败；各有独立真实成功正控，JSON/文本均核。内部编程异常另列，不充当这六种真实业务故障。
2. 两种互斥均由真实子进程、管道就绪与 stdin 屏障控制。共享 EventLog 挡迁移后原连接可继续写；真实 CLI 取得独占句柄后，另一进程的 EventLog 被拒且 payload 目录未创建，释放后可打开。未以 sleep 或同进程双连接替代。
3. 四个原名 failpoint 均真实 exit 77；分别核 DB 版本 0/1/1/2 和严格 format2 before/after 位置。普通打开在中间态拒绝，锁能重新取得；CLI 续做 0、再次调用 0。完整事件行、版本旁表、材料与触发器内容保持，UPDATE/DELETE 仍被 append-only 拒绝。
4. 初始进度发布与最终 completed 发布在真实 `os.replace` 边界失败；真实 SQLite authorizer 拒绝 DDL。均正确报 `migration_failed`，不声称 completed，资源释放，无注入时可续做。不是手写业务异常代替底层路径。
5. 原生 root symlink、DB symlink、DB hardlink 均先证明构造成功，再验证 `incompatible` 且进度未创建；本机无链接 skip。root symlink 特别证明 CLI 将原始 Path 交给 T2，未提前 resolve 丢失证据。成功与真实坏进度路径的 JSON/文本脱敏通过。
6. 解析缺参数真实 exit 2；restore/apply 仍不可用。重复 completed 调用的主库、进度、材料及备份 hash/mtime 保持；不自动建备份或服务目录。源代码只捕获 `AdmissionRefused`、直接沿用 `exc.code` 并调用已审 T2 迁移函数，没有复制摘要/版本/路径算法。

## 两个最少 CLI 负控

- `MCLI1-code`：只把 `exc.code` 改成恒定 `migration_failed`，仅运行真实 backup_mismatch JSON 一例。正常正控先成功，负例真实报告错误码；断言 `migration_failed != backup_mismatch` 将变异检出（1 failed、34 deselected）。
- `MCLI2-raw-path`：只给传入根路径加 `.resolve()`，仅运行真实 root symlink 一例。变异 CLI 错误放行并完成迁移（exit 0、ok=true），被期望 exit 2 的断言检出（1 failed、34 deselected）。

两者均为真实行为 kill，各只修改隔离副本的 data_cli.py 一个位置；没有 T2 变异。补丁、前后源码摘要、精确变异绑定和原始输出在 `cli-mutation-results.json`。汇总器首次错读 JUnit 截断的 message 属性，后改读既有完整 failure 文本；没有因此重跑变异或改变其原件。

## 首轮独立探针纠正

首轮 **30 passed / 5 failed** 原件完整保留在 `first-frozen-run/`。5 项均在 no-op 前由探针自己的只读 SQLite snapshot 创建零字节 WAL 与 SHM，之后 CLI 关闭时清掉它们；失败输出中的主 DB 与进度 hash/mtime 完全相同。只把 no-op 树采样移到额外 SQLite 观察之前，所有版本、历史事件、备份、触发器断言保留。修正前探针、精确顺序 patch 与分类记录位于 `fixture-history/`。不将这 5 项记为产品红或产品修复，候选产品从未改动。

## 作者 56 项原件核对

作者 56 项不是本次独立测试计数。冻结源码静态参数计数为迁移文件 29 + 既有 data CLI 文件 27；最终 raw 明确为 **56 passed，9.02s**。五份原 `/tmp/t3-admission/` 日志与冻结归档逐字节一致，19 份 archive 成员摘要匹配，三份源/测试的 before/after/freeze/候选摘要一致。旧 backup 测试前缀逐字节保持，仅末尾入口门禁按批准范围替换。

作者早期 25 failed / 26 passed 的 gzip 原件解压摘要也匹配；它只证明 migrate 入口当时缺失，不冒充深层故障与并发负控。作者 lock/ruff/diff 为原件核对事实，本片未重复执行这些门禁。文档保留候选与待独立/最终 full/CI 的边界，没有把本机结果写成 Windows 设备或全平台通过。

## 分类与边界

- 已修：产品缺陷无；独立探针修正 1 类观察顺序错误，保全首红。
- 部分修：0；未修：0；本片未决：0。
- 已满足：`acceptance-results.json` 的 12 项 T3 范围，包括两个窄变异与精确继承核对。
- 后续门禁：父会话同步实际 main、集中最终 full、四 required 原生 CI 与正式提交绑定；这些不是本片已通过的事项。

冻结 README 的数据段仍只列旧三个入口，并称“迁移启动门”待交付（197、252–257 行）。README 不在本片 26 项授权中；root 已明确由下一阶段更新使用说明与 migrate 命令，新增文档摘要须另绑最终组合，本报告不冒充已审该后续文字。该文档收口不改变本次已审七项非文档对象。

未写作者树、未 commit/push、未改契约。真实恢复 apply、容量保护、GC 与 Windows 设备支持仍不在本片交付范围。

JSON 正式报告 SHA256：`34237d0c26ca300c1c035e916215cea227dd3675d02774039238015881a0523e`。
原件索引：`evidence-index.json`，1767 项，SHA256 `9649685ed8c585b698d16586194608a35a302c35e9ba975a4546c36090580d89`。完整候选及两个变异树分别由各自 binding JSON 全量绑定，未混作原始运行输出。
