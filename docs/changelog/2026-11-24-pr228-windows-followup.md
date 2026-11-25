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
- **Pre-readiness failure must retain lock-cleanup evidence.** The first frozen independent review found that a child could create `daemon.lock` before authenticated readiness, then be reaped without an exact snapshot; retry could discard the dead handle and start a new generation while the old lock remained. The supervisor now captures a PID-matching pre-readiness lock snapshot separately from the authenticated readiness snapshot, always finishes retained-process cleanup before spawning, and fails closed if a residual lock cannot be proven to belong to the failed child. Cleanup errors retain both the dead `Popen` and the exact lock bytes for retry.
- **A reaped POSIX leader cannot authorise `killpg` by numeric PGID.** The same review found that normal completion reaped the leader before `_stop_process()` signalled its process group, allowing PID/PGID reuse. Group escalation now occurs only while the leader remains unreaped, which pins the numeric identity. After reaping, the code performs no further signal; it reports `exited` only when the already-signalled group is observed absent, otherwise `unknown`. Normal completion without a safe group identity no longer claims tree cleanup.

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

The first independent frozen review returned `partial_fixed`, not approval. Its three actionable findings were replayed against the author tree: the pre-readiness lock assertion failed with the lock still present, the retry test failed with `DID NOT RAISE HostError`, and the reaped-POSIX-leader test failed with `a reaped leader's numeric PGID must never be signalled`. The corresponding repairs turned all three regressions green; the real Seatbelt timeout/tree regression also passed. The remaining direct test-cleanup block was unified under `shutdown_daemon_and_wait`, and explicit exact-instance/timeout regressions were added.

The new cleanup regressions prove that the observed process is polled until exit and that PID reuse ends the wait without signalling the replacement. The native-child regression requires the original startup/source paths to survive interpreter switching.

## Independent Tier 1 review

The first frozen review bound commit `6db152ad76370f544972c239ff43e51399ddca4c` and tree SHA256 `31723ce5e000bb17d4235bdd0807cc5ee9d3010695701ed17ce15fed63f1e689`. It verified the frozen tree, ran positive lifecycle/isolation suites and valid mutation RED controls, but returned `partial_fixed` for the pre-readiness retry evidence, reaped POSIX PGID escalation, and one non-unified test cleanup path. Those findings were repaired and the parallel remote Windows/SLO work at `c1feb41` was integrated.

The first final re-review again returned `partial_fixed`: the cleanup helper treated an unreadable later birth identity as proof of exit, and an initial POSIX `killpg` `ProcessLookupError` incorrectly made an unsignalled group report `exited`. Both counterexamples were reproduced privately and now have regressions that fail on the old behavior. Cleanup distinguishes same birth, changed birth, confirmed PID absence, an exited-but-unreaped child via `WNOWAIT`, and unknown; process-group cleanup reports `exited` only after a signal was actually sent and the group was then observed absent.

The final code commit is `5e33b732a2f1514cddf2395384e40874e5f9ba3f`. Its normative tree covers 384 tracked files under `src/`, `tests/`, and `scripts/`, sorted by repository-relative UTF-8 path and hashed as `path bytes + NUL + raw SHA256(file bytes)`. The resulting SHA256 is `a693e9d3c3756ab1c61b32964e8f5a8e7b3666a2ef5a3576923f4f96298bcf89`.

Three independent reviewers for lifecycle, execution process-group isolation, and SLO environment/lock-root domains all returned `fixed` against that tree. Their report SHA256 values are `a4f69af0d1696784c67993d717e7657c43f80244a313dd0a13dab203dc9beba7`, `29b9047f281da725bea6834f66a96339a052d53b21590c28066f1d4a701c61fa`, and `22e9ae66549223ddc9ba0dd631d287c64d85a2f56f76cfcb397653508056f067`. The parent agent rebuilt a fresh private copy, recomputed the normative tree, replayed the key green regressions and three mutation controls, restored the tree, and confirmed the same hash; the replay receipt SHA256 is `60130e11b97d37b8c77956072af7696aef2ef9ec9a31232649faef2a046dfe02`.

## Full local suite and remaining verification

The final affected-domain regression and the lock, Ruff, diff, and local-link gates passed on the frozen code tree. The unfiltered `uv run --frozen pytest` run on that same tree exited 0; its dynamic totals and skip reasons are recorded in the log with SHA256 `dcc1c560a46c1b2a7cbf7a13e67027a82d4d0233c98450eb960621526faab74b`. Only `TMPDIR` and `TIANSHU_TEST_TMP_ROOT` were pointed at `/Volumes/Date/tianshu-pytest-8dGaXd` to satisfy the existing 2 GiB low-disk admission guard; no guard or assertion was relaxed.

Hosted required CI must still verify the final head, including real named-pipe authentication timing, `TerminateProcess` cleanup, native-child lock-root isolation, Daemon log-handle teardown, both paired Windows workers, strict shard reports, and all required jobs. Local macOS success is not Windows native evidence, and this repair does not claim physical process isolation or user-device validation.
