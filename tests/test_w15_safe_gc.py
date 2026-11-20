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
     **0 文件删除、0 数据库行删除**；锁内重新收集引用，不使用旧预演结果。
4. 真子进程锁互斥（标准管道屏障，零 `sleep`）：活跃子进程持有 `EventLog`（或 `daemon`）时，
   `tianshu data gc --approve` 以退出码 `2`（`maintenance_busy`）拒绝、0 删除，
   子进程继续正常追加事件。
5. POSIX 低空间静态库仍可回收孤儿；无目录同步平台拒绝含文件候选的整批请求。
   SQLite-only 路径在空间足够时仍可执行，低于 SQLite 最低余量时延后。
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support import CHILD_TEXT, approve_pending, engine_for, stack_for, write_exact

from tianshu import cli, data_cli, data_gc, memory_control, restore_access
from tianshu.backup import collect_references, create_backup, verify_backup
from tianshu.budget import BudgetLedger
from tianshu.credentials import register_secret
from tianshu.data_gc import DAY_MS, run_data_gc
from tianshu.disk_admission import DiskAdmission, DiskAdmissionError
from tianshu.events import EventLog
from tianshu.execpolicy import ExecPolicy
from tianshu.jobs import JOB_CANCELLED, JOB_DONE
from tianshu.loop import Limits, build_stack, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.platform_identity import open_entity
from tianshu.receipt import verify_receipts
from tianshu.replay import replay
from tianshu.resources import MemoryScope
from tianshu.restore_contract import (
    AbsentTargetV1,
    BindingV1,
    ColumnRefV1,
    ColumnV1,
    CopyFileV1,
    CopyInventoryV1,
    DatabaseBytesV1,
    DirRefV1,
    EntityRefV1,
    FamilyCoverageV1,
    ManifestRefV1,
    NativePathCodecV1,
    OriginV1,
    PlanInputsV1,
    RegistryMetaV1,
    RelationDigestV1,
    ReservedV1,
    RestoreEventV1,
    ScopeManifestV1,
    SealedHoldV1,
    SqlValueV1,
    StablePlacementV1,
    domain_digest,
)
from tianshu.restore_registry import PresentRegistryObservation, RestoreRegistry
from tianshu.snapshot import SnapshotStore
from tianshu.summaries import MaterialBlock, SummaryStore, build_material
from tianshu.undo_journal import ACTION_RESTORE, JournalItem, JournalStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
STARTUP = str(Path(__file__).resolve().parent / "_startup")
requires_gc_file_deletion = pytest.mark.skipif(
    os.name != "posix",
    reason="requires actual file unlink/parent fsync; Windows policy tracked in #200",
)


@pytest.fixture(autouse=True)
def _pin_restore_registry_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """将进程内默认恢复注册表隔离到用例私有目录，严禁触碰宿主机 `~/.tianshu`。"""
    isolated_home = tmp_path / "_isolated_restore_home"
    isolated_home.mkdir(exist_ok=True)
    monkeypatch.setattr(
        restore_access,
        "_registry_factory",
        lambda: RestoreRegistry._for_home(isolated_home),
    )
    return isolated_home


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


def test_approve_gc_does_not_recreate_database_deleted_before_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    db = root / "events.db"
    real_acquire = data_gc.acquire_restore_database_access
    calls = 0

    def delete_before_acquire(db_path: Path, **kwargs: Any):
        nonlocal calls
        calls += 1
        assert db_path == db
        db_path.unlink()
        return real_acquire(db_path, **kwargs)

    monkeypatch.setattr(data_gc, "acquire_restore_database_access", delete_before_acquire)
    report, code = run_data_gc(
        root,
        older_than_days=90,
        approve=True,
        clock=lambda: fx["now_ms"],
    )

    assert calls == 1
    assert code == 2
    assert report["code"] == "incompatible"
    assert not db.exists()
    assert report["deleted"]["orphan_blobs_count"] == 0
    assert report["deleted"]["empty_snapshots_count"] == 0
    assert (root / fx["orphan_artifact_rel"]).is_file()
    assert (root / fx["orphan_root_rel"]).is_file()


def _leave_committed_wal(root: Path) -> Path:
    """真实子进程提交未 checkpoint 的事件并硬退，留下 WAL 中的权威材料引用。"""
    blobs_before = set((root / "blobs").iterdir())
    leave_wal_code = """
import os, sys
from pathlib import Path
from tianshu.events import EventLog, MAX_INLINE_BYTES

root = Path(sys.argv[1])
log = EventLog(root / "events.db", blob_dir=root / "blobs")
log.conn.execute("PRAGMA wal_autocheckpoint=0")
for size in (32_768, MAX_INLINE_BYTES + 1):
    seq = log.append(
        "run_live_running",
        "tool.result",
        {
            "call_id": f"call_wal_{size}",
            "tool_id": "read_text",
            "status": "succeeded",
            "effect_state": "none_applied",
            "output": {"content": "W" * size},
        },
        actor_kind="core",
    )
ref = log.conn.execute('SELECT payload_ref FROM events WHERE seq = ?', (seq,)).fetchone()[0]
assert ref is not None and Path(ref).is_file()
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
    (wal_blob,) = set((root / "blobs").iterdir()) - blobs_before
    return wal_blob


@pytest.mark.parametrize("has_shm", [False, True], ids=["missing-shm", "existing-shm"])
@pytest.mark.parametrize("mode", ["preview", "no-headroom", "no-directory-sync"])
def test_gc_readonly_preflight_refuses_wal_without_mutating_it(
    _gc_effects_fixture, monkeypatch: pytest.MonkeyPatch, has_shm: bool, mode: str,
) -> None:
    fx = _gc_effects_fixture
    root: Path = fx["root"]
    _leave_committed_wal(root)
    shm = root / "events.db-shm"
    assert shm.is_file()
    if not has_shm:
        shm.unlink()
    before = _tree_snapshot(root)
    before_root_mtime = root.stat().st_mtime_ns

    if mode != "preview":
        if mode == "no-directory-sync":
            monkeypatch.setattr(data_gc, "os", SimpleNamespace(name="nt"))
        report, code = run_data_gc(
            root, approve=True, clock=lambda: fx["now_ms"],
            disk_free_probe=lambda _path: 0 if mode == "no-headroom" else 1 << 40,
        )
    else:
        result = _cli("data", "gc", "--data-root", root, "--dry-run", "--json")
        report, code = json.loads(result.stdout), result.returncode

    assert _tree_snapshot(root) == before
    assert root.stat().st_mtime_ns == before_root_mtime
    assert code == 2, report
    assert report["code"] == "incompatible"
    assert set(report["deleted"].values()) == {0}
    assert report["wal_checkpoint"] is None


def test_approve_reclaims_only_safe_targets_truncates_wal_and_preserves_replay_and_backup(
    tmp_path: Path,
) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    db_path = root / "events.db"
    file_gc = os.name == "posix"
    if file_gc:
        wal_blob = _leave_committed_wal(root)
        # Without the committed WAL, the newly persisted payload has no visible reference.
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            assert conn.execute(
                "SELECT 1 FROM events WHERE payload_ref=?", (str(wal_blob),)
            ).fetchone() is None
    else:
        for rel in (
            fx["orphan_artifact_rel"], fx["orphan_root_rel"], fx["deletable_empty_snap_rel"],
        ):
            (root / rel).unlink()
    wal_path = root / "events.db-wal"
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
    assert report["deleted"]["orphan_blobs_count"] == (2 if file_gc else 0)
    assert report["deleted"]["orphan_blobs_bytes"] == (fx["orphan_bytes_total"] if file_gc else 0)
    assert report["deleted"]["empty_snapshots_count"] == int(file_gc)
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
    if file_gc:
        assert wal_blob.is_file()
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

    # Read-only backup inspection can leave SQLite companions. Re-establish the
    # clean offline precondition through SQLite's normal RW recovery/close.
    with closing(EventLog(db_path, blob_dir=root / "blobs")):
        pass
    assert not any(Path(str(db_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))

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
        with closing(EventLog(root / "events.db", blob_dir=root / "blobs")):
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


@requires_gc_file_deletion
@pytest.mark.parametrize("available_bytes", [0, 1 << 40])
@pytest.mark.parametrize("replace_kind", ["later_blob", "empty_snapshot"])
def test_approve_preflights_all_candidate_identities_before_any_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_bytes: int,
    replace_kind: str,
) -> None:
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    real_compute = data_gc._compute_candidates
    fault_state: dict[str, Any] = {}

    def replace_later_candidate(conn, candidate_root, index, cutoff_ms):
        candidates = real_compute(conn, candidate_root, index, cutoff_ms)
        ordered_blobs = tuple(sorted(candidates.orphan_blobs, key=lambda item: item.path))
        assert len(ordered_blobs) >= 2
        snap_rel = fx["deletable_empty_snap_rel"]
        if replace_kind == "later_blob":
            target = candidate_root / ordered_blobs[-1].path
        else:
            target = candidate_root / snap_rel
        replacement = target.with_name(target.name + ".replacement")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)

        fault_state["blob_paths"] = {
            item.path: hashlib.sha256((candidate_root / item.path).read_bytes()).hexdigest()
            for item in ordered_blobs
        }
        snap_rel = fx["deletable_empty_snap_rel"]
        fault_state["snapshot_sha"] = hashlib.sha256(
            (candidate_root / snap_rel).read_bytes()
        ).hexdigest()
        fault_state["jobs_digest"] = _table_digest(conn, "jobs")
        fault_state["reservations_digest"] = _table_digest(
            conn, "budget_reservations"
        )
        return replace(candidates, orphan_blobs=ordered_blobs)

    monkeypatch.setattr(data_gc, "_compute_candidates", replace_later_candidate)
    report, code = run_data_gc(
        root,
        older_than_days=90,
        approve=True,
        clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _path: available_bytes,
    )

    assert code == 2
    assert report["code"] == "reference_unknown"
    assert set(report["deleted"].values()) == {0}
    assert report["wal_checkpoint"] is None
    assert report["notes"] == []
    for rel, expected_sha in fault_state["blob_paths"].items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == expected_sha
    snapshot = root / fx["deletable_empty_snap_rel"]
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == fault_state["snapshot_sha"]
    with closing(sqlite3.connect((root / "events.db").as_uri() + "?mode=ro", uri=True)) as conn:
        assert _table_digest(conn, "jobs") == fault_state["jobs_digest"]
        assert _table_digest(conn, "budget_reservations") == fault_state[
            "reservations_digest"
        ]


@pytest.fixture
def _gc_effects_fixture(tmp_path: Path) -> dict[str, Any]:
    fx = _build_rich_fixture(tmp_path)
    fx["candidate_files"] = {
        fx["orphan_artifact_rel"], fx["orphan_root_rel"], fx["deletable_empty_snap_rel"],
    }
    fx["file_baseline"] = {
        rel: (fx["root"] / rel).read_bytes()
        for rel in fx["candidate_files"] | fx["protected_files"]
    }
    fx["protected_table_baseline"] = _protected_tables_snapshot(fx["root"] / "events.db")
    return fx


def _remove_file_candidates_from_fixture(fx: dict[str, Any]) -> None:
    for rel in fx["candidate_files"]:
        (fx["root"] / rel).unlink()
        del fx["file_baseline"][rel]
    fx["candidate_files"].clear()


@pytest.mark.parametrize("available_bytes", [0, 1 << 40], ids=["low", "normal"])
@pytest.mark.parametrize("only_files", ["blobs", "snapshots"])
def test_gc_requires_directory_sync_before_writes(
    _gc_effects_fixture, monkeypatch: pytest.MonkeyPatch, available_bytes: int, only_files: str,
) -> None:
    fx = _gc_effects_fixture
    root = fx["root"]
    snapshot_rel = fx["deletable_empty_snap_rel"]
    for rel in fx["candidate_files"]:
        if (rel == snapshot_rel) != (only_files == "snapshots"):
            (root / rel).unlink()
    before = _tree_snapshot(root)
    # 只切换产品的平台策略，SQLite、路径及文件操作仍由当前 OS 执行。
    monkeypatch.setattr(data_gc, "os", SimpleNamespace(name="nt"))
    preview, preview_code = run_data_gc(root, clock=lambda: fx["now_ms"])
    assert preview_code == 3, preview
    candidates = preview["candidates"]
    assert candidates["orphan_blobs_count"] == (2 if only_files == "blobs" else 0)
    assert candidates["empty_snapshots_count"] == (1 if only_files == "snapshots" else 0)
    assert candidates["jobs_count"] == 2
    assert candidates["budget_reservations_count"] == 1

    real_connect = data_gc._connect_locked_rw
    writable_opens = []

    def track_write_connection(db: Path) -> sqlite3.Connection:
        writable_opens.append(db)
        return real_connect(db)

    monkeypatch.setattr(data_gc, "_connect_locked_rw", track_write_connection)
    report, code = run_data_gc(
        root, approve=True, clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _path: available_bytes,
    )
    assert code == 2, report
    assert report["code"] == "incompatible"
    assert set(report["deleted"].values()) == {0}
    assert report["wal_checkpoint"] is None
    assert writable_opens == []
    assert _tree_snapshot(root) == before


@pytest.mark.parametrize("headroom_ok", [False, True], ids=["deferred", "committed"])
def test_gc_rows_only_without_directory_sync(
    _gc_effects_fixture, monkeypatch: pytest.MonkeyPatch, headroom_ok: bool,
) -> None:
    fx = _gc_effects_fixture
    root = fx["root"]
    _remove_file_candidates_from_fixture(fx)
    before = _tree_snapshot(root)
    monkeypatch.setattr(data_gc, "os", SimpleNamespace(name="nt"))
    headroom = data_gc.MIN_DB_GC_HEADROOM_BYTES - int(not headroom_ok)

    report, code = run_data_gc(
        root, approve=True, clock=lambda: fx["now_ms"], disk_free_probe=lambda _path: headroom,
    )
    assert code == 0, report
    assert report["candidates"]["orphan_blobs_count"] == 0
    assert report["candidates"]["empty_snapshots_count"] == 0
    assert report["candidates"]["jobs_count"] == 2
    assert report["candidates"]["budget_reservations_count"] == 1
    assert report["deleted"]["reclaimed_file_bytes"] == 0
    assert report["db_rows_deferred_low_disk"] is not headroom_ok
    if headroom_ok:
        assert report["deleted"]["jobs_count"] == 2
        assert report["deleted"]["budget_reservations_count"] == 1
        assert report["wal_checkpoint"] == {"busy": 0, "log": 0, "checkpointed": 0}
    else:
        assert set(report["deleted"].values()) == {0}
        assert report["wal_checkpoint"] is None
        assert _tree_snapshot(root) == before

    db = root / "events.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        jobs = {row[0] for row in conn.execute("SELECT job_id FROM jobs")}
        reservations = {
            row[0] for row in conn.execute("SELECT reservation_id FROM budget_reservations")
        }
    assert jobs == fx["kept_job_ids"] | (set() if headroom_ok else fx["deletable_job_ids"])
    pending_reservations = set() if headroom_ok else {fx["deletable_resv_id"]}
    assert reservations == fx["kept_resv_ids"] | pending_reservations
    assert _protected_tables_snapshot(db) == fx["protected_table_baseline"]


def _assert_gc_failure_effects(
    fx: dict[str, Any], report: dict[str, Any], code: int, *,
    removed: set[str], db_committed: bool,
) -> None:
    root: Path = fx["root"]
    for rel, content in fx["file_baseline"].items():
        if rel in removed:
            assert not (root / rel).exists()
        else:
            assert (root / rel).read_bytes() == content
    db = root / "events.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        jobs = {row[0] for row in conn.execute("SELECT job_id FROM jobs")}
        reservations = {
            row[0] for row in conn.execute("SELECT reservation_id FROM budget_reservations")
        }
    assert _protected_tables_snapshot(db) == fx["protected_table_baseline"]
    assert jobs == fx["kept_job_ids"] | (set() if db_committed else fx["deletable_job_ids"])
    assert reservations == fx["kept_resv_ids"] | (
        set() if db_committed else {fx["deletable_resv_id"]}
    )

    snapshots = removed & {fx["deletable_empty_snap_rel"]}
    blobs = removed - snapshots
    blob_bytes = sum(len(fx["file_baseline"][rel]) for rel in blobs)
    snapshot_bytes = sum(len(fx["file_baseline"][rel]) for rel in snapshots)
    assert code == 2 and report["ok"] is False, report
    assert report["code"] == "gc_failed"
    assert report["deleted"] == {
        "orphan_blobs_count": len(blobs),
        "orphan_blobs_bytes": blob_bytes,
        "empty_snapshots_count": len(snapshots),
        "empty_snapshots_bytes": snapshot_bytes,
        "jobs_count": len(fx["deletable_job_ids"]) if db_committed else 0,
        "budget_reservations_count": int(db_committed),
        "reclaimed_file_bytes": blob_bytes + snapshot_bytes,
    }
    assert report["notes"]


@requires_gc_file_deletion
@pytest.mark.parametrize("available_bytes", [0, 1 << 40])
def test_gc_effects_survive_later_unlink_error(
    _gc_effects_fixture, monkeypatch: pytest.MonkeyPatch, capsys, available_bytes: int,
) -> None:
    fx = _gc_effects_fixture
    root: Path = fx["root"]
    earlier = root / fx["orphan_artifact_rel"]
    later = root / fx["orphan_root_rel"]
    earlier_parent = earlier.parent.stat()
    real_unlink = Path.unlink
    real_fsync = os.fsync
    operations: list[str] = []

    def fail_later_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == later:
            operations.append("later unlink failed")
            raise OSError(errno.EIO, "injected later unlink failure", str(path))
        real_unlink(path, missing_ok=missing_ok)
        if path == earlier:
            operations.append("earlier unlinked")

    def record_fsync(fd: int) -> None:
        real_fsync(fd)
        if os.path.samestat(os.fstat(fd), earlier_parent):
            operations.append("earlier parent synced")

    monkeypatch.setattr(Path, "unlink", fail_later_unlink)
    monkeypatch.setattr(os, "fsync", record_fsync)
    report, code = run_data_gc(
        root, approve=True, clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _path: available_bytes,
    )
    assert "injected later unlink failure" in report["error"]
    assert str(root) not in report["error"]
    _assert_gc_failure_effects(
        fx, report, code, removed={fx["orphan_artifact_rel"]}, db_committed=bool(available_bytes),
    )
    assert operations == ["earlier unlinked", "earlier parent synced", "later unlink failed"]
    assert report["wal_checkpoint"] is None
    data_cli._print_report(report, as_json=False)
    out, err = capsys.readouterr()
    assert out == ""
    assert "已确认删除" in err
    assert f"孤儿 blob：1 个（{len(fx['file_baseline'][fx['orphan_artifact_rel']])} 字节）" in err
    assert f"jobs 行：{len(fx['deletable_job_ids']) if available_bytes else 0} 行" in err


@requires_gc_file_deletion
@pytest.mark.parametrize("sync_root", [False, True], ids=["candidate-parent", "data-root"])
@pytest.mark.parametrize("available_bytes", [0, 1 << 40], ids=["unlink-first", "db-first"])
def test_gc_effects_survive_directory_fsync_error(
    _gc_effects_fixture, monkeypatch: pytest.MonkeyPatch, sync_root: bool, available_bytes: int,
) -> None:
    fx = _gc_effects_fixture
    root: Path = fx["root"]
    directory = root if sync_root else (root / fx["orphan_artifact_rel"]).parent
    directory_stat = directory.stat()
    real_fsync = os.fsync
    calls = []
    removed = fx["candidate_files"] if sync_root else {fx["orphan_artifact_rel"]}

    def fail_directory_fsync(fd: int) -> None:
        if os.path.samestat(os.fstat(fd), directory_stat):
            calls.append({rel for rel in fx["candidate_files"] if not (root / rel).exists()})
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    report, code = run_data_gc(
        root, approve=True, clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _path: available_bytes,
    )
    assert calls == [removed]
    _assert_gc_failure_effects(
        fx, report, code, removed=removed, db_committed=bool(available_bytes),
    )
    assert "injected directory fsync failure" in report["error"]
    expected_checkpoint = (
        {"busy": 0, "log": 0, "checkpointed": 0} if sync_root and available_bytes else None
    )
    assert report["wal_checkpoint"] == expected_checkpoint


@pytest.mark.parametrize("low_disk", [False, True], ids=["normal", "headroom-recovers"])
def test_gc_effects_with_db_delete_rollback(_gc_effects_fixture, low_disk: bool) -> None:
    fx = _gc_effects_fixture
    if os.name != "posix":
        _remove_file_candidates_from_fixture(fx)
    root: Path = fx["root"]
    with closing(sqlite3.connect(root / "events.db")) as conn:
        # Real SQLite abort after the preceding jobs DELETE has changed the transaction.
        conn.execute("""
            CREATE TRIGGER gc_test_abort_reservation_delete BEFORE DELETE ON budget_reservations
            WHEN NOT EXISTS (SELECT 1 FROM jobs WHERE job_id = 'job_cancelled_old')
            BEGIN SELECT RAISE(ABORT, 'injected reservation delete failure'); END
        """)
    space_checks = iter((0 if low_disk else 1 << 40, 1 << 40))
    report, code = run_data_gc(
        root, approve=True, clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _path: next(space_checks),
    )
    assert "injected reservation delete failure" in report["error"]
    _assert_gc_failure_effects(
        fx, report, code, removed=fx["candidate_files"] if low_disk else set(), db_committed=False,
    )
    assert report["wal_checkpoint"] is None
    assert report["db_rows_deferred_low_disk"] is False


def test_gc_effects_survive_sqlite_checkpoint_error(
    _gc_effects_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _gc_effects_fixture
    if os.name != "posix":
        _remove_file_candidates_from_fixture(fx)
    connect_rw = data_gc._connect_locked_rw

    def connect_with_checkpoint_failure(db: Path) -> sqlite3.Connection:
        conn = connect_rw(db)
        conn.set_authorizer(
            lambda action, arg, *rest: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and arg == "wal_checkpoint" else sqlite3.SQLITE_OK
        )
        return conn

    monkeypatch.setattr(data_gc, "_connect_locked_rw", connect_with_checkpoint_failure)
    report, code = run_data_gc(
        fx["root"], approve=True, clock=lambda: fx["now_ms"], disk_free_probe=lambda _path: 1 << 40,
    )
    assert "not authorized" in report["error"]
    _assert_gc_failure_effects(fx, report, code, removed=fx["candidate_files"], db_committed=True)
    assert report["wal_checkpoint"] is None


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

    calls: list[bool] = []
    real_collect = collect_references

    def tracked_collect(target_root: Path, *, require_static_db: bool = False):
        calls.append(require_static_db)
        index = real_collect(target_root, require_static_db=require_static_db)
        assert promoted_rel in index.protected_blobs
        return index

    monkeypatch.setattr("tianshu.data_gc.collect_references", tracked_collect)
    approved, a_code = run_data_gc(
        root, older_than_days=90, approve=True, clock=lambda: fx["now_ms"],
        disk_free_probe=lambda _path: 1 << 40,
    )
    assert calls == [os.name != "posix"]
    # 锁内重算后 promoted_rel 已进入 protected_blobs，绝不能按旧预演清单删除！
    assert (root / promoted_rel).read_bytes() == orphan_bytes
    if os.name == "posix":
        assert a_code == 0
        assert approved["deleted"]["orphan_blobs_count"] == 1
    else:
        assert a_code == 2 and approved["code"] == "incompatible"
        assert set(approved["deleted"].values()) == {0}


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


@requires_gc_file_deletion
def test_gc_effects_with_real_busy_checkpoint(_gc_effects_fixture) -> None:
    fx = _gc_effects_fixture
    db = fx["root"] / "events.db"
    # An external read-only SQLite transaction can pin WAL frames without being an EventLog writer.
    reader = """
import json, sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1])
conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, isolation_level=None)
conn.execute('BEGIN')
assert conn.execute("SELECT 1 FROM jobs WHERE job_id = 'job_cancelled_old'").fetchone() == (1,)
print(json.dumps({'ready': True}), flush=True)
assert sys.stdin.readline().strip() == 'release'
assert conn.execute("SELECT 1 FROM jobs WHERE job_id = 'job_cancelled_old'").fetchone() == (1,)
conn.close()
"""
    with _spawn_barrier_child(reader, db) as (proc, ready, _err_file):
        assert ready == {"ready": True}
        report, code = run_data_gc(
            fx["root"], approve=True, clock=lambda: fx["now_ms"],
            disk_free_probe=lambda _path: 1 << 40,
        )
        # The SQLite return row is real, not an injected busy code.
        checkpoint = report["wal_checkpoint"]
        assert checkpoint["busy"] == 1
        assert checkpoint["log"] > checkpoint["checkpointed"] >= 0
        assert proc.stdin is not None
        proc.stdin.write("release\n")
        proc.stdin.flush()
        assert proc.wait(timeout=10) == 0
    _assert_gc_failure_effects(fx, report, code, removed=fx["candidate_files"], db_committed=True)


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
    if os.name == "posix":
        assert zero_code == 0, zero_rep
        assert zero_rep["ok"] is True
        assert zero_rep["deleted"]["orphan_blobs_count"] == 2
        assert zero_rep["deleted"]["empty_snapshots_count"] == 1
        assert zero_rep["db_rows_deferred_low_disk"] is True
        assert zero_rep["deleted"]["jobs_count"] == 0
        assert not (root / fx["orphan_artifact_rel"]).exists()
        assert not (root / fx["orphan_root_rel"]).exists()
    else:
        assert zero_code == 2 and zero_rep["code"] == "incompatible"
        assert set(zero_rep["deleted"].values()) == {0}
        # Remove fixture candidates to exercise the native SQLite-only CLI below.
        for rel in (
            fx["orphan_artifact_rel"], fx["orphan_root_rel"], fx["deletable_empty_snap_rel"],
        ):
            (root / rel).unlink()

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


def test_run_command_stdout_and_stderr_spill_blobs_protected_and_verified(
    tmp_path: Path,
) -> None:
    """Defect #1：run_command 溢出 stdout_ref/stderr_ref 受保护，损坏或缺失必闭锁退 2。"""
    root = (tmp_path / "exec_root").resolve()
    ws = (tmp_path / "exec_ws").resolve()
    ws.mkdir(parents=True)

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    snapshots = SnapshotStore(root / "artifacts")
    try:
        stack = stack_for(log, snapshots=snapshots, data_root=root)
        policy = ExecPolicy(
            argv_allowlist=(sys.executable,),
            cwd_roots=(ws,),
            env_allowlist=("PATH",),
            max_output_bytes=128,
            timeout_ms=10_000,
        )
        task = stack.tasks.create_task(
            "执行超长标准输出与错误流命令",
            [{"root_path": str(ws)}],
            exec_policy=policy,
        )
        cmd_code = (
            "import sys; "
            "sys.stdout.write('O' * 2048); sys.stdout.flush(); "
            "sys.stderr.write('E' * 2048); sys.stderr.flush()"
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "call_exec_spill",
                            "run_command",
                            {"argv": [sys.executable, "-c", cmd_code]},
                        )
                    ],
                ),
                ModelResponse("end_turn", content="已执行命令"),
            ]
        )
        engine = engine_for(stack, model, limits=Limits(6, 20_000))
        res = engine.start(task.task_id, out_name="out.md")
        while res.state == "waiting_approval":
            assert approve_pending(stack, res.run_id)
            res = engine.resume_run(res.run_id)
        assert res.state == "succeeded"
        run_id = res.run_id

        # 同时覆盖根 blobs/<sha256> 下的流溢出引用解析与核验
        root_err_bytes = b"R" * 2048
        root_err_sha = hashlib.sha256(root_err_bytes).hexdigest()
        (root / "blobs").mkdir(parents=True, exist_ok=True)
        (root / "blobs" / root_err_sha).write_bytes(root_err_bytes)
        log.append(
            run_id,
            "tool.result",
            {
                "call_id": "call_root_blob_stream",
                "tool_id": "run_command",
                "status": "succeeded",
                "effect_state": "none_applied",
                "output": {
                    "exit_code": 0,
                    "signal": None,
                    "timed_out": False,
                    "truncated": True,
                    "stdout": "",
                    "stderr": "R" * 64,
                    "stdout_ref": None,
                    "stderr_ref": root_err_sha,
                    "stderr_merged": False,
                    "duration_ms": 5,
                },
            },
            actor_kind="core",
        )

        assert verify_receipts(log, run_id) == []
        events = log.read_run(run_id)
        exec_results = [
            e.payload["output"]
            for e in events
            if e.type == "tool.result" and e.payload.get("call_id") == "call_exec_spill"
        ]
        assert len(exec_results) == 1
        stdout_ref = exec_results[0]["stdout_ref"]
        stderr_ref = exec_results[0]["stderr_ref"]
        assert stdout_ref == hashlib.sha256(b"O" * 2048).hexdigest()
        assert stderr_ref == hashlib.sha256(b"E" * 2048).hexdigest()
    finally:
        log.close()

    stdout_blob = root / "artifacts" / "blobs" / stdout_ref
    stderr_blob = root / "artifacts" / "blobs" / stderr_ref
    root_stderr_blob = root / "blobs" / root_err_sha
    assert stdout_blob.read_bytes() == b"O" * 2048
    assert stderr_blob.read_bytes() == b"E" * 2048

    # 放入真正的未引用孤儿文件，验证引用索引精确保护三个流溢出 blob、只把孤儿列为候选
    orphan_path = root / "artifacts" / "blobs" / ("9" * 64)
    orphan_path.write_bytes(b"true-unreferenced-orphan")

    ref_idx = collect_references(root)
    assert ref_idx.unknown == []
    assert f"artifacts/blobs/{stdout_ref}" in ref_idx.protected_blobs
    assert f"artifacts/blobs/{stderr_ref}" in ref_idx.protected_blobs
    assert f"blobs/{root_err_sha}" in ref_idx.protected_blobs

    future_ms = int(time.time() * 1000) + 10 * DAY_MS
    ok_rep, ok_code = run_data_gc(
        root,
        older_than_days=1,
        approve=True,
        clock=lambda: future_ms,
    )
    if os.name == "posix":
        assert ok_code == 0, ok_rep
        assert ok_rep["deleted"]["orphan_blobs_count"] == 1
        assert not orphan_path.exists()
    else:
        assert ok_code == 2 and ok_rep["code"] == "incompatible"
        assert set(ok_rep["deleted"].values()) == {0}
        assert orphan_path.read_bytes() == b"true-unreferenced-orphan"
    assert stdout_blob.read_bytes() == b"O" * 2048
    assert stderr_blob.read_bytes() == b"E" * 2048
    assert root_stderr_blob.read_bytes() == root_err_bytes

    # GC 后回放、创建备份与独立备份校验全部通过，且备份清单登记 exec_stream_blobs
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as check_log:
        rep = replay(check_log, run_id)
        assert rep.ok, rep.problems
    backup_dir = tmp_path / "exec_stream_backup"
    manifest = create_backup(root, backup_dir)
    assert len(manifest.references["exec_stream_blobs"]) == 3
    v_rep = verify_backup(backup_dir)
    assert v_rep.ok, v_rep.problems

    # Restore the clean offline precondition after read-only backup verification;
    # do not unlink companions or weaken the exact reference_unknown assertions.
    db_path = root / "events.db"
    with closing(EventLog(db_path, blob_dir=root / "blobs")):
        pass
    assert not any(Path(str(db_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))

    # 负控：篡改或删除任一 stdout_ref / stderr_ref 原文时，GC 必以 reference_unknown 退 2 且 0 删除
    for target_blob, mode in (
        (stdout_blob, "tamper"),
        (stdout_blob, "delete"),
        (stderr_blob, "tamper"),
        (root_stderr_blob, "delete"),
    ):
        saved_bytes = target_blob.read_bytes()
        orphan_path.write_bytes(b"must-not-be-deleted-when-stream-ref-broken")
        try:
            if mode == "tamper":
                target_blob.write_bytes(b"corrupted-exec-stream-evidence")
            else:
                target_blob.unlink()
            cli_res = _cli(
                "data", "gc", "--data-root", root, "--older-than-days", "1", "--approve", "--json"
            )
            assert cli_res.returncode == 2, (mode, target_blob, cli_res.stdout, cli_res.stderr)
            err_json = json.loads(cli_res.stdout)
            assert err_json["ok"] is False
            assert err_json["code"] == "reference_unknown"
            assert err_json["deleted"]["orphan_blobs_count"] == 0
            assert orphan_path.is_file()
        finally:
            target_blob.write_bytes(saved_bytes)


@dataclass(frozen=True)
class _ReleasedPayloadStub:
    final_root: DirRefV1
    final_database: EntityRefV1
    binding_sha256: str
    origin_sha256: str


@dataclass(frozen=True)
class _SyntheticRestoreEvent:
    version: int
    registry_id: str
    sequence: int
    batch_id: str
    previous_sha256: str | None
    kind: str
    payload: Any


def _make_minimal_gc_root(root: Path, orphan_tag: bytes) -> Path:
    ws = root.parent / f"{root.name}_ws"
    ws.mkdir(parents=True, exist_ok=True)
    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        log.append(
            "r_min",
            "run.start",
            {
                "task": "min",
                "workspace": str(ws),
                "out": "out.md",
                "limits": {},
                "tools": [],
            },
            actor_kind="core",
            legacy_write=True,
        )
    orphan = root / "blobs" / hashlib.sha256(orphan_tag).hexdigest()
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(orphan_tag)
    return orphan


def _build_batch_events_for_root(
    *,
    registry_id: str,
    batch_id: str,
    seq_start: int,
    prev_sha: str | None,
    root: Path,
    backup_dir: Path,
    released: bool,
) -> tuple[tuple[Any, ...], OriginV1]:
    codec = NativePathCodecV1()
    parent = root.parent.resolve()
    root_resolved = root.resolve()
    db_path = root_resolved / "events.db"

    def _dir_ref(p: Path) -> DirRefV1:
        h = open_entity(p, "directory")
        try:
            return DirRefV1(codec.from_path(p.resolve()), h.identity)
        finally:
            h.close()

    def _file_ref(p: Path) -> EntityRefV1:
        h = open_entity(p, "file")
        try:
            return EntityRefV1(codec.from_path(p.resolve()), h.identity)
        finally:
            h.close()

    p_ref = _dir_ref(parent)
    s_ref = _dir_ref(root_resolved)
    d_ref = _file_ref(db_path)
    b_ref = _dir_ref(backup_dir)

    db_raw = db_path.read_bytes()
    db_sha = hashlib.sha256(db_raw).hexdigest()
    inv = CopyInventoryV1(1, (), (CopyFileV1(("events.db",), len(db_raw), db_sha),))
    inv_sha = domain_digest("inventory", inv)
    manifest = ManifestRefV1(
        base64.b64encode(b"{}").decode("ascii"),
        hashlib.sha256(b"{}").hexdigest(),
    )
    target_np = codec.from_path(root_resolved)
    target_db_np = codec.from_path(db_path)
    placement = StablePlacementV1(target_np, root_resolved.name, (p_ref,), ())
    absent = AbsentTargetV1("absent")
    inputs = PlanInputsV1(
        1,
        b_ref,
        manifest,
        inv,
        inv_sha,
        target_np.text,
        (),
        placement,
        absent,
        None,
        None,
        (),
        1,
        1,
    )
    col = ColumnV1(0, "seq", "INTEGER", True, SqlValueV1("null", None), 1)
    rel = RelationDigestV1("events", (col,), SqlValueV1("null", None), 1, "a" * 64)
    cov = FamilyCoverageV1(
        "events", ("events",), (ColumnRefV1("events", "seq"),), (("events.db",),), ("all",)
    )
    scope = ScopeManifestV1(1, 1, "a" * 64, (rel,), (cov,), inv)
    binding = BindingV1(
        1,
        registry_id,
        batch_id,
        target_np,
        target_db_np,
        s_ref.identity,
        d_ref.identity,
        target_np.text,
        (),
        manifest,
        inv,
        inv_sha,
        DatabaseBytesV1(len(db_raw), db_sha),
        scope,
        domain_digest("scope", scope),
        (),
    )
    binding_sha = domain_digest("binding", binding)
    stage_name = f".{root_resolved.name}.restore-{batch_id}"
    reserved = ReservedV1(
        inputs,
        domain_digest("plan", inputs),
        inv,
        inv_sha,
        placement,
        absent,
        stage_name,
    )
    e_reserved = RestoreEventV1(
        1, registry_id, seq_start, batch_id, prev_sha, "reserved", reserved
    )
    e_reserved_sha = domain_digest("event", e_reserved)
    sealed = SealedHoldV1(
        "b" * 64,
        s_ref.identity,
        d_ref.identity,
        binding,
        binding_sha,
    )
    e_sealed = RestoreEventV1(
        1, registry_id, seq_start + 1, batch_id, e_reserved_sha, "sealed_hold", sealed
    )
    e_sealed_sha = domain_digest("event", e_sealed)
    origin = OriginV1(1, registry_id, batch_id, e_sealed_sha, binding_sha)
    origin_sha = domain_digest("origin", origin)
    if not released:
        e_pub = _SyntheticRestoreEvent(
            1,
            registry_id,
            seq_start + 2,
            batch_id,
            e_sealed_sha,
            "published_hold",
            {"binding_sha256": binding_sha, "origin_sha256": origin_sha},
        )
        return (e_reserved, e_sealed, e_pub), origin

    e_rel = _SyntheticRestoreEvent(
        1,
        registry_id,
        seq_start + 2,
        batch_id,
        e_sealed_sha,
        "released",
        _ReleasedPayloadStub(
            final_root=s_ref,
            final_database=d_ref,
            binding_sha256=binding_sha,
            origin_sha256=origin_sha,
        ),
    )
    return (e_reserved, e_sealed, e_rel), origin


def test_restore_registry_dynamic_rebind_cross_batch_released_and_control_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect #2：动态调 _registry_factory()，严绑单批 ReleasedV1 与 (S, D) 实体身份。"""
    reg_id = "12345678-1234-4234-9234-123456789abc"
    batch_a = "11111111-2222-4333-8444-555555555555"
    batch_b = "66666666-7777-4888-9999-aaaaaaaaaaaa"
    batch_unknown = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

    backup_dummy = tmp_path / "dummy_backup"
    backup_dummy.mkdir()
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    orphan_a = _make_minimal_gc_root(root_a, b"orphan-in-root-a")
    orphan_b = _make_minimal_gc_root(root_b, b"orphan-in-root-b")

    evs_a, origin_a = _build_batch_events_for_root(
        registry_id=reg_id,
        batch_id=batch_a,
        seq_start=1,
        prev_sha=None,
        root=root_a,
        backup_dir=backup_dummy,
        released=True,
    )
    evs_b, origin_b = _build_batch_events_for_root(
        registry_id=reg_id,
        batch_id=batch_b,
        seq_start=4,
        prev_sha="c" * 64,
        root=root_b,
        backup_dir=backup_dummy,
        released=False,
    )

    custom_home = tmp_path / "custom_reg_home"
    auth_dir = custom_home / ".tianshu" / "restore-registry-v1"
    auth_dir.mkdir(parents=True)
    reg_db_file = auth_dir / "registry.db"
    reg_db_file.write_bytes(b"placeholder")

    codec = NativePathCodecV1()

    def _id_of(p: Path, kind: str):
        h = open_entity(p, kind)
        try:
            return h.identity
        finally:
            h.close()

    home_id = _id_of(custom_home, "directory")
    auth_id = _id_of(auth_dir, "directory")
    reg_db_id = _id_of(reg_db_file, "file")
    meta = RegistryMetaV1(
        1,
        reg_id,
        DirRefV1(codec.from_path(custom_home.resolve()), home_id),
        DirRefV1(codec.from_path(auth_dir.resolve()), auth_id),
        EntityRefV1(codec.from_path(reg_db_file.resolve()), reg_db_id),
    )

    obs = PresentRegistryObservation(
        True,
        custom_home,
        auth_dir,
        reg_db_file,
        home_id,
        auth_id,
        reg_db_id,
        meta,
        meta.to_bytes(),
        (*evs_a, *evs_b),
        6,
        "d" * 64,
    )

    counters = {"observe": 0, "revalidate": 0}

    class _CustomRegistry:
        def observe(self):
            counters["observe"] += 1
            return obs

        def revalidate(self, expected: PresentRegistryObservation) -> None:
            counters["revalidate"] += 1
            assert expected.head_sequence == obs.head_sequence
            assert expected.head_sha256 == obs.head_sha256

    # 在 data_gc 模块早已导入之后重绑 restore_access._registry_factory，证明运行时动态查找
    monkeypatch.setattr(restore_access, "_registry_factory", lambda: _CustomRegistry())

    # 1. 跨批次负控：虽然同一注册表里 Batch A 已是 released，但 Root B 处于 published_hold，必须拒！
    (root_b / ".restore-origin.json").write_bytes(origin_b.to_bytes())
    for kwargs in ({"dry_run": True}, {"approve": True}):
        rep_b, code_b = run_data_gc(root_b, older_than_days=1, **kwargs)
        assert code_b == 2, (kwargs, rep_b)
        assert rep_b["code"] == "restore_held"
        assert orphan_b.is_file()
    assert counters["observe"] >= 2

    # 2. 跨根伪造负控：把已 released 的 Root A 的 .restore-origin.json 复制到 Root B（实体不匹配）
    (root_b / ".restore-origin.json").write_bytes(origin_a.to_bytes())
    for kwargs in ({"dry_run": True}, {"approve": True}):
        rep_b2, code_b2 = run_data_gc(root_b, older_than_days=1, **kwargs)
        assert code_b2 == 2, (kwargs, rep_b2)
        assert rep_b2["code"] == "restore_held"
        assert orphan_b.is_file()

    # 3. 未知批次 / 摘要篡改负控：在存在其他 released 批次时，伪造未知 batch_id 依然闭锁拒绝
    forged_origin = OriginV1(
        1, reg_id, batch_unknown, origin_a.sealed_sha256, origin_a.binding_sha256
    )
    (root_b / ".restore-origin.json").write_bytes(forged_origin.to_bytes())
    rep_b3, code_b3 = run_data_gc(root_b, older_than_days=1, approve=True)
    assert code_b3 == 2
    assert rep_b3["code"] == "restore_held"
    assert orphan_b.is_file()

    # 4. Root A 写入自身匹配的合法 .restore-origin.json：预演放行退 3！
    (root_a / ".restore-origin.json").write_bytes(origin_a.to_bytes())
    rep_a_dry, code_a_dry = run_data_gc(root_a, older_than_days=1, dry_run=True)
    assert code_a_dry == 3, rep_a_dry
    assert rep_a_dry["ok"] is True
    assert rep_a_dry["candidates"]["orphan_blobs_bytes"] == len(b"orphan-in-root-a")

    # 5. 无条件控制标记负控：即便 Root A 已 released，残留临时/token 控制文件也无条件退 2！
    for marker_name in (
        ".restore-origin.json.tmp",
        ".restore-token.json",
        ".restore-token.json.tmp",
    ):
        marker_file = root_a / marker_name
        marker_file.write_bytes(origin_a.to_bytes())
        try:
            for kwargs in ({"dry_run": True}, {"approve": True}):
                m_rep, m_code = run_data_gc(root_a, older_than_days=1, **kwargs)
                assert m_code == 2, (marker_name, kwargs, m_rep)
                assert m_rep["code"] == "restore_held"
                assert orphan_a.is_file()
        finally:
            marker_file.unlink()

    # 6. 缺失见证负控：Root A 的 (S, D) 命中已解除批次，但磁盘 .restore-origin.json 被删，仍拒绝
    (root_a / ".restore-origin.json").unlink()
    missing_origin_rep, missing_origin_code = run_data_gc(
        root_a, older_than_days=1, approve=True
    )
    assert missing_origin_code == 2
    assert missing_origin_rep["code"] == "restore_held"
    assert orphan_a.is_file()

    # 7. 合法见证解除隔离；实际文件删除仍取决于平台能否同步目录。
    (root_a / ".restore-origin.json").write_bytes(origin_a.to_bytes())
    rep_a_exec, code_a_exec = run_data_gc(root_a, older_than_days=1, approve=True)
    if os.name == "posix":
        assert code_a_exec == 0, rep_a_exec
        assert rep_a_exec["ok"] is True
        assert rep_a_exec["deleted"]["orphan_blobs_count"] == 1
        assert not orphan_a.exists()
    else:
        assert code_a_exec == 2 and rep_a_exec["code"] == "incompatible"
        assert set(rep_a_exec["deleted"].values()) == {0}
        assert orphan_a.read_bytes() == b"orphan-in-root-a"
    assert counters["revalidate"] >= 1


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX RLIMIT_FSIZE 内核文件写入字节上限仅适用于类 Unix 平台",
)
def test_kernel_rlimit_fsize_zero_reclaims_orphans_without_creating_shm_or_wal(
    tmp_path: Path,
) -> None:
    """Defect #3：真子进程设 RLIMIT_FSIZE=(0, 0) 零写上限，不产 -shm/-wal 仍退 0 删孤儿。"""
    fx = _build_rich_fixture(tmp_path)
    root: Path = fx["root"]
    db_path = root / "events.db"

    assert not (root / "events.db-wal").exists()
    assert not (root / "events.db-shm").exists()
    db_bytes_before = db_path.read_bytes()

    rlimit_child = """
import resource, sys
# 保持默认 SIGXFSZ 信号语义：一旦尝试创建 32 KiB events.db-shm 或写任何 >0 字节文件将立即失败/被杀
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
from tianshu.cli import main
raise SystemExit(
    main(
        [
            "data",
            "gc",
            "--data-root",
            sys.argv[1],
            "--older-than-days",
            "1",
            "--approve",
            "--json",
        ]
    )
)
"""
    proc = subprocess.run(
        [sys.executable, "-c", rlimit_child, str(root)],
        env=_subproc_env(),
        capture_output=True,
        timeout=30,
        **CHILD_TEXT,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    rep = json.loads(proc.stdout)
    assert rep["ok"] is True
    assert rep["deleted"]["orphan_blobs_count"] == 2
    assert rep["deleted"]["empty_snapshots_count"] == 1
    assert rep["deleted"]["jobs_count"] == 0
    assert rep["deleted"]["budget_reservations_count"] == 0
    assert rep["db_rows_deferred_low_disk"] is True

    # 两个孤儿 blob 与超期空快照已真实从磁盘删除，events.db 零改动，且全程未产生 -wal / -shm
    assert not (root / fx["orphan_artifact_rel"]).exists()
    assert not (root / fx["orphan_root_rel"]).exists()
    assert not (root / fx["deletable_empty_snap_rel"]).exists()
    assert not (root / "events.db-wal").exists()
    assert not (root / "events.db-shm").exists()
    assert db_path.read_bytes() == db_bytes_before
    for rel in fx["protected_files"]:
        assert (root / rel).is_file(), f"受保护材料误删：{rel}"
