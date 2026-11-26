"""模型规划响应解析；模型提出文档，确认及状态由内核事件决定。"""

from __future__ import annotations

import copy
import json

from .canonical import jcs_sha256
from .plans import PlanStore, closed
from .workflow import final_text


def planning_contract():
    """送入规划 Run 的机器可读形状；不含任何工具授权。"""
    return {
        "top_level": ["plan", "verification_manifest"],
        "plan": {
            "format_version": 1, "plan_id": "unique_ascii_id", "revision": 1,
            "mission_id": "copy Mission.mission_id", "mission_digest": "copy supplied digest",
            "previous_digest": None,
            "verification_manifest": {"content_ref": "$core", "digest": "$core"},
            "steps": [{
                "step_id": "step_id", "title": "nonempty title",
                "instruction": "bounded instruction",
                "depends_on": [], "criterion_ids": ["Mission criterion_id"],
                "expected_artifacts": [{"artifact_id": "id", "kind": "file|content|source",
                                        "locator": "workspace-relative path or content ref"}],
                "verification_ids": ["check_id"],
            }],
        },
        "verification_manifest": {
            "format_version": 1, "mission_id": "copy Mission.mission_id",
            "mission_digest": "copy supplied digest",
            "checks": [{
                "format_version": 1, "check_id": "id", "criterion_ids": ["id"],
                "required": True, "kind": "file_exists",
                "inputs": {"workspace_binding_id": "0", "relative_path": "artifact.txt",
                           "file_kind": "regular"},
            }],
        },
        "other_checks": {
            "file_hash": ["workspace_binding_id", "relative_path", "file_kind",
                          "algorithm=sha256", "expected_digest"],
            "command_exit": ["executable", "executable_digest", "argv", "cwd_binding_id",
                             "env_profile_digest", "expected_exit_codes", "timeout_ms",
                             "isolation_profile_digest"],
            "source_reference": ["content_ref", "source_event_seq", "cited_digest", "locator"],
            "human": ["question", "required_artifact_ids", "required_check_ids"],
        },
        "digest_policy": (
            "Mission digest is supplied separately. Only verification_manifest binding may be "
            "the exact {$core,$core} placeholder: core computes its content address. "
            "Replans must increment revision and copy previous_digest."
        ),
    }


def consume_candidate(coordinator, goal, run_id):
    text = final_text(coordinator.log, coordinator.tasks.get_run(run_id))
    if len(text.encode()) > 256 * 1024:
        raise ValueError("规划响应超过限额")

    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("规划 JSON 包含重复字段")
            result[k] = v
        return result
    candidate = json.loads(text, object_pairs_hook=pairs)
    closed(candidate, {"plan", "verification_manifest"}, "规划响应")
    document, manifest = candidate["plan"], candidate["verification_manifest"]
    mission = coordinator.mission(goal["goal_id"])
    if document.get("verification_manifest") == {"content_ref": "$core", "digest": "$core"}:
        document = copy.deepcopy(document)
        digest = jcs_sha256(manifest)
        document["verification_manifest"] = {"content_ref": "content://sha256:" + digest,
                                             "digest": digest}
    plans = PlanStore(coordinator.stack)
    if goal["phase"] == "plan":
        return plans.create(
            goal["task_id"], mission, document, manifest, source_run_id=run_id,
            request_id=run_id + "_candidate")
    previous = plans.get(goal["plan_id"])
    # 已满足步骤不能被自动重写；检查、资源、依赖或 ID 改动需要重新人工确认。
    bounded = goal["config"]["replan_policy"] == "bounded" and previous["confirmed"]
    if bounded:
        if (manifest != previous["manifest"]
                or len(document.get("steps", [])) != len(previous["document"]["steps"])):
            bounded = False
        else:
            old_states = {s["step_id"]: s["state"] for s in previous["steps"]}
            for before, after in zip(previous["document"]["steps"], document["steps"], strict=True):
                if old_states[before["step_id"]] == "satisfied":
                    if before != after:
                        bounded = False
                elif ({k: v for k, v in before.items() if k not in ("title", "instruction")}
                      != {k: v for k, v in after.items() if k not in ("title", "instruction")}):
                    bounded = False
    result = plans.replan(
        previous["plan_id"], mission, document, manifest,
        expected_revision=previous["revision"], request_id=run_id + "_candidate")
    if bounded:
        coordinator.log.append("", "plan.confirmed", {
            "format_version": 1, "plan_id": result["plan_id"], "revision": result["revision"],
            "document_digest": result["document_digest"],
            "request_id": run_id + "_bounded_confirm",
            "goal_revision": goal["config"]["goal_revision"],
        }, task_id=goal["task_id"], actor_kind="core")
        result = plans.get(result["plan_id"])
    return result
