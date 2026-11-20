"""恢复 B 完整产品执行链验收（B01–B09）：受管导入、持久隔离、只读复核、故障恢复与中止。"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from restore_test_support import requires_managed_restore_writes
from support import CHILD_TEXT

from tianshu import (
    backup,
    daemon,
    diagnostics_export,
    event_schema,
    host,
    memory_control,
    migrate,
    restore_manager,
    summaries,
)
from tianshu.backup import create_backup, verify_backup
from tianshu.contents import ContentStore
from tianshu.data_state import AdmissionRefused
from tianshu.disk_admission import DiskAdmission
from tianshu.events import EventLog
from tianshu.exec import LOCAL_EXEC_BACKEND, spawn
from tianshu.execpolicy import ExecPolicy
from tianshu.loop import build_stack
from tianshu.maintenance import acquire_restore_gate
from tianshu.memory import MemoryProjection
from tianshu.permissions import ActionRequest, PolicyEngine
from tianshu.platform_identity import observe_entity
from tianshu.resources import MemoryScope
from tianshu.restore_access import RestoreScopeBusy
from tianshu.restore_contract import (
    BodySelectorV1,
    DirRefV1,
    EntityRefV1,
    NativePathCodecV1,
    OriginV1,
    RegistryMetaV1,
    RestoreResponseV1,
    RestoreResponseV2,
    SqlValueV1,
    domain_digest,
    validate_event_chain,
)
from tianshu.restore_registry import RestoreRegistry, RestoreRegistryCorrupt
from tianshu.snapshot import SnapshotStore
from tianshu.summaries import MaterialBlock, SummaryStore
from tianshu.undo_journal import ACTION_RESTORE, JournalItem, JournalStore

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
STARTUP = str(ROOT / "tests" / "_startup")


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


def _parse_resp_v1(raw: bytes | str) -> RestoreResponseV1:
    data = raw.rstrip(b"\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n").encode("utf-8")
    return RestoreResponseV1.from_bytes(data)


def _parse_resp_v2(raw: bytes | str) -> RestoreResponseV2:
    data = raw.rstrip(b"\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n").encode("utf-8")
    return RestoreResponseV2.from_bytes(data)


def _tree_snapshot(root: Path) -> dict[str, tuple[str, str]]:
    if not root.exists() and not root.is_symlink():
        return {}
    state: dict[str, tuple[str, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        cur = Path(dirpath)
        rel_dir = "." if cur == root else cur.relative_to(root).as_posix()
        state[rel_dir] = ("dir", "")
        dirnames.sort()
        for fname in sorted(filenames):
            fpath = cur / fname
            rel_f = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            if fpath.is_symlink():
                state[rel_f] = ("symlink", os.readlink(fpath))
            else:
                state[rel_f] = ("file", hashlib.sha256(fpath.read_bytes()).hexdigest())
    return state


def _make_rich_root(
    tmp_path: Path,
    name: str = "data",
    *,
    with_summary: bool = True,
    with_receipt: bool = True,
) -> tuple[Path, dict[str, str]]:
    root = (tmp_path / name).resolve()
    ws = (tmp_path / f"{name}_ws").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    target_file = ws / "doc.txt"
    target_file.write_bytes(b"before-body\n")

    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        stack.tasks._disk_admission = DiskAdmission(
            stack.tasks.disk_admission.context,
            probe=lambda _p: 1 << 62,
        )
        task = stack.tasks.create_task("审查受管恢复全链路", [str(ws)])
        run = stack.tasks.start_run(task.task_id)
        run_id = run.run_id
        stack.leases.acquire([ws], run_id, ttl_ms=3_600_000)

        req = ActionRequest(
            request_id="req_b1",
            task_id=task.task_id,
            run_id=run_id,
            job_id="job_b1",
            claim_epoch=1,
            owner_instance_id="inst_b1",
            goal_revision=1,
            lease_epoch=1,
            call_id="call_pending",
            attempt_id=1,
            tool_id="modify_text",
            tool_version="1.0",
            schema_hash="a" * 64,
            action="overwrite",
            resources=(str(target_file),),
            args_hash="b" * 64,
            preconditions={"target": "c" * 64},
        )
        decision = PolicyEngine().classify("overwrite", [str(target_file)], task.workspaces)
        approval = stack.approvals.request(req, decision)

        mem_res = memory_control.remember(
            stack,
            MemoryScope.user(),
            kind="preference",
            content="绝密偏好正文：恢复隔离期间默认脱敏不可直读。",
        )
        assert mem_res.ok

        # 外部大 payload 事件（落入 root/blobs）
        log.append(
            run_id,
            "tool.intent",
            {
                "call_id": "call_pending",
                "tool_id": "modify_text",
                "args": {"path": "doc.txt", "content": "Z" * 300_000},
                "args_hash": "d" * 64,
            },
            actor_kind="core",
            legacy_write=True,
        )

        if with_receipt:
            snap = SnapshotStore(root / "artifacts")
            snap.init_run(run_id)
            change = snap.record_preimage(
                run_id,
                "call_done",
                "doc.txt",
                b"before-body\n",
                hashlib.sha256(b"after-body\n").hexdigest(),
                root=ws,
            )
            intent_seq = log.append(
                run_id,
                "tool.intent",
                {
                    "call_id": "call_done",
                    "tool_id": "modify_text",
                    "args": {"path": "doc.txt", "content": "after-body\n"},
                    "args_hash": "e" * 64,
                },
                actor_kind="core",
                legacy_write=True,
            )
            log.append(
                run_id,
                "tool.result",
                {
                    "call_id": "call_done",
                    "tool_id": "modify_text",
                    "status": "succeeded",
                    "effect_state": "applied",
                    "output": {
                        "path": "doc.txt",
                        "sha256": change.expected_after,
                        "before_state": change.before_state,
                        "after_state": change.expected_after,
                    },
                },
                actor_kind="core",
            )
            from tianshu.receipt import build_receipt
            from tianshu.tools import ToolResult

            rec = build_receipt(
                run_id=run_id,
                call_id="call_done",
                tool_id="modify_text",
                args_hash="e" * 64,
                result=ToolResult(
                    "succeeded",
                    "applied",
                    {
                        "path": "doc.txt",
                        "sha256": change.expected_after,
                        "before_state": change.before_state,
                        "after_state": change.expected_after,
                    },
                ),
                backend="local_fs",
                root=str(ws),
                resource=str(target_file),
                action="overwrite",
                intent_seq=intent_seq,
                blob_ref=change.blob_ref,
            )
            assert rec is not None
            log.append(
                run_id,
                "receipt",
                rec.to_payload(),
                actor_kind="core",
                idem_key=rec.receipt_id,
            )
            jstore = JournalStore(root / "artifacts")
            jstore.begin(
                run_id,
                control_run="ctrl_1",
                items=(
                    JournalItem(
                        root=str(ws),
                        path="doc.txt",
                        call_id="call_done",
                        before_state=change.before_state,
                        expected_after=change.expected_after,
                        action=ACTION_RESTORE,
                    ),
                ),
            )

        if with_summary:
            sstore = SummaryStore(log)
            mat = summaries.build_material(
                [MaterialBlock(seq=1, role="user", content="早期上下文消息")],
                scope="run",
                task_id=task.task_id,
                run_id=run_id,
            )
            pending_sum = sstore.begin(
                mat,
                model="mock-model",
                attempt_id="att_1",
            )
            sstore.complete(
                pending_sum,
                text="摘要正文：早期消息已压缩",
                input_tokens=10,
                output_tokens=8,
                micros=100,
            )

        return root, {
            "task_id": task.task_id,
            "run_id": run_id,
            "approval_id": approval.approval_id,
            "memory_id": mem_res.output["memory_id"],
            "workspace": str(ws),
        }
    finally:
        log.close()


def _refresh_backup_db_entry(backup_dir: Path) -> None:
    manifest_path = backup_dir / "backup.json"
    raw = json.loads(manifest_path.read_bytes().decode("utf-8"))
    db_bytes = (backup_dir / "events.db").read_bytes()
    for entry in raw["files"]:
        if entry["path"] == "events.db":
            entry["size"] = len(db_bytes)
            entry["sha256"] = hashlib.sha256(db_bytes).hexdigest()
    conn = sqlite3.connect(
        (backup_dir / "events.db").resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    try:
        raw["db_user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        row = conn.execute("SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM events").fetchone()
        raw["event_count"] = int(row[0])
        raw["event_seq_head"] = int(row[1])
    finally:
        conn.close()
    manifest_path.write_bytes(json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"))


# ======================================================================== B01


@requires_managed_restore_writes
def test_b01_two_process_barriers_eventlog_vs_apply_and_reserved_vs_eventlog(tmp_path):
    """B01：真实双进程屏障覆盖『普通先 anchor』与『apply 先 reserved』，无第二空历史。"""
    root, _ = _make_rich_root(tmp_path)
    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)
    planned = restore_manager.plan(bdir, root)

    # 1) 普通进程先打开新根 anchor 并持锁 -> restore-apply 必须立即拒绝且不破坏普通连接
    child_code = """
import json, sys
from pathlib import Path
from tianshu.events import EventLog

log = EventLog(Path(sys.argv[1]))
print(json.dumps({"ready": True}), flush=True)
sys.stdin.readline()
log.close()
"""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code, str(root / "events.db")],
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **CHILD_TEXT,
    )
    try:
        assert proc.stdout is not None
        assert json.loads(proc.stdout.readline()) == {"ready": True}
        apply_res = _cli(
            "restore-apply",
            bdir,
            "--data-root",
            root,
            "--expect-plan",
            planned.plan_sha256,
            "--json",
        )
        assert apply_res.returncode == 2
        payload = _parse_resp_v1(apply_res.stdout.encode("utf-8"))
        assert payload.ok is False
    finally:
        if proc.stdin is not None:
            proc.stdin.write("done\n")
            proc.stdin.flush()
        proc.communicate(timeout=20)

    shutil.rmtree(root)

    # 2) apply 先提交 durable reserved 并硬退（此时目标目录根本不存在）-> 普通打开必须拒绝且零建文件
    planned2 = restore_manager.plan(bdir, root)
    crashed = _cli(
        "restore-apply",
        bdir,
        "--data-root",
        root,
        "--expect-plan",
        planned2.plan_sha256,
        failpoint="FP-RESERVED",
    )
    assert crashed.returncode == 77
    assert not root.exists()

    with pytest.raises(RestoreScopeBusy):
        EventLog(root / "events.db")
    assert not root.exists()


# ======================================================================== B02


@requires_managed_restore_writes
def test_b02_published_hold_blocks_every_public_material_background_and_alias_entrypoint(
    tmp_path,
):
    """B02：published_hold 阻断所有普通/后代/别名/硬链接/独立材料/维护/常驻入口。"""
    root, _ = _make_rich_root(tmp_path)
    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)

    p_res = _cli("restore-plan", bdir, "--data-root", root, "--json")
    assert p_res.returncode == 3
    plan_sha = json.loads(p_res.stdout)["plan_sha256"]

    a_res = _cli(
        "restore-apply", bdir, "--data-root", root, "--expect-plan", plan_sha, "--json"
    )
    assert a_res.returncode == 0, a_res.stderr
    parsed_apply = _parse_resp_v1(a_res.stdout.encode("utf-8"))
    assert parsed_apply.ok is True
    assert parsed_apply.result.phase == "published_hold"
    assert parsed_apply.result.held is True
    assert (root / ".restore-origin.json").is_file()
    assert (root / ".restore-token.json").is_file()

    # 标准与后代 EventLog
    with pytest.raises(RestoreScopeBusy):
        EventLog(root / "events.db")
    with pytest.raises(RestoreScopeBusy):
        EventLog(root / "nested" / "custom.db")

    # 内存库但把外部 blob 指向隔离根
    with pytest.raises(RestoreScopeBusy):
        EventLog(Path(":memory:"), blob_dir=root / "blobs")

    mem_log = EventLog(Path(":memory:"))
    try:
        with pytest.raises(RestoreScopeBusy):
            MemoryProjection(mem_log.conn, log=mem_log, blob_dir=root / "memory_blobs")
        with pytest.raises(RestoreScopeBusy):
            ContentStore(mem_log, blob_dir=root / "artifacts" / "blobs")
    finally:
        mem_log.close()

    # 独立材料与执行输出写者
    with pytest.raises(RestoreScopeBusy):
        SnapshotStore(root / "artifacts")
    with pytest.raises(RestoreScopeBusy):
        JournalStore(root / "artifacts")
    ws = tmp_path / "exec_ws"
    ws.mkdir()
    policy = ExecPolicy(
        argv_allowlist=(sys.executable,),
        cwd_roots=(ws,),
        max_output_bytes=4096,
        timeout_ms=5000,
    )
    with pytest.raises(RestoreScopeBusy):
        spawn(
            [sys.executable, "-c", "print('hi')"],
            cwd=ws,
            env={},
            policy=policy,
            capabilities=LOCAL_EXEC_BACKEND.capabilities,
            blob_dir=root / "blobs",
        )

    # 备份/引用/保留期/诊断/迁移/守护进程/Host
    with pytest.raises(RestoreScopeBusy):
        create_backup(root, tmp_path / "b2")
    with pytest.raises(RestoreScopeBusy):
        backup.collect_references(root)
    with pytest.raises(RestoreScopeBusy):
        backup.cleanup_plan(root)
    with pytest.raises(RestoreScopeBusy):
        backup.usage_report(root)
    with pytest.raises(RestoreScopeBusy):
        diagnostics_export.preview_diagnostics(root)
    with pytest.raises(RestoreScopeBusy):
        diagnostics_export.export_diagnostics(root, tmp_path / "diag_out")
    with pytest.raises(RestoreScopeBusy):
        migrate.migrate(root, backup=bdir)
    with pytest.raises(RestoreScopeBusy):
        daemon.acquire_lock(root)
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    with pytest.raises(RestoreScopeBusy):
        host.acquire_host_info(root, ui_dir=ui_dir)

    # 符号链接别名与硬链接别名绕过负控
    sym_root = tmp_path / "sym_alias"
    try:
        sym_root.symlink_to(root, target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(RestoreScopeBusy):
            EventLog(sym_root / "events.db")
        with pytest.raises(RestoreScopeBusy):
            SnapshotStore(sym_root / "artifacts")

    hard_db = tmp_path / "hardlink_events.db"
    try:
        os.link(root / "events.db", hard_db)
    except OSError:
        pass
    else:
        try:
            with pytest.raises(RestoreScopeBusy):
                EventLog(hard_db)
        finally:
            hard_db.unlink()

    # 父目录重命名绕过负控（按 S/D 实体与稳定祖先拦截）
    renamed_root = tmp_path / "data_renamed_attempt"
    root.rename(renamed_root)
    try:
        with pytest.raises(RestoreScopeBusy):
            EventLog(renamed_root / "events.db")
    finally:
        renamed_root.rename(root)


# ======================================================================== B03


def test_b03_empty_skeleton_allows_ordinary_open_but_origin_prevents_creation(tmp_path):
    bad_root = tmp_path / "bad_new"
    bad_root.mkdir()
    (bad_root / ".restore-origin.json").write_bytes(b"{}")
    with pytest.raises((AdmissionRefused, RestoreRegistryCorrupt)):
        EventLog(bad_root / "events.db")
    assert not (bad_root / "events.db").exists()

    ok_skeleton = tmp_path / "ok_skeleton"
    cur = ok_skeleton / "artifacts"
    for idx in range(40):
        cur = cur / f"d{idx}"
    cur.mkdir(parents=True)
    for idx in range(135):
        (ok_skeleton / "blobs" / f"s{idx}").mkdir(parents=True)
    log = EventLog(ok_skeleton / "events.db")
    log.close()


@pytest.fixture
def b03_empty_target(tmp_path):
    root, _ = _make_rich_root(tmp_path, name="target_empty_160")
    bdir = tmp_path / "backup_160"
    create_backup(root, bdir)
    shutil.rmtree(root)

    deep = root
    for idx in range(40):
        deep = deep / f"l{idx}"
    deep.mkdir(parents=True)
    for idx in range(135):
        (root / f"branch_{idx}" / "leaf").mkdir(parents=True)

    return bdir, root, deep


def test_b03_plan_handles_deep_empty_tree_and_refuses_hidden_file(b03_empty_target):
    bdir, root, deep = b03_empty_target
    sneaky = deep / "keep.bin"
    sneaky.write_bytes(b"x")
    with pytest.raises(restore_manager.RestoreOperationError) as exc_info:
        restore_manager.plan(bdir, root)
    assert exc_info.value.failure.category == "incompatible"
    assert sneaky.read_bytes() == b"x"
    sneaky.unlink()

    planned = restore_manager.plan(bdir, root)
    assert planned.inputs.original_target.kind == "empty"
    assert not RestoreRegistry().observe().present
    assert deep.is_dir() and not (root / "events.db").exists()


@requires_managed_restore_writes
def test_b03_apply_replaces_deep_empty_tree(b03_empty_target):
    bdir, root, _ = b03_empty_target
    planned = restore_manager.plan(bdir, root)
    res = restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    assert res.phase == "published_hold"
    assert (root / "events.db").is_file()
    assert not (root / "l0").exists()


# ======================================================================== B04


@requires_managed_restore_writes
def test_b04_unrelated_root_advances_during_stage_copy_and_shared_gate_blocks_apply(
    tmp_path, monkeypatch
):
    """B04：shared gate 下普通打开并行而 apply 忙；P2 复制期间不持 gate，无关根自由读写。"""
    root, _ = _make_rich_root(tmp_path, name="r_main")
    bdir = tmp_path / "b_main"
    create_backup(root, bdir)
    shutil.rmtree(root)
    planned = restore_manager.plan(bdir, root)

    with acquire_restore_gate(exclusive=False):
        other_a = EventLog(tmp_path / "other_a" / "events.db")
        other_b = EventLog(tmp_path / "other_b" / "events.db")
        other_a.close()
        other_b.close()
        with pytest.raises(restore_manager.RestoreOperationError) as busy_exc:
            restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
        assert busy_exc.value.failure.category == "maintenance_busy"

    in_copy = threading.Event()
    allow_finish = threading.Event()
    real_copy = backup._copy_file_verified

    def paused_copy(src: Path, dst: Path):
        in_copy.set()
        assert allow_finish.wait(20)
        return real_copy(src, dst)

    monkeypatch.setattr(backup, "_copy_file_verified", paused_copy)
    outcome: queue.Queue[object] = queue.Queue()

    def run_apply() -> None:
        try:
            outcome.put(
                restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
            )
        except BaseException as exc:
            outcome.put(exc)

    worker = threading.Thread(target=run_apply)
    worker.start()
    assert in_copy.wait(20)
    try:
        unrelated = EventLog(tmp_path / "unrelated_live" / "events.db")
        try:
            unrelated.append(
                "r_unrel",
                "run.start",
                event_schema.minimal_payload("run.start"),
            )
        finally:
            unrelated.close()
    finally:
        allow_finish.set()
    worker.join(timeout=20)
    result = outcome.get_nowait()
    assert isinstance(result, restore_manager.RestoreActionResultV1)
    assert result.phase == "published_hold"


# ======================================================================== B05


@requires_managed_restore_writes
def test_b05_dual_anchor_single_missing_or_tampered_witness_fails_closed(tmp_path):
    """B05：删 registry 留 origin、删 origin 留 registry、篡改摘要均闭锁为 record_corrupt。"""
    # 正控：无任何见证的纯净 legacy 根正常打开
    legacy_root = tmp_path / "legacy_clean"
    leg_log = EventLog(legacy_root / "events.db")
    leg_log.close()

    # 1) 构造第 1 份隔离根，删除 .restore-origin.json 保留 registry -> record_corrupt
    r1, _ = _make_rich_root(tmp_path, name="r1")
    b1 = tmp_path / "b1"
    create_backup(r1, b1)
    shutil.rmtree(r1)
    p1 = restore_manager.plan(b1, r1)
    restore_manager.apply(b1, r1, expect_plan_sha256=p1.plan_sha256)

    origin_bytes = (r1 / ".restore-origin.json").read_bytes()
    (r1 / ".restore-origin.json").unlink()
    with pytest.raises(RestoreRegistryCorrupt):
        EventLog(r1 / "events.db")
    with pytest.raises(RestoreRegistryCorrupt):
        SnapshotStore(r1 / "artifacts")

    # 恢复文件但篡改 1 个十六进制字符 -> record_corrupt
    bad_json = json.loads(origin_bytes.decode("utf-8"))
    bad_json["binding_sha256"] = (
        "0" if bad_json["binding_sha256"][0] != "0" else "1"
    ) + bad_json["binding_sha256"][1:]
    (r1 / ".restore-origin.json").write_bytes(restore_manager.restore_json_v1(bad_json))
    with pytest.raises(RestoreRegistryCorrupt):
        EventLog(r1 / "events.db")

    # 写回合法 origin，删掉 RestoreRegistry 目录 -> 闭锁为 RestoreRegistryCorrupt
    (r1 / ".restore-origin.json").write_bytes(origin_bytes)
    reg = RestoreRegistry()
    shutil.rmtree(reg.authority_directory)
    with pytest.raises(RestoreRegistryCorrupt):
        EventLog(r1 / "events.db")
    with pytest.raises(RestoreRegistryCorrupt):
        SnapshotStore(r1 / "artifacts")


# ======================================================================== B06


@pytest.fixture(params=[0, 1, 2], ids=["v0", "v1", "v2"])
def b06_versioned_backup(tmp_path, request):
    target_uv = request.param
    root, _ = _make_rich_root(tmp_path, name=f"root_v{target_uv}")
    if target_uv in (1, 2):
        pre_backup = tmp_path / f"pre_mig_{target_uv}"
        create_backup(root, pre_backup)
        if target_uv == 1:
            # 用 migration-after-step-1 硬退产生合法 step-1 库，再删进行中 sidecar
            crashed = _cli(
                "migrate",
                "--data-root",
                root,
                "--backup",
                pre_backup,
                failpoint="migration-after-step-1",
            )
            assert crashed.returncode == 77
            for sidecar in root.glob("*.migration.json"):
                sidecar.unlink()
        else:
            mig = _cli("migrate", "--data-root", root, "--backup", pre_backup, "--json")
            assert mig.returncode == 0, mig.stderr

    bdir = tmp_path / f"backup_v{target_uv}"
    create_backup(root, bdir)
    assert verify_backup(bdir).db_user_version == target_uv
    shutil.rmtree(root)
    return bdir, root, target_uv


def test_b06_plans_valid_backup_versions_without_writes(b06_versioned_backup):
    bdir, root, target_uv = b06_versioned_backup
    planned = restore_manager.plan(bdir, root)
    assert planned.verification.db_user_version == target_uv
    assert not root.exists() and not RestoreRegistry().observe().present


@requires_managed_restore_writes
def test_b06_import_keeps_original_db_version(b06_versioned_backup):
    bdir, root, target_uv = b06_versioned_backup
    planned = restore_manager.plan(bdir, root)
    applied = restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    assert applied.phase == "published_hold"
    rev = restore_manager.review(root)
    assert rev.review_complete is True

    # 验证恢复后的 user_version 保持原值（v1 绝未被偷升为 v2）
    conn = sqlite3.connect(
        (root / "events.db").resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == target_uv
    finally:
        conn.close()


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "future_uv",
        "fts_mismatch",
        "summary_source_bad",
        "unreferenced_extra_blob",
        "bad_exec_policy",
    ],
)
def test_b06_semantic_and_material_tampering_refused_before_reserved(tmp_path, tamper_kind):
    root, _ = _make_rich_root(tmp_path)
    bdir = tmp_path / "backup_tamper"
    create_backup(root, bdir)
    shutil.rmtree(root)

    if tamper_kind == "unreferenced_extra_blob":
        (bdir / "artifacts" / "blobs" / ("9" * 64)).write_bytes(b"rogue")
    else:
        conn = sqlite3.connect(str(bdir / "events.db"))
        try:
            if tamper_kind == "future_uv":
                conn.execute("PRAGMA user_version = 99")
            elif tamper_kind == "fts_mismatch":
                conn.execute("UPDATE memory_fts SET tokens = 'tampered_tokens'")
            elif tamper_kind == "summary_source_bad":
                conn.execute("UPDATE summary_sources SET digest = ?", ("f" * 64,))
            elif tamper_kind == "bad_exec_policy":
                conn.execute("UPDATE tasks SET exec_policy = 'not-json'")
            conn.commit()
        finally:
            conn.close()
        _refresh_backup_db_entry(bdir)

    with pytest.raises(restore_manager.RestoreOperationError):
        restore_manager.plan(bdir, root)
    assert not root.exists()
    assert RestoreRegistry().observe().present is False


# ======================================================================== B07


@pytest.mark.parametrize(
    ("candidate_task_count", "candidate_seconds", "problem"),
    [
        pytest.param(1, 100, "创建时间", id="older"),
        pytest.param(3, 200, "创建时间", id="equal-time"),
        pytest.param(1, 300, "事件序号", id="later-time-earlier-event-head"),
        pytest.param(3, 300, None, id="strictly-newer"),
        pytest.param(2, 300, None, id="newer-without-new-events"),
    ],
)
@requires_managed_restore_writes
def test_b07_later_review_requires_newer_backup(
    tmp_path, monkeypatch, candidate_task_count, candidate_seconds, problem,
):
    root = (tmp_path / "data").resolve()
    original = tmp_path / "original"
    candidate = tmp_path / "candidate"
    task_ids = []

    def seal(destination: Path, seconds: int) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(backup, "time", SimpleNamespace(time=lambda: seconds))
            create_backup(root, destination)

    for task_count in range(1, max(2, candidate_task_count) + 1):
        log = EventLog(root / "events.db", blob_dir=root / "blobs")
        try:
            stack = build_stack(log, data_root=root)
            task_ids.append(stack.tasks.create_task(f"B07 task {task_count}").task_id)
        finally:
            log.close()
        if task_count == 2:
            seal(original, 200)
        if task_count == candidate_task_count:
            seal(candidate, candidate_seconds)

    original_manifest = verify_backup(original).require_ok()
    candidate_manifest = verify_backup(candidate).require_ok()
    assert original_manifest.source_path == candidate_manifest.source_path == str(root)
    assert (original_manifest.created_at_ms, original_manifest.event_seq_head) == (200_000, 2)
    assert (candidate_manifest.created_at_ms, candidate_manifest.event_seq_head) == (
        candidate_seconds * 1000, candidate_task_count,
    )
    shutil.rmtree(root)
    planned = restore_manager.plan(original, root)
    restore_manager.apply(original, root, expect_plan_sha256=planned.plan_sha256)
    shutil.rmtree(original)  # 复核从已封存的 Binding 取得恢复点，不依赖原备份仍可读。

    if problem is not None:
        with pytest.raises(restore_manager.RestoreOperationError) as exc_info:
            restore_manager.review(root, later_backup=candidate)
        assert exc_info.value.failure.category == "incompatible"
        assert exc_info.value.failure.phase == "published_hold"
        review = exc_info.value.partial_review
        assert review is not None and not review.review_complete
        check, = (c for c in review.checks if c.name == "later_backup")
        assert not check.passed and any(problem in item for item in check.evidence)
        assert not review.known_later_changes
    else:
        plain = restore_manager.review(root)
        review = restore_manager.review(root, later_backup=candidate)
        assert review.review_complete and review.review_sha256 == plain.review_sha256
        check, = (c for c in review.checks if c.name == "later_backup")
        assert check.passed
        if candidate_task_count == 3:
            change, = (c for c in review.known_later_changes if c.family == "tasks")
            task_key_b64 = base64.b64encode(task_ids[-1].encode("utf-8")).decode("ascii")
            assert change.key == (SqlValueV1("text", task_key_b64),)
            assert change.evidence == ("added_in_later_backup",)
        else:
            assert not review.known_later_changes
    assert review.held and review.later_forget_delete_history == "completeness_unproven"


@requires_managed_restore_writes
def test_b07_held_review_is_zero_write_redacts_by_default_and_diffs_later_backup(
    tmp_path, monkeypatch,
):
    """B07：review 前后全目录与权威零字节变化；默认脱敏、按 selector 披露；后续备份列出遗忘差异。"""
    root, meta = _make_rich_root(tmp_path)
    b1 = tmp_path / "backup_older"
    with monkeypatch.context() as patch:
        patch.setattr(backup, "time", SimpleNamespace(time=lambda: 100))
        create_backup(root, b1)

    # 在活源上执行记忆擦除并生成较新的备份 B2
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = build_stack(log, data_root=root)
        mem_obj = stack.memory.get(meta["memory_id"], allowed=[MemoryScope.user()])
        assert mem_obj is not None
        erased = memory_control.erase(stack, mem_obj)
        assert erased.ok
    finally:
        log.close()
    b2 = tmp_path / "backup_later"
    with monkeypatch.context() as patch:
        patch.setattr(backup, "time", SimpleNamespace(time=lambda: 200))
        create_backup(root, b2)
    shutil.rmtree(root)

    # 导入旧备份 B1 至 published_hold
    planned = restore_manager.plan(b1, root)
    restore_manager.apply(b1, root, expect_plan_sha256=planned.plan_sha256)

    reg = RestoreRegistry()
    before_root = _tree_snapshot(root)
    before_reg = _tree_snapshot(reg.authority_directory)

    rev_cli = _cli(
        "restore-review",
        "--data-root",
        root,
        "--later-backup",
        b2,
        "--json",
    )
    assert rev_cli.returncode == 0, rev_cli.stderr
    assert "绝密偏好正文" not in rev_cli.stdout
    resp = _parse_resp_v2(rev_cli.stdout.encode("utf-8"))
    assert resp.ok is True
    rev = resp.result
    assert rev.review_complete is True
    assert len(rev.review_sha256) == 64
    assert rev.later_forget_delete_history == "completeness_unproven"
    assert any(ch.family == "memories" for ch in rev.known_later_changes)
    for obj in rev.objects:
        for body in obj.bodies:
            assert body.text is None and body.bytes_b64 is None

    # 零写证明：数据根与权威注册表无任何字节/伴随文件变化
    assert _tree_snapshot(root) == before_root
    assert _tree_snapshot(reg.authority_directory) == before_reg

    # 显式传入单个记忆正文 selector -> 仅该槽位披露原文，其余仍为 None
    mem_key = (
        SqlValueV1("text", base64.b64encode(meta["memory_id"].encode("utf-8")).decode("ascii")),
    )
    mem_selector = BodySelectorV1("memories", mem_key, "content").to_selector()
    disclosed_cli = _cli(
        "restore-review",
        "--data-root",
        root,
        "--body-scope",
        mem_selector,
        "--json",
    )
    assert disclosed_cli.returncode == 0, disclosed_cli.stderr
    disclosed_resp = _parse_resp_v2(disclosed_cli.stdout.encode("utf-8"))
    disclosed_bodies = [
        (obj.family, b.selector, b.text)
        for obj in disclosed_resp.result.objects
        for b in obj.bodies
        if b.text is not None or b.bytes_b64 is not None
    ]
    assert len(disclosed_bodies) == 1
    assert disclosed_bodies[0][1] == mem_selector
    assert "绝密偏好正文" in disclosed_bodies[0][2]

    # 坏后续备份使 review_complete=False 并退出 2
    bad_b2 = tmp_path / "missing_later_backup"
    bad_cli = _cli("restore-review", "--data-root", root, "--later-backup", bad_b2, "--json")
    assert bad_cli.returncode == 2
    bad_resp = _parse_resp_v2(bad_cli.stdout.encode("utf-8"))
    assert bad_resp.ok is False
    assert bad_resp.result is not None and bad_resp.result.review_complete is False


# ======================================================================== B08


@pytest.mark.parametrize(
    "fp",
    [
        "FP-INIT",
        "FP-RESERVED",
        "FP-P2-AFTER-STAGE-MKDIR",
        "FP-STAGE-OWNED",
        "FP-TOKEN-TMP-PARTIAL",
        "FP-TOKEN-TMP-FSYNC",
        "FP-TOKEN-RENAME",
        "FP-TOKEN-DIR-FSYNC",
        "FP-STAGE",
        "FP-SEALED",
        "FP-MARKER-TMP-PARTIAL",
        "FP-MARKER-TMP-FSYNC",
        "FP-MARKER-RENAME",
        "FP-MARKER",
        "FP-PUBLISH-INTENT",
        "FP-RENAME",
        "FP-PUBLISHED-BEFORE",
    ],
)
@requires_managed_restore_writes
def test_b08_crashpoints_exit_77_block_eventlog_and_resume_idempotently(tmp_path, fp):
    """B08：17 个窗口 os._exit(77) 硬退 → 普通打开被拒 → resume 幂等至 published_hold。"""
    root, _ = _make_rich_root(tmp_path, name=f"d_{fp.lower().replace('-', '_')}")
    bdir = tmp_path / f"b_{fp.lower().replace('-', '_')}"
    create_backup(root, bdir)
    shutil.rmtree(root)

    planned = restore_manager.plan(bdir, root)
    crashed = _cli(
        "restore-apply",
        bdir,
        "--data-root",
        root,
        "--expect-plan",
        planned.plan_sha256,
        failpoint=fp,
    )
    assert crashed.returncode == 77, crashed.stderr

    if fp == "FP-INIT":
        # FP-INIT 发生在权威初始化后、reserved 提交前：目标未动，重跑 plan+apply 即可成功
        assert not root.exists()
        replanned = restore_manager.plan(bdir, root)
        res = restore_manager.apply(bdir, root, expect_plan_sha256=replanned.plan_sha256)
        assert res.phase == "published_hold"
        return

    # 任何已提交 reserved 的中断态必须阻断普通 EventLog
    with pytest.raises(RestoreScopeBusy):
        EventLog(root / "events.db")

    # P5 核对不依赖外部原备份仍存在
    if fp in ("FP-PUBLISH-INTENT", "FP-RENAME", "FP-PUBLISHED-BEFORE"):
        shutil.rmtree(bdir)
        # publish_intent 及之后不可逆，禁止 abort！
        ab_res = _cli("restore-abort", "--data-root", root, "--json")
        assert ab_res.returncode == 2
        assert (
            _parse_resp_v1(ab_res.stdout.encode("utf-8")).error.category == "held"
        )

    res1 = _cli("restore-resume", "--data-root", root, "--json")
    assert res1.returncode == 0, res1.stderr
    parsed1 = _parse_resp_v1(res1.stdout.encode("utf-8"))
    assert parsed1.result.phase == "published_hold"

    # 二次同批 resume 幂等返回同一 published_hold 事实
    res2 = _cli("restore-resume", "--data-root", root, "--json")
    assert res2.returncode == 0
    parsed2 = _parse_resp_v1(res2.stdout.encode("utf-8"))
    assert parsed2.result == parsed1.result

    obs = RestoreRegistry().observe()
    validate_event_chain(obs.events)


@pytest.mark.parametrize(
    "abort_fp",
    [
        "FP-ABORT-INTENT",
        "FP-ABORT-AFTER-UNLINK",
        "FP-ABORT-AFTER-STAGE-RMDIR",
        "FP-ABORTED-BEFORE",
    ],
)
@requires_managed_restore_writes
def test_b08_abort_crashpoints_freeze_cleanup_plan_and_converge_via_resume_or_abort(
    tmp_path, abort_fp
):
    """B08：abort_intent 提交后清理硬退，后续无论调 resume 还是 abort 都只续同一冻结计划。"""
    root, _ = _make_rich_root(tmp_path, name=f"ab_{abort_fp.lower().replace('-', '_')}")
    bdir = tmp_path / f"bab_{abort_fp.lower().replace('-', '_')}"
    create_backup(root, bdir)
    shutil.rmtree(root)

    planned = restore_manager.plan(bdir, root)
    crashed_apply = _cli(
        "restore-apply",
        bdir,
        "--data-root",
        root,
        "--expect-plan",
        planned.plan_sha256,
        failpoint="FP-SEALED",
    )
    assert crashed_apply.returncode == 77

    crashed_abort = _cli("restore-abort", "--data-root", root, failpoint=abort_fp)
    assert crashed_abort.returncode == 77

    # abort_intent 未结清前普通打开仍被阻断
    with pytest.raises(RestoreScopeBusy):
        EventLog(root / "events.db")

    # 用 restore-resume 收尾：因为已有 abort_intent，resume 必须只续清理并结清为 aborted！
    resumed = _cli("restore-resume", "--data-root", root, "--json")
    assert resumed.returncode == 0, resumed.stderr
    parsed = _parse_resp_v1(resumed.stdout.encode("utf-8"))
    assert parsed.result.phase == "aborted"
    assert parsed.result.held is False
    assert not list(tmp_path.glob(f".{root.name}.restore-*"))

    # 重复 abort 幂等返回原 aborted 事实
    again = _cli("restore-abort", "--data-root", root, "--json")
    assert again.returncode == 0
    assert _parse_resp_v1(again.stdout.encode("utf-8")).result == parsed.result


@requires_managed_restore_writes
def test_b08_two_process_apply_abort_barrier_and_unknown_stage_file_refusal(tmp_path):
    """B08：两真实进程证明 apply 持操作独占期间 abort 忙退出；未知 stage 文件拒绝清理且不误删。"""
    root, _ = _make_rich_root(tmp_path)
    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)
    planned = restore_manager.plan(bdir, root)

    # 1) 子进程持有批次操作独占锁 -> 另一进程 restore-abort 立即返回 maintenance_busy
    crashed = _cli(
        "restore-apply",
        bdir,
        "--data-root",
        root,
        "--expect-plan",
        planned.plan_sha256,
        failpoint="FP-STAGE",
    )
    assert crashed.returncode == 77

    holder_code = """
import json, sys
from pathlib import Path
from tianshu import restore_manager
from tianshu.restore_registry import RestoreRegistry

reg = RestoreRegistry()
obs, bid, _ = restore_manager._resolve_batch_observation(reg, Path(sys.argv[1]), None)
obs, op_access, _ = restore_manager._acquire_batch_operation_exclusive(reg, obs, bid)
print(json.dumps({"locked": bid}), flush=True)
sys.stdin.readline()
op_access.close()
"""
    holder = subprocess.Popen(
        [sys.executable, "-u", "-c", holder_code, str(root)],
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **CHILD_TEXT,
    )
    try:
        assert holder.stdout is not None
        assert "locked" in json.loads(holder.stdout.readline())
        busy_abort = _cli("restore-abort", "--data-root", root, "--json")
        assert busy_abort.returncode == 2
        busy_resp = _parse_resp_v1(busy_abort.stdout.encode("utf-8"))
        assert busy_resp.error.category == "maintenance_busy"
    finally:
        if holder.stdin is not None:
            holder.stdin.write("release\n")
            holder.stdin.flush()
        holder.communicate(timeout=20)

    # 2) 在 stage 中放入未知用户文件 -> abort 拒绝构造清理计划且绝不删除该未知文件
    stage_dirs = list(tmp_path.glob(f".{root.name}.restore-*"))
    assert len(stage_dirs) == 1
    stranger = stage_dirs[0] / "do_not_delete_me.txt"
    stranger.write_bytes(b"precious")
    refused = _cli("restore-abort", "--data-root", root, "--json")
    assert refused.returncode == 2
    assert stranger.read_bytes() == b"precious"
    stranger.unlink()

    clean_abort = _cli("restore-abort", "--data-root", root, "--json")
    assert clean_abort.returncode == 0


# ======================================================================== B09


@pytest.fixture
def b09_backup(tmp_path):
    root, _ = _make_rich_root(tmp_path)
    bdir = tmp_path / "backup"
    create_backup(root, bdir)
    shutil.rmtree(root)
    return bdir, root


def _b09_crash(bdir, root, failpoint):
    planned = restore_manager.plan(bdir, root)
    crashed = _cli(
        "restore-apply", bdir, "--data-root", root,
        "--expect-plan", planned.plan_sha256, failpoint=failpoint,
    )
    assert crashed.returncode == 77, crashed.stderr
    obs = RestoreRegistry().observe()
    stage = root.parent / obs.events[0].payload.stage_name
    return obs, stage


def test_b09_plan_counts_copy_and_prepared_reservation_without_padding(b09_backup, monkeypatch):
    bdir, root = b09_backup
    reg = RestoreRegistry()
    initial = restore_manager.plan(bdir, root)
    exact_copy = sum(f.size for f in initial.inputs.copy_inventory.files)
    registry_bytes = initial.capacity_checks[1].required_known_bytes
    registry_free = registry_bytes
    probes = []

    def disk_free(where):
        probes.append(where)
        return exact_copy if where == root.parent else registry_free

    monkeypatch.setattr(restore_manager, "_disk_usage_probe", disk_free)
    planned = restore_manager.plan(bdir, root)
    assert planned.plan_sha256 == initial.plan_sha256
    assert probes == [root.parent, reg.home, root.parent]
    assert [check.required_known_bytes for check in planned.capacity_checks] == [
        exact_copy, registry_bytes, 0,
    ]
    registry_free -= 1
    with pytest.raises(restore_manager.RestoreOperationError) as short:
        restore_manager.plan(bdir, root)
    assert short.value.failure.category == "capacity_unavailable"
    assert not root.exists() and not reg.dot_directory.exists()


@requires_managed_restore_writes
def test_b09_planned_reservation_size_matches_persisted_event(b09_backup):
    bdir, root = b09_backup
    initial = restore_manager.plan(bdir, root)
    registry_bytes = initial.capacity_checks[1].required_known_bytes
    obs, _ = _b09_crash(bdir, root, "FP-RESERVED")
    assert registry_bytes == len(obs.events[0].to_bytes())


@requires_managed_restore_writes
def test_b09_registry_init_probes_actual_meta_before_sqlite_writes(b09_backup, monkeypatch):
    bdir, root = b09_backup
    reg = RestoreRegistry()
    planned = restore_manager.plan(bdir, root)
    codec = NativePathCodecV1()
    checked = []
    short = True

    def disk_free(where):
        if where.parent != reg.dot_directory or where == reg.authority_directory:
            return 1 << 40
        candidate_db = where / "registry.db"
        assert candidate_db.stat().st_size == 0
        meta = RegistryMetaV1(
            1, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            DirRefV1(codec.from_path(reg.home), observe_entity(reg.home, "directory")),
            DirRefV1(codec.from_path(reg.authority_directory), observe_entity(where, "directory")),
            EntityRefV1(codec.from_path(reg.path), observe_entity(candidate_db, "file")),
        )
        checked.append(len(meta.to_bytes()))
        return checked[-1] - int(short)

    monkeypatch.setattr(restore_manager, "_disk_usage_probe", disk_free)
    with pytest.raises(restore_manager.RestoreOperationError) as too_small:
        restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    assert too_small.value.failure.category == "capacity_unavailable"
    assert len(checked) == 1
    assert not reg.observe().present
    assert not root.exists()
    assert not list(root.parent.glob(f".{root.name}.restore-*"))

    short = False
    result = restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    assert result.phase == "published_hold"
    assert len(checked) == 2
    assert checked[-1] == len(reg.observe().meta_bytes)


@pytest.mark.parametrize("partial", [False, True], ids=["missing", "partial"])
@requires_managed_restore_writes
def test_b09_resume_counts_only_unverified_files_at_full_size(b09_backup, monkeypatch, partial):
    bdir, root = b09_backup
    obs, stage = _b09_crash(bdir, root, "FP-STAGE")
    pending = [f for f in obs.events[0].payload.copy_inventory.files if f.path != ("events.db",)]
    assert (stage / "events.db").read_bytes() == (bdir / "events.db").read_bytes()
    if partial:
        spec = max(pending, key=lambda f: f.size)
        assert spec.size > 2
        partial_file = stage.joinpath(*spec.path)
        partial_file.write_bytes(bdir.joinpath(*spec.path).read_bytes()[:spec.size // 2])
    exact = sum(f.size for f in pending)
    free = exact - 1
    monkeypatch.setattr(
        restore_manager, "_disk_usage_probe", lambda p: free if p == stage else 1 << 40,
    )
    before = _tree_snapshot(stage)
    with pytest.raises(restore_manager.RestoreOperationError) as short:
        restore_manager.resume(root)
    assert short.value.failure.category == "capacity_unavailable"
    assert short.value.failure.phase == "stage_owned"
    assert RestoreRegistry().observe().head_sha256 == obs.head_sha256
    assert _tree_snapshot(stage) == before
    assert not root.exists()

    free = exact
    assert restore_manager.resume(root).phase == "published_hold"
    for spec in pending:
        assert root.joinpath(*spec.path).read_bytes() == bdir.joinpath(*spec.path).read_bytes()


@requires_managed_restore_writes
def test_b09_temp_token_charged_in_full_before_any_copy_writes(b09_backup, monkeypatch):
    bdir, root = b09_backup
    obs, stage = _b09_crash(bdir, root, "FP-TOKEN-TMP-PARTIAL")
    assert obs.events[-1].kind == "stage_owned"
    token = base64.b64decode(obs.events[-1].payload.token_bytes_b64)
    tmp_token = stage / ".restore-token.json.tmp"
    assert 0 < tmp_token.stat().st_size < len(token)
    exact = sum(f.size for f in obs.events[0].payload.copy_inventory.files) + len(token)
    free = exact - 1
    monkeypatch.setattr(
        restore_manager, "_disk_usage_probe", lambda p: free if p == stage else 1 << 40,
    )
    before = _tree_snapshot(stage)
    with pytest.raises(restore_manager.RestoreOperationError) as short:
        restore_manager.resume(root)
    assert short.value.failure.category == "capacity_unavailable"
    assert _tree_snapshot(stage) == before
    assert RestoreRegistry().observe().head_sha256 == obs.head_sha256

    free = exact
    assert restore_manager.resume(root).phase == "published_hold"
    assert (root / ".restore-token.json").read_bytes() == token


@pytest.mark.parametrize("formal", [False, True], ids=["partial", "formal"])
@requires_managed_restore_writes
def test_b09_origin_only_charges_new_bytes_and_still_probes_capacity(
    b09_backup, monkeypatch, formal,
):
    bdir, root = b09_backup
    obs, stage = _b09_crash(bdir, root, "FP-MARKER-RENAME" if formal else "FP-MARKER-TMP-PARTIAL")
    sealed = obs.events[-1]
    assert sealed.kind == "sealed_hold"
    origin = OriginV1(
        1, sealed.registry_id, sealed.batch_id,
        domain_digest("event", sealed), sealed.payload.binding_sha256,
    ).to_bytes()
    if formal:
        assert (stage / ".restore-origin.json").read_bytes() == origin
    else:
        assert 0 < (stage / ".restore-origin.json.tmp").stat().st_size < len(origin)
    exact = 0 if formal else len(origin)
    free = None if formal else exact - 1
    monkeypatch.setattr(
        restore_manager, "_disk_usage_probe", lambda p: free if p == stage else 1 << 40,
    )
    before = _tree_snapshot(stage)
    with pytest.raises(restore_manager.RestoreOperationError) as short:
        restore_manager.resume(root)
    assert short.value.failure.category == "capacity_unavailable"
    assert RestoreRegistry().observe().head_sha256 == obs.head_sha256
    assert _tree_snapshot(stage) == before

    free = exact
    assert restore_manager.resume(root).phase == "published_hold"
    assert (root / ".restore-origin.json").read_bytes() == origin


@requires_managed_restore_writes
def test_b09_publish_checks_parent_before_removing_original_empty_target(b09_backup, monkeypatch):
    bdir, root = b09_backup
    (root / "empty" / "skeleton").mkdir(parents=True)
    obs, stage = _b09_crash(bdir, root, "FP-PUBLISH-INTENT")
    assert obs.events[-1].kind == "publish_intent"
    before = _tree_snapshot(root)
    parent_free = 0
    monkeypatch.setattr(
        restore_manager,
        "_disk_usage_probe",
        lambda p: parent_free if p == root.parent else 1 << 40,
    )
    with pytest.raises(restore_manager.RestoreOperationError) as short:
        restore_manager.resume(root)
    assert short.value.failure.category == "capacity_unavailable"
    assert RestoreRegistry().observe().head_sha256 == obs.head_sha256
    assert (stage / "events.db").is_file()
    assert _tree_snapshot(root) == before

    parent_free = 1
    assert restore_manager.resume(root).phase == "published_hold"


def _b09_record_capacity(patch):
    checked = set()
    real_probe = restore_manager._disk_usage_probe

    def probe(path):
        free = real_probe(path)
        if free > 0:
            checked.add(path)
        return free

    patch.setattr(restore_manager, "_disk_usage_probe", probe)
    return checked


def _b09_assert_resumed(root, bdir, before):
    batch = before.events[0].batch_id
    result = restore_manager.resume(root, batch_id=batch)
    assert result.phase == "published_hold" and result.held and result.batch_id == batch
    committed = RestoreRegistry().observe()
    assert committed.events[:len(before.events)] == before.events
    assert [ev.kind for ev in committed.events] == [
        "reserved", "stage_owned", "sealed_hold", "publish_intent", "published_hold",
    ]
    assert all(ev.batch_id == batch for ev in committed.events)
    for item in before.events[0].payload.copy_inventory.files:
        assert root.joinpath(*item.path).read_bytes() == bdir.joinpath(*item.path).read_bytes()
    assert restore_manager.resume(root, batch_id=batch) == result
    assert RestoreRegistry().observe().head_sha256 == committed.head_sha256


@pytest.mark.parametrize("errnum", [errno.ENOSPC, errno.EIO], ids=["disk-full", "io-error"])
@pytest.mark.parametrize("which", ["copy", "origin"])
@requires_managed_restore_writes
def test_b09_post_probe_write_failure_keeps_partial_file(b09_backup, monkeypatch, errnum, which):
    bdir, root = b09_backup
    before, stage = _b09_crash(bdir, root, "FP-STAGE-OWNED" if which == "copy" else "FP-SEALED")
    if which == "copy":
        path = stage / "events.db"
        expected = (bdir / "events.db").read_bytes()
    else:
        path = stage / ".restore-origin.json.tmp"
        sealed = before.events[-1]
        expected = OriginV1(
            1, sealed.registry_id, sealed.batch_id,
            domain_digest("event", sealed), sealed.payload.binding_sha256,
        ).to_bytes()
    previous_files = _tree_snapshot(stage)
    assert not path.exists()
    real_open, real_write = os.open, os.write
    destination_fd = None
    written = 0
    fault = OSError(errnum, "injected post-probe write failure")

    def open_file(name, flags, *args, **kwargs):
        nonlocal destination_fd
        fd = real_open(name, flags, *args, **kwargs)
        if name == path and flags & os.O_WRONLY:
            destination_fd = fd
        return fd

    def write(fd, data):
        nonlocal written
        if fd != destination_fd:
            return real_write(fd, data)
        assert stage in checked
        if written:
            raise fault
        written = real_write(fd, data[:len(data) // 2])
        assert written > 0
        return written

    with monkeypatch.context() as patch:
        checked = _b09_record_capacity(patch)
        patch.setattr(os, "open", open_file)
        patch.setattr(os, "write", write)
        with pytest.raises(restore_manager.RestoreOperationError) as failure:
            restore_manager.resume(root)
    assert failure.value.raw_cause is fault
    result = failure.value.failure
    expected_category = "capacity_unavailable" if errnum == errno.ENOSPC else "restore_failed"
    assert result.category == expected_category
    assert (result.phase, result.batch_id) == (before.events[-1].kind, before.events[0].batch_id)
    assert [Path(p.text) for p in result.retained_paths] == [stage]
    assert RestoreRegistry().observe().events == before.events
    assert path.read_bytes() == expected[:written]
    assert written < len(expected)
    assert not root.exists() and not (stage / ".restore-origin.json").exists()
    after = _tree_snapshot(stage)
    assert all(after[p] == contents for p, contents in previous_files.items())
    _b09_assert_resumed(root, bdir, before)


@pytest.mark.parametrize(
    "code", [sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR_FSYNC], ids=["full", "fsync"],
)
@pytest.mark.parametrize(
    ("event_kind", "failpoint"),
    [("reserved", "FP-INIT"), ("publish_intent", "FP-MARKER"), ("published_hold", "FP-RENAME")],
)
@requires_managed_restore_writes
def test_b09_post_probe_registry_commit_failure_retains_prior_facts(
    b09_backup, monkeypatch, code, event_kind, failpoint,
):
    bdir, root = b09_backup
    reg = RestoreRegistry()
    planned = restore_manager.plan(bdir, root)
    crashed = _cli(
        "restore-apply", bdir, "--data-root", root,
        "--expect-plan", planned.plan_sha256, failpoint=failpoint,
    )
    assert crashed.returncode == 77, crashed.stderr
    before = reg.observe()
    stage_glob = f".{root.name}.restore-*"
    stages = list(root.parent.glob(stage_glob))
    assert len(stages) == int(event_kind == "publish_intent")
    before_root = _tree_snapshot(root)
    before_stages = {stage: _tree_snapshot(stage) for stage in stages}
    real_connect = sqlite3.connect
    registry_uri = reg.path.resolve().as_uri()
    attempted_batches = []
    fault = sqlite3.OperationalError("injected registry commit failure")
    fault.sqlite_errorcode = code

    class FailingCommit(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                assert reg.authority_directory in checked
                assert self.in_transaction
                assert super().execute("PRAGMA journal_mode").fetchone()[0] == "delete"
                assert super().execute("PRAGMA synchronous").fetchone()[0] == 3  # EXTRA
                head = super().execute(
                    "SELECT e.kind, e.batch_id FROM restore_events e JOIN registry_head h "
                    "ON e.sequence = h.sequence AND e.event_sha256 = h.event_sha256",
                ).fetchone()
                assert head[0] == event_kind
                attempted_batches.append(head[1])
                raise fault
            return super().execute(sql, parameters)

    def connect(path, *args, **kwargs):
        if path == registry_uri:
            kwargs["factory"] = FailingCommit
        return real_connect(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        checked = _b09_record_capacity(patch)
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(restore_manager.RestoreOperationError) as failed:
            if before.events:
                restore_manager.resume(root)
            else:
                restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    error = failed.value.failure
    assert attempted_batches == [error.batch_id]
    assert error.category == (
        "capacity_unavailable" if code == sqlite3.SQLITE_FULL else "durability_unavailable"
    )
    assert error.phase == (before.events[-1].kind if before.events else None)
    observed = reg.observe()
    assert observed.events == before.events and observed.head_sha256 == before.head_sha256
    assert _tree_snapshot(root) == before_root
    assert {stage: _tree_snapshot(stage) for stage in stages} == before_stages
    assert list(root.parent.glob(stage_glob)) == stages
    if before.events:
        with pytest.raises(RestoreScopeBusy):
            EventLog(root / "events.db")
        if root.exists():
            assert "仍隔离" in error.message
            assert [Path(p.text) for p in error.retained_paths] == [root]
        _b09_assert_resumed(root, bdir, before)
    else:
        assert not root.exists() and not stages
        result = restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
        assert result.phase == "published_hold" and result.batch_id != error.batch_id


@pytest.mark.parametrize("errnum", [errno.ENOSPC, errno.EIO], ids=["disk-full", "io-error"])
@pytest.mark.parametrize("which", ["origin", "publish"])
@requires_managed_restore_writes
def test_b09_post_probe_rename_failure_retains_same_stage(b09_backup, monkeypatch, errnum, which):
    bdir, root = b09_backup
    if which == "publish":
        (root / "empty").mkdir(parents=True)
    before, stage = _b09_crash(
        bdir, root, "FP-PUBLISH-INTENT" if which == "publish" else "FP-MARKER-TMP-FSYNC",
    )
    if which == "publish":
        src, dst = stage, root
    else:
        src, dst = stage / ".restore-origin.json.tmp", stage / ".restore-origin.json"
    stage_identity = observe_entity(stage, "directory")
    before_stage = _tree_snapshot(stage)
    real_replace = os.replace
    fault = OSError(errnum, "injected post-probe rename failure")

    def replace(source, target, *args, **kwargs):
        if (source, target) == (src, dst):
            assert src.parent in checked and not dst.exists()
            raise fault
        return real_replace(source, target, *args, **kwargs)

    with monkeypatch.context() as patch:
        checked = _b09_record_capacity(patch)
        patch.setattr(os, "replace", replace)
        with pytest.raises(restore_manager.RestoreOperationError) as failed:
            restore_manager.resume(root)
    assert failed.value.raw_cause is fault
    error = failed.value.failure
    assert error.category == (
        "capacity_unavailable" if errnum == errno.ENOSPC
        else "restore_failed" if which == "publish" else "durability_unavailable"
    )
    assert (error.phase, error.batch_id) == (before.events[-1].kind, before.events[0].batch_id)
    assert RestoreRegistry().observe().events == before.events
    assert _tree_snapshot(stage) == before_stage and not root.exists()
    if which == "publish":
        assert [Path(p.text) for p in error.retained_paths] == [stage]
        assert "仍隔离" in error.message
        assert [cmd[2] for cmd in error.next_commands] == ["restore-resume"]
    _b09_assert_resumed(root, bdir, before)
    assert observe_entity(root, "directory") == stage_identity


@pytest.mark.parametrize("which", ["database", "material"])
@requires_managed_restore_writes
def test_b09_post_probe_resume_flushes_complete_copy_before_sealing(b09_backup, monkeypatch, which):
    bdir, root = b09_backup
    before, stage = _b09_crash(bdir, root, "FP-STAGE-OWNED")
    files = before.events[0].payload.copy_inventory.files
    rel = ("events.db",) if which == "database" else next(
        spec.path for spec in files if spec.path != ("events.db",)
    )
    src, dst = bdir.joinpath(*rel), stage.joinpath(*rel)
    real_fsync = os.fsync
    fault = OSError(errno.EIO, "injected fsync failure after writing complete copy")
    attempts = []

    def fsync(fd):
        if dst.exists() and os.path.samestat(os.fstat(fd), dst.stat()):
            assert stage in checked
            assert dst.read_bytes() == src.read_bytes()
            attempts.append(observe_entity(dst, "file"))
            raise fault
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        checked = _b09_record_capacity(patch)
        patch.setattr(os, "fsync", fsync)
        for _ in range(2):
            with pytest.raises(restore_manager.RestoreOperationError) as failed:
                restore_manager.resume(root)
            assert failed.value.raw_cause is fault
            assert RestoreRegistry().observe().events == before.events
            assert not root.exists()
        assert failed.value.failure.category == "durability_unavailable"
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    _b09_assert_resumed(root, bdir, before)
    assert observe_entity(root.joinpath(*rel), "file") == attempts[0]


@requires_managed_restore_writes
def test_b09_post_probe_pending_parent_barrier_is_retried(tmp_path, monkeypatch):
    base = tmp_path
    pending = base / "pending"
    root, _ = _make_rich_root(pending / "nested")
    bdir = base / "backup"
    create_backup(root, bdir)
    shutil.rmtree(pending)
    before, _ = _b09_crash(bdir, root, "FP-RESERVED")
    base_stat = base.stat()
    real_fsync = os.fsync
    fault = OSError(errno.EIO, "injected failure to persist pending parent")
    attempts = []

    def fsync(fd):
        if os.path.samestat(os.fstat(fd), base_stat):
            assert pending.is_dir() and not root.parent.exists()
            attempts.append(observe_entity(pending, "directory"))
            raise fault
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        for _ in range(2):
            with pytest.raises(restore_manager.RestoreOperationError) as failed:
                restore_manager.resume(root)
            assert failed.value.raw_cause is fault
            assert failed.value.failure.category == "durability_unavailable"
            assert failed.value.failure.phase == "reserved"
            assert RestoreRegistry().observe().events == before.events
            assert not root.parent.exists()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    _b09_assert_resumed(root, bdir, before)


@pytest.mark.parametrize("errnum", [errno.ENOSPC, errno.EIO], ids=["disk-full", "io-error"])
@pytest.mark.parametrize("removed", [False, True], ids=["before-removal", "after-removal"])
@requires_managed_restore_writes
def test_b09_post_probe_empty_target_removal_failure_retains_scene(
    b09_backup, monkeypatch, errnum, removed,
):
    bdir, root = b09_backup
    (root / "empty").mkdir(parents=True)
    before, stage = _b09_crash(bdir, root, "FP-PUBLISH-INTENT")
    original_stage = _tree_snapshot(stage)
    stage_identity = observe_entity(stage, "directory")
    real_rmdir = os.rmdir
    fault = OSError(errnum, "injected post-probe rmdir failure")

    def rmdir(path, *args, **kwargs):
        if path == root:
            assert root.parent in checked
            assert not tuple(root.iterdir())
            if removed:
                real_rmdir(path, *args, **kwargs)
            raise fault
        return real_rmdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        checked = _b09_record_capacity(patch)
        patch.setattr(os, "rmdir", rmdir)
        with pytest.raises(restore_manager.RestoreOperationError) as failed:
            restore_manager.resume(root)
    error = failed.value.failure
    assert failed.value.raw_cause is fault
    expected_category = "capacity_unavailable" if errnum == errno.ENOSPC else "restore_failed"
    assert error.category == expected_category
    assert error.phase == "publish_intent" and "仍隔离" in error.message
    assert error.batch_id == before.events[0].batch_id
    assert [Path(p.text) for p in error.retained_paths] == ([stage] if removed else [stage, root])
    assert error.next_commands == ((
        "tianshu", "data", "restore-resume", "--data-root", str(root), "--batch", error.batch_id,
    ),)
    assert root.exists() == (not removed) and _tree_snapshot(stage) == original_stage
    assert RestoreRegistry().observe().events == before.events
    _b09_assert_resumed(root, bdir, before)
    assert observe_entity(root, "directory") == stage_identity


@requires_managed_restore_writes
@pytest.mark.parametrize(
    ("failpoint", "phase", "retains_stage", "retains_final"),
    [
        ("FP-STAGE", "stage_owned", True, False),
        ("FP-PUBLISH-INTENT", "publish_intent", True, False),
        ("FP-RENAME", "publish_intent", False, True),
        ("FP-ABORT-INTENT", "abort_intent", True, False),
        ("FP-ABORT-AFTER-STAGE-RMDIR", "abort_intent", False, False),
    ],
)
def test_b09_existing_held_scene_is_preserved_without_durability(
    b09_backup, monkeypatch, failpoint, phase, retains_stage, retains_final,
):
    # Create real interrupted states on POSIX before simulating loss of capability.
    bdir, root = b09_backup
    if phase == "abort_intent":
        _, stage = _b09_crash(bdir, root, "FP-STAGE")
        crashed = _cli("restore-abort", "--data-root", root, failpoint=failpoint)
        assert crashed.returncode == 77, crashed.stderr
        before = RestoreRegistry().observe()
    else:
        before, stage = _b09_crash(bdir, root, failpoint)
    batch = before.events[-1].batch_id
    assert before.events[-1].kind == phase
    assert stage.is_dir() == retains_stage and root.is_dir() == retains_final
    retained = [path for path in (stage, root) if path.is_dir()]
    frozen = {path: (_tree_snapshot(path), observe_entity(path, "directory")) for path in retained}

    monkeypatch.setattr(restore_manager, "_managed_restore_writes_supported", lambda: False)
    commands = [restore_manager.resume]
    if phase != "publish_intent":
        commands.append(restore_manager.abort)
    for command in commands:
        with pytest.raises(restore_manager.RestoreOperationError) as failed:
            command(root, batch_id=batch)
        error = failed.value.failure
        assert error.category == "durability_unavailable"
        assert (error.phase, error.batch_id) == (phase, batch)
        assert [Path(p.text) for p in error.retained_paths] == retained
        assert RestoreRegistry().observe() == before
        assert stage.is_dir() == retains_stage and root.is_dir() == retains_final
        assert {
            path: (_tree_snapshot(path), observe_entity(path, "directory")) for path in retained
        } == frozen
        with pytest.raises(RestoreScopeBusy):
            EventLog(root / "events.db")


@requires_managed_restore_writes
def test_b09_post_rename_parent_fsync_failure_reports_final_only(b09_backup, monkeypatch):
    bdir, root = b09_backup
    before, stage = _b09_crash(bdir, root, "FP-PUBLISH-INTENT")
    original_stage = _tree_snapshot(stage)
    stage_identity = observe_entity(stage, "directory")
    real_fsync = os.fsync
    fault = OSError(errno.EIO, "injected publish parent fsync failure")
    attempts = []

    def fail_parent_sync(fd):
        if os.path.samestat(os.fstat(fd), root.parent.stat()):
            assert not stage.exists()
            assert observe_entity(root, "directory") == stage_identity
            attempts.append(fd)
            raise fault
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_parent_sync)
        for _ in range(2):
            with pytest.raises(restore_manager.RestoreOperationError) as failed:
                restore_manager.resume(root)
            error = failed.value.failure
            assert failed.value.raw_cause is fault
            assert error.category == "durability_unavailable"
            assert error.phase == "publish_intent" and "仍隔离" in error.message
            assert error.batch_id == before.events[0].batch_id
            assert [Path(p.text) for p in error.retained_paths] == [root]
            assert error.next_commands[0][-2:] == ("--batch", error.batch_id)
            assert _tree_snapshot(root) == original_stage
            assert RestoreRegistry().observe().events == before.events
            with pytest.raises(RestoreScopeBusy):
                EventLog(root / "events.db")
    assert len(attempts) == 2
    _b09_assert_resumed(root, bdir, before)


def test_b09_zero_capacity_refuses_plan_without_writes(b09_backup, monkeypatch):
    bdir, root = b09_backup
    monkeypatch.setattr(restore_manager, "_disk_usage_probe", lambda _p: 0)
    with pytest.raises(restore_manager.RestoreOperationError) as cap_exc:
        restore_manager.plan(bdir, root)
    assert cap_exc.value.failure.category == "capacity_unavailable"
    assert not root.exists() and not RestoreRegistry().observe().present


@requires_managed_restore_writes
def test_b09_abort_retries_parent_fsync_after_stage_is_absent(b09_backup, monkeypatch):
    bdir, root = b09_backup
    before, stage = _b09_crash(bdir, root, "FP-STAGE")
    batch = before.events[0].batch_id
    real_fsync = os.fsync
    fault = OSError(errno.EIO, "injected cleanup parent fsync failure")
    attempts = []

    def fail_parent_sync(fd):
        if os.path.samestat(os.fstat(fd), root.parent.stat()):
            assert not stage.exists() and not root.exists()
            attempts.append(fd)
            raise fault
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_parent_sync)
        with pytest.raises(restore_manager.RestoreOperationError) as failed:
            restore_manager.abort(root, batch_id=batch)
        assert failed.value.raw_cause is fault
        assert failed.value.failure.category == "durability_unavailable"
        assert failed.value.failure.phase == "abort_intent"
        assert failed.value.failure.retained_paths == ()
        held = RestoreRegistry().observe()
        assert held.events[:-1] == before.events
        assert held.events[-1].kind == "abort_intent"
        assert held.events[-1].batch_id == batch
        with pytest.raises(RestoreScopeBusy):
            EventLog(root / "events.db")

        with pytest.raises(restore_manager.RestoreOperationError) as retried:
            restore_manager.resume(root, batch_id=batch)
        assert retried.value.raw_cause is fault
        assert retried.value.failure.phase == "abort_intent"
        assert RestoreRegistry().observe() == held
    assert len(attempts) == 2
    done = restore_manager.abort(root, batch_id=batch)
    assert done.phase == "aborted" and not done.held and done.batch_id == batch
    ended = RestoreRegistry().observe()
    assert ended.events[:-1] == held.events and ended.events[-1].kind == "aborted"
    assert restore_manager.abort(root, batch_id=batch) == done
    assert RestoreRegistry().observe() == ended


@requires_managed_restore_writes
@pytest.mark.parametrize("boundary", ["home", "private-authority", "published-authority"])
def test_b09_registry_namespace_barrier_precedes_first_reservation(
    b09_backup, monkeypatch, boundary,
):
    bdir, root = b09_backup
    reg = RestoreRegistry()
    planned = restore_manager.plan(bdir, root)
    real_fsync = os.fsync
    fault = OSError(errno.EIO, "injected registry directory fsync failure")
    attempts = []

    def fail_registry_sync(fd):
        if boundary == "home":
            candidates = (reg.home,)
        elif boundary == "private-authority":
            candidates = tuple(reg.dot_directory.glob(".restore-registry-v1.init-*"))
        else:
            candidates = (reg.dot_directory,) if reg.authority_directory.is_dir() else ()
        st = os.fstat(fd)
        for directory in candidates:
            if os.path.samestat(st, directory.stat()):
                assert not root.exists()
                assert not tuple(root.parent.glob(f".{root.name}.restore-*"))
                attempts.append(directory)
                raise fault
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_registry_sync)
        empty_authority = None
        for _ in range(1 if boundary == "private-authority" else 2):
            with pytest.raises(restore_manager.RestoreOperationError) as failed:
                restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
            error = failed.value.failure
            assert error.category == "durability_unavailable"
            assert error.phase is None and error.batch_id is None
            assert error.plan_sha256 == planned.plan_sha256
            assert failed.value.raw_cause.__cause__ is fault
            if boundary == "private-authority":
                # An unpublished authority is forensic evidence, never auto-adopted.
                assert len(attempts) == 1 and not reg.authority_directory.exists()
                with pytest.raises(RestoreRegistryCorrupt, match="未发布初始化现场"):
                    reg.observe()
                private_authority = attempts[0]
                retained = _tree_snapshot(private_authority)
                assert retained
                with pytest.raises(restore_manager.RestoreOperationError) as refused:
                    restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
                assert refused.value.failure.category == "record_corrupt"
                assert _tree_snapshot(private_authority) == retained
                assert not reg.authority_directory.exists() and not root.exists()
                return
            snapshot = reg.observe()
            if boundary == "published-authority":
                assert snapshot.present and snapshot.events == ()
                assert snapshot.head_sha256 is None and snapshot.next_sequence == 1
                if empty_authority is None:
                    empty_authority = snapshot
                assert snapshot == empty_authority
            else:
                assert not snapshot.present
    assert len(attempts) == 2
    result = restore_manager.apply(bdir, root, expect_plan_sha256=planned.plan_sha256)
    assert result.phase == "published_hold"
    observed = reg.observe()
    if empty_authority is not None:
        assert observed.meta == empty_authority.meta
    assert observed.events[0].kind == "reserved" and observed.events[0].sequence == 1
    assert all(event.batch_id == result.batch_id for event in observed.events)
