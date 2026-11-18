"""F10 本地记忆：常量、异常、值对象、分词、写入闸门、Schema 与入参校验（内部模块）。

对外公开入口与重导出统一经 `tianshu.memory` 提供。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import TianshuError
from .events import canonical_bytes
from .resources import (
    MEMORY_SCOPES,
    SCOPE_TASK,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    MemoryScope,
)

# ---------------------------------------------------------------- 公开常量

#: 记忆种类（F10 §5）。封闭集：未知值在写入与读取两侧都抛错。
MEMORY_KINDS = frozenset({"preference", "fact", "decision", "procedure"})

#: 记忆状态（F10 §5）。`forgotten` 是 tombstone，不是删除。
MEMORY_STATUSES = frozenset({"candidate", "active", "superseded", "forgotten"})

#: 记忆来源（F10 §5）。`inferred` 不允许自动激活（激活走 confirm 事件）。
MEMORY_ORIGINS = frozenset({"user_explicit", "user_confirmed", "inferred"})

#: 单次召回最多条数（F10 §8 的提案值）。
MAX_RECALL_ITEMS = 5

#: 单次召回正文合计字节上限（UTF-8；F10 §8 的提案值）。
MAX_RECALL_BYTES = 4 * 1024

#: 分词器版本。变更分词规则必须改它；旧索引会被拒绝使用，要求先 rebuild()。
TOKENIZER_VERSION = "cjk-bigram-v1"

# ---------------------------------------------------------------- 封闭集字面量

_STATUS_CANDIDATE = "candidate"
_STATUS_ACTIVE = "active"
_STATUS_SUPERSEDED = "superseded"
_STATUS_FORGOTTEN = "forgotten"

_ORIGIN_WEIGHTS: dict[str, float] = {
    "user_explicit": 1.0,
    "user_confirmed": 0.6,
    "inferred": 0.3,
}

_CONTENT_AVAILABLE = "available"
_CONTENT_UNAVAILABLE = "content_unavailable"
_CONTENT_STATES = frozenset({_CONTENT_AVAILABLE, _CONTENT_UNAVAILABLE})

#: 记忆写工具。读工具（memory.search / memory.get）的 tool.result 也落事件，
#: 但它们不改投影，`apply_event()` 直接忽略。
_WRITE_TOOL_IDS = frozenset(
    {
        "memory.propose",
        "memory.remember",
        "memory.confirm",
        "memory.forget",
        "memory.edit",
        "memory.promote",
        "memory.erase",
        "memory.consolidate",
    }
)

#: 记忆资产层（F10 §4 / 契约 v1.2）。`kind` 说"是什么"，`layer` 说"多粗"。
MEMORY_LAYERS = frozenset({"l1", "l2", "l3"})

#: 投影格式版本。投影哈希覆盖它，跨版本不假装可比。
_SCHEMA_VERSION = 3

# ---------------------------------------------------------------- 错误


class MemoryStoreError(TianshuError):
    """记忆投影拒绝了一个输入/状态。

    与"暂时没有这条记忆"（返回 ``None``）分开：这里都是**契约级**拒绝——
    未知枚举、缺字段、来源为空、revision 回退、正文绕过写入闸门等。
    """


class MemoryWriteBlocked(MemoryStoreError):
    """写入闸门判定整条阻断。调用方不得把该候选写进 blob 或投影。"""


# ---------------------------------------------------------------- 值对象


@dataclass(frozen=True)
class Memory:
    """记忆资产（F10 §5 / PRD §4.1）。投影行读取或写入后的只读视图。"""

    memory_id: str
    revision: int
    scope: str
    scope_id: str
    kind: str
    content_ref: str
    source_refs: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    proof_count: int
    origin: str
    status: str
    consolidated_at: int | None
    created_at: int
    expires_at: int | None
    supersedes: str | None
    #: 资产层（契约 v1.2）：`l1` 原子事实 / `l2` 带证据的信念 / `l3` 偏好页面。
    #: L2/L3 是**可重建的合成层**（F10 §4），与 `kind` 正交。
    layer: str = "l1"
    #: 物理擦除时间（契约 v1.2）：与 `status=forgotten` 分开——forgotten 只是停召回，
    #: erased_at 表示正文副本与检索索引已被删除。
    erased_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好的字典。元组转列表，其余原样（调用方不得据此伪造事件）。"""
        return {
            "memory_id": self.memory_id,
            "revision": self.revision,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "kind": self.kind,
            "content_ref": self.content_ref,
            "source_refs": list(self.source_refs),
            "evidence": [dict(item) for item in self.evidence],
            "proof_count": self.proof_count,
            "origin": self.origin,
            "status": self.status,
            "consolidated_at": self.consolidated_at,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "supersedes": self.supersedes,
            "layer": self.layer,
            "erased_at": self.erased_at,
        }


@dataclass(frozen=True)
class Recalled:
    """一次召回结果：记忆元数据 + 该次实际返回的正文 + 排序分数。

    ``content`` 可能比投影里的正文短：字节预算裁剪发生在结果上，不改投影。
    ``score`` 是 origin 权重的固定值，**不是** FTS 的 bm25。
    """

    memory: Memory
    content: str
    score: float
    #: `score` 的口径：`origin`（词面路的 origin 权重）或 `rrf`（融合分数）。
    #: 两种分数不可比，必须让调用方一眼看出拿的是哪一种。
    score_kind: str = "origin"
    #: L2/L3 合成层是否已陈旧（作用域里有比它更新的 L1）：陈旧页降权但**不短路**，
    #: 同时仍保留 L1 证据（F10 §8「陈旧层先核验再用」）。
    #:
    #: **三态，不是二态**：`True` 陈旧 / `False` 已确认新鲜（页的 `basis_seq` 不小于
    #: 作用域里最大的 L1 `last_seq`）/ `None` **不可判定**（旧行没有 `seq:` 标记）。
    #: 把"判不了"写成 `False` 就是谎报新鲜——调用方会以为"这页跟得上"。
    stale: bool | None = None


#: 作用域优先级里的"最近"档位（数值越小越靠前）。
_PROXIMITY_TASK = 0
_PROXIMITY_WORKSPACE = 1
_PROXIMITY_USER = 2
#: 其它已授权作用域（v1 的装备里通常不会出现，但排序键必须对全集有定义）。
_PROXIMITY_OTHER = 3


@dataclass(frozen=True)
class RecallPriority:
    """这次检索"离谁更近"：当前任务 / 本 Run 绑定的 workspace。

    F10 §8 的"优先当前任务 / 工作区证据"。它是**调用方上下文**的函数，不是
    `allowed` 的函数：同一份库内容 + 同一组 `allowed`，只要当前任务和绑定目录
    不变，名次就逐位可复现（这正是旧口径担心的"顺序依赖调用方传了哪些
    scope"的解药——`allowed` 只决定看不看得见，优先级只由自身的 scope 与
    Run 的上下文决定）。
    """

    task_id: str | None = None
    workspace_roots: tuple[str, ...] = ()

    def proximity(self, scope: MemoryScope) -> int:
        return self.proximity_pair(scope.scope, scope.scope_id)

    def proximity_pair(self, scope: str, scope_id: str) -> int:
        """按 `(scope, scope_id)` 判档：不构造对象，排序键里每一行都要算一次。"""
        if scope == SCOPE_TASK:
            # 没有当前任务时 task 作用域与"其它已授权作用域"同档（排在 user 之后）：
            # 缺上下文就不该假装知道"哪个任务更近"。这与 `priority=None`（完全
            # 没有优先级、退回旧序）是两种不同状态，见 `RecallsPriority` docstring。
            if self.task_id is None:
                return _PROXIMITY_OTHER
            return _PROXIMITY_TASK if scope_id == self.task_id else _PROXIMITY_OTHER
        if scope == SCOPE_WORKSPACE and scope_id in self.workspace_roots:
            return _PROXIMITY_WORKSPACE
        if scope == SCOPE_USER:
            return _PROXIMITY_USER
        return _PROXIMITY_OTHER


@dataclass
class RecallOutcome:
    """一次检索的完整结果：条目 + **这一路到底有没有语义能力**。

    `semantic=False` 不是"没找到"，而是"这次只有词面召回"；`degraded` 给出
    原因（未配置 provider / 向量索引是旧 provider 建的 / 还没重建）。把两者
    混成一句"没结果"就是对能力撒谎。
    """

    items: list[Recalled] = field(default_factory=list)
    semantic: bool = False
    degraded: str | None = None
    fused: bool = False


def _proximity_of(memory: Memory, priority: RecallPriority | None) -> int:
    """作用域优先级；没有上下文时所有行同档（排序退化成旧的 origin → 时间）。"""
    if priority is None:
        return 0
    return priority.proximity_pair(memory.scope, memory.scope_id)


#: 新鲜度位次：`fresh` → `unknown` → `stale`。
#:
#: 三档不是二档：`unknown`（旧行没有 `seq:` 基准）**既不冒充新鲜、也不冒充陈旧**，
#: 排在"已确认新鲜"之后、"已确认陈旧"之前。`L1` 永远是 0——原子事实没有"合成层
#: 陈旧"这回事，`_stale_map` 也不会给它放键。
_FRESHNESS_FRESH = 0
_FRESHNESS_UNKNOWN = 1
_FRESHNESS_STALE = 2


def _freshness_rank(memory: Memory, stale: dict[str, bool | None] | None) -> int:
    if memory.layer == "l1" or stale is None:
        return _FRESHNESS_FRESH
    value = stale.get(memory.memory_id)
    if value is None:
        return _FRESHNESS_UNKNOWN
    return _FRESHNESS_STALE if value else _FRESHNESS_FRESH


def _order_by_priority(
    candidates: Sequence[tuple[Memory, str]],
    priority: RecallPriority | None,
    *,
    stale: dict[str, bool | None] | None = None,
) -> list[tuple[Memory, str]]:
    """词面路的名次：`(作用域优先级, 新鲜度, origin 权重, created_at 降序, memory_id)`。

    新鲜度排在 origin 权重**之前**、作用域优先级**之后**：同一优先级分组里，
    陈旧的合成层一律排在新鲜记忆后面——否则一条 `origin=inferred` 的旧 L2 页
    会因为权重比不上 `user_confirmed` 的 L1 而"看起来已经排对了"，等哪天来源
    权重相同（都是 `inferred`）时旧页又跑到第一（复审探针就是这样复现的）。
    三档位次见 `_FRESHNESS_*`：unknown 不冒充任何一档（旧行没有 `seq:` 时既不
    假设它新鲜、也不假设它陈旧）。作用域优先级仍是第一分组键。
    """
    return sorted(
        candidates,
        key=lambda item: (
            _proximity_of(item[0], priority),
            _freshness_rank(item[0], stale),
            -_ORIGIN_WEIGHTS[item[0].origin],
            -item[0].created_at,
            item[0].memory_id,
        ),
    )


def _crop(
    ordered: Sequence[tuple[Memory, str]],
    limit: int,
    max_bytes: int,
    *,
    scores: dict[str, float] | None = None,
    score_kind: str = "origin",
    stale: dict[str, bool] | None = None,
) -> list[Recalled]:
    """按名次裁剪到条数 + 正文字节上限。`score` 是这次排序用的分数。"""
    recalled: list[Recalled] = []
    used_bytes = 0
    for memory, content in ordered:
        if len(recalled) >= limit:
            break
        piece = _truncate_utf8(content, max_bytes - used_bytes)
        if piece is None:
            break
        size = len(piece.encode("utf-8"))
        score = (
            scores[memory.memory_id]
            if scores is not None
            else _ORIGIN_WEIGHTS[memory.origin]
        )
        recalled.append(
            Recalled(
                memory=memory,
                content=piece,
                score=score,
                score_kind=score_kind,
                stale=stale.get(memory.memory_id) if stale is not None else None,
            )
        )
        used_bytes += size
        if size < len(content.encode("utf-8")):
            # 本条被截断：后面不可能再放得下，直接结束。
            break
    return recalled


def _rf_fuse(fts_order: Sequence[str], semantic_order: Sequence[str]) -> list[tuple[str, float]]:
    """两路名次的 RRF 融合（`score = Σ 1/(60+rank)`，同分按 id 稳定）。

    延迟 import：`memory_index` 要 import 本模块的错误类型与实体，模块级互相
    import 会成环；融合只在这里用到，运行期再取。
    """
    from .memory_index import rf_fuse

    return rf_fuse([list(fts_order), list(semantic_order)])


# ---------------------------------------------------------------- 时钟


def _now() -> int:
    """墙钟毫秒。与事件 `ts` 同口径。"""
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 分词


def _is_cjk(char: str) -> bool:
    """是否一个汉字。用码点区间而不是 `unicodedata`，跨平台结果完全一致。"""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF  # CJK Ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK Unified
        or 0xF900 <= code <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x20000 <= code <= 0x2FA1F  # CJK Ext B–F
    )


def _is_ascii_word(char: str) -> bool:
    """`[A-Za-z0-9_]` 的逐字符判定（不用 str.isalnum，它接受非 ASCII 字母）。"""
    return (
        "a" <= char <= "z"
        or "A" <= char <= "Z"
        or "0" <= char <= "9"
        or char == "_"
    )


def _tokenize(text: str) -> tuple[str, ...]:
    """确定性切分：中文相邻双字 + 英文/数字/下划线整词（小写）。

    顺序保留、重复剔除。单字中文成词（没有相邻字可组双字）；跨标点的汉字
    不组双字，所以 ``报告用中文、先列结论`` 的 token 是
    ``报告 告用 用中 中文 先列 列结 结论``。

    例：``中文结论`` → ``中文 文结 结论``；``memory.search`` →
    ``memory search``；``ToolRegistry`` → ``toolregistry``。
    """
    if not isinstance(text, str):
        raise MemoryStoreError(f"分词输入必须是字符串：{type(text).__name__}")

    tokens: list[str] = []
    seen: set[str] = set()
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if _is_cjk(char):
            end = index + 1
            while end < length and _is_cjk(text[end]):
                end += 1
            run = text[index:end]
            pieces = [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
            for piece in pieces:
                if piece not in seen:
                    seen.add(piece)
                    tokens.append(piece)
            index = end
            continue
        if _is_ascii_word(char):
            end = index + 1
            while end < length and _is_ascii_word(text[end]):
                end += 1
            word = text[index:end].lower()
            if word not in seen:
                seen.add(word)
                tokens.append(word)
            index = end
            continue
        index += 1
    return tuple(tokens)


# ---------------------------------------------------------------- 写入闸门


# 密钥类：命中即整条阻断。宁可错杀一条候选，也不能把密钥写进内容寻址目录。
_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}")),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
)

# 长 base64 疑似密钥。真正的判定还要看大小写/数字混合，见
# `_looks_like_base64_secret()`：纯十六进制摘要（git sha / sha256）不算。
_BASE64_PATTERN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")
_HEX_PATTERN = re.compile(r"[0-9A-Fa-f]+")

# PII 类：替换成占位符，不阻断整条记忆。
_REDACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email",
        re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
)

_CN_ID_CHECK_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_ID_CHECK_CODES = "10X98765432"


def _valid_cn_id(candidate: str) -> bool:
    """18 位身份证：出生日期形状合理 + GB 11643 校验位正确。

    只做形状判断会把 18 位订单号也红掉；校验位能机械区分"真身份证形状"。
    校验位不对时**不红**——否则任何 18 位数字都会被吞掉，反而掩盖真号码。
    """
    if len(candidate) != 18:
        return False
    year = candidate[6:10]
    month = candidate[10:12]
    day = candidate[12:14]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return False
    if not (1900 <= int(year) <= 2099 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return False
    total = sum(int(candidate[i]) * _CN_ID_CHECK_WEIGHTS[i] for i in range(17))
    return _CN_ID_CHECK_CODES[total % 11] == candidate[17].upper()


def _looks_like_base64_secret(token: str) -> bool:
    """长 base64 串里同时出现大写、小写、数字才当密钥；纯十六进制摘要排除。"""
    if _HEX_PATTERN.fullmatch(token):
        return False
    has_lower = any("a" <= ch <= "z" for ch in token)
    has_upper = any("A" <= ch <= "Z" for ch in token)
    has_digit = any("0" <= ch <= "9" for ch in token)
    return has_lower and has_upper and has_digit


# ---------------------------------------------------------------- schema


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memories (
        memory_id       TEXT PRIMARY KEY,
        revision        INTEGER NOT NULL,
        scope           TEXT NOT NULL,
        scope_id        TEXT NOT NULL,
        kind            TEXT NOT NULL,
        content_ref     TEXT NOT NULL,
        source_refs     TEXT NOT NULL,
        evidence        TEXT NOT NULL,
        proof_count     INTEGER NOT NULL,
        origin          TEXT NOT NULL,
        status          TEXT NOT NULL,
        consolidated_at INTEGER,
        created_at      INTEGER NOT NULL,
        expires_at      INTEGER,
        supersedes      TEXT,
        layer           TEXT NOT NULL DEFAULT 'l1',
        content_state   TEXT NOT NULL,
        last_seq        INTEGER NOT NULL,
        fts_rowid       INTEGER,
        erased_at       INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_scope
        ON memories(scope, scope_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_dedup
        ON memories(scope, scope_id, kind, content_ref)
    """,
    # 机械去重：同一作用域 + 种类 + 正文，在 candidate/active 之间只能有一条。
    # 已遗忘 / 已取代的行不占键位，所以重新提出同一事实是合法的。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_dedup
        ON memories(scope, scope_id, kind, content_ref)
        WHERE status IN ('candidate', 'active')
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # memory_id 是 UNINDEXED 列：只用来关联回 memories，不参与词面匹配。
    # tokenchars '_' 让 `memory_search` 保持一个 token，和 _tokenize() 的
    # `[A-Za-z0-9_]+` 口径一致。
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        memory_id UNINDEXED,
        tokens,
        tokenize = "unicode61 tokenchars '_'"
    )
    """,
)

_MEMORY_COLUMNS = (
    "memory_id, revision, scope, scope_id, kind, content_ref, source_refs, "
    "evidence, proof_count, origin, status, consolidated_at, created_at, "
    "expires_at, supersedes, layer, content_state, last_seq, fts_rowid, erased_at"
)

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------- 校验小工具


def _require_str(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise MemoryStoreError(f"{field} 必须是字符串：{value!r}")
    if not allow_empty and not value:
        raise MemoryStoreError(f"{field} 不能为空")
    return value


def _require_int(value: object, field: str, *, minimum: int | None = None) -> int:
    # bool 是 int 的子类，但 `revision=True` 不是版本号，是类型错误。
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryStoreError(f"{field} 必须是整数：{value!r}")
    if minimum is not None and value < minimum:
        raise MemoryStoreError(f"{field} 不能小于 {minimum}：{value!r}")
    return value


def _require_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, field, minimum=0)


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise MemoryStoreError(f"{field} 必须是布尔值：{value!r}")
    return value


def _require_closed(value: object, allowed: frozenset[str], field: str) -> str:
    text = _require_str(value, field)
    if text not in allowed:
        raise MemoryStoreError(f"{field} 不在封闭集：{text!r}（合法值：{sorted(allowed)}）")
    return text


def _require_str_tuple(
    value: object, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        raise MemoryStoreError(f"{field} 必须是字符串列表：{value!r}")
    result = tuple(_require_str(item, f"{field}[{index}]") for index, item in enumerate(items))
    if not result and not allow_empty:
        # F10 §5：每条记忆必须能指回来源。空来源的"记忆"是不可审计的断言。
        raise MemoryStoreError(f"{field} 不能为空：记忆必须能指回来源")
    return result


def _require_json_list(value: object, field: str) -> tuple[dict[str, Any], ...]:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        raise MemoryStoreError(f"{field} 必须是对象列表：{value!r}")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise MemoryStoreError(f"{field}[{index}] 必须是对象：{item!r}")
        try:
            canonical_bytes(item)
        except (TypeError, ValueError) as exc:
            raise MemoryStoreError(f"{field}[{index}] 不能序列化成 JSON：{exc}") from exc
        result.append(dict(item))
    return tuple(result)


def _require_content_ref(value: object) -> str:
    text = _require_str(value, "content_ref")
    if not _SHA256_HEX.fullmatch(text):
        raise MemoryStoreError(f"content_ref 必须是 sha256 十六进制：{text!r}")
    return text


def _require_memory_id(value: object) -> str:
    text = _require_str(value, "memory_id")
    if len(text) > 256:
        raise MemoryStoreError("memory_id 过长（>256）")
    if any(ord(ch) < 0x20 for ch in text):
        raise MemoryStoreError("memory_id 不能包含控制字符")
    return text


def _scope_from_payload(scope: object, scope_id: object) -> MemoryScope:
    scope_text = _require_closed(scope, MEMORY_SCOPES, "scope")
    scope_id_text = _require_str(scope_id, "scope_id")
    try:
        if scope_text == SCOPE_WORKSPACE:
            return MemoryScope.workspace(scope_id_text)
        return MemoryScope(scope_text, scope_id_text)
    except TianshuError as exc:
        raise MemoryStoreError(f"作用域不合法：{scope_text!r}/{scope_id_text!r}（{exc}）") from exc


def _scope_key(scope: MemoryScope) -> tuple[str, str]:
    return (scope.scope, scope.scope_id)


def _allowed_keys(allowed: Sequence[MemoryScope]) -> tuple[tuple[str, str], ...]:
    if isinstance(allowed, (str, bytes)):
        raise MemoryStoreError("allowed 必须是 MemoryScope 序列，不是字符串")
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(allowed):
        if not isinstance(item, MemoryScope):
            raise MemoryStoreError(f"allowed[{index}] 必须是 MemoryScope：{item!r}")
        key = _scope_key(item)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def _redact_id_card(match: re.Match[str]) -> str:
    """只红掉校验位通过的身份证形状；普通 18 位数字原样保留。"""
    candidate = match.group(0)
    return "[REDACTED:id_card]" if _valid_cn_id(candidate) else candidate


def _redact(content: str) -> str:
    result = content
    for kind, pattern in _REDACT_PATTERNS:
        if kind == "id_card":
            result = pattern.sub(_redact_id_card, result)
        else:
            result = pattern.sub(f"[REDACTED:{kind}]", result)
    return result


def _truncate_utf8(content: str, max_bytes: int) -> str | None:
    """把正文裁到不超过 ``max_bytes`` 的前缀；剩余不足一个字符返回 None。

    按字节切在 UTF-8 多字节字符中间时逐字节回退，保证返回值可解码。
    """
    if max_bytes <= 0:
        return None
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    cut = encoded[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return None


def _write_tool_id(payload: object) -> str | None:
    """这条 tool.result 是否会改记忆投影；会就返回工具 id。"""
    if not isinstance(payload, dict):
        return None
    tool_id = payload.get("tool_id")
    if tool_id not in _WRITE_TOOL_IDS:
        return None
    if payload.get("status") != "succeeded":
        return None
    return str(tool_id)
