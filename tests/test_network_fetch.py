"""`network.fetch` 执行器与派发链路（G2 网络阶段 I2 的判据）。

立据：PRD §5.2.3「状态变化、数据外发、计费、可重试性分开表达」、§8.1「默认禁止
未授权出网；允许目标、重定向与子资源均受执行侧规则约束」、§11.1 G2。

这一组盯六件事，全部用**本机** loopback 夹具（`tests/httpserver.py`），不碰外网：

1. 抓取成功 → 有界摘要 + `content://sha256:` 引用；正文可读回、摘要不过界；
2. 上限与超时是执行侧的规则：响应按字节封顶，连不上 / 超时是**失败**而不是"没内容"；
3. 重定向只许同源：跨源 302 拒绝，且**第二跳一次都没发生**（命中数证明）；
4. 白名单与逐次批准两道门：没声明 → 不弹批准、不发包；声明了也仍要逐次批准；
5. 计费与取消：字节额度在**发包前**判定；取消之后不再有新请求；
6. 回放不联网：跑完停掉服务器，`replay` 仍然干净、命中数不增。
"""

from __future__ import annotations

import http.client
import re
import threading
import time

import pytest
from httpserver import NetFixture, closed_port
from support import approve_pending, engine_for

from tianshu.contents import parse_ref
from tianshu.events import sha256_hex
from tianshu.loop import Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.net import NetError, NetLimits, canonical_target, fetch, summary_text
from tianshu.replay import replay

REF = re.compile(r"content://sha256:[0-9a-f]{64}")


# ---------------------------------------------------------------- 夹具


def _stack(tmp_path, *, net_limits=None, limits=None):
    from tianshu.events import EventLog

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(
        log, data_root=tmp_path / "data",
        **({} if net_limits is None else {"net_limits": net_limits}),
    )
    return log, stack, limits


def _task(stack, *, allow=(), ws=None, goal="联网查资料"):
    bindings = [str(ws)] if ws is not None else []
    return stack.tasks.create_task(goal, bindings, outbound_allow=list(allow))


def _script(*urls: str, final: str = "写完了。") -> ScriptedModel:
    calls = [
        ModelResponse(
            "tool_use",
            tool_calls=[ToolCall(f"c{i}", "network.fetch", {"url": url})],
        )
        for i, url in enumerate(urls, start=1)
    ]
    calls.append(ModelResponse("end_turn", content=final))
    return ScriptedModel(calls)


def _drive(stack, engine, task, *, out_name="out.md", rounds=6):
    """一路批准到 Run 结束（最多 `rounds` 轮审批往返）。"""
    result = engine.start(task.task_id, out_name=out_name)
    while result.state == "waiting_approval" and rounds > 0:
        rounds -= 1
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    return result


def _results(log, run_id):
    return {
        e.payload["call_id"]: e.payload
        for e in log.read_run(run_id, verify=False)
        if e.type == "tool.result"
    }


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


# ---------------------------------------------------------------- 1 成功路径


def test_fetch_succeeds_and_returns_a_usable_reference(tmp_path):
    fixture = NetFixture().route(
        "/page", body="<h1>CSV 导出基准</h1><p>3 秒内完成。</p>"
    ).start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        engine = engine_for(stack, _script(fixture.url("/page")))
        result = _drive(stack, engine, task)
        assert result.outcome in {"succeeded", "noop"}, result

        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "succeeded", payload
        assert payload["effect_state"] == "none_applied"
        out = payload["output"]
        assert out["url"] == fixture.url("/page")
        assert out["status"] == 200
        assert "text/html" in out["content_type"]
        assert out["truncated"] is False
        assert "CSV 导出基准" in out["summary"]
        assert REF.fullmatch(out["ref"]), out["ref"]

        # 引用能读回**原文**（按任务可见）
        page = stack.contents.read(out["ref"], task_id=result.task_id)
        assert "CSV 导出基准" in page.data
        assert page.total_size == out["bytes"]
        # blob 的摘要就是引用里的摘要
        digest = parse_ref(out["ref"])
        blob = stack.snapshots.blob_dir / digest
        assert blob.is_file() and sha256_hex(blob.read_bytes()) == digest
    finally:
        log.close()
        fixture.stop()


def test_summary_is_bounded_but_reference_keeps_the_captured_bytes(tmp_path):
    body = ("长页面正文。" * 3000).encode()  # 21 KiB
    fixture = NetFixture().route("/big", body=body).start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/big"))), task)
        out = _results(log, result.run_id)["c1"]["output"]
        assert out["bytes"] == len(body)
        assert len(out["summary"].encode("utf-8")) <= 4096
        assert out["truncated"] is True, "摘要被截断必须如实标记"
        page = stack.contents.read(out["ref"], task_id=result.task_id, length=65536)
        assert page.total_size == len(body)
        assert page.data.encode("utf-8")[: len(body[:4096])] == body[:4096]
    finally:
        log.close()
        fixture.stop()


def test_binary_body_is_flagged_and_summary_is_lossy(tmp_path):
    fixture = NetFixture().route(
        "/blob", body=b"\x00\x01\x02\xff", content_type="application/octet-stream"
    ).start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/blob"))), task)
        out = _results(log, result.run_id)["c1"]["output"]
        assert out["binary"] is True
        assert out["bytes"] == 4
        # 原始字节仍在内容引用里（blob 可核对），但 `read_content` 只服务文本：
        # 二进制没有文本分页读取工具，这是登记过的边界，不假装能读。
        digest = parse_ref(out["ref"])
        blob = stack.snapshots.blob_dir / digest
        assert blob.read_bytes() == b"\x00\x01\x02\xff"
        from tianshu.contents import ContentError

        with pytest.raises(ContentError, match="不是 UTF-8"):
            stack.contents.read(out["ref"], task_id=result.task_id)
    finally:
        log.close()
        fixture.stop()


def test_response_is_capped_by_the_executor_limit(tmp_path):
    fixture = NetFixture().route("/big", body=b"x" * 5000).start()
    limits = NetLimits(max_bytes=1024)
    log, stack, _ = _stack(tmp_path, net_limits=limits)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/big"))), task)
        out = _results(log, result.run_id)["c1"]["output"]
        assert out["bytes"] == 1024
        assert out["truncated"] is True
        page = stack.contents.read(out["ref"], task_id=result.task_id)
        assert page.total_size == 1024, "内容引用只承诺**读到的那部分**"
    finally:
        log.close()
        fixture.stop()


# ---------------------------------------------------------------- 2 重定向


def test_redirect_body_is_not_downloaded_outside_the_byte_limit(tmp_path, monkeypatch):
    """真实 HTTP 跳转正文不能在最终响应大小与字节账本之外无界读取。"""
    reads = []
    original_read = http.client.HTTPResponse.read

    def record_read(response, amt=None):
        data = original_read(response, amt)
        reads.append((response.status, len(data)))
        return data

    monkeypatch.setattr(http.client.HTTPResponse, "read", record_read)
    fixture = NetFixture().route(
        "/redirect", status=302, body=b"x" * 262_144,
        headers={"Location": "/final"},
    ).route("/final", body=b"ok").start()
    log, stack, _ = _stack(tmp_path, net_limits=NetLimits(max_bytes=64))
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/redirect"))), task)
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "succeeded", payload
        assert payload["output"]["bytes"] == 2
        assert fixture.hits == [("GET", "/redirect"), ("GET", "/final")]
        assert sum(size for _, size in reads) == 2, reads
        assert all(status == 200 for status, _ in reads), reads
        usage = stack.budget.usage(f"run:{result.run_id}")
        assert usage["settled"]["bytes"] == 2
        assert usage["reserved"].get("bytes", 0) == 0
    finally:
        log.close()
        fixture.stop()


def test_same_origin_redirect_is_followed_and_recorded(tmp_path):
    fixture = NetFixture()
    fixture.route("/start", status=302, headers={"Location": "/final"})
    fixture.route("/final", body="到达")
    fixture.start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/start"))), task)
        out = _results(log, result.run_id)["c1"]["output"]
        assert out["url"] == fixture.url("/final")
        assert out["redirects"] == [fixture.url("/final")]
        assert out["summary"] == "到达"
        assert fixture.paths() == ["/start", "/final"]
    finally:
        log.close()
        fixture.stop()


def test_cross_origin_redirect_is_refused_and_second_hop_never_happens(tmp_path):
    other = NetFixture().route("/secret", body="不该被访问").start()
    fixture = NetFixture()
    fixture.route(
        "/start", status=302, headers={"Location": f"http://{other.base}/secret"}
    )
    fixture.start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/start"))), task)
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "failed", payload
        assert payload["output"]["reason"] == "net.redirect_origin_denied"
        assert fixture.hit_count == 1
        assert other.hit_count == 0, "跨源重定向不许跟过去：那是绕过批准"
    finally:
        log.close()
        fixture.stop()
        other.stop()


def test_https_to_http_redirect_is_refused_by_the_same_rule():
    """https → http 换的是 transport：按同源规则拒绝（不需要真起 TLS 服务）。"""
    from tianshu.net import _resolve_location

    https = canonical_target("network://https/example.com/a")
    with pytest.raises(NetError) as excinfo:
        _resolve_location(https, "http://example.com/a")
    assert excinfo.value.reason == "net.redirect_origin_denied"


# ---------------------------------------------------------------- 3 失败要如实


def test_connection_refused_is_an_honest_failure(tmp_path):
    port, sock = closed_port()
    log, stack, _ = _stack(tmp_path)
    try:
        origin = f"network://http/127.0.0.1:{port}"
        target = f"network://http/127.0.0.1:{port}/x"
        task = _task(stack, allow=(origin,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(target)), task)
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "failed", payload
        assert payload["effect_state"] == "none_applied"
        assert payload["output"]["reason"] in {"net.connect", "net.connect_timeout"}
        assert "ref" not in payload["output"], "失败不许伪造一个内容引用"
    finally:
        sock.close()
        log.close()


def test_timeout_is_bounded_and_reported(tmp_path):
    fixture = NetFixture().route("/slow", body="终于", delay_ms=400).start()
    limits = NetLimits(timeout_ms=100)
    log, stack, _ = _stack(tmp_path, net_limits=limits)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        started = time.monotonic()
        result = _drive(stack, engine_for(stack, _script(fixture.url("/slow"))), task)
        elapsed = time.monotonic() - started
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "failed", payload
        assert payload["output"]["reason"] in {"net.timeout", "net.connect_timeout"}
        assert elapsed < 3.0, f"超时上限没有生效：{elapsed:.2f}s"
    finally:
        log.close()
        fixture.stop()


# ---------------------------------------------------------------- 4 两道门


def test_no_allowlist_means_no_approval_and_no_packet(tmp_path):
    fixture = NetFixture().route("/page", body="hello").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, ws=_ws(tmp_path))  # 没声明
        result = _drive(stack, engine_for(stack, _script(fixture.url("/page"))), task)
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "rejected"
        assert payload["output"]["rule_id"] == "c4.outbound_not_allowed"
        assert fixture.hit_count == 0
    finally:
        log.close()
        fixture.stop()


def test_declared_origin_is_still_gated_by_a_per_request_approval(tmp_path):
    fixture = NetFixture().route("/page", body="hello").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        engine = engine_for(stack, _script(fixture.url("/page")))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval"
        assert fixture.hit_count == 0, "批准之前发包就是越权"
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
        assert result.outcome in {"succeeded", "noop"}
        assert fixture.hit_count == 1
    finally:
        log.close()
        fixture.stop()


def test_rejected_approval_sends_nothing(tmp_path):
    fixture = NetFixture().route("/page", body="hello").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        engine = engine_for(stack, _script(fixture.url("/page")))
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval"
        from tianshu.permissions import HumanIdentity

        for approval in stack.approvals.pending(run_id=result.run_id):
            stack.approvals.decide(
                approval.approval_id,
                decision="rejected",
                actor=HumanIdentity.local_cli(),
            )
        result = engine.resume_run(result.run_id)
        assert fixture.hit_count == 0
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "rejected"
        assert payload["output"]["rule_id"] == "c4.approval_rejected"
    finally:
        log.close()
        fixture.stop()


def test_each_url_needs_its_own_approval(tmp_path):
    """批准的是"这个 URL"，不是"这个源"：同源换路径还要再批一次。"""
    fixture = NetFixture().route("/a", body="A").route("/b", body="B").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        engine = engine_for(
            stack, _script(fixture.url("/a"), fixture.url("/b"))
        )
        result = engine.start(task.task_id, out_name="out.md")
        assert result.state == "waiting_approval"
        assert fixture.hit_count == 0
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
        # 第二个 URL 仍在等批准，且第一个 URL 已经发过一次
        assert result.state == "waiting_approval"
        assert fixture.paths() == ["/a"]
        approved = approve_pending(stack, result.run_id)
        assert len(approved) == 1
        result = engine.resume_run(result.run_id)
        assert fixture.paths() == ["/a", "/b"]
        assert result.outcome in {"succeeded", "noop"}
    finally:
        log.close()
        fixture.stop()


# ---------------------------------------------------------------- 5 计费与取消


def test_bytes_budget_is_enforced_before_sending(tmp_path):
    fixture = NetFixture().route("/a", body=b"a" * 10).route("/b", body=b"b" * 10).start()
    limits = NetLimits(max_bytes=1024)
    log, stack, _ = _stack(tmp_path, net_limits=limits)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        run_limits = Limits(
            max_turns=8, max_total_tokens=20_000, budget_limits={"bytes": 1024}
        )
        engine = engine_for(
            stack, _script(fixture.url("/a"), fixture.url("/b")), limits=run_limits
        )
        result = _drive(stack, engine, task)
        results = _results(log, result.run_id)
        assert results["c1"]["status"] == "succeeded"
        # 第二次：预占 1024 字节 > 剩余额度（1024 - 10）→ 派发前拒绝
        assert results["c2"]["status"] == "rejected", results["c2"]
        assert results["c2"]["output"]["rule_id"] == "dispatch.budget_refused"
        assert fixture.paths() == ["/a"], "额度不够时一个包都不许发"
        usage = stack.budget.usage(f"run:{result.run_id}")
        assert usage["settled"]["bytes"] == 10
    finally:
        log.close()
        fixture.stop()


def test_cancel_before_dispatch_sends_no_packet(tmp_path):
    """已批准的抓取，在派发前撞上取消：一个包都不发。

    这一条直接打派发层（和 `_dispatch_network` 的检查顺序一一对应）：
    创建 run → prepare（产生审批）→ 人工批准 → 请求取消 → dispatch。
    如果 take 的检查顺序反了（先发包再查取消），这里会看到 1 次命中。
    """
    from tianshu.dispatcher import build_request
    from tianshu.permissions import HumanIdentity
    from tianshu.tasks import RUN_RUNNING

    fixture = NetFixture().route("/page", body="hello").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        run = stack.tasks.start_run(task.task_id)
        job = stack.claims.enqueue(run.run_id)
        claim = stack.claims.claim(job.job_id)
        stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        url = fixture.url("/page")
        request = build_request(
            task=task,
            run=stack.tasks.get_run(run.run_id),
            claim=claim,
            call_id="c1",
            tool_id="network.fetch",
            target=url,
            args={"url": url},
        )
        decision = stack.dispatcher.prepare(request, claim, args={"url": url})
        assert decision.is_waiting, decision
        for approval in stack.approvals.pending(run_id=run.run_id):
            stack.approvals.decide(
                approval.approval_id,
                decision="approved",
                actor=HumanIdentity.local_cli(),
            )
        stack.tasks.request_cancel(run.run_id)
        result = stack.dispatcher.dispatch(decision.request_id, claim)
        assert result.status == "failed", result
        assert result.output["reason"] == "net.cancelled"
        assert fixture.hit_count == 0, "取消之后不许再发新请求"
    finally:
        log.close()
        fixture.stop()


def test_in_flight_cancel_stops_further_reads_but_cannot_unsend():
    """取消只能阻止**新**读/新请求：在途请求已经发出去了，如实记一次命中。

    这一条直接打执行器（`net.fetch` 的 `cancel` 回调），因为端到端的取消
    切换要跨线程写 SQLite（`request_cancel` 不是给另一个线程用的），
    而"每块读之前检查取消"本来就是执行器的责任。端到端的"取消后不再发包"
    由 `test_cancel_before_resume_sends_no_new_request` 证明。
    """
    from tianshu.net import NetCancelled

    fixture = NetFixture().route("/slow", body=b"z" * 100, body_delay_ms=500).start()
    try:
        state = {"cancelled": False}
        target = canonical_target(fixture.url("/slow"))

        def _cancel_after_delay() -> None:
            time.sleep(0.15)  # 请求已发出、正文还没到
            state["cancelled"] = True

        timer = threading.Thread(target=_cancel_after_delay, daemon=True)
        timer.start()
        with pytest.raises(NetCancelled):
            fetch(
                target,
                limits=NetLimits(timeout_ms=5000),
                cancel=lambda: state["cancelled"],
            )
        timer.join(timeout=5)
        assert fixture.hit_count == 1, "在途请求无法撤回；但也不许有第二条"
    finally:
        fixture.stop()


# ---------------------------------------------------------------- 6 回放


def test_replay_after_the_server_is_gone_does_not_refetch(tmp_path):
    fixture = NetFixture().route("/page", body="<p>来源</p>").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(
            stack, engine_for(stack, _script(fixture.url("/page"))), task
        )
        assert fixture.hit_count == 1
        fixture.stop()
        report = replay(log, result.run_id)
        assert report.ok, report.problems
        assert fixture.hit_count == 1, "回放不许重新联网"
    finally:
        log.close()
        fixture.stop()


def test_replay_reports_a_network_result_without_reference(tmp_path):
    """外发结果缺内容引用 = 记录不完整：回放报 problem，不静默当普通输出。

    事件表是 append-only（触发器挡 UPDATE），所以这里从零写一条**自洽但语义
    不完整**的日志来复现那种记录——而不是去改历史事件。
    """
    from tianshu.events import EventLog

    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    run_id = "run_synthetic"
    try:
        log.append(
            run_id,
            "run.start",
            {
                "run_id": run_id,
                "task_id": "task_synthetic",
                "goal_revision": 1,
                "lease_epoch": 1,
            },
        )
        log.append(
            run_id,
            "tool.intent",
            {
                "call_id": "c1",
                "tool_id": "network.fetch",
                "args": {"url": "network://https/example.com/a"},
                "args_hash": "0" * 64,
                "request_id": "req_synthetic",
                "request_digest": "0" * 64,
                "dispatch_id": "disp_synthetic",
                "grant_id": "grant_synthetic",
                "attempt_id": 1,
                "claim_epoch": 1,
            },
        )
        # 关键：status=succeeded 但没有 ref（模拟"写事件时漏了落盘"）
        log.append(
            run_id,
            "tool.result",
            {
                "call_id": "c1",
                "tool_id": "network.fetch",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {"url": "network://https/example.com/a", "status": 200},
            },
        )
        log.append(
            run_id,
            "run.end",
            {
                "outcome": "noop",
                "effect_state": "none_applied",
                "artifact": None,
                "error": None,
                "receipt_ids": [],
                "tokens": 0,
                "tokens_unknown_calls": 0,
                "turns": 0,
                "unbilled_model_intents": 0,
                "budget_enforced": False,
            },
        )
        report = replay(log, run_id)
        assert not report.ok
        assert any("外发结果缺内容引用" in p for p in report.problems), (
            report.problems
        )
    finally:
        log.close()


# ---------------------------------------------------------------- 7 参数与越权


def test_plain_url_argument_is_rejected_with_the_canonical_form(tmp_path):
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=("network://http/127.0.0.1:9",), ws=_ws(tmp_path))
        result = _drive(
            stack,
            engine_for(stack, _script("https://example.com/page")),
            task,
        )
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "rejected", payload
        assert "network://" in payload["output"]["error"]
    finally:
        log.close()


def test_zero_workspace_task_can_fetch(tmp_path):
    fixture = NetFixture().route("/page", body="无目录也能抓").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,))  # 没有绑定目录
        result = _drive(stack, engine_for(stack, _script(fixture.url("/page"))), task)
        payload = _results(log, result.run_id)["c1"]
        assert payload["status"] == "succeeded", payload
        assert result.outcome in {"succeeded", "noop"}
        assert fixture.hit_count == 1
    finally:
        log.close()
        fixture.stop()


def test_other_task_cannot_read_the_reference(tmp_path):
    fixture = NetFixture().route("/page", body="私有来源").start()
    log, stack, _ = _stack(tmp_path)
    try:
        task = _task(stack, allow=(fixture.origin_uri,), ws=_ws(tmp_path))
        result = _drive(stack, engine_for(stack, _script(fixture.url("/page"))), task)
        ref = _results(log, result.run_id)["c1"]["output"]["ref"]
        other = _task(stack, goal="别的任务")
        with pytest.raises(Exception) as excinfo:
            stack.contents.read(ref, task_id=other.task_id)
        assert "ContentNotVisible" in type(excinfo.value).__name__
    finally:
        log.close()
        fixture.stop()


# ---------------------------------------------------------------- 8 纯函数


def test_canonical_target_only_accepts_the_resource_form():
    assert canonical_target("network://https/example.com/a").to_uri() == (
        "network://https/example.com/a"
    )
    for bad in ("https://example.com/a", "http://example.com/a", "/etc/passwd", ""):
        with pytest.raises(NetError):
            canonical_target(bad)


def test_summary_text_never_splits_a_utf8_character():
    class _Faked:
        body = "中文".encode() * 100
        truncated = False

    text, clipped = summary_text(_Faked(), limit_bytes=4)  # type: ignore[arg-type]
    assert clipped is True
    assert text == "中"  # 4 字节只能放 1 个汉字（3 字节）+ 半个 → 退到整字
    assert len(text.encode()) <= 4
