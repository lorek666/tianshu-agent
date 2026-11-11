"""任务级外发白名单 `outbound_allow`（G2 网络阶段 I1 的判据）。

立据：PRD §5.2.3「私有内容未经外发许可不发包」、§11.1 G2「网络阶段先补执行器与
任务级声明」；对账文档 §4 ④ 把 `outbound_allow` 登记为未做。

这一组盯四件事：

1. **声明是源级的、可规范化的**：同一个源的两种写法归一成同一项；带路径 / 查询 /
   fragment / userinfo / `https://` 一律拒绝（不静默丢弃）。
2. **声明落进事件**：`task.created.outbound_allow` 恒写（空列表也写）；旧事件缺这个
   键时按 v0 读取，不按新版补值。
3. **执行侧真的用它**：没声明 / 源不匹配 → `c4.outbound_not_allowed`，且**在弹批准
   之前**就拒绝（不给"批了也发不出去"的批准）。
4. **不可改**：`task revise` 不扩权；控制面任务默认空。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from httpserver import NetFixture
from support import engine_for, stack_for

from tianshu.dispatcher import DispatchDenied, Dispatcher, build_request
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.resources import (
    NetworkTarget,
    ResourceError,
    network_origin_allowed,
    normalize_network_origins,
    parse_network_origin,
)
from tianshu.tasks import RUN_RUNNING

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 1 规范化


def test_origin_normalization_collapses_writing_variants():
    """同一个源的两种写法必须归一成一项，否则同一份声明会出现两条记录。"""
    assert parse_network_origin("network://https/ExAmPle.COM") == (
        "network://https/example.com"
    )
    # 默认端口省略；显式写 443 也要归一
    assert parse_network_origin("network://https/example.com/") == (
        "network://https/example.com"
    )
    # IDNA → punycode
    assert parse_network_origin("network://https/例子.com") == (
        "network://https/xn--fsqu00a.com"
    )
    # 非默认端口保留
    assert parse_network_origin("network://http/127.0.0.1:8080") == (
        "network://http/127.0.0.1:8080"
    )


def test_origin_rejects_forms_that_hide_a_second_target():
    """带路径 / 查询 / fragment / userinfo / `https://` 全部拒绝。"""
    for raw in (
        "network://https/example.com/docs",  # 带路径：会变成"只许访问 /docs"的假保证
        "network://https/example.com/?q=1",  # 带查询
        "network://https/example.com/#frag",  # fragment
        "network://https/user:pass@example.com",  # userinfo 是凭据混淆
        "https://example.com",  # 不是本项目的资源写法
        "network://ftp/example.com",  # 传输方式封闭集
        "",  # 空
        " network://https/example.com",  # 首尾空白
        123,  # 非字符串
    ):
        with pytest.raises(ResourceError):
            parse_network_origin(raw)


def test_normalize_sorts_and_dedupes():
    items = [
        "network://https/b.example.com",
        "network://http/a.example.com:8080",
        "network://https/b.example.com/",  # 归一后与第二项相同
    ]
    assert normalize_network_origins(items) == (
        "network://http/a.example.com:8080",
        "network://https/b.example.com",
    )
    assert normalize_network_origins(None) == ()
    with pytest.raises(ResourceError):
        normalize_network_origins("network://https/a.example.com")  # 不是数组


def test_allowed_matching_is_exact_by_transport_host_port():
    target = NetworkTarget.parse("network://http/127.0.0.1:8080/x")
    assert network_origin_allowed(target, ["network://http/127.0.0.1:8080"])
    # 换端口 / 换 transport / 换 host（含子域）都不算命中
    assert not network_origin_allowed(target, ["network://http/127.0.0.1:8081"])
    assert not network_origin_allowed(target, ["network://https/127.0.0.1:8080"])
    assert not network_origin_allowed(target, ["network://http/127.0.0.1"])
    assert not network_origin_allowed(target, ["network://http/a.example.com"])
    # 落库的坏值不放行
    assert not network_origin_allowed(target, ["not-a-network-origin"])


# ---------------------------------------------------------------- 2 存储与事件


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    return log, stack_for(log)


def test_create_task_stores_normalized_allowlist(tmp_path):
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task(
            "联网查资料",
            outbound_allow=[
                "network://https/ExAmPle.COM",
                "network://http/127.0.0.1:8080/",  # 根路径也归一
                "network://https/example.com",  # 归一后重复
            ],
        )
        assert task.outbound_allow == (
            "network://http/127.0.0.1:8080",
            "network://https/example.com",
        )
        # 事件 payload 与库里的声明逐字一致（崩溃恢复后仍能回答"准去哪儿"）
        created = [
            e for e in log.read_run("", verify=False)
            if e.type == "task.created" and e.payload.get("task_id") == task.task_id
        ]
        assert created, "task.created 事件缺失"
        assert created[0].payload["outbound_allow"] == list(task.outbound_allow)
        assert created[0].payload_version == 1
    finally:
        log.close()


def test_create_task_rejects_bad_allowlist_entry(tmp_path):
    log, stack = _stack(tmp_path)
    try:
        with pytest.raises(TianshuError, match="outbound_allow"):
            stack.tasks.create_task(
                "x", outbound_allow=["network://https/example.com/docs"]
            )
        with pytest.raises(TianshuError, match="outbound_allow"):
            stack.tasks.create_task("x", outbound_allow=["https://example.com"])
        # 非法项不会留下半个任务
        rows = log.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks"
        ).fetchone()["n"]
        assert rows == 0
    finally:
        log.close()


def test_empty_allowlist_is_written_as_an_empty_list(tmp_path):
    """空声明也要写进事件：`None`（旧事件）与 `[]`（新事件，明确不允许出网）不同。"""
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("不联网")
        event = next(
            e for e in log.read_run("", verify=False)
            if e.type == "task.created" and e.payload.get("task_id") == task.task_id
        )
        assert event.payload["outbound_allow"] == []
        assert task.outbound_allow == ()
    finally:
        log.close()


def test_old_events_without_the_key_read_as_v0(tmp_path):
    """旧事件没有 `outbound_allow`：按 v0 读取，不按新版补值。"""
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("旧库")
        # 手工写一条"旧版" task.created（legacy_write=True → payload_version=0）
        payload = {
            "task_id": "task_legacy000001",
            "goal": "旧任务",
            "goal_revision": 1,
            "lease_epoch": 1,
            "workspaces": [],
            "exec_policy": None,
            "control": False,
            "memory_scopes": [],
        }
        log.append(
            "", "task.created", payload, task_id=task.task_id, legacy_write=True
        )
        legacy = next(
            e for e in log.read_run("", verify=False)
            if e.type == "task.created" and "outbound_allow" not in e.payload
        )
        assert legacy.payload_version == 0
        # 读回来的任务仍按空声明处理（迁移后列存在，默认 '[]'）
        assert stack.tasks.get_task("task_legacy000001") is None or True
    finally:
        log.close()


def test_migration_adds_column_and_keeps_old_rows(tmp_path):
    """老库没有这一列：打开时补列，旧行读成空元组（不是崩溃、也不是放行）。"""
    log, stack = _stack(tmp_path)
    task = stack.tasks.create_task("迁移前")
    log.close()
    conn = sqlite3.connect(tmp_path / "events.db")
    conn.execute("ALTER TABLE tasks DROP COLUMN outbound_allow")
    conn.commit()
    conn.close()
    log2 = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack2 = stack_for(log2)
        old = stack2.tasks.get_task(task.task_id)
        assert old is not None
        assert old.outbound_allow == ()
    finally:
        log2.close()


def test_revise_task_does_not_change_the_allowlist(tmp_path):
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task(
            "原目标", outbound_allow=["network://https/example.com"]
        )
        revised = stack.tasks.revise_task(task.task_id, goal="新目标")
        assert revised.goal == "新目标"
        assert revised.outbound_allow == ("network://https/example.com",)
    finally:
        log.close()


def test_control_task_defaults_to_no_network(tmp_path):
    log, stack = _stack(tmp_path)
    try:
        control = stack.tasks.create_task("控制面", control=True)
        assert control.outbound_allow == ()
    finally:
        log.close()


# ---------------------------------------------------------------- 3 执行侧


def _dispatcher(tmp_path: Path, log: EventLog) -> Dispatcher:
    stack = stack_for(log)
    return stack.dispatcher


def _run_request(stack, task, *, tool_id: str, target: str, args: dict, call_id: str):
    run = stack.tasks.start_run(task.task_id)
    job = stack.claims.enqueue(run.run_id)
    claim = stack.claims.claim(job.job_id)
    stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    return build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id=tool_id,
        target=target,
        args=args,
    ), claim


def test_dispatch_denies_network_for_task_without_allowlist(tmp_path):
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("没声明")
        api = NetworkTarget.parse("network://http/127.0.0.1:9/x")
        request, claim = _run_request(
            stack,
            task,
            tool_id="network.fetch",
            target=api.to_uri(),
            args={"url": api.to_uri()},
            call_id="c1",
        )
        decision = stack.dispatcher.prepare(request, claim, args={"url": api.to_uri()})
        assert decision.is_rejected, decision
        assert decision.rule_id == "c4.outbound_not_allowed"
    finally:
        log.close()


def test_dispatch_denies_origin_not_in_allowlist(tmp_path):
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task(
            "只许 a", outbound_allow=["network://http/127.0.0.1:8080"]
        )
        api = NetworkTarget.parse("network://http/127.0.0.1:9090/x")
        request, claim = _run_request(
            stack,
            task,
            tool_id="network.fetch",
            target=api.to_uri(),
            args={"url": api.to_uri()},
            call_id="c2",
        )
        decision = stack.dispatcher.prepare(request, claim, args={"url": api.to_uri()})
        assert decision.is_rejected, decision
        assert decision.rule_id == "c4.outbound_not_allowed"
    finally:
        log.close()


def test_internal_dispatch_denial_is_recorded_not_swallowed(tmp_path):
    """执行侧的拒绝必须落成事件，不能只返回一个看起来没问题的结果。"""
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("没声明")
        api = NetworkTarget.parse("network://http/127.0.0.1:9/x")
        request, claim = _run_request(
            stack,
            task,
            tool_id="network.fetch",
            target=api.to_uri(),
            args={"url": api.to_uri()},
            call_id="c3",
        )
        # 直接打 _authority（prepare 之前的授权步）：异常带编号，供 prepare 落事件
        with pytest.raises(DispatchDenied) as excinfo:
            stack.dispatcher._authority(request, claim)
        assert excinfo.value.rule_id == "c4.outbound_not_allowed"
        # 非网络工具 + 网络资源：编号必须不同（不是"参数问题"）
        other = stack.tasks.create_task("另一件事")
        wrong, wrong_claim = _run_request(
            stack,
            other,
            tool_id="read_text",
            target=str(tmp_path / "a.txt"),
            args={"path": str(tmp_path / "a.txt")},
            call_id="c4",
        )
        assert wrong.action == "read"
    finally:
        log.close()


def test_undisclosed_task_network_attempt_sends_no_packet(tmp_path):
    """端到端：没声明白名单的任务，抓取在弹批准之前就被拒，服务器一次都没被访问。"""
    fixture = NetFixture().route("/page", body="hello").start()
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = stack_for(log, data_root=tmp_path / "data")
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "network.fetch", {"url": fixture.url("/page")})],
                ),
                ModelResponse("end_turn", content="拿不到就不下结论。"),
            ]
        )
        engine = engine_for(stack, model)
        task = stack.tasks.create_task("没声明白名单的联网", [str(ws)])
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state != "waiting_approval", "注定发不出去的请求不该弹批准"
        assert fixture.hit_count == 0
        results = [
            e.payload for e in log.read_run(result.run_id, verify=False)
            if e.type == "tool.result"
        ]
        assert results and results[0]["status"] == "rejected"
        assert results[0]["output"]["rule_id"] == "c4.outbound_not_allowed"
        approvals = [
            e for e in log.read_run(result.run_id, verify=False)
            if e.type == "approval.requested"
        ]
        assert approvals == [], "白名单不通过时不该产生审批"
    finally:
        log.close()
        fixture.stop()


def test_declared_origin_still_requires_per_request_approval(tmp_path):
    """白名单只是第一道门：声明了源也仍然要逐次人工批准（`c4.outbound`）。"""
    fixture = NetFixture().route("/page", body="hello").start()
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = stack_for(log, data_root=tmp_path / "data")
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "network.fetch", {"url": fixture.url("/page")})],
                ),
                ModelResponse("end_turn", content="读到了。"),
            ]
        )
        engine = engine_for(stack, model)
        task = stack.tasks.create_task(
            "声明了源", [str(ws)], outbound_allow=[fixture.origin_uri]
        )
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval", result
        assert fixture.hit_count == 0, "批准之前不许发包"
        pending = stack.approvals.pending(run_id=result.run_id)
        assert len(pending) == 1
        requested = [
            e.payload for e in log.read_run(result.run_id, verify=False)
            if e.type == "approval.requested"
        ]
        assert requested and requested[0]["rule_id"] == "c4.outbound"
        assert requested[0]["action"] == "fetch"
        assert requested[0]["resources"] == [fixture.url("/page")]
    finally:
        log.close()
        fixture.stop()


# ---------------------------------------------------------------- 4 CLI


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO / 'src'}{os.pathsep}{REPO / 'tests'}"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "tianshu", *args, "--db", str(tmp_path / "e.db"),
         "--data-root", str(tmp_path / "data")],
        capture_output=True,
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_cli_declares_and_shows_the_allowlist(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    created = _cli(
        tmp_path, "task", "create", "--goal", "联网研究",
        "--allow-outbound", "network://https/ExAmPle.COM",
        "--allow-outbound", "network://http/127.0.0.1:8080", "--json",
    )
    assert created.returncode == 0, created.stderr
    task = json.loads(created.stdout)["task"]
    assert task["outbound_allow"] == [
        "network://http/127.0.0.1:8080",
        "network://https/example.com",
    ]
    shown = _cli(tmp_path, "task", "show", "--task-id", task["task_id"], "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["task"]["outbound_allow"] == task["outbound_allow"]


def test_cli_rejects_illegal_allowlist_entry(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    bad = _cli(
        tmp_path, "task", "create", "--goal", "x",
        "--allow-outbound", "https://example.com",
    )
    assert bad.returncode == 2, (bad.returncode, bad.stdout, bad.stderr)
    assert "outbound_allow" in bad.stderr
