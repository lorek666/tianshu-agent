# 记忆检索评测（N2：作用域优先级 + 向量/RRF）

> **范围与口径（先读这段，别把数字读大）**
>
> 1. 本文只报**实测数字**，脚本是 [`scripts/eval_memory_retrieval.py`](../scripts/eval_memory_retrieval.py)，
>    任何人可用同一条命令复跑：`uv run python scripts/eval_memory_retrieval.py`。
> 2. 固定样例 **22 条 query / 25 条记忆**，其中 8 条词面重叠（`lexical`）、8 条中英混合
>    （`mixed`）、6 条同义改写（`paraphrase`）。
> 3. `hybrid` 用的是 **`HashEmbeddingProvider`（确定性哈希，无语义能力）**——它证明的是
>    "两路名次做 RRF 融合"这件事有效，**不是**"语义召回有效"。换成真 embedding provider
>    之后必须重跑本表，并把它记成"换 provider 后的实测"，不许沿用下面的数字。
> 4. **召回已经饱和（Recall@5 = 1.000）**：库只有 25 条、query 又都带词面线索，这个规模
>    测不出"召回上限"。真正有区分度的是 **MRR**（排第几）。结论也只写到这里。
> 5. `paraphrase` 组的样例在中文里仍有共享字（"别用英文回答" vs "以后一律用中文回答" 共享
>    "用"/"回答"），所以它们不是"零词面重叠"的硬负例；真正的同义改写覆盖不足，如实记在
>    §5。

## 1. 怎么测

每个 `Case` 给定 `query` 与唯一期望的 `memory_id`；两种模式跑同一批 query：

| 模式 | 候选 | 排序 |
|---|---|---|
| `fts` | FTS5（CJK 双字切分） | 作用域优先级 → origin 权重 → `created_at` 降序 |
| `hybrid-hash` | FTS 候选 **∪** 语义候选（语义命中 FTS 没召回的行也并进来） | 两路名次做 RRF（`k=60`）；融合后按 `(作用域优先级, 融合分, memory_id)` 排 |

指标：

- **Recall@5**：期望记忆是否出现在前 5 条（`MAX_RECALL_ITEMS = 5`）；
- **MRR**：期望记忆名次的倒数（第 1 名 = 1.0，第 5 名 = 0.2）。

## 2. 实测结果（2026-11-11，macOS arm64，Python 3.12）

| 模式 | Recall@5 | MRR | lexical MRR | mixed MRR | paraphrase MRR |
|---|---|---|---|---|---|
| `fts` | 1.000 | 0.864 | 0.688 | 1.000 | 0.917 |
| `hybrid-hash` | 1.000 | **0.955** | **1.000** | 0.875 | **1.000** |

分组 Recall@5 两种模式都是 1.000，不作区分。

**如实解读**：

- 融合把整体 MRR 从 0.864 提到 **0.955**：收益来自 `lexical` 组（0.688 → 1.000）——
  `search()` 的词面名次是"作用域优先级 → origin 权重 → 时间"，`bm25` 名次在最终排序里
  已被覆盖；`retrieve()` 把 `bm25` 名次与语义名次一起交给 RRF，正好补上这一刀。
- `mixed` 组 MRR 从 1.000 掉到 **0.875**：并集把语义路多捞的行带进来，在哈希基线里它们
  只部分重叠、却排到了前面。这是**真实代价**，写在这里而不是抹掉——并集扩大召回，必然
  带进噪声，得有真 provider 才能判断值不值。
- `paraphrase` 组两种模式都接近 1.0，**不能**当成"语义召回有效"的证据（见开头的口径
  第 3、5 条；中文共享字让这些"改写"仍带词面线索）。

## 3. 判据（与代码一一对应）

| 判据 | 落点 |
|---|---|
| 同一 query 下 task > workspace > user | `tests/test_memory_rank.py::test_scope_priority_orders_task_then_workspace_then_user` |
| 不传上下文时退回旧排序（向后兼容） | `tests/test_memory_rank.py::test_without_priority_the_old_order_is_kept` |
| 同上下文逐位可复现 | `tests/test_memory_rank.py::test_priority_is_reproducible_for_the_same_context` |
| 优先级不能把越权作用域捞进来 | `tests/test_memory_rank.py::test_priority_never_pulls_in_a_scope_that_is_not_allowed` |
| 过期项即便优先级最高也不参与 | `tests/test_memory_rank.py::test_expired_memory_is_excluded_even_at_the_highest_priority` |
| 裁剪仍在排序之后（4 KiB 预算） | `tests/test_memory_rank.py::test_byte_budget_crop_still_happens_after_priority` |
| 未配置 provider → `semantic=false` + 原因 | `tests/test_memory_vectors.py::test_fts_only_reports_semantic_false_with_reason` |
| 语义路**即使 FTS 空也要跑**，语义独有候选并入并集 | `test_semantic_route_runs_even_when_fts_finds_nothing` / `test_semantic_only_candidates_are_merged_with_fts_candidates` |
| 空索引/半套向量不得报可用 | `test_empty_index_is_not_reported_as_available` / `test_mixed_provider_vectors_are_detected_as_incomplete` |
| 换 provider 未重建 → 旧向量不参与 | `tests/test_memory_vectors.py::test_provider_switch_makes_old_vectors_ineligible` |
| 重建逐字节确定 | `test_rebuild_vectors_is_byte_identical_across_runs` |
| 无索引的 `rebuild()` 也清向量/作废构建标记 | `test_rebuild_without_an_index_still_clears_vector_state` |
| 索引建表在回滚事务后能重试 | `test_index_schema_creation_in_a_rolled_back_transaction_is_retried` |
| 公开 `search()` 真的过滤过期 | `test_direct_index_search_excludes_expired_rows` |
| `search()` 不偷偷变 hybrid | `test_search_stays_fts_only_and_retrieve_is_hybrid` |
| 索引跟随生命周期（候选/确认/忘记/擦除） | `test_index_follows_the_memory_lifecycle` / `test_erase_drops_the_vector` |
| RRF 分数可手算 | `tests/test_memory_vectors.py::test_rrf_scores_are_hand_checkable` |
| 语义路也不能越权 | `tests/test_memory_vectors.py::test_semantic_route_never_pulls_in_other_scopes` |
| 重建后不残留无行引用的向量 | `tests/test_memory_vectors.py::test_rebuild_clears_vectors_for_rows_that_are_no_longer_recallable` |

**负控**（都真跑过）：

1. 把排序键改回 `origin` 优先（去掉作用域优先级）→ 作用域顺序用例红；
2. 让 `availability()` 在 provider 不匹配/未构建时也返回 True → 降级用例红；
3. 去掉确认/忘记/擦除时的索引钩子 → 生命周期用例红；
4. 把语义路改回"只采纳 FTS 候选中出现过的 id"→ 两条并集用例红；
5. 去掉 `MemoryIndex.ensure_schema` 的事务判断 → 回滚重试用例红。

## 4. 能力边界（写进代码注释，也写在这里）

- 没有 provider → **FTS-only**，`semantic=false`，`degraded` 说明原因；这不是"没找到"。
- 换 provider 后未 `rebuild_vectors()` → 旧向量**不参与**融合，仍报降级。
- 向量表与 provider 身份落在 `memory_vector_meta`；`MemoryProjection.rebuild()` 会清空重放，
  向量也跟着重建（同事务）。
- 重排器（reranker）只有接缝设想，**未实现**；本表不含任何重排效果。

## 5. 没做到的（如实记）

1. **没有真 embedding provider**：内置的只有确定性哈希基线。语义能力要等用户显式配置的
   provider 接进来（数据外发许可另议，见 F10 §10）。
2. **规模太小**：25 条记忆测不出 Recall@5 的上限差异；跨千条记忆的分层召回没有实测。
3. **同义改写覆盖不足**：现有 `paraphrase` 样例仍有中文共享字，不是硬负例。
4. **没有对比 BM25 权重调参**：`bm25` 只用来产生名次，没试过与作用域优先级的加权组合；
   换成"优先级只作为 tie-breaker"之类的变体也没有评测。
5. **RRF 的 `k` 只用了 60**：默认值，没有做敏感性分析。