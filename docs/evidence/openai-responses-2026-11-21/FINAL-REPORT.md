# Responses 冻结代码树独立复核

对象：`b7c3773a7dcd310aafe765f2c456313a59742a2c`，Git tree `eed4d8fd25f9b25745cb8a2469fbc794a0faeaab`；直接父提交 `6e9bf340f7c7fe728f0eb19f44818b799ad1369d`，共同基线 `71eb57ef1544d126cc929b9172f5463a14999359`。独立工作树 `responses-independent-review`，2026-11-21。

**结论：PASS（本次受影响范围的 Tier 1 独立复核）。** 上轮两个阻断根因均为 **fixed / 已修**；本轮没有部分修复、未修或新确认的直接阻断项。三个冻结探针文件逐字复跑 24 项通过，独立扩展 4 项通过，两个 pytest 进程均退出 0。此结论允许进入同一代码树的最终本机全量验证，不代替全量、CI 或合并批准。

## 两项阻断的复核

1. **P1，父 Run 挂起保存点：fixed / 已修。** 在真实 `subtask.spawn`、审批与挂起流程中，之前拒绝的游标（seq 36）与 `job.parked`（seq 37）现在可以重建。投影完整时整库重建成功；删除该 Run 的游标和 turn 后直接恢复也成功。重建只有在两个事件相邻、序号连续、属于同一个 Run、`claim_epoch` 相同且原因为 `waiting_children` 时，才提前清空 `model_turn`；历史游标仍逐个核对摘要。独立负控分别改变前一游标摘要、挂起游标摘要、事件序号、Run、claim、挂起原因，或移除挂起事件，七种均被拒绝。完整生命周期中父 Run 在不同 claim 下挂起两次，实际 Worker 完成子 Run 后唤醒父 Run；两者最终均结束。删除两者派生行后连续两次整库重建，游标摘要、claim 与不可变事件均保持一致，父子离线 replay 均成功。
2. **P2，首个显式游标前的尝试数：fixed / 已修。** 重建器现在以 `run.start` 对应的初始游标为基准；没有 `run.cursor` 时，返回的初始游标不含尚未提交的尝试。恢复入口从 intent 补计，未再重复。原例恢复后两条 intent、返回及游标均报 2；错账导致后续整库重建失败的用例也已通过。另以真实控制面创建 Run，连续两个不同子进程在 `model-result-before-commit` 退出 77。这时已有 `[1,2]` 两条 intent、没有 `run.cursor` 或 `model.result`，但存在真实物化的初始游标行（计数 0）。保留这行、删行后直接恢复、删行后整库重建两次再恢复，三者均只发出一次新请求；最终 intent 为 `[1,2,3]`，返回与游标均为 3，未结算为 2。之后再次删投影重建，持久化摘要仍相等。

## 首轮事项与边界

- **fixed / 已修**：已提交模型终态且派生行丢失时直接恢复零次生成；已提交终态后暂停、切模再恢复为 `noop` 且新模型调用为零。
- **fixed / 已修**：待批提案的 `request_id`、`approval_id`、`claim_epoch` 和完整游标摘要；审批后的真实写结果晚于游标时只吸收一次，写入意图及凭据各一份。
- **fixed / 已修**：原 SSE 的未知初始状态、非空密文冲突及 `arguments.done.name` 冲突均拒绝；附加 `done.call_id` 正负控通过。
- **fixed / 已修**：C3 拒绝布尔/数值矛盾，包括嵌套对象数组；类型和值一致的对照通过。首次提交前崩溃计数现为 **fixed / 已修**，证据见上。
- **未验证**：全量 pytest、hosted required CI、付费官方或网关 smoke、真实钥匙串、Windows 真机与物理断电。未检查远端 PR 状态，未读取作者的工作树、自查或账本，不推断预发布错误数据的迁移能力。等待子任务的 Run 已清空终态 `model_turn`；重新驱动会产生下一次模型步骤（受控传输实测 1 次），与已提交最终结果后暂停再恢复的 0 次请求是不同状态。

## 版本与复现

本轮逐行阅读了增量中仅有的两个文件：`src/tianshu/_responses_rebuild.py` 和 `tests/test_responses_review_followup.py`。源码的两个行为变化是保存初始基准、识别合法挂起游标并保留原摘要校验。本轮计数来自独立目录内的探针与下方扩展，未将作者测试文件的结果计入。两提交之间完整 binary diff 的 SHA-256 为 `66f1e648b98079cc3d2d6a22f93c985a72268526db55c1b6312611bb85e6c9ae`，可用 `git -c core.quotePath=false diff --no-ext-diff --no-textconv --binary 6e9bf34..b7c3773 | shasum -a 256` 复核。

以独立工作树根为工作目录。原始输出分别在 `final-frozen.log` 和 `final-boundaries.log`。在证据目录分别以 `shasum -a 256 -c` 校验 `SHA256SUMS`、`FOLLOWUP-SHA256SUMS` 和 `FINAL-SHA256SUMS`。

```sh
E=.omc/artifacts/independent-responses-review
export UV_OFFLINE=1 TIANSHU_CREDENTIAL_BACKEND=env TIANSHU_API_KEY=review-only
PYTHONPATH=tests uv run --frozen python -m pytest -p conftest -o addopts= -v -s --tb=short \
  "$E/test_independent.py" "$E/test_followup.py" "$E/test_followup_impact.py"
review_temp=$(mktemp -d -t responses-freeze)
awk '/^```python$/{emit=1; next} /^```$/{emit=0} emit' "$E/FINAL-REPORT.md" > "$review_temp/test_final_boundaries.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="tests:$E" uv run --frozen python -m pytest \
  -p conftest -p no:cacheprovider -c pyproject.toml -o addopts= -v -s --tb=short "$review_temp/test_final_boundaries.py"
```

## 独立扩展探针

为便于只归档报告、日志及校验清单，下面保留可逐字执行的探针。子进程使用真实故障点，本地模型仅返回固定数据；负控在读出的 `Event` 副本上执行，不编辑数据库事件。

```python
import os
import subprocess
import sys
from contextlib import closing
from dataclasses import replace

import pytest
from support import approve_pending, engine_for, stack_for
from test_independent import LocalTransport, adapter, answer, call, msg
from test_followup import child_process, digest, drop_projections, select
from tianshu._responses_rebuild import rebuild_responses_progress
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.replay import replay
from tianshu.worker import Worker


def identities(log, rid):
    return [(e.seq, e.payload_hash) for e in log.read_run(rid)]


def assert_cursor(stack, log, rid):
    saved = select(log.read_run(rid), "run.cursor")[-1]
    cursor, meta = stack.tasks.load_cursor(rid)
    assert digest(cursor) == saved.payload["payload_hash"]
    assert meta["claim_epoch"] == saved.payload["claim_epoch"]
    return cursor.to_dict()


@pytest.mark.parametrize("route", ["retained", "dropped", "full-rebuild-twice"])
def test_before_first_checkpoint_two_crashes(tmp_path, route):
    child_process(tmp_path, "no_checkpoint")
    program = """
import sys
from pathlib import Path
from support import engine_for, stack_for
from test_independent import adapter, LocalTransport, answer, msg
from tianshu.events import EventLog
root = Path(sys.argv[1])
log = EventLog(root / 'db', blob_dir=root / 'blobs')
stack = stack_for(log, data_root=root)
(rid,) = log.conn.execute('SELECT run_id FROM runs').fetchone()
assert stack.tasks.load_cursor(rid) is None
engine_for(stack, adapter(LocalTransport(answer([msg()])))).resume_run(rid)
"""
    process = subprocess.run([sys.executable, "-c", program, str(tmp_path)],
                             env={**os.environ, "TIANSHU_FAILPOINT": "model-result-before-commit"},
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 77 and "[failpoint] model-result-before-commit" in process.stderr
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        (rid,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        facts = identities(log, rid)
        events = log.read_run(rid)
        assert not select(events, "run.cursor") and not select(events, "model.result")
        assert [e.payload["attempt"] for e in select(events, "model.intent")] == [1, 2]
        original, meta = stack.tasks.load_cursor(rid)
        assert original.budget_ledger["model_attempts"] == 0 and meta["claim_epoch"] == 0
        if route != "retained":
            drop_projections(log, rid)
            assert stack.tasks.load_cursor(rid) is None
        if route == "full-rebuild-twice":
            for _ in range(2):
                stack.tasks.rebuild_from_events()
                restored, restoration = stack.tasks.load_cursor(rid)
                assert restored.to_dict() == original.to_dict() and restoration["claim_epoch"] == 0
        assert identities(log, rid) == facts
        tr = LocalTransport(answer([msg()]))
        resumed = engine_for(stack, adapter(tr)).resume_run(rid)
        assert resumed.outcome == "noop" and len(tr.bodies) == 1
        events = log.read_run(rid)
        assert [e.payload["attempt"] for e in select(events, "model.intent")] == [1, 2, 3]
        assert len(select(events, "model.result")) == 1
        assert resumed.model_attempts_known and resumed.model_attempts == 3
        assert select(events, "run.end")[-1].payload["unbilled_model_intents"] == 2
        final = assert_cursor(stack, log, rid)
        assert final["budget_ledger"]["model_attempts"] == 3
        facts = identities(log, rid)
        drop_projections(log, rid)
        stack.tasks.rebuild_from_events()
        assert assert_cursor(stack, log, rid) == final and identities(log, rid) == facts
        print(route, "before first run.cursor: retained intents [1,2], cursor attempts=0;"
              " recovered intents [1,2,3], reported=saved=3, unbilled=2")


def test_repeated_park_and_worker_wakeup(tmp_path):
    assert os.environ["TIANSHU_AUTHORITY_ROOT"] == str(tmp_path / "authority")
    assert os.environ["TIANSHU_TEST_LOCK_ROOT"] and os.environ["TIANSHU_CREDENTIAL_BACKEND"] == "env"
    with closing(EventLog(tmp_path / "db", blob_dir=tmp_path / "blobs")) as log:
        st = stack_for(log, data_root=tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        t = st.tasks.create_task("local parent", [str(ws)], max_subtasks=1)
        item = call('{"subtask_id":"review-child","goal":"inspect local fixture"}', "subtask.spawn")
        tr = LocalTransport(answer([item]), answer([msg()]))
        eng = engine_for(st, adapter(tr))
        wait = eng.start(t.task_id, out_name="")
        rid = wait.run_id
        assert wait.state == "waiting_approval" and approve_pending(st, rid)
        assert eng.resume_run(rid).state == "running" and st.claims.job_for_run(rid).state == "waiting"
        events = log.read_run(rid)
        checkpoint = select(events, "run.cursor")[-1]
        park = select(events, "job.parked")[-1]
        earlier = select(events, "run.cursor")[-2]
        assert park.seq == checkpoint.seq + 1 and checkpoint.payload["claim_epoch"] == park.payload["claim_epoch"]
        assert assert_cursor(st, log, rid)["model_turn"] is None
        assert rebuild_responses_progress(events)[0].model_turn is None
        # One changed fact per negative control; the source event chain is never modified.
        cases = {
            "previous-hash": (earlier, {"payload": {**earlier.payload, "payload_hash": "0" * 64}}),
            "park-hash": (checkpoint, {"payload": {**checkpoint.payload, "payload_hash": "0" * 64}}),
            "gap": (park, {"seq": park.seq + 1}),
            "other-run": (park, {"run_id": "other-physical-run"}),
            "other-claim": (park, {"payload": {**park.payload, "claim_epoch": park.payload["claim_epoch"] + 1}}),
            "other-reason": (park, {"payload": {**park.payload, "reason": "paused"}}),
        }
        for label, (target, change) in cases.items():
            altered = [replace(e, **change) if e.seq == target.seq else e for e in events]
            with pytest.raises(TianshuError, match="游标不能从持久化事件精确重建"):
                rebuild_responses_progress(altered)
        with pytest.raises(TianshuError, match="游标不能从持久化事件精确重建"):
            rebuild_responses_progress([e for e in events if e.seq != park.seq])
        print("invalid witnesses rejected:", *cases, "missing-park")
        before = identities(log, rid)
        first = assert_cursor(st, log, rid)
        drop_projections(log, rid)
        st.tasks.rebuild_from_events()
        assert assert_cursor(st, log, rid) == first and identities(log, rid) == before
        again = LocalTransport(answer([msg("still waiting")]))
        assert engine_for(st, adapter(again)).resume_run(rid).state == "running"
        assert len(again.bodies) == 1 and st.claims.job_for_run(rid).state == "waiting"
        events = log.read_run(rid)
        parks = select(events, "job.parked")
        assert len(parks) == 2 and parks[0].payload["claim_epoch"] != parks[1].payload["claim_epoch"]
        twice = assert_cursor(st, log, rid)
        st.tasks.rebuild_from_events()
        assert assert_cursor(st, log, rid) == twice
        sub, = st.tasks.list_subtasks(rid)
        child_tr = LocalTransport(answer([msg("child finished")]))
        worker = Worker(st, model=adapter(child_tr), heartbeat_interval_ms=None)
        try:
            assert len(worker.drain(max_jobs=1)) == 1
        finally:
            worker.close()
        assert len(child_tr.bodies) == 1 and st.tasks.count_unfinished_subtasks(rid) == 0
        assert st.claims.job_for_run(rid).state == "pending"
        end_tr = LocalTransport(answer([msg("parent finished")]))
        end = engine_for(st, adapter(end_tr)).resume_run(rid)
        assert end.state == "succeeded" and len(end_tr.bodies) == 1
        child_id = sub.child_run_id
        originals = {r: assert_cursor(st, log, r) for r in (rid, child_id)}
        facts = {r: identities(log, r) for r in originals}
        for r in originals:
            drop_projections(log, r)
        for _ in range(2):
            st.tasks.rebuild_from_events()
            for r in originals:
                assert assert_cursor(st, log, r) == originals[r] and identities(log, r) == facts[r]
                assert replay(log, r).ok
        print("two parent parks, two claim epochs, child wakes parent; both Runs closed;"
              " double rebuild matches saved digests and replay for both")
```
