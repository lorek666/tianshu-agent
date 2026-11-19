"""Restore C 受管恢复隔离解除、确定性对账与受管 v2 数据库准入全矩阵测试（C01–C06）。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from restore_test_support import requires_managed_restore_writes
from support import CHILD_TEXT

from tianshu import authority, migrate, restore_manager, restore_registry
from tianshu.backup import create_backup, sha256_file, verify_backup
from tianshu.budget import BudgetLedger
from tianshu.data_state import AdmissionRefused
from tianshu.disk_admission import DiskAdmission
from tianshu.errors import McpObjectBlocked, WorkspaceBlocked
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.guard import (
    ensure_not_blocked,
    ensure_object_not_blocked,
    scan_blocked,
    scan_blocked_objects,
    unblock,
    unblock_object,
)
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.loop import build_stack
from tianshu.permissions import ActionRequest, ApprovalError, HumanIdentity, PolicyDecision
from tianshu.platform_identity import observe_entity
from tianshu.restore_access import RestoreScopeBusy
from tianshu.restore_contract import (
    RestoreResponseV2,
    domain_digest,
    validate_event_chain_v2,
)
from tianshu.restore_registry import (
    USER_VERSION,
    USER_VERSION_V2,
    RestoreRegistry,
    RestoreRegistryCorrupt,
    _expected_schema_rows,
)
from tianshu.workspace import WorkspaceBinding

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
STARTUP = str(ROOT / "tests" / "_startup")

# Every C scenario begins with a real managed import.
pytestmark = requires_managed_restore_writes


def _child_env(*, failpoint: str | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
    }
    for passthrough in (
        "TIANSHU_TEST_LOCK_ROOT",
        "PYTEST_TIANSHU_REGISTRY_HOME",
        "TIANSHU_AUTHORITY_ROOT",
    ):
        val = os.environ.get(passthrough)
        if val:
            env[passthrough] = val
    env["PYTHONPATH"] = os.pathsep.join([STARTUP, SRC, env.get("PYTHONPATH", "")])
    if failpoint is not None:
        env["TIANSHU_FAILPOINT"] = failpoint
    return env


def _cli(*args: object, failpoint: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tianshu", "data", *(str(a) for a in args)],
        env=_child_env(failpoint=failpoint),
        capture_output=True,
        timeout=45,
        **CHILD_TEXT,
    )


def _parse_resp_v2(raw: bytes | str) -> RestoreResponseV2:
    data = raw.rstrip(b"\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n").encode("utf-8")
    return RestoreResponseV2.from_bytes(data)


def _seed_clean_root(tmp_path: Path, name: str = "data_root") -> Path:
    root = (tmp_path / name).resolve()
    ws = (tmp_path / f"ws_{name}").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        stack.tasks._disk_admission = DiskAdmission(
            stack.tasks.disk_admission.context,
            probe=lambda _p: 1 << 62,
        )
        task = stack.tasks.create_task(
            "初始已完成任务",
            [WorkspaceBinding(str(ws), mode="writable")],
        )
        run_obj = stack.tasks.start_run(task.task_id)
        stack.tasks.transition(run_obj.run_id, "running", reason="seed")
        stack.tasks.finish_run(
            run_obj.run_id,
            outcome="succeeded",
            effect_state="none_applied",
            reason="done",
        )
    finally:
        log.close()
    return root


def _seed_dirty_inflight_root(tmp_path: Path, name: str = "dirty_root") -> tuple[Path, dict]:
    root = (tmp_path / name).resolve()
    ws = (tmp_path / f"ws_{name}").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    target_file = ws / "doc.txt"
    target_file.write_bytes(b"original\n")

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        stack.tasks._disk_admission = DiskAdmission(
            stack.tasks.disk_admission.context,
            probe=lambda _p: 1 << 62,
        )
        leases = LeaseStore(log)
        jobs = JobStore(log)
        ledger = BudgetLedger(log)

        task = stack.tasks.create_task(
            "崩溃前未决执行现场",
            [WorkspaceBinding(str(ws), mode="writable")],
            mcp_servers=["demo"],
            max_subtasks=2,
        )
        run_obj = stack.tasks.start_run(task.task_id)
        stack.tasks.transition(run_obj.run_id, "running", reason="start_inflight")

        # 1) 存活工作区租约
        leases.acquire([ws], run_obj.run_id, ttl_ms=600_000)

        # 2) 非终态作业（claimed）与非终态子任务（running）
        job = jobs.enqueue(run_obj.run_id)
        claim = jobs.claim(job.job_id, ttl_ms=600_000)
        assert claim.job_id == job.job_id

        child_task = stack.tasks.create_task("子任务目标", [])
        child_run = stack.tasks.start_run(child_task.task_id)
        stack.tasks.transition(child_run.run_id, "running", reason="sub_start")
        child_job = jobs.enqueue(child_run.run_id)
        log.conn.execute(
            "INSERT INTO subtasks ("
            "subtask_id, parent_task_id, parent_run_id, child_task_id, child_run_id, "
            "job_id, goal, limits, state, parent_goal_revision, parent_lease_epoch, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sub_alpha",
                task.task_id,
                run_obj.run_id,
                child_task.task_id,
                child_run.run_id,
                child_job.job_id,
                "子任务摘要",
                json.dumps({"max_turns": 2, "max_tokens": 512, "deadline_ms": 300000}),
                "running",
                run_obj.goal_revision,
                run_obj.lease_epoch,
                1_700_000_000_000,
            ),
        )

        # 3) 两张审批（pending / approved）+ 一张未消费 grant + 一条 waiting dispatch_request
        def _mk_req(req_id: str, call_id: str, tool_id: str, action: str, res_uri: str):
            args = {"path": str(target_file)}
            return ActionRequest(
                request_id=req_id,
                task_id=task.task_id,
                run_id=run_obj.run_id,
                job_id=claim.job_id,
                claim_epoch=claim.claim_epoch,
                owner_instance_id=claim.owner_instance_id,
                goal_revision=run_obj.goal_revision,
                lease_epoch=run_obj.lease_epoch,
                call_id=call_id,
                attempt_id=1,
                tool_id=tool_id,
                tool_version="1",
                schema_hash="a" * 64,
                action=action,
                resources=(res_uri,),
                args_hash=sha256_hex(canonical_bytes(args)),
            )

        req_pending = _mk_req("req_pend", "call_pend", "modify_text", "overwrite", str(target_file))
        app_pending = stack.approvals.request(
            req_pending,
            PolicyDecision(kind="approve", reason="待批覆盖", rule_id="c4.test_pend"),
        )

        req_approved = _mk_req("req_appr", "call_appr", "delete_file", "delete", str(target_file))
        app_approved = stack.approvals.request(
            req_approved,
            PolicyDecision(kind="approve", reason="已批未消费", rule_id="c4.test_appr"),
        )
        stack.approvals.decide(
            app_approved.approval_id,
            decision="approved",
            actor=HumanIdentity.local_cli(),
        )

        grant_req = _mk_req("req_grant", "call_unsent", "create_report", "create", str(ws / "n.md"))
        grant = stack.grants.issue(
            subject=f"{run_obj.run_id}#{claim.claim_epoch}",
            request=grant_req,
            issuer="policy:auto",
            ttl_ms=600_000,
        )

        log.conn.execute(
            "INSERT INTO dispatch_requests ("
            "request_id, task_id, run_id, request_digest, payload, args_payload, "
            "args_redacted, state, reason, rule_id, approval_id, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,0,'waiting','wait_human','c4.test_pend',?,?,?)",
            (
                req_pending.request_id,
                task.task_id,
                run_obj.run_id,
                req_pending.digest(),
                json.dumps(req_pending.to_canonical_dict()),
                "{}",
                app_pending.approval_id,
                1_700_000_000_000,
                1_700_000_000_000,
            ),
        )

        # 4) 三条未收尾 tool.intent：
        #    - call_unsent: boundary_protocol=1, state='intent'（未越界 -> none_applied）
        #    - call_exec_unk: run_command, state='executing'（未知副作用 -> unknown + 阻断 ws）
        #    - call_mcp_unk: mcp__demo__act, state='executing'（未知外发 -> 阻断 mcp://demo/act）
        for call_id, tool_id, action, target_str, w_root, r_key, d_state in (
            (
                "call_unsent",
                "create_report",
                "create",
                str(ws / "n.md"),
                str(ws),
                f"file:{ws / 'n.md'}",
                "intent",
            ),
            (
                "call_exec_unk",
                "run_command",
                "exec",
                str(ws),
                str(ws),
                f"dir:{ws}",
                "executing",
            ),
            (
                "call_mcp_unk",
                "mcp__demo__act",
                "fetch",
                "mcp://demo/act",
                "",
                "mcp:demo/act",
                "executing",
            ),
        ):
            args_obj = {"cmd": "echo hi"} if tool_id == "run_command" else {"x": 1}
            args_h = sha256_hex(canonical_bytes(args_obj))
            disp_id = f"disp_{call_id}"
            iseq = log.append(
                run_obj.run_id,
                "tool.intent",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "args": args_obj,
                    "args_hash": args_h,
                    "request_id": f"r_{call_id}",
                    "request_digest": "b" * 64,
                    "dispatch_id": disp_id,
                    "grant_id": grant.grant_id,
                    "approval_id": None,
                    "attempt_id": 1,
                    "claim_epoch": claim.claim_epoch,
                },
                task_id=task.task_id,
            )
            log.conn.execute(
                "INSERT INTO dispatches ("
                "dispatch_id, run_id, task_id, call_id, request_id, request_digest, "
                "tool_id, action, resources, target, rel_path, workspace_root, resource_key, "
                "approval_id, grant_id, claim_epoch, owner_instance_id, attempt_id, intent_seq, "
                "boundary_protocol, state, reason, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'',?,?)",
                (
                    disp_id,
                    run_obj.run_id,
                    task.task_id,
                    call_id,
                    f"r_{call_id}",
                    "b" * 64,
                    tool_id,
                    action,
                    json.dumps([target_str]),
                    target_str,
                    "",
                    w_root,
                    r_key,
                    None,
                    grant.grant_id,
                    claim.claim_epoch,
                    claim.owner_instance_id,
                    1,
                    iseq,
                    d_state,
                    1_700_000_000_000,
                    1_700_000_000_000,
                ),
            )

        # 5) 预算预占：一条绑定可证未发的 call_unsent，一条模型未知预占（不得按 0 退款）
        acc_id = f"acc_{run_obj.run_id}"
        ledger.create_account(
            acc_id,
            run_id=run_obj.run_id,
            limits={"tokens": 10_000, "requests": 10},
        )
        res_unsent = ledger.reserve(
            acc_id,
            attempt_id="att_unsent",
            need={"tokens": 200, "requests": 1},
            run_id=run_obj.run_id,
            call_id="call_unsent",
        )
        res_unknown = ledger.reserve(
            acc_id,
            attempt_id="att_model_inflight",
            need={"tokens": 500, "requests": 1},
            run_id=run_obj.run_id,
            call_id="",
        )
    finally:
        log.close()

    return root, {
        "ws": str(ws),
        "task_id": task.task_id,
        "run_id": run_obj.run_id,
        "child_run_id": child_run.run_id,
        "job_id": job.job_id,
        "child_job_id": child_job.job_id,
        "app_pending_id": app_pending.approval_id,
        "app_approved_id": app_approved.approval_id,
        "app_approved_action_digest": req_approved.action_digest(),
        "app_approved_request_digest": req_approved.digest(),
        "grant_id": grant.grant_id,
        "account_id": acc_id,
        "res_unsent_id": res_unsent.reservation_id,
        "res_unknown_id": res_unknown.reservation_id,
    }


def _import_to_published_hold(bdir: Path, root: Path) -> str:
    planned = restore_manager.plan(bdir, root)
    applied = restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    assert applied.phase == "published_hold"
    assert applied.held is True
    return applied.batch_id


# ======================================================================== C01


def test_c01_published_hold_review_to_release_preserves_immutable_origin_and_is_idempotent(
    tmp_path,
):
    """C01：published_hold -> restore-review -> restore-release 落库 ReleasedV1，origin 零变化。"""
    root = _seed_clean_root(tmp_path)
    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)

    batch_id = _import_to_published_hold(bdir, root)
    origin_path = root / ".restore-origin.json"
    origin_before = origin_path.read_bytes()
    origin_inode_before = observe_entity(origin_path, "file")

    rev_cli = _cli("restore-review", "--data-root", root, "--json")
    assert rev_cli.returncode == 0, rev_cli.stderr
    rev_resp = _parse_resp_v2(rev_cli.stdout.encode("utf-8"))
    assert rev_resp.ok is True
    review_sha = rev_resp.result.review_sha256

    rel_cli = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        review_sha,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert rel_cli.returncode == 0, rel_cli.stderr
    rel_resp = _parse_resp_v2(rel_cli.stdout.encode("utf-8"))
    assert rel_resp.ok is True
    assert rel_resp.result.batch_id == batch_id
    assert rel_resp.result.phase == "released"
    assert rel_resp.result.held is False
    assert rel_resp.result.review_sha256 == review_sha

    # .restore-origin.json 一次写入永不改写、永不删除
    assert origin_path.read_bytes() == origin_before
    assert observe_entity(origin_path, "file") == origin_inode_before

    obs = RestoreRegistry().observe()
    validate_event_chain_v2(obs.events)
    assert [e.kind for e in obs.events if e.batch_id == batch_id] == [
        "reserved",
        "stage_owned",
        "sealed_hold",
        "publish_intent",
        "published_hold",
        "release_intent",
        "released",
    ]

    # 同参数二次重入幂等返回同一结果
    rel_cli_2 = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        review_sha,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert rel_cli_2.returncode == 0
    assert _parse_resp_v2(rel_cli_2.stdout.encode("utf-8")).result == rel_resp.result


# ======================================================================== C02


def test_c02_release_preconditions_reject_zero_write_before_release_intent(tmp_path, monkeypatch):
    """C02：缺确认参数、expect_review 错 1 bit、未完整复核、字节/inode 篡改均退出 2 且零写。"""
    root = _seed_clean_root(tmp_path)
    # 写入一个受管材料文件以供篡改测试
    art_dir = root / "artifacts" / "blobs"
    art_dir.mkdir(parents=True, exist_ok=True)
    art_blob = art_dir / ("a" * 64)
    art_blob.write_bytes(b"artifact-bytes")

    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)

    batch_id = _import_to_published_hold(bdir, root)
    rev = restore_manager.review(root)
    review_sha = rev.review_sha256
    reg = RestoreRegistry()
    head_before = reg.observe().head_sequence

    # 1) 缺少 --acknowledge-unproven-later-history -> 退出 2 且零写
    no_ack = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        review_sha,
        "--json",
    )
    assert no_ack.returncode == 2
    assert _parse_resp_v2(no_ack.stdout.encode("utf-8")).error.category == "incompatible"
    assert reg.observe().head_sequence == head_before

    # 2) expect_review 错 1 hex 位 -> 退出 2 (binding_mismatch) 且零写
    flipped = ("0" if review_sha[0] != "0" else "1") + review_sha[1:]
    bad_rev = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        flipped,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert bad_rev.returncode == 2
    assert _parse_resp_v2(bad_rev.stdout.encode("utf-8")).error.category == "binding_mismatch"
    assert reg.observe().head_sequence == head_before

    # 2b) review_complete == False -> 退出 2 (incompatible) 且零写
    orig_compute = restore_manager._compute_review_locked
    holder: dict[str, str] = {}

    def _incomplete_review(*a, **kw):
        real = orig_compute(*a, **kw)
        red = replace(
            real.to_redacted_v1(),
            review_complete=False,
            unverifiable=("simulated_unverifiable",),
        )
        inc_sha = domain_digest("review", red)
        holder["sha"] = inc_sha
        return replace(
            real,
            review_complete=False,
            unverifiable=("simulated_unverifiable",),
            review_sha256=inc_sha,
        )

    monkeypatch.setattr(restore_manager, "_compute_review_locked", _incomplete_review)
    inc_preview = restore_manager._compute_review_locked(
        batch_id,
        reg.observe().events[0].payload,
        next(e for e in reg.observe().events if e.kind == "sealed_hold").payload,
        (root / ".restore-origin.json").read_bytes(),
        later_backup=None,
        normalized_scopes=(),
        seen_scopes=set(),
    )
    with pytest.raises(restore_manager.RestoreOperationError) as inc_exc:
        restore_manager.release(
            root,
            expect_review_sha256=inc_preview.review_sha256,
            acknowledge_unproven_later_history=True,
        )
    assert inc_exc.value.failure.category == "incompatible"
    assert reg.observe().head_sequence == head_before
    monkeypatch.setattr(restore_manager, "_compute_review_locked", orig_compute)

    # 3) 篡改恢复根内的材料字节 -> 退出 2 且零写（不进入 ReleaseIntentV1）
    restored_blob = root / "artifacts" / "blobs" / ("a" * 64)
    orig_blob_bytes = restored_blob.read_bytes()
    restored_blob.write_bytes(b"corrupted-bytes")
    tampered = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        review_sha,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert tampered.returncode == 2
    assert _parse_resp_v2(tampered.stdout.encode("utf-8")).error.category == "binding_mismatch"
    assert reg.observe().head_sequence == head_before
    restored_blob.write_bytes(orig_blob_bytes)

    # 4) 替换主库 events.db 的 inode（即便字节完全相同） -> 退出 2 且零写
    db_file = root / "events.db"
    db_bytes = db_file.read_bytes()
    db_Replacement = root / "events.db.swapped"
    db_Replacement.write_bytes(db_bytes)
    os.replace(db_Replacement, db_file)
    inode_tampered = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        review_sha,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert inode_tampered.returncode == 2
    parsed_tampered = _parse_resp_v2(inode_tampered.stdout.encode("utf-8"))
    assert parsed_tampered.error.category == "binding_mismatch"
    assert reg.observe().head_sequence == head_before
    assert [e.kind for e in reg.observe().events if e.batch_id == batch_id][-1] == "published_hold"


# ======================================================================== C03


def test_c03_v1_registry_atomically_upgrades_in_place_to_v2_preserving_history_bytes_and_inode(
    tmp_path,
):
    """C03：既有 v1 RestoreRegistry 在首次 restore-release 时原地升至 v2，历史字节与 inode 不变。"""
    # 批次 1：建立 v1 注册表并中止一条历史批次
    root1 = _seed_clean_root(tmp_path, "root_hist")
    b1 = tmp_path / "b_hist"
    create_backup(root1, b1)
    shutil.rmtree(root1)
    p1 = restore_manager.plan(b1, root1)
    crashed = _cli(
        "restore-apply",
        b1,
        "--data-root",
        root1,
        "--expect-plan",
        p1.plan_sha256,
        failpoint="FP-STAGE",
    )
    assert crashed.returncode == 77
    restore_manager.abort(root1)

    # 批次 2：推进至 published_hold（仍全部为 v1 7 阶段事件，USER_VERSION == 1）
    root2 = _seed_clean_root(tmp_path, "root_rel")
    b2 = tmp_path / "b_rel"
    create_backup(root2, b2)
    shutil.rmtree(root2)
    _import_to_published_hold(b2, root2)

    reg = RestoreRegistry()
    reg_inode_before = observe_entity(reg.path, "file")
    conn_before = sqlite3.connect(reg.path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert conn_before.execute("PRAGMA user_version").fetchone()[0] == USER_VERSION
        meta_before = list(conn_before.execute("SELECT * FROM registry_meta"))
        rows_before = list(
            conn_before.execute(
                "SELECT sequence, registry_id, batch_id, kind, previous_sha256, "
                "event_sha256, event_bytes FROM restore_events ORDER BY sequence ASC"
            )
        )
    finally:
        conn_before.close()
    assert len(rows_before) >= 7

    rev = restore_manager.review(root2)
    res = restore_manager.release(
        root2,
        expect_review_sha256=rev.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert res.phase == "released"

    # 验证原地无损升级：文件实体身份不变、USER_VERSION == 2、DDL 逐字匹配、历史行逐字节相等
    assert observe_entity(reg.path, "file") == reg_inode_before
    conn_after = sqlite3.connect(reg.path.resolve().as_uri() + "?mode=ro", uri=True)
    conn_after.row_factory = sqlite3.Row
    try:
        assert conn_after.execute("PRAGMA user_version").fetchone()[0] == USER_VERSION_V2
        actual_ddl = tuple(
            (r["name"], r["type"], r["tbl_name"], r["sql"])
            for r in conn_after.execute(
                "SELECT name,type,tbl_name,sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        assert actual_ddl == _expected_schema_rows(USER_VERSION_V2)
        meta_after = [tuple(r) for r in conn_after.execute("SELECT * FROM registry_meta")]
        assert meta_after == meta_before
        rows_after = [
            tuple(r)
            for r in conn_after.execute(
                "SELECT sequence, registry_id, batch_id, kind, previous_sha256, "
                "event_sha256, event_bytes FROM restore_events ORDER BY sequence ASC"
            )
        ]
        assert rows_after[: len(rows_before)] == rows_before
        assert len(rows_after) == len(rows_before) + 2
    finally:
        conn_after.close()


# ======================================================================== C04


def test_c04_deterministic_reconciliation_fences_runs_expires_approvals_and_blocks_unknown(
    tmp_path,
):
    """C04：非终态收口、旧审批失效、租约清空、未发归 none_applied、未知保留并登记阻断。"""
    root, meta = _seed_dirty_inflight_root(tmp_path)
    bdir = tmp_path / "backup_dirty"
    create_backup(root, bdir)
    shutil.rmtree(root)

    _import_to_published_hold(bdir, root)
    rev = restore_manager.review(root)
    rel = restore_manager.release(
        root,
        expect_review_sha256=rev.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert rel.phase == "released"
    assert rel.reconciled_counts.fenced_runs == 2
    assert rel.reconciled_counts.expired_approvals == 2
    assert rel.reconciled_counts.cleared_leases == 1
    assert rel.reconciled_counts.resolved_none_applied_intents == 1
    assert rel.reconciled_counts.preserved_unknown_intents == 2
    assert rel.reconciled_counts.cancelled_jobs == 2

    # 解除后用真实 EventLog + build_stack 验证所有投影与阻断状态
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        ledger = BudgetLedger(log)

        # 1) 非终态 runs 全 fenced，tasks.active_run_id 已清空
        main_run = stack.tasks.get_run(meta["run_id"])
        child_run = stack.tasks.get_run(meta["child_run_id"])
        assert main_run is not None and main_run.state == "fenced"
        assert main_run.outcome == "cancelled"
        assert main_run.effect_state == "unknown"
        assert child_run is not None and child_run.state == "fenced"
        main_task = stack.tasks.get_task(meta["task_id"])
        assert main_task is not None and main_task.active_run_id is None

        # 2) 旧审批全部 expired 且不可再消费；grants 已清零；dispatch_requests 已 rejected
        for aid in (meta["app_pending_id"], meta["app_approved_id"]):
            app = stack.approvals.get(aid)
            assert app is not None and app.state == "expired"
        with pytest.raises(ApprovalError):
            stack.approvals.consume(
                meta["app_approved_id"],
                action_digest=meta["app_approved_action_digest"],
                request_digest=meta["app_approved_request_digest"],
            )
        grant_obj = stack.grants.get(meta["grant_id"])
        assert grant_obj is not None and grant_obj.remaining_uses == 0
        req_state = log.conn.execute(
            "SELECT state FROM dispatch_requests WHERE request_id='req_pend'"
        ).fetchone()[0]
        assert req_state == "rejected"

        # 3) 工作区租约清空，jobs 全部 cancelled，subtasks 全部 fenced
        assert log.conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        for jid in (meta["job_id"], meta["child_job_id"]):
            j_state = log.conn.execute(
                "SELECT state, outcome FROM jobs WHERE job_id=?", (jid,)
            ).fetchone()
            assert (j_state["state"], j_state["outcome"]) == ("cancelled", "cancelled")
        sub_row = log.conn.execute(
            "SELECT state, error FROM subtasks WHERE subtask_id='sub_alpha'"
        ).fetchone()
        assert (sub_row["state"], sub_row["error"]) == ("fenced", "restore.release_reconciled")

        # 4) 未决 tool.intent 精确区分为 none_applied vs unknown，零重放！
        run_events = log.read_run(meta["run_id"])
        tool_results = {
            e.payload["call_id"]: e.payload for e in run_events if e.type == "tool.result"
        }
        assert tool_results["call_unsent"]["effect_state"] == "none_applied"
        assert tool_results["call_unsent"]["status"] == "failed"
        assert tool_results["call_exec_unk"]["effect_state"] == "unknown"
        assert tool_results["call_exec_unk"]["status"] == "unknown"
        assert tool_results["call_mcp_unk"]["effect_state"] == "unknown"
        assert tool_results["call_mcp_unk"]["status"] == "unknown"
        assert not (Path(meta["ws"]) / "n.md").exists()

        # 5) 预算账本：可证未发的预占已释放，未知预占保留占用绝不按 0 退款！
        r_unsent = ledger.get(meta["res_unsent_id"])
        r_unk = ledger.get(meta["res_unknown_id"])
        assert r_unsent is not None and r_unsent.state == "released"
        assert r_unk is not None and r_unk.state == "unknown"
        usage_report = ledger.usage(meta["account_id"])
        assert usage_report["reserved"]["tokens"] == 500
        assert usage_report["unknown_reservations"] == 1

        # 6) 未知副作用关联的工作区与 MCP 对象在日志及本机 authority 双重阻断生效！
        with pytest.raises(WorkspaceBlocked):
            ensure_not_blocked(log, meta["ws"])
        with pytest.raises(McpObjectBlocked):
            ensure_object_not_blocked(log, ("mcp://demo/act", "mcp://demo"))
        assert authority.is_blocked(meta["ws"]) is True
        assert authority.is_object_blocked("mcp://demo/act") is True
    finally:
        log.close()


@pytest.mark.parametrize(
    "history", ["missing", "earlier_unblock", "later_unblock", "already_blocked", "r2_commit_crash"]
)
def test_c04_release_blocks_existing_unknown_results_without_rewriting_them(tmp_path, history):
    root, meta = _seed_dirty_inflight_root(tmp_path)
    db = root / "events.db"
    run_id, ws, obj = meta["run_id"], meta["ws"], "mcp://demo/act"
    log = EventLog(db, blob_dir=root / "blobs")
    try:
        if history == "earlier_unblock":
            unblock(log, ws, reason="checked earlier activity")
            unblock_object(log, obj, reason="checked earlier activity")
        for call_id, tool_id in (
            ("call_exec_unk", "run_command"),
            ("call_mcp_unk", "mcp__demo__act"),
        ):
            log.append(
                run_id,
                "tool.result",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "status": "unknown",
                    "effect_state": "unknown",
                    "output": {"recovered": True, "error": "interrupted old recovery"},
                },
                task_id=meta["task_id"],
                idem_key=call_id,
            )
        if history == "later_unblock":
            unblock(log, ws, reason="checked this unknown outcome")
            unblock_object(log, obj, reason="checked this unknown outcome")
        elif history == "already_blocked":
            log.append(
                run_id,
                "recovery.blocked",
                {
                    "workspace": ws,
                    "call_ids": ["call_exec_unk"],
                    "reason": "old block",
                },
                actor_kind="system",
            )
            log.append(
                run_id,
                "recovery.object_blocked",
                {
                    "object": obj,
                    "call_ids": ["call_mcp_unk"],
                    "reason": "old block",
                },
                actor_kind="system",
            )
        pre_head = log.seq_head()
        events_before = [
            tuple(row) for row in log.conn.execute("SELECT * FROM events ORDER BY seq")
        ]
        versions_before = [
            tuple(row) for row in log.conn.execute("SELECT * FROM event_versions ORDER BY seq")
        ]
    finally:
        log.close()
    backup = tmp_path / "backup"
    create_backup(root, backup)
    verified = verify_backup(backup)
    assert verified.ok, verified.problems
    shutil.rmtree(root)
    batch = _import_to_published_hold(backup, root)
    reviewed = _cli("restore-review", "--data-root", root, "--json")
    assert reviewed.returncode == 0, (reviewed.stdout, reviewed.stderr)
    review_sha = _parse_resp_v2(reviewed.stdout).result.review_sha256
    args = (
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        review_sha,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert not authority.is_blocked(ws)
    assert not authority.is_object_blocked(obj)
    if history == "r2_commit_crash":
        fp = "FP-RELEASE-RECONCILE-COMMITTED"
        crashed = _cli(*args, failpoint=fp)
        assert crashed.returncode == 77 and f"[failpoint] {fp}\n" in crashed.stderr
        with pytest.raises(RestoreScopeBusy):
            EventLog(db, blob_dir=root / "blobs")
        assert not authority.is_blocked(ws)
        assert not authority.is_object_blocked(obj)
        resumed = _cli(*args, failpoint="FP-RELEASE-RECONCILE")
        assert resumed.returncode == 77 and "[failpoint] FP-RELEASE-RECONCILE\n" in resumed.stderr
        with pytest.raises(RestoreScopeBusy):
            EventLog(db, blob_dir=root / "blobs")
        assert authority.is_blocked(ws)
        assert authority.is_object_blocked(obj)

    released = _cli(*args)
    assert released.returncode == 0, (released.stdout, released.stderr)
    result = _parse_resp_v2(released.stdout).result
    assert result.phase == "released" and result.batch_id == batch
    assert result.reconciled_counts.preserved_unknown_intents == 0
    assert result.reconciled_counts.resolved_none_applied_intents == 1
    blocked = history != "later_unblock"
    log = EventLog(db, blob_dir=root / "blobs")
    try:
        assert [
            tuple(row)
            for row in log.conn.execute(
                "SELECT * FROM events WHERE seq <= ? ORDER BY seq",
                (pre_head,),
            )
        ] == events_before
        assert [
            tuple(row)
            for row in log.conn.execute(
                "SELECT * FROM event_versions WHERE seq <= ? ORDER BY seq",
                (pre_head,),
            )
        ] == versions_before
        results = [e.payload for e in log.read_types(["tool.result"]) if e.seq > pre_head]
        assert [(p["call_id"], p["effect_state"]) for p in results] == [
            ("call_unsent", "none_applied")
        ]
        for kind, field, target, call in (
            ("recovery.blocked", "workspace", ws, "call_exec_unk"),
            ("recovery.object_blocked", "object", obj, "call_mcp_unk"),
        ):
            events = log.read_types([kind])
            assert [(e.run_id, e.payload[field], e.payload["call_ids"]) for e in events] == (
                [(run_id, target, [call])] if blocked else []
            )
        assert scan_blocked(log).blocked == ({ws} if blocked else set())
        assert scan_blocked_objects(log).blocked == ({obj} if blocked else set())
        assert authority.is_blocked(ws) == blocked
        assert authority.is_object_blocked(obj) == blocked
        post_head = log.seq_head()
    finally:
        log.close()

    release_events = [e.kind for e in RestoreRegistry().observe().events if e.batch_id == batch]
    retried = _cli(*args, failpoint="FP-RELEASE-RECONCILE-COMMITTED")
    assert retried.returncode == 0, (retried.stdout, retried.stderr)
    assert _parse_resp_v2(retried.stdout).result == result
    assert [
        e.kind for e in RestoreRegistry().observe().events if e.batch_id == batch
    ] == release_events
    assert release_events[-2:] == ["release_intent", "released"]
    log = EventLog(db, blob_dir=root / "blobs")
    try:
        assert log.seq_head() == post_head
    finally:
        log.close()


# ======================================================================== C05


@pytest.mark.parametrize(
    "fp",
    [
        "FP-RELEASE-INTENT-BEFORE",
        "FP-RELEASE-INTENT",
        "FP-RELEASE-RECONCILE-BEFORE",
        "FP-RELEASE-RECONCILE-COMMITTED",
        "FP-RELEASE-RECONCILE",
        "FP-RELEASED-BEFORE",
        "FP-RELEASED",
    ],
)
def test_c05_release_failpoints_exit_77_block_midflight_and_converge_without_duplicate_events(
    tmp_path, fp
):
    """C05：各窗口 os._exit(77) 硬退 -> 未解除前普通打开被拒 -> 重入幂等零重复事件。"""
    root, meta = _seed_dirty_inflight_root(tmp_path, name=f"r_{fp.lower().replace('-', '_')}")
    bdir = tmp_path / f"b_{fp.lower().replace('-', '_')}"
    create_backup(root, bdir)
    shutil.rmtree(root)

    batch_id = _import_to_published_hold(bdir, root)
    rev = restore_manager.review(root)

    crashed = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        rev.review_sha256,
        "--acknowledge-unproven-later-history",
        failpoint=fp,
    )
    assert crashed.returncode == 77, crashed.stderr
    assert f"[failpoint] {fp}\n" in crashed.stderr

    registry = RestoreRegistry()
    observed = [event for event in registry.observe().events if event.batch_id == batch_id]
    if fp == "FP-RELEASE-RECONCILE-BEFORE":
        assert (root / "events.db-journal").stat().st_size > 0
    if fp != "FP-RELEASED":
        with pytest.raises(RestoreScopeBusy):
            EventLog(root / "events.db", blob_dir=root / "blobs")
        if fp != "FP-RELEASE-INTENT-BEFORE":
            assert observed[-1].kind == "release_intent"
            assert _cli("restore-abort", "--data-root", root, "--json").returncode == 2
            assert _cli("restore-resume", "--data-root", root, "--json").returncode == 2
    else:
        assert observed[-1].kind == "released"

    retry = _cli(
        "restore-release",
        "--data-root",
        root,
        "--expect-review",
        rev.review_sha256,
        "--acknowledge-unproven-later-history",
        "--json",
    )
    assert retry.returncode == 0, retry.stderr
    retry_resp = _parse_resp_v2(retry.stdout.encode("utf-8"))
    assert retry_resp.ok is True
    assert retry_resp.result.phase == "released"
    released = [event for event in registry.observe().events if event.batch_id == batch_id]
    assert [event.kind for event in released].count("release_intent") == 1
    assert [event.kind for event in released].count("released") == 1

    # 验证无重复对账事件
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        fenced_evs = [e for e in log.read_types(["run.fenced"]) if e.run_id == meta["run_id"]]
        assert len(fenced_evs) == 1
        exp_evs = log.read_types(["approval.expired"])
        assert len(exp_evs) == 2
        res_evs = [
            e
            for e in log.read_run(meta["run_id"])
            if e.type == "tool.result" and e.payload["call_id"] == "call_unsent"
        ]
        assert len(res_evs) == 1
    finally:
        log.close()


def test_c05_reentry_after_no_tail_r2_commit_replaces_inherited_proof(tmp_path, monkeypatch):
    source = _seed_clean_root(tmp_path)
    bdir = tmp_path / "first_backup"
    create_backup(source, bdir)
    shutil.rmtree(source)
    _import_to_published_hold(bdir, source)
    first_review = restore_manager.review(source)
    first_release = restore_manager.release(
        source, expect_review_sha256=first_review.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert first_release.phase == "released"
    first_registry = RestoreRegistry()
    first_observation = first_registry.observe()

    # 日常写入使旧批次见证过时；合法备份仍会携带这一行。
    _seed_clean_root(tmp_path)
    second_backup = tmp_path / "second_backup"
    create_backup(source, second_backup)
    backup_db_uri = (second_backup / "events.db").as_uri() + "?mode=ro&immutable=1"
    ro_conn = sqlite3.connect(backup_db_uri, uri=True)
    try:
        prior_proof = ro_conn.execute(
            "SELECT release_intent_sha256 FROM _restore_release_r2_proof WHERE singleton=1"
        ).fetchone()
        assert prior_proof == (domain_digest("event", first_observation.events[-2]),)
    finally:
        ro_conn.close()

    # 模拟原路径的独立安装，新 Home 使用已有 pytest 隔离钩子。
    # 旧 Home 事实和原导入实体都保留，旧 Home 的实体准入继续适用。
    source.rename(tmp_path / "first_restore_kept")
    second_home = tmp_path / "second_home"
    second_home.mkdir()
    monkeypatch.setenv("PYTEST_TIANSHU_REGISTRY_HOME", str(second_home))
    monkeypatch.setattr(restore_registry, "_user_home", lambda: second_home)
    root = source
    batch_id = _import_to_published_hold(second_backup, root)
    review = restore_manager.review(root)
    args = (
        "restore-release", "--data-root", root, "--expect-review", review.review_sha256,
        "--acknowledge-unproven-later-history", "--json",
    )
    db = root / "events.db"
    pre_sha = sha256_file(db)
    fp = "FP-RELEASE-RECONCILE-COMMITTED"
    crashed = _cli(*args, failpoint=fp)
    assert crashed.returncode == 77, (crashed.stdout, crashed.stderr)
    assert f"[failpoint] {fp}\n" in crashed.stderr
    assert sha256_file(db) != pre_sha
    registry = RestoreRegistry()
    intent = registry.observe().events[-1]
    assert intent.batch_id == batch_id and intent.kind == "release_intent"
    with pytest.raises(RestoreScopeBusy):
        EventLog(db, blob_dir=root / "blobs")

    # 拉起仅在新 R2 写事务提交后触发的同一个故障点。已提交态必须跳过它。
    retry = _cli(*args, failpoint=fp)
    assert retry.returncode == 0, (retry.stdout, retry.stderr)
    assert _parse_resp_v2(retry.stdout).result.phase == "released"
    history = [event for event in registry.observe().events if event.batch_id == batch_id]
    assert [event.kind for event in history[-2:]] == ["release_intent", "released"]
    pre_head = intent.payload.pre_release_event_seq_head
    assert history[-1].payload.post_release_event_seq_head == pre_head
    assert first_registry.observe() == first_observation


@pytest.mark.parametrize(
    "drift",
    ["grant", "budget_counter", "tail_event_version", "prefix_version", "missing_proof", "intent"],
)
def test_c05_reentry_refuses_third_state_without_writes(tmp_path, drift):
    root, meta = _seed_dirty_inflight_root(tmp_path)
    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)
    _import_to_published_hold(bdir, root)
    review = restore_manager.review(root)
    args = (
        "restore-release", "--data-root", root, "--expect-review", review.review_sha256,
        "--acknowledge-unproven-later-history", "--json",
    )
    crashed = _cli(*args, failpoint="FP-RELEASE-RECONCILE")
    assert crashed.returncode == 77, crashed.stderr
    registry = RestoreRegistry()
    intent = registry.observe()
    assert intent.present and intent.events[-1].kind == "release_intent"
    pre_head = intent.events[-1].payload.pre_release_event_seq_head

    db = root / "events.db"
    conn = sqlite3.connect(db)
    try:
        # 场景损伤限 pytest 临时夹具；不要 UPDATE events 或拆 append-only 触发器。
        if drift == "grant":
            cur = conn.execute(
                "UPDATE grants SET remaining_uses=1 WHERE grant_id=? AND remaining_uses=0",
                (meta["grant_id"],),
            )
        elif drift == "budget_counter":
            cur = conn.execute(
                "UPDATE budget_counters SET reserved=reserved-1 "
                "WHERE account_id=? AND dimension='tokens' AND reserved=500",
                (meta["account_id"],),
            )
        elif drift == "missing_proof":
            cur = conn.execute("DELETE FROM _restore_release_r2_proof WHERE singleton=1")
        elif drift == "intent":
            cur = conn.execute(
                "UPDATE _restore_release_r2_proof SET release_intent_sha256=? WHERE singleton=1",
                ("0" * 64,),
            )
        else:
            seq = pre_head + 1 if drift == "tail_event_version" else 1
            cur = conn.execute("UPDATE event_versions SET version=version+1 WHERE seq=?", (seq,))
        assert cur.rowcount == 1
        conn.commit()
        tail = list(conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (pre_head,)))
        assert tail
    finally:
        conn.close()

    db_sha, registry_sha = sha256_file(db), sha256_file(registry.path)
    authority_root = Path(os.environ["TIANSHU_AUTHORITY_ROOT"])
    assert authority_root.is_relative_to(tmp_path)
    authority_bytes = {p.name: p.read_bytes() for p in authority_root.iterdir() if p.is_file()}
    assert authority_bytes
    with pytest.raises(RestoreScopeBusy):
        EventLog(db, blob_dir=root / "blobs")

    retry = _cli(*args)
    assert retry.returncode == 2, (retry.stdout, retry.stderr)
    refused = _parse_resp_v2(retry.stdout)
    assert not refused.ok
    expected_category = "binding_mismatch" if drift == "intent" else "record_corrupt"
    assert refused.error.category == expected_category
    assert registry.observe() == intent  # 没有 ReleasedV1，也没有新的恢复事实。
    assert (sha256_file(db), sha256_file(registry.path)) == (db_sha, registry_sha)
    assert {
        p.name: p.read_bytes() for p in authority_root.iterdir() if p.is_file()
    } == authority_bytes
    with pytest.raises(RestoreScopeBusy):
        EventLog(db, blob_dir=root / "blobs")
    ro_conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        current_tail = list(
            ro_conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (pre_head,))
        )
        assert current_tail == tail
    finally:
        ro_conn.close()


# ======================================================================== C06


def test_c06_post_release_v2_admitted_by_entity_proof_and_copied_inode_refused(tmp_path):
    """C06（v2）：v2 库无 .migration.json 凭 (S, D) ReleasedV1 放行；复制新 inode 必拒。"""
    root = _seed_clean_root(tmp_path, "v2_src")
    b0 = tmp_path / "b0_for_mig"
    create_backup(root, b0)
    mig_report = migrate.migrate(root, backup=b0)
    assert mig_report.to_version == 2
    assert (root / "events.db.migration.json").is_file()

    # 生成 v2 备份（按契约不含 .migration.json）并销毁原根（含 .migration.json）
    b_v2 = tmp_path / "b_v2"
    create_backup(root, b_v2)
    assert not (b_v2 / "events.db.migration.json").exists()
    shutil.rmtree(root)

    _import_to_published_hold(b_v2, root)
    rev = restore_manager.review(root)
    rel = restore_manager.release(
        root,
        expect_review_sha256=rev.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert rel.db_user_version == 2
    assert rel.migration_required is False
    assert rel.post_release_admission == "admitted_v2_released_proof"
    assert rel.next_commands == ()
    # 绝不伪造假的 .migration.json
    assert not (root / "events.db.migration.json").exists()

    # 正控：凭 (S, D) 实体绑定的 ReleasedV1 证明正常打开 EventLog 并持续追加新业务事件 + 新备份
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        new_task = stack.tasks.create_task("解除后新增业务任务", [])
        assert new_task.task_id
    finally:
        log.close()

    # 主库字节因新业务写入合法推进后，二次打开仍凭不可变 (S, D) 实体身份正常放行
    log2 = EventLog(root / "events.db", blob_dir=root / "blobs")
    log2.close()
    post_bkp = tmp_path / "post_release_backup"
    bkp_cli = _cli("backup", "--data-root", root, "--dest", post_bkp, "--json")
    assert bkp_cli.returncode == 0, bkp_cli.stderr

    # 负控 1：复制同字节 v2 events.db 到新目录/新 inode（无 ReleasedV1）-> migration_incomplete
    copied_dir = tmp_path / "copied_unmanaged_v2"
    copied_dir.mkdir(parents=True, exist_ok=True)
    (copied_dir / "events.db").write_bytes((root / "events.db").read_bytes())
    with pytest.raises(AdmissionRefused) as exc_copy:
        EventLog(copied_dir / "events.db")
    assert exc_copy.value.code == "migration_incomplete"

    # 负控 2：连同 .restore-origin.json 复制到新 inode -> 实体身份不符，RestoreRegistryCorrupt
    copied_with_origin = tmp_path / "copied_with_origin_v2"
    copied_with_origin.mkdir(parents=True, exist_ok=True)
    (copied_with_origin / "events.db").write_bytes((root / "events.db").read_bytes())
    (copied_with_origin / ".restore-origin.json").write_bytes(
        (root / ".restore-origin.json").read_bytes()
    )
    with pytest.raises(RestoreRegistryCorrupt):
        EventLog(copied_with_origin / "events.db")

    # 负控 3：在原已解除根内将 events.db 替换为同字节新 inode -> RestoreRegistryCorrupt
    orig_db_bytes = (root / "events.db").read_bytes()
    swapped = root / "events.db.new_inode"
    swapped.write_bytes(orig_db_bytes)
    os.replace(swapped, root / "events.db")
    with pytest.raises(RestoreRegistryCorrupt):
        EventLog(root / "events.db")


def test_c06_post_release_v1_still_requires_explicit_backup_and_migrate_before_eventlog_open(
    tmp_path,
):
    """C06（v1）：v1 库在 released 后直接打开仍被拒，须经 data backup + migrate 后放行。"""
    root = _seed_clean_root(tmp_path, "v1_src")
    # 将源库推进到合法的 v1 中间机制版本（step=1）并生成备份
    conn = sqlite3.connect(str(root / "events.db"), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        migrate.STEPS[0].run(conn)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO migration_log(step, applied_at) VALUES (1, 1700000000000)")
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()

    b_v1 = tmp_path / "b_v1"
    create_backup(root, b_v1)
    shutil.rmtree(root)

    _import_to_published_hold(b_v1, root)
    rev = restore_manager.review(root)
    rel = restore_manager.release(
        root,
        expect_review_sha256=rev.review_sha256,
        acknowledge_unproven_later_history=True,
    )
    assert rel.db_user_version == 1
    assert rel.migration_required is True
    assert rel.post_release_admission == "requires_explicit_migrate_v1"
    assert len(rel.next_commands) == 2

    # 负控：即使已有 ReleasedV1，v1 中间版本打开 EventLog 仍以 migration_incomplete 拒绝
    with pytest.raises(AdmissionRefused) as exc_v1:
        EventLog(root / "events.db", blob_dir=root / "blobs")
    assert exc_v1.value.code == "migration_incomplete"
    assert sha256_file(root / ".restore-origin.json")

    # 正控：按 next_commands 执行 tianshu data backup + tianshu data migrate 升至 v2 后正常打开！
    pre_mig_bkp = tmp_path / "pre_mig_v1_bkp"
    bkp_res = _cli("backup", "--data-root", root, "--dest", pre_mig_bkp, "--json")
    assert bkp_res.returncode == 0, bkp_res.stderr

    mig_res = _cli("migrate", "--data-root", root, "--backup", pre_mig_bkp, "--json")
    assert mig_res.returncode == 0, mig_res.stderr
    assert (root / "events.db.migration.json").is_file()

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        t = stack.tasks.create_task("v1 显式迁移至 v2 后正常服务", [])
        assert t.task_id
    finally:
        log.close()
