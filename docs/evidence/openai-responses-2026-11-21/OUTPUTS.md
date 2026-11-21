# 独立复审原始输出

原始文件按字节收录；提取方法与边界见 [README](README.md)。

## probes.log

<!-- source-file: probes.log; sha256: 95775189a2e8a04b74b2e2a01fa2080b4a2d5c94c6b5a198235da3b445185d28; bytes: 2147 -->
```text
terminal: failed 模型续接作用域不兼容，请创建新 Run new model calls 0
Fapproval proposal bindings before/after: [{'approval_id': (True, False)}, {'request_id': (True, False)}] cursor digest matches: False claim epochs: 1 0
Fresume without derived rows: 1 noop None
FC3 conflicting argument boolean/numeric: []
Faccepted conflicting SSE: added_status tool_use
Faccepted conflicting SSE: added_cipher tool_use
Faccepted conflicting SSE: done_tool_name tool_use
Fdefault wire tool names outside portable function charset: ['memory.search', 'memory.get', 'memory.propose', 'memory.consolidate', 'memory.edit', 'memory.confirm', 'memory.forget', 'network.fetch', 'subtask.spawn', 'subtask.status', 'subtask.result', 'subtask.cancel']
.crash accounting: sent attempts [1, 2] returned 1 saved 1 unbilled 1
Fprefix, canonical items, call_id, 12k output, replay and default archive verified; old reader: AdmissionRefused [incompatible] model.result已登记payload版本不受支持：2。请保全数据目录，使用匹配版本核对备份和恢复；不要手改版本或删除数据库。

.
=========================== short test summary info ============================
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_committed_end_then_switch_model
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_rebuild_preserves_saved_approval_identity
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_direct_resume_without_cursor
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_c3_distinguishes_boolean_from_number
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_sse_observed_lifecycle_conflict[added_status]
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_sse_observed_lifecycle_conflict[added_cipher]
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_sse_observed_lifecycle_conflict[done_tool_name]
FAILED .omc/artifacts/independent-responses-review/test_independent.py::test_precommit_crash_reports_sent_attempts
8 failed, 2 passed in 1.56s
```

## followup-original.log

<!-- source-file: followup-original.log; sha256: 439ed039eabd6e210c892a4b306e27ab053c34ff9c4ffbdd688761d48d00bb3e; bytes: 988 -->
```text
terminal: noop None new model calls 0
.approval proposal bindings before/after: [{'approval_id': (True, True)}, {'request_id': (True, True)}] cursor digest matches: True claim epochs: 1 1
.resume without derived rows: 0 noop None
.C3 conflicting argument boolean/numeric: ['$.continuation：续接值或规范化投影不满足契约']
....default wire tool names outside portable function charset: ['memory.search', 'memory.get', 'memory.propose', 'memory.consolidate', 'memory.edit', 'memory.confirm', 'memory.forget', 'network.fetch', 'subtask.spawn', 'subtask.status', 'subtask.result', 'subtask.cancel']
.crash accounting: sent attempts [1, 2] returned 2 saved 2 unbilled 1
.prefix, canonical items, call_id, 12k output, replay and default archive verified; old reader: AdmissionRefused [incompatible] model.result已登记payload版本不受支持：2。请保全数据目录，使用匹配版本核对备份和恢复；不要手改版本或删除数据库。

.
10 passed in 1.08s
```

## followup-new.log

<!-- source-file: followup-new.log; sha256: acf9e0cdb0e9cb75d102071f328e8ac1efedefe3e3148a622d7dc77d4b57bd41; bytes: 2466 -->
```text
child:  [failpoint] after_result
late tool: 16 < 27 replayed outputs ['read-first', 'call-1'] write intents / receipts 1 1
.child:  [failpoint] after_result
late tool: 16 < 27 replayed outputs ['read-first', 'call-1'] write intents / receipts 1 1
.child:  [failpoint] model-result-before-commit
with_checkpoint drop False last cursor seq 13 intents 3 reported 3 saved 3
.child:  [failpoint] model-result-before-commit
with_checkpoint drop True last cursor seq 13 intents 3 reported 3 saved 3
.child:  [failpoint] model-result-before-commit
no_checkpoint drop False last cursor seq 0 intents 2 reported 3 saved 3
Fparked: durable cursor seq 36 park seq 37 original digest matches True
F......
=================================== FAILURES ===================================
________ test_attempts_on_both_sides_of_checkpoint[no_checkpoint-False] ________
.omc/artifacts/independent-responses-review/test_followup.py:167: in test_attempts_on_both_sides_of_checkpoint
    assert resumed.model_attempts == ledger["model_attempts"] == len(intents)
E   AssertionError: assert 3 == 2
E    +  where 2 = len([Event(seq=6, run_id='run_58537f1b237b', task_id=None, type='model.intent', ts=1795233689303, actor_kind='core', actor...c0b6de60ed015e436f5bc502f36fa3bea55048f50f8', parent_seq=None, idem_key='req_run_58537f1b237b_1#2', payload_version=1)])
______________________ test_parked_cursor_hash_round_trip ______________________
.omc/artifacts/independent-responses-review/test_followup.py:195: in test_parked_cursor_hash_round_trip
    stack.tasks.rebuild_from_events()
src/tianshu/_tasks_cursor_rebuild.py:610: in rebuild_from_events
    self._rebuild_responses_cursor(r["run_id"])
src/tianshu/_tasks_cursor_rebuild.py:621: in _rebuild_responses_cursor
    rebuilt = rebuild_responses_progress(self._log.read_run(run_id))
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/tianshu/_responses_rebuild.py:109: in rebuild_responses_progress
    raise TianshuError("Responses 游标不能从持久化事件精确重建")
E   tianshu.errors.TianshuError: Responses 游标不能从持久化事件精确重建
=========================== short test summary info ============================
FAILED .omc/artifacts/independent-responses-review/test_followup.py::test_attempts_on_both_sides_of_checkpoint[no_checkpoint-False]
FAILED .omc/artifacts/independent-responses-review/test_followup.py::test_parked_cursor_hash_round_trip
2 failed, 10 passed in 3.43s
```

## followup-impact.log

<!-- source-file: followup-impact.log; sha256: 2c58063351ba85bf6f126b0484bc1997f372ad55ee238d25d9f8ae62f8c54ca6; bytes: 2509 -->
```text
child:  [failpoint] model-result-before-commit
control crash: committed checkpoint matches original stored row; attempts 3 / 2
Fpark: earlier checkpoints valid; only model_turn changes; cursor / park 36 37
park: new model calls 0
F
=================================== FAILURES ===================================
_______________ test_control_crash_then_full_projection_rebuild ________________
.omc/artifacts/independent-responses-review/test_followup_impact.py:24: in test_control_crash_then_full_projection_rebuild
    stack.tasks.rebuild_from_events()
src/tianshu/_tasks_cursor_rebuild.py:610: in rebuild_from_events
    self._rebuild_responses_cursor(r["run_id"])
src/tianshu/_tasks_cursor_rebuild.py:621: in _rebuild_responses_cursor
    rebuilt = rebuild_responses_progress(self._log.read_run(run_id))
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/tianshu/_responses_rebuild.py:109: in rebuild_responses_progress
    raise TianshuError("Responses 游标不能从持久化事件精确重建")
E   tianshu.errors.TianshuError: Responses 游标不能从持久化事件精确重建
_____________ test_direct_resume_parked_when_derived_rows_missing ______________
.omc/artifacts/independent-responses-review/test_followup_impact.py:57: in test_direct_resume_parked_when_derived_rows_missing
    resumed = engine_for(stack, adapter(retry)).resume_run(rid)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/tianshu/loop.py:502: in resume_run
    stored = self._tasks.restore_responses_cursor(run_id)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/tianshu/_tasks_cursor_rebuild.py:615: in restore_responses_cursor
    self._rebuild_responses_cursor(run_id)
src/tianshu/_tasks_cursor_rebuild.py:621: in _rebuild_responses_cursor
    rebuilt = rebuild_responses_progress(self._log.read_run(run_id))
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/tianshu/_responses_rebuild.py:109: in rebuild_responses_progress
    raise TianshuError("Responses 游标不能从持久化事件精确重建")
E   tianshu.errors.TianshuError: Responses 游标不能从持久化事件精确重建
=========================== short test summary info ============================
FAILED .omc/artifacts/independent-responses-review/test_followup_impact.py::test_control_crash_then_full_projection_rebuild
FAILED .omc/artifacts/independent-responses-review/test_followup_impact.py::test_direct_resume_parked_when_derived_rows_missing
2 failed in 0.88s
```

## final-frozen.log

<!-- source-file: final-frozen.log; sha256: fa5231c07d31bbf5f32225c32687da2444b29c3621979868e9f3bccbe6b26acd; bytes: 5166 -->
```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.2.0, pluggy-1.7.0 -- /Users/ronfee/.codex/worktrees/responses-independent-review/天枢Agent/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /Users/ronfee/.codex/worktrees/responses-independent-review/天枢Agent
configfile: pyproject.toml
collecting ... collected 24 items

.omc/artifacts/independent-responses-review/test_independent.py::test_committed_end_then_switch_model terminal: noop None new model calls 0
PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_rebuild_preserves_saved_approval_identity approval proposal bindings before/after: [{'approval_id': (True, True)}, {'request_id': (True, True)}] cursor digest matches: True claim epochs: 1 1
PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_direct_resume_without_cursor resume without derived rows: 0 noop None
PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_c3_distinguishes_boolean_from_number C3 conflicting argument boolean/numeric: ['$.continuation：续接值或规范化投影不满足契约']
PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_sse_observed_lifecycle_conflict[added_status] PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_sse_observed_lifecycle_conflict[added_cipher] PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_sse_observed_lifecycle_conflict[done_tool_name] PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_manifest_names default wire tool names outside portable function charset: ['memory.search', 'memory.get', 'memory.propose', 'memory.consolidate', 'memory.edit', 'memory.confirm', 'memory.forget', 'network.fetch', 'subtask.spawn', 'subtask.status', 'subtask.result', 'subtask.cancel']
PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_precommit_crash_reports_sent_attempts crash accounting: sent attempts [1, 2] returned 2 saved 2 unbilled 1
PASSED
.omc/artifacts/independent-responses-review/test_independent.py::test_exact_prefix_private_diagnostics_and_old_reader prefix, canonical items, call_id, 12k output, replay and default archive verified; old reader: AdmissionRefused [incompatible] model.result已登记payload版本不受支持：2。请保全数据目录，使用匹配版本核对备份和恢复；不要手改版本或删除数据库。

PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_committed_tool_after_approval_checkpoint[direct] child:  [failpoint] after_result
late tool: 16 < 27 replayed outputs ['read-first', 'call-1'] write intents / receipts 1 1
PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_committed_tool_after_approval_checkpoint[explicit-rebuild] child:  [failpoint] after_result
late tool: 16 < 27 replayed outputs ['read-first', 'call-1'] write intents / receipts 1 1
PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_attempts_on_both_sides_of_checkpoint[with_checkpoint-False] child:  [failpoint] model-result-before-commit
with_checkpoint drop False last cursor seq 13 intents 3 reported 3 saved 3
PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_attempts_on_both_sides_of_checkpoint[with_checkpoint-True] child:  [failpoint] model-result-before-commit
with_checkpoint drop True last cursor seq 13 intents 3 reported 3 saved 3
PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_attempts_on_both_sides_of_checkpoint[no_checkpoint-False] child:  [failpoint] model-result-before-commit
no_checkpoint drop False last cursor seq 0 intents 2 reported 2 saved 2
PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_parked_cursor_hash_round_trip parked: durable cursor seq 36 park seq 37 original digest matches True
PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_done_call_id_identity[False] PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_done_call_id_identity[True] PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_nested_projection_types[True-1-False] PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_nested_projection_types[0-False-False] PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_nested_projection_types[1-1-False] PASSED
.omc/artifacts/independent-responses-review/test_followup.py::test_nested_projection_types[True-True-True] PASSED
.omc/artifacts/independent-responses-review/test_followup_impact.py::test_control_crash_then_full_projection_rebuild child:  [failpoint] model-result-before-commit
control crash: committed checkpoint matches original stored row; attempts 2 / 2
PASSED
.omc/artifacts/independent-responses-review/test_followup_impact.py::test_direct_resume_parked_when_derived_rows_missing park: earlier checkpoints valid; only model_turn changes; cursor / park 36 37
park: new model calls 1
PASSED

============================== 24 passed in 4.67s ==============================
```

## final-boundaries.log

<!-- source-file: final-boundaries.log; sha256: 0c678ba10e8566c18f32da93e6d44b8d6cbebfa471aa15a2f1d10ffaa276c032; bytes: 1479 -->
```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.2.0, pluggy-1.7.0 -- /Users/ronfee/.codex/worktrees/responses-independent-review/天枢Agent/.venv/bin/python3
rootdir: /Users/ronfee/.codex/worktrees/responses-independent-review/天枢Agent
configfile: pyproject.toml
collecting ... collected 4 items

::test_before_first_checkpoint_two_crashes[retained] child:  [failpoint] model-result-before-commit
retained before first run.cursor: retained intents [1,2], cursor attempts=0; recovered intents [1,2,3], reported=saved=3, unbilled=2
PASSED
::test_before_first_checkpoint_two_crashes[dropped] child:  [failpoint] model-result-before-commit
dropped before first run.cursor: retained intents [1,2], cursor attempts=0; recovered intents [1,2,3], reported=saved=3, unbilled=2
PASSED
::test_before_first_checkpoint_two_crashes[full-rebuild-twice] child:  [failpoint] model-result-before-commit
full-rebuild-twice before first run.cursor: retained intents [1,2], cursor attempts=0; recovered intents [1,2,3], reported=saved=3, unbilled=2
PASSED
::test_repeated_park_and_worker_wakeup invalid witnesses rejected: previous-hash park-hash gap other-run other-claim other-reason missing-park
two parent parks, two claim epochs, child wakes parent; both Runs closed; double rebuild matches saved digests and replay for both
PASSED

============================== 4 passed in 6.76s ===============================
```


## test-only-targeted.log

<!-- source-file: test-only-targeted.log; sha256: 203fe8730c999aa0c61490b6c422ecad07fbf14d6b25f3f1707d95f126950c40; bytes: 99 -->
```text
.............                                                            [100%]
13 passed in 7.27s
```


## test-only-negative.log

<!-- source-file: test-only-negative.log; sha256: 65599fcaf889f78e4d467cbc777ac379924bfacbdf3e7c187ee63a5e2a9d510b; bytes: 167 -->
```text
parent test fails at previous all-other-events-v1 guard: 23
guard rejected all mutations: 49 ; explicit-v2 fallbacks/future changes: 8 ; other event bump attempts: 41
```
