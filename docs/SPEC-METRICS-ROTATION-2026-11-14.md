# SPEC · 指标文件轮转与保留（metrics.jsonl rotation）

> 日期：2026-11-14 · 分支：`feat/metrics-rotation` · 基线：`main@b450c33`
> 性质：**产品行为变更（非契约变更）**——`metrics.jsonl` 不属于 C1–C5 五道契约，
> 不走 `docs(contract)`；但它是文档对账范围内的运行时行为陈述，本文与
> [runtime-metrics.md](runtime-metrics.md)、[KNOWLEDGE.md](KNOWLEDGE.md) §7 同步更新。
> 冲突面核查（2026-11-14）：在途 PR #146（restore-held 契约）/ #141（契约对账批次 B）/
> #140（普通文档对账批次 A）均不触碰 `src/tianshu/metrics.py`；
> `git log -- src/tianshu/metrics.py` 最近两次提交（f6b91fd / 90393c8）均为已合并历史。

## 1. 现状（代码事实）

- `metrics.py` 的写入路径 `_append()`（`src/tianshu/metrics.py:339`）每次追加
  单独 `os.open(O_WRONLY|O_CREAT|O_APPEND)` → `os.write` → `os.close`，进程内
  一把 `threading.Lock`；**没有常驻句柄**。文件只增不减，daemon 长期运行时
  `<data_root>/metrics.jsonl` 无上限增长。
- 这条限制已登记：KNOWLEDGE §7「指标」条目——「指标文件无轮转」。
- 生产写入方只有 daemon（`daemon.py:614` 创建 `MetricsRecorder`，采样默认
  5s 一次）；CLI / worker 不创建 recorder。按默认采样率与每行 ~200–400B 估算，
  指标文件一天增长约 3–5 MB，量级不大但**没有上界**，与「指标不该成为被测量的
  开销」的模块立场不符。
- 读取方：`read_metrics(path)` 读单个文件，带「无 `\n` 收尾的尾巴」宽容语义
  （90393c8）；`summarize()` / `metric_values()` 消费 records。
- 测试锚点：`tests/test_metrics.py::test_metric_file_stays_inside_data_root_and_module_has_no_network`
  断言数据根**只有** `metrics.jsonl` 一个文件。轮转后该断言必须改为
  「所有条目都以 `metrics.jsonl` 为前缀」——这是**有意的口径变更**，在 PR 正文说明。

## 2. 目标与非目标

**目标**
1. 指标文件按大小轮转：当前文件达到阈值后，先轮转再追加，单文件不超过
   `max_file_bytes + 一行`。
2. 保留上限：轮转文件按编号槽位保留 N 份，最旧的被下一轮轮转换出（覆盖），
   总磁盘占用上界 = `(N + 1) × max_file_bytes + 每行余量`。
3. **样本永不丢**：轮转失败（目标被占用、权限等）时降级为「继续追加当前文件」，
   绝不因为轮转失败丢弃采样点——诊断数据宁可晚收拾，不可消失。
4. 轮转本身可观测：每次成功轮转在指标流里记一条 `metrics_rotated` 事件。
5. 读取面跟上：`read_metrics(..., include_rotated=True)` 按时间顺序（最旧 → 最新）
   读回全部轮转历史；`list_metrics_files()` 给出有序文件清单。
6. 默认开启（既然登记了限制就按默认正确交付，不留「默认关」的口子）；
   参数可用构造参数与环境变量覆盖。

**非目标**
- 不做时间触发的轮转（只按大小；时间不构成额外价值）。
- 不冻结 SLO 阈值（PRD §10.6 仍留给发布候选，`slo_report()` 保持 unknown）。
- 不改 `daemon.py`（轮转是 recorder 内部行为，构造默认值即生效）。
- 不解决「state 响应可超 IPC 8MiB」（control.py 所有，仍保持登记，另行工单）。
- 不引入压缩 / 归档格式 / 第三方依赖（零依赖纪律）。

## 3. 设计

### 3.1 触发点与算法

- 检查在 `_append()` 的**锁内**、写入**之前**：`os.stat(self.path)` 取大小，
  `>= max_file_bytes` 即轮转。文件不存在（首次写入）不轮转。
- 轮转 = 编号槽位链式 `os.replace`（替换语义，POSIX/Windows 都成立）：

  ```
  for i in range(max_rotated_files - 1, 0, -1):
      if <metrics.jsonl.i 存在>: os.replace(<.i>, <.i+1>)
  os.replace(<metrics.jsonl>, <.1>)
  ```

  最旧一份（槽位 N）被链式覆盖——保留策略不需要显式 DELETE，杜绝「删除失败
  留下 N+1 份」的分支。每步 `os.replace` 失败（Windows 上读者持句柄会
  `PermissionError`）即**停止本轮轮转**、样本照常追加当前文件、
  `rotation_errors += 1`；已完成的链式位移保持合法 JSONL（槽位可能有空洞，
  读取侧按「存在的最大槽位」处理，不依赖连续编号）。
- 成功轮转后先写入样本（同一次 `_append` 调用内、锁释放前完成），再在
  **锁外**补一条 `metrics_rotated` 事件（`kind="event"`；`value` 即被轮转文件的
  字节数，stat 不到记 `null`（unknown），额外字段带 `max_file_bytes`）。
  补记事件走普通 `_append`，但按「记录的 metric 已是 `metrics_rotated`」抑制
  再嵌套记事件：**轮转事件的追加仍可触发轮转、不再产生新事件**——否则极小
  阈值下（阈值 < 一行）会无限递归。负控钉住这条。

### 3.2 配置

| 参数 | 默认 | 环境变量 | 约束 |
|---|---|---|---|
| `max_file_bytes` | `16 MiB` | `TIANSHU_METRICS_MAX_FILE_BYTES` | 构造传 `None`＝跟随环境/默认（沿用本模块 `enabled`/`interval_s` 的既有语义）；`0`＝**显式禁用轮转**（构造与环境变量同一语义）；负数在构造侧 `ValueError`、在 env 侧回落默认（与 `_env_interval()` 的容错一致） |
| `max_rotated_files` | `4` | `TIANSHU_METRICS_MAX_ROTATED_FILES` | 构造 `None`＝跟随环境/默认；`< 1` 构造侧 `ValueError`，env 侧回落默认 |

- 默认值理由：按 5s 采样 ≈ 3–5 MB/天，16 MiB 单片约保留 3–4 天，总上界
  5 × 16 MiB = 80 MiB，对「常驻 daemon 的诊断数据」是合理量级；数值拍定后
  在 runtime-metrics.md 写明出处，不冒充实测。
- 构造侧违反约束一律 `ValueError`（fail fast，不做静默夹紧）；env 侧沿用
  本模块「解析失败回落默认」的既有惯例，不因配置手误关掉指标。

### 3.3 读取面

- `list_metrics_files(data_root) -> list[tuple[int | None, Path]]`：
  返回 `(槽位, 路径)`，**时间序（最旧在前）**：槽位号越大越旧，当前文件
  槽位 `None` 在最后。
- `read_metrics(path, *, limit=None, include_rotated=False)`：
  - 默认行为不变（只读当前文件）——既有调用方与测试零迁移。
  - `include_rotated=True` 时按 `list_metrics_files` 顺序逐文件读取拼接，
    每个文件独立套用既有「尾巴宽容 / 坏行报错」语义；`limit` 在拼接后的
    全序列上取最后 N 条。
- 轮转文件名固定为 `metrics.jsonl.<slot>`（纯数字后缀才识别为轮转件，
  其他同名前缀文件不进清单——避免把用户手放的东西当指标读）。

### 3.4 并发与跨进程边界

- 单进程内：轮转检查、链式 replace、样本追加都在同一把 `_lock` 里，
  与既有追加语义一致。
- 跨进程（理论上 daemon 是唯一写入方；测试/误配置下两个 recorder 指向同一
  数据根）：`os.replace` 天然原子，输者拿到 `FileNotFoundError`/`PermissionError`
  → 按 3.1 降级为继续追加。不为此加跨进程锁（指标是 best-effort 诊断面，
  事件日志才是强一致面——不把两套可靠性语义搅在一起）。
- Windows 读者持句柄期间轮转会失败推迟：样本不丢、当前文件可能暂时超过
  阈值，下一轮重试。这是显式边界，写进文档，不当 bug 修。

## 4. 判据（测试 ↔ 设计映射，全部落在 `tests/test_metrics.py`）

| # | 测试 | 判据 |
|---|---|---|
| 1 | `test_rotation_splits_file_at_threshold` | 小阈值下写入多行：当前文件行数重新开始、`.1` 收下旧内容；`include_rotated=True` 读回按时间序全量 |
| 2 | `test_retention_overwrites_oldest_slot` | `max_rotated_files=2` 强制 3 次轮转：`.3` 不存在，`.2/.1/base` 内容并集 = 全部样本，无丢失 |
| 3 | `test_rotation_failure_keeps_the_sample` | 注入 `os.replace` 抛 `OSError`：样本仍在当前文件（数据不丢）、`rotation_errors` 递增、下轮恢复轮转 |
| 4 | `test_rotation_emits_observable_event_without_recursion` | 阈值小于一行：连续 `record()` 3 次必然终止（无递归爆栈），轮转事件 ≥ 1 条且事件自身不级联（非 `metrics_rotated` 记录恰 3 条） |
| 5 | `test_include_rotated_reads_chronologically` | 手造 `.1/.2` 与 base（带 `seq` 序号字段）：读取顺序 = `.2 → .1 → base`；`limit` 作用于拼接后全序列 |
| 6 | `test_list_metrics_files_orders_and_ignores_foreign_suffix` | `metrics.jsonl.tmp` / `metrics.jsonl.backup` 等非数字后缀不进清单；空洞槽位（只有 `.1/.3`）按存在序返回 |
| 7 | `test_rotation_params_validation` | 构造负值 → `ValueError`；env 非法/负值回落默认而非报错或禁用 |
| 8 | `test_zero_disables_rotation` | 构造或 env 传 `0`：超阈值也不轮转（向后逃生门，行为可断言） |
| 9 | 既有 `test_metric_file_stays_inside_data_root_and_module_has_no_network` | **无需变更**：默认参数 + 2 条小记录不触发轮转，「数据根只有 metrics.jsonl」的断言仍成立；「轮转后数据根条目仍全部以 `metrics.jsonl` 为前缀」并入判据 #1 |
| 10 | 既有全量 | daemon 默认参数下采样路径行为不变（rotation 不改变单行格式、`read_metrics` 默认签名） |

并发纪律：轮转由大小触发，测试用小阈值确定性构造，不靠 sleep；
无需真子进程（写入方是进程内单锁面，跨进程竞态在 3.4 明示为边界而非判据）。

## 5. 负控（变异后必须红，逐条实跑记录）

- **NC1**：删掉「超阈值先轮转」的检查（只保留追加）→ 判据 #1、#2 红。
- **NC2**：把「轮转失败仍追加样本」改成「失败即丢弃本次样本」→ 判据 #3 红
  （这条负控钉住的是**样本不丢**优先于**文件不超限**的优先级）。
- **NC3**：把 `suppress_rotation_note` 摘掉 → 判据 #4 红（RecursionError 或事件数爆炸）。

## 6. 文档对账

- [runtime-metrics.md](runtime-metrics.md)：新增「轮转与保留」小节（命名、默认值、
  env、读取 API、Windows 边界、80 MiB 上界的**计算依据**——标注是估算不是实测）。
- [KNOWLEDGE.md](KNOWLEDGE.md) §7「指标」条目：登记限制更新为「已按大小轮转 +
  保留 4 份（2026-11-14，见 SPEC）」；§8 追加会话日志一条。
- 本 SPEC：判据/负控以实测回填（计划条数会偏——§5 教训）。

## 7. 高危判定

不碰授权 / 租约 / 并发写（事件日志）/ 事件日志 / 计费——`metrics.jsonl` 是
诊断面不是事实源。按 CONTRIBUTING §7 **不构成强制独立复审项**；PR 仍按
标准门禁（四 required CI）交付。

## 8. 实测回填（2026-11-14）

- 判据实测 **8 条新增**（§4 的 #1–#8）+ 既有 `test_metrics.py` 14 条不变全绿，
  定向 **22 passed**（macOS arm64 本机）；§4 的 #9 锚点测试**无需变更**（按修订
  后口径）；#10 由全量门禁与 CI 承担。
- 负控三条（§5）全部实跑红并还原：NC1 拆大小检查 → 轮转/保留 2 条红；
  NC2 失败改为丢样本 → `test_rotation_failure_keeps_the_sample` 红；
  NC3 摘级联抑制 → `RecursionError` 红。
- 计划与实测偏差：无新增计划外判据；`list_metrics_files` 对「当前文件不存在
  仅有轮转件」的异常场景补了一条显式断言（并入 #6）。
