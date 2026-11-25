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
- Ruling: commit Tasks 1 and 2 together after the frozen verification —
  preserve the user's single final candidate CI and bind implementation and
  report collection to the same reviewed tree.

### Task 1: Durable auxiliary usage

**Files:** Create `src/tianshu/summary_telemetry.py`; modify
`src/tianshu/summaries.py`, `src/tianshu/_loop_summary.py`;
test `tests/test_summary_cache_telemetry.py`.

**Interfaces:** `begin_call(log, record, *, messages, identity, policy) -> None`,
`finish_call(log, summary_id, *, usage, cost) -> None`,
`read_calls(log) -> list[dict]`. Wire only at the real summary model boundary.

- [x] Write tests for persistence, immutable identity, invalid data, rejected bodies, unknown failures and hard exit.
- [x] Run the new tests and observe failure for missing persistence.
- [x] Implement the two append-only tables and validated versioned payloads.
- [x] Wire the real summary boundary without modifying accounting or main events.
- [x] Run targeted summary/replay/budget tests.
- [x] Commit implementation and tests with a reason.

### Task 2: Report and lifecycle evidence

**Files:** Modify `src/tianshu/eval/checks.py`, `src/tianshu/eval/runner.py`,
`src/tianshu/cache_metrics.py`, `src/tianshu/data_cli.py`;
test `tests/test_cache_metrics.py`, `tests/test_summary_cache_telemetry.py`.

**Interfaces:** `Observation.auxiliary_model_calls` carries versioned persisted
records. Cache sidecar adds auxiliary requests, auxiliary/all usage; existing
main usage remains main-only.

- [x] Write failing report tests for auxiliary coverage, unknowns and deduplication.
- [x] Collect root/child/followup records before closing the database.
- [x] Build separate auxiliary and combined metrics; retain C5 verdicts.
- [x] Prove backup roundtrip and GC protection; expose tables in data statistics.
- [x] Run affected domains, then commit.

### Task 3: Freeze and verify

**Files:** Update `docs/prompt-cache.md`, `docs/KNOWLEDGE.md`;
create `docs/changelog/2026-11-23-summary-cache-telemetry.md`.

- [x] Reconcile claims and link paid A/B prerequisites; do not mark it done.
- [x] Run lock/Ruff/diff checks and the final full local suite.
- [x] Obtain an independent Tier 1 review bound to the code/test tree.
- [x] Document the remaining paid experiment inputs precisely.

Implementation: `ebb47ffc7de5b6e6e65147f67b40c52cdebb0ebc`, tree
`aa9a5ce399584ef96584dbc7e8fb7777f37938ea`. Verification evidence is recorded in the
[changelog](../../changelog/2026-11-23-summary-cache-telemetry.md).

Hosted verification follows the completed freeze: push the draft, run one
complete candidate CI, and verify the actual head/merge/run/artifact identities.
The exact candidate result is maintained in
[PR #222](https://github.com/lorek666/tianshu-agent/pull/222), rather than
predeclaring a future CI result in this plan. No merge or release is part of
this plan.

### Task 4: Repair the failed native candidate

The initial checks above bind only `ebb47ff` and its unchanged code/test tree.
Candidate `0144efa` failed in run `45050478841`: a real backup/GC assertion
failure preceded the Windows shard timeout. Preserve that RED result.

- Ruling: finish the backup source's ordinary SQLite lifecycle before static GC,
  and exercise preview separately on POSIX and Windows — do not weaken GC or
  remove companion files by hand.
- Ruling: calibrate only the three changed/affected cache evaluation file hints
  from completed native log intervals — retain the existing partitioner,
  diagnostic wrapper, full-coverage verifier and 25-minute limit.
- Ruling: test scheduling with all files changed by the cache batch plus the
  affected evaluation assembly — the four-file subset cannot represent their
  complete workload and must not be used to reverse-tune measured hints.

- [x] Reproduce the static preview failure without a platform mock.
- [x] Close the source normally and retain preview/approved coverage.
- [x] Record native timing sources and add a real-process scheduling regression.
- [x] Pass the affected lifecycle, sharding, wrapper and workflow regressions.
- [ ] Freeze all repairs, pass full local pytest and lock/Ruff/diff checks.
- [ ] Bind the independent review to the corrected code/test tree.
- [ ] Push the complete repair once and verify one new complete candidate CI.

The remaining boxes describe the repair's pre-freeze state; final freeze and
hosted results are maintained in
[PR #222](https://github.com/lorek666/tianshu-agent/pull/222) and ignored evidence.
Paid-provider A/B remains unrun; this repair supplies no measured cache, cost
or task-quality improvement.
