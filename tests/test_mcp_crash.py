"""MCP 崩溃夹具与恢复判据（G2 MCP 阶段收尾轮：c10 的实测、NB21 的负控对象）。

对端是 `tests/fixtures/fake_mcp_server.py` **真子进程**，跑 Run 的也是真子进程
（CLI）。崩溃是 `os._exit(77)` 硬退：不走清理、不给恢复代码机会。它与 `kill -9`
在"进程死在这一步"这个意义上等价——**不**证明内核缓冲里的字节真的丢了（这一
口径与 `docs/powercut-evidence.md` 相同）。

为什么要两个窗口，各自证明什么：

- `mcp_after_call`（响应已收到；`tool.result` / 凭据 / 终态都没落盘）才是真正的
  "**可能已生效但结论丢了**"的窗口。server 的 `--effect-log` 证明它确实执行了。
  恢复对它唯一诚实的答案是 `unknown` + 阻断，**不是**重放、也不是"没证据=没写"。
- `mcp_before_call`（intent/dispatches 已提交、请求还没写进 server stdin）：恢复
  **分不出**它与上者的区别——它手上没有任何本机证据能说明"发没发出去"。所以同
  样是 `unknown` + 阻断。这条用例把"宁可不发也先列出来"的代价钉住，防止有人为
  了让日志好看，把"没收到结果"写成 `none_applied`。

另外三条用例不崩溃，测的是**不该被重判 / 不该留下互相矛盾的说法**：

- `isError: true` 的调用已经有 `tool.result`：结论是 `applied`（确实执行过）+
  `status=failed` + 凭据。恢复的判据是"结果事件有没有提交"，不是 server 回了
  什么，所以恢复必须一条都不改。
- 文件写工具的恢复终态：`applied` → 派发行 `done`，其余 → `error`。只写结果
  不动派发行，`dispatch_record()` 会继续显示 `intent/executing`——同一件事在事件
  里是"已结算"、在派发表里是"仍在执行"（复审 R2）。
- MCP 工具 id 被误塞进静态 `SPECS` 时，恢复仍必须按 MCP 结算（不查磁盘）。身份
  由 id 形状决定，不靠"静态表里没有它"。
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

from tianshu.dispatcher import DISPATCH_ERROR, DISPATCH_FINISHED
from tianshu.errors import WorkspaceBlocked
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces, ensure_not_blocked, unblock
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ACTION_OVERWRITE, HumanIdentity
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
        # 崩在派发中途：派发行还没到终态
        assert _dispatch_state(env, run_id) in ("intent", "executing")

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


def test_crash_before_send_is_indistinguishable_and_still_blocks(tmp_path):
    """请求没发出去也照样 unknown + 阻断：恢复没有证据分得清。

    这条不是"要求"而是**代价**：为了不在"可能已生效"上撒谎，恢复必须把"连发都
    没发"的一次调用也列出来等人工核对。谁要是把这里改成 `none_applied` 让日志
    好看，另一条用例（响应已收到）就会变成假话。
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

        report = _recover(env, run_id)
        assert report.unresolved == [CALL_ID], "口径：分不清就只报 unknown"
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
) -> None:
    log.conn.execute(
        _DISPATCH_INSERT,
        (
            dispatch_id, run_id, "task_x", CALL_ID, "req_x", "0" * 64, tool_id,
            ACTION_OVERWRITE, "[]", target, "", "", f"key:{target}", None,
            "grant_x", 1, "inst_x", 1, intent_seq, "intent", _NOW, _NOW,
        ),
    )


def test_mcp_tool_in_static_specs_is_still_not_treated_as_a_file_tool(
    tmp_path, monkeypatch
):
    """MCP id 万一进了静态 `SPECS`，恢复仍按 MCP 判（不读磁盘）。

    "不在 `SPECS` 里"是个会变的前提：有人把动态 id 塞进静态表、或注册表装配错
    了，恢复就会去查快照清单，把"没有写前证据"读成"确定没写"——一次可能已经
    生效的 MCP 调用被洗成 `none_applied`。身份由 id 形状决定，不由静态表决定。
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
        run_id = "run_mcp_static"
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
            target="mcp://fake/echo", intent_seq=seq,
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
                side_effect_level="idempotent",
                action=ACTION_OVERWRITE,
            ),
        )
        assert MCP_TOOL in SPECS, "变异必须真的生效，否则这条用例什么都没测"

        report = recover_run(log, run_id, ws, store, leases=stack.leases,
                             data_root=data)
        assert report.unresolved == [CALL_ID], (
            "即使静态表里出现了这个 id，也只能按 MCP 判 unknown"
        )
        assert report.none_applied == [], (
            "空清单里没有本次调用的条目，不能拿来证明 MCP 没执行"
        )
        results = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert len(results) == 1
        assert results[0]["tool_id"] == MCP_TOOL
        assert results[0]["output"]["reason"] == "mcp.crash_unsettled"
    finally:
        log.close()


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
        finally:
            log.close()