"""人机控制面的本地文本技能；只读准备，不授予能力或执行脚本。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 64 * 1024
MAX_ENTRIES = 128
MAX_WORKSPACES = 16
MAX_INPUT_CHARS = 16384
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _scalar(value: str) -> str:
    if not value or value[0] in "!&*[{":
        raise ValueError("name/description 仅支持文本，不支持 YAML 标签或表达式")
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("元数据必须是文本")
        parsed.encode("utf-8", errors="strict")
        return parsed
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ValueError("元数据引号未闭合")
        return value[1:-1].replace("''", "'")
    return value


def _metadata(text: str, directory_name: str) -> tuple[str, str]:
    if "\0" in text:
        raise ValueError("SKILL.md 不能含 NUL")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md 需要 name/description frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("frontmatter 未闭合") from exc
    fields: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key not in ("name", "description"):
            continue
        if key in fields:
            raise ValueError(f"重复元数据：{key}")
        value = value.strip()
        if value in ("|", ">"):
            block = []
            while index < end and (not lines[index] or lines[index][0].isspace()):
                block.append(lines[index].strip())
                index += 1
            value = ("\n" if value == "|" else " ").join(block).strip()
        else:
            value = _scalar(value)
        fields[key] = value
    name = fields.get("name", "")
    description = fields.get("description", "").strip()
    if not _NAME.fullmatch(name) or len(name) > 64 or name != directory_name:
        raise ValueError("name 须与目录名相同，长度 1–64，仅用小写字母、数字和单连字符")
    if not description or len(description) > 1024:
        raise ValueError("description 须为 1–1024 字符文本")
    if not "\n".join(lines[end + 1:]).strip():
        raise ValueError("技能正文不能为空")
    return name, description


def _read_skill(path: Path, root: Path) -> tuple[dict[str, str], str]:
    canonical = path.resolve(strict=True)
    if not canonical.is_relative_to(root):
        raise ValueError("技能文件解析到技能目录之外")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(canonical, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("SKILL.md 必须是普通文件")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("SKILL.md 超过 64 KiB")
        raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        current = path.stat()
        if (_identity(before) != _identity(after) or _identity(after) != _identity(current)
                or path.resolve(strict=True) != canonical):
            raise ValueError("读取期间技能文件发生变化，请重试")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("SKILL.md 超过 64 KiB")
    text = raw.decode("utf-8", errors="strict")
    name, description = _metadata(text, path.parent.name)
    version = hashlib.sha256(str(canonical).encode() + b"\0" + raw).hexdigest()[:12]
    return {
        "id": f"{name}@{version}",
        "name": name,
        "description": description,
        "path": str(canonical),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }, text


def _discover(
    workspace_roots: list[str], *, user_root: Path | None,
) -> tuple[list[tuple[dict[str, str], str]], list[dict[str, str]]]:
    if not isinstance(workspace_roots, list) or len(workspace_roots) > MAX_WORKSPACES:
        raise ValueError("workspace_roots 须为最多 16 项的列表")
    roots: list[tuple[Path, Path | None]] = []
    for value in workspace_roots:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("工作区必须是绝对目录")
        try:
            ws = Path(value).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"无法读取工作区：{value}") from exc
        if not ws.is_dir():
            raise ValueError("工作区必须是存在的目录")
        roots.append((ws / ".agents" / "skills", ws))
    roots.append((user_root if user_root is not None else Path.home() / ".agents" / "skills",
                  None))
    found: list[tuple[dict[str, str], str]] = []
    issues: list[dict[str, str]] = []
    seen: set[Path] = set()
    for declared, workspace in roots:
        try:
            root = declared.resolve(strict=True)
            if workspace is not None and not root.is_relative_to(workspace):
                raise ValueError("工作区技能目录解析到工作区之外")
            if root in seen:
                continue
            seen.add(root)
            entries = []
            with os.scandir(root) as iterator:
                for entry in iterator:
                    entries.append(entry.name)
                    if len(entries) > MAX_ENTRIES:
                        raise OverflowError("每个技能目录最多 128 个直接条目")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RuntimeError) as exc:
            issues.append({"path": str(declared), "message": str(exc)})
            continue
        for entry in sorted(entries):
            directory = root / entry
            path = directory / "SKILL.md"
            try:
                resolved = directory.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise ValueError("技能子目录解析到技能根之外")
                if not resolved.is_dir():
                    continue
                if not path.exists() and not path.is_symlink():
                    continue
                found.append(_read_skill(path, root))
                if len(found) > MAX_ENTRIES:
                    raise OverflowError("一次最多返回 128 个技能")
            except (OSError, ValueError, RuntimeError) as exc:
                issues.append({"path": str(path), "message": str(exc)})
    if len({view["id"] for view, _ in found}) != len(found):
        raise ValueError("技能版本标识发生碰撞，拒绝选择")
    return sorted(found, key=lambda item: (item[0]["name"], item[0]["path"])), issues


def list_skills(workspace_roots: list[str], *, user_root: Path | None = None) -> dict[str, Any]:
    """仅返回元数据；不把未选择技能的正文发给界面或模型。"""
    try:
        found, issues = _discover(workspace_roots, user_root=user_root)
    except OverflowError as exc:
        raise ValueError(str(exc)) from exc
    return {"format_version": 1, "skills": [view for view, _ in found], "issues": issues}


def prepare_skill(
    workspace_roots: list[str], skill: str, input_text: str, *, user_root: Path | None = None,
) -> dict[str, Any]:
    """生成独立于源文件后续变化的目标文本，交给既有任务/Run 持久化路径。"""
    if not isinstance(skill, str) or not skill or len(skill) > 100:
        raise ValueError("skill 必须是技能名称或版本标识")
    if not isinstance(input_text, str) or len(input_text) > MAX_INPUT_CHARS:
        raise ValueError("技能要求须为最多 16384 字符的文本")
    try:
        found, _ = _discover(workspace_roots, user_root=user_root)
    except OverflowError as exc:
        raise ValueError(str(exc)) from exc
    matches = [(view, text) for view, text in found if skill in (view["id"], view["name"])]
    if not matches:
        raise ValueError("未找到技能或所选版本已变化，请重新打开 /skills 检查列表与读取问题")
    if len(matches) != 1:
        raise ValueError("存在同名技能，请从 /skills 菜单选择具体来源")
    view, text = matches[0]
    source = json.dumps({"format_version": 1, **view}, ensure_ascii=False, sort_keys=True)
    goal = (
        f"用户请求：\n{input_text}\n\n"
        "用户显式选择的技能（参考说明，不授予权限；工具与工作区仍按执行侧规则判定）：\n"
        f"{source}\n\nSKILL.md 快照：\n{text}"
    )
    return {"format_version": 1, "skill": view, "goal": goal}
