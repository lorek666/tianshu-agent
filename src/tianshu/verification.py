"""确定性验收；模型只能解释结果，不能提交通过事实。"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path

from .canonical import jcs_bytes, jcs_sha256
from .errors import TianshuError
from .permissions import HumanIdentity
from .plans import bounded_text, closed, unique_ids
from .workspace import Workspace

CHECK_INPUTS = {
    "file_exists": {"workspace_binding_id", "relative_path", "file_kind"},
    "file_hash": {"workspace_binding_id", "relative_path", "file_kind",
                  "algorithm", "expected_digest"},
    "command_exit": {"executable", "executable_digest", "argv", "cwd_binding_id",
                     "env_profile_digest", "expected_exit_codes", "timeout_ms",
                     "isolation_profile_digest"},
    "source_reference": {"content_ref", "source_event_seq", "cited_digest", "locator"},
    "human": {"question", "required_artifact_ids", "required_check_ids"},
}
VERIFICATION_EVENTS = ("verification.requested", "verification.result", "verification.decided")
MAX_INPUT_FILES = 8192
MAX_INPUT_BYTES = 64 * 1024 * 1024


def validate_manifest(mission, manifest):
    closed(manifest, {"format_version", "mission_id", "mission_digest", "checks"}, "检查清单")
    if (type(manifest["format_version"]) is not int or manifest["format_version"] != 1
            or manifest["mission_id"] != mission["mission_id"]
            or manifest["mission_digest"] != jcs_sha256(mission)):
        raise ValueError("检查清单使命绑定无效")
    checks = manifest["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 256:
        raise ValueError("检查数量必须为 1–256")
    unique_ids([c.get("check_id") for c in checks if isinstance(c, dict)],
               "检查", maximum=256, empty=False)
    criteria = {c["criterion_id"] for c in mission["criteria"]}
    for check in checks:
        closed(check, {"format_version", "check_id", "criterion_ids", "required",
                       "kind", "inputs"}, "检查")
        if (check["format_version"] != 1 or type(check["format_version"]) is not int
                or check["required"] is not True or check["kind"] not in CHECK_INPUTS):
            raise ValueError("检查类型/版本无效或非必需")
        unique_ids(check["criterion_ids"], "检查条件", empty=False)
        if set(check["criterion_ids"]) - criteria:
            raise ValueError("检查引用其他使命条件")
        inputs = check["inputs"]
        closed(inputs, CHECK_INPUTS[check["kind"]], "检查参数")
        if check["kind"] in ("file_exists", "file_hash"):
            bounded_text(inputs["relative_path"], "相对路径", 4096)
            path = Path(inputs["relative_path"])
            if path.is_absolute() or ".." in path.parts or inputs["file_kind"] != "regular":
                raise ValueError("文件检查只允许工作区内普通文件")
            if check["kind"] == "file_hash" and (
                inputs["algorithm"] != "sha256" or len(inputs["expected_digest"]) != 64
            ):
                raise ValueError("文件摘要参数无效")
        elif check["kind"] == "human":
            bounded_text(inputs["question"], "验收问题")
            unique_ids(inputs["required_artifact_ids"], "验收产物", maximum=2048)
            unique_ids(inputs["required_check_ids"], "验收检查", maximum=256)
        elif check["kind"] == "command_exit":
            if (not isinstance(inputs["argv"], list)
                    or any(not isinstance(a, str) or "\x00" in a for a in inputs["argv"])
                    or type(inputs["timeout_ms"]) is not int or inputs["timeout_ms"] <= 0
                    or not isinstance(inputs["expected_exit_codes"], list)
                    or not inputs["expected_exit_codes"]
                    or any(type(n) is not int for n in inputs["expected_exit_codes"])):
                raise ValueError("命令检查参数无效")
    graph = {c["check_id"]: (c["inputs"]["required_check_ids"] if c["kind"] == "human" else [])
             for c in checks}
    visited, active = set(), set()

    def visit(node):
        if node not in graph or node in active:
            raise ValueError("人工检查引用缺失或循环")
        if node in visited:
            return
        active.add(node)
        for other in graph[node]:
            visit(other)
        active.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    if len(jcs_bytes(manifest)) > 256 * 1024:
        raise ValueError("检查清单过大")


def _file_bytes(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
            raise ValueError("检查对象不是有界普通文件")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or len(raw) > MAX_INPUT_BYTES):
            raise ValueError("读取期间文件发生变化")
        return raw
    finally:
        os.close(fd)


def input_fingerprint(stack, task_id):
    task = stack.tasks.get_task(task_id)
    if task is None:
        raise ValueError("任务不存在")
    entries, total = [], 0
    for index, binding in enumerate(task.workspaces):
        root = Path(binding.root_path).resolve(strict=True)
        entries.append([index, ".", root.stat().st_dev, root.stat().st_ino])
        for current, dirs, files in os.walk(root, followlinks=False):
            # Git 的管理文件不是被测源码，排除项显式进入指纹。
            dirs[:] = sorted(d for d in dirs if d != ".git")
            if any((Path(current) / d).is_symlink() for d in dirs):
                raise ValueError("无法证明含符号链接目录的输入闭包")
            for name in sorted(files):
                path = Path(current) / name
                if path.is_symlink():
                    raise ValueError("无法证明符号链接文件的输入闭包")
                raw = _file_bytes(path)
                total += len(raw)
                entries.append([index, path.relative_to(root).as_posix(),
                                hashlib.sha256(raw).hexdigest()])
                if len(entries) > MAX_INPUT_FILES or total > MAX_INPUT_BYTES:
                    raise ValueError("输入闭包超过验收限额，需要缩小工作区或人工修订验收")
    return jcs_sha256({"task_id": task_id, "revision": task.goal_revision,
                       "inputs": entries, "excluded": [".git/"]})


def observe_check(stack, task_id, check):
    """只读检查；command_exit 必须由阶段 Run 经过 Dispatcher 执行。"""
    result = {"result": "inconclusive", "reason_code": "verification.inconclusive",
              "input_fingerprint": "0" * 64, "evidence": {}}
    try:
        before = input_fingerprint(stack, task_id)
        result["input_fingerprint"] = before
        kind, inputs = check["kind"], check["inputs"]
        if kind in ("file_exists", "file_hash"):
            task = stack.tasks.get_task(task_id)
            index = int(inputs["workspace_binding_id"])
            if index < 0 or str(index) != inputs["workspace_binding_id"]:
                raise ValueError("工作区绑定无效")
            root = Path(task.workspaces[index].root_path)
            path = Workspace(root).resolve(inputs["relative_path"])
            raw = _file_bytes(path)
            digest = hashlib.sha256(raw).hexdigest()
            result["evidence"] = {"kind": "file_observation", "path": str(path),
                                  "sha256": digest, "bytes": len(raw)}
            passed = kind == "file_exists" or digest == inputs["expected_digest"]
            result.update(result="passed" if passed else "failed",
                          reason_code="verification.match" if passed else "verification.mismatch")
        elif kind == "source_reference":
            from .plans import read_material
            material = read_material(stack, task_id, inputs["content_ref"])
            source = next((e for e in stack.log.read_after(inputs["source_event_seq"] - 1, limit=1)
                           if e.seq == inputs["source_event_seq"] and e.task_id == task_id), None)
            if source is None or jcs_sha256(material) != inputs["cited_digest"]:
                raise ValueError("来源归属或摘要无效")
            if inputs["content_ref"] not in str(source.payload):
                raise ValueError("来源事件未绑定引用")
            result.update(result="passed", reason_code="verification.source_bound",
                          evidence={"kind": "source_snapshot", **inputs})
        elif kind == "human":
            result.update(result="blocked", reason_code="verification.human_required")
        else:
            result.update(result="blocked", reason_code="verification.command_requires_run")
        if before != input_fingerprint(stack, task_id):
            raise ValueError("检查期间输入改变")
    except FileNotFoundError:
        result.update(result="failed", reason_code="verification.file_missing")
    except (ValueError, OSError, IndexError, TianshuError):
        result.update(result="inconclusive", reason_code="verification.input_unverifiable")
    return result


def step_verified(stack, view, step_id):
    step = next(s for s in view["steps"] if s["step_id"] == step_id)
    events = [e for e in stack.log.read_types(VERIFICATION_EVENTS, task_id=view["task_id"])
              if e.payload["plan_id"] == view["plan_id"]
              and e.payload["revision"] == view["revision"] and e.payload["step_id"] == step_id]
    try:
        current = input_fingerprint(stack, view["task_id"])
    except (ValueError, OSError):
        return False
    for check_id in step["verification_ids"]:
        requests = [e for e in events if e.type == "verification.requested"
                    and e.payload["check_id"] == check_id]
        if not requests:
            if view["revision"] <= 1 or step_id in view.get("changed_step_ids", ()):
                return False
            from .plans import PlanStore
            previous = PlanStore(stack).get(view["plan_id"], revision=view["revision"] - 1)
            if (previous["document_digest"] != view["document"]["previous_digest"]
                    or not step_verified(stack, previous, step_id)):
                return False
            continue
        latest = requests[-1].payload
        results = [e for e in events if e.type == "verification.result"
                   and e.payload["attempt_id"] == latest["attempt_id"]]
        if not results or results[-1].payload["result"] != "passed":
            return False
        if results[-1].payload["input_fingerprint"] != current:
            return False
    return True


def request_check(stack, view, step_id, check_id, *, run_id, request_id):
    from .goals import request_id as checked_request_id
    from .plans import PlanStore, put_material

    checked_request_id(request_id)
    # 重试先核对持久化请求，已决定的 attempt 也可无副作用地返回。
    for prior in stack.log.read_types(("verification.requested",), task_id=view["task_id"]):
        if prior.payload["request_id"] == request_id:
            if (prior.payload["plan_id"], prior.payload["revision"],
                prior.payload["step_id"], prior.payload["check_id"], prior.run_id) != (
                view["plan_id"], view["revision"], step_id, check_id, run_id
            ):
                raise ValueError("验收请求编号冲突")
            return prior.payload
    current = PlanStore(stack).get(view["plan_id"])
    step = next((s for s in current["steps"] if s["step_id"] == step_id), None)
    if (not current["confirmed"] or current["revision"] != view["revision"]
            or current["state_revision"] != view["state_revision"]
            or step is None or step["state"] != "verifying"
            or check_id not in step["verification_ids"]):
        raise ValueError("只能验收当前已确认计划中待验证步骤的检查")
    if run_id:
        run = stack.tasks.get_run(run_id)
        if run is None or run.task_id != view["task_id"]:
            raise ValueError("验收 Run 不属于此任务")
    check = next(c for c in current["manifest"]["checks"] if c["check_id"] == check_id)
    observation = observe_check(stack, view["task_id"], check)
    attempt_id = f"verify_{uuid.uuid4().hex}"
    evidence = {
        "format_version": 1, "evidence_id": f"evidence_{uuid.uuid4().hex}",
        "task_id": view["task_id"], "mission_id": view["mission"]["mission_id"],
        "plan_digest": view["document_digest"], "step_id": step_id,
        "run_id": run_id, "attempt_id": attempt_id,
        "kind": observation["evidence"].get("kind", "file_observation"),
        "event_seqs": [], "content_refs": [],
        "input_fingerprint": observation["input_fingerprint"],
    }
    material = observation["evidence"]
    if check["kind"] == "human":
        evidence["kind"] = "human_decision"
        artifacts, checks = _human_material(
            stack, current, check, observation["input_fingerprint"])
        material = {"kind": "human_review_material", "question": check["inputs"]["question"],
                    "artifacts": artifacts, "checks": checks}
        evidence["event_seqs"] = [r["event_seq"] for r in checks]
    evidence["content_refs"] = [put_material(stack, view["task_id"], material, run_id=run_id)]
    if observation["input_fingerprint"] != input_fingerprint(stack, view["task_id"]):
        raise ValueError("冻结待验材料期间输入改变")
    with stack.log.transaction() as tx:
        if PlanStore(stack).get(view["plan_id"])["state_revision"] != view["state_revision"]:
            raise ValueError("冻结待验材料期间计划改变")
        for prior in stack.log.read_types(("verification.requested",), task_id=view["task_id"]):
            if prior.payload["request_id"] == request_id:
                if (prior.payload["plan_id"], prior.payload["step_id"],
                    prior.payload["check_id"]) != (
                    view["plan_id"], step_id, check_id
                ):
                    raise ValueError("验收请求编号冲突")
                return prior.payload
        ref = put_material(stack, view["task_id"], evidence, run_id=run_id)
        p = {
            "format_version": 1, "plan_id": view["plan_id"], "revision": view["revision"],
            "step_id": step_id, "check_id": check_id, "check_digest": jcs_sha256(check),
            "attempt_id": attempt_id,
            "input_fingerprint": observation["input_fingerprint"],
            "evidence_refs": [ref], "request_id": request_id,
        }
        seq = tx.append(run_id, "verification.requested", p,
                        task_id=view["task_id"], actor_kind="core")
        if check["kind"] != "human" and check["kind"] != "command_exit":
            result = {k: v for k, v in p.items() if k != "request_id"}
            result.update(result=observation["result"], reason_code=observation["reason_code"],
                          verified_event_seq=seq)
            tx.append(run_id, "verification.result", result,
                      task_id=view["task_id"], actor_kind="core")
        return p


def _human_material(stack, view, check, fingerprint):
    """解析本次确切产物和依赖结果，不能让缺产物的空对象成为待审证据。"""
    from .plans import put_material
    artifacts = {a["artifact_id"]: a for s in view["document"]["steps"]
                 for a in s["expected_artifacts"]}
    frozen = []
    task = stack.tasks.get_task(view["task_id"])
    for artifact_id in check["inputs"]["required_artifact_ids"]:
        artifact = artifacts[artifact_id]
        if artifact["kind"] != "file":
            # content/source 仍须通过任务可见性和内容存储重新核对。
            from .plans import read_material
            value = read_material(stack, view["task_id"], artifact["locator"])
            frozen.append({"artifact_id": artifact_id, "kind": artifact["kind"],
                           "content_ref": artifact["locator"], "sha256": jcs_sha256(value)})
            continue
        matches = []
        for binding in task.workspaces:
            path = Workspace(Path(binding.root_path)).resolve(artifact["locator"])
            if path.exists():
                matches.append(path)
        if len(matches) != 1:
            raise ValueError("待审产物缺失或工作区定位不唯一")
        try:
            raw = _file_bytes(matches[0])
        except OSError as exc:
            raise ValueError("无法读取待审产物") from exc
        # 原始字节另存为有界快照，避免 UI 只看到可变路径。
        import base64
        ref = put_material(stack, view["task_id"], {
            "encoding": "base64", "data": base64.b64encode(raw).decode("ascii"),
        })
        frozen.append({"artifact_id": artifact_id, "kind": "file",
                       "path": str(matches[0]), "sha256": hashlib.sha256(raw).hexdigest(),
                       "content_ref": ref})
    events = stack.log.read_types(VERIFICATION_EVENTS, task_id=view["task_id"])
    results = []
    for check_id in check["inputs"]["required_check_ids"]:
        request = next((e for e in reversed(events)
                        if e.type == "verification.requested"
                        and e.payload["plan_id"] == view["plan_id"]
                        and e.payload["revision"] == view["revision"]
                        and e.payload["check_id"] == check_id), None)
        result = next((e for e in reversed(events)
                       if request and e.type == "verification.result"
                       and e.payload["attempt_id"] == request.payload["attempt_id"]), None)
        if (result is None or result.payload["result"] != "passed"
                or result.payload["input_fingerprint"] != fingerprint):
            raise ValueError("人工检查依赖的检查尚未通过或证据失效")
        results.append({"check_id": check_id, "event_seq": result.seq,
                        "attempt_id": result.payload["attempt_id"],
                        "evidence_refs": result.payload["evidence_refs"]})
    return frozen, results


def decide_check(stack, params, *, actor):
    from .goals import request_id as checked_request_id
    from .plans import PlanStore

    if not isinstance(actor, HumanIdentity):
        raise ValueError("人工验收只能由真实人类控制面提交")
    closed(params, {
        "format_version", "request_id", "task_id", "mission_digest", "plan_digest",
        "plan_id", "revision", "step_id", "check_id", "check_digest", "attempt_id",
        "expected_state_revision", "input_fingerprint", "evidence_refs", "decision", "reason",
    }, "人工验收请求")
    checked_request_id(params["request_id"])
    if (type(params["format_version"]) is not int or params["format_version"] != 1
            or params["decision"] not in ("accept", "reject")):
        raise ValueError("人工验收版本或决定无效")
    bounded_text(params["reason"], "验收说明", 4096)
    plans = PlanStore(stack)
    with stack.log.transaction() as tx:
        view = plans.get(params["plan_id"])
        if (view["task_id"] != params["task_id"]
                or jcs_sha256(view["mission"]) != params["mission_digest"]
                or view["document_digest"] != params["plan_digest"]
                or view["revision"] != params["revision"]):
            raise ValueError("人工验收作用域已经改变")
        check = next((c for c in view["manifest"]["checks"]
                      if c["check_id"] == params["check_id"]), None)
        if check is None or check["kind"] != "human" or jcs_sha256(check) != params["check_digest"]:
            raise ValueError("此入口只能决定当前人类检查")
        fields = {"format_version", "plan_id", "revision", "step_id", "check_id",
                  "check_digest", "attempt_id", "input_fingerprint", "evidence_refs",
                  "request_id", "decision", "reason"}
        decision = {k: params[k] for k in fields}
        events = stack.log.read_types(VERIFICATION_EVENTS, task_id=params["task_id"])
        old = next((e for e in events if e.type == "verification.decided"
                    and e.payload["request_id"] == params["request_id"]), None)
        if old:
            if old.payload != decision:
                raise ValueError("人工决定请求编号冲突")
            return next(e.payload for e in events if e.type == "verification.result"
                        and e.payload["attempt_id"] == params["attempt_id"])
        if view["state_revision"] != params["expected_state_revision"] or not view["confirmed"]:
            raise ValueError("验收页面已过期")
        request = next((e for e in reversed(events) if e.type == "verification.requested"
                        and e.payload["attempt_id"] == params["attempt_id"]), None)
        if request is None or any(
            request.payload[k] != params[k] for k in (
                "plan_id", "revision", "step_id", "check_id", "check_digest",
                "input_fingerprint", "evidence_refs",
            )
        ):
            raise ValueError("待验 attempt 与页面绑定不符")
        latest = next((e for e in reversed(events) if e.type == "verification.requested"
                       and e.payload["plan_id"] == params["plan_id"]
                       and e.payload["step_id"] == params["step_id"]
                       and e.payload["check_id"] == params["check_id"]), None)
        if latest is None or latest.seq != request.seq or any(
            e.type == "verification.result" and e.payload["attempt_id"] == params["attempt_id"]
            for e in events
        ):
            raise ValueError("该 attempt 已结束或已被替代")
        if input_fingerprint(stack, params["task_id"]) != params["input_fingerprint"]:
            raise ValueError("验收材料已经变化")
        task = stack.tasks.get_task(params["task_id"])
        for run in stack.tasks.list_runs(task.task_id):
            if run.effect_state in ("unknown", "partial"):
                raise ValueError("存在未知副作用，不能以人工验收解除")
        seq = tx.append(request.run_id, "verification.decided", decision,
                        task_id=task.task_id, actor_kind="human", actor_id=actor.actor)
        result = {k: v for k, v in request.payload.items() if k != "request_id"}
        result.update(result="passed" if params["decision"] == "accept" else "failed",
                      reason_code="verification.human_decision", verified_event_seq=seq)
        tx.append(request.run_id, "verification.result", result,
                  task_id=task.task_id, actor_kind="core")
        if params["decision"] == "accept" and step_verified(stack, view, params["step_id"]):
            plans.change_step(view["plan_id"], params["step_id"], "satisfied",
                              evidence_refs=params["evidence_refs"],
                              request_id="decision_" + params["request_id"])
        elif params["decision"] == "reject":
            plans.change_step(view["plan_id"], params["step_id"], "failed",
                              reason="verification.human_rejected",
                              request_id="decision_" + params["request_id"])
        return result
