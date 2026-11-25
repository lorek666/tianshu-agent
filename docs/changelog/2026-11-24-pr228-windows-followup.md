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

## Hosted run 45553583330 and v40 repair

The previous documentation head `4133bbd0c8a52029c392dc82577f91e6bd148d50` completed run [45553583330](https://github.com/lorek666/tianshu-agent/actions/runs/45553583330) with a real failure. Digest-verified raw reports recorded six Windows failures and seven Ubuntu failures; the Windows aggregate failure was derivative. The complete failed-log SHA256 is `5abcf08c1b013564c978748048de4a192479c3d603e1b538d3d577929d2fd7d6`.

The Windows failures were test portability and restart-observation defects, not six independent product regressions: two tests accessed nonexistent `os.killpg`, one required an unavailable POSIX cleanup branch, one mutated process-global `os.name` and broke pathlib, one ran a native POSIX `WNOWAIT` child test on Windows, and one treated a transient missing `daemon.lock` during attached-stop restart as terminal. Module-local `SimpleNamespace` fakes preserve the behavioral assertions without changing global OS state. The native POSIX test is explicitly skipped on Windows, while a cross-platform regression covers Linux's no-status return contract. The restart loop retains its deadline, liveness, and replacement-owner assertions while tolerating only `FileNotFoundError`.

The Ubuntu failures exposed a product bug. On Linux, nonblocking `waitid(..., WNOWAIT)` returns `None` while the child is still running. The old code dereferenced `info.si_pid`, converted the resulting `AttributeError` to `unknown`, and refused to terminate a genuinely running owned Daemon. The supervisor now treats both `None` and `si_pid == 0` as running. Test cleanup also continues past an unchanged birth identity when the PID still exists, because a Linux zombie retains both; `WNOWAIT` must confirm its exit.

Repair commit `dbeed810899d3692e5b93e48b17d29d79a2971a8` binds normative tree SHA256 `ff87e28cf9ee45d9623a7b4648a2c828b0409c7f6c11d04fcb7918257100e3bf` over 384 tracked files under `src/`, `tests/`, and `scripts/`. Two private reverse mutations failed as required, with log SHA256 values `1187a5dd0f5ebdbe2d3a5e9a3231dc160665b955468e9b690a1fb2c3c5f43706` and `63fbe4be2bbc1874e449319765a4cd8e8e825489e76add015d89341829696ae7`. The affected-domain, lock, Ruff, diff, and unfiltered local pytest gates exited 0; the domain and full-suite log SHA256 values are `6b844a1cdf198df044ba7ef420743f87bd8216ebc48b21411745ec43f5e760d0` and `6066abc7cba50e12291c6eda27647bb32f98ba2a8fd70959a617eb9f15b5caee`. Dynamic totals and skip reasons remain in the logs. An independent second reader returned `fixed` for the same normative tree after focused tests, real-child probes, and two reverse mutations; the review receipt SHA256 is `e272b116f6e795d37e576bccfc30c26736aed21ac6441129d5552dc326c0c3f5`. The v40 repair still required a fresh hosted run.

## Hosted run 45579136730 and v41 signal follow-up

Head `92c6a372fa0b4df348789ef6329d90383ac53158` completed run [45579136730](https://github.com/lorek666/tianshu-agent/actions/runs/45579136730) with a real failure. All four pytest ZIPs were checked against their GitHub artifact digests. Both macOS shards and Windows indices 0, 1, and 3 had no failed nodes; Windows index 2 had exactly one failure: `tests/test_execution_admission.py::test_unsignalled_posix_group_is_unknown_after_process_lookup_error`. The Windows required aggregate failed as a derivative result. The complete failed-log SHA256 is `c559b3ead5ccfcbc9e7f3c57ac1e41d58803f1e12a91bc5e4bfbb07128cf79bd`.

The test already replaced module-local `os`, but product code evaluates `signal.SIGKILL` before calling the fake `killpg`. Windows has no such `signal` constant, so the test raised `AttributeError` before reaching its intended `ProcessLookupError` branch. Commit `b62a6dc142d50771d1a60a1b0ba7745908d649bd` also replaces module-local `signal`; it changes no process-global state and retains the `unknown` assertion. Its normative tree SHA256 is `e963b51a8f97e394a40dc10ba548c1398bd55ce5f5491ff2e2f92b334b25ae0d` over 384 tracked files.

The two focused tests passed, and the affected-domain gate exited 0 with log SHA256 `734390e792d90a6228bd4b3cea94f903a443cf232d2ecad44a9a9d453c86a038`. In a private copy, emptying the signal fake made the target test fail at `src/tianshu/execution_admission.py:496` for the intended missing-`SIGKILL` behavior; restoring it returned to green. Positive, negative, and restored log SHA256 values are `2de9fce2a129e8da29f56ca2520ffb53e6692456adc23a6a0e2e124adba5e421`, `6413f2ba7b76b95ac44fa3c2745893b76c32e8179ba9a55d7c36e5607ee5fa30`, and `5b19689a97d618d5cdfce632de67285a8efa3c57100a80350a5bb7ca9e05ae34`. An independent second reader returned `fixed` for the new normative tree, and the parent replayed the critical private probe. Review receipt SHA256: `6a150bfcd8fb5660dda0da649dd9c158750457a28c6ea3ea627ef73254f92103`. The unfiltered local suite exited 0 on the same normative tree with log SHA256 `763cbfb06c8b421c8852994d507efe8d3ead4a36940b7815258ad22c52e16b92`; dynamic totals and skip reasons remain in the log. Lock, Ruff, diff, and relative-link gates exited 0.

## Hosted run 45593616819 and v42 attached-exit follow-up

Head `106149771ac6d5a85e24fdac7a16279815043eb3` completed run [45593616819](https://github.com/lorek666/tianshu-agent/actions/runs/45593616819) with a real failure. The Windows shard-0 ZIP SHA256 `9525b2d429f275304f7edefae5b660c5ab0b3ae9e79b23f6f31a921c6ef43c70` matched its GitHub artifact digest. Its raw report is bound to synthetic merge `ac8b5e77c181f134526ec59bdfc1902bcb495714`, run ID `45593616819`, attempt `1`; index 0 has exactly one failed node, `tests/test_host_cleanup.py::test_late_real_host_cannot_replace_the_running_owner_or_stop_its_daemon`, while index 1 has no failed reports. The test body reached cleanup and printed `Host 已退出`; `TemporaryDirectory` then raised `PermissionError: [WinError 32]` while deleting `logs/daemon.log`. The complete failed-job log SHA256 is `a0a6617b743ae6763f4f518f329d7c1e8b13ae536fb78b855f2b70cb844066ef`.

The attached Host had confirmed only IPC endpoint disappearance. The Daemon could still hold the log handle inherited from the first Host after removing its lock and closing the listener. Attached shutdown now freezes the original PID plus OS birth identity at the first shutdown snapshot, waits for both endpoint absence and that process's exit, and never signals the attached PID. Windows observes exit through the existing read-only `OpenProcess(SYNCHRONIZE)` plus `WaitForSingleObject(0)` path. POSIX additionally recognizes an exited non-child zombie through `/proc` or bounded `ps`, because `waitid` cannot observe another process's child. A missing lock permits waiting but is not success evidence; unreadable locks, replacement instances, and unconfirmable identity still fail closed. Test cleanup can retain the pre-stop lock snapshot so lock removal cannot erase the process it must wait for.

The v42 normative tree covers the same 384 tracked files under `src/`, `tests/`, and `scripts/`; its SHA256 is `b7a3130964be51cbd0e73fcc37750ca4d3692705ae18c45ebf34b4fd3a6bcd42`. The focused positive gate exited 0 with log SHA256 `3ff1e2324a6668cb12e59ac32a1660883c64c173d5cfce6beb6a95261550a1db`. A private unfixed baseline made the two product regressions fail for the intended observations—`held_child.returncode` remained `None`, and the unknown-identity case did not raise `HostError`—with log SHA256 `875a7a0f89982983a03cab68a8c5e75612733dee61e87b75643c8d72f2be0c56`. The affected Host/Daemon domain exited 0; dynamic totals and skip reasons remain in the log, whose SHA256 is `1b38be1036e84d4a7f56a83545ec319ff0c75393803dca88fb79293e39366ed3`. Lock, Ruff, and diff gates exited 0.

The independent v42 reader returned `partial_fixed`, not approval (receipt SHA256 `c97202eda3fcca715c389378ff543123dea05d32f844c2ba41ff4019d682ea14`). It found that a Windows process could retain a queryable birth identity after exiting; the old ordering then returned before calling `pid_alive()` and its `WaitForSingleObject(0)`, causing a false live result. It also found that test cleanup froze lock bytes but sampled birth only later, which could adopt a reused PID as the cleanup target. The parent reproduced the Windows ordering failure before editing.

## v43 independent-review repairs (2026-11-25)

v43 always calls `pid_alive(pid)` when frozen birth has not already proved PID reuse. On Windows this preserves the read-only process-handle wait even while birth remains queryable. Test cleanup accepts a frozen `(pid, birth)` observation, validates its PID against the retained lock snapshot, and preserves an explicitly unknown birth instead of resampling it. The real dual-Host cleanup captures both observations before stopping the attached Host.

The v43 normative tree SHA256 is `b352f52a86fdfbc99a97805efbc009eb163d2ef271832b5d9e5aff28fcef0ed7`. The original Windows-ordering regression failed on the old code with `assert False` (log SHA256 `a7b9a4f89a4698e0f9fd25119f45cfbd4398d1281980ebf24b3c65183dc64691`). The repaired focused gate exited 0 (log SHA256 `9a27e78bc0d12a2b8f09fc72f4d5b35a4e724e75ea0fc57f561d5e8d80399914`). A private copy restoring both old behaviors failed the Windows observation, reused-PID cleanup timeout, unknown-birth resampling, and mismatched-PID shutdown guards for their intended assertions (log SHA256 `42b1495b7adf110c732412b508f6367f86e25c4b62b40501bee08540c8ad65ec`); restoring the repaired files returned to green (log SHA256 `7256316818e0211d1c4dc30d19c5b69dd944b4f7c923e409b47993643d15fb98`).

The affected Host/Daemon/SLO/process-identity domain exited 0; dynamic totals and skip reasons remain in the log, whose SHA256 is `cb5b0b1f94769cf511a6af28b4562648ebcf980802bda067b55e5f8a0483679d`.

The new independent Tier 1 reader returned `fixed` for the same normative tree after a private 17-test green run, a valid Windows-ordering mutation red, and restored green. The parent verified the receipt and all probe hashes, compared the four reviewed files byte-for-byte, recomputed the archive tree, and replayed the critical probes in a fresh private copy: green, intended mutation red, and restored green all matched. Review receipt SHA256: `82bcbb83570a10c24d9f2e62b4b3c7806c97c6d74bcc62958c3f5b729f6f9492`. Parent replay green/red/restored log SHA256 values are `a4835840358f59fd69453f15a3bad9641cd2cf0b3d31efa6e404619244987ad2`, `c5651d2e208900a551092f10fb4b7e8baec1ea961f62696f702039dde49d0e25`, and `b6c51b401d65a9c69435f8b86cecab752d65ded7188228aab3de3eb010990b60`.

The unfiltered suite ran on this same normative tree with only `TMPDIR` and `TIANSHU_TEST_TMP_ROOT` moved to the high-space volume and exited 0; dynamic totals and platform/opt-in skip reasons remain in the log, whose SHA256 is `51820e400cf705d8fff48663f379bca642d1b2fda2b6154e9d6bcd7b827d8ba0`. Lock, Ruff, diff, and changed-document link gates also exited 0. Fresh hosted required CI remains pending. The stopped v42 full-suite run is not final evidence for this changed code/test tree, and the macOS review does not claim native Windows validation.

## Hosted run 45608812868 and v44 SLO diagnostics (2026-11-25)

Head `4a1e20b7d4fcce797f23edd99dc023e5a4047468` completed run
[45608812868](https://github.com/lorek666/tianshu-agent/actions/runs/45608812868)
as `completed/failure`. Its synthetic merge is
`e666f5d1e8a2d7a154d94b7ee755ef4f841ea2dd`, attempt `1`, base
`a755e2abf7d75b6baf36a1e47f47646142f0bc8d`. All downloaded pytest and restore
ZIPs matched their GitHub artifact `digest`; both macOS shards, Windows indices
0–2, and all non-Windows jobs had no failed reports. Windows index 3 had exactly
one failed node:
`tests/test_slo_and_soak.py::test_measure_slo_smoke_and_run_soak_real_subprocesses`.
The Windows required summary failed as the derivative result.

The failed measurement recorded cold-start `p50=1906.458ms`,
`p95=2297.528ms`, `max=2297.528ms` against the frozen `2200.0ms` p95 limit. All
other measured SLO dimensions passed, `orphan_pids=[]`, and no safety assertion
failed. Three repeats use nearest-rank p95, so the p95 equals the maximum and a
single slow sample rejects the gate. This is not a reproduced v43 lifecycle
failure, but it remains a real red gate. The failed job log SHA256 is
`5ef4e8647c3d8cdc2afa890dfe834cd95c2acbacdb9bece266fee32dbde210c9`, the
Windows summary log SHA256 is
`a64850b166037828cce39e8c4880376843ad5ea09af31d77016e6fe736193fff`, and the
bound run record SHA256 is
`e2b76f708b9a33bf0bac52e1464f906f4f2ca02c4963ed918a943db452afc0ec`.

A timeline review does **not** establish same-job worker contention as the
cause: the paired worker reported completion at `12:00:42Z`, while the SLO test
began at `12:01:37Z`. Historical native runs show this node has passed far more
often than it has failed, and no dedicated cold-start issue or prior repair was
found. A speculative serial-pair mitigation was implemented and tested, then
withdrawn before commit because it would not change that measured interval and
could push the job toward its 25-minute limit. Its aborted full-suite run is not
acceptance evidence; the saved patch SHA256 is
`1818d449451d5454d477e0f3390a87ba3e3a2cb573b9f5732dc14403e10f586c`.

v44 therefore changes observability, not the performance gate. The SLO CLI can
write a separate `slo-samples-v1` document containing every raw numeric sample.
The real smoke test publishes that document, the aggregated reports, soak
materials, redacted stdout/stderr, return codes, timestamps, CPU count, platform,
and shard index before evaluating assertions. Windows uploads these files from
`slo-diagnostics/`, a sibling of the strict shard-report root, with `always()`;
the verifier still accepts exactly the four original `shard.json` files. The
pair runner supplies each worker's shard index through its private environment,
and the existing real four-worker overlap regression checks that binding.

The four whole-file shards, two independent runners, default concurrency,
25-minute job limit, raw reports, 60-second stack diagnostics, strict aggregate
verification, SLO thresholds, and existing assertions are unchanged. No retry,
sample discard, threshold increase, or failure suppression was introduced.

The real macOS SLO/soak smoke exited 0 and produced the expected diagnostic
bundle (log SHA256 `423b1d0e014eb1eab96f4420f7b344c2615be505dd574b756ac884826ca74f2d`).
The affected SLO/CI/Host lifecycle domain exited 0; dynamic totals and skip
reasons remain in log SHA256
`3191b63f848ac72062df85185af1c870d79c28cfee50c6a4e575d2f71dd204ce`.
The v44 normative tree SHA256 is
`4178264383f0b23cee3d98e2c9da52ebfe45c5bc6ba677c21bb551204e6ec927`.
The independent reader ran 55 focused tests green, disabled raw-sample emission to
produce a valid real-SLO `FileNotFoundError` red, and restored green. Its first
report returned `partial_fixed` only because the Windows p95 root cause remains
unrepaired, which is outside this diagnostics-only scope; the original report is
retained. The parent independently replayed 55 green / intended sample-file red /
restored green in a complete private copy. Parent log SHA256 values are
`c5041d5adb8e17000848ce7d829f8ccffbdd3b77673d39964f1c88c6911796ee`,
`7b5fe6b5cc32bbd7c4408df4618a10c7f3cb7438c5382555f1e5c7fda0a4903e`, and
`d0ba8b40647c1d2e8e26d87409bbed6470da5e31fa71793922b7e74e7457939e`.
Two earlier private-copy attempts omitted `.github` or `schemas/slo` and failed
before the intended behavior; those are harness failures, not mutation evidence.

Unfiltered local pytest on the same v44 code/test tree exited 0:
`5718 passed, 15 skipped in 1075.51s` (log SHA256
`3b0327cbe43ad23decdda0e00f23b7574af541ec1dd9dcfecd1ef3c55cff7055`), using
isolated temporary root `/Volumes/Date/tianshu-pytest-v44.q4xiaz`. That invocation
did not request `-rs`, so individual skip reasons were not emitted and are not
reconstructed here. No low-disk guard or assertion was relaxed. Scope-bound
independent rereview and fresh hosted required CI remain pending. This
diagnostics change does not claim the Windows p95 root cause is fixed, and macOS
results do not replace native Windows evidence.

### v44b independent-review repairs (2026-11-25)

The scope-bound second reader returned `partial_fixed` for tree `417826…` and
found three concrete gaps: a child timeout escaped before diagnostics were
published; raw-sample assertions compared only counts and extrema, so a changed
p50 could pass; and the diagnostic upload's `always()` guard had no negative
control. The report SHA256 is retained at
`/tmp/pr228-v44-diagnostics-scope-review.md` with its external `.sha256` file.

v44b converts `TimeoutExpired` into a distinct return code 124, preserves any
captured stdout/stderr, records `timeouts` in `run-context.json`, publishes the
bundle, and only then asserts failure. The real SLO test now reloads the frozen
threshold configuration and recomputes the complete metric object from raw
samples with `_summarize_samples()`, binding p50, p95, min/max, counts,
thresholds, and pass state. The workflow test asserts the exact diagnostic
upload step including `if: always()` and rejects a `success()` mutation.

The repaired normative tree SHA256 is
`44e96142792abda13e7280d4803e70d1bfd50378bdba7f288dee6ebf3594a6ae`.
Focused diagnostics/CI tests exited 0 with 57 passes; the real SLO/soak smoke
exited 0 (log SHA256
`128e72c602e7d2e637831ad47924f5efed5047559922113c9b4ed483b19de307`).
In a complete private copy, forcing a real SLO timeout returned 1 while
preserving `timeouts.slo=true` and return code 124; changing a non-extreme raw
sample returned 1 on the full metric comparison; changing the upload guard to
`success()` returned 1. Restoring all files returned to green and tree
`44e961…`. Probe log SHA256 values are
`eb7ca38504e5762c281f5f17e1dd01c5bfe47cb19d1021a0891f37702500b9bf`,
`ce211cbd3876c7ee4765b6b94ad8fe31ee2deb900f8754ca7c98e185ba78954d`,
`b9a42a4a99871188f6f530caaaa3f0ea453ec9b88495a6bac9bba61e66e13693`,
`96e0bcb839dca34eae37a630c2e911f1ce56906535d553898389c0828ccc4b61`, and
`1c6873f28b6506b744dc9b512af90aba54c140b88869385505becf4d231e5c72`.
The directly affected CI-harness/SLO domain exited 0 with `184 passed in
100.49s` (log SHA256
`17dc938607ede9c684e2981bb9845ae6b9c845f3b8874e11f2d3ac8c484cc46e`).
Unfiltered local pytest exited 0 with `5720 passed, 15 skipped in 1155.30s`
(log SHA256
`fffcd1461a9f98dbd122bc224cd2c9255838aa9d13396923836f74cbfa3733aa`),
using isolated temporary root `/Volumes/Date/tianshu-pytest-v44b.QRl7yX` and
`-rs`; the log retains every skip reason. No low-disk guard or assertion was
relaxed. A fresh independent Tier 1 reviewer returned `fixed` for the same
384-file tree after 66 scoped tests passed, a non-extreme sample mutation failed
on the intended `p50_ms` comparison, and restoration returned to 66 passes with
the original tree hash. The report SHA256 is
`b6c9a9fd471fca70ce840d59b922c28852947728ad57dfd8942a4d8c076f7682`; its
green/red/restored log SHA256 values are
`0a6471a969a550de51fc6833d1052a686b44e0a92ee46aefdb18beed707180d7`,
`49fd451561523066c2e21af30cbe9990b1ddd30b020c227efbba0c9f5c8ebb1a`, and
`dc2e0517c60823864f2a57862de44a649663a99e749b1fefc99717c9e86235c3`.
The parent compared all 384 reviewed files byte-for-byte and replayed the
critical probes in a second fresh private copy: 66 scoped tests passed, the
sample mutation failed on `p50_ms`, the upload-guard mutation failed on the exact
`always()` assertion, and restoration returned to 66 passes with tree
`44e961…`. Parent green/sample-red/upload-red/restored log SHA256 values are
`9dfb0937940290479f2d09a91d9e5e867318cb6848667f478c7fb864c4bf59a2`,
`2c4251296c1fdac7bdddd02b7d6a590a8839662d3bb9fe55755c0f34c635ff04`,
`325240c79d01befb460ea1f46c425363d26c9b00acae2fffd9410494a6b39169`, and
`ddcfb5ed6f0080f1e08a9803305e35d2a8b3d56c33823d434bdbee016641bb6b`.
Fresh hosted required CI remains pending; the earlier `417826…` full-suite
result is not reused as v44b acceptance evidence.
