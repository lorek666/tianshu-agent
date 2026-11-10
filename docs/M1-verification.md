# M1 验收记录

> M1 目标：让 Agent 的文件操作**可追踪、可撤销、崩溃后能安全恢复**。
> 三个 PR 依次交付，每个都带验收用例。

---

## 1. 操作凭据（PR #3）

**问题**：Agent 改了什么，能不对话就查清楚吗？

一条 `Receipt` 可独立核对，字段对齐 PRD §3.5。三条硬约束：

| 约束 | 为什么 |
|---|---|
| 只有 `effect_state=applied` 才产凭据 | partial/unknown 一律不产——不能拿一张看起来完整的凭据粉饰不确定状态 |
| `before_state` 显式记 `absent` | "新建"和"读不到"必须能区分，否则回滚会搞错 |
| `verify_receipts()` 双向对账 | 有条无凭、有凭无条都要报 |

**过程中修掉的自身 bug**：写入被拒绝时 `effect_state` 被标成 `partial`。根因是用"有没有 error 字符串"推导，而不是"实际发生了什么"。改按计数推导。

## 2. 写前快照与冲突安全撤销（PR #4）

**问题**：改坏了能撤回吗？撤回会踩掉用户后来的修改吗？

| 规矩 | 说明 |
|---|---|
| 快照在**写入之前**取 | 写完再取等于没取——原内容已经没了 |
| 撤销前整体校验 | 任一文件哈希 ≠ Agent 写完时 → 判冲突，整体拒绝，一个字节不动 |
| 内容寻址 blob | 相同内容自动去重 |

新增 `modify_text`（改已有文件），`create_report` 仍只管新建、已存在就拒绝。

## 3. 崩溃恢复与故障注入（PR #5）

**问题**：进程在中间被杀，重启后怎么知道改没改？

**不猜，用磁盘现状核实。** 四个故障点在**两条受控写路径**（`create_report`
新建、`modify_text` 覆盖已存在文件）上全部注入：

| 故障点 | 期望 | 实际 |
|---|---|---|
| `after_intent`（写了 intent 未执行） | 新建目标不存在 / 覆盖写目标仍是写前内容 → `none_applied` | ✅ |
| `after_write`（写完文件未写 result） | 目标哈希 == 意图写后 → `applied` | ✅ |
| `after_result`（写完 result 未写凭据） | 结果已在，无需补 | ✅ |
| `after_receipt`（凭据已写未收尾） | 同上（M2 起 result 与凭据同事务提交，两点落在同一处） | ✅ |

**核实规则**：

- 只读工具被杀：无论何时都是 `none_applied`，不阻断
- 写工具，目标内容 == 期望哈希 → `applied`，补 result + 凭据
- 写工具，目标不存在 → `none_applied`
- 写工具，目标哈希既非期望也非写前状态 → **`unknown` + 阻断该 workspace 的后续写入**
- 参数不可重建的写工具 → `unknown` + 阻断，但**必须补一条 result**，否则 intent 永远悬空

**100 次故障注入**（四个故障点 × 两条写路径；本机实测本用例约 9.8 秒，
整个 `test_recovery.py` 约 10.9 秒）验证四条不变量：

注入矩阵（`tests/test_recovery.py::test_100_random_fault_injections`）：

- 48 次 `create_report`（新建）+ 52 次 `modify_text`（覆盖写已存在文件），
  每个（写类型, 故障点）组合 12 次，另给本轮新增的覆盖写补 4 次；随机种子固定，
  只用来打乱执行顺序——覆盖面由配平矩阵保证，不交给随机抽样（纯随机可能恰好
  漏掉某个组合）。
- 覆盖写是 `ACTION_OVERWRITE`，必经审批：子进程停在 `waiting_approval` 后，
  按 `tests/support.py::approve_pending` 的同一公开入口（`approvals.decide`
  + `resume_run`）模拟「人在 CLI 上批准」。**授权没有被绕过**——策略判定、
  审批落库、动作摘要绑定、执行侧消费一样没少，省掉的只是等人。
- 子进程不走 `cli --model scripted`（那个脚本模型只发 list_files +
  create_report，正是复审 **中-3** 指出的证据缺口），而是用公开 API 装配同一条
  run 路径（`build_stack` + `AgentLoop` + `ScriptedModel`），命令它发
  `modify_text`；`src/tianshu/cli.py` 未改，也没有新增 `--model` 模式。

不变量（A 核对的目标从 `tool.intent` 参数解析：create 取 `name`、overwrite 取
`path`，并断言就是 `report.md`；预置后从未被写的 `note.md` 反过来断言没动过）：

- A. **真实写入目标**逐字节等于「写前」或「写入内容」之一，不许半截、不许第三种；
  第二次恢复不再改变内容，也不再补事件。
- B. 每个写入 intent 恰好一条 `tool.result`（不丢不重）；有写入结果必须有写前 intent。
- C. 声称 `applied` 的，result 的 `sha256` / `after_state` / 磁盘三者一致，
  `before_state` 必须等于写前状态（新建为 `absent`）；`after_intent` 那次必须如实
  判 `none_applied`，不许把「未执行」误报成 `applied`。
- D. `applied` 恰好对应一张 receipt；未执行不产凭据（凭据不许粉饰不确定状态）。

这组断言做过负控（临时副本，未提交）：让子进程只写**半截内容** → 8 个用例红；
把 100 次注入用例的不变量 A 改回读 `note.md`（中-3 的原始缺陷）→ 该用例红。
所以「A/B/C/D 全绿」不是空转。

「0 重复副作用」在覆盖写路径上的含义就是 A + B + D：目标内容只能是写前或写后
之一、恢复不会再写一次；结果与凭据各恰好一条。

**`delete_file` 为什么不在本矩阵里**：本轮补证只回应 中-3 点名的覆盖写路径。
`delete_file` 是独立的删除类动作（`ACTION_DELETE`，同样需要审批），其恢复判据
（写后应为 `absent`）与撤销路径（按 preimage 重建）和覆盖写不是同一组断言，
留作另开的一组注入矩阵跟进。审批本身不是阻塞点——覆盖写同样需要审批，已按上面
的方式在子进程里模拟。

## 4. 实机演示

```
$ TIANSHU_FAILPOINT=after_write tianshu run ...      # 写完文件后硬退
退出码: 77
文件是否存在: 是

$ tianshu recover --run-id run_d23d26262470 ...
已确认执行 : ['c2']
无法核实   : （无）

$ 凭据数: 1 | 对账: 一致
```

## 5. 测试总览

```
uv run pytest       →  63 passed（独立复验与修复后）
uv run ruff check . →  All checks passed
```

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_acceptance.py` | 13 | A1–A8 + A7 三形态 |
| `test_usage.py` | 9 | 三家 usage 字段、缺失、bool 冒充 |
| `test_receipts.py` | 6 | 凭据生成、参数绑定、拒绝、篡改 |
| `test_snapshot.py` | 6 | 快照、撤销、冲突、去重 |
| `test_recovery.py` | 11 | 四故障点 ×（新建 + 覆盖写）、幂等、阻断、100 次注入（create 48 + overwrite 52） |

（`63 passed` 是 M1 验收当时的全量快照；上表只列 M1 直接相关文件，后续里程碑
新增的测试不在本表。`test_recovery.py` 一行已按 中-3 补证更新：四个故障点 ×
两条写路径 = 8 个参数化用例，另加幂等、unknown 阻断、100 次注入各 1 个——该文件
当前为 **11 passed**，全量以最新 CI 为准。）

## 6. 仍未验证

| 项 | 原因 |
|---|---|
| Windows 平台 | 只有 macOS |
| 真实断电（文件系统层缓冲） | 用 `os._exit` 模拟，未测 fsync 边界 |
| 多进程并发写同一 workspace | M1 是单 writer，未做锁 |
| 大 workspace（数百文件）性能 | 未压测 |
| undo 中途崩溃的多文件一致性 | 预检阶段保证缺/坏证据零修改；执行中异常仍可能部分还原，完整 undo journal 由 M2 统一执行入口实现，不能宣称已解决 |
| 并发调用与权威数据目录 | 当前 guard 只在指定数据库上检查；换库/多进程租约由 M2 实现 |

## 7. 独立复验与修复记录（PR #6）

`docs/DEBUG-M0M1.md` 保存逐条反例与复验过程。最终由独立复核直接补齐：undo 必须提供 guard；三层写入逆序撤销与中间链冲突预检；preimage 哈希预检；最早启动窗口安全初始化；未知/截断模型结束原因失败；大 payload 先 fsync + 原子发布再提交引用。

实际 CLI 验证：同一文件 `create → modify(v2) → modify(v3)` 后 undo 成功回到文件不存在，事件含 `undo.completed`，receipt 对账一致。

**范围声明**：这里证明的是进程中断下受控工具路径的恢复与证据约束，不是物理断电或任意 shell 的安全沙箱。外部 blob 与 SQLite 是 durable publish + 引用，不是跨介质 ACID 事务。
