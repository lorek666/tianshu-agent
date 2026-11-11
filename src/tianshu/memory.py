"""F10 本地记忆：事件日志的另一个可重建投影。

## 形状

记忆不是第二个事实源（红线 1）。事件日志仍是唯一提交点：

- 写入侧只由 dispatcher 记 `tool.result` 事件（`memory.propose` /
  `memory.confirm` / `memory.forget`），本模块**不写事件**；
- 本模块只维护投影表（`memories` / `memory_fts`）与正文的内容寻址 blob；
- ``rebuild()`` 清空投影后按 seq 重放同一批事件，**且重放也走
  ``apply_event()``**——投影更新只有一条代码路径，重建不可能和实时写入
  走岔。

因此删掉投影不丢事实；正文按 `content_ref`（sha256）从内容寻址目录读回。
事件里只存引用与审计元数据，不重复存正文。

## 为什么必须和事件日志共用同一个 sqlite 连接

F10 §7 要求投影更新与事件**同事务**。分库做不到原子：事件提交了、投影没
提交，重建能修；投影提交了、事件没提交，这条记忆就成了没有来源的孤证。
所以构造时强制 ``conn is log.conn``，第二连接直接拒绝——宁可在这里炸，
也不要在崩溃后才发现两边不一致。

## 忘记 = tombstone，不是删行

``forget`` 把 `status` 改成 ``forgotten``、从 FTS 移除，但**保留 memories 行**。
于是重建时按事件顺序重放 propose → confirm → forget，最终仍是 forgotten；
"删一行就完事"的写法在重建时必然复活。物理擦除（连同历史事件与 blob）是
另一张契约工单，本模块不声称做过。

## 检索顺序（F10 §8，固定）

**先过滤 → 再排序 → 后裁剪**：

1. 过滤：`allowed` 作用域、`expires_at` 未过期、`status == "active"`；
2. 排序：`origin` 权重 `user_explicit > user_confirmed > inferred`，同权重按
   `created_at` 降序；
3. 裁剪：条数 ``limit`` + 正文字节 ``max_bytes``（超了截断，不是返回空）。

F10 §8 还要求「优先当前任务 / 工作区证据」，现在由 `RecallPriority` 实现
（N2）：排序键是 `(作用域优先级, origin 权重, created_at 降序, memory_id)`。
它是**调用方上下文**的函数，不是 `allowed` 的函数——`allowed` 只决定看不看
得见，优先级只由"这条记忆自己的 scope"与"当前 Run 的 task / workspace"决定，
所以同一份库内容 + 同一组 `allowed` + 同一上下文必然排出逐位相同的名次。

FTS5（``bm25`` 可用）负责**产生候选**，并按名次参与 RRF 融合；但**过滤永远
在排序与融合之前**：让关键词分数或向量相似度绕过作用域过滤，等于给越权召回
开了一道后门。

## 中文词面召回

中文没有空格分词，所以这里不用 SQLite 默认分词器去猜：

- 相邻两个汉字组成一个 token（确定性双字切分）；
- 英文/数字/下划线按 ``[A-Za-z0-9_]+`` 切，整体小写；
- ``TOKENIZER_VERSION`` 落库。版本不符时**拒绝使用**并提示先 ``rebuild()``，
  不静默按新分词器解释旧索引。

把 token 以空格拼好再灌进 FTS5（``tokenize = "unicode61 tokenchars '_'"``），
查询也走同一套切分，两边不会各说一套。

## 写入闸门

``redact_or_block()`` 是**写入前**的 PII / 密钥扫描，不是事后清理：

- 密钥类（`sk-`、`AKIA`、`ghp_` / `github_pat_`、私钥头、长 base64 疑似密钥）
  **整条阻断**，调用方不得入库；
- 邮箱 / 中国手机号 / 身份证号替换成 ``[REDACTED:<kind>]``。

``put_content()`` 自己再过一遍闸门：被阻断的内容连 blob 都写不进去；
`apply_event()` 读回正文时再核一次，防止绕过 `put_content()` 手工塞 blob。

## 明确不做

- 不做多租户 / owner / ACL（D2 单用户），作用域只有 user / workspace / task；
- 不做向量召回，不引入 embedding 依赖；
- 不把任务状态、grant、审批决定存成可覆盖的普通记忆；
- 不声称物理擦除（见上）；
- 不做 L2/L3 蒸馏与 freshness 判定（本模块只承载 L1 资产；`consolidated_at`
  字段保留给蒸馏层读）；
- **confirm 不改写 origin**：confirm 的 payload 里没有 origin，投影不替执行器
  发明"inferred → user_confirmed"这个来源迁移。要让确认后的来源显示为
  `user_confirmed`，必须由 proposer 在 propose 时给出（或另开契约加字段）。
  这是刻意的：来源是审计材料，不能由投影按"看起来应该是"改写。
- **作用域不参与排序**：F10 §8 的另一句"优先当前任务 / 工作区证据"没有实现。
  本次排序固定为 origin → created_at DESC；scope 在过滤阶段已决定可见性，
  再参与排序会让同一权重内的顺序取决于"调用方传了哪些 scope"，不可复现。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .events import EventLog, canonical_bytes, sha256_hex
from .fsutil import atomic_write_bytes
from .resources import (
    MEMORY_SCOPES,
    SCOPE_TASK,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    MemoryScope,
    parse_memory_uri,
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


# ---------------------------------------------------------------- 投影


class MemoryProjection:
    """事件日志上的记忆投影。所有写入都从 ``apply_event()`` 进入。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        log: EventLog,
        blob_dir: Path | None = None,
        clock: Callable[[], int] = _now,
        index: Any | None = None,
    ) -> None:
        if conn is not log.conn:
            raise MemoryStoreError(
                "记忆投影必须跑在事件日志的同一个 sqlite 连接上：投影更新与事件"
                "同事务提交是 F10 §7 的硬约束，第二个连接做不到原子"
            )
        if not callable(clock):
            raise MemoryStoreError("clock 必须是可调用对象")
        self._conn = conn
        self._log = log
        # 正文**不**放进事件 payload 的 blob 目录（`log.blob_dir`），哪怕那样
        # 只要一个目录更省事。两者的保留策略不同：
        #
        # - 事件 payload 是日志材料，必须留存供 replay / 审计，**不能**删；
        # - 记忆正文是投影产物，F10 §10 要给"物理擦除"留口子，将来会按策略删。
        #
        # 两个策略放同一个 content-addressed 目录还有一个具体误伤通道：如果某条
        # 记忆正文与某个事件 payload 恰好是同一段字节，它们就是**同一个文件**，
        # 将来按策略删记忆正文会把一条必须留存的日志材料一起删掉。
        #
        # 所以正文固定落在 DB 旁的 `memory_blobs/`，与事件 payload 目录无关。
        # 显式传 `blob_dir` 仍然优先（测试与将来的迁移入口需要）。
        if blob_dir is not None:
            self._blob_dir = Path(blob_dir)
        elif log.db_path.name == ":memory:":
            # 纯内存库没有"DB 旁边"可言：正文无处可落，如实退化成不可用，
            # 由 `content_state` 标注，而不是编一个当前目录下的路径。
            self._blob_dir = None
        else:
            self._blob_dir = log.db_path.parent / "memory_blobs"
        self._clock = clock
        # 向量索引是**可选的可重建派生数据**（`memory_index.MemoryIndex`），
        # 不传就是 FTS-only。这里用 duck typing 而不是 import：向量模块要 import
        # 本模块的错误类型与实体，反向 import 会成环；装配点（`open_memory`）
        # 负责把两者接起来并核对同一个连接。
        self._index = index
        # schema 建过一次就不再动。读路径（search / get / list / loadout）
        # 原本每次都走 `ensure_schema()`，而它开的是 `BEGIN IMMEDIATE`——
        # 一次纯读也要抢写锁，跟正在写的进程撞上就是 "database is locked"。
        self._schema_ready = False

    @property
    def log(self) -> Any:
        """这条投影认的事件日志（只读）。装配方用它核对"同一个事实源"。"""
        return self._log

    @property
    def index(self) -> Any | None:
        """装配的向量索引（没有就是 `None`）。只读，便于装配/审计核对能力边界。"""
        return self._index

    @property
    def conn(self) -> sqlite3.Connection:
        """投影与事务共用的连接（只读）。

        暴露出来是为了让 `Dispatcher` 能在边界上核对：传进来的投影是不是
        同一个连接。靠调用方"记得传对"是不够的。
        """
        return self._conn

    # ---- schema ----

    def ensure_schema(self) -> None:
        """建表 + 校验分词器版本。幂等；可在已有事务里调用（不隐式提交）。

        **不用 `executescript`**：它在 Python 里会先隐式 COMMIT 已打开的事务，
        那样 dispatcher 的"事件 + 投影同事务"会在这里被悄悄拆开。
        """
        if self._schema_ready:
            # 已就绪时只做一次**只读**版本核对：分词器版本写错/写旧要在
            # 第一次检索前报出来，但正常路径不该为了"再确认一次"去抢写锁。
            self._verify_tokenizer_version()
            return
        outer = self._log.in_transaction
        with self._log.transaction():
            self._ensure_schema()
            self._require_tokenizer_version()
        # 只在外层没有事务时缓存：若这次建表是在别人的事务里做的，那个事务
        # 回滚会把表一起回滚，缓存"已就绪"就会让后续调用在空库上直接失败。
        # 顶层调用（读路径）是自提交的，缓存安全。
        if not outer:
            self._schema_ready = True

    def _ensure_schema(self) -> None:
        for statement in _SCHEMA_STATEMENTS:
            self._conn.execute(statement)
        # v1 → v2：只加一个可空列（erased_at），因此**不需要**重建整份投影。
        # 更结构性的变化仍必须重建（投影可重建，事件是事实源）。
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "erased_at" not in columns:
            self._conn.execute("ALTER TABLE memories ADD COLUMN erased_at INTEGER")
        # v2 → v3：加 `layer` 列（默认 l1，老记忆都是原子事实）。只加列、不重建。
        if "layer" not in columns:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN layer TEXT NOT NULL DEFAULT 'l1'"
            )
        row = self._conn.execute(
            "SELECT value FROM memory_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None:
            try:
                stored = int(row[0])
            except (TypeError, ValueError):
                # 版本记录本身坏了：投影是可重建的，按"未版本化"处理并重写；
                # 正文的可用性不依赖这个键，分词器版本另有独立校验。
                stored = 0
            if stored > _SCHEMA_VERSION:
                raise MemoryStoreError(
                    f"投影 schema_version={stored} 高于本程序支持的 {_SCHEMA_VERSION}；"
                    "不要用旧版本程序打开新投影（会读错字段）"
                )
        self._conn.execute(
            "INSERT INTO memory_meta(key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(_SCHEMA_VERSION),),
        )
        if self._index is not None:
            self._index.ensure_schema()

    def _require_tokenizer_version(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM memory_meta WHERE key = 'tokenizer_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO memory_meta(key, value) VALUES ('tokenizer_version', ?)",
                (TOKENIZER_VERSION,),
            )
            return
        if row[0] != TOKENIZER_VERSION:
            raise MemoryStoreError(
                f"记忆索引的分词器版本是 {row[0]!r}，本程序是 {TOKENIZER_VERSION!r}；"
                "不同分词器产出的索引不可混用，请先调用 rebuild()"
            )

    def _verify_tokenizer_version(self) -> None:
        """只读核对分词器版本：不建表、不写 meta、不抢写锁。"""
        row = self._conn.execute(
            "SELECT value FROM memory_meta WHERE key = 'tokenizer_version'"
        ).fetchone()
        if row is None:
            # meta 被清掉/事务回滚过：退回完整建表路径，不能在空库上假装就绪。
            self._schema_ready = False
            self.ensure_schema()
            return
        if row[0] != TOKENIZER_VERSION:
            raise MemoryStoreError(
                f"记忆索引的分词器版本是 {row[0]!r}，本程序是 {TOKENIZER_VERSION!r}；"
                "不同分词器产出的索引不可混用，请先调用 rebuild()"
            )

    def _write_tokenizer_version(self) -> None:
        self._conn.execute(
            "INSERT INTO memory_meta(key, value) VALUES ('tokenizer_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (TOKENIZER_VERSION,),
        )

    # ---- 投影：唯一写入入口 ----

    def apply_event(self, event_type: str, payload: dict[str, Any], *, seq: int) -> None:
        """把一条事件应用到投影。**唯一**的投影写入入口，rebuild 也走这里。

        只认 ``tool.result`` 且 ``payload["tool_id"]`` 属于记忆写工具的事件；
        其余（含 memory.search / memory.get 的 result、其他工具、其他事件族）
        一律忽略，不报错。

        事件由 dispatcher 写；本方法**不写事件**。它开的事务会复用外层事务
        （`EventLog.transaction()` 可组合），所以调用方把 `append` 与这里包在
        同一个 `with log.transaction()` 里就是一次原子提交。

        ``seq`` 是这次写入的**来源 seq**。dispatcher 实时写入时传的是
        `tool.intent` 的 seq（不是 `tool.result` 的 seq）；`rebuild()` 用
        `dispatch.admitted.payload.intent_seq` 还原同一个值，保证重建后的
        `last_seq`、`event:<seq>` 来源与实时投影逐位一致。
        """
        if event_type != "tool.result":
            return
        if not isinstance(payload, dict):
            raise MemoryStoreError(f"tool.result 的 payload 必须是对象：{payload!r}")
        tool_id = payload.get("tool_id")
        if tool_id not in _WRITE_TOOL_IDS:
            return
        # dispatcher 在失败/被拒结果里也会写 tool.result，但 output 不是记忆
        # 形状；只有成功结果才可能改变投影（PRD §5.2.1 的接线口径）。
        if payload.get("status") != "succeeded":
            return
        output = payload.get("output")
        if not isinstance(output, dict):
            raise MemoryStoreError(f"{tool_id} 的 output 必须是对象：{output!r}")

        seq_value = _require_int(seq, "seq", minimum=1)
        # 写"新行"的四个工具共用同一套插入代码，差别只在期望的 status / origin /
        # supersedes 形状（见 `_apply_propose` 的 mode）。
        modes = {
            "memory.propose": "propose",
            "memory.remember": "remember",
            "memory.edit": "edit",
            "memory.promote": "promote",
        }
        mode = modes.get(tool_id)
        with self._log.transaction():
            self.ensure_schema()
            if tool_id == "memory.consolidate":
                self._apply_consolidate(output, seq_value)
            elif mode is not None:
                self._apply_propose(output, seq_value, mode=mode)
            elif tool_id == "memory.confirm":
                self._apply_confirm(output, seq_value)
            elif tool_id == "memory.erase":
                self._apply_erase(output, seq_value)
            else:
                self._apply_forget(output, seq_value)

    def rebuild(self) -> int:
        """清空投影并按事件 seq 重放，返回实际重放的记忆写事件条数。

        清空是重建的起点，不是另一条更新路径：每条事件的解释仍然只由
        ``apply_event()`` 负责。整个重放在一个事务里，失败则旧投影原样保留。

        **seq 的取法与 dispatcher 一致**：dispatcher 在实时写入时传的是这次
        调用的 `tool.intent` seq（`apply_event(..., seq=intent_seq)`），而不是
        `tool.result` 事件的 seq。重放若改传 result 的 seq，`last_seq` /
        `event:<seq>` 来源就会和实时投影不同，`projection_hash()` 断言立刻
        变红。所以这里先按 seq 顺序收集 `call_id → intent_seq`
        （`dispatch.admitted.payload.intent_seq` 是 PRD §5.3.3 里真实存在的
        指针；旧库里没有它时退回 `tool.intent` 的 seq），再逐条重放。
        没有 intent 记录时（只拿 tool.result 做夹具的库）才用 result 自己的
        seq——这是"日志里确实没有更早来源"的如实回退，不是猜一个号。
        """
        with self._log.transaction():
            self._ensure_schema()
            # 先写当前分词器版本：重放时 apply_event 的版本校验必须通过；
            # 若重放失败，整个事务回滚，旧版本记录也一起回来。
            self._write_tokenizer_version()
            events = self._log.read_types(["dispatch.admitted", "tool.intent", "tool.result"])
            intent_seqs: dict[tuple[str, str], int] = {}
            self._conn.execute("DELETE FROM memory_fts")
            self._conn.execute("DELETE FROM memories")
            # 向量与投影同源：重建一律先清向量 + 作废"已构建"标记。
            # **不管本次有没有装配索引**都要清——否则无索引的 rebuild 会留下
            # "行对了、向量残/缺"的半状态，之后带索引打开时静默部分召回。
            self._clear_vector_state()
            applied = 0
            for event in events:
                payload = event.payload
                call_id = payload.get("call_id")
                if event.type == "tool.intent":
                    if isinstance(call_id, str):
                        intent_seqs[(event.run_id, call_id)] = event.seq
                    continue
                if event.type == "dispatch.admitted":
                    intent_seq = payload.get("intent_seq")
                    if (
                        isinstance(call_id, str)
                        and isinstance(intent_seq, int)
                        and not isinstance(intent_seq, bool)
                        and intent_seq > 0
                    ):
                        intent_seqs[(event.run_id, call_id)] = intent_seq
                    continue
                if _write_tool_id(payload) is None:
                    continue
                seq_value = event.seq
                if isinstance(call_id, str):
                    seq_value = intent_seqs.get((event.run_id, call_id), event.seq)
                self.apply_event(event.type, payload, seq=seq_value)
                applied += 1
            if self._index is not None:
                # 重放完成后按当前 provider 全量重建向量（同时置 built 标记）：
                # 半套向量不会被 availability() 当成可用。
                self._index.rebuild(self)
        return applied

    def projection_hash(self) -> str:
        """投影的确定性摘要。实时写入与 rebuild 之后必须逐位相同。

        覆盖 memories 全部列（含 content_state / last_seq / fts_rowid）与
        FTS 的 token 内容——只比 memories 而不比索引，会漏掉"表对了、索引
        没刷新"这种半重建。
        """
        self.ensure_schema()
        memories: list[dict[str, Any]] = []
        rows = self._conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories ORDER BY memory_id"
        ).fetchall()
        for row in rows:
            memory = self._row_to_memory(row)
            record = memory.to_dict()
            record["content_state"] = row["content_state"]
            record["last_seq"] = row["last_seq"]
            record["fts_rowid"] = row["fts_rowid"]
            memories.append(record)
        fts: list[dict[str, Any]] = []
        for row in self._conn.execute(
            "SELECT rowid, memory_id, tokens FROM memory_fts ORDER BY memory_id"
        ).fetchall():
            fts.append(
                {
                    "memory_id": row["memory_id"],
                    "tokens": row["tokens"],
                    "rowid": int(row["rowid"]),
                }
            )
        return sha256_hex(
            canonical_bytes(
                {
                    "schema": _SCHEMA_VERSION,
                    "tokenizer": TOKENIZER_VERSION,
                    "memories": memories,
                    "fts": fts,
                }
            )
        )

    # ---- propose ----

    def _apply_propose(
        self, output: dict[str, Any], seq: int, *, mode: str = "propose"
    ) -> None:
        required = (
            "memory_id",
            "revision",
            "scope",
            "scope_id",
            "kind",
            "content_ref",
            "source_refs",
            "evidence",
            "proof_count",
            "origin",
            "status",
            "created_at",
            "deduped",
        )
        missing = [key for key in required if key not in output]
        if missing:
            raise MemoryStoreError(f"memory.propose 的 output 缺字段：{missing}")

        memory_id = _require_memory_id(output["memory_id"])
        revision = _require_int(output["revision"], "revision", minimum=1)
        scope = _scope_from_payload(output["scope"], output["scope_id"])
        kind = _require_closed(output["kind"], MEMORY_KINDS, "kind")
        content_ref = _require_content_ref(output["content_ref"])
        source_refs = _require_str_tuple(
            output["source_refs"], "source_refs", allow_empty=True
        )
        if not source_refs:
            # 工具 schema 里 source_refs 是可选的。dispatcher 传进来的 seq 是
            # 这次写入的**意图事件** seq（见 apply_event 的约定），它不可改、
            # 可回放，所以空来源不是"无来源"：记下意图事件，而不是留一个无法
            # 审计的空列表。这不是未知枚举的兜底——值来自 apply_event 的 seq，
            # rebuild() 会还原同一个号。
            source_refs = (f"event:{seq}",)
        evidence = _require_json_list(output["evidence"], "evidence")
        proof_count = _require_int(output["proof_count"], "proof_count", minimum=0)
        if proof_count != len(evidence):
            # F10 §14：proof_count 与证据条数必须机械一致，不靠人眼。
            raise MemoryStoreError(
                f"proof_count={proof_count} 与 evidence 条数 {len(evidence)} 不一致"
            )
        origin = _require_closed(output["origin"], MEMORY_ORIGINS, "origin")
        status = _require_closed(output["status"], MEMORY_STATUSES, "status")
        # `layer` 可选（默认 l1）：老事件/老测试夹具没有这个键也必须能重放。
        layer = _require_closed(output.get("layer", "l1"), MEMORY_LAYERS, "layer")
        created_at = _require_int(output["created_at"], "created_at", minimum=0)
        expires_at = _require_optional_int(output.get("expires_at"), "expires_at")
        deduped = _require_bool(output["deduped"], "deduped")
        supersedes_raw = output.get("supersedes")
        supersedes = None if supersedes_raw is None else _require_memory_id(supersedes_raw)

        if deduped:
            # 去重事件指向已有记忆：投影不该新增行，也不该重复进索引。
            # 但 revision 可能是已有记忆的当前版本，所以这里不按 propose 的
            # "新记忆 revision=1" 约束。
            row = self._load_row(memory_id)
            if row is None:
                raise MemoryStoreError(
                    f"deduped=true 的 propose 指向不存在的记忆：{memory_id!r}"
                )
            existing = self._row_to_memory(row)
            if existing.status not in (_STATUS_CANDIDATE, _STATUS_ACTIVE):
                raise MemoryStoreError(
                    f"deduped=true 只能指向 candidate/active 记忆：{memory_id!r}"
                    f" 当前是 {existing.status!r}"
                )
            if (
                (existing.scope, existing.scope_id) != _scope_key(scope)
                or existing.kind != kind
                or existing.content_ref != content_ref
            ):
                raise MemoryStoreError(
                    f"deduped=true 的 propose 与已有记忆 {memory_id!r} 的"
                    "作用域/种类/正文不一致"
                )
            return

        if revision != 1:
            raise MemoryStoreError(f"写新行的 revision 必须是 1：{revision}")
        if mode == "remember":
            # 控制面 `memory.remember`：人直接记住 → active + user_explicit。
            # 这条路径**只有**控制面能走到（执行器按 Task.is_control 核验）。
            if status != _STATUS_ACTIVE:
                raise MemoryStoreError(f"memory.remember 只能写 active，收到 {status!r}")
            if origin != "user_explicit":
                raise MemoryStoreError(
                    f"memory.remember 的 origin 必须是 user_explicit，收到 {origin!r}"
                )
            if supersedes is not None:
                raise MemoryStoreError("memory.remember 不 supersede 旧记忆")
        elif mode == "edit":
            # 编辑 = 旧版退役 + 新版立即生效（一次审批）。新版 origin 由审批
            # 出处决定：人批准过的编辑 → user_confirmed。
            if status != _STATUS_ACTIVE:
                raise MemoryStoreError(f"memory.edit 只能写 active，收到 {status!r}")
            # origin 描述"这一版正文"的来源：人自己写 → user_explicit；
            # 模型写、人批准 → user_confirmed。旧版的 origin 留在旧行。
            if origin not in ("user_explicit", "user_confirmed"):
                raise MemoryStoreError(
                    f"memory.edit 的 origin 只能是 user_explicit / user_confirmed，"
                    f"收到 {origin!r}"
                )
            if supersedes is None:
                raise MemoryStoreError("memory.edit 必须带 supersedes（旧版 id）")
        elif mode == "promote":
            # 提升 = 原作用域里退役 + 目标作用域里新建**候选**（人再确认才召回）。
            if status != _STATUS_CANDIDATE:
                raise MemoryStoreError(
                    f"memory.promote 只能写 candidate，收到 {status!r}"
                )
            if supersedes is None:
                raise MemoryStoreError("memory.promote 必须带 supersedes（原记忆 id）")
            if supersedes not in source_refs:
                raise MemoryStoreError(
                    f"memory.promote 的 source_refs 必须包含原记忆：{supersedes!r}"
                )
            old_row = self._load_row(supersedes)
            if old_row is None:
                raise MemoryStoreError(
                    f"memory.promote 的 supersedes 指向不存在的记忆：{supersedes!r}"
                )
            if origin != old_row["origin"]:
                raise MemoryStoreError(
                    f"memory.promote 的 origin 必须随原行（{old_row['origin']!r}），"
                    f"收到 {origin!r}；搬同一段正文不能降档"
                )
        else:
            # propose 只生成惰性候选；激活必须走 memory.confirm（F10 §7）。
            if status != _STATUS_CANDIDATE:
                raise MemoryStoreError(
                    f"memory.propose 只能写 candidate，收到 {status!r}"
                )
            if supersedes is not None:
                raise MemoryStoreError("memory.propose 不 supersede 旧记忆")
        if supersedes is not None and supersedes == memory_id:
            raise MemoryStoreError("记忆不能 supersede 自己")

        if self._load_row(memory_id) is not None:
            raise MemoryStoreError(f"记忆已存在，propose 不能覆盖：{memory_id!r}")

        duplicate = self._conn.execute(
            "SELECT memory_id FROM memories"
            " WHERE scope = ? AND scope_id = ? AND kind = ? AND content_ref = ?"
            "   AND status IN ('candidate', 'active') LIMIT 1",
            (scope.scope, scope.scope_id, kind, content_ref),
        ).fetchone()
        if duplicate is not None:
            raise MemoryStoreError(
                "重复记忆必须在进索引前由 find_duplicate() 去重："
                f"已有 {duplicate['memory_id']!r}，又收到 {memory_id!r}"
            )

        if supersedes is not None:
            self._supersede(supersedes, seq)

        content = self._read_blob(content_ref)
        tokens = ""
        content_state = _CONTENT_UNAVAILABLE
        if content is not None:
            safe, blocked = self.redact_or_block(content)
            if blocked:
                raise MemoryWriteBlocked(
                    f"记忆 {memory_id!r} 的正文命中密钥闸门：候选不得入库"
                )
            if safe != content:
                raise MemoryWriteBlocked(
                    f"记忆 {memory_id!r} 的正文未过写入闸门（含 PII）："
                    "必须先经 redact_or_block() 再 put_content()"
                )
            tokens = " ".join(_tokenize(content))
            content_state = _CONTENT_AVAILABLE

        fts_rowid = self._alloc_fts_rowid() if tokens else None
        try:
            self._conn.execute(
                f"INSERT INTO memories ({_MEMORY_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    revision,
                    scope.scope,
                    scope.scope_id,
                    kind,
                    content_ref,
                    json.dumps(list(source_refs), ensure_ascii=False),
                    json.dumps(list(evidence), ensure_ascii=False, sort_keys=True),
                    proof_count,
                    origin,
                    status,
                    None,
                    created_at,
                    expires_at,
                    supersedes,
                    layer,
                    content_state,
                    seq,
                    fts_rowid,
                    None,  # erased_at：新写入的行从未擦除
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MemoryStoreError(
                f"记忆 {memory_id!r} 与已有候选/活动记忆重复（唯一索引拒绝）"
            ) from exc
        if fts_rowid is not None:
            self._insert_fts(fts_rowid, memory_id, tokens)
        # candidate 不参与召回，钩子自己会跳过；active（remember / edit）立即建向量。
        self._index_upsert(memory_id)

    def _supersede(self, target_id: str, seq: int) -> None:
        """把被 supersede 的旧记忆移出召回，保留行（旧版可查）。"""
        row = self._load_row(target_id)
        if row is None:
            raise MemoryStoreError(f"supersedes 指向不存在的记忆：{target_id!r}")
        if seq <= int(row["last_seq"]):
            raise MemoryStoreError(
                f"supersede 的事件 seq={seq} 早于/等于目标 {target_id!r} 的"
                f" last_seq={row['last_seq']}；重建顺序会解释不了"
            )
        if row["status"] not in (_STATUS_CANDIDATE, _STATUS_ACTIVE):
            raise MemoryStoreError(
                f"只能 supersede candidate/active 记忆：{target_id!r} 当前是"
                f" {row['status']!r}"
            )
        self._drop_fts(row["fts_rowid"])
        self._conn.execute(
            "UPDATE memories SET status = ?, fts_rowid = NULL, last_seq = ?"
            " WHERE memory_id = ?",
            (_STATUS_SUPERSEDED, seq, target_id),
        )
        # 退役的行不再召回：向量也必须一起下掉，否则语义路还会把它捞回来。
        self._index_drop(target_id)

    def _stale_map(
        self, candidates: dict[str, tuple[Memory, str]]
    ) -> dict[str, bool]:
        """合成层新鲜度：作用域里最大 L1 `last_seq` > 页面 `basis_seq` ⇒ 陈旧。

        `basis_seq` 从 `source_refs` 的 `seq:<n>` 读。**只对能判定的页放键**：
        旧行没有这个标记就不放，调用方（`Recalled.stale`）会拿到 `None`
        = "不可判定"，而不是被压成 `False`（那等于谎报新鲜）。

        "更新的证据"只算**真的还能被召回**的 L1：`active`、未擦除、未过期
        （`expires_at` 已过的行不会被 `consolidatable_sources` 收进合成，也不会
        被检索返回，用它把页判陈旧就是误报）。
        """
        from .memory_distill import parse_basis_seq

        now = _require_int(self._clock(), "clock() 返回值")
        maxima: dict[tuple[str, str], int] = {}

        def scope_max(scope: str, scope_id: str) -> int:
            key = (scope, scope_id)
            if key not in maxima:
                row = self._conn.execute(
                    "SELECT MAX(last_seq) FROM memories"
                    " WHERE scope = ? AND scope_id = ? AND layer = 'l1'"
                    "   AND status = 'active' AND erased_at IS NULL"
                    "   AND (expires_at IS NULL OR expires_at > ?)",
                    (scope, scope_id, now),
                ).fetchone()
                maxima[key] = int(row[0]) if row is not None and row[0] is not None else 0
            return maxima[key]

        stale: dict[str, bool] = {}
        for memory_id, (memory, _content) in candidates.items():
            if memory.layer == "l1":
                continue
            basis = parse_basis_seq(memory.source_refs)
            if basis is None:
                continue
            stale[memory_id] = scope_max(memory.scope, memory.scope_id) > basis
        return stale

    # ---- consolidate（L2/L3 确定性合成，N3） ----

    def consolidatable_sources(
        self, scope: MemoryScope
    ) -> list[Any]:
        """该作用域里可参与合成的 L1：active、未擦除、正文在盘上。

        只取 `layer='l1'`：合成层的输入永远是原子事实，不是上一版合成页（否则
        会自我引用、越合越漂）。读不到正文的行跳过（诚实：不可用的证据不进页）。
        """
        from .memory_distill import ConsolidationSource

        self.ensure_schema()
        now = _require_int(self._clock(), "clock() 返回值")
        rows = self._conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories"
            " WHERE scope = ? AND scope_id = ? AND status = ? AND layer = 'l1'"
            " AND erased_at IS NULL ORDER BY memory_id",
            (scope.scope, scope.scope_id, _STATUS_ACTIVE),
        ).fetchall()
        sources: list[ConsolidationSource] = []
        for row in rows:
            memory = self._row_to_memory(row)
            if memory.expires_at is not None and memory.expires_at <= now:
                continue
            content = self._read_blob(memory.content_ref)
            if content is None:
                continue
            sources.append(
                ConsolidationSource(
                    memory_id=memory.memory_id,
                    revision=memory.revision,
                    content=content,
                    last_seq=int(row["last_seq"]),
                    kind=memory.kind,
                    origin=memory.origin,
                    layer=memory.layer,
                )
            )
        return sources

    def _apply_consolidate(self, output: dict[str, Any], seq: int) -> None:
        """把一次合成结果写进投影：每页一行候选 + 旧页 superseded。

        可回放的唯一依据是**事件里的 output**（正文引用/证据/时间/来源），
        不重新跑一遍合成算法——重算会让"事件说 A、投影写 B"变成可能，
        而重建必须逐位复现实时写入。
        """
        for key in ("scope", "scope_id", "pages"):
            if key not in output:
                raise MemoryStoreError(f"memory.consolidate 的 output 缺字段：{key}")
        scope = _scope_from_payload(output["scope"], output["scope_id"])
        pages = output["pages"]
        if not isinstance(pages, list):
            raise MemoryStoreError("memory.consolidate 的 pages 必须是数组")
        for raw in pages:
            if not isinstance(raw, dict):
                raise MemoryStoreError("pages 的每一项必须是对象")
            self._apply_consolidate_page(scope, raw, seq)

    def _apply_consolidate_page(
        self, scope: MemoryScope, page: dict[str, Any], seq: int
    ) -> None:
        required = (
            "memory_id",
            "layer",
            "kind",
            "content_ref",
            "evidence",
            "proof_count",
            "source_refs",
            "consolidated_at",
        )
        missing = [key for key in required if key not in page]
        if missing:
            raise MemoryStoreError(f"合成页缺字段：{missing}")
        memory_id = _require_memory_id(page["memory_id"])
        layer = _require_closed(page["layer"], MEMORY_LAYERS, "layer")
        if layer == "l1":
            raise MemoryStoreError("合成页的 layer 不能是 l1（合成只产出 l2/l3）")
        kind = _require_closed(page["kind"], MEMORY_KINDS, "kind")
        content_ref = _require_content_ref(page["content_ref"])
        evidence = _require_json_list(page["evidence"], "evidence")
        proof_count = _require_int(page["proof_count"], "proof_count", minimum=1)
        if proof_count != len(evidence):
            raise MemoryStoreError(
                f"合成页 {memory_id!r} 的 proof_count={proof_count} 与证据条数 "
                f"{len(evidence)} 不一致"
            )
        source_refs = _require_str_tuple(page["source_refs"], "source_refs")
        if not source_refs:
            raise MemoryStoreError(f"合成页 {memory_id!r} 没有来源")
        consolidated_at = _require_int(
            page["consolidated_at"], "consolidated_at", minimum=0
        )

        existing = self._load_row(memory_id)
        if existing is not None:
            # 同一作用域 + 同一层 + 同一正文 ⇒ 同一个 id：重复合成是幂等的。
            if existing["layer"] != layer or existing["content_ref"] != content_ref:
                raise MemoryStoreError(
                    f"合成页 id 冲突：{memory_id!r} 已存在且内容不同"
                )
            if existing["status"] not in (_STATUS_CANDIDATE, _STATUS_ACTIVE):
                # 页 id 由内容决定，所以"正文回退到旧版"会撞回一个已经退役的 id。
                # 静默 `return` 会让事件说"写了候选页"、投影却停在 superseded
                # （人 confirm 会直接失败）；悄悄复活又会改写已确认的结论。两条
                # 都不诚实，所以 fail-closed，由人决定怎么处理（复审复现过）。
                raise MemoryStoreError(
                    f"合成页 {memory_id!r} 已存在且状态是 {existing['status']!r}："
                    f"同一段正文不能再作为新的候选页写入"
                )
            return

        # 旧页退役：同作用域、同层的 **candidate** 页被这一页取代（保留行，旧内容
        # 与它当时站的证据可查）。**已 `confirm` 的 active 页不在这里退役**：
        # `memory.consolidate` 是模型可见、免审批的工具，若它能顺手把"人确认过的
        # 页面"置为 superseded，等于模型可以撤销人的召回授权（复审指出的设计边界）。
        # 人确认的页只有人能退（`memory.forget` / `memory.erase`）；新页与它并存，
        # 由 freshness（`stale`）决定谁排在前面。
        old_rows = self._conn.execute(
            "SELECT memory_id FROM memories"
            " WHERE scope = ? AND scope_id = ? AND layer = ?"
            "   AND status = 'candidate' ORDER BY memory_id",
            (scope.scope, scope.scope_id, layer),
        ).fetchall()
        for row in old_rows:
            self._supersede(str(row["memory_id"]), seq)

        content = self._read_blob(content_ref)
        tokens = ""
        content_state = _CONTENT_UNAVAILABLE
        if content is not None:
            safe, blocked = self.redact_or_block(content)
            if blocked:
                raise MemoryWriteBlocked(
                    f"合成页 {memory_id!r} 的正文命中密钥闸门：候选不得入库"
                )
            if safe != content:
                raise MemoryWriteBlocked(
                    f"合成页 {memory_id!r} 的正文未过写入闸门（含 PII）"
                )
            tokens = " ".join(_tokenize(content))
            content_state = _CONTENT_AVAILABLE
        fts_rowid = self._alloc_fts_rowid() if tokens else None
        try:
            self._conn.execute(
                f"INSERT INTO memories ({_MEMORY_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    1,
                    scope.scope,
                    scope.scope_id,
                    kind,
                    content_ref,
                    json.dumps(list(source_refs), ensure_ascii=False),
                    json.dumps(list(evidence), ensure_ascii=False, sort_keys=True),
                    proof_count,
                    "inferred",
                    _STATUS_CANDIDATE,
                    consolidated_at,
                    consolidated_at,
                    None,
                    None,
                    layer,
                    content_state,
                    seq,
                    fts_rowid,
                    None,
                ),
            )
        except sqlite3.IntegrityError as exc:  # pragma: no cover - id 已先查过
            raise MemoryStoreError(f"合成页写入被唯一索引拒绝：{memory_id!r}") from exc
        if fts_rowid is not None:
            self._insert_fts(fts_rowid, memory_id, tokens)
        self._index_upsert(memory_id)

    # ---- confirm / forget ----

    def _apply_confirm(self, output: dict[str, Any], seq: int) -> None:
        memory_id, row, revision, consolidated_at = self._parse_transition(
            output, expected_status=_STATUS_ACTIVE
        )
        if row["status"] != _STATUS_CANDIDATE:
            raise MemoryStoreError(
                f"只能确认 candidate 记忆：{memory_id!r} 当前是 {row['status']!r}"
            )
        if seq <= int(row["last_seq"]):
            raise MemoryStoreError(
                f"confirm 的事件 seq={seq} 早于/等于记忆 {memory_id!r} 的"
                f" last_seq={row['last_seq']}；重建顺序会解释不了"
            )
        # 候选在 propose 时正文可能不可用；确认时再试一次（只改投影，
        # 不写事件、不改 content_ref），正文恢复后记忆就能被召回。
        if row["content_state"] == _CONTENT_UNAVAILABLE:
            content = self._read_blob(row["content_ref"])
            if content is not None:
                # 补写索引也要过闸门：blob 可能是在 propose 之后被人工放进
                # 内容寻址目录的，不能因为"之前没读到"就跳过扫描。
                safe, blocked = self.redact_or_block(content)
                if blocked or safe != content:
                    raise MemoryWriteBlocked(
                        f"记忆 {memory_id!r} 恢复的正文未过写入闸门：拒绝索引"
                    )
                tokens = " ".join(_tokenize(content))
                fts_rowid = self._alloc_fts_rowid() if tokens else None
                if fts_rowid is not None:
                    self._insert_fts(fts_rowid, memory_id, tokens)
                self._conn.execute(
                    "UPDATE memories SET content_state = ?, fts_rowid = ?"
                    " WHERE memory_id = ?",
                    (_CONTENT_AVAILABLE, fts_rowid, memory_id),
                )
        # 契约 v1.2：人批准 confirm 时把 `inferred` **升级**为 `user_confirmed`。
        # 只升级、不降级；output 里给别的值一律拒绝（模型/执行器都不能发明来源）。
        upgraded_origin: str | None = output.get("origin")
        if upgraded_origin is not None:
            upgraded_origin = _require_closed(
                upgraded_origin, MEMORY_ORIGINS, "origin"
            )
            if upgraded_origin != "user_confirmed":
                raise MemoryStoreError(
                    f"confirm 的 origin 只能是 user_confirmed，收到 {upgraded_origin!r}"
                )
            if row["origin"] != "inferred":
                raise MemoryStoreError(
                    f"记忆 {memory_id!r} 的 origin={row['origin']!r} 不能升为"
                    " user_confirmed（只允许 inferred → user_confirmed）"
                )
        else:
            upgraded_origin = row["origin"]

        self._conn.execute(
            "UPDATE memories SET revision = ?, status = ?, consolidated_at = ?,"
            " origin = ?, last_seq = ? WHERE memory_id = ?",
            (
                revision,
                _STATUS_ACTIVE,
                consolidated_at,
                upgraded_origin,
                seq,
                memory_id,
            ),
        )
        # 确认后进入召回：这时才建向量（候选阶段不需要）。
        self._index_upsert(memory_id)

    def _apply_forget(self, output: dict[str, Any], seq: int) -> None:
        memory_id, row, revision, _ = self._parse_transition(
            output, expected_status=_STATUS_FORGOTTEN
        )
        if row["status"] not in (_STATUS_CANDIDATE, _STATUS_ACTIVE):
            raise MemoryStoreError(
                f"只能遗忘 candidate/active 记忆：{memory_id!r} 当前是"
                f" {row['status']!r}"
            )
        if seq <= int(row["last_seq"]):
            raise MemoryStoreError(
                f"forget 的事件 seq={seq} 早于/等于记忆 {memory_id!r} 的"
                f" last_seq={row['last_seq']}；重建顺序会解释不了"
            )
        self._drop_fts(row["fts_rowid"])
        self._conn.execute(
            "UPDATE memories SET revision = ?, status = ?, fts_rowid = NULL,"
            " last_seq = ? WHERE memory_id = ?",
            (revision, _STATUS_FORGOTTEN, seq, memory_id),
        )
        self._index_drop(memory_id)

    def _apply_erase(self, output: dict[str, Any], seq: int) -> None:
        """物理擦除：投影行置 forgotten + erased_at，正文标记不可用。

        事件本身只记"擦过"；文件删除由执行器在提交前完成。重建时看到
        `erased_at` 就**不再读 blob**——哪怕崩溃窗口里文件还留着。
        """
        for key in (
            "memory_id",
            "revision",
            "before_status",
            "status",
            "memory_uri",
            "erased_at",
        ):
            if key not in output:
                raise MemoryStoreError(f"memory.erase 的 output 缺字段：{key}")
        memory_id = _require_memory_id(output["memory_id"])
        revision = _require_int(output["revision"], "revision", minimum=2)
        before_status = _require_closed(
            output["before_status"], MEMORY_STATUSES, "before_status"
        )
        status = _require_closed(output["status"], MEMORY_STATUSES, "status")
        if status != _STATUS_FORGOTTEN:
            raise MemoryStoreError(
                f"memory.erase 的结果必须是 forgotten，收到 {status!r}"
            )
        erased_at = _require_int(output["erased_at"], "erased_at", minimum=0)
        memory_uri = _require_str(output["memory_uri"], "memory_uri")
        try:
            uri_scope = parse_memory_uri(memory_uri)
        except TianshuError as exc:
            raise MemoryStoreError(f"memory_uri 不合法：{memory_uri!r}（{exc}）") from exc

        row = self._load_row(memory_id)
        if row is None:
            raise MemoryStoreError(f"记忆不存在，无法擦除：{memory_id!r}")
        if (row["scope"], row["scope_id"]) != _scope_key(uri_scope):
            raise MemoryStoreError(
                f"memory_uri 与记忆 {memory_id!r} 的作用域不一致：{memory_uri!r}"
            )
        if row["erased_at"] is not None:
            raise MemoryStoreError(f"记忆 {memory_id!r} 已经擦除过，不能重复擦除")
        if revision != int(row["revision"]) + 1:
            raise MemoryStoreError(
                f"记忆 {memory_id!r} 的 revision 必须从 {row['revision']} 推进到"
                f" {int(row['revision']) + 1}，收到 {revision}"
            )
        if before_status != row["status"]:
            raise MemoryStoreError(
                f"记忆 {memory_id!r} 的 before_status={before_status!r} 与投影当前"
                f"状态 {row['status']!r} 不一致"
            )
        if seq <= int(row["last_seq"]):
            raise MemoryStoreError(
                f"erase 的事件 seq={seq} 早于/等于记忆 {memory_id!r} 的"
                f" last_seq={row['last_seq']}；重建顺序会解释不了"
            )
        self._drop_fts(row["fts_rowid"])
        self._conn.execute(
            "UPDATE memories SET revision = ?, status = ?, content_state = ?,"
            " erased_at = ?, fts_rowid = NULL, last_seq = ? WHERE memory_id = ?",
            (
                revision,
                _STATUS_FORGOTTEN,
                _CONTENT_UNAVAILABLE,
                erased_at,
                seq,
                memory_id,
            ),
        )
        # 已擦除：向量一并下掉，语义路也不能再把正文捞回来。
        self._index_drop(memory_id)

    def _parse_transition(
        self, output: dict[str, Any], *, expected_status: str
    ) -> tuple[str, sqlite3.Row, int, int | None]:
        for key in ("memory_id", "revision", "before_status", "status", "memory_uri"):
            if key not in output:
                raise MemoryStoreError(f"记忆状态迁移的 output 缺字段：{key}")
        memory_id = _require_memory_id(output["memory_id"])
        revision = _require_int(output["revision"], "revision", minimum=2)
        before_status = _require_closed(output["before_status"], MEMORY_STATUSES, "before_status")
        status = _require_closed(output["status"], MEMORY_STATUSES, "status")
        if status != expected_status:
            raise MemoryStoreError(f"状态迁移结果必须是 {expected_status!r}：{status!r}")
        memory_uri = _require_str(output["memory_uri"], "memory_uri")
        try:
            uri_scope = parse_memory_uri(memory_uri)
        except TianshuError as exc:
            raise MemoryStoreError(f"memory_uri 不合法：{memory_uri!r}（{exc}）") from exc

        row = self._load_row(memory_id)
        if row is None:
            raise MemoryStoreError(f"记忆不存在，无法迁移状态：{memory_id!r}")
        if (row["scope"], row["scope_id"]) != _scope_key(uri_scope):
            raise MemoryStoreError(
                f"memory_uri 与记忆 {memory_id!r} 的作用域不一致：{memory_uri!r}"
            )
        if revision != int(row["revision"]) + 1:
            raise MemoryStoreError(
                f"记忆 {memory_id!r} 的 revision 必须从 {row['revision']} 推进到"
                f" {int(row['revision']) + 1}，收到 {revision}"
            )
        if before_status != row["status"]:
            raise MemoryStoreError(
                f"记忆 {memory_id!r} 的 before_status={before_status!r} 与投影当前"
                f"状态 {row['status']!r} 不一致"
            )
        consolidated_at: int | None = None
        if expected_status == _STATUS_ACTIVE:
            if "consolidated_at" not in output:
                raise MemoryStoreError("memory.confirm 的 output 缺 consolidated_at")
            consolidated_at = _require_int(output["consolidated_at"], "consolidated_at", minimum=0)
        return memory_id, row, revision, consolidated_at

    # ---- 内部读写 ----

    def _load_row(self, memory_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()

    def _alloc_fts_rowid(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(fts_rowid), 0) FROM memories"
        ).fetchone()
        return int(row[0]) + 1

    def _insert_fts(self, rowid: int, memory_id: str, tokens: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_fts(rowid, memory_id, tokens) VALUES (?, ?, ?)",
            (rowid, memory_id, tokens),
        )

    def _drop_fts(self, fts_rowid: object) -> None:
        if fts_rowid is None:
            return
        self._conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (int(fts_rowid),))

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        try:
            scope = MemoryScope(row["scope"], row["scope_id"])
        except TianshuError as exc:
            raise MemoryStoreError(
                f"投影里的记忆 {row['memory_id']!r} 作用域非法：{exc}"
            ) from exc
        kind = _require_closed(row["kind"], MEMORY_KINDS, "kind")
        origin = _require_closed(row["origin"], MEMORY_ORIGINS, "origin")
        status = _require_closed(row["status"], MEMORY_STATUSES, "status")
        content_state = _require_closed(row["content_state"], _CONTENT_STATES, "content_state")
        try:
            raw_refs = json.loads(row["source_refs"])
            raw_evidence = json.loads(row["evidence"])
        except (TypeError, ValueError) as exc:
            raise MemoryStoreError(
                f"投影里的记忆 {row['memory_id']!r} 的 JSON 列损坏：{exc}"
            ) from exc
        source_refs = _require_str_tuple(raw_refs, "source_refs")
        evidence = _require_json_list(raw_evidence, "evidence")
        proof_count = _require_int(row["proof_count"], "proof_count", minimum=0)
        if proof_count != len(evidence):
            raise MemoryStoreError(
                f"投影里的记忆 {row['memory_id']!r} proof_count 与证据条数不一致"
            )
        revision = _require_int(row["revision"], "revision", minimum=1)
        created_at = _require_int(row["created_at"], "created_at", minimum=0)
        consolidated_at = _require_optional_int(row["consolidated_at"], "consolidated_at")
        expires_at = _require_optional_int(row["expires_at"], "expires_at")
        if content_state == _CONTENT_UNAVAILABLE and row["fts_rowid"] is not None:
            # 索引里有正文 token、投影却说正文不可用：半重建的典型形状。
            raise MemoryStoreError(
                f"投影里的记忆 {row['memory_id']!r} 正文不可用却仍在索引中"
            )
        return Memory(
            memory_id=row["memory_id"],
            revision=revision,
            scope=scope.scope,
            scope_id=scope.scope_id,
            kind=kind,
            content_ref=row["content_ref"],
            source_refs=source_refs,
            evidence=evidence,
            proof_count=proof_count,
            origin=origin,
            status=status,
            consolidated_at=consolidated_at,
            created_at=created_at,
            expires_at=expires_at,
            supersedes=row["supersedes"],
            layer=_require_closed(row["layer"], MEMORY_LAYERS, "layer"),
            erased_at=_require_optional_int(row["erased_at"], "erased_at"),
        )

    # ---- 正文 ----

    @property
    def blob_dir(self) -> Path | None:
        """正文内容寻址目录；纯内存库没有"DB 旁边"，此时为 None。"""
        return self._blob_dir

    def redact_or_block(self, content: str) -> tuple[str, bool]:
        """写入闸门。返回 ``(安全正文, 是否整条阻断)``。

        阻断时返回的正文是占位符，调用方不得再把它当原内容使用；更重要的是
        **不得入库**（``put_content`` 会直接抛错）。
        """
        text = _require_str(content, "content", allow_empty=True)
        for _kind, pattern in _BLOCK_PATTERNS:
            if pattern.search(text):
                return ("[REDACTED:blocked]", True)
        for match in _BASE64_PATTERN.finditer(text):
            if _looks_like_base64_secret(match.group(0)):
                return ("[REDACTED:blocked]", True)
        return (_redact(text), False)

    def put_content(self, content: str) -> str:
        """把正文写入内容寻址目录，返回 sha256 引用。

        这是**写入闸门**：命中密钥直接抛 `MemoryWriteBlocked`，blob 一个字节
        都不落盘；PII 先红act 再落盘，返回的 ref 对应红act 后的字节。
        """
        safe, blocked = self.redact_or_block(content)
        if blocked:
            raise MemoryWriteBlocked("正文命中密钥/密钥形状闸门：整条阻断，不入库")
        data = safe.encode("utf-8")
        ref = sha256_hex(data)
        if self._blob_dir is None:
            # 纯内存库没有"DB 旁边"可落。不编一个当前目录下的路径假装能存：
            # 引用照样返回，`apply_event()` 读不到正文时会标成
            # `content_unavailable`，`read_content()` 如实返回 None。
            return ref
        target = self._blob_dir / ref
        if not target.is_file():
            atomic_write_bytes(target, data)
        return ref

    def _read_blob(self, content_ref: str) -> str | None:
        """按引用读回正文。文件缺失 → None；内容与引用不符 → 抛错。

        **绝不回落到"最新记忆"**：引用指向哪一版就读哪一版，读不到就如实说
        不可用（F10 §10）。
        """
        # 引用形状先校验：`content_ref` 会被拼进文件路径，任何 `..` / 分隔符
        # 都等于把"读记忆正文"变成"读任意文件"。sha256 形状是唯一合法输入。
        reference = _require_content_ref(content_ref)
        if self._blob_dir is None:
            return None
        path = self._blob_dir / reference
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        actual = sha256_hex(data)
        if actual != reference:
            raise MemoryStoreError(
                f"正文 blob 与 content_ref 不符：{reference[:12]}… ≠ {actual[:12]}…"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryStoreError(f"正文 blob 不是 UTF-8：{content_ref[:12]}…") from exc

    def blob_path(self, content_ref: str) -> Path | None:
        """内容寻址文件路径；纯内存库没有路径（返回 None）。"""
        reference = _require_content_ref(content_ref)
        if self._blob_dir is None:
            return None
        return self._blob_dir / reference

    def content_ref_of(self, memory_id: str) -> tuple[str, int | None] | None:
        """按 id 取 `(content_ref, erased_at)`，**不做作用域过滤**。

        这是给恢复/审计用的内部读口：`memory.erase` 的副作用（删正文文件）在
        提交事务**之外**且不可逆，恢复必须能按 memory_id 找到它当时指向的
        blob 才能给出可核对结论。作用域过滤在这里没有意义——调用方是恢复
        流程，不是模型；模型侧仍只有 `get()`（带 `allowed`）。
        """
        self.ensure_schema()
        row = self._conn.execute(
            "SELECT content_ref, erased_at FROM memories WHERE memory_id = ?",
            (_require_memory_id(memory_id),),
        ).fetchone()
        if row is None:
            return None
        return (str(row["content_ref"]), row["erased_at"])

    def count_content_ref(
        self, content_ref: str, *, exclude_memory_id: str
    ) -> tuple[int, list[str]]:
        """还有多少**其它未擦除的投影行**（含 forgotten / superseded）引用这个 blob。

        已擦除的行不算：它自己都声明副本删了，不能拦住别人的擦除。

        擦除只能删没有任何其它行引用的正文；否则"擦一条"会让另一条正文凭空消失。
        返回 `(条数, memory_id 列表)`——列表进回执，供人核对"为什么没删"。
        """
        reference = _require_content_ref(content_ref)
        # 已擦除的行不再"占有"正文：它自己都声明副本删了，不能拦住别人的擦除。
        rows = self._conn.execute(
            "SELECT memory_id FROM memories WHERE content_ref = ? AND memory_id != ?"
            " AND erased_at IS NULL ORDER BY memory_id",
            (reference, exclude_memory_id),
        ).fetchall()
        return len(rows), [str(row["memory_id"]) for row in rows]

    def read_content(self, memory: Memory) -> str | None:
        """读回这一版正文。blob 缺失或内容哈希不符时**不**回落到其他版本。"""
        if not isinstance(memory, Memory):
            raise MemoryStoreError(f"read_content 需要 Memory：{type(memory).__name__}")
        if memory.erased_at is not None:
            # 已擦除：哪怕文件因为崩溃窗口还留着，也**不**再提供正文。
            return None
        return self._read_blob(memory.content_ref)

    # ---- 向量索引钩子（没有索引时全部 no-op） ----

    def _vector_tables(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('memory_vectors', 'memory_vector_meta')"
        ).fetchall()
        return {str(row["name"]) for row in rows}

    def _vector_tables_exist(self) -> bool:
        return len(self._vector_tables()) == 2

    def _clear_vector_state(self) -> None:
        """清空向量并作废 built 标记（哪张表在清哪张，半迁移的库也不留残片）。"""
        tables = self._vector_tables()
        if "memory_vectors" in tables:
            self._conn.execute("DELETE FROM memory_vectors")
        if "memory_vector_meta" in tables:
            self._conn.execute("DELETE FROM memory_vector_meta")

    def _index_upsert(self, memory_id: str) -> None:
        """让索引跟随投影：只有**可召回**（active、未擦除、正文在）的行才写向量。"""
        if self._index is None:
            return
        row = self._load_row(memory_id)
        if row is None:
            self._index.drop(memory_id)
            return
        memory = self._row_to_memory(row)
        if memory.status != _STATUS_ACTIVE or memory.erased_at is not None:
            self._index.drop(memory_id)
            return
        content = self._read_blob(memory.content_ref)
        if content is None:
            # 正文不可用：这条不能召回，**旧向量也必须下掉**——否则语义路还能
            # 拿一个没有正文的行去排，与"正文缺失不召回"自相矛盾。
            self._index.drop(memory_id)
            return
        self._index.upsert(memory, content, last_seq=int(row["last_seq"]))

    def _index_drop(self, memory_id: str) -> None:
        if self._index is not None:
            self._index.drop(memory_id)

    def iter_indexable(self) -> Iterable[tuple[Memory, str, int]]:
        """可召回的行（active / 未擦除 / 正文在盘上）——重建向量索引的输入。

        第三项是投影行的 `last_seq`（写向量时一并记下，供审计核对"这条向量
        对应哪一版"）。
        """
        self.ensure_schema()
        rows = self._conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories"
            " WHERE status = ? AND erased_at IS NULL ORDER BY memory_id",
            (_STATUS_ACTIVE,),
        ).fetchall()
        # 过期行不参与召回，但**保留向量**：延长有效期/重新激活时不用重建。
        # 建索引时仍给它们写向量，覆盖计数才与"可索引行"对得上（见
        # `MemoryIndex._indexable_row_count` 的同口径说明）。
        for row in rows:
            memory = self._row_to_memory(row)
            content = self._read_blob(memory.content_ref)
            if content is None:
                continue
            yield (memory, content, int(row["last_seq"]))

    def rebuild_vectors(self) -> int:
        """用当前 provider 重算全部向量（幂等、可重建）。返回条数。"""
        if self._index is None:
            raise MemoryStoreError("没有配置向量索引：rebuild_vectors() 无事可做")
        with self._log.transaction():
            self.ensure_schema()
            return int(self._index.rebuild(self))

    # ---- 读接口 ----

    def loadout(
        self, *, task_id: str, workspace_roots: Iterable[str]
    ) -> tuple[MemoryScope, ...]:
        """本 Run 可装备的作用域：user + 各 workspace + 当前 task。

        默认私有：`user` 永远在；workspace 只装备传入的根；task 只装备当前
        任务。不同任务挂不同装备，不注入全库（F10 §6）。
        """
        scopes: list[MemoryScope] = [MemoryScope.user()]
        seen: set[tuple[str, str]] = {_scope_key(scopes[0])}
        for index, root in enumerate(workspace_roots):
            if not isinstance(root, (str, Path)):
                raise MemoryStoreError(
                    f"workspace_roots[{index}] 必须是路径字符串：{root!r}"
                )
            try:
                scope = MemoryScope.workspace(root)
            except TianshuError as exc:
                raise MemoryStoreError(
                    f"workspace_roots[{index}] 不是合法绝对路径：{root!r}（{exc}）"
                ) from exc
            key = _scope_key(scope)
            if key not in seen:
                seen.add(key)
                scopes.append(scope)
        try:
            task_scope = MemoryScope.task(task_id)
        except TianshuError as exc:
            raise MemoryStoreError(f"task_id 不合法：{task_id!r}（{exc}）") from exc
        if _scope_key(task_scope) not in seen:
            scopes.append(task_scope)
        return tuple(scopes)

    def search(
        self,
        query: str,
        *,
        allowed: Sequence[MemoryScope],
        limit: int = MAX_RECALL_ITEMS,
        max_bytes: int = MAX_RECALL_BYTES,
        priority: RecallPriority | None = None,
    ) -> list[Recalled]:
        """**词面检索（明确 FTS-only）**。顺序：过滤 → 排序 → 裁剪。

        要语义融合请用 `retrieve()`：`search()` 不偷偷变 hybrid——库调用方装配了
        向量索引之后行为不该变；需要能力标注的场景一律走 `retrieve()`。
        """
        self.ensure_schema()
        text = _require_str(query, "query", allow_empty=True)
        limit_value = _require_int(limit, "limit", minimum=0)
        max_bytes_value = _require_int(max_bytes, "max_bytes", minimum=0)
        if limit_value == 0 or max_bytes_value == 0:
            return []
        candidates = self._ranked_candidates(text, allowed, priority=priority)
        stale_map = self._stale_map(
            {memory.memory_id: (memory, content) for memory, content in candidates}
        )
        # 陈旧合成层降权但不短路（F10 §8）：排序键唯一实现放在
        # `_order_by_priority`，词面路（search / retrieve 无索引分支）与融合路
        # 必须用同一套，否则"哪条路有降权"取决于有没有配向量索引——复审复现过。
        candidates = _order_by_priority(candidates, priority, stale=stale_map)
        return _crop(candidates, limit_value, max_bytes_value, stale=stale_map)

    def retrieve(
        self,
        query: str,
        *,
        allowed: Sequence[MemoryScope],
        limit: int = MAX_RECALL_ITEMS,
        max_bytes: int = MAX_RECALL_BYTES,
        priority: RecallPriority | None = None,
    ) -> RecallOutcome:
        """检索并**如实标注**这次有没有语义路（F10 §8 / §12）。

        顺序：**先过滤 → 再排序/融合 → 后裁剪**。

        1. 过滤：`allowed` 作用域、`expires_at` 未过期、`status == active`、正文在；
        2. 两路候选取**并集**：FTS 路（bm25 产生名次）+ 语义路（余弦名次，允许
           命中 FTS 没召回的行——只做"重排 FTS 候选"的向量不是召回）；
        3. RRF 融合 `bm25` 与语义两路名次；融合后按 `(作用域优先级, 融合分,
           memory_id)` 稳定排列（F10 §8 的"优先当前任务/工作区"仍是第一分组键）；
        4. 裁剪：条数 + 正文字节。

        `semantic=False` 只在"没有 provider / 索引未完整构建 / provider 不匹配"
        时出现，原因写进 `degraded`；语义路**跑了但没命中**是 `semantic=True` +
        空结果，不叫降级。
        """
        self.ensure_schema()
        text = _require_str(query, "query", allow_empty=True)
        limit_value = _require_int(limit, "limit", minimum=0)
        max_bytes_value = _require_int(max_bytes, "max_bytes", minimum=0)
        if limit_value == 0 or max_bytes_value == 0:
            return RecallOutcome()
        allowed_key_set = set(_allowed_keys(allowed))
        if not allowed_key_set:
            return RecallOutcome()
        now = _require_int(self._clock(), "clock() 返回值")
        fts_candidates = self._fts_candidates(text, allowed_key_set, now)
        stale_map = self._stale_map(
            {memory.memory_id: (memory, content) for memory, content in fts_candidates}
        )
        if self._index is None:
            return RecallOutcome(
                items=_crop(
                    _order_by_priority(fts_candidates, priority, stale=stale_map),
                    limit_value,
                    max_bytes_value,
                    stale=stale_map,
                ),
                semantic=False,
                degraded="未配置向量索引：本次只有词面召回（FTS）",
            )
        available, reason = self._index.availability()
        if not available:
            return RecallOutcome(
                items=_crop(
                    _order_by_priority(fts_candidates, priority, stale=stale_map),
                    limit_value,
                    max_bytes_value,
                    stale=stale_map,
                ),
                semantic=False,
                degraded=reason,
            )
        semantic_ids = self._index.search(
            text,
            allowed=allowed,
            # 语义路要多给一些候选：并集里它可能补进 FTS 没有的行，只取 limit
            # 会在重新融合前就把它们裁掉。
            limit=max(limit_value * 4, 16),
            now=now,
        )
        union: dict[str, tuple[Memory, str]] = {
            memory.memory_id: (memory, content)
            for memory, content in fts_candidates
        }
        for memory_id in semantic_ids:
            if memory_id in union:
                continue
            loaded = self._recall_candidate(memory_id, allowed_key_set, now)
            if loaded is not None:
                union[memory_id] = loaded
        if not union:
            return RecallOutcome(items=[], semantic=True, degraded=None, fused=False)
        fused = dict(
            _rf_fuse([memory.memory_id for memory, _ in fts_candidates], semantic_ids)
        )
        stale_map = self._stale_map(union)  # 并集里可能有语义补进来的页
        ordered = sorted(
            union.items(),
            key=lambda item: (
                _proximity_of(item[1][0], priority),
                # 新鲜度三档（fresh / unknown / stale）与词面路同一实现：
                # 陈旧合成层降权但**不短路**，L1 证据照旧参与（F10 §8）。
                _freshness_rank(item[1][0], stale_map),
                -fused.get(item[0], 0.0),
                item[0],
            ),
        )
        items = _crop(
            [(memory, content) for _memory_id, (memory, content) in ordered],
            limit_value,
            max_bytes_value,
            scores=fused,
            score_kind="rrf",
            stale=stale_map,
        )
        # `fused` 只在**语义候选真的进入最终集合**时为 True：语义路返回了 id 但
        # 全被过滤掉（正文不可读 / 竞态）时，名次里没有任何语义贡献，说"融合过"
        # 会让调用方误读。
        contributed = bool(set(semantic_ids) & set(union))
        return RecallOutcome(
            items=items, semantic=True, degraded=None, fused=contributed
        )

    def _recall_candidate(
        self,
        memory_id: str,
        allowed_key_set: set[tuple[str, str]],
        now: int,
    ) -> tuple[Memory, str] | None:
        """按 id 加载一条**可召回**的记忆（语义路补候选时用同一套过滤）。"""
        row = self._load_row(memory_id)
        if row is None:
            return None
        memory = self._row_to_memory(row)
        if (memory.scope, memory.scope_id) not in allowed_key_set:
            return None
        if memory.status != _STATUS_ACTIVE or memory.erased_at is not None:
            return None
        if memory.expires_at is not None and memory.expires_at <= now:
            return None
        content = self._read_blob(memory.content_ref)
        if content is None:
            return None
        return (memory, content)

    def _fts_candidates(
        self,
        text: str,
        allowed_key_set: set[tuple[str, str]],
        now: int,
    ) -> list[tuple[Memory, str]]:
        """FTS 产生候选（按 `bm25` 排序，已过滤）。名次保留给 RRF 用。"""
        tokens = _tokenize(text)
        if not tokens:
            return []
        query_text = " OR ".join(f'"{token}"' for token in tokens)
        rows = self._conn.execute(
            "SELECT m.memory_id, m.revision, m.scope, m.scope_id, m.kind,"
            " m.content_ref, m.source_refs, m.evidence, m.proof_count, m.origin,"
            " m.status, m.consolidated_at, m.created_at, m.expires_at,"
            " m.supersedes, m.layer, m.content_state, m.last_seq, m.fts_rowid,"
            " m.erased_at"
            " FROM memory_fts JOIN memories AS m"
            " ON m.memory_id = memory_fts.memory_id"
            " WHERE memory_fts MATCH ?"
            " ORDER BY bm25(memory_fts), m.memory_id",
            (query_text,),
        ).fetchall()
        candidates: list[tuple[Memory, str]] = []
        seen_ids: set[str] = set()
        for row in rows:
            memory = self._row_to_memory(row)
            if memory.memory_id in seen_ids:
                continue
            if (memory.scope, memory.scope_id) not in allowed_key_set:
                continue
            if memory.status != _STATUS_ACTIVE:
                continue
            if memory.expires_at is not None and memory.expires_at <= now:
                continue
            content = self._read_blob(memory.content_ref)
            if content is None:
                # 正文缺失：这条记忆不可用，**不**拿别的版本顶上。
                continue
            seen_ids.add(memory.memory_id)
            candidates.append((memory, content))
        return candidates

    def _ranked_candidates(
        self,
        text: str,
        allowed: Sequence[MemoryScope],
        *,
        priority: RecallPriority | None,
    ) -> list[tuple[Memory, str]]:
        """词面路的完整名次（FTS 候选 + 作用域优先级排序）。"""
        allowed_key_set = set(_allowed_keys(allowed))
        if not allowed_key_set:
            return []
        now = _require_int(self._clock(), "clock() 返回值")
        return _order_by_priority(
            self._fts_candidates(text, allowed_key_set, now), priority
        )

    def get(self, memory_id: str, *, allowed: Sequence[MemoryScope]) -> Memory | None:
        """按 id 取记忆（含 forgotten / 过期 / candidate）。作用域外一律 None。"""
        self.ensure_schema()
        memory_id_value = _require_memory_id(memory_id)
        allowed_key_set = set(_allowed_keys(allowed))
        if not allowed_key_set:
            return None
        row = self._load_row(memory_id_value)
        if row is None:
            return None
        memory = self._row_to_memory(row)
        if (memory.scope, memory.scope_id) not in allowed_key_set:
            return None
        return memory

    def observe(
        self, memory_id: str, *, allowed: Sequence[MemoryScope]
    ) -> tuple[str, int] | None:
        """廉价观察：返回 ``(status, revision)``，不可见则 None。

        给 freshness awareness 用：调用方只需要知道"这一层有没有新版本"，
        不需要把整条记忆搬出来。
        """
        memory = self.get(memory_id, allowed=allowed)
        if memory is None:
            return None
        return (memory.status, memory.revision)

    def list_memories(
        self,
        *,
        allowed: Sequence[MemoryScope],
        status: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """按作用域列出记忆（默认含所有状态，便于查看 tombstone）。"""
        self.ensure_schema()
        limit_value = _require_int(limit, "limit", minimum=0)
        if limit_value == 0:
            return []
        status_filter = (
            None
            if status is None
            else _require_closed(status, MEMORY_STATUSES, "status")
        )
        keys = _allowed_keys(allowed)
        if not keys:
            return []
        scope_clause = " OR ".join(["(scope = ? AND scope_id = ?)"] * len(keys))
        params: list[Any] = [value for key in keys for value in key]
        where = f"({scope_clause})"
        if status_filter is not None:
            where += " AND status = ?"
            params.append(status_filter)
        params.append(limit_value)
        rows = self._conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE {where}"
            " ORDER BY created_at DESC, memory_id ASC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def find_duplicate(
        self, *, scope: MemoryScope, kind: str, content: str
    ) -> Memory | None:
        """入库前去重：同 scope + kind + 正文的已有候选/活动记忆。

        正文先过写入闸门再算摘要——只拿原文哈希去比红act 后的存储引用，永远
        比不中，去重就形同虚设。命中密钥直接抛错：阻断的候选连查重都不该进。
        """
        self.ensure_schema()
        if not isinstance(scope, MemoryScope):
            raise MemoryStoreError(f"scope 必须是 MemoryScope：{scope!r}")
        kind_value = _require_closed(kind, MEMORY_KINDS, "kind")
        text = _require_str(content, "content", allow_empty=True)
        safe, blocked = self.redact_or_block(text)
        if blocked:
            raise MemoryWriteBlocked("候选正文命中密钥闸门：阻断，不入库")
        content_ref = sha256_hex(safe.encode("utf-8"))
        row = self._conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories"
            " WHERE scope = ? AND scope_id = ? AND kind = ? AND content_ref = ?"
            "   AND status IN ('candidate', 'active')"
            " ORDER BY created_at DESC, memory_id ASC LIMIT 1",
            (scope.scope, scope.scope_id, kind_value, content_ref),
        ).fetchone()
        return None if row is None else self._row_to_memory(row)


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


__all__ = [
    "MAX_RECALL_BYTES",
    "MAX_RECALL_ITEMS",
    "MEMORY_KINDS",
    "MEMORY_LAYERS",
    "MEMORY_ORIGINS",
    "MEMORY_STATUSES",
    "TOKENIZER_VERSION",
    "Memory",
    "MemoryProjection",
    "MemoryStoreError",
    "MemoryWriteBlocked",
    "Recalled",
]