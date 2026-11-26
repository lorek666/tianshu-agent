"""无模型的验证阶段；命令只通过现有 Dispatcher 派发。"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from .canonical import jcs_sha256
from .execpolicy import filtered_env
from .plans import PlanStore, put_material
from .run_policy import EFFECTS, PURE_ONLY
from .verification import input_fingerprint, request_check, step_verified

VERIFY_RUN = "verification.run_started"


def binding_for(log, run_id):
    events = log.read_run(run_id, event_types=(VERIFY_RUN,))
    if not events:
        return None
    if len(events) != 1:
        raise ValueError("验证阶段绑定重复")
    return events[0].payload


def bind_verification(stack, run_id, view, step_id, *, request_id):
    run = stack.tasks.get_run(run_id)
    if run is None or run.task_id != view["task_id"]:
        raise ValueError("验证 Run 归属错误")
    payload = {
        "format_version": 1, "plan_id": view["plan_id"], "revision": view["revision"],
        "plan_digest": view["document_digest"], "mission_digest": jcs_sha256(view["mission"]),
        "step_id": step_id, "request_id": request_id,
    }
    previous = binding_for(stack.log, run_id)
    if previous is not None:
        if previous != payload:
            raise ValueError("验证 Run 已绑定其他阶段")
        return
    stack.log.append(run_id, VERIFY_RUN, payload, task_id=view["task_id"], actor_kind="core")


def start_verification(stack, view, step_id, *, request_id, mission=None):
    """独立入口；Goal 可在其阶段事务内创建 Run 后调用 bind_verification。"""
    with stack.log.transaction():
        for event in stack.log.read_types((VERIFY_RUN,), task_id=view["task_id"]):
            if event.payload["request_id"] == request_id:
                if (event.payload["plan_id"], event.payload["revision"],
                    event.payload["step_id"]) != (view["plan_id"], view["revision"], step_id):
                    raise ValueError("验证启动请求编号冲突")
                return stack.tasks.get_run(event.run_id)
        current = PlanStore(stack).get(view["plan_id"])
        step = next((s for s in current["steps"] if s["step_id"] == step_id), None)
        if (not current["confirmed"] or current["document_digest"] != view["document_digest"]
                or step is None or step["state"] != "verifying"):
            raise ValueError("当前步骤不允许验证")
        command = any(c["kind"] == "command_exit" and c["check_id"] in step["verification_ids"]
                      for c in current["manifest"]["checks"])
        policy = EFFECTS if command else PURE_ONLY
        run = stack.tasks.start_run(view["task_id"], execution_policy=policy, payload={
            "task": "确定性验证阶段；不调用模型。", "out": "",
            "tools": ["run_command"] if command else [],
        })
        if mission is not None:
            from .mission import freeze_mission
            freeze_mission(stack.log, run.run_id, mission=mission,
                           registry=stack.dispatcher.registry)
        bind_verification(stack, run.run_id, current, step_id, request_id=request_id)
        stack.claims.enqueue(run.run_id)
    return run


def command_binding(stack, task_id, executable):
    """规划阶段可展示的真实绑定；摘要不暴露环境变量值。"""
    task = stack.tasks.get_task(task_id)
    policy = task.exec_policy if task else None
    if policy is None:
        raise ValueError("任务未配置命令策略")
    path = Path(executable)
    if not path.is_absolute():
        raise ValueError("可执行文件必须使用绝对路径")
    canonical = path.resolve(strict=True)
    backend = stack.dispatcher._exec_backend
    return {
        "executable": str(canonical),
        "executable_digest": hashlib.sha256(canonical.read_bytes()).hexdigest(),
        "env_profile_digest": jcs_sha256(filtered_env(policy, dict(os.environ))),
        "isolation_profile_digest": jcs_sha256({
            "policy": policy.to_dict(), "backend": asdict(backend.capabilities),
        }),
    }


def _command_preconditions(stack, task, check):
    inputs = check["inputs"]
    actual = command_binding(stack, task.task_id, inputs["executable"])
    if any(inputs[k] != v for k, v in actual.items()):
        raise ValueError("命令、环境或隔离配置已经改变")
    if inputs["argv"][0] != actual["executable"]:
        raise ValueError("argv 必须含绑定的可执行文件")
    if task.exec_policy.timeout_ms > inputs["timeout_ms"]:
        raise ValueError("执行策略期限超过检查期限")
    index = int(inputs["cwd_binding_id"])
    if index < 0 or str(index) != inputs["cwd_binding_id"]:
        raise ValueError("cwd 绑定无效")
    return str(Path(task.workspaces[index].root_path).resolve(strict=True))


def assert_execution_binding(stack, run_id, call_id, args):
    """执行守卫内及实际 spawn 前调用；普通 Run 不受本阶段路由影响。"""
    binding = binding_for(stack.log, run_id)
    if binding is None:
        return
    view = PlanStore(stack).get(binding["plan_id"])
    task = stack.tasks.get_task(view["task_id"])
    step = next((s for s in view["steps"] if s["step_id"] == binding["step_id"]), None)
    if (not view["confirmed"] or view["document_digest"] != binding["plan_digest"]
            or step is None or step["state"] != "verifying"):
        raise ValueError("验证计划或阶段已改变")
    check = next((c for c in view["manifest"]["checks"] if
                  c["check_id"] in step["verification_ids"]
                  and "verification_" + c["check_id"] == call_id), None)
    if check is None or check["kind"] != "command_exit":
        raise ValueError("验证 Run 只能执行已确认的检查命令")
    cwd = _command_preconditions(stack, task, check)
    if args != {"argv": check["inputs"]["argv"], "cwd": cwd}:
        raise ValueError("验证命令参数与已确认检查不符")
    requests = stack.log.read_run(run_id, event_types=("verification.requested",))
    request = next((e for e in reversed(requests)
                    if e.payload["check_id"] == check["check_id"]), None)
    if (request is None or request.payload["check_digest"] != jcs_sha256(check)
            or request.payload["input_fingerprint"] != input_fingerprint(stack, task.task_id)):
        raise ValueError("验收输入或检查已改变")
    budget = getattr(stack.dispatcher, "_budget", None)
    cursor, seen = "run:" + run_id, set()
    while budget is not None:
        if cursor in seen:
            raise ValueError("验证预算父链损坏")
        seen.add(cursor)
        account = budget.account(cursor)
        if account is None:
            break
        deadline = account["deadline_ms"]
        if (account["cancelled"] or deadline is not None
                and budget._clock() + check["inputs"]["timeout_ms"] > deadline):
            raise ValueError("验证命令超出剩余预算期限")
        if account["parent_id"] is None:
            break
        cursor = account["parent_id"]


def _command_result(loop, stack, run_id, claim, progress, task, view, step_id, check, request):
    inputs = check["inputs"]
    call_id = "verification_" + check["check_id"]
    proposal = next((p for p in progress.pending if p["call_id"] == call_id), None)
    if proposal is None:
        try:
            cwd = _command_preconditions(stack, task, check)
        except (ValueError, OSError, IndexError):
            return "blocked", "verification.command_binding_changed", None
        proposal = {"call_id": call_id, "tool_id": "run_command",
                    "args": {"argv": inputs["argv"], "cwd": cwd}, "attempt_id": 1}
        progress.pending = [proposal]
        loop._tasks.save_cursor(run_id, progress.to_cursor(), claim)
    # 恢复后仍重核现场，不能用旧审批覆盖新的命令/环境。
    try:
        _command_preconditions(stack, task, check)
    except (ValueError, OSError, IndexError):
        return "blocked", "verification.command_binding_changed", None
    state = loop._advance(run_id, claim, progress, task, proposal)
    if state != "done":
        return state, "", None
    progress.pending = []
    recorded = loop._results[call_id]
    output = recorded.get("output", {})
    if recorded.get("effect_state") in ("unknown", "partial"):
        return "inconclusive", "verification.effect_unknown", recorded
    if recorded.get("status") == "rejected":
        return "blocked", output.get("rule_id", "verification.command_blocked"), recorded
    passed = (type(output.get("exit_code")) is int
              and output["exit_code"] in inputs["expected_exit_codes"]
              and not output.get("timed_out") and not output.get("signal"))
    if type(output.get("exit_code")) is not int:
        return "inconclusive", "verification.command_incomplete", recorded
    return ("passed", "verification.command_match", recorded) if passed else (
        "failed", "verification.command_mismatch", recorded)


def _check_order(step, checks):
    ordered, seen = [], set()

    def visit(check_id):
        if check_id in seen:
            return
        seen.add(check_id)
        check = checks[check_id]
        if check["kind"] == "human":
            for dep in check["inputs"]["required_check_ids"]:
                if dep in step["verification_ids"]:
                    visit(dep)
        ordered.append(check_id)
    for check_id in step["verification_ids"]:
        visit(check_id)
    return ordered


def _command_evidence(stack, run_id, task, view, step_id, check, request,
                      recorded, fingerprint, source_seq):
    from .agent_documents import EVIDENCE_SCHEMA, validate_document
    # 完整结果已有 append-only tool.result；大 stdout/stderr 不复制进 256 KiB 文档。
    material = {
        "kind": "command_result", "binding": check["inputs"],
        "input_fingerprint": fingerprint, "result_event_seq": source_seq,
        "result_digest": None if recorded is None else jcs_sha256(recorded),
        "status": None if recorded is None else recorded.get("status"),
        "output": {},
    }
    output = {} if recorded is None else recorded.get("output", {})
    for key in ("exit_code", "signal", "timed_out", "truncated", "stdout_ref", "stderr_ref"):
        material["output"][key] = output.get(key)
    refs = []
    for key in ("stdout", "stderr"):
        text = output.get(key)
        if text is None:
            continue
        raw = text.encode()
        from .contents import make_ref
        from .fsutil import atomic_write_bytes
        from .restore_access import ensure_material_write_allowed
        digest = hashlib.sha256(raw).hexdigest()
        with ensure_material_write_allowed(stack.contents.blob_dir, owner_log=stack.log):
            atomic_write_bytes(stack.contents.blob_dir / digest, raw)
            stack.contents.record(digest, raw, task_id=task.task_id, run_id=run_id)
        material["output"][key + "_snapshot"] = make_ref(digest)
        refs.append(make_ref(digest))
    detail = put_material(stack, task.task_id, material, run_id=run_id)
    evidence = {
        "format_version": 1, "evidence_id": "evidence_" + uuid.uuid4().hex,
        "task_id": task.task_id, "mission_id": view["mission"]["mission_id"],
        "plan_digest": view["document_digest"], "step_id": step_id, "run_id": run_id,
        "attempt_id": request["attempt_id"], "kind": "command_result",
        "event_seqs": [source_seq] if source_seq else [], "content_refs": [detail, *refs],
        "input_fingerprint": fingerprint,
    }
    validate_document(EVIDENCE_SCHEMA, evidence, "命令证据")
    return put_material(stack, task.task_id, evidence, run_id=run_id)


def drive(loop, run_id, claim, progress, task):
    """由 AgentLoop 在真实租约、claim 和恢复校验后调用。"""
    binding = binding_for(loop._log, run_id)
    stack = SimpleNamespace(log=loop._log, tasks=loop._tasks, contents=loop._contents,
                            dispatcher=loop._dispatcher)
    plans = PlanStore(stack)
    view = plans.get(binding["plan_id"])
    if (view["document_digest"] != binding["plan_digest"]
            or jcs_sha256(view["mission"]) != binding["mission_digest"] or not view["confirmed"]):
        return loop._finish(run_id, claim, progress, outcome="failed", error="验证计划已过期")
    step_id = binding["step_id"]
    step = next(s for s in view["steps"] if s["step_id"] == step_id)
    checks = {c["check_id"]: c for c in view["manifest"]["checks"]}
    pending_human = False
    for check_id in _check_order(step, checks):
        if loop._safe_point(run_id, claim, progress) is not None:
            return loop._result(run_id, progress, state=loop._state_of(run_id))
        events = loop._log.read_run(run_id, event_types=("verification.result",))
        if any(e.payload["check_id"] == check_id for e in events):
            continue
        check = checks[check_id]
        try:
            request = request_check(stack, view, step_id, check_id, run_id=run_id,
                                    request_id=f"{run_id}_{check_id}")
        except (ValueError, OSError) as exc:
            plans.change_step(view["plan_id"], step_id, "blocked",
                              reason="verification.prerequisite_unverifiable",
                              request_id=f"{run_id}_blocked")
            return loop._finish(run_id, claim, progress, outcome="failed",
                                error=f"验收输入或依赖无法确认：{exc}")
        if check["kind"] == "human":
            pending_human = True
            continue
        if check["kind"] != "command_exit":
            continue
        result, reason, recorded = _command_result(
            loop, stack, run_id, claim, progress, task, view, step_id, check, request)
        if result in ("waiting", "reconciling"):
            return loop._result(run_id, progress, state=loop._state_of(run_id))
        try:
            fingerprint = input_fingerprint(stack, task.task_id)
        except (ValueError, OSError):
            fingerprint = request["input_fingerprint"]
            result, reason = "inconclusive", "verification.input_unverifiable"
        if fingerprint != request["input_fingerprint"]:
            result, reason = "inconclusive", "verification.inputs_changed"
        source_seq = loop._result_seqs.get("verification_" + check_id, 0)
        ref = _command_evidence(stack, run_id, task, view, step_id, check, request,
                                recorded, fingerprint, source_seq)
        payload = {k: v for k, v in request.items() if k != "request_id"}
        payload.update(result=result, reason_code=reason, verified_event_seq=source_seq,
                       evidence_refs=[ref])
        loop._log.append(run_id, "verification.result", payload,
                         task_id=task.task_id, actor_kind="core")
    fresh = plans.get(view["plan_id"])
    if step_verified(stack, fresh, step_id):
        plans.change_step(view["plan_id"], step_id, "satisfied", run_id=None,
                          request_id=f"{run_id}_satisfied")
    elif not pending_human:
        plans.change_step(view["plan_id"], step_id, "failed",
                          reason="verification.required_checks_failed",
                          request_id=f"{run_id}_failed")
    return loop._finish(run_id, claim, progress, outcome="succeeded", error=None)
