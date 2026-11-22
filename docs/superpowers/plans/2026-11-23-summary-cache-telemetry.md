# Summary Cache Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist auxiliary summary cache usage and report its coverage without claiming unmeasured benefits.

**Architecture:** Add immutable optional summary audit tables, record before/after the existing model boundary, and collect them alongside C5 evidence. Main conversation events and budget semantics stay unchanged.

**Tech Stack:** Python 3.12, SQLite, standard library, pytest, Ruff.

**Spec:** [2026-11-23-summary-cache-telemetry.md](../specs/2026-11-23-summary-cache-telemetry.md)

## Global Constraints

- Python `>=3.12,<3.13`; zero third-party runtime dependencies.
- Unknown usage/cost remains unknown; no model retries or extra paid calls.
- No changes to C1–C5, execution authorization or budget settlement.
- Isolated temporary data roots; no production credentials.
- Freeze all code before one full local suite and one candidate CI.

## Review Focus

- A returned response whose summary body is rejected still incurs usage.
- A hard exit after sending cannot become a measured zero or a retry.
- Corrupt or unsupported audit records must not supply a plausible hit ratio.
- Root/child/followup evidence must not count the same call twice.
- Legacy databases and backup/GC retain truthful unknown coverage.

## Execution Rulings

- Ruling: reuse `BudgetLedger.abandon_unsent` when pre-send telemetry fails —
  release only this unsent reservation without recording a sent zero-usage call.
- Ruling: restore shared-budget assembly in the evaluation engine and version
  RU-D1 to 1.0.1 with explicit offline child usage receipts — the old missing
  parent account concealed accumulated unknown reservations. Keep the token
  limit and acceptance checks unchanged; historical fixture scores are not
  directly comparable. This does not supply real-provider measurements.

### Task 1: Durable auxiliary usage

**Files:** Create `src/tianshu/summary_telemetry.py`; modify
`src/tianshu/summaries.py`, `src/tianshu/_loop_summary.py`;
test `tests/test_summary_cache_telemetry.py`.

**Interfaces:** `begin_call(log, record, *, messages, identity, policy) -> None`,
`finish_call(log, summary_id, *, usage, cost) -> None`,
`read_calls(log) -> list[dict]`. Wire only at the real summary model boundary.

- [ ] Write tests for persistence, immutable identity, invalid data, rejected bodies, unknown failures and hard exit.
- [ ] Run the new tests and observe failure for missing persistence.
- [ ] Implement the two append-only tables and validated versioned payloads.
- [ ] Wire the real summary boundary without modifying accounting or main events.
- [ ] Run targeted summary/replay/budget tests.
- [ ] Commit implementation and tests with a reason.

### Task 2: Report and lifecycle evidence

**Files:** Modify `src/tianshu/eval/checks.py`, `src/tianshu/eval/runner.py`,
`src/tianshu/cache_metrics.py`, `src/tianshu/data_cli.py`;
test `tests/test_cache_metrics.py`, `tests/test_summary_cache_telemetry.py`.

**Interfaces:** `Observation.auxiliary_model_calls` carries versioned persisted
records. Cache sidecar adds auxiliary requests, auxiliary/all usage; existing
main usage remains main-only.

- [ ] Write failing report tests for auxiliary coverage, unknowns and deduplication.
- [ ] Collect root/child/followup records before closing the database.
- [ ] Build separate auxiliary and combined metrics; retain C5 verdicts.
- [ ] Prove backup roundtrip and GC protection; expose tables in data statistics.
- [ ] Run affected domains, then commit.

### Task 3: Freeze and verify

**Files:** Update `docs/prompt-cache.md`, `docs/KNOWLEDGE.md`;
create `docs/changelog/2026-11-23-summary-cache-telemetry.md`.

- [ ] Reconcile claims and link paid A/B prerequisites; do not mark it done.
- [ ] Run lock/Ruff/diff checks and the final full local suite.
- [ ] Obtain an independent Tier 1 review bound to the code/test tree.
- [ ] Push the frozen draft, then run one complete candidate CI and verify actual identities.
- [ ] Report remaining paid experiment inputs precisely; no merge or release.
