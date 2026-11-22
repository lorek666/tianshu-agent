"""缓存旁路观测；真实 usage、上下文事实和 C5 质量分开计量，不参与计费。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .eval.runner import SuiteRun

SCHEMA = "tianshu.prompt-cache.v1"


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def cache_usage(usage: dict, *, vendor: str | None, adapter: str) -> dict[str, Any]:
    """保持 provider 原始计数；分母语义未知时不套用官方公式。"""
    inp = _count(usage.get("input_tokens"))
    read = _count(usage.get("cache_read_input_tokens"))
    creation = _count(usage.get("cache_creation_input_tokens"))
    total = None
    if vendor == "openai" and adapter in ("openai-compatible", "openai-responses"):
        total = inp
    elif vendor == "anthropic" and adapter == "anthropic":
        if inp is not None and read is not None and creation is not None:
            total = inp + read + creation
    valid = read is not None and total is not None and read <= total
    if vendor == "openai" and total is not None and read is not None and creation is not None:
        valid = valid and read + creation <= total
    return {
        "input_tokens": inp,
        "output_tokens": _count(usage.get("output_tokens")),
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": creation,
        "total_input_tokens": total,
        "read_fraction": read / total if valid and total else None,
        "hit": read > 0 if valid else None,
    }


def summarize_usage(samples: list[dict]) -> dict[str, Any]:
    """比率的分子分母只来自同一可观测子集，同时暴露未知覆盖。"""
    known = [sample for sample in samples if sample["hit"] is not None]
    total = sum(sample["total_input_tokens"] for sample in known)
    read = sum(sample["cache_read_input_tokens"] for sample in known)
    return {
        "calls": len(samples),
        "complete_calls": len(known),
        "incomplete_calls": len(samples) - len(known),
        "observed_input_tokens": total if known else None,
        "observed_cache_read_tokens": read if known else None,
        "observed_token_read_fraction": read / total if total else None,
        "observed_request_hit_fraction": (
            sum(sample["hit"] for sample in known) / len(known) if known else None
        ),
        "reported_cache_creation_tokens": (
            sum(sample["cache_creation_input_tokens"] for sample in samples
                if sample["cache_creation_input_tokens"] is not None)
            if any(sample["cache_creation_input_tokens"] is not None for sample in samples)
            else None
        ),
        "unknown_cache_creation_calls": sum(
            sample["cache_creation_input_tokens"] is None for sample in samples
        ),
    }


def context_condition(context: dict | None, workflow: dict | None = None) -> str:
    """仅给已记录的变换贴标签；没有 drop 不证明没有工具输出 spill。"""
    if context is None:
        return "unknown"
    parts = []
    if workflow:
        if workflow.get("mode") == "compact":
            parts.append("compact_generation")
        elif workflow.get("mode") == "execute" and workflow.get("source_goal"):
            parts.append("compact")
    if context.get("memories"):
        parts.append("memory")
    if context.get("summaries"):
        parts.append("semantic_summary")
    if context.get("dropped_seqs"):
        parts.append(
            "responses_compact" if context.get("budget_version") == "ctx-responses-compact-v1"
            else "trimmed"
        )
    return "+".join(parts) if parts else "baseline_or_spilled"


def _requests(events) -> tuple[list[dict], int, int]:
    intents: dict[tuple[str, str], list] = defaultdict(list)
    workflows = {event.run_id: event.payload for event in events if event.type == "run.workflow"}
    results = {}
    duplicates = 0
    for event in events:
        key = event.run_id, event.payload.get("request_id", "")
        if event.type == "model.intent":
            intents[key].append(event)
        elif event.type == "model.result":
            if key in results:
                duplicates += 1
            else:
                results[key] = event
    samples = []
    paired = 0
    for key, event in results.items():
        prior = [intent for intent in intents[key] if intent.seq < event.seq]
        intent = prior[-1] if prior else None
        paired += int(intent is not None)
        cost = event.payload["cost"]
        context = None if intent is None else intent.payload.get("context")
        samples.append({
            "run_id": event.run_id, "request_id": key[1], "result_seq": event.seq,
            "result_hash": event.payload_hash,
            "intent_seq": None if intent is None else intent.seq,
            "messages_hash": None if intent is None else intent.payload["messages_hash"],
            "condition": context_condition(context, workflows.get(event.run_id)),
            "provider": cost.get("provider"), "adapter": cost.get("adapter", ""),
            "model": cost.get("model"), "cost": cost.get("cost"),
            **cache_usage(
                event.payload["usage"], vendor=cost.get("provider"),
                adapter=cost.get("adapter", ""),
            ),
        })
    return samples, sum(map(len, intents.values())) - paired, duplicates


def build_cache_report(
    suite: SuiteRun, quality_report: dict, *, policy: str,
) -> dict[str, Any]:
    """复用本次评测的报告结论；不重跑判据、不把模型终态当任务成功。"""
    if (quality_report["suite_version"] != suite.suite_version
            or quality_report["model"] != suite.identity):
        raise ValueError("缓存与质量报告不属于同一个 SuiteRun")
    quality = {
        (case["case_id"], run["run_id"], run["run_index"]): (case, run)
        for case in quality_report["cases"] for run in case["run_details"]
    }
    runs, requests = [], []
    for observation in suite.observations:
        case, checked = quality[(observation.case_id, observation.run_id, observation.run_index)]
        events = [*observation.events, *observation.subtask_events]
        if observation.followup is not None:
            events.extend(observation.followup.events)
        # 同库父/子取证可重叠，先按真实事件身份去重，避免把取证视图当额外调用。
        unique = {(event.run_id, event.seq, event.payload_hash): event for event in events}
        samples, unpaired, duplicates = _requests(
            sorted(unique.values(), key=lambda event: event.seq)
        )
        requests.extend({**sample, "case_id": observation.case_id, "scope": case["scope"],
                         "run_index": observation.run_index} for sample in samples)
        workflows = {
            event.run_id: event.payload for event in events if event.type == "run.workflow"
        }
        conditions = sorted({sample["condition"] for sample in samples} | {
            context_condition(event.payload.get("context"), workflows.get(event.run_id))
            for event in events if event.type == "model.intent"
        })
        valid = not checked["invalid"]
        root_results = [observation.result]
        if observation.followup is not None:
            root_results.append(observation.followup.result)
        auxiliary = None
        if all(result is not None and result.model_attempts_known for result in root_results):
            root_ids = {observation.run_id}
            if observation.followup is not None:
                root_ids.add(observation.followup.run_id)
            auxiliary = max(0, sum(result.model_attempts for result in root_results) - sum(
                event.type == "model.intent" and event.run_id in root_ids
                for event in unique.values()
            ))
        runs.append({
            "case_id": observation.case_id, "scope": case["scope"], "run_id": observation.run_id,
            "run_index": observation.run_index, "fixture_digest": case["fixture_digest"],
            "conditions": conditions,
            "condition": conditions[0] if len(conditions) == 1 else (
                "mixed" if conditions else "unobserved"
            ),
            "quality_passed": checked["passed"] if valid else None,
            "invalid": checked["invalid"], "unavailable": checked["unavailable"],
            "safety_failed": any(
                check["kind"] == "safety" and not check["passed"]
                for check in checked["checks"]
            ) if valid else None,
            "attempts_without_result": unpaired,
            "duplicate_results": duplicates,
            "auxiliary_attempts_without_cache_telemetry": auxiliary,
            **{name: checked[name] for name in (
                "elapsed_ms", "turns", "tokens", "cost_total_micros", "unknown_cost_calls",
            )},
            "usage": summarize_usage(samples),
        })
    usage_groups: dict[tuple, list] = defaultdict(list)
    quality_groups: dict[tuple[str, str], list] = defaultdict(list)
    for request in requests:
        usage_groups[(
            request["scope"], request["provider"], request["adapter"],
            request["model"], request["condition"],
        )].append(request)
    for run in runs:
        quality_groups[(run["scope"], run["condition"])].append(run)
    return {
        "schema": SCHEMA, "suite_version": suite.suite_version,
        "repeat": suite.repeat, "model": dict(suite.identity),
        "selection": quality_report.get("selection"),
        "primary_report_sha256": hashlib.sha256(json.dumps(
            quality_report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "configured_primary_policy": policy,
        "evidence": (
            "offline" if suite.identity.get("adapter") == "scripted" else "provider_configured"
        ),
        "quality_verdict": quality_report["verdict"],
        "comparison": "observational_only",
        "limitations": [
            "比例仅覆盖已上报完整用量的 model.result；缺失值不等于零或未命中。",
            "厂商不明的兼容端点保留计数但不推断官方分母；没有长度估算。",
            "baseline_or_spilled 无已记录的摘要/裁剪/记忆，不证明工具输出从未 spill。",
            "mixed 样本不把整项任务成功归因于单个请求；条件间不是随机对照实验。",
            "语义摘要辅助调用未保存缓存 usage，单列缺口；总成本以 C5 报告/账本为准。",
            "策略是本次调用方配置，不是历史事件中的逐请求策略事实。",
        ],
        "usage": summarize_usage(requests),
        "usage_by_provider_condition": [
            {"scope": key[0], "provider": key[1], "adapter": key[2],
             "model": key[3], "condition": key[4],
             **summarize_usage(samples)}
            for key, samples in sorted(usage_groups.items(), key=lambda item: str(item[0]))
        ],
        "quality_by_condition": [
            {"scope": scope, "condition": condition, "samples": len(group),
             "valid_samples": sum(not run["invalid"] for run in group),
             "invalid_samples": sum(run["invalid"] for run in group),
             "unavailable_samples": sum(run["unavailable"] is not None for run in group),
             "passed": sum(run["quality_passed"] is True for run in group),
             "failed": sum(run["quality_passed"] is False for run in group),
             "success_rate": (
                 sum(run["quality_passed"] is True for run in group)
                 / sum(not run["invalid"] for run in group)
                 if any(not run["invalid"] for run in group) else None
             ),
             "safety_failed": sum(run["safety_failed"] is True for run in group)}
            for (scope, condition), group in sorted(quality_groups.items())
        ],
        "runs": runs, "requests": requests,
    }
