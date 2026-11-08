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
from collections.abc import Callable
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

    def init_run(self, run_id: str) -> None:
        """run 启动时创建空清单。

        这样「清单不存在」才能被解释成异常（被清理/损坏），
        而不是「这个 run 什么都没写」——后者会让恢复把有副作用的 intent
        判成 none_applied。
        """
        path = self.manifest_dir / f"{run_id}.json"
        if path.exists():
            return
        payload = json.dumps(
            {"version": MANIFEST_VERSION, "changes": []},
            ensure_ascii=False, indent=2,
        ).encode("utf-8")
        atomic_write_bytes(path, payload)

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

    def undo(
        self,
        run_id: str,
        ws: Workspace,
        *,
        guard: Callable[[], None] | None = None,
    ) -> UndoReport:
        """把该 run 的变更撤回。

        三条规矩：
        1. 撤销也是写操作，必须先过 guard（阻断检查）。
        2. 同一个文件被写过多次时，按**调用逆序**处理——正序检查会自相冲突。
        3. 预检阶段用内存模拟最终状态，全部通过才动手。
        """
        if guard is not None:
            guard()

        changes = self.load(run_id)
        if not changes:
            return UndoReport(ok=False, problems=[f"run {run_id} 没有可撤销的变更"])

        report = UndoReport(ok=False)

        # ---- 预检：按逆序模拟，不碰磁盘 ----
        disk_state: dict[str, str] = {}
        for c in changes:
            target = ws.resolve(c.path)
            disk_state[c.path] = (
                ABSENT if not target.exists()
                else hashlib.sha256(target.read_bytes()).hexdigest()
            )

        simulated = dict(disk_state)
        for c in reversed(changes):
            current = simulated[c.path]
            if current != c.expected_after:
                report.conflicts.append(
                    f"{c.path}：当前内容与 Agent 写完时不一致"
                    f"（{current[:12]}… ≠ {c.expected_after[:12]}…），拒绝覆盖"
                )
                continue
            simulated[c.path] = c.before_state
            if c.before_state != ABSENT:
                if not c.blob_ref or not Path(c.blob_ref).exists():
                    report.missing_blobs.append(f"{c.path}：写前内容丢失，无法还原")

        if report.conflicts or report.missing_blobs:
            report.problems.append("预检未通过，未做任何修改")
            return report

        # ---- 执行：同样按逆序 ----
        for c in reversed(changes):
            target = ws.resolve(c.path)
            if c.before_state == ABSENT:
                target.unlink(missing_ok=True)
                report.deleted.append(c.path)
            else:
                assert c.blob_ref is not None
                atomic_write_bytes(target, Path(c.blob_ref).read_bytes())
                report.restored.append(c.path)

        report.ok = True
        return report
