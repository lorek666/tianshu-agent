"""显式数据根的离线备份、独立验证、迁移、只读恢复预演与只读保留期审计。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import event_schema
from .backup import (
    BackupError,
    BackupRefused,
    cleanup_plan,
    create_backup,
    verify_backup,
)
from .budget import SETTLED
from .credentials import redact
from .data_state import AdmissionRefused
from .errors import TianshuError
from .jobs import TERMINAL_JOB_STATES
from .migrate import SCHEMA_VERSION
from .migrate import migrate as run_migration
from .pathkeys import overlap_key
from .permissions import APPROVAL_STATES
from .restore_access import acquire_restore_database_access
from .tasks import NON_TERMINAL_STATES, RUN_STATES


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "data", help="数据维护：离线备份 / 验证 / 迁移 / 只读恢复预演 / 保留期审计 / 离线安全 GC",
    )
    commands = parser.add_subparsers(dest="data_action", required=True)
    backup = commands.add_parser("backup", help="所有天枢连接关闭后，生成完整备份")
    backup.add_argument("--data-root", type=Path, required=True)
    backup.add_argument("--dest", type=Path, required=True)
    backup.add_argument("--json", action="store_true")
    verify = commands.add_parser("verify", help="只读验证备份完整性")
    verify.add_argument("backup", type=Path)
    verify.add_argument("--json", action="store_true")
    plan = commands.add_parser("restore-plan", help="只读检查恢复条件；不执行恢复")
    plan.add_argument("backup", type=Path)
    plan.add_argument("--data-root", type=Path, required=True)
    plan.add_argument("--json", action="store_true")

    apply_cmd = commands.add_parser(
        "restore-apply",
        help="按已核验计划执行受管导入并进入 published_hold 持久隔离态",
    )
    apply_cmd.add_argument("backup", type=Path)
    apply_cmd.add_argument("--data-root", type=Path, required=True)
    apply_cmd.add_argument("--expect-plan", required=True, dest="expect_plan")
    apply_cmd.add_argument("--json", action="store_true")

    review_cmd = commands.add_parser(
        "restore-review",
        help="对处于 published_hold 的数据根执行零副作用只读复核（默认全部正文脱敏）",
    )
    review_cmd.add_argument("--data-root", type=Path, required=True)
    review_cmd.add_argument("--batch", default=None, dest="batch_id")
    review_cmd.add_argument("--later-backup", type=Path, default=None, dest="later_backup")
    review_cmd.add_argument(
        "--body-scope",
        action="append",
        default=None,
        dest="body_scopes",
        help="显式披露指定 BodySelectorV1 槽位的原文（可重复）",
    )
    review_cmd.add_argument("--json", action="store_true")

    resume_cmd = commands.add_parser(
        "restore-resume",
        help="续做同一恢复批次（若有 abort_intent 则只续清理，否则推进至 published_hold）",
    )
    resume_cmd.add_argument("--data-root", type=Path, required=True)
    resume_cmd.add_argument("--batch", default=None, dest="batch_id")
    resume_cmd.add_argument("--json", action="store_true")

    abort_cmd = commands.add_parser(
        "restore-abort",
        help="中止尚未进入 publish_intent 的恢复批次并按冻结计划清理暂存目录",
    )
    abort_cmd.add_argument("--data-root", type=Path, required=True)
    abort_cmd.add_argument("--batch", default=None, dest="batch_id")
    abort_cmd.add_argument("--json", action="store_true")

    release_cmd = commands.add_parser(
        "restore-release",
        help="完成只读复核并确认后续遗忘/删除历史不可证明完整后，解除恢复隔离并确定性对账",
    )
    release_cmd.add_argument("--data-root", type=Path, required=True)
    release_cmd.add_argument("--batch", default=None, dest="batch_id")
    release_cmd.add_argument("--expect-review", required=True, dest="expect_review")
    release_cmd.add_argument(
        "--acknowledge-unproven-later-history",
        action="store_true",
        default=False,
        dest="acknowledge_unproven_later_history",
        help="显式确认已知晓备份点之后的 forget/erase/delete 历史不可证明完整",
    )
    release_cmd.add_argument("--json", action="store_true")

    migration = commands.add_parser("migrate", help="使用已验证备份显式迁移标准数据根")
    migration.add_argument("--data-root", type=Path, required=True)
    migration.add_argument("--backup", type=Path, required=True)
    migration.add_argument("--json", action="store_true")
    audit = commands.add_parser(
        "retention-audit",
        help="只读保留期审计：逐表行数/年龄/终态占比/增长投影/cleanup_plan 现状；零删除",
    )
    audit.add_argument("--data-root", type=Path, required=True)
    audit.add_argument("--json", action="store_true")

    gc = commands.add_parser(
        "gc",
        help="保守离线安全 GC：默认只读预演退 3；"
        "显式 --approve 持独占锁清理孤儿 blob 与超期终态作业/已结清预占",
    )
    gc.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="目标数据根目录（或传 --desktop 指向桌面端默认数据根）",
    )
    gc.add_argument(
        "--older-than-days",
        type=int,
        default=90,
        metavar="N",
        help="保留期天数（正整数 >= 1，默认 90）",
    )
    gc.add_argument(
        "--dry-run",
        action="store_true",
        help="只读预演候选清单与预计释放量，零文件修改、零 SQL 写语句（默认行为）",
    )
    gc.add_argument(
        "--approve",
        action="store_true",
        help="显式批准在离线独占维护锁下执行安全清理（与 --dry-run 互斥）",
    )
    gc.add_argument(
        "--desktop",
        action="store_true",
        default=argparse.SUPPRESS,
        help="使用桌面端默认数据目录",
    )
    gc.add_argument("--json", action="store_true")

    diag = commands.add_parser(
        "export-diagnostics",
        help="本地默认脱敏诊断包导出与内容清单预览（D7/F14：仅本地导出，绝不上传）",
    )
    diag.add_argument("--data-root", type=Path, required=True)
    diag.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="导出 zip 的本地目录；与 --preview 二选一",
    )
    diag.add_argument(
        "--preview",
        action="store_true",
        help="只读显示诊断包内容清单与条数估算，不写出任何文件",
    )
    diag.add_argument(
        "--section",
        action="append",
        default=None,
        dest="sections",
        help="要包含的分节（可重复；默认全选）",
    )
    diag.add_argument(
        "--no-redact",
        action="store_true",
        help="显式关闭路径与任务文本脱敏（默认开启脱敏，公开贴 issue 请勿加此项）",
    )
    diag.add_argument("--json", action="store_true")


def _backup(data_root: Path, dest: Path) -> dict[str, Any]:
    root = data_root.resolve()
    db = root / "events.db"
    # 取得外置锁会建锁目录；前置校验必须先做，不能把不存在的根变成新实例。
    if not root.is_dir() or not db.is_file():
        raise BackupRefused(f"数据根不存在、不是目录或没有 events.db：{root}")
    if db.resolve().parent != root:
        raise BackupRefused(f"数据根的 events.db 指向根外，不支持此布局：{db}")
    target = dest.resolve()
    with acquire_restore_database_access(db, exclusive=True):
        manifest = create_backup(root, target)
        verification = verify_backup(target)
        verification.require_ok()
    return {
        "operation": "backup", "ok": True, "backup": str(target),
        "offline": True, "manifest": manifest.to_dict(),
        "verification": verification.to_dict(),
    }


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 必须是非空字符串")
    return value


def _integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    return value


def _reconciliation(
    backup: Path, source_root: Path, *, source_aliases: Sequence[str] = (),
) -> dict[str, Any]:
    # 共用备份材料的路径/哈希判定，绝不按历史绝对引用回源读取。
    from .backup import _connect_saved_readonly, read_saved_event_payload

    result: dict[str, Any] = {
        "pending_intents": [], "pending_approvals": [],
        "non_terminal_runs": [], "live_leases": [],
        "projection_tables": {}, "complete": True,
    }
    # 自包含的已保存快照不能创建 WAL/SHM；活源连接不使用 immutable。
    conn = _connect_saved_readonly(backup / "events.db")
    conn.row_factory = sqlite3.Row
    try:
        # 版本旁表缺失/损坏应阻断预演，不能把所有事件猜成历史版本。
        rows = conn.execute(
            "SELECT e.seq, e.run_id, e.type, e.payload AS payload_inline,"
            " e.payload_ref, e.payload_hash, v.type AS version_type,"
            " COALESCE(v.version, 0) AS version FROM events e"
            " LEFT JOIN event_versions v ON v.seq=e.seq"
            " ORDER BY e.seq"
        )
        intents: list[dict[str, Any]] = []
        last_result: dict[tuple[str, str], int] = {}
        expected_runs: dict[str, tuple[str, str]] = {}
        for row in rows:
            seq = _integer(row["seq"], "events.seq")
            event_type = row["type"]
            if row["version_type"] is not None and row["version_type"] != event_type:
                raise ValueError(f"seq={seq} 的事件类型与版本旁表不一致")
            payload = read_saved_event_payload(
                backup, row, source_root=source_root, source_aliases=source_aliases,
            )
            event_schema.validate_payload(event_type, payload, row["version"])
            if event_type in {"run.start", "run.state", "run.finish", "run.fenced"}:
                run_id = _text(payload.get("run_id"), f"seq={seq} run_id")
                task_id = _text(payload.get("task_id"), f"seq={seq} task_id")
                if run_id != row["run_id"]:
                    raise ValueError(f"seq={seq} 的 Run 身份与事件不一致")
                if event_type == "run.start":
                    if run_id in expected_runs:
                        raise ValueError(f"Run {run_id} 存在重复开始事件")
                    expected_runs[run_id] = (task_id, "created")
                else:
                    prior = expected_runs.get(run_id)
                    if prior is None or prior[0] != task_id:
                        raise ValueError(f"Run {run_id} 的状态事件缺少一致的开始事件")
                    if event_type == "run.state":
                        if payload.get("from") != prior[1]:
                            raise ValueError(f"Run {run_id} 的状态事件不连续")
                        state = payload.get("to")
                    else:
                        state = "fenced" if event_type == "run.fenced" else payload.get("outcome")
                    if state not in RUN_STATES:
                        raise ValueError(f"Run 事件状态无法识别：{state!r}")
                    expected_runs[run_id] = (task_id, state)
            # 全部事件都完成精确版本/字段校验后，才选择工具事件汇总未决调用。
            if event_type not in {"tool.intent", "tool.result"}:
                continue
            run_id = _text(row["run_id"], f"seq={seq} run_id")
            call_id = _text(payload.get("call_id"), f"seq={seq} call_id")
            if event_type == "tool.result":
                last_result[(run_id, call_id)] = seq
            else:
                intents.append({
                    "run_id": run_id, "call_id": call_id, "seq": seq,
                    "tool_id": _text(payload.get("tool_id"), f"seq={seq} tool_id"),
                })
        result["pending_intents"] = [
            intent for intent in intents
            if last_result.get((intent["run_id"], intent["call_id"]), 0) < intent["seq"]
        ]

        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for name in ("approvals", "runs", "leases"):
            result["projection_tables"][name] = "present" if name in tables else "absent"
        missing = sorted({"approvals", "runs", "leases"} - tables)
        if missing:
            raise ValueError(
                f"恢复核对所需投影缺失：{', '.join(missing)}。"
                "无法判定未决审批、Run 和租约，不把缺失状态当成空状态；"
                "EventLog-only 历史备份也须先由匹配版本重建并验证投影。"
            )
        if "approvals" in tables:
            for row in conn.execute(
                "SELECT approval_id, run_id, action, state, resources, expires_at"
                " FROM approvals ORDER BY created_at, approval_id"
            ):
                if row["state"] not in APPROVAL_STATES:
                    raise ValueError(f"审批状态无法识别：{row['state']!r}")
                if row["state"] not in {"pending", "approved"}:
                    continue
                result["pending_approvals"].append({
                    "approval_id": _text(row["approval_id"], "approval_id"),
                    "run_id": _text(row["run_id"], "approvals.run_id"),
                    "action": _text(row["action"], "approvals.action"),
                    # 既有投影存的是逗号拼接文本，原样报告，不虚构 JSON 格式。
                    "state": row["state"],
                    "resources": _text(row["resources"], "approvals.resources"),
                    "expires_at": _integer(row["expires_at"], "approvals.expires_at"),
                })
        if "runs" in tables:
            actual_runs: dict[str, tuple[str, str]] = {}
            for row in conn.execute(
                "SELECT run_id, task_id, state FROM runs ORDER BY created_at, run_id"
            ):
                if row["state"] not in RUN_STATES:
                    raise ValueError(f"Run 状态无法识别：{row['state']!r}")
                run_id = _text(row["run_id"], "runs.run_id")
                task_id = _text(row["task_id"], "runs.task_id")
                actual_runs[run_id] = (task_id, row["state"])
                if row["state"] in NON_TERMINAL_STATES:
                    result["non_terminal_runs"].append({
                        "run_id": run_id,
                        "task_id": task_id,
                        "state": row["state"],
                    })
            if actual_runs != expected_runs:
                raise ValueError(
                    "runs 投影与事件中的 Run 身份/最终状态不一致；"
                    "须由匹配版本重建并验证，不能把缺失或矛盾状态当成空状态"
                )
        if "leases" in tables:
            now = int(time.time() * 1000)
            for row in conn.execute(
                "SELECT resource_key, run_id, owner_pid, owner_instance_id, expires_at"
                " FROM leases ORDER BY resource_key"
            ):
                expires = _integer(row["expires_at"], "leases.expires_at")
                if expires > now:
                    result["live_leases"].append({
                        "resource_key": _text(row["resource_key"], "leases.resource_key"),
                        "run_id": _text(row["run_id"], "leases.run_id"),
                        "owner_pid": _integer(row["owner_pid"], "leases.owner_pid"),
                        "owner_instance_id": row["owner_instance_id"],
                        "expires_at": expires,
                    })
    finally:
        conn.close()
    return result


def _restore_plan(backup: Path, data_root: Path) -> dict[str, Any]:
    from . import restore_manager
    from .restore_contract import NativePathCodecV1

    raw_backup = Path(backup)
    raw_target = Path(data_root)
    resolved_backup = raw_backup.resolve()
    resolved_target = raw_target.resolve()
    verification = verify_backup(raw_backup)
    report: dict[str, Any] = {
        "operation": "restore-plan", "ok": False, "apply_supported": False,
        "backup": str(resolved_backup), "data_root": str(resolved_target), "source_path": None,
        "same_path": False, "target_clean": False,
        "verification": verification.to_dict(), "problems": list(verification.problems),
        "memory_reactivation": "requires_quarantine_and_review",
        "later_forget_delete_history": "completeness_unproven",
        "reconciliation": {
            "pending_intents": [], "pending_approvals": [],
            "non_terminal_runs": [], "live_leases": [], "complete": False,
        },
        "notes": [
            "只读预演；不会创建恢复目标、执行恢复、启动服务或激活记忆。",
            "备份无法证明其创建后发生的 forget / erase / delete 历史完整。",
            "记忆恢复须先隔离并人工复核；备份校验成功不代表可安全激活。",
            "未决 intent 不可盲目重放；有效期未过的租约记录不证明原进程仍存活。",
        ],
    }
    try:
        try:
            restore_manager._inspect_original_target(resolved_target, NativePathCodecV1())
            report["target_clean"] = not raw_target.is_symlink() and not raw_target.is_junction()
        except restore_manager.RestoreOperationError:
            report["target_clean"] = False
        if not report["target_clean"]:
            report["problems"].append("恢复目标不是干净根：只接受不存在或全空目录骨架")
        manifest = verification.manifest
        if manifest is not None:
            source = Path(manifest.source_path)
            report.update({
                "source_path": manifest.source_path,
                "format_version": manifest.format_version,
                "db_user_version": manifest.db_user_version,
                "supported_db_user_version": SCHEMA_VERSION,
                "migration_required": manifest.db_user_version < SCHEMA_VERSION,
            })
            if not source.is_absolute():
                report["problems"].append("备份原数据根不是绝对路径，无法判定恢复位置")
            else:
                allowed = {overlap_key(str(source))} | {
                    overlap_key(alias)
                    for alias in manifest.source_aliases
                    if isinstance(alias, str) and Path(alias).is_absolute()
                }
                report["same_path"] = (
                    not raw_target.is_symlink()
                    and not raw_target.is_junction()
                    and overlap_key(str(resolved_target)) in allowed
                )
                if not report["same_path"]:
                    report["problems"].append("恢复目标与备份原数据根不同；尚不支持换路径恢复")
            if manifest.db_user_version > SCHEMA_VERSION:
                report["problems"].append("备份结构版本高于本程序；需要匹配版本的程序")
            if verification.ok and not report["problems"]:
                planned = restore_manager.plan(raw_backup, raw_target)
                report["reconciliation"] = planned.legacy_reconciliation.to_legacy_dict()
                report["plan_sha256"] = planned.plan_sha256
                report["plan"] = planned.to_dict()
                report["apply_supported"] = planned.apply_supported
    except restore_manager.RestoreOperationError as exc:
        report["problems"].append(f"无法完成恢复核对：{exc.failure.message}")
    except (BackupError, TianshuError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        report["problems"].append(f"无法完成恢复核对：{exc}")
    report["ok"] = not report["problems"]
    if not report["ok"]:
        report["apply_supported"] = False
    return report


class RetentionAuditRefused(TianshuError):
    """保留期审计拒绝；只读审计不做任何补救，拒绝即如实报告。"""


# CONTRACT-W15 §2 的逐面保护角色：如实展示每张表为什么不在候选集里。
# 表/列名与角色全是模块级封闭常量；SQL 全部为静态字面量，值一律参数绑定。
# ts = 毫秒时间戳列（None = 不报年龄）；state = 状态列（None = 不报直方图）。
_AUDIT_TABLES: dict[str, dict[str, Any]] = {
    "events": {"ts": "ts", "state": None, "protection": "红线 1 append-only，禁删"},
    "event_versions": {"ts": None, "state": None, "protection": "与 events 同事务写入的版本旁表"},
    "runs": {"ts": "created_at", "state": "state", "protection": "非终态行禁删（红线 2/6）"},
    "approvals": {
        "ts": "created_at", "state": "state",
        "protection": "一次性审批凭据（digest 绑定）",
    },
    "dispatches": {
        "ts": "created_at", "state": "state",
        "protection": "红线 2/6 恢复语义（boundary_protocol/intent_seq"
        " 是 none_applied 判定组成部分）",
    },
    "dispatch_requests": {
        "ts": "created_at", "state": "state",
        "protection": "终态脱敏（args_redacted）；审计证据（digest/reason/rule_id）",
    },
    "dispatch_meta": {"ts": None, "state": None, "protection": "派发元数据"},
    "summary_materials": {"ts": "created_at", "state": None, "protection": "回放三道校验依赖"},
    "summary_sources": {"ts": None, "state": None, "protection": "回放依赖（内容寻址共享）"},
    "summary_versions": {
        "ts": "created_at", "state": "status",
        "protection": "BEFORE DELETE 触发器 + 回放三道校验依赖",
    },
    "budget_accounts": {"ts": None, "state": None, "protection": "计费事实（聚合行）"},
    "budget_limits": {"ts": None, "state": None, "protection": "计费事实"},
    "budget_counters": {
        "ts": None, "state": None,
        "protection": "计费事实（reserved/settled 计数）",
    },
    "budget_reservations": {
        "ts": "created_at", "state": "state",
        "protection": "known 三态不合并（红线 5/11），未决行禁删",
    },
    "jobs": {
        "ts": "created_at", "state": "state",
        "protection": "outcome/output_ref 与 Receipt 关联",
    },
    "leases": {
        "ts": "acquired_at", "state": None,
        "protection": "release 即删行自清理（无增长问题）",
    },
    "memories": {
        "ts": "created_at", "state": "status",
        "protection": "用户数据；擦除只走显式 memory.erase",
    },
    "memory_meta": {"ts": None, "state": None, "protection": "记忆投影元数据"},
    "memory_vectors": {"ts": None, "state": None, "protection": "派生索引（rebuild 可重建）"},
    "content_objects": {
        "ts": "created_at", "state": None,
        "protection": "引用计数保护（cleanup_plan）",
    },
    "content_grants": {
        "ts": "created_at", "state": None, "protection": "按任务授权",
    },
    "migration_log": {
        "ts": "applied_at", "state": None,
        "protection": "append-only 迁移审计（行数=版本数）",
    },
    "schedules": {
        "ts": "created_at", "state": None,
        "protection": "W5 调度定义（enabled 由 CLI 管理）",
    },
    "schedules_triggers_v1": {
        "ts": "created_at", "state": None,
        "protection": "日历、本机目录和本机事件触发配置及监听基线",
    },
    "schedule_event_receipts_v1": {
        "ts": "created_at", "state": None,
        "protection": "事件触发去重记录（随对应调度定义删除）",
    },
}

# 增长投影只对 W15 §3.2 点名的增长面 + runs/summary_versions 两个记录面做。
_AUDIT_GROWTH_TABLES = (
    "events", "dispatches", "dispatch_requests", "budget_reservations",
    "runs", "summary_versions",
)
_FTS_SHADOW_SUFFIXES = {"data", "idx", "docsize", "config"}


def _audit_basics(conn: sqlite3.Connection, name: str) -> tuple[int, int | None, int | None]:
    """COUNT + MIN/MAX 时间列；SQL 全部字面量，标识符不进任何拼接。"""
    if name == "events":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM events",
        ).fetchone()
    elif name == "event_versions":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM event_versions",
        ).fetchone()
    elif name == "runs":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo, MAX(created_at) AS hi FROM runs",
        ).fetchone()
    elif name == "approvals":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo, MAX(created_at) AS hi FROM approvals",
        ).fetchone()
    elif name == "dispatches":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM dispatches",
        ).fetchone()
    elif name == "dispatch_requests":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM dispatch_requests",
        ).fetchone()
    elif name == "dispatch_meta":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM dispatch_meta",
        ).fetchone()
    elif name == "summary_materials":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM summary_materials",
        ).fetchone()
    elif name == "summary_sources":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM summary_sources",
        ).fetchone()
    elif name == "summary_versions":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM summary_versions",
        ).fetchone()
    elif name == "budget_accounts":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM budget_accounts",
        ).fetchone()
    elif name == "budget_limits":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM budget_limits",
        ).fetchone()
    elif name == "budget_counters":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM budget_counters",
        ).fetchone()
    elif name == "budget_reservations":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM budget_reservations",
        ).fetchone()
    elif name == "jobs":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM jobs",
        ).fetchone()
    elif name == "leases":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(acquired_at) AS lo,"
            " MAX(acquired_at) AS hi FROM leases",
        ).fetchone()
    elif name == "memories":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM memories",
        ).fetchone()
    elif name == "memory_meta":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM memory_meta",
        ).fetchone()
    elif name == "memory_vectors":
        row = conn.execute(
            "SELECT COUNT(*) AS n, NULL AS lo, NULL AS hi FROM memory_vectors",
        ).fetchone()
    elif name == "content_objects":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM content_objects",
        ).fetchone()
    elif name == "content_grants":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM content_grants",
        ).fetchone()
    elif name == "migration_log":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(applied_at) AS lo,"
            " MAX(applied_at) AS hi FROM migration_log",
        ).fetchone()
    elif name == "schedules":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM schedules",
        ).fetchone()
    elif name == "schedules_triggers_v1":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM schedules_triggers_v1",
        ).fetchone()
    elif name == "schedule_event_receipts_v1":
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS lo,"
            " MAX(created_at) AS hi FROM schedule_event_receipts_v1",
        ).fetchone()
    else:
        raise RetentionAuditRefused("未登记的表没有审计 SQL：" + name)
    return int(row["n"]), row["lo"], row["hi"]


def _audit_histogram(conn: sqlite3.Connection, name: str) -> dict[str, int] | None:
    """状态直方图（单行字面量 SQL）；未登记状态列的表返回 None。"""
    hist: dict[str, int] | None = None
    if name == "runs":
        rows = conn.execute("SELECT state AS s, COUNT(*) AS n FROM runs GROUP BY state").fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "approvals":
        rows = conn.execute(
            "SELECT state AS s, COUNT(*) AS n FROM approvals GROUP BY state",
        ).fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "dispatches":
        rows = conn.execute(
            "SELECT state AS s, COUNT(*) AS n FROM dispatches GROUP BY state",
        ).fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "dispatch_requests":
        rows = conn.execute(
            "SELECT state AS s, COUNT(*) AS n FROM dispatch_requests GROUP BY state",
        ).fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "summary_versions":
        rows = conn.execute(
            "SELECT status AS s, COUNT(*) AS n FROM summary_versions GROUP BY status",
        ).fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "budget_reservations":
        rows = conn.execute(
            "SELECT state AS s, COUNT(*) AS n FROM budget_reservations GROUP BY state",
        ).fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "jobs":
        rows = conn.execute("SELECT state AS s, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    elif name == "memories":
        rows = conn.execute(
            "SELECT status AS s, COUNT(*) AS n FROM memories GROUP BY status",
        ).fetchall()
        hist = {r["s"]: r["n"] for r in rows}
    return hist


def _connect_audit_readonly(db: Path) -> sqlite3.Connection:
    # 只读 URI 连接：SQLite 在连接层拒绝一切写语句；不加维护锁、不阻断写入。
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _absent_row(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "present": False, "row_count": None, "oldest_age_days": None,
        "newest_age_days": None, "state_histogram": None,
        "protection": spec["protection"],
    }


def _age_days(ts: int, now_ms: int) -> float:
    return round((now_ms - ts) / 86_400_000, 2)


def _audit_table(
    conn: sqlite3.Connection, name: str, now_ms: int,
) -> tuple[dict[str, Any], int, int | None, int | None]:
    """返回（报告行, 行数, 最老/最新毫秒时间戳）；时间戳供增长投影复用。"""
    n, lo, hi = _audit_basics(conn, name)
    out: dict[str, Any] = {}
    out["present"] = True
    out["row_count"] = n
    out["oldest_age_days"] = None
    out["newest_age_days"] = None
    out["state_histogram"] = None
    out["protection"] = _AUDIT_TABLES[name]["protection"]
    if lo is not None:
        out["oldest_age_days"] = _age_days(int(lo), now_ms)
    if hi is not None:
        out["newest_age_days"] = _age_days(int(hi), now_ms)
    if n:
        out["state_histogram"] = _audit_histogram(conn, name)
    return out, n, lo, hi


def _audit_growth(
    basics: dict[str, tuple[int, int | None, int | None]],
) -> dict[str, Any]:
    """增长投影：按表内历史平均速率直线外推；跨度不足或行数不足则不外推。"""
    out: dict[str, Any] = {}
    for name in _AUDIT_GROWTH_TABLES:
        entry: dict[str, Any] = {}
        entry["rows_per_day"] = None
        entry["projection_30d"] = None
        entry["projection_90d"] = None
        n, lo, hi = basics.get(name, (0, None, None))
        # 观测窗口不足 1 天不外推：几毫秒的跨度会外推出垃圾速率。
        if n and lo is not None and hi is not None and (hi - lo) >= 86_400_000:
            span_days = (hi - lo) / 86_400_000
            rate = n / span_days
            entry["rows_per_day"] = round(rate, 2)
            entry["projection_30d"] = int(n + rate * 30)
            entry["projection_90d"] = int(n + rate * 90)
        out[name] = entry
    return out


def _audit_candidates(conn: sqlite3.Connection, report_tables: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["criteria_note"] = (
        "候选测量≠可删判定；完整标准见 CONTRACT-W15 §3.3："
        "终态 + Receipt 对账通过 + 无恢复依赖 + 不在触发器/红线保护集"
        " +（若做归档）归档校验通过。"
    )
    out["jobs_terminal_rows"] = None
    out["budget_reservations_settled_known_rows"] = None
    out["non_terminal_runs"] = None
    jobs = report_tables.get("jobs")
    if jobs and jobs.get("present") and jobs.get("state_histogram") is not None:
        hist = jobs["state_histogram"]
        out["jobs_terminal_rows"] = sum(hist.get(s, 0) for s in TERMINAL_JOB_STATES)
    budget = report_tables.get("budget_reservations")
    if budget and budget.get("present"):
        rows = conn.execute(
            "SELECT state AS s, known AS k, COUNT(*) AS n"
            " FROM budget_reservations GROUP BY state, known",
        ).fetchall()
        out["budget_reservations_settled_known_rows"] = sum(
            r["n"] for r in rows if r["s"] == SETTLED and r["k"] == 1
        )
    runs = report_tables.get("runs")
    if runs and runs.get("present") and runs.get("state_histogram") is not None:
        hist = runs["state_histogram"]
        out["non_terminal_runs"] = sum(hist.get(s, 0) for s in NON_TERMINAL_STATES)
    return out


def _retention_audit(data_root: Path) -> dict[str, Any]:
    from .restore_access import ensure_path_not_held

    ensure_path_not_held(data_root)
    root = data_root.resolve()
    db = root / "events.db"
    # 与 backup 同款前置校验：不能把不存在的根变成新实例。
    if not root.is_dir() or not db.is_file():
        raise RetentionAuditRefused("数据根不存在、不是目录或没有 events.db：" + str(root))
    if db.resolve().parent != root:
        raise RetentionAuditRefused("数据根的 events.db 指向根外，不支持此布局：" + str(db))
    now_ms = int(time.time() * 1000)
    try:
        conn = _connect_audit_readonly(db)
    except sqlite3.Error as exc:
        raise RetentionAuditRefused("无法以只读方式打开 events.db：" + str(exc)) from exc
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }
        known = {t for t in tables if not t.startswith("sqlite_")}
        shadow = {
            t for t in known - set(_AUDIT_TABLES)
            if "_" in t and t.rsplit("_", 1)[0] in _AUDIT_TABLES
            and t.rsplit("_", 1)[1] in _FTS_SHADOW_SUFFIXES
        }
        unknown = sorted(known - set(_AUDIT_TABLES) - shadow)
        report_tables: dict[str, Any] = {}
        basics: dict[str, tuple[int, int | None, int | None]] = {}
        for name in _AUDIT_TABLES:
            if name in tables:
                row_out, n, lo, hi = _audit_table(conn, name, now_ms)
                report_tables[name] = row_out
                basics[name] = (n, lo, hi)
            else:
                report_tables[name] = _absent_row(_AUDIT_TABLES[name])
        report = {}
        report["operation"] = "retention-audit"
        report["ok"] = True
        report["data_root"] = str(root)
        report["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        report["tables"] = report_tables
        report["unknown_tables"] = unknown
        report["growth"] = _audit_growth(basics)
        report["candidates"] = _audit_candidates(conn, report_tables)
        report["notes"] = [
            "只读点读快照：mode=ro 连接，不加锁、不阻断写入，零删除。",
            "增长投影按表内历史平均速率直线外推（观测窗口 ≥1 天才外推），不是承诺。",
            "候选测量回答『有多少行满足终态条件』；可删判定还需 Receipt 对账"
            "与恢复依赖核验（CONTRACT-W15 §3.3）。",
        ]
    finally:
        conn.close()
    try:
        report["cleanup_plan"] = cleanup_plan(root)
    except (BackupError, TianshuError, OSError) as exc:
        report["cleanup_plan"] = {"dry_run": True, "error": "无法生成候选清单：" + str(exc)}
    return report


def _print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(redact(json.dumps(report, ensure_ascii=False, indent=2)))
        return
    operation = report["operation"]
    if operation == "backup":
        print(f"离线备份已生成并验证：{report['backup']}")
    elif operation == "verify":
        print("备份校验通过" if report["ok"] else "备份校验失败")
    elif operation == "migrate":
        print(redact(report["message"] if report["ok"] else report["error"]))
    elif operation == "retention-audit":
        print("保留期审计（只读快照，零删除）：" + str(report["data_root"]))
        print("结构版本：" + str(report["schema_version"]))
        for name in _AUDIT_GROWTH_TABLES:
            t = report["tables"].get(name, {})
            g = report["growth"][name]
            count = t.get("row_count")
            line = str(name) + "：" + (str(count) + " 行" if count is not None else "缺表")
            if g["rows_per_day"] is not None:
                line += "，≈" + str(g["rows_per_day"]) + " 行/天"
                line += "，30 天投影 ≈" + str(g["projection_30d"])
            print(line)
        c = report["candidates"]
        print("jobs 终态行（候选测量）：" + str(c["jobs_terminal_rows"]))
        print("budget_reservations settled+known（候选测量）："
              + str(c["budget_reservations_settled_known_rows"]))
        print("非终态 Run（禁删）：" + str(c["non_terminal_runs"]))
        cp = report["cleanup_plan"]
        if "error" in cp:
            print("cleanup_plan：" + cp["error"])
        else:
            print("cleanup_plan 候选 blob：" + str(len(cp["candidates"]))
                  + "（受保护 " + str(cp["protected_count"]) + "）")
        if report["unknown_tables"]:
            print("未识别表：" + ", ".join(report["unknown_tables"]))
    elif operation == "gc":
        if not report["ok"]:
            msg = redact(str(report.get("error") or "离线安全 GC 拒绝"))
            print(f"错误：{msg}", file=sys.stderr)
            deleted = report["deleted"]
            if any(deleted.values()):
                print(
                    "  已确认删除："
                    f"孤儿 blob：{deleted['orphan_blobs_count']} 个"
                    f"（{deleted['orphan_blobs_bytes']} 字节）；"
                    f"空快照：{deleted['empty_snapshots_count']} 个"
                    f"（{deleted['empty_snapshots_bytes']} 字节）；"
                    f"jobs 行：{deleted['jobs_count']} 行；"
                    f"预算预占：{deleted['budget_reservations_count']} 行。",
                    file=sys.stderr,
                )
            for note in report["notes"]:
                print(note, file=sys.stderr)
            return
        cand = report["candidates"]
        if report["dry_run"]:
            days = report["older_than_days"]
            print(f"离线安全 GC 预演（保留期 > {days} 天，零删除）：{report['data_root']}")
            print(
                f"  孤儿 blob 候选：{cand['orphan_blobs_count']} 个"
                f"（{cand['orphan_blobs_bytes']} 字节）"
            )
            print(
                f"  超期空终态快照候选：{cand['empty_snapshots_count']} 个"
                f"（{cand['empty_snapshots_bytes']} 字节）"
            )
            print(f"  超期终态 jobs 行候选：{cand['jobs_count']} 行")
            print(
                "  超期 settled+known 预算预占候选："
                f"{cand['budget_reservations_count']} 行"
            )
            print(f"  预计释放文件字节：{cand['reclaimable_file_bytes']}")
        else:
            deleted = report["deleted"]
            days = report["older_than_days"]
            print(f"离线安全 GC 已完成（保留期 > {days} 天）：{report['data_root']}")
            print(
                f"  已清理孤儿 blob：{deleted['orphan_blobs_count']} 个"
                f"（{deleted['orphan_blobs_bytes']} 字节）"
            )
            print(
                f"  已清理超期空终态快照：{deleted['empty_snapshots_count']} 个"
                f"（{deleted['empty_snapshots_bytes']} 字节）"
            )
            print(f"  已清理超期终态 jobs 行：{deleted['jobs_count']} 行")
            print(
                "  已清理超期 settled+known 预算预占："
                f"{deleted['budget_reservations_count']} 行"
            )
            print(f"  已释放文件字节：{deleted['reclaimed_file_bytes']}")
    elif operation == "export-diagnostics":
        manifest = report["manifest"]
        if report.get("preview_only"):
            print("诊断包内容预览（未写出文件，本地默认脱敏）：")
        else:
            print(f"已导出本地诊断包：{report['archive_path']}")
        print(f"默认脱敏：{'开启' if manifest['redacted'] else '已显式关闭（请勿公开贴出）'}")
        for item in manifest["sections"]:
            mark = "✓" if item["included"] else "×"
            print(
                f"  [{mark}] {item['section']}: {item['record_count']} 条"
                f"（约 {item['estimated_bytes']}B）— {item['description']}"
            )
    else:
        print("恢复预演通过，仍需人工复核；未执行恢复" if report["ok"] else "恢复预演未通过")
        print(f"备份：{report['backup']}")
        print(f"原数据根：{report['source_path'] or '无法读取'}")
        print(f"恢复目标（解析链接后）：{report['data_root']}")
        print(f"与原路径一致：{'是' if report['same_path'] else '否'}")
        print(f"目标为干净根：{'是' if report['target_clean'] else '否'}")
        print(f"备份完整性：{'通过' if report['verification']['ok'] else '失败'}")
        if "format_version" in report:
            print(f"备份格式版本：{report['format_version']}")
            print(f"结构版本：{report['db_user_version']}；"
                  f"本程序支持：{report['supported_db_user_version']}")
            if report["migration_required"]:
                print("恢复后仍需迁移；本命令不执行迁移。")
        if report["ok"]:
            reconciliation = report["reconciliation"]
            for key, label in (
                ("pending_intents", "未决 intent"),
                ("pending_approvals", "未决审批"),
                ("non_terminal_runs", "非终态 Run"),
                ("live_leases", "有效期未过的租约记录"),
            ):
                print(f"{label}：{len(reconciliation[key])}")
    for problem in report.get("problems", []):
        print(f"- {redact(problem)}")
    for note in report.get("notes", []):
        print(note)


def _redact_restore_failure(failure: Any) -> Any:
    from dataclasses import replace

    redacted_cause = (
        replace(failure.cause, message=redact(failure.cause.message))
        if failure.cause is not None
        else None
    )
    return replace(failure, message=redact(failure.message), cause=redacted_cause)


def _print_restore_response(
    response: Any, *, as_json: bool, has_disclosed_body: bool = False
) -> None:
    if as_json:
        _ = has_disclosed_body
        print(response.to_bytes().decode("ascii"))
        return
    payload = response.to_dict()
    op = payload["operation"]
    if payload["ok"]:
        res = payload["result"]
        if op == "restore-review":
            print(
                f"只读恢复复核通过（仍处于隔离态 published_hold）：batch={res['batch_id']}"
            )
            print(f"数据根：{res['data_root']['text']}")
            if "review_sha256" in res:
                print(f"Review SHA256：{res['review_sha256']}")
            print(f"Binding SHA256：{res['binding_sha256']}")
            print(f"Scope SHA256：{res['scope_sha256']}")
            print(f"对象总数：{len(res['objects'])}；已知后续变化：{len(res['known_later_changes'])}")
            print(f"后续遗忘/擦除/删除历史：{res['later_forget_delete_history']}")
        elif op == "restore-release":
            print(
                f"restore-release 完成：batch={res['batch_id']} "
                f"phase={res['phase']} held={res['held']}"
            )
            print(f"数据根：{res['data_root']['text']}")
            print(
                f"数据库版本：v{res['db_user_version']}；"
                f"解除后准入：{res['post_release_admission']}"
            )
            for cmd in res.get("next_commands", []):
                print("下一步命令：" + " ".join(cmd))
        else:
            print(
                f"{op} 完成：batch={res['batch_id']} phase={res['phase']} held={res['held']}"
            )
            print(f"数据根：{res['data_root']['text']}")
            if res.get("review_command"):
                print("下一步复核命令：" + " ".join(res["review_command"]))
    else:
        err = payload["error"]
        print(redact(f"{op} 失败 [{err['category']}]：{err['message']}"))
        if err.get("batch_id"):
            print(f"批次：{err['batch_id']}（阶段：{err['phase']}）")
        for cmd in err.get("next_commands", []):
            print("建议命令：" + " ".join(cmd))


def run(args: argparse.Namespace) -> int:
    if args.data_action in (
        "restore-apply",
        "restore-review",
        "restore-resume",
        "restore-abort",
        "restore-release",
    ):
        from . import restore_manager
        from .restore_contract import (
            RestoreCauseV1,
            RestoreFailureV1,
            RestoreFailureV2,
            RestoreResponseV1,
            RestoreResponseV2,
        )

        op = args.data_action
        use_v2 = op in ("restore-review", "restore-release")
        has_disclosed = False
        try:
            if op == "restore-apply":
                res = restore_manager.apply(
                    args.backup,
                    args.data_root,
                    expect_plan_sha256=args.expect_plan,
                )
            elif op == "restore-review":
                scopes = tuple(args.body_scopes or ())
                has_disclosed = bool(scopes)
                res = restore_manager.review(
                    args.data_root,
                    batch_id=args.batch_id,
                    later_backup=args.later_backup,
                    body_scopes=scopes,
                )
            elif op == "restore-release":
                res = restore_manager.release(
                    args.data_root,
                    batch_id=args.batch_id,
                    expect_review_sha256=args.expect_review,
                    acknowledge_unproven_later_history=bool(
                        args.acknowledge_unproven_later_history
                    ),
                )
            elif op == "restore-resume":
                res = restore_manager.resume(args.data_root, batch_id=args.batch_id)
            else:
                res = restore_manager.abort(args.data_root, batch_id=args.batch_id)
            response = (
                RestoreResponseV2(version=2, operation=op, ok=True, result=res, error=None)
                if use_v2
                else RestoreResponseV1(version=1, operation=op, ok=True, result=res, error=None)
            )
            _print_restore_response(response, as_json=args.json, has_disclosed_body=has_disclosed)
            return 0
        except restore_manager.RestoreOperationError as exc:
            red_err = _redact_restore_failure(exc.failure)
            if use_v2 or isinstance(red_err, RestoreFailureV2):
                err_v2 = (
                    red_err
                    if isinstance(red_err, RestoreFailureV2)
                    else RestoreFailureV2.from_v1(red_err)
                )
                response = RestoreResponseV2(
                    version=2,
                    operation=op,
                    ok=False,
                    result=exc.partial_review if op == "restore-review" else None,
                    error=err_v2,
                )
            else:
                response = RestoreResponseV1(
                    version=1,
                    operation=op,
                    ok=False,
                    result=None,
                    error=red_err,
                )
            _print_restore_response(response, as_json=args.json, has_disclosed_body=False)
            return 2
        except (TianshuError, OSError, ValueError) as exc:
            fallback_v1 = RestoreFailureV1(
                category="restore_failed",
                message=redact(str(exc)),
                batch_id=None,
                phase=None,
                plan_sha256=None,
                retained_paths=(),
                next_commands=(),
                cause=RestoreCauseV1(type(exc).__name__, redact(str(exc))),
            )
            response = (
                RestoreResponseV2(
                    version=2,
                    operation=op,
                    ok=False,
                    result=None,
                    error=RestoreFailureV2.from_v1(fallback_v1),
                )
                if use_v2
                else RestoreResponseV1(
                    version=1, operation=op, ok=False, result=None, error=fallback_v1
                )
            )
            _print_restore_response(response, as_json=args.json, has_disclosed_body=False)
            return 2

    if args.data_action == "backup":
        report = _backup(args.data_root, args.dest)
        code = 0
    elif args.data_action == "verify":
        report = {"operation": "verify", **verify_backup(args.backup).to_dict()}
        code = 0 if report["ok"] else 1
    elif args.data_action == "migrate":
        from .restore_access import RestoreScopeBusy

        try:
            migration = run_migration(args.data_root, backup=args.backup)
        except AdmissionRefused as exc:
            report = {
                "operation": "migrate", "ok": False, "code": exc.code,
                "error": str(exc),
            }
            code = 2
        except RestoreScopeBusy as exc:
            report = {
                "operation": "migrate", "ok": False, "code": "held",
                "error": str(exc),
            }
            code = 2
        else:
            report = {
                "operation": "migrate", "ok": True, "code": None,
                **migration.to_dict(), "message": migration.to_text(),
            }
            code = 0
    elif args.data_action == "retention-audit":
        report = _retention_audit(args.data_root)
        code = 0
    elif args.data_action == "gc":
        from .data_gc import run_data_gc

        report, code = run_data_gc(
            args.data_root,
            older_than_days=args.older_than_days,
            dry_run=bool(args.dry_run),
            approve=bool(args.approve),
        )
    elif args.data_action == "export-diagnostics":
        from .diagnostics_export import export_diagnostics, preview_diagnostics

        redact_flag = not bool(getattr(args, "no_redact", False))
        if args.preview:
            manifest = preview_diagnostics(
                args.data_root, sections=args.sections, redact_sensitive=redact_flag
            )
            report = {
                "operation": "export-diagnostics",
                "ok": True,
                "preview_only": True,
                "manifest": manifest,
            }
        else:
            if args.out_dir is None:
                raise TianshuError("导出诊断包必须指定 --out-dir（或传 --preview 仅预览清单）")
            exported = export_diagnostics(
                args.data_root,
                args.out_dir,
                sections=args.sections,
                redact_sensitive=redact_flag,
            )
            report = {
                "operation": "export-diagnostics",
                "preview_only": False,
                **exported,
            }
        code = 0
    else:
        report = _restore_plan(args.backup, args.data_root)
        code = 3 if report["ok"] else 2
    _print_report(report, as_json=args.json)
    return code
