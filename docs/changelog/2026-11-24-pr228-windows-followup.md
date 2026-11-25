# PR #228 Windows process-ownership follow-up

## Original problem

Hosted run `45336814667`, head `620ede5349f592e64b7b2f1bebb43a63bbfec9aa`, failed Windows shard 0 rather than timing out. The three assigned failures were:

- `tests/test_eval_scoped_checks.py::test_p1_old_global_checks_would_reject_that_shape`: the negative control selected an observation without first proving the real two-effect shape. The failed log retained no traceback for this assertion.
- `tests/test_host_supervisor_exit.py::test_shutdown_authentication_stall_after_readiness_is_bounded`: Windows raised `AssertionError: request never reached authentication` because cold helper startup/import/IPC competed with the measured `0.3s` graceful shutdown budget.
- `tests/test_host_supervisor_exit.py::test_post_readiness_bridge_bind_failure_reaps_only_owned_root[True]`: Windows raised `AssertionError: failed Host leaked its Daemon lock`. `run_host()` called `_abort_start()` after bridge binding failed; forced termination bypassed the Daemon's Python `finally`, so normal lock cleanup never ran.

The same three-test baseline passed on macOS arm64. That result was not treated as Windows reproduction or as evidence that the race was absent.

## Root causes and repair

- **Eval negative control could be falsely red.** It now proves that observations exist, `RunResult` exists, `n_applied == 2`, and both `notes.md` and `summary.md` were applied before evaluating the legacy whole-Run checks. Assertion diagnostics retain the complete observation and evaluated results; the legacy rejection criteria are unchanged.
- **Authentication timing did not identify the measured phase.** `ensure()` first completes a real authentication handshake and proves readiness. The test then enables the stall and leaves the accept loop free, so the shutdown helper launched by `stop(timeout=0.3)` is the connection that enters the authentication barrier. Its cold interpreter startup, imports, IPC authentication, graceful wait, escalation, and reaping all remain inside the original bounded stop path; the marker cannot be pre-created by a probe. A first revision that left a probe blocked in authentication was rejected locally because it prevented the helper from ever reaching the server-side barrier.
- **Failed-start cleanup assumed process termination runs Python cleanup.** `_abort_start()` now requests graceful shutdown first, bound to the exact readiness snapshot and same-connection `instance_id`. If that fails, it reaps only its owned `Popen`. After exit is confirmed, it removes only lock bytes exactly equal to the readiness snapshot. A replacement or unreadable lock is never intentionally cleared, and no PID from a replacement lock is signalled.
- **Cleanup failure discarded retry evidence.** Final lock read/compare/unlink failures now retain the dead owned `Popen` and readiness snapshot. State is cleared only after the exact lock is confirmed absent or removed. A retry that observes a replacement drops only the dead owned handle and preserves the replacement.
- **Lock identity changes lacked one cross-process linearization point.** Daemon lock creation, stale takeover, and identity-checked deletion now share the independent `daemon.lock.guard`. It does not reuse the Host metadata guard, because Host can hold that guard while waiting for its owned Daemon to exit. Blocking test reads use bounded queues, and failure paths reap children and join readers.
- **Numeric-PID escalation was not an atomic process identity check.** Forced cleanup now accepts only an owned `Popen`. On POSIX, `waitid(WNOWAIT)` must still observe a running, unreaped child before signalling; on macOS the ctypes `siginfo_t` matches the complete SDK layout. On Windows, `TerminateProcess` uses the retained original process handle and never falls back to a PID, `OpenProcess(PID)`, or `taskkill`. Attached Daemons are never force-signalled. A normal restart finishes retained-process wait and exact lock cleanup before spawning its replacement.
- **Windows tree cleanup is deliberately not claimed.** Replacing the old numeric-PID `taskkill /T` call removes its PID-reuse hazard but also removes that tree-termination behavior. Forced cleanup now confirms only the owned Daemon root; Daemon-created execution descendants may survive. This does not expand the product's isolation promise, and complete Windows process-tree supervision remains open under [#224](https://github.com/lorek666/tianshu-agent/issues/224).

No Daemon schema, authorization boundary, CI workflow, test time limit, or existing assertion was removed or relaxed.

## Validation

On macOS arm64 in `agent-general-purpose-pr228-fix`:

- The pre-change three-test baseline was green locally (`3 passed`); the original managed Windows log supplied the red evidence above.
- Private in-process mutations restored the legacy direct-terminate `_abort_start()` and unguarded `_unlink_lock()`. The new regressions failed deterministically with `graceful shutdown must not force-terminate the owned root` and `['acquired ...'] != ['blocked']`, respectively. The source tree was not modified by either mutation, and the original tests passed again after removing the mutation harness.
- The strict helper-authentication regression passed five consecutive local runs.
- Affected domains (`tests/test_eval_scoped_checks.py`, `tests/test_host_supervisor_exit.py`, `tests/test_daemon.py`, `tests/test_leases.py`, `tests/test_host.py`, `tests/test_integration_m5.py`): `189 passed`.
- `uv lock --check`, `uv run --frozen ruff check .`, and `git diff --check` passed.

The new lifecycle regressions use the real `held_child` subprocess and stdin barriers. They cover graceful success, graceful failure followed by owned-process reaping, initial replacement locks, replacement after the identity check, unreadable locks, unlink failure, and safe retry without sleep-based timing guesses.

The PID-reuse follow-up adds deterministic controls for a child exiting at the wait boundary, an exited-but-unreaped child, a Windows process whose `pid` property must never be read, missing handles, `TerminateProcess` failure without command/PID fallback, attached escalation without any signal, and retained lock cleanup before a normal retry. A local ctypes layout mistake initially caused a native macOS crash; the corrected structure matches the SDK's 104-byte `siginfo_t`, and the real-child probe confirms that `WNOWAIT` observes exit without reaping it.

## Independent Tier 1 review

Final independent review is pending for the integrated code/test tree SHA256 `4de20d3bef737a5a5eb2d576711c0a9a7719d0453149a1e66ec927a206a77959`. Reproduction: list tracked files under `src/` and `tests/` with `git ls-files -z src tests`, sort their repository-relative UTF-8 paths, then update SHA256 in order with `path bytes + NUL + SHA256(complete file bytes)`. Earlier reviews found retry-snapshot loss, a probe/helper barrier defect, initial-replacement retry evidence loss, unbounded child reads, and an ambiguous hash recipe; those findings were repaired and are not the final review binding.

## Full local suite and remaining verification

The final full `uv run --frozen pytest` result is pending and must not be inferred from the focused runs.

Hosted Windows required CI must still verify the new head, including real named-pipe authentication timing, `TerminateProcess` cleanup, both paired workers, strict shard reports, and all required jobs. Local macOS success is not Windows native evidence, and this repair does not claim physical process isolation or user-device validation.
