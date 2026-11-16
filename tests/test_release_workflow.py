from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _assert_latest_run_is_selected_before_success_is_required(workflow: str) -> None:
    compact = re.sub(r"\s+", "", workflow)
    expected = (
        "|sort_by(.databaseId)|last"
        '|select(.!=nulland.status=="completed")'
        '|select(.conclusion=="success")'
        "|.databaseId//empty"
    )
    assert expected in compact


def _assert_draft_creation_does_not_reuse_existing_release(workflow: str) -> None:
    assert workflow.count("gh release create") == 1
    assert 'gh release create "v$VERSION"' in workflow
    assert '--target "$COMMIT"' in workflow
    assert "--draft" in workflow
    for forbidden in (
        "gh release edit",
        "gh release upload",
        "gh release delete",
        "--clobber",
        "--skip-existing",
        "--cleanup-tag",
        "--latest",
    ):
        assert forbidden not in workflow


def test_release_matrix_uses_native_architecture_runners():
    workflow = _workflow()
    expected = {
        "macos-arm64": "macos-latest",
        "macos-x64": "macos-26-intel",
        "windows-x64": "windows-latest",
        "windows-arm64": "windows-11-arm",
    }
    for target, runner in expected.items():
        pair = re.compile(
            rf"- os:\s+{re.escape(runner)}\s*\n\s+target:\s+{re.escape(target)}"
        )
        assert pair.search(workflow)
    assert "not_run_cross_target" not in workflow


def test_release_dispatch_requires_exact_current_main():
    workflow = _workflow()

    assert 'test "$commit" = "$(git rev-parse origin/main)"' in workflow
    assert "git merge-base --is-ancestor" not in workflow


def test_release_tag_is_dereferenced_before_identity_check():
    workflow = _workflow()

    assert 'test "$(git cat-file -t "$GITHUB_REF")" = "tag"' in workflow
    assert 'commit="$(git rev-parse "${GITHUB_REF}^{commit}")"' in workflow


def test_release_preflight_requires_latest_completed_exact_sha_ci_success():
    workflow = _workflow()

    assert "--workflow ci.yml" in workflow
    assert "--event push" in workflow
    assert '--commit "$COMMIT"' in workflow
    assert "sort_by(.databaseId)" in workflow
    assert 'select(. != null and .status == "completed")' in workflow
    assert 'select(.conclusion == "success")' in workflow
    assert 'and .status == "completed")]' not in workflow
    assert 'all(.jobs[]; .conclusion == "success")' in workflow


def test_release_preflight_order_cannot_reuse_older_success_after_new_failure():
    _assert_latest_run_is_selected_before_success_is_required(_workflow())

    stale_filter = """
    | jq -r '
        [.[] | select(.headSha == $sha and .conclusion == "success")]
        | sort_by(.databaseId)
        | last
        | .databaseId // empty
      '
    """
    with pytest.raises(AssertionError):
        _assert_latest_run_is_selected_before_success_is_required(stale_filter)


def test_release_artifacts_are_adjacent_to_native_build_jobs_and_downloaded():
    workflow = _workflow()

    for target in (
        "macos-arm64",
        "macos-x64",
        "windows-x64",
        "windows-arm64",
    ):
        assert f"name: release-{target}" in workflow
        assert "name: release-${{ matrix.target }}" in workflow
        assert f"path: ${{{{ runner.temp }}}}/artifacts/{target}" in workflow
    assert "needs: [preflight, build]" in workflow
    assert "scripts/assemble_release.py" in workflow
    assert '--artifacts-root "$RUNNER_TEMP/artifacts"' in workflow


def test_release_uses_real_tauri_nsis_output_and_draft_only_write_permission():
    workflow = _workflow()

    assert '--target-triple "$TRIPLE"' in workflow
    assert '--bundle-dir "$RUNNER_TEMP/bundle-$TARGET"' in workflow
    assert '--out-dir "$RUNNER_TEMP/release-$TARGET"' in workflow
    assert "scripts/verify_release.py" in workflow
    assert "release-verification.json" in workflow
    assert "contents: write" in workflow
    assert workflow.count("contents: write") == 1
    assert "gh release create" in workflow
    assert "--draft" in workflow
    assert '--target "$COMMIT"' in workflow


def test_release_creates_new_draft_without_reuse_or_overwrite_path():
    _assert_draft_creation_does_not_reuse_existing_release(_workflow())

    overwrite = _workflow().replace(
        'gh release create "v$VERSION"',
        'gh release edit "v$VERSION" --clobber',
        1,
    )
    with pytest.raises(AssertionError):
        _assert_draft_creation_does_not_reuse_existing_release(overwrite)


def test_release_workflow_has_no_failure_masking_or_retries():
    workflow = _workflow()

    for forbidden in (
        "continue-on-error",
        "|| true",
        "pytest.skip",
        "pytest.mark.xfail",
        "retry",
    ):
        assert forbidden not in workflow
