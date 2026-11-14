"""真实 Run 生产者共享严格资格与创建前磁盘门。"""

import copy
import json
from contextlib import closing
from pathlib import Path

import pytest
from support import engine_for, stack_for

from tianshu import event_schema
from tianshu.errors import TianshuError
from tianshu.events import EVENT_TYPES, EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.tasks import TaskStore


def test_run_start_v2_only(tmp_path):
    from tianshu.run_policy import EFFECTS

    assert event_schema.current_version("run.start") == 2
    assert all(event_schema.current_version(t) == 1 for t in EVENT_TYPES - {"run.start"})
    assert event_schema.known_versions("run.start") == frozenset({0, 1, 2})
    history = json.loads((Path(__file__).parent / "fixtures/events/minimal_v1.json").read_text())
    # 历史 schema 仍不认识新增字段；新写不能假装 v1。
    schema = event_schema.schema_for("run.start", 1)
    assert "execution_policy" not in schema["properties"]
    payload = event_schema.minimal_payload("run.start")
    assert payload["execution_policy"] == EFFECTS.to_dict()
    for mode in ("future", True):
        bad = copy.deepcopy(payload)
        bad["execution_policy"]["mode"] = mode
        with pytest.raises(TianshuError):
            event_schema.detect_version("run.start", bad)
    assert history
    with closing(EventLog(tmp_path / "events.db")) as log:
        seq = log.append("run_x", "run.start", payload)
        assert (
            log.conn.execute("SELECT version FROM event_versions WHERE seq=?", (seq,)).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("mode", ["effects", "pure_only"])
@pytest.mark.parametrize("format_version", [1, 1.0, True, 2])
def test_real_run_policy_write_matches_strict_reopened_read(tmp_path, mode, format_version):
    db = tmp_path / "events.db"
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        payload = event_schema.minimal_payload("run.start")
        payload["execution_policy"] = {"format_version": format_version, "mode": mode}
        if type(format_version) is not int or format_version != 1:
            before = {
                table: [tuple(row) for row in log.conn.execute(f"SELECT * FROM {table}")]
                for table in ("events", "event_versions")
            }
            with pytest.raises(event_schema.PayloadContractError) as caught:
                log.append("policy_probe", "run.start", payload)
            assert caught.value.path.startswith("$.execution_policy")
            assert {
                table: [tuple(row) for row in log.conn.execute(f"SELECT * FROM {table}")]
                for table in before
            } == before
            assert list((tmp_path / "blobs").iterdir()) == []
            return
        log.append("policy_probe", "run.start", payload)
        assert log.read_run("policy_probe")[0].payload_version == 2
        policy = TaskStore(log).run_execution_policy("policy_probe")
        assert policy.to_dict() == {"format_version": 1, "mode": mode}
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as reopened:
        policy = TaskStore(reopened).run_execution_policy("policy_probe")
        assert policy.to_dict() == {"format_version": 1, "mode": mode}
        assert type(policy.format_version) is int


@pytest.mark.parametrize("event_type", ["task.created", "run.start"])
def test_other_json_integer_fields_still_accept_integral_float(tmp_path, event_type):
    with closing(EventLog(tmp_path / "events.db")) as log:
        payload = event_schema.minimal_payload(event_type)
        payload["goal_revision"] = 2.0
        log.append("integer_probe", event_type, payload)
        event = log.read_run("integer_probe")[0]
        assert event.payload["goal_revision"] == 2.0
        assert type(event.payload["goal_revision"]) is float
        assert event.payload_version == (2 if event_type == "run.start" else 1)


def test_direct_taskstore_requires_complete_context(tmp_path):
    from tianshu.run_policy import PURE_ONLY

    with closing(EventLog(tmp_path / "events.db")) as log:
        store = TaskStore(log)
        task = store.create_task("read")
        before = log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with pytest.raises(TianshuError) as caught:
            store.start_run(task.task_id)
        assert caught.value.rule_id == "dispatch.disk_space_unknown"
        assert store.list_runs(task.task_id) == []
        assert store.get_task(task.task_id).active_run_id is None
        assert log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        run = store.start_run(task.task_id, execution_policy=PURE_ONLY)
        assert store.run_execution_policy(run.run_id) == PURE_ONLY
        assert log.read_run(run.run_id)[0].payload_version == 2


def _low_stack(tmp_path):
    from tianshu.disk_admission import DiskAdmission

    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log, data_root=tmp_path / "data")
    original = stack.tasks.disk_admission
    stack.tasks._disk_admission = DiskAdmission(original.context, probe=lambda p: 0)
    return log, stack


def test_pure_only_writable_binding_has_no_effect_materials(tmp_path):
    from tianshu.run_policy import PURE_ONLY
    from tianshu.tool_registry import current_registry

    log, stack = _low_stack(tmp_path)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task = stack.tasks.create_task("read and talk", [str(workspace)])
        (workspace / "note.txt").write_text("read evidence", encoding="utf-8")

        class RecordingModel(ScriptedModel):
            manifests = []
            leases_during_model = []

            def complete(self, messages, tools):
                self.manifests.append(copy.deepcopy(tools))
                active = stack.tasks.get_task(task.task_id).active_run_id
                self.leases_during_model.append(stack.leases.list_for_run(active))
                return super().complete(messages, tools)

        model = RecordingModel(
            [
                ModelResponse(
                    "tool_use", tool_calls=[ToolCall("read1", "read_text", {"path": "note.txt"})]
                ),
                ModelResponse("end_turn", content="read evidence"),
            ]
        )
        result = engine_for(stack, model).start(task.task_id, execution_policy=PURE_ONLY)
        assert result.state == "succeeded", result.error
        assert model.calls == 2
        assert any(
            e.type == "tool.result" and e.payload.get("tool_id") == "read_text"
            for e in log.read_run(result.run_id)
        )
        assert stack.leases.list_for_run(result.run_id) == []
        assert not stack.snapshots.has_manifest(result.run_id)
        policy = stack.tasks.run_execution_policy(result.run_id)
        assert policy == PURE_ONLY
        payload = stack.tasks.run_payload(result.run_id)
        assert all(current_registry().spec(t).side_effect_level == "pure" for t in payload["tools"])
        expected = [
            t
            for t in current_registry().manifest()
            if current_registry().spec(t["name"]).side_effect_level == "pure"
        ]
        assert model.manifests == [expected, expected]
        assert model.leases_during_model == [[], []]
        assert stack.tasks.get_task(task.task_id).workspaces[0].can_write
    finally:
        log.close()


@pytest.mark.parametrize("producer", ["direct", "loop", "control", "memory_control"])
def test_new_effects_rejected_before_materials(tmp_path, producer):
    from tianshu.control import ControlPlane

    log, stack = _low_stack(tmp_path)
    try:
        task = stack.tasks.create_task("effects")
        before_tasks = log.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        from tianshu.control import ControlError

        with pytest.raises((TianshuError, ControlError)):
            if producer == "direct":
                stack.tasks.start_run(task.task_id)
            elif producer == "loop":
                engine_for(stack, ScriptedModel([])).start(task.task_id)
            elif producer == "control":
                ControlPlane(stack, model=ScriptedModel([])).handle(
                    "run.start", {"task_id": task.task_id}
                )
            else:
                stack.dispatcher.open_control_run(goal="remember", workspaces=[])
        assert stack.tasks.list_runs(task.task_id) == []
        assert log.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert log.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert log.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_tasks
        assert list(stack.snapshots.manifest_dir.iterdir()) == []
    finally:
        log.close()


@pytest.mark.parametrize("producer", ["control", "memory", "undo"])
@pytest.mark.parametrize("space", ["high", "initial_low", "late_low", "late_unknown"])
def test_control_creation_final_rejection_preserves_all_rows_and_materials(
    tmp_path, producer, space
):
    from tianshu.disk_admission import MIN_FREE_BYTES, DiskAdmission
    from tianshu.memory_control import remember
    from tianshu.resources import MemoryScope
    from tianshu.undo import apply_undo

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        source_run = None
        if producer == "undo":
            task = stack.tasks.create_task("create undo source", [str(workspace)])
            source_run = engine_for(
                stack,
                ScriptedModel(
                    [
                        ModelResponse(
                            "tool_use",
                            tool_calls=[
                                ToolCall(
                                    "write1",
                                    "create_report",
                                    {"name": "out.md", "content": "output"},
                                )
                            ],
                        ),
                        ModelResponse("end_turn", content="done"),
                    ]
                ),
            ).start(task.task_id)
            assert source_run.state == "succeeded", source_run.error
            assert (workspace / "out.md").read_text(encoding="utf-8") == "output"

        def rows():
            return {
                table: [tuple(row) for row in log.conn.execute(f"SELECT * FROM {table}")]
                for table in (
                    "tasks", "runs", "events", "event_versions", "jobs", "leases", "approvals"
                )
            }

        def materials():
            return {
                str(path): path.read_bytes()
                for root in (stack.snapshots.root, workspace)
                for path in root.rglob("*")
                if path.is_file()
            }

        observed = []

        def probe(path):
            in_transaction = log.in_transaction
            observed.append(in_transaction)
            if space == "initial_low":
                return 0
            if in_transaction and space == "late_low":
                return 0
            if in_transaction and space == "late_unknown":
                raise OSError("space probe unavailable at final creation gate")
            return MIN_FREE_BYTES

        stack.tasks._disk_admission = DiskAdmission(stack.tasks.disk_admission.context, probe=probe)
        before_rows, before_materials = rows(), materials()

        def produce():
            if producer == "control":
                control = stack.dispatcher.open_control_run(
                    goal="control final gate", workspaces=[str(workspace)]
                )
                stack.dispatcher.close_control_run(control)
            elif producer == "memory":
                result = remember(
                    stack, MemoryScope("user", "local"), kind="fact", content="remember evidence"
                )
                assert result.ok, result.output
            else:
                result = apply_undo(stack, source_run.run_id)
                assert result.ok, result.error

        if space == "high":
            produce()
            after = rows()
            assert len(after["tasks"]) - len(before_rows["tasks"]) == 1
            assert len(after["runs"]) - len(before_rows["runs"]) == 1
            assert len(after["jobs"]) - len(before_rows["jobs"]) == 1
            assert False in observed and True in observed
            if producer == "undo":
                assert not (workspace / "out.md").exists()
        else:
            rule = (
                "dispatch.disk_space_unknown" if space == "late_unknown"
                else "dispatch.disk_space_low"
            )
            with pytest.raises(TianshuError) as caught:
                produce()
            assert caught.value.rule_id == rule
            assert observed[0] is False
            assert (True in observed) == (space != "initial_low")
            assert rows() == before_rows
            assert materials() == before_materials


def test_control_creation_composes_with_outer_transaction_rollback(tmp_path):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        tables = ("tasks", "runs", "events", "event_versions", "jobs", "leases", "approvals")
        before = {
            table: [tuple(row) for row in log.conn.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        with pytest.raises(RuntimeError, match="outer rollback"):
            with log.transaction():
                control = stack.dispatcher.open_control_run(goal="outer control", workspaces=[])
                task = stack.tasks.get_task(control.task.task_id)
                assert task.active_run_id == control.run.run_id
                raise RuntimeError("outer rollback")
        assert {
            table: [tuple(row) for row in log.conn.execute(f"SELECT * FROM {table}")]
            for table in tables
        } == before


def test_subrun_policy_and_outer_transaction_rollback(tmp_path):
    from tianshu.run_policy import PURE_ONLY

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task("parent", max_subtasks=1)
        parent = stack.tasks.start_run(task.task_id)
        with pytest.raises(RuntimeError):
            with log.transaction():
                child = stack.tasks.create_subtask(
                    task, parent, subtask_id="rollback", goal="child"
                )
                assert stack.tasks.run_execution_policy(child.child_run_id) == PURE_ONLY
                raise RuntimeError("rollback")
        assert log.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert log.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert log.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        child = stack.tasks.create_subtask(task, parent, subtask_id="keep", goal="child")
        assert stack.tasks.run_execution_policy(child.child_run_id) == PURE_ONLY


@pytest.mark.parametrize("producer", ["direct", "loop", "control", "memory", "undo"])
def test_all_run_producers_write_v2_policy(tmp_path, producer):
    from tianshu.control import ControlPlane
    from tianshu.memory_control import remember
    from tianshu.resources import MemoryScope
    from tianshu.run_policy import EFFECTS
    from tianshu.undo import apply_undo

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task = stack.tasks.create_task("produce", [str(workspace)])
        if producer == "direct":
            stack.tasks.start_run(task.task_id, payload={"execution_policy": {"mode": "pure_only"}})
        elif producer == "control":
            ControlPlane(stack, model=ScriptedModel([])).handle(
                "run.start", {"task_id": task.task_id}
            )
        elif producer == "memory":
            remember(stack, MemoryScope("user", "local"), kind="fact", content="remember evidence")
        else:
            model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "write1", "create_report", {"name": "out.md", "content": "output"}
                            )
                        ],
                    ),
                    ModelResponse("end_turn", content="done"),
                ]
            )
            result = engine_for(stack, model).start(task.task_id)
            assert result.state == "succeeded", result.error
            if producer == "undo":
                assert apply_undo(stack, result.run_id).ok
                assert not (workspace / "out.md").exists()
        starts = list(log.conn.execute("SELECT seq,run_id FROM events WHERE type='run.start'"))
        assert len(starts) == (2 if producer == "undo" else 1)
        for row in starts:
            assert stack.tasks.run_execution_policy(row["run_id"]) == EFFECTS
            assert (
                log.conn.execute(
                    "SELECT version FROM event_versions WHERE seq=?", (row["seq"],)
                ).fetchone()[0]
                == 2
            )
        stack.tasks.rebuild_from_events()
        for row in starts:
            assert stack.tasks.run_execution_policy(row["run_id"]) == EFFECTS


def test_legacy_policy_reads_without_inventing_fact(tmp_path):
    from tianshu.run_policy import PURE_ONLY

    with closing(EventLog(tmp_path / "events.db")) as log:
        store = TaskStore(log)
        task = store.create_task("old")
        current = store.start_run(task.task_id, execution_policy=PURE_ONLY)
        payload = store.run_payload(current.run_id)
        payload.pop("execution_policy")
        for version in (0, 1):
            run_id = f"history{version}"
            seq = log.append(run_id, "run.start", payload, legacy_write=True)
            log.conn.execute("UPDATE event_versions SET version=? WHERE seq=?", (version, seq))
            before = log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            assert store.run_execution_policy(run_id) is None
            assert log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        seq = log.append("future", "run.start", event_schema.minimal_payload("run.start"))
        log.conn.execute("UPDATE event_versions SET version=99 WHERE seq=?", (seq,))
        with pytest.raises(TianshuError):
            store.run_execution_policy("future")


def test_injected_store_locations_and_instance_mismatch(tmp_path):
    from tianshu.run_policy import EFFECTS
    from tianshu.snapshot import SnapshotStore

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "guard")
        task = stack.tasks.create_task("actual stores")
        # 换成另一真实 SnapshotStore，不能借原 context 的路径为其作证。
        stack.dispatcher._snapshots = SnapshotStore(tmp_path / "other-volume")
        with pytest.raises(TianshuError) as caught:
            stack.dispatcher.prepare_run(task.task_id, EFFECTS)
        assert caught.value.rule_id == "dispatch.disk_space_unknown"
        assert stack.tasks.list_runs(task.task_id) == []


def test_late_bound_memory_or_content_cannot_borrow_other_context(tmp_path):
    from tianshu.contents import ContentStore
    from tianshu.memory import MemoryProjection

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "guard")
        task = stack.tasks.create_task("actual memory")
        engine = engine_for(stack, ScriptedModel([]))
        engine._memory = MemoryProjection(log.conn, log=log, blob_dir=tmp_path / "other-memory")
        with pytest.raises(TianshuError) as caught:
            engine.start(task.task_id)
        assert caught.value.rule_id == "dispatch.disk_space_unknown"
        stack.tasks._contents = ContentStore(log, blob_dir=tmp_path / "other-content")
        with pytest.raises(TianshuError) as caught:
            stack.tasks.start_run(task.task_id)
        assert caught.value.rule_id == "dispatch.disk_space_unknown"
        assert stack.tasks.list_runs(task.task_id) == []


def test_readonly_binding_is_not_a_write_target(tmp_path):
    from tianshu.disk_admission import DiskAdmission
    from tianshu.workspace import WorkspaceBinding

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        workspace = tmp_path / "readonly-volume"
        workspace.mkdir()
        task = stack.tasks.create_task(
            "read", [WorkspaceBinding(str(workspace), "readonly", "none")]
        )
        seen = []

        def probe(path):
            seen.append(path)
            return 0 if path == workspace or workspace in path.parents else 2147483648

        stack.tasks._disk_admission = DiskAdmission(stack.tasks.disk_admission.context, probe=probe)
        result = engine_for(stack, ScriptedModel([])).start(task.task_id)
        assert result.state == "succeeded", result.error
        assert not any(path == workspace or workspace in path.parents for path in seen)


def test_mcp_preparation_happens_after_probe_and_before_final_gate(tmp_path):
    import sys

    from tianshu.disk_admission import DiskAdmission
    from tianshu.loop import build_stack
    from tianshu.mcp import McpClients, McpServerConfig
    from tianshu.run_policy import PURE_ONLY

    clients = McpClients(
        {
            "fake": McpServerConfig(
                name="fake",
                command=(
                    sys.executable,
                    str(Path(__file__).parent / "fixtures/fake_mcp_server.py"),
                ),
            )
        }
    )
    with closing(EventLog(tmp_path / "events.db")) as log:
        try:
            stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
            assert clients.server("fake") is None
            gate = stack.tasks.disk_admission
            stack.tasks._disk_admission = DiskAdmission(gate.context, probe=lambda p: 0)
            task = stack.tasks.create_task("pure")
            assert (
                engine_for(stack, ScriptedModel([]))
                .start(task.task_id, execution_policy=PURE_ONLY)
                .state
                == "succeeded"
            )
            assert clients.server("fake") is None
            task = stack.tasks.create_task("effects low")
            with pytest.raises(TianshuError):
                engine_for(stack, ScriptedModel([])).start(task.task_id)
            assert clients.server("fake") is None
            # 准备后空间改变：真正 start_run 再测，不能把准备阶段当许可票据。
            stack.tasks._disk_admission = gate
            original = clients.start_all

            def prepare_then_drop():
                original()
                stack.tasks._disk_admission = DiskAdmission(gate.context, probe=lambda p: 0)

            clients.start_all = prepare_then_drop
            with pytest.raises(TianshuError):
                engine_for(stack, ScriptedModel([])).start(task.task_id)
            assert clients.ready_names() == ("fake",)
            assert stack.tasks.list_runs(task.task_id) == []
        finally:
            clients.close_all()


def test_two_real_processes_reopen_and_compete(tmp_path):
    import os
    import subprocess
    import sys

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        task = stack.tasks.create_task("compete")
    code = """
import json,sys
from pathlib import Path
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.errors import TianshuError
root=Path(sys.argv[1])
log=EventLog(root/"events.db")
stack=build_stack(log,data_root=root/"data")
print("READY",flush=True)
assert sys.stdin.readline()=="GO\\n"
try:
    run=stack.tasks.start_run(sys.argv[2])
    print(json.dumps({"run_id":run.run_id,"policy":stack.tasks.run_execution_policy(run.run_id).to_dict()}),flush=True)
except TianshuError as exc:
    print(json.dumps({"error":str(exc)}),flush=True)
log.close()
"""
    env = {
        k: v
        for k, v in os.environ.items()
        if not (
            k.startswith("TIANSHU_") or any(x in k for x in ("API_KEY", "ENDPOINT", "BASE_URL"))
        )
    }
    env["TIANSHU_AUTHORITY_ROOT"] = str(tmp_path / "authority")
    children = [
        subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(tmp_path), task.task_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            assert child.stdout.readline() == b"READY\n"
        for child in children:
            child.stdin.write(b"GO\n")
            child.stdin.flush()
        rows = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr.decode("utf-8", "replace")
            rows.append(json.loads(stdout))
        assert sum("run_id" in row for row in rows) == 1
        assert sum("error" in row and "活跃 run" in row["error"] for row in rows) == 1
        with closing(EventLog(tmp_path / "events.db")) as log:
            stack = stack_for(log, data_root=tmp_path / "data")
            runs = stack.tasks.list_runs(task.task_id)
            assert len(runs) == 1
            assert stack.tasks.run_execution_policy(runs[0].run_id).mode == "effects"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("producer", ["loop", "control", "memory", "undo"])
@pytest.mark.parametrize("low", [False, True])
def test_real_mcp_producer_preparation_and_low_disk_order(tmp_path, producer, low):
    import sys

    from tianshu.control import ControlError, ControlPlane
    from tianshu.disk_admission import DiskAdmission
    from tianshu.loop import build_stack
    from tianshu.mcp import McpClients, McpServerConfig
    from tianshu.memory_control import remember
    from tianshu.resources import MemoryScope
    from tianshu.undo import apply_undo

    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task = stack.tasks.create_task("produce", [str(workspace)])
        target_run = None
        if producer == "undo":
            target_run = engine_for(
                stack,
                ScriptedModel(
                    [
                        ModelResponse(
                            "tool_use",
                            tool_calls=[
                                ToolCall(
                                    "write1",
                                    "create_report",
                                    {"name": "out.md", "content": "output"},
                                )
                            ],
                        ),
                        ModelResponse("end_turn", content="done"),
                    ]
                ),
            ).start(task.task_id)
            assert target_run.state == "succeeded", target_run.error
        clients = McpClients(
            {
                "fake": McpServerConfig(
                    name="fake",
                    command=(
                        sys.executable,
                        str(Path(__file__).parent / "fixtures/fake_mcp_server.py"),
                    ),
                )
            }
        )
        try:
            stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
            assert clients.server("fake") is None
            if low:
                stack.tasks._disk_admission = DiskAdmission(
                    stack.tasks.disk_admission.context,
                    probe=lambda p: 0,
                )
            before = {
                table: log.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("tasks", "runs", "jobs", "events")
            }
            model = ScriptedModel([])

            def produce():
                if producer == "loop":
                    return engine_for(stack, model).start(task.task_id)
                if producer == "control":
                    return ControlPlane(stack, model=model).handle(
                        "run.start",
                        {"task_id": task.task_id},
                    )
                if producer == "memory":
                    return remember(
                        stack,
                        MemoryScope("user", "local"),
                        kind="fact",
                        content="remember evidence",
                    )
                result = apply_undo(stack, target_run.run_id)
                if not result.ok:
                    raise TianshuError(result.error)
                return result

            if low:
                with pytest.raises((TianshuError, ControlError)):
                    produce()
                assert clients.server("fake") is None
                assert model.calls == 0
                assert {
                    table: log.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before
                } == before
            else:
                produce()
                assert clients.ready_names() == ("fake",)
        finally:
            clients.close_all()
