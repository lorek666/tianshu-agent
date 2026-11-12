"""MCP 崩溃夹具与恢复判据（G2 MCP 阶段：c10 的实测、NB21 的负控对象）。

对端是 `tests/fixtures/fake_mcp_server.py` **真子进程**，跑 Run 的也是真子进程
（CLI）。崩溃是 `os._exit(77)` 硬退：不走清理、不给恢复代码机会。它与 `kill -9`
在"进程死在这一步"这个意义上等价——**不**证明内核缓冲里的字节真的丢了（这一
口径与 `docs/powercut-evidence.md` 相同）。

三个窗口，各自的结论**不相等**，这是本阶段的全部要点：

- `mcp_before_call`（intent/dispatches 已提交，**外发边界标记还没落盘**）：请求
  一定没写进 server 的 stdin。恢复可以判 `none_applied` + 不阻断——它的判据
  是发送**之前**提交的那一行 `dispatches.state=executing`。
- `mcp_marked`（标记已落盘，请求还没写）：恢复**没法**证明没发出去。它只能保
  守判 `unknown` + 阻断。这一小段窗口是有意付出的代价：宁可多挡一个目录，也不
  把可能已经发生的副作用说成没发生。
- `mcp_after_call`（响应已收到；`tool.result` / 凭据 / 终态都没落盘）：真正的
  "可能已生效但结论丢了"。server 的 `--effect-log` 证明它确实执行了。同样是
  `unknown` + 阻断，**不是**重放、也不是"没证据=没写"。

另外几条用例不崩溃，测的是**不该被重判 / 不该留下互相矛盾的说法**：

- `isError: true` 的调用已经有 `tool.result`：结论是 `applied`（确实执行过）+
  `status=failed` + 凭据。恢复的判据是"结果事件有没有提交"，不是 server 回了
  什么，所以恢复必须一条都不改。
- 文件写工具的恢复终态：`applied` → 派发行 `done`，其余 → `error`。只写结果
  不动派发行，`dispatch_record()` 会继续显示 `intent/executing`——同一件事在事件
  里是"已结算"、在派发表里是"仍在执行"（复审 R2）。
- `revoked`/`rejected` 的派发行是审计事实：MCP 的结论可以按"没越过外发边界"
  判 `none_applied`，但那行**不许**被改写（撤销过就该一直看得出撤销过）。
- MCP 工具 id 被误塞进静态 `SPECS` 时，恢复仍必须按 MCP 结算（不查磁盘）。身份
  由 id 形状决定，不靠"静态表里没有它"。
- 发包前撤销：标记推进不上就**不发**，结论是确定的 `rejected`/`none_applied`。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import CHILD_TEXT, approve_pending, engine_for

from tianshu.dispatcher import (
    DISPATCH_ERROR,
    DISPATCH_EXECUTING,
    DISPATCH_FINISHED,
    DISPATCH_INTENT,
)
from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces, ensure_not_blocked, unblock
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ACTION_OVERWRITE, ApprovalError, HumanIdentity
from tianshu.receipt import verify_receipts
from tianshu.recovery import assess_run, recover_run
from tianshu.snapshot import SnapshotStore
from tianshu.tools import SPECS, ToolSpec

SRC = str(Path(__file__).resolve().parents[1] / "src")
FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"

CALL_ID = "c1"
MCP_TOOL = "mcp__fake__echo"


# ---------------------------------------------------------------- 夹具


def _tool_call() -> ModelResponse:
    return ModelResponse(
        "tool_use", tool_calls=[ToolCall(CALL_ID, MCP_TOOL, {"message": "hi"})]
    )


def _end() -> ModelResponse:
    return ModelResponse("end_turn", content="完成了")


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@dataclass
class Env:
    root: Path
    ws: Path
    data: Path
    db: Path
    call_log: Path
    effect_log: Path
    config: Path
    log: EventLog
    stack: object
    task_id: str

    @property
    def ws_root(self) -> str:
        return str(self.ws.resolve())

    def cli(self) -> list[str]:
        return ["--db", str(self.db), "--data-root", str(self.data)]

    def reopen(self) -> None:
        """模拟"另一个进程接手"：换连接、换整套 store（同 `test_m2_acceptance`）。"""
        self.log.close()
        self.log = EventLog(self.db, blob_dir=self.root / "blobs")
        self.stack = build_stack(self.log, data_root=self.data)

    def calls(self) -> list[dict]:
        return _lines(self.call_log)

    def effects(self) -> list[dict]:
        return _lines(self.effect_log)

    def types(self, run_id: str) -> list[str]:
        return [e.type for e in self.log.read_run(run_id, verify=False)]

    def payloads(self, run_id: str, event_type: str) -> list[dict]:
        return [
            e.payload
            for e in self.log.read_run(run_id, verify=False)
            if e.type == event_type
        ]


def _config(mode: str, *, call_log: Path, effect_log: Path, cwd: Path) -> McpServerConfig:
    return McpServerConfig(
        name="fake",
        command=(
            sys.executable,
            str(FIXTURE),
            "--mode",
            mode,
            "--call-log",
            str(call_log),
            "--effect-log",
            str(effect_log),
        ),
        cwd=str(cwd),
    )


def _env(tmp_path: Path, *, mode: str = "normal") -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_bytes(b"note\n")
    data = tmp_path / "authority"
    data.mkdir()
    db = tmp_path / "events.db"
    log = EventLog(db, blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=data)
    call_log = tmp_path / "calls.jsonl"
    effect_log = tmp_path / "effects.jsonl"
    config_path = tmp_path / "mcp.json"
    command = _config(
        mode, call_log=call_log, effect_log=effect_log, cwd=tmp_path
    ).command
    config_path.write_text(
        json.dumps(
            {"servers": {"fake": {"command": list(command), "cwd": str(tmp_path)}}}
        ),
        encoding="utf-8",
    )
    task = stack.tasks.create_task(
        "崩溃后核对 MCP 调用", [str(ws)], mcp_servers=["fake"]
    )
    return Env(
        root=tmp_path,
        ws=ws,
        data=data,
        db=db,
        call_log=call_log,
        effect_log=effect_log,
        config=config_path,
        log=log,
        stack=stack,
        task_id=task.task_id,
    )


# 子进程入口：真 CLI + 脚本模型。模型响应由 `TIANSHU_TEST_MODEL` 传进去，
# 其余（MCP 配置、事件库、数据根）都走产品自己的参数/环境变量。
CHILD = """
import json, os, sys
sys.path.insert(0, __SRC__)
import tianshu.cli as cli
from tianshu.model import ModelResponse, ScriptedModel, ToolCall

spec = os.environ.get("TIANSHU_TEST_MODEL")
if spec:
    data = json.loads(spec)
    responses = [
        ModelResponse(
            item["finish_reason"],
            content=item.get("content", ""),
            tool_calls=[
                ToolCall(c["call_id"], c["name"], c["arguments"])
                for c in item.get("tool_calls", [])
            ],
        )
        for item in data["responses"]
    ]
    cli._scripted_model = lambda out_name: ScriptedModel(list(responses))

sys.exit(cli.main(sys.argv[1:]))
"""


def _child_src() -> str:
    return CHILD.replace("__SRC__", repr(SRC))


def _child(env: Env, argv: list[str], *, responses, env_extra=None, timeout=180):
    environment = dict(os.environ)
    for key in (
        "TIANSHU_TEST_MODEL",
        "TIANSHU_FAILPOINT",
        "TIANSHU_FAILPOINT_PATH",
        "TIANSHU_MCP_CONFIG",
    ):
        environment.pop(key, None)
    environment["TIANSHU_TEST_MODEL"] = json.dumps(
        {"responses": [r.to_payload() for r in responses]}
    )
    environment["TIANSHU_MCP_CONFIG"] = str(env.config)
    environment.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", _child_src(), *argv],
        capture_output=True,
        **CHILD_TEXT,
        timeout=timeout,
        env=environment,
    )


def _start(env: Env) -> str:
    """跑 `run start`：模型要的 MCP 调用必须先弹人工批准，所以停在 waiting。"""
    proc = _child(
        env,
        ["run", "start", "--task-id", env.task_id, "--out", "out.md",
         *env.cli(), "--json"],
        responses=[_tool_call(), _end()],
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["run"]["state"] == "waiting_approval"
    return payload["run"]["run_id"]


def _approve(env: Env, run_id: str) -> list[str]:
    """父进程替人按下 approve（同 `support.approve_pending`，但要先重开连接）。"""
    env.reopen()
    actor = HumanIdentity.local_cli()
    ids = [a.approval_id for a in env.stack.approvals.pending(run_id=run_id)]
    for approval_id in ids:
        env.stack.approvals.decide(approval_id, decision="approved", actor=actor)
    return ids


def _resume(env: Env, run_id: str, *, responses, env_extra=None):
    return _child(
        env,
        ["run", "resume", "--run-id", run_id, *env.cli(), "--json"],
        responses=responses,
        env_extra=env_extra,
    )


def _recover(env: Env, run_id: str):
    return recover_run(
        env.log,
        run_id,
        env.ws,
        env.stack.snapshots,
        leases=env.stack.leases,
        data_root=env.data,
        memory=env.stack.memory,
        blob_dir=env.root / "blobs",
    )


def _dispatch_state(env: Env, run_id: str) -> str:
    record = env.stack.dispatcher.dispatch_record(run_id, CALL_ID)
    assert record is not None, "这次调用应当已经派发过"
    return record.state


# ---------------------------------------------------------------- 崩溃窗口


def _crash_after_call(tmp_path: Path) -> tuple[Env, str]:
    """在"响应已收到、结果未落盘"处硬退，返回崩溃后的现场。"""
    env = _env(tmp_path)
    run_id = _start(env)
    approved = _approve(env, run_id)
    assert approved, "MCP 调用必须先弹人工批准"
    proc = _resume(
        env, run_id, responses=[_end()],
        env_extra={"TIANSHU_FAILPOINT": "mcp_after_call"},
    )
    assert proc.returncode == 77, (
        "应当在 mcp_after_call 处硬退，实际 "
        f"{proc.returncode}；stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    # 退出码 77 不够：要确认炸的是**这个**注入点，而不是别的路径碰巧 77。
    assert "[failpoint] mcp_after_call" in proc.stderr, proc.stderr
    env.reopen()
    return env, run_id


def test_crash_after_server_executed_settles_unknown_and_blocks(tmp_path):
    """响应已收到但结果没落盘：只能 unknown + 阻断，且不许重放。

    这条是 c10 的核心：库里只有 `tool.intent`，没有 `tool.result`；server 的
    effect-log 证明它**真的执行过**。任何"没证据=没写"的推理都会把一次可能已
    经发生的副作用洗成 `none_applied`。
    """
    env, run_id = _crash_after_call(tmp_path)
    try:
        assert env.types(run_id).count("tool.intent") == 1
        assert env.types(run_id).count("tool.result") == 0
        # 请求确实到达并真的执行了（call-log 只证明收到，effect-log 证明执行）
        assert len(env.calls()) == 1
        assert len(env.effects()) == 1
        assert env.effects()[0]["name"] == "echo"
        # 崩在派发中途：外发边界标记已落盘（这次调用确实"可能已经发出"）
        assert _dispatch_state(env, run_id) == DISPATCH_EXECUTING

        # 只读对账：拒绝接手、报告 unresolved，且不落任何事件
        before = len(env.types(run_id))
        assessment = assess_run(env.data, run_id, log=env.log)
        assert assessment.unresolved == (CALL_ID,)
        assert assessment.blocks_takeover is True
        assert len(env.types(run_id)) == before, "只读对账不许写事件"

        report = _recover(env, run_id)
        assert report.unresolved == [CALL_ID]
        assert report.none_applied == [] and report.applied == []
        assert [str(Path(p).resolve()) for p in report.blocked_workspaces] == [
            env.ws_root
        ]
        assert not report.ok

        results = env.payloads(run_id, "tool.result")
        assert len(results) == 1
        result = results[0]
        assert result["status"] == "unknown"
        assert result["effect_state"] == "unknown"
        output = result["output"]
        assert output["recovered"] is True
        assert output["reason"] == "mcp.crash_unsettled"
        assert output["tool_id"] == MCP_TOOL
        assert output["mcp"]["lookup"] == "tianshu mcp list"
        # 不编造执行结果：没有 after_state / sha256 / 正文
        assert "after_state" not in output
        assert "sha256" not in output
        # 不补凭据（unknown 不满足 applied）
        assert env.types(run_id).count("receipt") == 0
        # 已结算的派发行不能继续显示"仍在执行"
        assert _dispatch_state(env, run_id) == DISPATCH_ERROR
        # 阻断要落在目录上，人能看到并解除
        assert env.ws_root in blocked_workspaces(env.log)
        with pytest.raises(WorkspaceBlocked):
            ensure_not_blocked(env.log, env.ws)

        # 幂等：再恢复一次，不新增结果/凭据/阻断，也不再碰 server
        report2 = _recover(env, run_id)
        assert report2.unresolved == [] and report2.ok
        assert env.types(run_id).count("tool.result") == 1
        assert env.types(run_id).count("recovery.blocked") == 1
        assert env.types(run_id).count("receipt") == 0
        assert len(env.calls()) == 1, "恢复不许重放调用"
    finally:
        env.log.close()


def test_crash_before_send_settles_none_applied_and_does_not_block(tmp_path):
    """外发边界之前崩掉：请求一定没写出 → `none_applied`，不阻断、可直接接着跑。

    判据不是"日志里没有结果所以就当没发生"，而是**发送之前**提交的那一行
    `dispatches.state=executing`（`dispatcher._mark_mcp_send_boundary`）：崩溃
    发生在标记落盘之前，行还停在 `intent`。server 的 call-log 也是空的——请求
    确实没到。
    """
    env = _env(tmp_path)
    try:
        run_id = _start(env)
        assert _approve(env, run_id), "MCP 调用必须先弹人工批准"
        proc = _resume(
            env, run_id, responses=[_end()],
            env_extra={"TIANSHU_FAILPOINT": "mcp_before_call"},
        )
        assert proc.returncode == 77, proc.stdout + proc.stderr
        assert "[failpoint] mcp_before_call" in proc.stderr, proc.stderr
        env.reopen()

        assert env.types(run_id).count("tool.intent") == 1
        assert env.types(run_id).count("tool.result") == 0
        # 一个字节都没写进 server：call-log 与 effect-log 都是空的
        assert env.calls() == []
        assert env.effects() == []
        # 外发边界标记没有落盘 → 行还停在 `intent`
        assert _dispatch_state(env, run_id) == DISPATCH_INTENT

        # 只读对账也要能判"没发"：不拒绝接手，也不落事件
        before = len(env.types(run_id))
        assessment = assess_run(env.data, run_id, log=env.log)
        assert assessment.none_applied == (CALL_ID,)
        assert assessment.unresolved == ()
        assert assessment.blocks_takeover is False
        assert len(env.types(run_id)) == before, "只读对账不许写事件"

        report = _recover(env, run_id)
        assert report.none_applied == [CALL_ID]
        assert report.unresolved == [] and report.applied == []
        assert report.blocked_workspaces == [], "没发出去的调用不该挡任何目录"
        assert report.ok, report.problems
        results = env.payloads(run_id, "tool.result")
        assert len(results) == 1
        result = results[0]
        assert result["status"] == "failed"
        assert result["effect_state"] == "none_applied"
        output = result["output"]
        assert output["recovered"] is True
        assert output["reason"] == "mcp.not_sent"
        assert output["tool_id"] == MCP_TOOL
        # 不补凭据：没越过边界的调用没有可归因的效果
        assert env.types(run_id).count("receipt") == 0
        # 已结算的派发行不能继续显示"还没结算"
        assert _dispatch_state(env, run_id) == DISPATCH_ERROR
        assert env.ws_root not in blocked_workspaces(env.log)
        ensure_not_blocked(env.log, env.ws)

        # 幂等：再恢复一次，不新增结果/凭据/阻断
        report2 = _recover(env, run_id)
        assert report2.ok and report2.none_applied == []
        assert env.types(run_id).count("tool.result") == 1
        assert env.types(run_id).count("recovery.blocked") == 0

        # 不用人工解除阻断就能接着跑：不重放、也不再打 server
        proc = _resume(env, run_id, responses=[_end()])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        env.reopen()
        assert env.types(run_id).count("tool.intent") == 1, "不许重派"
        assert len(env.calls()) == 0, "恢复/续跑都不许再打 server"
        assert env.stack.tasks.get_run(run_id).state == "succeeded"
    finally:
        env.log.close()


def test_crash_after_marker_conservatively_blocks_and_refuses_revoke(tmp_path):
    """标记已落盘、请求还没写：恢复没法证明"没发"，只能保守 unknown + 阻断。

    这一小段窗口是有意付出的代价：宁可多挡一个目录，也不把可能已经发生的副作
    用说成没发生。它同时钉住另一半——派发行一旦越过外发边界，**撤销**就不能再
    假装成功，否则"撤销成功"这句话会盖住一个可能已经发出的请求。
    """
    env = _env(tmp_path)
    try:
        run_id = _start(env)
        approved = _approve(env, run_id)
        assert approved, "MCP 调用必须先弹人工批准"
        proc = _resume(
            env, run_id, responses=[_end()],
            env_extra={"TIANSHU_FAILPOINT": "mcp_marked"},
        )
        assert proc.returncode == 77, proc.stdout + proc.stderr
        assert "[failpoint] mcp_marked" in proc.stderr, proc.stderr
        env.reopen()

        assert env.types(run_id).count("tool.intent") == 1
        assert env.types(run_id).count("tool.result") == 0
        # 请求还没写进 server（call-log 空），但外发边界标记已经落盘
        assert env.calls() == []
        assert env.effects() == []
        assert _dispatch_state(env, run_id) == DISPATCH_EXECUTING

        # 越过外发边界之后，撤销必须报错，而不是"撤销成功"
        with pytest.raises(ApprovalError):
            env.stack.dispatcher.revoke(
                approved[0], actor=HumanIdentity.local_cli(), reason="想撤掉"
            )

        # 只读对账：无从证明没发 → 拒绝接手
        assessment = assess_run(env.data, run_id, log=env.log)
        assert assessment.unresolved == (CALL_ID,)
        assert assessment.blocks_takeover is True

        report = _recover(env, run_id)
        assert report.unresolved == [CALL_ID], "证明不了没发 = 按可能已发处理"
        assert report.none_applied == []
        result = env.payloads(run_id, "tool.result")[0]
        assert result["effect_state"] == "unknown"
        assert result["output"]["reason"] == "mcp.crash_unsettled"
        assert _dispatch_state(env, run_id) == DISPATCH_ERROR
        assert env.ws_root in blocked_workspaces(env.log)
    finally:
        env.log.close()


def test_blocked_workspace_can_be_unblocked_and_run_finishes_without_replay(tmp_path):
    """人工解除阻断后接着跑：不重放、不把 unknown 洗掉。

    "接着跑"必须由人显式 `unblock`——这条用例同时把解除路径走通（否则阻断就
    变成了"这个目录永久报废"），并证明恢复给出的结论在收尾后依然是 unknown。
    """
    env, run_id = _crash_after_call(tmp_path)
    try:
        _recover(env, run_id)
        assert env.ws_root in blocked_workspaces(env.log)

        env.reopen()
        unblock(env.log, env.ws, reason="人工核对过 server 的输出，确认没改本地文件")
        assert env.ws_root not in blocked_workspaces(env.log)

        proc = _resume(env, run_id, responses=[_end()])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        env.reopen()
        record = env.stack.tasks.get_run(run_id)
        assert record.effect_state == "unknown", "恢复给的结论不许被收尾洗掉"
        assert env.types(run_id).count("tool.intent") == 1, "不许重派"
        assert len(env.calls()) == 1, "不许再打一次 server"
    finally:
        env.log.close()


# ---------------------------------------------------------------- 不该被重判


def test_ise_error_result_is_applied_and_recovery_leaves_it_alone(tmp_path):
    """`isError:true` 是"执行过且失败"，不是 `unknown`：恢复一条都不改。

    判据是"`tool.result` 有没有提交"：提交了 → 恢复的 pending 集合里没有它。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    clients = McpClients(
        {
            "fake": _config(
                "is_error",
                call_log=tmp_path / "calls.jsonl",
                effect_log=tmp_path / "effects.jsonl",
                cwd=tmp_path,
            )
        }
    )
    try:
        stack = build_stack(log, data_root=data, mcp_clients=clients)
        task = stack.tasks.create_task(
            "isError 的调用", [str(ws)], mcp_servers=["fake"]
        )
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)
        assert result.state == "succeeded", result
        assert result.effect_state == "applied"
        run_id = result.run_id

        rows = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert len(rows) == 1
        assert rows[0]["status"] == "failed" and rows[0]["effect_state"] == "applied"
        receipts = [
            e for e in log.read_run(run_id, verify=False) if e.type == "receipt"
        ]
        assert len(receipts) == 1
        assert receipts[0].payload["backend"] == "mcp"
        assert verify_receipts(log, run_id) == []
        assert (
            stack.dispatcher.dispatch_record(run_id, CALL_ID).state
            == DISPATCH_FINISHED
        )

        # 恢复：没有待决调用 → 什么都不改
        report = recover_run(
            log, run_id, ws, stack.snapshots,
            leases=stack.leases, data_root=data, memory=stack.memory,
            blob_dir=tmp_path / "blobs",
        )
        assert report.ok and report.unresolved == [] and report.applied == []
        after = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert len(after) == 1 and after[0]["effect_state"] == "applied", "不许重判"
    finally:
        clients.close_all()
        log.close()


#: 派发表的最小形状：恢复只推进状态，不模拟整个派发。夹具用一条手写行，
#: 是为了让 "派发行有没有被推进" 这件事单独可测（不被真实派发的其他副作用干扰）。
_DISPATCH_INSERT = (
    "INSERT INTO dispatches (dispatch_id, run_id, task_id, call_id,"
    " request_id, request_digest, tool_id, action, resources, target,"
    " rel_path, workspace_root, resource_key, approval_id, grant_id,"
    " claim_epoch, owner_instance_id, attempt_id, intent_seq, state,"
    " created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_NOW = 1_700_000_000_000


def _insert_dispatch(
    log: EventLog,
    *,
    dispatch_id: str,
    run_id: str,
    tool_id: str,
    target: str,
    intent_seq: int,
    state: str = "intent",
) -> None:
    log.conn.execute(
        _DISPATCH_INSERT,
        (
            dispatch_id, run_id, "task_x", CALL_ID, "req_x", "0" * 64, tool_id,
            ACTION_OVERWRITE, "[]", target, "", "", f"key:{target}", None,
            "grant_x", 1, "inst_x", 1, intent_seq, state, _NOW, _NOW,
        ),
    )


@pytest.mark.parametrize("dispatch_state", ["intent", "executing"])
@pytest.mark.parametrize("side_effect_level", ["idempotent", "pure"])
def test_mcp_tool_in_static_specs_is_still_not_treated_as_a_file_tool(
    tmp_path, monkeypatch, side_effect_level, dispatch_state
):
    """MCP id 万一进了静态 `SPECS`，恢复仍按 MCP 判（不读磁盘）。

    "不在 `SPECS` 里"是个会变的前提：有人把动态 id 塞进静态表、或注册表装配错
    了，恢复就会去查快照清单。这个夹具的清单**存在但没有本次调用条目**——按文件
    工具的口径那就等于"确定没写"。所以只要 MCP 判据被绕开，两种派发行状态都会
    露出马脚：

    - `executing`（越过外发边界）：必须 `unknown` + 阻断，不能按空清单判
      `none_applied`（那会把一次可能已生效的调用洗掉）；
    - `intent`（没越过边界）：结论可以 `none_applied`，但 reason 必须是
      `mcp.not_sent`——不能是"清单里没有条目"那条文件工具的推理。
    """
    tag = f"{side_effect_level}-{dispatch_state}"
    ws = tmp_path / f"ws-{tag}"
    ws.mkdir()
    data = tmp_path / f"data-{tag}"
    data.mkdir()
    log = EventLog(tmp_path / f"events-{tag}.db", blob_dir=tmp_path / "blobs")
    artifacts = tmp_path / f"artifacts-{tag}"
    artifacts.mkdir()
    store = SnapshotStore(artifacts)
    try:
        stack = build_stack(log, data_root=data)
        run_id = f"run_mcp_static_{tag}"
        store.init_run(run_id)  # 清单存在但**没有**本次调用条目
        log.append(
            run_id,
            "run.start",
            {"task": "MCP 静态表误装", "workspace": str(ws), "out": "out.md",
             "limits": {}, "tools": []},
            actor_kind="core",
            legacy_write=True,
        )
        seq = log.append(
            run_id,
            "tool.intent",
            {"call_id": CALL_ID, "tool_id": MCP_TOOL, "args": {"message": "hi"},
             "args_hash": "0" * 64},
            actor_kind="core",
            legacy_write=True,
        )
        _insert_dispatch(
            log, dispatch_id="dsp_mcp", run_id=run_id, tool_id=MCP_TOOL,
            target="mcp://fake/echo", intent_seq=seq, state=dispatch_state,
        )
        monkeypatch.setitem(
            SPECS,
            MCP_TOOL,
            ToolSpec(
                tool_id=MCP_TOOL,
                version="9.9.9",
                description="故意塞进静态表的 MCP 工具（NB21 的变异形状）",
                input_schema={"type": "object", "properties": {},
                              "additionalProperties": False},
                side_effect_level=side_effect_level,
                action=ACTION_OVERWRITE,
            ),
        )
        assert MCP_TOOL in SPECS, "变异必须真的生效，否则这条用例什么都没测"

        # 只读对账也要按 MCP 判（它是**另一条分支**：撤了它，正常崩溃用例里
        # MCP id 不在静态表时仍会 unresolved，测不出来）。
        before = len(log.read_run(run_id, verify=False))
        assessment = assess_run(data, run_id, log=log)
        if dispatch_state == "executing":
            assert assessment.unresolved == (CALL_ID,)
            assert assessment.none_applied == ()
            assert assessment.blocks_takeover is True
        else:
            assert assessment.none_applied == (CALL_ID,)
            assert assessment.unresolved == ()
            assert assessment.blocks_takeover is False
        assert len(log.read_run(run_id, verify=False)) == before, "只读对账不许写事件"

        report = recover_run(log, run_id, ws, store, leases=stack.leases,
                             data_root=data)
        results = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert len(results) == 1
        assert results[0]["tool_id"] == MCP_TOOL
        if dispatch_state == "executing":
            assert report.unresolved == [CALL_ID], (
                "即使静态表里出现了这个 id，也只能按 MCP 判 unknown"
            )
            assert report.none_applied == [], (
                "空清单里没有本次调用的条目，不能拿来证明 MCP 没执行"
            )
            assert results[0]["output"]["reason"] == "mcp.crash_unsettled"
            assert report.blocked_workspaces == [str(ws.resolve())]
        else:
            assert report.none_applied == [CALL_ID]
            assert report.unresolved == []
            assert results[0]["output"]["reason"] == "mcp.not_sent", (
                "空清单不是判据：没发出这条结论来自外发边界标记"
            )
            assert report.blocked_workspaces == []
    finally:
        log.close()


def test_negative_control_script_refuses_unknown_case_ids():
    """负控脚本不许"静默跳过"：写错编号要返回 1，不能打印"全部变红"。

    复审第三轮抓到的假红入口之一。脚本本身跑真用例（要几十秒），所以这里只测
    **参数校验**这一段：`main(["NOT_A_CASE"])` 必须在跑任何用例之前拒绝。
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "negative_controls_mcp_crash.py"
    spec = importlib.util.spec_from_file_location("nc_mcp_crash", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在 exec 时要能从 `sys.modules` 找到自己的模块（否则
    # `_process_class` 解析注解会 KeyError），所以先登记、用完再清。
    sys.modules["nc_mcp_crash"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("nc_mcp_crash", None)
    # 只测参数校验这一段（跑用例要几十秒，测试里不跑）
    wanted, unknown = module._select(["NOT_A_CASE"])
    assert unknown == ["NOT_A_CASE"], "不认识的编号必须被点出来"
    assert wanted == {"NOT_A_CASE"}, "不认识的编号不许被悄悄换成别的"
    wanted, unknown = module._select([])
    assert unknown == []
    assert wanted == {case.name for case in module.CASES}, "空参数 = 全跑"
    assert module.main(["NOT_A_CASE"]) == 1, "拒绝时进程退出码必须是 1"


def test_recovery_does_not_rewrite_terminal_dispatch_rows(tmp_path):
    """恢复只推进 `intent/executing`；`revoked` / `rejected` 这些终态保留。

    复审 R2-2：撤销先于结算时把 `revoked` 改成 `error`（或 `applied` 改成
    `done`），等于在审计里抹掉"这次派发被撤销过"这条事实。结果照写，行不动。

    对 MCP 还多一层：`revoked`/`rejected` 同时是**"请求没越过外发边界"**的证据
    （标记只从 `intent` 推进），所以结论从 `unknown` 变成了确定的
    `none_applied`——行本身依旧保持终态。
    """
    # (a) 文件写：行已 revoked，磁盘却是写后状态 → 结果 applied，行仍 revoked
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    store = SnapshotStore(artifacts)
    try:
        stack = build_stack(log, data_root=data)
        run_id = "run_revoked"
        store.init_run(run_id)
        before = b"before\n"
        after = b"after\n"
        (ws / "report.md").write_bytes(before)
        store.record_preimage(
            run_id, CALL_ID, "report.md", before,
            hashlib.sha256(after).hexdigest(), root=ws,
        )
        log.append(
            run_id, "run.start",
            {"task": "撤销先于结算", "workspace": str(ws), "out": "out.md",
             "limits": {}, "tools": []},
            actor_kind="core", legacy_write=True,
        )
        seq = log.append(
            run_id, "tool.intent",
            {"call_id": CALL_ID, "tool_id": "create_report",
             "args": {"name": "report.md", "content": "after\n"},
             "args_hash": "0" * 64},
            actor_kind="core", legacy_write=True,
        )
        _insert_dispatch(
            log, dispatch_id="dsp_revoked", run_id=run_id, tool_id="create_report",
            target=str(ws / "report.md"), intent_seq=seq, state="revoked",
        )
        (ws / "report.md").write_bytes(after)

        report = recover_run(log, run_id, ws, store, leases=stack.leases,
                             data_root=data)
        assert report.applied == [CALL_ID], "结果照写（磁盘确实是写后状态）"
        row = log.conn.execute(
            "SELECT state FROM dispatches WHERE run_id=? AND call_id=?",
            (run_id, CALL_ID),
        ).fetchone()
        assert row["state"] == "revoked", "已撤销的派发行不许被恢复改写成 done/error"
    finally:
        log.close()

    # (b) MCP：行已 rejected（撤销/拒绝先到，标记只从 intent 推进 → 请求一定
    #     没写出）→ 结论 none_applied、不阻断，但行仍 rejected（审计事实）
    ws2 = tmp_path / "ws-mcp"
    ws2.mkdir()
    data2 = tmp_path / "data-mcp"
    data2.mkdir()
    log2 = EventLog(tmp_path / "events-mcp.db", blob_dir=tmp_path / "blobs")
    artifacts2 = tmp_path / "artifacts-mcp"
    artifacts2.mkdir()
    store2 = SnapshotStore(artifacts2)
    try:
        stack2 = build_stack(log2, data_root=data2)
        run_id2 = "run_rejected"
        store2.init_run(run_id2)
        log2.append(
            run_id2, "run.start",
            {"task": "MCP 被拒后崩溃", "workspace": str(ws2), "out": "out.md",
             "limits": {}, "tools": []},
            actor_kind="core", legacy_write=True,
        )
        seq2 = log2.append(
            run_id2, "tool.intent",
            {"call_id": CALL_ID, "tool_id": MCP_TOOL, "args": {"message": "hi"},
             "args_hash": "0" * 64},
            actor_kind="core", legacy_write=True,
        )
        _insert_dispatch(
            log2, dispatch_id="dsp_rejected", run_id=run_id2, tool_id=MCP_TOOL,
            target="mcp://fake/echo", intent_seq=seq2, state="rejected",
        )

        report2 = recover_run(log2, run_id2, ws2, store2, leases=stack2.leases,
                              data_root=data2)
        assert report2.none_applied == [CALL_ID], (
            "rejected 说明标记从没推进：请求没写出，结论只能是确定没生效"
        )
        assert report2.unresolved == []
        assert report2.blocked_workspaces == [], "没发出的调用不该挡目录"
        row2 = log2.conn.execute(
            "SELECT state FROM dispatches WHERE run_id=? AND call_id=?",
            (run_id2, CALL_ID),
        ).fetchone()
        assert row2["state"] == "rejected", "已被拒绝的派发行不许被改写成 error"
    finally:
        log2.close()


def test_recovery_marks_dispatch_row_terminal_for_file_writes(tmp_path):
    """文件写恢复也要把派发行推到终态：applied → `done`，其余 → `error`。

    复审 R2：只写 `tool.result` 不动 `dispatches`，`dispatch_record()` 会继续显示
    `intent/executing`——同一件事在事件里"已结算"、在派发表里"仍在执行"。
    """
    for applied in (True, False):
        tag = "applied" if applied else "none"
        ws = tmp_path / f"ws-{tag}"
        ws.mkdir()
        data = tmp_path / f"data-{tag}"
        data.mkdir()
        log = EventLog(tmp_path / f"events-{tag}.db", blob_dir=tmp_path / "blobs")
        artifacts = tmp_path / f"artifacts-{tag}"
        artifacts.mkdir()
        store = SnapshotStore(artifacts)
        try:
            stack = build_stack(log, data_root=data)
            run_id = f"run_file_{tag}"
            store.init_run(run_id)
            before = b"before\n"
            after = b"after\n"
            (ws / "report.md").write_bytes(before)
            store.record_preimage(
                run_id, CALL_ID, "report.md", before,
                hashlib.sha256(after).hexdigest(), root=ws,
            )
            log.append(
                run_id, "run.start",
                {"task": "恢复派发行终态", "workspace": str(ws), "out": "out.md",
                 "limits": {}, "tools": []},
                actor_kind="core", legacy_write=True,
            )
            seq = log.append(
                run_id, "tool.intent",
                {"call_id": CALL_ID, "tool_id": "create_report",
                 "args": {"name": "report.md", "content": "after\n"},
                 "args_hash": "0" * 64},
                actor_kind="core", legacy_write=True,
            )
            _insert_dispatch(
                log, dispatch_id="dsp_file", run_id=run_id, tool_id="create_report",
                target=str(ws / "report.md"), intent_seq=seq,
            )
            if applied:
                (ws / "report.md").write_bytes(after)  # 副作用真的发生了

            report = recover_run(log, run_id, ws, store, leases=stack.leases,
                                data_root=data)
            row = log.conn.execute(
                "SELECT state FROM dispatches WHERE run_id=? AND call_id=?",
                (run_id, CALL_ID),
            ).fetchone()
            assert row is not None
            if applied:
                assert report.applied == [CALL_ID]
                assert row["state"] == DISPATCH_FINISHED, (
                    "写成了，派发行不能还是 error"
                )
            else:
                assert report.none_applied == [CALL_ID]
                assert row["state"] == DISPATCH_ERROR
            stamp = log.conn.execute(
                "SELECT created_at, updated_at FROM dispatches"
                " WHERE run_id=? AND call_id=?",
                (run_id, CALL_ID),
            ).fetchone()
            assert stamp["updated_at"] > stamp["created_at"], (
                "恢复的推进时间戳必须是恢复那一刻，不能倒退成登记时刻"
            )
        finally:
            log.close()


def test_refused_send_boundary_does_not_call_server(tmp_path, monkeypatch):
    """标记推进不上（行已撤销/作废）→ 不发请求、结论确定、行保持终态。

    真实时间窗里"撤销恰好插在 intent 与标记之间"没法按需制造，所以这里把
    `_mark_mcp_send_boundary` 换成一个"把行改成撤销、返回 False"的替身：要测的
    是**调用方怎么处理 False**——不发请求、写确定结论、不产凭据、不改写行。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    call_log = tmp_path / "calls.jsonl"
    clients = McpClients(
        {
            "fake": _config(
                "normal",
                call_log=call_log,
                effect_log=tmp_path / "effects.jsonl",
                cwd=tmp_path,
            )
        }
    )
    try:
        stack = build_stack(log, data_root=data, mcp_clients=clients)
        task = stack.tasks.create_task(
            "发包前撤销", [str(ws)], mcp_servers=["fake"]
        )
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))

        def refuse(dispatch_id: str) -> bool:
            log.conn.execute(
                "UPDATE dispatches SET state='revoked' WHERE dispatch_id=?",
                (dispatch_id,),
            )
            return False

        monkeypatch.setattr(stack.dispatcher, "_mark_mcp_send_boundary", refuse)
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)

        rows = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert len(rows) == 1, rows
        assert rows[0]["status"] == "rejected"
        assert rows[0]["effect_state"] == "none_applied"
        assert rows[0]["output"]["reason"] == "mcp.revoked_before_send"
        receipts = [
            e for e in log.read_run(result.run_id, verify=False)
            if e.type == "receipt"
        ]
        assert receipts == [], "没发出去的调用不产凭据"
        assert not call_log.exists() or call_log.read_text(encoding="utf-8") == "", (
            "标记推进不上就不许打 server"
        )
        row = stack.dispatcher.dispatch_record(result.run_id, CALL_ID)
        assert row is not None and row.state == "revoked", (
            "撤销是审计事实，不许被这次结果改写成 done/error"
        )
    finally:
        clients.close_all()
        log.close()


def test_corrupt_manifest_does_not_drag_never_sent_mcp_into_unknown(tmp_path):
    """快照清单坏了也不该把"请求都没写出"的 MCP 调用推成 unknown 并挡目录。

    MCP 的判据是外发边界标记，不是那份文件证据。拿无关证据的损毁去冒充 MCP 的
    证据，等于凭空多挡一个目录——过度阻断也是误报。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    store = SnapshotStore(artifacts)
    try:
        stack = build_stack(log, data_root=data)
        run_id = "run_mcp_corrupt"
        store.init_run(run_id)
        store.manifest_path(run_id).write_bytes(b"{not json")
        log.append(
            run_id,
            "run.start",
            {"task": "清单坏掉时的 MCP 调用", "workspace": str(ws), "out": "out.md",
             "limits": {}, "tools": []},
            actor_kind="core",
            legacy_write=True,
        )
        seq = log.append(
            run_id,
            "tool.intent",
            {"call_id": CALL_ID, "tool_id": MCP_TOOL, "args": {"message": "hi"},
             "args_hash": "0" * 64},
            actor_kind="core",
            legacy_write=True,
        )
        _insert_dispatch(
            log, dispatch_id="dsp_corrupt", run_id=run_id, tool_id=MCP_TOOL,
            target="mcp://fake/echo", intent_seq=seq,
        )

        report = recover_run(log, run_id, ws, store, leases=stack.leases,
                             data_root=data)
        assert report.none_applied == [CALL_ID]
        assert report.unresolved == []
        assert report.blocked_workspaces == [], "MCP 不读清单，不该被清单的损毁挡住"
        results = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert len(results) == 1
        assert results[0]["output"]["reason"] == "mcp.not_sent"
        row = log.conn.execute(
            "SELECT state FROM dispatches WHERE run_id=? AND call_id=?",
            (run_id, CALL_ID),
        ).fetchone()
        assert row["state"] == DISPATCH_ERROR
    finally:
        log.close()
