# F5 · 上下文编译

PRD §7 P0 / §7 功能表 F5 / 附录 B 第 8 条。实现在 `src/tianshu/context.py`，
接线在 `src/tianshu/loop.py`，判定在 `src/tianshu/replay.py`。

## 1. 问题

F5 之前 `_model_step` 每一轮都把**全量历史**发给模型：

```python
resp = self._model.complete(progress.messages, tool_manifest())
```

一个 run 走得够久，上下文必然超过模型窗口，得到的是一次 400——而 400 发生在
"钱已经花到一半"的时候。更糟的是：即使把历史压下来，"当时到底发了什么"这个
问题在事件日志里没有答案，回放只能验证**完整历史**的哈希，无法验证某一次
真的发出去的那份视图。

F5 同时解决两件事：

1. 每轮把完整历史编译成"放得下、且不丢关键信息"的视图；
2. 把编译的**入参与产物身份**记进 `model.intent`，让回放能重算出同一个视图。

## 2. 接口

```python
BUDGET_VERSION = "ctx-v1"
TOOL_OUTPUT_SPILL_BYTES = 8 * 1024

@dataclass(frozen=True)
class ContextBudget:
    max_bytes: int
    spill_threshold: int = TOOL_OUTPUT_SPILL_BYTES

@dataclass(frozen=True)
class ContextView:
    messages: list[dict]           # 实际发给模型的
    digest: str                    # sha256(canonical_bytes(messages))
    source_seqs: list[int]         # 活下来的消息来自哪些事件 seq
    dropped_seqs: list[int]        # 被压掉的
    spills: list[tuple[str, bytes]]  # (sha256, 原文) 待落盘；compile 自己不写盘
    compacted: bool

def compile_context(messages, source_seqs, budget) -> ContextView
```

`compacted` 的含义是"视图与输入不完全相同"，即发生了落盘替换**或**整段丢弃。
它不区分两者——要看具体做了什么，读 `spills` / `dropped_seqs`。

## 3. 三个设计决定

### 3.1 预算是字节，不是 token

本项目零第三方依赖，手上没有 tokenizer。任何"估算 token"都是假装知道一个它
其实不知道的数：按"4 字符 ≈ 1 token"估算，在中文（3 字节/字）与 emoji
（4 字节/个）上会偏得离谱——既防不住 400，又让"这次为什么被压"无法解释。

字节数是精确、可复现的量：同一串 UTF-8 文本，任何机器上 `len(encode())` 都
一样，中文 3 字节、emoji 4 字节不用猜。**代价**是我们不承诺"一定不超过模型的
token 上限"：1 MiB 字节预算对常见模型是数十万 token 的量级，它是个宽松的护栏，
不是精确的窗口计算。要精确就该接真 tokenizer，那是另一个决定。

### 3.2 压缩是确定性摘要，不是让模型总结

让模型总结历史，等于在回放链路里放一个自由变量：同一个 run 回放时总结内容
会随采样、模型版本、网关变化，`messages_hash` 再也对不上——"事件日志足以重建
终态"这条契约就降级成"大概能重建"。

所以压缩产物只有两种，都由代码确定性地生成：

- **占位**：`[tool-output-spilled]` + 原文 sha256 + 精确字节数 + 头尾各 512
  字节预览。原文一字不差地落盘，需要时可取回。
- **说明消息**：`[context-compacted]` + 丢弃条数 + 被丢事件的 seq 列表。它是
  对"这里少了一段"的如实交代，不是对内容的复述。

两者都不含时间戳、随机数、模型输出，因此同输入必得同输出。

### 3.3 compile 是纯函数

不读时钟、不取随机数、不做 I/O。要落盘的原文以 `spills` 清单返回，由调用方在
发请求前写入内容寻址 blob。这样"编译"和"持久化"两个失败模式不会纠缠在一起：
编译失败（放不下）时不会留下半份磁盘状态。

## 4. 分区预算的顺序

1. **不可让前缀**：`system` 与紧随其后的首条 `user`（任务陈述）永不动。它们
   放不下就抛 `ContextOverflow`——绝不截断任务陈述，那等于让模型解一道被改过的题。
2. **落盘**：单条工具输出超过 `spill_threshold`（默认 8 KiB）→ 原文进 `spills`，
   内容换成占位。这一步与总预算无关：超阈值就替换。
3. **降级预览**：还超预算时，从**最旧的往返**开始，把它的工具输出换成更短的
   预览（`[tool-output-previewed ...]`，头 160 字节）。预览里的 sha256 与字节数
   仍然指向**原文**，不是指向上一层占位文本。
4. **整段丢弃**：预览到不能再短还超，就把这段往返整段丢掉，并把它的 seq 记入
   `dropped_seqs`。
5. **一条说明**：所有被丢的段合并成**一条**说明消息，放在不可让前缀之后
   （也就是第一段被丢的往返原来的位置）。丢得越多说明越长，所以列举最多 32 个
   seq，其余按"…等共 N 条"计数——说明本身的字节数必须有界，否则预算可能不收敛。

「往返」= 一条 `assistant` 消息加上紧跟其后的工具结果。**整段丢，不拆对**：
把 `tool_calls` 和它的 `tool` 结果拆开，OpenAI 与 Anthropic 都会直接 400。
`replay`/测试里的判据是"视图里活着的 `tool_calls` 与 `tool` 结果集合相等"。

只剩 system + 任务 + 说明仍然超预算时，同样抛 `ContextOverflow`：那说明给出的
预算根本容不下一次对话，继续只会给出一个"看起来跑了"的假象。

## 5. 预算内恒等（兼容性红线）

当没有任何工具输出超过阈值、且总字节 ≤ `max_bytes` 时：

- 返回的 `messages` **就是入参那个对象**（深相等，甚至是同一个 list）；
- `digest == sha256(canonical_bytes(messages))`；
- `spills == []`、`dropped_seqs == []`、`compacted is False`。

这不是优化，是兼容性要求。`Limits.context_budget_bytes` 默认 1 MiB，日常 run
远到不了，于是：

- 新日志的 `messages_hash` 与 F5 之前的数值**完全相同**；
- 历史事件日志、既有 fixture、514 条基线测试都不需要迁移；
- `replay` 的新旧两条路径在同一份输入上得到同一个数。

## 6. loop 接线

`_model_step` 的顺序：

1. `compile_context(progress.messages, progress.message_seqs, budget)`；
2. `ContextOverflow` → 记一条 `model.error`（`上下文预算不足：…`）并 `stop`，
   不写 `model.intent`（还没发出去，就不该留一条指向模型的意图）；
3. `_persist_spills(view.spills)` → `artifacts/blobs/<sha256>`（复用
   `SnapshotStore.blob_dir` 与 `fsutil.atomic_write_bytes`，同内容只存一份）；
   没有可用 blob 目录时明确失败，而不是让占位撒谎；
4. `model.intent` 的 payload：`messages_hash = view.digest`，并新增
   `context = {budget_version, max_bytes, spill_threshold, source_seqs, dropped_seqs}`。
   **不新增事件类型**；
5. `self._model.complete(view.messages, tool_manifest())`。

消息 → seq 溯源：`_Progress.message_seqs` 与 `messages` 平行，持久化进
`RunCursor.message_seqs`（带默认值，旧游标缺键即空列表，反序列化不炸）。
写入点有三处，都保证顺序与消息追加顺序一致：

- 新建进度：两条 `RUN_START_SEQ`（`run.start` 派生，没有独立消息事件；
  seq 从 1 开始，0 不会与真实事件冲突）；
- `_model_step`：`model.result` 与每条 `tool.not_dispatched` 的 seq 在**同一个
  事务里**拿到并补齐，因此游标里的 seq 列表与消息列表始终等长；
- `_absorb`：工具结果的 seq 取自 `_index_events` 的索引、`_record_result` 的
  写入返回值，或回事件里查一次；查不到记 `RUN_START_SEQ`（来源不明），不猜。

`_Progress.of` 发现长度不一致时把归因置空，改由 `message_source_seqs(events)`
从事件重建——这条重建规则与 `replay` 共用同一份实现，所以"循环写的归因"和
"回放算的归因"不可能分叉。

## 7. replay 兼容

`model.intent.payload` 带 `context` 时：

```
budget = budget_from_payload(context)
view   = compile_context(重建出的 messages, message_source_seqs(events)[:len(messages)], budget)
```

比对三样：`messages_hash == view.digest`、`context.source_seqs == view.source_seqs`、
`context.dropped_seqs == view.dropped_seqs`。任何一样不符都记 problem。

不带 `context`（M0–M3 的旧日志）时维持原行为：直接比
`messages_hash == sha256(canonical_bytes(messages))`。

回归证据：真实 run（`--model scripted`，3 轮，含一次 `create_report`）之后
`python -m tianshu replay --run-id <id>` 输出"结论：通过 / 校验 3 次 messages_hash"。

## 8. 测试钉住了什么

`tests/test_context.py`（41 条）：

| 主题 | 用例要点 |
|---|---|
| 恒等性 | 预算内同一对象、digest、无 spill、`compacted is False`；恰好 8 KiB 不落盘、8193 落盘 |
| 同输入同输出 | 重复调用逐字段相同；跨进程 + 三种 `PYTHONHASHSEED` 的 digest/归因一致 |
| 8 KiB 落盘 | 占位含 sha256/字节数/头尾预览；`spills` 可还原原文；内容寻址去重 |
| 字节计数 | 3000 汉字 = 9000 字节、3000 emoji = 12000 字节；预览不切多字节字符 |
| 分区预算 | 压完在预算内；system/任务不动；丢失前缀；说明消息内容、有界、可复现 |
| 顺序 | 先预览后丢弃（用"预览后刚好放得下"的精确预算验） |
| 配对 | 多工具一轮整段丢；视图里 `tool_calls` 与 `tool` 结果集合相等 |
| 报错 | 任务本身超预算、只剩说明仍超、`max_bytes<=0`、seq 不平行、负阈值 |
| 接线 | 默认预算 intent 带 context 且 hash 不变；大输出落盘后 replay 通过；小预算压缩后 replay 通过；溢出时 model.error + 不调模型 |
| 兼容 | 无 `context` 的旧日志照常通过；篡改 `context.source_seqs` 必须报错；旧游标可反序列化 |

## 9. 已知取舍与未做

- **CLI 没有暴露 `--context-budget-bytes`**：预算目前只能通过 `Limits` 注入
  （默认 1 MiB，足以保护日常 run）。要开放给用户，得同时改 `cli.py` 的
  `run` 与 `worker` 两个子命令——本单不碰 CLI。
- **游标里的完整历史不设上限**：编译只约束"发给模型的那份"。长 run 的游标
  仍会随轮数增长；要不要给游标本身做滚动窗口是另一个决定（会动到"消息序列
  可重建"这条契约）。
- **字节 ≠ token**：见 §3.1。真实窗口保护需要 tokenizer 或 provider 侧反馈，
  两者都还没有。
- **每轮多次序列化**：`compile_context` 会对消息做几次 `canonical_bytes` 以
  测量字节数。1 MiB 量级下约几十毫秒/轮，暂不优化；真要优化应先落一个
  "历史字节数增量维护"的结构，那会引入缓存失效问题，不值得在没有实测压力时做。
