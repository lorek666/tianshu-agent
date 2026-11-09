# PR G 落地记录：证据键补 workspace 根（issue #17）

> 对应 issue #17 与 M3 接缝提案 §5 第 4、7 条（`m3-backend-seam-proposal.md`）。
> 本文记录**为什么这么修**，以及哪些地方仍然不承诺。

## 1. 问题（实测）

- `snapshot.Change.path` 只有 workspace 相对路径。`Task.workspaces` 是数组，
  一个 Run 绑两个 workspace 时，`wsA/a.md` 与 `wsB/a.md` 撞成一条证据。
- `undo` 只拿一个 `ws` 去 `resolve`：撤销 wsA 的变更会写进**调用方传进来的**
  那个目录。写错目录是用户可感知的数据破坏（F11 是 P0）。
- 凭据自称"不看对话就能核对"，实际 `action` 是 tool_id、`resource` 只有相对
  路径、`evidence` 是 `"tool.result:succeeded"` 这种合成串、`verify_receipts`
  只看日志不看磁盘。

## 2. 修法

### 2.1 证据键 = (workspace 根, 相对路径)

```python
Change { call_id, root, path, before_state, expected_after, blob_ref }
#               ^^^^ 绝对、已规范化；path 仍是 `/` 分隔的相对路径
```

- `root` 来自**已核验的派发票据**（`AdmittedCall.workspace_root`，由
  `Dispatcher._resolve_target` 按"最深的覆盖绑定"选出）。`ToolContext` 自己
  只有相对路径，没有票据时 `prewrite()` 直接拒绝——不猜一个默认目录。
- 撤销按每条记录自己的 `root` 落盘；`undo(run_id, ws)` 的 `ws` 从"解析落盘
  目录"降级为**范围筛选**（只撤这个目录下的变更）。
- manifest 版本 2 → 3。v2 仍可读（`root = None`），但**不可撤销**。

### 2.2 为什么不用 inode（`ResourceIdentity` 的 `st_dev:st_ino`）当键

inode 在备份恢复、目录搬运、跨机器之后都会变，而证据要能活过这些事；memfs
后端根本没有 inode。证据要的是"人还能照着找回去"，不是"当下唯一"。目录身份
仍然由 `ResourceIdentity` 负责（租约/授权用），两件事不混。

### 2.3 旧格式：拒绝优于猜测

- `SnapshotStore.undo()` 遇到任何 `root is None` 的记录 → **整体拒绝**，一个
  字节都不改；问题列表说清是哪几条、为什么拒绝、可以怎么处置。
- CLI `undo` 在动手之前就返回退出码 2，并明确写出"已忽略 `--workspace`：
  旧记录没有根，拿它顶上就是猜"。
- 没有"单 workspace 兼容分支"。宁可撤销失败，也不能写错目录。

### 2.4 凭据：从"自洽的记录"变成"可核对的证据"

| 字段 | 变化 |
|---|---|
| `action` | 真实动作（`create`/`overwrite`/`delete`），不再是 tool_id |
| `root` | workspace 根（新增） |
| `resource` | **绝对路径**（原先是相对路径，跨 workspace 不唯一） |
| `intent_seq` | `tool.intent` 事件 seq（新增，args_hash 由它担保） |
| `blob_ref` | 写前快照 blob（新增） |
| `evidence` | 结构化引用：`{kind: result/intent/blob}`，逐条可解析，不再是合成串 |

`verify_receipts` 现在做四件事：凭据 ⟷ `tool.result`；证据引用逐条解析
（intent seq 真的是本 run 的 intent 且 args_hash 一致、blob 内容哈希等于
`before_state`）；凭据 ⟷ 快照 blob；**凭据 ⟷ 磁盘现状**（`after_state`）。
旧格式凭据会被明确报成"无法独立核对"，不默认通过。

两个刻意的判断：

1. **后端不一致时不假装核对**：凭据记的后端与当前后端不同，直接报"无法在当前
   后端核对磁盘"，而不是拿本地盘去比虚拟盘的哈希。
2. **已记录的撤销不算篡改**：`undo.completed` 里带上了 `{root, path}` 逐条
   记录，`verify_receipts` 只认"内容正好回到 `before_state`"的那一条。否则
   每一次合法撤销都会永久红着，"与磁盘不一致"就变成没人看的噪音。

## 3. 恢复（recover）同步改了两处

- 看盘用**证据里的根**，不是调用方传进来的目录。旧格式记录才退回显式
  `--workspace`（那只是"用户说了在哪看"，不参与写盘决策）。
- `recovery.blocked` 落在**真正无法核实的那个目录**上。原来的实现一律阻断调用
  方的 ws：多 workspace 下会挡错目录，而状态不明的目录还开着——比不阻断更糟。
  同时 `_foreign_lease`（别人正在写这条路径）也改成看证据指向的目录。

## 4. 没做 / 不承诺

1. **多 workspace 下的相对路径解析规则没变**：主循环仍把相对路径解析到"第一个
   可写绑定"（M2 已知限制，`docs/M2-verification.md` §5 第 1 条）。本次修的是
   **证据与撤销**，不是"模型怎么指到第二个目录"；用绝对路径今天就能到，
   M5 桌面端支持多 workspace 后这条才会真正被日常触发。
2. **旧格式没有自动迁移**。可以写一个迁移工具（按目录逐个 `recover` 核对后补
   `root`），但**迁移必须由人确认目录归属**，程序猜就等于把这次的 bug 换个地方
   重演。目前只提供拒绝信息与处置建议。
3. **数据库 schema 没动**：凭据/快照都是 payload 与宿主文件，没有加列、没有加
   索引，所以 M2 验收 §5 第 18 条的"旧库迁移"问题不因本 PR 恶化（也没被它修好）。
4. **`verify_receipts` 仍是按 run 核对**：跨 run 的全局核对（比如"这条路径上所有
   凭据的时间线")没做。
