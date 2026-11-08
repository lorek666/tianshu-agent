"""写前快照与冲突安全撤销（v2：以 call_id 为证据键）。

上一版的问题是**证据不可信**：
- manifest 在写完之后才落盘，崩溃窗口里没有证据；
- 证据按 path 索引，同一路径多次写会互相覆盖；
- 恢复只看"磁盘现在像不像执行后的样子"，于是文件本来就有相同内容时必然误判。

这一版改成：
1. **写之前**先原子落盘 preimage，条目以 `call_id` 为键，记录 before / expected_after。
2. 恢复只认这条证据：没有它 → 本次调用根本没进入写入阶段 → `none_applied`。
3. 撤销先整体预检（冲突 + blob 是否齐全），再动手；任一不满足，一个字节都不改。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_bytes
from .workspace import Workspace

ABSENT = "absent"
MANIFEST_VERSION = 2


@dataclass
class Change:
    call_id: str
    path: str
    before_state: str  # ABSENT 或 sha256
    expected_after: str  # sha256
    blob_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "path": self.path,
            "before_state": self.before_state,
            "expected_after": self.expected_after,
            "blob_ref": self.blob_ref,
        }


@dataclass
class UndoReport:
    ok: bool
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    missing_blobs: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.blob_dir = root / "blobs"
        self.manifest_dir = root / "snapshots"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    # ---- 证据 ----

    def record_preimage(
        self,
        run_id: str,
        call_id: str,
        rel_path: str,
        before: bytes | None,
        expected_after: str,
    ) -> Change:
        """在真正写文件之前调用，并立即原子落盘。"""
        if before is None:
            change = Change(call_id, rel_path, ABSENT, expected_after, None)
        else:
            digest = hashlib.sha256(before).hexdigest()
            blob = self.blob_dir / digest
            if not blob.exists():
                atomic_write_bytes(blob, before)  # 内容寻址，同内容只存一份
            change = Change(call_id, rel_path, digest, expected_after, str(blob))

        entries = [c for c in self.load(run_id) if c.call_id != call_id]
        entries.append(change)
        payload = json.dumps(
            {"version": MANIFEST_VERSION,
             "changes": [c.to_dict() for c in entries]},
            ensure_ascii=False, indent=2,
        ).encode("utf-8")
        atomic_write_bytes(self.manifest_dir / f"{run_id}.json", payload)
        return change

    def load(self, run_id: str) -> list[Change]:
        path = self.manifest_dir / f"{run_id}.json"
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["changes"] if isinstance(raw, dict) else raw
        return [
            Change(
                call_id=i["call_id"],
                path=i["path"],
                before_state=i["before_state"],
                expected_after=i["expected_after"],
                blob_ref=i.get("blob_ref"),
            )
            for i in items
        ]

    def find(self, run_id: str, call_id: str) -> Change | None:
        for c in self.load(run_id):
            if c.call_id == call_id:
                return c
        return None

    def has_manifest(self, run_id: str) -> bool:
        return (self.manifest_dir / f"{run_id}.json").exists()

    # ---- 撤销 ----

    def undo(self, run_id: str, ws: Workspace) -> UndoReport:
        changes = self.load(run_id)
        if not changes:
            return UndoReport(ok=False, problems=[f"run {run_id} 没有可撤销的变更"])

        report = UndoReport(ok=False)

        # 第一遍：只检查。冲突、blob 缺失都在这里挡住，不动任何文件。
        for c in changes:
            target = ws.resolve(c.path)
            current = (
                ABSENT if not target.exists()
                else hashlib.sha256(target.read_bytes()).hexdigest()
            )
            if current != c.expected_after:
                report.conflicts.append(
                    f"{c.path}：当前内容与 Agent 写完时不一致"
                    f"（{current[:12]}… ≠ {c.expected_after[:12]}…），拒绝覆盖"
                )
            if c.before_state != ABSENT:
                if not c.blob_ref or not Path(c.blob_ref).exists():
                    report.missing_blobs.append(f"{c.path}：写前内容丢失，无法还原")

        if report.conflicts or report.missing_blobs:
            report.problems.append("预检未通过，未做任何修改")
            return report

        # 第二遍：确认可逆，执行
        for c in changes:
            target = ws.resolve(c.path)
            if c.before_state == ABSENT:
                target.unlink(missing_ok=True)
                report.deleted.append(c.path)
            else:
                assert c.blob_ref is not None
                atomic_write_bytes(target, Path(c.blob_ref).read_bytes())
                report.restored.append(c.path)

        report.ok = not report.problems
        return report
