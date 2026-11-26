"""有限授权仍经真实 Dispatcher/Grant/执行守卫；规则不能替代具体动作票据。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_dispatch import _env, _overwrite_request

from tianshu.consent import (
    ConsentBindingV1,
    ConsentError,
    ConsentStore,
    command_matcher,
    directory_matcher,
    tool_binding,
    workspace_binding_digest,
)
from tianshu.dispatcher import Dispatcher, DispatchError, build_request
from tianshu.errors import TianshuError
from tianshu.permissions import HumanIdentity
from tianshu.workspace import ResourceIdentity


@pytest.fixture
def env(tmp_path):
    e = _env(tmp_path)
    task, run, claim = e.new_run()
    now = [1_000_000]
    mono = [20_000]
    binding = [ConsentBindingV1(
        task.task_id, run.run_id, run.goal_revision,
        workspace_binding_digest(task.workspaces), "a" * 64, "b" * 64,
    )]
    store = ConsentStore(
        e.log, tasks=e.tasks, data_root=e.dispatcher._data_root,
        runtime_session_id="session-a", binding_resolver=lambda _: binding[0],
        clock=lambda: now[0], monotonic=lambda: mono[0],
    )
    # 原有租约和 Grant 继续使用真实墙钟；Scope 的独立时钟只控制自身期限。
    e.dispatcher = Dispatcher(
        e.log, e.tasks, e.leases, e.dispatcher._policy, e.approvals,
        grants=e.grants, claims=e.claims, snapshots=e.snapshots,
        data_root=e.dispatcher._data_root, consents=store,
    )
    e.consent, e.binding, e.now, e.mono = store, binding, now, mono
    e.task, e.run, e.claim = task, run, claim
    yield e
    e.log.close()


def _issue(e, *, uses=2, matcher=None, request_id="human-issue"):
    return e.consent.issue({
        "format_version": 1, "consent_id": "scope-" + request_id,
        **e.binding[0].to_dict(), "action": "overwrite",
        "tool_binding": tool_binding(e.dispatcher.registry.spec("modify_text")),
        "matcher": matcher or directory_matcher(
            e.ws, recursive=False, max_file_bytes=100, max_total_bytes=150,
        ),
        "max_uses": uses, "expires_at": e.now[0] + 600_000,
        "runtime_session_id": "session-a",
        "issued_by": HumanIdentity.local_cli().to_dict(), "request_id": request_id,
    }, actor=HumanIdentity.local_cli())


def _request(e, *, call="c1", text="new"):
    return _overwrite_request(
        e, e.task, e.run, e.claim, call_id=call, text=text,
    )


def _prepare(e, **kw):
    request, args = _request(e, **kw)
    return request, args, e.dispatcher.prepare(request, e.claim, args=args)


def test_scope_produces_exact_grant_and_original_intent_before_effect(env):
    _issue(env, uses=1)
    request, _, decision = _prepare(env)
    assert decision.kind == "admitted"
    assert not decision.approval_id
    result = env.dispatcher.dispatch(request.request_id, env.claim)
    assert result.effect_state == "applied"
    assert (env.ws / "note.md").read_bytes() == b"new"
    events = env.log.read_run(env.run.run_id)
    types = [event.type for event in events]
    assert types.index("consent.used") < types.index("grant.issued")
    assert types.index("grant.consumed") < types.index("tool.intent")
    assert types.index("tool.intent") < types.index("execution.started")
    row = env.dispatcher.dispatch_record(env.run.run_id, request.call_id)
    grant = env.grants.get(row.grant_id)
    assert grant.issuer == "consent:scope-human-issue"
    assert grant.request_digest == request.digest()
    assert grant.remaining_uses == 0
    assert env.consent.list(run_id=env.run.run_id)[0]["remaining_uses"] == 0
    with pytest.raises(DispatchError, match="已经派发"):
        env.dispatcher.dispatch(request.request_id, env.claim)
    assert sum(event.type == "consent.used" for event in env.log.read_run(env.run.run_id)) == 1


def test_count_and_total_bytes_apply_to_future_content(env):
    _issue(env, uses=2)
    first, _, decision = _prepare(env, text="a" * 90)
    assert decision.kind == "admitted"
    assert env.dispatcher.dispatch(first.request_id, env.claim).effect_state == "applied"
    # 单文件未超限，累计即将超过 150 字节，只能重新逐次审批。
    _, _, decision = _prepare(env, call="c2", text="b" * 70)
    assert decision.kind == "waiting"
    second, _, decision = _prepare(env, call="c3", text="c" * 60)
    assert decision.kind == "admitted"
    assert env.dispatcher.dispatch(second.request_id, env.claim).effect_state == "applied"
    _, _, decision = _prepare(env, call="c4", text="d")
    assert decision.kind == "waiting"


@pytest.mark.parametrize("change", ["plan", "mission", "workspace", "run"])
def test_current_binding_must_match_and_never_cross_rounds(env, change):
    _issue(env)
    updates = {
        "plan": {"plan_digest": "c" * 64},
        "mission": {"mission_digest": "c" * 64},
        "workspace": {"workspace_binding_digest": "c" * 64},
        "run": {"run_id": "next-round"},
    }
    env.binding[0] = replace(env.binding[0], **updates[change])
    _, _, decision = _prepare(env)
    assert decision.kind == "waiting"


def test_missing_mission_plan_binding_is_closed(env):
    env.consent._binding_resolver = lambda _: None
    with pytest.raises(ConsentError, match="绑定"):
        _issue(env)


def test_monotonic_expiry_survives_wall_clock_rollback(env):
    _issue(env)
    request, _, decision = _prepare(env)
    assert decision.kind == "admitted"
    env.now[0] -= 50_000
    env.mono[0] += 600_001
    result = env.dispatcher.dispatch(request.request_id, env.claim)
    assert result.effect_state == "none_applied"
    assert (env.ws / "note.md").read_bytes() != b"new"


def test_revoked_prepared_request_cannot_select_another_scope(env):
    first = _issue(env)
    request, _, decision = _prepare(env)
    assert decision.kind == "admitted"
    _issue(env, request_id="second")
    env.consent.revoke(
        first.consent_id, actor=HumanIdentity.local_cli(), request_id="human-revoke",
    )
    result = env.dispatcher.dispatch(request.request_id, env.claim)
    assert result.effect_state == "none_applied"
    assert not [e for e in env.log.read_run(env.run.run_id) if e.type == "consent.used"]
    assert (env.ws / "note.md").read_bytes() != b"new"


def test_human_rejection_wins_over_matching_scope(env):
    request, args = _request(env)
    decision = env.dispatcher.prepare(request, env.claim, args=args)
    env.approvals.decide(
        decision.approval_id, decision="rejected", actor=HumanIdentity.local_cli(),
    )
    _issue(env)
    _, _, decision = _prepare(env, call="new-id")
    assert decision.kind == "rejected"
    assert decision.rule_id == "c4.approval_rejected"


def test_runtime_restart_expires_scope_and_records_fact(env):
    _issue(env)
    other = ConsentStore(
        env.log, tasks=env.tasks, data_root=env.dispatcher._data_root,
        runtime_session_id="session-new", binding_resolver=lambda _: env.binding[0],
        clock=lambda: env.now[0], monotonic=lambda: env.mono[0],
    )
    assert other.list(run_id=env.run.run_id)[0]["state"] == "expired"
    assert [e.type for e in env.log.read_run(env.run.run_id)].count("consent.expired") == 1
    _, _, decision = _prepare(env)
    assert decision.kind == "waiting"


def _rebuild(e, *, runtime="session-a", drop=False):
    if drop:
        with e.log.transaction():
            e.log.conn.execute("DROP TABLE consent_selections")
            e.log.conn.execute("DROP TABLE scoped_consents")
    e.tasks.rebuild_from_events()
    return ConsentStore(
        e.log, tasks=e.tasks, data_root=e.dispatcher._data_root,
        runtime_session_id=runtime, binding_resolver=lambda _: e.binding[0],
        clock=lambda: e.now[0], monotonic=lambda: e.mono[0],
    )


def test_consumed_scope_rebuild_restores_quota_and_used_selection(env):
    scope = _issue(env, uses=2)
    request, args, decision = _prepare(env)
    assert decision.kind == "admitted"
    assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "applied"
    before = env.log.seq_head()
    rebuilt = _rebuild(env, drop=True)
    rows = rebuilt.list(run_id=env.run.run_id)
    assert len(rows) == 1
    assert rows[0]["consent_id"] == scope.consent_id
    assert rows[0]["remaining_uses"] == 1 and rows[0]["used_bytes"] == 3
    assert rebuilt.selected(request) == scope.consent_id
    assert env.log.conn.execute(
        "SELECT used FROM consent_selections WHERE request_id=?", (request.request_id,),
    ).fetchone()["used"] == 1
    with pytest.raises(ConsentError), env.log.transaction():
        rebuilt.consume(request, args=args)
    assert _rebuild(env).list(run_id=env.run.run_id) == rows
    assert env.log.seq_head() == before


@pytest.mark.parametrize("terminal", ["revoked", "expired", "exhausted"])
def test_rebuild_never_revives_terminal_or_consumed_scope(env, terminal):
    scope = _issue(env, uses=1)
    if terminal == "revoked":
        env.consent.revoke(scope.consent_id, actor=HumanIdentity.local_cli(), request_id="revoke")
    elif terminal == "expired":
        _rebuild(env, runtime="other-session")
    else:
        request, _, _ = _prepare(env)
        assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "applied"
    rebuilt = _rebuild(env, drop=True)
    row = rebuilt.list(run_id=env.run.run_id)[0]
    if terminal != "exhausted":
        assert row["state"] == terminal
    assert row["remaining_uses"] == (0 if terminal == "exhausted" else 1)
    request, args = _request(env, call="after-rebuild")
    assert rebuilt.select(request, args=args) is None


def test_rebuild_preserves_original_monotonic_deadline(env):
    _issue(env)
    env.now[0] -= 50_000
    env.mono[0] += 599_999
    rebuilt = _rebuild(env)
    request, args = _request(env, call="before-monotonic-expiry")
    assert rebuilt.select(request, args=args) == "scope-human-issue"
    env.mono[0] += 2
    assert _rebuild(env).list(run_id=env.run.run_id)[0]["state"] == "expired"


def test_missing_projection_cannot_restart_scope_ttl_after_wall_rollback(env):
    _issue(env)
    env.now[0] -= 50_000
    env.mono[0] += 600_001
    rebuilt = _rebuild(env, drop=True)
    assert rebuilt.list(run_id=env.run.run_id)[0]["state"] == "expired"
    request, args = _request(env, call="past-original-ttl")
    assert rebuilt.select(request, args=args) is None


def test_lost_prepared_selection_cannot_choose_another_scope_after_rebuild(env):
    first = _issue(env)
    request, args, decision = _prepare(env)
    assert decision.kind == "admitted"
    _issue(env, request_id="replacement")
    env.consent.revoke(first.consent_id, actor=HumanIdentity.local_cli(), request_id="revoke")
    env.log.conn.execute("DELETE FROM consent_selections")
    rebuilt = _rebuild(env)
    with pytest.raises(ConsentError, match="来源"):
        rebuilt.select(request, args=args)
    assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "none_applied"
    assert not env.log.read_types(("consent.used",))


def test_rebuild_replaces_refilled_projection_with_consumed_facts(env):
    _issue(env, uses=2)
    request, _, _ = _prepare(env)
    assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "applied"
    with env.log.transaction():
        env.log.conn.execute("UPDATE scoped_consents SET remaining_uses=2,used_bytes=0")
        env.log.conn.execute("UPDATE consent_selections SET used=0")
    row = _rebuild(env).list(run_id=env.run.run_id)[0]
    assert row["remaining_uses"] == 1 and row["used_bytes"] == 3
    assert env.log.conn.execute(
        "SELECT used FROM consent_selections WHERE request_id=?", (request.request_id,),
    ).fetchone()["used"] == 1


@pytest.mark.parametrize("change", ["remaining", "bytes", "task", "duplicate"])
def test_rebuild_rejects_inconsistent_consumption_history(env, change):
    _issue(env, uses=2)
    request, _, _ = _prepare(env)
    assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "applied"
    used = env.log.read_types(("consent.used",))[0]
    payload = {**used.payload, "request_id": "another-request", "remaining_uses": 0}
    task_id = used.task_id
    if change == "remaining":
        payload["remaining_uses"] = 2
    elif change == "bytes":
        payload["bytes"] = 151
    elif change == "task":
        task_id = "other-task"
    else:
        payload = used.payload
    env.log.append(used.run_id, "consent.used", payload, task_id=task_id)
    with pytest.raises(TianshuError):
        _rebuild(env)


@pytest.mark.parametrize("invalid", ["future_event", "matcher", "state", "selection"])
def test_rebuild_refuses_unknown_history_and_projection_values(env, invalid):
    from tianshu.canonical import jcs_bytes

    _issue(env)
    if invalid in ("future_event", "matcher"):
        import hashlib

        issued = env.log.read_types(("consent.issued",))[0]
        payload = json.loads(json.dumps(issued.payload))
        if invalid == "matcher":
            payload["matcher"]["kind"] = "future_matcher"
        raw = jcs_bytes(payload)
        kind = "consent.future" if invalid == "future_event" else "consent.issued"
        row = env.log.conn.execute(
            "INSERT INTO events(run_id,task_id,type,ts,actor_kind,actor_id,payload,payload_hash)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (issued.run_id, issued.task_id, kind, issued.ts, "human", "cli:local",
             raw.decode(), hashlib.sha256(raw).hexdigest()),
        )
        env.log.conn.execute(
            "INSERT INTO event_versions VALUES (?,?,?)", (row.lastrowid, kind, 1),
        )
    elif invalid == "state":
        env.log.conn.execute("UPDATE scoped_consents SET state='unknown'")
    else:
        _prepare(env)
        env.log.conn.execute("UPDATE consent_selections SET used=2")
    with pytest.raises(TianshuError):
        _rebuild(env)


def test_explicit_rebuild_updates_existing_store_from_events(env):
    _issue(env)
    request, _, _ = _prepare(env)
    assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "applied"
    env.log.conn.execute("UPDATE scoped_consents SET remaining_uses=2,used_bytes=0")
    before = env.log.seq_head()
    env.consent.rebuild_from_events()
    row = env.consent.list(run_id=env.run.run_id)[0]
    assert row["remaining_uses"] == 1 and row["used_bytes"] == 3
    assert env.log.seq_head() == before


def test_directory_identity_and_recursion_are_enforced(env):
    _issue(env)
    sub = env.ws / "sub"
    sub.mkdir()
    (sub / "note.md").write_bytes(b"old")
    args = {"path": "sub/note.md", "content": "new"}
    request = build_request(
        task=env.task, run=env.run, claim=env.claim, call_id="sub",
        tool_id="modify_text", target=sub / "note.md", args=args,
    )
    assert env.dispatcher.prepare(request, env.claim, args=args).kind == "waiting"
    # 目录路径相同、底层目录身份变化，也不得接受原 Scope。
    old_key = ResourceIdentity.of(env.ws).key
    env.ws.rename(env.root / "previous-ws")
    env.ws.mkdir()
    (env.ws / "note.md").write_bytes(b"old")
    assert ResourceIdentity.of(env.ws).key != old_key
    request, args = _request(env, call="changed-directory")
    assert env.consent.select(request, args=args) is None


def test_scope_and_event_schemas_are_real_and_closed(env):
    scope = _issue(env).to_dict()
    from tianshu import event_schema

    assert not event_schema.payload_errors("consent.issued", scope)
    doc = json.loads((Path(__file__).parents[1] / "schemas/c4/consent.json").read_text())
    assert doc["$defs"]["scopedConsent"] == event_schema.CURRENT_SCHEMAS["consent.issued"]
    bad = {**scope, "matcher": {**scope["matcher"], "kind": "prefix_command"}}
    with pytest.raises(ConsentError):
        env.consent.issue(bad, actor=HumanIdentity.local_cli())


def test_exact_command_binds_executable_argv_environment_and_profile(env):
    import sys

    from tianshu.execpolicy import ISOLATION_BOUNDARIES, ExecPolicy

    task = replace(env.task, exec_policy=ExecPolicy(
        argv_allowlist=(str(Path(sys.executable).resolve()),),
        cwd_roots=(env.ws,), timeout_ms=1000, max_output_bytes=1000,
        isolation_requires=ISOLATION_BOUNDARIES,
    ))
    args = {"argv": [str(Path(sys.executable).resolve()), "-V"], "cwd": "."}
    matcher = command_matcher(task, args=args, cwd=env.ws)
    assert matcher["kind"] == "exact_command"
    assert matcher["argv"] == args["argv"]
    assert matcher["cwd"] == str(env.ws.resolve())
    assert matcher != command_matcher(task, args={**args, "argv": args["argv"] + ["x"]}, cwd=env.ws)
    assert matcher != command_matcher(
        replace(task, exec_policy=replace(task.exec_policy, timeout_ms=999)),
        args=args, cwd=env.ws,
    )


def test_control_issues_lists_and_revokes_without_accepting_actor_from_client(env):
    from types import SimpleNamespace

    from tianshu.consent_control import handle
    from tianshu.control import ControlError

    plane = SimpleNamespace(stack=SimpleNamespace(dispatcher=env.dispatcher))
    raw = _issue(env).to_dict()
    for key in ("consent_id", "issued_by", "runtime_session_id", "format_version", "task_id"):
        raw.pop(key)
    raw["request_id"] = "control-issue"
    issued = handle(plane, "issue", raw)["consent"]
    assert issued["issued_by"] == {"actor": "cli:local", "source": "cli"}
    assert issued["runtime_session_id"] == "session-a"
    listed = handle(plane, "list", {"run_id": env.run.run_id})["consents"]
    assert listed[-1]["consent_id"] == issued["consent_id"]
    revoked = handle(plane, "revoke", {
        "consent_id": issued["consent_id"], "request_id": "control-revoke",
    })["consent"]
    assert revoked["state"] == "revoked"
    with pytest.raises(ControlError):
        handle(plane, "issue", {**raw, "issued_by": {"actor": "model"}})


def test_scope_use_grant_and_intent_roll_back_together(env, monkeypatch):
    _issue(env)
    request, _, decision = _prepare(env)
    assert decision.kind == "admitted"
    original = env.log.append

    def reject_intent(run_id, event_type, *args, **kwargs):
        if event_type == "tool.intent":
            raise RuntimeError("injected intent failure")
        return original(run_id, event_type, *args, **kwargs)

    monkeypatch.setattr(env.log, "append", reject_intent)
    with pytest.raises(RuntimeError, match="intent failure"):
        env.dispatcher.dispatch(request.request_id, env.claim)
    assert env.consent.list(run_id=env.run.run_id)[0]["remaining_uses"] == 2
    assert env.dispatcher.dispatch_record(env.run.run_id, request.call_id) is None
    assert not env.log.conn.execute("SELECT 1 FROM grants").fetchall()
    assert not [e for e in env.log.read_run(env.run.run_id) if e.type == "consent.used"]
    assert (env.ws / "note.md").read_bytes() != b"new"


def test_unknown_effect_blocks_later_scope_use(env, monkeypatch):
    _issue(env)
    request, _, _ = _prepare(env)

    def fail_execute(*_args, **_kwargs):
        raise RuntimeError("effect cannot be established")

    monkeypatch.setattr(env.dispatcher._executor, "execute", fail_execute)
    assert env.dispatcher.dispatch(request.request_id, env.claim).effect_state == "unknown"
    _, _, decision = _prepare(env, call="after-unknown")
    assert decision.kind == "rejected"
    assert decision.rule_id == "dispatch.workspace_blocked"


def test_binding_change_after_intent_is_rejected_before_started(env, monkeypatch):
    import tianshu.dispatcher as dispatcher_module

    _issue(env)
    request, _, _ = _prepare(env)

    def change_plan(name):
        if name == "after_intent":
            env.binding[0] = replace(env.binding[0], plan_digest="c" * 64)

    monkeypatch.setattr(dispatcher_module, "failpoint", change_plan)
    result = env.dispatcher.dispatch(request.request_id, env.claim)
    assert result.effect_state == "none_applied"
    assert "execution.started" not in [e.type for e in env.log.read_run(env.run.run_id)]
    assert (env.ws / "note.md").read_bytes() != b"new"


def test_exact_command_scope_uses_complete_invocation_and_live_executable(env, monkeypatch):
    from tianshu.execpolicy import ISOLATION_BOUNDARIES, ExecPolicy
    from tianshu.tasks import RUN_RUNNING

    root = env.root / "command-ws"
    root.mkdir()
    binary = root / "test-executable"
    binary.write_bytes(b"fixed executable")
    binary.chmod(0o700)
    monkeypatch.setenv("SCOPED_TEST_ENV", "original")
    policy = ExecPolicy(
        argv_allowlist=(str(binary),), cwd_roots=(root,), env_allowlist=("SCOPED_TEST_ENV",),
        timeout_ms=1000, max_output_bytes=1000, isolation_requires=ISOLATION_BOUNDARIES,
    )
    task = env.tasks.create_task(
        "fixed command", [{"root_path": str(root)}], exec_policy=policy,
    )
    run = env.tasks.start_run(task.task_id)
    env.leases.acquire([root], run.run_id)
    job = env.claims.enqueue(run.run_id)
    claim = env.claims.claim(job.job_id)
    env.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    env.binding[0] = ConsentBindingV1(
        task.task_id, run.run_id, run.goal_revision,
        workspace_binding_digest(task.workspaces), "a" * 64, "b" * 64,
    )
    args = {"argv": [str(binary), "--literal-argument"], "cwd": "."}
    raw = {
        "format_version": 1, "consent_id": "exact-invocation",
        **env.binding[0].to_dict(), "action": "exec",
        "tool_binding": tool_binding(env.dispatcher.registry.spec("run_command")),
        "matcher": command_matcher(task, args=args, cwd=root), "max_uses": 1,
        "expires_at": env.now[0] + 600_000, "runtime_session_id": "session-a",
        "issued_by": HumanIdentity.local_cli().to_dict(), "request_id": "exact-command",
    }
    env.consent.issue(raw, actor=HumanIdentity.local_cli())

    def request(call, value):
        return build_request(
            task=task, run=run, claim=claim, call_id=call, tool_id="run_command",
            target=root, args=value,
        )

    # 完整匹配且准备不消耗；多一个 argv 也不能用。
    assert env.consent.select(request("exact", args), args=args) == "exact-invocation"
    changed = {**args, "argv": args["argv"] + ["unapproved"]}
    assert env.consent.select(request("extra-argv", changed), args=changed) is None
    monkeypatch.setenv("SCOPED_TEST_ENV", "changed")
    assert env.consent.select(request("new-env", args), args=args) is None
    monkeypatch.setenv("SCOPED_TEST_ENV", "original")
    binary.write_bytes(b"replaced executable")
    assert env.consent.select(request("new-binary", args), args=args) is None


def test_directory_scope_never_applies_to_delete_or_authority_files(env):
    _issue(env)
    args = {"path": "note.md"}
    request = build_request(
        task=env.task, run=env.run, claim=env.claim, call_id="delete",
        tool_id="delete_file", target=env.ws / "note.md", args=args,
    )
    assert env.dispatcher.prepare(request, env.claim, args=args).kind == "waiting"
    data = env.dispatcher._data_root
    data_binding = directory_matcher(
        data, recursive=True, max_file_bytes=100, max_total_bytes=100,
    )
    with pytest.raises(ConsentError, match="目录|权威"):
        _issue(env, matcher=data_binding, request_id="authority")


def _child_store(db, data_root, binding):
    from tianshu.events import EventLog
    from tianshu.tasks import TaskStore

    log = EventLog(Path(db))
    tasks = TaskStore(log, data_root=Path(data_root))
    store = ConsentStore(
        log, tasks=tasks, data_root=Path(data_root), runtime_session_id="session-a",
        binding_resolver=lambda _: ConsentBindingV1(**binding),
        clock=lambda: 1_000_000, monotonic=lambda: 20_000,
    )
    return log, store


def _consume_process(db, data_root, binding, request, args, barrier, result):
    from tianshu.permissions import ActionRequest

    log, store = _child_store(db, data_root, binding)
    try:
        barrier.wait(timeout=15)
        try:
            with log.transaction():
                store.consume(ActionRequest.of(request), args=args)
            result.put("consumed")
        except ConsentError:
            result.put("rejected")
    finally:
        log.close()


def test_two_processes_cannot_spend_the_last_scope_use_twice(env):
    import multiprocessing

    _issue(env, uses=1)
    requests = [_prepare(env, call="process-" + str(index))[:2] for index in range(2)]
    context = multiprocessing.get_context("spawn")
    barrier, result = context.Barrier(3), context.Queue()
    children = [context.Process(target=_consume_process, args=(
        str(env.log.db_path), str(env.dispatcher._data_root), env.binding[0].to_dict(),
        request.to_canonical_dict(), args, barrier, result,
    )) for request, args in requests]
    for child in children:
        child.start()
    try:
        barrier.wait(timeout=15)
        outcomes = [result.get(timeout=15), result.get(timeout=15)]
        assert sorted(outcomes) == ["consumed", "rejected"]
        for child in children:
            child.join(timeout=15)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
    assert env.consent.list(run_id=env.run.run_id)[0]["remaining_uses"] == 0
    assert len([e for e in env.log.read_run(env.run.run_id) if e.type == "consent.used"]) == 1


def _revoke_process(db, data_root, binding, barrier, ready, completed):
    log, store = _child_store(db, data_root, binding)
    try:
        ready.set()
        barrier.wait(timeout=15)
        store.revoke(
            "scope-human-issue", actor=HumanIdentity.local_cli(), request_id="child-revoke",
        )
        completed.set()
    finally:
        log.close()


@pytest.mark.parametrize("winner", ["revoke", "started"])
def test_two_process_revoke_and_execution_start_have_a_linear_order(env, monkeypatch, winner):
    import multiprocessing

    import tianshu.dispatcher as dispatcher_module

    _issue(env)
    request, _, _ = _prepare(env)
    context = multiprocessing.get_context("spawn")
    barrier, ready, completed = context.Barrier(2), context.Event(), context.Event()
    child = context.Process(target=_revoke_process, args=(
        str(env.log.db_path), str(env.dispatcher._data_root), env.binding[0].to_dict(),
        barrier, ready, completed,
    ))
    child.start()
    assert ready.wait(timeout=15)
    if winner == "revoke":
        def after_intent(name):
            if name == "after_intent":
                barrier.wait(timeout=15)
                assert completed.wait(timeout=15)
        monkeypatch.setattr(dispatcher_module, "failpoint", after_intent)
    else:
        execute = env.dispatcher._executor.execute

        def after_start(*args, **kwargs):
            barrier.wait(timeout=15)
            return execute(*args, **kwargs)
        monkeypatch.setattr(env.dispatcher._executor, "execute", after_start)
    try:
        result = env.dispatcher.dispatch(request.request_id, env.claim)
        child.join(timeout=15)
        assert child.exitcode == 0
        assert completed.is_set()
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
    events = env.log.read_run(env.run.run_id)
    types = [event.type for event in events]
    if winner == "revoke":
        assert result.effect_state == "none_applied"
        assert "execution.started" not in types
        assert (env.ws / "note.md").read_bytes() != b"new"
    else:
        assert result.effect_state == "applied"
        assert types.index("execution.started") < types.index("consent.revoked")
        assert (env.ws / "note.md").read_bytes() == b"new"
