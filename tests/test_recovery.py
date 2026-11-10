"""M1-PR3 验收：崩溃恢复与故障注入。

要证明的三件事：
1. 在任意时刻被杀，重启后都能判定「到底改了没改」
2. 判定不了的时候标 unknown，并且**阻断后续写入**
3. 绝不重复副作用、绝不误报成功

100 次注入覆盖四个故障点 × 三条写路径：`create_report`（新建）、
`modify_text`（覆盖已存在文件）、`delete_file`（删除，issue #54 补的第三格：
恢复后目标只能是「仍存在且为写前内容」或「已删除」，applied 的 after_state
必须是 absent，撤销按 preimage 重建回原字节）。

复审报告 中-3 指出旧版的两处证据缺口，本版对应修法：
- 注入只走 `create_report`：cli 的 scripted 模型只发 list_files + create_report。
  现在子进程用公开 API 直接装配一次 run（`build_stack` + `AgentLoop` +
  `ScriptedModel`，与 `tianshu.loop.run` 同一条装配路径），命令它发
  `modify_text`；不改 `src/tianshu/cli.py`，也不加 `--model` 模式。
- 不变量 A 读的是 `note.md`：那是预置后从未被写的诱饵。现在从 `tool.intent`
  的参数解析**真实写入目标**再逐字节核对；`note.md` 反过来被断言「没被动过」。
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from support import CHILD_TEXT, write_exact

from tianshu.events import EventLog
from tianshu.recovery import blocked_workspaces, recover_run
from tianshu.snapshot import ABSENT, SnapshotStore

FAILPOINTS = ["after_intent", "after_write", "after_result", "after_receipt"]

#: 三条受控写路径。create_report 是新建（写前状态 = absent）；
#: modify_text 是覆盖写（写前状态 = 已有内容）——中-3 缺的是覆盖写；
#: delete_file 是删除（写后状态 = absent，issue #54 缺的第三格）。
KINDS = ("create", "overwrite", "delete")
WRITE_TOOL = {
    "create": "create_report", "overwrite": "modify_text", "delete": "delete_file",
}
WRITE_KEY = {"create": "name", "overwrite": "path", "delete": "path"}
READ_ONLY_TOOLS = ("list_files", "read_text")

#: 真实写入目标。三个文件里只有它会被写：note.md 是诱饵（旧版不变量 A 读的
#: 就是它），不许被动；report.md 是三条写路径的共同目标。
TARGET = "report.md"
NOTE = "原始内容\n"
BEFORE = "写前内容\n"
AFTER = "写入内容\n"
NOTE_BYTES = NOTE.encode("utf-8")
BEFORE_BYTES = BEFORE.encode("utf-8")
AFTER_BYTES = AFTER.encode("utf-8")
BEFORE_SHA = hashlib.sha256(BEFORE_BYTES).hexdigest()
AFTER_SHA = hashlib.sha256(AFTER_BYTES).hexdigest()
#: 三条写路径的写后状态。删除的写后状态就是"不存在"，与快照证据的 ABSENT 同义。
AFTER_STATE = {"create": AFTER_SHA, "overwrite": AFTER_SHA, "delete": ABSENT}


# 子进程入口：直接用公开 API 装配一次 run，在指定故障点硬退。
#
# 为什么不走 `tianshu.cli main(["run", ...])`：cli 的 scripted 模型只会
# list_files + create_report（中-3 的根因），而这里要命令它发 modify_text。
#
# modify_text 是覆盖类动作（ACTION_OVERWRITE ∈ APPROVAL_ACTIONS），Run 会先
# 停在 waiting_approval。子进程按 tests/support.py::approve_pending 的同一
# 公开入口（approvals.decide + resume_run）模拟「人在 CLI 上敲了 approve」：
# 授权该判的、该落库的、该在执行侧消费的一样没少，省掉的只是等人。
_CHILD = """
import sys, pathlib
sys.path.insert(0, {src!r})
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, Limits, build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import HumanIdentity

ws = pathlib.Path({ws!r})
db = pathlib.Path({db!r})
write = ToolCall("c2", {tool_id!r}, {args!r})
model = ScriptedModel([
    ModelResponse("tool_use", tool_calls=[ToolCall("c1", "list_files", {{"path": "."}})]),
    ModelResponse("tool_use", tool_calls=[write]),
    ModelResponse("end_turn", content="完成"),
])
log = EventLog(db)
stack = build_stack(log)
engine = AgentLoop(
    tasks=stack.tasks,
    leases=stack.leases,
    claims=stack.claims,
    dispatcher=stack.dispatcher,
    approvals=stack.approvals,
    snapshots=stack.snapshots,
    model=model,
    limits=Limits(8, 20_000),
)
task = stack.tasks.create_task("改文件", [str(ws)])
result = engine.start(task.task_id, out_name={target!r})
if result.state == "waiting_approval":
    actor = HumanIdentity.local_cli()
    for approval in stack.approvals.pending(run_id=result.run_id):
        stack.approvals.decide(approval.approval_id, decision="approved", actor=actor)
    result = engine.resume_run(result.run_id)
sys.exit(0 if result.terminal else 1)
"""


def _spawn(ws: Path, db: Path, failpoint: str | None, kind: str):
    env = dict(os.environ)
    env.pop("TIANSHU_FAILPOINT", None)
    if failpoint:
        env["TIANSHU_FAILPOINT"] = failpoint
    src = str(Path(__file__).resolve().parents[1] / "src")
    args = {"path": TARGET, "content": AFTER} if kind != "create" else {
        "name": TARGET, "content": AFTER}
    if kind == "delete":
        args = {"path": TARGET}
    code = _CHILD.format(
        src=src,
        ws=str(ws),
        db=str(db),
        tool_id=WRITE_TOOL[kind],
        args=args,
        target=TARGET,
        after=AFTER,
    )
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, **CHILD_TEXT, timeout=120)


def _latest_run_id(db: Path) -> str:
    log = EventLog(db)
    row = log._conn.execute(
        "SELECT run_id FROM events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    log.close()
    assert row is not None, "没有事件"
    return row["run_id"]


def _setup(tmp_path: Path, kind: str = "create") -> tuple[Path, Path]:
    """建一个 case 工作区。

    note.md 是**诱饵**：旧版不变量 A 读的就是它（预置后从未被写），于是
    「写前/写后」断言其实一直在讲一个与写入无关的文件。它保留下来当反证。
    overwrite / delete 还要预置 report.md——modify_text 只改已存在的文件，
    delete_file 只删已存在的普通文件。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", NOTE)
    if kind in ("overwrite", "delete"):
        write_exact(ws / TARGET, BEFORE)
    return ws, tmp_path / "events.db"


def _write_call(events: list, kind: str) -> tuple[str, str]:
    """从 tool.intent 里取出本次写入的 call_id 与相对目标路径。

    目标路径从 intent 参数解析（create 用 name、overwrite 用 path），不靠
    测试侧写死：这样不变量 A 核对的必然是这次 Run 真正想写的那个文件。
    如果哪天注入又退回「只写 note.md」，这里会先红，而不是让 A 继续对着
    一个诱饵文件自说自话（中-3 的原始缺陷）。
    """
    intents = [e for e in events if e.type == "tool.intent"]
    assert len(intents) == 1, f"应当恰好一条写入 intent，实际 {len(intents)}"
    payload = intents[0].payload
    assert payload["tool_id"] == WRITE_TOOL[kind], (
        f"注入走了错误工具：{payload['tool_id']} != {WRITE_TOOL[kind]}"
    )
    rel = payload["args"].get(WRITE_KEY[kind])
    assert rel == TARGET, f"写入目标应当是 {TARGET}，实际 {rel!r}"
    return payload["call_id"], rel


def _write_result(events: list, call_id: str) -> dict:
    rows = [
        e.payload
        for e in events
        if e.type == "tool.result" and e.payload["call_id"] == call_id
    ]
    assert len(rows) == 1, f"写入调用 {call_id} 应当恰好一条 result，实际 {len(rows)}"
    return rows[0]


def _check_b(events: list, call_id: str) -> None:
    """不变量 B：每个写入 intent 恰好一条 result；只读结果可以没有 intent。

    M2 起 `tool.intent` 只对会改文件的工具记（它是写前证据）；`list_files`
    这类只读调用没有 intent，但有 result。所以方向是 "intent ⊆ result"，
    反过来只允许是只读工具——反过来说明有人给读操作也记了 intent。
    """
    result_calls = Counter(
        e.payload["call_id"] for e in events if e.type == "tool.result"
    )
    intents = [e.payload["call_id"] for e in events if e.type == "tool.intent"]
    assert len(intents) == len(set(intents)), f"同一 intent 被记了两次（{intents}）"
    assert call_id in intents, "写入没有留下写前 intent"
    assert set(intents) <= set(result_calls), "写入 intent 没有配对 result"
    # 少了这一句，"写入结果没有写前证据"这种绕过会被当成正常差异放过去。
    read_only = {
        e.payload["call_id"]
        for e in events
        if e.type == "tool.result"
        and e.payload.get("tool_id") in READ_ONLY_TOOLS
    }
    assert set(result_calls) - set(intents) <= read_only, "有写入结果却没有写前 intent"
    assert all(n == 1 for n in result_calls.values()), (
        f"同一个 call 的 result 重复了：{dict(result_calls)}"
    )


def _check_c(events: list, call_id: str, kind: str, ws: Path) -> str:
    """不变量 C：声称 applied 的必须与磁盘/证据逐字节一致，返回 effect_state。

    `before_state` 也要对得上：create 是 `absent`，overwrite 是写前内容的
    哈希。只核对 after 一边，"拿一个旧写入冒充这次"就查不出来。
    """
    payload = _write_result(events, call_id)
    state = payload["effect_state"]
    if state != "applied":
        return state
    out = payload["output"]
    expected_before = ABSENT if kind == "create" else BEFORE_SHA
    expected_after = AFTER_STATE[kind]
    assert out["path"] == TARGET, f"applied 结果指向了别的文件：{out['path']}"
    assert out["after_state"] == expected_after, (
        f"{kind} 的 after_state 应当是 {expected_after}，实际 {out['after_state']}"
    )
    assert out["before_state"] == expected_before, (
        f"before_state 不对：{out['before_state']} != {expected_before}"
    )
    if kind == "delete":
        # issue #54 的核心：删除的 applied 不允许把"已删除"写成 0 字节文件，
        # 也不允许去读一个不存在的目标。
        assert out["deleted"] is True, "删除的 applied 结果必须显式标 deleted"
        assert not (ws / out["path"]).exists(), "声称删除但文件还在"
    else:
        assert out["sha256"] == AFTER_SHA, "applied 的 sha256 不是意图写入的内容"
        assert out["after_state"] == AFTER_SHA, "after_state 与 sha256 自相矛盾"
        actual = hashlib.sha256((ws / out["path"]).read_bytes()).hexdigest()
        assert actual == out["sha256"], "声称 applied 但磁盘 sha256 与结果不符"
        assert actual == AFTER_SHA, "磁盘内容不是意图写入的内容"
    return state


def _check_approval_path(events: list, kind: str) -> None:
    """覆盖写必须真的过审批：子进程模拟的是「人批准」，不是「绕过授权」。

    create_report 是自动动作（AUTO_ACTIONS），不该出现审批事件；modify_text
    （`ACTION_OVERWRITE`）与 delete_file（`ACTION_DELETE`）必须有
    requested → decided → consumed 的完整痕迹：
    decided 是子进程模拟人敲的那一下，consumed 是执行侧真正消费了这张批准。
    少了任何一段，就说明测试在偷偷绕过授权。
    """
    types = [e.type for e in events]
    if kind == "create":
        assert "approval.requested" not in types, "create_report 不该触发审批"
        return
    for expected in ("approval.requested", "approval.decided", "approval.consumed"):
        assert expected in types, f"覆盖写缺少审批痕迹：{expected}"


# ---------------------------------------------------------------- 四个故障点


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fp", FAILPOINTS)
def test_recover_after_each_failpoint(tmp_path, fp, kind):
    ws, db = _setup(tmp_path, kind)
    proc = _spawn(ws, db, fp, kind)
    assert proc.returncode == 77, (
        f"{kind}/{fp} 应当在故障点退出，实际 {proc.returncode}；stderr={proc.stderr}"
    )
    # 退出码 77 还不够：要确认炸的是**这次指定的**故障点，不是别的路径碰巧 77。
    assert f"[failpoint] {fp}" in proc.stderr, (
        f"{kind}/{fp}：stderr 里没有该故障点标记；stderr={proc.stderr}"
    )

    run_id = _latest_run_id(db)
    log = EventLog(db)
    store = SnapshotStore(tmp_path / "artifacts")
    report = recover_run(log, run_id, ws, store)

    events = log.read_run(run_id)
    call_id, rel = _write_call(events, kind)
    _check_approval_path(events, kind)
    _check_b(events, call_id)
    state = _check_c(events, call_id, kind, ws)
    assert not report.unresolved, f"{kind}/{fp}：本场景应当可核实，不该有 unknown"

    # 诱饵没被动过：A 核对的不能是它。
    assert (ws / "note.md").read_bytes() == NOTE_BYTES, "note.md 被写了"

    before_bytes = None if kind == "create" else BEFORE_BYTES
    after_bytes = None if kind == "delete" else AFTER_BYTES
    observed = (ws / rel).read_bytes() if (ws / rel).exists() else None
    if fp == "after_intent":
        # 只写了 intent，还没执行：目标仍是写前状态（新建=不存在 / 覆盖或删除=原文）
        assert observed == before_bytes, f"{kind}/{fp}：文件不该被改动"
        assert state == "none_applied", f"{kind}/{fp}：没执行却报了 {state}"
        assert call_id in report.none_applied, f"{kind}/{fp}：应判为未执行"
    else:
        # 副作用已经发生 → 恢复必须识别为 applied，不能当没发生
        assert observed == after_bytes, f"{kind}/{fp}：目标应当处于写后状态"
        assert state == "applied", f"{kind}/{fp}：已执行却判 {state}"
        assert call_id in report.applied or any(
            e.payload.get("effect_state") == "applied"
            for e in events
            if e.type == "tool.result" and e.payload["call_id"] == call_id
        ), f"{kind}/{fp}：写入必须被识别为 applied"

    log.close()


def test_recovery_is_idempotent(tmp_path):
    """重复恢复不能再补事件，也不能再动文件（覆盖写路径）。"""
    ws, db = _setup(tmp_path, "overwrite")
    _spawn(ws, db, "after_write", "overwrite")
    run_id = _latest_run_id(db)

    log = EventLog(db)
    store = SnapshotStore(tmp_path / "artifacts")
    first = recover_run(log, run_id, ws, store)
    content1 = (ws / TARGET).read_bytes()
    n1 = len(log.read_run(run_id))
    second = recover_run(log, run_id, ws, store)
    content2 = (ws / TARGET).read_bytes()
    n2 = len(log.read_run(run_id))
    log.close()

    assert content1 == AFTER_BYTES
    assert content2 == content1, "第二次恢复不能再改文件"
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
        expected_after=hashlib.sha256("agent 想写的内容\n".encode()).hexdigest(), root=ws,
    )
    # 但磁盘上是第三种内容 —— 既非写前也非写后
    write_exact(ws / "note.md", "完全不相干的内容\n")

    report = recover_run(log, run_id, ws, store)
    assert report.unresolved == ["c1"], "既非期望也非写前状态，必须判 unknown"
    assert str(ws.resolve()) in blocked_workspaces(log)
    log.close()


# ---------------------------------------------------------------- 100 次注入


def test_100_random_fault_injections(tmp_path):
    """随机注入 100 次，验证四条不变量。

    矩阵是**配平**的：12 个 (写类型, 故障点) 组合各 8 次 = 96，另给
    delete 的每个故障点补 1 次 = 100（issue #54）；随机（种子固定）只用来
    打乱执行顺序。纯随机抽样可能恰好漏掉某个组合，那样又回到 中-3 说的
    窄证据面——覆盖由矩阵保证，不由运气保证。

    不变量：
    A. **真实写入目标**的内容必须是「写前」或「写入内容」之一（逐字节，不许
       半截）；第二次恢复不再改变内容，也不再补事件
    B. 每个写入 intent 恰好对应一条 tool.result（不丢不重）
    C. 声称 applied 的，output 的 sha256/after_state 必须与磁盘一致，
       before_state 必须是写前状态；没执行的那次不许报 applied
    D. applied 恰好对应一张 receipt（凭据也不能重复发）
    """
    rng = random.Random(20261108)
    cases = [(kind, fp) for kind in KINDS for fp in FAILPOINTS for _ in range(8)]
    cases += [("delete", fp) for fp in FAILPOINTS]  # 补到 100，删除多一份
    rng.shuffle(cases)

    assert len(cases) == 100, "注入总量应当保持 100 次量级"
    counts = Counter(cases)
    for kind in KINDS:
        for fp in FAILPOINTS:
            assert counts[(kind, fp)] >= 8, f"矩阵漏了 {kind}/{fp}"

    for i, (kind, fp) in enumerate(cases):
        case = tmp_path / f"case{i}"
        case.mkdir()
        ws, db = _setup(case, kind)
        before_bytes = None if kind == "create" else BEFORE_BYTES
        after_bytes = None if kind == "delete" else AFTER_BYTES

        proc = _spawn(ws, db, fp, kind)
        assert proc.returncode == 77, (
            f"第 {i} 次（{kind}/{fp}）未在故障点退出：rc={proc.returncode}；"
            f"stderr={proc.stderr}"
        )
        assert f"[failpoint] {fp}" in proc.stderr, (
            f"第 {i} 次（{kind}/{fp}）：stderr 里没有该故障点标记；stderr={proc.stderr}"
        )

        run_id = _latest_run_id(db)
        log = EventLog(db)
        store = SnapshotStore(case / "artifacts")
        events_before = log.read_run(run_id)
        call_id, rel = _write_call(events_before, kind)

        report = recover_run(log, run_id, ws, store)
        assert not report.unresolved, f"第 {i} 次（{kind}/{fp}）：不该有 unknown"

        # A：真实写入目标只能是写前或写后二者之一，且诱饵文件没被动过
        target = ws / rel
        content1 = target.read_bytes() if target.exists() else None
        assert content1 in (before_bytes, after_bytes), (
            f"第 {i} 次（{kind}/{fp}）：出现第三种内容 {content1!r}"
        )
        assert (ws / "note.md").read_bytes() == NOTE_BYTES, (
            f"第 {i} 次（{kind}/{fp}）：note.md 被写了"
        )

        # A（续）：第二次恢复不得再改内容，也不许再补事件——恢复不是"再写一次"
        n1 = len(log.read_run(run_id))
        recover_run(log, run_id, ws, store)
        content2 = target.read_bytes() if target.exists() else None
        n2 = len(log.read_run(run_id))
        assert content2 == content1, f"第 {i} 次（{kind}/{fp}）：第二次恢复改了内容"
        assert n2 == n1, f"第 {i} 次（{kind}/{fp}）：第二次恢复又补了事件"

        events = log.read_run(run_id)
        _check_approval_path(events, kind)
        _check_b(events, call_id)

        # C：applied 必须与磁盘一致；after_intent 那次必须如实判未执行
        state = _check_c(events, call_id, kind, ws)
        if fp == "after_intent":
            assert state == "none_applied", (
                f"第 {i} 次（{kind}/{fp}）：没执行却报了 {state}"
            )
            assert content1 == before_bytes, (
                f"第 {i} 次（{kind}/{fp}）：判未执行但文件被改了"
            )
        else:
            assert state == "applied", f"第 {i} 次（{kind}/{fp}）：已执行却判 {state}"
            assert content1 == after_bytes, (
                f"第 {i} 次（{kind}/{fp}）：判 applied 但磁盘不是写后状态"
            )

        # D：applied 恰好一张凭据；未执行不产凭据（凭据粉饰不确定状态是不许的）
        receipts = [
            e for e in events
            if e.type == "receipt" and e.payload.get("call_id") == call_id
        ]
        assert len(receipts) == (1 if state == "applied" else 0), (
            f"第 {i} 次（{kind}/{fp}）：凭据数 {len(receipts)} 与效果 {state} 不符"
        )
        log.close()
