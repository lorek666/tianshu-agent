"""资源种类解析的契约测试（`src/tianshu/resources.py`）。

这一层是记忆接入派发路径的地基，判错的后果不是"少个功能"：

- 把一条历史 `resources` 里的绝对路径读成 scheme → 历史请求无法重放；
- 把 `memory://../..` 读成文件路径 → 一次真实的越界文件访问。

所以这里断言的都是**判据顺序**，不是"大概能用"。
"""

from __future__ import annotations

import os

import pytest

from tianshu.resources import (
    KIND_FILE,
    KIND_MEMORY,
    MEMORY_SCOPES,
    RESOURCE_KINDS,
    MemoryScope,
    ResourceError,
    ResourceRef,
    is_memory_resource,
    memory_uri,
    parse_memory_uri,
    parse_resource,
    resource_uri,
)

# ---------------------------------------------------------------- 封闭集


def test_resource_kinds_are_closed():
    """封闭集：file / memory / network / mcp / subtask。

    口径变更（G1d）：旧判据是 `{file, memory}` 两种。PRD §5.2.3 要求「文件、网络目标、
    MCP 工具、记忆作用域、子任务各自规范化与校验」，所以三种新种类必须进集合；
    "封闭"这条性质**不变**——不在集合里的种类照旧拒绝（见下一条）。
    """
    assert RESOURCE_KINDS == frozenset(
        {KIND_FILE, KIND_MEMORY, "network", "mcp", "subtask"}
    )


def test_memory_scopes_are_closed():
    assert MEMORY_SCOPES == frozenset({"user", "workspace", "task"})


def test_unknown_kind_rejected():
    with pytest.raises(ResourceError):
        ResourceRef("quantum", "q://x")
    # 已知种类但**非规范形状**同样拒绝：写进 `resources` 的值会进审批摘要，
    # 允许两种写法等于让同一个目标有两种摘要。
    with pytest.raises(ResourceError):
        ResourceRef("network", "http://example.com")


# ---------------------------------------------------------------- file：逐字节不变


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/a.txt",
        "/Volumes/Date/天枢Agent/docs/PRD-v1.md",
        "/a/b/c",
    ],
)
def test_absolute_path_is_file_and_byte_identical(path: str):
    ref = parse_resource(path)
    assert ref.kind == KIND_FILE
    # 关键：回写后的字符串必须与输入**逐字节相同**。
    # 历史上每一条 `resources` 都是裸绝对路径，摘要比对要求它不变。
    assert ref.uri == path
    assert resource_uri(path) == path


def test_absolute_path_with_double_colon_slash_is_still_file():
    """`://` 可以是合法文件名的一部分，绝对路径必须优先判定。

    POSIX 上 `:` 与 `/` 都不是非法字符，`/Users/x/a://b` 是一条真实可用的路径。
    如果把"含 `://` 就当 scheme"放在前面，这条历史资源会被读成未知 scheme 而
    拒绝——历史请求就无法重放了。
    """
    weird = "/Users/x/a://b"
    assert os.path.isabs(weird)
    ref = parse_resource(weird)
    assert ref.kind == KIND_FILE
    assert ref.uri == weird


@pytest.mark.parametrize("raw", ["relative/path", "a.txt", "./x", ""])
def test_relative_or_empty_rejected(raw: str):
    with pytest.raises(ResourceError):
        parse_resource(raw)


@pytest.mark.parametrize("raw", [None, 0, 1.5, [], {}])
def test_non_string_rejected(raw: object):
    with pytest.raises(ResourceError):
        parse_resource(raw)


@pytest.mark.parametrize("raw", ["file:///etc/passwd", "http://evil", "https://x", "ftp://x"])
def test_unknown_scheme_rejected(raw: str):
    with pytest.raises(ResourceError):
        parse_resource(raw)


# ---------------------------------------------------------------- memory：URI 往返


def test_user_scope_uri():
    scope = MemoryScope.user()
    assert scope.to_uri() == "memory://user/local"
    assert parse_memory_uri("memory://user/local") == scope
    assert parse_resource("memory://user/local").kind == KIND_MEMORY


def test_task_scope_uri():
    scope = MemoryScope.task("task_abc123")
    assert scope.to_uri() == "memory://task/task_abc123"
    assert parse_memory_uri(scope.to_uri()) == scope


def test_workspace_scope_is_canonicalized():
    """`/a/b`、`/a/./b`、`/a/../a/b` 是同一个目录，必须算同一个作用域。

    否则用户会在三个"不同"的 workspace 记忆里找不到十分钟前存的东西——
    而这三个其实是同一个地方。
    """
    a = MemoryScope.workspace("/Volumes/Date/proj")
    b = MemoryScope.workspace("/Volumes/Date/proj/./")
    c = MemoryScope.workspace("/Volumes/Date/proj/../proj")
    assert a == b == c


def test_workspace_uri_percent_encodes_and_round_trips():
    root = "/Volumes/Date/天枢Agent"
    uri = memory_uri(MemoryScope.workspace(root))
    # 斜杠必须被编码，否则 URI 的段边界会被路径吃掉
    assert "/Volumes" not in uri
    assert "%2F" in uri
    assert parse_memory_uri(uri) == MemoryScope.workspace(root)


def test_workspace_scope_rejects_relative_path():
    with pytest.raises(ResourceError):
        MemoryScope("workspace", "relative/dir")


def test_user_scope_id_must_be_local():
    """D2 单用户：`user` 作用域没有第二个用户可言。"""
    with pytest.raises(ResourceError):
        MemoryScope("user", "root")
    with pytest.raises(ResourceError):
        parse_memory_uri("memory://user/root")


@pytest.mark.parametrize("scope_id", ["", "a/b", "x" * 129, "bad id!", "a\u0000b"])
def test_task_scope_id_shape(scope_id: str):
    with pytest.raises(ResourceError):
        MemoryScope("task", scope_id)


def test_unknown_memory_scope_rejected():
    with pytest.raises(ResourceError):
        MemoryScope("team", "x")
    with pytest.raises(ResourceError):
        parse_memory_uri("memory://team/x")


@pytest.mark.parametrize(
    "raw",
    [
        "memory://",
        "memory://user",
        "memory://user/",
        "memory://workspace/",
        "memory://workspace",  # 缺分隔符
    ],
)
def test_malformed_memory_uri_rejected(raw: str):
    with pytest.raises(ResourceError):
        parse_memory_uri(raw)


def test_non_memory_uri_rejected_by_memory_parser():
    with pytest.raises(ResourceError):
        parse_memory_uri("/tmp/x")  # 不是记忆引用
    with pytest.raises(ResourceError):
        parse_memory_uri("memory:/user/local")  # 单斜杠


# ---------------------------------------------------------------- 不能互相冒充


def test_memory_uri_never_becomes_a_file_path():
    """记忆引用绝不能被当成文件路径。

    这是 F10 明确拒绝的形状：把记忆塞进一个真实目录，等于让"删掉投影"变成
    "删掉用户文件"。这里断言的是"它永远走 memory 分支"，不是"它解析失败"。
    """
    ref = parse_resource("memory://user/local")
    assert ref.kind == KIND_MEMORY
    with pytest.raises(ResourceError):
        _ = ResourceRef(KIND_MEMORY, "/tmp/x").memory_scope


def test_memory_scope_rejects_file_resource():
    with pytest.raises(ResourceError):
        _ = parse_resource("/tmp/x").memory_scope


def test_is_memory_resource():
    assert is_memory_resource("memory://user/local") is True
    assert is_memory_resource("/tmp/x") is False
    with pytest.raises(ResourceError):
        is_memory_resource("http://x")


def test_resource_ref_normalizes_memory_uri():
    """同一作用域的不同写法必须归一成同一个 URI。

    `%2f` 与 `%2F` 是同一个字节，`/a/./b` 与 `/a/b` 是同一个目录——归一化之后
    它们才能被当成同一条记忆，而不是三条。
    """
    a = ResourceRef.memory(MemoryScope.workspace("/Volumes/Date/proj"))
    b = parse_resource("memory://workspace/%2FVolumes%2FDate%2Fproj%2F.")
    assert a == b


def test_relative_workspace_scope_is_rejected_before_realpath():
    """相对路径必须**拒绝**，不能被 `realpath` 按当前工作目录补成绝对路径。

    判据是"workspace 作用域必须是绝对路径"。先 realpath 就把这条判据换成了
    "取决于进程在哪启动"：同一个 URI 在 A 目录下指向 `A/notes`、在 B 目录下
    指向 `B/notes`——一条相对引用静默变成合法作用域，fail-closed 就没了。
    """
    with pytest.raises(ResourceError):
        parse_memory_uri("memory://workspace/notes")
    with pytest.raises(ResourceError):
        MemoryScope.workspace("notes")