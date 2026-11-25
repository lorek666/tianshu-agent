from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_artifacts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ci_artifacts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact(name, *, expired=False):
    return {"name": name, "expired": expired}


def test_resolves_latest_nonexpired_attempt_for_each_exact_shard():
    module = load_module()
    artifacts = [
        artifact("macos-pytest-123-1-0"),
        artifact("macos-pytest-123-3-0"),
        artifact("macos-pytest-123-2-1"),
        artifact("macos-pytest-123-9-1", expired=True),
        artifact("macos-pytest-123-4-10"),
        artifact("macos-pytest-123-5-01"),
        artifact("macos-pytest-124-8-0"),
        artifact("windows-pytest-123-8-0"),
        artifact("macos-slo-123-8-0"),
        {"name": None, "expired": False},
    ]
    resolved = module.resolve_artifacts(
        artifacts, run_id="123", prefix="macos-pytest", indices=[0, 1],
    )
    assert resolved == {
        "0": {"name": "macos-pytest-123-3-0", "attempt": 3},
        "1": {"name": "macos-pytest-123-2-1", "attempt": 2},
    }


def test_missing_shard_is_an_error_instead_of_falling_back_to_current_attempt():
    module = load_module()
    with pytest.raises(ValueError, match="shard 1"):
        module.resolve_artifacts(
            [artifact("macos-pytest-123-1-0")],
            run_id="123", prefix="macos-pytest", indices=[0, 1],
        )


def test_duplicate_artifact_names_for_one_attempt_are_rejected():
    module = load_module()
    with pytest.raises(ValueError, match="duplicate"):
        module.resolve_artifacts(
            [artifact("macos-pytest-123-1-0"), artifact("macos-pytest-123-1-0")],
            run_id="123", prefix="macos-pytest", indices=[0],
        )


def test_github_output_keeps_names_and_explicit_attempt_map(tmp_path):
    module = load_module()
    output = tmp_path / "github-output"
    resolved = {
        "0": {"name": "macos-pytest-123-1-0", "attempt": 1},
        "1": {"name": "macos-pytest-123-2-1", "attempt": 2},
    }
    module.write_github_output(resolved, output)
    assert output.read_text(encoding="utf-8").splitlines() == [
        "shard_0_name=macos-pytest-123-1-0",
        "shard_0_attempt=1",
        "shard_1_name=macos-pytest-123-2-1",
        "shard_1_attempt=2",
        'attempts={"0": "1", "1": "2"}',
    ]


def test_windows_pair_artifact_attempt_covers_both_worker_reports(tmp_path):
    module = load_module()
    output = tmp_path / "github-output"
    resolved = {
        "0": {"name": "windows-pytest-123-1-0", "attempt": 1},
        "1": {"name": "windows-pytest-123-2-1", "attempt": 2},
    }
    module.write_github_output(resolved, output, reports_per_artifact=2)
    assert output.read_text(encoding="utf-8").splitlines()[-1] == (
        'attempts={"0": "1", "1": "1", "2": "2", "3": "2"}'
    )


def test_artifact_api_uses_run_scoped_paginated_request(monkeypatch):
    module = load_module()
    requests = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"artifacts": [artifact("macos-pytest-123-1-0")]}).encode()

    def fake_urlopen(request, *, timeout):
        assert timeout == 30
        requests.append(request)
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    result = module.fetch_artifacts("owner/repo", "123", "test-token")
    assert result == [artifact("macos-pytest-123-1-0")]
    assert len(requests) == 1
    assert requests[0].full_url == (
        "https://api.github.com/repos/owner/repo/actions/runs/123/artifacts?per_page=100&page=1"
    )
    assert requests[0].get_header("Authorization") == "Bearer test-token"
    assert requests[0].get_header("X-github-api-version") == "2022-11-28"


@pytest.mark.parametrize("payload", [None, [], {}, {"artifacts": {}}, {"artifacts": [None]}])
def test_artifact_api_rejects_malformed_external_responses(monkeypatch, payload):
    module = load_module()

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda request, *, timeout: Response(),
    )
    with pytest.raises(ValueError, match="invalid GitHub artifact"):
        module.fetch_artifacts("owner/repo", "123", "test-token")
