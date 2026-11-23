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
