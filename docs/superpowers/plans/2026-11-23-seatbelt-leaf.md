# Seatbelt Leaf Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan.

**Goal:** Add an opt-in macOS single-process backend that admits real commands and MCP without allowing descendants.

**Architecture:** Reuse the Seatbelt policy generator, add a fixed no-fork backend, and carry its exact profile through `PreparedExecution`. Extend the real process probe to distinguish kernel-denied child creation with verified root termination from failed probes.

**Tech Stack:** Python 3.12 standard library, Darwin Seatbelt, existing pytest fixtures.

**Spec:** [Seatbelt 单进程执行设计](../specs/2026-11-23-seatbelt-leaf-design.md); [主循环规格 §10](../../SPEC-AGENT-LOOP-2026-11-22.md).

## Global Constraints

- No new runtime dependencies, event schemas, permission sources, or CI weakening.
- Existing `local`, `seatbelt`, `auto` selection and detached-descendant rejection stay intact.
- Unsupported platforms reject before starting the requested payload.
- Do not merge main, publish, or change the accepted #225/#227 candidates.
- Record evidence under ignored `.omc/artifacts/native-process-isolation/`.

## Review Focus

- A generic launch failure or absent descendant marker cannot prove enforcement.
- Probe and real command/MCP must use the same no-fork profile.
- A successful root exit cannot erase an unknown MCP call.
- Recovery must not kill a reused PID or assume a missing identity means death.
- Normal programs that need subprocesses fail explicitly with no backend fallback.

## Task 1: Implement and verify the restricted backend

**Files:** `src/tianshu/exec_seatbelt.py`, `src/tianshu/exec_backends.py`,
`src/tianshu/isolation.py`, `src/tianshu/execution_admission.py`,
`tests/test_seatbelt_leaf.py`, affected native-probe version tests and boundary docs.

**Interfaces:** `exec_backend("seatbelt_leaf")`, the existing `spawn` signature,
`PreparedExecution.admit/run/open_stdio/stop`, and `McpClients.for_run`.

- [x] Write real native tests: forbidden child creation, complete command and MCP admission, timeout/stop, crash recovery, profile drift, and platform rejection.
- [x] Observe the new backend tests fail before product changes.
- [x] Add `SeatbeltLeafExecBackend` and the fixed SBPL option; register only on supported Darwin.
- [x] Extend the process probe's denied-creation branch with positive timeout/cleanup evidence and invalidate its prior version.
- [x] Route the exact same native profile through `PreparedExecution`; keep legacy detached rejection.
- [x] Run targeted tests and a mutation that restores `process-fork`; preserve the genuine failures and restore original bytes.
- [x] Run affected isolation, command, MCP, Goal and control regressions.
- [x] Obtain independent second-reader probes for the frozen code/test tree.
- [x] Run the frozen full Python suite, lock/Ruff/diff; update capability boundaries accurately.
- [ ] Commit and open a stacked draft PR; freeze it, mark ready, and accept its own full CI with original Windows reports.

The checklist records the pre-commit local freeze. The final PR records the
subsequent commit, publication and hosted CI result.

Windows and general descendant-supporting execution remain open under #224.

## Task 2: Repair the candidate's Windows scheduling imbalance

The first complete hosted run `45134199845` was cancelled at the unchanged
25-minute Windows shard limit. Both pytest reports contain session exit 0,
but shard 0's cancelled job and the failed required summary remain RED.
Keep every original log and report; do not rerun the same candidate for a green.

**Files:** `scripts/pytest_ci_shards.py`, `tests/test_pytest_ci_shards.py`,
`tests/fixtures/windows_ci_45134199845_costs.json`, CI boundary documentation.

**Interfaces:** Preserve `partition(full, 2)`, whole-file ownership, original
collection order, both independent runners and the strict verifier.
Only fixed scheduling estimates change; product source and schemas do not.

- [x] Store complete-file Windows observations with raw-log hashes and per-file collection sizes.
- [x] Add `test_windows_weights_balance_complete_leaf_candidate_observations`;
  actual measured file costs must differ by less than 60 seconds after scheduling.
- [x] Observe that test fail with the previous weights.
- [x] For every complete file, retain the higher of its prior estimate and new
  observation rounded up in half-second units. Latest-only replacement failed two
  historical cohorts; keep those assertions. Do not use truncated lower bounds
  or promise future runner timing.
- [x] Run the new regression, the complete sharding/workflow/stack-wrapper domain,
  and a real mutation restoring old weights; verify byte-identical restoration.
- [x] Freeze the new non-document tree; run bare full pytest, lock/Ruff/diff and
  obtain an independent incremental review, retaining the existing product review scope.
- [ ] Submit one repair commit, freeze the draft PR, mark ready, and accept a new
  complete hosted run plus its original Windows reports. Preserve the cancelled run.

Repair commit `3eeb577` was submitted and locally verified, but its complete
run `45142192772` cancelled both Windows jobs at 25 minutes. This last item
therefore remains incomplete; Task 3 changes execution capacity without weakening gates.

## Task 3: Execute isolated worker pairs within the two Windows runners

**Design:** [Windows worker 对](../specs/2026-11-23-windows-ci-worker-pairs-design.md).
Run `45142192772` cancelled both Windows jobs; keep its incomplete/missing reports
as failures. No product or authority change; no test removal or timeout increase.

**Files:** `scripts/pytest_with_stacks.py`, new `scripts/pytest_ci_pair.py`,
`scripts/pytest_ci_shards.py`, CI workflow and their tests/documentation.

**Interfaces:** Explicit `--ci-pair-index=0/1 --ci-pair-root=<absolute-path>`
on the original wrapper; two independent child wrappers each use count4 and a
private basetemp/report. The verifier retains strict count2 and adds strict count4.

- [x] Obtain bounded independent design review; resolve direct blockers.
- [x] Add actual wrapper-pair success and count4 coverage tests; observe RED.
- [x] Add the explicit supervisor branch and count4 closed-set support.
- [x] Exercise two parent processes/four workers with a real TCP barrier and
  verify private runtime roots plus exact original reports.
- [x] Prove launch/worker failure and interruption cannot return green; verify
  owned worker roots are reaped and previous diagnostic negatives still bite.
- [x] Update workflow downloads/upload paths and exact strict verification;
  prove missing/extra/corrupt reports and failed/cancelled jobs reject for count4.
- [x] Reproduce the v5 independent review's first-root cleanup error with real
  pipe readers; make each root independent, preserve the first error, bound all
  cleanup waits, and explicitly report unconfirmed cleanup. Reaped roots receive
  no signals; mutations must detect swallowed errors and unbounded kill waits.
- [x] Force both LF and CRLF through real pipe readers; observe the LF-only
  handshake fail for CRLF, then accept the two exact ready frames. The interrupted
  v6 full is retained as exit2; test-only v7 requires its own frozen full suite.
- [x] Freeze, run affected regressions and bare full pytest/lock/Ruff/diff, bind
  independent incremental review, then commit one repair batch.
- [ ] Push draft, wait for the synchronize run to complete before ready, and
  accept a new complete run with exact head/base/merge and four original reports.

The v7 local and independent-review receipts bind non-document manifest SHA256
`d1922a038d823360ac90fe182fe01db6ea81349cc35100f5ee41030793eb3bd8`.
The fresh bare full suite exited 0; v6's interrupted exit2 remains rejected.
Hosted acceptance is separate and must bind the repair commit.

The v7 repair commit `416fd45` reached a new complete run `45160677871`,
but both Windows pairs failed during collection: pytest regrouped the new
module-scoped count2/count4 fixture parameters. All four original reports
record exit3 with empty selections. Keep the failure; throughput remains unproven.

## Task 4: Preserve native collection order in CI evidence fixtures

- [x] Reproduce the hosted collection failure through the original wrapper pairs
  while reading the actual CI test module, not a handwritten fixture substitute.
- [x] Keep count2/count4 parameters at function scope and cache real evidence
  cohorts in an unparameterized module fixture; preserve the strict order guard.
- [x] Verify the probe collects all four workers but its intentionally unexecuted
  reports remain rejected for missing finished nodes.
- [x] Verify the complete project collection, mutation negative and affected domain.
- [x] Freeze the test-only delta; run fresh bare full pytest/lock/Ruff/diff and
  bind a limited independent second-reader review before committing.
- [ ] Push draft, drain synchronize, ready and accept a new complete hosted run.

The v8 local and independent incremental receipts passed for non-document SHA256
`a4e482f6305d4ebd5d4e6682bcdba995608c8586cc5bffa5454a31a74dc86b23`.
The complete v7 run also timed out macOS Python at the unchanged 25-minute job
limit; macOS required therefore failed. This is separate from fixture collection.
PR #228 is draft again. Keep v8 local evidence within its scope.

## Task 5: Keep complete macOS Python coverage within separate native shards

**Design:** [macOS CI 分片](../specs/2026-11-23-macos-ci-shards-design.md).
Reuse count2 on two independent native runners; direct pytest, no new supervisor.

**Files:** `.github/workflows/ci.yml`, `tests/test_ci_workflow.py`,
new `tests/test_macos_ci_shards.py`, generic verifier wording and relevant docs.

- [x] First write workflow tests requiring two exact-SHA count2 shards, private
  runtime/report paths, always uploads/downloads and strict required verification;
  run against the old workflow and retain RED.
- [x] Add the two-runner matrix and use the existing plugin directly with
  `-o addopts="" -q -rs`; keep Windows count4 and diagnostic semantics unchanged.
- [x] Execute commands extracted from the actual workflow against independent
  real Git checkouts; retain original reports and reject missing/extra/corrupt
  identity, incomplete execution, failure and cancellation.
- [x] Run the affected CI domain and mutation negatives; freeze, run bare full
  pytest/lock/Ruff/diff and obtain a limited independent incremental review.
- [ ] Publish one repair batch as draft, drain synchronize, ready and accept the
  final exact head/base/merge run with both macOS and Windows original evidence.

The v9 local and bounded independent-review receipts passed for non-document
SHA256 `1e6aefab77348d1abef038418ca9048e2edbfed566c7793478e3916667f11240`.
Its own bare full-suite log SHA256 is
`079319f9dd9b3945b80c0e05a87c703e16333f8767e72b2c72c13f2cbe6778df`.
Independent original-report negatives and the unchanged Windows workflow were
replayed by the controller. Hosted throughput and final acceptance remain a
separate gate; neither the local suite nor draft skips can close that item.

The v9 commit `a4e48a0` reached complete hosted run `45170896526`, which
failed assertions in two Windows workers and macOS shard 1. All selections
finished; this was not collection failure or timeout. Keep all original reports
and logs. PR #228 is draft again; do not finalize v9 or rerun it for a green.

## Task 6: Correct CI observation assumptions without weakening acceptance

**Files:** `scripts/measure_slo.py`, `scripts/run_soak.py`,
`tests/test_pytest_ci_pairs.py`, `tests/test_slo_process_identity.py`,
`tests/test_slo_state_pressure.py`, `tests/test_net_resilience.py`, boundary docs.

**Interfaces:** Keep product `src`, schemas, strict count2/count4 verification,
timeouts, original diagnostic sample predicates and zero-orphan threshold.
This increment fixes development measurement and test evidence only.

- [x] Reproduce the invalid PPID assumption with a real intermediate launcher;
  bind four simultaneous workers to actual checkouts, plugin indices and exact
  private roots. Reject a coordinated swap of whole pair indices and directories.
- [x] Reproduce false orphan attribution using a real live process and a different
  recorded birth identity. Preserve every observed identity for a reused PID;
  missing initial/final identity remains conservative.
- [x] Reproduce the missed pressure window by waiting for the same subscriber's
  real watchdog drop before the extra large write. Diagnostic state calls use the
  already replaying backlog; ordinary SLO workload and sample rejection remain.
- [x] Reproduce the unconditional retry assertion with a real failed connection
  and an injected exhausted clock; assert exactly two or one attempts according
  to the remaining shared deadline. Do not claim the hosted connection duration
  or actual PID reuse was proved.
- [x] Run mutation negatives, affected regressions and complete project collection;
  freeze the new tree and run bare full pytest/lock/Ruff/diff.
- [x] Bind bounded independent incremental probes, verify the original artifacts,
  and replay them in a fresh private copy; keep the final read-back document-only.
- [ ] Push draft, drain synchronize, then ready; accept its new complete hosted
  run with exact head/base/merge, original ZIP digests and all six strict reports.

The v10b local gates and bounded independent review bind non-document SHA256
`667dd6561a44065091e1cded195428891d2a0ec8d6b38c80c76953b351645902`.
Its own bare full-suite log SHA256 is
`c1b23b5feb998f6c6d6dcf281b48111fd07a4ff84ecb33ff5ba90277c537728f`. The earlier v10 full exit0/index-diff exit2
remains rejected; v10b removes only the extra final LF and has its own full gates.
Final document-only read-back must preserve this non-document tree. The final PR
records the subsequent repair commit and exact hosted head/base/merge acceptance;
this local checklist does not claim that gate passed.

v9 local and independent PASS remain bound to v9.
Windows native sandbox, general descendant supervision and remote MCP isolation
remain unimplemented under open #224. Do not merge, release, alter #225/#227 or
start another major item.
