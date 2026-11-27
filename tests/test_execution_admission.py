"""实际 subject 与准入证据必须绑定，真实进程启动不能借用旧报告。"""

from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu.exec import LOCAL_EXEC_BACKEND
from tianshu.execpolicy import ISOLATION_BOUNDARIES, ExecPolicy, PolicyUnsupported
from tianshu.mcp import McpClients, McpServerConfig


def _settings(tmp_path):
    ws, data = tmp_path / "ws", tmp_path / "data"
    ws.mkdir()
    data.mkdir()
    return {
        "argv": (sys.executable, "-c", "print('actual-execution')"),
        "cwd": ws, "env": {"PATH": "/usr/bin:/bin"},
        "workspace": ws, "data_root": data,
        "policy": ExecPolicy(
            argv_allowlist=(sys.executable,), cwd_roots=(ws,),
            env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=65536,
        ),
    }


def test_reaped_posix_leader_is_never_signalled_by_reusable_group_id(tmp_path, monkeypatch):
    from tianshu import execution_admission
    from tianshu.execution_admission import PreparedExecution

    settings = _settings(tmp_path)
    execution = PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="command", task_id="task", run_id="run", **settings,
    )

    class ReapedProcess:
        pid = 123
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def forbidden_group_signal(*args, **kwargs):
        pytest.fail("a reaped leader's numeric PGID must never be signalled")

    monkeypatch.setattr(execution_admission, "os", SimpleNamespace(
        name="posix", killpg=forbidden_group_signal,
    ))
    assert execution._stop_process(ReapedProcess()) == "unknown"


def test_unsignalled_posix_group_is_unknown_after_process_lookup_error(tmp_path, monkeypatch):
    from tianshu import execution_admission
    from tianshu.execution_admission import PreparedExecution

    execution = PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="command", task_id="task", run_id="run",
        **_settings(tmp_path),
    )

    class MissingGroupProcess:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -9
            return -9

    def missing_group(*args, **kwargs):
        raise ProcessLookupError

    monkeypatch.setattr(execution_admission, "os", SimpleNamespace(
        name="posix", killpg=missing_group,
    ))
    monkeypatch.setattr(execution_admission, "signal", SimpleNamespace(SIGKILL=9))
    assert execution._stop_process(MissingGroupProcess()) == "unknown"


def test_subject_changes_when_binary_environment_scope_or_config_changes(tmp_path):
    from tianshu.execution_admission import build_execution_subject

    settings = _settings(tmp_path)
    first = build_execution_subject(kind="command", task_id="task", run_id="run", **settings)
    assert first.format_version == 1
    assert first != build_execution_subject(
        kind="local_mcp", task_id="task", run_id="run", **settings,
    )
    assert first != build_execution_subject(
        kind="command", task_id="task", run_id="other", **settings,
    )
    changed = {**settings, "env": {"PATH": "/bin"}}
    assert first.environment_digest != build_execution_subject(
        kind="command", task_id="task", run_id="run", **changed,
    ).environment_digest
    program = settings["workspace"] / "program"
    program.write_bytes(b"first")
    binary = {**settings, "argv": (str(program),)}
    before = build_execution_subject(kind="command", task_id="task", run_id="run", **binary)
    program.write_bytes(b"second")
    after = build_execution_subject(kind="command", task_id="task", run_id="run", **binary)
    assert before.executable_digest != after.executable_digest
    assert "PATH" not in json.dumps(first.to_dict())


def test_local_backend_cannot_admit_automatic_command_or_run_payload(tmp_path):
    from tianshu.execution_admission import PreparedExecution

    settings = _settings(tmp_path)
    marker = settings["workspace"] / "must-not-exist"
    settings["argv"] = (sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')")
    with PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="command", task_id="task", run_id="run", **settings,
    ) as execution:
        with pytest.raises(PolicyUnsupported):
            execution.admit(ISOLATION_BOUNDARIES)
    assert not marker.exists()


def test_admission_rejects_changed_subject_and_different_backend_instance(tmp_path):
    from tianshu.exec import LocalExecBackend
    from tianshu.execution_admission import PreparedExecution, validate_admission

    with PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="command", task_id="task", run_id="run",
        **_settings(tmp_path),
    ) as execution:
        admission = execution.admit(())
        validate_admission(admission, execution.subject, LOCAL_EXEC_BACKEND)
        with pytest.raises(PolicyUnsupported):
            validate_admission(
                admission, replace(execution.subject, run_id="new"), LOCAL_EXEC_BACKEND,
            )
        with pytest.raises(PolicyUnsupported):
            validate_admission(admission, execution.subject, LocalExecBackend())


def test_subject_binds_relative_interpreter_script_after_flags(tmp_path):
    from tianshu.execution_admission import PreparedExecution

    settings = _settings(tmp_path)
    script = settings["workspace"] / "server.py"
    script.write_text("print('first')")
    settings["argv"] = (sys.executable, "-u", "server.py")
    with PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="local_mcp", task_id="task", run_id="run", **settings,
    ) as execution:
        execution.admit(())
        script.write_text("print('changed')")
        with pytest.raises(PolicyUnsupported):
            execution.open_stdio()
    assert execution._proc is None


def test_automatic_mcp_refuses_before_process_initialization(tmp_path):
    settings = _settings(tmp_path)
    marker = settings["workspace"] / "started"
    configs = {"bad": McpServerConfig(
        name="bad", command=(sys.executable, "-c", f"open({str(marker)!r},'w').write('bad')"),
    )}
    clients = McpClients(configs)
    scoped = clients.for_run(
        "task", "run", workspace=settings["workspace"], data_root=settings["data_root"],
        exec_backend=LOCAL_EXEC_BACKEND, policy=settings["policy"], automatic=True,
    )
    scoped.start_all()
    assert not marker.exists()
    assert scoped.ready_names() == ()
    assert "隔离" in scoped.diagnostics()[0]
    scoped.close_all()


def test_run_scope_rejects_equal_but_different_backend_instance(tmp_path):
    from tianshu.exec import LocalExecBackend
    from tianshu.mcp import McpError

    settings = _settings(tmp_path)
    clients = McpClients({})
    first, second = LocalExecBackend(), LocalExecBackend()
    assert first == second
    assert first is not second
    scope = {
        "workspace": settings["workspace"], "data_root": settings["data_root"],
        "policy": settings["policy"],
    }
    frozen = clients.for_run("task", "run", exec_backend=first, **scope)
    assert clients.for_run("task", "run", exec_backend=first, **scope) is frozen
    with pytest.raises(McpError, match="scope"):
        clients.for_run("task", "run", exec_backend=second, **scope)


def test_run_sessions_are_dedicated_and_timeout_is_never_retried(tmp_path):
    settings = _settings(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    log = settings["workspace"] / "calls.jsonl"
    clients = McpClients({"fake": McpServerConfig(
        name="fake", command=(sys.executable, str(fixture), "--mode", "hang_on_call",
                              "--call-log", str(log)), call_timeout_ms=80,
    )})
    first = clients.for_run(
        "task", "first", workspace=settings["workspace"], data_root=settings["data_root"],
        exec_backend=LOCAL_EXEC_BACKEND, policy=settings["policy"], automatic=False,
    )
    second = clients.for_run(
        "task", "second", workspace=settings["workspace"], data_root=settings["data_root"],
        exec_backend=LOCAL_EXEC_BACKEND, policy=settings["policy"], automatic=False,
    )
    try:
        first.start_all()
        second.start_all()
        one, two = first.server("fake"), second.server("fake")
        assert one.pid != two.pid
        assert one.session_snapshot()["run_id"] == "first"
        assert two.session_snapshot()["run_id"] == "second"
        from tianshu.mcp import McpClosed, McpTimeout

        with pytest.raises(McpTimeout):
            one.call_tool("echo", {"message": "once"})
        with pytest.raises(McpClosed):
            one.call_tool("echo", {"message": "must-not-send"})
        assert len(log.read_text().splitlines()) == 1
        assert one.session_snapshot()["pending_unknown"] is True
    finally:
        first.close_all()
        second.close_all()
    assert one.reconcile()["process_tree"] in ("exited", "unknown")


@pytest.mark.parametrize("action", ["pause", "cancel", "fence"])
def test_idle_worker_reaps_stopped_run_mcp_without_closing_live_session(tmp_path, action):
    from tianshu.events import EventLog
    from tianshu.loop import build_stack
    from tianshu.model import ScriptedModel
    from tianshu.worker import Worker

    settings = _settings(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    clients = McpClients({"fake": McpServerConfig(
        name="fake", command=(sys.executable, str(fixture)),
    )})
    with closing(EventLog(settings["data_root"] / "events.db",
                          blob_dir=settings["data_root"] / "blobs")) as log:
        stack = build_stack(log, data_root=settings["data_root"], mcp_clients=clients)
        stopped_task = stack.tasks.create_task("待停止的任务")
        live_task = stack.tasks.create_task("仍在等待的任务")
        stopped = stack.tasks.start_run(stopped_task.task_id)
        live = stack.tasks.start_run(live_task.task_id)
        sessions = [
            clients.for_run(
                task.task_id, run.run_id,
                workspace=settings["workspace"], data_root=settings["data_root"],
                exec_backend=LOCAL_EXEC_BACKEND, automatic=False,
            )
            for task, run in ((stopped_task, stopped), (live_task, live))
        ]
        worker = Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=0)
        try:
            for session in sessions:
                session.start_all()
                assert session.server("fake").is_alive
            if action == "pause":
                stack.tasks.request_pause(stopped.run_id)
            elif action == "cancel":
                stack.tasks.request_cancel(stopped.run_id)
            else:
                stack.tasks.revise_task(stopped_task.task_id, goal="已变更目标")
            assert worker.run_once() is False
            assert not sessions[0].server("fake").is_alive
            assert sessions[1].server("fake").is_alive
            assert not any(event.type == "model.intent" for event in log.read_run(live.run_id))
        finally:
            worker.close()
            clients.close_all()


@pytest.mark.parametrize("action, expected_state", [
    ("finish", "succeeded"), ("pause", "paused"), ("cancel", "cancelled"),
])
def test_loop_safe_point_reaps_its_actual_mcp_session(tmp_path, action, expected_state):
    from support import engine_for

    from tianshu.events import EventLog
    from tianshu.loop import build_stack
    from tianshu.model import ModelResponse, ScriptedModel

    settings = _settings(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    clients = McpClients({"fake": McpServerConfig(
        name="fake", command=(sys.executable, str(fixture)),
    )})
    with closing(EventLog(settings["data_root"] / "events.db",
                          blob_dir=settings["data_root"] / "blobs")) as log:
        stack = build_stack(log, data_root=settings["data_root"], mcp_clients=clients)
        task = stack.tasks.create_task("在安全点收回本轮进程")

        class ControlledModel(ScriptedModel):
            server = None

            def complete(self, messages, tools):
                run_id = stack.tasks.get_task(task.task_id).active_run_id
                scoped = clients.for_run(
                    task.task_id, run_id, workspace=settings["workspace"],
                    data_root=settings["data_root"], exec_backend=LOCAL_EXEC_BACKEND,
                    automatic=False,
                )
                scoped.start_all()
                self.server = scoped.server("fake")
                assert self.server.is_alive
                stack.dispatcher._mcp_source_clients = clients
                stack.dispatcher._mcp_clients = scoped
                stack.dispatcher._mcp_scope_run_id = run_id
                if action == "pause":
                    stack.tasks.request_pause(run_id)
                elif action == "cancel":
                    stack.tasks.request_cancel(run_id)
                return ModelResponse("end_turn", content="已处理")

        model = ControlledModel([])
        try:
            result = engine_for(stack, model).start(task.task_id, out_name="")
            assert result.state == expected_state
            assert model.server is not None and not model.server.is_alive
        finally:
            clients.close_all()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_stdio_close_kills_grandchild_even_after_leader_exits(tmp_path):
    from tianshu.execution_admission import PreparedExecution
    from tianshu.leases import pid_alive

    settings = _settings(tmp_path)
    script = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        "print(p.pid,flush=True);sys.stdin.readline()"
    )
    settings["argv"] = (sys.executable, "-c", script)
    with PreparedExecution(
        LOCAL_EXEC_BACKEND, kind="local_mcp", task_id="task", run_id="run", **settings,
    ) as execution:
        execution.admit(())
        proc = execution.open_stdio()
        grandchild = int(proc.stdout.readline())
        assert pid_alive(grandchild)
        execution.stop()
        # Never claim known exit merely because the direct child was reaped.
        assert execution.reconcile()["process_tree"] in ("exited", "unknown")
        assert proc.poll() is not None


def _native_backend():
    from tianshu.exec_backends import exec_backend, preferred_exec_backend

    name = preferred_exec_backend()
    if name == "local":
        pytest.skip("当前平台没有原生隔离后端；能力保持 unsupported")
    return exec_backend(name)


def test_native_subject_probe_uses_actual_policy_not_a_narrower_probe_policy(tmp_path):
    from tianshu.execution_admission import PreparedExecution

    settings = _settings(tmp_path)
    settings["policy"] = replace(
        settings["policy"], cwd_roots=(settings["workspace"], settings["data_root"]),
    )
    with PreparedExecution(
        _native_backend(), kind="command", task_id="task", run_id="run", **settings,
    ) as execution:
        with pytest.raises(PolicyUnsupported):
            execution.admit(ISOLATION_BOUNDARIES)


def test_native_subject_binds_effective_environment_and_private_profile(tmp_path):
    from tianshu.events import canonical_bytes, sha256_hex
    from tianshu.execution_admission import PreparedExecution

    settings = _settings(tmp_path)
    with PreparedExecution(
        _native_backend(), kind="local_mcp", task_id="task", run_id="run", **settings,
    ) as execution:
        effective_env = execution._child_env(settings["env"])
        assert execution.subject.environment_digest == sha256_hex(canonical_bytes(effective_env))
        assert execution.subject.isolation_profile_digest != sha256_hex(
            canonical_bytes(settings["policy"].to_dict()),
        )


def test_native_scoped_mcp_stdio_initializes_calls_and_records_lifecycle(tmp_path):
    import shutil

    settings = _settings(tmp_path)
    fixture = settings["workspace"] / "server.py"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "fake_mcp_server.py", fixture)
    clients = McpClients({"native": McpServerConfig(
        name="native", command=(sys.executable, str(fixture)),
    )})
    events = []
    scoped = clients.for_run(
        "task", "run", workspace=settings["workspace"], data_root=settings["data_root"],
        exec_backend=_native_backend(), policy=settings["policy"], automatic=False,
        required_boundaries=ISOLATION_BOUNDARIES[:-1],
        on_event=lambda event_type, payload: events.append((event_type, payload)),
    )
    try:
        scoped.start_all()
        assert scoped.ready_names() == ("native",), scoped.diagnostics()
        server = scoped.server("native")
        assert server.call_tool("echo", {"message": "isolated"}).text_parts == ("echo:isolated",)
        snapshot = server.session_snapshot()
        assert snapshot["subject"]["kind"] == "local_mcp"
        assert all(
            snapshot["admission"]["boundaries"][boundary] == "enforced"
            for boundary in ISOLATION_BOUNDARIES[:-1]
        )
        assert snapshot["lifecycle"]["birth_identity"] is not None
        stages = [payload["stage"] for name, payload in events if name == "execution.admitted"]
        assert stages[:2] == ["prepared", "started"]
    finally:
        scoped.close_all()
    assert any(name == "execution.ended" for name, _ in events)


def test_native_command_tool_records_real_subject_and_unknown_tree(tmp_path):
    from tianshu.events import EventLog
    from tianshu.tools import ToolContext, _run_command
    from tianshu.workspace import Workspace

    settings = _settings(tmp_path)
    policy = replace(settings["policy"], isolation_requires=ISOLATION_BOUNDARIES[:-1])
    with closing(EventLog(settings["data_root"] / "events.db")) as log:
        result = _run_command(
            Workspace(settings["workspace"]), {"argv": list(settings["argv"])},
            ToolContext(
                run_id="real-run", log=log, exec_policy=policy, exec_backend=_native_backend(),
                data_root=settings["data_root"],
            ),
        )
        assert result.status == "failed", result
        assert result.effect_state == "unknown"
        assert result.output["stdout"].strip() == "actual-execution"
        events = log.read_run("real-run")
        subjects = [
            event.payload["subject"] for event in events if event.type == "execution.admitted"
        ]
        assert subjects[0]["kind"] == "command"
        assert subjects[0]["run_id"] == "real-run"
        assert events[-1].type == "execution.ended"
        assert events[-1].payload["process_tree"] == "unknown"


def test_unknown_call_survives_stopped_process_and_blocks_same_resource(tmp_path):
    from tianshu.events import EventLog
    from tianshu.execution_admission import (
        PreparedExecution,
        assert_no_unresolved_executions,
    )

    settings = _settings(tmp_path)
    with closing(EventLog(settings["data_root"] / "events.db")) as log:
        with PreparedExecution(
            LOCAL_EXEC_BACKEND, kind="local_mcp", task_id="task", run_id="run",
            on_event=lambda name, payload: log.append("run", name, payload),
            **settings,
        ) as execution:
            execution.admit(())
            proc = execution.open_stdio()
            execution.record_stage("call_pending", request_id=17, call_id="sent")
            proc.wait()
            execution.stop()
        with pytest.raises(PolicyUnsupported, match="未知"):
            assert_no_unresolved_executions(log, workspace=settings["workspace"])


def test_recovery_does_not_confuse_process_group_exit_with_entire_tree_exit(tmp_path):
    from tianshu.events import EventLog
    from tianshu.execution_admission import (
        PreparedExecution,
        assert_no_unresolved_executions,
    )

    settings = _settings(tmp_path)
    with closing(EventLog(settings["data_root"] / "events.db")) as log:
        with PreparedExecution(
            LOCAL_EXEC_BACKEND, kind="local_mcp", task_id="task", run_id="run",
            on_event=lambda name, payload: log.append(
                "run", name, payload, task_id="task",
            ), **settings,
        ) as execution:
            execution.admit(())
            proc = execution.open_stdio()
            proc.wait(timeout=5)
            execution.stop(record=False)
            with pytest.raises(PolicyUnsupported, match="整树退出"):
                assert_no_unresolved_executions(log, workspace=settings["workspace"])
            assert not [row for row in log.read_run("run") if row.type == "execution.ended"]


def test_active_session_is_not_reused_as_another_runs_recovery_proof(tmp_path):
    from tianshu.events import EventLog
    from tianshu.execution_admission import (
        PreparedExecution,
        assert_no_unresolved_executions,
    )

    settings = _settings(tmp_path)
    settings["argv"] = (sys.executable, "-c", "import sys;sys.stdin.read()")
    with closing(EventLog(settings["data_root"] / "events.db")) as log:
        with PreparedExecution(
            LOCAL_EXEC_BACKEND, kind="local_mcp", task_id="task", run_id="first",
            on_event=lambda name, payload: log.append(
                "first", name, payload, task_id="task",
            ), **settings,
        ) as execution:
            execution.admit(())
            execution.open_stdio()
            with pytest.raises(PolicyUnsupported):
                assert_no_unresolved_executions(
                    log, workspace=settings["workspace"],
                )


def test_native_automatic_mcp_refuses_when_descendants_can_detach(tmp_path):
    settings = _settings(tmp_path)
    marker = settings["workspace"] / "must-not-start"
    clients = McpClients({"native": McpServerConfig(
        name="native",
        command=(sys.executable, "-c", f"open({str(marker)!r},'w').write('bad')"),
    )})
    scoped = clients.for_run(
        "task", "automatic", workspace=settings["workspace"], data_root=settings["data_root"],
        exec_backend=_native_backend(), policy=settings["policy"], automatic=True,
    )
    try:
        scoped.start_all()
        assert not marker.exists()
        assert not scoped.ready_names()
        assert "process_tree" in scoped.diagnostics()[0]
    finally:
        clients.close_all()


def test_native_dispatcher_binds_mcp_run_generation_and_receipt(tmp_path, monkeypatch):
    import shutil

    from support import approve_pending, engine_for

    from tianshu.events import EventLog
    from tianshu.execpolicy import ISOLATION_BOUNDARIES
    from tianshu.loop import build_stack
    from tianshu.model import ModelResponse, ScriptedModel, ToolCall
    from tianshu.receipt import verify_receipts

    settings = _settings(tmp_path)
    backend = _native_backend()
    monkeypatch.setenv("TIANSHU_EXEC_BACKEND", backend.capabilities.name)
    fixture = settings["workspace"] / "server.py"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "fake_mcp_server.py", fixture)
    calls = settings["workspace"] / "calls.jsonl"
    clients = McpClients({"native": McpServerConfig(
        name="native", command=(sys.executable, str(fixture), "--call-log", str(calls)),
    )})
    with closing(EventLog(settings["data_root"] / "events.db",
                          blob_dir=settings["data_root"] / "blobs")) as log:
        stack = build_stack(log, data_root=settings["data_root"], mcp_clients=clients)
        task = stack.tasks.create_task(
            "调用原生 MCP", [str(settings["workspace"])], mcp_servers=["native"],
            exec_policy=replace(settings["policy"], isolation_requires=ISOLATION_BOUNDARIES[:-1]),
        )
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("native-call", "mcp__native__echo", {"message": "native"}),
            ]),
            ModelResponse("end_turn", content="完成。"),
        ])
        engine = engine_for(stack, model)
        try:
            result = engine.start(task.task_id, out_name="out.md")
            for _ in range(4):
                if result.state != "waiting_approval":
                    break
                approve_pending(stack, result.run_id)
                result = engine.resume_run(result.run_id)
            events = log.read_run(result.run_id)
            tool_result = next(
                row for row in events
                if row.type == "tool.result" and row.payload["call_id"] == "native-call"
            )
            assert tool_result.payload["status"] == "succeeded", tool_result.payload
            mcp = tool_result.payload["output"]["mcp"]
            assert "spawn" not in mcp and "endpoint" not in mcp
            scoped_server = stack.dispatcher._mcp_clients.server("native")
            snapshot = scoped_server.session_snapshot()
            assert snapshot["task_id"] == task.task_id
            assert snapshot["run_id"] == result.run_id
            assert snapshot["subject"]["kind"] == "local_mcp"
            assert snapshot["generation"]
            lifecycle = snapshot["lifecycle"]["lifecycle_id"]
            stages = [row.payload for row in events if row.type == "execution.admitted"
                      and row.payload.get("call_id") == "native-call"]
            assert [row["stage"] for row in stages] == ["call_pending", "call_completed"]
            assert all(row["request_id"] == mcp["request_id"] for row in stages)
            assert all(row["lifecycle"]["lifecycle_id"] == lifecycle for row in stages)
            assert all(row["subject"] == snapshot["subject"] for row in stages)
            assert len(calls.read_text().splitlines()) == 1
            assert verify_receipts(log, result.run_id) == []
            assert result.state == "succeeded"
            assert not scoped_server.is_alive, "终态 Run 必须关闭专属 MCP 进程"
            assert any(row.type == "execution.ended" and row.payload["lifecycle_id"] == lifecycle
                       for row in events)
            # Switching back to a trusted manual task must restore its separate
            # source client, never lend a previous Run's admitted process.
            manual = stack.tasks.create_task(
                "人工调用 MCP", [str(settings["workspace"])], mcp_servers=["native"],
            )
            manual_engine = engine_for(stack, ScriptedModel([
                ModelResponse("tool_use", tool_calls=[
                    ToolCall("manual-call", "mcp__native__echo", {"message": "manual"}),
                ]),
                ModelResponse("end_turn", content="完成。"),
            ]))
            manual_result = manual_engine.start(manual.task_id, out_name="manual.md")
            for _ in range(4):
                if manual_result.state != "waiting_approval":
                    break
                approve_pending(stack, manual_result.run_id)
                manual_result = manual_engine.resume_run(manual_result.run_id)
            assert stack.dispatcher._mcp_clients is clients
            assert clients.server("native") is not scoped_server
            assert clients.server("native").session_snapshot()["run_id"] == ""
        finally:
            clients.close_all()
