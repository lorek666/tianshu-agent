from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from support import CHILD_TEXT

from tianshu import ipc
from tianshu.control import ControlError, ControlPlane
from tianshu.disk_admission import DiskAdmission
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.model import ScriptedModel
from tianshu.run_policy import PURE_ONLY
from tianshu.tool_registry import current_registry

SRC = Path(__file__).resolve().parents[1] / "src"


CHILD = """
import sys

sys.path.insert(0, __SRC__)

import tianshu.cli as cli
import tianshu.loop as loop
from tianshu.disk_admission import DiskAdmission as CandidateDiskAdmission
from tianshu.model import ModelResponse, ScriptedModel


loop.DiskAdmission = lambda context: CandidateDiskAdmission(
    context,
    probe=lambda _path: 0,
)
cli._scripted_model = lambda _out_name: ScriptedModel(
    [ModelResponse("end_turn", content="完成")]
)
raise SystemExit(cli.main(sys.argv[1:]))
"""


def _cli_child(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "TIANSHU_MCP_CONFIG"
    }
    env["TIANSHU_AUTHORITY_ROOT"] = str(tmp_path / "authority")
    return subprocess.run(
        [sys.executable, "-c", CHILD.replace("__SRC__", repr(str(SRC))), *argv],
        cwd=tmp_path,
        env=env,
        timeout=30,
        capture_output=True,
        **CHILD_TEXT,
    )


def _cli_main(*argv: str) -> tuple[int, str]:
    import contextlib
    import io

    from tianshu.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue() + stderr.getvalue()


class _ControlRpc:
    """用真实 IPC 传输承载真实 ControlPlane 的最小 RPC 服务。

    EventLog 的 SQLite 连接只能在创建线程使用；真实 daemon 也在自己的服务线程
    建连接。这里把存储、Task、ControlPlane 与收发循环全部放进同一个 worker，
    避免测试自己制造 SQLite 的同线程错误。
    """

    def __init__(
        self, root: Path, db: Path, data_root: Path, workspace: Path
    ) -> None:
        self.root = root
        self.db = db
        self.data_root = data_root
        self.workspace = workspace
        self.stop = threading.Event()
        self.ready: queue.Queue[str | Exception] = queue.Queue()
        self.task_id = ""
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _send(self, connection: ipc.ServerConnection, request_id: str, method: str,
              params: dict) -> None:
        try:
            if method == "hello":
                result = {
                    "protocol": ipc.negotiate(params),
                    "daemon_version": "entrypoint-test",
                }
            else:
                result = self.plane.handle(method, params)
            connection.send(ipc.ok(request_id, result))
        except ControlError as exc:
            connection.send(ipc.fail(request_id, exc.code, exc.message, exc.details))
        except Exception as exc:  # noqa: BLE001 - 模拟 daemon 的帧边界
            connection.send(ipc.fail(request_id, "internal", str(exc)))

    def _run(self) -> None:
        connection: ipc.ServerConnection | None = None
        try:
            self.log = EventLog(self.db, blob_dir=self.db.parent / "blobs")
            stack = build_stack(self.log, data_root=self.data_root)
            stack.tasks._disk_admission = DiskAdmission(
                stack.tasks.disk_admission.context, probe=lambda _path: 0
            )
            self.task_id = stack.tasks.create_task(
                "只读检查", [str(self.workspace)]
            ).task_id
            self.plane = ControlPlane(stack, model=ScriptedModel([]))
            self.server = ipc.Server(self.root)
            self.server.start()
            self.ready.put(self.task_id)
            connection = self.server.accept()
            while not self.stop.is_set():
                message = connection.recv(timeout=0.1)
                if message is None:
                    if connection.eof:
                        break
                    continue
                request_id = str(message.get("id") or "")
                method = str(message.get("method") or "")
                params = message.get("params") or {}
                self._send(connection, request_id, method, params)
        except Exception as exc:  # noqa: BLE001 - 失败要交还主测试线程
            try:
                self.ready.put_nowait(exc)
            except queue.Full:
                pass
        finally:
            if connection is not None:
                connection.close()
            server = getattr(self, "server", None)
            if server is not None:
                server.close()
            plane = getattr(self, "plane", None)
            if plane is not None:
                plane.close()
            log = getattr(self, "log", None)
            if log is not None:
                log.close()

    def wait_ready(self) -> str:
        ready = self.ready.get(timeout=5)
        if isinstance(ready, Exception):
            raise ready
        return ready

    def client(self) -> ipc.Client:
        client = ipc.Client(self.root, timeout=5)
        client.connect()
        return client

    def close(self) -> None:
        self.stop.set()
        self.worker.join(timeout=5)
        if self.worker.is_alive():
            server = getattr(self, "server", None)
            if server is not None:
                server.close()
            self.worker.join(timeout=2)
        assert not self.worker.is_alive(), "RPC worker 未退出"


@contextmanager
def _control_rpc(
    root: Path, db: Path, data_root: Path, workspace: Path
):
    service = _ControlRpc(root, db, data_root, workspace)
    task_id = service.wait_ready()
    client = service.client()
    try:
        yield client, task_id
    finally:
        client.close()
        service.close()


def _short_root() -> Path:
    parent = None if os.name == "nt" else "/tmp"
    return Path(tempfile.mkdtemp(prefix="tsentry-", dir=parent))


def _create_task(db: Path, data_root: Path, workspace: Path) -> str:
    with closing(EventLog(db, blob_dir=db.parent / "blobs")) as log:
        stack = build_stack(log, data_root=data_root)
        return stack.tasks.create_task("只读检查", [str(workspace)]).task_id


def test_cli_run_start_explicit_pure_only_bypasses_low_probe(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    data_root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        stack = build_stack(log, data_root=data_root)
        task = stack.tasks.create_task("只读检查", [str(workspace)])

    proc = _cli_child(
        tmp_path,
        [
            "run",
            "start",
            "--task-id",
            task.task_id,
            "--execution-mode",
            "pure_only",
            "--model",
            "scripted",
            "--db",
            str(db),
            "--data-root",
            str(data_root),
        ],
    )
    assert proc.returncode == 0, proc.stderr

    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        stack = build_stack(log, data_root=data_root)
        runs = stack.tasks.list_runs(task.task_id)
        assert len(runs) == 1
        assert stack.tasks.run_execution_policy(runs[0].run_id) == PURE_ONLY
        stored = stack.tasks.get_task(task.task_id)
        assert stored is not None
        assert stored.workspaces[0].can_write is True


def test_cli_run_start_default_effects_is_rejected_before_run(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    data_root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = _create_task(db, data_root, workspace)

    proc = _cli_child(
        tmp_path,
        [
            "run",
            "start",
            "--task-id",
            task_id,
            "--model",
            "scripted",
            "--db",
            str(db),
            "--data-root",
            str(data_root),
        ],
    )

    assert proc.returncode == 2
    assert "dispatch.disk_space_low" in proc.stderr
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        stack = build_stack(log, data_root=data_root)
        assert stack.tasks.list_runs(task_id) == []
        assert stack.tasks.get_task(task_id).active_run_id is None


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [("effects", 2), ("pure_only", 0)],
)
def test_cli_compat_entrypoint_forwards_execution_mode(
    tmp_path: Path, mode: str, expected_code: int
) -> None:
    db = tmp_path / "events.db"
    data_root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    proc = _cli_child(
        tmp_path,
        [
            "run",
            "--task",
            "只读检查",
            "--workspace",
            str(workspace),
            "--execution-mode",
            mode,
            "--model",
            "scripted",
            "--db",
            str(db),
            "--data-root",
            str(data_root),
        ],
    )

    assert proc.returncode == expected_code, proc.stderr
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        stack = build_stack(log, data_root=data_root)
        tasks = stack.tasks.conn.execute(
            "SELECT task_id FROM tasks ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert tasks is not None
        task = stack.tasks.get_task(tasks["task_id"])
        assert task is not None and task.workspaces[0].can_write is True
        runs = stack.tasks.list_runs(task.task_id)
        if mode == "effects":
            assert runs == []
            assert "dispatch.disk_space_low" in proc.stderr
        else:
            assert len(runs) == 1
            assert stack.tasks.run_execution_policy(runs[0].run_id) == PURE_ONLY


def test_cli_unknown_execution_mode_is_bad_arguments_without_run(
    tmp_path: Path,
) -> None:
    db = tmp_path / "events.db"
    data_root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = _create_task(db, data_root, workspace)

    proc = _cli_child(
        tmp_path,
        [
            "run",
            "start",
            "--task-id",
            task_id,
            "--execution-mode",
            "unsafe",
            "--db",
            str(db),
            "--data-root",
            str(data_root),
        ],
    )

    assert proc.returncode == 2
    assert "invalid choice" in proc.stderr
    with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
        stack = build_stack(log, data_root=data_root)
        assert stack.tasks.list_runs(task_id) == []


@pytest.mark.parametrize("action", ["status", "pause", "resume", "cancel"])
def test_cli_existing_run_controls_reject_execution_mode(
    tmp_path: Path, action: str
) -> None:
    db = tmp_path / "events.db"
    code, output = _cli_main(
        "run",
        action,
        "--run-id",
        "run_missing",
        "--execution-mode",
        "pure_only",
        "--db",
        str(db),
    )
    assert code == 2
    assert "只适用于新建 Run" in output
    assert not db.exists()


def test_rpc_execution_policy_is_strict_and_controls_low_disk_admission(
    tmp_path: Path,
) -> None:
    db = tmp_path / "events.db"
    data_root = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rpc_root = _short_root()
    try:
        invalid = [
            None,
            [],
            {"format_version": True, "mode": "effects"},
            {"format_version": 1.0, "mode": "effects"},
            {"format_version": 2, "mode": "effects"},
            {"format_version": 1, "mode": "future"},
            {"format_version": 1, "mode": "effects", "approved": True},
        ]

        with _control_rpc(rpc_root, db, data_root, workspace) as (client, task_id):
            with pytest.raises(ipc.RemoteError) as caught:
                client.call("run.start", {"task_id": task_id})
            assert caught.value.code == "internal"
            assert "dispatch.disk_space_low" in caught.value.message

            for policy in invalid:
                with pytest.raises(ipc.RemoteError) as caught:
                    client.call(
                        "run.start",
                        {
                            "task_id": task_id,
                            "execution_policy": policy,
                        },
                    )
                assert caught.value.code == "bad_params", caught.value

            started = client.call(
                "run.start",
                {
                    "task_id": task_id,
                    "execution_policy": {
                        "format_version": 1,
                        "mode": "pure_only",
                    },
                },
            )["run"]

        with closing(EventLog(db, blob_dir=tmp_path / "blobs")) as log:
            stack = build_stack(log, data_root=data_root)
            runs = stack.tasks.list_runs(task_id)
            assert [run.run_id for run in runs] == [started["run_id"]]
            assert stack.tasks.run_execution_policy(started["run_id"]) == PURE_ONLY
            start_event = next(
                event
                for event in log.read_run(started["run_id"])
                if event.type == "run.start"
            )
            registry = current_registry()
            expected_pure_tools = [
                tool_id
                for tool_id in registry.ids()
                if registry.spec(tool_id).side_effect_level == "pure"
            ]
            assert start_event.payload["tools"] == expected_pure_tools
            stored = stack.tasks.get_task(task_id)
            assert stored is not None and stored.workspaces[0].can_write is True
    finally:
        shutil.rmtree(rpc_root, ignore_errors=True)
