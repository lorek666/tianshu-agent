"""SLO orphan evidence belongs to a process instance, including unreadable identities."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu.execution_admission import process_birth_identity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_slo as slo  # noqa: E402


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
