"""Run 创建前的当前空间保护：只读真实位置，不预留容量。"""

import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .run_policy import PURE_ONLY, RunExecutionPolicyV1

MIN_FREE_BYTES = 2147483648


class DiskAdmissionError(TianshuError):
    def __init__(self, rule_id: str, reason: str) -> None:
        self.rule_id = rule_id
        super().__init__(f"{rule_id}: {reason}")


def free_bytes(path: Path) -> int:
    """解析链接后寻找最近真实祖先；不创建目录、数据库或守卫文件。"""
    current = Path(path).expanduser().resolve()
    while True:
        try:
            existing = current.stat()
            break
        except FileNotFoundError:
            if current == current.parent:
                raise
            current = current.parent
    if not stat.S_ISDIR(existing.st_mode):
        current = current.parent
    return shutil.disk_usage(current).free


@dataclass(frozen=True)
class RunStorageContext:
    log: Any
    snapshots: Any | None
    memory: Any | None
    contents: Any | None
    data_root: Path | None
    authority_root: Callable[[], Path]

    @classmethod
    def from_stores(
        cls,
        log,
        *,
        snapshots,
        memory,
        contents,
        data_root,
        authority_root,
    ) -> "RunStorageContext":
        return cls(
            log,
            snapshots,
            memory,
            contents,
            Path(data_root) if data_root is not None else None,
            authority_root,
        )

    def locations(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        if self.log.db_path.name != ":memory:":
            paths.append(self.log.db_path)
        if self.log.blob_dir is not None:
            paths.append(self.log.blob_dir)
        if self.snapshots is not None:
            paths.extend(
                (self.snapshots.root, self.snapshots.blob_dir, self.snapshots.manifest_dir)
            )
        if self.memory is not None and self.memory.blob_dir is not None:
            paths.append(self.memory.blob_dir)
        if self.contents is not None:
            # ContentStore 的未配置位置会明确抛错，不能洗成“不适用”。
            paths.append(self.contents.blob_dir)
        if self.data_root is None:
            raise ValueError("缺少实际 execution.guard 数据根")
        paths.append(self.data_root / "execution.guard")
        paths.append(self.authority_root() / "authority.db")
        return tuple(Path(p) for p in paths)


class DiskAdmission:
    def __init__(
        self, context: RunStorageContext | None, *, probe: Callable[[Path], int] = free_bytes
    ) -> None:
        self.context = context
        self._probe = probe

    def assert_stores(self, log, *, snapshots, memory, contents, data_root) -> None:
        context = self.context
        if context is None or (
            context.log is not log
            or context.snapshots is not snapshots
            or context.memory is not memory
            or context.contents is not contents
            or (Path(data_root).resolve() if data_root is not None else None)
            != (context.data_root.resolve() if context.data_root is not None else None)
        ):
            raise DiskAdmissionError(
                "dispatch.disk_space_unknown", "实际存储实例与 Run context 不一致"
            )

    def require_new_run(
        self, policy: RunExecutionPolicyV1, *, writable_roots: tuple[Path, ...]
    ) -> None:
        if not isinstance(policy, RunExecutionPolicyV1):
            raise TianshuError("execution_policy 必须是 RunExecutionPolicyV1")
        if policy != PURE_ONLY:
            self.require_effect_space(writable_roots=writable_roots, targets=())

    def require_effect_space(
        self, *, writable_roots: tuple[Path, ...], targets: tuple[Path, ...]
    ) -> None:
        if self.context is None:
            raise DiskAdmissionError("dispatch.disk_space_unknown", "缺少完整 Run storage context")
        try:
            locations = self.context.locations() + writable_roots + targets
            # 每个真实位置分别现测；保留跨卷、嵌套挂载和缺失祖先的原位置。
            for path in dict.fromkeys(Path(p).expanduser().resolve() for p in locations):
                value = self._probe(path)
                if type(value) is not int or value < 0:
                    raise ValueError(f"{path}: free 不是非负整数")
                if value < MIN_FREE_BYTES:
                    raise DiskAdmissionError(
                        "dispatch.disk_space_low", f"{path}: 可用空间低于 2 GiB"
                    )
        except DiskAdmissionError:
            raise
        except Exception as exc:
            raise DiskAdmissionError(
                "dispatch.disk_space_unknown", f"无法证明必需位置的空间：{exc}"
            ) from exc
