"""Run the actual macOS CI commands and require complete native shard evidence."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from textwrap import dedent

import pytest
from test_ci_workflow import _job_block, _workflow
from test_pytest_ci_shards import FILES, SCRIPTS, project
from test_release_workflow import _bash


def _step(block, name):
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n(.*?)(?=^      - |\Z)", block,
    )
    assert match, f"missing step {name}"
    return match[1]


def _command(step):
    match = re.search(r"(?m)^        run: >-\n((?:          .*\n|\n)+)", step)
    assert match, "missing folded command"
    return " ".join(dedent(match[1]).splitlines())


def _assert_macos_shards(workflow):
    workers = _job_block(workflow, "macos_python")
    summary = _job_block(workflow, "macos_summary")
    assert "shard: [0, 1]" in workers
    assert "fail-fast: false" in workers
    assert "name: macos python shard ${{ matrix.shard }} · py3.12" in workers
    for block in (workers, summary):
        assert "timeout-minutes: 25" in block
        assert "ref: ${{ github.sha }}" in block
        assert "continue-on-error" not in block
    pytest_step = _step(workers, "Pytest")
    assert 'PYTHONPATH: scripts' in pytest_step
    assert 'PYTEST_ADDOPTS: ""' in pytest_step
    assert 'CI_SHARD_INDEX: ${{ matrix.shard }}' in pytest_step
    assert 'CI_SHARD_ROOT: ${{ runner.temp }}/macos-ci' in pytest_step
    command = _command(pytest_step)
    assert command.startswith('uv run --frozen pytest -o addopts="" -q -rs ')
    for option in (
        '-p pytest_ci_shards', '--ci-shard-index="$CI_SHARD_INDEX"',
        '--ci-shard-count=2', '--ci-shard-report="$CI_SHARD_ROOT/shard.json"',
        '--basetemp="$CI_SHARD_ROOT-runtime"',
        '-o cache_dir="$CI_SHARD_ROOT-cache"',
    ):
        assert option in command
    assert "pytest_with_stacks" not in workers
    upload = _step(workers, "上传 macOS 分片原始报告")
    assert "if: always()" in upload
    assert "uses: actions/upload-artifact@v4" in upload
    assert (
        "name: macos-pytest-${{ github.run_id }}-${{ github.run_attempt }}-"
        "${{ matrix.shard }}"
    ) in upload
    assert "path: ${{ runner.temp }}/macos-ci/shard.json" in upload
    assert "if-no-files-found: error" in upload
    assert "needs: [macos_python, desktop_macos]" in summary
    for index in range(2):
        download = _step(summary, f"下载 macOS 分片 {index}")
        assert "if: always()" in download
        assert "uses: actions/download-artifact@v4" in download
        assert (
            "name: macos-pytest-${{ github.run_id }}-${{ github.run_attempt }}-"
            f"{index}\n          path: "
            "${{ runner.temp }}/macos-ci-shards/" + str(index)
        ) in download
    verifier = _step(summary, "验证 macOS 上游结论与完整覆盖")
    assert "if: always()" in verifier
    assert "SHARD_REPORTS: ${{ runner.temp }}/macos-ci-shards" in verifier
    assert "UPSTREAM_RESULT: ${{ needs.macos_python.result }}" in verifier
    command = _command(verifier)
    for option in (
        'uv run --frozen python scripts/pytest_ci_shards.py verify',
        '--reports-root "$SHARD_REPORTS"', '--sha "$GITHUB_SHA"',
        '--run-id "$GITHUB_RUN_ID"', '--run-attempt "$GITHUB_RUN_ATTEMPT"',
        '--count 2 --indices 0 1', '--needs-result "$UPSTREAM_RESULT"',
    ):
        assert option in command


def test_macos_shards_keep_native_coverage_identity_and_strict_required_summary():
    _assert_macos_shards(_workflow())


@pytest.mark.parametrize("old,new", [
    ("shard: [0, 1]", "shard: [0]"),
    ("fail-fast: false", "fail-fast: true"),
    ("--ci-shard-count=2", "--ci-shard-count=4"),
    ('--ci-shard-report="$CI_SHARD_ROOT/shard.json"', ""),
    ('--basetemp="$CI_SHARD_ROOT-runtime"', ""),
    ('-o cache_dir="$CI_SHARD_ROOT-cache"', ""),
    ("if-no-files-found: error", "if-no-files-found: warn"),
    ("macos-ci-shards/1", "macos-ci-shards/0"),
    ("--count 2 --indices 0 1", "--count 2 --indices 0"),
    ('--needs-result "$UPSTREAM_RESULT"', '--needs-result success'),
    ("timeout-minutes: 25", "timeout-minutes: 30"),
    ("ref: ${{ github.sha }}", "ref: main"),
])
def test_macos_shard_guard_rejects_weakened_workflow(old, new):
    workflow = _workflow()
    assert old in workflow
    with pytest.raises(AssertionError):
        _assert_macos_shards(workflow.replace(old, new))


def test_actual_macos_commands_execute_complete_shards_and_reject_false_green(tmp_path):
    workflow = _workflow()
    _assert_macos_shards(workflow)
    command = _command(_step(_job_block(workflow, "macos_python"), "Pytest"))
    assert command.startswith("uv run --frozen pytest ")
    # Keep every workflow pytest argument; only select this test interpreter.
    command = command.replace(
        "uv run --frozen pytest ", f'"{sys.executable}" -m pytest ', 1,
    )
    sources = {
        **FILES,
        "tests/test_ci_shards.py": '''
import os
def test_private_roots_and_native_pytest(pytestconfig):
    root = os.environ["CI_SHARD_ROOT"]
    assert str(pytestconfig._tmp_path_factory.getbasetemp()) == root + "-runtime"
    assert str(pytestconfig.cache._cachedir) == root + "-cache"
    assert pytestconfig.pluginmanager.hasplugin("faulthandler")
    assert not pytestconfig.pluginmanager.hasplugin("pytest-python-stack-watchdog")
    assert not any(t.name == "pytest-python-stack-watchdog"
                   for t in __import__("threading").enumerate())
''',
    }
    root = tmp_path / "reports"
    root.mkdir()
    runtime = tmp_path / "native"
    runtime.mkdir()
    documents, children = [], []
    first_env = None
    try:
        for index in range(2):
            checkout = tmp_path / f"checkout-{index}"
            env = project(checkout, sources)
            env.update(CI_SHARD_INDEX=str(index),
                       CI_SHARD_ROOT=str(runtime / str(index)),
                       PYTHONPATH=str(SCRIPTS))
            if index:
                assert first_env is not None
                assert env["GITHUB_SHA"] == first_env["GITHUB_SHA"]
            else:
                first_env = dict(env)
            # Separate checkout/process/runtime roots, as on the two runners.
            children.append(subprocess.Popen(
                [_bash(), "--noprofile", "--norc", "-c", "set -euo pipefail\n" + command],
                cwd=checkout, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            ))
        for index, child in enumerate(children):
            output, _ = child.communicate(timeout=30)
            assert child.returncode == 0, output.decode("utf-8", errors="replace")
            original = runtime / str(index) / "shard.json"
            documents.append(json.loads(original.read_text()))
            # upload-artifact selects the file; summary downloads keep each
            # named artifact separate and contain no worker cache/runtime data.
            target = root / str(index) / "shard.json"
            target.parent.mkdir()
            shutil.copyfile(original, target)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert documents[0]["full_collection"] == documents[1]["full_collection"]
    full = [node for node, _ in documents[0]["full_collection"]]
    assert sorted(node for doc in documents for node in doc["finished"]) == sorted(full)
    assert all(doc["selected"] == doc["finished"] and doc["reports"] for doc in documents)
    verify = _command(_step(
        _job_block(workflow, "macos_summary"), "验证 macOS 上游结论与完整覆盖",
    ))
    prefix = "uv run --frozen python scripts/pytest_ci_shards.py"
    assert verify.startswith(prefix)
    verify = verify.replace(prefix, f'"{sys.executable}" "{SCRIPTS / "pytest_ci_shards.py"}"', 1)
    assert first_env is not None
    env = {**first_env, "SHARD_REPORTS": str(root), "UPSTREAM_RESULT": "success"}

    def check(expected):
        result = subprocess.run(
            [_bash(), "--noprofile", "--norc", "-c", "set -euo pipefail\n" + verify],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
        )
        assert (result.returncode == 0) is expected, result.stdout + result.stderr

    check(True)
    for result in ("failure", "cancelled", "skipped"):
        env["UPSTREAM_RESULT"] = result
        check(False)
    env["UPSTREAM_RESULT"] = "success"
    report = root / "1" / "shard.json"
    original = report.read_bytes()
    report.unlink()
    check(False)
    report.write_bytes(original)
    extra = root / "unexpected.json"
    extra.write_text("{}")
    check(False)
    extra.unlink()
    for field, value in (
        ("github_sha", "f" * 40), ("checkout_sha", "f" * 40),
        ("run_id", "999"), ("run_attempt", "99"), ("shard_index", 0),
        ("shard_count", 4), ("finished", []), ("reports", []),
        ("pytest_session_exit", 1),
    ):
        changed = deepcopy(documents[1])
        changed[field] = value
        report.write_text(json.dumps(changed))
        check(False)
    report.write_bytes(original)
    check(True)
