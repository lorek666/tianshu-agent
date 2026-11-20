# Restore payload history fix (2026-11-20)

## Background

Tier-1 restore review found two fail-open paths in recovery evidence. Restore and guard block scanners read only the inline event payload, so spilled `recovery.blocked` / `recovery.unblocked` payloads were invisible. Historical v0 `tool.result` rows with `status="unknown"` and no `effect_state` were also not consistently treated as unresolved side effects.

## Decision

All restore and guard block scanners use the verified saved-payload reader for inline and spilled payloads, including hash and reference validation. Historical v0 unknown results remain unknown; release derives the fenced run effect state from that versioned evidence and preserves block evidence. Live recovery applies the same conservative interpretation when repairing orphan block events. Existing `preserved_unknown_intents` counts remain scoped to newly reconciled unresolved intents and do not double-count already-recorded result rows.

## Verification

The candidate includes real EventLog/backup/import/release and live `recover_run` regressions covering spilled block history, v0 unknown results, malformed active block history, and unknown MCP dispatch targets. The focused regression set passed with 7 tests; `uv lock --check`, full Ruff, compilation, and `git diff --check` passed on the final code tree. The isolated full local command `uv run --frozen pytest -o addopts="" -q -rs` exited 0 with **4196 passed, 15 skipped in 868.89s (0:14:28)**. The independent Tier-1 second reader re-reviewed the final tree and marked all four findings fixed with no remaining code-level blocker. The final non-document `src` + `tests` diff fingerprint (`git diff --binary HEAD -- src tests | shasum -a 256`) is `7a531a7b4122558db2dc3991d4f103a118e823a850343f06117816ac21bb39c0`. Remote required CI and release-device evidence remain outstanding and are not represented as local proof.
