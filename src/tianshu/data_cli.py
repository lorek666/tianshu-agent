"""显式数据根的离线备份、独立验证、迁移与只读恢复预演。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import event_schema
from .backup import BackupError, BackupRefused, create_backup, verify_backup
from .credentials import redact
from .data_state import AdmissionRefused
from .errors import TianshuError
from .maintenance import acquire_database_access
from .migrate import SCHEMA_VERSION
from .migrate import migrate as run_migration
from .pathkeys import overlap_key
from .permissions import APPROVAL_STATES
from .tasks import NON_TERMINAL_STATES, RUN_STATES


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("data", help="数据维护：离线备份 / 验证 / 迁移 / 只读恢复预演")
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
    migration = commands.add_parser("migrate", help="使用已验证备份显式迁移标准数据根")
    migration.add_argument("--data-root", type=Path, required=True)
    migration.add_argument("--backup", type=Path, required=True)
    migration.add_argument("--json", action="store_true")


def _backup(data_root: Path, dest: Path) -> dict[str, Any]:
    root = data_root.resolve()
    db = root / "events.db"
    # 取得外置锁会建锁目录；前置校验必须先做，不能把不存在的根变成新实例。
    if not root.is_dir() or not db.is_file():
        raise BackupRefused(f"数据根不存在、不是目录或没有 events.db：{root}")
    if db.resolve().parent != root:
        raise BackupRefused(f"数据根的 events.db 指向根外，不支持此布局：{db}")
    target = dest.resolve()
    with acquire_database_access(db, exclusive=True):
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
            " WHERE e.type IN ('tool.intent','tool.result') ORDER BY e.seq"
        )
        intents: list[dict[str, Any]] = []
        last_result: dict[tuple[str, str], int] = {}
        for row in rows:
            seq = _integer(row["seq"], "events.seq")
            event_type = row["type"]
            if row["version_type"] is not None and row["version_type"] != event_type:
                raise ValueError(f"seq={seq} 的事件类型与版本旁表不一致")
            payload = read_saved_event_payload(
                backup, row, source_root=source_root, source_aliases=source_aliases,
            )
            event_schema.validate_payload(event_type, payload, row["version"])
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
            for row in conn.execute(
                "SELECT run_id, task_id, state FROM runs ORDER BY created_at, run_id"
            ):
                if row["state"] not in RUN_STATES:
                    raise ValueError(f"Run 状态无法识别：{row['state']!r}")
                if row["state"] in NON_TERMINAL_STATES:
                    result["non_terminal_runs"].append({
                        "run_id": _text(row["run_id"], "runs.run_id"),
                        "task_id": _text(row["task_id"], "runs.task_id"),
                        "state": row["state"],
                    })
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
    backup = backup.resolve()
    target = data_root.resolve()
    verification = verify_backup(backup)
    report: dict[str, Any] = {
        "operation": "restore-plan", "ok": False, "apply_supported": False,
        "backup": str(backup), "data_root": str(target), "source_path": None,
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
        report["target_clean"] = not target.exists() or (
            target.is_dir() and not any(target.iterdir())
        )
        if not report["target_clean"]:
            report["problems"].append("恢复目标不是干净根：只接受不存在或空目录")
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
                # source_path 已在备份创建时 canonicalize；不能再跟随活源链接，
                # 否则原根被替换成链接会把不同恢复目标误判为同一路径。
                report["same_path"] = overlap_key(str(source)) == overlap_key(
                    str(target)
                )
                if not report["same_path"]:
                    report["problems"].append("恢复目标与备份原数据根不同；尚不支持换路径恢复")
            if manifest.db_user_version > SCHEMA_VERSION:
                report["problems"].append("备份结构版本高于本程序；需要匹配版本的程序")
            if verification.ok and not report["problems"]:
                report["reconciliation"] = _reconciliation(
                    backup, source, source_aliases=manifest.source_aliases,
                )
    except (BackupError, TianshuError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        report["problems"].append(f"无法完成恢复核对：{exc}")
    report["ok"] = not report["problems"]
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


def run(args: argparse.Namespace) -> int:
    if args.data_action == "backup":
        report = _backup(args.data_root, args.dest)
        code = 0
    elif args.data_action == "verify":
        report = {"operation": "verify", **verify_backup(args.backup).to_dict()}
        code = 0 if report["ok"] else 1
    elif args.data_action == "migrate":
        try:
            migration = run_migration(args.data_root, backup=args.backup)
        except AdmissionRefused as exc:
            report = {
                "operation": "migrate", "ok": False, "code": exc.code,
                "error": str(exc),
            }
            code = 2
        else:
            report = {
                "operation": "migrate", "ok": True, "code": None,
                **migration.to_dict(), "message": migration.to_text(),
            }
            code = 0
    else:
        report = _restore_plan(args.backup, args.data_root)
        code = 3 if report["ok"] else 2
    _print_report(report, as_json=args.json)
    return code
