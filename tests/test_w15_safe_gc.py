"""W15 v2 保守离线安全 GC 完整验收与负控测试（CONTRACT-W15-RETENTION §6）。

覆盖判据：
1. 默认只读预演（无 `--approve` 或显式 `--dry-run`）退出 `3`，候选行数/字节数精确，
   `data_root` 与 `events.db` 全文件字节、哈希与 `st_mtime_ns` 100% 零改动（不产生 `-wal`/`-shm`）。
2. `--approve` 在离线独占锁下精准回收两处孤儿 blob（`artifacts/blobs/*` 与 `blobs/*`）、
   超期空终态快照、超期终态 `jobs` 行与超期 `settled & known=1` `budget_reservations` 行，
   执行 `PRAGMA wal_checkpoint(TRUNCATE)` 截断 `-wal` 并退出 `0`；随后 `tianshu replay`、
   `create_backup` 与 `verify_backup` 全部 100% 通过，二次执行幂等。
3. 绝对禁删不变量与负控：
   - 非空 `artifacts/snapshots/<run_id>.json`（`changes != []`）、已完成撤销日志
     `artifacts/undo/<run_id>.json` 及全部六类 `protected_blobs`（Receipt 写前副本、
     事件外置 payload、快照写前副本、撤销日志引用、内容对象、未擦除记忆正文）即使远超
     保留期也 **0 删除**；
   - `events`、`event_versions`、`summary_versions`、`summary_materials`、`summary_sources`、
     `dispatches`、`dispatch_requests`、`approvals`、`grants`、`runs`、`tasks`、`memories`、
     `content_objects`、`budget_accounts`、`budget_limits`、`budget_counters` 行数与逻辑摘要
     GC 前后逐字节相同；
   - `budget_reservations` 中 `state in ('pending', 'unknown', 'partial', 'released')`、
     `known = 0` 或 `run_id = ''` 的行即使超期也 **0 删除**；`BudgetLedger.usage()`、
     `reconcile()` 与 `pending()` 在 GC 前后输出完全一致；
   - 非终态 `Run`（`running` / `waiting_approval` / `reconciling` / `paused`）及仍含活跃
     `dispatches`（`intent` / `executing`）的 `Run` 完整保护其名下全部 `jobs`、`budget_reservations`
     与空快照清单；
   - 损坏快照清单、`blobs` 内符号链接、多硬链接 blob、引用材料哈希不符、或存在
     `in_progress` 撤销日志时，`tianshu data gc --approve` 以退出码 `2` 闭锁拒绝，
     **0 文件删除、0 数据库行删除**；且锁内二次运行 `collect_references` 消除 TOCTOU。
4. 真子进程锁互斥（标准管道屏障，零 `sleep`）：活跃子进程持有 `EventLog`（或 `daemon`）时，
   `tianshu data gc --approve` 以退出码 `2`（`maintenance_busy`）拒绝、0 删除，
   子进程继续正常追加事件。
5. 低磁盘空间回收证明：当可用空间 `< 2 GiB` 或模拟 `0` 字节（新副作用 Run 以
   `dispatch.disk_space_low` 拒绝）时，`tianshu data gc --approve` 仍成功退出 `0` 并回收孤儿 blob。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
from support import CHILD_TEXT, approve_pending, engine_for, stack_for, write_exact

from tianshu import cli, memory_control
from tianshu.backup import collect_references, create_backup, verify_backup
from tianshu.budget import BudgetLedger
from tianshu.credentials import register_secret
from tianshu.data_gc import DAY_MS, run_data_gc
from tianshu.disk_admission import DiskAdmission, DiskAdmissionError
from tianshu.events import EventLog
from tianshu.jobs import JOB_CANCELLED, JOB_DONE
from tianshu.loop import Limits, build_stack, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.replay import replay
from tianshu.resources import MemoryScope
from tianshu.snapshot import SnapshotStore
from tianshu.summaries import MaterialBlock, SummaryStore, build_material
from tianshu.undo_journal import ACTION_RESTORE, JournalItem, JournalStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
STARTUP = str(Path(__file__).resolve().parent / "_startup")


def _subproc_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
    }
    for passthrough in ("TIANSHU_TEST_LOCK_ROOT", "TIANSHU_AUTHORITY_ROOT"):
        if passthrough in os.environ:
            env[passthrough] = os.environ[passthrough]
    parts = [STARTUP, SRC]
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    if extra:
        env.update(extra)
    return env


def _cli(
    *args: object, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tianshu", *(str(a) for a in args)],
        env=_subproc_env(extra_env),
        capture_output=True,
        timeout=30,
        **CHILD_TEXT,
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    """记录相对路径 -> (sha256, size, st_mtime_ns)，含伴随文件以抓任何隐式写操作。"""
    state: dict[str, tuple[str, int, int]] = {}
    if not root.exists():
        return state
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[rel] = ("SYMLINK:" + os.readlink(path), 0, path.lstat().st_mtime_ns)
        elif path.is_file():
            raw = path.read_bytes()
            st = path.stat()
            state[rel] = (hashlib.sha256(raw).hexdigest(), len(raw), st.st_mtime_ns)
    return state


def _table_digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    quoted = ", ".join(f'"{c}"' for c in cols)
    rows = conn.execute(
        f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}'
    ).fetchall()
    canonical = json.dumps([list(r) for r in rows], ensure_ascii=False, sort_keys=True)
    return len(rows), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _protected_tables_snapshot(db_path: Path) -> dict[str, tuple[int, str]]:
    protected_names = (
        "events",
        "event_versions",
        "summary_materials",
        "summary_sources",
        "summary_versions",
        "dispatches",
        "dispatch_requests",
        "dispatch_meta",
        "approvals",
        "grants",
        "runs",
        "tasks",
        "subtasks",
        "memories",
        "memory_meta",
        "memory_vectors",
        "content_objects",
        "content_grants",
        "budget_accounts",
        "budget_limits",
        "budget_counters",
    )
    out: dict[str, tuple[int, str]] = {}
    has_wal = (db_path.with_name(db_path.name + "-wal")).exists()
    uri = db_path.resolve().as_uri() + ("?mode=ro" if has_wal else "?mode=ro&immutable=1")
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        present = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for name in protected_names:
            if name in present:
                out[name] = _table_digest(conn, name)
    return out


def _build_rich_fixture(tmp_path: Path) -> dict[str, Any]:
    """构造含真实写入 Run、Receipt、大 payload 外置、摘要、记忆、内容对象、已完成撤销日志、
    孤儿 blob、超期/未超期终态与非终态作业及全部状态预算预占的完整离线数据根。
    """
    now_ms = 1_800_000_000_000  # 固定逻辑当前时刻
    old_ms = now_ms - 120 * DAY_MS  # 120 天前（> 默认 90 天保留期）
    recent_ms = now_ms - 10 * DAY_MS  # 10 天前（< 90 天保留期）

    root = (tmp_path / "data_root").resolve()
    ws = (tmp_path / "workspace").resolve()
    ws.mkdir(parents=True)
    write_exact(ws / "note.md", "原始写前文本\n第二行\n")

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    snapshots = SnapshotStore(root / "artifacts")
    try:
        # 1. 跑一次带真实覆盖写 + 人工审批 + Receipt + 非空快照清单的完整终态 Run
        limits = Limits(6, 20_000)
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "call_mod_1",
                            "modify_text",
                            {"path": "note.md", "content": "修改后正文\n"},
                        )
                    ],
                ),
                ModelResponse("end_turn", content="已完成修改"),
            ]
        )
        res = run(
            task="修改笔记",
            workspace_dir=ws,
            out_name="out.md",
            model=model,
            log=log,
            limits=limits,
            snapshots=snapshots,
        )
        while res.state == "waiting_approval":
            st = stack_for(log, snapshots=snapshots, data_root=root)
            assert approve_pending(st, res.run_id)
            res = engine_for(st, model, limits=limits).resume_run(res.run_id)
        assert res.state == "succeeded"
        effect_run_id = res.run_id

        # 追加一条超过内联阈值的大工具结果以生成受保护的根 blobs/<sha256> payload_ref
        log.append(
            effect_run_id,
            "tool.result",
            {
                "call_id": "call_spill_payload",
                "tool_id": "read_text",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {"content": "S" * 300_000},
            },
            actor_kind="core",
        )

        stack = build_stack(log, data_root=root)
        stack.tasks._disk_admission = DiskAdmission(
            stack.tasks.disk_admission.context,
            probe=lambda _p: 1 << 62,
        )

        # 2. 写入未擦除记忆（memory_blobs/<sha256>）与内容对象（artifacts/blobs/<sha256>）
        mem_res = memory_control.remember(
            stack,
            MemoryScope.user(),
            kind="preference",
            content="报告顶部必须给出结论摘要。",
        )
        assert mem_res.ok
        mem_entry = stack.memory.get(
            mem_res.output["memory_id"], allowed=[MemoryScope.user()]
        )
        assert mem_entry is not None
        memory_blob_rel = f"memory_blobs/{mem_entry.content_ref}"

        content_raw = b"verified fetched content bytes for offline read_content"
        content_sha = hashlib.sha256(content_raw).hexdigest()
        content_path = stack.contents.blob_dir / content_sha
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_bytes(content_raw)
        stack.contents.record(content_sha, content_raw, task_id="task_content_holder")
        content_blob_rel = f"artifacts/blobs/{content_sha}"

        # 3. 写入持久化语义摘要（summary_materials / summary_sources / summary_versions）
        summaries = SummaryStore(log, clock=lambda: old_ms)
        material = build_material(
            [MaterialBlock(seq=1, role="user", content="保留关键安全约束")],
            scope="run",
            task_id="task_summary",
            run_id=effect_run_id,
        )
        pending_sum = summaries.begin(
            material,
            model="gpt-4o-mini",
            attempt_id="sum_attempt_1",
            run_id=effect_run_id,
            task_id="task_summary",
        )
        ready_sum = summaries.complete(
            pending_sum,
            "关键约束：所有写前证据必须保留。",
            input_tokens=32,
            output_tokens=16,
            micros=120,
        )

        # 4. 写入已完成撤销日志（completed undo journal）及其独立引用的写前 blob
        undo_blob_raw = b"undo journal referenced preimage blob"
        undo_blob_sha = hashlib.sha256(undo_blob_raw).hexdigest()
        (root / "artifacts" / "blobs" / undo_blob_sha).write_bytes(undo_blob_raw)
        jstore = JournalStore(root / "artifacts")
        j_obj = jstore.begin(
            "run_undo_done_old",
            control_run="run_ctrl_done",
            items=(
                JournalItem(
                    root=str(ws),
                    path="restored.txt",
                    call_id="call_u1",
                    before_state=undo_blob_sha,
                    expected_after="e" * 64,
                    action=ACTION_RESTORE,
                ),
            ),
        )
        j_obj = jstore.record_done(j_obj, 0, undo_blob_sha)
        jstore.complete(j_obj)
        undo_blob_rel = f"artifacts/blobs/{undo_blob_sha}"
        undo_journal_rel = "artifacts/undo/run_undo_done_old.json"

        # 把 effect_run_id 及其 job 回拨为 120 天前超期终态：
        # 它的 job 应可被清理，但它的非空快照清单与写前 blob / payload_ref 绝不可删！
        conn = log.conn
        conn.execute(
            "UPDATE runs SET created_at = ?, ended_at = ? WHERE run_id = ?",
            (old_ms, old_ms + 1000, effect_run_id),
        )
        conn.execute(
            "UPDATE jobs SET created_at = ?, ended_at = ? WHERE run_id = ?",
            (old_ms, old_ms + 1000, effect_run_id),
        )
        effect_job_id = conn.execute(
            "SELECT job_id FROM jobs WHERE run_id = ?", (effect_run_id,)
        ).fetchone()[0]

        # 5. 构造各类受控 Run / Job / Snapshot / Dispatch 组合：
        # (a) 超期无副作用终态 Run + 空快照清单 + cancelled job -> 空快照与 job 均可清理
        conn.execute(
            "INSERT INTO runs(run_id, task_id, goal_revision, lease_epoch, state,"
            " outcome, effect_state, created_at, ended_at)"
            " VALUES ('run_empty_old', 't_aux', 1, 1, 'cancelled', 'cancelled',"
            " 'none_applied', ?, ?)",
            (old_ms, old_ms + 500),
        )
        snapshots.init_run("run_empty_old")
        conn.execute(
            "INSERT INTO jobs(job_id, run_id, state, claim_epoch, cancel_requested,"
            " outcome, created_at, ended_at)"
            " VALUES ('job_cancelled_old', 'run_empty_old', ?, 1, 1, 'cancelled', ?, ?)",
            (JOB_CANCELLED, old_ms, old_ms + 500),
        )

        # (b) 未超期终态 Run + 空快照清单 + done job -> 未到保留期，均不可删
        conn.execute(
            "INSERT INTO runs(run_id, task_id, goal_revision, lease_epoch, state,"
            " outcome, effect_state, created_at, ended_at)"
            " VALUES ('run_empty_recent', 't_aux', 1, 1, 'succeeded', 'succeeded',"
            " 'none_applied', ?, ?)",
            (recent_ms, recent_ms + 500),
        )
        snapshots.init_run("run_empty_recent")
        conn.execute(
            "INSERT INTO jobs(job_id, run_id, state, claim_epoch, cancel_requested,"
            " outcome, created_at, ended_at)"
            " VALUES ('job_done_recent', 'run_empty_recent', ?, 1, 0, 'succeeded', ?, ?)",
            (JOB_DONE, recent_ms, recent_ms + 500),
        )

        # (c) 非终态 Run（running / waiting_approval / reconciling）且时间戳超期 -> 严格保护
        for idx, non_term_state in enumerate(("running", "waiting_approval", "reconciling")):
            rid = f"run_live_{non_term_state}"
            conn.execute(
                "INSERT INTO runs(run_id, task_id, goal_revision, lease_epoch, state,"
                " created_at) VALUES (?, 't_aux', 1, 1, ?, ?)",
                (rid, non_term_state, old_ms),
            )
            snapshots.init_run(rid)
            conn.execute(
                "INSERT INTO jobs(job_id, run_id, state, claim_epoch, cancel_requested,"
                " outcome, created_at, ended_at)"
                " VALUES (?, ?, ?, 1, 0, 'cancelled', ?, ?)",
                (f"job_on_{non_term_state}", rid, JOB_CANCELLED, old_ms, old_ms + idx + 1),
            )

        # (d) 终态 Run 但仍挂着活跃 dispatch（executing） -> 其超期终态 job 与空快照必须保护
        conn.execute(
            "INSERT INTO runs(run_id, task_id, goal_revision, lease_epoch, state,"
            " outcome, effect_state, created_at, ended_at)"
            " VALUES ('run_active_dispatch', 't_aux', 1, 1, 'failed', 'failed',"
            " 'unknown', ?, ?)",
            (old_ms, old_ms + 200),
        )
        snapshots.init_run("run_active_dispatch")
        conn.execute(
            "INSERT INTO jobs(job_id, run_id, state, claim_epoch, cancel_requested,"
            " outcome, created_at, ended_at)"
            " VALUES ('job_blocked_by_dispatch', 'run_active_dispatch', ?, 1, 0,"
            " 'failed', ?, ?)",
            (JOB_DONE, old_ms, old_ms + 200),
        )
        conn.execute(
            "INSERT INTO dispatches(dispatch_id, run_id, task_id, call_id, request_id,"
            " request_digest, tool_id, action, resources, target, rel_path,"
            " workspace_root, resource_key, claim_epoch, owner_instance_id,"
            " attempt_id, intent_seq, boundary_protocol, state, created_at, updated_at)"
            " VALUES ('disp_exec_1', 'run_active_dispatch', 't_aux', 'c_exec_1', 'req_1',"
            " ?, 'network.fetch', 'fetch', '[]', 'https://example.com', '', '', 'net',"
            " 1, 'inst', 1, 1, 1, 'executing', ?, ?)",
            ("9" * 64, old_ms, old_ms + 100),
        )

        # 6. 构造共享预算账本（父子链 + 全状态预占行）：
        clock_val = [old_ms]
        ledger = BudgetLedger(log, clock=lambda: clock_val[0])
        # AgentLoop 真实运行已写入 2 条未知价格（partial/known=0）预占：将它们也回拨到 120 天前，
        # 验证真实引擎产生的超期未决/未知金额预占同样严格保留！
        prior_resv_ids = {
            str(r[0])
            for r in conn.execute("SELECT reservation_id FROM budget_reservations").fetchall()
        }
        conn.execute(
            "UPDATE budget_reservations SET created_at = ?, settled_at = ?",
            (old_ms, old_ms + 50),
        )
        ledger.create_account("acc_root", limits={"tokens": 100_000, "requests": 100})
        ledger.create_account(
            "acc_child",
            parent_id="acc_root",
            run_id=effect_run_id,
            limits={"tokens": 50_000, "requests": 50},
        )
        # (6.1) 超期 settled & known=1 & 终态 Run -> 唯一允许被 GC 清理的预占行
        r_old_settled = ledger.reserve(
            "acc_child",
            attempt_id="att_old_settled",
            run_id=effect_run_id,
            call_id="c_b1",
            need={"tokens": 500, "requests": 1},
        )
        ledger.settle(
            r_old_settled.reservation_id,
            usage={"tokens": 420, "requests": 1},
            known=True,
        )

        # (6.2) 超期但 state='unknown'（known=False） -> 禁删
        r_old_unknown = ledger.reserve(
            "acc_child",
            attempt_id="att_old_unknown",
            run_id=effect_run_id,
            call_id="c_b2",
            need={"tokens": 300, "requests": 1},
        )
        ledger.settle(r_old_unknown.reservation_id, usage={}, known=False)

        # (6.3) 超期但 state='partial'（部分维度未知，known=False） -> 禁删
        r_old_partial = ledger.reserve(
            "acc_child",
            attempt_id="att_old_partial",
            run_id=effect_run_id,
            call_id="c_b3",
            need={"tokens": 200, "micros": 1000, "requests": 1},
        )
        ledger.settle(
            r_old_partial.reservation_id,
            usage={"tokens": 180, "requests": 1},
            known=False,
            known_dimensions={"tokens", "requests"},
        )

        # (6.4) 超期但 state='pending' -> 禁删
        r_old_pending = ledger.reserve(
            "acc_child",
            attempt_id="att_old_pending",
            run_id=effect_run_id,
            call_id="c_b4",
            need={"tokens": 150, "requests": 1},
        )

        # (6.5) 人工注入异常边界：state='settled' 但 known=0 -> 禁删
        r_settled_unknown_flag = ledger.reserve(
            "acc_child",
            attempt_id="att_settled_known0",
            run_id=effect_run_id,
            call_id="c_b5",
            need={"tokens": 100, "requests": 1},
        )
        ledger.settle(
            r_settled_unknown_flag.reservation_id,
            usage={"tokens": 90, "requests": 1},
            known=True,
        )
        conn.execute(
            "UPDATE budget_reservations SET known = 0 WHERE reservation_id = ?",
            (r_settled_unknown_flag.reservation_id,),
        )

        # (6.6) 超期 settled & known=1 但 run_id=''（账户级无归属 Run） -> 禁删
        r_empty_run = ledger.reserve(
            "acc_root",
            attempt_id="att_empty_run",
            run_id="",
            call_id="c_b6",
            need={"tokens": 80, "requests": 1},
        )
        ledger.settle(
            r_empty_run.reservation_id,
            usage={"tokens": 80, "requests": 1},
            known=True,
        )

        # (6.7) 超期 settled & known=1 但归属非终态 Run（run_live_running） -> 禁删
        r_live_run = ledger.reserve(
            "acc_root",
            attempt_id="att_live_run",
            run_id="run_live_running",
            call_id="c_b7",
            need={"tokens": 90, "requests": 1},
        )
        ledger.settle(
            r_live_run.reservation_id,
            usage={"tokens": 90, "requests": 1},
            known=True,
        )

        # (6.8) 未超期 settled & known=1（10 天前） -> 禁删
        clock_val[0] = recent_ms
        r_recent_settled = ledger.reserve(
            "acc_child",
            attempt_id="att_recent_settled",
            run_id="run_empty_recent",
            call_id="c_b8",
            need={"tokens": 110, "requests": 1},
        )
        ledger.settle(
            r_recent_settled.reservation_id,
            usage={"tokens": 100, "requests": 1},
            known=True,
        )
        conn.commit()

        usage_root_before = ledger.usage("acc_root")
        usage_child_before = ledger.usage("acc_child")
        reconcile_before = ledger.reconcile()
        pending_before = [r.reservation_id for r in ledger.pending()]

        # 7. 写入两个孤儿 blob（分别位于 artifacts/blobs 与根 blobs）
        orphan_artifact_bytes = b"orphan artifact blob payload 12345"
        orphan_root_bytes = b"orphan root blob payload 67890"
        orphan_artifact_rel = f"artifacts/blobs/{'1' * 64}"
        orphan_root_rel = f"blobs/{'2' * 64}"
        (root / orphan_artifact_rel).write_bytes(orphan_artifact_bytes)
        (root / orphan_root_rel).write_bytes(orphan_root_bytes)

        # 固定 remember 控制 Run 与其 job 的逻辑时间为 recent_ms，避免受未来年份墙钟影响
        mem_ctrl_row = conn.execute(
            "SELECT j.job_id, j.run_id FROM jobs j WHERE j.run_id != ?",
            (effect_run_id,),
        ).fetchone()
        assert mem_ctrl_row is not None
        mem_ctrl_job_id, mem_ctrl_run_id = str(mem_ctrl_row[0]), str(mem_ctrl_row[1])
        conn.execute(
            "UPDATE runs SET created_at = ?, ended_at = ? WHERE run_id = ?",
            (recent_ms, recent_ms + 100, mem_ctrl_run_id),
        )
        conn.execute(
            "UPDATE jobs SET created_at = ?, ended_at = ? WHERE job_id = ?",
            (recent_ms, recent_ms + 100, mem_ctrl_job_id),
        )
        conn.commit()

        # 记录所有受保护材料路径
        spill_row = conn.execute(
            "SELECT payload_hash FROM events WHERE payload_ref IS NOT NULL"
        ).fetchone()
        spill_blob_rel = f"blobs/{spill_row[0]}"
        preimage_sha = hashlib.sha256("原始写前文本\n第二行\n".encode()).hexdigest()
        preimage_blob_rel = f"artifacts/blobs/{preimage_sha}"
        non_empty_snap_rel = f"artifacts/snapshots/{effect_run_id}.json"
    finally:
        log.close()

    return {
        "root": root,
        "ws": ws,
        "now_ms": now_ms,
        "old_ms": old_ms,
        "recent_ms": recent_ms,
        "effect_run_id": effect_run_id,
        "effect_job_id": effect_job_id,
        "summary_id": ready_sum.summary_id,
        "orphan_artifact_rel": orphan_artifact_rel,
        "orphan_root_rel": orphan_root_rel,
        "orphan_bytes_total": len(orphan_artifact_bytes) + len(orphan_root_bytes),
        "deletable_empty_snap_rel": "artifacts/snapshots/run_empty_old.json",
        "deletable_job_ids": {effect_job_id, "job_cancelled_old"},
        "kept_job_ids": {
            mem_ctrl_job_id,
            "job_done_recent",
            "job_on_running",
            "job_on_waiting_approval",
            "job_on_reconciling",
            "job_blocked_by_dispatch",
        },
        "deletable_resv_id": r_old_settled.reservation_id,
        "kept_resv_ids": prior_resv_ids | {
            r_old_unknown.reservation_id,
            r_old_partial.reservation_id,
            r_old_pending.reservation_id,
            r_settled_unknown_flag.reservation_id,
            r_empty_run.reservation_id,
            r_live_run.reservation_id,
            r_recent_settled.reservation_id,
        },
        "protected_files": {
            spill_blob_rel,
            preimage_blob_rel,
            content_blob_rel,
            memory_blob_rel,
            undo_blob_rel,
            undo_journal_rel,
            non_empty_snap_rel,
            "artifacts/snapshots/run_empty_recent.json",
            "artifacts/snapshots/run_live_running.json",
            "artifacts/snapshots/run_live_waiting_approval.json",
            "artifacts/snapshots/run_live_reconciling.json",
            "artifacts/snapshots/run_active_dispatch.json",
        },
        "usage_root_before": usage_root_before,
        "usage_child_before": usage_child_before,
        "reconcile_before": reconcile_before,
        "pending_before": pending_before,
    }


def test_preview_modes_exit_3_with_exact_candidates_and_zero_byte_mutation(tmp_path: Path) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    snap_size = (root / fx["deletable_empty_snap_rel"]).stat().st_size

    before_tree = _tree_snapshot(root)
    before_db_sha = hashlib.sha256((root / "events.db").read_bytes()).hexdigest()

    # 1. 库入口（注入固定逻辑时钟）默认预演与显式 --dry-run
    for explicit_dry in (False, True):
        report, code = run_data_gc(
            root,
            older_than_days=90,
            dry_run=explicit_dry,
            approve=False,
            clock=lambda: fx["now_ms"],
        )
        assert code == 3
        assert report["ok"] is True
        assert report["dry_run"] is True
        assert report["approved"] is False
        assert report["data_root"] == "<PATH_REDACTED>"
        cand = report["candidates"]
        assert {c["path"] for c in cand["orphan_blobs"]} == {
            fx["orphan_artifact_rel"],
            fx["orphan_root_rel"],
        }
        assert cand["orphan_blobs_count"] == 2
        assert cand["orphan_blobs_bytes"] == fx["orphan_bytes_total"]
        assert [c["path"] for c in cand["empty_snapshots"]] == [fx["deletable_empty_snap_rel"]]
        assert cand["empty_snapshots_count"] == 1
        assert cand["empty_snapshots_bytes"] == snap_size
        assert cand["jobs_count"] == 2
        assert cand["budget_reservations_count"] == 1
        assert cand["reclaimable_file_bytes"] == fx["orphan_bytes_total"] + snap_size
        assert report["deleted"]["orphan_blobs_count"] == 0
        assert report["deleted"]["jobs_count"] == 0
        assert report["deleted"]["budget_reservations_count"] == 0

    assert _tree_snapshot(root) == before_tree
    assert hashlib.sha256((root / "events.db").read_bytes()).hexdigest() == before_db_sha

    # 2. 真实 CLI 子进程预演（省略 --approve 与显式 --dry-run，JSON 与人话输出，且路径脱敏）
    proc_json = _cli("data", "gc", "--data-root", root, "--json")
    assert proc_json.returncode == 3, proc_json.stderr
    cli_report = json.loads(proc_json.stdout)
    assert cli_report["dry_run"] is True
    assert cli_report["candidates"]["orphan_blobs_count"] == 2
    assert str(root) not in proc_json.stdout

    proc_text = _cli("data", "gc", "--data-root", root, "--dry-run")
    assert proc_text.returncode == 3, proc_text.stderr
    assert "离线安全 GC 预演" in proc_text.stdout
    assert "--approve" in proc_text.stdout
    assert str(root) not in proc_text.stdout

    # 预演全过程未创建 -wal/-shm，所有文件字节与 mtime_ns 完全未动
    assert _tree_snapshot(root) == before_tree


def test_approve_reclaims_only_safe_targets_truncates_wal_and_preserves_replay_and_backup(
    tmp_path: Path,
) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    db_path = root / "events.db"

    # 用真实子进程在 wal_autocheckpoint=0 下追加事件并 os._exit(0)，留下真实未截断的非空 -wal
    leave_wal_code = """
import os, sys
from pathlib import Path
from tianshu.events import EventLog

root = Path(sys.argv[1])
log = EventLog(root / "events.db", blob_dir=root / "blobs")
log.conn.execute("PRAGMA wal_autocheckpoint=0")
log.append(
    "run_live_running",
    "tool.result",
    {
        "call_id": "call_wal_pad",
        "tool_id": "read_text",
        "status": "succeeded",
        "effect_state": "none_applied",
        "output": {"content": "W" * 32_768},
    },
    actor_kind="core",
)
os._exit(0)
"""
    wal_maker = subprocess.run(
        [sys.executable, "-c", leave_wal_code, str(root)],
        env=_subproc_env(),
        capture_output=True,
        timeout=15,
        **CHILD_TEXT,
    )
    assert wal_maker.returncode == 0, wal_maker.stderr
    wal_path = root / "events.db-wal"
    assert wal_path.exists() and wal_path.stat().st_size > 32_768

    protected_before = _protected_tables_snapshot(db_path)

    report, code = run_data_gc(
        root,
        older_than_days=90,
        approve=True,
        clock=lambda: fx["now_ms"],
    )
    assert code == 0, report
    assert report["ok"] is True
    assert report["dry_run"] is False
    assert report["approved"] is True
    assert report["deleted"]["orphan_blobs_count"] == 2
    assert report["deleted"]["orphan_blobs_bytes"] == fx["orphan_bytes_total"]
    assert report["deleted"]["empty_snapshots_count"] == 1
    assert report["deleted"]["jobs_count"] == 2
    assert report["deleted"]["budget_reservations_count"] == 1
    assert report["wal_checkpoint"] is not None
    assert report["wal_checkpoint"]["busy"] == 0
    if wal_path.exists():
        assert wal_path.stat().st_size == 0

    # 1. 孤儿文件与超期空终态快照已物理删除
    assert not (root / fx["orphan_artifact_rel"]).exists()
    assert not (root / fx["orphan_root_rel"]).exists()
    assert not (root / fx["deletable_empty_snap_rel"]).exists()

    # 2. 全部受保护文件（六类 protected_blobs + 非空快照 + 受保护空快照 + undo 日志）完好无损
    for rel in fx["protected_files"]:
        assert (root / rel).is_file(), f"受保护材料被误删：{rel}"

    # 3. 全部禁删表（events、event_versions、summary_*、dispatches、budget_counters 等）未变
    protected_after = _protected_tables_snapshot(db_path)
    assert protected_after == protected_before

    # 4. jobs 与 budget_reservations 精确只删目标集，其余全部保留
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as ro:
        remaining_jobs = {r[0] for r in ro.execute("SELECT job_id FROM jobs").fetchall()}
        assert remaining_jobs == fx["kept_job_ids"]
        assert not (remaining_jobs & fx["deletable_job_ids"])

        remaining_resv = {
            r[0]
            for r in ro.execute(
                "SELECT reservation_id FROM budget_reservations"
            ).fetchall()
        }
        assert remaining_resv == fx["kept_resv_ids"]
        assert fx["deletable_resv_id"] not in remaining_resv

    # 5. BudgetLedger.usage() / reconcile() / pending() 在 GC 前后 100% 一致
    with closing(EventLog(db_path, blob_dir=root / "blobs")) as log:
        ledger = BudgetLedger(log)
        assert ledger.usage("acc_root") == fx["usage_root_before"]
        assert ledger.usage("acc_child") == fx["usage_child_before"]
        assert ledger.reconcile() == fx["reconcile_before"]
        assert [r.reservation_id for r in ledger.pending()] == fx["pending_before"]
        # 语义摘要记录仍可完整读回
        s_rec = SummaryStore(log, create=False).read(fx["summary_id"])
        assert s_rec is not None and s_rec.ready

        # 6. 库级 replay 与 CLI tianshu replay 均 100% 通过
        rep = replay(log, fx["effect_run_id"])
        assert rep.ok, rep.problems

    cli_replay = _cli("replay", "--run-id", fx["effect_run_id"], "--db", db_path, "--json")
    assert cli_replay.returncode == 0, cli_replay.stderr
    assert json.loads(cli_replay.stdout)["ok"] is True

    # 7. GC 后 create_backup 与 verify_backup 100% 通过
    backup_dir = tmp_path / "post_gc_backup"
    create_backup(root, backup_dir)
    verified = verify_backup(backup_dir)
    assert verified.ok, verified.problems

    # 8. 二次执行 GC 幂等：0 候选、0 删除、退 0
    second_report, second_code = run_data_gc(
        root,
        older_than_days=90,
        approve=True,
        clock=lambda: fx["now_ms"],
    )
    assert second_code == 0
    assert second_report["deleted"] == {
        "orphan_blobs_count": 0,
        "orphan_blobs_bytes": 0,
        "empty_snapshots_count": 0,
        "empty_snapshots_bytes": 0,
        "jobs_count": 0,
        "budget_reservations_count": 0,
        "reclaimed_file_bytes": 0,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "corrupt_snapshot",
        "symlink_in_blobs",
        "symlink_in_artifact_blobs",
        "hardlinked_orphan_blob",
        "corrupt_protected_blob_sha",
        "undo_journal_in_progress",
    ],
)
def test_reference_unknown_and_in_progress_undo_fail_closed_with_zero_deletions(
    tmp_path: Path, fault: str
) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]

    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"do-not-touch-outside-target")
    outside_before = outside.read_bytes()

    if fault == "corrupt_snapshot":
        (root / "artifacts" / "snapshots" / "r_corrupt.json").write_bytes(b"{truncated")
        expected_code = "reference_unknown"
    elif fault == "symlink_in_blobs":
        link_path = root / "blobs" / ("c" * 64)
        try:
            link_path.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"当前平台不支持创建符号链接：{exc}")
        expected_code = "reference_unknown"
    elif fault == "symlink_in_artifact_blobs":
        link_path = root / "artifacts" / "blobs" / ("d" * 64)
        try:
            link_path.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"当前平台不支持创建符号链接：{exc}")
        expected_code = "reference_unknown"
    elif fault == "hardlinked_orphan_blob":
        hardlink_target = tmp_path / "external_hardlink"
        os.link(root / fx["orphan_artifact_rel"], hardlink_target)
        expected_code = "reference_unknown"
    elif fault == "corrupt_protected_blob_sha":
        spill_blob = next(
            root / r for r in fx["protected_files"] if r.startswith("blobs/")
        )
        spill_blob.write_bytes(b"tampered bytes causing sha256 mismatch")
        expected_code = "reference_unknown"
    else:
        jstore = JournalStore(root / "artifacts")
        jstore.begin(
            "run_inflight_undo",
            control_run="run_ctrl_inflight",
            items=(
                JournalItem(
                    root=str(fx["ws"]),
                    path="note.md",
                    call_id="c_inflight",
                    before_state="absent",
                    expected_after="a" * 64,
                    action="delete",
                ),
            ),
        )
        expected_code = "undo_in_progress"

    tree_before = _tree_snapshot(root)
    proc = _cli("data", "gc", "--data-root", root, "--older-than-days", "1", "--approve", "--json")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    err_report = json.loads(proc.stdout)
    assert err_report["ok"] is False
    assert err_report["code"] == expected_code
    assert err_report["deleted"]["orphan_blobs_count"] == 0
    assert err_report["deleted"]["jobs_count"] == 0
    assert err_report["deleted"]["budget_reservations_count"] == 0

    # 0 文件被删、0 数据库字节改动、外部符号链接目标完好无损
    assert (root / fx["orphan_artifact_rel"]).is_file()
    assert (root / fx["orphan_root_rel"]).is_file()
    assert outside.read_bytes() == outside_before
    assert _tree_snapshot(root) == tree_before


def test_lock_internal_recollect_prevents_preview_to_approve_toctou(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预演阶段某 blob 是孤儿；进入独占锁后它已被新引用（或出现坏清单），--approve 必须重算保护。"""
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    orphan_rel: str = fx["orphan_artifact_rel"]
    orphan_path = root / orphan_rel
    orphan_bytes = orphan_path.read_bytes()
    orphan_sha = hashlib.sha256(orphan_bytes).hexdigest()
    promoted_rel = f"artifacts/blobs/{orphan_sha}"
    orphan_path.rename(root / promoted_rel)

    preview, p_code = run_data_gc(
        root, older_than_days=90, dry_run=True, clock=lambda: fx["now_ms"]
    )
    assert p_code == 3
    assert promoted_rel in {c["path"] for c in preview["candidates"]["orphan_blobs"]}

    # 模拟预演结束之后、--approve 拿到锁之前，正常任务把该 blob 登记进了 content_objects
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        st = build_stack(log, data_root=root)
        st.contents.record(orphan_sha, orphan_bytes, task_id="t_late_bind")

    calls: list[int] = []
    real_collect = collect_references

    def tracked_collect(target_root: Path):
        calls.append(1)
        return real_collect(target_root)

    monkeypatch.setattr("tianshu.data_gc.collect_references", tracked_collect)
    approved, a_code = run_data_gc(
        root, older_than_days=90, approve=True, clock=lambda: fx["now_ms"]
    )
    assert a_code == 0
    assert len(calls) == 1
    # 锁内重算后 promoted_rel 已进入 protected_blobs，绝不能按旧预演清单删除！
    assert (root / promoted_rel).read_bytes() == orphan_bytes
    assert approved["deleted"]["orphan_blobs_count"] == 1  # 只删了另一个真正的根孤儿


def test_cli_preconditions_desktop_bridge_held_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. 参数互斥 / 非法保留期 / 缺 --data-root 均退 2
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    before = _tree_snapshot(root)

    for bad_args in (
        ("data", "gc"),
        ("data", "gc", "--data-root", root, "--dry-run", "--approve"),
        ("data", "gc", "--data-root", root, "--older-than-days", "0"),
        ("data", "gc", "--data-root", root, "--older-than-days", "-7"),
        ("data", "gc", "--data-root", tmp_path / "missing_root", "--approve"),
    ):
        res = _cli(*bad_args, "--json")
        assert res.returncode == 2, (bad_args, res.stdout, res.stderr)
    assert not (tmp_path / "missing_root").exists()
    assert _tree_snapshot(root) == before

    # 2. 空库 / 损坏库 / 未完成迁移（v1 中间态）退 2
    empty_root = tmp_path / "empty_db_root"
    empty_root.mkdir()
    (empty_root / "events.db").write_bytes(b"")
    assert _cli("data", "gc", "--data-root", empty_root, "--json").returncode == 2

    corrupt_root = tmp_path / "corrupt_db_root"
    corrupt_root.mkdir()
    (corrupt_root / "events.db").write_bytes(b"not a valid sqlite3 database")
    assert _cli("data", "gc", "--data-root", corrupt_root, "--approve", "--json").returncode == 2

    v1_root = tmp_path / "v1_unmigrated_root"
    with closing(EventLog(v1_root / "events.db", blob_dir=v1_root / "blobs")) as log:
        log.append(
            "r_v1",
            "run.start",
            {"task": "t", "workspace": str(tmp_path), "out": "o.md", "limits": {}, "tools": []},
            actor_kind="core",
            legacy_write=True,
        )
    with closing(sqlite3.connect(v1_root / "events.db")) as c:
        c.execute(
            "CREATE TABLE migration_log (step INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        c.execute("INSERT INTO migration_log VALUES (1, 1000)")
        c.execute("PRAGMA user_version = 1")
        c.commit()
    v1_res = _cli("data", "gc", "--data-root", v1_root, "--approve", "--json")
    assert v1_res.returncode == 2
    assert json.loads(v1_res.stdout)["code"] == "migration_incomplete"

    # 3. 恢复隔离态（.restore-origin.json held）退 2 且零删除
    (root / ".restore-origin.json").write_bytes(b'{"version": 1}')
    try:
        held_preview = _cli("data", "gc", "--data-root", root, "--json")
        assert held_preview.returncode == 2
        assert json.loads(held_preview.stdout)["code"] == "restore_held"

        held_approve = _cli("data", "gc", "--data-root", root, "--approve", "--json")
        assert held_approve.returncode == 2
        assert json.loads(held_approve.stdout)["code"] == "restore_held"
        assert (root / fx["orphan_artifact_rel"]).is_file()
    finally:
        (root / ".restore-origin.json").unlink()

    # 4. 路径与密钥脱敏验证
    secret_val = "sk-super-secret-gc-token-987654321"
    register_secret(secret_val)
    err_rep, err_code = run_data_gc(
        tmp_path / f"missing_{secret_val}",
        approve=True,
    )
    assert err_code == 2
    dumped = json.dumps(err_rep, ensure_ascii=False)
    assert secret_val not in dumped
    assert str(tmp_path) not in dumped

    # 5. --desktop 桥接兼容（前置与后置 --desktop 均可定位桌面默认根，与 --data-root 混用退 2）
    monkeypatch.setenv("TIANSHU_DATA_ROOT", str(root))
    assert cli.main(["--desktop", "data", "gc", "--dry-run", "--json"]) == 3
    assert cli.main(["data", "gc", "--desktop", "--dry-run", "--json"]) == 3
    assert cli.main(["--desktop", "data", "gc", "--data-root", str(root)]) == 2


@contextmanager
def _spawn_barrier_child(code: str, *args: object):
    """启动真子进程并通管道握手就绪，stderr 写临时文件防 64KB 缓冲死锁。"""
    with tempfile.TemporaryFile("w+b") as err_file:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code, *(str(a) for a in args)],
            env=_subproc_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_file,
            **CHILD_TEXT,
        )
        try:
            q: queue.Queue[str] = queue.Queue()
            reader = threading.Thread(
                target=lambda: q.put(proc.stdout.readline()),
                daemon=True,
            )
            reader.start()
            try:
                ready_line = q.get(timeout=15)
            except queue.Empty:
                proc.kill()
                proc.wait(timeout=10)
                err_file.seek(0)
                err_txt = err_file.read().decode("utf-8", "replace")
                raise AssertionError(f"子进程未到达就绪屏障：{err_txt}") from None
            finally:
                reader.join(timeout=2)
            assert ready_line, "子进程提前退出未输出就绪信号"
            yield proc, json.loads(ready_line), err_file
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)


def test_true_subprocess_holding_eventlog_blocks_gc_approve_without_disrupting_writer(
    tmp_path: Path,
) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    holder_code = """
import json, sys
from pathlib import Path
from tianshu.events import EventLog

root = Path(sys.argv[1])
run_id = sys.argv[2]
log = EventLog(root / "events.db", blob_dir=root / "blobs")
try:
    print(json.dumps({"ready": True, "pid": __import__("os").getpid()}), flush=True)
    cmd = sys.stdin.readline().strip()
    assert cmd == "append_and_exit"
    seq = log.append(
        run_id,
        "tool.result",
        {
            "call_id": "after_gc_refused",
            "tool_id": "read_text",
            "status": "succeeded",
            "effect_state": "none_applied",
            "output": {"content": "child holder still alive"},
        },
        actor_kind="core",
    )
    print(json.dumps({"appended_seq": seq}), flush=True)
finally:
    log.close()
"""
    with _spawn_barrier_child(holder_code, root, fx["effect_run_id"]) as (proc, info, err_file):
        assert info["ready"] is True
        gc_proc = _cli("data", "gc", "--data-root", root, "--approve", "--json")
        assert gc_proc.returncode == 2, (gc_proc.stdout, gc_proc.stderr)
        gc_rep = json.loads(gc_proc.stdout)
        assert gc_rep["ok"] is False
        assert gc_rep["code"] == "maintenance_busy"
        assert gc_rep["deleted"]["orphan_blobs_count"] == 0
        # 孤儿文件在锁争用下分毫未动
        assert (root / fx["orphan_artifact_rel"]).is_file()
        assert (root / fx["orphan_root_rel"]).is_file()

        # 放行持锁子进程继续追加事件并正常退出
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write("append_and_exit\n")
        proc.stdin.flush()
        done_line = proc.stdout.readline()
        rc_child = proc.wait(timeout=10)
        err_file.seek(0)
        child_err = err_file.read().decode("utf-8", "replace")
        assert rc_child == 0, child_err
        assert child_err == ""
        appended = json.loads(done_line)
        assert appended["appended_seq"] >= 1


def test_low_disk_below_2gib_and_zero_bytes_still_reclaims_space(tmp_path: Path) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]

    # 先证明同一数据根在模拟 0 字节与 100 MiB（< 2 GiB）下拒绝启动新的副作用 Run
    for simulated_free in (0, 100 * 1024 * 1024):
        with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
            st = build_stack(log, data_root=root)
            st.tasks._disk_admission = DiskAdmission(
                st.tasks.disk_admission.context,
                probe=lambda _p, b=simulated_free: b,
            )
            task = st.tasks.create_task("低盘下尝试新建副作用运行", [str(fx["ws"])])
            with pytest.raises(DiskAdmissionError) as exc_info:
                st.tasks.start_run(task.task_id)
            assert exc_info.value.rule_id == "dispatch.disk_space_low"

    # 阶段 1：极端 0 字节可用空间下，data gc --approve 依然成功退 0 并回收全部孤儿文件！
    zero_rep, zero_code = run_data_gc(
        root,
        older_than_days=90,
        approve=True,
        clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _p: 0,
    )
    assert zero_code == 0, zero_rep
    assert zero_rep["ok"] is True
    assert zero_rep["deleted"]["orphan_blobs_count"] == 2
    assert zero_rep["deleted"]["empty_snapshots_count"] == 1
    assert zero_rep["db_rows_deferred_low_disk"] is True
    assert zero_rep["deleted"]["jobs_count"] == 0
    assert not (root / fx["orphan_artifact_rel"]).exists()
    assert not (root / fx["orphan_root_rel"]).exists()

    # 阶段 2：回收文件后空间回升至 100 MiB（仍 < 2 GiB），再次执行完成剩余 DB 行清理与 WAL 截断
    sub_2gib_code = """
import shutil, sys
from collections import namedtuple
usage = namedtuple("usage", ["total", "used", "free"])
shutil.disk_usage = lambda _p: usage(10 * 1024**3, 10 * 1024**3 - 100 * 1024**2, 100 * 1024**2)
from tianshu.cli import main
raise SystemExit(main())
"""
    cli_low_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            sub_2gib_code,
            "data",
            "gc",
            "--data-root",
            str(root),
            "--older-than-days",
            "90",
            "--approve",
            "--json",
        ],
        env=_subproc_env(),
        capture_output=True,
        timeout=30,
        **CHILD_TEXT,
    )
    assert cli_low_proc.returncode == 0, (cli_low_proc.stdout, cli_low_proc.stderr)
    low_rep = json.loads(cli_low_proc.stdout)
    assert low_rep["ok"] is True
    assert low_rep["db_rows_deferred_low_disk"] is False
    # 传入固定逻辑时钟复验剩余超期 jobs 与 settled+known 预占行均已清零
    final_rep, final_code = run_data_gc(
        root,
        older_than_days=90,
        approve=True,
        clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _p: 100 * 1024 * 1024,
    )
    assert final_code == 0
    assert (
        low_rep["deleted"]["jobs_count"] + final_rep["deleted"]["jobs_count"] == 2
    )
    assert (
        low_rep["deleted"]["budget_reservations_count"]
        + final_rep["deleted"]["budget_reservations_count"]
        == 1
    )


def test_live_daemon_lock_subprocess_and_exact_cutoff_boundary(tmp_path: Path) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]

    # 1. 真子进程持有 daemon.lock（未持 EventLog 文件锁时），writers_active 仍必须拦截 --approve
    daemon_holder = """
import json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
run_dir = root / "run"
run_dir.mkdir(parents=True, exist_ok=True)
lock_file = run_dir / "daemon.lock"
lock_file.write_bytes(json.dumps({"pid": os.getpid(), "instance_id": "d_live"}).encode())
try:
    print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
    sys.stdin.readline()
finally:
    lock_file.unlink(missing_ok=True)
"""
    with _spawn_barrier_child(daemon_holder, root) as (proc, _info, _err):
        busy_proc = _cli("data", "gc", "--data-root", root, "--approve", "--json")
        assert busy_proc.returncode == 2
        busy_rep = json.loads(busy_proc.stdout)
        assert busy_rep["code"] == "maintenance_busy"
        assert (root / fx["orphan_artifact_rel"]).is_file()
        assert proc.stdin is not None
        proc.stdin.write("stop\n")
        proc.stdin.flush()
        assert proc.wait(timeout=10) == 0

    # 2. 精确截止时刻边界：cutoff_ms = now_ms - N * 86_400_000
    # ended_at == cutoff_ms 尚未严格超期（保留），ended_at == cutoff_ms - 1 超期（清理）
    boundary_now = fx["old_ms"] + 500 + 30 * DAY_MS
    rep_at_boundary, code_b = run_data_gc(
        root,
        older_than_days=30,
        dry_run=True,
        clock=lambda: boundary_now,
    )
    assert code_b == 3
    # old_ms + 500 == cutoff_ms -> job_cancelled_old 不在候选集；
    # old_ms + 1/2/3 < cutoff_ms 但属非终态 Run 仍受保护；effect_job_id 同样保留
    assert rep_at_boundary["candidates"]["jobs_count"] == 0

    rep_past_boundary, code_p = run_data_gc(
        root,
        older_than_days=30,
        dry_run=True,
        clock=lambda: boundary_now + 1001,
    )
    assert code_p == 3
    assert rep_past_boundary["candidates"]["jobs_count"] == 2
