"""F2 执行策略：声明式策略、默认全拒、deny 优先、后端能力如实声明。

先把边界写清楚，免得下一个人把 F2 的 P0 理解成"实现 Seatbelt"：

- 本模块只交付**判定**：一次 spawn 在**策略上**允不允许、**后端**能不能按请求
  的方式执行。真正的进程创建（stdio、信号、退出码）是 F1 的事，会在本模块的
  判定之上实现。
- v1 不承诺 macOS 的进程级沙箱：`docs/TECH-ARCH.md` §6 已定调 `sandbox-exec`
  弃用、第三方 App 只有 App Sandbox 与 TCC，所以 macOS 的 L3 标 `unsupported`。
  本模块的价值是让这件事**由后端自己说出来**，而不是靠文档免责声明兜。

三条硬规则：

1. **默认全拒。** 空 `ExecPolicy()` 拒绝任何 spawn。argv 白名单为空、cwd 根为空、
   上限未声明，都按"拒绝"处理——不是"没限制所以放行"。默认安全只能靠 deny by
   default，不能靠"默认放行 + 事后 deny"。
2. **deny 优先。** `argv_denylist` 在 `argv_allowlist` 之前判定；命中 deny 就是拒，
   后面的规则不能翻盘。这条防的是"策略里拼错一条 allow，整台机器就全开"。
3. **`PolicyDenied` 与 `PolicyUnsupported` 是两种错，不许合并。**
   deny = "策略不允许你这么做"（找策略/找人批准）；unsupported = "这个后端在这台
   机器上做不到"（换后端或换平台，改策略没用）。两者刻意**没有公共子类**，就是
   为了让 `except` 不能把它们一网打尽——合并之后 UI 只能给用户一句含糊的"失败"。

两条实现约束：

- argv 必须是数组，字符串命令一律拒绝（否则等于把 shell 解释权交回去）。本模块
  不 import `subprocess`，也不存在 `shell=True` 的路径。
- 环境变量按白名单**过滤**后才允许传给子进程；`check_spawn` 会拒绝"没过滤的
  env"，避免调用方把 `os.environ` 直接交给子进程、把凭据一起带进去。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TianshuError
from .pathkeys import is_ancestor_or_self, overlap_key

# --------------------------------------------------------------- 规则 id
#
# 规则 id 是给 UI/日志看的稳定标识：用户要能一眼看出"该改策略还是该换机器"。
# 不要为了省事复用一个 id——复用了就再也分不清是哪条规则拦的。

RULE_ARGV_NOT_ARRAY = "f2.argv_not_array"
RULE_ARGV_EMPTY = "f2.argv_empty"
RULE_ARGV_NOT_STR = "f2.argv_not_str"
RULE_ARGV_NUL = "f2.argv_nul"
RULE_ARGV_NOT_ABSOLUTE = "f2.argv_not_absolute"
RULE_ARGV_DENIED = "f2.argv_denied"
RULE_ARGV_NOT_ALLOWED = "f2.argv_not_allowed"
RULE_CWD_NOT_ABSOLUTE = "f2.cwd_not_absolute"
RULE_CWD_OUTSIDE_ROOTS = "f2.cwd_outside_roots"
RULE_TIMEOUT_NOT_DECLARED = "f2.timeout_not_declared"
RULE_OUTPUT_LIMIT_NOT_DECLARED = "f2.output_limit_not_declared"
RULE_ENV_NOT_MAPPING = "f2.env_not_mapping"
RULE_ENV_INVALID = "f2.env_invalid"
RULE_ENV_NOT_FILTERED = "f2.env_not_filtered"

# --------------------------------------------------------------- 能力名与隔离级别

FEATURE_PTY = "pty"
FEATURE_SIGNALS = "signals"
FEATURE_PROCESS_GROUP = "process_group"
FEATURE_JOB_OBJECTS = "job_objects"
FEATURE_NETWORK_ISOLATION = "network_isolation"

#: 能力名 → `ExecBackendCapabilities` 的字段名。
#: 提供 `require()` 的通用入口，F1 加新能力时只需要在这里登记一行。
_FEATURE_ATTRS: dict[str, str] = {
    FEATURE_PTY: "supports_pty",
    FEATURE_SIGNALS: "supports_signals",
    FEATURE_PROCESS_GROUP: "supports_process_group",
    FEATURE_JOB_OBJECTS: "supports_job_objects",
    FEATURE_NETWORK_ISOLATION: "supports_network_isolation",
}

#: 后端如实声明的隔离级别。TECH-ARCH §6 的分层：
#: L1 = 路径控制，L2 = 进程树治理，L3 = 越界读防护，L4 = 容器。
ISOLATION_NONE = "none"
ISOLATION_PATH_ONLY = "path_only"
ISOLATION_PROCESS_TREE = "process_tree"
ISOLATION_APP_CONTAINER = "app_container"
ISOLATION_CONTAINER = "container"

RECOMMENDED_ISOLATION = (
    ISOLATION_PATH_ONLY,
    ISOLATION_PROCESS_TREE,
    ISOLATION_APP_CONTAINER,
    ISOLATION_CONTAINER,
)

#: 后端可声明的**封闭**隔离级别集。它必须与事件契约
#: `_ISOLATION_VIEW.declared` 的 enum 一致：输出契约收紧了，来源不收紧，
#: 一个 ``isolation="bogus"`` 的后端会把"成功执行"变成写事件时的 unknown。
ISOLATION_LEVELS: tuple[str, ...] = (ISOLATION_NONE, *RECOMMENDED_ISOLATION)

#: PRD §8.1.1 的四条隔离边界。它们是**被实测的对象**（`isolation.py` 用真实
#: 子进程逐条探测），不是后端自述的等级名。策略里的"隔离要求"用的是这四个
#: 边界名，不是 L1/L2/L3——等级是累计显示，边界才是证据单位。
ISOLATION_BOUNDARY_FILES = "files_authority"
ISOLATION_BOUNDARY_NETWORK = "network"
ISOLATION_BOUNDARY_CREDENTIALS = "credentials"
ISOLATION_BOUNDARY_PROCESS_TREE = "process_tree"
ISOLATION_BOUNDARIES: tuple[str, ...] = (
    ISOLATION_BOUNDARY_FILES,
    ISOLATION_BOUNDARY_NETWORK,
    ISOLATION_BOUNDARY_CREDENTIALS,
    ISOLATION_BOUNDARY_PROCESS_TREE,
)


class PolicyDenied(TianshuError):
    """策略不允许这次执行。

    **不是**后端能力问题：改策略、缩小 argv、换目录或找人批准都可能解决。
    与 `PolicyUnsupported` 刻意没有公共子类（见模块 docstring）。
    """

    def __init__(self, reason: str, *, rule_id: str) -> None:
        super().__init__(f"{reason}（规则：{rule_id}）")
        self.reason = reason
        self.rule_id = rule_id


class PolicyUnsupported(TianshuError):
    """这个后端在这台机器上做不到，不是策略不允许。

    改策略不会通过；只能换后端/换平台。`unsupported` 时**禁止降级**成
    "不隔离但能跑"——那会把一句"做不到"变成一次静默放宽。
    """

    def __init__(self, reason: str, *, backend: str, feature: str) -> None:
        super().__init__(
            f"后端 {backend} 无法提供 {feature}：{reason}"
            "（这是后端能力问题，不是策略拒绝；改策略不会通过）"
        )
        self.reason = reason
        self.backend = backend
        self.feature = feature


def _deny(reason: str, rule_id: str) -> PolicyDenied:
    return PolicyDenied(reason, rule_id=rule_id)


def _require_sequence_of_str(name: str, value: object) -> tuple[str, ...]:
    """把声明字段规范成 tuple[str, ...]。

    单个字符串会被当成"字符序列"逐字符迭代——这是最容易写出来的配置错误，
    必须显式拒绝，而不是悄悄解析成一堆单字符路径。
    """
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} 必须是序列，不是单个字符串：{value!r}")
    if not isinstance(value, Sequence):
        raise ValueError(f"{name} 必须是序列，收到：{type(value).__name__}")
    return tuple(value)


def _normalize_executable(name: str, item: object) -> str:
    if not isinstance(item, str) or not item:
        raise ValueError(f"{name} 的每一项必须是非空字符串：{item!r}")
    if "\x00" in item:
        raise ValueError(f"{name} 不能包含 NUL 字节：{item!r}")
    if not os.path.isabs(item):
        # 不做 PATH 查找：PATH 可被注入，同名二进制可被替换。策略里写什么
        # 绝对路径就只放行那个绝对路径。
        raise ValueError(f"{name} 的每一项必须是绝对路径：{item!r}")
    return os.path.normpath(item)


def _normalize_root(item: object) -> Path:
    if not isinstance(item, (str, os.PathLike)):
        raise ValueError(f"cwd_roots 的每一项必须是路径：{item!r}")
    raw = Path(item)
    text = str(raw)
    if "\x00" in text:
        raise ValueError(f"cwd_roots 不能包含 NUL 字节：{item!r}")
    if not raw.is_absolute():
        raise ValueError(f"cwd_roots 的每一项必须是绝对路径：{item!r}")
    # 构造时就 resolve：策略里存 canonical 形式，判定时 cwd 也 resolve，
    # 两边先消解符号链接/`..` 再比较。macOS 上 /tmp 是 /private/tmp 的
    # 符号链接，不统一就会出现"同一个目录两种写法、策略判成越界"。
    return raw.resolve()


def _normalize_env_name(item: object) -> str:
    if not isinstance(item, str) or not item:
        raise ValueError(f"env_allowlist 的每一项必须是非空字符串：{item!r}")
    if "\x00" in item or "=" in item:
        raise ValueError(f"env_allowlist 的项不能包含 NUL 或 '='：{item!r}")
    return item


def _normalize_boundaries(name: str, value: object) -> tuple[str, ...]:
    """把隔离要求规范成去重的边界名元组；非法名字在构造期就拒绝。"""
    items = _require_sequence_of_str(name, value)
    out: list[str] = []
    for item in items:
        if item not in ISOLATION_BOUNDARIES:
            raise ValueError(
                f"{name} 含有未知隔离边界：{item!r}"
                f"（可选：{list(ISOLATION_BOUNDARIES)}）"
            )
        if item not in out:
            out.append(item)
    return tuple(out)


def _require_limit(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数：{value!r}")
    if value < 0:
        raise ValueError(f"{name} 不能为负：{value}")
    # 0 是合法的**构造值**（表示"未声明"），但 check_spawn 会按"拒绝"处理。
    # 这样空策略可以构造出来并被拒绝，而不是构造时就炸。
    return value


@dataclass(frozen=True)
class ExecPolicy:
    """声明式执行策略。默认值即"全拒"。

    字段全部是"允许什么"或"上限是多少"，没有"关闭限制"的开关。空对象
    `ExecPolicy()` 拒绝任何 spawn：argv 白名单为空、cwd 根为空、上限未声明，
    三条独立规则各自都会拒绝。

    - `argv_allowlist`：允许的 argv[0]，**绝对路径精确匹配**（空 = 全拒）。
    - `argv_denylist`：显式拒绝的 argv[0]，**先于 allow 判定**（deny 优先）。
    - `cwd_roots`：允许的工作目录；构造时 resolve，判定时 cwd 也 resolve 后
      按祖先关系比较（空 = 全拒）。
    - `env_allowlist`：允许传给子进程的环境变量名（空 = 只传空 env）。
    - `max_output_bytes` / `timeout_ms`：必须显式声明且 > 0，否则拒绝。
    - `allow_pty`：是否请求 PTY；后端没有 PTY 时必须 `unsupported`。
    - `isolation_requires`：本次执行**要求实测证明**的隔离边界（PRD §8.1.1）。
      空 = 不声明隔离要求（执行按"受信任执行"如实标注）。非空时必须拿到
      `isolation.probe_backend()` 的实测报告，任何 `unknown` / `not_enforced`
      都拒绝——声明与"后端自称"都不算证据。
    """

    argv_allowlist: Sequence[str] = ()
    argv_denylist: Sequence[str] = ()
    cwd_roots: Sequence[Path] = ()
    env_allowlist: Sequence[str] = ()
    max_output_bytes: int = 0
    timeout_ms: int = 0
    allow_pty: bool = False
    isolation_requires: Sequence[str] = ()

    def to_dict(self) -> dict[str, Any]:
        """可持久化的声明形式（任务级策略存进 tasks 表用）。

        `cwd_roots` 存绝对路径字符串——读回来时 `ExecPolicy` 会再 resolve 一次，
        所以即使工作区根在两次运行之间换了挂载点，读回来的仍是 canonical 形式。
        """
        return {
            "argv_allowlist": list(self.argv_allowlist),
            "argv_denylist": list(self.argv_denylist),
            "cwd_roots": [str(p) for p in self.cwd_roots],
            "env_allowlist": list(self.env_allowlist),
            "max_output_bytes": self.max_output_bytes,
            "timeout_ms": self.timeout_ms,
            "allow_pty": self.allow_pty,
            "isolation_requires": list(self.isolation_requires),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> ExecPolicy | None:
        """从持久化形式读回；空/None 返回 None（= 没有配置 = 全拒）。"""
        if not raw:
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "argv_allowlist",
            tuple(
                _normalize_executable("argv_allowlist", item)
                for item in _require_sequence_of_str(
                    "argv_allowlist", self.argv_allowlist
                )
            ),
        )
        object.__setattr__(
            self,
            "argv_denylist",
            tuple(
                _normalize_executable("argv_denylist", item)
                for item in _require_sequence_of_str("argv_denylist", self.argv_denylist)
            ),
        )
        object.__setattr__(
            self,
            "cwd_roots",
            tuple(
                _normalize_root(item)
                for item in _require_sequence_of_str("cwd_roots", self.cwd_roots)
            ),
        )
        object.__setattr__(
            self,
            "env_allowlist",
            tuple(
                _normalize_env_name(item)
                for item in _require_sequence_of_str("env_allowlist", self.env_allowlist)
            ),
        )
        object.__setattr__(
            self, "max_output_bytes", _require_limit("max_output_bytes", self.max_output_bytes)
        )
        object.__setattr__(self, "timeout_ms", _require_limit("timeout_ms", self.timeout_ms))
        if not isinstance(self.allow_pty, bool):
            raise ValueError(f"allow_pty 必须是布尔值：{self.allow_pty!r}")
        object.__setattr__(
            self,
            "isolation_requires",
            _normalize_boundaries("isolation_requires", self.isolation_requires),
        )


@dataclass(frozen=True)
class ExecBackendCapabilities:
    """后端能力声明。**未声明 = 不支持**（fail closed）。

    `isolation` 必须是后端**如实**声明的级别（见 `ISOLATION_*`）：macOS v1 只能
    声明 `path_only`，标成更强级别就是在 UI 上撒谎（TECH-ARCH §6 的教训）。
    """

    name: str
    isolation: str
    supports_pty: bool = False
    supports_signals: bool = False
    supports_process_group: bool = False
    supports_job_objects: bool = False
    supports_network_isolation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"后端 name 必须是非空字符串：{self.name!r}")
        if not isinstance(self.isolation, str) or not self.isolation:
            raise ValueError(f"isolation 必须是非空字符串：{self.isolation!r}")
        if self.isolation not in ISOLATION_LEVELS:
            raise ValueError(
                f"未知隔离级别：{self.isolation!r}（可选：{list(ISOLATION_LEVELS)}）"
                "—— 后端自述级别属于封闭集，输出契约与能力声明必须同一套枚举"
            )
        for attr in _FEATURE_ATTRS.values():
            value = getattr(self, attr)
            if not isinstance(value, bool):
                raise ValueError(f"{attr} 必须是布尔值：{value!r}")

    def supports(self, feature: str) -> bool:
        try:
            attr = _FEATURE_ATTRS[feature]
        except KeyError:
            raise ValueError(
                f"未知能力：{feature!r}（可选：{sorted(_FEATURE_ATTRS)}）"
            ) from None
        return bool(getattr(self, attr))

    def require(self, feature: str) -> None:
        """要求后端具备某能力；没有就 `unsupported`，绝不降级。"""
        if not self.supports(feature):
            raise PolicyUnsupported(
                "后端没有声明该能力；v1 不允许降级成「不隔离但能跑」",
                backend=self.name,
                feature=feature,
            )


def check_spawn(
    policy: ExecPolicy,
    argv: Sequence[str],
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
) -> None:
    """判定一次 spawn 在策略上是否允许。不通过就抛 `PolicyDenied`。

    **不创建进程**（那是 F1）。判定顺序：argv（deny 先于 allow）→ cwd →
    上限 → env。任何一条不通过都立即抛，后面的规则不会把结论翻回来。

    `env` 必须是**已经过滤过**的环境（用 `filtered_env()` 产出）。直接传
    `os.environ` 会被拒：把原始环境交给子进程等于把凭据一起带进去。
    """
    if not isinstance(policy, ExecPolicy):
        raise TypeError(f"check_spawn 需要 ExecPolicy，收到：{type(policy).__name__}")
    _check_argv(policy, argv)
    _check_cwd(policy, cwd)
    _check_limits(policy)
    _check_env(policy, env)


def filtered_env(policy: ExecPolicy, env: Mapping[str, str]) -> dict[str, str]:
    """按 `env_allowlist` 过滤环境变量；未列出的变量不会出现在返回值里。"""
    if not isinstance(policy, ExecPolicy):
        raise TypeError(f"filtered_env 需要 ExecPolicy，收到：{type(policy).__name__}")
    if not isinstance(env, Mapping):
        raise _deny(
            f"env 必须是映射（name → value），收到：{type(env).__name__}",
            RULE_ENV_NOT_MAPPING,
        )
    allowed = set(policy.env_allowlist)
    out: dict[str, str] = {}
    for key, value in env.items():
        _validate_env_pair(key, value)
        if key in allowed:
            out[key] = value
    return out


def check_capabilities(
    policy: ExecPolicy, capabilities: ExecBackendCapabilities
) -> None:
    """判定后端能不能按策略请求的方式执行。做不到就 `PolicyUnsupported`。

    目前策略里唯一会"请求某种执行方式"的字段是 `allow_pty`。其余能力
    （信号、进程组/Job Object、网络隔离）由 F1 用 `capabilities.require()`
    按需检查——它们不是策略授予的权限，而是后端的事实。
    """
    if not isinstance(policy, ExecPolicy):
        raise TypeError(
            f"check_capabilities 需要 ExecPolicy，收到：{type(policy).__name__}"
        )
    if not isinstance(capabilities, ExecBackendCapabilities):
        raise TypeError(
            "check_capabilities 需要 ExecBackendCapabilities，"
            f"收到：{type(capabilities).__name__}"
        )
    if policy.allow_pty and not capabilities.supports_pty:
        raise PolicyUnsupported(
            "策略请求了 PTY（allow_pty=True），但后端没有 PTY 支持；"
            "不会静默降级成管道，因为那会改变交互语义与信号行为",
            backend=capabilities.name,
            feature=FEATURE_PTY,
        )


def check_exec(
    policy: ExecPolicy,
    capabilities: ExecBackendCapabilities,
    argv: Sequence[str],
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
) -> None:
    """F1 的推荐入口：策略判定 + 能力判定一次做完。

    分成两个函数是为了让"策略拒绝"和"后端做不到"在调用点就可区分；合成
    一个入口是为了让 F1 不会只做前者、忘了后者（忘了的后果是 pty 请求被
    静默降级）。
    """
    check_spawn(policy, argv, cwd, env)
    check_capabilities(policy, capabilities)


# --------------------------------------------------------------- 内部判定


def _check_argv(policy: ExecPolicy, argv: object) -> None:
    if isinstance(argv, (str, bytes)):
        raise _deny(
            "argv 必须是数组；字符串命令会被 shell 解释，等于绕过整条策略",
            RULE_ARGV_NOT_ARRAY,
        )
    if not isinstance(argv, Sequence):
        raise _deny(
            f"argv 必须是数组，收到：{type(argv).__name__}", RULE_ARGV_NOT_ARRAY
        )
    if not argv:
        raise _deny("argv 不能为空：至少要有可执行文件路径", RULE_ARGV_EMPTY)

    for index, item in enumerate(argv):
        if not isinstance(item, str):
            raise _deny(
                f"argv[{index}] 必须是字符串，收到：{type(item).__name__}",
                RULE_ARGV_NOT_STR,
            )
        if not item:
            raise _deny(f"argv[{index}] 不能为空字符串", RULE_ARGV_EMPTY)
        if "\x00" in item:
            raise _deny(f"argv[{index}] 不能包含 NUL 字节", RULE_ARGV_NUL)

    executable = os.path.normpath(argv[0])
    if not os.path.isabs(executable):
        raise _deny(
            f"argv[0] 必须是绝对路径：{argv[0]!r}"
            "（不做 PATH 查找：PATH 可被注入，同名二进制可被替换）",
            RULE_ARGV_NOT_ABSOLUTE,
        )

    # deny 优先：先判拒绝清单，命中就不再看 allowlist。
    if executable in policy.argv_denylist:
        raise _deny(
            f"argv[0] 命中 argv_denylist：{executable}"
            "（deny 优先：其他规则不能把它翻回来）",
            RULE_ARGV_DENIED,
        )

    if not policy.argv_allowlist:
        raise _deny(
            "argv_allowlist 为空 = 全拒；要放行必须显式声明可执行文件的绝对路径",
            RULE_ARGV_NOT_ALLOWED,
        )
    if executable not in policy.argv_allowlist:
        raise _deny(
            f"argv[0] 不在 argv_allowlist 中：{executable}"
            f"（允许：{list(policy.argv_allowlist)}）",
            RULE_ARGV_NOT_ALLOWED,
        )


def _check_cwd(policy: ExecPolicy, cwd: object) -> None:
    if not isinstance(cwd, (str, os.PathLike)):
        raise _deny(
            f"cwd 必须是路径，收到：{type(cwd).__name__}", RULE_CWD_NOT_ABSOLUTE
        )
    raw = Path(cwd)
    if not raw.is_absolute():
        raise _deny(
            f"cwd 必须是绝对路径：{cwd!r}（相对路径取决于宿主进程的当前目录）",
            RULE_CWD_NOT_ABSOLUTE,
        )
    resolved = raw.resolve()

    if not policy.cwd_roots:
        raise _deny(
            "cwd_roots 为空 = 全拒；要放行必须显式声明允许的工作目录",
            RULE_CWD_OUTSIDE_ROOTS,
        )
    target = overlap_key(resolved)
    for root in policy.cwd_roots:
        if is_ancestor_or_self(overlap_key(root), target):
            return
    raise _deny(
        f"cwd 不在任何 cwd_roots 内：{resolved}"
        f"（允许：{[str(root) for root in policy.cwd_roots]}）",
        RULE_CWD_OUTSIDE_ROOTS,
    )


def _check_limits(policy: ExecPolicy) -> None:
    if policy.timeout_ms <= 0:
        raise _deny(
            "timeout_ms 未声明（必须 > 0）：没有运行时长上限的 spawn 不允许执行",
            RULE_TIMEOUT_NOT_DECLARED,
        )
    if policy.max_output_bytes <= 0:
        raise _deny(
            "max_output_bytes 未声明（必须 > 0）：没有输出上限的 spawn 不允许执行",
            RULE_OUTPUT_LIMIT_NOT_DECLARED,
        )


def _validate_env_pair(key: object, value: object) -> None:
    if not isinstance(key, str) or not key:
        raise _deny(f"env 的键必须是非空字符串：{key!r}", RULE_ENV_INVALID)
    if not isinstance(value, str):
        raise _deny(
            f"env[{key!r}] 必须是字符串，收到：{type(value).__name__}", RULE_ENV_INVALID
        )
    if "=" in key or "\x00" in key:
        raise _deny(f"env 的键不能包含 '=' 或 NUL：{key!r}", RULE_ENV_INVALID)
    if "\x00" in value:
        raise _deny(f"env[{key!r}] 的值不能包含 NUL 字节", RULE_ENV_INVALID)


def _check_env(policy: ExecPolicy, env: object) -> None:
    if not isinstance(env, Mapping):
        raise _deny(
            f"env 必须是映射（name → value），收到：{type(env).__name__}",
            RULE_ENV_NOT_MAPPING,
        )
    allowed = set(policy.env_allowlist)
    extra: list[str] = []
    for key, value in env.items():
        _validate_env_pair(key, value)
        if key not in allowed:
            extra.append(key)
    if extra:
        raise _deny(
            f"env 含未在白名单中的变量：{sorted(extra)}"
            "（先用 filtered_env() 过滤；直接把原始环境交给子进程会连凭据一起带走）",
            RULE_ENV_NOT_FILTERED,
        )


__all__ = [
    "FEATURE_JOB_OBJECTS",
    "FEATURE_NETWORK_ISOLATION",
    "FEATURE_PROCESS_GROUP",
    "FEATURE_PTY",
    "FEATURE_SIGNALS",
    "ISOLATION_APP_CONTAINER",
    "ISOLATION_BOUNDARIES",
    "ISOLATION_BOUNDARY_CREDENTIALS",
    "ISOLATION_BOUNDARY_FILES",
    "ISOLATION_BOUNDARY_NETWORK",
    "ISOLATION_BOUNDARY_PROCESS_TREE",
    "ISOLATION_CONTAINER",
    "ISOLATION_LEVELS",
    "ISOLATION_NONE",
    "ISOLATION_PATH_ONLY",
    "ISOLATION_PROCESS_TREE",
    "RECOMMENDED_ISOLATION",
    "RULE_ARGV_DENIED",
    "RULE_ARGV_EMPTY",
    "RULE_ARGV_NOT_ABSOLUTE",
    "RULE_ARGV_NOT_ALLOWED",
    "RULE_ARGV_NOT_ARRAY",
    "RULE_ARGV_NOT_STR",
    "RULE_ARGV_NUL",
    "RULE_CWD_NOT_ABSOLUTE",
    "RULE_CWD_OUTSIDE_ROOTS",
    "RULE_ENV_INVALID",
    "RULE_ENV_NOT_FILTERED",
    "RULE_ENV_NOT_MAPPING",
    "RULE_OUTPUT_LIMIT_NOT_DECLARED",
    "RULE_TIMEOUT_NOT_DECLARED",
    "ExecBackendCapabilities",
    "ExecPolicy",
    "PolicyDenied",
    "PolicyUnsupported",
    "check_capabilities",
    "check_exec",
    "check_spawn",
    "filtered_env",
]
