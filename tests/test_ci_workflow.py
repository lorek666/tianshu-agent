from __future__ import annotations

import re
from pathlib import Path

import pytest

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
        "windows_shards",
        "desktop_windows",
        "windows_summary",
        "ui",
    ]

    for name in job_names:
        block = _job_block(workflow, name)
        if name == "windows_summary":
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


def test_windows_summary_keeps_always_and_draft_gate():
    workflow = _workflow()
    combined = f"if: always() && ({READY_GUARD})"

    with pytest.raises(AssertionError, match="windows_summary"):
        _assert_ready_pr_guard(workflow.replace(combined, "if: always()"))

    with pytest.raises(AssertionError, match="windows_summary"):
        _assert_ready_pr_guard(workflow.replace(combined, f"if: {READY_GUARD}"))


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
