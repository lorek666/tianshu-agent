"""Internal probes must not import Python modules from the subject workspace."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tianshu.exec import LOCAL_EXEC_BACKEND
from tianshu.exec_backends import exec_backend
from tianshu.exec_seatbelt import seatbelt_available
from tianshu.execpolicy import ExecPolicy
from tianshu.execution_admission import PreparedExecution
from tianshu.isolation import _PROBE_PROCESS_TREE, _run_child


def _shadow_module(workspace: Path, name: str) -> Path:
    marker = workspace / f"{name}-imported"
    (workspace / f"{name}.py").write_text(
        f"open({str(marker)!r}, 'w').write('workspace module executed')\n"
        "raise RuntimeError('internal probe imported a workspace module')\n",
        encoding="utf-8",
    )
    return marker


@pytest.mark.parametrize("module", ["pathlib", "sitecustomize"])
def test_boundary_probe_ignores_workspace_and_pythonpath_modules(tmp_path, module):
    marker = _shadow_module(tmp_path, module)
    result, facts, problem = _run_child(
        LOCAL_EXEC_BACKEND,
        "import pathlib,sys;"
        "print('ISOLATED='+str(sys.flags.isolated));"
        "print('NO_SITE='+str(sys.flags.no_site));"
        "print('SAFE_PATH='+str(sys.flags.safe_path))",
        [],
        workspace=tmp_path,
        timeout_ms=3000,
        env={"PATH": os.defpath, "PYTHONPATH": str(tmp_path)},
        env_allowlist=("PATH", "PYTHONPATH"),
    )
    assert not marker.exists(), (result, facts, problem)
    assert not problem, problem
    assert facts == {"ISOLATED": "1", "NO_SITE": "1", "SAFE_PATH": "True"}


def test_process_probe_without_fork_also_isolates_its_child(tmp_path, monkeypatch):
    from tianshu import isolation

    # Run the real fallback on POSIX too. The audit event comes from a real
    # Popen before the child starts, so this needs no scheduling assumption.
    preamble = """
import os, sys
if hasattr(os, 'fork'):
    del os.fork
def observe(event, args):
    if event == 'subprocess.Popen':
        print('CHILD_ISOLATED=' + str('-I' in args[1] and '-S' in args[1]), flush=True)
sys.addaudithook(observe)
"""
    facts = {}

    def observe_result(*args, **kwargs):
        result, parsed, problem = _run_child(*args, **kwargs)
        facts.update(parsed)
        return result, parsed, problem

    monkeypatch.setattr(isolation, "_PROBE_PROCESS_TREE", preamble + _PROBE_PROCESS_TREE)
    monkeypatch.setattr(isolation, "_run_child", observe_result)
    # Use the full probe's finally cleanup: Windows local execution can kill
    # only the root, so a direct _run_child would leave the test child behind.
    finding = isolation._probe_process_tree(
        LOCAL_EXEC_BACKEND, tmp_path, tmp_path, 1000,
    )
    assert facts.get("CHILD_ISOLATED") == "True", (facts, finding)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="detached session probe is POSIX-only")
@pytest.mark.parametrize("module", ["json", "pathlib"])
def test_detached_probe_root_and_child_ignore_workspace_modules(tmp_path, module):
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    marker = _shadow_module(workspace, module)
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(workspace,),
        env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=65536,
    )
    with PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="command", task_id="task", run_id="run",
        argv=(sys.executable, "-c", "print('business')"),
        cwd=workspace, env={"PATH": os.defpath}, policy=policy,
        workspace=workspace, data_root=data,
    ) as execution:
        finding = execution._probe_detached_descendant()
    assert not marker.exists(), finding
    # Local execution really permits the detached child. Interpreter hardening
    # must not manufacture a denied-fork finding by breaking its imports.
    assert finding.status == "not_enforced", finding
    assert "清理=confirmed" in finding.evidence


@pytest.mark.parametrize("backend_name", ["local", "seatbelt_leaf"])
def test_prepared_business_command_keeps_workspace_import_semantics(tmp_path, backend_name):
    if backend_name == "seatbelt_leaf" and not seatbelt_available():
        pytest.skip("requires native Darwin Seatbelt")
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    (workspace / "business_module.py").write_text("VALUE = 'business-import'\n")
    argv = (
        sys.executable, "-c",
        "import business_module,sys;"
        "print(business_module.VALUE);print(sys.flags.isolated,sys.flags.no_site)",
    )
    with PreparedExecution(
        exec_backend(backend_name), kind="command", task_id="task", run_id="run",
        argv=argv, cwd=workspace, env={"PATH": os.defpath},
        policy=ExecPolicy(
            argv_allowlist=(sys.executable,), cwd_roots=(workspace,),
            env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=65536,
        ),
        workspace=workspace, data_root=data,
    ) as execution:
        execution.admit(() if backend_name == "local" else ("process_tree",))
        result = execution.run()
    assert result.exit_code == 0, result
    assert result.stdout.splitlines() == ["business-import", "0 0"]


@pytest.mark.skipif(not seatbelt_available(), reason="requires native Darwin Seatbelt")
def test_leaf_admission_does_not_execute_shadow_modules(tmp_path):
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    markers = [
        _shadow_module(workspace, module)
        for module in ("pathlib", "json", "sitecustomize")
    ]
    with PreparedExecution(
        exec_backend("seatbelt_leaf"), kind="command", task_id="task", run_id="run",
        argv=(sys.executable, "-c", "print('business-ran')"),
        cwd=workspace, env={"PATH": os.defpath},
        policy=ExecPolicy(
            argv_allowlist=(sys.executable,), cwd_roots=(workspace,),
            env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=65536,
        ),
        workspace=workspace, data_root=data,
    ) as execution:
        execution.admit()
        result = execution.run()
    assert not any(marker.exists() for marker in markers)
    assert result.exit_code == 0, result
    assert result.stdout.strip() == "business-ran"
