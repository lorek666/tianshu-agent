"""Collect explicitly selected local evidence; never execute its historical claims.

This developer-only format is not C5 or a product approval. Originals stay
private. Only summary.json has a fixed field allowlist for sharing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tianshu.eval.report import DIMENSIONS, MIN_REPEAT, SCHEMA
from tianshu.metrics import read_metrics


class CollectionError(Exception):
    def __init__(self, category: str, code: int = 2):
        super().__init__(category)
        self.category = category
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def _constant(value: str):
    raise ValueError("nonfinite constant")


def _load(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_float=_finite, parse_constant=_constant)


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _validate_report(raw: bytes) -> None:
    report = _load(raw)
    fields = {
        "schema": str, "suite_version": str, "selection": dict, "generated_at": str,
        "model": dict, "repeat": int, "elapsed_ms": int, "workspace_retained": bool,
        "safety_gate": dict, "columns": dict, "dimensions": dict, "validity": dict,
        "overall": dict, "scope": dict, "categories": dict, "attribution": dict,
        "verdict": str, "cases": list,
    }
    if not isinstance(report, dict) or any(
        type(report.get(key)) is not expected for key, expected in fields.items()
    ):
        raise ValueError("report shape")
    repeat, cases = report["repeat"], report["cases"]
    if (report["schema"] != SCHEMA or repeat < MIN_REPEAT or not cases
            or report["verdict"] not in {"pass", "fail", "unsafe", "unknown"}
            or type(report["safety_gate"].get("passed")) is not bool
            or any(not isinstance(report["dimensions"].get(key), dict) for key in DIMENSIONS)):
        raise ValueError("report shape")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("case shape")
        case_id, digest = case.get("case_id"), case.get("fixture_digest")
        details = case.get("run_details")
        if (not isinstance(case_id, str) or not case_id or case_id in seen
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                or not _count(case.get("runs_count")) or case["runs_count"] != repeat
                or not isinstance(details, list) or len(details) != repeat
                or any(not isinstance(item, dict) for item in details)):
            raise ValueError("case shape")
        seen.add(case_id)
    overall, validity = report["overall"], report["validity"]
    if (any(not _count(overall.get(key)) for key in ("cases", "runs"))
            or any(not _count(validity.get(key)) for key in ("samples", "valid", "invalid"))
            or overall["cases"] != len(cases) or overall["runs"] != repeat * len(cases)
            or validity["samples"] != overall["runs"]
            or validity["valid"] + validity["invalid"] != validity["samples"]):
        raise ValueError("report counts")


def _validate_metrics(raw: bytes) -> None:
    lines = [line for line in raw.split(b"\n") if line.strip()]
    if not lines or any(not isinstance(_load(line), dict) for line in lines):
        raise ValueError("metrics shape")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # Do not inherit Git routing, tracing, or external fsmonitor settings.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "--no-pager", "-C", str(repo), *args],
        shell=False, capture_output=True, check=False, env=env,
    )


def _git_root(path: Path, *, optional: bool = False) -> Path | None:
    result = _git(path, "rev-parse", "--show-toplevel")
    if result.returncode:
        if optional and result.returncode == 128 and b"not a git repository" in result.stderr:
            return None
        raise CollectionError("GIT_FAILED", 1)
    return Path(result.stdout.decode("utf-8").strip()).resolve(strict=True)


def _observe_git(repo: Path) -> dict[str, object]:
    values = []
    for args in [("rev-parse", "--verify", "HEAD"), ("rev-parse", "--verify", "HEAD^{tree}"),
                 ("status", "--porcelain=v1", "-z", "--untracked-files=all",
                  "--ignore-submodules=none")]:
        result = _git(repo, *args)
        if result.returncode:
            raise CollectionError("GIT_FAILED", 1)
        values.append(result.stdout)
    return {"head": values[0].decode("ascii").strip(),
            "tree": values[1].decode("ascii").strip(), "dirty": bool(values[2])}


def _output_path(out: Path) -> Path:
    if out.exists() or out.is_symlink():
        raise CollectionError("OUTPUT_EXISTS")
    target = out.resolve()
    if not target.parent.is_dir():
        raise CollectionError("OUTPUT_INVALID")
    root = _git_root(target.parent, optional=True)
    if root is not None:
        tracked = _git(root, "--literal-pathspecs", "ls-files", "-z", "--", str(target))
        if tracked.returncode:
            raise CollectionError("GIT_FAILED", 1)
        if tracked.stdout:
            raise CollectionError("OUTPUT_TRACKED")
        result = _git(root, "check-ignore", "-q", "--", target.as_posix() + "/")
        if result.returncode == 1:
            raise CollectionError("OUTPUT_NOT_IGNORED")
        if result.returncode:
            raise CollectionError("GIT_FAILED", 1)
    return target


def _write_private(path: Path, payload: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_receipt(path: Path, payload: bytes) -> None:
    temporary = path.with_name("collection.json.tmp")
    _write_private(temporary, payload)
    os.replace(temporary, path)


def collect(*, repo: Path, reports: tuple[Path, ...], metrics: Path | None,
            out: Path) -> dict[str, object]:
    """Observe this collection only, without authenticating imported execution."""
    collector_hash = _sha(Path(__file__).read_bytes())
    started = _now()
    repo = repo.resolve(strict=True)
    if not repo.is_dir() or _git_root(repo) != repo:
        raise CollectionError("REPO_INVALID")
    before = _observe_git(repo)
    if before["dirty"]:
        raise CollectionError("REPO_DIRTY")
    if not reports:
        raise CollectionError("INPUT_INVALID")
    inputs = []
    selected = [(f"report-{i:04d}", "eval_report", p) for i, p in enumerate(reports, 1)]
    if metrics is not None:
        selected.append(("metrics", "metrics_jsonl", metrics))
    try:
        for identity, kind, source in selected:
            path = source.resolve(strict=True)
            if not path.is_file() or any(path.samefile(item[2]) for item in inputs):
                raise ValueError("input file")
            raw = path.read_bytes()
            (_validate_report if kind == "eval_report" else _validate_metrics)(raw)
            inputs.append((identity, kind, path, raw))
    except (OSError, ValueError, RecursionError) as exc:
        raise CollectionError("INPUT_INVALID") from exc
    target = _output_path(out)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise CollectionError("OUTPUT_EXISTS") from exc
    (target / "inputs").mkdir(mode=0o700)
    imported, files = [], []
    for identity, kind, _source, raw in inputs:
        relative = f"inputs/{identity}.{'json' if kind == 'eval_report' else 'jsonl'}"
        copy = target / relative
        _write_private(copy, raw)
        if copy.read_bytes() != raw:
            raise CollectionError("OUTPUT_CHANGED", 1)
        if kind == "metrics_jsonl":
            read_metrics(copy)
        imported.append({"id": identity, "file": relative, "historical_claims": "UNVERIFIED"})
        files.append({"id": identity, "bytes": len(raw), "sha256": _sha(raw)})
    after = _observe_git(repo)
    try:
        changed = (after != before
                   or any(source.read_bytes() != raw for _, _, source, raw in inputs)
                   or _sha(Path(__file__).read_bytes()) != collector_hash)
    except OSError as exc:
        raise CollectionError("SOURCE_CHANGED", 1) from exc
    if changed:
        raise CollectionError("SOURCE_CHANGED", 1)
    receipt = {
        "schema": "tianshu.dev.baseline-collection.v1", "collection_status": "COLLECTED",
        "execution_status": "NOT_EXECUTED_BY_TOOL", "approval_status": "NOT_EVALUATED",
        "caller_supplied": {"repo": str(repo), "inputs": [
            {"id": identity, "kind": kind, "path": str(source)}
            for identity, kind, source, raw in inputs
        ]},
        "imported": imported,
        "tool_observed": {
            "collection_started_at_utc": started, "checks_finished_at_utc": _now(),
            "collector_sha256": collector_hash, "git_before": before, "git_after": after,
            "files": files,
        },
    }
    payload = _json_bytes(receipt)
    summary = {
        "schema": "tianshu.dev.baseline-summary.v1", "execution_status": "NOT_EXECUTED_BY_TOOL",
        "approval_status": "NOT_EVALUATED", "input_count": len(inputs),
        "metrics_present": metrics is not None, "receipt_required": True,
        "receipt_sha256": _sha(payload),
    }
    _write_private(target / "summary.json", _json_bytes(summary))
    _publish_receipt(target / "collection.json", payload)
    return receipt


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CollectionError("INVALID_ARGUMENTS")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _Parser(prog="baseline-evidence", allow_abbrev=False)
        parser.add_argument("--repo", type=Path, required=True)
        parser.add_argument("--report", type=Path, action="append", required=True)
        parser.add_argument("--metrics", type=Path, action="append", default=[])
        parser.add_argument("--out", type=Path, required=True)
        args = parser.parse_args(argv)
        if len(args.metrics) > 1:
            raise CollectionError("INVALID_ARGUMENTS")
        collect(repo=args.repo, reports=tuple(args.report),
                metrics=args.metrics[0] if args.metrics else None, out=args.out)
        return 0
    except SystemExit as exc:
        return int(exc.code)
    except CollectionError as exc:
        category, code = exc.category, exc.code
    except KeyboardInterrupt:
        category, code = "INTERRUPTED", 130
    except OSError:
        category, code = "IO_FAILED", 1
    except Exception:
        category, code = "INTERNAL_ERROR", 1
    try:
        sys.stderr.write(f"baseline-evidence: {category}\n")
    except (OSError, ValueError):
        return code
    return code


if __name__ == "__main__":
    raise SystemExit(main())
