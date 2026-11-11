"""内容引用：大结果的有界摘要 + 可分页读取的引用（PRD §5.2.3）。

## 为什么需要这一层

上下文编译器早就会把超过阈值（默认 8 KiB）的工具输出换成占位、把原文写进
内容寻址 blob（`context.py` 的 spills + `loop._persist_spills`）。但占位只说了
"已落盘"，**没有可用引用**、没有读取工具、也没有"谁能读"的判定。结果是：

- "大结果有界"只成立一半（有界了，但取不回来）；
- 「分页读取引用同样验证任务可见范围」这条要求**完全没有实现**（PRD §2.3 也写明：
  工具自己截断的内容不会因 spills 被恢复）。

本模块补的就是这三件：引用形状、任务可见范围、分页读取（含摘要复核）。

## 三个刻意设计

1. **引用里自带摘要**：`content://sha256:<64hex>`。读的时候重算一遍 sha256，
   不符就拒绝——blob 目录是普通文件，能改它的人能改内容；引用本身是**校验和**，
   不是"一个文件名"。这和事件 `payload_ref + payload_hash` 是同一条思路。
2. **可见范围按 `(digest, task_id)` 授权**：内容寻址会**去重**（同一份内容多个
   任务引用只有一行对象），所以可见性不能记在对象上——那会让一个任务看到另一个
   任务的原文。授权在 spill 发生时由主循环按当前 Run 的 task 写入。
3. **不可见与不存在分开报**：`ContentNotVisible`（这个任务没有授权）与
   `ContentMissing`（有授权但 blob 不在了）是两件事，混成一个错误会让"权限
   问题"看起来像"文件丢了"，也会让排障去错地方。

## 不说的话

- 不做保留期 / 垃圾回收（属备份与保留期轮，已登记）；这里只写不删。
- 不做跨机器同步：blob 目录是本机的，引用只在同一台机器同一套数据根内有效。
- 不做"透明恢复工具自己截断的内容"：`read_text` 截断的那部分**从来没有**落盘，
  这里也没有它。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError

#: 引用的 scheme。与 `resources.py` 的 `content` 资源种类一致。
REF_SCHEME = "content"
REF_PREFIX = f"{REF_SCHEME}://sha256:"

#: 一次分页最多返回多少字节。上界存在是为了"引用很大"不会变成"响应很大"。
MAX_PAGE = 65536

#: 默认分页大小（读一屏够模型看一段，又不至于把 8 KiB 的上下文预算一次吃光）。
DEFAULT_PAGE = 16384

_DIGEST = re.compile(r"^[0-9a-f]{64}$")

SCHEMA = """
-- 内容寻址对象清单：一行一内容（去重）。size 是落盘时的字节数。
CREATE TABLE IF NOT EXISTS content_objects (
    digest     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- 可见范围：**按任务**授权，不记在对象上（见模块注释第 2 条）。
CREATE TABLE IF NOT EXISTS content_grants (
    digest     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    run_id     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (digest, task_id)
);
"""


class ContentError(TianshuError):
    """引用形状不对 / 分页参数不对。这是**调用方给错了**。"""


class ContentNotVisible(TianshuError):
    """这个任务没有这份内容的授权（PRD §5.2.3「验证任务可见范围」）。"""


class ContentMissing(TianshuError):
    """有授权，但内容不在存储里（blob 被删 / 数据根换了）。"""


class ContentDigestMismatch(TianshuError):
    """重算的 sha256 与引用不符：内容被改过，或引用是编的。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def make_ref(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST.match(digest):
        raise ContentError(f"内容摘要必须是 64 位小写十六进制：{digest!r}")
    return f"{REF_PREFIX}{digest}"


def parse_ref(raw: object) -> str:
    """`content://sha256:<64hex>` → digest。畸形一律抛错，不做模糊匹配。"""
    if not isinstance(raw, str) or not raw:
        raise ContentError(f"内容引用必须是非空字符串：{raw!r}")
    if not raw.startswith(REF_PREFIX):
        raise ContentError(
            f"内容引用必须以 {REF_PREFIX}<64hex> 开头：{raw!r}"
        )
    digest = raw[len(REF_PREFIX) :]
    if not _DIGEST.match(digest):
        raise ContentError(f"内容引用里的摘要形状不合法：{raw!r}")
    return digest


@dataclass(frozen=True)
class ContentPage:
    """一次分页读取的结果。`next_offset is None` 表示读完了。"""

    digest: str
    data: str
    offset: int
    next_offset: int | None
    total_size: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "ref": make_ref(self.digest),
            "digest": self.digest,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "total_size": self.total_size,
            "bytes": len(self.data.encode("utf-8")),
            "data": self.data,
        }


class ContentStore:
    """内容引用 + 可见范围的存储。建在**事件库同一个 SQLite 文件**里。

    与预算账本同样的理由：不制造第二个事实源；事务与事件同生共死。
    """

    def __init__(self, log: Any, *, blob_dir: Path | str | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self._log = log
        self._conn = log._conn
        self._blob_dir = Path(blob_dir) if blob_dir is not None else None
        self._clock = clock or _now_ms
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------ 写入

    def record(
        self, digest: str, data: bytes, *, task_id: str, run_id: str = ""
    ) -> bool:
        """登记一份内容并授权给 `task_id`。**幂等**（同 digest 只多一行授权）。

        返回 True 表示这份内容第一次出现（对象行是新写的）。blob 本身由调用方
        写（`loop._persist_spills` 用与快照/事件同一个原子写），这里只登记。
        """
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            raise ContentError(f"内容摘要形状不合法：{digest!r}")
        if not isinstance(data, (bytes, bytearray)):
            raise ContentError(f"内容必须是字节：{type(data).__name__}")
        if not isinstance(task_id, str) or not task_id:
            raise ContentError("内容授权必须绑定任务（task_id 不能为空）")
        now = self._clock()
        created = False
        with self._log.transaction():
            existing = self._conn.execute(
                "SELECT digest FROM content_objects WHERE digest = ?", (digest,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO content_objects (digest, size, created_at)"
                    " VALUES (?,?,?)",
                    (digest, len(data), now),
                )
                created = True
            self._conn.execute(
                "INSERT OR IGNORE INTO content_grants (digest, task_id, run_id,"
                " created_at) VALUES (?,?,?,?)",
                (digest, task_id, run_id, now),
            )
        return created

    # ------------------------------------------------------------ 查询

    def visible(self, digest: str, task_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM content_grants WHERE digest = ? AND task_id = ?",
            (digest, task_id),
        ).fetchone()
        return row is not None

    def object(self, digest: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT digest, size, created_at FROM content_objects WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        return {
            "digest": row["digest"],
            "size": row["size"],
            "created_at": row["created_at"],
        }

    def created_count(self) -> int:
        """已登记的内容份数（诊断用；不代表保留期策略）。"""
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM content_objects"
        ).fetchone()["n"]

    # ------------------------------------------------------------ 读取

    def read(
        self,
        ref: object,
        *,
        task_id: str,
        offset: int = 0,
        length: int = DEFAULT_PAGE,
    ) -> ContentPage:
        """按任务可见范围读一页。**三步都不可省**：授权 → 对象 → 摘要复核。"""
        digest = parse_ref(ref)
        if not isinstance(task_id, str) or not task_id:
            raise ContentError("读取内容必须提供任务身份")
        for name, value in (("offset", offset), ("length", length)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContentError(f"{name} 必须是整数：{value!r}")
        if offset < 0:
            raise ContentError(f"offset 不能为负：{offset}")
        if length <= 0 or length > MAX_PAGE:
            raise ContentError(f"length 必须在 1..{MAX_PAGE}：{length}")

        # 1) 授权先判：不可见的内容不该让调用方从错误信息里探出"它存在不存在"。
        if not self.visible(digest, task_id):
            raise ContentNotVisible(
                f"当前任务没有读取这份内容的授权：{make_ref(digest)}"
            )

        # 2) 对象行：有授权但没有对象 = 存储不一致（不是权限问题）。
        entry = self.object(digest)
        if entry is None:
            raise ContentMissing(f"内容未登记：{make_ref(digest)}")

        # 3) 读 blob 并**重算摘要**：引用是校验和，不是文件名。
        data = self._read_blob(digest)
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ContentDigestMismatch(
                f"内容摘要不符：引用 {digest}，实际 {actual}"
                "（blob 被改过，或引用与内容不是一对）"
            )
        total = len(data)
        if offset > total:
            raise ContentError(f"offset 越界：{offset} > 内容长度 {total}")
        page = data[offset : offset + length]
        consumed = len(page)
        try:
            text = page.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 分页边界可能切在一个多字节字符中间：把这一页收到**字符边界**上，
            # 让下一次从整字符开始（否则每次分页都会因为半截 UTF-8 而失败）。
            if not exc.reason.startswith("unexpected end"):
                raise ContentError(
                    f"内容不是 UTF-8 文本（offset={offset}）：{exc}"
                ) from exc
            consumed = exc.start
            if consumed <= 0:
                raise ContentError(
                    f"分页长度 {length} 切在多字节字符中间（offset={offset}），"
                    "请增大 length"
                ) from exc
            text = page[:consumed].decode("utf-8")
        next_offset = offset + consumed
        if next_offset >= total:
            next_offset_value: int | None = None
        else:
            next_offset_value = next_offset
        return ContentPage(
            digest=digest,
            data=text,
            offset=offset,
            next_offset=next_offset_value,
            total_size=total,
        )

    # ------------------------------------------------------------ 内部

    @property
    def blob_dir(self) -> Path:
        if self._blob_dir is None:
            raise TianshuError(
                "内容存储没有 blob 目录：读不到内容引用"
                "（ContentStore(blob_dir=...) 未配置）"
            )
        return self._blob_dir

    def _read_blob(self, digest: str) -> bytes:
        path = self.blob_dir / digest
        if not path.is_file():
            raise ContentMissing(f"内容文件不存在：{make_ref(digest)}")
        return path.read_bytes()


__all__ = [
    "ContentDigestMismatch",
    "ContentError",
    "ContentMissing",
    "ContentNotVisible",
    "ContentPage",
    "ContentStore",
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "REF_PREFIX",
    "REF_SCHEME",
    "SCHEMA",
    "make_ref",
    "parse_ref",
]
