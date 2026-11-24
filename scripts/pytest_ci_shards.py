"""Two Windows CI shards: preserve file order and verify complete pytest evidence.

This is development-only CI evidence, not a product contract.
The upstream job result also matters: the stack wrapper can fail after pytest exits.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

# Half-second scheduling units. The latest recalibration uses complete observed
# pytest status intervals from Windows run 45345940207 and completed-only file
# intervals from 45350527384. The latter canceled during test_w15_safe_gc.py and
# never started test_w4_streaming.py or test_windows_145_147_regressions.py; none
# of those three contributes a duration from the canceled run. Earlier complete
# runs supply the remaining hints. Add one unit per measured file for setup,
# teardown, and collection overhead. Unmeasured files use ~0.5s per node as a
# rough proxy. Canceled runs and missing artifacts remain failed evidence.
# These fixed hints are not acceptance limits or runner guarantees.
_FILE_WEIGHTS = {
    "tests/test_acceptance.py": 20,
    "tests/test_approval_reject.py": 12,
    "tests/test_assemble_release.py": 10,
    "tests/test_authority_block.py": 9,
    "tests/test_backend_contract.py": 68,
    "tests/test_backup.py": 20,
    "tests/test_backup_materials.py": 108,
    "tests/test_baseline_evidence.py": 44,
    "tests/test_browser_form_dispatch.py": 33,
    "tests/test_browser_forms.py": 2,
    "tests/test_browser_navigation.py": 1,
    "tests/test_browser_post_transport.py": 10,
    "tests/test_budget_ledger.py": 36,
    "tests/test_budget_review.py": 66,
    "tests/test_c1_c2_c5_schemas.py": 11,
    "tests/test_cache_metrics.py": 56,
    "tests/test_canonical.py": 2,
    "tests/test_capability.py": 17,
    "tests/test_ci_workflow.py": 3,
    "tests/test_content_read.py": 6,
    "tests/test_content_refs.py": 5,
    "tests/test_context.py": 10,
    "tests/test_control.py": 46,
    "tests/test_control_settings.py": 11,
    "tests/test_conversation.py": 33,
    "tests/test_cost_line.py": 14,
    "tests/test_crash_consistency.py": 20,
    "tests/test_credentials.py": 3,
    "tests/test_daemon.py": 72,
    "tests/test_data_admission.py": 30,
    "tests/test_data_binding.py": 142,
    "tests/test_data_cli.py": 81,
    "tests/test_data_migration_cli.py": 75,
    "tests/test_data_state.py": 2,
    "tests/test_debug_regressions.py": 18,
    "tests/test_diagnostics_and_sbom.py": 3,
    "tests/test_dispatch.py": 28,
    "tests/test_embedding_provider.py": 19,
    "tests/test_eval_followup.py": 9,
    "tests/test_eval_four_dimensions.py": 118,
    "tests/test_eval_harness.py": 143,
    "tests/test_eval_negative_controls.py": 8,
    "tests/test_eval_real_tasks.py": 396,
    "tests/test_eval_report.py": 2,
    "tests/test_eval_scoped_checks.py": 38,
    "tests/test_eval_separation.py": 182,
    "tests/test_event_schema.py": 29,
    "tests/test_events.py": 18,
    "tests/test_evidence_keys.py": 42,
    "tests/test_exec.py": 17,
    "tests/test_exec_backends.py": 8,
    "tests/test_exec_recovery.py": 30,
    "tests/test_execpolicy.py": 2,
    "tests/test_failclosed.py": 7,
    "tests/test_fsutil.py": 2,
    "tests/test_g1_admission.py": 8,
    "tests/test_g1d_resource_kinds.py": 2,
    "tests/test_g2_delegation_contract.py": 4,
    "tests/test_g2_delegation_control.py": 54,
    "tests/test_g2_delegation_exec.py": 24,
    "tests/test_g2_delegation_fence.py": 35,
    "tests/test_g2_delegation_recovery.py": 4,
    "tests/test_g2_delegation_replay.py": 11,
    "tests/test_g2_delegation_spawn.py": 17,
    "tests/test_g2_delegation_write.py": 18,
    "tests/test_host.py": 43,
    "tests/test_host_cleanup.py": 69,
    "tests/test_host_memory.py": 48,
    "tests/test_integration_m5.py": 68,
    "tests/test_ipc.py": 12,
    "tests/test_ipc_send_close.py": 2,
    "tests/test_isolation.py": 21,
    "tests/test_isolation_gate.py": 9,
    "tests/test_job_claims.py": 37,
    "tests/test_leases.py": 11,
    "tests/test_low_disk_admission.py": 14,
    "tests/test_low_disk_entrypoints.py": 22,
    "tests/test_low_disk_execution.py": 140,
    "tests/test_low_disk_run_creation.py": 51,
    "tests/test_m2_acceptance.py": 114,
    "tests/test_macos_outer_seal.py": 2,
    "tests/test_maintenance.py": 17,
    "tests/test_mcp_cli.py": 4,
    "tests/test_mcp_client.py": 8,
    "tests/test_mcp_content_ref.py": 37,
    "tests/test_mcp_crash.py": 49,
    "tests/test_mcp_dispatch.py": 19,
    "tests/test_mcp_http.py": 36,
    "tests/test_mcp_object_block.py": 39,
    "tests/test_mcp_registry.py": 2,
    "tests/test_mcp_transport_timeout.py": 16,
    "tests/test_measure_task_delivery.py": 9,
    "tests/test_memory_conflict.py": 3,
    "tests/test_memory_dispatch.py": 95,
    "tests/test_memory_distill.py": 12,
    "tests/test_memory_loop.py": 48,
    "tests/test_memory_outbound_gate.py": 25,
    "tests/test_memory_rank.py": 5,
    "tests/test_memory_store.py": 28,
    "tests/test_memory_vectors.py": 11,
    "tests/test_metrics.py": 11,
    "tests/test_migration.py": 18,
    "tests/test_model_contract.py": 5,
    "tests/test_model_routing.py": 3,
    "tests/test_model_usage_v2.py": 18,
    "tests/test_net_resilience.py": 21,
    "tests/test_network_fetch.py": 54,
    "tests/test_network_research_eval.py": 18,
    "tests/test_network_search.py": 4,
    "tests/test_outbound_allow.py": 23,
    "tests/test_package_app.py": 2,
    "tests/test_permissions.py": 8,
    "tests/test_powercut_image.py": 2,
    "tests/test_pricing.py": 12,
    "tests/test_prompt_cache.py": 6,
    "tests/test_public_network_boundaries.py": 10,
    "tests/test_pytest_ci_shards.py": 166,
    "tests/test_pytest_with_stacks.py": 37,
    "tests/test_receipts.py": 10,
    "tests/test_recovery.py": 241,
    "tests/test_recovery_atomicity.py": 14,
    "tests/test_release_build.py": 32,
    "tests/test_release_draft.py": 18,
    "tests/test_release_install.py": 2,
    "tests/test_release_manifest.py": 2,
    "tests/test_release_smoke.py": 11,
    "tests/test_release_verify.py": 49,
    "tests/test_release_version_identity.py": 1,
    "tests/test_release_workflow.py": 15,
    "tests/test_resources.py": 2,
    "tests/test_responses_conformance.py": 3,
    "tests/test_responses_contract.py": 2,
    "tests/test_responses_http.py": 4,
    "tests/test_responses_integration.py": 34,
    "tests/test_responses_model.py": 2,
    "tests/test_responses_preflight.py": 6,
    "tests/test_responses_review.py": 9,
    "tests/test_responses_review_followup.py": 28,
    "tests/test_responses_settings.py": 2,
    "tests/test_responses_transport.py": 44,
    "tests/test_responses_usage.py": 16,
    "tests/test_restore_b_execution.py": 26,
    "tests/test_restore_bc_review_regressions.py": 26,
    "tests/test_restore_c_release.py": 2,
    "tests/test_restore_contract.py": 4,
    "tests/test_restore_durability.py": 14,
    "tests/test_restore_eventlog_access.py": 30,
    "tests/test_restore_platform_access.py": 3,
    "tests/test_restore_registry.py": 23,
    "tests/test_round2_auth_execution.py": 42,
    "tests/test_round2_auth_memory.py": 24,
    "tests/test_round2_daemon_budget.py": 12,
    "tests/test_round2_data_integrity.py": 83,
    "tests/test_round2_inputs_ipc.py": 15,
    "tests/test_round2_inputs_model_mcp.py": 53,
    "tests/test_round2_inputs_net.py": 11,
    "tests/test_round2_summary_provenance.py": 20,
    "tests/test_rule_ids.py": 8,
    "tests/test_run_command.py": 19,
    "tests/test_run_control.py": 58,
    "tests/test_sandbox_backend.py": 2,
    "tests/test_schedule_webhook.py": 8,
    "tests/test_scheduler.py": 43,
    "tests/test_schemas.py": 2,
    "tests/test_slo_and_soak.py": 131,
    "tests/test_slo_state_pressure.py": 19,
    "tests/test_slow_subscriber.py": 64,
    "tests/test_snapshot.py": 11,
    "tests/test_soak.py": 1,
    "tests/test_summaries.py": 6,
    "tests/test_summary_cache_telemetry.py": 70,
    "tests/test_summary_context.py": 2,
    "tests/test_summary_loop.py": 38,
    "tests/test_summary_replay.py": 8,
    "tests/test_takeover.py": 2,
    "tests/test_task_attachments.py": 2,
    "tests/test_task_files.py": 6,
    "tests/test_tasks.py": 12,
    "tests/test_transactions.py": 4,
    "tests/test_undo_journal.py": 24,
    "tests/test_undo_recovery.py": 26,
    "tests/test_usage.py": 5,
    "tests/test_veto_scope.py": 6,
    "tests/test_w14_os_sandboxes.py": 2,
    "tests/test_w15_safe_gc.py": 94,
    "tests/test_w4_streaming.py": 6,
    "tests/test_w9_network_depth.py": 46,
    "tests/test_windows_145_147_regressions.py": 14,
    "tests/test_windows_compat.py": 16,
    "tests/test_workflow.py": 56,
    "tests/test_workflow_cli.py": 4,
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def partition(full, count):
    """Balance estimated work by whole file; retain original collection order."""
    require(type(count) is int and count == 2, "exactly two shards are required")
    require(isinstance(full, list) and full, "empty or invalid full collection")
    for pair in full:
        require(isinstance(pair, list) and len(pair) == 2
                and all(isinstance(value, str) and value for value in pair),
                "invalid collection entry")
        node, file = pair
        path = PurePosixPath(file)
        require(not path.is_absolute() and ".." not in path.parts
                and path.as_posix() == file and "\\" not in file
                and node.split("::", 1)[0] == file, "invalid collection path")
    require(len({node for node, _ in full}) == len(full), "duplicate node ID")
    sizes, owners, totals = Counter(file for _, file in full), {}, [0] * count
    weights = {file: _FILE_WEIGHTS.get(file, size) for file, size in sizes.items()}
    for file in sorted(weights, key=lambda file: (-weights[file], file)):
        index = min(range(count), key=lambda index: (totals[index], index))
        owners[file] = index
        totals[index] += weights[file]
    groups = [[node for node, file in full if owners[file] == index] for index in range(count)]
    require(all(groups), "empty shard")
    return groups


def pytest_addoption(parser):
    group = parser.getgroup("ci-shards")
    group.addoption("--ci-shard-index", type=int)
    group.addoption("--ci-shard-count", type=int)
    group.addoption("--ci-shard-report", type=Path)


def pytest_configure(config):
    index, count, report = (config.getoption(f"ci_shard_{key}")
                            for key in ("index", "count", "report"))
    if index is None and count is None and report is None:
        return
    try:
        require(count == 2 and index in (0, 1) and report is not None and report.is_absolute(),
                "use index 0/1, count 2 and an absolute report path")
        selectors = ("keyword", "markexpr", "ignore", "ignore_glob", "deselect", "lf",
                     "failedfirst", "newfirst", "stepwise", "stepwise_skip", "stepwise_reset",
                     "maxfail", "collectonly", "pyargs")
        require(config.args_source.name != "ARGS"
                and not any(config.getoption(name, default=None) for name in selectors),
                "CI shards require full collection without selectors or early stopping")
        checkout = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.rootpath,
                                  check=True, capture_output=True, text=True).stdout.strip()
        identity = {"checkout_sha": checkout, "github_sha": os.environ.get("GITHUB_SHA"),
                    "run_id": os.environ.get("GITHUB_RUN_ID"),
                    "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT")}
        require(checkout == identity["github_sha"] and len(checkout) == 40
                and all(identity[key] and identity[key].isdigit()
                        for key in ("run_id", "run_attempt")), "missing or mismatched CI identity")
        config.pluginmanager.register(Shard(index, count, report, identity), "ci-shard-recorder")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise pytest.UsageError(f"CI sharding: {exc}") from exc


class Shard:
    def __init__(self, index, count, report, identity):
        self.report = report
        self.data = {"version": 1, **identity, "shard_index": index, "shard_count": count,
                     "full_collection": [], "selected": [], "reports": [], "finished": []}

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        def snapshot():
            return [[item.nodeid, item.path.relative_to(config.rootpath).as_posix()]
                    for item in items]

        full = snapshot()
        self.data["full_collection"] = full
        yield
        if session.testsfailed:
            # Preserve pytest's collection-error exit status (normally 2); the
            # evidence is intentionally incomplete and the verifier rejects it.
            self.data["selected"] = []
            return
        require(snapshot() == full, "another plugin filtered or reordered the full collection")
        selected = partition(full, self.data["shard_count"])[self.data["shard_index"]]
        self.data["selected"] = selected
        selected_set = set(selected)
        config.hook.pytest_deselected(items=[item for item in items
                                            if item.nodeid not in selected_set])
        items[:] = [item for item in items if item.nodeid in selected_set]

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_finish(self, session):
        yield
        if session.testsfailed or not self.data["selected"]:
            # Collection can be interrupted before modifyitems runs at all.
            return
        require([item.nodeid for item in session.items] == self.data["selected"],
                "final collection differs from selected shard")

    def pytest_runtest_logreport(self, report):
        row = {"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome}
        if hasattr(report, "wasxfail"):
            row["wasxfail"] = str(report.wasxfail)
        self.data["reports"].append(row)

    def pytest_runtest_logfinish(self, nodeid):
        self.data["finished"].append(nodeid)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session):
        yield
        self.data["pytest_session_exit"] = int(session.exitstatus)
        self.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=True) + "\n", encoding="utf-8")
        temporary.replace(self.report)


def verify(args):
    require(args.needs_result == "success", "Windows shard jobs did not all succeed")
    require(args.count == 2 and args.indices == [0, 1], "expected shard indices 0 and 1")
    paths = [args.reports_root / str(index) / "shard.json" for index in args.indices]
    require({p for p in args.reports_root.rglob("*") if p.is_file()} == set(paths),
            "missing or extra shard artifacts")
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    full = documents[0]["full_collection"]
    groups = partition(full, args.count)
    for index, doc in enumerate(documents):
        require(isinstance(doc, dict) and type(doc["version"]) is int and doc["version"] == 1,
                "unsupported shard evidence version")
        expected = {"checkout_sha": args.sha, "github_sha": args.sha,
                    "run_id": args.run_id, "run_attempt": args.run_attempt,
                    "shard_index": index, "shard_count": args.count, "pytest_session_exit": 0}
        require(all(type(doc[key]) is type(value) and doc[key] == value
                    for key, value in expected.items()), "shard identity or pytest exit mismatch")
        require(doc["full_collection"] == full, "full collection differs between runners")
        selected = groups[index]
        require(doc["selected"] == selected and doc["finished"] == selected,
                "selected or finished nodes differ from the expected shard")
        require(isinstance(doc["reports"], list), "invalid phase reports")
        phases = {node: {} for node in selected}
        for row in doc["reports"]:
            require(isinstance(row, dict) and row["nodeid"] in phases
                    and row["when"] in ("setup", "call", "teardown")
                    and row["outcome"] in ("passed", "skipped"), "failed or invalid phase report")
            node_phases = phases[row["nodeid"]]
            require(row["when"] not in node_phases, "duplicate phase report")
            node_phases[row["when"]] = row["outcome"]
        for node_phases in phases.values():
            expected_phases = ({"setup", "teardown"} if node_phases.get("setup") == "skipped"
                               else {"setup", "call", "teardown"})
            require(set(node_phases) == expected_phases, "missing or unexpected phase report")
    print(f"Verified {len(full)} collected nodes across two Windows shards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("--reports-root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--indices", type=int, nargs="+", required=True)
    parser.add_argument("--needs-result", required=True)
    try:
        verify(parser.parse_args())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"CI shard verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
