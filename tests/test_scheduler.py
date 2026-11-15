"""W5 调度器测试：到期触发 / 追赶 / 幂等 / disabled / 重启存活 / daemon 集成。

时序纪律（AGENTS.md §四）：单元测试全部用**注入时钟**推进（FakeClock），
不用 sleep 猜；daemon 集成测试起真 daemon，等待用**有界轮询**
（`_wait_until`，test_daemon.py 同款模式——那是同步原语，不是计时猜测）。

契约断言用真实对象：真实 `EventLog` / `build_stack` / 真实事件行，不写
手写替身。负控（NC1 拆到期检查、NC2 拆同事务记账）见 PR body 的红输出。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tianshu import daemon, ipc
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.execpolicy import ExecPolicy
from tianshu.loop import build_stack
from tianshu.model import ScriptedModel
from tianshu.scheduler import Scheduler
from tianshu.workspace import WorkspaceViolation

# --------------------------------------------------------------------------- 夹具


class FakeClock:
    """注入时钟：毫秒 epoch，测试里显式推进。"""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_s(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


class Env:
    """一个真实 Stack + 注入时钟的调度器。"""

    def __init__(self, tmp_path: Path) -> None:
        self.data_root = tmp_path / "data"
        self.log = EventLog(
            self.data_root / "events.db",
            blob_dir=self.data_root / "artifacts" / "blobs",
        )
        self.stack = build_stack(self.log, data_root=self.data_root)
        self.clock = FakeClock()
        self.notes: list[str] = []
        self.scheduler = Scheduler(
            self.log,
            tasks=self.stack.tasks,
            dispatcher=self.stack.dispatcher,
            claims=self.stack.claims,
            clock=self.clock,
            note=self.notes.append,
        )

    def close(self) -> None:
        self.log.close()

    def task_created_count(self) -> int:
        row = self.log.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'task.created'"
        ).fetchone()
        return int(row[0])

    def run_start_count(self) -> int:
        row = self.log.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'run.start'"
        ).fetchone()
        return int(row[0])


@pytest.fixture()
def env(tmp_path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.log.close()


# --------------------------------------------------------------------------- 单元：触发


def test_fire_due_creates_task_run_and_job_once(env: Env) -> None:
    """到期触发：恰好一个 task/run/job；事件流是既有封闭集；记账同事务落定。"""
    ws = env.data_root / "ws"
    ws.mkdir()
    row = env.scheduler.create(
        "每小时检查一次", [{"root_path": str(ws), "mode": "readonly"}],
        interval_seconds=3600, now=env.clock(),
    )
    # 创建时点不触发，next_fire_at = created + interval
    assert env.task_created_count() == 0
    assert row["next_fire_at"] == env.clock() + 3600 * 1000

    env.clock.advance_s(3601)
    fired = env.scheduler.tick()
    assert len(fired) == 1
    run_id = fired[0]

    # 任务/Run/作业各一，事件是既有封闭集（task.created + run.start）
    run = env.stack.tasks.get_run(run_id)
    assert run is not None
    assert env.stack.claims.job_for_run(run_id) is not None
    assert env.task_created_count() == 1
    assert env.run_start_count() == 1

    # 记账：last_fired_at = 触发时刻，next_fire_at 从当前时间重排
    after = env.scheduler.get(row["schedule_id"])
    assert after is not None
    assert after["last_fired_at"] == env.clock()
    assert after["next_fire_at"] == env.clock() + 3600 * 1000
    assert after["last_error"] == ""


def test_tick_before_due_does_nothing(env: Env) -> None:
    env.scheduler.create("未到期", [], interval_seconds=60, now=env.clock())
    assert env.scheduler.tick() == []
    assert env.task_created_count() == 0


def test_no_double_fire_within_interval(env: Env) -> None:
    """同一次到期只消费一次：interval 内反复 tick 不产生第二个任务（NC2 的正控）。"""
    env.scheduler.create("interval 内不重复", [], interval_seconds=60, now=env.clock())
    env.clock.advance_s(61)
    assert len(env.scheduler.tick()) == 1
    env.clock.advance_s(29)  # 仍在 interval 内
    assert env.scheduler.tick() == []
    assert env.task_created_count() == 1


def test_refire_after_interval(env: Env) -> None:
    env.scheduler.create("周期触发", [], interval_seconds=60, now=env.clock())
    env.clock.advance_s(61)
    assert len(env.scheduler.tick()) == 1
    env.clock.advance_s(61)
    assert len(env.scheduler.tick()) == 1
    assert env.task_created_count() == 2


def test_catchup_fires_once_and_reanchors_from_now(env: Env) -> None:
    """追赶：停机期间落下的到期**补发一次**并从当前时间重排——不是补 N 次。"""
    # 模拟 daemon 停机一小时：schedule 在一小时前就该触发（interval 60s = 落下 60 次）
    env.scheduler.create("停机追赶", [], interval_seconds=60, now=env.clock() - 3600 * 1000)
    assert len(env.scheduler.tick()) == 1
    row = env.scheduler.list()[0]
    assert row["next_fire_at"] == env.clock() + 60 * 1000  # 从 now 重排
    assert env.task_created_count() == 1  # 一次，不是 60 次


def test_disabled_never_fires_until_enabled(env: Env) -> None:
    ws = env.data_root / "ws2"
    ws.mkdir()
    row = env.scheduler.create(
        "启停", [{"root_path": str(ws), "mode": "writable"}],
        interval_seconds=60, now=env.clock(),
    )
    env.scheduler.set_enabled(row["schedule_id"], False)
    env.clock.advance_s(3600)
    assert env.scheduler.tick() == []
    assert env.task_created_count() == 0

    # 停用期间到期的窗口在 re-enable 后由第一个 tick 追赶（同一语义）
    env.scheduler.set_enabled(row["schedule_id"], True)
    assert len(env.scheduler.tick()) == 1
    assert env.task_created_count() == 1


def test_fire_failure_rolls_back_and_records_error(env: Env) -> None:
    """触发失败：事务回滚（无任务/无事件），last_error 记录，next_fire_at 前移
    （确定性坏 schedule 不许每个 tick 刷重试风暴）；其余 schedule 不受影响。"""
    real_tasks = env.stack.tasks

    class _BoomOnBadGoal:
        """只对特定 goal 注入故障的 tasks 替身；其余透传真实存储。"""

        @staticmethod
        def create_task(goal, bindings=None, *, exec_policy=None, **_k: object):
            if goal == "必炸任务":
                raise TianshuError("工作区在两次运行之间失效")
            return real_tasks.create_task(goal, bindings, exec_policy=exec_policy)

        @staticmethod
        def start_run(task_id, *, payload, execution_policy, **_k: object):
            return real_tasks.start_run(
                task_id, payload=payload, execution_policy=execution_policy
            )

    good = env.scheduler.create("正常任务", [], interval_seconds=60, now=env.clock())
    bad = env.scheduler.create("必炸任务", [], interval_seconds=60, now=env.clock())

    env.clock.advance_s(61)
    env.scheduler._tasks = _BoomOnBadGoal()  # noqa: SLF001 - 定向故障注入
    fired = env.scheduler.tick()
    assert len(fired) == 1  # 好的那条照常触发

    bad_row = env.scheduler.get(bad["schedule_id"])
    assert bad_row is not None
    assert "工作区在两次运行之间失效" in bad_row["last_error"]
    assert bad_row["next_fire_at"] == env.clock() + 60 * 1000
    assert bad_row["last_fired_at"] is None  # 没成功过，不许记成触发过
    # 回滚：坏任务没留下任何 task.created / run.start 事件
    assert env.task_created_count() == 1  # 只有好的那条
    assert env.run_start_count() == 1
    # note 回调把失败带给了调用方（daemon 日志路径）
    assert any("TianshuError" in note for note in env.notes)
    assert good["schedule_id"]  # 占位引用，避免未使用告警


def test_scheduler_survives_restart(env: Env, tmp_path: Path) -> None:
    """重启存活：schedules 表在事件库里，关掉重开仍能看到；到期则首 tick 追赶。"""
    ws = env.data_root / "ws3"
    ws.mkdir()
    row = env.scheduler.create(
        "重启后仍在", [{"root_path": str(ws), "mode": "readonly"}],
        interval_seconds=60, now=env.clock(),
    )
    env.close()

    log2 = EventLog(
        env.data_root / "events.db",
        blob_dir=env.data_root / "artifacts" / "blobs",
    )
    try:
        stack2 = build_stack(log2, data_root=env.data_root)
        clock2 = FakeClock()
        clock2.now_ms = env.clock.now_ms + 120 * 1000  # 时间继续走
        scheduler2 = Scheduler(
            log2,
            tasks=stack2.tasks,
            dispatcher=stack2.dispatcher,
            claims=stack2.claims,
            clock=clock2,
        )
        rows = scheduler2.list()
        assert [r["schedule_id"] for r in rows] == [row["schedule_id"]]
        assert len(scheduler2.tick()) == 1  # 追赶：停机窗口补发一次
        assert scheduler2.get(row["schedule_id"])["last_fired_at"] == clock2()
    finally:
        log2.close()


def test_create_validates_inputs_at_creation_time(env: Env) -> None:
    """预校验：非法输入在创建时点报错，不制造"每次触发都失败"的 schedule。"""
    with pytest.raises(TianshuError):
        env.scheduler.create("   ", [], interval_seconds=60)
    with pytest.raises(TianshuError):
        env.scheduler.create("interval 太小", [], interval_seconds=0)
    with pytest.raises(TianshuError):
        env.scheduler.create("interval 是布尔", [], interval_seconds=True)
    with pytest.raises(TianshuError):
        env.scheduler.create("坏绑定", [{"mode": "readonly"}], interval_seconds=60)
    with pytest.raises(WorkspaceViolation):
        env.scheduler.create("坏绑定 mode", [{"root_path": "/tmp/x", "mode": "rw"}],
                             interval_seconds=60)
    with pytest.raises(ValueError):
        # exec 策略在 ExecPolicy 构造时自校验（argv 白名单必须绝对路径）——
        # 非法策略根本到不了 scheduler.create，CLI 侧同此语义。
        ExecPolicy(
            argv_allowlist=("relative/cmd",),
            max_output_bytes=1024, timeout_ms=1000,
        )


def test_enable_disable_remove_unknown_id_raise(env: Env) -> None:
    with pytest.raises(TianshuError):
        env.scheduler.set_enabled("sched_nope", True)
    with pytest.raises(TianshuError):
        env.scheduler.remove("sched_nope")
    assert env.scheduler.get("sched_nope") is None


def test_enabled_only_accepts_closed_values(env: Env) -> None:
    """封闭集纪律：enabled 列只会是 0/1（API 只收 bool），不允许第三态落库。"""
    row = env.scheduler.create("启停值", [], interval_seconds=60)
    assert env.scheduler.get(row["schedule_id"])["enabled"] is True
    env.scheduler.set_enabled(row["schedule_id"], False)
    assert env.scheduler.get(row["schedule_id"])["enabled"] is False
    raw = env.log.conn.execute(
        "SELECT enabled FROM schedules WHERE schedule_id = ?",
        (row["schedule_id"],),
    ).fetchone()[0]
    assert raw in (0, 1)


# --------------------------------------------------------------------------- daemon 集成


def _short_data_root(tmp_path: Path) -> Path:
    """unix socket 路径上限 100 字节（test_daemon.py 同款兜底）。"""
    candidate = tmp_path / "data"
    try:
        ipc.endpoint_path(candidate)
        return candidate
    except ipc.ProtocolError:
        return Path(tempfile.mkdtemp(prefix="tssched-", dir=tempfile.gettempdir()))


class _Harness:
    """在子线程里跑 daemon.serve（test_daemon.Harness 的最小本地版）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            daemon.serve(
                self.root,
                model=ScriptedModel([]),
                driver_interval_s=0.02,
                on_ready=self.ready.set,
                stop_event=self.stop,
            )
        except BaseException as exc:  # noqa: BLE001 - 测试要把启动失败带回来
            self.errors.append(exc)
        finally:
            self.ready.set()

    def start(self, timeout: float = 15.0) -> _Harness:
        self.thread.start()
        assert self.ready.wait(timeout), "daemon 没有在超时内就绪"
        if self.errors:
            raise self.errors[0]
        return self

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _fetchone(db: Path, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    """只读查询：daemon 运行期间轮询事件库（WAL 并发读）。用完即关。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.02) -> object:
    """有界轮询：谓词返回**真值**才算等到（计数谓词自己返回 bool）。

    这是同步原语（test_daemon.py 同款），不是计时猜测；超时返回 None 由
    调用方断言失败。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(interval)
    return None


def _backdated_schedule(root: Path, ws: Path, *, goal: str) -> str:
    """在 daemon 外预置一条已到期的 schedule（模拟 daemon 停机期间创建）。"""
    log = EventLog(root / "events.db", blob_dir=root / "artifacts" / "blobs")
    try:
        now_ms = int(time.time() * 1000)
        scheduler = Scheduler(log)
        row = scheduler.create(
            goal, [{"root_path": str(ws), "mode": "readonly"}],
            interval_seconds=3600, now=now_ms - 7200 * 1000,  # 两小时前建，早该触发
        )
        return row["schedule_id"]
    finally:
        log.close()


def test_daemon_fires_due_schedule_and_worker_drives_it(tmp_path: Path) -> None:
    """daemon 集成：过期 schedule 在启动后的第一个 tick 补发，daemon 自己的
    worker 认领驱动到终态；同一 schedule 不双发。"""
    root = _short_data_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    ws = root / "ws"
    ws.mkdir()
    schedule_id = _backdated_schedule(root, ws, goal="调度出来的任务")

    harness = _Harness(root).start()
    try:
        db = root / "events.db"

        def _task_event() -> object:
            return _fetchone(
                db,
                "SELECT payload FROM events"
                " WHERE type = 'task.created' AND payload LIKE '%调度出来的任务%'",
            )

        assert _wait_until(_task_event) is not None, "daemon 没有触发到期的 schedule"

        def _terminal_run() -> object:
            return _fetchone(
                db,
                """
                SELECT r.state FROM runs r
                JOIN events e ON e.task_id = r.task_id
                WHERE e.type = 'task.created'
                  AND e.payload LIKE '%调度出来的任务%'
                """,
            )

        found = _wait_until(_terminal_run)
        assert found is not None, "调度出来的 Run 没有被 daemon 驱动"
        assert found["state"] in {"succeeded", "failed"}

        created = _count_task_created(db, "调度出来的任务")
        sched = _fetchone(
            db,
            "SELECT last_fired_at, next_fire_at, last_error FROM schedules"
            " WHERE schedule_id = ?",
            (schedule_id,),
        )
        assert created == 1, "同一次到期只允许触发一次"
        assert sched is not None
        assert sched["last_fired_at"] is not None
        assert sched["next_fire_at"] > sched["last_fired_at"]  # 从触发时刻重排
        assert sched["last_error"] == ""
    finally:
        harness.stop_and_wait()


def test_daemon_restart_schedule_survives_and_catches_up(tmp_path: Path) -> None:
    """daemon 重启：schedules 表跨重启存活；重启后过期 schedule 追赶补发一次。"""
    root = _short_data_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    ws = root / "ws"
    ws.mkdir()
    first = _backdated_schedule(root, ws, goal="第一次触发的任务")

    first_run = _Harness(root).start()
    db = root / "events.db"
    assert _wait_until(
        lambda: _count_task_created(db, "第一次触发的任务") > 0
    ), "第一个 daemon 没有触发到期的 schedule"
    assert first_run.stop_and_wait()

    # daemon 停机期间：旧 schedule 已重排（interval 1h，不会马上再触发）；
    # 新建一条已过期的——重启后应当追赶补发。
    second = _backdated_schedule(root, ws, goal="重启后补发的任务")

    second_run = _Harness(root).start()
    try:
        assert _wait_until(
            lambda: _count_task_created(db, "重启后补发的任务") > 0
        ), "重启后的 daemon 没有追赶补发到期的 schedule"
        rows = _fetchone(
            db,
            "SELECT COUNT(*) AS n FROM schedules WHERE schedule_id IN (?, ?)",
            (first, second),
        )
        first_count = _count_task_created(db, "第一次触发的任务")
        second_count = _count_task_created(db, "重启后补发的任务")
        assert rows is not None and rows["n"] == 2, "两条 schedule 都要跨重启存活"
        assert first_count == 1 and second_count == 1
    finally:
        second_run.stop_and_wait()
        # 短路径兜底目录由调用方清理（与 test_daemon 同一约定）
        if not str(root).startswith(str(tmp_path)):
            import shutil

            shutil.rmtree(root, ignore_errors=True)


def _count_task_created(db: Path, goal_substring: str) -> int:
    row = _fetchone(
        db,
        "SELECT COUNT(*) AS n FROM events WHERE type = 'task.created'"
        " AND payload LIKE ?",
        (f"%{goal_substring}%",),
    )
    return int(row["n"]) if row is not None else 0
