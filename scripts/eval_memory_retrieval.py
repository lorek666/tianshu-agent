"""离线评测：FTS-only vs 混合检索（F10 §8 / §12）。

这个脚本**只报实测数字**，不预设结论。两种模式跑同一批 query、同一批记忆：

- `fts`：只有词面召回（FTS5 + CJK 双字切分），排序 = 作用域优先级 → origin → 时间；
- `hybrid`：词面候选 **∪** 语义候选（两路各自名次做 RRF 融合）。向量来自
  `HashEmbeddingProvider`——**确定性哈希基线，没有语义能力**，所以这组数字说明的是
  "两路名次融合"的效果，**不是**"语义召回"的效果。换真正的 embedding provider 后
  必须重跑，别把这里的数字当语义能力证明。

用法：

    uv run python scripts/eval_memory_retrieval.py
    uv run python scripts/eval_memory_retrieval.py --json > /tmp/eval.json

指标：Recall@5 与 MRR。判据是"给定 query，期望的那条记忆有没有进前 5 / 排第几"。
只统计**有期望答案**的 query（同义改写样例故意包含"词面完全不重叠"的，那种样例
哈希基线天然吃亏，如实报出来）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tianshu.events import EventLog
from tianshu.memory import MAX_RECALL_BYTES, MAX_RECALL_ITEMS, MemoryProjection, RecallPriority
from tianshu.memory_index import HashEmbeddingProvider, MemoryIndex
from tianshu.resources import MemoryScope

RUN = "run_eval"
NOW = 1_700_000_000_000


@dataclass(frozen=True)
class Case:
    query: str
    expected: str
    #: `lexical`：期望正文与 query 有词面重叠；`paraphrase`：只有语义相关。
    kind: str


#: 固定样例（中文 / 中英混合 / 标识符）。刻意包含两种难度：
#: 词面重叠的（两路都该赢）+ 同义改写的（哈希基线不该赢，如实报）。
CASES: tuple[Case, ...] = (
    Case("报告 中文 结论", "m_cn_report", "lexical"),
    Case("风险 先讲", "m_risk_first", "lexical"),
    Case("数据库 迁移 步骤", "m_migrate", "lexical"),
    Case("测试 覆盖率", "m_coverage", "lexical"),
    Case("失败 重试 次数", "m_retry", "lexical"),
    Case("日志 保留 天数", "m_log_retention", "lexical"),
    Case("部署 回滚", "m_rollback", "lexical"),
    Case("CLI 参数 解析", "m_cli_args", "lexical"),
    Case("unit test 命名", "m_test_naming", "mixed"),
    Case("HTTP 超时 设置", "m_http_timeout", "mixed"),
    Case("SQL index 优化", "m_sql_index", "mixed"),
    Case("JSON schema 校验", "m_json_schema", "mixed"),
    Case("Windows 路径 兼容", "m_windows_path", "mixed"),
    Case("macOS 签名 流程", "m_macos_sign", "mixed"),
    Case("memory_blobs 目录", "m_blob_dir", "mixed"),
    Case("tool.intent 事件", "m_intent_event", "mixed"),
    Case("别用英文回答", "m_cn_only", "paraphrase"),
    Case("先给出结论再解释", "m_conclusion_first", "paraphrase"),
    Case("数据库升级要小心", "m_migrate_careful", "paraphrase"),
    Case("出错以后不要立刻重试", "m_no_retry", "paraphrase"),
    Case("把过程写清楚一点", "m_explain_steps", "paraphrase"),
    Case("文件要按时间排序", "m_sort_by_time", "paraphrase"),
)

MEMORIES: tuple[tuple[str, str], ...] = (
    ("m_cn_report", "报告用中文写，先列结论，再列依据"),
    ("m_risk_first", "先讲风险，再讲方案"),
    ("m_migrate", "数据库迁移分三步：备份、演练、割接"),
    ("m_coverage", "测试覆盖率低于 80% 不放行"),
    ("m_retry", "失败最多重试三次，之后报警"),
    ("m_log_retention", "日志保留 30 天，超期归档"),
    ("m_rollback", "部署出问题先回滚，再定位原因"),
    ("m_cli_args", "CLI 参数用 argparse 解析，不要手写"),
    ("m_test_naming", "unit test 命名用 test_ 前缀 + 行为描述"),
    ("m_http_timeout", "HTTP 请求默认超时 10 秒"),
    ("m_sql_index", "SQL 慢查询先看 index 有没有命中"),
    ("m_json_schema", "JSON 输出必须过 schema 校验"),
    ("m_windows_path", "Windows 路径用 pathlib，不要拼字符串"),
    ("m_macos_sign", "macOS 签名用 codesign，要先公证"),
    ("m_blob_dir", "正文 blob 放 memory_blobs 目录，别塞事件库"),
    ("m_intent_event", "tool.intent 只存引用，不存正文"),
    ("m_cn_only", "以后一律用中文回答，不要用英文"),
    ("m_conclusion_first", "回答先给结论，再解释"),
    ("m_migrate_careful", "数据库升级有风险，备份演练割接缺一不可"),
    ("m_no_retry", "失败先排查原因，不要立刻重试"),
    ("m_explain_steps", "说明里把关键步骤写清楚"),
    ("m_sort_by_time", "文件按修改时间先后排序"),
    ("m_extra_1", "今天天气不错，适合出门走走"),
    ("m_extra_2", "把会议纪要发给全员"),
    ("m_extra_3", "周报里加上本周的风险项"),
)


def _record(log: EventLog, projection: MemoryProjection, payload: dict) -> None:
    with log.transaction() as tx:
        seq = tx.append(RUN, "tool.result", payload, legacy_write=True)
        projection.apply_event("tool.result", payload, seq=seq)


def _seed(log: EventLog, projection: MemoryProjection) -> None:
    scope = MemoryScope.user()
    for index, (memory_id, content) in enumerate(MEMORIES):
        content_ref = projection.put_content(content)
        _record(
            log,
            projection,
            {
                "tool_id": "memory.propose",
                "status": "succeeded",
                "output": {
                    "memory_id": memory_id,
                    "revision": 1,
                    "scope": scope.scope,
                    "scope_id": scope.scope_id,
                    "kind": "preference",
                    "content_ref": content_ref,
                    "source_refs": ["event:1"],
                    "evidence": [
                        {"source_memory_id": "memory_source", "quote": "原文引文"}
                    ],
                    "proof_count": 1,
                    "origin": "user_explicit",
                    "status": "candidate",
                    "created_at": index + 1,
                    "expires_at": None,
                    "deduped": False,
                },
            },
        )
        _record(
            log,
            projection,
            {
                "tool_id": "memory.confirm",
                "status": "succeeded",
                "output": {
                    "memory_id": memory_id,
                    "revision": 2,
                    "before_status": "candidate",
                    "status": "active",
                    "consolidated_at": NOW,
                    "memory_uri": scope.to_uri(),
                },
            },
        )


def _metrics(hits: list[list[str]], cases: tuple[Case, ...]) -> dict[str, float]:
    """Recall@5 与 MRR。命中定义为"期望 memory_id 出现在结果里"。"""
    recall = 0
    rr_total = 0.0
    for ranking, case in zip(hits, cases, strict=False):
        if case.expected in ranking:
            recall += 1
            rank = ranking.index(case.expected) + 1
            rr_total += 1.0 / rank
    count = len(cases) or 1
    return {"recall_at_5": recall / count, "mrr": rr_total / count, "cases": float(len(cases))}


def _run(hybrid: bool) -> tuple[list[list[str]], list[list[str]]]:
    root = Path(tempfile.mkdtemp(prefix="ts-memory-eval-"))
    log = EventLog(root / "events.db", blob_dir=root / "event_blobs")
    index = MemoryIndex(log.conn, provider=HashEmbeddingProvider(dim=64)) if hybrid else None
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=root / "memory_blobs", clock=lambda: NOW, index=index
    )
    projection.ensure_schema()
    _seed(log, projection)
    if hybrid:
        # 语义能力必须**构建完成**：`availability()` 只认"完整构建过"的索引，
        # 空表/半套向量一律报降级（评测要按产品的真实口径跑）。
        projection.rebuild_vectors()
    scope = MemoryScope.user()
    priority = RecallPriority(task_id="task_eval", workspace_roots=())
    try:
        all_rankings: list[list[str]] = []
        wanted_rankings: list[list[str]] = []
        for case in CASES:
            outcome = projection.retrieve(
                case.query,
                allowed=(scope,),
                limit=MAX_RECALL_ITEMS,
                max_bytes=MAX_RECALL_BYTES,
                priority=priority,
            )
            ranking = [item.memory.memory_id for item in outcome.items]
            all_rankings.append(ranking)
            wanted_rankings.append(ranking)
        return all_rankings, wanted_rankings
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F10 记忆检索离线评测（只报实测）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    rows = []
    for label, hybrid in (("fts", False), ("hybrid-hash", True)):
        rankings, _ = _run(hybrid)
        by_kind = {
            kind: _metrics(
                [r for r, c in zip(rankings, CASES, strict=False) if c.kind == kind],
                tuple(c for c in CASES if c.kind == kind),
            )
            for kind in ("lexical", "mixed", "paraphrase")
        }
        rows.append(
            {
                "mode": label,
                **{f"all_{k}": v for k, v in _metrics(rankings, CASES).items()},
                **{
                    f"{kind}_{key}": value
                    for kind, metrics in by_kind.items()
                    for key, value in metrics.items()
                },
            }
        )

    if args.json:
        print(json.dumps({"cases": len(CASES), "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"固定样例：{len(CASES)} 条 query / {len(MEMORIES)} 条记忆")
    print(
        f"{'mode':<12}{'Recall@5':>10}{'MRR':>10}"
        "   lexical R@5 / MRR   mixed R@5 / MRR   paraphrase R@5 / MRR"
    )
    for row in rows:
        print(
            f"{row['mode']:<12}{row['all_recall_at_5']:>10.3f}{row['all_mrr']:>10.3f}"
            f"{row['lexical_recall_at_5']:>19.3f}{row['lexical_mrr']:>7.3f}"
            f"{row['mixed_recall_at_5']:>19.3f}{row['mixed_mrr']:>7.3f}"
            f"{row['paraphrase_recall_at_5']:>22.3f}{row['paraphrase_mrr']:>7.3f}"
        )
    print()
    print("注意：hybrid 用的是确定性哈希 provider（无语义能力），")
    print("所以 paraphrase 组的表现不能当作语义召回能力，换真 provider 后必须重跑。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())