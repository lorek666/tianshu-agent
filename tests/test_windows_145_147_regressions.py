"""Issues #145 与 #147 的确定性屏障复现、负控与跨平台回归测试。

覆盖四组根因与防线：
1. #145 副作用落盘后的 SQLite 写锁争用（`SQLITE_BUSY`）：
   - Case 1：`_commit_result` 遇真实 `SQLITE_BUSY`、`_fail_unknown` 成功 → Run 进入
     `reconciling`（绝非 `succeeded` 或 `failed/none_applied`），工作区被阻断且租约保留；
   - Case 2：`_commit_result` 与 `_fail_unknown` 双双遇真实 `SQLITE_BUSY`、进入 `_finish`
     前锁释放 → `_finish` 识别未结算 `tool.intent` 转入 `reconciling`（绝不写
     `effect_state=none_applied`、绝不静默释放租约），随后 `recover_run` 依据写前快照
     将磁盘上的 `out.md` 对账恢复为 `applied` 并补齐 `receipt`；
   - Case 3：锁忙持续贯穿 `_finish` 的 `transition(RUN_RECONCILING)` → `OperationalError`
     上抛由 `Worker._settle` 将作业挂起为 `waiting`，Run 保持非终态且租约不丢。
2. #145 冗余写锁消除：
   - `resume_run` 对本实例已持有的 `ResourceIdentity` 目录租约不再发起冗余 `acquire` 写事务；
   - `renew_claim` 同库时将 `claims.heartbeat` 与 `leases.renew` 合并为单次写事务，并用
     `SAVEPOINT` 保持 `LeaseLost` 时保留心跳提交的精确语义。
3. #147 Windows 共享/句柄冲突（WinError 5/32/33）与本地 `unknown` 收束防线：
   - `LocalBackend.unlink` / `read_bytes` / `identity_key` 对瞬态 `winerror=32/5` 有界重试后
     成功落盘并产出 `receipt`；
   - 持续 `winerror=32` 耗尽有界重试后抛 `PermissionError` → 派发层记 `unknown` + 阻断工作区
     → `AgentLoop` 立即进入 `reconciling`，即使模型随后给出 `end_turn` 也绝不变成 `succeeded`；
   - POSIX `PermissionError`（`winerror=None`）零次重试、立即原样抛出，不被 `identity_key`
     静默洗白为 `path:` 退化键。
4. #147 64-bit Windows `HANDLE`（bit 31 置位 `0x80000124`）C-FFI 原型防截断回归：
   - 用真实 `ctypes.CFUNCTYPE` 验证无原型时 `0x80000124` 会被符号扩展为负数（负控），而
     `leases._win_pid_alive`、`leases._win_pid_started_at` 与 `isolation._win_process_active`
     均向底层 API 传递精确无符号 64 位句柄 `0x80000124`。
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import sqlite3
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest
from support import approve_pending, engine_for, stack_for, write_exact

from tianshu import fsutil, isolation
from tianshu import leases as leases_mod
from tianshu.capability import CapabilityProfile
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces, unblock
from tianshu.jobs import JobStore, renew_claim
from tianshu.leases import LeaseLost, LeaseStore
from tianshu.loop import Limits
from tianshu.model import ModelIdentity, ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import receipts_of
from tianshu.recovery import recover_run
from tianshu.sandbox import LocalBackend
from tianshu.tasks import RUN_QUEUED
from tianshu.worker import Worker


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _EndTurnFallbackModel:
    """复刻 M5 `FakeModel`：预设脚本耗尽后恒返回 `end_turn`。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        self._script = list(script)

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="test", vendor=None, model="fallback-end-turn")

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="test",
            model="fallback-end-turn",
            supports_tool_calling=True,
            supports_parallel_tool_calls=False,
            supports_streaming=False,
            declared_by="tests/test_windows_145_147_regressions.py",
        )

    def complete(self, messages: Any, tools: Any) -> ModelResponse:  # noqa: ARG002
        if not self._script:
            return ModelResponse(finish_reason="end_turn", content="完成")
        return self._script.pop(0)


@contextlib.contextmanager
def _sqlite_write_lock_holder(db_path: Path, blob_dir: Path):
    """在独立线程上持有真实 SQLite `BEGIN IMMEDIATE` 写锁，由 Event 精确控制窗口。"""
    opened = threading.Event()
    acquire_lock = threading.Event()
    locked = threading.Event()
    release_lock = threading.Event()
    holder_errors: list[BaseException] = []

    def _hold() -> None:
        other_log: EventLog | None = None
        try:
            other_log = EventLog(db_path, blob_dir=blob_dir, cross_thread=True)
            opened.set()
            if not acquire_lock.wait(10):
                return
            other_log.conn.execute("BEGIN IMMEDIATE")
            locked.set()
            release_lock.wait(10)
        except BaseException as exc:  # noqa: BLE001 - 保留竞争线程首错供主线程断言
            holder_errors.append(exc)
            opened.set()
            locked.set()
        finally:
            if other_log is not None:
                with contextlib.suppress(sqlite3.Error):
                    other_log.conn.execute("ROLLBACK")
                other_log.close()

    thread = threading.Thread(target=_hold, name="sqlite-busy-holder", daemon=True)
    thread.start()
    try:
        assert opened.wait(10), "竞争写连接未能打开"
        if holder_errors:
            raise holder_errors[0]
        yield acquire_lock, locked, release_lock
    finally:
        acquire_lock.set()
        release_lock.set()
        thread.join(10)
        assert not thread.is_alive(), "竞争写连接线程未退出"
    if holder_errors:
        raise holder_errors[0]


def _enqueue_create_run(stack, ws: Path, *, out_name: str = "out.md") -> tuple[str, str]:
    task = stack.tasks.create_task("生成报告", [str(ws)])
    run = stack.tasks.start_run(
        task.task_id, payload={"task": task.goal, "out": out_name}
    )
    job = stack.claims.enqueue(run.run_id)
    stack.tasks.transition(run.run_id, RUN_QUEUED, reason="enqueued")
    return run.run_id, job.job_id


# =========================================================================
# 1. Issue #145：副作用落盘后 SQLITE_BUSY 屏障复现与恢复对账
# =========================================================================


def test_145_commit_result_sqlite_busy_falls_back_to_unknown_and_reconciling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1：文件已落盘、`_commit_result` 撞真实 SQLITE_BUSY、`_fail_unknown` 成功。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    db_path = tmp_path / "events.db"
    blob_dir = tmp_path / "blobs"

    log = EventLog(db_path, blob_dir=blob_dir)
    try:
        log.conn.execute("PRAGMA busy_timeout=50")
        stack = stack_for(log, data_root=data_root)
        run_id, job_id = _enqueue_create_run(stack, ws)
        seen_busy: list[sqlite3.OperationalError] = []

        with _sqlite_write_lock_holder(db_path, blob_dir) as (
            acquire_lock,
            locked,
            release_lock,
        ):
            real_commit = stack.dispatcher._commit_result

            def contested_commit(*args: Any, **kwargs: Any) -> None:
                assert (ws / "out.md").read_bytes() == b"report body\n"
                acquire_lock.set()
                assert locked.wait(5), "竞争连接未进入 BEGIN IMMEDIATE"
                try:
                    real_commit(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    seen_busy.append(exc)
                    raise
                finally:
                    release_lock.set()

            monkeypatch.setattr(stack.dispatcher, "_commit_result", contested_commit)
            model = _EndTurnFallbackModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "c1",
                                "create_report",
                                {"name": "out.md", "content": "report body\n"},
                            )
                        ],
                    )
                ]
            )
            with Worker(stack, model=model, heartbeat_interval_ms=None) as worker:
                assert worker.run_once() is True
                assert worker.errors == [], worker.errors

        assert len(seen_busy) == 1
        assert "locked" in str(seen_busy[0]).lower()
        assert (ws / "out.md").read_bytes() == b"report body\n"

        run = stack.tasks.get_run(run_id)
        assert run is not None
        assert run.state == "reconciling", run
        assert run.effect_state == "unknown", run
        assert str(ws.resolve()) in blocked_workspaces(log)
        assert stack.leases.is_held_by(ws, run_id) is True
        job = stack.claims.get(job_id)
        assert job is not None and job.state == "waiting"

        # 人工核实磁盘文件并解除阻断后，resume 可在不重派的情况下正常收束并保留 unknown 结论
        unblock(log, ws, reason="人工核实 out.md 已完整落盘")
        resume_engine = engine_for(
            stack, ScriptedModel([ModelResponse("end_turn", content="好")])
        )
        resumed = resume_engine.resume_run(run_id)
        assert resumed.state == "succeeded"
        final_run = stack.tasks.get_run(run_id)
        assert final_run is not None and final_run.effect_state == "unknown"
        assert sum(1 for e in log.read_run(run_id) if e.type == "tool.intent") == 1
    finally:
        log.close()


def test_145_double_sqlite_busy_never_marks_none_applied_and_recovers_to_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 2：`_commit_result` 与 `_fail_unknown` 双双撞 SQLITE_BUSY，锁在 `_finish` 前释放。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    db_path = tmp_path / "events.db"
    blob_dir = tmp_path / "blobs"
    body = "双锁竞争下的报告正文\n"

    log = EventLog(db_path, blob_dir=blob_dir)
    try:
        log.conn.execute("PRAGMA busy_timeout=50")
        stack = stack_for(log, data_root=data_root)
        run_id, job_id = _enqueue_create_run(stack, ws)
        busy_stages: list[str] = []

        with _sqlite_write_lock_holder(db_path, blob_dir) as (
            acquire_lock,
            locked,
            release_lock,
        ):
            real_commit = stack.dispatcher._commit_result
            real_fail_unknown = stack.dispatcher._fail_unknown

            def contested_commit(*args: Any, **kwargs: Any) -> None:
                assert (ws / "out.md").read_bytes() == body.encode("utf-8")
                acquire_lock.set()
                assert locked.wait(5), "竞争连接未进入 BEGIN IMMEDIATE"
                try:
                    real_commit(*args, **kwargs)
                except sqlite3.OperationalError:
                    busy_stages.append("commit_result")
                    raise

            def contested_fail_unknown(*args: Any, **kwargs: Any):
                try:
                    return real_fail_unknown(*args, **kwargs)
                except sqlite3.OperationalError:
                    busy_stages.append("fail_unknown")
                    raise
                finally:
                    release_lock.set()

            monkeypatch.setattr(stack.dispatcher, "_commit_result", contested_commit)
            monkeypatch.setattr(
                stack.dispatcher, "_fail_unknown", contested_fail_unknown
            )

            model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "c1",
                                "create_report",
                                {"name": "out.md", "content": body},
                            )
                        ],
                    ),
                    ModelResponse("end_turn", content="完成"),
                ]
            )
            with Worker(stack, model=model, heartbeat_interval_ms=None) as worker:
                assert worker.run_once() is True

        assert busy_stages == ["commit_result", "fail_unknown"]
        run = stack.tasks.get_run(run_id)
        assert run is not None
        assert run.state == "reconciling", run
        assert run.outcome is None
        assert run.effect_state != "none_applied"
        types_before = [e.type for e in log.read_run(run_id)]
        assert "tool.intent" in types_before
        assert "tool.result" not in types_before
        assert "run.end" not in types_before
        assert stack.leases.is_held_by(ws, run_id) is True
        job = stack.claims.get(job_id)
        assert job is not None and job.state == "waiting"

        # 随后执行 recover_run：依托写前快照将磁盘上的 out.md 对账结算为 applied + receipt
        report = recover_run(
            log,
            run_id,
            ws,
            stack.snapshots,
            leases=stack.leases,
            data_root=data_root,
        )
        assert report.ok, report.problems
        assert report.applied == ["c1"]
        receipts = receipts_of(log, run_id)
        assert len(receipts) == 1
        assert receipts[0]["after_state"] == _sha256(body.encode("utf-8"))

        # 对账完成后继续该 Run：不重复写文件，干净收束为 succeeded / applied
        resumed = engine_for(
            stack, ScriptedModel([ModelResponse("end_turn", content="完成")])
        ).resume_run(run_id)
        assert resumed.state == "succeeded"
        settled = stack.tasks.get_run(run_id)
        assert settled is not None
        assert settled.state == "succeeded"
        assert settled.effect_state == "applied"
        assert sum(1 for e in log.read_run(run_id) if e.type == "tool.intent") == 1
    finally:
        log.close()


def test_145_triple_sqlite_busy_parks_job_without_releasing_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 3：锁忙一直持续到 `_finish` 的 `transition(RUN_RECONCILING)` 之后才释放。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    db_path = tmp_path / "events.db"
    blob_dir = tmp_path / "blobs"

    log = EventLog(db_path, blob_dir=blob_dir)
    try:
        log.conn.execute("PRAGMA busy_timeout=50")
        stack = stack_for(log, data_root=data_root)
        run_id, job_id = _enqueue_create_run(stack, ws)
        busy_stages: list[str] = []

        with _sqlite_write_lock_holder(db_path, blob_dir) as (
            acquire_lock,
            locked,
            release_lock,
        ):
            real_commit = stack.dispatcher._commit_result
            real_fail_unknown = stack.dispatcher._fail_unknown
            real_transition = stack.tasks.transition

            def contested_commit(*args: Any, **kwargs: Any) -> None:
                acquire_lock.set()
                assert locked.wait(5)
                try:
                    real_commit(*args, **kwargs)
                except sqlite3.OperationalError:
                    busy_stages.append("commit_result")
                    raise

            def contested_fail_unknown(*args: Any, **kwargs: Any):
                try:
                    return real_fail_unknown(*args, **kwargs)
                except sqlite3.OperationalError:
                    busy_stages.append("fail_unknown")
                    raise

            def contested_transition(target_run_id: str, to_state: str, **kwargs: Any):
                if to_state != "reconciling":
                    return real_transition(target_run_id, to_state, **kwargs)
                try:
                    return real_transition(target_run_id, to_state, **kwargs)
                except sqlite3.OperationalError:
                    busy_stages.append("transition_reconciling")
                    raise
                finally:
                    release_lock.set()

            monkeypatch.setattr(stack.dispatcher, "_commit_result", contested_commit)
            monkeypatch.setattr(
                stack.dispatcher, "_fail_unknown", contested_fail_unknown
            )
            monkeypatch.setattr(stack.tasks, "transition", contested_transition)

            model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "c1",
                                "create_report",
                                {"name": "out.md", "content": "triple busy\n"},
                            )
                        ],
                    )
                ]
            )
            with Worker(stack, model=model, heartbeat_interval_ms=None) as worker:
                assert worker.run_once() is True
                assert any("OperationalError" in err for err in worker.errors)

        assert busy_stages == [
            "commit_result",
            "fail_unknown",
            "transition_reconciling",
        ]
        run = stack.tasks.get_run(run_id)
        assert run is not None
        assert run.state == "running"
        assert "run.end" not in [e.type for e in log.read_run(run_id)]
        assert stack.leases.is_held_by(ws, run_id) is True
        job = stack.claims.get(job_id)
        assert job is not None and job.state == "waiting"
    finally:
        log.close()


# =========================================================================
# 2. Issue #145：`resume_run` 租约身份比对与 `renew_claim` 单事务合并
# =========================================================================


def test_145_resume_run_skips_redundant_lease_acquire_when_already_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "old.txt", "待删除\n")
    data_root = tmp_path / "data"
    data_root.mkdir()

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = stack_for(log, data_root=data_root)
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "delete_file", {"path": "old.txt"})],
                ),
                ModelResponse("end_turn", content="已删除"),
            ]
        )
        engine = engine_for(stack, model, limits=Limits(4, 10_000))
        first = engine.start(
            stack.tasks.create_task("删旧文件", [str(ws)]).task_id, out_name="out.md"
        )
        assert first.state == "waiting_approval"
        assert stack.leases.is_held_by(ws, first.run_id) is True
        assert approve_pending(stack, first.run_id)

        acquire_calls: list[list[Any]] = []
        real_acquire = stack.leases.acquire

        def spy_acquire(resources: list[Any], *args: Any, **kwargs: Any):
            acquire_calls.append(list(resources))
            return real_acquire(resources, *args, **kwargs)

        monkeypatch.setattr(stack.leases, "acquire", spy_acquire)
        second = engine.resume_run(first.run_id)
        assert second.state == "succeeded"
        assert acquire_calls == [], (
            f"租约仍由本实例有效持有时，resume_run 不应再次 acquire：{acquire_calls}"
        )
        assert not (ws / "old.txt").exists()

        # 对照：若租约已被释放（如暂停后恢复），resume_run 仍必须重新 acquire
        write_exact(ws / "old2.txt", "第二份\n")
        model2 = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c2", "delete_file", {"path": "old2.txt"})],
                ),
                ModelResponse("end_turn", content="已删除"),
            ]
        )
        engine2 = engine_for(stack, model2, limits=Limits(4, 10_000))
        paused_wait = engine2.start(
            stack.tasks.create_task("再删一份", [str(ws)]).task_id, out_name="out2.md"
        )
        assert paused_wait.state == "waiting_approval"
        assert approve_pending(stack, paused_wait.run_id)
        stack.leases.release_run(paused_wait.run_id)
        assert stack.leases.live_paths(paused_wait.run_id) == []
        acquire_calls.clear()

        resumed2 = engine2.resume_run(paused_wait.run_id)
        assert resumed2.state == "succeeded"
        assert len(acquire_calls) == 1
    finally:
        log.close()


def test_145_renew_claim_coalesces_into_single_transaction_and_preserves_savepoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    now = {"ms": 10_000}

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        leases = LeaseStore(log, clock=lambda: now["ms"])
        claims = JobStore(log, identity=leases.identity, clock=lambda: now["ms"])
        leases.acquire([ws_a, ws_b], "run_hb", ttl_ms=5_000)
        job = claims.enqueue("run_hb")
        claim = claims.claim(job.job_id, ttl_ms=5_000)

        outer_tx_entries = 0
        real_tx = log.transaction

        @contextlib.contextmanager
        def count_outer_tx():
            nonlocal outer_tx_entries
            if not log.in_transaction:
                outer_tx_entries += 1
            with real_tx() as tx:
                yield tx

        monkeypatch.setattr(log, "transaction", count_outer_tx)
        now["ms"] = 12_000
        assert renew_claim(leases, claims, claim, ttl_ms=6_000) is True
        assert outer_tx_entries == 1, "同库 renew_claim 必须在单次写事务内同时续期"
        updated_job = claims.get(job.job_id)
        assert updated_job is not None and updated_job.deadline_at == 18_000
        holder_a = leases.holder(ws_a)
        holder_b = leases.holder(ws_b)
        assert holder_a is not None and holder_a.expires_at == 18_000
        assert holder_b is not None and holder_b.expires_at == 18_000

        # SAVEPOINT 语义：若续租中途某条租约已把 owner_pid 改坏导致续不满而抛 LeaseLost，
        # 半截租约更新必须回滚，但已完成核验的 claim.heartbeat 仍在同一次提交中保留。
        log.conn.execute(
            "UPDATE leases SET owner_pid = owner_pid + 999999 WHERE canonical_path = ?",
            (str(ws_b.resolve()),),
        )
        # 让 live_paths 返回两条，触发 renew 更新第 1 条成功、第 2 条为 0 行后抛 LeaseLost
        monkeypatch.setattr(
            leases,
            "live_paths",
            lambda _run_id: [str(ws_a.resolve()), str(ws_b.resolve())],
        )
        outer_tx_entries = 0
        now["ms"] = 14_000
        with pytest.raises(LeaseLost):
            renew_claim(leases, claims, claim, ttl_ms=7_000)

        assert outer_tx_entries == 1
        after_job = claims.get(job.job_id)
        assert after_job is not None and after_job.deadline_at == 21_000
        after_a = leases.holder(ws_a)
        assert after_a is not None and after_a.expires_at == 18_000, (
            "SAVEPOINT 必须回滚 ws_a 的半截续租更新"
        )
    finally:
        log.close()


# =========================================================================
# 3. Issue #147：Windows 共享违规有界重试、持久冲突进 reconciling、POSIX 零重试
# =========================================================================


def test_147_local_backend_retries_transient_winerror_32_and_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalBackend()
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    target = tmp_path / "doc.txt"
    write_exact(target, "hello\n")

    # 1) identity_key (os.stat)：前两次抛 winerror=32 / winerror=5，第 3 次成功且保留 inode 键
    real_stat = os.stat
    stat_attempts = 0

    def flaky_stat(p: Any, *args: Any, **kwargs: Any):
        nonlocal stat_attempts
        stat_attempts += 1
        if stat_attempts in (1, 2):
            err = PermissionError(errno.EACCES, "sharing violation", str(p))
            err.winerror = 32 if stat_attempts == 1 else 5
            raise err
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky_stat)
    key = backend.identity_key(target)
    assert stat_attempts == 3
    if real_stat(target).st_ino:
        assert not key.startswith("path:")
    monkeypatch.setattr(os, "stat", real_stat)

    # 2) read_bytes：前两次抛 winerror=32 / winerror=5，第 3 次读回精确字节
    real_open = Path.open
    read_attempts = 0

    def flaky_open(self_path: Path, *args: Any, **kwargs: Any):
        nonlocal read_attempts
        if self_path == target:
            read_attempts += 1
            if read_attempts in (1, 2):
                err = PermissionError(errno.EACCES, "locked", str(self_path))
                err.winerror = 32 if read_attempts == 1 else 5
                raise err
        return real_open(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    data, total = backend.read_bytes(target)
    assert read_attempts == 3
    assert (data, total) == (b"hello\n", 6)
    monkeypatch.setattr(Path, "open", real_open)

    # 3) unlink：前两次抛 winerror=32 / winerror=5，第 3 次成功删除
    real_unlink = Path.unlink
    unlink_attempts = 0

    def flaky_unlink(self_path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal unlink_attempts
        if self_path == target:
            unlink_attempts += 1
            if unlink_attempts in (1, 2):
                err = PermissionError(errno.EACCES, "sharing violation", str(self_path))
                err.winerror = 32 if unlink_attempts == 1 else 5
                raise err
        real_unlink(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    backend.unlink(target)
    assert unlink_attempts == 3
    assert not target.exists()
    assert delays == [0.01, 0.02, 0.01, 0.02, 0.01, 0.02]


def test_147_posix_permission_error_without_winerror_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalBackend()
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    target = tmp_path / "readonly.txt"
    write_exact(target, "keep\n")

    stat_calls = 0

    def posix_denied_stat(p: Any, *args: Any, **kwargs: Any):
        nonlocal stat_calls
        stat_calls += 1
        raise PermissionError(errno.EACCES, "Permission denied", str(p))

    monkeypatch.setattr(os, "stat", posix_denied_stat)
    with pytest.raises(PermissionError, match="Permission denied"):
        backend.identity_key(target)
    assert stat_calls == 1
    assert delays == []

    unlink_calls = 0

    def posix_denied_unlink(self_path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        raise PermissionError(errno.EACCES, "Permission denied", str(self_path))

    monkeypatch.setattr(Path, "unlink", posix_denied_unlink)
    with pytest.raises(PermissionError, match="Permission denied"):
        backend.unlink(target)
    assert unlink_calls == 1
    assert delays == []


@pytest.mark.parametrize("mode", ["transient_ok", "persistent_unlink", "persistent_stat"])
def test_147_delete_after_approval_transient_succeeds_and_persistent_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """复刻 #147 审批删除链路：瞬态锁重试成功产 receipt；持久锁绝不伪装成 succeeded。"""
    sleep_delays: list[float] = []
    monkeypatch.setattr(time, "sleep", sleep_delays.append)
    ws = tmp_path / "ws"
    ws.mkdir()
    old_file = ws / "old.txt"
    write_exact(old_file, "要删掉的旧账单")
    data_root = tmp_path / "data"
    data_root.mkdir()

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = stack_for(log, data_root=data_root)
        model = _EndTurnFallbackModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "delete_file", {"path": "old.txt"})],
                )
            ]
        )
        engine = engine_for(stack, model, limits=Limits(4, 10_000))
        task = stack.tasks.create_task("删掉旧账单", [str(ws)])
        waiting = engine.start(task.task_id, out_name="summary.md")
        assert waiting.state == "waiting_approval"
        assert approve_pending(stack, waiting.run_id)

        attempts = 0
        if mode in ("transient_ok", "persistent_unlink"):
            real_unlink = Path.unlink

            def hooked_unlink(self_path: Path, *args: Any, **kwargs: Any) -> None:
                nonlocal attempts
                if self_path == old_file:
                    attempts += 1
                    if mode == "persistent_unlink" or attempts <= 2:
                        err = PermissionError(
                            errno.EACCES, "Windows sharing violation", str(self_path)
                        )
                        err.winerror = 32
                        raise err
                real_unlink(self_path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", hooked_unlink)
        else:
            real_stat = os.stat
            real_execute = stack.dispatcher._executor.execute
            in_execute = False

            def hooked_execute(*args: Any, **kwargs: Any):
                nonlocal in_execute
                in_execute = True
                try:
                    return real_execute(*args, **kwargs)
                finally:
                    in_execute = False

            def hooked_stat(p: Any, *args: Any, **kwargs: Any):
                nonlocal attempts
                if in_execute and Path(p) == old_file:
                    attempts += 1
                    err = PermissionError(
                        errno.EACCES, "Windows sharing violation", str(p)
                    )
                    err.winerror = 32
                    raise err
                return real_stat(p, *args, **kwargs)

            monkeypatch.setattr(stack.dispatcher._executor, "execute", hooked_execute)
            monkeypatch.setattr(os, "stat", hooked_stat)

        sleep_delays.clear()
        resumed = engine.resume_run(waiting.run_id)

        if mode == "transient_ok":
            assert attempts == 3
            assert sleep_delays == [0.01, 0.02]
            assert resumed.state == "succeeded"
            run = stack.tasks.get_run(waiting.run_id)
            assert run is not None
            assert run.state == "succeeded"
            assert run.effect_state == "applied"
            assert not old_file.exists()
            assert len(receipts_of(log, waiting.run_id)) == 1
        else:
            assert sleep_delays == list(fsutil._WINDOWS_REPLACE_RETRY_DELAYS[1:])
            if mode == "persistent_unlink":
                assert attempts == len(fsutil._WINDOWS_REPLACE_RETRY_DELAYS)
            else:
                assert attempts >= len(fsutil._WINDOWS_REPLACE_RETRY_DELAYS)
            assert resumed.state == "reconciling", resumed
            run = stack.tasks.get_run(waiting.run_id)
            assert run is not None
            assert run.state == "reconciling"
            assert run.effect_state == "unknown"
            assert old_file.exists()
            assert str(ws.resolve()) in blocked_workspaces(log)
            assert receipts_of(log, waiting.run_id) == []
    finally:
        log.close()


# =========================================================================
# 4. Issue #147：64-bit Windows `HANDLE`（`0x80000124`）C-FFI 防符号扩展回归
# =========================================================================


class _CffiKernel32Stub:
    """用真实 `ctypes.CFUNCTYPE` 指针模拟未声明原型即走 `c_int` 的 Win32 DLL。

    只要调用方漏设 `restype = wintypes.HANDLE` 或 `argtypes = [wintypes.HANDLE, ...]`，
    `_ctypes` C 层就会把 `0x80000124` 截断/符号扩展成 `-0x7ffffedc`！
    """

    HIGH_BIT_HANDLE = 0x80000124
    EXPECTED_EPOCH_MS = 1_700_000_123_456

    def __init__(self, *, open_returns: int = HIGH_BIT_HANDLE, wait_code: int = 0x102):
        self.received_handles: list[tuple[str, int]] = []
        self._callbacks: list[Any] = []
        self._open_returns = open_returns
        self._wait_code = wait_code

        self.OpenProcess = self._raw_fn(
            ctypes.CFUNCTYPE(
                ctypes.c_size_t, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
            ),
            self._open_process,
        )
        self.WaitForSingleObject = self._raw_fn(
            ctypes.CFUNCTYPE(wintypes.DWORD, ctypes.c_size_t, wintypes.DWORD),
            self._wait_single,
        )
        self.GetProcessTimes = self._raw_fn(
            ctypes.CFUNCTYPE(
                wintypes.BOOL,
                ctypes.c_size_t,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ),
            self._get_process_times,
        )
        self.GetExitCodeProcess = self._raw_fn(
            ctypes.CFUNCTYPE(
                wintypes.BOOL, ctypes.c_size_t, ctypes.POINTER(wintypes.DWORD)
            ),
            self._get_exit_code,
        )
        self.CloseHandle = self._raw_fn(
            ctypes.CFUNCTYPE(wintypes.BOOL, ctypes.c_size_t),
            self._close_handle,
        )

    def _raw_fn(self, proto: Any, impl: Any) -> Any:
        cb = proto(impl)
        self._callbacks.append(cb)
        # 转成默认返回 `c_int` 且无 `argtypes` 的裸函数指针，真实复现未声明原型的 `windll`
        return ctypes.cast(cb, ctypes.CFUNCTYPE(ctypes.c_int))

    def _open_process(self, _access: int, _inherit: int, _pid: int) -> int:
        return self._open_returns

    def _wait_single(self, handle: int, _ms: int) -> int:
        self.received_handles.append(("WaitForSingleObject", handle))
        return self._wait_code if handle == self.HIGH_BIT_HANDLE else 0xFFFFFFFF

    def _get_process_times(
        self, handle: int, created: Any, _exited: Any, _kernel: Any, _user: Any
    ) -> int:
        self.received_handles.append(("GetProcessTimes", handle))
        if handle != self.HIGH_BIT_HANDLE:
            return 0
        ticks = (self.EXPECTED_EPOCH_MS + 11_644_473_600_000) * 10_000
        created.contents.dwLowDateTime = ticks & 0xFFFFFFFF
        created.contents.dwHighDateTime = ticks >> 32
        return 1

    def _get_exit_code(self, handle: int, code_ptr: Any) -> int:
        self.received_handles.append(("GetExitCodeProcess", handle))
        if handle != self.HIGH_BIT_HANDLE:
            return 0
        code_ptr.contents.value = 259
        return 1

    def _close_handle(self, handle: int) -> int:
        self.received_handles.append(("CloseHandle", handle))
        return 1 if handle == self.HIGH_BIT_HANDLE else 0


def test_147_64bit_windows_handle_high_bit_not_sign_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 负控自证：同一裸 CFUNCTYPE 若不声明 `restype = wintypes.HANDLE`，返回值会被截成负数
    unprototyped = _CffiKernel32Stub()
    assert unprototyped.OpenProcess(0x00100000, False, 4242) < 0

    # 1) leases._win_pid_alive：WAIT_TIMEOUT(0x102) → True，句柄全链路保持 0x80000124
    stub_alive = _CffiKernel32Stub(wait_code=0x102)
    monkeypatch.setattr(leases_mod, "_open_kernel32", lambda: stub_alive)
    assert leases_mod._win_pid_alive(4242) is True
    assert stub_alive.received_handles == [
        ("WaitForSingleObject", 0x80000124),
        ("CloseHandle", 0x80000124),
    ]

    # WAIT_OBJECT_0(0) → False
    stub_exited = _CffiKernel32Stub(wait_code=0)
    monkeypatch.setattr(leases_mod, "_open_kernel32", lambda: stub_exited)
    assert leases_mod._win_pid_alive(4242) is False
    assert stub_exited.received_handles == [
        ("WaitForSingleObject", 0x80000124),
        ("CloseHandle", 0x80000124),
    ]

    # OpenProcess 返回 NULL 时按 ctypes.get_last_error() 区分 ERROR_ACCESS_DENIED(5)
    stub_denied = _CffiKernel32Stub(open_returns=0)
    monkeypatch.setattr(leases_mod, "_open_kernel32", lambda: stub_denied)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    assert leases_mod._win_pid_alive(4242) is True
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
    assert leases_mod._win_pid_alive(4242) is False

    # 2) leases._win_pid_started_at：64 位句柄原样传入 GetProcessTimes / CloseHandle
    stub_times = _CffiKernel32Stub()
    monkeypatch.setattr(leases_mod, "_open_kernel32", lambda: stub_times)
    assert (
        leases_mod._win_pid_started_at(4242) == _CffiKernel32Stub.EXPECTED_EPOCH_MS
    )
    assert stub_times.received_handles == [
        ("GetProcessTimes", 0x80000124),
        ("CloseHandle", 0x80000124),
    ]

    # 3) isolation._win_process_active：64 位句柄原样传入 GetExitCodeProcess / CloseHandle
    stub_iso = _CffiKernel32Stub()
    monkeypatch.setattr(isolation, "_open_kernel32", lambda: stub_iso)
    assert isolation._win_process_active(4242) is True
    assert stub_iso.received_handles == [
        ("GetExitCodeProcess", 0x80000124),
        ("CloseHandle", 0x80000124),
    ]
