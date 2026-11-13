"""Exercise CI sharding through real isolated pytest processes and verifier CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
WRAPPER = SCRIPTS / "pytest_with_stacks.py"
VERIFIER = SCRIPTS / "pytest_ci_shards.py"
RUN_ID, ATTEMPT = "123456", "2"
FILES = {
    "tests/test_a.py": "def test_zeta(): pass\ndef test_alpha(): pass\ndef test_mid(): pass\n",
    "tests/test_b.py": "def test_b1(): pass\ndef test_b2(): pass\n",
    "tests/test_c.py": "def test_c(): pass\n",
}


def isolated_env():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("TIANSHU_", "GITHUB_", "OPENAI_", "ANTHROPIC_",
                                  "AZURE_OPENAI_", "LITELLM_", "NEWAPI_", "AIZHONGZHUAN_"))
           and not any(word in key.upper() for word in
                       ("KEY", "TOKEN", "SECRET", "ENDPOINT", "BASE_URL", "API_BASE", "MODEL"))}
    env.update(PYTHONPATH=str(SCRIPTS), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               PYTEST_ADDOPTS="", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.pop("PYTHONFAULTHANDLER", None)
    return env


def run(args, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)


def project(root, files):
    root.mkdir()
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    env = isolated_env()
    env.update(GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00")
    for args in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=CI Probe", "-c", "user.email=probe@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "test fixture"]):
        result = run(args, root, env)
        assert result.returncode == 0, result.stdout + result.stderr
    result = run(["git", "rev-parse", "HEAD"], root, env)
    assert result.returncode == 0, result.stderr
    env.update(GITHUB_SHA=result.stdout.strip(), GITHUB_RUN_ID=RUN_ID,
               GITHUB_RUN_ATTEMPT=ATTEMPT)
    return env


def shards(tmp_path, files=None, extra=(), extra_env=None):
    files = FILES if files is None else files
    evidence = tmp_path / "reports"
    results, documents = [], []
    for index in range(2):
        root = tmp_path / f"checkout-{index}"
        env = project(root, files)
        # Both runners check out exactly the same Git object, including commit metadata.
        if index == 0:
            first_env = dict(env)
        else:
            assert env["GITHUB_SHA"] == first_env["GITHUB_SHA"]
        env.update(extra_env or {})
        report = evidence / str(index) / "shard.json"
        args = [sys.executable, str(WRAPPER), "-p", "pytest_ci_shards",
                f"--ci-shard-index={index}", "--ci-shard-count=2",
                f"--ci-shard-report={report}", "-o", "addopts=", "-q",
                "--stack-timeout=60", *extra]
        results.append(run(args, root, env))
        documents.append(json.loads(report.read_text()) if report.exists() else None)
    return evidence, first_env, results, documents


def verify(evidence, env, *, needs="success", extra=()):
    return run([sys.executable, str(VERIFIER), "verify", "--reports-root", str(evidence),
                "--sha", env["GITHUB_SHA"], "--run-id", RUN_ID, "--run-attempt", ATTEMPT,
                "--count", "2", "--indices", "0", "1", "--needs-result", needs, *extra],
               evidence.parent, env)


def test_independent_runners_cover_every_file_once_in_original_collection_order(tmp_path):
    evidence, env, results, docs = shards(tmp_path)
    assert [r.returncode for r in results] == [0, 0], [(r.stdout, r.stderr) for r in results]
    expected = [
        ["tests/test_a.py::test_zeta", "tests/test_a.py"],
        ["tests/test_a.py::test_alpha", "tests/test_a.py"],
        ["tests/test_a.py::test_mid", "tests/test_a.py"],
        ["tests/test_b.py::test_b1", "tests/test_b.py"],
        ["tests/test_b.py::test_b2", "tests/test_b.py"],
        ["tests/test_c.py::test_c", "tests/test_c.py"],
    ]
    assert docs[0]["full_collection"] == docs[1]["full_collection"] == expected
    assert docs[0]["selected"] == [pair[0] for pair in expected[:3]]
    assert docs[1]["selected"] == [pair[0] for pair in expected[3:]]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def phase_source(phase, action, *, mark=""):
    prefix = "import pytest\n"
    if phase == "call":
        return prefix + mark + f"def test_subject():\n    {action}\n"
    fixture = (f"@pytest.fixture\ndef phase():\n    {action}\n" if phase == "setup" else
               f"@pytest.fixture\ndef phase():\n    yield\n    {action}\n")
    return prefix + fixture + "def test_subject(phase): pass\n"


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("outcome", ["skip", "xfail"])
def test_pytest_successful_skip_and_xfail_in_every_phase_are_accepted(tmp_path, phase, outcome):
    files = {**FILES, "tests/test_a.py": phase_source(phase, f"pytest.{outcome}('expected')")}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [r.returncode for r in results] == [0, 0], [(r.stdout, r.stderr) for r in results]
    row = next(row for doc in docs for row in doc["reports"]
               if row["nodeid"] == "tests/test_a.py::test_subject" and row["when"] == phase)
    assert row["outcome"] == "skipped"
    assert ("wasxfail" in row) == (outcome == "xfail")
    result = verify(evidence, env)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("strict, exitcode", [(False, 0), (True, 1)])
def test_nonstrict_xpass_is_accepted_and_strict_xpass_is_rejected(tmp_path, strict, exitcode):
    files = {**FILES, "tests/test_a.py": phase_source(
        "call", "pass", mark=f"@pytest.mark.xfail(strict={strict})\n")}
    evidence, env, results, docs = shards(tmp_path, files)
    # test_b has two nodes, so test_a and test_c are assigned to shard 1.
    assert [r.returncode for r in results] == [0, exitcode]
    call = next(row for row in docs[1]["reports"] if row["when"] == "call")
    assert call["outcome"] == ("failed" if strict else "passed")
    result = verify(evidence, env)
    assert result.returncode == exitcode, result.stdout + result.stderr


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_real_phase_failures_cannot_be_hidden_by_successful_other_phases(tmp_path, phase):
    files = {**FILES, "tests/test_a.py": phase_source(phase, "assert False, 'expected failure'")}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [r.returncode for r in results] == [0, 1]
    assert any(row["when"] == phase and row["outcome"] == "failed"
               for row in docs[1]["reports"])
    # Even a forged session success cannot hide a real failed phase.
    docs[1]["pytest_session_exit"] = 0
    (evidence / "1" / "shard.json").write_text(json.dumps(docs[1]), encoding="utf-8")
    assert verify(evidence, env).returncode == 1


def native_control(tmp_path, files):
    root = tmp_path / "native-checkout"
    env = project(root, files)
    return run([sys.executable, str(WRAPPER), "-o", "addopts=", "-q", "--stack-timeout=60"],
               root, env)


@pytest.mark.parametrize("valid_files", [0, 1, 2])
def test_collection_error_preserves_native_exit_even_with_too_few_valid_files(
    tmp_path, valid_files,
):
    files = {"tests/test_a.py": "this is invalid python!\n"}
    files.update(dict(list(FILES.items())[1:1 + valid_files]))
    native = native_control(tmp_path, files)
    assert native.returncode == 2, native.stdout + native.stderr
    assert "SyntaxError" in native.stdout
    evidence, env, results, docs = shards(tmp_path, files)
    assert [r.returncode for r in results] == [native.returncode] * 2, [
        (r.stdout, r.stderr) for r in results]
    assert [doc["pytest_session_exit"] for doc in docs] == [2, 2]
    assert verify(evidence, env).returncode == 1


@pytest.mark.parametrize("phase", ["collection-early", "collection-late", "call"])
def test_real_keyboard_interrupt_preserves_native_exit_two(tmp_path, phase):
    source = ("raise KeyboardInterrupt\n" if phase.startswith("collection")
              else "def test_interrupt(): raise KeyboardInterrupt\n")
    file = "tests/test_z.py" if phase == "collection-late" else "tests/test_a.py"
    files = {**FILES, file: source}
    native = native_control(tmp_path, files)
    assert native.returncode == 2, native.stdout + native.stderr
    assert "KeyboardInterrupt" in native.stdout + native.stderr
    evidence, env, results, docs = shards(tmp_path, files)
    expected = [0, 2] if phase == "call" else [2, 2]
    assert [r.returncode for r in results] == expected, [(r.stdout, r.stderr) for r in results]
    assert [doc["pytest_session_exit"] for doc in docs] == expected
    assert verify(evidence, env).returncode == 1


@pytest.mark.parametrize("extra", [
    ("tests",), ("tests/test_a.py::test_alpha",), ("-k", "test"), ("-m", "never"),
    ("--ignore=tests/test_a.py",), ("--ignore-glob=*test_a.py",),
    ("--deselect=tests/test_a.py::test_alpha",), ("--lf",), ("--ff",), ("--nf",),
    ("--stepwise",), ("-x",), ("--maxfail=2",), ("--collect-only",),
])
def test_subset_reordering_and_early_stop_options_are_rejected(tmp_path, extra):
    _, _, results, docs = shards(tmp_path, extra=extra)
    assert [r.returncode for r in results] == [4, 4]
    assert docs == [None, None]


@pytest.mark.parametrize("hook", [
    "def pytest_collection_modifyitems(items): items.reverse()\n",
    "def pytest_collection_modifyitems(items): items.pop()\n",
    "import pytest\n@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
    "def pytest_collection_finish(session):\n    yield\n    session.items.pop()\n",
])
def test_other_plugins_cannot_silently_change_the_recorded_collection(tmp_path, hook):
    evidence, env, results, _ = shards(tmp_path, {**FILES, "conftest.py": hook})
    assert any(r.returncode != 0 for r in results)
    assert verify(evidence, env).returncode == 1


@pytest.mark.parametrize("files", [
    {"tests/test_empty.py": ""}, {"tests/test_only.py": "def test_one(): pass\n"},
])
def test_empty_collection_or_empty_shard_is_rejected(tmp_path, files):
    evidence, env, results, _ = shards(tmp_path, files)
    assert all(r.returncode != 0 for r in results)
    assert verify(evidence, env).returncode == 1


def test_actual_checkout_must_match_ci_sha(tmp_path):
    _, _, results, docs = shards(tmp_path, extra_env={"GITHUB_SHA": "0" * 40})
    assert [r.returncode for r in results] == [4, 4]
    assert docs == [None, None]


@pytest.fixture(scope="module")
def complete_evidence(tmp_path_factory):
    evidence, env, results, docs = shards(tmp_path_factory.mktemp("complete-shards"))
    assert [r.returncode for r in results] == [0, 0]
    assert verify(evidence, env).returncode == 0
    return env, docs


@pytest.mark.parametrize("mutation", [
    "missing-artifact", "extra-artifact", "bad-json", "version", "checkout_sha", "github_sha",
    "run_id", "run_attempt", "shard_index", "shard_count", "pytest_session_exit", "boolean-index",
    "collection-drift", "duplicate-node", "empty-collection", "invalid-path", "selected-missing",
    "selected-duplicate", "missing-setup", "missing-call", "missing-teardown", "duplicate-phase",
    "illegal-phase", "failed-phase", "illegal-outcome", "unselected-node", "missing-finish",
    "duplicate-finish", "setup-skipped-with-call",
])
def test_verifier_rejects_incomplete_or_mismatched_real_evidence(
    tmp_path, complete_evidence, mutation,
):
    env, original = complete_evidence
    docs = deepcopy(original)
    doc = docs[0]
    if mutation in ("version", "shard_index", "shard_count", "pytest_session_exit"):
        doc[mutation] += 1
    elif mutation in ("checkout_sha", "github_sha", "run_id", "run_attempt"):
        doc[mutation] = "wrong"
    elif mutation == "boolean-index":
        doc["shard_index"] = False
    elif mutation == "collection-drift":
        docs[1]["full_collection"].pop()
    elif mutation == "duplicate-node":
        doc["full_collection"].append(doc["full_collection"][0])
    elif mutation == "empty-collection":
        doc["full_collection"] = []
    elif mutation == "invalid-path":
        doc["full_collection"][0][1] = "../outside.py"
    elif mutation == "selected-missing":
        doc["selected"].pop()
    elif mutation == "selected-duplicate":
        doc["selected"].append(doc["selected"][0])
    elif mutation.startswith("missing-") and mutation.removeprefix("missing-") in (
        "setup", "call", "teardown",
    ):
        doc["reports"] = [row for row in doc["reports"]
                          if row["when"] != mutation.removeprefix("missing-")]
    elif mutation == "duplicate-phase":
        doc["reports"].append(doc["reports"][0])
    elif mutation == "illegal-phase":
        doc["reports"][0]["when"] = "unknown"
    elif mutation in ("failed-phase", "illegal-outcome"):
        doc["reports"][0]["outcome"] = "failed" if mutation == "failed-phase" else "unknown"
    elif mutation == "unselected-node":
        doc["reports"][0]["nodeid"] = docs[1]["selected"][0]
    elif mutation == "missing-finish":
        doc["finished"].pop()
    elif mutation == "duplicate-finish":
        doc["finished"].append(doc["finished"][0])
    elif mutation == "setup-skipped-with-call":
        doc["reports"][0]["outcome"] = "skipped"
    evidence = tmp_path / "reports"
    for index, content in enumerate(docs):
        directory = evidence / str(index)
        directory.mkdir(parents=True)
        (directory / "shard.json").write_text(json.dumps(content), encoding="utf-8")
    if mutation == "missing-artifact":
        (evidence / "1" / "shard.json").unlink()
    elif mutation == "extra-artifact":
        (evidence / "unexpected.json").write_text("{}", encoding="utf-8")
    elif mutation == "bad-json":
        (evidence / "0" / "shard.json").write_text("{", encoding="utf-8")
    result = verify(evidence, env)
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("needs", ["failure", "cancelled", "skipped"])
def test_green_json_never_overrides_failed_cancelled_or_skipped_upstream(
    complete_evidence, tmp_path, needs,
):
    env, docs = complete_evidence
    for index, doc in enumerate(docs):
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory / "shard.json").write_text(json.dumps(doc), encoding="utf-8")
    assert verify(tmp_path, env, needs=needs).returncode == 1


def test_late_real_watchdog_close_failure_keeps_session_zero_but_wrapper_and_gate_red(tmp_path):
    # Inject a real blocked diagnostic writer only after sessionfinish wrote its report.
    hook = '''import os
import threading

def pytest_unconfigure(config):
    plugin = next(p for p in config.pluginmanager.get_plugins()
                  if type(p).__name__ == "StackWatchdog")
    reader, writer = os.pipe()
    os.set_blocking(writer, False)
    while True:
        try:
            os.write(writer, b"x" * 4096)
        except BlockingIOError:
            break
    os.set_blocking(writer, True)
    os.dup2(writer, plugin.fd)
    os.close(writer)
    entered = threading.Event()
    def diagnose():
        entered.set()
        plugin.snapshot()
    thread = threading.Thread(target=diagnose, daemon=True)
    plugin.active[thread] = threading.Event()
    thread.start()
    assert entered.wait(5)
    # Keep the full pipe unread until process exit, as a stuck diagnostics sink would.
    plugin.probe_reader = reader
'''
    evidence, env, results, docs = shards(tmp_path, {**FILES, "conftest.py": hook})
    assert [r.returncode for r in results] == [3, 3], [(r.stdout, r.stderr) for r in results]
    assert [doc["pytest_session_exit"] for doc in docs] == [0, 0]
    assert verify(evidence, env, needs="failure").returncode == 1
