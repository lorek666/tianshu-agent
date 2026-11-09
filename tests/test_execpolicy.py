"""F2 验收：声明式 ExecPolicy、默认全拒、deny 优先、unsupported 不降级。

要钉死的是四条最容易被做软的事：

1. **空策略 = 全拒。** 不是"没限制所以放行"，也不是"默认放行后靠 deny 兜"——
   后者是沙箱最常见的失手方式。
2. **deny 优先。** 同一条命令同时命中 allow 与 deny 时必须拒；否则策略里
   拼错一条 allow 就等于全开。
3. **unsupported 不被降级。** 后端没有 PTY 时，请求 PTY 必须报错，而不是
   悄悄用管道顶上——那会把"做不到"变成一次静默放宽。
4. **deny 与 unsupported 是两种错。** 一个是"策略不允许"（改策略/找人批），
   一个是"这台机器做不到"（换后端/换平台）。合并成一个，UI 就只能给用户
   一句含糊的"失败了"。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

import tianshu.execpolicy as execpolicy_module
from tianshu.errors import TianshuError
from tianshu.execpolicy import (
    FEATURE_JOB_OBJECTS,
    FEATURE_PTY,
    FEATURE_SIGNALS,
    ISOLATION_PATH_ONLY,
    ISOLATION_PROCESS_TREE,
    RULE_ARGV_DENIED,
    RULE_ARGV_EMPTY,
    RULE_ARGV_NOT_ABSOLUTE,
    RULE_ARGV_NOT_ALLOWED,
    RULE_ARGV_NOT_ARRAY,
    RULE_ARGV_NOT_STR,
    RULE_ARGV_NUL,
    RULE_CWD_NOT_ABSOLUTE,
    RULE_CWD_OUTSIDE_ROOTS,
    RULE_ENV_INVALID,
    RULE_ENV_NOT_FILTERED,
    RULE_ENV_NOT_MAPPING,
    RULE_OUTPUT_LIMIT_NOT_DECLARED,
    RULE_TIMEOUT_NOT_DECLARED,
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    check_capabilities,
    check_exec,
    check_spawn,
    filtered_env,
)

EXE = "/usr/bin/echo"
ENV_OK = {"PATH": "/usr/bin:/bin"}


# ---------------------------------------------------------------- 夹具


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws.resolve()


def _policy(ws: Path, **overrides) -> ExecPolicy:
    base = dict(
        argv_allowlist=(EXE,),
        cwd_roots=(ws,),
        env_allowlist=("PATH",),
        max_output_bytes=64 * 1024,
        timeout_ms=5_000,
    )
    base.update(overrides)
    return ExecPolicy(**base)


def _argv(*extra: str) -> list[str]:
    return [EXE, *extra]


def _denied(policy: ExecPolicy, argv, cwd, env) -> PolicyDenied:
    with pytest.raises(PolicyDenied) as excinfo:
        check_spawn(policy, argv, cwd, env)
    return excinfo.value


# ---------------------------------------------------------------- 默认全拒


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "id"],
        ["/usr/bin/echo", "hi"],
        ["/usr/bin/python3", "-V"],
    ],
)
def test_empty_policy_denies_every_spawn(tmp_path, argv):
    """空策略必须拒绝任何 spawn——默认安全只能靠 deny by default。"""
    ws = _ws(tmp_path)
    denied = _denied(ExecPolicy(), argv, ws, {})
    assert denied.rule_id == RULE_ARGV_NOT_ALLOWED


def test_empty_policy_denies_even_with_clean_env(tmp_path):
    ws = _ws(tmp_path)
    denied = _denied(ExecPolicy(), _argv(), ws, ENV_OK)
    assert denied.rule_id == RULE_ARGV_NOT_ALLOWED


def test_policy_without_limits_still_denies(tmp_path):
    """argv 与 cwd 都声明了，但上限没声明 → 仍然拒。"""
    ws = _ws(tmp_path)
    policy = ExecPolicy(argv_allowlist=(EXE,), cwd_roots=(ws,))
    denied = _denied(policy, _argv(), ws, {})
    assert denied.rule_id == RULE_TIMEOUT_NOT_DECLARED


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"timeout_ms": 0}, RULE_TIMEOUT_NOT_DECLARED),
        ({"max_output_bytes": 0}, RULE_OUTPUT_LIMIT_NOT_DECLARED),
    ],
)
def test_missing_limits_are_denied(tmp_path, overrides, rule):
    ws = _ws(tmp_path)
    policy = _policy(ws, **overrides)
    denied = _denied(policy, _argv(), ws, ENV_OK)
    assert denied.rule_id == rule


# ---------------------------------------------------------------- deny 优先


def test_deny_wins_over_allow(tmp_path):
    """同一条 argv[0] 同时命中 allow 与 deny：结论必须是拒。"""
    ws = _ws(tmp_path)
    policy = _policy(ws, argv_denylist=(EXE,))
    denied = _denied(policy, _argv("hi"), ws, ENV_OK)
    assert denied.rule_id == RULE_ARGV_DENIED
    assert "deny" in str(denied)


def test_deny_beats_allow_even_with_everything_else_valid(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws, argv_denylist=(EXE,))
    # 其它条件全部满足，唯一的作用来自 deny：它必须照样拒。
    with pytest.raises(PolicyDenied) as excinfo:
        check_spawn(policy, _argv(), ws, ENV_OK)
    assert excinfo.value.rule_id == RULE_ARGV_DENIED


def test_allowlisted_executable_passes(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws)
    assert check_spawn(policy, _argv("hi"), ws, ENV_OK) is None


def test_executable_outside_allowlist_is_denied(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws)
    denied = _denied(policy, ["/usr/bin/id"], ws, ENV_OK)
    assert denied.rule_id == RULE_ARGV_NOT_ALLOWED


# ---------------------------------------------------------------- argv 形状


@pytest.mark.parametrize(
    ("argv", "rule"),
    [
        ("/usr/bin/echo hi", RULE_ARGV_NOT_ARRAY),
        (b"/usr/bin/echo", RULE_ARGV_NOT_ARRAY),
        ({"argv": [EXE]}, RULE_ARGV_NOT_ARRAY),
        ([], RULE_ARGV_EMPTY),
        ([EXE, 1], RULE_ARGV_NOT_STR),
        ([EXE, ""], RULE_ARGV_EMPTY),
        ([EXE, "a\x00b"], RULE_ARGV_NUL),
        (["echo"], RULE_ARGV_NOT_ABSOLUTE),
    ],
)
def test_argv_shape_rules(tmp_path, argv, rule):
    ws = _ws(tmp_path)
    policy = _policy(ws)
    denied = _denied(policy, argv, ws, ENV_OK)
    assert denied.rule_id == rule


def test_string_command_is_rejected_not_split(tmp_path):
    """`"echo hi"` 不能当命令跑：拆词等于把 shell 解释权交回去。"""
    ws = _ws(tmp_path)
    denied = _denied(_policy(ws), "/usr/bin/echo hi", ws, ENV_OK)
    assert denied.rule_id == RULE_ARGV_NOT_ARRAY
    assert "shell" in str(denied)


# ---------------------------------------------------------------- cwd


def test_cwd_outside_roots_is_denied(tmp_path):
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    denied = _denied(_policy(ws), _argv(), outside, ENV_OK)
    assert denied.rule_id == RULE_CWD_OUTSIDE_ROOTS
    assert str(outside.resolve()) in str(denied)


def test_cwd_relative_is_denied(tmp_path):
    ws = _ws(tmp_path)
    denied = _denied(_policy(ws), _argv(), "ws", ENV_OK)
    assert denied.rule_id == RULE_CWD_NOT_ABSOLUTE


def test_cwd_empty_roots_is_denied(tmp_path):
    ws = _ws(tmp_path)
    denied = _denied(_policy(ws, cwd_roots=()), _argv(), ws, ENV_OK)
    assert denied.rule_id == RULE_CWD_OUTSIDE_ROOTS


def test_cwd_nested_inside_root_passes(tmp_path):
    ws = _ws(tmp_path)
    sub = ws / "sub"
    sub.mkdir()
    assert check_spawn(_policy(ws), _argv(), sub, ENV_OK) is None


def test_symlinked_cwd_escape_is_denied(tmp_path):
    """策略根走 resolve，cwd 也走 resolve：符号链接逃逸在比较前就被消解。"""
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "link").symlink_to(outside)

    denied = _denied(_policy(ws), _argv(), ws / "link", ENV_OK)
    assert denied.rule_id == RULE_CWD_OUTSIDE_ROOTS


# ---------------------------------------------------------------- env


def test_filtered_env_drops_unlisted_variables(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws)
    env = {"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "leak", "HOME": "/root"}

    assert filtered_env(policy, env) == {"PATH": "/usr/bin"}


def test_unfiltered_env_is_denied(tmp_path):
    """直接传原始环境会被拒：不能把凭据一起交给子进程。"""
    ws = _ws(tmp_path)
    env = {"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "leak"}
    denied = _denied(_policy(ws), _argv(), ws, env)
    assert denied.rule_id == RULE_ENV_NOT_FILTERED
    assert "AWS_SECRET_ACCESS_KEY" in str(denied)


def test_filtered_env_then_spawn_passes(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws)
    raw = {"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "leak"}
    assert check_spawn(policy, _argv(), ws, filtered_env(policy, raw)) is None


@pytest.mark.parametrize(
    "env",
    [
        {"PATH": 1},
        {"PA=TH": "/usr/bin"},
        {"PATH": "a\x00b"},
    ],
)
def test_invalid_env_values_are_denied(tmp_path, env):
    ws = _ws(tmp_path)
    denied = _denied(_policy(ws), _argv(), ws, env)
    assert denied.rule_id == RULE_ENV_INVALID


def test_env_must_be_a_mapping(tmp_path):
    ws = _ws(tmp_path)
    denied = _denied(_policy(ws), _argv(), ws, ["PATH=/usr/bin"])
    assert denied.rule_id == RULE_ENV_NOT_MAPPING


# ---------------------------------------------------------------- unsupported


def test_pty_request_on_backend_without_pty_is_unsupported(tmp_path):
    """后端没有 PTY 时请求 PTY 必须 `unsupported`，不能降级成管道。"""
    ws = _ws(tmp_path)
    policy = _policy(ws, allow_pty=True)
    backend = ExecBackendCapabilities(
        name="macos-path-only", isolation=ISOLATION_PATH_ONLY
    )

    # 策略本身是允许的：失败只能来自能力层
    assert check_spawn(policy, _argv(), ws, ENV_OK) is None

    with pytest.raises(PolicyUnsupported) as excinfo:
        check_exec(policy, backend, _argv(), ws, ENV_OK)
    assert excinfo.value.backend == "macos-path-only"
    assert excinfo.value.feature == FEATURE_PTY
    assert not isinstance(excinfo.value, PolicyDenied)


def test_pty_request_on_capable_backend_passes(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws, allow_pty=True)
    backend = ExecBackendCapabilities(
        name="local-full",
        isolation=ISOLATION_PROCESS_TREE,
        supports_pty=True,
        supports_signals=True,
    )
    assert check_exec(policy, backend, _argv(), ws, ENV_OK) is None
    assert check_capabilities(policy, backend) is None


def test_no_pty_request_does_not_need_pty_capability(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws)  # allow_pty 默认 False
    backend = ExecBackendCapabilities(name="bare", isolation=ISOLATION_PATH_ONLY)
    assert check_exec(policy, backend, _argv(), ws, ENV_OK) is None


def test_capabilities_default_to_unsupported(tmp_path):
    """未声明 = 不支持：能力声明也必须 fail closed。"""
    backend = ExecBackendCapabilities(name="bare", isolation=ISOLATION_PATH_ONLY)
    assert backend.supports_pty is False
    assert backend.supports_signals is False
    assert backend.supports_job_objects is False
    with pytest.raises(PolicyUnsupported) as excinfo:
        backend.require(FEATURE_SIGNALS)
    assert excinfo.value.feature == FEATURE_SIGNALS


def test_require_supported_capability_passes():
    backend = ExecBackendCapabilities(
        name="win", isolation=ISOLATION_PROCESS_TREE, supports_job_objects=True
    )
    assert backend.require(FEATURE_JOB_OBJECTS) is None


def test_unknown_capability_name_is_a_programming_error():
    backend = ExecBackendCapabilities(name="x", isolation=ISOLATION_PATH_ONLY)
    with pytest.raises(ValueError):
        backend.require("teleport")


# ---------------------------------------------------------------- 两种错不合并


def test_denied_and_unsupported_are_distinct_error_types():
    denied = PolicyDenied("策略不允许", rule_id=RULE_ARGV_DENIED)
    unsupported = PolicyUnsupported("做不到", backend="b", feature=FEATURE_PTY)

    assert isinstance(denied, TianshuError)
    assert isinstance(unsupported, TianshuError)
    assert not isinstance(denied, PolicyUnsupported)
    assert not isinstance(unsupported, PolicyDenied)

    # 抓 deny 的 except 不能把 unsupported 一起吞掉
    with pytest.raises(PolicyUnsupported):
        try:
            raise unsupported
        except PolicyDenied:  # pragma: no cover - 不该进来
            raise AssertionError("unsupported 被当成 deny 捕获了") from None


def test_error_messages_name_the_rule_or_backend(tmp_path):
    ws = _ws(tmp_path)
    denied = _denied(_policy(ws, argv_denylist=(EXE,)), _argv(), ws, ENV_OK)
    assert RULE_ARGV_DENIED in str(denied)

    unsupported = PolicyUnsupported("没有 PTY", backend="b", feature=FEATURE_PTY)
    assert "b" in str(unsupported) and FEATURE_PTY in str(unsupported)
    assert "不是策略拒绝" in str(unsupported)


# ---------------------------------------------------------------- 声明校验


@pytest.mark.parametrize(
    "policy_kwargs",
    [
        {"argv_allowlist": EXE},  # 单个字符串，不是序列
        {"argv_allowlist": ("echo",)},  # 相对路径
        {"argv_denylist": ("/bin/sh", "/usr/bin/id")},  # 合法
        {"cwd_roots": ("rel",)},  # 相对根
        {"timeout_ms": -1},
        {"max_output_bytes": True},  # bool 不是合法整数
        {"allow_pty": "yes"},
        {"env_allowlist": ("A=B",)},
    ],
)
def test_invalid_policy_declarations_raise_value_error(policy_kwargs):
    if policy_kwargs == {"argv_denylist": ("/bin/sh", "/usr/bin/id")}:
        # 这一项是合法的：确保校验不会误伤正常声明
        assert ExecPolicy(**policy_kwargs).argv_denylist == (
            "/bin/sh",
            "/usr/bin/id",
        )
        return
    with pytest.raises(ValueError):
        ExecPolicy(**policy_kwargs)


def test_policy_normalizes_declared_paths(tmp_path):
    ws = _ws(tmp_path)
    messy_exe = str(Path(EXE).parent) + "//echo"
    messy_root = str(ws / "sub" / "..")
    policy = _policy(ws, argv_allowlist=(messy_exe,), cwd_roots=(messy_root,))

    assert policy.argv_allowlist == (EXE,)
    assert policy.cwd_roots == (ws,)
    assert check_spawn(policy, _argv(), ws, ENV_OK) is None


def test_policy_is_immutable(tmp_path):
    ws = _ws(tmp_path)
    policy = _policy(ws)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.timeout_ms = 1  # type: ignore[misc]


def test_backend_capabilities_validate_required_fields():
    with pytest.raises(ValueError):
        ExecBackendCapabilities(name="", isolation=ISOLATION_PATH_ONLY)
    with pytest.raises(ValueError):
        ExecBackendCapabilities(name="x", isolation="")
    with pytest.raises(ValueError):
        ExecBackendCapabilities(
            name="x", isolation=ISOLATION_PATH_ONLY, supports_pty="yes"  # type: ignore[arg-type]
        )


def test_check_functions_reject_wrong_types(tmp_path):
    ws = _ws(tmp_path)
    with pytest.raises(TypeError):
        check_spawn("policy", _argv(), ws, ENV_OK)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        check_capabilities(ExecPolicy(), "backend")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 没有 shell 路径


def test_module_never_builds_shell_commands():
    """F2 的 argv 只走数组；模块里不能出现 shell 解释或进程创建。

    用 AST 而不是字符串匹配：docstring 里会提到 `shell=True` 这个词（解释
    为什么不允许），子串匹配会误报。
    """
    tree = ast.parse(inspect.getsource(execpolicy_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not any(
                keyword.arg == "shell" for keyword in node.keywords
            ), "不允许任何 shell= 关键字参数"
        if isinstance(node, ast.Import):
            assert not any(alias.name == "subprocess" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {"subprocess", "os.spawn", "pty"}
    assert not hasattr(execpolicy_module, "subprocess")
    assert not hasattr(execpolicy_module, "pty")
