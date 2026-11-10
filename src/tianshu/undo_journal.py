"""撤销意图日志（undo journal）：跨多文件撤销的可续做边界。

问题（DEBUG-M0M1「宣称 4」）：撤销逐文件原子、逐文件记录，但**中途崩溃**会留下
「改了一半、日志看起来完好」的状态。恢复与撤销都不知道还有几个文件没动过，
下一次 `undo` 要么从头再来（重复删除/还原是数据损坏风险），要么假装没发生。

做法是**写前意图日志**，不是假装两阶段提交：

1. 动手前把本次撤销的完整计划原子落盘到 `artifacts/undo/<run_id>.json`：
   `status="in_progress"`、`done` 为空。写不成就不动手。
2. 每完成一个动作，原子重写 journal（`done` 追加一条，带 `post_state`）。
3. 全部完成才把 `status` 改成 `completed`；**之后**才允许写 `undo.completed`
   事件（事件的补写同样是幂等的，见 `undo.apply_undo`）。
4. 崩溃后：`done` 项当前磁盘状态必须与 `post_state` 一致，否则拒绝（不猜）；
   剩余项必须仍是写后状态（用户改过就拒绝），然后从断点继续。**绝不从头再来**。

journal 是**证据**不是事实源：它只描述「上一次撤销做到哪一步」，文件当前状态
才是判据。所以它只记录撤销意图与进度，不代替快照清单。

模块边界：`recovery.py` 属另一工作流，本模块不能改它；对外只提供
`outstanding(data_root)`（给 recover 扫「未完成撤销」用于阻断）与
`resume(stack, run_id)`（续做/补写完成事件）。`SnapshotStore.undo` 与
`undo.apply_undo` 内部接好 journal，调用方不需要知道文件格式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .fsutil import atomic_write_bytes

JOURNAL_VERSION = 1
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
_STATUSES = frozenset({STATUS_IN_PROGRESS, STATUS_COMPLETED})

ACTION_RESTORE = "restore"
ACTION_DELETE = "delete"
_ACTIONS = frozenset({ACTION_RESTORE, ACTION_DELETE})

ABSENT = "absent"


class UndoJournalError(TianshuError):
    """undo journal 的域错误基类。"""


class UndoJournalCorrupt(UndoJournalError):
    """journal 读不懂/自相矛盾。

    读不懂就不能猜：恢复要阻断对应工作区，撤销要拒绝续做。当成空 journal
    继续 —— 那会把「改了一半」重放成「又从零开始改一遍」。
    """


class UndoJournalConflict(UndoJournalError):
    """journal 与磁盘现状对不上（用户改过 / 动作没落成 / 范围不符）。

    这类冲突必须**零修改**地拒绝：盘上的字节已经和 journal 记录不是同一个
    世界，按记录继续写就是把用户后来的修改覆盖掉。
    """


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise UndoJournalCorrupt(message)


def _check_root(root: Any, where: str) -> str:
    _require(isinstance(root, str) and bool(root), f"{where}：root 必须是非空字符串")
    _require(Path(root).is_absolute(), f"{where}：root 必须是绝对路径：{root!r}")
    return str(Path(root))


def _check_rel(rel: Any, where: str) -> str:
    _require(isinstance(rel, str) and bool(rel), f"{where}：path 必须是非空字符串")
    if "\\" in rel:
        raise UndoJournalCorrupt(f"{where}：path 必须是 / 分隔：{rel!r}")
    pure = Path(rel)
    _require(not pure.is_absolute(), f"{where}：path 必须是相对路径：{rel!r}")
    parts = rel.split("/")
    _require(
        not any(part in ("", ".", "..") for part in parts),
        f"{where}：path 不允许空段 / . / ..：{rel!r}",
    )
    return rel


@dataclass(frozen=True)
class JournalItem:
    """一个撤销动作（执行顺序：对调用序列取逆序）。"""

    root: str
    path: str
    call_id: str
    before_state: str  # sha256 或 "absent"
    expected_after: str  # 动手前磁盘应当是什么（agent 写完的状态）
    action: str  # restore | delete

    @property
    def key(self) -> tuple[str, str]:
        return (self.root, self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "path": self.path,
            "call_id": self.call_id,
            "before_state": self.before_state,
            "expected_after": self.expected_after,
            "action": self.action,
        }

    @classmethod
    def from_dict(cls, raw: Any, where: str) -> JournalItem:
        _require(isinstance(raw, dict), f"{where}：条目必须是对象")
        item = cls(
            root=_check_root(raw.get("root"), where),
            path=_check_rel(raw.get("path"), where),
            call_id=raw.get("call_id"),
            before_state=raw.get("before_state"),
            expected_after=raw.get("expected_after"),
            action=raw.get("action"),
        )
        _require(
            isinstance(item.call_id, str) and bool(item.call_id),
            f"{where}：call_id 必须是非空字符串",
        )
        _require(
            isinstance(item.before_state, str) and bool(item.before_state),
            f"{where}：before_state 必须是非空字符串",
        )
        _require(
            isinstance(item.expected_after, str) and bool(item.expected_after),
            f"{where}：expected_after 必须是非空字符串",
        )
        _require(item.action in _ACTIONS, f"{where}：未知动作 {item.action!r}")
        if item.action == ACTION_DELETE:
            _require(
                item.before_state == ABSENT,
                f"{where}：删除动作的 before_state 必须是 absent",
            )
        else:
            _require(
                item.before_state != ABSENT,
                f"{where}：还原动作必须有写前内容（before_state 不能是 absent）",
            )
        return item


@dataclass(frozen=True)
class DoneItem:
    """一个已完成的撤销动作。`post_state` 是动作完成后磁盘上的实际状态。"""

    item: JournalItem
    post_state: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.item.to_dict(), "post_state": self.post_state}


@dataclass(frozen=True)
class UndoJournal:
    run_id: str
    control_run: str
    items: tuple[JournalItem, ...]
    done: tuple[DoneItem, ...]
    status: str
    version: int = JOURNAL_VERSION

    @property
    def completed(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def remaining(self) -> tuple[JournalItem, ...]:
        return self.items[len(self.done):]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "control_run": self.control_run,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "done": [item.to_dict() for item in self.done],
        }

    @classmethod
    def from_dict(cls, raw: Any, *, run_id: str) -> UndoJournal:
        _require(isinstance(raw, dict), "journal 顶层必须是对象")
        version = raw.get("version")
        if version != JOURNAL_VERSION:
            raise UndoJournalCorrupt(
                f"undo journal 版本不支持：{version!r}（本程序支持 "
                f"{JOURNAL_VERSION}）。字段语义可能不同，拒绝按猜的语义续做"
            )
        _require(raw.get("run_id") == run_id, "journal 的 run_id 与文件名不一致")
        control_run = raw.get("control_run", "")
        _require(isinstance(control_run, str), "control_run 必须是字符串")
        status = raw.get("status")
        _require(status in _STATUSES, f"journal 状态未知：{status!r}")

        raw_items = raw.get("items")
        _require(isinstance(raw_items, list), "journal 的 items 必须是数组")
        items = tuple(
            JournalItem.from_dict(entry, f"items[{i}]")
            for i, entry in enumerate(raw_items)
        )
        _require(bool(items), "journal 不能没有 items")

        raw_done = raw.get("done")
        _require(isinstance(raw_done, list), "journal 的 done 必须是数组")
        _require(len(raw_done) <= len(items), "done 比 items 还长：journal 已损坏")
        done: list[DoneItem] = []
        for i, entry in enumerate(raw_done):
            _require(isinstance(entry, dict), f"done[{i}] 必须是对象")
            post_state = entry.get("post_state")
            _require(
                isinstance(post_state, str) and bool(post_state),
                f"done[{i}]：post_state 必须是非空字符串",
            )
            recorded = JournalItem.from_dict(entry, f"done[{i}]")
            expected = items[i]
            _require(
                recorded == expected,
                f"done[{i}] 与 items[{i}] 不一致：journal 自相矛盾",
            )
            _require(
                post_state == expected.before_state,
                f"done[{i}]：post_state 与 before_state 不一致",
            )
            done.append(DoneItem(item=expected, post_state=post_state))
        if status == STATUS_COMPLETED:
            _require(
                len(done) == len(items),
                "journal 标 completed 但还有未完成的动作",
            )
        return cls(
            run_id=run_id,
            control_run=control_run,
            items=items,
            done=tuple(done),
            status=status,
            version=JOURNAL_VERSION,
        )


class JournalStore:
    """`artifacts/undo/<run_id>.json` 的读写。所有写入都是原子的。"""

    def __init__(self, artifacts_root: Path) -> None:
        self.root = Path(artifacts_root)
        self.dir = self.root / "undo"

    def path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def load(self, run_id: str) -> UndoJournal | None:
        path = self.path(run_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise UndoJournalCorrupt(
                f"undo journal 无法解析：{path}（{type(exc).__name__}: {exc}）。"
                "这份证据不能作为续做依据，也不能当成空 journal 从头撤销。"
            ) from exc
        return UndoJournal.from_dict(raw, run_id=run_id)

    def _save(self, journal: UndoJournal) -> None:
        payload = json.dumps(
            journal.to_dict(), ensure_ascii=False, indent=2
        ).encode("utf-8")
        atomic_write_bytes(self.path(journal.run_id), payload)

    def begin(
        self,
        run_id: str,
        *,
        control_run: str,
        items: tuple[JournalItem, ...],
    ) -> UndoJournal:
        """动手前落盘完整计划。已有 journal 时拒绝（续做走 `load`＋`record_done`）。"""
        if self.path(run_id).exists():
            raise UndoJournalConflict(
                f"{run_id} 已有未清理的 undo journal：拒绝覆盖已有意图日志"
            )
        journal = UndoJournal(
            run_id=run_id,
            control_run=control_run,
            items=items,
            done=(),
            status=STATUS_IN_PROGRESS,
        )
        self._save(journal)
        return journal

    def record_done(
        self, journal: UndoJournal, index: int, post_state: str
    ) -> UndoJournal:
        """记录第 `index` 个动作已经完成。必须在动作生效之后调用。"""
        if index != len(journal.done):
            raise UndoJournalConflict(
                f"journal 只能按顺序追加 done：期望 {len(journal.done)}，收到 {index}"
            )
        item = journal.items[index]
        updated = UndoJournal(
            run_id=journal.run_id,
            control_run=journal.control_run,
            items=journal.items,
            done=(*journal.done, DoneItem(item=item, post_state=post_state)),
            status=journal.status,
        )
        self._save(updated)
        return updated

    def complete(self, journal: UndoJournal) -> UndoJournal:
        if len(journal.done) != len(journal.items):
            raise UndoJournalConflict(
                f"还有 {len(journal.items) - len(journal.done)} 个动作未完成，"
                "不能把 journal 标成 completed"
            )
        updated = UndoJournal(
            run_id=journal.run_id,
            control_run=journal.control_run,
            items=journal.items,
            done=journal.done,
            status=STATUS_COMPLETED,
        )
        self._save(updated)
        return updated

    def set_control_run(self, journal: UndoJournal, control_run: str) -> UndoJournal:
        updated = UndoJournal(
            run_id=journal.run_id,
            control_run=control_run,
            items=journal.items,
            done=journal.done,
            status=journal.status,
        )
        self._save(updated)
        return updated


@dataclass(frozen=True)
class OutstandingUndo:
    """recover 侧看到的「未完成撤销」。

    `corrupt=True` 时 `roots` 为空：journal 读不懂就不知道它动过哪些目录。
    recover 仍应阻断调用方显式给出的 workspace（`recover --workspace` 本来就有），
    并把 `journal_path` 写进报告供人处置。
    """

    run_id: str
    journal_path: Path
    status: str  # in_progress | completed | corrupt
    control_run: str = ""
    roots: tuple[str, ...] = ()
    total: int = 0
    done_count: int = 0
    remaining: tuple[JournalItem, ...] = ()
    corrupt: bool = False
    needs_completion_event: bool = False
    error: str = ""

    @property
    def resumable(self) -> bool:
        return not self.corrupt

    @property
    def done(self) -> int:
        return self.done_count


def _artifacts_root(data_root: Path) -> Path:
    return Path(data_root) / "artifacts"


def outstanding(data_root: Path, *, log: Any = None) -> list[OutstandingUndo]:
    """扫出所有未完成的撤销（给 recover 报告 + 阻断用）。

    - `in_progress`：崩溃/中断的撤销，必须续做；
    - `corrupt`：读不懂的 journal，拒绝续做，交给人处置；
    - 传了 `log` 时，`completed` 但缺少 `undo.completed` 事件的 journal 也会列出
      （`needs_completion_event=True`，只需幂等补写事件，不需要动文件）。

    没有 `log` 时无法判断完成事件是否缺失，completed 的不列入 —— 它们对
    「是否还有文件要改」没有影响，不需要阻断。
    """
    store = JournalStore(_artifacts_root(data_root))
    if not store.dir.is_dir():
        return []
    found: list[OutstandingUndo] = []
    for path in sorted(store.dir.glob("*.json")):
        run_id = path.stem
        try:
            journal = store.load(run_id)
        except UndoJournalCorrupt as exc:
            found.append(
                OutstandingUndo(
                    run_id=run_id,
                    journal_path=path,
                    status="corrupt",
                    corrupt=True,
                    error=str(exc),
                )
            )
            continue
        if journal is None:  # 读的间隙被删了：按不存在处理
            continue
        roots = tuple(sorted({item.root for item in journal.items}))
        base = OutstandingUndo(
            run_id=run_id,
            journal_path=path,
            status=journal.status,
            control_run=journal.control_run,
            roots=roots,
            total=len(journal.items),
            done_count=len(journal.done),
            remaining=journal.remaining,
        )
        if journal.status == STATUS_IN_PROGRESS:
            found.append(base)
            continue
        # completed：只有当完成事件缺失时才需要人再跑一次（补写事件）。
        if log is None:
            continue
        try:
            event_present = any(
                event.type == "undo.completed" for event in log.read_run(run_id)
            )
        except TianshuError:
            # 事件日志读不出来：不能据此断言"事件存在"，列为待补写更安全。
            event_present = False
        if not event_present:
            found.append(
                OutstandingUndo(
                    run_id=base.run_id,
                    journal_path=base.journal_path,
                    status=base.status,
                    control_run=base.control_run,
                    roots=base.roots,
                    total=base.total,
                    done_count=base.done_count,
                    remaining=base.remaining,
                    needs_completion_event=True,
                )
            )
    return found


def read(data_root: Path, run_id: str) -> UndoJournal | None:
    """读一个 run 的 journal（recover 想拿明细时用）。损坏抛 `UndoJournalCorrupt`。"""
    return JournalStore(_artifacts_root(data_root)).load(run_id)


def resume(stack: Any, run_id: str, *, scope: Path | None = None) -> Any:
    """续做一个未完成的撤销；journal 已完成但事件缺失时幂等补写事件。

    返回 `undo.UndoOutcome`。实现复用 `undo.apply_undo`（它内部先看 journal，
    再决定是续做还是补写），所以 CLI、UI、recover 走的是**同一条**路径，
    不存在第二套续做逻辑。
    """
    from .undo import apply_undo  # 延迟导入：undo 依赖本模块，顶层导入会成环

    return apply_undo(stack, run_id, scope=scope)


__all__ = [
    "ABSENT",
    "ACTION_DELETE",
    "ACTION_RESTORE",
    "DoneItem",
    "JOURNAL_VERSION",
    "JournalItem",
    "JournalStore",
    "OutstandingUndo",
    "STATUS_COMPLETED",
    "STATUS_IN_PROGRESS",
    "UndoJournal",
    "UndoJournalConflict",
    "UndoJournalCorrupt",
    "UndoJournalError",
    "outstanding",
    "read",
    "resume",
]
