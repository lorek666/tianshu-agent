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

**不猜，用磁盘现状核实。** 四个故障点全部注入：

| 故障点 | 期望 | 实际 |
|---|---|---|
| `after_intent`（写了 intent 未执行） | 文件不存在 → `none_applied` | ✅ |
| `after_write`（写完文件未写 result） | 文件存在且哈希匹配 → `applied` | ✅ |
| `after_result`（写完 result 未写凭据） | 结果已在，无需补 | ✅ |
| `after_receipt`（凭据已写未收尾） | 同上 | ✅ |

**核实规则**：

- 只读工具被杀：无论何时都是 `none_applied`，不阻断
- 写工具，目标内容 == 期望哈希 → `applied`，补 result + 凭据
- 写工具，目标不存在 → `none_applied`
- 写工具，目标哈希既非期望也非写前状态 → **`unknown` + 阻断该 workspace 的后续写入**
- 参数不可重建的写工具 → `unknown` + 阻断，但**必须补一条 result**，否则 intent 永远悬空

**100 次随机注入**（随机故障点 × 100 用例，4.8 秒跑完）验证三条不变量：

- A. 恢复后文件内容必须是「写前」或「写入内容」之一，不能半截
- B. 每个 intent 恰好一条 result，不丢不重
- C. 没有任何一次把「未执行」误报成 `applied`

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
| `test_recovery.py` | 7 | 四故障点、幂等、阻断、100 次注入 |

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
