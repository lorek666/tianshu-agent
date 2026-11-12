"""MCP 对象级阻断（`mcp://server/tool` / `mcp://server`）的回归测试。

验收报告 §5-8 的欠账：此前"阻断对象"只有 workspace（目录）这一种，MCP 调用
结算成 `unknown` 时按最保守口径挡调用方传进来的那个目录，于是出现两个问题：

1. **挡错东西**：真正有未核实副作用的是**那个工具**（在 server 那一侧），不是
   本地目录；`tianshu recover --workspace` 还是调用方自己填的路径，代码没拿
   task 绑定核对过——拿它冒充"MCP 的目录"是假精确。
2. **解除粒度粗**：人工核对完工作区文件之后 `unblock`（目录）会把一切都放开，
   包括那个"server 侧可能已经执行过"的工具。

本文件钉住的模型（也是代码里的模型）：

- MCP 结算成 `unknown` 时**同时**登记对象阻断（精确，键 = `mcp://server/tool`）
  与目录阻断（保守，`--workspace` 目录）——新增一层，默认**不放松**已有阻断。
- 发外前两道核验：判定阶段（弹批准之前）一道、外发边界前（执行守卫内）一道。
  两道都拒绝，结论都是 `rejected` / `none_applied`（一个字节都没发）。
- 解除分开：`guard.unblock` 只解目录；`guard.unblock_object` 才解对象。
  "核对过工作区文件"不等于"核对过 server 侧副作用"。
- 对象键**只从派发行 `target` 读**，绝不按 `mcp__server__tool` 反解（规范化把
  `-`/`.` 换成了 `_`，反解出来的名字可能不是用户配的那个）。读不到派发行就
  不登记对象阻断（目录阻断照旧，保守方向不放松）。
- 旧版本权威库没有 `blocked_objects` 表 = 从来没有对象阻断 = 空集（能证明），
  不是损坏；表在但读不出来 → 按已阻断处理。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from support import approve_pending, engine_for

from tianshu import authority
from tianshu.authority import AuthorityUnavailable
from tianshu.dispatcher import DISPATCH_INTENT
from tianshu.errors import EventLogCorrupt, McpObjectBlocked
from tianshu.events import EventLog
from tianshu.guard import (
    assert_paths_not_blocked,
    blocked_objects,
    ensure_not_blocked,
    ensure_object_not_blocked,
    mirror_blocks_to_authority,
    scan_blocked_objects,
    unblock,
    unblock_object,
)
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ACTION_OVERWRITE
from tianshu.recovery import recover_run
from tianshu.resources import (
    ResourceError,
    mcp_block_keys,
    mcp_object_key,
    mcp_server_key,
)
from tianshu.snapshot import SnapshotStore

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
MCP_TOOL = "mcp__fake__echo"
CALL_ID = "c1"
_ECHO = "mcp://fake/echo"
_SERVER = "mcp://fake"


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


def _config(
    mode: str, *, call_log: Path, effect_log: Path, cwd: Path, name: str = "fake"
) -> McpServerConfig:
    return McpServerConfig(
        name=name,
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


def _stack_with_server(tmp_path: Path, *, mode: str = "normal", mcp: bool = True):
    """开一个带假 MCP server 的栈；返回 (log, stack, ws, data, call_log)。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_bytes(b"note\n")
    data = tmp_path / "data"
    data.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    call_log = tmp_path / "calls.jsonl"
    effect_log = tmp_path / "effects.jsonl"
    if not mcp:
        stack = build_stack(log, data_root=data)
        return log, stack, ws, data, call_log
    clients = McpClients(
        {
            "fake": _config(
                mode, call_log=call_log, effect_log=effect_log, cwd=tmp_path
            )
        }
    )
    stack = build_stack(log, data_root=data, mcp_clients=clients)
    return log, stack, ws, data, call_log


def _block(key: str, *, run_id: str = "run_foreign", reason: str = "人工核查前") -> None:
    authority.register_object_blocked(key, run_id=run_id, reason=reason)


# ---------------------------------------------------------------- 权威根/元数据


def test_register_and_release_object_block_roundtrip():
    """登记幂等（不刷新 blocked_at）、能读到、释放后不再有效、重新登记会复活。"""
    key = mcp_object_key("weather", "get-now")
    assert authority.active_object_keys() == []
    assert authority.is_object_blocked(key) is False

    authority.register_object_blocked(key, run_id="r1", reason="没核对过")
    assert authority.active_object_keys() == [key]
    assert authority.is_object_blocked(key) is True
    first = authority.active_object_blocks()[0]

    # 幂等：再登记一次不刷新 blocked_at（否则每次恢复都像"刚阻断"）
    authority.register_object_blocked(key, run_id="r2", reason="又来一次")
    again = authority.active_object_blocks()[0]
    assert again.blocked_at == first.blocked_at
    assert again.reason == "没核对过", "有效记录不被后来的登记改写"

    assert authority.release_object_blocked(key, reason="核对完了") is True
    assert authority.is_object_blocked(key) is False
    assert authority.active_object_keys() == []
    # 再释放一次：没有有效记录 = 没东西可释放（不是错误）
    assert authority.release_object_blocked(key) is False

    # 已解除的行不删：重新登记会复活同一条记录
    authority.register_object_blocked(key, run_id="r3", reason="又出现未知")
    assert authority.active_object_keys() == [key]
    assert authority.release_object_blocked(key, reason="核对完了") is True


def test_old_authority_database_without_objects_table_is_empty_not_broken(tmp_path):
    """旧版本权威库没有 `blocked_objects` 表 = 空集（能证明），不是损坏。

    升级用户第一次用不该被自己挡住；但表**在**却读不出来必须反过来按已阻断
    处理（那是"不知道"，不是"没有"）。
    """
    root = tmp_path / "authority"
    root.mkdir()
    db = root / authority.DB_NAME
    conn = sqlite3.connect(db)
    try:
        # 旧版本的库：只有目录表（照 authority.py 上一版 SCHEMA 建）。
        conn.executescript(
            """
            CREATE TABLE blocked_workspaces (
                workspace_key   TEXT PRIMARY KEY,
                workspace_path  TEXT NOT NULL,
                run_id          TEXT NOT NULL DEFAULT '',
                reason          TEXT NOT NULL DEFAULT '',
                blocked_at      INTEGER NOT NULL,
                released_at     INTEGER,
                release_reason  TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    assert authority.active_object_blocks(root) == []
    assert authority.is_object_blocked("mcp://weather/get-now", root) is False

    # 写路径会补建表：登记后立刻可读
    authority.register_object_blocked(
        "mcp://weather/get-now", run_id="r1", reason="x", root=root
    )
    assert authority.is_object_blocked("mcp://weather/get-now", root) is True


def test_corrupt_authority_makes_object_checks_block_not_pass(tmp_path):
    """权威库损坏 → 判定不了 → 按已阻断处理（读不出来 ≠ 没阻断）。"""
    root = tmp_path / "authority"
    root.mkdir()
    (root / authority.DB_NAME).write_bytes(b"not a sqlite database at all")
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        try:
            ensure_object_not_blocked(log, (_ECHO, _SERVER))
        except McpObjectBlocked as exc:
            assert "权威对象阻断状态读不出来" in str(exc)
        else:  # pragma: no cover - 变异（fail-open）才会走到
            raise AssertionError("权威根读不出来时必须按已阻断处理，不能放行")
        # 信息查询如实抛"判定不了"，而不是回一个凑出来的空集
        with pytest.raises(AuthorityUnavailable, match="权威阻断状态读不出来"):
            blocked_objects(log)
    finally:
        log.close()


def test_log_replay_block_and_unblock(tmp_path):
    """事件回放：blocked 加、unblocked 去；畸形记录不静默跳过。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        log.append(
            "run_a",
            "recovery.object_blocked",
            {"object": _ECHO, "call_ids": ["c1"], "reason": "x"},
            actor_kind="system",
        )
        assert scan_blocked_objects(log).blocked == frozenset({_ECHO})
        assert blocked_objects(log) == {_ECHO}

        log.append(
            "run_a",
            "recovery.object_unblocked",
            {"object": _ECHO, "reason": "核对过"},
            actor_kind="human",
        )
        assert scan_blocked_objects(log).blocked == frozenset()
        assert blocked_objects(log) == set()

        # 畸形 payload：不是"没阻断"，是"判定不了"
        log.append(
            "run_b",
            "recovery.object_blocked",
            {"object": "", "call_ids": [], "reason": "x"},
            actor_kind="system",
        )
        assert scan_blocked_objects(log).malformed
        with pytest.raises(EventLogCorrupt, match="对象阻断记录无法判定"):
            blocked_objects(log)
        with pytest.raises(McpObjectBlocked, match="无法判定的对象阻断记录"):
            ensure_object_not_blocked(log, (_ECHO,))
    finally:
        log.close()


def test_mirror_backfills_objects_but_does_not_revive_released_ones(tmp_path):
    """启动补登记：日志里**当前有效**的对象阻断进权威根；已解除的不复活。"""
    root = tmp_path / "authority"
    root.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        log.append(
            "run_a",
            "recovery.object_blocked",
            {"object": _ECHO, "call_ids": ["c1"], "reason": "x"},
            actor_kind="system",
        )
        log.append(
            "run_a",
            "recovery.object_unblocked",
            {"object": _SERVER, "reason": "核对过"},
            actor_kind="human",
        )
        # `_SERVER` 从未登记过，回放里它是"先解除"——不影响补登记结果。
        problems = mirror_blocks_to_authority(log)
        assert problems == []
        assert authority.active_object_keys() == [_ECHO]
        # 幂等：再补一次不报错、不重复
        assert mirror_blocks_to_authority(log) == []
        assert authority.active_object_keys() == [_ECHO]
    finally:
        log.close()


# ---------------------------------------------------------------- 判定阶段


def test_blocked_object_is_refused_before_any_approval_is_requested(tmp_path):
    """对象被阻断 → 在**弹批准之前**拒绝；一个请求都没发给 server。"""
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task(
            "被阻断的工具", [str(ws)], mcp_servers=["fake"]
        )
        _block(_ECHO)
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state != "waiting_approval", "不该为一次注定不发的调用弹批准"

        approvals = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "approval.requested"
        ]
        assert approvals == []
        rejected = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "dispatch.rejected"
        ]
        assert [r["rule_id"] for r in rejected] == ["dispatch.mcp_object_blocked"]
        assert "mcp://fake/echo" in rejected[0]["reason"]

        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert tool_result["status"] == "rejected"
        assert tool_result["effect_state"] == "none_applied"
        assert call_log.exists() is False or _lines(call_log) == []
    finally:
        log.close()


def test_server_level_block_covers_every_tool_of_that_server(tmp_path):
    """`mcp://<server>` 遮住它下面**全部**工具（否则那条记录永远读不到）。"""
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task(
            "server 级阻断", [str(ws)], mcp_servers=["fake"]
        )
        _block(_SERVER)
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        rejected = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "dispatch.rejected"
        ]
        assert [r["rule_id"] for r in rejected] == ["dispatch.mcp_object_blocked"]
        assert _lines(call_log) == []
    finally:
        log.close()


def test_block_of_another_object_does_not_block_this_one(tmp_path):
    """精确性：挡的是 `mcp://fake/echo`，别的工具/别的 server 不受影响。"""
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task("别的工具不该被挡", [str(ws)], mcp_servers=["fake"])
        _block(mcp_object_key("fake", "add"))
        _block(mcp_server_key("other"))
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)

        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert tool_result["effect_state"] == "applied"
        assert tool_result["status"] == "succeeded"
        assert len(_lines(call_log)) == 1, "这次调用真的发出去了"
    finally:
        log.close()


def test_block_registered_while_waiting_for_approval_is_refused_before_send(tmp_path):
    """等批准期间别的 Run 把同一对象标成阻断 → 批准后也**不发**（守卫内复核）。"""
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task(
            "批准期间被阻断", [str(ws)], mcp_servers=["fake"]
        )
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval"

        # 模拟另一个 Run 在等批准期间落了一条对象阻断（它刚超时、还没核对）。
        _block(_ECHO, run_id="run_other")
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)

        rejected = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "dispatch.rejected"
        ]
        assert [r["rule_id"] for r in rejected] == ["dispatch.mcp_object_blocked"]
        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert (tool_result["status"], tool_result["effect_state"]) == (
            "rejected",
            "none_applied",
        )
        assert _lines(call_log) == [], "一个字节都不该写进 server 的 stdin"
    finally:
        log.close()


def test_send_boundary_guard_recheck_holds_even_if_dispatch_phase_check_is_bypassed(
    tmp_path, monkeypatch
):
    """第二道核验（外发边界前、守卫内）自己也得站得住。

    第一道（判定阶段）在这里被替身跳掉：这正是"判定通过之后、真正发包之前"
    那段时间里落下的阻断必须被拦住的理由。第二道若失效，请求真的会发给 server。
    """
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task("第二道核验", [str(ws)], mcp_servers=["fake"])
        _block(_ECHO)
        monkeypatch.setattr(
            stack.dispatcher, "_refuse_blocked_mcp_object", lambda *a, **k: None
        )
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)

        rejected = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "dispatch.rejected"
        ]
        assert [r["rule_id"] for r in rejected] == ["dispatch.mcp_object_blocked"]
        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert tool_result["effect_state"] == "none_applied"
        assert _lines(call_log) == [], "守卫内复核没拦住就会真的发出去"
    finally:
        log.close()


# ---------------------------------------------------------------- 运行期 unknown


def test_in_run_unknown_registers_object_block(tmp_path):
    """跑着的时候 server 报内部错误（结果未知）→ 就地登记对象阻断。

    这条路径没有崩溃：`-32603` 可能已经在执行后失败，所以结算 `unknown`；结论
    与崩溃恢复一样——"这个工具可能执行过"，下一次必须先核对。
    """
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path, mode="internal_error")
    try:
        task = stack.tasks.create_task("内部错误", [str(ws)], mcp_servers=["fake"])
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)

        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert tool_result["effect_state"] == "unknown"
        blocked = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "recovery.object_blocked"
        ]
        assert [b["object"] for b in blocked] == [_ECHO]
        assert blocked[0]["call_ids"] == [CALL_ID]
        assert _ECHO in blocked_objects(log)
        # 目录**不**被这条路径挡住（这里没有"目录状态被改过"的证据）
        ensure_not_blocked(log, ws)

        # 下一次调同一个工具：直接拒，不会再发一次请求
        engine2 = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        second = engine2.start(task.task_id, out_name="out2.md")
        rejected = [
            e.payload
            for e in log.read_run(second.run_id, verify=False)
            if e.type == "dispatch.rejected"
        ]
        assert [r["rule_id"] for r in rejected] == ["dispatch.mcp_object_blocked"]
        assert len(_lines(call_log)) == 1, "第二次调用一个字都没发出去"
    finally:
        log.close()


# ---------------------------------------------------------------- 恢复侧

_DISPATCH_INSERT = (
    "INSERT INTO dispatches (dispatch_id, run_id, task_id, call_id,"
    " request_id, request_digest, tool_id, action, resources, target,"
    " rel_path, workspace_root, resource_key, approval_id, grant_id,"
    " claim_epoch, owner_instance_id, attempt_id, intent_seq,"
    " boundary_protocol, state, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_NOW = 1_700_000_000_000


def _legacy_run(
    tmp_path: Path,
    *,
    dispatch_state: str | None,
    protocol: int = 1,
    target: str = "mcp://srv.a/do-thing",
    tool_id: str = "mcp__srv_a__do_thing",
):
    """手工造一条"有 intent、没有 result"的 MCP 调用（可带/不带派发行）。"""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=data)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    store = SnapshotStore(artifacts)
    run_id = "run_legacy"
    store.init_run(run_id)
    log.append(
        run_id,
        "run.start",
        {
            "task": "对象阻断恢复判据",
            "workspace": str(ws),
            "out": "out.md",
            "limits": {},
            "tools": [],
        },
        actor_kind="core",
        legacy_write=True,
    )
    log.append(
        run_id,
        "tool.intent",
        {
            "call_id": CALL_ID,
            "tool_id": tool_id,
            "args": {"message": "hi"},
            "args_hash": "0" * 64,
        },
        actor_kind="core",
        legacy_write=True,
    )
    if dispatch_state is not None:
        log.conn.execute(
            _DISPATCH_INSERT,
            (
                "d_legacy", run_id, "task_x", CALL_ID, "req_x", "0" * 64,
                tool_id, ACTION_OVERWRITE, json.dumps([target]), target, "", "",
                f"key:{target}", None, "grant_x", 1, "inst_x", 1, 1, protocol,
                dispatch_state, _NOW, _NOW,
            ),
        )
        log.conn.commit()
    return log, stack, ws, data, store, run_id


def test_recovery_registers_object_from_dispatch_target_not_from_tool_id(tmp_path):
    """对象键从派发行 `target` 读，**不**按 `mcp__…__…` 反解（名字要原样）。"""
    log, stack, ws, data, store, run_id = _legacy_run(
        tmp_path, dispatch_state="executing"
    )
    try:
        report = recover_run(
            log, run_id, ws, store, leases=stack.leases, data_root=data
        )
        assert report.unresolved == [CALL_ID]
        # server "srv.a" / tool "do-thing"：反解会得到 srv_a / do_thing
        assert report.blocked_objects == ["mcp://srv.a/do-thing"]
        blocked = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "recovery.object_blocked"
        ]
        assert blocked[0]["object"] == "mcp://srv.a/do-thing"
        assert blocked[0]["call_ids"] == [CALL_ID]
        # 目录阻断**同时**还在（保守方向不放松）；两件事互不替代
        assert report.blocked_workspaces == [str(ws.resolve())]
        assert "mcp://srv.a/do-thing" in blocked_objects(log)
        assert authority.active_object_keys() == ["mcp://srv.a/do-thing"]
    finally:
        log.close()


def test_recovery_never_sent_does_not_register_object_block(tmp_path):
    """停在 intent（没越过外发边界）→ `none_applied`，对象与目录都不阻断。"""
    log, stack, ws, data, store, run_id = _legacy_run(
        tmp_path, dispatch_state=DISPATCH_INTENT
    )
    try:
        report = recover_run(
            log, run_id, ws, store, leases=stack.leases, data_root=data
        )
        assert report.none_applied == [CALL_ID]
        assert report.unresolved == []
        assert report.blocked_objects == []
        assert report.blocked_workspaces == []
        assert blocked_objects(log) == set()
    finally:
        log.close()


def test_recovery_without_dispatch_row_blocks_dir_but_cannot_name_the_object(tmp_path):
    """没有派发行 → 证明不了是哪个对象：不登记对象阻断（目录阻断照旧）。"""
    log, stack, ws, data, store, run_id = _legacy_run(tmp_path, dispatch_state=None)
    try:
        report = recover_run(
            log, run_id, ws, store, leases=stack.leases, data_root=data
        )
        assert report.unresolved == [CALL_ID]
        assert report.blocked_objects == [], "证明不了对象就不许编一个"
        assert report.blocked_workspaces == [str(ws.resolve())]
        assert blocked_objects(log) == set()
    finally:
        log.close()


def test_recovery_legacy_row_blocks_object_but_stays_conservative_on_the_row(tmp_path):
    """升级前的派发行（protocol=0）：可能已发出 → unknown，对象照 `target` 登记。

    `boundary_protocol` 只影响"能不能证明没发"，不影响"这次调用对应哪个对象"——
    `target` 是派发当时写下的原始 URI，两种版本都可信。
    """
    log, stack, ws, data, store, run_id = _legacy_run(
        tmp_path, dispatch_state="intent", protocol=0
    )
    try:
        report = recover_run(
            log, run_id, ws, store, leases=stack.leases, data_root=data
        )
        assert report.unresolved == [CALL_ID]
        assert report.blocked_objects == ["mcp://srv.a/do-thing"]
        assert report.blocked_workspaces == [str(ws.resolve())]
        (row,) = log.conn.execute(
            "SELECT state FROM dispatches WHERE dispatch_id='d_legacy'"
        ).fetchall()
        assert row["state"] == "error", "结果未知 → 派发行推到终态 error"
    finally:
        log.close()


# ---------------------------------------------------------------- 解除


def test_unblock_workspace_does_not_release_the_object(tmp_path):
    """核心口径：核对过工作区文件 ≠ 核对过 server 侧；目录解除不碰对象。"""
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task("两种阻断分开解除", [str(ws)], mcp_servers=["fake"])
        _block(_ECHO, run_id="run_old")
        # 目录阻断也来一条（恢复侧登记过的形状）
        log.append(
            "run_old",
            "recovery.blocked",
            {"workspace": str(ws.resolve()), "call_ids": ["c0"], "reason": "x"},
            actor_kind="system",
        )
        authority.register_blocked(str(ws), run_id="run_old", reason="x")
        with pytest.raises(Exception, match="workspace 存在未核实的副作用"):
            ensure_not_blocked(log, ws)

        unblock(log, ws, reason="人工核对过工作区文件")
        ensure_not_blocked(log, ws)  # 目录可以写了
        # 目录解除**不**解对象：这条断言是模型本身，不能只靠"会抛异常"间接表达
        # （负控把"顺手解对象"变异进来时，必须落到一条 AssertionError 上）。
        assert _ECHO in blocked_objects(log), "解除目录阻断不该顺带解除对象阻断"
        try:
            ensure_object_not_blocked(log, mcp_block_keys("fake", "echo"))
        except McpObjectBlocked as exc:
            assert "mcp://fake/echo" in str(exc)
        else:  # pragma: no cover - 变异才会走到
            raise AssertionError("目录解除后对象阻断必须仍然有效")

        # 目录解了，工具还是调不了：下一次调用仍然在判定阶段被拒
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        rejected = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "dispatch.rejected"
        ]
        assert [r["rule_id"] for r in rejected] == ["dispatch.mcp_object_blocked"]
        assert _lines(call_log) == []
    finally:
        log.close()


def test_unblock_object_allows_the_call_again_and_is_auditable(tmp_path):
    """显式解除对象阻断 → 调用重新可以发出；解除动作留在事件日志里。"""
    log, stack, ws, _data, call_log = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task("解除后重试", [str(ws)], mcp_servers=["fake"])
        _block(_ECHO)
        run_id = unblock_object(log, _ECHO, reason="核对过 server 输出：没有执行")
        assert run_id.startswith("unblock_obj_")
        events = [
            e.payload
            for e in log.read_run(run_id, verify=False)
            if e.type == "recovery.object_unblocked"
        ]
        assert [e["object"] for e in events] == [_ECHO]
        assert authority.active_object_keys() == []
        ensure_object_not_blocked(log, mcp_block_keys("fake", "echo"))  # 不抛

        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        while result.state == "waiting_approval":
            approve_pending(stack, result.run_id)
            result = engine.resume_run(result.run_id)
        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert (tool_result["status"], tool_result["effect_state"]) == (
            "succeeded",
            "applied",
        )
        assert len(_lines(call_log)) == 1
    finally:
        log.close()


def test_unblock_object_rejects_malformed_keys(tmp_path):
    """解除一个永远匹配不上的键 = 什么都没做却说"已解除"：直接拒绝。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        for bad in ("mcp://", "mcp://a/b/c", "file:///tmp/x", "mcp://A/b"):
            with pytest.raises(ResourceError):
                unblock_object(log, bad, reason="x")
    finally:
        log.close()


def test_object_block_is_cross_data_root(tmp_path):
    """对象阻断跨数据根可见（否则换个 `--db` 就能再调一次有未知副作用的工具）。"""
    log_a = EventLog(tmp_path / "a.db", blob_dir=tmp_path / "blobs-a")
    try:
        log_a.append(
            "run_a",
            "recovery.object_blocked",
            {"object": _ECHO, "call_ids": ["c1"], "reason": "x"},
            actor_kind="system",
        )
        # 启动补登记把日志里的有效对象阻断写进用户级权威根
        mirror_blocks_to_authority(log_a)
    finally:
        log_a.close()

    # 换一个数据根（新库、新目录）：权威根还是同一个
    log_b = EventLog(tmp_path / "b.db", blob_dir=tmp_path / "blobs-b")
    try:
        assert blocked_objects(log_b) == {_ECHO}
        with pytest.raises(McpObjectBlocked):
            ensure_object_not_blocked(log_b, mcp_block_keys("fake", "echo"))
    finally:
        log_b.close()


def test_object_block_does_not_block_file_writes_but_dir_block_still_does(tmp_path):
    """精确性：对象阻断只拦发外，不拦文件写；目录阻断照旧拦文件写。"""
    log, stack, ws, _data, _call = _stack_with_server(tmp_path)
    try:
        _block(_ECHO)
        ensure_not_blocked(log, ws)  # 对象阻断不影响目录
        target = ws / "out.txt"
        assert_paths_not_blocked(log, [target])

        authority.register_blocked(str(ws), run_id="run_old", reason="x")
        log.append(
            "run_old",
            "recovery.blocked",
            {"workspace": str(ws.resolve()), "call_ids": ["c0"], "reason": "x"},
            actor_kind="system",
        )
        with pytest.raises(Exception, match="workspace 存在未核实的副作用"):
            ensure_not_blocked(log, ws)
    finally:
        log.close()


def test_blocked_object_refusal_is_a_controlled_rejection_not_unknown(tmp_path):
    """拒绝路径必须是 rejected/none_applied，不能变成 unknown（那会凭空多挡目录）。"""
    log, stack, ws, _data, _call = _stack_with_server(tmp_path)
    try:
        task = stack.tasks.create_task("受控拒绝", [str(ws)], mcp_servers=["fake"])
        _block(_ECHO)
        engine = engine_for(stack, ScriptedModel([_tool_call(), _end()]))
        result = engine.start(task.task_id, out_name="out.md")
        (tool_result,) = [
            e.payload
            for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert tool_result["effect_state"] == "none_applied"
        assert tool_result["status"] == "rejected"
        assert report_ok(log, result.run_id), "拒绝路径不该留下 unknown 或阻断目录"
    finally:
        log.close()


def report_ok(log: EventLog, run_id: str) -> bool:
    """这条 Run 没有"效果未知"的结果，也没有被阻断的目录。"""
    for event in log.read_run(run_id, verify=False):
        if event.type == "tool.result" and event.payload.get("effect_state") == "unknown":
            return False
        if event.type == "recovery.blocked":
            return False
    return True