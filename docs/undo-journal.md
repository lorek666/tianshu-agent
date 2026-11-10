# 跨多文件撤销的事务性（T3 · 写前意图日志 + 幂等续做）

> 对应：`docs/DEBUG-M0M1.md`「仍然不成立的宣称 4」——撤销逐文件原子、逐文件记录，
> 中途崩溃会留下「改了一半、日志看起来完好」的状态，不宣称一次事务。
> 规格：`docs/SPEC-RESIDUALS-M0M1.md` §T3。基线：`main @ 540c543`。
> 分支：`feat/undo-journal`；worktree：`/Volumes/Date/ts-wt-undo`。

## 1. 问题

`SnapshotStore.undo` 的执行循环是：

```
预检全部通过 → for c in reversed(changes): apply(c) → 记 report
```

每条动作本身是原子的（派发/审批/写入/结果各自成事务），但**跨动作没有边界**：

- 第 2 个文件还没动手时进程被 kill，磁盘上是「撤了一半」，事件日志里只有
  `undo.failed` 都不会有（`os._exit` 不给异常处理机会）；
- 下一次 `undo` 若从头再来，会把**已经还原过的文件**再还原/再删除一次，并覆盖
  用户在这期间的手改；
- 若假装「什么都没发生」，那三个文件的状态就永远对不上账。

所以需要的是**可续做的撤销**，不是假装两阶段提交。

## 2. 设计

### 2.1 意图日志（`artifacts/undo/<run_id>.json`）

撤销**动手前**原子落盘完整计划；每完成一个动作原子重写：

```json
{
  "version": 1,
  "run_id": "run_xxx",
  "control_run": "run_ctl_xxx",
  "status": "in_progress",            // in_progress | completed
  "items": [                          // 执行顺序 = 调用序列的逆序
    {"root": "/abs/ws", "path": "c.md", "call_id": "c3",
     "before_state": "<sha256>", "expected_after": "<sha256>", "action": "restore"}
  ],
  "done": [                           // 已完成的前缀，post_state 是动作后实测状态
    {"root": "/abs/ws", "path": "c.md", "call_id": "c3",
     "before_state": "<sha256>", "expected_after": "<sha256>",
     "action": "restore", "post_state": "<sha256>"}
  ]
}
```

- 写失败就不动手（`begin` 在预检之后、第一次写盘之前）；
- `done` 只追加、且必须是 `items` 的前缀；`status=completed` 必须 `done==items`；
- journal 是**证据**不是事实源：文件当前状态才是判据，journal 只回答「做到哪一步」；
- 损坏（坏 JSON / 版本不认识 / done 与 items 对不上 / 路径越界）→ `UndoJournalCorrupt`，
  拒绝续做，绝不默认为「空 journal 从头来」。

### 2.2 崩溃后可续做（`SnapshotStore._resume_undo` / `_plan_resume`）

磁盘是所有**已生效**动作之后的状态，所以核对要分两段：

- **done 段倒着核**：磁盘必须等于最后一个 done 的 `post_state`；相邻 done 的
  `post_state` / `expected_after` 必须首尾相接（同一路径多次写入的链路）；
- **剩余段正着核**：应当仍等于 `expected_after`（用户改过 → 拒绝，不覆盖）；
  - 若恰好等于 `before_state`：说明上一个动作已完成但 journal 没来得及记
    （崩在「动作生效 → 原子重写 journal」之间）→ 按**已生效**补记一条 done，
    **不重写文件**。一次崩溃最多卡在一个动作中间，所以只对全局第一个未记录项
    做这个判别；
- 核对全部通过才动手；任何不一致 → `UndoJournalConflict`，**零修改**地拒绝；
- 续做从断点继续，绝不从头再来（已 done 的路径不会再被写一次）。

### 2.3 完成事件只写一次

- journal 全部完成才置 `status=completed`；
- `undo.completed` 在 journal 之后才写（`failpoint("after_undo_journal_complete")`
  正好落在这个窗口用于测试）；
- 已完成但事件缺失 → 核对磁盘后**幂等补写**（先查已有事件，`idem_key` 兜底）；
- 事件已存在但 journal 仍是 `in_progress` → 两者矛盾，按损坏拒绝（不补、不续做）；
- journal 已完成但磁盘又被改过 → 拒绝补事件（补一条「已完成」会把不一致盖住）。

### 2.4 控制 Run 的续用

撤销本身走「控制 Run → 派发 → 审批」。崩溃会把控制 Run、作业 claim、租约留在
`running` 状态。续做时：

1. 旧控制 Run 仍活着且作业被**活着的**实例持有 → `UndoRefused`（拒绝并发续做）；
2. 持有者已死 → 重新 `claim` 作业、重新拿租约（stale steal），**复用同一个控制
   Run**，完成事件里的 `control_run` 保持稳定；
3. 旧控制 Run 已收尾/缺失/作业已终结 → 先尽力收尾残留，再另开一个控制 Run，
   并把新 `control_run` 原子写回 journal。

`recovery.py` 属另一工作流（T2），**没有改**。T3 只提供两件事给 lead 接进 recover：

```python
outstanding(data_root, *, log=None) -> list[OutstandingUndo]
resume(stack, run_id, *, scope=None) -> undo.UndoOutcome
read(data_root, run_id) -> UndoJournal | None     # 需要明细时
```

`OutstandingUndo` 字段：`run_id / journal_path / status(in_progress|completed|corrupt) /
control_run / roots / total / done_count / remaining / corrupt / needs_completion_event /
error`；`resumable == not corrupt`。

- recover 对 `status=in_progress` 或 `corrupt` 的条目：报告 + 阻断 `roots`
  （corrupt 时 `roots` 为空，用 recover 调用方显式给的 `--workspace`）；
- 传 `log` 时，`completed` 但缺 `undo.completed` 事件的条目也会列出
  （`needs_completion_event=True`，只需 `resume` 补事件，不动文件）。

## 3. 测试证据

```
$ PYTHONPATH=/Volumes/Date/ts-wt-undo/src /Volumes/Date/天枢Agent/.venv/bin/python \
    -m pytest -o addopts="" -q tests/test_undo_journal.py
.......                                                                  [100%]
7 passed in 0.84s
```

| 用例 | 钉住的行为 |
|---|---|
| `test_crash_between_actions_resumes_from_breakpoint` | 3 文件撤销在第 2 个动作前 `os._exit(77)` → `in_progress`、done 项保持、outstanding 可识别、被阻断时续做零修改、解除阻断后从断点续做 → 三份回到写前字节、`undo.completed` 恰好一条、重复续做不补第二条 |
| `test_completed_journal_missing_event_is_backfilled_once` | journal 已完成但事件缺失 → 幂等补写一条、不动文件；再续做仍是一条 |
| `test_corrupt_journal_refuses_resume_with_zero_modification` | journal 损坏 → `UndoJournalCorrupt` / `UndoRefused(code=2)`、零修改、损坏证据不被改写；库入口同样 fail-closed；outstanding 标 `corrupt` |
| `test_conflict_on_done_item_refuses_resume` | done 项被改过（磁盘 ≠ post_state）→ 拒绝续做、零修改 |
| `test_conflict_on_remaining_item_refuses_resume` | 剩余项被改成第三种内容 → 拒绝续做、零修改（已完成项也不重写） |
| `test_completed_journal_then_disk_changed_refuses_backfill` | journal 已完成但磁盘又被改 → 拒绝补事件、零修改 |
| `test_completed_event_without_completed_journal_is_refused` | 事件说完成、journal 未完成 → 矛盾，按损坏拒绝、零修改 |

回归：`tests/test_snapshot.py` 现有语义（预检、partial、none_applied、缺 blob、
冲突原子性）全部保持绿；`tests/test_debug_regressions.py` 的 undo 用例、
`test_dispatch` / `test_run_control` / `test_approval_reject` / `test_recovery`
一并跑过：`124 passed in 16.70s`。

崩溃注入是真子进程（`subprocess` + `TIANSHU_FAILPOINT`，`crashpoint.failpoint`
写 stderr 后 `os._exit(77)`），断言含 `[failpoint] <name>` 标记，避免「77 是别的原因」。

## 4. 负控（证明用例不是恒真）

两次都只改产品代码、不改测试，跑同一个崩溃续做用例，确认变红；之后恢复。

### 负控①：去掉 journal 写入

把 `JournalStore._save` 改成 `return`（journal 永不存在）：

```
$ pytest -q tests/test_undo_journal.py::test_crash_between_actions_resumes_from_breakpoint
    journal = undo_journal.read(tmp_path, RUN)
>   assert journal is not None
E   assert None is not None
FAILED tests/test_undo_journal.py::test_crash_between_actions_resumes_from_breakpoint
1 failed in 0.17s
```

### 负控②：去掉「已有 journal → 续做」的检测

让 `SnapshotStore.undo` 永远走「从头再来」分支（`journal = None`）：

```
$ pytest -q tests/test_undo_journal.py::test_crash_between_actions_resumes_from_breakpoint
>   assert outcome.ok, outcome.problems
E   AssertionError: ['预检未通过，未做任何修改']
E   assert False
E    +  where False = UndoOutcome(... conflicts=['...c.md：当前内容与 Agent 写完时不一致...'], effect='none_applied', ...)
FAILED tests/test_undo_journal.py::test_crash_between_actions_resumes_from_breakpoint
1 failed in 0.15s
```

红的样子正是「从头再来会覆盖/重复撤销」被预检拦住——但恢复路径已经无法完成，
所以测试必须红。

## 5. 边界与仍不成立的部分

- **recover 接线未做**：`recovery.py` 属 T2。本分支只提供 `outstanding` / `resume`，
  由 lead 合并后把「未完成撤销 → 阻断 + 报告」接进 `tianshu recover` 并补集成测试。
  本轮测试里「工作区阻断」是用追加 `recovery.blocked` 事件模拟 recover 的行为，
  不是 T3 在产品代码里自动写阻断。
- **journal 只防崩溃，不防篡改**：它是本机审计材料，没有签名/加密；能改它的人
  本来也能改工作区。版本不认识一律拒绝，不按旧语义猜。
- **并发续做**：同一数据根上活着的另一实例持有作业时拒绝（不抢）；单用户假设不变。
- **`cli.py` 未改**：`tianshu undo --run-id` 已经是「先 `plan_undo` 预演、加
  `--approve` 再 `apply_undo`」，journal 感知在这两个库函数内部，CLI 无需改动。
  已完成 journal 的纯事件补写会走 `apply_undo`（需要 `--approve` 触发），
  `plan_undo` 预演为空列表——这是可接受的边角语义，已在代码注释里写明。
- **尚未做真实掉电**：本用例用 `os._exit`，不验证「journal 与文件写入谁先持久化」
  的存储层语义；那是 T1（powercut）的工作。

## 7. 恢复侧接线（合并后由 lead 补）

本工作流（T3）只提供库契约，`recovery.py` 属另一工作流，**没有**在原分支里接线。
合并后的接线很短（`recovery.recover_run` 内、与恢复同一次执行守卫）：

- 有 `data_root` 时调 `undo_journal.outstanding(data_root, log=log)`；
- `in_progress` → 把 journal 里记录的每个 root 阻断（`recovery.blocked` + 用户级权威根），
  并在报告 `problems` 里点名 journal 路径与 `tianshu undo --run-id <ID>` 续做建议；
- `corrupt` → 同样阻断（roots 读不出来时退到调用方显式给的 `--workspace`），
  明确要求人工核对，绝不当成"没有撤销"；
- `completed`（只差补事件）→ **不**阻断：文件已经全部还原，没有"改了一半"的风险；
- 扫描器自己抛异常 → 报告写明"无法确认是否存在未完成的撤销"，按最坏情况处理。

阻断是 journal 的**投影**，不是事实源：`guard.unblock` 可以解除，但只要 journal 还在
`in_progress`，下一次恢复会重新挡住——这条由 `tests/test_undo_recovery.py` 钉住
（5 条：阻断、损坏、completed 不阻断、解除后再恢复仍挡、状态翻转后阻断消失）。

负控（把接线去掉、其它不动）：4 failed / 1 passed，红在
`test_unfinished_undo_blocks_workspace`、`test_corrupt_journal_blocks_and_asks_for_human`、
`test_block_is_a_projection_of_the_journal`、`test_in_progress_status_is_what_blocks`。
