"""PRD §8.1.1 隔离门禁：四边界真实探针 + 后端×平台支持矩阵 + 不满足即关闭能力。

**先说清楚这个模块能证明什么、不能证明什么。**

- 能证明：在当前这台机器上，通过某个**后端** spawn 出来的子进程，实际能不能
  读/写工作区外文件、打开权威数据根里的 SQLite（`RW`）、连本机 loopback 端口、
  读到父进程的哨兵环境变量、以及超时杀树后孙进程是否还活着。全部用**真实子
  进程**测，父进程只观察，不用 mock 冒充。
- 不能证明：超出这些具体探针的"安全性"。本地后端没有沙箱（`isolation="none"`），
  探针只是把它如实地说出来；Windows / 其它架构没有实机证据，一律 `unknown`。

三态语义（与 `exec.ISOLATION_BOUNDARY_*` 对应）：

- `enforced`：探针观察到该边界确实被挡住；
- `not_enforced`：探针观察到子进程**能**越过该边界；
- `unknown`：探针自己没跑成 / 结果读不懂 / 平台能力不足以确认（例如 Windows
  没有 Job Object 时，即使这次看到孙进程死了，也不能声明"树一定能杀干净"）。

`unknown` 与 `not_enforced` **一样阻断**依赖它的能力：没有证据不等于隔离成立。

门（`require_isolation` / `require_capability`）只认**探针报告**，不认后端自称的
`ExecBackendCapabilities.isolation`：声明是"我说我有什么"，证据是"测出来是什么"。
两者冲突时门按证据拒绝（负控里有一个"声称 app_container 但探针不通过"的假后端）。

本轮**不改**既有 `run_command` 的默认行为（它仍只受显式 `ExecPolicy` 约束）；
门先以库 API 形式落地并被测试覆盖，接入模型生成代码 / 不可信本地 MCP / 直接执行
命令的子代理等能力由后续工作流按本门调用。
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .exec import (
    BOUNDARY_ENFORCED,
    BOUNDARY_NOT_ENFORCED,
    BOUNDARY_UNKNOWN,
    ISOLATION_BOUNDARIES,
    ISOLATION_BOUNDARY_CREDENTIALS,
    ISOLATION_BOUNDARY_FILES,
    ISOLATION_BOUNDARY_NETWORK,
    ISOLATION_BOUNDARY_PROCESS_TREE,
    ExecResult,
    platform_label,
)
from .execpolicy import (
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyUnsupported,
)

#: 探测逻辑版本。缓存键与报告都带它：探针语义变了，旧证据不能继续给新代码用。
PROBE_VERSION = "v1"

#: 非杀树探针的子进程超时上限。真实子进程出问题时宁可 unknown，不挂住调用方。
PROBE_TIMEOUT_MS = 5_000

#: 进程树探针：子进程被允许活这么久（足够启动 + 写孙进程 pid），然后超时杀树。
PROBE_KILL_WINDOW_MS = 1_500

#: 杀树后等孙进程真正消失的观察窗口（轮询）。
PROBE_GRACE_MS = 3_000

POLL_INTERVAL_S = 0.05

_SENTINEL_NAME = "TIANSHU_ISOLATION_SENTINEL"
_PROBE_SECRET = "isolation-probe-secret"


class IsolationRefused(PolicyUnsupported):
    """隔离边界未证明，依赖它的能力被关闭。

    故意继承 `PolicyUnsupported`：这是"后端在这台机器上做不到/没证据"，
    **不是**"策略不允许"——改策略不会通过，只能换后端/换平台，或者在
    `docs/isolation-matrix.md` 里补齐该平台的实测证据。与既有 F2 语义一致：
    不允许把一句"做不到"降级成"不隔离但能跑"。
    """

    def __init__(
        self,
        *,
        backend: str,
        boundary: str,
        status: str,
        evidence: str,
        capability: str | None = None,
    ) -> None:
        super().__init__(
            f"隔离边界 {boundary} 未证明（{status}）：{evidence}；"
            "依赖该边界的能力不予启用（无证据不等于隔离成立）",
            backend=backend,
            feature=f"isolation:{boundary}",
        )
        self.backend = backend
        self.boundary = boundary
        self.status = status
        self.evidence = evidence
        self.capability = capability


class SpawnBackend(Protocol):
    """探针要求的最小后端协议：能力声明 + spawn（与 `LocalExecBackend` 同形）。"""

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
    ) -> ExecResult: ...


@dataclass(frozen=True)
class IsolationFinding:
    """一个边界的探针结论。`evidence` 必须能复现（命令/退出码/观察）。"""

    boundary: str
    status: str
    evidence: str

    def __post_init__(self) -> None:
        if self.boundary not in ISOLATION_BOUNDARIES:
            raise ValueError(
                f"未知隔离边界：{self.boundary!r}（可选 {ISOLATION_BOUNDARIES}）"
            )
        if self.status not in (
            BOUNDARY_ENFORCED,
            BOUNDARY_NOT_ENFORCED,
            BOUNDARY_UNKNOWN,
        ):
            raise ValueError(f"未知探针状态：{self.status!r}")
        if not isinstance(self.evidence, str) or not self.evidence:
            raise ValueError("探针结论必须带可复现的 evidence")


@dataclass(frozen=True)
class IsolationReport:
    """一次四边界探针的完整报告。"""

    backend: str
    platform: str
    findings: tuple[IsolationFinding, ...] = field(default_factory=tuple)
    #: 探测现场：报告只对"这两个解析后的目录"有效。空串 = 手工构造/未绑定，
    #: 门会拒绝它（报告不是这次的证据，就不是证据）。
    workspace: str = ""
    data_root: str = ""
    probe_version: str = PROBE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not self.backend:
            raise ValueError("报告必须带后端名")
        if not isinstance(self.platform, str) or not self.platform:
            raise ValueError("报告必须带平台标签")
        seen = [finding.boundary for finding in self.findings]
        if sorted(seen) != sorted(ISOLATION_BOUNDARIES):
            raise ValueError(
                "隔离报告必须完整覆盖四边界且不重复："
                f"期望 {ISOLATION_BOUNDARIES}，实际 {tuple(seen)}"
            )

    def finding(self, boundary: str) -> IsolationFinding:
        for finding in self.findings:
            if finding.boundary == boundary:
                return finding
        raise ValueError(f"报告里没有边界：{boundary!r}")

    @property
    def enforced(self) -> tuple[str, ...]:
        return tuple(
            f.boundary for f in self.findings if f.status == BOUNDARY_ENFORCED
        )

    @property
    def not_enforced(self) -> tuple[str, ...]:
        return tuple(
            f.boundary for f in self.findings if f.status == BOUNDARY_NOT_ENFORCED
        )

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(
            f.boundary for f in self.findings if f.status == BOUNDARY_UNKNOWN
        )


# --------------------------------------------------------------- 视图、缓存与按策略的要求


#: 等级 → 该等级要求"累计"通过的边界。只用于**显示**（`isolation_view.level`）：
#: 门的判据是"请求了哪条边界就核哪条"，边界之间独立，不按这张表连带检查。
ISOLATION_LEVEL_BOUNDARIES: dict[str, tuple[str, ...]] = {
    "none": (),
    "path_only": (ISOLATION_BOUNDARY_FILES,),
    "process_tree": (
        ISOLATION_BOUNDARY_FILES,
        ISOLATION_BOUNDARY_PROCESS_TREE,
    ),
    "app_container": ISOLATION_BOUNDARIES,
}

_PROBE_CACHE: dict[tuple[object, ...], tuple[object, IsolationReport]] = {}


def measured_level(report: IsolationReport | None) -> str:
    """只按**实测**报告推累计等级；没有报告 = `none`（声明不产生等级）。

    `unknown` / `not_enforced` 都不算通过。累计语义：`process_tree` 要求
    files + tree 都 enforced，`app_container` 要求四边界全过——这与 TECH-ARCH
    的分层一致，但**只用于显示**，不是门的判据（见 `require_isolation_requirements`）。
    """
    if report is None:
        return "none"
    for level in ("app_container", "process_tree", "path_only"):
        if all(
            report.finding(boundary).status == BOUNDARY_ENFORCED
            for boundary in ISOLATION_LEVEL_BOUNDARIES[level]
        ):
            return level
    return "none"


def isolation_view(
    backend: Any, report: IsolationReport | None = None
) -> dict[str, Any]:
    """一次执行的隔离视图（机器可读；中文映射在 UI）。

    - `level`：有报告时按实测累计；无报告恒 `none`——**自述不产生等级**。
    - `unverified`：没有报告。UI 必须先处理它，不能直接按 `level` 说"受信任执行"。
    - `contradicted`：自述与实测不一致（报告胜出）。
    - `boundaries`：逐边界状态；无报告时四边界都是 `unknown`（不知道 ≠ 已挡住）。
    """
    capabilities = backend.capabilities
    declared = capabilities.isolation
    boundaries = {
        boundary: (
            report.finding(boundary).status
            if report is not None
            else BOUNDARY_UNKNOWN
        )
        for boundary in ISOLATION_BOUNDARIES
    }
    level = measured_level(report)
    return {
        "backend": _backend_name(backend),
        "declared": declared,
        "measured": level if report is not None else None,
        "level": level,
        "unverified": report is None,
        "contradicted": report is not None and level != declared,
        "boundaries": boundaries,
        "enforced_boundaries": (
            sorted(report.enforced) if report is not None else []
        ),
    }


def probe_backend_cached(
    backend: Any,
    *,
    workspace: str | os.PathLike[str],
    data_root: str | os.PathLike[str],
    timeout_ms: int = PROBE_TIMEOUT_MS,
    refresh: bool = False,
) -> IsolationReport:
    """带缓存的探测。缓存条目绑定**探测现场 + 后端对象身份 + 探测版本**：

    - 换 `data_root`/`workspace` → 重测（`files_authority` 的证据依赖这两个路径）；
    - `register_exec_backend(replace=True)` / `reset_exec_backends()` 之后解析到的
      是另一个对象 → 重测（同名新实现不能继承旧实现的 `enforced`）；
    - 探测逻辑升级（`PROBE_VERSION` 变）→ 重测。
    """
    ws = Path(workspace).resolve()
    root = Path(data_root).resolve()
    key: tuple[object, ...] = (
        id(backend),
        platform_label(),
        str(ws),
        str(root),
        timeout_ms,
        PROBE_VERSION,
    )
    cached = _PROBE_CACHE.get(key)
    if cached is not None and cached[0] is backend and not refresh:
        return cached[1]
    report = probe_backend(
        backend, workspace=ws, data_root=root, timeout_ms=timeout_ms
    )
    _PROBE_CACHE[key] = (backend, report)
    return report


def reset_probe_cache() -> None:  # pragma: no cover - 只给测试
    """清空进程内探测缓存。"""
    _PROBE_CACHE.clear()


def require_isolation_requirements(
    policy: ExecPolicy,
    backend: Any,
    *,
    workspace: str | os.PathLike[str],
    data_root: str | os.PathLike[str],
    report: IsolationReport | None = None,
) -> IsolationReport | None:
    """按策略声明的边界逐条过门；任何一条没有实测证明就拒绝。

    - `policy.isolation_requires` 为空 → 不探测、不拒绝（返回 None）；
    - 非空 → 必须有**绑定本次现场**的报告：`workspace` / `data_root` 必须与
      解析后路径一致，`probe_version` 必须是当前版本。调用方塞进来的其它现场
      报告一律拒绝（否则"换个数据根的重测"就绕过了缓存键）；
    - 边界之间**独立**：请求 `process_tree` 只核杀树，不隐含 files。
    """
    required = tuple(getattr(policy, "isolation_requires", ()))
    if not required:
        return None
    ws = Path(workspace).resolve()
    name = _backend_name(backend)
    if data_root is None:
        # 没有数据根就产不出绑定现场的报告；拿临时目录假装测过 =
        # 把另一个现场的结论当成这个任务的证据。
        raise IsolationRefused(
            backend=name,
            boundary=required[0],
            status=BOUNDARY_UNKNOWN,
            evidence="执行路径没有数据根：无法产出绑定本次现场的报告",
        )
    root = Path(data_root).resolve()
    if report is None:
        report = probe_backend_cached(
            backend, workspace=ws, data_root=root
        )
    if (
        report.workspace != str(ws)
        or report.data_root != str(root)
        or report.probe_version != PROBE_VERSION
    ):
        raise IsolationRefused(
            backend=name,
            boundary=required[0],
            status=BOUNDARY_UNKNOWN,
            evidence=(
                "隔离报告没有绑定本次探测现场"
                f"（报告 workspace={report.workspace!r} data_root={report.data_root!r}"
                f" probe={report.probe_version!r}；本次 {str(ws)!r}/{str(root)!r}"
                f"/{PROBE_VERSION!r}）"
            ),
        )
    for boundary in required:
        require_isolation(backend, boundary, report=report)
    return report


# --------------------------------------------------------------- 依赖关系


CAPABILITY_MODEL_GENERATED_CODE = "model_generated_code"
CAPABILITY_UNTRUSTED_LOCAL_MCP = "untrusted_local_mcp"
CAPABILITY_DIRECT_EXEC_SUBAGENT = "direct_exec_subagent"

#: 依赖隔离边界的能力。本轮的三个能力都要求**四边界全部 enforced**：
#: 它们都能让不可信代码在宿主上跑，任何一条边界失守都足以越界。
CAPABILITY_BOUNDARIES: dict[str, tuple[str, ...]] = {
    CAPABILITY_MODEL_GENERATED_CODE: ISOLATION_BOUNDARIES,
    CAPABILITY_UNTRUSTED_LOCAL_MCP: ISOLATION_BOUNDARIES,
    CAPABILITY_DIRECT_EXEC_SUBAGENT: ISOLATION_BOUNDARIES,
}


def _backend_name(backend: Any) -> str:
    capabilities = getattr(backend, "capabilities", None)
    name = getattr(capabilities, "name", "")
    return name if isinstance(name, str) and name else type(backend).__name__


# --------------------------------------------------------------- 门


def require_isolation(
    backend: Any,
    boundary: str,
    *,
    report: IsolationReport | None = None,
) -> IsolationFinding:
    """门：该边界没有 `enforced` 证据就拒绝。

    `report` 是 `probe_backend()` 的实测报告；不传按 `unknown` 处理——没有证据
    不等于隔离成立，这正是 fail-closed。返回 finding 方便调用方记录证据。
    """
    if boundary not in ISOLATION_BOUNDARIES:
        raise ValueError(
            f"未知隔离边界：{boundary!r}（可选 {ISOLATION_BOUNDARIES}）"
        )
    name = _backend_name(backend)
    if report is None:
        raise IsolationRefused(
            backend=name,
            boundary=boundary,
            status=BOUNDARY_UNKNOWN,
            evidence="没有提供 probe_backend() 的实测报告；无证据不等于隔离成立",
        )
    finding = report.finding(boundary)
    if finding.status != BOUNDARY_ENFORCED:
        raise IsolationRefused(
            backend=name,
            boundary=boundary,
            status=finding.status,
            evidence=finding.evidence,
        )
    return finding


def require_capability(
    backend: Any,
    capability: str,
    *,
    report: IsolationReport | None = None,
) -> tuple[IsolationFinding, ...]:
    """门：能力依赖的边界全部 `enforced` 才允许启用；否则抛 `IsolationRefused`。

    能力清单见 `CAPABILITY_BOUNDARIES`。返回通过的 findings。
    """
    try:
        required = CAPABILITY_BOUNDARIES[capability]
    except KeyError:
        raise ValueError(
            f"未知能力：{capability!r}（可选 {sorted(CAPABILITY_BOUNDARIES)}）"
        ) from None
    name = _backend_name(backend)
    if report is None:
        raise IsolationRefused(
            backend=name,
            boundary=required[0],
            status=BOUNDARY_UNKNOWN,
            evidence=(
                f"能力 {capability} 需要 {list(required)} 全部有实测证据，"
                "但没有提供 probe_backend() 报告；无证据不等于隔离成立"
            ),
            capability=capability,
        )
    missing = [
        report.finding(boundary)
        for boundary in required
        if report.finding(boundary).status != BOUNDARY_ENFORCED
    ]
    if missing:
        detail = "；".join(
            f"{finding.boundary}={finding.status}（{finding.evidence}）"
            for finding in missing
        )
        raise IsolationRefused(
            backend=name,
            boundary=missing[0].boundary,
            status=missing[0].status,
            evidence=(
                f"能力 {capability} 依赖的边界未全部证明：" + detail
            ),
            capability=capability,
        )
    return tuple(report.finding(boundary) for boundary in required)


def enabled_capabilities(
    backend: Any, *, report: IsolationReport | None = None
) -> list[str]:
    """当前证据下**允许启用**的能力（其余全部关闭）。"""
    allowed: list[str] = []
    for capability in sorted(CAPABILITY_BOUNDARIES):
        try:
            require_capability(backend, capability, report=report)
        except IsolationRefused:
            continue
        allowed.append(capability)
    return allowed


# --------------------------------------------------------------- 四边界探针


def probe_backend(
    backend: Any,
    *,
    workspace: str | os.PathLike[str],
    data_root: str | os.PathLike[str],
    timeout_ms: int = PROBE_TIMEOUT_MS,
) -> IsolationReport:
    """对后端跑四个真实子进程探针，返回完整报告。

    `workspace` 是允许子进程活动的目录；探针会在 `data_root` 下建临时目录放
    「工作区外文件」与「权威库探针」，结束后删除。探针只观察、不改被探测的
    真实数据：权威库探针用的是临时目录里的独立 SQLite 文件。
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError(f"timeout_ms 必须是正整数：{timeout_ms!r}")
    ws = Path(workspace).resolve()
    root = Path(data_root).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    probe_dir = Path(tempfile.mkdtemp(prefix="isolation-probe-", dir=str(root)))
    try:
        findings = (
            _probe_files_authority(backend, ws, probe_dir, timeout_ms),
            _probe_network(backend, ws, timeout_ms),
            _probe_credentials(backend, ws, timeout_ms),
            _probe_process_tree(backend, ws, probe_dir, timeout_ms),
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    return IsolationReport(
        backend=_backend_name(backend),
        platform=platform_label(),
        findings=findings,
        workspace=str(ws),
        data_root=str(root),
        probe_version=PROBE_VERSION,
    )


def _probe_policy(
    workspace: Path,
    *,
    env_allowlist: tuple[str, ...],
    timeout_ms: int,
) -> ExecPolicy:
    """探针用的最小策略：只允许本解释器、只允许 workspace 作为 cwd。"""
    return ExecPolicy(
        argv_allowlist=(sys.executable,),
        cwd_roots=(workspace,),
        env_allowlist=env_allowlist,
        max_output_bytes=64 * 1024,
        timeout_ms=timeout_ms,
    )


def _base_env() -> dict[str, str]:
    # 只给子进程一个最小 PATH；其余宿主环境不传（凭据探针会单独加哨兵）。
    return {"PATH": "/usr/bin:/bin"}


def _run_child(
    backend: Any,
    script: str,
    args: list[Any],
    *,
    workspace: Path,
    timeout_ms: int,
    env: dict[str, str],
    env_allowlist: tuple[str, ...],
) -> tuple[ExecResult | None, dict[str, str], str]:
    """spawn 一个探针子进程并解析 `KEY=VALUE` 行。

    返回 (result, parsed, problem)。`problem` 非空表示**探针本身没跑成**
    （spawn 抛错/超时/非零退出/无可解析输出）——调用方据此判 `unknown`，
    绝不能把"探针坏了"当成"边界被挡住了"。
    """
    argv = [sys.executable, "-c", script, *[str(arg) for arg in args]]
    policy = _probe_policy(
        workspace, env_allowlist=env_allowlist, timeout_ms=timeout_ms
    )
    try:
        result = backend.spawn(
            argv, cwd=str(workspace), env=dict(env), policy=policy
        )
    except Exception as exc:  # noqa: BLE001 - 后端自己坏了：unknown，不当通过
        return None, {}, f"spawn 失败：{type(exc).__name__}: {exc}"
    if not isinstance(result, ExecResult):
        return None, {}, f"后端返回了非 ExecResult：{type(result).__name__}"
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            parsed[key.strip()] = value.strip()
    detail = (
        f"退出码={result.exit_code} 信号={result.signal} 超时={result.timed_out}"
        f" 耗时={result.duration_ms}ms stdout={result.stdout.strip()!r}"
        f" stderr={result.stderr.strip()!r}"
    )
    if result.timed_out:
        return result, parsed, f"探针子进程超时（{timeout_ms}ms）：{detail}"
    if result.exit_code != 0:
        return result, parsed, f"探针子进程非零退出：{detail}"
    if not parsed:
        return result, parsed, f"探针子进程没有可解析输出：{detail}"
    return result, parsed, ""


def _status_from_flags(parsed: dict[str, str], keys: tuple[str, ...]) -> str:
    """多个 KEY=VALUE 观察合成结论：都 FAIL 才算 enforced。"""
    values = [parsed.get(key) for key in keys]
    if any(value is None for value in values):
        return BOUNDARY_UNKNOWN
    normalized = ["OK" if value in ("OK", "ODD") else value for value in values]
    if any(value == "OK" for value in normalized):
        return BOUNDARY_NOT_ENFORCED
    if all(value == "FAIL" for value in normalized):
        return BOUNDARY_ENFORCED
    return BOUNDARY_UNKNOWN


_PROBE_FILES = """
import pathlib, sqlite3, sys

results = []
try:
    text = pathlib.Path(sys.argv[1]).read_bytes().decode("utf-8")
    results.append("OUTSIDE_READ=" + ("OK" if {secret!r} in text else "ODD"))
except Exception:
    results.append("OUTSIDE_READ=FAIL")
try:
    pathlib.Path(sys.argv[2]).write_bytes(b"probe-write")
    results.append("OUTSIDE_WRITE=OK")
except Exception:
    results.append("OUTSIDE_WRITE=FAIL")
try:
    conn = sqlite3.connect(sys.argv[3])
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()
    conn.close()
    results.append("AUTHORITY_DB=OK")
except Exception:
    results.append("AUTHORITY_DB=FAIL")
print("\\n".join(results))
"""


def _probe_files_authority(
    backend: Any, workspace: Path, probe_dir: Path, timeout_ms: int
) -> IsolationFinding:
    outside_dir = probe_dir / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_read = outside_dir / "secret.txt"
    outside_read.write_bytes((_PROBE_SECRET + "\n").encode("utf-8"))
    outside_write = outside_dir / "written-by-child.txt"
    authority_db = probe_dir / "authority-probe.db"
    conn = sqlite3.connect(authority_db)
    try:
        conn.execute("CREATE TABLE probe_marker (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    script = _PROBE_FILES.format(secret=_PROBE_SECRET)
    result, parsed, problem = _run_child(
        backend,
        script,
        [outside_read, outside_write, authority_db],
        workspace=workspace,
        timeout_ms=timeout_ms,
        env=_base_env(),
        env_allowlist=("PATH",),
    )
    if problem:
        return IsolationFinding(
            ISOLATION_BOUNDARY_FILES, BOUNDARY_UNKNOWN, problem
        )
    status = _status_from_flags(
        parsed, ("OUTSIDE_READ", "OUTSIDE_WRITE", "AUTHORITY_DB")
    )
    observed = {
        "OUTSIDE_READ": parsed.get("OUTSIDE_READ"),
        "OUTSIDE_WRITE": parsed.get("OUTSIDE_WRITE"),
        "AUTHORITY_DB": parsed.get("AUTHORITY_DB"),
        "outside_written": outside_write.exists(),
    }
    evidence = (
        "子进程读工作区外文件 / 写工作区外文件 / 以 RW 打开权威库（BEGIN IMMEDIATE"
        f" 后回滚）；退出码={result.exit_code}，观察={observed}"
    )
    return IsolationFinding(ISOLATION_BOUNDARY_FILES, status, evidence)


_PROBE_NETWORK = """
import socket, sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2.0)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
    print("LOOPBACK=OK")
except Exception:
    print("LOOPBACK=FAIL")
finally:
    sock.close()
"""


def _probe_network(
    backend: Any, workspace: Path, timeout_ms: int
) -> IsolationFinding:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        result, parsed, problem = _run_child(
            backend,
            _PROBE_NETWORK,
            [port],
            workspace=workspace,
            timeout_ms=timeout_ms,
            env=_base_env(),
            env_allowlist=("PATH",),
        )
    finally:
        listener.close()
    if problem:
        return IsolationFinding(
            ISOLATION_BOUNDARY_NETWORK, BOUNDARY_UNKNOWN, problem
        )
    status = _status_from_flags(parsed, ("LOOPBACK",))
    evidence = (
        f"父进程监听 127.0.0.1:{port}，子进程尝试 connect；"
        f"退出码={result.exit_code}，观察={dict(parsed)}"
    )
    return IsolationFinding(ISOLATION_BOUNDARY_NETWORK, status, evidence)


_PROBE_CREDENTIALS = """
import os, sys

expected = os.environ.get(sys.argv[1])
print("SENTINEL=" + ("OK" if expected == sys.argv[2] else "FAIL"))
"""


def _probe_credentials(
    backend: Any, workspace: Path, timeout_ms: int
) -> IsolationFinding:
    token = secrets.token_hex(16)
    env = _base_env()
    env[_SENTINEL_NAME] = token
    result, parsed, problem = _run_child(
        backend,
        _PROBE_CREDENTIALS,
        [_SENTINEL_NAME, token],
        workspace=workspace,
        timeout_ms=timeout_ms,
        env=env,
        env_allowlist=("PATH", _SENTINEL_NAME),
    )
    if problem:
        return IsolationFinding(
            ISOLATION_BOUNDARY_CREDENTIALS, BOUNDARY_UNKNOWN, problem
        )
    status = _status_from_flags(parsed, ("SENTINEL",))
    evidence = (
        f"父进程设哨兵环境变量 {_SENTINEL_NAME}（值不落文档），子进程读；"
        f"退出码={result.exit_code}，观察={dict(parsed)}"
    )
    return IsolationFinding(ISOLATION_BOUNDARY_CREDENTIALS, status, evidence)


_PROBE_PROCESS_TREE = """
import pathlib, subprocess, sys, time

grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"]
)
pathlib.Path(sys.argv[1]).write_bytes(str(grandchild.pid).encode("utf-8"))
time.sleep(120)
"""


def _probe_process_tree(
    backend: Any, workspace: Path, probe_dir: Path, timeout_ms: int
) -> IsolationFinding:
    pid_file = probe_dir / "grandchild.pid"
    kill_window = min(PROBE_KILL_WINDOW_MS, timeout_ms)
    result, _parsed, problem = _run_child(
        backend,
        _PROBE_PROCESS_TREE,
        [pid_file],
        workspace=workspace,
        timeout_ms=kill_window,
        env=_base_env(),
        env_allowlist=("PATH",),
    )
    if result is None:
        return IsolationFinding(
            ISOLATION_BOUNDARY_PROCESS_TREE, BOUNDARY_UNKNOWN, problem
        )
    pid = _read_pid(pid_file)
    detail = (
        f"超时={result.timed_out} descendants_may_survive="
        f"{result.descendants_may_survive} 耗时={result.duration_ms}ms"
        f" 孙进程pid={pid}"
    )
    if not result.timed_out:
        return IsolationFinding(
            ISOLATION_BOUNDARY_PROCESS_TREE,
            BOUNDARY_UNKNOWN,
            f"探测窗口内子进程没有超时，未进入杀树路径（{detail}）；"
            "不能据此声明能杀树",
        )
    if pid is None:
        return IsolationFinding(
            ISOLATION_BOUNDARY_PROCESS_TREE,
            BOUNDARY_UNKNOWN,
            f"子进程没有留下孙进程 pid，无法确认树是否终止（{detail}）",
        )
    alive = _wait_pid_gone(pid, PROBE_GRACE_MS)
    if alive is None:
        _terminate_pid(pid)
        return IsolationFinding(
            ISOLATION_BOUNDARY_PROCESS_TREE,
            BOUNDARY_UNKNOWN,
            f"无法判定孙进程 {pid} 是否存活（{detail}）",
        )
    if alive:
        _terminate_pid(pid)
        return IsolationFinding(
            ISOLATION_BOUNDARY_PROCESS_TREE,
            BOUNDARY_NOT_ENFORCED,
            f"超时杀树后孙进程 {pid} 仍存活（{detail}）",
        )
    if result.descendants_may_survive:
        # 看到了这一次死亡，但后端自己说"无法确认树已终止"（Windows 无 Job Object）：
        # 一次观察不能升级成能力声明。
        return IsolationFinding(
            ISOLATION_BOUNDARY_PROCESS_TREE,
            BOUNDARY_UNKNOWN,
            f"孙进程 {pid} 本次已消失，但后端无法确认杀树"
            f"（descendants_may_survive=True，{detail}）",
        )
    return IsolationFinding(
        ISOLATION_BOUNDARY_PROCESS_TREE,
        BOUNDARY_ENFORCED,
        f"超时后按进程组杀树，孙进程 {pid} 在观察窗口内消失（{detail}）",
    )


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_bytes().decode("utf-8").strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool | None:
    """进程是否还活着；`None` = 无法判定（不能拿"读不出来"当"死了"）。"""
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # kill(pid, 0) 对僵尸进程也返回成功；用 ps 的状态列区分 Z（僵尸=已死）。
        ps = shutil.which("ps")
        if ps is None:
            return None
        try:
            proc = subprocess.run(
                [ps, "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        return not proc.stdout.strip().upper().startswith("Z")
    if sys.platform.startswith("win"):
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - Windows API 不可用：判不了
            return None
    return None


def _wait_pid_gone(pid: int, grace_ms: int) -> bool | None:
    """轮询等待 pid 消失。返回 True=仍活着，False=已死，None=判不了。"""
    deadline = time.monotonic() + grace_ms / 1000
    last: bool | None = True
    while time.monotonic() < deadline:
        last = _pid_alive(pid)
        if last is False:
            return False
        if last is None:
            return None
        time.sleep(POLL_INTERVAL_S)
    return last


def _terminate_pid(pid: int) -> None:
    """探针兜底清理：绝不给系统留下一个跑 120 秒的孙进程。"""
    try:
        if os.name == "posix":
            os.kill(pid, signal.SIGKILL)
        elif sys.platform.startswith("win"):
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


__all__ = [
    "CAPABILITY_BOUNDARIES",
    "CAPABILITY_DIRECT_EXEC_SUBAGENT",
    "CAPABILITY_MODEL_GENERATED_CODE",
    "CAPABILITY_UNTRUSTED_LOCAL_MCP",
    "IsolationFinding",
    "IsolationRefused",
    "IsolationReport",
    "PROBE_GRACE_MS",
    "PROBE_KILL_WINDOW_MS",
    "PROBE_TIMEOUT_MS",
    "PROBE_VERSION",
    "ISOLATION_LEVEL_BOUNDARIES",
    "SpawnBackend",
    "enabled_capabilities",
    "isolation_view",
    "measured_level",
    "probe_backend",
    "probe_backend_cached",
    "require_capability",
    "require_isolation",
    "require_isolation_requirements",
    "reset_probe_cache",
]
