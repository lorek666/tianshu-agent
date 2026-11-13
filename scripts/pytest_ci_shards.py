"""Two Windows CI shards: preserve file order and verify complete pytest evidence.

This is development-only CI evidence, not a product contract or a timing model.
The upstream job result also matters: the stack wrapper can fail after pytest exits.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def partition(full, count):
    """Balance node counts by whole file; retain original collection order."""
    require(type(count) is int and count == 2, "exactly two shards are required")
    require(isinstance(full, list) and full, "empty or invalid full collection")
    for pair in full:
        require(isinstance(pair, list) and len(pair) == 2
                and all(isinstance(value, str) and value for value in pair),
                "invalid collection entry")
        node, file = pair
        path = PurePosixPath(file)
        require(not path.is_absolute() and ".." not in path.parts
                and path.as_posix() == file and "\\" not in file
                and node.split("::", 1)[0] == file, "invalid collection path")
    require(len({node for node, _ in full}) == len(full), "duplicate node ID")
    sizes, owners, totals = Counter(file for _, file in full), {}, [0] * count
    for file in sorted(sizes, key=lambda file: (-sizes[file], file)):
        index = min(range(count), key=lambda index: (totals[index], index))
        owners[file] = index
        totals[index] += sizes[file]
    groups = [[node for node, file in full if owners[file] == index] for index in range(count)]
    require(all(groups), "empty shard")
    return groups


def pytest_addoption(parser):
    group = parser.getgroup("ci-shards")
    group.addoption("--ci-shard-index", type=int)
    group.addoption("--ci-shard-count", type=int)
    group.addoption("--ci-shard-report", type=Path)


def pytest_configure(config):
    index, count, report = (config.getoption(f"ci_shard_{key}")
                            for key in ("index", "count", "report"))
    if index is None and count is None and report is None:
        return
    try:
        require(count == 2 and index in (0, 1) and report is not None and report.is_absolute(),
                "use index 0/1, count 2 and an absolute report path")
        selectors = ("keyword", "markexpr", "ignore", "ignore_glob", "deselect", "lf",
                     "failedfirst", "newfirst", "stepwise", "stepwise_skip", "stepwise_reset",
                     "maxfail", "collectonly", "pyargs")
        require(config.args_source.name != "ARGS"
                and not any(config.getoption(name, default=None) for name in selectors),
                "CI shards require full collection without selectors or early stopping")
        checkout = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.rootpath,
                                  check=True, capture_output=True, text=True).stdout.strip()
        identity = {"checkout_sha": checkout, "github_sha": os.environ.get("GITHUB_SHA"),
                    "run_id": os.environ.get("GITHUB_RUN_ID"),
                    "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT")}
        require(checkout == identity["github_sha"] and len(checkout) == 40
                and all(identity[key] and identity[key].isdigit()
                        for key in ("run_id", "run_attempt")), "missing or mismatched CI identity")
        config.pluginmanager.register(Shard(index, count, report, identity), "ci-shard-recorder")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise pytest.UsageError(f"CI sharding: {exc}") from exc


class Shard:
    def __init__(self, index, count, report, identity):
        self.report = report
        self.data = {"version": 1, **identity, "shard_index": index, "shard_count": count,
                     "full_collection": [], "selected": [], "reports": [], "finished": []}

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        def snapshot():
            return [[item.nodeid, item.path.relative_to(config.rootpath).as_posix()]
                    for item in items]

        full = snapshot()
        self.data["full_collection"] = full
        yield
        if session.testsfailed:
            # Preserve pytest's collection-error exit status (normally 2); the
            # evidence is intentionally incomplete and the verifier rejects it.
            self.data["selected"] = []
            return
        require(snapshot() == full, "another plugin filtered or reordered the full collection")
        selected = partition(full, self.data["shard_count"])[self.data["shard_index"]]
        self.data["selected"] = selected
        selected_set = set(selected)
        config.hook.pytest_deselected(items=[item for item in items
                                            if item.nodeid not in selected_set])
        items[:] = [item for item in items if item.nodeid in selected_set]

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_finish(self, session):
        yield
        if session.testsfailed or not self.data["selected"]:
            # Collection can be interrupted before modifyitems runs at all.
            return
        require([item.nodeid for item in session.items] == self.data["selected"],
                "final collection differs from selected shard")

    def pytest_runtest_logreport(self, report):
        row = {"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome}
        if hasattr(report, "wasxfail"):
            row["wasxfail"] = str(report.wasxfail)
        self.data["reports"].append(row)

    def pytest_runtest_logfinish(self, nodeid):
        self.data["finished"].append(nodeid)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session):
        yield
        self.data["pytest_session_exit"] = int(session.exitstatus)
        self.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=True) + "\n", encoding="utf-8")
        temporary.replace(self.report)


def verify(args):
    require(args.needs_result == "success", "Windows shard jobs did not all succeed")
    require(args.count == 2 and args.indices == [0, 1], "expected shard indices 0 and 1")
    paths = [args.reports_root / str(index) / "shard.json" for index in args.indices]
    require({p for p in args.reports_root.rglob("*") if p.is_file()} == set(paths),
            "missing or extra shard artifacts")
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    full = documents[0]["full_collection"]
    groups = partition(full, args.count)
    for index, doc in enumerate(documents):
        require(isinstance(doc, dict) and type(doc["version"]) is int and doc["version"] == 1,
                "unsupported shard evidence version")
        expected = {"checkout_sha": args.sha, "github_sha": args.sha,
                    "run_id": args.run_id, "run_attempt": args.run_attempt,
                    "shard_index": index, "shard_count": args.count, "pytest_session_exit": 0}
        require(all(type(doc[key]) is type(value) and doc[key] == value
                    for key, value in expected.items()), "shard identity or pytest exit mismatch")
        require(doc["full_collection"] == full, "full collection differs between runners")
        selected = groups[index]
        require(doc["selected"] == selected and doc["finished"] == selected,
                "selected or finished nodes differ from the expected shard")
        require(isinstance(doc["reports"], list), "invalid phase reports")
        phases = {node: {} for node in selected}
        for row in doc["reports"]:
            require(isinstance(row, dict) and row["nodeid"] in phases
                    and row["when"] in ("setup", "call", "teardown")
                    and row["outcome"] in ("passed", "skipped"), "failed or invalid phase report")
            node_phases = phases[row["nodeid"]]
            require(row["when"] not in node_phases, "duplicate phase report")
            node_phases[row["when"]] = row["outcome"]
        for node_phases in phases.values():
            expected_phases = ({"setup", "teardown"} if node_phases.get("setup") == "skipped"
                               else {"setup", "call", "teardown"})
            require(set(node_phases) == expected_phases, "missing or unexpected phase report")
    print(f"Verified {len(full)} collected nodes across two Windows shards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("--reports-root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--indices", type=int, nargs="+", required=True)
    parser.add_argument("--needs-result", required=True)
    try:
        verify(parser.parse_args())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"CI shard verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
