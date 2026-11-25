"""Resolve the actual attempt of each uploaded CI shard artifact."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


def fetch_artifacts(repository: str, run_id: str, token: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid GitHub repository")
    if not run_id.isdigit() or not token:
        raise ValueError("missing run identity or GitHub token")
    artifacts = []
    page = 1
    while True:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/artifacts"
            f"?per_page=100&page={page}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
            raise ValueError("invalid GitHub artifact response")
        rows = payload["artifacts"]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("invalid GitHub artifact row")
        artifacts.extend(rows)
        if len(rows) < 100:
            return artifacts
        page += 1


def resolve_artifacts(
    artifacts: list[dict], *, run_id: str, prefix: str, indices: list[int],
) -> dict[str, dict[str, str | int]]:
    if not run_id.isdigit() or not prefix or not indices or len(set(indices)) != len(indices):
        raise ValueError("invalid artifact resolution identity")
    if any(type(index) is not int or index < 0 for index in indices):
        raise ValueError("invalid shard index")
    resolved = {}
    for index in indices:
        pattern = re.compile(rf"^{re.escape(prefix)}-{re.escape(run_id)}-([1-9][0-9]*)-{index}$")
        matches: dict[int, str] = {}
        for row in artifacts:
            name = row.get("name")
            if row.get("expired") is True or not isinstance(name, str):
                continue
            match = pattern.fullmatch(name)
            if match is None:
                continue
            attempt = int(match[1])
            if attempt in matches:
                raise ValueError(f"duplicate artifacts for shard {index} attempt {attempt}")
            matches[attempt] = name
        if not matches:
            raise ValueError(f"no nonexpired artifact for shard {index}")
        attempt = max(matches)
        resolved[str(index)] = {"name": matches[attempt], "attempt": attempt}
    return resolved


def write_github_output(
    resolved: dict[str, dict[str, str | int]], output: Path, *, reports_per_artifact: int = 1,
) -> None:
    if reports_per_artifact not in (1, 2):
        raise ValueError("reports per artifact must be one or two")
    lines = []
    attempts = {}
    for index in sorted(resolved, key=int):
        row = resolved[index]
        lines.append(f"shard_{index}_name={row['name']}")
        lines.append(f"shard_{index}_attempt={row['attempt']}")
        first = int(index) * reports_per_artifact
        for report_index in range(first, first + reports_per_artifact):
            attempts[str(report_index)] = str(row["attempt"])
    lines.append(f"attempts={json.dumps(attempts, sort_keys=True)}")
    with output.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--indices", required=True, nargs="+", type=int)
    parser.add_argument("--reports-per-artifact", type=int, default=1)
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        artifacts = fetch_artifacts(
            args.repository, args.run_id, os.environ.get(args.token_env, ""),
        )
        resolved = resolve_artifacts(
            artifacts, run_id=args.run_id, prefix=args.prefix, indices=args.indices,
        )
        if args.github_output is not None:
            write_github_output(
                resolved, args.github_output, reports_per_artifact=args.reports_per_artifact,
            )
    except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        parser.exit(1, f"CI artifact resolution failed: {exc}\n")
    print(json.dumps(resolved, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
