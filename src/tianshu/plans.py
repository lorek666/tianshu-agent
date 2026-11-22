"""不可变任务图、内容寻址材料及事件派生步骤视图。"""

from __future__ import annotations

import json
import re
from typing import Any

from .canonical import jcs_bytes, jcs_sha256
from .contents import MAX_PAGE, make_ref
from .fsutil import atomic_write_bytes
from .goals import request_id as checked_request_id
from .restore_access import ensure_material_write_allowed

MAX_DOCUMENT = 256 * 1024
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PLAN_EVENTS = ("plan.created", "plan.confirmed", "plan.step_changed")
STEP_STATES = (
    "pending", "ready", "running", "verifying", "satisfied", "failed", "blocked",
    "invalidated", "skipped",
)


def closed(value: Any, fields: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} 字段不匹配")


def bounded_text(value, name, maximum=8192):
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value.encode()) > maximum):
        raise ValueError(f"{name} 必须为有界非空文本")


def unique_ids(values, name, *, maximum=64, empty=True, pattern=ID):
    if (not isinstance(values, list) or len(values) > maximum or (not empty and not values)
            or any(not isinstance(i, str) or pattern.fullmatch(i) is None for i in values)
            or len(values) != len(set(values))):
        raise ValueError(f"{name} 必须是有界且不重复的 ID 列表")


def put_material(stack, task_id, value, *, run_id=""):
    data = jcs_bytes(value)
    if len(data) > MAX_DOCUMENT:
        raise ValueError("材料超过 256 KiB")
    import hashlib
    digest = hashlib.sha256(data).hexdigest()
    path = stack.contents.blob_dir / digest
    with ensure_material_write_allowed(stack.contents.blob_dir, owner_log=stack.log):
        if not path.exists():
            atomic_write_bytes(path, data)
        stack.contents.record(digest, data, task_id=task_id, run_id=run_id)
    return make_ref(digest)


def read_material(stack, task_id, ref):
    parts, offset = [], 0
    while True:
        page = stack.contents.read(ref, task_id=task_id, offset=offset, length=MAX_PAGE)
        parts.append(page.data)
        if sum(len(p.encode()) for p in parts) > MAX_DOCUMENT:
            raise ValueError("材料超过 256 KiB")
        if page.next_offset is None:
            return json.loads("".join(parts))
        offset = page.next_offset


def validate_graph(mission, document, manifest):
    from .agent_documents import PLAN_SCHEMA, validate_document
    from .mission import MissionSpecV1
    from .verification import validate_manifest

    MissionSpecV1.of(mission)
    validate_document(PLAN_SCHEMA, document, "Plan")
    closed(document, {
        "format_version", "plan_id", "revision", "mission_id", "mission_digest",
        "previous_digest", "verification_manifest", "steps",
    }, "Plan")
    if (type(document["format_version"]) is not int or document["format_version"] != 1
            or type(document["revision"]) is not int or document["revision"] < 1):
        raise ValueError("Plan 版本/修订无效")
    if (document["previous_digest"] is not None
            and (not isinstance(document["previous_digest"], str)
                 or re.fullmatch(r"[0-9a-f]{64}", document["previous_digest"]) is None)):
        raise ValueError("Plan 前修订摘要无效")
    unique_ids([document["plan_id"]], "plan_id")
    if (document["mission_id"] != mission["mission_id"]
            or document["mission_digest"] != jcs_sha256(mission)):
        raise ValueError("Plan Mission 绑定不匹配")
    validate_manifest(mission, manifest)
    closed(document["verification_manifest"], {"content_ref", "digest"}, "检查清单绑定")
    digest = jcs_sha256(manifest)
    if document["verification_manifest"] != {"content_ref": make_ref(digest), "digest": digest}:
        raise ValueError("检查清单摘要不匹配")
    steps = document["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise ValueError("步骤数量必须为 1–64")
    criteria = {c["criterion_id"]: c for c in mission["criteria"]}
    checks = {c["check_id"]: c for c in manifest["checks"]}
    ids = [s.get("step_id") for s in steps if isinstance(s, dict)]
    unique_ids(ids, "步骤")
    if len(ids) != len(steps):
        raise ValueError("步骤不是对象")
    artifacts = []
    covered = set()
    for step in steps:
        closed(step, {"step_id", "title", "instruction", "depends_on", "criterion_ids",
                      "expected_artifacts", "verification_ids"}, "步骤")
        bounded_text(step["title"], "步骤标题", 256)
        bounded_text(step["instruction"], "步骤说明")
        unique_ids(step["depends_on"], "依赖", maximum=63)
        unique_ids(step["criterion_ids"], "条件", empty=False,
                   pattern=re.compile(r"[A-Za-z0-9_.-]{1,128}"))
        unique_ids(step["verification_ids"], "检查", maximum=16, empty=False)
        if (set(step["depends_on"]) - set(ids) or step["step_id"] in step["depends_on"]
                or set(step["criterion_ids"]) - criteria.keys()
                or set(step["verification_ids"]) - checks.keys()):
            raise ValueError("步骤存在无效引用")
        if not isinstance(step["expected_artifacts"], list) or len(step["expected_artifacts"]) > 32:
            raise ValueError("预期产物无效")
        for artifact in step["expected_artifacts"]:
            closed(artifact, {"artifact_id", "kind", "locator"}, "产物")
            if artifact["kind"] not in ("file", "content", "source"):
                raise ValueError("产物类型无效")
            bounded_text(artifact["locator"], "产物定位", 4096)
            if artifact["kind"] == "file":
                from .verification import relative_file
                relative_file(artifact["locator"])
            else:
                from .contents import parse_ref
                parse_ref(artifact["locator"])
            artifacts.append(artifact["artifact_id"])
        for criterion_id in step["criterion_ids"]:
            matched = [checks[i] for i in step["verification_ids"]
                       if criterion_id in checks[i]["criterion_ids"]]
            if not any((c["kind"] == "human") == (criteria[criterion_id]["mode"] == "human")
                       for c in matched):
                raise ValueError("条件缺少对应的必需检查")
            covered.add(criterion_id)
    unique_ids(artifacts, "产物", maximum=2048)
    if covered != criteria.keys():
        raise ValueError("计划未覆盖全部条件")
    edges = {s["step_id"]: s["depends_on"] for s in steps}
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError("任务图存在环")
        if node in visited:
            return
        visiting.add(node)
        for dep in edges[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)
    for node in edges:
        visit(node)
    for check in checks.values():
        if check["kind"] == "human":
            if set(check["inputs"]["required_artifact_ids"]) - set(artifacts):
                raise ValueError("人工检查产物引用不存在")
    if len(jcs_bytes(document)) > MAX_DOCUMENT:
        raise ValueError("计划超过 256 KiB")


class PlanStore:
    def __init__(self, stack):
        self.stack, self.log = stack, stack.log

    def _events(self, plan_id):
        return [e for e in self.log.read_types(PLAN_EVENTS)
                if e.payload["plan_id"] == plan_id]

    def get(self, plan_id, *, revision=None):
        events = self._events(plan_id)
        created = [e for e in events if e.type == "plan.created"]
        if revision is not None:
            created = [e for e in created if e.payload["revision"] == revision]
        if not created:
            raise ValueError("计划不存在")
        event = created[-1]
        p = event.payload
        document = read_material(self.stack, event.task_id, p["document_ref"])
        mission = read_material(self.stack, event.task_id, p["mission_ref"])
        manifest = read_material(self.stack, event.task_id, p["manifest_ref"])
        if jcs_sha256(document) != p["document_digest"]:
            raise ValueError("计划摘要损坏")
        validate_graph(mission, document, manifest)
        task = self.stack.tasks.get_task(event.task_id)
        confirmed = any(e.type == "plan.confirmed" and
                        e.payload["document_digest"] == p["document_digest"] for e in events)
        states = {s["step_id"]: {"state": "pending", "attempt": 0,
                                 "execution_run_id": None, "evidence_refs": [],
                                 "reason_code": ""} for s in document["steps"]}
        previous = None
        if p["revision"] > 1:
            previous = self.get(plan_id, revision=p["revision"] - 1)
            if previous["document_digest"] != document["previous_digest"]:
                raise ValueError("计划修订链损坏")
            for old in previous["steps"]:
                if (old["step_id"] in states and old["step_id"] not in p["changed_step_ids"]
                        and old["state"] == "satisfied"):
                    states[old["step_id"]] = {k: old[k] for k in states[old["step_id"]]}
        for change in events:
            q = change.payload
            if change.type == "plan.step_changed" and q["revision"] == p["revision"]:
                states[q["step_id"]] = {k: q[k] for k in (
                    "state", "attempt", "execution_run_id", "evidence_refs", "reason_code")}
        stale = task.goal_revision != mission["goal_revision"]
        view = {
            "format_version": 1, "plan_id": plan_id, "task_id": event.task_id,
            "revision": p["revision"], "state_revision": events[-1].seq,
            "document_digest": p["document_digest"], "confirmed": confirmed and not stale,
            "mission": mission, "manifest": manifest, "document": document,
            "changed_step_ids": p["changed_step_ids"],
            "steps": [{**s, **states[s["step_id"]]} for s in document["steps"]],
        }
        from .verification import step_verified
        for s in document["steps"]:
            if states[s["step_id"]]["state"] == "satisfied" and not step_verified(
                self.stack, view, s["step_id"]
            ):
                states[s["step_id"]]["state"] = "invalidated"
        for s in document["steps"]:
            state = states[s["step_id"]]
            if stale:
                state["state"] = "invalidated"
            elif confirmed and state["state"] == "pending" and all(
                states[d]["state"] == "satisfied" for d in s["depends_on"]
            ):
                state["state"] = "ready"
        view["steps"] = [{**s, **states[s["step_id"]]} for s in document["steps"]]
        return view

    def create(self, task_id, mission, document, manifest, *, request_id,
               source_run_id=None):
        checked_request_id(request_id)
        validate_graph(mission, document, manifest)
        task = self.stack.tasks.get_task(task_id)
        if (task is None or mission["task_id"] != task_id
                or task.goal_revision != mission["goal_revision"]):
            raise ValueError("使命不属于当前任务修订")
        with self.log.transaction() as tx:
            prior = self._events(document["plan_id"])
            for e in prior:
                if e.type == "plan.created" and e.payload["request_id"] == request_id:
                    if e.payload["document_digest"] != jcs_sha256(document):
                        raise ValueError("request_id 已绑定不同的计划")
                    return self.get(document["plan_id"])
            if prior or document["revision"] != 1 or document["previous_digest"] is not None:
                raise ValueError("首修订必须为 1 且计划 ID 不重复")
            self._append(tx, task_id, mission, document, manifest, request_id, source_run_id)
        return self.get(document["plan_id"])

    def _append(self, tx, task_id, mission, document, manifest, request_id, source,
                changed_step_ids=None):
        refs = [put_material(self.stack, task_id, value) for value in
                (document, manifest, mission)]
        tx.append("", "plan.created", {
            "format_version": 1, "plan_id": document["plan_id"],
            "revision": document["revision"], "mission_id": mission["mission_id"],
            "mission_digest": jcs_sha256(mission), "document_ref": refs[0],
            "manifest_ref": refs[1], "mission_ref": refs[2],
            "document_digest": jcs_sha256(document),
            "previous_digest": document["previous_digest"],
            "changed_step_ids": (changed_step_ids if changed_step_ids is not None
                                 else [s["step_id"] for s in document["steps"]]),
            "source_run_id": source, "request_id": request_id,
        }, task_id=task_id, actor_kind="core")

    def confirm(self, plan_id, *, expected_revision, document_digest, request_id):
        checked_request_id(request_id)
        with self.log.transaction() as tx:
            view = self.get(plan_id)
            if view["revision"] != expected_revision or view["document_digest"] != document_digest:
                raise ValueError("计划已经改变，请刷新")
            task = self.stack.tasks.get_task(view["task_id"])
            if view["mission"]["goal_revision"] != task.goal_revision:
                raise ValueError("任务目标已改变")
            if not view["confirmed"]:
                tx.append("", "plan.confirmed", {
                    "format_version": 1, "plan_id": plan_id, "revision": expected_revision,
                    "document_digest": document_digest, "request_id": request_id,
                    "goal_revision": view["mission"]["goal_revision"],
                }, task_id=view["task_id"], actor_kind="human")
        return self.get(plan_id)

    def replan(self, plan_id, mission, document, manifest, *, expected_revision, request_id):
        checked_request_id(request_id)
        validate_graph(mission, document, manifest)
        with self.log.transaction() as tx:
            view = self.get(plan_id)
            if (view["revision"] != expected_revision
                    or document["plan_id"] != plan_id
                    or document["revision"] != expected_revision + 1
                    or document["previous_digest"] != view["document_digest"]):
                raise ValueError("计划修订冲突")
            task = self.stack.tasks.get_task(view["task_id"])
            if task.active_run_id or any(
                s["state"] in ("running", "verifying") for s in view["steps"]
            ):
                raise ValueError("活跃步骤不能热切计划")
            if jcs_sha256(mission) != jcs_sha256(view["mission"]):
                raise ValueError("重规划不能改变使命")
            old_steps = {s["step_id"]: s for s in view["document"]["steps"]}
            old_checks = {c["check_id"]: c for c in view["manifest"]["checks"]}
            new_checks = {c["check_id"]: c for c in manifest["checks"]}
            changed = {
                s["step_id"] for s in document["steps"]
                if old_steps.get(s["step_id"]) != s or any(
                    old_checks.get(c) != new_checks[c] for c in s["verification_ids"])
            }
            # 依赖链受影响的后继也不能借用旧证据。
            for _ in document["steps"]:
                descendants = {s["step_id"] for s in document["steps"]
                               if set(s["depends_on"]) & changed}
                if descendants <= changed:
                    break
                changed |= descendants
            self._append(tx, view["task_id"], mission, document, manifest, request_id, None,
                         changed_step_ids=sorted(changed))
        return self.get(plan_id)

    def change_step(self, plan_id, step_id, state, *, run_id=None, reason="",
                    evidence_refs=(), request_id):
        checked_request_id(request_id)
        if state not in STEP_STATES:
            raise ValueError("未知步骤状态")
        with self.log.transaction() as tx:
            view = self.get(plan_id)
            for event in self._events(plan_id):
                p = event.payload
                if event.type != "plan.step_changed" or p["request_id"] != request_id:
                    continue
                if (p["revision"] != view["revision"] or p["step_id"] != step_id
                        or p["state"] != state or p["reason_code"] != reason
                        or p["evidence_refs"] != list(evidence_refs)
                        or event.run_id != (run_id or "")):
                    raise ValueError("步骤请求编号已绑定不同迁移")
                return view
            step = next((s for s in view["steps"] if s["step_id"] == step_id), None)
            if step is None or not view["confirmed"]:
                raise ValueError("步骤不存在或计划未确认")
            allowed = {
                "ready": {"running", "blocked"}, "running": {"verifying", "failed", "blocked"},
                "verifying": {"satisfied", "failed", "blocked"},
                "failed": {"ready"}, "blocked": {"ready"},
            }
            if state not in allowed.get(step["state"], set()):
                raise ValueError("非法步骤迁移")
            if run_id is not None:
                run = self.stack.tasks.get_run(run_id)
                if (run is None or run.task_id != view["task_id"]
                        or run.goal_revision != view["mission"]["goal_revision"]):
                    raise ValueError("步骤 Run 不属于当前任务修订")
            if state == "satisfied":
                from .verification import step_verified
                if not step_verified(self.stack, view, step_id):
                    raise ValueError("步骤没有新鲜且完整的验收证据")
            tx.append(run_id or "", "plan.step_changed", {
                "format_version": 1, "plan_id": plan_id, "revision": view["revision"],
                "step_id": step_id, "state": state,
                "attempt": step["attempt"] + (state == "running"),
                "execution_run_id": run_id or step["execution_run_id"],
                "evidence_refs": list(evidence_refs), "reason_code": reason,
                "request_id": checked_request_id(request_id),
            }, task_id=view["task_id"], actor_kind="core")
        return self.get(plan_id)
