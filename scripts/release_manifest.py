from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release" / "release.schema.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".omc",
        ".tianshu",
        ".venv",
        "__pycache__",
        "logs",
        "refs",
        "tests",
    }
)
_FORBIDDEN_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".log", ".key"})


class ReleaseManifestError(ValueError):
    """发布清单或它引用的文件不满足发行约束。"""


def _schema() -> dict[str, Any]:
    try:
        raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseManifestError(f"无法读取发布清单 schema：{exc}") from exc
    if not isinstance(raw, dict):
        raise ReleaseManifestError("发布清单 schema 根必须是 object")
    return raw


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseManifestError(message)


def _validate_relative_path(raw: str) -> None:
    _expect(isinstance(raw, str) and raw != "", "files 的相对路径必须是非空字符串")
    _expect("\\" not in raw, f"files 路径必须使用 /：{raw!r}")
    path = PurePosixPath(raw)
    _expect(not path.is_absolute(), f"files 路径不得是绝对路径：{raw!r}")
    _expect(".." not in path.parts, f"files 路径不得包含 ..：{raw!r}")
    _expect(path.parts and path.parts[0] not in {"", "."}, f"files 路径不合法：{raw!r}")
    lowered = {part.lower() for part in path.parts}
    _expect(not (lowered & _FORBIDDEN_PARTS), f"发行物包含禁止目录：{raw!r}")
    _expect(path.suffix.lower() not in _FORBIDDEN_SUFFIXES, f"发行物包含禁止文件：{raw!r}")
    _expect(not path.name.startswith(".env"), f"发行物包含环境文件：{raw!r}")


def validate_manifest(data: object, *, base_dir: Path) -> None:
    """按真实 schema、路径规则和磁盘字节校验一个发布清单。"""

    schema = _schema()
    _expect(isinstance(data, dict), "发布清单根必须是 object")
    assert isinstance(data, dict)
    required = set(schema["required"])
    properties = schema["properties"]
    missing = required - set(data)
    unknown = set(data) - set(properties)
    _expect(not missing, f"发布清单缺少字段：{sorted(missing)}")
    _expect(not unknown, f"发布清单包含未知字段：{sorted(unknown)}")

    _expect(data["schema"] == properties["schema"]["const"], "不支持的发布清单 schema")
    _expect(data["product"] == properties["product"]["const"], "product 不是 tianshu-agent")
    _expect(
        isinstance(data["version"], str)
        and re.fullmatch(r"0\.[0-9]+\.[0-9]+", data["version"]) is not None,
        "version 必须匹配 0.x.y",
    )
    _expect(
        isinstance(data["commit"], str)
        and re.fullmatch(r"[0-9a-f]{40}", data["commit"]) is not None,
        "commit 必须是 40 位小写十六进制 SHA",
    )
    _expect(
        data["target"] in properties["target"]["enum"],
        f"未知发布 target：{data['target']!r}",
    )
    _expect(
        isinstance(data["python"], str)
        and re.fullmatch(r"3\.12\.[0-9]+", data["python"]) is not None,
        "python 必须是 3.12.x",
    )
    _expect(
        isinstance(data["python_build_release"], str) and data["python_build_release"] != "",
        "python_build_release 必须是非空字符串",
    )
    _expect(
        isinstance(data["ui_dist_sha256"], str)
        and _SHA256_RE.fullmatch(data["ui_dist_sha256"]) is not None,
        "ui_dist_sha256 必须是 64 位小写十六进制 SHA",
    )

    files = data["files"]
    _expect(isinstance(files, dict) and files, "files 必须是非空 object")
    assert isinstance(files, dict)
    root = Path(base_dir).resolve()
    ui_hashes: set[str] = set()
    for raw, digest in files.items():
        _validate_relative_path(raw)
        _expect(
            isinstance(digest, str) and _SHA256_RE.fullmatch(digest) is not None,
            f"{raw} 的 SHA256 不合法",
        )
        target = (root / raw).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ReleaseManifestError(f"files 路径逃出发布根：{raw!r}") from exc
        _expect(target.is_file(), f"发布清单引用的文件不存在：{raw}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        _expect(actual == digest, f"{raw} 的 SHA256 不匹配：期望 {digest}，实际 {actual}")
        if raw.endswith("ui/index.html") or raw.endswith("ui/dist/index.html"):
            ui_hashes.add(actual)

    _expect(
        data["ui_dist_sha256"] in ui_hashes,
        "ui_dist_sha256 必须对应 files 中的 UI index.html",
    )


def read_manifest(path: Path, *, base_dir: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseManifestError(f"无法读取发布清单：{exc}") from exc
    validate_manifest(data, base_dir=base_dir or Path(path).parent)
    assert isinstance(data, dict)
    return data


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
