"""写前快照与冲突安全撤销。

要回答的问题是：**Agent 改坏了东西，能不能撤回？撤回会不会踩掉用户后来的修改？**

两条规矩：
1. 快照必须在**写入之前**取。写完再取等于没取——那一刻原内容已经没了。
2. 撤销前必须校验当前文件仍等于 Agent 写完的版本。用户改过就报冲突，**绝不覆盖**。

存储布局（内容寻址，重复内容自动去重）：
    <root>/blobs/<sha256>              写前内容
    <root>/snapshots/<run_id>.json     变更清单
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .workspace import Workspace

ABSENT = "absent"


@dataclass
class Change:
    path: str  # 相对 workspace
    before_state: str  # ABSENT 或 sha256
    after_state: str  # sha256
    blob_ref: str | None = None  # 写前内容的 blob 路径

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "blob_ref": self.blob_ref,
        }


@dataclass
class UndoReport:
    ok: bool
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.blob_dir = root / "blobs"
        self.manifest_dir = root / "snapshots"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    # ---- 采集 ----

    def capture(self, run_id: str, rel_path: str, before: bytes | None) -> Change | None:
        """写入前调用。返回 Change（含写前 blob 引用）；after_state 由调用方补。"""
        if before is None:
            return Change(path=rel_path, before_state=ABSENT, after_state="")
        digest = hashlib.sha256(before).hexdigest()
        blob = self.blob_dir / digest
        if not blob.exists():
            blob.write_bytes(before)  # 内容寻址：同内容只存一份
        return Change(
            path=rel_path, before_state=digest, after_state="", blob_ref=str(blob)
        )

    def record(self, run_id: str, change: Change) -> None:
        """把一条变更写入该 run 的清单。重复路径以最后一次为准。"""
        items = [c for c in self.load(run_id) if c.path != change.path]
        items.append(change)
        path = self.manifest_dir / f"{run_id}.json"
        path.write_text(
            json.dumps([c.to_dict() for c in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, run_id: str) -> list[Change]:
        path = self.manifest_dir / f"{run_id}.json"
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Change(**item) for item in raw]

    # ---- 撤销 ----

    def undo(self, run_id: str, ws: Workspace) -> UndoReport:
        """把该 run 的变更全部撤回。先整体校验冲突，再动手——不留半撤状态。"""
        changes = self.load(run_id)
        if not changes:
            return UndoReport(ok=False, problems=[f"run {run_id} 没有可撤销的变更"])

        report = UndoReport(ok=False)

        # 第一遍：只检查，不改。任何冲突都整体拒绝。
        for c in changes:
            target = ws.resolve(c.path)
            if not target.exists():
                current = ABSENT
            else:
                current = hashlib.sha256(target.read_bytes()).hexdigest()

            if current != c.after_state:
                report.conflicts.append(
                    f"{c.path}：当前内容与 Agent 写完时不一致"
                    f"（磁盘 {current[:12]}… ≠ 记录 {c.after_state[:12]}…），"
                    f"说明之后被改过，拒绝覆盖"
                )

        if report.conflicts:
            report.problems.append("检测到冲突，未做任何修改")
            return report

        # 第二遍：确认无冲突，执行还原
        for c in changes:
            target = ws.resolve(c.path)
            if c.before_state == ABSENT:
                target.unlink(missing_ok=True)
                report.deleted.append(c.path)
            else:
                if not c.blob_ref or not Path(c.blob_ref).exists():
                    report.problems.append(f"{c.path}：写前内容丢失，无法还原")
                    continue
                target.write_bytes(Path(c.blob_ref).read_bytes())
                report.restored.append(c.path)

        report.ok = not report.problems
        return report


__all__ = ["SnapshotStore", "Change", "UndoReport", "ABSENT", "TianshuError"]
