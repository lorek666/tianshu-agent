"""工具契约：同一套断言，两个后端各跑一遍。

这是 M3 的判据本体（与 `test_model_contract.py` 同构）：断言只落在**工具可见
的行为**上——返回形状、异常、写前证据顺序、快照条目、凭据字段、effect_state
的实际取值，不测"哪个分支走了"。

两处刻意的设计：

1. **C0 违约后端**：故意做坏的后端（写入静默丢弃 / 读取无视 limit）必须让
   同一套断言变红。没有这条，"全绿"只证明测试跑完了。
2. **只对 local 的断言单独成文件**（`test_sandbox_backend.py` 与本文末尾的
   skip）：崩溃语义、权限、mode、symlink 只有 local 有证据，混进来会得到
   "memfs 不满足"的假红，删掉又会丢掉 M1 的覆盖。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from support import effects_admission

from tianshu import sandbox
from tianshu.dispatcher import Dispatcher, build_request
from tianshu.errors import TianshuError, ToolNotAllowed, WorkspaceViolation
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces
from tianshu.jobs import JobStore
from tianshu.leases import LeaseLost, LeaseStore
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ApprovalStore, GrantStore, HumanIdentity, PolicyEngine
from tianshu.receipt import receipts_of, verify_receipts
from tianshu.sandbox import MemBackend
from tianshu.snapshot import ABSENT, SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.tools import (
    READ_TEXT_LIMIT,
    FileExecutor,
    ToolContext,
    ToolResult,
    execute,
)
from tianshu.workspace import Workspace

ORIGINAL = "原始内容\n"
BACKENDS = ["local_fs", "memfs"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 夹具


@dataclass
class RunCtx:
    task: Any
    run: Any
    claim: Any


@dataclass
class Env:
    tmp: Path
    backend: sandbox.FileBackend
    ws: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    def path(self, rel: str) -> Path:
        return Workspace(self.ws).resolve(rel)


def _build_env(
    tmp_path: Path, backend: sandbox.FileBackend, ws: Path, *, seed: bool = True
) -> Env:
    data = tmp_path / "authority"
    data.mkdir(exist_ok=True)
    log = EventLog(tmp_path / "events.db")
    leases = LeaseStore(log)
    claims = JobStore(log)
    snapshots = SnapshotStore(tmp_path / "artifacts")
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=data,
        disk_admission=effects_admission(log, snapshots=snapshots,
            memory=None, contents=None, data_root=data))
    approvals = ApprovalStore(log)
    grants = GrantStore(log)
    dispatcher = Dispatcher(
        log,
        tasks,
        leases,
        PolicyEngine(),
        approvals,
        grants=grants,
        claims=claims,
        snapshots=snapshots,
        data_root=data,
    )
    env = Env(
        tmp=tmp_path,
        backend=backend,
        ws=ws,
        log=log,
        leases=leases,
        claims=claims,
        tasks=tasks,
        approvals=approvals,
        grants=grants,
        snapshots=snapshots,
        dispatcher=dispatcher,
    )
    if seed:
        backend.write_bytes_atomic(env.path("note.md"), ORIGINAL.encode("utf-8"))
    return env


@pytest.fixture(params=BACKENDS)
def env(request, tmp_path):
    if request.param == "local_fs":
        backend = sandbox.configure_backend("local_fs")
        ws = tmp_path / "ws"
        ws.mkdir()
        ws = ws.resolve()
    else:
        backend = sandbox.configure_backend(MemBackend(namespace=tmp_path / "mem"))
        ws = (tmp_path / "mem" / "ws").resolve()
        backend.require_root(ws)
    built = _build_env(tmp_path, backend, ws)
    try:
        yield built
    finally:
        built.log.close()


def _new_run(env: Env) -> RunCtx:
    task = env.tasks.create_task("整理笔记", [{"root_path": str(env.ws)}])
    run = env.tasks.start_run(task.task_id)
    env.leases.acquire([env.ws], run.run_id)
    job = env.claims.enqueue(run.run_id)
    claim = env.claims.claim(job.job_id)
    env.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    env.snapshots.init_run(run.run_id)
    return RunCtx(task=task, run=run, claim=claim)


def _read(env: Env, tool_id: str, args: dict[str, Any]) -> ToolResult:
    """只读工具走主循环同一条路：`execute()` + 不带 backend 的 ToolContext。"""
    return execute(Workspace(env.ws), tool_id, args, ToolContext())


def _dispatch(
    env: Env,
    rc: RunCtx,
    tool_id: str,
    target: Path,
    args: dict[str, Any],
    *,
    call_id: str = "c1",
    approve: bool = True,
) -> tuple[Any, ToolResult | None]:
    request = build_request(
        task=rc.task,
        run=rc.run,
        claim=rc.claim,
        call_id=call_id,
        tool_id=tool_id,
        target=target,
        args=args,
    )
    decision = env.dispatcher.prepare(request, rc.claim, args=args)
    if decision.is_rejected:
        return decision, None
    if decision.is_waiting:
        if not approve:
            return decision, None
        env.approvals.decide(
            decision.approval_id,
            decision="approved",
            actor=HumanIdentity.local_cli(),
        )
    return decision, env.dispatcher.dispatch(decision.request_id, rc.claim)


def _mkdir(env: Env, rel: str) -> None:
    if env.backend.name == "local_fs":
        env.path(rel).mkdir(parents=True, exist_ok=True)
    else:
        env.backend.write_bytes_atomic(env.path(rel) / ".keep", b"")


# ---------------------------------------------------------------- C0 金丝雀
#
# 这两个函数本身体现契约；参数化用例和"违约后端"元测试共用它们，
# 这样元测试证明的是"同一套断言"，而不是另写一套更弱的检查。


def assert_create_roundtrip(env: Env) -> None:
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "create_report", env.path("canary.md"),
        {"name": "canary.md", "content": "canary\n"},
    )
    assert result is not None and result.status == "succeeded", result
    assert env.backend.exists(env.path("canary.md")), "后端声称写入成功，但文件不存在"
    assert env.backend.read_bytes(env.path("canary.md"))[0] == b"canary\n"


def assert_read_limit(env: Env) -> None:
    env.backend.write_bytes_atomic(
        env.path("canary-big.md"), b"a" * (READ_TEXT_LIMIT + 1)
    )
    result = _read(env, "read_text", {"path": "canary-big.md"})
    assert result.output["truncated"] is True, "超过 64KiB 必须标 truncated"
    assert result.output["bytes"] == READ_TEXT_LIMIT


def assert_backend_sees_directories(env: Env) -> None:
    """文件与目录必须被区分开：exists 看得见目录、is_file/is_dir 互斥。"""
    _mkdir(env, "d")
    env.backend.write_bytes_atomic(env.path("d/f.txt"), b"x")
    d, f = env.path("d"), env.path("d/f.txt")

    assert env.backend.exists(d) is True, "目录也是存在的对象：exists 不能只认文件"
    assert env.backend.is_dir(d) is True
    assert env.backend.is_file(d) is False, "目录不是文件"
    assert env.backend.is_file(f) is True
    assert env.backend.is_dir(f) is False, "文件不是目录"
    for p in (d, f):
        assert not (env.backend.is_file(p) and env.backend.is_dir(p)), (
            "is_file 与 is_dir 必须互斥"
        )
    try:
        env.backend.size(d)
    except OSError:
        pass
    else:
        raise AssertionError("目录没有 size；size() 只对普通文件有定义")


def assert_directory_entries_report_zero_size(env: Env) -> None:
    """目录项的 size 是契约值 0，不是平台相关的 st_size。"""
    _mkdir(env, "d")
    result = _read(env, "list_files", {"path": "."})
    dirs = [e for e in result.output["entries"] if e["is_dir"]]
    assert dirs, "测试前提：至少有一个目录条目"
    assert [e["size"] for e in dirs] == [0] * len(dirs), (
        "目录 size 跨平台没有一致含义，契约把它定成 0"
    )


# ---------------------------------------------------------------- 读语义


def test_create_roundtrip_is_satisfied_by_both_backends(env):
    assert_create_roundtrip(env)


def test_read_limit_is_satisfied_by_both_backends(env):
    assert_read_limit(env)


def test_backends_distinguish_files_from_directories(env):
    assert_backend_sees_directories(env)


def test_directory_entries_report_zero_size(env):
    assert_directory_entries_report_zero_size(env)


def test_read_reports_exact_64k_boundary(env):
    env.backend.write_bytes_atomic(env.path("exact.md"), b"a" * READ_TEXT_LIMIT)
    result = _read(env, "read_text", {"path": "exact.md"})

    assert result.status == "succeeded"
    assert result.effect_state == "none_applied"
    assert result.output["bytes"] == READ_TEXT_LIMIT
    assert result.output["total_bytes"] == READ_TEXT_LIMIT
    assert result.output["truncated"] is False
    assert len(result.output["content"]) == READ_TEXT_LIMIT


def test_read_truncates_one_byte_over_the_limit(env):
    env.backend.write_bytes_atomic(
        env.path("big.md"), b"a" * (READ_TEXT_LIMIT + 1)
    )
    result = _read(env, "read_text", {"path": "big.md"})

    assert result.output["truncated"] is True
    assert result.output["bytes"] == READ_TEXT_LIMIT
    assert result.output["total_bytes"] == READ_TEXT_LIMIT + 1


def test_read_backtracks_to_a_utf8_boundary(env):
    # 65535 字节 ASCII + 3 字节汉字：截断点正好落在汉字中间
    env.backend.write_bytes_atomic(
        env.path("cjk.md"), b"a" * (READ_TEXT_LIMIT - 1) + "中".encode()
    )
    result = _read(env, "read_text", {"path": "cjk.md"})

    assert result.status == "succeeded"
    # 回退一个字节后仍然是合法 UTF-8，且不超过上限
    assert result.output["content"].encode("utf-8") == b"a" * (READ_TEXT_LIMIT - 1)
    assert result.output["bytes"] == READ_TEXT_LIMIT
    assert result.output["truncated"] is True


def test_read_rejects_non_utf8_prefix(env):
    env.backend.write_bytes_atomic(env.path("bin.dat"), b"\xff\xfe\x00\x01")
    result = _read(env, "read_text", {"path": "bin.dat"})

    assert result.status == "failed"
    assert result.effect_state == "none_applied"
    assert "UTF-8" in result.output["error"]


def test_read_missing_or_directory_is_failed(env):
    missing = _read(env, "read_text", {"path": "nope.md"})
    as_dir = _read(env, "read_text", {"path": "."})

    for result in (missing, as_dir):
        assert result.status == "failed"
        assert result.effect_state == "none_applied"


def test_list_files_is_sorted_and_nfc_normalized(env):
    env.backend.write_bytes_atomic(env.path("b.md"), b"bb")
    env.backend.write_bytes_atomic(env.path("a.md"), b"a")
    env.backend.write_bytes_atomic(env.path("sub") / "keep.txt", b"")
    env.backend.write_bytes_atomic(env.path("e\u0301.md"), b"nfd")  # macOS 风格 NFD

    result = _read(env, "list_files", {"path": "."})

    names = [e["name"] for e in result.output["entries"]]
    assert [unicodedata.normalize("NFC", n) for n in names] == names
    assert names == sorted(names), "排序键必须是输出里看到的那个名字"
    assert names == ["a.md", "b.md", "note.md", "sub", "\u00e9.md"]
    by_name = {e["name"]: e for e in result.output["entries"]}
    assert by_name["sub"]["is_dir"] is True
    assert by_name["a.md"] == {"name": "a.md", "is_dir": False, "size": 1}


def test_list_files_missing_directory_is_failed(env):
    result = _read(env, "list_files", {"path": "nope"})
    assert result.status == "failed"
    assert result.effect_state == "none_applied"


# ---------------------------------------------------------------- 边界


def test_parent_reference_is_refused(env):
    with pytest.raises(WorkspaceViolation):
        _read(env, "read_text", {"path": "../secret.txt"})


def test_absolute_path_is_refused(env):
    with pytest.raises(WorkspaceViolation):
        _read(env, "read_text", {"path": "/etc/passwd"})


def test_workspace_root_follows_backend_rules(env):
    if env.backend.name == "local_fs":
        with pytest.raises(WorkspaceViolation):
            Workspace(env.tmp / "does-not-exist")
    else:
        real = env.tmp / "real-dir"
        real.mkdir()
        with pytest.raises(WorkspaceViolation):
            Workspace(real)


def test_symlink_escape_is_refused_on_local(env):
    if env.backend.name != "local_fs":
        pytest.skip("memfs 没有符号链接，不承诺这条")
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    (env.ws / "link").symlink_to(outside)

    with pytest.raises(WorkspaceViolation):
        _read(env, "read_text", {"path": "link/secret.txt"})


# ---------------------------------------------------------------- 写语义


def test_create_reports_the_documented_shape(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "create_report", env.path("a.md"),
        {"name": "a.md", "content": "hi\n"},
    )

    assert result.status == "succeeded"
    assert result.effect_state == "applied"
    assert set(result.output) == {
        "path", "bytes", "sha256", "after_state", "before_state",
    }
    assert result.output["path"] == "a.md"
    assert result.output["bytes"] == 3
    assert result.output["sha256"] == _sha("hi\n")
    assert result.output["after_state"] == _sha("hi\n")
    assert result.output["before_state"] == ABSENT


def test_create_over_existing_is_refused_not_approved(env):
    rc = _new_run(env)
    decision, result = _dispatch(
        env, rc, "create_report", env.path("note.md"),
        {"name": "note.md", "content": "覆盖？"},
    )

    assert decision.is_rejected
    assert decision.rule_id == "dispatch.create_over_existing"
    assert result is None, "被策略拒绝时不应该有派发结果"
    assert env.backend.read_bytes(env.path("note.md"))[0] == ORIGINAL.encode()


def test_modify_roundtrip_reports_before_and_after(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "modify_text", env.path("note.md"),
        {"path": "note.md", "content": "改后的内容\n"},
    )

    assert result.status == "succeeded"
    assert result.effect_state == "applied"
    assert result.output["before_state"] == _sha(ORIGINAL)
    assert result.output["after_state"] == _sha("改后的内容\n")
    assert env.backend.read_bytes(env.path("note.md"))[0] == "改后的内容\n".encode()


def test_modify_missing_file_is_failed_without_write(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "modify_text", env.path("ghost.md"),
        {"path": "ghost.md", "content": "x"},
    )

    assert result.status == "failed"
    assert result.effect_state == "none_applied"
    assert not env.backend.exists(env.path("ghost.md"))


def test_delete_reports_absent_after_state(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "delete_file", env.path("note.md"), {"path": "note.md"}
    )

    assert result.status == "succeeded"
    assert result.output["deleted"] is True
    assert result.output["after_state"] == ABSENT
    assert result.output["before_state"] == _sha(ORIGINAL)
    assert not env.backend.exists(env.path("note.md"))
    assert _read(env, "read_text", {"path": "note.md"}).status == "failed"


def test_restore_with_wrong_hash_is_rejected_without_write(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "restore_file", env.path("note.md"),
        {"path": "note.md", "content_b64": "aGk=", "sha256": "00" * 32},
    )

    assert result.status == "rejected"
    assert result.effect_state == "none_applied"
    assert env.backend.read_bytes(env.path("note.md"))[0] == ORIGINAL.encode()


def test_unknown_argument_is_rejected_before_write(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "create_report", env.path("x.md"),
        {"name": "x.md", "content": "x", "extra": 1},
    )

    assert result.status == "rejected"
    assert not env.backend.exists(env.path("x.md"))


def test_paths_are_relative_and_posix(env):
    _mkdir(env, "sub")
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "create_report", env.path("sub/a.md"),
        {"name": "sub/a.md", "content": "x"},
    )

    assert result.output["path"] == "sub/a.md"
    assert "\\" not in result.output["path"]


def test_effect_state_only_uses_the_three_produced_values(env):
    seen = {_read(env, "list_files", {}).effect_state}
    rc = _new_run(env)
    _, applied = _dispatch(
        env, rc, "create_report", env.path("a.md"), {"name": "a.md", "content": "x"}
    )
    _, failed = _dispatch(
        env, rc, "modify_text", env.path("ghost.md"),
        {"path": "ghost.md", "content": "x"}, call_id="c2",
    )
    seen |= {applied.effect_state, failed.effect_state}

    assert seen == {"none_applied", "applied"}
    assert "partial" not in seen, "partial 从未由工具产生，别在契约里假装它存在"


# ---------------------------------------------------------------- 证据与凭据


def test_prewrite_evidence_exists_before_bytes_are_written(env):
    """写入发生时，快照里必须已经有本次调用的条目。

    顺序反了就等于没证据：崩溃窗口里无从核实"到底改了没有"。
    """
    rc = _new_run(env)
    observed: list[bool] = []
    probe = env.backend
    original_write = probe.write_bytes_atomic

    def spy(path, data):
        observed.append(env.snapshots.find(rc.run.run_id, "c1") is not None)
        original_write(path, data)

    probe.write_bytes_atomic = spy  # type: ignore[method-assign]
    try:
        _, result = _dispatch(
            env, rc, "create_report", env.path("a.md"),
            {"name": "a.md", "content": "x"},
        )
    finally:
        probe.write_bytes_atomic = original_write  # type: ignore[method-assign]

    assert result.status == "succeeded"
    assert observed == [True], "快照证据必须在写入之前落盘"


def test_snapshot_records_one_change_and_dedupes_blobs(env):
    env.backend.write_bytes_atomic(env.path("a.md"), b"same\n")
    env.backend.write_bytes_atomic(env.path("b.md"), b"same\n")
    rc = _new_run(env)
    _dispatch(env, rc, "modify_text", env.path("a.md"), {"path": "a.md", "content": "1"})
    _dispatch(
        env, rc, "modify_text", env.path("b.md"), {"path": "b.md", "content": "2"},
        call_id="c2",
    )

    changes = env.snapshots.load(rc.run.run_id)
    assert [c.path for c in changes] == ["a.md", "b.md"]
    assert all(c.before_state == _sha("same\n") for c in changes)
    blobs = list(env.snapshots.blob_dir.iterdir())
    assert len(blobs) == 1, "同内容只存一份（内容寻址去重）"


def test_failed_call_leaves_no_snapshot_entry(env):
    rc = _new_run(env)
    _dispatch(
        env, rc, "modify_text", env.path("ghost.md"),
        {"path": "ghost.md", "content": "x"},
    )

    assert env.snapshots.load(rc.run.run_id) == []
    assert env.snapshots.has_manifest(rc.run.run_id) is True


def test_snapshot_lives_outside_the_workspace(env):
    rc = _new_run(env)
    _dispatch(
        env, rc, "create_report", env.path("a.md"), {"name": "a.md", "content": "x"}
    )

    assert env.snapshots.root == env.tmp / "artifacts"
    assert not env.backend.exists(env.path("artifacts"))
    assert not env.backend.exists(env.path("blobs"))
    assert not env.backend.exists(env.path("snapshots"))
    if env.backend.name == "memfs":
        # 更严格的一版：连虚拟命名空间的宿主目录都不该出现
        assert not (env.tmp / "mem").exists()


def test_receipt_records_the_actual_backend(env):
    rc = _new_run(env)
    _, result = _dispatch(
        env, rc, "create_report", env.path("a.md"), {"name": "a.md", "content": "x"}
    )
    assert result.effect_state == "applied"

    payloads = receipts_of(env.log, rc.run.run_id)
    assert len(payloads) == 1
    assert payloads[0]["backend"] == env.backend.name
    assert payloads[0]["after_state"] == result.output["after_state"]
    assert verify_receipts(env.log, rc.run.run_id) == []


def test_no_receipt_without_an_applied_effect(env):
    rc = _new_run(env)
    _dispatch(
        env, rc, "modify_text", env.path("ghost.md"),
        {"path": "ghost.md", "content": "x"},
    )
    assert receipts_of(env.log, rc.run.run_id) == []


# ---------------------------------------------------------------- 租约


def test_lease_covers_paths_in_the_subtree_only(env):
    rc = _new_run(env)
    env.leases.assert_covers([env.path("sub/a.md")], rc.run.run_id)

    outside = (env.ws.parent / "outside").resolve()
    with pytest.raises(LeaseLost):
        env.leases.assert_covers([outside], rc.run.run_id)


# ---------------------------------------------------------------- C0 元测试


class _SilentWriteBackend(MemBackend):
    """假装写入成功，实际一个字节都没存。"""

    def write_bytes_atomic(self, p: Path, data: bytes) -> None:
        return None


class _NoLimitBackend(MemBackend):
    """无视 limit，永远返回全文。"""

    def read_bytes(self, p: Path, limit: int | None = None):
        return super().read_bytes(p, None)


def _bad_env(tmp_path: Path, backend: MemBackend) -> Env:
    sandbox.configure_backend(backend)
    ws = (tmp_path / "mem" / "ws").resolve()
    backend.require_root(ws)
    return _build_env(tmp_path, backend, ws, seed=False)


def test_contract_catches_a_backend_that_silently_drops_writes(tmp_path):
    env = _bad_env(tmp_path, _SilentWriteBackend(namespace=tmp_path / "mem"))
    try:
        with pytest.raises(AssertionError):
            assert_create_roundtrip(env)
    finally:
        env.log.close()


def test_contract_catches_a_backend_that_ignores_the_read_limit(tmp_path):
    env = _bad_env(tmp_path, _NoLimitBackend(namespace=tmp_path / "mem"))
    try:
        with pytest.raises(AssertionError):
            assert_read_limit(env)
    finally:
        env.log.close()


# ---------------------------------------------------------------- 失败路径


class _FailingWriteBackend(MemBackend):
    def write_bytes_atomic(self, p: Path, data: bytes) -> None:
        raise OSError("disk full")


def test_write_failure_is_unknown_and_blocks_the_workspace(tmp_path):
    env = _bad_env(tmp_path, _FailingWriteBackend(namespace=tmp_path / "mem"))
    try:
        rc = _new_run(env)
        _, result = _dispatch(
            env, rc, "create_report", env.path("a.md"),
            {"name": "a.md", "content": "x"},
        )

        assert result.status == "failed"
        assert result.effect_state == "unknown", (
            "写阶段抛 OSError 只能记 unknown：可能已经写了一部分"
        )
        assert env.snapshots.find(rc.run.run_id, "c1") is not None
        assert not env.backend.exists(env.path("a.md"))
        assert str(env.ws) in blocked_workspaces(env.log)
    finally:
        env.log.close()

# ---------------------------------------------------------------- 后端不混用


def _executor_for(env: Env, backend) -> FileExecutor:
    return FileExecutor(
        tasks=env.tasks,
        leases=env.leases,
        grants=env.grants,
        approvals=env.approvals,
        claims=env.claims,
        dispatch_lookup=env.dispatcher.dispatch_record,
        dispatch_mark=env.dispatcher.mark_dispatch,
        clock=lambda: 0,
        log=env.log,
        snapshots=env.snapshots,
        backend=backend,
    )


def test_executor_refuses_a_backend_that_is_not_the_current_one(env, tmp_path):
    """两个后端同时活在一个进程里 = 审批看到的世界与写入进入的世界不同。"""
    other = MemBackend(namespace=tmp_path / "other")
    with pytest.raises(TianshuError):
        _executor_for(env, other)


def test_executor_refuses_after_the_process_backend_changes(env, tmp_path):
    executor = env.dispatcher._executor  # noqa: SLF001 - 就是要测这个实例
    sandbox.configure_backend(MemBackend(namespace=tmp_path / "other"))

    with pytest.raises(ToolNotAllowed):
        executor.execute(object(), None)


class _FailingReadBackend(MemBackend):
    def read_bytes(self, p: Path, limit: int | None = None):
        raise PermissionError("denied")


def test_read_failure_is_failed_not_an_exception(tmp_path):
    """读失败是工具结论（failed），不该变成 unknown 或崩溃。"""
    env = _bad_env(tmp_path, _FailingReadBackend(namespace=tmp_path / "mem"))
    try:
        env.backend.write_bytes_atomic(env.path("a.md"), b"x")
        result = _read(env, "read_text", {"path": "a.md"})

        assert result.status == "failed"
        assert result.effect_state == "none_applied"
        assert "读取失败" in result.output["error"]
    finally:
        env.log.close()

# ---------------------------------------------------------------- 目录目标


@pytest.mark.parametrize(
    ("tool_id", "args"),
    [
        ("delete_file", {"path": "."}),
        ("modify_text", {"path": ".", "content": "x"}),
        ("restore_file", {"path": ".", "content_b64": "aGk="}),
        ("create_report", {"name": ".", "content": "x"}),
    ],
)
def test_directory_target_is_refused_by_policy(env, tool_id, args):
    """覆盖/删除类工具指向目录：策略层明确拒绝，rule id 能指路。"""
    rc = _new_run(env)
    decision, result = _dispatch(env, rc, tool_id, env.ws, args)

    assert decision.is_rejected, decision
    assert decision.rule_id == "dispatch.target_is_directory"
    assert result is None
    assert env.backend.is_dir(env.path("."))


def test_directory_target_is_a_tool_result_not_a_run_failure(tmp_path):
    """模型能碰到的写工具指向目录：模型收到的是工具结果，整轮 run 不该被判失败。

    复审复现的缺口：`observe_state` 读目录抛 IsADirectoryError，异常冒到
    loop 的兜底 `except Exception`，run 直接 failed——模型连"这是目录"都
    看不到，更没法自我修正。

    `restore_file` 不再出现在这条路径上：它是内部工具（exposed=False），
    模型提案也不会被派发（见 `test_internal_tool_not_dispatched`）。它自己
    的目录拒绝由 `test_directory_target_is_refused_by_policy` 在派发层覆盖。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db")
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall("c1", "delete_file", {"path": "."}),
                    ToolCall("c2", "modify_text", {"path": ".", "content": "x"}),
                    # 内部工具：提案可以出现，但不该被派发，也不该让整轮失败
                    ToolCall(
                        "c3", "restore_file", {"path": ".", "content_b64": "aGk="}
                    ),
                ],
            ),
            ModelResponse("end_turn", content="这些是目录，不能这样改。"),
        ]
    )
    try:
        result = run(
            task="目录目标",
            workspace_dir=ws,
            out_name="summary.md",
            model=model,
            log=log,
            limits=Limits(6, 20_000),
        )
        # 没有副作用时 run 的自然结论是 noop；关键是**不能是 failed**。
        assert result.outcome in ("succeeded", "noop"), result.outcome
        assert result.outcome != "failed"
        results = [
            e.payload
            for e in log.read_run(result.run_id)
            if e.type == "tool.result"
        ]
        assert [r["status"] for r in results] == ["rejected"] * 2
        assert all("目录" in r["output"]["error"] for r in results)
        # 内部工具没有被派发：没有 tool.result，只有一条留痕
        skipped = [
            e.payload
            for e in log.read_run(result.run_id)
            if e.type == "tool.not_dispatched"
        ]
        assert [s["call_id"] for s in skipped] == ["c3"]
        assert "内部工具" in skipped[0]["reason"]
    finally:
        log.close()


# ---------------------------------------------------------------- C0（目录）


class _FileAlwaysTrueBackend(MemBackend):
    def is_file(self, p: Path) -> bool:
        return True


class _ExistsBlindToDirsBackend(MemBackend):
    def exists(self, p: Path) -> bool:
        return p.as_posix() in self._files


class _BogusDirectorySizeBackend(MemBackend):
    def list_dir(self, p: Path) -> list[tuple[str, bool, int]]:
        return [
            (name, is_dir, 7 if is_dir else size)
            for name, is_dir, size in super().list_dir(p)
        ]


class _DirectorySizeDoesNotRaiseBackend(MemBackend):
    def size(self, p: Path) -> int:
        return 0


def _meta(tmp_path, backend):
    env = _bad_env(tmp_path, backend(namespace=tmp_path / "mem"))
    return env


def test_contract_catches_is_file_that_is_always_true(tmp_path):
    env = _meta(tmp_path, _FileAlwaysTrueBackend)
    try:
        with pytest.raises(AssertionError):
            assert_backend_sees_directories(env)
    finally:
        env.log.close()


def test_contract_catches_exists_that_cannot_see_directories(tmp_path):
    env = _meta(tmp_path, _ExistsBlindToDirsBackend)
    try:
        with pytest.raises(AssertionError):
            assert_backend_sees_directories(env)
    finally:
        env.log.close()


def test_contract_catches_bogus_directory_sizes(tmp_path):
    env = _meta(tmp_path, _BogusDirectorySizeBackend)
    try:
        with pytest.raises(AssertionError):
            assert_directory_entries_report_zero_size(env)
    finally:
        env.log.close()


def test_contract_catches_size_that_does_not_reject_directories(tmp_path):
    env = _meta(tmp_path, _DirectorySizeDoesNotRaiseBackend)
    try:
        with pytest.raises(AssertionError):
            assert_backend_sees_directories(env)
    finally:
        env.log.close()
