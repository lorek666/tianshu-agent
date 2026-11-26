# Control enqueue evidence implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan.

**Goal:** Replace the stale model-event assertion in the low-disk Run test with evidence from the real event contract and Worker.

**Architecture:** Keep the existing parametrized producer test. Bind its control branch to `EVENT_TYPES`, check the queued Run before driving it, then drive that same Run and check the resulting events and native MCP startup marker.

**Tech Stack:** Python 3.12 standard library and the existing pytest fixtures.

**Spec:** [Issue #226](https://github.com/lorek666/tianshu-agent/issues/226).

## Constraints and review focus

- Change no product code, event schema, isolation policy or CI behavior.
- Preserve the low-disk rejection and the other producer branches.
- Use the real Worker, model adapter, event log and MCP fixture process.
- Keep the accepted PR #225 candidate and its evidence unchanged.
- Treat missing events, absent startup or an unsuccessful Run as failures.

## Task: Repair the control branch evidence

- [ ] Bind the existing event name to `EVENT_TYPES` and observe its failure.
- [ ] Replace it with `model.intent` and `model.result`.
- [ ] Drive the queued Run with a finite scripted response and assert its events, MCP startup and successful state.
- [ ] Run the producer test, affected low-disk/Mission/MCP regressions, lock and Ruff checks.
- [ ] Prove negative controls fail when early model execution is allowed, the driver is omitted or either event name drifts.
- [ ] Commit and open a stacked PR against `codex/agent-loop-implementation`; bind a fresh complete CI run to the final head and base.

The parent PR remains unmerged. This follow-up does not resolve the native isolation scope in issue #224.
