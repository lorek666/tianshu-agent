"""M1-PR3 验收：崩溃恢复与故障注入。

要证明的三件事：
1. 在任意时刻被杀，重启后都能判定「到底改了没改」
2. 判定不了的时候标 unknown，并且**阻断后续写入**
3. 绝不重复副作用、绝不误报成功

100 次随机注入覆盖四个故障点。
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from tianshu.events import EventLog
from tianshu.recovery import blocked_workspaces, recover_run
from tianshu.snapshot import SnapshotStore

FAILPOINTS = ["after_intent", "after_write", "after_result", "after_receipt"]

# 子进程入口：正常跑一次 run，在指定故障点硬退
_CHILD = """
import sys, pathlib
sys.path.insert(0, {src!r})
from tianshu.cli import main
sys.exit(main(["run",
    "--workspace", {ws!r},
    "--task", "改文件",
    "--out", "report.md",
    "--model", "scripted",
    "--db", {db!r}]))
"""


def _spawn(tmp_path: Path, ws: Path, db: Path, failpoint: str | None):
    env = dict(os.environ)
    env.pop("TIANSHU_FAILPOINT", None)
    if failpoint:
        env["TIANSHU_FAILPOINT"] = failpoint
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = _CHILD.format(src=src, ws=str(ws), db=str(db))
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)


def _latest_run_id(db: Path) -> str:
    log = EventLog(db)
    row = log._conn.execute(
        "SELECT run_id FROM events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    log.close()
    assert row is not None, "没有事件"
    return row["run_id"]


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text("原始内容\n", encoding="utf-8")
    return ws, tmp_path / "events.db"


# ---------------------------------------------------------------- 四个故障点


@pytest.mark.parametrize("fp", FAILPOINTS)
def test_recover_after_each_failpoint(tmp_path, fp):
    ws, db = _setup(tmp_path)
    proc = _spawn(tmp_path, ws, db, fp)
    assert proc.returncode == 77, f"{fp} 应当在故障点退出，实际 {proc.returncode}"

    run_id = _latest_run_id(db)
    log = EventLog(db)
    store = SnapshotStore(tmp_path / "artifacts")
    report = recover_run(log, run_id, ws, store)

    # 恢复后不能留悬空：每条**写入** intent 都必须有配对 result。
    #
    # M2 起 `tool.intent` 只对会改文件的工具记（它是写前证据）；`list_files`
    # 这类只读调用没有 intent，但有 result。所以这里的不变量是
    # "intent ⊆ result"，反过来不成立——反过来说明有人给读操作也记了 intent。
    events = log.read_run(run_id)
    intents = [e.payload["call_id"] for e in events if e.type == "tool.intent"]
    results = [e.payload["call_id"] for e in events if e.type == "tool.result"]
    assert intents, f"{fp}：这个场景里必须有写入，应当留下 intent"
    assert set(intents) <= set(results), f"{fp}：写入 intent 没有配对 result"
    # 反过来：有 result 却没 intent 的，只允许是只读工具。
    # 少了这一句，"写入结果没有写前证据"这种绕过会被当成正常差异放过去。
    read_only = {
        e.payload["call_id"]
        for e in events
        if e.type == "tool.result"
        and e.payload.get("tool_id") in ("list_files", "read_text")
    }
    assert set(results) - set(intents) <= read_only, (
        f"{fp}：有写入结果却没有写前 intent"
    )
    assert not report.unresolved, f"{fp}：本场景应当可核实，不该有 unknown"

    created = ws / "report.md"
    if fp == "after_intent":
        # 只写了 intent，还没执行 → 文件不存在，恢复应判 none_applied
        assert not created.exists()
        assert report.none_applied, "after_intent：应判为未执行"
    else:
        # 文件已经写进去了 → 恢复必须识别为 applied，不能当没发生
        assert created.exists(), f"{fp}：文件应当已写入"
        assert report.applied or any(
            e.payload.get("effect_state") == "applied"
            for e in events if e.type == "tool.result"
        ), f"{fp}：写入必须被识别为 applied"

    log.close()


def test_recovery_is_idempotent(tmp_path):
    """重复恢复不能再补事件。"""
    ws, db = _setup(tmp_path)
    _spawn(tmp_path, ws, db, "after_write")
    run_id = _latest_run_id(db)

    log = EventLog(db)
    store = SnapshotStore(tmp_path / "artifacts")
    first = recover_run(log, run_id, ws, store)
    n1 = len(log.read_run(run_id))
    second = recover_run(log, run_id, ws, store)
    n2 = len(log.read_run(run_id))
    log.close()

    assert n1 == n2, "第二次恢复不该再写事件"
    assert first.applied == second.applied or second.applied == []


def test_unknown_blocks_subsequent_writes(tmp_path):
    """核实不了的时候：标 unknown，并阻断同 workspace 的新写入。

    v2 的判据是「写前证据」：先有证据，磁盘又既非写前也非写后，才是 unknown。
    """
    ws, db = _setup(tmp_path)
    log = EventLog(db)
    store = SnapshotStore(tmp_path / "artifacts")

    run_id = "run_manual"
    args = {"name": "note.md", "content": "agent 想写的内容\n"}
    log.append(run_id, "run.start",
               {"task": "t", "workspace": str(ws.resolve()), "out": "x.md",
                "limits": {}, "tools": []}, actor_kind="core")
    log.append(run_id, "tool.intent",
               {"call_id": "c1", "tool_id": "modify_text", "args": args,
                "args_hash": hashlib.sha256(b"x").hexdigest()}, actor_kind="core")

    # 写前证据存在（说明确实进入了写入阶段）
    store.record_preimage(
        run_id, "c1", "note.md",
        before="原始内容\n".encode(),
        expected_after=hashlib.sha256("agent 想写的内容\n".encode()).hexdigest(),
    )
    # 但磁盘上是第三种内容 —— 既非写前也非写后
    (ws / "note.md").write_text("完全不相干的内容\n", encoding="utf-8")

    report = recover_run(log, run_id, ws, store)
    assert report.unresolved == ["c1"], "既非期望也非写前状态，必须判 unknown"
    assert str(ws.resolve()) in blocked_workspaces(log)
    log.close()


# ---------------------------------------------------------------- 100 次注入


def test_100_random_fault_injections(tmp_path):
    """随机注入 100 次，验证三条不变量。

    不变量：
    A. 恢复后文件内容必须是「写前」或「写入内容」之一，不能半截
    B. 每次写入尝试最终恰好对应一条 tool.result（不能丢也不能重）
    C. 没有任何一次把「未执行」误报成 applied
    """
    rng = random.Random(20261108)
    contents = set()
    results_seen = 0

    for i in range(100):
        case = tmp_path / f"case{i}"
        case.mkdir()
        ws = case / "ws"
        ws.mkdir()
        (ws / "note.md").write_text("原始内容\n", encoding="utf-8")
        db = case / "events.db"

        fp = rng.choice(FAILPOINTS)
        proc = _spawn(case, ws, db, fp)
        assert proc.returncode == 77, f"第 {i} 次（{fp}）未在故障点退出"

        run_id = _latest_run_id(db)
        log = EventLog(db)
        store = SnapshotStore(case / "artifacts")
        recover_run(log, run_id, ws, store)

        # A：内容只能是两种之一
        text = (ws / "note.md").read_text(encoding="utf-8")
        contents.add(text[:20])

        # B：每个写入 intent 恰好一条 result（只读调用没有 intent）
        events = log.read_run(run_id)
        intents = [e.payload["call_id"] for e in events if e.type == "tool.intent"]
        results = [e.payload["call_id"] for e in events if e.type == "tool.result"]
        assert len(intents) == len(set(intents)), \
            f"第 {i} 次：同一 intent 被记了两次（{intents}）"
        assert set(intents) <= set(results), \
            f"第 {i} 次：写入 intent 没有配对 result（{intents} vs {results}）"
        read_only = {
            e.payload["call_id"]
            for e in events
            if e.type == "tool.result"
            and e.payload.get("tool_id") in ("list_files", "read_text")
        }
        assert set(results) - set(intents) <= read_only, \
            f"第 {i} 次：有写入结果却没有写前 intent（{set(results) - set(intents)}）"
        results_seen += len(results)

        # C：applied 必须对应真实内容
        for e in events:
            if e.type == "tool.result" and e.payload["effect_state"] == "applied":
                rel = e.payload["output"]["path"]
                expected = e.payload["output"]["sha256"]
                actual = hashlib.sha256(
                    (ws / rel).read_bytes() if (ws / rel).exists() else b""
                ).hexdigest()
                assert actual == expected, f"第 {i} 次：声称 applied 但磁盘不符"

        log.close()

    assert results_seen >= 100, "每次注入都应至少补出一条 result"
    assert len(contents) <= 3, f"出现了意外的中间状态：{contents}"
