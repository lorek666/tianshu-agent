"""F2 执行后端注册表：把「用哪个后端」变成可配置、可枚举、可拒绝的事实。

**先说清楚这个模块能证明什么。**

- 能证明：一次执行请求最终交给哪个后端；"这个后端在这台机器上不存在 / 没实现"
  会在**创建进程之前**以 `PolicyUnsupported` 报出来，而不是悄悄换成 local 跑。
- 不能证明：任何后端真的隔离了。隔离由 `isolation.py` 的实测报告说了算；这里的
  `capabilities` 只是后端**自述**，自述不构成授权（红线 9）。

三条硬规则：

1. **未知 / 未实现 = 拒绝，不回落。** PRD §7 F2 的"后端执行不了必须返回
   `unsupported`，禁止静默放宽"在这里落成一条代码路径：`exec_backend("container")`
   抛 `PolicyUnsupported`，**绝不**返回本地后端。
2. **注册表是进程级、代码级。** 选择来自 `TIANSHU_EXEC_BACKEND`（默认 `local`），
   不来自模型输出、任务参数或事件；第三方后端只能由宿主代码显式
   `register_exec_backend()`——注册就是一次代码改动，不是一次配置。
3. **名字稳定，平台是事实。** 注册表键 `"local"` 在所有平台都叫这个名字；平台差异
   如实落在 `capabilities.name`（`local-posix` / `local-windows`）。

进程级选择的代价与 `sandbox.py` 的文件后端相同：**一个进程只能用一种执行后端**，
per-run 不可换。这一条写进文档，不假装支持 per-run 切换。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import TianshuError
from .exec import LOCAL_EXEC_BACKEND, ExecResult
from .execpolicy import ExecBackendCapabilities, ExecPolicy, PolicyUnsupported

__all__ = [
    "DEFAULT_EXEC_BACKEND",
    "EXEC_BACKEND_ENV",
    "UNIMPLEMENTED_EXEC_BACKENDS",
    "ExecBackend",
    "exec_backend",
    "available_exec_backends",
    "configured_exec_backend_name",
    "register_exec_backend",
    "reset_exec_backends",
]

#: 进程级选择用的环境变量。与 `TIANSHU_SANDBOX_BACKEND`（文件后端）分开：
#: 两者是不同的接缝，混用一个变量会让"文件写入被限制、但命令仍能跑"变成误判。
EXEC_BACKEND_ENV = "TIANSHU_EXEC_BACKEND"

DEFAULT_EXEC_BACKEND = "local"

#: 本实现之外**已知**的名字 → 如实理由。`register_exec_backend()` 注册同名实现后，
#: 该名字会从这里移除（未来容器后端的接入口）。这里列名字不是"支持"，是
#: "知道有人会这么配置，且必须能说出为什么没有"。
_DEFAULT_UNIMPLEMENTED: dict[str, str] = {
    "app_container": (
        "Windows AppContainer / 受限令牌不在 v1 交付范围（TECH-ARCH §6 L3a）"
    ),
    "seatbelt": (
        "macOS 的 sandbox-exec 已被 Apple 弃用，第三方 App 只有 App Sandbox 与 TCC，"
        "v1 不承诺进程级沙箱（TECH-ARCH §6 L3b）"
    ),
    "landlock": "Linux 专属，v1 的四个构建目标不含 Linux 桌面",
    "container": "v1 没有容器后端（TECH-ARCH §6 L4 只写作可选、未交付）",
}

#: 可变的"未实现清单"：注册真实实现会移除对应条目。测试可用
#: `reset_exec_backends()` 恢复。
UNIMPLEMENTED_EXEC_BACKENDS: dict[str, str] = dict(_DEFAULT_UNIMPLEMENTED)


@runtime_checkable
class ExecBackend(Protocol):
    """执行后端的最小协议。`spawn` 的语义与 `exec.spawn()` 相同。

    实现必须**自己**再核一次策略（`check_exec()`），不能假设调用方核过——调用方
    也会核（防御纵深），但后端是最终创建进程的那个。
    """

    capabilities: ExecBackendCapabilities

    def spawn(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | os.PathLike[str],
        env: dict[str, str],
        policy: ExecPolicy,
        blob_dir: Path | None = None,
        pty: bool = False,
    ) -> ExecResult: ...  # pragma: no cover - 协议声明


_BACKENDS: dict[str, ExecBackend] = {DEFAULT_EXEC_BACKEND: LOCAL_EXEC_BACKEND}


def _require_backend(backend: object) -> ExecBackend:
    """核验一个对象是不是后端。核不过直接抛，不半信半疑地收下。"""
    if not isinstance(backend, ExecBackend):
        raise TianshuError(
            "执行后端必须提供 capabilities 与 spawn()，收到："
            f"{type(backend).__name__}"
        )
    capabilities = getattr(backend, "capabilities", None)
    if not isinstance(capabilities, ExecBackendCapabilities):
        raise TianshuError(
            "执行后端的 capabilities 必须是 ExecBackendCapabilities，收到："
            f"{type(capabilities).__name__}"
        )
    if not callable(getattr(backend, "spawn", None)):
        raise TianshuError("执行后端的 spawn 必须可调用")
    return backend


def _clean_name(name: object) -> str:
    if not isinstance(name, str):
        raise TypeError(f"执行后端名字必须是字符串，收到：{type(name).__name__}")
    cleaned = name.strip()
    if not cleaned:
        raise PolicyUnsupported(
            "执行后端名字不能为空",
            backend="<empty>",
            feature="exec_backend",
        )
    return cleaned


def available_exec_backends() -> tuple[str, ...]:
    """当前进程已注册的后端名字（排序，稳定）。"""
    return tuple(sorted(_BACKENDS))


def configured_exec_backend_name() -> str:
    """从 `TIANSHU_EXEC_BACKEND` 读选择；空/未设置 → `local`。

    每次调用都重新读环境变量：测试改 env 立刻生效，daemon 重启语义也简单。
    这里**不**校验注册表——校验在 `exec_backend()` 里做，读名字本身不产生副作用。
    """
    raw = os.environ.get(EXEC_BACKEND_ENV)
    if raw is None:
        return DEFAULT_EXEC_BACKEND
    cleaned = raw.strip()
    return cleaned or DEFAULT_EXEC_BACKEND


def register_exec_backend(
    backend: object, *, name: str | None = None, replace: bool = False
) -> str:
    """注册一个执行后端，返回它的注册名。

    - 默认名字 = `backend.capabilities.name`（第三方后端自报，通常是平台化的）；
    - 同名已存在 → 拒绝，除非显式 `replace=True`（"到底用哪个实现"必须可判定）；
    - 注册成功会从 `UNIMPLEMENTED_EXEC_BACKENDS` 移除同名条目——一个名字要么
      "没有实现"，要么"有实现"，不允许两种说法同时存在。
    """
    host = _require_backend(backend)
    key = _clean_name(name if name is not None else host.capabilities.name)
    if key in _BACKENDS and not replace:
        raise TianshuError(
            f"执行后端 {key!r} 已注册：重复注册会让“到底用哪个实现”不可判定；"
            "替换请显式 replace=True"
        )
    _BACKENDS[key] = host
    UNIMPLEMENTED_EXEC_BACKENDS.pop(key, None)
    return key


def reset_exec_backends() -> None:  # pragma: no cover - 只给测试
    """恢复到初始状态（`local` + 初始未实现清单）。"""
    _BACKENDS.clear()
    _BACKENDS[DEFAULT_EXEC_BACKEND] = LOCAL_EXEC_BACKEND
    UNIMPLEMENTED_EXEC_BACKENDS.clear()
    UNIMPLEMENTED_EXEC_BACKENDS.update(_DEFAULT_UNIMPLEMENTED)


def exec_backend(name: str | None = None) -> ExecBackend:
    """解析执行后端。做不到就 `PolicyUnsupported`，**绝不回落到 local**。

    `name=None` 时读 `TIANSHU_EXEC_BACKEND`（默认 `local`）。显式传名字供装配层
    与测试使用；模型输出、任务参数、事件里的任何字符串都不应该走到这里。
    """
    resolved = (
        configured_exec_backend_name() if name is None else _clean_name(name)
    )
    host = _BACKENDS.get(resolved)
    if host is not None:
        return host

    reason = UNIMPLEMENTED_EXEC_BACKENDS.get(resolved)
    if reason is not None:
        raise PolicyUnsupported(
            f"v1 没有这个执行后端：{reason}",
            backend=resolved,
            feature="exec_backend",
        )
    raise PolicyUnsupported(
        f"未注册的执行后端：{resolved!r}"
        f"（可用：{list(available_exec_backends())}）",
        backend=resolved,
        feature="exec_backend",
    )