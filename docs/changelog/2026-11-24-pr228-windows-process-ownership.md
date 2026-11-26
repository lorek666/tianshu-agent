# PR #228 Windows native-process ownership CI repair

## Original problem

Hosted run `45319988804` passed macOS, Ubuntu, and UI, but Windows failed in two paired Python shards:

- `held_child` compared binary stdout against `b"ready\n"` while Windows text mode emitted `b"ready\r\n"`, causing setup errors and weakening subsequent ownership evidence.
- Windows venv/uv interpreter launchers could own a launcher PID while the actual Python worker published a different PID. The new Daemon ownership guard correctly rejected that mismatch, but supervised test/SLO child roles still used the launcher.
- SLO and soak readiness accepted any healthy Host at the isolated endpoint instead of proving that it was the exact spawned PID and OS birth identity.
- The SLO slow-tool workload allowed the launcher path while the native supervised interpreter executed the command, so admission correctly refused it before approval/backpressure assertions.
- Several supervisor fixtures inherited a Windows OEM code page and crashed while printing the Host banner.

The state-pressure failures were downstream of Suite 2 Host readiness timeout, not evidence that invalid pressure samples had been accepted.

## Repair

- Emit fixture readiness bytes with `os.write`, keep readiness assertions inside cleanup scope, and force UTF-8 only in supervisor test children.
- Run every supervised command whose executable is the current interpreter through `sys._base_executable`, preserving the environment/import path and the existing exact-PID ownership guard.
- Launch SLO/soak Host roots natively and bind readiness to the spawned Host PID plus stable OS birth identity. Reject foreign, unreadable, or recycled identities; retain early-exit stderr evidence and the existing isolation/ownership checks.
- Keep one total 30-second Host startup deadline: launch, readiness, and `/state` polling share it; readiness receives only the remaining budget and a late return is rejected before any HTTP poll.
- Keep each cluster's temporary endpoint/data/authority/lock roots isolated, and authorize only the exact native interpreter used by the controlled SLO slow-tool workload.
- Add regressions for launcher normalization, foreign PID, recycled birth identity, and early-exit diagnostics. No ownership/authentication assertion or timeout was weakened; no test was removed.

## Evidence

On macOS arm64 in `agent-general-purpose-pr228-fix`:

- `uv run --frozen pytest -o addopts='' -q tests/test_host_supervisor_exit.py tests/test_execution_admission.py tests/test_slo_process_identity.py tests/test_slo_state_pressure.py` initially exposed the new fixture clock defect and the SLO native-interpreter allowlist mismatch; after repair, the focused supervisor/state run passed (`56 passed`).
- `uv run --frozen pytest -o addopts='' -q tests/test_slo_process_identity.py` passed (`29 passed`).
- Syntax compilation passed; `uv lock --check`, `uv run --frozen ruff check .`, and `git diff --check` passed. The final edited supervisor/identity regression run passed (`65 passed`).
- The startup-deadline negative control advances a fake clock through 5s launch + 29s readiness with `/state` otherwise available. Removing the post-readiness total-deadline check produces `DID NOT RAISE RuntimeError`; the final implementation rejects at the shared deadline without issuing an HTTP poll.
- 独立第二读者对最终总截止修复结论为 **PASS**，在私有副本从 `/Volumes/Date/天枢Agent/.qoder/worktrees/agent-general-purpose-pr228-fix` 外导入模块，并核对完整 code/test tree（路径 + 实际文件字节，500 文件）前后 SHA256 均为 `96cdaf12b86afdc91f659dea010f6584c47a06dc736619f664a98fd9aaae80b2`。私有副本删除返回后截止检查时 `1 failed, 1 passed`（`DID NOT RAISE`）；恢复逐字节后 `2 passed`，原树未变。
- 最终总截止树已在本机 mac-arm64 重跑完整 `uv run --frozen pytest`，退出码为 0；锁文件、Ruff、diff 与文档相对链接门禁通过。该结果不替代托管 Windows required CI 或用户设备验证。

## Remaining verification

Hosted Windows required CI must still verify the paired shards, real named-pipe behavior, launcher/PID normalization under uv, SLO/soak startup, state-pressure qualification, and authenticated bounded shutdown. The three named execution-admission failures did not reproduce in the macOS focused suite; their detailed Windows tracebacks were absent from the retrieved failed-log section and remain a hosted-CI verification item rather than a claim of resolution.
