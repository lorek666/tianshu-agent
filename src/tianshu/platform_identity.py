"""从真实已打开的原生句柄读取文件系统实体身份。

路径字符串只用于定位。锁和恢复判断使用 POSIX ``device+inode`` 或 Windows
``volume serial+128-bit file ID``；两者都由打开后的句柄查询，不能从路径 hash
或可能截断的 ``stat`` 值冒充。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .errors import TianshuError
from .restore_contract import EntityIdentityV1, PosixEntityIdentityV1, WindowsEntityIdentityV1

EntityKind = Literal["file", "directory"]


class NativeEntityError(TianshuError):
    """真实实体身份无法可靠取得，调用方必须闭锁。"""


def _reject_reparse(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_junction():
            raise NativeEntityError(f"实体不能是符号链接或junction：{path}")
    except OSError as exc:
        raise NativeEntityError(f"实体链接状态不可判定：{path}（{exc}）") from exc


@dataclass
class NativeEntityHandle:
    path: Path
    kind: EntityKind
    identity: EntityIdentityV1
    link_count: int
    _fd: int | None = field(default=None, repr=False)
    _windows_handle: int | None = field(default=None, repr=False)

    def verify_path(self, path: Path | None = None) -> None:
        candidate = self.path if path is None else Path(path)
        current_identity = observe_entity(candidate, self.kind)
        if current_identity != self.identity:
            raise NativeEntityError(
                f"路径当前映射的实体已经不匹配已持有句柄：{candidate}"
            )

    def close(self) -> None:
        if os.name == "nt":
            if self._windows_handle is None:
                return
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            if not kernel32.CloseHandle(self._windows_handle):
                raise NativeEntityError(
                    f"CloseHandle失败：{self.path}"
                    f"（{ctypes.WinError(ctypes.get_last_error())}）"
                )
            self._windows_handle = None
            return
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError as exc:
            raise NativeEntityError(f"关闭原生实体句柄失败：{self.path}（{exc}）") from exc
        self._fd = None


def _open_posix(path: Path, kind: EntityKind) -> NativeEntityHandle:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeEntityError(f"无法从真实句柄打开{kind}：{path}（{exc}）") from exc
    try:
        info = os.fstat(descriptor)
        expected = stat.S_ISREG(info.st_mode) if kind == "file" else stat.S_ISDIR(info.st_mode)
        if not expected or stat.S_ISLNK(info.st_mode):
            raise NativeEntityError(f"句柄类型不是普通{kind}：{path}")
        if info.st_ino == 0:
            raise NativeEntityError(f"POSIX句柄返回了零inode，不能作为实体身份：{path}")
        return NativeEntityHandle(
            path,
            kind,
            PosixEntityIdentityV1("posix", info.st_dev, info.st_ino),
            info.st_nlink,
            _fd=descriptor,
        )
    except OSError as exc:
        raise NativeEntityError(f"无法读取{kind}句柄身份：{path}（{exc}）") from exc
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows(path: Path, kind: EntityKind) -> NativeEntityHandle:  # pragma: no cover
    import ctypes
    from ctypes import wintypes

    class FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", FileId128),
        ]

    class FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FileTime),
            ("ftLastAccessTime", FileTime),
            ("ftLastWriteTime", FileTime),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_info.restype = wintypes.BOOL
    get_info_ex = kernel32.GetFileInformationByHandleEx
    get_info_ex.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_info_ex.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    share_read_write_delete = 0x00000007
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_id_info_class = 18
    invalid_handle = ctypes.c_void_p(-1).value

    flags = file_flag_open_reparse_point
    if kind == "directory":
        flags |= file_flag_backup_semantics
    handle = create_file(
        str(path),
        file_read_attributes,
        share_read_write_delete,
        None,
        open_existing,
        flags,
        None,
    )
    if handle in (None, 0, invalid_handle):
        raise NativeEntityError(
            f"CreateFileW无法打开{kind}：{path}（{ctypes.WinError(ctypes.get_last_error())}）"
        )
    try:
        basic = ByHandleFileInformation()
        if not get_info(handle, ctypes.byref(basic)):
            raise NativeEntityError(
                f"GetFileInformationByHandle失败：{path}（{ctypes.WinError(ctypes.get_last_error())}）"
            )
        attributes = basic.dwFileAttributes
        if attributes & file_attribute_reparse_point:
            raise NativeEntityError(f"实体是Windows reparse point：{path}")
        is_directory = bool(attributes & file_attribute_directory)
        if is_directory != (kind == "directory"):
            raise NativeEntityError(f"实体类型不是普通{kind}：{path}")

        detailed = FileIdInfo()
        if not get_info_ex(
            handle,
            file_id_info_class,
            ctypes.byref(detailed),
            ctypes.sizeof(detailed),
        ):
            raise NativeEntityError(
                f"GetFileInformationByHandleEx(FileIdInfo)失败："
                f"{path}（{ctypes.WinError(ctypes.get_last_error())}）"
            )
        identifier = bytes(detailed.FileId.Identifier)
        if not any(identifier):
            raise NativeEntityError(f"Windows句柄返回全零file ID，不能作为实体身份：{path}")
        return NativeEntityHandle(
            path,
            kind,
            WindowsEntityIdentityV1(
                "windows",
                int(detailed.VolumeSerialNumber),
                identifier.hex(),
            ),
            int(basic.nNumberOfLinks),
            _windows_handle=int(handle),
        )
    except BaseException:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)
        raise


def open_entity(path: Path, kind: EntityKind) -> NativeEntityHandle:
    """打开并持有一个真实原生实体句柄。"""

    if kind not in ("file", "directory"):
        raise ValueError(f"未知实体类型：{kind!r}")
    supplied = Path(path)
    _reject_reparse(supplied)
    try:
        canonical = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NativeEntityError(f"实体路径不可解析：{supplied}（{exc}）") from exc
    _reject_reparse(canonical)
    if os.name == "nt":
        return _open_windows(canonical, kind)
    return _open_posix(canonical, kind)


def observe_entity(path: Path, kind: EntityKind) -> EntityIdentityV1:
    """观察真实实体取得身份。
    
    在 POSIX 上使用 os.stat 观察，绝不能执行 open() + close()：
    根据 POSIX advisory locking 标准，进程关闭任何指向某文件的文件描述符时，
    操作系统内核会静默丢弃该进程在该文件上持有的所有 POSIX 锁（包括 SQLite WAL 锁）。
    若对正在使用的数据库文件调用 open+close，会导致 SQLite 锁保护瞬间失效并损坏数据库。
    """
    if kind not in ("file", "directory"):
        raise ValueError(f"未知实体类型：{kind!r}")
    if os.name == "nt":
        handle = open_entity(path, kind)
        try:
            return handle.identity
        finally:
            handle.close()

    supplied = Path(path)
    _reject_reparse(supplied)
    try:
        canonical = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NativeEntityError(f"实体路径不可解析：{supplied}（{exc}）") from exc
    _reject_reparse(canonical)
    try:
        info = os.stat(canonical)
        expected = stat.S_ISREG(info.st_mode) if kind == "file" else stat.S_ISDIR(info.st_mode)
        if not expected or stat.S_ISLNK(info.st_mode):
            raise NativeEntityError(f"路径类型不是普通{kind}：{path}")
        if info.st_ino == 0:
            raise NativeEntityError(f"POSIX返回了零inode，不能作为实体身份：{path}")
        return PosixEntityIdentityV1("posix", info.st_dev, info.st_ino)
    except OSError as exc:
        raise NativeEntityError(f"无法读取{kind}身份：{path}（{exc}）") from exc



__all__ = [
    "EntityKind",
    "NativeEntityError",
    "NativeEntityHandle",
    "observe_entity",
    "open_entity",
]
