"""数据库维护准入：普通连接共享，离线维护独占。

锁是协作式准入，不是进程沙箱；旧版或直接 SQLite 写入不受它约束。
锁文件放在数据根之外，替换数据根时不能顺手替换仍被持有的锁 inode。
POSIX 固定使用 /tmp/tianshu-maintenance-<uid>；Windows 由 OS 已知用户目录
取得 LocalAppData/Temp/tianshu-maintenance，不读取 HOME/TEMP 等可变环境。
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path

from .errors import TianshuError
from .pathkeys import overlap_key


class MaintenanceError(TianshuError):
    """无法可靠取得或释放维护准入。"""


class MaintenanceBusy(MaintenanceError):
    """活跃连接或另一维护操作正在持有不兼容的准入。"""


class DatabaseAccess:
    """一个独立的 OS 锁句柄；关闭幂等，不删除共享的锁文件。"""

    def __init__(
        self,
        file: io.FileIO | None = None,
        unlock: Callable[[], None] | None = None,
    ) -> None:
        self._file = file
        self._unlock = unlock

    def close(self) -> None:
        if self._file is None:
            return
        try:
            if self._unlock is not None:
                self._unlock()
                self._unlock = None
            # POSIX 只关自己的 fd，不显式 LOCK_UN：fork 后子进程关闭继承的
            # fd，不能撤掉仍由父进程持有的同一 open-file-description 锁。
            self._file.close()
        except OSError as exc:
            raise MaintenanceError(f"释放数据库维护准入失败：{exc}") from exc
        self._file = None

    def __enter__(self) -> DatabaseAccess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class MaintenanceConnection(sqlite3.Connection):
    """将准入绑定到真实连接，包括被 EventLog 外部持有的连接/游标。"""

    database_access: DatabaseAccess | None = None

    def close(self) -> None:
        # 跨线程 close 可能抛 ProgrammingError 而连接仍然可写；先关闭数据库，
        # 成功后才释放准入。保留原异常，调用者可以回原线程重试。
        super().close()
        if self.database_access is not None:
            self.database_access.close()


def _lock_root() -> Path:
    """OS 用户级固定锁根；不能按数据库权限或进程环境切换锁身份。"""
    if os.name != "nt":
        return Path("/tmp") / f"tianshu-maintenance-{os.getuid()}"
    return _windows_lock_root()


def _windows_lock_root() -> Path:
    """仅调用原生已知目录 API；不从环境变量补出另一套锁根。"""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    get_folder = shell32.SHGetFolderPathW
    get_folder.argtypes = [
        wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ]
    get_folder.restype = wintypes.LONG  # HRESULT，不能当成 BOOL/GetLastError 读取
    folder = ctypes.create_unicode_buffer(260)  # 官方调用约定：MAX_PATH
    result = get_folder(None, 0x001C, None, 0, folder)  # CSIDL_LOCAL_APPDATA
    if result != 0 or not folder.value:
        raise OSError(f"SHGetFolderPathW(LocalAppData) 失败：HRESULT=0x{result & 0xffffffff:08x}")
    return Path(folder.value) / "Temp" / "tianshu-maintenance"


def _prepare_lock_directory(root: Path) -> None:
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "nt" and root.is_junction()):
            raise MaintenanceError(f"维护锁目录不能是链接或非目录：{root}")
        if os.name != "nt":
            # /tmp 可被别的用户预建名字；先核所有者，再调整自己的目录权限。
            if info.st_uid != os.getuid():
                raise MaintenanceError(f"维护锁目录不属于当前用户：{root}")
            root.chmod(0o700)
        # Windows 继承 OS 用户目录 ACL，不用恒为 0 的 st_uid 冒充 owner SID
        # 核验；这里不宣称跨用户或管理员隔离，只拒绝最终目录重定向。
    except OSError as exc:
        raise MaintenanceError(f"无法准备数据库维护锁目录：{root}（{exc}）") from exc


def database_lock_path(db_path: Path) -> Path | None:
    """返回 canonical 数据库身份对应的外置锁路径；内存库不落盘。"""
    if str(db_path) == ":memory:":
        return None
    try:
        canonical = db_path.resolve()
        identity = overlap_key(str(canonical))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return _lock_root() / f"{digest}.lock"
    except (OSError, RuntimeError) as exc:
        raise MaintenanceError(f"无法确定数据库维护身份：{db_path}（{exc}）") from exc


def _lock_windows(file: io.FileIO, *, exclusive: bool) -> Callable[[], None]:
    # LockFileEx 不带 EXCLUSIVE flag 才是真共享；msvcrt.locking 仅有独占锁，
    # 不能用于多个普通连接同时运行的准入契约。偏移/长度均为第一个字节。
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock = kernel32.LockFileEx
    lock.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock.restype = wintypes.BOOL
    unlock = kernel32.UnlockFileEx
    unlock.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    unlock.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(file.fileno())
    overlapped = Overlapped()
    flags = 0x00000001 | (0x00000002 if exclusive else 0)
    if not lock(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())

    def release() -> None:
        # Windows 在 close/crash 后也释放，但官方注明释放可能延后；正常路径
        # 显式解锁，避免紧接着开始的维护被尚未释放的句柄误拒绝。
        if not unlock(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())

    return release


def acquire_database_access(
    db_path: Path, *, exclusive: bool = False,
) -> DatabaseAccess:
    """立即取得共享/独占准入；只把实际锁争用映射为 MaintenanceBusy。"""
    lock_path = database_lock_path(db_path)
    if lock_path is None:
        return DatabaseAccess()
    _prepare_lock_directory(lock_path.parent)
    file = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        file = io.FileIO(fd, mode="r+", closefd=True)
        if os.name != "nt":
            os.fchmod(file.fileno(), 0o600)
    except OSError as exc:
        if file is not None:
            file.close()
        raise MaintenanceError(f"无法打开数据库维护锁：{lock_path}（{exc}）") from exc
    try:
        unlock = None
        if os.name == "nt":
            unlock = _lock_windows(file, exclusive=exclusive)
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(file.fileno(), mode | fcntl.LOCK_NB)
    except OSError as exc:
        file.close()
        busy = (
            getattr(exc, "winerror", None) == 33
            if os.name == "nt"
            else exc.errno in (errno.EACCES, errno.EAGAIN)
        )
        if busy:
            raise MaintenanceBusy(
                f"数据库正在使用中，暂不能进入{'独占维护' if exclusive else '普通访问'}："
                f"{db_path}。请先关闭其他天枢进程或等待维护完成。"
            ) from exc
        raise MaintenanceError(f"取得数据库维护准入失败：{lock_path}（{exc}）") from exc
    except BaseException:
        file.close()
        raise
    return DatabaseAccess(file, unlock)
