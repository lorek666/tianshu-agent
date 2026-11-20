"""Execute the workflow's actual draft shell against local Git and a fake gh.

These are command-plan boundary tests, not evidence of a hosted release run.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from test_release_workflow import _bash, _git, _step, _workflow


@pytest.fixture
def draft(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    for message in ("old candidate", "candidate"):
        _git(
            repo, "-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message,
        )
    _git(repo, "remote", "add", "origin", repo.as_posix())
    return repo


def _run_draft(repo: Path, *, preview=True, pages=None, refs="", api_exit=0):
    root = repo.parent
    output = root / "create.json"
    command_log = root / "commands.jsonl"
    fake_gh = root / "gh.py"
    fake_gh.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['COMMAND_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'api':\n"
        "    print(os.environ['RELEASE_PAGES'])\n"
        "    sys.exit(int(os.environ['API_EXIT']))\n"
        "if args[:2] == ['release', 'create']:\n"
        "    Path(os.environ['CREATE_OUTPUT']).write_text(json.dumps(args))\n"
        "    sys.exit(0)\n"
        "sys.exit(99)\n",
        encoding="utf-8",
    )
    commit = _git(repo, "rev-parse", "HEAD")
    body = dedent(_step(_workflow(), "Create draft GitHub release").split("run: |\n", 1)[1])
    # Override only the external gh transport. Git tag checks execute real Git.
    wrapper = 'gh() { "$TEST_PYTHON" "$TEST_GH" "$@"; }\n' + body
    paths = [root / "release" / name for name in ("release-index.json", "install-macos.sh")]
    for path in paths:
        path.parent.mkdir(exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    env = {
        **os.environ,
        "VERSION": "0.1.0", "COMMIT": commit,
        "PREVIEW": "true" if preview else "false",
        "DRAFT_PATHS": "\n".join(path.as_posix() for path in paths),
        "GITHUB_REPOSITORY": "example/repo", "GITHUB_REF_TYPE": "branch",
        "TEST_PYTHON": Path(sys.executable).as_posix(), "TEST_GH": fake_gh.as_posix(),
        "CREATE_OUTPUT": str(output), "COMMAND_LOG": str(command_log),
        "RELEASE_PAGES": json.dumps(pages if pages is not None else [[]]),
        "API_EXIT": str(api_exit),
    }
    # refs is a tag at HEAD or its parent; no remote services are contacted.
    if refs:
        _git(repo, "tag", "v0.1.0", refs)
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-c", wrapper], cwd=repo,
        env=env, capture_output=True, text=True, timeout=25,
    )
    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    created = json.loads(output.read_text()) if output.exists() else None
    return result, created, commands, paths


@pytest.mark.parametrize("preview", [True, False])
def test_draft_uses_only_given_index_paths_and_correct_labels(draft, preview):
    result, created, commands, paths = _run_draft(draft, preview=preview)
    assert result.returncode == 0, result.stderr
    assert created is not None
    assert created[:3] == ["release", "create", "v0.1.0"]
    assert created[3:5] == [path.as_posix() for path in paths]
    assert "--draft" in created
    assert created[created.index("--target") + 1] == _git(draft, "rev-parse", "HEAD")
    assert ("--prerelease" in created) is preview
    title = created[created.index("--title") + 1]
    assert ("Apple Silicon Preview" in title) is preview
    assert commands[0][:3] == ["api", "--paginate", "--slurp"]
    assert "--clobber" not in created


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("existing", ["draft", "prerelease", "public"])
def test_existing_release_on_any_page_stops_creation(draft, preview, existing):
    pages = [[{"tag_name": "v0.0.9"}], [{
        "tag_name": "v0.1.0", "draft": existing == "draft",
        "prerelease": existing == "prerelease",
    }]]
    result, created, _, _ = _run_draft(draft, preview=preview, pages=pages)
    assert result.returncode != 0
    assert created is None


@pytest.mark.parametrize("pages,api_exit", [([], 0), ({}, 0), ([{}], 0), ([[]], 1)])
def test_api_error_or_invalid_response_is_not_release_absence(draft, pages, api_exit):
    result, created, _, _ = _run_draft(draft, pages=pages, api_exit=api_exit)
    assert result.returncode != 0
    assert created is None


@pytest.mark.parametrize("refs", ["HEAD", "HEAD^1"])
def test_preview_refuses_any_existing_tag(draft, refs):
    result, created, _, _ = _run_draft(draft, refs=refs)
    assert result.returncode != 0
    assert created is None


def test_full_candidate_refuses_tag_pointing_at_other_commit(draft):
    result, created, _, _ = _run_draft(draft, preview=False, refs="HEAD^1")
    assert result.returncode != 0
    assert created is None


def test_full_candidate_allows_matching_existing_tag_with_verification(draft):
    result, created, _, _ = _run_draft(draft, preview=False, refs="HEAD")
    assert result.returncode == 0, result.stderr
    assert "--verify-tag" in created


def test_version_concurrency_serializes_tag_and_manual_paths():
    workflow = _workflow()
    match = re.search(r"^  group: (.+)$", workflow, re.MULTILINE)
    assert match is not None
    assert "inputs.version" in match.group(1)
    assert "github.ref_name" in match.group(1)


def _run_asset_list(root: Path, *, names=None, extra=None, directory=False):
    release = root / "release"
    release.mkdir()
    names = names if names is not None else ["install-macos.sh", "archive.app.tar.gz"]
    (release / "release-index.json").write_text(json.dumps({
        "assets": [{"name": name} for name in names],
    }))
    for name in names:
        if "/" not in name:
            (release / name).write_text("fixture")
    if extra:
        (release / extra).write_text("unexpected")
    if directory:
        (release / names[0]).unlink()
        (release / names[0]).mkdir()
    script = dedent(_step(_workflow(), "Assemble candidate").split("run: |\n", 1)[1])
    script = script[script.index("# Use the index"):]
    output = root / "outputs"
    env = {
        **os.environ, "GITHUB_OUTPUT": output.as_posix(),
        "RELEASE_DIR": release.as_posix(),
    }
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-c",
         'set -euo pipefail\nrelease_dir="$RELEASE_DIR"\n' + script],
        env=env, capture_output=True, text=True, timeout=15,
    )
    return result, output, release


def test_asset_list_emits_exact_indexed_files_and_index(tmp_path):
    result, output, release = _run_asset_list(tmp_path)
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        "paths<<RELEASE_PATHS",
        (release / "release-index.json").as_posix(),
        (release / "install-macos.sh").as_posix(),
        (release / "archive.app.tar.gz").as_posix(),
        "RELEASE_PATHS",
    ]


@pytest.mark.parametrize("extra", ["stale.exe", ".hidden"])
def test_asset_list_rejects_any_unindexed_file(tmp_path, extra):
    result, output, _ = _run_asset_list(tmp_path, extra=extra)
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("names", [[], ["../escape"], ["ok", "ok"]])
def test_asset_list_rejects_missing_unsafe_or_duplicate_names(tmp_path, names):
    result, _, _ = _run_asset_list(tmp_path, names=names)
    assert result.returncode != 0
    assert not (tmp_path / "escape").exists()


def test_asset_list_rejects_directories_instead_of_files(tmp_path):
    result, _, _ = _run_asset_list(tmp_path, directory=True)
    assert result.returncode != 0
