# 内容引用与分页读取（PRD §5.2.3「有界摘要 + 内容引用」）

- 立据：`docs/PRD-v1.md` §5.2.3、§2.3（内容寻址现状）；施工规格
  [`SPEC-G1-CLOSURE-2026-11-11.md`](SPEC-G1-CLOSURE-2026-11-11.md) §3。
- 状态：**已实现**（`contents.py` + `context.py` 占位 + `loop._persist_spills` 授权 +
  `read_content` 工具）。
- 判据：`tests/test_content_refs.py`（12 条，存储层）+ `tests/test_content_read.py`
  （5 条，端到端）；负控 **12/12** 实测变红（见 §6）。

## 1 缺口是什么

上下文编译器早就会做有界化：单条工具输出超过阈值（默认 8 KiB）就换成占位，原文写进
内容寻址 blob（`context.py` 的 spills + `loop._persist_spills`）。但它只完成了一半：

| 要求（PRD §5.2.3） | 之前 | 现在 |
|---|---|---|
| 大结果以**有界摘要**返回 | 有界（占位） | 不变 |
| 结果带**内容引用** | 占位只说"已落盘"，**没有引用** | 占位给出 `content://sha256:<64hex>` |
| **分页读取**引用 | 没有读取工具 | `read_content`（offset / length / next_offset） |
| 读取**验证任务可见范围** | 没有授权记录 | `content_grants(digest, task_id)`，读取时核 |
| 引用可校验 | 无 | 读时**重算 sha256** 比对 |

## 2 形状

引用：`content://sha256:<64 hex>`（与 `contents.parse_ref()` 同源，封闭、严格）。
占位文本（占位是模型能看见的唯一线索）：

```
[tool-output-spilled]
sha256: <digest>
ref: content://sha256:<digest>
原始字节数: N
头 512 字节预览: ...
尾 512 字节预览: ...
完整内容已按内容寻址落盘。需要原文时用 read_content 分页读取：
{"ref": "content://sha256:<digest>"}（可选 offset / length）。
本占位由 compile_context 确定性生成，不含时间戳。
```

`read_content` 的 schema（封闭方言，执行前按 `toolschema` 校验）：

```
{ ref: string(pattern ^content://sha256:[0-9a-f]{64}$),
  offset: integer(minimum 0),
  length: integer(1..65536) }
required: [ref]
```

返回：`{ref, digest, offset, next_offset, total_size, bytes, data}`；`next_offset` 为
`null` 表示读完。

## 3 三个关键设计

### 3.1 去重 ≠ 共享可见

内容寻址会去重：同一份内容被两个任务引用，`content_objects` 只有**一行**。所以可见性
**不能**记在对象上——那会让一个任务看到另一个任务的原文。授权记在
`content_grants(digest, task_id)`，由主循环在 **spill 发生时**按当前 Run 的任务写入。

### 3.2 引用是校验和，不是文件名

blob 目录是普通文件，能改它的人能改内容。读取时重算 sha256 与引用比对，不符就拒绝
（`ContentDigestMismatch`）。这与事件 `payload_ref + payload_hash` 是同一条思路。

### 3.3 分页不切半个字符

分页按字节切片，但**不把多字节字符切一半**交给模型：切在字符中间时把这一页收到上一个
字符边界，`next_offset` 指到整字符；连一个整字符都装不下就明确报错（让调用方增大
`length`），不返回半截 UTF-8。

## 4 读工具为什么不过派发（与"同一套准入路径"不冲突）

`Dispatcher._typecheck()` 的既有口径是：**只读动作不经过派发**（没有可授权的副作用），
`read_text` / `list_files` / `memory.search` 都在这条路上。`read_content` 沿用同一口径：

- 同样过**白名单注册**（`tools.SPECS`，注册期方言校验 + `description_digest`）、
  同样过 **schema 参数校验**（非法引用在准入阶段就被拒，见 §6 的 NC6 与
  `test_content_read.py::test_read_content_rejects_malformed_reference`）、
  结果同样落 **`tool.result`** 留痕；
- 差别只在"可见范围"这个判据由**内容存储**执行——除了它没有人知道
  `(digest, task_id)` 的授权行。

**与规格的差异（如实登记）**：`SPEC-G1-CLOSURE` §3 原计划加一种 `content` 资源种类
并让策略层做"双闸"。实现时确认了既有的"只读不过派发"口径，按它落地更一致——给一个
永远走不到派发的资源种类只会多一份没人用的契约。规格已在 §3 改成这个口径。

## 5 边界

| 不做 | 理由 |
|---|---|
| 保留期 / 垃圾回收 | 属备份与保留期轮；这里只写不删 |
| 跨机器同步 | blob 目录是本机的，引用只在同一套数据根内有效 |
| 透明恢复工具**自己**截断的内容 | `read_text` 截断的那部分**从来没落盘**（PRD §2.3 明写） |
| 每工具 output 子契约（C2 后续） | 不改 `tool.result.payload` 的 schema：spill 是上下文层行为，事件仍保留原文（> 256 KiB 落 ref 是既有规则） |
| 引用删除后的读 | 读不到就是 `ContentMissing`（有授权但内容不在），不猜 |

## 6 负控（回退即红，实测 12/12）

存储层（`tests/test_content_refs.py`，6 项）：

| 回退 | 变红的判据 |
|---|---|
| 读取不校验任务可见范围 | 跨任务读 |
| 读取不复核 sha256 | 篡改 blob |
| 登记不写授权 | 去重/授权用例（8 条） |
| 不分页 | 分页重建 |
| 引用形状放水 | 引用封闭性 |
| 非 UTF-8 静默 `replace` | 非 UTF-8 拒绝 |

接线层（`tests/test_content_read.py`，6 项）：

| 回退 | 变红的判据 |
|---|---|
| 占位不给可用引用 | 摘要/端到端（3 条） |
| 主循环不登记授权 | 端到端（3 条） |
| 读取用空任务身份（丢可见范围） | 跨任务（2 条） |
| 忽略分页参数 | 分页续读 |
| 阅读器不校验 sha256 | 篡改 blob（存储层） |
| 工具未注册 | 端到端（collect 期就报错） |
