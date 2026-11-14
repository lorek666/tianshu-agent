"""创建前磁盘准入：真实材料、严格测量与无写入探测。"""

from contextlib import closing
from pathlib import Path

import pytest

from tianshu.errors import TianshuError
from tianshu.events import EventLog


@pytest.mark.parametrize(
    "value,rule",
    [
        (2147483647, "dispatch.disk_space_low"),
        (0, "dispatch.disk_space_low"),
        (2147483648, None),
        (2147483649, None),
        (True, "dispatch.disk_space_unknown"),
        (-1, "dispatch.disk_space_unknown"),
        (1.5, "dispatch.disk_space_unknown"),
        (OSError("unavailable"), "dispatch.disk_space_unknown"),
    ],
)
def test_threshold_and_invalid_values(tmp_path, value, rule):
    from tianshu.disk_admission import DiskAdmission, RunStorageContext
    from tianshu.run_policy import EFFECTS, PURE_ONLY

    with closing(EventLog(tmp_path / "events.db")) as log:
        context = RunStorageContext.from_stores(
            log,
            snapshots=None,
            memory=None,
            contents=None,
            data_root=tmp_path / "guard",
            authority_root=lambda: tmp_path / "authority",
        )

        def probe(path):
            if isinstance(value, Exception):
                raise value
            return value

        gate = DiskAdmission(context, probe=probe)
        gate.require_new_run(PURE_ONLY, writable_roots=())
        if rule:
            with pytest.raises(TianshuError) as caught:
                gate.require_new_run(EFFECTS, writable_roots=())
            assert caught.value.rule_id == rule
        else:
            gate.require_new_run(EFFECTS, writable_roots=())
        assert not (tmp_path / "guard").exists()
        assert not (tmp_path / "authority").exists()


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"format_version": True, "mode": "effects"},
        {"format_version": 2, "mode": "effects"},
        {"format_version": 1, "mode": "future"},
        {"format_version": 1, "mode": "effects", "approved": True},
    ],
)
def test_policy_is_strict(value):
    from tianshu.run_policy import RunExecutionPolicyV1

    with pytest.raises(TianshuError):
        RunExecutionPolicyV1.of(value)


def test_native_probe_missing_ancestor_and_symlink(tmp_path):
    from tianshu.disk_admission import free_bytes

    target = tmp_path / "missing" / "child"
    assert type(free_bytes(target)) is int
    assert free_bytes(target) >= 0
    assert not target.parent.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    assert free_bytes(alias / "missing" / "child") >= 0
    assert not target.parent.exists()


@pytest.mark.parametrize(
    "which",
    [
        "db",
        "payload",
        "snapshot_root",
        "snapshot_blob",
        "snapshot_manifest",
        "memory",
        "content",
        "guard",
        "authority",
        "workspace_a",
        "workspace_b",
    ],
)
@pytest.mark.parametrize("invalid", [0, None])
def test_each_real_location_and_missing_ancestor(tmp_path, which, invalid):
    from tianshu.contents import ContentStore
    from tianshu.disk_admission import DiskAdmission, RunStorageContext
    from tianshu.memory import MemoryProjection
    from tianshu.run_policy import EFFECTS
    from tianshu.snapshot import SnapshotStore

    paths = {
        name: tmp_path / name
        for name in (
            "db",
            "payload",
            "snapshot_root",
            "snapshot_blob",
            "snapshot_manifest",
            "memory",
            "content",
            "guard",
            "authority",
            "workspace_a",
            "workspace_b",
        )
    }
    for path in paths.values():
        path.mkdir()
    with closing(EventLog(paths["db"] / "events.db", blob_dir=paths["payload"])) as log:
        shots = SnapshotStore(paths["snapshot_root"])
        shots.blob_dir = paths["snapshot_blob"]
        shots.manifest_dir = paths["snapshot_manifest"]
        memory = MemoryProjection(log.conn, log=log, blob_dir=paths["memory"])
        contents = ContentStore(log, blob_dir=paths["content"])
        context = RunStorageContext.from_stores(
            log,
            snapshots=shots,
            memory=memory,
            contents=contents,
            data_root=paths["guard"] / "missing",
            authority_root=lambda: paths["authority"] / "missing",
        )
        seen = []

        def probe(path):
            seen.append(path)
            if path == paths[which] or paths[which] in path.parents:
                return invalid
            return 2147483648

        with pytest.raises(TianshuError) as caught:
            DiskAdmission(context, probe=probe).require_new_run(
                EFFECTS,
                writable_roots=(paths["workspace_a"], paths["workspace_b"]),
            )
        assert caught.value.rule_id == (
            "dispatch.disk_space_low" if invalid == 0 else "dispatch.disk_space_unknown"
        )
        assert any(p == paths[which] or paths[which] in p.parents for p in seen)
        assert not (paths["guard"] / "missing").exists()
        assert not (paths["authority"] / "missing").exists()


def test_authority_resolution_is_current_each_call(tmp_path, monkeypatch):
    from tianshu.authority import authority_root
    from tianshu.disk_admission import DiskAdmission, RunStorageContext
    from tianshu.run_policy import EFFECTS

    with closing(EventLog(tmp_path / "events.db")) as log:
        seen = []
        gate = DiskAdmission(
            RunStorageContext.from_stores(
                log,
                snapshots=None,
                memory=None,
                contents=None,
                data_root=tmp_path,
                authority_root=authority_root,
            ),
            probe=lambda p: seen.append(p) or 2147483648,
        )
        for suffix in ("one", "two"):
            monkeypatch.setenv("TIANSHU_AUTHORITY_ROOT", str(tmp_path / suffix))
            gate.require_new_run(EFFECTS, writable_roots=())
            assert tmp_path / suffix / "authority.db" in seen


def test_required_content_location_cannot_be_declared_not_applicable(tmp_path):
    from tianshu.contents import ContentStore
    from tianshu.disk_admission import DiskAdmission, RunStorageContext
    from tianshu.run_policy import EFFECTS

    with closing(EventLog(tmp_path / "events.db")) as log:
        context = RunStorageContext.from_stores(
            log,
            snapshots=None,
            memory=None,
            contents=ContentStore(log),
            data_root=tmp_path,
            authority_root=lambda: tmp_path / "authority",
        )
        with pytest.raises(TianshuError) as caught:
            DiskAdmission(context, probe=lambda p: 2147483648).require_new_run(
                EFFECTS,
                writable_roots=(),
            )
        assert caught.value.rule_id == "dispatch.disk_space_unknown"


def test_memory_database_exemption_keeps_actual_disk_materials(tmp_path):
    from tianshu.disk_admission import DiskAdmission, RunStorageContext
    from tianshu.run_policy import EFFECTS

    with closing(EventLog(Path(":memory:"))) as log:
        context = RunStorageContext.from_stores(
            log,
            snapshots=None,
            memory=None,
            contents=None,
            data_root=tmp_path / "guard",
            authority_root=lambda: tmp_path / "authority",
        )
        seen = []

        def probe(path):
            seen.append(path)
            return 0 if path == tmp_path / "guard" / "execution.guard" else 2147483648

        with pytest.raises(TianshuError) as caught:
            DiskAdmission(context, probe=probe).require_new_run(EFFECTS, writable_roots=())
        assert caught.value.rule_id == "dispatch.disk_space_low"
        assert seen == [tmp_path / "guard" / "execution.guard"]
        assert not (tmp_path / "guard").exists()


def test_native_probe_uses_nearest_actual_nested_directory(tmp_path, monkeypatch):
    from tianshu.disk_admission import free_bytes

    nested = tmp_path / "workspace" / "nested-volume"
    nested.mkdir(parents=True)
    seen = []

    def disk_usage(path):
        from collections import namedtuple

        seen.append(path)
        return namedtuple("Usage", "total used free")(10, 1, 9)

    monkeypatch.setattr("tianshu.disk_admission.shutil.disk_usage", disk_usage)
    assert free_bytes(nested / "missing" / "report.txt") == 9
    assert seen == [nested]
    assert not (nested / "missing").exists()
