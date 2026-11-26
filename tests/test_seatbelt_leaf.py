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
@pytest.mark.parametrize("fork_required", [False, True], ids=["leaf-command", "fork-denied"])
def test_automatic_goal_leaf_command_closure(tmp_path, monkeypatch, fork_required):
    from support import approve_pending, engine_for, stack_for
    from test_goal_v2 import advance, candidate, create, drive, get

    from tianshu.control import ControlPlane
    from tianshu.exec_backends import EXEC_BACKEND_ENV
    from tianshu.goal_v2 import GoalCoordinator
    from tianshu.loop import Limits
    from tianshu.model import ModelResponse, ScriptedModel, ToolCall
    from tianshu.plans import PlanStore
    from tianshu.run_policy import EFFECTS
    from tianshu.worker import Worker

    monkeypatch.setenv(EXEC_BACKEND_ENV, "seatbelt_leaf")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = stack_for(log, data_root=tmp_path)
        policy = ExecPolicy(
            argv_allowlist=(sys.executable,), cwd_roots=(workspace,),
            env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=65536,
            isolation_requires=ISOLATION_BOUNDARIES,
        )
        task = stack.tasks.create_task("隔离命令闭环", [str(workspace)], exec_policy=policy)
        plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=3))
        env = plane, stack, task, workspace
        goal = create(
            env, mode="bounded_auto", completion_policy="verified",
            execution_policy=EFFECTS.to_dict(),
            isolation_requirements=list(ISOLATION_BOUNDARIES),
        )
        advance(env, goal)
        mission = GoalCoordinator(stack).mission(goal["goal_id"])
        drive(stack, json.dumps(candidate(mission)))
        planned = get(env, goal)
        plan = PlanStore(stack).get(planned["plan_id"])
        PlanStore(stack).confirm(
            plan["plan_id"], expected_revision=1,
            document_digest=plan["document_digest"], request_id="confirm",
        )
        code = (
            "import os;os.fork();from pathlib import Path;Path('escaped').write_text('bad')"
            if fork_required else
            "from pathlib import Path;Path('report.md').write_text('leaf')"
        )
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[ToolCall("command", "run_command", {
                "argv": [sys.executable, "-c", code], "cwd": str(workspace),
            })], usage={"input_tokens": 1, "output_tokens": 1}),
            ModelResponse("end_turn", content="命令已结束",
                          usage={"input_tokens": 1, "output_tokens": 1}),
        ])
        with Worker(stack, model=model, heartbeat_interval_ms=0) as worker:
            worker.drain(max_jobs=10)
            assert not worker.errors
        pending = stack.approvals.pending(run_id=get(env, planned)["phase_run_id"])
        assert len(pending) == 1
        run_id = pending[0].run_id
        assert approve_pending(stack, run_id) == [pending[0].approval_id]
        engine_for(stack, model).resume_run(run_id)
        GoalCoordinator(stack).tick(goal["goal_id"])
        drive(stack)
        result = get(env, goal)
        admitted = [
            event for run in stack.tasks.list_runs(task.task_id)
            for event in log.read_run(run.run_id, event_types=("execution.admitted",))
            if event.payload["subject"]["kind"] == "command"
        ]
        assert admitted, result["blocker"]
        assert all(
            event.payload["admission"]["backend"] == "seatbelt_leaf"
            and all(event.payload["admission"]["boundaries"][boundary] == "enforced"
                    for boundary in ISOLATION_BOUNDARIES)
            for event in admitted
        )
        command_run_ids = {event.run_id for event in admitted}
        assert all(
            log.read_run(run_id, event_types=("execution.ended",))[-1].payload["process_tree"]
            == "exited"
            for run_id in command_run_ids
        )
        assert not stack.approvals.pending()
        command_results = [
            event for event in log.read_run(run_id, event_types=("tool.result",))
            if event.payload["tool_id"] == "run_command"
        ]
        assert len(command_results) == 1
        assert command_results[0].payload["status"] == (
            "failed" if fork_required else "succeeded"
        )
        assert not (workspace / "escaped").exists()
        if fork_required:
            assert result["status"] == "blocked", result
            assert result["blocker"]["category"] == "verification_failed"
            assert not (workspace / "report.md").exists()
        else:
            assert result["status"] == "completed", result["blocker"]
            assert (workspace / "report.md").read_text(encoding="utf-8") == "leaf"
        assert_no_unresolved_executions(log, workspace=workspace)
    finally:
        log.close()


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
