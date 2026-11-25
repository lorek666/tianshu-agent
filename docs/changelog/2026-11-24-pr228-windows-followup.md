# PR #228 Windows process-ownership follow-up

## Original problem

Hosted run `45336814667`, head `620ede5349f592e64b7b2f1bebb43a63bbfec9aa`, failed Windows shard 0 rather than timing out. The three assigned failures were:

- `tests/test_eval_scoped_checks.py::test_p1_old_global_checks_would_reject_that_shape`: the negative control selected an observation without first proving the real two-effect shape. The failed log retained no traceback for this assertion.
- `tests/test_host_supervisor_exit.py::test_shutdown_authentication_stall_after_readiness_is_bounded`: Windows raised `AssertionError: request never reached authentication` because cold helper startup/import/IPC competed with the measured `0.3s` graceful shutdown budget.
- `tests/test_host_supervisor_exit.py::test_post_readiness_bridge_bind_failure_reaps_only_owned_root[True]`: Windows raised `AssertionError: failed Host leaked its Daemon lock`. `run_host()` called `_abort_start()` after bridge binding failed; forced termination bypassed the Daemon's Python `finally`, so normal lock cleanup never ran.

The same three-test baseline passed on macOS arm64. That result was not treated as Windows reproduction or as evidence that the race was absent.

## Second native Windows run

Hosted run `45368602694`, head `dbb6a43c5de0684368acfd53f1366fd659328fbe`, passed Windows shard 1 and exposed six different shard-0 failures:

- Three SLO state-pressure diagnostics failed before their assertions with `Host 启动进程提前退出 rc=1` and `SloConfigError: state 背压诊断的测试锁根未生效；不启动子进程`. Switching from the Windows venv launcher to the base interpreter replaced the inherited `PYTHONPATH` with only `sys.path`. The `tests/_startup` entry was therefore absent in the real child, so `sitecustomize.py` never installed the test maintenance-lock root. The diagnostic correctly refused to run with an unisolated lock root.
- Three Host lifecycle tests reached their test-body assertions but failed during `TemporaryDirectory` cleanup with `PermissionError: [WinError 32]` on `logs/daemon.log`. The cleanup helper treated delivery of a graceful shutdown request as proof of process exit. The Daemon could still hold the inherited log handle briefly, so deleting the temporary directory raced real process teardown.

These were not the original three failures and were not attributed to the earlier repair family. Shard 1 recorded `1339 passed, 40 skipped`; that partial success did not cover the failing shard.

## Root causes and repair

- **Eval negative control could be falsely red.** It now proves that observations exist, `RunResult` exists, `n_applied == 2`, and both `notes.md` and `summary.md` were applied before evaluating the legacy whole-Run checks. Assertion diagnostics retain the complete observation and evaluated results; the legacy rejection criteria are unchanged.
- **Authentication timing did not identify the measured phase.** `ensure()` first completes a real authentication handshake and proves readiness. The test then enables the stall and leaves the accept loop free, so the shutdown helper launched by `stop(timeout=0.3)` is the connection that enters the authentication barrier. Its cold interpreter startup, imports, IPC authentication, graceful wait, escalation, and reaping all remain inside the original bounded stop path; the marker cannot be pre-created by a probe. A first revision that left a probe blocked in authentication was rejected locally because it prevented the helper from ever reaching the server-side barrier.
- **Failed-start cleanup assumed process termination runs Python cleanup.** `_abort_start()` now requests graceful shutdown first, bound to the exact readiness snapshot and same-connection `instance_id`. If that fails, it reaps only its owned `Popen`. After exit is confirmed, it removes only lock bytes exactly equal to the readiness snapshot. A replacement or unreadable lock is never intentionally cleared, and no PID from a replacement lock is signalled.
- **Cleanup failure discarded retry evidence.** Final lock read/compare/unlink failures now retain the dead owned `Popen` and readiness snapshot. State is cleared only after the exact lock is confirmed absent or removed. A retry that observes a replacement drops only the dead owned handle and preserves the replacement.
- **Lock identity changes lacked one cross-process linearization point.** Daemon lock creation, stale takeover, and identity-checked deletion now share the independent `daemon.lock.guard`. It does not reuse the Host metadata guard, because Host can hold that guard while waiting for its owned Daemon to exit. Blocking test reads use bounded queues, and failure paths reap children and join readers.
- **Numeric-PID escalation was not an atomic process identity check.** Forced cleanup now accepts only an owned `Popen`. On POSIX, `waitid(WNOWAIT)` must still observe a running, unreaped child before signalling; on macOS the ctypes `siginfo_t` matches the complete SDK layout. On Windows, `TerminateProcess` uses the retained original process handle and never falls back to a PID, `OpenProcess(PID)`, or `taskkill`. Attached Daemons are never force-signalled. A normal restart finishes retained-process wait and exact lock cleanup before spawning its replacement.
- **Windows tree cleanup is deliberately not claimed.** Replacing the old numeric-PID `taskkill /T` call removes its PID-reuse hazard but also removes that tree-termination behavior. Forced cleanup now confirms only the owned Daemon root; Daemon-created execution descendants may survive. This does not expand the product's isolation promise, and complete Windows process-tree supervision remains open under [#224](https://github.com/lorek666/tianshu-agent/issues/224).
- **Native interpreter switching must preserve the caller's test environment.** `scripts/measure_slo.py` now retains an existing `PYTHONPATH` and appends it to the native interpreter's `sys.path`. The Windows Host child launcher applies the same rule. This keeps `tests/_startup` loaded in the real child without changing the production maintenance-lock getter or weakening the diagnostic's refusal of an unisolated lock root.
- **Shutdown acknowledgement is not process exit.** Test cleanup now sends an exact-`instance_id` graceful request, records PID plus OS birth identity, and waits for that observed process to exit before the temporary directory is removed. PID reuse is accepted as evidence that the original process has already exited; it never authorizes signalling a replacement. Timeout remains a hard failure and no unowned PID is terminated. The helper is shared by Host supervisor and cleanup tests so request delivery cannot be mistaken for handle release again.

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

### Integrated follow-up validation

On macOS arm64 in `agent-general-purpose-pr228-integration`:

- Focused Host/native-child regression: `155 passed, 1 skipped`; real SLO state-pressure diagnostics: `22 passed`; affected domains: `326 passed, 1 skipped`.
- Private temporary-plugin mutations restored the old `PYTHONPATH` replacement and request-only cleanup behavior. Both negative controls failed deterministically with `PYTHONPATH 缺少 startup:src` and `assert True is None`; the source tree was not modified by either mutation.
- The first full run correctly failed four disk-space checks because the default temporary volume had less than the required 2 GiB free. The final run used `TMPDIR` and `TIANSHU_TEST_TMP_ROOT` on `/Volumes/Date` and completed with exit code 0. No low-space guard or failing assertion was relaxed.
- `uv lock --check`, `uv run --frozen ruff check .`, and `git diff --check` passed.

The new cleanup regressions prove that the observed process is polled until exit and that PID reuse ends the wait without signalling the replacement. The native-child regression requires the original startup/source paths to survive interpreter switching.

## Independent Tier 1 review

Final independent review is pending for the integrated tree. Reproduction: list tracked files under `src/`, `tests/`, and `scripts/` with `git ls-files -z src tests scripts`, sort their repository-relative UTF-8 paths, then update SHA256 in order with `path bytes + NUL + SHA256(complete file bytes)`. Earlier reviews found retry-snapshot loss, a probe/helper barrier defect, initial-replacement retry evidence loss, unbounded child reads, and an ambiguous hash recipe; those findings were repaired and are not the final review binding. The old `src/` + `tests/` hash `4de20d3bef737a5a5eb2d576711c0a9a7719d0453149a1e66ec927a206a77959` is invalidated by the SLO script and test follow-up.

## Full local suite and remaining verification

The final full local suite is green. Independent review of the final `src tests scripts` tree is still required before push.

Hosted required CI must still verify the new head, including real named-pipe authentication timing, `TerminateProcess` cleanup, native-child lock-root isolation, Daemon log-handle teardown, both paired workers, strict shard reports, and all required jobs. Local macOS success is not Windows native evidence, and this repair does not claim physical process isolation or user-device validation.
