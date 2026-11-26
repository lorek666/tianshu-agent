"""从有限授权事件恢复额度与消费事实，不从缓存补签授权。"""

from __future__ import annotations

import json

from .consent_contract import CONSENT_EVENT_SCHEMAS
from .errors import TianshuError
from .event_schema import validate_payload


def rebuild_projection(log):
    conn = log.conn
    unknown = conn.execute(
        "SELECT DISTINCT type FROM events WHERE type LIKE 'consent.%'",
    ).fetchall()
    if any(row["type"] not in CONSENT_EVENT_SCHEMAS for row in unknown):
        raise TianshuError("有限授权历史包含未知事件类型")
    old = {row["consent_id"]: row for row in conn.execute("SELECT * FROM scoped_consents")}
    for row in old.values():
        validate_payload("consent.issued", json.loads(row["document"]), 1)
        if (row["state"] not in ("active", "revoked", "expired")
                or type(row["remaining_uses"]) is not int or row["remaining_uses"] < 0
                or type(row["used_bytes"]) is not int or row["used_bytes"] < 0):
            raise TianshuError("有限授权投影含未知状态或非法额度")
    pending = conn.execute("SELECT * FROM consent_selections").fetchall()
    if any(row["used"] not in (0, 1) for row in pending):
        raise TianshuError("有限授权选择投影含未知消费状态")
    states, selections, issued_requests = {}, {}, set()
    for event in log.read_types(CONSENT_EVENT_SCHEMAS):
        p = event.payload
        validate_payload(event.type, p, event.payload_version)
        consent_id = p["consent_id"]
        if event.type == "consent.issued":
            if (consent_id in states or p["request_id"] in issued_requests
                    or event.task_id != p["task_id"] or event.run_id != p["run_id"]
                    or event.actor_kind != "human" or event.actor_id != p["issued_by"]["actor"]):
                raise TianshuError("有限授权签发事件身份重复或绑定不符")
            matcher = p["matcher"]
            directory = matcher["kind"] == "directory_overwrite"
            if (p["max_uses"] >= 2**63 or p["expires_at"] >= 2**63
                    or p["action"] != ("overwrite" if directory else "exec")
                    or p["tool_binding"]["tool_id"] != (
                        "modify_text" if directory else "run_command")
                    or directory and (matcher["max_total_bytes"] < matcher["max_file_bytes"]
                                      or matcher["max_total_bytes"] >= 2**63)):
                raise TianshuError("有限授权签发历史限额或工具绑定不符")
            previous = old.get(consent_id)
            # V1 签发事实没有单调时钟锚点。仅保留同一份缓存的原期限；缓存丢失
            # 时以 0 拒绝继续使用，绝不能用当前墙钟重新发放一段 TTL。
            deadline = (previous["monotonic_expires"] if previous is not None
                        and json.loads(previous["document"]) == p
                        and previous["runtime_session_id"] == p["runtime_session_id"] else 0)
            if type(deadline) is not int or not 0 <= deadline < 2**63:
                raise TianshuError("有限授权单调时钟期限非法")
            states[consent_id] = {
                "consent_id": consent_id, "task_id": p["task_id"], "run_id": p["run_id"],
                "request_id": p["request_id"], "document": json.dumps(p), "state": "active",
                "remaining_uses": p["max_uses"], "used_bytes": 0,
                "monotonic_expires": deadline, "runtime_session_id": p["runtime_session_id"],
            }
            issued_requests.add(p["request_id"])
            continue
        row = states.get(consent_id)
        if (row is None or row["task_id"] != event.task_id or row["run_id"] != event.run_id
                or row["state"] != "active"):
            raise TianshuError("有限授权后续事件没有有效签发或任务绑定不符")
        if event.type == "consent.used":
            doc = json.loads(row["document"])
            count = p["bytes"]
            if (p["request_id"] in selections or row["remaining_uses"] <= 0
                    or p["remaining_uses"] != row["remaining_uses"] - 1
                    or doc["action"] == "exec" and count != 0
                    or doc["action"] == "overwrite" and (
                        count > doc["matcher"]["max_file_bytes"]
                        or row["used_bytes"] + count > doc["matcher"]["max_total_bytes"])):
                raise TianshuError("有限授权消费历史重复或超出已签发额度")
            row["remaining_uses"] -= 1
            row["used_bytes"] += count
            selections[p["request_id"]] = {
                "request_id": p["request_id"], "request_digest": p["request_digest"],
                "consent_id": consent_id, "used": 1,
            }
        else:
            if event.type == "consent.revoked" and event.actor_kind != "human":
                raise TianshuError("有限授权撤销缺少人类控制面身份")
            row["state"] = "revoked" if event.type == "consent.revoked" else "expired"
    prepared = {
        event.payload["request_id"]: event.payload["request_digest"]
        for event in log.read_types(("dispatch.requested",))
        if event.payload.get("rule_id") == "c4.scoped_consent"
    }
    for row in pending:
        if (row["used"] == 0 and row["request_id"] not in selections and row["consent_id"] in states
                and prepared.get(row["request_id"]) == row["request_digest"]):
            selections[row["request_id"]] = dict(row)
    conn.execute("DELETE FROM consent_selections")
    conn.execute("DELETE FROM scoped_consents")
    for row in states.values():
        conn.execute(
            "INSERT INTO scoped_consents VALUES (:consent_id,:task_id,:run_id,:request_id,"
            ":document,:state,:remaining_uses,:used_bytes,:monotonic_expires,:runtime_session_id)",
            row,
        )
    for row in selections.values():
        conn.execute(
            "INSERT INTO consent_selections VALUES (:request_id,:request_digest,:consent_id,:used)",
            row,
        )
