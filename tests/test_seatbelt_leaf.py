"""Native evidence for the opt-in, descendant-free macOS execution backend."""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.exec_backends import exec_backend
from tianshu.exec_seatbelt import seatbelt_available
from tianshu.execpolicy import ISOLATION_BOUNDARIES, ExecPolicy, PolicyUnsupported
from tianshu.execution_admission import (
    PreparedExecution,
    assert_no_unresolved_executions,
    process_birth_identity,
    reconcile_lifecycle,
)
from tianshu.isolation import _probe_process_tree, _wait_pid_gone
from tianshu.mcp import McpClients, McpServerConfig

native = pytest.mark.skipif(not seatbelt_available(), reason="requires native Darwin Seatbelt")


def _settings(tmp_path):
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    return {
        "argv": (sys.executable, "-c", "print('leaf-command')"),
        "cwd": workspace, "env": {"PATH": os.defpath},
        "workspace": workspace, "data_root": data,
        "policy": ExecPolicy(
            argv_allowlist=(sys.executable,), cwd_roots=(workspace,),
            env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=65536,
            isolation_requires=ISOLATION_BOUNDARIES,
        ),
    }


def test_leaf_backend_unavailable_platform_rejects_without_fallback():
    if seatbelt_available():
        assert exec_backend("seatbelt_leaf").capabilities.name == "seatbelt_leaf"
    else:
        with pytest.raises(PolicyUnsupported):
            exec_backend("seatbelt_leaf")


@native
@pytest.mark.parametrize("method", ["fork", "double_fork", "posix_spawn", "popen", "detached"])
def test_leaf_kernel_rejects_real_child_creation(tmp_path, method):
    settings = _settings(tmp_path)
    marker = settings["workspace"] / "child-created"
    child = f"from pathlib import Path;Path({str(marker)!r}).write_bytes(b'escaped')"
    if method in ("fork", "double_fork"):
        action = f"""
pid = os.fork()
if pid == 0:
    if {method == 'double_fork'!r}:
        second = os.fork()
        if second != 0:
            os.waitpid(second, 0)
            os._exit(0)
    exec({child!r})
    os._exit(0)
os.waitpid(pid, 0)
"""
    elif method == "posix_spawn":
        action = (
            f"pid=os.posix_spawn(sys.executable,[sys.executable,'-c',{child!r}],os.environ);"
            "os.waitpid(pid,0)"
        )
    else:
        action = (
            f"subprocess.run([sys.executable,'-c',{child!r}],check=True,"
            f"start_new_session={method == 'detached'!r},timeout=3)"
        )
    script = (
        "import json,os,subprocess,sys\ntry:\n"
        + "".join("    " + line + "\n" for line in action.strip().splitlines())
        + "except OSError as exc:\n    print(json.dumps({'denied_errno':exc.errno}))\n"
        + "else:\n    print(json.dumps({'child_created':True}))\n"
    )
    result = exec_backend("seatbelt_leaf").spawn(
        (sys.executable, "-c", script), cwd=settings["cwd"], env=settings["env"],
        policy=settings["policy"],
    )
    assert result.exit_code == 0, result
    assert not result.timed_out
    assert json.loads(result.stdout).get("denied_errno") in (errno.EPERM, errno.EACCES)
    assert not marker.exists()


@native
def test_leaf_command_tool_passes_full_subject_admission(tmp_path):
    from tianshu.tools import ToolContext, _run_command
    from tianshu.workspace import Workspace

    settings = _settings(tmp_path)
    with closing(EventLog(settings["data_root"] / "events.db")) as log:
        result = _run_command(
            Workspace(settings["workspace"]), {"argv": list(settings["argv"])},
            ToolContext(
                run_id="leaf-run", log=log, exec_policy=settings["policy"],
                exec_backend=exec_backend("seatbelt_leaf"), data_root=settings["data_root"],
            ),
        )
        assert result.status == "succeeded", result
        assert result.effect_state == "applied"
        assert result.output["stdout"].strip() == "leaf-command"
        events = log.read_run("leaf-run")
        admitted = [e for e in events if e.type == "execution.admitted"]
        assert admitted
        assert all(
            e.payload["admission"]["boundaries"][boundary] == "enforced"
            for e in admitted for boundary in ISOLATION_BOUNDARIES
        )
        assert events[-1].type == "execution.ended"
        assert events[-1].payload["process_tree"] == "exited"
        assert_no_unresolved_executions(log, workspace=settings["workspace"])


@native
def test_leaf_automatic_mcp_initializes_calls_and_stops(tmp_path):
    settings = _settings(tmp_path)
    script = settings["workspace"] / "server.py"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "fake_mcp_server.py", script)
    calls = settings["workspace"] / "calls.jsonl"
    clients = McpClients({"leaf": McpServerConfig(
        name="leaf", command=(sys.executable, str(script), "--call-log", str(calls)),
    )})
    with closing(EventLog(settings["data_root"] / "events.db")) as log:
        scoped = clients.for_run(
            "task", "automatic", workspace=settings["workspace"],
            data_root=settings["data_root"], exec_backend=exec_backend("seatbelt_leaf"),
            policy=settings["policy"], automatic=True,
            on_event=lambda name, payload: log.append("automatic", name, payload),
        )
        try:
            scoped.start_all()
            assert scoped.ready_names() == ("leaf",), scoped.diagnostics()
            server = scoped.server("leaf")
            assert server.call_tool("echo", {"message": "native"}).text_parts == ("echo:native",)
            snapshot = server.session_snapshot()
            assert snapshot["subject"]["kind"] == "local_mcp"
            assert snapshot["run_id"] == "automatic"
            assert all(
                snapshot["admission"]["boundaries"][boundary] == "enforced"
                for boundary in ISOLATION_BOUNDARIES
            )
            assert len(calls.read_text(encoding="utf-8").splitlines()) == 1
        finally:
            clients.close_all()
        assert not server.is_alive
        assert server.reconcile()["process_tree"] == "exited"
        assert_no_unresolved_executions(log, workspace=settings["workspace"])


@native
@pytest.mark.parametrize("finish", ["timeout", "stop"])
def test_leaf_root_termination_is_confirmed(tmp_path, finish):
    settings = _settings(tmp_path)
    settings["argv"] = (
        sys.executable, "-c", "import sys;print('root-ready',flush=True);sys.stdin.read()"
        if finish == "stop" else
        "import threading;print('root-ready',flush=True);threading.Event().wait()",
    )
    settings["policy"] = replace(settings["policy"], timeout_ms=500)
    with PreparedExecution(
        exec_backend("seatbelt_leaf"), kind="command", task_id="task", run_id="run",
        **settings,
    ) as execution:
        execution.admit()
        if finish == "timeout":
            result = execution.run()
            assert result.timed_out
            assert "root-ready" in result.stdout
            assert not result.descendants_may_survive
        else:
            proc = execution.open_stdio()
            assert proc.stdout.readline() == b"root-ready\n"
            assert execution.stop()["process_tree"] == "exited"
            assert proc.poll() is not None
        assert execution.reconcile()["process_tree"] == "exited"


@native
def test_leaf_reaped_root_never_signals_a_reused_process_group(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with PreparedExecution(
        exec_backend("seatbelt_leaf"), kind="command", task_id="task", run_id="run",
        **settings,
    ) as execution:
        execution.admit()
        proc = execution.open_stdio()
        assert proc.wait(timeout=3) == 0

        def unrelated_group(pgid, sig):
            # This PGID could now belong to a different identity. Even signal 0
            # must not override the managed wait that already proved root exit.
            raise AssertionError(f"reaped root used as process group: {pgid}, {sig}")

        monkeypatch.setattr(os, "killpg", unrelated_group)
        assert execution.stop()["process_tree"] == "exited"
        assert execution.reconcile()["process_tree"] == "exited"


@native
@pytest.mark.parametrize(
    "payload",
    [
        "print('FORK_DENIED=1',flush=True)",  # No timeout or root termination.
        "import threading;threading.Event().wait()",  # Missing denial evidence.
        "import threading;print('FORK_DENIED=11',flush=True);threading.Event().wait()",
        "import threading;print('FORK_DENIED=garbled',flush=True);threading.Event().wait()",
        "raise PermissionError(13,'exec failed before the probe')",
    ],
)
def test_leaf_broken_probe_cannot_prove_process_boundary(tmp_path, monkeypatch, payload):
    from tianshu import isolation

    settings = _settings(tmp_path)
    monkeypatch.setattr(isolation, "_PROBE_PROCESS_TREE", payload)
    finding = _probe_process_tree(
        exec_backend("seatbelt_leaf"), settings["workspace"], settings["data_root"], 250,
    )
    assert finding.status == "unknown"


@native
@pytest.mark.parametrize(
    "missing",
    [{"signal": None}, {"descendants_may_survive": True}],
    ids=["root-termination-not-confirmed", "cleanup-unknown"],
)
def test_leaf_incomplete_cleanup_result_cannot_prove_boundary(tmp_path, missing):
    settings = _settings(tmp_path)

    class IncompleteResult:
        # Run the real kernel-denied fork and actual timeout cleanup, then drop
        # one piece of termination evidence. The gate must fail closed.
        def spawn(self, *args, **kwargs):
            result = exec_backend("seatbelt_leaf").spawn(*args, **kwargs)
            assert result.timed_out and "FORK_DENIED=" in result.stdout
            return replace(result, **missing)

    finding = _probe_process_tree(
        IncompleteResult(), settings["workspace"], settings["data_root"], 250,
    )
    assert finding.status == "unknown"


@native
def test_leaf_profile_change_invalidates_admission_before_payload(tmp_path):
    settings = _settings(tmp_path)
    marker = settings["workspace"] / "payload"
    settings["argv"] = (
        sys.executable, "-c", f"open({str(marker)!r},'w').write('must-not-run')",
    )
    with PreparedExecution(
        exec_backend("seatbelt_leaf"), kind="command", task_id="task", run_id="run",
        **settings,
    ) as execution:
        execution.admit()
        execution._prefix = ()
        with pytest.raises(PolicyUnsupported, match="现场重测"):
            execution.run()
    assert not marker.exists()


@native
def test_leaf_missing_creation_identity_does_not_prove_exit(tmp_path):
    settings = _settings(tmp_path)
    settings["argv"] = (sys.executable, "-c", "import sys;sys.stdin.read()")
    with PreparedExecution(
        exec_backend("seatbelt_leaf"), kind="local_mcp", task_id="task", run_id="run",
        **settings,
    ) as execution:
        execution.admit()
        execution.open_stdio()
        current = execution.lifecycle()
        assert reconcile_lifecycle(current, process_tree_enforced=True)["process_tree"] == "unknown"
        execution.stop()
        missing = {**current, "birth_identity": None}
        assert reconcile_lifecycle(missing, process_tree_enforced=True)["process_tree"] == "unknown"


@native
def test_leaf_host_crash_preserves_live_lifecycle_block(tmp_path):
    settings = _settings(tmp_path)
    script = """
import json, os, pathlib, sys
from tianshu.exec_backends import exec_backend
from tianshu.execpolicy import ExecPolicy
from tianshu.execution_admission import PreparedExecution
from tianshu.events import EventLog
from tianshu.crashpoint import failpoint
root = pathlib.Path(sys.argv[1])
ws, data = root / 'workspace', root / 'data'
log = EventLog(data/'events.db')
policy = ExecPolicy(argv_allowlist=(sys.executable,), cwd_roots=(ws,),
                    env_allowlist=('PATH',), timeout_ms=3000, max_output_bytes=4096)
execution = PreparedExecution(
    exec_backend('seatbelt_leaf'), kind='local_mcp', task_id='task', run_id='crashed',
    argv=(sys.executable,'-c',
          "import threading;print('ready',flush=True);threading.Event().wait()"),
    cwd=ws, env={'PATH':os.defpath}, policy=policy, workspace=ws, data_root=data,
    on_event=lambda name,payload:log.append('crashed',name,payload))
execution.__enter__()
execution.admit()
proc = execution.open_stdio()
assert proc.stdout.readline() == b'ready\\n'
(root/'lifecycle.json').write_text(json.dumps(execution.lifecycle()), encoding='utf-8')
failpoint('test.leaf.after_ready')
raise AssertionError('configured crashpoint did not fire')
"""
    host = subprocess.run(
        (sys.executable, "-c", script, str(tmp_path)), timeout=20,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env={
            **os.environ, "TMPDIR": str(tmp_path),
            "TIANSHU_FAILPOINT": "test.leaf.after_ready",
        },
    )
    assert host.returncode == 77, host.stderr
    lifecycle = json.loads((tmp_path / "lifecycle.json").read_text(encoding="utf-8"))
    pid, birth = lifecycle["pid"], lifecycle["birth_identity"]
    try:
        assert birth is not None and process_birth_identity(pid) == birth
        reconciled = reconcile_lifecycle(lifecycle, process_tree_enforced=True)
        assert reconciled["process_tree"] == "unknown"
        with closing(EventLog(settings["data_root"] / "events.db")) as log:
            with pytest.raises(PolicyUnsupported, match="整树退出"):
                assert_no_unresolved_executions(log, workspace=settings["workspace"])
    finally:
        if birth is not None and process_birth_identity(pid) == birth:
            os.kill(pid, signal.SIGKILL)
        assert _wait_pid_gone(pid, 3000) is False
