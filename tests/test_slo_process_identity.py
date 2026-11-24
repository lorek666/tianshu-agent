"""SLO orphan evidence belongs to a process instance, including unreadable identities."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu.execution_admission import process_birth_identity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_slo as slo  # noqa: E402


@pytest.mark.skipif(os.name == "nt", reason="POSIX command-line marker scan")
def test_marker_only_orphan_retains_its_source(tmp_path, live_process, monkeypatch, capsys):
    _, pid, _ = live_process
    marker = tmp_path / "private-marker"
    run = slo.subprocess.run

    def marked_ps(argv, **kwargs):
        if argv == ["ps", "-axo", "pid=,command="]:
            # Only discovery is injected; kernel identity and liveness stay real.
            return SimpleNamespace(returncode=0, stdout=f"{pid} python {marker}\n")
        return run(argv, **kwargs)

    monkeypatch.setattr(slo.subprocess, "run", marked_ps)
    clock = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    tracked = slo.TrackedPids()
    assert slo._find_orphan_pids(tracked, marker) == [pid]
    evidence = json.loads(capsys.readouterr().err)
    assert evidence["process_sources"][str(pid)] == ["command_line_marker"]
    assert evidence["process_bindings"][0]["source"] == "command_line_marker"
    assert str(marker) not in json.dumps(evidence)


@pytest.mark.parametrize("field", ["action", "outcome"])
@pytest.mark.parametrize("value", [[], {}])
def test_ill_typed_stop_diagnostics_do_not_abort_capture(live_process, field, value):
    child, pid, _ = live_process
    record = {
        "pid": pid, "observer_pid": os.getpid(), "monotonic_ns": 42,
        "action": "terminate_requested", "returncode": None,
        "result": {"outcome": "command_timeout"},
    }
    if field == "action":
        record["action"] = value
    else:
        record["result"]["outcome"] = value
    tracked = slo.TrackedPids()
    slo._capture_host_stop(
        child, tracked,
        io.BytesIO(json.dumps({"host_supervisor_process": record}).encode()),
        phase="after_wait",
    )
    assert tracked.host_stops[0]["source"] == "owned_host_root"
    assert len(tracked.host_stops) == (1 if field == "action" else 2)
    assert all("terminate_outcome" not in row for row in tracked.host_stops)


def test_recovery_daemon_launcher_drains_unread_stderr_to_a_file():
    payload_bytes = 256 * 1024
    proc, stderr = slo._launch_recovery_daemon(
        [sys.executable, "-c",
         f"import sys; sys.stderr.buffer.write(b'x' * {payload_bytes}); "
         "sys.stderr.buffer.flush()"],
        env=dict(os.environ),
    )
    try:
        assert proc.wait(timeout=5) == 0
        stderr.seek(0, os.SEEK_END)
        assert stderr.tell() == payload_bytes
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        stderr.close()


@pytest.fixture
def live_process():
    child = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import os,sys; print(os.getpid(), flush=True); sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        pid = int(child.stdout.readline())
        birth = process_birth_identity(pid)
        assert birth is not None
        yield child, pid, birth
    finally:
        if child.poll() is None:
            assert child.stdin is not None
            child.stdin.write("G")
            child.stdin.flush()
        child.wait(timeout=10)
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()


@pytest.mark.parametrize("identity", ["same", "different", "missing_initial", "missing_final"])
def test_orphan_check_requires_the_recorded_instance_or_fails_closed(
    tmp_path, monkeypatch, live_process, identity,
):
    _, pid, birth = live_process
    other_birth = process_birth_identity(os.getpid())
    assert other_birth is not None and other_birth != birth
    recorded = birth if identity == "same" else other_birth
    if identity == "missing_initial":
        recorded = None
    if identity == "missing_final":
        monkeypatch.setattr(slo, "process_birth_identity", lambda _pid: None, raising=False)
    # Keep the real kernel liveness/birth checks; advance only the grace clock.
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    found = slo._find_orphan_pids({pid: {recorded}}, tmp_path / "unused-marker")
    assert found == ([] if identity == "different" else [pid])


@pytest.mark.parametrize("current_first", [False, True])
def test_recording_a_reused_pid_keeps_every_observed_instance(
    tmp_path, monkeypatch, live_process, current_first,
):
    _, pid, birth = live_process
    other_birth = process_birth_identity(os.getpid())
    assert other_birth is not None and other_birth != birth
    sequence = [birth, other_birth] if current_first else [other_birth, birth]
    identities = iter(sequence)
    monkeypatch.setattr(slo, "process_birth_identity", lambda _pid: next(identities))
    tracked = {}
    slo._record_pid(pid, tracked)
    slo._record_pid(pid, tracked)
    monkeypatch.setattr(slo, "process_birth_identity", process_birth_identity)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == [pid]


@pytest.mark.parametrize("lock_name", ["host.lock", "daemon.lock"])
@pytest.mark.parametrize("later", ["missing", "different"])
def test_unchanged_lock_owner_does_not_bind_a_later_pid_occupant(
    tmp_path, monkeypatch, live_process, lock_name, later,
):
    """The PID transition is controlled; the final occupant is a real process."""
    import json

    _, pid, birth = live_process
    previous = process_birth_identity(os.getpid())
    assert previous is not None and previous != birth
    tracked = slo.TrackedPids()
    root = tmp_path / "data"
    lock = slo.ipc.run_dir(root) / lock_name
    lock.parent.mkdir(parents=True)
    owner = {"pid": pid, "started_at": 123, "instance_id": "old-daemon"}
    if lock_name == "host.lock":
        owner = {"pid": pid, "port": 0, "started_at": 123,
                 "token": "private-old-host", "ui_dir": ""}
    lock.write_text(json.dumps(owner))
    monkeypatch.setattr(slo, "process_birth_identity", lambda queried: previous)
    slo._record_lock_pids(root, tracked)
    if lock_name == "host.lock":
        owner["port"] = 47123
        lock.write_text(json.dumps(owner))
    monkeypatch.setattr(slo, "process_birth_identity",
                        lambda queried: None if later == "missing" else birth)
    slo._record_lock_pids(root, tracked)
    monkeypatch.setattr(slo, "process_birth_identity", process_birth_identity)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == []


@pytest.mark.parametrize("lock_name", ["host.lock", "daemon.lock"])
def test_new_lock_owner_reusing_a_pid_is_still_a_residual(
    tmp_path, monkeypatch, live_process, lock_name,
):
    import json

    _, pid, birth = live_process
    previous = process_birth_identity(os.getpid())
    assert previous is not None and previous != birth
    tracked = slo.TrackedPids()
    root = tmp_path / "data"
    lock = slo.ipc.run_dir(root) / lock_name
    lock.parent.mkdir(parents=True)
    owner = {"pid": pid, "started_at": 123, "instance_id": "old-daemon"}
    identity_key = "instance_id"
    if lock_name == "host.lock":
        owner = {"pid": pid, "port": 47123, "started_at": 123,
                 "token": "private-old-host", "ui_dir": ""}
        identity_key = "token"
    lock.write_text(json.dumps(owner))
    monkeypatch.setattr(slo, "process_birth_identity", lambda queried: previous)
    slo._record_lock_pids(root, tracked)
    owner[identity_key] = "private-new-owner"
    lock.write_text(json.dumps(owner))
    monkeypatch.setattr(slo, "process_birth_identity", process_birth_identity)
    slo._record_lock_pids(root, tracked)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == [pid]


def test_orphan_failure_exposes_instance_observations(
    tmp_path, monkeypatch, live_process, capsys,
):
    import json

    _, pid, birth = live_process
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids({pid: {birth}}, tmp_path / "unused-marker") == [pid]
    evidence = capsys.readouterr().err
    assert evidence, "A residual must include its observed native identity facts"
    report = json.loads(evidence)
    observation = report["orphan_process_observations"][0]
    assert observation["pid"] == pid
    assert observation["recorded_births"] == [birth]
    assert observation["pid_alive"] is True and observation["current_birth"] == birth


def test_failure_keeps_each_lock_owner_bound_to_its_first_and_last_observation(
    tmp_path, monkeypatch, live_process, capsys,
):
    import json

    _, pid, birth = live_process
    tracked = slo.TrackedPids()
    lock = slo.ipc.run_dir(tmp_path) / "daemon.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": pid, "started_at": 1, "instance_id": "private-owner"}))
    slo._record_lock_pids(tmp_path, tracked, phase="ready")
    monkeypatch.setattr(slo, "process_birth_identity", lambda pid: None)
    slo._record_lock_pids(tmp_path, tracked, phase="before_stop")
    monkeypatch.setattr(slo, "process_birth_identity", process_birth_identity)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == [pid]
    text = capsys.readouterr().err
    assert "private-owner" not in text and str(tmp_path) not in text
    report = json.loads(text)
    row = report["process_bindings"][0]
    assert row["pid"] == pid and row["recorded_births"] == [birth]
    assert row["first_birth"] == birth and row["last_birth"] is None
    assert row["first_phase"] == "ready" and row["last_phase"] == "before_stop"
    assert row["first_monotonic_ns"] <= row["last_monotonic_ns"]
    assert report["orphan_process_observations"][0]["reason"] == "recorded_instance_running"
    assert report["orphan_process_observations"][0]["monotonic_ns"] >= row["last_monotonic_ns"]


def test_failure_keeps_direct_host_and_daemon_wait_facts(
    tmp_path, monkeypatch, live_process, capsys,
):
    import json

    child, pid, birth = live_process
    tracked = slo.TrackedPids()
    slo._record_pid(pid, tracked)
    stderr = tmp_path / "host-stderr"
    stderr.write_text(json.dumps({"host_supervisor_process": {
        "pid": pid, "observer_pid": 123, "action": "final_wait_timeout",
        "monotonic_ns": 456, "returncode": None,
        "argv": ["private-token"], "secret": "private-token",
    }}) + "\n")
    with stderr.open("rb") as stream:
        slo._capture_host_stop(child, tracked, stream, phase="final_stop")
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == [pid]
    text = capsys.readouterr().err
    assert "private-token" not in text and str(tmp_path) not in text
    rows = json.loads(text)["host_stop_observations"]
    assert any(row["source"] == "owned_host_root" and row["returncode"] is None for row in rows)
    assert any(row["source"] == "owned_daemon_root"
               and row["action"] == "final_wait_timeout" for row in rows)


@pytest.mark.parametrize("lock_name", ["host.lock", "daemon.lock"])
def test_lock_owner_with_unreadable_initial_identity_stays_conservative(
    tmp_path, monkeypatch, live_process, lock_name,
):
    import json

    _, pid, _ = live_process
    tracked = slo.TrackedPids()
    lock = slo.ipc.run_dir(tmp_path) / lock_name
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({
        "pid": pid, "port": 47123, "started_at": 123,
        "token": "private-host", "instance_id": "private-daemon",
    }))
    monkeypatch.setattr(slo, "process_birth_identity", lambda queried: None)
    slo._record_lock_pids(tmp_path, tracked)
    monkeypatch.setattr(slo, "process_birth_identity", process_birth_identity)
    slo._record_lock_pids(tmp_path, tracked)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == [pid]


@pytest.mark.parametrize("lock_name", ["host.lock", "daemon.lock"])
def test_same_lock_metadata_in_another_data_root_is_a_distinct_owner(
    tmp_path, monkeypatch, live_process, lock_name,
):
    import json

    _, pid, birth = live_process
    previous = process_birth_identity(os.getpid())
    assert previous is not None and previous != birth
    tracked = slo.TrackedPids()
    for index in (0, 1):
        root = tmp_path / str(index)
        lock = slo.ipc.run_dir(root) / lock_name
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({
            "pid": pid, "port": 47123, "started_at": 123,
            "token": "private-host", "instance_id": "private-daemon",
        }))
        monkeypatch.setattr(slo, "process_birth_identity",
                            (lambda queried: previous) if index == 0 else process_birth_identity)
        slo._record_lock_pids(root, tracked)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(slo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert slo._find_orphan_pids(tracked, tmp_path / "unused-marker") == [pid]
