"""验收通过事实必须仍能读回同一条完整材料链。"""

from __future__ import annotations

import hashlib

from .agent_documents import EVIDENCE_SCHEMA, validate_document
from .canonical import jcs_sha256
from .errors import TianshuError
from .plans import read_material

_BINDINGS = (
    "plan_id", "revision", "step_id", "check_id", "check_digest", "attempt_id",
    "input_fingerprint",
)
_KINDS = {
    "file_exists": "file_observation", "file_hash": "file_observation",
    "source_reference": "source_snapshot", "command_exit": "command_result",
    "human": "human_decision",
}


def evidence_verified(stack, view, check, request, result):
    """缺件、损坏、不可见或绑定不符一律不能沿用 passed。"""
    try:
        _verify_result(stack, view, check, request, result, set())
    except (ValueError, OSError, TianshuError, KeyError, TypeError, IndexError):
        return False
    return True


def human_request_verified(stack, view, check, request):
    """人工决定落账前核对冻结材料；缺件时不能先写 passed 再投影失效。"""
    try:
        p = request.payload
        if (check["kind"] != "human" or p["plan_id"] != view["plan_id"]
                or p["revision"] != view["revision"] or p["check_id"] != check["check_id"]
                or p["check_digest"] != jcs_sha256(check) or not p["evidence_refs"]):
            return False
        verified = set()
        for ref in p["evidence_refs"]:
            evidence, material = _evidence(stack, view, check, request, ref)
            _human_material(stack, view, check, evidence, material, verified)
    except (ValueError, OSError, TianshuError, KeyError, TypeError, IndexError):
        return False
    return True


def _event(stack, seq, task_id, run_id, kind):
    events = stack.log.read_after(seq - 1, limit=1)
    if (not events or events[0].seq != seq or events[0].task_id != task_id
            or events[0].run_id != run_id or events[0].type != kind):
        raise ValueError("验收材料来源事件绑定无效")
    return events[0]


def _read_content(stack, task_id, ref):
    # read 在返回任意一页前读取完整 blob 并重算摘要，同时核对任务可见性。
    stack.contents.read(ref, task_id=task_id, length=1, encoding="base64")


def _evidence(stack, view, check, event, ref):
    evidence = read_material(stack, view["task_id"], ref)
    validate_document(EVIDENCE_SCHEMA, evidence, "已保存证据")
    expected = {
        "task_id": view["task_id"], "mission_id": view["mission"]["mission_id"],
        "plan_digest": view["document_digest"], "step_id": event.payload["step_id"],
        "run_id": event.run_id, "attempt_id": event.payload["attempt_id"],
        "input_fingerprint": event.payload["input_fingerprint"], "kind": _KINDS[check["kind"]],
    }
    if any(evidence[k] != value for k, value in expected.items()) or not evidence["content_refs"]:
        raise ValueError("已保存证据与验收作用域不符")
    for content_ref in evidence["content_refs"]:
        _read_content(stack, view["task_id"], content_ref)
    return evidence, read_material(stack, view["task_id"], evidence["content_refs"][0])


def _verify_result(stack, view, check, request, result, verified):
    if result.seq in verified:
        return
    p, q = request.payload, result.payload
    if (q["result"] != "passed" or request.run_id != result.run_id
            or any(p[k] != q[k] for k in _BINDINGS)
            or p["plan_id"] != view["plan_id"] or p["revision"] != view["revision"]
            or p["check_id"] != check["check_id"] or p["check_digest"] != jcs_sha256(check)
            or not p["evidence_refs"] or not q["evidence_refs"]):
        raise ValueError("验收请求与结果绑定不符")
    if check["kind"] != "command_exit" and p["evidence_refs"] != q["evidence_refs"]:
        raise ValueError("只读或人工验收不能替换已冻结证据")
    for ref in set(p["evidence_refs"]) | set(q["evidence_refs"]):
        event = result if ref in q["evidence_refs"] else request
        evidence, material = _evidence(stack, view, check, event, ref)
        if ref not in q["evidence_refs"]:
            continue
        if check["kind"] == "command_exit":
            _command_material(stack, view, check, result, evidence, material)
        elif check["kind"] == "human":
            decision = _event(stack, q["verified_event_seq"], view["task_id"],
                              result.run_id, "verification.decided")
            if (decision.payload["decision"] != "accept"
                    or any(decision.payload[k] != p[k] for k in (*_BINDINGS, "evidence_refs"))):
                raise ValueError("人工决定与待审证据不符")
            _human_material(stack, view, check, evidence, material, verified)
        else:
            if q["verified_event_seq"] != request.seq or material["kind"] != evidence["kind"]:
                raise ValueError("观察材料与请求不符")
            if check["kind"] == "source_reference":
                if material != {"kind": "source_snapshot", **check["inputs"]}:
                    raise ValueError("来源材料与已确认检查不符")
                _read_content(stack, view["task_id"], material["content_ref"])
    verified.add(result.seq)


def _command_material(stack, view, check, result, evidence, material):
    seq = result.payload["verified_event_seq"]
    source = _event(stack, seq, view["task_id"], result.run_id, "tool.result")
    if (evidence["event_seqs"] != [seq] or material["kind"] != "command_result"
            or material["binding"] != check["inputs"]
            or material["input_fingerprint"] != result.payload["input_fingerprint"]
            or material["result_event_seq"] != seq
            or material["result_digest"] != jcs_sha256(source.payload)
            or material["status"] != source.payload["status"]
            or source.payload["tool_id"] != "run_command"
            or source.payload["call_id"] != "verification_" + check["check_id"]):
        raise ValueError("命令材料与实际执行结果不符")
    output = material["output"]
    for key in ("exit_code", "signal", "timed_out", "truncated", "stdout_ref", "stderr_ref"):
        if output[key] != source.payload["output"].get(key):
            raise ValueError("命令材料替换了实际输出")
    if (output["exit_code"] not in check["inputs"]["expected_exit_codes"]
            or output["signal"] or output["timed_out"]):
        raise ValueError("命令实际结果没有通过检查")
    for key in ("stdout", "stderr"):
        text = source.payload["output"].get(key)
        if (not isinstance(text, str) or output.get(key + "_snapshot") !=
                "content://sha256:" + hashlib.sha256(text.encode()).hexdigest()
                or output[key + "_snapshot"] not in evidence["content_refs"]):
            raise ValueError("命令输出快照与实际输出不符")
    if output["truncated"] and not (output["stdout_ref"] or output["stderr_ref"]):
        raise ValueError("截断命令输出没有完整原文")
    snapshots = getattr(stack.dispatcher, "_snapshots", None)
    blob_dir = snapshots.blob_dir if snapshots is not None else stack.log.blob_dir
    for key in ("stdout_ref", "stderr_ref"):
        digest = output[key]
        if digest is None:
            continue
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest) or blob_dir is None):
            raise ValueError("完整命令输出引用无效")
        with (blob_dir / digest).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("完整命令输出摘要不符")


def _human_material(stack, view, check, evidence, material, verified):
    if (material["kind"] != "human_review_material"
            or material["question"] != check["inputs"]["question"]
            or [a["artifact_id"] for a in material["artifacts"]]
            != check["inputs"]["required_artifact_ids"]
            or [c["check_id"] for c in material["checks"]]
            != check["inputs"]["required_check_ids"]
            or evidence["event_seqs"] != [c["event_seq"] for c in material["checks"]]):
        raise ValueError("人工验收材料与检查不符")
    for artifact in material["artifacts"]:
        _read_content(stack, view["task_id"], artifact["content_ref"])
    events = stack.log.read_types(
        ("verification.requested", "verification.result"), task_id=view["task_id"])
    checks = {c["check_id"]: c for c in view["manifest"]["checks"]}
    for dependency in material["checks"]:
        result = next((e for e in events if e.seq == dependency["event_seq"]), None)
        request = next((e for e in events if e.type == "verification.requested"
                        and e.payload["attempt_id"] == dependency["attempt_id"]), None)
        if (request is None or result is None or result.type != "verification.result"
                or result.payload["check_id"] != dependency["check_id"]
                or result.payload["attempt_id"] != dependency["attempt_id"]
                or result.payload["evidence_refs"] != dependency["evidence_refs"]):
            raise ValueError("人工验收依赖材料与已保存检查不符")
        _verify_result(stack, view, checks[dependency["check_id"]], request, result, verified)
