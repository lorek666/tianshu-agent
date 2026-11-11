"""L2/L3 确定性合成（F10 §4 / §8 的 freshness 基础）。

**这一版只做确定性合成，LLM 摘要明确延后**（SPEC §4.2 第 6 条）：生成式合成会让
"两次合成是否一致"变成不可机械核对的事，而 L2/L3 的立论恰恰是"可重建、可回放、
能和证据逐条对账"。所以：

- L2 = 同作用域 `active` L1 按**稳定 token 交叠**聚类，正文是引用式拼装；
- L3 = user 作用域偏好页，按字节预算打包成"启动即可用"的页面；
- 两次调用**逐字节相同**；改一条源只会动与它相关的那一块（增量 delta 天然成立）。

不承诺"比 L1 更好用"——除非评测拿出来（现在没有）。L2/L3 是**投影**，不是第二份
事实：正文放进内容寻址目录、证据写 `memory_id + revision + 原文引文`，重建时按事件
重放即可复现同一页。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .memory import MEMORY_LAYERS, MemoryStoreError

__all__ = [
    "L2_MIN_GROUP",
    "L2_SIMILARITY",
    "L3_PAGE_BYTES",
    "ConsolidationSource",
    "Page",
    "build_l2_page",
    "build_l3_page",
    "consolidate",
    "source_refs_for",
]

#: 聚类阈值：两个 token 集合的**重叠系数**（|A∩B| / min(|A|,|B|)）≥ 该值视为
#: "同一场景"。用重叠系数而不是 Jaccard：中文短句里 bigram 很多，Jaccard 对
#: 长度差极度敏感，"报告用中文写" 与 "报告用中文，先列结论" 会被判成不相似。
#: 写死并进测试，不许悄悄调——调了就是换了聚类口径，得重跑判据。
L2_SIMILARITY = 0.34

#: L2 至少两行才算"场景"：单条事实不需要 L2 层（回落 L1）。
L2_MIN_GROUP = 2

#: L3 页面字节预算（F10 §8：预算好的可启动页面）。
L3_PAGE_BYTES = 4 * 1024

#: 证据引文的截断上限（字节）。
QUOTE_LIMIT_BYTES = 200

#: `source_refs` 里记录"这一页看到的最大逻辑时钟"的标记前缀。
BASIS_SEQ_PREFIX = "seq:"


@dataclass(frozen=True)
class ConsolidationSource:
    """合成输入：一条**可参与合成**的记忆（只读快照，不持有投影行）。"""

    memory_id: str
    revision: int
    content: str
    last_seq: int
    kind: str
    origin: str
    layer: str = "l1"


@dataclass(frozen=True)
class Page:
    """一页合成结果（L2 或 L3）。写入投影时按这些字段落行。"""

    layer: str
    kind: str
    content: str
    evidence: tuple[dict[str, Any], ...]
    proof_count: int
    source_refs: tuple[str, ...]
    basis_seq: int

    def to_output(self) -> dict[str, Any]:
        """工具 result 里的形状（正文不在事件里，只给引用与证据）。"""
        return {
            "layer": self.layer,
            "kind": self.kind,
            "evidence": [dict(item) for item in self.evidence],
            "proof_count": self.proof_count,
            "source_refs": list(self.source_refs),
            "basis_seq": self.basis_seq,
        }


def source_refs_for(members: list[ConsolidationSource]) -> tuple[str, ...]:
    """来源引用：`seq:<basis_seq>` + 每个成员的 `memory_id`（稳定排序）。

    `seq:` 那一项是 freshness 的判据：页里"看到"的最大 L1 `last_seq`。作用域里
    后来出现 `last_seq` 更大的 L1 时，这一页就陈旧了。
    """
    basis = max((member.last_seq for member in members), default=0)
    ids = sorted(member.memory_id for member in members)
    return (f"{BASIS_SEQ_PREFIX}{basis}", *ids)


def parse_basis_seq(source_refs: tuple[str, ...]) -> int | None:
    """从 `source_refs` 里读回 `basis_seq`（旧行没有就 None，不猜）。"""
    for ref in source_refs:
        if ref.startswith(BASIS_SEQ_PREFIX):
            raw = ref[len(BASIS_SEQ_PREFIX) :]
            try:
                value = int(raw)
            except ValueError:
                return None
            return value if value >= 0 else None
    return None


def _tokens(text: str) -> frozenset[str]:
    """稳定 token 集合：英文/数字词 + 汉字单字 + 相邻汉字双字组。

    与 FTS 分词器**分开实现**（聚类只要求确定性，不要求与检索同一套切分）。
    单字与双字同时收：只用双字时，短句之间的交叠被长度差压垮（"报告用中文" vs
    "报告用中文写" 只差一个 bigram）；只用单字则被"的/用"这类高频字主导。
    """
    lowered = text.lower()
    tokens: set[str] = set()
    word: list[str] = []
    cjk_run: list[str] = []
    for char in lowered:
        if "\u4e00" <= char <= "\u9fff":
            if word:
                tokens.add("".join(word))
                word = []
            cjk_run.append(char)
            tokens.add(char)
            continue
        if cjk_run:
            if len(cjk_run) >= 2:
                tokens.update(
                    cjk_run[index] + cjk_run[index + 1]
                    for index in range(len(cjk_run) - 1)
                )
            cjk_run = []
        if char.isalnum():
            word.append(char)
        elif word:
            tokens.add("".join(word))
            word = []
    if cjk_run and len(cjk_run) >= 2:
        tokens.update(
            cjk_run[index] + cjk_run[index + 1]
            for index in range(len(cjk_run) - 1)
        )
    if word:
        tokens.add("".join(word))
    return frozenset(tokens)


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """重叠系数：`|A∩B| / min(|A|,|B|)`（任一侧为空视为不相似）。"""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _one_line(content: str) -> str:
    """正文压成一行（换行/连续空白折成单个空格），用于引用式拼装。"""
    return " ".join(content.split())


def _quote(content: str) -> str:
    """证据引文：截断到 `QUOTE_LIMIT_BYTES`，截断处明确标注。"""
    raw = content.encode("utf-8")
    if len(raw) <= QUOTE_LIMIT_BYTES:
        return content
    cut = raw[:QUOTE_LIMIT_BYTES]
    while cut and cut[-1:] and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore") + "…[截断]"


def _line(member: ConsolidationSource) -> str:
    return f"- {_one_line(member.content)} [{member.memory_id}@{member.revision}]"


def _clusters(
    sources: list[ConsolidationSource],
) -> list[list[ConsolidationSource]]:
    """按 token 交叠聚簇（确定性）。

    实现是"按 memory_id 排序后逐个并入第一个够像的簇"：不做层次聚类/不依赖打分
    顺序，同一批输入必然得到同一批簇（换成 union-find 也要写成确定性的并查规则，
    否则两次合成可能差一条）。
    """
    token_map = {source.memory_id: _tokens(source.content) for source in sources}
    clusters: list[list[ConsolidationSource]] = []
    for source in sources:
        tokens = token_map[source.memory_id]
        placed = False
        for cluster in clusters:
            representative = cluster[0]
            if _similarity(tokens, token_map[representative.memory_id]) >= L2_SIMILARITY:
                cluster.append(source)
                placed = True
                break
        if not placed:
            clusters.append([source])
    return clusters


def build_l2_page(sources: list[ConsolidationSource]) -> Page | None:
    """L2 场景页：聚成 ≥2 条的簇，引用式拼装，每条一行、块间空行。"""
    members = sorted(sources, key=lambda item: item.memory_id)
    clusters = [
        cluster
        for cluster in _clusters(members)
        if len(cluster) >= L2_MIN_GROUP
    ]
    if not clusters:
        return None
    clusters.sort(key=lambda cluster: cluster[0].memory_id)
    blocks: list[str] = []
    evidence: list[dict[str, Any]] = []
    used: list[ConsolidationSource] = []
    for cluster in clusters:
        cluster = sorted(cluster, key=lambda item: item.memory_id)
        blocks.append("\n".join(_line(member) for member in cluster))
        for member in cluster:
            evidence.append(
                {
                    "memory_id": member.memory_id,
                    "revision": member.revision,
                    "quote": _quote(member.content),
                }
            )
            used.append(member)
    content = "\n\n".join(blocks) + "\n"
    return Page(
        layer="l2",
        kind="fact",
        content=content,
        evidence=tuple(evidence),
        proof_count=len(evidence),
        source_refs=source_refs_for(used),
        basis_seq=max(member.last_seq for member in used),
    )


def build_l3_page(sources: list[ConsolidationSource]) -> Page | None:
    """L3 偏好页：user 作用域里"人给的"偏好，按 4 KiB 预算打包。

    `kind=preference`、`origin ∈ {user_explicit, user_confirmed}`、`active` 才算
    该进页面的内容。行按 memory_id 排；放不下时**整行**不进（不做半行截断），
    并在末尾写一行"预算已满"，让读页面的人知道这不是全部。
    """
    members = sorted(
        (
            source
            for source in sources
            if source.kind == "preference"
            and source.origin in ("user_explicit", "user_confirmed")
            and source.layer == "l1"
        ),
        key=lambda item: item.memory_id,
    )
    if not members:
        return None
    header = "# 偏好页面（确定性合成，按 memory_id 排序）\n\n"
    lines: list[str] = []
    evidence: list[dict[str, Any]] = []
    used: list[ConsolidationSource] = []
    used_bytes = len(header.encode("utf-8"))
    truncated = False
    for member in members:
        line = _line(member) + "\n"
        size = len(line.encode("utf-8"))
        if used_bytes + size > L3_PAGE_BYTES:
            truncated = True
            break
        lines.append(line)
        evidence.append(
            {
                "memory_id": member.memory_id,
                "revision": member.revision,
                "quote": _quote(member.content),
            }
        )
        used.append(member)
        used_bytes += size
    if not used:
        return None
    content = header + "".join(lines)
    if truncated:
        content += "\n[预算已满：仅包含按 memory_id 排序的前若干条]\n"
    return Page(
        layer="l3",
        kind="preference",
        content=content,
        evidence=tuple(evidence),
        proof_count=len(evidence),
        source_refs=source_refs_for(used),
        basis_seq=max(member.last_seq for member in used),
    )


def consolidate(
    sources: list[ConsolidationSource], *, scope: Any
) -> tuple[Page, ...]:
    """一次合成：该作用域的 L2（若有簇）+ 该作用域的 L3（user 作用域）。

    返回顺序固定（L2 在前、L3 在后）：写入投影时行序稳定，重建才能逐位一致。
    """
    pages: list[Page] = []
    l2 = build_l2_page(sources)
    if l2 is not None:
        pages.append(l2)
    # L3 只对 user 作用域有意义：偏好页是"关于这个人"的页面，workspace / task
    # 的偏好不是同一层语义。
    if scope.scope == "user":
        l3 = build_l3_page(sources)
        if l3 is not None:
            pages.append(l3)
    for page in pages:
        if page.layer not in MEMORY_LAYERS or page.layer == "l1":
            raise MemoryStoreError(f"合成页的 layer 非法：{page.layer!r}")
        if page.proof_count != len(page.evidence):
            raise MemoryStoreError(
                f"合成页的 proof_count={page.proof_count} 与证据条数 "
                f"{len(page.evidence)} 不一致"
            )
    return tuple(pages)


def page_memory_id(scope_uri: str, page: Page) -> str:
    """合成页的**确定性** memory_id：同一作用域 + 同一层 + 同一正文 ⇒ 同一个 id。

    这样重复 `consolidate` 是幂等的（同一页不会无限增长），也让"旧页被新页
    supersede"的关系可以按 (scope, layer) 找到，而不是靠时间戳猜。
    """
    digest = hashlib.sha256(
        f"{scope_uri}\x00{page.layer}\x00{page.content}".encode()
    ).hexdigest()
    return f"mem_{digest[:16]}"


def summarize_for_tests(page: Page) -> dict[str, Any]:
    """测试/审计用的小结（不参与写入路径）。"""
    return {
        "layer": page.layer,
        "kind": page.kind,
        "bytes": len(page.content.encode("utf-8")),
        "proof_count": page.proof_count,
        "basis_seq": page.basis_seq,
        "memory_ids": [item["memory_id"] for item in page.evidence],
    }