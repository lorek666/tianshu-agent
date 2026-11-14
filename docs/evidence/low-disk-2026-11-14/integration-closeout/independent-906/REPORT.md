# 906 集成修复增量独立复审

日期：2026-11-14。结论：`PASS_906_INTEGRATION_DELTA_WITH_EXACT_879_INHERITANCE`。本轮审核的七条产品/测试路径、事实披露与证据封装增量未发现新的阻断项；`BASE-B1` 仍为未修。本结论不是完整分支、后续事实回填、最终 Git 对象或原生 CI 的批准。

## 一、固定输入与审核边界

输入为 `/tmp/tianshu-g3-20261113/low-disk-integration-fix-root-d2v3tbh6/candidate-complete` 的 906 个普通文件。审核者逐件重算，并在本包 `source/` 保留完整副本，在 `work/` 保留完成负控后逐件还原的隔离副本。未修改 live 产品、旧报告、旧证据或 root 的门禁输出。HEAD 上下文为 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215`，906 内容以清单而非该 HEAD 单独绑定。

- 906 source map SHA256：`df72efbd9d5b078067861c9c20169d499e7fa4b1f1472a76d925d4d84e8fa360`。
- 冻结 facts SHA256：`2dfbaf5f718b8bbf303560fc458bd02e422e91e56e66edc8a1625024937f89c9`。
- 对 879 patch SHA256：`6bb515f5cb5651edecd1acb607802271a29e75c2017bd75d54890c95bd35bc30`。
- 前轮 879 map SHA256：`edcef0a5cd71a1320338d20590a3e74c0540cbcc6505fa585754fb6427947441`。

与 879 对比：9 个修改、33 个新增、6 个删除。修改中只有 `src/tianshu/loop.py` 与六条测试路径属于产品/测试；其余两条为 `docs/KNOWLEDGE.md`、`docs/LOW-DISK-2026-11-14.md`。所有其他 `src/` 文件逐字节继承 879，特别是 `memory_exec.py` 已精确恢复 879 字节。UI 全部 57 个文件、schema、PRD、契约、锁文件、CI/lint 配置和 `.gitignore` 在此轮均未改。详见 `logs/source-delta.json`、`logs/static-review.json` 和逐路径 patch。

审核继续继承原 T1/T2/T3/717/879 证据的既有范围，不扩大其批准。原 717 报告依然是 `NOT_APPROVED_PUBLIC_EVIDENCE_CLOSURE_REQUIRED`；879 文档/公开封装增量通过也不抵消其后首跑 full 的真实红灯。本次只核新的可定位增量与新探针；不重跑旧 T3 的 52 Python + 4 App，也不将旧 11-call 红灯改写为新结果。

## 二、逐项结论

**INT-LOOP1：已修。** loop 的唯一生产变更是删除三行 `pure_only` 写提案前置过滤。`_dispatchable` 的真实工具/schema/内部工具检查、pure manifest 过滤和所有真实执行侧门逐字节继承。独立 Worker 探针让真实只读子任务收到两个合法写提案：`memory.propose` 与 `create_report`。两条均经过 Dispatcher，分别留下 `c4.memory_scope_denied`、`c4.readonly_binding` 的 `dispatch.rejected` 与 `tool.result/rejected/none_applied`；无 `tool.not_dispatched`、intent、审批、文件或记忆行。每次提供给实际模型调用的 manifest 均由真实 registry 核验为 pure。

父目标版本改变后，独立探针得到 child `cancelled`、仅一次模型调用、`dispatch.parent_fenced`；subtask 仍 fenced，summary/result_ref 均空，父事件没有 `subtask.finished`。隔离恢复旧三行过滤时，同一 Worker 探针在实际缺少拒绝审计处变红；父 fence 探针真实观测到 `succeeded`/两次模型调用并变红。负控只证明旧过滤的审计/时序回归，不声称旧路径实际写入文件。

独立单条真实 eval 入口只跑 `RU-D1`、`repeat=1`、离线 ScriptedModel，20 条判据全通过；不是默认 repeat≥3 的正式能力报告。只读判据负控向该真实观察副本追加一条合成 `create_report/succeeded/applied` 子事件，判据立即 false，同时报告 1 条已生效结果与非纯读成功。该负控证明判据能发现合成危险观察，不是实际产品写入。

真实 pure root 持有实际租约的精准回归测试也通过，拒绝规则为 `dispatch.run_effects_disallowed`，仍无文件；未因移除模型层过滤而开放执行资格。

**INT-FIXTURE1：已修。** approval 与 m2 的 AgentLoop AST 除新增 `memory=self.stack.memory`、`contents=self.stack.contents` 外完全相同；recovery 的真实子进程构造除同 stack 的两个关键词外其他字节完全相同。旧断言、故障点、硬退返回码与恢复检查均未放宽。每处各选一条原测试正控通过；在自有隔离副本逐个恢复旧漏接线构造时，三处分别在真实 start 的 `DiskAdmission.assert_stores` 因实际存储实例不一致变红；recovery 是子进程返回 1 而非请求故障点的 77，准确表明旧夹具没走到故障点，不能记作成功 crashpoint 证据。

真实代码 `RULE_IDS`、真实 C4 schema enum 与 PRD 已发布规则表集合均为 78；精准原测试还核 schema 序列化、三分区全集/互斥及三条低盘规则均在 REJECTION。只更新原75的陈旧计数，没有删旧契约断言。

**TEST-MEM-INNER1：已修（测试装配修复，非内层产品新修复）。** 两个 user/task 用例仍走真实 grant、intent、dispatch、claim 和 `_begin_memory_execution`。正常上游先拒绝且 0 个 `execution.started`。测试注入只针对本 request 的上游 authority/资格两个方法；由真实上下文签出 permit 后立即恢复原方法，再直接调用真实 `_verify` 与完整 `execute`，两处均按只读子任务拒绝。上游调用恰两次、execution.started 恰一个、记忆 0 行。没有手造 permit 或替换内层方法。

独立去掉自有 work 副本中 `_verify` 的 child guard，仅运行这两个精准用例，两个均在测试第265行直接 `_verify` 对应的 raises 断言报 `DID NOT RAISE MemoryDispatchRefused`；随后源文件已还原并逐件核回 906。本轮不采用作者早期 full-execute 负控的异常类型失败来证明内层 kill，也不声称该负控实际写入记忆。原 `_verify` 与879完全相同；本次改的是恢复有效验收路径。

**PUB-ARCHIVE1：已修。** 六件历史脚本仅从 `.py` 迁为 `.py.txt`，每件 size/hash/字节对照旧879与原 index 相同；旧六路径实际不存在。新 `archived-script-paths.json` 显式一一映射六件，没有越界或碰撞。保持原 artifact-index 不改，通过新映射解析原173条，全部通过；除此之外327件既有低盘证据文件均与879字节相同。不得把旧清单的六个历史路径表述成当前仍存在的文件。

`integration-closeout/published-subset.json` 精确列25件，目录只有这25件与清单本身；逐件与绝对定位原件核 size/hash/字节，其中14件同时核原独立包 index 记录。其余为原index自身或原门禁原件，未假称整个旧包均已公开。新增7个本地文档链接均可解析。

首 full 的 `41 failed, 3141 passed, 8 skipped / 495.61s`、原完整 UI 的13文件91测试、首 ruff 的六历史脚本7错等披露与原日志一致。历史脚本以文本归档且保留旧字节，没有改 lint ignore/config 来掩盖红灯；后续 root 修后 ruff 输出是另一个版本绑定的结果。BASE-B1 的未修披露保留，未产生 CLI 预算新承诺。

**BASE-B1：未修，精确继承。** 本片没有相关生产代码变化；保留原限制，不将它变成“已修”，也不凭本窄审宣称整支所有历史边界均已批准。

## 三、独立执行证据与负控边界

独立命令、cwd、必要环境覆盖与原始 stdout/stderr 均在 `logs/*.command.json`、`logs/*.stdout.txt`、`logs/*.stderr.txt`；聚合为 `logs/check-results.json`。Python 使用工作树 `.venv/bin/python` 仅作解释器，所有实际模块来自固定 source 或审核者 work 副本，禁 bytecode，全部临时数据库/authority 位于本输出目录；未调用真实或付费模型。

正控由 `probes/current_path_probe.py` 和 `probes/run_checks.py` 执行：四个真实离线路径通过；精准 pytest `7 passed in 1.02s`。选择为 approval 一条、m2 一条、recovery `after_intent-create` 一条、真实工具/规则枚举一条、pure-only审计一条、MemoryExecutor user/task 两条。不是作者311项矩阵的复跑，也不是41条旧失败的全量复跑。

六个负控命令全部在指定真实行为处变红：旧filter Worker/parent-fence各一；去内层guard的user/task合并一；三旧构造各一。只在本包 work 修改，逐次 finally 还原，结束时固定输入、source与work完整906清单相等；最终只读证据为 `logs/close-readback.json`。另有一个只读判据的合成观察负控，不与六条产品/夹具负控混算。

本轮三个观察器修正单列而不洗成产品证据：最早精准pytest因 `--basetemp` 父目录不存在造成7个setup错误，原日志完整保留，仅修观察器后同样7项通过；内层负控第一次运行在文本匹配 assertion 处停下，尚未修改 memory源码或启动该测试，修正匹配后继续；最终读回脚本最初误以为 pytest 会打印 raises 内下一行而断言失败，保留原stderr后改为日志第265行与固定源第266行的精确绑定。细节见 `logs/observer-corrections.json`。这些均不计入行为红或产品失败。

## 四、外部门禁原件的独立绑定

root 另行完成 `/tmp/tianshu-g3-20261113/low-disk-corrected-python-gates-qil3l7i3`。本审核者没有重跑 full；只独立核其原件、log字节/hash、source-before/source-after与906精确相等，status/deleted前后字节相等，并在本包 `external-corrected-python-gates/` 保留该目录11件普通文件的原字节副本。

原件显示 `uv lock --check`、`uv run --frozen pytest`、`uv run --frozen ruff check .` 均exit0；pytest为 `3183 passed, 8 skipped in 509.43s`，命令用时509.86s。它是root同源全量门的外部证据，不是本审核者独立重跑的结果。UI此次未重跑；仅按57文件精确不变继承879原完整13文件91项与install/typecheck/build结果。未核原生CI、未作跨平台用户设备声明。

## 五、封存与结论适用范围

本报告、facts、所有输入/脚本/日志/运行数据库与完整source/work快照纳入 `hash-index.json`，索引自身除外。所有普通文件0444、目录0555，完成后逐件只读复核。pytest convenience current symlink在记录目标后移除，未移除任何源文件或数据库证据。

本906增量的四项问题已修且无新增窄范围阻断；BASE-B1未修。未审核906冻结之后四份事实文档或新的公开subset，也未批准未来最终Git对象。root后续文档封装回填须用新固定delta另行绑定，不能把这里的906结果直接挪给不同来源清单。
