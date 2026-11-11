"""F10 检索的第二路：向量索引（**可重建投影，不是第二份事实**）。

## 为什么单独一个模块

记忆的事实源仍是事件日志（红线 1），`memories` 那张表是第一层投影。向量是
**同一份正文的另一种索引**：删掉它不丢任何事实，`rebuild()` 能逐字节重建。
把它塞进 `memory.py` 会让"投影"和"索引"两件事混在一起——投影的可重建性
（`projection_hash()`）会被一个依赖 provider 的派生数据污染。

## 语义能力必须自报家门

这里只定义**接缝**，不假装有能力：

- :class:`EmbeddingProvider` 是协议（`model_id` / `dim` / `embed()`）；
- :class:`HashEmbeddingProvider` 是**确定性哈希基线，没有语义能力**——它只用于
  测试与离线基线，任何对外说明都不许把它写成"语义召回"；
- 外部 provider 只有在用户显式配置、且该任务允许数据外发时才启用；未配置时
  检索**降级为 FTS-only 并如实标注**（`semantic=False` + `degraded` 原因），
  不静默假装有语义。

## 不混空间

检索前先按 `model_id + dim` 过滤：换过 provider 之后，旧向量**不参与**融合，
直到 `rebuild_vectors()` 用新 provider 重算。否则两套不同空间的分数会直接
相加，给出一个既不可解释也不可复现的名次。

## 融合

RRF（reciprocal rank fusion）：两路各自排序后 `score = Σ 1/(k + rank)`，
同分按 `memory_id` 稳定排序。RRF 只用**名次**，不用原始分数——bm25 与余弦
的量纲根本不可比，谁也不该被"归一化"之后假装能相加。
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import struct
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .memory import MemoryStoreError
from .resources import MemoryScope

if TYPE_CHECKING:  # pragma: no cover - 只要类型，runtime 不反向依赖 memory 的实体
    from .memory import Memory

__all__ = [
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "MemoryIndex",
    "RRF_K",
    "rf_fuse",
]

#: RRF 的平滑常数（原论文的默认值）。
RRF_K = 60

#: 向量按小端 float32 平铺存 blob。维度写进行里，跨维度不混用。
_VECTOR_FORMAT = "<{dim}f"

#: 索引是**可重建**的派生数据：它的版本只影响"能不能直接用"，不影响事实。
#: provider 换掉后旧向量不参与检索（见模块 docstring）。
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_vectors (
        memory_id  TEXT PRIMARY KEY,
        revision   INTEGER NOT NULL,
        model_id   TEXT NOT NULL,
        dim        INTEGER NOT NULL,
        vec        BLOB NOT NULL,
        last_seq   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_vector_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

#: 记录"这张表是哪套 provider 建的"。schema 说明与降级原因要能指到具体值。
_META_MODEL = "provider_model_id"
_META_DIM = "provider_dim"
#: 只有**成功构建过**（`rebuild()` 完整跑完）才置位。空表不算"能语义召回"：
#: 否则旧库/新 provider 打开时会报 semantic=true，实际一条向量都没有。
_META_BUILT = "built"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """向量 provider 的接缝。

    只要求三件事：稳定的 `model_id`、固定的 `dim`、批量 `embed()`。**不约定**
    是否远程、是否要钱、是否允许外发——那是装配策略（F10 §10 数据边界）的事，
    由调用方在把 provider 交给索引之前决定。
    """

    model_id: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:  # pragma: no cover
        ...


def _hash_tokens(text: str) -> tuple[str, ...]:
    """哈希基线用的确定性切分（**不是** FTS 的分词器）。

    FTS 的双字切分以 `memory.TOKENIZER_VERSION` 为契约；这里刻意不复用：哈希
    基线是"另一套独立信号"，复用它会让两路打分同源，融合就失去意义。规则写成
    最小、可复现的一套：相邻汉字双字组 + 英文/数字词（小写）。
    """
    tokens: list[str] = []
    for chunk in _TOKEN_RE.findall(text):
        tokens.append(chunk.lower())
    cjk = [char for char in text if "\u4e00" <= char <= "\u9fff"]
    for index in range(len(cjk) - 1):
        tokens.append(cjk[index] + cjk[index + 1])
    return tuple(tokens)


class HashEmbeddingProvider:
    """确定性哈希向量：**没有语义能力**，只用于测试与离线基线。

    做法：每个 token 用 blake2b 派生出 `dim` 维上的 ±1 稀疏贡献，累加后 L2
    归一化。它抓住的是"词面重叠"的连续版本，**抓不住同义词/改写**——任何把
    它当语义模型的说法都是假的，`docs/memory-retrieval-eval.md` 里按这个口径
    报告数字。
    """

    def __init__(self, *, dim: int = 64, model_id: str = "hash-64-v1") -> None:
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise MemoryStoreError(f"hash provider 的 dim 必须是正整数：{dim!r}")
        self.dim = dim
        self.model_id = model_id

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, str):
            raise MemoryStoreError("embed() 需要字符串序列，不是单个字符串")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _hash_tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "little") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        return _normalize(vector)


def _normalize(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return values
    return [value / norm for value in values]


def _pack(vector: Sequence[float], *, dim: int) -> bytes:
    if len(vector) != dim:
        raise MemoryStoreError(f"向量维度不符：期望 {dim}，实际 {len(vector)}")
    return struct.pack(_VECTOR_FORMAT.format(dim=dim), *vector)


def _unpack(blob: bytes, *, dim: int) -> tuple[float, ...]:
    expected = struct.calcsize(_VECTOR_FORMAT.format(dim=dim))
    if len(blob) != expected:
        raise MemoryStoreError(
            f"向量 blob 长度不符：期望 {expected} 字节（dim={dim}），实际 {len(blob)}"
        )
    return struct.unpack(_VECTOR_FORMAT.format(dim=dim), blob)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """两向量余弦；先各自归一化，零向量按 0 分处理（不猜方向）。"""
    norm_l = math.sqrt(sum(value * value for value in left))
    norm_r = math.sqrt(sum(value * value for value in right))
    if norm_l == 0.0 or norm_r == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (norm_l * norm_r)


def rf_fuse(rankings: Sequence[Sequence[str]], *, k: int = RRF_K) -> list[tuple[str, float]]:
    """RRF 融合：`score = Σ 1/(k + rank)`（rank 从 1 开始）。

    纯函数：给定同一批名次表，输出逐位相同。同分按 `memory_id` 升序，不依赖
    字典插入顺序——否则同一份数据在不同进程里可能排出不同名次。
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise MemoryStoreError(f"RRF 的 k 必须是正整数：{k!r}")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, memory_id in enumerate(ranking, start=1):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class MemoryIndex:
    """`memory_vectors` 表 + 向量检索。所有写入都在调用方的事务里。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        if not isinstance(conn, sqlite3.Connection):
            raise MemoryStoreError("MemoryIndex 需要 sqlite3.Connection")
        if provider is not None:
            model_id = getattr(provider, "model_id", None)
            dim = getattr(provider, "dim", None)
            if not isinstance(model_id, str) or not model_id:
                raise MemoryStoreError("provider.model_id 必须是非空字符串")
            if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
                raise MemoryStoreError("provider.dim 必须是正整数")
        self._conn = conn
        self._provider = provider
        self._schema_ready = False

    @property
    def provider(self) -> EmbeddingProvider | None:
        return self._provider

    def ensure_schema(self) -> None:
        """建表（幂等，可在已有事务里调用）。

        与 `MemoryProjection.ensure_schema()` 同一个坑：**建表本身不缓存**。
        如果这次建表发生在调用方的事务里，那个事务回滚会把表一起回滚，缓存
        "已就绪"就会让后续调用在空库上直接 `no such table`。只有在外层没有
        事务（自提交）时才缓存。
        """
        if self._schema_ready:
            return
        outer = self._conn.in_transaction
        for statement in _SCHEMA_STATEMENTS:
            self._conn.execute(statement)
        if not outer:
            self._schema_ready = True

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_vector_meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM memory_vector_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def stored_model(self) -> tuple[str | None, int | None]:
        """表里现存向量属于哪套 provider（没有就是 `(None, None)`）。"""
        self.ensure_schema()
        model_id = self._meta(_META_MODEL)
        raw_dim = self._meta(_META_DIM)
        try:
            dim = int(raw_dim) if raw_dim is not None else None
        except ValueError:
            dim = None
        return model_id, dim

    def availability(self) -> tuple[bool, str | None]:
        """现在能不能做语义召回：`(可用, 不可用的原因)`。

        四个前提缺一不可：装配了 provider、**成功构建过完整索引**（`built` 标记）、
        索引属于**当前** provider（`model_id + dim`）、向量覆盖当前全部可索引行
        （数量对得上）。只看 provider 身份不够——空表和"半套向量"都会报成可用，
        那是对能力撒谎；这里宁可保守报降级，也不给假 `semantic=true`。
        """
        if self._provider is None:
            return (False, "未配置向量 provider：检索降级为 FTS-only（无语义召回）")
        self.ensure_schema()
        stored_model, stored_dim = self.stored_model()
        if stored_model is None:
            return (
                False,
                f"向量索引尚未建立（当前 provider {self._provider.model_id}）；"
                "先 rebuild_vectors() 再启用语义召回",
            )
        if stored_model != self._provider.model_id or stored_dim != self._provider.dim:
            return (
                False,
                f"向量索引属于 {stored_model}/{stored_dim}，当前 provider 是 "
                f"{self._provider.model_id}/{self._provider.dim}；旧向量不参与融合，"
                "先 rebuild_vectors()",
            )
        if self._meta(_META_BUILT) != "1":
            return (
                False,
                "向量索引未完整构建（可能被投影重建或新 provider 弄脏）；"
                "先 rebuild_vectors()",
            )
        indexable = self._indexable_row_count()
        vectors = self._vector_count(stored_model, stored_dim)
        if vectors != indexable:
            return (
                False,
                f"向量覆盖不完整：可索引行 {indexable} 条、当前空间向量 {vectors} 条"
                "（可能有行缺向量或混入旧 provider 的向量）；先 rebuild_vectors()",
            )
        return (True, None)

    def _indexable_row_count(self) -> int:
        """可索引行数（active / 未擦除 / 正文在）——与 `iter_indexable` 同口径。

        不排除过期行：过期只是"不参与召回"，向量仍保留（重新激活/延长有效期时
        不用重建）。召回过滤在 `search()` 里做。
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'active'"
            " AND erased_at IS NULL AND content_state = 'available'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _vector_count(self, model_id: str, dim: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memory_vectors WHERE model_id = ? AND dim = ?",
            (model_id, dim),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    # ---- 写入（调用方负责事务） ----

    def upsert(self, memory: Memory, content: str, *, last_seq: int = 0) -> None:
        """给一条**可召回**的记忆写向量。没有 provider 时静默跳过（FTS-only）。"""
        if self._provider is None:
            return
        self.ensure_schema()
        vector = self._embed_one(content)
        self._conn.execute(
            "INSERT INTO memory_vectors(memory_id, revision, model_id, dim, vec, last_seq)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(memory_id) DO UPDATE SET"
            " revision=excluded.revision, model_id=excluded.model_id,"
            " dim=excluded.dim, vec=excluded.vec, last_seq=excluded.last_seq",
            (
                memory.memory_id,
                memory.revision,
                self._provider.model_id,
                self._provider.dim,
                _pack(vector, dim=self._provider.dim),
                last_seq,
            ),
        )

    def drop(self, memory_id: str) -> None:
        self.ensure_schema()
        self._conn.execute(
            "DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,)
        )

    def _embed_one(self, text: str) -> list[float]:
        assert self._provider is not None
        vectors = self._provider.embed([text])
        if len(vectors) != 1:
            raise MemoryStoreError(
                f"provider.embed() 返回 {len(vectors)} 条向量，期望 1 条"
            )
        vector = vectors[0]
        if len(vector) != self._provider.dim:
            raise MemoryStoreError(
                f"provider.embed() 返回维度 {len(vector)}，与 provider.dim="
                f"{self._provider.dim} 不符"
            )
        return [float(value) for value in vector]

    def rebuild(self, projection: Any) -> int:
        """用当前 provider 重算全部可召回记忆的向量，返回条数。

        清空是重建的起点：provider 换了之后，旧向量与新向量不可比，留着只会
        制造"有的能融合有的不能"的半状态。整个重建在调用方事务里，失败回滚。
        """
        self.ensure_schema()
        # 先清空并**作废** build 标记：中途失败时不能留下"旧向量 + 新 provider"
        # 或"半套向量"被当成可用。
        self._conn.execute("DELETE FROM memory_vectors")
        self._set_meta(_META_BUILT, "0")
        if self._provider is None:
            self._conn.execute(
                "DELETE FROM memory_vector_meta WHERE key IN (?, ?, ?)",
                (_META_MODEL, _META_DIM, _META_BUILT),
            )
            return 0
        written = 0
        for memory, content, last_seq in projection.iter_indexable():
            self.upsert(memory, content, last_seq=last_seq)
            written += 1
        self._set_meta(_META_MODEL, self._provider.model_id)
        self._set_meta(_META_DIM, str(self._provider.dim))
        self._set_meta(_META_BUILT, "1")
        return written

    # ---- 读 ----

    def search(
        self,
        query: str,
        *,
        allowed: Sequence[MemoryScope],
        limit: int,
        now: int | None = None,
    ) -> list[str]:
        """按余弦相似度返回 `memory_id`（已按作用域/状态/过期过滤）。

        返回**空列表**有三个正当理由：没有 provider、索引不可用、真的没有候选。
        调用方必须结合 :meth:`availability` 区分"降级"与"没有结果"，不能把前者
        说成"没找到"。
        """
        available, _reason = self.availability()
        if not available or limit <= 0:
            return []
        allowed_keys = {
            (scope.scope, scope.scope_id) for scope in allowed
        }
        if not allowed_keys:
            return []
        assert self._provider is not None
        query_vector = self._embed_one(query)
        rows = self._conn.execute(
            "SELECT v.memory_id, v.vec, m.scope, m.scope_id"
            " FROM memory_vectors AS v JOIN memories AS m"
            " ON m.memory_id = v.memory_id"
            " WHERE v.model_id = ? AND v.dim = ?"
            " AND m.status = 'active' AND m.erased_at IS NULL"
            " AND (m.expires_at IS NULL OR m.expires_at > ?)",
            (self._provider.model_id, self._provider.dim, now if now is not None else 0),
        ).fetchall()
        scored: list[tuple[float, str]] = []
        for row in rows:
            if (str(row["scope"]), str(row["scope_id"])) not in allowed_keys:
                continue
            vector = _unpack(row["vec"], dim=self._provider.dim)
            score = _cosine(query_vector, vector)
            if score <= 0.0:
                continue
            scored.append((score, str(row["memory_id"])))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [memory_id for _score, memory_id in scored[:limit]]