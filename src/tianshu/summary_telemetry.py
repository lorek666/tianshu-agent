"""语义摘要的追加式旁路遥测；不进入主消息流，不参与结算或重试。"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .canonical import jcs_sha256
from .errors import TianshuError
from .events import canonical_bytes, sha256_hex

if TYPE_CHECKING:
    from .events import EventLog
    from .model import ModelIdentity
    from .pricing import CostRecord
    from .summaries import SummaryRecord


SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_model_calls (
    summary_id TEXT PRIMARY KEY REFERENCES summary_versions(summary_id),
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summary_model_usage (
    summary_id TEXT PRIMARY KEY REFERENCES summary_model_calls(summary_id),
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS summary_model_calls_no_replace
BEFORE INSERT ON summary_model_calls
WHEN EXISTS (SELECT 1 FROM summary_model_calls WHERE summary_id=NEW.summary_id) BEGIN
    SELECT RAISE(ABORT, 'summary telemetry cannot be replaced');
END;
CREATE TRIGGER IF NOT EXISTS summary_model_usage_no_replace
BEFORE INSERT ON summary_model_usage
WHEN EXISTS (SELECT 1 FROM summary_model_usage WHERE summary_id=NEW.summary_id) BEGIN
    SELECT RAISE(ABORT, 'summary telemetry cannot be replaced');
END;
CREATE TRIGGER IF NOT EXISTS summary_model_calls_no_update
BEFORE UPDATE ON summary_model_calls BEGIN
    SELECT RAISE(ABORT, 'summary telemetry is append-only');
END;
CREATE TRIGGER IF NOT EXISTS summary_model_calls_no_delete
BEFORE DELETE ON summary_model_calls BEGIN
    SELECT RAISE(ABORT, 'summary telemetry is append-only');
END;
CREATE TRIGGER IF NOT EXISTS summary_model_usage_no_update
BEFORE UPDATE ON summary_model_usage BEGIN
    SELECT RAISE(ABORT, 'summary telemetry is append-only');
END;
CREATE TRIGGER IF NOT EXISTS summary_model_usage_no_delete
BEFORE DELETE ON summary_model_usage BEGIN
    SELECT RAISE(ABORT, 'summary telemetry is append-only');
END;
"""

_REQUEST_SCHEMA = "tianshu.summary-call.v1"
_RESPONSE_SCHEMA = "tianshu.summary-usage.v1"
_USAGE_KEYS = {
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
}
_REQUEST_KEYS = {
    "schema", "summary_id", "material_id", "run_id", "task_id", "attempt_id",
    "prompt_version", "summary_version", "messages_hash", "provider", "adapter", "model", "policy",
}


def _require(condition: bool) -> None:
    if not condition:
        raise TianshuError("摘要缓存遥测损坏、版本不支持或身份不一致")


def _digest(value: object) -> bool:
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _count(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _decode(row) -> dict:
    try:
        payload = json.loads(row["payload"])
        raw = canonical_bytes(payload)
        _require(type(payload) is dict and raw.decode("utf-8") == row["payload"])
        _require(sha256_hex(raw) == row["payload_hash"])
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise TianshuError("摘要缓存遥测无法读取") from exc


def _request(payload: dict, record) -> None:
    from .summaries import STATUSES

    _require(set(payload) == _REQUEST_KEYS and payload["schema"] == _REQUEST_SCHEMA)
    for name in (
        "summary_id", "material_id", "run_id", "task_id", "attempt_id",
        "prompt_version", "summary_version",
    ):
        _require(type(payload[name]) is str and payload[name] == record[name])
    _require(all(_digest(payload[key]) for key in ("summary_id", "material_id", "messages_hash")))
    _require(type(payload["adapter"]) is str)
    _require(all(payload[key] is None or type(payload[key]) is str
                 for key in ("provider", "model", "policy")))
    _require(type(record["version"]) is int and record["version"] >= 1)
    _require(record["status"] in STATUSES)
    _require(type(record["model"]) is str)
    _require(jcs_sha256({key: record[key] for key in (
        "material_id", "version", "model", "prompt_version", "summary_version",
    )}) == record["summary_id"])
    if payload["adapter"]:
        _require(record["model"] == ":".join((
            payload["adapter"], payload["provider"] or "", payload["model"] or "",
        )))


def _response(payload: dict, request: dict, request_hash: str) -> None:
    _require(
        set(payload) == {"schema", "request_hash", "usage", "cost"}
        and payload["schema"] == _RESPONSE_SCHEMA
        and payload["request_hash"] == request_hash
    )
    usage, cost = payload["usage"], payload["cost"]
    _require(type(usage) is dict and set(usage) <= _USAGE_KEYS)
    _require({"input_tokens", "output_tokens"} <= set(usage))
    _require(all(_count(value) for value in usage.values()))
    if request["provider"] == "openai" and request["adapter"] in (
        "openai-compatible", "openai-responses",
    ) and usage["input_tokens"] is not None:
        reported = [usage[key] for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
                    if usage.get(key) is not None]
        _require(sum(reported) <= usage["input_tokens"])
    _require(type(cost) is dict and set(cost) == {
        "provider", "adapter", "model", "units", "unit_price_ver", "cost", "currency",
    })
    _require(all(cost[key] == request[key] for key in ("provider", "adapter", "model")))
    _require(type(cost["units"]) is dict and all(_count(v) for v in cost["units"].values()))
    _require(cost["units"] == {key: usage[key] for key in ("input_tokens", "output_tokens")})
    _require(_count(cost["cost"]))
    _require(all(cost[key] is None or type(cost[key]) is str
                 for key in ("unit_price_ver", "currency")))


def begin_call(
    log: EventLog, record: SummaryRecord, *,
    messages: list[dict], identity: ModelIdentity | None, policy: str | None,
) -> None:
    """预占已获准后、网络调用前持久化；记录失败则不发送。"""
    payload = {
        "schema": _REQUEST_SCHEMA,
        **{key: getattr(record, key) for key in (
            "summary_id", "material_id", "run_id", "task_id", "attempt_id",
            "prompt_version", "summary_version",
        )},
        "messages_hash": sha256_hex(canonical_bytes(messages)),
        "provider": identity.vendor or None if identity is not None else None,
        "adapter": identity.adapter if identity is not None else "",
        "model": identity.model or None if identity is not None else None,
        "policy": policy,
    }
    with log.transaction():
        saved = log.conn.execute(
            "SELECT * FROM summary_versions WHERE summary_id=?", (record.summary_id,),
        ).fetchone()
        _require(saved is not None and saved["status"] == "pending")
        _request(payload, saved)
        raw = canonical_bytes(payload)
        log.conn.execute(
            "INSERT INTO summary_model_calls VALUES (?,?,?)",
            (record.summary_id, raw.decode("utf-8"), sha256_hex(raw)),
        )


def finish_call(
    log: EventLog, summary_id: str, *, usage: dict, cost: CostRecord,
) -> None:
    """保存唯一最终回执；无回执、异常或硬退出继续表现为未知。"""
    with log.transaction():
        row = log.conn.execute(
            "SELECT * FROM summary_model_calls WHERE summary_id=?", (summary_id,),
        ).fetchone()
        _require(row is not None)
        request = _decode(row)
        payload = {
            "schema": _RESPONSE_SCHEMA, "request_hash": row["payload_hash"],
            "usage": dict(usage), "cost": cost.to_payload(),
        }
        _response(payload, request, row["payload_hash"])
        raw = canonical_bytes(payload)
        log.conn.execute(
            "INSERT INTO summary_model_usage VALUES (?,?,?)",
            (summary_id, raw.decode("utf-8"), sha256_hex(raw)),
        )


def read_calls(log: EventLog) -> list[dict]:
    """只读旧库时不建表；校验版本/摘要身份/调用哈希后才交给报告。"""
    tables = {row[0] for row in log.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('summary_model_calls','summary_model_usage')",
    )}
    if not tables:
        return []
    _require(tables == {"summary_model_calls", "summary_model_usage"})
    responses = {row["summary_id"]: row for row in log.conn.execute(
        "SELECT * FROM summary_model_usage",
    )}
    calls = []
    for row in log.conn.execute("SELECT * FROM summary_model_calls ORDER BY summary_id"):
        request = _decode(row)
        saved = log.conn.execute(
            "SELECT * FROM summary_versions WHERE summary_id=?", (row["summary_id"],),
        ).fetchone()
        _require(saved is not None)
        _request(request, saved)
        response_row = responses.pop(row["summary_id"], None)
        response = _decode(response_row) if response_row is not None else None
        if response is not None:
            _response(response, request, row["payload_hash"])
        calls.append({
            "request": request, "request_hash": row["payload_hash"],
            "response": response,
            "response_hash": response_row["payload_hash"] if response_row is not None else None,
        })
    _require(not responses)
    return calls
