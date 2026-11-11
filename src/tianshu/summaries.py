"""模型语义摘要的记录与材料身份（PRD §7.1）。

## 这一层解决什么

`context.py` 到现在只会确定性裁剪：落盘占位、降级预览、整段丢弃。**被丢掉的那段
历史就真的没了**——模型下一轮只能看到"已丢弃 N 条消息（seq …）"这句说明。PRD
§7.1 允许"后续引入模型摘要"，但附加了五条边界。本模块只做其中"记录 + 材料身份"
那部分（H1），生成路径在 `loop`，注入与回放在 `context` / `replay`。

## 三个刻意的设计

1. **材料是"本来就要丢的那几段"，不是"最近 N 条"**。只有确定会消失的内容才值得
   花一次模型调用去压缩；材料由调用方按 `ContextView.dropped_seqs` 还原后传进来，
   本模块只负责**有界化**（单条 1 KiB、总共 8 KiB）与**身份化**。
2. **身份里同时有 seq 与原文哈希**。`material_id` 由 `(scope, sources, 材料版本)`
   的 JCS 摘要算出，每条来源记 `seq:<N>` 与原文的 sha256。这样"这份摘要到底摘的
   是什么"可以逐条核对：原文改一个字，id 就变，旧摘要不会被误当成新材料的结论。
   截断只影响**渲染文本**，不影响来源哈希——记录的是原文身份，不是截断片段的身份。
3. **版本只追加，正文写入后不可改**。同一材料换模型 / 显式刷新 = 追加 `version+1`
   的新行；历史 `model.intent` 按 `summary_id` 指向旧行，回放永远读当时那一份。
   这既是 PRD §7.1 第 3 条，也是"换摘要模型不改变历史结果"的实现基础。

## 与事件的关系

**不新增事件类型**（`EVENT_TYPES` 仍 41 个）。摘要调用的审计面是三处，各有单一
职责、不重复存正文：

- 本模块的表：摘要正文、来源、模型 / prompt / 摘要版本、实际 usage、`attempt_id`；
- 预算账本的 `budget_reservations` / `budget_counters`：这次调用预占与结算了多少；
- `model.intent.payload.context.summaries`：这一轮**实际引用**了哪几个版本；
- `RunResult.summary_calls`：这个 Run 一共做了几次摘要调用（resume 不丢）。

## 不说的话

- 不保证摘要"更好"：只保证有来源、有界、可回放、遗漏可发现（探测器在测试里）。
- 不做保留期 / 垃圾回收；不删除任何版本（表上连 `DELETE` 都被触发器拦住）。
- 不做跨机器同步、不做跨 Run 复用（材料身份含事件 seq）。
- 不判断摘要正文里有没有指令——那是执行侧闸门的事（PRD §8.1）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .canonical import jcs_sha256
from .errors import TianshuError

#: 材料渲染格式版本。改了渲染规则必须改它：否则同一批来源会算出同一个
#: `material_id`，而渲染文本不同，"同材料同摘要"的判定就失效了。
MATERIAL_VERSION = "summary-material-v1"

#: 摘要提示词版本与摘要正文格式版本（PRD §7.1 第 2 条要求记录这两个）。
PROMPT_VERSION = "summary-prompt-v1"
SUMMARY_VERSION = "summary-v1"

#: 单条材料渲染上限 / 材料总字节上限 / 摘要正文上限。
#:
#: 三个都是**字节**，不是 token：字节是可精确复现的量（同一串 UTF-8 在任何机器
#: 上 `len(encode())` 都一样）。它们约束的是**输入输出有界**，不冒充 PRD §5.7 的
#: token 上界——token 记账仍走账本的 estimate / upper 两种口径。
ITEM_BYTES = 1024
MATERIAL_BYTES = 8192
SUMMARY_MAX_BYTES = 2048

#: `failed` 原因的有界长度。失败原因来自异常文本，不能让它无限长。
ERROR_MAX_BYTES = 512

#: 材料最多来源条数（防止一次摘要挂上几千条来源把记录撑大）。
MAX_SOURCES = 256

#: 状态机。`pending → ready | failed` 一次性；`ready` / `failed` 是终态。
PENDING = "pending"
READY = "ready"
FAILED = "failed"
STATUSES: tuple[str, ...] = (PENDING, READY, FAILED)

SCHEMA = """
-- 材料：一次"要摘什么"的身份。内容寻址：同材料同 id。
CREATE TABLE IF NOT EXISTS summary_materials (
    material_id  TEXT PRIMARY KEY,
    scope        TEXT NOT NULL,
    task_id      TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL,
    text_digest  TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);

-- 逐条来源：事件 seq + **原文内容哈希**（不是截断后的渲染片段）。
CREATE TABLE IF NOT EXISTS summary_sources (
    material_id TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    digest      TEXT NOT NULL,
    PRIMARY KEY (material_id, ordinal)
);

-- 摘要版本：同一材料可追加多个版本（换模型 / 显式刷新），历史引用不改指向。
CREATE TABLE IF NOT EXISTS summary_versions (
    summary_id      TEXT PRIMARY KEY,
    material_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL,
    text            TEXT,
    text_digest     TEXT,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    summary_version TEXT NOT NULL,
    attempt_id      TEXT NOT NULL DEFAULT '',
    error           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    micros          INTEGER,
    run_id          TEXT NOT NULL DEFAULT '',
    task_id         TEXT NOT NULL DEFAULT '',
    scope           TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    UNIQUE (material_id, version)
);

-- 追加式：记录不许删（订正靠追加新版本）。正文写入后不许改。
CREATE TRIGGER IF NOT EXISTS summary_versions_no_delete
BEFORE DELETE ON summary_versions
BEGIN
    SELECT RAISE(ABORT, '摘要记录是追加式：不允许 DELETE（订正靠追加新版本）');
END;

CREATE TRIGGER IF NOT EXISTS summary_versions_frozen_text
BEFORE UPDATE ON summary_versions
WHEN OLD.status IN ('ready', 'failed')
     AND (COALESCE(NEW.text, '') IS NOT COALESCE(OLD.text, '')
          OR COALESCE(NEW.text_digest, '') IS NOT COALESCE(OLD.text_digest, ''))
BEGIN
    SELECT RAISE(ABORT, '摘要正文写入后不可改：需要刷新请追加新版本');
END;

CREATE TRIGGER IF NOT EXISTS summary_materials_no_delete
BEFORE DELETE ON summary_materials
BEGIN
    SELECT RAISE(ABORT, '摘要材料是追加式：不允许 DELETE');
END;

CREATE TRIGGER IF NOT EXISTS summary_sources_no_delete
BEFORE DELETE ON summary_sources
BEGIN
    SELECT RAISE(ABORT, '摘要来源是追加式：不允许 DELETE');
END;
"""


class SummaryError(TianshuError):
    """摘要记录层的错误基类。"""


class SummaryMaterialError(SummaryError):
    """材料构造不成立（空、超界、来源形状不对）。这是**调用方给错了**。"""


class SummaryEmpty(SummaryError):
    """摘要正文是空白：宁可判失败回落确定性裁剪，也不把空串当成摘要。"""


class SummaryTooLong(SummaryError):
    """摘要正文超过 `SUMMARY_MAX_BYTES`：拒绝而不是静默截断。"""


class SummaryStateError(SummaryError):
    """非法状态转移（重复结算 / 覆盖已写正文 / 同材料已有未结算尝试）。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def text_digest(text: str) -> str:
    """摘要正文 / 材料渲染文本的摘要：`sha256(UTF-8 字节)`。

    与 `contents.py` 的引用口径同源（内容寻址一律按 UTF-8 字节算），不另造一套。
    """
    if not isinstance(text, str):
        raise SummaryError(f"摘要只对文本定义：{type(text).__name__}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节截断，不切坏多字节字符（与 `context._preview` 同一条规则）。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


@dataclass(frozen=True)
class MaterialBlock:
    """一条材料来源：事件 seq + 角色 + **原文**。"""

    seq: int
    role: str
    content: str

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq <= 0:
            raise SummaryMaterialError(f"材料来源的 seq 必须是正整数：{self.seq!r}")
        if not isinstance(self.role, str) or not self.role:
            raise SummaryMaterialError(f"材料来源的 role 必须是非空字符串：{self.role!r}")
        if not isinstance(self.content, str):
            raise SummaryMaterialError("材料来源的正文必须是字符串")


@dataclass(frozen=True)
class MaterialSource:
    """材料来源的**身份**：种类 + 引用 + 原文内容哈希。"""

    kind: str
    ref: str
    digest: str


@dataclass(frozen=True)
class SummaryMaterial:
    """一份有界材料：身份 + 逐条来源 + 渲染文本。"""

    material_id: str
    scope: str
    task_id: str
    run_id: str
    sources: tuple[MaterialSource, ...]
    text: str

    def __post_init__(self) -> None:
        if not self.sources:
            raise SummaryMaterialError("材料至少要有一条来源")
        if len(self.sources) > MAX_SOURCES:
            raise SummaryMaterialError(
                f"材料来源超过上限 {MAX_SOURCES}：{len(self.sources)}"
            )
        if len(self.text.encode("utf-8")) > MATERIAL_BYTES:
            raise SummaryMaterialError(
                f"材料渲染文本 {len(self.text.encode('utf-8'))}B 超过上限 "
                f"{MATERIAL_BYTES}B"
            )

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def digest(self) -> str:
        return text_digest(self.text)


@dataclass(frozen=True)
class SummaryRecord:
    """一条摘要版本记录（与表行一一对应）。"""

    summary_id: str
    material_id: str
    version: int
    status: str
    text: str | None
    text_digest: str | None
    model: str
    prompt_version: str
    summary_version: str
    attempt_id: str
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    micros: int | None
    run_id: str
    task_id: str
    scope: str
    created_at: int

    @property
    def ready(self) -> bool:
        return self.status == READY

    def to_payload(self) -> dict[str, Any]:
        """诊断 / CLI 用的投影。`text` 只在 ready 时出现（failed 没有正文）。"""
        out: dict[str, Any] = {
            "summary_id": self.summary_id,
            "material_id": self.material_id,
            "version": self.version,
            "status": self.status,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "summary_version": self.summary_version,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "scope": self.scope,
            "created_at": self.created_at,
        }
        if self.text is not None:
            out["text"] = self.text
            out["text_digest"] = self.text_digest
            out["bytes"] = len(self.text.encode("utf-8"))
        if self.error is not None:
            out["error"] = self.error
        if self.input_tokens is not None or self.output_tokens is not None:
            out["usage"] = {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }
        if self.micros is not None:
            out["micros"] = self.micros
        return out


# ---------------------------------------------------------------- 材料


def render_block(block: MaterialBlock) -> str:
    """把一条来源渲染成材料里的一节。确定性：同输入同输出。"""
    content = block.content
    raw = content.encode("utf-8")
    if len(raw) > ITEM_BYTES:
        content = _clip_bytes(content, ITEM_BYTES) + "…（单条已截断）"
    return f"[seq={block.seq} role={block.role}]\n{content}\n"


def build_material(
    blocks: Iterable[MaterialBlock],
    *,
    scope: str,
    task_id: str = "",
    run_id: str = "",
) -> SummaryMaterial:
    """把候选来源有界化成一份材料。纯函数：同输入必得同 `material_id`。

    选取顺序就是传入顺序（调用方按 seq 升序给）；**放不下就停**，不为了凑满上限
    去拆一条来源——拆出来的半条会让"来源哈希"与"渲染文本"对不上。
    """
    if not isinstance(scope, str) or not scope:
        raise SummaryMaterialError("材料必须有作用域（scope 不能为空）")
    ordered = list(blocks)
    if not ordered:
        raise SummaryMaterialError("没有可摘要的材料（来源为空）")
    sources: list[MaterialSource] = []
    pieces: list[str] = []
    total = 0
    for block in ordered:
        if len(sources) >= MAX_SOURCES:
            break
        piece = render_block(block)
        size = len(piece.encode("utf-8"))
        if total + size > MATERIAL_BYTES:
            break
        sources.append(
            MaterialSource(
                kind="event",
                ref=f"seq:{block.seq}",
                digest=hashlib.sha256(block.content.encode("utf-8")).hexdigest(),
            )
        )
        pieces.append(piece)
        total += size
    if not sources:
        raise SummaryMaterialError(
            f"材料放不下：单条渲染最大 {MATERIAL_BYTES}B，第一条就超了"
        )
    text = "".join(pieces)
    identity = {
        "material_version": MATERIAL_VERSION,
        "scope": scope,
        "sources": [
            {"kind": s.kind, "ref": s.ref, "digest": s.digest} for s in sources
        ],
    }
    return SummaryMaterial(
        material_id=jcs_sha256(identity),
        scope=scope,
        task_id=task_id,
        run_id=run_id,
        sources=tuple(sources),
        text=text,
    )


def prompt_for(material: SummaryMaterial) -> str:
    """摘要提示词：把材料交给模型，要求**只**压缩、不加新事实、有界输出。

    提示词是**版本化**的（`PROMPT_VERSION`）：它变了，同一材料的摘要内容就可能
    变，所以记录里必须写清是哪一版（PRD §7.1 第 2 条）。
    """
    limit = SUMMARY_MAX_BYTES
    return (
        f"[{PROMPT_VERSION}]\n"
        "下面是一段即将被上下文预算丢弃的早期对话材料。请把它压缩成要点，要求：\n"
        f"1. 只输出纯文本要点，总长度不超过 {limit} 字节（UTF-8）；\n"
        "2. 保留：硬性约束、数字、文件名/路径、错误信息、用户明确要求的产出格式；\n"
        "3. 不要新增材料里没有的事实，不要写建议或解释，不要输出工具调用；\n"
        "4. 无法判断重要性时，优先保留约束与数字。\n"
        f"材料（{len(material.sources)} 条，{material.bytes} 字节）：\n"
        f"{material.text}"
    )


# ---------------------------------------------------------------- 存储


def _row_to_record(row: sqlite3.Row) -> SummaryRecord:
    return SummaryRecord(
        summary_id=row["summary_id"],
        material_id=row["material_id"],
        version=int(row["version"]),
        status=row["status"],
        text=row["text"],
        text_digest=row["text_digest"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        summary_version=row["summary_version"],
        attempt_id=row["attempt_id"],
        error=row["error"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        micros=row["micros"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        scope=row["scope"],
        created_at=int(row["created_at"]),
    )


class SummaryStore:
    """摘要记录存储。建在**事件库同一个 SQLite 文件**里（与内容引用、预算账本同理）。

    默认构造**建表**（与 `contents.py` / `budget.py` 在 `build_stack` 里同一条路），
    但建表发生在**事务之外**：`executescript()` 会先提交当前事务，在事务里顺手建
    表会把外层事务的原子性打断（回滚之后摘要行还在），那样"摘要行与事件同生共死"
    就不成立。回放用 `create=False` 打开：不建表，读不到就当作"这个库没有摘要记录"。
    """

    def __init__(
        self,
        log: Any,
        *,
        clock: Callable[[], int] | None = None,
        create: bool = True,
    ) -> None:
        self._log = log
        self._conn = log._conn
        self._clock = clock or _now_ms
        #: `create=False` 只给**回放**用：读一个历史库不该顺手给它加表。
        #:
        #: 建表必须发生在事务**之外**：`executescript()` 会先提交当前事务
        #: （SQLite 语义），在事务里顺手建表会把外层事务的原子性打断——那样
        #: "摘要行与事件同生共死"就不成立（外层回滚后摘要行还在）。
        self._schema_ready = False
        if create:
            self.ensure_schema()

    # ------------------------------------------------------------ schema

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self._conn.executescript(SCHEMA)
        self._schema_ready = True

    def _require_schema(self) -> None:
        if not self.has_schema():
            raise SummaryStateError(
                "摘要表不存在：这个库还没有摘要记录。写之前先 ensure_schema()"
                "（回放用 create=False 打开时**不建表**，读到的是「没有记录」）"
            )

    def has_schema(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("summary_versions",),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ 写入

    def begin(
        self,
        material: SummaryMaterial,
        *,
        model: str,
        prompt_version: str = PROMPT_VERSION,
        summary_version: str = SUMMARY_VERSION,
        attempt_id: str = "",
        run_id: str = "",
        task_id: str = "",
    ) -> SummaryRecord:
        """登记一次**未结算**的摘要尝试（`pending`），版本 = 同材料 `max+1`。

        为什么先写 pending 再调模型：崩溃恢复时要能回答"这次调用到底发生了没有"。
        没有这一行，恢复只能猜；猜成"没发生"就会偷偷再调一次（PRD §7.1 第 4 条
        禁止的正是这个）。
        """
        if not isinstance(model, str) or not model:
            raise SummaryStateError("摘要记录必须写明模型身份（model 不能为空）")
        if not isinstance(prompt_version, str) or not prompt_version:
            raise SummaryStateError("摘要记录必须写明 prompt 版本")
        if not isinstance(summary_version, str) or not summary_version:
            raise SummaryStateError("摘要记录必须写明摘要版本")
        self._require_schema()
        now = self._clock()
        with self._log.transaction():
            inflight = self._conn.execute(
                "SELECT summary_id FROM summary_versions"
                " WHERE material_id = ? AND status = ?",
                (material.material_id, PENDING),
            ).fetchone()
            if inflight is not None:
                raise SummaryStateError(
                    "同一材料已有未结算的摘要尝试："
                    f"{inflight['summary_id']}（崩溃遗留要先结案，不能并发重试）"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO summary_materials"
                " (material_id, scope, task_id, run_id, bytes, text_digest, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    material.material_id,
                    material.scope,
                    task_id or material.task_id,
                    run_id or material.run_id,
                    material.bytes,
                    material.digest,
                    now,
                ),
            )
            for ordinal, source in enumerate(material.sources):
                self._conn.execute(
                    "INSERT OR IGNORE INTO summary_sources"
                    " (material_id, ordinal, kind, ref, digest) VALUES (?,?,?,?,?)",
                    (material.material_id, ordinal, source.kind, source.ref, source.digest),
                )
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM summary_versions"
                " WHERE material_id = ?",
                (material.material_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            summary_id = jcs_sha256(
                {
                    "material_id": material.material_id,
                    "version": version,
                    "model": model,
                    "prompt_version": prompt_version,
                    "summary_version": summary_version,
                }
            )
            self._conn.execute(
                "INSERT INTO summary_versions"
                " (summary_id, material_id, version, status, text, text_digest, model,"
                "  prompt_version, summary_version, attempt_id, error, input_tokens,"
                "  output_tokens, micros, run_id, task_id, scope, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    summary_id,
                    material.material_id,
                    version,
                    PENDING,
                    None,
                    None,
                    model,
                    prompt_version,
                    summary_version,
                    attempt_id,
                    None,
                    None,
                    None,
                    None,
                    run_id or material.run_id,
                    task_id or material.task_id,
                    material.scope,
                    now,
                ),
            )
            record = self._read_locked(summary_id)
        assert record is not None
        return record

    def complete(
        self,
        record: SummaryRecord,
        text: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        micros: int | None = None,
    ) -> SummaryRecord:
        """把 `pending` 结算成 `ready`：正文与摘要同一次提交。

        正文边界（空白 / 超长）一律**拒绝**，不静默截断——截断会让人以为
        "摘要就这么多"，而实际是"模型给了别的、被我们剪了"。
        """
        if record.status != PENDING:
            raise SummaryStateError(
                f"只有 pending 的摘要能结算，当前是 {record.status}：{record.summary_id}"
            )
        if not isinstance(text, str):
            raise SummaryTooLong(f"摘要正文必须是字符串：{type(text).__name__}")
        if not text.strip():
            raise SummaryEmpty("摘要正文是空白：判失败，回落确定性裁剪")
        if "\x00" in text:
            raise SummaryTooLong("摘要正文含 NUL 字节：拒绝入库")
        size = len(text.encode("utf-8"))
        if size > SUMMARY_MAX_BYTES:
            raise SummaryTooLong(
                f"摘要正文 {size}B 超过上限 {SUMMARY_MAX_BYTES}B：拒绝而不是截断"
            )
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("micros", micros),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise SummaryStateError(f"{name} 必须是非负整数或 None：{value!r}")
        self._require_schema()
        with self._log.transaction():
            changed = self._conn.execute(
                "UPDATE summary_versions SET status = ?, text = ?, text_digest = ?,"
                " input_tokens = ?, output_tokens = ?, micros = ?, error = NULL"
                " WHERE summary_id = ? AND status = ?",
                (
                    READY,
                    text,
                    text_digest(text),
                    input_tokens,
                    output_tokens,
                    micros,
                    record.summary_id,
                    PENDING,
                ),
            ).rowcount
            if changed != 1:
                raise SummaryStateError(
                    f"摘要结算失败（状态已被别人改过）：{record.summary_id}"
                )
            fresh = self._read_locked(record.summary_id)
        assert fresh is not None
        return fresh

    def fail(
        self,
        record: SummaryRecord,
        error: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        micros: int | None = None,
    ) -> SummaryRecord:
        """把 `pending` 结算成 `failed`：原因**有界**，正文保持 NULL。"""
        if record.status != PENDING:
            raise SummaryStateError(
                f"只有 pending 的摘要能结案，当前是 {record.status}：{record.summary_id}"
            )
        text = str(error)
        if len(text.encode("utf-8")) > ERROR_MAX_BYTES:
            text = _clip_bytes(text, ERROR_MAX_BYTES) + "…（已截断）"
        self._require_schema()
        with self._log.transaction():
            changed = self._conn.execute(
                "UPDATE summary_versions SET status = ?, error = ?, text = NULL,"
                " text_digest = NULL, input_tokens = ?, output_tokens = ?, micros = ?"
                " WHERE summary_id = ? AND status = ?",
                (
                    FAILED,
                    text or "（无原因）",
                    input_tokens,
                    output_tokens,
                    micros,
                    record.summary_id,
                    PENDING,
                ),
            ).rowcount
            if changed != 1:
                raise SummaryStateError(
                    f"摘要结案失败（状态已被别人改过）：{record.summary_id}"
                )
            fresh = self._read_locked(record.summary_id)
        assert fresh is not None
        return fresh

    # ------------------------------------------------------------ 查询

    def _read_locked(self, summary_id: str) -> SummaryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM summary_versions WHERE summary_id = ?", (summary_id,)
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def read(self, summary_id: str) -> SummaryRecord | None:
        """按 id 读一条。**不建表**：表格不存在等价于"这个库没有摘要记录"。"""
        if not isinstance(summary_id, str) or not summary_id:
            raise SummaryStateError(f"summary_id 必须是非空字符串：{summary_id!r}")
        if not self.has_schema():
            return None
        return self._read_locked(summary_id)

    def version(self, material_id: str, version: int) -> SummaryRecord | None:
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT * FROM summary_versions WHERE material_id = ? AND version = ?",
            (material_id, int(version)),
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def versions(self, material_id: str) -> list[SummaryRecord]:
        """同一材料的全部版本（version 升序）。"""
        if not self.has_schema():
            return []
        rows = self._conn.execute(
            "SELECT * FROM summary_versions WHERE material_id = ? ORDER BY version",
            (material_id,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def latest(self, material_id: str) -> SummaryRecord | None:
        """同材料最新版本（**任何状态**）。调用方据 status 决定复用 / 结案 / 重试。"""
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT * FROM summary_versions WHERE material_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (material_id,),
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def latest_ready(self, material_id: str) -> SummaryRecord | None:
        """同材料最新 **ready** 版本。历史 Run 引用的是具体 id，不是"最新"。"""
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT * FROM summary_versions WHERE material_id = ? AND status = ?"
            " ORDER BY version DESC LIMIT 1",
            (material_id, READY),
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def pending(self, material_id: str) -> list[SummaryRecord]:
        if not self.has_schema():
            return []
        rows = self._conn.execute(
            "SELECT * FROM summary_versions WHERE material_id = ? AND status = ?"
            " ORDER BY version",
            (material_id, PENDING),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def material(self, material_id: str) -> dict[str, Any] | None:
        """材料台账（审计用）：身份 + 逐条来源。不含渲染文本。"""
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT * FROM summary_materials WHERE material_id = ?", (material_id,)
        ).fetchone()
        if row is None:
            return None
        sources = self._conn.execute(
            "SELECT ordinal, kind, ref, digest FROM summary_sources"
            " WHERE material_id = ? ORDER BY ordinal",
            (material_id,),
        ).fetchall()
        return {
            "material_id": row["material_id"],
            "scope": row["scope"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "bytes": int(row["bytes"]),
            "text_digest": row["text_digest"],
            "created_at": int(row["created_at"]),
            "sources": [
                {
                    "ordinal": int(s["ordinal"]),
                    "kind": s["kind"],
                    "ref": s["ref"],
                    "digest": s["digest"],
                }
                for s in sources
            ],
        }

    def count_for_run(self, run_id: str) -> int:
        """这个 Run 做过几次摘要调用（`RunResult.summary_calls` 的来源）。

        按 **pending + ready + failed** 全算：一条失败的尝试也是一次真实调用
        （预占过、可能计费过）。"没调用过"与"调了但失败"是两件事。
        """
        if not run_id or not self.has_schema():
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM summary_versions WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["n"])

    def summary_count(self) -> int:
        if not self.has_schema():
            return 0
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM summary_versions"
            ).fetchone()["n"]
        )

    def material_count(self) -> int:
        if not self.has_schema():
            return 0
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM summary_materials"
            ).fetchone()["n"]
        )


__all__ = [
    "ERROR_MAX_BYTES",
    "FAILED",
    "ITEM_BYTES",
    "MATERIAL_BYTES",
    "MATERIAL_VERSION",
    "MAX_SOURCES",
    "PENDING",
    "PROMPT_VERSION",
    "READY",
    "STATUSES",
    "SUMMARY_MAX_BYTES",
    "SUMMARY_VERSION",
    "MaterialBlock",
    "MaterialSource",
    "SummaryEmpty",
    "SummaryError",
    "SummaryMaterial",
    "SummaryMaterialError",
    "SummaryRecord",
    "SummaryStateError",
    "SummaryStore",
    "SummaryTooLong",
    "build_material",
    "prompt_for",
    "render_block",
    "text_digest",
]
