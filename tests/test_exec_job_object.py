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
from tianshu.execpolicy import PolicyUnsupported

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

    def heap_free(self, memory: int) -> None:
        self.freed_attributes.append(memory)

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
        self.inherited_handle_lists.append(tuple(handle.value for handle in handles))
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

    def adopt_pipe(handle: int, mode: str):
        # open_osfhandle 接管原始句柄；dup 后关闭 raw，模拟这一所有权转移。
        fd = os.dup(handle)
        os.close(handle)
        api.pipe_handles.discard(handle)
        return os.fdopen(fd, mode, buffering=0)

    monkeypatch.setattr(job, "_wrap_pipe", adopt_pipe)
    return api


def _create(api: FakeNative, tmp_path: Path, **kwargs):
    argv = kwargs.pop("argv", [sys.executable, "-c", "pass"])
    return job.create_managed_process(argv, cwd=tmp_path, env={}, **kwargs)


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
                "CreatePipe",
                "SetHandleInformation",
                "GetProcessHeap",
                "HeapAlloc",
                "HeapFree",
                "InitializeProcThreadAttributeList",
                "UpdateProcThreadAttributeList",
                "DeleteProcThreadAttributeList",
                "CreateProcessW",
                "ResumeThread",
                "IsProcessInJob",
                "GetExitCodeProcess",
                "WaitForSingleObject",
                "CloseHandle",
            )
        }
    )
    job._declare_prototypes(kernel32)
    for function in vars(kernel32).values():
        assert isinstance(function.argtypes, list)
        assert function.restype is not None
    assert kernel32.CreateJobObjectW.restype is job._HANDLE
    assert kernel32.GetProcessHeap.restype is job._HANDLE
    assert kernel32.QueryInformationJobObject.argtypes[0] is job._HANDLE
    assert kernel32.QueryInformationJobObject.argtypes[2] is job._LPVOID
    assert kernel32.TerminateJobObject.restype is job._BOOL
    assert kernel32.ResumeThread.restype is job._DWORD
    assert kernel32.CloseHandle.argtypes == [job._HANDLE]


@pytest.mark.parametrize("failure", ["job", "limits", "allocate", "initialize", "update"])
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
        assert HIGH_PROCESS in fake_windows.closed
        assert HIGH_THREAD in fake_windows.closed
        assert fake_windows.resume_calls == []
    elif failure in {"handle_list", "create"}:
        assert fake_windows.terminated == [HIGH_JOB]
    if failure != "job":
        assert HIGH_JOB in fake_windows.closed


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


def test_close_attempts_all_handles_even_if_one_close_fails(fake_windows):
    fake_windows.close_failure.add(HIGH_PROCESS)
    controller = _controller(fake_windows)
    assert controller.close(0) == job.TREE_UNKNOWN
    assert fake_windows.closed[-3:] == [HIGH_PROCESS, HIGH_THREAD, HIGH_JOB]
    assert controller.query() == job.TREE_CLOSED
    assert controller.close() == job.TREE_CLOSED


def test_wait_timeout_does_not_terminate_job_or_fake_returncode(
    fake_windows, tmp_path, monkeypatch
):
    now = [10.0]
    monkeypatch.setattr(job.time, "monotonic", lambda: now[0])
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


# --------------------------------------------------------------- Windows 原生证据

native_only = pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows Job Object")


@native_only
class TestWindowsNative:
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
            [sys.executable, "-I", "-S", str(script)], cwd=tmp_path, env={}
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

    def test_child_and_grandchild_timeout_kills_real_tree(self, tmp_path):
        identities = self._spawn_tree(tmp_path, detached=False)
        process = identities.pop("process")
        records = []
        try:
            records = _wait_records([identities[name] for name in ("root", "child", "grandchild")])
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(0.2)
            assert process.controller.state == job.TREE_RUNNING
            assert process.controller.stop(10) == job.TREE_EXITED
            _assert_dead(records)
        finally:
            process.close()
            _cleanup_records(records.values())

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
            _cleanup_records(records.values())

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
            "creationflags=0x00000008|0x00000200,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            # leader 先退，不 wait 后代。
            "",
            encoding="utf-8",
        )
        process = job.create_managed_process(
            [sys.executable, "-I", "-S", str(root_script)], cwd=tmp_path, env={}
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
            [sys.executable, "-I", "-S", str(script)], cwd=tmp_path, env={}
        )
        records = []
        try:
            _wait_records([denied])
            assert json.loads(denied.read_text(encoding="utf-8"))["winerror"] != 0
            records = _wait_records([child_record])
            assert process.controller.stop(10) == job.TREE_EXITED
            _assert_dead(records)
        finally:
            process.close()
            _cleanup_records(records)

    def test_os_closing_parent_job_handle_kills_tree(self, tmp_path):
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
            "creationflags=0x00000008|0x00000200,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        helper.write_text(
            "import os,sys,time\n"
            f"sys.path.insert(0,{str(Path(job.__file__).resolve().parents[1])!r})\n"
            "from tianshu.exec_job_object import create_managed_process\n"
            "p=create_managed_process([sys.executable,'-I','-S',"
            f"{str(root_script)!r}],cwd={str(tmp_path)!r},env={{}})\n"
            f"while not os.path.exists({str(child_record)!r}): time.sleep(0.01)\n"
            # 不调用 close/finalizer；os._exit 让 Windows 关闭父进程全部 HANDLE。
            "os._exit(0)\n",
            encoding="utf-8",
        )
        helper_process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(helper)],
            cwd=tmp_path,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        records = []
        try:
            assert helper_process.wait(20) == 0
            records = _wait_records([child_record])
            _assert_dead(records)
        finally:
            if helper_process.poll() is None:
                helper_process.kill()
                helper_process.wait(10)
            _cleanup_records(records)

    def test_stop_does_not_kill_unrelated_pid_identity(self, tmp_path):
        outsider_script = tmp_path / "outsider.py"
        outsider_ready = tmp_path / "outsider.ready"
        outsider_script.write_text(
            "import os,time\n"
            f"open({str(outsider_ready)!r},'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        outsider = subprocess.Popen(
            [sys.executable, "-I", "-S", str(outsider_script)],
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
                [sys.executable, "-I", "-S", "-c", "import time; time.sleep(120)"],
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

    @staticmethod
    def _tree_scripts(tmp_path: Path, *, detached: bool, generations: int):
        scripts = []
        flags = "0x00000008|0x00000200" if detached else "0"
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
            [sys.executable, "-I", "-S", str(scripts[0])], cwd=tmp_path, env={}
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


def _wait_records(paths: list[Path], timeout: float = 20) -> list[dict[str, int]]:
    deadline = time.monotonic() + timeout
    records = []
    for path in paths:
        while time.monotonic() < deadline:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                threading.Event().wait(0.01)
                continue
            if not isinstance(value.get("pid"), int):
                raise AssertionError(f"无效 PID 屏障：{path}: {value}")
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


def _assert_dead(records: list[dict[str, int]], *, require_birth: bool = True) -> None:
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
            result = kernel32.WaitForSingleObject(handle, 0)
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
