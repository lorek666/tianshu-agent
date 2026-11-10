"""写前快照与冲突安全撤销（v3：证据键 = (workspace 根, 相对路径)）。

上一版的问题是**证据不可信**：
- manifest 在写完之后才落盘，崩溃窗口里没有证据；
- 证据按 path 索引，同一路径多次写会互相覆盖；
- 恢复只看"磁盘现在像不像执行后的样子"，于是文件本来就有相同内容时必然误判。

v2 把证据改成"写之前落盘、以 call_id 为键"，但**键里只有 workspace 相对路径**。
一个 Run 可以绑多个 workspace（`Task.workspaces` 是数组），两个 workspace 下的
`a.md` 会撞成同一条证据，而 undo 又只拿一个 ws 去 resolve——撤销就写进了错误
的目录。那是用户可感知的数据破坏，不是理论问题。

v3 的证据键是 **(workspace 根, 相对路径)**：

1. `Change.root` 记录**写入当时**的 workspace 根（绝对、已规范化），
   `Change.path` 仍是 `/` 分隔的相对路径。撤销按每条记录自己的 root 落盘，
   不再依赖调用方传进来的那一个目录。
2. 为什么不用 inode（`ResourceIdentity` 的 `st_dev:st_ino`）当键：inode 在
   备份恢复、目录搬运、跨机器之后都会变，而证据要能活过这些事；memfs 后端
   根本没有 inode。证据要的是"人还能照着找回去"，不是"当下唯一"。
3. **旧清单（v2，没有 root）一律拒绝撤销**。没有 root 就无法确定该写哪个目录，
   任何"用传进来的那个 workspace 顶上"的做法都是在猜——宁可撤销失败，也不能
   写错目录。恢复（recovery）仍可工作，因为它必须显式传 workspace 且只陈述
   "在这个目录里看到了什么"。

其余规矩不变：写前原子落盘、撤销先整体预检、任一冲突一个字节都不改。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import SnapshotCorrupt, TianshuError, ToolNotAllowed
from .fsutil import atomic_write_bytes
from .sandbox import current_backend
from .workspace import Workspace

ABSENT = "absent"
#: 3 起每条证据都带 workspace 根。旧清单（2）仍可读，但不可撤销。
MANIFEST_VERSION = 3

#: 本程序认得的历史版本。更高的版本说明清单是更新的程序写的，字段语义可能
#: 已经变了——宁可拒绝读，也不要按旧语义解释新证据。
_READABLE_VERSIONS = (2, 3)


def _clean_root(root: str | Path) -> str:
    """workspace 根一律规范化成绝对路径字符串。"""
    raw = str(root)
    if not raw:
        raise TianshuError("快照证据缺少 workspace 根：拒绝生成没有归属的写入证据")
    return str(Path(raw).resolve())


def _clean_rel(rel_path: str) -> str:
    """证据里的路径一律是 `/` 分隔的相对路径（`Workspace.relative` 的产物）。

    故意不接受绝对路径和 `..`：manifest 是撤销时的落盘依据，里面存一条可以
    指到 workspace 外面的路径，等于把越界写伪装成合法证据。反斜杠同样拒绝——
    Windows 上它也是分隔符，而证据只存 `/`，接受它就有了第二套路径语义。
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise TianshuError("快照证据的路径不能为空")
    if "\\" in rel_path:
        raise TianshuError(
            f"快照证据的路径必须是 `/` 分隔：{rel_path!r}"
            "（Windows 反斜杠不接受，避免第二套路径语义）"
        )
    pure = PurePosixPath(rel_path)
    if pure.is_absolute():
        raise TianshuError(f"快照证据只接受 workspace 相对路径：{rel_path!r}")
    if any(part in ("..", ".") for part in pure.parts):
        raise TianshuError(f"快照证据的路径不允许 . / ..：{rel_path!r}")
    return pure.as_posix()


@dataclass
class Change:
    """一条写入证据。键是 `(root, path)`，`call_id` 是同一次调用内的去重键。"""

    call_id: str
    root: str | None  # None = 旧格式（v2 清单），无法确定写入目录
    path: str
    before_state: str  # ABSENT 或 sha256
    expected_after: str  # sha256 或 ABSENT（删除）
    blob_ref: str | None = None

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.root, self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "root": self.root,
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
    roots: list[str] = field(default_factory=list)  # 本次实际涉及的 workspace 根
    #: 每条已完成的撤销：{root, path, result}。只记相对路径会再次丢掉根，
    #: 而 `verify_receipts` 要靠 (root, path) 认出"这是一次已记录的撤销"。
    entries: list[dict[str, str]] = field(default_factory=list)


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.blob_dir = root / "blobs"
        self.manifest_dir = root / "snapshots"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    def manifest_path(self, run_id: str) -> Path:
        return self.manifest_dir / f"{run_id}.json"

    def init_run(self, run_id: str) -> None:
        """run 启动时创建空清单。

        这样「清单不存在」才能被解释成异常（被清理/损坏），
        而不是「这个 run 什么都没写」——后者会让恢复把有副作用的 intent
        判成 none_applied。
        """
        path = self.manifest_path(run_id)
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
        *,
        root: str | Path,
    ) -> Change:
        """在真正写文件之前调用，并立即原子落盘。

        `root` 是这次写入所属的 workspace 根，**必填**：没有根的写入证据
        既不能撤销也不能定位，宁可在这里炸掉，也不要在撤销时才发现。
        """
        resolved_root = _clean_root(root)
        rel = _clean_rel(rel_path)

        if before is None:
            change = Change(call_id, resolved_root, rel, ABSENT, expected_after, None)
        else:
            digest = hashlib.sha256(before).hexdigest()
            blob = self.blob_dir / digest
            if not blob.exists():
                atomic_write_bytes(blob, before)  # 内容寻址，同内容只存一份
            change = Change(
                call_id, resolved_root, rel, digest, expected_after, str(blob)
            )

        entries = [c for c in self.load(run_id) if c.call_id != call_id]
        entries.append(change)
        payload = json.dumps(
            {"version": MANIFEST_VERSION,
             "changes": [c.to_dict() for c in entries]},
            ensure_ascii=False, indent=2,
        ).encode("utf-8")
        atomic_write_bytes(self.manifest_path(run_id), payload)
        return change

    def load(self, run_id: str) -> list[Change]:
        path = self.manifest_path(run_id)
        if not path.exists():
            return []
        # 按字节读再显式解码：文本模式在 Windows 上会做换行翻译，
        # 而快照清单是审计材料——读它就该和写它一样"字节进字节出"。
        try:
            return self._parse(path.read_bytes())
        except TianshuError:
            # 版本不认这类**明确的拒绝**原样往上抛：它比"文件坏了"更具体，
            # 调用方据此说的话也不一样（"升级程序" vs "证据被破坏"）。
            raise
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            # 坏 JSON / 缺字段 / 类型不对 / 读不出来，全都意味着"这份证据不可信"。
            # 以前这里直接漏出 `JSONDecodeError`：恢复和撤销会看到一个裸的解析
            # 异常，既不是域错误，也没有说清是哪份文件——校验器自己先崩了，
            # 就没人替它回答"到底写没写"。转成域错误，让调用方必须处理。
            raise SnapshotCorrupt(
                f"快照清单无法解析：{path}（{type(exc).__name__}: {exc}）。"
                "这份证据不能作为结论来源：恢复必须判 unknown 并阻断，"
                "撤销必须拒绝，不能当成空清单继续。"
            ) from exc

    def _parse(self, raw_bytes: bytes) -> list[Change]:
        raw = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(raw, dict):
            version = raw.get("version")
            if isinstance(version, int) and version not in _READABLE_VERSIONS:
                raise TianshuError(
                    f"无法解析快照清单版本 {version}"
                    f"（本程序支持 {_READABLE_VERSIONS}）：字段语义可能不同，"
                    "拒绝按猜的语义解释——尤其不要用旧版本撤销新清单"
                )
            items = raw["changes"]
        else:
            # v1 清单就是一个裸列表，没有 version 也没有 root。
            items = raw
        return [
            Change(
                call_id=i["call_id"],
                root=i.get("root"),
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
        return self.manifest_path(run_id).exists()

    # ---- 撤销 ----

    def undo(
        self,
        run_id: str,
        ws: Workspace | None = None,
        *,
        guard: Callable[[], None] | None = None,
        apply: Callable[[Change, bytes | None], None] | None = None,
        allow_direct_write: bool = False,
    ) -> UndoReport:
        """把该 run 的变更撤回。

        五条规矩：
        1. 撤销也是写操作，必须先过 guard（阻断检查）。
        2. **落盘目录由证据决定**：每条记录按自己的 `root` 解析，不用调用方传的
           单一目录。`ws` 只是**范围筛选**（只撤这个 workspace 下的变更），
           不再参与路径解析——那正是跨目录写错的来源。
        3. 同一文件被写过多次时，按**调用逆序**处理——正序检查会自相冲突。
        4. 预检阶段用内存模拟最终状态，全部通过才动手。
        5. **真正写文件必须经 `apply` 回调**（M2：派发 / 审批 / 租约）。
           不传 apply 就只能显式 `allow_direct_write=True`；那是 M1 语义，
           供旧测试与恢复工具使用，M2 的 CLI 不会走这条路。
        """
        if guard is None:
            raise ToolNotAllowed("undo 必须提供执行侧 guard，不能绕过阻断检查")
        if apply is None and not allow_direct_write:
            raise ToolNotAllowed(
                "undo 必须通过授权派发路径（传 apply 回调）；"
                "直写需显式 allow_direct_write=True"
            )
        guard()

        changes = self.load(run_id)
        if not changes:
            return UndoReport(ok=False, problems=[f"run {run_id} 没有可撤销的变更"])

        report = UndoReport(ok=False)

        # ---- 旧格式：不知道写的是哪个目录。宁可失败，也不猜 ----
        unscoped = [c for c in changes if not c.root]
        if unscoped:
            listed = ", ".join(f"{c.path}（call_id={c.call_id}）" for c in unscoped)
            report.problems.append(
                f"该 run 的快照是旧格式（v2，没有 workspace 根）：{listed}。"
                "无法确定这些写入落在哪个目录，拒绝撤销——写错目录比撤销失败"
                "更糟。请先用 `tianshu recover --workspace <目录>` 核对，"
                "或按 manifest 手工处理。"
            )
            return self._refuse(report)

        selected = [c for c in changes if ws is None or c.root == str(ws.root)]
        if not selected:
            known = "、".join(sorted({str(c.root) for c in changes}))
            report.problems.append(
                f"该 run 在 {ws.root} 下没有可撤销的变更（已记录的 workspace：{known}）"
            )
            return report

        # ---- 预检：校验当前磁盘等于最后一次 after，再按逆序模拟 ----
        report.roots = sorted({str(c.root) for c in selected})
        by_root: dict[str, Workspace] = {}
        for root in report.roots:
            try:
                by_root[root] = Workspace(Path(root))
            except TianshuError as exc:
                report.problems.append(
                    f"证据里的 workspace 根现在不可用：{root}（{exc}）；"
                    "无法把文件写回去，拒绝撤销"
                )
                return self._refuse(report)

        # 键必须是 (root, path)：两个 workspace 下的同名相对路径是两条独立证据。
        disk_state: dict[tuple[str | None, str], str] = {}
        preimages: dict[str, bytes] = {}
        io = current_backend()
        for c in selected:
            target = by_root[str(c.root)].resolve(c.path)
            # 工作区的当前状态问后端；证据（blob / manifest）始终在宿主盘上，
            # 因为它是审计材料，不能跟着工作区的后端一起被虚拟化。
            disk_state[c.key] = (
                ABSENT if not io.exists(target)
                else hashlib.sha256(io.read_bytes(target)[0]).hexdigest()
            )

        simulated = dict(disk_state)
        for c in reversed(selected):
            current = simulated[c.key]
            if current != c.expected_after:
                report.conflicts.append(
                    f"{Path(str(c.root)) / c.path}：当前内容与 Agent 写完时不一致"
                    f"（{current[:12]}… ≠ {c.expected_after[:12]}…），拒绝覆盖"
                )
                return self._refuse(report)
            if c.before_state != ABSENT:
                if not c.blob_ref or not Path(c.blob_ref).is_file():
                    report.missing_blobs.append(
                        f"{Path(str(c.root)) / c.path}：写前内容丢失，无法还原"
                    )
                    return self._refuse(report)
                preimage = Path(c.blob_ref).read_bytes()
                if hashlib.sha256(preimage).hexdigest() != c.before_state:
                    report.problems.append(
                        f"{Path(str(c.root)) / c.path}：写前 blob 哈希不符"
                    )
                    return self._refuse(report)
                # 每条证据可能引用同一内容；预检一次性保存字节，执行不再读可变引用。
                preimages[c.call_id] = preimage
            simulated[c.key] = c.before_state

        # ---- 执行：逆序恢复调用链。跨多文件的崩溃仍须如实记 partial ----
        for c in reversed(selected):
            target = by_root[str(c.root)].resolve(c.path)
            preimage = None if c.before_state == ABSENT else preimages[c.call_id]
            if apply is not None:
                apply(c, preimage)
            elif preimage is None:
                try:
                    io.unlink(target)
                except FileNotFoundError:
                    pass  # 本来就不存在：撤销"新建"时的幂等语义
            else:
                io.write_bytes_atomic(target, preimage)
            result = "deleted" if preimage is None else "restored"
            (report.deleted if preimage is None else report.restored).append(c.path)
            report.entries.append(
                {"root": str(c.root), "path": c.path, "result": result}
            )

        report.ok = True
        return report

    @staticmethod
    def _refuse(report: UndoReport) -> UndoReport:
        report.problems.append("预检未通过，未做任何修改")
        return report
