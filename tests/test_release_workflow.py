from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

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


def _step(workflow: str, name: str) -> str:
    match = re.search(
        rf"^      - name: {re.escape(name)}\n(.*?)(?=^ {{0,6}}\S|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, name
    return match.group(1)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args], encoding="utf-8", timeout=15,
    ).strip()


def _bash() -> str:
    if os.name == "nt":
        # Use Git for Windows' Bash, not a System32 WSL launcher.
        git = shutil.which("git")
        assert git is not None
        for parent in Path(git).parents:
            shell = parent / "bin" / "bash.exe"
            if shell.is_file():
                return str(shell)
        pytest.fail(f"Git Bash not found near {git}")
    shell = shutil.which("bash")
    assert shell is not None
    return shell


@pytest.fixture
def checkout(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--initial-branch=main")
    for message in ("previous main", "candidate main"):
        _git(
            origin, "-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message,
        )
    _git(
        origin, "-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
        "-c", "tag.gpgSign=false", "tag", "-am", "candidate", "v0.1.0",
    )
    _git(origin, "tag", "v0.1.1")
    _git(tmp_path, "clone", str(origin), "checkout")
    return tmp_path / "checkout"


def _run_identity(checkout: Path, **overrides: str):
    workflow = _workflow()
    body = _step(workflow, "Resolve and bind identity").split("        run: |\n", 1)[1]
    output = checkout.parent / "step-output"
    commit = _git(checkout, "rev-parse", "HEAD")
    env = {
        **os.environ,
        "GITHUB_OUTPUT": output.as_posix(),
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_NAME": "main",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_WORKFLOW_SHA": commit,
        "INPUT_VERSION": "0.1.0",
        "INPUT_COMMIT": commit,
        "INPUT_PREVIEW": "false",
        **overrides,
    }
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-c", dedent(body)],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=25,
    )
    outputs = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()
    ) if output.exists() else {}
    return result, outputs


@pytest.mark.parametrize("trigger", ["preview", "default", "tag"])
def test_release_identity_emits_only_selected_native_runners(checkout, trigger):
    workflow = _workflow()
    settings = {"INPUT_PREVIEW": "true"} if trigger == "preview" else {}
    if trigger == "tag":
        settings = {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/tags/v0.1.0",
            "GITHUB_REF_NAME": "v0.1.0",
            "GITHUB_REF_TYPE": "tag",
            "INPUT_PREVIEW": "true",  # only a manual dispatch can select preview
        }
    result, outputs = _run_identity(checkout, **settings)
    assert result.returncode == 0, result.stderr
    assert outputs["commit"] == _git(checkout, "rev-parse", "HEAD")
    assert outputs["version"] == "0.1.0"
    preview = trigger == "preview"
    assert outputs["preview"] == ("true" if preview else "false")
    matrix = json.loads(outputs["matrix"])
    native = [("macos-latest", "macos-arm64", "aarch64-apple-darwin")]
    if not preview:
        native += [
            ("macos-26-intel", "macos-x64", "x86_64-apple-darwin"),
            ("windows-latest", "windows-x64", "x86_64-pc-windows-msvc"),
            ("windows-11-arm", "windows-arm64", "aarch64-pc-windows-msvc"),
        ]
    assert matrix == {
        "include": [
            {"os": runner, "target": target, "triple": triple}
            for runner, target, triple in native
        ],
    }
    assert "matrix: ${{ fromJSON(needs.preflight.outputs.matrix) }}" in workflow
    assert "not_run_cross_target" not in workflow


@pytest.mark.parametrize(
    "overrides",
    [
        {"INPUT_PREVIEW": ""},
        {"INPUT_PREVIEW": "arm64"},
        {"GITHUB_REF": "refs/heads/feature", "INPUT_PREVIEW": "true"},
        {"GITHUB_WORKFLOW_SHA": "b" * 40, "INPUT_PREVIEW": "true"},
        {
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF_TYPE": "tag",
            "GITHUB_REF": "refs/tags/v0.1.1", "GITHUB_REF_NAME": "v0.1.1",
        },
    ],
)
def test_release_identity_refuses_invalid_preview_or_source(checkout, overrides):
    result, outputs = _run_identity(checkout, **overrides)
    assert result.returncode != 0
    assert not outputs


def test_release_identity_refuses_existing_main_ancestor(checkout):
    ancestor = _git(checkout, "rev-parse", "HEAD^1")
    result, outputs = _run_identity(checkout, INPUT_COMMIT=ancestor, INPUT_PREVIEW="true")
    assert result.returncode != 0
    assert not outputs


def test_release_dispatch_exposes_opt_in_preview():
    workflow = _workflow()
    assert re.search(r"^      macos_arm64_preview:\n(?:^ {8}[^\n]+\n)*^        type: boolean$",
                     workflow, re.MULTILINE)
    preview_input = workflow.split("      macos_arm64_preview:\n", 1)[1].split("  push:", 1)[0]
    assert "default: false" in preview_input
    assert "INPUT_PREVIEW: ${{ inputs.macos_arm64_preview }}" in workflow


def test_release_dispatch_requires_exact_current_main():
    workflow = _workflow()

    assert 'test "$commit" = "$(git rev-parse origin/main)"' in workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in workflow
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

    downloads = {
        "macos-arm64": "macOS arm64",
        "macos-x64": "macOS x64",
        "windows-x64": "Windows x64",
        "windows-arm64": "Windows arm64",
    }
    assert workflow.count("uses: actions/download-artifact@v4") == len(downloads)
    for target, label in downloads.items():
        download = _step(workflow, f"Download {label}")
        if target == "macos-arm64":
            assert "\n        if:" not in "\n" + download
        else:
            assert "if: needs.preflight.outputs.preview == 'false'" in download
        assert f"name: release-{target}" in download
        assert f"path: ${{{{ runner.temp }}}}/artifacts/{target}" in download
    assert "name: release-${{ matrix.target }}" in workflow
    assert "needs: [preflight, build]" in workflow
    assemble = _step(workflow, "Assemble candidate")
    assert "PREVIEW: ${{ needs.preflight.outputs.preview }}" in assemble
    assert 'if [[ "$PREVIEW" == "true" ]]; then' in assemble
    assert "--macos-arm64-preview" in assemble
    assert "scripts/assemble_release.py" in assemble
    assert '--artifacts-root "$RUNNER_TEMP/artifacts"' in assemble


def test_draft_only_attaches_assets_from_index():
    workflow = _workflow()
    assemble = _step(workflow, "Assemble candidate")
    assert "release-index.json" in assemble
    assert ".assets[].name" in assemble
    assert "paths<<RELEASE_PATHS" in assemble
    assert "path: ${{ steps.assemble.outputs.paths }}" in _step(
        workflow, "Upload complete candidate",
    )
    draft = _step(workflow, "Create draft GitHub release")
    assert "DRAFT_PATHS: ${{ steps.assemble.outputs.paths }}" in draft
    assert '"${files[@]}"' in draft
    assert "/release/*" not in workflow
    assert "--prerelease" in draft
    assert "Apple Silicon Preview" in draft


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


def test_release_checks_ui_dist_after_tauri_packaging():
    workflow = _workflow()
    package = workflow.index(
        "uv run --frozen python scripts/package_app.py"
    )
    ui_check = workflow.index("git diff --exit-code -- ui/dist")
    verify = workflow.index(
        "uv run --frozen python scripts/verify_release.py"
    )
    assert package < ui_check < verify


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


@pytest.mark.parametrize("latest", ["in_progress", "queued", "failure", "cancelled"])
def test_ci_selector_never_falls_back_to_older_success(tmp_path, latest):
    script = _step(_workflow(), "Require successful push CI for this exact commit")
    match = re.search(r"\| jq -r --arg sha \"\$COMMIT\" '(.+?)'\)", script, re.DOTALL)
    assert match is not None
    sha = "a" * 40
    runs = [
        {"databaseId": 1, "headSha": sha, "status": "completed", "conclusion": "success"},
        {
            "databaseId": 2, "headSha": sha,
            "status": latest if latest in {"in_progress", "queued"} else "completed",
            "conclusion": None if latest in {"in_progress", "queued"} else latest,
        },
    ]
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(runs), encoding="utf-8")
    result = subprocess.run(
        ["jq", "-r", "--arg", "sha", sha, match.group(1), str(path)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip()
    assert "--branch main" in script


def test_ci_selector_requires_all_required_names_and_all_jobs(tmp_path):
    script = _step(_workflow(), "Require successful push CI for this exact commit")
    filters = re.findall(r"jq -e '(.+?)' \"\$RUNNER_TEMP/ci-jobs.json\"", script, re.DOTALL)
    assert len(filters) == 2
    names = [
        "test (ubuntu-latest) · py3.12", "test (macos-latest) · py3.12",
        "test (windows-latest) · py3.12", "ui · typecheck / test / build",
    ]
    complete = [{"name": name, "conclusion": "success"} for name in names]
    cases = [
        (complete, True),
        (complete[:-1], False),
        ([*complete, {"name": "windows-shard", "conclusion": "failure"}], False),
        ([], False),
        *(
            ([*complete, {"name": name, "conclusion": conclusion}], False)
            for name in (
                "macos python shard 0 · py3.12", "macos python shard 1 · py3.12",
                "desktop rust checks · macos",
            )
            for conclusion in ("failure", "cancelled", "skipped")
        ),
    ]
    path = tmp_path / "jobs.json"
    for jobs, expected in cases:
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        results = [subprocess.run(
            ["jq", "-e", query, str(path)], capture_output=True, timeout=15,
        ) for query in filters]
        assert all(result.returncode == 0 for result in results) is expected
