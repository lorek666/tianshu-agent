"""Read-only delivery of text files committed by a Run's file tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .events import EventLog, canonical_bytes, sha256_hex

# A preview should fit one desktop RPC even for heavily escaped JSON text.
PREVIEW_BYTES = 65536
_FILE_WRITES = {"create_report": "create", "modify_text": "overwrite"}
_RECEIPT_ID = re.compile(r"rcpt_[0-9a-f]{12}")


class ArtifactNotFound(TianshuError):
    """No matching verified file was produced by this Run."""


@dataclass(frozen=True)
class _Artifact:
    artifact_id: str
    path: str
    sha256: str
    tool_id: str
    raw: bytes

    def view(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "bytes": len(self.raw),
            "sha256": self.sha256,
            "tool_id": self.tool_id,
        }


def _artifact(
    run_id: str, receipt: dict[str, Any], intent: dict[str, Any], result: dict[str, Any]
) -> _Artifact | None:
    tool = receipt.get("tool_id")
    artifact_id = receipt.get("receipt_id")
    call_id = receipt.get("call_id")
    if (
        tool not in _FILE_WRITES
        or receipt.get("action") != _FILE_WRITES[tool]
        or receipt.get("run_id") != run_id
        or receipt.get("effect_state") != "applied"
        or not isinstance(artifact_id, str)
        or not _RECEIPT_ID.fullmatch(artifact_id)
        or not isinstance(call_id, str)
        or result.get("status") != "succeeded"
        or result.get("effect_state") != "applied"
        or any(event.get("tool_id") != tool or event.get("call_id") != call_id
               for event in (intent, result))
    ):
        return None
    args, output = intent.get("args"), result.get("output")
    if not isinstance(args, dict) or not isinstance(output, dict):
        return None
    text = args.get("content")
    resource, root, rel = receipt.get("resource"), receipt.get("root"), output.get("path")
    if not all(isinstance(item, str) for item in (text, resource, root, rel)):
        return None
    # Check the recorded paths lexically. This view never opens the workspace;
    # symlinks and later edits cannot affect the bytes it returns.
    target, workspace = Path(resource), Path(root)
    if (
        not target.is_absolute() or not workspace.is_absolute()
        or workspace not in target.parents or ".." in target.parts
        or target.relative_to(workspace).as_posix() != rel
    ):
        return None
    raw = text.encode("utf-8")
    digest = sha256_hex(raw)
    args_digest = sha256_hex(canonical_bytes(args))
    if (
        digest != receipt.get("after_state") or digest != output.get("sha256")
        or digest != output.get("after_state") or len(raw) != output.get("bytes")
        or args_digest != intent.get("args_hash") or args_digest != receipt.get("args_hash")
    ):
        return None
    return _Artifact(artifact_id, rel, digest, tool, raw)


def _artifacts(log: EventLog, run_id: str, *, only: str | None = None) -> list[_Artifact]:
    intents: dict[int, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    files: list[_Artifact] = []
    for event in log.read_run(run_id):
        p = event.payload
        if event.type == "tool.intent" and p.get("tool_id") in _FILE_WRITES:
            intents[event.seq] = p
        elif event.type == "tool.result" and p.get("tool_id") in _FILE_WRITES:
            results[p["call_id"]] = p
        elif event.type == "receipt" and p.get("tool_id") in _FILE_WRITES:
            if only is not None and p.get("receipt_id") != only:
                continue
            intent = intents.get(p.get("intent_seq"))
            result = results.get(p.get("call_id"))
            if intent is not None and result is not None:
                item = _artifact(run_id, p, intent, result)
                if item is not None:
                    files.append(item)
    return files


def list_artifacts(log: EventLog, run_id: str) -> list[dict[str, Any]]:
    return [item.view() for item in _artifacts(log, run_id)]


def preview_artifact(log: EventLog, run_id: str, artifact_id: str) -> dict[str, Any]:
    files = _artifacts(log, run_id, only=artifact_id) if _RECEIPT_ID.fullmatch(artifact_id) else []
    if len(files) != 1:
        raise ArtifactNotFound("本次运行不存在此可核对的文件产物")
    file = files[0]
    # All bytes were encoded from verified UTF-8 text; only the last character
    # can be incomplete after slicing.
    return {
        **file.view(),
        "text": file.raw[:PREVIEW_BYTES].decode("utf-8", errors="ignore"),
        "truncated": len(file.raw) > PREVIEW_BYTES,
    }
