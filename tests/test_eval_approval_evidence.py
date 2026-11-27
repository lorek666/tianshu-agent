"""C5 approval evidence regression using new runs; historical reports stay intact.

Positive observations come from run_suite + ScriptedModel + the real tool manifest.
Negative controls mutate copies of those observations, never the append-only database.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from copy import deepcopy
from dataclasses import replace

import pytest

from tianshu import authority, credentials, maintenance, restore_registry
from tianshu import model as model_module
from tianshu.eval import SUITE_VERSION, build_report, compare_reports, fixture_digest
from tianshu.eval import runner as runner_module
from tianshu.eval.cases import CASE_BY_ID
from tianshu.eval.checks import (
    approval_not_consumed_for,
    approval_path_exercised,
    approval_requested_for_target,
    approval_veto_recorded,
    approval_write_gated,
    completion_failures,
    effect_none_on,
    evaluate_checks,
    file_unchanged,
    safety_violations,
)
from tianshu.eval.report import _check_fingerprint
from tianshu.eval.runner import run_suite
from tianshu.events import canonical_bytes, sha256_hex
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.tools import tool_manifest, validate_args


@pytest.fixture(autouse=True)
def _private_evaluation(
    monkeypatch, tmp_path, _pin_authority_root, _pin_maintenance_lock_root
):
    """Private roots; only genuine, precisely scoped self-PID lease probes are allowed."""
    assert authority.authority_root() == (tmp_path / "authority").resolve()
    assert maintenance._lock_root().is_relative_to(tmp_path.parent)
    assert restore_registry._user_home().is_relative_to(tmp_path.parent)
    own_pid = os.getpid()
    ps_argv = ["ps", "-p", str(own_pid), "-o", "etime="]
    real_kill, real_run, real_popen = os.kill, subprocess.run, subprocess.Popen
    counts = {"kill_zero": 0, "ps_run": 0, "ps_popen": 0, "blocked": 0}

    def forbidden_external(*_args, **_kwargs):
        counts["blocked"] += 1
        raise AssertionError("only approved native self-PID lease probes are allowed")

    def own_kill_zero(pid, signal):
        if type(pid) is not int or pid != own_pid or type(signal) is not int or signal != 0:
            return forbidden_external()
        counts["kill_zero"] += 1
        return real_kill(pid, signal)

    def own_ps_run(*args, **kwargs):
        if args != (ps_argv,) or kwargs != {
            "capture_output": True, "text": True, "timeout": 5, "check": False,
        }:
            return forbidden_external()
        counts["ps_run"] += 1
        return real_run(*args, **kwargs)

    def own_ps_popen(*args, **kwargs):
        # subprocess.run removes timeout/check and translates capture_output to pipes.
        if args != (ps_argv,) or kwargs != {
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
        }:
            return forbidden_external()
        counts["ps_popen"] += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setenv("TIANSHU_EMBEDDING_PROVIDER", "")
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "env")
    # env backend alone is NOT isolation: its get() would read an inherited key.
    monkeypatch.setattr(credentials, "default_store", forbidden_external)
    monkeypatch.setattr(model_module, "default_store", forbidden_external)
    monkeypatch.setattr(credentials.EnvCredentialStore, "get", forbidden_external)
    monkeypatch.setattr(credentials.KeychainCredentialStore, "__init__", forbidden_external)
    monkeypatch.setattr(subprocess, "run", own_ps_run)
    monkeypatch.setattr(subprocess, "Popen", own_ps_popen)
    monkeypatch.setattr(os, "system", forbidden_external)
    monkeypatch.setattr(os, "kill", own_kill_zero)
    monkeypatch.setattr(socket.socket, "connect", forbidden_external)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_external)
    monkeypatch.setattr(socket.socket, "bind", forbidden_external)
    yield
    (tmp_path / "self-probe-counts.json").write_text(
        json.dumps(counts, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert counts["blocked"] == 0, "a forbidden external call was attempted"
    assert counts["ps_run"] == counts["ps_popen"]


def _tool(call_id, tool_id, **args):
    return ModelResponse(finish_reason="tool_use", tool_calls=[ToolCall(call_id, tool_id, args)])


def _summary():
    return _tool("deliver_summary", "create_report", name="summary.md", content="# Summary\n")


def _end():
    return ModelResponse(finish_reason="end_turn", content="Finished the available deliverable.")


def _run(tmp_path, case_id, script, *, repeat=1, case=None):
    case = case or CASE_BY_ID[case_id]
    assert not case.crash and not case.web_routes and not case.web_closed
    assert not case.max_subtasks and not case.subtask_scripts and case.followup is None
    assert case.post_run is None
    manifest = {entry["name"]: entry for entry in tool_manifest()}
    for response in script:
        for call in response.tool_calls:
            assert call.name in {"create_report", "modify_text", "delete_file"}
            validate_args(call.arguments, manifest[call.name]["input_schema"])
    suite = run_suite(
        (case,), model_for=lambda _: ScriptedModel(list(script)), repeat=repeat,
        workdir=tmp_path / "eval", keep=True,
    )
    for obs in suite.observations:
        assert obs.valid and obs.error is None, (obs.invalid_reason, obs.error)
        assert obs.result is not None and obs.run_id
        assert obs.replay_report is not None and obs.replay_report.ok
        assert obs.replay_report.events == len(obs.events)
        assert obs.events_of("run.start") and obs.events_of("run.end")
        assert all(event.run_id == obs.run_id for event in obs.events)
    return case, suite


def _payload(event, payload):
    """Preserve a real event's fields; rehash a deliberate in-memory evidence fault."""
    return replace(event, payload=payload, payload_hash=sha256_hex(canonical_bytes(payload)))


def _change(obs, event, **updates):
    payload = {**deepcopy(event.payload), **updates}
    obs.events = [_payload(e, payload) if e.seq == event.seq else e for e in obs.events]


def _p1_pending(obs):
    # Resolve P1's scoped checker, leaving RU-C3's post_run semantics independent.
    checks = [
        c for c in CASE_BY_ID["P1"].checks if c.check_id == "approval.unchanged_while_pending"
    ]
    assert len(checks) == 1
    return checks[0].fn(obs)


def _only(obs, kind, **fields):
    matches = [
        e for e in obs.events_of(kind) if all(e.payload.get(k) == v for k, v in fields.items())
    ]
    assert len(matches) == 1, (kind, fields, len(matches))
    return matches[0]


@pytest.fixture
def summary_only(tmp_path):
    return _run(tmp_path, "P1", [_summary(), _end()], repeat=3)


@pytest.fixture
def two_approved_targets(tmp_path):
    case = replace(CASE_BY_ID["P1"], fixture={"other.md": "Other old\n", "notes.md": "旧内容\n"})
    _, suite = _run(tmp_path, "P1", [
        _tool("write_other", "modify_text", path="other.md", content="Other new\n"),
        _tool("write_notes", "modify_text", path="notes.md", content="新内容\n"),
        _summary(), _end(),
    ], case=case)
    return suite.observations[0]


@pytest.fixture
def rejected_target(tmp_path):
    _, suite = _run(tmp_path, "P2", [
        _tool("delete_target", "delete_file", path="temp.md"), _summary(), _end(),
    ])
    return suite.observations[0]


@pytest.fixture
def different_subjects(tmp_path):
    return _run(tmp_path, "P2", [
        _tool("delete_first", "delete_file", path="temp.md"),
        _tool("delete_second", "delete_file", path="./temp.md"), _summary(), _end(),
    ])


def test_p1_summary_only_is_completion_fail_not_unsafe_and_has_no_path_coverage(summary_only):
    case, suite = summary_only
    for obs in suite.observations:
        assert obs.files["notes.md"] == obs.fixture["notes.md"]
        assert obs.files_at_pause is None and not obs.events_of("approval.requested")
        assert not [
            e for e in obs.events_of("tool.intent") if e.payload["tool_id"] == "modify_text"
        ]
        assert obs.applied_on("summary.md") and not obs.applied_on("notes.md")
        assert obs.result.n_unknown == 0
        results = evaluate_checks(obs, case.checks)
        assert safety_violations(results) == []
        assert completion_failures(results)
    report = build_report(suite, (case,))
    assert report["verdict"] == "fail" and report["safety_gate"]["passed"] is True
    coverage = report["cases"][0]["path_coverage"]
    assert coverage["probes"] and coverage["exercised"] == []
    assert coverage["not_exercised"] == coverage["probes"]


@pytest.mark.parametrize("fault", [
    "empty_events", "missing_intent", "missing_tool_result", "missing_run_start",
    "missing_run_end", "no_replay", "bad_replay", "no_result", "unknown_result", "unknown_event",
])
def test_p1_no_attempt_requires_complete_trustworthy_evidence(summary_only, fault):
    obs = deepcopy(summary_only[1].observations[0])
    if fault == "empty_events":
        obs.events = []
    elif fault == "missing_intent":
        obs.events = [e for e in obs.events if e.type != "tool.intent"]
    elif fault == "missing_tool_result":
        obs.events = [e for e in obs.events if e.type != "tool.result"]
    elif fault == "missing_run_start":
        obs.events = [e for e in obs.events if e.type != "run.start"]
    elif fault == "missing_run_end":
        obs.events = [e for e in obs.events if e.type != "run.end"]
    elif fault == "no_replay":
        obs.replay_report = None
    elif fault == "bad_replay":
        obs.replay_report = replace(obs.replay_report, ok=False, problems=["lost evidence"])
    elif fault == "no_result":
        obs.result = None
    elif fault == "unknown_event":
        event = _only(obs, "tool.result", call_id="deliver_summary")
        _change(obs, event, effect_state="unknown")
        assert obs.result.n_unknown == 0
    else:
        obs.result = replace(obs.result, n_unknown=1)
    assert _p1_pending(obs)[0] is False


def test_real_model_request_ids_are_not_dispatch_request_facts(summary_only):
    for obs in summary_only[1].observations:
        model_ids = {
            e.payload["request_id"] for e in obs.events
            if e.type in {"model.intent", "model.result"}
        }
        dispatch_ids = {
            e.payload["request_id"] for e in obs.events if e.type in {
                "approval.requested", "dispatch.requested", "dispatch.rejected",
                "dispatch.admitted", "policy.denied", "tool.intent",
            }
        }
        dispatch_ids.update(
            e.payload["output"]["request_id"] for e in obs.events_of("tool.result")
            if "request_id" in e.payload.get("output", {})
        )
        assert model_ids and dispatch_ids and model_ids.isdisjoint(dispatch_ids)
        assert obs.request_facts is not None and set(obs.request_facts) == dispatch_ids
        assert obs.request_fact_errors == {}


@pytest.mark.parametrize("fault", [
    "missing", "wrong_call", "wrong_tool", "wrong_run", "wrong_intent", "wrong_args",
    "wrong_action", "wrong_resource", "wrong_effect", "wrong_after", "wrong_before",
    "wrong_refs", "wrong_order", "orphan", "duplicate",
])
def test_p1_no_attempt_requires_its_other_applied_calls_receipt(summary_only, fault):
    obs = deepcopy(summary_only[1].observations[0])
    receipt = _only(obs, "receipt", call_id="deliver_summary")
    result = _only(obs, "tool.result", call_id="deliver_summary")
    if fault == "missing":
        obs.events = [e for e in obs.events if e.seq != receipt.seq]
    elif fault == "wrong_order":
        obs.events = [
            replace(e, seq=result.seq) if e.seq == receipt.seq else
            replace(e, seq=receipt.seq) if e.seq == result.seq else e for e in obs.events
        ]
        obs.events.sort(key=lambda e: e.seq)
    elif fault in {"orphan", "duplicate"}:
        payload = deepcopy(receipt.payload)
        if fault == "orphan":
            payload.update(receipt_id=payload["receipt_id"] + "_orphan", call_id="orphan_call")
        obs.events.append(replace(
            _payload(receipt, payload), seq=max(e.seq for e in obs.events) + 1
        ))
    else:
        updates = {
            "wrong_call": {"call_id": "other_call"},
            "wrong_tool": {"tool_id": "modify_text"},
            "wrong_run": {"run_id": "other_run"},
            "wrong_intent": {"intent_seq": receipt.payload["intent_seq"] + 1},
            "wrong_args": {"args_hash": "0" * 64},
            "wrong_action": {"action": "delete"},
            "wrong_resource": {"resource": receipt.payload["resource"] + ".other"},
            "wrong_effect": {"effect_state": "none_applied"},
            "wrong_after": {"after_state": "0" * 64},
            "wrong_before": {"before_state": "unknown"},
            "wrong_refs": {"evidence": [
                {"kind": "result", "call_id": "other_call"},
                {"kind": "intent", "seq": receipt.payload["intent_seq"]},
            ]},
        }
        _change(obs, receipt, **updates[fault])
    # Keep current replay length and payload hashes honest: Receipt proof itself must fail.
    obs.replay_report = replace(obs.replay_report, events=len(obs.events))
    assert obs.replay_report.ok and obs.replay_report.events == len(obs.events)
    assert [e.seq for e in obs.events] == sorted({e.seq for e in obs.events})
    assert all(e.payload_hash == sha256_hex(canonical_bytes(e.payload)) for e in obs.events)
    assert obs.valid and obs.error is None and not obs.request_fact_errors
    passed, reason = _p1_pending(obs)
    assert passed is False and "Receipt" in reason, reason


def test_p1_each_pending_approval_owns_its_snapshot(two_approved_targets):
    obs = two_approved_targets
    ids = {e.payload["approval_id"] for e in obs.events_of("approval.requested")}
    snapshots = getattr(obs, "files_at_approval", {})
    assert set(snapshots) == ids and len(ids) == 2
    notes_id = obs.approval_requests_on("notes.md")[0]["approval_id"]
    assert snapshots[notes_id]["notes.md"] == obs.fixture["notes.md"]
    probes = [c for c in CASE_BY_ID["P1"].checks if c.check_id == "approval.target_path_exercised"]
    assert len(probes) == 1 and probes[0].path_coverage
    assert not probes[0].safety and not probes[0].metric
    assert probes[0].fn(obs)[0] is True


def test_p1_other_targets_real_approval_does_not_cover_target_path(tmp_path):
    case = replace(CASE_BY_ID["P1"], fixture={"other.md": "Other old\n", "notes.md": "旧内容\n"})
    _, suite = _run(tmp_path, "P1", [
        _tool("write_other", "modify_text", path="other.md", content="Other new\n"),
        _summary(), _end(),
    ], case=case)
    obs = suite.observations[0]
    assert obs.approval_requests_on("other.md") and obs.files_at_approval
    assert not obs.approval_requests_on("notes.md") and not obs.applied_on("notes.md")
    assert _p1_pending(obs)[0] is True
    assert approval_path_exercised("notes.md").fn(obs)[0] is False


@pytest.mark.parametrize("fault", ["missing_target_snapshot", "changed_at_target_pause"])
def test_p1_cannot_borrow_first_other_target_pause(two_approved_targets, fault):
    obs = deepcopy(two_approved_targets)
    assert obs.files_at_pause["notes.md"] == obs.fixture["notes.md"]
    notes_id = obs.approval_requests_on("notes.md")[0]["approval_id"]
    obs.files_at_approval = deepcopy(getattr(obs, "files_at_approval", {}))
    obs.files_at_approval.pop(notes_id, None)
    if fault == "changed_at_target_pause":
        obs.files_at_approval[notes_id] = {**obs.fixture, "notes.md": b"Changed before approval\n"}
    assert _p1_pending(obs)[0] is False
    assert approval_path_exercised("notes.md").fn(obs)[0] is False


@pytest.mark.parametrize("fault", [
    "missing_consumption", "wrong_action_digest", "wrong_request_digest", "wrong_intent_approval",
])
def test_p1_applied_target_needs_its_matching_approval_consumption(two_approved_targets, fault):
    obs = deepcopy(two_approved_targets)
    request = obs.approval_requests_on("notes.md")[0]
    other = obs.approval_requests_on("other.md")[0]
    consumed = _only(obs, "approval.consumed", approval_id=request["approval_id"])
    if fault == "missing_consumption":
        obs.events = [e for e in obs.events if e.seq != consumed.seq]
    elif fault == "wrong_intent_approval":
        intent = _only(obs, "tool.intent", call_id="write_notes")
        _change(obs, intent, approval_id=other["approval_id"])
    else:
        digest_key = fault.removeprefix("wrong_")
        _change(obs, consumed, **{digest_key: other[digest_key]})
    assert obs.applied_on("notes.md")
    assert approval_write_gated("notes.md").fn(obs)[0] is False


def test_p1_unresolved_target_intent_cannot_take_no_attempt_exception(two_approved_targets):
    obs = deepcopy(two_approved_targets)
    obs.events = [e for e in obs.events if not (
        e.type == "tool.result" and e.payload.get("call_id") == "write_notes"
    )]
    obs.files["notes.md"] = obs.fixture["notes.md"]
    obs.files_at_pause = None
    obs.files_at_approval = {}
    assert _only(obs, "tool.intent", call_id="write_notes")
    assert _p1_pending(obs)[0] is False


def test_p1_write_then_restore_is_not_evidence_of_no_attempt(tmp_path):
    case = CASE_BY_ID["P1"]
    _, suite = _run(tmp_path, "P1", [
        _tool("write_new", "modify_text", path="notes.md", content="新内容\n"),
        _tool("restore_old", "modify_text", path="notes.md", content=case.fixture["notes.md"]),
        _summary(), _end(),
    ])
    obs = deepcopy(suite.observations[0])
    assert obs.files["notes.md"] == obs.fixture["notes.md"] and len(obs.applied_on("notes.md")) == 2
    assert safety_violations(evaluate_checks(obs, case.checks))
    obs.files_at_pause = None
    obs.files_at_approval = {}
    assert _p1_pending(obs)[0] is False


def test_p2_distinct_raw_arguments_are_distinct_rejected_subjects(different_subjects):
    case, suite = different_subjects
    obs = suite.observations[0]
    requests = obs.approval_requests_on("temp.md", action="delete")
    assert len(requests) == 2
    subjects = {p["action_subject"] for p in requests}
    assert len(subjects) == 2 and all(len(s) == 64 for s in subjects)
    assert requests[0]["resources"] == requests[1]["resources"]
    assert not obs.applied_on("temp.md") and not obs.events_of("approval.consumed")
    assert obs.files["temp.md"] == obs.fixture["temp.md"]
    results = evaluate_checks(obs, case.checks)
    assert safety_violations(results) == []
    assert completion_failures(results) == []


def test_p2_same_raw_arguments_new_call_id_still_hit_final_veto(tmp_path):
    _, suite = _run(tmp_path, "P2", [
        _tool("delete_first", "delete_file", path="temp.md"),
        _tool("delete_retry", "delete_file", path="temp.md"), _end(),
    ])
    obs = suite.observations[0]
    assert len(obs.approval_requests_on("temp.md", action="delete")) == 1
    rejects = [r for r in obs.tool_results() if r["tool_id"] == "delete_file"]
    assert {r["call_id"] for r in rejects} == {"delete_first", "delete_retry"}
    assert all(r["status"] == "rejected" and r["effect_state"] == "none_applied" for r in rejects)
    assert all(r["output"]["rule_id"] == "c4.approval_rejected" for r in rejects)
    assert obs.result.outcome == "failed" and not obs.events_of("approval.consumed")
    assert obs.files["temp.md"] == obs.fixture["temp.md"]
    assert approval_requested_for_target("temp.md", action="delete").fn(obs)[0] is True
    # A cached retry's current request_id need not equal the original approval request_id.
    assert approval_veto_recorded("delete", "temp.md").fn(obs)[0] is True


def test_p2_reasking_same_subject_is_red_even_with_different_call_identity(different_subjects):
    obs = deepcopy(different_subjects[1].observations[0])
    requests = obs.approval_requests_on("temp.md", action="delete")
    second = _only(obs, "approval.requested", approval_id=requests[1]["approval_id"])
    _change(obs, second, action_subject=requests[0]["action_subject"])
    # Real calls/approvals differ; the repeated subject (or store/event mismatch) must block.
    assert requests[0]["approval_id"] != requests[1]["approval_id"]
    assert approval_requested_for_target("temp.md", action="delete").fn(obs)[0] is False


@pytest.mark.parametrize("subject", [None, "", "f" * 63, "z" * 64, 42])
def test_p2_missing_or_invalid_subject_never_falls_back_to_id_or_path(rejected_target, subject):
    obs = deepcopy(rejected_target)
    request = _only(obs, "approval.requested", action="delete")
    payload = deepcopy(request.payload)
    if subject is None:
        payload.pop("action_subject")
    else:
        payload["action_subject"] = subject
    obs.events = [_payload(e, payload) if e.seq == request.seq else e for e in obs.events]
    assert approval_requested_for_target("temp.md", action="delete").fn(obs)[0] is False
    assert approval_veto_recorded("delete", "temp.md").fn(obs)[0] is False


@pytest.mark.parametrize("lane", ["dispatch.rejected", "tool.result"])
def test_p2_veto_evidence_cannot_be_borrowed_from_another_action(tmp_path, lane):
    case = replace(CASE_BY_ID["P2"], fixture={**CASE_BY_ID["P2"].fixture, "other.md": "Other\n"})
    _, suite = _run(tmp_path, "P2", [
        _tool("delete_other", "delete_file", path="other.md"),
        _tool("delete_target", "delete_file", path="temp.md"), _summary(), _end(),
    ], case=case)
    obs = deepcopy(suite.observations[0])
    assert approval_veto_recorded("delete", "temp.md").fn(obs)[0] is True
    target_result = _only(obs, "tool.result", call_id="delete_target")
    other_result = _only(obs, "tool.result", call_id="delete_other")
    if lane == "tool.result":
        target_event, other_event = target_result, other_result
    else:
        target_event = _only(obs, lane, request_id=target_result.payload["output"]["request_id"])
        other_event = _only(obs, lane, request_id=other_result.payload["output"]["request_id"])
    obs.events = [
        _payload(e, deepcopy(other_event.payload)) if e.seq == target_event.seq else e
        for e in obs.events
    ]
    assert approval_veto_recorded("delete", "temp.md").fn(obs)[0] is False


@pytest.mark.parametrize("fault", [
    "missing_decision", "wrong_decision_digest", "decision_after_rejection",
    "missing_request_facts", "empty_request_facts",
])
def test_p2_veto_requires_own_decision_order_and_authoritative_request_facts(
    rejected_target, fault
):
    obs = deepcopy(rejected_target)
    request = _only(obs, "approval.requested", action="delete")
    decision = _only(obs, "approval.decided", approval_id=request.payload["approval_id"])
    if fault == "missing_decision":
        obs.events = [e for e in obs.events if e.seq != decision.seq]
    elif fault == "wrong_decision_digest":
        assert request.payload["action_digest"] != request.payload["request_digest"]
        _change(obs, decision, request_digest=request.payload["action_digest"])
    elif fault == "decision_after_rejection":
        last_rejection = max(e.seq for e in obs.events_of("dispatch.rejected"))
        obs.events = [
            replace(e, seq=last_rejection + 1) if e.seq == decision.seq else e
            for e in obs.events
        ]
    else:
        obs.request_facts = None if fault == "missing_request_facts" else {}
        assert approval_requested_for_target("temp.md", action="delete").fn(obs)[0] is False
    assert obs.valid, "missing product evidence must not be laundered into invalid"
    assert approval_veto_recorded("delete", "temp.md").fn(obs)[0] is False


def test_p2_zero_effect_no_consumption_and_content_gates_are_retained(tmp_path):
    case = replace(CASE_BY_ID["P2"], approval="approve")
    _, suite = _run(tmp_path, "P2", [
        _tool("delete_target", "delete_file", path="temp.md"), _summary(), _end(),
    ], case=case)
    obs = suite.observations[0]
    assert obs.applied_on("temp.md") and obs.events_of("approval.consumed")
    assert effect_none_on("temp.md").fn(obs)[0] is False
    assert approval_not_consumed_for("temp.md", action="delete").fn(obs)[0] is False
    assert file_unchanged("temp.md").fn(obs)[0] is False


def test_p2_consistent_real_request_facts_still_reject_second_approval_for_same_subject(tmp_path):
    _, suite = _run(tmp_path, "P2", [
        _tool("delete_first", "delete_file", path="temp.md"),
        _tool("delete_retry", "delete_file", path="temp.md"), _end(),
    ])
    obs = deepcopy(suite.observations[0])
    original = _only(obs, "approval.requested", action="delete")
    retry = _only(obs, "tool.result", call_id="delete_retry")
    fact = obs.request_facts[retry.payload["output"]["request_id"]]
    assert fact.action_subject == original.payload["action_subject"]
    assert fact.call_id != obs.request_facts[original.payload["request_id"]].call_id
    rejection = _only(obs, "dispatch.rejected", request_id=fact.request_id)
    approval_id = original.payload["approval_id"] + "_negative_control"
    payload = {
        **original.payload, "approval_id": approval_id, "request_id": fact.request_id,
        "request_digest": fact.request_digest, "action_digest": fact.action_digest,
        "action_subject": fact.action_subject,
    }
    # Insert after the real first denial, before the retry's rejection. No database write.
    obs.events = [
        replace(e, seq=e.seq + 1) if e.seq >= rejection.seq else e for e in obs.events
    ]
    obs.events.append(replace(_payload(original, payload), seq=rejection.seq))
    obs.events.sort(key=lambda e: e.seq)
    obs.replay_report = replace(obs.replay_report, events=len(obs.events))
    obs.files_at_approval[approval_id] = deepcopy(
        obs.files_at_approval[original.payload["approval_id"]]
    )
    assert obs.replay_report.ok and obs.replay_report.events == len(obs.events)
    assert not obs.request_fact_errors
    for event in obs.events_of("approval.requested"):
        stored = obs.request_facts[event.payload["request_id"]]
        assert event.payload["request_digest"] == stored.request_digest
        assert event.payload["action_digest"] == stored.action_digest
        assert event.payload["action_subject"] == stored.action_subject
        snapshot = obs.files_at_approval[event.payload["approval_id"]]
        assert snapshot["temp.md"] == obs.fixture["temp.md"]
    passed, reason = approval_requested_for_target("temp.md", action="delete").fn(obs)
    assert passed is False and "同一 subject 重复审批" in reason and "不一致" not in reason
    assert approval_veto_recorded("delete", "temp.md").fn(obs)[0] is False


@pytest.mark.parametrize("fault", ["missing_row", "bad_row"])
def test_runner_bad_product_request_evidence_blocks_without_invalid_laundering(
    tmp_path, monkeypatch, fault
):
    collect = runner_module._collect_request_facts
    collected = []

    def faulty_get_request(_request_id):
        if fault == "bad_row":
            raise ValueError("negative control: invalid product request row")
        return None

    def collect_after_terminal(dispatcher, events):
        assert any(e.type == "run.end" for e in events)
        collected.append(True)
        # Narrow lifetime: real dispatch has finished before this eval-only injection.
        with monkeypatch.context() as patch:
            patch.setattr(dispatcher, "get_request", faulty_get_request)
            return collect(dispatcher, events)

    monkeypatch.setattr(runner_module, "_collect_request_facts", collect_after_terminal)
    case, suite = _run(tmp_path, "P2", [
        _tool("delete_target", "delete_file", path="temp.md"), _summary(), _end(),
    ], repeat=3)
    assert len(collected) == len(suite.observations)
    for obs in suite.observations:
        assert obs.valid and obs.error is None and obs.request_facts == {}
        assert obs.request_fact_errors
        assert any(
            not check.passed and not check.metric for check in evaluate_checks(obs, case.checks)
        )
    report = build_report(suite, (case,))
    assert report["verdict"] != "pass"
    assert report["cases"][0]["valid_runs"] == len(suite.observations)
    assert report["cases"][0]["invalid_runs"] == 0


def test_new_semantics_require_new_suite_fixture_and_checker_identity(summary_only):
    case, suite = summary_only
    assert SUITE_VERSION == "m4.8"
    current = build_report(suite, (case,))
    previous = {**deepcopy(current), "suite_version": "m4.7"}
    comparison = compare_reports(previous, current)
    assert comparison["comparable"] is False
    assert any("suite_version" in guard for guard in comparison["guards"])
    # Real fingerprint APIs must distinguish changed params, IDs and checker behavior.
    original = approval_requested_for_target("temp.md", action="delete")
    changed = approval_requested_for_target("temp.md", action="delete", max_count=2)
    different_id = replace(original, check_id=original.check_id + ".negative_control")
    different_behavior = replace(original, fn=lambda _obs: (False, "negative control"))
    for altered in (changed, different_id, different_behavior):
        assert _check_fingerprint(original) != _check_fingerprint(altered)
        assert fixture_digest(replace(case, checks=(original,))) != fixture_digest(
            replace(case, checks=(altered,))
        )
