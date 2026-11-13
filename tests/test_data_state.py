"""进度是严格版本化证据；不强制转换数字、不修正步骤。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest


def raw_state(tmp_path):
    return {
        "format": "tianshu-migration",
        "format_version": 2,
        "db_path": str((tmp_path / "events.db").resolve()),
        "from_version": 0,
        "target_version": 2,
        "status": "completed",
        "applied_steps": [1, 2],
        "current_step": None,
        "plan_sha256": "a" * 64,
        "backup": {"path": str((tmp_path / "backup").resolve()), "manifest_sha256": "b" * 64},
        "baseline": {
            "sqlite_algorithm": "tianshu-sqlite-logical-v1",
            "sqlite_sha256": "c" * 64,
            "material_algorithm": "tianshu-material-inventory-v1",
            "material_sha256": "d" * 64,
        },
        "started_at_ms": 1,
        "updated_at_ms": 2,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("format_version", "2"),
        ("format_version", 3),
        ("from_version", -1),
        ("from_version", True),
        ("from_version", "0"),
        ("target_version", 1),
        ("target_version", 2.0),
        ("current_step", True),
        ("current_step", "2"),
        ("current_step", -1),
        ("status", "unknown"),
        ("applied_steps", [True, 2]),
        ("applied_steps", ["1", 2]),
        ("applied_steps", [1, 1, 2]),
        ("applied_steps", [2, 1]),
        ("applied_steps", [2]),
        ("applied_steps", [1]),
        ("applied_steps", [1, 2, 3]),
        ("plan_sha256", "g" * 64),
        ("plan_sha256", "a" * 63),
        ("started_at_ms", True),
        ("updated_at_ms", -1),
        ("updated_at_ms", 0),
        ("db_path", "relative/events.db"),
        ("format", "other"),
    ],
)
def test_strict_state_rejects_bad_fields(tmp_path, field, value):
    from tianshu.data_state import MigrationStateCorrupt, parse_state

    raw = raw_state(tmp_path)
    raw[field] = value
    with pytest.raises(MigrationStateCorrupt):
        parse_state(raw)


@pytest.mark.parametrize("field", ["backup", "baseline", "db_path", "plan_sha256"])
def test_state_requires_all_bindings(tmp_path, field):
    from tianshu.data_state import MigrationStateCorrupt, parse_state

    raw = raw_state(tmp_path)
    del raw[field]
    with pytest.raises(MigrationStateCorrupt):
        parse_state(raw)


@pytest.mark.parametrize("field", ["backup", "baseline", "root"])
def test_unknown_fields_rejected(tmp_path, field):
    from tianshu.data_state import MigrationStateCorrupt, parse_state

    raw = raw_state(tmp_path)
    if field == "root":
        raw["extra"] = 1
    else:
        raw[field]["extra"] = 1
    with pytest.raises(MigrationStateCorrupt):
        parse_state(raw)


def test_roundtrip_and_direct_constructor_validate(tmp_path):
    from tianshu.data_state import MigrationStateCorrupt, encode_state, parse_state

    state = parse_state(raw_state(tmp_path))
    assert parse_state(json.loads(encode_state(state))) == state
    with pytest.raises(MigrationStateCorrupt):
        replace(state, current_step=True)
    with pytest.raises(MigrationStateCorrupt):
        replace(state.backup, manifest_sha256="bad")


@pytest.mark.parametrize(
    ("done", "current", "actual", "expected"),
    [
        ([], None, 0, "idle"),
        ([], 1, 0, "before_step"),
        ([], 1, 1, "after_step"),
        ([1], None, 1, "idle"),
        ([1], 2, 1, "before_step"),
        ([1], 2, 2, "after_step"),
        ([1, 2], None, 2, "idle"),
    ],
)
def test_reachable_positions_only(tmp_path, done, current, actual, expected):
    from tianshu.data_state import classify_progress, parse_state

    raw = raw_state(tmp_path)
    raw.update(status="in_progress", applied_steps=done, current_step=current)
    assert classify_progress(parse_state(raw), actual) == expected


@pytest.mark.parametrize(
    ("done", "current", "actual"),
    [
        ([], None, 1),
        ([], 1, 2),
        ([1], None, 2),
        ([1], 2, 0),
        ([1, 2], None, 1),
    ],
)
def test_db_cannot_unconditionally_fill_progress(tmp_path, done, current, actual):
    from tianshu.data_state import MigrationStateCorrupt, classify_progress, parse_state

    raw = raw_state(tmp_path)
    raw.update(status="in_progress", applied_steps=done, current_step=current)
    with pytest.raises(MigrationStateCorrupt):
        classify_progress(parse_state(raw), actual)


@pytest.mark.parametrize("version", [0, 1, 2])
def test_completed_sequence_depends_on_origin(tmp_path, version):
    from tianshu.data_state import classify_progress, parse_state

    raw = raw_state(tmp_path)
    raw.update(from_version=version, applied_steps=list(range(version + 1, 3)))
    assert classify_progress(parse_state(raw), 2) == "completed"


@pytest.mark.parametrize("field", ["format_version", "manifest_sha256"])
def test_duplicate_json_keys_refused(tmp_path, field):
    from tianshu.data_state import MigrationStateCorrupt, parse_state, read_state_file

    raw = raw_state(tmp_path)
    payload = json.dumps(raw)
    p = tmp_path / "state.json"
    p.write_bytes(payload.encode("utf-8"))
    assert read_state_file(p) == parse_state(raw)

    value = raw[field] if field == "format_version" else raw["backup"][field]
    pair = f"{json.dumps(field)}: {json.dumps(value)}"
    assert payload.count(pair) == 1
    p.write_bytes(payload.replace(pair, f"{pair}, {pair}", 1).encode("utf-8"))
    with pytest.raises(MigrationStateCorrupt, match="重复字段"):
        read_state_file(p)


def test_legacy_complete_strict_and_not_encoded_as_new(tmp_path):
    from tianshu.data_state import LegacyStateV1, MigrationStateCorrupt, encode_state, parse_state

    p = Path(__file__).parent / "fixtures" / "data_admission" / "legacy-completed.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    state = parse_state(raw)
    assert isinstance(state, LegacyStateV1)
    with pytest.raises(MigrationStateCorrupt):
        encode_state(state)
    raw["backup"]["event_count"] = True
    with pytest.raises(MigrationStateCorrupt):
        parse_state(raw)
