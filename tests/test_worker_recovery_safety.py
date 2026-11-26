"""Worker 与显式续跑的启动恢复、心跳生命周期与异常保真。"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest
from support import stack_for

from tianshu import _cli_ops, cli
from tianshu import daemon as daemon_module
from tianshu import worker as worker_module
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.jobs import JobLost
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.tasks import RUN_PAUSED, RUN_QUEUED
from tianshu.worker import HeartbeatLoop, Worker, run_worker_once


@pytest.fixture
def queue(tmp_path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = stack_for(log, data_root=tmp_path / "data")
        jobs = []
        for goal in ("第一个任务", "第二个任务"):
            task = stack.tasks.create_task(goal)
            run = stack.tasks.start_run(task.task_id, payload={"task": goal, "out": ""})
            job = stack.claims.enqueue(run.run_id)
            stack.tasks.transition(run.run_id, RUN_QUEUED, reason="enqueued")
            jobs.append(job)
        yield stack, jobs
    finally:
        log.close()


def _events(log):
    return [
        tuple(row)
        for row in log.conn.execute("SELECT seq, payload_hash FROM events ORDER BY seq")
    ]


@pytest.mark.parametrize("phase", ["reconcile_finished_subtasks", "wake_stale_parents"])
@pytest.mark.parametrize("error_type", [TianshuError, sqlite3.OperationalError, KeyboardInterrupt])
def test_startup_failure_blocks_first_and_later_claims(queue, monkeypatch, phase, error_type):
    stack, jobs = queue
    failure = error_type("startup recovery failed")
    original = getattr(stack.tasks, phase)
    model = ScriptedModel([ModelResponse("end_turn", content="不应调用")])
    worker = Worker(stack, model=model, heartbeat_interval_ms=None)
    before = _events(stack.log)

    def fail():
        raise failure

    monkeypatch.setattr(stack.tasks, phase, fail)
    try:
        for attempt in range(2):
            if attempt:
                # 依赖恢复正常、诊断被清空也不能让失败实例重新获得领取资格。
                monkeypatch.setattr(stack.tasks, phase, original)
                worker.errors.clear()
            caught = None
            try:
                worker.run_once()
            except BaseException as exc:
                caught = exc
            assert model.calls == 0, "启动恢复失败后不得调用模型"
            assert [stack.claims.get(job.job_id) for job in jobs] == jobs
            assert _events(stack.log) == before, "拒绝领取不能写 claim/Run/租约事件"
            assert caught is failure, "必须保留启动恢复的原始异常"
    finally:
        worker.close()


def test_successful_startup_reconciles_once_and_drives_the_real_queue(queue, monkeypatch):
    stack, jobs = queue
    model = ScriptedModel(
        [ModelResponse("end_turn", content="完成一"), ModelResponse("end_turn", content="完成二")]
    )
    worker = Worker(stack, model=model, heartbeat_interval_ms=None)
    try:
        assert worker.run_once()
        assert model.calls == 1

        def must_not_reconcile_again():
            raise AssertionError("成功启动恢复不得重复执行")

        monkeypatch.setattr(stack.tasks, "reconcile_finished_subtasks", must_not_reconcile_again)
        monkeypatch.setattr(stack.tasks, "wake_stale_parents", must_not_reconcile_again)
        assert worker.run_once()
        assert not worker.run_once()
        assert model.calls == 2
        assert worker.errors == []
        assert all(stack.tasks.get_run(job.run_id).state == "succeeded" for job in jobs)
    finally:
        worker.close()


def _daemon_for_recovery(stack, model):
    class BoundedStop(threading.Event):
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            assert self.waits <= 3, "the driver did not process or reject the queued request"
            return super().wait(timeout)

    instance = daemon_module._Daemon(
        stack.data_root, model=model, limits=None, driver_interval_s=0,
        stop_event=BoundedStop(), on_ready=None,
    )
    instance.driver_stack, instance.driver_log = stack, stack.log
    instance.plane = SimpleNamespace(resume_drained=lambda _run_id: None)
    return instance


@pytest.mark.parametrize("preloaded", [False, True])
@pytest.mark.parametrize("failed_startup", [False, True])
def test_daemon_checks_startup_before_explicit_resume(
    queue, monkeypatch, preloaded, failed_startup,
):
    stack, (job, next_job) = queue
    claim = stack.claims.claim(job.job_id)
    stack.tasks.transition(job.run_id, RUN_PAUSED, claim=claim, reason="test startup order")
    waiting = stack.claims.park(claim, reason=RUN_PAUSED)
    before = _events(stack.log)
    model = ScriptedModel([ModelResponse("end_turn", content="resumed")])
    instance = _daemon_for_recovery(stack, model)
    original_wake = stack.tasks.wake_stale_parents
    original_complete = model.complete
    wake_calls = []

    def wake():
        wake_calls.append(True)
        if not preloaded:
            instance.resume_requests.put(job.run_id)
        if failed_startup:
            raise sqlite3.OperationalError("startup recovery was interrupted")
        return original_wake()

    def complete(messages, tools):
        instance.stop.set()
        assert wake_calls == [True], "an explicit request must wait for startup recovery"
        return original_complete(messages, tools)

    if preloaded:
        instance.resume_requests.put(job.run_id)
    monkeypatch.setattr(stack.tasks, "wake_stale_parents", wake)
    monkeypatch.setattr(model, "complete", complete)
    monkeypatch.setattr(daemon_module, "DRIVER_MAX_CONSECUTIVE_FAILURES", 2)
    instance._driver_loop()
    actual = stack.claims.get(job.job_id)
    assert wake_calls == [True]
    assert stack.claims.get(next_job.job_id) == next_job
    if failed_startup:
        assert actual == waiting and model.calls == 0
        assert _events(stack.log) == before
    else:
        assert actual.state == "done" and actual.claim_epoch == waiting.claim_epoch + 1
        assert model.calls == 1


@pytest.mark.parametrize("route,failed_cleanup", [
    ("worker", "settle"), ("daemon", "settle"), ("daemon", "drain"),
])
def test_cleanup_does_not_replace_model_interrupt(queue, monkeypatch, route, failed_cleanup):
    stack, (job, next_job) = queue
    model = ScriptedModel([ModelResponse("end_turn", content="interrupted")])
    primary = KeyboardInterrupt("model interrupt")
    secondary = sqlite3.OperationalError(f"{failed_cleanup} I/O failure")
    real_park = stack.claims.park
    seen = []

    def complete(_messages, _tools):
        assert stack.claims.get(job.job_id).state == "claimed"
        seen.append("model")
        raise primary

    def park(claim, *, reason):
        assert claim.run_id == job.run_id
        seen.append("settle")
        if failed_cleanup == "settle":
            raise secondary
        return real_park(claim, reason=reason)

    monkeypatch.setattr(model, "complete", complete)
    monkeypatch.setattr(stack.claims, "park", park)
    instance = _daemon_for_recovery(stack, model)

    def drain(run_id):
        assert run_id == job.run_id
        seen.append("drain")
        instance.stop.set()
        if failed_cleanup == "drain":
            raise secondary

    instance.plane.resume_drained = drain
    instance.resume_requests.put(job.run_id)
    caught = None
    try:
        if route == "worker":
            with Worker(stack, model=model, heartbeat_interval_ms=None) as worker:
                worker.run_once()
        else:
            instance._driver_loop()
    except BaseException as exc:
        caught = exc
    assert seen == (["model", "settle"] if route == "worker" else ["model", "settle", "drain"])
    assert caught is primary, "cleanup must preserve the original model interrupt"
    assert any(str(secondary) in note for note in primary.__notes__)
    assert stack.claims.get(next_job.job_id) == next_job


def test_heartbeat_timeout_cannot_lose_the_live_thread_or_allow_restart():
    entered = threading.Event()
    release = threading.Event()

    def beat():
        entered.set()
        assert release.wait(5), "测试未释放真实心跳线程"
        return True

    loop = HeartbeatLoop(beat, interval_ms=1, join_timeout_ms=0)
    loop.start()
    thread = loop._thread
    try:
        assert entered.wait(5)
        with pytest.raises(TianshuError):
            loop.stop()
        assert not loop.stopped_promptly
        with pytest.raises(TianshuError, match="已经启动"):
            loop.start()
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        loop.stop()
    assert loop.stopped_promptly, "超时后必须仍能确认原线程真实退出"


def test_heartbeat_cleanup_error_preserves_the_body_exception(monkeypatch):
    primary = ValueError("original engine failure")
    secondary = RuntimeError("stop failure")
    loop = HeartbeatLoop(lambda: False)
    loop.start()
    thread = loop._thread
    thread.join(5)
    assert not thread.is_alive()

    def fail_stop():
        raise secondary

    monkeypatch.setattr(loop, "stop", fail_stop)
    caught = None
    try:
        raise primary
    except ValueError:
        try:
            loop.__exit__(ValueError, primary, primary.__traceback__)
        except BaseException as exc:
            caught = exc
    assert caught is None, "清理异常不能替换正在传播的主异常"
    assert any("stop failure" in note for note in primary.__notes__)


@pytest.mark.parametrize("primary_type", [RuntimeError, KeyboardInterrupt])
def test_worker_keeps_connection_and_blocks_claims_after_engine_error(
    queue, monkeypatch, primary_type
):
    stack, jobs = queue
    entered = threading.Event()
    release = threading.Event()
    primary = primary_type("original engine failure")
    loops = []
    threads = []

    class ControlledHeartbeat(HeartbeatLoop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, join_timeout_ms=0)
            loops.append(self)

        def start(self):
            result = super().start()
            threads.append(self._thread)
            return result

    class FailingEngine:
        def resume_run(self, run_id):
            assert run_id == jobs[0].run_id
            assert entered.wait(5), "引擎必须等到真实心跳线程进入屏障"
            raise primary

    monkeypatch.setattr(worker_module, "HeartbeatLoop", ControlledHeartbeat)
    worker = Worker(
        stack,
        model=ScriptedModel([]),
        engine_factory=lambda claim: FailingEngine(),
        heartbeat_interval_ms=1,
    )
    goal_ticks = []
    monkeypatch.setattr(worker, "_tick_goals", lambda: goal_ticks.append("tick"))
    channel = worker._side_channel()
    real_beat = channel.beat

    def blocked_beat(claim, *, ttl_ms):
        result = real_beat(claim, ttl_ms=ttl_ms)
        entered.set()
        assert release.wait(5), "测试未释放真实心跳线程"
        return result

    monkeypatch.setattr(channel, "beat", blocked_beat)
    try:
        if primary_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                worker.run_once()
            assert caught.value is primary
        else:
            assert worker.run_once()
            assert any("original engine failure" in message for message in worker.errors)

        assert any("心跳线程未如期退出" in message for message in worker.errors), (
            "引擎异常也必须在 finally 采集心跳未收拢的诊断"
        )
        assert goal_ticks == ["tick"], "旧心跳存活时不能继续推进 Goal"
        before = _events(stack.log)
        before_jobs = [stack.claims.get(job.job_id) for job in jobs]
        caught = None
        try:
            worker.run_once()
        except BaseException as exc:
            caught = exc
        assert [stack.claims.get(job.job_id) for job in jobs] == before_jobs
        assert _events(stack.log) == before, "旧心跳未退出时不得领取下一个作业"
        assert isinstance(caught, TianshuError)

        with pytest.raises(TianshuError):
            worker.close()
        assert worker._side is channel
        assert channel._log.conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        worker.close()
    assert all(loop.stopped_promptly for loop in loops)
    assert worker._side is None


def test_interrupted_join_cannot_be_reclassified_as_clean_by_retry(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def beat():
        entered.set()
        assert release.wait(5)

    loop = HeartbeatLoop(beat, interval_ms=1, join_timeout_ms=0)
    loop.start()
    thread = loop._thread
    real_join = thread.join
    interruption = KeyboardInterrupt("join interrupted")

    def interrupted_join(timeout):
        raise interruption

    try:
        assert entered.wait(5)
        monkeypatch.setattr(thread, "join", interrupted_join)
        with pytest.raises(KeyboardInterrupt) as caught:
            loop.stop()
        assert caught.value is interruption
    finally:
        monkeypatch.setattr(thread, "join", real_join)
        release.set()
        real_join(5)
        assert not thread.is_alive()
    with pytest.raises(TianshuError, match="join 曾中断"):
        loop.stop()
    assert not loop.stopped_promptly


def test_worker_wrapper_cleanup_keeps_the_original_startup_failure(queue, monkeypatch):
    stack, jobs = queue
    primary = TianshuError("original startup failure")
    secondary = RuntimeError("worker close failure")
    before = _events(stack.log)

    def fail_reconcile():
        raise primary

    def fail_close(self):
        raise secondary

    monkeypatch.setattr(stack.tasks, "reconcile_finished_subtasks", fail_reconcile)
    monkeypatch.setattr(Worker, "close", fail_close)
    caught = None
    try:
        run_worker_once(
            stack.claims,
            stack.claims.identity,
            stack=stack,
            model=ScriptedModel([]),
            heartbeat_interval_ms=None,
        )
    except BaseException as exc:
        caught = exc
    assert caught is primary, "公开 wrapper 的清理异常不能替换恢复首错"
    assert any("worker close failure" in note for note in primary.__notes__)
    assert [stack.claims.get(job.job_id) for job in jobs] == jobs
    assert _events(stack.log) == before


def test_cli_cleanup_keeps_the_original_startup_failure(queue, monkeypatch):
    stack, _ = queue
    primary = TianshuError("original startup failure")
    secondary = RuntimeError("worker close failure")

    def fail_reconcile():
        raise primary

    def fail_close(self):
        raise secondary

    monkeypatch.setattr(stack.tasks, "reconcile_finished_subtasks", fail_reconcile)
    monkeypatch.setattr(Worker, "close", fail_close)
    monkeypatch.setattr(_cli_ops, "_authority_stack", lambda *args, **kwargs: stack)
    monkeypatch.setattr(cli, "_model_from", lambda args: ScriptedModel([]))
    args = argparse.Namespace(
        db=stack.log.db_path,
        data_root=stack.data_root,
        max_turns=1,
        max_total_tokens=100,
        once=True,
        max_jobs=1,
        json=True,
    )
    caught = None
    try:
        _cli_ops._cmd_worker(args)
    except BaseException as exc:
        caught = exc
    assert caught is primary, "CLI 清理异常不能替换恢复首错"
    assert any("worker close failure" in note for note in primary.__notes__)


@pytest.mark.parametrize("start_phase", ["before_native_start", "after_native_start"])
def test_thread_start_failure_preserves_the_handle_and_connection(queue, monkeypatch, start_phase):
    stack, jobs = queue
    entered = threading.Event()
    release = threading.Event()
    original_start = threading.Thread.start
    loops = []
    primary = RuntimeError(f"start failed {start_phase}")

    class ControlledHeartbeat(HeartbeatLoop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, join_timeout_ms=0)
            loops.append(self)

    def fail_start(thread):
        if start_phase == "after_native_start":
            original_start(thread)
            assert entered.wait(5), "真实线程必须进入心跳屏障"
        raise primary

    monkeypatch.setattr(worker_module, "HeartbeatLoop", ControlledHeartbeat)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    worker = Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=1)
    channel = worker._side_channel()
    real_beat = channel.beat

    def beat(claim, *, ttl_ms):
        result = real_beat(claim, ttl_ms=ttl_ms)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(channel, "beat", beat)
    try:
        assert worker.run_once()
        loop = loops[0]
        assert loop._thread is not None
        assert worker._heartbeat is loop
        assert not loop.stopped_promptly
        assert any(str(primary) in message for message in worker.errors)
        before = _events(stack.log)
        before_jobs = [stack.claims.get(job.job_id) for job in jobs]
        with pytest.raises(TianshuError, match="生命周期"):
            worker.run_once()
        assert [stack.claims.get(job.job_id) for job in jobs] == before_jobs
        assert _events(stack.log) == before
        with pytest.raises(TianshuError):
            worker.close()
        assert worker._side is channel
        assert channel._log.conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        release.set()
        if loops:
            thread = loops[0]._thread
            if thread is not None:
                if start_phase == "before_native_start":
                    # 仅为清理注入失败的真实线程；产品仍按未知状态拒绝。
                    original_start(thread)
                thread.join(5)
                assert not thread.is_alive()
            worker.close()


def test_worker_counts_real_beats_once_across_repeated_close_and_new_jobs(queue, monkeypatch):
    stack, jobs = queue
    entered = {job.run_id: threading.Event() for job in jobs}
    model = ScriptedModel(
        [ModelResponse("end_turn", content="完成一"), ModelResponse("end_turn", content="完成二")]
    )
    worker = Worker(stack, model=model, heartbeat_interval_ms=60_000)
    real_complete = model.complete

    def complete(messages, tools):
        assert entered[jobs[model.calls].run_id].wait(5)
        return real_complete(messages, tools)

    monkeypatch.setattr(model, "complete", complete)
    try:
        for expected, job in enumerate(jobs, 1):
            channel = worker._side_channel()
            real_beat = channel.beat

            def beat(claim, *, ttl_ms, original=real_beat):
                result = original(claim, ttl_ms=ttl_ms)
                entered[claim.run_id].set()
                return result

            monkeypatch.setattr(channel, "beat", beat)
            assert worker.run_once()
            assert worker.beats == expected, "驱动退出前必须采集本轮心跳统计"
            worker.close()
            worker.close()
            assert worker.beats == expected
            assert stack.tasks.get_run(job.run_id).state == "succeeded"
        assert model.calls == 2
        assert worker.errors == []
    finally:
        worker.close()


@pytest.mark.parametrize(("route", "failed_log"), [
    ("direct_resume", None),
    ("driver_loop", None),
    ("driver_loop", "时心跳未收拢"),
    ("driver_loop", "续跑心跳清理失败"),
])
def test_daemon_preserves_live_resume_heartbeat_and_blocks_new_claims(
    queue, monkeypatch, route, failed_log,
):
    stack, jobs = queue
    entered = threading.Event()
    release = threading.Event()
    loops = []
    threads = []
    primary = KeyboardInterrupt("model interrupt with live heartbeat")
    instance = daemon_module._Daemon(
        stack.data_root,
        model=ScriptedModel([]),
        limits=None,
        driver_interval_s=0.1,
        stop_event=threading.Event(),
        on_ready=None,
    )
    instance.driver_stack, instance.driver_log = stack, stack.log
    instance.plane = SimpleNamespace(resume_drained=lambda run_id: None)
    original_beat = daemon_module._DriverSideChannel.beat

    class ControlledHeartbeat(HeartbeatLoop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, join_timeout_ms=0)
            loops.append(self)

        def start(self):
            result = super().start()
            threads.append(self._thread)
            return result

    def blocked_beat(channel, claim):
        entered.set()
        assert release.wait(5)
        return original_beat(channel, claim)

    class Engine:
        def __init__(self, **kwargs):
            pass

        def current_claim(self, run_id):
            return None

        def resume_run(self, run_id):
            assert entered.wait(5), "续跑必须等真实心跳进入屏障"
            if failed_log is not None:
                raise primary
            if route == "driver_loop":
                instance.stop.set()

    monkeypatch.setattr(daemon_module, "HeartbeatLoop", ControlledHeartbeat)
    monkeypatch.setattr(daemon_module._DriverSideChannel, "beat", blocked_beat)
    monkeypatch.setattr(daemon_module, "AgentLoop", Engine)
    try:
        if route == "driver_loop":
            instance.resume_requests.put(jobs[0].run_id)
            if failed_log is None:
                instance._driver_loop()
            else:
                reader, writer = os.pipe()
                os.close(reader)
                stderr = sys.stderr
                log_errors = []

                class Stderr:
                    def write(self, text):
                        if failed_log in text:
                            try:
                                os.write(writer, text.encode())
                            except OSError as exc:
                                log_errors.append(exc)
                                raise
                        return stderr.write(text)

                    def flush(self):
                        stderr.flush()

                try:
                    caught = None
                    with monkeypatch.context() as patch:
                        patch.setattr(sys, "stderr", Stderr())
                        try:
                            instance._driver_loop()
                        except BaseException as exc:
                            caught = exc
                    assert len(log_errors) == 1, "诊断必须命中真实已关闭的 stderr 管道"
                    assert caught is primary, "日志写入失败不能覆盖原模型中断"
                    assert any(type(log_errors[0]).__name__ in note for note in primary.__notes__)
                finally:
                    os.close(writer)
        else:
            instance._resume_one(Engine(), jobs[0].run_id)
        assert threads[0].is_alive()
        assert stack.claims.get(jobs[1].job_id) == jobs[1]
        channel = instance.side
        assert channel is not None, "driver 收尾不能关闭仍由真实心跳使用的连接"
        assert channel._log.conn.execute("SELECT 1").fetchone()[0] == 1
        before = _events(stack.log)
        before_jobs = [stack.claims.get(job.job_id) for job in jobs]
        instance._resume_one(Engine(), jobs[1].run_id)
        assert [stack.claims.get(job.job_id) for job in jobs] == before_jobs
        assert _events(stack.log) == before, "旧心跳未退出时不能领取新作业"
        assert instance.stop.is_set(), "未收拢的心跳必须停止 daemon 驱动"
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        for loop in loops:
            loop.stop()
        if instance.side is not None:
            instance.side.close()
    # 主线程已 park 作业；放行后真实续期应读到 claim 失效，而非连接已关闭。
    assert all(isinstance(loop.error, JobLost) for loop in loops), [
        (type(loop.error).__name__, str(loop.error)) for loop in loops
    ]
