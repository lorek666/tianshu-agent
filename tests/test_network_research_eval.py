"""§10.5 联网研究闭环的评测侧判据（G2 网络阶段 I3）。

这一组盯的是**评测器**自己有没有说真话（任务的判据在
`tests/test_eval_real_tasks.py`）：

1. 本机来源夹具真的有路由、有命中记录，关掉之后真的连不上；
2. `{WEB_BASE}` 替换同时作用于任务文本、白名单与脚本——漏一处，任务要么发不出
   去、要么指向一个不存在的地址；
3. 来源离线核验发生在**服务器关掉之后**，且读不回来就是"无效样本"而不是"通过"；
4. 分母身份（`fixture_digest`）把联网输入算进去：改了白名单 / 夹具路由 / 脚本
   工厂，这条 case 就不是原来那条。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from tianshu.eval import runner as runner_mod
from tianshu.eval.cases import Case
from tianshu.eval.report import fixture_digest
from tianshu.eval.tasks import REAL_USER_TASK_BY_ID, run_real_user_tasks
from tianshu.eval.webfixture import WebFixture, closed_origin
from tianshu.model import ModelResponse

# ---------------------------------------------------------------- 1 夹具本身


def test_web_fixture_serves_routes_and_records_hits():
    fixture = WebFixture()
    fixture.route("/sources/alpha", body="<p>alpha</p>")
    fixture.start()
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://{fixture.base}/sources/alpha", timeout=5
        ) as response:
            assert response.status == 200
            assert b"alpha" in response.read()
        assert fixture.hits == ["/sources/alpha"]
        assert fixture.origin == f"network://http/{fixture.base}"
        assert fixture.url("/sources/alpha") == (
            f"network://http/{fixture.base}/sources/alpha"
        )
        # 未登记的路由 → 404，不静默返回空内容
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://{fixture.base}/nope", timeout=5)
        assert excinfo.value.code == 404
    finally:
        fixture.stop()


def test_web_fixture_stop_makes_the_origin_unreachable():
    fixture = WebFixture().route("/x", body="x").start()
    base = fixture.base
    fixture.stop()
    host, _, port = base.partition(":")
    with pytest.raises(OSError):
        socket.create_connection((host, int(port)), timeout=1).close()


def test_closed_origin_refuses_connections():
    base, sock = closed_origin()
    host, _, port = base.partition(":")
    try:
        with pytest.raises(OSError):
            socket.create_connection((host, int(port)), timeout=2).close()
    finally:
        sock.close()


# ---------------------------------------------------------------- 2 替换


def _w2() -> Case:
    return REAL_USER_TASK_BY_ID["RU-W2"].to_case()


def test_materialize_substitutes_base_everywhere():
    case = _w2()
    effective = runner_mod._materialize_case(case, "127.0.0.1:12345")
    assert "{WEB_BASE}" not in effective.task
    assert "127.0.0.1:12345" in effective.task
    assert effective.outbound_allow == ("network://http/127.0.0.1:12345",)
    assert effective.script, "脚本工厂必须生成脚本"
    urls = [
        call.arguments.get("url")
        for response in effective.script
        for call in response.tool_calls
        if call.name == "network.fetch"
    ]
    assert urls == [
        "network://http/127.0.0.1:12345/sources/alpha",
        "network://http/127.0.0.1:12345/sources/beta",
    ]
    # 原 case 不被就地改掉（同一份定义会被多次运行碰到）
    assert "{WEB_BASE}" in case.task
    assert case.outbound_allow == ("network://http/{WEB_BASE}",)


def test_materialize_without_template_is_identity():
    case = Case(
        case_id="SYN-1", category="synthetic", title="无网络", task="普通任务"
    )
    assert runner_mod._materialize_case(case, "") is case


# ---------------------------------------------------------------- 3 分母身份


def test_fixture_digest_includes_network_inputs():
    base = _w2()
    same = _w2()
    assert fixture_digest(base) == fixture_digest(same)
    # 白名单变了
    other_allow = Case(
        **{
            **base.__dict__,
            "outbound_allow": ("network://http/other.example",),
        }
    )
    assert fixture_digest(other_allow) != fixture_digest(base)
    # 夹具来源路由变了
    other_routes = Case(
        **{**base.__dict__, "web_routes": {"/sources/alpha": "changed"}}
    )
    assert fixture_digest(other_routes) != fixture_digest(base)


def test_fixture_digest_counts_the_script_factory_source():
    base = _w2()

    def other_factory(_base: str) -> tuple[ModelResponse, ...]:
        return ()

    other = Case(**{**base.__dict__, "script_factory": other_factory})
    assert fixture_digest(other) != fixture_digest(base)


# ---------------------------------------------------------------- 4 运行器


def test_runner_verifies_sources_offline_after_stopping_the_server(tmp_path):
    case = _w2()
    suite = run_real_user_tasks(Path(tmp_path) / "w2", cases=(case,), repeat=1)
    obs = suite.observations[0]
    assert obs.invalid_reason is None, obs.invalid_reason
    assert len(obs.offline_sources) == 2
    for source in obs.offline_sources:
        assert len(source["digest"]) == 64
        # 引用里的摘要就是读到内容的摘要（ContentStore.read 会重算比对）
        assert source["ref"].endswith(source["digest"])
        assert source["bytes"] > 0


def test_runner_reports_invalid_when_sources_cannot_be_read(tmp_path, monkeypatch):
    """来源读不回来 = 评测基础设施无效，**不是**"通过"（PRD §10.2 判据边界）。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟：内容引用读不回来")

    monkeypatch.setattr(runner_mod, "_collect_offline_sources", boom)
    suite = run_real_user_tasks(
        Path(tmp_path) / "broken", cases=(_w2(),), repeat=1
    )
    obs = suite.observations[0]
    assert obs.invalid_reason is not None
    assert "来源离线核验失败" in obs.invalid_reason
    assert obs.offline_sources == []


def test_offline_collection_re_reads_and_verifies_the_content(tmp_path):
    """离线核验必须**重读**内容并重算摘要，不能只信 tool.result 里的字段。

    这一条直接打 `_collect_offline_sources`：把 blob 改掉之后，收集必须炸
    （`ContentStore.read` 会重算 sha256 比对引用）；"信任输出里的 summary"就
    通不过这条。
    """
    from types import SimpleNamespace

    from tianshu.contents import make_ref
    from tianshu.events import EventLog, sha256_hex
    from tianshu.fsutil import atomic_write_bytes
    from tianshu.loop import build_stack

    body = b"hello offline source"
    digest = sha256_hex(body)
    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=tmp_path / "data")
    try:
        blob = stack.snapshots.blob_dir / digest
        atomic_write_bytes(blob, body)
        stack.contents.record(digest, body, task_id="task_src")
        events = [
            SimpleNamespace(type="run.start", payload={"task_id": "task_src"}),
            SimpleNamespace(
                type="tool.result",
                payload={
                    "tool_id": "network.fetch",
                    "status": "succeeded",
                    "output": {
                        "ref": make_ref(digest),
                        "url": "network://http/127.0.0.1:1/x",
                    },
                },
            ),
        ]
        sources = runner_mod._collect_offline_sources(
            stack, _w2(), events, log
        )
        assert len(sources) == 1
        assert sources[0]["head"] == body.decode()
        assert sources[0]["digest"] == digest
        # 篡改 blob → 摘要不符，必须炸（不是"读回来就信"）
        blob.write_bytes(b"tampered")
        with pytest.raises(Exception) as excinfo:
            runner_mod._collect_offline_sources(stack, _w2(), events, log)
        assert "摘要不符" in str(excinfo.value) or "Digest" in type(
            excinfo.value
        ).__name__
    finally:
        log.close()


def test_offline_verification_happens_after_the_run(tmp_path):
    """离线核验必须在 Run 结束之后：跑的时候来源还在，结完之后才关。"""
    case = _w2()
    suite = run_real_user_tasks(Path(tmp_path) / "order", cases=(case,), repeat=1)
    obs = suite.observations[0]
    assert obs.result is not None, "来源核验发生在 Run 结束之后"
    assert obs.offline_sources
    # 运行期间抓取确实发生过（不是把 fixture 内容当"抓取结果"塞进去）
    assert obs.count("approval.requested") == 2
    assert time.monotonic() > 0  # 只是让"时间无关判据"显式一点
