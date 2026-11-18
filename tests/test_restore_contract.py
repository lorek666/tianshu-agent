"""恢复 B 的纯值契约：真实 schema、固定字节与严格负例，不启动管理器。"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import importlib
import json
import math
import os
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/data/restore-v1.json"
UUID = "12345678-1234-4234-9234-123456789abc"
BATCH = "87654321-4321-4321-8321-cba987654321"
SHA = "a" * 64


def api():
    module = importlib.import_module("tianshu.restore_contract")
    assert Path(module.__file__).resolve() == ROOT / "src/tianshu/restore_contract.py"
    return module


@pytest.mark.parametrize("value", [0.5, float("inf"), float("nan"), {"a": [1.0]}])
def test_control_codec_rejects_all_json_floats(value):
    with pytest.raises(ValueError):
        api().restore_json_v1(value)


def test_control_codec_exact_bytes_and_unrestricted_sql_integer():
    assert api().restore_json_v1({"z": 9007199254740993, "a": "中"}) == (
        b'{"a":"\\u4e2d","z":9007199254740993}'
    )
    assert api().restore_json_v1({"b": True, "a": None}) == b'{"a":null,"b":true}'


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"version":1 }',
        b'{"version":1}\n',
        b'{"n":-0}',
        b'{"n":1.0}',
        b'{"n":1e0}',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b"\xef\xbb\xbf{}",
        b'"\xff"',
        '"中"'.encode(),
    ],
)
def test_persistent_control_decoder_rejects_noncanonical_or_ambiguous_bytes(raw):
    with pytest.raises(ValueError):
        api().decode_restore_json_v1(raw)


@pytest.mark.parametrize("value", [{1: "bad"}, {"x": b"bytes"}, {"x": {1}}, object()])
def test_control_encoder_rejects_non_json_values_without_coercion(value):
    with pytest.raises(ValueError):
        api().restore_json_v1(value)


# 独立 literal，来自最终 T0 附录 I；期望值不调用被测 encoder/digest 生成。
VECTORS = [
    (
        "sql-row",
        [["null"]],
        "5b5b226e756c6c225d5d",
        "d1730024f1057e25363c4e7cd94e4ed0c9bc544cd1cba6d3c4a259f0dc780fb2",
    ),
    (
        "sql-row",
        [["text", "bnVsbA=="]],
        "5b5b2274657874222c22626e567362413d3d225d5d",
        "0d31097f90b246b4cfad556b7ad1e391e73f061121e5266829c30efb5f449bc4",
    ),
    (
        "sql-row",
        [["integer", "9007199254740993"]],
        "5b5b22696e7465676572222c2239303037313939323534373430393933225d5d",
        "6915ca8a2ba4a6155bb177662db5fa1d19b2ad48fc65a6ad7f5349f80feaa0b2",
    ),
    (
        "sql-row",
        [["integer", "1"]],
        "5b5b22696e7465676572222c2231225d5d",
        "65c0e096ff3f91f31dca35357ef0e6f4db1f943ad7e190f724af5c55f0555013",
    ),
    (
        "sql-row",
        [["real", "3ff0000000000000"]],
        "5b5b227265616c222c2233666630303030303030303030303030225d5d",
        "235be75e3dbf018acffabd9aabad6ed9baa93e4c8713f7f40a8fd8aaa7aaa1ed",
    ),
    (
        "sql-row",
        [["blob", "MQ=="]],
        "5b5b22626c6f62222c224d513d3d225d5d",
        "92550aef60aff2ee50f17bf0444f5cb54c93cd0e92c567a999f999af7b9f7d99",
    ),
    (
        "sql-row",
        [["text", "MQ=="]],
        "5b5b2274657874222c224d513d3d225d5d",
        "79577370f2198391c47e71bc5155b1521f2db771af3737d101b284e4649e2356",
    ),
    (
        "sql-row",
        [["integer", "-9223372036854775808"]],
        "5b5b22696e7465676572222c222d39323233333732303336383534373735383038225d5d",
        "6e55ca2c719e0cafb8a0d84a56c2b7965b999a9f4e2bade40ff60c1b58aa6ff7",
    ),
    (
        "sql-row",
        [["real", "8000000000000000"]],
        "5b5b227265616c222c2238303030303030303030303030303030225d5d",
        "3706c5c2739dc861d34d6472d92d3e20fbfda0b256e7c9ef5f67549b3219144b",
    ),
    (
        "plan",
        {"version": 1},
        "7b2276657273696f6e223a317d",
        "5e5ecf861766e48aaf0b96606983a9191e086db3b945ff5c1465b6979f0f59b6",
    ),
    (
        "binding",
        {"version": 1},
        "7b2276657273696f6e223a317d",
        "4bb159f6af1a0dcb0493179acb37af3d422758cacd91cd6e3c9966ea75a2acaf",
    ),
]


@pytest.mark.parametrize(("label", "payload", "canonical_hex", "digest"), VECTORS)
def test_sql_typed_golden_vectors_preserve_storage_classes(label, payload, canonical_hex, digest):
    m = api()
    assert m.restore_json_v1(payload) == bytes.fromhex(canonical_hex)
    assert m.domain_digest(label, payload) == digest
    if label == "sql-row":
        assert [m.SqlValueV1.from_wire(x).to_wire() for x in payload] == payload


def test_domains_are_closed_and_separated():
    m = api()
    expected_v1 = {
        "meta",
        "event",
        "plan",
        "inventory",
        "sql-row",
        "sql-relation",
        "sql-schema",
        "scope",
        "binding",
        "cleanup",
        "token",
        "origin",
        "lock",
    }
    expected_v2 = {*expected_v1, "review", "reconcile"}
    schema_v2 = json.loads((ROOT / "schemas/data/restore-v2.json").read_bytes().decode("utf-8"))
    assert m.DIGEST_LABELS_V1 == expected_v1
    assert m.DIGEST_LABELS == expected_v2 == set(schema_v2["$defs"]["DigestDomainV2"]["enum"])
    assert schema_v2 == m.contract_schema_v2()
    digests_v2 = {m.domain_digest(label, {"version": 1}) for label in expected_v2}
    assert len(digests_v2) == len(expected_v2)
    for invalid in ("unknown", "PLAN", "plan\x00event", True, b"plan"):
        with pytest.raises(ValueError):
            m.domain_digest(invalid, {})


@pytest.mark.parametrize(
    "raw",
    [
        [],
        ["null", None],
        ["integer"],
        ["integer", True],
        ["integer", 1],
        ["integer", "01"],
        ["integer", "-0"],
        ["integer", "+1"],
        ["integer", "9223372036854775808"],
        ["integer", "-9223372036854775809"],
        ["real", "3ff000000000000"],
        ["real", "3FF0000000000000"],
        ["real", 1.0],
        ["text", "MQ"],
        ["text", "MR=="],
        ["blob", "MQ==\n"],
        ["blob", "_w=="],
        ["unknown", ""],
        ["integer", "MQ=="],
    ],
)
def test_sql_typed_codec_rejects_invalid_tags_numbers_and_base64(raw):
    with pytest.raises(ValueError):
        api().SqlValueV1.from_wire(raw)


def test_sql_values_come_from_real_typeof_and_exact_cast_bytes():
    m = api()
    conn = sqlite3.connect(":memory:")
    conn.text_factory = bytes
    try:
        cases = [
            ("NULL", (), ["null"]),
            ("?", (9007199254740993,), ["integer", "9007199254740993"]),
            ("1.0", (), ["real", "3ff0000000000000"]),
            ("-0.0", (), ["real", "8000000000000000"]),
            ("?", (math.inf,), ["real", "7ff0000000000000"]),
            ("'1'", (), ["text", "MQ=="]),
            ("x'31'", (), ["blob", "MQ=="]),
            ("CAST(x'ff' AS TEXT)", (), ["text", "/w=="]),
        ]
        for expression, params, expected in cases:
            row = conn.execute(
                f"SELECT typeof(v), v, CAST(v AS BLOB) FROM (SELECT {expression} AS v)",
                params,
            ).fetchone()
            value = m.sql_value_from_sqlite(
                row[0].decode("ascii"),
                row[1],
                text_bytes=row[2] if row[0] == b"text" else None,
            )
            assert value.to_wire() == expected
        nan = conn.execute("SELECT typeof(?), ?", (math.nan, math.nan)).fetchone()
        assert m.sql_value_from_sqlite(nan[0].decode(), nan[1]).to_wire() == ["null"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("storage", "value", "raw"),
    [
        ("integer", 1.0, None),
        ("integer", True, None),
        ("real", 1, None),
        ("text", 1, b"1"),
        ("text", "1", None),
        ("blob", "1", None),
        ("null", 0, None),
        ("unknown", None, None),
    ],
)
def test_sql_extraction_never_guesses_or_coerces_storage_class(storage, value, raw):
    with pytest.raises(ValueError):
        api().sql_value_from_sqlite(storage, value, text_bytes=raw)


def test_closed_sets_match_real_schema_and_normative_source():
    m = api()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    prd = (ROOT / "docs/PRD-v1.md").read_text(encoding="utf-8")
    assert "[导入、持久隔离与只读复核契约](restore-held-contract.md)" in prd
    source = (ROOT / "docs/restore-held-contract.md").read_text(encoding="utf-8")
    phases = {
        "reserved",
        "stage_owned",
        "sealed_hold",
        "publish_intent",
        "published_hold",
        "abort_intent",
        "aborted",
    }
    errors = {
        "held",
        "record_corrupt",
        "binding_mismatch",
        "incompatible",
        "capacity_unavailable",
        "durability_unavailable",
        "maintenance_busy",
        "restore_failed",
    }
    assert m.PHASES == set(schema["$defs"]["Phase"]["enum"]) == phases
    assert m.ERROR_CATEGORIES == set(schema["$defs"]["ErrorCategory"]["enum"]) == errors
    assert "`reserved → stage_owned → sealed_hold → publish_intent → published_hold`" in source
    assert "`abort_intent → aborted`" in source
    error_section = source.split("## 6.", 1)[1].split("## 7.", 1)[0]
    error_line = next(line for line in error_section.splitlines() if line.startswith("错误类别"))
    assert set(error_line.split("：", 1)[1].split("；", 1)[0].split("、")) == errors
    assert "released" not in schema["$defs"]["Phase"]["enum"]


def native(m, path):
    return m.NativePathCodecV1().from_path(path.resolve())


def samples(tmp_path):
    """类型值夹具；实体从本次tmp真实stat取得，不声称授予T1操作许可。"""
    from tianshu.backup import verify_backup

    m = api()
    parent = tmp_path / "parent"
    parent.mkdir(exist_ok=True)
    backup = tmp_path / "backup"
    backup.mkdir(exist_ok=True)
    stage = parent / f".target.restore-{BATCH}"
    stage.mkdir(exist_ok=True)
    database = stage / "events.db"
    database.write_bytes(b"db")

    def identity(path):
        st = path.stat()
        if os.name == "posix":
            return m.PosixEntityIdentityV1("posix", st.st_dev, st.st_ino)
        # T0在Windows不伪造file identity；纯值样本使用显式合法值，不作原生身份实测。
        return m.WindowsEntityIdentityV1("windows", 1, "1" * 32)

    def directory(path):
        return m.DirRefV1(native(m, path), identity(path))

    p = directory(parent)
    s = directory(stage)
    target = native(m, parent / "target")
    target_db = native(m, parent / "target/events.db")
    file_hash = hashlib.sha256(b"db").hexdigest()
    inventory = m.CopyInventoryV1(1, (), (m.CopyFileV1(("events.db",), 2, file_hash),))
    manifest = m.ManifestRefV1(base64.b64encode(b"{}").decode(), hashlib.sha256(b"{}").hexdigest())
    placement = m.StablePlacementV1(target, "target", (p,), ())
    absent = m.AbsentTargetV1("absent")
    inputs = m.PlanInputsV1(
        1,
        directory(backup),
        manifest,
        inventory,
        m.domain_digest("inventory", inventory),
        target.text,
        (),
        placement,
        absent,
        None,
        None,
        (),
        1,
        1,
    )
    column = m.ColumnV1(0, "id", "INTEGER", True, m.SqlValueV1("null", None), 1)
    relation = m.RelationDigestV1("events", (column,), m.SqlValueV1("null", None), 0, SHA)
    coverage = m.FamilyCoverageV1(
        "events",
        ("events",),
        (m.ColumnRefV1("events", "id"),),
        (("events.db",),),
        ("all",),
    )
    scope = m.ScopeManifestV1(1, 1, SHA, (relation,), (coverage,), inventory)
    binding = m.BindingV1(
        1,
        UUID,
        BATCH,
        target,
        target_db,
        s.identity,
        identity(database),
        target.text,
        (),
        manifest,
        inventory,
        inputs.inventory_sha256,
        m.DatabaseBytesV1(2, file_hash),
        scope,
        m.domain_digest("scope", scope),
        (),
    )
    reserved = m.ReservedV1(
        inputs,
        m.domain_digest("plan", inputs),
        inventory,
        inputs.inventory_sha256,
        placement,
        absent,
        stage.name,
    )
    e1 = m.RestoreEventV1(1, UUID, 1, BATCH, None, "reserved", reserved)
    token = m.TokenV1(1, UUID, BATCH, m.domain_digest("event", e1), s, p)
    token_path = stage / ".restore-token.json"
    token_path.write_bytes(token.to_bytes())
    token_file = m.CleanupFileV1(
        (token_path.name,),
        identity(token_path),
        token_path.stat().st_size,
        hashlib.sha256(token_path.read_bytes()).hexdigest(),
        None,
        (".restore-token.json",),
        base64.b64encode(token.to_bytes()).decode(),
    )
    owned = m.StageOwnedV1(
        token.reserved_sha256,
        s,
        p,
        (),
        m.ControlLayoutV1((".restore-token.json",), (".restore-token.json.tmp",)),
        base64.b64encode(token.to_bytes()).decode(),
        m.domain_digest("token", token),
        m.ControlLayoutV1((".restore-origin.json",), (".restore-origin.json.tmp",)),
    )
    e2 = m.RestoreEventV1(1, UUID, 2, BATCH, m.domain_digest("event", e1), "stage_owned", owned)
    sealed = m.SealedHoldV1(
        m.domain_digest("event", e2),
        binding.root_identity,
        binding.database_identity,
        binding,
        m.domain_digest("binding", binding),
    )
    e3 = m.RestoreEventV1(1, UUID, 3, BATCH, m.domain_digest("event", e2), "sealed_hold", sealed)
    origin = m.OriginV1(1, UUID, BATCH, m.domain_digest("event", e3), sealed.binding_sha256)
    intent = m.PublishIntentV1(
        origin.sealed_sha256,
        sealed.binding_sha256,
        placement,
        absent,
        binding.root_identity,
        binding.database_identity,
        base64.b64encode(origin.to_bytes()).decode(),
        m.domain_digest("origin", origin),
    )
    e4 = m.RestoreEventV1(1, UUID, 4, BATCH, m.domain_digest("event", e3), "publish_intent", intent)
    published = m.PublishedHoldV1(
        m.domain_digest("event", e4),
        sealed.binding_sha256,
        m.DirRefV1(target, s.identity),
        m.EntityRefV1(target_db, binding.database_identity),
        True,
        intent.origin_sha256,
    )
    e5 = m.RestoreEventV1(
        1,
        UUID,
        5,
        BATCH,
        m.domain_digest("event", e4),
        "published_hold",
        published,
    )
    legacy = m.LegacyReconciliation(
        1,
        (m.LegacyIntentV1("run", "call", 1, "tool"),),
        (m.LegacyApprovalV1("approval", "run", "delete", "pending", "p", 2),),
        (m.LegacyRunV1("run", "task", "created"),),
        (m.LegacyLeaseV1("resource", "run", 1, "", 2),),
        m.ProjectionTablesV1("present", "present", "present"),
        True,
        "legacy_summary_only",
    )
    verification = verify_backup(backup)
    plan = m.RestorePlanV1(1, inputs, reserved.plan_sha256, False, (), verification, legacy, ())
    snapshot = m.FileSnapshotV1(native(m, database), binding.database_identity, 2, file_hash)
    capacity = m.CapacityCheckV1(target, s.identity, 100, 2, False)
    source = m.SourceRefV1("events", (m.SqlValueV1("integer", "1"),), "body", None)
    status = m.NamedValueV1("state", m.SqlValueV1("text", "MQ=="))
    later = m.LaterChangeV1("events", source.key, SHA, None, ("saved backup",))
    dependency = m.DependencyRefV1("algorithm", "1", SHA)
    empty = m.EmptyTargetV1("empty", m.DirRefV1(target, s.identity))
    windows_identity = m.WindowsEntityIdentityV1("windows", 1, "f" * 32)
    node = m.NodeInfoV1("file", database.stat().st_nlink, 2, binding.database_identity)
    child = m.ChildEntry("events.db", node)
    lock = m.LockKeyV1("entity", s.identity)
    home = directory(tmp_path)
    authority_path = tmp_path / ".tianshu/restore-registry-v1"
    meta = m.RegistryMetaV1(
        1,
        UUID,
        home,
        m.DirRefV1(native(m, authority_path), home.identity),
        m.EntityRefV1(native(m, authority_path / "registry.db"), binding.database_identity),
    )
    selector = m.BodySelectorV1("events", (m.SqlValueV1("integer", "1"),), "body")
    body = m.BodyViewV1(
        "body", 2, hashlib.sha256(b"db").hexdigest(), selector.to_selector(), None, None
    )
    review = m.RestoreReviewV1(
        BATCH,
        "published_hold",
        True,
        target,
        sealed.binding_sha256,
        binding.scope_sha256,
        True,
        (m.CheckV1("all", True, ("observed",)),),
        (m.ReviewObjectV1("events", selector.key, (), (), (body,)),),
        (),
        (),
        "completeness_unproven",
        (),
    )
    action = m.RestoreActionResultV1(
        BATCH,
        "published_hold",
        True,
        target,
        reserved.plan_sha256,
        sealed.binding_sha256,
        m.domain_digest("event", e5),
        1,
        2,
        ("restore-review",),
    )
    failure = m.RestoreFailureV1(
        "restore_failed",
        "failure",
        None,
        None,
        None,
        (target,),
        (),
        m.RestoreCauseV1("OSError", "reason"),
    )
    cleanup_absent = m.CleanupPlanV1(1, "absent_before_stage_owned", None, p, (p,), (), (), ())
    cleanup_present = m.CleanupPlanV1(
        1,
        "present",
        s,
        p,
        (p,),
        (),
        (
            token_file,
            m.CleanupFileV1(
                ("events.db",), binding.database_identity, 2, file_hash, ("events.db",), None, None
            ),
        ),
        (m.CleanupDirectoryV1((), s.identity, (".restore-token.json", "events.db")),),
    )
    return locals()


def test_all_wire_types_are_strict_frozen_dataclasses_and_schema_bound(tmp_path):
    m = api()
    values = samples(tmp_path)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema == m.contract_schema()
    assert "$ref" in schema and schema["$schema"].endswith("2020-12/schema")
    for name, cls in m.CONTRACT_TYPES.items():
        if name in {"EntityIdentityV1", "OriginalTargetV1", "RestorePayloadV1"}:
            continue
        assert dataclasses.is_dataclass(cls), name
        fields = {f.name for f in dataclasses.fields(cls)}
        definition = schema["$defs"][name]
        if name not in {"SqlValueV1", "LockKeyV1"}:
            assert set(definition["properties"]) == set(definition["required"]) == fields
            assert definition["additionalProperties"] is False
    for value in values.values():
        if isinstance(value, m.ContractV1):
            assert type(value).from_bytes(value.to_bytes()) == value
            assert type(value).from_dict(value.to_dict()) == value
            with pytest.raises(dataclasses.FrozenInstanceError):
                value.version = 2


def test_native_path_uses_current_os_canonical_parts_and_real_pathkeys(tmp_path):
    from tianshu.pathkeys import overlap_key

    m = api()
    path = tmp_path.resolve()
    value = native(m, path)
    assert value.text == str(path)
    assert value.comparison_key == overlap_key(str(path))
    assert value.segments == path.parts
    assert value.os == ("windows" if os.name == "nt" else "posix")
    assert m.NativePathV1.from_dict(value.to_dict()) == value
    for field, invalid in [
        ("os", "other"),
        ("text", "relative"),
        ("text", str(path / "..")),
        ("segments", ["wrong"]),
        ("comparison_key", "wrong"),
    ]:
        raw = value.to_dict()
        raw[field] = invalid
        with pytest.raises(ValueError):
            m.NativePathV1.from_dict(raw)
    raw = value.to_dict()
    raw["os"] = "posix" if os.name == "nt" else "windows"
    with pytest.raises(ValueError):
        m.NativePathV1.from_dict(raw)


def test_native_path_codec_injection_cannot_bypass_current_os_validation(tmp_path):
    m = api()

    class CountingCodec(m.NativePathCodecV1):
        calls = 0

        def canonical_parts(self, text):
            self.calls += 1
            return super().canonical_parts(text)

    codec = CountingCodec()
    value = native(m, tmp_path)
    assert m.NativePathV1.from_dict(value.to_dict(), path_codec=codec) == value
    assert codec.calls > 0
    bad = value.to_dict() | {"comparison_key": "forged"}
    with pytest.raises(ValueError):
        m.NativePathV1.from_dict(bad, path_codec=codec)


@pytest.mark.parametrize(
    "bad",
    [
        (),
        ("",),
        (".",),
        ("..",),
        ("a/b",),
        ("a\\b",),
        ("/abs",),
        ("C:\\abs",),
        (True,),
        ("nul\x00",),
    ],
)
def test_copy_files_reject_empty_or_traversing_relative_paths(bad):
    with pytest.raises(ValueError):
        api().CopyFileV1(bad, 1, SHA)


@pytest.mark.parametrize(
    ("field", "bad"), [("version", True), ("version", 1.0), ("version", 2), ("files", True)]
)
def test_inventory_rejects_wrong_types_and_unknown_versions(field, bad):
    raw = {"version": 1, "directories": [], "files": []}
    raw[field] = bad
    with pytest.raises(ValueError):
        api().CopyInventoryV1.from_dict(raw)


def test_inventory_requires_unique_sorted_complete_parent_closure():
    m = api()
    a = m.CopyFileV1(("a", "b", "file"), 1, SHA)
    directories = (("a",), ("a", "b"))
    assert m.CopyInventoryV1(1, directories, (a,)).files == (a,)
    for dirs, files in [
        ((), (a,)),
        (directories[::-1], (a,)),
        (directories + (("a",),), (a,)),
        (directories, (a, a)),
        ((("a",),), (m.CopyFileV1(("a",), 1, SHA),)),
    ]:
        with pytest.raises(ValueError):
            m.CopyInventoryV1(1, dirs, files)


def test_every_object_rejects_extra_missing_bool_hash_and_uuid_fields(tmp_path):
    m = api()
    values = samples(tmp_path)
    for value in values.values():
        if not isinstance(value, m.ContractV1) or isinstance(value, (m.SqlValueV1, m.LockKeyV1)):
            continue
        raw = value.to_dict()
        with pytest.raises(ValueError):
            type(value).from_dict(raw | {"unexpected": 1})
        for field in raw:
            with pytest.raises(ValueError):
                type(value).from_dict({k: v for k, v in raw.items() if k != field})
    for field, bad in [
        ("registry_id", UUID.upper()),
        ("registry_id", "bad"),
        ("sequence", True),
        ("sequence", 0),
        ("version", True),
        ("previous_sha256", "A" * 64),
        ("kind", "released"),
    ]:
        raw = values["e1"].to_dict()
        raw[field] = bad
        with pytest.raises(ValueError):
            m.RestoreEventV1.from_dict(raw)


def test_plan_inventory_manifest_and_binding_hashes_are_checked(tmp_path):
    m = api()
    values = samples(tmp_path)
    for key, field in [
        ("inputs", "inventory_sha256"),
        ("reserved", "plan_sha256"),
        ("binding", "scope_sha256"),
        ("sealed", "binding_sha256"),
        ("manifest", "sha256"),
        ("owned", "token_sha256"),
    ]:
        raw = values[key].to_dict()
        raw[field] = "b" * 64
        with pytest.raises(ValueError):
            type(values[key]).from_dict(raw)
    raw = values["reserved"].to_dict()
    raw["placement"]["final_name"] = "other"
    with pytest.raises(ValueError):
        m.ReservedV1.from_dict(raw)


def test_cleanup_control_bytes_bind_exact_prefix_and_formal_completion(tmp_path):
    m = api()
    v = samples(tmp_path)
    raw = v["token"].to_bytes()
    prefix = raw[:5]
    valid = m.CleanupFileV1(
        (".restore-token.json.tmp",),
        v["s"].identity,
        len(prefix),
        hashlib.sha256(prefix).hexdigest(),
        None,
        (".restore-token.json",),
        base64.b64encode(raw).decode(),
    )
    for changes in [{"sha256": SHA}, {"size": len(raw) + 1}, {"path": (".restore-token.json",)}]:
        with pytest.raises(ValueError):
            dataclasses.replace(valid, **changes)


def test_native_parent_chain_rejects_repeated_same_path(tmp_path):
    m = api()
    v = samples(tmp_path)
    anchor = Path(tmp_path.anchor)
    directory = v["directory"](anchor)
    with pytest.raises(ValueError):
        m.StablePlacementV1(native(m, anchor / "target"), "target", (directory, directory), ())


def test_full_event_chain_enforces_predecessors_references_and_envelope(tmp_path):
    m = api()
    v = samples(tmp_path)
    chain = tuple(v[f"e{i}"] for i in range(1, 6))
    assert m.validate_event_chain(chain) is None
    for bad in [chain[1:], (chain[0], chain[0]), (chain[0], chain[2]), chain[::-1]]:
        with pytest.raises(ValueError):
            m.validate_event_chain(bad)
    second = dataclasses.replace(chain[1], previous_sha256="b" * 64)
    with pytest.raises(ValueError):
        m.validate_event_chain((chain[0], second))
    with pytest.raises(ValueError):
        dataclasses.replace(chain[0], payload=v["sealed"])
    with pytest.raises(ValueError):
        dataclasses.replace(chain[0], batch_id=UUID)


def test_absent_and_present_cleanup_variants_and_aborted_barriers(tmp_path):
    m = api()
    v = samples(tmp_path)
    for name in ("cleanup_absent", "cleanup_present"):
        assert m.CleanupPlanV1.from_bytes(v[name].to_bytes()) == v[name]
    absent = v["cleanup_absent"]
    for changes in [
        {"stage": v["s"]},
        {"observed_parent_chain": ()},
        {"parent": None},
        {"files": v["cleanup_present"].files},
    ]:
        with pytest.raises(ValueError):
            dataclasses.replace(absent, **changes)
    present = v["cleanup_present"]
    for changes in [
        {"stage": None},
        {"parent": None},
        {"directories_postorder": ()},
        {"missing_parent_suffix": ("later",)},
    ]:
        with pytest.raises(ValueError):
            dataclasses.replace(present, **changes)
    for cleanup, prefix in [(absent, (v["e1"],)), (present, (v["e1"], v["e2"]))]:
        predecessor = prefix[-1]
        abort = m.AbortIntentV1(
            m.domain_digest("event", predecessor),
            predecessor.kind,
            True,
            cleanup,
            m.domain_digest("cleanup", cleanup),
        )
        intent = m.RestoreEventV1(
            1,
            UUID,
            len(prefix) + 1,
            BATCH,
            m.domain_digest("event", predecessor),
            "abort_intent",
            abort,
        )
        absent_variant = cleanup.stage is None
        barriers = (
            ()
            if absent_variant
            else (
                m.DirectoryBarrierV1(v["s"].identity, (".restore-token.json",)),
                m.DirectoryBarrierV1(v["s"].identity, ("events.db",)),
                m.DirectoryBarrierV1(v["p"].identity, ()),
            )
        )
        done = m.AbortedV1(
            m.domain_digest("event", intent),
            abort.cleanup_plan_sha256,
            () if absent_variant else ((".restore-token.json",), ("events.db",), ()),
            barriers,
            cleanup.parent,
            v["p"] if absent_variant else None,
            True,
            v["absent"],
        )
        end = m.RestoreEventV1(
            1,
            UUID,
            len(prefix) + 2,
            BATCH,
            m.domain_digest("event", intent),
            "aborted",
            done,
        )
        m.validate_event_chain((*prefix, intent, end))
        bad_done = (
            dataclasses.replace(done, absence_anchor_barrier=None)
            if absent_variant
            else (dataclasses.replace(done, directory_barriers=barriers[:-1]))
        )
        bad_end = dataclasses.replace(end, payload=bad_done)
        with pytest.raises(ValueError):
            m.validate_event_chain((*prefix, intent, bad_end))


def test_absent_cleanup_keeps_missing_parent_suffix_and_real_ancestor(tmp_path):
    m = api()
    v = samples(tmp_path)
    cleanup = m.CleanupPlanV1(
        1,
        "absent_before_stage_owned",
        None,
        None,
        (v["p"],),
        ("missing",),
        (),
        (),
    )
    assert cleanup.parent is None and cleanup.observed_parent_chain[-1] == v["p"]
    with pytest.raises(ValueError):
        dataclasses.replace(cleanup, parent=v["p"])
    root = native(m, Path(v["p"].path.text) / "missing/target")
    placement = m.StablePlacementV1(root, "target", (v["p"],), ("missing",))
    inputs = dataclasses.replace(v["inputs"], placement=placement, saved_source_path=root.text)
    reserved = dataclasses.replace(
        v["reserved"], plan=inputs, plan_sha256=m.domain_digest("plan", inputs), placement=placement
    )
    event = dataclasses.replace(v["e1"], payload=reserved)
    abort = m.AbortIntentV1(
        m.domain_digest("event", event),
        "reserved",
        True,
        cleanup,
        m.domain_digest("cleanup", cleanup),
    )
    intent = m.RestoreEventV1(
        1, UUID, 2, BATCH, m.domain_digest("event", event), "abort_intent", abort
    )
    done = m.AbortedV1(
        m.domain_digest("event", intent),
        abort.cleanup_plan_sha256,
        (),
        (),
        None,
        v["p"],
        True,
        v["absent"],
    )
    end = m.RestoreEventV1(1, UUID, 3, BATCH, m.domain_digest("event", intent), "aborted", done)
    m.validate_event_chain((event, intent, end))
    assert not (Path(v["p"].path.text) / "missing").exists()
    bad_end = dataclasses.replace(end, payload=dataclasses.replace(done, stable_parent=v["p"]))
    with pytest.raises(ValueError):
        m.validate_event_chain((event, intent, bad_end))


def test_body_selector_exact_codec_and_default_redaction(tmp_path):
    m = api()
    v = samples(tmp_path)
    selector = v["selector"]
    encoded = selector.to_selector()
    assert encoded.startswith("v1:") and "=" not in encoded
    assert m.BodySelectorV1.from_selector(encoded) == selector
    assert not selector.matches("events.other", selector.key, "body")
    assert not selector.matches("events", selector.key, "body.other")
    assert selector.matches("events", selector.key, "body")
    assert v["review"].objects[0].bodies[0].text is None
    assert v["review"].objects[0].bodies[0].bytes_b64 is None
    for invalid in [
        encoded + "=",
        "v2:" + encoded[3:],
        "v1:!",
        "v1:",
        "v1:" + base64.urlsafe_b64encode(b'{"family":"x"}').decode().rstrip("="),
    ]:
        with pytest.raises(ValueError):
            m.BodySelectorV1.from_selector(invalid)


def test_review_and_response_do_not_claim_release_or_partial_success(tmp_path):
    m = api()
    v = samples(tmp_path)
    for operation, result in [("restore-review", v["review"]), ("restore-apply", v["action"])]:
        response = m.RestoreResponseV1(1, operation, True, result, None)
        assert m.RestoreResponseV1.from_bytes(response.to_bytes()) == response
    with pytest.raises(ValueError):
        dataclasses.replace(v["review"], phase="sealed_hold")
    with pytest.raises(ValueError):
        dataclasses.replace(v["review"], held=False)
    with pytest.raises(ValueError):
        dataclasses.replace(v["action"], held=False)
    with pytest.raises(ValueError):
        m.RestoreResponseV1(1, "restore-review", False, v["review"], v["failure"])
    partial = dataclasses.replace(v["review"], review_complete=False, unverifiable=("source",))
    failure = m.RestoreResponseV1(1, "restore-review", False, partial, v["failure"])
    assert failure.result is partial
    with pytest.raises(ValueError):
        m.RestoreResponseV1(1, "restore-apply", False, v["action"], v["failure"])


def test_legacy_reconciliation_uses_exact_existing_item_types_and_scope(tmp_path):
    m = api()
    legacy = samples(tmp_path)["legacy"]
    raw = legacy.to_dict()
    assert raw["version"] == 1
    assert raw["pending_approvals"][0]["resources"] == "p"
    assert raw["live_leases"][0]["owner_instance_id"] == ""
    assert raw["reconciliation_scope"] == "legacy_summary_only"
    assert set(raw["projection_tables"]) == {"approvals", "runs", "leases"}
    for mutate in [
        lambda r: r["projection_tables"].update(other="present"),
        lambda r: r["pending_approvals"][0].update(resources=[]),
        lambda r: r["live_leases"][0].update(owner_pid=True),
        lambda r: r.update(complete=1),
        lambda r: r.update(reconciliation_scope="complete"),
    ]:
        bad = copy.deepcopy(raw)
        mutate(bad)
        with pytest.raises(ValueError):
            m.LegacyReconciliation.from_dict(bad)
    assert set(legacy.to_legacy_dict()) == {
        "pending_intents",
        "pending_approvals",
        "non_terminal_runs",
        "live_leases",
        "projection_tables",
        "complete",
    }


def test_sql_relation_and_schema_digest_sort_without_dropping_duplicates(tmp_path):
    m = api()
    column = samples(tmp_path)["column"]
    integer = m.SqlValueV1("integer", "1")
    text = m.SqlValueV1("text", "MQ==")
    relation = m.make_sql_relation(
        "r", (column,), m.SqlValueV1("null", None), ((text,), (integer,), (integer,))
    )
    assert len(relation.rows) == 3
    assert relation.rows[0] == relation.rows[1] == (integer,)
    assert m.domain_digest("sql-relation", relation) != m.domain_digest(
        "sql-relation",
        dataclasses.replace(relation, rows=relation.rows[1:]),
    )
    null = m.SqlValueV1("null", None)
    schema_row = (text, text, text, integer, null)
    schema = m.make_sql_schema(
        integer, (schema_row, schema_row), (m.SqlRelationRefV1("r", (column,)),)
    )
    assert schema.sqlite_schema_rows == (schema_row, schema_row)
    with pytest.raises(ValueError):
        dataclasses.replace(schema, sqlite_schema_rows=((text,),))
    with pytest.raises(ValueError):
        dataclasses.replace(schema, sqlite_schema_rows=((text, text, text, text, null),))


@pytest.mark.parametrize("events", [(object(),), ({"registry_id": UUID},)])
def test_chain_rejects_non_contract_values_with_domain_error(events):
    with pytest.raises(api().RestoreContractError):
        api().validate_event_chain(events)


def test_sql_schema_digest_uses_actual_sqlite_schema_five_columns_and_relations():
    m = api()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE r (v)")
        conn.execute("PRAGMA user_version=1")
        names = ("type", "name", "tbl_name", "rootpage", "sql")
        query = ",".join(f"typeof({name}),{name},CAST({name} AS BLOB)" for name in names)
        raw_rows = conn.execute(f"SELECT {query} FROM sqlite_schema").fetchall()

        def typed(raw):
            return m.sql_value_from_sqlite(
                raw[0], raw[1], text_bytes=raw[2] if raw[0] == "text" else None
            )

        rows = tuple(tuple(typed(raw[i : i + 3]) for i in range(0, 15, 3)) for raw in raw_rows)
        version = conn.execute(
            "SELECT typeof(user_version),user_version,CAST(user_version AS BLOB) "
            "FROM pragma_user_version",
        ).fetchone()
        default = conn.execute(
            "SELECT typeof(dflt_value),dflt_value,CAST(dflt_value AS BLOB) "
            "FROM pragma_table_info('r')",
        ).fetchone()
        column = conn.execute("PRAGMA table_info(r)").fetchone()
        assert column[3] in (0, 1)
        columns = (
            m.ColumnV1(column[0], column[1], column[2], bool(column[3]), typed(default), column[5]),
        )
        value = m.make_sql_schema(typed(version), rows, (m.SqlRelationRefV1("r", columns),))
    finally:
        conn.close()
    assert value.to_dict() == {
        "user_version": ["integer", "1"],
        "sqlite_schema_rows": [
            [
                ["text", "dGFibGU="],
                ["text", "cg=="],
                ["text", "cg=="],
                ["integer", "2"],
                ["text", "Q1JFQVRFIFRBQkxFIHIgKHYp"],
            ]
        ],
        "relations": [
            {
                "name": "r",
                "columns": [
                    {
                        "ordinal": 0,
                        "name": "v",
                        "declared_type": "",
                        "not_null": False,
                        "default_value": ["null"],
                        "primary_key_ordinal": 0,
                    }
                ],
            }
        ],
    }
    # 独立标准库离线参考，见证据包sql-schema-reference-vector.json；不是被测encoder生成。
    assert m.domain_digest("sql-schema", value) == (
        "de8f4c2eeb1174df56535d8acb89612332be6f19b64dca006bc3c8d29597ce25"
    )


def test_no_entry_or_string_policy_quota_is_added():
    m = api()
    files = tuple(
        m.CopyFileV1((f"file-{i:04}",), 0, hashlib.sha256(b"").hexdigest()) for i in range(257)
    )
    inventory = m.CopyInventoryV1(1, (), files)
    assert len(m.CopyInventoryV1.from_bytes(inventory.to_bytes()).files) == 257
    text = "中" * 65537
    assert m.decode_restore_json_v1(m.restore_json_v1(text)) == text


def test_sql_signed64_range_rejects_long_canonical_decimal_without_python_digit_coercion():
    with pytest.raises(api().RestoreContractError):
        api().SqlValueV1.from_wire(["integer", "9" * 5000])


def test_schema_references_are_local_real_definitions_and_semantic_branches_are_present():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    definitions = schema["$defs"]

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/$defs/")
                assert value["$ref"].removeprefix("#/$defs/") in definitions
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    assert len(definitions["RestoreEventV1"]["allOf"]) == 7
    assert len(definitions["CleanupPlanV1"]["allOf"]) == 2
    assert len(definitions["CleanupFileV1"]["oneOf"]) == 2
    assert definitions["SqlSchemaV1"]["properties"]["sqlite_schema_rows"]["items"]["minItems"] == 5


def test_observation_is_not_a_serializable_head_authority():
    m = api()
    assert "RegistryObservationV1" not in m.CONTRACT_TYPES
    assert (
        "RegistryObservationV1"
        not in json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]
    )


def test_lock_keys_and_native_node_values_are_closed_wire_values_only(tmp_path):
    m = api()
    v = samples(tmp_path)
    keys = [
        m.LockKeyV1("gate", None),
        m.LockKeyV1("namespace", v["target"].comparison_key),
        v["lock"],
    ]
    assert keys[0].to_bytes() == b'["gate"]'
    for key in keys:
        assert m.LockKeyV1.from_bytes(key.to_bytes()) == key
        assert m.domain_digest("lock", key) != m.domain_digest("plan", key)
    for raw in [
        ["gate", "x"],
        ["namespace", True],
        ["namespace", "relative"],
        ["entity", {}],
        ["other"],
        [],
    ]:
        with pytest.raises(ValueError):
            m.LockKeyV1.from_wire(raw)
    for changes in [{"kind": "link"}, {"link_count": True}, {"size": -1}]:
        with pytest.raises(ValueError):
            dataclasses.replace(v["node"], **changes)


def test_interleaved_batches_keep_global_sequence_and_per_batch_predecessor(tmp_path):
    m = api()
    first = samples(tmp_path)
    second_root = tmp_path / "another"
    second_root.mkdir()
    second = samples(second_root)
    other_batch = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    inputs = dataclasses.replace(
        second["inputs"],
        registry_id=UUID,
        registry_head_sha256=m.domain_digest("event", first["e1"]),
    )
    payload = dataclasses.replace(
        second["reserved"],
        plan=inputs,
        plan_sha256=m.domain_digest("plan", inputs),
        stage_name=f".target.restore-{other_batch}",
    )
    reserved = m.RestoreEventV1(
        1, UUID, 2, other_batch, m.domain_digest("event", first["e1"]), "reserved", payload
    )
    owned = dataclasses.replace(
        first["e2"], sequence=3, previous_sha256=m.domain_digest("event", reserved)
    )
    m.validate_event_chain((first["e1"], reserved, owned))
    duplicate = dataclasses.replace(
        reserved, sequence=4, previous_sha256=m.domain_digest("event", owned)
    )
    with pytest.raises(ValueError):
        m.validate_event_chain((first["e1"], reserved, owned, duplicate))


def test_unknown_error_and_incomplete_windows_identity_are_rejected(tmp_path):
    v = samples(tmp_path)
    for category in ("unknown", "released", True):
        with pytest.raises(ValueError):
            dataclasses.replace(v["failure"], category=category)
    for value in ("f" * 31, "F" * 32, "f" * 16, "g" * 32):
        with pytest.raises(ValueError):
            dataclasses.replace(v["windows_identity"], file_id_hex=value)


def cleanup_chain(v, cleanup, prefix=None):
    """从真实现场的冻结计划构造全链，不模拟原生删除/持久化。"""
    m = v["m"]
    prefix = (v["e1"], v["e2"]) if prefix is None else prefix
    predecessor = prefix[-1]
    prior_hash = m.domain_digest("event", predecessor)
    abort = m.AbortIntentV1(
        prior_hash, predecessor.kind, True, cleanup, m.domain_digest("cleanup", cleanup)
    )
    intent = m.RestoreEventV1(
        1, UUID, len(prefix) + 1, BATCH, prior_hash, "abort_intent", abort
    )
    paths = tuple(f.path for f in cleanup.files) + tuple(
        d.path for d in cleanup.directories_postorder
    )
    directories = {d.path: d.identity for d in cleanup.directories_postorder}
    barriers = tuple(
        m.DirectoryBarrierV1(directories[path[:-1]] if path else cleanup.parent.identity, path)
        for path in paths
    )
    done = m.AbortedV1(
        m.domain_digest("event", intent),
        abort.cleanup_plan_sha256,
        paths,
        barriers,
        cleanup.parent,
        None,
        True,
        v["absent"],
    )
    end = m.RestoreEventV1(
        1, UUID, len(prefix) + 2, BATCH, m.domain_digest("event", intent), "aborted", done
    )
    return (*prefix, intent, end)


def present_cleanup(v, files, directories=()):
    m = v["m"]
    paths = {f.path for f in files} | {d.path for d in directories}
    children = tuple(sorted((path[0] for path in paths if len(path) == 1), key=m.restore_json_v1))
    return m.CleanupPlanV1(
        1,
        "present",
        v["s"],
        v["p"],
        (v["p"],),
        (),
        tuple(sorted(files, key=lambda f: m.restore_json_v1(f.path))),
        (*directories, m.CleanupDirectoryV1((), v["s"].identity, children)),
    )


def test_first_abort_rejects_inventory_external_directory(tmp_path):
    v = samples(tmp_path)
    m = v["m"]
    foreign = v["stage"] / "foreign-empty-directory"
    foreign.mkdir()
    directory = m.CleanupDirectoryV1((foreign.name,), v["identity"](foreign), ())
    assert directory.path not in v["inventory"].directories
    cleanup = present_cleanup(v, v["cleanup_present"].files, (directory,))
    chain = cleanup_chain(v, cleanup)
    with pytest.raises(m.RestoreContractError):
        m.validate_event_chain(chain)
    assert foreign.is_dir() and v["database"].read_bytes() == b"db"


def test_first_abort_accepts_only_created_subset_of_inventory_directories(tmp_path):
    v = samples(tmp_path)
    m = v["m"]
    inventory = dataclasses.replace(v["inventory"], directories=(("created",), ("not-created",)))
    inputs = dataclasses.replace(
        v["inputs"],
        copy_inventory=inventory,
        inventory_sha256=m.domain_digest("inventory", inventory),
    )
    reserved = dataclasses.replace(
        v["reserved"],
        plan=inputs,
        plan_sha256=m.domain_digest("plan", inputs),
        copy_inventory=inventory,
        inventory_sha256=inputs.inventory_sha256,
    )
    v["e1"] = dataclasses.replace(v["e1"], payload=reserved)
    token = dataclasses.replace(v["token"], reserved_sha256=m.domain_digest("event", v["e1"]))
    v["token_path"].write_bytes(token.to_bytes())
    owned = dataclasses.replace(
        v["owned"],
        reserved_sha256=token.reserved_sha256,
        token_bytes_b64=base64.b64encode(token.to_bytes()).decode(),
        token_sha256=m.domain_digest("token", token),
    )
    v["e2"] = dataclasses.replace(
        v["e2"], previous_sha256=token.reserved_sha256, payload=owned
    )
    token_file = dataclasses.replace(
        v["token_file"],
        size=len(token.to_bytes()),
        sha256=hashlib.sha256(token.to_bytes()).hexdigest(),
        control_expected_bytes_b64=owned.token_bytes_b64,
    )
    created = v["stage"] / "created"
    created.mkdir()
    directory = m.CleanupDirectoryV1((created.name,), v["identity"](created), ())
    cleanup = present_cleanup(v, (token_file, v["cleanup_present"].files[-1]), (directory,))
    assert not (v["stage"] / "not-created").exists()
    assert m.validate_event_chain(cleanup_chain(v, cleanup)) is None


@pytest.mark.parametrize("formal", [".restore-token.json", ".restore-origin.json"])
def test_first_abort_rejects_formal_and_temporary_control_coexistence(tmp_path, formal):
    v = samples(tmp_path)
    m = v["m"]
    value = v["token"] if formal == ".restore-token.json" else v["origin"]
    raw = value.to_bytes()
    files = tuple(f for f in v["cleanup_present"].files if f.control_formal != (formal,))
    controls = []
    for name in (formal, formal + ".tmp"):
        path = v["stage"] / name
        path.write_bytes(raw)
        controls.append(m.CleanupFileV1(
            (name,), v["identity"](path), len(raw), hashlib.sha256(raw).hexdigest(),
            None, (formal,), base64.b64encode(raw).decode(),
        ))
    prefix = (v["e1"], v["e2"]) if formal == ".restore-token.json" else (
        v["e1"], v["e2"], v["e3"]
    )
    with pytest.raises(m.RestoreContractError):
        cleanup = present_cleanup(v, (*files, *controls))
        m.validate_event_chain(cleanup_chain(v, cleanup, prefix))
    assert all((v["stage"] / name).read_bytes() == raw for name in (formal, formal + ".tmp"))


@pytest.mark.parametrize("token_state", ["absent", "temporary"])
@pytest.mark.parametrize("payload_size", [0, 1, 2])
def test_first_abort_rejects_payload_without_formal_token(tmp_path, token_state, payload_size):
    v = samples(tmp_path)
    m = v["m"]
    v["token_path"].unlink()
    raw = b"db"[:payload_size]
    v["database"].write_bytes(raw)
    payload = dataclasses.replace(
        v["cleanup_present"].files[-1], size=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )
    files = (payload,)
    if token_state == "temporary":
        temporary = v["stage"] / ".restore-token.json.tmp"
        temporary.write_bytes(v["token"].to_bytes())
        files += (dataclasses.replace(
            v["token_file"], path=(temporary.name,), identity=v["identity"](temporary)
        ),)
    cleanup = present_cleanup(v, files)
    chain = cleanup_chain(v, cleanup)
    assert not v["token_path"].exists()
    with pytest.raises(m.RestoreContractError):
        m.validate_event_chain(chain)
    assert v["database"].read_bytes() == raw


@pytest.mark.parametrize("token_size", [None, 0, 5, "complete"])
def test_first_abort_accepts_empty_stage_or_lone_legal_token_temporary(tmp_path, token_size):
    v = samples(tmp_path)
    m = v["m"]
    v["database"].unlink()
    v["token_path"].unlink()
    files = ()
    if token_size is not None:
        raw = v["token"].to_bytes()
        prefix = raw if token_size == "complete" else raw[:token_size]
        temporary = v["stage"] / ".restore-token.json.tmp"
        temporary.write_bytes(prefix)
        files = (dataclasses.replace(
            v["token_file"], path=(temporary.name,), identity=v["identity"](temporary),
            size=len(prefix), sha256=hashlib.sha256(prefix).hexdigest(),
        ),)
    assert m.validate_event_chain(cleanup_chain(v, present_cleanup(v, files))) is None


def test_committed_abort_plan_replay_keeps_original_facts_after_token_deletion(tmp_path):
    v = samples(tmp_path)
    m = v["m"]
    chain = cleanup_chain(v, v["cleanup_present"])
    saved = tuple(event.to_bytes() for event in chain)
    assert m.validate_event_chain(chain) is None
    v["token_path"].unlink()
    assert v["database"].read_bytes() == b"db"
    recovered = tuple(m.RestoreEventV1.from_bytes(raw) for raw in saved)
    assert m.validate_event_chain(recovered) is None
    v["token_path"].write_bytes(v["token"].to_bytes())
    assert m.validate_event_chain(recovered) is None


@pytest.mark.parametrize("payload_size", [0, 1, 2])
def test_first_abort_accepts_partial_or_complete_copy_with_formal_token(tmp_path, payload_size):
    v = samples(tmp_path)
    m = v["m"]
    raw = b"db"[:payload_size]
    v["database"].write_bytes(raw)
    payload = dataclasses.replace(
        v["cleanup_present"].files[-1], size=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )
    cleanup = present_cleanup(v, (v["token_file"], payload))
    assert m.validate_event_chain(cleanup_chain(v, cleanup)) is None


def test_first_abort_accepts_formal_token_without_payload(tmp_path):
    v = samples(tmp_path)
    v["database"].unlink()
    cleanup = present_cleanup(v, (v["token_file"],))
    assert v["m"].validate_event_chain(cleanup_chain(v, cleanup)) is None
