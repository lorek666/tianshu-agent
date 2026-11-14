"""千条级检索评测（W6，SPEC-N2-EMBEDDING-PROVIDER §2.4）。

动机：`eval_memory_retrieval.py` 的 22 query / 25 记忆 Recall@5 已饱和
（1.000），回答不了「规模上去以后两路还行不行、RRF 的 k 敏不敏感」。

方法（全部确定性，固定 seed）：

- 背景记忆由模板合成，条目正文唯一（有断言）；黄金条目嵌在背景里；
- 黄金 query 固定 20 条（词面重叠 / 中英混合两种难度，无 paraphrase——
  哈希基线抓不住改写，如实声明）；
- N ∈ {50, 200, 1000}；FTS-only vs hybrid（hash 基线——**没有语义能力**，
  这组数字说明"两路名次融合在规模下的行为"，不是"语义召回"）；
- k 敏感性：N=1000 下 k ∈ {30, 60, 100}——FTS 名次与语义名次**各自独立
  获取**，用 `rf_fuse(k=...)` 纯函数重排（产品路径的 k 固定 60，这里是
  索引层实验，不冒充产品口径）；
- 真 provider 分数留接口：`--provider openai` 读 embedding 环境变量四件套；
  没跑就没有数字，**不编造、不宣称语义收益**。

用法：

    uv run python scripts/eval_memory_scale.py
    uv run python scripts/eval_memory_scale.py --json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tianshu.events import EventLog
from tianshu.memory import MAX_RECALL_BYTES, MAX_RECALL_ITEMS, MemoryProjection, RecallPriority
from tianshu.memory_index import HashEmbeddingProvider, MemoryIndex, rf_fuse
from tianshu.resources import MemoryScope

RUN = "run_scale_eval"
NOW = 1_700_000_000_000

#: 评测的规模阶梯。
SCALE_STEPS: tuple[int, ...] = (50, 200, 1000)
#: k 敏感性实验点（含产品默认 60 作对照）。
K_STEPS: tuple[int, ...] = (30, 60, 100)


@dataclass(frozen=True)
class Case:
    query: str
    expected: str
    kind: str  # lexical | mixed


# ---------------------------------------------------------------- 黄金集

GOLD: tuple[tuple[str, str], ...] = (
    ("g_deploy", "部署回滚先演练再执行"),
    ("g_backup", "数据库备份每天凌晨全量"),
    ("g_alert", "报警分级：P0 电话、P1 短信"),
    ("g_quota", "API 配额超限先降级再扩容"),
    ("g_log", "日志按天滚动保留两周"),
    ("g_cache", "缓存穿透用空值缓存兜底"),
    ("g_queue", "队列积压超阈值自动扩分区"),
    ("g_secret", "密钥只走环境变量不落盘"),
    ("g_review", "合并前必须两人复审"),
    ("g_gate", "发布门禁红就不准上线"),
    ("g_test name", "测试命名 test_ 加行为"),
    ("g_http timeout", "HTTP 超时默认 10 秒"),
    ("g_sql index", "慢查询先看索引命中"),
    ("g_json schema", "输出必须过 schema 校验"),
    ("g_ci cache", "CI 依赖缓存按 lockfile 键"),
    ("g_rate limit", "外发频率下限每秒一次"),
    ("g_retry policy", "连接失败重试一次"),
    ("g_budget", "预算按字节预占结算"),
    ("g_scope id", "记忆按作用域隔离"),
    ("g_blob dir", "正文 blob 与事件分目录"),
)

CASES: tuple[Case, ...] = tuple(
    Case(query, memory_id, "lexical" if i < 10 else "mixed")
    for i, (query, memory_id) in enumerate(
        (
            ("回滚 演练", "g_deploy"),
            ("备份 全量", "g_backup"),
            ("报警 分级", "g_alert"),
            ("配额 超限", "g_quota"),
            ("日志 滚动", "g_log"),
            ("缓存 穿透", "g_cache"),
            ("队列 积压", "g_queue"),
            ("密钥 环境", "g_secret"),
            ("复审 合并", "g_review"),
            ("发布 门禁", "g_gate"),
            ("test 命名", "g_test name"),
            ("HTTP 超时", "g_http timeout"),
            ("SQL 索引", "g_sql index"),
            ("JSON 校验", "g_json schema"),
            ("CI 缓存", "g_ci cache"),
            ("频率 下限", "g_rate limit"),
            ("连接 重试", "g_retry policy"),
            ("字节 预算", "g_budget"),
            ("作用域 隔离", "g_scope id"),
            ("blob 目录", "g_blob dir"),
        )
    )
)

# ---------------------------------------------------------------- 背景合成

_TOPICS = (
    "部署", "监控", "权限", "计费", "日志", "缓存", "队列", "网关", "备份", "审计",
    "网关", "套餐", "工单", "灰度", "巡检", "演练", "归档", "标签", "模板", "签名",
)
_TAILS = (
    "负责人在值班表里查", "每周例会过一遍", "异常时升级到值班工程师",
    "相关文档在内部 wiki", "历史记录保留一个季度", "配置变更要走审批",
    "与平台组的接口人对齐", "上线前在预发环境验证", "数据口径以报表为准",
    "实施细节看运行手册",
)
_SHAPES = ("规则", "约定", "备忘", "流程")


def _bg_content(index: int) -> str:
    """确定性合成第 index 条背景记忆（random.Random(seed) 不受全局状态影响）。"""
    rng = random.Random(f"bg-{index}")
    topic = _TOPICS[index % len(_TOPICS)]
    shape = _SHAPES[rng.randrange(len(_SHAPES))]
    tail = _TAILS[rng.randrange(len(_TAILS))]
    return f"{topic}{shape}#{index:05d}：{topic}相关的条目，{tail}（编号 {index:05d}）"


def build_case_set(n: int) -> tuple[list[tuple[str, str]], tuple[Case, ...]]:
    """返回 (全部记忆 id+正文, 有效黄金 query 集)。

    黄金条目占前 len(GOLD) 个位置，其余是背景；n < 黄金数时直接报错。
    """
    if n < len(GOLD):
        raise ValueError(f"规模 {n} 小于黄金条目数 {len(GOLD)}")
    memories: list[tuple[str, str]] = [
        (memory_id, content) for memory_id, content in GOLD
    ]
    for i in range(n - len(GOLD)):
        memories.append((f"bg_{i:05d}", _bg_content(i)))
    # 确定性去重检查：正文唯一（合成模板撞车会让"唯一期望"失去意义）。
    contents = [c for _, c in memories]
    assert len(set(contents)) == len(contents), "背景合成出现重复正文"
    return memories, CASES


# ---------------------------------------------------------------- 执行


def _record(log: EventLog, projection: MemoryProjection, payload: dict) -> None:
    with log.transaction() as tx:
        seq = tx.append(RUN, "tool.result", payload, legacy_write=True)
        projection.apply_event("tool.result", payload, seq=seq)


def _seed_all(log: EventLog, projection: MemoryProjection, memories) -> None:  # noqa: ANN001
    scope = MemoryScope.user()
    for order, (memory_id, content) in enumerate(memories):
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
                    "created_at": order + 1,
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
    """Recall@5 与 MRR；命中 = 期望 memory_id 出现在结果里。"""
    recall = 0
    rr_total = 0.0
    for ranking, case in zip(hits, cases, strict=False):
        if case.expected in ranking[:5]:
            recall += 1
            rank = ranking.index(case.expected) + 1
            rr_total += 1.0 / rank
    count = len(cases) or 1
    return {
        "recall_at_5": recall / count,
        "mrr": rr_total / count,
        "cases": float(len(cases)),
    }


def _run_mode(n: int, hybrid: bool, provider) -> dict[str, float | str]:  # noqa: ANN001
    """一个规模 × 模式的完整跑分：建库 → seed → rebuild → 逐 query 检索。"""
    memories, cases = build_case_set(n)
    root = Path(tempfile.mkdtemp(prefix="ts-scale-eval-"))
    log = EventLog(root / "events.db", blob_dir=root / "event_blobs")
    index = (
        MemoryIndex(log.conn, provider=provider)
        if hybrid
        else None
    )
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=root / "memory_blobs", clock=lambda: NOW, index=index
    )
    projection.ensure_schema()
    try:
        _seed_all(log, projection, memories)
        if hybrid:
            projection.rebuild_vectors()
        scope = MemoryScope.user()
        priority = RecallPriority(task_id="task_scale", workspace_roots=())
        rankings: list[list[str]] = []
        for case in cases:
            outcome = projection.retrieve(
                case.query,
                allowed=(scope,),
                limit=MAX_RECALL_ITEMS,
                max_bytes=MAX_RECALL_BYTES,
                priority=priority,
            )
            rankings.append([item.memory.memory_id for item in outcome.items])
        by_kind = {
            kind: _metrics(
                [r for r, c in zip(rankings, cases, strict=False) if c.kind == kind],
                tuple(c for c in cases if c.kind == kind),
            )
            for kind in ("lexical", "mixed")
        }
        row: dict[str, float | str] = {
            "mode": "hybrid" if hybrid else "fts",
            "n": n,
            **{f"all_{k}": v for k, v in _metrics(rankings, cases).items()},
            **{
                f"{kind}_{key}": value
                for kind, metrics in by_kind.items()
                for key, value in metrics.items()
            },
        }
        return row
    finally:
        log.close()


def _run_k_sensitivity(n: int, provider) -> list[dict[str, float | str]]:  # noqa: ANN001
    """N=1000 下 k ∈ {30,60,100} 的重排实验（索引层，两路名次独立获取）。"""
    memories, cases = build_case_set(n)
    root = Path(tempfile.mkdtemp(prefix="ts-scale-k-"))
    log = EventLog(root / "events.db", blob_dir=root / "event_blobs")
    index = MemoryIndex(log.conn, provider=provider)
    projection = MemoryProjection(
        log.conn, log=log, blob_dir=root / "memory_blobs", clock=lambda: NOW, index=index
    )
    projection.ensure_schema()
    try:
        _seed_all(log, projection, memories)
        projection.rebuild_vectors()
        scope = MemoryScope.user()
        now = NOW
        allowed_keys = set()
        from tianshu.memory import _allowed_keys  # 私有面：脚本实验用，不进产品口径

        allowed_keys = set(_allowed_keys((scope,)))
        rankings_by_query: dict[str, tuple[list[str], list[str]]] = {}
        for case in cases:
            fts = projection._fts_candidates(case.query, allowed_keys, now)
            fts_ranking = [m.memory_id for m, _content in fts]
            semantic = index.search(
                case.query, allowed=(scope,), limit=MAX_RECALL_ITEMS * 4, now=now
            )
            rankings_by_query[case.query] = (fts_ranking, semantic)
        rows: list[dict[str, float | str]] = []
        for k in K_STEPS:
            rankings = [
                [memory_id for memory_id, _score in rf_fuse([fts, sem], k=k)]
                for fts, sem in rankings_by_query.values()
            ]
            by_kind = {
                kind: _metrics(
                    [r for r, c in zip(rankings, cases, strict=False) if c.kind == kind],
                    tuple(c for c in cases if c.kind == kind),
                )
                for kind in ("lexical", "mixed")
            }
            rows.append(
                {
                    "mode": f"k{k}",
                    "n": n,
                    **{f"all_{key}": v for key, v in _metrics(rankings, cases).items()},
                    **{
                        f"{kind}_{key}": value
                        for kind, metrics in by_kind.items()
                        for key, value in metrics.items()
                    },
                }
            )
        return rows
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="千条级记忆检索评测（W6，只报实测）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--provider",
        choices=("hash", "openai"),
        default="hash",
        help="hash=确定性基线（默认）；openai=外部端点（读环境变量四件套）",
    )
    parser.add_argument(
        "--skip-k", action="store_true", help="跳过 k 敏感性（省时间）"
    )
    args = parser.parse_args(argv)

    if args.provider == "openai":
        from tianshu.embedding_openai import from_env

        provider = from_env()  # 缺配置在这里报错并点名缺什么
        provider_label = "openai"
    else:
        provider = HashEmbeddingProvider(dim=64)
        provider_label = "hash 基线（无语义能力，只说明两路融合在规模下的行为）"

    rows: list[dict[str, float | str]] = []
    for n in SCALE_STEPS:
        rows.append(_run_mode(n, hybrid=False, provider=None))
        rows.append(_run_mode(n, hybrid=True, provider=provider))
    if not args.skip_k:
        rows.extend(_run_k_sensitivity(SCALE_STEPS[-1], provider))

    if args.json:
        print(json.dumps({"provider": provider_label, "rows": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"千条级检索评测 · provider={provider_label}")
    print(f"{'模式':<12}{'N':>6}  {'Recall@5':>9}  {'MRR':>7}")
    for row in rows:
        mode, n = row["mode"], row["n"]
        recall, mrr = row["all_recall_at_5"], row["all_mrr"]
        print(f"{mode:<12}{n:>6}  {recall:>9.3f}  {mrr:>7.3f}")
    print()
    print("口径：hybrid 是 hash 基线，不代表语义召回；真 provider 分数需要")
    print("--provider openai 与真实端点凭据，没跑就没有数字。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
