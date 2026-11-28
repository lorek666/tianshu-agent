"""Windows Job Object 的结构/故障负控与 Windows 真子进程证据。

非 Windows 只跳过原生用例；结构、原型、原子参数与失败清理仍在本地真实执行。
fake API 仅证明本模块的 fail-closed 控制流，绝不作为 Windows 可用性证据。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu import exec_job_object as job
from tianshu.execpolicy import ExecPolicy, PolicyUnsupported

_REAL_WRAP_PIPE = job._wrap_pipe

HIGH_JOB = 0x800001234567
HIGH_PROCESS = 0x800002345678
HIGH_THREAD = 0x800003456789
HIGH_ATTRIBUTES = 0x80000456789A


class FakeNative:
    """同形 fault API；管道是真实本机 fd，Win32 HANDLE 保留高 bit。"""

    def __init__(self) -> None:
        self.failure = ""
        self.active = 0
        self.exit_code_value = 7
        self.wait_result = job.WAIT_OBJECT_0
        self.pipe_handles: set[int] = set()
        self.closed: list[int] = []
        self.created_jobs = 0
        self.job_flags: list[int] = []
        self.handle_flags: list[tuple[int, int, int]] = []
        self.attributes: list[int] = []
        self.deleted_attributes: list[int] = []
        self.freed_attributes: list[int] = []
        self.terminated: list[int] = []
        self.terminated_processes: list[tuple[int, int]] = []
        self.create_calls: list[dict[str, object]] = []
        self.inherited_handle_lists: list[tuple[int, ...]] = []
        self.create_inherits = True
        self.membership_checks: list[tuple[int, int]] = []
        self.resume_calls: list[int] = []
        self.query_calls: list[int] = []
        self.close_failure: set[int] = set()
        self.terminate_active: int | None = None

    def create_job(self) -> int:
        self.created_jobs += 1
        return 0 if self.failure == "job" else HIGH_JOB

    def set_job_limits(self, handle: int, flags: int) -> bool:
        self.job_flags.append(flags)
        return self.failure != "limits"

    def create_pipe(self) -> tuple[int, int]:
        if self.failure == "pipe":
            raise job.JobObjectError("CreatePipe injected failure")
        read, write = os.pipe()
        self.pipe_handles.update((read, write))
        return read, write

    def set_handle_information(self, handle: int, mask: int, flags: int) -> bool:
        self.handle_flags.append((handle, mask, flags))
        return self.failure != "inherit"

    def initialize_attributes(self, memory: int, count: int, size: int) -> tuple[bool, int]:
        if self.failure == "initialize":
            return False, 0
        if memory == 0:
            return True, 128
        self.attributes.append(memory)
        return True, size

    def heap_allocate(self, size: int) -> int:
        assert size == 128
        return 0 if self.failure == "allocate" else HIGH_ATTRIBUTES

    def heap_free(self, memory: int) -> bool:
        self.freed_attributes.append(memory)
        return self.failure != "heap_free"

    def update_attributes(self, memory: int, attribute: int, value: int, size: int) -> bool:
        if attribute == job.PROC_THREAD_ATTRIBUTE_JOB_LIST:
            assert size == ctypes.sizeof(job._HANDLE)
            assert ctypes.cast(value, ctypes.POINTER(job._HANDLE)).contents.value == HIGH_JOB
            return self.failure != "update"
        assert attribute == job.PROC_THREAD_ATTRIBUTE_HANDLE_LIST
        assert size == 3 * ctypes.sizeof(job._HANDLE)
        handles = ctypes.cast(
            value, ctypes.POINTER(job._HANDLE * 3)
        ).contents
        self.inherited_handle_lists.append(tuple(handles))
        return self.failure != "handle_list"

    def delete_attributes(self, memory: int) -> None:
        self.deleted_attributes.append(memory)

    def create_process(
        self, application, command_line, cwd, environment, startup, inherited_handles
    ) -> tuple[int, int, int, int]:
        self.create_calls.append(
            {
                "application": application,
                "command_line": command_line.value,
                "cwd": cwd,
                "environment": _unicode_block(environment),
                "startup": startup,
                "cb": startup.StartupInfo.cb,
                "attribute_list": startup.lpAttributeList,
                "stdin": startup.StartupInfo.hStdInput,
                "stdout": startup.StartupInfo.hStdOutput,
                "stderr": startup.StartupInfo.hStdError,
                "inherited_handles": inherited_handles,
            }
        )
        if self.failure == "create":
            raise job.JobObjectError("CreateProcessW injected failure")
        os.write(startup.StartupInfo.hStdOutput, b"managed-out")
        os.write(startup.StartupInfo.hStdError, b"managed-err")
        return HIGH_PROCESS, HIGH_THREAD, 4242, 4343

    def resume_thread(self, thread: int) -> int:
        self.resume_calls.append(thread)
        return 0xFFFFFFFF if self.failure == "resume" else 1

    def is_process_in_job(self, process: int, job_handle: int) -> bool:
        self.membership_checks.append((process, job_handle))
        if self.failure == "membership":
            raise job.JobObjectError("IsProcessInJob injected failure")
        return self.failure != "not_in_job"

    def query_job(self, handle: int) -> int:
        self.query_calls.append(handle)
        if self.failure == "query":
            raise job.JobObjectError("QueryInformationJobObject injected failure")
        return self.active

    def terminate_job(self, handle: int) -> bool:
        self.terminated.append(handle)
        if self.failure == "terminate":
            return False
        self.active = self.terminate_active if self.terminate_active is not None else self.active
        return True

    def terminate_process(self, process: int, exit_code: int) -> bool:
        self.terminated_processes.append((process, exit_code))
        self.exit_code_value = exit_code
        self.active = 0
        return True

    def process_identity(self, process: int) -> tuple[int, int]:
        assert process == HIGH_PROCESS
        return 4242, 0x100012345678

    def exit_code(self, process: int) -> int | None:
        assert process == HIGH_PROCESS
        if self.failure == "exit_code":
            raise job.JobObjectError("GetExitCodeProcess injected failure")
        return self.exit_code_value

    def wait(self, process: int, milliseconds: int) -> int:
        assert process == HIGH_PROCESS
        return self.wait_result

    def close_handle(self, handle: int) -> bool:
        self.closed.append(handle)
        if handle in self.pipe_handles:
            self.pipe_handles.discard(handle)
            try:
                os.close(handle)
            except OSError:
                pass
        return handle not in self.close_failure


def _unicode_block(buffer) -> str:
    width = ctypes.sizeof(ctypes.c_wchar)
    value = ctypes.wstring_at(buffer, ctypes.sizeof(buffer) // width)
    # create_unicode_buffer 自带终止符；展示时去掉它，保留块内显式的双 NUL。
    return value.removesuffix("\x00")


@pytest.fixture
def fake_windows(monkeypatch):
    api = FakeNative()
    monkeypatch.setattr(job, "_WINDOWS", True)
    monkeypatch.setattr(job, "_load_native_api", lambda: api)
    monkeypatch.setattr(job, "_attribute_probe", lambda current: True)

    def adopt_pipe(handle: int, mode: str, *, transfer=None):
        # open_osfhandle 接管原始句柄；dup 后关闭 raw，模拟这一所有权转移。
        fd = os.dup(handle)
        os.close(handle)
        api.pipe_handles.discard(handle)
        if transfer is not None:
            transfer()
        return os.fdopen(fd, mode, buffering=0)

    monkeypatch.setattr(job, "_wrap_pipe", adopt_pipe)
    return api


def _create(api: FakeNative, tmp_path: Path, **kwargs):
    argv = kwargs.pop("argv", [sys.executable, "-c", "pass"])
    return job.create_managed_process(argv, cwd=tmp_path, env={}, **kwargs)


@pytest.mark.parametrize("interrupt", ["cancel", "claim_lost", "fenced"])
def test_dispatch_stops_job_on_inflight_run_control(
    tmp_path, monkeypatch, fake_windows, interrupt,
):
    from test_isolation_gate import _exec_request, _make_env, _policy

    from tianshu import isolation
    from tianshu.execution_admission import PreparedExecution
    from tianshu.permissions import HumanIdentity

    backend = job.WindowsJobObjectExecBackend()
    measured = isolation.IsolationFinding("process_tree", "enforced", "measured")
    monkeypatch.setattr(isolation, "_probe_process_tree", lambda *_a: measured)
    monkeypatch.setattr(PreparedExecution, "_probe_detached_descendant", lambda self: measured)
    env = _make_env(tmp_path, exec_backend=backend)
    try:
        task, run, claim = env.new_run(exec_policy=_policy(env.ws))
        args = {"argv": [sys.executable, "-c", "pass"]}
        decision = env.dispatcher.prepare(
            _exec_request(env, run, claim, args=args, task=task), claim, args=args,
        )
        assert decision.is_waiting, decision
        env.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli(),
        )
        fake_windows.active = 2
        fake_windows.terminate_active = 0
        fake_windows.exit_code_value = job.STILL_ACTIVE
        interrupted = False
        now = [0.0]
        monkeypatch.setattr(job, "time", SimpleNamespace(monotonic=lambda: now[0]))

        def wait(handle, milliseconds):
            nonlocal interrupted
            assert handle == HIGH_PROCESS
            if milliseconds == 0:
                return job.WAIT_TIMEOUT
            if not interrupted:
                if interrupt == "cancel":
                    assert env.tasks.request_cancel(run.run_id).control_requested == "cancel"
                elif interrupt == "claim_lost":
                    env.claims.cancel(claim.job_id, reason="claim lost during execution")
                else:
                    # Public goal revisions wait for the execution guard. Exercise the
                    # real fence transition here without going through that outer lock.
                    with env.log.transaction() as tx:
                        env.tasks._fence_run(tx, run.run_id, int(time.time() * 1000),
                                             reason="test in-flight fence")
                interrupted = True
                now[0] += 0.1
                return job.WAIT_TIMEOUT
            # If cancellation isn't checked, simulate the leader finishing on its own.
            fake_windows.exit_code_value = 0
            return job.WAIT_OBJECT_0

        fake_windows.wait = wait
        result = env.dispatcher.dispatch(decision.request_id, claim)

        assert interrupted
        assert fake_windows.exit_code_value == job.STILL_ACTIVE
        assert fake_windows.terminated == [HIGH_JOB]
        assert result.status == "failed" and result.effect_state == "applied", result
        assert result.output["interrupted"] is True
        assert not result.output["timed_out"]
        history = env.log.read_run(run.run_id)
        if interrupt == "fenced":
            assert env.tasks.get_run(run.run_id).state == "fenced"
            assert next(e.seq for e in history if e.type == "run.fenced") < next(
                e.seq for e in history if e.type == "execution.ended"
            )
        assert [e.payload["process_tree"] for e in history if e.type == "execution.ended"] == [
            "exited",
        ]
    finally:
        env.log.close()


@pytest.mark.parametrize(
    "failure,authority_write_fails", [("query", False), ("resume", False), ("query", True)],
)
def test_unknown_job_completion_blocks_command_and_write_in_workspace(
    tmp_path, monkeypatch, fake_windows, failure, authority_write_fails,
):
    from dataclasses import replace

    from test_isolation_gate import _exec_request, _make_env, _policy

    from tianshu import authority, isolation
    from tianshu.dispatcher import Dispatcher, build_request
    from tianshu.exec_backends import exec_backend
    from tianshu.execution_admission import PreparedExecution, assert_no_unresolved_executions
    from tianshu.permissions import HumanIdentity, PolicyEngine

    backend = job.WindowsJobObjectExecBackend()
    measured = isolation.IsolationFinding("process_tree", "enforced", "measured")
    monkeypatch.setattr(isolation, "_probe_process_tree", lambda *_a: measured)
    monkeypatch.setattr(PreparedExecution, "_probe_detached_descendant", lambda self: measured)
    env = _make_env(tmp_path, exec_backend=backend)
    try:
        task, run, claim = env.new_run(exec_policy=_policy(env.ws))
        args = {"argv": [sys.executable, "-c", "pass"]}
        decision = env.dispatcher.prepare(
            _exec_request(env, run, claim, args=args, task=task), claim, args=args,
        )
        assert decision.is_waiting, decision
        env.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli(),
        )
        fake_windows.active = 1
        fake_windows.failure = failure
        if failure == "resume":
            # The process was suspended, but its thread handle failed to close.
            fake_windows.close_failure.add(HIGH_THREAD)
        if authority_write_fails:
            def fail_registration(*_args, **_kwargs):
                raise authority.AuthorityWriteFailed("injected authority write failure")

            monkeypatch.setattr(authority, "register_blocked", fail_registration)
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.status == "failed" and result.effect_state == "unknown"
        history = env.log.read_run(run.run_id)
        ended = [e.payload for e in history if e.type == "execution.ended"]
        assert len(ended) == 1 and ended[0]["process_tree"] == "unknown"
        if failure == "query":
            assert ended[0]["birth_identity"] == f"windows-filetime:{0x100012345678}"
        assert result.output["descendants_may_survive"] is True
        assert len([e for e in history if e.type == "recovery.blocked"]) == 1
        local = Dispatcher(
            env.log, env.tasks, env.leases, PolicyEngine(), env.approvals,
            grants=env.grants, claims=env.claims, snapshots=env.snapshots,
            data_root=env.data, exec_backend=exec_backend("local"),
        )
        for tool, target, inputs in (
            ("run_command", env.ws, args),
            ("create_report", env.ws / "report.md", {"name": "report.md", "content": "blocked"}),
        ):
            request = build_request(task=task, run=run, claim=claim, call_id=f"next-{tool}",
                                    tool_id=tool, target=target, args=inputs)
            refused = local.prepare(request, claim, args=inputs)
            assert refused.is_rejected and refused.rule_id == "dispatch.workspace_blocked"

        if authority_write_fails:
            assert "跨数据根阻断未生效" in result.output["error"]
        else:
            other_root = tmp_path / "other-data"
            other_root.mkdir()
            other = replace(_make_env(other_root), ws=env.ws)
            try:
                other_task, other_run, other_claim = other.new_run(exec_policy=None)
                name = "cross-db.md"
                inputs = {"name": name, "content": "blocked"}
                request = build_request(
                    task=other_task, run=other_run, claim=other_claim, call_id="cross-db",
                    tool_id="create_report", target=env.ws / name, args=inputs,
                )
                refused = other.dispatcher.prepare(request, other_claim, args=inputs)
                assert refused.is_rejected and refused.rule_id == "dispatch.workspace_blocked"
                assert not (env.ws / name).exists()
            finally:
                other.log.close()

        env.log.append(run.run_id, "execution.ended", {
            **ended[0], "subject_digest": "a" * 64, "process_tree": "exited",
            "pid": 9823, "birth_identity": "different",
        }, task_id=task.task_id)
        with pytest.raises(PolicyUnsupported, match="整树退出"):
            assert_no_unresolved_executions(env.log, workspace=env.ws)
    finally:
        env.log.close()


def test_non_windows_is_unavailable_without_loading_native_api(monkeypatch, tmp_path):
    monkeypatch.setattr(job, "_WINDOWS", False)
    monkeypatch.setattr(
        job,
        "_load_native_api",
        lambda: pytest.fail("非 Windows 不得加载 kernel32"),
    )
    assert job.availability() is False
    with pytest.raises(PolicyUnsupported, match="不会回落"):
        job.create_managed_process([sys.executable], cwd=tmp_path, env={})
    with pytest.raises(PolicyUnsupported):
        job.JobController(object(), 1, 2, 3)


@pytest.mark.parametrize(
    ("pointer_bits", "startup_size", "extended_size"),
    [(64, 104, 112), (32, 68, 72)],
)
def test_native_structure_layout(pointer_bits, startup_size, extended_size):
    assert ctypes.sizeof(ctypes.c_void_p) * 8 in (32, 64)
    if ctypes.sizeof(ctypes.c_void_p) * 8 != pointer_bits:
        pytest.skip("当前宿主的指针宽度不是该 Windows ABI")
    assert ctypes.sizeof(job._StartupInfoW) == startup_size
    assert ctypes.sizeof(job._StartupInfoExW) == extended_size
    assert job._StartupInfoExW.StartupInfo.offset == 0
    assert ctypes.sizeof(job._JobBasicAccountingInformation) == 48
    assert job._JobBasicAccountingInformation.ActiveProcesses.offset == 40
    assert job._JobBasicLimitInformation.LimitFlags.offset == 16
    if pointer_bits == 64:
        assert ctypes.sizeof(job._JobBasicLimitInformation) == 64
    else:
        assert ctypes.sizeof(job._JobBasicLimitInformation) == 44


def test_high_bit_handles_and_process_information_are_pointer_sized():
    handle = job._HANDLE(HIGH_JOB)
    assert handle.value == HIGH_JOB
    assert ctypes.sizeof(handle) == ctypes.sizeof(ctypes.c_void_p)
    info = job._ProcessInformation()
    info.hProcess = job._HANDLE(HIGH_PROCESS)
    info.hThread = job._HANDLE(HIGH_THREAD)
    info.dwProcessId = 4242
    assert info.hProcess == HIGH_PROCESS
    assert info.hThread == HIGH_THREAD
    assert info.dwProcessId == 4242


def test_every_native_api_has_explicit_argtypes_and_restype():
    kernel32 = SimpleNamespace(
        **{
            name: SimpleNamespace()
            for name in (
                "CreateJobObjectW",
                "SetInformationJobObject",
                "QueryInformationJobObject",
                "TerminateJobObject",
                "TerminateProcess",
                "CreatePipe",
                "SetHandleInformation",
                "GetProcessHeap",
                "HeapAlloc",
                "HeapFree",
                "InitializeProcThreadAttributeList",
                "UpdateProcThreadAttribute",
                "DeleteProcThreadAttributeList",
                "CreateProcessW",
                "ResumeThread",
                "IsProcessInJob",
                "GetExitCodeProcess",
                "GetProcessId",
                "GetProcessTimes",
                "WaitForSingleObject",
                "CloseHandle",
            )
        }
    )
    job._declare_prototypes(kernel32)
    for name, function in vars(kernel32).items():
        assert isinstance(function.argtypes, list)
        assert (function.restype is None) == (name == "DeleteProcThreadAttributeList")
    assert kernel32.CreateJobObjectW.restype is job._HANDLE
    assert kernel32.GetProcessHeap.restype is job._HANDLE
    assert kernel32.GetProcessId.restype is job._DWORD
    assert kernel32.UpdateProcThreadAttribute.restype is job._BOOL
    assert kernel32.GetProcessTimes.restype is job._BOOL
    assert kernel32.QueryInformationJobObject.argtypes[0] is job._HANDLE
    assert kernel32.QueryInformationJobObject.argtypes[2] is job._LPVOID
    assert kernel32.TerminateJobObject.restype is job._BOOL
    assert kernel32.TerminateProcess.argtypes == [job._HANDLE, job._UINT]
    assert kernel32.ResumeThread.restype is job._DWORD
    assert kernel32.CloseHandle.argtypes == [job._HANDLE]


def test_native_api_accepts_ctypes_handle_returns_and_the_real_job_limits_buffer(monkeypatch):
    if ctypes.sizeof(job._HANDLE) != 8:
        pytest.skip("该用例核对 Windows 64 位扩展 Job 结构")
    handle_fn = ctypes.CFUNCTYPE(job._HANDLE)
    allocation_sizes = []
    attribute_sizes = []

    def set_limits(handle, kind, contents, size):
        assert handle.value == HIGH_JOB
        assert kind == 9
        assert size == 144
        flags = ctypes.cast(contents, ctypes.POINTER(job._JobBasicLimitInformation))
        assert flags.contents.LimitFlags == job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        return 1

    def initialize_attribute_list(memory, count, flags, size_pointer):
        assert count == 2 and flags == 0
        size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
        attribute_sizes.append(size.contents.value)
        if not memory:
            size.contents.value = 128
            return 0  # ERROR_INSUFFICIENT_BUFFER: the documented sizing call
        assert memory == HIGH_ATTRIBUTES and size.contents.value == 128
        return 1

    def heap_alloc(heap, flags, size):
        assert heap == HIGH_JOB and flags == 0
        allocation_sizes.append(size)
        return HIGH_ATTRIBUTES

    api = object.__new__(job._NativeAPI)
    api.kernel32 = SimpleNamespace(
        CreateJobObjectW=ctypes.CFUNCTYPE(job._HANDLE, job._LPVOID, job._LPCWSTR)(
            lambda _attrs, _name: HIGH_JOB,
        ),
        GetProcessHeap=handle_fn(lambda: HIGH_JOB),
        HeapAlloc=ctypes.CFUNCTYPE(job._HANDLE, job._HANDLE, job._DWORD, ctypes.c_size_t)(
            heap_alloc,
        ),
        SetInformationJobObject=set_limits,
        InitializeProcThreadAttributeList=initialize_attribute_list,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122, raising=False)
    assert api.create_job() == HIGH_JOB
    assert api.set_job_limits(HIGH_JOB, job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    assert api.initialize_attributes(0, 2, 0) == (True, 128)
    memory = api.heap_allocate(128)
    assert memory == HIGH_ATTRIBUTES
    assert allocation_sizes == [128]
    assert api.initialize_attributes(memory, 2, 128) == (True, 128)
    assert attribute_sizes == [0, 128]


def test_native_process_info_preserves_ctypes_handle_fields():
    def create_process(_application, _args, _pa, _ta, _inherit, _flags,
                       _env, _cwd, _startup, information):
        info = ctypes.cast(information, ctypes.POINTER(job._ProcessInformation)).contents
        info.hProcess = HIGH_PROCESS
        info.hThread = HIGH_THREAD
        info.dwProcessId = 4242
        info.dwThreadId = 4343
        return 1

    api = object.__new__(job._NativeAPI)
    api.kernel32 = SimpleNamespace(CreateProcessW=create_process)
    assert api.create_process(
        sys.executable, ctypes.create_unicode_buffer(sys.executable),
        os.getcwd(), job._environment_block({}), job._StartupInfoExW(), (),
    ) == (HIGH_PROCESS, HIGH_THREAD, 4242, 4343)


@pytest.mark.parametrize(
    "failure", ["job", "limits", "allocate", "initialize", "update", "heap_free"],
)
def test_availability_faults_fail_closed_before_process_creation(monkeypatch, failure):
    api = FakeNative()
    api.failure = failure
    monkeypatch.setattr(job, "_WINDOWS", True)
    monkeypatch.setattr(job, "_load_native_api", lambda: api)
    assert job.availability() is False
    assert api.create_calls == []
    assert api.pipe_handles == set()
    if failure != "job":
        assert HIGH_JOB in api.closed
    if failure == "update":
        assert api.deleted_attributes == [HIGH_ATTRIBUTES]
        assert api.freed_attributes == [HIGH_ATTRIBUTES]


def test_availability_rejects_an_unconfirmed_probe_close(monkeypatch):
    api = FakeNative()
    api.close_failure.add(HIGH_JOB)
    monkeypatch.setattr(job, "_WINDOWS", True)
    monkeypatch.setattr(job, "_load_native_api", lambda: api)

    assert job.availability() is False
    assert api.create_calls == []


def test_create_uses_atomic_suspended_unicode_process_and_native_pipes(fake_windows, tmp_path):
    process = _create(fake_windows, tmp_path, argv=[sys.executable, "-c", "print('x y')"])
    try:
        call = fake_windows.create_calls[0]
        startup = call["startup"]
        assert call["application"] == os.path.normpath(sys.executable)
        assert call["command_line"] == subprocess.list2cmdline(
            [os.path.normpath(sys.executable), "-c", "print('x y')"]
        )
        assert call["cwd"] == str(tmp_path.resolve())
        assert call["environment"] == "\x00\x00"
        assert call["cb"] == ctypes.sizeof(job._StartupInfoExW)
        assert call["attribute_list"] == HIGH_ATTRIBUTES
        assert startup.StartupInfo.dwFlags == job.STARTF_USESTDHANDLES
        assert startup.StartupInfo.hStdInput != startup.StartupInfo.hStdOutput
        assert startup.StartupInfo.hStdOutput != startup.StartupInfo.hStdError
        assert job._CREATE_FLAGS == (
            job.CREATE_SUSPENDED
            | job.CREATE_UNICODE_ENVIRONMENT
            | job.CREATE_EXTENDED_STARTUPINFO_PRESENT
        )
        assert fake_windows.job_flags == [job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE]
        assert not fake_windows.job_flags[0] & job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        assert fake_windows.resume_calls == [HIGH_THREAD]
        assert fake_windows.membership_checks == [(HIGH_PROCESS, HIGH_JOB)]
        child = {call["stdin"], call["stdout"], call["stderr"]}
        assert set(call["inherited_handles"]) == child
        assert fake_windows.inherited_handle_lists == [tuple(call["inherited_handles"])]
        # _wrap_pipe 的 fd dup 与原始 HANDLE 数值不同，但继承标志绑定原始 HANDLE。
        inherited = {h for h, _mask, flags in fake_windows.handle_flags if flags}
        noninherited = {h for h, _mask, flags in fake_windows.handle_flags if not flags}
        assert inherited == child
        assert len(noninherited) == 3
        assert not inherited & noninherited
        assert process.stdin.fileno() >= 0
        assert process.stdout.fileno() >= 0
        assert process.stderr.fileno() >= 0
        os.set_blocking(process.stdout.fileno(), False)
        assert os.read(process.stdout.fileno(), 64) == b"managed-out"
        assert os.read(process.stdout.fileno(), 64) == b""
        assert os.read(process.stderr.fileno(), 64) == b"managed-err"
        assert process.pid == 4242
        assert process.poll() == 7
        assert process.returncode == 7
        assert process.controller.query() == job.TREE_EXITED
    finally:
        assert process.close() == job.TREE_EXITED
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    assert fake_windows.pipe_handles == set()
    assert {HIGH_JOB, HIGH_PROCESS, HIGH_THREAD} <= set(fake_windows.closed)


def test_handle_returning_native_methods_keep_integer_handles():
    kernel32 = SimpleNamespace(
        CreateJobObjectW=lambda *_args: HIGH_JOB,
        GetProcessHeap=lambda: 17,
        HeapAlloc=lambda *_args: HIGH_ATTRIBUTES,
    )
    api = object.__new__(job._NativeAPI)
    api.kernel32 = kernel32

    assert api.create_job() == HIGH_JOB
    assert api.heap_allocate(128) == HIGH_ATTRIBUTES
    assert type(api.create_job()) is int


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("job", job.JobObjectError),
        ("limits", job.JobObjectError),
        ("pipe", job.JobObjectError),
        ("inherit", job.JobObjectError),
        ("allocate", job.JobObjectError),
        ("initialize", job.JobObjectError),
        ("update", job.JobObjectError),
        ("handle_list", job.JobObjectError),
        ("create", job.JobObjectError),
        ("membership", job.JobObjectError),
        ("not_in_job", job.JobObjectError),
        ("resume", job.JobObjectError),
    ],
)
def test_creation_failures_cleanup_every_allocated_resource(
    fake_windows, tmp_path, failure, expected
):
    fake_windows.failure = failure
    with pytest.raises(expected):
        _create(fake_windows, tmp_path)
    assert fake_windows.pipe_handles == set()
    assert fake_windows.freed_attributes == (
        [HIGH_ATTRIBUTES]
        if failure in {"update", "handle_list", "create", "membership", "not_in_job", "resume"}
        else []
    )
    if failure in {"membership", "not_in_job", "resume"}:
        assert fake_windows.terminated == [HIGH_JOB]
        assert fake_windows.terminated_processes == [(HIGH_PROCESS, 1)]
        assert HIGH_PROCESS in fake_windows.closed
        assert HIGH_THREAD in fake_windows.closed
        assert fake_windows.resume_calls == ([HIGH_THREAD] if failure == "resume" else [])
    else:
        assert fake_windows.terminated == []
    if failure != "job":
        assert HIGH_JOB in fake_windows.closed


def test_creation_cleanup_terminates_exact_process_even_if_job_terminate_fails(
    fake_windows, tmp_path
):
    fake_windows.failure = "not_in_job"

    def fail_job(handle):
        raise job.JobObjectError("TerminateJobObject injected failure")

    fake_windows.terminate_job = fail_job
    with pytest.raises(job.JobObjectError, match="未在目标 Job"):
        _create(fake_windows, tmp_path)

    assert fake_windows.terminated_processes == [(HIGH_PROCESS, 1)]
    assert HIGH_PROCESS in fake_windows.closed
    assert HIGH_THREAD in fake_windows.closed


def test_child_handle_close_failure_is_fail_closed(fake_windows, tmp_path):
    original = fake_windows.close_handle
    failed: list[int] = []

    def close(handle):
        # 只让第一个子端关闭失败一次；清理重试必须成功。
        if handle < 0x800000000000 and not failed:
            failed.append(handle)
            return False
        return original(handle)

    fake_windows.close_handle = close
    with pytest.raises(job.JobObjectError, match="关闭子进程 stdio"):
        _create(fake_windows, tmp_path)
    assert failed
    assert fake_windows.pipe_handles == set()
    assert HIGH_JOB in fake_windows.closed


def test_creation_cleanup_failure_is_reported_as_unknown(fake_windows, tmp_path):
    fake_windows.failure = "resume"
    fake_windows.close_failure.add(HIGH_JOB)
    with pytest.raises(job.JobObjectError, match="清理未确认") as error:
        _create(fake_windows, tmp_path)
    assert isinstance(error.value.__cause__, job.JobObjectError)
    assert "ResumeThread" in str(error.value.__cause__)
    assert fake_windows.pipe_handles == set()


def test_fdopen_failure_does_not_double_close_transferred_handle(monkeypatch):
    api = FakeNative()
    raw_read, raw_write = api.create_pipe()
    handles = job._Handles(api)
    handles.add(raw_read)
    adopted = []

    def open_osfhandle(handle, flags):
        fd = os.dup(handle)
        os.close(handle)
        api.pipe_handles.discard(handle)
        adopted.append(fd)
        return fd

    def cannot_open(*_args, **_kwargs):
        raise OSError("fdopen")

    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=open_osfhandle)
    )
    monkeypatch.setattr(job.os, "O_BINARY", getattr(os, "O_BINARY", 0), raising=False)
    monkeypatch.setattr(job.os, "fdopen", cannot_open)
    try:
        with pytest.raises(OSError, match="fdopen"):
            _REAL_WRAP_PIPE(raw_read, "rb", transfer=lambda: handles.discard(raw_read))
        assert len(adopted) == 1
        with pytest.raises(OSError):
            os.fstat(adopted[0])
        assert raw_read not in api.closed
        assert raw_read not in handles.values
        assert api.pipe_handles == {raw_write}
    finally:
        handles.close_all()
        os.close(raw_write)


@pytest.mark.parametrize("argv", [["python"], [], "python", ["", "arg"], ["a\x00b"]])
def test_explicit_application_path_is_required_before_create(fake_windows, tmp_path, argv):
    with pytest.raises(ValueError):
        job.create_managed_process(argv, cwd=tmp_path, env={})
    assert fake_windows.create_calls == []
    assert fake_windows.pipe_handles == set()
    assert fake_windows.created_jobs == 0


def test_unicode_environment_is_sorted_and_double_terminated():
    block = _unicode_block(job._environment_block({"z": "last", "Alpha": "值", "beta": "two"}))
    assert block == "Alpha=值\x00beta=two\x00z=last\x00\x00"


def _controller(api: FakeNative, clock=None, sleep=None):
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    return job.JobController(api, HIGH_JOB, HIGH_PROCESS, HIGH_THREAD, **kwargs)


def test_controller_requires_zero_active_processes_for_exited(fake_windows):
    controller = _controller(fake_windows)
    assert controller.query() == job.TREE_EXITED
    fake_windows.active = 1
    assert controller.query() == job.TREE_RUNNING
    assert controller.stop(0) == job.TREE_UNKNOWN
    assert fake_windows.terminated == [HIGH_JOB]
    fake_windows.active = 0
    assert controller.query() == job.TREE_EXITED


@pytest.mark.parametrize("failure", ["query", "terminate"])
def test_query_and_terminate_failures_are_unknown(fake_windows, tmp_path, failure):
    fake_windows.failure = failure
    controller = _controller(fake_windows)
    if failure == "terminate":
        fake_windows.active = 1
        assert controller.query() == job.TREE_RUNNING
    else:
        assert controller.query() == job.TREE_UNKNOWN
    assert controller.stop(0) == job.TREE_UNKNOWN
    assert fake_windows.terminated == [HIGH_JOB]
    controller.close(0)


def test_leader_identity_failure_is_not_a_fake_returncode(fake_windows, tmp_path):
    fake_windows.failure = "exit_code"
    process = _create(fake_windows, tmp_path)
    try:
        with pytest.raises(job.JobObjectError, match="GetExitCodeProcess"):
            process.poll()
        assert process.returncode is None
    finally:
        process.close(0)


def test_controller_timeout_uses_injected_clock_not_sleep_guess(fake_windows):
    now = [0.0]
    controller = _controller(
        fake_windows, clock=lambda: now[0], sleep=lambda _s: now.__setitem__(0, now[0] + _s)
    )
    fake_windows.active = 1
    assert controller.stop(0.025) == job.TREE_UNKNOWN
    assert now[0] == pytest.approx(0.025)


def test_close_automatically_retries_transient_handle_failure(fake_windows):
    original = fake_windows.close_handle
    failed = False

    def close_once(handle):
        nonlocal failed
        if handle == HIGH_PROCESS and not failed:
            failed = True
            fake_windows.closed.append(handle)
            return False
        return original(handle)

    fake_windows.close_handle = close_once
    controller = _controller(fake_windows)
    assert controller.close(0) == job.TREE_UNKNOWN
    assert controller._closed
    assert controller._open_handles == set()
    assert fake_windows.closed.count(HIGH_PROCESS) == 2
    assert fake_windows.closed.count(HIGH_THREAD) == 1
    assert fake_windows.closed.count(HIGH_JOB) == 1
    assert controller.query() == job.TREE_CLOSED
    assert controller.close() == job.TREE_CLOSED


def test_failed_job_close_cannot_be_reconciled_as_exited(fake_windows, tmp_path):
    from tianshu.execution_admission import PreparedExecution

    proc = _create(fake_windows, tmp_path)
    fake_windows.close_failure.add(HIGH_JOB)
    assert proc.close(0) == job.TREE_UNKNOWN
    execution = object.__new__(PreparedExecution)
    execution._job_backend = True
    assert execution._process_tree_state(proc) == job.TREE_UNKNOWN
    fake_windows.close_failure.clear()
    proc.close(0)


def test_close_keeps_permanently_failed_handle_for_bounded_retry(fake_windows):
    original = fake_windows.close_handle
    fake_windows.close_failure.add(HIGH_PROCESS)
    controller = _controller(fake_windows)
    assert controller.close(0) == job.TREE_UNKNOWN
    assert not controller._closed
    assert controller._open_handles == {HIGH_PROCESS}
    assert fake_windows.closed.count(HIGH_PROCESS) == job._HANDLE_CLOSE_ATTEMPTS
    assert fake_windows.closed.count(HIGH_THREAD) == 1
    assert fake_windows.closed.count(HIGH_JOB) == 1

    fake_windows.close_failure.clear()
    fake_windows.close_handle = original
    assert controller.close(0) == job.TREE_EXITED
    assert controller._closed
    assert controller._open_handles == set()
    assert fake_windows.closed.count(HIGH_PROCESS) == job._HANDLE_CLOSE_ATTEMPTS + 1


def test_managed_close_automatically_retries_controller_handle_failure(
    fake_windows, tmp_path,
):
    original = fake_windows.close_handle
    failed = False

    def close_once(handle):
        nonlocal failed
        if handle == HIGH_PROCESS and not failed:
            failed = True
            fake_windows.closed.append(handle)
            return False
        return original(handle)

    fake_windows.close_handle = close_once
    process = _create(fake_windows, tmp_path)
    assert process.close(0) == job.TREE_UNKNOWN
    assert process._closed
    assert process.close(0) == job.TREE_UNKNOWN
    assert fake_windows.closed.count(HIGH_PROCESS) == 2


def test_signaled_leader_may_have_exit_code_259(fake_windows, tmp_path):
    fake_windows.exit_code_value = job.STILL_ACTIVE
    process = _create(fake_windows, tmp_path)
    try:
        assert process.poll() == 259
        assert process.wait(0) == 259
        assert process.returncode == 259
    finally:
        process.close(0)


def test_poll_rechecks_exit_code_if_leader_signals_after_still_active(fake_windows, tmp_path):
    codes = iter((job.STILL_ACTIVE, 7))
    fake_windows.exit_code = lambda _handle: next(codes)
    process = _create(fake_windows, tmp_path)
    try:
        assert process.poll() == 7
        assert process.returncode == 7
    finally:
        process.close(0)


def test_close_retry_rechecks_tree_before_confirming_exit(fake_windows, tmp_path):
    process = _create(fake_windows, tmp_path)
    fake_windows.close_failure.add(HIGH_JOB)
    assert process.close(0) == job.TREE_UNKNOWN
    assert process.controller.query() == job.TREE_UNKNOWN
    fake_windows.close_failure.clear()
    assert process.close(0) == job.TREE_EXITED


def test_wait_timeout_does_not_terminate_job_or_fake_returncode(
    fake_windows, tmp_path, monkeypatch
):
    now = [10.0]
    monkeypatch.setattr(
        job,
        "time",
        SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep),
    )
    fake_windows.exit_code_value = job.STILL_ACTIVE
    fake_windows.wait_result = job.WAIT_TIMEOUT
    fake_windows.active = 1

    def advance_wait(_process, milliseconds):
        now[0] += milliseconds / 1000
        return job.WAIT_TIMEOUT

    fake_windows.wait = advance_wait
    process = _create(fake_windows, tmp_path)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        process.wait(0.02)
    assert excinfo.value.timeout == 0.02
    assert fake_windows.terminated == []
    assert process.returncode is None
    assert process.controller.state == job.TREE_RUNNING
    assert process.close(0) == job.TREE_UNKNOWN
    assert fake_windows.terminated == [HIGH_JOB]


def test_close_after_confirmed_exit_does_not_terminate_again(fake_windows, tmp_path):
    process = _create(fake_windows, tmp_path)
    assert process.controller.stop(0) == job.TREE_EXITED
    assert process.close() == job.TREE_EXITED
    assert process.close() == job.TREE_EXITED
    assert fake_windows.terminated == []


def test_backend_spawn_collects_binary_pipes_and_closes_job(fake_windows, tmp_path):
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=1000, max_output_bytes=4096,
    )
    result = job.WindowsJobObjectExecBackend().spawn(
        [sys.executable, "-c", "pass"], cwd=tmp_path, env={}, policy=policy
    )

    assert result.exit_code == 7
    assert result.stdout == "managed-out"
    assert result.stderr == "managed-err"
    assert result.descendants_may_survive is False
    assert fake_windows.terminated == []
    assert fake_windows.pipe_handles == set()
    assert HIGH_JOB in fake_windows.closed


def test_backend_spawn_automatically_retries_close_and_reports_unknown(
    fake_windows, tmp_path,
):
    original = fake_windows.close_handle
    failed = False

    def close_once(handle):
        nonlocal failed
        if handle == HIGH_PROCESS and not failed:
            failed = True
            fake_windows.closed.append(handle)
            return False
        return original(handle)

    fake_windows.close_handle = close_once
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=1000, max_output_bytes=4096,
    )
    result = job.WindowsJobObjectExecBackend().spawn(
        [sys.executable, "-c", "pass"], cwd=tmp_path, env={}, policy=policy
    )

    assert result.exit_code == 7
    assert result.descendants_may_survive is True
    assert fake_windows.closed.count(HIGH_PROCESS) == 2
    assert HIGH_JOB in fake_windows.closed


def test_mcp_execution_close_retries_managed_handle_failure(fake_windows, tmp_path):
    from tianshu.execution_admission import PreparedExecution
    from tianshu.mcp import McpServerConfig, StdioServer

    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=1000, max_output_bytes=4096,
    )
    execution = PreparedExecution(
        job.WindowsJobObjectExecBackend(), kind="local_mcp", task_id="task",
        run_id="run", argv=(sys.executable, "-c", "pass"), cwd=tmp_path,
        env={}, policy=policy, workspace=tmp_path, data_root=tmp_path,
    )
    execution.__enter__()
    proc = _create(fake_windows, tmp_path)
    execution._proc = proc
    original = fake_windows.close_handle
    failed = False

    def close_once(handle):
        nonlocal failed
        if handle == HIGH_PROCESS and not failed:
            failed = True
            fake_windows.closed.append(handle)
            return False
        return original(handle)

    fake_windows.close_handle = close_once
    server = StdioServer(McpServerConfig(
        name="fake", command=(sys.executable, "-c", "pass"),
    ))
    server._execution = execution
    server._proc = proc
    server.close()

    assert proc._closed
    assert fake_windows.closed.count(HIGH_PROCESS) == 2
    assert server._responses.get_nowait() == (None, {})


def test_backend_timeout_cleans_tree_without_fake_leader_exit(fake_windows, tmp_path):
    fake_windows.exit_code_value = job.STILL_ACTIVE
    fake_windows.wait_result = job.WAIT_TIMEOUT
    fake_windows.active = 1
    fake_windows.terminate_active = 0
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=20, max_output_bytes=4096,
    )
    result = job.WindowsJobObjectExecBackend().spawn(
        [sys.executable, "-c", "pass"], cwd=tmp_path, env={}, policy=policy
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.descendants_may_survive is False
    assert fake_windows.terminated == [HIGH_JOB]


def test_backend_pty_is_refused_before_process_creation(fake_windows, tmp_path):
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=1000, max_output_bytes=4096, allow_pty=True,
    )
    with pytest.raises(PolicyUnsupported, match="pty"):
        job.WindowsJobObjectExecBackend().spawn(
            [sys.executable, "-c", "pass"], cwd=tmp_path, env={}, policy=policy, pty=True
        )
    assert fake_windows.created_jobs == 0


def test_subject_admission_uses_native_job_without_popen_fallback(
    fake_windows, tmp_path, monkeypatch
):
    from tianshu import execution_admission

    monkeypatch.setattr(
        execution_admission.subprocess, "Popen",
        lambda *args, **kwargs: pytest.fail("Job subject 不得回落 Popen"),
    )
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=1000, max_output_bytes=4096,
    )
    argv = (sys.executable, "-c", "pass")
    with execution_admission.PreparedExecution(
        job.WindowsJobObjectExecBackend(), kind="command", task_id="task",
        run_id="run", argv=argv, cwd=tmp_path, env={}, policy=policy,
        workspace=tmp_path, data_root=tmp_path,
    ) as execution:
        assert execution._tmp is None
        assert execution._profile_rules["windows_job_object"]["backend"] == (
            "windows_job_object"
        )
        result = execution.spawn(argv, cwd=tmp_path, env={}, policy=policy)

    assert result.exit_code == 7
    assert result.stdout == "managed-out"
    assert result.stderr == "managed-err"
    assert result.descendants_may_survive is False
    assert HIGH_JOB in fake_windows.closed


@pytest.mark.parametrize(
    ("generic", "detached", "expected"),
    [
        ("unknown", "enforced", "unknown"),
        ("not_enforced", "enforced", "not_enforced"),
        ("enforced", "not_enforced", "not_enforced"),
        ("enforced", "unknown", "unknown"),
        ("not_enforced", "unknown", "not_enforced"),
        ("unknown", "not_enforced", "not_enforced"),
        ("enforced", "enforced", "enforced"),
    ],
)
def test_admission_requires_both_process_tree_probes_to_pass(
    tmp_path, monkeypatch, generic, detached, expected,
):
    from tianshu import execution_admission
    from tianshu.execution_admission import IsolationRefused
    from tianshu.isolation import IsolationFinding, IsolationReport

    execution = object.__new__(execution_admission.PreparedExecution)
    execution.workspace = tmp_path
    execution.data_root = tmp_path
    execution.backend = object()
    execution.capabilities = SimpleNamespace(name="fake")
    execution.subject = SimpleNamespace(digest=lambda: "subject-digest")
    execution.lifecycle_id = "lifecycle-id"
    findings = tuple(
        IsolationFinding(
            boundary, generic if boundary == "process_tree" else "enforced",
            f"generic-{boundary}",
        )
        for boundary in ("files_authority", "credentials", "network", "process_tree")
    )
    monkeypatch.setattr(
        execution_admission, "probe_backend",
        lambda *_args, **_kwargs: IsolationReport(
            backend="fake", platform="test", findings=findings,
            workspace=str(tmp_path), data_root=str(tmp_path),
        ),
    )
    monkeypatch.setattr(
        execution_admission, "_backend_identity", lambda _backend: "backend-id",
    )
    monkeypatch.setattr(
        execution, "_probe_detached_descendant",
        lambda: IsolationFinding("process_tree", detached, "detached-probe"),
    )

    if expected == "enforced":
        admission = execution.admit(("process_tree",))
        assert admission.boundaries["process_tree"] == "enforced"
    else:
        with pytest.raises(IsolationRefused) as caught:
            execution.admit(("process_tree",))
        assert caught.value.status == expected


def test_subject_collection_failure_still_closes_job(fake_windows, tmp_path, monkeypatch):
    from tianshu import execution_admission

    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=1000, max_output_bytes=4096,
    )
    argv = (sys.executable, "-c", "pass")
    with execution_admission.PreparedExecution(
        job.WindowsJobObjectExecBackend(), kind="command", task_id="task",
        run_id="run", argv=argv, cwd=tmp_path, env={}, policy=policy,
        workspace=tmp_path, data_root=tmp_path,
    ) as execution:
        closed = []
        proc = SimpleNamespace(
            controller=SimpleNamespace(stop=lambda timeout: job.TREE_UNKNOWN),
            close=lambda timeout: closed.append(timeout) or job.TREE_UNKNOWN,
        )
        monkeypatch.setattr(
            execution_admission, "_run_managed_pipes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("collect")),
        )
        with pytest.raises(OSError, match="collect"):
            execution._collect_process(proc, policy=policy)
    assert closed == [0]


def test_probe_cleanup_does_not_terminate_mismatched_exact_handle(monkeypatch):
    from tianshu.execution_admission import PreparedExecution

    terminated = []
    closed = []
    api = SimpleNamespace(
        process_identity=lambda handle: (11, 20),
        wait=lambda *_args: pytest.fail("身份不符不得等待或终止该句柄"),
        terminate_process=lambda *args: terminated.append(args) or True,
        close_handle=lambda handle: closed.append(handle) or True,
    )
    monkeypatch.setattr(job, "_load_native_api", lambda: api)

    confirmed, survivors = PreparedExecution._cleanup_windows_probe_records([
        {"pid": 10, "birth": 20, "handle": 99},
    ])

    assert confirmed is False
    assert survivors == []
    assert terminated == []
    assert closed == []


@pytest.mark.parametrize(
    ("tree_state", "may_survive", "identity_valid", "close_ok", "survivor", "expected"),
    [
        ("exited", False, True, True, False, "enforced"),
        ("exited", True, True, True, False, "unknown"),
        ("unknown", False, True, True, False, "unknown"),
        ("exited", False, True, False, False, "unknown"),
        ("exited", False, False, True, False, "unknown"),
        ("exited", False, True, True, True, "not_enforced"),
    ],
)
def test_windows_probe_requires_confirmed_tree_and_exact_handle_cleanup(
    tmp_path, monkeypatch, tree_state, may_survive, identity_valid,
    close_ok, survivor, expected,
):
    from tianshu import execution_admission
    from tianshu.exec import ExecResult

    execution = object.__new__(execution_admission.PreparedExecution)
    execution.workspace = tmp_path
    execution._job_backend = True
    closed = []
    proc = SimpleNamespace(close=lambda timeout: closed.append(timeout) or "exited")

    def launch(argv, *, cwd, env):
        assert argv[-1] == str(os.getpid())
        Path(argv[-3]).write_text(json.dumps({"pid": 10, "birth": 20}))
        return proc

    facts = {"descendant_pid": 10, "descendant_handle": 99, "denied_winerror": 5}
    result = ExecResult(
        exit_code=0, signal=None, timed_out=False, stdout=json.dumps(facts),
        stderr="", truncated=False, stdout_ref=None, stderr_ref=None,
        duration_ms=0, descendants_may_survive=may_survive,
    )
    waits = iter([job.WAIT_TIMEOUT, job.WAIT_OBJECT_0] if survivor else [job.WAIT_OBJECT_0])

    def identity(handle):
        if not identity_valid:
            raise job.JobObjectError("identity unavailable")
        return 10, 20

    api = SimpleNamespace(
        process_identity=identity,
        wait=lambda handle, timeout: next(waits),
        terminate_process=lambda handle, code: True,
        close_handle=lambda handle: close_ok,
    )
    monkeypatch.setattr(execution, "_launch_process", launch)
    close_timeouts = []

    def collect(*_args, **kwargs):
        close_timeouts.append(kwargs["close_timeout"])
        return result, tree_state

    monkeypatch.setattr(execution, "_collect_process", collect)
    monkeypatch.setattr(execution_admission.exec_job_object, "_load_native_api", lambda: api)
    monkeypatch.setattr(
        execution_admission.os, "kill",
        lambda *_args, **_kwargs: pytest.fail("Windows 探针不得按 PID 清理"),
    )

    finding = execution._probe_windows_descendant()

    assert finding.status == expected
    assert closed == [5.0]
    assert close_timeouts == [5.0]


def test_windows_detached_probe_accepts_popen_lifecycle(tmp_path, monkeypatch):
    from tianshu import execution_admission
    from tianshu.exec import LocalExecBackend

    policy = ExecPolicy(
        argv_allowlist=(sys.executable,), cwd_roots=(tmp_path,),
        env_allowlist=(), timeout_ms=5000, max_output_bytes=4096,
    )
    execution = execution_admission.PreparedExecution(
        LocalExecBackend(), kind="command", task_id="task", run_id="run",
        argv=(sys.executable, "-c", "pass"), cwd=tmp_path, env={},
        policy=policy, workspace=tmp_path, data_root=tmp_path,
    )
    # A real Popen is reaped through the normal collector. The short child
    # replaces only the Windows-specific probe payload for this regression.
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('{}')"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    monkeypatch.setattr(execution, "_launch_process", lambda *args, **kwargs: proc)
    monkeypatch.setattr(job, "_load_native_api", lambda: SimpleNamespace())

    finding = execution._probe_windows_descendant()

    assert finding.status == "unknown"
    assert proc.returncode == 0
    assert list(tmp_path.glob(".tianshu-detach-probe-*")) == []


def test_windows_detached_probe_requires_native_api_before_launch(tmp_path, monkeypatch):
    from tianshu import execution_admission

    execution = object.__new__(execution_admission.PreparedExecution)
    execution.workspace = tmp_path

    def missing_export():
        raise AttributeError("missing Win32 export")

    monkeypatch.setattr(job, "_load_native_api", missing_export)
    monkeypatch.setattr(
        execution, "_launch_process",
        lambda *args, **kwargs: pytest.fail("launched a probe without cleanup API"),
    )

    finding = execution._probe_windows_descendant()

    assert finding.status == "unknown"
    assert "missing Win32 export" in finding.evidence
    assert list(tmp_path.glob(".tianshu-detach-probe-*")) == []


def test_probe_allows_the_exact_interpreter_it_starts(tmp_path):
    from tianshu import isolation
    from tianshu.exec import ExecResult
    from tianshu.execpolicy import check_spawn

    executable = str(tmp_path / "base-python.exe")

    def spawn(argv, *, cwd, env, policy):
        assert argv[0] == executable
        check_spawn(policy, argv, cwd, env)
        return ExecResult(0, None, False, "READY=1", "", False, None, None, 0)

    result, parsed, problem = isolation._run_child(
        SimpleNamespace(spawn=spawn), "pass", [], workspace=tmp_path,
        timeout_ms=1000, env={}, env_allowlist=(), executable=executable,
    )
    assert result is not None, problem
    assert parsed == {"READY": "1"}


@pytest.mark.parametrize(
    ("wait_mode", "may_survive", "identity_valid", "close_ok", "expected"),
    [
        ("exited", False, True, True, "enforced"),
        ("exited", True, True, True, "unknown"),
        ("failed", False, True, True, "unknown"),
        ("alive", False, True, True, "not_enforced"),
        ("exited", False, False, True, "unknown"),
        ("exited", False, True, False, "unknown"),
    ],
)
def test_windows_generic_probe_uses_exact_handle_and_never_pid_kill(
    tmp_path, monkeypatch, wait_mode, may_survive, identity_valid, close_ok, expected,
):
    from tianshu import isolation
    from tianshu.exec import ExecResult

    waits = []
    terminated = []
    closed = []
    killed = []

    class ProbeApi:
        def GetProcessId(self, _handle):
            return 10 if identity_valid else 11

        def GetProcessTimes(self, _handle, created, _exited, _kernel, _user):
            target = getattr(created, "_obj", created)
            target.dwLowDateTime = 20
            target.dwHighDateTime = 0
            return 1

        def WaitForSingleObject(self, _handle, _milliseconds):
            waits.append(_handle)
            if wait_mode == "failed":
                return 0xFFFFFFFF
            if wait_mode == "alive" and not terminated:
                return job.WAIT_TIMEOUT
            return job.WAIT_OBJECT_0

        def TerminateProcess(self, handle, code):
            terminated.append((handle, code))
            return True

        def CloseHandle(self, handle):
            closed.append(handle)
            return close_ok

    api = ProbeApi()
    monkeypatch.setattr(isolation, "_open_kernel32", lambda: api)
    monkeypatch.setattr(isolation, "_configure_windows_probe_api", lambda _api: None)
    monkeypatch.setattr(isolation, "PROBE_GRACE_MS", 5)
    monkeypatch.setattr(isolation, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(isolation.os, "kill", lambda *args: killed.append(args) or 0)
    result = ExecResult(
        exit_code=None, signal=None, timed_out=True, stdout="", stderr="",
        truncated=False, stdout_ref=None, stderr_ref=None, duration_ms=1,
        descendants_may_survive=may_survive,
    )

    def run_child(_backend, _script, args, **_kwargs):
        assert args[1] == os.getpid()
        Path(args[0]).write_bytes(json.dumps({"pid": 10, "birth": 20}).encode("utf-8"))
        return result, {"GRANDCHILD_HANDLE": "99"}, "probe-timeout"

    monkeypatch.setattr(isolation, "_run_child", run_child)
    finding = isolation._probe_windows_process_tree(object(), tmp_path, 1000)

    assert finding.status == expected
    assert killed == []
    assert list(tmp_path.iterdir()) == []
    if identity_valid:
        assert closed and set(closed) == {99}
    else:
        assert closed == []
        assert waits == []
        assert terminated == []
    if wait_mode == "alive":
        assert terminated == [(99, 1)]


def test_windows_generic_probe_missing_identity_is_unknown_without_pid_cleanup(
    tmp_path, monkeypatch,
):
    from tianshu import isolation
    from tianshu.exec import ExecResult

    result = ExecResult(
        exit_code=None, signal=None, timed_out=True, stdout="", stderr="",
        truncated=False, stdout_ref=None, stderr_ref=None, duration_ms=1,
        descendants_may_survive=False,
    )
    monkeypatch.setattr(
        isolation, "_run_child",
        lambda *_args, **_kwargs: (result, {}, "missing-record"),
    )
    monkeypatch.setattr(
        isolation, "_open_kernel32",
        lambda: pytest.fail("没有精确句柄时不得打开或清理进程"),
    )
    finding = isolation._probe_windows_process_tree(object(), tmp_path, 1000)
    assert finding.status == "unknown"
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------- Windows 原生证据

native_only = pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows Job Object")


# Run the actual executable; the Windows venv redirector uses its own Job.
_NATIVE_PYTHON = getattr(sys, "_base_executable", sys.executable)


# The control process is outside the Job. Load the test-only isolation hook
# explicitly because -I -S ignores PYTHONPATH and skips automatic startup hooks.
_JOB_CONTROL_HELPER = """\
import json, os, runpy, sys, time
from contextlib import closing
from pathlib import Path

source, startup, database, control, run_id, job_id, *paths = sys.argv[1:]
sys.path.insert(0, source)
expected_lock = Path(os.environ["TIANSHU_TEST_LOCK_ROOT"])
expected_registry = Path(os.environ["PYTEST_TIANSHU_REGISTRY_HOME"])
runpy.run_path(startup)
from tianshu import maintenance, restore_registry
from tianshu.events import EventLog
from tianshu.jobs import JobStore
from tianshu.tasks import TaskStore
assert maintenance._lock_root() == expected_lock
assert restore_registry._user_home() == expected_registry

deadline = time.monotonic() + 25
while True:
    try:
        records = [json.loads(Path(path).read_bytes()) for path in paths]
    except (OSError, ValueError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.02)
    else:
        break
with closing(EventLog(Path(database))) as log:
    claims = JobStore(log)
    tasks = TaskStore(log)
    if control == "cancel":
        assert tasks.request_cancel(run_id).control_requested == "cancel"
    elif control == "claim_lost":
        assert claims.cancel(job_id, reason="test claim lost").state == "cancelled"
    else:
        assert control == "fenced"
        # Public goal revisions wait for the execution guard; drive the same
        # fence transition directly to exercise a fence while the Job is live.
        with log.transaction() as tx:
            tasks._fence_run(tx, run_id, int(time.time() * 1000), reason="test in-flight fence")
        assert tasks.get_run(run_id).state == "fenced"
"""


def _existing_process_records(paths: list[Path]) -> list[dict[str, int]]:
    records = []
    for path in paths:
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and all(
            type(record.get(key)) is int and record[key] > 0 for key in ("pid", "birth")
        ):
            records.append(record)
    return records


@native_only
class TestWindowsNative:
    @pytest.mark.parametrize(
        ("backend_name", "expected"),
        [("local", "not_enforced"), ("windows_job_object", "enforced")],
    )
    def test_generic_process_tree_probe_duplicates_and_cleans_exact_handle(
        self, tmp_path, backend_name, expected,
    ):
        from tianshu import isolation
        from tianshu.exec import LocalExecBackend

        backend = (
            LocalExecBackend() if backend_name == "local"
            else job.WindowsJobObjectExecBackend()
        )
        finding = isolation._probe_windows_process_tree(backend, tmp_path, 1000)
        assert finding.status == expected, finding
        assert "精确HANDLE=" in finding.evidence
        assert list(tmp_path.iterdir()) == []

    def test_local_mcp_job_close_reaps_threads_and_wakes_waiters(self, tmp_path):
        from tianshu.mcp import McpServerConfig, StdioServer

        fixture = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
        config = McpServerConfig(
            name="native-job", command=(_NATIVE_PYTHON, str(fixture)),
        )
        server = StdioServer(
            config, default_cwd=tmp_path,
            execution_scope={
                "exec_backend": job.WindowsJobObjectExecBackend(),
                "required_boundaries": ("process_tree",),
                "workspace": str(tmp_path), "data_root": str(tmp_path),
                "task_id": "native-task", "run_id": "native-run",
            },
        )
        try:
            server.start()
            result = server.call_tool("echo", {"message": "job"})
            assert result.text_parts == ("echo:job",)
        finally:
            server.close()

        assert server._proc._closed
        assert server._proc._close_state == job.TREE_EXITED
        assert not server._reader.is_alive()
        assert not server._stderr_reader.is_alive()
        assert server._responses.get_nowait() == (None, {})

    def test_pipe_stdio_wait_pid_returncode_and_close(self, tmp_path):
        script = tmp_path / "stdio.py"
        script.write_text(
            "import os,sys\n"
            "data=os.read(0,100)\n"
            "os.write(1,b'out:'+data)\n"
            "os.write(2,b'err:'+data)\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        process = job.create_managed_process(
            [_NATIVE_PYTHON, "-I", "-S", str(script)], cwd=tmp_path, env={}
        )
        try:
            assert process.pid > 0
            process.stdin.write(b"input")
            process.stdin.close()
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            assert process.wait(15) == 7
            assert process.returncode == 7
            assert _read_available(process.stdout) == b"out:input"
            assert _read_available(process.stderr) == b"err:input"
            assert process.controller.query() == job.TREE_EXITED
        finally:
            assert process.close() == job.TREE_EXITED
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))

    @pytest.mark.parametrize("control", ["cancel", "claim_lost", "fenced"])
    def test_dispatch_job_run_control_clears_native_tree(self, tmp_path, control):
        from test_isolation_gate import _exec_request, _make_env, _policy

        from tianshu.permissions import HumanIdentity

        env = _make_env(tmp_path, exec_backend=job.WindowsJobObjectExecBackend())
        helper = None
        records = []
        record_paths = [env.ws / f"tree{index}.json" for index in range(3)]
        try:
            scripts = self._tree_scripts(env.ws, detached=True, generations=3)
            args = {"argv": [_NATIVE_PYTHON, "-I", "-S", str(scripts[0])]}
            task, run, claim = env.new_run(exec_policy=_policy(
                env.ws, argv_allowlist=(_NATIVE_PYTHON,), timeout_ms=30_000,
            ))
            decision = env.dispatcher.prepare(
                _exec_request(env, run, claim, args=args, task=task), claim, args=args,
            )
            assert decision.is_waiting, decision
            env.approvals.decide(
                decision.approval_id, decision="approved", actor=HumanIdentity.local_cli(),
            )
            control_script = tmp_path / "request-control.py"
            control_script.write_bytes(_JOB_CONTROL_HELPER.encode("utf-8"))
            helper = subprocess.Popen(
                [_NATIVE_PYTHON, "-I", "-S", str(control_script),
                 str(Path(job.__file__).resolve().parents[1]),
                 str(Path(__file__).resolve().parent / "_startup" / "sitecustomize.py"),
                 str(tmp_path / "events.db"), control, run.run_id, claim.job_id,
                 *map(str, record_paths)],
                cwd=tmp_path, stderr=subprocess.PIPE,
            )
            result = env.dispatcher.dispatch(decision.request_id, claim)
            assert result.status == "failed" and result.effect_state == "applied", result
            assert result.output["interrupted"] and not result.output["timed_out"], result
            _, stderr = helper.communicate(timeout=10)
            assert helper.returncode == 0, stderr.decode(errors="replace")
            records = _wait_records(record_paths)
            history = env.log.read_run(run.run_id)
            ended = [row for row in history if row.type == "execution.ended"]
            assert [row.payload["process_tree"] for row in ended] == ["exited"]
            if control == "fenced":
                assert next(row.seq for row in history if row.type == "run.fenced") < ended[0].seq
            _assert_dead(records)
        finally:
            if helper is not None:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait(10)
                helper.stderr.close()
            _cleanup_records(records or _existing_process_records(record_paths))
            env.log.close()

    def test_child_and_grandchild_timeout_kills_real_tree(self, tmp_path):
        scripts = self._tree_scripts(tmp_path, detached=False, generations=3)
        paths = [tmp_path / f"tree{index}.json" for index in range(3)]
        backend = job.WindowsJobObjectExecBackend()
        policy = ExecPolicy(
            argv_allowlist=(_NATIVE_PYTHON,), cwd_roots=(tmp_path,),
            timeout_ms=8000, max_output_bytes=4096,
        )
        try:
            result = backend.spawn(
                [_NATIVE_PYTHON, "-I", "-S", str(scripts[0])], cwd=tmp_path,
                env={}, policy=policy,
            )
            records = _existing_process_records(paths)
            assert len(records) == len(paths), "子进程树未在超时前全部启动"
            assert result.timed_out and not result.descendants_may_survive, result
            _assert_dead(records)
        finally:
            _cleanup_records(_existing_process_records(paths))

    def test_multigeneration_detached_processes_stay_in_job(self, tmp_path):
        identities = self._spawn_tree(tmp_path, detached=True, generations=4)
        process = identities.pop("process")
        records = []
        try:
            paths = [identities[f"level{index}"] for index in range(4)]
            records = _wait_records(paths)
            assert process.controller.query() == job.TREE_RUNNING
            assert process.controller.stop(10) == job.TREE_EXITED
            _assert_dead(records)
        finally:
            process.close()
            _cleanup_records(records)

    def test_leader_normal_exit_still_queries_and_kills_descendants(self, tmp_path):
        child_script = tmp_path / "child.py"
        child_record = tmp_path / "child.json"
        root_script = tmp_path / "root.py"
        child_script.write_text(
            _PROCESS_IDENTITY_SOURCE + f"_record({str(child_record)!r})\ntime.sleep(120)\n",
            encoding="utf-8",
        )
        root_script.write_text(
            "import subprocess,sys\n"
            + f"p=subprocess.Popen([sys.executable,'-I','-S',{str(child_script)!r}],"
            "creationflags=0x00000008,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            # leader 先退，不 wait 后代。
            "",
            encoding="utf-8",
        )
        process = job.create_managed_process(
            [_NATIVE_PYTHON, "-I", "-S", str(root_script)], cwd=tmp_path, env={}
        )
        records = []
        try:
            record = _wait_records([child_record])[0]
            records = [record]
            assert process.wait(10) == 0
            assert process.controller.query() == job.TREE_RUNNING
            assert process.controller.stop(10) == job.TREE_EXITED
            _assert_dead(records)
        finally:
            process.close()
            _cleanup_records(records)

    def test_subject_admission_process_tree_and_run(self, tmp_path):
        from tianshu.execution_admission import PreparedExecution

        workspace = tmp_path / "workspace"
        data_root = tmp_path / "data"
        workspace.mkdir()
        data_root.mkdir()
        policy = ExecPolicy(
            argv_allowlist=(_NATIVE_PYTHON,), cwd_roots=(workspace,),
            env_allowlist=("PATH",), timeout_ms=15_000, max_output_bytes=64 * 1024,
        )
        with PreparedExecution(
            job.WindowsJobObjectExecBackend(), kind="command", task_id="native-task",
            run_id="native-run", argv=(_NATIVE_PYTHON, "-c", "pass"),
            cwd=workspace, env={"PATH": os.defpath}, policy=policy,
            workspace=workspace, data_root=data_root,
        ) as execution:
            admission = execution.admit(required_boundaries=("process_tree",))
            assert admission.boundaries["process_tree"] == "enforced"
            result = execution.run()
            assert execution.reconcile()["process_tree"] == "exited"

        assert result.exit_code == 0
        assert result.descendants_may_survive is False

    def test_breakaway_is_denied_and_regular_descendant_is_killed(self, tmp_path):
        script = tmp_path / "breakaway.py"
        denied = tmp_path / "denied.json"
        child_record = tmp_path / "child.json"
        child_script = tmp_path / "child.py"
        child_script.write_text(
            _PROCESS_IDENTITY_SOURCE + f"_record({str(child_record)!r})\ntime.sleep(120)\n",
            encoding="utf-8",
        )
        script.write_text(
            "import json,subprocess,sys,time\n"
            f"denied={str(denied)!r}\n"
            f"child_record={str(child_record)!r}\n"
            "code='import time; time.sleep(120)'\n"
            "try:\n"
            " p=subprocess.Popen([sys.executable,'-I','-S','-c',code],"
            "creationflags=0x01000000,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "except OSError as exc:\n"
            " open(denied,'w').write(json.dumps({'winerror':exc.winerror}))\n"
            "else:\n"
            " open(denied,'w').write(json.dumps({'winerror':0}))\n"
            " p.terminate(); p.wait()\n"
            # 普通后代仍必须被 Job 杀死。
            "p=subprocess.Popen([sys.executable,'-I','-S',"
            f"{str(child_script)!r}],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"while not __import__('os').path.exists({str(child_record)!r}): time.sleep(0.01)\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        process = job.create_managed_process(
            [_NATIVE_PYTHON, "-I", "-S", str(script)], cwd=tmp_path, env={}
        )
        records = []
        try:
            assert _wait_records([denied], key="winerror")[0]["winerror"] == 5
            records = _wait_records([child_record])
            assert process.controller.stop(10) == job.TREE_EXITED
            _assert_dead(records)
        finally:
            process.close()
            _cleanup_records(records)

    @pytest.mark.parametrize("kill_on_close", [True, False], ids=["enabled", "negative-control"])
    def test_os_closing_parent_job_handle(self, tmp_path, kill_on_close):
        # helper 自己创建 Job；helper 硬退后由 OS 关闭其最后一个 Job 句柄。
        helper = tmp_path / "close_parent.py"
        root_script = tmp_path / "root.py"
        child_script = tmp_path / "child.py"
        child_record = tmp_path / "child.json"
        child_script.write_text(
            _PROCESS_IDENTITY_SOURCE + f"_record({str(child_record)!r})\ntime.sleep(120)\n",
            encoding="utf-8",
        )
        root_script.write_text(
            "import subprocess,sys,time\n"
            f"p=subprocess.Popen([sys.executable,'-I','-S',{str(child_script)!r}],"
            "creationflags=0x00000008,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        helper.write_text(
            "import json,os,sys,time\n"
            f"sys.path.insert(0,{str(Path(job.__file__).resolve().parents[1])!r})\n"
            "from tianshu.exec_job_object import _NativeAPI, create_managed_process\n"
            + ("" if kill_on_close else (
                "set_limits=_NativeAPI.set_job_limits\n"
                "_NativeAPI.set_job_limits=lambda self, handle, flags: "
                "set_limits(self, handle, 0)\n"
            ))
            + "p=create_managed_process([sys.executable,'-I','-S',"
            f"{str(root_script)!r}],cwd={str(tmp_path)!r},env={{}})\n"
            f"leader={str(tmp_path / 'leader.json')!r}\n"
            "pid,birth=p.controller._api.process_identity(p.controller._process)\n"
            "with open(leader,'w',encoding='utf-8') as record:\n"
            " json.dump({'pid':pid,'birth':birth},record)\n"
            "while True:\n"
            " try:\n"
            f"  with open({str(child_record)!r},encoding='utf-8') as record: json.load(record)\n"
            " except (FileNotFoundError,json.JSONDecodeError): time.sleep(0.01)\n"
            " else: break\n"
            # 不调用 close/finalizer；os._exit 让 Windows 关闭父进程全部 HANDLE。
            "os._exit(0)\n",
            encoding="utf-8",
        )
        helper_process = subprocess.Popen(
            [_NATIVE_PYTHON, "-I", "-S", str(helper)],
            cwd=tmp_path,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        records = []
        try:
            records = _wait_records([tmp_path / "leader.json", child_record])
            assert helper_process.wait(20) == 0
            if kill_on_close:
                _assert_dead(records, timeout_ms=5000)
            else:
                for record in records:
                    _assert_alive(record)
        finally:
            if helper_process.poll() is None:
                helper_process.kill()
                helper_process.wait(10)
            _cleanup_records(records)

    def test_stop_does_not_kill_unrelated_pid_identity(self, tmp_path):
        outsider_script = tmp_path / "outsider.py"
        outsider_ready = tmp_path / "outsider.ready"
        outsider_script.write_text(
            _PROCESS_IDENTITY_SOURCE + f"_record({str(outsider_ready)!r})\ntime.sleep(120)\n",
            encoding="utf-8",
        )
        outsider = subprocess.Popen(
            [_NATIVE_PYTHON, "-I", "-S", str(outsider_script)],
            cwd=tmp_path,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process = None
        try:
            _wait_records([outsider_ready])
            outsider_identity = _handle_identity(outsider._handle)
            process = job.create_managed_process(
                [_NATIVE_PYTHON, "-I", "-S", "-c", "import time; time.sleep(120)"],
                cwd=tmp_path,
                env={},
            )
            assert process.pid != outsider_identity["pid"]
            assert process.controller.stop(10) == job.TREE_EXITED
            _assert_alive(outsider_identity)
        finally:
            if process is not None:
                process.close()
            outsider.terminate()
            outsider.wait(10)

    def test_probe_cleanup_refuses_stale_birth_on_exact_handle(self, tmp_path):
        from tianshu.execution_admission import PreparedExecution

        ready = tmp_path / "outsider.ready"
        script = _PROCESS_IDENTITY_SOURCE + f"_record({str(ready)!r})\ntime.sleep(120)\n"
        outsider = subprocess.Popen(
            [_NATIVE_PYTHON, "-I", "-S", "-c", script],
            cwd=tmp_path, env=os.environ.copy(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        duplicate = None
        try:
            identity = _wait_records([ready])[0]
            original = _handle_identity(outsider._handle)
            kernel32 = _identity_api()
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = job._HANDLE
            kernel32.DuplicateHandle.argtypes = [
                job._HANDLE, job._HANDLE, job._HANDLE,
                ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                wintypes.BOOL, wintypes.DWORD,
            ]
            kernel32.DuplicateHandle.restype = job._BOOL
            duplicate = wintypes.HANDLE()
            assert kernel32.DuplicateHandle(
                kernel32.GetCurrentProcess(), outsider._handle,
                kernel32.GetCurrentProcess(), ctypes.byref(duplicate), 0, False, 2,
            )
            confirmed, survivors = PreparedExecution._cleanup_windows_probe_records([
                {"pid": identity["pid"], "birth": original["birth"] + 1,
                 "handle": duplicate.value},
            ])
            assert confirmed is False
            assert survivors == []
            _assert_alive(original)
        finally:
            if duplicate is not None and duplicate.value:
                _identity_api().CloseHandle(duplicate)
            outsider.terminate()
            outsider.wait(10)

    @staticmethod
    def _tree_scripts(tmp_path: Path, *, detached: bool, generations: int):
        scripts = []
        flags = "0x00000008" if detached else "0"
        for index in range(generations):
            script = tmp_path / f"tree{index}.py"
            record = tmp_path / f"tree{index}.json"
            if index + 1 == generations:
                source = f"_record({str(record)!r})\ntime.sleep(120)\n"
            else:
                source = (
                    f"_record({str(record)!r})\n"
                    "p=subprocess.Popen([sys.executable,'-I','-S',"
                    f"{str(tmp_path / f'tree{index + 1}.py')!r}],creationflags={flags},"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                    "time.sleep(120)\n"
                )
            script.write_text(_PROCESS_IDENTITY_SOURCE + source, encoding="utf-8")
            scripts.append(script)
        return scripts

    @classmethod
    def _spawn_tree(cls, tmp_path: Path, *, detached: bool, generations: int = 3):
        scripts = cls._tree_scripts(tmp_path, detached=detached, generations=generations)
        process = job.create_managed_process(
            [_NATIVE_PYTHON, "-I", "-S", str(scripts[0])], cwd=tmp_path, env={}
        )
        identities = {"process": process}
        identities.update(
            {f"level{index}": tmp_path / f"tree{index}.json" for index in range(generations)}
        )
        return identities


_PROCESS_IDENTITY_SOURCE = """
import ctypes,json,os,subprocess,sys,time
from ctypes import wintypes
_k=ctypes.WinDLL('kernel32',use_last_error=True)
_k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
_k.OpenProcess.restype=wintypes.HANDLE
_k.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
_k.GetProcessTimes.restype=wintypes.BOOL
_k.CloseHandle.argtypes=[wintypes.HANDLE]
_k.CloseHandle.restype=wintypes.BOOL
def _birth():
    h=_k.OpenProcess(0x0400,False,os.getpid())
    if not h: raise OSError(ctypes.get_last_error(),'OpenProcess identity')
    try:
        created=wintypes.FILETIME(); exited=wintypes.FILETIME()
        kernel=wintypes.FILETIME(); user=wintypes.FILETIME()
        if not _k.GetProcessTimes(
            h,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel),ctypes.byref(user)
        ):
            raise OSError(ctypes.get_last_error(),'GetProcessTimes identity')
        return (created.dwHighDateTime<<32)|created.dwLowDateTime
    finally: _k.CloseHandle(h)
def _record(path):
    with open(path,'w',encoding='utf-8') as f:
        json.dump({'pid':os.getpid(),'birth':_birth()},f)
"""


def _read_available(stream) -> bytes:
    chunks = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            chunk = os.read(stream.fileno(), 65536)
        except BlockingIOError:
            threading.Event().wait(0.01)
            continue
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
    raise AssertionError("stdout/stderr 管道未在有界期限内排空")


def _wait_records(
    paths: list[Path], timeout: float = 20, *, key: str = "pid",
) -> list[dict[str, int]]:
    deadline = time.monotonic() + timeout
    records = []
    for path in paths:
        while time.monotonic() < deadline:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                threading.Event().wait(0.01)
                continue
            if not isinstance(value, dict) or type(value.get(key)) is not int:
                raise AssertionError(f"无效 {key} 屏障：{path}: {value}")
            records.append(value)
            break
        else:
            raise AssertionError(f"子进程屏障超时：{path}")
    return records


def _identity_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [job._DWORD, job._BOOL, job._DWORD]
    kernel32.OpenProcess.restype = job._HANDLE
    kernel32.GetProcessTimes.argtypes = [job._HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = job._BOOL
    kernel32.GetProcessId.argtypes = [job._HANDLE]
    kernel32.GetProcessId.restype = job._DWORD
    kernel32.WaitForSingleObject.argtypes = [job._HANDLE, job._DWORD]
    kernel32.WaitForSingleObject.restype = job._DWORD
    kernel32.CloseHandle.argtypes = [job._HANDLE]
    kernel32.CloseHandle.restype = job._BOOL
    return kernel32


def _handle_identity(handle: int) -> dict[str, int]:
    kernel32 = _identity_api()
    pid = kernel32.GetProcessId(handle)
    if not pid:
        raise ctypes.WinError(ctypes.get_last_error())
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "pid": pid,
        "birth": (created.dwHighDateTime << 32) | created.dwLowDateTime,
    }


def _assert_dead(
    records: list[dict[str, int]], *, require_birth: bool = True, timeout_ms: int = 2000,
) -> None:
    # The Job accounting count can reach zero before the process object is signaled.
    kernel32 = _identity_api()
    for record in records:
        if require_birth:
            assert isinstance(record.get("birth"), int), record
        handle = kernel32.OpenProcess(0x0400 | 0x00100000, False, record["pid"])
        if not handle:
            error = ctypes.get_last_error()
            assert error == 87, f"无法确认 PID {record['pid']} 已退出：WinError {error}"
            continue
        try:
            if require_birth:
                current = _handle_identity(handle)
                if current["birth"] != record["birth"]:
                    continue  # 原进程已退出；当前 PID 属于后来复用的身份。
            result = kernel32.WaitForSingleObject(handle, timeout_ms)
            assert result == job.WAIT_OBJECT_0, f"PID {record['pid']} 仍存活：{result}"
        finally:
            kernel32.CloseHandle(handle)


def _assert_alive(record: dict[str, int]) -> None:
    kernel32 = _identity_api()
    handle = kernel32.OpenProcess(0x0400 | 0x00100000, False, record["pid"])
    assert handle, ctypes.WinError(ctypes.get_last_error())
    try:
        assert _handle_identity(handle)["birth"] == record["birth"]
        assert kernel32.WaitForSingleObject(handle, 0) == job.WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _cleanup_records(records, *, require_birth: bool = True) -> None:
    if os.name != "nt":
        return
    kernel32 = _identity_api()
    kernel32.TerminateProcess.argtypes = [job._HANDLE, job._UINT]
    kernel32.TerminateProcess.restype = job._BOOL
    for record in records:
        handle = kernel32.OpenProcess(0x0001 | 0x0400 | 0x00100000, False, record["pid"])
        if not handle:
            continue
        try:
            if require_birth and _handle_identity(handle)["birth"] != record["birth"]:
                continue
            kernel32.TerminateProcess(handle, 1)
            kernel32.WaitForSingleObject(handle, 5000)
        finally:
            kernel32.CloseHandle(handle)



def test_unknown_completion_blocks_another_data_root_when_terminal_is_visible(
    tmp_path, monkeypatch, fake_windows,
):
    """源库一旦公布未知终态，另一进程的普通写入也必须被拒绝。"""
    from contextlib import contextmanager

    from test_isolation_gate import _exec_request, _make_env, _policy

    from tianshu import isolation
    from tianshu.execution_admission import PreparedExecution
    from tianshu.permissions import HumanIdentity

    backend = job.WindowsJobObjectExecBackend()
    measured = isolation.IsolationFinding("process_tree", "enforced", "measured")
    monkeypatch.setattr(isolation, "_probe_process_tree", lambda *_a: measured)
    monkeypatch.setattr(PreparedExecution, "_probe_detached_descendant", lambda self: measured)
    env = _make_env(tmp_path, exec_backend=backend)
    try:
        task, run, claim = env.new_run(exec_policy=_policy(env.ws))
        args = {"argv": [sys.executable, "-c", "pass"]}
        decision = env.dispatcher.prepare(
            _exec_request(env, run, claim, args=args, task=task), claim, args=args,
        )
        assert decision.is_waiting
        env.approvals.decide(
            decision.approval_id, decision="approved", actor=HumanIdentity.local_cli(),
        )
        fake_windows.active = 1
        fake_windows.failure = "query"
        other_root = tmp_path / "other-data"
        other_root.mkdir()
        target = env.ws / "cross-root-race.md"
        child_result = None
        transaction = env.log.transaction

        @contextmanager
        def check_other_root_after_commit():
            nonlocal child_result
            with transaction() as tx:
                yield tx
            if env.log.in_transaction or child_result is not None or not any(
                e.type == "execution.ended" and e.payload["process_tree"] == "unknown"
                for e in env.log.read_run(run.run_id)
            ):
                return
            child_result = subprocess.run(
                [sys.executable, "-c", """
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from test_isolation_gate import _make_env
from tianshu.dispatcher import build_request
from tianshu.exec_backends import exec_backend

root, target = map(Path, sys.argv[2:])
env = replace(_make_env(root, exec_backend=exec_backend('local')), ws=target.parent)
try:
    task, run, claim = env.new_run(exec_policy=None)
    args = {'name': target.name, 'content': 'cross-root marker'}
    request = build_request(task=task, run=run, claim=claim, call_id='cross-root-write',
                            tool_id='create_report', target=target, args=args)
    decision = env.dispatcher.prepare(request, claim, args=args)
    if decision.is_rejected:
        print(json.dumps({'rule': decision.rule_id}))
    else:
        assert not decision.is_waiting, decision
        result = env.dispatcher.dispatch(decision.request_id, claim)
        print(json.dumps({'rule': result.output.get('rule_id')}))
finally:
    env.log.close()
""", str(Path(__file__).resolve().parent), str(other_root), str(target)],
                capture_output=True, text=True, timeout=20, check=False,
            )

        monkeypatch.setattr(env.log, "transaction", check_other_root_after_commit)
        result = env.dispatcher.dispatch(decision.request_id, claim)
        assert result.status == "failed" and result.effect_state == "unknown"
        assert child_result is not None
        assert child_result.returncode == 0, child_result.stderr
        assert not target.exists(), "另一数据根在未知终态公布后仍写入了文件"
        assert json.loads(child_result.stdout) == {"rule": "dispatch.workspace_blocked"}
    finally:
        env.log.close()
