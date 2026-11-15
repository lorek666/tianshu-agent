from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_matrix_uses_native_architecture_runners():
    workflow = WORKFLOW.read_text(encoding="utf-8")
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
