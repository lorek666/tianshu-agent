"""Enable the timing hooks for exactly the existing smoke test's children."""

import os
from pathlib import Path

import pytest

TRACE_ENV = "TIANSHU_PRIVATE_APPROVAL_TIMING_OUT"
ISOLATION_HOOK = "TIANSHU_PRIVATE_APPROVAL_ISOLATION_HOOK"
TARGET = "tests/test_slo_and_soak.py::test_measure_slo_smoke_and_run_soak_real_subprocesses"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.nodeid != TARGET:
        yield
        return

    output = Path(os.environ["TIANSHU_APPROVAL_DIAGNOSTIC_OUTPUT"]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = Path(item.config.rootpath) / "tests" / "_startup" / "sitecustomize.py"
    hook_dir = Path(__file__).resolve().parent / "hooks"
    assert original.is_file() and str(original.parent) in os.environ["PYTHONPATH"].split(os.pathsep)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(TRACE_ENV, str(output))
        mp.setenv(ISOLATION_HOOK, str(original))
        mp.setenv("PYTHONPATH", str(hook_dir), prepend=os.pathsep)
        yield
