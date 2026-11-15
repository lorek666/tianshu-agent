#!/usr/bin/env python3
"""在原生 Windows runner 上产出恢复 T1 句柄与锁证据。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from tianshu import maintenance, restore_access
from tianshu.events import EventLog
from tianshu.maintenance import (
    MaintenanceBusy,
    acquire_entity_access,
)
from tianshu.platform_identity import observe_entity, open_entity
from tianshu.restore_registry import RestoreRegistry


def _expect_busy(action) -> bool:
    try:
        action()
    except MaintenanceBusy:
        return True
    return False


def _expect_permission_error(action) -> bool:
    try:
        action()
    except PermissionError:
        return True
    return False


def run_probe() -> dict[str, object]:
    if os.name != "nt":
        raise SystemExit("restore T1 Windows probe must run on Windows")

    with tempfile.TemporaryDirectory(prefix="tianshu-restore-t1-") as raw:
        root = Path(raw)
        lock_root = root / "locks"
        home = root / "home"
        home.mkdir()
        maintenance._lock_root = lambda: lock_root

        registry = RestoreRegistry._for_home(home)
        registry_observation = registry.initialize(
            registry_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        restore_access._registry_factory = lambda: registry

        database = root / "data" / "events.db"
        database.parent.mkdir()
        database.write_bytes(b"")
        directory_handle = open_entity(database.parent, "directory")
        database_handle = open_entity(database, "file")
        try:
            first_identity = database_handle.identity
            first_file_id = database_handle.identity.file_id_hex
            first_volume_serial = database_handle.identity.volume_serial

            alias = root / "events-alias.db"
            os.link(database, alias)
            hardlink_identity_equal = (
                observe_entity(alias, "file") == first_identity
            )

            moved = root / "events-original.db"
            database.rename(moved)
            database.write_bytes(b"replacement")
            replacement_identity_changed = (
                observe_entity(database, "file") != first_identity
            )
            database.unlink()
            moved.rename(database)
        finally:
            database_handle.close()

        shared_first = acquire_entity_access(first_identity)
        shared_second = acquire_entity_access(first_identity)
        exclusive_blocked = _expect_busy(
            lambda: acquire_entity_access(first_identity, exclusive=True)
        )
        shared_first.close()
        exclusive_still_blocked = _expect_busy(
            lambda: acquire_entity_access(first_identity, exclusive=True)
        )
        shared_second.close()
        exclusive = acquire_entity_access(first_identity, exclusive=True)
        exclusive.close()

        alias.unlink()
        log = EventLog(database)
        connected_alias = root / "connected-alias.db"
        live_database_rename = root / "live-database-renamed.db"
        live_ancestor_rename = root / "live-data-renamed"
        try:
            os.link(database, connected_alias)
            eventlog_alias_blocked = _expect_busy(
                lambda: acquire_entity_access(
                    observe_entity(connected_alias, "file"), exclusive=True
                )
            )
            eventlog_database_rename_blocked = _expect_permission_error(
                lambda: database.rename(live_database_rename)
            )
            eventlog_ancestor_rename_blocked = _expect_permission_error(
                lambda: database.parent.rename(live_ancestor_rename)
            )
            assert eventlog_database_rename_blocked is True
            assert eventlog_ancestor_rename_blocked is True
            assert database.is_file()
            assert database.parent.is_dir()
            assert not live_database_rename.exists()
            assert not live_ancestor_rename.exists()
        finally:
            log.close()

        if connected_alias.exists():
            connected_alias.unlink()
        directory_handle.close()

        result: dict[str, object] = {
            "status": "PASS",
            "os": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "identity_api": "GetFileInformationByHandleEx(FileIdInfo)",
            "volume_serial": int(first_volume_serial),
            "file_id_hex": first_file_id,
            "file_id_bytes": len(bytes.fromhex(first_file_id)),
            "hardlink_identity_equal": hardlink_identity_equal,
            "replacement_identity_changed": replacement_identity_changed,
            "directory_handle_opened": True,
            "rename_with_open_handle": True,
            "shared_locks_coexist": True,
            "exclusive_blocked_by_shared": exclusive_blocked,
            "exclusive_blocked_until_all_shared_close": exclusive_still_blocked,
            "eventlog_hardlink_exclusive_blocked": eventlog_alias_blocked,
            "eventlog_database_rename_blocked": eventlog_database_rename_blocked,
            "eventlog_ancestor_rename_blocked": eventlog_ancestor_rename_blocked,
            "registry_present": registry_observation.present,
            "registry_registry_id": registry_observation.meta.registry_id,
            "registry_publish_api": "MoveFileExW(MOVEFILE_WRITE_THROUGH)",
            "directory_power_loss_durability": "NOT_PROVEN",
            "sqlite_internal_vfs_handle": "NOT_EXPOSED_BY_STDLIB",
        }
        assert result["status"] == "PASS"
        assert len(str(result["file_id_hex"])) == 32
        assert result["file_id_bytes"] == 16
        assert result["hardlink_identity_equal"] is True
        assert result["replacement_identity_changed"] is True
        assert result["rename_with_open_handle"] is True
        assert result["exclusive_blocked_by_shared"] is True
        assert result["exclusive_blocked_until_all_shared_close"] is True
        assert result["eventlog_hardlink_exclusive_blocked"] is True
        assert result["eventlog_database_rename_blocked"] is True
        assert result["eventlog_ancestor_rename_blocked"] is True
        assert result["registry_present"] is True
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = run_probe()
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
