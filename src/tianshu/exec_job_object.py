"""Windows Job Object 进程树核心（显式 opt-in，不提供文件/网络/凭据隔离）。

Availability
-----------
``availability()`` 只在 Windows 且内核支持 ``PROCESS_THREAD_ATTRIBUTE_JOB_LIST``
原子绑定时返回 True。``create_managed_process()`` 是唯一选择入口；非 Windows、
旧内核或任一必要 API 不可用时抛 ``PolicyUnsupported``，绝不回落 ``Popen/local``。
Job Object 只治理进程树，不能据此声明四边界隔离已经成立。

Create / managed process
------------------------
创建走完整 ``CreateProcessW``：显式 application path、无 shell、无 PATH 搜索，
``CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT``。
``STARTUPINFOEXW`` 内嵌 ``STARTUPINFOW``，通过进程属性列表把不可继承 Job 与尚未
运行的线程原子绑定。Job 开启 ``KILL_ON_JOB_CLOSE``、不开启 breakaway。创建、
管道、属性列表、Job 配置、绑定或 ``ResumeThread`` 失败都会终止已建 Job 并关闭
所有资源；不调用未文档化的 ``NtResumeProcess``，也不实现容易逃逸的绑定后回退。

``ManagedProcess`` 提供 ``pid`` / ``returncode`` / ``poll`` / ``wait`` 和管道
``stdin`` / ``stdout`` / ``stderr``。两个输出流可直接交给 ``os.set_blocking`` 与
``os.read(fileno(), ...)``；调用方负责排空管道，避免子进程写满后阻塞。
``wait(timeout)`` 只等待 leader，并遵循 ``Popen`` 语义：超时抛
``subprocess.TimeoutExpired``，不自行终止 Job。``poll`` 只返回 leader 退出码。
调用方通过 ``controller.stop`` 或 ``close`` 清理并确认整树；leader 正常退出后
仍有后代时，必须继续查询 Job，不能把 leader 退出码当作整树退出证据。

Controller / result state
-------------------------
``JobController`` 负责 ``stop`` / ``query`` / ``close``。状态封闭为
``running`` / ``exited`` / ``unknown`` / ``closed``：只有
``QueryInformationJobObject(JobObjectBasicAccountingInformation).ActiveProcesses == 0``
才是 ``exited``；Terminate、Query、等待失败，或终止后有界查询仍非零，均为
``unknown``。``close`` 最后关闭 Job 句柄；只有关闭前已经证明整树为空才返回
``exited``，不能把“句柄已关”或“leader 已退”洗成整树退出。意外崩溃时 OS 关闭
父 Job 句柄，由 KILL_ON_JOB_CLOSE 终止树，但调用方无法再查询原 Job，故结果仍不得
事后伪报 ``exited``。

本切片只使用标准库。Windows 原生行为必须由真实 Windows 子进程测试证明；其它
平台上的结构/API 故障注入只是 fail-closed 单元证据，不是原生可用性证据。
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal

from .errors import TianshuError
from .exec import RULE_PTY_NOT_ALLOWED, ExecResult, _run_managed_pipes
from .execpolicy import (
    FEATURE_JOB_OBJECTS,
    FEATURE_PTY,
    ISOLATION_NONE,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    check_spawn,
    filtered_env,
)

# c_char_p 返回值会变成 bytes 或 None；HANDLE 是指针宽度的不透明整数。
_HANDLE = ctypes.c_void_p
_BOOL = ctypes.c_int32
_DWORD = ctypes.c_uint32
_UINT = ctypes.c_uint
_WORD = ctypes.c_uint16
_LPVOID = ctypes.c_void_p
_LPWSTR = ctypes.c_wchar_p
_LPCWSTR = ctypes.c_wchar_p
_LPBYTE = ctypes.POINTER(ctypes.c_ubyte)

_WINDOWS = os.name == "nt"
_BACKEND = "windows_job_object"

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_FLAGS = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_EXTENDED_STARTUPINFO_PRESENT
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# 显式清掉 BREAKAWAY_OK；不设 SILENT_BREAKAWAY_OK，后代必须留在 Job。
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
_TREE_JOIN_TIMEOUT_S = 5.0
_HANDLE_CLOSE_ATTEMPTS = 3
_HANDLE_CLOSE_RETRY_S = 0.01

TreeState = Literal["running", "exited", "unknown", "closed"]
TREE_RUNNING: TreeState = "running"
TREE_EXITED: TreeState = "exited"
TREE_UNKNOWN: TreeState = "unknown"
TREE_CLOSED: TreeState = "closed"


class JobObjectError(TianshuError):
    """Windows 原生进程核心失败；调用方不得把它当作“未执行”或重试依据。"""


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD),
        ("lpReserved", _LPWSTR),
        ("lpDesktop", _LPWSTR),
        ("lpTitle", _LPWSTR),
        ("dwX", _DWORD),
        ("dwY", _DWORD),
        ("dwXSize", _DWORD),
        ("dwYSize", _DWORD),
        ("dwXCountChars", _DWORD),
        ("dwYCountChars", _DWORD),
        ("dwFillAttribute", _DWORD),
        ("dwFlags", _DWORD),
        ("wShowWindow", _WORD),
        ("cbReserved2", _WORD),
        ("lpReserved2", _LPBYTE),
        ("hStdInput", _HANDLE),
        ("hStdOutput", _HANDLE),
        ("hStdError", _HANDLE),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _StartupInfoW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", _HANDLE),
        ("hThread", _HANDLE),
        ("dwProcessId", _DWORD),
        ("dwThreadId", _DWORD),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", _DWORD),
        ("lpSecurityDescriptor", _LPVOID),
        ("bInheritHandle", _BOOL),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


def _declare_prototypes(kernel32: ctypes.CDLL) -> None:
    """声明全部 Win32 原型；HANDLE 返回值和参数一律保留指针宽度与高位。"""
    size_ptr = ctypes.POINTER(ctypes.c_size_t)
    startup_ptr = ctypes.POINTER(_StartupInfoExW)
    process_info_ptr = ctypes.POINTER(_ProcessInformation)
    signatures = {
        "CreateJobObjectW": (
            [ctypes.POINTER(_SecurityAttributes), _LPCWSTR],
            _HANDLE,
        ),
        "SetInformationJobObject": (
            [_HANDLE, ctypes.c_int, _LPVOID, _DWORD],
            _BOOL,
        ),
        "QueryInformationJobObject": (
            [
                _HANDLE,
                ctypes.c_int,
                _LPVOID,
                _DWORD,
                ctypes.POINTER(_DWORD),
            ],
            _BOOL,
        ),
        "TerminateJobObject": ([_HANDLE, _UINT], _BOOL),
        "TerminateProcess": ([_HANDLE, _UINT], _BOOL),
        "CreatePipe": (
            [
                ctypes.POINTER(_HANDLE),
                ctypes.POINTER(_HANDLE),
                ctypes.POINTER(_SecurityAttributes),
                _DWORD,
            ],
            _BOOL,
        ),
        "SetHandleInformation": ([_HANDLE, _DWORD, _DWORD], _BOOL),
        "GetProcessHeap": ([], _HANDLE),
        "HeapAlloc": ([_HANDLE, _DWORD, ctypes.c_size_t], _LPVOID),
        "HeapFree": ([_HANDLE, _DWORD, _LPVOID], _BOOL),
        "InitializeProcThreadAttributeList": (
            [_LPVOID, _DWORD, _DWORD, size_ptr],
            _BOOL,
        ),
        "UpdateProcThreadAttribute": (
            [
                _LPVOID,
                _DWORD,
                ctypes.c_size_t,
                _LPVOID,
                ctypes.c_size_t,
                _LPVOID,
                size_ptr,
            ],
            _BOOL,
        ),
        "DeleteProcThreadAttributeList": ([_LPVOID], None),
        "CreateProcessW": (
            [
                _LPCWSTR,
                _LPWSTR,
                ctypes.POINTER(_SecurityAttributes),
                ctypes.POINTER(_SecurityAttributes),
                _BOOL,
                _DWORD,
                _LPVOID,
                _LPCWSTR,
                startup_ptr,
                process_info_ptr,
            ],
            _BOOL,
        ),
        "ResumeThread": ([_HANDLE], _DWORD),
        "IsProcessInJob": ([_HANDLE, _HANDLE, ctypes.POINTER(_BOOL)], _BOOL),
        "GetExitCodeProcess": ([_HANDLE, ctypes.POINTER(_DWORD)], _BOOL),
        "GetProcessId": ([_HANDLE], _DWORD),
        "GetProcessTimes": (
            [_HANDLE, _LPVOID, _LPVOID, _LPVOID, _LPVOID],
            _BOOL,
        ),
        "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
        "CloseHandle": ([_HANDLE], _BOOL),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(kernel32, name)
        function.argtypes = argtypes
        function.restype = restype


def _error(operation: str) -> JobObjectError:
    return JobObjectError(f"{operation} 失败（WinError {ctypes.get_last_error()}）")


class _NativeAPI:
    """带显式原型的 kernel32 薄封装；测试可注入同形 fault API。"""

    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _declare_prototypes(self.kernel32)

    def create_job(self) -> int:
        attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, False)
        return self.kernel32.CreateJobObjectW(ctypes.byref(attrs), None) or 0

    def set_job_limits(self, job: int, flags: int) -> bool:
        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JobBasicLimitInformation),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = flags
        return bool(
            self.kernel32.SetInformationJobObject(
                _HANDLE(job),
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )

    def query_job(self, job: int) -> int:
        info = _JobBasicAccountingInformation()
        returned = _DWORD()
        ok = self.kernel32.QueryInformationJobObject(
            _HANDLE(job),
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if not ok or returned.value != ctypes.sizeof(info):
            raise _error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def terminate_job(self, job: int) -> bool:
        return bool(self.kernel32.TerminateJobObject(_HANDLE(job), 1))

    def terminate_process(self, process: int, exit_code: int) -> bool:
        return bool(self.kernel32.TerminateProcess(_HANDLE(process), exit_code))

    def create_pipe(self) -> tuple[int, int]:
        read = _HANDLE()
        write = _HANDLE()
        if not self.kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), None, 0):
            raise _error("CreatePipe")
        return read.value or 0, write.value or 0

    def set_handle_information(self, handle: int, mask: int, flags: int) -> bool:
        return bool(self.kernel32.SetHandleInformation(_HANDLE(handle), mask, flags))

    def heap_allocate(self, size: int) -> int:
        heap = self.kernel32.GetProcessHeap()
        return self.kernel32.HeapAlloc(heap, 0, size) or 0

    def heap_free(self, memory: int) -> bool:
        return bool(self.kernel32.HeapFree(self.kernel32.GetProcessHeap(), 0, memory))

    def initialize_attributes(self, memory: int, count: int, size: int) -> tuple[bool, int]:
        required = ctypes.c_size_t(size)
        ok = self.kernel32.InitializeProcThreadAttributeList(
            memory, count, 0, ctypes.byref(required)
        )
        if not memory:
            # The documented sizing call fails with ERROR_INSUFFICIENT_BUFFER.
            ok = not ok and ctypes.get_last_error() == 122
        return bool(ok), int(required.value)

    def update_attributes(self, memory: int, attribute: int, value: int, size: int) -> bool:
        return bool(
            self.kernel32.UpdateProcThreadAttribute(
                memory,
                0,
                attribute,
                value,
                size,
                None,
                None,
            )
        )

    def delete_attributes(self, memory: int) -> None:
        self.kernel32.DeleteProcThreadAttributeList(memory)

    def create_process(
        self,
        application: str,
        command_line: ctypes.Array[ctypes.c_wchar],
        cwd: str,
        environment: ctypes.Array[ctypes.c_wchar],
        startup: _StartupInfoExW,
        inherited_handles: tuple[int, ...],
    ) -> tuple[int, int, int, int]:
        info = _ProcessInformation()
        if not self.kernel32.CreateProcessW(
            application,
            command_line,
            None,
            None,
            bool(inherited_handles),
            _CREATE_FLAGS,
            environment,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            raise _error("CreateProcessW")
        return (
            info.hProcess or 0,
            info.hThread or 0,
            int(info.dwProcessId),
            int(info.dwThreadId),
        )

    def resume_thread(self, thread: int) -> int:
        return int(self.kernel32.ResumeThread(_HANDLE(thread)))

    def is_process_in_job(self, process: int, job: int) -> bool:
        result = _BOOL()
        if not self.kernel32.IsProcessInJob(
            _HANDLE(process), _HANDLE(job), ctypes.byref(result)
        ):
            raise _error("IsProcessInJob")
        return bool(result.value)

    def exit_code(self, process: int) -> int | None:
        code = _DWORD()
        if not self.kernel32.GetExitCodeProcess(_HANDLE(process), ctypes.byref(code)):
            raise _error("GetExitCodeProcess")
        return int(code.value)

    def process_identity(self, process: int) -> tuple[int, int]:
        class _FileTime(ctypes.Structure):
            _fields_ = [("low", _DWORD), ("high", _DWORD)]

        pid = self.kernel32.GetProcessId(_HANDLE(process))
        if not pid:
            raise _error("GetProcessId")
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        if not self.kernel32.GetProcessTimes(
            _HANDLE(process),
            ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            raise _error("GetProcessTimes")
        return int(pid), (int(created.high) << 32) | int(created.low)

    def wait(self, process: int, milliseconds: int) -> int:
        return int(self.kernel32.WaitForSingleObject(_HANDLE(process), milliseconds))

    def close_handle(self, handle: int) -> bool:
        return bool(self.kernel32.CloseHandle(_HANDLE(handle)))


def _load_native_api() -> _NativeAPI:
    return _NativeAPI()


def _unsupported(reason: str) -> PolicyUnsupported:
    return PolicyUnsupported(reason, backend=_BACKEND, feature=FEATURE_JOB_OBJECTS)


@contextmanager
def _attribute_list(
    api, job: int, inherited_handles: tuple[int, ...] = ()
) -> Iterator[int]:
    count = 1 + bool(inherited_handles)
    ok, size = api.initialize_attributes(0, count, 0)
    if not ok or size < ctypes.sizeof(_HANDLE):
        raise JobObjectError("InitializeProcThreadAttributeList 无法分配属性列表")
    memory = api.heap_allocate(size)
    if not memory:
        raise JobObjectError("属性列表 HeapAlloc 失败")
    initialized = False
    try:
        ok, actual_size = api.initialize_attributes(memory, count, size)
        if not ok or actual_size != size:
            raise JobObjectError("InitializeProcThreadAttributeList 初始化失败")
        initialized = True
        job_value = _HANDLE(job)
        if not api.update_attributes(
            memory,
            PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.addressof(job_value),
            ctypes.sizeof(job_value),
        ):
            raise JobObjectError("PROC_THREAD_ATTRIBUTE_JOB_LIST 不受支持或更新失败")
        if inherited_handles:
            handle_values = (_HANDLE * len(inherited_handles))(
                *inherited_handles
            )
            if not api.update_attributes(
                memory,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.addressof(handle_values),
                ctypes.sizeof(handle_values),
            ):
                raise JobObjectError("PROC_THREAD_ATTRIBUTE_HANDLE_LIST 更新失败")
        yield memory
    finally:
        try:
            if initialized:
                api.delete_attributes(memory)
        finally:
            if not api.heap_free(memory):
                raise JobObjectError("属性列表 HeapFree 失败")


def _attribute_probe(api) -> bool:
    """用一个空 Job 实测原子属性支持，不创建进程、不靠版本号猜测。"""
    job = api.create_job()
    if not job:
        return False
    handles = _Handles(api)
    handles.add(job)
    supported = False
    try:
        if api.set_job_limits(job, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
            with _attribute_list(api, job):
                supported = True
    except (JobObjectError, OSError):
        supported = False
    finally:
        closed = handles.close_all()
    return closed and supported


def availability() -> bool:
    """本机是否可用原子 Job 绑定；非 Windows 直接 False，不尝试任何 fallback。"""
    if not _WINDOWS:
        return False
    try:
        return _attribute_probe(_load_native_api())
    except (JobObjectError, OSError, AttributeError):
        return False


class _Handles:
    def __init__(self, api) -> None:
        self.api = api
        self.values: set[int] = set()

    def add(self, handle: int) -> int:
        if not handle:
            raise JobObjectError("Win32 API 返回空 HANDLE")
        self.values.add(handle)
        return handle

    def discard(self, handle: int) -> None:
        self.values.discard(handle)

    def close(self, handle: int) -> None:
        if self.api.close_handle(handle):
            self.values.discard(handle)

    def close_all(self) -> bool:
        closed = True
        for handle in tuple(self.values):
            for attempt in range(_HANDLE_CLOSE_ATTEMPTS):
                try:
                    self.close(handle)
                except (JobObjectError, OSError):
                    pass
                if handle not in self.values:
                    break
                closed = False
                if attempt + 1 < _HANDLE_CLOSE_ATTEMPTS:
                    time.sleep(_HANDLE_CLOSE_RETRY_S)
        return closed


def _environment_block(env: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    entries: list[str] = []
    for key, value in env.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise ValueError("Job 环境变量名必须非空且不能含 '=' 或 NUL")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("Job 环境变量值必须是字符串且不能含 NUL")
        entries.append(f"{key}={value}")
    # Windows 要求 Unicode 环境块按不区分大小写的键排序。
    entries.sort(key=str.casefold)
    return ctypes.create_unicode_buffer("\x00".join(entries) + "\x00\x00")


def _wrap_pipe(
    handle: int, mode: str, *, transfer: Callable[[], None] | None = None
) -> BinaryIO:
    import msvcrt

    flags = os.O_BINARY | (os.O_RDONLY if mode == "rb" else os.O_WRONLY)
    fd = msvcrt.open_osfhandle(handle, flags)
    if transfer is not None:
        transfer()
    try:
        if mode == "rb":
            return os.fdopen(fd, "rb", buffering=0)
        if mode == "wb":
            return os.fdopen(fd, "wb", buffering=0)
        raise ValueError(f"未知管道模式：{mode!r}")
    except BaseException:
        os.close(fd)
        raise


def _adopt_stream(handles: _Handles, raw: int, mode: str) -> BinaryIO:
    return _wrap_pipe(
        raw, mode, transfer=lambda: handles.discard(raw)
    )


def _validate_creation(argv: Sequence[str], cwd: os.PathLike[str] | str) -> tuple[str, str]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("argv 必须是非空字符串数组")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise ValueError("argv 的每一项必须是非空、无 NUL 的字符串")
    application = os.path.normpath(argv[0])
    if not os.path.isabs(application):
        raise ValueError("argv[0] 必须是显式绝对 application path，不做 PATH 搜索")
    directory = Path(cwd)
    if not directory.is_absolute():
        raise ValueError("cwd 必须是绝对路径")
    return application, str(directory.resolve())


class JobController:
    """一个已原子绑定进程的 Job 控制器；只能由创建路径或内部测试构造。"""

    def __init__(
        self,
        api,
        job: int,
        process: int,
        thread: int,
        *,
        clock: object = time.monotonic,
        sleep: object = time.sleep,
    ) -> None:
        if not _WINDOWS:
            raise _unsupported("当前平台不能构造 Windows Job 控制器")
        self._api = api
        self._job = job
        self._process = process
        self._thread = thread
        self._clock = clock
        self._sleep = sleep
        self._closed = False
        self._close_failed = False
        self._open_handles = {job, process, thread}
        self._state: TreeState = TREE_RUNNING
        self._empty_observed = False

    @property
    def state(self) -> TreeState:
        """最近一次已确认状态；需要主动刷新时调用 query。"""
        return self._state

    def mark_unknown(self) -> None:
        if not self._closed:
            self._state = TREE_UNKNOWN
            self._empty_observed = False

    def query(self) -> TreeState:
        if self._closed:
            return TREE_CLOSED
        if self._close_failed or self._job not in self._open_handles:
            self._state = TREE_UNKNOWN
            return self._state
        try:
            active = self._api.query_job(self._job)
        except (JobObjectError, OSError):
            self.mark_unknown()
        else:
            self._empty_observed = active == 0
            self._state = TREE_EXITED if self._empty_observed else TREE_RUNNING
        return self._state

    def _query_until(self, timeout: float) -> TreeState:
        deadline = float(self._clock()) + max(0.0, timeout)
        while True:
            state = self.query()
            if state != TREE_RUNNING:
                return state
            remaining = deadline - float(self._clock())
            if remaining <= 0:
                return TREE_RUNNING
            self._sleep(min(0.01, remaining))

    def stop(self, timeout: float = _TREE_JOIN_TIMEOUT_S) -> TreeState:
        """终止 Job 并有界查询。失败/仍有进程/超时均返回 unknown。"""
        if timeout < 0:
            raise ValueError("timeout 不能为负")
        if self._closed:
            return TREE_CLOSED
        state = self.query()
        if state == TREE_EXITED:
            return TREE_EXITED
        if state == TREE_CLOSED or self._job not in self._open_handles:
            return TREE_UNKNOWN
        try:
            if not self._api.terminate_job(self._job):
                self._state = TREE_UNKNOWN
                return TREE_UNKNOWN
        except (JobObjectError, OSError):
            self._state = TREE_UNKNOWN
            return TREE_UNKNOWN
        state = self._query_until(timeout)
        return TREE_EXITED if state == TREE_EXITED else TREE_UNKNOWN

    def close(self, timeout: float = _TREE_JOIN_TIMEOUT_S) -> TreeState:
        """先尽力清树，再关闭 process/thread，最后关闭 Job 句柄。"""
        if timeout < 0:
            raise ValueError("timeout 不能为负")
        if self._closed:
            return TREE_CLOSED
        state = self._state
        if self._close_failed:
            if self._job in self._open_handles:
                # Keep query() fail-closed until the retry, while the original Job
                # handle still lets us observe and stop the same tree.
                self._close_failed = False
                try:
                    state = self.stop(timeout)
                finally:
                    self._close_failed = True
            elif self._empty_observed:
                state = TREE_EXITED
        elif state in (TREE_RUNNING, TREE_UNKNOWN) and self._job in self._open_handles:
            state = self.stop(timeout)
        elif state == TREE_EXITED:
            pass
        elif state != TREE_UNKNOWN:
            state = self.query()
        close_failed = False
        for handle in (self._process, self._thread, self._job):
            if handle not in self._open_handles:
                continue
            for attempt in range(_HANDLE_CLOSE_ATTEMPTS):
                try:
                    closed = self._api.close_handle(handle)
                except (JobObjectError, OSError):
                    closed = False
                if closed:
                    self._open_handles.discard(handle)
                    break
                close_failed = True
                if attempt + 1 < _HANDLE_CLOSE_ATTEMPTS:
                    self._sleep(_HANDLE_CLOSE_RETRY_S)
            if handle in self._open_handles:
                state = TREE_UNKNOWN
        if self._open_handles:
            self._close_failed = True
            self._state = TREE_UNKNOWN
            return TREE_UNKNOWN
        self._closed = True
        self._state = (
            TREE_EXITED if state == TREE_EXITED and not close_failed else TREE_UNKNOWN
        )
        return self._state


class ManagedProcess:
    """受 Job 管理的进程与三条管道；不是 Popen，也没有 local fallback。"""

    def __init__(
        self,
        controller: JobController,
        pid: int,
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: BinaryIO,
        command: Sequence[str] = (),
    ) -> None:
        if not _WINDOWS:
            raise _unsupported("当前平台不能构造 Windows 受管进程")
        self.controller = controller
        self.pid = pid
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._command = tuple(command)
        self._returncode: int | None = None
        self._closed = False
        self._close_state: TreeState | None = None

    def birth_identity(self) -> str:
        pid, birth = self.controller._api.process_identity(self.controller._process)
        if pid != self.pid or birth <= 0:
            raise JobObjectError("Windows Job leader HANDLE 身份与启动记录不符")
        return f"windows-filetime:{birth}"

    @property
    def returncode(self) -> int | None:
        """已确认的 leader 退出码；非 None 本身不代表整树已经退出。"""
        return self._returncode

    def _leader_code(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        code = self.controller._api.exit_code(self.controller._process)
        if code == STILL_ACTIVE:
            result = self.controller._api.wait(self.controller._process, 0)
            if result == WAIT_OBJECT_0:
                # The leader can exit after the first GetExitCodeProcess.
                self._returncode = self.controller._api.exit_code(self.controller._process)
                return self._returncode
            if result == WAIT_TIMEOUT:
                return None
            raise JobObjectError(f"WaitForSingleObject 返回未知状态 {result:#x}")
        self._returncode = code
        return code

    def poll(self) -> int | None:
        """不阻塞地返回 leader 退出码；不代替整树确认。"""
        if self._closed:
            return None
        return self._leader_code()

    def wait(self, timeout: float | None = None) -> int:
        """只等待 leader；超时抛 TimeoutExpired，清树由 controller 负责。"""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout 不能为负")
        if self._closed:
            raise JobObjectError("受管进程已经关闭，不能等待 leader")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self._leader_code()
            if code is not None:
                return code
            if deadline is None:
                milliseconds = INFINITE
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self._command, timeout)
                milliseconds = max(1, min(100, int(remaining * 1000)))
            result = self.controller._api.wait(self.controller._process, milliseconds)
            if result == WAIT_TIMEOUT:
                continue
            if result != WAIT_OBJECT_0:
                raise JobObjectError(f"WaitForSingleObject 返回未知状态 {result:#x}")
            code = self._leader_code()
            if code is None:
                raise JobObjectError("leader 已 signal，但退出码仍为 STILL_ACTIVE")
            return code

    def close(self, timeout: float = _TREE_JOIN_TIMEOUT_S) -> TreeState:
        """确认或清树后关闭 Job 与全部管道；重复调用返回同一最终状态。"""
        if self._closed:
            assert self._close_state is not None
            return self._close_state
        state = self.controller.close(timeout)
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                state = TREE_UNKNOWN
        if not self.controller._closed:
            return TREE_UNKNOWN
        self._closed = True
        self._close_state = TREE_EXITED if state == TREE_EXITED else TREE_UNKNOWN
        return self._close_state

    def __enter__(self) -> ManagedProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def tree_state(self) -> TreeState:
        if self._closed:
            assert self._close_state is not None
            return self._close_state
        return self.controller.state


def _create_with_api(
    api, argv: Sequence[str], cwd: os.PathLike[str] | str, env: Mapping[str, str]
) -> ManagedProcess:
    application, directory = _validate_creation(argv, cwd)
    environment = _environment_block(env)
    command_line = subprocess.list2cmdline([application, *argv[1:]])
    handles = _Handles(api)
    streams: list[BinaryIO] = []
    process = thread = 0
    try:
        job = handles.add(api.create_job())
        if not api.set_job_limits(job, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
            raise JobObjectError("SetInformationJobObject(KILL_ON_JOB_CLOSE) 失败")

        stdin_read, stdin_write = (handles.add(handle) for handle in api.create_pipe())
        stdout_read, stdout_write = (handles.add(handle) for handle in api.create_pipe())
        stderr_read, stderr_write = (handles.add(handle) for handle in api.create_pipe())
        child_ends = (stdin_read, stdout_write, stderr_write)
        parent_ends = (stdin_write, stdout_read, stderr_read)
        for handle in child_ends:
            if not api.set_handle_information(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
                raise JobObjectError("子进程 stdio HANDLE 不可继承设置失败")
        for handle in parent_ends:
            if not api.set_handle_information(handle, HANDLE_FLAG_INHERIT, 0):
                raise JobObjectError("父进程 stdio HANDLE 继承清理失败")

        startup = _StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoExW)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = _HANDLE(stdin_read)
        startup.StartupInfo.hStdOutput = _HANDLE(stdout_write)
        startup.StartupInfo.hStdError = _HANDLE(stderr_write)
        command_buffer = ctypes.create_unicode_buffer(command_line)
        with _attribute_list(api, job, child_ends) as attributes:
            startup.lpAttributeList = attributes
            process, thread, pid, _thread_id = api.create_process(
                application, command_buffer, directory, environment, startup, child_ends
            )
            handles.add(process)
            handles.add(thread)
            if not api.is_process_in_job(process, job):
                raise JobObjectError("CreateProcessW 后未在目标 Job 中复核到暂停 leader")

        # 父进程只保留管道另一端；关闭失败也不能让父端写句柄一直阻止 EOF。
        for handle in child_ends:
            handles.close(handle)
            if handle in handles.values:
                raise JobObjectError("关闭子进程 stdio HANDLE 失败")
        stdin = _adopt_stream(handles, stdin_write, "wb")
        streams.append(stdin)
        stdout = _adopt_stream(handles, stdout_read, "rb")
        streams.append(stdout)
        stderr = _adopt_stream(handles, stderr_read, "rb")
        streams.append(stderr)

        if api.resume_thread(thread) == 0xFFFFFFFF:
            raise JobObjectError("ResumeThread 失败")
        return ManagedProcess(
            JobController(api, job, process, thread),
            pid,
            stdin,
            stdout,
            stderr,
            tuple(argv),
        )
    except BaseException as exc:
        # 已绑定但未恢复的 leader 也在 Job 内；Terminate + 原句柄等待，不按 PID 猜。
        leader_exited = tree_exited = not process
        if process and process in handles.values:
            try:
                api.terminate_job(job)
            except (JobObjectError, OSError):
                pass
            try:
                api.terminate_process(process, 1)
            except (JobObjectError, OSError):
                pass
            try:
                leader_exited = api.wait(process, int(_TREE_JOIN_TIMEOUT_S * 1000)) == WAIT_OBJECT_0
            except (JobObjectError, OSError):
                pass
            try:
                deadline = time.monotonic() + _TREE_JOIN_TIMEOUT_S
                while True:
                    if api.query_job(job) == 0:
                        tree_exited = True
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
            except (JobObjectError, OSError):
                pass
        closed = handles.close_all()
        for stream in streams:
            try:
                stream.close()
            except OSError:
                closed = False
        if isinstance(exc, Exception) and not (leader_exited and tree_exited and closed):
            raise JobObjectError("Windows Job 启动失败，进程树或句柄清理未确认") from exc
        raise


def create_managed_process(
    argv: Sequence[str],
    *,
    cwd: os.PathLike[str] | str,
    env: Mapping[str, str],
) -> ManagedProcess:
    """显式选择 Windows Job 核心并创建受管进程；不可用时 fail-closed。"""
    if not _WINDOWS:
        raise _unsupported("Windows Job Object 仅原生可用；不会回落 local/Popen")
    try:
        api = _load_native_api()
    except (OSError, AttributeError) as exc:
        raise _unsupported(f"kernel32 必要 API 不可用：{exc}") from exc
    if not _attribute_probe(api):
        raise _unsupported("Job 或 PROC_THREAD_ATTRIBUTE_JOB_LIST 原子绑定不可用")
    return _create_with_api(api, argv, cwd, env)


def native_profile() -> dict[str, object]:
    return {
        "backend": _BACKEND,
        "os": "windows",
        "architecture": platform.machine().lower(),
        "create_flags": _CREATE_FLAGS,
        "job_limit_flags": JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        "breakaway": False,
        "atomic_binding": "PROC_THREAD_ATTRIBUTE_JOB_LIST",
        "inherited_handles": "PROC_THREAD_ATTRIBUTE_HANDLE_LIST",
        "inherited_handle_count": 3,
    }


class WindowsJobObjectExecBackend:
    capabilities = ExecBackendCapabilities(
        name=_BACKEND,
        isolation=ISOLATION_NONE,
        supports_job_objects=True,
    )

    def spawn(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | os.PathLike[str],
        env: dict[str, str],
        policy: ExecPolicy,
        blob_dir: Path | None = None,
        pty: bool = False,
    ) -> ExecResult:
        if not isinstance(policy, ExecPolicy):
            raise TypeError("spawn 需要 ExecPolicy 实例")
        child_env = filtered_env(policy, env)
        check_spawn(policy, argv, cwd, child_env)
        if blob_dir is not None:
            from .restore_access import ensure_path_not_held

            ensure_path_not_held(blob_dir)
        if pty:
            if not policy.allow_pty:
                raise PolicyDenied(
                    "本次调用请求了 PTY，但策略没有允许",
                    rule_id=RULE_PTY_NOT_ALLOWED,
                )
            self.capabilities.require(FEATURE_PTY)
            raise PolicyUnsupported(
                "Windows Job Object 不提供 PTY", backend=_BACKEND, feature="pty"
            )
        proc = create_managed_process(argv, cwd=cwd, env=child_env)
        try:
            result, _state = _run_managed_pipes(
                proc,
                policy=policy,
                blob_dir=blob_dir,
                finalize_tree=lambda _timed_out: proc.controller.stop(),
                close_stdin=True,
            )
        finally:
            close_state = proc.close()
        if close_state != TREE_EXITED:
            result = replace(result, descendants_may_survive=True)
        return result


__all__ = [
    "TREE_CLOSED",
    "TREE_EXITED",
    "TREE_RUNNING",
    "TREE_UNKNOWN",
    "JobController",
    "JobObjectError",
    "ManagedProcess",
    "TreeState",
    "WindowsJobObjectExecBackend",
    "availability",
    "create_managed_process",
    "native_profile",
]
