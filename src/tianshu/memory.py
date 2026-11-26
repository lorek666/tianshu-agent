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

## 内部模块拆分（零契约变更）

- `_memory_types.py`：常量、异常、值对象、排序裁剪、分词、PII/密钥闸门、Schema 与入参校验
- `_memory_apply.py`：`_MemoryApplyMixin`（写事件应用与蒸馏源提取）
- 本模块：保留 `MemoryProjection` 投影与检索入口，并重导出全部公开符号及测试/实验脚本
  依赖的私有符号（`_SCHEMA_VERSION` / `_order_by_priority` / `_allowed_keys`）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from ._memory_apply import _MemoryApplyMixin
from ._memory_types import (
    _BASE64_PATTERN,
    _BLOCK_PATTERNS,
    _CONTENT_STATES,
    _CONTENT_UNAVAILABLE,
    _MEMORY_COLUMNS,
    _SCHEMA_STATEMENTS,
    _SCHEMA_VERSION,
    _STATUS_ACTIVE,
    _WRITE_TOOL_IDS,
    MAX_RECALL_BYTES,
    MAX_RECALL_ITEMS,
    MEMORY_KINDS,
    MEMORY_LAYERS,
    MEMORY_ORIGINS,
    MEMORY_STATUSES,
    TOKENIZER_VERSION,
    Memory,
    MemoryStoreError,
    MemoryWriteBlocked,
    Recalled,
    RecallOutcome,
    RecallPriority,
    _allowed_keys,
    _crop,
    _freshness_rank,
    _looks_like_base64_secret,
    _now,
    _order_by_priority,
    _proximity_of,
    _redact,
    _require_closed,
    _require_content_ref,
    _require_int,
    _require_json_list,
    _require_memory_id,
    _require_optional_int,
    _require_str,
    _require_str_tuple,
    _rf_fuse,
    _scope_key,
    _tokenize,
    _write_tool_id,
)
from .errors import TianshuError
from .events import EventLog, canonical_bytes, sha256_hex
from .fsutil import atomic_write_bytes
from .resources import MemoryScope


class MemoryProjection(_MemoryApplyMixin):
    """事件日志上的记忆投影。所有写入都从 ``apply_event()`` 进入。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        log: EventLog,
        blob_dir: Path | None = None,
        data_root: Path | None = None,
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
        from .restore_access import (
            default_material_paths,
            ensure_path_not_held,
            preflight_legacy_material_layout,
        )

        root = log.data_root if data_root is None else data_root
        preflight_legacy_material_layout(log.db_path, root)
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
        # 正文目录使用声明布局的默认记忆路径；显式目录仍优先。
        self._blob_dir = (
            Path(blob_dir)
            if blob_dir is not None
            else default_material_paths(log.db_path, root)[2]
        )
        if self._blob_dir is not None:
            ensure_path_not_held(self._blob_dir)
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
        from .restore_access import ensure_log_write_allowed, ensure_path_not_held

        ensure_log_write_allowed(self._log)
        if self._blob_dir is not None:
            ensure_path_not_held(self._blob_dir)
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
        from .restore_access import ensure_log_write_allowed, ensure_path_not_held

        ensure_log_write_allowed(self._log)
        if self._blob_dir is not None:
            ensure_path_not_held(self._blob_dir)
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
        from .restore_access import ensure_log_write_allowed, ensure_path_not_held

        ensure_log_write_allowed(self._log)
        if self._blob_dir is not None:
            ensure_path_not_held(self._blob_dir)
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
        from .restore_access import ensure_material_write_allowed

        with ensure_material_write_allowed(self._blob_dir, owner_log=self._log):
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
        owners = [str(row["memory_id"]) for row in rows]
        try:
            from .mcp_results import referenced_mcp_digests

            if reference in referenced_mcp_digests(self._log):
                owners.append("mcp:append-only-result")
        except Exception:  # noqa: BLE001 - 数不清引用就不删，宁可留字节
            owners.append("mcp:reference-projection-unavailable")
        return len(owners), owners

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
        from .restore_access import ensure_log_write_allowed, ensure_path_not_held

        ensure_log_write_allowed(self._log)
        if self._blob_dir is not None:
            ensure_path_not_held(self._blob_dir)
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
        allow_embedding_outbound: bool = False,
    ) -> RecallOutcome:
        """检索并**如实标注**这次有没有语义路（F10 §8 / §12）。

        顺序：**先过滤 → 再排序/融合 → 后裁剪**。

        1. 过滤：`allowed` 作用域、`expires_at` 未过期、`status == active`、正文在；
        2. 两路候选取**并集**：FTS 路（bm25 产生名次）+ 语义路（余弦名次，允许
           命中 FTS 没召回的行——只做"重排 FTS 候选"的向量不是召回）；
        3. RRF 融合 `bm25` 与语义两路名次；融合后按 `(作用域优先级, 融合分,
           memory_id)` 稳定排列（F10 §8 的"优先当前任务/工作区"仍是第一分组键）；
        4. 裁剪：条数 + 正文字节。

        `semantic=False` 只在"没有 provider / 索引未完整构建 / provider 不匹配 /
        外部 provider 的外发未被本次调用允许"时出现，原因写进 `degraded`；
        语义路**跑了但没命中**是 `semantic=True` + 空结果，不叫降级。

        外发 gate：`allow_embedding_outbound` 默认 **False**——模型工具的检索
        永不触发 embedding 的网络外发；只有人显式发起的控制面动作（CLI search /
        面板 RPC）才传 True。`sends_data_off_device=False` 的本机 provider
        （如 hash 基线）不受此门影响。本门只管**检索**：写入激活（confirm/edit/
        remember apply 的向量 upsert）也会把正文送端点，外发授权来源是批准
        该记忆动作的人（SPEC-N2 §2.3 边界表）。
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
        # 外发 gate（W6，SPEC-N2-EMBEDDING-PROVIDER §2.3）：检索 embed(query)
        # 会把查询文本发给配置端点——模型工具路径默认不打开这个许可（安全默认），
        # 外部 provider 时降级为 FTS-only 并如实标注；控制面显式动作（CLI /
        # 面板 RPC）由调用点传 True。未知 provider 按"会外发"处理。
        provider = self._index.provider
        sends_off_device = bool(
            getattr(provider, "sends_data_off_device", True) if provider is not None else False
        )
        if sends_off_device and not allow_embedding_outbound:
            return RecallOutcome(
                items=_crop(
                    _order_by_priority(fts_candidates, priority, stale=stale_map),
                    limit_value,
                    max_bytes_value,
                    stale=stale_map,
                ),
                semantic=False,
                degraded=(
                    "外部 embedding provider 的网络外发未被本次调用允许"
                    "（模型检索不外发）；本次只有词面召回（FTS）"
                ),
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
