# M0–M1 独立 Debug 报告

> 审查对象：`main @ 7fd5731`（`3b0ac91 feat(m1): 崩溃恢复与 100 次故障注入`）
> 方法：读实现与测试，运行基线测试，并在本仓库 `.tmp/` 中用临时 workspace 做对抗性复现。
> 基线：`uv sync --python 3.12 && uv run pytest` → **41 passed in 5.19s**。
> 限制：本次只写报告，没有修改实现代码。没有实际切断电源或做内核级故障注入；有关真实断电的结论是依据写入顺序与 fsync 调用情况作出的静态判断。

## 结论：不能进入 M2

现有测试证明了正常路径和少量进程中断场景，但没有证明 PRD 的核心承诺。恢复会误报未执行的调用，阻断可绕过，覆盖写和 undo 都可能造成不可恢复的数据损失。

**M2 准入最小集合（全部必须完成）：**

1. 修复 S1：恢复不得凭“目标哈希等于期望”推断执行者；manifest/写前证据不可恢复时必须 `unknown`，不得伪造 `before_state=absent` 的成功凭据。
2. 修复 S2：所有写入入口，包括 `run()`、`execute()`、undo 及以后 Host/RPC 入口，都在同一授权边界检查阻断；补齐 undo + unknown 的明确语义与测试。
3. 修复 S3/S4：实现原子替换和 crash-safe 元数据提交；消除 manifest 丢失窗口、result 无 receipt 窗口，修复缺 blob 时的部分 undo。
4. 将下述反例纳入故障测试：已存在同内容文件、modify 目标消失、after_result 缺 receipt、缺 manifest、缺 preimage blob、partial write、并发/嵌套恢复。
5. 修复中等级别的真实模型预算与连续调用错误映射（M2 会接入更多模型和工具，不能把已知的静默成功继承下去）。

不必等真实断电实验室才能开始修复，但在 M1 对外宣称“断电可恢复”前，至少要有 fsync/原子 rename 的实现及断电或等效存储故障验证。

---

## 严重

### S1. 恢复把“目标恰好像执行后”误判为已执行，并编造写前状态

**现象**

- `create_report` 有 intent 但从未执行，目标已经由用户/其他进程创建且内容相同，恢复返回 `applied`，`before_state=absent`。
- `modify_text` 有 intent 但从未执行，原文件本来就等于请求内容，同样返回 `applied` 和 `before_state=absent`。
- 已修改文件后来被用户删除，`modify_text` 恢复返回 `none_applied`；正确的应是“曾经是否执行未知/当前状态已改变”，不能断言没有副作用。
- manifest 丢失时，只要当前内容匹配期望，仍恢复为成功。此时恢复既不能证明本次调用写过，也没有真实 preimage。

**代码位置与复现**

- [recovery.py:99–119](src/tianshu/recovery.py#L99-L119)：`current == expected_after` 直接判成功；[recovery.py:121–128](src/tianshu/recovery.py#L121-L128)：目标不存在对所有写工具都判未执行。
- 在临时目录仅写一条 `tool.intent(call_id=c1, tool_id=create_report, args={name: report.md, content: new})`，事先创建 `report.md = new`；调用 `recover_run()` 的实际输出：
  `applied=['c1']`，result output 含 `before_state='absent', recovered=True`。
- 对 `modify_text(path=note.md, content=same)`，预置 `note.md = same`，得到同样的假阳性。
- 对 `modify_text(path=missing.md, content=new)`，不创建文件，实际得到 `none_applied=['c1']`。

**影响**

违反“绝不误报成功”。可能删除用户文件来 undo 一个 agent 从未创建的同名文件；修改类操作的真实写前内容永久丢失，receipt 却仍显示可审计。哈希相同不能证明写者、调用是否发生、写前是否存在。

**建议修法**

- 分离 `current_observation`（磁盘当前是什么）与 `execution_outcome`（本调用做了什么）；仅凭 after hash 不能证明后者。
- 没有可信的 execution record 或原子提交的 before/after evidence 时标 `unknown`，保留观测，不生成 applied receipt。
- 读取 manifest 中与 `call_id` 精确匹配的 change；不存在、路径冲突、内容不可信时 fail closed。`load()` 返回空列表必须区分“无变更”和“manifest 缺失”。
- `modify_text` 目标消失且 after 曾为非 absent 时必须 unknown；不能套用 create 的 absent 规则。
- 新工具在写入前原子提交 preimage；恢复只认与本次 attempt/call 绑定的证据。

### S2. `recovery.blocked` 不是执行边界；undo 可绕过且没有解除语义

**现象**

仅 CLI `run` 启动时检查一次。直接调用 `loop.run()` 可在已阻断 workspace 内成功写文件；`tools.execute()` 没有阻断检查；`cli undo` 完全没有检查。阻断永久存在于日志中，没有核查完成后的解除事件，未知调用也没有 undo 记录。

**代码位置与复现**

- [cli.py:98–109](src/tianshu/cli.py#L98-L109)：唯一 `ensure_not_blocked` 调用。
- [loop.py:118–128](src/tianshu/loop.py#L118-L128)：库入口没有检查；[tools.py:317–328](src/tianshu/tools.py#L317-L328)：所谓“唯一授权/校验点”没有授权上下文。
- [cli.py:153–156](src/tianshu/cli.py#L153-L156)：undo 直接操作磁盘。
- [recovery.py:197–219](src/tianshu/recovery.py#L197-L219)：只查询 `recovery.blocked`，没有 resolved/cleared 逻辑。
- 实测先写 `recovery.blocked(workspace=<ws>)`，再直接 `loop.run()`：`outcome='succeeded'`，`new.md` 确实生成。另一个已阻断 workspace 调用 `store.undo()`：`ok=True`，文件已还原。

**影响**

所谓“阻断后续写入”只覆盖一个 CLI 路径。接入 M2 的 daemon/RPC、worker 或并行 Run 后，绕过是默认行为。检查与执行之间还存在 TOCTOU：另一个 Run 可在检查之后写入 unknown。

**建议修法**

- 阻断成为 ToolContext/Grant 的必要条件，在不可绕过的执行 dispatcher 再校验；不要依赖 CLI 预检。
- workspace 使用规范化稳定 ID，并在写入事务中校验 lease/阻断版本，避免 check-then-act。
- 为 unknown 与 undo 定义组合规则：默认禁止在未对账 workspace 内 undo；允许时必须有逐调用的可信证据，并写 `undo.intent/result`。
- 提供人工核查后追加的 `recovery.resolved(run_id, call_id, evidence, decided_by)`；由最新阻断/解除事件及未决 unknown 计算当前状态，不能删除历史。
- 补库入口、execute、undo、第二个 Run、嵌套路径与解除后恢复的测试。

### S3. 覆盖写直接截断原文件；没有原子替换或 fsync，进程中断也可能留半文件

**现象**

`Path.write_bytes()` 以截断方式打开已有文件。若在写入过程中进程被杀或异常，原文已经丢失，磁盘可留下半个新文件。manifest 此时通常还未写，因此不能 undo。代码没有任何 `fsync`、临时文件原子 rename 或父目录同步。

**代码位置**

- [tools.py:209–219](src/tianshu/tools.py#L209-L219)：读取 preimage 后直接 `target.write_bytes(data)`，最后才 `ctx.record(change)`。
- 新建文件也有未覆盖窗口：[tools.py:174–183](src/tianshu/tools.py#L174-L183)。
- 全仓库无 `fsync` / `os.replace` 调用；[events.py:91–95](src/tianshu/events.py#L91-L95) 只有 SQLite 的 WAL/FULL 设置。
- `after_write` 故障点位于整个 write 调用之后：[loop.py:251–269](src/tianshu/loop.py#L251-L269)，根本没有覆盖 write 内部的故障。

**影响**

普通进程中断就可能永久损坏原文件，不需要真实断电。真断电时文件、目录项、blob、manifest 和 SQLite 的持久化顺序更不受控。当前“恢复后只能是写前或写入内容”的不变量没有被实现保证。

**建议修法**

- 同目录创建临时文件，写入→flush→`os.fsync(file)`→`os.replace(temp, target)`→按平台能力 fsync 父目录；replace 之前保存并持久化 preimage。
- create 使用真正不可覆盖的提交语义；不能用“先 exists 检查，再写”的 TOCTOU。
- 写前 blob 也使用临时文件 + fsync + 原子发布，引用前确认完整哈希。
- 明确 Windows 上目标被占用、目录 fsync 不支持等错误处理；不可静默退化。
- 加写中故障点与存储故障测试；真断电承诺需用可回滚块设备/VM 掉电等存储级实验验证，`os._exit` 不足以替代。

### S4. undo 的“原子性”只在无错误且没有并发的理想路径成立

**现象**

undo 第一遍只校验文件 after hash，不检查 preimage blob 是否存在、内容是否正确。第二遍发现某文件 blob 丢失时会 `continue`，此前文件已经还原，最终形成部分撤销。并发用户写入或 IO 错误也可在校验后、写入时造成不一致。

**代码位置与复现**

- [snapshot.py:94–135](src/tianshu/snapshot.py#L94-L135)：缺失 blob 在第二遍才检查，并继续处理。
- 实测构造 `a.md`、`b.md` 两条可匹配 after hash 的 change，删除 `b` 的 preimage blob 后 undo：
  `ok=False, restored=['a.md'], problems=['b.md：写前内容丢失，无法还原']`；
  实际 `a.md='A-before'`、`b.md='B-agent'`，与“一个字节都不动”的承诺相反。

**影响**

用户既不能完整保留 agent 修改，也不能完整回到原始状态；损坏 manifest 还会加剧此问题。undo 没有事件记录，之后恢复无法知道文件变化来自 undo。

**建议修法**

- 预检必须读取并校验所有 preimage blob 的 SHA-256、路径类型和 after evidence，再允许任何写入。
- 用 workspace 锁/lease 防止并发 Run 与 undo；undo 使用临时文件和原子 replace，不能靠 `write_bytes` 还原。
- undo 先持久化操作 journal 与本次当前状态的 preimage，记录逐文件结果；崩溃后可继续或回退，返回明确的 `partial/unknown`，不能宣称跨多文件天然原子。
- 将 undo 纳入事件日志、receipt 与阻断协议，删除或还原后重跑 recover 不得误判。

---

## 中等

### M1. “单提交点”只实现为逐事件 autocommit；manifest、result、receipt 不是原子事务

**现象与代码位置**

- [events.py:91](src/tianshu/events.py#L91) 使用 `isolation_level=None`，[events.py:126–147](src/tianshu/events.py#L126-L147) 每次 append 独立 INSERT；没有显式组合事务。
- 主循环顺序为：文件系统写入 → manifest → `tool.result` → `receipt` → `run.end`，见 [loop.py:251–331](src/tianshu/loop.py#L251-L331)。
- 恢复又是两个独立提交：[recovery.py:116–118](src/tianshu/recovery.py#L116-L118) 先 `_record_result`，后 `_record_receipt`。
- 大 payload 先 `Path.write_bytes` 再 INSERT：[events.py:114–146](src/tianshu/events.py#L114-L146)。blob 丢失会在读取时变成未处理异常，非内容寻址路径校验。

**实际影响**

- 已有 `after_result` 故障点会留下 applied result 但无 receipt；`verify_receipts()` 实测返回 `副作用已发生但没有凭据`，恢复因已有 result 而不补。
- `after_write` 名称不真实：此时文件已写，但 manifest 尚未写，恢复会丢失 preimage。
- manifest 写成功后、result 前崩溃可留下文件变更与 manifest，但主循环没有恢复 attempt 身份。
- PRD §5.3 所要求的与 fencing/grant/intent 同事务也没有对应表或事务实现；M2 不能直接在现有 append 上叠加审批。

**建议修法**

- 明确定义一个 effect commit record，把 before/after evidence、attempt、result、receipt 的提交作为一个 SQLite 事务；外部副作用与 DB 不能假装同一 ACID 事务，使用 prepared intent + durable evidence + reconciliation。
- `call_id + attempt_id` 必须唯一；恢复发现缺 receipt 时按确定性 ID 补齐，或对账标记异常，不能仅按 result 存在跳过。
- 将 manifest 元数据放入同一事务或引入带 prepare/commit 状态的 journal；payload 文件先完整持久化并校验，再允许引用。
- 为每个提交边界建立故障点，不只测四个粗粒度位置。

### M2. 未知 write tool 会被误判为无副作用

**代码位置**：[recovery.py:76–85](src/tianshu/recovery.py#L76-L85)。

`SPECS.get(tool_id)` 返回 `None` 时不进入 pure 分支，随后若 intent 有 `path/content` 且当前哈希匹配，就可能被标为 applied；不匹配才可能 unknown。恢复不知道工具语义时不应推断其行为。

**影响**：schema/工具版本变化后，历史事件可能得到错误恢复结论。

**建议修法**：按 `(tool_id, version)` 找恢复器；未知工具或版本一律 unknown 并阻断，禁止通用 path/content 猜测。旧工具版本要保留兼容恢复器。

### M3. 真实模型路径会把 provider 错误、工具缺失、预算未知静默记为成功

**代码位置**

- [model.py:191–204](src/tianshu/model.py#L191-L204)：HTTP/网络错误返回 `finish_reason='error'`，不抛异常。
- [model.py:233–240](src/tianshu/model.py#L233-L240)：所有未知 finish reason 映射成 `end_turn`，包括 `None` 和未知枚举。
- [loop.py:217–219](src/tianshu/loop.py#L217-L219)：只要 finish 不是 tool_use 就结束，error/length/content filter 等不需要 tool call 的错误会走到这里。
- 没有 artifact 时统一 `outcome='noop'`；[cli.py:130](src/tianshu/cli.py#L130) 将 noop 当退出码 0。usage 全未知只增加计数，不阻断预算。

**影响**：key 错误、服务限流、截断、工具调用能力缺失均可显示“noop/成功”，现有 ScriptedModel 测试无法发现。M2 接更多 provider 后会放大。

**建议修法**：把 transport error、invalid/truncated response 与正常结束分开；未知 finish 保留 unknown 且失败，不映射为 end_turn；定义 noop 的明确产品语义；未知 token usage 时按保守策略暂停/限制或明确标为不受控，不得返回预算已保证。

### M4. 文件与 workspace 身份、锁和校验不足以支持多进程真实环境

**代码位置**

- [workspace.py:26–43](src/tianshu/workspace.py#L26-L43)：先 resolve 再读/写，路径在检查后仍可被替换，symlink/目录竞争存在 TOCTOU。
- [recovery.py:213–219](src/tianshu/recovery.py#L213-L219)：阻断 key 是另一份 `Path.resolve()` 字符串，没有与 `run.start.workspace`、设备/inode 或 lease 关联。
- 没有 workspace 锁；[snapshot.py:75–83](src/tianshu/snapshot.py#L75-L83) manifest 的读-改-写可丢失并发更新，绝对 `blob_ref` 让数据搬迁后失效。
- [recovery.py:59–66](src/tianshu/recovery.py#L59-L66)：intent/result 以 call_id 建字典，重复 ID 会覆盖；无唯一约束，不能证明“一次 intent 恰好一个 result”。

**影响**：双 CLI/后续 daemon 与旧 worker 可相互覆盖，100 次顺序子进程测试完全未覆盖。路径 Unicode 规范化也未用于实际 resource identity。

**建议修法**：M2 的 lease 必须落到实际工具 dispatcher 与 snapshot/undo，而非只做作业表；使用稳定 workspace ID、原子文件描述符/句柄操作，限制 call/attempt 唯一性。本项可与 S2/S3 一并修，不建议另起一套并行权限机制。

---

## 轻微

### L1. 测试数量和断言存在“覆盖假象”

- [tests/test_recovery.py:151–205](tests/test_recovery.py#L151-L205) 的 100 次注入只跑 `create_report`，没有 `modify_text`，也没有“write_bytes 内部”的故障点。
- 测试 A 只读取 `note.md`，而 scripted 工具写的是 `report.md`；所以 A 实际证明的是“未触碰输入文件”，不是目标文件原子性。
- `after_result` 的 100 次测试只检查 intent/result 配对，不运行 `verify_receipts()`，缺 receipt 仍可通过。
- 所有故障都在新进程正常创建 DB、打开 snapshot 后注入，未测损坏 manifest/blob、同内容、用户并发写、文件删除、重复 call_id、未知 tool/version。

**建议**：保留现有测试作为 smoke tests，另建按操作类型 × commit boundary × 外部状态矩阵的故障测试；测试名和文档不要写“断电模拟”，应写“进程终止模拟”。

### L2. 快照内容会替换同一路径旧 preimage，且 undo 不留痕

[snapshot.py:75–83](src/tianshu/snapshot.py#L75-L83) 对相同路径仅保留最后一次 change；多次修改同一文件会丢失最初状态，无法真正“撤销这次 Run”。如果这是有意设计，manifest 必须记录操作链/基线并定义撤销边界；否则按 call 保存所有 evidence。undo 成功后也没有状态标记，可以重复执行，与后续恢复/新 Run 的关系未定义。

---

## 对四个指定问题的直接回答

1. **原子性**：没有做到。SQLite 每条事件各自 autocommit，文件、manifest、result、receipt 分属不同提交。最危险的两个窗口是“文件已写、manifest 未写”和“result 已写、receipt 未写”。
2. **真实断电**：当前不能承诺。覆盖写非原子，用户文件/blob/manifest 无 fsync，目录项无同步；SQLite WAL/FULL 只保护数据库自身，不会替这些文件提供顺序保证。
3. **阻断**：只在 CLI `run` 入口检查一次，库入口、execute、undo 都可绕过；没有 lease fencing 和解除协议。
4. **undo × recovery**：没有统一状态机。unknown 后 undo 仍可写；恢复成功可产生错误 preimage 的 receipt；manifest 缺失可能被当成“无需证据”；after_result 缺 receipt 不会修复；重复 recover/undo 无法表达完整事实。

## 建议的修复顺序

1. 先封住 S1/S2：不确定一律 unknown，授权/阻断下沉到唯一执行边界。
2. 再重做 S3/S4/M1 的写前证据、原子替换与提交协议，避免在错误恢复算法上继续加分支。
3. 把 undo 纳入同一事件/恢复协议，再扩测试矩阵。
4. 最后修 M3 的真实 provider 失败语义，通过 M1 准入复核后才进入 M2。

---

## PR #6 独立复验（c9f7c61，附查 e30901b）

**裁决：不通过，不建议合并为“M1 已完成”。** 在独立 detached worktree 固定审查 `c9f7c61`，`uv run pytest` → **52 passed in 5.63s**；后续 `e30901b` 仍 **52 passed**，且其恢复/undo 的相关逻辑未修。并行工作期间主工作树曾变化，因此以下反例均以独立提交中的实现为准，未把未提交修改算作 PR 内容。

| 指定检查 | 复验结果 | 判定 |
|---|---|---|
| create 目标已存在且相同，无证据 | `applied=[]`，`none_applied=['c1']`，不生成凭据 | 原假阳性已修；但见 R1 缺 manifest 反例 |
| modify 前后内容一致 | 有可信证据且磁盘匹配时 `none_applied` | **接受内容副作用口径**：`none_applied` 而非 `applied/failed`；执行状态可另用 `succeeded/noop`，不能混淆写入成功和效果有无 |
| 直接 `run()` / `execute()` | 不写目标文件；`run()` 返回 `outcome=noop` 但 `effect_state=unknown`；execute 无 log 拒绝 | 文件阻断有效，但 run outcome 错，见 R4 |
| 已阻断 workspace 下 undo | `ok=True`，`restored=['note.md']`，原文被还原 | **仍完全可绕过**，见 R2 |
| 缺一个 preimage blob 后 undo | 两文件都不动，`restored=[]`，报 missing_blobs | 指定反例已修 |
| manifest 与 result 的提交窗口 | manifest 虽改为写前落盘，但缺 manifest/条目仍直接 `none_applied` | **仍阻塞 M2**，见 R1 |

### R1（严重）缺 manifest 或条目仍把副作用 intent 判成未执行

[recovery.py:91–102](src/tianshu/recovery.py#L91-L102) 将“没有证据”无条件解释为“写入未开始”；[snapshot.py:95–119](src/tianshu/snapshot.py#L95-L119) 已能区分无 manifest，却没有被用于恢复。

实测：只有 `modify_text` intent，SnapshotStore 没有任何 manifest，`has_manifest=False`；恢复仍返回 `none_applied=['c1']`，不阻断。未知写工具没有证据时也走同一路径。

“写前证据”解决了正常崩溃的顺序，但**无法证明证据从未存在**：磁盘故障、丢文件、旧版本数据、错误 artifacts 路径、多进程 manifest 覆盖均会形成同样外观。不能把“未观测到证据”升格为“证明没有写”。

**修法**：有副作用 intent 且无可用 manifest/条目 → `unknown` + 阻断；只有顺序明确的 intent 在 prepare 尚未开始且能证明存储完整时才能 none_applied。未知 tool/version 也 fail closed。正常运行可把 intent 和 preimage preparation 移入同一事务化执行记录，减少无证据歧义；缺证据仍不能自动通过。

### R2（严重）undo 仍未经过 guard，CLI 与库入口都能写已阻断 workspace

[cli.py:152–156](src/tianshu/cli.py#L152-L156) 仍直接 `store.undo()`，[snapshot.py:123–162](src/tianshu/snapshot.py#L123-L162) 无 log/guard 参数。

实测 `is_blocked=True` 后走当前 `store.undo()`，返回 `ok=True, restored=['note.md']`。这正是原 S2 的一个复现分支，不能标为全部修复。

**修法**：undo 使用带 log/run lease 的执行上下文，在 dispatcher 校验授权、workspace 阻断和租约；不能只给 CLI 加预检。undo 本身是写操作，须产生 intent/result/receipt。

### R3（严重）同一路径多步写入使 undo 自相冲突，undo 后日志仍声称 applied

实测同一 Run：`create_report(x=agent)` 后 `modify_text(x=changed)`。manifest 顺序是 c1 absent→agent，c2 agent→changed。undo 预检先要求当前文件等于 agent，但实际为 changed，因此 `ok=False`，不能撤销整个 Run。按 call 保存证据是对的，但按调用顺序 undo 是错的。

另测一次正常 create 后 undo：文件确实删除，但事件中的旧 result 仍为 applied，没有 undo intent/result；`recover_run()` 返回空报告，无法说明后续变化。

**修法**：同 Run undo 按调用逆序处理，同一路径只恢复调用链最早的 preimage，并以最终 after 为冲突校验基准。写 undo 事件并纳入恢复状态；旧 applied result 不应被误读为撤销后的当前状态。重复 undo、部分 undo 和 undo 中途崩溃均须有明确状态。

### R4（中等）阻断被吞成 `outcome=noop`

[loop.py:257–263](src/tianshu/loop.py#L257-L263) 未捕获 `WorkspaceBlocked`，落入通用异常后 result 为 failed/unknown；随后 [loop.py:218–224](src/tianshu/loop.py#L218-L224) 无 artifact 便给 `noop`。CLI 可能以成功语义呈现，虽然目标文件未写。

**修法**：权限拒绝与执行中断单独映射 rejected/cancelled/blocked，不能进入 noop；CLI 对 unknown 或阻断非零退出。该问题不是文件越权，但会误导 M2 任务状态机。

### R5（中等）“result/receipt 原子”只覆盖小 payload

[events.py:150–163](src/tianshu/events.py#L150-L163) 的 `append_many()` 确有 BEGIN/COMMIT，但 [events.py:114–146](src/tianshu/events.py#L114-L146) 在事务内直接写外部 blob；大 payload 文件没有 fsync，且崩溃可留下“数据库 ref 已提交、文件未持久化”的跨介质不一致。小 payload 的 result/receipt 原子成立，不能概括为全部原子性成立。

**修法**：payload 先完整写入、fsync、原子发布并校验，再在一个 DB 事务中提交引用；读取缺失 ref 要显式报 unknown。文件与 SQLite 不可能靠 BEGIN 变成同一 ACID 事务，应采用 durable evidence + reconciliation。

### PR 准入判断

1. **必须在本 PR 或前置修复 PR 解决 R1–R4**，否则 M2 的租约/审批会继承错误阻断与不可审计 undo。
2. R5 至少修复大 payload 的 durable publish 和缺失处理；不需要声称文件系统与 DB 单次原子提交。
3. `modify_text` 同内容的建议口径：`status=succeeded`（若调用本身成功），`effect_state=none_applied`，不产生 applied receipt；恢复无法确认调用完成时则保持 interrupted/unknown，不用 `failed` 代替。
4. `e30901b` 还加入了数百个 `.tmp` 下的 SQLite、报告和快照文件到提交。它们不是本次实现修改，但属于 PR 卫生问题：合入前移除并忽略测试临时产物，避免仓库膨胀和本机路径/测试数据进入源码历史。

---

## 修复状态总表（2026-11-10 复核补登）

本报告写作时（`7fd5731`）只诊断、不改代码。之后的 M1 硬化、M2 统一执行入口与
M5 复审逐条落地。下表把每条发现对应到**落实位置**与**钉住它的测试**；
「仍未覆盖」一列只写真的没有证据的部分。

判定口径：有回归测试、且做过负控（把修复回退后测试会红）才算修好；
文档里写着「已修」但找不到测试的条目，一律按未修处理。

| 编号 | 结论 | 落实位置 | 钉住它的测试 | 仍未覆盖 |
|---|---|---|---|---|
| S1 | 已修 | 恢复 v2 的唯一判据是写前证据：无证据→`none_applied`；缺清单→`unknown`+阻断；修改类目标消失→`unknown`；`before_state` 只来自证据、不再编 `absent` | `test_debug_regressions.py::test_s1_*`（4 条） | — |
| S2 | 已修 | 阻断核验下沉到执行边界（派发/执行器在改文件前按**真正要写的路径**再判）；撤销走完整派发链（dispatcher→审批→租约→凭据）；`guard.unblock` 给出解除语义 | `test_s2_blocking_is_enforced_at_dispatcher`、`test_s2_write_tool_without_log_is_refused`、`test_s2_unblock_restores_writability`、`test_r2_undo_refused_on_blocked_workspace`、`test_failclosed.py::test_block_inserted_inside_execution_guard_still_blocks_the_write` | 库调用方不传 `data_root` 时只有就地两道核验，没有跨进程线性化（`KNOWLEDGE.md` §5 已写明） |
| S3 | 已修 | `fsutil.atomic_write_bytes`：同目录临时文件 → `fsync` → `os.replace` → `fsync` 父目录，并保留原 mode；各后端写入统一走 `write_bytes_atomic` | `test_s3_atomic_write_leaves_no_partial_file`、`tests/test_backend_contract.py`（4 后端契约：证据先于字节、写失败→`unknown`+阻断） | 真实断电（页缓存→盘）未做存储级实验，见下 |
| S4 | 已修 | 撤销先整体预检（每份 preimage 的 SHA-256、路径类型、after 证据）再动一个字节；缺/坏证据零修改；逐文件原子替换；中途失败记 `partial` | `test_s4_undo_aborts_when_blob_missing`、`test_undo_rejects_corrupt_preimage_before_touching_any_file`、`test_three_writes_undo_to_original_and_middle_conflict_is_atomic` | 跨多文件不是一次事务：中途崩溃可留部分还原（逐文件都有记录、可续做），不宣称「跨文件天然原子」 |
| M1 | 已修（范围内） | `result`+`receipt` 同事务 `append_many`；大 payload 先 fsync+原子发布再提交引用；`EventTransaction` 让状态投影与事件同一次提交 | `test_m1_result_and_receipt_are_atomic`、`test_m1_large_payload_is_durable_before_reference_commit`、`tests/test_transactions.py`（7 条） | 文件系统副作用与 SQLite 不可能同一 ACID 事务：采用 durable evidence + reconciliation，不是「单事务」 |
| M2 | 已修 | `SPECS.get(tool_id) is None` → `unknown` + 阻断，不再套用「没证据=没写」 | `test_failclosed.py::test_unknown_tool_recovery_is_unknown_and_blocks` | 按 `(tool_id, version)` 的 per-version 恢复器仍未落地（当前整表 fail closed） |
| M3 | 已修 | 传输层失败抛 `ModelError`（不再伪造 `error`/`unknown` 结束原因）；`finish_reason` 是 13 值封闭集，越界即报错；未知/截断结束原因失败，不是 `noop`；usage 缺失记 `unknown_usage_calls`，不记 0 | `test_m3_provider_error_is_failure_not_noop`、`test_m3_truncated_finish_reason_is_failure`、`tests/test_model_contract.py`、`tests/test_usage.py` | 未知 usage 时预算「无法核实」是如实记录 + 限额，不是精确计量 |
| M4 | 大部分已修 | `ResourceIdentity`（设备+inode，符号链接同身份）+ `pathkeys`（NFC/大小写）+ 租约（过期、死主接管、跨进程守卫）+ 派发唯一索引 | `tests/test_leases.py`（含真子进程 `test_two_processes_one_resource`、`test_execution_guard_excludes_across_processes`）、`tests/test_evidence_keys.py` | **换 `--db` 绕过目录阻断（authority 数据根逃逸）未实现**；完整 takeover 未实现（issue #61） |
| L1 | 已修 | 注入矩阵扩到 create + 覆盖写；不变量核对**真正要写的那个文件**（不再对诱饵 `note.md` 自说自话）；坏清单/坏 blob 纳入用例 | `tests/test_recovery.py`（11 条：failpoint × 2 条写路径、100 次随机注入、四条不变量）、`test_failclosed.py` 注入矩阵、本轮新增坏清单 3 条 | — |
| L2 | 已修 | 证据按 `call_id` 保存（同一路径多次写各留一条）；撤销按调用逆序、同路径取最早 preimage；撤销写 `undo.completed` 事件留痕 | `test_r3_create_then_modify_same_path_can_be_undone`、`test_r3_undo_appends_audit_event`、`test_three_writes_undo_to_original_and_middle_conflict_is_atomic` | — |
| R1 | 已修 | 缺清单→`unknown`+阻断；**坏清单（本轮补）**→写工具 `unknown`+阻断、只读工具不受影响，不再漏出裸解析异常 | `test_r1_missing_manifest_is_unknown_not_none_applied`、`test_r1_corrupt_manifest_is_unknown_and_blocks`、`test_r1_corrupt_manifest_does_not_block_read_only_calls` | — |
| R2 | 已修 | 撤销强制 guard + 完整派发链；没有日志/租约/审批就拒绝 | `test_r2_undo_refused_on_blocked_workspace`、`test_undo_requires_guard_at_library_boundary` | — |
| R3 | 已修 | 同一路径按调用逆序撤销，冲突以最终 after 为基准；撤销写事件 | `test_r3_*`、`test_three_writes_undo_to_original_and_middle_conflict_is_atomic` | — |
| R4 | 已修 | 阻断不是普通工具失败：任务显式 `failed`，不进 `noop` | `test_r4_blocked_tool_makes_run_fail_not_noop` | — |
| R5 | 已修 | 大 payload 先 fsync+原子发布再提交引用；缺失 ref → `EventLogCorrupt`；`.tmp/` 已 ignore、无跟踪文件 | `test_m1_large_payload_is_durable_before_reference_commit`、`test_light2_missing_payload_blob_is_event_log_corrupt` | — |

### 本轮复核新发现并修掉的残留：坏清单让校验器自己先崩了

L1 当时把「未测损坏 manifest/blob」列为缺口。本轮补测发现：清单文件被截断或被写坏后，
`SnapshotStore.load()` 直接漏出 `json.JSONDecodeError`——恢复和撤销还没开始判断就先崩了，
CLI 只打印 `错误：Expecting property name...`，既没说清哪份文件坏了，也没有阻断。

口径不变：**读不懂 ≠ 没写过**。修法：

- 新增域错误 `SnapshotCorrupt`（与 `EventLogCorrupt` 同一条道理，只是介质不同：一个在
  SQLite，一个在 `artifacts/snapshots/<run>.json`）。
- 恢复侧在动手前整份核对一次：读不懂就把所有**写工具/未知工具**的 pending 判 `unknown`
  + 阻断，**不**退回「没证据=没写」；只读工具不读这份证据（本来无副作用），照旧判
  `none_applied`——过度阻断也是误报。逐条取证时清单才坏的窄窗口按同一口径处理。
- 撤销侧（`undo.plan`）明确拒绝并给出补救路径（先 `recover` 核对或手工还原、先备份），
  库入口 `SnapshotStore.undo()` 抛类型化错误；两种路径都一个字节都不动。

负控（先回退修复再跑新用例，确认会红）：

```text
FAILED tests/test_debug_regressions.py::test_r1_corrupt_manifest_is_unknown_and_blocks
FAILED tests/test_debug_regressions.py::test_undo_refuses_corrupt_manifest
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

（第三条 `test_r1_corrupt_manifest_does_not_block_read_only_calls` 钉住的是相反方向：
不能因为清单坏了就把只读工具也挡上——过度阻断同样是误报。）

修复后同一组用例全绿；全量 `1080 passed / 3 skipped`，`ruff check .` 干净。

### 仍然不成立的宣称（不要读成「已解决」）

1. **真实断电**：写入路径已有 fsync + 原子 rename，但「恢复后只可能是写前或写入内容」在
   **存储故障语义**上仍未验证（没有块设备级掉电实验）。见 [M2-verification.md](M2-verification.md) §8。
2. **Windows**：`msvcrt.locking`、`OpenProcess`、`\\?\` 路径只有实现、没有实机证据；
   `windows-latest` 上仍有命名管道握手偶发失败（issue #50）。
3. **`authority` 数据根逃逸与完整 takeover**：未实现（issue #61）。
4. **跨多文件撤销的事务性**：不宣称——逐文件原子 + 逐文件记录，不是一次事务。
