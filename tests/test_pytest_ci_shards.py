"""Exercise CI sharding through real isolated pytest processes and verifier CLI."""

from __future__ import annotations

import importlib.util
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


def report_path(root, index, _count):
    return root / str(index) / "shard.json"


def shards(tmp_path, files=None, extra=(), extra_env=None, count=2):
    files = FILES if files is None else files
    evidence = tmp_path / "reports"
    results, documents = [], []
    for index in range(count):
        root = tmp_path / f"checkout-{index}"
        env = project(root, files)
        # Both runners check out exactly the same Git object, including commit metadata.
        if index == 0:
            first_env = dict(env)
        else:
            assert env["GITHUB_SHA"] == first_env["GITHUB_SHA"]
        env.update(extra_env or {})
        report = report_path(evidence, index, count)
        args = [sys.executable, str(WRAPPER), "-p", "pytest_ci_shards",
                f"--ci-shard-index={index}", f"--ci-shard-count={count}",
                f"--ci-shard-report={report}", "-o", "addopts=", "-q",
                "--stack-timeout=60", *extra]
        results.append(run(args, root, env))
        documents.append(json.loads(report.read_text()) if report.exists() else None)
    return evidence, first_env, results, documents


def verify(evidence, env, *, needs="success", extra=(), count=2, attempts=None, indices=None):
    indices = range(count) if indices is None else indices
    identity = (["--run-attempts", json.dumps(attempts)] if attempts is not None
                else ["--run-attempt", ATTEMPT])
    return run([sys.executable, str(VERIFIER), "verify", "--reports-root", str(evidence),
                "--sha", env["GITHUB_SHA"], "--run-id", RUN_ID, *identity,
                "--count", str(count), "--indices", *map(str, indices),
                "--needs-result", needs, *extra],
               evidence.parent, env)


def test_verifier_rejects_unsuccessful_upstream_with_platform_neutral_message(tmp_path):
    env = isolated_env()
    env.update(GITHUB_SHA="0" * 40, GITHUB_RUN_ID=RUN_ID)
    result = verify(tmp_path, env, needs="failure")
    assert result.returncode == 1
    assert "shard jobs did not all succeed" in result.stderr
    assert "Windows shard jobs" not in result.stderr


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


def test_expensive_file_is_accounted_for_by_runners_and_verifier(tmp_path):
    heavy = "tests/test_eval_real_tasks.py"
    evidence, env, results, docs = shards(tmp_path, {**FILES, heavy: "def test_case(): pass\n"})
    assert [result.returncode for result in results] == [0, 0]
    assert docs[0]["selected"] == [f"{heavy}::test_case"]
    assert docs[1]["selected"] == [node for node, file in docs[1]["full_collection"]
                                   if file != heavy]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


@pytest.mark.parametrize("count", [2, 4])
def test_slo_runs_first_in_its_shard_without_changing_full_collection(tmp_path, count):
    slo_file = "tests/test_slo_and_soak.py"
    slo_node = f"{slo_file}::test_measure_slo_smoke_and_run_soak_real_subprocesses"
    files = {
        **FILES,
        "tests/test_eval_real_tasks.py": "def test_case(): pass\n",
        "tests/test_recovery.py": "def test_case(): pass\n",
        "tests/test_low_disk_execution.py": "def test_case(): pass\n",
        "tests/test_goal_v2.py": "def test_case(): pass\n",
        "tests/test_m2_acceptance.py": "def test_case(): pass\n",
        slo_file: "def test_other(): pass\n"
                  "def test_measure_slo_smoke_and_run_soak_real_subprocesses(): pass\n",
    }
    evidence, env, results, docs = shards(tmp_path, files, count=count)
    assert [result.returncode for result in results] == [0] * count, [
        (result.stdout, result.stderr) for result in results]
    owner = next(doc for doc in docs if slo_node in doc["selected"])
    spec = importlib.util.spec_from_file_location("ci_shard_contract", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected_groups = module.partition(owner["full_collection"], count)
    assert [set(doc["selected"]) for doc in docs] == [set(group) for group in expected_groups]
    assert owner["selected"][0] == slo_node
    assert len(owner["selected"]) > 1
    assert owner["finished"] == owner["selected"]
    assert owner["full_collection"] == docs[0]["full_collection"]
    assert [node for node, _ in owner["full_collection"]] != owner["selected"]
    accepted = verify(evidence, env, count=count)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    owner_index = docs.index(owner)
    changed = deepcopy(owner)
    changed["selected"][0], changed["selected"][1] = changed["selected"][1], changed["selected"][0]
    changed["finished"] = list(changed["selected"])
    report_path(evidence, owner_index, count).write_text(json.dumps(changed), encoding="utf-8")
    rejected = verify(evidence, env, count=count)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr


def test_windows_file_weights_balance_observed_cost_in_a_reduced_collection(tmp_path):
    # Complete-file log windows from run 45058496468, rounded up to seconds.
    # Earlier observations predate the online-soak/Goal changes and corpus reuse.
    # This is scheduling input, not a claim that the new Windows run passed.
    observed = {
        "tests/test_eval_harness.py": (19, 54),
        "tests/test_eval_real_tasks.py": (40, 175),
        "tests/test_low_disk_execution.py": (97, 62),
        "tests/test_m2_acceptance.py": (45, 66),
        "tests/test_recovery.py": (16, 110),
        "tests/test_slo_and_soak.py": (9, 85),
        "tests/test_w15_safe_gc.py": (43, 63),
    }
    files = {file: "".join(f"def test_probe_{index}(): pass\n" for index in range(count))
             for file, (count, _) in observed.items()}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [result.returncode for result in results] == [0, 0], [
        (result.stdout, result.stderr) for result in results]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    costs = [sum(observed[file][1] for file in {node.split("::", 1)[0]
                                               for node in doc["selected"]}) for doc in docs]
    assert abs(costs[0] - costs[1]) < 60, costs


@pytest.mark.parametrize("fast_file,fast_nodes,fast_seconds", [
    ("tests/test_assemble_release.py", 148, 5),
    ("tests/test_restore_contract.py", 119, 2),
])
def test_windows_weights_correct_fast_parameterized_files_and_slow_small_files(
    tmp_path, fast_file, fast_nodes, fast_seconds,
):
    # Complete-file intervals from run 44927917869, rounded up to whole seconds.
    # Each large, fast file must not outweigh four smaller integration files.
    observed = {
        fast_file: (fast_nodes, fast_seconds),
        "tests/test_data_migration_cli.py": (30, 37),
        "tests/test_host_cleanup.py": (32, 38),
        "tests/test_integration_m5.py": (16, 34),
        "tests/test_slow_subscriber.py": (4, 29),
    }
    files = {file: "".join(f"def test_probe_{index}(): pass\n" for index in range(count))
             for file, (count, _) in observed.items()}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [result.returncode for result in results] == [0, 0], [
        (result.stdout, result.stderr) for result in results]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    costs = [sum(observed[file][1] for file in {node.split("::", 1)[0]
                                               for node in doc["selected"]}) for doc in docs]
    assert abs(costs[0] - costs[1]) < 15, costs


def test_windows_weights_cover_accumulated_medium_file_estimation_errors(tmp_path):
    # Complete-file intervals from runs 44927917869 and 44939541307, rounded up.
    # These files have comparable node counts but very different execution costs.
    observed = {
        "tests/test_data_binding.py": (76, 63),
        "tests/test_data_state.py": (52, 1),
        "tests/test_eval_report.py": (33, 1),
        "tests/test_model_contract.py": (77, 3),
        "tests/test_model_usage_v2.py": (73, 8),
        "tests/test_mcp_crash.py": (24, 50),
        "tests/test_round2_data_integrity.py": (39, 52),
        "tests/test_network_fetch.py": (24, 29),
        "tests/test_host_memory.py": (18, 23),
        "tests/test_isolation.py": (13, 20),
    }
    files = {file: "".join(f"def test_probe_{index}(): pass\n" for index in range(count))
             for file, (count, _) in observed.items()}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [result.returncode for result in results] == [0, 0], [
        (result.stdout, result.stderr) for result in results]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    costs = [sum(observed[file][1] for file in {node.split("::", 1)[0]
                                               for node in doc["selected"]}) for doc in docs]
    assert abs(costs[0] - costs[1]) < 15, costs


def test_windows_weights_include_goal_and_online_soak_costs(tmp_path):
    # Complete file log intervals from run 45058496468. The timeout was cumulative:
    # old weights balanced their own estimates but missed new Goal/online-soak cost.
    # These observations exercise scheduling; they are not native CI acceptance.
    observed = {
        "tests/test_browser_form_dispatch.py": (12, 17.166),
        "tests/test_consent.py": (40, 16.329),
        "tests/test_data_cli.py": (31, 30.715),
        "tests/test_exec.py": (33, 8.283),
        "tests/test_goal_v2.py": (98, 81.111),
        "tests/test_ipc.py": (38, 3.497),
        "tests/test_isolation.py": (13, 19.515),
        "tests/test_memory_distill.py": (22, 5.590),
        "tests/test_migration.py": (14, 6.913),
        "tests/test_recovery.py": (16, 109.594),
        "tests/test_round2_inputs_model_mcp.py": (45, 22.557),
        "tests/test_slo_and_soak.py": (9, 84.437),
    }
    files = {file: "".join(f"def test_probe_{index}(): pass\n" for index in range(count))
             for file, (count, _) in observed.items()}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [result.returncode for result in results] == [0, 0], [
        (result.stdout, result.stderr) for result in results]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    costs = [sum(observed[file][1] for file in {node.split("::", 1)[0]
                                               for node in doc["selected"]}) for doc in docs]
    assert abs(costs[0] - costs[1]) < 15, costs


def test_windows_weights_balance_complete_leaf_candidate_observations():
    # This fixture records complete-file windows from a cancelled native run.
    # Its report exit 0 did not make that job pass. Replay checks scheduling
    # estimates only; the repaired candidate still needs its own full CI.
    observations = json.loads(
        (Path(__file__).parent / "fixtures" / "windows_ci_45134199845_costs.json")
        .read_text(encoding="utf-8")
    )["files"]
    spec = importlib.util.spec_from_file_location("observed_ci_shards", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    full = [[f"{file}::test_observed_{index}", file]
            for file, count, _ in observations for index in range(count)]
    groups = module.partition(full, 2)
    seconds = {file: cost for file, _, cost in observations}
    costs = [sum(seconds[file] for file in {node.split("::", 1)[0] for node in group})
             for group in groups]
    assert abs(costs[0] - costs[1]) < 60, costs


def test_windows_weights_include_measured_summary_and_cache_evaluation_work(tmp_path):
    # Complete-file intervals from Windows run 45050478841, rounded up.
    # Include all test files changed by this cache batch and the affected
    # evaluation assembly, rather than a subset dominated by one large file.
    # That run was RED: these completed intervals are scheduling evidence only.
    observed = {
        "tests/test_cache_metrics.py": (46, 46),
        "tests/test_summary_cache_telemetry.py": (28, 32),
        "tests/test_eval_separation.py": (8, 153),
        "tests/test_eval_real_tasks.py": (37, 172),
        "tests/test_model_usage_v2.py": (73, 8),
        "tests/test_prompt_cache.py": (100, 2),
    }
    files = {file: "".join(f"def test_probe_{index}(): pass\n" for index in range(count))
             for file, (count, _) in observed.items()}
    evidence, env, results, docs = shards(tmp_path, files)
    assert [result.returncode for result in results] == [0, 0], [
        (result.stdout, result.stderr) for result in results]
    accepted = verify(evidence, env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    costs = [sum(observed[file][1] for file in {node.split("::", 1)[0]
                                               for node in doc["selected"]}) for doc in docs]
    assert abs(costs[0] - costs[1]) < 15, costs


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


@pytest.mark.parametrize("count", [1, 3, 5])
def test_runner_and_verifier_reject_other_shard_counts(tmp_path, count):
    files = {f"tests/test_{i}.py": "def test_case(): pass\n" for i in range(count)}
    evidence, env, results, reports = shards(tmp_path, files=files, count=count)
    assert [r.returncode for r in results] == [4] * count
    assert all("use count 2/4" in r.stderr for r in results)
    assert reports == [None] * count
    rejected = verify(evidence, env, count=count)
    assert rejected.returncode == 1
    assert "expected every index of count 2 or 4" in rejected.stderr


@pytest.fixture(scope="module")
def _complete_evidence_cache():
    return {}


@pytest.fixture(params=(2, 4))
def complete_evidence(tmp_path_factory, request, _complete_evidence_cache):
    # Module-scoped parametrization makes pytest regroup collection by count.
    # Keep function-scoped parameters compatible with the strict CI order guard,
    # while caching each real subprocess cohort once per module and worker.
    count = request.param
    if count not in _complete_evidence_cache:
        files = {**FILES, "tests/test_d.py": "def test_d(): pass\n"}
        evidence, env, results, docs = shards(
            tmp_path_factory.mktemp("complete-shards"), files, count=count)
        assert [r.returncode for r in results] == [0] * count
        assert verify(evidence, env, count=count).returncode == 0
        _complete_evidence_cache[count] = env, docs
    return _complete_evidence_cache[count]


@pytest.mark.parametrize("invalid_plan", ["missing", "duplicate", "out-of-range"])
def test_verifier_rejects_incomplete_or_duplicate_shard_indices(
    complete_evidence, tmp_path, invalid_plan,
):
    env, docs = complete_evidence
    count = len(docs)
    for index, doc in enumerate(docs):
        report = report_path(tmp_path, index, count)
        report.parent.mkdir()
        report.write_text(json.dumps(doc), encoding="utf-8")
    indices = list(range(count))
    if invalid_plan == "missing":
        indices.pop()
    elif invalid_plan == "duplicate":
        indices[-1] = 0
    else:
        indices[-1] = count
    rejected = verify(tmp_path, env, count=count, indices=indices)
    assert rejected.returncode == 1
    assert "expected every index of count 2 or 4" in rejected.stderr


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
    count = len(docs)
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
        path = report_path(evidence, index, count)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(content), encoding="utf-8")
    if mutation == "missing-artifact":
        report_path(evidence, 1, count).unlink()
    elif mutation == "extra-artifact":
        (evidence / "unexpected.json").write_text("{}", encoding="utf-8")
    elif mutation == "bad-json":
        report_path(evidence, 0, count).write_text("{", encoding="utf-8")
    result = verify(evidence, env, count=count)
    assert result.returncode == 1, result.stdout + result.stderr


def test_verifier_accepts_explicit_mixed_attempts_after_failed_job_rerun(
    complete_evidence, tmp_path,
):
    env, original = complete_evidence
    docs = deepcopy(original)
    docs[0]["run_attempt"] = "1"
    attempts = {str(index): doc["run_attempt"] for index, doc in enumerate(docs)}
    for index, doc in enumerate(docs):
        path = report_path(tmp_path, index, len(docs))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    accepted = verify(tmp_path, env, count=len(docs), attempts=attempts)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert verify(tmp_path, env, count=len(docs)).returncode == 1


@pytest.mark.parametrize("mutation", ["missing", "extra", "non-string", "empty", "both"])
def test_verifier_rejects_incomplete_or_ambiguous_attempt_maps(
    complete_evidence, tmp_path, mutation,
):
    env, docs = complete_evidence
    for index, doc in enumerate(docs):
        path = report_path(tmp_path, index, len(docs))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    attempts = {str(index): ATTEMPT for index in range(len(docs))}
    extra = []
    if mutation == "missing":
        attempts.pop("0")
    elif mutation == "extra":
        attempts["9"] = ATTEMPT
    elif mutation == "non-string":
        attempts["0"] = int(ATTEMPT)
    elif mutation == "empty":
        attempts = {}
    else:
        extra = ["--run-attempt", ATTEMPT]
    result = run([sys.executable, str(VERIFIER), "verify", "--reports-root", str(tmp_path),
                  "--sha", env["GITHUB_SHA"], "--run-id", RUN_ID,
                  "--run-attempts", json.dumps(attempts), *extra,
                  "--count", str(len(docs)), "--indices", *map(str, range(len(docs))),
                  "--needs-result", "success"], tmp_path.parent, env)
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.parametrize("needs", ["failure", "cancelled", "skipped"])
def test_green_json_never_overrides_failed_cancelled_or_skipped_upstream(
    complete_evidence, tmp_path, needs,
):
    env, docs = complete_evidence
    for index, doc in enumerate(docs):
        path = report_path(tmp_path, index, len(docs))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    assert verify(tmp_path, env, needs=needs, count=len(docs)).returncode == 1


def test_four_valid_reports_moved_between_runner_artifacts_are_rejected(
    complete_evidence, tmp_path,
):
    env, docs = complete_evidence
    if len(docs) != 4:
        return  # The two-shard historical format has only two artifacts.
    for index, doc in enumerate(docs):
        path = report_path(tmp_path, index, 4)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    assert verify(tmp_path, env, count=4).returncode == 0
    misplaced = tmp_path / "0" / "0" / "shard.json"
    misplaced.parent.mkdir()
    report_path(tmp_path, 0, 4).rename(misplaced)
    assert verify(tmp_path, env, count=4).returncode == 1


@pytest.mark.parametrize("count", [0, 1, 3, 5, True, 2.0])
def test_partition_count_remains_a_closed_set(count):
    spec = importlib.util.spec_from_file_location("ci_shards_closed_set", VERIFIER)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    with pytest.raises(ValueError, match="two or four"):
        plugin.partition([[f"tests/test_{i}.py::test_a", f"tests/test_{i}.py"]
                          for i in range(8)], count)


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
