"""F10 记忆投影写入事件处理与蒸馏源提取（内部 Mixin，由 `MemoryProjection` 继承）。

所有投影变更仍只能由 `MemoryProjection.apply_event()` 在与事件日志同一 SQLite
事务内触发；本模块不独立持有连接、不写事件、不对外暴露公共类型。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ._memory_types import (
    _CONTENT_AVAILABLE,
    _CONTENT_UNAVAILABLE,
    _MEMORY_COLUMNS,
    _STATUS_ACTIVE,
    _STATUS_CANDIDATE,
    _STATUS_FORGOTTEN,
    _STATUS_SUPERSEDED,
    MEMORY_KINDS,
    MEMORY_LAYERS,
    MEMORY_ORIGINS,
    MEMORY_STATUSES,
    Memory,
    MemoryStoreError,
    MemoryWriteBlocked,
    _require_bool,
    _require_closed,
    _require_content_ref,
    _require_int,
    _require_json_list,
    _require_memory_id,
    _require_optional_int,
    _require_str,
    _require_str_tuple,
    _scope_from_payload,
    _scope_key,
    _tokenize,
)
from .errors import TianshuError
from .resources import MemoryScope, parse_memory_uri


class _MemoryApplyMixin:
    """记忆写事件应用与合成层新鲜度辅助，供 `MemoryProjection` 组合继承。"""

    _conn: sqlite3.Connection
    _clock: Any

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
