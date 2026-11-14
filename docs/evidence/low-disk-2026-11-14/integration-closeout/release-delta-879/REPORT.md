# 低磁盘公开证据与事实文档增量独立销账

日期：2026-11-14（Asia/Shanghai）  
独立复审者：`/root/review_low_disk_integration`

## 1. 结论与适用范围

**`PASS_RELEASE_DOCUMENT_EVIDENCE_DELTA_ONLY_WITH_EXACT_717_INHERITANCE`**。

这次只批准下述 **879 项固定候选相对原 717 项的文档与公开证据增量**：INT-E1 已修，
DOC-N1 已修；新发现的范围内文档/证据阻断为 0。原 717 审计的真实 exit 1、缺件记录和
`NOT_APPROVED_PUBLIC_EVIDENCE_CLOSURE_REQUIRED` 结论均保持原样，不将旧报告改为通过。

BASE-B1 仍为 **P2、未修、基线既有**：CLI 引擎没有装配预算账本。当前文档已准确披露，
其他入口的预算证据不得外推 CLI；本轮未修产品，也未重新运行预算探针。

本结论**不批准最终组合、正式提交或原生 CI**。root 在本轮期间告知：879 候选一次集中门
已经启动，lock exit 0，full pytest 当时仍在运行且早期已有 8F，尚无最终回溯/汇总。
这是 root 的协调状态，本代理没有执行或诊断这些门禁，也没有核实其后续状态。
失败不能由本次公开证据销账抵消。后续实质产品变动须另冻并重审受影响范围。

## 2. 固定输入与字节继承

新候选目录：
`/tmp/tianshu-g3-20261113/low-disk-release-candidate-cxszmop9/candidate-complete`。

- 新 879 项 `source-hashes.json`：
  `edcef0a5cd71a1320338d20590a3e74c0540cbcc6505fa585754fb6427947441`
- root 新 `facts.json`：
  `6b4b5fe6cdb435a14644918c36409d7fee97c1f00cfff0d69e7dba30434f97ea`
- root `delta-vs-reviewed-t3.patch`：
  `02fd4a60cfb0f4121088b4a50173845ebd70faebacddbc696ba96b6198d9c0dd`
- 原 717 项源清单：
  `7acc68435efe4e926dcf303b36f0a89ae0ec478d17c3f5e8a4c80e3a05939db6`

独立重算全部 879 文件，路径集合精确，无额外文件或缺件；审计末尾再次重算。
新完整源的逐字节副本保存在本包 `source/`，输入身份三件保存在 `inputs/`。

相对 717：**修改 5、新增 162、删除 0**。修改仅为 `.gitignore`、`docs/KNOWLEDGE.md`、
`docs/LOW-DISK-2026-11-14.md`、`docs/PRD-v1.md`、`docs/low-disk-admission-contract.md`。
新增拆分为整分支公开子集 35+清单 1、T3 独立公开子集 119+清单 1、root 717 身份三件、
漏掉的视觉 dist 三件，合计 162。

以下 **267 路径逐 hash 零差异**：原 717 中 `src/`、`tests/`、`schemas/`、`ui/src/`、
`ui/dist/` 下文件，以及 `pyproject.toml`、`uv.lock`、`ui/package.json`、`ui/pnpm-lock.yaml`。
因此本轮没有新产品行为变动；此前各切片只能按原身份、原范围继承，不变成整功能验收。
全部 delta 与 267 路径列于 `logs/source-delta.json`。

五文件可读差异为 `logs/document-delta.patch`，SHA256：
`62e51ff383117b166ce934c8ee1b7ca52eafab7af5d879e75076d659d0098c5f`。

## 3. INT-E1：已修，仅对新 879 项销账

原公开 `artifact-index.json` 的 SHA256 仍为
`42a9fb2d0d622d9883d4181f1c472d359121c9224de260b8d4cebb5d31cc65dc`，与原 717
副本逐字节一致。其 **173 项现均真实存在，并与原索引 size/hash 一致**。
原来漏掉的三件现在确实属于新源清单，且与产品 `ui/dist` 对应文件一致：

- `t3/visual-static-demo/dist/assets/index-CIZ6Bgii.js`，332572 字节，
  `fe672593496fdb8228021e3457107e032569e903206361c1338f0050e010e533`
- `t3/visual-static-demo/dist/assets/index-XPu6LbkB.css`，28789 字节，
  `799a089898524ac56f7d6845cccd07ed971161ce87f070237ec69f295e664225`
- `t3/visual-static-demo/dist/index.html`，951 字节，
  `52420ffb1ff112f4c19458e9c45e12effb3ceed2d553e565f6c4c137322c632e`

以上路径相对于候选 `docs/evidence/low-disk-2026-11-14/`。
使用本代理独有临时 Git 沙盒复制原/新 `.gitignore`，用 `git check-ignore -q` 分别核验：
修改前三件都被忽略（exit 0），修改后三件都不被忽略（exit 1）；无关
`unrelated/dist/control.js` 前后均仍被忽略。没有操作产品 Git 索引或创建提交。
沙盒用后移除，完整 8 条命令及 stdout/stderr/exit 留在 `logs/gitignore-sandbox.json`。
三件绑定见 `logs/dist-closure.json`。

## 4. 公开子集、历史冻结口径与文档准确性

公开 `integration-review-717/published-subset.json` 的 **35 条**和
`t3-independent-717/published-subset.json` 的 **119 条**均逐一核对：

1. 公开文件实际 hash/size 等于清单声明。
2. 公开文件与 `original` 指定的原件逐字节相同。
3. 原相对路径匹配原 hash-index 的条目；索引文件自身绑定其独立 SHA256。
4. 改名只有 `.py`、`.log` 额外增加 `.txt`；`original_relative_path`（如提供）准确。
5. 两个目录不存在清单外文件，`published-subset.json` 自身除外。

清单与 LOW-DISK 文档明确只公开子集，不声称历史 hash-index 中的完整输入、源副本和
测试数据库都在仓库可下载。本轮不要求公开全部原始包，也不将原索引中未公开文件当缺件。
完整逐件结论见 `logs/subset-mapping.json`。

原整分支包 `/tmp/low-disk-whole-branch-review-20261114` 的 **1025 个索引文件加索引自身**，
以及 T3 包 `/tmp/low-disk-t3-independent-20261114` 的 **1485 个索引文件加索引自身**，
本轮均重新核 hash/size、精确文件覆盖、文件 0444 / 目录 0555。原正式身份如下：

- 整分支 REPORT：`7f27c76e4c0106c5abb77e75d8e624d0ccdeac405bb9c9913299818e6ca2f693`
- 整分支 facts：`351e38bc07887ed0a7a4a73add4d1bab01479b669480783a02cac9147fd76a2d`
- 整分支 index：`7be453886f728e690f33528c67c8b3b3506a3c524a35a0ecf96db942741fc239`
- T3 REPORT：`e1de27bf1c6ae73718a91ce9bf8568a2a0fca8956db37f6a64401c36428c877d`
- T3 facts：`a07fe5ce56a24884edcd4ed5756a033194c081e1d833dcbd14cfe79e02ebdeb1`
- T3 index：`537c937fe34e0395f5861abc660bb90edc88944f4b1876098adbd1c2138bb757`

`root-reviewed-717/` 三件逐字节匹配原 root 冻结，源清单和 facts 也匹配原整分支保留输入。
亲核作者 map 恰为 711、root map 恰为 717，差集恰为文档列出的 6 件派生物，且 711
交集 hash 一致。**DOC-N1 已修**：711+6=717 明确为历史口径，三件漏 dist 另计。

契约 §1–5 除“当前 75 项”改为明确“契约提交阶段当时 75 项”的事实句外，文本完全相同；
§6 的 LD-01–06 表完全相同。PRD 变更只更新各冻结切片的验收状态与限制，没有放宽契约。
本次新增的 10 个本地 Markdown 链接目标均存在。

BASE-B1 在 LOW-DISK 和 KNOWLEDGE 中明确为 P2 未修，写明基线 a022/T2/T3 都未传
`budget=stack.budget`；离线成功周期属于缺口复现，不是预算验证通过。
文档保留不得把其他入口预算证据外推 CLI 的限制，状态与原独立报告一致。

## 5. T3 独立结果的材料核对

本轮只读取封存材料，**没有运行以下测试**。文档的计数与正式 T3 报告、facts、实际命令、
原日志和负控 JUnit 逐项对账：

- 首轮 Python 50 passed；paused 补充 2 passed / 50 deselected，共 52 个新增独立案例。
- 真实 App DOM 首轮 4 passed；这与 Python 是分开的命令，不存在一次合并 56 项命令。
- 复原正控 Python 5 passed / 47 deselected、App 2 passed / 2 被名称过滤未执行，均单列不重计。
- 11 个最小变异均 runner exit 1、selected 1、call failure 1、error 0；不是 setup/import/collection kill。
  Python JUnit 确认 failure 1 / error 0；UI 各 3 个过滤未执行项，不是作者 skip。
  每项的 717 before/restored map 都与原 map 一致。
- 27 份已有拒绝案例的持久材料核对为 CLI 8、IPC threshold 5、IPC bad 14，不增加测试数。

App 案例在 HostClient 边界使用 mock，证明真实 App 的 DOM/参数接线；真实后端证据来自
另外的 CLI/原生 IPC。不能将这两层材料改称付费模型或完整 UI 端到端验证。

`logs/t3-raw-reconciliation.json` 保存精简核对结果，原件全部通过公开映射和原索引身份绑定。
原报告保留的 UI 过滤元数据修正、71/211 路径计数观察器错误及平台限制未被删改。

## 6. 执行记录、观察器错误与剩余门禁

主要审计脚本：`probes/audit_release_delta.py`，仅标准库文件/hash/文本/XML 与自有 Git 沙盒；
不导入产品、不访问 live 产品源码、不运行产品测试、API、UI 或真实模型。

完整成功命令、脚本 SHA256、执行时间和 exit 0 在 `logs/audit-2.command.json`；stdout 在
`logs/audit-2.stdout.txt`，stderr 在 `logs/audit-2.stderr.txt`。系统 Python 只充当文件观察器，
不构成对该 Python 版本的产品支持声明。

第一次完整审计在观察器断言处 exit 1：7 个 Python 负控的原单项记录没有汇总后来增加的
`authored_skips=0` / `filtered_unexecuted=0` 两字段，所有共有字段相同；4 个 UI 记录完全相同。
修正为严格比较全部原字段，并仅允许 Python 汇总多出这两个零值字段。首脚本
`probes/audit_release_delta_first_observer.py`、首 exit/原 stderr 保留，说明在
`logs/audit-observer-correction.json`。这不是产品失败，也没有将旧产品红改写成绿。
恢复上下文时另有一次临时路径观察误把 map 放在 candidate-complete 内产生 FileNotFoundError；
列目录后修正，未导入或执行产品。此前临时 JSON 观察的 list/dict 错配与过量输出也不计产品测试。

本轮产品/旧证据写入为 0，产品测试与集中门运行数为 0。root 的 full pytest、全仓 ruff、
完整 UI 门禁、正式提交对象和原生 CI 均须另交实际结果；真实跨卷/用户设备、付费模型、
10h soak、恢复 apply、容量/GC 和发行不在本轮批准范围。

本包 facts SHA256：
`2cae5e8ab2c2d229c24da07c3f6a37575d1df817fe51517e9820572761c05038`。
封存策略为文件 0444、目录 0555，`hash-index.json` 覆盖本包除索引自身外全部普通文件；
索引自身 SHA256 在最终交付消息单列。原 717 不批准历史与本次 879 窄销账同时保留。
