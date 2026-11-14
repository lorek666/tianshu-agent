"""macOS Keychain 直调后端（F12 W1，仅 darwin 加载）。

探针证据（2026-11-14，darwin 25 / macOS arm64，见
[SPEC-F12-KEYCHAIN-2026-11-14](../../docs/SPEC-F12-KEYCHAIN-2026-11-14.md) §2）：

1. **默认（login）keychain 的 `SecItemAdd` / `SecItemCopyMatching` /
   `SecItemUpdate` / `SecItemDelete` 全链路可用**——这是本模块唯一的通路；
2. `SecKeychainCreate` 一律返回 `-25294`：文件 keychain 的进程内创建已不可用
   （`.keychain` / `.keychain-db` / 无扩展名三种子路径都试过）；
3. `SecItem*` 挂 `kSecUseKeychain=<文件引用>` 时**该键被忽略**，条目实际落进
   login keychain（`security dump-keychain <文件>` 0 命中、
   `security find-generic-password` 命中 login）——文件 keychain 的 SecItem
   路由在这版 macOS 上已失效，本模块因此**只支持默认 keychain**。

## 防交互卡死（daemon 的硬要求）

所有查询统一挂 `kSecUseAuthenticationUI = kSecUseAuthenticationUIFail`：
daemon 是无头进程，钥匙串锁定时（`-25308` errSecInteractionNotAllowed）
必须**立即失败**成 `CredentialUnsupported`，绝不弹 GUI 密码框挂住主循环。
常量缺失就拒绝启动（fail closed），不冒险做一次可能弹框的访问。

## OSStatus 映射（与 credentials.py 的三态对齐）

- `-25300`（item not found）不是错误：`get_item()` 返回 `None`（未配置），
  `delete_item()` 按幂等成功处理——终态「不存在」已达成；
- `-25299`（duplicate）在 `set_item()` 内部转 `SecItemUpdate`，不外抛；
- 其余 OSStatus（含 `-25308`）→ `CredentialUnsupported`（带数值与已知名称）。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading

from .errors import CredentialUnsupported

__all__ = ["delete_item", "get_item", "set_item"]

#: errSecSuccess 与几个要给出人话的常见码；其余按数值报告，不编造名称。
_STATUS_NAMES = {
    0: "errSecSuccess",
    -25293: "errSecAuthFailed",
    -25294: "errSecNoSuchKeychain",
    -25299: "errSecDuplicateItem",
    -25300: "errSecItemNotFound",
    -25308: "errSecInteractionNotAllowed（钥匙串已锁定/需要交互解锁）",
}

_ERR_DUPLICATE = -25299
_ERR_ITEM_NOT_FOUND = -25300

#: CFDataGetBytes 的 range 参数是结构体（location, length）——传成单个 long
#: 会撞 CoreFoundation 的断言直接崩进程（探针实测踩过）。
class _CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


class _Bindings:
    """框架绑定：函数签名与常量全部 `in_dll` 读取，不手抄数值。"""

    def __init__(self) -> None:
        cf_path = ctypes.util.find_library("CoreFoundation")
        sec_path = ctypes.util.find_library("Security")
        if not cf_path or not sec_path:
            raise CredentialUnsupported(
                "macOS 系统库 CoreFoundation/Security 不可定位，Keychain 后端不可用"
            )
        self.cf = ctypes.CDLL(cf_path)
        self.sec = ctypes.CDLL(sec_path)
        osstatus = ctypes.c_int32

        def fn(lib: ctypes.CDLL, name: str, restype: object, argtypes: list) -> None:
            f = getattr(lib, name)
            f.restype = restype
            f.argtypes = argtypes

        fn(self.cf, "CFStringCreateWithCString", ctypes.c_void_p,
           [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32])
        fn(self.cf, "CFDataCreate", ctypes.c_void_p,
           [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long])
        fn(self.cf, "CFDataGetLength", ctypes.c_long, [ctypes.c_void_p])
        fn(self.cf, "CFDataGetBytes", None,
           [ctypes.c_void_p, _CFRange, ctypes.c_void_p])
        fn(self.cf, "CFDictionaryCreateMutable", ctypes.c_void_p,
           [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p])
        fn(self.cf, "CFDictionarySetValue", None,
           [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p])
        fn(self.cf, "CFRelease", None, [ctypes.c_void_p])
        fn(self.sec, "SecItemAdd", osstatus,
           [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
        fn(self.sec, "SecItemCopyMatching", osstatus,
           [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
        fn(self.sec, "SecItemUpdate", osstatus, [ctypes.c_void_p, ctypes.c_void_p])
        fn(self.sec, "SecItemDelete", osstatus, [ctypes.c_void_p])

        self.kCFAllocatorDefault = ctypes.c_void_p.in_dll(self.cf, "kCFAllocatorDefault")
        self.kCFBooleanTrue = ctypes.c_void_p.in_dll(self.cf, "kCFBooleanTrue")
        self.kCFStringEncodingUTF8 = ctypes.c_uint32(0x08000100)
        for name in (
            "kSecClass", "kSecAttrService", "kSecAttrAccount", "kSecValueData",
            "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
            "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail",
        ):
            setattr(self, name, ctypes.c_void_p.in_dll(self.sec, name))
        self.kSecClassGenericPassword = ctypes.c_void_p.in_dll(
            self.sec, "kSecClassGenericPassword"
        )

    # -- CF 构造 ------------------------------------------------------------

    def cfstr(self, pool: list[ctypes.c_void_p], text: str) -> ctypes.c_void_p:
        ref = self.cf.CFStringCreateWithCString(
            self.kCFAllocatorDefault, text.encode("utf-8"), self.kCFStringEncodingUTF8
        )
        pool.append(ref)
        return ref

    def cfdata(self, pool: list[ctypes.c_void_p], raw: bytes) -> ctypes.c_void_p:
        ref = self.cf.CFDataCreate(self.kCFAllocatorDefault, raw, len(raw))
        pool.append(ref)
        return ref

    def cfdict(
        self, pool: list[ctypes.c_void_p], pairs: list[tuple[ctypes.c_void_p, ctypes.c_void_p]]
    ) -> ctypes.c_void_p:
        d = self.cf.CFDictionaryCreateMutable(
            self.kCFAllocatorDefault, 0, None, None
        )
        pool.append(d)
        for key, value in pairs:
            self.cf.CFDictionarySetValue(d, key, value)
        return d

    def read_data(self, ref: ctypes.c_void_p) -> bytes:
        length = self.cf.CFDataGetLength(ref)
        buffer = (ctypes.c_ubyte * length)()
        self.cf.CFDataGetBytes(
            ref, _CFRange(0, length), ctypes.cast(buffer, ctypes.c_void_p)
        )
        return bytes(buffer)

    def release_all(self, pool: list[ctypes.c_void_p]) -> None:
        for ref in reversed(pool):
            if ref:
                self.cf.CFRelease(ref)

    def base_query(
        self, pool: list[ctypes.c_void_p], service: str, account: str
    ) -> ctypes.c_void_p:
        return self.cfdict(pool, [
            (self.kSecClass, self.kSecClassGenericPassword),
            (self.kSecAttrService, self.cfstr(pool, service)),
            (self.kSecAttrAccount, self.cfstr(pool, account)),
            # 无头进程防交互：锁定立即失败，绝不弹 GUI 密码框（见模块 docstring）。
            (self.kSecUseAuthenticationUI, self.kSecUseAuthenticationUIFail),
        ])


_B: _Bindings | None = None
_B_LOCK = threading.Lock()


def _bindings() -> _Bindings:
    global _B
    if _B is None:
        with _B_LOCK:
            if _B is None:
                _B = _Bindings()
    return _B


def _unsupported(operation: str, ref: str, status: int) -> CredentialUnsupported:
    name = _STATUS_NAMES.get(status, "未知错误码")
    return CredentialUnsupported(
        f"Keychain {operation}失败（{ref}）：OSStatus {status}（{name}）"
    )


def get_item(service: str, account: str) -> bytes | None:
    """读一个泛型密码条目；不存在返回 `None`（未配置，不是错误）。"""
    b = _bindings()
    pool: list[ctypes.c_void_p] = []
    try:
        query = b.base_query(pool, service, account)
        # 查询要求返回数据：复制进基础查询后再补，避免把数据键混进 base_query。
        b.cf.CFDictionarySetValue(query, b.kSecReturnData, b.kCFBooleanTrue)
        b.cf.CFDictionarySetValue(query, b.kSecMatchLimit, b.kSecMatchLimitOne)
        out = ctypes.c_void_p()
        status = b.sec.SecItemCopyMatching(query, ctypes.byref(out))
        if status == _ERR_ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise _unsupported("读取", f"{service}/{account}", status)
        pool.append(out)
        return b.read_data(out)
    finally:
        b.release_all(pool)


def set_item(service: str, account: str, value: bytes) -> None:
    """写一个泛型密码条目；已存在则原地 update（无「删了还没写回」的空窗）。"""
    b = _bindings()
    pool: list[ctypes.c_void_p] = []
    try:
        add = b.cfdict(pool, [
            (b.kSecClass, b.kSecClassGenericPassword),
            (b.kSecAttrService, b.cfstr(pool, service)),
            (b.kSecAttrAccount, b.cfstr(pool, account)),
            (b.kSecValueData, b.cfdata(pool, value)),
            (b.kSecUseAuthenticationUI, b.kSecUseAuthenticationUIFail),
        ])
        status = b.sec.SecItemAdd(add, None)
        if status == 0:
            return
        if status == _ERR_DUPLICATE:
            update = b.cfdict(pool, [(b.kSecValueData, b.cfdata(pool, value))])
            status = b.sec.SecItemUpdate(b.base_query(pool, service, account), update)
            if status != 0:
                raise _unsupported("更新", f"{service}/{account}", status)
            return
        raise _unsupported("写入", f"{service}/{account}", status)
    finally:
        b.release_all(pool)


def delete_item(service: str, account: str) -> None:
    """删一个条目；不存在（-25300）按幂等成功处理——终态已达成。"""
    b = _bindings()
    pool: list[ctypes.c_void_p] = []
    try:
        status = b.sec.SecItemDelete(b.base_query(pool, service, account))
        if status in (0, _ERR_ITEM_NOT_FOUND):
            return
        raise _unsupported("删除", f"{service}/{account}", status)
    finally:
        b.release_all(pool)
