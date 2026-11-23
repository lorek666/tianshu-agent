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
