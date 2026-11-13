"""Collect-only developer evidence: byte fidelity is not historical execution proof."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from tianshu.eval.cases import Case
from tianshu.eval.checks import Observation, artifact_exists
from tianshu.eval.report import build_report
from tianshu.eval.runner import SuiteRun
from tianshu.loop import RunResult

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "baseline_evidence.py"
SUMMARY_KEYS = {
    "schema", "execution_status", "approval_status", "input_count",
    "metrics_present", "receipt_required", "receipt_sha256",
}


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
         "-c", f"core.hooksPath={repo / 'empty-hooks'}", "-C", str(repo), *args],
        capture_output=True, check=True,
    )
    return result.stdout


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / ".gitignore").write_bytes(b".omc/\nempty-hooks/\n")
    (repo / "tracked.txt").write_bytes(b"fixed source\n")
    git(repo, "add", ".gitignore", "tracked.txt")
    git(repo, "-c", "user.name=Evidence Fixture", "-c", "user.email=fixture@invalid",
        "commit", "--quiet", "-m", "fixture")
    return repo


@pytest.fixture
def collector():
    assert SCRIPT.is_file(), "collect-only tool is not implemented"
    spec = importlib.util.spec_from_file_location("baseline_evidence_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_report() -> dict:
    case = Case(
        case_id="COLLECT-FIXTURE", category="normal", title="collector fixture",
        task="Return a summary", fixture={}, checks=(artifact_exists(),),
    )
    observations = [
        Observation(
            case_id=case.case_id, category=case.category, run_index=index,
            run_id=f"run_collect_{index}", out_name=case.out_name, fixture={},
            files={"summary.md": b"ok\n"}, events=[],
            result=RunResult(
                run_id=f"run_collect_{index}", outcome="succeeded",
                effect_state="none_applied", state="succeeded",
            ),
        )
        for index in range(3)
    ]
    suite = SuiteRun(
        observations=observations, workdir=Path("."), elapsed_ms=1,
        identity={"adapter": "scripted", "vendor": None, "model": ""},
        suite_version="collector-fixture", repeat=3,
    )
    return build_report(suite, (case,))


@pytest.fixture
def report_file(tmp_path):
    path = tmp_path / "report.json"
    path.write_bytes(json.dumps(real_report(), ensure_ascii=False, indent=2).encode() + b"\n")
    return path


def args_for(repo, report, out, metrics=None):
    args = ["--repo", str(repo), "--report", str(report), "--out", str(out)]
    if metrics is not None:
        args.extend(["--metrics", str(metrics)])
    return args


@pytest.mark.parametrize("with_metrics", [False, True])
def test_collect_preserves_bytes_and_provenance(
    collector, source_repo, report_file, tmp_path, capsys, with_metrics,
):
    out = tmp_path / "collected"
    original = report_file.read_bytes()
    metrics = None
    if with_metrics:
        metrics = tmp_path / "metrics.jsonl"
        metrics.write_bytes(b'{"metric":"rss_mib","value":null}')
    assert collector.main(args_for(source_repo, report_file, out, metrics)) == 0
    assert capsys.readouterr() == ("", "")
    receipt_bytes = (out / "collection.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    assert set(receipt) == {
        "schema", "collection_status", "execution_status", "approval_status",
        "caller_supplied", "imported", "tool_observed",
    }
    assert receipt["schema"] == "tianshu.dev.baseline-collection.v1"
    assert receipt["collection_status"] == "COLLECTED"
    assert receipt["execution_status"] == "NOT_EXECUTED_BY_TOOL"
    assert receipt["approval_status"] == "NOT_EVALUATED"
    observed = receipt["tool_observed"]
    assert set(observed) == {
        "collection_started_at_utc", "checks_finished_at_utc", "collector_sha256",
        "git_before", "git_after", "files",
    }
    assert observed["git_before"] == observed["git_after"] == {
        "head": git(source_repo, "rev-parse", "HEAD").decode().strip(),
        "tree": git(source_repo, "rev-parse", "HEAD^{tree}").decode().strip(),
        "dirty": False,
    }
    assert observed["collector_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert (out / "inputs" / "report-0001.json").read_bytes() == original
    assert report_file.read_bytes() == original
    for item in receipt["imported"]:
        assert set(item) == {"id", "file", "historical_claims"}
        assert item["historical_claims"] == "UNVERIFIED"
        entry = next(x for x in observed["files"] if x["id"] == item["id"])
        raw = (out / item["file"]).read_bytes()
        assert entry == {"id": item["id"], "bytes": len(raw),
                         "sha256": hashlib.sha256(raw).hexdigest()}
    summary = json.loads((out / "summary.json").read_bytes())
    assert set(summary) == SUMMARY_KEYS
    assert summary == {
        "schema": "tianshu.dev.baseline-summary.v1",
        "execution_status": "NOT_EXECUTED_BY_TOOL", "approval_status": "NOT_EVALUATED",
        "input_count": 1 + int(with_metrics), "metrics_present": with_metrics,
        "receipt_required": True, "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    assert git(source_repo, "status", "--porcelain=v1") == b""
    if os.name != "nt":
        assert out.stat().st_mode & 0o777 == 0o700
        assert (out / "collection.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", [
    "missing", "directory", "duplicate", "canonical_alias", "hardlink",
    "missing_field", "wrong_type", "duplicate_key", "nan", "infinity", "overflow",
    "truncated", "no_cases", "missing_runs", "bad_digest", "bad_counts",
    "bad_validity", "bad_dimensions", "bad_schema", "bad_verdict", "root_array",
    "metrics_bad_line", "metrics_bad_tail", "metrics_empty", "metrics_array",
    "metrics_duplicate", "metrics_nan", "metrics_directory",
])
def test_rejects_incomplete_inputs(
    collector, source_repo, report_file, tmp_path, capsys, bad,
):
    report = json.loads(report_file.read_bytes())
    out = tmp_path / "collected"
    args = args_for(source_repo, report_file, out)
    metrics = tmp_path / "metrics.jsonl"
    if bad == "missing":
        report_file.unlink()
    elif bad == "directory":
        report_file.unlink()
        report_file.mkdir()
    elif bad in {"duplicate", "canonical_alias", "hardlink"}:
        other = report_file
        if bad == "canonical_alias":
            (tmp_path / "sub").mkdir()
            other = tmp_path / "sub" / ".." / report_file.name
        elif bad == "hardlink":
            other = tmp_path / "alias.json"
            os.link(report_file, other)
        args += ["--report", str(other)]
    elif bad.startswith("metrics_"):
        raw = {
            "metrics_bad_line": b'{PRIVATE_BAD_JSON}\n',
            "metrics_bad_tail": b'{"metric":"rss_mib","value":null}\n{"PRIVATE_TAIL":',
            "metrics_empty": b'\n  \n', "metrics_array": b'[]\n',
            "metrics_duplicate": b'{"metric":"rss_mib","metric":"cpu_pct"}\n',
            "metrics_nan": b'{"metric":"rss_mib","value":NaN}\n',
        }.get(bad)
        if raw is None:
            metrics.mkdir()
        else:
            metrics.write_bytes(raw)
        args += ["--metrics", str(metrics)]
    else:
        if bad == "missing_field":
            del report["overall"]
        elif bad == "wrong_type":
            report["repeat"] = True
        elif bad == "no_cases":
            report["cases"] = []
        elif bad == "missing_runs":
            del report["cases"][0]["run_details"]
        elif bad == "bad_digest":
            report["cases"][0]["fixture_digest"] = "unknown"
        elif bad == "bad_counts":
            report["overall"]["runs"] = 0
        elif bad == "bad_validity":
            report["validity"]["valid"] = 0
        elif bad == "bad_dimensions":
            del report["dimensions"]["safety"]
        elif bad == "bad_schema":
            report["schema"] = "PRIVATE_SCHEMA"
        elif bad == "bad_verdict":
            report["verdict"] = "PRIVATE_VERDICT"
        elif bad == "root_array":
            report = []
        raw = json.dumps(report).encode()
        if bad == "duplicate_key":
            raw = b'{"schema":"tianshu.eval.v1",' + raw[1:]
        elif bad in {"nan", "infinity", "overflow"}:
            number = {"nan": b"NaN", "infinity": b"Infinity", "overflow": b"1e400"}[bad]
            raw = b'{"private_number":' + number + b',' + raw[1:]
        elif bad == "truncated":
            raw = raw[:-1]
        report_file.write_bytes(raw)
    assert collector.main(args) == 2
    assert not (out / "collection.json").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "baseline-evidence: INPUT_INVALID\n"
    assert "PRIVATE" not in captured.err


@pytest.mark.parametrize("state", ["fail", "unsafe", "unknown", "subset", "old_suite"])
def test_preserves_unapproved_report_states(
    collector, source_repo, report_file, tmp_path, state,
):
    report = real_report()
    if state in {"fail", "unsafe", "unknown"}:
        report["verdict"] = state
    if state == "unknown":
        report["validity"]["valid"] = 0
        report["validity"]["invalid"] = 3
    if state == "subset":
        report["selection"]["complete"] = False
    if state == "old_suite":
        report["suite_version"] = "m4.4"
    report_file.write_bytes(json.dumps(report).encode())
    out = tmp_path / "collected"
    assert collector.main(args_for(source_repo, report_file, out)) == 0
    assert (out / "inputs" / "report-0001.json").read_bytes() == report_file.read_bytes()
    assert json.loads((out / "collection.json").read_bytes())["approval_status"] == "NOT_EVALUATED"


def test_private_claims_never_become_public(
    collector, source_repo, report_file, tmp_path, monkeypatch, capsys,
):
    report = real_report()
    report["model"] = {"adapter": "real", "model": "PRIVATE_MODEL", "url": "PRIVATE_URL"}
    report["command"] = f"touch {tmp_path / 'MUST_NOT_EXIST'}"
    report["start"] = report["end"] = report["exit_code"] = "PRIVATE_CLAIM"
    report["text"] = "PRIVATE_TOKEN_BODY"
    report_file.write_bytes(json.dumps(report).encode())
    out = tmp_path / "collected"
    monkeypatch.setattr(sys, "argv", ["PRIVATE_PROG"])
    assert collector.main(args_for(source_repo, report_file, out)) == 0
    assert capsys.readouterr() == ("", "")
    summary = (out / "summary.json").read_bytes()
    assert b"PRIVATE" not in summary
    assert set(json.loads(summary)) == SUMMARY_KEYS
    observed = json.loads((out / "collection.json").read_bytes())["tool_observed"]
    assert not ({"command", "start", "end", "exit_code", "model"} & set(observed))
    assert not (tmp_path / "MUST_NOT_EXIST").exists()
    for extra in [["--PRIVATE_UNKNOWN", "PRIVATE_TOKEN"], ["--metrics"],
                  ["--metrics", str(report_file), "--metrics", str(report_file)]]:
        assert collector.main(args_for(source_repo, report_file, tmp_path / "bad") + extra) == 2
        result = capsys.readouterr()
        assert result.out == "" and "PRIVATE" not in result.err
    assert collector.main(["--help"]) == 0
    assert "PRIVATE" not in capsys.readouterr().out


def test_collect_has_no_execution_or_discovery(
    collector, source_repo, report_file, tmp_path, monkeypatch,
):
    import tianshu.eval
    import tianshu.eval.runner
    import tianshu.eval.tasks

    adjacent = tmp_path / "ipc.key"
    adjacent.write_bytes(b"PRIVATE_DO_NOT_READ")
    report = real_report()
    report["manifest"] = {"key": str(adjacent), "workspace": str(tmp_path)}
    report_file.write_bytes(json.dumps(report).encode())
    parent = source_repo / ".omc" / "artifacts"
    parent.mkdir(parents=True)
    read_bytes = Path.read_bytes
    run = subprocess.run

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected model, network, or discovery")

    def read_explicit(path):
        assert path != adjacent, "followed an imported path"
        return read_bytes(path)

    def git_only(argv, **kwargs):
        assert argv[0] == "git" and kwargs.get("shell") is False
        assert "--no-index" not in argv
        assert any(x in argv for x in ("rev-parse", "status", "check-ignore", "ls-files"))
        if "ls-files" in argv:
            assert "--literal-pathspecs" in argv and "-z" in argv
        return run(argv, **kwargs)

    monkeypatch.setenv("TIANSHU_EVAL_REAL_MODEL", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE_ENV_KEY")
    monkeypatch.setattr(tianshu.eval, "run_suite", forbidden)
    monkeypatch.setattr(tianshu.eval.runner, "run_suite", forbidden)
    monkeypatch.setattr(tianshu.eval.tasks, "real_model_factory", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(Path, "read_bytes", read_explicit)
    monkeypatch.setattr(collector.subprocess, "run", git_only)
    assert collector.main(args_for(source_repo, report_file, parent / "collected")) == 0


def test_output_must_be_untracked_or_ignored(
    collector, source_repo, report_file, tmp_path, capsys,
):
    bad = source_repo / "unignored-output"
    assert collector.main(args_for(source_repo, report_file, bad)) == 2
    assert not bad.exists()
    assert "PRIVATE" not in capsys.readouterr().err
    parent = source_repo / ".omc" / "artifacts"
    parent.mkdir(parents=True)
    assert collector.main(args_for(source_repo, report_file, parent / "new")) == 0
    assert git(source_repo, "status", "--porcelain=v1") == b""


@pytest.mark.parametrize("existing_parent", [False, True])
def test_be02_directory_ignore_accepts_new_output(
    collector, source_repo, report_file, tmp_path, capsys, existing_parent,
):
    (source_repo / ".gitignore").write_bytes(b"/ignored/\n")
    git(source_repo, "add", ".gitignore")
    git(source_repo, "-c", "user.name=Evidence Fixture", "-c", "user.email=fixture@invalid",
        "commit", "--quiet", "-m", "directory ignore")
    out = source_repo / "ignored"
    if existing_parent:
        out.mkdir()
        out = out / "new"
        assert git(source_repo, "check-ignore", "-q", "--", str(out)) == b""
    else:
        with pytest.raises(subprocess.CalledProcessError) as rejected:
            git(source_repo, "check-ignore", "-q", "--", str(out))
        assert rejected.value.returncode == 1
        assert git(source_repo, "check-ignore", "-q", "--", out.as_posix() + "/") == b""
    assert not out.exists()
    assert git(source_repo, "status", "--porcelain=v1") == b""
    assert collector.main(args_for(source_repo, report_file, out)) == 0
    assert capsys.readouterr() == ("", "")
    assert (out / "collection.json").is_file()
    assert git(source_repo, "status", "--porcelain=v1") == b""


def test_be02_literal_output_ignores_neighboring_tracked_name(
    collector, source_repo, report_file, capsys,
):
    (source_repo / ".gitignore").write_bytes(b"/*/\n")
    neighbor = source_repo / "ignoreda"
    neighbor.write_bytes(b"tracked neighboring file\n")
    git(source_repo, "add", ".gitignore", "ignoreda")
    git(source_repo, "-c", "user.name=Evidence Fixture", "-c", "user.email=fixture@invalid",
        "commit", "--quiet", "-m", "literal output neighbor")
    out = source_repo / "ignored[ab]"
    assert git(source_repo, "ls-files", "-z", "--", str(out)) == b"ignoreda\0"
    assert git(source_repo, "--literal-pathspecs", "ls-files", "-z", "--", str(out)) == b""
    assert git(source_repo, "check-ignore", "-q", "--", out.as_posix() + "/") == b""
    before_index = git(source_repo, "ls-files", "--stage", "-z")
    assert collector.main(args_for(source_repo, report_file, out)) == 0
    assert capsys.readouterr() == ("", "")
    assert (out / "collection.json").is_file()
    assert neighbor.read_bytes() == b"tracked neighboring file\n"
    assert git(source_repo, "ls-files", "--stage", "-z") == before_index
    assert git(source_repo, "status", "--porcelain=v1") == b""


@pytest.mark.parametrize("tracked_kind", ["file", "subtree"])
@pytest.mark.parametrize("basename", ["ignored", "ignored[ab]"])
def test_be02_missing_tracked_output_is_rejected(
    collector, source_repo, report_file, tmp_path, capsys, tracked_kind, basename,
):
    foreign = tmp_path / "output-repo"
    foreign.mkdir()
    git(foreign, "init", "--quiet")
    ignore = b"/ignored/\n" if basename == "ignored" else b"/*/\n"
    (foreign / ".gitignore").write_bytes(ignore)
    out = foreign / basename
    tracked = out
    if tracked_kind == "subtree":
        out.mkdir()
        tracked = out / "tracked.txt"
    tracked.write_bytes(b"tracked output must not be replaced\n")
    git(foreign, "add", "-f", ".gitignore", str(tracked))
    git(foreign, "-c", "user.name=Evidence Fixture", "-c", "user.email=fixture@invalid",
        "commit", "--quiet", "-m", "tracked output")
    tracked.unlink()
    if tracked_kind == "subtree":
        out.rmdir()
    assert not out.exists()
    indexed = git(foreign, "--literal-pathspecs", "ls-files", "-z", "--", str(out))
    assert indexed
    try:
        git(foreign, "check-ignore", "-q", "--", out.as_posix() + "/")
    except subprocess.CalledProcessError as rejected:
        assert rejected.returncode == 1
        assert tracked_kind == "subtree"
    before_status = git(foreign, "status", "--porcelain=v1", "-z")
    before_index = git(foreign, "ls-files", "--stage", "-z")
    assert collector.main(args_for(source_repo, report_file, out)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err in {
        "baseline-evidence: OUTPUT_NOT_IGNORED\n", "baseline-evidence: OUTPUT_TRACKED\n",
    }
    assert not out.exists()
    assert not (out / "collection.json").exists()
    assert git(foreign, "status", "--porcelain=v1", "-z") == before_status
    assert git(foreign, "ls-files", "--stage", "-z") == before_index
    assert (foreign / ".gitignore").read_bytes() == ignore
    assert git(source_repo, "status", "--porcelain=v1") == b""


@pytest.mark.parametrize("command", ["ls-files", "check-ignore"])
@pytest.mark.parametrize("failure", ["exit", "io"])
def test_be02_git_lookup_errors_fail_closed(
    collector, source_repo, report_file, tmp_path, monkeypatch, capsys, command, failure,
):
    parent = source_repo / ".omc" / "artifacts"
    parent.mkdir(parents=True)
    out = parent / "new"
    run_git = collector._git
    fired = []

    def failing_git(repo, *args):
        if command in args:
            fired.append(command)
            if failure == "io":
                raise OSError("PRIVATE_GIT_IO_FAILURE")
            return subprocess.CompletedProcess(args, 128, b"", b"PRIVATE_GIT_FAILURE")
        return run_git(repo, *args)

    monkeypatch.setattr(collector, "_git", failing_git)
    assert collector.main(args_for(source_repo, report_file, out)) == 1
    assert fired == [command]
    category = "IO_FAILED" if failure == "io" else "GIT_FAILED"
    assert capsys.readouterr() == ("", f"baseline-evidence: {category}\n")
    assert not out.exists()
    assert not (out / "collection.json").exists()


@pytest.mark.parametrize("change", ["stable", "changed", "removed", "permission_error"])
def test_collector_drift_during_initial_input_read(
    collector, source_repo, report_file, tmp_path, monkeypatch, capsys, change,
):
    local_script = tmp_path / "collector-source.py"
    original_source = SCRIPT.read_bytes()
    local_script.write_bytes(original_source)
    monkeypatch.setattr(collector, "__file__", str(local_script))
    read_bytes = Path.read_bytes
    checkpoints = []
    blocked = False

    def read_with_early_change(path):
        nonlocal blocked
        if path == local_script and blocked:
            raise PermissionError("PRIVATE_EARLY_SOURCE_DENIAL")
        raw = read_bytes(path)
        if path == report_file and not checkpoints:
            checkpoints.append("initial_report_read_completed")
            if change == "changed":
                local_script.write_bytes(original_source + b"\n# early collector change\n")
            elif change == "removed":
                local_script.unlink()
            elif change == "permission_error":
                blocked = True
        return raw

    monkeypatch.setattr(Path, "read_bytes", read_with_early_change)
    out = tmp_path / "collected"
    code = collector.main(args_for(source_repo, report_file, out))
    assert checkpoints == ["initial_report_read_completed"]
    assert git(source_repo, "status", "--porcelain=v1") == b""
    captured = capsys.readouterr()
    assert captured.out == ""
    if change == "stable":
        assert code == 0
        assert captured.err == ""
        receipt = json.loads(read_bytes(out / "collection.json"))
        assert receipt["tool_observed"]["collector_sha256"] == hashlib.sha256(
            original_source,
        ).hexdigest()
    else:
        assert code == 1, (
            f"early {change}: exit={code}, receipt={(out / 'collection.json').exists()}"
        )
        assert captured.err in {
            "baseline-evidence: IO_FAILED\n", "baseline-evidence: SOURCE_CHANGED\n",
        }
        assert not (out / "collection.json").exists()
        assert "PRIVATE" not in captured.err


@pytest.mark.parametrize("failure", ["removed", "permission_error"])
def test_collector_identity_precedes_admission_checks(
    collector, source_repo, report_file, tmp_path, monkeypatch, capsys, failure,
):
    local_script = tmp_path / "collector-source.py"
    local_script.write_bytes(SCRIPT.read_bytes())
    monkeypatch.setattr(collector, "__file__", str(local_script))
    if failure == "removed":
        local_script.unlink()
    read_bytes, run_git, output_path = Path.read_bytes, collector._git, collector._output_path
    touches = []

    def read(path):
        if path == local_script:
            touches.append("collector")
            if failure == "permission_error":
                raise PermissionError("PRIVATE_ENTRY_SOURCE_DENIAL")
        elif path == report_file:
            touches.append("report")
        return read_bytes(path)

    def observed_git(*args, **kwargs):
        touches.append("git")
        return run_git(*args, **kwargs)

    def observed_output(*args, **kwargs):
        touches.append("output")
        return output_path(*args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(collector, "_git", observed_git)
    monkeypatch.setattr(collector, "_output_path", observed_output)
    out = tmp_path / "collected"
    assert collector.main(args_for(source_repo, report_file, out)) == 1
    assert capsys.readouterr() == ("", "baseline-evidence: IO_FAILED\n")
    assert touches == ["collector"]
    assert not out.exists()


@pytest.mark.parametrize("change", [
    "tracked", "head", "input", "collector", "input_removed", "collector_removed",
])
def test_observed_drift_prevents_receipt(
    collector, source_repo, report_file, tmp_path, monkeypatch, capsys, change,
):
    out = tmp_path / "collected"
    original = report_file.read_bytes()
    local_script = tmp_path / "collector-source.py"
    local_script.write_bytes(SCRIPT.read_bytes())
    monkeypatch.setattr(collector, "__file__", str(local_script))
    write = collector._write_private

    def change_after_copy(path, raw):
        write(path, raw)
        if path.name != "report-0001.json":
            return
        if change in {"tracked", "head"}:
            (source_repo / "tracked.txt").write_bytes(b"changed source\n")
            if change == "head":
                git(source_repo, "add", "tracked.txt")
                git(source_repo, "-c", "user.name=Evidence Fixture",
                    "-c", "user.email=fixture@invalid", "commit", "--quiet", "-m", "changed")
        elif change == "input":
            report_file.write_bytes(original + b" ")
        elif change == "input_removed":
            report_file.unlink()
        elif change == "collector_removed":
            local_script.unlink()
        else:
            local_script.write_bytes(local_script.read_bytes() + b"\n# changed\n")

    monkeypatch.setattr(collector, "_write_private", change_after_copy)
    assert collector.main(args_for(source_repo, report_file, out)) == 1
    assert capsys.readouterr().err == "baseline-evidence: SOURCE_CHANGED\n"
    assert not (out / "collection.json").exists()
    assert (out / "inputs" / "report-0001.json").read_bytes() == original


def test_initial_dirty_is_not_archived(collector, source_repo, report_file, tmp_path, capsys):
    (source_repo / "tracked.txt").write_bytes(b"dirty\n")
    out = tmp_path / "collected"
    assert collector.main(args_for(source_repo, report_file, out)) == 2
    assert capsys.readouterr().err == "baseline-evidence: REPO_DIRTY\n"
    assert not out.exists()


@pytest.mark.parametrize("phase", ["copy", "summary", "temporary", "replace"])
def test_output_failures_prevent_receipt(
    collector, source_repo, report_file, tmp_path, monkeypatch, capsys, phase,
):
    out = tmp_path / "collected"
    original = report_file.read_bytes()
    write = collector._write_private
    failure_name = {"copy": "report-0001.json", "summary": "summary.json",
                    "temporary": "collection.json.tmp"}.get(phase)

    def fail_write(path, payload):
        if path.name == failure_name:
            raise OSError("PRIVATE_WRITE_FAILURE")
        return write(path, payload)

    def fail_replace(*args):
        raise OSError("PRIVATE_REPLACE_FAILURE")

    monkeypatch.setattr(collector, "_write_private", fail_write)
    if phase == "replace":
        monkeypatch.setattr(collector.os, "replace", fail_replace)
    assert collector.main(args_for(source_repo, report_file, out)) == 1
    assert capsys.readouterr() == ("", "baseline-evidence: IO_FAILED\n")
    assert not (out / "collection.json").exists()
    assert report_file.read_bytes() == original
    if phase != "copy":
        assert (out / "inputs" / "report-0001.json").read_bytes() == original


@pytest.mark.parametrize("nonempty", [False, True])
def test_existing_output_is_never_overwritten(
    collector, source_repo, report_file, tmp_path, capsys, nonempty,
):
    out = tmp_path / "collected"
    out.mkdir()
    if nonempty:
        (out / "collection.json").write_bytes(b"existing receipt\n")
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert collector.main(args_for(source_repo, report_file, out)) == 2
    assert capsys.readouterr().err == "baseline-evidence: OUTPUT_EXISTS\n"
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def clean_child_env():
    env = {
        key: value for key, value in os.environ.items()
        if not (key.startswith("TIANSHU_") or any(
            token in key.upper() for token in ("API_KEY", "API_TOKEN", "BASE_URL", "ENDPOINT")
        ) or key.upper().endswith(("_KEY", "_TOKEN")))
    }
    env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
    return env


BARRIER_BOOTSTRAP = r'''
import importlib.util
import sys

script, phase = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("collector_child", script)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
publish = collector._publish_receipt

def blocked_publish(path, payload):
    if phase == "after":
        publish(path, payload)
    sys.stdout.buffer.write(b"RECEIPT_BARRIER\n")
    sys.stdout.buffer.flush()
    if sys.stdin.buffer.readline() != b"CONTINUE\n":
        raise RuntimeError("test barrier closed")
    if phase == "before":
        publish(path, payload)

collector._publish_receipt = blocked_publish
raise SystemExit(collector.main(sys.argv[3:]))
'''


@pytest.mark.parametrize("phase", ["before", "after"])
def test_cli_child_commit_boundary(source_repo, report_file, tmp_path, phase):
    out = tmp_path / "collected"
    process = subprocess.Popen(
        [sys.executable, "-c", BARRIER_BOOTSTRAP, str(SCRIPT), phase,
         *args_for(source_repo, report_file, out)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=clean_child_env(),
    )
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert ready.get(timeout=15) == b"RECEIPT_BARRIER\n", "collector missed publish barrier"
        assert process.poll() is None
        assert (out / "inputs" / "report-0001.json").read_bytes() == report_file.read_bytes()
        assert (out / "collection.json").exists() == (phase == "after")
        process.terminate()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode != 0
        assert stdout == stderr == b""
        assert (out / "collection.json").exists() == (phase == "after")
        if phase == "after":
            observed = json.loads((out / "collection.json").read_bytes())["tool_observed"]
            assert "exit_code" not in observed
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
        reader.join(timeout=5)
        assert not reader.is_alive()


def test_cli_real_success_and_failure(source_repo, report_file, tmp_path):
    out = tmp_path / "collected"
    success = subprocess.run(
        [sys.executable, str(SCRIPT), *args_for(source_repo, report_file, out)],
        env=clean_child_env(), capture_output=True, timeout=15,
    )
    assert success.returncode == 0
    assert success.stdout == success.stderr == b""
    assert (out / "collection.json").is_file()
    missing = tmp_path / "PRIVATE_MISSING"
    failure = subprocess.run(
        [sys.executable, str(SCRIPT), *args_for(source_repo, missing, tmp_path / "bad")],
        env=clean_child_env(), capture_output=True, timeout=15,
    )
    assert failure.returncode == 2
    assert failure.stdout == b""
    assert failure.stderr == b"baseline-evidence: INPUT_INVALID\n"
    assert not (tmp_path / "bad" / "collection.json").exists()
