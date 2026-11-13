"""摘要来源损坏不能凭正文仍在就通过回放；只篡改临时投影，事件保持原件。"""

import pytest
from test_summary_replay import _ok_case

from tianshu.replay import replay


@pytest.mark.parametrize("damage", [
    "ref", "digest", "kind", "ordinal", "empty", "duplicate",
    "material_scope", "material_run", "material_task", "material_bytes", "material_digest",
    "record_scope", "record_run", "record_task",
    "ordinal_fraction", "material_bytes_fraction", "record_version_fraction",
])
def test_summary_source_ledger_damage_is_a_replay_problem(tmp_path, damage):
    log, _stack, _task, run, _store, record, _view = _ok_case(tmp_path)
    try:
        assert replay(log, run.run_id).ok
        events_before = [
            tuple(row) for row in log.conn.execute("SELECT * FROM events ORDER BY seq")
        ]
        with log.transaction():
            if damage == "empty":
                log.conn.execute(
                    "UPDATE summary_sources SET material_id=? WHERE material_id=?",
                    ("f" * 64, record.material_id),
                )
            elif damage == "duplicate":
                first = log.conn.execute(
                    "SELECT ref,digest FROM summary_sources WHERE material_id=? AND ordinal=0",
                    (record.material_id,),
                ).fetchone()
                log.conn.execute(
                    "UPDATE summary_sources SET ref=?,digest=? WHERE material_id=? AND ordinal=1",
                    (*first, record.material_id),
                )
            elif damage == "ordinal_fraction":
                log.conn.execute(
                    "UPDATE summary_sources SET ordinal=0.25 WHERE material_id=? AND ordinal=0",
                    (record.material_id,),
                )
            elif damage == "material_bytes_fraction":
                log.conn.execute(
                    "UPDATE summary_materials SET bytes=bytes+0.25 WHERE material_id=?",
                    (record.material_id,),
                )
            elif damage == "record_version_fraction":
                log.conn.execute(
                    "UPDATE summary_versions SET version=version+0.25 WHERE summary_id=?",
                    (record.summary_id,),
                )
            elif damage in {"ref", "digest", "kind", "ordinal"}:
                values = {"ref": "seq:999999", "digest": "0" * 64, "kind": "memory", "ordinal": 8}
                log.conn.execute(
                    f"UPDATE summary_sources SET {damage}=? WHERE material_id=? AND ordinal=0",
                    (values[damage], record.material_id),
                )
            elif damage.startswith("material_"):
                column = {"run": "run_id", "task": "task_id", "digest": "text_digest"}.get(
                    damage[9:], damage[9:],
                )
                value = 1 if column == "bytes" else "foreign"
                log.conn.execute(
                    f"UPDATE summary_materials SET {column}=? WHERE material_id=?",
                    (value, record.material_id),
                )
            else:
                column = {"run": "run_id", "task": "task_id", "scope": "scope"}[damage[7:]]
                log.conn.execute(
                    f"UPDATE summary_versions SET {column}=? WHERE summary_id=?",
                    ("foreign", record.summary_id),
                )
        report = replay(log, run.run_id)
        assert not report.ok, (damage, report)
        assert report.messages_verified == 0
        assert any("摘要" in problem and "来源" in problem for problem in report.problems)
        assert [tuple(row) for row in log.conn.execute("SELECT * FROM events ORDER BY seq")] == (
            events_before
        )
    finally:
        log.close()
