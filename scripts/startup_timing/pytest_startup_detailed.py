"""Extend the repository's isolated startup smoke test with an external trace."""

import os
from pathlib import Path


def pytest_runtest_call(item):
    if item.name != "test_measure_slo_smoke_and_run_soak_real_subprocesses":
        return
    output = os.environ.get("TIANSHU_STARTUP_TIMING_DIR")
    if not output:
        return
    if not os.environ.get("TIANSHU_TEST_LOCK_ROOT") or not (
        os.environ.get("TIANSHU_TEST_REGISTRY_HOME")
        or os.environ.get("PYTEST_TIANSHU_REGISTRY_HOME")
    ):
        raise RuntimeError("maintenance lock and registry home fixtures are required")
    frozen = Path(item.config.rootpath)
    isolation = frozen / "tests" / "_startup" / "sitecustomize.py"
    probe = Path(__file__).with_name("trace_startup_detailed.py")
    patch = item.funcargs["monkeypatch"]
    staged = item.funcargs["tmp_path"] / "startup_hook"
    staged.mkdir()
    (staged / "sitecustomize.py").write_text(
        isolation.read_text(encoding="utf-8") + "\n\n"
        'if os.environ.get("TIANSHU_STARTUP_TIMING_DIR"):\n'
        "    import trace_startup_detailed\n",
        encoding="utf-8",
    )
    (staged / probe.name).write_bytes(probe.read_bytes())
    patch.setenv("PYTHONPATH", str(staged) + os.pathsep + os.environ["PYTHONPATH"])
