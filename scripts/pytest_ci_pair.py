"""Run two isolated CI shard wrappers; never replace their diagnostic path."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _cleanup(children: list[subprocess.Popen]) -> None:
    errors: list[BaseException] = []
    # Only signal still-owned roots; never use a reaped PID or PGID. One OS
    # failure must not prevent cleanup of the other roots.
    for child in children:
        try:
            if child.poll() is None:
                child.terminate()
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            errors.append(exc)
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # Escalate below, after checking that the root is still owned.
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            errors.append(exc)
        try:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            errors.append(exc)
        confirmed = False
        try:
            confirmed = child.poll() is not None
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            errors.append(exc)
        if not confirmed:
            print(f"CI worker root {child.pid}: cleanup unconfirmed", file=sys.stderr)
            errors.append(subprocess.SubprocessError("worker cleanup unconfirmed"))
    if errors:
        raise errors[0]


def run_pair(index: int, root: Path, pytest_args: list[str]) -> int:
    runtime = root.with_name(root.name + "-runtime")
    root.mkdir(parents=True, exist_ok=False)
    runtime.mkdir(parents=True, exist_ok=False)
    scripts = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(scripts), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    children: list[subprocess.Popen] = []
    try:
        for worker in range(index * 2, index * 2 + 2):
            children.append(subprocess.Popen([
                sys.executable, str(scripts / "pytest_with_stacks.py"),
                *pytest_args, "-p", "pytest_ci_shards",
                f"--ci-shard-index={worker}", "--ci-shard-count=4",
                f"--ci-shard-report={root / str(worker) / 'shard.json'}",
                f"--basetemp={runtime / str(worker)}",
                "-o", f"cache_dir={runtime / ('cache-' + str(worker))}",
            ], env=env))
        codes = [child.wait() for child in children]
        return next((code if code > 0 else 1 for code in codes if code), 0)
    finally:
        _cleanup(children)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--ci-pair-index", type=int, choices=(0, 1), required=True)
    parser.add_argument("--ci-pair-root", type=Path, required=True)
    args, pytest_args = parser.parse_known_args(argv)
    if not args.ci_pair_root.is_absolute():
        parser.error("--ci-pair-root must be absolute")
    if any(arg.split("=", 1)[0].startswith("--ci-shard-")
           or arg.split("=", 1)[0] == "--basetemp" for arg in pytest_args):
        parser.error("worker shard options and basetemp are owned by the pair runner")
    try:
        return run_pair(args.ci_pair_index, args.ci_pair_root, pytest_args)
    except KeyboardInterrupt:
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"CI worker pair failed ({type(exc).__name__})", file=sys.stderr)
        return 1
