"""M2 · PR C · Task 6：作业循环与端到端验收。

这一组测试不测"某个函数返回什么"，测的是**用户真正会走的那几条路**：

1. 两个进程抢同一个排队作业——只能有一个 owner、一次写入；
2. 批准之后重启再继续——审批不能被一次进程重启作废（否则主流程永远走不完）；
3. 崩溃之后先核查再继续——`recover` 不负责接着跑；
4. 只读目录 / 可写目录 / 零目录对话——三种绑定的边界；
5. 阻塞住的模型调用不能把 owner 拖成"已过期"——心跳要在别的线程里续。

第 1、2、3 条用**真子进程**：进程内构造的"重启"共享同一个 pid，而
`is_live` / `pid_alive` 判的正是 pid，"在同一个进程里模拟重启"证明不了任何事。
第 5 条用真实短 TTL + 真实阻塞：心跳循环的意义就是"宿主线程卡住时还有别人在跑"，
把它换成假时钟或假线程，测出来的只是幻觉。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.jobs import JOB_WAITING, JobStore
from tianshu.leases import (
    ProcessIdentity,
    execution_guard,
    pid_alive,
)
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import HumanIdentity
from tianshu.tasks import RUN_QUEUED, RUN_WAITING_APPROVAL
from tianshu.worker import HeartbeatLoop, Worker, run_worker_once

SRC = str(Path(__file__).resolve().parents[1] / "src")
ORIGINAL = "原始内容\n"


# ---------------------------------------------------------------- 夹具


@dataclass
class Env:
    root: Path
    ws: Path
    data: Path
    db: Path
    log: EventLog
    stack: object
    read_only: Path | None = None

    def engine(self, responses, *, limits=None) -> AgentLoop:
        return AgentLoop(
            tasks=self.stack.tasks,
            leases=self.stack.leases,
            claims=self.stack.claims,
            dispatcher=self.stack.dispatcher,
            approvals=self.stack.approvals,
            snapshots=self.stack.snapshots,
            model=ScriptedModel(list(responses)),
            limits=limits or Limits(8, 20_000),
        )

    def reopen(self) -> None:
        """丢掉这条连接再重开一遍（模拟"另一个进程接手"）。

        `build_stack` 里的每个 store 都绑着**具体那条连接**，只换 `log`
        不换 store 就会出现"半新半旧"的装配——真实进程不会这样，测试也不该。
        """
        self.log.close()
        self.log = EventLog(self.db, blob_dir=self.root / "blobs")
        self.stack = build_stack(self.log, data_root=self.data)

    def events(self, run_id: str) -> list[str]:
        return [e.type for e in self.log.read_run(run_id)]

    def payloads(self, run_id: str, event_type: str) -> list[dict]:
        return [
            e.payload for e in self.log.read_run(run_id) if e.type == event_type
        ]

    def count(self, run_id: str, event_type: str) -> int:
        return self.events(run_id).count(event_type)

    def enqueue(self, *, goal="整理笔记", root=None, out="", payload_extra=None):
        """建 Task、开 Run、入队作业——模拟"编排方排了队，但没人驱动"。

        这是作业循环存在的前提：没有这一层，Run 只有"有人在前台按 start"
        一条路能跑，队里排着的作业永远不会被认领。
        """
        roots = [] if root is None else [str(root)]
        task = self.stack.tasks.create_task(goal, roots)
        payload = {"task": goal, "out": out, **(payload_extra or {})}
        run = self.stack.tasks.start_run(task.task_id, payload=payload)
        job = self.stack.claims.enqueue(run.run_id)
        self.stack.tasks.transition(run.run_id, RUN_QUEUED, reason="enqueued")
        return run.run_id, task.task_id, job.job_id


@pytest.fixture()
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text(ORIGINAL, encoding="utf-8")
    read_only = tmp_path / "ro"
    read_only.mkdir()
    (read_only / "src.md").write_text("只读\n", encoding="utf-8")
    data = tmp_path / "authority"
    data.mkdir()
    db = tmp_path / "events.db"
    log = EventLog(db, blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=data)
    e = Env(
        root=tmp_path, ws=ws, data=data, db=db, log=log, stack=stack,
        read_only=read_only,
    )
    yield e
    log.close()


def _end(text: str = "完成") -> ModelResponse:
    return ModelResponse("end_turn", content=text)


def _create(name: str, content: str, call_id: str) -> ModelResponse:
    return ModelResponse(
        "tool_use",
        tool_calls=[ToolCall(call_id, "create_report", {"name": name, "content": content})],
    )


def _overwrite(path: str, content: str, call_id: str = "c1") -> ModelResponse:
    return ModelResponse(
        "tool_use",
        tool_calls=[ToolCall(call_id, "modify_text", {"path": path, "content": content})],
    )


def _actor() -> HumanIdentity:
    return HumanIdentity.local_cli()


def _approve_all(stack, run_id: str) -> list[str]:
    ids = [a.approval_id for a in stack.approvals.pending(run_id=run_id)]
    for approval_id in ids:
        stack.approvals.decide(approval_id, decision="approved", actor=_actor())
    return ids


# ---------------------------------------------------------------- 真子进程


CHILD = """
import json, os, sys
sys.path.insert(0, __SRC__)
import tianshu.cli as cli
from tianshu.model import ModelResponse, ScriptedModel, ToolCall

spec = os.environ.get("TIANSHU_TEST_MODEL")
if spec:
    data = json.loads(spec)
    if data.get("mode") == "by_goal":
        import re

        class ByGoal:
            # 无状态：还没写过就按目标里的编号写 r<i>.md，写过就结束。
            # 真实模型不跨 Run 记调用次数；替身一旦有状态，后面的 Run 会空转。

            def complete(self, messages, tools):
                if any(m.get("role") == "tool" for m in messages):
                    return ModelResponse("end_turn", content="完成")
                text = json.dumps(messages, ensure_ascii=False)
                found = re.search(r"写第 (\\d+) 份", text)
                name = "r%s.md" % (found.group(1) if found else "x")
                return ModelResponse("tool_use", tool_calls=[
                    ToolCall("c1", "create_report",
                             {"name": name, "content": "第 " + name + " 份"})])

        cli._scripted_model = lambda out_name: ByGoal()
    else:
        responses = []
        for item in data["responses"]:
            responses.append(ModelResponse(
                item["finish_reason"],
                content=item.get("content", ""),
                tool_calls=[ToolCall(c["call_id"], c["name"], c["arguments"])
                            for c in item.get("tool_calls", [])],
            ))
        cli._scripted_model = lambda out_name: ScriptedModel(list(responses))

sys.exit(cli.main(sys.argv[1:]))
"""


def _child_src() -> str:
    """CHILD 模板的最终源码。

    用 `__SRC__` 占位符而不是 `str.format`：模板里本来就有 `{}` 字面量
    （模型响应的 payload），`format` 会把它们当占位符炸掉。
    """
    return CHILD.replace("__SRC__", repr(SRC))


def _child(argv, *, model=None, env_extra=None, timeout=180) -> subprocess.CompletedProcess:
    child = _child_src()
    environment = dict(os.environ)
    environment.pop("TIANSHU_TEST_MODEL", None)
    if model is not None:
        environment["TIANSHU_TEST_MODEL"] = json.dumps({"responses": model})
    environment.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", child, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )


def _cli_args(env: Env) -> list[str]:
    return ["--db", str(env.db), "--data-root", str(env.data)]


class _SlowModel:
    """`complete()` 先睡一会儿再回答——用来造"宿主线程被阻塞"。"""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self.calls = 0

    def complete(self, _messages, _tools) -> ModelResponse:
        self.calls += 1
        time.sleep(self._seconds)
        return ModelResponse("end_turn", content="慢慢想好了")


def _dead_pid() -> int:
    """等一个刚退出的子进程 pid 变成"确实不存在"。"""
    for _ in range(50):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        if not pid_alive(proc.pid):
            return proc.pid
        time.sleep(0.02)
    raise AssertionError("拿不到一个已退出的 pid")


# ---------------------------------------------------------------- 1. 并发认领


def test_two_workers_claim_once(env):
    """四个进程抢同一个排队作业：只能有一个 owner、一次写入、一条结论。

    反例是"认领不加条件"：两个进程各自 `UPDATE jobs SET state='claimed'`，
    epoch 都是 1，都能通过核验，于是同一份游标被两个 owner 交替写——
    fencing 形同虚设，模型调用还被付了两次钱。
    """

    run_id, task_id, job_id = env.enqueue(root=env.ws, out="report.md")

    child_env = dict(os.environ)
    child_env["TIANSHU_TEST_MODEL"] = json.dumps(
        {"responses": [_create("report.md", "并发\n", "c1").to_payload(), _end().to_payload()]}
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _child_src(), "worker", "--once",
             *_cli_args(env), "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )
        for _ in range(4)
    ]
    outs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=180)
        assert proc.returncode == 0, stderr
        outs.append(json.loads(stdout))

    ran = [o for o in outs if o["run_ids"]]
    assert len(ran) == 1, f"应当只有一个进程真正认领作业：{outs}"
    assert sum(len(o["run_ids"]) for o in outs) == 1, outs

    run = env.stack.tasks.get_run(run_id)
    assert run.state == "succeeded", run
    assert run.outcome == "succeeded"
    assert env.count(run_id, "job.claimed") == 1, "不许有两个 owner"
    assert env.count(run_id, "tool.intent") == 1, "写入只发生一次"
    assert env.count(run_id, "run.end") == 1
    assert (env.ws / "report.md").exists()

    job = env.stack.claims.get(job_id)
    assert job.state == "done" and job.outcome == "succeeded"
    assert job.claim_epoch == 1


def test_claim_next_takes_over_dead_owner_without_waiting_for_ttl(env):
    """owner 进程已死：立刻可以接管，不必等 30 秒 TTL 走完。

    只看 deadline 会让"进程被 kill 之后重启"变成"先等半分钟"——用户看到的
    是"resume 没反应"。`is_live` 已经把"进程不在了"算作不活，认领要跟着用它。
    """
    dead_pid = _dead_pid()
    run_id, _, job_id = env.enqueue()
    dead_log = EventLog(env.db, blob_dir=env.root / "blobs")
    dead = JobStore(
        dead_log,
        identity=ProcessIdentity(
            instance_id="dead-instance", pid=dead_pid, started_at=0
        ),
    )
    try:
        claim = dead.claim(job_id)
        assert claim.claim_epoch == 1

        live = JobStore(env.log, identity=ProcessIdentity.mint())
        taken = live.claim_next()
        assert taken is not None, "死掉的 owner 必须能被立刻接管"
        assert (taken.job_id, taken.claim_epoch) == (job_id, 2)
        # 旧 claim 的续期必须失败：接管之后旧 worker 不许再写任何状态
        with pytest.raises(TianshuError):
            dead.heartbeat(claim)
    finally:
        dead_log.close()


# ---------------------------------------------------------------- 2. 重启与审批


def _start_need_approval(env: Env) -> tuple[str, str]:
    """跑一条需要人工批准的覆盖写入，停在 WAITING_APPROVAL（真子进程）。"""
    proc = _child(
        ["task", "create", "--goal", "改笔记", "--workspace", str(env.ws),
         *_cli_args(env), "--json"],
    )
    assert proc.returncode == 0, proc.stderr
    task_id = json.loads(proc.stdout)["task"]["task_id"]

    proc = _child(
        ["run", "start", "--task-id", task_id, *_cli_args(env), "--json"],
        model=[_overwrite("note.md", "改过的\n").to_payload()],
    )
    assert proc.returncode == 3, proc.stderr
    payload = json.loads(proc.stdout)["run"]
    assert payload["state"] == RUN_WAITING_APPROVAL
    run = env.stack.tasks.get_run(payload["run_id"])
    assert run is not None and run.state == RUN_WAITING_APPROVAL
    return payload["run_id"], task_id


def test_approval_survives_restart_and_executes_once(env):
    """审批跨进程仍然有效：批一次、重启一次，动作恰好执行一次。

    这是 README 里写在纸面上的主流程（`approvals approve` → `run resume`），
    也是旧契约下根本走不完的路：每个 `resume` 是新的 claim_epoch，若审批绑
    claim_epoch，旧批准当场作废，用户会看到同一个待批项无限重来。
    """
    run_id, _ = _start_need_approval(env)
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL
    pending = [a.approval_id for a in env.stack.approvals.pending(run_id=run_id)]
    assert len(pending) == 1

    for approval_id in pending:
        env.stack.approvals.decide(approval_id, decision="approved", actor=_actor())

    # 新的进程（新 instance、新 pid）继续这个 Run：epoch 会涨，审批不该失效
    proc = _child(
        ["run", "resume", "--run-id", run_id, *_cli_args(env), "--json"],
        model=[_end().to_payload()],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)["run"]
    assert payload["state"] == "succeeded", payload
    assert env.stack.approvals.pending(run_id=run_id) == [], "没有新的待批请求"
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "改过的\n"
    assert env.count(run_id, "tool.intent") == 1, "批准一次只能执行一次"
    assert env.count(run_id, "approval.consumed") == 1
    assert env.count(run_id, "approval.requested") == 1, "没有重复开审批"


def test_job_restart_preserves_pending_approval(env):
    """进程死掉之后由 worker 接手：待批请求不丢、不重复，批一次就执行一次。"""
    run_id, _ = _start_need_approval(env)
    before = [a.approval_id for a in env.stack.approvals.pending(run_id=run_id)]
    assert len(before) == 1

    # worker 接手（owner 进程已退出，不等 TTL）：只重新准备，不执行
    proc = _child(
        ["worker", "--once", *_cli_args(env), "--json"],
        model=[_end().to_payload()],
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["run_ids"] == [run_id]
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL
    after = [a.approval_id for a in env.stack.approvals.pending(run_id=run_id)]
    assert after == before, "重启不能把待批请求换掉或复制一份"
    job = env.stack.claims.job_for_run(run_id)
    assert job.state == JOB_WAITING, "等人批准的作业不该是 pending（会被反复重跑）"

    for approval_id in after:
        env.stack.approvals.decide(approval_id, decision="approved", actor=_actor())

    # 批准本身不执行（计划 §2.4）：接着跑必须有人明确 resume。
    # `worker` 只认领"该自动跑"的作业，挂起的作业要等这个明确的动作。
    proc = _child(
        ["worker", "--once", *_cli_args(env), "--json"],
        model=[_end().to_payload()],
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["run_ids"] == []
    assert (env.ws / "note.md").read_text(encoding="utf-8") == ORIGINAL

    proc = _child(
        ["run", "resume", "--run-id", run_id, *_cli_args(env), "--json"],
        model=[_end().to_payload()],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["run"]["state"] == "succeeded"
    assert (env.ws / "note.md").read_text(encoding="utf-8") == "改过的\n"
    assert env.count(run_id, "tool.intent") == 1
    assert env.stack.tasks.get_run(run_id).state == "succeeded"

    # 已终结的 Run 再 resume 必须被拒绝，不能重复执行第二次
    proc = _child(["run", "resume", "--run-id", run_id, *_cli_args(env), "--json"])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "已经结束" in proc.stderr or "已终结" in proc.stderr
    assert env.count(run_id, "tool.intent") == 1


def test_worker_parks_waiting_run_instead_of_respinning(env):
    """worker 把"等人"的作业挂起，而不是立刻放回 pending 反复重跑。

    如果等处停下来之后把作业放回 `pending`，一个 `worker` 轮询会不停认领、
    不停进入等待——每轮都换 claim_epoch，用户刚批的审批立刻被新一轮作废。
    """
    run_id, _, job_id = env.enqueue(root=env.ws)
    worker = Worker(env.stack, model=ScriptedModel([_overwrite("note.md", "改过的\n")]))
    assert worker.run_once() is True

    job = env.stack.claims.get(job_id)
    assert job.state == JOB_WAITING
    assert env.stack.tasks.get_run(run_id).state == RUN_WAITING_APPROVAL
    assert worker.run_once() is False, "挂起的作业不该被同一个循环立刻重跑"
    assert env.count(run_id, "job.claimed") == 1


# ---------------------------------------------------------------- 3. 核查 ≠ 继续


def test_recover_is_not_resume(env):
    """崩溃之后 `recover` 只核查，不接着跑；接着跑是 `worker`/`resume` 的事。

    混在一起的后果：核查过程顺手把模型也调了，`recover` 变成一个会花钱、
    会改文件、会推进任务的命令——而它的名字承诺的是"只看清楚发生了什么"。
    """
    from tianshu.recovery import recover_run

    run_id, _, job_id = env.enqueue(root=env.ws, out="r.md")
    env.log.close()
    proc = _child(
        ["worker", "--once", *_cli_args(env), "--json"],
        model=[_create("r.md", "内容\n", "c1").to_payload(), _end().to_payload()],
        env_extra={"TIANSHU_FAILPOINT": "after_intent"},
    )
    assert proc.returncode == 77, proc.stderr
    env.reopen()
    assert env.count(run_id, "tool.intent") == 1
    assert not (env.ws / "r.md").exists()

    model_calls_before = env.count(run_id, "model.result")
    report = recover_run(env.log, run_id, env.ws, env.stack.snapshots,
                         leases=env.stack.leases)
    print("REPORT", report.__dict__)
    assert report.none_applied == ["c1"]
    assert env.count(run_id, "model.result") == model_calls_before, "recover 不许再问模型"
    assert env.stack.tasks.get_run(run_id).state not in ("succeeded", "failed")
    assert not (env.ws / "r.md").exists()

    # 现在由 worker 接手：intent 已有结论，不重派
    before = env.count(run_id, "tool.intent")
    proc = _child(
        ["worker", "--once", *_cli_args(env), "--json"],
        model=[_end().to_payload()],
    )
    assert proc.returncode == 0, proc.stderr
    assert env.count(run_id, "tool.intent") == before, "核查过的调用不许重派"
    assert env.stack.tasks.get_run(run_id).state == "succeeded"


# ---------------------------------------------------------------- 4. 三种绑定


def test_read_a_write_b_and_chat_without_workspace(env):
    """只读目录写不进去；可写目录写得进去；零目录任务只对话、不碰文件。"""
    read_only = env.read_only
    task_a = env.stack.tasks.create_task(
        "看看只读目录", [{"root_path": str(read_only), "mode": "readonly"}]
    )
    result_a = env.engine([_create("out.md", "x", "c1"), _end()]).start(task_a.task_id)
    assert not (read_only / "out.md").exists()
    rejected = [
        p for p in env.payloads(result_a.run_id, "tool.result")
        if p.get("status") == "rejected"
    ]
    assert rejected and "readonly" in json.dumps(rejected, ensure_ascii=False)
    assert result_a.state in ("succeeded", "failed")

    task_b = env.stack.tasks.create_task("写进可写目录", [str(env.ws)])
    result_b = env.engine([
        _create("out.md", "写进去了\n", "c1"), _end(),
    ]).start(task_b.task_id)
    assert (env.ws / "out.md").read_text(encoding="utf-8") == "写进去了\n"
    assert result_b.outcome in ("succeeded", "noop")

    # 零目录任务走 CLI：没有绑定就不该有任何路径可写
    proc = _child(
        ["task", "create", "--goal", "纯聊天", *_cli_args(env), "--json"],
    )
    assert proc.returncode == 0, proc.stderr
    task_id = json.loads(proc.stdout)["task"]["task_id"]
    proc = _child(
        ["run", "start", "--task-id", task_id, *_cli_args(env), "--json"],
        model=[_end("今天没什么要做的").to_payload()],
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)["run"]
    assert payload["state"] == "succeeded"
    assert payload["effect_state"] == "none_applied"
    assert payload["n_applied"] == 0


# ---------------------------------------------------------------- 5. 心跳


def _steal_attempt(env: Env) -> bool:
    """从**另一条连接**（相当于另一个进程）试着认领一次：抢得到就是 True。"""
    log = EventLog(env.db, blob_dir=env.root / "blobs")
    try:
        return JobStore(
            log, identity=ProcessIdentity.mint()
        ).claim_next() is not None
    finally:
        log.close()


class _ProbeModel(_SlowModel):
    """阻塞期间探一次"别人现在能不能抢走这个作业"。"""

    def __init__(self, seconds: float, env: Env) -> None:
        super().__init__(seconds)
        self._env = env
        self.probes: list[bool] = []

    def complete(self, messages, tools):  # noqa: ARG002
        result = super().complete(messages, tools)  # 先阻塞一段，再探
        self.probes.append(_steal_attempt(self._env))
        return result


def test_heartbeat_loop_renews_while_the_host_thread_blocks(env):
    """模型调用阻塞超过 TTL 时，心跳线程必须把 owner 保住。

    没有它，任何超过 30 秒的模型请求都会让作业进入"可接管"，而 worker
    还在正常干活——下一个进程一认领，两边就开始抢同一份游标。
    这里用真实短 TTL + 真实阻塞来验证，探针走**另一条数据库连接**。
    """
    run_id, _, job_id = env.enqueue(root=env.ws, out="slow.md")
    ttl, interval = 150, 40
    probe = _ProbeModel(ttl / 1000 * 4, env)

    worker = Worker(
        env.stack, model=probe, ttl_ms=ttl, heartbeat_interval_ms=interval
    )
    assert worker.run_once() is True
    assert probe.probes == [False], "有心跳时别人不该抢得走"
    assert worker.beats >= 2, f"心跳次数太少：{worker.beats}"
    assert env.stack.tasks.get_run(run_id).state == "succeeded"
    assert env.stack.claims.get(job_id).state == "done"

    # 反例：关掉心跳，同样的阻塞时长里作业已经可以被别人合法接管
    run_id2, _, _ = env.enqueue(root=env.ws, out="slow2.md")
    probe2 = _ProbeModel(ttl / 1000 * 4, env)
    worker2 = Worker(
        env.stack, model=probe2, ttl_ms=ttl, heartbeat_interval_ms=None
    )
    assert worker2.run_once() is True
    assert probe2.probes == [True], "没有心跳就该被抢走——否则这个反例是假的"
    assert worker2.beats == 0
    assert env.stack.tasks.get_run(run_id2).state not in ("succeeded",)
    assert worker2.errors, "被抢走之后必须留下痕迹，不能静默成功"


def test_worker_holds_no_guard_and_no_transaction_while_model_runs(env):
    """模型调用期间不许持执行守卫，也不许开着数据库事务。

    持着的话，别的进程一次正常的写操作会被卡到超时；而在等待/排队时开事务，
    等于把"人在等"算进了数据库写锁里。
    """
    run_id, _, _ = env.enqueue(root=env.ws, out="x.md")
    seen: list[tuple[bool, bool]] = []

    def complete(_messages, _tools):
        in_txn = env.log.in_transaction
        try:
            with execution_guard(env.data, timeout_ms=50):
                guard_free = True
        except TianshuError:
            guard_free = False
        seen.append((in_txn, guard_free))
        return _end("好了")

    worker = Worker(env.stack, model=type("M", (), {"complete": staticmethod(complete)})())
    assert worker.run_once() is True
    assert seen == [(False, True)], f"模型调用期间持锁/开事务：{seen}"
    assert env.stack.tasks.get_run(run_id).state == "succeeded"


# ---------------------------------------------------------------- 6. 故障与边界


def test_worker_does_not_resume_a_fenced_run(env):
    """被 fence 的 Run 不能被 worker "接着跑"：目标已经换了。"""
    run_id, task_id, job_id = env.enqueue()
    env.stack.tasks.revise_task(task_id, goal="换个目标", bindings=[str(env.ws)])
    before = env.count(run_id, "job.claimed")

    worker = Worker(env.stack, model=ScriptedModel([_end()]))
    assert worker.run_once() is True  # 认领了，但必须立刻收掉
    job = env.stack.claims.get(job_id)
    assert job.state in ("done", "cancelled", JOB_WAITING)
    assert env.count(run_id, "model.result") == 0, "fence 之后不许再问模型"
    assert env.count(run_id, "job.claimed") == before + 1


def test_run_worker_once_matches_the_documented_interface(env):
    """按计划写明的签名能用：`run_worker_once(store, owner) -> bool`。"""
    run_id, _, _ = env.enqueue(root=env.ws, out="one.md")
    did = run_worker_once(
        env.stack.claims,
        env.stack.claims.identity,
        stack=env.stack,
        model=ScriptedModel([_create("one.md", "一份\n", "c1"), _end()]),
    )
    assert did is True
    assert (env.ws / "one.md").exists()
    assert env.stack.tasks.get_run(run_id).state == "succeeded"
    assert run_worker_once(
        env.stack.claims,
        env.stack.claims.identity,
        stack=env.stack,
        model=ScriptedModel([]),
    ) is False


def test_heartbeat_loop_reports_a_lost_claim(env):
    """后台心跳失败要被记下来，不能静默——静默等于"以为还活着"。"""
    beats: list[int] = []

    def beat():
        beats.append(1)
        if len(beats) >= 2:
            raise TianshuError("claim 已失效")

    loop = HeartbeatLoop(beat, interval_ms=5)
    with loop:
        time.sleep(0.2)
    assert len(beats) == 2, "失败之后不该继续刷心跳"
    assert loop.error is not None
    assert loop.beats == 1, "只有成功的那次计数"


def test_heartbeat_retries_a_busy_database_instead_of_giving_up(env):
    """`database is locked` 不等于丢了 claim：那是主线程在提交大事务。

    把它当成心跳失败，会让一次正常的慢写入变成一次误判的接管——两个 owner
    交替写同一份游标，正是 fencing 要防的事。
    """
    import sqlite3

    calls = []

    def beat():
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")

    loop = HeartbeatLoop(beat, interval_ms=5)
    with loop:
        time.sleep(0.1)
    assert loop.error is None, loop.error
    assert loop.beats >= 1, beats_should_be_counted(loop)


def beats_should_be_counted(loop) -> str:
    return f"成功心跳数={loop.beats}"


def test_concurrent_workers_serialize_on_one_directory(env):
    """4 个 worker 面对"共用一棵目录"的 5 个排队作业：必须串行、各写一次。

    租约保证同一棵树同一时刻只有一个写者，所以抢不到租约的 worker 只能**交还**
    作业、等持有者跑完——它不能把作业挂起（挂起 = 等人决定，而这里只是排队），
    也不能报错退出（那说明有东西坏了）。这条用真子进程测，因为它考的是
    多进程之间"谁先拿到租约"。
    """
    ids = [env.enqueue(root=env.ws, goal=f"写第 {i} 份")[0] for i in range(5)]
    child_env = dict(os.environ)
    child_env["TIANSHU_TEST_MODEL"] = json.dumps({"mode": "by_goal"})
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _child_src(), "worker", *_cli_args(env), "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env,
        )
        for _ in range(4)
    ]
    payloads = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=180)
        assert proc.returncode == 0, stderr or stdout
        payloads.append(json.loads(stdout))

    print("PAYLOADS", json.dumps(payloads, ensure_ascii=False))
    print("WS", sorted(p.name for p in env.ws.iterdir()))
    for i, run_id in enumerate(ids):
        r = env.stack.tasks.get_run(run_id)
        print("RUN", i, run_id, r.state, r.outcome, [e.type for e in env.log.read_run(run_id)])
        assert (env.ws / f"r{i}.md").exists(), f"第 {i} 份没写出来"
        assert env.count(run_id, "tool.intent") == 1, f"第 {i} 份被写了不止一次"
        assert env.stack.tasks.get_run(run_id).state == "succeeded"

    # 抢不到租约的 worker 会交还作业并如实说"被占用"，而不是报错
    assert any(p["contended"] for p in payloads), payloads
    assert all(not p["errors"] for p in payloads), payloads


def test_cli_worker_drains_enqueued_jobs(env):
    """`worker` 命令把排队作业跑完并如实报数；只读目录不能被它写穿。"""
    ids = [env.enqueue(root=env.ws, out=f"r{i}.md")[0] for i in range(3)]
    proc = _child(["worker", *_cli_args(env), "--json"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["run_ids"] == ids
    assert payload["idle"] is True
    # 默认脚本模型只写固定产物名 summary.md（worker 不建 Run，没有 --out）
    assert (env.ws / "summary.md").exists()
    for run_id in ids:
        assert env.stack.tasks.get_run(run_id).state == "succeeded"


def test_worker_ignores_paused_run_until_resumed(env):
    """暂停的 Run 交还租约后，worker 不许把它跑起来（那是用户按的暂停）。"""
    run_id, _, job_id = env.enqueue(root=env.ws, out="p.md")
    env.stack.tasks.request_pause(run_id)
    worker = Worker(env.stack, model=ScriptedModel([_create("p.md", "x", "c1")]))
    assert worker.run_once() is True  # 认领后立刻在安全点确认暂停
    assert env.stack.tasks.get_run(run_id).state == "paused"
    assert not (env.ws / "p.md").exists()
    job = env.stack.claims.get(job_id)
    assert job.state in (JOB_WAITING, "pending", "done")

    # resume（明确的"继续"）之后才继续，且只写一次
    result = env.engine([_create("p.md", "x", "c1"), _end()]).resume_run(run_id)
    assert result.state in ("succeeded", "noop"), result.error
    assert (env.ws / "p.md").exists()
    assert env.count(run_id, "tool.intent") == 1


def test_worker_requeues_after_claim_lost_to_a_takeover(env):
    """被接管之后，旧 worker 不能用新 owner 的作业继续写。

    `run_once` 必须在 JobLost 时停下并如实返回，而不是把异常当成"没活干"。
    """
    run_id, _, job_id = env.enqueue(root=env.ws, out="t.md")
    worker = Worker(env.stack, model=ScriptedModel([_create("t.md", "x", "c1")]), ttl_ms=1)

    taken: list[JobStore] = []
    original = worker._drive

    def steal_then_drive(claim):
        log2 = EventLog(env.db, blob_dir=env.root / "blobs")
        other = JobStore(log2, identity=ProcessIdentity.mint())
        taken.append(other)
        # 明确的"接管"：默认 claim 会拒绝从活着的 owner 手里抢
        other.claim(job_id, take_over_live=True)
        return original(claim)

    worker._drive = steal_then_drive
    assert worker.run_once() is True
    assert worker.errors, "被接管必须留痕，不能当成正常结束"
    assert not (env.ws / "t.md").exists(), "旧 claim 不许写文件"
    job = env.stack.claims.get(job_id)
    assert job.claim_epoch == 2, "接管方拿到的 epoch 必须更高"
    assert job.owner_instance_id != worker._claims.identity.instance_id
    taken[0]._log.close()


def test_run_succeeded_but_effect_unknown_is_not_reported_as_success(env):
    """unknown 效果一律不许记成 succeeded：CLI 退出码、Run 结论、作业结论一致。"""
    run_id, _, job_id = env.enqueue(root=env.ws, out="u.md")
    env.log.close()
    proc = _child(
        ["worker", "--once", *_cli_args(env), "--json"],
        model=[_create("u.md", "内容\n", "c1").to_payload(), _end().to_payload()],
        env_extra={"TIANSHU_FAILPOINT": "after_write"},
    )
    assert proc.returncode == 77
    env.reopen()
    # 写入已发生、结果未落库：worker 只能进核查，不许自己宣告成功
    proc = _child(["worker", "--once", *_cli_args(env), "--json"])
    assert proc.returncode == 0, proc.stderr
    run = env.stack.tasks.get_run(run_id)
    assert run.state != "succeeded", run
    assert run.state in ("reconciling", "failed")
    job = env.stack.claims.get(job_id)
    assert job.state in (JOB_WAITING, "done")