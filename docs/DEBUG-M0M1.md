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
