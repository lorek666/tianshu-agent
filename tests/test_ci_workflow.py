from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
from test_release_workflow import _bash

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
READY_GUARD = (
    "github.event_name == 'push' || github.event.pull_request.draft == false"
)


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job_block(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match, f"missing CI job {name}"
    return match.group("body")


def _job_names(workflow: str) -> list[str]:
    _, marker, jobs = workflow.partition("\njobs:\n")
    assert marker, "missing jobs section"
    return re.findall(r"(?m)^  ([a-zA-Z0-9_-]+):\s*$", jobs)


def _assert_ready_pr_guard(workflow: str) -> None:
    trigger = re.search(
        r"(?ms)^  pull_request:\n(?P<body>.*?)(?=^[A-Za-z])",
        workflow,
    )
    assert trigger, "missing pull_request trigger"
    types_line = re.search(r"(?m)^    types:\s*\[(?P<types>[^\]]+)\]", trigger["body"])
    assert types_line, "pull_request must declare explicit activity types"
    assert "ready_for_review" in {
        item.strip() for item in types_line["types"].split(",")
    }

    job_names = _job_names(workflow)
    assert job_names == [
        "test",
        "macos_python",
        "desktop_macos",
        "macos_summary",
        "windows_shards",
        "desktop_windows",
        "windows_summary",
        "ui",
    ]

    for name in job_names:
        block = _job_block(workflow, name)
        if name in {"windows_summary", "macos_summary"}:
            expected = f"if: always() && ({READY_GUARD})"
        else:
            expected = f"if: {READY_GUARD}"
        job_guards = [
            line.strip()
            for line in block.splitlines()
            if line.startswith("    if:")
        ]
        assert job_guards == [expected], f"{name} must not run for a draft PR"


def test_pull_request_matrix_starts_only_after_ready_for_review():
    _assert_ready_pr_guard(_workflow())


def test_ready_pr_gate_cannot_lose_the_ready_for_review_event():
    workflow = _workflow().replace(", ready_for_review", "")
    with pytest.raises(AssertionError, match="ready_for_review"):
        _assert_ready_pr_guard(workflow)


def test_ready_pr_gate_cannot_leave_any_runner_job_unguarded():
    workflow = _workflow()
    block = _job_block(workflow, "windows_shards")
    unguarded_block = block.replace(f"    if: {READY_GUARD}\n", "", 1)
    assert unguarded_block != block
    with pytest.raises(AssertionError, match="windows_shards"):
        _assert_ready_pr_guard(workflow.replace(block, unguarded_block, 1))


def test_ready_pr_gate_rejects_a_new_unguarded_runner_job():
    workflow = _workflow()
    rogue = """
  rogue_runner:
    runs-on: ubuntu-latest
    steps:
      - run: echo unguarded
"""
    with pytest.raises(AssertionError):
        _assert_ready_pr_guard(workflow.rstrip() + rogue)


@pytest.mark.parametrize("summary", ["windows_summary", "macos_summary"])
def test_required_summaries_keep_always_and_draft_gate(summary):
    workflow = _workflow()
    block = _job_block(workflow, summary)
    combined = f"if: always() && ({READY_GUARD})"

    with pytest.raises(AssertionError, match=summary):
        _assert_ready_pr_guard(workflow.replace(block, block.replace(combined, "if: always()"), 1))

    with pytest.raises(AssertionError, match=summary):
        _assert_ready_pr_guard(
            workflow.replace(block, block.replace(combined, f"if: {READY_GUARD}"), 1)
        )


def _assert_macos_complete_native_gates(workflow):
    ubuntu = _job_block(workflow, "test")
    assert "- ubuntu-latest" in ubuntu
    assert "- macos-latest" not in ubuntu
    assert "cargo " not in ubuntu
    python = _job_block(workflow, "macos_python")
    desktop = _job_block(workflow, "desktop_macos")
    for block in (python, desktop):
        assert "runs-on: macos-latest" in block
        assert "timeout-minutes: 25" in block
        assert "ref: ${{ github.sha }}" in block
    for command in (
        "uv python install 3.12", "uv lock --check", "uv sync --frozen",
        "uv run --frozen ruff check .",
        'scripts/check_licenses_and_sbom.py --out "${RUNNER_TEMP}/tianshu-sbom.cdx.json"',
        'uv run --frozen pytest -o addopts="" -q -rs',
    ):
        assert command in python
    assert "cargo " not in python
    assert "working-directory: desktop/src-tauri" in desktop
    for command in (
        "cargo fmt --check", "cargo test --locked",
        "cargo clippy --locked --all-targets -- -D warnings",
    ):
        assert command in desktop
    summary = _job_block(workflow, "macos_summary")
    assert "timeout-minutes: 25" in summary
    assert "name: test (macos-latest) · py3.12" in summary
    assert "needs: [macos_python, desktop_macos]" in summary
    assert "${{ needs.macos_python.result }}" in summary
    assert "${{ needs.desktop_macos.result }}" in summary


def test_macos_python_and_rust_keep_complete_native_gates_separately():
    _assert_macos_complete_native_gates(_workflow())


@pytest.mark.parametrize("job", ["macos_python", "desktop_macos"])
@pytest.mark.parametrize("replacement", ["", "          ref: main\n"])
def test_macos_native_jobs_cannot_lose_exact_checkout(job, replacement):
    workflow = _workflow()
    block = _job_block(workflow, job)
    changed = block.replace("          ref: ${{ github.sha }}\n", replacement, 1)
    assert changed != block
    with pytest.raises(AssertionError):
        _assert_macos_complete_native_gates(workflow.replace(block, changed, 1))


@pytest.mark.parametrize("python_result", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("desktop_result", ["success", "failure", "cancelled", "skipped"])
def test_macos_required_summary_shell_rejects_any_unsuccessful_upstream(
    python_result, desktop_result,
):
    block = _job_block(_workflow(), "macos_summary")
    match = re.search(r"(?m)^        run: \|\n((?:          .*\n|\n)+)", block)
    assert match is not None
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-c", "set -euo pipefail\n" + dedent(match[1])],
        env={**os.environ, "PYTHON_RESULT": python_result, "DESKTOP_RESULT": desktop_result},
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) is (python_result == desktop_result == "success")


def _assert_state_pressure_uploads(workflow):
    for job, upload_name, artifact, diagnostics in (
        (
            "macos_python",
            "上传 state 背压诊断证据",
            "macos-slo-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}",
            "${{ runner.temp }}/macos-slo-diagnostics",
        ),
        (
            "windows_shards",
            "上传 Windows SLO 原始样本与失败诊断",
            "windows-slo-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}",
            "${{ runner.temp }}/windows-ci/slo-diagnostics",
        ),
    ):
        block = _job_block(workflow, job)
        assert f"TIANSHU_SLO_DIAGNOSTICS_DIR: {diagnostics}" in block
        upload = re.search(
            rf"(?ms)^      - name: {re.escape(upload_name)}\n(?P<body>.*?)(?=^      - |\Z)",
            block,
        )
        assert upload, f"{job} must upload state-pressure evidence"
        body = upload["body"]
        assert "if: always()" in body
        assert "uses: actions/upload-artifact@v4" in body
        assert f"name: {artifact}" in body
        assert f"path: {diagnostics}" in body
        assert "if-no-files-found: warn" in body


def test_state_pressure_evidence_is_uploaded_even_when_pytest_fails():
    _assert_state_pressure_uploads(_workflow())


@pytest.mark.parametrize("old,new", [
    (
        "      - name: 上传 state 背压诊断证据\n        if: always()",
        "      - name: 上传 state 背压诊断证据\n        if: success()",
    ),
    (
        "          path: ${{ runner.temp }}/macos-slo-diagnostics",
        "          path: ${{ runner.temp }}/macos-ci",
    ),
    (
        "          name: macos-slo-${{ github.run_id }}-${{ github.run_attempt }}-"
        "${{ matrix.shard }}",
        "          name: macos-slo-${{ github.run_id }}-${{ github.run_attempt }}",
    ),
    (
        "          path: ${{ runner.temp }}/windows-ci/slo-diagnostics",
        "          path: ${{ runner.temp }}/windows-ci/reports",
    ),
])
def test_state_pressure_upload_regressions_are_rejected(old, new):
    workflow = _workflow()
    assert old in workflow
    with pytest.raises(AssertionError):
        _assert_state_pressure_uploads(workflow.replace(old, new, 1))


def _assert_rerun_artifact_resolution(workflow):
    for summary, prefix, count in (
        ("macos_summary", "macos-pytest", 2),
        ("windows_summary", "windows-pytest", 4),
    ):
        block = _job_block(workflow, summary)
        artifact_count = count
        assert "id: shard_artifacts" in block
        assert "GITHUB_TOKEN: ${{ github.token }}" in block
        assert "uv run --frozen python scripts/ci_artifacts.py" in block
        assert '--repository "$GITHUB_REPOSITORY"' in block
        assert '--run-id "$GITHUB_RUN_ID"' in block
        assert f'--prefix {prefix} --indices {" ".join(map(str, range(artifact_count)))}' in block
        assert "--reports-per-artifact" not in block
        assert '--github-output "$GITHUB_OUTPUT"' in block
        assert "--run-attempts '${{ steps.shard_artifacts.outputs.attempts }}'" in block
        for index in range(artifact_count):
            assert (
                "name: ${{ steps.shard_artifacts.outputs.shard_"
                f"{index}_name }}}}\n          path: "
                "${{ runner.temp }}/" + ("macos" if count == 2 else "windows")
                + f"-ci-shards/{index}"
            ) in block
        current_attempt_name = (
            f"name: {prefix}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-"
        )
        assert current_attempt_name not in block


def test_required_summaries_resolve_actual_upstream_artifact_attempts():
    _assert_rerun_artifact_resolution(_workflow())


@pytest.mark.parametrize("old,new", [
    (
        "--run-attempts '${{ steps.shard_artifacts.outputs.attempts }}'",
        '--run-attempt "$GITHUB_RUN_ATTEMPT"',
    ),
    (
        "name: ${{ steps.shard_artifacts.outputs.shard_0_name }}",
        "name: macos-pytest-${{ github.run_id }}-${{ github.run_attempt }}-0",
    ),
    (
        "name: ${{ steps.shard_artifacts.outputs.shard_1_name }}\n"
        "          path: ${{ runner.temp }}/windows-ci-shards/1",
        "name: windows-pytest-${{ github.run_id }}-${{ github.run_attempt }}-1\n"
        "          path: ${{ runner.temp }}/windows-ci-shards/1",
    ),
])
def test_required_summary_rejects_current_attempt_artifact_assumption(old, new):
    workflow = _workflow()
    assert old in workflow
    with pytest.raises(AssertionError):
        _assert_rerun_artifact_resolution(workflow.replace(old, new, 1))


def test_main_push_ci_is_not_made_cancellable_or_draft_gated():
    workflow = _workflow()
    assert re.search(r"(?m)^  push:\n    branches: \[main\]$", workflow)
    assert (
        "group: ${{ github.workflow }}-${{ github.event_name == "
        "'pull_request' && github.ref || github.sha }}"
    ) in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert READY_GUARD in workflow


def test_main_push_concurrency_regression_detects_fixed_group():
    workflow = _workflow()
    guard = (
        "group: ${{ github.workflow }}-${{ github.event_name == "
        "'pull_request' && github.ref || github.sha }}"
    )
    assert guard in workflow
    with pytest.raises(AssertionError):
        assert guard in workflow.replace(guard, "group: ${{ github.workflow }}")


def test_pr_template_records_the_candidate_freeze_contract():
    template = PR_TEMPLATE.read_text(encoding="utf-8")
    for required in (
        "开发迭代期间 PR 为 draft",
        "已同步最新 `origin/main`",
        "全量本地门禁只对最终代码树运行一次",
        "最终复审记录 head SHA/tree 与 base SHA",
        "required CI 记录 head/base 和 event merge SHA",
    ):
        assert required in template


def _assert_windows_independent_shards(workflow):
    workers = _job_block(workflow, "windows_shards")
    summary = _job_block(workflow, "windows_summary")
    assert "runs-on: windows-latest" in workers
    assert "shard: [0, 1, 2, 3]" in workers
    assert "timeout-minutes: 25" in workers
    assert "fail-fast: false" in workers
    assert "ref: ${{ github.sha }}" in workers
    assert "continue-on-error" not in workers + summary
    assert "--ci-pair-" not in workers
    assert "CI_SHARD_INDEX: ${{ matrix.shard }}" in workers
    assert "CI_SHARD_REPORT: ${{ runner.temp }}/windows-ci/shard.json" in workers
    assert (
        "TIANSHU_SLO_DIAGNOSTICS_DIR: ${{ runner.temp }}/windows-ci/slo-diagnostics"
        in workers
    )
    assert (
        "      - name: 上传 Windows SLO 原始样本与失败诊断\n"
        "        if: always()\n"
        "        uses: actions/upload-artifact@v4\n"
        "        with:\n"
        "          name: windows-slo-${{ github.run_id }}-${{ github.run_attempt }}-"
        "${{ matrix.shard }}\n"
        "          path: ${{ runner.temp }}/windows-ci/slo-diagnostics\n"
        "          if-no-files-found: warn\n"
    ) in workers
    assert (
        "uv run --frozen python scripts/pytest_with_stacks.py -p pytest_ci_shards\n"
        "          --ci-shard-index=${{ matrix.shard }} --ci-shard-count=4\n"
        "          --ci-shard-report=\"$CI_SHARD_REPORT\""
    ) in workers
    assert '--stack-timeout=60' in workers
    assert "path: ${{ runner.temp }}/windows-ci/shard.json\n" in workers
    assert "if-no-files-found: error" in workers
    assert "needs: [windows_shards, desktop_windows]" in summary
    assert "permissions:\n      contents: read\n      actions: read" in summary
    assert "--prefix windows-pytest --indices 0 1 2 3" in summary
    assert "--reports-per-artifact" not in summary
    assert "--count 4 --indices 0 1 2 3" in summary
    for index in range(4):
        assert (
            "name: ${{ steps.shard_artifacts.outputs.shard_"
            f"{index}_name }}}}\n          path: "
            "${{ runner.temp }}/windows-ci-shards/" + str(index)
        ) in summary


def test_windows_independent_runners_isolate_slo_and_keep_required_gates():
    _assert_windows_independent_shards(_workflow())


@pytest.mark.parametrize("old,new", [
    ("--count 4 --indices 0 1 2 3", "--count 2 --indices 0 1"),
    ("windows-ci-shards/1", "windows-ci-shards/0"),
    ("shard: [0, 1, 2, 3]", "shard: [0, 1]"),
    ("--ci-shard-count=4", "--ci-shard-count=2"),
    ("--prefix windows-pytest --indices 0 1 2 3", "--prefix windows-pytest --indices 0 1"),
    (
        "permissions:\n      contents: read\n      actions: read",
        "permissions:\n      contents: read",
    ),
    (
        "--ci-shard-report=\"$CI_SHARD_REPORT\"",
        "--ci-pair-index=${{ matrix.shard }} --ci-pair-root=\"$CI_SHARD_REPORT\"",
    ),
    ("timeout-minutes: 25", "timeout-minutes: 30"),
    ("--stack-timeout=60", "--stack-timeout=0"),
    (
        "CI_SHARD_INDEX: ${{ matrix.shard }}",
        "CI_SHARD_INDEX: 0",
    ),
    (
        "TIANSHU_SLO_DIAGNOSTICS_DIR: ${{ runner.temp }}/windows-ci/slo-diagnostics",
        'TIANSHU_SLO_DIAGNOSTICS_DIR: ""',
    ),
    (
        "      - name: 上传 Windows SLO 原始样本与失败诊断\n        if: always()",
        "      - name: 上传 Windows SLO 原始样本与失败诊断\n        if: success()",
    ),
])
def test_windows_independent_runner_guard_rejects_weakened_workflow(old, new):
    workflow = _workflow()
    assert old in workflow
    with pytest.raises(AssertionError):
        _assert_windows_independent_shards(workflow.replace(old, new))
